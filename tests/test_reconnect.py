"""Tests for v0.21.0 — surviving a dropped WebSocket.

Every E2E test here talks to SurrealDB through :class:`tests._proxy.CuttableProxy`, which can
abort the connection the way a network failure does. Measured on 2.7.0 and 3.3.2 alike: the
SDK's receive task ends within milliseconds, the server forgets the session's live queries, and
the dead client fails every later call. Nothing in this file is version-gated.

Every wait is bounded (``asyncio.timeout``): the defect this release fixes is a reader that
hangs forever, so a regression must fail a test, not stall the suite.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest

from surreal_orm_lite import BaseSurrealModel, SurrealDBConnectionManager
from surreal_orm_lite.exceptions import SurrealDbConnectionError
from surreal_orm_lite.live import is_dead, recv_task_of
from tests._proxy import CuttableProxy

TABLE = "Reconnected"


class Reconnected(BaseSurrealModel):
    id: str
    name: str = ""
    role: str = ""


def _direct_url() -> str:
    host = os.environ.get("SURREALDB_HOST", "localhost")
    port = os.environ.get("SURREALDB_PORT", "8000")
    return f"ws://{host}:{port}/rpc"


@contextlib.asynccontextmanager
async def proxied(*tables: str) -> AsyncIterator[CuttableProxy]:
    """An ORM connection routed through a cuttable proxy, with ``tables`` defined and dropped.

    Cleanup goes through a *direct* connection, so it works whatever state the test left the
    proxy in (cut, refusing, or closed).
    """
    tables = tables or (TABLE,)
    async with CuttableProxy() as proxy:
        SurrealDBConnectionManager.set_connection(url=proxy.url(), user="root", password="root", namespace="ns", database="db")
        client = await SurrealDBConnectionManager.get_client()
        for table in tables:
            await client.query(f"REMOVE TABLE IF EXISTS {table}; DEFINE TABLE {table} SCHEMALESS;", {})
        try:
            yield proxy
        finally:
            await SurrealDBConnectionManager.close_connection()
            proxy.accept()
            SurrealDBConnectionManager.set_connection(
                url=_direct_url(), user="root", password="root", namespace="ns", database="db"
            )
            cleanup = await SurrealDBConnectionManager.get_client()
            for table in tables:
                with contextlib.suppress(Exception):
                    await cleanup.query(f"REMOVE TABLE IF EXISTS {table};", {})
            await SurrealDBConnectionManager.close_connection()


async def _wait_dead(client: Any, timeout: float = 2.0) -> None:
    """Until the SDK has noticed the drop — a few milliseconds in practice."""
    async with asyncio.timeout(timeout):
        while not is_dead(client):
            await asyncio.sleep(0.005)


# ==================== Task 1 — the connection manager replaces dead clients ====================


class TestDeadClientDetectionUnit:
    def test_a_client_without_a_receive_task_is_never_dead(self) -> None:
        """HTTP clients have no socket to lose."""

        class _HttpLike:
            pass

        assert recv_task_of(_HttpLike()) is None
        assert is_dead(_HttpLike()) is False

    @pytest.mark.asyncio
    async def test_a_finished_receive_task_means_dead(self) -> None:
        class _WsLike:
            def __init__(self) -> None:
                self.recv_task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(0))

        client = _WsLike()
        assert is_dead(client) is False
        await client.recv_task
        assert is_dead(client) is True

    @pytest.mark.asyncio
    async def test_a_session_wrapper_is_looked_through(self) -> None:
        class _Inner:
            def __init__(self) -> None:
                self.recv_task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(0))

        class _Wrapper:
            def __init__(self) -> None:
                self._connection = _Inner()

        wrapper = _Wrapper()
        assert recv_task_of(wrapper) is wrapper._connection.recv_task
        await wrapper._connection.recv_task


class TestConcurrentOpenUnit:
    @pytest.mark.asyncio
    async def test_concurrent_get_client_opens_a_single_connection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Before v0.21.0 every caller opened its own client and the last one won the cache;
        the others leaked their socket — and a live query started on one could never be killed."""
        opened: list[object] = []

        class _Client:
            async def close(self) -> None:
                pass

        async def _slow_open(cls: Any, *, signin_as_configured: bool) -> object:
            await asyncio.sleep(0.05)
            client = _Client()
            opened.append(client)
            return client

        SurrealDBConnectionManager.set_connection(
            url="ws://unused.invalid/rpc", user="root", password="root", namespace="ns", database="db"
        )
        monkeypatch.setattr(SurrealDBConnectionManager, "_open_client", classmethod(_slow_open))
        try:
            clients = await asyncio.gather(*(SurrealDBConnectionManager.get_client() for _ in range(20)))
        finally:
            await SurrealDBConnectionManager.close_connection()

        assert len(opened) == 1
        assert all(c is opened[0] for c in clients)


