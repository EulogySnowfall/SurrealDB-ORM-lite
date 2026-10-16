"""``surreal_orm_lite.testing`` (v0.25.0): test helpers mirroring the full SurrealDB-ORM's ``surreal_orm.testing``.

``embedded_database()`` runs a block against an isolated in-process database (the official SDK's embedded
engine) and puts the previous connection back afterwards; ``SurrealFixture``/``@fixture`` declare test data
that ``load()`` saves for one block and deletes afterwards. The classes that open the engine or a server are
suffixed ``E2E``; only the ``…ServerE2E`` classes need a server (this worktree's slot, both lines).
"""

import asyncio
import contextlib
import dataclasses
import logging
import subprocess
import sys
import weakref
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from surrealdb import RecordID
from surrealdb.connections.async_embedded import AsyncEmbeddedSurrealConnection

import surreal_orm_lite
from surreal_orm_lite import BaseSurrealEdge, BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager, testing
from surreal_orm_lite.exceptions import SurrealDbAuthenticationError, SurrealDbConnectionError, SurrealDbError
from surreal_orm_lite.testing import SurrealFixture, embedded_database, fixture
from tests.conftest import orm_client
from tests.test_embedded import (
    _calls_of,
    _count_constructions,
    _embedded_engine_available,
    _in_new_loop,
    _record_calls,
    _signatures,
    _slot_client,
)

pytestmark = pytest.mark.skipif(not _embedded_engine_available(), reason="the installed surrealdb SDK has no embedded engine")

#: A server configuration that is never connected: nothing in this module reaches a server.
_SERVER = ("ws://127.0.0.1:1/rpc", "root", "root", "ns", "db")


class Note(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="th_note")

    id: str | None = None
    text: str = ""


def _white(name: str) -> Any:
    """A private attribute of the connection manager (white box)."""
    return getattr(SurrealDBConnectionManager, f"_SurrealDBConnectionManager__{name}")


def _set_white(name: str, value: Any) -> None:
    setattr(SurrealDBConnectionManager, f"_SurrealDBConnectionManager__{name}", value)


def _lost_identity() -> "weakref.WeakSet[asyncio.AbstractEventLoop]":
    lost: weakref.WeakSet[asyncio.AbstractEventLoop] = _white("lost_identity")
    return lost


def _state() -> dict[str, Any]:
    """What a block must put back: settings (password included), stored session, embedded flag."""
    manager = SurrealDBConnectionManager
    return {
        "kwargs": manager.get_connection_kwargs(),
        "password": _white("password"),
        "password_set": manager.is_password_set(),
        "session": manager.get_session_token(),
        "refresh": manager.get_refresh_token(),
        "embedded": manager.is_embedded(),
        "set": manager.is_connection_set(),
    }


def _configure_server(session: str = "s", refresh: str = "r") -> dict[str, Any]:
    """Configure the (never connected) server settings with white-box stored tokens; return the state."""
    SurrealDBConnectionManager.set_connection(*_SERVER)
    _set_white("session_token", session)
    _set_white("refresh_token", refresh)
    return _state()


@contextlib.asynccontextmanager
async def _unset_after() -> AsyncIterator[None]:
    """Every test of this module ends with ``unset_connection()`` (it also clears the lost-identity loops)."""
    try:
        yield
    finally:
        await SurrealDBConnectionManager.unset_connection()


class TestTestingPackage:
    """The subpackage itself: its exports, and no pytest import (it is imported by applications' test code)."""

    def test_embedded_database_is_exported_from_the_subpackage_only(self) -> None:
        assert "embedded_database" in testing.__all__
        assert testing.embedded_database is embedded_database
        assert not hasattr(surreal_orm_lite, "embedded_database")

    def test_the_fixture_helpers_are_exported_from_the_subpackage_only(self) -> None:
        assert sorted(testing.__all__) == ["SurrealFixture", "embedded_database", "fixture"]
        assert testing.SurrealFixture is SurrealFixture
        assert testing.fixture is fixture
        assert not hasattr(surreal_orm_lite, "SurrealFixture")
        assert not hasattr(surreal_orm_lite, "fixture")

    def test_importing_it_does_not_import_pytest(self) -> None:
        code = (
            "import sys\n"
            "import surreal_orm_lite.testing\n"
            "print(sorted(m for m in sys.modules if m.split('.')[0] in {'pytest', '_pytest'}))\n"
        )
        result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120, check=False)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "[]"


