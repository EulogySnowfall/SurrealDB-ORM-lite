"""Issue #218 — the keys of sub-model fields in writes: aliases, ``merge()``/``bulk_update()`` dicts and instances.

A sub-model is read back by Pydantic, which takes each sub-field from the first of its read keys present (its aliases,
then its name under ``populate_by_name``). The ORM writes each sub-field under one key: the key ``model_dump()`` writes
when a read takes it, else the first key a read takes (a). ``merge()`` never hides the value it merges and never
removes a spelling that holds an object (b), and a sub-model instance in a keyword write is written as ``save()``
writes it (c). Models whose sub-models are read and written under their names send what v0.22.2 sent.

Every E2E test runs on both SurrealDB lines: nothing here depends on the server line.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from pydantic import AliasChoices, AliasPath, BaseModel, ConfigDict, Field, RootModel
from pydantic.alias_generators import to_camel

from surreal_orm_lite import (
    BaseSurrealModel,
    SchemaMode,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    SurrealFunc,
    _client,
    _sdk,
    _sub_keys,
)
from surreal_orm_lite.define_parser import render_kind
from surreal_orm_lite.migrations import ModelIntrospector, split_destructive
from surreal_orm_lite.utils import PLAIN_CONTENT
from tests._readme_tables import assert_server_header_sentence, assert_server_rows, doc_part
from tests.conftest import orm_client

# ==================== Models ====================


class Addr(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    zip_code: str = ""
    city_name: str = ""


class ProfAlias(BaseModel):
    """Dumped by alias: ``save()`` stores ``firstName``, ``homeAddr``, ``metaData``."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True)
    first_name: str = ""
    home_addr: Addr | None = None
    meta_data: dict[str, Any] = {}


class ProfName(BaseModel):
    """Dumped by name, read by alias first: ``save()`` stores ``first_name``, a read takes ``firstName`` first."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    first_name: str = ""
    nick_name: str | None = None
    home_addr: Addr | None = None


class Unreadable(BaseModel):
    """Case (a): ``model_dump()`` writes ``firstName``, which no read takes."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)
    first_name: str = Field(default="", validation_alias="fn", serialization_alias="firstName")


class AliasOnly(BaseModel):
    """Read by alias only: a dict naming the Python name used to store a key no read takes."""

    first_name: str = Field(default="", alias="firstName")


def _only(first_name: str) -> AliasOnly:
    """An ``AliasOnly`` built by its alias, the only key its validation takes."""
    return AliasOnly.model_validate({"firstName": first_name})


