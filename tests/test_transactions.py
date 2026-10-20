import asyncio
import contextlib
import os
import traceback
from typing import Any

import pytest
from surrealdb import RecordID

from surreal_orm_lite import (
    BaseSurrealModel,
    SurrealConfigDict,
    SurrealDBConnectionManager,
)
from surreal_orm_lite.concurrency import is_conflict_error
from surreal_orm_lite.exceptions import SurrealDbConflictError, SurrealDbError
from surreal_orm_lite.signals import post_delete, post_save
from surreal_orm_lite.transaction import (
    _CANCELLED_AFTER_FAILURE,
    BufferedTransaction,
    InteractiveTransaction,
    Transaction,
)
from tests._readme_tables import assert_server_header_sentence


def test_transaction_is_exported() -> None:
    import surreal_orm_lite

    assert surreal_orm_lite.Transaction is Transaction
    assert "Transaction" in surreal_orm_lite.__all__


def test_tx_subclasses_exported() -> None:
    import surreal_orm_lite

    assert surreal_orm_lite.BufferedTransaction is BufferedTransaction
    assert surreal_orm_lite.InteractiveTransaction is InteractiveTransaction


@pytest.mark.asyncio
async def test_add_namespaces_variables() -> None:
    tx = BufferedTransaction()
    a, b = RecordID("User", "a"), RecordID("User", "b")
    await tx.add("CREATE $rid CONTENT $data;", {"rid": a, "data": {"n": 1}})
    await tx.add("UPDATE $rid MERGE $data;", {"rid": b, "data": {"n": 2}})
    # Each call's vars are renamed with a per-statement prefix to avoid collisions.
    assert tx.variables == {"t0_rid": a, "t0_data": {"n": 1}, "t1_rid": b, "t1_data": {"n": 2}}
    assert tx.statements == [
        "CREATE $t0_rid CONTENT $t0_data;",
        "UPDATE $t1_rid MERGE $t1_data;",
    ]


@pytest.mark.asyncio
async def test_build_query_wraps_in_transaction() -> None:
    tx = BufferedTransaction()
    await tx.add("CREATE $rid CONTENT $data;", {"rid": RecordID("User", "a"), "data": {"n": 1}})
    query = tx.build_query()
    assert query == "BEGIN TRANSACTION;\nCREATE $t0_rid CONTENT $t0_data;\nCOMMIT TRANSACTION;"


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
    with pytest.raises(SurrealDbError, match="the server refused the query: auth failed"):
        Transaction.raise_for_status({"error": {"message": "auth failed"}})
    with pytest.raises(SurrealDbError, match="unrecognized"):
        Transaction.raise_for_status({"result": None})
    with pytest.raises(SurrealDbError, match="unrecognized"):
        Transaction.raise_for_status(None)


def test_raise_for_status_types_conflict() -> None:
    # Real 3.x buffered conflict shape: ALL ERR statements are NotExecuted, the
    # real cause ("Cannot COMMIT: Transaction conflict ... can be retried") is last.
    raw = {
        "result": [
            {"result": None, "status": "OK"},
            {
                "details": {"kind": "NotExecuted"},
                "result": "The query was not executed due to a failed transaction",
                "status": "ERR",
            },
            {
                "details": {"kind": "NotExecuted"},
                "result": "The query was not executed due to a failed transaction",
                "status": "ERR",
            },
            {
                "details": {"kind": "NotExecuted"},
                "result": "Cannot COMMIT: Transaction conflict: Write conflict, retry the transaction. This transaction can be retried",
                "status": "ERR",
            },
        ]
    }
    with pytest.raises(SurrealDbConflictError, match="can be retried"):
        Transaction.raise_for_status(raw)


def test_raise_for_status_bare_filler_without_details_surfaces_cause() -> None:
    # Defensive: if a server ever drops `details` on filler statements, the selector
    # must still skip the bare filler and surface the substantive cause.
    raw = {
        "result": [
            {"result": "The query was not executed due to a failed transaction", "status": "ERR"},
            {"result": "Cannot COMMIT: a concrete server error", "status": "ERR"},
        ]
    }
    with pytest.raises(SurrealDbError, match="a concrete server error"):
        Transaction.raise_for_status(raw)


def test_raise_for_status_types_single_err_conflict_26() -> None:
    # Real 2.6.5 shape: a single ERR statement (no `details`) whose message prefixes the
    # filler then appends the retryable cause. Must be typed as a conflict.
    raw = {
        "result": [
            {
                "result": "The query was not executed due to a failed transaction. Failed to commit transaction due to a read or write conflict. This transaction can be retried",
                "status": "ERR",
            }
        ]
    }
    with pytest.raises(SurrealDbConflictError, match="can be retried"):
        Transaction.raise_for_status(raw)


def test_raise_for_status_surfaces_cause_not_filler() -> None:
    # Even for a non-conflict failure where the real cause is a later NotExecuted
    # statement, surface the cause, not the generic filler.
    raw = {
        "result": [
            {
                "details": {"kind": "NotExecuted"},
                "result": "The query was not executed due to a failed transaction",
                "status": "ERR",
            },
            {
                "details": {"kind": "NotExecuted"},
                "result": "Cannot COMMIT: some other server error",
                "status": "ERR",
            },
        ]
    }
    with pytest.raises(SurrealDbError, match="some other server error") as ei:
        Transaction.raise_for_status(raw)
    assert not isinstance(ei.value, SurrealDbConflictError)  # not a conflict


@pytest.mark.asyncio
async def test_interactive_commit_wraps_conflict() -> None:
    class _ConflictOnCommit:
        async def commit(self, txn_id: Any) -> None:
            raise RuntimeError("Transaction conflict: Write conflict, retry the transaction. This transaction can be retried")

        async def cancel(self, txn_id: Any) -> None:
            return None

    with pytest.raises(SurrealDbConflictError):
        await InteractiveTransaction(_ConflictOnCommit(), "TXN").commit()


