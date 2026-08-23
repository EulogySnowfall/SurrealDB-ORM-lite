import inspect
import os
import re
import warnings
from collections.abc import Iterable, Mapping
from typing import Any

from .constants import LOOKUP_OPERATORS
from .functions import SurrealFunc, Var

# Pattern for valid field names: alphanumeric, underscores, dots (for nested fields)
# Must start with a letter or underscore
VALID_FIELD_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*$")

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

# Pattern for valid SurrealQL variable references: $variable_name
VALID_VARIABLE_REF_PATTERN = re.compile(r"^\$[a-zA-Z_][a-zA-Z0-9_]*$")


def remove_quotes_for_variables(query: str) -> str:
    # Regex to remove single quotes around variables ($)
    return re.sub(r"'(\$[a-zA-Z_]\w*)'", r"\1", query)


_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))


def user_stacklevel() -> int:
    """``stacklevel`` that makes ``warnings.warn`` point at the first frame outside the ORM.

    A fixed ``stacklevel`` lands inside ``query_set.py``/``q.py``, and Python's default
    filters only surface a ``DeprecationWarning`` reported from ``__main__`` — so the
    deprecation runway never reached users at all (issue #156 follow-up). Call sites reach
    here through paths of differing depth, so the level is measured, not guessed.
    """
    frame = inspect.currentframe()
    level = 0
    while frame is not None and frame.f_back is not None:
        frame = frame.f_back
        level += 1
        if os.path.dirname(os.path.abspath(frame.f_code.co_filename)) != _PACKAGE_DIR:
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


def parse_lookup(key: str) -> tuple[str, str]:
    """
    Parse a filter key into field name and lookup type.

    Args:
        key: The filter key in the format ``field__lookup`` or just ``field``.

    Returns:
        A tuple of (field_name, lookup_type). Defaults to ``"exact"`` if no lookup is specified.
    """
    if "__" in key:
        field_name, lookup_name = key.split("__", 1)
    else:
        field_name, lookup_name = key, "exact"
    return field_name, lookup_name


def build_filter_condition(field: str, lookup: str, value: Any, counter: int) -> tuple[str, dict[str, Any], int]:
    """
    Build a single parameterized filter condition.

    Args:
        field: The field name.
        lookup: The lookup type (e.g. ``"exact"``, ``"gt"``, ``"in"``).
        value: The filter value.
        counter: The current variable counter for unique naming.

    Returns:
        A tuple of (sql_fragment, variables_dict, next_counter).

    Raises:
        ValueError: If the lookup type is not supported or the field name is invalid.
    """
    validate_field_name(field, "filter field")
    op = LOOKUP_OPERATORS.get(lookup)
    if op is None:
        raise ValueError(f"Unsupported lookup type: '{lookup}'")

    var_name = f"_f{counter}"

    if lookup == "isnull":
        if not isinstance(value, bool):
            raise ValueError(f"isnull lookup requires a boolean value, got {type(value).__name__}")
        if value:
            return f"{field} IS NULL", {}, counter
        else:
            return f"{field} IS NOT NULL", {}, counter
    elif lookup in ("in", "not_in", "containsall", "containsany"):
        if isinstance(value, (str, dict)) or not hasattr(value, "__iter__"):
            raise ValueError(f"'{lookup}' lookup requires an iterable (list, tuple, or set), got {type(value).__name__}")
        return f"{field} {op} ${var_name}", {var_name: list(value)}, counter + 1
    elif isinstance(value, Var):
        # The explicit, unambiguous form (v0.14.3).
        return f"{field} {op} {value.reference}", {}, counter
    elif isinstance(value, str) and value.startswith("$$"):
        # "$$x" escapes to the literal "$x" — the only way to filter on a value that
        # really does start with a dollar sign (issue #156).
        return f"{field} {op} ${var_name}", {var_name: value[1:]}, counter + 1
    elif isinstance(value, str) and value.startswith("$"):
        # Backward compat: string values starting with $ are variable references. This is
        # a trap for user-supplied data — a literal "$admin" silently matches nothing — so
        # it is deprecated in favour of Var("admin"), with "$$admin" for the literal.
        if not VALID_VARIABLE_REF_PATTERN.match(value):
            raise ValueError(
                f"Invalid variable reference '{value}': must match $variable_name pattern. "
                f"To filter on the literal string, escape the dollar sign: '${value}'."
            )
        warnings.warn(
            f"Passing '{value}' as a filter value is interpreted as a reference to the query "
            f"variable {value} and is deprecated; use Var({value[1:]!r}) to reference a "
            f"variable, or '${value}' to match the literal string.",
            DeprecationWarning,
            stacklevel=user_stacklevel(),
        )
        return f"{field} {op} {value}", {}, counter
    else:
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
