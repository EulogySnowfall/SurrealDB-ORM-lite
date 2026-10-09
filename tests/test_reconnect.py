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
from uuid import UUID, uuid4

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


# ==================== Task 3 — auto-resubscribe ====================


@pytest.fixture
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the reconnect backoff so the tests measure behaviour, not patience."""
    from surreal_orm_lite.live import LiveModelStream, LiveStream

    for cls in (LiveStream, LiveModelStream):
        monkeypatch.setattr(cls, "reconnect_delay", 0.05)
        monkeypatch.setattr(cls, "reconnect_max_delay", 0.2)


async def _resubscribed(stream: Any, old: Any, timeout: float = 5.0) -> None:
    """Until the stream runs under a new live query id."""
    async with asyncio.timeout(timeout):
        while stream.live_id is None or stream.live_id == old:
            await asyncio.sleep(0.01)


async def _direct_lives(table: str = TABLE) -> dict[str, Any]:
    """The live queries the server holds on *table*, read over a separate direct connection."""
    from surrealdb import AsyncSurreal

    client = AsyncSurreal(_direct_url())
    await client.connect(_direct_url())
    try:
        await client.signin({"username": "root", "password": "root"})
        await client.use("ns", "db")
        info = await client.query(f"INFO FOR TABLE {table};", {})
        return dict(info.get("lives") or {})
    finally:
        await client.close()


@pytest.mark.usefixtures("fast_backoff")
class TestAutoResubscribeE2E:
    @pytest.mark.asyncio
    async def test_watch_resubscribes_after_a_drop(self) -> None:
        async with proxied() as proxy, Reconnected.objects().watch() as stream:
            old = stream.live_id
            proxy.cut()
            await _resubscribed(stream, old)

            await Reconnected(id="after", name="x").save()

            envelope = await _next_within(stream)
            assert envelope["action"] == "CREATE"
            assert str(envelope["record"].id) == "after"
            assert stream.is_active is True
            assert proxy.connections == 2

    @pytest.mark.asyncio
    async def test_a_reader_parked_in_async_for_never_notices(self) -> None:
        """The queue moves to the new uuid unchanged, so a pending read simply completes."""
        async with proxied() as proxy, Reconnected.objects().watch() as stream:
            old = stream.live_id
            pending = asyncio.ensure_future(_next_within(stream, timeout=5.0))
            await asyncio.sleep(0.05)
            proxy.cut()
            await _resubscribed(stream, old)
            await Reconnected(id="late", name="x").save()
            envelope = await pending
            assert str(envelope["record"].id) == "late"

    @pytest.mark.asyncio
    async def test_typed_stream_keeps_its_filter_and_diff_mode(self) -> None:
        async with proxied() as proxy, Reconnected.objects().filter(role="admin").live(diff=True) as stream:
            old = stream.live_id
            proxy.cut()
            await _resubscribed(stream, old)

            await Reconnected(id="guest", name="g", role="guest").save()
            boss = Reconnected(id="boss", name="a", role="admin")
            await boss.save()
            await boss.merge(name="b")

            created = await _next_within(stream)
            updated = await _next_within(stream)
            assert created.action == "CREATE"
            assert isinstance(created.instance, Reconnected)
            assert created.instance.id == "boss"
            assert updated.action == "UPDATE"
            assert updated.changed_fields == ["name"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["async", "sync"])
    async def test_on_reconnect_receives_the_old_and_new_uuids(self, kind: str) -> None:
        calls: list[tuple[Any, Any]] = []
        done = asyncio.Event()

        def _sync(old: UUID, new: UUID) -> None:
            calls.append((old, new))
            done.set()

        async def _async(old: UUID, new: UUID) -> None:
            _sync(old, new)

        callback = _async if kind == "async" else _sync
        async with proxied() as proxy, Reconnected.objects().watch(on_reconnect=callback) as stream:
            old = stream.live_id
            proxy.cut()
            async with asyncio.timeout(5):
                await done.wait()

            assert calls == [(old, stream.live_id)]
            assert isinstance(calls[0][0], UUID) and isinstance(calls[0][1], UUID)
            assert calls[0][0] != calls[0][1]

    @pytest.mark.asyncio
    async def test_a_failing_on_reconnect_is_logged_and_the_stream_carries_on(self, caplog: pytest.LogCaptureFixture) -> None:
        async def _broken(old: UUID, new: UUID) -> None:
            raise RuntimeError("catch-up failed")

        async with proxied() as proxy, Reconnected.objects().watch(on_reconnect=_broken) as stream:
            old = stream.live_id
            with caplog.at_level("ERROR", logger="surreal_orm_lite.live"):
                proxy.cut()
                await _resubscribed(stream, old)
                await Reconnected(id="still", name="x").save()
                envelope = await _next_within(stream)
            assert str(envelope["record"].id) == "still"
            assert "on_reconnect" in caplog.text
            assert "catch-up failed" in caplog.text

    @pytest.mark.asyncio
    async def test_resumes_once_the_server_is_reachable_again(self) -> None:
        async with proxied() as proxy, Reconnected.objects().watch() as stream:
            old = stream.live_id
            proxy.refuse()
            proxy.cut()
            await asyncio.sleep(0.4)  # several refused attempts
            assert stream.live_id == old, "nothing to resubscribe to while the server is unreachable"
            proxy.accept()
            await _resubscribed(stream, old)

            await Reconnected(id="back", name="x").save()
            envelope = await _next_within(stream)
            assert str(envelope["record"].id) == "back"

    @pytest.mark.asyncio
    async def test_gives_up_after_the_last_attempt(self) -> None:
        async with proxied() as proxy, Reconnected.objects().watch() as stream:
            stream.reconnect_max_attempts = 2
            proxy.refuse()
            proxy.cut()
            with pytest.raises(SurrealDbConnectionError, match="2 attempts"):
                await _next_within(stream, timeout=5.0)
            assert stream.is_active is False

    @pytest.mark.asyncio
    async def test_the_typed_stream_forwards_its_own_policy(self) -> None:
        async with proxied() as proxy, Reconnected.objects().live() as stream:
            stream.reconnect_max_attempts = 1
            proxy.refuse()
            proxy.cut()
            with pytest.raises(SurrealDbConnectionError, match="1 attempt"):
                await _next_within(stream, timeout=5.0)
            assert stream.is_active is False

    @pytest.mark.asyncio
    async def test_stop_during_a_pending_resubscribe_leaves_nothing_behind(self) -> None:
        async with proxied() as proxy:
            stream = await Reconnected.objects().watch().start()
            proxy.refuse()
            proxy.cut()
            await asyncio.sleep(0.1)

            await stream.stop()
            proxy.accept()
            await asyncio.sleep(0.4)

            assert stream.is_active is False
            assert proxy.connections == 1, "a cancelled resubscribe must not reconnect afterwards"
            assert await _direct_lives() == {}

    @pytest.mark.asyncio
    async def test_a_drop_during_on_reconnect_resubscribes_again(self) -> None:
        calls: list[UUID] = []
        second = asyncio.Event()
        proxy_ref: list[CuttableProxy] = []

        async def _flaky(old: UUID, new: UUID) -> None:
            calls.append(new)
            if len(calls) == 1:
                proxy_ref[0].cut()
            else:
                second.set()

        async with proxied() as proxy, Reconnected.objects().watch(on_reconnect=_flaky) as stream:
            proxy_ref.append(proxy)
            proxy.cut()
            async with asyncio.timeout(5):
                await second.wait()
            await Reconnected(id="twice", name="x").save()
            envelope = await _next_within(stream)
            assert str(envelope["record"].id) == "twice"
            assert stream.live_id == calls[-1]

    @pytest.mark.asyncio
    async def test_exactly_one_live_query_remains_after_a_resubscribe(self) -> None:
        async with proxied() as proxy, Reconnected.objects().watch() as stream:
            old = stream.live_id
            proxy.cut()
            await _resubscribed(stream, old)
            lives = await _direct_lives()
            assert list(lives) == [str(stream.live_id)]
        assert await _direct_lives() == {}


class _FakeWs:
    """An SDK-shaped WebSocket client: a live-query registry and a receive task to end."""

    def __init__(self) -> None:
        self.live_queues: dict[str, Any] = {}
        self.recv_task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(3600))

    def drop(self) -> None:
        self.recv_task.cancel()

    async def close(self) -> None:
        self.recv_task.cancel()


@pytest.mark.usefixtures("fast_backoff")
class TestResubscribeFailuresUnit:
    """Which failures are retried, driven with fakes so each one can be produced on demand."""

    @staticmethod
    def _stream(
        monkeypatch: pytest.MonkeyPatch, clients: list[Any], starter_errors: list[Exception | None]
    ) -> tuple[Any, list[int]]:
        from surreal_orm_lite.live import LiveStream

        SurrealDBConnectionManager.set_connection(
            url="ws://unused.invalid/rpc", user="root", password="root", namespace="ns", database="db"
        )

        async def _get_client(cls: Any) -> Any:
            item = clients.pop(0)
            if isinstance(item, Exception):
                raise item
            # What the real get_client() does for every client it opens.
            if SurrealDBConnectionManager._identity_of(item) is None:
                SurrealDBConnectionManager._stamp_identity(item, replayable=True)
            return item

        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", classmethod(_get_client))
        starts = [0]

        async def _starter(client: Any = None) -> UUID:
            starts[0] += 1
            error = starter_errors.pop(0) if starter_errors else None
            if error is not None:
                raise error
            return uuid4()

        return LiveStream("t", _starter), starts

    @pytest.mark.asyncio
    async def test_a_rejected_session_token_is_never_retried_as_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from surreal_orm_lite.exceptions import SurrealDbAuthenticationError

        first = _FakeWs()
        rejected = SurrealDbAuthenticationError("restoring the session on a new connection failed")
        stream, starts = self._stream(monkeypatch, [first, rejected], [])
        await stream.start()
        first.drop()

        with pytest.raises(SurrealDbAuthenticationError):
            await _next_within(stream)
        assert starts[0] == 1, "no subscription may be opened once the identity is lost"
        assert stream.is_active is False

    @pytest.mark.asyncio
    async def test_a_missing_table_is_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from surreal_orm_lite.exceptions import SurrealDbNotFoundError

        first, second = _FakeWs(), _FakeWs()
        stream, starts = self._stream(monkeypatch, [first, second], [None, SurrealDbNotFoundError("gone")])
        await stream.start()
        first.drop()

        with pytest.raises(SurrealDbNotFoundError):
            await _next_within(stream)
        assert starts[0] == 2

    @pytest.mark.asyncio
    async def test_connection_failures_are_retried_until_one_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        first, fourth = _FakeWs(), _FakeWs()
        clients: list[Any] = [first, SurrealDbConnectionError("down"), ConnectionRefusedError("down"), fourth]
        stream, starts = self._stream(monkeypatch, clients, [])
        await stream.start()
        old = stream.live_id
        first.drop()

        await _resubscribed(stream, old)
        assert starts[0] == 2
        assert stream._client is fourth
        await stream.stop()

    def test_the_failure_classifier(self) -> None:
        from surreal_orm_lite.live import is_connection_failure

        class _Dead:
            """A client whose receive task has finished — what a dropped socket leaves behind."""

            def __init__(self) -> None:
                loop = asyncio.new_event_loop()
                self.recv_task = loop.create_task(asyncio.sleep(0))
                loop.run_until_complete(self.recv_task)
                loop.close()

        ConnectionClosedError = type("ConnectionClosedError", (Exception,), {"__module__": "websockets.exceptions"})

        assert is_connection_failure(SurrealDbConnectionError("x"), None)
        assert is_connection_failure(ConnectionRefusedError("x"), None)
        assert is_connection_failure(ConnectionClosedError("x"), None)
        # The SDK's in-flight-request KeyError: only a connection failure because the client died.
        assert is_connection_failure(KeyError("req"), _Dead())
        assert not is_connection_failure(KeyError("req"), None)
        assert not is_connection_failure(ValueError("bad"), None)


# ==================== Task 3 — security review: identity drift on resubscribe ====================


@pytest.mark.usefixtures("fast_backoff")
class TestResubscribeIdentityUnit:
    """A live query runs with the permissions of whoever opened it; a resubscribe must not change that."""

    @pytest.mark.asyncio
    async def test_an_identity_change_since_start_refuses_to_resubscribe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from surreal_orm_lite.exceptions import SurrealDbAuthenticationError

        first, second = _FakeWs(), _FakeWs()
        clients: list[Any] = [first, second]
        stream, starts = TestResubscribeFailuresUnit._stream(monkeypatch, clients, [])
        await stream.start()
        SurrealDBConnectionManager._replay_identity_changed()  # e.g. signin() as someone else
        first.drop()

        with pytest.raises(SurrealDbAuthenticationError, match="comes back as changed"):
            await _next_within(stream)
        assert starts[0] == 1
        assert clients == [second], "no connection is even opened"

    @pytest.mark.asyncio
    async def test_an_unreplayable_identity_refuses_to_resubscribe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from surreal_orm_lite.exceptions import SurrealDbAuthenticationError

        first, second = _FakeWs(), _FakeWs()
        stream, starts = TestResubscribeFailuresUnit._stream(monkeypatch, [first, second], [])
        SurrealDBConnectionManager._stamp_identity(first, replayable=False)  # signin(store=False)
        await stream.start()
        first.drop()

        with pytest.raises(SurrealDbAuthenticationError, match="cannot restore"):
            await _next_within(stream)
        assert starts[0] == 1

    @pytest.mark.asyncio
    async def test_a_store_false_signin_after_start_does_not_block_a_resubscribe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Minting a token for someone else (store=False) does not change what a reconnect
        restores, so a stream opened before it may resubscribe (review finding #4b)."""
        first, second = _FakeWs(), _FakeWs()
        stream, starts = TestResubscribeFailuresUnit._stream(monkeypatch, [first, second], [])
        await stream.start()
        old = stream.live_id
        SurrealDBConnectionManager._stamp_identity(first, replayable=False)
        first.drop()

        await _resubscribed(stream, old)
        assert starts[0] == 2
        await stream.stop()

    @pytest.mark.asyncio
    async def test_a_changed_configuration_refuses_to_resubscribe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """set_connection() and the set_* setters change who a new connection signs in as (#3a)."""
        from surreal_orm_lite.exceptions import SurrealDbAuthenticationError

        first, second = _FakeWs(), _FakeWs()
        stream, starts = TestResubscribeFailuresUnit._stream(monkeypatch, [first, second], [])
        await stream.start()
        await SurrealDBConnectionManager.set_database("elsewhere")
        first.drop()

        with pytest.raises(SurrealDbAuthenticationError):
            await _next_within(stream)
        assert starts[0] == 1


