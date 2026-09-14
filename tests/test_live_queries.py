"""Tests for v0.19.0 — live queries (base): ``live()``, ``subscribe_live()``, ``kill()``.

Live queries work on **both** supported SurrealDB lines, so almost nothing here is
version-gated. The two measured divergences each get both branches, probed at runtime:

* a live query on a table that does not exist raises on 3.x and succeeds on 2.6.x;
* after ``kill()``, 3.x sends a ``KILLED`` notification and 2.6.x sends nothing — the ORM
  makes both terminate the stream, and the tests assert termination, not the mechanism.

Every test that consumes a stream does so inside ``asyncio.wait_for``: a live query that never
terminates would otherwise hang the whole suite instead of failing one test.
"""

import asyncio
from enum import StrEnum
from typing import Any

import pytest

from surreal_orm_lite import BaseSurrealModel, SurrealDBConnectionManager
from surreal_orm_lite.enum import LiveAction
from surreal_orm_lite.exceptions import SurrealDbError, SurrealDbNotFoundError
from tests.conftest import orm_client


class _FakeClient:
    """Stand-in for the SDK connection: only ``live_queues`` matters to the registry."""

    def __init__(self) -> None:
        self.live_queues: dict[str, list[asyncio.Queue[Any]]] = {}


class Watched(BaseSurrealModel):
    id: str
    name: str = ""


# ==================== Task 1 — LiveAction ====================


class TestLiveAction:
    def test_is_a_str_enum(self) -> None:
        assert issubclass(LiveAction, StrEnum)

    def test_members_equal_the_raw_server_strings(self) -> None:
        assert LiveAction.CREATE == "CREATE"
        assert LiveAction.UPDATE == "UPDATE"
        assert LiveAction.DELETE == "DELETE"

    def test_carries_the_3x_killed_action(self) -> None:
        assert LiveAction.KILLED == "KILLED"


# ==================== Task 1 — subscriber registry ====================


class TestSubscriberRegistry:
    """The registry is keyed by event loop, so every test here runs inside one."""

    @pytest.mark.asyncio
    async def test_register_returns_a_queue_visible_to_both_registries(self) -> None:
        from surreal_orm_lite import live

        client = _FakeClient()
        queue = live.register_subscriber(client, "abc")
        assert queue in client.live_queues["abc"]
        assert queue in live._bucket()["abc"]
        live.close_subscribers("abc")

    @pytest.mark.asyncio
    async def test_close_pushes_the_sentinel_to_every_queue(self) -> None:
        from surreal_orm_lite import live

        client = _FakeClient()
        first = live.register_subscriber(client, "abc")
        second = live.register_subscriber(client, "abc")
        live.close_subscribers("abc")
        assert first.get_nowait() is live._STREAM_END
        assert second.get_nowait() is live._STREAM_END

    @pytest.mark.asyncio
    async def test_close_drops_the_entry_and_is_idempotent(self) -> None:
        from surreal_orm_lite import live

        client = _FakeClient()
        live.register_subscriber(client, "abc")
        live.close_subscribers("abc")
        assert "abc" not in live._bucket()
        live.close_subscribers("abc")  # must not raise

    @pytest.mark.asyncio
    async def test_unregister_leaves_the_other_subscribers_intact(self) -> None:
        from surreal_orm_lite import live

        client = _FakeClient()
        first = live.register_subscriber(client, "abc")
        second = live.register_subscriber(client, "abc")
        live.unregister_subscriber(client, "abc", first)
        assert live._bucket()["abc"] == [second]
        assert client.live_queues["abc"] == [second]
        live.close_subscribers("abc")

    @pytest.mark.asyncio
    async def test_unregister_of_the_last_queue_drops_the_entry(self) -> None:
        from surreal_orm_lite import live

        client = _FakeClient()
        only = live.register_subscriber(client, "abc")
        live.unregister_subscriber(client, "abc", only)
        assert "abc" not in live._bucket()

    @pytest.mark.asyncio
    async def test_accepts_a_uuid_as_well_as_a_string(self) -> None:
        from uuid import uuid4

        from surreal_orm_lite import live

        client = _FakeClient()
        key = uuid4()
        queue = live.register_subscriber(client, key)
        assert queue in client.live_queues[str(key)]
        live.close_subscribers(key)
        assert str(key) not in live._bucket()

    @pytest.mark.asyncio
    async def test_close_all_ends_every_stream_on_this_loop(self) -> None:
        from surreal_orm_lite import live

        client = _FakeClient()
        first = live.register_subscriber(client, "one")
        second = live.register_subscriber(client, "two")
        live.close_all_subscribers()
        assert first.get_nowait() is live._STREAM_END
        assert second.get_nowait() is live._STREAM_END
        assert live._bucket() == {}

    @pytest.mark.asyncio
    async def test_close_all_on_an_empty_registry_is_a_no_op(self) -> None:
        from surreal_orm_lite import live

        live.close_all_subscribers()
        assert live._bucket() == {}

    @pytest.mark.asyncio
    async def test_registry_is_partitioned_by_event_loop(self) -> None:
        """A queue belongs to the loop that awaits it, so one loop must not see another's."""
        from surreal_orm_lite import live

        client = _FakeClient()
        live.register_subscriber(client, "mine")
        assert "mine" in live._bucket()

        # A separate thread, because a loop cannot be run from inside a running one.
        assert "mine" not in _in_another_loop(_bucket_keys)
        live.close_subscribers("mine")

    @pytest.mark.asyncio
    async def test_closed_loops_are_pruned(self) -> None:
        from surreal_orm_lite import live

        _in_another_loop(_register_and_leave)
        assert any(loop.is_closed() for loop in live._SUBSCRIBERS)
        live._prune_dead_loops()
        assert all(not loop.is_closed() for loop in live._SUBSCRIBERS)


