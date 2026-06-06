# tests/test_transactions.py
import pytest

from surreal_orm_lite.transaction import Transaction


def test_add_namespaces_variables() -> None:
    tx = Transaction()
    tx.add("CREATE User:a CONTENT $data;", {"data": {"n": 1}})
    tx.add("UPDATE User:b MERGE $data;", {"data": {"n": 2}})
    # Each call's vars are renamed with a per-statement prefix to avoid collisions.
    assert tx.variables == {"t0_data": {"n": 1}, "t1_data": {"n": 2}}
    assert tx.statements == [
        "CREATE User:a CONTENT $t0_data;",
        "UPDATE User:b MERGE $t1_data;",
    ]


def test_build_query_wraps_in_transaction() -> None:
    tx = Transaction()
    tx.add("CREATE User:a CONTENT $data;", {"data": {"n": 1}})
    query = tx.build_query()
    assert query == "BEGIN TRANSACTION;\nCREATE User:a CONTENT $t0_data;\nCOMMIT TRANSACTION;"


def test_build_query_empty_raises() -> None:
    tx = Transaction()
    with pytest.raises(ValueError, match="empty transaction"):
        tx.build_query()


def test_is_empty() -> None:
    tx = Transaction()
    assert tx.is_empty is True
    tx.add("DELETE User:a;", None)
    assert tx.is_empty is False


def test_raise_for_status_ok_does_nothing() -> None:
    # Shape captured from query_raw() on a successful transaction.
    raw = {
        "result": [
            {"result": None, "status": "OK"},
            {"result": [{"id": "TxUser:ok"}], "status": "OK"},
            {"result": None, "status": "OK"},
        ]
    }
    Transaction.raise_for_status(raw)  # must not raise


def test_raise_for_status_raises_root_cause() -> None:
    from surreal_orm_lite.exceptions import SurrealDbError

    # Shape captured from query_raw() on a failed transaction (duplicate create).
    raw = {
        "result": [
            {"result": None, "status": "OK"},
            {
                "details": {"kind": "NotExecuted"},
                "result": "The query was not executed due to a failed transaction",
                "status": "ERR",
            },
            {"details": {"kind": "AlreadyExists"}, "result": "Database record `probe:c` already exists", "status": "ERR"},
            {
                "details": {"kind": "NotExecuted"},
                "result": "Cannot COMMIT: the transaction was aborted due to a prior error",
                "status": "ERR",
            },
        ]
    }
    with pytest.raises(SurrealDbError, match="already exists"):
        Transaction.raise_for_status(raw)


# Append to tests/test_transactions.py
import contextlib
import os

from surreal_orm_lite import BaseSurrealModel, SurrealDBConnectionManager, SurrealConfigDict


def _connect() -> None:
    SurrealDBConnectionManager.set_connection(
        url=f"ws://{os.environ.get('SURREALDB_HOST', 'localhost')}:{os.environ.get('SURREALDB_PORT', '8000')}/rpc",
        user="root",
        password="root",
        namespace="ns",
        database="db",
    )


async def _clear(client) -> None:
    """Delete all TxUser rows, tolerating a never-created table.

    On SurrealDB 3.x, DELETE on a table that does not exist yet raises NotFoundError;
    suppress it so test setup is idempotent across server versions.
    """
    with contextlib.suppress(Exception):
        await client.query("DELETE TxUser;", {})


class TxUser(BaseSurrealModel):
    model_config = SurrealConfigDict(primary_key="id")
    id: str | None = None
    name: str


class TestTransactionE2E:
    @pytest.mark.asyncio
    async def test_empty_transaction_is_noop(self) -> None:
        _connect()
        async with SurrealDBConnectionManager.transaction():
            pass  # nothing buffered → no query sent, no error
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_commit_persists_saves(self) -> None:
        _connect()
        client = await SurrealDBConnectionManager.get_client()
        await _clear(client)
        async with SurrealDBConnectionManager.transaction() as tx:
            await TxUser(id="alice", name="Alice").save(tx=tx)
            await TxUser(id="bob", name="Bob").save(tx=tx)
        rows = await client.query("SELECT * FROM TxUser;", {})
        assert len(rows) == 2
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_exception_rolls_back_saves(self) -> None:
        _connect()
        client = await SurrealDBConnectionManager.get_client()
        await _clear(client)
        with pytest.raises(RuntimeError):
            async with SurrealDBConnectionManager.transaction() as tx:
                await TxUser(id="carol", name="Carol").save(tx=tx)
                raise RuntimeError("boom")
        rows = await client.query("SELECT * FROM TxUser;", {})
        assert len(rows) == 0  # nothing persisted
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_save_in_tx_without_id_raises(self) -> None:
        _connect()
        from surreal_orm_lite.exceptions import SurrealDbError

        with pytest.raises(SurrealDbError, match="explicit id"):
            async with SurrealDBConnectionManager.transaction() as tx:
                await TxUser(name="NoId").save(tx=tx)
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_failed_transaction_raises_and_rolls_back(self) -> None:
        _connect()
        from surreal_orm_lite.exceptions import SurrealDbError

        client = await SurrealDBConnectionManager.get_client()
        await _clear(client)
        # Two creates with the same id inside one tx → second fails → whole tx rolls back.
        with pytest.raises(SurrealDbError, match="rolled back"):
            async with SurrealDBConnectionManager.transaction() as tx:
                await TxUser(id="dup", name="A").save(tx=tx)
                await TxUser(id="dup", name="B").save(tx=tx)
        rows = await client.query("SELECT * FROM TxUser WHERE id = TxUser:dup;", {})
        assert not rows  # nothing persisted
        await SurrealDBConnectionManager.close_connection()