OWNED_ACCESS = "reconnect_owned_acct"
OWNED_USERS = "reconnect_owner"


@contextlib.asynccontextmanager
async def record_user(*, store: bool) -> AsyncIterator[str]:
    """Inside :func:`proxied`: make ``Reconnected`` owner-readable, sign a record user up, yield its id."""
    root = await SurrealDBConnectionManager.get_client()
    await root.query(
        f"DEFINE TABLE OVERWRITE {TABLE} SCHEMALESS PERMISSIONS FOR select WHERE owner = $auth.id;"
        f"DEFINE TABLE OVERWRITE {OWNED_USERS} SCHEMALESS PERMISSIONS FOR select WHERE id = $auth.id;"
        f"DEFINE ACCESS OVERWRITE {OWNED_ACCESS} ON DATABASE TYPE RECORD"
        f" SIGNUP (CREATE {OWNED_USERS} SET email = $email)"
        f" SIGNIN (SELECT * FROM {OWNED_USERS} WHERE email = $email);",
        {},
    )
    try:
        await SurrealDBConnectionManager.signup(
            access=OWNED_ACCESS, variables={"email": f"{uuid4().hex}@example.test"}, store=store
        )
        user_id = await root.query("RETURN <string> $auth.id;", {})
        yield str(user_id)
    finally:
        SurrealDBConnectionManager.clear_session()
        await SurrealDBConnectionManager.close_connection()
        cleanup = await SurrealDBConnectionManager.get_client()
        for statement in (f"REMOVE ACCESS {OWNED_ACCESS} ON DATABASE;", f"REMOVE TABLE {OWNED_USERS};"):
            with contextlib.suppress(Exception):
                await cleanup.query(statement, {})