@pytest.mark.asyncio
async def test_interactive_commit_passes_non_conflict_through() -> None:
    class _BoomOnCommit:
        async def commit(self, txn_id: Any) -> None:
            raise RuntimeError("disk on fire")

        async def cancel(self, txn_id: Any) -> None:
            return None

    with pytest.raises(RuntimeError, match="disk on fire"):
        await InteractiveTransaction(_BoomOnCommit(), "TXN").commit()


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
async def test_add_renames_in_one_pass() -> None:
    # Each $name is renamed once: a variable named like a renamed one is not renamed twice, and the parameters
    # that are not variables ($this, $value) are left alone.
    tx = BufferedTransaction()
    await tx.add("RETURN [$a, $t0_a, $this, $value];", {"a": 1, "t0_a": 2})
    assert tx.statements == ["RETURN [$t0_a, $t0_t0_a, $this, $value];"]
    assert tx.variables == {"t0_a": 1, "t0_t0_a": 2}


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
        from surreal_orm_lite._sdk import NotFoundError

        _connect()  # ws://
        client = await SurrealDBConnectionManager.get_client()
        if not await _native_txn_supported():
            await SurrealDBConnectionManager.close_connection()
            pytest.skip("native interactive transactions require SurrealDB 3.x")

        with contextlib.suppress(Exception):
            await client.query("DELETE TxSpike;", {})

        async def _select_outside() -> list:
            # SurrealDB 3.x raises NotFoundError for a SELECT on a never-materialized
            # table; the isolation contract treats that as "0 rows visible".
            try:
                return await client.query("SELECT * FROM TxSpike;", {})
            except NotFoundError:
                return []

        txn = await client.begin()
        await client.query("CREATE TxSpike:s1 CONTENT { name: 'x' };", {}, txn_id=txn)
        inside = await client.query("SELECT * FROM TxSpike;", {}, txn_id=txn)
        outside = await _select_outside()
        assert len(inside) == 1  # voit l'écriture non-committée
        assert len(outside) == 0  # isolation : invisible hors txn
        await client.cancel(txn)
        after = await _select_outside()
        assert len(after) == 0  # rollback effectif

        txn2 = await client.begin()
        await client.query("CREATE TxSpike:s2 CONTENT { name: 'y' };", {}, txn_id=txn2)
        await client.commit(txn2)
        final = await client.query("SELECT * FROM TxSpike;", {})
        assert len(final) == 1  # commit persiste
        await client.query("DELETE TxSpike;", {})
        await SurrealDBConnectionManager.close_connection()


# =============================================================================
# Unit tests with fakes — exercise the tx-routed paths in QuerySet / BaseModel
# without requiring a SurrealDB server. These keep patch coverage above 90% on
# CI matrix cells that don't expose those branches naturally.
# =============================================================================


class _FakeTx:
    """Minimal Transaction double honoring the ABC's call-site interface.

    Stores every add()/run_read() call, returns canned values. ``is_interactive``
    is configurable to exercise both strategy branches in model_base / query_set.
    """

    def __init__(self, interactive: bool = True, read_rows: Any = None, add_rows: Any = None) -> None:
        self.interactive = interactive
        self.read_rows = read_rows
        self.add_rows = add_rows
        self.read_calls: list[tuple[str, dict[str, Any]]] = []
        self.add_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.post_commit: list[Any] = []
        self.absorbed: list[BaseException] = []

    @property
    def is_interactive(self) -> bool:
        return self.interactive

    async def run_read(self, sql: str, variables: dict[str, Any] | None = None) -> Any:
        self.read_calls.append((sql, variables or {}))
        if isinstance(self.read_rows, Exception):
            raise self.read_rows
        return self.read_rows

    async def add(self, sql: str, variables: dict[str, Any] | None = None) -> Any:
        self.add_calls.append((sql, variables))
        return self.add_rows

    def enqueue_post_commit(self, cb: Any) -> None:
        self.post_commit.append(cb)

    def _absorb_failure(self, error: BaseException) -> None:
        self.absorbed.append(error)


class TestTxRoutedQuerySetUnits:
    @pytest.mark.asyncio
    async def test_execute_query_tx_run_read_notfound_returns_empty(self) -> None:
        # query_set.py: _execute_query NotFoundError branch on the tx path.
        from surreal_orm_lite._sdk import NotFoundError

        error = NotFoundError("NotFound", "nope")
        tx = _FakeTx(read_rows=error)
        rows = await TxUser.objects(tx=tx)._execute_query("SELECT * FROM TxUser;", {})
        assert rows == []
        assert tx.absorbed == [error]  # read as no rows: not a failed statement (issue #268)

    @pytest.mark.asyncio
    async def test_execute_query_tx_relayed_error_is_not_absorbed(self) -> None:
        from surreal_orm_lite._sdk import ServerError

        tx = _FakeTx(read_rows=ServerError("Internal", "boom"))
        with pytest.raises(ServerError, match="boom"):
            await TxUser.objects(tx=tx)._execute_query("SELECT * FROM TxUser;", {})
        assert tx.absorbed == []  # relayed: the transaction's failure stands (issue #268)

    @pytest.mark.asyncio
    async def test_get_tx_returns_record(self) -> None:
        tx = _FakeTx(read_rows=[{"id": "TxUser:a", "name": "A"}])
        u = await TxUser.objects(tx=tx).get("a")
        assert u.name == "A"
        # The SELECT used the parametrized $rid form, not interpolation.
        assert tx.read_calls[0][0] == "SELECT * FROM $rid;"

    @pytest.mark.asyncio
    async def test_get_tx_empty_rows_raises(self) -> None:
        from surreal_orm_lite.exceptions import SurrealDbNotFoundError

        tx = _FakeTx(read_rows=[])
        with pytest.raises(SurrealDbNotFoundError):
            await TxUser.objects(tx=tx).get("missing")

    @pytest.mark.asyncio
    async def test_get_tx_run_read_notfound_normalized(self) -> None:
        # Review Fix #1: NotFoundError from the SDK is normalized to the ORM type.
        from surreal_orm_lite._sdk import NotFoundError
        from surreal_orm_lite.exceptions import SurrealDbNotFoundError

        error = NotFoundError("NotFound", "missing table")
        tx = _FakeTx(read_rows=error)
        with pytest.raises(SurrealDbNotFoundError):
            await TxUser.objects(tx=tx).get("a")
        assert tx.absorbed == [error]

    @pytest.mark.asyncio
    async def test_all_tx_returns_rows(self) -> None:
        tx = _FakeTx(read_rows=[{"id": "TxUser:a", "name": "A"}, {"id": "TxUser:b", "name": "B"}])
        rows = await TxUser.objects(tx=tx).all()
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_all_tx_notfound_returns_empty_list(self) -> None:
        from surreal_orm_lite._sdk import NotFoundError

        error = NotFoundError("NotFound", "table missing")
        tx = _FakeTx(read_rows=error)
        rows = await TxUser.objects(tx=tx).all()
        assert rows == []
        assert tx.absorbed == [error]

    @pytest.mark.asyncio
    async def test_bulk_create_tx_buffered_returns_inputs(self) -> None:
        # Buffered: add() returns None, bulk_create returns the input models unchanged.
        tx = _FakeTx(interactive=False, add_rows=None)
        models = [TxUser(id="x", name="X"), TxUser(id="y", name="Y")]
        out = await TxUser.objects(tx=tx).bulk_create(models)
        assert out is models
        assert tx.add_calls[0][0].startswith("INSERT INTO TxUser")

    @pytest.mark.asyncio
    async def test_bulk_create_tx_interactive_returns_db_rows(self) -> None:
        # Interactive: add() returns rows, bulk_create rehydrates models from them.
        tx = _FakeTx(interactive=True, add_rows=[{"id": "TxUser:a", "name": "A"}])
        out = await TxUser.objects(tx=tx).bulk_create([TxUser(id="a", name="A")])
        assert len(out) == 1
        assert out[0].name == "A"

    @pytest.mark.asyncio
    async def test_bulk_update_tx_buffered_returns_zero(self) -> None:
        # Buffered add() returns None → bulk_update reports 0 (not knowable until commit).
        tx = _FakeTx(interactive=False, add_rows=None)
        n = await TxUser.objects(tx=tx).filter(name="x").bulk_update(name="y")
        assert n == 0

    @pytest.mark.asyncio
    async def test_bulk_delete_tx_buffered_returns_zero(self) -> None:
        tx = _FakeTx(interactive=False, add_rows=None)
        n = await TxUser.objects(tx=tx).filter(name="x").bulk_delete()
        assert n == 0


