"""Schema introspection (v0.30.0): the immutable schema state (spec §5.4, §5.6 E10/E11, §6.1; AC-2).

The ten frozen, slotted dataclasses of ``surreal_orm_lite.schema_state`` and their read-only map. The seven classes
the full SurrealDB-ORM has keep its attribute names, positional order, defaults and accepted inputs; the lite
additions come after a ``KW_ONLY`` marker, so a full-ORM positional call that reaches an attribute the lite does not
provide raises ``TypeError``. Unit tests only (no server).
"""

import copy
import dataclasses
import pickle
import typing
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pytest

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
    _empty_map,
    _FrozenMap,
    frozen_map,
)

_FACTORY = "factory"
_MISSING = dataclasses.MISSING

# Spec §5.4: (name, default — "factory" for an empty-map factory, MISSING when required —, keyword-only). The full
# ORM's attributes first, in its order and with its defaults (positional); then the lite ones (keyword-only).
_PARITY: dict[type, list[tuple[str, object, bool]]] = {
    SchemaState: [
        ("tables", _FACTORY, False),
        # the full ORM's 2nd attribute, applied_migrations, is not provided: everything after is keyword-only
        ("connection", "default", True),
        ("analyzers", _FACTORY, True),
        ("accesses", _FACTORY, True),
        ("functions", _FACTORY, True),
        ("params", _FACTORY, True),
        ("other", _FACTORY, True),
        ("issues", (), True),
    ],
    TableState: [
        ("name", _MISSING, False),
        ("schema_mode", "SCHEMAFULL", False),
        ("table_type", "normal", False),
        ("fields", _FACTORY, False),
        ("indexes", _FACTORY, False),
        ("events", _FACTORY, False),
        ("changefeed", None, False),
        ("permissions", _FACTORY, False),
        # the full ORM's 9th attribute, access, is not provided
        ("view_query", None, True),
        ("relation_in", (), True),
        ("relation_out", (), True),
        ("enforced", False, True),
        ("comment", None, True),
        ("drop", False, True),
        ("changefeed_include_original", False, True),
        ("other", _FACTORY, True),
        ("raw", "", True),
    ],
    FieldState: [
        ("name", _MISSING, False),
        ("field_type", _MISSING, False),
        ("nullable", True, False),
        ("default", None, False),
        ("assertion", None, False),
        # the full ORM's 6th attribute, encrypted, is not provided
        ("flexible", False, True),
        ("readonly", False, True),
        ("value", None, True),
        ("reference", False, True),
        ("on_delete", None, True),
        ("comment", None, True),
        ("path", (), True),
        ("table", "", True),
        ("default_always", False, True),
        ("computed", None, True),
        ("permissions", _FACTORY, True),
        ("raw", "", True),
    ],
    IndexState: [
        ("name", _MISSING, False),
        ("fields", (), False),
        ("unique", False, False),
        ("search_analyzer", None, False),
        ("bm25", None, False),
        ("highlights", False, False),
        ("hnsw", False, False),
        ("dimension", None, False),
        ("dist", None, False),
        ("vector_type", None, False),
        ("efc", None, False),
        ("m", None, False),
        ("concurrently", False, False),
        ("table", "", True),
        ("mtree", False, True),
        ("count", False, True),
        ("comment", None, True),
        ("raw", "", True),
    ],
    EventState: [
        ("name", _MISSING, False),
        ("table", _MISSING, False),
        ("when", _MISSING, False),
        ("then", _MISSING, False),
        ("comment", None, True),
        ("async_mode", False, True),
        ("retry", None, True),
        ("max_depth", None, True),
        ("raw", "", True),
    ],
    AccessState: [
        ("name", _MISSING, False),
        ("table", "", False),
        ("signup_fields", _FACTORY, False),
        ("signin_where", "", False),
        ("duration_token", "15m", False),
        ("duration_session", "12h", False),
        ("access_type", "RECORD", False),
        ("duration_grant", None, False),
        ("signup", None, True),
        ("signin", None, True),
        ("with_refresh", False, True),
        ("comment", None, True),
        ("raw", "", True),
    ],
    AnalyzerState: [
        ("name", _MISSING, False),
        ("tokenizers", (), False),
        ("filters", (), False),
        ("function", None, True),
        ("comment", None, True),
        ("raw", "", True),
    ],
    FunctionState: [
        ("name", _MISSING, False),
        ("parameters", _MISSING, False),
        ("returns", _MISSING, False),
        ("permissions", _MISSING, False),
        ("comment", _MISSING, False),
        ("raw", _MISSING, False),
    ],
    ParamState: [
        ("name", _MISSING, False),
        ("value", _MISSING, False),
        ("permissions", _MISSING, False),
        ("comment", _MISSING, False),
        ("raw", _MISSING, False),
    ],
    SchemaIssue: [
        ("kind", _MISSING, False),
        ("name", _MISSING, False),
        ("table", _MISSING, False),
        ("message", _MISSING, False),
        ("statement", None, False),
    ],
}

# The smallest valid instance of each class.
_MINIMAL: dict[type, Callable[[], Any]] = {
    SchemaState: lambda: SchemaState(),
    TableState: lambda: TableState(name="t"),
    FieldState: lambda: FieldState(name="x", field_type="int"),
    IndexState: lambda: IndexState(name="i"),
    EventState: lambda: EventState("e", "t", "true", "(x)"),
    AccessState: lambda: AccessState(name="a"),
    AnalyzerState: lambda: AnalyzerState(name="an"),
    FunctionState: lambda: FunctionState("fn::f", (), None, "FULL", None, ""),
    ParamState: lambda: ParamState("p", "1", "FULL", None, ""),
    SchemaIssue: lambda: SchemaIssue("table", "t", None, "m"),
}


