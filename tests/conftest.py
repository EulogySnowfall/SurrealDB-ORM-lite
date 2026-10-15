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

**The v0.23.0 encoding boundary is the identity on everything v0.22.2 could send.** Every payload
the ORM hands the SDK goes through ``surreal_orm_lite.encoding.encode()``, which converts rich
values (durations, dates, sets, ranges, exponent decimals) and must return the very object it was
given for anything else, so existing payloads keep their exact bytes. The autouse guard below
proves it on the whole pre-existing suite, on both server lines: for every test of a module that
existed at v0.22.2 it wraps ``encode`` and fails the test if any call returned a different object.
Only the three modules written for the rich field types, the two v0.24.0 geometry modules and the
v0.24.3 read-hook module are exempt (``_REWRITE_MODULES``), and ``tests/test_embedded.py`` (v0.25.0),
which writes rich and geo values on the embedded engine. A new test that needs rich values goes into
one of them — never add a pre-existing module to that set.
"""

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import pytest_asyncio

from surreal_orm_lite import SurrealDBConnectionManager, encoding

#: The test modules written for v0.23.0's rich field types, v0.24.0's geometries, v0.24.3's read
#: hook and v0.25.0's embedded engine: the only ones whose values ``encode()`` may rewrite. Never list a
#: module that existed at v0.22.2 here.
_REWRITE_MODULES = frozenset(
    {
        "tests.test_encoding",
        "tests.test_rich_types",
        "tests.test_rich_fields",
        "tests.test_geo_types",
        "tests.test_geo_fields",
        "tests.test_decoding",
        "tests.test_embedded",
    }
)


@pytest_asyncio.fixture(autouse=True, loop_scope="function")
async def _drop_cached_connection() -> None:
    yield
    await SurrealDBConnectionManager.close_connection()


@pytest.fixture(autouse=True)
def _boundary_identity_guard(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail a pre-existing test if the encoding boundary rewrote any value it sent (spec §9.2)."""
    if request.module.__name__ in _REWRITE_MODULES:
        yield
        return
    real = encoding.encode
    seen: list[str] = []

    def guarded(value: Any) -> Any:
        out = real(value)
        if out is not value:
            seen.append(repr(value)[:200])
        return out

    monkeypatch.setattr(encoding, "encode", guarded)
    yield
    assert not seen, f"encode() rewrote values in a v0.22.2 test: {seen}"


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

    The final drops are bounded (10 s each).
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
                await asyncio.wait_for(client.query(f"REMOVE TABLE {table};", {}), 10)
        await SurrealDBConnectionManager.close_connection()


@contextlib.asynccontextmanager
async def change_feed_database(database: str) -> AsyncIterator[Any]:
    """Connected ORM client on ``ns``/``database``, removed and redefined on entry, removed on exit.

    **Every change-feed table outside ``tests/test_change_feeds.py`` lives in a database of its
    own, through this helper — never in ``ns``/``db``.** Measured on 2026-10-12 and again on
    2026-10-14: SurrealDB 2.7.0 applies the ``LIMIT`` of ``SHOW CHANGES FOR TABLE t …`` to the
    whole database's feed before keeping ``t``'s entries, and ``REMOVE TABLE`` keeps a table's
    feed history (on 3.3.3 too). An entry of another change-feed table in ``ns``/``db`` therefore
    misleads the 2.x end-of-feed search of ``changes()`` and makes the feed of
    ``tests/test_change_feeds.py`` replay old writes — on a long-running server, or in a random
    order. ``REMOVE DATABASE`` clears the feed on both lines.

    ``database`` is a constant identifier of the calling test module, never user input.
    """
    SurrealDBConnectionManager.set_connection(url=_ws_url(), user="root", password="root", namespace="ns", database=database)
    client = await SurrealDBConnectionManager.get_client()
    await client.query(f"REMOVE DATABASE IF EXISTS {database}; DEFINE DATABASE {database};", {})
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            cleanup = await SurrealDBConnectionManager.get_client()
            await cleanup.query(f"REMOVE DATABASE IF EXISTS {database};", {})
        await SurrealDBConnectionManager.close_connection()
