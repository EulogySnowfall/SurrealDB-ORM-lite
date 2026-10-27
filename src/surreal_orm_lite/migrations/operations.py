"""The operations of a migration (the full SurrealDB-ORM's ``migrations.operations``).

An operation is one schema change: ``forwards()`` renders the SurrealQL that applies it, ``backwards()`` the
SurrealQL that undoes it, and ``state_forwards()``/``state_backwards()`` give the schema state after either, without a
database. Every concrete operation is a frozen dataclass deriving from :class:`Operation`, with the full ORM's
arguments in its order and the lite's additions keyword-only.

The statements are written by the renderers the appliers already use — ``table_ddl()``'s, ``Index.ddl()``,
``Analyzer.ddl()``, ``Event.ddl()``, ``function_ddl()``, ``schema.access_statement()`` — through ``render``: a
creation writes the plain ``DEFINE <KIND>`` form (it fails when the definition exists), a redefinition ``OVERWRITE``,
an analyzer always ``OVERWRITE``, and a creation is undone by the matching ``REMOVE … IF EXISTS`` (#276). A removal of
a definition that holds no data can carry the definition it removes (``previous=``), which makes it reversible;
``DropTable`` and ``DropField`` never are, their records' values being lost.

Construction checks what it can without a server — names, kinds, literals, expressions, table keys, index and
analyzer arguments — with the checkers of the appliers, and raises ``ValueError`` (``TypeError`` for a wrong type)
naming the operation and the argument; a message quotes a name, never a SurrealQL text. ``repr()`` shows names,
kinds, flags and comments only: every attribute holding SurrealQL text is left out of it.

Expressions, rules, view queries, event bodies, function bodies and SQL are trusted application code embedded as
written — SurrealQL takes no bound parameter in DDL. The checks catch mistakes; they are not an injection defence:
never build an operation from user input.
"""

import inspect
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Collection, Coroutine, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import KW_ONLY, dataclass, field
from functools import partial
from typing import Any, Literal, TypeVar, cast

from ..define_parser import (
    _DURATION,
    DefineParseError,
    blank_literals,
    parse_kind,
    render_kind,
    scan_fragment,
    top_level_keywords,
)
from ..functions import SurrealFunc, normalize_function_name
from ..schema import (
    AccessParts,
    Event,
    _fragment,
    _identifier_name,
    access_statement,
    changefeed_text,
    check_view_query,
    comment_text,
    function_ddl,
    permissions_rules,
    relation_tables,
)
from ..schema_state import AccessState, AnalyzerState, EventState, FieldState, IndexState, SchemaState, TableState
from ..search import Analyzer, Index, checked_line
from ..surql_literal import _identifier, to_surql_literal
from ..types import SchemaMode, TableType
from ..utils import VALID_ALIAS_PATTERN, validate_field_name
from . import compare, render
from . import state as _state

__all__ = [
    "AddField",
    "AlterField",
    "AlterTable",
    "CreateIndex",
    "CreateTable",
    "DataMigration",
    "DefaultExpression",
    "DefineAccess",
    "DefineAnalyzer",
    "DefineEvent",
    "DefineFunction",
    "DropField",
    "DropIndex",
    "DropTable",
    "Operation",
    "RawSQL",
    "RebuildIndex",
    "RemoveAccess",
    "RemoveAnalyzer",
    "RemoveEvent",
    "RemoveFunction",
    "split_destructive",
]

_T = TypeVar("_T")

#: A data migration's function: ``async def`` taking no argument.
AsyncFunction = Callable[[], Coroutine[Any, Any, None]]


class Operation(ABC):
    """One schema change of a :class:`~surreal_orm_lite.migrations.Migration` (the full ORM's base class).

    ``forwards()`` with no argument renders the SurrealDB 3.x form; ``line=2`` the form for SurrealDB 2.x and the
    embedded engine. *state* is the schema the statement is rendered for, read by the rules that depend on the
    table's definition (``FLEXIBLE`` before SurrealDB 3.x); without it, those rules write nothing.

    ``reversible`` is read-only: ``backwards()`` of an irreversible operation returns ``""``. ``destructive`` is true
    for a removal (``Drop*``/``Remove*``), whether or not it can be undone. ``describe()`` names what the operation
    changes, never its SurrealQL text.

    ``state_forwards()`` and ``state_backwards()`` are pure and follow SurrealDB's DDL semantics: a definition replaces
    the one of the same name, a table definition keeps the table's fields, indexes and events, and a removal of a
    definition the state does not hold leaves it as it is — neither ever raises for a missing definition.
    ``state_backwards()`` of an irreversible operation raises ``ValueError("<describe()> is not reversible.")``,
    except ``DataMigration``'s, whose schema effect is none either way.
    """

    @property
    @abstractmethod
    def reversible(self) -> bool:
        """Whether ``backwards()`` undoes the operation."""

    @property
    def destructive(self) -> bool:
        """Whether the operation removes a definition (a removal overrides this)."""
        return False

    @abstractmethod
    def forwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        """The SurrealQL statement(s) that apply the operation."""

    @abstractmethod
    def backwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        """The SurrealQL statement(s) that undo the operation; ``""`` when it is not reversible."""

    @abstractmethod
    def describe(self) -> str:
        """A one-line description naming what the operation changes (names only, never SurrealQL text)."""

    @abstractmethod
    def state_forwards(self, state: SchemaState) -> SchemaState:
        """The schema state after ``forwards()`` was applied to *state* (pure)."""

    @abstractmethod
    def state_backwards(self, state: SchemaState) -> SchemaState:
        """The schema state after ``backwards()`` was applied to *state* (pure)."""


# --- the shared checks -----------------------------------------------------------------------------------------------


@contextmanager
def _refusal(subject: str) -> Iterator[None]:
    """Re-raise a ``TypeError`` or ``ValueError`` of a shared checker with *subject* (the operation) in front."""
    try:
        yield
    except TypeError as error:
        raise TypeError(f"{subject}: {error}") from error
    except ValueError as error:
        raise ValueError(f"{subject}: {error}") from error


def _definition_name(value: object, *, subject: str, what: str) -> str:
    """The name of something an operation defines, checked as the appliers check it (``schema._identifier_name()``:
    an identifier, letters, digits and underscores, not starting with a digit)."""
    with _refusal(subject):
        return _identifier_name(value, what=what)


def _removal_name(value: object, *, subject: str, what: str) -> str:
    """A name a removal writes in backticks (D26): any non-empty ``str`` without a NUL character — a database may hold
    names SurrealDB accepts in backticks (``user-events``, ``2024_sales``)."""
    if not isinstance(value, str):
        raise TypeError(f"{subject}: {what} must be a str, got {type(value).__name__}.")
    if not value or "\x00" in value:
        raise ValueError(f"{subject}: {what} must be a non-empty str without a NUL character (\\x00).")
    return value


def _function_name(value: object, *, subject: str) -> str:
    """A ``fn::`` name, through ``normalize_function_name()`` (``"greet"`` → ``"fn::greet"``)."""
    with _refusal(subject):
        return normalize_function_name(cast(str, value))