def _shape(cls: type) -> list[tuple[str, object, bool]]:
    """``(name, default or "factory" or MISSING, kw_only)`` of every dataclass field of *cls*, in order."""
    shape: list[tuple[str, object, bool]] = []
    for item in dataclasses.fields(cls):
        default = _FACTORY if item.default_factory is not dataclasses.MISSING else item.default
        shape.append((item.name, default, bool(item.kw_only)))
    return shape


def _one_of_each() -> SchemaState:
    """A ``SchemaState`` holding one of each nested state (rebuilt identically on every call)."""
    field_state = FieldState(
        name="address.city",
        field_type="string",
        nullable=False,
        default="'Paris'",
        assertion="$value != ''",
        readonly=True,
        comment="city",
        table="person",
        permissions={"update": "NONE", "select": "FULL", "create": "FULL"},
        raw="DEFINE FIELD address.city ON person TYPE string",
    )
    index = IndexState(name="by_city", fields=["address.city"], unique=True, bm25=[1.2, 0.75], table="person")
    event = EventState("audit", "person", "$event = 'CREATE'", "(CREATE log SET at = time::now())", comment="e")
    table = TableState(
        name="person",
        schema_mode="SCHEMALESS",
        fields={"address.city": field_state},
        indexes={"by_city": index},
        events={"audit": event},
        changefeed="1d",
        permissions={"select": "FULL"},
        relation_in="a | b",
        other={"lives": {"b": "x", "a": "y"}},
        raw="DEFINE TABLE person SCHEMALESS",
    )
    access = AccessState(name="acc", table="user", signup_fields={"email": "$email"}, signin_where="email = $email")
    analyzer = AnalyzerState(name="an", tokenizers=["blank", "class"], filters=["lowercase"], function="fn::an")
    function = FunctionState("fn::greet", ["name"], "string", "FULL", "says hi", "DEFINE FUNCTION fn::greet() {}")
    param = ParamState("endpoint", "'https://x'", "FULL", None, "DEFINE PARAM $endpoint VALUE 'https://x'")
    issue = SchemaIssue("field", "bad", "person", "could not parse its definition: …", statement="DEFINE FIELD bad")
    return SchemaState(
        tables={"person": table},
        connection="isp",
        analyzers={"an": analyzer},
        accesses={"acc": access},
        functions={"fn::greet": function},
        params={"endpoint": param},
        other={"apis": {"/b": "DEFINE API '/b'", "/a": "DEFINE API '/a'"}},
        issues=[issue],
    )


def _assert_frozen_shape(state: SchemaState) -> None:
    """Every map of *state* (nested ones included) is a ``_FrozenMap``, every sequence a ``tuple``."""
    for mapping in (state.tables, state.analyzers, state.accesses, state.functions, state.params, state.other):
        assert type(mapping) is _FrozenMap
    assert type(state.other["apis"]) is _FrozenMap
    assert type(state.issues) is tuple
    table = state.tables["person"]
    for mapping in (table.fields, table.indexes, table.events, table.permissions, table.other, table.other["lives"]):
        assert type(mapping) is _FrozenMap
    assert type(table.relation_in) is tuple
    assert type(table.relation_out) is tuple
    field_state = table.fields["address.city"]
    assert type(field_state.path) is tuple
    assert type(field_state.permissions) is _FrozenMap
    assert type(table.indexes["by_city"].fields) is tuple
    assert type(table.indexes["by_city"].bm25) is tuple
    assert type(state.accesses["acc"].signup_fields) is _FrozenMap
    assert type(state.analyzers["an"].tokenizers) is tuple
    assert type(state.analyzers["an"].filters) is tuple
    assert type(state.functions["fn::greet"].parameters) is tuple


