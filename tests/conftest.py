"""Shared test fixtures.

The one thing every test in this suite has in common is the connection manager, and it
caches a single client. A WebSocket client is bound to the event loop it connected on, while
pytest-asyncio gives each test a fresh loop — so a client left in the cache by one test is
already stale for the next, and the SDK fails with ``got Future attached to a different
loop``. In declaration order that stayed hidden, because the tests that connect over
WebSocket happen to close the connection themselves; under ``pytest-randomly`` (or
``pytest-xdist``) a test from another file lands in between and the whole transaction suite
falls over (issue #160).

Dropping the cached client after every test makes the coupling impossible rather than
unlikely. It costs a reconnect per test, which is negligible next to the round-trips the E2E
tests already make.
"""

import pytest_asyncio

from src.surreal_orm_lite import SurrealDBConnectionManager


@pytest_asyncio.fixture(autouse=True, loop_scope="function")
async def _drop_cached_connection() -> None:
    yield
    await SurrealDBConnectionManager.close_connection()