async def _direct_query(sql: str) -> Any:
    from surrealdb import AsyncSurreal

    client = AsyncSurreal(_direct_url())
    await client.connect(_direct_url())
    try:
        await client.signin({"username": "root", "password": "root"})
        await client.use("ns", "db")
        return await client.query(sql, {})
    finally:
        await client.close()


@pytest.mark.usefixtures("fast_backoff")
class TestResubscribeIdentityE2E:
    @pytest.mark.asyncio
    async def test_a_stored_record_identity_resubscribes_with_its_own_permissions(self) -> None:
        async with proxied() as proxy, record_user(store=True) as user_id, Reconnected.objects().watch() as stream:
            old = stream.live_id
            proxy.cut()
            await _resubscribed(stream, old)

            # Written as root on a separate connection: the live query must still filter by
            # the record user's permission, so only the owned record is reported.
            await _direct_query(f"CREATE {TABLE}:foreign SET owner = {OWNED_USERS}:nobody;")
            await _direct_query(f"CREATE {TABLE}:mine SET owner = <record> '{user_id}';")

            envelope = await _next_within(stream)
            assert str(envelope["record"].id) == "mine"

    @pytest.mark.asyncio
    async def test_an_unstored_record_identity_is_not_resubscribed_as_root(self) -> None:
        from surreal_orm_lite.exceptions import SurrealDbAuthenticationError

        async with proxied() as proxy, record_user(store=False):
            async with Reconnected.objects().watch() as stream:
                proxy.cut()
                with pytest.raises(SurrealDbAuthenticationError):
                    await _next_within(stream)
                assert stream.is_active is False
            await asyncio.sleep(0.2)
            assert await _direct_lives() == {}