class TestEmbeddedDatabaseE2E:
    """AC-12, AC-19's ``embedded_database()`` case (E6, E7, E11), no server."""

    async def test_it_yields_the_engine_client_with_the_defaults(self) -> None:
        async with _unset_after(), embedded_database() as client:
            assert isinstance(client, AsyncEmbeddedSurrealConnection)
            assert SurrealDBConnectionManager.is_embedded()
            assert SurrealDBConnectionManager.get_connection_kwargs() == {
                "url": "mem://",
                "user": None,
                "namespace": "test",
                "database": "test",
            }
            assert await SurrealDBConnectionManager.get_client() is client
            assert await client.query("RETURN 1;") == 1

    async def test_each_block_is_a_fresh_database(self) -> None:
        async with _unset_after():
            async with embedded_database():
                await Note(id="a", text="A").save()
                assert await Note.objects().count() == 1
            async with embedded_database():
                assert await Note.objects().count() == 0

    async def test_the_block_engine_is_closed_on_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _record_calls(monkeypatch, "close")
        async with _unset_after():
            async with embedded_database() as client:
                assert _calls_of(calls, client) == []
            assert _calls_of(calls, client) == [("close", ())]
            assert _slot_client() is None
            assert not SurrealDBConnectionManager.is_connected()

    async def test_the_previous_settings_and_stored_session_come_back(self) -> None:
        async with _unset_after():
            before = _configure_server()
            async with embedded_database():
                assert SurrealDBConnectionManager.is_embedded()
                assert SurrealDBConnectionManager.get_session_token() is None  # not replayed on the block's engine
                await Note(id="a", text="A").save()
            assert _state() == before
            assert SurrealDBConnectionManager.get_session_token() == "s"
            assert SurrealDBConnectionManager.get_refresh_token() == "r"
            assert SurrealDBConnectionManager.is_password_set()
            assert SurrealDBConnectionManager.is_embedded() is False
            assert _slot_client() is None

    async def test_the_previous_state_comes_back_when_the_body_raises(self) -> None:
        async with _unset_after():
            before = _configure_server()
            with pytest.raises(RuntimeError, match="boom"):
                async with embedded_database():
                    await Note(id="a", text="A").save()
                    raise RuntimeError("boom")
            assert _state() == before
            assert _slot_client() is None

    async def test_blocks_nest(self) -> None:
        async with _unset_after(), embedded_database() as outer:
            await Note(id="a", text="A").save()
            async with embedded_database() as inner:
                assert inner is not outer
                assert await Note.objects().count() == 0
                await Note(id="b", text="B").save()
                assert await Note.objects().count() == 1
            assert await SurrealDBConnectionManager.get_client() is outer
            assert await Note.objects().count() == 1
            assert (await Note.objects().get("a")).text == "A"

    async def test_an_outer_engine_with_data_survives_a_block(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _record_calls(monkeypatch, "close")
        async with _unset_after():
            SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="t")
            outer = await SurrealDBConnectionManager.get_client()
            await Note(id="a", text="A").save()
            async with embedded_database() as client:
                assert client is not outer
                assert await Note.objects().count() == 0
            # Detached for the block, never closed (SDK 2.x keeps a closed engine's data, so only the call shows it).
            assert _calls_of(calls, outer) == []
            assert _calls_of(calls, client) == [("close", ())]
            assert await SurrealDBConnectionManager.get_client() is outer
            assert SurrealDBConnectionManager.get_connection_kwargs() == {
                "url": "mem://",
                "user": None,
                "namespace": "t",
                "database": "t",
            }
            assert (await Note.objects().get("a")).text == "A"

    @pytest.mark.parametrize("url", ["ws://h/rpc", "http://h", ""])
    async def test_a_server_url_is_refused_before_anything_changes(self, url: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _unset_after():
            before = _configure_server()
            calls = _count_constructions(monkeypatch)
            with pytest.raises(ValueError, match="embedded URL") as info:
                async with embedded_database(url):
                    pytest.fail("the block must not run")  # pragma: no cover
            assert str(info.value) == f"embedded_database() needs an embedded URL (mem://, surrealkv://<path>, …), not {url!r}"
            assert _state() == before
            assert calls == []

    async def test_a_server_url_is_reported_before_an_empty_namespace(self) -> None:
        async with _unset_after():
            with pytest.raises(ValueError, match="embedded URL"):
                async with embedded_database("ws://h/rpc", namespace=""):
                    pytest.fail("the block must not run")  # pragma: no cover

    @pytest.mark.parametrize("namespace", ["test", ""])
    async def test_a_url_the_sdk_refuses_gets_e11_before_anything_changes(
        self, namespace: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with _unset_after():
            before = _configure_server()
            calls = _count_constructions(monkeypatch)
            with pytest.raises(SurrealDbConnectionError, match="cannot open") as info:
                async with embedded_database("rocksdb:///x", namespace=namespace):
                    pytest.fail("the block must not run")  # pragma: no cover
            exc = info.value
            assert not isinstance(exc, ValueError)
            assert "issues/233" in str(exc)
            assert exc.__cause__ is not None
            assert str(exc.__cause__) in str(exc)
            assert getattr(exc, "retryable", None) is False
            assert _state() == before
            assert calls == []

    async def test_a_misspelt_scheme_gets_e11_without_the_rocksdb_hint(self) -> None:
        async with _unset_after():
            with pytest.raises(SurrealDbConnectionError, match="cannot open") as info:
                async with embedded_database("htp://h"):
                    pytest.fail("the block must not run")  # pragma: no cover
            assert "233" not in str(info.value)

    @pytest.mark.parametrize(("namespace", "database"), [("", "test"), ("test", "")])
    async def test_an_empty_namespace_or_database_is_refused_before_anything_changes(
        self, namespace: str, database: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with _unset_after():
            before = _configure_server()
            calls = _count_constructions(monkeypatch)
            with pytest.raises(ValueError, match="namespace and a database") as info:
                async with embedded_database(namespace=namespace, database=database):
                    pytest.fail("the block must not run")  # pragma: no cover
            assert str(info.value) == "embedded_database() needs a namespace and a database"
            assert _state() == before
            assert calls == []

    async def test_an_engine_that_cannot_open_puts_the_state_back(self) -> None:
        async with _unset_after():
            before = _configure_server()
            with pytest.raises(SurrealDbConnectionError, match="Can't open the embedded database"):
                async with embedded_database("surrealkv:///proc/forbidden/kv"):
                    pytest.fail("the block must not run")  # pragma: no cover
            assert _state() == before
            assert _slot_client() is None

    async def test_nothing_configured_before_means_nothing_configured_after(self) -> None:
        async with _unset_after():
            await SurrealDBConnectionManager.unset_connection()
            async with embedded_database():
                assert SurrealDBConnectionManager.is_connection_set()
            assert SurrealDBConnectionManager.is_connection_set() is False
            assert SurrealDBConnectionManager.get_connection_string() is None
            assert _slot_client() is None
            assert not SurrealDBConnectionManager.is_connected()

    async def test_a_surrealkv_database_outlives_its_block(self, tmp_path: Path) -> None:
        url = f"surrealkv://{tmp_path / 'kv'}"
        async with _unset_after():
            async with embedded_database(url) as client:
                await Note(id="a", text="A").save()
                del client  # the closed-client rule: the path is reopened below
            async with embedded_database(url):
                assert (await Note.objects().get("a")).text == "A"

    async def test_function_signatures_of_the_block_are_forgotten(self) -> None:
        async with _unset_after():
            async with embedded_database():
                _signatures()[("mem://", "test", "test", "fn::x")] = ("a",)
            assert SurrealDBConnectionManager.function_signature_cache_size() == 0


class TestEmbeddedDatabaseStateE2E:
    """AC-23, no server: the snapshot keeps secrets out of its repr, a lost identity survives a block, and
    entry and exit are each one guarded step."""

    async def test_the_snapshot_keeps_secrets_out_of_its_repr(self) -> None:
        async with _unset_after():
            SurrealDBConnectionManager.set_connection("ws://127.0.0.1:1/rpc", "root", "pw-secret", "ns", "db")
            _set_white("session_token", "tok-secret")
            _set_white("refresh_token", "ref-secret")
            state = await SurrealDBConnectionManager._enter_embedded_block("mem://", "t", "t")
            try:
                text = repr(state)
                for secret in ("pw-secret", "tok-secret", "ref-secret"):
                    assert secret not in text
                assert "ws://127.0.0.1:1/rpc" in text  # the other fields are shown
                with pytest.raises(dataclasses.FrozenInstanceError):
                    state.url = "x"  # type: ignore[misc]
                assert SurrealDBConnectionManager.get_connection_kwargs() == {
                    "url": "mem://",
                    "user": None,
                    "namespace": "t",
                    "database": "t",
                }
                assert SurrealDBConnectionManager.get_session_token() is None
            finally:
                await SurrealDBConnectionManager._exit_embedded_block(state)
            assert _white("password") == "pw-secret"
            assert SurrealDBConnectionManager.get_session_token() == "tok-secret"
            assert SurrealDBConnectionManager.get_refresh_token() == "ref-secret"

    async def test_a_lost_identity_survives_a_block(self) -> None:
        async with _unset_after():
            SurrealDBConnectionManager.set_connection(*_SERVER)
            _lost_identity().add(asyncio.get_running_loop())
            try:
                async with embedded_database():
                    assert await Note.objects().count() == 0  # the block's engine is not blocked meanwhile
                with pytest.raises(SurrealDbAuthenticationError, match="dropped while it was authenticated"):
                    await SurrealDBConnectionManager.get_client()
            finally:
                _lost_identity().clear()

    async def test_a_closed_loop_is_not_put_back_in_the_lost_identity_state(self) -> None:
        closed = asyncio.new_event_loop()
        closed.close()
        async with _unset_after():
            SurrealDBConnectionManager.set_connection(*_SERVER)
            running = asyncio.get_running_loop()
            _lost_identity().add(closed)
            _lost_identity().add(running)
            try:
                async with embedded_database():
                    assert len(_lost_identity()) == 0
                assert running in _lost_identity()
                assert closed not in _lost_identity()
            finally:
                _lost_identity().clear()

    async def test_entry_and_exit_each_take_the_guard_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        guards: list[str] = []
        steps: list[tuple[str, bool]] = []
        held = [False]
        real_guard = SurrealDBConnectionManager._embedded_guard
        real_set_connection = SurrealDBConnectionManager.set_connection
        real_clear = SurrealDBConnectionManager.clear_function_signature_cache

        @contextlib.asynccontextmanager
        async def counting_guard(cls: Any) -> AsyncIterator[None]:
            async with real_guard():
                guards.append("guard")
                held[0] = True
                try:
                    yield
                finally:
                    held[0] = False

        def recording_set_connection(cls: Any, *args: Any, **kwargs: Any) -> None:
            steps.append(("set_connection", held[0]))
            assert _slot_client() is None, "the slot is detached before the settings change"
            real_set_connection(*args, **kwargs)

        def recording_clear(cls: Any) -> None:
            steps.append(("clear_function_signature_cache", held[0]))
            real_clear()

        async with _unset_after():
            SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="t")
            outer = await SurrealDBConnectionManager.get_client()  # an outer engine for the entry to detach
            monkeypatch.setattr(SurrealDBConnectionManager, "_embedded_guard", classmethod(counting_guard))
            monkeypatch.setattr(SurrealDBConnectionManager, "set_connection", classmethod(recording_set_connection))
            monkeypatch.setattr(SurrealDBConnectionManager, "clear_function_signature_cache", classmethod(recording_clear))

            state = await SurrealDBConnectionManager._enter_embedded_block("mem://", "test", "test")
            assert guards == ["guard"]
            assert steps == [("set_connection", True), ("clear_function_signature_cache", True)]
            assert state.embedded_client is outer
            steps.clear()

            await SurrealDBConnectionManager._exit_embedded_block(state)
            assert guards == ["guard", "guard"]
            assert steps == [("clear_function_signature_cache", True)]
            assert _slot_client() is outer

            guards.clear()
            async with embedded_database():
                pass
            assert len(guards) >= 2  # entry and exit, plus the block's own get_client()

    async def test_a_concurrent_first_get_client_never_crosses_the_block(self) -> None:
        """Whichever of the two takes the guard first, the slot ends consistent: the thread's engine is either
        the block's (closed with it) or the outer settings' (detached with the rest, and back afterwards)."""
        async with _unset_after():
            SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="t")  # not opened
            lock = _white("embedded_lock")
            lock.acquire()
            try:
                opener = asyncio.ensure_future(_in_new_loop(SurrealDBConnectionManager.get_client))
                entering = asyncio.ensure_future(SurrealDBConnectionManager._enter_embedded_block("mem://", "test", "test"))
                await asyncio.sleep(0.05)
                assert not opener.done()
                assert not entering.done()
            finally:
                lock.release()
            state = await entering
            seen = await opener
            block = await SurrealDBConnectionManager.get_client()
            await SurrealDBConnectionManager._exit_embedded_block(state)
            if seen is block:  # the block went first: the thread was served the block's engine
                assert state.embedded_client is None
                assert _slot_client() is None
            else:  # the thread went first: its engine was detached on entry and is back
                assert state.embedded_client is seen
                assert _slot_client() is seen
            assert SurrealDBConnectionManager.get_connection_kwargs()["database"] == "t"


# ---------------------------------------------------------------------------
# SurrealFixture and @fixture (AC-13): declarative test data, saved for one block
# ---------------------------------------------------------------------------


class Member(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="member")

    id: str | None = None
    name: str = ""
    role: str = ""


class Visitor(BaseSurrealModel):
    """A second model class on the ``member`` table: the interrupted-load tests patch its ``save`` only."""

    model_config = SurrealConfigDict(table_name="member")

    id: str | None = None
    name: str = ""
    role: str = ""


class Follows(BaseSurrealEdge):
    model_config = SurrealConfigDict(table_name="follows")


@fixture
class Team(SurrealFixture):
    alice = Member(id="alice", name="Alice", role="admin")
    bob = Member(id="bob", name="Bob", role="player")
    carol = Member(name="Carol", role="player")  # an id given by the database
    follows = Follows(in_=RecordID("member", "alice"), out=RecordID("member", "bob"))  # after its nodes


@fixture
class ServerTeam(SurrealFixture):
    """``Team`` minus the edge, for the server lines."""

    alice = Member(id="alice", name="Alice", role="admin")
    bob = Member(id="bob", name="Bob", role="player")
    carol = Member(name="Carol", role="player")


@fixture
class Duplicated(SurrealFixture):
    """Its third save fails: the same explicit id as the first one."""

    first = Member(id="dup", name="First")
    other = Member(id="other", name="Other")
    second = Member(id="dup", name="Second")


@fixture
class Interrupted(SurrealFixture):
    """A member, then a visitor whose ``save`` the tests patch to be cancelled."""

    member = Member(id="m", name="Member")
    visitor = Visitor(id="v", name="Visitor")


async def _counts() -> tuple[int, int]:
    """``(members, follows)`` in the current database."""
    return await Member.objects().count(), await Follows.objects().count()


def _label(instance: Any) -> str:
    return "follows" if isinstance(instance, Follows) else str(instance.name)


def _record_writes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record ``("save" | "delete", label)`` for every save and delete of ``Member`` and ``Follows``."""
    events: list[tuple[str, str]] = []
    for model in (Member, Follows):
        for method in ("save", "delete"):
            real = getattr(model, method)

            def recording(real: Any = real, method: str = method) -> Any:
                async def call(self: Any, *args: Any, **kwargs: Any) -> Any:
                    events.append((method, _label(self)))
                    return await real(self, *args, **kwargs)

                return call

            monkeypatch.setattr(model, method, recording())
    return events


async def _cancelled_save(self: Any, *args: Any, **kwargs: Any) -> Any:
    raise asyncio.CancelledError()


class TestFixtureDecorator:
    """``@fixture`` (E9, what it collects) and E8 of ``load()``: no database."""

    def test_a_plain_class_is_refused(self) -> None:
        class Plain:
            alice = Member(name="Alice")

        with pytest.raises(TypeError, match="SurrealFixture subclass"):
            fixture(Plain)  # type: ignore[type-var]
        assert not hasattr(Plain, "_fixture_instances")

    def test_something_other_than_a_class_is_refused(self) -> None:
        with pytest.raises(TypeError, match="SurrealFixture subclass, not Member"):
            fixture(Member(name="Alice"))  # type: ignore[type-var]

    def test_it_collects_only_the_model_instances_in_declaration_order(self) -> None:
        @fixture
        class Mixed(SurrealFixture):
            alice = Member(name="Alice")
            note = "x"
            bob = Member(name="Bob")
            size = 3
            follows = Follows(in_=RecordID("member", "alice"), out=RecordID("member", "bob"))

        assert list(Mixed._fixture_instances) == ["alice", "bob", "follows"]
        assert Mixed._fixture_instances["alice"] is Mixed.alice  # the templates themselves, kept as declared

    def test_it_returns_the_class_itself(self) -> None:
        class Same(SurrealFixture):
            alice = Member(name="Alice")

        assert fixture(Same) is Same

    def test_inherited_instances_are_not_collected(self) -> None:
        """As the full ORM: only the class's own attributes."""

        @fixture
        class Parent(SurrealFixture):
            alice = Member(name="Alice")

        @fixture
        class Child(Parent):
            bob = Member(name="Bob")

        assert list(Child._fixture_instances) == ["bob"]
        assert list(Parent._fixture_instances) == ["alice"]

    async def test_load_without_the_decorator_is_refused(self) -> None:
        class Undecorated(SurrealFixture):
            alice = Member(name="Alice")

        with pytest.raises(ValueError, match="Undecorated has no fixture instances. Did you forget the @fixture decorator"):
            async with Undecorated.load():
                raise AssertionError("the body never runs")

    async def test_an_undecorated_subclass_declaring_instances_is_refused(self) -> None:
        """It would inherit its decorated parent's instances and silently drop its own."""

        @fixture
        class Parent(SurrealFixture):
            alice = Member(name="Alice")

        class Child(Parent):
            bob = Member(name="Bob")

        with pytest.raises(
            ValueError,
            match=r"Child declares fixture instances but is not decorated with @fixture: only its parent's would "
            r"load\. Did you forget the @fixture decorator\?",
        ):
            async with Child.load():
                raise AssertionError("the body never runs")

    async def test_load_of_a_fixture_without_instances_is_refused(self) -> None:
        @fixture
        class Empty(SurrealFixture):
            note = "x"

        with pytest.raises(ValueError, match="has no fixture instances"):
            async with Empty.load():
                raise AssertionError("the body never runs")
        with pytest.raises(ValueError, match="has no fixture instances"):
            async with SurrealFixture.load():
                raise AssertionError("the body never runs")


class TestSurrealFixtureE2E:
    """AC-13 on the in-process engine, one ``embedded_database()`` per test: no server."""

    async def test_load_yields_saved_copies_and_deletes_them_on_exit(self) -> None:
        async with _unset_after(), embedded_database():
            async with Team.load() as team:
                assert isinstance(team, Team)
                assert team.alice.get_raw_id() == "alice"  # an explicit id is kept (D9)
                assert team.bob.get_raw_id() == "bob"
                assert team.carol.get_id() is not None
                assert team.follows.get_id() is not None
                assert team.bob is not Team.bob  # a copy: the template is never saved
                assert (team.alice.name, team.alice.role) == ("Alice", "admin")
                assert team.follows.in_ == RecordID("member", "alice")
                assert await _counts() == (3, 1)
                admins = await Member.objects().filter(role="admin").exec()
                assert [admin.get_raw_id() for admin in admins] == ["alice"]
                carol = await Member.objects().get(team.carol.get_raw_id())
                assert carol.name == "Carol"
            assert await _counts() == (0, 0)
        # The templates are untouched.
        assert (Team.alice.id, Team.bob.id, Team.carol.id, Team.follows.id) == ("alice", "bob", None, None)

    async def test_an_undecorated_subclass_without_instances_loads_its_parents(self) -> None:
        """As in the full ORM: a subclass that declares no instance of its own reuses its decorated parent's."""

        class Reused(ServerTeam):
            note = "x"

        async with _unset_after(), embedded_database():
            async with Reused.load() as team:
                assert isinstance(team, Reused)
                assert team.alice.get_raw_id() == "alice"
                assert await Member.objects().count() == 3
            assert await Member.objects().count() == 0

    async def test_saved_in_declaration_order_and_deleted_in_reverse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        events = _record_writes(monkeypatch)
        async with _unset_after(), embedded_database():
            async with Team.load():
                assert events == [("save", "Alice"), ("save", "Bob"), ("save", "Carol"), ("save", "follows")]
                events.clear()
            assert events == [("delete", "follows"), ("delete", "Carol"), ("delete", "Bob"), ("delete", "Alice")]

    async def test_a_body_that_raises_is_cleaned_up_and_the_error_propagates(self) -> None:
        async with _unset_after(), embedded_database():
            with pytest.raises(RuntimeError, match="boom"):
                async with Team.load():
                    assert await _counts() == (3, 1)
                    raise RuntimeError("boom")
            assert await _counts() == (0, 0)

    async def test_a_failing_save_deletes_the_earlier_ones_and_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """D9: the full ORM leaves the earlier saves in place."""
        events = _record_writes(monkeypatch)
        async with _unset_after(), embedded_database():
            with pytest.raises(SurrealDbError, match="already exists"):
                async with Duplicated.load():
                    raise AssertionError("the body never runs")
            assert events[-2:] == [("delete", "Other"), ("delete", "First")]  # in reverse order
            assert await Member.objects().count() == 0

    async def test_a_failing_cleanup_delete_does_not_hide_the_load_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def failing_delete(self: Any, *args: Any, **kwargs: Any) -> None:
            raise SurrealDbError("cannot delete")

        monkeypatch.setattr(Member, "delete", failing_delete)
        async with _unset_after(), embedded_database():
            with (
                caplog.at_level(logging.DEBUG, logger="surreal_orm_lite.testing"),
                pytest.raises(SurrealDbError, match="already exists"),
            ):
                async with Duplicated.load():
                    raise AssertionError("the body never runs")
        failed = [r for r in caplog.records if r.name == "surreal_orm_lite.testing" and r.levelno == logging.DEBUG]
        assert [r.getMessage() for r in failed] == [
            "Fixture cleanup: failed to delete Member other",
            "Fixture cleanup: failed to delete Member dup",
        ]
        assert all(r.exc_info is not None and "cannot delete" in str(r.exc_info[1]) for r in failed)

    async def test_a_cancelled_load_deletes_the_earlier_saves_and_re_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """D9: the second template's ``save`` is cancelled; the first one's record is deleted."""
        monkeypatch.setattr(Visitor, "save", _cancelled_save)
        async with _unset_after(), embedded_database():
            with pytest.raises(asyncio.CancelledError):
                async with Interrupted.load():
                    raise AssertionError("the body never runs")
            assert await Member.objects().count() == 0

    async def test_a_load_cancelled_from_outside_deletes_the_earlier_saves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A real ``Task.cancel()`` while the second ``save`` waits: the cleanup still reaches the database."""
        waiting = asyncio.Event()
        entered: list[bool] = []

        async def hanging_save(self: Any, *args: Any, **kwargs: Any) -> Any:
            waiting.set()
            await asyncio.Event().wait()

        async def load() -> None:
            async with Interrupted.load():
                entered.append(True)

        monkeypatch.setattr(Visitor, "save", hanging_save)
        async with _unset_after(), embedded_database():
            async with asyncio.timeout(5):
                task = asyncio.ensure_future(load())
                await waiting.wait()
                assert await Member.objects().count() == 1
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert entered == []
            assert await Member.objects().count() == 0

    async def test_a_record_deleted_in_the_body_is_skipped_at_cleanup(self, caplog: pytest.LogCaptureFixture) -> None:
        """``carol`` has no edge: deleting a node also deletes its edges, which would fail a second delete."""
        async with _unset_after(), embedded_database():
            with caplog.at_level(logging.DEBUG, logger="surreal_orm_lite.testing"):
                async with Team.load() as team:
                    carol_id = team.carol.get_id()
                    await team.carol.delete()
            assert await _counts() == (0, 0)
        failed = [r for r in caplog.records if r.name == "surreal_orm_lite.testing" and r.levelno == logging.DEBUG]
        assert len(failed) == 1
        assert failed[0].getMessage() == f"Fixture cleanup: failed to delete Member {carol_id}"
        assert failed[0].exc_info is not None and isinstance(failed[0].exc_info[1], SurrealDbError)

    async def test_deleting_a_node_in_the_body_leaves_nothing_behind(self) -> None:
        """Its edge goes with it (SurrealDB deletes a node's edges); the two failing deletes are skipped."""
        async with _unset_after(), embedded_database():
            async with Team.load() as team:
                await team.bob.delete()
                assert await _counts() == (2, 0)
            assert await _counts() == (0, 0)

    async def test_two_loads_in_sequence(self) -> None:
        async with _unset_after(), embedded_database():
            async with Team.load() as first:
                first_alice = first.alice
            async with Team.load() as second:
                assert second.alice is not first_alice
                assert second.alice.get_raw_id() == "alice"
                assert await _counts() == (3, 1)
            assert await _counts() == (0, 0)


class TestSurrealFixtureServerE2E:
    """AC-13 on this worktree's slot servers (both lines): the helpers work on any configured connection."""

    async def test_load_saves_and_deletes_on_the_server(self) -> None:
        async with orm_client("member"):
            async with ServerTeam.load() as team:
                assert team.alice.get_raw_id() == "alice"
                assert team.carol.get_id() is not None
                assert team.bob is not ServerTeam.bob
                assert await Member.objects().count() == 3
                admins = await Member.objects().filter(role="admin").exec()
                assert [admin.get_raw_id() for admin in admins] == ["alice"]
            assert await Member.objects().count() == 0
        assert ServerTeam.carol.id is None

    async def test_a_failing_save_deletes_the_earlier_ones_on_the_server(self) -> None:
        async with orm_client("member"):
            with pytest.raises(SurrealDbError, match="already exists"):
                async with Duplicated.load():
                    raise AssertionError("the body never runs")
            assert await Member.objects().count() == 0

    async def test_a_cancelled_load_deletes_the_earlier_saves_on_the_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Visitor, "save", _cancelled_save)
        async with orm_client("member"):
            with pytest.raises(asyncio.CancelledError):
                async with Interrupted.load():
                    raise AssertionError("the body never runs")
            assert await Member.objects().count() == 0