class TestTxRoutedModelUnits:
    @pytest.mark.asyncio
    async def test_refresh_tx_interactive_no_id_raises(self) -> None:
        u = TxUser(name="NoId")  # no id
        tx = _FakeTx(interactive=True, read_rows=[])
        with pytest.raises(SurrealDbError, match="not recorded yet"):
            await u.refresh(tx=tx)

    @pytest.mark.asyncio
    async def test_refresh_tx_interactive_no_dict_raises(self) -> None:
        # run_read returns no dict-shaped row → "no record found".
        u = TxUser(id="ghost", name="x")
        tx = _FakeTx(interactive=True, read_rows=[])
        with pytest.raises(SurrealDbError, match="no record found"):
            await u.refresh(tx=tx)

    @pytest.mark.asyncio
    async def test_save_tx_interactive_autoid_applies_returned_fields(self) -> None:
        # save(tx=) auto-id on an interactive tx: rows returned by add() are merged into self.
        tx = _FakeTx(interactive=True, add_rows=[{"id": "TxUser:auto", "name": "Auto"}])
        u = TxUser(name="Auto")  # no id → triggers auto-id branch
        await u.save(tx=tx)
        assert u.id == "TxUser:auto"

    @pytest.mark.asyncio
    async def test_update_tx_with_signals_enqueues_post_commit(self) -> None:
        # update() tx + signals branch: pre_update fires inline, post_update is enqueued.
        from surreal_orm_lite.signals import post_update, pre_update

        seen_pre: list[Any] = []
        seen_post: list[Any] = []

        async def on_pre(sender: Any, instance: Any, update_fields: Any) -> None:
            seen_pre.append(update_fields)

        async def on_post(sender: Any, instance: Any, update_fields: Any) -> None:
            seen_post.append(update_fields)

        pre_update.connect(TxUser)(on_pre)
        post_update.connect(TxUser)(on_post)
        try:
            tx = _FakeTx(interactive=False, add_rows=None)
            u = TxUser(id="u1", name="N")
            await u.update(tx=tx)
            assert seen_pre and not seen_post  # pre fired, post deferred
            for cb in tx.post_commit:
                await cb()
            assert seen_post  # post fired after we drained the queue
        finally:
            pre_update.disconnect(on_pre, TxUser)
            post_update.disconnect(on_post, TxUser)

    @pytest.mark.asyncio
    async def test_merge_tx_buffered_with_signals_applies_to_instance(self) -> None:
        # merge(tx=) buffered + signals: pre_update fires, fields applied in-memory, post_update deferred.
        from surreal_orm_lite.signals import post_update, pre_update

        fired_pre: list[Any] = []
        fired_post: list[Any] = []

        async def on_pre(sender: Any, instance: Any, update_fields: Any) -> None:
            fired_pre.append(update_fields)

        async def on_post(sender: Any, instance: Any, update_fields: Any) -> None:
            fired_post.append(update_fields)

        pre_update.connect(TxUser)(on_pre)
        post_update.connect(TxUser)(on_post)
        try:
            tx = _FakeTx(interactive=False, add_rows=None)
            u = TxUser(id="u2", name="Before")
            await u.merge(tx=tx, name="After")
            assert u.name == "After"  # buffered: applied in-memory at buffer time
            assert fired_pre and not fired_post
            for cb in tx.post_commit:
                await cb()
            assert fired_post
        finally:
            pre_update.disconnect(on_pre, TxUser)
            post_update.disconnect(on_post, TxUser)

    @pytest.mark.asyncio
    async def test_objects_tx_attaches_tx_to_queryset(self) -> None:
        tx = _FakeTx()
        qs = TxUser.objects(tx=tx)
        assert qs._tx is tx


# =============================================================================
# Issue #268: after a failed statement, the transaction is cancelled when the block ends and leaving
# the block raises — on every strategy, as buffered transactions already fail as a whole.
# =============================================================================


class _FailingTxnClient(_FakeTxnClient):
    """``_FakeTxnClient`` whose reads raise ``read_error`` and whose writes raise ``write_error`` when set."""

    def __init__(self) -> None:
        super().__init__()
        self.read_error: BaseException | None = None
        self.write_error: BaseException | None = None

    async def query_raw(self, sql: str, params: Any = None, session_id: Any = None, txn_id: Any = None) -> dict[str, Any]:
        if self.write_error is not None:
            self.calls.append(("query_raw", txn_id))
            raise self.write_error
        return await super().query_raw(sql, params, session_id, txn_id)

    async def query(self, sql: str, vars: Any = None, session_id: Any = None, txn_id: Any = None) -> Any:
        if self.read_error is not None:
            self.calls.append(("query", txn_id))
            raise self.read_error
        return await super().query(sql, vars, session_id, txn_id)