# ==================== review of the branch — lifecycle and connection fixes ====================


@pytest.mark.usefixtures("fast_backoff")
class TestReviewLifecycleE2E:
    @pytest.mark.asyncio
    async def test_a_stream_restarted_after_stop_during_a_resubscribe_still_resubscribes(self) -> None:
        """Finding #1: the pending flag survived the cancelled task, so the next drop hung."""
        async with proxied() as proxy:
            stream = await Reconnected.objects().watch().start()
            proxy.refuse()
            proxy.cut()
            await asyncio.sleep(0.1)
            await stream.stop()
            proxy.accept()

            await stream.start()
            old = stream.live_id
            proxy.cut()
            await _resubscribed(stream, old)
            await Reconnected(id="again", name="x").save()
            envelope = await _next_within(stream)
            assert str(envelope["record"].id) == "again"
            await stream.stop()

    @pytest.mark.asyncio
    async def test_close_connection_during_the_backoff_cancels_the_resubscribe(self) -> None:
        """Finding #2: the resubscribe used to reopen a connection and leak a live query."""
        calls: list[Any] = []
        async with proxied() as proxy:
            stream = await Reconnected.objects().watch(on_reconnect=lambda o, n: calls.append(n)).start()
            proxy.refuse()
            proxy.cut()
            await asyncio.sleep(0.1)

            await SurrealDBConnectionManager.close_connection()
            proxy.accept()
            await asyncio.sleep(0.5)

            with pytest.raises(StopAsyncIteration):
                await _next_within(stream)
            assert calls == []
            assert proxy.connections == 1
            assert await _direct_lives() == {}
            await stream.stop()

    @pytest.mark.asyncio
    async def test_kill_of_the_old_uuid_during_the_backoff_cancels_the_resubscribe(self) -> None:
        async with proxied() as proxy:
            stream = await Reconnected.objects().watch().start()
            old = stream.live_id
            assert old is not None
            proxy.refuse()
            proxy.cut()
            await asyncio.sleep(0.1)

            await SurrealDBConnectionManager.kill(old)
            proxy.accept()
            await asyncio.sleep(0.5)

            with pytest.raises(StopAsyncIteration):
                await _next_within(stream)
            assert proxy.connections == 1
            assert await _direct_lives() == {}

    @pytest.mark.asyncio
    async def test_subscribing_to_a_dropped_uuid_after_the_client_was_replaced_ends_at_once(self) -> None:
        """Finding #7: the old uuid used to be registered on the new client and wait forever."""
        async with proxied() as proxy:
            stream = await Reconnected.objects().watch(auto_resubscribe=False).start()
            old = stream.live_id
            assert old is not None
            client = await SurrealDBConnectionManager.get_client()
            proxy.cut()
            await _wait_dead(client)
            await SurrealDBConnectionManager.get_client()  # replaces the dead client

            reader = SurrealDBConnectionManager.subscribe_live(old)
            with pytest.raises(StopAsyncIteration):
                await _next_within(reader, timeout=1.0)
            await stream.stop()


