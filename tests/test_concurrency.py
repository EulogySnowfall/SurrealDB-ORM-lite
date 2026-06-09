"""Tests for v0.12.0 optimistic concurrency: SurrealDbConflictError,
is_conflict_error, and the retry_on_conflict decorator."""

import asyncio
import contextlib
import os

import pytest

from surreal_orm_lite import (
    BaseSurrealModel,
    SurrealConfigDict,
    SurrealDBConnectionManager,
)
from surreal_orm_lite._sdk import AsyncSurreal
from surreal_orm_lite.concurrency import is_conflict_error, retry_on_conflict
from surreal_orm_lite.exceptions import SurrealDbConflictError, SurrealDbError
from surreal_orm_lite.transaction import InteractiveTransaction, Transaction

# Verbatim server messages captured on :8001 (3.1.3) and :8002 (2.6.5).
CONFLICT_3X = "Transaction conflict: Write conflict, retry the transaction. This transaction can be retried"
CONFLICT_3X_BUFFERED = "Transaction failed and rolled back: Cannot COMMIT: Transaction conflict: Write conflict, retry the transaction. This transaction can be retried"
CONFLICT_26 = "The query was not executed due to a failed transaction. Failed to commit transaction due to a read or write conflict. This transaction can be retried"
FILLER = "The query was not executed due to a failed transaction"
ALREADY = "Transaction failed and rolled back: Database record `probe:c` already exists"


def test_conflict_error_is_surrealdb_error_subclass() -> None:
    assert issubclass(SurrealDbConflictError, SurrealDbError)
    err = SurrealDbConflictError("boom")
    assert isinstance(err, SurrealDbError)
    assert str(err) == "boom"


def test_is_conflict_error_true_for_real_messages() -> None:
    assert is_conflict_error(SurrealDbError(CONFLICT_3X)) is True
    assert is_conflict_error(SurrealDbError(CONFLICT_3X_BUFFERED)) is True
    assert is_conflict_error(SurrealDbError(CONFLICT_26)) is True


def test_is_conflict_error_true_for_conflict_type() -> None:
    assert is_conflict_error(SurrealDbConflictError("anything at all")) is True


def test_is_conflict_error_false_for_non_conflict() -> None:
    # The non-retryable filler must NOT match (would loop on duplicate-key errors).
    assert is_conflict_error(SurrealDbError(FILLER)) is False
    assert is_conflict_error(SurrealDbError(ALREADY)) is False
    assert is_conflict_error(ValueError("totally unrelated")) is False


def test_is_conflict_error_walks_cause_chain() -> None:
    inner = RuntimeError(CONFLICT_3X)
    outer = SurrealDbError("wrapper with no signal of its own")
    outer.__cause__ = inner
    assert is_conflict_error(outer) is True


def _record_sleep(monkeypatch) -> list[float]:
    """Patch the decorator's asyncio.sleep to record delays without waiting."""
    slept: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr("surreal_orm_lite.concurrency.asyncio.sleep", _fake_sleep)
    return slept


@pytest.mark.asyncio
async def test_retry_succeeds_after_one_conflict(monkeypatch) -> None:
    slept = _record_sleep(monkeypatch)
    calls = {"n": 0}

    @retry_on_conflict(max_retries=3, base_delay=0.05, backoff_factor=2.0, jitter=False)
    async def op() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise SurrealDbError(CONFLICT_3X)
        return "ok"

    assert await op() == "ok"
    assert calls["n"] == 2
    assert slept == [0.05]  # one retry, deterministic backoff


@pytest.mark.asyncio
async def test_retry_exhausts_then_raises_conflict(monkeypatch) -> None:
    slept = _record_sleep(monkeypatch)

    @retry_on_conflict(max_retries=3, base_delay=0.05, max_delay=2.0, backoff_factor=2.0, jitter=False)
    async def always_conflict() -> None:
        raise SurrealDbError(CONFLICT_3X)

    with pytest.raises(SurrealDbConflictError, match="persisted after 3 retries"):
        await always_conflict()
    # 4 attempts (1 + 3 retries) → sleeps before retries 1..3 only.
    assert slept == [0.05, 0.10, 0.20]


@pytest.mark.asyncio
async def test_non_conflict_not_retried(monkeypatch) -> None:
    slept = _record_sleep(monkeypatch)

    @retry_on_conflict(max_retries=3, jitter=False)
    async def boom() -> None:
        raise ValueError("not a conflict")

    with pytest.raises(ValueError, match="not a conflict"):
        await boom()
    assert slept == []  # never retried


@pytest.mark.asyncio
async def test_backoff_capped_at_max_delay(monkeypatch) -> None:
    slept = _record_sleep(monkeypatch)

    @retry_on_conflict(max_retries=4, base_delay=1.0, max_delay=2.0, backoff_factor=10.0, jitter=False)
    async def always_conflict() -> None:
        raise SurrealDbError(CONFLICT_3X)

    with pytest.raises(SurrealDbConflictError):
        await always_conflict()
    # 1.0, then 10.0→capped 2.0, 100.0→2.0, 1000.0→2.0
    assert slept == [1.0, 2.0, 2.0, 2.0]


def test_retry_param_validation() -> None:
    with pytest.raises(ValueError, match="max_retries"):
        retry_on_conflict(max_retries=-1)
    with pytest.raises(ValueError, match="base_delay"):
        retry_on_conflict(base_delay=0)
    with pytest.raises(ValueError, match="max_delay"):
        retry_on_conflict(max_delay=0)
    with pytest.raises(ValueError, match="backoff_factor"):
        retry_on_conflict(backoff_factor=0)


