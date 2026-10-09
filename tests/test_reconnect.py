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