class TestFrozenMap:
    """Spec §6.1: a read-only mapping in sorted key order, equal to any mapping with the same items, picklable."""

    def test_reads_like_a_mapping_in_key_order(self) -> None:
        mapping = frozen_map({"b": 1, "a": 2})
        assert type(mapping) is _FrozenMap
        assert isinstance(mapping, Mapping)
        assert list(mapping) == ["a", "b"]
        assert list(mapping.items()) == [("a", 2), ("b", 1)]
        assert list(mapping.keys()) == ["a", "b"]
        assert list(mapping.values()) == [2, 1]
        assert len(mapping) == 2
        assert "a" in mapping
        assert "z" not in mapping
        assert mapping["b"] == 1
        assert mapping.get("a") == 2
        assert mapping.get("z") is None
        assert mapping.get("z", 0) == 0
        with pytest.raises(KeyError):
            mapping["z"]

    def test_codepoint_order(self) -> None:
        keys = ["é", "b", "_", "B", "a", "10", "9"]
        assert list(frozen_map(dict.fromkeys(keys, 0))) == sorted(keys)

    def test_equal_to_any_mapping_with_the_same_items(self) -> None:
        mapping = frozen_map({"b": 1, "a": 2})
        plain = {"a": 2, "b": 1}
        assert mapping == plain
        assert plain == mapping  # dict.__eq__ defers to the map's
        assert mapping == frozen_map([("a", 2), ("b", 1)])
        assert mapping != {"a": 2}
        assert mapping != {"a": 2, "b": 2}
        assert mapping != [("a", 2), ("b", 1)]
        assert frozen_map({}) == {}

    def test_built_from_pairs(self) -> None:
        mapping = frozen_map([("b", 1), ("a", 2)])
        assert list(mapping) == ["a", "b"]
        assert mapping == {"a": 2, "b": 1}
        assert list(_FrozenMap({"b": 1, "a": 2})) == ["a", "b"]
        assert list(frozen_map(iter([("y", 1), ("x", 2)]))) == ["x", "y"]

    def test_is_read_only(self) -> None:
        mapping: Any = frozen_map({"a": 1})
        with pytest.raises(TypeError):
            mapping["a"] = 2
        with pytest.raises(TypeError):
            mapping["b"] = 2
        with pytest.raises(TypeError):
            del mapping["a"]
        for name in ("pop", "popitem", "update", "clear", "setdefault", "__setitem__", "__delitem__"):
            assert not hasattr(mapping, name), name
            with pytest.raises(AttributeError):
                getattr(mapping, name)
        with pytest.raises(AttributeError):
            mapping.extra = 1
        assert mapping == {"a": 1}

    def test_not_hashable(self) -> None:
        with pytest.raises(TypeError):
            hash(frozen_map({"a": 1}))
        with pytest.raises(TypeError):
            hash(frozen_map({}))

    def test_repr_is_the_sorted_dicts(self) -> None:
        assert repr(frozen_map({"b": 1, "a": 2})) == repr({"a": 2, "b": 1})
        assert repr(frozen_map({})) == "{}"

    def test_a_map_of_texts_shows_its_keys_only(self) -> None:
        """A map built with ``hide_values=True`` (rules, expressions, unmodelled sections) keeps its values out of
        ``repr``/``str``, and keeps that through ``pickle``, ``copy`` and ``deepcopy``; equality ignores it."""
        texts = _FrozenMap({"b": "s3cr3t-0042", "a": "x"}, hide_values=True)
        assert repr(texts) == "{'a': ..., 'b': ...}"
        assert str(texts) == repr(texts)
        assert repr(_FrozenMap({}, hide_values=True)) == "{}"
        assert type(texts) is _FrozenMap
        assert list(texts) == ["a", "b"]
        assert texts["b"] == "s3cr3t-0042"
        assert texts == {"a": "x", "b": "s3cr3t-0042"}
        assert texts == frozen_map({"a": "x", "b": "s3cr3t-0042"})
        assert frozen_map({"a": "x", "b": "s3cr3t-0042"}) == texts
        with pytest.raises(TypeError):
            hash(texts)
        clones = [copy.copy(texts), copy.deepcopy(texts)]
        clones += [pickle.loads(pickle.dumps(texts, protocol=p)) for p in range(pickle.HIGHEST_PROTOCOL + 1)]
        for clone in clones:
            assert type(clone) is _FrozenMap
            assert clone == texts
            assert repr(clone) == "{'a': ..., 'b': ...}"
        assert repr(copy.deepcopy(frozen_map({"a": 1}))) == "{'a': 1}"

    def test_pickle_copy_and_deepcopy(self) -> None:
        mapping = frozen_map({"b": [1, 2], "a": frozen_map({"x": 1})})
        clones = [pickle.loads(pickle.dumps(mapping)), copy.copy(mapping), copy.deepcopy(mapping)]
        for clone in clones:
            assert clone == mapping
            assert type(clone) is _FrozenMap
            assert list(clone) == ["a", "b"]
            assert type(clone["a"]) is _FrozenMap
        deep = copy.deepcopy(mapping)
        assert deep["b"] == [1, 2]
        assert deep["b"] is not mapping["b"]
        for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
            assert pickle.loads(pickle.dumps(mapping, protocol=protocol)) == mapping

    def test_empty_map(self) -> None:
        empty = _empty_map()
        assert type(empty) is _FrozenMap
        assert len(empty) == 0
        assert empty == {}
        assert list(empty) == []
        assert _empty_map() == empty


