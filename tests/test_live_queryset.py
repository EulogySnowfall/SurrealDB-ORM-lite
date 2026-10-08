"""Tests for v0.20.0 — typed, filtered live queries.

Every E2E test runs on both supported lines. The headline regression is 2.x: bound parameters
never reach a live query there, so a filter that is not inlined matches nothing — the filtered
tests below fail on 2.x the moment inlining regresses. Streams are always read through
``asyncio.wait_for`` so a silent server fails a test instead of hanging the suite.
"""

import asyncio
from typing import Any

import pytest
from pydantic import Field

from surreal_orm_lite import BaseSurrealModel, Q, Var
from surreal_orm_lite.enum import LiveAction
from surreal_orm_lite.exceptions import SurrealDbError
from tests.conftest import orm_client

TABLE = "Ticket"


class Ticket(BaseSurrealModel):
    id: str
    status: str = ""
    age: int = 0
    label: str = Field(default="", alias="label_col")
    owner: Any = None


@pytest.fixture(autouse=True)
def _clear_registry() -> Any:
    from surreal_orm_lite import live

    yield
    live._SUBSCRIBERS.clear()
    live._RECENTLY_KILLED.clear()


async def _define(client: Any, *tables: str) -> None:
    for table in tables or (TABLE,):
        await client.query(f"DEFINE TABLE {table} SCHEMALESS;", {})


async def _take(stream: Any, count: int, timeout: float = 10.0) -> list[Any]:
    async def _read() -> list[Any]:
        seen: list[Any] = []
        async for item in stream:
            seen.append(item)
            if len(seen) >= count:
                break
        return seen

    return await asyncio.wait_for(_read(), timeout=timeout)


async def _assert_silent(stream: Any, wait: float = 1.0) -> None:
    """Nothing more arrives. A cancelled read leaves the stream usable (v0.19.0 contract)."""
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(stream.__anext__(), timeout=wait)


# ==================== Task 2 — compilation (unit) ====================


class TestCompileLive:
    def test_plain(self) -> None:
        assert Ticket.objects()._compile_live(False) == "LIVE SELECT * FROM Ticket;"

    def test_diff(self) -> None:
        assert Ticket.objects()._compile_live(True) == "LIVE SELECT DIFF FROM Ticket;"

    def test_filter_is_inlined(self) -> None:
        sql = Ticket.objects().filter(status="it's", age__gte=3)._compile_live(False)
        assert sql == "LIVE SELECT * FROM Ticket WHERE status = 'it\\'s' AND age >= 3;"
        assert "$" not in sql

    def test_alias_is_translated(self) -> None:
        sql = Ticket.objects().filter(label="x")._compile_live(False)
        assert "label_col = 'x'" in sql

    def test_var_takes_its_value_from_variables(self) -> None:
        sql = Ticket.objects().filter(age__gte=Var("min")).variables(min=5)._compile_live(False)
        assert sql.endswith("WHERE age >= 5;")

    def test_unbound_var_is_refused(self) -> None:
        """PR #199 review #4: unbound, ``$missing`` would read as NONE and ``age >= NONE``
        matches every record."""
        with pytest.raises(SurrealDbError, match=r"\$missing is not bound"):
            Ticket.objects().filter(age__gte=Var("missing"))._compile_live(False)

    def test_server_parameter_var_is_left_as_a_reference(self) -> None:
        sql = Ticket.objects().filter(owner=Var("auth"))._compile_live(False)
        assert sql.endswith("WHERE owner = $auth;")

    def test_record_id_filter_is_coerced(self) -> None:
        sql = Ticket.objects().filter(id="t1")._compile_live(False)
        assert sql.endswith("WHERE id = `Ticket`:`t1`;")

    def test_fetch(self) -> None:
        sql = Ticket.objects().filter(status="a").fetch("owner")._compile_live(False)
        assert sql == "LIVE SELECT * FROM Ticket WHERE status = 'a' FETCH owner;"

    def test_uninlinable_value_raises_type_error(self) -> None:
        from datetime import timedelta

        with pytest.raises(TypeError, match="timedelta"):
            Ticket.objects().filter(age=timedelta(1))._compile_live(False)

    @pytest.mark.parametrize(
        ("label", "build"),
        [
            ("select()", lambda qs: qs.select("status")),
            ("limit()", lambda qs: qs.limit(1)),
            ("offset()", lambda qs: qs.offset(1)),
            ("order_by()", lambda qs: qs.order_by("age")),
            ("values()", lambda qs: qs.values("status")),
        ],
    )
    def test_refused_clauses_are_named(self, label: str, build: Any) -> None:
        with pytest.raises(SurrealDbError) as excinfo:
            build(Ticket.objects())._compile_live(False)
        assert label in str(excinfo.value)
        assert "v0.20.0" not in str(excinfo.value)


