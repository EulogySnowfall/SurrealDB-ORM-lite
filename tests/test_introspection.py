"""Schema introspection (v0.30.0): ``DatabaseIntrospector`` reads a database's schema with ``INFO FOR DB`` and one
``INFO FOR TABLE`` per table (spec §5.1, §5.5–§5.7, §6.2, §7).

Unit tests first, with a fake client installed on the resolved connection class (no server): the exports, the
statements sent (AC-3), the sections read, the errors E1–E7 and E9, the issues I1–I5 with the secret check (AC-10) and
the connection resolution (AC-4). Then the classes suffixed ``E2E``, on the line under test (``SURREALDB_PORT``) and on
``mem://``: the portable fixture of spec §9.3 (AC-5), connections, HTTP and transactions (AC-4), a spy on the real
client (AC-3), the one-line-only items through ``_require_ddl`` (AC-8, spec §9.4), the record-user refusal and the
removed database (AC-10). E2E tables are prefixed ``isp_`` (plus the fixture's ``select``) and live in the database
``isp_db`` of namespace ``ns``, read through the named connection ``"isp"``; a test reading the default connection
passes ``tables=``.
"""

import contextlib
import inspect
import logging
import os
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any
from unittest import mock

import pytest
from surrealdb import AsyncSurreal

import surreal_orm_lite
from surreal_orm_lite import (
    AccessState,
    AnalyzerState,
    DatabaseIntrospector,
    EventState,
    FieldState,
    FunctionState,
    IndexState,
    ParamState,
    SchemaIssue,
    SchemaState,
    SurrealDbAuthenticationError,
    SurrealDbConnectionError,
    SurrealDBConnectionManager,
    SurrealDbError,
    SurrealDbNotFoundError,
    TableState,
)
from surreal_orm_lite import introspection as introspection_module
from surreal_orm_lite.define_parser import (
    _R_AMBIGUOUS,
    _R_BRACKETS,
    _R_HEADER,
    _R_IDENT,
    _R_INDEX,
    _R_NO_READING,
    _R_SIGNATURE,
    _R_STRING,
)
from tests.conftest import orm_client

#: The names spec §5.8 exports that this module provides so far (``generate_models_from_db`` joins them later).
_EXPORTS = (
    "DatabaseIntrospector",
    "ModelCodeGenerator",
    "SchemaState",
    "TableState",
    "FieldState",
    "IndexState",
    "EventState",
    "AccessState",
    "AnalyzerState",
    "FunctionState",
    "ParamState",
    "SchemaIssue",
)

#: E9 (spec §5.6), exactly.
_E9 = (
    "connection= takes the name of a registered connection, not a client object: pass the name it was added with "
    "(add_connection()), or call DatabaseIntrospector() inside SurrealDBConnectionManager.using(name)."
)

#: The logger the issues are reported on (spec §5.7).
_LOGGER = "surreal_orm_lite.introspection"


def _e1(name: str, exc: object) -> str:
    """E1 (spec §5.6) for connection *name* and the server's error *exc*."""
    return (
        f"Cannot read the schema of connection {name!r}: this session may not run INFO FOR DB. Schema introspection "
        "needs a system user (root, namespace or database user, any role); a record user or an anonymous session "
        f"cannot read the schema. Server said: {exc}"
    )


def _parse_failure(reason: str) -> str:
    """I1's message (spec §5.7) for a fixed parse *reason*."""
    return f"could not parse its definition: {reason}"


# --- the fake client -------------------------------------------------------------------------------------------------


def _info_table(name: str) -> str:
    """The ``INFO FOR TABLE`` statement the reader sends for *name*: backquoted, ``\\`` then `````` escaped."""
    escaped = name.replace("\\", "\\\\").replace("`", "\\`")
    return f"INFO FOR TABLE `{escaped}`;"


def _db(**sections: object) -> dict[str, object]:
    """An ``INFO FOR DB`` answer: the 2.x sections, empty, then *sections*."""
    empty: dict[str, object] = {
        key: {} for key in ("accesses", "analyzers", "apis", "configs", "functions", "models", "params", "tables", "users")
    }
    return {**empty, **sections}


def _table(**sections: object) -> dict[str, object]:
    """An ``INFO FOR TABLE`` answer: its five sections, empty, then *sections*."""
    empty: dict[str, object] = {key: {} for key in ("events", "fields", "indexes", "lives", "tables")}
    return {**empty, **sections}


class _FakeClient:
    """An SDK-like client: answers ``INFO FOR DB;`` and each ``INFO FOR TABLE`` from dicts, records every call.

    *tables* maps a table name to its ``INFO FOR TABLE`` answer (an empty one by default); *errors* maps a statement
    to the exception it raises.
    """

    def __init__(
        self,
        db: object,
        tables: Mapping[str, object] | None = None,
        errors: Mapping[str, BaseException] | None = None,
    ) -> None:
        self.db = db
        self.tables = {_info_table(name): answer for name, answer in (tables or {}).items()}
        self.errors = dict(errors or {})
        self.calls: list[tuple[str, object, tuple[object, ...], dict[str, object]]] = []

    async def query(self, sql: str, vars: object = None, *args: object, **kwargs: object) -> object:
        self.calls.append((sql, vars, args, kwargs))
        if sql in self.errors:
            raise self.errors[sql]
        if sql == "INFO FOR DB;":
            return self.db
        return self.tables.get(sql, _table())

    @property
    def statements(self) -> list[str]:
        return [call[0] for call in self.calls]


def _install(monkeypatch: pytest.MonkeyPatch, client: object, name: str = "default") -> list[tuple[object, ...]]:
    """Make the connection class of *name* hand out *client*; return the ``get_client`` calls it receives."""
    connection = SurrealDBConnectionManager._manager_for(name)
    requests: list[tuple[object, ...]] = []

    async def get_client(*args: object, **kwargs: object) -> object:
        requests.append((args, kwargs))
        return client

    monkeypatch.setattr(connection, "get_client", get_client)
    return requests


def _named(name: str) -> None:
    """Register *name* on a server URL nothing listens on: the fake client stands in for it, nothing is opened."""
    SurrealDBConnectionManager.add_connection(
        name, url="ws://localhost:9/rpc", user="root", password="root", namespace="ns", database="isp_db"
    )


def _issue_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == _LOGGER]


_TABLE_A = "DEFINE TABLE a TYPE ANY SCHEMALESS PERMISSIONS NONE"
_TABLE_B = "DEFINE TABLE b TYPE ANY SCHEMALESS PERMISSIONS NONE"


# === Unit tests (no server) ===========================================================================================


