"""Multi-database (v0.29.0): named connections, each on a connection class of its own behind a routing manager.

``SurrealDBConnectionManager`` holds no connection state any more: each connection — the default one included —
is a private subclass whose name-mangled class attributes are that connection's state, and every state-touching
classmethod of the public class resolves the connection class and runs there. Unit tests first (no server); the
classes that open an engine or a server are suffixed ``E2E``. Models are prefixed ``Md``, tables ``md_*``.
"""

import ast
import asyncio
import contextlib
import dataclasses
import inspect
import logging
import os
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from surrealdb import AsyncSurreal

import surreal_orm_lite
from surreal_orm_lite import (
    AuthenticatedUserMixin,
    BaseSurrealEdge,
    BaseSurrealModel,
    ConnectionConfig,
    SurrealConfigDict,
    SurrealDbAuthenticationError,
    SurrealDbConnectionError,
    SurrealDBConnectionManager,
    SurrealDbError,
    connection_manager,
    live,
)
from tests._proxy import CuttableProxy
from tests.conftest import default_connection_class
from tests.test_time_travel import _forbidden_client

#: The prefix of the manager's name-mangled private attributes.
_P = "_SurrealDBConnectionManager__"

#: The classmethods of the class body that are deliberately not routed (spec §6.1): the resolvers themselves and
#: the registry, which acts by the name it is given.
_NOT_ROUTED = {
    "_route",
    "_manager_for",
    "add_connection",
    "remove_connection",
    "get_config",
    "list_connections",
    "using",
    "get_active_connection_name",
}

#: The routing policy of every routed classmethod that does not act on the current connection (spec §6.1).
_POLICIES = {
    "set_connection": "default",
    "unset_connection": "default",
    "close_all_connections": "every",
    "subscribe_live": "live",
    "kill": "live",
}


class MdCustomer(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="md_customer")

    id: str | None = None
    name: str = ""


def _class_methods() -> dict[str, classmethod]:  # type: ignore[type-arg]
    """Every classmethod of the router's class body, by its name in ``vars()``."""
    return {name: value for name, value in vars(SurrealDBConnectionManager).items() if isinstance(value, classmethod)}


def _routed_names() -> list[str]:
    return sorted(name for name in _class_methods() if name not in _NOT_ROUTED)


def _is_template(value: Any, template: Any) -> bool:
    """Whether *value* is still what a fresh connection starts with (spec AC-2)."""
    if hasattr(template, "locked"):
        return hasattr(value, "locked") and not value.locked()
    if template is None or isinstance(template, int):
        return bool(value == template)
    return type(value) is type(template) and len(value) == 0


class TestConnectionClasses:
    """AC-2: the state template, one class per connection, and the routed classmethods (no server)."""

    def test_state_template_names_every_state_attribute(self) -> None:
        declared = {
            name[len(_P) :]
            for name, value in vars(SurrealDBConnectionManager).items()
            if name.startswith(_P) and not callable(value) and not isinstance(value, classmethod | staticmethod)
        }
        assert {key for key, _ in connection_manager._STATE} == declared
        assert len(connection_manager._STATE) == len(declared)

    def test_default_is_a_connection_class_with_fresh_state(self) -> None:
        cls = SurrealDBConnectionManager._manager_for("default")
        assert cls is not SurrealDBConnectionManager
        assert issubclass(cls, SurrealDBConnectionManager)
        assert cls._connection_name == "default"
        assert SurrealDBConnectionManager._connection_name is None
        assert SurrealDBConnectionManager._manager_for() is cls
        for key, factory in connection_manager._STATE:
            assert _P + key in vars(cls), key
            template = factory()
            if template is not None and not isinstance(template, int):
                assert vars(cls)[_P + key] is not vars(SurrealDBConnectionManager)[_P + key], key

    def test_every_state_touching_classmethod_is_routed(self) -> None:
        methods = _class_methods()
        assert set(methods) >= _NOT_ROUTED
        for name in _NOT_ROUTED:
            assert not hasattr(methods[name].__func__, "__surreal_routed__"), name
        for name in _routed_names():
            expected = _POLICIES.get(name, "current")
            assert getattr(methods[name].__func__, "__surreal_routed__", None) == expected, name
        assert set(_POLICIES) <= set(_routed_names())
        # Private methods are routed too, the name-mangled one included.
        assert {"_acquire_client", "_open_client", _P + "prune_dead_loops"} <= set(_routed_names())

    def test_routed_coroutines_stay_coroutine_functions(self) -> None:
        """M15: an ``async def`` wrapper for a coroutine method, a plain one for the rest — on Python 3.11 too."""
        default = SurrealDBConnectionManager._manager_for("default")
        for name in _routed_names():
            function = vars(SurrealDBConnectionManager)[name].__func__
            original = inspect.unwrap(function)
            expected = inspect.iscoroutinefunction(original)
            assert inspect.iscoroutinefunction(function) is expected, name
            assert inspect.iscoroutinefunction(getattr(SurrealDBConnectionManager, name)) is expected, name
            assert inspect.iscoroutinefunction(getattr(default, name)) is expected, name
            assert inspect.signature(function) == inspect.signature(original), name
            assert function.__name__ == original.__name__, name
            assert name.endswith(function.__name__), name
            assert function.__doc__ == original.__doc__, name
        for name in ("get_client", "close_connection", "signin", "signup", "kill", "reconnect", "set_url"):
            assert inspect.iscoroutinefunction(getattr(SurrealDBConnectionManager, name)), name
        for name in ("transaction", "ephemeral_client", "is_connected", "subscribe_live"):
            assert not inspect.iscoroutinefunction(getattr(SurrealDBConnectionManager, name)), name

    def test_signup_keeps_the_invalidation_wrapper_outermost(self) -> None:
        function = vars(SurrealDBConnectionManager)["signup"].__func__
        assert function.__surreal_cache_writes__ is True
        assert function.__surreal_routed__ == "current"
        routed = function.__wrapped__
        assert inspect.iscoroutinefunction(routed)
        assert not hasattr(routed, "__surreal_cache_writes__")
        assert routed.__surreal_routed__ == "current"


class _Sentinel(Exception):
    """Raised by the substitute client: the ORM went through the substituted ``get_client``."""


class _SubstituteClient:
    """What an application's substitute of ``get_client`` hands the ORM: every request raises :class:`_Sentinel`."""

    async def query(self, *args: Any, **kwargs: Any) -> Any:
        raise _Sentinel("query")

    async def select(self, *args: Any, **kwargs: Any) -> Any:
        raise _Sentinel("select")

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        raise _Sentinel("create")

    async def upsert(self, *args: Any, **kwargs: Any) -> Any:
        raise _Sentinel("upsert")

    async def insert(self, *args: Any, **kwargs: Any) -> Any:
        raise _Sentinel("insert")

    async def version(self, *args: Any, **kwargs: Any) -> Any:
        raise _Sentinel("version")


class TestSubstitutedGetClient:
    """AC-1: the ORM acquires clients with ``get_client()`` without argument, so a substitute still intercepts."""

    async def test_a_substituted_get_client_still_intercepts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        acquired: list[bool] = []

        async def fake() -> Any:
            acquired.append(True)
            return _SubstituteClient()

        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", staticmethod(fake))
        with pytest.raises(_Sentinel):
            await MdCustomer(name="x").save()
        with pytest.raises(_Sentinel):
            await MdCustomer.objects().exec()
        assert len(acquired) == 2

    async def test_relate_and_a_change_feed_start_go_through_it_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Through the model's connection class, which inherits the substitute (spec AC-1)."""
        acquired: list[bool] = []

        async def fake() -> Any:
            acquired.append(True)
            return _SubstituteClient()

        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", staticmethod(fake))
        with pytest.raises(_Sentinel, match="query"):
            await MdCustomer(id="a").relate("md_knows", MdCustomer(id="b"))
        with pytest.raises(_Sentinel, match="version"):
            await MdCustomer.objects().changes().start()
        assert len(acquired) == 2


class _LiveClient:
    """A server client as the live registry and ``kill()`` see it: a ``live_queues`` dict and a recording ``kill``."""

    def __init__(self) -> None:
        self.live_queues: dict[str, list[asyncio.Queue[Any]]] = {}
        self.killed: list[str] = []

    async def kill(self, query_uuid: Any) -> None:
        self.killed.append(str(query_uuid))

    async def close(self) -> None:
        """No socket to close."""


def _named_connection(monkeypatch: pytest.MonkeyPatch, name: str) -> tuple[type[SurrealDBConnectionManager], _LiveClient]:
    """A connection class *name* for one test, on a ``ws://`` URL never contacted, with a fake client cached for the
    running loop. White box: registered straight into the private registry, which *monkeypatch* restores."""
    connection = connection_manager._new_connection_class(name)
    monkeypatch.setitem(connection_manager._REGISTRY, name, connection)
    connection.set_connection(url="ws://127.0.0.1:9/rpc", user="u", password="p", namespace="t", database=name)
    client = _LiveClient()
    vars(connection)[_P + "clients"][asyncio.get_running_loop()] = client
    return connection, client


def _start_loop_thread(factory: Callable[[], Coroutine[Any, Any, None]], result: dict[str, Any]) -> threading.Thread:
    """Run ``factory()`` on a new event loop in a new thread; what it raises lands in ``result["error"]``."""

    def run() -> None:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(factory())
        except BaseException as exc:  # noqa: BLE001 - reported through `result`
            result["error"] = exc
        finally:
            loop.close()

    thread = threading.Thread(target=run)
    thread.start()
    return thread


@pytest.fixture
def clear_live_registry() -> Iterator[None]:
    """Forget every live reader a test registered, on every loop, owners included."""
    yield
    live._SUBSCRIBERS.clear()
    live._RECENTLY_KILLED.clear()
    live._OWNERS.clear()


@pytest.mark.usefixtures("clear_live_registry")
class TestLiveReadersByOwner:
    """Spec §6.5: the reader registry keeps its v0.28.0 shape; a parallel table records which connection owns each
    reader, so a connection releases its own readers only (M13). No server: fake clients."""

    async def test_readers_keep_their_shape_and_record_their_owner(self) -> None:
        client = _LiveClient()
        a = live.register_subscriber(client, "u-a", owner="md_a")
        b = live.register_subscriber(client, "u-b", owner="md_b")
        d = live.register_subscriber(client, "u-d")
        assert live._bucket() == {"u-a": [a], "u-b": [b], "u-d": [d]}
        assert client.live_queues == {"u-a": [a], "u-b": [b], "u-d": [d]}
        assert live.owner_of("u-a") == "md_a"
        assert live.owner_of("u-b") == "md_b"
        assert live.owner_of("u-d") == "default"
        # Owners are per event loop: outside one, nothing is owned.
        assert await asyncio.to_thread(live.owner_of, "u-a") is None

    async def test_an_owner_releases_only_its_readers(self) -> None:
        client = _LiveClient()
        a = live.register_subscriber(client, "u-a", owner="md_a")
        b = live.register_subscriber(client, "u-b", owner="md_b")
        d = live.register_subscriber(client, "u-d")
        live.close_all_subscribers(owner="md_a")
        assert a.get_nowait() is live._STREAM_END
        for waiting in (b, d):
            with pytest.raises(asyncio.QueueEmpty):
                waiting.get_nowait()
        assert "u-a" in live._killed()
        assert "u-b" not in live._killed()
        assert "u-d" not in live._killed()
        assert live._bucket() == {"u-b": [b], "u-d": [d]}
        assert "u-a" not in live._OWNERS[asyncio.get_running_loop()]
        assert live.owner_of("u-a") is None
        assert live.owner_of("u-b") == "md_b"
        # A reader registered without an owner belongs to the default connection.
        live.close_all_subscribers(owner="default")
        assert d.get_nowait() is live._STREAM_END
        assert live._bucket() == {"u-b": [b]}

    async def test_no_owner_releases_every_reader(self) -> None:
        client = _LiveClient()
        queues = [live.register_subscriber(client, f"u-{owner}", owner=owner) for owner in ("md_a", "md_b", "default")]
        queues.append(live.register_subscriber(client, "u-plain"))
        live.close_all_subscribers()
        assert all(queue.get_nowait() is live._STREAM_END for queue in queues)
        assert live._bucket() == {}
        assert live._OWNERS.get(asyncio.get_running_loop(), {}) == {}
        assert {"u-md_a", "u-md_b", "u-default", "u-plain"} <= set(live._killed())

    async def test_release_and_unregister_drop_the_owner(self) -> None:
        loop = asyncio.get_running_loop()
        client = _LiveClient()
        b = live.register_subscriber(client, "u-b", owner="md_b")
        live.release_subscribers("u-b")
        assert b.get_nowait() is live._STREAM_END
        assert live.owner_of("u-b") is None
        assert "u-b" not in live._OWNERS[loop]

        first = live.register_subscriber(client, "u-c", owner="md_a")
        second = live.register_subscriber(client, "u-c", owner="md_a")
        live.unregister_subscriber(client, "u-c", first)
        assert live.owner_of("u-c") == "md_a"  # another reader of the uuid is still registered
        live.unregister_subscriber(client, "u-c", second)
        assert live.owner_of("u-c") is None
        assert "u-c" not in live._OWNERS[loop]

        assert live.owner_of("never-registered") is None
        live.register_subscriber(client, "u-e", owner="md_a")
        live._SUBSCRIBERS.clear()  # as a test that clears the registry directly
        assert live._OWNERS[loop]["u-e"] == "md_a"
        assert live.owner_of("u-e") is None  # a stale owner routes nothing

    async def test_every_loop_releases_that_owners_readers_on_another_loop(self) -> None:
        parked = threading.Event()
        result: dict[str, Any] = {}

        async def read_on_other_loop() -> None:
            client = _LiveClient()
            mine = live.register_subscriber(client, "t-a", owner="md_a")
            theirs = live.register_subscriber(client, "t-b", owner="md_b")
            parked.set()
            result["mine"] = await asyncio.wait_for(mine.get(), timeout=10)
            # The release is one callback on this loop: it has run in full once the read above returns.
            result["theirs_waiting"] = theirs.empty()
            result["owners"] = (live.owner_of("t-a"), live.owner_of("t-b"))

        thread = _start_loop_thread(read_on_other_loop, result)
        assert await asyncio.to_thread(parked.wait, 10)
        client = _LiveClient()
        here_a = live.register_subscriber(client, "m-a", owner="md_a")
        here_b = live.register_subscriber(client, "m-b", owner="md_b")
        live.close_all_subscribers(owner="md_a", every_loop=True)
        assert here_a.get_nowait() is live._STREAM_END
        assert here_b.empty()
        await asyncio.to_thread(thread.join, 15)
        assert not thread.is_alive()
        assert "error" not in result, result.get("error")
        assert result["mine"] is live._STREAM_END
        assert result["theirs_waiting"] is True
        assert result["owners"] == (None, "md_b")

    async def test_every_loop_from_a_thread_without_a_running_loop(self) -> None:
        client = _LiveClient()
        a = live.register_subscriber(client, "m-a", owner="md_a")
        b = live.register_subscriber(client, "m-b", owner="md_b")
        # Without every_loop there is no loop to release: as in v0.28.0, that is an error.
        with pytest.raises(RuntimeError):
            await asyncio.to_thread(live.close_all_subscribers, owner="md_a")
        assert a.empty()
        await asyncio.to_thread(live.close_all_subscribers, owner="md_a", every_loop=True)
        # Handed to this loop with call_soon_threadsafe: it runs once the test yields to the loop.
        assert await asyncio.wait_for(a.get(), timeout=10) is live._STREAM_END
        assert b.empty()
        assert live.owner_of("m-a") is None
        assert live.owner_of("m-b") == "md_b"

    async def test_open_stream_and_rebind_keep_the_owner(self) -> None:
        first, second = _LiveClient(), _LiveClient()
        iterator = live.open_stream(first, "u-c", owner="md_a")
        assert live.owner_of("u-c") == "md_a"
        iterator.rebind(second, "u-c")
        assert live.owner_of("u-c") == "md_a"
        iterator.rebind(first, "u-c2")  # a resubscribe: a new uuid
        assert live.owner_of("u-c") is None
        assert live.owner_of("u-c2") == "md_a"
        plain = live.open_stream(first, "u-p")
        assert live.owner_of("u-p") == "default"
        await iterator.aclose()
        await plain.aclose()
        assert live.owner_of("u-c2") is None
        assert live.owner_of("u-p") is None


