"""Tests for v0.22.0 — ``SurrealConfigDict(table_name=…)``, the full SurrealDB-ORM's config key.

Every path reaches the table through ``get_table_name()`` — model writes and reads, the
``QuerySet``, live queries, record-id coercion, ``computed_field_ddl()``, ``access_ddl()``,
relation targets — so one key renames the model's table everywhere. A path that kept reading
the class name would silently split one model over two tables; the E2E tests below prove there
is none by asking the server which tables exist (``INFO FOR DB``).

The legacy ``_table_name: ClassVar[str]`` spelling was only ever honoured by reads. It is now a
deprecated fallback honoured by reads **and** writes, with a ``DeprecationWarning`` attributed
to the module holding the ``class`` statement.

E2E tables are dedicated (``tn_*``, plus the class names that must stay absent) and dropped on
entry and exit by ``orm_client``. Same behaviour on SurrealDB 2.x and 3.x.
"""

import asyncio
import warnings
from typing import Any, ClassVar

import pytest
from surrealdb import RecordID

from surreal_orm_lite import (
    AuthenticatedUserMixin,
    BaseSurrealModel,
    Computed,
    SurrealConfigDict,
    computed,
)
from surreal_orm_lite.enum import LiveAction
from tests.conftest import orm_client

TN_TABLES = ("tn_item_tbl", "TnItem", "tn_holder", "tn_holds", "tn_legacy", "TnLegacy")


class TnThing(BaseSurrealModel):
    id: str | None = None


class TnItem(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="tn_item_tbl")

    id: str | None = None
    name: str = ""
    qty: int = 0


class TnHolder(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="tn_holder")

    id: str | None = None


def _table_name_warnings(records: Any) -> list[warnings.WarningMessage]:
    """The ``DeprecationWarning``s about ``_table_name`` among ``records``."""
    return [w for w in records if issubclass(w.category, DeprecationWarning) and "_table_name" in str(w.message)]


async def _db_tables(client: Any) -> set[str]:
    """The table names ``INFO FOR DB`` reports."""
    info = await client.query("INFO FOR DB;", {})
    assert isinstance(info, dict)
    return set(info["tables"])