# ==================== Task 2 — filtered raw watch() (E2E, both lines) ====================


class TestFilteredWatchE2E:
    @pytest.mark.asyncio
    async def test_records_outside_the_filter_are_not_reported(self) -> None:
        async with orm_client(TABLE) as client:
            await _define(client)
            async with Ticket.objects().filter(status="active").watch() as stream:
                await client.query(f"CREATE {TABLE}:off SET status = 'inactive';", {})
                await client.query(f"CREATE {TABLE}:on SET status = 'active';", {})
                [envelope] = await _take(stream, 1)
                assert envelope["action"] == LiveAction.CREATE
                assert envelope["result"]["status"] == "active"
                await _assert_silent(stream)

    @pytest.mark.asyncio
    async def test_compiled_query_is_a_snapshot(self) -> None:
        """Review focus #1: a filter added after watch() does not reach the built stream."""
        async with orm_client(TABLE) as client:
            await _define(client)
            qs = Ticket.objects().filter(status="a")
            stream = qs.watch()
            qs.filter(status="never")  # would make the stream match nothing if re-read
            async with stream:
                await client.query(f"CREATE {TABLE}:snap SET status = 'a';", {})
                [envelope] = await _take(stream, 1)
                assert str(envelope["record"].id) == "snap"

    @pytest.mark.asyncio
    async def test_entering_and_leaving_the_filter(self) -> None:
        """Entering via UPDATE reports UPDATE; leaving reports nothing; a DELETE is reported only
        when the record still matched. Asserted on both lines (spec §2, footnote 1)."""
        async with orm_client(TABLE) as client:
            await _define(client)
            async with Ticket.objects().filter(status="active").watch() as stream:
                await client.query(f"CREATE {TABLE}:a SET status = 'active';", {})
                await client.query(f"CREATE {TABLE}:b SET status = 'inactive';", {})
                await client.query(f"UPDATE {TABLE}:a SET status = 'inactive';", {})
                await client.query(f"UPDATE {TABLE}:b SET status = 'active';", {})
                await client.query(f"DELETE {TABLE}:a;", {})
                await client.query(f"DELETE {TABLE}:b;", {})
                seen = await _take(stream, 3)
                await _assert_silent(stream)
        assert [(e["action"], str(e["record"].id)) for e in seen] == [
            (LiveAction.CREATE, "a"),
            (LiveAction.UPDATE, "b"),
            (LiveAction.DELETE, "b"),
        ]

    @pytest.mark.asyncio
    async def test_q_objects_and_lookups(self) -> None:
        async with orm_client(TABLE) as client:
            await _define(client)
            qs = Ticket.objects().filter(Q(status="vip") | Q(age__gte=10), age__lt=100)
            async with qs.watch() as stream:
                await client.query(f"CREATE {TABLE}:no1 SET status = 'x', age = 5;", {})
                await client.query(f"CREATE {TABLE}:no2 SET status = 'vip', age = 500;", {})
                await client.query(f"CREATE {TABLE}:yes SET status = 'x', age = 50;", {})
                [envelope] = await _take(stream, 1)
                assert str(envelope["record"].id) == "yes"
                await _assert_silent(stream)

    @pytest.mark.asyncio
    async def test_var_with_variables(self) -> None:
        async with orm_client(TABLE) as client:
            await _define(client)
            async with Ticket.objects().filter(age__gte=Var("min")).variables(min=5).watch() as stream:
                await client.query(f"CREATE {TABLE}:low SET age = 1;", {})
                await client.query(f"CREATE {TABLE}:high SET age = 9;", {})
                [envelope] = await _take(stream, 1)
                assert str(envelope["record"].id) == "high"

    @pytest.mark.asyncio
    async def test_record_id_filter(self) -> None:
        async with orm_client(TABLE) as client:
            await _define(client)
            async with Ticket.objects().filter(id="t-1").watch() as stream:
                await client.query(f"CREATE {TABLE}:other SET age = 1;", {})
                await client.query(f"CREATE {TABLE}:`t-1` SET age = 2;", {})
                [envelope] = await _take(stream, 1)
                assert str(envelope["record"].id) == "t-1"

    @pytest.mark.asyncio
    async def test_adversarial_string_matches_exactly(self) -> None:
        nasty = "it's \\ ' OR true; --"
        async with orm_client(TABLE) as client:
            await _define(client)
            async with Ticket.objects().filter(status=nasty).watch() as stream:
                await client.query(f"CREATE {TABLE}:decoy SET status = 'it\\'s';", {})
                await client.query(f"CREATE {TABLE}:hit SET status = $s;", {"s": nasty})
                [envelope] = await _take(stream, 1)
                assert envelope["result"]["status"] == nasty
                await _assert_silent(stream)

    @pytest.mark.asyncio
    async def test_dollar_value_matches_literally(self) -> None:
        """Review focus #2, end to end: '$$admin' is the literal string '$admin'."""
        async with orm_client(TABLE) as client:
            await _define(client)
            async with Ticket.objects().filter(status="$$admin").watch() as stream:
                await client.query(f"CREATE {TABLE}:d SET status = $s;", {"s": "$admin"})
                [envelope] = await _take(stream, 1)
                assert envelope["result"]["status"] == "$admin"

    @pytest.mark.asyncio
    async def test_aliased_field(self) -> None:
        async with orm_client(TABLE) as client:
            await _define(client)
            async with Ticket.objects().filter(label="x").watch() as stream:
                await client.query(f"CREATE {TABLE}:n SET label_col = 'y';", {})
                await client.query(f"CREATE {TABLE}:y SET label_col = 'x';", {})
                [envelope] = await _take(stream, 1)
                assert str(envelope["record"].id) == "y"

    @pytest.mark.asyncio
    async def test_fetch_resolves_links_in_notifications(self) -> None:
        async with orm_client(TABLE, "Owner") as client:
            await _define(client, TABLE, "Owner")
            await client.query("CREATE Owner:1 SET name = 'U1';", {})
            async with Ticket.objects().fetch("owner").watch() as stream:
                await client.query(f"CREATE {TABLE}:f SET owner = Owner:1;", {})
                [envelope] = await _take(stream, 1)
                assert envelope["result"]["owner"]["name"] == "U1"

    @pytest.mark.asyncio
    async def test_diff_mode_sends_patches(self) -> None:
        """The raw diff differs by line and watch() passes it through untouched: the root patch
        path is "" on 3.x and "/" on 2.x, and a DELETE carries "replace '' → None" on 3.x but
        the whole last record on 2.x. live() normalises both (see TestToChangeEvent)."""
        async with orm_client(TABLE) as client:
            await _define(client)
            async with Ticket.objects().watch(diff=True) as stream:
                await client.query(f"CREATE {TABLE}:p SET age = 1;", {})
                await client.query(f"UPDATE {TABLE}:p SET age = 2;", {})
                await client.query(f"DELETE {TABLE}:p;", {})
                create, update, delete = await _take(stream, 3)
        assert create["result"][0]["path"] in ("", "/")
        assert create["result"][0]["value"]["age"] == 1
        assert {"op": "replace", "path": "/age", "value": 2} in update["result"]
        assert delete["result"] in ([{"op": "replace", "path": "", "value": None}],) or (
            delete["result"]["age"] == 2  # SurrealDB 2.x: the last state, not a patch
        )