def _in_another_loop(coro_factory: Any) -> Any:
    """Run *coro_factory* to completion on a brand-new loop in a separate thread."""
    import asyncio as _asyncio
    import threading

    box: dict[str, Any] = {}

    def _run() -> None:
        loop = _asyncio.new_event_loop()
        try:
            box["value"] = loop.run_until_complete(coro_factory())
        finally:
            loop.close()

    thread = threading.Thread(target=_run)
    thread.start()
    thread.join(timeout=10)
    return box.get("value")


async def _bucket_keys() -> list[str]:
    from surreal_orm_lite import live

    return list(live._bucket())


async def _register_and_leave() -> None:
    from surreal_orm_lite import live

    live.register_subscriber(_FakeClient(), "left-behind")


class TestOpenStreamGuards:
    @pytest.mark.asyncio
    async def test_refuses_an_sdk_connection_without_live_queues(self) -> None:
        """Degrading would silently drop `action`; a wrong answer is worse than an error."""
        from surreal_orm_lite import live

        with pytest.raises(SurrealDbError, match="live_queues"):
            live.open_stream(object(), "abc")

    @pytest.mark.asyncio
    async def test_subscribing_to_an_already_killed_uuid_ends_at_once(self) -> None:
        """Nothing can ever wake such a stream, so it must not park on the queue."""
        from surreal_orm_lite import live

        client = _FakeClient()
        live.register_subscriber(client, "gone")
        live.close_subscribers("gone")

        stream = live.open_stream(client, "gone")
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(stream.__anext__(), timeout=5)


@pytest.fixture(autouse=True)
def _clear_registry() -> Any:
    from surreal_orm_lite import live

    yield
    live._SUBSCRIBERS.clear()
    live._RECENTLY_KILLED.clear()


# ==================== E2E helpers ====================

TABLE = "Watched"


async def _define_table(client: Any, table: str = TABLE) -> None:
    """SurrealDB 3.x refuses a live query on an undefined table, so E2E tests define it."""
    await client.query(f"DEFINE TABLE {table} SCHEMALESS;", {})


async def _collect(stream: Any, count: int, timeout: float = 10.0) -> list[dict[str, Any]]:
    """Read ``count`` envelopes, failing the test rather than hanging if they never arrive."""

    async def _read() -> list[dict[str, Any]]:
        seen: list[dict[str, Any]] = []
        async for envelope in stream:
            seen.append(envelope)
            if len(seen) >= count:
                break
        return seen

    return await asyncio.wait_for(_read(), timeout=timeout)