def _flag(value: object, *, subject: str, what: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{subject}: {what} must be a bool, got {type(value).__name__}.")
    return value


def _comment(value: object, *, subject: str, what: str) -> str | None:
    """A ``COMMENT``: ``None``, or a ``str`` without a NUL character (the literal is written by the renderer)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{subject}: {what} must be a str, got {type(value).__name__}.")
    if "\x00" in value:
        raise ValueError(f"{subject}: {what} holds a NUL character (\\x00), which SurrealQL cannot carry.")
    return value


def _description(value: object, *, subject: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{subject}: description= must be a str, got {type(value).__name__}.")


def _expression(value: object, *, subject: str, what: str) -> str | None:
    """One SurrealQL expression (``scan_fragment()``: literals terminated, brackets balanced, no comment marker, no NUL,
    no ``;`` outside brackets), stripped; ``None`` for none."""
    if value is None:
        return None
    return _fragment(value, subject=subject, what=what, empty="leave it out (None) for none", one_expression=True)


def _shown(name: str) -> str:
    """A name as ``describe()`` shows it: an identifier as it is, any other through ``repr()`` (``'user-events'``), so
    a log line never holds a raw newline or backtick from the database."""
    return name if VALID_ALIAS_PATTERN.fullmatch(name) else repr(name)


def _shown_field(name: str, path: Sequence[str]) -> str:
    """A field's name as ``describe()`` shows it: as it is when every segment is an identifier or ``*``."""
    if all(segment == "*" or VALID_ALIAS_PATTERN.fullmatch(segment) for segment in path):
        return name
    return repr(name)


def _not_reversible(operation: Operation) -> ValueError:
    """D39's refusal of ``state_backwards()`` for an irreversible operation."""
    return ValueError(f"{operation.describe()} is not reversible.")


def _placed(state: SchemaState, statement: str) -> SchemaState:
    """*state* with the definition *statement* writes, read by ``render.read_own()``."""
    return _state.place(state, render.read_own(statement))


def _check_previous(
    previous: Any, *, subject: str, kind: type[Operation], what: str, name: str, table: str | None = None
) -> None:
    """``previous=``: ``None``, or a *kind* operation that is a creation (its own ``previous`` ``None`` and its
    ``overwrite`` ``False``) of the same definition — the same *name* and, for an index or an event, the same
    *table* (*what* names the kind of definition in the message)."""
    if previous is None:
        return
    if type(previous) is not kind:
        raise ValueError(f"{subject}: previous= must be a {kind.__name__}, got {type(previous).__name__}.")
    creation: Any = previous  # a CreateIndex, DefineAnalyzer, DefineEvent, DefineAccess or DefineFunction
    if creation.previous is not None or getattr(creation, "overwrite", False):
        raise ValueError(f"{subject}: previous= must be a creation: an operation without previous= and with overwrite=False.")
    if creation.name != name:
        raise ValueError(f"{subject}: previous= names {what} {creation.name!r}, not {name!r}.")
    if table is not None and creation.table != table:
        raise ValueError(f"{subject}: previous= is on table {creation.table!r}, not {table!r}.")


def _set(owner: object, values: Mapping[str, Any], *, rename: Callable[[str], str] = str) -> None:
    """Set each normalised value on the frozen *owner*, its key renamed by *rename*."""
    for key, value in values.items():
        object.__setattr__(owner, rename(key), value)


# --- table clauses ---------------------------------------------------------------------------------------------------

_SCHEMA_MODES = ("SCHEMAFULL", "SCHEMALESS")
_TABLE_TYPES = ("normal", "any", "relation")


def _schema_mode(value: object, *, subject: str, what: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, SchemaMode):
        return value.value
    if isinstance(value, str) and value.upper() in _SCHEMA_MODES:
        return value.upper()
    text = f"{subject}: {what} must be 'SCHEMAFULL' or 'SCHEMALESS' (any case), a SchemaMode or None"
    if not isinstance(value, str):
        raise TypeError(f"{text}, got {type(value).__name__}.")
    raise ValueError(f"{text}, got {value!r}.")


def _table_type(value: object, *, subject: str, what: str) -> str | None:
    """``None`` (no ``TYPE`` clause), ``"normal"``, ``"any"`` or ``"relation"`` in any case, or a ``TableType`` —
    ``USER``, ``STREAM`` and ``HASH`` write ``TYPE NORMAL``, as ``define_table()`` does."""
    if value is None:
        return None
    if isinstance(value, TableType):
        return value.value if value in (TableType.ANY, TableType.RELATION) else "normal"
    if isinstance(value, str) and value.lower() in _TABLE_TYPES:
        return value.lower()
    text = f"{subject}: {what} must be None, 'normal', 'any', 'relation' or a TableType"
    if not isinstance(value, str):
        raise TypeError(f"{text}, got {type(value).__name__}.")
    raise ValueError(f"{text}, got {value!r}.")


def _table_key(check: Callable[[], _T], *, key: str, argument: str) -> _T:
    """*check*'s result — a v0.34.0 checker of a model key, called with the operation as the model —, its messages
    naming the argument (``changefeed=``) instead of the model key (``model_config['changefeed']``)."""
    try:
        return check()
    except TypeError as error:
        raise TypeError(str(error).replace(f"model_config[{key!r}]", argument)) from error
    except ValueError as error:
        raise ValueError(str(error).replace(f"model_config[{key!r}]", argument)) from error


def _table_clauses(owner: object, *, subject: str, prefix: str) -> dict[str, Any]:
    """The table clauses of *owner* whose attributes are ``<prefix><key>``, checked by the appliers' checkers and
    normalised (keys without *prefix*): the schema mode upper-case, the type lower-case, the change feed as
    ``changefeed_text()`` writes it, the permissions as the four-operation map of ``permissions_rules()``, the view
    query stripped, the relation ends as tuples of names."""

    def given(key: str) -> Any:
        return getattr(owner, prefix + key)

    def argument(key: str) -> str:
        return f"{prefix}{key}="

    changefeed, permissions, comment, view_query = (
        given(key) for key in ("changefeed", "permissions", "comment", "view_query")
    )
    clauses: dict[str, Any] = {
        "schema_mode": _schema_mode(given("schema_mode"), subject=subject, what=argument("schema_mode")),
        "table_type": _table_type(given("table_type"), subject=subject, what=argument("table_type")),
        "changefeed": None
        if changefeed is None
        else _table_key(lambda: changefeed_text(changefeed, model=subject), key="changefeed", argument=argument("changefeed")),
        "permissions": None
        if permissions is None
        else _table_key(
            lambda: permissions_rules(permissions, model=subject), key="permissions", argument=argument("permissions")
        ),
        "comment": _table_key(lambda: comment_text(comment, model=subject), key="comment", argument=argument("comment")),
        "view_query": None
        if view_query is None
        else _table_key(lambda: check_view_query(view_query, model=subject), key="view_query", argument=argument("view_query")),
    }
    for end in ("relation_in", "relation_out"):
        check = partial(relation_tables, given(end), model=subject, key=end)
        clauses[end] = _table_key(check, key=end, argument=argument(end))
    clauses["enforced"] = _flag(given("enforced"), subject=subject, what=argument("enforced"))
    clauses["drop"] = _flag(given("drop"), subject=subject, what=argument("drop"))
    relation = bool(clauses["relation_in"] or clauses["relation_out"] or clauses["enforced"])
    if clauses["view_query"] is not None and (clauses["table_type"] is not None or relation):
        raise ValueError(
            f"{subject}: {argument('view_query')} takes no {argument('table_type')} and no relation key "
            f"({argument('relation_in')}, {argument('relation_out')}, {argument('enforced')}): a view's table is "
            "computed by SurrealDB."
        )
    if relation and clauses["table_type"] != "relation":
        raise ValueError(
            f"{subject}: {argument('relation_in')}, {argument('relation_out')} and {argument('enforced')} need "
            f"{prefix}table_type='relation'."
        )
    return clauses


def _table_text(owner: "CreateTable | AlterTable", *, prefix: str, mode: render.Mode) -> str:
    """The table statement of *owner*'s ``<prefix>`` clauses in *mode*."""

    def given(key: str) -> Any:
        return getattr(owner, prefix + key)

    return render.table_statement(
        owner.name,
        schema_mode=given("schema_mode"),
        table_type=given("table_type"),
        changefeed=given("changefeed"),
        permissions=given("permissions"),
        comment=given("comment"),
        view_query=given("view_query"),
        relation_in=given("relation_in"),
        relation_out=given("relation_out"),
        enforced=given("enforced"),
        drop=given("drop"),
        mode=mode,
    )


def _table_arguments(state: TableState) -> dict[str, Any]:
    """The arguments of a table operation that write *state*: every clause it holds — the change feed with ``INCLUDE
    ORIGINAL`` when it has it, ``DROP`` — and no ``TYPE`` for a view (SurrealDB's ``TYPE ANY``, which a view takes
    implicitly); permissions whose rules are all ``NONE`` (a table's default) become ``None``."""
    changefeed = state.changefeed
    if changefeed is not None and state.changefeed_include_original:
        changefeed += " INCLUDE ORIGINAL"
    return {
        "schema_mode": state.schema_mode,
        "table_type": None if state.view_query is not None else state.table_type,
        "changefeed": changefeed,
        "permissions": None if all(rule == "NONE" for rule in state.permissions.values()) else dict(state.permissions),
        "comment": state.comment,
        "view_query": state.view_query,
        "relation_in": tuple(state.relation_in),
        "relation_out": tuple(state.relation_out),
        "enforced": state.enforced,
        "drop": state.drop,
    }


# --- field clauses ---------------------------------------------------------------------------------------------------

_FIELD_OPERATIONS = ("select", "create", "update")
_ON_DELETE_WORDS = ("IGNORE", "UNSET", "CASCADE", "REJECT")
_ON_DELETE_THEN = re.compile(r"THEN\s+(.+)", re.IGNORECASE | re.DOTALL)
_WHERE = re.compile(r"WHERE\s+(.+)", re.IGNORECASE | re.DOTALL)


def _previous_key(key: str) -> str:
    """The ``previous_*`` attribute of a field clause: ``previous_type`` for the kind (the full ORM's name)."""
    return "previous_type" if key == "field_type" else f"previous_{key}"


def _field_path(name: str, path: object, *, subject: str) -> tuple[str, ...]:
    """A field's path segments: *path*, or *name* split at ``.`` when it is empty; each segment a non-empty ``str``
    without NUL (or ``*``), and *name* the segments joined with ``.`` (``FieldState``'s rule)."""
    if isinstance(path, str):
        raise TypeError(f"{subject}: path= takes a sequence of str, not a str: write path={[path]!r}.")
    if not isinstance(path, Sequence):
        raise TypeError(f"{subject}: path= must be a list or tuple of str, got {type(path).__name__}.")
    segments = tuple(path) or tuple(name.split("."))
    for segment in segments:
        if not isinstance(segment, str):
            raise TypeError(f"{subject}: path= holds a {type(segment).__name__}, not a str.")
        if not segment or "\x00" in segment:
            raise ValueError(f"{subject}: every path segment must be a non-empty str without a NUL character (\\x00).")
    joined = ".".join(segments)
    if joined != name:
        raise ValueError(f"{subject}: name= {name!r} is not its path joined with '.' ({joined!r}).")
    return segments


def _kind_text(value: object, *, subject: str, what: str) -> str:
    """A kind, parsed and written in canonical form (``"none | int"`` → ``"option<int>"``)."""
    if not isinstance(value, str):
        raise TypeError(f"{subject}: {what} must be a str such as 'int' or 'option<string>', got {type(value).__name__}.")
    try:
        return render_kind(parse_kind(value))
    except DefineParseError as error:
        raise ValueError(f"{subject}: {what} is not a SurrealQL kind the ORM can read: {error}.") from error


def _default(value: object, *, subject: str, what: str) -> object:
    """A ``DEFAULT`` (D8, D23): ``None``, a ``SurrealFunc`` or a ``DefaultExpression`` (written as they are), or a
    Python value ``to_surql_literal()`` writes — a ``str`` is always a string literal."""
    if value is None or isinstance(value, SurrealFunc | render.Expression):
        return value
    try:
        to_surql_literal(value)
    except (TypeError, ValueError, ArithmeticError) as error:
        raise TypeError(
            f"{subject}: {what} has no SurrealQL literal form (a {type(value).__name__} SurrealDB cannot store as "
            "written); write an expression as SurrealFunc(...) or DefaultExpression(...)."
        ) from error
    return value


def _on_delete(value: object, *, subject: str, what: str) -> str | None:
    """``REFERENCE ON DELETE``'s action: ``IGNORE``, ``UNSET``, ``CASCADE`` or ``REJECT`` (any case, kept upper-case) or
    ``THEN <expression>``."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{subject}: {what} must be a str, got {type(value).__name__}.")
    text = value.strip()
    if text.upper() in _ON_DELETE_WORDS:
        return text.upper()
    then = _ON_DELETE_THEN.fullmatch(text)
    if then is None:
        raise ValueError(f"{subject}: {what} must be IGNORE, UNSET, CASCADE, REJECT or 'THEN <expression>'.")
    return f"THEN {_expression(then.group(1), subject=subject, what=f'the expression of {what}')}"


def _rule(value: object, *, subject: str, what: str) -> str:
    """One permissions rule: ``FULL`` or ``NONE`` (any case, kept upper-case) or ``WHERE <condition>``."""
    if not isinstance(value, str):
        raise TypeError(f"{subject}: {what} must be a str, got {type(value).__name__}.")
    rule = value.strip()
    if rule.upper() in ("FULL", "NONE"):
        return rule.upper()
    where = _WHERE.fullmatch(rule)
    if where is None:
        raise ValueError(f"{subject}: {what} must be 'FULL', 'NONE' or 'WHERE <condition>'.")
    return f"WHERE {_expression(where.group(1), subject=subject, what=f'the condition of {what}')}"


def _field_permissions(value: object, *, subject: str, what: str) -> dict[str, str] | None:
    """A field's permissions (``FieldState``'s form): ``select``, ``create`` and ``update`` mapped to a rule; an
    operation left out is ``FULL``, a field's default."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError(
            f"{subject}: {what} must be a mapping of select, create and update to a rule, got {type(value).__name__}."
        )
    rules: dict[str, str] = {}
    for operation, rule in value.items():
        if operation not in _FIELD_OPERATIONS:
            raise ValueError(f"{subject}: {what} names the operation {operation!r}; a field's are select, create and update.")
        rules[operation] = _rule(rule, subject=subject, what=f"{what} rule for {operation!r}")
    return rules