@pytest.mark.usefixtures("fast_backoff")
class TestReviewIdentityE2E:
    @pytest.mark.asyncio
    async def test_after_clear_session_and_a_reconnect_new_streams_resubscribe(self) -> None:
        """Finding #4a: clear_session() used to make every later stream refuse to resubscribe."""
        async with proxied() as proxy, record_user(store=True):
            SurrealDBConnectionManager.clear_session()
            await SurrealDBConnectionManager.reconnect()  # now really the configured identity
            async with Reconnected.objects().watch() as stream:
                old = stream.live_id
                proxy.cut()
                await _resubscribed(stream, old)

    @pytest.mark.asyncio
    async def test_set_connection_to_another_database_refuses_to_resubscribe(self) -> None:
        """Finding #3a: a new connection would now open the live query somewhere else."""
        from surreal_orm_lite.exceptions import SurrealDbAuthenticationError

        async with proxied() as proxy, Reconnected.objects().watch() as stream:
            SurrealDBConnectionManager.set_connection(
                url=proxy.url(), user="root", password="root", namespace="ns", database="elsewhere"
            )
            proxy.cut()
            with pytest.raises(SurrealDbAuthenticationError):
                await _next_within(stream)
            assert stream.is_active is False


class TestOpenClientFailuresE2E:
    @pytest.mark.asyncio
    async def test_rejected_configured_credentials_are_not_retryable(self) -> None:
        """Finding #6a: a server that answered "no" answers the same on the next attempt."""
        from surreal_orm_lite.live import is_connection_failure

        SurrealDBConnectionManager.set_connection(
            url=_direct_url(), user="root", password="wrong-password", namespace="ns", database="db"
        )
        try:
            with pytest.raises(SurrealDbConnectionError) as excinfo:
                await SurrealDBConnectionManager.get_client()
        finally:
            SurrealDBConnectionManager.set_connection(
                url=_direct_url(), user="root", password="root", namespace="ns", database="db"
            )
        assert str(excinfo.value) == "Can't connect to the database."
        assert is_connection_failure(excinfo.value, None) is False

    @pytest.mark.asyncio
    async def test_an_unreachable_server_is_retryable(self) -> None:
        from surreal_orm_lite.live import is_connection_failure

        async with CuttableProxy() as proxy:
            proxy.refuse()
            SurrealDBConnectionManager.set_connection(
                url=proxy.url(), user="root", password="root", namespace="ns", database="db"
            )
            with pytest.raises(SurrealDbConnectionError) as excinfo:
                await SurrealDBConnectionManager.get_client()
        assert is_connection_failure(excinfo.value, None) is True

    @pytest.mark.asyncio
    async def test_a_cancelled_open_closes_its_socket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Finding #8: a stop() landing inside the open used to leak the connected socket."""
        from surreal_orm_lite import connection_manager

        closed: list[bool] = []

        class _SlowSurreal:
            def __init__(self, url: str) -> None:
                pass

            async def connect(self, url: str) -> None:
                pass

            async def signin(self, payload: Any) -> None:
                await asyncio.sleep(10)

            async def close(self) -> None:
                closed.append(True)

        monkeypatch.setattr(connection_manager, "AsyncSurreal", _SlowSurreal)
        SurrealDBConnectionManager.set_connection(
            url="ws://unused.invalid/rpc", user="root", password="root", namespace="ns", database="db"
        )
        task = asyncio.ensure_future(SurrealDBConnectionManager.get_client())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == [True]

    def test_transport_errors_are_matched_by_module(self) -> None:
        """Finding #6b: aiohttp's ServerDisconnectedError is not an OSError."""
        from surreal_orm_lite.live import is_connection_failure

        base = type("ClientConnectionError", (Exception,), {"__module__": "aiohttp.client_exceptions"})
        disconnected = type("ServerDisconnectedError", (base,), {"__module__": "aiohttp.client_exceptions"})
        response = type("ClientResponseError", (Exception,), {"__module__": "aiohttp.client_exceptions"})
        assert is_connection_failure(disconnected("gone"), None)
        assert not is_connection_failure(response("400"), None)


