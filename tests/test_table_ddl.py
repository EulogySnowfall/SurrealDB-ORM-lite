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
"""

import ast
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
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
from surrealdb import Range as SdkRange
from surrealdb import RecordID

import surreal_orm_lite
from surreal_orm_lite import (
    BaseSurrealEdge,
    BaseSurrealModel,
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
    TableType,
    computed,
    model_ddl,
    schema,
)
from surreal_orm_lite.define_parser import parse_kind, render_kind

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