class TestDeadClientReplacementE2E:
    @pytest.mark.asyncio
    async def test_the_proxy_cut_ends_the_sdk_receive_task(self) -> None:
        """Sanity check of the harness itself."""
        async with proxied() as proxy:
            client = await SurrealDBConnectionManager.get_client()
            proxy.cut()
            await _wait_dead(client)

    @pytest.mark.asyncio
    async def test_get_client_replaces_a_dropped_client(self) -> None:
        async with proxied() as proxy:
            before = await SurrealDBConnectionManager.get_client()
            await Reconnected(id="one", name="a").save()
            proxy.cut()
            await _wait_dead(before)

            rows = await Reconnected.objects().all()

            after = await SurrealDBConnectionManager.get_client()
            assert after is not before
            assert [r.id for r in rows] == ["one"]
            assert proxy.connections == 2

    @pytest.mark.asyncio
    async def test_the_session_is_replayed_on_the_replacement(self) -> None:
        """A rebuilt client must come back as the record user, never as the configured root."""
        access, table = "reconnect_acct", "reconnect_user"
        async with proxied(table) as proxy:
            client = await SurrealDBConnectionManager.get_client()
            await client.query(
                f"DEFINE TABLE OVERWRITE {table} SCHEMALESS PERMISSIONS FOR select WHERE id = $auth.id;"
                f"DEFINE ACCESS OVERWRITE {access} ON DATABASE TYPE RECORD"
                f" SIGNUP (CREATE {table} SET email = $email)"
                f" SIGNIN (SELECT * FROM {table} WHERE email = $email);",
                {},
            )
            email = f"{uuid4().hex}@example.test"
            try:
                await SurrealDBConnectionManager.signup(access=access, variables={"email": email})
                proxy.cut()
                await _wait_dead(client)

                info = await SurrealDBConnectionManager.info()

                assert info is not None and info["email"] == email
            finally:
                SurrealDBConnectionManager.clear_session()
                await SurrealDBConnectionManager.close_connection()
                root = await SurrealDBConnectionManager.get_client()
                with contextlib.suppress(Exception):
                    await root.query(f"REMOVE ACCESS {access} ON DATABASE;", {})

    @pytest.mark.asyncio
    async def test_killing_a_live_query_on_a_dropped_client_is_a_no_op(self) -> None:
        """The live query died with the socket; there is nothing to send and nothing to raise."""
        from surreal_orm_lite import live

        async with proxied() as proxy:
            client = await SurrealDBConnectionManager.get_client()
            live_id = await client.query(f"LIVE SELECT * FROM {TABLE};", {})
            proxy.cut()
            await _wait_dead(client)

            await SurrealDBConnectionManager.kill(live_id)  # must not raise

            assert str(live_id) in live._killed()
            assert proxy.connections == 1, "kill() must not reconnect just to send a KILL"


# ==================== Task 2 — a drop ends readers instead of hanging ====================


async def _next_within(stream: Any, timeout: float = 3.0) -> Any:
    async with asyncio.timeout(timeout):
        return await anext(stream)