class TestParity:
    """Spec §5.4 (D4, AC-2): the attributes of the ten classes, their defaults and which are keyword-only."""

    def test_the_ten_classes(self) -> None:
        assert len(_PARITY) == 10
        assert set(_PARITY) == set(_MINIMAL)

    @pytest.mark.parametrize("cls", list(_PARITY), ids=[cls.__name__ for cls in _PARITY])
    def test_attributes_defaults_and_keyword_only(self, cls: type) -> None:
        assert _shape(cls) == _PARITY[cls]

    @pytest.mark.parametrize("cls", list(_PARITY), ids=[cls.__name__ for cls in _PARITY])
    def test_map_defaults_are_empty_frozen_maps(self, cls: type) -> None:
        instance = _MINIMAL[cls]()
        for item in dataclasses.fields(cls):
            if item.default_factory is dataclasses.MISSING:
                continue
            made = item.default_factory()
            assert type(made) is _FrozenMap, item.name
            assert made == {}, item.name
            assert type(getattr(instance, item.name)) is _FrozenMap, item.name

    @pytest.mark.parametrize("cls", list(_PARITY), ids=[cls.__name__ for cls in _PARITY])
    def test_slotted_and_frozen(self, cls: type) -> None:
        assert "__slots__" in vars(cls)
        instance = _MINIMAL[cls]()
        assert not hasattr(instance, "__dict__")
        for item in dataclasses.fields(cls):
            with pytest.raises(dataclasses.FrozenInstanceError):
                setattr(instance, item.name, None)
            with pytest.raises(dataclasses.FrozenInstanceError):
                delattr(instance, item.name)
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError, TypeError)):
            instance.not_an_attribute = 1

    def test_secrets_are_out_of_repr(self) -> None:
        """Spec §8: every attribute holding SurrealQL text (a statement, an expression, a rule, a body) is out of
        ``repr``; names, kinds, flags, durations and comments stay in it."""
        repr_flags = {
            cls.__name__: [item.name for item in dataclasses.fields(cls) if not item.repr]
            for cls in _PARITY
            if any(not item.repr for item in dataclasses.fields(cls))
        }
        assert repr_flags == {
            "TableState": ["view_query", "raw"],
            "FieldState": ["default", "assertion", "value", "on_delete", "computed", "raw"],
            "IndexState": ["raw"],
            "EventState": ["when", "then", "raw"],
            "AccessState": ["signin_where", "signup", "signin", "raw"],
            "AnalyzerState": ["raw"],
            "FunctionState": ["permissions", "raw"],
            "ParamState": ["value", "permissions", "raw"],
            "SchemaIssue": ["statement"],
        }

    def test_names_attributes_are_read_only_sequences(self) -> None:
        """D29: every names attribute is annotated ``Sequence[str]``, ``issues`` ``Sequence[SchemaIssue]``."""
        names = [
            (IndexState, "fields"),
            (AnalyzerState, "tokenizers"),
            (AnalyzerState, "filters"),
            (FieldState, "path"),
            (FunctionState, "parameters"),
            (TableState, "relation_in"),
            (TableState, "relation_out"),
        ]
        for cls, attribute in names:
            assert typing.get_type_hints(cls)[attribute] == Sequence[str], (cls.__name__, attribute)
        assert typing.get_type_hints(SchemaState)["issues"] == Sequence[SchemaIssue]
        assert typing.get_type_hints(SchemaState)["tables"] == Mapping[str, TableState]
        assert typing.get_type_hints(TableState)["permissions"] == Mapping[str, str]
        assert typing.get_type_hints(IndexState)["bm25"] == tuple[float, float] | bool | None


class TestFullOrmCallShapes:
    """AC-2: the full ORM's calls with its required arguments work as they are."""

    def test_schema_state_without_arguments(self) -> None:
        state = SchemaState()
        assert state.tables == {}
        assert state.connection == "default"
        assert state.analyzers == {}
        assert state.accesses == {}
        assert state.functions == {}
        assert state.params == {}
        assert state.other == {}
        assert state.issues == ()

    def test_table_state_by_keyword(self) -> None:
        table = TableState(name="t")
        assert (table.name, table.schema_mode, table.table_type) == ("t", "SCHEMAFULL", "normal")
        assert table.fields == {}
        assert table.indexes == {}
        assert table.events == {}
        assert table.changefeed is None
        assert table.permissions == {}
        assert table.view_query is None
        assert table.relation_in == ()
        assert table.relation_out == ()
        assert table.enforced is False
        assert table.comment is None
        assert table.drop is False
        assert table.changefeed_include_original is False
        assert table.other == {}
        assert table.raw == ""

    def test_field_state_by_keyword(self) -> None:
        state = FieldState(name="x", field_type="int")
        assert state.nullable is True
        assert state.default is None
        assert state.assertion is None
        assert state.path == ("x",)
        assert state.table == ""
        assert state.permissions == {}
        assert state.raw == ""

    def test_event_state_by_position(self) -> None:
        event = EventState("e", "t", "true", "(x)")
        assert (event.name, event.table, event.when, event.then) == ("e", "t", "true", "(x)")
        assert event.comment is None
        assert (event.async_mode, event.retry, event.max_depth) == (False, None, None)
        assert event.raw == ""

    def test_access_state_by_keyword(self) -> None:
        access = AccessState(name="a")
        assert access.table == ""
        assert access.signup_fields == {}
        assert access.signin_where == ""
        assert access.duration_token == "15m"
        assert access.duration_session == "12h"
        assert access.access_type == "RECORD"
        assert access.duration_grant is None

    def test_analyzer_state_by_keyword(self) -> None:
        analyzer = AnalyzerState(name="an")
        assert analyzer.tokenizers == ()
        assert analyzer.filters == ()
        assert analyzer.function is None

    def test_index_state_by_keyword(self) -> None:
        index = IndexState(name="i", fields=["a"])
        assert index.fields == ("a",)
        assert index.unique is False
        assert index.bm25 is None
        assert index.concurrently is False

    def test_the_full_orms_positional_shapes_up_to_a_dropped_attribute(self) -> None:
        field_state = FieldState("a", "int", False, "18", "$value > 0")
        assert (field_state.nullable, field_state.default, field_state.assertion) == (False, "18", "$value > 0")
        index = IndexState("i", ["a"], True)
        assert (index.fields, index.unique) == (("a",), True)
        access = AccessState("a", "user", {"e": "$e"}, "e = $e", "15m", "12h", "RECORD", None)
        assert (access.table, access.signup_fields, access.signin_where) == ("user", {"e": "$e"}, "e = $e")
        assert (access.duration_token, access.duration_session, access.access_type) == ("15m", "12h", "RECORD")
        assert access.duration_grant is None
        analyzer = AnalyzerState("an", ["blank"], ["lowercase"])
        assert (analyzer.tokenizers, analyzer.filters) == (("blank",), ("lowercase",))
        table = TableState("t", "SCHEMALESS", "any", {}, {}, {}, "1d", {"select": "FULL"})
        assert (table.schema_mode, table.table_type, table.changefeed) == ("SCHEMALESS", "any", "1d")
        assert table.permissions == {"select": "FULL"}

    def test_a_positional_call_reaching_a_dropped_attribute_raises(self) -> None:
        any_cls: Any = FieldState
        with pytest.raises(TypeError):
            any_cls("a", "int", True, None, None, False)  # the full ORM's 6th: encrypted
        any_cls = TableState
        with pytest.raises(TypeError):
            any_cls("t", "SCHEMAFULL", "normal", {}, {}, {}, None, {}, None)  # the full ORM's 9th: access
        any_cls = SchemaState
        with pytest.raises(TypeError):
            any_cls({}, [])  # the full ORM's 2nd: applied_migrations


