"""Shared test fixtures.

**Import the package one way only: ``surreal_orm_lite``.** The repository is a src layout, so
``src.surreal_orm_lite`` used to resolve too, and half the suite imported it that way. Python
treats the two spellings as two unrelated modules built from the same files, which quietly gives
the suite *two of everything*: two ``SurrealDBConnectionManager`` classes with their own client
caches, two signal registries, two alias caches. The autouse fixture below then dropped the
cached client of only one of them, which is exactly the stale-client coupling it exists to
prevent (issue #160), and a handler registered through one spelling could never fire for a model
defined through the other. ``src/__init__.py`` — the shim that made the second spelling
importable, and which was never shipped in the wheel — is gone, and
``test_import_identity.py`` fails if either spelling comes back.

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

import contextlib
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio

from surreal_orm_lite import SurrealDBConnectionManager


@pytest_asyncio.fixture(autouse=True, loop_scope="function")
async def _drop_cached_connection() -> None:
    yield
    await SurrealDBConnectionManager.close_connection()


def _ws_url() -> str:
    host = os.environ.get("SURREALDB_HOST", "localhost")
    port = os.environ.get("SURREALDB_PORT", "8000")
    return f"ws://{host}:{port}/rpc"


@contextlib.asynccontextmanager
async def orm_client(*tables: str) -> AsyncIterator[Any]:
    """Connected ORM client with ``tables`` dropped before and after (default: ``Aliased``).

    An async context manager rather than a fixture: the SDK's WebSocket client is bound to the
    event loop that created it, and fixtures run in a different loop from the test body. Dropping
    a table also drops its field definitions, which is what isolates DDL tests.
    """
    tables = tables or ("Aliased",)
    SurrealDBConnectionManager.set_connection(url=_ws_url(), user="root", password="root", namespace="ns", database="db")
    client = await SurrealDBConnectionManager.get_client()
    for table in tables:
        with contextlib.suppress(Exception):
            await client.query(f"REMOVE TABLE {table};", {})
    try:
        yield client
    finally:
        for table in tables:
            with contextlib.suppress(Exception):
                await client.query(f"REMOVE TABLE {table};", {})
        await SurrealDBConnectionManager.close_connection()
