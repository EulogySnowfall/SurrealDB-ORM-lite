"""Migration system (v0.38.0): the comparator, the diff and the replays — ``migrations/compare.py``,
``migrations/state.py``, ``SchemaState.diff()`` and the attributes the migrations add to ``schema_state``.

``compare.literal_equal(a, b)`` (``TestLiteralEqual``) compares two SurrealQL ``DEFAULT`` texts: two literals the ORM
can read by their value and kind — a float with or without its ``f`` suffix, a decimal however many trailing zeros the
server keeps (M15 b, c), a string however quoted —, anything else by its text.

The comparator (``TestComparator``, spec §5.7.1): each definition is reduced to a key — ``compare.field_key()``,
``table_key()``, ``index_key()``, ``event_key()``, ``access_key()``, ``analyzer_key()`` — that normalises what the
servers render differently from the ORM's statements (M15). The DB-side texts below were measured on 2026-10-27 on
SurrealDB 3.3.4 and 2.7.0 (slot 1, ``INFO FOR DB``/``INFO FOR TABLE`` after the ORM's statements): besides the
whitespace, both servers end the last statement of an event's block with ``;`` (3.3.4 when the block holds several
statements, 2.7.0 always, on separate lines), and 3.3.4 drops the parentheses of a block holding one parenthesised
expression — the comparator normalises both.

The diff (``TestDiffRules``, ``TestDiffOrder``: spec §5.7, AC-6), the effective target read from the *target*'s marks
(``TestEffectiveTarget``, ``TestServerFields``: D12, D28, AC-28), the unmarked diff that compares every clause
(``TestUnmarkedDiff``: C2, AC-12), what it ignores (``TestIgnored``) and the definitions an operation cannot write
(``TestUnrepresentable``). The marks and ``applied_migrations`` (``TestStateAttributes``: §5.9, §5.10, C4, AC-31,
AC-33). The strict and file replays and the rewind (``TestReplay``, ``TestRewind``: §6.3, C9, E-R1, D36, D37), and
Task 3's round trip in its ``diff()`` form (``TestRoundTripDiff``, AC-4).

The models' state (spec §5.9, ``migrations/introspector.py``): ``introspect_models()`` reads back what each model's
appliers write (``TestModelState``), with the marks — ``TableState.undeclared`` (``TestUndeclared``),
``FieldState.undeclared`` and ``FieldState.server`` (``TestFieldMarks``, AC-33) —; the operations its diff from an empty
state gives replay to a state that diffs ``[]`` against it in both directions, marks removed (``TestRoundTrip``, AC-4);
``get_registered_models()`` walks the model classes depth first (``TestRegistryWalk``, AC-32).

Against the database (spec §5.10, §6.5), over WebSocket and HTTP on the line under test and the embedded engine, each
test in a database of its own: ``schema_diff()`` (``TestSchemaDiffE2E``, AC-8), ``makemigrations()`` reading the
current state from the database by default (``TestFromDbE2E``, AC-19; ``TestExistingTablesE2E``, C8, AC-8 bis, AC-16
+), what models leave to the database kept (``TestUndeclaredE2E``, ``TestCarriedE2E``, AC-28), names in backticks
(``TestNamesE2E``, AC-29), defaults holding ``;`` (``TestDefaultsE2E``, AC-30), ``applied_migrations`` read
(``TestAppliedMigrationsE2E``, AC-31, AC-33), named connections (``TestNamedConnectionE2E``, AC-20, WebSocket and HTTP)
and the tracking table skipped by the model generator (``TestGenerateSkipsTrackingTableE2E``, AC-23). Unit, without a
server: the history checks of ``makemigrations()`` on a fake read (``TestFromDbHistory``, E-H1–E-H3), the applied
names' reader on a fake client (``TestAppliedMigrationsReader``), the exports (``TestExports``) and the import layering
in fresh interpreters (``TestLayering``, AC-1).
"""

import ast
import dataclasses
import inspect
import itertools
import logging
import re
import shutil
import subprocess
import sys
import textwrap
import types
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import Field

import surreal_orm_lite
from surreal_orm_lite import (
    Analyzer,
    AuthenticatedUserMixin,
    BaseSurrealEdge,
    BaseSurrealModel,
    Computed,
    DatabaseIntrospector,
    Index,
    ModelCodeGenerator,
    SchemaMode,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    TableType,
    _sdk,
    computed,
    generate_models_from_db,
    get_registered_models,
    schema_diff,
)
from surreal_orm_lite.define_parser import parse_define_access, parse_define_event, parse_define_field, parse_define_table
from surreal_orm_lite.exceptions import SurrealDbAuthenticationError, SurrealDbError
from surreal_orm_lite.migrations import (
    AddField,
    AlterField,
    AlterTable,
    CreateIndex,
    CreateTable,
    DefaultExpression,
    DefineAccess,
    DefineAnalyzer,
    DefineEvent,
    DefineFunction,
    DropField,
    DropIndex,
    DropTable,
    Migration,
    MigrationExecutor,
    MigrationGenerator,
    ModelIntrospector,
    Operation,
    RawSQL,
    RebuildIndex,
    RemoveAccess,
    RemoveAnalyzer,
    RemoveEvent,
    RemoveFunction,
    compare,
    executor,
    introspect_models,
    introspector,
    makemigrations,
    render,
    state,
)
from surreal_orm_lite.migrations.constants import MIGRATIONS_TABLE
from surreal_orm_lite.schema import Event, access_statement
from surreal_orm_lite.schema_state import (
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
from surreal_orm_lite.time_travel import cached_major
from tests.conftest import _migration_url, migration_database, new_database_name
from tests.test_migration_operations import (
    AdPlain,
    AdRefresh,
    AdUserTable,
    MigLikes,
    MigPerson,
    MigStats,
    MigUser,
    _round_trips,
)


class TestLiteralEqual:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("1.5", "1.5f"),
            ("1.10dec", "1.1dec"),
            ("18", "18"),
            ("-3", "-3"),
            ("true", "true"),
            ("'a;b'", "'a;b'"),
            ('"it\'s"', "'it\\'s'"),
            ("d'2026-01-01T00:00:00Z'", "d'2026-01-01T00:00:00.000000Z'"),
            ("NONE", "NONE"),
            ("time::now()", "time::now()"),
            (" 18 ", "18"),
        ],
    )
    def test_equal(self, a: str, b: str) -> None:
        assert compare.literal_equal(a, b) is True
        assert compare.literal_equal(b, a) is True

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("1.5", "1.6f"),
            ("'a'", "'a '"),
            ("1", "1.0"),
            ("1", "1dec"),
            ("1.5f", "1.5dec"),
            ("true", "1"),
            ("false", "0"),
            ("'1'", "1"),
            ("d'2026-01-01T00:00:00Z'", "d'2026-01-01T00:00:01Z'"),
            ("NONE", "NULL"),
            ("time::now()", "time::now( )"),
            ("18", "time::now()"),
        ],
    )
    def test_different(self, a: str, b: str) -> None:
        assert compare.literal_equal(a, b) is False
        assert compare.literal_equal(b, a) is False


# --- states built by hand ----------------------------------------------------------------------------------------------

#: The marks ``ModelIntrospector`` sets on every field (spec §5.9): the clauses a model cannot declare (F8).
_FIELD_MARKS = frozenset(
    {"flexible", "reference", "default_always", "readonly", "computed", "assertion", "permissions", "comment"}
)


def _f(name: str, kind: str, *, table: str = "person", **changes: Any) -> FieldState:
    """A field of *table* as ``DatabaseIntrospector`` reads it: not nullable unless said, no mark unless given."""
    return FieldState(name=name, field_type=kind, nullable=changes.pop("nullable", False), table=table, **changes)


def _i(name: str, *fields: str, table: str = "person", **changes: Any) -> IndexState:
    return IndexState(name, fields, table=table, **changes)


def _e(name: str, then: str, *, table: str = "person", when: str = "true", **changes: Any) -> EventState:
    return EventState(name, table, when, then, **changes)


def _t(
    name: str,
    *fields: FieldState,
    mode: str = "SCHEMAFULL",
    table_type: str = "normal",
    indexes: tuple[IndexState, ...] = (),
    events: tuple[EventState, ...] = (),
    **changes: Any,
) -> TableState:
    return TableState(
        name,
        mode,
        table_type,
        {field.name: field for field in fields},
        {index.name: index for index in indexes},
        {event.name: event for event in events},
        **changes,
    )


def _s(
    *tables: TableState,
    analyzers: tuple[AnalyzerState, ...] = (),
    accesses: tuple[AccessState, ...] = (),
    **changes: Any,
) -> SchemaState:
    return SchemaState(
        {table.name: table for table in tables},
        analyzers={analyzer.name: analyzer for analyzer in analyzers},
        accesses={access.name: access for access in accesses},
        **changes,
    )


def _replay(*operations: Operation, start: SchemaState | None = None) -> SchemaState:
    """The state each operation's ``state_forwards()`` gives in turn, from *start* (an empty state)."""
    result = SchemaState() if start is None else start
    for operation in operations:
        result = operation.state_forwards(result)
    return result


def _unmarked(value: SchemaState) -> SchemaState:
    """*value* with every mark removed: the state ``DatabaseIntrospector`` or a replay would give."""
    tables = {}
    for name, table in value.tables.items():
        fields = {key: dataclasses.replace(field, server=False, undeclared=frozenset()) for key, field in table.fields.items()}
        tables[name] = dataclasses.replace(table, fields=fields, undeclared=frozenset())
    return dataclasses.replace(value, tables=tables)


_SIGNIN = "email = $email AND crypto::argon2::compare(password, $password)"
_SIGNUP_FIELDS = {"email": "$email", "password": "crypto::argon2::generate($password)"}


def _access(**changes: Any) -> AccessState:
    name = changes.pop("name", "account")
    return AccessState(name, "users", changes.pop("signup_fields", _SIGNUP_FIELDS), _SIGNIN, **changes)


def _access_op(**changes: Any) -> DefineAccess:
    return DefineAccess(changes.pop("name", "account"), "users", _SIGNUP_FIELDS, _SIGNIN, **changes)


# --- the comparator ----------------------------------------------------------------------------------------------------

# Measured on 2026-10-27 (slot 1): INFO FOR TABLE/DB after the ORM's statements, the same on 3.3.4 and 2.7.0 unless the
# name ends with the line.
_DB_VALUE = "DEFINE FIELD nxt ON pr TYPE int VALUE age + 1 PERMISSIONS FULL"
_DB_CONCAT = "DEFINE FIELD full ON pr TYPE string VALUE string::concat(name, ' ', email) PERMISSIONS FULL"
_DB_FLOAT = "DEFINE FIELD f ON pr TYPE float DEFAULT 1.5f PERMISSIONS FULL"
_DB_DECIMAL_3 = "DEFINE FIELD d ON pr TYPE decimal DEFAULT 1.1dec PERMISSIONS FULL"
_DB_DECIMAL_2 = "DEFINE FIELD d ON pr TYPE decimal DEFAULT 1.10dec PERMISSIONS FULL"
_DB_FLEXIBLE_ANY_2 = "DEFINE FIELD bag ON pr FLEXIBLE TYPE any PERMISSIONS FULL"
_DB_FIELD_RULES = "DEFINE FIELD age ON pr TYPE int PERMISSIONS FOR select WHERE age >= 18, FOR create, update FULL"
_DB_TABLE = "DEFINE TABLE pr TYPE NORMAL SCHEMAFULL CHANGEFEED 1w INCLUDE ORIGINAL PERMISSIONS NONE"
_DB_TABLE_RULES = (
    "DEFINE TABLE pr TYPE NORMAL SCHEMALESS PERMISSIONS FOR select WHERE published = true OR user = $auth.id, "
    "FOR create, update, delete NONE"
)
_DB_VIEW = "DEFINE TABLE pv TYPE ANY SCHEMALESS AS SELECT count() AS n FROM pr WHERE age >= 18 GROUP ALL PERMISSIONS NONE"
_DB_EVENTS_3 = (
    "DEFINE EVENT audit ON pr WHEN $event = 'CREATE' THEN { CREATE log SET at = time::now(); CREATE log2 SET x = 1; }"
)
_DB_EVENTS_2 = (
    "DEFINE EVENT audit ON pr WHEN $event = 'CREATE' THEN {\nCREATE log SET at = time::now();\nCREATE log2 SET x = 1;\n}"
)
_DB_EVENT_3 = "DEFINE EVENT one ON pr WHEN true THEN { CREATE log SET at = time::now() }"
_DB_EVENT_2 = "DEFINE EVENT one ON pr WHEN true THEN { CREATE log SET at = time::now(); }"
_DB_PAREN_3 = "DEFINE EVENT paren ON pr WHEN true THEN { CREATE log SET a = 1 }"
_DB_PAREN_2 = "DEFINE EVENT paren ON pr WHEN true THEN { (CREATE log SET a = 1) }"
_DB_ASYNC_3 = "DEFINE EVENT asy ON pr ASYNC RETRY 1 MAXDEPTH 3 WHEN true THEN { CREATE log SET a = 1 }"
_DB_ACCESS = (
    "DEFINE ACCESS pacc ON DATABASE TYPE RECORD SIGNUP (CREATE prk SET email = $email, password = "
    "crypto::argon2::generate($password)) SIGNIN (SELECT * FROM prk WHERE email = $email AND "
    "crypto::argon2::compare(password, $password)) WITH JWT ALGORITHM HS512 KEY '[REDACTED]' WITH ISSUER KEY "
    "'[REDACTED]' DURATION FOR TOKEN 15m, FOR SESSION 12h"
)


def _db_field(text: str) -> FieldState:
    return parse_define_field(text).state


def _own(statement: str) -> Any:
    return render.read_own(statement)


