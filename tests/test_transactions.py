import contextlib
import os
from typing import Any

import pytest

from surreal_orm_lite import (
    BaseSurrealModel,
    SurrealConfigDict,
    SurrealDBConnectionManager,
)
from surreal_orm_lite.exceptions import SurrealDbError
from surreal_orm_lite.signals import post_delete, post_save
from surreal_orm_lite.transaction import BufferedTransaction, InteractiveTransaction, Transaction


def test_transaction_is_exported() -> None:
    import surreal_orm_lite

    assert surreal_orm_lite.Transaction is Transaction
    assert "Transaction" in surreal_orm_lite.__all__


def test_tx_subclasses_exported() -> None:
    import surreal_orm_lite

    assert surreal_orm_lite.BufferedTransaction is BufferedTransaction
    assert surreal_orm_lite.InteractiveTransaction is InteractiveTransaction
    assert surreal_orm_lite.__version__ == "0.9.0"


@pytest.mark.asyncio
async def test_add_namespaces_variables() -> None:
    tx = BufferedTransaction()
    await tx.add("CREATE User:a CONTENT $data;", {"data": {"n": 1}})
    await tx.add("UPDATE User:b MERGE $data;", {"data": {"n": 2}})
    # Each call's vars are renamed with a per-statement prefix to avoid collisions.
    assert tx.variables == {"t0_data": {"n": 1}, "t1_data": {"n": 2}}
    assert tx.statements == [
        "CREATE User:a CONTENT $t0_data;",
        "UPDATE User:b MERGE $t1_data;",
    ]


@pytest.mark.asyncio
async def test_build_query_wraps_in_transaction() -> None:
    tx = BufferedTransaction()
    await tx.add("CREATE User:a CONTENT $data;", {"data": {"n": 1}})
    query = tx.build_query()
    assert query == "BEGIN TRANSACTION;\nCREATE User:a CONTENT $t0_data;\nCOMMIT TRANSACTION;"


def test_build_query_empty_raises() -> None:
    tx = BufferedTransaction()
    with pytest.raises(ValueError, match="empty transaction"):
        tx.build_query()


@pytest.mark.asyncio
async def test_is_empty() -> None:
    tx = BufferedTransaction()
    assert tx.is_empty is True
    await tx.add("DELETE User:a;", None)
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
    # Shape captured from query_raw() on a failed transaction (duplicate create).
    raw = {
        "result": [
            {"result": None, "status": "OK"},
            {
                "details": {"kind": "NotExecuted"},
                "result": "The query was not executed due to a failed transaction",
                "status": "ERR",
            },
            {
                # Real SurrealDB 3.1.3 shape: kind at top level, details.kind == "Record".
                "kind": "AlreadyExists",
                "details": {"kind": "Record", "details": {"id": "probe:c"}},
                "result": "Database record `probe:c` already exists",
                "status": "ERR",
            },
            {
                "details": {"kind": "NotExecuted"},
                "result": "Cannot COMMIT: the transaction was aborted due to a prior error",
                "status": "ERR",
            },
        ]
    }
    with pytest.raises(SurrealDbError, match="already exists"):
        Transaction.raise_for_status(raw)


def test_raise_for_status_rejects_unrecognized_shape() -> None:
    # Defensive: a response missing "result" (or where "result" isn't a list) must NOT
    # be treated as success — that would silently report a failed transaction as
    # committed if a server version ever returned a top-level error envelope.
    with pytest.raises(SurrealDbError, match="unrecognized"):
        Transaction.raise_for_status({"error": {"message": "auth failed"}})
    with pytest.raises(SurrealDbError, match="unrecognized"):
        Transaction.raise_for_status({"result": None})
    with pytest.raises(SurrealDbError, match="unrecognized"):
        Transaction.raise_for_status(None)


@pytest.mark.asyncio
async def test_fire_post_commit_runs_callbacks_in_order() -> None:
    tx = BufferedTransaction()
    seen: list[int] = []

    async def cb(n: int) -> None:
        seen.append(n)

    tx.enqueue_post_commit(lambda: cb(1))
    tx.enqueue_post_commit(lambda: cb(2))
    tx.enqueue_post_commit(lambda: cb(3))
    await tx.fire_post_commit()
    assert seen == [1, 2, 3]