# ==================== Task 3 — envelope → ModelChangeEvent (unit) ====================

from surreal_orm_lite._sdk import RecordID  # noqa: E402
from surreal_orm_lite.live import ModelChangeEvent, to_change_event  # noqa: E402


class Strict(BaseSurrealModel):
    id: str
    name: str  # required: a minimal instance cannot validate


def _envelope(action: str, record: RecordID, result: Any) -> dict[str, Any]:
    return {"action": action, "record": record, "result": result, "id": "lq"}


class TestToChangeEvent:
    def test_full_record(self) -> None:
        rid = RecordID(TABLE, "a")
        event = to_change_event(Ticket, _envelope("CREATE", rid, {"id": rid, "status": "s", "label_col": "L"}), diff=False)
        assert isinstance(event, ModelChangeEvent)
        assert event.action is LiveAction.CREATE
        assert isinstance(event.instance, Ticket)
        assert event.instance.id == "a"
        assert event.instance.label == "L"
        assert event.record_id == str(rid)
        assert event.changed_fields == []

    def test_raw_is_not_mutated_by_hydration(self) -> None:
        rid = RecordID(TABLE, "a")
        result = {"id": rid, "status": "s"}
        event = to_change_event(Ticket, _envelope("UPDATE", rid, result), diff=False)
        assert event.raw is result
        assert result["id"] is rid

    def test_delete_carries_the_last_state(self) -> None:
        rid = RecordID(TABLE, "a")
        event = to_change_event(Ticket, _envelope("DELETE", rid, {"id": rid, "status": "gone"}), diff=False)
        assert event.action is LiveAction.DELETE
        assert event.instance.status == "gone"

    def test_diff_create_hydrates_from_the_root_replace(self) -> None:
        rid = RecordID(TABLE, "a")
        patches = [{"op": "replace", "path": "", "value": {"id": rid, "age": 1}}]
        event = to_change_event(Ticket, _envelope("CREATE", rid, patches), diff=True)
        assert event.instance.age == 1
        assert event.changed_fields == []
        assert event.raw is patches

    def test_diff_update_reports_python_field_names(self) -> None:
        rid = RecordID(TABLE, "a")
        patches = [
            {"op": "replace", "path": "/age", "value": 2},
            {"op": "change", "path": "/label_col", "value": "@@ -1 +1 @@"},
            {"op": "add", "path": "/age/extra", "value": 1},
            {"op": "add", "path": "/a~1b~0c", "value": 1},
        ]
        event = to_change_event(Ticket, _envelope("UPDATE", rid, patches), diff=True)
        assert event.changed_fields == ["age", "label", "a/b~c"]
        assert event.instance.id == "a"
        assert event.record_id == str(rid)

    def test_diff_delete_is_a_minimal_instance(self) -> None:
        rid = RecordID(TABLE, "a")
        patches = [{"op": "replace", "path": "", "value": None}]
        event = to_change_event(Ticket, _envelope("DELETE", rid, patches), diff=True)
        assert event.instance.id == "a"
        assert event.changed_fields == []

    def test_minimal_instance_falls_back_to_construct(self) -> None:
        """Review focus #3: required fields make validation fail; the id must survive."""
        rid = RecordID("Strict", "z")
        event = to_change_event(Strict, _envelope("UPDATE", rid, [{"op": "replace", "path": "/name", "value": "n"}]), diff=True)
        assert isinstance(event.instance, Strict)
        assert event.instance.id == "z"
        assert event.changed_fields == ["name"]

    def test_diff_create_on_2x_uses_a_slash_root(self) -> None:
        """SurrealDB 2.x spells the root pointer "/" where 3.x spells it ""."""
        rid = RecordID(TABLE, "a")
        patches = [{"op": "replace", "path": "/", "value": {"id": rid, "age": 1}}]
        event = to_change_event(Ticket, _envelope("CREATE", rid, patches), diff=True)
        assert event.instance.age == 1
        assert event.changed_fields == []

    def test_diff_delete_on_2x_carries_the_last_state(self) -> None:
        """SurrealDB 2.x answers a diff-mode DELETE with the whole record, not a patch list."""
        rid = RecordID(TABLE, "a")
        event = to_change_event(Ticket, _envelope("DELETE", rid, {"id": rid, "age": 9}), diff=True)
        assert event.instance.id == "a"
        assert event.instance.age == 9
        assert event.changed_fields == []