class TestIdentityStampOnOpenUnit:
    """Security review of the review fixes: the stamp a freshly opened client gets."""

    @staticmethod
    def _fake_open(monkeypatch: pytest.MonkeyPatch, closed: list[bool]) -> None:
        class _Client:
            async def close(self) -> None:
                closed.append(True)

        async def _open(cls: Any, *, signin_as_configured: bool) -> Any:
            return _Client()

        SurrealDBConnectionManager.set_connection(
            url="ws://unused.invalid/rpc", user="root", password="root", namespace="ns", database="db"
        )
        monkeypatch.setattr(SurrealDBConnectionManager, "_open_client", classmethod(_open))

    @pytest.mark.asyncio
    async def test_a_cancelled_replay_neither_caches_nor_stamps_the_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        closed: list[bool] = []
        self._fake_open(monkeypatch, closed)

        async def _slow_replay(cls: Any, client: Any) -> None:
            await asyncio.sleep(10)

        monkeypatch.setattr(SurrealDBConnectionManager, "_replay_session", classmethod(_slow_replay))
        task = asyncio.ensure_future(SurrealDBConnectionManager.get_client())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert SurrealDBConnectionManager._cached_client() is None
        assert closed == [True]

    @pytest.mark.asyncio
    async def test_an_identity_change_during_the_open_leaves_the_stamp_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        closed: list[bool] = []
        self._fake_open(monkeypatch, closed)

        async def _replay_while_someone_signs_in(cls: Any, client: Any) -> None:
            SurrealDBConnectionManager._replay_identity_changed()  # e.g. signin() on another loop

        monkeypatch.setattr(SurrealDBConnectionManager, "_replay_session", classmethod(_replay_while_someone_signs_in))
        try:
            client = await SurrealDBConnectionManager.get_client()
            stamp = SurrealDBConnectionManager._identity_of(client)
            assert stamp is not None
            assert stamp[0] != SurrealDBConnectionManager._current_replay_epoch()
        finally:
            await SurrealDBConnectionManager.close_connection()