@pytest.mark.usefixtures("clear_live_registry")
class TestLiveOwnerRouting:
    """AC-5 owner rule (no server, fake clients): ``subscribe_live()``/``kill()`` on the router reach the connection
    whose stream opened the uuid; a connection's teardown releases its own readers only."""

    async def test_subscribe_live_and_kill_reach_the_owner_of_the_uuid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, a_client = _named_connection(monkeypatch, "md_a")
        md_b, b_client = _named_connection(monkeypatch, "md_b")
        live.register_subscriber(a_client, "u-a", owner="md_a")
        live.register_subscriber(a_client, "u-a2", owner="md_a")
        live.register_subscriber(a_client, "u-gone", owner="md_gone")
        default_reader = live.register_subscriber(_LiveClient(), "u-d")
        token = connection_manager._ACTIVE.set("md_b")
        try:
            # The owner first, whatever connection is current.
            reader = SurrealDBConnectionManager.subscribe_live("u-a")
            assert reader._client is a_client
            assert live.owner_of("u-a") == "md_a"
            # No reader of the uuid on this loop: the current connection, which owns the new reader.
            other = SurrealDBConnectionManager.subscribe_live("u-new")
            assert other._client is b_client
            assert live.owner_of("u-new") == "md_b"

            await SurrealDBConnectionManager.kill("u-a")
            assert a_client.killed == ["u-a"]
            assert reader._queue is not None and reader._queue.get_nowait() is live._STREAM_END
            # An owner that is no registered connection: the current one.
            await SurrealDBConnectionManager.kill("u-gone")
            assert b_client.killed == ["u-gone"]
            # The default's reader: the default, which has no client on this loop — released, nothing sent.
            await SurrealDBConnectionManager.kill("u-d")
            assert default_reader.get_nowait() is live._STREAM_END
            assert a_client.killed == ["u-a"]
            assert b_client.killed == ["u-gone"]
            # On a connection class, never re-routed.
            await md_b.kill("u-a2")
            assert b_client.killed == ["u-gone", "u-a2"]
            assert a_client.killed == ["u-a"]
            # The uuid given by keyword is read too.
            live.register_subscriber(a_client, "u-kw", owner="md_a")
            await SurrealDBConnectionManager.kill(query_uuid="u-kw")
            assert a_client.killed == ["u-a", "u-kw"]
        finally:
            connection_manager._ACTIVE.reset(token)

    async def test_close_connection_releases_its_own_readers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        md_a, a_client = _named_connection(monkeypatch, "md_a")
        _, b_client = _named_connection(monkeypatch, "md_b")
        a = live.register_subscriber(a_client, "u-a", owner="md_a")
        b = live.register_subscriber(b_client, "u-b", owner="md_b")
        d = live.register_subscriber(_LiveClient(), "u-d")
        await md_a.close_connection()
        assert a.get_nowait() is live._STREAM_END
        assert b.empty()
        assert d.empty()
        assert vars(md_a)[_P + "clients"].get(asyncio.get_running_loop()) is None
        # The default's close: the readers registered without an owner, and no other.
        monkeypatch.setattr(default_connection_class(), _P + "url", "ws://127.0.0.1:9/rpc")
        await SurrealDBConnectionManager.close_connection()
        assert d.get_nowait() is live._STREAM_END
        assert b.empty()
        assert live.owner_of("u-b") == "md_b"

    async def test_close_all_connections_per_connection_then_on_the_router(self, monkeypatch: pytest.MonkeyPatch) -> None:
        md_a, a_client = _named_connection(monkeypatch, "md_a")
        _, b_client = _named_connection(monkeypatch, "md_b")
        parked, first_release = threading.Event(), threading.Event()
        result: dict[str, Any] = {}

        async def read_on_other_loop() -> None:
            client = _LiveClient()
            mine = live.register_subscriber(client, "t-a", owner="md_a")
            theirs = live.register_subscriber(client, "t-b", owner="md_b")
            parked.set()
            result["mine"] = await asyncio.wait_for(mine.get(), timeout=10)
            result["theirs_waiting"] = theirs.empty()
            first_release.set()
            result["theirs"] = await asyncio.wait_for(theirs.get(), timeout=10)

        thread = _start_loop_thread(read_on_other_loop, result)
        assert await asyncio.to_thread(parked.wait, 10)
        a = live.register_subscriber(a_client, "u-a", owner="md_a")
        b = live.register_subscriber(b_client, "u-b", owner="md_b")
        gone = live.register_subscriber(_LiveClient(), "u-gone", owner="md_gone")

        await md_a.close_all_connections()
        assert a.get_nowait() is live._STREAM_END
        assert b.empty()
        assert gone.empty()
        assert await asyncio.to_thread(first_release.wait, 10)
        assert result["theirs_waiting"] is True

        # On the router: every registered connection, then whatever reader none of them owns.
        await SurrealDBConnectionManager.close_all_connections()
        assert b.get_nowait() is live._STREAM_END
        assert gone.get_nowait() is live._STREAM_END
        await asyncio.to_thread(thread.join, 15)
        assert not thread.is_alive()
        assert "error" not in result, result.get("error")
        assert result["mine"] is live._STREAM_END
        assert result["theirs"] is live._STREAM_END


class TestRouterStateE2E:
    """AC-2: after a scenario on the default connection, the router still holds only its template (``mem://``)."""

    async def test_the_router_keeps_no_state(self) -> None:
        default = SurrealDBConnectionManager._manager_for("default")
        SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="t")
        try:
            client = await SurrealDBConnectionManager.get_client()
            assert await SurrealDBConnectionManager.get_client() is client
            async with SurrealDBConnectionManager.transaction() as tx:
                await tx.add("UPSERT md_item:1 SET v = 1;")
            assert SurrealDBConnectionManager.is_connected()
            assert vars(default)[_P + "url"] == "mem://"
            assert vars(default)[_P + "embedded_client"] is client
            for key, factory in connection_manager._STATE:
                assert _is_template(vars(SurrealDBConnectionManager)[_P + key], factory()), key
        finally:
            await SurrealDBConnectionManager.unset_connection()
        assert vars(default)[_P + "url"] is None
        for key, factory in connection_manager._STATE:
            assert _is_template(vars(SurrealDBConnectionManager)[_P + key], factory()), key


# === The registry (spec §5.1, §5.2, §6.1) =====================================================================

#: An address nothing listens on: the unit tests below never open a server client.
_UNREACHABLE = "ws://127.0.0.1:9/rpc"

#: The two ways a test changes a connection's settings under its holders (D7).
_CHANGES = ("re-register", "remove")

#: The transports of the line under test, and the embedded engine.
_KINDS = ("ws", "http", "mem")


def _url(kind: str) -> str:
    """The URL of the server line under test (``SURREALDB_PORT``) for *kind* ``ws`` or ``http``."""
    host = os.environ.get("SURREALDB_HOST", "localhost")
    port = os.environ.get("SURREALDB_PORT", "8000")
    return f"ws://{host}:{port}/rpc" if kind == "ws" else f"http://{host}:{port}"


def _register(kind: str, name: str, *, database: str) -> None:
    """Register *name* on database *database* of namespace ``ns``: on the line under test, or on ``mem://``."""
    if kind == "mem":
        SurrealDBConnectionManager.add_connection(name, url="mem://", namespace="ns", database=database)
    else:
        SurrealDBConnectionManager.add_connection(
            name, url=_url(kind), user="root", password="root", namespace="ns", database=database
        )


async def _change(change: str, register: Callable[[], None]) -> None:
    """Re-register ``md_a`` with other settings (*register*), or remove it."""
    if change == "re-register":
        register()
    else:
        await SurrealDBConnectionManager.remove_connection("md_a")


async def _drain_background() -> None:
    """Wait for the manager's fire-and-forget closes started on this event loop."""
    loop = asyncio.get_running_loop()
    tasks = [task for task in list(connection_manager._BACKGROUND) if task.get_loop() is loop]
    await asyncio.gather(*tasks, return_exceptions=True)


@contextlib.asynccontextmanager
async def _root(database: str) -> AsyncIterator[Any]:
    """A raw SDK client signed in as root on database *database* of namespace ``ns`` (never the ORM's)."""
    client = AsyncSurreal(_url("ws"))
    await client.connect(_url("ws"))
    try:
        await client.signin({"username": "root", "password": "root"})
        await client.use("ns", database)
        yield client
    finally:
        await client.close()


async def _rows_of(client: Any, table: str) -> list[Any]:
    """The rows of *table* — none when it does not exist (SurrealDB 3.x refuses to select a missing table)."""
    info = await client.query("INFO FOR DB;", {})
    if table not in (info.get("tables") or {}):
        return []
    return list(await client.query(f"SELECT * FROM {table};", {}))


async def _root_rows(database: str, table: str) -> list[Any]:
    """What a root client reads from *table* of database *database*: proof of where a write landed."""
    async with _root(database) as client:
        return await _rows_of(client, table)


async def _engine_rows(client: Any, database: str, table: str) -> list[Any]:
    """The rows of *table* in *database* of an embedded engine, through a request-scoped ``USE DB`` (spec M5)."""
    raw = await client.query_raw(f"USE DB {database}; SELECT * FROM {table};", {})
    return list(raw["result"][-1]["result"])


def _values(rows: list[Any]) -> list[Any]:
    return sorted(row["v"] for row in rows)


async def _reset_databases(*, define: bool) -> None:
    """Remove the databases ``md_a`` and ``md_b`` of the line under test, and define them again when asked."""
    async with _root("db") as client:
        for database in ("md_a", "md_b"):
            await client.query(f"REMOVE DATABASE IF EXISTS {database};", {})
            if define:
                await client.query(f"DEFINE DATABASE {database};", {})


@contextlib.asynccontextmanager
async def _databases(kind: str, *names: str) -> AsyncIterator[None]:
    """Register each of *names* on the database of the same name (``ws``/``http``: on the line under test, the
    databases ``md_a`` and ``md_b`` removed before and after; ``mem``: an engine of its own)."""
    server = kind != "mem"
    if server:
        await _reset_databases(define=True)
    for name in names:
        _register(kind, name, database=name)
    try:
        yield
    finally:
        for name in names:
            await SurrealDBConnectionManager.remove_connection(name)
        if server:
            await _reset_databases(define=False)


def _record_raw(monkeypatch: pytest.MonkeyPatch, client: Any) -> list[str]:
    """Record the statement of each ``query_raw()`` *client* sends from now on (the batch of a buffered commit,
    the writes of an interactive transaction). Patched on the client's class and keyed on ``id(client)``, as
    ``tests.test_time_travel._record_queries`` does."""
    sent: list[str] = []
    cls = type(client)
    real = cls.query_raw
    owner = id(client)

    async def recording(self: Any, query: str, *args: Any, **kwargs: Any) -> Any:
        if id(self) == owner:
            sent.append(query)
        return await real(self, query, *args, **kwargs)

    monkeypatch.setattr(cls, "query_raw", recording)
    return sent


class _FakeSocket:
    """A transport whose ``close()`` records the event loop it ran on."""

    def __init__(self) -> None:
        self.closed_on: list[asyncio.AbstractEventLoop] = []
        self.closed = threading.Event()

    async def close(self) -> None:
        self.closed_on.append(asyncio.get_running_loop())
        self.closed.set()


class _FakeServerClient:
    """A WebSocket-like server client: a transport, a live-query table and a ``close()`` that counts its calls."""

    def __init__(self) -> None:
        self.socket = _FakeSocket()
        self.live_queues: dict[str, list[asyncio.Queue[Any]]] = {}
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _HttpLikeClient:
    """The SDK 2.0 HTTP client's shape (spec M17): no transport, and a ``close()`` that raises."""

    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        raise NotImplementedError


class _RecordingSet(set):  # type: ignore[type-arg]
    """``connection_manager._BACKGROUND`` for one test: it also keeps every task ever added."""

    def __init__(self) -> None:
        super().__init__()
        self.recorded: list[asyncio.Task[Any]] = []

    def add(self, task: Any) -> None:
        self.recorded.append(task)
        super().add(task)


@pytest_asyncio.fixture(loop_scope="function")
async def unset_default() -> AsyncIterator[None]:
    """The default connection unset for the test, its settings put back after it."""
    saved = default_connection_class()._settings()
    await SurrealDBConnectionManager.unset_connection()
    yield
    await SurrealDBConnectionManager.unset_connection()
    if saved[0] is not None:
        url, user, password, namespace, database = saved
        SurrealDBConnectionManager.set_connection(url, user, password, namespace, database)