@pytest.mark.asyncio
async def test_fire_post_commit_first_raise_skips_rest() -> None:
    # Documented contract: the first callback that raises propagates the exception;
    # remaining callbacks are NOT invoked. The commit is already durable at this point
    # — we don't try to keep going on partial state.
    tx = BufferedTransaction()
    seen: list[int] = []

    async def cb(n: int) -> None:
        seen.append(n)

    async def boom() -> None:
        raise RuntimeError("handler crashed")

    tx.enqueue_post_commit(lambda: cb(1))
    tx.enqueue_post_commit(boom)
    tx.enqueue_post_commit(lambda: cb(3))  # must NOT run
    with pytest.raises(RuntimeError, match="handler crashed"):
        await tx.fire_post_commit()
    assert seen == [1]


@pytest.mark.asyncio
async def test_add_namespacing_respects_word_boundary() -> None:
    # A var name that is a prefix of another must not be corrupted by the rename.
    tx = BufferedTransaction()
    await tx.add("UPDATE t SET a = $id, b = $identity;", {"id": 1, "identity": 2})
    assert tx.statements == ["UPDATE t SET a = $t0_id, b = $t0_identity;"]
    assert tx.variables == {"t0_id": 1, "t0_identity": 2}


@pytest.mark.asyncio
async def test_buffered_run_read_raises() -> None:
    tx = BufferedTransaction()
    assert tx.is_interactive is False
    with pytest.raises(SurrealDbError, match="3.x"):
        await tx.run_read("SELECT * FROM User;", {})


