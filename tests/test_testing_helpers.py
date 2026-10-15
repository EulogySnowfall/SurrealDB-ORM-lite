"""``surreal_orm_lite.testing`` (v0.25.0): test helpers mirroring the full SurrealDB-ORM's ``surreal_orm.testing``.

``embedded_database()`` runs a block against an isolated in-process database (the official SDK's embedded
engine) and puts the previous connection back afterwards. The classes that open the engine are suffixed
``E2E``; none of them needs a server.
"""

import asyncio
import contextlib
import dataclasses
import subprocess
import sys
import weakref
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from surrealdb.connections.async_embedded import AsyncEmbeddedSurrealConnection

import surreal_orm_lite
from surreal_orm_lite import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager, testing
from surreal_orm_lite.exceptions import SurrealDbAuthenticationError, SurrealDbConnectionError
from surreal_orm_lite.testing import embedded_database
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