class TestConnectionConfig:
    """Spec §5.2, AC-3: the full ORM's frozen dataclass, without ``protocol``, password out of ``repr``."""

    def test_frozen(self) -> None:
        config = ConnectionConfig("mem://", None, None, "t", "t")
        with pytest.raises(dataclasses.FrozenInstanceError):
            config.url = "x"  # type: ignore[misc]

    def test_built_by_position_or_by_keyword(self) -> None:
        assert ConnectionConfig("mem://", None, None, "t", "t") == ConnectionConfig(
            url="mem://", user=None, password=None, namespace="t", database="t"
        )

    def test_user_and_password_are_never_left_out(self) -> None:
        with pytest.raises(TypeError):
            ConnectionConfig(url="mem://", password=None, namespace="t", database="t")  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            ConnectionConfig(url="mem://", user=None, namespace="t", database="t")  # type: ignore[call-arg]

    def test_the_full_orms_fields_in_order_without_defaults(self) -> None:
        fields = dataclasses.fields(ConnectionConfig)
        assert [field.name for field in fields] == ["url", "user", "password", "namespace", "database"]
        for field in fields:
            assert field.default is dataclasses.MISSING, field.name
            assert field.default_factory is dataclasses.MISSING, field.name

    def test_the_password_stays_out_of_repr_and_counts_for_equality(self) -> None:
        config = ConnectionConfig("ws://db:8000/rpc", "root", "secret", "shop", "main")
        assert "secret" not in repr(config)
        assert "root" in repr(config)
        assert config != ConnectionConfig("ws://db:8000/rpc", "root", "other", "shop", "main")
        assert config == ConnectionConfig("ws://db:8000/rpc", "root", "secret", "shop", "main")

    def test_exported(self) -> None:
        assert surreal_orm_lite.ConnectionConfig is ConnectionConfig
        names = surreal_orm_lite.__all__
        assert names.index("ConnectionConfig") == names.index("SurrealDBConnectionManager") + 1


@pytest.mark.usefixtures("unset_default")
class TestRegistry:
    """AC-3: ``add_connection()``, ``remove_connection()``, ``get_config()``, ``list_connections()`` and the ``name``
    parameters (``mem://``, no server)."""

    def test_invalid_arguments_change_nothing(self) -> None:
        add = SurrealDBConnectionManager.add_connection
        with pytest.raises(TypeError, match=r"^Connection name must be a str, not int\.$"):
            add(1, url="mem://", namespace="t", database="t")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match=r"^Connection name must not be empty\.$"):
            add("", url="mem://", namespace="t", database="t")
        for url, namespace, database in (("", "t", "t"), ("mem://", "", "t"), ("mem://", "t", "")):
            with pytest.raises(ValueError, match="needs a non-empty url, namespace and database"):
                add("md_a", url=url, namespace=namespace, database=database)
        for user, password in ((None, None), ("root", None), (None, "root"), ("", "root")):
            with pytest.raises(ValueError, match="a server URL needs user and password"):
                add("md_a", url=_UNREACHABLE, user=user, password=password, namespace="t", database="t")
        with pytest.raises(SurrealDbConnectionError) as info:
            add("md_a", url="rocksdb://x", namespace="t", database="t")
        assert info.value.retryable is False  # type: ignore[attr-defined]
        assert SurrealDBConnectionManager.list_connections() == []
        assert set(connection_manager._REGISTRY) == {"default"}

    def test_registration_listing_and_config(self) -> None:
        SurrealDBConnectionManager.add_connection("md_a", url="mem://", namespace="t", database="a")
        assert SurrealDBConnectionManager.list_connections() == ["md_a"]
        assert SurrealDBConnectionManager.get_config("md_a") == ConnectionConfig("mem://", None, None, "t", "a")
        assert SurrealDBConnectionManager.get_config("nope") is None
        assert SurrealDBConnectionManager.get_config() is None
        # Listed once its URL is set, its config only once it is complete.
        SurrealDBConnectionManager.set_connection(url="ws://x", namespace="n", database=None)
        assert SurrealDBConnectionManager.list_connections() == ["default", "md_a"]
        assert SurrealDBConnectionManager.get_config("default") is None
        SurrealDBConnectionManager.set_connection(url="mem://", namespace="n", database="d")
        assert SurrealDBConnectionManager.get_config() == ConnectionConfig("mem://", None, None, "n", "d")
        SurrealDBConnectionManager.add_connection("md_b", url=_UNREACHABLE, user="u", password="p", namespace="t", database="b")
        assert SurrealDBConnectionManager.list_connections() == ["default", "md_a", "md_b"]
        assert SurrealDBConnectionManager.get_config("md_b") == ConnectionConfig(_UNREACHABLE, "u", "p", "t", "b")
        with pytest.raises(TypeError, match="Connection name must be a str"):
            SurrealDBConnectionManager.get_config(1)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="Connection name must not be empty"):
            SurrealDBConnectionManager.get_config("")

    def test_the_same_settings_again_change_nothing(self) -> None:
        SurrealDBConnectionManager.add_connection("md_a", url=_UNREACHABLE, user="u", password="p", namespace="t", database="a")
        conn = SurrealDBConnectionManager._manager_for("md_a")
        generation = conn._settings_generation()
        setattr(conn, _P + "session_token", "token")
        SurrealDBConnectionManager.add_connection("md_a", url=_UNREACHABLE, user="u", password="p", namespace="t", database="a")
        assert SurrealDBConnectionManager._manager_for("md_a") is conn
        assert conn.get_session_token() == "token"
        assert conn._settings_generation() == generation
        # Any of the five differing is a change: the password too.
        SurrealDBConnectionManager.add_connection("md_a", url=_UNREACHABLE, user="u", password="q", namespace="t", database="a")
        assert conn.get_session_token() is None
        assert conn._settings_generation() == generation + 1

    @pytest.mark.usefixtures("clear_live_registry")
    async def test_unknown_names(self) -> None:
        loop = asyncio.get_running_loop()
        SurrealDBConnectionManager.set_connection(url=_UNREACHABLE, user="u", password="p", namespace="t", database="d")
        SurrealDBConnectionManager.add_connection("md_a", url=_UNREACHABLE, user="u", password="p", namespace="t", database="a")
        for name in ("default", "md_a"):
            client = _FakeServerClient()
            vars(SurrealDBConnectionManager._manager_for(name))[_P + "clients"][loop] = client
            live.register_subscriber(client, f"u-{name}", owner=name)

        def snapshot() -> Any:
            return (
                {
                    name: (dict(vars(conn)[_P + "clients"]), conn._settings())
                    for name, conn in connection_manager._REGISTRY.items()
                },
                {uuid: list(queues) for uuid, queues in live._bucket().items()},
            )

        before = snapshot()
        with pytest.raises(ValueError, match=r"^Unknown connection: 'nope'\. Register it with add_connection\(\) first\.$"):
            await SurrealDBConnectionManager.get_client("nope")
        with pytest.raises(ValueError, match="Unknown connection: 'nope'"):
            SurrealDBConnectionManager.transaction("nope")
        assert await SurrealDBConnectionManager.close_connection("nope") is None
        assert await SurrealDBConnectionManager.close_connection(name="nope") is None
        assert snapshot() == before
        with pytest.raises(TypeError, match=r"^Connection name must be a str, not int\.$"):
            await SurrealDBConnectionManager.close_connection(1)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="Connection name must not be empty"):
            await SurrealDBConnectionManager.close_connection("")
        with pytest.raises(TypeError, match="Connection name must be a str"):
            await SurrealDBConnectionManager.get_client(1)  # type: ignore[arg-type]
        assert snapshot() == before

    async def test_name_by_position_and_by_keyword(self) -> None:
        SurrealDBConnectionManager.add_connection("md_a", url="mem://", namespace="t", database="a")
        conn = SurrealDBConnectionManager._manager_for("md_a")
        client = await SurrealDBConnectionManager.get_client("md_a")
        assert await SurrealDBConnectionManager.get_client(name="md_a") is client
        assert vars(conn)[_P + "embedded_client"] is client
        async with SurrealDBConnectionManager.transaction("md_a") as tx:
            assert tx._client is client
        async with SurrealDBConnectionManager.transaction(name="md_a") as tx:
            assert tx._client is client
        # The default is not configured: only the name reached md_a.
        with pytest.raises(ValueError, match=r"^Connection not been set\.$"):
            await SurrealDBConnectionManager.get_client()
        await SurrealDBConnectionManager.close_connection("md_a")
        assert conn.is_connected() is False

    async def test_close_connection_is_silent_for_a_removed_active_name(self) -> None:
        """P6: the active name comes from ``using()`` (here its private ``ContextVar``) and was removed meanwhile."""
        loop = asyncio.get_running_loop()
        SurrealDBConnectionManager.set_connection(url=_UNREACHABLE, user="u", password="p", namespace="t", database="d")
        default_client = _FakeServerClient()
        vars(default_connection_class())[_P + "clients"][loop] = default_client
        SurrealDBConnectionManager.add_connection("md_a", url="mem://", namespace="t", database="a")
        token = connection_manager._ACTIVE.set("md_a")
        try:
            await SurrealDBConnectionManager.remove_connection("md_a")
            assert await SurrealDBConnectionManager.close_connection() is None
            assert vars(default_connection_class())[_P + "clients"][loop] is default_client
            assert default_client.close_calls == 0
            with pytest.raises(ValueError, match=r"^Unknown connection: 'md_a'\."):
                await SurrealDBConnectionManager.get_client()
        finally:
            connection_manager._ACTIVE.reset(token)

    async def test_remove_connection(self) -> None:
        assert await SurrealDBConnectionManager.remove_connection("nope") is None
        with pytest.raises(TypeError, match="Connection name must be a str"):
            await SurrealDBConnectionManager.remove_connection(1)  # type: ignore[arg-type]
        SurrealDBConnectionManager.add_connection("md_a", url="mem://", namespace="t", database="a")
        conn = SurrealDBConnectionManager._manager_for("md_a")
        await SurrealDBConnectionManager.get_client("md_a")
        assert conn.is_connected() is True
        generation = conn._settings_generation()
        await SurrealDBConnectionManager.remove_connection("md_a")
        assert conn.is_connected() is False
        assert conn.get_url() is None
        assert conn._settings_generation() == generation + 1
        assert "md_a" not in SurrealDBConnectionManager.list_connections()
        assert "md_a" not in connection_manager._REGISTRY
        with pytest.raises(ValueError, match="Unknown connection: 'md_a'"):
            await SurrealDBConnectionManager.get_client("md_a")

    async def test_removing_the_default_unsets_it(self) -> None:
        SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="d")
        default = default_connection_class()
        await SurrealDBConnectionManager.get_client()
        generation = default._settings_generation()
        await SurrealDBConnectionManager.remove_connection("default")
        assert connection_manager._REGISTRY["default"] is default
        assert default.get_url() is None
        assert default.is_connected() is False
        assert default._settings_generation() == generation
        assert "default" not in SurrealDBConnectionManager.list_connections()

    async def test_two_mem_names_are_two_databases(self) -> None:
        """M6: each ``mem://`` connection is an engine, so a database, of its own."""
        SurrealDBConnectionManager.add_connection("md_a", url="mem://", namespace="t", database="t")
        SurrealDBConnectionManager.add_connection("md_b", url="mem://", namespace="t", database="t")
        a = await SurrealDBConnectionManager.get_client("md_a")
        b = await SurrealDBConnectionManager.get_client("md_b")
        assert a is not b
        await a.query("UPSERT md_item:1 SET v = 'a';", {})
        assert _values(await _rows_of(a, "md_item")) == ["a"]
        assert await _rows_of(b, "md_item") == []