class TestComparator:
    """Spec §5.7.1: each M15 difference between the ORM's statement and the servers' rendering compares equal, a real
    change does not."""

    def test_whitespace_outside_literals_in_an_expression(self) -> None:
        mine = _own(AddField("pr", "nxt", "int", value="age+1").forwards())
        assert compare.field_key(mine) == compare.field_key(_db_field(_DB_VALUE))
        concat = _own(AddField("pr", "full", "string", value="string::concat(name,' ',email)").forwards())
        assert compare.field_key(concat) == compare.field_key(_db_field(_DB_CONCAT))

    def test_a_real_change_of_an_expression(self) -> None:
        mine = _own(AddField("pr", "nxt", "int", value="age + 2").forwards())
        assert compare.field_key(mine) != compare.field_key(_db_field(_DB_VALUE))

    def test_whitespace_inside_a_string_literal_is_significant(self) -> None:
        wider = _own(AddField("pr", "full", "string", value="string::concat(name, '  ', email)").forwards())
        assert compare.field_key(wider) != compare.field_key(_db_field(_DB_CONCAT))
        assert compare.expression_key("a = ' x '") != compare.expression_key("a = 'x'")
        assert compare.expression_key("`first name` = 1") != compare.expression_key("`firstname` = 1")
        assert compare.expression_key("a  =  'x  y'") == "a='x  y'"
        assert compare.expression_key(None) is None

    def test_a_float_default_gains_its_suffix(self) -> None:
        mine = _own(AddField("pr", "f", "float", default=1.5).forwards())
        assert compare.field_key(mine) == compare.field_key(_db_field(_DB_FLOAT))
        other = _own(AddField("pr", "f", "float", default=1.6).forwards())
        assert compare.field_key(other) != compare.field_key(_db_field(_DB_FLOAT))

    def test_a_decimal_default_normalised_by_3x_only(self) -> None:
        mine = _own("DEFINE FIELD `d` ON `pr` TYPE decimal DEFAULT 1.10dec;")
        assert compare.field_key(mine) == compare.field_key(_db_field(_DB_DECIMAL_3))
        assert compare.field_key(mine) == compare.field_key(_db_field(_DB_DECIMAL_2))
        assert compare.field_key(_f("d", "decimal", default="1.2dec")) != compare.field_key(_db_field(_DB_DECIMAL_3))

    def test_a_string_default_compares_its_value(self) -> None:
        assert compare.field_key(_f("s", "string", default="'a'")) == compare.field_key(_f("s", "string", default='"a"'))
        assert compare.field_key(_f("s", "string", default="'a'")) != compare.field_key(_f("s", "string", default="'a '"))
        assert compare.field_key(_f("s", "int", default="1")) != compare.field_key(_f("s", "int", default="1.0"))

    def test_an_expression_default_without_whitespace(self) -> None:
        assert compare.field_key(_f("t", "datetime", default="time::now( )")) == compare.field_key(
            _f("t", "datetime", default="time::now()")
        )

    def test_a_change_feed_by_its_length(self) -> None:
        mine = _own(CreateTable("pr", table_type="normal", changefeed="7d INCLUDE ORIGINAL").forwards())
        assert compare.table_key(mine) == compare.table_key(parse_define_table(_DB_TABLE))
        assert compare.table_key(_t("pr", changefeed="1w")) != compare.table_key(_t("pr", changefeed="2w"))
        assert compare.table_key(_t("pr", changefeed="1h")) != compare.table_key(
            _t("pr", changefeed="1h", changefeed_include_original=True)
        )

    def test_the_default_permissions(self) -> None:
        mine = _own(AddField("pr", "age", "int").forwards())
        assert compare.field_key(mine) == compare.field_key(_db_field("DEFINE FIELD age ON pr TYPE int PERMISSIONS FULL"))
        assert compare.table_key(_own(CreateTable("pr", table_type="normal").forwards())) == compare.table_key(
            parse_define_table("DEFINE TABLE pr TYPE NORMAL SCHEMAFULL PERMISSIONS NONE")
        )
        assert compare.table_key(_t("pr", permissions={"select": "FULL"})) != compare.table_key(_t("pr"))

    def test_permission_rules_without_whitespace(self) -> None:
        field = _own(AddField("pr", "age", "int", permissions={"select": "WHERE age>=18"}).forwards())
        assert compare.field_key(field) == compare.field_key(_db_field(_DB_FIELD_RULES))
        stricter = _own(AddField("pr", "age", "int", permissions={"select": "WHERE age >= 21"}).forwards())
        assert compare.field_key(stricter) != compare.field_key(_db_field(_DB_FIELD_RULES))
        rules = {"select": "WHERE published=true OR user=$auth.id"}
        table = _own(CreateTable("pr", schema_mode="SCHEMALESS", table_type="normal", permissions=rules).forwards())
        assert compare.table_key(table) == compare.table_key(parse_define_table(_DB_TABLE_RULES))

    def test_a_rule_of_another_form_compares_its_text(self) -> None:
        assert compare.field_key(_f("a", "int", permissions={"select": "x  y"})) == compare.field_key(
            _f("a", "int", permissions={"select": "x y"})
        )
        assert compare.table_key(_t("pr", permissions={"Select": "full"})) == compare.table_key(
            _t("pr", permissions={"select": "FULL"})
        )

    def test_flexible_is_ignored_on_a_kind_holding_any(self) -> None:
        mine = _own(AddField("pr", "bag", "any").forwards())
        assert compare.field_key(mine) == compare.field_key(_db_field(_DB_FLEXIBLE_ANY_2))
        assert compare.field_key(_f("tags", "array", flexible=True)) == compare.field_key(_f("tags", "array"))
        assert compare.field_key(_f("o", "object", flexible=True)) != compare.field_key(_f("o", "object"))

    def test_a_nullable_kind_however_written(self) -> None:
        assert compare.field_key(FieldState("bag", "any")) == compare.field_key(_f("bag", "any"))
        assert compare.field_key(FieldState("a", "option<int>", False)) == compare.field_key(FieldState("a", "int", True))
        assert compare.field_key(_f("a", "int")) != compare.field_key(_f("a", "int", nullable=True))
        assert compare.field_key(_f("a", "frob<")) == compare.field_key(_f("a", "frob<"))  # a kind the parser cannot read

    def test_a_view_query(self) -> None:
        query = "SELECT count() AS n FROM pr WHERE age>=18 GROUP ALL"
        mine = _own(CreateTable("pv", schema_mode=None, view_query=query).forwards())
        assert compare.table_key(mine) == compare.table_key(parse_define_table(_DB_VIEW))
        other = _own(CreateTable("pv", schema_mode=None, view_query=query.replace("18", "21")).forwards())
        assert compare.table_key(other) != compare.table_key(parse_define_table(_DB_VIEW))

    @pytest.mark.parametrize("rendered", [_DB_EVENTS_3, _DB_EVENTS_2])
    def test_a_block_of_several_statements(self, rendered: str) -> None:
        then = "CREATE log SET at = time::now(); CREATE log2 SET x = 1"
        mine = _own(Event("audit", then=then, when="$event = 'CREATE'").ddl("pr"))
        assert compare.event_key(mine) == compare.event_key(parse_define_event(rendered))

    @pytest.mark.parametrize("rendered", [_DB_EVENT_3, _DB_EVENT_2])
    def test_a_block_of_one_statement(self, rendered: str) -> None:
        mine = _own(Event("one", then="CREATE log SET at = time::now()").ddl("pr"))
        assert compare.event_key(mine) == compare.event_key(parse_define_event(rendered))
        changed = _own(Event("one", then="CREATE log SET at = time::now() - 1s").ddl("pr"))
        assert compare.event_key(changed) != compare.event_key(parse_define_event(rendered))

    @pytest.mark.parametrize("rendered", [_DB_PAREN_3, _DB_PAREN_2])
    def test_a_block_of_one_parenthesised_expression(self, rendered: str) -> None:
        mine = _own(Event("paren", then="(CREATE log SET a = 1)").ddl("pr"))
        assert compare.event_key(mine) == compare.event_key(parse_define_event(rendered))
        changed = _own(Event("paren", then="(CREATE log SET a = 2)").ddl("pr"))
        assert compare.event_key(changed) != compare.event_key(parse_define_event(rendered))

    def test_block_normalisations_stop_at_literals(self) -> None:
        assert compare.expression_key("{ RETURN ';}' }") != compare.expression_key("{ RETURN '}' }")
        assert compare.expression_key("{ RETURN ';' }") == "{RETURN';'}"
        assert compare.expression_key("{ a; }") == compare.expression_key("{ a }")
        assert compare.expression_key("{ (a) + (b) }") == "{(a)+(b)}"
        assert compare.expression_key("{ ((a)) }") == "{(a)}"
        assert compare.expression_key("{ (a); }") == "{a}"
        assert compare.expression_key("{ a; b; }") == "{a;b}"
        assert compare.expression_key("( a ) + { b }") == "(a)+{b}"
        assert compare.expression_key("{ (a ") == "{(a"  # unbalanced: no unwrapping
        assert compare.expression_key("{ (a] }") == "{(a]}"  # mismatched: no unwrapping

    def test_an_async_event_with_the_default_counts(self) -> None:
        mine = _own(Event("asy", then="CREATE log SET a = 1", async_mode=True).ddl("pr"))
        assert (mine.retry, mine.max_depth) == (None, None)
        assert compare.event_key(mine) == compare.event_key(parse_define_event(_DB_ASYNC_3))
        assert compare.event_key(_e("asy", "{ a }", async_mode=True, retry=2)) != compare.event_key(
            _e("asy", "{ a }", async_mode=True)
        )
        assert compare.event_key(_e("e", "{ a }")) != compare.event_key(_e("e", "{ a }", async_mode=True))

    def test_an_access_as_rendered(self) -> None:
        mine = _own(DefineAccess("pacc", "prk", _SIGNUP_FIELDS, _SIGNIN).forwards())
        stored = parse_define_access(_DB_ACCESS)
        assert compare.access_key(mine) == compare.access_key(stored)
        assert compare.access_key(mine) != compare.access_key(dataclasses.replace(stored, duration_token="1h"))
        assert compare.access_key(mine) == compare.access_key(dataclasses.replace(stored, raw="x"))

    def test_signup_fields_as_a_mapping(self) -> None:
        keyed = {"email": "$email", "id": "$email", "password": "crypto::argon2::generate($password)"}
        reordered = {"password": "crypto::argon2::generate( $password )", "email": "$email", "id": "$email"}
        assert compare.access_key(_access(signup_fields=keyed)) == compare.access_key(_access(signup_fields=reordered))
        assert compare.access_key(_access(signup_fields=keyed)) != compare.access_key(_access())

    def test_access_durations_by_length_and_the_grant_only_with_refresh(self) -> None:
        assert compare.access_key(_access(duration_session="1d")) == compare.access_key(_access(duration_session="24h"))
        assert compare.access_key(_access(duration_token="NONE")) != compare.access_key(_access())
        assert compare.access_key(_access(duration_token="none")) == compare.access_key(_access(duration_token="NONE"))
        assert compare.access_key(_access(duration_token=None)) != compare.access_key(_access())
        assert compare.access_key(_access(duration_grant="NONE")) == compare.access_key(_access())
        with_refresh = _access(with_refresh=True, duration_grant="30d")
        assert compare.access_key(with_refresh) != compare.access_key(_access(with_refresh=True, duration_grant="60d"))
        assert compare.access_key(with_refresh) != compare.access_key(_access())

    def test_an_access_of_another_shape_compares_its_texts(self) -> None:
        jwt = AccessState("api", access_type="JWT", raw="DEFINE ACCESS api … KEY 'a'")
        assert compare.access_key(jwt) == compare.access_key(dataclasses.replace(jwt, raw="DEFINE ACCESS api … KEY 'b'"))
        odd = AccessState("odd", signup="(RETURN 1)", signin="(SELECT * FROM a)")
        assert compare.access_key(odd) == compare.access_key(dataclasses.replace(odd, signup="( RETURN 1 )"))
        assert compare.access_key(odd) != compare.access_key(dataclasses.replace(odd, signup="(RETURN 2)"))

    def test_index_options_with_the_server_defaults(self) -> None:
        full_text = _i("body_ft", "body", search_analyzer="english")
        assert compare.index_key(full_text) == compare.index_key(_i("body_ft", "body", search_analyzer="english", bm25=True))
        stored = _i("body_ft", "body", search_analyzer="english", bm25=(1.2000000476837158, 0.75))
        assert compare.index_key(full_text) == compare.index_key(stored)
        tuned = _i("body_ft", "body", search_analyzer="english", bm25=(1.5, 0.75))
        assert compare.index_key(full_text) != compare.index_key(tuned)
        vector = _i("emb_v", "emb", hnsw=True, dimension=4)
        spelled = _i("emb_v", "emb", hnsw=True, dimension=4, dist="euclidean", vector_type="F32", efc=150, m=12)
        assert compare.index_key(vector) == compare.index_key(spelled)
        assert compare.index_key(vector) != compare.index_key(dataclasses.replace(spelled, dist="COSINE"))
        mtree = _i("emb_m", "emb", mtree=True, dimension=4, dist="euclidean")
        assert compare.index_key(mtree) == compare.index_key(dataclasses.replace(mtree, dist="EUCLIDEAN"))
        unique = _i("u", "a", unique=True)
        assert compare.index_key(unique) == compare.index_key(dataclasses.replace(unique, concurrently=True))
        assert compare.index_key(unique) != compare.index_key(_i("u", "a"))

    def test_analyzers_by_tokenizers_filters_function_and_comment(self) -> None:
        mine = AnalyzerState("en", ["blank"], ["lowercase", "mapper('/tmp/Map.txt')"])
        stored = AnalyzerState("en", ["BLANK"], ["lowercase", "mapper(/tmp/map.txt)"])
        assert compare.analyzer_key(mine) == compare.analyzer_key(stored)
        assert compare.analyzer_key(mine) != compare.analyzer_key(AnalyzerState("en", ["blank"], ["lowercase"]))
        assert compare.analyzer_key(mine) != compare.analyzer_key(dataclasses.replace(mine, comment="x"))
        assert compare.analyzer_key(mine) != compare.analyzer_key(dataclasses.replace(mine, function="fn::f"))

    def test_raw_path_table_and_marks_are_not_compared(self) -> None:
        plain = _f("a", "int", raw="DEFINE FIELD a ON person TYPE int")
        marked = dataclasses.replace(plain, raw="", table="other", server=True, undeclared={"comment", "default"})
        assert compare.field_key(plain) == compare.field_key(marked)
        table = _t("person", raw="x", other={"lives": {"a": "b"}})
        marked_table = dataclasses.replace(table, raw="", other={}, undeclared={"drop"})
        assert compare.table_key(table) == compare.table_key(marked_table)
        assert compare.field_key(FieldState("a.b", "int", path=["a.b"])) == compare.field_key(FieldState("a.b", "int"))

    def test_on_delete_actions(self) -> None:
        cascade = _f("o", "record<user>", reference=True, on_delete="cascade")
        assert compare.field_key(cascade) == compare.field_key(dataclasses.replace(cascade, on_delete="CASCADE"))
        then = dataclasses.replace(cascade, on_delete="THEN UPDATE a SET b=1")
        assert compare.field_key(then) == compare.field_key(dataclasses.replace(cascade, on_delete="THEN UPDATE a SET b = 1"))
        assert compare.field_key(then) != compare.field_key(cascade)


# --- the diff ----------------------------------------------------------------------------------------------------------


def _person(*fields: FieldState, **changes: Any) -> TableState:
    return _t("person", *fields, **changes)


_AGE = _f("age", "int")


class TestDiffRules:
    """Spec §5.7 (AC-6): one change of each kind between two unmarked states gives its operation, exactly."""

    def test_no_change(self) -> None:
        current = _s(_person(_AGE, indexes=(_i("by_age", "age"),)), analyzers=(AnalyzerState("en", ["blank"]),))
        assert current.diff(current) == []
        assert SchemaState().diff(SchemaState()) == []

    def test_a_field_added(self) -> None:
        target = _s(_person(_AGE, _f("email", "string")))
        assert _s(_person(_AGE)).diff(target) == [AddField("person", "email", "string")]

    def test_a_field_retyped(self) -> None:
        operations = _s(_person(_AGE)).diff(_s(_person(_f("age", "string"))))
        assert operations == [AlterField("person", "age", "string", previous_type="int")]

    def test_a_field_made_optional(self) -> None:
        operations = _s(_person(_AGE)).diff(_s(_person(_f("age", "int", nullable=True))))
        assert operations == [AlterField("person", "age", "int", nullable=True, previous_type="int")]

    def test_a_field_given_a_default(self) -> None:
        operations = _s(_person(_AGE)).diff(_s(_person(_f("age", "int", default="18"))))
        assert operations == [AlterField("person", "age", "int", default=18, previous_type="int")]

    def test_a_field_removed_children_before_parents(self) -> None:
        current = _s(_person(_AGE, _f("address", "object"), _f("address.city", "string"), _f("address_b", "string")))
        assert current.diff(_s(_person(_AGE))) == [
            DropField("person", "address_b"),
            DropField("person", "address.city"),
            DropField("person", "address"),
        ]

    def test_fields_added_parents_first(self) -> None:
        target = _s(_person(_AGE, _f("address", "object"), _f("address.city", "string")))
        assert _s(_person(_AGE)).diff(target) == [
            AddField("person", "address", "object"),
            AddField("person", "address.city", "string"),
        ]

    def test_a_table_schema_mode_changed(self) -> None:
        operations = _s(_person(mode="SCHEMALESS")).diff(_s(_person()))
        assert operations == [
            AlterTable("person", "SCHEMAFULL", "normal", previous_schema_mode="SCHEMALESS", previous_table_type="normal")
        ]

    def test_a_table_comment_changed(self) -> None:
        operations = _s(_person()).diff(_s(_person(comment="People")))
        assert operations == [
            AlterTable(
                "person",
                "SCHEMAFULL",
                "normal",
                comment="People",
                previous_schema_mode="SCHEMAFULL",
                previous_table_type="normal",
            )
        ]

    def test_a_table_change_feed_changed(self) -> None:
        operations = _s(_person(changefeed="1d")).diff(_s(_person(changefeed="1h", changefeed_include_original=True)))
        assert operations == [
            AlterTable(
                "person",
                "SCHEMAFULL",
                "normal",
                changefeed="1h INCLUDE ORIGINAL",
                previous_schema_mode="SCHEMAFULL",
                previous_table_type="normal",
                previous_changefeed="1d",
            )
        ]

    def test_table_permissions_changed(self) -> None:
        rules = {"select": "WHERE $auth.id = id", "create": "FULL", "update": "NONE", "delete": "NONE"}
        operations = _s(_person()).diff(_s(_person(permissions=rules)))
        assert operations == [
            AlterTable(
                "person",
                "SCHEMAFULL",
                "normal",
                permissions={"select": "WHERE $auth.id = id", "create": "FULL"},
                previous_schema_mode="SCHEMAFULL",
                previous_table_type="normal",
            )
        ]

    def test_an_index_added(self) -> None:
        operations = _s(_person(_AGE)).diff(_s(_person(_AGE, indexes=(_i("by_age", "age", unique=True),))))
        assert operations == [CreateIndex("person", "by_age", ["age"], unique=True)]

    def test_an_index_changed(self) -> None:
        current = _s(_person(_AGE, indexes=(_i("by_age", "age"),)))
        operations = current.diff(_s(_person(_AGE, indexes=(_i("by_age", "age", unique=True),))))
        assert operations == [
            CreateIndex("person", "by_age", ["age"], unique=True, previous=CreateIndex("person", "by_age", ["age"]))
        ]

    def test_an_index_removed(self) -> None:
        operations = _s(_person(_AGE, indexes=(_i("by_age", "age"),))).diff(_s(_person(_AGE)))
        assert operations == [DropIndex("person", "by_age", previous=CreateIndex("person", "by_age", ["age"]))]

    def test_an_analyzer_changed_rebuilds_its_other_indexes(self) -> None:
        post = _t("post", _f("body", "string", table="post"), _f("title", "string", table="post"))
        body = _i("post_body", "body", table="post", search_analyzer="english", bm25=(1.2, 0.75))
        title = _i("post_title", "title", table="post", search_analyzer="english", bm25=True)
        current = _s(
            dataclasses.replace(post, indexes={"post_body": body, "post_title": title}),
            analyzers=(AnalyzerState("english", ["blank"], ["lowercase"]),),
        )
        new_title = dataclasses.replace(title, highlights=True)
        target = _s(
            dataclasses.replace(post, indexes={"post_body": body, "post_title": new_title}),
            analyzers=(AnalyzerState("english", ["blank"], ["lowercase", "ascii"]),),
        )
        previous_title = CreateIndex("post", "post_title", ["title"], search_analyzer="english", bm25=True)
        assert current.diff(target) == [
            DefineAnalyzer(
                "english", ["blank"], ["lowercase", "ascii"], previous=DefineAnalyzer("english", ["blank"], ["lowercase"])
            ),
            CreateIndex(
                "post",
                "post_title",
                ["title"],
                search_analyzer="english",
                bm25=True,
                highlights=True,
                previous=previous_title,
            ),
            RebuildIndex("post", "post_body"),
        ]

    def test_an_analyzer_added_rebuilds_nothing(self) -> None:
        operations = _s().diff(_s(analyzers=(AnalyzerState("english", ["blank"]),)))
        assert operations == [DefineAnalyzer("english", ["blank"])]

    def test_an_event_added_changed_and_removed(self) -> None:
        audit = _e("audit", "{ CREATE log SET who = $after.id }", when="$event = 'CREATE'")
        current = _s(_person(events=(audit, _e("gone", "{ CREATE log }"))))
        changed = dataclasses.replace(audit, when="$event = 'UPDATE'")
        target = _s(_person(events=(changed, _e("hello", "{ CREATE hi }"))))
        previous = DefineEvent("audit", "person", "$event = 'CREATE'", "CREATE log SET who = $after.id")
        assert current.diff(target) == [
            DefineEvent("audit", "person", "$event = 'UPDATE'", "CREATE log SET who = $after.id", previous=previous),
            DefineEvent("hello", "person", None, "CREATE hi"),
            RemoveEvent("gone", "person", previous=DefineEvent("gone", "person", None, "CREATE log")),
        ]

    def test_a_view_query_changed(self) -> None:
        view = _t("adults", mode="SCHEMALESS", table_type="any", view_query="SELECT * FROM person WHERE age >= 18")
        target = dataclasses.replace(view, view_query="SELECT * FROM person WHERE age >= 21")
        assert _s(_person(), view).diff(_s(_person(), target)) == [
            AlterTable(
                "adults",
                "SCHEMALESS",
                view_query="SELECT * FROM person WHERE age >= 21",
                previous_schema_mode="SCHEMALESS",
                previous_view_query="SELECT * FROM person WHERE age >= 18",
            )
        ]

    def test_a_relation_s_ends_changed(self) -> None:
        likes = _t("likes", mode="SCHEMALESS", table_type="relation", relation_in=["person"], relation_out=["post"])
        target = dataclasses.replace(likes, relation_out=["post", "comment"])
        assert _s(likes).diff(_s(target)) == [
            AlterTable(
                "likes",
                "SCHEMALESS",
                "relation",
                relation_in="person",
                relation_out="post | comment",
                previous_schema_mode="SCHEMALESS",
                previous_table_type="relation",
                previous_relation_in="person",
                previous_relation_out="post",
            )
        ]

    def test_relation_ends_in_another_order_are_equal(self) -> None:
        likes = _t("likes", mode="SCHEMALESS", table_type="relation", relation_in=["a", "b"], relation_out=["c"])
        assert _s(likes).diff(_s(dataclasses.replace(likes, relation_in=["b", "a"]))) == []

    def test_an_access_s_durations_changed(self) -> None:
        operations = _s(accesses=(_access(),)).diff(_s(accesses=(_access(duration_token="1h"),)))
        assert operations == [DefineAccess("account", "users", _SIGNUP_FIELDS, _SIGNIN, "1h", previous=_access_op())]

    def test_an_access_added_and_removed(self) -> None:
        assert _s().diff(_s(accesses=(_access(),))) == [_access_op()]
        assert _s(accesses=(_access(),)).diff(_s()) == [RemoveAccess("account", previous=_access_op())]

    def test_a_table_added_with_its_definitions(self) -> None:
        post = _t(
            "post",
            _f("title", "string", table="post"),
            _f("body", "string", table="post"),
            mode="SCHEMALESS",
            comment="Posts",
            indexes=(_i("post_title", "title", table="post", unique=True),),
            events=(_e("b", "{ CREATE b }", table="post"), _e("a", "{ CREATE a }", table="post")),
        )
        assert _s().diff(_s(post)) == [
            CreateTable("post", "SCHEMALESS", "normal", comment="Posts"),
            AddField("post", "body", "string"),
            AddField("post", "title", "string"),
            CreateIndex("post", "post_title", ["title"], unique=True),
            DefineEvent("a", "post", None, "CREATE a"),
            DefineEvent("b", "post", None, "CREATE b"),
        ]

    def test_a_table_removed(self) -> None:
        assert _s(_person(_AGE)).diff(_s()) == [DropTable("person")]

    def test_an_analyzer_removed(self) -> None:
        operations = _s(analyzers=(AnalyzerState("english", ["blank"]),)).diff(_s())
        assert operations == [RemoveAnalyzer("english", previous=DefineAnalyzer("english", ["blank"]))]

    def test_the_method_is_the_module_function(self) -> None:
        current, target = _s(_person(_AGE)), _s(_person(_f("age", "float")))
        assert current.diff(target) == state.diff(current, target)