class Choices(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    nick: str = Field(default="", validation_alias=AliasChoices("nickName", "handle"))


class PathOnly(BaseModel):
    """Read only through an ``AliasPath`` of two steps: no plain key to write it under."""

    x: str = Field(default="", validation_alias=AliasPath("a", "b"))


class Node(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    leaf_name: str = ""
    child_node: Node | None = None


class Holder(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="sub_keys_holder", extra="allow")
    id: str | None = None
    by_alias: ProfAlias | None = None
    by_name: ProfName | None = None
    unreadable: Unreadable | None = None
    alias_only: AliasOnly | None = None
    choices: Choices | None = None
    many: list[ProfName] = []
    keyed: dict[str, ProfName] = {}
    node: Node | None = None
    hidden: ProfName | None = Field(default=None, exclude=True)


class PathHolder(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="sub_keys_path")
    id: str | None = None
    path_only: PathOnly | None = None


class PlainAddr(BaseModel):
    city: str = ""
    zip: str = ""


class Plain(BaseModel):
    first_name: str = ""
    home: PlainAddr | None = None
    tags: list[str] = []


class PlainHolder(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="sub_keys_plain")
    id: str | None = None
    profile: Plain | None = None
    items: list[Plain] = []
    score: int = 0


class StrictHolder(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="sub_keys_strict", schema_mode=SchemaMode.SCHEMAFULL)
    id: str | None = None
    by_name: ProfName | None = None


class StrictAliases(BaseSurrealModel):
    """A ``SCHEMAFULL`` table whose sub-models are written under aliases: ``define_table()`` declares those keys."""

    model_config = SurrealConfigDict(table_name="sub_keys_strict_alias", schema_mode=SchemaMode.SCHEMAFULL)
    id: str | None = None
    alias_only: AliasOnly | None = None
    by_alias: ProfAlias | None = None
    unreadable: Unreadable | None = None
    many: list[AliasOnly] = []


class AliasOnlyList(RootModel[list[AliasOnly]]):
    """A root model: dumped as its root, a list of sub-models read by alias only."""


class AliasOnlyRoot(RootModel[AliasOnly]):
    """A root model over one sub-model read by alias only."""


class Shaped(BaseSurrealModel):
    """Sub-models in a fixed tuple of different types and under root models."""

    model_config = SurrealConfigDict(table_name="sub_keys_shaped")
    id: str | None = None
    pair: tuple[AliasOnly, int] | None = None
    roots: AliasOnlyList | None = None
    root_one: AliasOnlyRoot | None = None


class StrictShaped(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="sub_keys_strict_shaped", schema_mode=SchemaMode.SCHEMAFULL)
    id: str | None = None
    pair: tuple[AliasOnly, int] | None = None
    roots: AliasOnlyList | None = None
    root_one: AliasOnlyRoot | None = None


TABLES = (
    "sub_keys_holder",
    "sub_keys_path",
    "sub_keys_plain",
    "sub_keys_strict",
    "sub_keys_strict_alias",
    "sub_keys_shaped",
    "sub_keys_strict_shaped",
)


async def _row(client: Any, table: str, key: str) -> dict[str, Any]:
    rows = await _client.query(client, f"SELECT * FROM {table} WHERE id = $rid;", {"rid": _sdk.RecordID(table, key)})
    assert len(rows) == 1
    row: dict[str, Any] = rows[0]
    return row


# ==================== Keys (no server) ====================


class TestSubModelKeys:
    def test_read_keys_and_written_key(self) -> None:
        name = _sub_keys.keys_of(ProfName).fields["first_name"]
        assert (name.reads, name.key) == (("firstName", "first_name"), "first_name")
        alias = _sub_keys.keys_of(ProfAlias).fields["first_name"]
        assert (alias.reads, alias.key) == (("firstName", "first_name"), "firstName")
        unreadable = _sub_keys.keys_of(Unreadable).fields["first_name"]
        assert (unreadable.reads, unreadable.key) == (("fn", "first_name"), "fn")
        only = _sub_keys.keys_of(AliasOnly).fields["first_name"]
        assert (only.reads, only.key) == (("firstName",), "firstName")
        choices = _sub_keys.keys_of(Choices).fields["nick"]
        assert (choices.reads, choices.key) == (("nickName", "handle", "nick"), "nick")
        assert _sub_keys.keys_of(PathOnly).fields["x"].key is None

    def test_ddl_declares_the_keys_the_orm_writes(self) -> None:
        """``define_table()`` declares each sub-model field under the key a write sends: an alias read alone, a
        ``serialize_by_alias`` dump, the read key of an unread ``serialization_alias``; plain sub-models by name."""
        paths = {column.path for column in StrictAliases._table_columns()}
        for path in (
            ("alias_only", "firstName"),
            ("by_alias", "firstName"),
            ("by_alias", "homeAddr"),
            ("by_alias", "homeAddr", "zip_code"),
            ("by_alias", "metaData"),
            ("unreadable", "fn"),
            ("many", "*", "firstName"),
        ):
            assert path in paths, path
        assert not {("alias_only", "first_name"), ("by_alias", "first_name"), ("unreadable", "first_name")} & paths
        by_name = {column.path for column in StrictHolder._table_columns()}
        assert {("by_name", "first_name"), ("by_name", "home_addr", "city_name")} <= by_name
        assert {column.path for column in PlainHolder._table_columns()} >= {
            ("profile", "first_name"),
            ("items", "*", "home", "city"),
        }

    def test_migration_from_an_earlier_ddl_adds_the_new_key_and_proposes_the_old_ones_removal(self) -> None:
        """A table an earlier version defined declares ``alias_only.first_name``: the diff adds ``alias_only.firstName``
        and its ``DropField`` of the old key is the removal ``makemigrations()`` writes only with ``drop_missing``."""
        target = ModelIntrospector([StrictAliases]).introspect()
        table = target.tables["sub_keys_strict_alias"]
        new = table.fields["alias_only.firstName"]
        fields = {name: field for name, field in table.fields.items() if name != "alias_only.firstName"}
        fields["alias_only.first_name"] = replace(new, name="alias_only.first_name", path=["alias_only", "first_name"])
        current = replace(target, tables={**target.tables, "sub_keys_strict_alias": replace(table, fields=fields)})
        keep, removals = split_destructive(current.diff(target))
        assert [operation.describe() for operation in keep] == ["Add field alias_only.firstName to sub_keys_strict_alias"]
        assert [operation.describe() for operation in removals] == [
            "Drop field alias_only.first_name from sub_keys_strict_alias"
        ]

    def test_a_save_rekeys_a_fixed_tuple_and_root_models(self) -> None:
        """(a) a sub-model in a fixed tuple of different types or under a ``RootModel`` is stored under the key a read
        takes (``firstName``), not under its name, which no read takes."""
        holder = Shaped(id="r", pair=(_only("Ann"), 1), roots=AliasOnlyList([_only("Bo")]), root_one=AliasOnlyRoot(_only("Cy")))
        payload = holder._write_payload()
        assert payload == {"pair": ({"firstName": "Ann"}, 1), "roots": [{"firstName": "Bo"}], "root_one": {"firstName": "Cy"}}
        assert Shaped.model_validate(payload) == holder.model_copy(update={"id": None})

    def test_keyword_writes_rekey_a_fixed_tuple_and_root_models(self) -> None:
        """(b)/(c) ``bulk_update()``, ``merge()`` and an instance in a keyword write: the keys ``save()`` writes."""
        given = {"pair": [{"first_name": "B"}, 2], "roots": [{"first_name": "C"}], "root_one": {"first_name": "D"}}
        expected = {"pair": [{"firstName": "B"}, 2], "roots": [{"firstName": "C"}], "root_one": {"firstName": "D"}}
        assert Shaped._assigned_values(given) == expected
        merge_content, bound = Shaped._merge_content(given)
        assert merge_content == PLAIN_CONTENT and bound["data"] == expected
        instances = {"pair": (_only("E"), 3), "roots": AliasOnlyList([_only("F")]), "root_one": AliasOnlyRoot(_only("G"))}
        saved = Shaped(id="i", **instances)._write_payload()
        assert Shaped._assigned_values(instances) == saved
        assert Shaped._merge_content(instances)[1]["data"] == saved
        assert Shaped._assigned_values({"pair": [{"first_name": "x"}]}) == {"pair": [{"first_name": "x"}]}  # not a pair

    def test_ddl_of_a_fixed_tuple_and_root_models(self) -> None:
        """``define_table()`` follows: a root model stands for its root (an array, not an ``object`` with a ``root``
        field), its sub-model's fields under the keys written; an object in a fixed tuple is free-form."""
        columns = {column.path: column for column in StrictShaped._table_columns()}
        assert set(columns) == {("pair",), ("roots",), ("roots", "*", "firstName"), ("root_one",), ("root_one", "firstName")}
        assert render_kind(columns[("pair",)].kind) == "option<[object, int]>" and columns[("pair",)].free_object
        assert render_kind(columns[("roots",)].kind) == "option<array<object>>"
        assert render_kind(columns[("root_one",)].kind) == "option<object>"
        assert not columns[("roots",)].free_object and not columns[("root_one",)].free_object

    def test_plain_sub_models_have_no_shape(self) -> None:
        assert _sub_keys.keys_of(Plain).plain
        assert all(_sub_keys.field_shape(PlainHolder, name) is None for name in PlainHolder.model_fields)
        assert _sub_keys.shapes_of(PlainHolder) == {}

    def test_a_save_stores_the_key_a_read_takes(self) -> None:
        """(a) ``firstName`` would be stored, which no read takes; the ORM stores ``fn``, which the read takes first."""
        holder = Holder(id="a", unreadable=Unreadable(first_name="Annie"))
        assert holder.unreadable is not None
        assert holder.unreadable.model_dump() == {"firstName": "Annie"}  # what v0.22.2 stored
        stored = holder._write_payload()["unreadable"]
        assert stored == {"fn": "Annie"}
        assert Unreadable.model_validate(stored).first_name == "Annie"

    def test_a_save_refuses_a_field_no_plain_key_reads(self) -> None:
        with pytest.raises(ValueError, match=r"PathOnly\.x is read only through an AliasPath"):
            PathHolder(id="p", path_only=PathOnly(x="v"))._write_payload()

    def test_a_save_by_alias_or_by_name_is_unchanged(self) -> None:
        """A dumped key a read takes is kept: ``save()`` writes what it always wrote."""
        holder = Holder(id="h", by_alias=ProfAlias(first_name="A"), by_name=ProfName(first_name="B"))
        payload = holder._write_payload()
        assert payload["by_alias"] == holder.model_dump()["by_alias"]
        assert payload["by_name"] == holder.model_dump()["by_name"]
        assert "firstName" in payload["by_alias"] and "first_name" in payload["by_name"]

    def test_plain_payloads_are_the_given_objects(self) -> None:
        """No alias on the sub-models: the very objects given, as v0.22.2 sent them."""
        profile = {"first_name": "x", "home": {"city": "Paris"}, "unknown": 1}
        items = [{"first_name": "y"}]
        merge_content, bound = PlainHolder._merge_content({"profile": profile, "items": items, "score": 3})
        assert merge_content == PLAIN_CONTENT
        assert bound["data"]["profile"] is profile and bound["data"]["items"] is items
        assigned = PlainHolder._assigned_values({"profile": profile, "items": items})
        assert assigned["profile"] is profile and assigned["items"] is items
        holder = PlainHolder(id="p", profile=Plain(first_name="x", home=PlainAddr(city="Paris")), items=[Plain()])
        assert holder._write_payload() == holder.model_dump(exclude={"id"})

    def test_a_dict_naming_a_field_twice_is_refused(self) -> None:
        with pytest.raises(ValueError, match=r"ProfName\.first_name is named twice"):
            Holder._merge_content({"by_name": {"first_name": "a", "firstName": "b"}})
        with pytest.raises(ValueError, match=r"ProfName\.first_name is named twice"):
            Holder._assigned_values({"by_name": {"first_name": "a", "firstName": "b"}})

    def test_merge_payload_of_a_scalar_removes_the_spellings_a_read_takes_first(self) -> None:
        merge_content, bound = Holder._merge_content({"by_name": {"firstName": "Bob"}, "choices": {"handle": "C"}})
        assert merge_content == PLAIN_CONTENT
        assert bound["data"] == {
            "by_name": {"first_name": "Bob", "firstName": None},
            "choices": {"nick": "C", "nickName": None, "handle": None},
        }

    def test_merge_payload_of_none_removes_every_spelling(self) -> None:
        _, bound = Holder._merge_content({"by_name": {"nick_name": None}})
        assert bound["data"] == {"by_name": {"nickName": None, "nick_name": None}}

    def test_merge_payload_of_an_object_picks_the_stored_spelling(self) -> None:
        merge_content, bound = Holder._merge_content({"by_name": {"homeAddr": {"zipCode": "3"}}})
        assert merge_content == (
            "object::from_entries([[$m0, object::from_entries([[IF $this[$m1][$m2] != NONE THEN $m2 "
            "ELSE IF $this[$m1][$m3] != NONE THEN $m3 ELSE $m4 END, $m5]])]])"
        )
        assert bound == {
            "m0": "by_name",
            "m1": "by_name",
            "m2": "homeAddr",
            "m3": "home_addr",
            "m4": "home_addr",
            "m5": {"zip_code": "3", "zipCode": None},
        }

    def test_instances_are_dumped_as_save_dumps_them(self) -> None:
        """(c) the keys ``save()`` writes, never the SDK's ``BufferError``."""
        profile = ProfAlias(first_name="A", home_addr=Addr(zip_code="1"))
        saved = Holder(id="h", by_alias=profile)._write_payload()["by_alias"]
        assert Holder._assigned_values({"by_alias": profile})["by_alias"] == saved
        _, bound = Holder._merge_content({"many": [ProfName(first_name="x")], "unreadable": Unreadable(first_name="u")})
        assert bound["data"]["many"] == [{"first_name": "x", "nick_name": None, "home_addr": None}]
        assert bound["data"]["unreadable"] == {"fn": "u"}

    def test_an_instance_save_would_not_write_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not a field save\\(\\) writes"):
            Holder._assigned_values({"hidden": ProfName(first_name="x")})
        with pytest.raises(ValueError, match="not a field save\\(\\) writes"):
            PlainHolder._assigned_values({"undeclared": Plain()})

    def test_an_extra_instance_is_dumped(self) -> None:
        assert Holder._assigned_values({"loose": Plain(first_name="z")}) == {
            "loose": {"first_name": "z", "home": None, "tags": []}
        }

    def test_too_many_picked_objects_are_refused(self) -> None:
        deep: dict[str, Any] = {"leaf_name": "x"}
        for _ in range(_sub_keys.MAX_PICKS + 1):
            deep = {"child_node": deep}
        with pytest.raises(ValueError, match="at most 3 nested sub-models"):
            Holder._merge_content({"node": deep})


# ==================== E2E, both lines ====================


class TestSubModelKeysE2E:
    @pytest.mark.asyncio
    async def test_a_round_trips_a_serialization_alias_no_read_takes(self) -> None:
        async with orm_client(*TABLES) as client:
            await Holder(id="a", unreadable=Unreadable(first_name="Annie")).save()
            got = await Holder.objects().get("a")
            assert got.unreadable is not None and got.unreadable.first_name == "Annie"
            await got.merge(unreadable={"first_name": "Bea"})
            got = await Holder.objects().get("a")
            assert got.unreadable is not None and got.unreadable.first_name == "Bea"
            assert (await _row(client, "sub_keys_holder", "a"))["unreadable"] == {"fn": "Bea"}

    @pytest.mark.asyncio
    async def test_b_merge_into_a_sub_model_dumped_by_alias(self) -> None:
        """#219's scenario: nothing stored is lost, the merged values are the values read."""
        async with orm_client(*TABLES) as client:
            profile = ProfAlias(first_name="Ann", home_addr=Addr(zip_code="1", city_name="Paris"), meta_data={"b": 2})
            holder = await Holder(id="b", by_alias=profile).save()
            await holder.merge(by_alias={"homeAddr": {"zipCode": "3"}, "metaData": {"a": 1}})
            await holder.merge(by_alias={"first_name": "Bob", "home_addr": {"city_name": "Lyon"}})
            got = await Holder.objects().get("b")
            assert got.by_alias == ProfAlias(
                first_name="Bob", home_addr=Addr(zip_code="3", city_name="Lyon"), meta_data={"a": 1, "b": 2}
            )
            assert holder.by_alias == got.by_alias
            stored = (await _row(client, "sub_keys_holder", "b"))["by_alias"]
            assert stored == {
                "firstName": "Bob",
                "homeAddr": {"zip_code": "3", "city_name": "Lyon"},
                "metaData": {"a": 1, "b": 2},
            }

    @pytest.mark.asyncio
    async def test_b_merge_into_a_sub_model_dumped_by_name(self) -> None:
        async with orm_client(*TABLES) as client:
            holder = await Holder(
                id="n", by_name=ProfName(first_name="Ann", home_addr=Addr(zip_code="1", city_name="Paris"))
            ).save()
            await holder.merge(by_name={"firstName": "Bob", "homeAddr": {"zipCode": "3"}})
            got = await Holder.objects().get("n")
            assert got.by_name == ProfName(first_name="Bob", home_addr=Addr(zip_code="3", city_name="Paris"))
            stored = (await _row(client, "sub_keys_holder", "n"))["by_name"]
            assert stored == {"first_name": "Bob", "home_addr": {"zip_code": "3", "city_name": "Paris"}}

    @pytest.mark.asyncio
    async def test_b_rows_holding_both_spellings(self) -> None:
        """A row an earlier merge left with both spellings: the merged value is the value read, no object removed."""
        async with orm_client(*TABLES) as client:
            await _client.query(
                client,
                "CREATE sub_keys_holder:l CONTENT $data;",
                {
                    "data": {
                        "by_name": {
                            "first_name": "Ann",
                            "firstName": "Legacy",
                            "nick_name": "n1",
                            "nickName": "n2",
                            "home_addr": {"zip_code": "1", "city_name": "Paris"},
                            "homeAddr": {"zipCode": "9"},
                        },
                        "choices": {"nickName": "A", "handle": "B"},
                    }
                },
            )
            got = await Holder.objects().get("l")
            assert got.by_name == ProfName(first_name="Legacy", nick_name="n2", home_addr=Addr(zip_code="9"))
            assert got.choices == Choices(nick="A")
            await got.merge(by_name={"first_name": "New", "nick_name": None, "home_addr": {"city_name": "Lyon"}})
            await got.merge(choices={"handle": "C"})
            again = await Holder.objects().get("l")
            assert again.by_name == ProfName(first_name="New", nick_name=None, home_addr=Addr(zip_code="9", city_name="Lyon"))
            assert again.choices == Choices(nick="C")
            stored = (await _row(client, "sub_keys_holder", "l"))["by_name"]
            assert stored["home_addr"] == {"zip_code": "1", "city_name": "Paris"}  # never removed
            assert "nick_name" not in stored and "nickName" not in stored

    @pytest.mark.asyncio
    async def test_b_dict_and_list_of_sub_models(self) -> None:
        async with orm_client(*TABLES) as client:
            await _client.query(
                client,
                "CREATE sub_keys_holder:k CONTENT $data;",
                {"data": {"keyed": {"x": {"firstName": "Old", "homeAddr": {"zipCode": "9", "cityName": "Nice"}}}}},
            )
            got = await Holder.objects().get("k")
            await got.merge(keyed={"x": {"first_name": "New", "home_addr": {"zip_code": "1"}}}, many=[{"firstName": "m"}])
            again = await Holder.objects().get("k")
            assert again.keyed == {"x": ProfName(first_name="New", home_addr=Addr(zip_code="1", city_name="Nice"))}
            assert again.many == [ProfName(first_name="m")]
            assert (await _row(client, "sub_keys_holder", "k"))["many"] == [{"first_name": "m"}]

    @pytest.mark.asyncio
    async def test_b_three_picked_levels(self) -> None:
        async with orm_client(*TABLES):

            def chain(last: str) -> Node:
                return Node(
                    leaf_name="0",
                    child_node=Node(leaf_name="1", child_node=Node(leaf_name="2", child_node=Node(leaf_name=last))),
                )

            holder = await Holder(id="d", node=chain("3")).save()
            await holder.merge(node={"childNode": {"childNode": {"childNode": {"leafName": "x"}}}})
            got = await Holder.objects().get("d")
            assert got.node == chain("x")

    @pytest.mark.asyncio
    async def test_b_refresh_false_and_transaction(self) -> None:
        async with orm_client(*TABLES):
            holder = await Holder(
                id="t", by_name=ProfName(first_name="Ann", home_addr=Addr(zip_code="1", city_name="Paris"))
            ).save()
            await holder.merge(by_name={"homeAddr": {"zipCode": "2"}}, refresh=False)
            got = await Holder.objects().get("t")
            assert got.by_name == ProfName(first_name="Ann", home_addr=Addr(zip_code="2", city_name="Paris"))
            async with SurrealDBConnectionManager.transaction() as tx:
                await got.merge(tx=tx, by_name={"firstName": "Tx", "homeAddr": {"zipCode": "3"}})
            got = await Holder.objects().get("t")
            assert got.by_name == ProfName(first_name="Tx", home_addr=Addr(zip_code="3", city_name="Paris"))

    @pytest.mark.asyncio
    async def test_b_server_values_and_bulk_update_assign_the_whole_sub_model(self) -> None:
        """``SET`` replaces the stored object: its keys are those ``save()`` writes, so no other spelling hides them."""
        async with orm_client(*TABLES) as client:
            await _client.query(
                client,
                "CREATE sub_keys_holder:s CONTENT $data;",
                {"data": {"by_name": {"first_name": "Ann", "firstName": "Legacy"}, "by_alias": {"first_name": "Old"}}},
            )
            got = await Holder.objects().get("s")
            await got.merge(by_name={"firstName": "SV"}, server_values={"stamp": SurrealFunc("1")})
            assert got.by_name == ProfName(first_name="SV")
            count = (
                await Holder.objects().filter(id="s").bulk_update(by_alias={"first_name": "Bulk", "homeAddr": {"zipCode": "5"}})
            )
            assert count == 1
            again = await Holder.objects().get("s")
            assert again.by_name == ProfName(first_name="SV")
            assert again.by_alias == ProfAlias(first_name="Bulk", home_addr=Addr(zip_code="5"))
            row = await _row(client, "sub_keys_holder", "s")
            assert row["by_name"] == {"first_name": "SV"}
            assert row["by_alias"] == {"firstName": "Bulk", "homeAddr": {"zip_code": "5"}}

    @pytest.mark.asyncio
    async def test_c_instances_in_keyword_writes(self) -> None:
        async with orm_client(*TABLES) as client:
            profile = ProfAlias(first_name="Ann", home_addr=Addr(zip_code="1", city_name="Paris"), meta_data={"b": 2})
            await Holder(id="c", by_alias=profile).save()
            saved = (await _row(client, "sub_keys_holder", "c"))["by_alias"]
            await Holder(id="c2").save()
            got = await Holder.objects().get("c2")
            await got.merge(by_alias=profile, many=[ProfName(first_name="m")])
            assert (await _row(client, "sub_keys_holder", "c2"))["by_alias"] == saved
            await (
                Holder.objects()
                .filter(id="c2")
                .bulk_update(by_name=ProfName(first_name="b"), unreadable=Unreadable(first_name="u"))
            )
            again = await Holder.objects().get("c2")
            assert again.by_alias == profile and again.many == [ProfName(first_name="m")]
            assert again.by_name == ProfName(first_name="b") and again.unreadable == Unreadable(first_name="u")

    @pytest.mark.asyncio
    async def test_b_schemafull_table_keeps_the_declared_names(self) -> None:
        """``define_table()`` declares the sub-fields by name; alias keys of a merge dict land there, on both lines."""
        async with orm_client(*TABLES):
            await StrictHolder.define_table()
            holder = await StrictHolder(
                id="f", by_name=ProfName(first_name="Ann", home_addr=Addr(zip_code="1", city_name="Paris"))
            ).save()
            await holder.merge(by_name={"firstName": "Bob", "homeAddr": {"zipCode": "2"}})
            got = await StrictHolder.objects().get("f")
            assert got.by_name == ProfName(first_name="Bob", home_addr=Addr(zip_code="2", city_name="Paris"))

    @pytest.mark.asyncio
    async def test_a_schemafull_table_takes_the_keys_the_orm_writes(self) -> None:
        """A sub-model read by alias only, or dumped by alias, on a ``SCHEMAFULL`` table the ORM defined: every write
        lands in a declared column and reads back, on both lines (3.x refused it, 2.x dropped it without the DDL)."""
        async with orm_client(*TABLES) as client:
            await StrictAliases.define_table()
            holder = await StrictAliases(
                id="s",
                alias_only=_only("Ann"),
                by_alias=ProfAlias(first_name="Bo", home_addr=Addr(zip_code="1"), meta_data={"k": 1}),
                unreadable=Unreadable(first_name="Cy"),
                many=[_only("Di")],
            ).save()
            got = await StrictAliases.objects().get("s")
            assert got.alias_only == _only("Ann") and got.many == [_only("Di")]
            assert got.by_alias == ProfAlias(first_name="Bo", home_addr=Addr(zip_code="1"), meta_data={"k": 1})
            assert got.unreadable == Unreadable(first_name="Cy")
            row = await _row(client, "sub_keys_strict_alias", "s")
            assert row["alias_only"] == {"firstName": "Ann"} and row["unreadable"] == {"fn": "Cy"}
            await holder.merge(alias_only={"first_name": "Eve"}, by_alias={"homeAddr": {"cityName": "Lyon"}})
            await StrictAliases.objects().filter(id="s").bulk_update(many=[{"firstName": "Fay"}])
            got = await StrictAliases.objects().get("s")
            assert got.alias_only == _only("Eve") and got.many == [_only("Fay")]
            assert got.by_alias is not None and got.by_alias.home_addr == Addr(zip_code="1", city_name="Lyon")

    @pytest.mark.asyncio
    async def test_a_fixed_tuple_and_root_models_round_trip(self) -> None:
        """A sub-model read by alias only, in a fixed tuple of different types or under a root model: every write
        reads back, on a schemaless table and on a ``SCHEMAFULL`` table the ORM defined, on both lines."""
        async with orm_client(*TABLES) as client:
            await StrictShaped.define_table()
            for model, table in ((Shaped, "sub_keys_shaped"), (StrictShaped, "sub_keys_strict_shaped")):
                holder = await model(
                    id="r", pair=(_only("Ann"), 1), roots=AliasOnlyList([_only("Bo")]), root_one=AliasOnlyRoot(_only("Cy"))
                ).save()
                got = await model.objects().get("r")
                assert got.pair == (_only("Ann"), 1), table
                assert got.roots == AliasOnlyList([_only("Bo")]) and got.root_one == AliasOnlyRoot(_only("Cy")), table
                row = await _row(client, table, "r")
                assert row["pair"] == [{"firstName": "Ann"}, 1] and row["roots"] == [{"firstName": "Bo"}], table
                await holder.merge(pair=[{"first_name": "Dee"}, 2], root_one={"first_name": "Eve"})
                await model.objects().filter(id="r").bulk_update(roots=[{"first_name": "Fay"}, {"firstName": "Gus"}])
                got = await model.objects().get("r")
                assert got.pair == (_only("Dee"), 2) and got.root_one == AliasOnlyRoot(_only("Eve")), table
                assert got.roots == AliasOnlyList([_only("Fay"), _only("Gus")]), table

    @pytest.mark.asyncio
    async def test_plain_models_send_v0_22_2_bytes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No alias on the sub-models: every write sends, encoded, the bytes v0.22.2 sent."""
        sent: dict[str, list[Any]] = {"create": [], "update": [], "merge": [], "query": []}
        for name in sent:
            real = getattr(_client, name)

            def spy(*args: Any, _real: Any = real, _name: str = name, **kwargs: Any) -> Any:
                sent[_name].append(args[2] if len(args) > 2 else kwargs.get("variables"))
                return _real(*args, **kwargs)

            monkeypatch.setattr(_client, name, spy)
        async with orm_client(*TABLES):
            holder = PlainHolder(id="p", profile=Plain(first_name="x", home=PlainAddr(city="Paris")), items=[Plain(tags=["t"])])
            expected_row = holder.model_dump(exclude={"id"})  # what v0.22.2 sent for save() and update()
            await holder.save()
            await holder.update()
            profile = {"first_name": "y", "home": {"zip": "1"}, "unknown": 2}
            await holder.merge(profile=profile)
            await holder.merge(profile=profile, server_values={"score": SurrealFunc("1")})
            await PlainHolder.objects().filter(id="p").bulk_update(profile=profile, items=[{"first_name": "z"}])
        assert [_sdk.cbor_encode(data) for data in sent["create"]] == [_sdk.cbor_encode(expected_row)]
        assert [_sdk.cbor_encode(data) for data in sent["update"]] == [_sdk.cbor_encode(expected_row)]
        assert [_sdk.cbor_encode(data) for data in sent["merge"]] == [_sdk.cbor_encode({"profile": profile})]
        assert sent["merge"][0]["profile"] is profile
        server = next(variables for variables in sent["query"] if variables and "_sv_profile" in variables)
        assert _sdk.cbor_encode(server["_sv_profile"]) == _sdk.cbor_encode(profile)
        bulk = next(variables for variables in sent["query"] if variables and "_v0" in variables)
        assert _sdk.cbor_encode([bulk["_v0"], bulk["_v1"]]) == _sdk.cbor_encode([profile, [{"first_name": "z"}]])


# ==================== Docs ====================

_SERVER_ROW = [
    "Sub-model keys in writes (aliases, `merge()`/`bulk_update()` dicts and instances)",
    "same on both lines: each sub-field under the key `save()` writes, else the first key a read takes, also in "
    "`define_table()`; `merge()` keeps the stored spelling of an object",
    "same on both lines (measured on 3.3.5)",
    "v0.50.10",
]
_HEADER_SENTENCE = "The row added in v0.50.10 was measured on 2.7.0, 3.3.5 and 4.0.1."


class TestSubModelKeysDocs:
    def test_behaviour_table(self) -> None:
        assert_server_rows([_SERVER_ROW])
        assert_server_header_sentence(_HEADER_SENTENCE)

    def test_models_guide_section(self) -> None:
        part = " ".join(doc_part("### Sub-model keys and aliases").split())
        for text in (
            'Field(validation_alias="fn", serialization_alias="firstName")',
            "Naming one field twice raises `ValueError`",
            "written as `save()`",
            "assign the field whole (`SET`)",
            "up to three such objects nested",
            "`define_table()` (and `table_ddl()`) declares each sub-model field under that same key",
            'A field read by its alias only (`Field(alias="firstName")`',
            "A generated migration proposes that removal",
            "`drop_missing=True` (`--drop-missing`)",
            "UPDATE <table> SET p.firstName = p.first_name WHERE p.first_name != NONE AND p.firstName = NONE",
        ):
            assert text in part, text
