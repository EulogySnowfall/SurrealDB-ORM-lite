"""Multi-database (v0.29.0): named connections, each on a connection class of its own behind a routing manager.

``SurrealDBConnectionManager`` holds no connection state any more: each connection — the default one included —
is a private subclass whose name-mangled class attributes are that connection's state, and every state-touching
classmethod of the public class resolves the connection class and runs there. Unit tests first (no server); the
classes that open an engine or a server are suffixed ``E2E``. Models are prefixed ``Md``, tables ``md_*``.
"""

import asyncio
import contextlib
import dataclasses
import inspect
import os
import threading
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from typing import Any

import pytest
import pytest_asyncio
from surrealdb import AsyncSurreal

import surreal_orm_lite
from surreal_orm_lite import (
    BaseSurrealModel,
    ConnectionConfig,
    SurrealConfigDict,
    SurrealDbConnectionError,
    SurrealDBConnectionManager,
    connection_manager,
    live,
)
from tests.conftest import default_connection_class

#: The prefix of the manager's name-mangled private attributes.
_P = "_SurrealDBConnectionManager__"

#: The classmethods of the class body that are deliberately not routed (spec §6.1): the resolvers themselves and
#: the registry, which acts by the name it is given.
_NOT_ROUTED = {"_route", "_manager_for", "add_connection", "remove_connection", "get_config", "list_connections"}

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