def test_public_exports() -> None:
    import surreal_orm_lite as m
    from surreal_orm_lite.concurrency import (
        is_conflict_error as ice,
    )
    from surreal_orm_lite.concurrency import (
        retry_on_conflict as roc,
    )
    from surreal_orm_lite.exceptions import SurrealDbConflictError as sdce

    assert m.retry_on_conflict is roc
    assert m.is_conflict_error is ice
    assert m.SurrealDbConflictError is sdce
    for name in ("retry_on_conflict", "is_conflict_error", "SurrealDbConflictError"):
        assert name in m.__all__


# --------------------------------------------------------------------------- #
# E2E: real conflicts against a live SurrealDB (8001 = 3.1.3, 8002 = 2.6.5)
# --------------------------------------------------------------------------- #


def _url() -> str:
    host = os.environ.get("SURREALDB_HOST", "localhost")
    port = os.environ.get("SURREALDB_PORT", "8000")
    return f"ws://{host}:{port}/rpc"


async def _raw_client():
    """A standalone SDK connection (NOT the ORM singleton) for interleaving txns."""
    db = AsyncSurreal(_url())
    await db.connect(_url())
    await db.signin({"username": "root", "password": "root"})
    await db.use("ns", "db")
    return db


async def _supports_interactive(client) -> bool:
    try:
        txn = await client.begin()
    except Exception:
        return False
    with contextlib.suppress(Exception):
        await client.cancel(txn)
    return True


def _connect() -> None:
    SurrealDBConnectionManager.set_connection(
        url=_url(),
        user="root",
        password="root",
        namespace="ns",
        database="db",
    )


class OccUser(BaseSurrealModel):
    model_config = SurrealConfigDict(primary_key="id")
    id: str | None = None
    name: str


class TestConflictE2E:
    @pytest.mark.asyncio
    async def test_interactive_conflict_is_typed(self) -> None:
        c0 = await _raw_client()
        if not await _supports_interactive(c0):
            await c0.close()
            pytest.skip("native interactive transactions require SurrealDB 3.x")
        with contextlib.suppress(Exception):
            await c0.query("DELETE counter:occ;")
        await c0.query("CREATE counter:occ SET n = 0;")
        c1 = await _raw_client()
        c2 = await _raw_client()
        try:
            t1 = await c1.begin()
            t2 = await c2.begin()
            tx1 = InteractiveTransaction(c1, t1)
            tx2 = InteractiveTransaction(c2, t2)
            # read-then-write the same key in both → write/write conflict at commit
            await tx1.run_read("SELECT * FROM counter:occ;", {})
            await tx2.run_read("SELECT * FROM counter:occ;", {})
            await tx1.add("UPDATE counter:occ SET n = 11;", {})
            await tx2.add("UPDATE counter:occ SET n = 22;", {})
            await tx1.commit()  # wins
            with pytest.raises(SurrealDbConflictError):
                await tx2.commit()  # loses → typed conflict
        finally:
            for c in (c0, c1, c2):
                with contextlib.suppress(Exception):
                    await c.close()

    @pytest.mark.asyncio
    async def test_buffered_conflict_raise_for_status(self) -> None:
        # Force a conflict with several interleaved BEGIN..COMMIT batches; feed a
        # failing raw response through raise_for_status. Skip if none reproduced
        # (2.6.x serialises more, so a small race may not always conflict).
        seed = await _raw_client()
        with contextlib.suppress(Exception):
            await seed.query("DELETE counter:occb;")
        await seed.query("CREATE counter:occb SET n = 0;")
        clients = [await _raw_client() for _ in range(8)]
        q = (
            "BEGIN TRANSACTION;\n"
            "LET $v = (SELECT n FROM counter:occb)[0].n;\n"
            "UPDATE counter:occb SET n = $v + 1;\n"
            "COMMIT TRANSACTION;"
        )
        results = await asyncio.gather(*[c.query_raw(q) for c in clients], return_exceptions=True)
        failed = next(
            (
                r
                for r in results
                if isinstance(r, dict) and any(isinstance(s, dict) and s.get("status") == "ERR" for s in r.get("result", []))
            ),
            None,
        )
        for c in (seed, *clients):
            with contextlib.suppress(Exception):
                await c.close()
        if failed is None:
            pytest.skip("no transaction conflict reproduced on this server/run")
        with pytest.raises(SurrealDbConflictError, match="can be retried"):
            Transaction.raise_for_status(failed)

    @pytest.mark.asyncio
    async def test_retry_on_conflict_wraps_live_transaction(self) -> None:
        # The decorator composes with a real transaction() on both DB lines: the
        # happy path commits and returns its value (no conflict needed here).
        _connect()
        client = await SurrealDBConnectionManager.get_client()
        with contextlib.suppress(Exception):
            await client.query("DELETE OccUser;", {})

        @retry_on_conflict(max_retries=2, base_delay=0.01, jitter=False)
        async def create_user() -> str:
            async with SurrealDBConnectionManager.transaction() as tx:
                await OccUser(id="zoe", name="Zoe").save(tx=tx)
            return "done"

        assert await create_user() == "done"
        rows = await client.query("SELECT * FROM OccUser;", {})
        assert len(rows) == 1
        await SurrealDBConnectionManager.close_connection()