def _field_clauses(owner: object, *, subject: str, previous: bool) -> dict[str, Any]:
    """The field clauses of *owner* — its target attributes, or its ``previous_*`` ones —, checked and normalised
    (keys as ``AddField`` names them)."""

    def attribute(key: str) -> str:
        return _previous_key(key) if previous else key

    def given(key: str) -> Any:
        return getattr(owner, attribute(key))

    def argument(key: str) -> str:
        return f"{attribute(key)}="

    kind = given("field_type")
    clauses: dict[str, Any] = {
        "field_type": None if kind is None else _kind_text(kind, subject=subject, what=argument("field_type")),
        "default": _default(given("default"), subject=subject, what=argument("default")),
        "comment": _comment(given("comment"), subject=subject, what=argument("comment")),
        "on_delete": _on_delete(given("on_delete"), subject=subject, what=argument("on_delete")),
        "permissions": _field_permissions(given("permissions"), subject=subject, what=argument("permissions")),
    }
    for key in ("assertion", "value", "computed"):
        clauses[key] = _expression(given(key), subject=subject, what=argument(key))
    for key in ("flexible", "readonly", "reference", "nullable", "default_always"):
        clauses[key] = _flag(given(key), subject=subject, what=argument(key))
    if clauses["on_delete"] is not None and not clauses["reference"]:
        raise ValueError(f"{subject}: {argument('on_delete')} needs {attribute('reference')}=True.")
    if clauses["computed"] is not None:
        set_with_computed = (
            ("default", clauses["default"] is not None),
            ("value", clauses["value"] is not None),
            ("readonly", clauses["readonly"]),
            ("assertion", clauses["assertion"] is not None),
            ("reference", clauses["reference"]),
        )
        clashing = [argument(key) for key, given_ in set_with_computed if given_]
        if clashing:
            raise ValueError(
                f"{subject}: {argument('computed')} cannot be combined with {', '.join(clashing)} (SurrealDB refuses "
                "them with COMPUTED)."
            )
    if clauses["default_always"] and clauses["default"] is None:
        raise ValueError(f"{subject}: {attribute('default_always')}=True needs {argument('default')} (DEFAULT ALWAYS).")
    return clauses


def _default_value(text: str | None) -> object:
    """The ``default=`` of a ``FieldState.default`` text: the Python value ``render.literal_value()`` reads when it is
    written back as the same literal (``compare.literal_equal()``: ``1.5f`` gives ``1.5``, ``1.1dec``
    ``Decimal('1.1')``), else a ``DefaultExpression`` (``time::now()``, ``NONE``); ``'a;b'`` gives the ``str``."""
    if text is None:
        return None
    value = render.literal_value(text)
    if value is not None and compare.literal_equal(to_surql_literal(value), text):
        return value
    return DefaultExpression(text)


def _field_arguments(state: FieldState) -> dict[str, Any]:
    """The field clauses of *state* as ``AddField`` arguments; permissions whose rules are all ``FULL`` (a field's
    default) become ``None``."""
    permissions = state.permissions
    return {
        "field_type": state.field_type,
        "default": _default_value(state.default),
        "assertion": state.assertion,
        "flexible": state.flexible,
        "readonly": state.readonly,
        "value": state.value,
        "reference": state.reference,
        "on_delete": state.on_delete,
        "nullable": state.nullable,
        "comment": state.comment,
        "permissions": None if all(rule == "FULL" for rule in permissions.values()) else dict(permissions),
        "default_always": state.default_always,
        "computed": state.computed,
    }


def _path_argument(state: FieldState) -> tuple[str, ...]:
    """``path=`` for *state*: its segments when one holds a dot, else ``()`` (derived from the name)."""
    path = tuple(state.path)
    return () if path == tuple(state.name.split(".")) else path


# --- DefaultExpression -----------------------------------------------------------------------------------------------


@dataclass(frozen=True, repr=False)
class DefaultExpression(render.Expression):
    """One SurrealQL expression written as is after ``DEFAULT`` (lite, D23) — a default **taken from a schema state**:
    the text SurrealDB rendered or the ORM's renderer wrote (``time::now()``, ``NONE``, ``{ LET $x = 1; RETURN $x;
    }``).

    The diff gives a default it cannot write back as a Python literal this type; a hand-written migration may use it
    or ``SurrealFunc``. Checked by ``define_parser.scan_fragment()`` with the one-expression rule: no ``;`` outside
    brackets — so ``'a;b'`` and a block holding statements are accepted, unlike ``SurrealFunc``, which refuses any
    ``;``. Its refusals name the argument, never the text; ``repr()`` is ``DefaultExpression(...)``. Kept stripped.
    """

    def __post_init__(self) -> None:
        text: object = self.text
        if not isinstance(text, str):
            raise TypeError(f"DefaultExpression: the text must be a str, got {type(text).__name__}.")
        stripped = text.strip()
        reason = None
        if not stripped:
            reason = "it is empty"
        else:
            try:
                if scan_fragment(stripped):
                    reason = "it holds a ';' outside brackets"
            except DefineParseError as error:
                reason = str(error)
        if reason is not None:
            raise ValueError(f"DefaultExpression: the text is not one SurrealQL expression: {reason}")
        object.__setattr__(self, "text", stripped)

    def __repr__(self) -> str:
        return "DefaultExpression(...)"


# --- tables ----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CreateTable(Operation):
    """Create a table, a view (``view_query``) or a relation (``table_type="relation"``): the plain ``DEFINE TABLE``,
    which fails when the table exists — redefining one is :class:`AlterTable`.

    ``schema_mode`` is ``"SCHEMAFULL"`` (the default) or ``"SCHEMALESS"`` (any case), a ``SchemaMode``, or ``None``
    (no schema keyword: SurrealDB's ``SCHEMALESS``); ``table_type`` ``None`` (no ``TYPE`` clause: SurrealDB's ``TYPE
    ANY``), ``"normal"``, ``"any"``, ``"relation"``
    or a ``TableType``; ``changefeed`` ``"7d"`` or ``"1h INCLUDE ORIGINAL"``; ``permissions`` maps ``select``,
    ``create``, ``update``, ``delete`` to ``FULL``, ``NONE`` or a ``WHERE`` condition (an operation left out is
    ``NONE``); ``relation_in``/``relation_out`` (a name, ``"a | b"`` or a sequence) and ``enforced`` need
    ``table_type="relation"``; ``view_query`` takes neither. ``drop=True`` (lite) writes ``DROP``.

    ::

        CreateTable(name="person", table_type="normal", comment="People")
        # DEFINE TABLE `person` TYPE NORMAL SCHEMAFULL COMMENT 'People';
    """

    name: str
    schema_mode: str | SchemaMode | None = "SCHEMAFULL"
    table_type: str | TableType | None = None
    changefeed: str | None = None
    permissions: Mapping[str, str] | None = field(default=None, repr=False)
    comment: str | None = None
    view_query: str | None = field(default=None, repr=False)
    relation_in: str | Sequence[str] | None = None
    relation_out: str | Sequence[str] | None = None
    enforced: bool = False
    _: KW_ONLY
    drop: bool = False

    def __post_init__(self) -> None:
        name = _definition_name(self.name, subject="CreateTable", what="Table")
        _set(self, _table_clauses(self, subject=f"CreateTable {name!r}", prefix=""))

    @property
    def reversible(self) -> bool:
        return True

    def forwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        return _table_text(self, prefix="", mode="plain")

    def backwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        return render.removal("table", self.name, if_exists=True)

    def describe(self) -> str:
        return f"Create table {self.name}"

    def state_forwards(self, state: SchemaState) -> SchemaState:
        return _placed(state, self.forwards())

    def state_backwards(self, state: SchemaState) -> SchemaState:
        return _state.take_out(state, "table", self.name)

    @classmethod
    def from_table_state(cls, state: TableState) -> "CreateTable":
        """The operation that creates *state*: every clause it holds (the change feed with ``INCLUDE ORIGINAL``,
        ``DROP``); ``NONE``-only permissions become ``None``."""
        return cls(name=state.name, **_table_arguments(state))