# ==================== Task 4 — typed live() (E2E, both lines) ====================

from uuid import UUID  # noqa: E402

from surreal_orm_lite import SurrealDBConnectionManager  # noqa: E402
from surreal_orm_lite.signals import post_live_change  # noqa: E402


class TestTypedLiveE2E:
    @pytest.mark.asyncio
    async def test_yields_typed_events_for_the_filter_only(self) -> None:
        async with orm_client(TABLE) as client:
            await _define(client)
            async with Ticket.objects().filter(status="active").live() as stream:
                assert stream.is_active and isinstance(stream.live_id, UUID)
                assert stream.table == TABLE
                await client.query(f"CREATE {TABLE}:off SET status = 'inactive';", {})
                await client.query(f"CREATE {TABLE}:on SET status = 'active', label_col = 'L';", {})
                await client.query(f"DELETE {TABLE}:on;", {})
                created, deleted = await _take(stream, 2)
                await _assert_silent(stream)
            assert not stream.is_active
        assert created.action is LiveAction.CREATE
        assert isinstance(created.instance, Ticket)
        assert (created.instance.id, created.instance.label) == ("on", "L")
        assert created.record_id == f"{TABLE}:on"
        assert deleted.action is LiveAction.DELETE
        assert deleted.instance.id == "on"

    @pytest.mark.asyncio
    async def test_diff_mode(self) -> None:
        async with orm_client(TABLE) as client:
            await _define(client)
            async with Ticket.objects().live(diff=True) as stream:
                await client.query(f"CREATE {TABLE}:d SET age = 1, label_col = 'a';", {})
                await client.query(f"UPDATE {TABLE}:d SET age = 2, label_col = 'b';", {})
                await client.query(f"DELETE {TABLE}:d;", {})
                create, update, delete = await _take(stream, 3)
        assert create.instance.age == 1
        assert sorted(update.changed_fields) == ["age", "label"]
        assert update.instance.id == "d"
        assert update.record_id == f"{TABLE}:d"
        assert delete.instance.id == "d"

    @pytest.mark.asyncio
    async def test_two_filtered_streams_are_independent(self) -> None:
        """Review focus #5."""
        async with orm_client(TABLE) as client:
            await _define(client)
            async with (
                Ticket.objects().filter(status="a").live() as first,
                Ticket.objects().filter(status="b").live() as second,
            ):
                await client.query(f"CREATE {TABLE}:x SET status = 'a';", {})
                await client.query(f"CREATE {TABLE}:y SET status = 'b';", {})
                [a] = await _take(first, 1)
                [b] = await _take(second, 1)
                await _assert_silent(first, 0.5)
                await _assert_silent(second, 0.5)
        assert (a.instance.id, b.instance.id) == ("x", "y")

    @pytest.mark.asyncio
    async def test_await_form_still_returns_a_uuid_and_warns(self) -> None:
        async with orm_client(TABLE) as client:
            await _define(client)
            with pytest.warns(DeprecationWarning, match=r"async with"):
                live_id = await Ticket.objects().filter(status="w").live()
            assert isinstance(live_id, UUID)
            raw = SurrealDBConnectionManager.subscribe_live(live_id)
            try:
                await client.query(f"CREATE {TABLE}:n SET status = 'x';", {})
                await client.query(f"CREATE {TABLE}:w SET status = 'w';", {})
                [envelope] = await _take(raw, 1)
                assert str(envelope["record"].id) == "w"
            finally:
                await SurrealDBConnectionManager.kill(live_id)

    def test_reconnect_kwargs_are_not_accepted_yet(self) -> None:
        with pytest.raises(TypeError):
            Ticket.objects().live(auto_resubscribe=True)  # type: ignore[call-arg]

    def test_refused_clause_raises_at_call_time(self) -> None:
        with pytest.raises(SurrealDbError, match=r"limit\(\)"):
            Ticket.objects().limit(1).live()