class TestConversions:
    """Spec §5.4: the conversions of ``__post_init__`` (lists → tuples, dicts → read-only maps, pipe-joined names)."""

    def test_names_become_tuples(self) -> None:
        index = IndexState(name="i", fields=["a"])
        assert index.fields == ("a",)
        assert type(index.fields) is tuple
        analyzer = AnalyzerState(name="an", tokenizers=["blank"], filters=["lowercase"])
        assert (analyzer.tokenizers, analyzer.filters) == (("blank",), ("lowercase",))
        assert type(analyzer.tokenizers) is tuple
        assert type(analyzer.filters) is tuple
        function = FunctionState("fn::f", ["a", "b"], None, "FULL", None, "")
        assert function.parameters == ("a", "b")
        assert type(function.parameters) is tuple
        assert IndexState(name="i", fields=(name for name in ["a", "b"])).fields == ("a", "b")

    def test_bm25(self) -> None:
        listed = IndexState(name="i", bm25=[1.2, 0.75])
        assert listed.bm25 == (1.2, 0.75)
        assert type(listed.bm25) is tuple
        assert IndexState(name="i", bm25=(1.2, 0.75)).bm25 == (1.2, 0.75)
        assert IndexState(name="i", bm25=True).bm25 is True
        assert IndexState(name="i", bm25=False).bm25 is False
        assert IndexState(name="i").bm25 is None

    def test_relation_names(self) -> None:
        table = TableState(name="t", relation_in="a | b", relation_out=None)  # type: ignore[arg-type]
        assert table.relation_in == ("a", "b")
        assert table.relation_out == ()
        assert type(table.relation_in) is tuple
        assert type(table.relation_out) is tuple
        assert TableState(name="t", relation_in="").relation_in == ()
        assert TableState(name="t", relation_in="person").relation_in == ("person",)
        assert TableState(name="t", relation_in=" a |  | b|c ").relation_in == ("a", "b", "c")
        assert TableState(name="t", relation_in=["a"]).relation_in == ("a",)
        assert TableState(name="t", relation_out=("x", "y")).relation_out == ("x", "y")

    def test_maps_become_frozen_maps(self) -> None:
        field_state = FieldState(name="x", field_type="int", permissions={"update": "NONE", "select": "FULL"})
        assert type(field_state.permissions) is _FrozenMap
        assert list(field_state.permissions) == ["select", "update"]
        table = TableState(
            name="t",
            fields={"x": field_state},
            indexes={"i": IndexState(name="i")},
            events={"e": EventState("e", "t", "true", "(x)")},
            permissions=[("update", "NONE"), ("select", "FULL")],
            other={"lives": {"b": "x", "a": "y"}},
        )
        for mapping in (table.fields, table.indexes, table.events, table.permissions, table.other):
            assert type(mapping) is _FrozenMap
        assert table.permissions == {"select": "FULL", "update": "NONE"}
        assert type(table.other["lives"]) is _FrozenMap
        assert list(table.other["lives"]) == ["a", "b"]
        access = AccessState(name="a", signup_fields={"name": "$name", "email": "$email"})
        assert type(access.signup_fields) is _FrozenMap
        assert list(access.signup_fields) == ["email", "name"]

    def test_schema_state_maps_and_issues(self) -> None:
        issue = SchemaIssue("table", "t", None, "m")
        state = SchemaState(
            tables={"t": TableState(name="t")},
            analyzers={"an": AnalyzerState(name="an")},
            accesses={"a": AccessState(name="a")},
            functions={"fn::f": FunctionState("fn::f", (), None, "FULL", None, "")},
            params={"p": ParamState("p", "1", "FULL", None, "")},
            other={"apis": {"/b": "x", "/a": "y"}},
            issues=[issue],
        )
        for mapping in (state.tables, state.analyzers, state.accesses, state.functions, state.params, state.other):
            assert type(mapping) is _FrozenMap
        assert type(state.other["apis"]) is _FrozenMap
        assert list(state.other["apis"]) == ["/a", "/b"]
        assert state.issues == (issue,)
        assert type(state.issues) is tuple

    def test_a_frozen_map_given_is_kept_equal(self) -> None:
        permissions = frozen_map({"select": "FULL"})
        field_state = FieldState(name="x", field_type="int", permissions=permissions)
        assert field_state.permissions == permissions
        assert type(field_state.permissions) is _FrozenMap

    def test_field_path(self) -> None:
        assert FieldState(name="a.b", field_type="int").path == ("a", "b")
        assert FieldState(name="tags.*", field_type="string").path == ("tags", "*")
        assert FieldState(name="first-name", field_type="string").path == ("first-name",)
        assert FieldState(name="x", field_type="int", path=["a.b"]).path == ("a.b",)
        assert FieldState(name="x", field_type="int", path=[]).path == ("x",)
        assert type(FieldState(name="x", field_type="int", path=["a", "b"]).path) is tuple

    def test_nothing_else_is_validated(self) -> None:
        table = TableState(name="t", schema_mode="frob", table_type="stream", changefeed="soon")
        assert (table.schema_mode, table.table_type, table.changefeed) == ("frob", "stream", "soon")
        access = AccessState(name="a", access_type="KEY", duration_token="NONE", duration_session=None)
        assert (access.access_type, access.duration_token, access.duration_session) == ("KEY", "NONE", None)
        assert FieldState(name="x", field_type="frob<", nullable=False, default="18").default == "18"