class TestInteractiveFailedStatementUnits:
    """The interactive strategy records every statement that fails; ``commit()`` then cancels and raises."""

    @pytest.mark.asyncio
    async def test_commit_after_a_failed_write_cancels_and_raises(self) -> None:
        c = _FailingTxnClient()
        tx = InteractiveTransaction(c, "TXN")
        await tx.add("CREATE User:1 CONTENT $data;", {"data": {}})
        c.err_envelope = {"result": [{"status": "ERR", "result": "boom"}]}
        with pytest.raises(SurrealDbError, match="rolled back: boom") as failed:
            await tx.add("CREATE User:2 CONTENT $data;", {"data": {}})
        with pytest.raises(SurrealDbError, match="nothing was committed") as info:
            await tx.commit()
        assert type(info.value) is SurrealDbError
        assert info.value.__cause__ is failed.value
        assert str(info.value) == f"{_CANCELLED_AFTER_FAILURE} (SurrealDbError)"  # the type name only (#278)
        assert ("commit", "TXN") not in c.calls
        assert c.calls.count(("cancel", "TXN")) == 1

    @pytest.mark.asyncio
    async def test_commit_after_a_request_that_raised_cancels_and_raises(self) -> None:
        c = _FailingTxnClient()
        c.write_error = RuntimeError("socket closed")
        tx = InteractiveTransaction(c, "TXN")
        with pytest.raises(RuntimeError, match="socket closed"):
            await tx.add("CREATE User:1;", {})
        with pytest.raises(SurrealDbError, match="nothing was committed"):
            await tx.commit()
        assert ("commit", "TXN") not in c.calls
        assert ("cancel", "TXN") in c.calls

    @pytest.mark.asyncio
    async def test_commit_after_a_failed_read_cancels_and_raises(self) -> None:
        from surreal_orm_lite._sdk import ServerError

        c = _FailingTxnClient()
        c.read_error = ServerError("Internal", "read refused")
        tx = InteractiveTransaction(c, "TXN")
        await tx.add("CREATE User:1;", {})
        with pytest.raises(ServerError, match="read refused"):
            await tx.run_read("SELECT * FROM User;", {})
        with pytest.raises(SurrealDbError, match="nothing was committed"):
            await tx.commit()
        assert ("commit", "TXN") not in c.calls
        assert ("cancel", "TXN") in c.calls

    @pytest.mark.asyncio
    async def test_a_failure_the_orm_reads_as_an_answer_does_not_fail_the_transaction(self) -> None:
        """A missing table reads as no rows: the ORM absorbs that read's error, and the transaction commits."""
        from surreal_orm_lite._sdk import NotFoundError

        c = _FailingTxnClient()
        c.read_error = NotFoundError("NotFound", "The table 'User' does not exist")
        tx = InteractiveTransaction(c, "TXN")
        with pytest.raises(NotFoundError) as info:
            await tx.run_read("SELECT * FROM User;", {})
        tx._absorb_failure(info.value)
        await tx.commit()
        assert ("commit", "TXN") in c.calls
        assert ("cancel", "TXN") not in c.calls

    @pytest.mark.asyncio
    async def test_a_conflict_raises_a_conflict_at_commit(self) -> None:
        c = _FailingTxnClient()
        c.write_error = RuntimeError(
            "Transaction conflict: Write conflict, retry the transaction. This transaction can be retried"
        )
        tx = InteractiveTransaction(c, "TXN")
        with pytest.raises(SurrealDbConflictError):
            await tx.add("UPDATE counter:c SET n += 1;", {})
        with pytest.raises(SurrealDbConflictError, match="nothing was committed"):
            await tx.commit()
        assert ("commit", "TXN") not in c.calls

    @pytest.mark.asyncio
    async def test_retry_on_conflict_retries_a_transaction_whose_conflict_was_caught(self) -> None:
        from surreal_orm_lite.concurrency import retry_on_conflict

        attempts: list[_FailingTxnClient] = []

        @retry_on_conflict(max_retries=2, base_delay=0.001, jitter=False)
        async def work() -> str:
            c = _FailingTxnClient()
            if not attempts:
                c.write_error = RuntimeError("Transaction conflict: Write conflict. This transaction can be retried")
            attempts.append(c)
            tx = InteractiveTransaction(c, "TXN")
            with contextlib.suppress(SurrealDbConflictError):
                await tx.add("UPDATE counter:c SET n += 1;", {})
            await tx.commit()
            return "done"

        assert await work() == "done"
        assert len(attempts) == 2
        assert ("commit", "TXN") not in attempts[0].calls
        assert ("commit", "TXN") in attempts[1].calls

    @pytest.mark.asyncio
    async def test_cancel_is_sent_once(self) -> None:
        c = _FailingTxnClient()
        tx = InteractiveTransaction(c, "TXN")
        c.err_envelope = {"result": [{"status": "ERR", "result": "boom"}]}
        with pytest.raises(SurrealDbError):
            await tx.add("CREATE User:1;", {})
        with pytest.raises(SurrealDbError, match="nothing was committed"):
            await tx.commit()
        await tx.cancel()  # transaction() cancels again after a failed commit
        assert c.calls.count(("cancel", "TXN")) == 1

    @pytest.mark.asyncio
    async def test_a_transaction_without_failure_commits(self) -> None:
        c = _FailingTxnClient()
        tx = InteractiveTransaction(c, "TXN")
        await tx.add("CREATE User:1;", {})
        await tx.run_read("SELECT * FROM User;", {})
        await tx.commit()
        assert ("commit", "TXN") in c.calls
        assert ("cancel", "TXN") not in c.calls


class _SlowTxnClient(_FakeTxnClient):
    """``_FakeTxnClient`` whose requests never answer."""

    async def query_raw(self, sql: str, params: Any = None, session_id: Any = None, txn_id: Any = None) -> dict[str, Any]:
        self.calls.append(("query_raw", txn_id))
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    async def query(self, sql: str, vars: Any = None, session_id: Any = None, txn_id: Any = None) -> Any:
        self.calls.append(("query", txn_id))
        await asyncio.sleep(10)


class TestInterruptedStatementUnits:
    """A statement interrupted inside the block (a timeout, a task cancellation) fails the transaction too."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["add", "run_read"])
    async def test_commit_after_an_interrupted_statement_cancels_and_raises(self, path: str) -> None:
        c = _SlowTxnClient()
        tx = InteractiveTransaction(c, "TXN")
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.01):
                await getattr(tx, path)("SELECT 1;", {})
        with pytest.raises(SurrealDbError, match="nothing was committed"):
            await tx.commit()
        assert ("commit", "TXN") not in c.calls
        assert c.calls.count(("cancel", "TXN")) == 1


class TestFailedStatementReadme:
    """The README's 2.x vs 3.x table says where the v0.30.3 row was measured."""

    def test_server_behaviour_header_sentence(self) -> None:
        """Its exact text, in version order among the other releases' sentences."""
        assert_server_header_sentence(
            "The row added in v0.30.3 was measured on 2.7.0 and 3.3.4, over WebSocket and HTTP, and on the embedded "
            "engine, identical on 3.1.5."
        )