class TestPostLiveChangeE2E:
    @pytest.mark.asyncio
    async def test_handlers_receive_the_full_orm_kwargs(self) -> None:
        received: list[dict[str, Any]] = []
        done = asyncio.Event()

        @post_live_change.connect(Ticket)
        async def handler(sender: type, **kwargs: Any) -> None:
            received.append({"sender": sender, **kwargs})
            done.set()

        try:
            async with orm_client(TABLE) as client:
                await _define(client)
                async with Ticket.objects().live() as stream:
                    await client.query(f"CREATE {TABLE}:s SET age = 1;", {})
                    [event] = await _take(stream, 1)
                    await asyncio.wait_for(done.wait(), timeout=5)
        finally:
            post_live_change.disconnect(handler, Ticket)
        [call] = received
        assert call["sender"] is Ticket
        assert call["instance"] is event.instance
        assert call["action"] is LiveAction.CREATE
        assert call["record_id"] == f"{TABLE}:s"
        assert call["changed_fields"] == []

    @pytest.mark.asyncio
    async def test_a_failing_handler_is_logged_and_never_stops_the_stream(self, caplog: Any) -> None:
        @post_live_change.connect(Ticket)
        async def boom(sender: type, **kwargs: Any) -> None:
            raise RuntimeError("handler exploded")

        try:
            async with orm_client(TABLE) as client:
                await _define(client)
                async with Ticket.objects().live() as stream:
                    await client.query(f"CREATE {TABLE}:1 SET age = 1;", {})
                    await client.query(f"CREATE {TABLE}:2 SET age = 2;", {})
                    events = await _take(stream, 2)
                    await asyncio.sleep(0.1)
        finally:
            post_live_change.disconnect(boom, Ticket)
        assert len(events) == 2
        assert "handler exploded" in caplog.text

    def test_no_task_without_handlers(self) -> None:
        assert not post_live_change.has_handlers(Ticket)


