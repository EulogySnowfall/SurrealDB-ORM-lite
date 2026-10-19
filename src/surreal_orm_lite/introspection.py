"""Read the schema of a SurrealDB database (v0.30.0) — the full SurrealDB-ORM's ``DatabaseIntrospector``.

::

    schema = await DatabaseIntrospector().introspect()                    # the current connection
    schema = await DatabaseIntrospector("analytics").introspect()         # a named connection
    async with SurrealDBConnectionManager.using("archive"):
        schema = await DatabaseIntrospector(tables=["order"]).introspect()  # "archive", one table

    schema.tables["person"].fields["age"].field_type    # 'int' — read as option<int> (2.x) or none | int (3.x)
    schema.issues                                       # () when everything was read and parsed

    source = ModelCodeGenerator().generate(schema)      # the Python source of models for those tables

**Read-only.** ``introspect()`` sends ``INFO FOR DB;`` and then one ``INFO FOR TABLE `<name>`;`` per table (in
codepoint order of the names, each name escaped as an identifier) on the connection's client, and nothing else: no
record is read, nothing is defined or written. It never goes through a transaction — inside an open
``transaction()`` block it reads the committed schema, without the block's own ``DEFINE`` statements.

**What is read** (spec §5.5). ``INFO FOR DB``: ``tables`` (each then read with ``INFO FOR TABLE``: its fields,
indexes and events), ``analyzers``, ``accesses``, ``functions`` and ``params`` become the typed states of
:mod:`surreal_orm_lite.schema_state`; ``users`` is **never read** — not parsed, stored or logged (before SurrealDB
3.2.2 ``INFO`` shows user password hashes); every other section (``apis``, ``configs``, ``models``, 3.x ``buckets``,
``modules``, ``sequences``, a future one) is kept as returned in ``SchemaState.other``. Of ``INFO FOR TABLE``, the
``lives`` (running live queries) and ``tables`` (views fed by the table) sections are ignored and any other section
goes to ``TableState.other``. Kinds, nullability, field paths, flags and names are normalised to one form on
SurrealDB 2.x, 3.x and the embedded engine; expression texts are kept as each line rendered them.

**Nothing is guessed.** A definition the parser cannot read exactly, a section of the wrong shape, an ``INFO FOR
TABLE`` that fails: each becomes a :class:`SchemaIssue` in ``SchemaState.issues`` (in the order found) and one
WARNING on this module's logger — the reader never raises for them. An issue's message is a fixed text with names
only, never a definition's text, which may hold a credential (``DEFINE PARAM $api_key VALUE '…'``); the statement is
kept in ``SchemaIssue.statement``, out of its ``repr``.

**Errors.** ``INFO FOR DB`` refused (a record user, an anonymous session) raises ``SurrealDbAuthenticationError``;
failing otherwise, or answering something other than an object, ``SurrealDbError`` — ORM errors (a connection that
cannot be opened …) propagate unchanged. A ``tables=`` name the database does not define raises
``SurrealDbNotFoundError`` before any ``INFO FOR TABLE``. Arguments are checked when the introspector is built.
"""

import dataclasses
import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol, TypeVar

from .connection_manager import SurrealDBConnectionManager
from .define_parser import (
    DefineParseError,
    parse_define_access,
    parse_define_analyzer,
    parse_define_event,
    parse_define_field,
    parse_define_function,
    parse_define_index,
    parse_define_param,
    parse_define_table,
)
from .exceptions import SurrealDbAuthenticationError, SurrealDbError, SurrealDbNotFoundError, SurrealORMError
from .model_generator import ModelCodeGenerator
from .routing import check_name
from .schema_state import (
    AccessState,
    AnalyzerState,
    EventState,
    FieldState,
    FunctionState,
    IndexState,
    ParamState,
    SchemaIssue,
    SchemaState,
    TableState,
)
from .surql_literal import _identifier

