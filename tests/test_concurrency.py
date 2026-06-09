"""Tests for v0.12.0 optimistic concurrency: SurrealDbConflictError,
is_conflict_error, and the retry_on_conflict decorator."""

from surreal_orm_lite.concurrency import is_conflict_error
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
