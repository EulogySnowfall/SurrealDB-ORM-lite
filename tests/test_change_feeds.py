"""Tests for v0.21.0 — change feeds: ``QuerySet.changes()`` → ``ChangeModelStream``.

A change feed is SurrealDB's durable log of a table's writes (``DEFINE TABLE … CHANGEFEED``),
read with ``SHOW CHANGES FOR TABLE … SINCE …``. Measured on 2.7.0, 3.3.2 and 3.1.5: the entries
look the same on both lines, but **the cursor does not**. 3.x's ``SINCE`` takes the versionstamp
an entry carries; 2.x's takes that versionstamp ``>> 16``, so resuming a 2.x feed with the stamp
it returned silently yields nothing, forever. ``SINCE <datetime>`` is always empty on 3.x and
imprecise on 2.x. The ORM normalises all three, and these tests assert the *contract* — exact
resume, no history on ``since=None``, at-least-once at transaction granularity — on both lines.

Every wait is bounded: a feed that never yields must fail a test, not hang the suite.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from surreal_orm_lite import BaseSurrealModel, SurrealDBConnectionManager
from surreal_orm_lite.changefeed import ChangeModelStream, feed_events, next_cursor, since_sql
from surreal_orm_lite.enum import LiveAction
from surreal_orm_lite.exceptions import SurrealDbError
from surreal_orm_lite.live import is_dead

TABLE = "Fed"


class Fed(BaseSurrealModel):
    id: str
    name: str = ""


class Strict(BaseSurrealModel):
    id: str
    name: str
    age: int


def _url(scheme: str = "ws") -> str:
    host = os.environ.get("SURREALDB_HOST", "localhost")
    port = os.environ.get("SURREALDB_PORT", "8000")
    return f"{scheme}://{host}:{port}" + ("/rpc" if scheme == "ws" else "")


@contextlib.asynccontextmanager
async def feed_client(definition: str = "CHANGEFEED 1h", *, scheme: str = "ws", url: str | None = None) -> AsyncIterator[Any]:
    """Connected client with ``Fed`` recreated as ``DEFINE TABLE Fed SCHEMALESS <definition>``."""
    SurrealDBConnectionManager.set_connection(
        url=url or _url(scheme), user="root", password="root", namespace="ns", database="db"
    )
    client = await SurrealDBConnectionManager.get_client()
    await client.query(f"REMOVE TABLE IF EXISTS {TABLE};", {})
    if definition is not None:
        await client.query(f"DEFINE TABLE {TABLE} SCHEMALESS {definition};", {})
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            cleanup = await SurrealDBConnectionManager.get_client()
            await cleanup.query(f"REMOVE TABLE IF EXISTS {TABLE};", {})
        await SurrealDBConnectionManager.close_connection()


async def _write(operation: Any) -> Any:
    """Run a test write, retrying SurrealDB's retryable conflicts.

    Seen once in CI on a fresh 2.7.0 container: a plain write to a change-feed table failed with
    "read or write conflict … can be retried", with no concurrent writer in the test (never
    reproduced locally). The ORM's answer to a retryable conflict is ``retry_on_conflict``.
    """
    from surreal_orm_lite import retry_on_conflict

    @retry_on_conflict(max_retries=5)
    async def _run() -> Any:
        return await operation()

    return await _run()


async def _take(stream: Any, count: int, timeout: float = 5.0) -> list[Any]:
    events: list[Any] = []
    async with asyncio.timeout(timeout):
        while len(events) < count:
            events.append(await anext(stream))
    return events


async def _nothing_within(stream: Any, seconds: float = 0.5) -> None:
    """Assert the stream yields nothing for *seconds* (a cancelled read loses nothing)."""
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(seconds):
            event = await anext(stream)
            pytest.fail(f"unexpected event {event.action} {event.record_id}")


def _ids(events: list[Any]) -> list[str]:
    return [e.record_id.split(":", 1)[1] for e in events]


async def _major(client: Any) -> int:
    return int((await client.version()).rsplit("-", 1)[-1].split(".")[0])


# ==================== unit ====================


class TestCursorUnit:
    def test_the_next_cursor_depends_on_the_server_line(self) -> None:
        # 2.x stamps are counter << 16 and SINCE takes the counter; 3.x SINCE takes the stamp.
        assert next_cursor(196608, major=2) == 4
        assert next_cursor(117410827676221440, major=3) == 117410827676221441

    def test_since_sql_for_an_int_is_the_int(self) -> None:
        assert since_sql(42, major=3) == "42"
        assert since_sql(42, major=2) == "42"

    def test_since_sql_for_a_datetime_on_3x_is_an_exact_versionstamp(self) -> None:
        moment = datetime(2026, 10, 9, 11, 57, 33, 165000, tzinfo=UTC)
        epoch_ms = (moment - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)
        assert since_sql(moment, major=3) == str(epoch_ms << 16)

    def test_since_sql_for_a_datetime_on_2x_is_a_datetime_literal(self) -> None:
        moment = datetime(2026, 10, 9, 11, 57, 33, tzinfo=UTC)
        assert since_sql(moment, major=2) == "d'2026-10-09T11:57:33Z'"


class TestArgumentValidationUnit:
    @pytest.mark.parametrize("since", [True, -1, 1.5, object(), b"0"])
    def test_rejects_an_unusable_since_type(self, since: Any) -> None:
        with pytest.raises((TypeError, ValueError)):
            Fed.objects().changes(since=since)

    def test_rejects_an_unparseable_since_string(self) -> None:
        with pytest.raises(ValueError, match="ISO"):
            Fed.objects().changes(since="yesterday")

    def test_accepts_the_three_since_shapes(self) -> None:
        assert Fed.objects().changes(since=0).cursor is None  # nothing happens before the first poll
        Fed.objects().changes(since=datetime(2026, 1, 1))  # naive means UTC
        Fed.objects().changes(since="2026-01-01T00:00:00Z")

    def test_rejects_a_batch_size_below_one(self) -> None:
        with pytest.raises(ValueError, match="batch_size"):
            Fed.objects().changes(batch_size=0)

    def test_rejects_a_negative_poll_interval(self) -> None:
        with pytest.raises(ValueError, match="poll_interval"):
            Fed.objects().changes(poll_interval=-1)

    def test_returns_a_change_model_stream_for_the_table(self) -> None:
        stream = Fed.objects().changes()
        assert isinstance(stream, ChangeModelStream)
        assert stream.table == TABLE


class TestRefusedClausesUnit:
    """``SHOW CHANGES`` has no WHERE: a filtered queryset would silently stream the whole table."""

    @pytest.mark.parametrize(
        ("label", "build"),
        [
            ("filter()", lambda qs: qs.filter(name="x")),
            ("variables()", lambda qs: qs.variables(x=1)),
            ("select()", lambda qs: qs.select("name")),
            ("limit()", lambda qs: qs.limit(1)),
            ("offset()", lambda qs: qs.offset(1)),
            ("order_by()", lambda qs: qs.order_by("name")),
            ("values()", lambda qs: qs.values("name")),
            ("fetch()", lambda qs: qs.fetch("name")),
        ],
    )
    def test_refuses(self, label: str, build: Any) -> None:
        with pytest.raises(SurrealDbError) as excinfo:
            build(Fed.objects()).changes()
        assert label in str(excinfo.value)

    def test_refuses_annotate_and_a_transaction(self) -> None:
        from surreal_orm_lite import Count

        with pytest.raises(SurrealDbError, match=r"annotate\(\)"):
            Fed.objects().annotate(total=Count("id")).changes()
        with pytest.raises(SurrealDbError, match=r"objects\(tx=\)"):
            Fed.objects(tx=object()).changes()  # type: ignore[arg-type]

    def test_names_every_offending_clause_at_once(self) -> None:
        with pytest.raises(SurrealDbError) as excinfo:
            Fed.objects().filter(name="x").limit(2).changes()
        assert "filter()" in str(excinfo.value)
        assert "limit()" in str(excinfo.value)


class TestFeedEventsUnit:
    """Entry shapes copied from the measured output of 2.7.0, 3.3.2 and 3.1.5."""

    @staticmethod
    def _rid(key: str) -> Any:
        from surreal_orm_lite._sdk import RecordID

        return RecordID(TABLE, key)

    def test_update_and_delete(self) -> None:
        entry = {
            "versionstamp": 7,
            "changes": [
                {"update": {"id": self._rid("a"), "name": "x"}},
                {"delete": {"id": self._rid("b")}},
            ],
        }
        upd, dele = feed_events(Fed, entry)
        assert upd.action == LiveAction.UPDATE
        assert upd.instance == Fed(id="a", name="x")
        assert upd.record_id == f"{TABLE}:a"
        assert upd.raw == {"id": self._rid("a"), "name": "x"}
        assert dele.action == LiveAction.DELETE
        assert dele.instance.id == "b"
        assert dele.record_id == f"{TABLE}:b"

    def test_a_create_is_reported_as_an_update(self) -> None:
        """A change feed does not record the kind of write; there is no `create` key."""
        [event] = feed_events(Fed, {"versionstamp": 1, "changes": [{"update": {"id": self._rid("n")}}]})
        assert event.action == LiveAction.UPDATE

    def test_include_original_update_reads_current_and_the_patch_paths(self) -> None:
        item = {
            "current": {"id": self._rid("a"), "name": "new"},
            "update": [{"op": "replace", "path": "/name", "value": "old"}],
        }
        [event] = feed_events(Fed, {"versionstamp": 1, "changes": [item]})
        assert event.action == LiveAction.UPDATE
        assert event.instance.name == "new"
        assert event.changed_fields == ["name"]
        assert event.raw == item

    def test_include_original_delete_with_the_original_record(self) -> None:
        """3.3.x carries `original` on a delete; 2.x and 3.1.x do not."""
        original = {"id": self._rid("a"), "name": "last"}
        [event] = feed_events(Fed, {"versionstamp": 1, "changes": [{"delete": {"id": self._rid("a"), "original": original}}]})
        assert event.action == LiveAction.DELETE
        assert event.instance.name == "last"

    def test_definitions_are_skipped(self) -> None:
        assert feed_events(Fed, {"versionstamp": 1, "changes": [{"define_table": {"name": TABLE}}]}) == []

    def test_an_invalid_record_carries_its_validation_error(self) -> None:
        [event] = feed_events(Strict, {"versionstamp": 1, "changes": [{"update": {"id": self._rid("a")}}]})
        assert event.validation_error is not None
        assert event.instance.id == "a"

    def test_a_delete_of_a_model_with_required_fields_is_minimal(self) -> None:
        [event] = feed_events(Strict, {"versionstamp": 1, "changes": [{"delete": {"id": self._rid("a")}}]})
        assert event.instance.id == "a"
        assert event.validation_error is None


# ==================== E2E (both lines) ====================


def _fast(stream: ChangeModelStream[Any]) -> ChangeModelStream[Any]:
    stream.reconnect_delay = 0.05
    stream.reconnect_max_delay = 0.2
    return stream


class TestChangesE2E:
    @pytest.mark.asyncio
    async def test_yields_updates_and_deletes_as_model_instances(self) -> None:
        async with feed_client():
            stream = await Fed.objects().changes(poll_interval=0.05).start()
            record = Fed(id="a", name="first")
            await _write(lambda: record.save())
            await _write(lambda: record.merge(name="second"))
            await _write(lambda: record.delete())

            events = await _take(stream, 3)

            assert [e.action for e in events] == [LiveAction.UPDATE, LiveAction.UPDATE, LiveAction.DELETE]
            assert isinstance(events[0].instance, Fed)
            assert [e.instance.name for e in events[:2]] == ["first", "second"]
            assert _ids(events) == ["a", "a", "a"]

    @pytest.mark.asyncio
    async def test_since_none_replays_no_history(self) -> None:
        async with feed_client():
            await _write(lambda: Fed(id="old1").save())
            await _write(lambda: Fed(id="old2").save())
            stream = await Fed.objects().changes(poll_interval=0.05).start()
            await _write(lambda: Fed(id="new").save())

            [event] = await _take(stream, 1)
            assert _ids([event]) == ["new"]
            await _nothing_within(stream)

    @pytest.mark.asyncio
    async def test_resume_from_the_cursor_is_exact(self) -> None:
        """The headline contract — and on 2.x the one the raw versionstamp silently breaks."""
        async with feed_client():
            first = await Fed.objects().changes(poll_interval=0.05).start()
            for n in range(5):
                await _write(lambda n=n: Fed(id=f"r{n}").save())
            consumed = await _take(first, 3)
            cursor = first.cursor
            first.stop()
            assert isinstance(cursor, int)

            second = Fed.objects().changes(since=cursor, poll_interval=0.05)
            rest = await _take(second, 2)

            assert _ids(consumed) == ["r0", "r1", "r2"]
            assert _ids(rest) == ["r3", "r4"]
            await _nothing_within(second)

    @pytest.mark.asyncio
    async def test_the_cursor_moves_past_a_transaction_only_after_its_last_event(self) -> None:
        async with feed_client() as client:
            stream = await Fed.objects().changes(poll_interval=0.05).start()
            before = stream.cursor
            await _write(lambda: client.query(f"BEGIN; CREATE {TABLE}:t1; CREATE {TABLE}:t2; COMMIT;", {}))

            await _take(stream, 1)
            assert stream.cursor == before, "resuming here must re-deliver the whole transaction"
            await _take(stream, 1)
            after = stream.cursor
            assert after != before

            redelivered = Fed.objects().changes(since=before, poll_interval=0.05) if before is not None else None
            if redelivered is not None:
                assert _ids(await _take(redelivered, 2)) == ["t1", "t2"]
            resumed = Fed.objects().changes(since=after, poll_interval=0.05)
            await _nothing_within(resumed)

    @pytest.mark.asyncio
    async def test_pages_by_batch_size_without_losing_anything(self) -> None:
        async with feed_client():
            stream = await Fed.objects().changes(poll_interval=0.05, batch_size=2).start()
            for n in range(7):
                await _write(lambda n=n: Fed(id=f"p{n}").save())
            events = await _take(stream, 7)
            assert _ids(events) == [f"p{n}" for n in range(7)]

    @pytest.mark.asyncio
    async def test_since_a_datetime(self) -> None:
        """Exact on 3.x (the ORM converts it to a versionstamp); a superset on 2.x."""
        async with feed_client() as client:
            await _write(lambda: Fed(id="early").save())
            await asyncio.sleep(0.2)
            moment = await client.query("RETURN time::now();", {})
            await asyncio.sleep(0.2)
            await _write(lambda: Fed(id="late").save())

            stream = Fed.objects().changes(since=moment, poll_interval=0.05)
            seen: list[str] = []
            async with asyncio.timeout(5):
                while "late" not in seen:
                    seen.extend(_ids([await anext(stream)]))

            if await _major(client) >= 3:
                assert seen == ["late"]
            else:
                assert seen[-1] == "late"

    @pytest.mark.asyncio
    async def test_include_original_reports_the_changed_fields(self) -> None:
        async with feed_client("CHANGEFEED 1h INCLUDE ORIGINAL"):
            stream = await Fed.objects().changes(poll_interval=0.05).start()
            record = Fed(id="o", name="a")
            await _write(lambda: record.save())
            await _write(lambda: record.merge(name="b"))
            await _write(lambda: record.delete())

            created, updated, deleted = await _take(stream, 3)

            assert created.action == LiveAction.UPDATE
            assert updated.instance.name == "b"
            assert updated.changed_fields == ["name"]
            assert deleted.action == LiveAction.DELETE
            assert deleted.instance.id == "o"

    @pytest.mark.asyncio
    async def test_a_table_without_a_change_feed_is_refused(self) -> None:
        async with feed_client(""):
            with pytest.raises(SurrealDbError, match="CHANGEFEED"):
                await Fed.objects().changes().start()

    @pytest.mark.asyncio
    async def test_a_missing_table_is_refused(self) -> None:
        async with feed_client(None):  # type: ignore[arg-type]
            with pytest.raises(SurrealDbError, match="does not exist"):
                await Fed.objects().changes().start()

    @pytest.mark.asyncio
    async def test_works_over_http(self) -> None:
        async with feed_client(scheme="http"):
            stream = await Fed.objects().changes(poll_interval=0.05).start()
            await _write(lambda: Fed(id="h").save())
            [event] = await _take(stream, 1)
            assert _ids([event]) == ["h"]

    @pytest.mark.asyncio
    async def test_stop_ends_a_pending_iteration(self) -> None:
        async with feed_client():
            stream = await Fed.objects().changes(poll_interval=0.05).start()
            pending = asyncio.ensure_future(anext(stream))
            await asyncio.sleep(0.1)
            stream.stop()
            with pytest.raises(StopAsyncIteration):
                async with asyncio.timeout(2):
                    await pending

    @pytest.mark.asyncio
    async def test_start_is_idempotent_and_iteration_starts_lazily(self) -> None:
        async with feed_client():
            stream = Fed.objects().changes(poll_interval=0.05)
            assert await stream.start() is stream
            cursor = stream.cursor
            assert await stream.start() is stream
            assert stream.cursor == cursor

            lazy = Fed.objects().changes(poll_interval=0.05)
            await _write(lambda: Fed(id="x").save())
            await _nothing_within(lazy)  # its "now" is the first read, after the write


class TestChangesReconnectE2E:
    @pytest.mark.asyncio
    async def test_a_drop_while_polling_loses_nothing_and_duplicates_nothing(self) -> None:
        from tests._proxy import CuttableProxy

        async with CuttableProxy() as proxy, feed_client(url=proxy.url()):
            stream = _fast(await Fed.objects().changes(poll_interval=0.05).start())
            await _write(lambda: Fed(id="d0").save())
            await _write(lambda: Fed(id="d1").save())
            before = await _take(stream, 2)

            client = await SurrealDBConnectionManager.get_client()
            proxy.cut()
            # Let the SDK notice the drop first: a write already in flight when the socket goes
            # fails (the SDK's KeyError), and retrying ordinary queries is not part of v0.21.0.
            async with asyncio.timeout(2):
                while not is_dead(client):
                    await asyncio.sleep(0.005)
            for n in range(2, 5):
                await _write(lambda n=n: Fed(id=f"d{n}").save())
            after = await _take(stream, 3)

            assert _ids(before + after) == [f"d{n}" for n in range(5)]
            assert proxy.connections >= 2

    @pytest.mark.asyncio
    async def test_gives_up_after_the_last_attempt(self) -> None:
        from surreal_orm_lite.exceptions import SurrealDbConnectionError
        from tests._proxy import CuttableProxy

        async with CuttableProxy() as proxy, feed_client(url=proxy.url()):
            stream = _fast(await Fed.objects().changes(poll_interval=0.05).start())
            stream.reconnect_max_attempts = 2
            proxy.refuse()
            proxy.cut()
            with pytest.raises(SurrealDbConnectionError, match="2 attempts"):
                async with asyncio.timeout(5):
                    await anext(stream)
            proxy.accept()


class TestExports:
    def test_v0_21_symbols_are_public(self) -> None:
        import surreal_orm_lite as orm

        for name in ("ChangeModelStream", "ReconnectCallback"):
            assert name in orm.__all__
            assert hasattr(orm, name)
        assert orm.ChangeModelStream is ChangeModelStream


async def _direct_write(sql: str) -> None:
    """A write on a separate, direct connection — not through the stream's proxy."""
    from surrealdb import AsyncSurreal

    client = AsyncSurreal(_url())
    await client.connect(_url())
    try:
        await client.signin({"username": "root", "password": "root"})
        await client.use("ns", "db")
        await client.query(sql, {})
    finally:
        await client.close()