async def _churn(client: Any, table: str = TABLE) -> None:
    """One create, one update and one delete — the three actions a live query reports."""
    created = await client.query(f"CREATE {table}:one SET name = 'a';", {})
    assert created
    await client.query(f"UPDATE {table}:one SET name = 'b';", {})
    await client.query(f"DELETE {table}:one;", {})


# ==================== Task 2 — subscribe_live() and kill() ====================


class TestSubscribeLiveE2E:
    @pytest.mark.asyncio
    async def test_reports_create_update_and_delete(self) -> None:
        async with orm_client(TABLE) as client:
            await _define_table(client)
            live_id = await Watched.objects().live()
            stream = SurrealDBConnectionManager.subscribe_live(live_id)
            try:
                await _churn(client)
                envelopes = await _collect(stream, 3)
            finally:
                await SurrealDBConnectionManager.kill(live_id)

        assert [e["action"] for e in envelopes] == [
            LiveAction.CREATE,
            LiveAction.UPDATE,
            LiveAction.DELETE,
        ]
        assert all(e["record"] is not None for e in envelopes)
        assert envelopes[0]["result"]["name"] == "a"
        assert envelopes[1]["result"]["name"] == "b"

    @pytest.mark.asyncio
    async def test_buffers_notifications_emitted_before_the_first_read(self) -> None:
        """A write between subscribe_live() and the first iteration must not be lost."""
        async with orm_client(TABLE) as client:
            await _define_table(client)
            live_id = await Watched.objects().live()
            stream = SurrealDBConnectionManager.subscribe_live(live_id)
            try:
                await client.query(f"CREATE {TABLE}:early SET name = 'early';", {})
                await asyncio.sleep(0.2)
                envelopes = await _collect(stream, 1)
            finally:
                await SurrealDBConnectionManager.kill(live_id)

        assert envelopes[0]["result"]["name"] == "early"

    @pytest.mark.asyncio
    async def test_kill_terminates_an_in_flight_stream(self) -> None:
        """The crux of the dual-line contract: 3.x sends KILLED, 2.6.x sends nothing."""
        async with orm_client(TABLE) as client:
            await _define_table(client)
            live_id = await Watched.objects().live()
            stream = SurrealDBConnectionManager.subscribe_live(live_id)

            async def _drain_to_exhaustion() -> int:
                seen = 0
                async for _ in stream:
                    seen += 1
                return seen

            consumer = asyncio.create_task(_drain_to_exhaustion())
            await asyncio.sleep(0.2)
            await SurrealDBConnectionManager.kill(live_id)
            # No timeout here would mean a hang; wait_for turns it into a failure.
            assert await asyncio.wait_for(consumer, timeout=10) == 0

    @pytest.mark.asyncio
    async def test_no_notification_arrives_after_kill(self) -> None:
        async with orm_client(TABLE) as client:
            await _define_table(client)
            live_id = await Watched.objects().live()
            stream = SurrealDBConnectionManager.subscribe_live(live_id)
            await SurrealDBConnectionManager.kill(live_id)
            await client.query(f"CREATE {TABLE}:after SET name = 'after';", {})
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(stream.__anext__(), timeout=10)

    @pytest.mark.asyncio
    async def test_kill_of_an_unknown_uuid_is_a_no_op(self) -> None:
        from uuid import uuid4

        async with orm_client(TABLE):
            await SurrealDBConnectionManager.kill(uuid4())

    @pytest.mark.asyncio
    async def test_kill_is_idempotent(self) -> None:
        async with orm_client(TABLE) as client:
            await _define_table(client)
            live_id = await Watched.objects().live()
            await SurrealDBConnectionManager.kill(live_id)
            await SurrealDBConnectionManager.kill(live_id)

    @pytest.mark.asyncio
    async def test_two_readers_both_receive_every_notification(self) -> None:
        async with orm_client(TABLE) as client:
            await _define_table(client)
            live_id = await Watched.objects().live()
            first = SurrealDBConnectionManager.subscribe_live(live_id)
            second = SurrealDBConnectionManager.subscribe_live(live_id)
            try:
                await client.query(f"CREATE {TABLE}:shared SET name = 'shared';", {})
                both = await asyncio.gather(_collect(first, 1), _collect(second, 1))
            finally:
                await SurrealDBConnectionManager.kill(live_id)

        assert both[0][0]["action"] == LiveAction.CREATE
        assert both[1][0]["action"] == LiveAction.CREATE


