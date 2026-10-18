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
import functools
import inspect
import logging
import os
import re
import sys
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Optional
from unittest import mock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import BaseModel, Field
from surrealdb import AsyncSurreal, RecordID

import surreal_orm_lite
from surreal_orm_lite import (
    AuthenticatedUserMixin,
    BaseSurrealEdge,
    BaseSurrealModel,
    ConnectionConfig,
    Count,
    Q,
    QueryCache,
    QuerySet,
    Subquery,
    SurrealConfigDict,
    SurrealDbAuthenticationError,
    SurrealDbConnectionError,
    SurrealDBConnectionManager,
    SurrealDbError,
    Transaction,
    cache,
    connection_manager,
    embedded,
    live,
    post_delete,
    post_save,
    post_update,
    pre_delete,
    pre_save,
    pre_update,
    routing,
    time_travel,
)
from surreal_orm_lite.testing import SurrealFixture, embedded_database, fixture
from tests._proxy import CuttableProxy
from tests.conftest import default_connection_class
from tests.test_embedded import (
    _EMBEDDED_BEHAVIOUR,
    _cells,
    _normalised,
    _readme,
    _readme_part,
    _subsection,
    _table_rows,
    _tables,
)
from tests.test_paging import _PAGING_ROW
from tests.test_query_cache import _QUERY_CACHE_ROW, _bullets
from tests.test_time_travel import _SERVER_BEHAVIOUR, _SERVER_TABLE_WIDTHS, _forbidden_client

#: The prefix of the manager's name-mangled private attributes.
_P = "_SurrealDBConnectionManager__"

