"""Tests for v0.12.0 optimistic concurrency: SurrealDbConflictError,
is_conflict_error, and the retry_on_conflict decorator."""

from surreal_orm_lite.exceptions import SurrealDbConflictError, SurrealDbError


def test_conflict_error_is_surrealdb_error_subclass() -> None:
    assert issubclass(SurrealDbConflictError, SurrealDbError)
    err = SurrealDbConflictError("boom")
    assert isinstance(err, SurrealDbError)
    assert str(err) == "boom"