# ==================== Task 3 — live() guards ====================


class TestLiveClauseGuards:
    """A clause a table-level live query cannot honour must be refused, never ignored."""

    @pytest.mark.parametrize(
        ("label", "build"),
        [
            ("filter()", lambda qs: qs.filter(name="a")),
            ("select()", lambda qs: qs.select("name")),
            ("limit()", lambda qs: qs.limit(1)),
            ("offset()", lambda qs: qs.offset(1)),
            ("order_by()", lambda qs: qs.order_by("name")),
            ("fetch()", lambda qs: qs.fetch("name")),
            ("values()", lambda qs: qs.values("name")),
        ],
    )
    @pytest.mark.asyncio
    async def test_rejects_unsupported_clause(self, label: str, build: Any) -> None:
        with pytest.raises(SurrealDbError) as excinfo:
            await build(Watched.objects()).live()
        assert label in str(excinfo.value)
        assert "v0.20.0" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_rejects_annotate(self) -> None:
        from surreal_orm_lite import Count

        with pytest.raises(SurrealDbError, match=r"annotate\(\)"):
            await Watched.objects().annotate(total=Count("id")).live()

    @pytest.mark.asyncio
    async def test_names_every_offending_clause_at_once(self) -> None:
        """A caller who chained three unusable clauses should not have to fix them one by one."""
        with pytest.raises(SurrealDbError) as excinfo:
            await Watched.objects().filter(name="a").limit(2).order_by("name").live()
        message = str(excinfo.value)
        assert "filter()" in message
        assert "limit()" in message
        assert "order_by()" in message

    @pytest.mark.asyncio
    async def test_rejects_a_q_filter(self) -> None:
        from surreal_orm_lite import Q

        with pytest.raises(SurrealDbError, match="filter"):
            await Watched.objects().filter(Q(name="a")).live()

    def test_watch_rejects_the_same_clauses(self) -> None:
        with pytest.raises(SurrealDbError, match="filter"):
            Watched.objects().filter(name="a").watch()

    @pytest.mark.asyncio
    async def test_rejects_a_non_websocket_connection(self) -> None:
        """The SDK would raise a bare NotImplementedError; the ORM explains the requirement."""
        SurrealDBConnectionManager.set_connection(
            url="http://localhost:8000", user="root", password="root", namespace="ns", database="db"
        )
        try:
            with pytest.raises(SurrealDbError) as excinfo:
                await Watched.objects().live()
        finally:
            await SurrealDBConnectionManager.close_connection()
        message = str(excinfo.value)
        assert "WebSocket" in message
        assert "http://localhost:8000" in message


class TestLiveE2E:
    @pytest.mark.asyncio
    async def test_returns_a_uuid(self) -> None:
        from uuid import UUID

        async with orm_client(TABLE) as client:
            await _define_table(client)
            live_id = await Watched.objects().live()
            try:
                assert isinstance(live_id, UUID)
            finally:
                await SurrealDBConnectionManager.kill(live_id)

    @pytest.mark.asyncio
    async def test_missing_table_diverges_by_server_line(self) -> None:
        """3.x refuses to watch an undefined table; 2.6.x accepts and stays silent.

        Both branches are asserted from one test so neither line can quietly stop being
        covered: whichever server is under test, something is checked.
        """
        async with orm_client(TABLE):
            try:
                live_id = await Watched.objects().live()
            except SurrealDbNotFoundError as exc:
                # SurrealDB 3.x
                assert TABLE in str(exc)
                assert "DEFINE TABLE" in str(exc)
            else:
                # SurrealDB 2.6.x
                assert live_id is not None
                await SurrealDBConnectionManager.kill(live_id)


# ==================== Task 4 — LiveStream / watch() ====================


