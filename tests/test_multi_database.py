"""Multi-database (v0.29.0): named connections, each on a connection class of its own behind a routing manager.

``SurrealDBConnectionManager`` holds no connection state any more: each connection — the default one included —
is a private subclass whose name-mangled class attributes are that connection's state, and every state-touching
classmethod of the public class resolves the connection class and runs there. Unit tests first (no server); the
classes that open an engine or a server are suffixed ``E2E``. Models are prefixed ``Md``, tables ``md_*``.
"""

import asyncio
import inspect
import threading
from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import pytest

from surreal_orm_lite import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager, connection_manager, live
from tests.conftest import default_connection_class

#: The prefix of the manager's name-mangled private attributes.
_P = "_SurrealDBConnectionManager__"

#: The classmethods of the class body that are deliberately not routed (spec §6.1): the resolvers themselves.
_NOT_ROUTED = {"_route", "_manager_for"}

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
