"""Multi-database (v0.29.0): named connections, each on a connection class of its own behind a routing manager.

``SurrealDBConnectionManager`` holds no connection state any more: each connection — the default one included —
is a private subclass whose name-mangled class attributes are that connection's state, and every state-touching
classmethod of the public class resolves the connection class and runs there. Unit tests first (no server); the
classes that open an engine or a server are suffixed ``E2E``. Models are prefixed ``Md``, tables ``md_*``.
"""

import inspect
from typing import Any

import pytest

from surreal_orm_lite import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager, connection_manager

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