class TxFailRow(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="tx_fail_row", primary_key="id")
    id: str | None = None
    age: int | str


#: ``age`` must be a non-negative ``int``: ``-1`` fails the field's ASSERT, ``"abc"`` its type.
_TX_FAIL_SCHEMA = "DEFINE TABLE tx_fail_row SCHEMAFULL; DEFINE FIELD age ON tx_fail_row TYPE int ASSERT $value >= 0;"


@contextlib.asynccontextmanager
async def _tx_fail_db(transport: str) -> Any:
    """Connect over *transport* (``ws``/``http``) with ``tx_fail_row`` defined fresh; drop it afterwards."""
    host, port = os.environ.get("SURREALDB_HOST", "localhost"), os.environ.get("SURREALDB_PORT", "8000")
    url = f"ws://{host}:{port}/rpc" if transport == "ws" else f"http://{host}:{port}"
    SurrealDBConnectionManager.set_connection(url=url, user="root", password="root", namespace="ns", database="db")
    client = await SurrealDBConnectionManager.get_client()
    with contextlib.suppress(Exception):
        await client.query("REMOVE TABLE tx_fail_row;", {})
    await client.query(_TX_FAIL_SCHEMA, {})
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            await client.query("REMOVE TABLE tx_fail_row;", {})
        await SurrealDBConnectionManager.close_connection()


async def _stored_ages(client: Any) -> dict[str, Any]:
    rows = await client.query("SELECT * FROM tx_fail_row;", {})
    return {str(row["id"].id): row["age"] for row in rows or []}


async def _fail_assert(tx: Transaction) -> None:
    await TxFailRow(id="bad", age=-1).save(tx=tx)


async def _fail_type(tx: Transaction) -> None:
    await TxFailRow(id="bad", age="abc").save(tx=tx)


async def _fail_write_by_filter(tx: Transaction) -> None:
    await TxFailRow.objects(tx=tx).filter(age=1).bulk_update(age="abc")


async def _fail_call_function(tx: Transaction) -> None:
    await SurrealDBConnectionManager.call_function("fn::tx_fail_missing", tx=tx)


_FAILING_STATEMENTS = {
    "field-assert": _fail_assert,
    "type-error": _fail_type,
    "write-by-filter": _fail_write_by_filter,
    "call-function": _fail_call_function,
}


@pytest.mark.parametrize("transport", ["ws", "http"])
class TestFailedStatementFailsTheTransactionE2E:
    """On every strategy — interactive (3.x over WebSocket), buffered (2.x, HTTP) — a statement that fails inside the
    block, its error caught there, makes leaving the block raise, and nothing of the block is committed."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failing", sorted(_FAILING_STATEMENTS))
    async def test_a_caught_failure_raises_at_exit_and_commits_nothing(self, transport: str, failing: str) -> None:
        async with _tx_fail_db(transport) as client:
            with pytest.raises(SurrealDbError):
                async with SurrealDBConnectionManager.transaction() as tx:
                    await TxFailRow(id="ok", age=1).save(tx=tx)
                    with contextlib.suppress(SurrealDbError):
                        await _FAILING_STATEMENTS[failing](tx)
            assert await _stored_ages(client) == {}

    @pytest.mark.asyncio
    async def test_the_block_s_own_exception_wins(self, transport: str) -> None:
        async with _tx_fail_db(transport) as client:
            with pytest.raises(RuntimeError, match="the block's own"):
                async with SurrealDBConnectionManager.transaction() as tx:
                    await TxFailRow(id="ok", age=1).save(tx=tx)
                    with contextlib.suppress(SurrealDbError):
                        await _fail_assert(tx)
                    raise RuntimeError("the block's own")
            assert await _stored_ages(client) == {}

    @pytest.mark.asyncio
    async def test_a_block_without_failure_commits(self, transport: str) -> None:
        async with _tx_fail_db(transport) as client:
            async with SurrealDBConnectionManager.transaction() as tx:
                await TxFailRow(id="a", age=1).save(tx=tx)
                await TxFailRow(id="b", age=2).save(tx=tx)
            assert await _stored_ages(client) == {"a": 1, "b": 2}

    @pytest.mark.asyncio
    async def test_the_next_transaction_commits(self, transport: str) -> None:
        async with _tx_fail_db(transport) as client:
            with pytest.raises(SurrealDbError):
                async with SurrealDBConnectionManager.transaction() as tx:
                    await TxFailRow(id="ok", age=1).save(tx=tx)
                    with contextlib.suppress(SurrealDbError):
                        await _fail_type(tx)
            async with SurrealDBConnectionManager.transaction() as tx:
                await TxFailRow(id="next", age=3).save(tx=tx)
            assert await _stored_ages(client) == {"next": 3}


class TestInteractiveFailedStatementE2E:
    """SurrealDB 3.x over WebSocket: the interactive strategy's exit error, and the reads the ORM answers itself."""

    @pytest.mark.asyncio
    async def test_the_exit_error_names_the_failed_statement(self) -> None:
        async with _tx_fail_db("ws") as client:
            if not await _native_txn_supported():
                pytest.skip("requires SurrealDB 3.x")
            with pytest.raises(SurrealDbError, match="nothing was committed") as info:
                async with SurrealDBConnectionManager.transaction() as tx:
                    assert tx.is_interactive
                    await TxFailRow(id="ok", age=1).save(tx=tx)
                    with pytest.raises(SurrealDbError, match="rolled back") as failed:
                        await _fail_assert(tx)
            assert info.value.__cause__ is failed.value
            assert await _stored_ages(client) == {}

    @pytest.mark.asyncio
    async def test_explicit_commit_after_a_failure_raises_and_cancels(self) -> None:
        async with _tx_fail_db("ws") as client:
            if not await _native_txn_supported():
                pytest.skip("requires SurrealDB 3.x")
            with pytest.raises(SurrealDbError, match="nothing was committed"):
                async with SurrealDBConnectionManager.transaction() as tx:
                    await TxFailRow(id="ok", age=1).save(tx=tx)
                    with contextlib.suppress(SurrealDbError):
                        await _fail_type(tx)
                    await tx.commit()
            assert await _stored_ages(client) == {}

    @pytest.mark.asyncio
    async def test_reads_of_a_missing_table_do_not_fail_the_transaction(self) -> None:
        """A table never written reads as no rows (``exec()``, ``all()``, ``count()``) and ``get()`` raises "not
        found": the ORM answers those reads itself, so the block still commits."""
        from surreal_orm_lite.exceptions import SurrealDbNotFoundError

        async with _tx_fail_db("ws") as client:
            if not await _native_txn_supported():
                pytest.skip("requires SurrealDB 3.x")
            await _clear(client)
            with contextlib.suppress(Exception):
                await client.query("REMOVE TABLE TxUser;", {})
            async with SurrealDBConnectionManager.transaction() as tx:
                assert await TxUser.objects(tx=tx).filter(name="x").exec() == []
                assert await TxUser.objects(tx=tx).all() == []
                assert await TxUser.objects(tx=tx).count() == 0
                with pytest.raises(SurrealDbNotFoundError):
                    await TxUser.objects(tx=tx).get("nobody")
                await TxFailRow(id="ok", age=1).save(tx=tx)
            assert await _stored_ages(client) == {"ok": 1}