class TestExports:
    """AC-1 (part): the reader and the state classes import from the package and from ``introspection``."""

    def test_names_are_exported(self) -> None:
        for name in _EXPORTS:
            assert name in surreal_orm_lite.__all__, name
            assert name in introspection_module.__all__, name
            assert getattr(surreal_orm_lite, name) is getattr(introspection_module, name)

    def test_the_state_classes_are_the_schema_state_ones(self) -> None:
        from surreal_orm_lite import schema_state

        for cls in (SchemaState, TableState, FieldState, IndexState, EventState, AccessState, AnalyzerState):
            assert cls is getattr(schema_state, cls.__name__)
        for cls in (FunctionState, ParamState, SchemaIssue):
            assert cls is getattr(schema_state, cls.__name__)

    def test_signature(self) -> None:
        assert inspect.iscoroutinefunction(DatabaseIntrospector.introspect)
        parameters = inspect.signature(DatabaseIntrospector).parameters
        assert [(p.name, p.kind, p.default) for p in parameters.values()] == [
            ("connection", inspect.Parameter.POSITIONAL_OR_KEYWORD, None),
            ("tables", inspect.Parameter.KEYWORD_ONLY, None),
        ]


class TestStatements:
    """AC-3: ``INFO FOR DB;`` then one ``INFO FOR TABLE`` per wanted table, in codepoint order, ``{}`` as variables,
    no ``txn_id``, on the client ``get_client()`` hands out without an argument."""

    async def test_every_table_in_codepoint_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tables = {
            "b": _TABLE_B,
            "a": _TABLE_A,
            "B": "DEFINE TABLE B TYPE ANY SCHEMALESS PERMISSIONS NONE",
            "we`ird": "DEFINE TABLE `we\\`ird` TYPE ANY SCHEMALESS PERMISSIONS NONE",
            "back\\slash": "DEFINE TABLE `back\\\\slash` TYPE ANY SCHEMALESS PERMISSIONS NONE",
        }
        client = _FakeClient(_db(tables=tables))
        requests = _install(monkeypatch, client)
        schema = await DatabaseIntrospector().introspect()
        assert client.statements == [
            "INFO FOR DB;",
            "INFO FOR TABLE `B`;",
            "INFO FOR TABLE `a`;",
            "INFO FOR TABLE `b`;",
            "INFO FOR TABLE `back\\\\slash`;",
            "INFO FOR TABLE `we\\`ird`;",
        ]
        assert all(vars == {} and args == () and kwargs == {} for _, vars, args, kwargs in client.calls)
        assert requests == [((), {})]
        assert list(schema.tables) == ["B", "a", "b", "back\\slash", "we`ird"]
        assert schema.tables["we`ird"].name == "we`ird"
        assert schema.issues == ()

    async def test_tables_filter_reads_those_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient(_db(tables={"a": _TABLE_A, "b": _TABLE_B}))
        _install(monkeypatch, client)
        schema = await DatabaseIntrospector(tables=["b", "b"]).introspect()
        assert client.statements == ["INFO FOR DB;", "INFO FOR TABLE `b`;"]
        assert list(schema.tables) == ["b"]

    async def test_tables_filter_is_materialised_at_construction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient(_db(tables={"a": _TABLE_A, "b": _TABLE_B}))
        _install(monkeypatch, client)
        introspector = DatabaseIntrospector(tables=(name for name in ["a"]))
        assert list((await introspector.introspect()).tables) == ["a"]
        assert list((await introspector.introspect()).tables) == ["a"]  # reusable: the generator was read once

    async def test_empty_tables_reads_no_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        analyzer = "DEFINE ANALYZER an TOKENIZERS BLANK FILTERS LOWERCASE"
        client = _FakeClient(_db(tables={"a": _TABLE_A}, analyzers={"an": analyzer}))
        _install(monkeypatch, client)
        schema = await DatabaseIntrospector(tables=[]).introspect()
        assert client.statements == ["INFO FOR DB;"]
        assert schema.tables == {}
        assert schema.analyzers["an"].tokenizers == ("blank",)


