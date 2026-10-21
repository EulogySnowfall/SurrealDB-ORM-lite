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

On the engines (``TestTableDdlE2E``, every engine of both lines): each list of statements, applied raw, is accepted and
read back — the table's type, mode, comment, change feed and permissions, each column's kind and ``FLEXIBLE``.

The ORM's writes (``TestOrmWritesE2E``): on a ``SCHEMAFULL`` table defined by a model's ``table_ddl()`` — one column of
every kind, a typed computed field ``tot`` — every write path is accepted and every field reads back equal, and the
computed field reads ``i + 1`` after each write; SurrealDB 3.1 refuses the ORM's ``set`` value, which is pinned there.
"""

import ast
import contextlib
import os
import re
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum, IntEnum, StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, ClassVar, Literal, TypeVar
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, Field, computed_field
from surrealdb import Datetime as SdkDatetime
from surrealdb import Duration as SdkDuration
from surrealdb import InternalError, RecordID
from surrealdb import Range as SdkRange

import surreal_orm_lite
from surreal_orm_lite import (
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
    SurrealDBConnectionManager,
    SurrealDbNotFoundError,
    TableType,
    computed,
    model_ddl,
    schema,
)
from surreal_orm_lite.define_parser import normalise_kind, parse_define_field, parse_define_table, parse_kind, render_kind

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


def _register_tdl(kind: str, name: str = "tdl") -> None:
    if kind == "mem":
        SurrealDBConnectionManager.add_connection(name, url="mem://", namespace="ns", database="tdl_db")
    else:
        SurrealDBConnectionManager.add_connection(
            name, url=_url(kind), user="root", password="root", namespace="ns", database="tdl_db"
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


class TestOrmWritesE2E:
    """Every ORM write into the table ``table_ddl()`` defines for the rich model (``TdlKitchenBase`` on SurrealDB 3.1,
    which refuses the ORM's ``set`` value; ``TdlKitchen`` elsewhere), ``anyv`` holding scalars: accepted, every field
    read back equal, and the typed computed field ``tot`` read ``i + 1`` after each write, on every engine."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_writes_round_trip(self, kind: str) -> None:
        async with _tdl(kind) as client:
            model = TdlKitchenBase if await _line(client) == (3, 1) else TdlKitchen
            await _apply_ddl(client, " ".join(model.table_ddl()))

            k1 = _kitchen(model, "k1")
            await k1.save()
            await _read_back(model, "k1", k1)

            k1.s = "changed"
            k1.i = 4
            k1.opt = 5
            k1.osub = TdlAddress(city="Rome", zip="00100")
            k1.anyv = "s"
            await k1.update()
            await _read_back(model, "k1", k1)

            await k1.merge(i=9, opt=None, dc={"z": {"w": 1}})
            merged = k1.model_copy(update={"i": 9, "opt": None, "dc": {"nested": {"deep": [1, 2]}, "k": "v", "z": {"w": 1}}})
            k1 = await _read_back(model, "k1", merged)
            assert k1.tot == 10

            k2 = _kitchen(model, "k2", i=2)
            await k2.upsert()
            await _read_back(model, "k2", k2)
            k2.i = 7
            await k2.upsert()
            assert (await _read_back(model, "k2", k2)).tot == 8

            await k1.patch([{"op": "replace", "path": "/s", "value": "p"}])
            assert (await _read_back(model, "k1", k1)).s == "p"
            await k1.atomic_append("ls", "c")
            assert (await _read_back(model, "k1", k1)).ls == ["a", "b", "c"]
            await k1.atomic_increment("i", 2)
            assert (await _read_back(model, "k1", k1)).tot == 12

            k3 = _kitchen(model, "k3", s="c", i=30)
            k4 = _kitchen(model, "k4", s="c", i=40)
            await model.objects().bulk_create([k3, k4])
            await _read_back(model, "k3", k3)
            await _read_back(model, "k4", k4)
            assert await model.objects().filter(s="c").bulk_update(s="bu") == 2
            k3.s = k4.s = "bu"
            assert (await _read_back(model, "k3", k3)).tot == 31
            assert (await _read_back(model, "k4", k4)).tot == 41

            defaults = _kitchen_fields(model, None, i=50)
            del defaults["s"]
            made, created = await model.objects().get_or_create(defaults=defaults, s="g")
            assert created is True
            assert (await _read_back(model, made.id, made)).tot == 51
            again, created = await model.objects().get_or_create(defaults=defaults, s="g")
            assert created is False
            assert again.get_raw_id() == made.get_raw_id()

            k6 = _kitchen(model, "k6", osub=None, opt=None, anyv=None, geo=None, rng=None)
            await k6.save()
            await _read_back(model, "k6", k6)

            await k1.delete()
            with pytest.raises(SurrealDbNotFoundError):
                await model.objects().get("k1")

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