class TestExportsV020:
    def test_full_orm_names_are_importable_from_the_package(self) -> None:
        import surreal_orm_lite as orm

        for name in ("LiveModelStream", "ModelChangeEvent", "post_live_change", "LiveAction", "LiveStream"):
            assert name in orm.__all__
            assert getattr(orm, name) is not None


# ==================== Final review fixes ====================


class TestExplicitFormE2E:
    @pytest.mark.asyncio
    async def test_documented_explicit_form_keeps_no_unread_buffer(self) -> None:
        """README "The explicit form": hand the started stream to a reader task and kill it by
        uuid from elsewhere. One reader registered, nothing buffered unread, and the kill ends
        the reader's loop."""
        async with orm_client(TABLE) as client:
            await _define(client)
            stream = await Ticket.objects().watch().start()
            live_id = stream.live_id
            seen: list[Any] = []

            async def reader() -> None:
                async for notif in stream:
                    seen.append(notif)

            task = asyncio.create_task(reader())
            try:
                for i in range(5):
                    await client.query(f"CREATE {TABLE}:x{i} SET age = {i};", {})
                assert len(client.live_queues[str(live_id)]) == 1
                for _ in range(50):
                    if len(seen) == 5:
                        break
                    await asyncio.sleep(0.05)
            finally:
                await SurrealDBConnectionManager.kill(live_id)
            await asyncio.wait_for(task, timeout=5)
        assert len(seen) == 5

    @pytest.mark.asyncio
    async def test_subscribe_live_without_a_connection_points_at_the_current_api(self) -> None:
        from surreal_orm_lite import SurrealDBConnectionManager as Manager

        await Manager.close_connection()
        with pytest.raises(SurrealDbError) as excinfo:
            Manager.subscribe_live("00000000-0000-0000-0000-000000000000")
        message = str(excinfo.value)
        assert "watch()" in message
        assert "await Model.objects().live()" not in message


# ==================== PR #199 review fixes — the typed stream ====================

import collections.abc  # noqa: E402
import logging  # noqa: E402


class Linked(BaseSurrealModel):
    id: str
    owner: str = ""  # a record link; fetch() turns it into a nested dict


class TestInvalidRecords:
    def test_invalid_record_is_kept_with_its_error(self) -> None:
        """Review #1: a record that does not fit the model must not end the stream."""
        from pydantic import ValidationError

        rid = RecordID("Strict", "z")
        result = {"id": rid, "other": 1}
        event = to_change_event(Strict, _envelope("CREATE", rid, result), diff=False)
        assert isinstance(event.validation_error, ValidationError)
        assert isinstance(event.instance, Strict)
        assert event.instance.id == "z"
        assert event.raw is result

    def test_valid_record_has_no_error(self) -> None:
        rid = RecordID(TABLE, "a")
        event = to_change_event(Ticket, _envelope("CREATE", rid, {"id": rid, "age": 1}), diff=False)
        assert event.validation_error is None

    def test_constructed_instance_uses_python_field_names(self) -> None:
        rid = RecordID("Linked", "l")
        event = to_change_event(Linked, _envelope("CREATE", rid, {"id": rid, "owner": {"name": "U"}}), diff=False)
        assert event.validation_error is not None
        assert event.instance.owner == {"name": "U"}


