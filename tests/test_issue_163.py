"""Regression tests for issue #163 — the client cache is per-event-loop (v0.14.5).

``SurrealDBConnectionManager`` cached one client in a class attribute with no notion of the
loop that created it. A WebSocket client is bound to the loop it connected on, so handing it
to code running on another loop fails with ``got Future attached to a different loop``. Two
successive ``asyncio.run(...)`` calls in the same process were enough to trigger it.

The cache is now keyed by the running loop: each loop gets its own connection, entries whose
loop has been closed are pruned, and ``close_connection()`` only closes the current loop's
client.
"""

import asyncio
import os
import threading

import pytest

from src import surreal_orm_lite
from src.surreal_orm_lite import BaseSurrealModel, SurrealDBConnectionManager

SURREALDB_HOST = os.environ.get("SURREALDB_HOST", "localhost")
SURREALDB_PORT = os.environ.get("SURREALDB_PORT", "8000")
HTTP_URL = f"http://{SURREALDB_HOST}:{SURREALDB_PORT}"
WS_URL = f"ws://{SURREALDB_HOST}:{SURREALDB_PORT}/rpc"


class Issue163Model(BaseSurrealModel):
    id: str | None = None
    name: str


# A stale client does not always raise: before the fix a WebSocket one could block forever.
# Every call that would hang on a regression is wrapped so the test fails fast instead of
# freezing CI.
TIMEOUT = 15.0


def _run(coro) -> object:  # type: ignore[no-untyped-def]
    """``asyncio.run`` with a deadline, so a regression fails instead of hanging."""

    async def _guarded() -> object:
        return await asyncio.wait_for(coro, timeout=TIMEOUT)

    return asyncio.run(_guarded())


def _connect(url: str) -> None:
    SurrealDBConnectionManager.set_connection(url, "root", "root", "ns", "db")


def _cache() -> dict:
    """The manager's private per-loop cache.

    Reached through the mangled name on purpose: the alternative is a test-only hook in
    production code, and the cache's shape is exactly what these tests are about.
    """
    return SurrealDBConnectionManager._SurrealDBConnectionManager__clients  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
async def _clean_manager() -> None:
    yield
    await SurrealDBConnectionManager.close_all_connections()


# ==================== The reported failure ====================


@pytest.mark.parametrize("url", [HTTP_URL, WS_URL], ids=["http", "ws"])
def test_two_successive_asyncio_run_calls(url: str) -> None:
    """The issue's reproduction: each ``asyncio.run`` creates and closes its own loop."""
    _connect(url)

    async def work() -> int:
        client = await SurrealDBConnectionManager.get_client()
        rows = await client.query("RETURN 1;")
        await SurrealDBConnectionManager.close_connection()
        return rows

    assert _run(work()) == 1
    # Before the fix this second run reused a client bound to the first, closed loop.
    assert _run(work()) == 1


def test_second_run_without_an_explicit_close() -> None:
    """The cache must recover even when the caller never closes the connection.

    Requiring ``close_connection()`` before leaving a loop would only move the trap.
    """
    _connect(WS_URL)

    async def work() -> int:
        client = await SurrealDBConnectionManager.get_client()
        return await client.query("RETURN 1;")

    assert _run(work()) == 1
    assert _run(work()) == 1


def test_orm_survives_a_second_loop() -> None:
    """The same thing through the public ORM surface rather than the raw client."""
    _connect(WS_URL)

    async def work(name: str) -> str:
        await Issue163Model.objects().delete_table()
        await Issue163Model(id="a", name=name).save()
        row = await Issue163Model.objects().get("a")
        await SurrealDBConnectionManager.close_all_connections()
        return row.name

    assert _run(work("first")) == "first"
    assert _run(work("second")) == "second"


# ==================== Concurrent loops ====================


def test_two_live_loops_keep_their_own_client() -> None:
    """A second loop must not evict the first one's client while it is still in use."""
    _connect(WS_URL)
    results: dict[str, object] = {}
    started = threading.Event()
    release = threading.Event()

    async def hold_then_query() -> None:
        client = await SurrealDBConnectionManager.get_client()
        results["first_client"] = id(client)
        started.set()
        await asyncio.get_running_loop().run_in_executor(None, release.wait)
        # The other loop has since asked for a client; this one must still work.
        results["first_query"] = await asyncio.wait_for(client.query("RETURN 1;"), TIMEOUT)
        await SurrealDBConnectionManager.close_connection()

    thread = threading.Thread(target=lambda: _run(hold_then_query()))
    thread.start()
    started.wait(timeout=10)

    async def second_loop() -> None:
        client = await SurrealDBConnectionManager.get_client()
        results["second_client"] = id(client)
        results["second_query"] = await client.query("RETURN 2;")
        await SurrealDBConnectionManager.close_connection()

    _run(second_loop())
    release.set()
    thread.join(timeout=10)

    assert results["second_query"] == 2
    assert results["first_query"] == 1, "the first loop's client was evicted from under it"
    assert results["first_client"] != results["second_client"]


# ==================== Cache bookkeeping ====================


async def test_same_loop_reuses_one_client() -> None:
    _connect(HTTP_URL)

    first = await SurrealDBConnectionManager.get_client()
    second = await SurrealDBConnectionManager.get_client()
    assert first is second


async def test_close_connection_only_closes_the_current_loop() -> None:
    _connect(HTTP_URL)
    await SurrealDBConnectionManager.get_client()

    other_loop = asyncio.new_event_loop()
    try:
        _cache()[other_loop] = object()
        assert len(_cache()) == 2

        await SurrealDBConnectionManager.close_connection()
        assert len(_cache()) == 1
        assert other_loop in _cache(), "the other loop's client must survive"
    finally:
        other_loop.close()


async def test_entries_for_closed_loops_are_pruned() -> None:
    _connect(HTTP_URL)

    dead_loop = asyncio.new_event_loop()
    dead_loop.close()
    _cache()[dead_loop] = object()

    # Asking for a client prunes the dead entry rather than leaking it.
    await SurrealDBConnectionManager.get_client()
    assert len(_cache()) == 1
    assert dead_loop not in _cache()


async def test_close_all_connections_empties_the_cache() -> None:
    _connect(HTTP_URL)
    await SurrealDBConnectionManager.get_client()

    await SurrealDBConnectionManager.close_all_connections()
    assert len(_cache()) == 0


async def test_unset_connection_drops_every_client() -> None:
    _connect(HTTP_URL)
    await SurrealDBConnectionManager.get_client()

    await SurrealDBConnectionManager.unset_connection()
    assert len(_cache()) == 0
    assert SurrealDBConnectionManager.is_connection_set() is False

    # Leave the manager usable for the rest of the suite.
    _connect(HTTP_URL)


async def test_context_manager_still_works() -> None:
    _connect(HTTP_URL)

    async with SurrealDBConnectionManager() as client:
        assert await client.query("RETURN 1;") == 1
    assert len(_cache()) == 0


async def test_get_client_without_connection_settings_raises() -> None:
    await SurrealDBConnectionManager.unset_connection()
    try:
        with pytest.raises(ValueError, match="Connection not been set"):
            await surreal_orm_lite.SurrealDBConnectionManager.get_client()
    finally:
        _connect(HTTP_URL)