class TestTableNameConfig:
    def test_default_is_class_name(self) -> None:
        assert TnThing.get_table_name() == "TnThing"
        assert TnThing.objects()._model_table == "TnThing"

    def test_explicit_none_is_the_default(self) -> None:
        class TnNone(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name=None)
            id: str | None = None

        assert TnNone.get_table_name() == "TnNone"

    def test_configured_name_is_returned(self) -> None:
        assert TnItem.get_table_name() == "tn_item_tbl"
        assert TnItem.objects()._model_table == "tn_item_tbl"
        assert TnItem(id="a")._record_id() == RecordID("tn_item_tbl", "a")

    def test_other_config_keys_are_kept(self) -> None:
        """Pydantic merges the parent config: ``populate_by_name`` survives ``table_name``."""
        assert TnItem.model_config.get("populate_by_name") is True

    def test_subclass_inherits_table_name(self) -> None:
        class TnItemChild(TnItem):
            extra: str = ""

        assert TnItemChild.get_table_name() == "tn_item_tbl"
        assert TnItemChild.objects()._model_table == "tn_item_tbl"

    def test_subclass_can_override_table_name(self) -> None:
        class TnItemOther(TnItem):
            model_config = SurrealConfigDict(table_name="tn_other")

        assert TnItemOther.get_table_name() == "tn_other"
        assert TnItem.get_table_name() == "tn_item_tbl"

    @pytest.mark.parametrize("name", ["bad-name", "a;b", "a b", "", "1abc", "tbl\n"])
    def test_invalid_table_name_rejected_at_class_definition(self, name: str) -> None:
        with pytest.raises(ValueError, match="must be an identifier"):

            class TnBad(BaseSurrealModel):
                model_config = SurrealConfigDict(table_name=name)
                id: str | None = None

    def test_non_string_table_name_rejected_at_class_definition(self) -> None:
        with pytest.raises(TypeError, match="table_name"):

            class TnBadType(BaseSurrealModel):
                model_config = SurrealConfigDict(table_name=5)  # type: ignore[typeddict-item]
                id: str | None = None

    def test_computed_field_ddl_uses_table_name(self) -> None:
        class TnComputed(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tn_item_tbl")
            id: str | None = None
            qty: int = 0
            doubled: Computed[int] = computed("qty * 2")

        statements = TnComputed.computed_field_ddl()
        assert statements == ["DEFINE FIELD OVERWRITE doubled ON tn_item_tbl VALUE qty * 2;"]

    def test_access_ddl_uses_table_name(self) -> None:
        class TnAcctUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="tn_acct_user")
            email: str
            password: str

        table_ddl, access_ddl = TnAcctUser.access_ddl()
        assert table_ddl.startswith("DEFINE TABLE OVERWRITE tn_acct_user ")
        assert access_ddl.startswith("DEFINE ACCESS OVERWRITE tn_acct_user_access ")
        assert "CREATE tn_acct_user SET" in access_ddl
        assert "SELECT * FROM tn_acct_user WHERE" in access_ddl
        assert "TnAcctUser" not in table_ddl + access_ddl

    def test_legacy_table_name_is_deprecated_but_honoured(self) -> None:
        with pytest.warns(DeprecationWarning, match="_table_name") as records:

            class TnLegacyUnit(BaseSurrealModel):
                _table_name: ClassVar[str] = "tn_legacy"
                id: str | None = None

        ours = _table_name_warnings(records)
        assert len(ours) == 1
        # Attributed to the class statement — not to pydantic's metaclass nor to the ORM.
        assert ours[0].filename == __file__
        message = str(ours[0].message)
        assert "TnLegacyUnit._table_name is deprecated" in message
        assert "SurrealConfigDict(table_name=" in message
        assert "wins" not in message
        assert TnLegacyUnit.get_table_name() == "tn_legacy"
        assert TnLegacyUnit.objects()._model_table == "tn_legacy"
        assert TnLegacyUnit(id="a")._record_id() == RecordID("tn_legacy", "a")

    def test_table_name_wins_over_legacy(self) -> None:
        with pytest.warns(DeprecationWarning, match="table_name wins") as records:

            class TnBoth(BaseSurrealModel):
                model_config = SurrealConfigDict(table_name="tn_both")
                _table_name: ClassVar[str] = "tn_legacy"
                id: str | None = None

        assert len(_table_name_warnings(records)) == 1
        assert TnBoth.get_table_name() == "tn_both"
        assert TnBoth.objects()._model_table == "tn_both"

    def test_legacy_private_attr_is_ignored(self) -> None:
        """Without ``ClassVar`` it is a Pydantic private attribute: it never named a table."""
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter("always")

            class TnPrivate(BaseSurrealModel):
                _table_name = "x"
                id: str | None = None

            class TnPrivateAnnotated(BaseSurrealModel):
                _table_name: str = "x"
                id: str | None = None

        assert _table_name_warnings(records) == []
        for model in (TnPrivate, TnPrivateAnnotated):
            assert model.get_table_name() == model.__name__
            # It used to be the repr of a ModelPrivateAttr, interpolated into SurrealQL.
            assert model.objects()._model_table == model.__name__

    def test_legacy_subclass_does_not_warn_again(self) -> None:
        with pytest.warns(DeprecationWarning, match="_table_name"):

            class TnLegacyParent(BaseSurrealModel):
                _table_name: ClassVar[str] = "tn_legacy"
                id: str | None = None

        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter("always")

            class TnLegacyChild(TnLegacyParent):
                extra: str = ""

        assert _table_name_warnings(records) == []
        assert TnLegacyChild.get_table_name() == "tn_legacy"
        assert TnLegacyChild.objects()._model_table == "tn_legacy"

    @pytest.mark.parametrize("name", ["a;b", "bad-name", ""])
    def test_invalid_legacy_table_name_rejected(self, name: str) -> None:
        with pytest.raises(ValueError, match="_table_name"):

            class TnLegacyBad(BaseSurrealModel):
                _table_name: ClassVar[str] = name
                id: str | None = None