# =============================================================================
# Issue #278: the errors the ORM raises when a transaction fails give the server's reason and the failed statement's
# position, never the statement's text; the embedded engine's commit error is a SurrealDbError too.
# =============================================================================

#: A literal the tests below put in a statement the server cannot parse: no error the ORM raises may show it.
_LITERAL = "tx-literal-5f1e9a"
#: A statement no SurrealDB line can parse (``SELEC``), holding ``_LITERAL``. The server reports it at column 7.
_UNPARSABLE = f"SELEC '{_LITERAL}' FRM tx_fail_row;"
#: A statement no SurrealDB line can parse, whose literal holds a retryable conflict's words.
_CONFLICT_WORDS = "SELEC 'write conflict' FRM tx_fail_row;"


def _shown(exc: BaseException, text: str) -> list[str]:
    """Where *exc* shows *text*: its ``str()``, its ``repr()`` and its formatted traceback (chained errors included)."""
    shown = {"str": str(exc), "repr": repr(exc), "traceback": "".join(traceback.format_exception(exc))}
    return [where for where, value in shown.items() if text in value]


def _parse_error(position: str, line: str) -> str:
    """A SurrealDB 3.x parse error at *position*, quoting *line* of the query (the shape measured on 3.3.4)."""
    return f"Parse error: Unexpected token `a strand`, expected Eof\n --> [{position}]\n  |\n{line}\n  |       ^^^^^\n"


class TestRaiseForStatusStatementTextUnits:
    """``raise_for_status()`` builds its error from the server's reason and the position it gives."""

    def test_an_error_envelope_gives_the_reason_and_the_position(self) -> None:
        message = _parse_error("1:7", f"1 | {_UNPARSABLE}")
        with pytest.raises(SurrealDbError) as info:
            Transaction.raise_for_status({"error": {"code": -32000, "message": message}})
        assert type(info.value) is SurrealDbError
        assert str(info.value) == (
            "Transaction failed: the server refused the statement: Parse error: Unexpected token `a strand`, "
            "expected Eof (at line 1, column 7 of the statement)"
        )
        assert _shown(info.value, _LITERAL) == []
        assert info.value.__cause__ is None
        assert info.value.__suppress_context__
        assert str(info.value.__context__) == message  # the server's error stays reachable

    def test_an_error_envelope_without_a_position(self) -> None:
        with pytest.raises(SurrealDbError) as info:
            Transaction.raise_for_status({"error": {"code": -32000, "message": "There was a problem with authentication"}})
        assert str(info.value) == "Transaction failed: the server refused the query: There was a problem with authentication"

    def test_a_retryable_conflict_in_an_error_envelope_is_a_conflict(self) -> None:
        message = "Transaction conflict: Write conflict, retry the transaction. This transaction can be retried"
        with pytest.raises(SurrealDbConflictError, match="can be retried"):
            Transaction.raise_for_status({"error": {"code": -32000, "message": message}})

    @pytest.mark.parametrize("envelope", [True, False], ids=["error-envelope", "statement-error"])
    def test_a_parse_error_quoting_conflict_words_is_not_a_conflict(self, envelope: bool) -> None:
        """The conflict typing reads the server's reason, not its excerpt of the statement."""
        message = _parse_error("1:7", f"1 | {_CONFLICT_WORDS}")
        raw = (
            {"error": {"code": -32000, "message": message}} if envelope else {"result": [{"status": "ERR", "result": message}]}
        )
        with pytest.raises(SurrealDbError) as info:
            Transaction.raise_for_status(raw)
        assert type(info.value) is SurrealDbError
        assert not is_conflict_error(info.value)

    @pytest.mark.parametrize("raw", [{"result": None}, {"error": {"message": None}}, None, [_LITERAL]])
    def test_an_unrecognized_shape_is_not_quoted(self, raw: Any) -> None:
        with pytest.raises(SurrealDbError, match="unrecognized query_raw\\(\\) response shape") as info:
            Transaction.raise_for_status(raw)
        assert _shown(info.value, _LITERAL) == []
        assert _shown(info.value, "result") == []
        assert str(info.value.__context__) == repr(raw)
        assert info.value.__suppress_context__

    def test_a_statement_error_that_quotes_the_query_keeps_its_first_line(self) -> None:
        # A value parsed at run time can be refused with a parse error quoting it (SurrealDB 2.x).
        message = (
            "Parse error: Unexpected token `an identifier`, expected no more tokens\n --> [1:5]\n  |\n"
            f"1 | {_LITERAL} x(\n  |     ^^^ \n"
        )
        raw = {"result": [{"status": "ERR", "result": message}]}
        with pytest.raises(SurrealDbError) as info:
            Transaction.raise_for_status(raw)
        assert str(info.value) == (
            "Transaction failed and rolled back: Parse error: Unexpected token `an identifier`, expected no more tokens"
        )
        assert _shown(info.value, _LITERAL) == []
        assert str(info.value.__context__) == message

    def test_a_statement_error_without_an_excerpt_is_kept_whole(self) -> None:
        raw = {"result": [{"status": "ERR", "result": "Database record `probe:c` already exists"}]}
        with pytest.raises(SurrealDbError) as info:
            Transaction.raise_for_status(raw)
        assert str(info.value) == "Transaction failed and rolled back: Database record `probe:c` already exists"
        assert info.value.__context__ is None


class _EnvelopeClient:
    """A client whose ``query_raw()`` answers with an error envelope carrying *message*, as a server does for a query
    it refuses as a whole."""

    def __init__(self, message: str) -> None:
        self.message = message

    async def query_raw(self, sql: str, params: Any = None) -> dict[str, Any]:
        return {"error": {"code": -32000, "message": self.message}}


#: The reason ``_parse_error()`` gives.
_REASON = "Parse error: Unexpected token `a strand`, expected Eof"