class TestReviewFixesStreamE2E:
    @pytest.mark.asyncio
    async def test_invalid_records_do_not_end_the_stream(self, caplog: Any) -> None:
        """Review #1, end to end: fetch() resolves a link the model types as str."""
        async with orm_client("Linked", "Owner") as client:
            await _define(client, "Linked", "Owner")
            await client.query("CREATE Owner:1 SET name = 'U';", {})
            with caplog.at_level(logging.WARNING, logger="surreal_orm_lite.live"):
                async with Linked.objects().fetch("owner").live() as stream:
                    await client.query("CREATE Linked:a SET owner = Owner:1;", {})
                    await client.query("CREATE Linked:b SET owner = Owner:1;", {})
                    first, second = await _take(stream, 2)
                    assert stream.is_active
        assert first.validation_error is not None and second.validation_error is not None
        assert first.instance.owner["name"] == "U"
        assert caplog.text.count("does not validate") == 1  # logged once per stream

    @pytest.mark.asyncio
    async def test_handlers_run_in_event_order(self) -> None:
        """Review #2: a slow CREATE handler must not let the DELETE handler finish first."""
        order: list[str] = []
        done = asyncio.Event()

        @post_live_change.connect(Ticket)
        async def handler(sender: type, action: LiveAction, **kwargs: Any) -> None:
            if action == LiveAction.CREATE:
                await asyncio.sleep(0.3)
            order.append(action)
            if len(order) == 2:
                done.set()

        try:
            async with orm_client(TABLE) as client:
                await _define(client)
                async with Ticket.objects().live() as stream:
                    await client.query(f"CREATE {TABLE}:o SET age = 1;", {})
                    await client.query(f"DELETE {TABLE}:o;", {})
                    await _take(stream, 2)
                    await asyncio.wait_for(done.wait(), timeout=5)
        finally:
            post_live_change.disconnect(handler, Ticket)
        assert order == [LiveAction.CREATE, LiveAction.DELETE]

    @pytest.mark.asyncio
    async def test_normal_exit_lets_pending_handlers_finish(self) -> None:
        """Review #3: breaking out after the last event must not cancel its handler."""
        finished: list[str] = []

        @post_live_change.connect(Ticket)
        async def handler(sender: type, record_id: str, **kwargs: Any) -> None:
            await asyncio.sleep(0.3)
            finished.append(record_id)

        try:
            async with orm_client(TABLE) as client:
                await _define(client)
                async with Ticket.objects().live() as stream:
                    await client.query(f"CREATE {TABLE}:n SET age = 1;", {})
                    await _take(stream, 1)
        finally:
            post_live_change.disconnect(handler, Ticket)
        assert finished == [f"{TABLE}:n"]

    @pytest.mark.asyncio
    async def test_error_exit_cancels_pending_handlers(self) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        @post_live_change.connect(Ticket)
        async def slow(sender: type, **kwargs: Any) -> None:
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        try:
            async with orm_client(TABLE) as client:
                await _define(client)
                with pytest.raises(RuntimeError, match="boom"):
                    async with Ticket.objects().live() as stream:
                        await client.query(f"CREATE {TABLE}:c SET age = 1;", {})
                        await _take(stream, 1)
                        await asyncio.wait_for(started.wait(), timeout=5)
                        raise RuntimeError("boom")
                await asyncio.wait_for(cancelled.wait(), timeout=5)
        finally:
            post_live_change.disconnect(slow, Ticket)

    @pytest.mark.asyncio
    async def test_drain_gives_up_after_the_timeout(self, caplog: Any) -> None:
        cancelled = asyncio.Event()

        @post_live_change.connect(Ticket)
        async def stuck(sender: type, **kwargs: Any) -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        try:
            async with orm_client(TABLE) as client:
                await _define(client)
                stream = Ticket.objects().live()
                stream.signal_drain_timeout = 0.2
                async with stream:
                    await client.query(f"CREATE {TABLE}:t SET age = 1;", {})
                    await _take(stream, 1)
                await asyncio.wait_for(cancelled.wait(), timeout=5)
        finally:
            post_live_change.disconnect(stuck, Ticket)
        assert "post_live_change" in caplog.text

    @pytest.mark.asyncio
    async def test_live_is_a_coroutine_again_for_create_task(self) -> None:
        """Review #6: v0.19.0 code passing ``qs.live()`` to create_task/run keeps working."""
        async with orm_client(TABLE) as client:
            await _define(client)
            assert isinstance(Ticket.objects().live(), collections.abc.Coroutine)
            with pytest.warns(DeprecationWarning, match="async with"):
                live_id = await asyncio.create_task(Ticket.objects().live())
            assert isinstance(live_id, UUID)
            await SurrealDBConnectionManager.kill(live_id)

    @pytest.mark.asyncio
    async def test_an_awaited_stream_refuses_to_start(self) -> None:
        """Review #7: ``uid = await s`` then ``async with s`` used to open two live queries and
        kill only one."""
        async with orm_client(TABLE) as client:
            await _define(client)
            stream = Ticket.objects().live()
            with pytest.warns(DeprecationWarning):
                live_id = await stream
            try:
                with pytest.raises(SurrealDbError, match="awaited"):
                    await stream.start()
                with pytest.raises(RuntimeError):
                    await stream
            finally:
                await SurrealDBConnectionManager.kill(live_id)

    @pytest.mark.asyncio
    async def test_a_started_stream_refuses_to_be_awaited(self) -> None:
        async with orm_client(TABLE) as client:
            await _define(client)
            async with Ticket.objects().live() as stream:
                with pytest.raises(SurrealDbError, match="already started"):
                    await stream

    @pytest.mark.asyncio
    async def test_http_is_refused_before_any_client_is_opened(self) -> None:
        """Review #10."""
        SurrealDBConnectionManager.set_connection(
            url="http://localhost:8000", user="root", password="root", namespace="ns", database="db"
        )
        try:
            for build in (lambda: Ticket.objects().watch(), lambda: Ticket.objects().live()):
                with pytest.raises(SurrealDbError, match="WebSocket"):
                    await build().start()
                assert SurrealDBConnectionManager._cached_client() is None
        finally:
            await SurrealDBConnectionManager.close_connection()