#: The classmethods of the class body that are deliberately not routed (spec §6.1): the resolvers themselves and
#: the registry, which acts by the name it is given.
_NOT_ROUTED = {
    "_route",
    "_manager_for",
    "_register_for_block",
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


#: The routed classmethods whose first parameter after ``cls`` is the connection ``name`` (spec D26), with what
#: ``inspect.signature()`` showed for each on the router in v0.28.0 — and still shows: ``name`` is hidden from it.
_TAKES_NAME = {
    "get_client": "() -> Any",
    "close_connection": "() -> None",
    "transaction": "() -> Any",
}


def _takes_name(function: Callable[..., Any]) -> bool:
    """Whether *function* declares ``name`` first after ``cls``."""
    parameters = list(inspect.signature(function).parameters)
    return len(parameters) > 1 and parameters[1] == "name"


def _without_name(signature: inspect.Signature) -> inspect.Signature:
    """*signature* without its ``name`` parameter: what a routed method taking a name shows (spec D26)."""
    return signature.replace(parameters=[p for p in signature.parameters.values() if p.name != "name"])


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
            if _takes_name(original):
                # D26: name is hidden from introspection; every other parameter and the return annotation stay.
                assert inspect.signature(function) == _without_name(inspect.signature(original)), name
            else:
                assert inspect.signature(function) == inspect.signature(original), name
            assert function.__name__ == original.__name__, name
            assert name.endswith(function.__name__), name
            assert function.__doc__ == original.__doc__, name
        for name in ("get_client", "close_connection", "signin", "signup", "kill", "reconnect", "set_url"):
            assert inspect.iscoroutinefunction(getattr(SurrealDBConnectionManager, name)), name
        for name in ("transaction", "ephemeral_client", "is_connected", "subscribe_live"):
            assert not inspect.iscoroutinefunction(getattr(SurrealDBConnectionManager, name)), name

    def test_exactly_three_routed_methods_take_a_name_and_none_shows_it(self) -> None:
        """D26: the rule lives in ``_routed``, so a new method taking a name cannot forget it; changing the set of
        methods that take one is deliberate."""
        default = SurrealDBConnectionManager._manager_for("default")
        taking: set[str] = set()
        for name in _routed_names():
            function = vars(SurrealDBConnectionManager)[name].__func__
            if _takes_name(inspect.unwrap(function)):
                taking.add(name)
            for introspected in (function, getattr(SurrealDBConnectionManager, name), getattr(default, name)):
                assert "name" not in inspect.signature(introspected).parameters, name
        assert taking == set(_TAKES_NAME)

    @pytest.mark.parametrize("method", sorted(_TAKES_NAME))
    def test_name_is_hidden_from_introspection(self, method: str) -> None:
        """D26: v0.28.0's ``()`` on the router and on every connection class, so FastAPI's
        ``Depends(SurrealDBConnectionManager.get_client)`` adds no ``?name=``; ``inspect.unwrap`` keeps ``name``."""
        SurrealDBConnectionManager.add_connection("md_a", url="mem://", namespace="t", database="a")
        owners = (
            SurrealDBConnectionManager,
            SurrealDBConnectionManager._manager_for("default"),
            SurrealDBConnectionManager._manager_for("md_a"),
        )
        for owner in owners:
            signature = inspect.signature(getattr(owner, method))
            assert list(signature.parameters) == [], owner
            assert str(signature) == _TAKES_NAME[method], owner
        function = vars(SurrealDBConnectionManager)[method].__func__
        assert "name" in inspect.signature(inspect.unwrap(function)).parameters
        assert list(inspect.signature(function).parameters) == ["cls"]
        assert function.__surreal_routed__ == "current"

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
async def _root(database: str, url: str | None = None) -> AsyncIterator[Any]:
    """A raw SDK client signed in as root on database *database* of namespace ``ns`` (never the ORM's), on the line
    under test or on the WebSocket *url*."""
    url = url or _url("ws")
    client = AsyncSurreal(url)
    await client.connect(url)
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


async def _root_rows(database: str, table: str, url: str | None = None) -> list[Any]:
    """What a root client reads from *table* of database *database* (on the line under test, or on *url*): proof of
    where a write landed."""
    async with _root(database, url) as client:
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
        """Hidden from ``inspect.signature()`` (D26), ``name`` still routes, by position and by keyword."""
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
        reopened = await SurrealDBConnectionManager.get_client("md_a")
        assert reopened is not client
        assert conn.is_connected() is True
        await SurrealDBConnectionManager.close_connection(name="md_a")
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


def _manager_attributes() -> list[tuple[str, str, int]]:
    """``(path, attribute, line)`` of every ``SurrealDBConnectionManager.<attribute>`` node in the package's code
    outside ``connection_manager.py`` (AST nodes: docstrings and comments do not count)."""
    package = Path(surreal_orm_lite.__file__).parent
    found: list[tuple[str, str, int]] = []
    for path in sorted(package.rglob("*.py")):
        relative = path.relative_to(package).as_posix()
        if relative == "connection_manager.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
                continue
            if node.value.id == "SurrealDBConnectionManager":
                found.append((relative, node.attr, node.lineno))
    return found


class TestSingleResolver:
    """AC-7: outside ``connection_manager.py`` the package reaches a connection through its resolvers only — with no
    allowlist."""

    def test_every_call_goes_through_a_resolved_connection(self) -> None:
        found = _manager_attributes()
        # The scan sees the package: the models' resolver names _manager_for, the testing helpers _register_for_block.
        assert ("model_base.py", "_manager_for") in {(path, attribute) for path, attribute, _ in found}
        assert ("testing/database.py", "_register_for_block") in {(path, attribute) for path, attribute, _ in found}
        offenders = [f"{path}:{line} {attribute}" for path, attribute, line in found if attribute not in _RESOLVERS]
        assert offenders == []


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


async def _offline() -> Any:
    """A substitute of a manager method, put on ``SurrealDBConnectionManager`` by hand: no database here."""
    raise ConnectionError("no database in this test")


#: The routed methods the hand-patching tests replace: a public coroutine, a private one, a context-manager factory.
_HAND_PATCHED = ("get_client", "_open_client", "transaction")


class TestHandPatchedMethods:
    """README section 30, "Which connection a manager method uses": a manager method replaced by hand on
    ``SurrealDBConnectionManager`` is inherited by every connection. Put back from ``vars()``, it is the classmethod
    again; ``SurrealDBConnectionManager.<name>`` is a method bound to the router, which every connection would inherit
    (no server, nothing opened)."""

    @pytest.mark.parametrize("name", _HAND_PATCHED)
    def test_put_back_from_vars_each_connection_runs_on_itself(self, name: str) -> None:
        SurrealDBConnectionManager.add_connection("md_a", url="mem://", namespace="t", database="a")
        connections = [SurrealDBConnectionManager._manager_for(each) for each in ("default", "md_a")]
        saved = vars(SurrealDBConnectionManager)[name]
        assert isinstance(saved, classmethod)
        setattr(SurrealDBConnectionManager, name, _offline)
        try:
            for connection in connections:
                assert getattr(connection, name) is _offline, connection
        finally:
            setattr(SurrealDBConnectionManager, name, saved)
        assert vars(SurrealDBConnectionManager)[name] is saved
        for connection in connections:
            assert getattr(connection, name).__self__ is connection, connection

    @pytest.mark.parametrize("name", _HAND_PATCHED)
    def test_monkeypatch_and_patch_object_put_the_classmethod_back(self, name: str) -> None:
        """What the README says pytest's ``monkeypatch.setattr()`` and ``unittest.mock.patch.object()`` do."""
        saved = vars(SurrealDBConnectionManager)[name]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(SurrealDBConnectionManager, name, _offline)
            assert vars(SurrealDBConnectionManager)[name] is _offline
        assert vars(SurrealDBConnectionManager)[name] is saved
        with mock.patch.object(SurrealDBConnectionManager, name, _offline):
            assert vars(SurrealDBConnectionManager)[name] is _offline
        assert vars(SurrealDBConnectionManager)[name] is saved

    @pytest.mark.parametrize("name", _HAND_PATCHED)
    def test_the_bound_method_put_back_is_bound_to_the_router(self, name: str) -> None:
        """What the README warns against: put back, ``SurrealDBConnectionManager.<name>`` is what every connection
        inherits, and its ``cls`` is the router, which resolves the current connection."""
        SurrealDBConnectionManager.add_connection("md_a", url="mem://", namespace="t", database="a")
        connection = SurrealDBConnectionManager._manager_for("md_a")
        saved = vars(SurrealDBConnectionManager)[name]
        bound = getattr(SurrealDBConnectionManager, name)
        assert bound.__self__ is SurrealDBConnectionManager
        setattr(SurrealDBConnectionManager, name, bound)
        try:
            assert getattr(connection, name).__self__ is SurrealDBConnectionManager
        finally:
            setattr(SurrealDBConnectionManager, name, saved)
        assert getattr(connection, name).__self__ is connection


@pytest.mark.usefixtures("unset_default")
class TestHandPatchedMethodsE2E:
    """The same on ``mem://``, as the README states it: after a hand-made substitute of ``get_client``, a model routed
    to ``md_a`` writes ``md_a`` when the classmethod is put back from ``vars()``, and the default connection's database,
    with no error, when ``SurrealDBConnectionManager.get_client`` saved before the patch is put back."""

    @pytest.mark.parametrize(("put_back", "lands_in"), [("vars", "md_a"), ("bound", "default")])
    async def test_where_a_routed_model_writes(self, put_back: str, lands_in: str) -> None:
        async with _routed_databases("mem"):
            saved = vars(SurrealDBConnectionManager)["get_client"]
            restored = saved if put_back == "vars" else SurrealDBConnectionManager.get_client
            SurrealDBConnectionManager.get_client = _offline  # type: ignore[method-assign]
            try:
                with pytest.raises(ConnectionError, match="no database in this test"):
                    await MdEvent(id="e0", title="zero").save()
                SurrealDBConnectionManager.get_client = restored  # type: ignore[method-assign]
                await MdEvent(id="e1", title="one").save()
            finally:
                SurrealDBConnectionManager.get_client = saved  # type: ignore[method-assign]
            elsewhere = "default" if lands_in == "md_a" else "md_a"
            assert _titles(await _rows_in("mem", lands_in, "md_event")) == ["one"]
            assert await _rows_in("mem", elsewhere, "md_event") == []


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


# === One transaction, one connection (spec §6.1 tagging, §6.3, §6.6, §7.1, X1, X1m) =========================


def _x1(operation: str, tx_name: str, model: str, model_name: str) -> str:
    """X1's exact text (spec §5.7)."""
    return (
        f"{operation} cannot run in this transaction: the transaction belongs to connection {tx_name!r} and {model} "
        f"runs on connection {model_name!r}. A transaction is one connection's (one database): open it on "
        f"{model_name!r} — SurrealDBConnectionManager.transaction({model_name!r}) — or run the block inside "
        f"using({tx_name!r})."
    )


def _x1m(tx_name: str, name: str) -> str:
    """X1m's exact text (spec §5.7)."""
    return (
        f"call_function() cannot run in this transaction: the transaction belongs to connection {tx_name!r} and the "
        f"call runs on connection {name!r}. A transaction is one connection's (one database): call it inside "
        f"using({tx_name!r}), or open the transaction on {name!r}."
    )


def _takes_tx(value: Any) -> bool:
    try:
        return "tx" in inspect.signature(value).parameters
    except (TypeError, ValueError):
        return False


def _tx_methods() -> list[tuple[str, str]]:
    """``(owner, name)`` of every public method with a ``tx`` parameter (spec §6.6, AC-15), found by introspection:
    the models' — ``BaseSurrealModel``, ``BaseSurrealEdge``, ``AuthenticatedUserMixin`` — and the manager's
    ``call_function()``. A queryset joins a transaction through ``objects(tx=)`` only."""
    found = [
        (owner.__name__, name)
        for owner in (BaseSurrealModel, BaseSurrealEdge, AuthenticatedUserMixin)
        for name in sorted(dir(owner))
        if not name.startswith("_") and callable(getattr(owner, name, None)) and _takes_tx(getattr(owner, name))
    ]
    return [*found, ("SurrealDBConnectionManager", "call_function")]


_TX_METHODS = _tx_methods()
_TX_IDS = [f"{owner}.{name}" for owner, name in _TX_METHODS]

#: How the tests call each model method of :data:`_TX_METHODS` with the transaction ``tx``, on *target* — an
#: instance of the owner's test model (``MdEvent``, ``MdLikes``, ``MdMember``); classmethods on its class.
_TX_CALLS: dict[str, Callable[[Any, Any], Any]] = {
    "atomic_append": lambda m, tx: m.atomic_append("title", "x", tx=tx),
    "atomic_append_many": lambda m, tx: m.atomic_append_many("title", ["x"], tx=tx),
    "atomic_increment": lambda m, tx: m.atomic_increment("n", tx=tx),
    "atomic_remove": lambda m, tx: m.atomic_remove("title", "x", tx=tx),
    "atomic_remove_many": lambda m, tx: m.atomic_remove_many("title", ["x"], tx=tx),
    "atomic_set_add": lambda m, tx: m.atomic_set_add("title", "x", tx=tx),
    "atomic_set_add_many": lambda m, tx: m.atomic_set_add_many("title", ["x"], tx=tx),
    "call_function": lambda m, tx: type(m).call_function("fn::md_ping", tx=tx),
    "define_access": lambda m, tx: type(m).define_access(tx=tx),
    "define_computed_fields": lambda m, tx: type(m).define_computed_fields(tx=tx),
    "delete": lambda m, tx: m.delete(tx=tx),
    "merge": lambda m, tx: m.merge(tx=tx, n=1),
    "objects": lambda m, tx: type(m).objects(tx=tx),
    "patch": lambda m, tx: m.patch([{"op": "replace", "path": "/n", "value": 1}], tx=tx),
    "refresh": lambda m, tx: BaseSurrealModel.refresh(m, tx=tx),
    "relate": lambda m, tx: m.relate("md_follows", MdEvent(id="b"), tx=tx),
    "remove_all_relations": lambda m, tx: m.remove_all_relations("md_follows", tx=tx),
    "remove_relation": lambda m, tx: m.remove_relation("md_follows", MdEvent(id="b"), tx=tx),
    "save": lambda m, tx: m.save(tx=tx),
    "update": lambda m, tx: m.update(tx=tx),
    "upsert": lambda m, tx: m.upsert(tx=tx),
}


def _tx_target(owner: str) -> Any:
    """An instance of the test model of *owner*: each is routed to ``md_a``."""
    if owner == "BaseSurrealEdge":
        return MdLikes(id="l1", in_=RecordID("md_event", "a"), out=RecordID("md_event", "b"))
    if owner == "AuthenticatedUserMixin":
        return MdMember(id="m1", email="m@example.com", password="p")
    return MdEvent(id="e1", title="x")


async def _call_with_tx(owner: str, name: str, target: Any, tx: Any) -> Any:
    """Call the method *owner*.*name* with the transaction *tx* (on *target* for a model method)."""
    if owner == "SurrealDBConnectionManager":
        return await SurrealDBConnectionManager.call_function("fn::md_ping", tx=tx)
    result = _TX_CALLS[name](target, tx)
    return await result if inspect.isawaitable(result) else result


def _expected_x1(owner: str, name: str, target: Any, tx_name: str, name_of_call: str = "md_a") -> str:
    """The X1 (X1m for the manager) a call of :func:`_call_with_tx` raises with a transaction of *tx_name*."""
    if owner == "SurrealDBConnectionManager":
        return _x1m(tx_name, name_of_call)
    model = type(target).__name__
    operation = f"{model}.objects(tx=)" if name == "objects" else f"{model}.{name}()"
    return _x1(operation, tx_name, model, "md_a")


_SIGNALS = (pre_save, post_save, pre_update, post_update, pre_delete, post_delete)


@contextlib.contextmanager
def _signal_spy(*models: type) -> Iterator[list[str]]:
    """Record the name of every ``pre_*``/``post_*`` signal sent for *models*; the handlers leave on exit."""
    fired: list[str] = []
    handlers: list[tuple[Any, Any]] = []

    def recorder(name: str) -> Callable[..., Coroutine[Any, Any, None]]:
        async def handler(sender: type, **kwargs: Any) -> None:
            fired.append(name)

        return handler

    for signal in _SIGNALS:
        handler = recorder(signal.name)
        handlers.append((signal, handler))
        for model in models:
            signal.connect(model)(handler)
    try:
        yield fired
    finally:
        for signal, handler in handlers:
            for model in models:
                signal.disconnect(handler, model)


def _record_cache_writes(patched: pytest.MonkeyPatch) -> list[Any]:
    """Record the tables of every ``cache.writing()`` block an ORM write method enters from now on (spec §6.4: the
    guard runs before it, so a refused write invalidates nothing and notes nothing on the transaction)."""
    entered: list[Any] = []
    real = cache.writing

    @contextlib.asynccontextmanager
    async def recording(tables: Any, tx: Any = None) -> AsyncIterator[None]:
        entered.append(tables)
        async with real(tables, tx):
            yield

    patched.setattr(cache, "writing", recording)
    return entered


@contextlib.asynccontextmanager
async def _mem_default_and_md_a() -> AsyncIterator[None]:
    """The default connection (database ``md_b``) and ``md_a`` (database ``md_a``) on ``mem://``, an engine each.
    Used with the ``unset_default`` fixture."""
    SurrealDBConnectionManager.set_connection(url="mem://", namespace="ns", database="md_b")
    SurrealDBConnectionManager.add_connection("md_a", url="mem://", namespace="ns", database="md_a")
    try:
        yield
    finally:
        await SurrealDBConnectionManager.remove_connection("md_a")
        await SurrealDBConnectionManager.close_connection()


@pytest.mark.usefixtures("unset_default")
class TestTransactionPerConnection:
    """AC-15 (``mem://``): a transaction carries the name of the connection it was opened on, and the models of that
    connection write in it."""

    async def test_the_transaction_is_tagged_with_its_connection(self) -> None:
        async with _mem_default_and_md_a():
            async with SurrealDBConnectionManager.transaction("md_a") as tx:
                assert tx._connection_name == "md_a"
            async with SurrealDBConnectionManager.using("md_a"), SurrealDBConnectionManager.transaction() as tx:
                assert tx._connection_name == "md_a"
            async with SurrealDBConnectionManager.transaction() as tx:
                assert tx._connection_name == "default"
            assert Transaction._connection_name is None

    async def test_a_model_of_the_connection_commits_in_it(self) -> None:
        async with _mem_default_and_md_a():
            async with SurrealDBConnectionManager.transaction("md_a") as tx:
                await MdEvent(id="e1", title="in md_a").save(tx=tx)
            async with SurrealDBConnectionManager.using("md_a"), SurrealDBConnectionManager.transaction() as tx:
                await MdNote(id="n1", text="in md_a too").save(tx=tx)
            md_a = await SurrealDBConnectionManager.get_client("md_a")
            default = await SurrealDBConnectionManager.get_client()
            assert _titles(await _rows_of(md_a, "md_event")) == ["in md_a"]
            assert [row["text"] for row in await _rows_of(md_a, "md_note")] == ["in md_a too"]
            assert await _rows_of(default, "md_event") == []
            assert await _rows_of(default, "md_note") == []


@pytest.mark.usefixtures("unset_default")
class TestTransactionPerConnectionE2E:
    """AC-15 (both lines, ws and http): ``transaction("md_a")`` and ``using("md_a")`` + ``transaction()`` run on
    ``md_a`` — interactive on 3.x over WebSocket, buffered otherwise."""

    @pytest.mark.parametrize("kind", ["ws", "http"])
    async def test_a_model_of_the_connection_commits_in_it(self, kind: str) -> None:
        async with _routed_databases(kind):
            async with SurrealDBConnectionManager.transaction("md_a") as tx:
                assert tx._connection_name == "md_a"
                await MdEvent(id="e1", title="in md_a").save(tx=tx)
            async with SurrealDBConnectionManager.using("md_a"), SurrealDBConnectionManager.transaction() as tx:
                assert tx._connection_name == "md_a"
                await MdNote(id="n1", text="in md_a too").save(tx=tx)
            assert _titles(await _root_rows("md_a", "md_event")) == ["in md_a"]
            assert [row["text"] for row in await _root_rows("md_a", "md_note")] == ["in md_a too"]
            assert await _root_rows("md_b", "md_event") == []
            assert await _root_rows("md_b", "md_note") == []


@pytest.mark.usefixtures("unset_default")
class TestTransactionGuard:
    """AC-15, X1, X1m (``mem://``): every public method with a ``tx`` parameter refuses a transaction of another
    connection before anything — no client acquired, nothing queued on the transaction, no signal, no cache write —
    and a model of the transaction's own connection still commits in the same block."""

    def test_the_list_is_complete(self) -> None:
        names = {name for owner, name in _TX_METHODS if owner != "SurrealDBConnectionManager"}
        assert names == set(_TX_CALLS)
        owners = {owner for owner, _ in _TX_METHODS}
        assert owners == {"BaseSurrealModel", "BaseSurrealEdge", "AuthenticatedUserMixin", "SurrealDBConnectionManager"}
        public = [name for name in dir(QuerySet) if not name.startswith("_")]
        assert [name for name in public if _takes_tx(getattr(QuerySet, name))] == []

    @pytest.mark.parametrize(("owner", "name"), _TX_METHODS, ids=_TX_IDS)
    async def test_another_connections_transaction_is_refused(
        self, owner: str, name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = _tx_target(owner)
        async with _mem_default_and_md_a():
            async with SurrealDBConnectionManager.transaction() as tx:
                with monkeypatch.context() as patched, _signal_spy(MdEvent, MdLikes, MdMember) as fired:
                    patched.setattr(SurrealDBConnectionManager, "get_client", staticmethod(_forbidden_client))
                    writes = _record_cache_writes(patched)
                    stats = QueryCache.stats()
                    with pytest.raises(SurrealDbError) as info:
                        if owner == "SurrealDBConnectionManager":
                            async with SurrealDBConnectionManager.using("md_a"):
                                await _call_with_tx(owner, name, target, tx)
                        else:
                            await _call_with_tx(owner, name, target, tx)
                    assert str(info.value) == _expected_x1(owner, name, target, "default")
                    assert fired == []
                    assert writes == []
                    assert QueryCache.stats() == stats
                    assert tx.is_empty
                    assert tx._cache_written() == frozenset()
                await MdCustomer(id="c1", name="kept").save(tx=tx)
            assert [customer.name for customer in await MdCustomer.objects().exec()] == ["kept"]
            md_a = await SurrealDBConnectionManager.get_client("md_a")
            for table in ("md_event", "md_likes", "md_member", "md_follows"):
                assert await _rows_of(md_a, table) == [], table


class _TrapReached(Exception):
    """A method body used the object as its transaction: past every guard, as an untagged object should go."""


#: The modules of the guards (``routing.check_transaction()``, the ``invalidates()`` wrapper and ``writing()``).
_GUARD_MODULES = frozenset({"surreal_orm_lite.routing", "surreal_orm_lite.cache"})


class _Trap:
    """A transaction object no guard may ask anything — ``tests/test_time_travel.py``'s ``_UntouchableTx`` (spec §6.3).

    ``inspect.getattr_static`` reads the instance ``__dict__`` and the class without calling ``__getattr__``; any other
    read of a missing attribute lands here and is recorded in ``asked``. The tag (``_connection_name``), or any question
    from a guard module, fails the test. A method body that uses the object as its transaction once the guards let it
    through gets :class:`_TrapReached` and stops there: what it does with a fake transaction is not under test.
    """

    def __init__(self) -> None:
        self.asked: list[str] = []

    def __getattr__(self, name: str) -> Any:
        self.asked.append(name)
        caller = sys._getframe(1).f_globals.get("__name__")
        if name == "_connection_name" or caller in _GUARD_MODULES:
            pytest.fail(f"the guard asked the transaction for {name!r} ({caller})")
        raise _TrapReached(name)


def _tagged_trap(name: str) -> _Trap:
    """A trap tagged with connection *name* through its instance ``__dict__``, as ``transaction()`` tags one."""
    trap = _Trap()
    trap.__dict__["_connection_name"] = name
    return trap


@pytest.mark.usefixtures("unset_default")
class TestGuardNeverAsksTheTransaction:
    """AC-15, spec §6.3: the tag is read statically and first; the transaction object itself is never asked."""

    def test_check_transaction_reads_the_tag_statically(self) -> None:
        untagged = _Trap()
        assert routing.check_transaction(MdEvent, untagged, "op") is None
        tagged = _tagged_trap("md_b")
        with pytest.raises(SurrealDbError) as info:
            routing.check_transaction(MdEvent, tagged, "op")
        assert str(info.value) == _x1("op", "md_b", "MdEvent", "md_a")
        assert untagged.asked == []
        assert tagged.asked == []

    def test_only_a_str_tag_is_a_connection(self) -> None:
        """No transaction, a transaction the manager did not tag, and a tag that is not a ``str``: nothing checked."""
        assert routing.check_transaction(MdEvent, None, "op") is None
        assert routing.check_transaction(MdEvent, object(), "op") is None
        for tag in (None, 1, b"md_b"):
            trap = _Trap()
            trap.__dict__["_connection_name"] = tag
            assert routing.check_transaction(MdEvent, trap, "op") is None
            assert trap.asked == []
        assert routing.check_transaction(MdEvent, _tagged_trap("md_a"), "op") is None

    @pytest.mark.parametrize(("owner", "name"), _TX_METHODS, ids=_TX_IDS)
    async def test_a_tagged_trap_is_refused_unasked(self, owner: str, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", staticmethod(_forbidden_client))
        target = _tx_target(owner)
        trap = _tagged_trap("md_b")
        with _signal_spy(MdEvent, MdLikes, MdMember) as fired, pytest.raises(SurrealDbError) as info:
            await _call_with_tx(owner, name, target, trap)
        assert str(info.value) == _expected_x1(owner, name, target, "md_b", name_of_call="default")
        assert trap.asked == []
        assert fired == []

    @pytest.mark.parametrize(("owner", "name"), _TX_METHODS, ids=_TX_IDS)
    async def test_an_untagged_trap_reaches_the_method_unasked(self, owner: str, name: str) -> None:
        """On a model of the default connection (``using("default")``): no X1, and no guard asked the object."""
        trap = _Trap()
        async with _mem_default_and_md_a(), SurrealDBConnectionManager.using("default"):
            try:
                await _call_with_tx(owner, name, _tx_target(owner), trap)
            except Exception as exc:  # the body's own behaviour with a fake transaction is not under test
                assert "cannot run in this transaction" not in str(exc)
        assert "_connection_name" not in trap.asked


@pytest.mark.usefixtures("unset_default")
class TestTransactionGenerationModelsE2E:
    """AC-28 item 4 with models (both lines; ws, http and ``mem://``): a block whose connection is registered again
    with other settings, or removed, while it is open commits nothing, fires no deferred ``post_*`` signal and
    invalidates no cached read (R12; spec §7.1). Completes ``TestTransactionGenerationE2E`` (raw writes)."""

    @pytest.mark.parametrize("change", _CHANGES)
    @pytest.mark.parametrize("kind", _KINDS)
    async def test_nothing_is_committed_signalled_or_invalidated(
        self, kind: str, change: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        QueryCache.clear()
        async with _routed_databases(kind):
            old = await MdEvent(id="old", title="old").save()
            client = await SurrealDBConnectionManager.get_client("md_a")
            sent = _record_raw(monkeypatch, client)
            # The same table, cached on another connection: a committed md_event write would invalidate it (D16).
            async with SurrealDBConnectionManager.using("default"):
                assert await MdEvent.objects().cache().exec() == []
                assert await MdEvent.objects().cache().exec() == []
            hits = QueryCache.stats()["hits"]
            refused = pytest.raises(SurrealDbConnectionError, match="nothing was committed")
            with _signal_spy(MdEvent) as fired, refused as info:
                async with SurrealDBConnectionManager.transaction("md_a") as tx:
                    await MdEvent(id="e1", title="x").save(tx=tx)
                    await old.delete(tx=tx)
                    await _change(change, lambda: _register(kind, "md_a", database="md_b"))
            assert info.value.retryable is False  # type: ignore[attr-defined]
            assert "'md_a'" in str(info.value)
            assert "registered again with other settings" in str(info.value)
            assert [name for name in fired if name.startswith("post_")] == []
            async with SurrealDBConnectionManager.using("default"):
                assert await MdEvent.objects().cache().exec() == []
            assert QueryCache.stats()["hits"] == hits + 1
            await _drain_background()
            assert not any("BEGIN TRANSACTION" in statement for statement in sent), sent
            if kind != "mem":
                assert _titles(await _root_rows("md_a", "md_event")) == ["old"]
                assert await _root_rows("md_b", "md_event") == []
            elif change == "re-register":
                assert _titles(await _engine_rows(client, "md_a", "md_event")) == ["old"]
                assert await _engine_rows(client, "md_b", "md_event") == []


# === Cross-connection guards: subqueries, relations, links (spec §6.3, §6.4, X2–X4) ==========================


class MdRates(BaseSurrealEdge):
    """An edge model of the default connection."""

    model_config = SurrealConfigDict(table_name="md_rates")

    stars: int = 1


class MdAuthor(BaseSurrealModel):
    """Routed to ``md_a``: the model the ``author`` link of :class:`MdPost` names."""

    model_config = SurrealConfigDict(table_name="md_author", connection="md_a")

    id: str | None = None
    name: str = ""


class MdTag(BaseSurrealModel):
    """Routed to ``md_a``: the model the ``tags`` links of :class:`MdPost` name."""

    model_config = SurrealConfigDict(table_name="md_tag", connection="md_a")

    id: str | None = None
    label: str = ""


class MdAddress(BaseModel):
    """A nested Pydantic object: a dotted path into it follows no link."""

    city: str = ""


class MdPost(BaseSurrealModel):
    """On the default connection, with links typed with models of ``md_a`` and of the default connection, an
    untyped link (``RecordID``) and a nested object."""

    model_config = SurrealConfigDict(table_name="md_post")

    id: str | None = None
    title: str = ""
    author: MdAuthor | RecordID | None = None
    tags: list[MdTag | RecordID] | None = None
    reviewer: MdCustomer | RecordID | None = None
    editor: RecordID | None = None
    address: MdAddress | None = None


class MdShapes(BaseSurrealModel):
    """Every annotation shape ``routing.linked_models()`` walks (spec §6.3)."""

    model_config = SurrealConfigDict(table_name="md_shapes")

    id: str | None = None
    optional: Optional[MdAuthor] = None  # noqa: UP045 (typing.Optional on purpose)
    union: MdAuthor | MdTag | RecordID | None = None
    annotated: Annotated[MdAuthor | None, "meta"] = None
    listed: list[MdTag] = []
    nested: Optional[Annotated[list[MdTag], "meta"]] = None  # noqa: UP045 (typing.Optional on purpose)
    mapped: dict[str, MdAuthor] = {}
    sequence: Sequence[MdTag] = ()
    tupled: tuple[MdTag, ...] = ()
    twice: MdAuthor | list[MdAuthor] | None = None
    plain: RecordID | None = None
    text: str = ""
    aliased: MdAuthor | None = Field(default=None, alias="by")


def _x2(inner: str, inner_name: str, outer: str, outer_name: str) -> str:
    """X2's exact text (spec §5.7)."""
    return (
        f"{inner} (connection {inner_name!r}) cannot be read in a subquery of {outer} (connection {outer_name!r}): "
        f"SurrealDB runs a subquery in the database of the statement around it. Read {inner} first through its own "
        "connection and filter on its values, or route both models to one connection."
    )


def _x3(operation: str, a: str, a_name: str, b: str, b_name: str) -> str:
    """X3's exact text (spec §5.7)."""
    return (
        f"{operation} cannot relate records of two connections: {a} runs on connection {a_name!r} and {b} on "
        f"connection {b_name!r}. A relation is stored in one database: route the models (and the edge model) to one "
        "connection."
    )


def _x4(model: str, field: str, linked: str, linked_name: str, model_name: str) -> str:
    """X4's exact text (spec §5.7)."""
    return (
        f"{model}.{field} links to {linked}, which runs on connection {linked_name!r}, while {model} runs on "
        f"connection {model_name!r}: SurrealDB resolves a record link in the database of the statement, so following "
        f"it would read the wrong database. Read {linked} through its own connection, or route both models to one "
        "connection."
    )


class _RelationSubstitute(_SubstituteClient):
    """The substitute client of AC-1, with the SDK call a typed edge is created through."""

    async def insert_relation(self, *args: Any, **kwargs: Any) -> Any:
        raise _Sentinel("insert_relation")


@contextlib.contextmanager
def _substitute(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every client the ORM acquires is :class:`_RelationSubstitute`: a call that passed the guards raises
    :class:`_Sentinel` at its first request."""

    async def fake() -> Any:
        return _RelationSubstitute()

    with monkeypatch.context() as patched:
        patched.setattr(SurrealDBConnectionManager, "get_client", staticmethod(fake))
        yield


async def _run(call: Callable[[], Any]) -> Any:
    """Run *call*: await the coroutine it returns (a queryset read or write), else return what it returns (the stream
    of ``live()`` or ``watch()``, which is never awaited: ``await qs.live()`` is the deprecated uuid form)."""
    result = call()
    return await result if inspect.iscoroutine(result) else result


#: Reads, writes and streams of a queryset whose filter value is a subquery of ``md_a`` while the statement runs on
#: the default connection: each raises X2 when it compiles, before any request.
_X2_CALLS: dict[str, Callable[[QuerySet], Any]] = {
    "exec": lambda qs: qs.exec(),
    "count": lambda qs: qs.count(),
    "exists": lambda qs: qs.exists(),
    "cache().exec": lambda qs: qs.cache().exec(),
    "bulk_delete": lambda qs: qs.bulk_delete(),
    "bulk_update": lambda qs: qs.bulk_update(name="x"),
    "live": lambda qs: qs.live(),
    "watch": lambda qs: qs.watch(),
}


class TestSubqueryGuard:
    """AC-16, X2 (no server): a subquery whose model runs on another connection than the statement around it is
    refused when the statement compiles — before any request, whatever the depth, in a live query too; the
    names are compared, never looked up (``md_a`` is not even registered here). Same-connection subqueries compile
    as in v0.28.0."""

    @pytest.mark.parametrize("call", list(_X2_CALLS), ids=list(_X2_CALLS))
    async def test_a_queryset_filter_value_of_another_connection_is_refused(
        self, call: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", staticmethod(_forbidden_client))
        qs = MdCustomer.objects().filter(id__in=MdEvent.objects().select("id"))
        with pytest.raises(SurrealDbError) as info:
            await _run(lambda: _X2_CALLS[call](qs))
        assert str(info.value) == _x2("MdEvent", "md_a", "MdCustomer", "default")

    async def test_a_subquery_object_and_a_q_are_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", staticmethod(_forbidden_client))
        inner = MdEvent.objects().filter(title="x").select("title")
        expected = _x2("MdEvent", "md_a", "MdCustomer", "default")
        for qs in (
            MdCustomer.objects().filter(name__in=Subquery(inner)),
            MdCustomer.objects().filter(Q(name="a") | Q(name__in=Subquery(inner))),
            MdCustomer.objects().filter(name=Subquery(inner.order_by("-title").limit(1))),
        ):
            with pytest.raises(SurrealDbError) as info:
                await qs.exec()
            assert str(info.value) == expected

    async def test_two_levels_deep_the_statement_decides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The innermost subquery runs in the database of the outermost statement, the one the block is sent to."""
        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", staticmethod(_forbidden_client))
        deep = MdCustomer.objects().filter(id__in=MdNote.objects().filter(id__in=MdEvent.objects()))
        with pytest.raises(SurrealDbError) as info:
            await deep.exec()
        assert str(info.value) == _x2("MdEvent", "md_a", "MdCustomer", "default")
        with pytest.raises(SurrealDbError) as info:
            deep.live()
        assert str(info.value) == _x2("MdEvent", "md_a", "MdCustomer", "default")
        reverse = MdEvent.objects().filter(id__in=MdEvent.objects().filter(id__in=MdNote.objects()))
        with pytest.raises(SurrealDbError) as info:
            await reverse.exec()
        assert str(info.value) == _x2("MdNote", "default", "MdEvent", "md_a")

    async def test_the_links_of_a_subquery_are_checked_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """X4 for a link the inner queryset follows: its Q is translated while the statement compiles."""
        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", staticmethod(_forbidden_client))
        expected = _x4("MdPost", "author", "MdAuthor", "md_a", "default")
        for inner in (
            MdPost.objects().filter(**{"author.name": "x"}),
            MdPost.objects().filter(Q(**{"author.name": "x"})),
            MdPost.objects().select("author.name"),
        ):
            with pytest.raises(SurrealDbError) as info:
                await MdNote.objects().filter(id__in=inner).exec()
            assert str(info.value) == expected

    def test_same_connection_subqueries_compile_as_before(self) -> None:
        """The v0.28.0 text, byte for byte (statement, variables and live query)."""
        qs = MdNote.objects().filter(id__in=MdCustomer.objects().filter(name="x").select("id"), text="t")
        qs = qs.filter(Q(text__in=Subquery(MdCustomer.objects().select("name").order_by("-name").limit(1))))
        assert qs._compile_query(subqueries=qs._subquery_compiler()) == (
            "{ LET $_sq0 = (SELECT VALUE id FROM md_customer WHERE name = $_f0); LET $_sq1 = (SELECT name AS _sqv, "
            "name FROM md_customer ORDER BY name DESC LIMIT 1)._sqv; (SELECT * FROM md_note WHERE id IN $_sq0 AND "
            "text = $_f1 AND text IN $_sq1) };",
            {"_f0": "x", "_f1": "t"},
        )
        assert qs._compile_live(False) == (
            "LIVE SELECT * FROM md_note WHERE id IN (SELECT VALUE id FROM md_customer WHERE name = 'x') AND text = 't' "
            "AND text IN (SELECT name AS _sqv, name FROM md_customer ORDER BY name DESC LIMIT 1)._sqv;"
        )

    @pytest.mark.usefixtures("unset_default")
    async def test_inside_using_the_same_subquery_runs(self) -> None:
        async with _mem_default_and_md_a():
            async with SurrealDBConnectionManager.using("md_a"):
                await MdEvent(id="e1", title="alice").save()
                await MdCustomer(id="c1", name="alice").save()
                await MdCustomer(id="c2", name="bob").save()
                qs = MdCustomer.objects().filter(name__in=MdEvent.objects().select("title"))
                assert [customer.id for customer in await qs.exec()] == ["c1"]
                assert await qs.count() == 1
            with pytest.raises(SurrealDbError, match="cannot be read in a subquery of"):
                await MdCustomer.objects().filter(name__in=MdEvent.objects().select("title")).exec()
            assert await _rows_of(await SurrealDBConnectionManager.get_client(), "md_customer") == []


#: ``(id, call, operation, b, b_name)``: a relation helper on ``MdCustomer(id="c1")`` (the default connection) and the
#: model X3 names against it — the target's class first, then the edge class, then ``model_class``.
_X3_CASES: list[tuple[str, Callable[[MdCustomer], Any], str, str, str]] = [
    ("relate-str-edge", lambda c: c.relate("md_likes", MdEvent(id="e1")), "relate", "MdEvent", "md_a"),
    ("relate-edge-here-target-there", lambda c: c.relate(MdRates, MdEvent(id="e1")), "relate", "MdEvent", "md_a"),
    ("relate-edge-there-target-there", lambda c: c.relate(MdLikes, MdEvent(id="e1")), "relate", "MdEvent", "md_a"),
    ("relate-edge-there", lambda c: c.relate(MdLikes, MdCustomer(id="c2")), "relate", "MdLikes", "md_a"),
    ("relate-reverse", lambda c: c.relate("md_likes", MdEvent(id="e1"), True), "relate", "MdEvent", "md_a"),
    ("remove_relation-str-edge", lambda c: c.remove_relation("md_likes", MdEvent(id="e1")), "remove_relation", "MdEvent", "md_a"),
    ("remove_relation-edge-there", lambda c: c.remove_relation(MdLikes, MdCustomer(id="c2")), "remove_relation", "MdLikes", "md_a"),
    ("remove_all_relations", lambda c: c.remove_all_relations(MdLikes), "remove_all_relations", "MdLikes", "md_a"),
    ("remove_all_relations-list", lambda c: c.remove_all_relations(["md_follows", MdRates, MdLikes], "both"), "remove_all_relations", "MdLikes", "md_a"),
    ("get_related-edge-there", lambda c: c.get_related(MdLikes), "get_related", "MdLikes", "md_a"),
    ("get_related-model_class", lambda c: c.get_related("md_likes", model_class=MdEvent), "get_related", "MdEvent", "md_a"),
    ("get_related-edge-first", lambda c: c.get_related(MdLikes, "in", MdEvent), "get_related", "MdLikes", "md_a"),
    ("get_related-edge-here", lambda c: c.get_related(MdRates, model_class=MdEvent), "get_related", "MdEvent", "md_a"),
]  # fmt: skip


class TestRelationGuard:
    """AC-17, X3 (no server): ``relate()``, ``remove_relation()``, ``remove_all_relations()`` and ``get_related()``
    refuse records, edge models and ``model_class`` of two connections before any request and any signal; a
    ``RecordID`` or ``"table:id"`` target names no model and is not checked."""

    @pytest.mark.parametrize(
        ("call", "operation", "b", "b_name"), [case[1:] for case in _X3_CASES], ids=[case[0] for case in _X3_CASES]
    )
    async def test_two_connections_are_refused(
        self, call: Callable[[MdCustomer], Any], operation: str, b: str, b_name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", staticmethod(_forbidden_client))
        with _signal_spy(MdCustomer, MdEvent, MdLikes, MdRates) as fired, pytest.raises(SurrealDbError) as info:
            await call(MdCustomer(id="c1", name="c"))
        assert str(info.value) == _x3(f"MdCustomer.{operation}()", "MdCustomer", "default", b, b_name)
        assert fired == []

    async def test_a_source_of_md_a_is_named_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", staticmethod(_forbidden_client))
        with pytest.raises(SurrealDbError) as info:
            await MdEvent(id="e1").relate(MdLikes, MdCustomer(id="c1"))
        assert str(info.value) == _x3("MdEvent.relate()", "MdEvent", "md_a", "MdCustomer", "default")
        with pytest.raises(SurrealDbError) as info:
            await MdEvent(id="e1").relate(MdRates, MdEvent(id="e2"))
        assert str(info.value) == _x3("MdEvent.relate()", "MdEvent", "md_a", "MdRates", "default")

    async def test_a_record_id_or_a_string_target_is_not_checked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The call goes on to its request (the substitute's sentinel), even when the id is of a table of ``md_a``."""
        customer = MdCustomer(id="c1", name="c")
        calls: list[Callable[[], Any]] = [
            lambda: customer.relate("md_likes", RecordID("md_event", "e1")),
            lambda: customer.relate("md_likes", "md_event:e1"),
            lambda: customer.relate(MdRates, RecordID("md_event", "e1")),
            lambda: customer.remove_relation("md_likes", "md_event:e1"),
            lambda: customer.remove_relation(MdRates, RecordID("md_event", "e1")),
            lambda: customer.remove_relation("md_likes", "e1"),
            lambda: customer.remove_all_relations(["md_likes", MdRates], "both"),
            lambda: customer.get_related("md_likes", model_class=MdCustomer),
            lambda: customer.get_related(MdRates, "both"),
        ]
        with _substitute(monkeypatch):
            for call in calls:
                with pytest.raises(_Sentinel):
                    await call()

    @pytest.mark.usefixtures("unset_default")
    async def test_inside_using_one_connection_relates(self) -> None:
        async with _mem_default_and_md_a(), SurrealDBConnectionManager.using("md_a"):
            customer = await MdCustomer(id="c1", name="c").save()
            await MdEvent(id="e1", title="e").save()
            edge = await customer.relate(MdLikes, MdEvent(id="e1"), weight=3)
            assert edge.weight == 3
            await customer.relate(MdRates, MdEvent(id="e1"))
            related = await customer.get_related(MdLikes, model_class=MdEvent)
            assert [event.id for event in related] == ["e1"]
            await customer.remove_relation(MdLikes, MdEvent(id="e1"))
            await customer.remove_all_relations([MdRates, "md_likes"])
            assert await customer.get_related(MdLikes) == []
            md_a = await SurrealDBConnectionManager.get_client("md_a")
            assert await _rows_of(md_a, "md_likes") == []
            assert await _rows_of(md_a, "md_rates") == []


#: Reads, writes and streams of ``MdPost`` (the default connection) that follow its ``author`` link to ``MdAuthor``
#: (``md_a``): each raises X4 before any request.
_X4_CALLS: dict[str, Callable[[], Any]] = {
    "fetch": lambda: MdPost.objects().fetch("author").exec(),
    "fetch-path": lambda: MdPost.objects().fetch("author.name").exec(),
    "filter": lambda: MdPost.objects().filter(**{"author.name": "x"}).exec(),
    "filter-lookup": lambda: MdPost.objects().filter(**{"author.name__contains": "x"}).count(),
    "Q": lambda: MdPost.objects().filter(Q(title="t") | Q(**{"author.name": "x"})).exec(),
    "select": lambda: MdPost.objects().select("author.*").exec(),
    "order_by": lambda: MdPost.objects().order_by("-author.name").exec(),
    "values": lambda: MdPost.objects().values("author.name").annotate(n=Count()).exec(),
    "annotate": lambda: MdPost.objects().values("title").annotate(n=Count("author.name")).exec(),
    "sum": lambda: MdPost.objects().sum("author.age"),
    "first": lambda: MdPost.objects().fetch("author").first(),
    "get": lambda: MdPost.objects().filter(**{"author.name": "x"}).get(),
    "cache": lambda: MdPost.objects().fetch("author").cache().exec(),
    "bulk_delete": lambda: MdPost.objects().filter(**{"author.name": "x"}).bulk_delete(),
    "get_or_create": lambda: MdPost.objects().get_or_create(**{"author.name__contains": "x"}),
    "live-fetch": lambda: MdPost.objects().fetch("author").live(),
    "live-filter": lambda: MdPost.objects().filter(**{"author.name": "x"}).live(),
}


class TestLinkGuard:
    """AC-18, X4 (no server but the ``using()`` case): a queryset that follows a link through a field typed with a
    model of another connection is refused before any request; a field typed ``RecordID`` (N8), a nested object
    and a link to a model of the same connection are not; inside ``using()`` every model is on one connection.
    ``routing.linked_models()`` walks the annotation, and caches it only once the model is complete."""

    @pytest.mark.parametrize("call", list(_X4_CALLS), ids=list(_X4_CALLS))
    async def test_a_link_to_another_connection_is_refused(self, call: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", staticmethod(_forbidden_client))
        with _signal_spy(MdPost) as fired, pytest.raises(SurrealDbError) as info:
            await _run(_X4_CALLS[call])
        assert str(info.value) == _x4("MdPost", "author", "MdAuthor", "md_a", "default")
        assert fired == []

    async def test_a_list_of_links_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", staticmethod(_forbidden_client))
        for call in (lambda: MdPost.objects().fetch("tags").exec(), lambda: MdPost.objects().fetch("title", "tags").live()):
            with pytest.raises(SurrealDbError) as info:
                await _run(call)
            assert str(info.value) == _x4("MdPost", "tags", "MdTag", "md_a", "default")

    async def test_untyped_links_nested_objects_and_same_connection_links_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """They reach their request (the substitute's sentinel)."""
        calls: list[Callable[[], Any]] = [
            lambda: MdPost.objects().fetch("editor").exec(),
            lambda: MdPost.objects().filter(**{"editor.name": "x"}).exec(),
            lambda: MdPost.objects().filter(**{"address.city": "x"}).order_by("address.city").exec(),
            lambda: MdPost.objects().select("address.city").exec(),
            lambda: MdPost.objects().fetch("reviewer").filter(**{"reviewer.name": "x"}).exec(),
            lambda: MdPost.objects().filter(**{"author": RecordID("md_author", "a1")}).exec(),
            lambda: MdPost.objects().select("author").exec(),
            lambda: MdNote.objects().filter(**{"text.x": "x"}).exec(),
        ]
        with _substitute(monkeypatch):
            for call in calls:
                with pytest.raises(_Sentinel):
                    await call()

    @pytest.mark.usefixtures("unset_default")
    async def test_inside_using_the_link_is_followed(self) -> None:
        async with _mem_default_and_md_a(), SurrealDBConnectionManager.using("md_a"):
            await MdAuthor(id="a1", name="Ann").save()
            await MdTag(id="t1", label="news").save()
            await MdPost(id="p1", title="hello", author=RecordID("md_author", "a1"), tags=[RecordID("md_tag", "t1")]).save()
            [post] = await MdPost.objects().fetch("author", "tags").filter(**{"author.name": "Ann"}).exec()
            assert isinstance(post.author, MdAuthor)
            assert post.author.name == "Ann"
            assert [tag.label for tag in post.tags or []] == ["news"]
            assert await MdPost.objects().filter(**{"author.name": "Bob"}).count() == 0

    def test_linked_models_walks_the_annotation(self) -> None:
        cases = {
            "optional": (MdAuthor,),
            "union": (MdAuthor, MdTag),
            "annotated": (MdAuthor,),
            "listed": (MdTag,),
            "nested": (MdTag,),
            "mapped": (MdAuthor,),
            "sequence": (MdTag,),
            "tupled": (MdTag,),
            "twice": (MdAuthor,),
            "by": (MdAuthor,),  # a column: the field stored under it
            "plain": (),
            "text": (),
            "address": (),
            "missing": (),
        }
        for column, expected in cases.items():
            model = MdPost if column == "address" else MdShapes
            assert routing.linked_models(model, column) == expected, column
        assert routing.linked_models(MdPost, "author") == (MdAuthor,)
        assert routing.linked_models(MdPost, "reviewer") == (MdCustomer,)

    async def test_a_model_rebuilt_later_turns_the_guard_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """While a forward reference is unresolved nothing is cached, so ``model_rebuild()`` turns X4 on."""
        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", staticmethod(_forbidden_client))

        class MdLate(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="md_late")

            id: str | None = None
            later: "MdLater | RecordID | None" = None

        assert MdLate.__pydantic_complete__ is False
        assert routing.linked_models(MdLate, "later") == ()
        assert routing.linked_models(MdLate, "later") == ()

        class MdLater(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="md_later", connection="md_a")

            id: str | None = None

        MdLate.model_rebuild()
        assert routing.linked_models(MdLate, "later") == (MdLater,)
        with pytest.raises(SurrealDbError) as info:
            await MdLate.objects().fetch("later").exec()
        assert str(info.value) == _x4("MdLate", "later", "MdLater", "md_a", "default")

    async def test_the_linked_models_connection_is_read_at_each_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cached: the linked classes; never their connection (a ``using()`` block, or a model routed elsewhere)."""
        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", staticmethod(_forbidden_client))
        with pytest.raises(SurrealDbError, match="links to"):
            await MdPost.objects().fetch("author").exec()
        monkeypatch.setitem(MdAuthor.model_config, "connection", None)
        with _substitute(monkeypatch), pytest.raises(_Sentinel):
            await MdPost.objects().fetch("author").exec()
        monkeypatch.undo()
        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", staticmethod(_forbidden_client))
        with pytest.raises(SurrealDbError, match="would read the wrong database"):
            await MdPost.objects().fetch("author").exec()


# --- the guards on the line under test (both lines) -----------------------------------------------------------


@pytest.mark.usefixtures("unset_default")
class TestSubqueryGuardE2E:
    """AC-16 (both lines, ws): X2 sends nothing; the same subquery inside ``using("md_a")`` and a subquery between
    two models of ``md_a`` read ``md_a``."""

    async def test_x2_sends_nothing_and_one_connection_reads(self) -> None:
        async with _routed_databases("ws"):
            await MdEvent(id="e1", title="alice").save()
            await MdAuthor(id="a1", name="alice").save()
            await MdAuthor(id="a2", name="bob").save()
            with pytest.raises(SurrealDbError, match="cannot be read in a subquery of"):
                await MdCustomer.objects().filter(name__in=MdEvent.objects().select("title")).exec()
            same = MdAuthor.objects().filter(name__in=MdEvent.objects().select("title"))
            assert [author.id for author in await same.exec()] == ["a1"]
            async with SurrealDBConnectionManager.using("md_a"):
                await MdCustomer(id="c1", name="alice").save()
                qs = MdCustomer.objects().filter(name__in=MdEvent.objects().select("title"))
                assert [customer.id for customer in await qs.exec()] == ["c1"]
            assert [row["name"] for row in await _root_rows("md_a", "md_customer")] == ["alice"]
            assert await _root_rows("md_b", "md_customer") == []
            assert await _root_rows("md_b", "md_event") == []


@pytest.mark.usefixtures("unset_default")
class TestRelationGuardE2E:
    """AC-17 (both lines, ws): X3 writes no edge in either database; records and edge models of ``md_a`` relate in
    ``md_a``."""

    async def test_x3_writes_nothing_and_one_connection_relates(self) -> None:
        async with _routed_databases("ws"):
            customer = await MdCustomer(id="c1", name="c").save()
            await MdEvent(id="e1", title="one").save()
            await MdEvent(id="e2", title="two").save()
            with pytest.raises(SurrealDbError, match="cannot relate records of two connections"):
                await customer.relate("md_likes", MdEvent(id="e1"))
            with pytest.raises(SurrealDbError, match="cannot relate records of two connections"):
                await customer.relate(MdLikes, MdCustomer(id="c1"))
            assert await _root_rows("md_a", "md_likes") == []
            assert await _root_rows("md_b", "md_likes") == []
            await MdEvent(id="e1").relate(MdLikes, MdEvent(id="e2"), weight=2)
            related = await MdEvent(id="e1").get_related(MdLikes, model_class=MdEvent)
            assert [event.title for event in related] == ["two"]
            assert [row["weight"] for row in await _root_rows("md_a", "md_likes")] == [2]
            assert await _root_rows("md_b", "md_likes") == []


@pytest.mark.usefixtures("unset_default")
class TestLinkGuardE2E:
    """AC-18 (both lines, ws): X4 sends nothing; inside ``using("md_a")`` the post and its author are in one
    database and the link is followed."""

    async def test_x4_sends_nothing_and_one_connection_follows(self) -> None:
        async with _routed_databases("ws"):
            await MdAuthor(id="a1", name="Ann").save()
            await MdPost(id="p1", title="elsewhere", author=RecordID("md_author", "a1")).save()
            with pytest.raises(SurrealDbError, match="would read the wrong database"):
                await MdPost.objects().fetch("author").exec()
            with pytest.raises(SurrealDbError, match="links to"):
                await MdPost.objects().filter(**{"author.name": "Ann"}).exec()
            [post] = await MdPost.objects().exec()
            assert post.author == RecordID("md_author", "a1")
            async with SurrealDBConnectionManager.using("md_a"):
                await MdPost(id="p2", title="here", author=RecordID("md_author", "a1")).save()
                [here] = await MdPost.objects().fetch("author").filter(**{"author.name": "Ann"}).exec()
                assert isinstance(here.author, MdAuthor)
                assert (here.title, here.author.name) == ("here", "Ann")
            assert [row["title"] for row in await _root_rows("md_a", "md_post")] == ["here"]
            assert [row["title"] for row in await _root_rows("md_b", "md_post")] == ["elsewhere"]


# === Embedded engines per connection (spec §5.6, §6.7, §6.8) =================================================


def _x5(name: str, url: str, holder: str) -> str:
    return (
        f"Connection {name!r} cannot open {url!r}: connection {holder!r} already has that store open in this process, "
        f"and two in-process engines on one store miss each other's writes. Use one connection for the store, or close "
        f"{holder!r} first."
    )


def _r8(name: str, active: str) -> str:
    return (
        f"embedded_database(connection={name!r}) cannot isolate {name!r} inside using({active!r}): every model of the "
        f"block would use {active!r} and reach its real database. Leave connection=None (it isolates {active!r}), or "
        "open the block outside using()."
    )


def _slot(name: str) -> Any:
    """The client in the embedded slot of connection *name* (white box)."""
    return vars(connection_manager._REGISTRY[name])[_P + "embedded_client"]


def _claim(url: str) -> str | None:
    """The name of the connection holding the on-disk store of *url*, if any."""
    holder = _holder(url)
    return None if holder is None else holder._connection_name


def _holder(url: str) -> type[SurrealDBConnectionManager] | None:
    """The connection class holding the on-disk store of *url*, if any: claims belong to classes, not names."""
    key = embedded.store_key(url)
    assert key is not None
    return connection_manager._STORE_CLAIMS.get(key)


class TestStoreKey:
    """Spec §6.7: the canonical path of the on-disk store an embedded URL opens — one key for every spelling."""

    def test_in_memory_and_server_urls_hold_no_store(self) -> None:
        for url in (None, "", "mem://", "memory://", _UNREACHABLE, "http://localhost:8000", "rocksdb://x"):
            assert embedded.store_key(url) is None, url

    def test_whatever_follows_an_in_memory_scheme_holds_no_store(self, tmp_path: Path) -> None:
        """Read from the SDK's parser: two clients on ``mem://abc`` are two databases, and nothing is written there."""
        for url in ("mem://abc", "memory://abc", "mem:///abs/path", "mem://kv", f"mem://{tmp_path}/x"):
            assert embedded.store_key(url) is None, url
        assert embedded.store_key("surrealkv://kv") is not None

    def test_every_spelling_of_one_path_is_one_store(self, tmp_path: Path) -> None:
        path = tmp_path / "kv"
        expected = os.path.realpath(path)
        spellings = (
            f"surrealkv://{path}",
            f"surrealkv://{path}/",
            f"surrealkv+versioned://{path}",
            f"file://{path}",
            f"surrealkv://{path}?versioned=true",
            f"file://{path}/#fragment",
        )
        for url in spellings:
            assert embedded.store_key(url) == expected, url
        assert embedded.store_key(f"surrealkv://{tmp_path / 'other'}") == os.path.realpath(tmp_path / "other")

    def test_relative_paths_and_symbolic_links_resolve(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "kv").mkdir()
        (tmp_path / "link").symlink_to(tmp_path / "kv")
        monkeypatch.chdir(tmp_path)
        expected = os.path.realpath(tmp_path / "kv")
        assert embedded.store_key("surrealkv://kv") == expected
        assert embedded.store_key(f"surrealkv://{tmp_path / 'link'}") == expected


class TestStoreClaims:
    """AC-19, X5 (no server): an on-disk store is open under one connection name at a time in the process; two names
    on ``mem://`` are two databases."""

    async def test_a_second_name_on_one_store_raises_x5_until_the_first_closes(self, tmp_path: Path) -> None:
        path = tmp_path / "kv"
        url = f"surrealkv://{path}"
        SurrealDBConnectionManager.add_connection("md_a", url=url, namespace="t", database="t")
        client = await SurrealDBConnectionManager.get_client("md_a")
        await client.query("CREATE md_item:1 SET v = 1;", {})
        del client  # the closed-client rule of v0.25.0: the path is reopened below
        assert _claim(url) == "md_a"
        for spelling in (url, f"{url}/", f"file://{path}", f"surrealkv+versioned://{path}"):
            SurrealDBConnectionManager.add_connection("md_b", url=spelling, namespace="t", database="t")
            with pytest.raises(SurrealDbConnectionError) as info:
                await SurrealDBConnectionManager.get_client("md_b")
            assert str(info.value) == _x5("md_b", spelling, "md_a")
            assert info.value.retryable is False  # type: ignore[attr-defined]
            assert _slot("md_b") is None
            assert _claim(url) == "md_a"
        await SurrealDBConnectionManager.close_connection("md_a")
        assert _claim(url) is None
        md_b = await SurrealDBConnectionManager.get_client("md_b")
        assert _claim(url) == "md_b"
        assert _values(await _rows_of(md_b, "md_item")) == [1]  # md_a's committed row, on the one store
        del md_b
        with pytest.raises(SurrealDbConnectionError, match="already has that store open in this process"):
            await SurrealDBConnectionManager.get_client("md_a")

    async def test_remove_connection_releases_the_store(self, tmp_path: Path) -> None:
        url = f"surrealkv://{tmp_path / 'kv'}"
        SurrealDBConnectionManager.add_connection("md_a", url=url, namespace="t", database="t")
        SurrealDBConnectionManager.add_connection("md_b", url=url, namespace="t", database="t")
        await SurrealDBConnectionManager.get_client("md_a")
        with pytest.raises(SurrealDbConnectionError, match="already has that store open in this process"):
            await SurrealDBConnectionManager.get_client("md_b")
        await SurrealDBConnectionManager.remove_connection("md_a")
        assert _claim(url) is None
        assert await SurrealDBConnectionManager.get_client("md_b") is _slot("md_b")
        assert _claim(url) == "md_b"

    async def test_a_store_that_cannot_open_is_not_claimed(self) -> None:
        url = "surrealkv:///proc/forbidden/kv"
        for name in ("md_a", "md_b"):
            SurrealDBConnectionManager.add_connection(name, url=url, namespace="t", database="t")
            with pytest.raises(SurrealDbConnectionError, match="Can't open the embedded database"):
                await SurrealDBConnectionManager.get_client(name)
            assert _claim(url) is None

    async def test_the_same_name_reopens_its_store_in_an_embedded_database_block(self, tmp_path: Path) -> None:
        """v0.25.0 behaviour on the connection's own store; the claim stays the connection's after the block."""
        url = f"surrealkv://{tmp_path / 'kv'}"
        SurrealDBConnectionManager.add_connection("md_a", url=url, namespace="t", database="t")
        outer = await SurrealDBConnectionManager.get_client("md_a")
        async with embedded_database(url, connection="md_a") as block:
            assert block is not outer
            assert await SurrealDBConnectionManager.get_client("md_a") is block
            assert _claim(url) == "md_a"
            del block
        assert await SurrealDBConnectionManager.get_client("md_a") is outer
        assert _claim(url) == "md_a"
        SurrealDBConnectionManager.add_connection("md_b", url=url, namespace="t", database="t")
        with pytest.raises(SurrealDbConnectionError, match="already has that store open in this process"):
            await SurrealDBConnectionManager.get_client("md_b")

    async def test_a_close_inside_such_a_block_keeps_the_outer_engines_claim(self, tmp_path: Path) -> None:
        """The block's close releases the store while the outer engine, detached, still has it open: the claim is
        the outer engine's again when it comes back."""
        url = f"surrealkv://{tmp_path / 'kv'}"
        SurrealDBConnectionManager.add_connection("md_a", url=url, namespace="t", database="t")
        outer = await SurrealDBConnectionManager.get_client("md_a")
        async with embedded_database(url, connection="md_a") as block:
            del block
            await SurrealDBConnectionManager.close_connection("md_a")
        assert await SurrealDBConnectionManager.get_client("md_a") is outer
        assert _claim(url) == "md_a"
        SurrealDBConnectionManager.add_connection("md_b", url=url, namespace="t", database="t")
        with pytest.raises(SurrealDbConnectionError, match="already has that store open in this process"):
            await SurrealDBConnectionManager.get_client("md_b")

    async def test_another_store_in_a_block_is_released_after_it(self, tmp_path: Path) -> None:
        url = f"surrealkv://{tmp_path / 'kv'}"
        SurrealDBConnectionManager.add_connection("md_a", url="mem://", namespace="t", database="t")
        async with embedded_database(url, connection="md_a"):
            assert _claim(url) == "md_a"
        assert _claim(url) is None

    async def test_two_names_on_mem_are_two_databases(self) -> None:
        claims = dict(connection_manager._STORE_CLAIMS)
        SurrealDBConnectionManager.add_connection("md_a", url="mem://", namespace="t", database="t")
        SurrealDBConnectionManager.add_connection("md_b", url="mem://", namespace="t", database="t")
        a_client = await SurrealDBConnectionManager.get_client("md_a")
        b_client = await SurrealDBConnectionManager.get_client("md_b")
        assert a_client is not b_client
        await a_client.query("CREATE md_item:1 SET v = 1;", {})
        assert await _rows_of(b_client, "md_item") == []
        assert dict(connection_manager._STORE_CLAIMS) == claims  # in memory: nothing to claim

    async def test_two_names_on_one_in_memory_path_are_two_databases(self) -> None:
        """Whatever follows ``mem://`` names no store: no X5, and each name is a database of its own."""
        claims = dict(connection_manager._STORE_CLAIMS)
        SurrealDBConnectionManager.add_connection("md_a", url="mem://abc", namespace="t", database="t")
        SurrealDBConnectionManager.add_connection("md_b", url="mem://abc", namespace="t", database="t")
        a_client = await SurrealDBConnectionManager.get_client("md_a")
        b_client = await SurrealDBConnectionManager.get_client("md_b")
        assert a_client is not b_client
        await a_client.query("CREATE md_item:1 SET v = 1;", {})
        await b_client.query("CREATE md_item:2 SET v = 2;", {})
        assert _values(await _rows_of(a_client, "md_item")) == [1]
        assert _values(await _rows_of(b_client, "md_item")) == [2]
        assert dict(connection_manager._STORE_CLAIMS) == claims

    async def test_an_in_memory_url_and_a_store_on_the_same_text_open_side_by_side(self, tmp_path: Path) -> None:
        store = f"surrealkv://{tmp_path}/x"
        SurrealDBConnectionManager.add_connection("md_a", url=f"mem://{tmp_path}/x", namespace="t", database="t")
        SurrealDBConnectionManager.add_connection("md_b", url=store, namespace="t", database="t")
        a_client = await SurrealDBConnectionManager.get_client("md_a")
        b_client = await SurrealDBConnectionManager.get_client("md_b")
        assert a_client is not b_client
        assert _claim(store) == "md_b"
        await a_client.query("CREATE md_item:1 SET v = 1;", {})
        assert await _rows_of(b_client, "md_item") == []

    def test_claims_belong_to_connection_classes_not_names(self, tmp_path: Path) -> None:
        """Two classes of one name — a name removed and registered again — are two connections: the second is
        refused (X5), and the second's release or reclaim leaves the first's claim in place."""
        url = f"surrealkv://{tmp_path / 'kv'}"
        first = connection_manager._new_connection_class("md_a")
        second = connection_manager._new_connection_class("md_a")
        key = connection_manager._claim_store(url, first)
        assert key == embedded.store_key(url)
        try:
            assert connection_manager._claim_store(url, first) is None  # its own store, again: nothing new to claim
            with pytest.raises(SurrealDbConnectionError) as info:
                connection_manager._claim_store(url, second)
            assert str(info.value) == _x5("md_a", url, "md_a")
            assert info.value.retryable is False  # type: ignore[attr-defined]
            connection_manager._release_store(key, second)
            assert connection_manager._STORE_CLAIMS[key] is first
            connection_manager._reclaim_store(key, second)
            assert connection_manager._STORE_CLAIMS[key] is first
        finally:
            connection_manager._release_store(key, first)
        assert key not in connection_manager._STORE_CLAIMS


@fixture
class MdFixtures(SurrealFixture):
    """A record of the default connection and one of ``md_a``."""

    customer = MdCustomer(id="c1", name="Ada")
    event = MdEvent(id="e1", title="Launch")


def _untouched(*names: str) -> list[Any]:
    """What a refused ``embedded_database()`` must leave as it was: the registry, and each connection's settings,
    stored session, embedded slot and per-loop clients."""
    state: list[Any] = [list(connection_manager._REGISTRY)]
    for name in names:
        connection = connection_manager._REGISTRY[name]
        state.append(
            (
                name,
                connection._settings(),
                connection.get_session_token(),
                vars(connection)[_P + "embedded_client"],
                dict(vars(connection)[_P + "clients"]),
            )
        )
    return state


@pytest.mark.usefixtures("unset_default")
class TestEmbeddedDatabaseConnection:
    """AC-20, spec §5.6, §6.8 (no server): ``embedded_database(connection=)`` isolates one connection, the current
    one by default; R8 and R3 before anything changes; ``SurrealFixture`` across two connections."""

    async def test_an_explicit_connection_is_isolated_alone(self) -> None:
        async with _mem_default_and_md_a():
            default = await SurrealDBConnectionManager.get_client()
            await MdCustomer(id="c1", name="default").save()
            md_a = await SurrealDBConnectionManager.get_client("md_a")
            await md_a.query("CREATE md_customer:a SET name = 'md_a';", {})
            default_connection_class()._SurrealDBConnectionManager__session_token = "kept"  # type: ignore[attr-defined]
            config = SurrealDBConnectionManager.get_config("md_a")
            try:
                async with embedded_database(connection="md_a") as client:
                    assert client is not md_a
                    assert await SurrealDBConnectionManager.get_client("md_a") is client
                    assert SurrealDBConnectionManager.get_config("md_a") == ConnectionConfig(
                        "mem://", None, None, "test", "test"
                    )
                    async with SurrealDBConnectionManager.using("md_a"):
                        assert await MdCustomer.objects().count() == 0
                        await MdCustomer(id="block", name="block").save()
                    # The default connection is not touched.
                    assert await SurrealDBConnectionManager.get_client() is default
                    assert SurrealDBConnectionManager.get_session_token() == "kept"
                    assert [customer.id for customer in await MdCustomer.objects().exec()] == ["c1"]
                assert await SurrealDBConnectionManager.get_client("md_a") is md_a
                assert SurrealDBConnectionManager.get_config("md_a") == config
                assert [row["name"] for row in await _rows_of(md_a, "md_customer")] == ["md_a"]
                assert await SurrealDBConnectionManager.get_client() is default
                assert SurrealDBConnectionManager.get_session_token() == "kept"
            finally:
                SurrealDBConnectionManager.clear_session()

    async def test_inside_using_the_block_isolates_the_current_connection(self) -> None:
        async with _mem_default_and_md_a():
            default = await SurrealDBConnectionManager.get_client()
            md_a = await SurrealDBConnectionManager.get_client("md_a")
            async with SurrealDBConnectionManager.using("md_a"):
                async with embedded_database() as client:
                    assert client is not md_a
                    assert await SurrealDBConnectionManager.get_client() is client
                    await MdCustomer(id="in-block", name="isolated").save()  # default-routed: the block's connection
                    assert await MdCustomer.objects().count() == 1
                assert await SurrealDBConnectionManager.get_client() is md_a
            assert await _rows_of(md_a, "md_customer") == []
            assert await _rows_of(default, "md_customer") == []
            assert await SurrealDBConnectionManager.get_client() is default

    async def test_outside_any_block_it_isolates_the_default(self) -> None:
        async with _mem_default_and_md_a():
            default = await SurrealDBConnectionManager.get_client()
            md_a = await SurrealDBConnectionManager.get_client("md_a")
            async with embedded_database() as client:
                assert client is not default
                assert await SurrealDBConnectionManager.get_client() is client
                assert await SurrealDBConnectionManager.get_client("md_a") is md_a
                await MdCustomer(id="in-block").save()
            assert await SurrealDBConnectionManager.get_client() is default
            assert await _rows_of(default, "md_customer") == []

    async def test_r8_before_anything_changes(self) -> None:
        async with _mem_default_and_md_a():
            await SurrealDBConnectionManager.get_client()
            await SurrealDBConnectionManager.get_client("md_a")
            SurrealDBConnectionManager.add_connection("md_b", url="mem://", namespace="ns", database="md_b")
            before = _untouched("default", "md_a", "md_b")
            async with SurrealDBConnectionManager.using("md_a"):
                for name in ("default", "md_b", "md_new"):
                    with pytest.raises(ValueError) as info:
                        async with embedded_database(connection=name):
                            pytest.fail("the block must not run")  # pragma: no cover
                    assert str(info.value) == _r8(name, "md_a")
                    assert _untouched("default", "md_a", "md_b") == before
                async with embedded_database(connection="md_a") as client:
                    assert await SurrealDBConnectionManager.get_client() is client
            assert _untouched("default", "md_a", "md_b") == before

    async def test_r3_before_anything_changes(self) -> None:
        async with _mem_default_and_md_a():
            await SurrealDBConnectionManager.get_client()
            before = _untouched("default", "md_a")
            cases: list[tuple[Any, type[Exception], str]] = [
                (1, TypeError, "Connection name must be a str, not int."),
                ("", ValueError, "Connection name must not be empty."),
            ]
            for connection, error, text in cases:
                with pytest.raises(error) as info:
                    async with embedded_database(connection=connection):
                        pytest.fail("the block must not run")  # pragma: no cover
                assert str(info.value) == text
                assert _untouched("default", "md_a") == before

    async def test_an_unregistered_name_is_registered_for_the_block_only(self) -> None:
        assert "md_a" not in connection_manager._REGISTRY
        async with embedded_database(connection="md_a") as client:
            assert "md_a" in SurrealDBConnectionManager.list_connections()
            assert await SurrealDBConnectionManager.get_client("md_a") is client
            await MdEvent(id="e1", title="routed").save()  # routed to md_a: the block's engine
            assert [event.title for event in await MdEvent.objects().exec()] == ["routed"]
            async with SurrealDBConnectionManager.using("md_a"):
                assert await MdEvent.objects().count() == 1
        assert "md_a" not in connection_manager._REGISTRY
        assert "md_a" not in SurrealDBConnectionManager.list_connections()
        with pytest.raises(ValueError) as info:
            await MdEvent.objects().exec()
        assert str(info.value) == _r2("MdEvent", "md_a")

    async def test_surreal_fixture_saves_each_record_through_its_connection(self) -> None:
        async with _mem_default_and_md_a():
            default = await SurrealDBConnectionManager.get_client()
            md_a = await SurrealDBConnectionManager.get_client("md_a")
            async with MdFixtures.load() as loaded:
                assert (loaded.customer.id, loaded.event.id) == ("c1", "e1")
                assert [row["name"] for row in await _rows_of(default, "md_customer")] == ["Ada"]
                assert _titles(await _rows_of(md_a, "md_event")) == ["Launch"]
                assert await _rows_of(default, "md_event") == []
                assert await _rows_of(md_a, "md_customer") == []
            assert await _rows_of(default, "md_customer") == []
            assert await _rows_of(md_a, "md_event") == []
            async with SurrealDBConnectionManager.using("md_a"), MdFixtures.load():
                assert [row["name"] for row in await _rows_of(md_a, "md_customer")] == ["Ada"]
                assert _titles(await _rows_of(md_a, "md_event")) == ["Launch"]
                assert await _rows_of(default, "md_customer") == []
                assert await _rows_of(default, "md_event") == []
            assert await _rows_of(md_a, "md_customer") == []
            assert await _rows_of(md_a, "md_event") == []


# === Server lines, the query cache and threads across connections (spec §4, §7.11, §9.3; AC-9, AC-10, AC-21-23) =


@dataclasses.dataclass(frozen=True)
class _Side:
    """One connection of a pair the per-connection decisions of spec §4 run on, side by side in one event loop."""

    name: str
    #: ``"ws"`` for a server connection over WebSocket, ``"mem"`` for the embedded engine.
    kind: str
    #: The major its server answers ``version()`` with; the embedded engine of the surrealdb SDK 2.x: 2 (spec M12).
    line: int
    #: Reads a table of its database from outside the ORM (a root client, or the engine's own client).
    rows: Callable[[str], Awaitable[list[Any]]]

    @property
    def interactive(self) -> bool:
        """Whether ``transaction()`` is interactive on it: WebSocket to SurrealDB 3.x (spec §4)."""
        return self.kind == "ws" and self.line >= 3


#: The tables of the decisions, defined beforehand in each connection's database: SurrealDB 3.x refuses to watch a
#: missing table, and ``md_event`` is the database's only change-feed table (SurrealDB 2.x applies ``SHOW CHANGES``'
#: ``LIMIT`` to the whole database's feed).
_DECISION_TABLES = (
    "DEFINE TABLE md_customer SCHEMALESS; DEFINE TABLE md_note SCHEMALESS; DEFINE TABLE md_post SCHEMALESS;"
    " DEFINE TABLE md_event SCHEMALESS CHANGEFEED 1h;"
)

#: An absolute ``as_of()`` moment: resolved without a request.
_MOMENT = datetime(2026, 1, 1, tzinfo=UTC)


def _record_requests(monkeypatch: pytest.MonkeyPatch, client: Any) -> list[str]:
    """The statement of each ``query()`` and the target of each ``select()`` *client* sends from now on.

    Patched on the client's class and keyed on ``id(client)``, as ``tests.test_time_travel._record_queries`` does; each
    wrapper keeps the signature it wraps (``functools.wraps``), so the wrappers of two clients of one class stack.
    """
    sent: list[str] = []
    cls = type(client)
    owner = id(client)

    def wrap(real: Any) -> Any:
        @functools.wraps(real)
        async def recording(self: Any, *args: Any, **kwargs: Any) -> Any:
            if id(self) == owner:
                sent.append(str(args[0] if args else next(iter(kwargs.values()), "")))
            return await real(self, *args, **kwargs)

        return recording

    for method in ("query", "select"):
        monkeypatch.setattr(cls, method, wrap(getattr(cls, method)))
    return sent


async def _line_of(name: str) -> int:
    """The major of the server connection *name* talks to (``changes()``' rule; the embedded engine answers 2)."""
    client = await SurrealDBConnectionManager.get_client(name)
    return time_travel.line_major(await client.version())


def _ws_url_at(port: str) -> str:
    """The WebSocket URL of the server on *port* of ``SURREALDB_HOST``."""
    return f"ws://{os.environ.get('SURREALDB_HOST', 'localhost')}:{port}/rpc"


async def _server_side(name: str, url: str, database: str) -> _Side:
    """Register *name* as root on *database* of namespace ``ns`` at the WebSocket *url*, the database removed and
    defined again with :data:`_DECISION_TABLES` first."""
    async with _root("db", url) as root:
        await root.query(f"REMOVE DATABASE IF EXISTS {database}; DEFINE DATABASE {database};", {})
    async with _root(database, url) as root:
        await root.query(_DECISION_TABLES, {})
    SurrealDBConnectionManager.add_connection(name, url=url, user="root", password="root", namespace="ns", database=database)

    async def rows(table: str) -> list[Any]:
        return await _root_rows(database, table, url)

    return _Side(name, "ws", await _line_of(name), rows)


async def _remove_server_database(url: str, database: str) -> None:
    async with _root("db", url) as root:
        await root.query(f"REMOVE DATABASE IF EXISTS {database};", {})


@contextlib.asynccontextmanager
async def _mixed_lines() -> AsyncIterator[list[_Side]]:
    """``md_3x`` on ``SURREALDB_PORT_3X`` and ``md_2x`` on ``SURREALDB_PORT_2X`` (same ``SURREALDB_HOST``), each on
    database ``md_mixed`` of namespace ``ns``, removed before and after; skips without both ports (spec §9.1, §9.3).
    ``SURREALDB_PORT`` is not read: the pair is the same in each line's run."""
    port_3x, port_2x = os.environ.get("SURREALDB_PORT_3X"), os.environ.get("SURREALDB_PORT_2X")
    if not (port_3x and port_2x):
        pytest.skip("needs SURREALDB_PORT_3X and SURREALDB_PORT_2X (both server lines)")
    urls = {"md_3x": _ws_url_at(port_3x), "md_2x": _ws_url_at(port_2x)}
    try:
        sides = [await _server_side(name, url, "md_mixed") for name, url in urls.items()]
        assert [side.line for side in sides] == [3, 2], "SURREALDB_PORT_3X and SURREALDB_PORT_2X name the two lines"
        yield sides
    finally:
        for name, url in urls.items():
            await SurrealDBConnectionManager.remove_connection(name)
            await _remove_server_database(url, "md_mixed")


@contextlib.asynccontextmanager
async def _beside_embedded() -> AsyncIterator[list[_Side]]:
    """``md_a`` over WebSocket to the line under test (database ``md_a``) and ``md_mem`` on ``mem://`` — a 2.x core —
    in one process: the always-on twin of :func:`_mixed_lines` (spec §9.3)."""
    url = _url("ws")
    try:
        server = await _server_side("md_a", url, "md_a")
        _register("mem", "md_mem", database="md_mem")
        engine = await SurrealDBConnectionManager.get_client("md_mem")
        await engine.query(_DECISION_TABLES, {})

        async def rows(table: str) -> list[Any]:
            return await _rows_of(engine, table)

        yield [server, _Side("md_mem", "mem", await _line_of("md_mem"), rows)]
    finally:
        for name in ("md_a", "md_mem"):
            await SurrealDBConnectionManager.remove_connection(name)
        await _remove_server_database(url, "md_a")


async def _decide_transactions(side: _Side) -> None:
    """Spec §4, transactions: ``transaction()`` inside ``using()`` takes the strategy of the block's server, and a read
    inside it sees the uncommitted write (interactive) or is refused (buffered)."""
    async with SurrealDBConnectionManager.using(side.name), SurrealDBConnectionManager.transaction() as tx:
        assert type(tx).__name__ == ("InteractiveTransaction" if side.interactive else "BufferedTransaction")
        await MdCustomer(id="t1", name=side.name).save(tx=tx)
        if side.interactive:
            assert [customer.name for customer in await MdCustomer.objects(tx=tx).exec()] == [side.name]
        else:
            with pytest.raises(SurrealDbError, match="Reads inside a transaction require"):
                await MdCustomer.objects(tx=tx).exec()
        assert await side.rows("md_customer") == []  # nothing committed yet
    assert [row["name"] for row in await side.rows("md_customer")] == [side.name]


async def _decide_time_travel(side: _Side, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §4, ``as_of()`` + ``fetch()``: refused below SurrealDB 3.x before the read is sent (T8; the embedded
    ``mem://`` engine keeps no history at all: T6), sent on 3.x — whose in-memory server then refuses the store (T7)."""
    async with SurrealDBConnectionManager.using(side.name):
        await MdPost(id="p1", title=side.name).save()
        sent = _record_requests(monkeypatch, await SurrealDBConnectionManager.get_client())

        def read() -> Awaitable[Any]:
            return MdPost.objects().as_of(_MOMENT).fetch("author").exec()  # type: ignore[no-any-return]

        if side.kind == "mem":
            with pytest.raises(SurrealDbError) as info:
                await read()
            assert str(info.value) == embedded.history_unsupported("mem://")
            assert sent == []
        elif side.line < 3:
            with pytest.raises(SurrealDbError) as info:
                await read()
            assert str(info.value) == time_travel.fetch_refused()
            assert sent == []
        else:
            try:
                await read()
            except SurrealDbError as exc:  # T7: a server started in memory keeps no history
                assert "needs a store that keeps history" in str(exc), exc
            assert [statement for statement in sent if statement.endswith(" FETCH author VERSION $_as_of;")], sent


async def _decide_change_feeds(sides: list[_Side]) -> None:
    """Spec §4, change feeds: a stream on each connection at once, in one event loop, reads its own feed with its own
    line's cursor rule — ``changes(since=stream.cursor)`` resumes right after the last event, on each."""
    streams: list[Any] = []
    cursors: list[int | None] = []
    try:
        for side in sides:
            async with SurrealDBConnectionManager.using(side.name):
                stream = MdEvent.objects().changes(poll_interval=0.05)
                await stream.start()
            streams.append(stream)
        for index, side in enumerate(sides):
            async with SurrealDBConnectionManager.using(side.name):
                await MdEvent(id=f"c{index}", title=side.name).save()
        for index, (side, stream) in enumerate(zip(sides, streams, strict=True)):
            [event] = await _next_events(stream, 1)
            assert (event.record_id, event.instance.title) == (f"md_event:c{index}", side.name)
            assert stream._major == side.line
            cursors.append(stream.cursor)
    finally:
        for stream in streams:
            stream.stop()
    for index, (side, cursor) in enumerate(zip(sides, cursors, strict=True)):
        async with SurrealDBConnectionManager.using(side.name):
            await MdEvent(id=f"r{index}", title=side.name).save()
            resumed = MdEvent.objects().changes(since=cursor, poll_interval=0.05)
            try:
                [event] = await _next_events(resumed, 1)
            finally:
                resumed.stop()
        assert (event.record_id, event.instance.title) == (f"md_event:r{index}", side.name)


async def _decide_live(sides: list[_Side]) -> None:
    """Spec §4, live queries: a stream on each server connection at once receives its own connection's events only;
    the embedded engine refuses one (E1) before anything is opened."""
    async with contextlib.AsyncExitStack() as stack:
        streams: list[tuple[int, _Side, Any]] = []
        for index, side in enumerate(sides):
            async with SurrealDBConnectionManager.using(side.name):
                if side.kind == "mem":
                    with pytest.raises(SurrealDbError) as info:
                        await MdNote.objects().live().start()
                    assert str(info.value) == embedded.live_unsupported("mem://")
                    continue
                stream = await MdNote.objects().live().start()
            stack.push_async_callback(stream.stop)
            streams.append((index, side, stream))
        # Written last to first: a stream reading another connection would get that connection's write first.
        for index, side in reversed(list(enumerate(sides))):
            async with SurrealDBConnectionManager.using(side.name):
                await MdNote(id=f"l{index}", text=side.name).save()
        for index, side, stream in streams:
            [event] = await _next_events(stream, 1)
            assert (event.record_id, event.instance.text) == (f"md_note:l{index}", side.name)


class _PerConnectionDecisions:
    """Spec §4: each connection decides on its own server — two connections side by side, in one process and one event
    loop. A subclass gives the pair (:meth:`_sides`)."""

    @staticmethod
    def _sides() -> contextlib.AbstractAsyncContextManager[list[_Side]]:
        raise NotImplementedError

    async def test_transactions(self) -> None:
        async with self._sides() as sides:
            for side in sides:
                await _decide_transactions(side)

    async def test_as_of_fetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with self._sides() as sides:
            for side in sides:
                await _decide_time_travel(side, monkeypatch)

    async def test_change_feeds(self) -> None:
        async with self._sides() as sides:
            await _decide_change_feeds(sides)

    async def test_live_streams(self) -> None:
        async with self._sides() as sides:
            await _decide_live(sides)


class TestMixedLinesE2E(_PerConnectionDecisions):
    """AC-10 (spec §9.3): ``md_3x`` and ``md_2x``, a SurrealDB 3.x and a 2.x server in one process — interactive
    against buffered transactions, ``as_of().fetch()`` refused on ``md_2x`` only, each line's change-feed cursor, live
    streams that receive their own events only. Runs when ``SURREALDB_PORT_3X`` and ``SURREALDB_PORT_2X`` are set
    (the ``mixed-lines`` CI job, the team's ``test-dbs.sh test``), skips otherwise."""

    @staticmethod
    def _sides() -> contextlib.AbstractAsyncContextManager[list[_Side]]:
        return _mixed_lines()


class TestServerBesideEmbeddedE2E(_PerConnectionDecisions):
    """AC-9, and AC-10's always-on twin (spec §9.3): the line under test and the embedded engine side by side in one
    process — the decisions of :class:`TestMixedLinesE2E` with ``md_a`` over WebSocket and ``md_mem`` on ``mem://``
    (a 2.x core); a ws, an http and a ``mem://`` connection each decide their transaction strategy on their own
    server."""

    @staticmethod
    def _sides() -> contextlib.AbstractAsyncContextManager[list[_Side]]:
        return _beside_embedded()

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


class MdItem(BaseSurrealModel):
    """Routed nowhere: the default connection, or the ``using()`` block's (the query-cache tests)."""

    model_config = SurrealConfigDict(table_name="md_item")

    id: str | None = None
    v: str = ""


@pytest.fixture
def fresh_query_cache() -> Iterator[None]:
    """The query cache's default settings and an empty store around the test: the cache is process-wide."""
    QueryCache.configure()
    QueryCache.clear()
    yield
    QueryCache.configure()
    QueryCache.clear()


async def _cached_values(name: str) -> list[str]:
    """The values ``MdItem.objects().cache().exec()`` returns inside ``using(name)``."""
    async with SurrealDBConnectionManager.using(name):
        return sorted(item.v for item in await MdItem.objects().cache().exec())


def _cache_counts() -> tuple[int, int]:
    stats = QueryCache.stats()
    return stats["hits"], stats["misses"]


@pytest.mark.usefixtures("fresh_query_cache")
class TestCachePerConnectionE2E:
    """AC-21, spec §7.11 (both lines, ws and http, and ``mem://``): the ORM's cached reads are scoped per client, so
    per connection — the same statement on ``md_a`` and ``md_b`` is two entries, each with its own rows; invalidation
    stays per table name across connections (D16: over-invalidation, never a stale hit); the application's own keys
    tell connections apart only when the connection's name is among their variables (README section 30's recipe)."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_cached_reads_stay_on_their_connection(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _databases(kind, "md_a", "md_b"):
            clients = {name: await SurrealDBConnectionManager.get_client(name) for name in ("md_a", "md_b")}
            for name, client in clients.items():
                await client.query("UPSERT md_item:one SET v = $v;", {"v": name})
            requests = {name: _record_requests(monkeypatch, client) for name, client in clients.items()}
            # Two misses: one statement, two databases.
            for name in clients:
                assert await _cached_values(name) == [name]
            assert {name: len(sent) for name, sent in requests.items()} == {"md_a": 1, "md_b": 1}
            assert _cache_counts() == (0, 2)
            # Out of band (not an ORM write): each database changes, and no cached read hears of it.
            for client in clients.values():
                await client.query("UPSERT md_item:oob SET v = 'oob';", {})
            before = {name: len(sent) for name, sent in requests.items()}
            # Two hits, each with its own rows, and no request.
            for name in clients:
                assert await _cached_values(name) == [name]
            assert {name: len(sent) for name, sent in requests.items()} == before
            assert _cache_counts() == (2, 2)
            # D16: an ORM write to md_item on md_a also invalidates md_b's cached md_item read.
            async with SurrealDBConnectionManager.using("md_a"):
                await MdItem(id="orm", v="orm").save()
            assert await _cached_values("md_b") == ["md_b", "oob"]
            assert len(requests["md_b"]) == before["md_b"] + 1
            assert _cache_counts() == (2, 3)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_application_keys_name_the_connection(self, kind: str) -> None:
        async with _databases(kind, "md_a", "md_b"):
            statement = "SELECT * FROM md_item WHERE v = $v;"
            recipe: dict[str, str] = {}
            bare: dict[str, str] = {}
            for name in ("md_a", "md_b"):
                async with SurrealDBConnectionManager.using(name):
                    variables = {"v": "x"}
                    connection = MdItem.get_connection_name()
                    recipe[name] = QueryCache.make_key(statement, {**variables, "connection": connection}, "md_item")
                    bare[name] = QueryCache.make_key(statement, variables, "md_item")
            assert recipe["md_a"] != recipe["md_b"]
            assert bare["md_a"] == bare["md_b"]  # the documented limit: such a key holds no connection
            QueryCache.set(recipe["md_a"], ["md_a's rows"], "md_item")
            assert QueryCache.get(recipe["md_a"]) == ["md_a's rows"]
            # The table is left as is in the recipe, so an ORM write to md_item — on any connection — removes it.
            async with SurrealDBConnectionManager.using("md_b"):
                await MdItem(id="w", v="w").save()
            assert QueryCache.get(recipe["md_a"]) is None


@pytest.mark.usefixtures("unset_default")
class TestRemoveConnectionE2E:
    """AC-22 (both lines, ws): ``remove_connection()`` closes the connection's clients of two event loops (the second
    one in a thread), ends its live stream, forgets its session and bumps its generation; a model routed to it then
    raises R2, and ``add_connection()`` of the same name right after registers a fresh connection that works."""

    async def test_everything_it_held_ends(self) -> None:
        router = SurrealDBConnectionManager
        async with _live_connections():
            connection = router._manager_for("md_a")
            here = await router.get_client("md_a")
            async with router.using("md_a"):
                await router.signin(username="root", password="root")
            assert connection.get_session_token() is not None
            stream = await MdEvent.objects().live().start()
            try:
                parked = threading.Event()
                outcome: dict[str, Any] = {}

                async def on_another_loop() -> None:
                    client = await router.get_client("md_a")
                    assert await client.query("RETURN 1;", {}) == 1
                    receiving = live.recv_task_of(client)
                    assert receiving is not None
                    outcome["client"] = client
                    parked.set()
                    # This loop runs until the removal has closed the client's transport here, on its own loop.
                    await asyncio.wait({receiving}, timeout=15)
                    outcome["dead"] = live.is_dead(client)
                    others = {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}
                    if others:
                        await asyncio.wait(others, timeout=10)

                thread = _start_loop_thread(on_another_loop, outcome)
                assert await asyncio.to_thread(parked.wait, 10)
                assert outcome["client"] is not here
                generation = connection._settings_generation()

                await router.remove_connection("md_a")

                with pytest.raises(StopAsyncIteration):
                    await _read_one(stream)
                assert stream.is_active is False
                await _drain_background()
                here_receiving = live.recv_task_of(here)
                assert here_receiving is not None
                await asyncio.wait({here_receiving}, timeout=10)
                assert live.is_dead(here)
                await asyncio.to_thread(thread.join, 30)
                assert not thread.is_alive()
                assert "error" not in outcome, outcome.get("error")
                assert outcome["dead"] is True
                assert vars(connection)[_P + "clients"] == {}
                assert connection.get_session_token() is None
                assert connection._settings_generation() == generation + 1
                assert "md_a" not in router.list_connections()
                with pytest.raises(ValueError) as info:
                    await MdEvent.objects().exec()
                assert str(info.value) == _r2("MdEvent", "md_a")
            finally:
                await stream.stop()

            _register("ws", "md_a", database="md_a")
            assert router._manager_for("md_a") is not connection
            await MdEvent(id="again", title="again").save()
            assert [event.title for event in await MdEvent.objects().exec()] == ["again"]
            assert _titles(await _root_rows("md_a", "md_event")) == ["again"]


class TestThreadsE2E:
    """AC-23 (threads; both lines, ws and http, and ``mem://``): two threads, each running its own event loop with
    ``asyncio.run()``, write through ``md_a`` and ``md_b`` while both ``using()`` blocks are open; each write lands in
    its own database (asserted after ``join()``). A new thread starts outside any block (spec M9); the embedded engine
    is the connection's one for the whole process."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_two_threads_two_connections(self, kind: str) -> None:
        async with _databases(kind, "md_a", "md_b"):
            both_open = threading.Barrier(2)
            outcome: dict[str, Any] = {}

            async def work(name: str) -> None:
                outcome[f"{name} active"] = SurrealDBConnectionManager.get_active_connection_name()
                async with SurrealDBConnectionManager.using(name):
                    outcome[name] = await SurrealDBConnectionManager.get_client()
                    await asyncio.to_thread(both_open.wait, 10)  # both blocks are open, on two loops
                    await MdNote(id="t", text=name).save()
                if kind != "mem":
                    await SurrealDBConnectionManager.close_connection(name)  # this loop's client only

            def run(name: str) -> None:
                try:
                    asyncio.run(work(name))
                except BaseException as exc:  # noqa: BLE001 - reported through `outcome`
                    outcome[f"{name} error"] = exc

            threads = [threading.Thread(target=run, args=(name,)) for name in ("md_a", "md_b")]
            for thread in threads:
                thread.start()
            for thread in threads:
                await asyncio.to_thread(thread.join, 30)
            assert not any(thread.is_alive() for thread in threads)
            assert {key: value for key, value in outcome.items() if key.endswith("error")} == {}
            assert (outcome["md_a active"], outcome["md_b active"]) == (None, None)
            assert outcome["md_a"] is not outcome["md_b"]
            for name in ("md_a", "md_b"):
                if kind == "mem":
                    engine = await SurrealDBConnectionManager.get_client(name)
                    assert outcome[name] is engine
                    rows = await _rows_of(engine, "md_note")
                else:
                    rows = await _root_rows(name, "md_note")
                assert [row["text"] for row in rows] == [name]


# --- AC-24: README section 30, the tables and sentences it touches, and its examples (spec §11; C4, C7, P5, P8, P9) --

_SECTION_29 = "### 29. Query cache (`cache()`, `QueryCache`)"
_SECTION_30 = "### 30. Multiple databases (`add_connection()`, `using()`)"
#: Spec §11 item 1: section 30's subsections, in order, after an untitled intro holding the first example.
_SUBSECTIONS_30 = [
    "Routing a model",
    "Redirecting a block: `using()`",
    "Transactions and other operations across connections",
    "What each connection keeps for itself",
    "Which connection a manager method uses",
    "Coming from the full SurrealDB-ORM",
]
#: Spec §11 item 1: the four examples it writes out, exactly (the intro one first).
_INTRO_EXAMPLE = """\
from surreal_orm_lite import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager

# Any URL works: ws://…/rpc or http://… (with user= and password=), mem://, surrealkv://<path>
SurrealDBConnectionManager.set_connection(url="mem://", namespace="shop", database="main")   # "default"
SurrealDBConnectionManager.add_connection("analytics", url="mem://", namespace="shop", database="analytics")

class Customer(BaseSurrealModel):                       # no connection key: "default"
    id: str | None = None
    name: str

class PageView(BaseSurrealModel):
    model_config = SurrealConfigDict(connection="analytics")
    id: str | None = None
    path: str

await Customer(id="ada", name="Ada").save()             # shop/main
await PageView(id="v1", path="/pricing").save()         # shop/analytics
names = SurrealDBConnectionManager.list_connections()   # ['default', 'analytics']
"""
_USING_EXAMPLE = """\
async with SurrealDBConnectionManager.using("analytics"):
    elsewhere = await Customer.objects().count()        # 0
here = await Customer.objects().count()                 # 1
"""
_TRANSACTION_EXAMPLE = """\
from surreal_orm_lite import SurrealDbError

async with SurrealDBConnectionManager.transaction("analytics") as tx:
    await PageView(id="v2", path="/").save(tx=tx)

try:
    async with SurrealDBConnectionManager.transaction() as tx:      # the default connection
        await PageView(id="v3", path="/x").save(tx=tx)
except SurrealDbError as exc:
    refused = type(exc).__name__                        # SurrealDbError
"""
_KEY_EXAMPLE = """\
from surreal_orm_lite import QueryCache

statement = "SELECT count() AS n FROM page_view GROUP ALL;"
key_main = QueryCache.make_key(statement, {"connection": "default"}, "page_view")
key_analytics = QueryCache.make_key(statement, {"connection": "analytics"}, "page_view")
distinct = key_main != key_analytics                   # True
"""
#: Spec §11 (C4, P5): the last bullet of "What each connection keeps for itself", exactly.
_D7_BULLET = (
    "- Registering a name again with other settings, or removing it with `remove_connection()`, ends what the ORM "
    "holds on the old settings: a client still opening is closed unused (a retryable `SurrealDbConnectionError`: "
    "retry the call), live streams end, a change feed ends at its next read (`SurrealDbConnectionError`: its cursor "
    "belongs to the old database, start a new one), and an open `transaction()` is cancelled when its block ends, "
    "with nothing committed (`SurrealDbConnectionError`: open it again). A client you obtained yourself with "
    "`get_client()` before the change is yours: on WebSocket its socket is closed and its next request fails; an "
    "HTTP client keeps the namespace and database it was opened on, so call `get_client()` again; an embedded client "
    "is the connection's one in-process session: it follows a new namespace or database at the ORM's next call, but "
    "after a new URL or `remove_connection()` it is the previous engine (with SDK 2.x still running), so stop using "
    "it, as after `close_connection()`. `set_connection()` and the `set_*()` setters do none of this: clients opened "
    "on the previous settings keep serving, as in earlier versions."
)
#: AC-24: the revision-3 wording P5 replaced, which must not come back.
_REVISION_3_WORDING = "one in-process session and follows the new namespace and database"
#: Spec §11 (§7.11): the paragraph before the application-key example, exactly.
_KEY_PARAGRAPH = (
    "Entries you cache yourself with `QueryCache.make_key()` and `set()` are not scoped to a connection: the key is "
    "built from the table, the statement and the variables only. When the same statement runs on two connections, "
    "put the connection's name among the variables (keep the table name as it is, so the ORM's writes still "
    "invalidate the entry)."
)
#: Spec §11 (C7, P9, D26): the nine "Coming from the full SurrealDB-ORM" bullets, exactly, and no tenth.
_COMING_FROM_30 = [
    "- Same names: `add_connection()`, `remove_connection()`, `get_config()`, `list_connections()`, "
    "`get_active_connection_name()`, `using()`, `ConnectionConfig`, `SurrealConfigDict(connection=)`, "
    "`get_connection_name()`, `get_client(name)`, `close_connection(name)`.",
    "- `ConnectionConfig` has the same fields in the same order, built by position or by keyword, without `protocol` "
    "(the official SDK speaks CBOR only); `user` and `password` may be `None` (the embedded engine needs neither).",
    "- `close_connection()` without a name: closing an embedded `mem://` client discards its database, so "
    "`close_connection()` without a name stays on the current connection; `close_all_connections()` closes every one "
    "(full ORM: `close_connection()` closes every connection).",
    "- `list_connections()` lists `'default'` first while it is configured (full ORM: registration order).",
    "- Registering a name again with other settings closes its open clients and ends what the ORM holds on the old "
    "settings (full ORM: drops the clients without closing them). `set_connection()` keeps its open clients, as in "
    "earlier lite versions.",
    "- The getters, the setters and `reconnect()` follow `using()` (full ORM: the default connection).",
    "- `transaction(name)`, `embedded_database(connection=)` and the errors for operations across connections are lite "
    "additions.",
    "- `add_connection()` checks its arguments before registering anything (a non-empty name, URL, namespace and "
    "database; a user and password for a server URL, optional on the embedded engine), and a model's `connection` key "
    "must be a non-empty string, checked when the class is defined (full ORM: no checks). `ConnectionConfig` keeps the "
    "password out of its `repr`.",
    "- `get_client(name)`, `close_connection(name)` and `transaction(name)` accept `name` in code, but "
    "`inspect.signature()` does not show it, so FastAPI's `Depends(SurrealDBConnectionManager.get_client)` adds no "
    "`?name=` query parameter: a request never chooses the connection (full ORM: `name` is visible and becomes a query "
    "parameter).",
]
#: Spec §11 item 5: the row v0.29.0 adds to the 2.x/3.x table, cell by cell, and the clause it appends to the header.
_MULTI_DATABASE_ROW = [
    "Several connections in one process (`add_connection()`, `using()`)",
    "each connection follows its own server: a 2.x connection's transactions are buffered, whatever the others are",
    "each 3.x WebSocket connection gets interactive transactions, beside 2.x connections in the same process",
    "v0.29.0",
]
_HEADER_CLAUSE_29 = (
    "rows added in v0.28.0 on 2.7.0 and 3.3.4, identical on 3.1.5; the rows added in v0.28.1 on 2.7.0 and 3.3.4, "
    "identical on 3.1.5; rows added in v0.29.0 on 2.7.0 and 3.3.4 in one process, identical on 3.1.5"
)
#: Spec §11 item 6 (P8): all four cells of the embedded table's "Clients" row and of its new row.
_EMBEDDED_CLIENTS_ROW = [
    "Clients",
    "one per connection for the whole process, shared by every event loop and thread",
    "one per connection and event loop",
    "v0.25.0",
]
_EMBEDDED_CONNECTIONS_ROW = [
    "Several connections (`add_connection()`)",
    "`mem://`: each name is a database of its own; an on-disk store (`surrealkv://`, `file://`) is open under one "
    "name at a time — a second name raises `SurrealDbConnectionError`",
    "each name opens its own clients; two databases on one server are isolated",
    "v0.29.0",
]
#: Spec §11 item 7: the two rewritten "Connections and event loops" bullets, and the v0.28.0 one they replace.
_EVENT_LOOP_BULLETS = [
    "- `close_connection()` closes **the running loop's** client of the current connection only (the default, or the "
    "one a `using()` block names).",
    "- `close_all_connections()` tears down every cached client of every connection; `unset_connection()` does that "
    "for the default connection only, `remove_connection(name)` for a named one.",
]
_EVENT_LOOP_V028 = "tears down every cached client (used by `unset_connection()`)"
_SECTION_30_LINK = "[section 30](#30-multiple-databases-add_connection-using)"
_CONTEXT_MANAGER_SENTENCE = (
    "`async with SurrealDBConnectionManager():` uses the current connection (the default, or the `using()` block's) "
    "and closes that connection's client on exit."
)
#: Spec §5.4 as section 30's table: the method column and the connection it acts on.
_WHICH_CONNECTION_ROWS = [
    [
        "`add_connection()`, `remove_connection()`, `get_config()`, `list_connections()`, `using()`, "
        "`get_active_connection_name()`",
        "the registry, by the name given",
    ],
    ["`set_connection()`, `unset_connection()`", "always the default connection"],
    ["`close_all_connections()`", "every connection, on every event loop"],
    ["`get_client(name)`, `close_connection(name)`, `transaction(name)`", "_name_, else the current connection"],
    [
        "`subscribe_live(uuid)`, `kill(uuid)`",
        "the connection whose live stream started _uuid_ on this event loop, else the current connection",
    ],
    [
        "every other method: the session methods (`signin()`, `info()` …), the getters and setters (`get_url()`, "
        "`set_database()` …), `reconnect()`, `validate_connection()`, `ephemeral_client()`, `call_function()`, "
        "`is_connected()`, `is_embedded()`, `async with SurrealDBConnectionManager():`",
        "the current connection",
    ],
]
#: "Which connection a manager method uses": putting back a manager method replaced by hand (review of PR #254).
_HAND_PATCH_NEEDLES = (
    "replace by hand",
    "inherited by every connection",
    "`vars(SurrealDBConnectionManager)[name]`",
    "pytest's `monkeypatch.setattr()` and `unittest.mock.patch.object()`",
    "`SurrealDBConnectionManager.<name>`",
    "a method bound to `SurrealDBConnectionManager`",
    "acts on the current connection",
    "with no error",
)
_HAND_PATCH_EXAMPLE = """\
async def offline():
    raise ConnectionError("no database in this test")

saved = vars(SurrealDBConnectionManager)["get_client"]
SurrealDBConnectionManager.get_client = offline         # every connection calls offline()
try:
    await PageView.objects().count()                    # raises ConnectionError
except ConnectionError:
    pass
finally:
    SurrealDBConnectionManager.get_client = saved       # the classmethod again
views = await PageView.objects().count()                # 2
"""
#: "Transactions and other operations across connections": each refusal (X1–X5) and each limit the ORM cannot see
#: (N8) is one bullet holding these names together.
_ACROSS_CONNECTIONS = {
    "X1 transactions": ("`transaction(name)`", "`objects(tx=)`", "`refresh(tx=)`", "`call_function(tx=)`", "independent"),
    "X2 subqueries": ("`Subquery`", "filter value", "`SurrealDbError`"),
    "X3 relations": ("`relate()`", "`remove_relation()`", "`remove_all_relations()`", "`get_related()`", "`RecordID`"),
    "X4 links": ("`fetch()`", "dotted path", "`order_by()`", "`values()`", "`SurrealDbError`"),
    "X5 on-disk stores": ("`surrealkv://`", "`file://`", "`SurrealDbConnectionError`", "`mem://`"),
    "inside using()": ("`using()`", "none of these"),
    "N8 untyped links": ("`RecordID`", "`str`", "`dict`"),
    "N8 graph arrows": ('`select("->likes->Post")`', "`traverse()`"),
    "N8 edges": ("endpoint ids",),
    "N8 raw SurrealQL": ("`raw_query()`", "`QuerySet.query()`", "`call_function()`", "`Transaction.add()`"),
}
#: "What each connection keeps for itself": each holder of spec §11 is one bullet holding these names together.
_HOLDERS = {
    "clients": ("one per event loop", "one for the whole process"),
    "session and tokens": ("`signin()`", "token", "`using("),
    "live streams and change feeds": ("live streams", "change feeds", "when they start"),
    "transactions": ("transactions", "interactive", "buffered"),
    "stored-function signatures": ("stored-function signatures", "`call_function()`"),
    "cached reads": ("`cache()`", "never served to another connection"),
}


def _section_30() -> str:
    return _readme_part(_SECTION_30)


def _python_blocks(text: str) -> list[str]:
    """The ``python`` blocks of *text*, in order."""
    return re.findall(r"^```python\n(.*?)^```$", text, re.MULTILINE | re.DOTALL)


def _part_30(heading: str) -> str:
    """One ``####`` subsection of section 30, as written."""
    return _subsection(f"#### {heading}", _section_30())


class TestMultiDatabaseDocs:
    """Unit, AC-24: what README section 30, the features, roadmap, comparison and both behaviour tables, the
    "Connections and event loops" bullets and the "Context Manager" sentence say about several connections (spec §11;
    C4, C7, P5, P8, P9)."""

    def test_section_30_follows_section_29(self) -> None:
        """Right after section 29 (after its "Coming from" bullets) and before the ``---`` of "Configuration Options"."""
        readme = _readme()
        start_29 = readme.index(f"\n{_SECTION_29}\n")
        start_30 = readme.index(f"\n{_SECTION_30}\n")
        configuration = readme.index("\n## Configuration Options\n")
        assert start_29 < start_30 < configuration
        assert re.findall(r"^#{2,3} .*$", readme[start_29 + 1 : configuration], re.MULTILINE) == [
            _SECTION_29,
            _SECTION_30,
        ]
        assert readme[:configuration].rstrip().endswith("\n---"), "section 30 ends before the --- of the next part"

    def test_subsections_in_order(self) -> None:
        section = _section_30()
        assert re.findall(r"^#### (.*)$", section, re.MULTILINE) == _SUBSECTIONS_30
        intro = section.split("\n#### ", 1)[0]
        assert _python_blocks(intro)[:1] == [_INTRO_EXAMPLE]

    def test_examples_as_the_spec_writes_them(self) -> None:
        """The intro example first, then each other spec example in its subsection; python blocks only."""
        blocks = _python_blocks(_section_30())
        assert blocks[0] == _INTRO_EXAMPLE
        assert _USING_EXAMPLE in _python_blocks(_part_30(_SUBSECTIONS_30[1]))
        assert _TRANSACTION_EXAMPLE in _python_blocks(_part_30(_SUBSECTIONS_30[2]))
        assert _KEY_EXAMPLE in _python_blocks(_part_30(_SUBSECTIONS_30[3]))
        fences = re.findall(r"^```(\S*)$", _section_30(), re.MULTILINE)
        assert fences[1::2] == [""] * (len(fences) // 2) and set(fences[0::2]) == {"python"}, fences
        assert "mem://" in blocks[0].splitlines()[2], "the first example says it runs on mem://"

    def test_registry_names(self) -> None:
        intro = _normalised(_section_30().split("\n#### ", 1)[0])
        for name in (
            "`add_connection(",
            "`get_config(",
            "`ConnectionConfig`",
            "`list_connections()`",
            "`remove_connection(",
            "`unset_connection()`",
            "`get_client(name)`",
            "`close_connection(name)`",
            "`transaction(name)`",
            "`SurrealDbConnectionError`",
        ):
            assert name in intro, name

    def test_routing_a_model(self) -> None:
        text = _normalised(_part_30("Routing a model"))
        for name in (
            'SurrealConfigDict(connection="analytics")',
            "`get_connection_name()`",
            "`TypeError`",
            "`ValueError`",
            "first database call",
            "`BaseSurrealEdge`",
            "`AuthenticatedUserMixin`",
            "`table_name`",
            "when it starts",
        ):
            assert name in text, name

    def test_redirecting_a_block(self) -> None:
        text = _normalised(_part_30(_SUBSECTIONS_30[1]))
        for name in (
            "`get_active_connection_name()`",
            "`ValueError`",
            "nest",
            "`contextvars.ContextVar`",
            "`asyncio.to_thread()`",
            "`threading.Thread`",
            "`embedded_database()`",
            "`embedded_database(connection=",
        ):
            assert name in text, name

    @pytest.mark.parametrize("needles", list(_ACROSS_CONNECTIONS.values()), ids=list(_ACROSS_CONNECTIONS))
    def test_operations_across_connections(self, needles: tuple[str, ...]) -> None:
        """X1–X5 and the N8 limits, each one bullet."""
        bullets = _bullets(_part_30(_SUBSECTIONS_30[2]))
        assert [bullet for bullet in bullets if all(needle in bullet for needle in needles)], bullets

    @pytest.mark.parametrize("needles", list(_HOLDERS.values()), ids=list(_HOLDERS))
    def test_what_each_connection_keeps(self, needles: tuple[str, ...]) -> None:
        bullets = _bullets(_part_30("What each connection keeps for itself"))
        assert [bullet for bullet in bullets[:-1] if all(needle in bullet for needle in needles)], bullets

    def test_d7_bullet_exactly(self) -> None:
        """C4 and P5: the last holder bullet, exactly; the revision-3 wording is gone from the whole README."""
        bullets = _bullets(_part_30("What each connection keeps for itself"))
        assert bullets[-1] == _D7_BULLET, bullets[-1]
        assert _REVISION_3_WORDING not in _normalised(_readme())

    def test_application_key_paragraph_then_its_example(self) -> None:
        """§7.11: the paragraph exactly, after the bullets, and the make_key() example right after it."""
        part = _part_30("What each connection keeps for itself")
        after_bullets = part.split(_D7_BULLET[-40:], 1)[1]
        paragraph, example = after_bullets.split("```python\n", 1)
        assert _normalised(paragraph) == _KEY_PARAGRAPH, paragraph
        assert example.split("```", 1)[0] == _KEY_EXAMPLE

    def test_which_connection_table_and_timing(self) -> None:
        part = _part_30("Which connection a manager method uses")
        [lines] = _tables(part)
        assert _cells(lines[0]) == ["Method", "Acts on"]
        assert [cells for _, cells in _table_rows(part)] == _WHICH_CONNECTION_ROWS
        text = _normalised(part)
        assert "when it is awaited" in text and "when it is opened" in text, text
        assert "the innermost `using()` block's, else the default one" in text, text

    def test_which_connection_hand_patched_methods(self) -> None:
        """Review of PR #254: how to put back a manager method replaced by hand, after the table, with its example."""
        part = _part_30("Which connection a manager method uses")
        after_table = part.split(_tables(part)[0][-1], 1)[1]
        text = _normalised(after_table)
        for needle in _HAND_PATCH_NEEDLES:
            assert needle in text, needle
        assert _python_blocks(part) == [_HAND_PATCH_EXAMPLE]

    def test_coming_from_the_full_orm(self) -> None:
        """C7, P9 and D26: nine bullets, exactly, and no tenth."""
        assert _bullets(_part_30("Coming from the full SurrealDB-ORM")) == _COMING_FROM_30

    def test_features_row(self) -> None:
        features = [cells for _, cells in _table_rows(_readme_part("## Features"))]
        firsts = [cells[0] for cells in features]
        row = ["Multiple databases (named connections, `using()`)", "✅"]
        assert row in features, features
        assert firsts.index(row[0]) == firsts.index("Query cache (`cache()`, TTL, invalidated by ORM writes)") + 1

    def test_roadmap_row(self) -> None:
        roadmap = [cells for _, cells in _table_rows(_readme_part("## Roadmap"))]
        firsts = [cells[0] for cells in roadmap]
        start = firsts.index("v0.28.0")
        assert roadmap[start : start + 3] == [
            ["v0.28.0", "Query cache", "✅ Released"],
            ["v0.29.0", "Tier 2 — Multi-database", "✅ Released"],
            ["v0.30.0 – v0.39.0", "Tier 3 — Advanced (search, DDL, migrations, CLI)", "📋 Planned"],
        ], roadmap

    def test_comparison_row(self) -> None:
        comparison = [cells for _, cells in _table_rows(_readme_part("## SurrealDB-ORM-lite vs SurrealDB-ORM"))]
        firsts = [cells[0] for cells in comparison]
        start = firsts.index("Query Cache") + 1
        assert comparison[start] == ["Multi-database", "✅ v0.29.0 (`add_connection()`, `using()`)", "✅"], comparison
        assert comparison[start + 1][0] == "Schema Introspection", comparison

    def test_server_behaviour_row(self) -> None:
        """One row, Since v0.29.0, after the query-cache row and v0.28.1's rows, before the table's last row (later
        versions add their rows after it)."""
        rows = [cells for _, cells in _table_rows(_readme_part(_SERVER_BEHAVIOUR))]
        index = rows.index(_MULTI_DATABASE_ROW)
        assert rows[index - 1] == _PAGING_ROW, rows[index - 1]
        between = rows[rows.index(_QUERY_CACHE_ROW) + 1 : index]
        assert between and all("v0.28.1" in cells[3] for cells in between), between
        assert index < len(rows) - 1 and rows[-1][3] == "v0.19.1", rows[-1]
        assert [cells for cells in rows if cells[3] == "v0.29.0"] == [_MULTI_DATABASE_ROW]

    def test_server_behaviour_table_keeps_its_widths(self) -> None:
        """Every line 535 characters, the columns 136, 175, 181 and 38 wide: no line re-padded."""
        [lines] = _tables(_readme_part(_SERVER_BEHAVIOUR))
        assert {len(line) for line in lines} == {535}
        for line in lines:
            assert [len(cell) for cell in line.split("|")[1:-1]] == _SERVER_TABLE_WIDTHS, line

    def test_server_behaviour_header_clause(self) -> None:
        """v0.29.0's clause follows v0.28.0's and v0.28.1's (later versions append theirs after it)."""
        header = _normalised(_readme_part(_SERVER_BEHAVIOUR).split("\n|", 1)[0])
        assert _HEADER_CLAUSE_29 in header, header

    def test_embedded_table_rows(self) -> None:
        """P8: all four cells of the "Clients" row and of the new row, which comes before the catch-all last row."""
        rows = [cells for _, cells in _table_rows(_readme_part(_EMBEDDED_BEHAVIOUR))]
        [clients] = [cells for cells in rows if cells[0] == "Clients"]
        assert clients == _EMBEDDED_CLIENTS_ROW, clients
        assert rows[-2] == _EMBEDDED_CONNECTIONS_ROW, rows[-2]
        assert rows[-1][0] == "Everything else", rows[-1]

    def test_connections_and_event_loops(self) -> None:
        """Spec §11 item 7: the two bullets rewritten exactly, the third kept, a paragraph pointing to section 30."""
        part = _readme_part("### Connections and event loops")
        bullets = _bullets(part)
        assert bullets[:2] == _EVENT_LOOP_BULLETS, bullets
        assert bullets[2].startswith("- `is_connected()` answers for the loop asking"), bullets
        assert len(bullets) == 3, bullets
        assert _EVENT_LOOP_V028 not in _normalised(_readme())
        paragraphs = [_normalised(paragraph) for paragraph in part.split("\n\n")]
        [index] = [index for index, paragraph in enumerate(paragraphs) if paragraph.startswith(_EVENT_LOOP_BULLETS[0])]
        after = paragraphs[index + 1]  # the paragraph right after the list
        assert _SECTION_30_LINK in after and "per-loop clients" in after, after

    def test_context_manager_sentence(self) -> None:
        part = _readme_part("### Context Manager")
        assert _normalised(part.split("```", 2)[2]) == _CONTEXT_MANAGER_SENTENCE, part

    def test_tables_have_no_raw_pipe_in_a_cell(self) -> None:
        """Every table v0.29.0 touches keeps one cell count per line and no escaped pipe."""
        parts = [
            _section_30(),
            _readme_part("## Features"),
            _readme_part(_SERVER_BEHAVIOUR),
            _readme_part(_EMBEDDED_BEHAVIOUR),
            _readme_part("## Roadmap"),
            _readme_part("## SurrealDB-ORM-lite vs SurrealDB-ORM"),
        ]
        tables = [lines for part in parts for lines in _tables(part)]
        assert len(tables) == len(parts), "section 30 has its table"
        for lines in tables:
            assert len({len(_cells(line)) for line in lines}) == 1 and "\\|" not in "".join(lines), lines

    def test_typing_examples(self) -> None:
        """AC-25: the typing guard exercises the registry, ``using()`` and model routing."""
        source = (Path(__file__).parent / "typing_examples.py").read_text(encoding="utf-8")
        for name in (
            "SurrealConfigDict(connection=",
            ".add_connection(",
            "assert_type(SurrealDBConnectionManager.get_config(",
            "ConnectionConfig | None",
            "SurrealDBConnectionManager.using(",
            ".get_connection_name(), str)",
            ".get_client(",
            ".transaction(",
            "embedded_database(connection=None)",
        ):
            assert name in source, name


#: The results section 30's comments state, by the names its examples assign (AC-24: each one asserted).
_STATED_RESULTS_30 = {"names", "where", "elsewhere", "here", "refused", "distinct", "views"}


def _stated_results_30(blocks: list[str]) -> dict[str, str]:
    """``{name: comment}`` for each line of *blocks*, indented or not, that assigns a result and states it in a
    trailing comment."""
    stated: dict[str, str] = {}
    for block in blocks:
        for name, comment in re.findall(r"^\s*(\w+) = .*?  # (.+)$", block, re.MULTILINE):
            assert name not in stated, f"{name} is stated twice"
            stated[name] = comment
    return stated


class TestMultiDatabaseReadmeExamplesE2E:
    """AC-24: every ``python`` block of README section 30 runs as written, in order and in one namespace, inside
    ``embedded_database()`` (the blocks point the default connection at ``mem://`` themselves); every result a
    comment states is the one the examples got, and each write landed in the database its comment names."""

    async def test_section_30_examples(self) -> None:
        blocks = _python_blocks(_section_30())
        stated = _stated_results_30(blocks)
        assert set(stated) == _STATED_RESULTS_30, sorted(stated)
        namespace: dict[str, Any] = {"__name__": "readme_section_30"}
        router = SurrealDBConnectionManager
        try:
            async with embedded_database():
                for code in blocks:
                    compiled = compile(code, "<README section 30>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                    # A doctest of this repository's own README (trusted text), top-level awaits included.
                    outcome = eval(compiled, namespace)
                    if inspect.iscoroutine(outcome):
                        await outcome

                for name, comment in stated.items():
                    assert comment == str(namespace[name]), (name, comment, namespace[name])

                customer, page_view = namespace["Customer"], namespace["PageView"]
                assert router.get_config() == ConnectionConfig("mem://", None, None, "shop", "main")
                assert router.get_config("analytics") == ConnectionConfig("mem://", None, None, "shop", "analytics")
                assert [row.get_raw_id() for row in await customer.objects().exec()] == ["ada"]
                assert sorted(row.get_raw_id() for row in await page_view.objects().exec()) == ["v1", "v2"]
                async with router.using("default"):
                    assert await page_view.objects().count() == 0  # v3 was refused, v1 and v2 are elsewhere
        finally:
            namespace.clear()
            await router.remove_connection("analytics")