@pytest.mark.usefixtures("clear_live_registry")
class TestReRegistration:
    """D7, AC-3, AC-28 item 3: other settings retire the connection's server clients on every event loop and refuse
    a client still opening on the old ones (R11). No server: fake clients."""

    async def test_other_settings_retire_every_loops_clients(self) -> None:
        loop = asyncio.get_running_loop()
        SurrealDBConnectionManager.add_connection("md_a", url="mem://", namespace="t", database="a")
        conn = SurrealDBConnectionManager._manager_for("md_a")
        clients = vars(conn)[_P + "clients"]
        here = _FakeServerClient()
        clients[loop] = here
        reader = live.register_subscriber(here, "u-a", owner="md_a")
        closed_loop = asyncio.new_event_loop()
        closed_loop.close()
        gone = _FakeServerClient()
        clients[closed_loop] = gone
        setattr(conn, _P + "session_token", "token")
        vars(conn)[_P + "function_signatures"][("u", "n", "d", "fn::x")] = ("a",)
        generation = conn._settings_generation()

        there = _FakeServerClient()
        parked = threading.Event()
        result: dict[str, Any] = {}

        async def keep_a_client_on_another_loop() -> None:
            result["loop"] = asyncio.get_running_loop()
            clients[result["loop"]] = there
            parked.set()
            # The loop keeps running until the retirement has closed the socket here.
            result["closed"] = await asyncio.to_thread(there.socket.closed.wait, 10)
            current = asyncio.current_task()
            await asyncio.gather(*(t for t in asyncio.all_tasks() if t is not current))

        thread = _start_loop_thread(keep_a_client_on_another_loop, result)
        assert await asyncio.to_thread(parked.wait, 10)

        SurrealDBConnectionManager.add_connection("md_a", url="mem://", namespace="t", database="other")

        assert len(clients) == 0
        assert conn._settings_generation() == generation + 1
        assert reader.get_nowait() is live._STREAM_END
        assert conn.get_session_token() is None
        assert conn.function_signature_cache_size() == 0
        await _drain_background()
        assert here.socket.closed_on == [loop]
        await asyncio.to_thread(thread.join, 15)
        assert not thread.is_alive()
        assert "error" not in result, result.get("error")
        assert result["closed"] is True
        assert there.socket.closed_on == [result["loop"]]
        assert gone.socket.closed_on == []
        # The transport only: a client the application kept fails on its next request instead of reopening.
        assert here.close_calls == there.close_calls == gone.close_calls == 0

    async def test_from_a_thread_with_no_running_loop(self) -> None:
        loop = asyncio.get_running_loop()
        SurrealDBConnectionManager.add_connection("md_a", url="mem://", namespace="t", database="a")
        conn = SurrealDBConnectionManager._manager_for("md_a")
        here = _FakeServerClient()
        vars(conn)[_P + "clients"][loop] = here
        reader = live.register_subscriber(here, "u-a", owner="md_a")
        generation = conn._settings_generation()
        await asyncio.to_thread(
            SurrealDBConnectionManager.add_connection, "md_a", url="mem://", namespace="t", database="other"
        )
        assert loop not in vars(conn)[_P + "clients"]
        assert conn._settings_generation() == generation + 1
        assert await asyncio.to_thread(here.socket.closed.wait, 10)
        assert here.socket.closed_on == [loop]
        assert await asyncio.wait_for(reader.get(), 10) is live._STREAM_END
        await _drain_background()

    @staticmethod
    def _park_the_open(
        monkeypatch: pytest.MonkeyPatch, conn: type[SurrealDBConnectionManager], make: Callable[[], Any]
    ) -> tuple[asyncio.Event, asyncio.Event, list[Any]]:
        """Replace *conn*'s ``_open_client`` by one that waits for a gate, then returns ``make()``."""
        entered, gate = asyncio.Event(), asyncio.Event()
        opened: list[Any] = []

        async def fake_open(cls: Any, *, signin_as_configured: bool) -> Any:
            entered.set()
            await gate.wait()
            client = make()
            opened.append(client)
            return client

        monkeypatch.setattr(conn, "_open_client", classmethod(fake_open))
        return entered, gate, opened

    @pytest.mark.parametrize("change", _CHANGES)
    async def test_a_client_opening_on_the_old_settings_raises_r11(self, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
        def register(database: str) -> None:
            SurrealDBConnectionManager.add_connection(
                "md_a", url=_UNREACHABLE, user="u", password="p", namespace="t", database=database
            )

        register("a")
        conn = SurrealDBConnectionManager._manager_for("md_a")
        entered, gate, opened = self._park_the_open(monkeypatch, conn, _FakeServerClient)
        task = asyncio.create_task(SurrealDBConnectionManager.get_client("md_a"))
        await entered.wait()
        await _change(change, lambda: register("b"))
        gate.set()
        with pytest.raises(SurrealDbConnectionError, match="registered again with other settings") as info:
            await task
        assert info.value.retryable is True  # type: ignore[attr-defined]
        assert str(info.value) == (
            "Connection 'md_a' was removed or registered again with other settings while a client was opening on "
            "the old ones; that client was closed unused. Retry the call."
        )
        assert asyncio.get_running_loop() not in vars(conn)[_P + "clients"]
        await _drain_background()
        assert opened[0].close_calls == 1

    async def test_the_unused_client_is_closed_quietly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """P7: the SDK 2.0 HTTP client's ``close()`` raises; the background close keeps that to itself."""
        recording = _RecordingSet()
        monkeypatch.setattr(connection_manager, "_BACKGROUND", recording)
        SurrealDBConnectionManager.add_connection("md_a", url=_UNREACHABLE, user="u", password="p", namespace="t", database="a")
        conn = SurrealDBConnectionManager._manager_for("md_a")
        entered, gate, opened = self._park_the_open(monkeypatch, conn, _HttpLikeClient)
        task = asyncio.create_task(SurrealDBConnectionManager.get_client("md_a"))
        await entered.wait()
        SurrealDBConnectionManager.add_connection("md_a", url=_UNREACHABLE, user="u", password="p", namespace="t", database="b")
        gate.set()
        with pytest.raises(SurrealDbConnectionError, match="registered again with other settings") as info:
            await task
        assert info.value.retryable is True  # type: ignore[attr-defined]
        assert recording.recorded
        await asyncio.gather(*recording.recorded, return_exceptions=True)
        for background in recording.recorded:
            assert background.done()
            assert not background.cancelled()
            assert background.exception() is None
        assert opened[0].close_calls == 1

    @pytest.mark.usefixtures("unset_default")
    async def test_set_connection_on_the_default_keeps_its_clients(self) -> None:
        """v0.28.0 tolerance, kept: ``set_connection()`` retires nothing and bumps no generation (spec §14.6)."""
        loop = asyncio.get_running_loop()
        SurrealDBConnectionManager.set_connection(url=_UNREACHABLE, user="u", password="p", namespace="t", database="a")
        default = default_connection_class()
        client = _FakeServerClient()
        vars(default)[_P + "clients"][loop] = client
        generation = default._settings_generation()
        SurrealDBConnectionManager.set_connection(url=_UNREACHABLE, user="u", password="p", namespace="t", database="b")
        assert vars(default)[_P + "clients"][loop] is client
        assert default._settings_generation() == generation
        assert client.socket.closed_on == []


class TestRemoveOrder:
    """Spec §6.1: ``remove_connection()`` unregisters the name, then retires its clients, then unsets it."""

    async def test_unregistered_then_retired_then_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        SurrealDBConnectionManager.add_connection("md_a", url="mem://", namespace="t", database="a")
        conn = SurrealDBConnectionManager._manager_for("md_a")
        generation = conn._settings_generation()
        real_unset = conn.unset_connection
        seen: dict[str, Any] = {}

        async def recording_unset(cls: Any) -> None:
            seen["registered"] = "md_a" in connection_manager._REGISTRY
            seen["generation"] = conn._settings_generation()
            # The name is free again at once: registering it now makes a new connection.
            SurrealDBConnectionManager.add_connection("md_a", url="mem://", namespace="t", database="new")
            await real_unset()

        monkeypatch.setattr(conn, "unset_connection", classmethod(recording_unset))
        await SurrealDBConnectionManager.remove_connection("md_a")
        assert seen == {"registered": False, "generation": generation + 1}
        new = SurrealDBConnectionManager._manager_for("md_a")
        assert new is not conn
        assert SurrealDBConnectionManager.get_config("md_a") == ConnectionConfig("mem://", None, None, "t", "new")
        assert conn.get_url() is None


# === using() and the current connection (spec §5.3, §5.4, §5.7 R9 R10) ========================================

#: The lost-identity text of v0.28.0, byte for byte: the default connection keeps it (spec R10, D24).
_V028_LOST_IDENTITY = (
    "The SurrealDB connection on this event loop dropped while it was authenticated as an "
    "identity the connection manager does not store (signin/signup/authenticate with "
    "store=False), so a new connection cannot restore it — and answering as the configured "
    "user instead would silently change privileges. Sign in or authenticate again, call "
    "invalidate(), or reconnect()/close_connection() to continue as the configured user."
)


def _active() -> str | None:
    return SurrealDBConnectionManager.get_active_connection_name()


@pytest.mark.usefixtures("unset_default")
class TestUsing:
    """AC-4, spec §5.3, §5.4, D25, M9, P6: ``using()`` redirects a block of code to a named connection (``mem://``)."""

    async def test_validated_at_entry_before_the_block_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        SurrealDBConnectionManager.add_connection("md_a", url="mem://", namespace="t", database="a")
        # Registered but never configured (an embedded_database() block's name): not listed, so unknown here.
        monkeypatch.setitem(connection_manager._REGISTRY, "md_bare", connection_manager._new_connection_class("md_bare"))
        ran: list[Any] = []
        cases: list[tuple[Any, type[Exception], str]] = [
            (1, TypeError, r"^Connection name must be a str, not int\.$"),
            ("", ValueError, r"^Connection name must not be empty\.$"),
            ("nope", ValueError, r"^Unknown connection: 'nope'\. Register it with add_connection\(\) first\.$"),
            ("md_bare", ValueError, r"^Unknown connection: 'md_bare'\."),
            # The default is never unknown, except while it is not configured.
            ("default", ValueError, r"^Unknown connection: 'default'\."),
        ]
        for name, error, text in cases:
            block = SurrealDBConnectionManager.using(name)  # nothing is checked before the block is entered
            with pytest.raises(error, match=text):
                async with block:
                    ran.append(name)  # pragma: no cover - the block never runs
            assert _active() is None
        assert ran == []
        SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="d")
        async with SurrealDBConnectionManager.using("default"):
            assert _active() == "default"
        assert _active() is None

    async def test_nesting_and_exceptions_restore_the_outer_name(self) -> None:
        SurrealDBConnectionManager.add_connection("md_a", url="mem://", namespace="t", database="a")
        SurrealDBConnectionManager.add_connection("md_b", url="mem://", namespace="t", database="b")
        assert _active() is None
        async with SurrealDBConnectionManager.using("md_a"):
            assert _active() == "md_a"
            async with SurrealDBConnectionManager.using("md_b"):
                assert _active() == "md_b"
                async with SurrealDBConnectionManager.using("md_a"):
                    assert _active() == "md_a"
                assert _active() == "md_b"
            assert _active() == "md_a"
            with pytest.raises(RuntimeError, match="boom"):
                async with SurrealDBConnectionManager.using("md_b"):
                    assert _active() == "md_b"
                    raise RuntimeError("boom")
            assert _active() == "md_a"
            # A failed entry leaves the outer name as it was.
            with pytest.raises(ValueError, match="Unknown connection: 'nope'"):
                async with SurrealDBConnectionManager.using("nope"):
                    pass  # pragma: no cover
            assert _active() == "md_a"
        assert _active() is None
        with pytest.raises(KeyError):
            async with SurrealDBConnectionManager.using("md_a"):
                raise KeyError("x")
        assert _active() is None

    async def test_context_semantics(self) -> None:
        """M9: a task created inside keeps the name after the block; ``to_thread`` and ``gather`` children carry it;
        a new thread starts without it."""
        SurrealDBConnectionManager.add_connection("md_a", url="mem://", namespace="t", database="a")
        released = asyncio.Event()
        seen: dict[str, Any] = {}

        async def outlives_the_block() -> None:
            seen["task, in the block"] = _active()
            await released.wait()
            seen["task, after the block"] = _active()

        async def child() -> str | None:
            return _active()

        in_thread: dict[str, Any] = {}
        async with SurrealDBConnectionManager.using("md_a"):
            task = asyncio.create_task(outlives_the_block())
            seen["to_thread"] = await asyncio.to_thread(_active)
            seen["gather"] = await asyncio.gather(child(), child())
            thread = threading.Thread(target=lambda: in_thread.setdefault("name", _active()))
            thread.start()
            await asyncio.to_thread(thread.join, 10)
            assert not thread.is_alive()
        assert _active() is None
        released.set()
        await task
        assert seen == {
            "task, in the block": "md_a",
            "task, after the block": "md_a",
            "to_thread": "md_a",
            "gather": ["md_a", "md_a"],
        }
        assert in_thread == {"name": None}

    async def test_when_the_connection_is_resolved(self) -> None:
        """D25: a coroutine method resolves its connection when it starts running; a context-manager factory (and
        any plain method) at the call."""
        SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="d")
        SurrealDBConnectionManager.add_connection("md_a", url="mem://", namespace="t", database="a")
        default_client = await SurrealDBConnectionManager.get_client()
        a_client = await SurrealDBConnectionManager.get_client("md_a")
        assert default_client is not a_client
        created_outside = SurrealDBConnectionManager.get_client()
        async with SurrealDBConnectionManager.using("md_a"):
            created_inside = SurrealDBConnectionManager.get_client()
            factory_inside = SurrealDBConnectionManager.transaction()
            assert await SurrealDBConnectionManager.get_client() is a_client
            # Created outside, awaited inside: the block's connection.
            assert await created_outside is a_client
        # Created inside, awaited outside: the default.
        assert await created_inside is default_client
        # The factory resolved at the call: entered outside the block, it still runs on md_a.
        async with factory_inside as tx:
            assert tx._client is a_client
        async with SurrealDBConnectionManager.transaction() as tx:
            assert tx._client is default_client

    async def test_a_connection_removed_inside_its_block(self) -> None:
        """P6 with ``using()``: ``close_connection()`` returns ``None`` and closes nothing, ``get_client()`` raises R1,
        and the block still exits cleanly."""
        loop = asyncio.get_running_loop()
        SurrealDBConnectionManager.set_connection(url=_UNREACHABLE, user="u", password="p", namespace="t", database="d")
        default_client = _FakeServerClient()
        vars(default_connection_class())[_P + "clients"][loop] = default_client
        SurrealDBConnectionManager.add_connection("md_a", url="mem://", namespace="t", database="a")
        async with SurrealDBConnectionManager.using("md_a"):
            await SurrealDBConnectionManager.get_client()
            await SurrealDBConnectionManager.remove_connection("md_a")
            assert _active() == "md_a"
            assert await SurrealDBConnectionManager.close_connection() is None
            assert vars(default_connection_class())[_P + "clients"][loop] is default_client
            assert default_client.close_calls == 0
            with pytest.raises(ValueError, match=r"^Unknown connection: 'md_a'\. Register it with add_connection"):
                await SurrealDBConnectionManager.get_client()
            with pytest.raises(ValueError, match=r"^Unknown connection: 'md_a'\."):
                SurrealDBConnectionManager.get_url()
        assert _active() is None
        assert SurrealDBConnectionManager.get_url() == _UNREACHABLE