class _FakeTxnClient:
    """Async client double for InteractiveTransaction (records txn_id usage)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.read_rows: list[Any] = [{"id": "User:1", "name": "x"}]
        self.write_envelope: dict[str, Any] = {"result": [{"status": "OK", "result": [{"id": "User:1"}]}]}
        self.err_envelope: dict[str, Any] | None = None

    async def query_raw(self, sql: str, params: Any = None, session_id: Any = None, txn_id: Any = None) -> dict[str, Any]:
        self.calls.append(("query_raw", txn_id))
        if self.err_envelope is not None and "CREATE" in sql:
            return self.err_envelope
        return self.write_envelope

    async def query(self, sql: str, vars: Any = None, session_id: Any = None, txn_id: Any = None) -> Any:
        self.calls.append(("query", txn_id))
        return self.read_rows

    async def commit(self, txn_id: Any, session_id: Any = None) -> None:
        self.calls.append(("commit", txn_id))

    async def cancel(self, txn_id: Any, session_id: Any = None) -> None:
        self.calls.append(("cancel", txn_id))


@pytest.mark.asyncio
async def test_interactive_is_interactive() -> None:
    tx = InteractiveTransaction(_FakeTxnClient(), "TXN")
    assert tx.is_interactive is True


@pytest.mark.asyncio
async def test_interactive_add_passes_txn_id_and_returns_rows() -> None:
    c = _FakeTxnClient()
    tx = InteractiveTransaction(c, "TXN")
    rows = await tx.add("CREATE User:1 CONTENT $data;", {"data": {"name": "x"}})
    assert rows == [{"id": "User:1"}]
    assert ("query_raw", "TXN") in c.calls


@pytest.mark.asyncio
async def test_interactive_add_raises_on_err() -> None:
    c = _FakeTxnClient()
    c.err_envelope = {"result": [{"status": "ERR", "result": "boom"}]}
    tx = InteractiveTransaction(c, "TXN")
    with pytest.raises(SurrealDbError, match="rolled back"):
        await tx.add("CREATE User:1 CONTENT $data;", {"data": {}})


@pytest.mark.asyncio
async def test_interactive_run_read_passes_txn_id() -> None:
    c = _FakeTxnClient()
    tx = InteractiveTransaction(c, "TXN")
    rows = await tx.run_read("SELECT * FROM User;", {})
    assert rows == [{"id": "User:1", "name": "x"}]
    assert ("query", "TXN") in c.calls


@pytest.mark.asyncio
async def test_interactive_commit_and_cancel_use_txn_id() -> None:
    c = _FakeTxnClient()
    await InteractiveTransaction(c, "TXN").commit()
    assert ("commit", "TXN") in c.calls
    c2 = _FakeTxnClient()
    await InteractiveTransaction(c2, "TXN2").cancel()
    assert ("cancel", "TXN2") in c2.calls


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
    async def test_failed_transaction_raises_and_rolls_back(self) -> None:
        _connect()
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

    @pytest.mark.asyncio
    async def test_update_in_tx_commits(self) -> None:
        _connect()
        client = await SurrealDBConnectionManager.get_client()
        await _clear(client)
        await TxUser(id="dave", name="Dave").save()

        u = TxUser(id="dave", name="David")
        async with SurrealDBConnectionManager.transaction() as tx:
            await u.update(tx=tx)

        rows = await client.query("SELECT name FROM TxUser:dave;", {})
        assert rows[0]["name"] == "David"
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_merge_in_tx_commits(self) -> None:
        _connect()
        client = await SurrealDBConnectionManager.get_client()
        await _clear(client)
        await TxUser(id="erin", name="Erin").save()

        erin = TxUser(id="erin", name="Erin")
        async with SurrealDBConnectionManager.transaction() as tx:
            await erin.merge(tx=tx, name="Erin Updated")

        rows = await client.query("SELECT name FROM TxUser:erin;", {})
        assert rows[0]["name"] == "Erin Updated"
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_delete_in_tx_commits(self) -> None:
        _connect()
        client = await SurrealDBConnectionManager.get_client()
        await _clear(client)
        await TxUser(id="frank", name="Frank").save()

        frank = TxUser(id="frank", name="Frank")
        async with SurrealDBConnectionManager.transaction() as tx:
            await frank.delete(tx=tx)

        rows = await client.query("SELECT * FROM TxUser:frank;", {})
        assert len(rows) == 0
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_mixed_ops_atomic(self) -> None:
        _connect()
        client = await SurrealDBConnectionManager.get_client()
        await _clear(client)
        await TxUser(id="gina", name="Gina").save()

        with pytest.raises(RuntimeError):
            async with SurrealDBConnectionManager.transaction() as tx:
                await TxUser(id="hank", name="Hank").save(tx=tx)
                await TxUser(id="gina", name="Gina").delete(tx=tx)
                raise RuntimeError("rollback everything")

        rows = await client.query("SELECT id FROM TxUser;", {})
        ids = {str(r["id"]) for r in rows}
        assert "TxUser:gina" in ids  # delete rolled back
        assert "TxUser:hank" not in ids  # create rolled back
        await SurrealDBConnectionManager.close_connection()


class TestSaveTxAutoId:
    @pytest.mark.asyncio
    async def test_interactive_save_autoid_assigns_id(self) -> None:
        _connect()
        if not await _native_txn_supported():
            await SurrealDBConnectionManager.close_connection()
            pytest.skip("requires SurrealDB 3.x")
        client = await SurrealDBConnectionManager.get_client()
        await _clear(client)
        u = TxUser(name="AutoWS")  # pas d'id
        async with SurrealDBConnectionManager.transaction() as tx:
            await u.save(tx=tx)
        assert u.id is not None
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_http_save_without_id_raises(self) -> None:
        _connect_http()
        with pytest.raises(SurrealDbError, match="explicit id"):
            async with SurrealDBConnectionManager.transaction() as tx:
                await TxUser(name="NoId").save(tx=tx)
        await SurrealDBConnectionManager.close_connection()


@pytest.mark.asyncio
async def test_refresh_tx_works_in_interactive() -> None:
    _connect()
    if not await _native_txn_supported():
        await SurrealDBConnectionManager.close_connection()
        pytest.skip("requires SurrealDB 3.x")
    client = await SurrealDBConnectionManager.get_client()
    await _clear(client)
    await TxUser(id="ref1", name="Before").save()
    await client.query("UPDATE TxUser:ref1 SET name = 'After';", {})
    u = TxUser(id="ref1", name="Before")
    async with SurrealDBConnectionManager.transaction() as tx:
        await u.refresh(tx=tx)
    assert u.name == "After"
    await SurrealDBConnectionManager.close_connection()


@pytest.mark.asyncio
async def test_refresh_tx_raises_when_buffered() -> None:
    _connect_http()
    u = TxUser(id="x", name="x")
    async with SurrealDBConnectionManager.transaction() as tx:
        with pytest.raises(SurrealDbError, match="3.x"):
            await u.refresh(tx=tx)
    await SurrealDBConnectionManager.close_connection()


@pytest.mark.asyncio
async def test_interactive_read_sees_uncommitted_write() -> None:
    _connect()
    if not await _native_txn_supported():
        await SurrealDBConnectionManager.close_connection()
        pytest.skip("requires SurrealDB 3.x")
    client = await SurrealDBConnectionManager.get_client()
    await _clear(client)
    async with SurrealDBConnectionManager.transaction() as tx:
        await TxUser(id="u1", name="InTx").save(tx=tx)
        found = await TxUser.objects(tx=tx).filter(name="InTx").exec()
        assert len(found) == 1
    await SurrealDBConnectionManager.close_connection()


class TxSignalUser(BaseSurrealModel):
    """Separate model so signal handlers in these tests don't leak into TxUser tests."""

    model_config = SurrealConfigDict(primary_key="id")
    id: str | None = None
    name: str