__all__ = [
    "AccessState",
    "AnalyzerState",
    "DatabaseIntrospector",
    "EventState",
    "FieldState",
    "FunctionState",
    "IndexState",
    "ModelCodeGenerator",
    "ParamState",
    "SchemaIssue",
    "SchemaState",
    "TableState",
]

logger = logging.getLogger(__name__)

#: E9 (spec §5.6): a client object where the name of a registered connection is expected.
_CLIENT_OBJECT = (
    "connection= takes the name of a registered connection, not a client object: pass the name it was added with "
    "(add_connection()), or call DatabaseIntrospector() inside SurrealDBConnectionManager.using(name)."
)

#: The ``INFO FOR DB`` sections that are not kept in ``SchemaState.other``: never read, or read on their own.
_NOT_OTHER = frozenset({"users", "tables"})
#: The ``INFO FOR TABLE`` sections that are ignored: running live queries, views fed by the table.
_IGNORED_TABLE_SECTIONS = frozenset({"lives", "tables"})

#: I3 and I5 (spec §5.7).
_TYPE_AS_WRITTEN = "its TYPE clause was not understood and is kept as written"
_SAME_NAME = "two definitions have the same name"


class _Named(Protocol):
    @property
    def name(self) -> str: ...


_S = TypeVar("_S", bound=_Named)


def _connection_name(connection: object) -> str | None:
    """*connection* when it is ``None`` or a usable name: a client object → E9 (first), another type or ``""`` → R3."""
    if connection is None:
        return None
    if not isinstance(connection, str) and hasattr(connection, "query"):
        raise TypeError(_CLIENT_OBJECT)
    return check_name(connection)


def _table_names(tables: Iterable[str] | None) -> tuple[str, ...] | None:
    """*tables* materialised to a ``tuple``: a ``str`` → E5, an item that is not a ``str`` → E6."""
    if tables is None:
        return None
    if isinstance(tables, str):
        raise TypeError("tables= takes an iterable of table names, not a str: write tables=['person'].")
    names = tuple(tables)
    for item in names:
        if not isinstance(item, str):  # checked at run time: the annotation does not reach the caller's data
            raise TypeError(f"tables= takes table names (str), got {type(item).__name__}.")
    return names


def _definitions(value: object) -> dict[str, str] | None:
    """A section's value when it is a mapping of names to definition texts, else ``None``."""
    if not isinstance(value, Mapping):
        return None
    if not all(isinstance(key, str) and isinstance(text, str) for key, text in value.items()):
        return None
    return dict(value)


async def _info_for_db(client: Any, name: str) -> dict[str, Any]:
    """``INFO FOR DB`` on *client*: E1 when the session may not run it, E2 when it fails otherwise, E3 for an answer
    that is not an object; an ORM error propagates unchanged."""
    try:
        info = await client.query("INFO FOR DB;", {})
    except SurrealORMError:
        raise
    except Exception as exc:
        if "not enough permissions" in str(exc).lower():
            raise SurrealDbAuthenticationError(
                f"Cannot read the schema of connection {name!r}: this session may not run INFO FOR DB. Schema "
                "introspection needs a system user (root, namespace or database user, any role); a record user or "
                f"an anonymous session cannot read the schema. Server said: {exc}"
            ) from exc
        raise SurrealDbError(f"Cannot read the schema of connection {name!r}: INFO FOR DB failed: {exc}") from exc
    if not isinstance(info, dict):
        raise SurrealDbError(
            f"Cannot read the schema of connection {name!r}: INFO FOR DB returned {type(info).__name__}, not an object."
        )
    return info