@pytest.mark.usefixtures("unset_default", "clear_live_registry")
class TestRoutingTable:
    """AC-5, spec §5.4: on the router, a method acts on the name given, else the ``using()`` block's connection, else
    the default — except ``set_connection()``/``unset_connection()`` (the default) and ``close_all_connections()``
    (every connection). ``mem://`` connections; the default on an address nothing listens on, never contacted."""

    @staticmethod
    def _connections() -> tuple[
        type[SurrealDBConnectionManager], type[SurrealDBConnectionManager], type[SurrealDBConnectionManager]
    ]:
        SurrealDBConnectionManager.set_connection(url=_UNREACHABLE, user="u", password="p", namespace="t", database="d")
        SurrealDBConnectionManager.add_connection("md_a", url="mem://", namespace="t", database="a")
        SurrealDBConnectionManager.add_connection("md_b", url="mem://", namespace="t", database="b")
        return (
            default_connection_class(),
            SurrealDBConnectionManager._manager_for("md_a"),
            SurrealDBConnectionManager._manager_for("md_b"),
        )

    async def test_the_current_methods_act_on_the_block_connection(self) -> None:
        default, md_a, md_b = self._connections()
        router = SurrealDBConnectionManager
        async with router.using("md_a"):
            # Settings, read.
            assert router.get_url() == router.get_connection_string() == "mem://"
            assert (router.get_namespace(), router.get_database(), router.get_user()) == ("t", "a", None)
            assert router.get_connection_kwargs() == {"url": "mem://", "user": None, "namespace": "t", "database": "a"}
            assert router.is_password_set() is False
            assert router.is_embedded() is True
            assert router.is_connection_set() is True
            router.require_connection()
            # The client, the connection state, a transaction.
            assert router.is_connected() is False
            client = await router.get_client()
            assert client is vars(md_a)[_P + "embedded_client"]
            assert router.is_connected() is True
            assert await router.validate_connection() is True
            async with router.transaction() as tx:
                assert tx._client is client
            with pytest.raises(SurrealDbError, match=r"ephemeral_client\(\) opens a second session"):
                async with router.ephemeral_client():
                    pass  # pragma: no cover
            # The session: the embedded owner answers, the default (never contacted) is not asked.
            assert await router.info() is None
            await router.invalidate()
            with pytest.raises(SurrealDbAuthenticationError):
                await router.authenticate("not-a-jwt")
            # A stored function, read and called on md_a.
            await client.query("DEFINE FUNCTION fn::md_ping() { RETURN 'md_a'; };", {})
            assert await router.call_function("md_ping") == "md_a"
            # reconnect() reopens md_a's engine and hands back its new client.
            again = await router.reconnect()
            assert again is not client
            assert again is vars(md_a)[_P + "embedded_client"]
            # Settings, written.
            await router.set_namespace("n2")
            await router.set_database("a2")
            await router.set_user("ua")
            await router.set_password("pa")
            await router.set_url("mem://")
            assert router.get_config("md_a") == ConnectionConfig("mem://", "ua", "pa", "n2", "a2")
            # The stored session and the function signatures.
            setattr(md_a, _P + "session_token", "a-token")
            setattr(md_a, _P + "refresh_token", "a-refresh")
            setattr(default, _P + "session_token", "d-token")
            vars(md_a)[_P + "function_signatures"][("mem://", "n2", "a2", "fn::x")] = ("x",)
            vars(default)[_P + "function_signatures"][(_UNREACHABLE, "t", "d", "fn::x")] = ("x",)
            assert router.is_authenticated() is True
            assert (router.get_session_token(), router.get_refresh_token()) == ("a-token", "a-refresh")
            assert router.function_signature_cache_size() == 1
            router.clear_function_signature_cache()
            router.clear_session()
            assert router.function_signature_cache_size() == 0
            assert router.is_authenticated() is False
            # close_connection() closes md_a's engine only.
            assert md_b.is_connected() is False
            await router.get_client("md_b")
            await router.close_connection()
            assert md_a.is_connected() is False
            assert md_b.is_connected() is True
        # Outside the block: the default, untouched.
        assert router.get_config("default") == ConnectionConfig(_UNREACHABLE, "u", "p", "t", "d")
        assert router.is_embedded() is False
        assert router.get_session_token() == "d-token"
        assert router.function_signature_cache_size() == 1
        assert router.is_connected() is False
        router.clear_session()

    async def test_an_explicit_name_wins_over_the_block(self) -> None:
        _, md_a, md_b = self._connections()
        router = SurrealDBConnectionManager
        async with router.using("md_a"):
            b_client = await router.get_client("md_b")
            assert b_client is vars(md_b)[_P + "embedded_client"]
            async with router.transaction("md_b") as tx:
                assert tx._client is b_client
            a_client = await router.get_client()
            assert a_client is not b_client
            await router.close_connection(name="md_b")
            assert md_b.is_connected() is False
            assert md_a.is_connected() is True

    async def test_set_and_unset_connection_act_on_the_default(self) -> None:
        self._connections()
        router = SurrealDBConnectionManager
        async with router.using("md_a"):
            router.set_connection(url="mem://", namespace="t", database="d2")
            assert router.get_database() == "a"
            assert router.get_config("default") == ConnectionConfig("mem://", None, None, "t", "d2")
            await router.unset_connection()
            assert router.get_config("default") is None
            assert router.get_config("md_a") == ConnectionConfig("mem://", None, None, "t", "a")
            assert router.get_url() == "mem://"
        assert router.get_url() is None
        assert router.list_connections() == ["md_a", "md_b"]

    async def test_close_all_connections_closes_every_connection(self) -> None:
        loop = asyncio.get_running_loop()
        default, md_a, md_b = self._connections()
        default_client = _FakeServerClient()
        vars(default)[_P + "clients"][loop] = default_client
        await SurrealDBConnectionManager.get_client("md_a")
        await SurrealDBConnectionManager.get_client("md_b")
        async with SurrealDBConnectionManager.using("md_a"):
            await SurrealDBConnectionManager.close_all_connections()
        assert md_a.is_connected() is False
        assert md_b.is_connected() is False
        assert len(vars(default)[_P + "clients"]) == 0
        assert default_client.close_calls == 1

    async def test_the_instance_context_manager_uses_the_block_connection(self) -> None:
        _, md_a, md_b = self._connections()
        await SurrealDBConnectionManager.get_client("md_b")
        async with SurrealDBConnectionManager.using("md_a"):
            async with SurrealDBConnectionManager() as client:
                assert client is vars(md_a)[_P + "embedded_client"]
            assert vars(md_a)[_P + "embedded_client"] is None
            assert md_a.is_connected() is False
        assert md_b.is_connected() is True


@pytest.mark.usefixtures("unset_default")
class TestNamedTexts:
    """AC-29, spec R9, R10, D24: the texts of a named connection name it; the default's stay byte-identical."""

    async def test_r9_names_the_connection(self) -> None:
        SurrealDBConnectionManager.add_connection("md_a", url="mem://", namespace="t", database="a")
        async with SurrealDBConnectionManager.using("md_a"):
            await SurrealDBConnectionManager.set_database("")
            with pytest.raises(ValueError) as info:
                await SurrealDBConnectionManager.get_client()
            assert str(info.value) == "Connection not been set ('md_a')."
            with pytest.raises(ValueError, match=r"^Connection not been set \('md_a'\)\.$"):
                SurrealDBConnectionManager.require_connection()
        with pytest.raises(ValueError) as info:
            await SurrealDBConnectionManager.get_client()
        assert str(info.value) == "Connection not been set."
        with pytest.raises(ValueError) as info:
            await SurrealDBConnectionManager.get_client("default")
        assert str(info.value) == "Connection not been set."

    def test_r10_names_the_connection(self) -> None:
        named = SurrealDBConnectionManager._lost_identity_error("md_a")
        assert isinstance(named, SurrealDbAuthenticationError)
        assert str(named).startswith("The SurrealDB connection 'md_a' on this event loop dropped")
        assert str(named) == _V028_LOST_IDENTITY.replace("The SurrealDB connection on", "The SurrealDB connection 'md_a' on", 1)
        assert str(SurrealDBConnectionManager._lost_identity_error("default")) == _V028_LOST_IDENTITY
        assert str(SurrealDBConnectionManager._lost_identity_error(None)) == _V028_LOST_IDENTITY


# === E2E: the line under test (ws, http) and the embedded engine =============================================


@pytest.mark.parametrize("kind", ["ws", "http"])
class TestTwoDatabasesE2E:
    """M4: two connections on two databases of one server are isolated (both lines)."""

    async def test_each_connection_writes_its_own_database(self, kind: str) -> None:
        async with _databases(kind, "md_a", "md_b"):
            a = await SurrealDBConnectionManager.get_client("md_a")
            b = await SurrealDBConnectionManager.get_client(name="md_b")
            assert a is not b
            await a.query("UPSERT md_item:a SET v = 'a';", {})
            await b.query("UPSERT md_item:b SET v = 'b';", {})
            assert _values(await _root_rows("md_a", "md_item")) == ["a"]
            assert _values(await _root_rows("md_b", "md_item")) == ["b"]
            async with SurrealDBConnectionManager.transaction("md_a") as tx:
                await tx.add("UPSERT md_item:t SET v = 't';")
            assert _values(await _root_rows("md_a", "md_item")) == ["a", "t"]
            assert _values(await _root_rows("md_b", "md_item")) == ["b"]


@pytest.mark.parametrize("kind", _KINDS)
class TestReRegistrationE2E:
    """AC-28 items 1-2 (raw clients): after a re-registration with another database, the next client serves it; the
    client the application kept behaves as documented for its transport."""

    async def test_the_next_client_serves_the_new_settings(self, kind: str) -> None:
        async with _databases(kind, "md_a"):
            c1 = await SurrealDBConnectionManager.get_client("md_a")
            if kind == "mem":
                await c1.query("UPSERT md_item:old SET v = 'old';", {})
            _register(kind, "md_a", database="md_b")
            c2 = await SurrealDBConnectionManager.get_client("md_a")
            if kind == "mem":
                # The connection's one in-process session, re-selected on md_b at the ORM's call.
                assert c2 is c1
                assert await _rows_of(c1, "md_item") == []
                await c2.query("UPSERT md_item:new SET v = 'new';", {})
                assert _values(await _rows_of(c1, "md_item")) == ["new"]
                assert _values(await _engine_rows(c1, "md_a", "md_item")) == ["old"]
                return
            assert c2 is not c1
            await c2.query("UPSERT md_item:new SET v = 'new';", {})
            assert _values(await _root_rows("md_b", "md_item")) == ["new"]
            assert await _root_rows("md_a", "md_item") == []
            await _drain_background()
            if kind == "ws":
                # Its transport was closed: its receive task ends, its next request fails.
                await asyncio.wait({c1.recv_task}, timeout=10)
                assert live.is_dead(c1)
                with pytest.raises(Exception):  # noqa: B017 - the SDK's own error for a closed socket
                    await c1.query("RETURN 1;", {})
            else:
                # SDK 2.0 HTTP (M17): it keeps the namespace and database it was opened on.
                await c1.query("UPSERT md_item:kept SET v = 'kept';", {})
                assert _values(await _root_rows("md_a", "md_item")) == ["kept"]
                assert _values(await _root_rows("md_b", "md_item")) == ["new"]


