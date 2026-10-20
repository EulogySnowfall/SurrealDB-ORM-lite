from __future__ import annotations

import inspect
import operator
import os
import re
import warnings
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, SupportsIndex, TypeAlias, cast

import pydantic

from . import geo as _geo
from ._sdk import RecordID, SdkRange, ServerError
from .constants import GEOMETRY_LOOKUP_OPERATORS, LOOKUP_OPERATORS, SESSION_PARAMETERS, TEXT_LOOKUP_FUNCTIONS
from .exceptions import SurrealDbAuthenticationError, SurrealDbConflictError, SurrealDbError, SurrealDbNotFoundError
from .functions import SurrealFunc, Var
from .subquery import SubqueryCompiler, inside_a_list, lookup_refused, subquery_condition, subquery_of
from .types import Range

if TYPE_CHECKING:
    from .model_base import BaseSurrealModel

# Pattern for valid field names: alphanumeric, underscores, dots (for nested fields)
# Must start with a letter or underscore
VALID_FIELD_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*$")

# Pattern for a field path followed by ``.*`` (``author.*``, ``meta.inner.*``): every field of the nested
# object or of the record a link points to. ``select()`` accepts it besides ``*`` and VALID_FIELD_PATTERN.
VALID_FIELD_STAR_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*\.\*$")

# Pattern for valid alias names: alphanumeric and underscores only
# Must start with a letter or underscore (like Python identifiers)
VALID_ALIAS_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# Pattern for valid graph traversal paths: arrow-separated segments
# e.g. ->follows->User, <-follows<-User, ->follows->User->likes->Post
VALID_GRAPH_PATH_PATTERN = re.compile(r"^(<-|->)[a-zA-Z_][a-zA-Z0-9_]*((<-|->)[a-zA-Z_][a-zA-Z0-9_]*)*$")

# Pattern for valid record thing strings: table:id where both parts are safe
# ID part allows alphanumeric, underscores, and hyphens, optionally backtick-quoted
VALID_THING_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*:(`[a-zA-Z0-9_-]+`|[a-zA-Z0-9_-]+)$")

# A record id that SurrealQL reads as a bare identifier. Anything else (a digit run, a
# hyphen, punctuation) must be backtick-quoted to keep its string form.
BARE_RECORD_ID = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# A ``str`` filter value that is ``$`` and a variable name (``"$max_age"``): the form the ORM read as a reference
# to that query variable until v0.28.1. It is data now; :func:`warn_on_variable_names` says so when the statement's
# ``variables()`` give that name. Used with ``fullmatch``.
VARIABLE_NAME_VALUE = re.compile(r"\$([a-zA-Z_][a-zA-Z0-9_]*)")

#: The largest ``limit()``/``offset()`` value: SurrealQL's largest integer (a signed 64-bit one).
MAX_PAGING_VALUE = 2**63 - 1

#: The ``DeprecationWarning`` of the ``"$$"`` escape (:func:`unescape_filter_value`): the same text whatever the value.
ESCAPE_DEPRECATED = (
    "A filter value starting with '$$' is read with one '$' less: the '$$' escape is deprecated. A string filter "
    "value is always data, so write it with one '$' less; Var('name') references a query variable."
)


_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))


def user_stacklevel(*, also_skip: tuple[str, ...] = ()) -> int:
    """``stacklevel`` that makes ``warnings.warn`` point at the first frame outside the ORM.

    A fixed ``stacklevel`` lands inside ``query_set.py``/``q.py``, and Python's default
    filters only surface a ``DeprecationWarning`` reported from ``__main__`` — so the
    deprecation runway never reached users at all (issue #156 follow-up). Call sites reach
    here through paths of differing depth, so the level is measured, not guessed.

    Args:
        also_skip: directories whose frames count as "inside" too, subdirectories included —
            e.g. Pydantic's, for a warning raised while its metaclass builds a model, so the
            warning lands on the user's ``class`` statement (v0.22.0).
    """
    frame = inspect.currentframe()
    level = 0
    while frame is not None and frame.f_back is not None:
        frame = frame.f_back
        level += 1
        directory = os.path.dirname(os.path.abspath(frame.f_code.co_filename))
        if directory == _PACKAGE_DIR:
            continue
        if any(directory == root or directory.startswith(root + os.sep) for root in also_skip):
            continue
        return level
    return 2