class TestValidation:
    """Spec §5.6 E10 and E11 (D30): a ``str`` where a sequence of names goes, a non-text ``FieldState.default``."""

    @pytest.mark.parametrize(
        ("build", "message"),
        [
            (
                lambda: IndexState(name="i", fields="email"),
                "IndexState.fields takes a sequence of str, not a str: write fields=['email'].",
            ),
            (
                lambda: AnalyzerState(name="an", tokenizers="blank"),
                "AnalyzerState.tokenizers takes a sequence of str, not a str: write tokenizers=['blank'].",
            ),
            (
                lambda: AnalyzerState(name="an", filters="lowercase"),
                "AnalyzerState.filters takes a sequence of str, not a str: write filters=['lowercase'].",
            ),
            (
                lambda: FunctionState("fn::f", "a", None, "FULL", None, ""),
                "FunctionState.parameters takes a sequence of str, not a str: write parameters=['a'].",
            ),
            (
                lambda: FieldState(name="x", field_type="int", path="x"),
                "FieldState.path takes a sequence of str, not a str: write path=['x'].",
            ),
            (
                lambda: FieldState(name="x", field_type="int", path="address.city"),
                "FieldState.path takes a sequence of str, not a str: write path=['address', 'city'].",
            ),
        ],
        ids=["fields", "tokenizers", "filters", "parameters", "path", "dotted-path"],
    )
    def test_e10(self, build: Callable[[], object], message: str) -> None:
        with pytest.raises(TypeError) as caught:
            build()
        assert str(caught.value) == message

    @pytest.mark.parametrize(("default", "type_name"), [(18, "int"), (True, "bool"), (1.5, "float"), ([], "list")])
    def test_e11(self, default: object, type_name: str) -> None:
        with pytest.raises(TypeError) as caught:
            FieldState(name="x", field_type="int", default=default)  # type: ignore[arg-type]
        assert str(caught.value) == (
            f"FieldState.default takes SurrealQL text (str) or None, not {type_name}: "
            "write default='18' for 18 or default=\"'active'\" for the string 'active'."
        )

    def test_text_defaults_are_accepted(self) -> None:
        assert FieldState(name="x", field_type="int", default="18").default == "18"
        assert FieldState(name="x", field_type="string", default="'active'").default == "'active'"
        assert FieldState(name="x", field_type="int", default=None).default is None


class TestRoundTrip:
    """Spec §5.4: equality, pickle, copy and deepcopy of a whole schema; maps stay read-only, sequences tuples."""

    def test_equal_to_a_rebuilt_one(self) -> None:
        state = _one_of_each()
        assert state == _one_of_each()
        _assert_frozen_shape(state)
        assert state.tables["person"].relation_in == ("a", "b")
        assert state.tables["person"].fields["address.city"].path == ("address", "city")
        assert state != SchemaState()

    def test_pickle_copy_and_deepcopy(self) -> None:
        state = _one_of_each()
        for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
            clone = pickle.loads(pickle.dumps(state, protocol=protocol))
            assert clone == state
            _assert_frozen_shape(clone)
        deep = copy.deepcopy(state)
        assert deep == state
        assert deep is not state
        assert deep.tables["person"] is not state.tables["person"]
        _assert_frozen_shape(deep)
        shallow = copy.copy(state)
        assert shallow == state
        _assert_frozen_shape(shallow)

    @pytest.mark.parametrize("cls", list(_PARITY), ids=[cls.__name__ for cls in _PARITY])
    def test_each_class_round_trips(self, cls: type) -> None:
        instance = _MINIMAL[cls]()
        assert pickle.loads(pickle.dumps(instance)) == instance
        assert copy.deepcopy(instance) == instance
        assert copy.copy(instance) == instance

    def test_states_holding_a_map_are_not_hashable(self) -> None:
        for state in (SchemaState(), TableState(name="t"), FieldState(name="x", field_type="int"), AccessState("a")):
            with pytest.raises(TypeError):
                hash(state)
        assert hash(EventState("e", "t", "true", "(x)")) == hash(EventState("e", "t", "true", "(x)"))
        assert hash(IndexState(name="i", fields=["a"])) == hash(IndexState(name="i", fields=["a"]))


_SECRET = "s3cr3t-0042"