class TestTableNameE2E:
    @pytest.mark.asyncio
    async def test_table_name_is_honoured_by_every_path(self) -> None:
        async with orm_client(*TN_TABLES) as client:
            explicit = await TnItem(id="a", name="alpha", qty=1).save()
            assert explicit.get_id() == "a"

            auto = await TnItem(name="auto", qty=2).save()
            assert isinstance(auto.id, RecordID)
            assert auto.id.table_name == "tn_item_tbl"

            fetched = await TnItem.objects().get("a")
            assert isinstance(fetched, TnItem)
            assert fetched.name == "alpha"

            matched = await TnItem.objects().filter(qty__gte=1).exec()
            assert sorted(item.name for item in matched) == ["alpha", "auto"]
            assert await TnItem.objects().count() == 2

            await explicit.merge(name="alpha2")
            explicit.qty = 5
            await explicit.update()
            stored = await TnItem.objects().get("a")
            assert (stored.name, stored.qty) == ("alpha2", 5)

            created = await TnItem.objects().bulk_create([TnItem(id="b1", name="b1"), TnItem(id="b2", name="b2")])
            assert len(created) == 2
            assert await TnItem.objects().count() == 4

            await TnItem(id="b1").delete()
            assert await TnItem.objects().count() == 3

            tables = await _db_tables(client)
            assert "tn_item_tbl" in tables
            assert "TnItem" not in tables

            await TnItem.objects().delete_table()
            assert await TnItem.objects().count() == 0
            assert "TnItem" not in await _db_tables(client)

    @pytest.mark.asyncio
    async def test_relate_targets_table_name(self) -> None:
        async with orm_client(*TN_TABLES) as client:
            holder = await TnHolder(id="h").save()
            item = await TnItem(id="i", name="item").save()

            await holder.relate("tn_holds", item)

            targets = await client.query(
                "SELECT VALUE ->tn_holds->? FROM ONLY $src;",
                {"src": RecordID("tn_holder", "h")},
            )
            assert targets == [RecordID("tn_item_tbl", "i")]

    @pytest.mark.asyncio
    async def test_watch_uses_table_name(self) -> None:
        async with orm_client(*TN_TABLES) as client:
            # SurrealDB 3.x refuses a live query on an undefined table.
            await client.query("DEFINE TABLE tn_item_tbl SCHEMALESS;", {})
            async with asyncio.timeout(5):
                async with TnItem.objects().watch() as stream:
                    assert stream.table == "tn_item_tbl"
                    await TnItem(id="w", name="watched").save()
                    envelope = await stream.__anext__()

            assert envelope["action"] == LiveAction.CREATE
            assert envelope["record"] == RecordID("tn_item_tbl", "w")
            assert envelope["result"]["name"] == "watched"

    @pytest.mark.asyncio
    async def test_legacy_model_reads_and_writes_same_table(self) -> None:
        with pytest.warns(DeprecationWarning, match="_table_name"):

            class TnLegacy(BaseSurrealModel):
                _table_name: ClassVar[str] = "tn_legacy"
                id: str | None = None
                name: str = ""

        async with orm_client(*TN_TABLES) as client:
            await TnLegacy(id="x", name="legacy").save()

            fetched = await TnLegacy.objects().get("x")
            assert isinstance(fetched, TnLegacy)
            assert fetched.name == "legacy"
            assert await TnLegacy.objects().count() == 1

            tables = await _db_tables(client)
            assert "tn_legacy" in tables
            assert "TnLegacy" not in tables