class TestDiffOrder:
    """Spec §5.7's order: analyzers, new tables (views last), tables in both, rebuilds, accesses, removed tables (views
    first), removed analyzers."""

    def test_every_step_in_order(self) -> None:
        english = AnalyzerState("english", ["blank"], ["lowercase"])
        post = _t(
            "post",
            _f("body", "string", table="post"),
            indexes=(_i("post_body", "body", table="post", search_analyzer="english", bm25=(1.2, 0.75)),),
        )
        audit = _e("audit", "{ CREATE log SET who = $after.id }")
        current = _s(
            _person(_AGE, _f("nick", "string"), indexes=(_i("by_age", "age"),), events=(audit,)),
            post,
            _t("gone"),
            _t("gone_view", mode="SCHEMALESS", table_type="any", view_query="SELECT * FROM gone"),
            analyzers=(english, AnalyzerState("old", ["blank"])),
            accesses=(_access(), _access(name="old_acc")),
        )
        zeta = _t(
            "zeta",
            _f("a", "int", table="zeta"),
            indexes=(_i("za", "a", table="zeta"),),
            events=(_e("ze", "{ CREATE z }", table="zeta"),),
        )
        target = _s(
            _person(
                _f("age", "string"),
                _f("email", "string"),
                comment="People",
                indexes=(_i("by_age", "age", "email"),),
                events=(_e("greet", "{ CREATE hi }"),),
            ),
            dataclasses.replace(post, indexes={"post_body": dataclasses.replace(post.indexes["post_body"], bm25=True)}),
            zeta,
            _t("aview", mode="SCHEMALESS", table_type="any", view_query="SELECT * FROM person"),
            _t("alpha"),
            analyzers=(dataclasses.replace(english, filters=["lowercase", "ascii"]), AnalyzerState("fresh", ["blank"])),
            accesses=(_access(duration_token="1h"), _access(name="new_acc")),
        )
        previous_english = DefineAnalyzer("english", ["blank"], ["lowercase"])
        previous_audit = DefineEvent("audit", "person", None, "CREATE log SET who = $after.id")
        assert current.diff(target) == [
            DefineAnalyzer("fresh", ["blank"]),
            DefineAnalyzer("english", ["blank"], ["lowercase", "ascii"], previous=previous_english),
            CreateTable("alpha", "SCHEMAFULL", "normal"),
            CreateTable("zeta", "SCHEMAFULL", "normal"),
            AddField("zeta", "a", "int"),
            CreateIndex("zeta", "za", ["a"]),
            DefineEvent("ze", "zeta", None, "CREATE z"),
            CreateTable("aview", "SCHEMALESS", view_query="SELECT * FROM person"),
            AlterTable(
                "person",
                "SCHEMAFULL",
                "normal",
                comment="People",
                previous_schema_mode="SCHEMAFULL",
                previous_table_type="normal",
            ),
            AlterField("person", "age", "string", previous_type="int"),
            AddField("person", "email", "string"),
            DropField("person", "nick"),
            CreateIndex("person", "by_age", ["age", "email"], previous=CreateIndex("person", "by_age", ["age"])),
            DefineEvent("greet", "person", None, "CREATE hi"),
            RemoveEvent("audit", "person", previous=previous_audit),
            RebuildIndex("post", "post_body"),
            DefineAccess("account", "users", _SIGNUP_FIELDS, _SIGNIN, "1h", previous=_access_op()),
            _access_op(name="new_acc"),
            RemoveAccess("old_acc", previous=_access_op(name="old_acc")),
            DropTable("gone_view"),
            DropTable("gone"),
            RemoveAnalyzer("old", previous=DefineAnalyzer("old", ["blank"])),
        ]


# --- the effective target ----------------------------------------------------------------------------------------------

_HAND_TABLE = _person(
    changefeed="1h", changefeed_include_original=True, permissions={"select": "FULL"}, comment="hand", drop=True
)
_UNDECLARED = _FIELD_MARKS | {"default", "value"}


class TestEffectiveTarget:
    """Spec §5.7 (D12, D28; AC-28's unit part): where the *target* carries marks, the diff targets what
    ``define_table()`` writes over the current definition."""

    def test_undeclared_table_clauses_are_kept(self) -> None:
        target = _person(undeclared={"permissions", "comment", "changefeed", "drop"})
        assert _s(_HAND_TABLE).diff(_s(target)) == []

    def test_a_declared_comment_change_keeps_the_rest(self) -> None:
        target = _person(comment="People", undeclared={"permissions", "changefeed", "drop"})
        assert _s(_HAND_TABLE).diff(_s(target)) == [
            AlterTable(
                "person",
                "SCHEMAFULL",
                "normal",
                changefeed="1h INCLUDE ORIGINAL",
                permissions={"select": "FULL"},
                comment="People",
                previous_schema_mode="SCHEMAFULL",
                previous_table_type="normal",
                previous_changefeed="1h INCLUDE ORIGINAL",
                previous_permissions={"select": "FULL"},
                previous_comment="hand",
                drop=True,
                previous_drop=True,
            )
        ]

    def test_undeclared_schema_mode_and_type(self) -> None:
        current = _person(mode="SCHEMALESS", table_type="any")
        assert _s(current).diff(_s(_person(undeclared={"schema_mode", "table_type"}))) == []
        relation = _t("likes", mode="SCHEMALESS", table_type="relation", relation_in=["a"], relation_out=["b"])
        assert _s(relation).diff(_s(_t("likes", mode="SCHEMALESS", undeclared={"table_type"}))) == []

    def test_a_retyped_field_carries_the_undeclared_clauses(self) -> None:
        current = _f(
            "score",
            "int",
            assertion="$value >= 0",
            readonly=True,
            permissions={"update": "NONE"},
            comment="pts",
            default="1",
            default_always=True,
        )
        target = _f("score", "float", undeclared=_UNDECLARED)
        assert _s(_person(current)).diff(_s(_person(target))) == [
            AlterField(
                "person",
                "score",
                "float",
                1,
                "$value >= 0",
                readonly=True,
                comment="pts",
                permissions={"update": "NONE"},
                default_always=True,
                previous_type="int",
                previous_default=1,
                previous_assertion="$value >= 0",
                previous_readonly=True,
                previous_comment="pts",
                previous_permissions={"update": "NONE"},
                previous_default_always=True,
            )
        ]

    def test_no_alter_field_when_only_undeclared_clauses_differ(self) -> None:
        current = _f("score", "int", assertion="$value >= 0", readonly=True, comment="pts", default="1", default_always=True)
        assert _s(_person(current)).diff(_s(_person(_f("score", "int", undeclared=_UNDECLARED)))) == []

    def test_a_declared_default_keeps_always(self) -> None:
        current = _f("n", "int", default="1", default_always=True)
        target = _f("n", "int", default="2", undeclared=_FIELD_MARKS | {"value"})
        assert _s(_person(current)).diff(_s(_person(target))) == [
            AlterField(
                "person",
                "n",
                "int",
                2,
                default_always=True,
                previous_type="int",
                previous_default=1,
                previous_default_always=True,
            )
        ]

    def test_default_always_without_a_default_is_dropped(self) -> None:
        current = _f("n", "int", default="1", default_always=True)
        target = _f("n", "int", undeclared={"default_always"})
        assert _s(_person(current)).diff(_s(_person(target))) == [
            AlterField("person", "n", "int", previous_type="int", previous_default=1, previous_default_always=True)
        ]

    def test_a_computed_field_stays_computed(self) -> None:
        current = _s(_person(_f("total", "int", computed="a + 1")))
        same = _f("total", "int", value="a+1", undeclared=_FIELD_MARKS | {"default"})
        assert current.diff(_s(_person(same))) == []
        changed = _f("total", "int", value="a + 2", undeclared=_FIELD_MARKS | {"default"})
        assert current.diff(_s(_person(changed))) == [
            AlterField("person", "total", "int", computed="a + 2", previous_type="int", previous_computed="a + 1")
        ]
        assert current.diff(_s(_person(_f("total", "int", undeclared=_UNDECLARED)))) == []

    def test_an_undeclared_default_and_value_are_kept(self) -> None:
        current = _f("n", "int", default="5", value="$value OR 0")
        assert _s(_person(current)).diff(_s(_person(_f("n", "int", undeclared=_UNDECLARED)))) == []

    def test_a_flexible_object_is_kept(self) -> None:
        current = _s(_person(_f("meta", "object", flexible=True)))
        assert current.diff(_s(_person(_f("meta", "object", undeclared=_UNDECLARED)))) == []
        optional = _f("meta", "object", nullable=True, undeclared=_UNDECLARED)
        assert current.diff(_s(_person(optional))) == [
            AlterField("person", "meta", "object", flexible=True, nullable=True, previous_type="object", previous_flexible=True)
        ]

    def test_a_flexible_scalar_is_not_kept(self) -> None:
        current = _f("name", "string", flexible=True)
        assert _s(_person(current)).diff(_s(_person(_f("name", "string", undeclared=_UNDECLARED)))) == [
            AlterField("person", "name", "string", previous_type="string", previous_flexible=True)
        ]

    def test_an_undeclared_reference_with_its_action(self) -> None:
        current = _f("owner", "record<user>", reference=True, on_delete="CASCADE")
        target = _f("owner", "record<user>", nullable=True, undeclared=_UNDECLARED)
        assert _s(_person(current)).diff(_s(_person(target))) == [
            AlterField(
                "person",
                "owner",
                "record<user>",
                reference=True,
                on_delete="CASCADE",
                nullable=True,
                previous_type="record<user>",
                previous_reference=True,
                previous_on_delete="CASCADE",
            )
        ]

    def test_marks_are_ignored_on_a_new_table_and_field(self) -> None:
        target = _s(
            _person(_AGE, undeclared={"drop", "comment"}),
            _t("post", _f("t", "string", table="post", undeclared=_FIELD_MARKS), undeclared={"drop"}),
        )
        assert _s(_person()).diff(target) == [
            CreateTable("post", "SCHEMAFULL", "normal"),
            AddField("post", "t", "string"),
            AddField("person", "age", "int"),
        ]

    def test_the_effective_definitions(self) -> None:
        table = state.effective(_HAND_TABLE, _person(comment="x", undeclared={"permissions", "changefeed", "drop"}))
        assert (table.comment, table.changefeed, table.changefeed_include_original, table.drop) == ("x", "1h", True, True)
        assert table.permissions == {"select": "FULL"}
        unmarked = _person(comment="x")
        assert state.effective(_HAND_TABLE, unmarked) is unmarked
        field = state.effective(_f("a", "int", comment="c"), _f("a", "string", undeclared={"comment"}))
        assert (field.field_type, field.comment) == ("string", "c")
        plain_field = _f("a", "string")
        assert state.effective(_f("a", "int", comment="c"), plain_field) is plain_field
        unreadable = state.effective(_f("x", "frob<", flexible=True), _f("x", "frob<", undeclared={"flexible"}))
        assert unreadable.flexible is False  # a kind the parser cannot read holds no object
        mixed: Any = state.effective
        with pytest.raises(TypeError, match=r"^effective\(\) takes two TableStates or two FieldStates\.$"):
            mixed(_person(), plain_field)


class TestServerFields:
    """Spec §5.9: a ``server_fields`` column (``server=True``) keeps the current ``DEFAULT``, ``VALUE`` and
    ``COMPUTED`` when it has one."""

    @pytest.mark.parametrize(
        "current",
        [
            _f("at", "datetime", default="time::now()"),
            _f("at", "datetime", value="time::now()"),
            _f("at", "datetime", computed="time::now()"),
            _f("at", "datetime", default="time::now()", default_always=True),
        ],
    )
    def test_the_current_value_rule_is_kept(self, current: FieldState) -> None:
        target = _f("at", "datetime", default="d'2020-01-01T00:00:00Z'", server=True, undeclared=_FIELD_MARKS | {"value"})
        assert _s(_person(current)).diff(_s(_person(target))) == []

    def test_a_retype_carries_the_value_rule(self) -> None:
        current = _f("at", "datetime", value="time::now()")
        target = _f("at", "datetime", nullable=True, server=True, undeclared=_UNDECLARED)
        assert _s(_person(current)).diff(_s(_person(target))) == [
            AlterField(
                "person",
                "at",
                "datetime",
                nullable=True,
                value="time::now()",
                previous_type="datetime",
                previous_value="time::now()",
            )
        ]

    def test_without_a_current_rule_the_model_s_default_applies(self) -> None:
        target = _f("at", "int", default="5", server=True, undeclared=_FIELD_MARKS | {"value"})
        assert _s(_person(_f("at", "int"))).diff(_s(_person(target))) == [
            AlterField("person", "at", "int", 5, previous_type="int")
        ]