class TestTransactionSignalsE2E:
    @pytest.mark.asyncio
    async def test_post_save_fires_once_after_successful_commit(self) -> None:
        _connect()
        client = await SurrealDBConnectionManager.get_client()
        with contextlib.suppress(Exception):
            await client.query("DELETE TxSignalUser;", {})

        seen: list[tuple[str, bool]] = []

        @post_save.connect(TxSignalUser)
        async def _on_save(sender, instance, created) -> None:  # type: ignore[no-untyped-def]
            seen.append((str(instance.id), created))

        try:
            async with SurrealDBConnectionManager.transaction() as tx:
                await TxSignalUser(id="sig1", name="A").save(tx=tx)
                await TxSignalUser(id="sig2", name="B").save(tx=tx)
                # No signals must have fired yet — writes are still buffered.
                assert seen == []
            # Commit succeeded → both post_save fire exactly once, in buffer order.
            # In tx mode the instance keeps the raw id string it was constructed with
            # (no post-commit refresh), hence "sig1"/"sig2" instead of "TxSignalUser:sig1".
            assert seen == [("sig1", True), ("sig2", True)]
        finally:
            post_save.disconnect(_on_save, TxSignalUser)
            await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_post_save_does_not_fire_on_rollback_from_body_exception(self) -> None:
        _connect()
        client = await SurrealDBConnectionManager.get_client()
        with contextlib.suppress(Exception):
            await client.query("DELETE TxSignalUser;", {})

        seen: list[str] = []

        @post_save.connect(TxSignalUser)
        async def _on_save(sender, instance, created) -> None:  # type: ignore[no-untyped-def]
            seen.append(str(instance.id))

        try:
            with pytest.raises(RuntimeError):
                async with SurrealDBConnectionManager.transaction() as tx:
                    await TxSignalUser(id="rb1", name="A").save(tx=tx)
                    raise RuntimeError("boom")
            # Body raised before commit → buffer discarded, no post_save fired.
            assert seen == []
        finally:
            post_save.disconnect(_on_save, TxSignalUser)
            await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_post_save_does_not_fire_on_server_rollback(self) -> None:
        _connect()
        client = await SurrealDBConnectionManager.get_client()
        with contextlib.suppress(Exception):
            await client.query("DELETE TxSignalUser;", {})

        seen: list[str] = []

        @post_save.connect(TxSignalUser)
        async def _on_save(sender, instance, created) -> None:  # type: ignore[no-untyped-def]
            seen.append(str(instance.id))

        try:
            # Two creates with the same id inside one tx → server rolls back the batch.
            with pytest.raises(SurrealDbError, match="rolled back"):
                async with SurrealDBConnectionManager.transaction() as tx:
                    await TxSignalUser(id="dupSig", name="A").save(tx=tx)
                    await TxSignalUser(id="dupSig", name="B").save(tx=tx)
            # Commit failed → no post_save fired (key regression: was firing pre-fix).
            assert seen == []
        finally:
            post_save.disconnect(_on_save, TxSignalUser)
            await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_post_delete_does_not_fire_on_rollback(self) -> None:
        _connect()
        client = await SurrealDBConnectionManager.get_client()
        with contextlib.suppress(Exception):
            await client.query("DELETE TxSignalUser;", {})
        await TxSignalUser(id="killme", name="X").save()

        seen: list[str] = []

        @post_delete.connect(TxSignalUser)
        async def _on_delete(sender, instance) -> None:  # type: ignore[no-untyped-def]
            seen.append(str(instance.id))

        try:
            with pytest.raises(RuntimeError):
                async with SurrealDBConnectionManager.transaction() as tx:
                    await TxSignalUser(id="killme", name="X").delete(tx=tx)
                    raise RuntimeError("rollback")
            assert seen == []
        finally:
            post_delete.disconnect(_on_delete, TxSignalUser)
            await SurrealDBConnectionManager.close_connection()