class TestBufferedPositionUnits:
    """A buffered commit names the queued statement a position falls in, and gives the line and column in the text
    the caller queued: the ORM's ``$name`` → ``$t<N>_name`` renaming does not shift the column."""

    @staticmethod
    async def _commit_error(queued: list[tuple[str, dict[str, Any] | None]], message: str) -> str:
        tx = BufferedTransaction(_EnvelopeClient(message))
        for statement, variables in queued:
            await tx.add(statement, variables)
        with pytest.raises(SurrealDbError) as info:
            await tx.commit()
        return str(info.value)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("queued", "position", "expected"),
        [
            ([("CREATE a:1;", None), ("SELEC 'x' FRM t;", None)], "3:7", "statement 2 of 2: {r} (at line 1, column 7"),
            (
                [("CREATE a:1\nSET b = 1;", None), ("RETURN 1;\nSELEC 'x';\nRETURN 2;", None)],
                "5:9",
                "statement 2 of 2: {r} (at line 2, column 9",
            ),
            ([("CREATE t SET a = $v, b = 'x' c;", {"v": 1})], "2:33", "statement 1 of 1: {r} (at line 1, column 30"),
            (
                [("RETURN 0;", None), ("CREATE t SET a = $v,\n  b = $w + $v x;", {"v": 1, "w": 2})],
                "4:21",
                "statement 2 of 2: {r} (at line 2, column 15",
            ),
            ([("CREATE t SET a = $v, b = 'x' c;", {"v": 1})], "2:20", "statement 1 of 1: {r} (at line 1, column 19"),
            ([("CREATE t SET a = $v, b = 'x' c;", {"other": 1})], "2:30", "statement 1 of 1: {r} (at line 1, column 30"),
            ([("RETURN (1 +", None)], "3:8", "the query: {r} (at line 3, column 8 of the query)"),
        ],
        ids=[
            "one-line-statements",
            "multi-line-statements",
            "a-renamed-parameter-before-the-column",
            "renamed-parameters-on-earlier-lines-and-after-the-column",
            "inside-a-renamed-parameter",
            "a-parameter-not-renamed",
            "outside-every-statement",
        ],
    )
    async def test_the_position_names_the_statement_as_queued(
        self, queued: list[tuple[str, dict[str, Any] | None]], position: str, expected: str
    ) -> None:
        message = await self._commit_error(queued, _parse_error(position, "…"))
        assert message.startswith("Transaction failed: the server refused " + expected.format(r=_REASON)), message


class _RaisingClient:
    """A client whose ``query_raw()`` raises *error* (not the embedded engine)."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def query_raw(self, sql: str, params: Any = None) -> dict[str, Any]:
        raise self.error


class TestCancellationErrorTextUnits:
    """The error ``commit()`` raises after a failed statement names only that error's type (issue #278)."""

    @pytest.mark.asyncio
    async def test_the_cancellation_error_names_the_failure_s_type(self) -> None:
        c = _FailingTxnClient()
        c.err_envelope = {"error": {"code": -32000, "message": _parse_error("1:7", f"1 | {_UNPARSABLE}")}}
        tx = InteractiveTransaction(c, "TXN")
        with pytest.raises(SurrealDbError) as failed:
            await tx.add(f"CREATE {_UNPARSABLE}", {})
        with pytest.raises(SurrealDbError) as info:
            await tx.commit()
        assert type(info.value) is SurrealDbError
        assert str(info.value) == f"{_CANCELLED_AFTER_FAILURE} (SurrealDbError)"
        assert info.value.__cause__ is failed.value
        assert _shown(info.value, _LITERAL) == []

    @pytest.mark.asyncio
    async def test_a_conflict_s_cancellation_error_is_a_conflict(self) -> None:
        c = _FailingTxnClient()
        c.write_error = RuntimeError(f"{_LITERAL}: Write conflict. This transaction can be retried")
        tx = InteractiveTransaction(c, "TXN")
        with pytest.raises(SurrealDbConflictError):
            await tx.add("UPDATE counter:c SET n += 1;", {})
        with pytest.raises(SurrealDbConflictError) as info:
            await tx.commit()
        assert str(info.value) == f"{_CANCELLED_AFTER_FAILURE} (SurrealDbConflictError)"
        assert _LITERAL not in str(info.value)

    @pytest.mark.asyncio
    async def test_a_runtime_error_of_a_server_client_is_not_rewritten(self) -> None:
        tx = BufferedTransaction(_RaisingClient(RuntimeError("Event loop is closed")))
        await tx.add("CREATE User:1;", None)
        with pytest.raises(RuntimeError, match="Event loop is closed"):
            await tx.commit()


@pytest.mark.parametrize("transport", ["ws", "http"])
class TestBufferedCommitErrorTextE2E:
    """A buffered commit (SurrealDB 2.x over WebSocket, HTTP on both lines) that the server refuses raises
    ``SurrealDbError`` naming the statement, without its text."""

    @pytest.mark.asyncio
    async def test_the_commit_error_keeps_the_statement_text_out(self, transport: str) -> None:
        async with _tx_fail_db(transport) as client:
            if transport == "ws" and await _native_txn_supported():
                pytest.skip("WebSocket on SurrealDB 3.x runs interactive transactions")
            with pytest.raises(SurrealDbError) as info:
                async with SurrealDBConnectionManager.transaction() as tx:
                    assert not tx.is_interactive
                    await TxFailRow(id="ok", age=1).save(tx=tx)
                    await tx.add(_UNPARSABLE)
                    queued = len(tx.statements)
            assert type(info.value) is SurrealDbError
            message = str(info.value)
            assert message.startswith(f"Transaction failed: the server refused statement {queued} of {queued}: ")
            assert "Parse error: " in message
            assert message.endswith("(at line 1, column 7 of the statement)")
            assert _shown(info.value, _LITERAL) == []
            assert info.value.__cause__ is None
            assert _LITERAL in str(info.value.__context__)  # the server's error stays reachable
            assert await _stored_ages(client) == {}

    @pytest.mark.asyncio
    async def test_the_column_is_the_one_in_the_queued_text(self, transport: str) -> None:
        """The ORM renames ``$v`` to ``$t1_v`` before sending: the column given is still the caller's."""
        statement = "CREATE tx_fail_row SET age = $v, note = 'x' oops;"
        async with _tx_fail_db(transport):
            if transport == "ws" and await _native_txn_supported():
                pytest.skip("WebSocket on SurrealDB 3.x runs interactive transactions")
            with pytest.raises(SurrealDbError) as info:
                async with SurrealDBConnectionManager.transaction() as tx:
                    await TxFailRow(id="ok", age=1).save(tx=tx)
                    await tx.add(statement, {"v": 2})
            column = statement.index("oops") + 1
            assert str(info.value).endswith(f"(at line 1, column {column} of the statement)"), str(info.value)

    @pytest.mark.asyncio
    async def test_a_parse_error_quoting_conflict_words_is_not_a_conflict(self, transport: str) -> None:
        async with _tx_fail_db(transport) as client:
            if transport == "ws" and await _native_txn_supported():
                pytest.skip("WebSocket on SurrealDB 3.x runs interactive transactions")
            with pytest.raises(SurrealDbError) as info:
                async with SurrealDBConnectionManager.transaction() as tx:
                    await TxFailRow(id="ok", age=1).save(tx=tx)
                    await tx.add(_CONFLICT_WORDS)
            assert type(info.value) is SurrealDbError
            assert not is_conflict_error(info.value)
            assert await _stored_ages(client) == {}