class _Reader:
    """One ``introspect()`` run: the client, the connection's name and the issues found, in order."""

    def __init__(self, client: Any, name: str) -> None:
        self.client = client
        self.name = name
        self.issues: list[SchemaIssue] = []

    def issue(self, kind: str, name: str, table: str | None, message: str, statement: str | None = None) -> None:
        """Record an issue and log it once (spec §5.7): the message is a fixed text, names are formatted with ``%r``."""
        self.issues.append(SchemaIssue(kind=kind, name=name, table=table, message=message, statement=statement))
        logger.warning("Schema introspection: %s (%s %r)", message, kind, name)

    def section(self, section: str, value: object, table: str | None) -> dict[str, str] | None:
        """*value* as a mapping of names to definitions, or ``None`` after issue I4."""
        definitions = _definitions(value)
        if definitions is None:
            self.issue("section", section, table, f"section {section!r} is not a mapping of names to definitions")
        return definitions

    def parse(self, kind: str, definitions: Mapping[str, str], parse: Callable[[str], _S], table: str | None) -> dict[str, _S]:
        """Each definition parsed, keyed by its state's name; one that cannot be parsed is left out (issue I1), and so
        is a second one with the same name (I5's text: the first is kept, nothing is dropped unreported)."""
        states: dict[str, _S] = {}
        for key in sorted(definitions):
            statement = definitions[key]
            try:
                state = parse(statement)
            except DefineParseError as error:
                self.issue(kind, key, table, f"could not parse its definition: {error}", statement)
                continue
            if state.name in states:
                self.issue(kind, key, table, _SAME_NAME, statement)
                continue
            states[state.name] = state
        return states

    def fields(self, definitions: Mapping[str, str], table: str) -> dict[str, FieldState]:
        """The fields, keyed by their canonical name: I1 (left out), I3 (kept, its TYPE as written), I5 (a second
        field with the same name: the first one is kept)."""
        fields: dict[str, FieldState] = {}
        for key in sorted(definitions):
            statement = definitions[key]
            try:
                parsed = parse_define_field(statement)
            except DefineParseError as error:
                self.issue("field", key, table, f"could not parse its definition: {error}", statement)
                continue
            if parsed.state.name in fields:
                self.issue("field", key, table, _SAME_NAME, statement)
                continue
            if parsed.type_as_written:
                self.issue("field", key, table, _TYPE_AS_WRITTEN, statement)
            fields[parsed.state.name] = parsed.state
        return fields

    async def table(self, name: str, statement: str) -> TableState | None:
        """The table *name* with its fields, indexes and events; ``None`` when its definition cannot be parsed (I1).
        When ``INFO FOR TABLE`` fails, the table is kept with empty sections (I2)."""
        try:
            state = parse_define_table(statement)
        except DefineParseError as error:
            self.issue("table", name, name, f"could not parse its definition: {error}", statement)
            return None
        try:
            info = await self.client.query(f"INFO FOR TABLE {_identifier(name)};", {})
        except Exception as exc:
            self.issue("table", name, name, f"INFO FOR TABLE failed: {exc}")
            return state
        if not isinstance(info, dict):
            self.issue("table", name, name, f"INFO FOR TABLE failed: it returned {type(info).__name__}, not an object.")
            return state
        fields: dict[str, FieldState] = {}
        indexes: dict[str, IndexState] = {}
        events: dict[str, EventState] = {}
        other: dict[str, dict[str, str]] = {}
        for section in sorted(info):
            if section in _IGNORED_TABLE_SECTIONS:
                continue
            definitions = self.section(section, info[section], name)
            if definitions is None:
                continue
            if section == "fields":
                fields = self.fields(definitions, name)
            elif section == "indexes":
                indexes = self.parse("index", definitions, parse_define_index, name)
            elif section == "events":
                events = self.parse("event", definitions, parse_define_event, name)
            else:
                other[section] = definitions
        return dataclasses.replace(state, fields=fields, indexes=indexes, events=events, other=other)

    async def read(self, info: Mapping[str, Any], wanted_names: tuple[str, ...] | None) -> SchemaState:
        """The schema from the ``INFO FOR DB`` answer *info*, reading each wanted table (spec §6.2)."""
        if "tables" in info:
            tables_section = self.section("tables", info["tables"], None) or {}
        else:
            self.issue("section", "tables", None, "section 'tables' is not a mapping of names to definitions")
            tables_section = {}
        if wanted_names is None:
            wanted = sorted(tables_section)
        else:
            missing = sorted(set(wanted_names) - tables_section.keys())
            if missing:
                raise SurrealDbNotFoundError(
                    f"Not defined in the database of connection {self.name!r}: {', '.join(map(repr, missing))}."
                )
            wanted = sorted(set(wanted_names))

        analyzers: dict[str, AnalyzerState] = {}
        accesses: dict[str, AccessState] = {}
        functions: dict[str, FunctionState] = {}
        params: dict[str, ParamState] = {}
        other: dict[str, dict[str, str]] = {}
        for section in sorted(info):
            if section in _NOT_OTHER:
                continue
            definitions = self.section(section, info[section], None)
            if definitions is None:
                continue
            if section == "analyzers":
                analyzers = self.parse("analyzer", definitions, parse_define_analyzer, None)
            elif section == "accesses":
                accesses = self.parse("access", definitions, parse_define_access, None)
            elif section == "functions":
                functions = self.parse("function", definitions, parse_define_function, None)
            elif section == "params":
                params = self.parse("param", definitions, parse_define_param, None)
            else:
                other[section] = definitions

        tables: dict[str, TableState] = {}
        for name in wanted:
            state = await self.table(name, tables_section[name])
            if state is not None:
                tables[name] = state
        return SchemaState(
            tables=tables,
            connection=self.name,
            analyzers=analyzers,
            accesses=accesses,
            functions=functions,
            params=params,
            other=other,
            issues=tuple(self.issues),
        )