class TestMergeTxSync:
    @pytest.mark.asyncio
    async def test_merge_in_tx_applies_data_to_instance_at_buffer(self) -> None:
        # The non-tx path calls refresh() to resync; the tx path can't (no reads inside
        # a tx + buffered write), so it applies the merged fields to self at buffer time.
        _connect()
        client = await SurrealDBConnectionManager.get_client()
        await _clear(client)
        await TxUser(id="sync", name="Old").save()

        u = TxUser(id="sync", name="Old")
        async with SurrealDBConnectionManager.transaction() as tx:
            await u.merge(tx=tx, name="NewName")
            # Instance reflects the buffered merge immediately.
            assert u.name == "NewName"
        # After commit, instance and DB agree.
        assert u.name == "NewName"
        rows = await client.query("SELECT name FROM TxUser:sync;", {})
        assert rows[0]["name"] == "NewName"
        await SurrealDBConnectionManager.close_connection()


def _connect_http() -> None:
    SurrealDBConnectionManager.set_connection(
        url=f"http://{os.environ.get('SURREALDB_HOST', 'localhost')}:{os.environ.get('SURREALDB_PORT', '8000')}",
        user="root",
        password="root",
        namespace="ns",
        database="db",
    )


class TestTransactionDispatchE2E:
    @pytest.mark.asyncio
    async def test_ws_interactive_when_supported(self) -> None:
        _connect()  # ws://
        if not await _native_txn_supported():
            await SurrealDBConnectionManager.close_connection()
            pytest.skip("requires SurrealDB 3.x")
        async with SurrealDBConnectionManager.transaction() as tx:
            assert tx.is_interactive is True
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_http_uses_buffered_and_commits(self) -> None:
        _connect_http()
        client = await SurrealDBConnectionManager.get_client()
        await _clear(client)
        async with SurrealDBConnectionManager.transaction() as tx:
            assert tx.is_interactive is False
            await TxUser(id="http1", name="H").save(tx=tx)
        rows = await client.query("SELECT * FROM TxUser;", {})
        assert len(rows) == 1
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_interactive_commit_and_rollback(self) -> None:
        _connect()
        if not await _native_txn_supported():
            await SurrealDBConnectionManager.close_connection()
            pytest.skip("requires SurrealDB 3.x")
        client = await SurrealDBConnectionManager.get_client()
        await _clear(client)
        async with SurrealDBConnectionManager.transaction() as tx:
            await TxUser(id="w1", name="W").save(tx=tx)
        assert len(await client.query("SELECT * FROM TxUser;", {})) == 1
        await _clear(client)
        with pytest.raises(RuntimeError):
            async with SurrealDBConnectionManager.transaction() as tx:
                await TxUser(id="w2", name="W2").save(tx=tx)
                raise RuntimeError("boom")
        assert len(await client.query("SELECT * FROM TxUser;", {})) == 0
        await SurrealDBConnectionManager.close_connection()


async def _native_txn_supported() -> bool:
    """True if the server exposes the SDK's native transaction RPC (SurrealDB 3.x)."""
    import contextlib

    client = await SurrealDBConnectionManager.get_client()
    try:
        txn = await client.begin()
    except Exception:
        return False
    with contextlib.suppress(Exception):
        await client.cancel(txn)
    return True