class TestLiveStreamUnit:
    def test_is_inactive_before_start(self) -> None:
        stream = Watched.objects().watch()
        assert stream.is_active is False
        assert stream.live_id is None
        assert stream.table == TABLE

    @pytest.mark.asyncio
    async def test_stop_before_start_is_safe(self) -> None:
        await Watched.objects().watch().stop()

    @pytest.mark.asyncio
    async def test_iterating_before_start_ends_immediately(self) -> None:
        with pytest.raises(StopAsyncIteration):
            await Watched.objects().watch().__anext__()


class TestWatchE2E:
    @pytest.mark.asyncio
    async def test_yields_envelopes_and_exposes_the_handle(self) -> None:
        async with orm_client(TABLE) as client:
            await _define_table(client)
            async with Watched.objects().watch() as stream:
                assert stream.is_active is True
                assert stream.live_id is not None
                await client.query(f"CREATE {TABLE}:w SET name = 'w';", {})
                envelopes = await _collect(stream, 1)
            assert stream.is_active is False
            assert stream.live_id is None

        assert envelopes[0]["action"] == LiveAction.CREATE
        assert envelopes[0]["result"]["name"] == "w"

    @pytest.mark.asyncio
    async def test_kills_the_subscription_on_exit(self) -> None:
        async with orm_client(TABLE) as client:
            await _define_table(client)
            async with Watched.objects().watch() as stream:
                live_id = stream.live_id
            assert live_id is not None
            # Already dead: a second kill is the documented no-op, and nothing is left reading.
            await SurrealDBConnectionManager.kill(live_id)
            assert str(live_id) not in _subscribers()

    @pytest.mark.asyncio
    async def test_kills_the_subscription_when_the_body_raises(self) -> None:
        async with orm_client(TABLE) as client:
            await _define_table(client)
            stream = Watched.objects().watch()
            with pytest.raises(RuntimeError):
                async with stream:
                    raise RuntimeError("boom")
            assert stream.is_active is False

    @pytest.mark.asyncio
    async def test_double_start_is_a_no_op(self) -> None:
        async with orm_client(TABLE) as client:
            await _define_table(client)
            stream = Watched.objects().watch()
            async with stream:
                first = stream.live_id
                await stream.start()
                assert stream.live_id == first


def _subscribers() -> dict[str, Any]:
    from surreal_orm_lite import live

    return live._SUBSCRIBERS


# ==================== Task 5 — SDK coupling guard and exports ====================


class TestSdkCoupling:
    @pytest.mark.asyncio
    async def test_connection_still_exposes_live_queues(self) -> None:
        """The ORM reads the envelope out of this registry because the SDK's own
        ``subscribe_live()`` discards the action. If a future SDK drops the attribute, the
        ORM degrades silently — this test makes that change loud instead."""
        from surreal_orm_lite.live import _sdk_queues

        async with orm_client(TABLE) as client:
            assert _sdk_queues(client) is not None

    @pytest.mark.asyncio
    async def test_sdk_subscribe_live_still_discards_the_action(self) -> None:
        """Documents *why* the tap exists: if the SDK ever starts yielding the envelope, this
        test fails and the ORM can drop its own registry."""
        async with orm_client(TABLE) as client:
            await _define_table(client)
            live_id = await Watched.objects().live()
            try:
                generator = await client.subscribe_live(live_id)
                await client.query(f"CREATE {TABLE}:sdk SET name = 'sdk';", {})
                payload = await asyncio.wait_for(generator.__anext__(), timeout=10)
            finally:
                await SurrealDBConnectionManager.kill(live_id)
        assert "action" not in payload
        assert payload["name"] == "sdk"


class TestExports:
    def test_live_symbols_are_public(self) -> None:
        import surreal_orm_lite

        assert surreal_orm_lite.LiveAction is LiveAction
        assert "LiveAction" in surreal_orm_lite.__all__
        assert "LiveStream" in surreal_orm_lite.__all__


# ==================== Connection teardown must release readers ====================