def _secret_schema() -> SchemaState:
    """A schema whose every SurrealQL text holds a credential: a JWT key, a user's ``PASSHASH`` in ``other``, literal
    passwords and tokens in expressions, rules and bodies. Every one of them contains ``_SECRET``."""
    field_state = FieldState(
        name="api.token",
        field_type="string",
        nullable=False,
        default=f"'{_SECRET}-default'",
        assertion=f"$value != '{_SECRET}-assert'",
        readonly=True,
        value=f"crypto::argon2::generate('{_SECRET}-value')",
        reference=True,
        on_delete=f"THEN http::post('https://hook.example', {{ key: '{_SECRET}-on-delete' }})",
        comment="the API token",
        table="vault",
        computed=f"string::concat('{_SECRET}-computed', id)",
        permissions={"select": f"WHERE $token.key = '{_SECRET}-field-rule'", "update": "NONE"},
        raw=f"DEFINE FIELD api.token ON vault TYPE string DEFAULT '{_SECRET}-default'",
    )
    index = IndexState(name="by_token", fields=["api.token"], unique=True, table="vault", raw="DEFINE INDEX by_token")
    event = EventState(
        "on_token",
        "vault",
        f"$after.api.token != '{_SECRET}-when'",
        f"{{ http::post('https://hook.example', {{ token: '{_SECRET}-then' }}) }}",
        comment="notify",
        raw=f"DEFINE EVENT on_token ON vault THEN {{ http::post('https://hook.example', '{_SECRET}-then') }}",
    )
    table = TableState(
        name="vault",
        fields={"api.token": field_state},
        indexes={"by_token": index},
        events={"on_token": event},
        changefeed="1d",
        permissions={"select": f"WHERE $auth.key = '{_SECRET}-table-rule'", "delete": "NONE"},
        view_query=f"SELECT * FROM secrets WHERE key = '{_SECRET}-view'",
        comment="secrets",
        other={"future": {"thing": f"DEFINE THING thing KEY '{_SECRET}-table-other'"}},
        raw=f"DEFINE TABLE vault SCHEMAFULL PERMISSIONS FOR select WHERE $auth.key = '{_SECRET}-table-rule'",
    )
    access = AccessState(
        name="acc",
        table="user",
        signup_fields={"password": f"crypto::argon2::generate('{_SECRET}-signup-field')"},
        signin_where=f"pass = '{_SECRET}-signin-where'",
        duration_token="1h",
        signup=f"(CREATE user SET password = crypto::argon2::generate('{_SECRET}-signup-field'))",
        signin=f"(SELECT * FROM user WHERE pass = '{_SECRET}-signin-where')",
        with_refresh=True,
        comment="login",
        raw=f"DEFINE ACCESS acc ON DATABASE TYPE RECORD WITH JWT ALGORITHM HS512 KEY '{_SECRET}-jwt-key'",
    )
    analyzer = AnalyzerState(name="an", tokenizers=["blank"], filters=["lowercase"], raw=f"-- {_SECRET}-analyzer")
    function = FunctionState(
        "fn::login",
        ["user"],
        "string",
        f"WHERE $token.key = '{_SECRET}-function-rule'",
        "logs in",
        f"DEFINE FUNCTION fn::login($user: string) {{ RETURN http::post('https://x', {{ password: '{_SECRET}-body' }}) }}",
    )
    param = ParamState(
        "api_key",
        f"'{_SECRET}-param'",
        f"WHERE $token.key = '{_SECRET}-param-rule'",
        "the key",
        f"DEFINE PARAM $api_key VALUE '{_SECRET}-param'",
    )
    issue = SchemaIssue("param", "broken", None, "could not parse its definition", statement=f"DEFINE PARAM {_SECRET}")
    return SchemaState(
        tables={"vault": table},
        connection="isp",
        analyzers={"an": analyzer},
        accesses={"acc": access},
        functions={"fn::login": function},
        params={"api_key": param},
        other={
            "users": {"admin": f"DEFINE USER admin ON DATABASE PASSHASH '$argon2id$v=19$m=19456${_SECRET}' ROLES OWNER"},
            "apis": {"/b": f"DEFINE API '/b' FOR get THEN {{ RETURN '{_SECRET}-api' }}", "/a": "DEFINE API '/a'"},
        },
        issues=[issue],
    )


def _every_state(schema: SchemaState) -> list[object]:
    """*schema* and every state it holds."""
    states: list[object] = [schema, *schema.analyzers.values(), *schema.accesses.values(), *schema.issues]
    states += [*schema.functions.values(), *schema.params.values()]
    for table in schema.tables.values():
        states += [table, *table.fields.values(), *table.indexes.values(), *table.events.values()]
    return states