class TestReviewChangeFeedE2E:
    @pytest.mark.asyncio
    async def test_a_database_level_change_feed_is_accepted(self) -> None:
        """Finding #5: DEFINE DATABASE … CHANGEFEED covers every table, without a table clause."""
        database = "cf_dbfeed"
        SurrealDBConnectionManager.set_connection(url=_url(), user="root", password="root", namespace="ns", database=database)
        client = await SurrealDBConnectionManager.get_client()
        try:
            await client.query(f"DEFINE DATABASE OVERWRITE {database} CHANGEFEED 1h;", {})
            await client.query(f"DEFINE TABLE OVERWRITE {TABLE} SCHEMALESS;", {})
            stream = await Fed.objects().changes(poll_interval=0.05).start()
            await _write(lambda: Fed(id="dbf").save())
            [event] = await _take(stream, 1)
            assert _ids([event]) == ["dbf"]
        finally:
            with contextlib.suppress(Exception):
                await client.query(f"REMOVE DATABASE {database};", {})
            await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_the_word_changefeed_in_a_comment_is_not_a_change_feed(self) -> None:
        async with feed_client("COMMENT 'no changefeed here'"):
            with pytest.raises(SurrealDbError, match="no change feed"):
                await Fed.objects().changes().start()

    @pytest.mark.asyncio
    async def test_stop_ends_a_read_waiting_in_the_retry_backoff(self) -> None:
        """Finding #10: stop() used to wait out the backoff, up to 30 s."""
        from tests._proxy import CuttableProxy

        async with CuttableProxy() as proxy, feed_client(url=proxy.url()):
            stream = await Fed.objects().changes(poll_interval=0.05).start()
            stream.reconnect_delay = 10.0
            proxy.refuse()
            proxy.cut()
            pending = asyncio.ensure_future(anext(stream))
            await asyncio.sleep(0.3)
            stream.stop()
            with pytest.raises(StopAsyncIteration):
                async with asyncio.timeout(1):
                    await pending
            proxy.accept()

    @pytest.mark.asyncio
    async def test_a_drop_over_http_is_retried(self) -> None:
        """Finding #6b: aiohttp's ServerDisconnectedError is not an OSError, yet a lost connection."""
        from tests._proxy import CuttableProxy

        async with CuttableProxy() as proxy, feed_client(url=proxy.url("http")):
            stream = _fast(await Fed.objects().changes(poll_interval=0.05).start())
            await _direct_write(f"CREATE {TABLE}:h0;")
            first = await _take(stream, 1)

            proxy.cut()  # the pooled keep-alive connection dies under the next poll
            await _direct_write(f"CREATE {TABLE}:h1;")
            await _direct_write(f"CREATE {TABLE}:h2;")
            rest = await _take(stream, 2)

            assert _ids(first + rest) == ["h0", "h1", "h2"]