class TestUnmarkedDiff:
    """C2 (AC-12's unit part): without marks — a state ``DatabaseIntrospector``, a replay or a hand-built state gives —
    every clause is compared."""

    def test_an_alter_field_s_assertion_and_default_are_compared(self) -> None:
        operation = AlterField(
            "person",
            "age",
            "int",
            default=21,
            assertion="$value >= 18",
            previous_type="int",
            previous_default=18,
            previous_assertion="$value >= 0",
        )
        before = _replay(CreateTable("person"), AddField("person", "age", "int", default=18, assertion="$value >= 0"))
        after = operation.state_forwards(before)
        assert before.diff(after) == [operation]
        assert after.diff(before) == [
            AlterField(
                "person",
                "age",
                "int",
                default=18,
                assertion="$value >= 0",
                previous_type="int",
                previous_default=21,
                previous_assertion="$value >= 18",
            )
        ]

    def test_the_same_states_with_the_target_s_marks_give_nothing(self) -> None:
        current = _s(_person(_f("age", "int", default="21", assertion="$value >= 18"), drop=True))
        target = _s(_person(_f("age", "int", undeclared={"assertion", "default"}), undeclared={"drop"}))
        assert current.diff(target) == []

    def test_a_removed_default(self) -> None:
        operations = _s(_person(_f("n", "int", default="5"))).diff(_s(_person(_f("n", "int"))))
        assert operations == [AlterField("person", "n", "int", previous_type="int", previous_default=5)]

    def test_a_drop_added(self) -> None:
        assert _s(_person()).diff(_s(_person(drop=True))) == [
            AlterTable(
                "person",
                "SCHEMAFULL",
                "normal",
                previous_schema_mode="SCHEMAFULL",
                previous_table_type="normal",
                drop=True,
            )
        ]

    def test_changed_permissions(self) -> None:
        target = _person(comment="hand", drop=True, changefeed="1h", changefeed_include_original=True)
        operations = _s(_HAND_TABLE).diff(_s(target))
        assert len(operations) == 1
        alter = operations[0]
        assert isinstance(alter, AlterTable)
        assert alter.permissions is None
        assert alter.previous_permissions == {"select": "FULL", "create": "NONE", "update": "NONE", "delete": "NONE"}

    def test_a_computed_field_against_a_value_field(self) -> None:
        current = _s(_person(_f("total", "int", computed="a + 1")))
        assert current.diff(_s(_person(_f("total", "int", value="a + 1")))) == [
            AlterField("person", "total", "int", value="a + 1", previous_type="int", previous_computed="a + 1")
        ]

    def test_marks_on_the_current_state_change_nothing(self) -> None:
        current = _s(_person(_f("n", "int", default="5", undeclared={"default"}), undeclared={"comment"}, comment="x"))
        operations = current.diff(_s(_person(_f("n", "int"))))
        assert [type(operation) for operation in operations] == [AlterTable, AlterField]

    def test_states_differing_only_by_their_marks_are_equal(self) -> None:
        current = _s(dataclasses.replace(_HAND_TABLE, fields={"a": _f("a", "int", comment="c")}))
        marked = _s(
            dataclasses.replace(
                current.tables["person"],
                undeclared={"comment", "drop"},
                fields={"a": _f("a", "int", comment="c", server=True, undeclared={"comment"})},
            )
        )
        assert current.diff(marked) == []
        assert marked.diff(current) == []
        assert compare.table_key(current.tables["person"]) == compare.table_key(marked.tables["person"])
        assert compare.field_key(current.tables["person"].fields["a"]) == compare.field_key(marked.tables["person"].fields["a"])
        assert _unmarked(marked) == current


# --- what the diff ignores and what it cannot write --------------------------------------------------------------------


class TestIgnored:
    def test_the_tracking_table(self) -> None:
        tracking = _t(MIGRATIONS_TABLE, _f("name", "string", table=MIGRATIONS_TABLE))
        assert _s(tracking).diff(_s()) == []
        assert _s().diff(_s(tracking)) == []
        assert _s(tracking).diff(_s(dataclasses.replace(tracking, comment="x"))) == []

    def test_a_relation_s_in_and_out(self) -> None:
        likes = _t(
            "likes",
            _f("in", "record<person>", table="likes"),
            _f("out", "record<post>", table="likes"),
            mode="SCHEMALESS",
            table_type="relation",
            relation_in=["person"],
            relation_out=["post"],
        )
        bare = dataclasses.replace(likes, fields={})
        assert _s(likes).diff(_s(bare)) == []
        assert _s(bare).diff(_s(likes)) == []
        assert _s().diff(_s(likes)) == [
            CreateTable("likes", "SCHEMALESS", "relation", relation_in="person", relation_out="post")
        ]

    @pytest.mark.parametrize("flexible", [False, True])
    def test_a_database_side_derived_item_field(self, flexible: bool) -> None:
        derived = _f("k.*", "string", flexible=flexible, permissions={"select": "FULL", "create": "FULL", "update": "FULL"})
        current = _s(_person(_f("k", "array<string>"), derived))
        assert current.diff(_s(_person(_f("k", "array<string>")))) == []
        sets = _s(_person(_f("k", "set<int>", nullable=True), _f("k.*", "int")))
        assert sets.diff(_s(_person(_f("k", "set<int>", nullable=True)))) == []

    @pytest.mark.parametrize(
        "item",
        [
            _f("k.*", "int"),  # another kind than the array's items
            _f("k.*", "string", comment="c"),  # a clause of its own
            _f("k.*", "string", assertion="$value != ''"),
            _f("k.*", "string", permissions={"update": "NONE"}),
        ],
    )
    def test_an_item_field_with_a_definition_of_its_own_is_compared(self, item: FieldState) -> None:
        current = _s(_person(_f("k", "array<string>"), item))
        assert current.diff(_s(_person(_f("k", "array<string>")))) == [DropField("person", "k.*")]

    @pytest.mark.parametrize("parent", [_f("bag", "array"), _f("bag", "array<any>"), _f("bag", "object"), None])
    def test_an_item_field_of_another_parent_is_compared(self, parent: FieldState | None) -> None:
        fields = (_f("bag.*", "any"),) if parent is None else (parent, _f("bag.*", "any"))
        target = () if parent is None else (parent,)
        assert _s(_person(*fields)).diff(_s(_person(*target))) == [DropField("person", "bag.*")]

    def test_an_item_field_of_an_unreadable_parent_is_compared(self) -> None:
        current = _s(_person(_f("k", "frob<"), _f("k.*", "string")))
        assert current.diff(_s(_person(_f("k", "frob<")))) == [DropField("person", "k.*")]

    def test_a_model_side_item_field_is_added(self) -> None:
        target = _s(_person(_f("bag", "array"), _f("bag.*", "any")))
        assert _s(_person(_f("bag", "array"))).diff(target) == [AddField("person", "bag.*", "any")]

    def test_functions_params_other_sections_and_issues(self) -> None:
        current = _s(
            _person(),
            functions={"fn::a": FunctionState("fn::a", (), None, "FULL", None, "")},
            params={"p": ParamState("p", "1", "FULL", None, "")},
            other={"apis": {"/a": "DEFINE API '/a'"}},
            issues=[SchemaIssue("field", "x", "person", "m")],
            applied_migrations=["0001_a"],
            connection="other",
        )
        assert current.diff(_s(_person())) == []
        assert _s(_person()).diff(current) == []


class TestUnrepresentable:
    """Spec §5.7: a current definition no operation can write — an MTREE or COUNT index, an access of another shape,
    an analyzer ``search.Analyzer`` refuses, a name only a database holds — gives a redefinition with
    ``overwrite=True`` and a removal without ``previous`` (both irreversible)."""

    def test_an_mtree_index_redefined(self) -> None:
        current = _s(_person(_f("emb", "array<float>"), indexes=(_i("vec", "emb", mtree=True, dimension=4),)))
        target = _s(_person(_f("emb", "array<float>"), indexes=(_i("vec", "emb", hnsw=True, dimension=4),)))
        assert current.diff(target) == [CreateIndex("person", "vec", ["emb"], hnsw=True, dimension=4, overwrite=True)]

    def test_an_extra_mtree_and_count_index_removed(self) -> None:
        current = _s(_person(_AGE, indexes=(_i("old", "age", mtree=True, dimension=4), _i("n", count=True))))
        assert current.diff(_s(_person(_AGE))) == [DropIndex("person", "n"), DropIndex("person", "old")]

    def test_names_only_a_database_holds(self) -> None:
        events = _t(
            "user-events",
            _f("first-name", "string", table="user-events"),
            indexes=(_i("idx-x", "first-name", table="user-events"),),
            events=(_e("on-x", "{ CREATE a }", table="user-events"),),
        )
        assert _s(events, _t("2024_sales")).diff(_s()) == [DropTable("2024_sales"), DropTable("user-events")]
        assert _s(events).diff(_s(_t("user-events"))) == [
            DropField("user-events", "first-name"),
            DropIndex("user-events", "idx-x"),
            RemoveEvent("on-x", "user-events"),
        ]

    def test_an_access_of_another_shape(self) -> None:
        jwt = AccessState("api", access_type="JWT")
        assert _s(accesses=(jwt,)).diff(_s()) == [RemoveAccess("api")]
        operations = _s(accesses=(dataclasses.replace(jwt, name="account"),)).diff(_s(accesses=(_access(),)))
        assert operations == [_access_op(overwrite=True)]

    def test_an_analyzer_search_analyzer_refuses(self) -> None:
        odd = AnalyzerState("odd", ["blank"], ["edgengram(10,2)"])
        assert _s(analyzers=(odd,)).diff(_s()) == [RemoveAnalyzer("odd")]
        target = _s(analyzers=(AnalyzerState("odd", ["blank"], ["edgengram(2,10)"]),))
        assert _s(analyzers=(odd,)).diff(target) == [DefineAnalyzer("odd", ["blank"], ["edgengram(2,10)"])]

    def test_an_event_schema_event_refuses(self) -> None:
        odd = _e("odd", "{ CREATE a }", async_mode=True, retry=70000)
        assert _s(_person(events=(odd,))).diff(_s(_person())) == [RemoveEvent("odd", "person")]
        operations = _s(_person(events=(odd,))).diff(_s(_person(events=(_e("odd", "{ CREATE b }"),))))
        assert operations == [DefineEvent("odd", "person", None, "CREATE b", overwrite=True)]

    def test_an_unwritable_definition_both_states_hold_alike_gives_nothing(self) -> None:
        holding = _s(
            _person(
                _AGE,
                indexes=(_i("old", "age", mtree=True, dimension=4),),
                events=(_e("odd", "{ a }", async_mode=True, retry=70000),),
            ),
            analyzers=(AnalyzerState("odd", ["blank"], ["edgengram(10,2)"]),),
            accesses=(AccessState("api", access_type="JWT"),),
        )
        assert holding.diff(holding) == []

    def test_a_target_definition_no_operation_writes_is_refused(self) -> None:
        mtree = _i("old", "age", mtree=True, dimension=4)
        with pytest.raises(ValueError, match=r"^SchemaState\.diff\(\): index 'old' on table 'person' of the target"):
            _s(_person(_AGE)).diff(_s(_person(_AGE, indexes=(mtree,))))
        with pytest.raises(ValueError, match=r"^SchemaState\.diff\(\): index 'old' on table 'post' of the target"):
            _s().diff(_s(_t("post", indexes=(dataclasses.replace(mtree, table="post"),))))
        with pytest.raises(ValueError, match=r"^SchemaState\.diff\(\): access 'api' of the target"):
            _s().diff(_s(accesses=(AccessState("api", access_type="JWT"),)))
        with pytest.raises(ValueError, match=r"^SchemaState\.diff\(\): analyzer 'odd' of the target"):
            _s().diff(_s(analyzers=(AnalyzerState("odd", ["blank"], ["edgengram(10,2)"]),)))
        odd = _e("odd", "{ CREATE a }", async_mode=True, retry=70000)
        with pytest.raises(ValueError, match=r"^SchemaState\.diff\(\): event 'odd' on table 'person' of the target"):
            _s(_person()).diff(_s(_person(events=(odd,))))
        with pytest.raises(ValueError, match=r"cannot be written by a migration operation"):
            _s(analyzers=(AnalyzerState("odd", ["blank"]),)).diff(
                _s(analyzers=(AnalyzerState("odd", ["blank"], ["edgengram(10,2)"]),))
            )


# --- the state attributes ----------------------------------------------------------------------------------------------


class TestStateAttributes:
    """Spec §5.9–§5.10 (C4, D22, D28; AC-31's and AC-33's unit parts)."""

    def test_the_field_marks(self) -> None:
        assert FieldState(name="a", field_type="int", server=True).server is True
        field = FieldState(name="a", field_type="int", undeclared=["comment", "default"])
        assert field.undeclared == frozenset({"comment", "default"})
        assert type(field.undeclared) is frozenset
        assert FieldState(name="a", field_type="int").undeclared == frozenset()
        assert "server=True" in repr(FieldState(name="a", field_type="int", server=True))

    def test_the_full_orm_s_6th_positional_argument_is_still_refused(self) -> None:
        any_cls: Any = FieldState
        with pytest.raises(TypeError):
            any_cls("a", "int", True, None, None, False)

    def test_the_table_mark(self) -> None:
        table = TableState("t", undeclared=["comment", "drop"])
        assert table.undeclared == frozenset({"comment", "drop"})
        assert "undeclared=frozenset(" in repr(table)

    @pytest.mark.parametrize("cls", [FieldState, TableState])
    def test_a_bare_str_is_refused(self, cls: Any) -> None:
        arguments = {"field_type": "int"} if cls is FieldState else {}
        with pytest.raises(TypeError, match=r"\.undeclared takes a collection of str, not a str"):
            cls(name="a", undeclared="comment", **arguments)

    def test_an_unknown_key_is_refused_with_the_allowed_ones(self) -> None:
        with pytest.raises(ValueError, match=r"^FieldState\.undeclared: unknown key 'drop'") as field_error:
            FieldState(name="a", field_type="int", undeclared=["comment", "drop"])
        assert "'assertion'" in str(field_error.value)
        with pytest.raises(ValueError, match=r"^TableState\.undeclared: unknown key 'default'") as table_error:
            TableState("t", undeclared=["default"])
        assert "'schema_mode'" in str(table_error.value)

    def test_a_key_that_is_not_a_str_is_refused(self) -> None:
        any_cls: Any = TableState
        with pytest.raises(TypeError, match=r"^TableState\.undeclared holds a int, not a str"):
            any_cls("t", undeclared=[1])
        with pytest.raises(TypeError, match=r"^TableState\.undeclared takes a collection of str, not a int"):
            any_cls("t", undeclared=1)

    def test_applied_migrations(self) -> None:
        assert SchemaState({}, ["0001_a"]).applied_migrations == ("0001_a",)
        assert SchemaState(applied_migrations=["0001_a"]) == SchemaState({}, ["0001_a"])
        assert SchemaState().applied_migrations == ()
        assert type(SchemaState({}, ["0001_a", "0002_b"]).applied_migrations) is tuple
        any_cls: Any = SchemaState
        with pytest.raises(TypeError, match=r"applied_migrations takes a sequence of str, not a str"):
            any_cls({}, "0001_a")
        with pytest.raises(TypeError):
            any_cls({}, [], "default")

    def test_place_and_take_out_keep_the_applied_migrations(self) -> None:
        start = SchemaState({}, ["0001_a"])
        assert state.place(start, TableState("t")).applied_migrations == ("0001_a",)
        assert state.take_out(state.place(start, TableState("t")), "table", "t").applied_migrations == ("0001_a",)


# --- the replays -------------------------------------------------------------------------------------------------------

_E_R1_TAIL = (
    "is neither in the database nor defined by an earlier migration, so SurrealDB would refuse the removal. A RawSQL "
    "that defines it declares that in state_operations=."
)
_START = _s(_person(_AGE, indexes=(_i("by_age", "age"),)))


def _m(name: str, *operations: Operation) -> Migration:
    return Migration(name=name, operations=list(operations))


def _with(**changes: Any) -> SchemaState:
    return dataclasses.replace(_START, **changes)