class DatabaseIntrospector:
    """Read the schema of a database — the full SurrealDB-ORM's ``DatabaseIntrospector`` (v0.30.0).

    ::

        schema = await DatabaseIntrospector().introspect()
        schema = await DatabaseIntrospector("analytics", tables=["page_view"]).introspect()

    :param connection: the **name** of a registered connection (``add_connection()``), or ``None`` for the current
        one — the innermost ``using()`` block's, else ``"default"`` — resolved when :meth:`introspect` runs, so a
        ``using()`` block around the ``await`` applies. A client object is refused: it does not carry the name the
        ORM routes by.
    :param tables: the tables to read (``INFO FOR TABLE`` is sent for those only); ``None`` reads every table, an
        empty iterable none (the database-level sections are read all the same). Read once, here.
    :raises TypeError: *connection* is a client object (an object with a ``query`` attribute) or not a ``str``;
        *tables* is a ``str``, or one of its items is not a ``str``.
    :raises ValueError: *connection* is empty.
    """

    __slots__ = ("_connection", "_tables")

    def __init__(self, connection: str | None = None, *, tables: Iterable[str] | None = None) -> None:
        self._connection = _connection_name(connection)
        self._tables = _table_names(tables)

    async def introspect(self) -> SchemaState:
        """Read the schema: ``INFO FOR DB;``, then ``INFO FOR TABLE`` for each wanted table, outside any transaction.

        Reusable: it holds no state between calls. What cannot be read or parsed is reported in
        ``SchemaState.issues`` (and logged), never raised.

        :raises ValueError: the connection is not registered, or not configured.
        :raises SurrealDbAuthenticationError: the session may not run ``INFO FOR DB`` (a record user, an anonymous
            session).
        :raises SurrealDbNotFoundError: a ``tables=`` name is not a table of the database.
        :raises SurrealDbError: ``INFO FOR DB`` failed otherwise, or did not answer an object.
        :raises SurrealDbConnectionError: what ``get_client()`` raises, unchanged.
        """
        manager = SurrealDBConnectionManager._manager_for(self._connection)
        name = manager._connection_name or "default"
        client = await manager.get_client()
        info = await _info_for_db(client, name)
        return await _Reader(client, name).read(info, self._tables)