class TestQuerySetTxE2E:
    @pytest.mark.asyncio
    async def test_interactive_bulk_update_in_tx_commits(self) -> None:
        _connect()
        if not await _native_txn_supported():
            await SurrealDBConnectionManager.close_connection()
            pytest.skip("requires SurrealDB 3.x")
        client = await SurrealDBConnectionManager.get_client()
        await _clear(client)
        await TxUser(id="a", name="guest").save()
        await TxUser(id="b", name="guest").save()
        async with SurrealDBConnectionManager.transaction() as tx:
            n = await TxUser.objects(tx=tx).filter(name="guest").bulk_update(name="member")
            assert n == 2
        rows = await client.query("SELECT name FROM TxUser;", {})
        assert all(r["name"] == "member" for r in rows)
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_interactive_bulk_delete_rolls_back(self) -> None:
        _connect()
        if not await _native_txn_supported():
            await SurrealDBConnectionManager.close_connection()
            pytest.skip("requires SurrealDB 3.x")
        client = await SurrealDBConnectionManager.get_client()
        await _clear(client)
        await TxUser(id="d1", name="x").save()
        with pytest.raises(RuntimeError):
            async with SurrealDBConnectionManager.transaction() as tx:
                await TxUser.objects(tx=tx).filter(name="x").bulk_delete()
                raise RuntimeError("rollback")
        assert len(await client.query("SELECT * FROM TxUser;", {})) == 1
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_http_bulk_update_in_tx_commits(self) -> None:
        _connect_http()
        client = await SurrealDBConnectionManager.get_client()
        await _clear(client)
        await TxUser(id="h1", name="guest").save()
        async with SurrealDBConnectionManager.transaction() as tx:
            await TxUser.objects(tx=tx).filter(name="guest").bulk_update(name="member")
        rows = await client.query("SELECT name FROM TxUser;", {})
        assert rows[0]["name"] == "member"
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_http_read_in_tx_raises(self) -> None:
        _connect_http()
        client = await SurrealDBConnectionManager.get_client()
        await _clear(client)
        async with SurrealDBConnectionManager.transaction() as tx:
            with pytest.raises(SurrealDbError, match="3.x"):
                await TxUser.objects(tx=tx).filter(name="x").exec()
        await SurrealDBConnectionManager.close_connection()


class TestNativeTxSpikeE2E:
    """Valide l'API de transaction NATIVE du SDK officiel sur le serveur courant.

    Sur SurrealDB 3.x : begin()/txn_id/commit/cancel fonctionnent, les lectures avec
    txn_id voient les écritures non-committées, et l'isolation est réelle. Sur 2.6.x,
    begin() lève NotFoundError → le test est skippé (le fallback buffering est testé
    ailleurs)."""

    @pytest.mark.asyncio
    async def test_native_interactive_transaction(self) -> None:
        _connect()  # ws://
        client = await SurrealDBConnectionManager.get_client()
        if not await _native_txn_supported():
            await SurrealDBConnectionManager.close_connection()
            pytest.skip("native interactive transactions require SurrealDB 3.x")

        with contextlib.suppress(Exception):
            await client.query("DELETE TxSpike;", {})

        txn = await client.begin()
        await client.query("CREATE TxSpike:s1 CONTENT { name: 'x' };", {}, txn_id=txn)
        inside = await client.query("SELECT * FROM TxSpike;", {}, txn_id=txn)
        outside = await client.query("SELECT * FROM TxSpike;", {})
        assert len(inside) == 1   # voit l'écriture non-committée
        assert len(outside) == 0  # isolation : invisible hors txn
        await client.cancel(txn)
        after = await client.query("SELECT * FROM TxSpike;", {})
        assert len(after) == 0    # rollback effectif

        txn2 = await client.begin()
        await client.query("CREATE TxSpike:s2 CONTENT { name: 'y' };", {}, txn_id=txn2)
        await client.commit(txn2)
        final = await client.query("SELECT * FROM TxSpike;", {})
        assert len(final) == 1    # commit persiste
        await client.query("DELETE TxSpike;", {})
        await SurrealDBConnectionManager.close_connection()