class TestReplay:
    """Spec §6.3 (C9, D31 as amended, D36, D37; E-R1)."""

    def test_a_file_replay(self) -> None:
        m1 = _m("0001_a", AddField("person", "email", "string"))
        m2 = _m("0002_b", CreateTable("post"), DropIndex("person", "by_age"))
        result = state.replay([m1, m2], start=_with(applied_migrations=["0000_z"]), strict=False)
        assert result.applied_migrations == ("0000_z", "0001_a", "0002_b")
        assert sorted(result.tables) == ["person", "post"]
        assert sorted(result.tables["person"].fields) == ["age", "email"]
        assert result.tables["person"].indexes == {}
        assert state.replay([], strict=True) == SchemaState()

    def test_strict_has_no_default(self) -> None:
        replay: Any = state.replay
        advance: Any = state.advance
        with pytest.raises(TypeError):
            replay([_m("0001_a")], start=_START)
        with pytest.raises(TypeError):
            advance(_m("0001_a"), _START)

    def test_advance_appends_the_name(self) -> None:
        result = state.advance(_m("0001_a", AddField("person", "email", "string")), _START, strict=True)
        assert result.applied_migrations == ("0001_a",)
        assert "email" in result.tables["person"].fields

    def test_until_stops_after_that_migration(self) -> None:
        migrations = [_m("0001_a", CreateTable("a")), _m("0002_b", CreateTable("b")), _m("0003_c", CreateTable("c"))]
        result = state.replay(migrations, until="0002_b", strict=False)
        assert sorted(result.tables) == ["a", "b"]
        assert result.applied_migrations == ("0001_a", "0002_b")
        with pytest.raises(ValueError, match=r"^replay\(\): no migration named '0009_x' among those replayed\.$"):
            state.replay(migrations, until="0009_x", strict=False)

    def test_e_r1_for_a_missing_field(self) -> None:
        migrations = [_m("0001_a", AddField("person", "email", "string")), _m("0002_b", DropField("person", "nick"))]
        with pytest.raises(ValueError) as error:
            state.replay(migrations, start=_START, strict=True)
        assert str(error.value) == (
            f"Migration '0002_b', operation 1 (Drop field nick from person): field 'nick' of table 'person' {_E_R1_TAIL}"
        )

    def test_the_plan_s_text(self) -> None:
        with pytest.raises(ValueError) as error:
            state.replay([_m("0002_b", DropField("person", "age"))], start=_s(_person()), strict=True)
        assert str(error.value) == (
            "Migration '0002_b', operation 1 (Drop field age from person): field 'age' of table 'person' is neither in "
            "the database nor defined by an earlier migration, so SurrealDB would refuse the removal. A RawSQL that "
            "defines it declares that in state_operations=."
        )

    @pytest.mark.parametrize(
        ("operation", "what"),
        [
            (DropTable("people"), "table 'people'"),
            (DropTable("user-events"), "table 'user-events'"),
            (DropField("people", "age"), "field 'age' of table 'people'"),
            (DropIndex("person", "by_email"), "index 'by_email' of table 'person'"),
            (DropIndex("people", "by_age"), "index 'by_age' of table 'people'"),
            (RemoveEvent("audit", "person"), "event 'audit' of table 'person'"),
            (RemoveEvent("audit", "people"), "event 'audit' of table 'people'"),
            (RemoveAnalyzer("english"), "analyzer 'english'"),
            (RemoveAccess("account"), "access 'account'"),
            (RemoveFunction("greet"), "function fn::greet"),
        ],
    )
    def test_e_r1_for_each_removal(self, operation: Operation, what: str) -> None:
        migration = _m("0003_c", AddField("person", "email", "string"), operation)
        with pytest.raises(ValueError) as error:
            state.advance(migration, _START, strict=True)
        assert str(error.value) == f"Migration '0003_c', operation 2 ({operation.describe()}): {what} {_E_R1_TAIL}"
        assert state.advance(migration, _START, strict=False).applied_migrations == ("0003_c",)

    def test_a_file_replay_leaves_the_state_unchanged(self) -> None:
        result = state.advance(_m("0002_b", DropField("person", "nick"), DropTable("gone")), _START, strict=False)
        assert result == _with(applied_migrations=("0002_b",))

    def test_a_removal_of_what_an_earlier_operation_placed(self) -> None:
        migrations = [
            _m("0001_a", AddField("person", "nick", "string"), DefineAnalyzer("english", ["blank"])),
            _m(
                "0002_b",
                DropField("person", "nick"),
                RemoveAnalyzer("english"),
                DefineFunction("greet", "RETURN 1;"),
                RemoveFunction("greet"),
                DefineAccess("account", "person", {"email": "$email"}, "email = $email"),
                RemoveAccess("account"),
                DefineEvent("audit", "person", None, "CREATE a"),
                RemoveEvent("audit", "person"),
                DropIndex("person", "by_age"),
                DropTable("person"),
            ),
        ]
        result = state.replay(migrations, start=_START, strict=True)
        assert (result.tables, result.analyzers, result.functions, result.accesses) == ({}, {}, {}, {})

    @pytest.mark.parametrize(
        "issue",
        [
            SchemaIssue("field", "nick", "person", "could not parse its definition"),
            SchemaIssue("index", "x", "person", "m"),
            SchemaIssue("table", "person", None, "m"),
            SchemaIssue("section", "tables", None, "m"),
        ],
    )
    def test_an_issue_of_the_read_hides_a_table_s_definition(self, issue: SchemaIssue) -> None:
        start = _with(issues=[issue])
        for operation in (DropField("person", "nick"), DropIndex("person", "x"), RemoveEvent("e", "person")):
            result = state.advance(_m("0002_b", operation), start, strict=True)
            assert result == dataclasses.replace(start, applied_migrations=("0002_b",))

    def test_a_table_issue_hides_the_table(self) -> None:
        for issue in (SchemaIssue("table", "people", None, "m"), SchemaIssue("section", "tables", None, "m")):
            result = state.advance(_m("0002_b", DropTable("people")), _with(issues=[issue]), strict=True)
            assert "people" not in result.tables

    def test_an_issue_on_another_table_does_not_hide(self) -> None:
        start = _with(issues=[SchemaIssue("field", "nick", "post", "m"), SchemaIssue("table", "post", None, "m")])
        with pytest.raises(ValueError, match=r"field 'nick' of table 'person' is neither"):
            state.advance(_m("0002_b", DropField("person", "nick")), start, strict=True)
        field_issue = _with(issues=[SchemaIssue("field", "a", "people", "m")])
        with pytest.raises(ValueError, match=r"table 'people' is neither"):
            state.advance(_m("0002_b", DropTable("people")), field_issue, strict=True)

    @pytest.mark.parametrize(
        ("operation", "issue"),
        [
            (RemoveAnalyzer("english"), SchemaIssue("analyzer", "english", None, "m")),
            (RemoveAnalyzer("english"), SchemaIssue("section", "analyzers", None, "m")),
            (RemoveAccess("account"), SchemaIssue("access", "account", None, "m")),
            (RemoveAccess("account"), SchemaIssue("section", "accesses", None, "m")),
            (RemoveFunction("greet"), SchemaIssue("function", "fn::greet", None, "m")),
            (RemoveFunction("greet"), SchemaIssue("section", "functions", None, "m")),
        ],
    )
    def test_an_issue_of_its_kind_hides_a_database_definition(self, operation: Operation, issue: SchemaIssue) -> None:
        start = _with(issues=[issue])
        assert state.advance(_m("0002_b", operation), start, strict=True).applied_migrations == ("0002_b",)

    def test_an_issue_of_another_kind_does_not_hide(self) -> None:
        start = _with(issues=[SchemaIssue("access", "english", None, "m"), SchemaIssue("section", "functions", None, "m")])
        with pytest.raises(ValueError, match=r"analyzer 'english' is neither"):
            state.advance(_m("0002_b", RemoveAnalyzer("english")), start, strict=True)

    def test_raw_sql_state_operations_are_not_checked(self) -> None:
        raw = RawSQL(
            "REMOVE FIELD IF EXISTS nick ON person;", state_operations=[DropField("person", "nick"), DropTable("gone")]
        )
        assert state.advance(_m("0002_b", raw), _START, strict=True) == _with(applied_migrations=("0002_b",))

    @pytest.mark.parametrize("strict", [True, False])
    def test_alterations_and_additions_on_missing_definitions_are_placed(self, strict: bool) -> None:
        migration = _m(
            "0002_b",
            AlterField("person", "nick", "string"),
            AddField("post", "title", "string"),
            AlterTable("gone", "SCHEMALESS"),
        )
        result = state.advance(migration, _START, strict=strict)
        assert result.tables["person"].fields["nick"].field_type == "string"
        assert (result.tables["post"].schema_mode, result.tables["post"].table_type) == ("SCHEMALESS", "any")
        assert "title" in result.tables["post"].fields
        assert result.tables["gone"].schema_mode == "SCHEMALESS"

    def test_the_issues_and_the_connection_are_kept(self) -> None:
        start = _with(issues=[SchemaIssue("param", "p", None, "m")], connection="other")
        result = state.advance(_m("0002_b", AddField("person", "email", "string")), start, strict=True)
        assert (result.issues, result.connection) == (start.issues, "other")

    def test_forwards_sql_renders_from_the_file_replay(self) -> None:
        migration = _m("0002_b", DropField("person", "nick"), AddField("person", "bag", "any"))
        schemafull = _s(_person())
        assert migration.forwards_sql(line=2, state=schemafull) == [
            "REMOVE FIELD `nick` ON `person`;",
            "DEFINE FIELD `bag` ON `person` TYPE any FLEXIBLE;",
        ]


class TestRewind:
    """Spec §6.3, §5.6: ``rewind()`` undoes a migration's operations in reverse order and drops its name."""

    @staticmethod
    def _migration() -> Migration:
        previous_event = DefineEvent("audit", "person", None, "CREATE a")
        return _m(
            "0002_b",
            CreateTable("post", comment="Posts"),
            AddField("post", "title", "string"),
            CreateIndex("post", "post_title", ["title"], unique=True),
            AlterField("person", "age", "string", previous_type="int"),
            DefineEvent("audit", "person", None, "CREATE b", previous=previous_event),
            RemoveAnalyzer("english", previous=DefineAnalyzer("english", ["blank"])),
        )

    @staticmethod
    def _start() -> SchemaState:
        return _replay(
            CreateTable("person"),
            AddField("person", "age", "int"),
            DefineEvent("audit", "person", None, "CREATE a"),
            DefineAnalyzer("english", ["blank"]),
            start=SchemaState(applied_migrations=["0001_a"]),
        )

    def test_rewind_gives_back_the_state_before(self) -> None:
        start = self._start()
        advanced = state.advance(self._migration(), start, strict=False)
        assert advanced.applied_migrations == ("0001_a", "0002_b")
        assert advanced.diff(start) != []
        rewound = state.rewind(self._migration(), start=advanced)
        assert rewound.diff(start) == []
        assert start.diff(rewound) == []
        assert rewound.applied_migrations == ("0001_a",)

    def test_two_migrations_rewound_in_undo_order(self) -> None:
        start = self._start()
        second = _m(
            "0003_c",
            AlterTable("post", "SCHEMALESS", "normal", previous_schema_mode="SCHEMAFULL", previous_comment="Posts"),
        )
        advanced = state.replay([self._migration(), second], start=start, strict=True)
        rewound = state.rewind(self._migration(), state.rewind(second, advanced))
        assert rewound.diff(start) == []
        assert rewound.applied_migrations == ("0001_a",)

    def test_an_irreversible_operation_raises(self) -> None:
        with pytest.raises(ValueError, match=r"^Drop table person is not reversible\.$"):
            state.rewind(_m("0002_b", DropTable("person")), self._start())


class TestRoundTripDiff:
    """AC-4 (spec §6.2): for each reversible operation *op* and a state *s* holding what its ``previous``/
    ``previous_*`` describe, ``s.diff(op.state_backwards(op.state_forwards(s)))`` is ``[]`` — Task 3's round trip in
    its ``diff()`` form."""

    @pytest.mark.parametrize(("operation", "start"), _round_trips(), ids=lambda value: type(value).__name__)
    def test_the_round_trip(self, operation: Operation, start: SchemaState) -> None:
        assert start.diff(operation.state_backwards(operation.state_forwards(start))) == []


# --- the models' state: introspect_models(), ModelIntrospector, get_registered_models() -------------------------------


class MiBare(BaseSurrealModel):
    """A node model declaring no table clause."""

    model_config = SurrealConfigDict(table_name="mi_bare")

    id: str | None = None
    label: str = ""


class MiCommented(BaseSurrealModel):
    """A node model declaring its comment only, on a ``TYPE ANY`` table."""

    model_config = SurrealConfigDict(table_name="mi_commented", table_type=TableType.ANY, comment="Commented")

    id: str | None = None
    data: dict[str, Any] = {}


class MiServer(BaseSurrealModel):
    """``server_fields``: one column with a constant default (written), one without."""

    model_config = SurrealConfigDict(
        table_name="mi_server", schema_mode=SchemaMode.SCHEMAFULL, server_fields=["created", "rank"]
    )

    id: str | None = None
    created: datetime | None = None
    rank: int = 0
    note: str = ""


class MiSchemaless(AuthenticatedUserMixin, BaseSurrealModel):
    """A user model declaring its schema mode (``SCHEMALESS``) and its table type."""

    model_config = SurrealConfigDict(
        table_name="mi_schemaless", schema_mode=SchemaMode.SCHEMALESS, table_type=TableType.NORMAL, comment="Members"
    )

    email: str
    password: str


class MiOther(BaseSurrealModel):
    """A model of another connection."""

    model_config = SurrealConfigDict(table_name="mi_other", connection="other")

    id: str | None = None
    label: str = ""


#: Every kind of model v0.34.0 writes: a SCHEMAFULL node with every declarable clause, an index trio with an analyzer
#: and two events (``MigPerson``), a node declaring nothing, a ``TYPE ANY`` node, ``server_fields``, a view, an edge, a
#: SCHEMAFULL user model with a ``primary_key``, a SCHEMALESS user model with and without its ``table_type``, a
#: ``TableType.USER`` model and a user model with refresh tokens.
_MODEL_SET: tuple[type[BaseSurrealModel], ...] = (
    MigPerson,
    MiBare,
    MiCommented,
    MiServer,
    MigStats,
    MigLikes,
    MigUser,
    AdPlain,
    MiSchemaless,
    AdUserTable,
    AdRefresh,
)
#: The keys of ``TableState.undeclared`` for a model declaring none of the table clauses.
_NOTHING_DECLARED = frozenset({"schema_mode", "comment", "changefeed", "permissions", "drop"})


def _model_id(model: type[BaseSurrealModel]) -> str:
    return model.__name__


def _appliers_state(model: type[BaseSurrealModel]) -> SchemaState:
    """The state ``render.read_own()`` gives for the statements a model's appliers write (spec §5.9), without marks:
    the table statement of ``table_ddl()`` (an auth model: of ``access_ddl()``) with ``table_ddl()``'s fields, the
    analyzers and indexes of ``index_ddl(line=3)``, ``event_ddl()``, and an auth model's access statement."""
    ddl = model.table_ddl()
    auth = issubclass(model, AuthenticatedUserMixin)
    table = render.read_own(model.access_ddl()[0] if auth else ddl[0])
    assert isinstance(table, TableState)
    fields = [render.read_own(statement) for statement in ddl[1:]]
    defined = [render.read_own(statement) for statement in model.index_ddl(line=3)]
    events = [render.read_own(statement) for statement in model.event_ddl()]
    accesses = []
    if auth:
        accesses.append(render.read_own(access_statement(model._access_parts(), clause="")))  # type: ignore[attr-defined]
    table = dataclasses.replace(
        table,
        fields={field.name: field for field in fields},
        indexes={index.name: index for index in defined if isinstance(index, IndexState)},
        events={event.name: event for event in events},
    )
    return SchemaState(
        {table.name: table},
        analyzers={analyzer.name: analyzer for analyzer in defined if isinstance(analyzer, AnalyzerState)},
        accesses={access.name: access for access in accesses},
    )


class TestModelState:
    """Spec §5.9: a model's state is what its appliers write on a new table, read back by the parser (M17)."""

    @pytest.mark.parametrize("model", _MODEL_SET, ids=_model_id)
    def test_each_model_is_its_appliers_statements_read_back(self, model: type[BaseSurrealModel]) -> None:
        assert _unmarked(introspect_models([model])) == _appliers_state(model)

    def test_the_node_model(self) -> None:
        table = introspect_models([MigPerson]).tables["mig_person"]
        assert (table.schema_mode, table.table_type, table.comment) == ("SCHEMAFULL", "normal", "People")
        assert (table.changefeed, table.changefeed_include_original) == ("7d", True)
        assert list(table.fields) == sorted(render.read_own(statement).name for statement in MigPerson.table_ddl()[1:])
        assert table.fields["age"].default == "18"
        assert table.fields["full"].value == "string::concat(name, ' ', email)"
        assert set(table.indexes) == {"mig_person_email", "mig_person_bio", "mig_person_emb"}
        assert table.indexes["mig_person_bio"].search_analyzer == "mig_english"
        assert set(table.events) == {"mig_audit", "mig_every"}

    def test_the_analyzer(self) -> None:
        result = introspect_models([MigPerson])
        assert list(result.analyzers) == ["mig_english"]
        assert result.analyzers["mig_english"].filters == ("lowercase", "snowball(english)")
        assert result.accesses == {}

    def test_the_view_and_the_edge(self) -> None:
        result = introspect_models([MigStats, MigLikes])
        view, edge = result.tables["mig_stats"], result.tables["mig_likes"]
        assert view.view_query == "SELECT count() AS n FROM mig_person GROUP ALL"
        assert view.fields == {}
        assert (edge.table_type, edge.relation_in, edge.relation_out, edge.enforced) == (
            "relation",
            ("mig_person",),
            ("mig_person", "mig_page"),
            True,
        )
        assert list(edge.fields) == ["weight"]

    def test_a_primary_key_user_model(self) -> None:
        result = introspect_models([MigUser])
        table, access = result.tables["mig_user"], result.accesses["mig_account"]
        assert table.schema_mode == "SCHEMAFULL"
        assert dict(table.permissions) == {
            "select": "WHERE id = $auth.id",
            "update": "WHERE id = $auth.id",
            "create": "NONE",
            "delete": "NONE",
        }
        assert set(table.fields) == {"email", "password", "name"}
        assert access.table == "mig_user"
        assert list(access.signup_fields.items())[0] == ("email", "$email")
        assert access.signup_fields["id"] == "$email"

    def test_a_schemaless_user_model(self) -> None:
        table = introspect_models([AdPlain]).tables["AdPlain"]
        assert (table.schema_mode, table.table_type) == ("SCHEMALESS", "any")
        assert dict(table.permissions)["select"] == "WHERE id = $auth.id"
        assert set(table.fields) == {"email", "password"}

    def test_several_models(self) -> None:
        result = introspect_models(_MODEL_SET)
        assert set(result.tables) == {model.get_table_name() for model in _MODEL_SET}
        assert set(result.accesses) == {"mig_account", "AdPlain_access", "mi_schemaless_access", "ad_user", "ad_refresh"}
        assert list(result.analyzers) == ["mig_english"]
        assert (result.applied_migrations, result.issues, result.connection) == ((), (), "default")

    def test_the_models_attribute(self) -> None:
        assert ModelIntrospector([MiBare, MiServer]).models == [MiBare, MiServer]
        assert ModelIntrospector(model for model in (MiServer,)).models == [MiServer]
        assert ModelIntrospector([]).introspect() == SchemaState()

    def test_two_models_on_one_table_with_the_same_definition(self) -> None:
        class MiBareAgain(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="mi_bare")

            id: str | None = None
            label: str = ""

        assert introspect_models([MiBare, MiBareAgain]) == introspect_models([MiBare])

    def test_two_models_on_one_table_with_different_definitions(self) -> None:
        class MiBareInt(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="mi_bare")

            id: str | None = None
            label: int = 0

        message = (
            "Models MiBare and MiBareInt both declare table 'mi_bare' with different definitions; pass one of them in models=."
        )
        with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
            introspect_models([MiBare, MiBareInt])

    def test_one_analyzer_object_shared_by_two_models(self) -> None:
        class MiPosts(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_name="mi_posts",
                indexes=[Index("mi_posts_body", "body", search_analyzer=MigPerson.get_indexes()[1].search_analyzer)],
            )

            id: str | None = None
            body: str = ""

        result = introspect_models([MigPerson, MiPosts])
        assert list(result.analyzers) == ["mig_english"]

    def test_one_analyzer_name_with_two_definitions(self) -> None:
        other = Analyzer("mig_english", ["blank"], ["lowercase"])

        class MiNotes(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_name="mi_notes", indexes=[Index("mi_notes_body", "body", search_analyzer=other)]
            )

            id: str | None = None
            body: str = ""

        message = (
            "Analyzer 'mig_english' is declared twice with different definitions (models MigPerson and MiNotes); "
            "declare it once and reference that object."
        )
        with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
            introspect_models([MigPerson, MiNotes])

    def test_one_access_name_with_two_definitions(self) -> None:
        class MiMembers(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="mi_members", access_name="ad_user")

            email: str
            password: str

        message = (
            "Models AdUserTable and MiMembers both declare access 'ad_user' with different definitions; pass one of "
            "them in models=."
        )
        with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
            introspect_models([AdUserTable, MiMembers])

    def test_a_model_refusal_propagates(self) -> None:
        class MiExcluded(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="mi_excluded")

            email: str
            password: str = Field(exclude=True)

        with pytest.raises(TypeError, match="MiExcluded"):
            introspect_models([MiExcluded])

    def test_a_statement_read_to_another_kind_is_a_bug(self) -> None:
        class MiBroken(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="mi_broken")

            id: str | None = None

            @classmethod
            def table_ddl(cls, *, overwrite: bool = True) -> list[str]:
                return ["DEFINE FIELD OVERWRITE `a` ON `mi_broken` TYPE int;"]

        message = (
            "ModelIntrospector: a statement MiBroken writes for its TableState defines something else; this is a bug "
            "of surreal_orm_lite."
        )
        with pytest.raises(RuntimeError, match=f"^{re.escape(message)}$"):
            introspect_models([MiBroken])

    def test_an_index_statement_read_to_another_kind_is_a_bug(self) -> None:
        class MiBrokenIndex(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="mi_broken_index")

            id: str | None = None

            @classmethod
            def index_ddl(cls, *, overwrite: bool = True, line: Literal[2, 3] = 3) -> list[str]:
                return ["DEFINE EVENT OVERWRITE `e` ON `mi_broken_index` THEN { CREATE a };"]

        with pytest.raises(RuntimeError, match=r"^ModelIntrospector: .*MiBrokenIndex writes for its IndexState "):
            introspect_models([MiBrokenIndex])

    @pytest.mark.parametrize("item", [object(), "MiBare", MiBare(), dict])
    def test_an_item_that_is_not_a_model_is_refused(self, item: Any) -> None:
        with pytest.raises(TypeError, match=r"^ModelIntrospector: models= holds .*, not a BaseSurrealModel subclass\.$"):
            ModelIntrospector([MiBare, item])

    def test_a_bare_str_is_refused(self) -> None:
        with pytest.raises(TypeError, match=r"^ModelIntrospector: models= takes an iterable of model classes"):
            introspect_models("MiBare")  # type: ignore[arg-type]

    @pytest.mark.parametrize("connection", [3, b"other", ["other"]])
    def test_a_connection_that_is_not_a_str_is_refused(self, connection: Any) -> None:
        with pytest.raises(TypeError, match=r"^ModelIntrospector: connection= takes a connection name \(str\) or None"):
            ModelIntrospector([MiBare], connection=connection)

    def test_the_connection_filter(self) -> None:
        assert ModelIntrospector([MiBare, MiOther], connection="other").models == [MiOther]
        assert ModelIntrospector([MiBare, MiOther], connection="default").models == [MiBare]
        assert ModelIntrospector([MiBare, MiOther]).models == [MiBare, MiOther]
        result = introspect_models([MiBare, MiOther], connection="other")
        assert (set(result.tables), result.connection) == ({"mi_other"}, "other")
        assert introspect_models([MiBare, MiOther], connection="nowhere") == SchemaState(connection="nowhere")