def _makes_schemafull(mode: object, other: object) -> bool:
    """Whether a table statement with the schema mode *mode* makes ``SCHEMAFULL`` a table whose mode was *other*
    (``None`` included: unknown, D32)."""
    return mode == "SCHEMAFULL" and other != "SCHEMAFULL"


@dataclass(frozen=True)
class AlterTable(Operation):
    """Redefine a table: ``DEFINE TABLE OVERWRITE`` with every target clause it is given and nothing else (an
    ``OVERWRITE`` resets the others, M13); the table's fields, indexes and events are kept (M29).

    Reversible when ``previous_schema_mode`` is given: ``backwards()`` writes the ``previous_*`` clauses. On SurrealDB
    2.x and the embedded engine (``line=2``), when ``forwards()`` makes the table ``SCHEMAFULL`` — ``schema_mode``
    ``SCHEMAFULL`` and ``previous_schema_mode`` not (``None`` included, D32) —, the table statement is followed by one
    ``DEFINE FIELD OVERWRITE … FLEXIBLE;`` per column of the table in *state* (the state after the migration) that
    ``render.flexible_before_3()`` selects, one statement per line, but the ``skip_fields`` the migration defines
    itself (M27, R33); ``backwards()`` does the same when it restores ``SCHEMAFULL``, from the state before the
    migration. SurrealDB 3.x keeps the values without them.
    """

    name: str
    schema_mode: str | SchemaMode | None = None
    table_type: str | TableType | None = None
    changefeed: str | None = None
    permissions: Mapping[str, str] | None = field(default=None, repr=False)
    comment: str | None = None
    view_query: str | None = field(default=None, repr=False)
    relation_in: str | Sequence[str] | None = None
    relation_out: str | Sequence[str] | None = None
    enforced: bool = False
    previous_schema_mode: str | SchemaMode | None = None
    previous_table_type: str | TableType | None = None
    previous_changefeed: str | None = None
    previous_permissions: Mapping[str, str] | None = field(default=None, repr=False)
    previous_comment: str | None = None
    previous_view_query: str | None = field(default=None, repr=False)
    previous_relation_in: str | Sequence[str] | None = None
    previous_relation_out: str | Sequence[str] | None = None
    previous_enforced: bool = False
    _: KW_ONLY
    drop: bool = False
    previous_drop: bool = False

    def __post_init__(self) -> None:
        name = _definition_name(self.name, subject="AlterTable", what="Table")
        subject = f"AlterTable {name!r}"
        target = _table_clauses(self, subject=subject, prefix="")
        previous = _table_clauses(self, subject=subject, prefix="previous_")
        _set(self, target)
        _set(self, previous, rename=lambda key: f"previous_{key}")

    @property
    def reversible(self) -> bool:
        return self.previous_schema_mode is not None

    def _with_redefinitions(
        self, statement: str, *, line: int, state: SchemaState | None, restores: bool, skip_fields: Collection[str]
    ) -> str:
        """*statement*, followed on line 2 by the ``FLEXIBLE`` redefinitions when it makes the table ``SCHEMAFULL``."""
        if line != 2 or state is None or not restores:
            return statement
        return "\n".join([statement, *render.flexible_redefinitions(self.name, state, skip=skip_fields)])

    def forwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None, skip_fields: Collection[str] = ()) -> str:
        checked = checked_line(line)
        statement = _table_text(self, prefix="", mode="overwrite")
        restores = _makes_schemafull(self.schema_mode, self.previous_schema_mode)
        return self._with_redefinitions(statement, line=checked, state=state, restores=restores, skip_fields=skip_fields)

    def backwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None, skip_fields: Collection[str] = ()) -> str:
        checked = checked_line(line)
        if not self.reversible:
            return ""
        statement = _table_text(self, prefix="previous_", mode="overwrite")
        restores = _makes_schemafull(self.previous_schema_mode, self.schema_mode)
        return self._with_redefinitions(statement, line=checked, state=state, restores=restores, skip_fields=skip_fields)

    def describe(self) -> str:
        return f"Alter table {self.name}"

    def state_forwards(self, state: SchemaState) -> SchemaState:
        return _placed(state, self.forwards())

    def state_backwards(self, state: SchemaState) -> SchemaState:
        if not self.reversible:
            raise _not_reversible(self)
        return _placed(state, self.backwards())

    @classmethod
    def from_table_state(cls, state: TableState) -> "AlterTable":
        """The operation that redefines a table as *state*, without its previous definition (not reversible)."""
        return cls(name=state.name, **_table_arguments(state))

    @classmethod
    def from_table_states(cls, current: TableState, target: TableState) -> "AlterTable":
        """The operation that redefines *current* as *target*: every clause of both."""
        previous = {f"previous_{key}": value for key, value in _table_arguments(current).items()}
        return cls(name=target.name, **_table_arguments(target), **previous)


@dataclass(frozen=True)
class DropTable(Operation):
    """Remove a table with its records, fields, indexes and events: ``REMOVE TABLE``. Not reversible (the records are
    gone). The name may be any name a database holds (D26), written in backticks."""

    name: str

    def __post_init__(self) -> None:
        _removal_name(self.name, subject="DropTable", what="name=")

    @property
    def reversible(self) -> bool:
        return False

    @property
    def destructive(self) -> bool:
        return True

    def forwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        return render.removal("table", self.name)

    def backwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        return ""

    def describe(self) -> str:
        return f"Drop table {_shown(self.name)}"

    def state_forwards(self, state: SchemaState) -> SchemaState:
        return _state.take_out(state, "table", self.name)

    def state_backwards(self, state: SchemaState) -> SchemaState:
        raise _not_reversible(self)


# --- fields ----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AddField(Operation):
    """Define a field: the plain ``DEFINE FIELD`` (``OVERWRITE`` with ``overwrite=True``), undone by ``REMOVE FIELD IF
    EXISTS``.

    ``name`` is the canonical path (``"address.city"``, ``"tags.*"``); ``path=`` only for a segment holding a dot.
    ``field_type`` is kind text, kept canonical; ``nullable=True`` wraps it in ``option<…>`` unless it admits ``NONE``
    already. ``default`` is a Python value written as a literal (a ``str`` is a string), a ``SurrealFunc`` or a
    :class:`DefaultExpression`; ``default_always=True`` (lite) writes ``DEFAULT ALWAYS``. ``computed`` (lite, SurrealDB
    3.x) writes ``COMPUTED <expr>`` and refuses ``default``, ``value``, ``readonly``, ``assertion`` and ``reference``.
    ``on_delete`` needs ``reference=True``. ``permissions`` maps ``select``/``create``/``update`` to a rule.
    ``encrypted`` does not exist: a full-ORM positional call reaching its 6th argument raises ``TypeError``.

    On SurrealDB 2.x and the embedded engine (``line=2``), ``FLEXIBLE`` is also written when *state* holds the table
    ``SCHEMAFULL`` and the kind holds ``any`` (or is a list of sub-models with such a child, v0.34.0, R25).
    """

    table: str
    name: str
    field_type: str
    default: Any = field(default=None, repr=False)
    assertion: str | None = field(default=None, repr=False)
    _: KW_ONLY
    flexible: bool = False
    readonly: bool = False
    value: str | None = field(default=None, repr=False)
    comment: str | None = None
    reference: bool = False
    on_delete: str | None = field(default=None, repr=False)
    nullable: bool = False
    overwrite: bool = False
    permissions: Mapping[str, str] | None = field(default=None, repr=False)
    path: Sequence[str] = ()
    default_always: bool = False
    computed: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        table = _definition_name(self.table, subject="AddField", what="Table")
        name = _removal_name(self.name, subject="AddField", what="name=")
        subject = f"AddField {name!r} on {table!r}"
        path = _field_path(name, self.path, subject=subject)
        field_type: object = self.field_type
        if field_type is None:
            raise TypeError(f"{subject}: field_type= must be a str such as 'int' or 'option<string>', got NoneType.")
        clauses = _field_clauses(self, subject=subject, previous=False)
        _flag(self.overwrite, subject=subject, what="overwrite=")
        object.__setattr__(self, "path", path)
        _set(self, clauses)

    @property
    def reversible(self) -> bool:
        return True

    def forwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        mode: render.Mode = "overwrite" if self.overwrite else "plain"
        return render.field_statement(self.table, render.field_state(self), mode=mode, line=line, state=state)

    def backwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        return render.removal("field", self.name, table=self.table, path=self.path, if_exists=True)

    def describe(self) -> str:
        return f"Add field {_shown_field(self.name, self.path)} to {self.table}"

    def state_forwards(self, state: SchemaState) -> SchemaState:
        return _placed(state, self.forwards())

    def state_backwards(self, state: SchemaState) -> SchemaState:
        return _state.take_out(state, "field", self.name, self.table)

    @classmethod
    def from_field_state(cls, table: str, state: FieldState) -> "AddField":
        """The operation that defines *state* on *table*: every clause it holds (``default_always``, ``computed``),
        its ``default`` text read back as a Python value or a :class:`DefaultExpression`, ``FULL``-only permissions as
        ``None``."""
        return cls(table=table, name=state.name, path=_path_argument(state), **_field_arguments(state))


