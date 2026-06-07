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