class TestUndeclared:
    """Spec §5.9, D28: ``TableState.undeclared`` — ``drop`` always, each table clause the model leaves to the
    database, and ``table_type`` for a user model whose ``access_ddl()`` table statement writes no ``TYPE``."""

    @staticmethod
    def _undeclared(model: type[BaseSurrealModel]) -> frozenset[str]:
        return introspect_models([model]).tables[model.get_table_name()].undeclared

    def test_a_node_model_declaring_nothing(self) -> None:
        assert self._undeclared(MiBare) == _NOTHING_DECLARED

    def test_a_declared_comment(self) -> None:
        assert self._undeclared(MiCommented) == _NOTHING_DECLARED - {"comment"}

    def test_every_clause_declared(self) -> None:
        assert self._undeclared(MigPerson) == {"drop"}

    def test_the_view_and_the_edge(self) -> None:
        assert self._undeclared(MigStats) == _NOTHING_DECLARED - {"comment"}
        assert self._undeclared(MigLikes) == _NOTHING_DECLARED

    def test_a_schemafull_user_model_writes_its_permissions(self) -> None:
        assert self._undeclared(MigUser) == {"comment", "changefeed", "drop"}
        assert self._undeclared(AdUserTable) == {"changefeed", "drop"}

    def test_a_schemaless_user_model_without_its_table_type(self) -> None:
        assert self._undeclared(AdPlain) == _NOTHING_DECLARED | {"table_type"}

    def test_a_schemaless_user_model_declaring_its_table_type(self) -> None:
        assert self._undeclared(MiSchemaless) == {"changefeed", "permissions", "drop"}

    def test_drop_on_every_table(self) -> None:
        assert all("drop" in table.undeclared for table in introspect_models(_MODEL_SET).tables.values())


class TestFieldMarks:
    """Spec §5.9, AC-33: ``FieldState.undeclared`` — the clauses a model cannot declare (F8) on every field,
    ``default`` without a ``DEFAULT``, ``value`` without a ``VALUE`` — and ``FieldState.server`` on the
    ``server_fields`` columns; states not built from models carry none."""

    @staticmethod
    def _fields(model: type[BaseSurrealModel]) -> dict[str, FieldState]:
        return dict(introspect_models([model]).tables[model.get_table_name()].fields)

    def test_every_field_holds_what_models_cannot_declare(self) -> None:
        fields = [field for table in introspect_models(_MODEL_SET).tables.values() for field in table.fields.values()]
        assert len(fields) > 30
        assert all(field.undeclared >= _FIELD_MARKS for field in fields)
        assert all(field.undeclared - _FIELD_MARKS <= {"default", "value"} for field in fields)

    def test_default(self) -> None:
        fields = self._fields(MigPerson)
        assert "default" not in fields["age"].undeclared
        assert "default" in fields["name"].undeclared
        assert all(("default" in field.undeclared) is (field.default is None) for field in fields.values())

    def test_value(self) -> None:
        fields = self._fields(MigPerson)
        assert fields["full"].undeclared == _FIELD_MARKS | {"default"}
        assert fields["age"].undeclared == _FIELD_MARKS | {"value"}
        assert all(("value" in field.undeclared) is (field.value is None) for field in fields.values())

    def test_server_exactly_on_server_fields(self) -> None:
        fields = self._fields(MiServer)
        assert {name for name, field in fields.items() if field.server} == {"created", "rank"}
        assert fields["rank"].default == "0"
        assert "default" not in fields["rank"].undeclared

    def test_a_computed_field_is_not_a_server_field(self) -> None:
        assert MigPerson.get_server_fields() == {"full"}
        assert not any(field.server for field in self._fields(MigPerson).values())

    def test_states_not_built_from_models_carry_none(self) -> None:
        read = [
            render.read_own("DEFINE FIELD OVERWRITE `age` ON `t` TYPE int DEFAULT 18;"),
            parse_define_field("DEFINE FIELD age ON t TYPE int DEFAULT 18 PERMISSIONS FULL").state,
            FieldState("age", "int"),
        ]
        assert all(isinstance(field, FieldState) for field in read)
        assert all((field.undeclared, field.server) == (frozenset(), False) for field in read)
        tables = [render.read_own(MiBare.table_ddl()[0]), parse_define_table("DEFINE TABLE t TYPE NORMAL SCHEMAFULL")]
        assert all(table.undeclared == frozenset() for table in tables)

    def test_the_marks_are_ignored_by_the_comparator(self) -> None:
        marked = introspect_models([MigPerson]).tables["mig_person"]
        unmarked = _unmarked(introspect_models([MigPerson])).tables["mig_person"]
        assert compare.table_key(marked) == compare.table_key(unmarked)
        assert all(
            compare.field_key(field) == compare.field_key(unmarked.fields[name]) for name, field in marked.fields.items()
        )


class TestRoundTrip:
    """AC-4: the operations the diff from an empty state gives for the models — what ``makemigrations()`` writes for a
    new database —, replayed, give a state that diffs ``[]`` against the models' state in both directions, marks
    removed; their statements are the appliers' (spec §5.4's golden equality, as sets per kind)."""

    def test_the_replay_diffs_empty(self) -> None:
        target = introspect_models(_MODEL_SET)
        replayed = _replay(*SchemaState().diff(target))
        assert replayed.diff(target) == []
        unmarked = _unmarked(target)
        assert replayed.diff(unmarked) == []
        assert unmarked.diff(replayed) == []

    def test_a_new_table_leaves_its_undeclared_mode_and_type_to_surrealdb(self) -> None:
        bare = _t("bare", mode="SCHEMALESS", table_type="any", undeclared={"schema_mode", "table_type", "drop"})
        assert _s().diff(_s(bare)) == [CreateTable("bare", schema_mode=None, table_type=None)]
        assert CreateTable("bare", schema_mode=None, table_type=None).forwards() == "DEFINE TABLE `bare`;"
        declared = dataclasses.replace(bare, undeclared={"drop"})
        assert _s().diff(_s(declared)) == [CreateTable("bare", schema_mode="SCHEMALESS", table_type="any")]
        # The replay reads SurrealDB's defaults back: nothing left to do.
        assert _replay(*_s().diff(_s(bare))).diff(_s(bare)) == []

    def test_every_model_kind_is_written(self) -> None:
        operations = SchemaState().diff(introspect_models(_MODEL_SET))
        kinds = {type(operation).__name__ for operation in operations}
        assert kinds == {"DefineAnalyzer", "CreateTable", "AddField", "CreateIndex", "DefineEvent", "DefineAccess"}
        assert sum(isinstance(operation, DefineAccess) for operation in operations) == 5

    @pytest.mark.parametrize("model", [MigPerson, MiBare, MiCommented, MiServer, MigStats, MigLikes], ids=_model_id)
    def test_the_statements_are_the_appliers(self, model: type[BaseSurrealModel]) -> None:
        statements = [operation.forwards() for operation in SchemaState().diff(introspect_models([model]))]
        ddl = model.table_ddl()
        appliers = [*ddl, *model.index_ddl(), *model.event_ddl()]
        kept = [statement for statement in appliers if statement.startswith("DEFINE ANALYZER")]
        plain = [render.plain(statement) for statement in appliers if not statement.startswith("DEFINE ANALYZER")]
        assert statements[len(kept)] == render.plain(ddl[0])
        for kind in ("ANALYZER", "TABLE", "FIELD", "INDEX", "EVENT"):
            expected = [statement for statement in (*kept, *plain) if statement.startswith(f"DEFINE {kind} ")]
            assert sorted(statement for statement in statements if statement.startswith(f"DEFINE {kind} ")) == sorted(
                expected
            ), kind

    def test_an_access_is_the_model_access(self) -> None:
        operations = SchemaState().diff(introspect_models([MigUser]))
        access = [operation for operation in operations if isinstance(operation, DefineAccess)]
        assert len(access) == 1
        # The SET items in the state's order (the id item first, then by name): the same access for the comparator (M19).
        model_access = render.read_own(access_statement(MigUser._access_parts(), clause=""))
        assert compare.access_key(_own(access[0].forwards())) == compare.access_key(model_access)
        assert operations[0].forwards() == render.plain(MigUser.access_ddl()[0])


_MODULES = itertools.count()


@pytest.fixture
def model_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Defines models in a module created for the test (``types.ModuleType``), registered in ``sys.modules`` while
    the test runs: ``model_module(source)`` runs *source* in it and returns it."""

    def define(source: str) -> types.ModuleType:
        module = types.ModuleType(f"mig_registry_{next(_MODULES)}")
        monkeypatch.setitem(sys.modules, module.__name__, module)
        exec(textwrap.dedent(source), module.__dict__)
        return module

    return define


def _of(module: types.ModuleType, models: list[type[BaseSurrealModel]]) -> list[type[BaseSurrealModel]]:
    """The classes of *models* defined in *module*, in their order."""
    return [model for model in models if model.__module__ == module.__name__]


_ORDER = """
    from surreal_orm_lite import BaseSurrealEdge, BaseSurrealModel, SurrealConfigDict

    class A(BaseSurrealModel):
        model_config = SurrealConfigDict(table_name="mi_walk_a")
        id: str | None = None

    class _Base(BaseSurrealModel):
        model_config = SurrealConfigDict(table_name="mi_walk_base")
        id: str | None = None

    class B(BaseSurrealModel):
        model_config = SurrealConfigDict(table_name="mi_walk_b")
        id: str | None = None

    class C(_Base):
        model_config = SurrealConfigDict(table_name="mi_walk_c")
"""


class TestRegistryWalk:
    """D18, D33, C11, AC-32: ``get_registered_models()`` walks the model classes depth first, without a registry."""

    def test_a_new_list_on_each_call(self) -> None:
        first = get_registered_models()
        assert first
        assert get_registered_models() is not first
        first.clear()
        assert get_registered_models()

    def test_the_library_classes_are_not_models(self) -> None:
        models = get_registered_models()
        assert BaseSurrealModel not in models
        assert BaseSurrealEdge not in models
        assert not any(model.__module__.split(".")[0] == "surreal_orm_lite" for model in models)
        assert MigPerson in models and MigLikes in models and MigUser in models

    def test_the_top_level_export(self) -> None:
        import surreal_orm_lite.migrations as migrations
        import surreal_orm_lite.model_base as model_base

        assert surreal_orm_lite.get_registered_models is model_base.get_registered_models
        assert "get_registered_models" in surreal_orm_lite.__all__
        assert "get_registered_models" not in migrations.__all__
        assert not hasattr(migrations, "get_registered_models")

    def test_a_module_created_for_the_test(self, model_module: Any) -> None:
        module = model_module(_ORDER)
        assert _of(module, get_registered_models()) == [module.A, module.C, module.B]

    def test_an_edge_subclassing_the_library_edge(self, model_module: Any) -> None:
        module = model_module(
            """
            from surreal_orm_lite import BaseSurrealEdge, SurrealConfigDict

            class Follows(BaseSurrealEdge):
                model_config = SurrealConfigDict(table_name="mi_walk_follows")

            class _Weighted(BaseSurrealEdge):
                weight: int = 1

            class Rates(_Weighted):
                model_config = SurrealConfigDict(table_name="mi_walk_rates")
            """
        )
        assert _of(module, get_registered_models()) == [module.Follows, module.Rates]

    def test_a_class_with_two_model_bases_is_listed_once(self, model_module: Any) -> None:
        module = model_module(
            """
            from surreal_orm_lite import BaseSurrealModel, SurrealConfigDict

            class A(BaseSurrealModel):
                id: str | None = None
                a: int = 0

            class B(BaseSurrealModel):
                id: str | None = None
                b: int = 0

            class D(A, B):
                model_config = SurrealConfigDict(table_name="mi_walk_d")
            """
        )
        assert _of(module, get_registered_models()) == [module.A, module.D, module.B]

    def test_models_none_reads_the_registry(self, model_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        module = model_module(_ORDER)
        assert _of(module, ModelIntrospector().models) == [module.A, module.C, module.B]
        assert _of(module, ModelIntrospector(models=None).models) == [module.A, module.C, module.B]
        # The whole suite's models would be read: keep this module's, through the function the introspector calls.
        walk = introspector.get_registered_models
        monkeypatch.setattr(introspector, "get_registered_models", lambda: _of(module, walk()))
        expected = {"mi_walk_a", "mi_walk_c", "mi_walk_b"}
        assert set(introspect_models().tables) == expected
        assert set(introspect_models(None).tables) == expected
        assert set(ModelIntrospector(models=None).introspect().tables) == expected

    def test_models_none_with_a_connection(self, model_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        module = model_module(
            """
            from surreal_orm_lite import BaseSurrealModel, SurrealConfigDict

            class Here(BaseSurrealModel):
                model_config = SurrealConfigDict(table_name="mi_walk_here")
                id: str | None = None

            class There(BaseSurrealModel):
                model_config = SurrealConfigDict(table_name="mi_walk_there", connection="other")
                id: str | None = None
            """
        )
        walk = introspector.get_registered_models
        monkeypatch.setattr(introspector, "get_registered_models", lambda: _of(module, walk()))
        assert ModelIntrospector(connection="other").models == [module.There]
        assert set(introspect_models(connection="other").tables) == {"mi_walk_there"}


# === Task 11: schema_diff(), makemigrations() against the database, applied_migrations, exports (spec §5.10, §6.5) ===
#
# E2E over WebSocket and HTTP on the line under test and the embedded engine, each test in a database of its own
# (``migration_database()``, spec §9.2), each choosing its expectation per line from ``cached_major()``.

#: The engines of the E2E classes below.
_KINDS = ["ws", "http", "mem"]


async def _major(client: Any) -> int:
    return 3 if await cached_major(client) >= 3 else 2


async def _run(client: Any, statements: str) -> None:
    """Run *statements* by hand, every status checked."""
    answer = await client.query_raw(statements, {})
    assert "error" not in answer, answer["error"]
    assert all(entry["status"] == "OK" for entry in answer["result"]), answer["result"]


async def _info_table(client: Any, table: str) -> dict[str, Any]:
    info = await client.query(f"INFO FOR TABLE `{table}`;", {})
    assert isinstance(info, dict)
    return info


async def _table_text(client: Any, table: str) -> str:
    info = await client.query("INFO FOR DB;", {})
    assert isinstance(info, dict)
    return str(info["tables"][table])


async def _apply(*models: type[BaseSurrealModel]) -> None:
    """Every applier of each model, in order: ``define_view()`` or ``define_table()``, ``define_access()`` for a user
    model, ``define_indexes()``, ``define_events()``."""
    for model in models:
        if model.model_config.get("view_query"):
            await model.define_view()
        else:
            await model.define_table()
        if issubclass(model, AuthenticatedUserMixin):
            await model.define_access()
        await model.define_indexes()
        await model.define_events()


def _files(directory: Path) -> list[str]:
    """The migration files of *directory*, by name."""
    return sorted(path.stem for path in directory.glob("*.py") if not path.name.startswith("_"))


def _operations(directory: Path, name: str) -> list[Operation]:
    return list(MigrationExecutor(directory).load_migration(name).operations)


def _models_for(line: int) -> list[type[BaseSurrealModel]]:
    """Task 5's model set; refresh tokens (``WITH REFRESH``) need SurrealDB 3.x, so ``AdRefresh`` is left out below."""
    return [model for model in _MODEL_SET if line >= 3 or model is not AdRefresh]


