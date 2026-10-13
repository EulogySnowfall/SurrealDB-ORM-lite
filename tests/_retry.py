"""Retry a test's own writes on SurrealDB's retryable conflicts.

Shared by the tests that write to a ``CHANGEFEED`` table, so that a conflict raised by the
server itself does not make them flaky.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


async def retry_write(operation: Callable[[], Awaitable[T]]) -> T:
    """Run a test write, retrying SurrealDB's retryable conflicts.

    SurrealDB 2.x (measured on 2.7.0) fails a write to a CHANGEFEED table with a retryable
    "read or write conflict … can be retried" about every 10 s, caused by a server background
    task, even with a single writer. Measured: none on a plain table, none on 3.3.4. A refused
    transaction writes nothing, so replaying the write is safe.

    *operation* is called once per attempt, so it must create a new coroutine each time (a
    ``lambda`` does). The ORM's answer to a retryable conflict is ``retry_on_conflict``.
    """
    from surreal_orm_lite import retry_on_conflict

    @retry_on_conflict(max_retries=5)
    async def _run() -> T:
        return await operation()

    return await _run()