@dataclass(frozen=True)
class AlterField(Operation):
    """Redefine a field: ``DEFINE FIELD OVERWRITE`` with the target clauses (``field_type=None`` writes no ``TYPE``).

    Reversible when ``previous_type`` is given: ``backwards()`` writes the ``previous_*`` clauses, every clause a field
    statement holds (an ``OVERWRITE`` resets the others). The arguments are :class:`AddField`'s, the ``previous_*``
    ones checked the same way.
    """

    table: str
    name: str
    field_type: str | None = None
    default: Any = field(default=None, repr=False)
    assertion: str | None = field(default=None, repr=False)
    _: KW_ONLY
    flexible: bool = False
    readonly: bool = False
    value: str | None = field(default=None, repr=False)
    reference: bool = False
    on_delete: str | None = field(default=None, repr=False)
    nullable: bool = False
    comment: str | None = None
    permissions: Mapping[str, str] | None = field(default=None, repr=False)
    path: Sequence[str] = ()
    default_always: bool = False
    computed: str | None = field(default=None, repr=False)
    previous_type: str | None = None
    previous_default: Any = field(default=None, repr=False)
    previous_assertion: str | None = field(default=None, repr=False)
    previous_flexible: bool = False
    previous_readonly: bool = False
    previous_nullable: bool = False
    previous_reference: bool = False
    previous_on_delete: str | None = field(default=None, repr=False)
    previous_value: str | None = field(default=None, repr=False)
    previous_comment: str | None = None
    previous_permissions: Mapping[str, str] | None = field(default=None, repr=False)
    previous_default_always: bool = False
    previous_computed: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        table = _definition_name(self.table, subject="AlterField", what="Table")
        name = _removal_name(self.name, subject="AlterField", what="name=")
        subject = f"AlterField {name!r} on {table!r}"
        path = _field_path(name, self.path, subject=subject)
        target = _field_clauses(self, subject=subject, previous=False)
        previous = _field_clauses(self, subject=subject, previous=True)
        object.__setattr__(self, "path", path)
        _set(self, target)
        _set(self, previous, rename=_previous_key)

    @property
    def reversible(self) -> bool:
        return self.previous_type is not None

    def forwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        return render.field_statement(
            self.table,
            render.field_state(self),
            mode="overwrite",
            line=line,
            state=state,
            typed=self.field_type is not None,
        )

    def backwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        if not self.reversible:
            return ""
        return render.field_statement(
            self.table, render.field_state(self, previous=True), mode="overwrite", line=line, state=state
        )

    def describe(self) -> str:
        return f"Alter field {_shown_field(self.name, self.path)} on {self.table}"

    def state_forwards(self, state: SchemaState) -> SchemaState:
        return _placed(state, self.forwards())

    def state_backwards(self, state: SchemaState) -> SchemaState:
        if not self.reversible:
            raise _not_reversible(self)
        return _placed(state, self.backwards())

    @classmethod
    def from_field_states(cls, table: str, current: FieldState, target: FieldState) -> "AlterField":
        """The operation that redefines *current* as *target* on *table*: every clause of both."""
        previous = {_previous_key(key): value for key, value in _field_arguments(current).items()}
        return cls(table=table, name=target.name, path=_path_argument(target), **_field_arguments(target), **previous)


@dataclass(frozen=True)
class DropField(Operation):
    """Remove a field: ``REMOVE FIELD``; its children stay (M30). Not reversible: a ``SCHEMAFULL`` table drops the
    values at the next write. The names may be any a database holds (D26), written in backticks."""

    table: str
    name: str
    _: KW_ONLY
    path: Sequence[str] = ()

    def __post_init__(self) -> None:
        table = _removal_name(self.table, subject="DropField", what="table=")
        name = _removal_name(self.name, subject="DropField", what="name=")
        object.__setattr__(self, "path", _field_path(name, self.path, subject=f"DropField {name!r} from {table!r}"))

    @property
    def reversible(self) -> bool:
        return False

    @property
    def destructive(self) -> bool:
        return True

    def forwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        return render.removal("field", self.name, table=self.table, path=self.path)

    def backwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        return ""

    def describe(self) -> str:
        return f"Drop field {_shown_field(self.name, self.path)} from {_shown(self.table)}"

    def state_forwards(self, state: SchemaState) -> SchemaState:
        return _state.take_out(state, "field", self.name, self.table)

    def state_backwards(self, state: SchemaState) -> SchemaState:
        raise _not_reversible(self)


# --- indexes ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CreateIndex(Operation):
    """Define an index: ``Index(...).ddl(table, line=…)``, the plain form unless ``overwrite`` or ``previous`` — a
    unique, full-text (``search_analyzer``, an analyzer's name) or vector (``hnsw=True``) index, the full ORM's
    arguments checked by ``search.Index``. ``line=2`` writes the full-text keyword ``SEARCH``, 3 ``FULLTEXT``.

    Undone by ``REMOVE INDEX IF EXISTS``, or, with ``previous=`` (a ``CreateIndex`` of the same index), by its
    statement with ``OVERWRITE``; ``overwrite=True`` without ``previous`` is not reversible. ``concurrently=True``
    is refused: an index is built inside its migration's transaction.
    """

    table: str
    name: str
    fields: Sequence[str]
    unique: bool = False
    search_analyzer: str | None = None
    bm25: tuple[float, float] | bool | None = None
    highlights: bool = False
    hnsw: bool = False
    dimension: int | None = None
    dist: str | None = None
    vector_type: str | None = None
    efc: int | None = None
    m: int | None = None
    concurrently: bool = False
    comment: str | None = None
    _: KW_ONLY
    overwrite: bool = False
    previous: "CreateIndex | None" = None

    def __post_init__(self) -> None:
        table = _definition_name(self.table, subject="CreateIndex", what="Table")
        name = _definition_name(self.name, subject="CreateIndex", what="Index")
        subject = f"CreateIndex {name!r} on {table!r}"
        if self.concurrently is True:
            raise ValueError(
                "CreateIndex(concurrently=True) is not supported yet: an index is built inside its migration's transaction."
            )
        _flag(self.concurrently, subject=subject, what="concurrently=")
        analyzer: object = self.search_analyzer
        if analyzer is not None and not isinstance(analyzer, str):
            raise TypeError(f"{subject}: search_analyzer= names an analyzer (a str), got {type(analyzer).__name__}.")
        with _refusal(subject):
            index = self._index()
        _flag(self.overwrite, subject=subject, what="overwrite=")
        _check_previous(self.previous, subject=subject, kind=CreateIndex, what="index", name=name, table=table)
        for key in ("fields", "bm25", "dist", "vector_type", "comment"):
            object.__setattr__(self, key, getattr(index, key))

    def _index(self) -> Index:
        return Index(
            self.name,
            self.fields,
            unique=self.unique,
            search_analyzer=self.search_analyzer,
            bm25=self.bm25,
            highlights=self.highlights,
            comment=self.comment,
            hnsw=self.hnsw,
            dimension=self.dimension,
            dist=self.dist,
            vector_type=self.vector_type,
            efc=self.efc,
            m=self.m,
        )

    @property
    def reversible(self) -> bool:
        return self.previous is not None or not self.overwrite

    def forwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        mode: render.Mode = "overwrite" if self.overwrite or self.previous is not None else "plain"
        return render.index_statement(self._index(), self.table, mode=mode, line=checked_line(line))

    def backwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked = checked_line(line)
        if self.previous is not None:
            return render.index_statement(self.previous._index(), self.table, mode="overwrite", line=checked)
        if self.overwrite:
            return ""
        return render.removal("index", self.name, table=self.table, if_exists=True)

    def describe(self) -> str:
        return f"Create index {self.name} on {self.table}"

    def state_forwards(self, state: SchemaState) -> SchemaState:
        return _placed(state, self.forwards())

    def state_backwards(self, state: SchemaState) -> SchemaState:
        if self.previous is not None:
            return _placed(state, self.backwards())
        if self.overwrite:
            raise _not_reversible(self)
        return _state.take_out(state, "index", self.name, self.table)

    @classmethod
    def from_index_state(cls, table: str, state: IndexState) -> "CreateIndex | None":
        """The operation that defines *state* on *table*, or ``None`` for an index it cannot write: ``MTREE``,
        ``COUNT``, ``CONCURRENTLY``, or one ``search.Index`` refuses (a field path such as ``tags.*``)."""
        if state.mtree or state.count or state.concurrently:
            return None
        try:
            return cls(
                table,
                state.name,
                list(state.fields),
                unique=state.unique,
                search_analyzer=state.search_analyzer,
                bm25=state.bm25,
                highlights=state.highlights,
                hnsw=state.hnsw,
                dimension=state.dimension,
                dist=state.dist,
                vector_type=state.vector_type,
                efc=state.efc,
                m=state.m,
                comment=state.comment,
            )
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True)
class DropIndex(Operation):
    """Remove an index: ``REMOVE INDEX``. Reversible with ``previous=`` (the ``CreateIndex`` that defines it), whose
    plain statement ``backwards()`` writes. The names may be any a database holds (D26)."""

    table: str
    name: str
    _: KW_ONLY
    previous: CreateIndex | None = None

    def __post_init__(self) -> None:
        table = _removal_name(self.table, subject="DropIndex", what="table=")
        name = _removal_name(self.name, subject="DropIndex", what="name=")
        subject = f"DropIndex {name!r} from {table!r}"
        _check_previous(self.previous, subject=subject, kind=CreateIndex, what="index", name=name, table=table)

    @property
    def reversible(self) -> bool:
        return self.previous is not None

    @property
    def destructive(self) -> bool:
        return True

    def forwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        return render.removal("index", self.name, table=self.table)

    def backwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked = checked_line(line)
        return "" if self.previous is None else self.previous.forwards(line=checked)

    def describe(self) -> str:
        return f"Drop index {_shown(self.name)} from {_shown(self.table)}"

    def state_forwards(self, state: SchemaState) -> SchemaState:
        return _state.take_out(state, "index", self.name, self.table)

    def state_backwards(self, state: SchemaState) -> SchemaState:
        if self.previous is None:
            raise _not_reversible(self)
        return _placed(state, self.backwards())