class TestTransactionGenerationE2E:
    """AC-28 item 4 (raw writes), P1, P10: a transaction whose connection is removed or registered again with other
    settings while it is open is cancelled at its exit; nothing is committed (R12)."""

    @staticmethod
    async def _assert_nothing_committed(kind: str, change: str, client: Any, sent: list[str]) -> None:
        assert not any("BEGIN TRANSACTION" in statement for statement in sent), sent
        if kind != "mem":
            assert await _root_rows("md_a", "md_item") == []
            assert await _root_rows("md_b", "md_item") == []
        elif change == "re-register":
            assert await _engine_rows(client, "md_a", "md_item") == []
            assert await _engine_rows(client, "md_b", "md_item") == []

    @pytest.mark.parametrize("change", _CHANGES)
    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_exit_raises_r12(self, kind: str, change: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _databases(kind, "md_a"):
            client = await SurrealDBConnectionManager.get_client("md_a")
            sent = _record_raw(monkeypatch, client)
            with pytest.raises(SurrealDbConnectionError, match="nothing was committed") as info:
                async with SurrealDBConnectionManager.transaction("md_a") as tx:
                    await tx.add("UPSERT md_item:1 SET v = 1;")
                    await _change(change, lambda: _register(kind, "md_a", database="md_b"))
            assert info.value.retryable is False  # type: ignore[attr-defined]
            assert str(info.value) == (
                "Connection 'md_a' was removed or registered again with other settings while this transaction was "
                "open on it; the transaction was cancelled and nothing was committed. Open it again."
            )
            await _drain_background()
            await self._assert_nothing_committed(kind, change, client, sent)

    @pytest.mark.parametrize("change", _CHANGES)
    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_body_exception_keeps_its_identity_and_gets_a_note(
        self, kind: str, change: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with _databases(kind, "md_a"):
            client = await SurrealDBConnectionManager.get_client("md_a")
            sent = _record_raw(monkeypatch, client)
            mine = RuntimeError("mine")
            with pytest.raises(RuntimeError) as info:
                async with SurrealDBConnectionManager.transaction("md_a") as tx:
                    await tx.add("UPSERT md_item:1 SET v = 1;")
                    await _change(change, lambda: _register(kind, "md_a", database="md_b"))
                    raise mine
            assert info.value is mine
            notes = [note for note in mine.__notes__ if "registered again with other settings" in note]
            assert len(notes) == 1
            assert "'md_a'" in notes[0]
            assert "nothing was committed" in notes[0]
            await _drain_background()
            await self._assert_nothing_committed(kind, change, client, sent)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_body_exception_without_a_change_gets_no_note(self, kind: str) -> None:
        async with _databases(kind, "md_a"):
            client = await SurrealDBConnectionManager.get_client("md_a")
            with pytest.raises(RuntimeError, match="plain") as info:
                async with SurrealDBConnectionManager.transaction("md_a") as tx:
                    await tx.add("UPSERT md_item:1 SET v = 1;")
                    raise RuntimeError("plain")
            assert not hasattr(info.value, "__notes__")
            rows = await (_rows_of(client, "md_item") if kind == "mem" else _root_rows("md_a", "md_item"))
            assert rows == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_same_settings_again_commit(self, kind: str) -> None:
        async with _databases(kind, "md_a"):
            client = await SurrealDBConnectionManager.get_client("md_a")
            async with SurrealDBConnectionManager.transaction("md_a") as tx:
                await tx.add("UPSERT md_item:1 SET v = 1;")
                _register(kind, "md_a", database="md_a")
            rows = await (_rows_of(client, "md_item") if kind == "mem" else _root_rows("md_a", "md_item"))
            assert _values(rows) == [1]

    async def test_nested_blocks_get_the_note_once(self) -> None:
        """P10: both blocks were open across the change; the inner one notes it, the outer one does not again."""
        async with _databases("mem", "md_a"):
            mine = RuntimeError("mine")
            with pytest.raises(RuntimeError) as info:
                async with SurrealDBConnectionManager.transaction("md_a") as outer:
                    await outer.add("UPSERT md_item:1 SET v = 1;")
                    async with SurrealDBConnectionManager.transaction("md_a") as inner:
                        await inner.add("UPSERT md_item:2 SET v = 2;")
                        _register("mem", "md_a", database="md_b")
                        raise mine
            assert info.value is mine
            assert len(mine.__notes__) == 1
            assert "registered again with other settings" in mine.__notes__[0]


class TestServerBesideEmbeddedE2E:
    """AC-9: a ws and an http connection to the line under test and a ``mem://`` one, side by side in one process;
    each decides its transaction strategy on its own server."""

    async def test_side_by_side(self) -> None:
        await _reset_databases(define=True)
        _register("ws", "md_a", database="md_a")
        _register("http", "md_b", database="md_b")
        _register("mem", "md_mem", database="md_mem")
        try:
            ws = await SurrealDBConnectionManager.get_client("md_a")
            http = await SurrealDBConnectionManager.get_client("md_b")
            mem = await SurrealDBConnectionManager.get_client("md_mem")
            assert len({id(ws), id(http), id(mem)}) == 3
            await ws.query("UPSERT md_item:1 SET v = 'ws';", {})
            await http.query("UPSERT md_item:1 SET v = 'http';", {})
            await mem.query("UPSERT md_item:1 SET v = 'mem';", {})
            assert _values(await _root_rows("md_a", "md_item")) == ["ws"]
            assert _values(await _root_rows("md_b", "md_item")) == ["http"]
            assert _values(await _rows_of(mem, "md_item")) == ["mem"]
            line = int(str(await ws.version()).rsplit("-", 1)[-1].split(".")[0])
            async with SurrealDBConnectionManager.transaction("md_a") as tx:
                assert tx.is_interactive is (line >= 3)
            async with SurrealDBConnectionManager.transaction("md_b") as tx:
                assert tx.is_interactive is False
            async with SurrealDBConnectionManager.transaction("md_mem") as tx:
                assert tx.is_interactive is False
        finally:
            for name in ("md_a", "md_b", "md_mem"):
                await SurrealDBConnectionManager.remove_connection(name)
            await _reset_databases(define=False)


#: A record access on database ``md_a`` only (AC-11): a record user signs up and in there, and reads itself.
_RECORD_ACCESS = (
    "DEFINE TABLE OVERWRITE md_account SCHEMALESS PERMISSIONS FOR select, update WHERE id = $auth.id;"
    "DEFINE ACCESS OVERWRITE md_account ON DATABASE TYPE RECORD"
    " SIGNUP (CREATE md_account SET email = $email, pass = crypto::argon2::generate($pass))"
    " SIGNIN (SELECT * FROM md_account WHERE email = $email AND crypto::argon2::compare(pass, $pass))"
    " DURATION FOR TOKEN 15m, FOR SESSION 12h;"
)


@contextlib.asynccontextmanager
async def _record_access(url: str | None = None) -> AsyncIterator[dict[str, str]]:
    """``md_a`` registered on database ``md_a`` (through *url*, else straight to the line under test), with the
    record access ``md_account`` defined there only; the default connection on ``ns``/``db`` as root. Yields the
    variables of a record user to sign up."""
    await _reset_databases(define=True)
    async with _root("md_a") as root:
        await root.query(_RECORD_ACCESS, {})
    SurrealDBConnectionManager.set_connection(url=_url("ws"), user="root", password="root", namespace="ns", database="db")
    SurrealDBConnectionManager.add_connection(
        "md_a", url=url or _url("ws"), user="root", password="root", namespace="ns", database="md_a"
    )
    try:
        yield {"email": f"{uuid4().hex}@example.test", "pass": "s3cret-passphrase"}
    finally:
        await SurrealDBConnectionManager.remove_connection("md_a")
        SurrealDBConnectionManager.clear_session()
        await SurrealDBConnectionManager.close_connection()
        await _reset_databases(define=False)


@pytest.mark.usefixtures("unset_default")
class TestSessionsPerConnectionE2E:
    """AC-11 (both lines, ws): a session is stored, replayed, lost and ended per connection — ``using()`` picks it."""

    async def test_a_record_session_stays_on_its_connection(self) -> None:
        router = SurrealDBConnectionManager
        async with _record_access() as variables:
            default_client = await router.get_client()
            async with router.using("md_a"):
                assert router.is_authenticated() is False
                tokens = await router.signup(access="md_account", variables=variables)
                assert router.is_authenticated() is True
                assert router.get_session_token() == tokens.access
                me = await router.info()
                assert me is not None and me["email"] == variables["email"]
            # The default: no session, still root.
            assert router.is_authenticated() is False
            assert router.get_session_token() is None
            assert await router.info() is None
            assert await router.get_client() is default_client
            # reconnect() inside the block replays md_a's token, and only on md_a.
            async with router.using("md_a"):
                before = await router.get_client()
                again = await router.reconnect()
                assert again is not before
                me = await router.info()
                assert me is not None and me["email"] == variables["email"]
                assert router.get_session_token() == tokens.access
            assert await router.get_client() is default_client
            assert await router.info() is None
            assert SurrealDBConnectionManager._manager_for("md_a").get_session_token() == tokens.access

    async def test_invalidate_and_clear_session_act_on_the_block_connection(self) -> None:
        router = SurrealDBConnectionManager
        async with _record_access() as variables:
            await router.signin(username="root", password="root")
            default_token = router.get_session_token()
            assert default_token is not None
            async with router.using("md_a"):
                await router.signup(access="md_account", variables=variables)
                await router.invalidate()
                assert router.is_authenticated() is False
                assert await router.info() is None
                await router.signin(access="md_account", variables=variables)
                assert router.is_authenticated() is True
                router.clear_session()
                assert router.is_authenticated() is False
                # clear_session() forgets the replay only: the live session is still the record user.
                me = await router.info()
                assert me is not None and me["email"] == variables["email"]
            assert router.get_session_token() == default_token
            assert await router.info() is None

    async def test_a_dropped_unstored_session_blocks_only_its_connection(self) -> None:
        """R10: the lost-identity state is per connection and event loop, and its error names the connection."""
        router = SurrealDBConnectionManager
        async with CuttableProxy() as proxy, _record_access(proxy.url()) as variables:
            async with router.using("md_a"):
                await router.signup(access="md_account", variables=variables, store=False)
                assert router.is_authenticated() is False
                client = await router.get_client()
                receiving = live.recv_task_of(client)
                assert receiving is not None
                proxy.cut()
                await asyncio.wait({receiving}, timeout=10)
                assert live.is_dead(client)
                with pytest.raises(SurrealDbAuthenticationError) as info:
                    await router.get_client()
                assert str(info.value).startswith("The SurrealDB connection 'md_a' on this event loop dropped")
            # The default is served, as its configured user.
            default_client = await router.get_client()
            assert await default_client.query("RETURN 1;", {}) == 1
            assert await router.info() is None
            async with router.using("md_a"):
                with pytest.raises(SurrealDbAuthenticationError, match="'md_a'"):
                    await router.get_client()
                # An explicit choice ends the state: back to the configured user.
                await router.reconnect()
                assert await router.info() is None


class TestConcurrentBlocksE2E:
    """AC-23 (tasks): two tasks, each inside its own ``using()`` block and interleaved, write their own database
    (``mem://``)."""

    async def test_two_tasks_in_two_blocks(self) -> None:
        _register("mem", "md_a", database="md_a")
        _register("mem", "md_b", database="md_b")
        a_open, b_open, a_wrote = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def write(name: str, mine: asyncio.Event, theirs: asyncio.Event) -> tuple[str | None, Any]:
            async with SurrealDBConnectionManager.using(name):
                mine.set()
                await theirs.wait()  # both blocks are open from here on
                if name == "md_b":
                    await a_wrote.wait()  # md_a writes first, then md_b while md_a's block is still open
                client = await SurrealDBConnectionManager.get_client()
                await client.query("UPSERT md_item:1 SET v = $v;", {"v": name})
                if name == "md_a":
                    a_wrote.set()
                    await b_open.wait()
                return SurrealDBConnectionManager.get_active_connection_name(), client

        (a_name, a_client), (b_name, b_client) = await asyncio.gather(
            write("md_a", a_open, b_open), write("md_b", b_open, a_open)
        )
        assert (a_name, b_name) == ("md_a", "md_b")
        assert _active() is None
        assert a_client is await SurrealDBConnectionManager.get_client("md_a")
        assert b_client is await SurrealDBConnectionManager.get_client("md_b")
        assert a_client is not b_client
        assert _values(await _rows_of(a_client, "md_item")) == ["md_a"]
        assert _values(await _rows_of(b_client, "md_item")) == ["md_b"]


# === Models routed to a connection (spec §5.5, §6.4) =========================================================


class MdEvent(BaseSurrealModel):
    """Routed to ``md_a``."""

    model_config = SurrealConfigDict(table_name="md_event", connection="md_a")

    id: str | None = None
    title: str = ""
    n: int = 0


class MdNote(BaseSurrealModel):
    """Routed nowhere: the default connection, or the ``using()`` block's."""

    model_config = SurrealConfigDict(table_name="md_note")

    id: str | None = None
    text: str = ""


class MdOrphan(BaseSurrealModel):
    """Routed to a name nobody registers."""

    model_config = SurrealConfigDict(table_name="md_orphan", connection="md_missing")

    id: str | None = None


class MdLikes(BaseSurrealEdge):
    """An edge model routed to ``md_a``, like the records it links."""

    model_config = SurrealConfigDict(table_name="md_likes", connection="md_a")

    weight: int = 1


class MdMember(AuthenticatedUserMixin, BaseSurrealModel):
    """A record user of ``md_a``: its access method and table exist there only."""

    model_config = SurrealConfigDict(table_name="md_member", access_name="md_member", connection="md_a")

    id: str | None = None
    email: str
    password: str


def _r2(model: str, name: str) -> str:
    return (
        f"{model}: connection {name!r} is not registered. Register it with "
        f"SurrealDBConnectionManager.add_connection({name!r}, …) first."
    )


class TestModelRouting:
    """AC-6 (no server but ``mem://``): R7 at class definition, inheritance and override, ``get_connection_name()``
    and its ``using()`` override, R2 at the first database call only."""

    def test_r7_at_class_definition(self) -> None:
        with pytest.raises(TypeError) as numbered:

            class MdNumbered(BaseSurrealModel):
                model_config = SurrealConfigDict(connection=1)  # type: ignore[typeddict-item]

        assert str(numbered.value) == "MdNumbered: connection must be a str, got int."
        with pytest.raises(ValueError) as blank:

            class MdBlank(BaseSurrealModel):
                model_config = SurrealConfigDict(connection="")

        assert str(blank.value) == "MdBlank: connection must not be empty."

    def test_inherited_and_overridden(self) -> None:
        class MdBase(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="md_base", connection="md_a")

        class MdChild(MdBase):
            pass

        class MdElsewhere(MdBase):
            model_config = SurrealConfigDict(connection="md_b")

        class MdBack(MdBase):
            model_config = SurrealConfigDict(connection=None)

        assert MdChild.get_connection_name() == "md_a"
        assert MdChild.get_table_name() == "md_base"
        assert MdElsewhere.get_connection_name() == "md_b"
        assert MdBack.get_connection_name() == "default"

    async def test_get_connection_name(self) -> None:
        assert MdCustomer.get_connection_name() == "default"
        assert MdEvent.get_connection_name() == "md_a"
        assert MdOrphan.get_connection_name() == "md_missing"
        _register("mem", "md_b", database="md_b")
        async with SurrealDBConnectionManager.using("md_b"):
            # The block wins over the model's own connection.
            assert MdCustomer.get_connection_name() == "md_b"
            assert MdEvent.get_connection_name() == "md_b"
            assert MdOrphan.get_connection_name() == "md_b"
            await MdOrphan(id="x").save()
            assert [orphan.id for orphan in await MdOrphan.objects().exec()] == ["x"]
        assert MdEvent.get_connection_name() == "md_a"

    async def test_r2_at_the_first_database_call_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", _forbidden_client)
        # Defining the class, objects() and the builders raise nothing.
        queryset = MdOrphan.objects().filter(id="x").order_by("id").limit(1)
        calls: list[Callable[[], Awaitable[Any]]] = [
            queryset.exec,
            MdOrphan.objects().count,
            lambda: MdOrphan.objects().get("x"),
            MdOrphan(id="x").save,
            lambda: MdOrphan.call_function("fn::md_ping"),
            lambda: MdOrphan.raw_query("SELECT * FROM md_orphan;"),
        ]
        for call in calls:
            with pytest.raises(ValueError) as info:
                await call()
            assert str(info.value) == _r2("MdOrphan", "md_missing")

    async def test_a_removed_connection_raises_r2(self) -> None:
        _register("mem", "md_a", database="md_a")
        await MdEvent(id="e1").save()
        await SurrealDBConnectionManager.remove_connection("md_a")
        with pytest.raises(ValueError) as info:
            await MdEvent.objects().exec()
        assert str(info.value) == _r2("MdEvent", "md_a")

    @pytest.mark.usefixtures("unset_default")
    async def test_a_queryset_resolves_its_connection_when_it_runs(self) -> None:
        _register("mem", "md_a", database="md_a")
        client = await SurrealDBConnectionManager.get_client("md_a")
        await client.query("CREATE md_note:1 SET text = 'in md_a';")
        queryset = MdNote.objects()  # built outside the block, while the default is not configured
        async with SurrealDBConnectionManager.using("md_a"):
            assert [note.text for note in await queryset.exec()] == ["in md_a"]
        with pytest.raises(ValueError, match="Connection not been set"):
            await queryset.exec()


#: Attributes of ``SurrealDBConnectionManager`` the package may name outside ``connection_manager.py`` (spec §6.4):
#: the resolvers and the registry. Everything else goes through a resolved connection class.
_RESOLVERS = frozenset(
    {
        "_manager_for",
        "_register_for_block",
        "get_active_connection_name",
        "add_connection",
        "remove_connection",
        "get_config",
        "list_connections",
        "using",
    }
)

# Temporary: removed entry by entry, empty and deleted at Task 9 (spec AC-7).
_NOT_YET_ROUTED: dict[tuple[str, str | None], str] = {
    ("testing/database.py", None): "Task 9 adds embedded_database(connection=)",
}


def _manager_attributes() -> list[tuple[str, tuple[str, ...], str, int]]:
    """``(path, enclosing functions, attribute, line)`` of every ``SurrealDBConnectionManager.<attribute>`` node in the
    package's code outside ``connection_manager.py`` (AST nodes: docstrings and comments do not count)."""
    package = Path(surreal_orm_lite.__file__).parent
    found: list[tuple[str, tuple[str, ...], str, int]] = []
    for path in sorted(package.rglob("*.py")):
        relative = path.relative_to(package).as_posix()
        if relative == "connection_manager.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
                continue
            if node.value.id != "SurrealDBConnectionManager":
                continue
            functions: list[str] = []
            parent = parents.get(node)
            while parent is not None:
                if isinstance(parent, ast.FunctionDef | ast.AsyncFunctionDef):
                    functions.append(parent.name)
                parent = parents.get(parent)
            found.append((relative, tuple(functions), node.attr, node.lineno))
    return found


def _not_yet_routed(path: str, functions: tuple[str, ...]) -> tuple[str, str | None] | None:
    """The entry of :data:`_NOT_YET_ROUTED` covering a node of *path* inside *functions*, if any."""
    for key in ((path, None), *((path, name) for name in functions)):
        if key in _NOT_YET_ROUTED:
            return key
    return None


class TestSingleResolver:
    """AC-7: outside ``connection_manager.py`` the package reaches a connection through its resolvers only."""

    def test_every_call_goes_through_a_resolved_connection(self) -> None:
        found = _manager_attributes()
        # The scan sees the package: the models' resolver names _manager_for.
        assert any(path == "model_base.py" and attribute == "_manager_for" for path, _, attribute, _ in found)
        offenders = [
            f"{path}:{line} {attribute}"
            for path, functions, attribute, line in found
            if attribute not in _RESOLVERS and _not_yet_routed(path, functions) is None
        ]
        assert offenders == []

    def test_no_stale_allowlist_entry(self) -> None:
        needed = {
            _not_yet_routed(path, functions)
            for path, functions, attribute, _ in _manager_attributes()
            if attribute not in _RESOLVERS
        }
        assert set(_NOT_YET_ROUTED) <= needed


@contextlib.asynccontextmanager
async def _routed_databases(kind: str) -> AsyncIterator[None]:
    """``md_a`` on database ``md_a`` and the default connection on database ``md_b``, both through *kind* (``ws`` or
    ``http`` to the line under test, as root; ``mem``: an engine each). Used with the ``unset_default`` fixture."""
    async with _databases(kind, "md_a"):
        if kind == "mem":
            SurrealDBConnectionManager.set_connection(url="mem://", namespace="ns", database="md_b")
        else:
            SurrealDBConnectionManager.set_connection(
                url=_url(kind), user="root", password="root", namespace="ns", database="md_b"
            )
        try:
            yield
        finally:
            await SurrealDBConnectionManager.close_connection()


async def _rows_in(kind: str, connection: str, table: str) -> list[Any]:
    """The rows of *table* where *connection* (``md_a`` or ``default``) keeps them: read back with a root client
    on its database, or through its own engine (``mem``)."""
    if kind == "mem":
        return await _rows_of(await SurrealDBConnectionManager.get_client(connection), table)
    return await _root_rows("md_a" if connection == "md_a" else "md_b", table)


def _titles(rows: list[Any]) -> list[str]:
    return sorted(row["title"] for row in rows)


@pytest.mark.usefixtures("unset_default")
class TestRoutedModelsE2E:
    """AC-8 (both lines, ws and http, and ``mem://``): every operation of a model routed to ``md_a`` reads and
    writes ``md_a``, never the default connection's database; AC-28 item 1, the model part."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_every_operation_stays_in_its_database(self, kind: str) -> None:
        async with _routed_databases(kind):
            a = await SurrealDBConnectionManager.get_client("md_a")
            await a.query("DEFINE FUNCTION OVERWRITE fn::md_ping() { RETURN 'md_a'; };", {})

            first = await MdEvent(id="e1", title="one", n=1).save()
            first.title = "uno"
            await first.update()
            await first.merge(n=2)
            await first.patch([{"op": "replace", "path": "/title", "value": "eins"}])
            second = await MdEvent(id="e2", title="two").upsert()
            await second.atomic_increment("n", 5)
            first.title = "stale"
            await first.refresh()
            assert first.title == "eins"
            assert _titles(await _rows_in(kind, "md_a", "md_event")) == ["eins", "two"]

            assert sorted(event.title for event in await MdEvent.objects().filter(n__gte=2).exec()) == ["eins", "two"]
            assert (await MdEvent.objects().get("e2")).n == 5
            assert sorted(event.title for event in await MdEvent.objects().all()) == ["eins", "two"]
            assert await MdEvent.objects().count() == 2
            await MdEvent.objects().bulk_create([MdEvent(id="e3", title="three"), MdEvent(id="e4", title="four")])
            assert await MdEvent.objects().filter(title="three").bulk_update(n=9) == 1
            assert (await MdEvent.objects().get("e3")).n == 9

            await first.relate("md_follows", second)
            await first.relate(MdLikes, second, weight=3)
            assert [event.title for event in await first.get_related(MdLikes, model_class=MdEvent)] == ["two"]
            assert [event.title for event in await first.get_related("md_follows", model_class=MdEvent)] == ["two"]
            raw = await MdEvent.raw_query("SELECT * FROM md_event WHERE title = $title;", {"title": "four"})
            assert [event.title for event in raw] == ["four"]  # type: ignore[union-attr]
            assert await MdEvent.call_function("fn::md_ping") == "md_a"

            assert _titles(await _rows_in(kind, "md_a", "md_event")) == ["eins", "four", "three", "two"]
            assert len(await _rows_in(kind, "md_a", "md_likes")) == 1
            assert len(await _rows_in(kind, "md_a", "md_follows")) == 1
            for table in ("md_event", "md_likes", "md_follows"):
                assert await _rows_in(kind, "default", table) == [], table

            assert await MdEvent.objects().filter(title="four").bulk_delete() == 1
            await second.delete()
            assert _titles(await _rows_in(kind, "md_a", "md_event")) == ["eins", "three"]
            assert await MdEvent.objects().delete_table() is True
            assert await _rows_in(kind, "md_a", "md_event") == []
            assert await _rows_in(kind, "default", "md_event") == []

    async def test_as_of_reads_the_history_of_the_models_connection(self, tmp_path: Path) -> None:
        """The default keeps no history (``mem://``: T6 if the read went there); ``md_a`` does."""
        SurrealDBConnectionManager.set_connection(url="mem://", namespace="ns", database="md_b")
        SurrealDBConnectionManager.add_connection(
            "md_a", url=f"surrealkv+versioned://{tmp_path / 'kv'}", namespace="ns", database="md_a"
        )
        client = await SurrealDBConnectionManager.get_client("md_a")
        await client.query("CREATE md_event:a SET title = 'old', n = 1 VERSION d'2020-01-01T00:00:00Z';")
        await client.query("CREATE md_event:a SET title = 'new', n = 2 VERSION d'2021-01-01T00:00:00Z';")
        [old] = await MdEvent.objects().as_of(datetime(2020, 6, 1, tzinfo=UTC)).exec()
        assert (old.title, old.n) == ("old", 1)
        [current] = await MdEvent.objects().exec()
        assert (current.title, current.n) == ("new", 2)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_re_registration_moves_the_model(self, kind: str) -> None:
        """AC-28 item 1: after ``add_connection("md_a", …, database="md_b")`` the model reads and writes ``md_b``."""
        async with _databases(kind, "md_a"):
            await MdEvent(id="old", title="old").save()
            _register(kind, "md_a", database="md_b")
            assert await MdEvent.objects().exec() == []
            await MdEvent(id="new", title="new").save()
            assert [event.title for event in await MdEvent.objects().exec()] == ["new"]
            if kind == "mem":
                client = await SurrealDBConnectionManager.get_client("md_a")
                assert _titles(await _engine_rows(client, "md_a", "md_event")) == ["old"]
                assert _titles(await _engine_rows(client, "md_b", "md_event")) == ["new"]
            else:
                assert _titles(await _root_rows("md_a", "md_event")) == ["old"]
                assert _titles(await _root_rows("md_b", "md_event")) == ["new"]


_PASSPHRASE = "s3cret-passphrase"


@pytest.mark.usefixtures("unset_default")
class TestRoutedModelAuthE2E:
    """AC-12: an ``AuthenticatedUserMixin`` model routed to ``md_a`` defines its access, signs up and in on ``md_a``
    (both lines, ws); ``bind=True`` adopts the session on ``md_a`` only, also on an embedded named connection."""

    async def test_model_auth_runs_on_its_connection(self) -> None:
        async with _routed_databases("ws"):
            await MdMember.define_access()
            async with _root("md_a") as root:
                assert "md_member" in str(await root.query("INFO FOR DB;", {}))
            async with _root("md_b") as root:
                assert "md_member" not in str(await root.query("INFO FOR DB;", {}))
            email = f"{uuid4().hex}@example.test"
            created = await MdMember.signup(email=email, password=_PASSPHRASE)
            assert [row["email"] for row in await _root_rows("md_a", "md_member")] == [email]
            assert await _root_rows("md_b", "md_member") == []
            signed_in = await MdMember.signin(email=email, password=_PASSPHRASE)
            assert signed_in.user.get_id() == created.user.get_id()
            assert (await MdMember.authenticate(created.tokens.access)).email == email

            connection = SurrealDBConnectionManager._manager_for("md_a")
            assert connection.is_authenticated() is False
            bound = await MdMember.signin(email=email, password=_PASSPHRASE, bind=True)
            assert connection.get_session_token() == bound.tokens.access
            assert SurrealDBConnectionManager.is_authenticated() is False
            assert SurrealDBConnectionManager.get_session_token() is None

    async def test_bind_true_on_an_embedded_named_connection(self) -> None:
        SurrealDBConnectionManager.set_connection(url="mem://", namespace="ns", database="md_b")
        _register("mem", "md_a", database="md_a")
        await MdMember.define_access()
        with pytest.raises(SurrealDbError, match="bind=True"):
            await MdMember.signup(email="m@x", password="p")
        result = await MdMember.signup(email="m@x", password="p", bind=True)
        assert SurrealDBConnectionManager._manager_for("md_a").get_session_token() == result.tokens.access
        async with SurrealDBConnectionManager.using("md_a"):
            me = await SurrealDBConnectionManager.info()
        assert me is not None and me["email"] == "m@x"
        assert SurrealDBConnectionManager.is_authenticated() is False
        assert await _rows_of(await SurrealDBConnectionManager.get_client(), "md_member") == []


# === Change feeds (spec §6.4, AC-14) ==========================================================================

#: The change-feed kinds: WebSocket to the line under test, and the embedded engine (from now, from a cursor).
_FEED_KINDS = ("ws", "mem")


@contextlib.asynccontextmanager
async def _feed_databases(kind: str, table: str) -> AsyncIterator[None]:
    """:func:`_routed_databases` with a change feed on *table* in ``md_a`` and in the default's ``md_b``. One
    change-feed table per database: SurrealDB 2.x applies ``SHOW CHANGES``' ``LIMIT`` to the whole database's feed."""
    async with _routed_databases(kind):
        for connection in ("md_a", "default"):
            client = await SurrealDBConnectionManager.get_client(connection)
            await client.query(f"DEFINE TABLE {table} SCHEMALESS CHANGEFEED 1h;", {})
        yield


async def _read_one(stream: Any) -> Any:
    """One ``__anext__()`` of *stream*, within ten seconds (a stream that should have ended fails, not hangs)."""
    async with asyncio.timeout(10):
        return await anext(stream)


async def _next_events(stream: Any, count: int) -> list[Any]:
    """The next *count* events of *stream*, within ten seconds."""
    events: list[Any] = []
    async with asyncio.timeout(10):
        while len(events) < count:
            events.append(await anext(stream))
    return events


def _fail_once(monkeypatch: pytest.MonkeyPatch, client: Any, method: str) -> None:
    """Make *client*'s *method* raise ``RuntimeError("failed once")`` the next time only (patched on its class, keyed
    on ``id(client)``)."""
    cls = type(client)
    real = getattr(cls, method)
    owner = id(client)
    left = [1]

    async def failing(self: Any, *args: Any, **kwargs: Any) -> Any:
        if id(self) == owner and left[0]:
            left[0] -= 1
            raise RuntimeError("failed once")
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(cls, method, failing)


def _count_client_calls(monkeypatch: pytest.MonkeyPatch, connection: type[SurrealDBConnectionManager]) -> list[str]:
    """Record every ``get_client()`` and ``_open_client()`` of *connection* from now on (spec AC-14's client spy)."""
    calls: list[str] = []
    for name in ("get_client", "_open_client"):
        real = getattr(connection, name)

        def spy(real: Any = real, name: str = name) -> Any:
            async def recording(*args: Any, **kwargs: Any) -> Any:
                calls.append(name)
                return await real(*args, **kwargs)

            return recording

        monkeypatch.setattr(connection, name, spy())
    return calls


async def _write_in_md_b(kind: str) -> None:
    """A change on ``md_event`` in database ``md_b``, where ``md_a`` gets registered again: a read of that feed with
    ``md_a``'s position would return it (``mem``: the database ``md_b`` of ``md_a``'s engine, request-scoped, M5)."""
    if kind == "mem":
        client = await SurrealDBConnectionManager.get_client("md_a")
        await client.query_raw(
            "USE DB md_b; DEFINE TABLE IF NOT EXISTS md_event SCHEMALESS CHANGEFEED 1h; CREATE md_event:b SET title = 'b';",
            {},
        )
    else:
        async with _root("md_b") as root:
            await root.query("CREATE md_event:b SET title = 'b';", {})


#: R13, verbatim (spec §5.7).
_R13 = (
    "Connection 'md_a' was removed or registered again with other settings while this change feed was being read; "
    "its position belongs to the previous database, so the stream ended. Start a new one."
)


@pytest.mark.usefixtures("unset_default")
class TestRoutedChangeFeedsE2E:
    """AC-14 (both lines, ws; ``mem://`` with a cursor): a change feed reads its model's connection, bound once."""

    @pytest.mark.parametrize("kind", _FEED_KINDS)
    async def test_a_routed_model_reads_its_connections_feed(self, kind: str) -> None:
        async with _feed_databases(kind, "md_event"):
            stream = MdEvent.objects().changes(poll_interval=0.05)
            await stream.start()
            await MdEvent(id="a1", title="one").save()
            default = await SurrealDBConnectionManager.get_client("default")
            await default.query("CREATE md_event:d1 SET title = 'default';", {})
            [event] = await _next_events(stream, 1)
            assert (event.record_id, event.instance.title) == ("md_event:a1", "one")
            cursor = stream.cursor
            stream.stop()
            await MdEvent(id="a2", title="two").save()
            resumed = MdEvent.objects().changes(since=cursor, poll_interval=0.05)
            [event] = await _next_events(resumed, 1)
            assert event.record_id == "md_event:a2"
            resumed.stop()

    @pytest.mark.parametrize("kind", _FEED_KINDS)
    async def test_a_stream_started_in_a_block_keeps_its_connection(self, kind: str) -> None:
        async with _feed_databases(kind, "md_note"):
            async with SurrealDBConnectionManager.using("md_a"):
                stream = MdNote.objects().changes(poll_interval=0.05)
                await stream.start()
            default = await SurrealDBConnectionManager.get_client("default")
            await default.query("CREATE md_note:d1 SET text = 'default';", {})
            a = await SurrealDBConnectionManager.get_client("md_a")
            await a.query("CREATE md_note:a1 SET text = 'md_a';", {})
            [event] = await _next_events(stream, 1)
            assert (event.record_id, event.instance.text) == ("md_note:a1", "md_a")
            stream.stop()

    @pytest.mark.parametrize("kind", _FEED_KINDS)
    async def test_a_failed_start_keeps_its_first_binding(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _feed_databases(kind, "md_note"):
            _fail_once(monkeypatch, await SurrealDBConnectionManager.get_client("md_a"), "version")
            async with SurrealDBConnectionManager.using("md_a"):
                stream = MdNote.objects().changes(poll_interval=0.05)
                with pytest.raises(RuntimeError, match="failed once"):
                    await stream.start()
            # Started again outside the block: still md_a.
            await stream.start()
            default = await SurrealDBConnectionManager.get_client("default")
            await default.query("CREATE md_note:d1 SET text = 'default';", {})
            a = await SurrealDBConnectionManager.get_client("md_a")
            await a.query("CREATE md_note:a1 SET text = 'md_a';", {})
            [event] = await _next_events(stream, 1)
            assert (event.record_id, event.instance.text) == ("md_note:a1", "md_a")
            stream.stop()


@pytest.mark.usefixtures("unset_default")
class TestChangeFeedGenerationE2E:
    """AC-14 R13, P3 (both lines, ws; ``mem://``): a change feed whose connection is registered again with other
    settings, or removed, ends at its next read — before any request and before any pending event — and at every
    later read; the default connection keeps its v0.28.0 tolerance."""

    @pytest.mark.parametrize("change", _CHANGES)
    @pytest.mark.parametrize("kind", _FEED_KINDS)
    async def test_the_next_read_raises_r13(self, kind: str, change: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _feed_databases(kind, "md_event"):
            stream = MdEvent.objects().changes(poll_interval=0.05)
            await stream.start()
            await MdEvent(id="a1", title="one").save()
            [event] = await _next_events(stream, 1)
            assert event.record_id == "md_event:a1"
            await _write_in_md_b(kind)
            connection = SurrealDBConnectionManager._manager_for("md_a")
            calls = _count_client_calls(monkeypatch, connection)
            await _change(change, lambda: _register(kind, "md_a", database="md_b"))
            for _ in range(2):
                with pytest.raises(SurrealDbConnectionError) as info:
                    await _read_one(stream)
                assert str(info.value) == _R13
                assert info.value.retryable is False  # type: ignore[attr-defined]
            assert calls == []  # nothing reached md_b, nothing was even opened
            stream.stop()
            with pytest.raises(StopAsyncIteration):
                await _read_one(stream)
            if change == "re-register":
                fresh = MdEvent.objects().changes(poll_interval=0.05)
                await fresh.start()
                await MdEvent(id="b2", title="in md_b").save()
                [event] = await _next_events(fresh, 1)
                assert (event.record_id, event.instance.title) == ("md_event:b2", "in md_b")
                fresh.stop()

    @pytest.mark.parametrize("kind", _FEED_KINDS)
    async def test_a_pending_event_is_not_handed_out(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """P3: events a poll fetched but did not yield yet are dropped, and the cursor stays where it was."""
        async with _feed_databases(kind, "md_event"):
            stream = MdEvent.objects().changes(poll_interval=0.05)
            await stream.start()
            await MdEvent(id="p1", n=1).save()
            await MdEvent(id="p2", n=2).save()
            [first] = await _next_events(stream, 1)
            assert first.record_id == "md_event:p1"
            assert len(stream._pending) == 1  # the second event was fetched by the same poll
            before = stream.cursor
            calls = _count_client_calls(monkeypatch, SurrealDBConnectionManager._manager_for("md_a"))
            _register(kind, "md_a", database="md_b")
            with pytest.raises(SurrealDbConnectionError, match="so the stream ended"):
                await _read_one(stream)
            assert stream.cursor == before
            assert calls == []
            stream.stop()

    async def test_the_second_check_closes_the_race(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A change landing while ``get_client()`` runs is caught before the operation, and not retried (P3)."""
        _register("mem", "md_a", database="md_a")
        connection = SurrealDBConnectionManager._manager_for("md_a")
        client = await connection.get_client()
        await client.query("DEFINE TABLE md_event SCHEMALESS CHANGEFEED 1h;")
        stream = MdEvent.objects().changes()
        await stream.start()
        acquired: list[bool] = []

        async def changing_get_client(*args: Any, **kwargs: Any) -> Any:
            acquired.append(True)
            connection._retire_server_clients()  # the generation moves while the client is being acquired
            return client

        ran: list[bool] = []

        async def operation(client: Any) -> Any:
            ran.append(True)
            return None

        monkeypatch.setattr(connection, "get_client", changing_get_client)
        with (
            caplog.at_level(logging.WARNING, logger="surreal_orm_lite.changefeed"),
            pytest.raises(SurrealDbConnectionError) as info,
        ):
            await stream._with_retry(operation)
        assert str(info.value) == _R13
        assert info.value.retryable is False  # type: ignore[attr-defined]
        assert ran == []
        assert acquired == [True]
        assert [record for record in caplog.records if record.name == "surreal_orm_lite.changefeed"] == []

    async def test_the_default_keeps_its_v028_tolerance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``set_connection()`` and ``set_database()`` on the default leave its stream reading on the cached client."""
        async with _feed_databases("ws", "md_note"):
            stream = MdNote.objects().changes(poll_interval=0.05)
            await stream.start()
            client = await SurrealDBConnectionManager.get_client()
            await client.query("CREATE md_note:n1 SET text = 'one';", {})
            [event] = await _next_events(stream, 1)
            assert event.record_id == "md_note:n1"
            served: list[Any] = []
            default = default_connection_class()
            real = default.get_client

            async def recording(*args: Any, **kwargs: Any) -> Any:
                result = await real(*args, **kwargs)
                served.append(result)
                return result

            monkeypatch.setattr(default, "get_client", recording)
            SurrealDBConnectionManager.set_connection(
                url=_url("ws"), user="root", password="root", namespace="ns", database="md_a"
            )
            await client.query("CREATE md_note:n2 SET text = 'two';", {})
            [event] = await _next_events(stream, 1)
            assert event.record_id == "md_note:n2"
            await SurrealDBConnectionManager.set_database("db")
            await client.query("CREATE md_note:n3 SET text = 'three';", {})
            [event] = await _next_events(stream, 1)
            assert event.record_id == "md_note:n3"
            assert served and all(result is client for result in served)
            stream.stop()


# === Live streams keep their connection (spec §6.5, §7.6) =====================================================


@pytest.mark.usefixtures("clear_live_registry")
class TestLiveStreamBinding:
    """Spec §6.5 (no server, fake clients): a stream resolves its connection once when it starts — the one it is
    given, else the current one — and stops on it wherever it is stopped from."""

    @staticmethod
    def _starter(received: list[Any]) -> Callable[[Any], Awaitable[UUID]]:
        async def start(client: Any) -> UUID:
            received.append(client)
            return uuid4()

        return start

    async def test_a_stream_without_a_connection_starts_on_the_current_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        md_a, a_client = _named_connection(monkeypatch, "md_a")
        received: list[Any] = []
        stream = live.LiveStream("md_item", self._starter(received))
        async with SurrealDBConnectionManager.using("md_a"):
            await stream.start()
        assert received == [a_client]
        assert stream.live_id is not None
        assert live.owner_of(stream.live_id) == "md_a"
        live_id = stream.live_id
        await stream.stop()  # outside the block: still md_a's
        assert a_client.killed == [str(live_id)]

    async def test_the_given_connection_wins_over_the_block(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, a_client = _named_connection(monkeypatch, "md_a")
        md_b, b_client = _named_connection(monkeypatch, "md_b")
        received: list[Any] = []
        stream = live.LiveStream("md_item", self._starter(received), connection=lambda: md_b)
        async with SurrealDBConnectionManager.using("md_a"):
            await stream.start()
            assert stream.live_id is not None
            live_id = stream.live_id
            assert live.owner_of(live_id) == "md_b"
            await stream.stop()
        assert received == [b_client]
        assert b_client.killed == [str(live_id)]
        assert a_client.killed == []

    async def test_an_orphaned_start_is_killed_on_its_connection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A ``LIVE SELECT`` that answers after its stream stopped is killed on the stream's connection."""
        md_a, a_client = _named_connection(monkeypatch, "md_a")
        _, b_client = _named_connection(monkeypatch, "md_b")
        started: asyncio.Future[UUID] = asyncio.get_running_loop().create_future()
        orphan = uuid4()
        started.set_result(orphan)
        async with SurrealDBConnectionManager.using("md_b"):
            live._kill_when_started(started, md_a)
            await asyncio.gather(*list(live._ORPHAN_KILLS))
        assert a_client.killed == [str(orphan)]
        assert b_client.killed == []


@contextlib.asynccontextmanager
async def _live_connections(md_a_url: str | None = None) -> AsyncIterator[None]:
    """``md_a`` on database ``md_a`` (through *md_a_url*, else straight to the line under test), ``md_b`` and the
    default connection on database ``md_b``, all as root over WebSocket, with ``md_event`` and ``md_note`` defined in
    both databases (SurrealDB 3.x refuses to watch a missing table). Used with the ``unset_default`` fixture."""
    await _reset_databases(define=True)
    for database in ("md_a", "md_b"):
        async with _root(database) as root:
            await root.query("DEFINE TABLE md_event SCHEMALESS; DEFINE TABLE md_note SCHEMALESS;", {})
    SurrealDBConnectionManager.set_connection(url=_url("ws"), user="root", password="root", namespace="ns", database="md_b")
    SurrealDBConnectionManager.add_connection(
        "md_a", url=md_a_url or _url("ws"), user="root", password="root", namespace="ns", database="md_a"
    )
    _register("ws", "md_b", database="md_b")
    try:
        yield
    finally:
        for name in ("md_a", "md_b"):
            await SurrealDBConnectionManager.remove_connection(name)
        await SurrealDBConnectionManager.close_connection()
        await _reset_databases(define=False)


async def _create(connection: str, table: str, key: str) -> None:
    """A new record ``<table>:<key>`` written through *connection*'s client — always a new record, never an ``UPSERT``
    that changes nothing, which SurrealDB 2.7.0 does not notify (M10)."""
    client = await SurrealDBConnectionManager.get_client(connection)
    await client.query(f"CREATE {table}:{key} SET title = '{key}', text = '{key}';", {})


async def _lives(database: str, table: str) -> list[str]:
    """The uuids of the live queries the server runs on *table* of *database*, read by a root client of its own."""
    async with _root(database) as root:
        info = await root.query(f"INFO FOR TABLE {table};", {})
    return sorted(str(key) for key in (info.get("lives") or {}))


@pytest.mark.usefixtures("unset_default")
class TestRoutedLiveE2E:
    """AC-13 (both lines, ws): a live stream runs on its model's connection — or its ``using()`` block's — and keeps
    it to stop, resubscribe and check its identity; ``subscribe_live()``/``kill()`` find it through the uuid's owner;
    only that connection's teardown, its removal or its registration with other settings ends it."""

    async def test_a_stream_receives_its_connections_events_only(self) -> None:
        async with _live_connections(), MdEvent.objects().live() as stream:
            assert await _lives("md_a", "md_event") == [str(stream.live_id)]
            assert await _lives("md_b", "md_event") == []
            # The same table in the other database first: had the stream run there, this would be its first event.
            await _create("md_b", "md_event", "b1")
            await _create("default", "md_event", "d1")
            await MdEvent(id="a1", title="one").save()
            [event] = await _next_events(stream, 1)
            assert (event.record_id, event.instance.title) == ("md_event:a1", "one")

    async def test_only_its_own_connections_close_ends_it(self) -> None:
        async with _live_connections(), MdEvent.objects().live() as stream:
            await SurrealDBConnectionManager.get_client("md_b")
            await SurrealDBConnectionManager.close_connection("md_b")
            await SurrealDBConnectionManager.close_connection()  # the default's: not the stream's either
            await MdEvent(id="a1", title="one").save()
            [event] = await _next_events(stream, 1)
            assert event.record_id == "md_event:a1"
            await SurrealDBConnectionManager.close_connection("md_a")
            with pytest.raises(StopAsyncIteration):
                await _read_one(stream)
            assert stream.is_active is False

    async def test_a_stream_started_in_a_block_keeps_its_connection(self) -> None:
        async with _live_connections():
            async with SurrealDBConnectionManager.using("md_a"):
                stream = await MdNote.objects().live().start()
            try:
                assert stream.live_id is not None
                assert await _lives("md_a", "md_note") == [str(stream.live_id)]
                await _create("default", "md_note", "d1")
                await _create("md_a", "md_note", "a1")
                [event] = await _next_events(stream, 1)
                assert (event.record_id, event.instance.text) == ("md_note:a1", "a1")
            finally:
                await stream.stop()  # outside the block: the KILL goes to md_a
            assert await _lives("md_a", "md_note") == []

    async def test_subscribe_live_and_kill_find_the_streams_connection(self) -> None:
        async with _live_connections():
            stream = await MdEvent.objects().watch().start()
            try:
                assert stream.live_id is not None
                # Outside any using() block, where the current connection is the default.
                reader = SurrealDBConnectionManager.subscribe_live(stream.live_id)
                await MdEvent(id="a1", title="one").save()
                assert str((await _read_one(reader))["record"]) == "md_event:a1"
                assert str((await _read_one(stream))["record"]) == "md_event:a1"
                await SurrealDBConnectionManager.kill(stream.live_id)
                assert await _lives("md_a", "md_event") == []
                for ended in (reader, stream):
                    with pytest.raises(StopAsyncIteration):
                        await _read_one(ended)
            finally:
                await stream.stop()

    async def test_a_drop_resubscribes_on_its_connection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(live.LiveModelStream, "reconnect_delay", 0.05)
        reconnected = asyncio.Event()
        moves: list[tuple[UUID, UUID]] = []

        def on_reconnect(old: UUID, new: UUID) -> None:
            moves.append((old, new))
            reconnected.set()

        async with CuttableProxy() as proxy, _live_connections(proxy.url()):
            default_client = await SurrealDBConnectionManager.get_client()
            async with MdEvent.objects().live(on_reconnect=on_reconnect) as stream:
                first = await SurrealDBConnectionManager.get_client("md_a")
                old = stream.live_id
                proxy.cut()
                async with asyncio.timeout(10):
                    await reconnected.wait()
                assert old is not None and moves == [(old, stream.live_id)]
                assert proxy.connections == 2
                second = await SurrealDBConnectionManager.get_client("md_a")
                assert second is not first
                assert stream._stream._client is second
                await MdEvent(id="a1", title="after").save()
                [event] = await _next_events(stream, 1)
                assert (event.record_id, event.instance.title) == ("md_event:a1", "after")
                assert str(stream.live_id) in await _lives("md_a", "md_event")
            assert await SurrealDBConnectionManager.get_client() is default_client

    @pytest.mark.parametrize("change", _CHANGES)
    async def test_the_same_settings_keep_it_other_settings_end_it(self, change: str) -> None:
        async with _live_connections(), MdEvent.objects().live() as stream:
            _register("ws", "md_a", database="md_a")  # the same settings: nothing changes
            await MdEvent(id="a1", title="one").save()
            [event] = await _next_events(stream, 1)
            assert event.record_id == "md_event:a1"
            await _change(change, lambda: _register("ws", "md_a", database="md_b"))
            with pytest.raises(StopAsyncIteration):
                await _read_one(stream)
            assert stream.is_active is False

    async def test_the_deprecated_uuid_form_starts_on_the_models_connection(self) -> None:
        async with _live_connections():
            with pytest.warns(DeprecationWarning, match="async with"):
                live_id = await MdEvent.objects().live()
            assert await _lives("md_a", "md_event") == [str(live_id)]
            assert await _lives("md_b", "md_event") == []
            # The uuid is the caller's: no stream registered it, so kill() acts on the current connection.
            async with SurrealDBConnectionManager.using("md_a"):
                await SurrealDBConnectionManager.kill(live_id)
            assert await _lives("md_a", "md_event") == []