class TestSections:
    """Spec §5.5: what is read, kept raw, ignored or reported."""

    async def test_every_section_typed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _db(
            tables={"person": "DEFINE TABLE person TYPE NORMAL SCHEMAFULL COMMENT 'p' PERMISSIONS NONE"},
            analyzers={"an": "DEFINE ANALYZER an TOKENIZERS BLANK,CLASS FILTERS LOWERCASE,ASCII"},
            accesses={
                "acc": "DEFINE ACCESS acc ON DATABASE TYPE RECORD SIGNUP (CREATE user SET email = $email) WITH JWT "
                "ALGORITHM HS512 KEY '[REDACTED]' WITH ISSUER KEY '[REDACTED]' DURATION FOR TOKEN 15m, FOR SESSION 12h"
            },
            functions={"greet": "DEFINE FUNCTION fn::greet($name: string) { RETURN $name; } PERMISSIONS FULL"},
            params={"endpoint": "DEFINE PARAM $endpoint VALUE 'https://x' PERMISSIONS FULL"},
        )
        person = _table(
            fields={
                "age": "DEFINE FIELD age ON person TYPE option<int> DEFAULT 18 PERMISSIONS FULL",
                "tags[*]": "DEFINE FIELD tags[*] ON person TYPE string PERMISSIONS FULL",
            },
            indexes={"person_age": "DEFINE INDEX person_age ON person FIELDS age UNIQUE"},
            events={"ev": "DEFINE EVENT ev ON person WHEN $event = 'CREATE' THEN (CREATE log SET x = 1)"},
        )
        client = _FakeClient(db, {"person": person})
        _install(monkeypatch, client)
        schema = await DatabaseIntrospector().introspect()
        assert schema.issues == ()
        table = schema.tables["person"]
        assert (table.schema_mode, table.table_type, table.comment) == ("SCHEMAFULL", "normal", "p")
        assert list(table.fields) == ["age", "tags.*"]
        assert (table.fields["age"].field_type, table.fields["age"].nullable, table.fields["age"].default) == (
            "int",
            True,
            "18",
        )
        assert table.fields["tags.*"].path == ("tags", "*")
        assert table.indexes["person_age"].fields == ("age",)
        assert table.indexes["person_age"].unique is True
        assert table.events["ev"].then == "(CREATE log SET x = 1)"
        assert schema.analyzers["an"].filters == ("lowercase", "ascii")
        assert schema.accesses["acc"].table == "user"
        assert list(schema.functions) == ["fn::greet"]
        assert schema.functions["fn::greet"].parameters == ("name",)
        assert list(schema.params) == ["endpoint"]
        assert schema.params["endpoint"].value == "'https://x'"
        assert schema.other == {"apis": {}, "configs": {}, "models": {}}

    async def test_users_are_never_parsed_nor_kept(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=_LOGGER)
        # A user statement the parser could not read, with a password hash: no issue, nowhere in the result.
        users = {"admin": "DEFINE USER admin ON DATABASE PASSHASH '$argon2id$v=19$secret-hash' ROLES OWNER ((("}
        client = _FakeClient(_db(users=users))
        _install(monkeypatch, client)
        schema = await DatabaseIntrospector().introspect()
        assert schema.issues == ()
        assert "users" not in schema.other
        assert "secret-hash" not in repr(schema)
        assert _issue_records(caplog) == []

    async def test_other_sections_are_kept_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _db(
            apis={"/a": "DEFINE API '/a' FOR any THEN {}"},
            buckets={},
            configs={"GRAPHQL": "DEFINE CONFIG GRAPHQL AUTO"},
            models={},
            modules={},
            sequences={"s": "DEFINE SEQUENCE s BATCH 1000 START 10"},
        )
        client = _FakeClient(db)
        _install(monkeypatch, client)
        schema = await DatabaseIntrospector().introspect()
        assert schema.other == {
            "apis": {"/a": "DEFINE API '/a' FOR any THEN {}"},
            "buckets": {},
            "configs": {"GRAPHQL": "DEFINE CONFIG GRAPHQL AUTO"},
            "models": {},
            "modules": {},
            "sequences": {"s": "DEFINE SEQUENCE s BATCH 1000 START 10"},
        }
        assert schema.issues == ()

    async def test_a_section_that_is_not_a_mapping_is_an_issue(self, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _db(modules=[], functions=["DEFINE FUNCTION fn::f() { RETURN 1; } PERMISSIONS FULL"], params={"p": 1})
        client = _FakeClient(db)
        _install(monkeypatch, client)
        schema = await DatabaseIntrospector().introspect()
        assert schema.functions == {}
        assert schema.params == {}
        assert "modules" not in schema.other
        assert schema.issues == (
            SchemaIssue("section", "functions", None, "section 'functions' is not a mapping of names to definitions"),
            SchemaIssue("section", "modules", None, "section 'modules' is not a mapping of names to definitions"),
            SchemaIssue("section", "params", None, "section 'params' is not a mapping of names to definitions"),
        )

    @pytest.mark.parametrize("tables", [[], "x", None], ids=["list", "str", "missing"])
    async def test_a_tables_section_that_is_not_a_mapping(self, monkeypatch: pytest.MonkeyPatch, tables: object) -> None:
        db = _db(tables=tables)
        if tables is None:
            del db["tables"]
        client = _FakeClient(db)
        _install(monkeypatch, client)
        schema = await DatabaseIntrospector().introspect()
        assert schema.tables == {}
        assert client.statements == ["INFO FOR DB;"]
        assert schema.issues == (
            SchemaIssue("section", "tables", None, "section 'tables' is not a mapping of names to definitions"),
        )

    async def test_table_sections(self, monkeypatch: pytest.MonkeyPatch) -> None:
        answer = _table(
            lives={"0190": "LIVE SELECT * FROM a"},
            tables={"v": "DEFINE TABLE v TYPE ANY SCHEMALESS AS SELECT * FROM a PERMISSIONS NONE"},
            future={"k": "DEFINE SOMETHING k"},
            broken=["x"],
        )
        client = _FakeClient(_db(tables={"a": _TABLE_A}), {"a": answer})
        _install(monkeypatch, client)
        schema = await DatabaseIntrospector().introspect()
        assert schema.tables["a"].other == {"future": {"k": "DEFINE SOMETHING k"}}
        assert schema.tables["a"].fields == {}
        assert schema.issues == (
            SchemaIssue("section", "broken", "a", "section 'broken' is not a mapping of names to definitions"),
        )


class TestErrors:
    """Spec §5.6: E1–E7 and E9, raised with their exact texts."""

    @pytest.mark.parametrize(
        "error",
        [
            Exception("IAM error: Not enough permissions to perform this action"),
            type("NotAllowedError", (Exception,), {})("IAM error: Not enough permissions to perform this action"),
            RuntimeError("there was a problem: not enough permissions"),
        ],
        ids=["exception", "not-allowed", "lower-case"],
    )
    async def test_e1_refused(self, monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
        client = _FakeClient(_db(), errors={"INFO FOR DB;": error})
        _install(monkeypatch, client)
        with pytest.raises(SurrealDbAuthenticationError) as caught:
            await DatabaseIntrospector().introspect()
        assert str(caught.value) == _e1("default", error)
        assert caught.value.__cause__ is error

    @pytest.mark.parametrize(
        "error",
        [
            Exception("boom"),
            type("NotFoundError", (Exception,), {})("The database 'x' does not exist"),
        ],
        ids=["other", "removed-database"],
    )
    async def test_e2_failed(self, monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
        _named("isp_unit")
        client = _FakeClient(_db(), errors={"INFO FOR DB;": error})
        _install(monkeypatch, client, "isp_unit")
        with pytest.raises(SurrealDbError) as caught:
            await DatabaseIntrospector("isp_unit").introspect()
        assert type(caught.value) is SurrealDbError
        assert str(caught.value) == f"Cannot read the schema of connection 'isp_unit': INFO FOR DB failed: {error}"
        assert caught.value.__cause__ is error

    async def test_an_orm_error_propagates_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        error = SurrealDbConnectionError("the connection dropped")
        client = _FakeClient(_db(), errors={"INFO FOR DB;": error})
        _install(monkeypatch, client)
        with pytest.raises(SurrealDbConnectionError) as caught:
            await DatabaseIntrospector().introspect()
        assert caught.value is error

    async def test_a_get_client_error_propagates_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        error = SurrealDbConnectionError("cannot connect")

        async def get_client(*args: object, **kwargs: object) -> object:
            raise error

        monkeypatch.setattr(SurrealDBConnectionManager._manager_for("default"), "get_client", get_client)
        with pytest.raises(SurrealDbConnectionError) as caught:
            await DatabaseIntrospector().introspect()
        assert caught.value is error

    @pytest.mark.parametrize(("answer", "kind"), [([], "list"), (None, "NoneType"), ("x", "str")])
    async def test_e3_not_an_object(self, monkeypatch: pytest.MonkeyPatch, answer: object, kind: str) -> None:
        client = _FakeClient(answer)
        _install(monkeypatch, client)
        with pytest.raises(SurrealDbError) as caught:
            await DatabaseIntrospector().introspect()
        assert (
            str(caught.value) == f"Cannot read the schema of connection 'default': INFO FOR DB returned {kind}, not an object."
        )

    async def test_e4_unknown_tables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient(_db(tables={"a": _TABLE_A}))
        _install(monkeypatch, client)
        with pytest.raises(SurrealDbNotFoundError) as caught:
            await DatabaseIntrospector(tables=["zz", "a", "zz", "yy"]).introspect()
        assert str(caught.value) == "Not defined in the database of connection 'default': 'yy', 'zz'."
        assert client.statements == ["INFO FOR DB;"]

    def test_e5_tables_is_a_str(self) -> None:
        with pytest.raises(TypeError) as caught:
            DatabaseIntrospector(tables="person")
        assert str(caught.value) == "tables= takes an iterable of table names, not a str: write tables=['person']."

    @pytest.mark.parametrize(("item", "kind"), [(1, "int"), (None, "NoneType"), (b"a", "bytes")])
    def test_e6_a_table_name_is_not_a_str(self, item: object, kind: str) -> None:
        with pytest.raises(TypeError) as caught:
            DatabaseIntrospector(tables=["a", item])  # type: ignore[list-item]
        assert str(caught.value) == f"tables= takes table names (str), got {kind}."

    @pytest.mark.parametrize(("connection", "kind"), [(object(), "object"), (42, "int"), (b"x", "bytes")])
    def test_e7_r3_not_a_str(self, connection: object, kind: str) -> None:
        with pytest.raises(TypeError) as caught:
            DatabaseIntrospector(connection)  # type: ignore[arg-type]
        assert str(caught.value) == f"Connection name must be a str, not {kind}."

    def test_e7_r3_empty(self) -> None:
        with pytest.raises(ValueError, match=r"^Connection name must not be empty\.$"):
            DatabaseIntrospector("")

    async def test_e7_r1_unknown_at_introspect(self) -> None:
        introspector = DatabaseIntrospector("isp_unknown")  # registration is checked when it runs
        with pytest.raises(ValueError, match=r"^Unknown connection: 'isp_unknown'\."):
            await introspector.introspect()

    def test_e9_an_sdk_like_client(self) -> None:
        class SdkLikeClient:
            def __init__(self) -> None:
                self.sent: list[str] = []

            async def query(self, sql: str, vars: object) -> object:
                self.sent.append(sql)
                return {}

        client = SdkLikeClient()
        with pytest.raises(TypeError) as caught:
            DatabaseIntrospector(client)  # type: ignore[arg-type]
        assert str(caught.value) == _E9
        assert client.sent == []

    def test_e9_is_checked_before_r3(self) -> None:
        with pytest.raises(TypeError) as caught:
            DatabaseIntrospector(mock.MagicMock())
        assert str(caught.value) == _E9


class TestIssues:
    """Spec §5.7: I1–I5 — appended in the order found, logged once each, never raised; the secret check (AC-10)."""

    async def test_i1_unparsable_definitions(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        broken_table = "DEFINE TABLE `broken TYPE ANY SCHEMALESS PERMISSIONS NONE"
        statements = {
            "access": "DEFINE ACCESS a ON DATABASE TYPE FROB",
            "analyzer": "DEFINE ANALYZER an TOKENIZERS blank FILTERS lowercase(",
            "function": "DEFINE FUNCTION fn::f($a: int) RETURN 1",
            "param": "DEFINE PARAM $p VALUE 'unterminated",
            "field": "DEFINE FIELD a ON t VALUE b ASSERT ASSERT c PERMISSIONS FULL",
            "index": "DEFINE INDEX ix ON t COUNT WHERE a > 1",
            "event": "DEFINE EVENT e ON t ASYNC RETRY 3 MAXDEPTH 2 WHEN true THEN (CREATE x)",
        }
        db = _db(
            tables={"broken": broken_table, "t": "DEFINE TABLE t TYPE NORMAL SCHEMAFULL PERMISSIONS NONE"},
            accesses={"a": statements["access"]},
            analyzers={"an": statements["analyzer"]},
            functions={"f": statements["function"]},
            params={"p": statements["param"]},
        )
        t = _table(
            fields={"a": statements["field"], "ok": "DEFINE FIELD ok ON t TYPE int PERMISSIONS FULL"},
            indexes={"ix": statements["index"]},
            events={"e": statements["event"]},
        )
        client = _FakeClient(db, {"t": t})
        _install(monkeypatch, client)
        schema = await DatabaseIntrospector().introspect()
        assert client.statements == ["INFO FOR DB;", "INFO FOR TABLE `t`;"]  # no INFO FOR TABLE for the broken table
        assert list(schema.tables) == ["t"]
        assert list(schema.tables["t"].fields) == ["ok"]
        assert (schema.tables["t"].indexes, schema.tables["t"].events) == ({}, {})
        assert (schema.accesses, schema.analyzers, schema.functions, schema.params) == ({}, {}, {}, {})
        # The database-level sections in codepoint order, then each table (spec §6.2).
        expected = [
            ("access", "a", None, _R_HEADER, statements["access"]),
            ("analyzer", "an", None, _R_BRACKETS, statements["analyzer"]),
            ("function", "f", None, _R_SIGNATURE, statements["function"]),
            ("param", "p", None, _R_STRING, statements["param"]),
            ("table", "broken", "broken", _R_IDENT, broken_table),
            ("event", "e", "t", _R_NO_READING, statements["event"]),
            ("field", "a", "t", _R_AMBIGUOUS, statements["field"]),
            ("index", "ix", "t", _R_INDEX, statements["index"]),
        ]
        assert [(i.kind, i.name, i.table, i.message, i.statement) for i in schema.issues] == [
            (kind, name, table, _parse_failure(reason), statement) for kind, name, table, reason, statement in expected
        ]
        records = _issue_records(caplog)
        assert [(r.levelno, r.getMessage()) for r in records] == [
            (logging.WARNING, f"Schema introspection: {_parse_failure(reason)} ({kind} {name!r})")
            for kind, name, _, reason, _ in expected
        ]

    async def test_i1_keeps_a_secret_out_of_message_repr_and_log(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        statement = "DEFINE PARAM $api_key VALUE 's3cr3t-0042"
        client = _FakeClient(_db(params={"api_key": statement}))
        _install(monkeypatch, client)
        schema = await DatabaseIntrospector().introspect()
        (issue,) = schema.issues
        assert (issue.kind, issue.name, issue.table) == ("param", "api_key", None)
        assert issue.message == _parse_failure(_R_STRING)
        assert issue.statement == statement
        assert "s3cr3t-0042" not in issue.message
        assert "s3cr3t-0042" not in repr(issue)
        assert "s3cr3t-0042" not in repr(schema)
        assert "s3cr3t-0042" not in caplog.text
        for record in caplog.records:
            assert "s3cr3t-0042" not in record.getMessage()
            assert "s3cr3t-0042" not in repr(record.args)

    async def test_i2_info_for_table_failing(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        error = Exception("A user generated conversion error occured")
        db = _db(
            tables={
                "a": "DEFINE TABLE a TYPE NORMAL SCHEMAFULL COMMENT 'kept' PERMISSIONS NONE",
                "b": _TABLE_B,
                "c": "DEFINE TABLE c TYPE ANY SCHEMALESS PERMISSIONS NONE",
            }
        )
        b = _table(fields={"x": "DEFINE FIELD x ON b TYPE int PERMISSIONS FULL"})
        client = _FakeClient(db, {"b": b, "c": ["not", "an", "object"]}, errors={_info_table("a"): error})
        _install(monkeypatch, client)
        schema = await DatabaseIntrospector().introspect()
        assert list(schema.tables) == ["a", "b", "c"]
        a = schema.tables["a"]
        assert (a.comment, a.schema_mode, a.fields, a.indexes, a.events, a.other) == ("kept", "SCHEMAFULL", {}, {}, {}, {})
        assert list(schema.tables["b"].fields) == ["x"]
        assert schema.tables["c"].fields == {}
        assert schema.issues == (
            SchemaIssue("table", "a", "a", f"INFO FOR TABLE failed: {error}"),
            SchemaIssue("table", "c", "c", "INFO FOR TABLE failed: it returned list, not an object."),
        )
        assert len(_issue_records(caplog)) == 2

    async def test_i2_a_table_name_holding_nul(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Never sent: the identifier escaping refuses a NUL, which is reported for that table only.
        client = _FakeClient(
            _db(
                tables={
                    "a\x00b": "DEFINE TABLE `a\x00b` TYPE ANY SCHEMALESS PERMISSIONS NONE",
                    "c": _TABLE_A.replace(" a ", " c "),
                }
            )
        )
        _install(monkeypatch, client)
        schema = await DatabaseIntrospector().introspect()
        assert client.statements == ["INFO FOR DB;", "INFO FOR TABLE `c`;"]
        assert list(schema.tables) == ["a\x00b", "c"]
        ((kind, name, table, message),) = [(i.kind, i.name, i.table, i.message) for i in schema.issues]
        assert (kind, name, table) == ("table", "a\x00b", "a\x00b")
        assert message.startswith("INFO FOR TABLE failed: A value containing a NUL character")

    async def test_i3_type_kept_as_written(self, monkeypatch: pytest.MonkeyPatch) -> None:
        statement = "DEFINE FIELD f ON t TYPE frob< PERMISSIONS FULL"
        client = _FakeClient(_db(tables={"t": _TABLE_A.replace(" a ", " t ")}), {"t": _table(fields={"f": statement})})
        _install(monkeypatch, client)
        schema = await DatabaseIntrospector().introspect()
        field = schema.tables["t"].fields["f"]
        assert (field.field_type, field.nullable, field.raw) == ("frob<", False, statement)
        assert schema.issues == (
            SchemaIssue("field", "f", "t", "its TYPE clause was not understood and is kept as written", statement),
        )

    async def test_i5_two_fields_with_the_same_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        first = "DEFINE FIELD `a.b` ON t TYPE int PERMISSIONS FULL"
        second = "DEFINE FIELD a.b ON t TYPE string PERMISSIONS FULL"
        client = _FakeClient(
            _db(tables={"t": _TABLE_A.replace(" a ", " t ")}), {"t": _table(fields={"`a.b`": first, "a.b": second})}
        )
        _install(monkeypatch, client)
        schema = await DatabaseIntrospector().introspect()
        field = schema.tables["t"].fields["a.b"]
        assert (field.path, field.field_type) == (("a.b",), "int")  # the first, in codepoint order of the keys
        assert schema.issues == (SchemaIssue("field", "a.b", "t", "two definitions have the same name", second),)

    async def test_i5_two_definitions_of_another_kind_with_the_same_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Never measured (the servers key each section by the name): the second one is reported, not dropped silently.
        second = "DEFINE ANALYZER an TOKENIZERS CLASS"
        client = _FakeClient(_db(analyzers={"an": "DEFINE ANALYZER an TOKENIZERS BLANK", "an2": second}))
        _install(monkeypatch, client)
        schema = await DatabaseIntrospector().introspect()
        assert list(schema.analyzers) == ["an"]
        assert schema.analyzers["an"].tokenizers == ("blank",)
        assert schema.issues == (SchemaIssue("analyzer", "an2", None, "two definitions have the same name", second),)

    async def test_issues_keep_their_order_and_never_raise(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        db = _db(
            tables={"t": _TABLE_A.replace(" a ", " t "), "u": _TABLE_B.replace(" b ", " u ")},
            modules=[],
            params={"p": "DEFINE PARAM $p VALUE 'x"},
        )
        t = _table(fields={"f": "DEFINE FIELD f ON t TYPE frob< PERMISSIONS FULL"})
        client = _FakeClient(db, {"t": t}, errors={_info_table("u"): Exception("gone")})
        _install(monkeypatch, client)
        schema = await DatabaseIntrospector().introspect()
        assert [(i.kind, i.name) for i in schema.issues] == [
            ("section", "modules"),
            ("param", "p"),
            ("field", "f"),
            ("table", "u"),
        ]
        assert [r.getMessage() for r in _issue_records(caplog)] == [
            f"Schema introspection: {i.message} ({i.kind} {i.name!r})" for i in schema.issues
        ]


class TestConnections:
    """Spec §5.1, §7.2: the connection is resolved when ``introspect()`` runs; ``SchemaState.connection`` names it."""

    async def test_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, _FakeClient(_db()))
        assert (await DatabaseIntrospector().introspect()).connection == "default"

    async def test_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _named("isp_unit")
        named = _FakeClient(_db(tables={"a": _TABLE_A}))
        default = _FakeClient(_db())
        _install(monkeypatch, named, "isp_unit")
        _install(monkeypatch, default)
        schema = await DatabaseIntrospector("isp_unit").introspect()
        assert schema.connection == "isp_unit"
        assert list(schema.tables) == ["a"]
        assert default.calls == []

    async def test_using_is_resolved_when_introspect_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _named("isp_unit")
        named = _FakeClient(_db(tables={"a": _TABLE_A}))
        default = _FakeClient(_db())
        _install(monkeypatch, named, "isp_unit")
        _install(monkeypatch, default)
        introspector = DatabaseIntrospector()  # constructed outside the block
        async with SurrealDBConnectionManager.using("isp_unit"):
            schema = await introspector.introspect()
        assert schema.connection == "isp_unit"
        assert default.calls == []
        assert (await introspector.introspect()).connection == "default"


# === E2E: the line under test and mem:// ==============================================================================

#: The portable fixture of spec §9.3, exactly.
FIXTURE_DDL = """\
DEFINE TABLE isp_company SCHEMAFULL COMMENT 'Companies';
DEFINE FIELD name ON isp_company TYPE string;
DEFINE TABLE isp_person SCHEMAFULL;
DEFINE FIELD name ON isp_person TYPE string ASSERT string::len($value) > 0;
DEFINE FIELD email ON isp_person TYPE string VALUE string::lowercase($value);
DEFINE FIELD age ON isp_person TYPE option<int> DEFAULT 18;
DEFINE FIELD status ON isp_person TYPE 'active' | 'inactive' DEFAULT 'active';
DEFINE FIELD created ON isp_person TYPE datetime DEFAULT time::now() READONLY;
DEFINE FIELD updated ON isp_person TYPE datetime VALUE time::now();
DEFINE FIELD tags ON isp_person TYPE array<string>;
DEFINE FIELD address ON isp_person TYPE option<object>;
DEFINE FIELD address.city ON isp_person TYPE string;
DEFINE FIELD meta ON isp_person TYPE option<object> FLEXIBLE;
DEFINE FIELD location ON isp_person TYPE option<geometry<point>>;
DEFINE FIELD trial ON isp_person TYPE option<duration>;
DEFINE FIELD balance ON isp_person TYPE option<decimal>;
DEFINE FIELD token ON isp_person TYPE option<uuid>;
DEFINE FIELD employer ON isp_person TYPE option<record<isp_company>>;
DEFINE FIELD `first-name` ON isp_person TYPE option<string>;
DEFINE FIELD `class` ON isp_person TYPE option<string>;
DEFINE INDEX isp_person_email ON isp_person FIELDS email UNIQUE;
DEFINE EVENT isp_person_created ON isp_person WHEN $event = 'CREATE' THEN (CREATE isp_log SET msg = 'created');
DEFINE TABLE isp_likes TYPE RELATION IN isp_person OUT isp_person | isp_company SCHEMAFULL;
DEFINE FIELD since ON isp_likes TYPE option<datetime>;
DEFINE TABLE isp_log SCHEMALESS;
DEFINE TABLE `isp-weird` SCHEMALESS;
DEFINE TABLE `select` SCHEMALESS;
DEFINE ANALYZER isp_ascii TOKENIZERS class FILTERS lowercase, ascii;
DEFINE FUNCTION fn::isp_greet($name: string) { RETURN 'Hello ' + $name; };
DEFINE PARAM $isp_endpoint VALUE 'https://example.com';
"""

#: The fixture's tables, in codepoint order.
_FIXTURE_TABLES = ["isp-weird", "isp_company", "isp_likes", "isp_log", "isp_person", "select"]


def _url(kind: str) -> str:
    """The URL of *kind*: ``ws`` or ``http`` on the line under test (``SURREALDB_PORT``), or ``mem``."""
    if kind == "mem":
        return "mem://"
    host = os.environ.get("SURREALDB_HOST", "localhost")
    port = os.environ.get("SURREALDB_PORT", "8000")
    return f"ws://{host}:{port}/rpc" if kind == "ws" else f"http://{host}:{port}"


async def _apply_ddl(client: Any, ddl: str) -> None:
    """Run *ddl* and check every statement (spec §9.3): ``query()`` checks only the first one (M38)."""
    response = await client.query_raw(ddl, {})
    assert "error" not in response, response["error"]
    refused = [result for result in response["result"] if result["status"] != "OK"]
    assert response["result"] and not refused, refused


async def _major(client: Any) -> int:
    """The major version of the engine *client* talks to: ``"surrealdb-3.3.4"`` → 3; ``mem://`` → 2 (M38)."""
    return int((await client.version()).rsplit("-", 1)[-1].split(".", 1)[0])


async def _require_ddl(client: Any, statement: str, *, line: int, item: str) -> None:
    """Apply a one-line-only *statement* (spec §9.4): refused on its own *line* → fail, on the other → skip."""
    major = await _major(client)
    try:
        response = await client.query_raw(statement, {})
        accepted = "error" not in response and all(r["status"] == "OK" for r in response["result"])
    except Exception:
        accepted = False
    if accepted:
        return
    if major == line:
        pytest.fail(f"SurrealDB {line}.x refused its own DDL for {item}: fix the test's statement")
    pytest.skip(f"SurrealDB {line}.x only: {item}")


def _register_isp(kind: str) -> None:
    if kind == "mem":
        SurrealDBConnectionManager.add_connection("isp", url="mem://", namespace="ns", database="isp_db")
    else:
        SurrealDBConnectionManager.add_connection(
            "isp", url=_url(kind), user="root", password="root", namespace="ns", database="isp_db"
        )


@contextlib.asynccontextmanager
async def _isp(kind: str, ddl: str = FIXTURE_DDL) -> AsyncIterator[Any]:
    """The connection ``"isp"`` on *kind*, its database ``isp_db`` reset in one query (M38) and *ddl* applied;
    removed, then unregistered, on exit."""
    _register_isp(kind)
    client = await SurrealDBConnectionManager.get_client("isp")
    await client.query("REMOVE DATABASE IF EXISTS isp_db; DEFINE DATABASE isp_db;", {})
    await _apply_ddl(client, ddl)
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            cleanup = await SurrealDBConnectionManager.get_client("isp")
            await cleanup.query("REMOVE DATABASE IF EXISTS isp_db;", {})
        await SurrealDBConnectionManager.remove_connection("isp")


def _spy(monkeypatch: pytest.MonkeyPatch, client: Any) -> list[tuple[object, ...]]:
    """Record every ``query`` call *client* receives (and let it through). The SDK's ``query()`` runs on its own
    ``query_raw()``, so this sees each statement the ORM sends through ``query()`` once."""
    calls: list[tuple[object, ...]] = []
    real_query = client.query

    async def query(*args: object, **kwargs: object) -> object:
        calls.append(("query", args, kwargs))
        return await real_query(*args, **kwargs)

    monkeypatch.setattr(client, "query", query)
    return calls


def _info_calls(*tables: str) -> list[tuple[object, ...]]:
    """The calls AC-3 expects: ``INFO FOR DB;`` then each of *tables*, every one with ``{}``."""
    statements = ["INFO FOR DB;", *(_info_table(table) for table in tables)]
    return [("query", (statement, {}), {}) for statement in statements]


def _check_fixture(schema: SchemaState, major: int) -> None:
    """AC-5: the §9.3 fixture as read on every line — normalised kinds, expression texts as both lines render them."""
    assert schema.issues == ()
    assert schema.connection == "isp"
    assert list(schema.tables) == _FIXTURE_TABLES

    company = schema.tables["isp_company"]
    assert (company.schema_mode, company.table_type, company.comment) == ("SCHEMAFULL", "normal", "Companies")
    assert list(company.fields) == ["name"]
    assert (company.fields["name"].field_type, company.fields["name"].nullable) == ("string", False)

    person = schema.tables["isp_person"]
    assert (person.schema_mode, person.table_type, person.comment) == ("SCHEMAFULL", "normal", None)
    assert list(person.fields) == [
        "address",
        "address.city",
        "age",
        "balance",
        "class",
        "created",
        "email",
        "employer",
        "first-name",
        "location",
        "meta",
        "name",
        "status",
        "tags",
        "tags.*",
        "token",
        "trial",
        "updated",
    ]
    fields = person.fields
    kinds = {name: (field.field_type, field.nullable) for name, field in fields.items()}
    assert kinds == {
        "address": ("object", True),
        "address.city": ("string", False),
        "age": ("int", True),
        "balance": ("decimal", True),
        "class": ("string", True),
        "created": ("datetime", False),
        "email": ("string", False),
        "employer": ("record<isp_company>", True),
        "first-name": ("string", True),
        "location": ("geometry<point>", True),
        "meta": ("object", True),
        "name": ("string", False),
        "status": ("'active' | 'inactive'", False),
        "tags": ("array<string>", False),
        "tags.*": ("string", False),
        "token": ("uuid", True),
        "trial": ("duration", True),
        "updated": ("datetime", False),
    }
    assert all(field.table == "isp_person" for field in fields.values())
    assert (fields["age"].default, fields["status"].default, fields["created"].default) == ("18", "'active'", "time::now()")
    assert fields["created"].readonly is True
    assert [name for name, field in fields.items() if field.readonly] == ["created"]
    assert fields["email"].value == "string::lowercase($value)"
    assert fields["updated"].value == "time::now()"
    assert fields["name"].assertion == "string::len($value) > 0"
    assert [name for name, field in fields.items() if field.flexible] == ["meta"]
    assert fields["tags.*"].path == ("tags", "*")
    assert fields["address.city"].path == ("address", "city")
    assert fields["first-name"].path == ("first-name",)
    assert fields["name"].permissions == {"select": "FULL", "create": "FULL", "update": "FULL"}
    index = person.indexes["isp_person_email"]
    assert (list(person.indexes), index.fields, index.unique, index.table) == (
        ["isp_person_email"],
        ("email",),
        True,
        "isp_person",
    )
    event = person.events["isp_person_created"]
    assert (list(person.events), event.table, event.when, event.then) == (
        ["isp_person_created"],
        "isp_person",
        "$event = 'CREATE'",
        "(CREATE isp_log SET msg = 'created')",
    )

    likes = schema.tables["isp_likes"]
    assert (likes.schema_mode, likes.table_type, likes.relation_in, likes.relation_out) == (
        "SCHEMAFULL",
        "relation",
        ("isp_person",),
        ("isp_person", "isp_company"),
    )
    assert {name: (f.field_type, f.nullable) for name, f in likes.fields.items()} == {
        "in": ("record<isp_person>", False),
        "out": ("record<isp_person | isp_company>", False),
        "since": ("datetime", True),
    }
    for name in ("isp_log", "isp-weird", "select"):
        table = schema.tables[name]
        assert (table.name, table.schema_mode, table.table_type, table.fields) == (name, "SCHEMALESS", "any", {})

    assert list(schema.analyzers) == ["isp_ascii"]
    assert (schema.analyzers["isp_ascii"].tokenizers, schema.analyzers["isp_ascii"].filters) == (
        ("class",),
        ("lowercase", "ascii"),
    )
    assert list(schema.functions) == ["fn::isp_greet"]
    greet = schema.functions["fn::isp_greet"]
    assert (greet.parameters, greet.returns, greet.permissions) == (("name",), None, "FULL")
    assert list(schema.params) == ["isp_endpoint"]
    assert (schema.params["isp_endpoint"].value, schema.params["isp_endpoint"].permissions) == (
        "'https://example.com'",
        "FULL",
    )
    assert schema.accesses == {}
    expected_other: dict[str, dict[str, str]] = {"apis": {}, "configs": {}, "models": {}}
    if major >= 3:
        expected_other = {**expected_other, "buckets": {}, "modules": {}, "sequences": {}}
    assert schema.other == expected_other
    assert "users" not in schema.other


class TestIntrospectionE2E:
    """AC-3, AC-4, AC-5 and AC-10 on the line under test (WebSocket, HTTP) and on ``mem://``."""

    @pytest.mark.parametrize("kind", ["ws", "http", "mem"])
    async def test_fixture(self, kind: str) -> None:
        async with _isp(kind) as client:
            schema = await DatabaseIntrospector("isp").introspect()
            _check_fixture(schema, await _major(client))

    @pytest.mark.parametrize("kind", ["ws", "mem"])
    async def test_only_info_statements_are_sent(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _isp(kind) as client:
            calls = _spy(monkeypatch, client)
            await DatabaseIntrospector("isp").introspect()
            assert calls == _info_calls(*_FIXTURE_TABLES)
            calls.clear()
            await DatabaseIntrospector("isp", tables=["isp_person"]).introspect()
            assert calls == _info_calls("isp_person")
            calls.clear()
            schema = await DatabaseIntrospector("isp", tables=[]).introspect()
            assert calls == _info_calls()
            assert schema.tables == {}
            assert list(schema.analyzers) == ["isp_ascii"]

    @pytest.mark.parametrize("kind", ["ws", "mem"])
    async def test_e9_a_real_client(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _isp(kind) as client:
            calls = _spy(monkeypatch, client)
            with pytest.raises(TypeError) as caught:
                DatabaseIntrospector(client)
            assert str(caught.value) == _E9
            assert calls == []

    @pytest.mark.parametrize("kind", ["ws", "mem"])
    async def test_using_block(self, kind: str) -> None:
        async with _isp(kind):
            introspector = DatabaseIntrospector(tables=["isp_company"])
            async with SurrealDBConnectionManager.using("isp"):
                schema = await introspector.introspect()
            assert schema.connection == "isp"
            assert list(schema.tables) == ["isp_company"]
            assert schema.tables["isp_company"].comment == "Companies"

    @pytest.mark.parametrize("kind", ["ws", "mem"])
    async def test_default_connection_reads_its_own_database(self, kind: str) -> None:
        async with _isp(kind), contextlib.AsyncExitStack() as stack:
            if kind == "mem":
                SurrealDBConnectionManager.set_connection(url="mem://", namespace="ns", database="db")
                default = await SurrealDBConnectionManager.get_client()
            else:
                default = await stack.enter_async_context(orm_client("isp_default"))
            await default.query("DEFINE TABLE isp_default SCHEMALESS COMMENT 'default';", {})
            schema = await DatabaseIntrospector(tables=["isp_default"]).introspect()
            assert schema.connection == "default"
            assert list(schema.tables) == ["isp_default"]
            assert schema.tables["isp_default"].comment == "default"
            with pytest.raises(
                SurrealDbNotFoundError, match=r"^Not defined in the database of connection 'default': 'isp_company'\.$"
            ):
                await DatabaseIntrospector(tables=["isp_company"]).introspect()

    @pytest.mark.parametrize("kind", ["ws", "mem"])
    async def test_an_open_transaction_is_not_seen(self, kind: str) -> None:
        async with _isp(kind):
            async with SurrealDBConnectionManager.transaction("isp") as tx:
                await tx.add("DEFINE TABLE isp_tx SCHEMALESS;")
                inside = await DatabaseIntrospector("isp").introspect()
            after = await DatabaseIntrospector("isp").introspect()
        assert "isp_tx" not in inside.tables
        assert list(inside.tables) == _FIXTURE_TABLES
        assert "isp_tx" in after.tables

    @pytest.mark.parametrize("kind", ["ws", "mem"])
    async def test_e1_a_record_user(self, kind: str) -> None:
        if kind == "mem":
            pytest.skip("the embedded engine's session is its owner: it may always run INFO (M19)")
        access = (
            "DEFINE ACCESS isp_acc ON DATABASE TYPE RECORD SIGNUP (CREATE isp_user SET name = $name) "
            "SIGNIN (SELECT * FROM isp_user WHERE name = $name);"
        )
        async with _isp(kind, FIXTURE_DDL + access), SurrealDBConnectionManager.using("isp"):
            await SurrealDBConnectionManager.signup(access="isp_acc", variables={"name": "isp-reader"})
            try:
                with pytest.raises(SurrealDbAuthenticationError) as caught:
                    await DatabaseIntrospector().introspect()
            finally:
                await SurrealDBConnectionManager.invalidate()
            assert list((await DatabaseIntrospector().introspect()).tables)[:2] == ["isp-weird", "isp_company"]
        assert str(caught.value) == _e1("isp", caught.value.__cause__)
        assert "not enough permissions" in str(caught.value).lower()

    async def test_a_removed_database(self) -> None:
        """AC-10 (server lines): another client removes ``isp_db`` under the ``isp`` session — 3.x fails (E2), 2.x
        answers an empty schema (M38)."""
        async with _isp("ws") as client:
            major = await _major(client)
            other = AsyncSurreal(_url("ws"))
            try:
                await other.signin({"username": "root", "password": "root"})
                await other.use("ns", "isp_db")
                await other.query("REMOVE DATABASE isp_db;", {})
            finally:
                await other.close()
            if major >= 3:
                with pytest.raises(SurrealDbError) as caught:
                    await DatabaseIntrospector("isp").introspect()
                assert type(caught.value) is SurrealDbError
                assert "INFO FOR DB failed" in str(caught.value)
                assert "does not exist" in str(caught.value)
            else:
                schema = await DatabaseIntrospector("isp").introspect()
                assert (schema.tables, schema.issues) == ({}, ())


def _field(table: str, name: str) -> Callable[[SchemaState], FieldState]:
    return lambda schema: schema.tables[table].fields[name]


def _index(name: str) -> Callable[[SchemaState], IndexState]:
    return lambda schema: schema.tables["isp_x"].indexes[name]


#: The one-line-only items of spec §9.4: (line, item, statement, check of what was read).
_ONE_LINE_ITEMS: list[tuple[int, str, str, Callable[[SchemaState], None]]] = [
    (
        3,
        "COMPUTED",
        "DEFINE FIELD c ON isp_x COMPUTED a * 2;",
        lambda s: _check(_field("isp_x", "c")(s), computed="a * 2", field_type="any", value=None),
    ),
    (
        3,
        "REFERENCE",
        "DEFINE FIELD r ON isp_x TYPE option<record<isp_x>> REFERENCE ON DELETE CASCADE;",
        lambda s: _check(
            _field("isp_x", "r")(s), reference=True, on_delete="CASCADE", field_type="record<isp_x>", nullable=True
        ),
    ),
    (
        3,
        "DEFINE SEQUENCE",
        "DEFINE SEQUENCE isp_seq START 10;",
        lambda s: _check(s, other_sequences=["isp_seq"]),
    ),
    (
        3,
        "file",
        "DEFINE FIELD fl ON isp_x TYPE option<file>;",
        lambda s: _check(_field("isp_x", "fl")(s), field_type="file", nullable=True),
    ),
    (
        3,
        "WITH REFRESH",
        "DEFINE ACCESS isp_refresh ON DATABASE TYPE RECORD SIGNIN (SELECT * FROM isp_user WHERE name = $name) WITH REFRESH;",
        lambda s: _check(
            s.accesses["isp_refresh"],
            with_refresh=True,
            access_type="RECORD",
            signin_where="name = $name",
            duration_grant="4w2d",
        ),
    ),
    (
        3,
        "FULLTEXT",
        "DEFINE INDEX isp_ft ON isp_x FIELDS name FULLTEXT ANALYZER isp_ascii BM25 HIGHLIGHTS;",
        lambda s: _check(_index("isp_ft")(s), fields=("name",), search_analyzer="isp_ascii", bm25=(1.2, 0.75), highlights=True),
    ),
    (
        2,
        "MTREE",
        "DEFINE INDEX isp_mt ON isp_x FIELDS v MTREE DIMENSION 3;",
        lambda s: _check(_index("isp_mt")(s), mtree=True, dimension=3, dist="EUCLIDEAN", vector_type="F64", hnsw=False),
    ),
    (
        2,
        "SEARCH ANALYZER",
        "DEFINE INDEX isp_search ON isp_x FIELDS name SEARCH ANALYZER isp_ascii BM25 HIGHLIGHTS;",
        lambda s: _check(_index("isp_search")(s), search_analyzer="isp_ascii", bm25=(1.2, 0.75), highlights=True),
    ),
    (
        2,
        "geometry<feature>",
        "DEFINE FIELD gf ON isp_x TYPE option<geometry<feature>>;",
        lambda s: _check(_field("isp_x", "gf")(s), field_type="geometry<feature>", nullable=True),
    ),
]


def _check(read: object, **expected: object) -> None:
    """Each attribute of *expected* on *read*; ``other_sequences`` names the keys of ``other["sequences"]``."""
    for attribute, value in expected.items():
        if attribute == "other_sequences":
            assert isinstance(read, SchemaState)
            assert list(read.other["sequences"]) == value
            assert read.other["sequences"]["isp_seq"].startswith("DEFINE SEQUENCE isp_seq ")
        else:
            assert getattr(read, attribute) == value, attribute


class TestOneLineItemsE2E:
    """AC-8 (spec §9.4): each 3.x-only item is typed on 3.x and skipped on 2.x and ``mem://``; each 2.x-only item is
    typed on 2.x and ``mem://`` and skipped on 3.x; a refusal on the item's own line fails."""

    @pytest.mark.parametrize("kind", ["ws", "mem"])
    @pytest.mark.parametrize(("line", "item", "statement", "check"), _ONE_LINE_ITEMS, ids=[i[1] for i in _ONE_LINE_ITEMS])
    async def test_item(self, kind: str, line: int, item: str, statement: str, check: Callable[[SchemaState], None]) -> None:
        async with _isp(kind, FIXTURE_DDL + "DEFINE TABLE isp_x SCHEMALESS;") as client:
            await _require_ddl(client, statement, line=line, item=item)
            schema = await DatabaseIntrospector("isp", tables=["isp_x"]).introspect()
            assert schema.issues == ()
            check(schema)