class TestDropEndsReadersE2E:
    """v0.19.0/v0.20.0 left these readers parked on their queue forever after a drop."""

    @pytest.mark.asyncio
    async def test_watch_without_resubscribe_raises_on_drop(self) -> None:
        async with proxied() as proxy:
            async with Reconnected.objects().watch(auto_resubscribe=False) as stream:
                proxy.cut()
                with pytest.raises(SurrealDbConnectionError, match="dropped"):
                    await _next_within(stream)
                assert stream.is_active is False
                assert stream.live_id is None
            # Leaving the block after the loss sends nothing: there is no socket left to send on.
            assert proxy.connections == 1

    @pytest.mark.asyncio
    async def test_live_without_resubscribe_raises_on_drop(self) -> None:
        async with proxied() as proxy:
            async with Reconnected.objects().live(auto_resubscribe=False) as stream:
                proxy.cut()
                with pytest.raises(SurrealDbConnectionError):
                    await _next_within(stream)
                assert stream.is_active is False
            assert proxy.connections == 1

    @pytest.mark.asyncio
    async def test_iterating_again_after_the_loss_ends_the_stream(self) -> None:
        async with proxied() as proxy, Reconnected.objects().watch(auto_resubscribe=False) as stream:
            proxy.cut()
            with pytest.raises(SurrealDbConnectionError):
                await _next_within(stream)
            with pytest.raises(StopAsyncIteration):
                await _next_within(stream)

    @pytest.mark.asyncio
    async def test_a_secondary_subscriber_raises_on_drop(self) -> None:
        """A ``subscribe_live()`` reader is pinned to one uuid: it cannot follow a resubscribe."""
        async with proxied() as proxy, Reconnected.objects().watch() as stream:
            assert stream.live_id is not None
            reader = SurrealDBConnectionManager.subscribe_live(stream.live_id)
            proxy.cut()
            with pytest.raises(SurrealDbConnectionError):
                await _next_within(reader)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("auto_resubscribe", [True, False])
    async def test_close_connection_still_ends_the_stream_cleanly(self, auto_resubscribe: bool) -> None:
        """Closing the socket on purpose also ends the SDK's receive task; that is not a drop."""
        async with proxied() as proxy:
            async with Reconnected.objects().watch(auto_resubscribe=auto_resubscribe) as stream:
                await SurrealDBConnectionManager.close_connection()
                with pytest.raises(StopAsyncIteration):
                    await _next_within(stream)
                await asyncio.sleep(0.2)  # room for a wrongly scheduled resubscribe to show up
            assert proxy.connections == 1

    @pytest.mark.asyncio
    async def test_kill_still_ends_the_stream_cleanly(self) -> None:
        async with proxied(), Reconnected.objects().watch() as stream:
            assert stream.live_id is not None
            await SurrealDBConnectionManager.kill(stream.live_id)
            with pytest.raises(StopAsyncIteration):
                await _next_within(stream)


class TestDropCallbackUnit:
    @pytest.mark.asyncio
    async def test_a_released_queue_ignores_the_drop(self) -> None:
        """kill()/close_connection() release the queue *before* the socket closes."""
        from surreal_orm_lite import live

        class _Client:
            def __init__(self) -> None:
                self.live_queues: dict[str, Any] = {}
                self.recv_task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(10))

        client = _Client()
        iterator = live.open_stream(client, "q-1")
        live.release_subscribers("q-1")
        client.recv_task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        with pytest.raises(StopAsyncIteration):
            await _next_within(iterator)

    @pytest.mark.asyncio
    async def test_a_detached_reader_stops_watching_the_socket(self) -> None:
        from surreal_orm_lite import live

        class _Client:
            def __init__(self) -> None:
                self.live_queues: dict[str, Any] = {}
                self.recv_task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(10))

        client = _Client()
        iterator = live.open_stream(client, "q-2")
        await iterator.aclose()
        assert client.recv_task.remove_done_callback(iterator._connection_lost) == 0
        client.recv_task.cancel()