class TestRepr:
    """Spec §8, AC-10: no SurrealQL text the server returned reaches ``repr``/``str`` — names and structure do."""

    def test_no_definition_text_reaches_repr(self) -> None:
        schema = _secret_schema()
        states = _every_state(schema)
        assert len(states) == 10
        for state in states:
            for text in (repr(state), str(state)):
                assert _SECRET not in text, text
                for marker in ("DEFINE", "PASSHASH", "argon2", "KEY '", "http::", "WHERE", "SELECT", "$value"):
                    assert marker not in text, (marker, text)

    def test_names_and_structure_stay_in_repr(self) -> None:
        schema = _secret_schema()
        text = repr(schema)
        for name in ("'vault'", "'api.token'", "'by_token'", "'on_token'", "'acc'", "'an'", "'fn::login'"):
            assert name in text, name
        for name in ("'api_key'", "'broken'", "'isp'", "'users'", "'admin'", "'apis'", "'/a'", "'/b'", "'future'"):
            assert name in text, name
        assert "other={'apis': {'/a': ..., '/b': ...}, 'users': {'admin': ...}}" in text
        table = repr(schema.tables["vault"])
        assert "permissions={'delete': ..., 'select': ...}" in table
        assert "other={'future': {'thing': ...}}" in table
        assert "changefeed='1d'" in table
        assert "comment='secrets'" in table
        field_state = repr(schema.tables["vault"].fields["api.token"])
        for shown in ("field_type='string'", "nullable=False", "readonly=True", "reference=True", "table='vault'"):
            assert shown in field_state, shown
        assert "permissions={'select': ..., 'update': ...}" in field_state
        assert "comment='the API token'" in field_state
        access = repr(schema.accesses["acc"])
        for shown in ("table='user'", "signup_fields={'password': ...}", "duration_token='1h'", "with_refresh=True"):
            assert shown in access, shown
        assert "returns='string'" in repr(schema.functions["fn::login"])
        assert "parameters=('user',)" in repr(schema.functions["fn::login"])
        assert "fields=('api.token',)" in repr(schema.tables["vault"].indexes["by_token"])
        assert "message='could not parse its definition'" in repr(schema.issues[0])

    def test_event_async_attributes_stay_in_repr(self) -> None:
        """v0.31.0 (spec §5.5): ``async_mode``, ``retry`` and ``max_depth`` are flags and numbers, shown in ``repr``;
        ``when`` and ``then`` stay out of it."""
        text = repr(EventState("e", "t", "true", "(x)", async_mode=True, retry=2, max_depth=3))
        assert "async_mode=True, retry=2, max_depth=3" in text
        assert "(x)" not in text
        assert "'true'" not in text

    def test_the_texts_are_still_there(self) -> None:
        schema = _secret_schema()
        assert schema.other["users"]["admin"].endswith("ROLES OWNER")
        assert _SECRET in schema.accesses["acc"].raw
        assert _SECRET in schema.functions["fn::login"].raw
        assert _SECRET in schema.tables["vault"].permissions["select"]
        assert _SECRET in schema.tables["vault"].events["on_token"].then
        assert schema == _secret_schema()

    def test_a_round_tripped_schema_keeps_its_texts_out_of_repr(self) -> None:
        schema = _secret_schema()
        clones = [copy.copy(schema), copy.deepcopy(schema), pickle.loads(pickle.dumps(schema))]
        for clone in clones:
            assert clone == schema
            assert repr(clone) == repr(schema)
            assert _SECRET not in repr(clone)

    def test_text_maps_given_as_plain_maps_are_hidden(self) -> None:
        """A plain ``_FrozenMap`` (or ``dict``) given for a map of texts is rebuilt with its values hidden."""
        rules = frozen_map({"select": f"WHERE key = '{_SECRET}'"})
        assert _SECRET not in repr(FieldState(name="x", field_type="int", permissions=rules))
        assert _SECRET not in repr(TableState(name="t", permissions=rules, other={"s": rules}))
        assert _SECRET not in repr(AccessState(name="a", signup_fields=rules))
        assert _SECRET not in repr(SchemaState(other={"s": rules}))

    def test_param_value_and_raw_are_out_of_repr(self) -> None:
        param = ParamState(
            name="k",
            value="s3cr3t",
            permissions="FULL",
            comment=None,
            raw="DEFINE PARAM $k VALUE 's3cr3t' PERMISSIONS FULL",
        )
        text = repr(param)
        assert "s3cr3t" not in text
        assert "'k'" in text
        assert "FULL" not in text
        assert param.value == "s3cr3t"
        assert param == ParamState("k", "s3cr3t", "FULL", None, "DEFINE PARAM $k VALUE 's3cr3t' PERMISSIONS FULL")
        assert param != ParamState("k", "other", "FULL", None, "DEFINE PARAM $k VALUE 's3cr3t' PERMISSIONS FULL")

    def test_issue_statement_is_out_of_repr(self) -> None:
        issue = SchemaIssue("param", "api_key", None, "could not parse its definition: …", statement="DEFINE …secret…")
        assert "secret" not in repr(issue)
        assert "DEFINE" not in repr(issue)
        assert "api_key" in repr(issue)
        assert issue.statement == "DEFINE …secret…"
        assert SchemaIssue("param", "p", None, "m").statement is None

    def test_a_schema_with_a_param_keeps_its_value_out_of_repr(self) -> None:
        state = SchemaState(params={"k": ParamState("k", "'s3cr3t'", "FULL", None, "DEFINE PARAM $k VALUE 's3cr3t'")})
        assert "s3cr3t" not in repr(state)

    def test_maps_of_states_show_as_dicts_maps_of_texts_show_their_keys(self) -> None:
        field_state = FieldState(name="x", field_type="int", permissions={"update": "NONE", "select": "FULL"})
        assert "permissions={'select': ..., 'update': ...}" in repr(field_state)
        table = TableState(name="t", fields={"x": FieldState(name="x", field_type="int")})
        assert "fields={'x': FieldState(name='x', field_type='int', nullable=True, flexible=False" in repr(table)