@dataclass(frozen=True)
class RebuildIndex(Operation):
    """Rebuild an index: ``REBUILD INDEX[ IF EXISTS]`` — after its analyzer was redefined (an index keeps the tokens of
    the analyzer it was built with, v0.35.0). Its own backwards (D7); no schema effect."""

    table: str
    name: str
    if_exists: bool = False

    def __post_init__(self) -> None:
        table = _definition_name(self.table, subject="RebuildIndex", what="Table")
        name = _definition_name(self.name, subject="RebuildIndex", what="Index")
        _flag(self.if_exists, subject=f"RebuildIndex {name!r} on {table!r}", what="if_exists=")

    @property
    def reversible(self) -> bool:
        return True

    def forwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        exists = " IF EXISTS" if self.if_exists else ""
        return f"REBUILD INDEX{exists} {_identifier(self.name)} ON {_identifier(self.table)};"

    def backwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        return self.forwards(line=line)

    def describe(self) -> str:
        return f"Rebuild index {self.name} on {self.table}"

    def state_forwards(self, state: SchemaState) -> SchemaState:
        return state

    def state_backwards(self, state: SchemaState) -> SchemaState:
        return state


# --- analyzers -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DefineAnalyzer(Operation):
    """Define a full-text analyzer: ``Analyzer(...).ddl(overwrite=True)`` — always ``OVERWRITE`` (idempotent: the
    executor commits analyzers before the batch whose indexes use them). Tokenizers and filters are checked and
    normalised by ``search.Analyzer``.

    Undone by ``REMOVE ANALYZER IF EXISTS``, or, with ``previous=`` (a ``DefineAnalyzer`` of the same name), by its
    statement.
    """

    name: str
    tokenizers: Sequence[str] = ()
    filters: Sequence[str] = ()
    _: KW_ONLY
    function: str | None = None
    comment: str | None = None
    previous: "DefineAnalyzer | None" = None

    def __post_init__(self) -> None:
        name = _definition_name(self.name, subject="DefineAnalyzer", what="Analyzer")
        subject = f"DefineAnalyzer {name!r}"
        with _refusal(subject):
            analyzer = self._analyzer()
        _check_previous(self.previous, subject=subject, kind=DefineAnalyzer, what="analyzer", name=name)
        for key in ("tokenizers", "filters", "function"):
            object.__setattr__(self, key, getattr(analyzer, key))

    def _analyzer(self) -> Analyzer:
        return Analyzer(self.name, self.tokenizers, self.filters, function=self.function, comment=self.comment)

    @property
    def reversible(self) -> bool:
        return True

    def forwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        return self._analyzer().ddl(overwrite=True)

    def backwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        if self.previous is not None:
            return self.previous.forwards()
        return render.removal("analyzer", self.name, if_exists=True)

    def describe(self) -> str:
        return f"Define analyzer {self.name}"

    def state_forwards(self, state: SchemaState) -> SchemaState:
        return _placed(state, self.forwards())

    def state_backwards(self, state: SchemaState) -> SchemaState:
        if self.previous is not None:
            return _placed(state, self.backwards())
        return _state.take_out(state, "analyzer", self.name)

    @classmethod
    def from_analyzer_state(cls, state: AnalyzerState) -> "DefineAnalyzer | None":
        """The operation that defines *state*, or ``None`` when ``search.Analyzer`` refuses it (a filter SurrealDB
        stores and the lite cannot write, such as ``edgengram(10,2)``, M32)."""
        try:
            return cls(state.name, state.tokenizers, state.filters, function=state.function, comment=state.comment)
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True)
class RemoveAnalyzer(Operation):
    """Remove an analyzer: ``REMOVE ANALYZER``. Reversible with ``previous=`` (its ``DefineAnalyzer``)."""

    name: str
    _: KW_ONLY
    previous: DefineAnalyzer | None = None

    def __post_init__(self) -> None:
        name = _removal_name(self.name, subject="RemoveAnalyzer", what="name=")
        _check_previous(self.previous, subject=f"RemoveAnalyzer {name!r}", kind=DefineAnalyzer, what="analyzer", name=name)

    @property
    def reversible(self) -> bool:
        return self.previous is not None

    @property
    def destructive(self) -> bool:
        return True

    def forwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        return render.removal("analyzer", self.name)

    def backwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        return "" if self.previous is None else self.previous.forwards()

    def describe(self) -> str:
        return f"Remove analyzer {_shown(self.name)}"

    def state_forwards(self, state: SchemaState) -> SchemaState:
        return _state.take_out(state, "analyzer", self.name)

    def state_backwards(self, state: SchemaState) -> SchemaState:
        if self.previous is None:
            raise _not_reversible(self)
        return _placed(state, self.backwards())


# --- events ----------------------------------------------------------------------------------------------------------


def _block_content(text: str) -> str:
    """*text* without one ``{ … }`` block around it all — the content ``Event.ddl()`` wraps in a block again — or
    *text* stripped when it is not one block."""
    stripped = text.strip()
    if not stripped.startswith("{"):
        return stripped
    blanked = blank_literals(stripped)
    depth = 0
    for index, char in enumerate(blanked):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return stripped[1:index].strip() if index == len(stripped) - 1 else stripped
    return stripped


@dataclass(frozen=True)
class DefineEvent(Operation):
    """Define an event: ``Event(...).ddl(table)``, the plain form unless ``overwrite`` or ``previous`` — ``when`` one
    expression or ``None`` (no ``WHEN``), ``then`` written as a block (``THEN { … }``), ``async_mode`` (SurrealDB
    3.x's ``ASYNC``) with ``retry``/``max_depth``, checked by ``schema.Event``.

    Undone by ``REMOVE EVENT IF EXISTS``, or, with ``previous=`` (a ``DefineEvent`` of the same event), by its
    statement with ``OVERWRITE``; ``overwrite=True`` without ``previous`` is not reversible.
    """

    name: str
    table: str
    when: str | None = field(repr=False)
    then: str = field(repr=False)
    comment: str | None = None
    _: KW_ONLY
    async_mode: bool = False
    retry: int | None = None
    max_depth: int | None = None
    overwrite: bool = False
    previous: "DefineEvent | None" = None

    def __post_init__(self) -> None:
        name = _definition_name(self.name, subject="DefineEvent", what="Event")
        table = _definition_name(self.table, subject="DefineEvent", what="Table")
        subject = f"DefineEvent {name!r} on {table!r}"
        with _refusal(subject):
            event = self._event()
        _flag(self.overwrite, subject=subject, what="overwrite=")
        _check_previous(self.previous, subject=subject, kind=DefineEvent, what="event", name=name, table=table)
        object.__setattr__(self, "when", event.when)
        object.__setattr__(self, "then", event.then)

    def _event(self) -> Event:
        return Event(
            self.name,
            then=self.then,
            when=self.when,
            async_mode=self.async_mode,
            retry=self.retry,
            max_depth=self.max_depth,
            comment=self.comment,
        )

    @property
    def reversible(self) -> bool:
        return self.previous is not None or not self.overwrite

    def forwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        statement = self._event().ddl(self.table)
        return statement if self.overwrite or self.previous is not None else render.plain(statement)

    def backwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        if self.previous is not None:
            return self.previous._event().ddl(self.table)
        if self.overwrite:
            return ""
        return render.removal("event", self.name, table=self.table, if_exists=True)

    def describe(self) -> str:
        return f"Define event {self.name} on {self.table}"

    def state_forwards(self, state: SchemaState) -> SchemaState:
        return _placed(state, self.forwards())

    def state_backwards(self, state: SchemaState) -> SchemaState:
        if self.previous is not None:
            return _placed(state, self.backwards())
        if self.overwrite:
            raise _not_reversible(self)
        return _state.take_out(state, "event", self.name, self.table)

    @classmethod
    def from_event_state(cls, state: EventState) -> "DefineEvent | None":
        """The operation that defines *state*, or ``None`` when ``schema.Event`` refuses it. ``then`` loses the one
        ``{ … }`` block around it (the servers render the block ``Event.ddl()`` writes), so it is not wrapped twice;
        ``WHEN true`` — what the servers render for an event without ``WHEN`` — is ``when=None``, so the operation
        writes the statement ``Event.ddl()`` writes for that event."""
        try:
            return cls(
                state.name,
                state.table,
                None if state.when.strip().lower() == "true" else state.when,
                _block_content(state.then),
                state.comment,
                async_mode=state.async_mode,
                retry=state.retry,
                max_depth=state.max_depth,
            )
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True)
class RemoveEvent(Operation):
    """Remove an event: ``REMOVE EVENT``. Reversible with ``previous=`` (its ``DefineEvent``), whose plain statement
    ``backwards()`` writes. The names may be any a database holds (D26)."""

    name: str
    table: str
    _: KW_ONLY
    previous: DefineEvent | None = None

    def __post_init__(self) -> None:
        name = _removal_name(self.name, subject="RemoveEvent", what="name=")
        table = _removal_name(self.table, subject="RemoveEvent", what="table=")
        subject = f"RemoveEvent {name!r} from {table!r}"
        _check_previous(self.previous, subject=subject, kind=DefineEvent, what="event", name=name, table=table)

    @property
    def reversible(self) -> bool:
        return self.previous is not None

    @property
    def destructive(self) -> bool:
        return True

    def forwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        return render.removal("event", self.name, table=self.table)

    def backwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked = checked_line(line)
        return "" if self.previous is None else self.previous.forwards(line=checked)

    def describe(self) -> str:
        return f"Remove event {_shown(self.name)} from {_shown(self.table)}"

    def state_forwards(self, state: SchemaState) -> SchemaState:
        return _state.take_out(state, "event", self.name, self.table)

    def state_backwards(self, state: SchemaState) -> SchemaState:
        if self.previous is None:
            raise _not_reversible(self)
        return _placed(state, self.backwards())