class TestTeardownEndsStreamsE2E:
    """A reader parked on the queue only wakes if something wakes it.

    Nothing in the SDK reports a closed WebSocket to a live-query subscriber: its receive task
    catches the close and never touches ``live_queues``. So every ORM path that takes the
    connection away has to end the streams itself, or an application's ordinary shutdown leaves
    a permanently wedged task behind and the server-side subscription is never killed.
    """

    @pytest.mark.asyncio
    async def test_close_connection_ends_an_open_stream(self) -> None:
        async with orm_client(TABLE) as client:
            await _define_table(client)
            live_id = await Watched.objects().live()
            stream = SurrealDBConnectionManager.subscribe_live(live_id)

            async def consume() -> str:
                async for _ in stream:
                    pass
                return "ended"

            consumer = asyncio.create_task(consume())
            await asyncio.sleep(0.2)
            await SurrealDBConnectionManager.close_connection()
            assert await asyncio.wait_for(consumer, timeout=10) == "ended"

    @pytest.mark.asyncio
    async def test_close_connection_releases_a_watch_block(self) -> None:
        async with orm_client(TABLE) as client:
            await _define_table(client)

            async def watch_forever() -> str:
                async with Watched.objects().watch() as stream:
                    async for _ in stream:
                        pass
                return "exited"

            watcher = asyncio.create_task(watch_forever())
            await asyncio.sleep(0.3)
            await SurrealDBConnectionManager.close_connection()
            assert await asyncio.wait_for(watcher, timeout=10) == "exited"

    @pytest.mark.asyncio
    async def test_close_all_connections_ends_an_open_stream(self) -> None:
        async with orm_client(TABLE) as client:
            await _define_table(client)
            live_id = await Watched.objects().live()
            stream = SurrealDBConnectionManager.subscribe_live(live_id)

            async def consume() -> str:
                async for _ in stream:
                    pass
                return "ended"

            consumer = asyncio.create_task(consume())
            await asyncio.sleep(0.2)
            await SurrealDBConnectionManager.close_all_connections()
            assert await asyncio.wait_for(consumer, timeout=10) == "ended"


# ==================== Error paths a mis-sequenced caller hits ====================


class TestLiveErrorPaths:
    @pytest.mark.asyncio
    async def test_subscribe_live_without_a_connection_explains_itself(self) -> None:
        await SurrealDBConnectionManager.close_connection()
        with pytest.raises(SurrealDbError) as excinfo:
            SurrealDBConnectionManager.subscribe_live("00000000-0000-0000-0000-000000000000")
        message = str(excinfo.value)
        assert "event loop" in message
        assert "live()" in message

    @pytest.mark.asyncio
    async def test_a_kill_failure_that_is_not_an_unknown_uuid_is_reported(self) -> None:
        """Only "no such live query" is swallowed; anything else must reach the caller."""

        class _AngryClient:
            live_queues: dict[str, Any] = {}

            async def kill(self, query_uuid: Any) -> None:
                raise RuntimeError("connection reset by peer")

        async with orm_client(TABLE):
            original = SurrealDBConnectionManager.get_client
            SurrealDBConnectionManager.get_client = classmethod(  # type: ignore[method-assign,assignment]
                lambda cls: asyncio.sleep(0, result=_AngryClient())
            )
            try:
                with pytest.raises(SurrealDbError, match="Failed to kill live query"):
                    await SurrealDBConnectionManager.kill("11111111-1111-1111-1111-111111111111")
            finally:
                SurrealDBConnectionManager.get_client = original  # type: ignore[method-assign]

    @pytest.mark.asyncio
    async def test_a_failed_kill_still_releases_the_readers(self) -> None:
        """The sentinel is pushed in a `finally`, so a broken connection cannot wedge a reader."""

        class _AngryClient:
            live_queues: dict[str, Any] = {}

            async def kill(self, query_uuid: Any) -> None:
                raise RuntimeError("connection reset by peer")

        from surreal_orm_lite import live

        async with orm_client(TABLE):
            queue = live.register_subscriber(_AngryClient(), "22222222-2222-2222-2222-222222222222")
            original = SurrealDBConnectionManager.get_client
            SurrealDBConnectionManager.get_client = classmethod(  # type: ignore[method-assign,assignment]
                lambda cls: asyncio.sleep(0, result=_AngryClient())
            )
            try:
                with pytest.raises(SurrealDbError):
                    await SurrealDBConnectionManager.kill("22222222-2222-2222-2222-222222222222")
            finally:
                SurrealDBConnectionManager.get_client = original  # type: ignore[method-assign]
            assert queue.get_nowait() is live._STREAM_END