class SdItem(BaseSurrealModel):
    """Two required columns: ``define_table()`` writes their ``TYPE`` only."""

    model_config = SurrealConfigDict(table_name="sd_item", schema_mode=SchemaMode.SCHEMAFULL)

    id: str | None = None
    age: int


class TestSchemaDiffE2E:
    """AC-8 (``schema_diff()``'s half): ``[]`` when the models and the database agree — after their appliers, after
    ``migrate()`` —; a hand redefinition the models describe differently gives its operation, one they cannot declare
    is carried (C2)."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_after_the_appliers(self, kind: str) -> None:
        async with migration_database(kind) as client:
            models = _models_for(await _major(client))
            await _apply(*models)
            assert await schema_diff(models=models) == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_after_migrate(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            models = _models_for(await _major(client))
            assert await makemigrations("initial", migrations_dir=tmp_path, models=models) is not None
            assert await MigrationExecutor(tmp_path).migrate() == ["0001_initial"]
            assert await schema_diff(models=models) == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_type_changed_by_hand(self, kind: str) -> None:
        async with migration_database(kind) as client:
            await SdItem.define_table()
            await _run(client, "DEFINE FIELD OVERWRITE age ON sd_item TYPE string;")
            operations = await schema_diff(models=[SdItem])
            assert len(operations) == 1
            operation = operations[0]
            assert isinstance(operation, AlterField)
            assert (operation.table, operation.name, operation.field_type) == ("sd_item", "age", "int")
            assert operation.previous_type == "string"

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_assert_set_by_hand_is_carried(self, kind: str) -> None:
        async with migration_database(kind) as client:
            await SdItem.define_table()
            before = await DatabaseIntrospector().introspect()
            await _run(client, "DEFINE FIELD OVERWRITE age ON sd_item TYPE int ASSERT $value >= 0;")
            assert await schema_diff(models=[SdItem]) == []
            # Two database states carry no mark: every clause is compared (C2).
            after = await DatabaseIntrospector().introspect()
            operations = before.diff(after)
            assert len(operations) == 1 and isinstance(operations[0], AlterField)
            assert operations[0].assertion is not None and "$value >= 0" in operations[0].assertion


class FdItem(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="fd_item", schema_mode=SchemaMode.SCHEMAFULL)

    id: str | None = None
    name: str = ""
    age: int = 0


class FdItemChanged(BaseSurrealModel):
    """``FdItem`` with ``age`` retyped and a column added."""

    model_config = SurrealConfigDict(table_name="fd_item", schema_mode=SchemaMode.SCHEMAFULL)

    id: str | None = None
    name: str = ""
    age: float = 0.0
    email: str = ""


def _e_r1(name: str, position: int, operation: Operation, what: str) -> str:
    return (
        f"Migration '{name}', operation {position} ({operation.describe()}): {what} is neither in the database nor "
        "defined by an earlier migration, so SurrealDB would refuse the removal. A RawSQL that defines it declares that "
        "in state_operations=."
    )


class TestFromDbE2E:
    """AC-19: ``makemigrations()`` reads the current state from the database by default — the applied migrations' and
    the appliers' definitions, the pending files replayed over them strictly (§5.10, C9, D31)."""

    def test_from_db_is_the_default(self) -> None:
        assert inspect.signature(makemigrations).parameters["from_db"].default is True

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_nothing_right_after_migrate(self, kind: str, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        async with migration_database(kind):
            assert await makemigrations("initial", migrations_dir=tmp_path, models=[FdItem]) is not None
            assert await MigrationExecutor(tmp_path).migrate() == ["0001_initial"]
            caplog.set_level(logging.INFO, logger="surreal_orm_lite.migrations")
            assert await makemigrations("again", migrations_dir=tmp_path, models=[FdItem]) is None
            assert "No changes detected." in caplog.messages
            assert _files(tmp_path) == ["0001_initial"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_model_change_gives_the_files_operations(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind):
            database_dir, files_dir = tmp_path / "db", tmp_path / "files"
            assert await makemigrations("initial", migrations_dir=database_dir, models=[FdItem]) is not None
            assert await MigrationExecutor(database_dir).migrate() == ["0001_initial"]
            shutil.copytree(database_dir, files_dir)
            from_db = await makemigrations("change", migrations_dir=database_dir, models=[FdItemChanged])
            offline = await makemigrations("change", migrations_dir=files_dir, models=[FdItemChanged], from_db=False)
            assert from_db is not None and offline is not None
            operations = _operations(database_dir, from_db.stem)
            assert operations == _operations(files_dir, offline.stem)
            assert {type(operation) for operation in operations} == {AddField, AlterField}

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_two_calls_before_migrate(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind):
            assert await makemigrations("initial", migrations_dir=tmp_path, models=[FdItem]) is not None
            assert await makemigrations("again", migrations_dir=tmp_path, models=[FdItem]) is None
            assert _files(tmp_path) == ["0001_initial"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_tables_define_table_made_and_a_file_creating_them(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            await FdItem.define_table()
            assert await makemigrations("initial", migrations_dir=tmp_path, models=[FdItem], from_db=False) is not None
            # No tracking table: the file is pending, replayed over the database's definitions without error.
            assert await makemigrations("again", migrations_dir=tmp_path, models=[FdItem]) is None
            assert _files(tmp_path) == ["0001_initial"]
            assert MIGRATIONS_TABLE not in (await client.query("INFO FOR DB;", {}))["tables"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_pending_removal_of_a_missing_field(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind):
            assert await makemigrations("initial", migrations_dir=tmp_path, models=[FdItem]) is not None
            assert await MigrationExecutor(tmp_path).migrate() == ["0001_initial"]
            drop = DropField("fd_item", "nope")
            MigrationGenerator(tmp_path).generate("drop_nope", [drop], ["0001_initial"])
            with pytest.raises(ValueError) as raised:
                await makemigrations("next", migrations_dir=tmp_path, models=[FdItemChanged])
            assert str(raised.value) == _e_r1("0002_drop_nope", 1, drop, "field 'nope' of table 'fd_item'")
            assert _files(tmp_path) == ["0001_initial", "0002_drop_nope"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_pending_alteration_of_a_missing_field_is_placed(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind):
            assert await makemigrations("initial", migrations_dir=tmp_path, models=[FdItem]) is not None
            assert await MigrationExecutor(tmp_path).migrate() == ["0001_initial"]
            alter = AlterField("fd_item", "email", "string", default="")
            MigrationGenerator(tmp_path).generate("email", [alter], ["0001_initial"])
            # OVERWRITE defines it: the replay places it, and the models then declare it as written.
            assert await makemigrations("next", migrations_dir=tmp_path, models=[FdItemChanged]) is not None
            operations = _operations(tmp_path, "0003_next")
            assert [type(operation) for operation in operations] == [AlterField]
            assert operations[0].name == "age"

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_recorded_name_without_a_file(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            await MigrationExecutor(tmp_path).ensure_migrations_table()
            answer = await client.query_raw(f"CREATE `{MIGRATIONS_TABLE}` SET name = $name;", {"name": "0099_gone"})
            assert [entry["status"] for entry in answer["result"]] == ["OK"], answer
            with pytest.raises(ValueError) as raised:
                await makemigrations("initial", migrations_dir=tmp_path, models=[FdItem])
            assert str(raised.value) == (
                f"Migration '0099_gone' is applied but has no file in {tmp_path}: the database's history does not "
                f"match {tmp_path}."
            )
            assert _files(tmp_path) == []


class ExItem(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="ex_item", schema_mode=SchemaMode.SCHEMAFULL)

    id: str | None = None
    name: str = ""


class ExItemEmail(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="ex_item", schema_mode=SchemaMode.SCHEMAFULL)

    id: str | None = None
    name: str = ""
    email: str = ""


class ExLoose(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="ex_loose")

    id: str | None = None
    bag: Any = None


class ExLooseFull(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="ex_loose", schema_mode=SchemaMode.SCHEMAFULL)

    id: str | None = None
    bag: Any = None


def _batches(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The text of every batch the executor sends; the real batch is sent."""
    texts: list[str] = []
    real = executor._send

    async def spy(client: Any, batch: Any, line: int, name: str | None) -> Any:
        texts.append(batch.text())
        return await real(client, batch, line, name)

    monkeypatch.setattr(executor, "_send", spy)
    return texts


class TestExistingTablesE2E:
    """C8 (AC-8 bis, AC-16 +): a table ``define_table()`` made, with no migration file creating it — the default
    ``makemigrations()`` writes what the models changed only, ``migrate()`` applies it."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_field_added_to_the_model(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind):
            await ExItem.define_table()
            path = await makemigrations("add_email", migrations_dir=tmp_path, models=[ExItemEmail])
            assert path is not None and path.stem == "0001_add_email"
            operations = _operations(tmp_path, path.stem)
            assert len(operations) == 1 and isinstance(operations[0], AddField)
            assert (operations[0].table, operations[0].name) == ("ex_item", "email")
            assert await MigrationExecutor(tmp_path).migrate() == [path.stem]
            assert await schema_diff(models=[ExItemEmail]) == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_schemaless_table_made_schemafull(self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        async with migration_database(kind) as client:
            line = await _major(client)
            await ExLoose.define_table()
            await _run(client, "CREATE ex_loose:1 SET bag = {a: 1, b: {c: 2}};")
            path = await makemigrations("schemafull", migrations_dir=tmp_path, models=[ExLooseFull])
            assert path is not None
            operations = _operations(tmp_path, path.stem)
            assert len(operations) == 1 and isinstance(operations[0], AlterTable)
            batches = _batches(monkeypatch)
            assert await MigrationExecutor(tmp_path).migrate() == [path.stem]
            sent = [text for text in batches if "`ex_loose`" in text]
            assert len(sent) == 1
            # Below 3 the batch redefines the any column FLEXIBLE (chosen from the read); on 3.x the table statement only.
            assert ("DEFINE FIELD OVERWRITE `bag` ON `ex_loose`" in sent[0]) is (line < 3)
            bag = (await _info_table(client, "ex_loose"))["fields"]["bag"]
            assert ("FLEXIBLE" in bag) is (line < 3)
            await _run(client, "UPDATE ex_loose:1;")  # a write: the SCHEMAFULL table re-checks the record
            assert await client.query("SELECT VALUE bag FROM ex_loose:1;", {}) == [{"a": 1, "b": {"c": 2}}]
            # The files hold no column of ex_loose: on line 2 the table statement comes alone.
            shown = await MigrationExecutor(tmp_path).show_sql(path.stem, line=2)
            assert shown == operations[0].forwards(line=2)
            assert "DEFINE FIELD" not in shown


def _fake_read(monkeypatch: pytest.MonkeyPatch, read: SchemaState) -> None:
    async def introspect(self: DatabaseIntrospector) -> SchemaState:
        return read

    monkeypatch.setattr(DatabaseIntrospector, "introspect", introspect)


class TestFromDbHistory:
    """AC-19, AC-18 (``makemigrations()``'s row, spec §5.6, §5.10): a fake database read — E-H1, E-H2 and E-H3 raise
    before any file is written. No server."""

    def _write(self, directory: Path) -> None:
        first = MigrationGenerator(directory).generate("a", [CreateTable("hi_a")], []).stem
        MigrationGenerator(directory).generate("b", [CreateTable("hi_b")], [first])

    async def test_an_applied_name_without_a_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._write(tmp_path)
        _fake_read(monkeypatch, SchemaState({}, ["0001_a", "0099_gone"]))
        with pytest.raises(ValueError) as raised:
            await makemigrations("c", migrations_dir=tmp_path, models=[FdItem])
        assert str(raised.value) == (
            f"Migration '0099_gone' is applied but has no file in {tmp_path}: the database's history does not match {tmp_path}."
        )
        assert _files(tmp_path) == ["0001_a", "0002_b"]

    async def test_an_applied_migration_whose_dependency_is_pending(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._write(tmp_path)
        _fake_read(monkeypatch, SchemaState({}, ["0002_b"]))
        with pytest.raises(ValueError) as raised:
            await makemigrations("c", migrations_dir=tmp_path, models=[FdItem])
        assert str(raised.value) == (
            f"Migration '0002_b' is applied but its dependency '0001_a' is not: the database's history does not match "
            f"{tmp_path}."
        )
        assert _files(tmp_path) == ["0001_a", "0002_b"]

    async def test_the_tracking_table_could_not_be_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._write(tmp_path)
        issue = SchemaIssue("table", MIGRATIONS_TABLE, MIGRATIONS_TABLE, "could not read the applied migrations")
        _fake_read(monkeypatch, SchemaState({}, issues=[issue]))
        with pytest.raises(SurrealDbError) as raised:
            await makemigrations("c", migrations_dir=tmp_path, models=[FdItem])
        assert str(raised.value) == (
            "makemigrations() could not read the applied migrations from _surreal_orm_migrations; nothing was written."
        )
        assert _files(tmp_path) == ["0001_a", "0002_b"]

    async def test_another_issue_of_the_tracking_table_is_no_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issue = SchemaIssue("table", MIGRATIONS_TABLE, MIGRATIONS_TABLE, "INFO FOR TABLE failed: boom")
        _fake_read(monkeypatch, SchemaState({}, issues=[issue]))
        path = await makemigrations("initial", migrations_dir=tmp_path, models=[FdItem])
        assert path is not None and path.stem == "0001_initial"

    async def test_the_pending_files_are_replayed_over_the_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._write(tmp_path)
        _fake_read(monkeypatch, SchemaState({}, ["0001_a"]))
        path = await makemigrations("c", migrations_dir=tmp_path, models=[FdItem])
        assert path is not None
        migration = MigrationExecutor(tmp_path).load_migration(path.stem)
        assert list(migration.dependencies) == ["0002_b"]
        # hi_a and hi_b are the database's (applied) and the files' (pending): their removals are left out.
        assert [type(operation) for operation in migration.operations][0] is CreateTable


class UdItem(BaseSurrealModel):
    """A model declaring none of the table's permissions, comment and change feed."""

    model_config = SurrealConfigDict(table_name="ud_item")

    id: str | None = None
    n: int = 0


class UdItemCommented(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="ud_item", comment="x")

    id: str | None = None
    n: int = 0


_UD_HAND = (
    "DEFINE TABLE ud_item TYPE NORMAL SCHEMALESS CHANGEFEED 1h INCLUDE ORIGINAL COMMENT 'hand' PERMISSIONS FOR select FULL;"
)


class TestUndeclaredE2E:
    """AC-28 (the table part, D12): the clauses a model leaves to the database are kept — ``schema_diff()`` ``[]``,
    nothing to write —, and an ``AlterTable`` for a clause the model declares keeps them, forwards and backwards."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_hand_set_clauses_are_kept(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            await _run(client, _UD_HAND)
            await UdItem.define_table()
            assert await schema_diff(models=[UdItem]) == []
            assert await makemigrations("nothing", migrations_dir=tmp_path, models=[UdItem]) is None
            path = await makemigrations("comment", migrations_dir=tmp_path, models=[UdItemCommented])
            assert path is not None
            operations = _operations(tmp_path, path.stem)
            assert len(operations) == 1 and isinstance(operations[0], AlterTable)
            alter = operations[0]
            assert (alter.comment, alter.changefeed, alter.previous_comment) == ("x", "1h INCLUDE ORIGINAL", "hand")
            assert alter.permissions is not None and alter.permissions["select"] == "FULL"
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate() == [path.stem]
            table = parse_define_table(await _table_text(client, "ud_item"))
            assert (table.comment, table.changefeed, table.changefeed_include_original) == ("x", "1h", True)
            assert table.permissions["select"] == "FULL"
            assert await executor.rollback("") == [path.stem]
            table = parse_define_table(await _table_text(client, "ud_item"))
            assert (table.comment, table.changefeed, table.changefeed_include_original) == ("hand", "1h", True)
            assert table.permissions["select"] == "FULL"


class CrItem(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="cr_item", schema_mode=SchemaMode.SCHEMAFULL)

    id: str | None = None
    n: int


class CrItemFloat(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="cr_item", schema_mode=SchemaMode.SCHEMAFULL)

    id: str | None = None
    n: float


class CpItem(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="cp_item", schema_mode=SchemaMode.SCHEMAFULL)

    id: str | None = None
    a: str = ""
    full: Computed[str] = computed("string::concat(a, 'x')")


class CpItemChanged(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="cp_item", schema_mode=SchemaMode.SCHEMAFULL)

    id: str | None = None
    a: str = ""
    full: Computed[str] = computed("string::concat(a, 'y')")


class DrItem(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="dr_item", comment="a")

    id: str | None = None
    n: int = 0


class DrItemChanged(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="dr_item", comment="b")

    id: str | None = None
    n: int = 0


class TestCarriedE2E:
    """AC-28 (the field part, D12): a clause set by hand that the models cannot declare is kept through the operation
    the models' change writes — each file then applied by ``migrate()``."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_default_always_through_a_type_change(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            await CrItem.define_table()
            await _run(client, "DEFINE FIELD OVERWRITE n ON cr_item TYPE int DEFAULT ALWAYS 1;")
            assert await schema_diff(models=[CrItem]) == []
            path = await makemigrations("float", migrations_dir=tmp_path, models=[CrItemFloat])
            assert path is not None
            operations = _operations(tmp_path, path.stem)
            assert len(operations) == 1 and isinstance(operations[0], AlterField)
            assert operations[0].default_always is True
            assert await MigrationExecutor(tmp_path).migrate() == [path.stem]
            field = parse_define_field((await _info_table(client, "cr_item"))["fields"]["n"]).state
            assert (field.field_type, field.default, field.default_always) == ("float", "1", True)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_computed_field_stays_computed(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            if await _major(client) < 3:
                # Below 3, COMPUTED is not parsed: a hand-written AddField(computed=…) is refused before any DDL (E-D3).
                operation = AddField("cp_item", "c", "int", computed="1")
                name = MigrationGenerator(tmp_path).generate("computed", [operation], []).stem
                with pytest.raises(SurrealDbError) as raised:
                    await MigrationExecutor(tmp_path).migrate()
                assert str(raised.value) == (
                    f"Migration '{name}', operation 1 ({operation.describe()}): COMPUTED fields need SurrealDB 3.x; "
                    "this connection runs SurrealDB 2.x or the embedded engine."
                )
                return
            await CpItem.define_table()
            await _run(client, "DEFINE FIELD OVERWRITE full ON cp_item TYPE option<string> COMPUTED string::concat(a, 'x');")
            assert await schema_diff(models=[CpItem]) == []
            path = await makemigrations("computed", migrations_dir=tmp_path, models=[CpItemChanged])
            assert path is not None
            operations = _operations(tmp_path, path.stem)
            assert len(operations) == 1 and isinstance(operations[0], AlterField)
            assert await MigrationExecutor(tmp_path).migrate() == [path.stem]
            field = parse_define_field((await _info_table(client, "cp_item"))["fields"]["full"]).state
            assert field.computed == "string::concat(a, 'y')"
            assert field.value is None

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_drop_table_stays_drop(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            await DrItem.define_table()
            await _run(client, "DEFINE TABLE OVERWRITE dr_item TYPE NORMAL DROP SCHEMALESS COMMENT 'a' PERMISSIONS NONE;")
            assert await schema_diff(models=[DrItem]) == []
            path = await makemigrations("comment", migrations_dir=tmp_path, models=[DrItemChanged])
            assert path is not None
            operations = _operations(tmp_path, path.stem)
            assert len(operations) == 1 and isinstance(operations[0], AlterTable)
            assert operations[0].drop is True
            assert await MigrationExecutor(tmp_path).migrate() == [path.stem]
            table = parse_define_table(await _table_text(client, "dr_item"))
            assert (table.drop, table.comment) == (True, "b")


class NmItem(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="nm_item")

    id: str | None = None


class TestNamesE2E:
    """AC-29 (D26): definitions under names SurrealDB accepts only in backticks appear as removals, never an error."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_non_identifier_names(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            await NmItem.define_table()
            await _run(
                client,
                "DEFINE TABLE `user-events` SCHEMAFULL; DEFINE FIELD `first-name` ON `user-events` TYPE string; "
                "DEFINE INDEX `idx-x` ON `user-events` FIELDS `first-name`; DEFINE TABLE `2024_sales`;",
            )
            operations = await schema_diff(models=[NmItem])
            assert sorted(operation.describe() for operation in operations) == [
                "Drop table '2024_sales'",
                "Drop table 'user-events'",
            ]
            assert await makemigrations("keep", migrations_dir=tmp_path, models=[NmItem]) is None
            path = await makemigrations("drop", migrations_dir=tmp_path, models=[NmItem], drop_missing=True, from_db=True)
            assert path is not None
            assert {operation.describe() for operation in _operations(tmp_path, path.stem)} == {
                "Drop table '2024_sales'",
                "Drop table 'user-events'",
            }
            assert await MigrationExecutor(tmp_path).migrate() == [path.stem]
            tables = set((await client.query("INFO FOR DB;", {}))["tables"])
            assert not {"user-events", "2024_sales"} & tables
            assert await schema_diff(models=[NmItem]) == []


class DfItem(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="df_item", schema_mode=SchemaMode.SCHEMAFULL)

    id: str | None = None
    sep: str = "a;b"


class DfBlock(BaseSurrealModel):
    """``x`` retyped: the database's ``DEFAULT`` block, which no model can declare, is carried into the file."""

    model_config = SurrealConfigDict(table_name="df_block")

    id: str | None = None
    x: float


class TestDefaultsE2E:
    """AC-30 (D23): a default holding ``;`` — the model's literal, the database's block — is written, loads and
    applies."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_literal_default_holding_a_semicolon(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind):
            path = await makemigrations("initial", migrations_dir=tmp_path, models=[DfItem])
            assert path is not None
            assert "default='a;b'" in path.read_text(encoding="utf-8")
            assert await MigrationExecutor(tmp_path).migrate() == [path.stem]
            assert await schema_diff(models=[DfItem]) == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_database_block_default(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            await _run(
                client,
                "DEFINE TABLE df_block SCHEMALESS; DEFINE FIELD x ON df_block TYPE int DEFAULT { LET $x = 1; RETURN $x; };",
            )
            path = await makemigrations("block", migrations_dir=tmp_path, models=[DfBlock])
            assert path is not None
            assert "DefaultExpression(" in path.read_text(encoding="utf-8")
            operations = _operations(tmp_path, path.stem)
            alter = next(operation for operation in operations if isinstance(operation, AlterField))
            assert isinstance(alter.default, DefaultExpression)
            # Applied on a database without the table: the field statement defines it.
            await _run(client, "REMOVE TABLE df_block;")
            assert await MigrationExecutor(tmp_path).migrate() == [path.stem]
            await _run(client, "CREATE df_block:1;")
            assert await client.query("SELECT VALUE x FROM df_block:1;", {}) == [1.0]


class TestAppliedMigrationsE2E:
    """AC-31, AC-33 (the database part): ``DatabaseIntrospector().introspect().applied_migrations`` lists the applied
    names in order — whatever ``tables=`` says —, ``()`` without the tracking table; a read carries no mark."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_applied_names(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind):
            assert (await DatabaseIntrospector().introspect()).applied_migrations == ()
            first = MigrationGenerator(tmp_path).generate("a", [CreateTable("am_t", "SCHEMAFULL")], []).stem
            second = MigrationGenerator(tmp_path).generate("b", [AddField("am_t", "n", "int")], [first]).stem
            assert await MigrationExecutor(tmp_path).migrate() == [first, second]
            read = await DatabaseIntrospector().introspect()
            assert read.applied_migrations == (first, second)
            assert (await DatabaseIntrospector(tables=["am_t"]).introspect()).applied_migrations == (first, second)
            assert list(read.issues) == []
            for table in read.tables.values():
                assert table.undeclared == frozenset()
                for column in table.fields.values():
                    assert (column.undeclared, column.server) == (frozenset(), False)
            replayed = MigrationExecutor(tmp_path).migration_state()
            assert replayed.applied_migrations == (first, second)
            assert all(table.undeclared == frozenset() for table in replayed.tables.values())


class _AppliedClient:
    """An SDK-like client whose database lists the tracking table; the applied names' read answers *answer* (an
    exception is raised)."""

    def __init__(self, answer: object) -> None:
        self.answer = answer
        self.statements: list[str] = []

    async def query(self, sql: str, vars: object = None) -> object:
        self.statements.append(sql)
        if sql == "INFO FOR DB;":
            return {"tables": {MIGRATIONS_TABLE: f"DEFINE TABLE {MIGRATIONS_TABLE} TYPE NORMAL SCHEMAFULL PERMISSIONS NONE"}}
        if sql.startswith("INFO FOR TABLE"):
            return {"events": {}, "fields": {}, "indexes": {}, "lives": {}, "tables": {}}
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def _client_of(monkeypatch: pytest.MonkeyPatch, client: object) -> None:
    async def get_client(*args: object, **kwargs: object) -> object:
        return client

    monkeypatch.setattr(SurrealDBConnectionManager._manager_for("default"), "get_client", get_client)


class TestAppliedMigrationsReader:
    """Spec §5.10 (D22): the reader's error rules, with a fake client — no server."""

    async def test_the_names_in_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _AppliedClient([{"name": "0001_a", "applied_at": "x"}, {"name": "0002_b", "applied_at": "y"}])
        _client_of(monkeypatch, client)
        read = await DatabaseIntrospector(tables=[]).introspect()
        assert read.applied_migrations == ("0001_a", "0002_b")
        assert client.statements == [
            "INFO FOR DB;",
            f"SELECT name, applied_at FROM `{MIGRATIONS_TABLE}` ORDER BY applied_at, name;",
        ]

    async def test_a_server_failure_is_an_issue(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _client_of(monkeypatch, _AppliedClient(_sdk.ServerError("Internal", "boom")))
        read = await DatabaseIntrospector().introspect()
        assert read.applied_migrations == ()
        assert [(issue.kind, issue.name, issue.message) for issue in read.issues] == [
            ("table", MIGRATIONS_TABLE, "could not read the applied migrations")
        ]

    async def test_an_answer_that_is_not_a_list_is_an_issue(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _client_of(monkeypatch, _AppliedClient("nope"))
        read = await DatabaseIntrospector().introspect()
        assert read.applied_migrations == ()
        assert [issue.message for issue in read.issues] == ["could not read the applied migrations"]

    async def test_a_refusal_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _client_of(monkeypatch, _AppliedClient(_sdk.ServerError("Internal", "Not enough permissions to perform this action")))
        with pytest.raises(SurrealDbAuthenticationError):
            await DatabaseIntrospector().introspect()

    async def test_a_transport_error_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _client_of(monkeypatch, _AppliedClient(ConnectionResetError("gone")))
        with pytest.raises(ConnectionResetError):
            await DatabaseIntrospector().introspect()


class NcItem(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="nc_item", connection="mig_other")

    id: str | None = None
    n: int = 0


class NcHere(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="nc_here")

    id: str | None = None


class TestNamedConnectionE2E:
    """AC-20: ``connection="mig_other"`` (a second database) — the executor, ``makemigrations()`` and
    ``schema_diff()`` touch only it; a model of another connection is left out."""

    @pytest.mark.parametrize("kind", ["ws", "http"])
    async def test_a_second_database(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            other = new_database_name()
            SurrealDBConnectionManager.add_connection(
                "mig_other", url=_migration_url(kind), user="root", password="root", namespace="ns", database=other
            )
            try:
                models = [NcItem, NcHere]
                path = await makemigrations("initial", migrations_dir=tmp_path, models=models, connection="mig_other")
                assert path is not None
                assert [operation.describe() for operation in _operations(tmp_path, path.stem)][0] == "Create table nc_item"
                assert not any("nc_here" in operation.describe() for operation in _operations(tmp_path, path.stem))
                executor = MigrationExecutor(tmp_path, connection="mig_other")
                assert await executor.migrate() == [path.stem]
                assert await schema_diff(models=models, connection="mig_other") == []
                assert await makemigrations("again", migrations_dir=tmp_path, models=models, connection="mig_other") is None
                assert (await DatabaseIntrospector("mig_other").introspect()).applied_migrations == (path.stem,)
                # The default connection's database was not touched.
                assert (await client.query("INFO FOR DB;", {}))["tables"] == {}
                assert [operation.describe() for operation in await schema_diff(models=models)] == ["Create table nc_here"]
            finally:
                async with SurrealDBConnectionManager.using("mig_other"):
                    cleanup = await SurrealDBConnectionManager.get_client()
                    await cleanup.query(f"REMOVE DATABASE IF EXISTS {other};", {})


class TestGenerateSkipsTrackingTableE2E:
    """AC-23 (D19): ``generate_models_from_db()`` and ``ModelCodeGenerator`` skip the tracking table, with a comment
    line."""

    _LINE = "# _surreal_orm_migrations: the migrations' tracking table (not modelled)"

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_generate_models_from_db(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind):
            MigrationGenerator(tmp_path).generate("a", [CreateTable("gs_t", "SCHEMAFULL"), AddField("gs_t", "n", "int")], [])
            assert await MigrationExecutor(tmp_path).migrate() == ["0001_a"]
            source = await generate_models_from_db()
            assert self._LINE in source.splitlines()
            assert MIGRATIONS_TABLE not in source.replace(self._LINE, "")
            assert "class GsT(" in source

    def test_the_generator_alone(self) -> None:
        tracking = TableState(MIGRATIONS_TABLE, fields={"name": FieldState("name", "string", table=MIGRATIONS_TABLE)})
        issue = SchemaIssue("table", MIGRATIONS_TABLE, MIGRATIONS_TABLE, "could not read the applied migrations")
        source = ModelCodeGenerator().generate(SchemaState({MIGRATIONS_TABLE: tracking}, issues=[issue]))
        assert self._LINE in source.splitlines()
        assert MIGRATIONS_TABLE not in source.replace(self._LINE, "")
        assert "Not generated" not in source
        assert self._LINE not in ModelCodeGenerator().generate(SchemaState())


#: Spec §5.1's ``surreal_orm_lite.migrations.__all__``.
_MIGRATIONS_ALL = {
    "Migration",
    "Operation",
    "CreateTable",
    "AlterTable",
    "DropTable",
    "AddField",
    "AlterField",
    "DropField",
    "CreateIndex",
    "DropIndex",
    "RebuildIndex",
    "DefineAnalyzer",
    "RemoveAnalyzer",
    "DefineEvent",
    "RemoveEvent",
    "DefineAccess",
    "RemoveAccess",
    "DefineFunction",
    "RemoveFunction",
    "DataMigration",
    "RawSQL",
    "DefaultExpression",
    "split_destructive",
    "MigrationGenerator",
    "generate_empty_migration",
    "MigrationExecutor",
    "MigrationStatementError",
    "ModelIntrospector",
    "introspect_models",
    "makemigrations",
    "DatabaseIntrospector",
    "ModelCodeGenerator",
    "MIGRATIONS_TABLE",
}
#: Spec §5.1's top-level additions from the migrations package.
_TOP_LEVEL = ("schema_diff", "DefineAnalyzer", "DefineEvent", "RebuildIndex", "RemoveEvent")


class TestExports:
    """AC-1: spec §5.1's names, exactly."""

    def test_the_migrations_package(self) -> None:
        import surreal_orm_lite.migrations as migrations

        assert len(migrations.__all__) == len(set(migrations.__all__)) == 33
        assert set(migrations.__all__) == _MIGRATIONS_ALL
        for name in _MIGRATIONS_ALL:
            assert getattr(migrations, name) is not None, name
        assert migrations.DatabaseIntrospector is DatabaseIntrospector
        assert migrations.ModelCodeGenerator is ModelCodeGenerator

    def test_the_top_level(self) -> None:
        import surreal_orm_lite.introspection as introspection_module
        import surreal_orm_lite.migrations as migrations

        for name in _TOP_LEVEL[1:]:
            assert name in surreal_orm_lite.__all__, name
            assert getattr(surreal_orm_lite, name) is getattr(migrations, name)
        assert "schema_diff" in surreal_orm_lite.__all__
        assert surreal_orm_lite.schema_diff is introspection_module.schema_diff
        assert "schema_diff" in introspection_module.__all__
        # The other migration names stay in the sub-package.
        for name in _MIGRATIONS_ALL - {"DatabaseIntrospector", "ModelCodeGenerator", *_TOP_LEVEL}:
            assert name not in surreal_orm_lite.__all__, name

    def test_the_names_import(self) -> None:
        namespace: dict[str, Any] = {}
        exec(f"from surreal_orm_lite.migrations import {', '.join(sorted(_MIGRATIONS_ALL))}", namespace)
        exec(f"from surreal_orm_lite import {', '.join(_TOP_LEVEL)}, get_registered_models", namespace)
        exec("from surreal_orm_lite.introspection import schema_diff", namespace)
        assert all(name in namespace for name in (*_MIGRATIONS_ALL, *_TOP_LEVEL))


_SRC = Path(surreal_orm_lite.__file__).parent


def _imports_migrations(node: ast.stmt) -> bool:
    """Whether the module-level statement *node* imports the ``migrations`` package or one of its modules."""
    if isinstance(node, ast.Import):
        return any(alias.name.startswith("surreal_orm_lite.migrations") for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if node.level == 0:
            return module.startswith("surreal_orm_lite.migrations")
        return module.split(".")[0] == "migrations" or (module == "" and any(a.name == "migrations" for a in node.names))
    return False


def _type_checking(node: ast.stmt) -> bool:
    return isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"


class TestLayering:
    """AC-1 (spec §5.1): no import cycle — four fresh interpreters —, and no core module imports the ``migrations``
    package at module level (``surreal_orm_lite/__init__.py`` imports it last, after every other import)."""

    @pytest.mark.parametrize(
        "statement",
        [
            "import surreal_orm_lite",
            "import surreal_orm_lite.model_auth",
            "import surreal_orm_lite.migrations.render",
            "from surreal_orm_lite.migrations import introspector",
        ],
    )
    def test_a_fresh_interpreter(self, statement: str) -> None:
        completed = subprocess.run([sys.executable, "-c", statement], capture_output=True, text=True, check=False)
        assert completed.returncode == 0, completed.stderr

    def test_no_core_module_imports_migrations(self) -> None:
        offenders = []
        for path in sorted(_SRC.glob("*.py")):
            if path.name == "__init__.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if not _type_checking(node) and _imports_migrations(node):
                    offenders.append(path.name)
        assert offenders == []

    def test_the_package_init_imports_migrations_last(self) -> None:
        tree = ast.parse((_SRC / "__init__.py").read_text(encoding="utf-8"))
        imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
        flags = [_imports_migrations(node) for node in imports]
        assert any(flags)
        first = flags.index(True)
        assert all(flags[first:]), "every import after the first migrations import is a migrations import"