def split_statements(query: str) -> list[str]:
    """Split SurrealQL on the statement separator, ignoring ``;`` inside strings and comments.

    Deliberately a scanner and not a parser: it only needs to tell "one statement" from
    "several", and the string/comment forms below are the ones that can hide a semicolon.
    Single quotes, double quotes (with backslash escapes), ``--`` / ``//`` / ``#`` line
    comments and ``/* … */`` block comments are honoured; anything else is ordinary text.
    Comments are dropped rather than kept, so a query ending in a trailing comment still
    counts as one statement.
    """
    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 0
    length = len(query)

    while index < length:
        char = query[index]

        if quote is not None:
            current.append(char)
            if char == "\\" and index + 1 < length:
                current.append(query[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue

        if char in "'\"":
            quote = char
            current.append(char)
            index += 1
            continue

        if query.startswith("/*", index):
            end = query.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue

        if query.startswith("--", index) or query.startswith("//", index) or char == "#":
            end = query.find("\n", index)
            index = length if end == -1 else end
            continue

        if char == ";":
            statements.append("".join(current))
            current = []
            index += 1
            continue

        current.append(char)
        index += 1

    statements.append("".join(current))
    return [statement for statement in statements if statement.strip()]


def warn_on_multiple_statements(query: str) -> None:
    """Warn when a raw query carries more than one statement.

    The SDK returns the result set of the **first** statement only, and says nothing about
    the rest, so a two-statement query silently loses half its output (issue #156). Run one
    statement per call, or use ``transaction()`` when they must be atomic.
    """
    count = len(split_statements(query))
    if count > 1:
        warnings.warn(
            f"The query carries {count} statements but only the first one's results are "
            "returned by the SurrealDB SDK; run one statement per call, or use "
            "transaction() if they must be atomic.",
            UserWarning,
            stacklevel=user_stacklevel(),
        )


def non_negative_int(method: str, value: object) -> int:
    """*value* as the ``int`` that *method* (``"limit()"`` or ``"offset()"``) writes into its statement.

    An integer from ``0`` to :data:`MAX_PAGING_VALUE` (``2**63 - 1``, SurrealQL's largest integer) is returned as a
    plain ``int``: an ``int`` subclass, such as an ``IntEnum`` member, or an integer type with ``__index__``, such as
    NumPy's, gives its value. Anything else is refused, naming *method* and *value*: ``limit("10")`` and
    ``limit(-1)`` raise instead of reaching a statement. The server may still refuse a large value when the read runs
    (SurrealDB 2.x one above ``2**32 - 1``).

    Raises:
        TypeError: *value* is a ``bool`` or not an integer (no ``__index__``: a ``str``, a ``float``, ``None``).
        ValueError: *value* is negative, or above :data:`MAX_PAGING_VALUE`.
    """
    if isinstance(value, bool):
        raise TypeError(f"{method} takes a non-negative integer, got {value!r}")
    try:
        number = operator.index(cast(SupportsIndex, value))
    except TypeError:
        raise TypeError(f"{method} takes a non-negative integer, got {value!r}") from None
    if number < 0:
        raise ValueError(f"{method} takes a non-negative integer, got {value!r}")
    if number > MAX_PAGING_VALUE:
        raise ValueError(f"{method} takes an integer up to {MAX_PAGING_VALUE} (2**63 - 1), got {value!r}")
    return int(number)


def validate_field_name(field: str, context: str = "field") -> None:
    """
    Validate a field name to prevent SQL injection.

    Args:
        field: The field name to validate.
        context: Description of where the field is used (for error messages).

    Raises:
        ValueError: If the field name contains invalid characters.
    """
    if not field or not field.strip():
        raise ValueError(f"{context} name cannot be empty")
    if not VALID_FIELD_PATTERN.match(field):
        raise ValueError(
            f"Invalid {context} name '{field}': must contain only alphanumeric characters, "
            "underscores, and dots (for nested fields), and start with a letter or underscore"
        )


#: What ``select()`` accepts, as its refusals say it.
SELECT_ITEMS = (
    "select() takes '*', a field name or a dotted field path (letters, digits and underscores, each part "
    "starting with a letter or an underscore), optionally followed by '.*' (every field of a nested object or "
    "of a linked record); write a computed projection with query()"
)


def is_select_item(item: str) -> bool:
    """Whether *item* is a ``select()`` item: ``*``, a field path, or a field path followed by ``.*``.

    A field path is :data:`VALID_FIELD_PATTERN`'s (``name``, ``author.name``), as ``order_by()`` and ``values()``
    accept it; ``author.*`` is :data:`VALID_FIELD_STAR_PATTERN`'s. The whole string must match: a trailing newline
    is not part of a name.
    """
    return item == "*" or any(pattern.fullmatch(item) for pattern in (VALID_FIELD_PATTERN, VALID_FIELD_STAR_PATTERN))


def validate_select_item(item: object) -> str:
    """Check one ``select()`` item (:func:`is_select_item`) and return it.

    Raises:
        TypeError: *item* is not a ``str``.
        ValueError: *item* is not ``*``, a field path or a field path followed by ``.*``; the message names it.
    """
    if not isinstance(item, str):
        raise TypeError(f"select() takes field names as str, got {type(item).__name__} {item!r}")
    if not is_select_item(item):
        raise ValueError(f"select() cannot select {item!r}: {SELECT_ITEMS}")
    return item


def build_set_clause(merged: Mapping[str, Any], param_prefix: str = "_sv_") -> tuple[str, dict[str, Any]]:
    """Compile a ``SET field = value, …`` clause from a field mapping (v0.13.0).

    A :class:`~surreal_orm_lite.functions.SurrealFunc` value is **inlined** as a raw
    SurrealQL expression so the server evaluates it; every other value is **bound** to a
    ``$<param_prefix><field>`` query variable, which is the injection boundary::

        build_set_clause({"name": "Alice", "joined": SurrealFunc("time::now()")})
        # ("name = $_sv_name, joined = time::now()", {"_sv_name": "Alice"})

    Field names are validated as plain identifiers before being interpolated (a dotted
    nested path is rejected here: it would need a different binding strategy).

    Returns:
        The clause text and the variables to bind alongside it.

    Raises:
        ValueError: If ``merged`` is empty (``SET`` with no assignment is a parse error on
            both server lines), or if a field name is not a plain identifier.
    """
    if not merged:
        raise ValueError("build_set_clause() needs at least one field: an empty SET clause is invalid SurrealQL.")
    parts: list[str] = []
    variables: dict[str, Any] = {}
    for field, value in merged.items():
        validate_alias_name(field)
        if isinstance(value, SurrealFunc):
            parts.append(f"{field} = {value.expression}")
        else:
            var_name = f"{param_prefix}{field}"
            parts.append(f"{field} = ${var_name}")
            variables[var_name] = value
    return ", ".join(parts), variables


#: The ``CONTENT`` of a full replace that holds no ``datetime``: ``data`` bound whole, the statement sent before #261.
PLAIN_CONTENT = "$data"

#: A sent ``datetime`` (``{sent}``, its parameter) that keeps the stored value (``{stored}``, its path in ``$this``) when
#: that value floors to it at the microsecond (#261). Measured on SurrealDB 2.7.0, 3.3.4 and the embedded engine: a
#: stored value of another type, or none, compares false and gives the sent value.
_KEEP_STORED_DATETIME = "IF {sent} <= {stored} AND {stored} < {sent} + 1us THEN {stored} ELSE {sent} END"

#: The most values a full replace rebuilds to keep stored datetimes (#261): each top-level ``datetime`` it keeps counts
#: one, and so does each item of a sub-object or array it rebuilds below the top level. A kept datetime reads ``$this``
#: three times and the server copies the value it reads each time: without a cap, the cost of a long list of datetimes
#: grew with the square of its length. The top-level datetimes take it first, so up to 32 of them are always kept; what
#: is left goes to the fields below the top level in the order of the data, so whether one of those is kept depends on
#: the fields before it (#261 review).
_KEEP_LIMIT = 32

#: The deepest a kept ``datetime`` sits, in sub-objects and arrays below the top level (#261). Each level nests an
#: ``object::from_entries()`` or an array in the statement, and the servers refuse one nested too deep: a datetime five
#: sub-objects down already fails on SurrealDB 3.1.5 (17 on 3.3.4, 27 on 2.7.0 and the embedded engine).
_KEEP_MAX_DEPTH = 3

#: How :func:`replace_content` writes a value: ``True`` for a ``datetime`` that keeps the stored value, ``None`` for a
#: value bound whole, and for a sub-object or array it rebuilds, the plan of each of its items in turn.
_KeepPlan: TypeAlias = "Literal[True] | list[_KeepPlan | None]"


def _keep_plan(value: Any, depth: int, left: int) -> tuple[_KeepPlan | None, int]:
    """How a full replace writes ``value``, a sub-object or array ``depth`` levels below the top level, and what it costs (#261).

    ``(None, 0)`` when ``value`` is bound whole: it is not a ``dict`` (with ``str`` keys), a ``list`` or a ``tuple``,
    sits ``_KEEP_MAX_DEPTH`` levels down or more, has more items than ``left``, or keeps no stored datetime. Otherwise
    its items are rebuilt: each ``datetime`` keeps the stored value, each sub-object or array is planned in turn with
    what is left (and bound whole when it does not fit), and the cost is its number of items plus theirs, at most
    ``left``.
    """
    if depth >= _KEEP_MAX_DEPTH:
        return None, 0
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            return None, 0
        items: Iterable[Any] = value.values()
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        return None, 0
    cost = len(value)
    if cost > left:
        return None, 0
    plans: list[_KeepPlan | None] = []
    for item in items:
        if isinstance(item, datetime):
            plans.append(True)
            continue
        plan, inner = _keep_plan(item, depth + 1, left - cost)
        plans.append(plan)
        cost += inner
    if all(plan is None for plan in plans):
        return None, 0
    return plans, cost


def replace_content(data: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Compile the ``CONTENT`` of a full replace (``UPDATE``/``UPSERT $rid CONTENT …``) of ``data`` (#261).

    SurrealDB stores datetimes to the nanosecond, the SDK reads them floored to the microsecond: writing a
    ``datetime`` back unchanged would change the stored value, which a ``READONLY`` field refuses. So a ``datetime`` of
    ``data`` keeps the stored value when that value floors to it at the microsecond (``$this``, the record before the
    write, holds it; an ``UPSERT`` that creates the record has none and writes the sent value). Any other datetime is
    written as sent, so a changed ``READONLY`` value is still refused. ``data`` is rebuilt along the paths to the
    datetimes it keeps, with ``object::from_entries()`` and arrays, on SurrealDB 2.x and 3.x alike; every key and value
    is **bound** (``$c0``, ``$c1``…), nothing of ``data`` is written into the statement::

        replace_content({"title": "u", "created": dt})
        # ("object::from_entries([[$c0, $c1], [$c2, IF $c3 <= $this[$c2] AND $this[$c2] < $c3 + 1us THEN $this[$c2] ELSE $c3 END]])",
        #  {"c0": "title", "c1": "u", "c2": "created", "c3": dt})

    The statement stays bounded whatever the data (``_KEEP_LIMIT``, ``_KEEP_MAX_DEPTH``). The top-level datetimes are
    kept first, up to 32: that is the guarantee, whatever the other fields hold. Then, field by field in the order of
    ``data``, a sub-object or array is rebuilt to keep the datetimes it holds down to three levels below the top level,
    as long as the values it rebuilds fit in what is left of those 32 (:func:`_keep_plan`). That part depends on the
    data: a list of timestamped items before a sub-object can use up what is left, and the sub-object's datetime is then
    written as sent. Anything past the budget (a long list, a large sub-object, a deeper datetime) is bound whole and
    written as sent, and so is a set, whose items have no position to match a stored value by.

    Returns:
        The expression and the variables to bind alongside it: ``(PLAIN_CONTENT, {"data": data})`` when it keeps no
        stored datetime (``data`` holds none within those bounds, or has a key that is not a ``str``), the statement
        of before #261.
    """
    if not all(isinstance(key, str) for key in data):
        return PLAIN_CONTENT, {"data": data}
    left = _KEEP_LIMIT
    plans: dict[str, _KeepPlan] = {}
    for key, value in data.items():
        if isinstance(value, datetime) and left:
            plans[key] = True
            left -= 1
    for key, value in data.items():
        if not isinstance(value, datetime):
            plan, cost = _keep_plan(value, 0, left)
            if plan is not None:
                plans[key] = plan
                left -= cost
    if not plans:
        return PLAIN_CONTENT, {"data": data}
    variables: dict[str, Any] = {}

    def bind(value: Any) -> str:
        name = f"c{len(variables)}"
        variables[name] = value
        return f"${name}"

    def written(value: Any, plan: _KeepPlan | None, stored: str) -> str:
        """``value`` as ``plan`` writes it, ``stored`` being its path in ``$this``."""
        if plan is None:
            return bind(value)
        if plan is True:
            sent = bind(value)
            return _KEEP_STORED_DATETIME.format(sent=sent, stored=stored)
        if isinstance(value, dict):
            entries = []
            for (key, item), inner in zip(value.items(), plan, strict=True):
                bound = bind(key)
                entries.append(f"[{bound}, {written(item, inner, f'{stored}[{bound}]')}]")
            return f"object::from_entries([{', '.join(entries)}])"
        items = (
            written(item, inner, f"{stored}[{index}]") for index, (item, inner) in enumerate(zip(value, plan, strict=True))
        )
        return "[" + ", ".join(items) + "]"

    entries = []
    for key, value in data.items():
        bound = bind(key)
        entries.append(f"[{bound}, {written(value, plans.get(key), f'$this[{bound}]')}]")
    return f"object::from_entries([{', '.join(entries)}])", variables


def merge_extra_vars(
    variables: dict[str, Any],
    extra_vars: Mapping[str, Any] | None,
    reserved: Iterable[str] = (),
) -> dict[str, Any]:
    """Merge caller-supplied ``extra_vars`` into a statement's bindings (v0.13.0).

    ``extra_vars`` carries the values a ``SurrealFunc`` expression references (e.g.
    ``crypto::argon2::generate($password)`` with ``extra_vars={"password": raw}``), so
    user input stays a bound parameter instead of being formatted into the query.

    A key that would shadow an internal binding (the record id, or a ``$_sv_*`` field
    parameter produced by :func:`build_set_clause`) is rejected rather than silently
    overwriting it. ``reserved`` additionally rejects names the statement owns but may not
    have bound on this particular branch, so validation does not vary by code path.

    Returns:
        A new dict with both sets of bindings (``variables`` is left unmodified).

    Raises:
        ValueError: If any ``extra_vars`` key collides with an internal or reserved binding.
    """
    if not extra_vars:
        return dict(variables)
    conflicting = sorted((set(variables) | set(reserved)) & set(extra_vars))
    if conflicting:
        raise ValueError(
            f"extra_vars keys collide with internal query bindings: {conflicting}. Rename them (e.g. add a suffix)."
        )
    return {**variables, **extra_vars}


def validate_alias_name(alias: str) -> None:
    """
    Validate an alias name to prevent SQL injection.

    Args:
        alias: The alias name to validate.

    Raises:
        ValueError: If the alias name contains invalid characters.
    """
    if not alias or not alias.strip():
        raise ValueError("alias name cannot be empty")
    if not VALID_ALIAS_PATTERN.match(alias):
        raise ValueError(
            f"Invalid alias name '{alias}': must contain only alphanumeric characters "
            "and underscores, and start with a letter or underscore"
        )


def validate_edge_name(edge: str) -> None:
    """
    Validate an edge (relation table) name to prevent injection.

    Args:
        edge: The edge/relation name to validate.

    Raises:
        ValueError: If the edge name contains invalid characters.
    """
    if not edge or not edge.strip():
        raise ValueError("edge name cannot be empty")
    if not VALID_ALIAS_PATTERN.match(edge):
        raise ValueError(
            f"Invalid edge name '{edge}': must contain only alphanumeric characters "
            "and underscores, and start with a letter or underscore"
        )


def validate_graph_path(path: str) -> None:
    """
    Validate a graph traversal path to prevent injection.

    Accepts paths like ``->follows->User``, ``<-follows<-User``,
    or mixed ``->follows->User->likes->Post``.

    Args:
        path: The graph traversal path to validate.

    Raises:
        ValueError: If the path contains invalid characters or structure.
    """
    if not path or not path.strip():
        raise ValueError("graph path cannot be empty")
    if not VALID_GRAPH_PATH_PATTERN.match(path):
        raise ValueError(
            f"Invalid graph path '{path}': must be arrow-separated segments starting with -> or <- (e.g. '->follows->User')"
        )


def validate_thing(thing: str) -> None:
    """
    Validate a ``table:id`` record identifier to prevent injection.

    Args:
        thing: The record identifier in ``table:id`` format.

    Raises:
        ValueError: If the thing string is not a valid ``table:id`` format.
    """
    if not thing or not thing.strip():
        raise ValueError("record identifier cannot be empty")
    if not VALID_THING_PATTERN.match(thing):
        raise ValueError(
            f"Invalid record identifier '{thing}': must be in 'table:id' format "
            "with only alphanumeric characters, underscores, and hyphens"
        )


def format_record_id(id_value: Any) -> str:
    """Render a Python id value as the SurrealQL record-id literal that addresses that record.

    Keys off the value's *type*, not its textual shape: ``5`` is the integer record id ``5``
    (what the SDK writes for ``id: int``), while ``"5"`` is the string record id and must be
    backtick-quoted — unquoted, ``M:5`` would address a record the ORM never wrote (#156).
    """
    if isinstance(id_value, bool):
        raise ValueError("a record id cannot be a boolean")
    if isinstance(id_value, int):
        return str(id_value)
    text = str(id_value)
    if BARE_RECORD_ID.match(text):
        return text
    if "`" in text:
        raise ValueError(f"Invalid record id '{text}': a record id cannot contain a backtick")
    return f"`{text}`"


# The column that holds a record's identity. It is a RecordID on the row, never a string,
# so a lookup on it has to be coerced before binding (issue #159). A model that aliases its
# identity through ``primary_key`` still stores that alias as an ordinary column alongside
# ``id``, and that column is a plain value — only ``id`` itself is coerced.
RECORD_ID_COLUMN = "id"

# Lookups whose operand is a record id, and therefore get coerced.
_RECORD_ID_SCALAR_LOOKUPS = frozenset({"exact", "gt", "gte", "lt", "lte"})
_RECORD_ID_SEQUENCE_LOOKUPS = frozenset({"in", "not_in"})

# Lookups that treat their operand as text or as a collection. A record id is neither, and
# SurrealDB answers an empty set rather than an error — the silent failure #159 is about.
_RECORD_ID_INVALID_LOOKUPS = frozenset(
    {
        "like",
        "ilike",
        "contains",
        "not_contains",
        "containsall",
        "containsany",
        "startswith",
        "endswith",
        "match",
        "regex",
    }
)


def coerce_record_id(value: Any, table: str) -> RecordID:
    """Convert a filter value for the ``id`` column into the ``RecordID`` the row stores.

    Accepts the forms a caller naturally reaches for, and keeps the typing rule
    :func:`format_record_id` applies on the relation paths — a Python ``int`` is an *integer*
    record id, a ``str`` is a *string* one, and the two address different records:

    ==================  ==========================
    ``RecordID(...)``   returned unchanged
    ``5``               ``RecordID(table, 5)``
    ``"5"``             ``RecordID(table, "5")``
    ``"`5`"``           ``RecordID(table, "5")`` — backticks stripped, as ``get()`` does
    ``"table:d"``       ``RecordID(table, "d")`` — only when the prefix is *this* table
    ``"Other:d"``       ``RecordID(table, "Other:d")`` — may genuinely be the string id
    ``["paid"]``        ``RecordID(table, ["paid"])`` — an array id, a grouped view's (v0.32.0)
    ==================  ==========================

    A ``tuple`` is an array id too (``("paid",)``), copied into a ``list``.
    """
    if isinstance(value, RecordID):
        return value
    if isinstance(value, bool):
        raise ValueError("a record id cannot be a boolean")
    if isinstance(value, int):
        return RecordID(table, value)
    if isinstance(value, (list, tuple)):
        return RecordID(table, list(value))

    raw = str(value)
    prefix = f"{table}:"
    if raw.startswith(prefix):
        raw = raw[len(prefix) :]
    if len(raw) > 1 and raw.startswith("`") and raw.endswith("`"):
        raw = raw[1:-1]
    return RecordID(table, raw)


def _instance_record_id(instance: BaseSurrealModel) -> RecordID:
    """Return the record id of a model instance, refusing an unsaved one."""
    record_id = instance._record_id()
    if record_id is None:
        raise ValueError(f"An unsaved {type(instance).__name__} has no record id; save it first or pass a RecordID.")
    return record_id


def as_record_value(value: Any) -> Any:
    """Replace a model instance by the record id that addresses it; return anything else unchanged.

    What a model instance means as a filter value (v0.22.0): ``filter(customer=alice)`` asks
    for the rows whose ``customer`` column links to alice, i.e. ``customer = <alice's
    RecordID>``. Binding the instance itself would hand the SDK a Pydantic model to encode.
    Applied to every scalar filter value, to each item of a sequence lookup, and to the
    create payload of ``get_or_create``/``update_or_create`` — so the row those create is the
    row their next lookup finds.

    Raises:
        ValueError: for an unsaved instance — it has no record id to compare against.
    """
    from .model_base import BaseSurrealModel  # model_base imports this module

    if isinstance(value, BaseSurrealModel):
        return _instance_record_id(value)
    return value


def coerce_endpoint(value: Any) -> RecordID:
    """Read an edge endpoint (``in``/``out``) as the ``RecordID`` the edge stores (v0.22.0).

    The one rule shared by the endpoint fields of an edge model and by the filters on its
    endpoint columns, so the edge ``get_or_create(in_="person:alice", …)`` creates is the
    edge its next lookup finds:

    ======================  ==================================================
    ``RecordID(...)``       returned unchanged
    a model instance        its record id — an unsaved one raises
    ``"table:id"``          ``RecordID("table", "id")`` — the id is a string
    ``"table:`a-b`"``       ``RecordID("table", "a-b")`` — backticks stripped
    anything else           ``ValueError``
    ======================  ==================================================

    A string must pass :func:`validate_thing` (id made of letters, digits, ``_`` and ``-``,
    optionally backtick-quoted), the rule ``relate()`` already applies to its targets. An id
    with other characters (``joe@x.com``) or an integer id: pass the instance or a
    ``RecordID``.

    Raises:
        ValueError: for an unsaved instance, a malformed string, or any other type.
    """
    from .model_base import BaseSurrealModel  # model_base imports this module

    if isinstance(value, RecordID):
        return value
    if isinstance(value, BaseSurrealModel):
        return _instance_record_id(value)
    if isinstance(value, str):
        validate_thing(value)
        table, _, raw = value.partition(":")
        if len(raw) > 1 and raw.startswith("`") and raw.endswith("`"):
            raw = raw[1:-1]
        return RecordID(table, raw)
    raise ValueError(
        f"an edge endpoint must be a model instance, a RecordID or a 'table:id' string, got {type(value).__name__}"
    )


def parse_lookup(key: str) -> tuple[str, str]:
    """
    Parse a filter key into field name and lookup type.

    The key is split on its first ``__``. Leading underscores of the lookup part belong to the
    field (v0.22.0): no lookup name starts with ``_``, so ``in___in`` is the ``in`` lookup on the
    field ``in_`` and ``type___gt`` the ``gt`` lookup on ``type_``. A key without ``__`` is a
    field name (``in_`` → ``("in_", "exact")``).

    Args:
        key: The filter key in the format ``field__lookup`` or just ``field``.

    Returns:
        A tuple of (field_name, lookup_type). Defaults to ``"exact"`` if no lookup is specified.
    """
    if "__" in key:
        field_name, lookup_name = key.split("__", 1)
        while lookup_name.startswith("_"):
            field_name += "_"
            lookup_name = lookup_name[1:]
    else:
        field_name, lookup_name = key, "exact"
    return field_name, lookup_name


def _spatial_condition(
    field: str, lookup: str, op: str, value: Any, counter: int, endpoint_columns: Collection[str]
) -> tuple[str, dict[str, Any], int]:
    """The condition of a spatial lookup (``inside``, ``outside``, ``intersects``; v0.24.0).

    One geometry, bound as ``$_f<n>`` (``geo._as_geometry``), or a ``Var`` reference. Refused on the
    ``id`` column and on edge endpoint columns, which hold record ids.
    """
    if field == RECORD_ID_COLUMN:
        raise ValueError(
            f"'{lookup}' cannot be applied to the record id column '{field}': a record id is not a "
            "geometry. Filter on a geometry field."
        )
    if field in endpoint_columns:
        raise ValueError(
            f"'{lookup}' cannot be applied to the edge endpoint column '{field}': it holds a record id, "
            "and a record id is not a geometry."
        )
    if isinstance(value, Var):
        return f"{field} {op} {value.reference}", {}, counter
    var_name = f"_f{counter}"
    return f"{field} {op} ${var_name}", {var_name: _geo._as_geometry(value, lookup)}, counter + 1


#: The characters SurrealDB's ``string::matches`` (the Rust ``regex`` crate) reads as syntax outside a class. A
#: ``like`` pattern escapes these and no other: a Rust regex refuses some escapes Python's ``re.escape`` writes.
_REGEX_METACHARACTERS = frozenset("\\.+*?()|[]{}^$")


def _like_regex(pattern: str, *, ignore_case: bool) -> str:
    """The regular expression of an SQL ``LIKE`` *pattern* (#251), for ``string::matches``.

    ``%`` matches any run of characters, newlines included (``.*`` under ``(?s)``), ``_`` exactly one (``.``), and
    every other character is literal. Anchored at both ends: ``^`` and ``$`` are the text's start and end in a Rust
    regex, never a line's. *ignore_case* (``ilike``) adds ``(?i)``.
    """
    parts = []
    for char in pattern:
        if char == "%":
            parts.append(".*")
        elif char == "_":
            parts.append(".")
        elif char in _REGEX_METACHARACTERS:
            parts.append("\\" + char)
        else:
            parts.append(char)
    flags = "(?is)" if ignore_case else "(?s)"
    return f"{flags}^{''.join(parts)}$"


def _text_condition(
    field: str, lookup: str, function: str, value: Any, counter: int, subqueries: SubqueryCompiler | None
) -> tuple[str, dict[str, Any], int]:
    """The condition of a text lookup (``startswith``, ``endswith``, ``like``, ``ilike``, ``match``, ``regex``; #251).

    ``((<field>).is_string() AND <function>(<field>, $_f<n>))``: the string function alone fails the whole query on
    a record whose field is ``NONE``, ``NULL`` or not a string, and the guard makes such a record never match (and
    match the negation). The method form parses on both lines and on the embedded engine (``type::is::string`` is
    2.x-only, ``type::is_string`` 3.x-only). The parentheses make it test the path's value as a whole: SurrealDB 3.x
    applies ``authors.name.is_string()`` to each element of the array a dotted path through an array of record links
    or objects gives (``[true, true]``, truthy), which then reached the function and failed the query; such a path is
    an array, never matches, and matches the negation on both lines.

    The value is a ``str`` — data, whatever it starts with, the ``"$$"`` escape unescaped and a variable-like value
    noted as in :func:`build_filter_condition` — or a ``Var`` reference, except for ``like``/``ilike``, whose
    pattern is translated to a regular expression before it is bound (:func:`_like_regex`).

    Raises:
        ValueError: a ``Var`` given to ``like``/``ilike``, or a value that is neither a ``str`` nor a ``Var``.
    """
    if isinstance(value, Var):
        if lookup in ("like", "ilike"):
            raise ValueError(
                f"'{lookup}' cannot take a Var: its pattern is translated to a regular expression before it is sent, "
                "and a Var's value is only known to the database. Pass the pattern as a string, or use 'regex' "
                "with a Var holding a regular expression."
            )
        return _guarded(field, function, value.reference), {}, counter
    if not isinstance(value, str):
        raise ValueError(f"'{lookup}' lookup requires a string, got {type(value).__name__}")
    variable_like = VARIABLE_NAME_VALUE.fullmatch(value)
    if subqueries is not None and variable_like is not None:
        subqueries.variable_like.append(variable_like.group(1))
    value = unescape_filter_value(value, warn=subqueries is None or subqueries.warn)
    if lookup in ("like", "ilike"):
        value = _like_regex(value, ignore_case=lookup == "ilike")
    var_name = f"_f{counter}"
    return _guarded(field, function, f"${var_name}"), {var_name: value}, counter + 1


def _guarded(field: str, function: str, argument: str) -> str:
    """``((<field>).is_string() AND <function>(<field>, <argument>))``, the condition of a text lookup (#251).

    The path is parenthesised before ``.is_string()``, so a dotted path through an array is tested as the array it
    gives, not element by element (:func:`_text_condition`).
    """
    return f"(({field}).is_string() AND {function}({field}, {argument}))"


def _list_item(item: Any, field: str, lookup: str) -> Any:
    """An item of a sequence lookup's list, returned unchanged; a subquery there raises ``ValueError`` (v0.27.0)."""
    if subquery_of(item) is not None:
        raise ValueError(inside_a_list(field, lookup))
    return item


def unescape_filter_value(value: Any, *, warn: bool = True) -> Any:
    """The data a scalar filter value stands for: *value* itself, but for the deprecated ``"$$"`` escape.

    A ``str`` is data, whatever it starts with: ``"$x"`` is compared with the string ``"$x"``, and only
    ``Var("x")`` references the query variable ``$x``. The ``"$$"`` escape of v0.14.3, written when ``"$x"``
    still read as a reference, keeps its meaning so that escaped values find the same records: a ``str``
    starting with ``"$$"`` is the same string with one ``$`` less (``"$$x"`` is ``"$x"``, ``"$$$x"`` is
    ``"$$x"``), with a ``DeprecationWarning`` saying to write the value with one ``$`` less
    (:data:`ESCAPE_DEPRECATED`, the same text whatever the value). An item of a list lookup never had the escape,
    and is not unescaped.

    Args:
        value: a scalar filter value, or a ``get_or_create()``/``update_or_create()`` ``exact`` criterion.
        warn: emit the ``DeprecationWarning`` (the WHERE builder does once per read; the create payload of the same
            call and a statement compiled again for the same read do not).
    """
    if not (isinstance(value, str) and value.startswith("$$")):
        return value
    if warn:
        warnings.warn(ESCAPE_DEPRECATED, DeprecationWarning, stacklevel=user_stacklevel())
    return value[1:]


def warn_on_variable_names(names: Iterable[str], variables: Collection[str]) -> None:
    """Warn for each of *names* that *variables* or SurrealDB's session parameters hold, once per name: a ``str``
    filter value ``"$<name>"`` is data.

    Until v0.28.1 ``filter(age__lte="$max_age").variables(max_age=25)`` compiled ``age <= $max_age``. The string
    is data now, bound and compared as it is, so such a call changes its result without an error (a number sorts
    below a string: ``lt``/``lte`` match every number, ``gt``/``gte``/``exact`` none). A ``UserWarning`` names
    ``Var("max_age")``, the reference. ``filter(owner="$auth")`` likewise compares ``owner`` with the string: a
    name of :data:`~surreal_orm_lite.constants.SESSION_PARAMETERS` that *variables* do not hold gets the
    ``UserWarning`` of :func:`session_parameter_warning`, which names ``Var("auth")``; its text is the same apart
    from that name, so the warnings registry of a call site holds one entry per session parameter at most. Any
    other name is data the caller meant (``"$x"`` with no variable ``x``): nothing warns.

    Args:
        names: the names of the scalar ``str`` filter values of one statement that match :data:`VARIABLE_NAME_VALUE`
            (``"$max_age"`` gives ``max_age``), in order.
        variables: the names the statement's ``variables()`` give.
    """
    for name in dict.fromkeys(names):
        if name in variables:
            warnings.warn(
                f"The filter value '${name}' is compared as the string '${name}', not with the variable {name} "
                f"given to variables(); to reference the variable, use Var('{name}').",
                UserWarning,
                stacklevel=user_stacklevel(),
            )
        elif name in SESSION_PARAMETERS:
            warnings.warn(session_parameter_warning(name), UserWarning, stacklevel=user_stacklevel())


def session_parameter_warning(name: str) -> str:
    """The ``UserWarning`` text of a ``str`` filter value ``"$<name>"`` naming SurrealDB's session parameter *name*.

    *name* is one of :data:`~surreal_orm_lite.constants.SESSION_PARAMETERS`: the text is fixed apart from it.
    """
    return (
        f"The filter value '${name}' is compared as the string '${name}', not with SurrealDB's ${name} parameter; "
        f"to read the parameter, use Var('{name}')."
    )


def reference_hint(name: str, subqueries: SubqueryCompiler | None) -> str | None:
    """The hint of the ``ValueError`` for a ``str`` filter value ``"$<name>"`` that cannot be data where it is.

    An edge's ``in``/``out`` holds a record id, so ``filter(in_="$p")`` raises; when ``p`` is a variable of the
    statement (:meth:`SubqueryCompiler.names_a_variable`) or one of SurrealDB's session parameters, the message ends
    with the ``Var(...)`` that reads it (v0.28.1). ``None`` for any other name.
    """
    if subqueries is not None and subqueries.names_a_variable(name):
        return f"to reference the variable, use Var('{name}')"
    if name in SESSION_PARAMETERS:
        return f"to read SurrealDB's ${name} parameter, use Var('{name}')"
    return None


def build_filter_condition(
    field: str,
    lookup: str,
    value: Any,
    counter: int,
    record_table: str | None = None,
    *,
    endpoint_columns: Collection[str] = (),
    subqueries: SubqueryCompiler | None = None,
    view: bool = False,
) -> tuple[str, dict[str, Any], int]:
    """
    Build a single parameterized filter condition.

    A ``str`` value is data, whatever it starts with: ``"$x"`` is bound and compared with the string ``"$x"``.
    ``Var("x")`` is the only reference to a query variable, written ``$x``. A scalar ``str`` starting with
    ``"$$"`` (the deprecated escape) loses one ``$``, with a ``DeprecationWarning`` (:func:`unescape_filter_value`)
    unless ``subqueries.warn`` is false. The name of a scalar ``str`` that is ``$`` and a variable name (``"$x"``
    gives ``x``) is noted in ``subqueries.variable_like``: ``QuerySet._build_where`` checks it against the
    statement's ``variables()`` and SurrealDB's session parameters (``$auth``…) once every condition is compiled
    (:func:`warn_on_variable_names`). On an edge endpoint column such a value is not a record id: when it names a
    variable or a session parameter, the ``ValueError`` ends with the ``Var(...)`` that reads it
    (:func:`reference_hint`).

    A model instance given as the value — or as an item of a sequence lookup — is bound as its
    record id (:func:`as_record_value`, v0.22.0).

    A ``Range`` (ORM or SDK) given to ``in``/``not_in`` is bound as one value (v0.23.0); on the
    ``id`` column and on edge endpoint columns it raises ``ValueError``.

    The spatial lookups ``inside``, ``outside`` and ``intersects`` (v0.24.0,
    ``GEOMETRY_LOOKUP_OPERATORS``) compile to SurrealQL's ``INSIDE``, ``OUTSIDE`` and
    ``INTERSECTS`` with one geometry bound as one value: an ORM or SDK geometry as it is, a GeoJSON
    geometry object as the ORM value, or a ``Var`` reference. ``outside`` keeps SurrealQL's
    semantics: it also holds for a record whose field is missing or holds no geometry.

    The text lookups ``startswith``, ``endswith``, ``like``, ``ilike``, ``match`` and ``regex`` (#251,
    ``TEXT_LOOKUP_FUNCTIONS``) compile to SurrealQL's string functions, guarded by the field's type
    (:func:`_text_condition`): ``((name).is_string() AND string::starts_with(name, $_f0))``. A record whose field
    is missing, ``NULL`` or not a string (a dotted path through an array included) never matches. ``like``/``ilike``
    bind their pattern translated to an anchored regular expression (:func:`_like_regex`); ``match``/``regex`` bind
    theirs as it is.

    A subquery (v0.27.0) — a ``Subquery``, or a ``QuerySet`` read as one — given as the whole value is compiled
    by *subqueries*, the statement's compiler (``subquery.subquery_condition``): ``in``, ``not_in``,
    ``containsall`` and ``containsany`` compare with all its values, ``exact``, ``gt``, ``gte``, ``lt``, ``lte``,
    ``contains`` and ``not_contains`` with its first value (no record when it is empty). Its values are compared
    as stored: no record-id or endpoint coercion. The ``id`` and endpoint columns keep their refusals.

    On a view model's id column, ``in`` compiles ``$_f0 CONTAINS id``: SurrealDB 3.x answers no record to ``id IN``
    on a view table (v0.32.0).

    Args:
        field: The field name.
        lookup: The lookup type (e.g. ``"exact"``, ``"gt"``, ``"in"``).
        value: The filter value.
        counter: The current variable counter for unique naming.
        record_table: The table the query runs against, when known. It is what lets a lookup
            on the ``id`` column be coerced to a ``RecordID`` (issue #159). ``None`` — a
            ``Q`` compiled outside a QuerySet — leaves values untouched.
        endpoint_columns: The queried model's edge endpoint columns (``in``/``out`` on an edge
            model, none on a node; v0.22.0). On those columns a scalar or ``in``/``not_in``
            value goes through :func:`coerce_endpoint`, so ``"person:alice"`` is the record id
            it names, and a text/collection lookup is refused. Node columns are untouched: a
            string column may legitimately hold ``"a:b"``.
        subqueries: The compiler of the statement the condition goes into (v0.27.0), which binds a subquery
            value in a ``LET`` (or inline, for a live query). ``None`` — a ``Q`` compiled on its own — refuses a
            subquery.
        view: The queried model is a materialized view (v0.32.0), set by the QuerySet: ``in`` on the ``id``
            column compiles ``<values> CONTAINS id``, the same values bound. ``False`` leaves every condition as
            before.

    Returns:
        A tuple of (sql_fragment, variables_dict, next_counter).

    Raises:
        ValueError: If the lookup type is not supported, the field name is invalid, a
            text/collection lookup is applied to the ``id`` column or to an endpoint column,
            a sequence lookup gets a single model instance, a model instance is unsaved, or
            an endpoint value is not a record id — or an ``in``/``not_in`` lookup gets a
            ``Range`` on the ``id`` column or on an endpoint column (v0.23.0) — or a spatial
            lookup gets a value that is not a geometry, or is applied to the ``id`` column or
            to an endpoint column (v0.24.0). ``InvalidGeometryError`` (a ``ValueError``) for a
            malformed GeoJSON object. A text lookup given a value that is neither a ``str`` nor a ``Var``, or
            ``like``/``ilike`` given a ``Var`` (#251). With a subquery (v0.27.0): a lookup that takes none —
            ``isnull``, the text and the spatial lookups; a subquery as an item of a list; no *subqueries*
            compiler; and the inner queryset's refusals (README section 28).
    """
    validate_field_name(field, "filter field")
    sub = subquery_of(value)
    spatial_op = GEOMETRY_LOOKUP_OPERATORS.get(lookup)
    if spatial_op is not None:
        if sub is not None:
            raise ValueError(lookup_refused(lookup))
        return _spatial_condition(field, lookup, spatial_op, value, counter, endpoint_columns)
    text_function = TEXT_LOOKUP_FUNCTIONS.get(lookup)
    if text_function is None and lookup not in LOOKUP_OPERATORS:
        raise ValueError(f"Unsupported lookup type: '{lookup}'")

    is_record_id_column = record_table is not None and field == RECORD_ID_COLUMN
    if is_record_id_column and lookup in _RECORD_ID_INVALID_LOOKUPS:
        raise ValueError(
            f"'{lookup}' cannot be applied to the record id column '{field}': a record id is "
            "not a string. Filter on a regular column, or reach for the textual form "
            "explicitly in a raw query (e.g. string::contains(record::id(id), '...'))."
        )
    is_endpoint_column = field in endpoint_columns
    if is_endpoint_column and lookup in _RECORD_ID_INVALID_LOOKUPS:
        raise ValueError(f"'{lookup}' cannot be applied to the edge endpoint column '{field}': it holds a record id, not text.")

    if text_function is not None:
        if sub is not None:
            raise ValueError(lookup_refused(lookup))
        return _text_condition(field, lookup, text_function, value, counter, subqueries)

    op = LOOKUP_OPERATORS[lookup]
    var_name = f"_f{counter}"
    if sub is not None:
        return subquery_condition(field, lookup, op, sub, counter, subqueries, view=view and is_record_id_column)

    if lookup == "isnull":
        if not isinstance(value, bool):
            raise ValueError(f"isnull lookup requires a boolean value, got {type(value).__name__}")
        if value:
            return f"{field} IS NULL", {}, counter
        else:
            return f"{field} IS NOT NULL", {}, counter
    elif lookup in ("in", "not_in", "containsall", "containsany"):
        if lookup in ("in", "not_in") and isinstance(value, (Range, SdkRange)):
            if field == RECORD_ID_COLUMN:
                raise ValueError(
                    f"'{lookup}' with a Range cannot be applied to the record id column '{field}': a record id "
                    "never falls inside a value range. Filter on a regular column."
                )
            if is_endpoint_column:
                raise ValueError(
                    f"'{lookup}' with a Range cannot be applied to the edge endpoint column '{field}': it holds "
                    "a record id, which never falls inside a value range."
                )
            return f"{field} {op} ${var_name}", {var_name: value}, counter + 1
        if isinstance(value, (str, dict)) or not hasattr(value, "__iter__"):
            raise ValueError(f"'{lookup}' lookup requires an iterable (list, tuple, or set), got {type(value).__name__}")
        if isinstance(value, pydantic.BaseModel):
            # A Pydantic model is iterable — it would become a list of (name, value) pairs.
            raise ValueError(
                f"'{lookup}' lookup requires an iterable (list, tuple, or set), got a single "
                f"{type(value).__name__} instance; wrap it in a list."
            )
        items = [as_record_value(_list_item(item, field, lookup)) for item in value]
        if is_record_id_column and lookup in _RECORD_ID_SEQUENCE_LOOKUPS:
            items = [coerce_record_id(item, cast(str, record_table)) for item in items]
            if view and lookup == "in":
                return f"${var_name} CONTAINS {field}", {var_name: items}, counter + 1
        elif is_endpoint_column and lookup in _RECORD_ID_SEQUENCE_LOOKUPS:
            items = [coerce_endpoint(item) for item in items]
        return f"{field} {op} ${var_name}", {var_name: items}, counter + 1

    value = as_record_value(value)
    if isinstance(value, Var):
        # The one variable reference (v0.14.3): every other value is bound as data.
        return f"{field} {op} {value.reference}", {}, counter
    variable_like = VARIABLE_NAME_VALUE.fullmatch(value) if isinstance(value, str) else None
    if subqueries is not None and variable_like is not None:
        subqueries.variable_like.append(variable_like.group(1))
    value = unescape_filter_value(value, warn=subqueries is None or subqueries.warn)
    if is_record_id_column and lookup in _RECORD_ID_SCALAR_LOOKUPS:
        value = coerce_record_id(value, cast(str, record_table))
    elif is_endpoint_column and lookup in _RECORD_ID_SCALAR_LOOKUPS:
        try:
            value = coerce_endpoint(value)
        except ValueError as exc:
            hint = None if variable_like is None else reference_hint(variable_like.group(1), subqueries)
            if hint is None:
                raise
            raise ValueError(f"{exc}; {hint}") from exc
    return f"{field} {op} ${var_name}", {var_name: value}, counter + 1


# JSON Patch (RFC 6902) operation kinds. SurrealDB also accepts ``change`` (string diff).
_JSON_PATCH_OPS = frozenset({"add", "remove", "replace", "move", "copy", "test", "change"})
_OPS_REQUIRING_VALUE = frozenset({"add", "replace", "test", "change"})
_OPS_REQUIRING_FROM = frozenset({"move", "copy"})


def validate_json_pointer(pointer: Any, context: str = "path") -> None:
    """
    Validate an RFC 6901 JSON Pointer.

    Accepts the empty string (the whole document) or a string starting with ``/``.

    Args:
        pointer: The JSON Pointer to validate.
        context: Description of where the pointer is used (for error messages).

    Raises:
        ValueError: If the pointer is not a string, or a non-empty string not starting with ``/``.
    """
    if not isinstance(pointer, str):
        raise ValueError(f"{context} must be a string JSON Pointer, got {type(pointer).__name__}")
    if pointer and not pointer.startswith("/"):
        raise ValueError(f"Invalid {context} '{pointer}': a JSON Pointer must be empty or start with '/'")


def validate_patch_operations(operations: Any) -> None:
    """
    Validate a JSON Patch (RFC 6902) document: a non-empty list of operation dicts.

    Checks that ``op`` is a known kind, ``path`` is present and a valid JSON Pointer, ``value``
    is present for add/replace/test/change, and ``from`` is present and valid for move/copy.

    Accepts the six RFC 6902 ops (add/remove/replace/move/copy/test) plus ``change`` — a
    SurrealDB-specific string-diff extension, not part of RFC 6902.

    The operations are bound as query data (never string-interpolated into SurrealQL), so this
    validation exists for fast, clear errors — not as the injection boundary.

    Args:
        operations: The JSON Patch document to validate.

    Raises:
        ValueError: If the document or any operation is malformed.
    """
    if not isinstance(operations, list) or not operations:
        raise ValueError("patch operations must be a non-empty list of operation dicts")
    for i, op in enumerate(operations):
        if not isinstance(op, dict):
            raise ValueError(f"patch operation #{i} must be a dict, got {type(op).__name__}")
        kind = op.get("op")
        if kind not in _JSON_PATCH_OPS:
            raise ValueError(f"patch operation #{i} has invalid op {kind!r}; expected one of {sorted(_JSON_PATCH_OPS)}")
        if "path" not in op:
            raise ValueError(f"patch operation #{i} ({kind!r}) is missing required 'path'")
        validate_json_pointer(op["path"], f"operation #{i} path")
        if kind in _OPS_REQUIRING_VALUE and "value" not in op:
            raise ValueError(f"patch operation #{i} ({kind!r}) is missing required 'value'")
        if kind in _OPS_REQUIRING_FROM:
            if "from" not in op:
                raise ValueError(f"patch operation #{i} ({kind!r}) is missing required 'from'")
            validate_json_pointer(op["from"], f"operation #{i} from")


def _ddl_refusal_class(statement: str, error: BaseException) -> type[SurrealDbError]:
    """The ORM class of a refused DDL *statement* (``apply_ddl_statements(classify=True)``), from the server's text.

    ``SurrealDbAuthenticationError`` when the session may not define (``IAM error: Not enough permissions …``, on
    every line and through an interactive transaction alike); ``SurrealDbNotFoundError`` when a ``REMOVE`` names a
    definition that ``does not exist``; ``SurrealDbError`` for anything else (a parse error, a definition that
    already exists, a body the server refuses).
    """
    text = str(error).lower()
    if "not enough permissions" in text:
        return SurrealDbAuthenticationError
    if statement.startswith("REMOVE ") and "does not exist" in text:
        return SurrealDbNotFoundError
    return SurrealDbError


#: Where a server's parse error starts quoting the statement it refused: `` --> [line:column]``, after a newline, or
#: after the two characters ``\n`` when the error quotes a response's ``repr()`` (a 3.x interactive transaction).
_STATEMENT_EXCERPT = re.compile(r"\s*(?:\\n\s*)?--> \[(\d+):(\d+)\]")
#: The names of a definition the ORM writes — backquoted identifiers and ``fn::`` paths —, which a server's reason
#: names (``The event 'x' already exists``): they are left out of the comparison below.
_STATEMENT_NAMES = re.compile(r"`[^`]*`|\bfn::\w+(?:::\w+)*")
#: A run of this many characters of a statement's text, its names aside, in a server's reason means the reason quotes
#: the statement.
_QUOTED_RUN = 12
_REASON_LEFT_OUT = "the server's message quotes the statement, so it is left out"


def _quotes_statement(reason: str, statement: str) -> bool:
    """Whether *reason* holds a run of ``_QUOTED_RUN`` characters of *statement*'s text, outside its names."""
    for part in _STATEMENT_NAMES.split(statement):
        for start in range(len(part) - _QUOTED_RUN + 1):
            run = part[start : start + _QUOTED_RUN]
            if run.strip() and run in reason:
                return True
    return False


def _ddl_refusal_reason(statement: str, error: BaseException, *, redact: bool = True) -> tuple[str, bool]:
    """The server's reason for refusing *statement*, without any of the statement's text, and whether it is the
    error's whole text.

    A parse error's excerpt of the statement (from `` --> [line:column]`` on) is replaced by its position; with
    *redact* (the default), a reason that still quotes the statement (``_quotes_statement()``) is replaced by a fixed
    text. ``redact=False`` (v0.32.0's view DDL) keeps such a reason: a view's query is trusted application code, and
    its refusals name its source tables (``The table 'order_archive' does not exist``).
    """
    text = str(error)
    excerpt = _STATEMENT_EXCERPT.search(text)
    if excerpt is None:
        reason = text
    else:
        reason = f"{text[: excerpt.start()]} (at line {excerpt[1]}, column {excerpt[2]} of the statement)"
    if redact and _quotes_statement(reason, statement):
        return _REASON_LEFT_OUT, False
    return reason, excerpt is None


async def apply_ddl_statements(
    statements: list[str],
    *,
    client: Any,
    tx: Any = None,
    what: str,
    hint: str = "",
    kind: str = "definition",
    classify: bool = False,
    subjects: Sequence[str] | None = None,
    redact: bool = True,
) -> list[str]:
    """Run DDL statements one at a time, normalising every rejection to `SurrealDbError`.

    One statement per call so a failure can name the offending one, and one owner for the
    error handling because the exception type depends on *how* the statement was run:

    - directly, or in a **buffered** transaction, the SDK surfaces a ``ServerError``
      (``InternalError`` on 2.6.x, ``ValidationError`` on 3.x), and on the embedded engine of
      SDK 2.0.0, a bare ``RuntimeError`` for a statement the engine cannot parse;
    - in a **native interactive** transaction (WebSocket + SurrealDB 3.x), ``Transaction.add``
      has already inspected the response and raised ``SurrealDbError`` itself.

    Catching only the first leaves the interactive path propagating a bare
    "Transaction failed and rolled back" with no mention of which definition caused it.

    The message is ``Can't apply {what} {kind}: {statement} -> {server message}{hint}``, the server's
    error chained as ``__cause__``. By default every refusal is a plain ``SurrealDbError``. With
    ``classify=True`` (v0.31.0's event and function DDL) the class says why:
    ``SurrealDbAuthenticationError`` when the session may not define, ``SurrealDbNotFoundError`` when a
    ``REMOVE`` names a missing definition, ``SurrealDbError`` otherwise — and a
    ``SurrealDbConflictError`` propagates unchanged, so ``retry_on_conflict`` still sees it.

    With *subjects* (v0.31.0's event and function DDL, whose ``when``, ``then`` or body may hold a
    secret) the message names the statement's subject instead — ``Can't apply {what} {kind}: {subject}
    -> {reason}{hint}`` — and never quotes the statement: the reason is the server's message without
    a parse error's excerpt of the statement (its position kept), or a fixed text when it still quotes
    the statement. The server's error is chained as ``__cause__`` only when its whole message is the
    reason; otherwise it stays reachable as ``__context__``, which a traceback does not print.

    With ``redact=False`` (v0.32.0's view DDL, whose query is trusted application code and whose refusals
    name its source tables) a reason that quotes the statement is kept; a parse error's excerpt is still
    replaced by its position.

    :param what: names the kind of definition, for the message (e.g. ``"access"``).
    :param hint: appended to the message — extra guidance when the cause is likely known.
    :param kind: what the statements do, for the message: ``"definition"`` or ``"removal"``.
    :param classify: map each refusal to the class that says why (see above).
    :param subjects: one per statement, how the message names it (``"'audit' on table 'account'"``).
    :param redact: with *subjects*, replace a reason that still quotes the statement by a fixed text (default).
    :returns: the statements applied, so callers can hand them straight back.
    :raises ValueError: *subjects* does not have one subject per statement — before any request.
    """
    if subjects is not None and len(subjects) != len(statements):
        raise ValueError(
            f"apply_ddl_statements(): {len(statements)} statements and {len(subjects)} subjects; one subject per statement."
        )
    for index, statement in enumerate(statements):
        try:
            if tx is not None:
                await tx.add(statement, None)
            else:
                await client.query(statement, {})
        except (ServerError, SurrealDbError, RuntimeError) as e:
            if classify and isinstance(e, SurrealDbConflictError):
                raise
            refusal = _ddl_refusal_class(statement, e) if classify else SurrealDbError
            if subjects is None:
                raise refusal(f"Can't apply {what} {kind}: {statement} -> {e}{hint}") from e
            reason, whole = _ddl_refusal_reason(statement, e, redact=redact)
            message = f"Can't apply {what} {kind}: {subjects[index]} -> {reason}{hint}"
            if whole:
                raise refusal(message) from e
            raise refusal(message) from None
    return statements