class TestInvalidRecordSafetyE2E:
    @pytest.mark.asyncio
    async def test_the_warning_never_logs_record_values(self, caplog: Any) -> None:
        """Security review: the ValidationError text embeds the input values."""
        async with orm_client("Linked", "Owner") as client:
            await _define(client, "Linked", "Owner")
            await client.query("CREATE Owner:1 SET secret = 'S3CRET-VALUE';", {})
            with caplog.at_level(logging.WARNING, logger="surreal_orm_lite.live"):
                async with Linked.objects().fetch("owner").live() as stream:
                    await client.query("CREATE Linked:a SET owner = Owner:1;", {})
                    await _take(stream, 1)
        assert "does not validate" in caplog.text
        assert "owner" in caplog.text
        assert "S3CRET-VALUE" not in caplog.text

    @pytest.mark.asyncio
    async def test_handlers_only_receive_validated_instances(self) -> None:
        """Security review: handlers written for the full ORM assume a validated instance."""
        received: list[str] = []
        done = asyncio.Event()

        @post_live_change.connect(Linked)
        async def handler(sender: type, record_id: str, **kwargs: Any) -> None:
            received.append(record_id)
            done.set()

        try:
            async with orm_client("Linked", "Owner") as client:
                await _define(client, "Linked", "Owner")
                await client.query("CREATE Owner:1 SET name = 'U';", {})
                async with Linked.objects().live() as stream:
                    await client.query("CREATE Linked:bad SET owner = 42;", {})
                    await client.query("CREATE Linked:ok SET owner = 'plain';", {})
                    bad, ok = await _take(stream, 2)
                    await asyncio.wait_for(done.wait(), timeout=5)
        finally:
            post_live_change.disconnect(handler, Linked)
        assert bad.validation_error is not None and ok.validation_error is None
        assert received == ["Linked:ok"]
