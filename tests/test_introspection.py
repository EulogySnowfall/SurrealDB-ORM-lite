"""Schema introspection (v0.30.0): ``DatabaseIntrospector`` reads a database's schema with ``INFO FOR DB`` and one
``INFO FOR TABLE`` per table (spec §5.1, §5.5–§5.7, §6.2, §7); ``generate_models_from_db()`` writes the models generated
from it (spec §5.3).

Unit tests first, with a fake client installed on the resolved connection class (no server): the exports (AC-1), the
statements sent (AC-3), the sections read, the errors E1–E7 and E9, the issues I1–I5 with the secret check (AC-10),
the connection resolution (AC-4), and ``generate_models_from_db()``'s arguments, E8 and files (AC-14). Then the classes
suffixed ``E2E``, on the line under test (``SURREALDB_PORT``) and on ``mem://``: the portable fixture of spec §9.3
(AC-5), connections, HTTP and transactions (AC-4), a spy on the real client (AC-3), the one-line-only items through
``_require_ddl`` (AC-8, spec §9.4), the record-user refusal and the removed database (AC-10), and the round trip — the
§9.3 golden source on every line (AC-9), the generated models saving and reading the fixture's rich values back
(AC-13), ``server_fields`` by attribute name (AC-18). E2E tables are prefixed ``isp_`` (plus the fixture's ``select``
and AC-18's ``Select``) and live in the database ``isp_db`` of namespace ``ns``, read through the named connection
``"isp"``; a test reading the default connection passes ``tables=``. Last, README section 31 and the tables it touches
(AC-16, spec §11.1), with its examples run as written on ``mem://``.
"""

import ast
import asyncio
import contextlib
import importlib.util
import inspect
import logging
import os
import pathlib
import re
import stat
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from types import ModuleType
from typing import Any
from unittest import mock

import aiohttp
import pytest
from surrealdb import AsyncSurreal, RecordID
from surrealdb.errors import InternalError, NotAllowedError, NotFoundError, ThrownError, ValidationError
from websockets.exceptions import ConnectionClosedError

import surreal_orm_lite
from surreal_orm_lite import (
    AccessState,
    AnalyzerState,
    DatabaseIntrospector,
    Duration,
    EventState,
    FieldState,
    FunctionState,
    GeometryPoint,
    IndexState,
    ModelCodeGenerator,
    ParamState,
    SchemaIssue,
    SchemaState,
    SurrealDbAuthenticationError,
    SurrealDbConnectionError,
    SurrealDBConnectionManager,
    SurrealDbError,
    SurrealDbNotFoundError,
    TableState,
    generate_models_from_db,
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
    _R_TOO_COMPLEX,
)
from surreal_orm_lite.live import is_connection_failure, is_dead
from surreal_orm_lite.testing import embedded_database
from tests._proxy import CuttableProxy
from tests._readme_tables import (
    assert_embedded_rows,
    assert_server_header_sentence,
    assert_server_rows,
    assert_server_table,
)
from tests.conftest import orm_client
from tests.test_embedded import (
    _EMBEDDED_BEHAVIOUR,
    _cells,
    _normalised,
    _readme,
    _readme_part,
    _subsection,
    _table_rows,
    _tables,
)
from tests.test_model_generator import DOCSTRING, GOLDEN
from tests.test_query_cache import _bullets
from tests.test_time_travel import _SERVER_BEHAVIOUR