# --- accesses --------------------------------------------------------------------------------------------------------

_DURATION_FORM = re.compile(rf"{_DURATION}|NONE")
#: The ``WITH JWT`` clauses ``DefineAccess`` cannot write: it writes none, and SurrealDB generates an ``HS512`` key.
_JWT_KEYWORDS = ("AUTHENTICATE", "URL", "ALGORITHM")


def _duration(value: object, *, subject: str, what: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{subject}: {what} must be a str such as '15m' or '12h', got {type(value).__name__}.")
    if _DURATION_FORM.fullmatch(value) is None:
        raise ValueError(f"{subject}: {what} is {value!r}, not a SurrealQL duration (such as '15m', '12h', '30d' or NONE).")
    return value


def _signup_fields(value: object, *, subject: str) -> dict[str, str]:
    """SIGNUP's columns and their expressions: column names checked as ``utils.validate_field_name()`` checks them,
    each value one expression."""
    if not isinstance(value, Mapping):
        raise TypeError(
            f"{subject}: signup_fields= must be a mapping of column to SurrealQL expression, got {type(value).__name__}."
        )
    if not value:
        raise ValueError(f"{subject}: signup_fields= sets no column; SIGNUP creates the record with them.")
    fields: dict[str, str] = {}
    for column, expression in value.items():
        if not isinstance(column, str):
            raise TypeError(f"{subject}: signup_fields= holds a {type(column).__name__} column, not a str.")
        with _refusal(subject):
            validate_field_name(column, context="signup_fields column")
        what = f"the expression of signup_fields[{column!r}]"
        if expression is None:
            raise TypeError(f"{subject}: {what} must be a str, got NoneType.")
        fields[column] = cast(str, _expression(expression, subject=subject, what=what))
    return fields


def _jwt_refused(raw: str) -> bool:
    """Whether the stored definition *raw* holds a ``WITH JWT`` clause ``DefineAccess`` cannot write: ``AUTHENTICATE``,
    a ``URL``, an algorithm other than ``HS512``."""
    try:
        keywords = top_level_keywords(raw, _JWT_KEYWORDS)
    except DefineParseError:
        return True
    for position, keyword in keywords:
        if keyword != "ALGORITHM":
            return True
        following = raw[position + len(keyword) :].split(None, 1)
        if not following or following[0].upper() != "HS512":
            return True
    return False


@dataclass(frozen=True)
class DefineAccess(Operation):
    """Define a record access: ``schema.access_statement()`` — the statement ``define_access()`` writes, the same on
    every line —, the plain form unless ``overwrite`` or ``previous``.

    ``signup_fields`` maps a column to its SurrealQL expression; a primary-key user model's record id is the item
    ``"id": "$<key>"`` (#338), written first whatever the mapping's order. ``signin_where`` is SIGNIN's ``WHERE``
    expression; ``with_refresh=True`` (SurrealDB 3.x) adds ``WITH REFRESH`` and the grant's duration (``30d`` unless
    ``duration_grant``). A redefined access gets a new signing key from SurrealDB: every token issued before is
    refused (M20).

    Undone by ``REMOVE ACCESS IF EXISTS``, or, with ``previous=`` (a ``DefineAccess`` of the same name), by its
    statement with ``OVERWRITE``; ``overwrite=True`` without ``previous`` is not reversible.
    """

    name: str
    table: str
    signup_fields: Mapping[str, str] = field(repr=False)
    signin_where: str = field(repr=False)
    duration_token: str = "15m"
    duration_session: str = "12h"
    comment: str | None = None
    _: KW_ONLY
    with_refresh: bool = False
    duration_grant: str | None = None
    overwrite: bool = False
    previous: "DefineAccess | None" = None

    def __post_init__(self) -> None:
        name = _definition_name(self.name, subject="DefineAccess", what="Access")
        table = _definition_name(self.table, subject="DefineAccess", what="Table")
        subject = f"DefineAccess {name!r} for {table!r}"
        fields = _signup_fields(self.signup_fields, subject=subject)
        signin_where: object = self.signin_where
        if signin_where is None:
            raise TypeError(f"{subject}: signin_where= must be a str, got NoneType.")
        where = _expression(signin_where, subject=subject, what="signin_where=")
        _duration(self.duration_token, subject=subject, what="duration_token=")
        _duration(self.duration_session, subject=subject, what="duration_session=")
        if self.duration_grant is not None:
            _duration(self.duration_grant, subject=subject, what="duration_grant=")
        _comment(self.comment, subject=subject, what="comment=")
        _flag(self.with_refresh, subject=subject, what="with_refresh=")
        _flag(self.overwrite, subject=subject, what="overwrite=")
        _check_previous(self.previous, subject=subject, kind=DefineAccess, what="access", name=name)
        object.__setattr__(self, "signup_fields", fields)
        object.__setattr__(self, "signin_where", where)

    def _statement(self, clause: Literal["OVERWRITE", ""]) -> str:
        parts = AccessParts(
            name=self.name,
            table=self.table,
            signup_fields=self.signup_fields,
            signin_where=self.signin_where,
            duration_token=self.duration_token,
            duration_session=self.duration_session,
            duration_grant=self.duration_grant,
            with_refresh=self.with_refresh,
        )
        return access_statement(parts, clause=clause, comment=self.comment)

    @property
    def reversible(self) -> bool:
        return self.previous is not None or not self.overwrite

    def forwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        return self._statement("OVERWRITE" if self.overwrite or self.previous is not None else "")

    def backwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        if self.previous is not None:
            return self.previous._statement("OVERWRITE")
        if self.overwrite:
            return ""
        return render.removal("access", self.name, if_exists=True)

    def describe(self) -> str:
        return f"Define access {self.name} for {self.table}"

    def state_forwards(self, state: SchemaState) -> SchemaState:
        return _placed(state, self.forwards())

    def state_backwards(self, state: SchemaState) -> SchemaState:
        if self.previous is not None:
            return _placed(state, self.backwards())
        if self.overwrite:
            raise _not_reversible(self)
        return _state.take_out(state, "access", self.name)

    @classmethod
    def from_access_state(cls, state: AccessState) -> "DefineAccess | None":
        """The operation that defines *state*, or ``None`` for an access it cannot write: not ``TYPE RECORD``; a
        SIGNUP or SIGNIN of another shape than ``CREATE <table> SET …`` / ``SELECT … FROM <table> WHERE …`` (``table``
        or ``signin_where`` empty); a duration the statement does not name; an ``AUTHENTICATE`` clause, a ``WITH JWT
        … URL`` or an algorithm other than ``HS512`` in ``raw`` (``DefineAccess`` writes no key; SurrealDB generates an
        ``HS512`` one, M19); or arguments ``DefineAccess`` refuses."""
        if state.access_type != "RECORD" or not state.table or not state.signin_where:
            return None
        if state.duration_token is None or state.duration_session is None or _jwt_refused(state.raw):
            return None
        try:
            return cls(
                state.name,
                state.table,
                dict(state.signup_fields),
                state.signin_where,
                state.duration_token,
                state.duration_session,
                state.comment,
                with_refresh=state.with_refresh,
                duration_grant=state.duration_grant if state.with_refresh else None,
            )
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True)
class RemoveAccess(Operation):
    """Remove a database access: ``REMOVE ACCESS … ON DATABASE``. Reversible with ``previous=`` (its
    ``DefineAccess``) — SurrealDB then generates a new signing key (M20)."""

    name: str
    _: KW_ONLY
    previous: DefineAccess | None = None

    def __post_init__(self) -> None:
        name = _removal_name(self.name, subject="RemoveAccess", what="name=")
        _check_previous(self.previous, subject=f"RemoveAccess {name!r}", kind=DefineAccess, what="access", name=name)

    @property
    def reversible(self) -> bool:
        return self.previous is not None

    @property
    def destructive(self) -> bool:
        return True

    def forwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        return render.removal("access", self.name)

    def backwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        return "" if self.previous is None else self.previous.forwards()

    def describe(self) -> str:
        return f"Remove access {_shown(self.name)}"

    def state_forwards(self, state: SchemaState) -> SchemaState:
        return _state.take_out(state, "access", self.name)

    def state_backwards(self, state: SchemaState) -> SchemaState:
        if self.previous is None:
            raise _not_reversible(self)
        return _placed(state, self.backwards())


