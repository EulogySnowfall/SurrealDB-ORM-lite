"""Retry a test's own writes on SurrealDB's retryable conflicts.

Shared by the tests that write to a ``CHANGEFEED`` table, so that a conflict raised by the
server itself does not make them flaky, and by every E2E helper that resets a database of its
own (``REMOVE DATABASE IF EXISTS x; DEFINE DATABASE x; …``, issue #396) or writes one raw statement to a
``CHANGEFEED`` table (issue #406).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")


async def retry_write(operation: Callable[[], Awaitable[T]]) -> T:
    """Run a test write, retrying SurrealDB's retryable conflicts.

    SurrealDB 2.x (measured on 2.7.0) refuses concurrent writes to a CHANGEFEED table with a
    retryable "read or write conflict … can be retried", even on different records (2.5 to 4 %
    with 8 writers); a single writer is refused at most once per change-feed GC cycle (10 s by
    default). Measured: none on a plain table, none on 3.3.4. A refused transaction writes
    nothing, so replaying the write is safe for the data.

    *operation* is called once per attempt, so it must create a new coroutine each time (a
    ``lambda`` does). The ORM's answer to a retryable conflict is ``retry_on_conflict``.
    """
    from surreal_orm_lite import retry_on_conflict

    @retry_on_conflict(max_retries=5)
    async def _run() -> T:
        return await operation()

    return await _run()


#: The text SurrealDB puts in a refusal that may be replayed as is.
RETRYABLE_CONFLICT = "can be retried"

#: How many times :func:`apply_reset` sends a reset before it fails the test.
RESET_ATTEMPTS = 5

#: The pause before the n-th replay of a reset is ``n`` times this many seconds.
_RESET_BACKOFF = 0.05


async def _reset_refusals(client: Any, query: str) -> list[str]:
    """Send *query* through ``query_raw()`` and return the text of every refused statement (``[]`` when SurrealDB
    accepted them all): ``query()`` checks only the first statement. A query-level error or an answer without any
    statement result fails the test at once."""
    response = await client.query_raw(query, {})
    if "error" in response:
        raise AssertionError(f"{query!r} was refused: {response['error']}")
    results = response.get("result") or []
    if not results:
        raise AssertionError(f"{query!r} answered no statement result: {response!r}")
    return [str(result.get("result")) for result in results if result.get("status") != "OK"]


async def apply_reset(client: Any, query: str) -> None:
    """Run the database reset *query* (``REMOVE DATABASE IF EXISTS x; DEFINE DATABASE x; …``) and check every statement.

    SurrealDB 2.x (seen once on 2.7.0, issue #396) can refuse the ``DEFINE DATABASE`` of a reset with "Failed to commit
    transaction due to a read or write conflict. This transaction can be retried" when a background server transaction
    (change-feed GC, database reclaim) races it. A reset starts with ``REMOVE DATABASE IF EXISTS``, so replaying the
    whole of it is safe: it is replayed — at most :data:`RESET_ATTEMPTS` times in all, with a short growing pause — only
    while **every** refusal carries that retryable text. Any other refusal, a query-level error or an empty answer
    fails the test at once with ``AssertionError`` and the server's text.

    *client* is the SDK client of the database's connection; *query* is a constant of the test, never user input.
    """
    await _replay(client, query)


async def apply_write(client: Any, statement: str) -> None:
    """Run the single raw *statement* — a write or a definition on a ``CHANGEFEED`` table — and check it.

    On SurrealDB 2.x the change-feed GC can refuse it with the retryable conflict :func:`retry_write` describes (issue
    #406). One refused statement is one refused transaction, which writes nothing, so the statement is replayed as
    :func:`apply_reset` replays a reset: only on that retryable text, at most :data:`RESET_ATTEMPTS` times in all. Pass
    one statement per call: replaying a query whose other statements were applied would fail on them.
    """
    await _replay(client, statement)


async def _replay(client: Any, query: str) -> None:
    """:func:`apply_reset`'s loop: send *query*, replay it while every refusal is retryable, fail on anything else."""
    for attempt in range(1, RESET_ATTEMPTS + 1):
        refusals = await _reset_refusals(client, query)
        if not refusals:
            return
        if not all(RETRYABLE_CONFLICT in refusal for refusal in refusals):
            raise AssertionError(f"{query!r} was refused: {refusals}")
        if attempt == RESET_ATTEMPTS:
            raise AssertionError(f"{query!r} was refused {RESET_ATTEMPTS} times with a retryable conflict: {refusals}")
        await asyncio.sleep(_RESET_BACKOFF * attempt)