#: The 13 names spec §5.8 exports (AC-1).
_EXPORTS = (
    "DatabaseIntrospector",
    "ModelCodeGenerator",
    "generate_models_from_db",
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


#: What the server answers to an ``INFO FOR TABLE`` it cannot run: an SDK ``ServerError`` (3.x, 2.x, the embedded
#: engine), and the embedded engine's bare ``RuntimeError`` for a statement it cannot parse (measured with SDK 2.0.0).
_SERVER_ERRORS = [
    ValidationError("Validation", "Parse error: Unexpected token `WHERE`, expected Eof"),
    InternalError("Internal", "There was a problem with the database: Parse error: Unexpected token `WHERE`"),
    NotFoundError("NotFound", "The table 'a' does not exist"),
    ThrownError("Thrown", "An error occurred: boom"),
    RuntimeError("There was a problem with the database: Parse error: Unexpected token `WHERE`, expected Eof"),
]
_SERVER_ERROR_IDS = ["3.x-validation", "2.x-internal", "not-found", "thrown", "embedded-runtime"]

#: An ``INFO FOR TABLE`` the server never answered: the ORM's errors, and what the transports raise for a connection
#: lost after ``INFO FOR DB`` (``websockets``, ``aiohttp``, an ``OSError``, the SDK's ``KeyError`` for a request caught
#: by a drop).
_LOST_CONNECTION_ERRORS = [
    SurrealDbConnectionError("the connection dropped"),
    SurrealDbError("the ORM refused it"),
    ConnectionClosedError(None, None),
    aiohttp.ServerDisconnectedError(),
    ConnectionResetError(104, "Connection reset by peer"),
    KeyError("4f0c2a10-5d5e-4c1d-9d5b-1a2b3c4d5e6f"),
]
_LOST_CONNECTION_IDS = ["orm-connection", "orm-error", "websockets", "aiohttp", "os-error", "sdk-key-error"]


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
    """AC-1: the 13 names of spec §5.8 import from the package and from ``introspection``, with their signatures."""

    def test_names_are_exported(self) -> None:
        assert len(_EXPORTS) == 13
        for name in _EXPORTS:
            assert name in surreal_orm_lite.__all__, name
            assert name in introspection_module.__all__, name
            assert getattr(surreal_orm_lite, name) is getattr(introspection_module, name)
        assert sorted(introspection_module.__all__) == sorted(_EXPORTS)

    def test_the_names_import(self) -> None:
        namespace: dict[str, Any] = {}
        exec(f"from surreal_orm_lite import {', '.join(_EXPORTS)}", namespace)
        exec(f"from surreal_orm_lite.introspection import {', '.join(_EXPORTS)}", namespace)
        assert all(name in namespace for name in _EXPORTS)

    def test_generate_models_from_db_signature(self) -> None:
        assert inspect.iscoroutinefunction(generate_models_from_db)
        assert inspect.iscoroutinefunction(ModelCodeGenerator.generate) is False
        parameters = inspect.signature(generate_models_from_db).parameters
        assert [(p.name, p.kind, p.default) for p in parameters.values()] == [
            ("output_path", inspect.Parameter.POSITIONAL_OR_KEYWORD, None),
            ("connection", inspect.Parameter.POSITIONAL_OR_KEYWORD, None),
            ("tables", inspect.Parameter.KEYWORD_ONLY, None),
            ("overwrite", inspect.Parameter.KEYWORD_ONLY, True),
        ]
        assert parameters["overwrite"].default is True

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

    @pytest.mark.parametrize("error", _LOST_CONNECTION_ERRORS, ids=_LOST_CONNECTION_IDS)
    async def test_info_for_table_without_an_answer_propagates(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: BaseException
    ) -> None:
        """An ``INFO FOR TABLE`` the server did not answer — the connection dropped after ``INFO FOR DB``, an ORM
        error — propagates unchanged: no issue, no state, and no table read after it."""
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        client = _FakeClient(_db(tables={"a": _TABLE_A, "b": _TABLE_B}), errors={_info_table("a"): error})
        _install(monkeypatch, client)
        with pytest.raises(type(error)) as caught:
            await DatabaseIntrospector().introspect()
        assert caught.value is error
        assert client.statements == ["INFO FOR DB;", _info_table("a")]
        assert _issue_records(caplog) == []

    @pytest.mark.parametrize(
        "error",
        [
            NotAllowedError("NotAllowed", "Anonymous access not allowed: Not enough permissions to perform this action"),
            InternalError("Internal", "IAM error: Not enough permissions to perform this action"),
        ],
        ids=["3.x", "2.x"],
    )
    async def test_info_for_table_refused_after_info_for_db(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
    ) -> None:
        """A session that ran ``INFO FOR DB`` may run ``INFO FOR TABLE``: a refusal means it lost its rights meanwhile
        (its client was closed and reopened anonymously, its token expired), not that the table cannot be read."""
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        _named("isp_unit")
        client = _FakeClient(_db(tables={"a": _TABLE_A, "b": _TABLE_B}), errors={_info_table("a"): error})
        _install(monkeypatch, client, "isp_unit")
        with pytest.raises(SurrealDbAuthenticationError) as caught:
            await DatabaseIntrospector("isp_unit").introspect()
        assert str(caught.value) == (
            "Cannot read the schema of connection 'isp_unit': INFO FOR TABLE 'a' was refused after INFO FOR DB "
            f"succeeded; the session lost its rights while it was read. Server said: {error}"
        )
        assert caught.value.__cause__ is error
        assert client.statements == ["INFO FOR DB;", _info_table("a")]
        assert _issue_records(caplog) == []

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
            "event": "DEFINE EVENT e ON t WHEN true THEN CREATE x",
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

    async def test_i1_a_keyword_soup_is_refused_in_bounded_time(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Server text is not trusted to be small or kind: a definition whose clauses take more work to read than its
        # length allows is reported, kept, and the rest of the schema read.
        caplog.set_level(logging.DEBUG)
        statement = "DEFINE FIELD a ON t DEFAULT 's3cr3t-0042' " + "VALUE x ASSERT x " * 3000 + "+"
        t = _table(fields={"a": statement, "ok": "DEFINE FIELD ok ON t TYPE int PERMISSIONS FULL"})
        client = _FakeClient(_db(tables={"t": "DEFINE TABLE t TYPE NORMAL SCHEMAFULL PERMISSIONS NONE"}), {"t": t})
        _install(monkeypatch, client)
        started = time.perf_counter()
        schema = await DatabaseIntrospector().introspect()
        assert time.perf_counter() - started < 10.0  # unbounded, it took a quarter of a minute
        assert list(schema.tables["t"].fields) == ["ok"]
        (issue,) = schema.issues
        assert (issue.kind, issue.name, issue.table) == ("field", "a", "t")
        assert issue.message == _parse_failure(_R_TOO_COMPLEX)
        assert issue.statement == statement
        assert "s3cr3t-0042" not in repr(issue)
        assert "s3cr3t-0042" not in repr(schema)
        assert "s3cr3t-0042" not in caplog.text

    async def test_i1_a_regex_the_two_lines_close_apart(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # SurrealDB 3.3.4's renderings of regexes ending with an escaped backslash, which 2.x refuses: read with 2.x's
        # rule, the regex ran on to the end and swallowed the clauses after it, with no issue. Each one is reported.
        statements = {
            "has_bs": r"DEFINE FIELD has_bs ON t VALUE name = /\\/ PERMISSIONS FULL",
            "zip": (
                r"DEFINE FIELD zip ON t TYPE string ASSERT $value = /a\\/ PERMISSIONS FOR select WHERE $auth.admin = true,"
                " FOR create, update FULL"
            ),
            "cmt": r"DEFINE FIELD cmt ON t TYPE string ASSERT $value = /a\\/ COMMENT 'x' PERMISSIONS FULL",
        }
        t = _table(fields={**statements, "ok": "DEFINE FIELD ok ON t TYPE int PERMISSIONS FULL"})
        client = _FakeClient(_db(tables={"t": "DEFINE TABLE t TYPE NORMAL SCHEMAFULL PERMISSIONS NONE"}), {"t": t})
        _install(monkeypatch, client)
        schema = await DatabaseIntrospector().introspect()
        assert list(schema.tables["t"].fields) == ["ok"]
        assert [(i.kind, i.name, i.table, i.message, i.statement) for i in schema.issues] == [
            ("field", name, "t", _parse_failure(_R_AMBIGUOUS), statements[name]) for name in sorted(statements)
        ]
        source = ModelCodeGenerator().generate(schema)
        assert "computed(" not in source and "PERMISSIONS" not in source and "COMMENT" not in source
        for name in statements:
            assert f"- field 't.{name}': " in source

    async def test_i2_info_for_table_failing(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        error = InternalError("Internal", "A user generated conversion error occured")
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

    @pytest.mark.parametrize("error", _SERVER_ERRORS, ids=_SERVER_ERROR_IDS)
    async def test_i2_a_server_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
    ) -> None:
        """What the server answers to ``INFO FOR TABLE`` itself — an SDK ``ServerError`` on every line, a bare
        ``RuntimeError`` from the embedded engine for a statement it cannot parse — is an issue of that table only."""
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        client = _FakeClient(_db(tables={"a": _TABLE_A, "b": _TABLE_B}), errors={_info_table("a"): error})
        _install(monkeypatch, client)
        schema = await DatabaseIntrospector().introspect()
        assert client.statements == ["INFO FOR DB;", _info_table("a"), _info_table("b")]
        assert list(schema.tables) == ["a", "b"]
        assert schema.issues == (SchemaIssue("table", "a", "a", f"INFO FOR TABLE failed: {error}"),)
        assert [record.getMessage() for record in _issue_records(caplog)] == [
            f"Schema introspection: INFO FOR TABLE failed: {error} (table 'a')"
        ]

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
        client = _FakeClient(db, {"t": t}, errors={_info_table("u"): InternalError("Internal", "gone")})
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


#: A table whose generated source holds a non-ASCII alias, so the file tests see it written as UTF-8.
_GEN_TABLE = "DEFINE TABLE isp_gen TYPE NORMAL SCHEMAFULL PERMISSIONS NONE"
_GEN_FIELD = "DEFINE FIELD `café` ON isp_gen TYPE option<string> PERMISSIONS FULL"
_GEN_STATEMENTS = ["INFO FOR DB;", "INFO FOR TABLE `isp_gen`;"]


class _HookClient(_FakeClient):
    """A fake client that runs *hook* when it receives ``INFO FOR DB;`` — after the arguments and E8 are checked."""

    def __init__(self, hook: Callable[[], None]) -> None:
        super().__init__(_db(tables={"isp_gen": _GEN_TABLE}), tables={"isp_gen": _table(fields={"café": _GEN_FIELD})})
        self.hook = hook

    async def query(self, sql: str, vars: object = None, *args: object, **kwargs: object) -> object:
        if sql == "INFO FOR DB;":
            self.hook()
        return await super().query(sql, vars, *args, **kwargs)


def _gen_client() -> _FakeClient:
    return _HookClient(lambda: None)


def _e8(output_path: str | os.PathLike[str]) -> str:
    """E8 (spec §5.6) for *output_path*."""
    return f"{os.fspath(output_path)!r} already exists: overwrite=False refuses to replace it."


def _temporary_files(directory: pathlib.Path) -> list[str]:
    """The ``.<name>.<hex>.tmp`` files the atomic replacement may leave in *directory* (spec §5.3)."""
    return sorted(path.name for path in directory.iterdir() if path.name.endswith(".tmp"))


def _new_file_mode(directory: pathlib.Path) -> int:
    """The permission bits ``open()`` gives a new file in *directory* (the umask's)."""
    reference = directory / f"reference-{uuid.uuid4().hex}"
    with open(reference, "x", encoding="utf-8"):
        pass
    try:
        return stat.S_IMODE(reference.stat().st_mode)
    finally:
        reference.unlink()


def _failing_writes(monkeypatch: pytest.MonkeyPatch, name: str) -> OSError:
    """Make every ``write`` to a file named *name* or ``.<name>.*.tmp`` fail; return the error raised."""
    error = OSError("No space left on device")
    real_open = pathlib.Path.open

    def open_(self: pathlib.Path, *args: Any, **kwargs: Any) -> Any:
        file = real_open(self, *args, **kwargs)
        if self.name == name or (self.name.startswith(f".{name}.") and self.name.endswith(".tmp")):

            def write(text: str) -> int:
                raise error

            file.write = write
        return file

    monkeypatch.setattr(pathlib.Path, "open", open_)
    return error


class TestGenerateModelsFromDb:
    """Spec §5.3, AC-14: ``generate_models_from_db()`` returns the generated source; with ``output_path`` it writes it
    (UTF-8, ``"\\n"``), replacing an existing file atomically by default, refusing it with ``overwrite=False`` (E8,
    before any statement; then exclusive creation). Arguments are checked first (E9, R3, E5, E6, ``os.fspath``)."""

    async def test_returns_the_source_without_a_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
        monkeypatch.chdir(tmp_path)
        client = _gen_client()
        _install(monkeypatch, client)
        source = await generate_models_from_db()
        assert source == ModelCodeGenerator().generate(await DatabaseIntrospector().introspect())
        assert "class IspGen(BaseSurrealModel):" in source
        assert 'alias="café"' in source
        assert client.statements == _GEN_STATEMENTS * 2
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize("as_type", [str, pathlib.Path], ids=["str", "path"])
    async def test_writes_a_new_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, as_type: Callable[[pathlib.Path], object]
    ) -> None:
        _install(monkeypatch, _gen_client())
        path = tmp_path / "app" / "generated" / "models.py"
        source = await generate_models_from_db(as_type(path))  # type: ignore[arg-type]
        assert path.read_bytes() == source.encode("utf-8")
        assert source.endswith("\n") and "\r" not in source
        assert 'alias="café"' in source
        assert _temporary_files(path.parent) == []
        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == _new_file_mode(path.parent)

    async def test_replaces_an_existing_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
        _install(monkeypatch, _gen_client())
        path = tmp_path / "models.py"
        path.write_text("old\r\n", encoding="utf-8")
        if os.name == "posix":
            path.chmod(0o600)
        source = await generate_models_from_db(path)
        assert path.read_bytes() == source.encode("utf-8")
        assert sorted(p.name for p in tmp_path.iterdir()) == ["models.py"]
        if os.name == "posix":  # a new file's permissions, not the replaced one's
            assert stat.S_IMODE(path.stat().st_mode) == _new_file_mode(tmp_path)

    async def test_a_failing_replace_keeps_the_previous_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        _install(monkeypatch, _gen_client())
        path = tmp_path / "models.py"
        path.write_text("previous", encoding="utf-8")
        error = OSError("cannot replace")

        def replace(src: object, dst: object) -> None:
            raise error

        monkeypatch.setattr(os, "replace", replace)
        with pytest.raises(OSError) as caught:
            await generate_models_from_db(path)
        assert caught.value is error
        assert path.read_text(encoding="utf-8") == "previous"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["models.py"]

    @pytest.mark.parametrize("overwrite", [True, False])
    async def test_a_write_failure_leaves_no_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, overwrite: bool
    ) -> None:
        _install(monkeypatch, _gen_client())
        path = tmp_path / "models.py"
        if overwrite:
            path.write_text("previous", encoding="utf-8")
        error = _failing_writes(monkeypatch, "models.py")
        with pytest.raises(OSError) as caught:
            await generate_models_from_db(path, overwrite=overwrite)
        assert caught.value is error
        if overwrite:  # the previous file is intact, the temporary file removed
            assert sorted(p.name for p in tmp_path.iterdir()) == ["models.py"]
            assert path.read_text(encoding="utf-8") == "previous"
        else:  # the file this call created is removed
            assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize("overwrite", [True, False])
    @pytest.mark.parametrize("error", _LOST_CONNECTION_ERRORS[:3], ids=_LOST_CONNECTION_IDS[:3])
    async def test_a_lost_connection_leaves_the_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, error: BaseException, overwrite: bool
    ) -> None:
        """A connection lost after ``INFO FOR DB`` raises before anything is generated: an existing file is left byte
        for byte (it is not replaced by a module whose tables "could not be read"), and none is created."""
        client = _FakeClient(_db(tables={"isp_gen": _GEN_TABLE}), errors={_info_table("isp_gen"): error})
        _install(monkeypatch, client)
        path = tmp_path / "models.py"
        previous = b"# the working models\r\nclass IspGen: ...\n"
        if overwrite:
            path.write_bytes(previous)
        with pytest.raises(type(error)) as caught:
            await generate_models_from_db(path, overwrite=overwrite)
        assert caught.value is error
        assert client.statements == _GEN_STATEMENTS
        if overwrite:
            assert path.read_bytes() == previous
            assert sorted(p.name for p in tmp_path.iterdir()) == ["models.py"]
        else:
            assert list(tmp_path.iterdir()) == []

    @pytest.mark.skipif(os.name != "posix", reason="symbolic links (POSIX)")
    async def test_a_symlink_is_replaced_not_written_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        _install(monkeypatch, _gen_client())
        target = tmp_path / "target.py"
        target.write_text("target", encoding="utf-8")
        path = tmp_path / "models.py"
        path.symlink_to(target)
        source = await generate_models_from_db(path)
        assert not path.is_symlink()
        assert path.read_text(encoding="utf-8") == source
        assert target.read_text(encoding="utf-8") == "target"
        assert _temporary_files(tmp_path) == []

    @pytest.mark.parametrize("as_type", [str, pathlib.Path], ids=["str", "path"])
    async def test_overwrite_false_e8_before_any_statement(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, as_type: Callable[[pathlib.Path], object]
    ) -> None:
        client = _gen_client()
        _install(monkeypatch, client)
        path = tmp_path / "models.py"
        path.write_text("mine", encoding="utf-8")
        with pytest.raises(FileExistsError) as caught:
            await generate_models_from_db(as_type(path), overwrite=False)  # type: ignore[arg-type]
        assert str(caught.value) == _e8(path) == f"{str(path)!r} already exists: overwrite=False refuses to replace it."
        assert client.calls == []
        assert path.read_text(encoding="utf-8") == "mine"

    @pytest.mark.skipif(os.name != "posix", reason="symbolic links (POSIX)")
    async def test_overwrite_false_refuses_a_dangling_symlink(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        client = _gen_client()
        _install(monkeypatch, client)
        path = tmp_path / "models.py"
        path.symlink_to(tmp_path / "missing.py")
        with pytest.raises(FileExistsError) as caught:
            await generate_models_from_db(path, overwrite=False)
        assert str(caught.value) == _e8(path)
        assert client.calls == []
        assert not (tmp_path / "missing.py").exists()

    async def test_overwrite_false_writes_a_new_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
        _install(monkeypatch, _gen_client())
        path = tmp_path / "new" / "models.py"
        source = await generate_models_from_db(str(path), overwrite=False)
        assert path.read_bytes() == source.encode("utf-8")
        assert sorted(p.name for p in path.parent.iterdir()) == ["models.py"]

    async def test_overwrite_false_a_file_created_after_the_check(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        path = tmp_path / "models.py"
        client = _HookClient(lambda: path.write_text("theirs", encoding="utf-8"))
        _install(monkeypatch, client)
        with pytest.raises(FileExistsError) as caught:
            await generate_models_from_db(path, overwrite=False)
        assert str(caught.value) != _e8(path)  # the OS's, from mode "x"
        assert client.statements == _GEN_STATEMENTS
        assert path.read_text(encoding="utf-8") == "theirs"

    async def test_e9_before_e8_and_any_statement(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
        installed = _gen_client()
        _install(monkeypatch, installed)
        path = tmp_path / "models.py"
        path.write_text("mine", encoding="utf-8")
        client = _gen_client()
        with pytest.raises(TypeError) as caught:
            await generate_models_from_db(path, client, overwrite=False)  # type: ignore[arg-type]
        assert str(caught.value) == _E9
        assert client.calls == [] and installed.calls == []
        with pytest.raises(TypeError) as caught:
            await generate_models_from_db(connection=mock.MagicMock())
        assert str(caught.value) == _E9

    @pytest.mark.parametrize(
        ("arguments", "error", "message"),
        [
            ({"connection": 42}, TypeError, "Connection name must be a str, not int."),
            ({"connection": object()}, TypeError, "Connection name must be a str, not object."),
            ({"connection": ""}, ValueError, "Connection name must not be empty."),
            ({"tables": "person"}, TypeError, "tables= takes an iterable of table names, not a str: write tables=['person']."),
            ({"tables": ["a", 1]}, TypeError, "tables= takes table names (str), got int."),
            ({"output_path": 42, "connection": 42}, TypeError, "Connection name must be a str, not int."),
        ],
        ids=["r3-int", "r3-object", "r3-empty", "e5", "e6", "arguments-first"],
    )
    async def test_argument_errors_before_any_statement(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, arguments: dict[str, Any], error: type, message: str
    ) -> None:
        client = _gen_client()
        _install(monkeypatch, client)
        path = tmp_path / "models.py"
        path.write_text("mine", encoding="utf-8")
        with pytest.raises(error) as caught:
            await generate_models_from_db(**{"output_path": path, "overwrite": False, **arguments})
        assert str(caught.value) == message
        assert client.calls == []

    async def test_output_path_of_another_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _gen_client()
        _install(monkeypatch, client)
        with pytest.raises(TypeError) as caught:
            await generate_models_from_db(42)  # type: ignore[arg-type]
        with pytest.raises(TypeError) as expected:
            os.fspath(42)  # type: ignore[call-overload]
        assert str(caught.value) == str(expected.value)
        assert client.calls == []

    async def test_connection_and_tables_are_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _named("isp_unit")
        named = _FakeClient(_db(tables={"a": _TABLE_A, "b": _TABLE_B}))
        default = _FakeClient(_db())
        _install(monkeypatch, named, "isp_unit")
        _install(monkeypatch, default)
        source = await generate_models_from_db(None, "isp_unit", tables=["b"])
        assert named.statements == ["INFO FOR DB;", "INFO FOR TABLE `b`;"]
        assert "class B(BaseSurrealModel):" in source and "class A(" not in source
        assert 'connection="isp_unit"' in source
        named.calls.clear()
        async with SurrealDBConnectionManager.using("isp_unit"):
            assert await generate_models_from_db(tables=["b"]) == source
        assert named.statements == ["INFO FOR DB;", "INFO FOR TABLE `b`;"]
        assert default.calls == []


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

#: Tables with a change feed or permissions other than the default ``NONE`` — ``isp_owned`` has one ``WHERE`` rule
#: for the four operations, which SurrealDB renders ``FOR select, create, update, delete WHERE …``.
_FEED_DDL = """
DEFINE TABLE isp_feed SCHEMAFULL CHANGEFEED 1d INCLUDE ORIGINAL
    PERMISSIONS FOR select WHERE $auth.id = owner, FOR create, update FULL, FOR delete NONE;
DEFINE FIELD owner ON isp_feed TYPE option<record>;
DEFINE TABLE isp_open SCHEMALESS CHANGEFEED 3h PERMISSIONS FULL;
DEFINE TABLE isp_owned SCHEMAFULL PERMISSIONS FOR select, create, update, delete WHERE $auth.id = owner;
"""

#: The module generated from ``_FEED_DDL`` on SurrealDB 3.x, 2.x and the embedded engine.
_FEED_SOURCE = (
    DOCSTRING
    + """

from surreal_orm_lite import BaseSurrealModel, SchemaMode, SurrealConfigDict, TableType
from surrealdb import RecordID


class IspFeed(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_name="isp_feed",
        schema_mode=SchemaMode.SCHEMAFULL,
        changefeed="1d INCLUDE ORIGINAL",
        permissions={"select": "WHERE $auth.id = owner", "create": "FULL", "update": "FULL"},
        connection="isp",
    )

    id: str | None = None
    owner: RecordID | None = None


class IspOpen(BaseSurrealModel):
    # SCHEMALESS: fields not declared here are kept (extra="allow")
    model_config = SurrealConfigDict(
        table_name="isp_open",
        table_type=TableType.ANY,
        changefeed="3h",
        permissions={"select": "FULL", "create": "FULL", "update": "FULL", "delete": "FULL"},
        connection="isp",
        extra="allow",
    )

    id: str | None = None


class IspOwned(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_name="isp_owned",
        schema_mode=SchemaMode.SCHEMAFULL,
        permissions={
            "select": "WHERE $auth.id = owner",
            "create": "WHERE $auth.id = owner",
            "update": "WHERE $auth.id = owner",
            "delete": "WHERE $auth.id = owner",
        },
        connection="isp",
    )

    id: str | None = None
"""
)


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


def _cut_at(
    monkeypatch: pytest.MonkeyPatch, client: Any, proxy: CuttableProxy, statement: str, *, websocket: bool
) -> list[str]:
    """Record the statements sent through *client*, and cut its connection (new ones refused) right before
    *statement* — on a WebSocket, once the SDK has seen the socket close, so that the request fails as it is sent."""
    sent: list[str] = []
    real_query = client.query

    async def query(sql: str, *args: object, **kwargs: object) -> object:
        sent.append(sql)
        if sql == statement:
            proxy.refuse()
            proxy.cut()
            for _ in range(500):  # the SDK's receive task ends within milliseconds of the cut
                if not websocket or is_dead(client):
                    break
                await asyncio.sleep(0.01)
        return await real_query(sql, *args, **kwargs)

    monkeypatch.setattr(client, "query", query)
    return sent


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

    @pytest.mark.parametrize("kind", ["ws", "http", "mem"])
    async def test_i2_a_server_error_on_info_for_table(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """The server's own error for one ``INFO FOR TABLE`` — 3.x's ``ValidationError``, 2.x's ``InternalError``, the
        embedded engine's ``RuntimeError`` — is issue I2 of that table; the other tables are read."""
        async with _isp(kind) as client:
            statement = _info_table("isp_company")
            real_query = client.query

            async def query(sql: str, *args: object, **kwargs: object) -> object:
                return await real_query(sql.replace(";", " WHERE;") if sql == statement else sql, *args, **kwargs)

            monkeypatch.setattr(client, "query", query)
            schema = await DatabaseIntrospector("isp").introspect()
        assert list(schema.tables) == _FIXTURE_TABLES
        ((kind_, name, table, message),) = [(i.kind, i.name, i.table, i.message) for i in schema.issues]
        assert (kind_, name, table) == ("table", "isp_company", "isp_company")
        assert message.startswith("INFO FOR TABLE failed: ") and "WHERE" in message, message
        company = schema.tables["isp_company"]
        assert (company.comment, company.fields) == ("Companies", {})
        assert "email" in schema.tables["isp_person"].fields

    @pytest.mark.parametrize("call", ["introspect", "generate"])
    @pytest.mark.parametrize("kind", ["ws", "http"])
    async def test_a_connection_lost_after_info_for_db(
        self,
        kind: str,
        call: str,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The connection drops between ``INFO FOR DB`` and an ``INFO FOR TABLE`` (a proxy cuts it and refuses new
        ones): the transport's error propagates, no issue is recorded, and ``generate_models_from_db()`` leaves the
        existing file byte for byte."""
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        path = tmp_path / "models.py"
        previous = b"# the working models\r\nclass IspPerson: ...\n"
        path.write_bytes(previous)
        async with _isp(kind), CuttableProxy() as proxy:
            SurrealDBConnectionManager.add_connection(
                "isp_cut", url=proxy.url(kind), user="root", password="root", namespace="ns", database="isp_db"
            )
            try:
                client = await SurrealDBConnectionManager.get_client("isp_cut")
                sent = _cut_at(monkeypatch, client, proxy, _info_table("isp_likes"), websocket=kind == "ws")
                with pytest.raises(Exception) as caught:
                    if call == "introspect":
                        await DatabaseIntrospector("isp_cut").introspect()
                    else:
                        await generate_models_from_db(path, "isp_cut")
            finally:
                await SurrealDBConnectionManager.remove_connection("isp_cut")
        assert is_connection_failure(caught.value, None), repr(caught.value)
        assert sent == ["INFO FOR DB;", *map(_info_table, _FIXTURE_TABLES[:3])]
        assert path.read_bytes() == previous
        assert sorted(p.name for p in tmp_path.iterdir()) == ["models.py"]
        assert _issue_records(caplog) == []

    @pytest.mark.parametrize("kind", ["ws", "http", "mem"])
    async def test_changefeed_and_permissions_comments(self, kind: str) -> None:
        """A table's change feed and permissions are comments of its class, the same text on every line."""
        async with _isp(kind, _FEED_DDL):
            source = await generate_models_from_db(None, "isp")
        assert source == _FEED_SOURCE

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


#: M47: regular expressions and the graph wildcard, written as SurrealDB's documentation writes them (the
#: ``countrycode`` example puts ASSERT before VALUE; the servers render VALUE first).
_M47_DDL = """\
DEFINE TABLE isp_rx SCHEMALESS;
DEFINE FIELD countrycode ON isp_rx TYPE string ASSERT $value = /[A-Z]{3}/ VALUE $value OR $before OR 'GBR';
DEFINE FIELD zip ON isp_rx TYPE option<string> ASSERT $value = NONE OR $value = /^[0-9]{5}$/
    PERMISSIONS FOR select FULL, FOR create, update WHERE $auth.admin = true;
DEFINE FIELD code ON isp_rx TYPE string ASSERT $value = /^[a-z]+$/ COMMENT 'lower only';
DEFINE FIELD fans ON isp_rx VALUE <-isp_likes<-?;
DEFINE FIELD k ON isp_rx TYPE string DEFAULT 'x'
    PERMISSIONS FOR select WHERE $auth.email = /@corp$/, FOR create, update FULL;
DEFINE FIELD half ON isp_rx TYPE option<number> VALUE $value / 2 COMMENT 'a/b';
DEFINE TABLE isp_rview AS SELECT * FROM isp_rx WHERE code = /a/ PERMISSIONS FULL;
DEFINE PARAM $isp_re VALUE /^a+$/;
"""

_ADMIN_WRITES = {"select": "FULL", "create": "WHERE $auth.admin = true", "update": "WHERE $auth.admin = true"}
_TABLE_ACTIONS = ("select", "create", "update", "delete")


class TestRegularExpressionsE2E:
    """M47 on the line under test and on ``mem://``: a regex or the graph wildcard ends its expression, and the
    clauses after it — ``PERMISSIONS``, ``COMMENT`` — are read, with no issue."""

    @pytest.mark.parametrize("kind", ["ws", "mem"])
    async def test_clauses_after_a_regex_or_a_graph_wildcard(self, kind: str) -> None:
        async with _isp(kind, _M47_DDL):
            schema = await DatabaseIntrospector("isp", tables=["isp_rview", "isp_rx"]).introspect()
        assert schema.issues == ()
        fields = schema.tables["isp_rx"].fields
        _check(
            fields["countrycode"],
            value="$value OR $before OR 'GBR'",
            assertion="$value = /[A-Z]{3}/",
            comment=None,
            permissions=dict.fromkeys(("select", "create", "update"), "FULL"),
        )
        _check(
            fields["zip"],
            field_type="string",
            nullable=True,
            assertion="$value = NONE OR $value = /^[0-9]{5}$/",
            permissions=_ADMIN_WRITES,
        )
        _check(fields["code"], assertion="$value = /^[a-z]+$/", comment="lower only")
        _check(fields["fans"], value="<-isp_likes<-?", comment=None)
        _check(
            fields["k"],
            default="'x'",
            permissions={"select": "WHERE $auth.email = /@corp$/", "create": "FULL", "update": "FULL"},
        )
        _check(fields["half"], value="$value / 2", comment="a/b")
        view = schema.tables["isp_rview"]
        _check(view, view_query="SELECT * FROM isp_rx WHERE code = /a/", permissions=dict.fromkeys(_TABLE_ACTIONS, "FULL"))
        _check(schema.params["isp_re"], value="/^a+$/", permissions="FULL", comment=None)
        source = ModelCodeGenerator().generate(schema)
        assert '    k: str = "x"\n' in source
        assert '    fans: Computed[Any] = computed("<-isp_likes<-?")\n' in source
        assert "PERMISSIONS" not in source


#: Regexes that SurrealDB 2.x and 3.x close at different slashes (a backslash pair before a ``/``): each line refuses
#: the other's form, and the parser, which does not know the line, reports the definition (I1) rather than read a
#: clause into the regex. ``(line, item, statement, name, rendering)``; the 3.x renderings are 3.3.4's and 3.1.5's.
_SPLIT_REGEXES = [
    (
        3,
        "a regex ending with an escaped backslash",
        r"DEFINE FIELD has_bs ON isp_x VALUE name = /\\/;",
        "has_bs",
        r"DEFINE FIELD has_bs ON isp_x VALUE name = /\\/ PERMISSIONS FULL",
    ),
    (
        3,
        "a regex ending with an escaped backslash, before PERMISSIONS",
        r"DEFINE FIELD zip ON isp_x TYPE string ASSERT $value = /a\\/"
        " PERMISSIONS FOR select WHERE $auth.admin = true, FOR create, update FULL;",
        "zip",
        r"DEFINE FIELD zip ON isp_x TYPE string ASSERT $value = /a\\/"
        " PERMISSIONS FOR select WHERE $auth.admin = true, FOR create, update FULL",
    ),
    (
        3,
        "a regex ending with an escaped backslash, before COMMENT",
        r"DEFINE FIELD cmt ON isp_x TYPE string ASSERT $value = /a\\/ COMMENT 'x';",
        "cmt",
        r"DEFINE FIELD cmt ON isp_x TYPE string ASSERT $value = /a\\/ COMMENT 'x' PERMISSIONS FULL",
    ),
    (
        2,
        "a regex holding a backslash then an escaped slash",
        r"DEFINE FIELD two ON isp_x TYPE string ASSERT $value = /a\\/b/ COMMENT 'x';",
        "two",
        r"DEFINE FIELD two ON isp_x TYPE string ASSERT $value = /a\\/b/ COMMENT 'x' PERMISSIONS FULL",
    ),
]


class TestSplitRegularExpressionsE2E:
    """A regex the two lines close at different slashes, on the line that accepts it (``_require_ddl``: skipped on the
    other line, and the 3.x forms on ``mem://``): an I1 issue that keeps the statement, never a clause read into it."""

    @pytest.mark.parametrize("kind", ["ws", "mem"])
    @pytest.mark.parametrize(
        ("line", "item", "statement", "name", "rendering"), _SPLIT_REGEXES, ids=[row[3] for row in _SPLIT_REGEXES]
    )
    async def test_reported_not_read(self, kind: str, line: int, item: str, statement: str, name: str, rendering: str) -> None:
        async with _isp(kind, "DEFINE TABLE isp_x SCHEMALESS;") as client:
            await _require_ddl(client, statement, line=line, item=item)
            schema = await DatabaseIntrospector("isp", tables=["isp_x"]).introspect()
        assert schema.tables["isp_x"].fields == {}
        assert [(i.kind, i.name, i.table, i.message, i.statement) for i in schema.issues] == [
            ("field", name, "isp_x", _parse_failure(_R_AMBIGUOUS), rendering)
        ]
        source = ModelCodeGenerator().generate(schema)
        assert f"- field 'isp_x.{name}': " in source
        assert "computed(" not in source and "PERMISSIONS" not in source and "COMMENT" not in source


@contextlib.contextmanager
def _imported(path: pathlib.Path) -> Iterator[ModuleType]:
    """The module written at *path*, imported under a unique name; removed from ``sys.modules`` on exit."""
    name = f"isp_models_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(name, None)


async def _round_trip(models: Any) -> None:
    """AC-13 on the fixture's generated module: every field of ``IspPerson`` saved and read back, ``merge()``, an edge,
    the row the event writes."""
    company = await models.IspCompany(name="Acme").save()
    assert isinstance(company.id, RecordID) and company.id.table_name == "isp_company"

    person = await models.IspPerson(
        name="Ada",
        email="ADA@Example.COM",
        age=36,
        status="inactive",
        tags=["math", "engines"],
        address=models.IspPersonAddress(city="London"),
        meta={"k": 1, "nested": {"x": "y"}},
        location=GeometryPoint(-0.1276, 51.5072),
        trial=Duration.parse("1h30m"),
        balance=Decimal("12.50"),
        token=uuid.UUID("018f1f2e-0000-7000-8000-000000000001"),
        employer=company.id,
        first_name="Ada",
        class_="vip",
    ).save()
    assert person.email == "ada@example.com"  # VALUE string::lowercase($value), applied by the server
    assert isinstance(person.created, datetime) and isinstance(person.updated, datetime)  # hydrated by save()

    got = await models.IspPerson.objects().get(person.get_raw_id())
    assert got.get_raw_id() == person.get_raw_id()
    for field in models.IspPerson.model_fields:
        if field != "id":
            assert getattr(got, field) == getattr(person, field), field
    assert (got.first_name, got.class_, got.address.city) == ("Ada", "vip", "London")
    assert (got.trial, got.balance, got.employer) == (Duration.parse("1h30m"), Decimal("12.50"), company.id)
    assert got.location == GeometryPoint(-0.1276, 51.5072)
    assert got.model_dump(by_alias=True)["first-name"] == "Ada"

    await person.merge(name="Ada Lovelace")
    assert person.name == "Ada Lovelace"
    merged = await models.IspPerson.objects().get(person.get_raw_id())
    assert (merged.name, merged.created, merged.email) == ("Ada Lovelace", person.created, "ada@example.com")

    since = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    edge = await models.IspLikes(in_=person.id, out=company.id, since=since).save()
    assert isinstance(edge.id, RecordID) and edge.id.table_name == "isp_likes"
    stored = await models.IspLikes.objects().get(edge.get_raw_id())
    assert (stored.in_, stored.out, stored.since) == (person.id, company.id, since)

    logs = await models.IspLog.objects().all()
    assert [log.model_extra for log in logs] == [{"msg": "created"}]


class TestRoundTripE2E:
    """AC-9, AC-13, AC-18 on the line under test and on ``mem://``: the fixture's source is the §9.3 golden on every
    line, its generated module (written by ``generate_models_from_db()``) works against the database it describes, and a
    ``server_fields`` column needing an alias is hydrated by ``save()`` (M40)."""

    @pytest.mark.parametrize("kind", ["ws", "mem"])
    async def test_the_golden_source_ac9(self, kind: str) -> None:
        async with _isp(kind):
            source = ModelCodeGenerator().generate(await DatabaseIntrospector("isp").introspect())
        assert source == GOLDEN

    @pytest.mark.parametrize("kind", ["ws", "mem"])
    async def test_generated_models_ac13(self, kind: str, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "isp_models.py"
        async with _isp(kind):
            source = await generate_models_from_db(path, "isp")
            assert source == GOLDEN
            assert path.read_text(encoding="utf-8") == source
            with _imported(path) as models:
                assert models.IspPerson.get_server_fields() == frozenset({"created", "updated"})
                await _round_trip(models)

    @pytest.mark.parametrize("kind", ["ws", "mem"])
    async def test_server_fields_by_attribute_ac18(self, kind: str, tmp_path: pathlib.Path) -> None:
        ddl = (
            "DEFINE TABLE isp_sf SCHEMAFULL;\n"
            "DEFINE FIELD `created-at` ON isp_sf TYPE datetime DEFAULT time::now();\n"
            "DEFINE FIELD note ON isp_sf TYPE string;\n"
            "DEFINE TABLE `Select` SCHEMALESS;\n"
        )
        tables = ["Select", "isp_sf", "select"]
        path = tmp_path / "isp_sf_models.py"
        async with _isp(kind, FIXTURE_DDL + ddl):
            schema = await DatabaseIntrospector("isp", tables=tables).introspect()
            assert (list(schema.tables), schema.issues) == (tables, ())
            source = await generate_models_from_db(path, "isp", tables=tables)
            assert source == ModelCodeGenerator().generate(schema)
            assert [line for line in source.splitlines() if line.startswith("# - table")] == [
                "# - table 'Select': its name is a SurrealQL keyword the ORM cannot use as a table name",
                "# - table 'select': its name is a SurrealQL keyword the ORM cannot use as a table name",
            ]
            assert 'created_at: datetime | None = Field(default=None, alias="created-at")' in source
            with _imported(path) as models:
                model = models.IspSf
                assert model.get_server_fields() == frozenset({"created_at"})
                row = await model(note="n").save()
                assert isinstance(row.created_at, datetime)
                got = await model.objects().get(row.get_raw_id())
                assert (got.created_at, got.note) == (row.created_at, "n")


#: Spec AC-18b (D25 amended, M48): one column per kind of constant ``DEFAULT``, plus one the model does not hold.
_CONSTANT_DEFAULTS_DDL = """\
DEFINE TABLE isp_cd SCHEMAFULL;
DEFINE FIELD title ON isp_cd TYPE string;
DEFINE FIELD tags ON isp_cd TYPE array<string> DEFAULT [];
DEFINE FIELD nums ON isp_cd TYPE array<int> DEFAULT [1, 2];
DEFINE FIELD prefs ON isp_cd TYPE object DEFAULT {};
DEFINE FIELD prefs.theme ON isp_cd TYPE string DEFAULT 'light';
DEFINE FIELD ttl ON isp_cd TYPE duration DEFAULT 1h;
DEFINE FIELD due ON isp_cd TYPE datetime DEFAULT d'2030-01-01T00:00:00Z';
DEFINE FIELD token ON isp_cd TYPE uuid DEFAULT u'0190d3b4-0000-7000-8000-000000000000';
DEFINE FIELD created ON isp_cd TYPE datetime DEFAULT time::now();
"""
_CONSTANT_DEFAULT_LINES = [
    "    created: datetime | None = None",
    '    due: datetime = datetime.fromisoformat("2030-01-01T00:00:00Z")',
    "    nums: list[int] = Field(default_factory=lambda: [1, 2])",
    "    prefs: IspCdPrefs = Field(default_factory=IspCdPrefs)",
    "    tags: list[str] = Field(default_factory=list)",
    "    title: str",
    '    token: UUID = UUID("0190d3b4-0000-7000-8000-000000000000")',
    '    ttl: Duration = Field(default_factory=lambda: Duration.parse("1h"))',
]
_TOKEN = uuid.UUID("0190d3b4-0000-7000-8000-000000000000")


async def _row(client: Any, record: str) -> object:
    """The stored row of *record* without ``id`` and ``created`` (the column the model does not hold)."""
    rows = await client.query(f"SELECT * OMIT id, created FROM {record};", {})
    assert isinstance(rows, list) and len(rows) == 1, rows
    return rows[0]


class TestConstantDefaultsE2E:
    """AC-18b on the line under test and on ``mem://``: a constant ``DEFAULT`` copied into a typed Python default stores,
    when the caller sets nothing, exactly the row the server's own ``DEFAULT`` stores, and the caller's value otherwise;
    ``update()`` after ``save()`` passes; a ``DEFAULT`` expression stays in ``server_fields`` (M48)."""

    @pytest.mark.parametrize("kind", ["ws", "mem"])
    async def test_constant_defaults_ac18b(self, kind: str, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "isp_cd_models.py"
        async with _isp(kind, _CONSTANT_DEFAULTS_DDL) as client:
            source = await generate_models_from_db(path, "isp", tables=["isp_cd"])
            assert source.split("    id: str | None = None\n", 1)[1].splitlines() == _CONSTANT_DEFAULT_LINES
            assert 'class IspCdPrefs(BaseModel):\n    theme: str = "light"\n' in source
            assert "update() after save()" not in source
            with _imported(path) as models:
                model = models.IspCd
                assert model.get_server_fields() == frozenset({"created"})

                await client.query("CREATE isp_cd:raw SET title = 't';", {})
                saved = await model(id="py", title="t").save()
                assert await _row(client, "isp_cd:py") == await _row(client, "isp_cd:raw")
                assert isinstance(saved.created, datetime)  # in server_fields: the server's value, read back
                raw = await model.objects().get("raw")
                assert saved.model_dump(exclude={"id", "created"}) == raw.model_dump(exclude={"id", "created"})
                assert (saved.tags, saved.nums, saved.prefs.theme, saved.ttl, saved.token) == (
                    [],
                    [1, 2],
                    "light",
                    Duration.parse("1h"),
                    _TOKEN,
                )
                assert saved.due == datetime(2030, 1, 1, tzinfo=UTC)
                saved.title = "u"
                await saved.update()
                assert (await model.objects().get("py")).title == "u"

                values = {
                    "tags": ["a"],
                    "nums": [3],
                    "ttl": Duration.parse("5m"),
                    "due": datetime(2031, 2, 3, 4, 5, 6, tzinfo=UTC),
                    "token": uuid.UUID("0190d3b4-0000-7000-8000-000000000001"),
                }
                explicit = await model(id="ex", title="t", prefs=models.IspCdPrefs(theme="dark"), **values).save()
                got = await model.objects().get("ex")
                for name, value in values.items():
                    assert getattr(explicit, name) == getattr(got, name) == value, name
                assert explicit.prefs.theme == got.prefs.theme == "dark"
                explicit.title = "v"
                await explicit.update()
                assert (await model.objects().get("ex")).nums == [3]


#: Spec AC-18c (M48): a required and an optional sub-field filled by a ``DEFAULT`` expression on create.
_SUB_FIELD_DEFAULTS_DDL = """\
DEFINE TABLE isp_sfd SCHEMAFULL;
DEFINE FIELD title ON isp_sfd TYPE string;
DEFINE FIELD prefs ON isp_sfd TYPE object;
DEFINE FIELD prefs.theme ON isp_sfd TYPE string DEFAULT 'light';
DEFINE FIELD prefs.at ON isp_sfd TYPE datetime DEFAULT time::now();
DEFINE TABLE isp_sfo SCHEMAFULL;
DEFINE FIELD title ON isp_sfo TYPE string;
DEFINE FIELD prefs ON isp_sfo TYPE object;
DEFINE FIELD prefs.seen ON isp_sfo TYPE option<datetime> DEFAULT time::now();
"""


async def _stored_at(client: Any, record: RecordID, column: str) -> list[object]:
    """``prefs.<column>`` of *record* as the SDK reads it, then as the server writes it (nanoseconds included)."""
    query = f"SELECT VALUE [prefs.{column}, <string> prefs.{column}] FROM ONLY $rid;"  # column: a constant of the test
    values = await client.query(query, {"rid": record})
    assert isinstance(values, list) and len(values) == 2, values
    return values


async def _save_how(instance: Any, how: str) -> Any:
    """``instance.save()``, or ``save(tx=)`` in an interactive transaction on the connection ``"isp"``."""
    if how != "tx":
        return await instance.save()
    async with SurrealDBConnectionManager.transaction("isp") as tx:
        if not tx.is_interactive:
            pytest.skip("a buffered transaction returns no row before commit: save() keeps the instance's values")
        return await instance.save(tx=tx)


class TestSubFieldDefaultsE2E:
    """AC-18c on the line under test and on ``mem://`` (M48, §14.9), inverted by #261: ``save()`` applies the row the
    server returns, so a sub-field a ``DEFAULT`` fills on create holds the server's value right after it — with an
    explicit id, an auto id and in an interactive transaction — and a following ``update()`` keeps it, required or
    optional, to the nanosecond. Before #261 only the explicit-id ``save()`` (directly or in an interactive
    transaction) left it ``None``; the auto-id one already applied the whole row. The generated classes no longer
    carry a comment about it."""

    @pytest.mark.parametrize("kind", ["ws", "mem"])
    @pytest.mark.parametrize("how", ["explicit-id", "auto-id", "tx"])
    async def test_update_after_save_ac18c(self, kind: str, how: str, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "isp_sfd_models.py"
        async with _isp(kind, _SUB_FIELD_DEFAULTS_DDL) as client:
            source = await generate_models_from_db(path, "isp", tables=["isp_sfd", "isp_sfo"])
            assert "update() after save()" not in source
            for name in ("IspSfd", "IspSfo"):
                block = source.split(f"class {name}(BaseSurrealModel):\n", 1)[1]
                assert block.splitlines()[0].startswith("    model_config = "), name
            with _imported(path) as models:
                for model, prefs, column in (
                    (models.IspSfd, models.IspSfdPrefs, "at"),  # required
                    (models.IspSfo, models.IspSfoPrefs, "seen"),  # optional
                ):
                    instance = model(id=None if how == "auto-id" else "x", title="t", prefs=prefs())
                    saved = await _save_how(instance, how)
                    record = saved._record_id()
                    assert record is not None and record.table_name == model.get_table_name()
                    stored, written = await _stored_at(client, record, column)
                    assert isinstance(stored, datetime), column
                    assert getattr(saved.prefs, column) == stored, column
                    saved.title = "u"
                    await saved.update()
                    assert await _stored_at(client, record, column) == [stored, written], column
                    assert (await model.objects().get(record.id)).title == "u"


# --- AC-16: README section 31, the tables it touches and its examples (spec §6.5, §11.1 items 1–8; D18, D26) --------

_SECTION_30 = "### 30. Multiple databases (`add_connection()`, `using()`)"
_SECTION_31 = "### 31. Schema introspection (`DatabaseIntrospector`, `generate_models_from_db()`)"
#: Spec §11.1 item 1: section 31's subsections, in order, after an untitled intro paragraph.
_SUBSECTIONS_31 = [
    "Reading the schema",
    "Generating models",
    "How SurrealDB kinds become fields",
    "What the generator does not do",
    "SurrealDB 2.x and 3.x",
    "Permissions",
    "Coming from the full SurrealDB-ORM",
]
#: Spec §11.1 item 1: the block that opens "Reading the schema", exactly (it runs on ``mem://`` as written).
_READ_EXAMPLE = """\
from surreal_orm_lite import DatabaseIntrospector, SurrealDBConnectionManager

SurrealDBConnectionManager.set_connection(url="mem://", namespace="shop", database="main")
client = await SurrealDBConnectionManager.get_client()
await client.query(
    "DEFINE TABLE person SCHEMAFULL;"
    "DEFINE FIELD name ON person TYPE string;"
    "DEFINE FIELD age ON person TYPE option<int> DEFAULT 18;"
    "DEFINE FIELD address ON person TYPE option<object>;"
    "DEFINE FIELD address.city ON person TYPE string;"
    "DEFINE INDEX person_name ON person FIELDS name UNIQUE;",
    {},
)

schema = await DatabaseIntrospector().introspect()
person = schema.tables["person"]
person.schema_mode                    # 'SCHEMAFULL'
person.fields["age"].field_type       # 'int'
person.fields["age"].nullable         # True: option<int> on SurrealDB 2.x, none | int on 3.x
person.fields["age"].default          # '18': SurrealQL text
person.fields["address.city"].path    # ('address', 'city')
person.indexes["person_name"].unique  # True
schema.issues                         # (): everything was read and parsed
"""
#: Spec §11.1 item 1: the block that opens "Generating models", exactly; the source it prints follows it.
_GENERATE_EXAMPLE = """\
from surreal_orm_lite import ModelCodeGenerator, generate_models_from_db

source = ModelCodeGenerator().generate(schema)          # the same text as: await generate_models_from_db()
print(source)
await generate_models_from_db("models_generated.py")    # writes the file, replacing it if it exists (as the full ORM)
try:
    await generate_models_from_db("models_generated.py", overwrite=False)
except FileExistsError as exc:
    print(exc)  # 'models_generated.py' already exists: overwrite=False refuses to replace it.
"""
#: What the generate example prints after the source: E8's text for its ``overwrite=False`` call.
_E8_PRINTED = "'models_generated.py' already exists: overwrite=False refuses to replace it.\n"
#: The names section 31's examples state a result for (``<expression>  # <repr>[: note]``), by block.
_STATED_31 = [
    "person.schema_mode",
    'person.fields["age"].field_type',
    'person.fields["age"].nullable',
    'person.fields["age"].default',
    'person.fields["address.city"].path',
    'person.indexes["person_name"].unique',
    "schema.issues",
    "archive.connection",
]
#: Spec §11.1 item 7 (P1, P2; D25 amended): the 18 "Coming from the full SurrealDB-ORM" bullets, exactly, and no 19th.
_COMING_FROM_31 = [
    "- **Same names and call shapes**: `DatabaseIntrospector(connection).introspect()`, "
    "`ModelCodeGenerator().generate(state)`, `generate_models_from_db(output_path, connection)`, and the state "
    "classes `SchemaState`, `TableState`, `FieldState`, `IndexState`, `EventState`, `AccessState` and "
    "`AnalyzerState`, built by position or by keyword with the full ORM's arguments. Lite attributes (`raw`, `path`, "
    "`table`, `comment`, …) come after them and are keyword-only. `FunctionState`, `ParamState`, `SchemaIssue` and "
    "`tables=` are lite additions.",
    "- **State is immutable**: frozen dataclasses with read-only maps, and every definition keeps the server's "
    "statement in `raw` (full ORM: mutable dataclasses, no statement).",
    "- **Sequences are tuples**: compare `index.fields`, `analyzer.tokenizers`, `analyzer.filters`, "
    '`table.relation_in` and `table.relation_out` with tuples (`index.fields == ("email",)`); lists are accepted when '
    'constructing, and so is the full ORM\'s pipe-joined `relation_in="person | company"`. Relation ends are `()` '
    "where the full ORM has `None` (not a relation table, or any table).",
    "- **Permission values are `'FULL'`, `'NONE'` or `'WHERE <expr>'`** (full ORM: the bare condition).",
    "- **`default` is SurrealQL text**: `'18'`, `\"'active'\"`, `'time::now()'`, with `default_always` for `DEFAULT "
    "ALWAYS` (full ORM: a Python value, where `'active'` and `time::now()` both become strings). A `FieldState` built "
    "with a `default` that is not text raises `TypeError`.",
    "- **Typed defaults and `server_fields`**: a constant `DEFAULT` becomes a typed Python default "
    "(`Field(default_factory=list)`, `Duration.parse(…)`, `datetime.fromisoformat(…)`, `UUID(…)`), and any other "
    "`DEFAULT` becomes a `server_fields` entry (full ORM: `Field(default=…)` holding the SurrealQL text as a Python "
    "string, such as `'[]'` or `'time::now()'`, and no `server_fields`).",
    '- **`nullable` means "may be NONE"**: it is true for an optional kind (`option<T>`, rendered `none | T` by '
    'SurrealDB 3.x); `null | T` is not nullable, since it refuses NONE (full ORM: nullable). `field_type` is `"any"` '
    "for a field without `TYPE`, as in the full ORM.",
    "- **Accesses are database-level**: they are in `schema.accesses`, and `TableState` has no `access` attribute; "
    'find a table\'s with `[a for a in schema.accesses.values() if a.table == "user"]`. `table`, `signup_fields` and '
    "`signin_where` are read from `SIGNUP (CREATE <table> SET …)` (also `CREATE type::thing('<table>', …) SET …` and "
    "`CREATE type::record('<table>', …) SET …`, the forms `define_access()` uses for a `primary_key`) and `SIGNIN "
    "(SELECT … FROM <table> WHERE …)`. The full ORM reads the first form only, and another shape leaves them empty, "
    "with field names unescaped; durations are kept as rendered, `'NONE'` included.",
    "- **`encrypted` is not provided**: the lite has no `Encrypted` type, so a hashing `VALUE` "
    "(`crypto::argon2::generate($value)`) stays in `FieldState.value` and the field is generated as a writable field.",
    '- **APIs and the GraphQL config are not typed**: their definitions are in `SchemaState.other["apis"]` and '
    '`SchemaState.other["configs"]`, as SurrealDB returns them (full ORM: `ApiState`, `GraphQLConfigState`).',
    "- **`SchemaState.diff()` and `schema_diff()` come with v0.38.0** (migrations), as does `applied_migrations`; the "
    "`inspectdb` command comes with v0.39.0.",
    "- **A refused or failed `INFO FOR DB` raises** `SurrealDbAuthenticationError` or `SurrealDbError`, and a "
    "definition that cannot be read or parsed becomes a `SchemaIssue` in `schema.issues` and a logged warning (full "
    "ORM: an empty state, and definitions dropped with a debug log).",
    "- **`on_delete` keeps `'THEN <expr>'`** for `REFERENCE ON DELETE THEN …` (full ORM: `'THEN'` alone).",
    "- **`connection=` is a name**: the name a connection was added with (`add_connection()`), else the current one "
    '(`using()`, then `"default"`). A client object is refused with a `TypeError`, because a client does not carry '
    "the ORM's connection name, which routing, reconnects and the generated models' `connection=` key need (full ORM: "
    "an `HTTPConnection`).",
    "- **Generated models use comments** where the full ORM writes `SurrealConfigDict(permissions=…, changefeed=…)` "
    "(`# Permissions: …`, `# Changefeed: …`): those keys arrive in the lite's `SurrealConfigDict` with later minors. A "
    "view is generated with `view_query=…` since v0.32.0, and a relation table's ends with `relation_in=…`, "
    "`relation_out=…` and `enforced=True` since v0.33.0, as in the full ORM (a `# View: AS …` comment when the lite "
    "cannot declare the view: an edge, a computed field, a query it refuses; an `# IN … OUT …` comment when a table "
    "name is not an identifier). The base class says what the full ORM's `table_type=` says (`BaseSurrealEdge` for a "
    "relation table). `READONLY` is a comment too (full ORM: `Field(readonly=True)`).",
    "- **`RecordID` instead of `ForeignKey`/`ReferencesField`**; no `VectorField` or `Encrypted`; `computed()` "
    "instead of `Computed()`; `Duration`, `UUID`, `Decimal` and the geometry classes instead of `str`, `dict` and "
    "`float`; nested fields become sub-models (full ORM: dotted attribute lines).",
    "- **No singularisation**: `game_tables` becomes `GameTables` (full ORM: a trailing `s` is stripped, so `status` "
    "becomes `Statu`).",
    "- **`overwrite=False` added**: `generate_models_from_db(path)` replaces an existing file as the full ORM does, "
    "here atomically; `overwrite=False` refuses it with `FileExistsError`.",
]
_PERMISSIONS_31 = [
    "Introspection needs a system user: root, a namespace user or a database user, any role. A record user or an "
    'anonymous session gets `SurrealDbAuthenticationError` ("this session may not run INFO FOR DB"); on the embedded '
    "engine the anonymous session owns the database and may read it.",
    "The `users` section is never read. On SurrealDB before 3.2.2 (every 2.x release, 3.x before 3.2.2, and the "
    "embedded engine of SDK 2.0.0), `INFO FOR DB` shows user password hashes to every role that may run `INFO` "
    "([SurrealDB's `INFO` documentation](https://surrealdb.com/docs/surrealql/statements/info)); introspection never "
    "reads, stores, logs or writes them.",
]

#: Spec §11.1 item 7: one key phrase per bullet, in order (AC-16).
_COMING_FROM_PHRASES_31 = (
    "Same names and call shapes",
    "State is immutable",
    "Sequences are tuples",
    "'WHERE <expr>'",
    "`default` is SurrealQL text",
    "Typed defaults and `server_fields`",
    "may be NONE",
    "Accesses are database-level",
    "`encrypted` is not provided",
    "APIs and the GraphQL config are not typed",
    "come with v0.38.0",
    "`INFO FOR DB` raises",
    "`on_delete` keeps",
    "`connection=` is a name",
    "Generated models use comments",
    "instead of `ForeignKey`/`ReferencesField`",
    "No singularisation",
    "`overwrite=False` added",
)
#: Spec §11.1 item 1b (D25 amended, M48): the ``DEFAULT`` bullet of "How SurrealDB kinds become fields", and the
#: ``update()`` bullet of "What the generator does not do" as #261 narrowed it (the sub-field bullet is gone), exactly
#: (whitespace normalised).
_DEFAULT_BULLET_31 = (
    "- **`DEFAULT`**: a constant the model can hold exactly becomes the Python default, so the database stores the same "
    "value whether you set the field or not, and stores yours when you do — `DEFAULT 18` → `= 18`, `'active'` → "
    '`= "active"`, `true` → `= True`, `0.5f` on a `float` → `= 0.5`, `1.5dec` → `= Decimal("1.5")`, `NONE` → `= None`, '
    "`[]` → `= Field(default_factory=list)` (`set` for a set), `[1, 2]` → `= Field(default_factory=lambda: [1, 2])`, "
    "`{}` → `= Field(default_factory=dict)` or the sub-model's class, `1h30m` → "
    "`= Field(default_factory=lambda: Duration.parse(\"1h30m\"))`, `d'2030-01-01T00:00:00Z'` → "
    '`= datetime.fromisoformat("2030-01-01T00:00:00Z")`, `u\'…\'` → `= UUID("…")`. The value is copied when the models '
    "are generated: generate them again after changing a `DEFAULT`. A `{}` whose sub-model needs a value makes the "
    "field required, because SurrealDB refuses a create that leaves it to that default. Any other default — an "
    "expression such as `time::now()`, a record id, `math::INF`, a non-empty object, a datetime with nanoseconds, a "
    "literal of another type — makes the field `T | None = None` and lists its attribute in `server_fields` "
    "([section 20](#20-field-aliases-server_fields-and-mergerefreshfalse)): omitted on create, so that the server's "
    "default applies, and read back after `save()`. A value you set on such a field before `save()` is not written: "
    "set it with `merge()` once the record exists."
)
_UPDATE_BULLET_31 = (
    "- `update()` may fail on an instance holding a `READONLY` datetime below the top level, in a sub-model "
    "(`address.at`), a `tuple`, an array (`array<datetime>`, `items.*.at`) or a set, after `get()` as after `save()`: "
    "Python keeps microseconds, SurrealDB nanoseconds, and the server refuses the changed value. `update()` keeps the "
    "top-level `READONLY` datetimes to the nanosecond, up to 32 per record; below the top level it keeps one only while "
    "the fields before it leave room, which depends on the data (see [Precision and `Decimal`](#precision-and-decimal)). "
    "The generated class says so, as does a class with more than 32 top-level fields that may hold a datetime: change "
    "such a record with `merge()`."
)
#: Spec §11.1 item 8 (D18): the key phrases and the link AC-16 names.
_PERMISSIONS_PHRASES_31 = ("never read", "before 3.2.2", "never reads, stores, logs or writes them")
_INFO_DOCUMENTATION = "https://surrealdb.com/docs/surrealql/statements/info"
#: Spec §11.1 items 2–4: the features row, the roadmap split and the comparison row.
_FEATURES_ROW_31 = ["Schema introspection (`DatabaseIntrospector`, `generate_models_from_db()`)", "✅"]
_ROADMAP_ROWS_31 = [
    ["v0.29.0", "Tier 2 — Multi-database", "✅ Released"],
    ["v0.30.0", "Tier 3 — Schema introspection", "✅ Released"],
    ["v0.31.0", "Tier 3 — Server-side events and DDL helpers", "✅ Released"],
    ["v0.32.0", "Tier 3 — Materialized views", "✅ Released"],
]
_COMPARISON_ROW_31 = ["Schema Introspection", "✅ v0.30.0 (`DatabaseIntrospector`, `generate_models_from_db()`)", "✅"]
#: Spec §11.1 item 5: the six rows v0.30.0 adds to the 2.x/3.x table, cell by cell, then the row of a regex the two
#: lines close at different slashes; and the header's new sentence.
_INTROSPECTION_ROWS = [
    [
        "Schema introspection (`DatabaseIntrospector`, `generate_models_from_db()`)",
        "renders `option<T>`, array items as `tags[*]`, `FLEXIBLE` before `TYPE`, IF expressions bare: kinds and field "
        "paths are normalised to the 3.x forms",
        "renders optional kinds with `none` first, items as `tags.*`, `FLEXIBLE` after `TYPE`, IF expressions in "
        "parentheses: same state and model types; expressions kept as rendered",
        "v0.30.0",
    ],
    [
        "Definitions only one line has (schema introspection)",
        "`MTREE` and `SEARCH ANALYZER` indexes, `geometry<feature>`: read and typed",
        "`COMPUTED`, `REFERENCE`, `FULLTEXT`, kind `file`, `WITH REFRESH`, `BEARER`: read and typed; `DEFINE SEQUENCE`: "
        "kept raw",
        "v0.30.0",
    ],
    [
        "`INFO` (schema introspection) by a record user or an anonymous session",
        "refused: `SurrealDbAuthenticationError` (the SDK raises `InternalError`)",
        "refused: `SurrealDbAuthenticationError` (the SDK raises `NotAllowedError`)",
        "v0.30.0",
    ],
    [
        "`array<T, N>` read by schema introspection",
        "at most N items; the generated `list[T]` leaves the check to the server",
        "exactly N items; the generated `list[T]` leaves the check to the server",
        "v0.30.0",
    ],
    [
        "Schema introspection of a database removed while the connection is open",
        "empty schema",
        "`SurrealDbError` (the database does not exist) until the database is defined again",
        "v0.30.0",
    ],
    ["DDL not yet committed in a transaction, seen by schema introspection", "invisible", "invisible", "v0.30.0"],
    [
        "A regex with a backslash pair before a `/` (schema introspection)",
        "`/a\\\\/b/` accepted (a backslash, then an escaped slash), `/\\\\/` refused: such a definition is a `SchemaIssue`, "
        "not read",
        "`/\\\\/` accepted (an escaped backslash, then the closing slash), `/a\\\\/b/` refused: such a definition is a "
        "`SchemaIssue`, not read",
        "v0.30.0",
    ],
]
_HEADER_SENTENCE_30 = (
    "Rows added in v0.30.0 were measured on 2.7.0, 3.3.4 and the embedded engine, with 3.1.5 where it differs."
)
#: Spec §11.1 item 6: the row v0.30.0 adds to the embedded-engine table, cell by cell.
_EMBEDDED_INTROSPECTION_ROW = [
    "Schema introspection (`DatabaseIntrospector`)",
    "reads like a 2.x server (core 2.3.10): same state and same generated model types as on the servers; the anonymous "
    "owner session may run `INFO`; `COUNT` indexes cannot be defined",
    "need a system user (root, namespace or database user)",
    "v0.30.0",
]
#: Spec §11.1 item 1: what each subsection holds, by the names it must mention.
_PART_NEEDLES_31 = {
    "Reading the schema": (
        "`INFO FOR DB;`",
        "`INFO FOR TABLE`",
        "`schema.accesses`",
        "`schema.analyzers`",
        "`schema.functions`",
        "`schema.params`",
        "`schema.other`",
        "`users`",
        "`raw`",
        "`repr()`",
        "`SchemaIssue`",
        "`surreal_orm_lite.introspection`",
        "`SurrealDbAuthenticationError`",
        "`SurrealDbNotFoundError`",
        "`tables=`",
        "an empty list reads none",
        "`connection=`",
        "`using()`",
        "client object",
        "`TypeError`",
    ),
    "Generating models": (
        "`generate_models_from_db(output_path=None, connection=None, *, tables=None, overwrite=True)`",
        "atomically",
        "`os.replace()`",
        "`overwrite=False`",
        "`FileExistsError`",
        "`BaseSurrealEdge`",
        "`GameTables`",
        "`RangeModel`",
    ),
    "How SurrealDB kinds become fields": (
        "`option<T>`",
        "`PointField = None`",
        "sub-model",
        "`FLEXIBLE`",
        "`server_fields`",
        "`time::now()`",
        'Decimal("1.5")',
        "Computed[T]",
        "`computed()` refuses",
        "`$value`",
        "`= Field(default_factory=list)`",
        "`= datetime.fromisoformat(",
        "generate them again after changing a `DEFAULT`",
        "`merge()` once the record exists",
        'extra="allow"',
        "`Field(alias=",
        "`datetime_`",
        "`select`",
        "`Select`",
        "not an identifier",
    ),
    "What the generator does not do": (
        "`ASSERT`",
        "`PERMISSIONS`",
        "`# READONLY`",
        "`update()`",
        "`merge()`",
        "below the top level",
        "`array<T, N>`",
        "`null | T`",
        "`define_computed_fields()`",
        "128 columns",
        "`ruff format`",
        "`Literal`",
    ),
    "SurrealDB 2.x and 3.x": (
        "`option<int>`",
        "`none | int`",
        "`tags[*]`",
        "`tags.*`",
        "`FLEXIBLE TYPE object`",
        "`TYPE object FLEXIBLE`",
        "`VALUE IF b THEN 1 ELSE 2 END`",
        "`VALUE (IF b THEN 1 ELSE 2 END)`",
        "`COMPUTED`",
        "`MTREE`",
        "`DEFINE SEQUENCE`",
        "`transaction()`",
        "`REMOVE DATABASE`",
        "`SurrealDbError`",
        "empty schema",
    ),
}


def _section_31() -> str:
    return _readme_part(_SECTION_31)


def _part_31(heading: str) -> str:
    """One ``####`` subsection of section 31, as written."""
    return _subsection(f"#### {heading}", _section_31())


def _python_blocks_31() -> list[str]:
    """Section 31's ``python`` blocks, in order."""
    blocks: list[str] = re.findall(r"^```python\n(.*?)^```$", _section_31(), re.MULTILINE | re.DOTALL)
    assert blocks, "README section 31 has no example"
    return blocks


def _printed_index(blocks: list[str]) -> int:
    """The index of the block showing what the generate example prints: the one right after that example."""
    return blocks.index(_GENERATE_EXAMPLE) + 1


def _stated(block: str) -> list[tuple[str, str]]:
    """``(expression, comment)`` for each top-level line of *block* that is a bare expression stating its result in a
    trailing comment (``person.schema_mode  # 'SCHEMAFULL'``)."""
    stated = []
    for expression, comment in re.findall(r"^(\S[^#\n]*?) {2,}# (.+)$", block, re.MULTILINE):
        try:
            compile(expression, "<README section 31>", "eval")
        except SyntaxError:
            continue  # an assignment, or a statement
        if not expression.startswith(("await ", "print(")):
            stated.append((expression, comment))
    return stated


class TestReadmeSection31:
    """AC-15, AC-16: README section 31 — its place, subsections, examples (run on ``mem://`` as written), the
    ``Permissions`` and "Coming from the full SurrealDB-ORM" texts of spec §11.1 items 7–8 — and the features,
    roadmap, comparison and both behaviour tables (spec §11.1 items 2–6)."""

    def test_section_31_follows_section_30(self) -> None:
        """Right after section 30 (after its "Coming from" bullets), then section 32 (v0.31.0), section 33
        (v0.32.0) and section 34 (v0.33.0), and before the ``---`` of "Configuration Options"."""
        readme = _readme()
        start_30 = readme.index(f"\n{_SECTION_30}\n")
        start_31 = readme.index(f"\n{_SECTION_31}\n")
        configuration = readme.index("\n## Configuration Options\n")
        assert start_30 < start_31 < configuration
        assert re.findall(r"^#{2,3} .*$", readme[start_30 + 1 : configuration], re.MULTILINE) == [
            _SECTION_30,
            _SECTION_31,
            "### 32. Server-side events and stored functions (`Event`, `define_events()`, `define_function()`)",
            "### 33. Materialized views (`view_query`, `define_view()`)",
            "### 34. Relation constraints (`relation_in`, `define_relation()`)",
        ]
        assert readme[:configuration].rstrip().endswith("\n---"), "the last section ends before the --- of the next part"

    def test_subsections_in_order(self) -> None:
        assert re.findall(r"^#### (.*)$", _section_31(), re.MULTILINE) == _SUBSECTIONS_31

    def test_the_spec_examples_open_their_subsections(self) -> None:
        """Spec §11.1 item 1: each example exactly, first in its subsection; the printed source right after the
        generate example; ``python`` fences only."""
        assert _python_blocks_31()[0] == _READ_EXAMPLE
        assert re.findall(r"^```python\n(.*?)^```$", _part_31(_SUBSECTIONS_31[0]), re.MULTILINE | re.DOTALL)[0] == (
            _READ_EXAMPLE
        )
        generating = re.findall(r"^```python\n(.*?)^```$", _part_31(_SUBSECTIONS_31[1]), re.MULTILINE | re.DOTALL)
        assert generating[0] == _GENERATE_EXAMPLE
        assert generating[1].startswith('"""Models generated by surreal_orm_lite.generate_models_from_db()'), generating
        fences = re.findall(r"^```(\S*)$", _section_31(), re.MULTILINE)
        assert fences[1::2] == [""] * (len(fences) // 2) and set(fences[0::2]) == {"python"}, fences
        assert "mem://" in _READ_EXAMPLE.splitlines()[2], "the first example says it runs on mem://"

    def test_intro_paragraph(self) -> None:
        """Read-only, the two INFO statements, a system user; one shape on both lines, expressions as rendered."""
        intro = _normalised(_section_31().split("\n#### ", 1)[0])
        for needle in ("`INFO FOR DB`", "`INFO FOR TABLE`", "system user", "2.x", "3.x", "as each line renders them"):
            assert needle in intro, needle

    @pytest.mark.parametrize("heading", list(_PART_NEEDLES_31))
    def test_what_each_subsection_says(self, heading: str) -> None:
        text = _normalised(_part_31(heading))
        missing = [needle for needle in _PART_NEEDLES_31[heading] if needle not in text]
        assert not missing, missing

    def test_kinds_table(self) -> None:
        """The §6.4.4 mapping as a two-column table, no pipe in a cell (a kind union is written in prose)."""
        part = _part_31("How SurrealDB kinds become fields")
        [lines] = _tables(part)
        assert _cells(lines[0]) == ["SurrealDB kind", "Generated annotation"]
        rows = dict(cells for _, cells in _table_rows(part))
        assert rows["`number`"].startswith("`int`, `float` or `Decimal`"), rows
        assert rows["`duration`"].startswith("`Duration`"), rows
        assert rows["`record`, `record<…>`"] == "`RecordID`", rows
        assert rows["`[A, B]` (a tuple)"] == "`tuple[A, B]`", rows

    def test_permissions_exactly(self) -> None:
        """Spec §11.1 item 8 (D18): both paragraphs exactly, the key phrases of AC-16 and the link."""
        part = _part_31("Permissions")
        paragraphs = [_normalised(paragraph) for paragraph in part.split("\n\n")[1:] if paragraph.strip()]
        assert paragraphs == _PERMISSIONS_31, paragraphs
        text = _normalised(part)
        for phrase in _PERMISSIONS_PHRASES_31:
            assert phrase in text, phrase
        assert f"]({_INFO_DOCUMENTATION})" in text

    def test_default_bullets_exactly(self) -> None:
        """Spec §11.1 item 1b: the ``DEFAULT`` bullet; the M29 ``update()`` bullet, narrowed by #261, and no sub-field
        bullet after it (``save()`` reads the sub-fields a ``DEFAULT`` fills back)."""
        kinds = _bullets(_part_31("How SurrealDB kinds become fields"))
        assert _DEFAULT_BULLET_31 in kinds, kinds
        assert kinds[kinds.index(_DEFAULT_BULLET_31) - 1].startswith("- **Sub-models**")
        limits = _bullets(_part_31("What the generator does not do"))
        assert _UPDATE_BULLET_31 in limits, limits
        assert limits[limits.index(_UPDATE_BULLET_31) + 1].startswith("- `array<T, N>` becomes `list[T]`")
        assert not any("after `save()`, which reads back only" in bullet for bullet in limits), limits

    def test_coming_from_the_full_orm(self) -> None:
        """Spec §11.1 item 7 (P1, P2; D25 amended): 18 bullets, exactly, and no 19th."""
        assert _bullets(_part_31("Coming from the full SurrealDB-ORM")) == _COMING_FROM_31

    def test_coming_from_key_phrases_in_order(self) -> None:
        """AC-16: one key phrase per bullet, in order, matched after collapsing whitespace."""
        bullets = _bullets(_part_31("Coming from the full SurrealDB-ORM"))
        assert len(bullets) == len(_COMING_FROM_PHRASES_31)
        for bullet, phrase in zip(bullets, _COMING_FROM_PHRASES_31, strict=True):
            assert phrase in bullet, (phrase, bullet)

    def test_features_row(self) -> None:
        features = [cells for _, cells in _table_rows(_readme_part("## Features"))]
        firsts = [cells[0] for cells in features]
        assert _FEATURES_ROW_31 in features, features
        assert firsts.index(_FEATURES_ROW_31[0]) == firsts.index("Multiple databases (named connections, `using()`)") + 1

    def test_roadmap_rows(self) -> None:
        roadmap = [cells for _, cells in _table_rows(_readme_part("## Roadmap"))]
        start = [cells[0] for cells in roadmap].index("v0.29.0")
        assert roadmap[start : start + 4] == _ROADMAP_ROWS_31, roadmap
        assert roadmap[start + 4][0] == "v0.33.0", roadmap

    def test_comparison_row(self) -> None:
        """Spec §11.1 item 4: the label stays ``Schema Introspection``, the lite cell names the API, the full ORM's
        cell stays ``✅``; right after the Multi-database row."""
        comparison = [cells for _, cells in _table_rows(_readme_part("## SurrealDB-ORM-lite vs SurrealDB-ORM"))]
        firsts = [cells[0] for cells in comparison]
        assert comparison[firsts.index("Multi-database") + 1] == _COMPARISON_ROW_31, comparison

    def test_server_behaviour_rows(self) -> None:
        """Spec §11.1 item 5: seven rows, Since v0.30.0, together and in version order before the table's last row
        (later releases, v0.30.x follow-ups among them, add their rows in version order)."""
        assert_server_rows(_INTROSPECTION_ROWS)

    def test_server_behaviour_table_keeps_its_widths(self) -> None:
        """Every line 535 characters, the columns 136, 175, 181 and 38 wide: the new rows are padded to them."""
        assert_server_table()

    def test_server_behaviour_header_sentence(self) -> None:
        """The sentence v0.30.0 adds to the table's intro paragraph, in version order among the others."""
        assert_server_header_sentence(_HEADER_SENTENCE_30)

    def test_embedded_table_row(self) -> None:
        """Spec §11.1 item 6: all four cells, in version order before the catch-all "Everything else" row; widths
        kept (69, 343, 113, 9)."""
        assert_embedded_rows([_EMBEDDED_INTROSPECTION_ROW])

    def test_tables_have_no_raw_pipe_in_a_cell(self) -> None:
        """Every table v0.30.0 touches keeps one cell count per line and no escaped pipe."""
        parts = [
            _section_31(),
            _readme_part("## Features"),
            _readme_part(_SERVER_BEHAVIOUR),
            _readme_part(_EMBEDDED_BEHAVIOUR),
            _readme_part("## Roadmap"),
            _readme_part("## SurrealDB-ORM-lite vs SurrealDB-ORM"),
        ]
        tables = [lines for part in parts for lines in _tables(part)]
        assert len(tables) == len(parts), "section 31 has its kinds table"
        for lines in tables:
            assert len({len(_cells(line)) for line in lines}) == 1 and "\\|" not in "".join(lines), lines

    async def test_section_31_examples(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC-16: every ``python`` block runs as written, in order and in one namespace, on ``mem://`` (inside
        ``embedded_database()``, with ``tmp_path`` as the working directory); every result a comment states is the one
        the examples got; the block after the generate example is exactly the source it prints, which is also the file
        it writes, and the ``overwrite=False`` call prints E8's text."""
        monkeypatch.chdir(tmp_path)
        blocks = _python_blocks_31()
        printed = _printed_index(blocks)
        runnable = blocks[:printed] + blocks[printed + 1 :]
        stated = [pair for block in runnable for pair in _stated(block)]
        assert [expression for expression, _ in stated] == _STATED_31, stated
        namespace: dict[str, Any] = {"__name__": "readme_section_31"}
        try:
            async with embedded_database():
                for code in runnable:
                    compiled = compile(code, "<README section 31>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                    # A doctest of this repository's own README (trusted text), top-level awaits included.
                    outcome = eval(compiled, namespace)
                    if inspect.iscoroutine(outcome):
                        await outcome
                    for expression, comment in _stated(code):
                        value = repr(eval(expression, namespace))
                        assert comment == value or comment.startswith(f"{value}: "), (expression, comment, value)

                assert namespace["source"] == blocks[printed]
                assert capsys.readouterr().out == blocks[printed] + "\n" + _E8_PRINTED
                assert (tmp_path / "models_generated.py").read_text(encoding="utf-8") == blocks[printed]
                assert list(namespace["just_person"].tables) == ["person"]
                assert namespace["no_table"].tables == {} and namespace["no_table"].connection == "default"
                assert namespace["archive"].tables == {}
        finally:
            namespace.clear()
            await SurrealDBConnectionManager.remove_connection("archive")
