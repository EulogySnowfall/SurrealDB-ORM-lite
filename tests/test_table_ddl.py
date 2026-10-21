"""Model table DDL (v0.34.0): a model declares its table's clauses with the full SurrealDB-ORM's keys —
``SurrealConfigDict(schema_mode=SchemaMode.…, changefeed=…, permissions={…}, comment=…, flexible_fields=[…])``.

``SchemaMode`` (``TestSchemaMode``): the full ORM's enum — two members, same values — exported from the package root.

The class-time check (``TestTableConfig``): ``schema.check_table_config()`` runs when the ``class`` statement does, on
every model, inherited values re-checked with the subclass's name. In order, the first failure raising: a view model
with ``schema_mode``, ``table_type`` or ``flexible_fields``; a ``schema_mode`` that is not a ``SchemaMode`` (a plain
``"SCHEMAFULL"`` included); ``TableType.USER`` with ``SchemaMode.SCHEMALESS``; a declared ``SchemaMode.SCHEMAFULL`` with
``extra="allow"`` (a ``TableType.USER`` model with ``extra="allow"`` and no declared mode still imports); the change
feed (``schema.changefeed_text()``: a duration, optionally ``INCLUDE ORIGINAL`` in any case, whitespace folded); the
permissions (``schema.permissions_rules()``: a mapping of ``select``/``create``/``update``/``delete`` to ``FULL``,
``NONE``, ``WHERE <condition>`` or a bare condition, each condition one expression ``scan_fragment()`` accepts); the
comment (``schema.comment_text()``: no NUL); ``flexible_fields`` (a ``list`` or ``tuple`` of the model's field names).
Each refusal is compared with its exact text and class.

The getters (``TestGetters``): ``get_schema_mode()`` — declared, else ``SCHEMAFULL`` for ``TableType.USER`` and
``SCHEMALESS`` for ``TableType.HASH``, else ``None``; ``get_changefeed()`` normalised; ``get_permissions()`` the
declared mapping as given (a ``dict`` copy), ``{}`` when the model declares none; the normalised four-operation map only
through ``schema.permissions_rules()``. ``model_config`` keeps every key as declared.

The docstrings (``TestOldNumbers``): the version numbers the roadmap moved are gone — ``TableType``'s other members take
effect in ``define_table()`` (v0.34.0), ``ModelFactory`` comes in v0.40.0.

The kinds (``TestKinds``): ``model_ddl.field_kind()`` maps each annotation to the SurrealQL kind its column is defined
with, written by ``render_kind()`` — the scalar types, ``int | float | Decimal`` as ``number``, geometry fields,
literals and ``str`` or ``int`` enums as literal unions, arrays, tuples, sets, free-form objects, ``option<…>`` for
``None``, ``any`` for ``Any`` and every type without a SurrealQL counterpart. ``holds_object()``, ``holds_any()`` and
``untyped_arrays()`` read a kind.

The columns (``TestColumns``): ``model_ddl.model_columns()`` lists, in declaration order, every column an ORM write
may send — the fields but ``id``, an edge's ``in_``/``out`` and ``Field(exclude=True)``, then the Pydantic computed
fields, each under its column (an alias) — each followed by the item definition of an untyped array (``bag.*``) and by
the fields of a sub-model (``address.city``, ``subs.*.city``, its computed fields included). A record model, a
sub-model in a union or a container, a dict and a sub-model already on the path are free-form objects. A column is
optional when its annotation admits ``None`` or its default is ``None`` (never ``any``); the constant default of a
column that is not optional is its ``DEFAULT`` literal. ``FLEXIBLE`` requested on a kind without an object, and a
computed field's expression ``scan_fragment()`` refuses, raise their exact texts. ``model_ddl`` imports no model module.

The statements (``TestTableDdl``): ``table_ddl()`` renders, with no connection, ``DEFINE TABLE {OVERWRITE | IF NOT
EXISTS} `<table>` TYPE NORMAL|ANY`` and the clauses the model declares — schema mode, comment, change feed,
permissions, in that order (``schema.declared_clauses()``) — then one ``DEFINE FIELD`` per column: ``TYPE <kind>``,
``FLEXIBLE`` when the declared mode is ``SCHEMAFULL`` and the column holds a free-form object or asks for it, the
``DEFAULT`` of a constant default, a computed field's ``VALUE``. A ``server_fields`` column without a constant default
gets no ``DEFAULT``. A ``TableType.USER`` model with ``extra="allow"`` is refused; an edge model renders its relation
statement then its data fields, a view model its view statement. ``model_ddl.field_permissions_text()`` writes a
field's permissions as SurrealDB renders them.

The carried statements (``TestCarriedTable``, ``TestCarriedFields``), from the definitions as the servers render them:
over a table that exists, ``schema.table_statement_keeping()`` writes the model's type clause, then the ``DROP``, schema
mode, comment, change feed and permissions — each declared one, else the stored one —, and ``relation_statement_keeping()``
gives v0.33.0's statements through it; ``schema.view_statement()`` writes a view's declared comment, change feed and
permissions after ``AS (…)`` and ``schema.view_statement_keeping()`` keeps a stored view's clauses around it. Over a field
that exists, ``model_ddl.field_statement_keeping()`` writes the column's ``TYPE``, ``FLEXIBLE``, ``DEFAULT`` and
``VALUE`` and keeps the stored ``REFERENCE``, ``DEFAULT``/``VALUE`` the model does not write, ``READONLY``, ``ASSERT``,
``PERMISSIONS`` and ``COMMENT``: a stored ``COMPUTED`` stays ``COMPUTED`` (the model's expression, no ``DEFAULT``), a
``server_fields`` column keeps the database's ``DEFAULT``, ``VALUE`` or ``COMPUTED``. A carried value that fails its
check gives ``None``; a value the model replaces is not checked.

The guards' texts (``TestGuardTexts``): ``schema.table_refused()`` names ``define_table()`` or ``define_access()``;
``schema.relation_table_refused()`` keeps v0.33.0's texts and names ``define_table()`` when it is the caller;
``schema.relation_schemafull_refused()`` names the data fields of an edge model declared ``SCHEMAFULL``.

The field read (``TestReadTableFields``): ``schema.read_table_fields()`` sends ``INFO FOR TABLE`` once and keys each
field's definition by its path on both lines' renderings (``bag.*`` and ``bag[*]``, a keyword key bare or quoted); a
definition the parser cannot read is ``None`` under the path of its key; a refusal is the table definition's.

On the engines (``TestTableDdlE2E``, every engine of both lines): each list of statements, applied raw, is accepted and
read back — the table's type, mode, comment, change feed and permissions, each column's kind and ``FLEXIBLE``.

The ORM's writes (``TestOrmWritesE2E``): on a ``SCHEMAFULL`` table defined by a model's ``table_ddl()`` — one column of
every kind, a typed computed field ``tot`` — every write path is accepted and every field reads back equal to what
the write sent, and the computed field reads ``i + 1`` after each write; the same on the table its ``define_table()``
defines, with an object and an array in its ``Any`` column; SurrealDB 3.1 refuses the ORM's ``set`` value, which is
pinned there.

Applying (``TestDefineTable``, ``TestTableGuard``, on fake clients): ``define_table()`` reads ``INFO FOR DB`` once —
and ``INFO FOR TABLE`` once for a table that exists, with ``overwrite=True`` — then sends one statement per call: the
table, then each column. A missing table gets ``table_ddl()``'s statements; a table that exists gets the carried ones,
whose ``FLEXIBLE`` follows the mode the table will have (the declared one, else the stored one); ``overwrite=False``
sends ``IF NOT EXISTS`` and reads no field. The line (``client.version()``) is read once, outside any transaction, and
only for a table that is ``SCHEMAFULL`` once its statement is applied with a column holding ``any``: before SurrealDB
3.x those columns are written ``FLEXIBLE``. It returns the statements joined with a newline (a ``str``, the full ORM's
return). Before any DDL the guard refuses a view, a relation table, a table or a field definition it cannot read or
carry; a transaction of another connection, a non-``bool`` ``overwrite`` and the model's own faults are refused before
any request; a refused read or statement names the table or the field, never the statement.

On the engines (``TestDefineTableE2E``, ``TestExistingTableE2E``, ``TestComputedE2E``, ``TestTableGuardE2E``,
``TestTablePermissionsE2E``): a missing table is defined and a second call changes nothing; a table tuned by hand keeps
every clause, field clause, index, event, view, record, live query and change-feed entry the model does not declare, a
``DROP`` table its ``DROP``, a ``server_fields`` column the database's ``DEFAULT``; a ``COMPUTED`` field (SurrealDB
3.x) stays ``COMPUTED`` and its records keep their value; a view or a relation table under a node model is left as it
is; a record user gets the refusal of the first read and nothing else is sent.

Edge and view models (``TestEdgeAndViewDdl``, ``TestViewRefusal``, on fake clients): ``relation_ddl()`` and
``view_ddl()`` write the clauses the model declares, as its ``table_ddl()`` does, and nothing more for a model without
them. ``define_relation()`` of an edge model declaring ``SCHEMAFULL`` with data fields is refused before any request —
it defines no field —, and otherwise writes the declared clauses and keeps the stored ones. ``define_table()`` of an
edge model applies its relation statement, under ``define_relation()``'s guard naming ``define_table()``, then its data
fields over their stored definitions, never ``in``/``out``; of a view model, ``define_view()``'s statement, a ``str``.
``define_view()`` over a stored view keeps the clauses the model does not declare, and a refusal quoting its statement
is left out exactly when the statement writes ``PERMISSIONS`` (declared, ``permissions={}`` included, or kept).

On the engines (``TestEdgeAndViewE2E``): the edge and view statements applied raw are read back; ``define_relation()``
writes the declared comment and permissions and keeps the change feed; ``define_table()`` of an edge defines its data
field, which an edge then carries, and refuses a view or, with ``overwrite=False``, an ordinary table; ``define_view()``
keeps a view's clauses set by hand unless declared; ``define_table()`` of a view model equals ``define_view()`` and keeps
its guard over a table holding records.

The line facts (``TestLineFactsE2E``, every engine of both lines): ``define_table()`` reads the line only when needed
and writes ``FLEXIBLE`` on the columns holding ``any`` before SurrealDB 3.x, so an ``Any``, an untyped list and a
sub-model's ``Any`` round-trip objects and arrays on every engine; raw, on a ``SCHEMAFULL`` table, 2.x and the embedded
engine empty such values without ``FLEXIBLE``, which 3.x refuses there, and 3.x refuses an object item of an untyped
list without its item definition. A write naming a field a ``SCHEMAFULL`` table does not define is refused on 3.x and
the field dropped before 3; a record stored before the table became ``SCHEMAFULL`` reads back unchanged.

Transactions (``TestTableTransactionsE2E``, every engine of both lines): the reads run on the committed state before
anything is queued; the statements apply with the block — at once when interactive, at the commit when buffered — and
not at all when it raises; a transaction of another connection is refused before any request; a definition of the same
table or field queued earlier in the block is replaced at commit, a limit to lift.

The access (``TestAccessDdl``, on fake clients): ``access_ddl()`` and ``define_access()`` read the model's schema mode as
``define_table()`` does. A model whose schema mode is ``SCHEMAFULL`` — ``TableType.USER``, or a declared
``SchemaMode.SCHEMAFULL`` — gets its table through the model table DDL: the table statement, with the access's
permissions unless it declares its own, one ``DEFINE FIELD`` per field, then the access statement; ``define_access()``
reads and carries as ``define_table()`` does, its guards and its transaction check naming ``define_access()``. Every
other model keeps v0.17.0's ``SCHEMALESS`` table statement with the clauses it declares — v0.17.0's text byte for byte
when it declares none —, applied on its own when it declares permissions, whose refusal never quotes them.
``TableType.USER`` with ``extra="allow"``, a non-``bool`` ``overwrite`` and a computed field's faulty expression are
refused before any request, unless ``with_table=False``, which renders and applies the access statement alone.

On the servers (``TestAccessE2E``, over WebSocket, both lines): a ``TableType.USER`` model's ``define_access()`` defines
a ``SCHEMAFULL`` table with its fields and the access's permissions, and signup and signin work there; ``define_table()``
afterwards, or a second ``define_access()``, sends the carried statements and changes nothing; a view under the table is
refused, naming ``define_access()``.

The round trip (``TestRoundTripE2E``, every engine of both lines): ``generate_models_from_db()`` writes a table's
clauses as keys, and each generated model's ``define_table()`` on a fresh database reads back the same table clauses
and field kinds, except what a model does not hold — a typed record link, a ``DEFAULT`` on an optional or server-filled
field, an ``ASSERT``, a ``COMPUTED`` field written ``VALUE``; over the database it was read from, a ``COMPUTED`` field
stays ``COMPUTED`` and every ``DEFAULT``, ``VALUE`` and ``ASSERT`` stays.

The README (``TestReadmeSection35``): section 35 — its place, its subsections, its examples (run on ``mem://`` as
written, each printed line equal to its comment), the sentences each subsection says, its kinds and source-rule tables
and its "Coming from the full SurrealDB-ORM" bullets —, the sentences sections 16, 19, 31, 33 and 34 change, the three
rows of the 2.x/3.x table and its intro sentence, the embedded engine's row, and the features, roadmap and comparison
rows.
"""

import ast
import contextlib
import importlib.util
import inspect
import os
import re
import sys
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum, IntEnum, StrEnum
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Annotated, Any, ClassVar, Literal, TypeVar
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, Field, computed_field
from surrealdb import Datetime as SdkDatetime
from surrealdb import Duration as SdkDuration
from surrealdb import InternalError, NotAllowedError, RecordID
from surrealdb import Range as SdkRange

import surreal_orm_lite
from surreal_orm_lite import (
    AuthenticatedUserMixin,
    BaseSurrealEdge,
    BaseSurrealModel,
    BoundExcluded,
    BoundIncluded,
    Computed,
    Duration,
    GeoField,
    Geometry,
    GeometryField,
    GeometryPoint,
    GeometryPolygon,
    PointField,
    Range,
    SchemaMode,
    SurrealConfigDict,
    SurrealDbAuthenticationError,
    SurrealDBConnectionManager,
    SurrealDbError,
    SurrealDbNotFoundError,
    TableType,
    Transaction,
    computed,
    generate_models_from_db,
    model_ddl,
    schema,
)
from surreal_orm_lite.define_parser import normalise_kind, parse_define_field, parse_define_table, parse_kind, render_kind
from surreal_orm_lite.schema_state import FieldState, TableState
from surreal_orm_lite.testing import embedded_database
from tests._readme_tables import (
    assert_embedded_rows,
    assert_server_header_sentence,
    assert_server_rows,
    assert_server_table,
)
from tests._readme_tables import cells as _cells
from tests._readme_tables import normalised as _normalised
from tests._readme_tables import readme as _readme
from tests._readme_tables import readme_part as _readme_part
from tests._readme_tables import subsection as _subsection
from tests._readme_tables import table_rows as _table_rows
from tests._readme_tables import tables as _md_tables

# --- the exact texts of the class-time refusals ---------------------------------------------------------------------


def _tc1(name: str, type_name: str) -> str:
    return f"{name}: model_config['schema_mode'] must be a surreal_orm_lite.SchemaMode, got {type_name}."


def _tc2(name: str, type_name: str) -> str:
    return f"{name}: model_config['changefeed'] must be a str such as '7d' or '7d INCLUDE ORIGINAL', got {type_name}."


def _tc3(name: str, value: str) -> str:
    return (
        f"{name}: model_config['changefeed'] is {value!r}, not a change feed SurrealDB reads: a duration such as '7d' "
        "or '1h30m', optionally followed by ' INCLUDE ORIGINAL'."
    )


def _tc4(name: str, type_name: str) -> str:
    return (
        f"{name}: model_config['permissions'] must be a mapping of operation to rule, such as {{'select': 'FULL'}}, "
        f"got {type_name}."
    )


def _tc5(name: str, key: object) -> str:
    return (
        f"{name}: model_config['permissions'] names the operation {key!r}; a table's operations are select, create, "
        "update and delete."
    )


def _tc6a(name: str, operation: str, type_name: str) -> str:
    return f"{name}: model_config['permissions'][{operation!r}] must be a str, got {type_name}."


def _tc6b(name: str, operation: str) -> str:
    return f"{name}: model_config['permissions'][{operation!r}] is empty; write 'FULL', 'NONE' or a condition."


def _tc6c(name: str, operation: str, reason: str) -> str:
    return f"{name}: model_config['permissions'][{operation!r}] is not SurrealQL the ORM can embed: {reason}."


def _tc6d(name: str, operation: str) -> str:
    return f"{name}: model_config['permissions'][{operation!r}] is one condition; it holds a ';' outside brackets."


def _tc7a(name: str, type_name: str) -> str:
    return f"{name}: model_config['comment'] must be a str or None, got {type_name}."


def _tc7b(name: str) -> str:
    return f"{name}: model_config['comment'] holds a NUL character (\\x00), which SurrealQL cannot carry."


def _tc8(name: str, keys: str) -> str:
    return (
        f"{name}: a materialized view's table is computed by SurrealDB; leave {keys} out (a view model takes "
        "permissions, changefeed and comment)."
    )


def _tc9(name: str) -> str:
    return f"{name}: a TableType.USER table is SCHEMAFULL; leave schema_mode out or set SchemaMode.SCHEMAFULL."


def _tc10(name: str) -> str:
    return (
        f'{name}: extra="allow" keeps fields the model does not declare, which a SCHEMAFULL table refuses (SurrealDB '
        '3.x) or drops (2.x); declare SchemaMode.SCHEMALESS or remove extra="allow".'
    )


def _tc11a(name: str, type_name: str) -> str:
    return f"{name}: model_config['flexible_fields'] must be a list or tuple of field names, got {type_name}."


def _tc11b(name: str, type_name: str) -> str:
    return f"{name}: model_config['flexible_fields'] holds a {type_name}, not a field name."


def _tc11c(name: str, field: str) -> str:
    return f"{name}: model_config['flexible_fields'] names {field!r}, which is not a field of {name}."


_R_STRING = "a string literal is not terminated"
_R_BRACKETS = "its brackets are not balanced"

#: A view query the class-time check accepts.
_VIEW_QUERY = "SELECT count() AS n FROM tdl_note GROUP ALL"


def _model(name: str = "TdlConf", **config: Any) -> type[BaseSurrealModel]:
    """A node model named *name* on the table ``tdl_conf`` with *config*'s keys, built by a ``class`` statement."""
    namespace: dict[str, Any] = {
        "model_config": SurrealConfigDict(table_name="tdl_conf", **config),
        "__annotations__": {"id": RecordID | None, "body": str, "meta": dict[str, Any] | None},
        "id": None,
        "body": "",
        "meta": None,
    }
    return type(name, (BaseSurrealModel,), namespace)


def _refused(error: type[Exception], name: str = "TdlConf", **config: Any) -> str:
    """The text of the *error* the ``class`` statement of :func:`_model` raises, its class checked exactly."""
    with pytest.raises(error) as raised:
        _model(name, **config)
    assert type(raised.value) is error
    return str(raised.value)


class TestSchemaMode:
    """``SchemaMode`` is the full SurrealDB-ORM's enum — two members, same values — exported from the root."""

    def test_imported_from_the_package_root(self) -> None:
        assert surreal_orm_lite.SchemaMode is SchemaMode
        assert "SchemaMode" in surreal_orm_lite.__all__
        assert "SchemaMode" in surreal_orm_lite.types.__all__

    def test_members_and_values(self) -> None:
        assert SchemaMode.SCHEMAFULL == "SCHEMAFULL"
        assert SchemaMode.SCHEMALESS == "SCHEMALESS"
        assert [member.name for member in SchemaMode] == ["SCHEMAFULL", "SCHEMALESS"]
        assert len(SchemaMode) == 2

    def test_a_str_enum(self) -> None:
        assert isinstance(SchemaMode.SCHEMAFULL, str)
        assert SchemaMode("SCHEMALESS") is SchemaMode.SCHEMALESS
        assert str(SchemaMode.SCHEMAFULL) == "SCHEMAFULL"


# --- the accepted forms ---------------------------------------------------------------------------------------------

#: (declared change feed, the normalised text).
_CHANGEFEEDS: list[tuple[str, str]] = [
    ("7d", "7d"),
    ("1h30m", "1h30m"),
    ("1w2d", "1w2d"),
    ("0s", "0s"),
    (" 1h   include original ", "1h INCLUDE ORIGINAL"),
    ("7d INCLUDE ORIGINAL", "7d INCLUDE ORIGINAL"),
    ("7d Include\tOriginal", "7d INCLUDE ORIGINAL"),
]

#: (declared permissions, the normalised four-operation map).
_PERMISSIONS: list[tuple[Any, dict[str, str]]] = [
    ({}, {"select": "NONE", "create": "NONE", "update": "NONE", "delete": "NONE"}),
    ({"select": "full"}, {"select": "FULL", "create": "NONE", "update": "NONE", "delete": "NONE"}),
    ({"select": " None "}, {"select": "NONE", "create": "NONE", "update": "NONE", "delete": "NONE"}),
    (
        {"create": "WHERE $auth.id != NONE"},
        {"select": "NONE", "create": "WHERE $auth.id != NONE", "update": "NONE", "delete": "NONE"},
    ),
    (
        {"create": "$auth.id != NONE"},
        {"select": "NONE", "create": "WHERE $auth.id != NONE", "update": "NONE", "delete": "NONE"},
    ),
    (
        {"update": "  where   id = $auth.id  "},
        {"select": "NONE", "create": "NONE", "update": "WHERE id = $auth.id", "delete": "NONE"},
    ),
    (
        {"delete": "a = 'b; c'"},
        {"select": "NONE", "create": "NONE", "update": "NONE", "delete": "WHERE a = 'b; c'"},
    ),
    (
        {"delete": "FULL", "update": "id = $auth.id", "create": "NONE", "select": "Full"},
        {"select": "FULL", "create": "NONE", "update": "WHERE id = $auth.id", "delete": "FULL"},
    ),
    (
        MappingProxyType({"select": "FULL"}),
        {"select": "FULL", "create": "NONE", "update": "NONE", "delete": "NONE"},
    ),
]


class TestTableConfig:
    """The five keys are checked when the ``class`` statement runs: each accepted form, each refusal with its exact
    text and class, in order (the first failure wins), inherited values re-checked with the subclass's name."""

    @pytest.mark.parametrize(("value", "text"), _CHANGEFEEDS, ids=repr)
    def test_changefeed_forms(self, value: str, text: str) -> None:
        model = _model(changefeed=value)
        assert model.model_config["changefeed"] == value  # type: ignore[typeddict-item]
        assert schema.changefeed_text(value, model=model) == text

    @pytest.mark.parametrize(("value", "rules"), _PERMISSIONS, ids=repr)
    def test_permissions_forms(self, value: Any, rules: dict[str, str]) -> None:
        model = _model(permissions=value)
        assert model.model_config["permissions"] is value  # type: ignore[typeddict-item]
        result = schema.permissions_rules(value, model=model)
        assert result == rules
        assert list(result) == ["select", "create", "update", "delete"]

    @pytest.mark.parametrize("value", ["it's", "", "a\nb", "\\"], ids=repr)
    def test_comment_forms(self, value: str) -> None:
        model = _model(comment=value)
        assert model.model_config["comment"] == value  # type: ignore[typeddict-item]
        assert schema.comment_text(value, model=model) == value

    def test_comment_none(self) -> None:
        assert schema.comment_text(None, model=_model()) is None

    @pytest.mark.parametrize("value", [["meta"], ("meta",), [], ["meta", "body"]], ids=repr)
    def test_flexible_fields_forms(self, value: Any) -> None:
        model = _model(flexible_fields=value)
        assert model.model_config["flexible_fields"] is value  # type: ignore[typeddict-item]

    @pytest.mark.parametrize("member", list(SchemaMode), ids=str)
    def test_schema_mode_members(self, member: SchemaMode) -> None:
        assert _model(schema_mode=member).model_config["schema_mode"] is member  # type: ignore[typeddict-item]

    def test_every_key_at_once(self) -> None:
        model = _model(
            schema_mode=SchemaMode.SCHEMAFULL,
            changefeed="7d",
            permissions={"select": "FULL"},
            comment="c",
            flexible_fields=["meta"],
        )
        assert model.get_schema_mode() is SchemaMode.SCHEMAFULL

    def test_none_declares_nothing(self) -> None:
        model = _model(schema_mode=None, changefeed=None, permissions=None, comment=None, flexible_fields=None)
        assert model.get_schema_mode() is None
        assert model.get_changefeed() is None
        assert model.get_permissions() == {}

    # --- TC1, TC2, TC3 ----------------------------------------------------------------------------------------------

    @pytest.mark.parametrize(("value", "type_name"), [("SCHEMAFULL", "str"), ("schemaless", "str"), (1, "int")], ids=repr)
    def test_schema_mode_not_a_schema_mode(self, value: object, type_name: str) -> None:
        assert _refused(TypeError, schema_mode=value) == _tc1("TdlConf", type_name)

    def test_schema_mode_a_table_type(self) -> None:
        assert _refused(TypeError, schema_mode=TableType.USER) == _tc1("TdlConf", "TableType")

    @pytest.mark.parametrize(("value", "type_name"), [(7, "int"), (b"7d", "bytes"), (["7d"], "list")], ids=repr)
    def test_changefeed_not_a_str(self, value: object, type_name: str) -> None:
        assert _refused(TypeError, changefeed=value) == _tc2("TdlConf", type_name)

    @pytest.mark.parametrize("value", ["7 days", "1h INCLUDE", "", "  ", "7D", "d", "7d INCLUDE ORIGINALS", "7d;"], ids=repr)
    def test_changefeed_not_a_change_feed(self, value: str) -> None:
        assert _refused(ValueError, changefeed=value) == _tc3("TdlConf", value)

    # --- TC4, TC5, TC6 ----------------------------------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("value", "type_name"), [(["select"], "list"), ("FULL", "str"), ({"select"}, "set"), (1, "int")], ids=repr
    )
    def test_permissions_not_a_mapping(self, value: object, type_name: str) -> None:
        assert _refused(TypeError, permissions=value) == _tc4("TdlConf", type_name)

    @pytest.mark.parametrize("key", ["read", "SELECT", "Select", "", 1, None], ids=repr)
    def test_permissions_unknown_operation(self, key: object) -> None:
        assert _refused(ValueError, permissions={key: "FULL"}) == _tc5("TdlConf", key)

    def test_permissions_operation_printed_with_repr(self) -> None:
        assert _refused(ValueError, permissions={1: "FULL"}).startswith(
            "TdlConf: model_config['permissions'] names the operation 1;"
        )

    @pytest.mark.parametrize(("rule", "type_name"), [(1, "int"), (None, "NoneType"), (True, "bool")], ids=repr)
    def test_permissions_rule_not_a_str(self, rule: object, type_name: str) -> None:
        assert _refused(TypeError, permissions={"select": rule}) == _tc6a("TdlConf", "select", type_name)

    @pytest.mark.parametrize("rule", ["", "  ", "\n", "WHERE", " where  "], ids=repr)
    def test_permissions_rule_empty(self, rule: str) -> None:
        assert _refused(ValueError, permissions={"update": rule}) == _tc6b("TdlConf", "update")

    @pytest.mark.parametrize(
        ("rule", "reason"),
        [("a = 'b", _R_STRING), ("WHERE a = 'b", _R_STRING), ("count(a", _R_BRACKETS), ("WHERE (a = 1", _R_BRACKETS)],
        ids=repr,
    )
    def test_permissions_rule_not_embeddable(self, rule: str, reason: str) -> None:
        assert _refused(ValueError, permissions={"select": rule}) == _tc6c("TdlConf", "select", reason)

    def test_permissions_rule_with_a_trailing_note(self) -> None:
        text = _refused(ValueError, permissions={"select": "a = 1 -- n"})
        assert text == _tc6c("TdlConf", "select", "it holds a SurrealQL comment marker (-- , #, // or /* */)")

    @pytest.mark.parametrize("rule", ["a; b", "WHERE a; b", "a = 1;"], ids=repr)
    def test_permissions_rule_two_statements(self, rule: str) -> None:
        assert _refused(ValueError, permissions={"select": rule}) == _tc6d("TdlConf", "select")

    def test_permissions_operations_in_the_mapping_order(self) -> None:
        """The first operation of the mapping whose key or rule fails is reported."""
        assert _refused(TypeError, permissions={"delete": 1, "read": "FULL"}) == _tc6a("TdlConf", "delete", "int")
        assert _refused(ValueError, permissions={"read": "FULL", "delete": 1}) == _tc5("TdlConf", "read")
        assert _refused(ValueError, permissions={"update": "a; b", "select": ""}) == _tc6d("TdlConf", "update")

    # --- TC7 --------------------------------------------------------------------------------------------------------

    @pytest.mark.parametrize(("value", "type_name"), [(3, "int"), (b"c", "bytes"), (["c"], "list")], ids=repr)
    def test_comment_not_a_str(self, value: object, type_name: str) -> None:
        assert _refused(TypeError, comment=value) == _tc7a("TdlConf", type_name)

    def test_comment_with_a_nul(self) -> None:
        assert _refused(ValueError, comment="a\x00b") == _tc7b("TdlConf")

    # --- TC8 --------------------------------------------------------------------------------------------------------

    def _view(self, **config: Any) -> type[BaseSurrealModel]:
        namespace: dict[str, Any] = {
            "model_config": SurrealConfigDict(table_name="tdl_note_count", view_query=_VIEW_QUERY, **config),
            "__annotations__": {"id": RecordID | None, "n": int},
            "id": None,
            "n": 0,
        }
        return type("TdlNoteCount", (BaseSurrealModel,), namespace)

    def test_a_view_model_takes_permissions_changefeed_and_comment(self) -> None:
        view = self._view(permissions={"select": "FULL"}, changefeed="7d", comment="counts")
        assert view.get_permissions() == {"select": "FULL"}
        assert view.get_changefeed() == "7d"
        assert view.get_schema_mode() is None

    @pytest.mark.parametrize(
        ("config", "keys"),
        [
            ({"table_type": TableType.NORMAL}, "table_type"),
            ({"table_type": TableType.ANY}, "table_type"),
            ({"schema_mode": SchemaMode.SCHEMAFULL}, "schema_mode"),
            ({"flexible_fields": ["n"]}, "flexible_fields"),
            ({"table_type": TableType.NORMAL, "schema_mode": SchemaMode.SCHEMALESS}, "schema_mode, table_type"),
            (
                {"flexible_fields": [], "table_type": TableType.USER, "schema_mode": SchemaMode.SCHEMAFULL},
                "schema_mode, table_type, flexible_fields",
            ),
        ],
        ids=repr,
    )
    def test_view_model_refuses_the_table_keys(self, config: dict[str, Any], keys: str) -> None:
        with pytest.raises(TypeError) as raised:
            self._view(**config)
        assert type(raised.value) is TypeError
        assert str(raised.value) == _tc8("TdlNoteCount", keys)

    def test_tc8_before_tc1(self) -> None:
        with pytest.raises(TypeError) as raised:
            self._view(schema_mode="SCHEMAFULL")
        assert str(raised.value) == _tc8("TdlNoteCount", "schema_mode")

    def test_a_view_subclass_of_a_model_with_flexible_fields(self) -> None:
        """An inherited value is re-checked with the subclass's name."""

        class TdlBase(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_base", flexible_fields=["meta"])

            id: RecordID | None = None
            meta: dict[str, Any] | None = None

        with pytest.raises(TypeError) as raised:

            class TdlBaseView(TdlBase):
                model_config = SurrealConfigDict(table_name="tdl_base_view", view_query=_VIEW_QUERY)

        assert str(raised.value) == _tc8("TdlBaseView", "flexible_fields")

    # --- TC9, TC10 --------------------------------------------------------------------------------------------------

    def test_user_table_schemaless(self) -> None:
        text = _refused(TypeError, table_type=TableType.USER, schema_mode=SchemaMode.SCHEMALESS)
        assert text == _tc9("TdlConf")

    @pytest.mark.parametrize("member", [TableType.NORMAL, TableType.STREAM, TableType.HASH, TableType.ANY], ids=str)
    def test_other_table_types_take_either_mode(self, member: TableType) -> None:
        for mode in SchemaMode:
            assert _model(table_type=member, schema_mode=mode).get_schema_mode() is mode

    def test_user_table_schemafull_declared(self) -> None:
        model = _model(table_type=TableType.USER, schema_mode=SchemaMode.SCHEMAFULL)
        assert model.get_schema_mode() is SchemaMode.SCHEMAFULL

    def test_schemafull_with_extra_allow(self) -> None:
        assert _refused(TypeError, schema_mode=SchemaMode.SCHEMAFULL, extra="allow") == _tc10("TdlConf")

    def test_schemaless_with_extra_allow(self) -> None:
        model = _model(schema_mode=SchemaMode.SCHEMALESS, extra="allow")
        assert model.get_schema_mode() is SchemaMode.SCHEMALESS

    def test_user_table_with_extra_allow_still_imports(self) -> None:
        """No declared mode: the model imports as in 0.33 (its table DDL refuses it later)."""
        model = _model(table_type=TableType.USER, extra="allow")
        assert model.get_table_type() is TableType.USER
        assert model.get_schema_mode() is SchemaMode.SCHEMAFULL
        assert model(body="b", other=1).model_extra == {"other": 1}

    def test_inherited_schemafull_with_extra_allow_in_the_subclass(self) -> None:
        parent = _model("TdlParent", schema_mode=SchemaMode.SCHEMAFULL)
        with pytest.raises(TypeError) as raised:

            class TdlChild(parent):  # type: ignore[valid-type, misc]
                model_config = SurrealConfigDict(extra="allow")

        assert str(raised.value) == _tc10("TdlChild")

    def test_inherited_user_table_with_schemaless_in_the_subclass(self) -> None:
        parent = _model("TdlParent", table_type=TableType.USER)
        with pytest.raises(TypeError) as raised:

            class TdlChild(parent):  # type: ignore[valid-type, misc]
                model_config = SurrealConfigDict(schema_mode=SchemaMode.SCHEMALESS)

        assert str(raised.value) == _tc9("TdlChild")

    # --- TC11 -------------------------------------------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("value", "type_name"), [("meta", "str"), ({"meta"}, "set"), ({"meta": True}, "dict"), (1, "int")], ids=repr
    )
    def test_flexible_fields_not_a_list(self, value: object, type_name: str) -> None:
        assert _refused(TypeError, flexible_fields=value) == _tc11a("TdlConf", type_name)

    @pytest.mark.parametrize(("value", "type_name"), [([1], "int"), (("meta", None), "NoneType")], ids=repr)
    def test_flexible_fields_item_not_a_str(self, value: object, type_name: str) -> None:
        assert _refused(TypeError, flexible_fields=value) == _tc11b("TdlConf", type_name)

    @pytest.mark.parametrize(("value", "field"), [(["nope"], "nope"), (["meta", "Meta"], "Meta"), ([""], "")], ids=repr)
    def test_flexible_fields_not_a_field(self, value: object, field: str) -> None:
        assert _refused(ValueError, flexible_fields=value) == _tc11c("TdlConf", field)

    def test_flexible_fields_every_item_is_a_str_before_any_name_is_read(self) -> None:
        assert _refused(TypeError, flexible_fields=["nope", 1]) == _tc11b("TdlConf", "int")

    def test_flexible_fields_names_python_fields_not_columns(self) -> None:
        from pydantic import Field

        class TdlAliased(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_aliased", flexible_fields=["meta"])

            id: RecordID | None = None
            meta: dict[str, Any] | None = Field(default=None, alias="meta-data")

        assert TdlAliased.model_config["flexible_fields"] == ["meta"]  # type: ignore[typeddict-item]
        with pytest.raises(ValueError) as raised:

            class TdlAliasedColumn(BaseSurrealModel):
                model_config = SurrealConfigDict(table_name="tdl_aliased", flexible_fields=["meta-data"])

                id: RecordID | None = None
                meta: dict[str, Any] | None = Field(default=None, alias="meta-data")

        assert str(raised.value) == _tc11c("TdlAliasedColumn", "meta-data")

    # --- the order --------------------------------------------------------------------------------------------------

    def test_tc1_before_tc2(self) -> None:
        assert _refused(TypeError, schema_mode="SCHEMAFULL", changefeed=7) == _tc1("TdlConf", "str")

    def test_tc1_before_tc9(self) -> None:
        assert _refused(TypeError, table_type=TableType.USER, schema_mode="SCHEMALESS") == _tc1("TdlConf", "str")

    def test_tc9_before_tc10(self) -> None:
        text = _refused(TypeError, table_type=TableType.USER, schema_mode=SchemaMode.SCHEMALESS, extra="allow")
        assert text == _tc9("TdlConf")

    def test_tc10_before_tc2(self) -> None:
        assert _refused(TypeError, schema_mode=SchemaMode.SCHEMAFULL, extra="allow", changefeed=7) == _tc10("TdlConf")

    def test_changefeed_before_permissions_before_comment_before_flexible_fields(self) -> None:
        bad = {"changefeed": "7 days", "permissions": {"read": "FULL"}, "comment": 3, "flexible_fields": "meta"}
        assert _refused(ValueError, **bad) == _tc3("TdlConf", "7 days")
        del bad["changefeed"]
        assert _refused(ValueError, **bad) == _tc5("TdlConf", "read")
        del bad["permissions"]
        assert _refused(TypeError, **bad) == _tc7a("TdlConf", "int")
        del bad["comment"]
        assert _refused(TypeError, **bad) == _tc11a("TdlConf", "str")

    def test_the_relation_check_runs_first(self) -> None:
        """A plain-string ``table_type`` is v0.33.0's refusal, before the table clauses are read."""
        text = _refused(TypeError, table_type="user", schema_mode="SCHEMAFULL")
        assert text == "TdlConf: model_config['table_type'] must be a surreal_orm_lite.TableType, got str."

    # --- inheritance ------------------------------------------------------------------------------------------------

    def test_a_subclass_with_a_wrong_rule_is_named(self) -> None:
        parent = _model("TdlParent", permissions={"select": "FULL"})
        with pytest.raises(TypeError) as raised:

            class TdlChild(parent):  # type: ignore[valid-type, misc]
                model_config = SurrealConfigDict(permissions={"select": 1})  # type: ignore[dict-item]

        assert str(raised.value) == _tc6a("TdlChild", "select", "int")

    def test_an_edge_model_takes_the_keys(self) -> None:
        class TdlLikes(BaseSurrealEdge):
            model_config = SurrealConfigDict(
                table_name="tdl_likes",
                schema_mode=SchemaMode.SCHEMAFULL,
                changefeed="1d",
                permissions={"select": "FULL"},
                comment="likes",
            )

            stars: int = 1

        assert TdlLikes.get_schema_mode() is SchemaMode.SCHEMAFULL
        assert TdlLikes.get_changefeed() == "1d"

    def test_an_edge_model_with_a_wrong_key_is_refused(self) -> None:
        with pytest.raises(ValueError) as raised:

            class TdlLikes(BaseSurrealEdge):
                model_config = SurrealConfigDict(table_name="tdl_likes", changefeed="1 day")

        assert str(raised.value) == _tc3("TdlLikes", "1 day")


class TestGetters:
    """``get_schema_mode()``, ``get_changefeed()`` and ``get_permissions()`` — the full SurrealDB-ORM's names."""

    @pytest.mark.parametrize("member", list(SchemaMode), ids=str)
    def test_schema_mode_declared(self, member: SchemaMode) -> None:
        assert _model(schema_mode=member).get_schema_mode() is member

    def test_schema_mode_from_the_table_type(self) -> None:
        assert _model(table_type=TableType.USER).get_schema_mode() is SchemaMode.SCHEMAFULL
        assert _model(table_type=TableType.HASH).get_schema_mode() is SchemaMode.SCHEMALESS
        assert _model(table_type=TableType.HASH, schema_mode=SchemaMode.SCHEMAFULL).get_schema_mode() is (SchemaMode.SCHEMAFULL)

    @pytest.mark.parametrize("member", [TableType.NORMAL, TableType.STREAM, TableType.ANY, None], ids=str)
    def test_schema_mode_not_declared(self, member: TableType | None) -> None:
        assert _model(table_type=member).get_schema_mode() is None

    def test_schema_mode_of_a_model_without_keys(self) -> None:
        class TdlPlain(BaseSurrealModel):
            id: RecordID | None = None

        assert TdlPlain.get_schema_mode() is None
        assert TdlPlain.get_changefeed() is None
        assert TdlPlain.get_permissions() == {}

    def test_schema_mode_of_an_edge_model(self) -> None:
        class TdlEdge(BaseSurrealEdge):
            model_config = SurrealConfigDict(table_name="tdl_edge")

        class TdlFullEdge(BaseSurrealEdge):
            model_config = SurrealConfigDict(table_name="tdl_full_edge", schema_mode=SchemaMode.SCHEMAFULL)

        assert TdlEdge.get_schema_mode() is None
        assert TdlFullEdge.get_schema_mode() is SchemaMode.SCHEMAFULL

    @pytest.mark.parametrize(("value", "text"), _CHANGEFEEDS, ids=repr)
    def test_changefeed_normalised(self, value: str, text: str) -> None:
        model = _model(changefeed=value)
        assert model.get_changefeed() == text
        assert model.model_config["changefeed"] == value  # type: ignore[typeddict-item]

    def test_changefeed_the_full_orms_form_is_the_declared_string(self) -> None:
        assert _model(changefeed="7d").get_changefeed() == "7d"
        assert _model().get_changefeed() is None

    def test_permissions_the_declared_mapping(self) -> None:
        declared = {"select": "full", "create": "$auth.id != NONE"}
        model = _model(permissions=declared)
        result = model.get_permissions()
        assert result == {"select": "full", "create": "$auth.id != NONE"}
        assert type(result) is dict
        assert result is not declared
        result["update"] = "FULL"
        result["select"] = "NONE"
        assert model.model_config["permissions"] == {"select": "full", "create": "$auth.id != NONE"}  # type: ignore[typeddict-item]
        assert model.get_permissions() == {"select": "full", "create": "$auth.id != NONE"}

    def test_permissions_a_mapping_proxy_gives_a_dict(self) -> None:
        model = _model(permissions=MappingProxyType({"select": "FULL"}))
        result = model.get_permissions()
        assert result == {"select": "FULL"}
        assert type(result) is dict

    def test_permissions_empty_and_not_declared(self) -> None:
        assert _model(permissions={}).get_permissions() == {}
        assert _model().get_permissions() == {}
        assert _model(permissions=None).get_permissions() == {}

    def test_permissions_the_normalised_map(self) -> None:
        model = _model(permissions={"select": "full", "create": "$auth.id != NONE"})
        rules = schema.permissions_rules(model.model_config["permissions"], model=model)  # type: ignore[typeddict-item]
        assert rules == {"select": "FULL", "create": "WHERE $auth.id != NONE", "update": "NONE", "delete": "NONE"}
        empty = _model(permissions={})
        assert schema.permissions_rules(empty.model_config["permissions"], model=empty) == {  # type: ignore[typeddict-item]
            "select": "NONE",
            "create": "NONE",
            "update": "NONE",
            "delete": "NONE",
        }

    def test_permissions_rules_takes_a_class_name(self) -> None:
        with pytest.raises(ValueError) as raised:
            schema.permissions_rules({"read": "FULL"}, model="TdlGenerated")
        assert str(raised.value) == _tc5("TdlGenerated", "read")

    def test_model_config_keeps_every_key_as_declared(self) -> None:
        permissions = {"select": "full"}
        flexible = ("meta",)
        model = _model(
            schema_mode=SchemaMode.SCHEMAFULL,
            changefeed=" 1h   include original ",
            permissions=permissions,
            comment="it's",
            flexible_fields=flexible,
        )
        config: dict[str, Any] = dict(model.model_config)
        assert config["schema_mode"] is SchemaMode.SCHEMAFULL
        assert config["changefeed"] == " 1h   include original "
        assert config["permissions"] is permissions
        assert config["permissions"] == {"select": "full"}
        assert config["comment"] == "it's"
        assert config["flexible_fields"] is flexible

    def test_a_subclass_inherits_the_keys(self) -> None:
        parent = _model(
            "TdlParent",
            schema_mode=SchemaMode.SCHEMAFULL,
            changefeed="7d",
            permissions={"select": "FULL"},
            comment="c",
            flexible_fields=["meta"],
        )

        class TdlChild(parent):  # type: ignore[valid-type, misc]
            pass

        assert TdlChild.get_schema_mode() is SchemaMode.SCHEMAFULL
        assert TdlChild.get_changefeed() == "7d"
        assert TdlChild.get_permissions() == {"select": "FULL"}
        assert TdlChild.model_config["comment"] == "c"
        assert TdlChild.model_config["flexible_fields"] == ["meta"]

    def test_none_resets_an_inherited_key(self) -> None:
        parent = _model(
            "TdlParent",
            schema_mode=SchemaMode.SCHEMAFULL,
            changefeed="7d",
            permissions={"select": "FULL"},
            comment="c",
            flexible_fields=["meta"],
        )

        class TdlChild(parent):  # type: ignore[valid-type, misc]
            model_config = SurrealConfigDict(
                schema_mode=None, changefeed=None, permissions=None, comment=None, flexible_fields=None
            )

        assert TdlChild.get_schema_mode() is None
        assert TdlChild.get_changefeed() is None
        assert TdlChild.get_permissions() == {}
        assert TdlChild.model_config["comment"] is None

    def test_none_resets_the_mode_to_the_table_types(self) -> None:
        parent = _model("TdlParent", table_type=TableType.HASH, schema_mode=SchemaMode.SCHEMAFULL)

        class TdlChild(parent):  # type: ignore[valid-type, misc]
            model_config = SurrealConfigDict(schema_mode=None)

        assert parent.get_schema_mode() is SchemaMode.SCHEMAFULL
        assert TdlChild.get_schema_mode() is SchemaMode.SCHEMALESS


class TestOldNumbers:
    """The docstrings name the versions the roadmap gives today: the table DDL is v0.34.0, ``ModelFactory`` v0.40.0."""

    _SOURCES = ("model_base.py", "schema.py", "types.py", "testing/__init__.py")

    @pytest.mark.parametrize("source", _SOURCES)
    def test_no_old_number(self, source: str) -> None:
        text = (Path(surreal_orm_lite.__file__).parent / source).read_text(encoding="utf-8")
        assert "the table DDL of v0.37.0" not in text
        assert "comes in v0.39.0" not in text

    def test_table_type_members_take_effect(self) -> None:
        doc = " ".join((TableType.__doc__ or "").split())
        assert "take effect in define_table() (v0.34.0)" in doc

    def test_model_factory_version(self) -> None:
        from surreal_orm_lite import testing

        doc = " ".join((testing.__doc__ or "").split())
        assert "``ModelFactory`` comes in v0.40.0." in doc

    def test_computed_field_ddl_note(self) -> None:
        doc = " ".join((BaseSurrealModel.computed_field_ddl.__doc__ or "").split())
        assert "Explicit field types are a later roadmap item" not in doc
        assert "define_table() (v0.34.0) writes the computed field's TYPE" in doc

    def test_get_table_type_and_the_key_docstrings(self) -> None:
        doc = " ".join((BaseSurrealModel.get_table_type.__doc__ or "").split())
        assert "takes effect in define_table() (v0.34.0)" in doc
        config_doc = " ".join((surreal_orm_lite.SurrealConfigDict.__doc__ or "").split())
        assert "takes effect in define_table() (v0.34.0)" in config_doc


# --- kinds and columns (model_ddl) ----------------------------------------------------------------------------------


class TdlTier(StrEnum):
    FREE = "free"
    PRO = "pro"


class TdlLevel(IntEnum):
    ONE = 1
    TWO = 2


class TdlMixed(Enum):
    """An enum whose values mix ``str`` and ``int``: no literal union, ``any``."""

    A = "a"
    ONE = 1


class TdlRatio(Enum):
    """An enum holding a ``float``: no literal union, ``any``."""

    HALF = 0.5


class TdlNoMembers(Enum):
    """An enum without members: no literal union, ``any``."""


class TdlStr(str):
    """A ``str`` subclass that is not an enum."""


class TdlOpaque:
    """A class with no SurrealQL counterpart."""


class TdlAddress(BaseModel):
    city: str
    zip: str | None = None


class TdlCalc(BaseModel):
    a: int = 1

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dbl(self) -> int:
        return self.a * 2


class TdlBox(BaseModel):
    """A sub-model holding an untyped list."""

    l: list = Field(default_factory=list)  # noqa: E741


class TdlOpen(BaseModel):
    """A sub-model that keeps undeclared keys."""

    model_config = ConfigDict(extra="allow")

    k: int = 0


class TdlTree(BaseModel):
    """A sub-model that holds itself."""

    label: str
    child: "TdlTree | None" = None


class TdlPerson(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_name="tdl_person",
        schema_mode=SchemaMode.SCHEMAFULL,
        changefeed="7d",
        permissions={"select": "FULL", "create": "$auth.id != NONE"},
        comment="People",
        connection="tdl",
    )

    id: str | None = None
    name: str
    age: int = 18
    nickname: str | None = None
    tier: TdlTier = TdlTier.FREE
    tags: list[str] = Field(default_factory=list)
    address: TdlAddress | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    password: str = Field(alias="password_hash")


class TdlNote(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="tdl_note", connection="tdl")

    id: str | None = None
    body: str
    done: bool = False


class TdlLink(BaseSurrealEdge):
    model_config = SurrealConfigDict(table_name="tdl_link", connection="tdl")

    stars: int = 1


class TdlKinds(BaseSurrealModel):
    """One column per kind of the README's "Field types" table, in declaration order."""

    model_config = SurrealConfigDict(table_name="tdl_kinds", schema_mode=SchemaMode.SCHEMAFULL, connection="tdl")

    id: str | None = None
    dt: datetime
    n: int | float | Decimal
    pt: PointField = None
    tu: tuple[int, str]
    st: set[int]
    ie: TdlLevel
    lit: Literal["a", "b"]
    subs: list[TdlAddress]
    bag: list  # type: ignore[type-arg]
    lol: list[list] = Field(default_factory=list)  # type: ignore[type-arg]
    ob: list | None = None  # type: ignore[type-arg]
    tags: list[str]
    x: int = None  # type: ignore[assignment]
    owner: TdlNote | None = None
    u: int | Any
    sub: TdlCalc
    dl: dict[str, Any] | list[str]
    two: list[int] | list[str]
    c: list[int] | dict[str, Any] | None = None


def _columns(model: type[BaseSurrealModel]) -> tuple[model_ddl.Column, ...]:
    """``model_ddl.model_columns()`` of *model*, called with what the model's table DDL passes."""
    return model_ddl.model_columns(
        model,
        columns=model.get_field_aliases(),
        computed=model.get_computed_fields(),
        flexible=model.model_config.get("flexible_fields") or (),
        exclude={"id", "in_", "out"} if model.__surreal_edge__ else {"id"},
        server=model.model_config.get("server_fields") or (),
    )


def _rows(model: type[BaseSurrealModel]) -> list[tuple[tuple[str, ...], str, bool, str | None]]:
    """``(path, kind text, free_object, default)`` of each column of *model*, in order."""
    return [(column.path, render_kind(column.kind), column.free_object, column.default) for column in _columns(model)]


#: A geometry field of two kinds, as a name (a string in an annotation reads as a forward reference).
_POINT_OR_POLYGON: Any = GeoField["point|polygon"]


def _kind_text(annotation: Any) -> str:
    return render_kind(model_ddl.field_kind(annotation))


#: (test id, annotation, the kind's text) — one row per row of the README's "Field types" table.
_KIND_ROWS: list[tuple[str, Any, str]] = [
    ("str", str, "string"),
    ("str-subclass", TdlStr, "string"),
    ("bool", bool, "bool"),
    ("int", int, "int"),
    ("float", float, "float"),
    ("decimal", Decimal, "decimal"),
    ("number", int | float | Decimal, "number"),
    ("number-any-order", Decimal | int | float, "number"),
    ("optional-number", int | float | Decimal | None, "option<number>"),
    ("int-or-float", int | float, "int | float"),
    ("datetime", datetime, "datetime"),
    ("date", date, "datetime"),
    ("sdk-datetime", SdkDatetime, "any"),
    ("timedelta", timedelta, "duration"),
    ("orm-duration", Duration, "duration"),
    ("sdk-duration", SdkDuration, "duration"),
    ("uuid", UUID, "uuid"),
    ("bytes", bytes, "bytes"),
    ("record-id", RecordID, "record"),
    ("point-field", PointField, "option<geometry<point>>"),
    ("geo-field-two-kinds", GeoField["point|polygon"], "option<geometry<point | polygon>>"),
    ("geometry-field", GeometryField, "option<geometry>"),
    ("geometry-class", GeometryPoint, "geometry<point>"),
    ("geometry-union", GeometryPoint | GeometryPolygon, "geometry<point | polygon>"),
    ("geometry-base", Geometry, "geometry"),
    ("orm-range-of-int", Range[int], "range"),
    ("orm-range", Range, "range"),
    ("sdk-range", SdkRange, "range"),
    ("literal-str", Literal["a", "b"], "'a' | 'b'"),
    ("literal-int", Literal[1, 2], "1 | 2"),
    ("literal-none", Literal["a", None], "option<'a'>"),
    ("literal-bool", Literal[True], "bool"),
    ("literal-bools", Literal[True, False], "bool"),
    ("literal-float", Literal[1.5], "any"),
    ("literal-str-and-int", Literal["a", 1], "'a' | 1"),
    ("literal-quote", Literal["it's"], "'it\\'s'"),
    ("str-enum", TdlTier, "'free' | 'pro'"),
    ("int-enum", TdlLevel, "1 | 2"),
    ("mixed-enum", TdlMixed, "any"),
    ("float-enum", TdlRatio, "any"),
    ("list-of-str", list[str], "array<string>"),
    ("list", list, "array"),
    ("list-of-any", list[Any], "array"),
    ("list-of-int-or-any", list[int | Any], "array"),
    ("sequence", Sequence[int], "array<int>"),
    ("bare-sequence", Sequence, "array"),
    ("tuple-ellipsis", tuple[int, ...], "array<int>"),
    ("tuple-any-ellipsis", tuple[Any, ...], "array"),
    ("tuple", tuple[int, str], "[int, string]"),
    ("set", set[int], "set<int>"),
    ("frozenset", frozenset[int], "set<int>"),
    ("abstract-set", AbstractSet[int], "set<int>"),
    ("bare-set", set, "set"),
    ("set-of-any", set[Any], "set"),
    ("dict", dict, "object"),
    ("dict-of-int", dict[str, int], "object"),
    ("mapping", Mapping[str, Any], "object"),
    ("optional", int | None, "option<int>"),
    ("union", int | str, "int | string"),
    ("union-repeated", int | int, "int"),
    ("union-with-any", int | Any, "any"),
    # A union holding a list or a set with an object or another list, set or tuple: each of those written ``array``
    # (its items then get ``TYPE any``), since SurrealDB derives one item type for all of them.
    ("union-dict-and-list", dict[str, Any] | list[str], "object | array"),
    ("union-optional-list-and-dict", list[int] | dict[str, Any] | None, "option<array | object>"),
    ("union-two-lists", list[int] | list[str], "array"),
    ("union-typed-and-untyped-list", list[int] | list, "array"),  # type: ignore[type-arg]
    ("union-list-of-dicts-and-dict", list[dict[str, Any]] | dict[str, Any], "array | object"),
    ("union-list-of-models-and-model", list[TdlAddress] | TdlAddress, "array | object"),
    ("union-set-and-dict", set[int] | dict[str, Any], "array | object"),
    ("union-set-and-list", set[int] | list[str], "array"),
    ("union-two-sets", set[int] | frozenset[str], "array"),
    ("union-tuple-and-list", tuple[int, str] | list[str], "array"),
    ("union-two-tuple-ellipsis", tuple[int, ...] | tuple[str, ...], "array"),
    ("list-of-union-list-and-dict", list[list[int] | dict[str, Any]], "array<array | object>"),
    # … an object item of such a container becomes an ``any`` item: the kind holds no object any more.
    ("union-list-of-dicts-and-list", list[dict[str, Any]] | list[str], "array"),
    ("union-list-of-models-and-list", list[TdlAddress] | list[int], "array"),
    ("union-tuple-with-dict-and-list", tuple[dict[str, Any], int] | list[str], "array"),
    ("union-set-and-list-of-dicts", set[int] | list[dict[str, Any]], "array"),
    ("list-of-union-list-of-dicts-and-list", list[list[dict[str, Any]] | list[str]], "array<array>"),
    ("tuple-of-union-list-of-dicts-and-list", tuple[list[dict[str, Any]] | list[str], int], "[array, int]"),
    # … and the unions SurrealDB takes as written.
    ("union-untyped-list-and-dict", list | dict[str, Any], "array | object"),  # type: ignore[type-arg]
    ("union-tuple-and-dict", tuple[int, str] | dict[str, Any], "[int, string] | object"),
    ("union-two-tuples", tuple[int, str] | tuple[str, int], "[int, string] | [string, int]"),
    ("union-list-and-int", list[int] | int, "array<int> | int"),
    ("union-list-and-geometry", list[str] | GeometryPoint, "array<string> | geometry<point>"),
    ("optional-any", Any | None, "any"),
    ("any", Any, "any"),
    ("object", object, "any"),
    ("unknown-class", TdlOpaque, "any"),
    ("annotated", Annotated[int, Field(ge=0)], "int"),
    ("list-of-optional", list[int | None], "array<option<int>>"),
    ("list-of-list", list[list], "array<array>"),  # type: ignore[type-arg]
    ("sub-model", TdlAddress, "object"),
    ("optional-sub-model", TdlAddress | None, "option<object>"),
    ("list-of-sub-model", list[TdlAddress], "array<object>"),
    ("record-model", TdlNote, "object"),
    ("edge-model", TdlLink, "object"),
    ("sub-model-or-int", TdlAddress | int, "object | int"),
    ("optional-geometry-union", GeometryPoint | GeometryPolygon | None, "option<geometry<point | polygon>>"),
    ("geometry-or-int", GeometryPoint | int, "geometry<point> | int"),
    ("list-of-geometry", list[GeometryPoint], "array<geometry<point>>"),
    ("annotated-item", list[Annotated[int, Field(ge=0)]], "array<int>"),
    ("bare-tuple", tuple, "array"),
    ("empty-tuple", tuple[()], "array"),
    ("none", None, "none"),
    ("time", time, "any"),
    ("iterable", Iterable[int], "any"),
    ("deque", deque[int], "any"),
    ("defaultdict", defaultdict[str, int], "object"),
    ("literal-nul", Literal["a\x00"], "any"),
    ("literal-beyond-64-bits", Literal[2**70], "any"),
    ("bare-abstract-set", AbstractSet, "set"),
    ("empty-enum", TdlNoMembers, "any"),
    ("type-variable", TypeVar("TdlT"), "any"),
    ("class-var", ClassVar[int], "any"),
]


class TestKinds:
    """``model_ddl.field_kind()``: one case per row of the README's "Field types" table."""

    @pytest.mark.parametrize(("annotation", "text"), [pytest.param(a, t, id=i) for i, a, t in _KIND_ROWS])
    def test_kind(self, annotation: Any, text: str) -> None:
        assert _kind_text(annotation) == text

    def test_the_kinds_of_model_fields(self) -> None:
        """A field's annotation and metadata as Pydantic stores them: the geometry marker, ``Computed``, ``Annotated``."""

        class TdlFieldKinds(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_field_kinds")

            pt: PointField = None
            gf: _POINT_OR_POLYGON = None
            ge: GeometryField = None
            total: Computed[int] = computed("1 + 1")
            ann: Annotated[int, Field(ge=0)] = 0

        kinds = {
            name: render_kind(model_ddl.field_kind(info.annotation, info.metadata))
            for name, info in TdlFieldKinds.model_fields.items()
        }
        assert kinds == {
            "pt": "option<geometry<point>>",
            "gf": "option<geometry<point | polygon>>",
            "ge": "option<geometry>",
            "total": "option<int>",
            "ann": "int",
        }

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("option<array<object>>", True),
            ("object | int", True),
            ("[int, object]", True),
            ("set<object>", True),
            ("array<string>", False),
            ("any", False),
            ("geometry<point>", False),
            ("record<tdl_note>", False),
            ("{ a: object }", True),
            ("{ a: int }", False),
        ],
    )
    def test_holds_object(self, text: str, expected: bool) -> None:
        assert model_ddl.holds_object(parse_kind(text)) is expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("[int, any]", True),
            ("option<array>", True),
            ("set", True),
            ("any", True),
            ("array<array>", True),
            ("int | any", True),
            ("array<string>", False),
            ("set<int>", False),
            ("object", False),
            ("option<int>", False),
        ],
    )
    def test_holds_any(self, text: str, expected: bool) -> None:
        assert model_ddl.holds_any(parse_kind(text)) is expected

    @pytest.mark.parametrize(
        ("text", "depths"),
        [
            ("array", [1]),
            ("array<array>", [2]),
            ("option<array>", [1]),
            ("array | int", [1]),
            ("option<array> | int", [1]),
            ("array<option<array>>", [2]),
            ("array | array<array>", [1, 2]),
            ("array<string>", []),
            ("array<object>", []),
            ("array<array<int>>", []),
            ("set", []),
            ("int", []),
        ],
    )
    def test_untyped_arrays(self, text: str, depths: list[int]) -> None:
        assert model_ddl.untyped_arrays(parse_kind(text)) == depths


def _fx1(name: str, field: str, kind: str) -> str:
    return (
        f"{name}: the field {field!r} is FLEXIBLE (flexible_fields or json_schema_extra), but its kind {kind} holds no "
        "object; SurrealDB accepts FLEXIBLE on object kinds only."
    )


def _cv1(name: str, field: str, reason: str) -> str:
    return f"{name}: the computed field {field!r} is not SurrealQL the ORM can embed: {reason}."


class TestColumns:
    """``model_ddl.model_columns()``: the columns an ORM write may send, in order, with their kinds and ``DEFAULT``."""

    def test_the_person_columns(self) -> None:
        assert _rows(TdlPerson) == [
            (("name",), "string", False, None),
            (("age",), "int", False, "18"),
            (("nickname",), "option<string>", False, None),
            (("tier",), "'free' | 'pro'", False, "'free'"),
            (("tags",), "array<string>", False, None),
            (("address",), "option<object>", False, None),
            (("address", "city"), "string", False, None),
            (("address", "zip"), "option<string>", False, None),
            (("meta",), "object", True, None),
            (("password_hash",), "string", False, None),
        ]
        columns = _columns(TdlPerson)
        assert [column.field for column in columns] == [
            "name",
            "age",
            "nickname",
            "tier",
            "tags",
            "address",
            "address",
            "address",
            "meta",
            "password",
        ]
        assert not any(column.item or column.server or column.value or column.flexible_requested for column in columns)

    def test_the_kinds_columns(self) -> None:
        assert _rows(TdlKinds) == [
            (("dt",), "datetime", False, None),
            (("n",), "number", False, None),
            (("pt",), "option<geometry<point>>", False, None),
            (("tu",), "[int, string]", False, None),
            (("st",), "set<int>", False, None),
            (("ie",), "1 | 2", False, None),
            (("lit",), "'a' | 'b'", False, None),
            (("subs",), "array<object>", False, None),
            (("subs", "*", "city"), "string", False, None),
            (("subs", "*", "zip"), "option<string>", False, None),
            (("bag",), "array", False, None),
            (("bag", "*"), "any", False, None),
            (("lol",), "array<array>", False, None),
            (("lol", "*", "*"), "any", False, None),
            (("ob",), "option<array>", False, None),
            (("ob", "*"), "any", False, None),
            (("tags",), "array<string>", False, None),
            (("x",), "option<int>", False, None),
            (("owner",), "option<object>", True, None),
            (("u",), "any", False, None),
            (("sub",), "object", False, None),
            (("sub", "a"), "int", False, "1"),
            (("sub", "dbl"), "int", False, None),
            (("dl",), "object | array", True, None),
            (("dl", "*"), "any", False, None),
            (("two",), "array", False, None),
            (("two", "*"), "any", False, None),
            (("c",), "option<array | object>", True, None),
            (("c", "*"), "any", False, None),
        ]

    def test_the_union_columns(self) -> None:
        """A union keeps ``free_object`` only while its kind holds an object: ``list[dict[str, Any]] | list[str]`` is
        written ``array`` (its items ``any``), which holds none, so a ``SCHEMAFULL`` table does not write it
        ``FLEXIBLE`` — SurrealDB 3.x refuses ``FLEXIBLE`` on a kind without an object."""
        assert _rows(TdlUnions) == [
            (("dl",), "object | array", True, None),
            (("dl", "*"), "any", False, None),
            (("two",), "array", False, None),
            (("two", "*"), "any", False, None),
            (("c",), "option<array | object>", True, None),
            (("c", "*"), "any", False, None),
            (("sd",), "array | object", True, None),
            (("sd", "*"), "any", False, None),
            (("tl",), "array", False, None),
            (("tl", "*"), "any", False, None),
            (("nl",), "array<array | object>", True, None),
            (("nl", "*", "*"), "any", False, None),
            (("lds",), "array", False, None),
            (("lds", "*"), "any", False, None),
            (("nls",), "array<array>", False, None),
            (("nls", "*", "*"), "any", False, None),
            (("tdl",), "option<[array, int]>", False, None),
        ]

        class TdlUnionItems(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_union_items")

            lms: list[TdlAddress] | list[int]
            sdl: set[int] | list[dict[str, Any]]
            tds: tuple[dict[str, Any], int] | list[str]
            mm: list[TdlAddress] | TdlAddress

        assert _rows(TdlUnionItems) == [
            (("lms",), "array", False, None),
            (("lms", "*"), "any", False, None),
            (("sdl",), "array", False, None),
            (("sdl", "*"), "any", False, None),
            (("tds",), "array", False, None),
            (("tds", "*"), "any", False, None),
            (("mm",), "array | object", True, None),
            (("mm", "*"), "any", False, None),
        ]

    def test_item_definitions(self) -> None:
        class TdlItems(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_items")

            bag: list  # type: ignore[type-arg]
            box: TdlBox
            boxes: list[TdlBox] = Field(default_factory=list)
            tags: list[str] = Field(default_factory=list)
            dicts: list[dict] = Field(default_factory=list)  # type: ignore[type-arg]
            st: set = Field(default_factory=set)  # type: ignore[type-arg]

        columns = _columns(TdlItems)
        assert [(column.path, column.item) for column in columns] == [
            (("bag",), False),
            (("bag", "*"), True),
            (("box",), False),
            (("box", "l"), False),
            (("box", "l", "*"), True),
            (("boxes",), False),
            (("boxes", "*", "l"), False),
            (("boxes", "*", "l", "*"), True),
            (("tags",), False),
            (("dicts",), False),
            (("st",), False),
        ]
        items = [column for column in columns if column.item]
        assert all(render_kind(column.kind) == "any" and column.holds_any and column.default is None for column in items)
        assert [column.field for column in items] == ["bag", "box", "boxes"]
        assert [column.holds_any for column in columns if not column.item] == [
            True,
            False,
            True,
            False,
            True,
            False,
            False,
            True,
        ]

    def test_sub_model_shapes(self) -> None:
        class TdlShapes(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_shapes")

            a: TdlAddress
            b: TdlAddress | None = None
            c: list[TdlAddress] | None = None
            d: TdlAddress | int
            e: dict[str, TdlAddress] = Field(default_factory=dict)
            f: tuple[TdlAddress, int]

        assert _rows(TdlShapes) == [
            (("a",), "object", False, None),
            (("a", "city"), "string", False, None),
            (("a", "zip"), "option<string>", False, None),
            (("b",), "option<object>", False, None),
            (("b", "city"), "string", False, None),
            (("b", "zip"), "option<string>", False, None),
            (("c",), "option<array<object>>", False, None),
            (("c", "*", "city"), "string", False, None),
            (("c", "*", "zip"), "option<string>", False, None),
            (("d",), "object | int", True, None),
            (("e",), "object", True, None),
            (("f",), "[object, int]", True, None),
        ]

    def test_a_self_referencing_sub_model_stops_with_a_free_form_object(self) -> None:
        class TdlForest(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_forest")

            tree: TdlTree

        assert _rows(TdlForest) == [
            (("tree",), "object", False, None),
            (("tree", "label"), "string", False, None),
            (("tree", "child"), "option<object>", True, None),
        ]

    def test_record_models_are_free_form_objects(self) -> None:
        class TdlLinks(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_links")

            note: TdlNote
            link: TdlLink | None = None
            notes: list[TdlNote] = Field(default_factory=list)

        assert _rows(TdlLinks) == [
            (("note",), "object", True, None),
            (("link",), "option<object>", True, None),
            (("notes",), "array<object>", True, None),
        ]

    def test_a_sub_model_with_extra_allow_is_free_form_with_its_fields(self) -> None:
        class TdlOpenHolder(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_open_holder")

            open: TdlOpen
            opens: list[TdlOpen] = Field(default_factory=list)

        assert _rows(TdlOpenHolder) == [
            (("open",), "object", True, None),
            (("open", "k"), "int", False, "0"),
            (("opens",), "array<object>", True, None),
            (("opens", "*", "k"), "int", False, "0"),
        ]

    def test_the_nullable_rule(self) -> None:
        class TdlNullable(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_nullable")

            x: int = None  # type: ignore[assignment]
            y: Any = None
            z: int | None
            w: list[str] = None  # type: ignore[assignment]
            lit: Literal["a"] = None  # type: ignore[assignment]

        assert _rows(TdlNullable) == [
            (("x",), "option<int>", False, None),
            (("y",), "any", False, None),
            (("z",), "option<int>", False, None),
            (("w",), "option<array<string>>", False, None),
            (("lit",), "option<'a'>", False, None),
        ]

    def test_the_default_rule(self) -> None:
        class TdlDefaults(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_defaults")

            age: int = 18
            tier: TdlTier = TdlTier.FREE
            level: TdlLevel = TdlLevel.TWO
            done: bool = False
            ratio: float = 1.5
            d: Decimal = Decimal("1.50")
            title: str = "it's"
            at: datetime = datetime(2024, 1, 2, tzinfo=UTC)
            day: date = date(2024, 1, 2)
            ttl: timedelta = timedelta(hours=1, minutes=30)
            grace: Duration = Duration(90 * 10**9)
            key: UUID = UUID(int=1)
            sub: TdlCalc = Field(default_factory=TdlCalc)
            opt: int | None = 5
            anything: Any = 5
            tags: list[str] = Field(default_factory=list)
            pair: tuple[int, int] = (1, 2)
            r: RecordID = RecordID("a", 1)
            mixed: TdlMixed = TdlMixed.A
            ratio_enum: TdlRatio = TdlRatio.HALF
            big: int = 2**70
            nul: str = "a\x00"
            required: str

        assert [(column.path, column.default) for column in _columns(TdlDefaults)] == [
            (("age",), "18"),
            (("tier",), "'free'"),
            (("level",), "2"),
            (("done",), "false"),
            (("ratio",), "1.5"),
            (("d",), "1.50dec"),
            (("title",), "'it\\'s'"),
            (("at",), "d'2024-01-02T00:00:00Z'"),
            (("day",), "d'2024-01-02T00:00:00Z'"),
            (("ttl",), "1h30m"),
            (("grace",), "1m30s"),
            (("key",), "u'00000000-0000-0000-0000-000000000001'"),
            (("sub",), None),
            (("sub", "a"), "1"),
            (("sub", "dbl"), None),
            (("opt",), None),
            (("anything",), None),
            (("tags",), None),
            (("pair",), None),
            (("r",), None),
            (("mixed",), "'a'"),
            (("ratio_enum",), None),
            (("big",), None),
            (("nul",), None),
            (("required",), None),
        ]

    def test_server_fields(self) -> None:
        class TdlServer(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_note", server_fields=["status", "stamp", "owner_ref"])

            body: str = ""
            status: str = "draft"
            stamp: datetime = Field(default_factory=lambda: datetime(2024, 1, 2, tzinfo=UTC))
            owner_ref: str

        assert [(column.path, render_kind(column.kind), column.server, column.default) for column in _columns(TdlServer)] == [
            (("body",), "string", False, "''"),
            (("status",), "string", True, "'draft'"),
            (("stamp",), "datetime", True, None),
            (("owner_ref",), "string", True, None),
        ]

    def test_left_out_fields(self) -> None:
        class TdlLeftOut(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_left_out")

            id: str | None = None
            kept: int = 0
            secret: str = Field(default="", exclude=True)

        assert [column.path for column in _columns(TdlLeftOut)] == [("kept",)]
        assert [column.path for column in _columns(TdlLink)] == [("stars",)]
        assert _rows(TdlLink) == [(("stars",), "int", False, "1")]

    def test_aliases_are_the_columns(self) -> None:
        class TdlAliased(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_aliased")

            password: str = Field(alias="password_hash")
            first: str = Field(alias="first-name")
            dotted: int = Field(default=0, alias="a.b")
            sub: TdlAddress

        columns = _columns(TdlAliased)
        assert [column.path for column in columns] == [
            ("password_hash",),
            ("first-name",),
            ("a.b",),
            ("sub",),
            ("sub", "city"),
            ("sub", "zip"),
        ]
        assert [column.field for column in columns][:3] == ["password", "first", "dotted"]

    def test_computed_fields(self) -> None:
        class TdlCart(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_cart")

            items: list[str] = Field(default_factory=list)
            total: Computed[int] = computed("count(items)")

            @computed_field  # type: ignore[prop-decorator]
            @property
            def shout(self) -> str:
                return "!"

            @computed_field  # type: ignore[prop-decorator]
            @property
            def first(self) -> str | None:
                return self.items[0] if self.items else None

        columns = _columns(TdlCart)
        assert [(column.path, render_kind(column.kind), column.value, column.default) for column in columns] == [
            (("items",), "array<string>", None, None),
            (("total",), "option<int>", "count(items)", None),
            (("shout",), "string", None, None),
            (("first",), "option<string>", None, None),
        ]

    def test_flexible_requested(self) -> None:
        class TdlFlexible(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_flexible", flexible_fields=["meta"])

            meta: dict[str, Any] = Field(default_factory=dict)
            extra_meta: dict[str, Any] = Field(default_factory=dict, json_schema_extra={"flexible": True})
            address: TdlAddress | None = Field(default=None, json_schema_extra={"flexible": True})
            plain: dict[str, Any] = Field(default_factory=dict, json_schema_extra={"flexible": False})

        assert [(column.path, column.flexible_requested) for column in _columns(TdlFlexible)] == [
            (("meta",), True),
            (("extra_meta",), True),
            (("address",), True),
            (("address", "city"), False),
            (("address", "zip"), False),
            (("plain",), False),
        ]

    def test_fx1_flexible_on_a_kind_without_an_object(self) -> None:
        class TdlFlexInt(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_flex_int", flexible_fields=["age"])

            age: int = 18

        class TdlFlexExtra(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_flex_extra")

            tags: list[str] | None = Field(default=None, json_schema_extra={"flexible": True})

        with pytest.raises(ValueError) as raised:
            _columns(TdlFlexInt)
        assert type(raised.value) is ValueError
        assert str(raised.value) == _fx1("TdlFlexInt", "age", "int")
        with pytest.raises(ValueError) as raised:
            _columns(TdlFlexExtra)
        assert str(raised.value) == _fx1("TdlFlexExtra", "tags", "option<array<string>>")

    @pytest.mark.parametrize(
        ("expression", "reason"),
        [
            ("count(items) -- n", "it holds a SurrealQL comment marker (-- , #, // or /* */)"),
            ("count(items", "its brackets are not balanced"),
            ("string::concat(name, 'x)", "a string literal is not terminated"),
        ],
    )
    def test_cv1_a_computed_expression_the_orm_cannot_embed(self, expression: str, reason: str) -> None:
        class TdlBadCart(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_cart")

            items: list[str] = Field(default_factory=list)
            total: Computed[int] = computed(expression)

        with pytest.raises(ValueError) as raised:
            _columns(TdlBadCart)
        assert type(raised.value) is ValueError
        assert str(raised.value) == _cv1("TdlBadCart", "total", reason)

    def test_the_first_field_at_fault_raises(self) -> None:
        class TdlTwoFaults(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_two_faults", flexible_fields=["age"])

            age: int = 18
            total: Computed[int] = computed("count(items")

        class TdlTwoFaultsReversed(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_two_faults", flexible_fields=["age"])

            total: Computed[int] = computed("count(items")
            age: int = 18

        with pytest.raises(ValueError) as raised:
            _columns(TdlTwoFaults)
        assert str(raised.value) == _fx1("TdlTwoFaults", "age", "int")
        with pytest.raises(ValueError) as raised:
            _columns(TdlTwoFaultsReversed)
        assert str(raised.value) == _cv1("TdlTwoFaultsReversed", "total", "its brackets are not balanced")


class TestModelDdlImports:
    """``model_ddl`` reads annotations only: it imports no model module, so every model module can import it."""

    def test_no_model_module_imported(self) -> None:
        tree = ast.parse(Path(model_ddl.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(f"{node.module or ''}.{alias.name}".lstrip(".") for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        for module in ("model_base", "edge", "model_auth", "connection_manager"):
            assert not any(name == module or name.endswith(f".{module}") or name.startswith(f"{module}.") for name in imported)


# --- the statements of table_ddl() ----------------------------------------------------------------------------------


class TdlLog(BaseSurrealModel):
    """G3: a ``TYPE ANY`` table declared ``SCHEMALESS``: no ``FLEXIBLE`` on its free-form object."""

    model_config = SurrealConfigDict(
        table_name="tdl_log", table_type=TableType.ANY, schema_mode=SchemaMode.SCHEMALESS, connection="tdl"
    )

    id: str | None = None
    payload: dict[str, Any]
    extra: Any = None


class TdlUser(BaseSurrealModel):
    """G4: a ``TableType.USER`` table is ``SCHEMAFULL``."""

    model_config = SurrealConfigDict(table_name="tdl_user", table_type=TableType.USER, connection="tdl")

    id: str | None = None
    email: str


class TdlUserHash(BaseSurrealModel):
    """G4: a ``TableType.HASH`` table is ``SCHEMALESS``."""

    model_config = SurrealConfigDict(table_name="tdl_user", table_type=TableType.HASH, connection="tdl")

    id: str | None = None
    email: str


class TdlUserStream(BaseSurrealModel):
    """G4: a ``TableType.STREAM`` table with a change feed."""

    model_config = SurrealConfigDict(table_name="tdl_user", table_type=TableType.STREAM, changefeed="1d", connection="tdl")

    id: str | None = None
    email: str


class TdlPermNone(BaseSurrealModel):
    """G5: ``permissions={}`` declares ``PERMISSIONS NONE``."""

    model_config = SurrealConfigDict(table_name="tdl_g5", permissions={}, connection="tdl")

    id: str | None = None


class TdlPermFull(BaseSurrealModel):
    """G5: ``FULL`` on the four operations, in any case."""

    model_config = SurrealConfigDict(
        table_name="tdl_g5",
        permissions={"select": "full", "create": "full", "update": "full", "delete": "full"},
        connection="tdl",
    )

    id: str | None = None


class TdlPermWhere(BaseSurrealModel):
    """G5: one ``WHERE`` rule, the other operations ``NONE``."""

    model_config = SurrealConfigDict(table_name="tdl_g5", permissions={"select": "WHERE owner = $auth.id"}, connection="tdl")

    id: str | None = None


class TdlFeed(BaseSurrealModel):
    """G5: a change feed written normalised."""

    model_config = SurrealConfigDict(table_name="tdl_g5", changefeed=" 1h   include original ", connection="tdl")

    id: str | None = None


class TdlQuote(BaseSurrealModel):
    """G5: a comment holding a quote."""

    model_config = SurrealConfigDict(table_name="tdl_g5", comment="it's", connection="tdl")

    id: str | None = None


class TdlCart(BaseSurrealModel):
    """G6: a computed field, written ``TYPE option<…> VALUE``."""

    model_config = SurrealConfigDict(table_name="tdl_cart", connection="tdl")

    id: str | None = None
    items: list[str] = Field(default_factory=list)
    total: Computed[int] = computed("count(items)")


class TdlKeyword(BaseSurrealModel):
    """G10: a field named after a SurrealQL keyword."""

    model_config = SurrealConfigDict(table_name="tdl_kw", connection="tdl")

    id: str | None = None
    select: int = 0


class TdlOdd(BaseSurrealModel):
    """G10: columns that are not identifiers, one holding a dot (one segment)."""

    model_config = SurrealConfigDict(table_name="tdl_odd", connection="tdl")

    id: str | None = None
    first: str = Field(alias="first-name")
    dotted: int = Field(default=0, alias="a.b")


class TdlNoteStatus(BaseSurrealModel):
    """G11: a ``server_fields`` column with a constant default keeps it as its ``DEFAULT`` on a new column."""

    model_config = SurrealConfigDict(table_name="tdl_note", server_fields=["status"], connection="tdl")

    id: str | None = None
    body: str
    status: str = "draft"


class TdlNoteStamped(BaseSurrealModel):
    """G12: ``server_fields`` columns without a constant default get no ``DEFAULT``."""

    model_config = SurrealConfigDict(table_name="tdl_note", server_fields=["stamp", "owner_ref"], connection="tdl")

    id: str | None = None
    body: str
    stamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    owner_ref: str


class TdlLikesBare(BaseSurrealEdge):
    """An edge model without the table keys: its relation statement, then its data fields."""

    model_config = SurrealConfigDict(
        table_name="tdl_likes_bare",
        relation_in="tdl_person",
        relation_out=["tdl_post", "tdl_book"],
        enforced=True,
        connection="tdl",
    )

    stars: int = 1


class TdlStatsBare(BaseSurrealModel):
    """A view model without the table keys: its view statement."""

    model_config = SurrealConfigDict(table_name="tdl_stats", view_query=_VIEW_QUERY, connection="tdl")

    n: int = 0


_G1 = [
    "DEFINE TABLE OVERWRITE `tdl_person` TYPE NORMAL SCHEMAFULL COMMENT 'People' CHANGEFEED 7d PERMISSIONS FOR select "
    "FULL, FOR create WHERE $auth.id != NONE, FOR update, delete NONE;",
    "DEFINE FIELD OVERWRITE `name` ON `tdl_person` TYPE string;",
    "DEFINE FIELD OVERWRITE `age` ON `tdl_person` TYPE int DEFAULT 18;",
    "DEFINE FIELD OVERWRITE `nickname` ON `tdl_person` TYPE option<string>;",
    "DEFINE FIELD OVERWRITE `tier` ON `tdl_person` TYPE 'free' | 'pro' DEFAULT 'free';",
    "DEFINE FIELD OVERWRITE `tags` ON `tdl_person` TYPE array<string>;",
    "DEFINE FIELD OVERWRITE `address` ON `tdl_person` TYPE option<object>;",
    "DEFINE FIELD OVERWRITE `address`.`city` ON `tdl_person` TYPE string;",
    "DEFINE FIELD OVERWRITE `address`.`zip` ON `tdl_person` TYPE option<string>;",
    "DEFINE FIELD OVERWRITE `meta` ON `tdl_person` TYPE object FLEXIBLE;",
    "DEFINE FIELD OVERWRITE `password_hash` ON `tdl_person` TYPE string;",
]

_G2 = [
    "DEFINE TABLE OVERWRITE `tdl_note` TYPE NORMAL;",
    "DEFINE FIELD OVERWRITE `body` ON `tdl_note` TYPE string;",
    "DEFINE FIELD OVERWRITE `done` ON `tdl_note` TYPE bool DEFAULT false;",
]

_G3 = [
    "DEFINE TABLE OVERWRITE `tdl_log` TYPE ANY SCHEMALESS;",
    "DEFINE FIELD OVERWRITE `payload` ON `tdl_log` TYPE object;",
    "DEFINE FIELD OVERWRITE `extra` ON `tdl_log` TYPE any;",
]

_G4_USER = [
    "DEFINE TABLE OVERWRITE `tdl_user` TYPE NORMAL SCHEMAFULL;",
    "DEFINE FIELD OVERWRITE `email` ON `tdl_user` TYPE string;",
]
_G4_HASH = ["DEFINE TABLE OVERWRITE `tdl_user` TYPE NORMAL SCHEMALESS;", _G4_USER[1]]
_G4_STREAM = ["DEFINE TABLE OVERWRITE `tdl_user` TYPE NORMAL CHANGEFEED 1d;", _G4_USER[1]]

_G5_NONE = ["DEFINE TABLE OVERWRITE `tdl_g5` TYPE NORMAL PERMISSIONS NONE;"]
_G5_FULL = ["DEFINE TABLE OVERWRITE `tdl_g5` TYPE NORMAL PERMISSIONS FULL;"]
_G5_WHERE = [
    "DEFINE TABLE OVERWRITE `tdl_g5` TYPE NORMAL PERMISSIONS FOR select WHERE owner = $auth.id, FOR create, update, delete "
    "NONE;"
]
_G5_FEED = ["DEFINE TABLE OVERWRITE `tdl_g5` TYPE NORMAL CHANGEFEED 1h INCLUDE ORIGINAL;"]
_G5_QUOTE = ["DEFINE TABLE OVERWRITE `tdl_g5` TYPE NORMAL COMMENT 'it\\'s';"]

_G6 = [
    "DEFINE TABLE OVERWRITE `tdl_cart` TYPE NORMAL;",
    "DEFINE FIELD OVERWRITE `items` ON `tdl_cart` TYPE array<string>;",
    "DEFINE FIELD OVERWRITE `total` ON `tdl_cart` TYPE option<int> VALUE count(items);",
]

_G9 = [
    "DEFINE TABLE OVERWRITE `tdl_kinds` TYPE NORMAL SCHEMAFULL;",
    "DEFINE FIELD OVERWRITE `dt` ON `tdl_kinds` TYPE datetime;",
    "DEFINE FIELD OVERWRITE `n` ON `tdl_kinds` TYPE number;",
    "DEFINE FIELD OVERWRITE `pt` ON `tdl_kinds` TYPE option<geometry<point>>;",
    "DEFINE FIELD OVERWRITE `tu` ON `tdl_kinds` TYPE [int, string];",
    "DEFINE FIELD OVERWRITE `st` ON `tdl_kinds` TYPE set<int>;",
    "DEFINE FIELD OVERWRITE `ie` ON `tdl_kinds` TYPE 1 | 2;",
    "DEFINE FIELD OVERWRITE `lit` ON `tdl_kinds` TYPE 'a' | 'b';",
    "DEFINE FIELD OVERWRITE `subs` ON `tdl_kinds` TYPE array<object>;",
    "DEFINE FIELD OVERWRITE `subs`.*.`city` ON `tdl_kinds` TYPE string;",
    "DEFINE FIELD OVERWRITE `subs`.*.`zip` ON `tdl_kinds` TYPE option<string>;",
    "DEFINE FIELD OVERWRITE `bag` ON `tdl_kinds` TYPE array;",
    "DEFINE FIELD OVERWRITE `bag`.* ON `tdl_kinds` TYPE any;",
    "DEFINE FIELD OVERWRITE `lol` ON `tdl_kinds` TYPE array<array>;",
    "DEFINE FIELD OVERWRITE `lol`.*.* ON `tdl_kinds` TYPE any;",
    "DEFINE FIELD OVERWRITE `ob` ON `tdl_kinds` TYPE option<array>;",
    "DEFINE FIELD OVERWRITE `ob`.* ON `tdl_kinds` TYPE any;",
    "DEFINE FIELD OVERWRITE `tags` ON `tdl_kinds` TYPE array<string>;",
    "DEFINE FIELD OVERWRITE `x` ON `tdl_kinds` TYPE option<int>;",
    "DEFINE FIELD OVERWRITE `owner` ON `tdl_kinds` TYPE option<object> FLEXIBLE;",
    "DEFINE FIELD OVERWRITE `u` ON `tdl_kinds` TYPE any;",
    "DEFINE FIELD OVERWRITE `sub` ON `tdl_kinds` TYPE object;",
    "DEFINE FIELD OVERWRITE `sub`.`a` ON `tdl_kinds` TYPE int DEFAULT 1;",
    "DEFINE FIELD OVERWRITE `sub`.`dbl` ON `tdl_kinds` TYPE int;",
    "DEFINE FIELD OVERWRITE `dl` ON `tdl_kinds` TYPE object | array FLEXIBLE;",
    "DEFINE FIELD OVERWRITE `dl`.* ON `tdl_kinds` TYPE any;",
    "DEFINE FIELD OVERWRITE `two` ON `tdl_kinds` TYPE array;",
    "DEFINE FIELD OVERWRITE `two`.* ON `tdl_kinds` TYPE any;",
    "DEFINE FIELD OVERWRITE `c` ON `tdl_kinds` TYPE option<array | object> FLEXIBLE;",
    "DEFINE FIELD OVERWRITE `c`.* ON `tdl_kinds` TYPE any;",
]

_G10_KEYWORD = [
    "DEFINE TABLE OVERWRITE `tdl_kw` TYPE NORMAL;",
    "DEFINE FIELD OVERWRITE `select` ON `tdl_kw` TYPE int DEFAULT 0;",
]
_G10_ODD = [
    "DEFINE TABLE OVERWRITE `tdl_odd` TYPE NORMAL;",
    "DEFINE FIELD OVERWRITE `first-name` ON `tdl_odd` TYPE string;",
    "DEFINE FIELD OVERWRITE `a.b` ON `tdl_odd` TYPE int DEFAULT 0;",
]

_G11 = [
    "DEFINE TABLE OVERWRITE `tdl_note` TYPE NORMAL;",
    "DEFINE FIELD OVERWRITE `body` ON `tdl_note` TYPE string;",
    "DEFINE FIELD OVERWRITE `status` ON `tdl_note` TYPE string DEFAULT 'draft';",
]

_G12 = [
    "DEFINE TABLE OVERWRITE `tdl_note` TYPE NORMAL;",
    "DEFINE FIELD OVERWRITE `body` ON `tdl_note` TYPE string;",
    "DEFINE FIELD OVERWRITE `stamp` ON `tdl_note` TYPE datetime;",
    "DEFINE FIELD OVERWRITE `owner_ref` ON `tdl_note` TYPE string;",
]

#: (model, its ``table_ddl()``) for every golden of a node model: G1–G6 and G9–G12.
_GOLDENS: list[tuple[type[BaseSurrealModel], list[str]]] = [
    (TdlPerson, _G1),
    (TdlNote, _G2),
    (TdlLog, _G3),
    (TdlUser, _G4_USER),
    (TdlUserHash, _G4_HASH),
    (TdlUserStream, _G4_STREAM),
    (TdlPermNone, _G5_NONE),
    (TdlPermFull, _G5_FULL),
    (TdlPermWhere, _G5_WHERE),
    (TdlFeed, _G5_FEED),
    (TdlQuote, _G5_QUOTE),
    (TdlCart, _G6),
    (TdlKinds, _G9),
    (TdlKeyword, _G10_KEYWORD),
    (TdlOdd, _G10_ODD),
    (TdlNoteStatus, _G11),
    (TdlNoteStamped, _G12),
]

_GOLDEN_IDS = [model.__name__ for model, _ in _GOLDENS]


def _if_not_exists(statements: list[str]) -> list[str]:
    """*statements* with ``IF NOT EXISTS`` where each says ``OVERWRITE``."""
    return [re.sub(r"^DEFINE (TABLE|FIELD) OVERWRITE ", r"DEFINE \1 IF NOT EXISTS ", statement) for statement in statements]


def _ux1(name: str) -> str:
    return (
        f'{name}: extra="allow" keeps fields the model does not declare, and a TableType.USER table is SCHEMAFULL, which '
        f'refuses them (SurrealDB 3.x) or drops them (2.x); remove extra="allow" from {name}.'
    )


class TestTableDdl:
    """``table_ddl()``: the goldens exactly, ``IF NOT EXISTS``, the refusals, edges and views, no connection needed."""

    @pytest.mark.parametrize(("model", "statements"), _GOLDENS, ids=_GOLDEN_IDS)
    def test_each_golden(self, model: type[BaseSurrealModel], statements: list[str]) -> None:
        assert model.table_ddl() == statements
        assert model.table_ddl(overwrite=True) == statements

    @pytest.mark.parametrize(("model", "statements"), _GOLDENS, ids=_GOLDEN_IDS)
    def test_each_golden_if_not_exists(self, model: type[BaseSurrealModel], statements: list[str]) -> None:
        kept = model.table_ddl(overwrite=False)
        assert kept == _if_not_exists(statements)
        assert all(" IF NOT EXISTS " in statement and "OVERWRITE" not in statement for statement in kept)

    def test_e1_overwrite_must_be_a_bool(self) -> None:
        for value in ("yes", 1, None):
            with pytest.raises(TypeError) as raised:
                TdlNote.table_ddl(overwrite=value)  # type: ignore[arg-type]
            assert str(raised.value) == f"overwrite= must be a bool, got {type(value).__name__}."

    def test_ux1_a_user_table_with_extra_allow(self) -> None:
        class TdlUserOpen(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_user", table_type=TableType.USER, extra="allow")

            email: str

        with pytest.raises(TypeError) as raised:
            TdlUserOpen.table_ddl()
        assert type(raised.value) is TypeError
        assert str(raised.value) == _ux1("TdlUserOpen")

    @pytest.mark.parametrize(
        ("expression", "reason"),
        [
            ("count(items) -- n", "it holds a SurrealQL comment marker (-- , #, // or /* */)"),
            ("count(items", "its brackets are not balanced"),
        ],
    )
    def test_cv1_through_table_ddl(self, expression: str, reason: str) -> None:
        class TdlBadTotal(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_cart")

            items: list[str] = Field(default_factory=list)
            total: Computed[int] = computed(expression)

        with pytest.raises(ValueError) as raised:
            TdlBadTotal.table_ddl()
        assert type(raised.value) is ValueError
        assert str(raised.value) == _cv1("TdlBadTotal", "total", reason)

    def test_fx1_through_table_ddl(self) -> None:
        class TdlFlexAge(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_name="tdl_flex_age", schema_mode=SchemaMode.SCHEMAFULL, flexible_fields=["age"]
            )

            age: int = 18

        with pytest.raises(ValueError) as raised:
            TdlFlexAge.table_ddl()
        assert str(raised.value) == _fx1("TdlFlexAge", "age", "int")

    async def test_no_connection_needed(self) -> None:
        await SurrealDBConnectionManager.unset_connection()
        assert TdlPerson.table_ddl() == _G1
        assert TdlKinds.table_ddl() == _G9

    def test_an_edge_model_without_the_keys(self) -> None:
        stars = "DEFINE FIELD OVERWRITE `stars` ON `tdl_likes_bare` TYPE int DEFAULT 1;"
        assert TdlLikesBare.relation_ddl() == (
            "DEFINE TABLE OVERWRITE `tdl_likes_bare` TYPE RELATION IN `tdl_person` OUT `tdl_post` | `tdl_book` ENFORCED;"
        )
        assert TdlLikesBare.table_ddl() == [TdlLikesBare.relation_ddl(), stars]
        assert TdlLikesBare.table_ddl(overwrite=False) == [
            TdlLikesBare.relation_ddl(overwrite=False),
            stars.replace("OVERWRITE", "IF NOT EXISTS"),
        ]
        assert TdlLink.table_ddl() == [
            "DEFINE TABLE OVERWRITE `tdl_link` TYPE RELATION;",
            "DEFINE FIELD OVERWRITE `stars` ON `tdl_link` TYPE int DEFAULT 1;",
        ]

    def test_a_view_model_without_the_keys(self) -> None:
        assert TdlStatsBare.table_ddl() == [TdlStatsBare.view_ddl()]
        assert TdlStatsBare.table_ddl(overwrite=False) == [TdlStatsBare.view_ddl(overwrite=False)]
        assert TdlStatsBare.table_ddl() == [f"DEFINE TABLE OVERWRITE `tdl_stats` AS ({_VIEW_QUERY});"]

    def test_flexible_only_on_a_schemafull_model(self) -> None:
        class TdlLooseMeta(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_loose_meta", flexible_fields=["meta"])

            meta: dict[str, Any] = Field(default_factory=dict)
            owner: TdlNote | None = None

        assert "FLEXIBLE" not in " ".join(TdlLog.table_ddl())
        assert "DEFINE FIELD OVERWRITE `meta` ON `tdl_person` TYPE object FLEXIBLE;" in TdlPerson.table_ddl()
        assert TdlLooseMeta.table_ddl() == [
            "DEFINE TABLE OVERWRITE `tdl_loose_meta` TYPE NORMAL;",
            "DEFINE FIELD OVERWRITE `meta` ON `tdl_loose_meta` TYPE object;",
            "DEFINE FIELD OVERWRITE `owner` ON `tdl_loose_meta` TYPE option<object>;",
        ]

    def test_flexible_fields_on_a_schemafull_model(self) -> None:
        class TdlFlexMeta(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_name="tdl_flex_meta", schema_mode=SchemaMode.SCHEMAFULL, flexible_fields=["address"]
            )

            address: TdlAddress | None = None
            open: TdlOpen = Field(default_factory=TdlOpen)
            extra_meta: TdlAddress | None = Field(default=None, json_schema_extra={"flexible": True})

        assert TdlFlexMeta.table_ddl() == [
            "DEFINE TABLE OVERWRITE `tdl_flex_meta` TYPE NORMAL SCHEMAFULL;",
            "DEFINE FIELD OVERWRITE `address` ON `tdl_flex_meta` TYPE option<object> FLEXIBLE;",
            "DEFINE FIELD OVERWRITE `address`.`city` ON `tdl_flex_meta` TYPE string;",
            "DEFINE FIELD OVERWRITE `address`.`zip` ON `tdl_flex_meta` TYPE option<string>;",
            "DEFINE FIELD OVERWRITE `open` ON `tdl_flex_meta` TYPE object FLEXIBLE;",
            "DEFINE FIELD OVERWRITE `open`.`k` ON `tdl_flex_meta` TYPE int DEFAULT 0;",
            "DEFINE FIELD OVERWRITE `extra_meta` ON `tdl_flex_meta` TYPE option<object> FLEXIBLE;",
            "DEFINE FIELD OVERWRITE `extra_meta`.`city` ON `tdl_flex_meta` TYPE string;",
            "DEFINE FIELD OVERWRITE `extra_meta`.`zip` ON `tdl_flex_meta` TYPE option<string>;",
        ]

    def test_the_kitchen_computed_field(self) -> None:
        """The typed computed field of the rich model: ``TYPE option<int> VALUE``, inherited from its base."""
        tot = "DEFINE FIELD OVERWRITE `tot` ON `tdl_kitchen` TYPE option<int> VALUE i + 1;"
        assert tot in TdlKitchen.table_ddl()
        assert tot in TdlKitchenBase.table_ddl()
        assert TdlKitchen.get_computed_fields() == {"tot": "i + 1"}
        assert TdlKitchen.table_ddl()[-1] == "DEFINE FIELD OVERWRITE `st` ON `tdl_kitchen` TYPE set<int>;"
        assert not any("`st`" in statement for statement in TdlKitchenBase.table_ddl())

    def test_the_kitchen_statements(self) -> None:
        assert TdlKitchenBase.table_ddl() == _KITCHEN_BASE

    def test_declared_clauses_order(self) -> None:
        clauses = schema.TableClauses(
            schema_mode="SCHEMAFULL",
            comment="c",
            changefeed="1d INCLUDE ORIGINAL",
            permissions={"select": "FULL", "create": "NONE", "update": "NONE", "delete": "NONE"},
        )
        assert schema.declared_clauses(clauses) == [
            "SCHEMAFULL",
            "COMMENT 'c'",
            "CHANGEFEED 1d INCLUDE ORIGINAL",
            "PERMISSIONS FOR select FULL, FOR create, update, delete NONE",
        ]
        assert schema.declared_clauses(schema.TableClauses()) == []
        assert schema.declared_clauses(schema.TableClauses(schema_mode="SCHEMALESS")) == ["SCHEMALESS"]
        none = dict.fromkeys(("select", "create", "update", "delete"), "NONE")
        assert schema.declared_clauses(schema.TableClauses(permissions=none)) == ["PERMISSIONS NONE"]

    def test_table_statement(self) -> None:
        assert schema.table_statement("tdl_x", "TYPE NORMAL", schema.TableClauses(), True) == (
            "DEFINE TABLE OVERWRITE `tdl_x` TYPE NORMAL;"
        )
        assert schema.table_statement("tdl_x", "TYPE ANY", schema.TableClauses(comment="c"), False) == (
            "DEFINE TABLE IF NOT EXISTS `tdl_x` TYPE ANY COMMENT 'c';"
        )
        with pytest.raises(ValueError):
            schema.table_statement("tdl x", "TYPE NORMAL", schema.TableClauses(), True)

    def test_the_declared_clauses_of_a_model(self) -> None:
        assert TdlPerson._declared_table_clauses() == schema.TableClauses(
            schema_mode="SCHEMAFULL",
            comment="People",
            changefeed="7d",
            permissions={"select": "FULL", "create": "WHERE $auth.id != NONE", "update": "NONE", "delete": "NONE"},
        )
        assert TdlNote._declared_table_clauses() == schema.TableClauses()
        assert TdlUserHash._declared_table_clauses() == schema.TableClauses(schema_mode="SCHEMALESS")
        assert TdlFeed._declared_table_clauses() == schema.TableClauses(changefeed="1h INCLUDE ORIGINAL")

    @pytest.mark.parametrize(
        ("permissions", "text"),
        [
            ({"select": "FULL", "create": "FULL", "update": "FULL"}, None),
            ({}, None),
            ({"select": "NONE", "create": "NONE", "update": "NONE"}, "NONE"),
            (
                {"select": "WHERE $auth.id = id", "create": "FULL", "update": "FULL"},
                "FOR select WHERE $auth.id = id, FOR create, update FULL",
            ),
            ({"select": "FULL", "create": "NONE", "update": "FULL"}, "FOR select, update FULL, FOR create NONE"),
            ({"update": "NONE", "select": "WHERE true"}, "FOR select WHERE true, FOR create FULL, FOR update NONE"),
        ],
    )
    def test_field_permissions_text(self, permissions: dict[str, str], text: str | None) -> None:
        assert model_ddl.field_permissions_text(permissions) == text

    def test_field_statement(self) -> None:
        column = model_ddl.Column(path=("subs", "*", "city"), field="subs", kind=parse_kind("string"))
        assert model_ddl.field_statement("tdl_x", column, overwrite=True, schemafull=True) == (
            "DEFINE FIELD OVERWRITE `subs`.*.`city` ON `tdl_x` TYPE string;"
        )
        item = model_ddl.Column(path=("bag", "*"), field="bag", kind=parse_kind("any"), holds_any=True, item=True)
        assert model_ddl.field_statement("tdl_x", item, overwrite=False, schemafull=True) == (
            "DEFINE FIELD IF NOT EXISTS `bag`.* ON `tdl_x` TYPE any;"
        )
        with pytest.raises(TypeError):
            model_ddl.field_statement("tdl_x", column, overwrite="yes", schemafull=True)  # type: ignore[arg-type]


# --- the carried statements: a table and fields that exist ----------------------------------------------------------


class TdlNoteTuned(BaseSurrealModel):
    """The model of the table tuned by hand: ``TdlNote``'s fields, ``email`` and ``created`` (a server field)."""

    model_config = SurrealConfigDict(table_name="tdl_note", server_fields=["created"], connection="tdl")

    id: str | None = None
    body: str
    done: bool = False
    email: str | None = None
    created: datetime | None = None


class TdlNoteMine(BaseSurrealModel):
    """The same table with its own schema mode, comment and permissions: those win over the stored ones."""

    model_config = SurrealConfigDict(
        table_name="tdl_note",
        schema_mode=SchemaMode.SCHEMALESS,
        comment="mine",
        permissions={"select": "FULL"},
        server_fields=["created"],
        connection="tdl",
    )

    id: str | None = None
    body: str
    done: bool = False
    email: str | None = None
    created: datetime | None = None


class TdlDrop(BaseSurrealModel):
    """A model without the table keys on a ``DROP`` table."""

    model_config = SurrealConfigDict(table_name="tdl_drop", connection="tdl")

    id: str | None = None
    body: str


class TdlNoteShapes(BaseSurrealModel):
    """Columns over stored fields: a constant default, a free-form object, a sub-model's object, ``Any``."""

    model_config = SurrealConfigDict(table_name="tdl_note", connection="tdl")

    id: str | None = None
    age: int = 18
    meta: dict[str, Any] = Field(default_factory=dict)
    address: TdlAddress | None = None
    tags: list[str] = Field(default_factory=list)
    anything: Any = None


#: The stored tables as SurrealDB renders them in ``INFO FOR DB`` — the same text on 3.3.4, 2.7.0 and 3.1.5: with
#: ``DROP``; without it (the table the end-to-end tests tune by hand: a ``DROP`` table stores no record); a ``DROP``
#: table of its own.
_STORED_NOTE_DROP = (
    "DEFINE TABLE tdl_note TYPE ANY DROP SCHEMAFULL COMMENT 'keep' CHANGEFEED 1h INCLUDE ORIGINAL PERMISSIONS FOR select "
    "WHERE owner = $auth.id, FOR create FULL, FOR update, delete NONE"
)
_STORED_NOTE = (
    "DEFINE TABLE tdl_note TYPE ANY SCHEMAFULL COMMENT 'keep' CHANGEFEED 1h INCLUDE ORIGINAL PERMISSIONS FOR select "
    "WHERE owner = $auth.id, FOR create FULL, FOR update, delete NONE"
)
_STORED_DROP = "DEFINE TABLE tdl_drop TYPE ANY DROP SCHEMALESS COMMENT 'd' PERMISSIONS NONE"

_K1 = (
    "DEFINE TABLE OVERWRITE `tdl_note` TYPE NORMAL DROP SCHEMAFULL COMMENT 'keep' CHANGEFEED 1h INCLUDE ORIGINAL "
    "PERMISSIONS FOR select WHERE owner = $auth.id, FOR create FULL, FOR update, delete NONE;"
)
_K2 = (
    "DEFINE TABLE OVERWRITE `tdl_note` TYPE NORMAL DROP SCHEMALESS COMMENT 'mine' CHANGEFEED 1h INCLUDE ORIGINAL "
    "PERMISSIONS FOR select FULL, FOR create, update, delete NONE;"
)
_K1E = (
    "DEFINE TABLE OVERWRITE `tdl_note` TYPE NORMAL SCHEMAFULL COMMENT 'keep' CHANGEFEED 1h INCLUDE ORIGINAL PERMISSIONS "
    "FOR select WHERE owner = $auth.id, FOR create FULL, FOR update, delete NONE;"
)
_K2E = (
    "DEFINE TABLE OVERWRITE `tdl_note` TYPE NORMAL SCHEMALESS COMMENT 'mine' CHANGEFEED 1h INCLUDE ORIGINAL PERMISSIONS "
    "FOR select FULL, FOR create, update, delete NONE;"
)
_K1D = [
    "DEFINE TABLE OVERWRITE `tdl_drop` TYPE NORMAL DROP COMMENT 'd';",
    "DEFINE FIELD OVERWRITE `body` ON `tdl_drop` TYPE string;",
]

#: (case, model, stored table, carried statement).
_CARRIED_TABLES: list[tuple[str, type[BaseSurrealModel], str, str]] = [
    ("K1", TdlNote, _STORED_NOTE_DROP, _K1),
    ("K2", TdlNoteMine, _STORED_NOTE_DROP, _K2),
    ("K1E", TdlNoteTuned, _STORED_NOTE, _K1E),
    ("K2E", TdlNoteMine, _STORED_NOTE, _K2E),
]

#: A stored view with a hand-set mode, comment, change feed and permissions (the same text on every line), and its
#: carried statement for a view model without the table keys.
_STORED_VIEW = (
    'DEFINE TABLE tdl_stats TYPE ANY DROP SCHEMAFULL COMMENT "it\'s" AS SELECT count() AS n FROM tdl_note GROUP ALL '
    "CHANGEFEED 1h PERMISSIONS FOR select WHERE true, FOR create, update, delete NONE"
)
_K8 = (
    f"DEFINE TABLE OVERWRITE `tdl_stats` DROP SCHEMAFULL AS ({_VIEW_QUERY}) COMMENT 'it\\'s' CHANGEFEED 1h PERMISSIONS "
    "FOR select WHERE true, FOR create, update, delete NONE;"
)

#: v0.33.0's carried relation statements: the stored definitions, the type clause and what it sent (copied).
_RELATION_CLAUSE = "TYPE RELATION IN `person` OUT `post` | `book` ENFORCED"
_LIKES_CLAUSE = "TYPE RELATION IN `rel_person` OUT `rel_post` | `rel_book` ENFORCED"
_LIKES_STORED = (
    "DEFINE TABLE rel_likes TYPE ANY SCHEMAFULL COMMENT 'keep' CHANGEFEED 1h INCLUDE ORIGINAL PERMISSIONS FOR select "
    "WHERE in = $auth.id, FOR create FULL, FOR update, delete NONE"
)
_LIKES_READ_BACK = (
    "DEFINE TABLE rel_likes TYPE RELATION IN rel_person OUT rel_post | rel_book ENFORCED SCHEMAFULL COMMENT 'keep' "
    "CHANGEFEED 1h INCLUDE ORIGINAL PERMISSIONS FOR select WHERE in = $auth.id, FOR create FULL, FOR update, delete NONE"
)
_RELATION_CARRIED: list[tuple[str, str, str, str, str]] = [
    (
        "K1",
        "t",
        _RELATION_CLAUSE,
        'DEFINE TABLE t TYPE NORMAL SCHEMAFULL COMMENT "it\'s \\\\ a \\"c\\" line" CHANGEFEED 1h INCLUDE ORIGINAL '
        "PERMISSIONS FOR select WHERE in = $auth.id OR out = $auth.id, FOR create FULL, FOR update, delete NONE",
        "DEFINE TABLE OVERWRITE `t` TYPE RELATION IN `person` OUT `post` | `book` ENFORCED SCHEMAFULL COMMENT "
        "'it\\'s \\\\ a \"c\" line' CHANGEFEED 1h INCLUDE ORIGINAL PERMISSIONS FOR select WHERE in = $auth.id OR "
        "out = $auth.id, FOR create FULL, FOR update, delete NONE;",
    ),
    (
        "K2",
        "t",
        _RELATION_CLAUSE,
        "DEFINE TABLE t TYPE ANY DROP SCHEMALESS PERMISSIONS NONE",
        "DEFINE TABLE OVERWRITE `t` TYPE RELATION IN `person` OUT `post` | `book` ENFORCED DROP;",
    ),
    (
        "K3",
        "t",
        _RELATION_CLAUSE,
        "DEFINE TABLE t TYPE ANY SCHEMALESS PERMISSIONS FULL",
        "DEFINE TABLE OVERWRITE `t` TYPE RELATION IN `person` OUT `post` | `book` ENFORCED PERMISSIONS FULL;",
    ),
    (
        "K4",
        "t",
        _RELATION_CLAUSE,
        "DEFINE TABLE t TYPE ANY SCHEMALESS PERMISSIONS NONE",
        "DEFINE TABLE OVERWRITE `t` TYPE RELATION IN `person` OUT `post` | `book` ENFORCED;",
    ),
    (
        "K5",
        "t",
        _RELATION_CLAUSE,
        "DEFINE TABLE t TYPE RELATION IN company OUT post ENFORCED SCHEMAFULL PERMISSIONS FOR select FULL, FOR create, "
        "update, delete NONE",
        "DEFINE TABLE OVERWRITE `t` TYPE RELATION IN `person` OUT `post` | `book` ENFORCED SCHEMAFULL PERMISSIONS FOR "
        "select FULL, FOR create, update, delete NONE;",
    ),
    (
        "K6",
        "rel_likes",
        _LIKES_CLAUSE,
        _LIKES_STORED,
        "DEFINE TABLE OVERWRITE `rel_likes` TYPE RELATION IN `rel_person` OUT `rel_post` | `rel_book` ENFORCED "
        "SCHEMAFULL COMMENT 'keep' CHANGEFEED 1h INCLUDE ORIGINAL PERMISSIONS FOR select WHERE in = $auth.id, FOR "
        "create FULL, FOR update, delete NONE;",
    ),
    (
        "K6-read-back",
        "rel_likes",
        _LIKES_CLAUSE,
        _LIKES_READ_BACK,
        "DEFINE TABLE OVERWRITE `rel_likes` TYPE RELATION IN `rel_person` OUT `rel_post` | `rel_book` ENFORCED "
        "SCHEMAFULL COMMENT 'keep' CHANGEFEED 1h INCLUDE ORIGINAL PERMISSIONS FOR select WHERE in = $auth.id, FOR "
        "create FULL, FOR update, delete NONE;",
    ),
    (
        "K7",
        "rel_likes",
        "TYPE RELATION",
        _LIKES_READ_BACK,
        "DEFINE TABLE OVERWRITE `rel_likes` TYPE RELATION SCHEMAFULL COMMENT 'keep' CHANGEFEED 1h INCLUDE ORIGINAL "
        "PERMISSIONS FOR select WHERE in = $auth.id, FOR create FULL, FOR update, delete NONE;",
    ),
]


def _kept_table(model: type[BaseSurrealModel], stored: str | TableState) -> str | None:
    """``table_statement_keeping()`` of *model*'s type clause and declared clauses over the stored table *stored*."""
    state = parse_define_table(stored) if isinstance(stored, str) else stored
    return schema.table_statement_keeping(model.get_table_name(), model._table_head(), model._declared_table_clauses(), state)


class TestCarriedTable:
    """Over a table that exists, ``table_statement_keeping()`` writes the model's type clause, then the ``DROP``, schema
    mode, comment, change feed and permissions — each declared one, else the stored one —, ``None`` when a carried
    value fails its check; ``relation_statement_keeping()`` gives v0.33.0's statements through it. Views carry the same
    clauses around ``AS``. The guards' texts."""

    @pytest.mark.parametrize(
        ("model", "stored", "statement"), [case[1:] for case in _CARRIED_TABLES], ids=[case[0] for case in _CARRIED_TABLES]
    )
    def test_each_carried_table(self, model: type[BaseSurrealModel], stored: str, statement: str) -> None:
        assert _kept_table(model, stored) == statement

    def test_a_drop_table_of_its_own(self) -> None:
        """``SCHEMALESS``, the default, is not written; the new column is written as on a new table."""
        state = parse_define_table(_STORED_DROP)
        statements = [
            _kept_table(TdlDrop, state),
            *(
                model_ddl.field_statement("tdl_drop", column, overwrite=True, schemafull=False)
                for column in TdlDrop._table_columns()
            ),
        ]
        assert statements == _K1D

    def test_a_declared_mode_is_written_either_way(self) -> None:
        """A declared ``SCHEMALESS`` turns a ``SCHEMAFULL`` table back; a declared ``SCHEMAFULL`` is written over a
        ``SCHEMALESS`` one; ``PERMISSIONS NONE`` when the declared permissions are all ``NONE``."""
        clauses = schema.TableClauses(schema_mode="SCHEMAFULL", permissions=schema.permissions_rules({}, model="M"))
        assert schema.table_statement_keeping("tdl_drop", "TYPE NORMAL", clauses, parse_define_table(_STORED_DROP)) == (
            "DEFINE TABLE OVERWRITE `tdl_drop` TYPE NORMAL DROP SCHEMAFULL COMMENT 'd' PERMISSIONS NONE;"
        )

    def test_a_declared_change_feed_wins(self) -> None:
        clauses = schema.TableClauses(changefeed="7d")
        assert schema.table_statement_keeping("tdl_note", "TYPE ANY", clauses, parse_define_table(_STORED_NOTE)) == (
            "DEFINE TABLE OVERWRITE `tdl_note` TYPE ANY SCHEMAFULL COMMENT 'keep' CHANGEFEED 7d PERMISSIONS FOR select "
            "WHERE owner = $auth.id, FOR create FULL, FOR update, delete NONE;"
        )

    @pytest.mark.parametrize(
        ("table", "clause", "stored", "statement"),
        [case[1:] for case in _RELATION_CARRIED],
        ids=[case[0] for case in _RELATION_CARRIED],
    )
    def test_v033_relation_statements_unchanged(self, table: str, clause: str, stored: str, statement: str) -> None:
        state = parse_define_table(stored)
        assert schema.table_statement_keeping(table, clause, schema.TableClauses(), state) == statement
        assert schema.relation_statement_keeping(table, clause, state) == statement

    @pytest.mark.parametrize(
        "state",
        [
            TableState(name="tdl_note", comment="a\x00b"),
            TableState(name="tdl_note", permissions={"select": "WHERE a; b"}),
            TableState(name="tdl_note", permissions={"update": "WHERE a = 'b"}),
            TableState(name="tdl_note", permissions={"delete": "SOMETIMES"}),
        ],
        ids=["nul-in-comment", "top-level-semicolon", "unterminated-literal", "not-a-rule"],
    )
    def test_none_for_a_carried_value_that_fails_its_check(self, state: TableState) -> None:
        assert _kept_table(TdlNote, state) is None
        assert schema.relation_statement_keeping("tdl_note", "TYPE RELATION", state) is None

    def test_a_declared_clause_is_not_checked_as_carried(self) -> None:
        """Only what is carried is checked: a declared comment or declared permissions replace a faulty stored one."""
        bad_comment = TableState(name="tdl_note", comment="a\x00b")
        bad_rule = TableState(name="tdl_note", permissions={"select": "WHERE a; b"})
        assert _kept_table(TdlNoteMine, bad_comment) == (
            "DEFINE TABLE OVERWRITE `tdl_note` TYPE NORMAL SCHEMALESS COMMENT 'mine' PERMISSIONS FOR select FULL, FOR "
            "create, update, delete NONE;"
        )
        assert _kept_table(TdlNoteMine, bad_rule) == _kept_table(TdlNoteMine, bad_comment)

    @pytest.mark.parametrize(("table", "error"), [("a-b", ValueError), (3, TypeError)], ids=repr)
    def test_the_table_name_is_checked(self, table: Any, error: type[Exception]) -> None:
        state = parse_define_table(_STORED_DROP)
        with pytest.raises(error):
            schema.table_statement_keeping(table, "TYPE NORMAL", schema.TableClauses(), state)
        with pytest.raises(error):
            schema.view_statement_keeping(table, _VIEW_QUERY, schema.TableClauses(), state)

    # --- views ---------------------------------------------------------------------------------------------------

    def test_view_statement_without_clauses_is_unchanged(self) -> None:
        bare = f"DEFINE TABLE OVERWRITE `tdl_stats` AS ({_VIEW_QUERY});"
        assert schema.view_statement("tdl_stats", _VIEW_QUERY, True) == bare
        assert schema.view_statement("tdl_stats", _VIEW_QUERY, True, clauses=schema.TableClauses()) == bare

    def test_view_statement_with_declared_clauses(self) -> None:
        """The comment, change feed and permissions after ``AS (…)``."""
        clauses = schema.TableClauses(
            comment="Counts",
            changefeed="1h INCLUDE ORIGINAL",
            permissions=schema.permissions_rules({"select": "FULL"}, model="TdlStats"),
        )
        assert schema.view_statement("tdl_stats", _VIEW_QUERY, False, clauses=clauses) == (
            f"DEFINE TABLE IF NOT EXISTS `tdl_stats` AS ({_VIEW_QUERY}) COMMENT 'Counts' CHANGEFEED 1h INCLUDE ORIGINAL "
            "PERMISSIONS FOR select FULL, FOR create, update, delete NONE;"
        )

    def test_k8_a_stored_view(self) -> None:
        state = parse_define_table(_STORED_VIEW)
        assert schema.view_statement_keeping("tdl_stats", _VIEW_QUERY, TdlStatsBare._declared_table_clauses(), state) == _K8

    def test_k8_the_declared_clauses_replace_the_stored_ones(self) -> None:
        clauses = schema.TableClauses(
            comment="Counts", permissions=schema.permissions_rules({"select": "FULL"}, model="TdlStats")
        )
        assert schema.view_statement_keeping("tdl_stats", _VIEW_QUERY, clauses, parse_define_table(_STORED_VIEW)) == (
            f"DEFINE TABLE OVERWRITE `tdl_stats` DROP SCHEMAFULL AS ({_VIEW_QUERY}) COMMENT 'Counts' CHANGEFEED 1h "
            "PERMISSIONS FOR select FULL, FOR create, update, delete NONE;"
        )

    def test_a_stored_view_with_defaults_only_is_the_bare_statement(self) -> None:
        state = parse_define_table(f"DEFINE TABLE tdl_stats TYPE ANY SCHEMALESS AS {_VIEW_QUERY} PERMISSIONS NONE")
        assert schema.view_statement_keeping("tdl_stats", _VIEW_QUERY, schema.TableClauses(), state) == (
            schema.view_statement("tdl_stats", _VIEW_QUERY, True)
        )

    def test_none_for_a_stored_view_comment_holding_nul(self) -> None:
        state = TableState(name="tdl_stats", schema_mode="SCHEMALESS", comment="a\x00b", view_query=_VIEW_QUERY)
        assert schema.view_statement_keeping("tdl_stats", _VIEW_QUERY, schema.TableClauses(), state) is None
        declared = schema.TableClauses(comment="Counts")
        assert schema.view_statement_keeping("tdl_stats", _VIEW_QUERY, declared, state) == (
            f"DEFINE TABLE OVERWRITE `tdl_stats` AS ({_VIEW_QUERY}) COMMENT 'Counts';"
        )


# --- the guards' texts ----------------------------------------------------------------------------------------------


def _tg1(name: str, method: str, table: str) -> str:
    return (
        f"{name}.{method}(): the table '{table}' is a materialized view; defining it as an ordinary table would drop its "
        f"query. Declare view_query on {name}, give it another table_name, or remove the view first. Nothing was "
        "defined."
    )


def _tg2(name: str, method: str, table: str) -> str:
    return (
        f"{name}.{method}(): the definition of the table '{table}' could not be read, so the ORM cannot keep the clauses "
        f"{name} does not declare (schema mode, permissions, change feed, comment). Nothing was defined."
    )


def _tg3(name: str, method: str, table: str) -> str:
    return (
        f"{name}.{method}(): the table '{table}' is a relation table (TYPE RELATION) and {name} is not an edge model; "
        "defining it as an ordinary table would drop its relation constraint and refuse every edge. Derive "
        f"{name} from BaseSurrealEdge, or give it another table_name. Nothing was defined."
    )


def _tg4(name: str, method: str, table: str, field: str) -> str:
    return (
        f"{name}.{method}(): the definition of the field '{field}' on the table '{table}' could not be read, so the ORM "
        f"cannot keep the clauses {name} does not write (ASSERT, PERMISSIONS, READONLY, DEFAULT, VALUE, COMPUTED, "
        "COMMENT). Nothing was defined."
    )


#: RG1–RG3 as v0.33.0's ``define_relation()`` raises them, and as ``define_table()`` of an edge model does.
_RG_DEFINE_RELATION = {
    "RG1": (
        "TdlLikes.define_relation(): the table 'tdl_likes' is a materialized view; defining it as a relation table would "
        "drop its query and turn it into an ordinary table that no longer updates. Give TdlLikes another table_name, or "
        "remove the view first. Nothing was defined."
    ),
    "RG2": (
        "TdlLikes.define_relation(): the definition of the table 'tdl_likes' could not be read, so the ORM cannot keep "
        "its other clauses (schema mode, permissions, change feed, comment). Nothing was defined."
    ),
    "RG3": (
        "TdlLikes.define_relation(overwrite=False): the table 'tdl_likes' exists and is not a relation table; DEFINE "
        "TABLE IF NOT EXISTS would leave it as it is, and the relation constraint would not apply. Call "
        "define_relation() with overwrite=True, which keeps the table's other clauses. Nothing was defined."
    ),
}
_RG_DEFINE_TABLE = {
    "RG1": (
        "TdlLikes.define_table(): the table 'tdl_likes' is a materialized view; defining it as a relation table would "
        "drop its query and turn it into an ordinary table that no longer updates. Give TdlLikes another table_name, or "
        "remove the view first. Nothing was defined."
    ),
    "RG2": (
        "TdlLikes.define_table(): the definition of the table 'tdl_likes' could not be read, so the ORM cannot keep its "
        "other clauses (schema mode, permissions, change feed, comment). Nothing was defined."
    ),
    "RG3": (
        "TdlLikes.define_table(overwrite=False): the table 'tdl_likes' exists and is not a relation table; DEFINE TABLE "
        "IF NOT EXISTS would leave it as it is, and the relation constraint would not apply. Call define_table() with "
        "overwrite=True, which keeps the table's other clauses. Nothing was defined."
    ),
}


class TestGuardTexts:
    """``table_refused()`` TG1–TG4 naming ``define_table()`` or ``define_access()``; ``relation_table_refused()``
    RG1–RG3 — v0.33.0's texts by default, naming ``define_table()`` when it is the caller; RG4."""

    @pytest.mark.parametrize("method", ["define_table", "define_access"])
    def test_tg1_to_tg4(self, method: Any) -> None:
        assert schema.table_refused("TG1", "TdlNote", "tdl_note", method=method) == _tg1("TdlNote", method, "tdl_note")
        assert schema.table_refused("TG2", "TdlNote", "tdl_note", method=method) == _tg2("TdlNote", method, "tdl_note")
        assert schema.table_refused("TG3", "TdlNote", "tdl_note", method=method) == _tg3("TdlNote", method, "tdl_note")
        assert schema.table_refused("TG4", "TdlNote", "tdl_note", method=method, field="address.city") == _tg4(
            "TdlNote", method, "tdl_note", "address.city"
        )

    def test_tg_texts_exactly(self) -> None:
        """Word for word, ``define_table`` the default."""
        assert schema.table_refused("TG1", "TdlNote", "tdl_note") == (
            "TdlNote.define_table(): the table 'tdl_note' is a materialized view; defining it as an ordinary table would "
            "drop its query. Declare view_query on TdlNote, give it another table_name, or remove the view first. "
            "Nothing was defined."
        )
        assert schema.table_refused("TG2", "TdlNote", "tdl_note") == (
            "TdlNote.define_table(): the definition of the table 'tdl_note' could not be read, so the ORM cannot keep "
            "the clauses TdlNote does not declare (schema mode, permissions, change feed, comment). Nothing was defined."
        )
        assert schema.table_refused("TG3", "TdlNote", "tdl_note") == (
            "TdlNote.define_table(): the table 'tdl_note' is a relation table (TYPE RELATION) and TdlNote is not an edge "
            "model; defining it as an ordinary table would drop its relation constraint and refuse every edge. Derive "
            "TdlNote from BaseSurrealEdge, or give it another table_name. Nothing was defined."
        )
        assert schema.table_refused("TG4", "TdlNote", "tdl_note", field="tags.*") == (
            "TdlNote.define_table(): the definition of the field 'tags.*' on the table 'tdl_note' could not be read, so "
            "the ORM cannot keep the clauses TdlNote does not write (ASSERT, PERMISSIONS, READONLY, DEFAULT, VALUE, "
            "COMPUTED, COMMENT). Nothing was defined."
        )

    def test_tg4_names_a_field(self) -> None:
        with pytest.raises(ValueError, match="field="):
            schema.table_refused("TG4", "TdlNote", "tdl_note")

    @pytest.mark.parametrize("guard", ["RG1", "RG2", "RG3"])
    def test_rg1_to_rg3(self, guard: Any) -> None:
        assert schema.relation_table_refused(guard, "TdlLikes", "tdl_likes") == _RG_DEFINE_RELATION[guard]
        assert (
            schema.relation_table_refused(guard, "TdlLikes", "tdl_likes", method="define_relation")
            == _RG_DEFINE_RELATION[guard]
        )
        assert schema.relation_table_refused(guard, "TdlLikes", "tdl_likes", method="define_table") == _RG_DEFINE_TABLE[guard]

    def test_rg4(self) -> None:
        assert schema.relation_schemafull_refused("TdlLikes", ["stars"]) == (
            "TdlLikes.define_relation(): TdlLikes declares SchemaMode.SCHEMAFULL and has data fields ('stars'); "
            "define_relation() defines no field, and a SCHEMAFULL table refuses (SurrealDB 3.x) or drops (2.x and the "
            "embedded engine) the fields it does not define. Call TdlLikes.define_table(), which applies the same "
            "relation statement and defines them. Nothing was defined."
        )
        assert "data fields ('stars', 'note');" in schema.relation_schemafull_refused("TdlLikes", ("stars", "note"))


# --- the carried fields ---------------------------------------------------------------------------------------------


def _column(model: type[BaseSurrealModel], *path: str) -> model_ddl.Column:
    """The column *path* of *model*'s table DDL."""
    return next(column for column in model._table_columns() if column.path == path)


def _kept_field(
    model: type[BaseSurrealModel], stored: str | FieldState, *, schemafull: bool = False, before_3: bool = False
) -> str | None:
    """``field_statement_keeping()`` of *model*'s column over the stored field *stored* (the column of its path)."""
    state = parse_define_field(stored).state if isinstance(stored, str) else stored
    column = _column(model, *state.path)
    return model_ddl.field_statement_keeping(
        model.get_table_name(), column, schemafull=schemafull, before_3=before_3, stored=state
    )


_K3 = (
    "DEFINE FIELD OVERWRITE `email` ON `tdl_note` TYPE option<string> ASSERT $value != '' PERMISSIONS FOR select WHERE "
    "$auth.id = id, FOR create, update FULL COMMENT 'mail';"
)
_K4 = "DEFINE FIELD OVERWRITE `created` ON `tdl_note` TYPE option<datetime> DEFAULT time::now() READONLY;"
_K5 = "DEFINE FIELD OVERWRITE `done` ON `tdl_note` TYPE bool DEFAULT false COMMENT 'd';"
_K6 = "DEFINE FIELD OVERWRITE `total` ON `tdl_cart` TYPE option<int> VALUE count(items) COMMENT 'c';"
_K7 = "DEFINE FIELD OVERWRITE `meta` ON `tdl_note` TYPE object;"
_K9 = (
    "DEFINE FIELD OVERWRITE `total` ON `tdl_cart` TYPE option<int> COMPUTED count(items) PERMISSIONS FOR select WHERE "
    "true, FOR create, update FULL COMMENT 'c';"
)
_K10 = "DEFINE FIELD OVERWRITE `age` ON `tdl_note` TYPE int COMPUTED 18;"
_K11 = "DEFINE FIELD OVERWRITE `status` ON `tdl_note` TYPE string DEFAULT 'new';"

#: (case, model, the stored field as a line renders it in ``INFO FOR TABLE``, the table's mode is ``SCHEMAFULL``, the
#: carried statement). Each server renders a field's ``COMMENT`` before its ``PERMISSIONS``, whatever order the
#: definition was written in; 3.x writes ``none | T`` and ``TYPE object FLEXIBLE``, 2.x ``option<T>`` and ``FLEXIBLE
#: TYPE object``; ``COMPUTED`` is 3.x's.
_CARRIED_FIELDS: list[tuple[str, type[BaseSurrealModel], str, bool, str]] = [
    (
        "K3",
        TdlNoteTuned,
        "DEFINE FIELD email ON tdl_note TYPE string ASSERT $value != '' COMMENT 'mail' PERMISSIONS FOR select WHERE "
        "$auth.id = id, FOR create, update FULL",
        True,
        _K3,
    ),
    (
        "K4-3x",
        TdlNoteTuned,
        "DEFINE FIELD created ON tdl_note TYPE none | datetime DEFAULT time::now() READONLY PERMISSIONS FULL",
        True,
        _K4,
    ),
    (
        "K4-2x",
        TdlNoteTuned,
        "DEFINE FIELD created ON tdl_note TYPE option<datetime> DEFAULT time::now() READONLY PERMISSIONS FULL",
        True,
        _K4,
    ),
    ("K5", TdlNoteTuned, "DEFINE FIELD done ON tdl_note TYPE bool DEFAULT true COMMENT 'd' PERMISSIONS FULL", True, _K5),
    ("K6", TdlCart, "DEFINE FIELD total ON tdl_cart VALUE 1 COMMENT 'c' PERMISSIONS FULL", False, _K6),
    ("K7-2x", TdlNoteShapes, "DEFINE FIELD meta ON tdl_note FLEXIBLE TYPE object PERMISSIONS FULL", False, _K7),
    ("K7-3x", TdlNoteShapes, "DEFINE FIELD meta ON tdl_note TYPE object FLEXIBLE PERMISSIONS FULL", False, _K7),
    (
        "K9",
        TdlCart,
        "DEFINE FIELD total ON tdl_cart TYPE none | int COMPUTED count(items) COMMENT 'c' PERMISSIONS FOR select WHERE "
        "true, FOR create, update FULL",
        False,
        _K9,
    ),
    ("K10", TdlNoteShapes, "DEFINE FIELD age ON tdl_note TYPE int COMPUTED 18 PERMISSIONS FULL", False, _K10),
    ("K11", TdlNoteStatus, "DEFINE FIELD status ON tdl_note TYPE string DEFAULT 'new' PERMISSIONS FULL", False, _K11),
]


class TestCarriedFields:
    """Over a field that exists, ``field_statement_keeping()`` writes the column's ``TYPE``, ``FLEXIBLE``, ``DEFAULT``
    and ``VALUE`` and keeps the stored ``REFERENCE``, ``DEFAULT``/``VALUE`` the model does not write, ``READONLY``,
    ``ASSERT``, ``PERMISSIONS`` and ``COMMENT``: a stored ``COMPUTED`` stays ``COMPUTED``, without ``DEFAULT``; a
    ``server_fields`` column keeps the database's ``DEFAULT``, ``VALUE`` or ``COMPUTED``. ``None`` when a carried value
    fails its check."""

    @pytest.mark.parametrize(
        ("model", "stored", "schemafull", "statement"),
        [case[1:] for case in _CARRIED_FIELDS],
        ids=[case[0] for case in _CARRIED_FIELDS],
    )
    def test_each_carried_field(self, model: type[BaseSurrealModel], stored: str, schemafull: bool, statement: str) -> None:
        assert _kept_field(model, stored, schemafull=schemafull) == statement

    def test_k3_is_read_clause_by_clause(self) -> None:
        """The stored ``ASSERT``, permissions and comment are three clauses, each carried in its place."""
        state = parse_define_field(_CARRIED_FIELDS[0][2]).state
        assert state.assertion == "$value != ''"
        assert state.comment == "mail"
        assert dict(state.permissions) == {"select": "WHERE $auth.id = id", "create": "FULL", "update": "FULL"}

    def test_k7_on_a_schemafull_table(self) -> None:
        """A free-form object is ``FLEXIBLE`` on a ``SCHEMAFULL`` table, whatever was stored."""
        for stored in ("DEFINE FIELD meta ON tdl_note TYPE object PERMISSIONS FULL", _CARRIED_FIELDS[6][2]):
            assert _kept_field(TdlNoteShapes, stored, schemafull=True) == (
                "DEFINE FIELD OVERWRITE `meta` ON `tdl_note` TYPE object FLEXIBLE;"
            )

    def test_a_stored_flexible_is_kept_on_an_object_kind(self) -> None:
        """A sub-model's object is not free-form: a stored ``FLEXIBLE`` is kept on a ``SCHEMAFULL`` table only."""
        flexible = "DEFINE FIELD address ON tdl_note TYPE none | object FLEXIBLE PERMISSIONS FULL"
        plain = "DEFINE FIELD address ON tdl_note TYPE none | object PERMISSIONS FULL"
        statement = "DEFINE FIELD OVERWRITE `address` ON `tdl_note` TYPE option<object>"
        assert _kept_field(TdlNoteShapes, flexible, schemafull=True) == f"{statement} FLEXIBLE;"
        assert _kept_field(TdlNoteShapes, plain, schemafull=True) == f"{statement};"
        assert _kept_field(TdlNoteShapes, flexible, schemafull=False) == f"{statement};"

    def test_a_stored_flexible_is_dropped_on_a_kind_without_object(self) -> None:
        stored = "DEFINE FIELD tags ON tdl_note TYPE object FLEXIBLE PERMISSIONS FULL"
        assert _kept_field(TdlNoteShapes, stored, schemafull=True) == (
            "DEFINE FIELD OVERWRITE `tags` ON `tdl_note` TYPE array<string>;"
        )

    def test_flexible_on_any_before_3(self) -> None:
        stored = "DEFINE FIELD anything ON tdl_note TYPE any PERMISSIONS FULL"
        assert _kept_field(TdlNoteShapes, stored, schemafull=True, before_3=True) == (
            "DEFINE FIELD OVERWRITE `anything` ON `tdl_note` TYPE any FLEXIBLE;"
        )
        assert _kept_field(TdlNoteShapes, stored, schemafull=True) == (
            "DEFINE FIELD OVERWRITE `anything` ON `tdl_note` TYPE any;"
        )
        assert _kept_field(TdlNoteShapes, stored, schemafull=False, before_3=True) == (
            "DEFINE FIELD OVERWRITE `anything` ON `tdl_note` TYPE any;"
        )

    def test_nested_and_item_paths(self) -> None:
        """A nested column and an item definition, from either line's rendering of their path."""
        assert _kept_field(TdlPerson, "DEFINE FIELD address.city ON tdl_person TYPE string COMMENT 'c' PERMISSIONS FULL") == (
            "DEFINE FIELD OVERWRITE `address`.`city` ON `tdl_person` TYPE string COMMENT 'c';"
        )
        for path in ("bag[*]", "bag.*"):
            stored = f"DEFINE FIELD {path} ON tdl_kinds TYPE any PERMISSIONS FULL"
            assert _kept_field(TdlKinds, stored, schemafull=True) == "DEFINE FIELD OVERWRITE `bag`.* ON `tdl_kinds` TYPE any;"
            assert _kept_field(TdlKinds, stored, schemafull=True, before_3=True) == (
                "DEFINE FIELD OVERWRITE `bag`.* ON `tdl_kinds` TYPE any FLEXIBLE;"
            )

    # --- the server_fields rule ----------------------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("stored", "statement"),
        [
            ("DEFINE FIELD status ON tdl_note TYPE string DEFAULT 'new' PERMISSIONS FULL", _K11),
            (
                "DEFINE FIELD status ON tdl_note TYPE string DEFAULT ALWAYS 'new' PERMISSIONS FULL",
                "DEFINE FIELD OVERWRITE `status` ON `tdl_note` TYPE string DEFAULT ALWAYS 'new';",
            ),
            (
                "DEFINE FIELD status ON tdl_note TYPE string VALUE 'new' PERMISSIONS FULL",
                "DEFINE FIELD OVERWRITE `status` ON `tdl_note` TYPE string VALUE 'new';",
            ),
            (
                "DEFINE FIELD status ON tdl_note TYPE string COMPUTED 'new' PERMISSIONS FULL",
                "DEFINE FIELD OVERWRITE `status` ON `tdl_note` TYPE string COMPUTED 'new';",
            ),
            (
                "DEFINE FIELD status ON tdl_note TYPE string PERMISSIONS FULL",
                "DEFINE FIELD OVERWRITE `status` ON `tdl_note` TYPE string DEFAULT 'draft';",
            ),
            (
                "DEFINE FIELD status ON tdl_note TYPE string READONLY COMMENT 's' PERMISSIONS FULL",
                "DEFINE FIELD OVERWRITE `status` ON `tdl_note` TYPE string DEFAULT 'draft' READONLY COMMENT 's';",
            ),
        ],
        ids=["default", "default-always", "value", "computed", "no-rule", "no-rule-other-clauses"],
    )
    def test_a_server_column_keeps_the_database_rule(self, stored: str, statement: str) -> None:
        """The model's ``DEFAULT`` only where the database has no ``DEFAULT``, ``VALUE`` or ``COMPUTED``."""
        assert _kept_field(TdlNoteStatus, stored) == statement

    def test_a_server_column_without_constant_default(self) -> None:
        """No ``DEFAULT`` of the model's to write: the stored one is kept, and none is added where there is none."""
        stored = "DEFINE FIELD stamp ON tdl_note TYPE datetime DEFAULT time::now() PERMISSIONS FULL"
        assert _kept_field(TdlNoteStamped, stored) == (
            "DEFINE FIELD OVERWRITE `stamp` ON `tdl_note` TYPE datetime DEFAULT time::now();"
        )
        assert _kept_field(TdlNoteStamped, "DEFINE FIELD owner_ref ON tdl_note TYPE string PERMISSIONS FULL") == (
            "DEFINE FIELD OVERWRITE `owner_ref` ON `tdl_note` TYPE string;"
        )

    def test_an_ordinary_column_writes_its_default(self) -> None:
        """The model's ``DEFAULT`` wins over a stored ``DEFAULT ALWAYS``, written without ``ALWAYS``."""
        stored = "DEFINE FIELD done ON tdl_note TYPE bool DEFAULT ALWAYS true PERMISSIONS FULL"
        assert _kept_field(TdlNoteTuned, stored) == "DEFINE FIELD OVERWRITE `done` ON `tdl_note` TYPE bool DEFAULT false;"

    # --- the COMPUTED rule ---------------------------------------------------------------------------------------

    def test_a_stored_value_is_kept_when_the_model_writes_none(self) -> None:
        stored = "DEFINE FIELD body ON tdl_note TYPE string VALUE string::trim($value) PERMISSIONS FULL"
        assert _kept_field(TdlNoteTuned, stored) == (
            "DEFINE FIELD OVERWRITE `body` ON `tdl_note` TYPE string VALUE string::trim($value);"
        )

    def test_a_computed_field_writes_computed_over_a_stored_computed(self) -> None:
        """The model's expression, never ``VALUE``; no stored ``DEFAULT`` or ``VALUE`` next to ``COMPUTED``."""
        stored = FieldState(name="total", field_type="int", computed="1", default="0", value="2", readonly=True)
        assert (
            _kept_field(TdlCart, stored)
            == "DEFINE FIELD OVERWRITE `total` ON `tdl_cart` TYPE option<int> COMPUTED count(items);"
        )

    def test_a_stored_computed_keeps_flexible_permissions_and_comment(self) -> None:
        stored = "DEFINE FIELD meta ON tdl_note TYPE object COMPUTED {} COMMENT 'm' PERMISSIONS FOR select NONE, FOR create, update FULL"
        assert _kept_field(TdlNoteShapes, stored, schemafull=True) == (
            "DEFINE FIELD OVERWRITE `meta` ON `tdl_note` TYPE object FLEXIBLE COMPUTED {} PERMISSIONS FOR select NONE, FOR "
            "create, update FULL COMMENT 'm';"
        )

    # --- order and checks ----------------------------------------------------------------------------------------

    def test_clause_order(self) -> None:
        """``TYPE``, ``FLEXIBLE``, ``REFERENCE``, ``DEFAULT``, ``READONLY``, ``VALUE``, ``ASSERT``, ``PERMISSIONS``,
        ``COMMENT``."""
        column = model_ddl.Column(path=("owner",), field="owner", kind=parse_kind("option<record<tdl_person>>"))
        stored = FieldState(
            name="owner",
            field_type="record<tdl_person>",
            default="NONE",
            default_always=True,
            assertion="$value != NONE",
            readonly=True,
            value="$value",
            reference=True,
            on_delete="CASCADE",
            comment="o",
            permissions={"select": "FULL", "create": "NONE", "update": "NONE"},
        )
        assert model_ddl.field_statement_keeping("tdl_note", column, schemafull=True, stored=stored) == (
            "DEFINE FIELD OVERWRITE `owner` ON `tdl_note` TYPE option<record<tdl_person>> REFERENCE ON DELETE CASCADE "
            "DEFAULT ALWAYS NONE READONLY VALUE $value ASSERT $value != NONE PERMISSIONS FOR select FULL, FOR create, "
            "update NONE COMMENT 'o';"
        )

    def test_a_reference_is_kept(self) -> None:
        column = model_ddl.Column(path=("owner",), field="owner", kind=parse_kind("option<record<tdl_person>>"))
        for on_delete, clause in [(None, "REFERENCE"), ("THEN NONE", "REFERENCE ON DELETE THEN NONE")]:
            stored = FieldState(name="owner", field_type="record<tdl_person>", reference=True, on_delete=on_delete)
            assert model_ddl.field_statement_keeping("tdl_note", column, schemafull=False, stored=stored) == (
                f"DEFINE FIELD OVERWRITE `owner` ON `tdl_note` TYPE option<record<tdl_person>> {clause};"
            )

    @pytest.mark.parametrize(
        "stored",
        [
            FieldState(name="email", field_type="string", assertion="$value != '' -- n"),
            FieldState(name="email", field_type="string", default="'a'; 'b'"),
            FieldState(name="email", field_type="string", computed="count(items) -- n"),
            FieldState(name="email", field_type="string", value="string::trim($value"),
            FieldState(name="email", field_type="string", permissions={"select": "WHERE a; b"}),
            FieldState(name="email", field_type="string", permissions={"update": "SOMETIMES"}),
            FieldState(name="email", field_type="string", reference=True, on_delete="THEN 'a"),
            FieldState(name="email", field_type="string", comment="a\x00b"),
        ],
        ids=[
            "assert-comment-marker",
            "default-top-level-semicolon",
            "computed-comment-marker",
            "value-unbalanced-bracket",
            "rule-top-level-semicolon",
            "not-a-rule",
            "on-delete-unterminated-literal",
            "nul-in-comment",
        ],
    )
    def test_none_for_a_carried_value_that_fails_its_check(self, stored: FieldState) -> None:
        assert _kept_field(TdlNoteTuned, stored) is None

    def test_a_value_the_model_replaces_is_not_checked(self) -> None:
        """The model's ``DEFAULT`` or computed expression replaces a faulty stored one, which is not carried."""
        assert _kept_field(TdlNoteTuned, FieldState(name="done", field_type="bool", default="'a'; 'b'")) == (
            "DEFINE FIELD OVERWRITE `done` ON `tdl_note` TYPE bool DEFAULT false;"
        )
        assert _kept_field(TdlCart, FieldState(name="total", field_type="int", computed="count(items) -- n")) == (
            "DEFINE FIELD OVERWRITE `total` ON `tdl_cart` TYPE option<int> COMPUTED count(items);"
        )
        assert _kept_field(TdlCart, FieldState(name="total", field_type="int", value="(1")) == (
            "DEFINE FIELD OVERWRITE `total` ON `tdl_cart` TYPE option<int> VALUE count(items);"
        )

    def test_the_table_name_is_checked(self) -> None:
        stored = FieldState(name="email", field_type="string")
        with pytest.raises(ValueError):
            model_ddl.field_statement_keeping("a-b", _column(TdlNoteTuned, "email"), schemafull=False, stored=stored)


# --- the field read ----------------------------------------------------------------------------------------------------

#: The ``fields`` of ``INFO FOR TABLE`` for one table as SurrealDB 3.3.4 and 2.7.0 render them: 3.x keys a keyword
#: field bare and writes array items ``.*``, 2.x quotes the keyword and writes ``[*]``; both add the item field of a
#: typed array of objects (``subs.*``).
_FIELDS_3X = {
    "`a.b`": "DEFINE FIELD `a.b` ON tdl_kinds TYPE int DEFAULT 0 PERMISSIONS FULL",
    "`first-name`": "DEFINE FIELD `first-name` ON tdl_kinds TYPE string PERMISSIONS FULL",
    "address": "DEFINE FIELD address ON tdl_kinds TYPE none | object PERMISSIONS FULL",
    "address.city": "DEFINE FIELD address.city ON tdl_kinds TYPE string PERMISSIONS FULL",
    "bag": "DEFINE FIELD bag ON tdl_kinds TYPE array PERMISSIONS FULL",
    "bag.*": "DEFINE FIELD bag.* ON tdl_kinds TYPE any PERMISSIONS FULL",
    "select": "DEFINE FIELD `select` ON tdl_kinds TYPE int DEFAULT 0 PERMISSIONS FULL",
    "subs": "DEFINE FIELD subs ON tdl_kinds TYPE array<object> PERMISSIONS FULL",
    "subs.*": "DEFINE FIELD subs.* ON tdl_kinds TYPE object PERMISSIONS FULL",
    "subs.*.city": "DEFINE FIELD subs.*.city ON tdl_kinds TYPE string PERMISSIONS FULL",
}
_FIELDS_2X = {
    "`a.b`": "DEFINE FIELD `a.b` ON tdl_kinds TYPE int DEFAULT 0 PERMISSIONS FULL",
    "`first-name`": "DEFINE FIELD `first-name` ON tdl_kinds TYPE string PERMISSIONS FULL",
    "`select`": "DEFINE FIELD `select` ON tdl_kinds TYPE int DEFAULT 0 PERMISSIONS FULL",
    "address": "DEFINE FIELD address ON tdl_kinds TYPE option<object> PERMISSIONS FULL",
    "address.city": "DEFINE FIELD address.city ON tdl_kinds TYPE string PERMISSIONS FULL",
    "bag": "DEFINE FIELD bag ON tdl_kinds TYPE array PERMISSIONS FULL",
    "bag[*]": "DEFINE FIELD bag[*] ON tdl_kinds TYPE any PERMISSIONS FULL",
    "subs": "DEFINE FIELD subs ON tdl_kinds TYPE array<object> PERMISSIONS FULL",
    "subs[*]": "DEFINE FIELD subs[*] ON tdl_kinds TYPE object PERMISSIONS FULL",
    "subs[*].city": "DEFINE FIELD subs[*].city ON tdl_kinds TYPE string PERMISSIONS FULL",
}
_FIELD_PATHS = {
    ("a.b",),
    ("first-name",),
    ("address",),
    ("address", "city"),
    ("bag",),
    ("bag", "*"),
    ("select",),
    ("subs",),
    ("subs", "*"),
    ("subs", "*", "city"),
}

#: A record user's refusal, as the servers word it.
_NOT_ENOUGH_PERMISSIONS = "IAM error: Not enough permissions to perform this action"


class _InfoClient:
    """A client answering every statement with *answer*, or raising *error*; it records each statement."""

    def __init__(self, answer: object = None, *, error: BaseException | None = None) -> None:
        self.answer = answer
        self.error = error
        self.queries: list[str] = []

    async def query(self, statement: str, variables: object) -> object:
        assert variables == {}
        self.queries.append(statement)
        if self.error is not None:
            raise self.error
        return self.answer


def _table_answer(fields: object) -> dict[str, object]:
    """``INFO FOR TABLE``'s answer holding *fields*."""
    return {"events": {}, "fields": fields, "indexes": {}, "lives": {}, "tables": {}}


class TestReadTableFields:
    """``read_table_fields()`` sends ``INFO FOR TABLE`` once and keys each field's ``FieldState`` by its path, on both
    lines' renderings; a definition the parser cannot read is ``None`` under the path of its key; a refusal is the
    table definition's S1/S2."""

    @pytest.mark.parametrize("fields", [_FIELDS_3X, _FIELDS_2X], ids=["3x", "2x"])
    async def test_keys_are_paths(self, fields: dict[str, str]) -> None:
        client = _InfoClient(_table_answer(fields))
        read = await schema.read_table_fields(client, "tdl_kinds", kind="definition")
        assert set(read) == _FIELD_PATHS
        for definition in fields.values():
            state = parse_define_field(definition).state
            assert read[tuple(state.path)] == state
        assert client.queries == ["INFO FOR TABLE `tdl_kinds`;"]

    async def test_both_renderings_read_the_same(self) -> None:
        three = await schema.read_table_fields(_InfoClient(_table_answer(_FIELDS_3X)), "tdl_kinds", kind="definition")
        two = await schema.read_table_fields(_InfoClient(_table_answer(_FIELDS_2X)), "tdl_kinds", kind="definition")
        assert {path: (state.field_type, state.default) for path, state in three.items() if state is not None} == {
            path: (state.field_type, state.default) for path, state in two.items() if state is not None
        }

    async def test_an_unreadable_definition_is_none_under_its_path(self) -> None:
        """Keyed by its ``INFO`` key read as a path; a key that is not one is left out."""
        done = "DEFINE FIELD done ON tdl_note TYPE bool DEFAULT false PERMISSIONS FULL"
        fields: dict[str, object] = {
            "done": done,
            "email": "DEFINE FIELD email ON tdl_note TYPE string ASSERT (a",
            "subs[*].city": 3,
            "`first-name`": "DEFINE FIELD",
            "a..b": "DEFINE FIELD",
            "x y": "DEFINE FIELD",
            "": "DEFINE FIELD",
        }
        read = await schema.read_table_fields(_InfoClient(_table_answer(fields)), "tdl_note", kind="definition")
        assert read == {
            ("done",): parse_define_field(done).state,
            ("email",): None,
            ("subs", "*", "city"): None,
            ("first-name",): None,
        }

    @pytest.mark.parametrize("answer", [_table_answer({}), _table_answer(None), {"events": {}}, [], None])
    async def test_no_fields(self, answer: object) -> None:
        assert await schema.read_table_fields(_InfoClient(answer), "tdl_note", kind="definition") == {}

    @pytest.mark.parametrize(
        ("error", "refusal"),
        [
            (InternalError("Internal", _NOT_ENOUGH_PERMISSIONS), SurrealDbAuthenticationError),
            (NotAllowedError("NotAllowed", _NOT_ENOUGH_PERMISSIONS), SurrealDbAuthenticationError),
            (InternalError("Internal", "There was a problem with the database"), SurrealDbError),
            (RuntimeError("the engine is closed"), SurrealDbError),
        ],
        ids=["s1-internal", "s1-not-allowed", "s2-internal", "s2-runtime"],
    )
    async def test_s1_s2_a_refused_info(self, error: BaseException, refusal: type[SurrealDbError]) -> None:
        client = _InfoClient(error=error)
        with pytest.raises(SurrealDbError) as raised:
            await schema.read_table_fields(client, "tdl_note", kind="definition")
        assert type(raised.value) is refusal
        assert str(raised.value) == f"Can't apply table definition: 'tdl_note' -> {error}"
        assert raised.value.__cause__ is error
        assert client.queries == ["INFO FOR TABLE `tdl_note`;"]

    @pytest.mark.parametrize(("table", "error"), [("a-b", ValueError), (3, TypeError)], ids=repr)
    async def test_the_table_name_is_checked(self, table: Any, error: type[Exception]) -> None:
        client = _InfoClient(_table_answer({}))
        with pytest.raises(error):
            await schema.read_table_fields(client, table, kind="definition")
        assert client.queries == []


# --- define_table(): the reads, the guards, the statements applied (fake clients) -----------------------------------


class TdlNoteMeta(BaseSurrealModel):
    """A model without a schema mode and with a free-form object: ``FLEXIBLE`` follows the table's mode."""

    model_config = SurrealConfigDict(table_name="tdl_note", connection="tdl")

    id: str | None = None
    body: str
    meta: dict[str, Any] = Field(default_factory=dict)


class TdlNoteMetaLoose(BaseSurrealModel):
    """The same columns, declared ``SCHEMALESS``: no ``FLEXIBLE``, whatever was stored."""

    model_config = SurrealConfigDict(table_name="tdl_note", schema_mode=SchemaMode.SCHEMALESS, connection="tdl")

    id: str | None = None
    body: str
    meta: dict[str, Any] = Field(default_factory=dict)


class TdlNoteAge(BaseSurrealModel):
    """An ordinary column with a constant default, over a stored ``COMPUTED`` field (K10)."""

    model_config = SurrealConfigDict(table_name="tdl_note", connection="tdl")

    id: str | None = None
    age: int = 18


class TdlLooseSub(BaseModel):
    """A sub-model holding ``Any``."""

    meta: Any = None


class TdlLoose(BaseSurrealModel):
    """A ``SCHEMAFULL`` model whose columns hold ``any``: ``Any``, untyped lists (required, ``list[Any]``, optional) and
    a sub-model's ``Any``."""

    model_config = SurrealConfigDict(table_name="tdl_loose", schema_mode=SchemaMode.SCHEMAFULL, connection="tdl")

    id: str | None = None
    anything: Any = None
    items: list = Field(default_factory=list)  # type: ignore[type-arg]
    bag: list[Any] = Field(default_factory=list)
    maybe: list | None = None  # type: ignore[type-arg]
    sub: TdlLooseSub = Field(default_factory=TdlLooseSub)


#: ``TdlLoose.table_ddl()`` — what ``define_table()`` applies on a new table on SurrealDB 3.x: an item definition for
#: each untyped list, no ``FLEXIBLE`` (3.x refuses it on a kind without an object).
_LOOSE = [
    "DEFINE TABLE OVERWRITE `tdl_loose` TYPE NORMAL SCHEMAFULL;",
    "DEFINE FIELD OVERWRITE `anything` ON `tdl_loose` TYPE any;",
    "DEFINE FIELD OVERWRITE `items` ON `tdl_loose` TYPE array;",
    "DEFINE FIELD OVERWRITE `items`.* ON `tdl_loose` TYPE any;",
    "DEFINE FIELD OVERWRITE `bag` ON `tdl_loose` TYPE array;",
    "DEFINE FIELD OVERWRITE `bag`.* ON `tdl_loose` TYPE any;",
    "DEFINE FIELD OVERWRITE `maybe` ON `tdl_loose` TYPE option<array>;",
    "DEFINE FIELD OVERWRITE `maybe`.* ON `tdl_loose` TYPE any;",
    "DEFINE FIELD OVERWRITE `sub` ON `tdl_loose` TYPE object;",
    "DEFINE FIELD OVERWRITE `sub`.`meta` ON `tdl_loose` TYPE any;",
]

#: What ``define_table()`` applies on a new table before SurrealDB 3.x (2.x servers, the embedded engine): the same,
#: with ``FLEXIBLE`` on every column holding ``any`` — without it those engines empty an object or an array there.
_LOOSE_BEFORE_3 = [
    "DEFINE TABLE OVERWRITE `tdl_loose` TYPE NORMAL SCHEMAFULL;",
    "DEFINE FIELD OVERWRITE `anything` ON `tdl_loose` TYPE any FLEXIBLE;",
    "DEFINE FIELD OVERWRITE `items` ON `tdl_loose` TYPE array FLEXIBLE;",
    "DEFINE FIELD OVERWRITE `items`.* ON `tdl_loose` TYPE any FLEXIBLE;",
    "DEFINE FIELD OVERWRITE `bag` ON `tdl_loose` TYPE array FLEXIBLE;",
    "DEFINE FIELD OVERWRITE `bag`.* ON `tdl_loose` TYPE any FLEXIBLE;",
    "DEFINE FIELD OVERWRITE `maybe` ON `tdl_loose` TYPE option<array> FLEXIBLE;",
    "DEFINE FIELD OVERWRITE `maybe`.* ON `tdl_loose` TYPE any FLEXIBLE;",
    "DEFINE FIELD OVERWRITE `sub` ON `tdl_loose` TYPE object;",
    "DEFINE FIELD OVERWRITE `sub`.`meta` ON `tdl_loose` TYPE any FLEXIBLE;",
]


_INFO = "INFO FOR DB;"
_INFO_NOTE = "INFO FOR TABLE `tdl_note`;"
_INFO_PERSON = "INFO FOR TABLE `tdl_person`;"

#: The fields of the table tuned by hand, as SurrealDB 3.x renders them in ``INFO FOR TABLE``.
_TUNED_FIELDS = {
    "body": "DEFINE FIELD body ON tdl_note TYPE string PERMISSIONS FULL",
    "created": "DEFINE FIELD created ON tdl_note TYPE none | datetime DEFAULT time::now() READONLY PERMISSIONS FULL",
    "done": "DEFINE FIELD done ON tdl_note TYPE bool DEFAULT true COMMENT 'd' PERMISSIONS FULL",
    "email": (
        "DEFINE FIELD email ON tdl_note TYPE string ASSERT $value != '' COMMENT 'mail' PERMISSIONS FOR select WHERE "
        "$auth.id = id, FOR create, update FULL"
    ),
}
_BODY = "DEFINE FIELD OVERWRITE `body` ON `tdl_note` TYPE string;"
#: What ``TdlNoteTuned.define_table()`` sends over the table tuned by hand, in the model's column order.
_TUNED = [_K1E, _BODY, _K5, _K3, _K4]

#: A stored view, a relation table, and a table the parser cannot read, on ``tdl_note``.
_STORED_NOTE_VIEW = "DEFINE TABLE tdl_note TYPE ANY SCHEMALESS AS SELECT count() AS n FROM tdl_src GROUP ALL PERMISSIONS NONE"
_STORED_NOTE_RELATION = "DEFINE TABLE tdl_note TYPE RELATION IN tdl_person OUT tdl_person SCHEMALESS PERMISSIONS NONE"
_STORED_NOTE_BAD_RULE = (
    "DEFINE TABLE tdl_note TYPE ANY SCHEMALESS PERMISSIONS FOR select WHERE a; b, FOR create, update, delete NONE"
)
_STORED_NOTE_FULL = "DEFINE TABLE tdl_note TYPE NORMAL SCHEMAFULL PERMISSIONS NONE"
_STORED_NOTE_LESS = "DEFINE TABLE tdl_note TYPE ANY SCHEMALESS PERMISSIONS NONE"
_STORED_LOG_FULL = "DEFINE TABLE tdl_log TYPE ANY SCHEMAFULL PERMISSIONS NONE"


class _TableClient:
    """A client answering ``INFO FOR DB;`` with ``{"tables": tables}``, ``INFO FOR TABLE …;`` with ``{"fields":
    fields}`` and every other statement with ``[]``. It records each statement, raises *info_error* for the first
    read, *fields_error* for the second and *ddl_error* for the statements starting with *refuse* (every one when
    *refuse* is ``None``), answers ``version()`` with *version*, counts those calls and records how many statements
    preceded the last one (``version_at``)."""

    def __init__(
        self,
        tables: Mapping[str, object] | None = None,
        fields: Mapping[str, object] | None = None,
        *,
        info_error: BaseException | None = None,
        fields_error: BaseException | None = None,
        ddl_error: BaseException | None = None,
        refuse: str | None = None,
        version: str = "surrealdb-3.3.4",
    ) -> None:
        self.tables = dict(tables or {})
        self.fields = dict(fields or {})
        self.info_error = info_error
        self.fields_error = fields_error
        self.ddl_error = ddl_error
        self.refuse = refuse
        self.version_text = version
        self.queries: list[str] = []
        self.version_calls = 0
        self.version_at: int | None = None

    async def query(self, statement: str, variables: object) -> object:
        assert variables == {}
        self.queries.append(statement)
        if statement == _INFO:
            if self.info_error is not None:
                raise self.info_error
            return {"tables": dict(self.tables), "functions": {}}
        if statement.startswith("INFO FOR TABLE "):
            if self.fields_error is not None:
                raise self.fields_error
            return _table_answer(dict(self.fields))
        if self.ddl_error is not None and (self.refuse is None or statement.startswith(self.refuse)):
            raise self.ddl_error
        return []

    async def version(self) -> str:
        self.version_calls += 1
        self.version_at = len(self.queries)
        return self.version_text


class _QueuingTx:
    """A transaction of the connection *name* whose ``add()`` records each statement."""

    def __init__(self, name: str = "tdl") -> None:
        self._connection_name = name
        self.added: list[tuple[str, object]] = []

    async def add(self, statement: str, variables: object) -> None:
        self.added.append((statement, variables))


@pytest.fixture
def use_client(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], None]:
    """Register the connection ``"tdl"`` (never opened) and make ``get_client()`` answer the client given."""

    def use(client: Any) -> None:
        SurrealDBConnectionManager.add_connection("tdl", url="mem://", namespace="ns", database="tdl_db")

        async def get_client(*args: Any, **kwargs: Any) -> Any:
            return client

        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", get_client)

    return use


class TestDefineTable:
    """``define_table()`` on fake clients: the reads it sends, the statements it applies one per call and returns
    joined with a newline, ``FLEXIBLE`` following the table's mode, a transaction getting the DDL only, and the
    refusals before any request or naming the table or the field, never the statement."""

    def test_signature(self) -> None:
        """A class method and a coroutine, wrapped as an invalidating write of the query cache; ``BaseSurrealEdge``
        does not redefine it."""
        function = inspect.getattr_static(BaseSurrealModel, "define_table")
        assert isinstance(function, classmethod)
        assert inspect.iscoroutinefunction(function.__func__)
        assert function.__func__.__surreal_cache_writes__ is True
        assert inspect.signature(BaseSurrealModel.define_table) == inspect.Signature(
            [
                inspect.Parameter("overwrite", inspect.Parameter.KEYWORD_ONLY, default=True, annotation=bool),
                inspect.Parameter("tx", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Transaction | None),
            ],
            return_annotation=str,
        )
        assert "define_table" not in vars(BaseSurrealEdge)

    async def test_a_missing_table(self, use_client: Callable[[Any], None]) -> None:
        """``INFO FOR DB`` once, then ``table_ddl()``'s statements one per call; their text joined with a newline."""
        client = _TableClient()
        use_client(client)
        result = await TdlPerson.define_table()
        assert isinstance(result, str)
        assert result == "\n".join(_G1) == "\n".join(TdlPerson.table_ddl())
        assert client.queries == [_INFO, *_G1]
        assert client.version_calls == 0

    async def test_overwrite_false_on_a_missing_table(self, use_client: Callable[[Any], None]) -> None:
        client = _TableClient()
        use_client(client)
        assert await TdlPerson.define_table(overwrite=False) == "\n".join(_if_not_exists(_G1))
        assert client.queries == [_INFO, *_if_not_exists(_G1)]

    async def test_an_existing_table(self, use_client: Callable[[Any], None]) -> None:
        """``INFO FOR TABLE`` once, then the carried statements: K1E, then each column over its stored field."""
        client = _TableClient({"tdl_note": _STORED_NOTE}, _TUNED_FIELDS)
        use_client(client)
        assert await TdlNoteTuned.define_table() == "\n".join(_TUNED)
        assert client.queries == [_INFO, _INFO_NOTE, *_TUNED]
        assert client.version_calls == 0

    async def test_the_declared_clauses_win(self, use_client: Callable[[Any], None]) -> None:
        """K2E: the declared mode, comment and permissions over the stored ones, the change feed kept."""
        client = _TableClient({"tdl_note": _STORED_NOTE}, _TUNED_FIELDS)
        use_client(client)
        assert await TdlNoteMine.define_table() == "\n".join([_K2E, *_TUNED[1:]])
        assert client.queries == [_INFO, _INFO_NOTE, _K2E, *_TUNED[1:]]

    async def test_a_new_column_on_an_existing_table(self, use_client: Callable[[Any], None]) -> None:
        """A column without a stored field gets ``field_statement()``'s; a stored field the model does not define,
        readable or not, is left as it is."""
        client = _TableClient(
            {"tdl_note": _STORED_NOTE_LESS}, {"other": "DEFINE FIELD other ON tdl_note TYPE int", "odd": "DEFINE FIELD"}
        )
        use_client(client)
        assert await TdlNote.define_table() == "\n".join(_G2)
        assert client.queries == [_INFO, _INFO_NOTE, *_G2]

    @pytest.mark.parametrize(
        ("model", "stored", "overwrite", "flexible"),
        [
            (TdlNoteMeta, None, True, False),
            (TdlNoteMeta, _STORED_NOTE_FULL, True, True),
            (TdlNoteMeta, _STORED_NOTE_LESS, True, False),
            (TdlNoteMetaLoose, _STORED_NOTE_FULL, True, False),
            (TdlNoteMeta, None, False, False),
            (TdlNoteMeta, _STORED_NOTE_FULL, False, True),
            (TdlNoteMetaLoose, _STORED_NOTE_FULL, False, True),
            (TdlNoteMetaLoose, None, False, False),
        ],
        ids=[
            "new-no-mode",
            "schemafull-kept",
            "schemaless-kept",
            "schemaless-declared",
            "if-not-exists-new",
            "if-not-exists-schemafull",
            "if-not-exists-schemafull-declared-schemaless",
            "if-not-exists-new-declared-schemaless",
        ],
    )
    async def test_flexible_follows_the_tables_mode(
        self,
        use_client: Callable[[Any], None],
        model: type[BaseSurrealModel],
        stored: str | None,
        overwrite: bool,
        flexible: bool,
    ) -> None:
        """``overwrite=True``: the declared mode, else the stored one, else ``SCHEMALESS``; ``overwrite=False`` (``IF
        NOT EXISTS`` leaves a table as it is): the stored mode, else the declared one, else ``SCHEMALESS``."""
        client = _TableClient({} if stored is None else {"tdl_note": stored})
        use_client(client)
        statements = (await model.define_table(overwrite=overwrite)).split("\n")
        clause = "OVERWRITE" if overwrite else "IF NOT EXISTS"
        meta = f"DEFINE FIELD {clause} `meta` ON `tdl_note` TYPE object"
        assert statements[-1] == (f"{meta} FLEXIBLE;" if flexible else f"{meta};")
        reads = [_INFO, _INFO_NOTE] if stored is not None and overwrite else [_INFO]
        assert client.queries == [*reads, *statements]

    async def test_overwrite_false_reads_no_field(self, use_client: Callable[[Any], None]) -> None:
        """``IF NOT EXISTS`` on a table that exists: no ``INFO FOR TABLE``, no field carried, none refused."""
        client = _TableClient(
            {"tdl_note": _STORED_NOTE_BAD_RULE}, {"done": "DEFINE FIELD done ON tdl_note TYPE bool ASSERT (a"}
        )
        use_client(client)
        assert await TdlNote.define_table(overwrite=False) == "\n".join(_if_not_exists(_G2))
        assert client.queries == [_INFO, *_if_not_exists(_G2)]

    async def test_a_transaction_gets_the_ddl_only(self, use_client: Callable[[Any], None]) -> None:
        """The reads on the client, on committed state, before anything is queued; the statements in the block."""
        client = _TableClient({"tdl_note": _STORED_NOTE}, _TUNED_FIELDS)
        use_client(client)
        tx = _QueuingTx()
        assert await TdlNoteTuned.define_table(tx=tx) == "\n".join(_TUNED)  # type: ignore[arg-type]
        assert client.queries == [_INFO, _INFO_NOTE]
        assert tx.added == [(statement, None) for statement in _TUNED]

    @pytest.mark.parametrize(
        ("version", "statements"),
        [("surrealdb-3.3.4", _LOOSE), ("surrealdb-2.7.0", _LOOSE_BEFORE_3), ("surrealdb-2.0.0", _LOOSE_BEFORE_3)],
        ids=["3.x", "2.x", "embedded"],
    )
    async def test_the_line_is_read_once_when_a_column_holds_any(
        self, use_client: Callable[[Any], None], version: str, statements: list[str]
    ) -> None:
        """A ``SCHEMAFULL`` table with columns holding ``any`` (item definitions included): ``version()`` once, after
        the reads and before any DDL. Before 3 — a 2.x server, or the embedded engine, which answers the SDK's
        ``surrealdb-2.0.0`` — those columns are written ``FLEXIBLE``; on 3.x the statements are ``table_ddl()``'s."""
        client = _TableClient(version=version)
        use_client(client)
        assert await TdlLoose.define_table() == "\n".join(statements)
        assert TdlLoose.table_ddl() == _LOOSE
        assert client.queries == [_INFO, *statements]
        assert (client.version_calls, client.version_at) == (1, 1)

    @pytest.mark.parametrize(
        ("model", "stored"),
        [(TdlNote, None), (TdlNote, _STORED_NOTE_FULL), (TdlPerson, None)],
        ids=["no-mode", "schemafull-kept", "schemafull-declared"],
    )
    async def test_no_line_read_without_a_column_holding_any(
        self, use_client: Callable[[Any], None], model: type[BaseSurrealModel], stored: str | None
    ) -> None:
        """No column holds ``any``: no ``version()``, whatever the table's mode."""
        client = _TableClient({} if stored is None else {model.get_table_name(): stored}, version="surrealdb-2.7.0")
        use_client(client)
        statements = (await model.define_table()).split("\n")
        assert client.queries[-len(statements) :] == statements
        assert client.version_calls == 0

    @pytest.mark.parametrize(
        ("model", "stored", "overwrite", "reads"),
        [
            (TdlNoteShapes, None, True, False),
            (TdlNoteShapes, _STORED_NOTE_LESS, True, False),
            (TdlNoteShapes, _STORED_NOTE_FULL, True, True),
            (TdlNoteShapes, None, False, False),
            (TdlNoteShapes, _STORED_NOTE_FULL, False, True),
            (TdlLog, None, True, False),
            (TdlLog, _STORED_LOG_FULL, True, False),
            (TdlLog, _STORED_LOG_FULL, False, True),
        ],
        ids=[
            "new-no-mode",
            "schemaless-kept",
            "schemafull-kept",
            "if-not-exists-new",
            "if-not-exists-schemafull",
            "schemaless-declared",
            "schemaless-declared-over-schemafull",
            "if-not-exists-schemafull-declared-schemaless",
        ],
    )
    async def test_the_tables_mode_decides_the_line_read(
        self,
        use_client: Callable[[Any], None],
        model: type[BaseSurrealModel],
        stored: str | None,
        overwrite: bool,
        reads: bool,
    ) -> None:
        """The line is read only when the table is ``SCHEMAFULL`` once its statement is applied (the declared mode,
        else the stored one; with ``overwrite=False`` the stored one first); then, before 3, the column holding ``any``
        is written ``FLEXIBLE`` — a new one, or one carried over its stored field."""
        table = model.get_table_name()
        stored_field = f"DEFINE FIELD {'anything' if model is TdlNoteShapes else 'extra'} ON {table} TYPE any PERMISSIONS FULL"
        client = _TableClient(
            {} if stored is None else {table: stored},
            {} if stored is None else {"anything" if model is TdlNoteShapes else "extra": stored_field},
            version="surrealdb-2.7.0",
        )
        use_client(client)
        statements = (await model.define_table(overwrite=overwrite)).split("\n")
        assert client.version_calls == (1 if reads else 0)
        [column] = [statement for statement in statements if " TYPE any" in statement]
        assert column.endswith(" TYPE any FLEXIBLE;" if reads else " TYPE any;"), column

    async def test_the_line_is_read_outside_the_transaction(self, use_client: Callable[[Any], None]) -> None:
        """``version()`` on the client, like the reads, before anything is queued; the statements in the block."""
        client = _TableClient(version="surrealdb-2.7.0")
        use_client(client)
        tx = _QueuingTx()
        assert await TdlLoose.define_table(tx=tx) == "\n".join(_LOOSE_BEFORE_3)  # type: ignore[arg-type]
        assert client.queries == [_INFO]
        assert (client.version_calls, client.version_at) == (1, 1)
        assert tx.added == [(statement, None) for statement in _LOOSE_BEFORE_3]

    async def test_a_transaction_of_another_connection(self, use_client: Callable[[Any], None]) -> None:
        client = _TableClient()
        use_client(client)
        tx = _QueuingTx("other")
        with pytest.raises(SurrealDbError) as raised:
            await TdlPerson.define_table(tx=tx)  # type: ignore[arg-type]
        assert type(raised.value) is SurrealDbError
        assert str(raised.value).startswith("TdlPerson.define_table() cannot run in this transaction: ")
        assert client.queries == []
        assert tx.added == []

    @pytest.mark.parametrize("overwrite", ["yes", 1, None], ids=repr)
    async def test_e1_before_any_request(self, use_client: Callable[[Any], None], overwrite: Any) -> None:
        client = _TableClient()
        use_client(client)
        with pytest.raises(TypeError) as raised:
            await TdlPerson.define_table(overwrite=overwrite)
        assert str(raised.value) == f"overwrite= must be a bool, got {type(overwrite).__name__}."
        assert client.queries == []

    async def test_ux1_and_cv1_before_any_request(self, use_client: Callable[[Any], None]) -> None:
        class TdlUserOpen(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_user", table_type=TableType.USER, extra="allow", connection="tdl")

            email: str

        class TdlBadSum(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tdl_cart", connection="tdl")

            items: list[str] = Field(default_factory=list)
            total: Computed[int] = computed("count(items")

        client = _TableClient()
        use_client(client)
        with pytest.raises(TypeError) as ux1:
            await TdlUserOpen.define_table()
        assert (type(ux1.value), str(ux1.value)) == (TypeError, _ux1("TdlUserOpen"))
        with pytest.raises(ValueError) as cv1:
            await TdlBadSum.define_table()
        assert (type(cv1.value), str(cv1.value)) == (ValueError, _cv1("TdlBadSum", "total", _R_BRACKETS))
        assert client.queries == []

    @pytest.mark.parametrize(
        ("error", "refusal"),
        [
            (InternalError("Internal", _NOT_ENOUGH_PERMISSIONS), SurrealDbAuthenticationError),
            (NotAllowedError("NotAllowed", _NOT_ENOUGH_PERMISSIONS), SurrealDbAuthenticationError),
            (InternalError("Internal", "There was a problem with the database"), SurrealDbError),
            (RuntimeError("the engine is closed"), SurrealDbError),
        ],
        ids=["s1-internal", "s1-not-allowed", "s2-internal", "s2-runtime"],
    )
    @pytest.mark.parametrize("read", ["db", "table"])
    async def test_s1_s2_a_refused_read(
        self, use_client: Callable[[Any], None], error: BaseException, refusal: type[SurrealDbError], read: str
    ) -> None:
        if read == "db":
            client = _TableClient(info_error=error)
            sent = [_INFO]
        else:
            client = _TableClient({"tdl_note": _STORED_NOTE}, fields_error=error)
            sent = [_INFO, _INFO_NOTE]
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await TdlNoteTuned.define_table()
        assert type(raised.value) is refusal
        assert str(raised.value) == f"Can't apply table definition: 'tdl_note' -> {error}"
        assert raised.value.__cause__ is error
        assert client.queries == sent

    async def test_s3_the_table_statement_refused(self, use_client: Callable[[Any], None]) -> None:
        error = NotAllowedError("NotAllowed", _NOT_ENOUGH_PERMISSIONS)
        client = _TableClient(ddl_error=error)
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await TdlPerson.define_table()
        assert type(raised.value) is SurrealDbAuthenticationError
        assert str(raised.value) == f"Can't apply table definition: 'tdl_person' -> {_NOT_ENOUGH_PERMISSIONS}"
        assert raised.value.__cause__ is error
        assert client.queries == [_INFO, _G1[0]]

    @pytest.mark.parametrize(
        ("reason", "text"),
        [
            ("The field is locked", "The field is locked"),
            (
                "Parse error: Unexpected token `DEFAULT`\n --> [1:47]\n  |\n1 | DEFINE FIELD OVERWRITE `age` ON `tdl_person` "
                "TYPE int DEFAULT 18",
                "Parse error: Unexpected token `DEFAULT` (at line 1, column 47 of the statement)",
            ),
            (
                "Found a problem in DEFINE FIELD OVERWRITE `age` ON `tdl_person` TYPE int DEFAULT 18",
                "the server's message quotes the statement, so it is left out",
            ),
        ],
        ids=["whole", "parse-excerpt", "quoting"],
    )
    async def test_s4_a_field_statement_refused(self, use_client: Callable[[Any], None], reason: str, text: str) -> None:
        """The field named, never its statement; the statements before it stay applied (no transaction)."""
        client = _TableClient(ddl_error=InternalError("Internal", reason), refuse="DEFINE FIELD OVERWRITE `age` ")
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await TdlPerson.define_table()
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == f"Can't apply field definition: 'age' on table 'tdl_person' -> {text}"
        assert "TYPE int DEFAULT 18" not in str(raised.value)
        assert client.queries == [_INFO, *_G1[:3]]

    async def test_s4_names_a_nested_field(self, use_client: Callable[[Any], None]) -> None:
        client = _TableClient(
            ddl_error=InternalError("Internal", "The field is locked"), refuse="DEFINE FIELD OVERWRITE `address`.`city` "
        )
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await TdlPerson.define_table()
        assert str(raised.value) == "Can't apply field definition: 'address.city' on table 'tdl_person' -> The field is locked"
        assert client.queries == [_INFO, *_G1[:8]]


class TestTableGuard:
    """TG1–TG4 before any DDL, naming ``define_table()``: only the reads are sent."""

    @pytest.mark.parametrize("overwrite", [True, False], ids=["overwrite", "if-not-exists"])
    async def test_tg1_a_view(self, use_client: Callable[[Any], None], overwrite: bool) -> None:
        client = _TableClient({"tdl_note": _STORED_NOTE_VIEW})
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await TdlNote.define_table(overwrite=overwrite)
        assert (type(raised.value), str(raised.value)) == (SurrealDbError, _tg1("TdlNote", "define_table", "tdl_note"))
        assert client.queries == [_INFO]

    @pytest.mark.parametrize("overwrite", [True, False], ids=["overwrite", "if-not-exists"])
    @pytest.mark.parametrize("stored", ["DEFINE TABLE tdl_note TYPE", 3], ids=["unparsable", "not-a-str"])
    async def test_tg2_an_unreadable_table(self, use_client: Callable[[Any], None], overwrite: bool, stored: object) -> None:
        client = _TableClient({"tdl_note": stored})
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await TdlNote.define_table(overwrite=overwrite)
        assert (type(raised.value), str(raised.value)) == (SurrealDbError, _tg2("TdlNote", "define_table", "tdl_note"))
        assert client.queries == [_INFO]

    async def test_tg2_a_carried_rule_that_fails_its_check(self, use_client: Callable[[Any], None]) -> None:
        """Refused before the field read; a model declaring its permissions replaces the rule, which is not checked."""
        client = _TableClient({"tdl_note": _STORED_NOTE_BAD_RULE})
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await TdlNote.define_table()
        assert (type(raised.value), str(raised.value)) == (SurrealDbError, _tg2("TdlNote", "define_table", "tdl_note"))
        assert client.queries == [_INFO]

        client = _TableClient({"tdl_note": _STORED_NOTE_BAD_RULE})
        use_client(client)
        assert (await TdlNoteMine.define_table()).startswith(
            "DEFINE TABLE OVERWRITE `tdl_note` TYPE NORMAL SCHEMALESS COMMENT 'mine' PERMISSIONS FOR select FULL, FOR "
            "create, update, delete NONE;\n"
        )

    @pytest.mark.parametrize("overwrite", [True, False], ids=["overwrite", "if-not-exists"])
    async def test_tg3_a_relation_table(self, use_client: Callable[[Any], None], overwrite: bool) -> None:
        client = _TableClient({"tdl_note": _STORED_NOTE_RELATION})
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await TdlNote.define_table(overwrite=overwrite)
        assert (type(raised.value), str(raised.value)) == (SurrealDbError, _tg3("TdlNote", "define_table", "tdl_note"))
        assert client.queries == [_INFO]

    @pytest.mark.parametrize(
        ("fields", "field"),
        [
            ({"done": "DEFINE FIELD done ON tdl_note TYPE bool ASSERT (a"}, "done"),
            ({"done": 3}, "done"),
            (
                {"done": "DEFINE FIELD done ON tdl_note TYPE bool PERMISSIONS FOR select WHERE a; b, FOR create, update FULL"},
                "done",
            ),
            ({"body": "DEFINE FIELD body ON tdl_note TYPE string ASSERT $value != '' COMMENT 'a\x00b'"}, "body"),
        ],
        ids=["unparsable", "not-a-str", "carried-rule", "carried-comment"],
    )
    async def test_tg4_a_field_the_model_redefines(
        self, use_client: Callable[[Any], None], fields: dict[str, object], field: str
    ) -> None:
        client = _TableClient({"tdl_note": _STORED_NOTE_LESS}, fields)
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await TdlNote.define_table()
        assert (type(raised.value), str(raised.value)) == (
            SurrealDbError,
            _tg4("TdlNote", "define_table", "tdl_note", field),
        )
        assert client.queries == [_INFO, _INFO_NOTE]

    async def test_tg4_names_the_canonical_path(self, use_client: Callable[[Any], None]) -> None:
        client = _TableClient(
            {"tdl_kinds": "DEFINE TABLE tdl_kinds TYPE NORMAL SCHEMAFULL PERMISSIONS NONE"},
            {"bag[*]": "DEFINE FIELD bag[*] ON tdl_kinds TYPE any ASSERT (a"},
        )
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await TdlKinds.define_table()
        assert str(raised.value) == _tg4("TdlKinds", "define_table", "tdl_kinds", "bag.*")
        assert client.queries == [_INFO, "INFO FOR TABLE `tdl_kinds`;"]


# --- the rich model of the ORM writes -------------------------------------------------------------------------------


class TdlKitchenBase(BaseSurrealModel):
    """Every kind the ORM writes, on a ``SCHEMAFULL`` table, with a typed computed field; ``set[int]`` is on
    :class:`TdlKitchen` (SurrealDB 3.1 refuses the ORM's ``set`` value)."""

    model_config = SurrealConfigDict(table_name="tdl_kitchen", schema_mode=SchemaMode.SCHEMAFULL, connection="tdl")

    id: str | None = None
    s: str
    i: int
    f: float
    b: bool
    d: Decimal
    dt: datetime
    da: date
    td: timedelta
    du: Duration
    u: UUID
    by: bytes
    r: RecordID
    ls: list[str]
    tu: tuple[int, str]
    dc: dict[str, Any]
    sub: TdlAddress
    subs: list[TdlAddress]
    osub: TdlAddress | None = None
    lit: Literal["a", "b"]
    en: TdlTier
    ie: TdlLevel
    opt: int | None = None
    un: int | str
    anyv: Any = None
    geo: PointField = None
    rng: Range[int] | None = None
    al: str = Field(alias="aliased_col")
    tot: Computed[int] = computed("i + 1")


class TdlKitchen(TdlKitchenBase):
    """:class:`TdlKitchenBase` with a ``set[int]`` column."""

    st: set[int]


_KITCHEN_BASE = [
    "DEFINE TABLE OVERWRITE `tdl_kitchen` TYPE NORMAL SCHEMAFULL;",
    "DEFINE FIELD OVERWRITE `s` ON `tdl_kitchen` TYPE string;",
    "DEFINE FIELD OVERWRITE `i` ON `tdl_kitchen` TYPE int;",
    "DEFINE FIELD OVERWRITE `f` ON `tdl_kitchen` TYPE float;",
    "DEFINE FIELD OVERWRITE `b` ON `tdl_kitchen` TYPE bool;",
    "DEFINE FIELD OVERWRITE `d` ON `tdl_kitchen` TYPE decimal;",
    "DEFINE FIELD OVERWRITE `dt` ON `tdl_kitchen` TYPE datetime;",
    "DEFINE FIELD OVERWRITE `da` ON `tdl_kitchen` TYPE datetime;",
    "DEFINE FIELD OVERWRITE `td` ON `tdl_kitchen` TYPE duration;",
    "DEFINE FIELD OVERWRITE `du` ON `tdl_kitchen` TYPE duration;",
    "DEFINE FIELD OVERWRITE `u` ON `tdl_kitchen` TYPE uuid;",
    "DEFINE FIELD OVERWRITE `by` ON `tdl_kitchen` TYPE bytes;",
    "DEFINE FIELD OVERWRITE `r` ON `tdl_kitchen` TYPE record;",
    "DEFINE FIELD OVERWRITE `ls` ON `tdl_kitchen` TYPE array<string>;",
    "DEFINE FIELD OVERWRITE `tu` ON `tdl_kitchen` TYPE [int, string];",
    "DEFINE FIELD OVERWRITE `dc` ON `tdl_kitchen` TYPE object FLEXIBLE;",
    "DEFINE FIELD OVERWRITE `sub` ON `tdl_kitchen` TYPE object;",
    "DEFINE FIELD OVERWRITE `sub`.`city` ON `tdl_kitchen` TYPE string;",
    "DEFINE FIELD OVERWRITE `sub`.`zip` ON `tdl_kitchen` TYPE option<string>;",
    "DEFINE FIELD OVERWRITE `subs` ON `tdl_kitchen` TYPE array<object>;",
    "DEFINE FIELD OVERWRITE `subs`.*.`city` ON `tdl_kitchen` TYPE string;",
    "DEFINE FIELD OVERWRITE `subs`.*.`zip` ON `tdl_kitchen` TYPE option<string>;",
    "DEFINE FIELD OVERWRITE `osub` ON `tdl_kitchen` TYPE option<object>;",
    "DEFINE FIELD OVERWRITE `osub`.`city` ON `tdl_kitchen` TYPE string;",
    "DEFINE FIELD OVERWRITE `osub`.`zip` ON `tdl_kitchen` TYPE option<string>;",
    "DEFINE FIELD OVERWRITE `lit` ON `tdl_kitchen` TYPE 'a' | 'b';",
    "DEFINE FIELD OVERWRITE `en` ON `tdl_kitchen` TYPE 'free' | 'pro';",
    "DEFINE FIELD OVERWRITE `ie` ON `tdl_kitchen` TYPE 1 | 2;",
    "DEFINE FIELD OVERWRITE `opt` ON `tdl_kitchen` TYPE option<int>;",
    "DEFINE FIELD OVERWRITE `un` ON `tdl_kitchen` TYPE int | string;",
    "DEFINE FIELD OVERWRITE `anyv` ON `tdl_kitchen` TYPE any;",
    "DEFINE FIELD OVERWRITE `geo` ON `tdl_kitchen` TYPE option<geometry<point>>;",
    "DEFINE FIELD OVERWRITE `rng` ON `tdl_kitchen` TYPE option<range>;",
    "DEFINE FIELD OVERWRITE `aliased_col` ON `tdl_kitchen` TYPE string;",
    "DEFINE FIELD OVERWRITE `tot` ON `tdl_kitchen` TYPE option<int> VALUE i + 1;",
]


# === E2E: the connection "tdl" on the database tdl_db, over WebSocket and HTTP, and mem:// =========================

#: The engines every E2E class runs on: the line under test over WebSocket and HTTP, and the embedded engine.
_KINDS = ["ws", "http", "mem"]

#: The reset before each E2E test, in one query.
_TDL_RESET = "REMOVE DATABASE IF EXISTS tdl_db; DEFINE DATABASE tdl_db;"

#: The skip reason of the keyword-named fields on SurrealDB 3.1.
_KEYWORD_SKIP = "SurrealDB 3.1 stores a keyword field name it cannot read back"


def _url(kind: str) -> str:
    """The URL of *kind*: ``ws`` or ``http`` on the line under test (``SURREALDB_PORT``), or ``mem``."""
    if kind == "mem":
        return "mem://"
    host = os.environ.get("SURREALDB_HOST", "localhost")
    port = os.environ.get("SURREALDB_PORT", "8000")
    return f"ws://{host}:{port}/rpc" if kind == "ws" else f"http://{host}:{port}"


def _register_tdl(kind: str, name: str = "tdl", *, database: str = "tdl_db") -> None:
    if kind == "mem":
        SurrealDBConnectionManager.add_connection(name, url="mem://", namespace="ns", database=database)
    else:
        SurrealDBConnectionManager.add_connection(
            name, url=_url(kind), user="root", password="root", namespace="ns", database=database
        )


async def _apply_ddl(client: Any, ddl: str) -> None:
    """Run *ddl* and check every statement: ``query()`` checks only the first one."""
    response = await client.query_raw(ddl, {})
    assert "error" not in response, response["error"]
    refused = [result for result in response["result"] if result["status"] != "OK"]
    assert response["result"] and not refused, refused


@contextlib.asynccontextmanager
async def _tdl(kind: str) -> AsyncIterator[Any]:
    """The connection ``"tdl"`` on *kind*, its database ``tdl_db`` reset in one query; removed, then unregistered, on
    exit."""
    _register_tdl(kind)
    client = await SurrealDBConnectionManager.get_client("tdl")
    await _apply_ddl(client, _TDL_RESET)
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            cleanup = await SurrealDBConnectionManager.get_client("tdl")
            await cleanup.query("REMOVE DATABASE IF EXISTS tdl_db;", {})
        await SurrealDBConnectionManager.remove_connection("tdl")


async def _tables(client: Any) -> dict[str, Any]:
    """The ``tables`` of ``INFO FOR DB`` on *client*'s database."""
    return dict((await client.query("INFO FOR DB;", {}))["tables"])


async def _line(client: Any) -> tuple[int, int]:
    """``(major, minor)`` of the engine *client* talks to: ``"surrealdb-3.1.5"`` → ``(3, 1)``; ``mem://`` →
    ``(2, 0)`` (the SDK's version, whose core is 2.x)."""
    match = re.search(r"(\d+)\.(\d+)", await client.version())
    assert match is not None
    return int(match.group(1)), int(match.group(2))


async def _major(client: Any) -> int:
    """The major version of the engine *client* talks to: ``"surrealdb-3.3.4"`` → 3; ``mem://`` → 2."""
    return (await _line(client))[0]


async def _table_info(client: Any, table: str) -> dict[str, Any]:
    """``INFO FOR TABLE`` *table* (a constant name of these tests)."""
    info: dict[str, Any] = await client.query(f"INFO FOR TABLE {table};", {})
    return info


def _feed(text: str | None) -> tuple[Duration | None, bool]:
    """A change feed as ``(duration, include original)``: SurrealDB renders ``7d`` as ``1w``."""
    if text is None:
        return None, False
    duration, _, rest = text.partition(" ")
    return Duration.parse(duration), rest == "INCLUDE ORIGINAL"


class TestTableDdlE2E:
    """Each golden of a node model, applied raw on every engine of both lines, is accepted and read back: the table's
    type, mode, comment, change feed and permissions, and each written column's kind, ``option<…>`` and ``FLEXIBLE``
    (an item definition keyed ``("bag", "*")`` on both renderings)."""

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize(("model", "statements"), _GOLDENS, ids=_GOLDEN_IDS)
    async def test_each_golden_is_applied_and_read_back(
        self, kind: str, model: type[BaseSurrealModel], statements: list[str]
    ) -> None:
        async with _tdl(kind) as client:
            if model is TdlKeyword and await _line(client) == (3, 1):
                pytest.skip(_KEYWORD_SKIP)
            assert model.table_ddl() == statements
            await _apply_ddl(client, " ".join(statements))
            table = model.get_table_name()
            state = parse_define_table((await _tables(client))[table])
            assert state.table_type == ("any" if model.get_table_type() is TableType.ANY else "normal")
            assert state.schema_mode == (model.get_schema_mode() or SchemaMode.SCHEMALESS).value
            assert state.comment == model.model_config.get("comment")
            feed = state.changefeed
            if feed is not None and state.changefeed_include_original:
                feed += " INCLUDE ORIGINAL"
            assert _feed(feed) == _feed(model.get_changefeed())
            declared = model.model_config.get("permissions")
            if declared is not None:
                assert state.permissions == schema.permissions_rules(declared, model=model)
            fields = [parse_define_field(text).state for text in (await _table_info(client, table))["fields"].values()]
            stored = {field.path: field for field in fields}
            columns = _columns(model)
            assert len(statements) == 1 + len(columns)
            for column, statement in zip(columns, statements[1:], strict=True):
                field = stored[column.path]
                assert (field.field_type, field.nullable) == normalise_kind(render_kind(column.kind)), statement
                assert field.flexible is (" FLEXIBLE" in statement), statement
                assert field.value == column.value, statement

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_kitchen_is_applied(self, kind: str) -> None:
        async with _tdl(kind) as client:
            model = TdlKitchenBase if await _line(client) == (3, 1) else TdlKitchen
            await _apply_ddl(client, " ".join(model.table_ddl()))
            fields = (await _table_info(client, "tdl_kitchen"))["fields"]
            stored = {parse_define_field(text).state.path for text in fields.values()}
            assert {column.path for column in _columns(model)} <= stored


# --- the ORM's writes into a table table_ddl() defines --------------------------------------------------------------


def _kitchen_fields(model: type[TdlKitchenBase], record: str | None, **values: Any) -> dict[str, Any]:
    """The fields of an instance of *model* with id *record* (none when ``None``): one value of each kind, ``anyv`` a
    scalar; *values* override."""
    fields: dict[str, Any] = {
        "s": "x",
        "i": 3,
        "f": 2.0,
        "b": True,
        "d": Decimal("1.50"),
        "dt": datetime(2024, 1, 2, 3, 4, 5, 123456, tzinfo=UTC),
        "da": date(2024, 1, 2),
        "td": timedelta(seconds=90),
        "du": Duration.parse("1h30m"),
        "u": UUID("12345678-1234-5678-1234-567812345678"),
        "by": b"\x00\x01",
        "r": RecordID("tdl_note", "ada"),
        "ls": ["a", "b"],
        "tu": (1, "x"),
        "dc": {"nested": {"deep": [1, 2]}, "k": "v"},
        "sub": TdlAddress(city="Paris"),
        "subs": [TdlAddress(city="A", zip="1"), TdlAddress(city="B")],
        "osub": None,
        "lit": "a",
        "en": TdlTier.PRO,
        "ie": TdlLevel.TWO,
        "opt": None,
        "un": "s",
        "anyv": 5,
        "geo": GeometryPoint(1.0, 2.0),
        "rng": Range(BoundIncluded(1), BoundExcluded(5)),
        "al": "al",
    }
    if record is not None:
        fields["id"] = record
    if "st" in model.model_fields:
        fields["st"] = {3, 1, 2}
    fields.update(values)
    return fields


def _kitchen(model: type[TdlKitchenBase], record: str, **values: Any) -> TdlKitchenBase:
    """An instance of *model* with id *record*: :func:`_kitchen_fields`."""
    return model(**_kitchen_fields(model, record, **values))


async def _read_back(model: type[TdlKitchenBase], record: Any, expected: TdlKitchenBase) -> TdlKitchenBase:
    """The record *record* read with ``get()``: every field equal to *expected*'s (the id is the one read; ``tot``
    aside, the server computes it) and ``tot`` equal to ``i + 1``."""
    got = await model.objects().get(record)
    assert got.model_dump(exclude={"id", "tot"}) == expected.model_dump(exclude={"id", "tot"})
    assert got.tot == got.i + 1
    return got


def _intent(instance: TdlKitchenBase, **changes: Any) -> TdlKitchenBase:
    """A deep copy of *instance* with *changes*: what a write is meant to store, taken before the write (a write
    syncs its instance from the row the server returns, so the instance itself proves nothing)."""
    return instance.model_copy(update=changes, deep=True)


async def _write_everything(model: type[TdlKitchenBase], first: Any, second: Any) -> None:
    """Every ORM write into *model*'s table, ``anyv`` holding *first* (then *second* after ``update()``): each accepted,
    every field read back equal to what the write sent, and ``tot`` read ``i + 1`` after ``save()``, ``update()``,
    ``upsert()``, ``merge()``, ``bulk_create()``, ``bulk_update()`` and ``get_or_create()``."""
    k1 = _kitchen(model, "k1", anyv=first)
    sent = _intent(k1)
    await k1.save()
    await _read_back(model, "k1", sent)

    k1.s = "changed"
    k1.i = 4
    k1.opt = 5
    k1.osub = TdlAddress(city="Rome", zip="00100")
    k1.anyv = second
    sent = _intent(k1)
    await k1.update()
    await _read_back(model, "k1", sent)

    await k1.merge(i=9, opt=None, dc={"z": {"w": 1}})
    k1 = await _read_back(model, "k1", _intent(sent, i=9, opt=None, dc={"nested": {"deep": [1, 2]}, "k": "v", "z": {"w": 1}}))
    assert k1.tot == 10

    k2 = _kitchen(model, "k2", i=2, anyv=first)
    sent = _intent(k2)
    await k2.upsert()
    await _read_back(model, "k2", sent)
    k2.i = 7
    sent = _intent(k2)
    await k2.upsert()
    assert (await _read_back(model, "k2", sent)).tot == 8

    sent = _intent(k1, s="p")
    await k1.patch([{"op": "replace", "path": "/s", "value": "p"}])
    await _read_back(model, "k1", sent)
    sent = _intent(sent, ls=["a", "b", "c"])
    await k1.atomic_append("ls", "c")
    await _read_back(model, "k1", sent)
    sent = _intent(sent, i=11)
    await k1.atomic_increment("i", 2)
    assert (await _read_back(model, "k1", sent)).tot == 12

    k3 = _kitchen(model, "k3", s="c", i=30, anyv=first)
    k4 = _kitchen(model, "k4", s="c", i=40, anyv=second)
    sent3, sent4 = _intent(k3), _intent(k4)
    await model.objects().bulk_create([k3, k4])
    await _read_back(model, "k3", sent3)
    await _read_back(model, "k4", sent4)
    assert await model.objects().filter(s="c").bulk_update(s="bu") == 2
    assert (await _read_back(model, "k3", _intent(sent3, s="bu"))).tot == 31
    assert (await _read_back(model, "k4", _intent(sent4, s="bu"))).tot == 41

    defaults = _kitchen_fields(model, None, i=50, anyv=first)
    del defaults["s"]
    sent = model(**defaults, s="g")
    made, created = await model.objects().get_or_create(defaults=defaults, s="g")
    assert created is True
    assert (await _read_back(model, made.id, sent)).tot == 51
    again, created = await model.objects().get_or_create(defaults=defaults, s="g")
    assert created is False
    assert again.get_raw_id() == made.get_raw_id()

    k6 = _kitchen(model, "k6", osub=None, opt=None, anyv=None, geo=None, rng=None)
    sent = _intent(k6)
    await k6.save()
    await _read_back(model, "k6", sent)

    await k1.delete()
    with pytest.raises(SurrealDbNotFoundError):
        await model.objects().get("k1")


#: ``TdlKitchen``'s typed computed field, as ``table_ddl()`` and ``define_table()`` write it.
_KITCHEN_TOT = "DEFINE FIELD OVERWRITE `tot` ON `tdl_kitchen` TYPE option<int> VALUE i + 1;"


class TestOrmWritesE2E:
    """Every ORM write into the rich model's table (``TdlKitchenBase`` on SurrealDB 3.1, which refuses the ORM's ``set``
    value; ``TdlKitchen`` elsewhere): accepted, every field read back equal, and the typed computed field ``tot`` read
    ``i + 1`` after each write, on every engine — over ``table_ddl()``'s statements applied by hand with ``anyv``
    holding scalars, and over ``define_table()``'s with ``anyv`` holding an object and an array (kept on SurrealDB 2.x
    and the embedded engine by the ``FLEXIBLE`` ``define_table()`` adds there)."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_writes_round_trip(self, kind: str) -> None:
        async with _tdl(kind) as client:
            model = TdlKitchenBase if await _line(client) == (3, 1) else TdlKitchen
            await _apply_ddl(client, " ".join(model.table_ddl()))
            await _write_everything(model, 5, "s")

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_writes_round_trip_after_define_table(self, kind: str) -> None:
        """``define_table()`` returns ``table_ddl()``'s statements, ``anyv`` written ``FLEXIBLE`` before SurrealDB 3.x;
        then an object and an array in ``anyv`` read back equal on every engine."""
        async with _tdl(kind) as client:
            line = await _line(client)
            model = TdlKitchenBase if line == (3, 1) else TdlKitchen
            applied = (await model.define_table()).split("\n")
            assert _KITCHEN_TOT in applied
            anyv = "DEFINE FIELD OVERWRITE `anyv` ON `tdl_kitchen` TYPE any;"
            flexible = "DEFINE FIELD OVERWRITE `anyv` ON `tdl_kitchen` TYPE any FLEXIBLE;"
            expected = [flexible if line[0] < 3 and statement == anyv else statement for statement in model.table_ddl()]
            assert applied == expected
            await _write_everything(model, {"x": {"y": [1, {"z": 2}]}}, [1, {"k": "v"}])

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_set_field(self, kind: str) -> None:
        """A ``set[int]`` field reads back equal on every engine but SurrealDB 3.1, which refuses the ORM's ``set``
        value (README behaviour row "`TYPE set<…>` columns": "3.1: writing the array is refused")."""
        async with _tdl(kind) as client:
            await _apply_ddl(client, " ".join(TdlKitchen.table_ddl()))
            kitchen = _kitchen(TdlKitchen, "k1")
            if await _line(client) == (3, 1):
                with pytest.raises(InternalError, match="Expected `set` but found") as refused:
                    await kitchen.save()
                assert type(refused.value) is InternalError, "SurrealDB 3.1 refuses the ORM's set value with the SDK's error"
                assert await TdlKitchen.objects().count() == 0
                return
            await kitchen.save()
            assert (await _read_back(TdlKitchen, "k1", kitchen)).st == {1, 2, 3}


class TdlUnions(BaseSurrealModel):
    """Unions holding several containers, on a table in SurrealDB's default mode (``SCHEMALESS``)."""

    model_config = SurrealConfigDict(table_name="tdl_unions", connection="tdl")

    id: str | None = None
    dl: dict[str, Any] | list[str] = Field(default_factory=list)
    two: list[int] | list[str] = Field(default_factory=list)
    c: list[int] | dict[str, Any] | None = None
    sd: set[int] | dict[str, Any] = Field(default_factory=set)
    tl: tuple[int, str] | list[str] = Field(default_factory=list)
    nl: list[list[int] | dict[str, Any]] = Field(default_factory=list)
    lds: list[dict[str, Any]] | list[str] = Field(default_factory=list)
    nls: list[list[dict[str, Any]] | list[str]] = Field(default_factory=list)
    tdl: tuple[list[dict[str, Any]] | list[str], int] | None = None


class TdlUnionsStrict(TdlUnions):
    """``TdlUnions`` on a ``SCHEMAFULL`` table."""

    model_config = SurrealConfigDict(table_name="tdl_unions", schema_mode=SchemaMode.SCHEMAFULL, connection="tdl")


#: ``TdlUnions.table_ddl()``'s column statements: each union holding a list or a set with an object or another
#: container is written with untyped ``array`` members, whose items get ``TYPE any``.
_UNION_COLUMNS = [
    "DEFINE FIELD OVERWRITE `dl` ON `tdl_unions` TYPE object | array;",
    "DEFINE FIELD OVERWRITE `dl`.* ON `tdl_unions` TYPE any;",
    "DEFINE FIELD OVERWRITE `two` ON `tdl_unions` TYPE array;",
    "DEFINE FIELD OVERWRITE `two`.* ON `tdl_unions` TYPE any;",
    "DEFINE FIELD OVERWRITE `c` ON `tdl_unions` TYPE option<array | object>;",
    "DEFINE FIELD OVERWRITE `c`.* ON `tdl_unions` TYPE any;",
    "DEFINE FIELD OVERWRITE `sd` ON `tdl_unions` TYPE array | object;",
    "DEFINE FIELD OVERWRITE `sd`.* ON `tdl_unions` TYPE any;",
    "DEFINE FIELD OVERWRITE `tl` ON `tdl_unions` TYPE array;",
    "DEFINE FIELD OVERWRITE `tl`.* ON `tdl_unions` TYPE any;",
    "DEFINE FIELD OVERWRITE `nl` ON `tdl_unions` TYPE array<array | object>;",
    "DEFINE FIELD OVERWRITE `nl`.*.* ON `tdl_unions` TYPE any;",
    "DEFINE FIELD OVERWRITE `lds` ON `tdl_unions` TYPE array;",
    "DEFINE FIELD OVERWRITE `lds`.* ON `tdl_unions` TYPE any;",
    "DEFINE FIELD OVERWRITE `nls` ON `tdl_unions` TYPE array<array>;",
    "DEFINE FIELD OVERWRITE `nls`.*.* ON `tdl_unions` TYPE any;",
    "DEFINE FIELD OVERWRITE `tdl` ON `tdl_unions` TYPE option<[array, int]>;",
]

#: ``TdlUnionsStrict.table_ddl()``'s column statements: ``FLEXIBLE`` only where the kind still holds an object —
#: SurrealDB 3.x refuses it on ``array``, ``array<array>`` and ``[array, int]``.
_UNION_COLUMNS_STRICT = [
    "DEFINE FIELD OVERWRITE `dl` ON `tdl_unions` TYPE object | array FLEXIBLE;",
    "DEFINE FIELD OVERWRITE `dl`.* ON `tdl_unions` TYPE any;",
    "DEFINE FIELD OVERWRITE `two` ON `tdl_unions` TYPE array;",
    "DEFINE FIELD OVERWRITE `two`.* ON `tdl_unions` TYPE any;",
    "DEFINE FIELD OVERWRITE `c` ON `tdl_unions` TYPE option<array | object> FLEXIBLE;",
    "DEFINE FIELD OVERWRITE `c`.* ON `tdl_unions` TYPE any;",
    "DEFINE FIELD OVERWRITE `sd` ON `tdl_unions` TYPE array | object FLEXIBLE;",
    "DEFINE FIELD OVERWRITE `sd`.* ON `tdl_unions` TYPE any;",
    "DEFINE FIELD OVERWRITE `tl` ON `tdl_unions` TYPE array;",
    "DEFINE FIELD OVERWRITE `tl`.* ON `tdl_unions` TYPE any;",
    "DEFINE FIELD OVERWRITE `nl` ON `tdl_unions` TYPE array<array | object> FLEXIBLE;",
    "DEFINE FIELD OVERWRITE `nl`.*.* ON `tdl_unions` TYPE any;",
    "DEFINE FIELD OVERWRITE `lds` ON `tdl_unions` TYPE array;",
    "DEFINE FIELD OVERWRITE `lds`.* ON `tdl_unions` TYPE any;",
    "DEFINE FIELD OVERWRITE `nls` ON `tdl_unions` TYPE array<array>;",
    "DEFINE FIELD OVERWRITE `nls`.*.* ON `tdl_unions` TYPE any;",
    "DEFINE FIELD OVERWRITE `tdl` ON `tdl_unions` TYPE option<[array, int]>;",
]


class TestUnionContainersE2E:
    """A union holding a list or a set with an object or another list, set or tuple (``dict[str, Any] | list[str]``,
    ``list[int] | list[str]``): every ORM write is accepted after ``define_table()`` and reads back equal, on every engine
    of both lines and in both table modes. Written as SurrealDB reads it, ``object | array<string>`` would make it
    derive the item definition ``dl.*`` ``TYPE string`` and refuse ``{"k": 1}``; 2.x and the embedded engine would
    refuse ``["a"]`` in ``array<int> | array<string>``. A list of objects in such a union (``lds``, ``nls``, ``tdl``)
    becomes an ``array`` holding no object: no ``FLEXIBLE`` on 3.x, which refuses it there, and its nested objects
    are kept."""

    def test_the_statements(self) -> None:
        assert TdlUnions.table_ddl() == ["DEFINE TABLE OVERWRITE `tdl_unions` TYPE NORMAL;", *_UNION_COLUMNS]

    def test_the_schemafull_statements(self) -> None:
        assert TdlUnionsStrict.table_ddl() == [
            "DEFINE TABLE OVERWRITE `tdl_unions` TYPE NORMAL SCHEMAFULL;",
            *_UNION_COLUMNS_STRICT,
        ]

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize("model", [TdlUnions, TdlUnionsStrict], ids=["schemaless", "schemafull"])
    async def test_writes_round_trip(self, kind: str, model: type[TdlUnions]) -> None:
        async with _tdl(kind):
            await model.define_table()
            first = model(
                id="u1",
                dl={"k": 1, "n": {"r": [1, "x"]}},
                two=["a", "b"],
                c={"q": [1], "w": "s"},
                sd={"q": {"r": 1}},
                tl=(1, "s"),
                nl=[{"k": "s"}, [1, 2]],
                lds=[{"a": {"b": 1}}, {"c": [{"d": "e"}]}],
                nls=[[{"k": {"m": 1}}], ["s"]],
                tdl=([{"a": {"b": 1}}], 2),
            )
            sent = first.model_dump()
            await first.save()
            assert (await model.objects().get("u1")).model_dump() == sent

            second = model(
                id="u2",
                dl=["a"],
                two=[1, 2],
                c=[1, 2],
                sd={1, 2},
                tl=["a", "b"],
                nl=[[3], {"q": [1]}],
                lds=["a"],
                nls=[["x", "y"]],
                tdl=(["s"], 1),
            )
            sent = second.model_dump()
            await second.save()
            assert (await model.objects().get("u2")).model_dump() == sent

            first.dl = ["z"]
            first.two = [3]
            first.c = None
            first.sd = {3}
            first.tl = ["c"]
            first.nl = [{"m": {"o": 1}}]
            first.lds = [{"x": 1}]
            first.nls = [[{"q": {"r": [1]}}]]
            first.tdl = None
            sent = first.model_dump()
            await first.update()
            assert (await model.objects().get("u1")).model_dump() == sent


# --- define_table() on the engines ----------------------------------------------------------------------------------


def _spy(monkeypatch: pytest.MonkeyPatch, client: Any) -> list[tuple[str, tuple[object, ...]]]:
    """Record every ``query``, ``query_raw`` and ``version`` call *client* receives, with its arguments (and let it
    through) — v0.31.0's spy."""
    calls: list[tuple[str, tuple[object, ...]]] = []

    def recording(method: str) -> Any:
        real = getattr(client, method)

        async def call(*args: object, **kwargs: object) -> object:
            calls.append((method, args))
            return await real(*args, **kwargs)

        return call

    for method in ("query", "query_raw", "version"):
        monkeypatch.setattr(client, method, recording(method))
    return calls


def _sent(calls: list[tuple[str, tuple[object, ...]]]) -> list[object]:
    """The statements *calls* (:func:`_spy`'s) sent through ``query()``, after checking that nothing else was sent: no
    ``version()``, and a ``query_raw()`` only for those statements (the SDK's ``query()`` runs it itself)."""
    statements = [args[0] for method, args in calls if method == "query"]
    assert [method for method, _ in calls if method == "version"] == []
    assert [args[0] for method, args in calls if method == "query_raw"] == statements
    return statements


def _field_states(info: Mapping[str, Any]) -> dict[tuple[str, ...], FieldState]:
    """The fields of an ``INFO FOR TABLE`` answer, each parsed, keyed by path."""
    states = [parse_define_field(text).state for text in info["fields"].values()]
    return {tuple(state.path): state for state in states}


def _clauses(state: TableState) -> tuple[object, ...]:
    """The clauses of a stored table a model without keys keeps: ``DROP``, mode, comment, change feed, permissions."""
    return (
        state.drop,
        state.schema_mode,
        state.comment,
        state.changefeed,
        state.changefeed_include_original,
        state.permissions,
    )


async def _define_table_entries(client: Any, table: str) -> int:
    """How many ``define_table`` entries the change feed of *table* holds."""
    entries = await client.query(f"SHOW CHANGES FOR TABLE {table} SINCE 0 LIMIT 100;", {})
    return [kind for entry in entries for change in entry["changes"] for kind in change].count("define_table")


class TestDefineTableE2E:
    """On every engine of both lines: on a missing table ``define_table()`` reads ``INFO FOR DB`` once and applies
    ``table_ddl()``'s statements, returning them joined with a newline; a second call reads the table's fields too,
    sends the same statements and changes nothing. ``overwrite=False`` defines a missing table and leaves one that
    exists as it is, ``FLEXIBLE`` following its stored mode."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_missing_table(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _tdl(kind) as client:
            calls = _spy(monkeypatch, client)
            result = await TdlPerson.define_table()
            assert isinstance(result, str)
            assert result == "\n".join(_G1) == "\n".join(TdlPerson.table_ddl())
            assert _sent(calls) == [_INFO, *_G1]

            tables = await _tables(client)
            state = parse_define_table(tables["tdl_person"])
            assert (state.table_type, state.schema_mode, state.comment) == ("normal", "SCHEMAFULL", "People")
            assert _feed(state.changefeed) == _feed("7d")
            assert state.permissions == schema.permissions_rules(TdlPerson.model_config["permissions"], model=TdlPerson)
            info = await _table_info(client, "tdl_person")
            stored = _field_states(info)
            for column, statement in zip(_columns(TdlPerson), _G1[1:], strict=True):
                field = stored[column.path]
                assert (field.field_type, field.nullable) == normalise_kind(render_kind(column.kind)), statement
                assert field.flexible is (" FLEXIBLE" in statement), statement

            calls.clear()
            assert await TdlPerson.define_table() == result
            assert _sent(calls) == [_INFO, _INFO_PERSON, *_G1]
            assert await _tables(client) == tables
            assert await _table_info(client, "tdl_person") == info

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_overwrite_false_on_a_missing_table(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _tdl(kind) as client:
            calls = _spy(monkeypatch, client)
            assert await TdlPerson.define_table(overwrite=False) == "\n".join(_if_not_exists(_G1))
            assert _sent(calls) == [_INFO, *_if_not_exists(_G1)]
            state = parse_define_table((await _tables(client))["tdl_person"])
            assert (state.schema_mode, state.comment) == ("SCHEMAFULL", "People")

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_overwrite_false_leaves_an_existing_table(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """``IF NOT EXISTS``: the table and its stored field stay as they are, a new column is defined — ``FLEXIBLE``
        on a free-form object, since the stored table is ``SCHEMAFULL``."""
        async with _tdl(kind) as client:
            await _apply_ddl(
                client,
                "DEFINE TABLE tdl_note SCHEMAFULL COMMENT 'keep'; DEFINE FIELD body ON tdl_note TYPE string ASSERT $value != '';",
            )
            before = (await _tables(client))["tdl_note"]
            body = (await _table_info(client, "tdl_note"))["fields"]["body"]
            statements = [
                "DEFINE TABLE IF NOT EXISTS `tdl_note` TYPE NORMAL;",
                "DEFINE FIELD IF NOT EXISTS `body` ON `tdl_note` TYPE string;",
                "DEFINE FIELD IF NOT EXISTS `meta` ON `tdl_note` TYPE object FLEXIBLE;",
            ]
            calls = _spy(monkeypatch, client)
            assert await TdlNoteMeta.define_table(overwrite=False) == "\n".join(statements)
            assert _sent(calls) == [_INFO, *statements]
            assert (await _tables(client))["tdl_note"] == before
            fields = (await _table_info(client, "tdl_note"))["fields"]
            assert fields["body"] == body
            meta = parse_define_field(fields["meta"]).state
            assert (meta.field_type, meta.flexible) == ("object", True)


#: The table tuned by hand (no ``DROP``: a ``DROP`` table stores no record): its clauses, the fields of K3, K4 and
#: K5 and ``body``, an index, an event, a view that reads it and a record.
_AC9_FIXTURE = (
    "DEFINE TABLE tdl_note TYPE ANY SCHEMAFULL COMMENT 'keep' CHANGEFEED 1h INCLUDE ORIGINAL "
    "PERMISSIONS FOR select WHERE owner = $auth.id, FOR create FULL; "
    "DEFINE FIELD email ON tdl_note TYPE string ASSERT $value != '' "
    "PERMISSIONS FOR select WHERE $auth.id = id, FOR create, update FULL COMMENT 'mail'; "
    "DEFINE FIELD created ON tdl_note TYPE option<datetime> DEFAULT time::now() READONLY; "
    "DEFINE FIELD done ON tdl_note TYPE bool DEFAULT true COMMENT 'd'; "
    "DEFINE FIELD body ON tdl_note TYPE string; "
    "DEFINE INDEX tdl_note_body ON tdl_note FIELDS body; "
    "DEFINE EVENT tdl_note_ev ON tdl_note WHEN $event = 'CREATE' THEN {}; "
    "DEFINE TABLE tdl_note_count AS SELECT count() AS n FROM tdl_note GROUP ALL; "
    "CREATE tdl_note:n1 SET body = 'b', email = 'e@x.y';"
)


async def _tune_note(client: Any) -> TdlNoteTuned:
    """The table tuned by hand on ``tdl_db`` (each statement checked); the stored table reads as ``_STORED_NOTE``. Returns the
    record ``n1`` read through the model."""
    await _apply_ddl(client, _AC9_FIXTURE)
    assert (await _tables(client))["tdl_note"] == _STORED_NOTE
    assert await client.query("SELECT VALUE n FROM tdl_note_count;", {}) == [1]
    return await TdlNoteTuned.objects().get("n1")


class TestExistingTableE2E:
    """On every engine of both lines: over a table tuned by hand ``define_table()`` sends the carried statements —
    the model's type and column kinds, every clause and field clause it does not declare kept —, and the table keeps
    its index, event, the view that reads it, its record, its live query and its change-feed history (plus one
    ``define_table`` entry); declared clauses win; a ``server_fields`` column keeps the database's ``DEFAULT``; a
    ``DROP`` table keeps its ``DROP``."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_table_tuned_by_hand(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _tdl(kind) as client:
            n1 = await _tune_note(client)
            live_id: Any = None
            if kind == "ws":
                response = await client.query_raw("LIVE SELECT * FROM tdl_note;", {})
                [result] = response["result"]
                assert result["status"] == "OK", result
                live_id = result["result"]
            try:
                before = parse_define_table((await _tables(client))["tdl_note"])
                info_before = await _table_info(client, "tdl_note")
                fields_before = _field_states(info_before)
                feed_before = await _define_table_entries(client, "tdl_note")

                calls = _spy(monkeypatch, client)
                assert await TdlNoteTuned.define_table() == "\n".join(_TUNED)
                assert _sent(calls) == [_INFO, _INFO_NOTE, *_TUNED]

                tables = await _tables(client)
                after = parse_define_table(tables["tdl_note"])
                assert after.table_type == "normal"
                assert _clauses(after) == _clauses(before)
                info = await _table_info(client, "tdl_note")
                fields = _field_states(info)
                for column in _columns(TdlNoteTuned):
                    field = fields[column.path]
                    assert (field.field_type, field.nullable) == normalise_kind(render_kind(column.kind)), column.path
                email, email_before = fields[("email",)], fields_before[("email",)]
                assert (email.assertion, email.permissions, email.comment) == (
                    email_before.assertion,
                    email_before.permissions,
                    email_before.comment,
                )
                created, created_before = fields[("created",)], fields_before[("created",)]
                assert (created.default, created.readonly) == (created_before.default, created_before.readonly)
                assert (fields[("done",)].default, fields[("done",)].comment) == ("false", "d")
                assert info["indexes"] == info_before["indexes"]
                assert list(info["indexes"]) == ["tdl_note_body"]
                assert info["events"] == info_before["events"]
                assert list(info["events"]) == ["tdl_note_ev"]
                assert info["lives"] == info_before["lives"]
                assert len(info["lives"]) == (1 if kind == "ws" else 0)
                assert await _define_table_entries(client, "tdl_note") == feed_before + 1

                assert (await TdlNoteTuned.objects().get("n1")).model_dump() == n1.model_dump()
                assert parse_define_table(tables["tdl_note_count"]).view_query is not None
                await TdlNoteTuned(id="n2", body="c").save()
                assert await client.query("SELECT VALUE n FROM tdl_note_count;", {}) == [2]
            finally:
                if live_id is not None:
                    with contextlib.suppress(Exception):
                        await client.kill(live_id)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_declared_clauses_win(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """K2E: the declared mode, comment and permissions replace the stored ones; the change feed is kept."""
        async with _tdl(kind) as client:
            await _tune_note(client)
            calls = _spy(monkeypatch, client)
            assert await TdlNoteMine.define_table() == "\n".join([_K2E, *_TUNED[1:]])
            assert _sent(calls) == [_INFO, _INFO_NOTE, _K2E, *_TUNED[1:]]
            state = parse_define_table((await _tables(client))["tdl_note"])
            assert (state.table_type, state.schema_mode, state.comment) == ("normal", "SCHEMALESS", "mine")
            assert (_feed(state.changefeed), state.changefeed_include_original) == (_feed("1h"), True)
            assert state.permissions == schema.permissions_rules({"select": "FULL"}, model=TdlNoteMine)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_k11_a_server_column_keeps_the_databases_default(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _tdl(kind) as client:
            await _apply_ddl(client, "DEFINE TABLE tdl_note; DEFINE FIELD status ON tdl_note TYPE string DEFAULT 'new';")
            statements = ["DEFINE TABLE OVERWRITE `tdl_note` TYPE NORMAL;", _BODY, _K11]
            calls = _spy(monkeypatch, client)
            assert await TdlNoteStatus.define_table() == "\n".join(statements)
            assert _sent(calls) == [_INFO, _INFO_NOTE, *statements]
            status = _field_states(await _table_info(client, "tdl_note"))[("status",)]
            assert status.default == "'new'"
            await TdlNoteStatus(id="s1", body="b").save()
            assert await client.query("SELECT VALUE status FROM tdl_note:s1;", {}) == ["new"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_k1d_a_drop_table(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _tdl(kind) as client:
            await _apply_ddl(client, "DEFINE TABLE tdl_drop TYPE ANY DROP SCHEMALESS COMMENT 'd' PERMISSIONS NONE;")
            assert (await _tables(client))["tdl_drop"] == _STORED_DROP
            calls = _spy(monkeypatch, client)
            assert await TdlDrop.define_table() == "\n".join(_K1D)
            assert _sent(calls) == [_INFO, "INFO FOR TABLE `tdl_drop`;", *_K1D]
            state = parse_define_table((await _tables(client))["tdl_drop"])
            assert (state.table_type, state.drop, state.schema_mode, state.comment) == ("normal", True, "SCHEMALESS", "d")


#: The skip reason of the ``COMPUTED`` tests before SurrealDB 3.
_COMPUTED_SKIP = "SurrealDB 2.x has no COMPUTED fields"

#: README section 35's sentence on ``COMPUTED`` fields, which the pin backs.
_COMPUTED_SENTENCE = (
    "README section 35, Field types: A computed field is written `VALUE`; over a field the database defines `COMPUTED` "
    "(SurrealDB 3.x), `define_table()` writes `COMPUTED`: replacing it by `VALUE` would leave the stored records "
    "without the field until their next write."
)

#: K9's stored form on ``tdl_cart``, with a record.
_CART_FIXTURE = (
    "DEFINE TABLE tdl_cart; "
    "DEFINE FIELD items ON tdl_cart TYPE array<string>; "
    "DEFINE FIELD total ON tdl_cart TYPE option<int> COMPUTED count(items) COMMENT 'c' "
    "PERMISSIONS FOR select WHERE true, FOR create, update FULL; "
    "CREATE tdl_cart:c1 SET items = ['a', 'b'];"
)


async def _total(client: Any) -> list[Any]:
    """``total`` of the record ``tdl_cart:c1``."""
    total: list[Any] = await client.query("SELECT VALUE total FROM tdl_cart:c1;", {})
    return total


class TestComputedE2E:
    """SurrealDB 3.x (skipped before 3): a stored ``COMPUTED`` field stays ``COMPUTED`` — with the model's
    expression, its permissions and comment kept, no ``DEFAULT`` — and its records keep their value; ``VALUE`` over it
    would empty them (the pin)."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_computed_field_stays_computed(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _tdl(kind) as client:
            if await _major(client) < 3:
                pytest.skip(_COMPUTED_SKIP)
            await _apply_ddl(client, _CART_FIXTURE)
            assert await _total(client) == [2]
            statements = [
                "DEFINE TABLE OVERWRITE `tdl_cart` TYPE NORMAL;",
                "DEFINE FIELD OVERWRITE `items` ON `tdl_cart` TYPE array<string>;",
                _K9,
            ]
            calls = _spy(monkeypatch, client)
            assert await TdlCart.define_table() == "\n".join(statements)
            assert _sent(calls) == [_INFO, "INFO FOR TABLE `tdl_cart`;", *statements]
            total = _field_states(await _table_info(client, "tdl_cart"))[("total",)]
            assert (total.computed, total.value, total.default, total.comment) == ("count(items)", None, None, "c")
            assert total.permissions == {"select": "WHERE true", "create": "FULL", "update": "FULL"}
            assert await _total(client) == [2]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_value_over_computed_empties_the_records(self, kind: str) -> None:
        """A raw ``DEFINE FIELD OVERWRITE … VALUE`` over a ``COMPUTED`` field leaves the stored record without it."""
        async with _tdl(kind) as client:
            if await _major(client) < 3:
                pytest.skip(_COMPUTED_SKIP)
            await _apply_ddl(client, _CART_FIXTURE)
            await _apply_ddl(client, "DEFINE FIELD OVERWRITE total ON tdl_cart TYPE option<int> VALUE count(items);")
            assert await _total(client) == [None], _COMPUTED_SENTENCE

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_k10_a_stored_computed_without_default(self, kind: str) -> None:
        """K10: the stored ``COMPUTED`` is kept and no ``DEFAULT`` is written, which SurrealDB refuses with it."""
        async with _tdl(kind) as client:
            if await _major(client) < 3:
                pytest.skip(_COMPUTED_SKIP)
            await _apply_ddl(client, "DEFINE TABLE tdl_note; DEFINE FIELD age ON tdl_note TYPE int COMPUTED 18;")
            assert await TdlNoteAge.define_table() == "\n".join(["DEFINE TABLE OVERWRITE `tdl_note` TYPE NORMAL;", _K10])
            age = _field_states(await _table_info(client, "tdl_note"))[("age",)]
            assert (age.computed, age.default) == ("18", None)
            await _apply_ddl(client, "CREATE tdl_note:a1;")
            assert await client.query("SELECT VALUE age FROM tdl_note:a1;", {}) == [18]


class TdlStatsNode(BaseSurrealModel):
    """A node model on the table of a view."""

    model_config = SurrealConfigDict(table_name="tdl_stats", connection="tdl")

    id: str | None = None
    n: int = 0


class TestTableGuardE2E:
    """On every engine of both lines: a node model refuses a view (TG1) and a relation table (TG3) with either
    ``overwrite``, after ``INFO FOR DB`` alone; the table is left as it is."""

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize("overwrite", [True, False], ids=["overwrite", "if-not-exists"])
    async def test_tg1_a_view(self, kind: str, overwrite: bool, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _tdl(kind) as client:
            await _apply_ddl(client, f"CREATE tdl_note:n1 SET body = 'x'; DEFINE TABLE tdl_stats AS {_VIEW_QUERY};")
            before = (await _tables(client))["tdl_stats"]
            calls = _spy(monkeypatch, client)
            with pytest.raises(SurrealDbError) as raised:
                await TdlStatsNode.define_table(overwrite=overwrite)
            assert _sent(calls) == [_INFO]
            assert (type(raised.value), str(raised.value)) == (
                SurrealDbError,
                _tg1("TdlStatsNode", "define_table", "tdl_stats"),
            )
            assert (await _tables(client))["tdl_stats"] == before
            assert await client.query("SELECT VALUE n FROM tdl_stats;", {}) == [1]

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize("overwrite", [True, False], ids=["overwrite", "if-not-exists"])
    async def test_tg3_a_relation_table(self, kind: str, overwrite: bool, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _tdl(kind) as client:
            await _apply_ddl(client, "DEFINE TABLE tdl_note TYPE RELATION IN tdl_person OUT tdl_person;")
            before = (await _tables(client))["tdl_note"]
            calls = _spy(monkeypatch, client)
            with pytest.raises(SurrealDbError) as raised:
                await TdlNote.define_table(overwrite=overwrite)
            assert _sent(calls) == [_INFO]
            assert (type(raised.value), str(raised.value)) == (SurrealDbError, _tg3("TdlNote", "define_table", "tdl_note"))
            assert (await _tables(client))["tdl_note"] == before
            assert parse_define_table(before).table_type == "relation"


#: A record access on ``tdl_db``: a user signed up through it is a record user, who may not define.
_TDL_ACCESS = (
    "DEFINE ACCESS tdl_acc ON DATABASE TYPE RECORD SIGNUP (CREATE tdl_user SET name = $name) "
    "SIGNIN (SELECT * FROM tdl_user WHERE name = $name);"
)


class TestTablePermissionsE2E:
    """On the servers: a record user may not define a table — the refusal of the first read, ``INFO FOR DB``, and
    nothing else is sent. The embedded engine's session is its owner, which may always define."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_s1_a_record_user(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        if kind == "mem":
            pytest.skip("the embedded engine's session is its owner: it may always define")
        async with _tdl(kind) as client, SurrealDBConnectionManager.using("tdl"):
            await _apply_ddl(client, _TDL_ACCESS)
            await SurrealDBConnectionManager.signup(access="tdl_acc", variables={"name": "tdl-user"})
            try:
                calls = _spy(monkeypatch, client)
                with pytest.raises(SurrealDbError) as raised:
                    await TdlNote.define_table()
                assert _sent(calls) == [_INFO]
            finally:
                await SurrealDBConnectionManager.invalidate()
            assert "tdl_note" not in await _tables(client)
        assert type(raised.value) is SurrealDbAuthenticationError
        assert str(raised.value).startswith("Can't apply table definition: 'tdl_note' -> ")
        assert "not enough permissions" in str(raised.value).lower()


# --- edge and view models: the clauses they declare -----------------------------------------------------------------


class TdlLikes(BaseSurrealEdge):
    """G7: an edge model declaring ``SCHEMAFULL`` and permissions, with a data field."""

    model_config = SurrealConfigDict(
        table_name="tdl_likes",
        relation_in="tdl_person",
        relation_out="tdl_note",
        schema_mode=SchemaMode.SCHEMAFULL,
        permissions={"select": "FULL"},
        connection="tdl",
    )

    stars: int = 1


class TdlFollows(BaseSurrealEdge):
    """G7b: an edge model declaring a comment and permissions, and no schema mode."""

    model_config = SurrealConfigDict(
        table_name="tdl_follows",
        relation_in="tdl_person",
        relation_out="tdl_person",
        comment="Follows",
        permissions={"select": "FULL"},
        connection="tdl",
    )

    since: int = 0


class TdlStats(BaseSurrealModel):
    """G8: a view model declaring a comment and permissions."""

    model_config = SurrealConfigDict(
        table_name="tdl_stats", view_query=_VIEW_QUERY, comment="Counts", permissions={"select": "FULL"}, connection="tdl"
    )

    n: int = 0


class TdlStatsOwned(BaseSurrealModel):
    """A view model whose permission rule the refusal of its statement may quote."""

    model_config = SurrealConfigDict(
        table_name="tdl_stats",
        view_query=_VIEW_QUERY,
        permissions={"select": "WHERE $auth.id = owner_of_view"},
        connection="tdl",
    )

    n: int = 0


class TdlStatsClosed(BaseSurrealModel):
    """A view model declaring ``permissions={}``: its statement writes ``PERMISSIONS NONE``."""

    model_config = SurrealConfigDict(table_name="tdl_stats", view_query=_VIEW_QUERY, permissions={}, connection="tdl")

    n: int = 0


class TdlPersonNode(BaseSurrealModel):
    """A node of ``tdl_person``, the ``IN`` table of the edge models."""

    model_config = SurrealConfigDict(table_name="tdl_person", connection="tdl")

    id: str | None = None


_G7 = [
    "DEFINE TABLE OVERWRITE `tdl_likes` TYPE RELATION IN `tdl_person` OUT `tdl_note` SCHEMAFULL PERMISSIONS FOR select "
    "FULL, FOR create, update, delete NONE;",
    "DEFINE FIELD OVERWRITE `stars` ON `tdl_likes` TYPE int DEFAULT 1;",
]
_G7B = [
    "DEFINE TABLE OVERWRITE `tdl_follows` TYPE RELATION IN `tdl_person` OUT `tdl_person` COMMENT 'Follows' PERMISSIONS "
    "FOR select FULL, FOR create, update, delete NONE;",
    "DEFINE FIELD OVERWRITE `since` ON `tdl_follows` TYPE int DEFAULT 0;",
]
_G8 = (
    f"DEFINE TABLE OVERWRITE `tdl_stats` AS ({_VIEW_QUERY}) COMMENT 'Counts' PERMISSIONS FOR select FULL, FOR create, "
    "update, delete NONE;"
)

#: RG4 for ``TdlLikes``.
_RG4 = (
    "TdlLikes.define_relation(): TdlLikes declares SchemaMode.SCHEMAFULL and has data fields ('stars'); define_relation() "
    "defines no field, and a SCHEMAFULL table refuses (SurrealDB 3.x) or drops (2.x and the embedded engine) the fields "
    "it does not define. Call TdlLikes.define_table(), which applies the same relation statement and defines them. "
    "Nothing was defined."
)

_INFO_LIKES = "INFO FOR TABLE `tdl_likes`;"

#: A relation table ``tdl_likes`` as SurrealDB renders it, with a change feed and a comment; its fields; and the
#: carried statements ``TdlLikes.define_table()`` sends over it.
_STORED_LIKES = (
    "DEFINE TABLE tdl_likes TYPE RELATION IN tdl_person OUT tdl_note SCHEMALESS COMMENT 'keep' CHANGEFEED 1h PERMISSIONS NONE"
)
_STORED_LIKES_FIELDS = {
    "in": "DEFINE FIELD in ON tdl_likes TYPE record<tdl_person> PERMISSIONS FULL",
    "out": "DEFINE FIELD out ON tdl_likes TYPE record<tdl_note> PERMISSIONS FULL",
    "stars": "DEFINE FIELD stars ON tdl_likes TYPE int DEFAULT 1 COMMENT 's' PERMISSIONS FULL",
}
_LIKES_CARRIED = [
    "DEFINE TABLE OVERWRITE `tdl_likes` TYPE RELATION IN `tdl_person` OUT `tdl_note` SCHEMAFULL COMMENT 'keep' CHANGEFEED "
    "1h PERMISSIONS FOR select FULL, FOR create, update, delete NONE;",
    "DEFINE FIELD OVERWRITE `stars` ON `tdl_likes` TYPE int DEFAULT 1 COMMENT 's';",
]
_STORED_LIKES_VIEW = (
    "DEFINE TABLE tdl_likes TYPE ANY SCHEMALESS AS SELECT count() AS n FROM tdl_note GROUP ALL PERMISSIONS NONE"
)

#: A relation table ``tdl_follows`` set by hand, and what ``TdlFollows.define_relation()`` sends over it.
_STORED_FOLLOWS = (
    "DEFINE TABLE tdl_follows TYPE RELATION IN tdl_person OUT tdl_person SCHEMALESS COMMENT 'old' CHANGEFEED 1h "
    "PERMISSIONS FULL"
)
_FOLLOWS_CARRIED = (
    "DEFINE TABLE OVERWRITE `tdl_follows` TYPE RELATION IN `tdl_person` OUT `tdl_person` COMMENT 'Follows' CHANGEFEED 1h "
    "PERMISSIONS FOR select FULL, FOR create, update, delete NONE;"
)

#: K8's stored view with the clauses ``TdlStats`` declares instead of the stored ones.
_K8_DECLARED = (
    f"DEFINE TABLE OVERWRITE `tdl_stats` DROP SCHEMAFULL AS ({_VIEW_QUERY}) COMMENT 'Counts' CHANGEFEED 1h PERMISSIONS "
    "FOR select FULL, FOR create, update, delete NONE;"
)


def _g1(name: str, table: str) -> str:
    """G1 of ``define_view()`` (v0.32.0): an ordinary table holding records under a view model."""
    return (
        f"{name}.define_view(): the table '{table}' exists, is not a view and holds records; defining the view would "
        "delete them, with the table's fields, indexes and events. If they are not needed, remove the table first "
        f'(await {name}.raw_query("REMOVE TABLE `{table}`;")); otherwise give the model another table_name. Nothing was '
        "defined."
    )


class TestEdgeAndViewDdl:
    """An edge model's ``relation_ddl()`` and a view model's ``view_ddl()`` write the clauses it declares, as its
    ``table_ddl()`` does; ``define_relation()`` of an edge model declaring ``SCHEMAFULL`` with data fields is refused
    before any request; ``define_table()`` of an edge applies its relation statement, guarded as ``define_relation()``'s
    and naming ``define_table()``, then its data fields; of a view model, ``define_view()``'s statement."""

    def test_g7_an_edge_declaring_schemafull(self) -> None:
        assert TdlLikes.relation_ddl() == _G7[0]
        assert TdlLikes.table_ddl() == _G7
        assert TdlLikes.relation_ddl(overwrite=False) == _if_not_exists(_G7)[0]
        assert TdlLikes.table_ddl(overwrite=False) == _if_not_exists(_G7)

    def test_g7b_an_edge_without_schema_mode(self) -> None:
        assert TdlFollows.relation_ddl() == _G7B[0]
        assert TdlFollows.table_ddl() == _G7B
        assert TdlFollows.table_ddl(overwrite=False) == _if_not_exists(_G7B)

    def test_g8_a_view(self) -> None:
        assert TdlStats.view_ddl() == _G8
        assert TdlStats.table_ddl() == [_G8]
        assert TdlStats.view_ddl(overwrite=False) == _if_not_exists([_G8])[0]
        assert TdlStats.table_ddl(overwrite=False) == _if_not_exists([_G8])

    def test_without_the_keys_unchanged(self) -> None:
        """v0.33.0's and v0.32.0's statements for models without the keys."""
        assert TdlLikesBare.relation_ddl() == (
            "DEFINE TABLE OVERWRITE `tdl_likes_bare` TYPE RELATION IN `tdl_person` OUT `tdl_post` | `tdl_book` ENFORCED;"
        )
        assert TdlLink.relation_ddl(overwrite=False) == "DEFINE TABLE IF NOT EXISTS `tdl_link` TYPE RELATION;"
        assert TdlStatsBare.view_ddl() == f"DEFINE TABLE OVERWRITE `tdl_stats` AS ({_VIEW_QUERY});"
        assert TdlStatsClosed.view_ddl() == f"DEFINE TABLE OVERWRITE `tdl_stats` AS ({_VIEW_QUERY}) PERMISSIONS NONE;"

    @pytest.mark.parametrize("overwrite", [True, False], ids=["overwrite", "if-not-exists"])
    async def test_rg4_before_any_request(self, use_client: Callable[[Any], None], overwrite: bool) -> None:
        client = _TableClient()
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await TdlLikes.define_relation(overwrite=overwrite)
        assert (type(raised.value), str(raised.value)) == (SurrealDbError, _RG4)
        assert client.queries == []

    async def test_rg4_names_each_top_level_data_column(self, use_client: Callable[[Any], None]) -> None:
        """In declaration order, each column under its name in the table (an alias), never a sub-model's field."""

        class TdlRated(BaseSurrealEdge):
            model_config = SurrealConfigDict(table_name="tdl_rated", schema_mode=SchemaMode.SCHEMAFULL, connection="tdl")

            stars: int = 1
            note: str = Field(default="", alias="the-note")
            where: TdlAddress | None = None

        client = _TableClient()
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await TdlRated.define_relation()
        assert "has data fields ('stars', 'the-note', 'where');" in str(raised.value)
        assert client.queries == []

    async def test_no_rg4_without_a_data_column_or_a_declared_mode(self, use_client: Callable[[Any], None]) -> None:
        """A ``SCHEMAFULL`` edge without data columns, and an edge that does not declare its mode, reach the read."""

        class TdlBareFull(BaseSurrealEdge):
            model_config = SurrealConfigDict(table_name="tdl_bare_full", schema_mode=SchemaMode.SCHEMAFULL, connection="tdl")

        client = _TableClient()
        use_client(client)
        statement = "DEFINE TABLE OVERWRITE `tdl_bare_full` TYPE RELATION SCHEMAFULL;"
        assert await TdlBareFull.define_relation() == statement
        assert client.queries == [_INFO, statement]

        client = _TableClient()
        use_client(client)
        assert await TdlFollows.define_relation() == _G7B[0]
        assert client.queries == [_INFO, _G7B[0]]

    async def test_define_relation_keeps_what_the_model_does_not_declare(self, use_client: Callable[[Any], None]) -> None:
        """The declared comment and permissions replace the stored ones; the change feed is kept."""
        client = _TableClient({"tdl_follows": _STORED_FOLLOWS})
        use_client(client)
        assert await TdlFollows.define_relation() == _FOLLOWS_CARRIED
        assert client.queries == [_INFO, _FOLLOWS_CARRIED]

    async def test_define_table_of_an_edge_on_a_missing_table(self, use_client: Callable[[Any], None]) -> None:
        """The relation statement, then the data fields — never ``in``/``out`` —, joined with a newline."""
        client = _TableClient()
        use_client(client)
        result = await TdlLikes.define_table()
        assert isinstance(result, str)
        assert result == "\n".join(_G7) == "\n".join(TdlLikes.table_ddl())
        assert client.queries == [_INFO, *_G7]
        assert client.version_calls == 0

    async def test_define_table_of_an_edge_on_a_relation_table(self, use_client: Callable[[Any], None]) -> None:
        """The carried relation statement, then each data field over its stored definition; ``in``/``out`` left as
        SurrealDB defines them."""
        client = _TableClient({"tdl_likes": _STORED_LIKES}, _STORED_LIKES_FIELDS)
        use_client(client)
        assert await TdlLikes.define_table() == "\n".join(_LIKES_CARRIED)
        assert client.queries == [_INFO, _INFO_LIKES, *_LIKES_CARRIED]

    async def test_define_table_of_an_edge_if_not_exists(self, use_client: Callable[[Any], None]) -> None:
        """``overwrite=False`` over a relation table: ``IF NOT EXISTS`` statements, no field read."""
        client = _TableClient({"tdl_likes": _STORED_LIKES}, _STORED_LIKES_FIELDS)
        use_client(client)
        assert await TdlLikes.define_table(overwrite=False) == "\n".join(_if_not_exists(_G7))
        assert client.queries == [_INFO, *_if_not_exists(_G7)]

    @pytest.mark.parametrize(
        ("stored", "overwrite", "guard"),
        [
            (_STORED_LIKES_VIEW, True, "RG1"),
            (_STORED_LIKES_VIEW, False, "RG1"),
            ("DEFINE TABLE tdl_likes TYPE", True, "RG2"),
            (3, False, "RG2"),
            ("DEFINE TABLE tdl_likes TYPE ANY SCHEMALESS PERMISSIONS NONE", False, "RG3"),
        ],
        ids=["rg1-overwrite", "rg1-if-not-exists", "rg2-unparsable", "rg2-not-a-str", "rg3"],
    )
    async def test_rg1_to_rg3_name_define_table(
        self, use_client: Callable[[Any], None], stored: object, overwrite: bool, guard: str
    ) -> None:
        client = _TableClient({"tdl_likes": stored})
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await TdlLikes.define_table(overwrite=overwrite)
        assert (type(raised.value), str(raised.value)) == (SurrealDbError, _RG_DEFINE_TABLE[guard])
        assert client.queries == [_INFO]

    async def test_rg2_a_carried_rule_through_define_table(self, use_client: Callable[[Any], None]) -> None:
        class TdlLikes(BaseSurrealEdge):
            model_config = SurrealConfigDict(table_name="tdl_likes", connection="tdl")

        client = _TableClient(
            {
                "tdl_likes": "DEFINE TABLE tdl_likes TYPE ANY SCHEMALESS PERMISSIONS FOR select WHERE a; b, FOR create, "
                "update, delete NONE"
            }
        )
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await TdlLikes.define_table()
        assert str(raised.value) == _RG_DEFINE_TABLE["RG2"]
        with pytest.raises(SurrealDbError) as relation:
            await TdlLikes.define_relation()
        assert str(relation.value) == _RG_DEFINE_RELATION["RG2"]
        assert client.queries == [_INFO, _INFO]

    async def test_define_table_of_a_view_model(self, use_client: Callable[[Any], None]) -> None:
        """``define_view()``'s statement, a ``str``; over a stored view, its carried form."""
        client = _TableClient()
        use_client(client)
        result = await TdlStats.define_table()
        assert isinstance(result, str)
        assert result == _G8
        assert client.queries == [_INFO, _G8]

        client = _TableClient({"tdl_stats": _STORED_VIEW})
        use_client(client)
        assert await TdlStatsBare.define_table() == _K8 == await TdlStatsBare.define_view()
        assert client.queries == [_INFO, _K8] * 2

    @pytest.mark.parametrize("model", [TdlLikes, TdlStats], ids=["edge", "view"])
    async def test_a_transaction_of_another_connection(
        self, use_client: Callable[[Any], None], model: type[BaseSurrealModel]
    ) -> None:
        client = _TableClient()
        use_client(client)
        tx = _QueuingTx("other")
        with pytest.raises(SurrealDbError) as raised:
            await model.define_table(tx=tx)  # type: ignore[arg-type]
        assert str(raised.value).startswith(f"{model.__name__}.define_table() cannot run in this transaction: ")
        assert client.queries == []
        assert tx.added == []

    async def test_a_transaction_gets_the_edges_ddl_only(self, use_client: Callable[[Any], None]) -> None:
        client = _TableClient({"tdl_likes": _STORED_LIKES}, _STORED_LIKES_FIELDS)
        use_client(client)
        tx = _QueuingTx()
        assert await TdlLikes.define_table(tx=tx) == "\n".join(_LIKES_CARRIED)  # type: ignore[arg-type]
        assert client.queries == [_INFO, _INFO_LIKES]
        assert tx.added == [(statement, None) for statement in _LIKES_CARRIED]


#: A server's reason quoting the view's query: a run of the statement's text.
_VIEW_REASON = f"Found a problem in {_VIEW_QUERY}"
_LEFT_OUT = "the server's message quotes the statement, so it is left out"


class TestViewRefusal:
    """A refusal of ``define_view()`` quoting its statement is left out exactly when the statement writes
    ``PERMISSIONS`` — declared (``permissions={}`` included) or kept from the stored view —, so a permission rule is
    never quoted; otherwise v0.32.0's whole reason, which names the view's source tables."""

    async def _refusal(
        self, use_client: Callable[[Any], None], model: type[BaseSurrealModel], reason: str, stored: str | None = None
    ) -> tuple[SurrealDbError, BaseException, list[str]]:
        error = InternalError("Internal", reason)
        client = _TableClient({} if stored is None else {"tdl_stats": stored}, ddl_error=error)
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await model.define_view()
        return raised.value, error, client.queries

    async def test_a_declared_rule_is_never_quoted(self, use_client: Callable[[Any], None]) -> None:
        statement = (
            f"DEFINE TABLE OVERWRITE `tdl_stats` AS ({_VIEW_QUERY}) PERMISSIONS FOR select WHERE $auth.id = owner_of_view, "
            "FOR create, update, delete NONE;"
        )
        assert TdlStatsOwned.view_ddl() == statement
        refusal, error, sent = await self._refusal(
            use_client, TdlStatsOwned, "Found a problem in PERMISSIONS FOR select WHERE $auth.id = owner_of_view"
        )
        assert type(refusal) is SurrealDbError
        assert str(refusal) == f"Can't apply view definition: 'tdl_stats' -> {_LEFT_OUT}"
        assert "owner_of_view" not in str(refusal)
        assert refusal.__context__ is error
        assert sent == [_INFO, statement]

    @pytest.mark.parametrize(
        ("model", "stored", "redacted"),
        [
            (TdlStatsOwned, None, True),
            (TdlStatsClosed, None, True),
            (TdlStatsBare, None, False),
            (
                TdlStatsBare,
                "DEFINE TABLE tdl_stats TYPE ANY SCHEMALESS AS SELECT count() AS n FROM tdl_note GROUP ALL PERMISSIONS NONE",
                False,
            ),
            (TdlStatsBare, _STORED_VIEW, True),
            (TdlStatsClosed, _STORED_VIEW, True),
        ],
        ids=["declared-rule", "declared-none", "none", "stored-without", "stored-with", "declared-none-over-stored"],
    )
    async def test_redacted_when_the_statement_writes_permissions(
        self, use_client: Callable[[Any], None], model: type[BaseSurrealModel], stored: str | None, redacted: bool
    ) -> None:
        refusal, error, sent = await self._refusal(use_client, model, _VIEW_REASON, stored)
        assert type(refusal) is SurrealDbError
        if redacted:
            assert str(refusal) == f"Can't apply view definition: 'tdl_stats' -> {_LEFT_OUT}"
            assert refusal.__context__ is error
        else:
            assert str(refusal) == f"Can't apply view definition: 'tdl_stats' -> {_VIEW_REASON}"
            assert refusal.__cause__ is error
        assert sent[0] == _INFO and ("PERMISSIONS" in sent[1]) is redacted

    async def test_g3_a_stored_view_whose_clause_fails_its_check(self, use_client: Callable[[Any], None]) -> None:
        """A carried rule that fails its check: v0.32.0's G3, nothing defined; a declared rule replaces it."""
        stored = (
            "DEFINE TABLE tdl_stats TYPE ANY SCHEMALESS AS SELECT count() AS n FROM tdl_note GROUP ALL PERMISSIONS FOR "
            "select WHERE a; b, FOR create, update, delete NONE"
        )
        client = _TableClient({"tdl_stats": stored})
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await TdlStatsBare.define_view()
        assert str(raised.value) == (
            "TdlStatsBare.define_view(): the definition of the table 'tdl_stats' could not be read, so the ORM cannot "
            "tell whether it is a view. Nothing was defined."
        )
        assert client.queries == [_INFO]
        assert await TdlStats.define_view() == _G8
        assert client.queries == [_INFO, _INFO, _G8]


class TestEdgeAndViewE2E:
    """On every engine of both lines: G7, G7b and G8 applied raw and read back; ``define_relation()`` writes the clauses
    an edge model declares and keeps the others; ``define_table()`` of an edge model applies its relation statement and
    its data fields, then an edge carries them; ``define_view()`` keeps a view's clauses set by hand unless declared;
    ``define_table()`` of a view model is ``define_view()``."""

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize(
        ("model", "statements"), [(TdlLikes, _G7), (TdlFollows, _G7B), (TdlStats, [_G8])], ids=["G7", "G7b", "G8"]
    )
    async def test_each_golden_is_applied_and_read_back(
        self, kind: str, model: type[BaseSurrealModel], statements: list[str]
    ) -> None:
        async with _tdl(kind) as client:
            await _apply_ddl(client, "DEFINE TABLE tdl_note;")
            assert model.table_ddl() == statements
            await _apply_ddl(client, " ".join(statements))
            table = model.get_table_name()
            state = parse_define_table((await _tables(client))[table])
            assert state.schema_mode == (model.get_schema_mode() or SchemaMode.SCHEMALESS).value
            assert state.comment == model.model_config.get("comment")
            assert state.permissions == schema.permissions_rules(model.model_config["permissions"], model=model)
            if model is TdlStats:
                assert state.view_query == _VIEW_QUERY
                return
            assert issubclass(model, BaseSurrealEdge)
            assert (state.table_type, tuple(state.relation_in), tuple(state.relation_out)) == (
                "relation",
                model.get_relation_in(),
                model.get_relation_out(),
            )
            fields = _field_states(await _table_info(client, table))
            for column, statement in zip(_columns(model), statements[1:], strict=True):
                field = fields[column.path]
                assert (field.field_type, field.nullable, field.default) == (
                    *normalise_kind(render_kind(column.kind)),
                    column.default,
                ), statement

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_define_relation_writes_the_declared_clauses(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """G7b over a relation table set by hand: the declared comment and permissions written, the change feed kept."""
        async with _tdl(kind) as client:
            await _apply_ddl(
                client,
                "DEFINE TABLE tdl_follows TYPE RELATION IN tdl_person OUT tdl_person COMMENT 'old' CHANGEFEED 1h "
                "PERMISSIONS FULL;",
            )
            calls = _spy(monkeypatch, client)
            assert await TdlFollows.define_relation() == _FOLLOWS_CARRIED
            assert _sent(calls) == [_INFO, _FOLLOWS_CARRIED]
            state = parse_define_table((await _tables(client))["tdl_follows"])
            assert (state.table_type, state.schema_mode, state.comment) == ("relation", "SCHEMALESS", "Follows")
            assert (_feed(state.changefeed), state.changefeed_include_original) == (_feed("1h"), False)
            assert state.permissions == schema.permissions_rules({"select": "FULL"}, model=TdlFollows)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_rg4_sends_nothing(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _tdl(kind) as client:
            calls = _spy(monkeypatch, client)
            with pytest.raises(SurrealDbError) as raised:
                await TdlLikes.define_relation()
            assert (type(raised.value), str(raised.value)) == (SurrealDbError, _RG4)
            assert calls == []
            assert "tdl_likes" not in await _tables(client)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_define_table_of_an_edge(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """G7's relation statement and ``stars``, never ``in``/``out``; an edge keeps its data on the ``SCHEMAFULL``
        table; a second call sends the carried statements and changes nothing."""
        async with _tdl(kind) as client:
            await _apply_ddl(client, "CREATE tdl_person:p1; CREATE tdl_note:n1 SET body = 'b';")
            calls = _spy(monkeypatch, client)
            result = await TdlLikes.define_table()
            assert isinstance(result, str)
            assert result == "\n".join(_G7) == "\n".join(TdlLikes.table_ddl())
            assert _sent(calls) == [_INFO, *_G7]
            tables = await _tables(client)
            info = await _table_info(client, "tdl_likes")
            state = parse_define_table(tables["tdl_likes"])
            assert (state.table_type, state.schema_mode) == ("relation", "SCHEMAFULL")
            assert sorted(info["fields"]) == ["in", "out", "stars"]

            calls.clear()
            assert await TdlLikes.define_table() == result
            assert _sent(calls) == [_INFO, _INFO_LIKES, *_G7]
            assert await _tables(client) == tables
            assert await _table_info(client, "tdl_likes") == info

            edge = await TdlPersonNode(id="p1").relate(TdlLikes, RecordID("tdl_note", "n1"), stars=3)
            assert edge.stars == 3
            assert await client.query("SELECT VALUE stars FROM tdl_likes;", {}) == [3]

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize("overwrite", [True, False], ids=["overwrite", "if-not-exists"])
    async def test_rg1_a_view_under_an_edge(self, kind: str, overwrite: bool, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _tdl(kind) as client:
            await _apply_ddl(client, f"DEFINE TABLE tdl_note; DEFINE TABLE tdl_likes AS {_VIEW_QUERY};")
            before = (await _tables(client))["tdl_likes"]
            calls = _spy(monkeypatch, client)
            with pytest.raises(SurrealDbError) as raised:
                await TdlLikes.define_table(overwrite=overwrite)
            assert (type(raised.value), str(raised.value)) == (SurrealDbError, _RG_DEFINE_TABLE["RG1"])
            assert _sent(calls) == [_INFO]
            assert (await _tables(client))["tdl_likes"] == before

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_rg3_an_ordinary_table_with_overwrite_false(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _tdl(kind) as client:
            await _apply_ddl(client, "DEFINE TABLE tdl_likes TYPE ANY;")
            before = (await _tables(client))["tdl_likes"]
            calls = _spy(monkeypatch, client)
            with pytest.raises(SurrealDbError) as raised:
                await TdlLikes.define_table(overwrite=False)
            assert (type(raised.value), str(raised.value)) == (SurrealDbError, _RG_DEFINE_TABLE["RG3"])
            assert _sent(calls) == [_INFO]
            assert (await _tables(client))["tdl_likes"] == before

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_define_view_keeps_the_clauses_set_by_hand(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """K8: a view without keys keeps the ``DROP``, mode, comment, change feed and permissions set by hand; the
        clauses a view model declares replace them, the others kept. The source table holds no record, so the view
        holds none (SurrealDB 3.1 refuses ``OVERWRITE`` on a view that holds records)."""
        async with _tdl(kind) as client:
            await _apply_ddl(
                client,
                'DEFINE TABLE tdl_note; DEFINE TABLE tdl_stats TYPE ANY DROP SCHEMAFULL COMMENT "it\'s" CHANGEFEED 1h '
                f"PERMISSIONS FOR select WHERE true AS {_VIEW_QUERY};",
            )
            before = parse_define_table((await _tables(client))["tdl_stats"])
            assert (before.drop, before.schema_mode, before.comment, before.changefeed) == (True, "SCHEMAFULL", "it's", "1h")
            calls = _spy(monkeypatch, client)
            assert await TdlStatsBare.define_view() == _K8
            assert _sent(calls) == [_INFO, _K8]
            after = parse_define_table((await _tables(client))["tdl_stats"])
            assert after.view_query == _VIEW_QUERY
            assert _clauses(after) == _clauses(before)
            assert after.permissions == {"select": "WHERE true", "create": "NONE", "update": "NONE", "delete": "NONE"}

            calls.clear()
            assert await TdlStats.define_view() == _K8_DECLARED
            assert _sent(calls) == [_INFO, _K8_DECLARED]
            declared = parse_define_table((await _tables(client))["tdl_stats"])
            assert (declared.drop, declared.schema_mode, declared.comment) == (True, "SCHEMAFULL", "Counts")
            assert (_feed(declared.changefeed), declared.changefeed_include_original) == (_feed("1h"), False)
            assert declared.permissions == schema.permissions_rules({"select": "FULL"}, model=TdlStats)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_define_table_of_a_view_model(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """``define_view()``'s statement, a ``str``, equal to ``define_view()``'s on the same state; the source table
        holds no record (SurrealDB 3.1 refuses ``OVERWRITE`` on a view that holds records)."""
        async with _tdl(kind) as client:
            await _apply_ddl(client, "DEFINE TABLE tdl_note;")
            calls = _spy(monkeypatch, client)
            result = await TdlStats.define_table()
            assert isinstance(result, str)
            assert result == _G8
            assert _sent(calls) == [_INFO, _G8]
            tables = await _tables(client)
            assert await TdlStats.define_view() == result == await TdlStats.define_table()
            assert await _tables(client) == tables
            state = parse_define_table(tables["tdl_stats"])
            assert (state.view_query, state.comment) == (_VIEW_QUERY, "Counts")

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_g1_through_define_table(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """v0.32.0's guard: an ordinary table holding records is never turned into the view."""
        async with _tdl(kind) as client:
            await _apply_ddl(client, "DEFINE TABLE tdl_note; CREATE tdl_stats:x SET a = 1;")
            before = (await _tables(client))["tdl_stats"]
            calls = _spy(monkeypatch, client)
            with pytest.raises(SurrealDbError) as raised:
                await TdlStats.define_table()
            assert (type(raised.value), str(raised.value)) == (SurrealDbError, _g1("TdlStats", "tdl_stats"))
            assert _sent(calls) == [_INFO, "SELECT id FROM `tdl_stats` LIMIT 1;"]
            assert (await _tables(client))["tdl_stats"] == before
            assert await client.query("SELECT VALUE a FROM tdl_stats;", {}) == [1]


# --- the line facts define_table() is built around -------------------------------------------------------------------


class TdlNoteStrict(BaseSurrealModel):
    """``tdl_note`` declared ``SCHEMAFULL``, with one column, ``body``."""

    model_config = SurrealConfigDict(table_name="tdl_note", schema_mode=SchemaMode.SCHEMAFULL, connection="tdl")

    id: str | None = None
    body: str


@dataclass(frozen=True)
class _Refused:
    """A refusal whose text holds *reason*: of the write, or, when *ddl*, of the definitions themselves."""

    reason: str
    ddl: bool = False


#: The README 2.x/3.x rows the line facts back, by their first cell.
_UNTYPED_ROW = "An `Any` or untyped `list`/`set` field of a `SCHEMAFULL` model"
_UNDECLARED_ROW = "A write naming a field a `SCHEMAFULL` table does not define"

#: The failures of the line-fact pins once a SurrealDB line changes.
_UNTYPED_CHANGED = (
    "SurrealDB changed how this line stores an untyped value in a SCHEMAFULL table: re-measure it, then update this "
    f"test's expectation, define_table()'s FLEXIBLE rule and the README 2.x/3.x row '{_UNTYPED_ROW}'"
)
_UNDECLARED_CHANGED = (
    "SurrealDB changed how this line treats a write naming a field a SCHEMAFULL table does not define: re-measure it, "
    f"then update this test's expectation and the README 2.x/3.x row '{_UNDECLARED_ROW}'"
)

_NO_SUCH_FIELD = _Refused("no such field exists")
_NOT_FLEXIBLE = _Refused("FLEXIBLE can only be used with types containing object", ddl=True)

#: Raw facts on a SCHEMAFULL table: (the field definitions as (path, clauses), each ``DEFINE FIELD OVERWRITE`` in that
#: order, the value written, what reads back — or the refusal — before SurrealDB 3, on 3.1, on 3.2 and later). An
#: optional array gets an implicit item definition ``v.* TYPE any`` from SurrealDB 3.x, which on 3.1 still refuses an
#: object item; the item definition written after the array, as ``define_table()`` does, lifts it.
_UNTYPED_CASES: list[tuple[tuple[tuple[str, str], ...], str, object, object, object]] = [
    ((("v", "TYPE any"),), "{x: {y: 1}}", {}, {"x": {"y": 1}}, {"x": {"y": 1}}),
    ((("v", "TYPE any"),), "[{k: 1}]", [], [{"k": 1}], [{"k": 1}]),
    ((("v", "TYPE array"),), "[{k: 1}]", [], _NO_SUCH_FIELD, _NO_SUCH_FIELD),
    ((("v", "TYPE array"), ("v.*", "TYPE any")), "[{k: 1}]", [{}], [{"k": 1}], [{"k": 1}]),
    ((("v", "TYPE option<array>"),), "[{k: 1}]", [], _NO_SUCH_FIELD, [{"k": 1}]),
    ((("v", "TYPE option<array>"), ("v.*", "TYPE any")), "[{k: 1}]", [{}], [{"k": 1}], [{"k": 1}]),
    ((("v", "TYPE any FLEXIBLE"),), "{x: {y: 1}}", {"x": {"y": 1}}, _NOT_FLEXIBLE, _NOT_FLEXIBLE),
    (
        (("v", "TYPE array FLEXIBLE"), ("v.*", "TYPE any FLEXIBLE")),
        "[{k: 1}, [{m: 1}]]",
        [{"k": 1}, [{"m": 1}]],
        _NOT_FLEXIBLE,
        _NOT_FLEXIBLE,
    ),
    (
        (("v", "TYPE option<array> FLEXIBLE"), ("v.*", "TYPE any FLEXIBLE")),
        "[{k: 1}]",
        [{"k": 1}],
        _NOT_FLEXIBLE,
        _NOT_FLEXIBLE,
    ),
]
_UNTYPED_IDS = [
    "any-object",
    "any-array",
    "array",
    "array-items",
    "option-array",
    "option-array-items",
    "any-flexible",
    "array-flexible",
    "option-array-flexible",
]

#: The columns ``define_table()`` writes ``FLEXIBLE`` before SurrealDB 3.x for ``TdlLoose``: every one holding ``any``.
_LOOSE_FLEXIBLE = {
    ("anything",),
    ("items",),
    ("items", "*"),
    ("bag",),
    ("bag", "*"),
    ("maybe",),
    ("maybe", "*"),
    ("sub", "meta"),
}


async def _refusal(client: Any, query: str) -> str | None:
    """Run *query* raw: ``None`` when SurrealDB accepts every statement, else the first refusal's text (a parse error
    refuses the whole query)."""
    response = await client.query_raw(query, {})
    if "error" in response:
        return str(response["error"])
    refused = [str(result["result"]) for result in response["result"] if result["status"] != "OK"]
    return refused[0] if refused else None


class TestLineFactsE2E:
    """On every engine of both lines. ``define_table()`` reads the line (``client.version()``, once) only for a
    ``SCHEMAFULL`` table with a column holding ``any``, and before SurrealDB 3.x writes those columns ``FLEXIBLE``: the
    untyped values then round-trip on every engine. The raw facts this is built around: on a ``SCHEMAFULL`` table, 2.x
    and the embedded engine empty an object or an array in an ``any`` or untyped array column unless it is
    ``FLEXIBLE``, which 3.x refuses there; 3.x keeps them, but refuses an object item of a required untyped array
    without its item definition (3.1 also of an optional one, despite the item definition SurrealDB adds itself). A
    write naming a field a ``SCHEMAFULL`` table does not define — a ``merge()`` key, a field of a record stored before
    — is refused on 3.x and the field dropped before."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_define_table_reads_the_line_once(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _tdl(kind) as client:
            major = await _major(client)
            expected = _LOOSE if major >= 3 else _LOOSE_BEFORE_3
            calls = _spy(monkeypatch, client)
            assert await TdlLoose.define_table() == "\n".join(expected)
            assert [method for method, _ in calls if method == "version"] == ["version"]
            assert [args[0] for method, args in calls if method == "query"] == [_INFO, *expected]
            fields = _field_states(await _table_info(client, "tdl_loose"))
            assert {column.path for column in _columns(TdlLoose)} <= fields.keys()
            flexible = {path for path, field in fields.items() if field.flexible}
            assert flexible == (set() if major >= 3 else _LOOSE_FLEXIBLE)

            calls.clear()
            assert await TdlNote.define_table() == "\n".join(_G2)
            assert await TdlLog.define_table() == "\n".join(_G3)
            assert _sent(calls) == [_INFO, *_G2, _INFO, *_G3]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_untyped_values_round_trip(self, kind: str) -> None:
        """An object or an array in an ``Any``, an untyped list (with objects and nested arrays), a sub-model's ``Any``
        and an optional untyped list read back equal on every engine, SurrealDB 3.1 included: ``define_table()`` writes
        each list's item definition after it."""
        async with _tdl(kind):
            await TdlLoose.define_table()
            loose = TdlLoose(
                id="l1",
                anything={"x": {"y": 1}},
                items=[1, "b", {"k": 1}, [{"m": 1}]],
                bag=[{"k": 1}],
                sub=TdlLooseSub(meta={"q": 1}),
            )
            sent = loose.model_dump()
            await loose.save()
            assert (await TdlLoose.objects().get("l1")).model_dump() == sent

            maybe = TdlLoose(id="l2", maybe=[{"k": 1}])
            sent = maybe.model_dump()
            await maybe.save()
            assert (await TdlLoose.objects().get("l2")).model_dump() == sent

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize(("definitions", "value", "before_3", "line_3_1", "line_3"), _UNTYPED_CASES, ids=_UNTYPED_IDS)
    async def test_an_untyped_value_raw(
        self,
        kind: str,
        definitions: tuple[tuple[str, str], ...],
        value: str,
        before_3: object,
        line_3_1: object,
        line_3: object,
    ) -> None:
        """What a ``SCHEMAFULL`` table stores for an object or an array in an ``any`` or untyped array column, with and
        without ``FLEXIBLE`` and the item definition — the facts of the README 2.x/3.x row on ``Any`` and untyped
        ``list``/``set`` fields. Reads through the ORM's client."""
        async with _tdl(kind) as client:
            line = await _line(client)
            expected = before_3 if line[0] < 3 else line_3_1 if line == (3, 1) else line_3
            ddl = "DEFINE TABLE tdl_raw SCHEMAFULL; " + " ".join(
                f"DEFINE FIELD OVERWRITE {path} ON tdl_raw {clauses};" for path, clauses in definitions
            )
            refused = await _refusal(client, ddl)
            if isinstance(expected, _Refused) and expected.ddl:
                assert refused is not None and expected.reason in refused, _UNTYPED_CHANGED
                return
            assert refused is None, f"{_UNTYPED_CHANGED}: {refused}"
            refused = await _refusal(client, f"CREATE tdl_raw:1 SET v = {value} RETURN NONE;")
            if isinstance(expected, _Refused):
                assert refused is not None and expected.reason in refused, _UNTYPED_CHANGED
                assert await client.query("SELECT VALUE v FROM tdl_raw;", {}) == [], _UNTYPED_CHANGED
                return
            assert refused is None, f"{_UNTYPED_CHANGED}: {refused}"
            assert await client.query("SELECT VALUE v FROM tdl_raw:1;", {}) == [expected], _UNTYPED_CHANGED

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_write_naming_an_undeclared_field(self, kind: str) -> None:
        """A record stored before the table became ``SCHEMAFULL`` reads back unchanged after ``define_table()``; its
        next ``merge()`` is refused on SurrealDB 3.x (the field it holds is not defined) and drops that field before 3.
        A ``merge()`` naming a key the model does not declare: refused on 3.x, dropped before 3 — the README 2.x/3.x row
        on a write naming a field a ``SCHEMAFULL`` table does not define."""
        async with _tdl(kind) as client:
            major = await _major(client)
            await _apply_ddl(client, "CREATE tdl_note:n1 SET body = 'b', extra = {k: 1}, gone = 'g' RETURN NONE;")
            before = await client.query("SELECT * FROM tdl_note:n1;", {})
            await TdlNoteStrict.define_table()
            assert await client.query("SELECT * FROM tdl_note:n1;", {}) == before

            note = await TdlNoteStrict.objects().get("n1")
            if major >= 3:
                with pytest.raises(InternalError, match="no such field exists") as refused:
                    await note.merge(body="c")
                assert type(refused.value) is InternalError, _UNDECLARED_CHANGED
                assert await client.query("SELECT * FROM tdl_note:n1;", {}) == before, _UNDECLARED_CHANGED
            else:
                await note.merge(body="c")
                n1 = [{"id": RecordID("tdl_note", "n1"), "body": "c"}]
                assert await client.query("SELECT * FROM tdl_note:n1;", {}) == n1, _UNDECLARED_CHANGED

            fresh = TdlNoteStrict(id="n2", body="x")
            await fresh.save()
            if major >= 3:
                with pytest.raises(InternalError, match="no such field exists") as refused:
                    await fresh.merge(nope=1)
                assert type(refused.value) is InternalError, _UNDECLARED_CHANGED
            else:
                await fresh.merge(nope=1)
            n2 = [{"id": RecordID("tdl_note", "n2"), "body": "x"}]
            assert await client.query("SELECT * FROM tdl_note:n2;", {}) == n2, _UNDECLARED_CHANGED


# --- define_table(tx=…) ----------------------------------------------------------------------------------------------


class _Rollback(Exception):
    """Raised inside a ``transaction()`` block to roll it back."""


#: A definition of ``body`` queued by hand before ``define_table(tx=…)`` in the same block.
_QUEUED_BODY = "DEFINE FIELD OVERWRITE `body` ON `tdl_note` TYPE string ASSERT $value != '';"

#: The failure of the queued-definition test once ``define_table(tx=…)`` sees a definition queued in its block.
_QUEUED_SEEN = (
    "define_table(tx=…) now keeps a definition of the same table or field queued earlier in the block: rewrite the "
    "README section 35 paragraph 'Transactions, connections and the cache', define_table()'s docstring and this test"
)


class TestTableTransactionsE2E:
    """``define_table(tx=tx)`` on every engine of both lines, in a buffered transaction (HTTP, SurrealDB 2.x, the
    embedded engine) or an interactive one (3.x over WebSocket): its reads — ``INFO FOR DB`` and the line — run on the
    committed state before its statements join the block; the statements apply with the block — at the commit when
    buffered, in the block when interactive — and not at all when the block raises; a transaction of another
    connection is refused before any request. A definition of the same table or field queued earlier in the block is
    not seen by the reads, a documented limit."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_reads_come_first_and_the_block_applies_the_statements(
        self, kind: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with _tdl(kind) as client:
            major = await _major(client)
            statements = _LOOSE if major >= 3 else _LOOSE_BEFORE_3
            interactive = kind == "ws" and major >= 3
            async with SurrealDBConnectionManager.transaction("tdl") as tx:
                assert tx.is_interactive is interactive
                calls = _spy(monkeypatch, client)
                assert await TdlLoose.define_table(tx=tx) == "\n".join(statements)
                sent = [(method, args[0] if args else None) for method, args in calls]
                reads = [("query", _INFO), ("query_raw", _INFO), ("version", None)]
                if interactive:
                    # Sent at once, through the transaction, after the reads of the committed state.
                    assert sent == [*reads, *(("query_raw", statement) for statement in statements)]
                else:
                    # Queued, after the reads: nothing but the reads has reached the server.
                    assert sent == reads
                    assert getattr(tx, "statements", None) == statements
                monkeypatch.undo()
                # Outside the block, until the commit, the table does not exist.
                assert "tdl_loose" not in await _tables(client)
                await TdlLoose(id="l1", items=[{"k": 1}]).save(tx=tx)
            fields = _field_states(await _table_info(client, "tdl_loose"))
            assert {path for path, field in fields.items() if field.flexible} == (set() if major >= 3 else _LOOSE_FLEXIBLE)
            assert (await TdlLoose.objects().get("l1")).items == [{"k": 1}]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_block_that_raises_applies_nothing(self, kind: str) -> None:
        async with _tdl(kind) as client:
            interactive = kind == "ws" and await _major(client) >= 3
            with pytest.raises(_Rollback):
                async with SurrealDBConnectionManager.transaction("tdl") as tx:
                    assert tx.is_interactive is interactive
                    await TdlLoose.define_table(tx=tx)
                    raise _Rollback
            assert "tdl_loose" not in await _tables(client)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_transaction_of_another_connection(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _tdl(kind) as client:
            _register_tdl(kind, "tdl2")
            try:
                async with SurrealDBConnectionManager.transaction("tdl2") as tx:
                    other = await SurrealDBConnectionManager.get_client("tdl2")
                    calls = _spy(monkeypatch, client)
                    other_calls = _spy(monkeypatch, other)
                    with pytest.raises(SurrealDbError) as raised:
                        await TdlLoose.define_table(tx=tx)
                    assert (calls, other_calls) == ([], [])
                    if not tx.is_interactive:
                        assert getattr(tx, "statements", None) == []
                    monkeypatch.undo()
            finally:
                await SurrealDBConnectionManager.remove_connection("tdl2")
            assert type(raised.value) is SurrealDbError
            assert str(raised.value).startswith(
                "TdlLoose.define_table() cannot run in this transaction: the transaction belongs to connection 'tdl2' "
                "and TdlLoose runs on connection 'tdl'."
            )
            assert "tdl_loose" not in await _tables(client)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_definition_queued_earlier_in_the_block_is_replaced(self, kind: str) -> None:
        """A documented limit: ``define_table(tx=…)`` reads the committed definitions, so a definition of the same table
        or field queued earlier in the block is replaced at commit; a limit to lift. Here the queued ``ASSERT`` on
        ``body`` is gone once the block commits: the statement built from the committed definition replaced it. README
        section 35, "Transactions, connections and the cache", says so: define those clauses before the block. The
        same on a buffered and an interactive transaction."""
        async with _tdl(kind) as client:
            await _apply_ddl(client, "DEFINE TABLE tdl_note; DEFINE FIELD body ON tdl_note TYPE string;")
            interactive = kind == "ws" and await _major(client) >= 3
            async with SurrealDBConnectionManager.transaction("tdl") as tx:
                assert tx.is_interactive is interactive
                await tx.add(_QUEUED_BODY, None)
                assert await TdlNote.define_table(tx=tx) == "\n".join(_G2), _QUEUED_SEEN
            body = parse_define_field((await _table_info(client, "tdl_note"))["fields"]["body"]).state
            assert (body.field_type, body.assertion) == ("string", None), _QUEUED_SEEN


# --- access_ddl() and define_access(): the model's schema mode, the declared clauses --------------------------------


class TdlAuthPlain(AuthenticatedUserMixin, BaseSurrealModel):
    """A0: an auth model with none of the new keys — v0.17.0's statements byte for byte."""

    model_config = SurrealConfigDict(table_name="tdl_user", connection="tdl")

    id: str | None = None
    email: str
    password: str


class TdlAuthUser(AuthenticatedUserMixin, BaseSurrealModel):
    """A1: ``TableType.USER`` without ``schema_mode`` — a ``SCHEMAFULL`` table with its fields."""

    model_config = SurrealConfigDict(table_name="tdl_user", table_type=TableType.USER, connection="tdl")

    id: str | None = None
    email: str
    password: str


class TdlAuthReset(TdlAuthUser):
    """A0 again: a subclass resetting ``table_type`` declares nothing."""

    model_config = SurrealConfigDict(table_type=None)


class TdlAuthRules(AuthenticatedUserMixin, BaseSurrealModel):
    """A2: declared permissions replace v0.17.0's."""

    model_config = SurrealConfigDict(
        table_name="tdl_user",
        permissions={"select": "WHERE id = $auth.id", "update": "WHERE id = $auth.id", "create": "FULL"},
        connection="tdl",
    )

    id: str | None = None
    email: str
    password: str


class TdlAuthFeed(AuthenticatedUserMixin, BaseSurrealModel):
    """A3: a comment and a change feed in v0.17.0's statement."""

    model_config = SurrealConfigDict(table_name="tdl_user", comment="Users", changefeed="1d", connection="tdl")

    id: str | None = None
    email: str
    password: str


class TdlAuthFull(AuthenticatedUserMixin, BaseSurrealModel):
    """A4: a declared ``SchemaMode.SCHEMAFULL`` without ``table_type`` — A1's statements."""

    model_config = SurrealConfigDict(table_name="tdl_user", schema_mode=SchemaMode.SCHEMAFULL, connection="tdl")

    id: str | None = None
    email: str
    password: str


class TdlAuthUserFull(AuthenticatedUserMixin, BaseSurrealModel):
    """A4: ``TableType.USER`` and a declared ``SchemaMode.SCHEMAFULL`` — A1's statements."""

    model_config = SurrealConfigDict(
        table_name="tdl_user", table_type=TableType.USER, schema_mode=SchemaMode.SCHEMAFULL, connection="tdl"
    )

    id: str | None = None
    email: str
    password: str


class TdlAuthHash(AuthenticatedUserMixin, BaseSurrealModel):
    """A5: ``TableType.HASH`` — v0.17.0's ``SCHEMALESS`` statement with ``TYPE NORMAL``."""

    model_config = SurrealConfigDict(table_name="tdl_user", table_type=TableType.HASH, connection="tdl")

    id: str | None = None
    email: str
    password: str


class TdlAuthAny(AuthenticatedUserMixin, BaseSurrealModel):
    """A5: ``TableType.ANY`` — ``TYPE ANY SCHEMALESS``."""

    model_config = SurrealConfigDict(table_name="tdl_user", table_type=TableType.ANY, connection="tdl")

    id: str | None = None
    email: str
    password: str


class TdlAuthLess(AuthenticatedUserMixin, BaseSurrealModel):
    """A declared ``SchemaMode.SCHEMALESS`` writes v0.17.0's ``SCHEMALESS``."""

    model_config = SurrealConfigDict(table_name="tdl_user", schema_mode=SchemaMode.SCHEMALESS, connection="tdl")

    id: str | None = None
    email: str
    password: str


class TdlAuthOpen(AuthenticatedUserMixin, BaseSurrealModel):
    """UX1: ``TableType.USER`` with ``extra="allow"`` imports, and is refused when its table is defined."""

    model_config = SurrealConfigDict(table_name="tdl_user", table_type=TableType.USER, extra="allow", connection="tdl")

    id: str | None = None
    email: str
    password: str


#: v0.17.0's access statement of the ``tdl_user`` models.
_A_ACCESS = (
    "DEFINE ACCESS OVERWRITE tdl_user_access ON DATABASE TYPE RECORD "
    "SIGNUP ( CREATE tdl_user SET email = $email, password = crypto::argon2::generate($password) ) "
    "SIGNIN ( SELECT * FROM tdl_user WHERE email = $email AND crypto::argon2::compare(password, $password) ) "
    "DURATION FOR TOKEN 15m, FOR SESSION 12h;"
)
_A_ACCESS_KEPT = (
    "DEFINE ACCESS IF NOT EXISTS tdl_user_access ON DATABASE TYPE RECORD "
    "SIGNUP ( CREATE tdl_user SET email = $email, password = crypto::argon2::generate($password) ) "
    "SIGNIN ( SELECT * FROM tdl_user WHERE email = $email AND crypto::argon2::compare(password, $password) ) "
    "DURATION FOR TOKEN 15m, FOR SESSION 12h;"
)

_A0 = "DEFINE TABLE OVERWRITE tdl_user SCHEMALESS PERMISSIONS FOR select, update WHERE id = $auth.id;"
_A0_KEPT = "DEFINE TABLE IF NOT EXISTS tdl_user SCHEMALESS PERMISSIONS FOR select, update WHERE id = $auth.id;"

_A1 = [
    "DEFINE TABLE OVERWRITE `tdl_user` TYPE NORMAL SCHEMAFULL PERMISSIONS FOR select, update WHERE id = $auth.id, FOR "
    "create, delete NONE;",
    "DEFINE FIELD OVERWRITE `email` ON `tdl_user` TYPE string;",
    "DEFINE FIELD OVERWRITE `password` ON `tdl_user` TYPE string;",
]

_A2 = (
    "DEFINE TABLE OVERWRITE tdl_user SCHEMALESS PERMISSIONS FOR select, update WHERE id = $auth.id, FOR create FULL, FOR "
    "delete NONE;"
)
_A3 = (
    "DEFINE TABLE OVERWRITE tdl_user SCHEMALESS COMMENT 'Users' CHANGEFEED 1d PERMISSIONS FOR select, update WHERE id = "
    "$auth.id;"
)
_A5_HASH = "DEFINE TABLE OVERWRITE tdl_user TYPE NORMAL SCHEMALESS PERMISSIONS FOR select, update WHERE id = $auth.id;"
_A5_ANY = "DEFINE TABLE OVERWRITE tdl_user TYPE ANY SCHEMALESS PERMISSIONS FOR select, update WHERE id = $auth.id;"

#: The access's permissions as SurrealDB reads them back from A1's table statement.
_A1_PERMISSIONS = {"select": "WHERE id = $auth.id", "create": "NONE", "update": "WHERE id = $auth.id", "delete": "NONE"}

_INFO_USER = "INFO FOR TABLE `tdl_user`;"

#: The ``tdl_user`` table as A1's statements store it, and its fields.
_STORED_USER = (
    "DEFINE TABLE tdl_user TYPE NORMAL SCHEMAFULL COMMENT 'keep' PERMISSIONS FOR select WHERE owner = $auth.id, FOR "
    "create, update, delete FULL"
)
_STORED_USER_FIELDS = {
    "email": "DEFINE FIELD email ON tdl_user TYPE string ASSERT $value != '' PERMISSIONS FULL",
    "password": "DEFINE FIELD password ON tdl_user TYPE string PERMISSIONS FULL",
}
_STORED_USER_VIEW = "DEFINE TABLE tdl_user TYPE ANY SCHEMALESS AS SELECT count() AS n FROM tdl_note GROUP ALL PERMISSIONS NONE"


class TdlAuthTotal(AuthenticatedUserMixin, BaseSurrealModel):
    """CV1 through ``access_ddl()``: a ``TableType.USER`` model whose computed field's expression is not balanced."""

    model_config = SurrealConfigDict(table_name="tdl_user", table_type=TableType.USER, connection="tdl")

    id: str | None = None
    email: str
    password: str
    total: Computed[int] = computed("count(items")


class TestAccessDdl:
    """``access_ddl()``/``define_access()``: the schema mode decides — ``SCHEMAFULL`` (``TableType.USER`` or declared)
    through the model table DDL, every other model v0.17.0's ``SCHEMALESS`` statement with its declared clauses; the
    refusals before any request; the declared permissions never quoted."""

    def test_a0_a_model_without_the_new_keys_is_v0170_byte_for_byte(self) -> None:
        assert TdlAuthPlain.access_ddl() == [
            "DEFINE TABLE OVERWRITE tdl_user SCHEMALESS PERMISSIONS FOR select, update WHERE id = $auth.id;",
            "DEFINE ACCESS OVERWRITE tdl_user_access ON DATABASE TYPE RECORD SIGNUP ( CREATE tdl_user SET email = $email, "
            "password = crypto::argon2::generate($password) ) SIGNIN ( SELECT * FROM tdl_user WHERE email = $email AND "
            "crypto::argon2::compare(password, $password) ) DURATION FOR TOKEN 15m, FOR SESSION 12h;",
        ]
        assert TdlAuthPlain.access_ddl(overwrite=False) == [
            "DEFINE TABLE IF NOT EXISTS tdl_user SCHEMALESS PERMISSIONS FOR select, update WHERE id = $auth.id;",
            "DEFINE ACCESS IF NOT EXISTS tdl_user_access ON DATABASE TYPE RECORD SIGNUP ( CREATE tdl_user SET email = "
            "$email, password = crypto::argon2::generate($password) ) SIGNIN ( SELECT * FROM tdl_user WHERE email = $email "
            "AND crypto::argon2::compare(password, $password) ) DURATION FOR TOKEN 15m, FOR SESSION 12h;",
        ]
        assert TdlAuthPlain.get_schema_mode() is None
        assert TdlAuthReset.access_ddl() == [_A0, _A_ACCESS]

    def test_a1_a_user_table_is_schemafull_with_its_fields(self) -> None:
        assert TdlAuthUser.access_ddl() == [*_A1, _A_ACCESS]
        assert TdlAuthUser.access_ddl(overwrite=False) == [*_if_not_exists(_A1), _A_ACCESS_KEPT]
        assert all("IF NOT EXISTS" in statement for statement in TdlAuthUser.access_ddl(overwrite=False))

    def test_a2_declared_permissions(self) -> None:
        assert TdlAuthRules.access_ddl() == [_A2, _A_ACCESS]

    def test_a3_a_comment_and_a_change_feed(self) -> None:
        assert TdlAuthFeed.access_ddl() == [_A3, _A_ACCESS]

    @pytest.mark.parametrize("model", [TdlAuthFull, TdlAuthUserFull], ids=lambda model: model.__name__)
    def test_a4_a_declared_schemafull_is_a1(self, model: type[TdlAuthFull]) -> None:
        assert model.access_ddl() == [*_A1, _A_ACCESS]
        assert model.access_ddl(overwrite=False) == [*_if_not_exists(_A1), _A_ACCESS_KEPT]

    @pytest.mark.parametrize(
        ("model", "statement"),
        [(TdlAuthHash, _A5_HASH), (TdlAuthAny, _A5_ANY), (TdlAuthLess, _A0)],
        ids=["hash", "any", "declared-schemaless"],
    )
    def test_a5_the_type_clause_only_when_table_type_is_declared(self, model: type[TdlAuthHash], statement: str) -> None:
        assert model.access_ddl() == [statement, _A_ACCESS]
        assert model.access_ddl(overwrite=False)[0] == statement.replace("OVERWRITE", "IF NOT EXISTS")

    @pytest.mark.parametrize("model", [TdlAuthPlain, TdlAuthRules, TdlAuthUser, TdlAuthFull, TdlAuthOpen, TdlAuthTotal])
    def test_with_table_false_is_unchanged(self, model: type[TdlAuthPlain]) -> None:
        assert model.access_ddl(with_table=False) == [_A_ACCESS]
        assert model.access_ddl(overwrite=False, with_table=False) == [_A_ACCESS_KEPT]

    def test_ux1_from_access_ddl(self) -> None:
        with pytest.raises(TypeError) as raised:
            TdlAuthOpen.access_ddl()
        assert type(raised.value) is TypeError
        assert str(raised.value) == _ux1("TdlAuthOpen")

    def test_cv1_through_access_ddl(self) -> None:
        with pytest.raises(ValueError) as raised:
            TdlAuthTotal.access_ddl()
        assert type(raised.value) is ValueError
        assert str(raised.value) == _cv1("TdlAuthTotal", "total", _R_BRACKETS)

    @pytest.mark.parametrize("overwrite", ["yes", 1], ids=repr)
    def test_e1_through_access_ddl(self, overwrite: Any) -> None:
        with pytest.raises(TypeError) as raised:
            TdlAuthUser.access_ddl(overwrite=overwrite)
        assert str(raised.value) == f"overwrite= must be a bool, got {type(overwrite).__name__}."

    async def test_ux1_from_define_access_before_any_request(self, use_client: Callable[[Any], None]) -> None:
        client = _TableClient()
        use_client(client)
        with pytest.raises(TypeError) as raised:
            await TdlAuthOpen.define_access()
        assert str(raised.value) == _ux1("TdlAuthOpen")
        assert client.queries == []
        assert client.version_calls == 0

    async def test_e1_and_cv1_from_define_access_before_any_request(self, use_client: Callable[[Any], None]) -> None:
        client = _TableClient()
        use_client(client)
        with pytest.raises(TypeError):
            await TdlAuthUser.define_access(overwrite="yes")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            await TdlAuthTotal.define_access()
        assert client.queries == []

    async def test_with_table_false_applies_the_access_alone(self, use_client: Callable[[Any], None]) -> None:
        """No UX1, no read: the access statement only, as in v0.17.0."""
        for model in (TdlAuthOpen, TdlAuthUser, TdlAuthPlain):
            client = _TableClient()
            use_client(client)
            assert await model.define_access(with_table=False) == [_A_ACCESS]
            assert client.queries == [_A_ACCESS]

    async def test_case_1_applies_v0170_statements(self, use_client: Callable[[Any], None]) -> None:
        """No read: v0.17.0's two statements, one per call — and so for a declared clause other than permissions."""
        for model, statement in ((TdlAuthPlain, _A0), (TdlAuthFeed, _A3), (TdlAuthHash, _A5_HASH)):
            client = _TableClient()
            use_client(client)
            assert await model.define_access() == [statement, _A_ACCESS]
            assert client.queries == [statement, _A_ACCESS]

    async def test_case_1_declared_permissions(self, use_client: Callable[[Any], None]) -> None:
        client = _TableClient()
        use_client(client)
        assert await TdlAuthRules.define_access() == [_A2, _A_ACCESS]
        assert client.queries == [_A2, _A_ACCESS]

    @pytest.mark.parametrize(
        ("reason", "text"),
        [
            ("The table is locked", "The table is locked"),
            (
                "Found a problem in PERMISSIONS FOR select, update WHERE id = $auth.id",
                "the server's message quotes the statement, so it is left out",
            ),
        ],
        ids=["whole", "quoting"],
    )
    async def test_case_1_a_refusal_never_quotes_the_declared_permissions(
        self, use_client: Callable[[Any], None], reason: str, text: str
    ) -> None:
        """The table statement on its own, named by its table; the access statement is not sent."""
        error = InternalError("Internal", reason)
        client = _TableClient(ddl_error=error, refuse="DEFINE TABLE ")
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await TdlAuthRules.define_access()
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == f"Can't apply table definition: 'tdl_user' -> {text}"
        assert "$auth.id" not in str(raised.value)
        assert client.queries == [_A2]

    async def test_case_2_a_missing_table(self, use_client: Callable[[Any], None]) -> None:
        """``INFO FOR DB`` once, then A1's statements one per call, then the access statement."""
        for model in (TdlAuthUser, TdlAuthFull, TdlAuthUserFull):
            client = _TableClient()
            use_client(client)
            result = await model.define_access()
            assert result == [*_A1, _A_ACCESS] == model.access_ddl()
            assert client.queries == [_INFO, *_A1, _A_ACCESS]
            assert client.version_calls == 0

    async def test_case_2_overwrite_false(self, use_client: Callable[[Any], None]) -> None:
        client = _TableClient()
        use_client(client)
        assert await TdlAuthUser.define_access(overwrite=False) == [*_if_not_exists(_A1), _A_ACCESS_KEPT]
        assert client.queries == [_INFO, *_if_not_exists(_A1), _A_ACCESS_KEPT]

    async def test_case_2_an_existing_table(self, use_client: Callable[[Any], None]) -> None:
        """The carried statements: the access's permissions written over the stored ones, the stored comment and the
        fields' ``ASSERT`` kept."""
        client = _TableClient({"tdl_user": _STORED_USER}, _STORED_USER_FIELDS)
        use_client(client)
        carried = [
            "DEFINE TABLE OVERWRITE `tdl_user` TYPE NORMAL SCHEMAFULL COMMENT 'keep' PERMISSIONS FOR select, update WHERE "
            "id = $auth.id, FOR create, delete NONE;",
            "DEFINE FIELD OVERWRITE `email` ON `tdl_user` TYPE string ASSERT $value != '';",
            _A1[2],
        ]
        assert await TdlAuthUser.define_access() == [*carried, _A_ACCESS]
        assert client.queries == [_INFO, _INFO_USER, *carried, _A_ACCESS]

        client = _TableClient({"tdl_user": _STORED_USER}, _STORED_USER_FIELDS)
        use_client(client)
        assert (await TdlAuthUser.define_table()).split("\n") == [
            "DEFINE TABLE OVERWRITE `tdl_user` TYPE NORMAL SCHEMAFULL COMMENT 'keep' PERMISSIONS FOR select WHERE owner "
            "= $auth.id, FOR create, update, delete FULL;",
            *carried[1:],
        ]

    @pytest.mark.parametrize("overwrite", [True, False], ids=["overwrite", "if-not-exists"])
    async def test_case_2_tg1_names_define_access(self, use_client: Callable[[Any], None], overwrite: bool) -> None:
        client = _TableClient({"tdl_user": _STORED_USER_VIEW})
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await TdlAuthUser.define_access(overwrite=overwrite)
        assert (type(raised.value), str(raised.value)) == (
            SurrealDbError,
            _tg1("TdlAuthUser", "define_access", "tdl_user"),
        )
        assert client.queries == [_INFO]

    async def test_case_2_a_transaction(self, use_client: Callable[[Any], None]) -> None:
        """The read on the client before anything is queued; the table, field and access statements in the block."""
        client = _TableClient()
        use_client(client)
        tx = _QueuingTx()
        assert await TdlAuthUser.define_access(tx=tx) == [*_A1, _A_ACCESS]  # type: ignore[arg-type]
        assert client.queries == [_INFO]
        assert tx.added == [(statement, None) for statement in [*_A1, _A_ACCESS]]

    async def test_case_2_a_transaction_of_another_connection(self, use_client: Callable[[Any], None]) -> None:
        client = _TableClient()
        use_client(client)
        tx = _QueuingTx("other")
        with pytest.raises(SurrealDbError) as raised:
            await TdlAuthUser.define_access(tx=tx)  # type: ignore[arg-type]
        assert str(raised.value).startswith("TdlAuthUser.define_access() cannot run in this transaction: ")
        assert client.queries == []
        assert tx.added == []

    def test_define_access_keeps_its_signature(self) -> None:
        """v0.17.0's keywords and its ``list[str]`` return."""
        assert inspect.signature(AuthenticatedUserMixin.define_access) == inspect.Signature(
            [
                inspect.Parameter("overwrite", inspect.Parameter.KEYWORD_ONLY, default=True, annotation=bool),
                inspect.Parameter("with_table", inspect.Parameter.KEYWORD_ONLY, default=True, annotation=bool),
                inspect.Parameter("tx", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Transaction | None),
            ],
            return_annotation=list[str],
        )


#: The identity the E2E signups use, and its password.
_TDL_EMAIL = "ada@example.test"
_TDL_PASSWORD = "s3cret-passphrase"


async def _signup_and_signin(model: type[TdlAuthUser], client: Any) -> None:
    """``signup()`` then ``signin()`` of *model* succeed, and the record reads back its email."""
    created = await model.signup(email=_TDL_EMAIL, password=_TDL_PASSWORD)
    assert created.tokens.access
    assert created.user.email == _TDL_EMAIL
    assert created.user.password != _TDL_PASSWORD
    signed_in = await model.signin(email=_TDL_EMAIL, password=_TDL_PASSWORD)
    assert signed_in.user.get_id() == created.user.get_id()
    assert signed_in.user.email == _TDL_EMAIL
    assert await client.query("SELECT VALUE email FROM tdl_user;", {}) == [_TDL_EMAIL]


async def _user_table(client: Any) -> tuple[Any, dict[str, Any]]:
    """The ``tdl_user`` entry of ``INFO FOR DB`` and ``INFO FOR TABLE tdl_user``."""
    return (await _tables(client))["tdl_user"], await _table_info(client, "tdl_user")


class TestAccessE2E:
    """Over WebSocket on both lines (and 3.1.5): a ``SCHEMAFULL`` access table — ``TableType.USER`` or declared — is
    defined with its fields by ``define_access()``, and signup and signin work there; ``define_table()`` afterwards, or
    a second ``define_access()``, sends the carried statements and changes nothing; a view under it is refused, naming
    ``define_access()``."""

    async def test_a_user_model_without_schema_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _tdl("ws") as client:
            calls = _spy(monkeypatch, client)
            assert await TdlAuthUser.define_access() == [*_A1, _A_ACCESS]
            assert _sent(calls) == [_INFO, *_A1, _A_ACCESS]
            monkeypatch.undo()

            stored, info = await _user_table(client)
            state = parse_define_table(stored)
            assert (state.table_type, state.schema_mode) == ("normal", "SCHEMAFULL")
            assert state.permissions == _A1_PERMISSIONS
            fields = _field_states(info)
            assert {path: (field.field_type, field.nullable) for path, field in fields.items()} == {
                ("email",): ("string", False),
                ("password",): ("string", False),
            }
            assert "tdl_user_access" in (await client.query("INFO FOR DB;", {}))["accesses"]
            await _signup_and_signin(TdlAuthUser, client)

    async def test_define_access_then_define_table_changes_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _tdl("ws") as client:
            await TdlAuthUser.define_access()
            before = await _user_table(client)
            calls = _spy(monkeypatch, client)
            assert await TdlAuthUser.define_table() == "\n".join(_A1)
            assert _sent(calls) == [_INFO, _INFO_USER, *_A1]
            monkeypatch.undo()
            assert await _user_table(client) == before
            await _signup_and_signin(TdlAuthUser, client)

    async def test_a_declared_schemafull_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _tdl("ws") as client:
            assert await TdlAuthFull.define_access() == [*_A1, _A_ACCESS]
            stored, info = await _user_table(client)
            assert parse_define_table(stored).schema_mode == "SCHEMAFULL"
            assert set(_field_states(info)) == {("email",), ("password",)}
            await _signup_and_signin(TdlAuthFull, client)

            before = await _user_table(client)
            calls = _spy(monkeypatch, client)
            assert await TdlAuthFull.define_access() == [*_A1, _A_ACCESS]
            assert _sent(calls) == [_INFO, _INFO_USER, *_A1, _A_ACCESS]
            monkeypatch.undo()
            assert await _user_table(client) == before
            assert (await TdlAuthFull.signin(email=_TDL_EMAIL, password=_TDL_PASSWORD)).user.email == _TDL_EMAIL

    async def test_tg1_names_define_access(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _tdl("ws") as client:
            await _apply_ddl(client, f"CREATE tdl_note:n1 SET body = 'x'; DEFINE TABLE tdl_user AS {_VIEW_QUERY};")
            before = (await _tables(client))["tdl_user"]
            calls = _spy(monkeypatch, client)
            with pytest.raises(SurrealDbError) as raised:
                await TdlAuthUser.define_access()
            assert _sent(calls) == [_INFO]
            monkeypatch.undo()
            assert (type(raised.value), str(raised.value)) == (
                SurrealDbError,
                _tg1("TdlAuthUser", "define_access", "tdl_user"),
            )
            assert (await _tables(client))["tdl_user"] == before
            assert "tdl_user_access" not in (await client.query("INFO FOR DB;", {}))["accesses"]


# === the generator writes the keys back: the round trip ==============================================================

#: A schema written by hand on ``tdl_db``: a ``TYPE ANY`` table; a ``SCHEMAFULL`` table with a comment, a change feed,
#: permissions, an ``ASSERT``, a computed ``VALUE`` field, constant and server ``DEFAULT``s and a typed record link,
#: holding the record ``tdl_rt:1``; and a ``SCHEMAFULL`` table with every kind the ORM writes — a sub-object, an
#: array of sub-objects, literals, a column that is not a Python name.
_RT_SCHEMA = (
    "DEFINE TABLE tdl_rt_any SCHEMALESS; "
    "DEFINE FIELD n ON tdl_rt_any TYPE option<int>; "
    "DEFINE TABLE tdl_rt TYPE NORMAL SCHEMAFULL COMMENT 'round trip' CHANGEFEED 1d INCLUDE ORIGINAL "
    "PERMISSIONS FOR select WHERE $auth.id != NONE, FOR create, update FULL; "
    "DEFINE FIELD a ON tdl_rt TYPE int ASSERT $value > 0; "
    "DEFINE FIELD v ON tdl_rt TYPE option<int> VALUE a + 1; "
    "DEFINE FIELD age ON tdl_rt TYPE option<int> DEFAULT 18; "
    "DEFINE FIELD lvl ON tdl_rt TYPE int DEFAULT 1; "
    "DEFINE FIELD created ON tdl_rt TYPE datetime DEFAULT time::now(); "
    "DEFINE FIELD rec ON tdl_rt TYPE option<record<tdl_rt_any>>; "
    "DEFINE TABLE tdl_rt_kinds TYPE NORMAL SCHEMAFULL; "
    "DEFINE FIELD s ON tdl_rt_kinds TYPE string; "
    "DEFINE FIELD i ON tdl_rt_kinds TYPE int; "
    "DEFINE FIELD f ON tdl_rt_kinds TYPE float; "
    "DEFINE FIELD b ON tdl_rt_kinds TYPE bool; "
    "DEFINE FIELD d ON tdl_rt_kinds TYPE decimal; "
    "DEFINE FIELD dt ON tdl_rt_kinds TYPE datetime; "
    "DEFINE FIELD du ON tdl_rt_kinds TYPE duration; "
    "DEFINE FIELD u ON tdl_rt_kinds TYPE uuid; "
    "DEFINE FIELD bt ON tdl_rt_kinds TYPE bytes; "
    "DEFINE FIELD r ON tdl_rt_kinds TYPE record; "
    "DEFINE FIELD ls ON tdl_rt_kinds TYPE array<string>; "
    "DEFINE FIELD st ON tdl_rt_kinds TYPE set<int>; "
    "DEFINE FIELD tu ON tdl_rt_kinds TYPE [int, string]; "
    "DEFINE FIELD dc ON tdl_rt_kinds TYPE object; "
    "DEFINE FIELD sub ON tdl_rt_kinds TYPE object; "
    "DEFINE FIELD sub.city ON tdl_rt_kinds TYPE string; "
    "DEFINE FIELD sub.zip ON tdl_rt_kinds TYPE option<string>; "
    "DEFINE FIELD subs ON tdl_rt_kinds TYPE array<object>; "
    "DEFINE FIELD subs.*.city ON tdl_rt_kinds TYPE string; "
    "DEFINE FIELD subs.*.zip ON tdl_rt_kinds TYPE option<string>; "
    "DEFINE FIELD osub ON tdl_rt_kinds TYPE option<object>; "
    "DEFINE FIELD osub.city ON tdl_rt_kinds TYPE string; "
    "DEFINE FIELD osub.zip ON tdl_rt_kinds TYPE option<string>; "
    "DEFINE FIELD lit ON tdl_rt_kinds TYPE 'a' | 'b'; "
    "DEFINE FIELD ie ON tdl_rt_kinds TYPE 1 | 2; "
    "DEFINE FIELD opt ON tdl_rt_kinds TYPE option<int>; "
    "DEFINE FIELD un ON tdl_rt_kinds TYPE int | string; "
    "DEFINE FIELD anyv ON tdl_rt_kinds TYPE any; "
    "DEFINE FIELD geo ON tdl_rt_kinds TYPE option<geometry<point>>; "
    "DEFINE FIELD rng ON tdl_rt_kinds TYPE option<range>; "
    "DEFINE FIELD `first-name` ON tdl_rt_kinds TYPE string; "
)

#: SurrealDB 3.x: a ``COMPUTED`` field beside the ``VALUE`` one.
_RT_COMPUTED = "DEFINE FIELD c ON tdl_rt TYPE option<int> COMPUTED a + 1; "

#: The record the source database holds.
_RT_RECORD = "CREATE tdl_rt:1 SET a = 1 RETURN NONE;"

#: The tables of the round trip, in codepoint order.
_RT_TABLES = ["tdl_rt", "tdl_rt_any", "tdl_rt_kinds"]

#: What a field's definition is compared on.
_RT_FIELD_ATTRIBUTES = ("field_type", "nullable", "default", "value", "computed", "assertion")


def _import_generated(path: Path) -> ModuleType:
    """Import the module written at *path* (removed from ``sys.modules`` afterwards)."""
    name = f"generated_tables_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[name]
    return module


def _rt_clauses(state: TableState) -> tuple[object, ...]:
    """A stored table's type, mode, comment, change feed and permissions."""
    return (
        state.table_type,
        state.schema_mode,
        state.comment,
        state.changefeed,
        state.changefeed_include_original,
        dict(state.permissions),
    )


async def _rt_schema(client: Any) -> dict[str, tuple[TableState, dict[tuple[str, ...], FieldState]]]:
    """Each round-trip table's stored definition and fields, keyed by path."""
    tables = await _tables(client)
    return {table: (parse_define_table(tables[table]), _field_states(await _table_info(client, table))) for table in _RT_TABLES}


@contextlib.asynccontextmanager
async def _tdl2(kind: str) -> AsyncIterator[Any]:
    """The connection ``"tdl2"`` on *kind*, on a fresh database ``tdl_db2``; removed, then unregistered, on exit."""
    _register_tdl(kind, "tdl2", database="tdl_db2")
    client = await SurrealDBConnectionManager.get_client("tdl2")
    await _apply_ddl(client, "REMOVE DATABASE IF EXISTS tdl_db2; DEFINE DATABASE tdl_db2;")
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            cleanup = await SurrealDBConnectionManager.get_client("tdl2")
            await cleanup.query("REMOVE DATABASE IF EXISTS tdl_db2;", {})
        await SurrealDBConnectionManager.remove_connection("tdl2")


class TestRoundTripE2E:
    """On every engine of both lines: ``generate_models_from_db()`` writes the table clauses as keys —
    ``table_type=TableType.ANY``, ``schema_mode=SchemaMode.SCHEMAFULL``, ``changefeed=``, ``permissions=``,
    ``comment=`` — and each generated model's ``define_table()`` on a fresh database defines its table with the same
    type, mode, comment, change feed and permissions, and every field with the same kind, except what a model does
    not hold: a typed record link is written ``record``, a ``DEFAULT`` on an optional field or a server ``DEFAULT`` is
    not written (the field is optional in the model), an ``ASSERT`` is not written, and a ``COMPUTED`` field
    (SurrealDB 3.x) is written ``VALUE`` on a database that does not define it. Over the database it was read from,
    ``define_table()`` keeps the ``COMPUTED`` field ``COMPUTED`` and the record keeps its value; the ``VALUE`` field,
    every ``DEFAULT`` and the ``ASSERT`` stay."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_generated_models_define_the_same_tables(self, kind: str, tmp_path: Path) -> None:
        async with _tdl(kind) as client:
            has_computed = await _major(client) >= 3
            await _apply_ddl(client, _RT_SCHEMA + (_RT_COMPUTED if has_computed else "") + _RT_RECORD)
            source = await _rt_schema(client)

            path = tmp_path / "tdl_rt_models.py"
            text = await generate_models_from_db(path, "tdl", tables=_RT_TABLES)
            for comment in ("# Comment:", "# Changefeed:", "# Permissions:"):
                assert comment not in text, comment
            module = _import_generated(path)
            models: list[type[BaseSurrealModel]] = [module.TdlRt, module.TdlRtAny, module.TdlRtKinds]
            rt, rt_any, rt_kinds = models
            assert (rt.get_table_type(), rt.get_schema_mode()) == (TableType.NORMAL, SchemaMode.SCHEMAFULL)
            assert (rt_any.get_table_type(), rt_any.get_schema_mode()) == (TableType.ANY, None)
            assert (rt_kinds.get_table_type(), rt_kinds.get_schema_mode()) == (TableType.NORMAL, SchemaMode.SCHEMAFULL)
            assert rt.get_changefeed() == "1d INCLUDE ORIGINAL"
            assert rt.model_config["comment"] == "round trip"
            assert schema.permissions_rules(rt.get_permissions(), model=rt) == source["tdl_rt"][0].permissions
            assert rt.get_computed_fields() == ({"c": "a + 1", "v": "a + 1"} if has_computed else {"v": "a + 1"})

            # (1) On a fresh database.
            async with _tdl2(kind) as fresh_client:
                async with SurrealDBConnectionManager.using("tdl2"):
                    for model in models:
                        assert isinstance(await model.define_table(), str)
                fresh = await _rt_schema(fresh_client)
            for table in _RT_TABLES:
                assert _rt_clauses(fresh[table][0]) == _rt_clauses(source[table][0]), table
            differences: dict[tuple[str, tuple[str, ...], str], tuple[object, object]] = {}
            for table in _RT_TABLES:
                before, after = source[table][1], fresh[table][1]
                assert set(after) == set(before), (table, set(after) ^ set(before))
                for field_path, field in before.items():
                    for attribute in _RT_FIELD_ATTRIBUTES:
                        pair = getattr(field, attribute), getattr(after[field_path], attribute)
                        if pair[0] != pair[1]:
                            differences[(table, field_path, attribute)] = pair
            expected = {
                ("tdl_rt", ("rec",), "field_type"): ("record<tdl_rt_any>", "record"),
                ("tdl_rt", ("age",), "default"): ("18", None),
                ("tdl_rt", ("created",), "nullable"): (False, True),
                ("tdl_rt", ("created",), "default"): (source["tdl_rt"][1][("created",)].default, None),
                ("tdl_rt", ("a",), "assertion"): (source["tdl_rt"][1][("a",)].assertion, None),
            }
            if has_computed:
                expected[("tdl_rt", ("c",), "computed")] = ("a + 1", None)
                expected[("tdl_rt", ("c",), "value")] = (None, "a + 1")
            assert differences == expected

            # (2) Over the database the models were read from.
            before = source["tdl_rt"][1]
            for model in models:
                assert isinstance(await model.define_table(), str)
            after = _field_states(await _table_info(client, "tdl_rt"))
            for field_path, field in before.items():
                kept = (field.default, field.value, field.computed, field.assertion)
                assert (
                    after[field_path].default,
                    after[field_path].value,
                    after[field_path].computed,
                    after[field_path].assertion,
                ) == kept, field_path
            assert await client.query("SELECT VALUE v FROM tdl_rt:1;", {}) == [2]
            if has_computed:
                assert (after[("c",)].computed, after[("c",)].value) == ("a + 1", None)
                assert await client.query("SELECT VALUE c FROM tdl_rt:1;", {}) == [2]


# === README: section 35, the sections it changes, the tables ========================================================

_SECTION_16 = "### 16. Computed fields: `Computed[...]` → `DEFINE FIELD … VALUE`"
_SECTION_19 = "### 19. Model-level authentication (`AuthenticatedUserMixin`)"
_SECTION_31 = "### 31. Schema introspection (`DatabaseIntrospector`, `generate_models_from_db()`)"
_SECTION_33 = "### 33. Materialized views (`view_query`, `define_view()`)"
_SECTION_34 = "### 34. Relation constraints (`relation_in`, `define_relation()`)"
_SECTION_35 = "### 35. Model table DDL (`define_table()`, `schema_mode`, `permissions`)"
_SERVER_BEHAVIOUR = "### ORM behaviour: SurrealDB 2.x vs 3.x"
_EMBEDDED_BEHAVIOUR = "### ORM behaviour: the embedded engine"
#: Section 35's anchor, as the sections that point to it write it.
_LINK_35 = "(#35-model-table-ddl-define_table-schema_mode-permissions)"
#: Section 35's subsections, in order, after an untitled intro that holds example A.
_SUBSECTIONS_35 = [
    "Declaring the table",
    "Field types",
    "Applying it",
    "Views and edges",
    "User models",
    "What SurrealDB checks",
    "SurrealDB 2.x and 3.x",
    "Transactions, connections and the cache",
    "Security",
    "Coming from the full SurrealDB-ORM",
]
_README_EXAMPLE_A_35 = """\
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from surreal_orm_lite import BaseSurrealModel, SchemaMode, SurrealConfigDict


class Tier(StrEnum):
    FREE = "free"
    PRO = "pro"


class Address(BaseModel):
    city: str
    zip: str | None = None


class Person(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_name="person",
        schema_mode=SchemaMode.SCHEMAFULL,
        changefeed="7d",
        permissions={"select": "FULL", "create": "$auth.id != NONE"},
        comment="People",
    )

    id: str | None = None
    name: str
    age: int = 18
    nickname: str | None = None
    tier: Tier = Tier.FREE
    tags: list[str] = Field(default_factory=list)
    address: Address | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    password: str = Field(alias="password_hash")


print(*Person.table_ddl(), sep="\\n")
# DEFINE TABLE OVERWRITE `person` TYPE NORMAL SCHEMAFULL COMMENT 'People' CHANGEFEED 7d PERMISSIONS FOR select FULL, FOR create WHERE $auth.id != NONE, FOR update, delete NONE;
# DEFINE FIELD OVERWRITE `name` ON `person` TYPE string;
# DEFINE FIELD OVERWRITE `age` ON `person` TYPE int DEFAULT 18;
# DEFINE FIELD OVERWRITE `nickname` ON `person` TYPE option<string>;
# DEFINE FIELD OVERWRITE `tier` ON `person` TYPE 'free' | 'pro' DEFAULT 'free';
# DEFINE FIELD OVERWRITE `tags` ON `person` TYPE array<string>;
# DEFINE FIELD OVERWRITE `address` ON `person` TYPE option<object>;
# DEFINE FIELD OVERWRITE `address`.`city` ON `person` TYPE string;
# DEFINE FIELD OVERWRITE `address`.`zip` ON `person` TYPE option<string>;
# DEFINE FIELD OVERWRITE `meta` ON `person` TYPE object FLEXIBLE;
# DEFINE FIELD OVERWRITE `password_hash` ON `person` TYPE string;
sql = await Person.define_table()  # idempotent: run it at start-up
print(sql == "\\n".join(Person.table_ddl()))
# True

await Person(id="ada", name="Ada", address=Address(city="Paris"), meta={"plan": {"seats": 3}}, password="hash").save()
ada = await Person.objects().get("ada")
print(ada.age, ada.tier, ada.address.city, ada.meta)
# 18 free Paris {'plan': {'seats': 3}}
"""
_README_GETTERS_35 = """\
print(Person.get_schema_mode(), Person.get_changefeed(), Person.get_permissions())
# SCHEMAFULL 7d {'select': 'FULL', 'create': '$auth.id != NONE'}
"""
_README_EXAMPLE_B_35 = """\
from datetime import datetime

from surreal_orm_lite import SurrealDBConnectionManager


class Note(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="note", server_fields=["created"])

    id: str | None = None
    body: str
    email: str | None = None
    created: datetime | None = None
    done: bool = False


client = await SurrealDBConnectionManager.get_client()
await client.query(  # a table tuned by hand
    "DEFINE TABLE note SCHEMAFULL COMMENT 'keep' CHANGEFEED 1h PERMISSIONS FOR select WHERE owner = $auth.id;"
    "DEFINE FIELD email ON note TYPE string ASSERT $value != '' COMMENT 'mail';"
    "DEFINE FIELD created ON note TYPE option<datetime> DEFAULT time::now() READONLY;",
    {},
)
print(await Note.define_table())
# DEFINE TABLE OVERWRITE `note` TYPE NORMAL SCHEMAFULL COMMENT 'keep' CHANGEFEED 1h PERMISSIONS FOR select WHERE owner = $auth.id, FOR create, update, delete NONE;
# DEFINE FIELD OVERWRITE `body` ON `note` TYPE string;
# DEFINE FIELD OVERWRITE `email` ON `note` TYPE option<string> ASSERT $value != '' COMMENT 'mail';
# DEFINE FIELD OVERWRITE `created` ON `note` TYPE option<datetime> DEFAULT time::now() READONLY;
# DEFINE FIELD OVERWRITE `done` ON `note` TYPE bool DEFAULT false;
note = await Note(id="n1", body="b").save()
print(note.created is not None, note.done)
# True False
"""
_README_VIEW_35 = """\
from surrealdb import RecordID


class NoteCount(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_name="note_count", view_query="SELECT count() AS n FROM note GROUP ALL", permissions={"select": "FULL"}
    )

    id: RecordID | None = None
    n: int


print(await NoteCount.define_table())
# DEFINE TABLE OVERWRITE `note_count` AS (SELECT count() AS n FROM note GROUP ALL) PERMISSIONS FOR select FULL, FOR create, update, delete NONE;
"""
_README_EDGE_35 = """\
from surreal_orm_lite import BaseSurrealEdge, SurrealDbError


class Rates(BaseSurrealEdge):
    model_config = SurrealConfigDict(
        table_name="rates", relation_in="person", relation_out="note", schema_mode=SchemaMode.SCHEMAFULL
    )

    stars: int = 1


try:
    await Rates.define_relation()  # it defines no field, which this SCHEMAFULL table needs
except SurrealDbError as exc:
    print(type(exc).__name__)
# SurrealDbError
print(await Rates.define_table())
# DEFINE TABLE OVERWRITE `rates` TYPE RELATION IN `person` OUT `note` SCHEMAFULL;
# DEFINE FIELD OVERWRITE `stars` ON `rates` TYPE int DEFAULT 1;
edge = await ada.relate(Rates, note, stars=5)
print(edge.stars)
# 5
"""
_README_USER_35 = """\
from surreal_orm_lite import AuthenticatedUserMixin, TableType


class Member(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="member", table_type=TableType.USER, access_name="member_access")

    id: str | None = None
    email: str
    password: str


*table, access = Member.access_ddl()
print(*table, sep="\\n")
# DEFINE TABLE OVERWRITE `member` TYPE NORMAL SCHEMAFULL PERMISSIONS FOR select, update WHERE id = $auth.id, FOR create, delete NONE;
# DEFINE FIELD OVERWRITE `email` ON `member` TYPE string;
# DEFINE FIELD OVERWRITE `password` ON `member` TYPE string;
"""
_README_EXAMPLES_35 = [
    _README_EXAMPLE_A_35,
    _README_GETTERS_35,
    _README_EXAMPLE_B_35,
    _README_VIEW_35,
    _README_EDGE_35,
    _README_USER_35,
]
#: The ``python`` blocks of each part of section 35 (the intro is the text before the first subsection).
_BLOCKS_BY_PART_35 = {
    "": [_README_EXAMPLE_A_35],
    "Declaring the table": [_README_GETTERS_35],
    "Field types": [],
    "Applying it": [_README_EXAMPLE_B_35],
    "Views and edges": [_README_VIEW_35, _README_EDGE_35],
    "User models": [_README_USER_35],
    "What SurrealDB checks": [],
    "SurrealDB 2.x and 3.x": [],
    "Transactions, connections and the cache": [],
    "Security": [],
    "Coming from the full SurrealDB-ORM": [],
}
#: The sentences section 35 says word for word (whitespace normalised), each once, by subsection.
_SENTENCES_35 = {
    "Field types": (
        "Anything else is `any`.",
        "A computed field is written `VALUE`; over a field the database defines `COMPUTED` (SurrealDB 3.x), "
        "`define_table()` writes `COMPUTED`: replacing it by `VALUE` would leave the stored records without the field "
        "until their next write.",
        "A `server_fields` column that is not nullable and has no constant default (a `default_factory`, or a required "
        "field) is written `TYPE <kind>` with no `DEFAULT`: every create — a `save()` that creates the record, "
        "`objects().bulk_create()`, `signup()` — leaves it out, so SurrealDB refuses it until the database gives the "
        "column a `DEFAULT` or `VALUE` (`DEFINE FIELD OVERWRITE created ON note TYPE datetime DEFAULT time::now()`), "
        "which `define_table()` then keeps; or annotate it `T | None`.",
    ),
    "Applying it": (
        'It returns the statements applied (queued, with a buffered `tx`), table first, joined with `"\\n"` — the full '
        'ORM\'s return. On a new table that is `"\\n".join(Model.table_ddl())`, except on SurrealDB 2.x, where '
        "`define_table()` adds `FLEXIBLE` to the `Any`/untyped columns of a `SCHEMAFULL` table.",
        "Each call on a table with a change feed adds one `define_table` entry to it; `changes()` skips it, a raw "
        "`SHOW CHANGES` shows it.",
    ),
    "Views and edges": (
        "**Changed**: `define_view()` keeps the permissions, comment, change feed, `DROP` and `SCHEMAFULL` set by hand "
        "on a stored view unless the model declares them (it used to reset them), and when its statement writes "
        "permissions, a refusal that quotes the statement is reported without it.",
        "An edge model that declares `schema_mode=SchemaMode.SCHEMAFULL` and has data fields is applied with "
        "`define_table()`: `define_relation()` defines no field and raises before any request.",
    ),
    "User models": (
        "`define_access()` ([section 19](#19-model-level-authentication-authenticatedusermixin)) and `define_table()` "
        "read the same schema mode. A model with `table_type=TableType.USER` (the full ORM's key for an auth table) or "
        "`schema_mode=SchemaMode.SCHEMAFULL` gets from `define_access()` a `SCHEMAFULL` table with one `DEFINE FIELD` "
        "per field and the access's permissions, then the access: signup and signin work on SurrealDB 2.x and 3.x, "
        "`define_table()` is optional, and any call order gives the same table. Every other model keeps v0.17.0's "
        "`SCHEMALESS` table.",
    ),
    "Transactions, connections and the cache": (
        'A definition of the same table or field queued earlier in the block (`tx.add("DEFINE FIELD …")`) is not seen '
        "by `define_table()` and is replaced at commit by the statement it builds: define it before the block.",
    ),
}
#: What the intro and each subsection of section 35 say (key phrases, whitespace normalised).
_INTRO_NEEDLES_35 = (
    "Since v0.34.0",
    "the full SurrealDB-ORM's keys",
    "`schema_mode`, `changefeed`, `permissions`, `comment` and `flexible_fields`",
    "one `DEFINE FIELD` per column from the annotations",
    "keeping what the model does not declare",
    "SurrealDB 2.x, 3.x and the embedded engine",
    "(#orm-behaviour-surrealdb-2x-vs-3x)",
    "`mem://`",
)
_PART_NEEDLES_35 = {
    "Declaring the table": (
        "`SchemaMode.SCHEMAFULL`",
        "`SchemaMode.SCHEMALESS`",
        "the mode is not declared: `define_table()` keeps the stored one, and a new table is `SCHEMALESS`",
        '`"7d"`, `"1h30m"` or `"1w2d"`',
        "`INCLUDE ORIGINAL`",
        '`"select"`, `"create"`, `"update"`, `"delete"`',
        '`"WHERE <condition>"` or a bare condition',
        "An operation left out is `NONE`",
        "`permissions={}` declares `PERMISSIONS NONE`",
        "`COMMENT 'it\\'s'`",
        '`Field(json_schema_extra={"flexible": True})`',
        "raises `ValueError`",
        "`TYPE NORMAL` for `TableType.NORMAL`, `USER`, `STREAM` and `HASH` and for a model without `table_type`",
        "`TYPE ANY` for `TableType.ANY`",
        "`USER` makes the table `SCHEMAFULL` and `HASH` makes it `SCHEMALESS`",
        "checked when the class is defined",
        '`schema_mode`, even `"SCHEMAFULL"`, included',
        '`schema_mode=SchemaMode.SCHEMAFULL` with `extra="allow"`',
        "`TableType.USER` with `SchemaMode.SCHEMALESS`",
        "A subclass inherits the keys; setting one to `None` resets it",
        "`get_schema_mode()` returns the declared mode",
        "else `None` (not declared)",
        '`get_changefeed()` returns the normalised text (`"1h INCLUDE ORIGINAL"`',
        "`get_permissions()` returns the declared mapping as given",
        "a copy, so changing it never changes the model",
        "`{}` when the model declares none, and for `permissions={}`, which declares `PERMISSIONS NONE`",
    ),
    "Field types": (
        "`Annotated` unwrapped",
        "every field but `id`",
        "`@computed_field`s",
        "`Field(exclude=True)`",
        "(`address.city`, `subs.*.city`)",
        "(`x: int = None`)",
        "`DEFAULT 1.50dec`",
        "SurrealDB applies `DEFAULT` to a `NONE` on every create",
        "the database's `DEFAULT`, `VALUE` or `COMPUTED` on the column is kept",
        "`Computed[T]` is `TYPE option<T>`",
        "The expression is checked before any request",
        "`ValueError`",
        "`TYPE any` (`bag.*`, `lol.*.*`), on every line",
        "required untyped array (`no such field exists`)",
        "writes each of its lists, sets and tuples as an untyped `array`, which gets its item definition",
        "`dict[str, Any] | list[str]` is `object | array` with `dl.*` `TYPE any`, `list[int] | list[str]` is `array`",
        'so `object | array<string>` would refuse `{"k": 1}`',
        "written after `TYPE`, only on a table that is `SCHEMAFULL`",
        "on SurrealDB 2.x and the embedded engine, on the columns that hold `any`",
        "A stored `FLEXIBLE` on an object column is kept",
    ),
    "Applying it": (
        "one `INFO FOR DB`, and one `INFO FOR TABLE` for a table that exists",
        "one statement per call",
        "parents before children",
        "applied by hand, their `OVERWRITE` resets every clause the model does not write",
        "What the model declares wins; the rest is kept from the database",
        "Nothing is removed",
        "the views that read it ([section 33](#33-materialized-views-view_query-define_view))",
        "its live queries and its change feed's history",
        "A key left out never removes a stored clause",
        "migrations come with v0.38.0",
        "`schema_mode=SchemaMode.SCHEMALESS` turns a `SCHEMAFULL` table back",
        "idempotent: run it at start-up",
        "`overwrite=False` writes `IF NOT EXISTS`",
        "raises `SurrealDbError` on a materialized view",
        "on a relation table (`TYPE RELATION`",
        "cannot read back or carry",
        "`Can't apply field definition: 'age' on table 'person' -> …`",
        "never the statement",
        "`tx=` makes the call atomic",
    ),
    "Views and edges": (
        "A view model's `define_table()` applies `define_view()`",
        "`[view_ddl()]`",
        "No field is defined on a view",
        "the relation statement `define_relation()` carries",
        "never `in` and `out`",
        "The declared clauses reach `view_ddl()` and `relation_ddl()`",
        "a model without the keys renders the same statements as before",
        "`table_type`, `schema_mode` and `flexible_fields` raise `TypeError` there",
    ),
    "User models": (
        "`define_access()` writes the access's permissions, as in v0.17.0",
        "`define_table()` keeps them unless the model declares its own",
        'A `TableType.USER` model with `extra="allow"` raises `TypeError` there (with `with_table=True`)',
        "a `server_fields` column without a constant default needs a database `DEFAULT`",
        "a 0.33 `TableType.USER` model's table turns `SCHEMAFULL` at its next `define_access()`",
        "records already there are checked at their next write",
    ),
    "What SurrealDB checks": (
        "**Records already in the table**",
        "SurrealDB checks a record at its next write",
        "reads back unchanged",
        "**A write naming a field the table does not define**",
        "`merge()` with a key the model does not declare, `bulk_update()`, a raw query",
        "refused on SurrealDB 3.x",
        "`Found field 'nope', but no such field exists for table 'note'`",
        "dropped on 2.x and the embedded engine, silently: declare the field on the model",
        "**A node model over a table that holds edges**",
        "declare `table_type=TableType.ANY` for a table that holds records and edges",
    ),
    "SurrealDB 2.x and 3.x": (
        "SurrealDB 2.x and the embedded engine empty the objects and arrays of an `any` column",
        "optional or not, silently",
        "`FLEXIBLE` keeps them",
        "3.x keeps them without it, and refuses `FLEXIBLE` on such a column",
        "`client.version()`",
        "only when the table is `SCHEMAFULL` and a column holds `any`",
        "`table_ddl()`, which needs no connection, writes the 3.x form",
        "the item definition `TYPE any` (`bag.*`) on every line",
        "refused on 3.x and dropped on 2.x and the embedded engine",
        "`COMPUTED` fields are SurrealDB 3.x's",
        "(#orm-behaviour-surrealdb-2x-vs-3x)",
        "(#orm-behaviour-the-embedded-engine)",
    ),
    "Transactions, connections and the cache": (
        "`tx=`",
        "before anything is queued, on committed state",
        "interactive",
        "buffered",
        "the transaction's own `SurrealDbError`",
        "A block that raises applies nothing",
        "not a lock",
        "`connection`",
        "`using()`",
        "another connection raises `SurrealDbError` before any request",
        "(#29-query-cache-cache-querycache)",
        "drops the model's cached reads",
        "`define_events()` runs before or after",
        "`define_computed_fields()` still writes `VALUE` alone",
        "`OWNER` or `EDITOR`",
        "`SurrealDbAuthenticationError`",
        "fires no signal",
    ),
    "Security": (
        "written in backquotes",
        "never from text",
        "closed SurrealQL encoder",
        "checked when the class is defined",
        "A computed field's expression gets the same check before any request",
        "would swallow the `ASSERT`, `PERMISSIONS` and `COMMENT` written after it",
        "they are not an injection defence",
        "trusted application code, never user input",
        "never reset stored permissions to SurrealDB's defaults",
        "names the table and the field, never the statement",
        "redacted when its statement writes permissions",
        "privileged session",
    ),
}
#: One key phrase per "Coming from the full SurrealDB-ORM" bullet, in order (the accepted differences).
_COMING_FROM_PHRASES_35 = (
    "**The same keys, `SchemaMode` and getters**",
    "**Not `SCHEMAFULL` by default**",
    "**Declared wins, the rest is kept from the database**",
    '**`define_table()` returns the statements joined with `"\\n"`**',
    "**One statement per call**",
    "**`TYPE NORMAL` is written for `TableType.NORMAL` and for a model without `table_type`**",
    "**`FLEXIBLE` is written after `TYPE`**",
    "**A view model's `define_table()` defines no field**",
    "**Kinds for `datetime`, `Decimal`, `UUID`, durations, geometries, enums, literals and sub-models**",
    "**No `DEFAULT` on a nullable field**",
    "**`changefeed` may carry `INCLUDE ORIGINAL`.**",
    "**`table_type`, `schema_mode` and `flexible_fields` are refused on a view model.**",
    "**`TableType.USER` makes the table `SCHEMAFULL` in `define_table()`**",
    "**Upgrading from 0.33 or earlier**",
)
#: Sections 16 and 19: what they say of ``define_table()``, word for word (whitespace normalised).
_SECTION_16_SENTENCE = (
    f"Since v0.34.0, `define_table()` ([section 35]{_LINK_35}) writes a computed field's `TYPE` too "
    "(`TYPE option<T> VALUE …`), checks its expression, and keeps a field the database defines `COMPUTED` (SurrealDB "
    "3.x) `COMPUTED`; `define_computed_fields()` turns such a field into `VALUE`."
)
_SECTION_19_SENTENCES = (
    "Since v0.34.0, a user model whose schema mode is `SCHEMAFULL` — `table_type=TableType.USER` (the full ORM's key "
    "for an auth table) or `schema_mode=SchemaMode.SCHEMAFULL` — gets from `define_access()` a `SCHEMAFULL` table with "
    "one `DEFINE FIELD` per field (section 35); every other model keeps the `SCHEMALESS` table printed above. "
    "`define_table()` gives such a model the same table, so the two can be called in any order."
)
#: Section 31: the generated-models bullet and the keys its "One class per table" bullet lists.
_GENERATED_MODELS_BULLET_35 = (
    "- **Generated models use the full ORM's keys** (since v0.34.0): `table_type=TableType.ANY` for a `TYPE ANY` table, "
    "`schema_mode=SchemaMode.SCHEMAFULL`, `changefeed=…`, `permissions={…}` and `comment=…`, as the full ORM writes "
    "them (it writes no `schema_mode` or `comment`); a view with `view_query=…` since v0.32.0 and a relation table's "
    "ends with `relation_in=…`, `relation_out=…` and `enforced=True` since v0.33.0. A comment stays where the class "
    "statement would refuse the key (`# View: AS …`, `# IN … OUT …`, `# Permissions: …` for a rule it cannot embed). "
    "The base class says `RELATION` (`BaseSurrealEdge`). `READONLY` is a comment (full ORM: `Field(readonly=True)`)."
)
_ONE_CLASS_PER_TABLE_35 = (
    "the table's clauses — `table_type=TableType.ANY` for a `TYPE ANY` node table, `schema_mode=SchemaMode.SCHEMAFULL`, "
    f"`changefeed=`, `permissions={{…}}` and `comment=` (since v0.34.0, [section 35]{_LINK_35}) —"
)
#: Section 33: its Security sentences on a view's permissions, and its "Coming from" bullet on ``define_table()``.
_SECTION_33_PERMISSIONS = (
    "A view takes the permissions, comment and change feed its model declares (`permissions=`, `comment=`, "
    "`changefeed=`, since v0.34.0); `define_view()` keeps those set by hand that the model does not declare. Without "
    "either, SurrealDB applies its default `PERMISSIONS NONE`: a record user reads nothing from a view."
)
_SECTION_33_DEFINE_TABLE = (
    f"`define_table()` (since v0.34.0, [section 35]{_LINK_35}) accepts a view model and applies it through "
    "`define_view()`, so full-ORM code calling `define_table()` on a view works."
)
#: Section 34: the sentences on the clauses an edge model declares and on ``TableType``'s other members.
_SECTION_34_DECLARED = (
    "- **The clauses the model declares (`schema_mode`, `permissions`, `changefeed`, `comment`, since v0.34.0) are "
    "written, the others kept from the database** (the full ORM renders every clause from the model and resets the "
    "others)."
)
_SECTION_34_TABLE_TYPE = f"its other members take effect in `define_table()` ([section 35]{_LINK_35})"
_SECTION_34_DEFINE_TABLE = (
    "`define_table()` (since v0.34.0) applies an edge model through the statement `define_relation()` carries, then its "
    "data fields; migrations come with v0.38.0."
)
#: Exactly the three rows v0.34.0 adds to the 2.x/3.x table, cell by cell, within the table's column widths.
_TABLE_DDL_ROWS = [
    [
        "Model table DDL (`define_table()`: `DEFINE TABLE` + `DEFINE FIELD`)",
        "same on both lines: one `DEFINE TABLE OVERWRITE` and one `DEFINE FIELD OVERWRITE` per column, keeping what the "
        "model does not declare",
        "the same",
        "v0.34.0",
    ],
    [
        "An `Any` or untyped `list`/`set` field of a `SCHEMAFULL` model",
        "`define_table()` adds `FLEXIBLE` (read from `client.version()`): without it these engines empty such values, "
        "optional or not; the item definitions as on 3.x",
        "kept without `FLEXIBLE` (refused there); an untyped `list` gets an item definition `TYPE any`, without which 3.x "
        "refuses an object item of a required one (`no such field exists`)",
        "v0.34.0",
    ],
    [
        "A write naming a field a `SCHEMAFULL` table does not define (an undeclared `merge()` key, a raw query, records "
        "predating `SCHEMAFULL`)",
        "the field is dropped, silently",
        "the write is refused (`no such field exists`)",
        "v0.34.0",
    ],
]
_HEADER_SENTENCE_34 = (
    "Rows added in v0.34.0 were measured on 2.7.0, 3.3.4 and the embedded engine; 3.1.5 behaves as 3.3.4 except that it "
    "refuses a Python `set` written to a `set` field, and an object item of an optional untyped `list`."
)
#: The row v0.34.0 adds to the embedded engine's table, cell by cell.
_EMBEDDED_TABLE_DDL_ROW = [
    "Model table DDL (v0.34.0)",
    "as on a 2.x server: `define_table()` adds `FLEXIBLE` to the `Any` and untyped columns of a `SCHEMAFULL` table; a "
    "write naming a field the table does not define drops that field",
    "3.x keeps those values without `FLEXIBLE` and refuses that write",
    "v0.34.0",
]
_FEATURES_ROW_35 = ["Model table DDL (`define_table()`, `schema_mode`, `permissions`)", "✅"]
_ROADMAP_ROWS_35 = [
    ["v0.33.0", "Tier 3 — `TYPE RELATION` constraints", "✅ Released"],
    ["v0.34.0", "Tier 3 — Model table DDL", "✅ Released"],
    ["v0.35.0 – v0.40.0", "Tier 3 — Advanced (search, migrations, CLI)", "📋 Planned"],
    ["v0.41.0", "Beta Phase (API freeze, hardening)", "📋 Planned"],
]
_COMPARISON_ROW_35 = ["Model table DDL", "✅ v0.34.0 (`define_table()`)", "✅"]


def _section_35() -> str:
    return _readme_part(_SECTION_35)


def _part_35(heading: str) -> str:
    """One ``####`` subsection of section 35, as written (the intro for ``""``)."""
    if not heading:
        return _section_35().split("\n#### ", 1)[0]
    return _subsection(f"#### {heading}", _section_35())


def _readme_blocks(text: str) -> list[str]:
    """The ``python`` blocks of *text*, in order."""
    blocks: list[str] = re.findall(r"^```python\n(.*?)^```$", text, re.MULTILINE | re.DOTALL)
    return blocks


def _readme_printed(block: str) -> list[str]:
    """What a README example prints: its comment lines that start a line (``# <printed line>``), in order."""
    return re.findall(r"^# (.*)$", block, re.MULTILINE)


def _readme_bullets(text: str) -> list[str]:
    """The ``- `` bullets of *text*, each with its continuation lines, whitespace normalised."""
    return [_normalised(bullet) for bullet in re.findall(r"^- .*?(?=^- |^\s*$|\Z)", text, re.MULTILINE | re.DOTALL)]


def _anchor(heading: str) -> str:
    """GitHub's anchor of a README heading: lower case, punctuation but ``-`` and ``_`` dropped, spaces as ``-``."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


class TestReadmeSection35:
    """README section 35 and what v0.34.0 changes elsewhere in the README: section 35's place, subsections, examples
    (run on ``mem://`` as written, each printed line equal to its comment), sentences and tables; the sentences of
    sections 16, 19, 31, 33 and 34; the features, roadmap, comparison and both behaviour tables."""

    def test_section_35_follows_section_34(self) -> None:
        """Right after section 34 (after its "Coming from" bullets) and before the ``---`` of "Configuration Options"."""
        text = _readme()
        start_34 = text.index(f"\n{_SECTION_34}\n")
        start_35 = text.index(f"\n{_SECTION_35}\n")
        configuration = text.index("\n## Configuration Options\n")
        assert start_34 < start_35 < configuration
        assert re.findall(r"^#{2,3} .*$", text[start_34 + 1 : configuration], re.MULTILINE) == [
            _SECTION_34,
            _SECTION_35,
        ]
        assert text[:configuration].rstrip().endswith("\n---"), "section 35 ends before the --- of the next part"

    def test_subsections_in_order(self) -> None:
        assert re.findall(r"^#### (.*)$", _section_35(), re.MULTILINE) == _SUBSECTIONS_35

    def test_the_examples_and_where_they_stand(self) -> None:
        """Example A exactly, the block of the untitled intro; example B exactly, the first thing of "Applying it";
        the getters, the view, the edge and the user model in their parts; no other block; ``python`` fences and the
        tables' fences only."""
        section = _section_35()
        assert _readme_blocks(section) == _README_EXAMPLES_35
        for heading, blocks in _BLOCKS_BY_PART_35.items():
            assert _readme_blocks(_part_35(heading)) == blocks, heading
        part = _part_35("Applying it")
        assert part.split("\n\n", 1)[1].startswith(f"```python\n{_README_EXAMPLE_B_35}```"), part
        fences = re.findall(r"^```(\S*)$", section, re.MULTILINE)
        assert fences == ["python", ""] * len(_README_EXAMPLES_35), fences

    def test_intro_paragraph(self) -> None:
        intro = _normalised(_section_35().split("\n```python\n", 1)[0])
        missing = [needle for needle in _INTRO_NEEDLES_35 if needle not in intro]
        assert not missing, missing

    @pytest.mark.parametrize("heading", list(_PART_NEEDLES_35))
    def test_what_each_subsection_says(self, heading: str) -> None:
        text = _normalised(_part_35(heading))
        missing = [needle for needle in _PART_NEEDLES_35[heading] if needle not in text]
        assert not missing, missing

    @pytest.mark.parametrize("heading", list(_SENTENCES_35))
    def test_sentences_word_for_word(self, heading: str) -> None:
        """Each sentence exactly once in its subsection, and nowhere else in the section."""
        section = _normalised(_section_35())
        part = _normalised(_part_35(heading))
        for sentence in _SENTENCES_35[heading]:
            assert part.count(sentence) == 1, sentence
            assert section.count(sentence) == 1, sentence

    def test_no_prescribed_order_and_no_list_return(self) -> None:
        """``define_table()`` returns a ``str`` and needs no call order with ``define_access()``: neither section 35
        nor section 19 says otherwise."""
        for text in (_normalised(_section_35()), _normalised(_readme_part(_SECTION_19))):
            for claim in ("before `define_table()`", "returns a list", "after `define_access()`"):
                assert claim not in text, claim

    def test_no_local_reference(self) -> None:
        """The transaction limit is described in words: no design reference, no issue number."""
        section = _section_35()
        for marker in ("§", r"\bspec\b", r"\.claude"):
            assert re.search(marker, section) is None, marker
        assert re.findall(r"(?<![(\w])#\d+", _part_35("Transactions, connections and the cache")) == []

    def test_field_types_table(self) -> None:
        """Two columns, no pipe in a cell (a union is written in prose); a row for each family of annotations."""
        [lines] = _md_tables(_part_35("Field types"))
        assert _cells(lines[0]) == ["Python annotation", "SurrealQL kind"]
        rows = dict(cells for _, cells in _table_rows(_part_35("Field types")))
        assert len({len(_cells(line)) for line in lines}) == 1 and "\\|" not in "".join(lines), lines
        assert rows["a union of `int`, `float` and `Decimal`, in any order"] == "`number`", rows
        assert rows["`datetime`, `date`"] == "`datetime`", rows
        assert rows["`RecordID`"] == "`record`", rows
        assert rows["`list`, `list[Any]`"] == "`array`, and an item definition (below)", rows
        assert rows["`dict`, `dict[K, V]`, `Mapping[K, V]`"] == "`object`, free-form", rows
        assert rows["`Optional[X]`, a union with `None`"] == "`option<X>`", rows
        assert rows["any other union"] == (
            "the union of its members' kinds, `any` when one is `any`; a list or a set with an object or another "
            "container: `array` (below)"
        ), rows
        assert rows["`Any`, `object`, the SDK's `Datetime`"] == "`any`", rows

    def test_source_rule_tables(self) -> None:
        """The source rule in "Applying it", as two tables: the table's clauses, then each field's."""
        tables = _md_tables(_part_35("Applying it"))
        assert [_cells(lines[0]) for lines in tables] == [
            ["Table clause", "The model declares it", "The model leaves it out"],
            ["Field clause", "Written from the model", "Kept from the database"],
        ]
        table_rows = [_cells(line)[0] for line in tables[0][2:]]
        field_rows = [_cells(line)[0] for line in tables[1][2:]]
        assert table_rows == ["type", "schema mode", "`COMMENT`, `CHANGEFEED`", "`PERMISSIONS`", "`DROP`"]
        assert field_rows == [
            "`TYPE`",
            "`FLEXIBLE`",
            "`DEFAULT`",
            "`VALUE`, `COMPUTED`",
            "`REFERENCE`, `READONLY`, `ASSERT`, `PERMISSIONS`, `COMMENT`",
        ]
        assert _cells(tables[1][-1])[1:] == ["—", "always"]

    def test_coming_from_the_full_orm(self) -> None:
        """Fourteen bullets, one key phrase each, in order; the facts of the accepted differences."""
        bullets = _readme_bullets(_part_35("Coming from the full SurrealDB-ORM"))
        assert len(bullets) == len(_COMING_FROM_PHRASES_35), bullets
        for bullet, phrase in zip(bullets, _COMING_FROM_PHRASES_35, strict=True):
            assert phrase in bullet, (phrase, bullet)
        needles = [
            (0, "`get_permissions()` returns the declared mapping, as the full ORM's"),
            (0, '`get_changefeed()` returns the normalised text (`"1h INCLUDE ORIGINAL"`), equal to the declared string'),
            (0, "`get_schema_mode()` may be `None`"),
            (1, "the full ORM makes every table `SCHEMAFULL` unless told otherwise"),
            (2, "the full ORM resets every clause it does not render"),
            (3, "also takes `overwrite=` and `tx=`"),
            (4, "raises `SurrealDbError` naming the table or the field, never the statement"),
            (4, "`MigrationStatementError` (a `RuntimeError` from `surreal_orm.migrations`) with the SQL in its message"),
            (5, "SurrealDB then stores `TYPE NORMAL` for a `SCHEMAFULL` table"),
            (5, "`TYPE ANY` for a `SCHEMALESS` one"),
            (5, "declare `TableType.ANY` for a table that holds both records and edges"),
            (6, '("FLEXIBLE must be specified after TYPE")'),
            (6, "on SurrealDB 2.x to the columns that hold `any`"),
            (7, "the full ORM also writes one `DEFINE FIELD` per model field on the view's table"),
            (8, "an item definition `TYPE any` for an untyped `list`"),
            (8, "`record` for `RecordID`"),
            (12, "in `define_access()` (the lite's model-level auth), which then defines the model's fields too"),
            (13, "the five keys were accepted and ignored"),
            (13, "a plain-string `schema_mode` included"),
            (13, "`define_table()`, `define_relation()`, `define_view()` and `define_access()`"),
            (13, "A view model with `table_type` raises `TypeError`"),
            (13, "`define_view()` keeps a view's clauses set by hand"),
            (13, 'raise `TypeError` with `extra="allow"`'),
            (13, "generated models carry the keys"),
        ]
        assert [(index, needle) for index, needle in needles if needle not in bullets[index]] == []

    def test_links_resolve(self) -> None:
        """Every in-page link of section 35, and of the sentences that point to it, names a heading of the README."""
        anchors = {_anchor(heading) for heading in re.findall(r"^#{2,4} (.*)$", _readme(), re.MULTILINE)}
        pointers = _SECTION_16_SENTENCE + _ONE_CLASS_PER_TABLE_35 + _SECTION_33_DEFINE_TABLE + _SECTION_34_TABLE_TYPE
        links = re.findall(r"\]\(#([^)]+)\)", _section_35() + pointers)
        assert len(links) >= 12, links
        assert [link for link in links if link not in anchors] == []

    def test_section_16_sentence(self) -> None:
        assert _normalised(_readme_part(_SECTION_16)).count(_SECTION_16_SENTENCE) == 1

    def test_section_19_sentences(self) -> None:
        """Right after the ``with_table=False`` sentence."""
        section_19 = _normalised(_readme_part(_SECTION_19))
        opt_out = "`define_access(with_table=False)` opts out when you manage the table yourself."
        assert section_19.count(f"{opt_out} {_SECTION_19_SENTENCES}") == 1, section_19

    def test_section_31_generated_models(self) -> None:
        """The generated-models bullet exactly; the class prose lists the table keys and keeps the comments only where
        the class statement would refuse a key."""
        section_31 = _readme_part(_SECTION_31)
        bullets = _readme_bullets(_subsection("#### Coming from the full SurrealDB-ORM", section_31))
        assert _GENERATED_MODELS_BULLET_35 in bullets, [bullet for bullet in bullets if "Generated models" in bullet]
        assert not any("Generated models use comments" in bullet for bullet in bullets)
        generating = _normalised(_subsection("#### Generating models", section_31))
        assert _ONE_CLASS_PER_TABLE_35 in generating, generating
        assert "where the class statement would refuse the key" in generating, generating
        for stale in ("`# Changefeed: 1d`", "are comments of its class, and the module's header"):
            assert stale not in generating, stale
        limits = _normalised(_subsection("#### What the generator does not do", section_31))
        assert "only comments of its class" not in limits, limits
        assert "`define_table()` keeps them" in limits, limits

    def test_section_33_sentences(self) -> None:
        """Its Security paragraph says what a view's permissions are now, and ``define_table()`` is in the present."""
        section_33 = _readme_part(_SECTION_33)
        security = _normalised(_subsection("#### Security", section_33))
        assert security.count(_SECTION_33_PERMISSIONS) == 1, security
        assert "resets permissions set by hand" not in security, security
        bullets = _readme_bullets(_subsection("#### Coming from the full SurrealDB-ORM", section_33))
        assert _SECTION_33_DEFINE_TABLE in bullets[2], bullets[2]
        assert "comes with v0.34.0" not in _normalised(section_33)

    def test_section_34_sentences(self) -> None:
        """The declared clauses are written, ``TableType``'s other members take effect, ``define_table()`` is in the
        present."""
        section_34 = _readme_part(_SECTION_34)
        declaring = _normalised(_subsection("#### Declaring the constraint", section_34))
        assert _SECTION_34_TABLE_TYPE in declaring, declaring
        bullets = _readme_bullets(_subsection("#### Coming from the full SurrealDB-ORM", section_34))
        assert _SECTION_34_DECLARED in bullets, bullets
        assert _SECTION_34_DEFINE_TABLE in bullets[2], bullets[2]
        assert "**`TableType`'s other members take effect in `define_table()`**" in bullets[9], bullets[9]
        text = _normalised(section_34)
        for stale in ("no effect until v0.34.0", "comes with v0.34.0", "keys yet", "recorded for the table DDL"):
            assert stale not in text, stale

    def test_server_behaviour_rows(self) -> None:
        """Exactly three rows, Since v0.34.0, together and in version order before the table's last row."""
        assert_server_rows(_TABLE_DDL_ROWS)

    def test_server_behaviour_table_keeps_its_widths(self) -> None:
        """Every line 535 characters, the columns 136, 175, 181 and 38 wide: the new rows are padded to them."""
        assert_server_table()

    def test_server_behaviour_header_sentence(self) -> None:
        assert_server_header_sentence(_HEADER_SENTENCE_34)

    def test_embedded_table_row(self) -> None:
        """All four cells, in version order before the catch-all "Everything else" row; widths kept."""
        assert_embedded_rows([_EMBEDDED_TABLE_DDL_ROW])

    def test_features_row(self) -> None:
        features = [row for _, row in _table_rows(_readme_part("## Features"))]
        firsts = [row[0] for row in features]
        assert _FEATURES_ROW_35 in features, features
        relations = "Relation constraints (`relation_in`, `define_relation()`, `TYPE RELATION`)"
        assert firsts.index(_FEATURES_ROW_35[0]) == firsts.index(relations) + 1

    def test_roadmap_rows(self) -> None:
        roadmap = [row for _, row in _table_rows(_readme_part("## Roadmap"))]
        start = [row[0] for row in roadmap].index("v0.33.0")
        assert roadmap[start : start + 4] == _ROADMAP_ROWS_35, roadmap

    def test_comparison_row(self) -> None:
        """Released, right after ``TYPE RELATION`` enforcement and before full-text search."""
        comparison = [row for _, row in _table_rows(_readme_part("## SurrealDB-ORM-lite vs SurrealDB-ORM"))]
        firsts = [row[0] for row in comparison]
        start = firsts.index("TYPE RELATION enforcement")
        assert comparison[start + 1] == _COMPARISON_ROW_35, comparison
        assert firsts[start + 2] == "Full-Text Search", firsts

    def test_tables_have_no_raw_pipe_in_a_cell(self) -> None:
        """Every table v0.34.0 touches, section 35's included, keeps one cell count per line and no escaped pipe."""
        parts = [
            _readme_part("## Features"),
            _readme_part(_SERVER_BEHAVIOUR),
            _readme_part(_EMBEDDED_BEHAVIOUR),
            _readme_part("## Roadmap"),
            _readme_part("## SurrealDB-ORM-lite vs SurrealDB-ORM"),
        ]
        found = [lines for part in parts for lines in _md_tables(part)]
        assert len(found) == len(parts)
        for lines in found:
            assert len({len(_cells(line)) for line in lines}) == 1 and "\\|" not in "".join(lines), lines
        section = _md_tables(_section_35())
        assert len(section) == 3, "section 35 has its kinds table and the two source-rule tables"
        for lines in section:
            assert len({len(_cells(line)) for line in lines}) == 1 and "\\|" not in "".join(lines), lines

    async def test_section_35_examples(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Every ``python`` block runs as written, in order and in one namespace, on ``mem://`` (inside
        ``embedded_database()``); what each block prints is, line by line, its ``# `` comment lines."""
        capsys.readouterr()
        namespace: dict[str, Any] = {"__name__": "readme_section_35"}
        try:
            async with embedded_database():
                for code in _readme_blocks(_section_35()):
                    compiled = compile(code, "<README section 35>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                    # A doctest of this repository's own README (trusted text), top-level awaits included.
                    outcome = eval(compiled, namespace)
                    if inspect.iscoroutine(outcome):
                        await outcome
                    assert capsys.readouterr().out.splitlines() == _readme_printed(code), code
        finally:
            namespace.clear()
