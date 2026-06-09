"""Tests for v0.12.0 optimistic concurrency: SurrealDbConflictError,
is_conflict_error, and the retry_on_conflict decorator."""

import pytest

from surreal_orm_lite.concurrency import is_conflict_error, retry_on_conflict
from surreal_orm_lite.exceptions import SurrealDbConflictError, SurrealDbError

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
