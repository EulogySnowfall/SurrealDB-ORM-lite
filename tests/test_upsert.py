import os

import pytest

from surreal_orm_lite import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from surreal_orm_lite.exceptions import SurrealDbError


def _connect() -> None:
    SurrealDBConnectionManager.set_connection(
        url=f"ws://{os.environ.get('SURREALDB_HOST', 'localhost')}:{os.environ.get('SURREALDB_PORT', '8000')}/rpc",
        user="root",
        password="root",
        namespace="ns",
        database="db",
    )


async def _clear(client, table: str) -> None:
    import contextlib

    with contextlib.suppress(Exception):
        await client.query(f"DELETE {table};", {})


class UpUser(BaseSurrealModel):
    model_config = SurrealConfigDict(primary_key="id")
    id: str | None = None
    name: str
    age: int = 0


class TestUpsertE2E:
    @pytest.mark.asyncio
    async def test_upsert_creates_then_replaces(self) -> None:
        _connect()
        client = await SurrealDBConnectionManager.get_client()
        await _clear(client, "UpUser")
        # create
        u = UpUser(id="alice", name="Alice", age=30)
        await u.upsert()
        rows = await client.query("SELECT * FROM UpUser;", {})
        assert len(rows) == 1
        assert rows[0]["name"] == "Alice"
        assert rows[0]["age"] == 30
        # replace (age omitted from the new model defaults to 0 → REPLACE drops the old 30)
        u2 = UpUser(id="alice", name="Alice2")
        await u2.upsert()
        rows = await client.query("SELECT * FROM UpUser;", {})
        assert len(rows) == 1  # still one record (no duplicate)
        assert rows[0]["name"] == "Alice2"
        assert rows[0]["age"] == 0  # REPLACE semantics
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_upsert_without_id_raises(self) -> None:
        _connect()
        with pytest.raises(SurrealDbError, match="explicit id"):
            await UpUser(name="NoId").upsert()
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_upsert_in_tx_commits(self) -> None:
        _connect()
        client = await SurrealDBConnectionManager.get_client()
        await _clear(client, "UpUser")
        async with SurrealDBConnectionManager.transaction() as tx:
            await UpUser(id="txu", name="Tx").upsert(tx=tx)
        rows = await client.query("SELECT * FROM UpUser WHERE id = UpUser:txu;", {})
        assert len(rows) == 1
        assert rows[0]["name"] == "Tx"
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_upsert_in_tx_rolls_back(self) -> None:
        _connect()
        client = await SurrealDBConnectionManager.get_client()
        await _clear(client, "UpUser")
        with pytest.raises(RuntimeError):
            async with SurrealDBConnectionManager.transaction() as tx:
                await UpUser(id="txr", name="TxR").upsert(tx=tx)
                raise RuntimeError("boom")
        rows = await client.query("SELECT * FROM UpUser WHERE id = UpUser:txr;", {})
        assert not rows
        await SurrealDBConnectionManager.close_connection()


class TestUpdateOrCreateE2E:
    @pytest.mark.asyncio
    async def test_creates_when_absent(self) -> None:
        _connect()
        client = await SurrealDBConnectionManager.get_client()
        await _clear(client, "UpUser")
        obj, created = await UpUser.objects().update_or_create(name="Alice", defaults={"age": 30})
        assert created is True
        assert obj.name == "Alice"
        assert obj.age == 30
        rows = await client.query("SELECT * FROM UpUser;", {})
        assert len(rows) == 1
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_updates_when_present(self) -> None:
        _connect()
        client = await SurrealDBConnectionManager.get_client()
        await _clear(client, "UpUser")
        first, c1 = await UpUser.objects().update_or_create(name="Bob", defaults={"age": 1})
        assert c1 is True
        second, c2 = await UpUser.objects().update_or_create(name="Bob", defaults={"age": 2})
        assert c2 is False
        assert second.age == 2
        rows = await client.query("SELECT * FROM UpUser;", {})
        assert len(rows) == 1  # no duplicate
        assert rows[0]["age"] == 2
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_raises_on_multiple_matches(self) -> None:
        _connect()
        client = await SurrealDBConnectionManager.get_client()
        await _clear(client, "UpUser")
        await UpUser(id="m1", name="Dup", age=1).save()
        await UpUser(id="m2", name="Dup", age=2).save()
        with pytest.raises(SurrealDbError, match="multiple"):
            await UpUser.objects().update_or_create(name="Dup", defaults={"age": 9})
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_requires_criteria(self) -> None:
        _connect()
        with pytest.raises(SurrealDbError, match="criteria"):
            await UpUser.objects().update_or_create(defaults={"age": 1})
        await SurrealDBConnectionManager.close_connection()


class TestGetOrCreateE2E:
    @pytest.mark.asyncio
    async def test_creates_when_absent(self) -> None:
        _connect()
        client = await SurrealDBConnectionManager.get_client()
        await _clear(client, "UpUser")
        obj, created = await UpUser.objects().get_or_create(name="Zoe", defaults={"age": 5})
        assert created is True
        assert obj.name == "Zoe"
        assert obj.age == 5
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_returns_existing_without_writing(self) -> None:
        _connect()
        client = await SurrealDBConnectionManager.get_client()
        await _clear(client, "UpUser")
        first, c1 = await UpUser.objects().get_or_create(name="Yan", defaults={"age": 7})
        assert c1 is True
        # defaults differ, but the existing record must NOT be overwritten
        second, c2 = await UpUser.objects().get_or_create(name="Yan", defaults={"age": 99})
        assert c2 is False
        assert second.age == 7  # unchanged
        rows = await client.query("SELECT * FROM UpUser;", {})
        assert len(rows) == 1
        assert rows[0]["age"] == 7
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_raises_on_multiple_matches(self) -> None:
        _connect()
        client = await SurrealDBConnectionManager.get_client()
        await _clear(client, "UpUser")
        await UpUser(id="g1", name="DupG", age=1).save()
        await UpUser(id="g2", name="DupG", age=2).save()
        with pytest.raises(SurrealDbError, match="multiple"):
            await UpUser.objects().get_or_create(name="DupG")
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_requires_criteria(self) -> None:
        _connect()
        with pytest.raises(SurrealDbError, match="criteria"):
            await UpUser.objects().get_or_create(defaults={"age": 1})
        await SurrealDBConnectionManager.close_connection()