class TestInteractiveErrorTextE2E:
    """SurrealDB 3.x over WebSocket: a statement the server cannot parse fails inside the block, the block catches its
    error; neither that error nor the cancellation error shows the statement's text."""

    @pytest.mark.asyncio
    async def test_the_exit_error_keeps_the_statement_text_out(self) -> None:
        async with _tx_fail_db("ws") as client:
            if not await _native_txn_supported():
                pytest.skip("requires SurrealDB 3.x")
            with pytest.raises(SurrealDbError) as info:
                async with SurrealDBConnectionManager.transaction() as tx:
                    assert tx.is_interactive
                    await TxFailRow(id="ok", age=1).save(tx=tx)
                    with pytest.raises(SurrealDbError) as failed:
                        await tx.add(_UNPARSABLE)
            assert type(info.value) is SurrealDbError
            assert str(info.value) == f"{_CANCELLED_AFTER_FAILURE} (SurrealDbError)"
            assert info.value.__cause__ is failed.value
            assert str(failed.value) == (
                "Transaction failed: the server refused the statement: Parse error: Unexpected token `a strand`, "
                "expected Eof (at line 1, column 7 of the statement)"
            )
            assert _shown(info.value, _LITERAL) == []
            assert _shown(failed.value, _LITERAL) == []
            assert _LITERAL in str(failed.value.__context__)  # the server's error stays reachable
            assert await _stored_ages(client) == {}

    @pytest.mark.asyncio
    async def test_an_explicit_commit_s_error_keeps_the_statement_text_out(self) -> None:
        async with _tx_fail_db("ws") as client:
            if not await _native_txn_supported():
                pytest.skip("requires SurrealDB 3.x")
            with pytest.raises(SurrealDbError) as info:
                async with SurrealDBConnectionManager.transaction() as tx:
                    await TxFailRow(id="ok", age=1).save(tx=tx)
                    with pytest.raises(SurrealDbError) as failed:
                        await tx.add(_UNPARSABLE)
                    await tx.commit()
            assert str(info.value) == f"{_CANCELLED_AFTER_FAILURE} (SurrealDbError)"
            assert info.value.__cause__ is failed.value
            assert _shown(info.value, _LITERAL) == []
            assert await _stored_ages(client) == {}

    @pytest.mark.asyncio
    async def test_a_parse_error_quoting_conflict_words_is_not_a_conflict(self) -> None:
        async with _tx_fail_db("ws") as client:
            if not await _native_txn_supported():
                pytest.skip("requires SurrealDB 3.x")
            with pytest.raises(SurrealDbError) as info:
                async with SurrealDBConnectionManager.transaction() as tx:
                    await TxFailRow(id="ok", age=1).save(tx=tx)
                    with pytest.raises(SurrealDbError) as failed:
                        await tx.add(_CONFLICT_WORDS)
            assert type(failed.value) is SurrealDbError
            assert type(info.value) is SurrealDbError
            assert str(info.value) == f"{_CANCELLED_AFTER_FAILURE} (SurrealDbError)"
            assert not is_conflict_error(info.value)
            assert await _stored_ages(client) == {}


def _require_embedded_engine() -> None:
    """Skip when the installed SDK has no in-process engine."""
    from tests.test_embedded import _embedded_engine_available

    if not _embedded_engine_available():
        pytest.skip("the installed surrealdb SDK has no embedded engine")


@contextlib.asynccontextmanager
async def _embedded_tx_db() -> Any:
    """The embedded engine (``mem://``) with ``tx_fail_row`` defined; unset afterwards."""
    _require_embedded_engine()
    SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="t")
    try:
        client = await SurrealDBConnectionManager.get_client()
        await client.query(_TX_FAIL_SCHEMA, {})
        yield client
    finally:
        await SurrealDBConnectionManager.unset_connection()


class TestEmbeddedCommitErrorE2E:
    """The embedded engine raises a bare ``RuntimeError`` for a query it cannot parse: the commit raises
    ``SurrealDbError`` (``SurrealDbConflictError`` for a retryable conflict), as on a server."""

    @pytest.mark.asyncio
    async def test_the_commit_error_is_a_surreal_db_error_without_the_statement_text(self) -> None:
        async with _embedded_tx_db() as client:
            with pytest.raises(SurrealDbError) as info:
                async with SurrealDBConnectionManager.transaction() as tx:
                    await TxFailRow(id="ok", age=1).save(tx=tx)
                    await tx.add(_UNPARSABLE)
                    queued = len(tx.statements)
            assert type(info.value) is SurrealDbError
            message = str(info.value)
            assert message.startswith(f"Transaction failed: the server refused statement {queued} of {queued}: ")
            assert message.endswith("(at line 1, column 7 of the statement)")
            assert _shown(info.value, _LITERAL) == []
            assert info.value.__cause__ is None
            assert isinstance(info.value.__context__, RuntimeError)  # the engine's error stays reachable
            assert _LITERAL in str(info.value.__context__)
            assert await _stored_ages(client) == {}

    @pytest.mark.asyncio
    async def test_a_retryable_conflict_is_a_conflict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from surrealdb import AsyncEmbeddedSurrealConnection

        async def conflict(self: Any, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("Failed to commit transaction due to a read or write conflict. This transaction can be retried")

        async with _embedded_tx_db():
            monkeypatch.setattr(AsyncEmbeddedSurrealConnection, "query_raw", conflict)
            with pytest.raises(SurrealDbConflictError, match="can be retried"):
                async with SurrealDBConnectionManager.transaction() as tx:
                    await TxFailRow(id="ok", age=1).save(tx=tx)