# --- functions -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DefineFunction(Operation):
    """Define a stored function (lite, #276): ``function_ddl(...)``, the plain form unless ``overwrite`` or
    ``previous`` — ``name`` as ``call_function()`` takes it (kept ``fn::…``), ``body`` always wrapped in a block,
    ``parameters`` mapping each name (no ``$``) to its kind in declaration order, ``returns`` a kind, ``permissions``
    ``FULL``, ``NONE`` or ``WHERE <condition>``.

    Undone by ``REMOVE FUNCTION IF EXISTS``, or, with ``previous=`` (a ``DefineFunction`` of the same name), by its
    statement with ``OVERWRITE``; ``overwrite=True`` without ``previous`` is not reversible.
    """

    name: str
    body: str = field(repr=False)
    _: KW_ONLY
    parameters: Mapping[str, str] | None = None
    returns: str | None = None
    permissions: str | None = field(default=None, repr=False)
    comment: str | None = None
    overwrite: bool = False
    previous: "DefineFunction | None" = None

    def __post_init__(self) -> None:
        name = _function_name(self.name, subject="DefineFunction")
        object.__setattr__(self, "name", name)
        subject = f"DefineFunction {name!r}"
        with _refusal(subject):
            self._statement(overwrite=True)
        _flag(self.overwrite, subject=subject, what="overwrite=")
        _check_previous(self.previous, subject=subject, kind=DefineFunction, what="function", name=name)
        object.__setattr__(self, "body", self.body.strip())
        if self.parameters is not None:
            object.__setattr__(self, "parameters", dict(self.parameters))

    def _statement(self, *, overwrite: bool) -> str:
        return function_ddl(
            self.name,
            self.body,
            parameters=self.parameters,
            returns=self.returns,
            permissions=self.permissions,
            comment=self.comment,
            overwrite=overwrite,
        )

    @property
    def reversible(self) -> bool:
        return self.previous is not None or not self.overwrite

    def forwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        statement = self._statement(overwrite=True)
        return statement if self.overwrite or self.previous is not None else render.plain(statement)

    def backwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        if self.previous is not None:
            return self.previous._statement(overwrite=True)
        if self.overwrite:
            return ""
        return render.removal("function", self.name, if_exists=True)

    def describe(self) -> str:
        return f"Define function {self.name}"

    def state_forwards(self, state: SchemaState) -> SchemaState:
        return _placed(state, self.forwards())

    def state_backwards(self, state: SchemaState) -> SchemaState:
        if self.previous is not None:
            return _placed(state, self.backwards())
        if self.overwrite:
            raise _not_reversible(self)
        return _state.take_out(state, "function", self.name)


@dataclass(frozen=True)
class RemoveFunction(Operation):
    """Remove a stored function (lite, #276): ``REMOVE FUNCTION fn::…``. Reversible with ``previous=`` (its
    ``DefineFunction``)."""

    name: str
    _: KW_ONLY
    previous: DefineFunction | None = None

    def __post_init__(self) -> None:
        name = _function_name(self.name, subject="RemoveFunction")
        object.__setattr__(self, "name", name)
        _check_previous(self.previous, subject=f"RemoveFunction {name!r}", kind=DefineFunction, what="function", name=name)

    @property
    def reversible(self) -> bool:
        return self.previous is not None

    @property
    def destructive(self) -> bool:
        return True

    def forwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        return render.removal("function", self.name)

    def backwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        return "" if self.previous is None else self.previous.forwards()

    def describe(self) -> str:
        return f"Remove function {self.name}"

    def state_forwards(self, state: SchemaState) -> SchemaState:
        return _state.take_out(state, "function", self.name)

    def state_backwards(self, state: SchemaState) -> SchemaState:
        if self.previous is None:
            raise _not_reversible(self)
        return _placed(state, self.backwards())


# --- data and raw SQL ------------------------------------------------------------------------------------------------


def _sql(value: object, *, owner: str, what: str) -> None:
    """*value*, a ``RawSQL``'s or a ``DataMigration``'s SQL: one or more statements ``render.check_statements()``
    accepts (no ``BEGIN``, ``COMMIT``, ``CANCEL`` or ``USE``)."""
    if render.check_statements(cast(str, value), what=owner) == 0:
        raise ValueError(f"{owner}: {what} holds no statement.")


def _async_function(value: object, *, what: str) -> None:
    """A data migration's function: ``None``, or an ``async def`` callable taking no argument."""
    if value is None:
        return
    message = f"DataMigration: {what} must be an async function taking no argument (async def …())."
    if not inspect.iscoroutinefunction(value):
        raise TypeError(message)
    try:
        parameters = inspect.signature(value).parameters.values()
    except (TypeError, ValueError):  # pragma: no cover - every async def has a signature; a defence only
        raise TypeError(message) from None
    required = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    if any(parameter.kind in required and parameter.default is inspect.Parameter.empty for parameter in parameters):
        raise TypeError(message)


@dataclass(frozen=True)
class DataMigration(Operation):
    """Transform records (the full ORM's): ``forwards_sql`` and/or ``forwards_func`` (an ``async def`` taking no
    argument), run by ``upgrade()`` or ``migrate(schema_only=False)`` after the schema; ``backwards_sql`` and
    ``backwards_func`` run by ``rollback()``. Reversible when it has either backwards. No schema effect: its
    ``state_forwards()`` and ``state_backwards()`` return the state as it is."""

    forwards_sql: str | None = field(default=None, repr=False)
    backwards_sql: str | None = field(default=None, repr=False)
    forwards_func: AsyncFunction | None = None
    backwards_func: AsyncFunction | None = None
    description: str = "Data migration"

    def __post_init__(self) -> None:
        if not self.forwards_sql and not self.forwards_func:
            raise ValueError("DataMigration requires either forwards_sql or forwards_func")
        if self.forwards_sql is not None:
            _sql(self.forwards_sql, owner="DataMigration", what="forwards_sql=")
        if self.backwards_sql is not None and self.backwards_sql != "":
            _sql(self.backwards_sql, owner="DataMigration", what="backwards_sql=")
        _async_function(self.forwards_func, what="forwards_func=")
        _async_function(self.backwards_func, what="backwards_func=")
        _description(self.description, subject="DataMigration")

    @property
    def reversible(self) -> bool:
        return bool(self.backwards_sql or self.backwards_func)

    @property
    def has_func(self) -> bool:
        """Whether the forwards runs an async function (the full ORM's)."""
        return self.forwards_func is not None

    def forwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        return self.forwards_sql or ""

    def backwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        return self.backwards_sql or ""

    def describe(self) -> str:
        return self.description

    def state_forwards(self, state: SchemaState) -> SchemaState:
        return state

    def state_backwards(self, state: SchemaState) -> SchemaState:
        return state


@dataclass(frozen=True)
class RawSQL(Operation):
    """Run SurrealQL the operations do not cover (the full ORM's): ``sql`` forwards, ``reverse_sql`` backwards — one
    or more statements, never ``BEGIN``, ``COMMIT``, ``CANCEL`` or ``USE`` (each migration runs in a transaction of its
    own).

    ``state_operations`` (lite, Django's) are operations whose ``state_forwards()`` is applied instead of the SQL's,
    which the ORM cannot read — a definition a ``RawSQL`` makes is known to the replays only through them; they are
    never sent. Reversible when ``reverse_sql`` is given and every ``state_operations`` item is (D39).
    """

    sql: str = field(repr=False)
    reverse_sql: str = field(default="", repr=False)
    description: str = "Raw SQL"
    _: KW_ONLY
    state_operations: Sequence[Operation] = ()

    def __post_init__(self) -> None:
        _sql(self.sql, owner="RawSQL", what="sql=")
        reverse_sql: object = self.reverse_sql
        if not isinstance(reverse_sql, str):
            raise TypeError(f"RawSQL: reverse_sql= must be a str, got {type(reverse_sql).__name__}.")
        if reverse_sql:
            _sql(reverse_sql, owner="RawSQL", what="reverse_sql=")
        _description(self.description, subject="RawSQL")
        operations: object = self.state_operations
        if not isinstance(operations, list | tuple):
            raise TypeError(
                f"RawSQL: state_operations= must be a list or tuple of operations, not {type(operations).__name__}."
            )
        for position, operation in enumerate(operations, 1):
            if not isinstance(operation, Operation):
                raise TypeError(
                    f"RawSQL: state_operations= item {position} is not an Operation (got {type(operation).__name__})."
                )
        object.__setattr__(self, "state_operations", tuple(operations))

    @property
    def reversible(self) -> bool:
        return bool(self.reverse_sql) and all(operation.reversible for operation in self.state_operations)

    def forwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        return self.sql

    def backwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        checked_line(line)
        return self.reverse_sql if self.reversible else ""

    def describe(self) -> str:
        return self.description

    def state_forwards(self, state: SchemaState) -> SchemaState:
        for operation in self.state_operations:
            state = operation.state_forwards(state)
        return state

    def state_backwards(self, state: SchemaState) -> SchemaState:
        if not self.reversible:
            raise _not_reversible(self)
        for operation in reversed(self.state_operations):
            state = operation.state_backwards(state)
        return state


def split_destructive(operations: Sequence[Operation]) -> tuple[list[Operation], list[Operation]]:
    """Separate the removals from the rest: ``(keep, destructive)`` (the full ORM's function), partitioned on
    ``Operation.destructive`` (D24) — ``DropTable``, ``DropField``, ``DropIndex``, ``RemoveAnalyzer``,
    ``RemoveEvent``, ``RemoveAccess``, ``RemoveFunction``, whether or not a ``previous=`` makes them reversible. A
    definition the database holds and no model declares is not necessarily obsolete (an event added by hand, an
    analyzer of another module), so a caller opts into its removal. Order is kept in each part."""
    keep = [operation for operation in operations if not operation.destructive]
    destructive = [operation for operation in operations if operation.destructive]
    return keep, destructive
