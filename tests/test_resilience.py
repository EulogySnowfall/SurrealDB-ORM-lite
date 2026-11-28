"""Connection resilience (v0.51.0): the reconnect policy and the per-connection resilience settings.

Unit tests (no server): :class:`~surreal_orm_lite.ReconnectPolicy` and its conversion to the SDK's
``ReconnectOptions``, the ``reconnect=``/``renew_access=`` settings of ``set_connection()`` and ``add_connection()``,
:class:`~surreal_orm_lite.ConnectionConfig`'s new fields and the settings epoch every setter bumps.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from surrealdb import ReconnectOptions

import surreal_orm_lite
from surreal_orm_lite import (
    ConnectionConfig,
    ReconnectPolicy,
    SurrealDBConnectionManager,
    _sdk,
    connection_manager,
)
from surreal_orm_lite.testing import embedded_database
from tests.conftest import default_connection_class
from tests.test_embedded import _embedded_engine_available

#: The prefix of the manager's name-mangled private attributes.
_P = "_SurrealDBConnectionManager__"
#: An address nothing listens on: these tests never open a server client.
_UNREACHABLE = "ws://127.0.0.1:9/rpc"
#: The ORM's field of ReconnectPolicy for each field of the SDK's ReconnectOptions (``enabled`` has none).
_SDK_FIELD = {
    "attempts": "attempts",
    "delay": "retry_delay",
    "max_delay": "retry_delay_max",
    "multiplier": "retry_delay_multiplier",
    "jitter": "retry_delay_jitter",
}


def _sdk_error(**kwargs: Any) -> BaseException:
    """What the SDK's ``ReconnectOptions`` raises for these ORM-named values."""
    try:
        ReconnectOptions(**{_SDK_FIELD[name]: value for name, value in kwargs.items()})
    except (TypeError, ValueError) as exc:
        return exc
    raise AssertionError(f"the SDK accepts {kwargs!r}")


@pytest_asyncio.fixture(loop_scope="function")
async def unset_default() -> AsyncIterator[None]:
    """The default connection unset for the test; its settings, reconnect policy and renewal put back after it."""
    default = default_connection_class()
    saved = default._settings()
    saved_resilience = (vars(default)[_P + "reconnect"], vars(default)[_P + "renew_access"])
    await SurrealDBConnectionManager.unset_connection()
    yield
    await SurrealDBConnectionManager.unset_connection()
    if saved[0] is not None:
        url, user, password, namespace, database = saved
        SurrealDBConnectionManager.set_connection(
            url, user, password, namespace, database, reconnect=saved_resilience[0], renew_access=saved_resilience[1]
        )


class _FakeServerClient:
    """A server client nothing talks to: what the retirement of a connection's clients takes out of the cache."""

    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class TestReconnectPolicy:
    """The policy is the SDK's ``ReconnectOptions`` under the ORM's names, validated by the SDK at construction."""

    def test_defaults_are_the_sdks(self) -> None:
        policy = ReconnectPolicy()
        assert policy.to_sdk() == ReconnectOptions()
        sdk_defaults = ReconnectOptions()
        for name, sdk_name in _SDK_FIELD.items():
            assert getattr(policy, name) == getattr(sdk_defaults, sdk_name), name

    def test_every_field_reaches_the_sdk(self) -> None:
        policy = ReconnectPolicy(attempts=3, delay=0.05, max_delay=0.2, multiplier=1.5, jitter=0.0)
        assert policy.to_sdk() == ReconnectOptions(
            enabled=True,
            attempts=3,
            retry_delay=0.05,
            retry_delay_max=0.2,
            retry_delay_multiplier=1.5,
            retry_delay_jitter=0.0,
        )

    def test_attempts_none_means_no_limit(self) -> None:
        assert ReconnectPolicy(attempts=None).to_sdk().attempts == -1
        assert ReconnectPolicy(attempts=-1).to_sdk().attempts == -1

    def test_zero_attempts_is_allowed(self) -> None:
        options = ReconnectPolicy(attempts=0).to_sdk()
        assert options.attempts == 0
        assert options.enabled is True

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"attempts": "3"},
            {"attempts": True},
            {"attempts": 2.0},
            {"attempts": -2},
            {"delay": "1"},
            {"delay": False},
            {"delay": -0.1},
            {"max_delay": None},
            {"max_delay": -1},
            {"multiplier": "2"},
            {"multiplier": -2.0},
            {"jitter": 1.5},
            {"jitter": -0.1},
            {"jitter": "0.1"},
        ],
        ids=repr,
    )
    def test_bad_values_raise_the_sdks_error_at_construction(self, kwargs: dict[str, Any]) -> None:
        expected = _sdk_error(**kwargs)
        with pytest.raises(type(expected)) as info:
            ReconnectPolicy(**kwargs)
        assert type(info.value) is type(expected)
        assert str(info.value) == str(expected)

    def test_frozen(self) -> None:
        policy = ReconnectPolicy()
        with pytest.raises(dataclasses.FrozenInstanceError):
            policy.attempts = 1  # type: ignore[misc]

    def test_equality_and_hash_by_value(self) -> None:
        assert ReconnectPolicy(delay=0.05) == ReconnectPolicy(delay=0.05)
        assert ReconnectPolicy(delay=0.05) != ReconnectPolicy(delay=0.1)
        assert hash(ReconnectPolicy(delay=0.05)) == hash(ReconnectPolicy(delay=0.05))

    def test_exported(self) -> None:
        assert surreal_orm_lite.ReconnectPolicy is ReconnectPolicy
        assert "ReconnectPolicy" in surreal_orm_lite.__all__
        assert _sdk.ReconnectOptions is ReconnectOptions
        assert "ReconnectOptions" in _sdk.__all__


class TestConnectionConfigFields:
    """``ConnectionConfig`` gains ``reconnect`` and ``renew_access``, appended with defaults: v0.29.0 code still works."""

    def test_v029_construction_still_works_and_the_new_fields_default_to_true(self) -> None:
        by_position = ConnectionConfig("mem://", None, None, "t", "t")
        by_keyword = ConnectionConfig(url="mem://", user=None, password=None, namespace="t", database="t")
        assert by_position == by_keyword
        assert by_position.reconnect is True
        assert by_position.renew_access is True

    def test_the_new_fields_are_appended(self) -> None:
        fields = dataclasses.fields(ConnectionConfig)
        assert [field.name for field in fields][5:] == ["reconnect", "renew_access"]
        assert [field.default for field in fields][5:] == [True, True]

    def test_the_new_fields_count_for_equality(self) -> None:
        policy = ReconnectPolicy(attempts=None)
        config = ConnectionConfig("ws://db:8000/rpc", "root", "secret", "shop", "main", policy, False)
        assert config.reconnect == policy
        assert config.renew_access is False
        assert config != ConnectionConfig("ws://db:8000/rpc", "root", "secret", "shop", "main")
        assert "secret" not in repr(config)


@pytest.mark.usefixtures("unset_default")
class TestConnectionSettings:
    """``reconnect=``/``renew_access=`` per connection, validated before anything is stored, and the settings epoch."""

    def test_set_connection_defaults(self) -> None:
        SurrealDBConnectionManager.set_connection(_UNREACHABLE, "u", "p", "t", "d")
        config = SurrealDBConnectionManager.get_config()
        assert config is not None
        assert config.reconnect is True
        assert config.renew_access is True

    def test_set_connection_stores_them(self) -> None:
        policy = ReconnectPolicy(delay=0.05, max_delay=0.2)
        SurrealDBConnectionManager.set_connection(_UNREACHABLE, "u", "p", "t", "d", reconnect=policy, renew_access=False)
        assert SurrealDBConnectionManager.get_config() == ConnectionConfig(_UNREACHABLE, "u", "p", "t", "d", policy, False)
        SurrealDBConnectionManager.set_connection(_UNREACHABLE, "u", "p", "t", "d", reconnect=False)
        config = SurrealDBConnectionManager.get_config()
        assert config is not None
        assert config.reconnect is False
        # set_connection() gives every setting it does not name its default again.
        assert config.renew_access is True

    def test_add_connection_stores_them(self) -> None:
        policy = ReconnectPolicy(attempts=None)
        SurrealDBConnectionManager.add_connection(
            "rs_a", url=_UNREACHABLE, user="u", password="p", namespace="t", database="a", reconnect=policy, renew_access=False
        )
        assert SurrealDBConnectionManager.get_config("rs_a") == ConnectionConfig(
            _UNREACHABLE, "u", "p", "t", "a", policy, False
        )
        SurrealDBConnectionManager.add_connection("rs_b", url="mem://", namespace="t", database="b")
        config = SurrealDBConnectionManager.get_config("rs_b")
        assert config is not None
        assert (config.reconnect, config.renew_access) == (True, True)

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"reconnect": "yes"}, r"^reconnect must be True, False or a ReconnectPolicy, not str\.$"),
            ({"reconnect": 1}, r"^reconnect must be True, False or a ReconnectPolicy, not int\.$"),
            ({"reconnect": None}, r"^reconnect must be True, False or a ReconnectPolicy, not NoneType\.$"),
            (
                {"reconnect": ReconnectOptions()},
                r"^reconnect must be True, False or a ReconnectPolicy, not ReconnectOptions\.$",
            ),
            ({"renew_access": 1}, r"^renew_access must be True or False, not int\.$"),
            ({"renew_access": "no"}, r"^renew_access must be True or False, not str\.$"),
        ],
        ids=repr,
    )
    def test_a_bad_type_raises_before_anything_is_stored(self, kwargs: dict[str, Any], message: str) -> None:
        SurrealDBConnectionManager.set_connection(_UNREACHABLE, "u", "p", "t", "d", renew_access=False)
        SurrealDBConnectionManager.add_connection("rs_a", url=_UNREACHABLE, user="u", password="p", namespace="t", database="a")
        default = default_connection_class()
        conn = SurrealDBConnectionManager._manager_for("rs_a")
        default_before = (SurrealDBConnectionManager.get_config(), default._settings_epoch())
        conn_before = (SurrealDBConnectionManager.get_config("rs_a"), conn._settings_epoch(), conn._settings_generation())

        with pytest.raises(TypeError, match=message):
            SurrealDBConnectionManager.set_connection("mem://", None, None, "x", "y", **kwargs)
        with pytest.raises(TypeError, match=message):
            SurrealDBConnectionManager.add_connection("rs_a", url="mem://", namespace="x", database="y", **kwargs)
        with pytest.raises(TypeError, match=message):
            SurrealDBConnectionManager.add_connection("rs_new", url="mem://", namespace="x", database="y", **kwargs)

        assert (SurrealDBConnectionManager.get_config(), default._settings_epoch()) == default_before
        assert (
            SurrealDBConnectionManager.get_config("rs_a"),
            conn._settings_epoch(),
            conn._settings_generation(),
        ) == conn_before
        assert "rs_new" not in connection_manager._REGISTRY

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ({}, {"reconnect": ReconnectPolicy(delay=0.05)}),
            ({"reconnect": ReconnectPolicy(delay=0.05)}, {"reconnect": ReconnectPolicy(delay=0.1)}),
            ({"reconnect": ReconnectPolicy()}, {"reconnect": False}),
            ({}, {"renew_access": False}),
        ],
        ids=repr,
    )
    async def test_re_registering_with_other_resilience_settings_retires_the_clients(
        self, first: dict[str, Any], second: dict[str, Any]
    ) -> None:
        loop = asyncio.get_running_loop()
        settings: dict[str, Any] = {"url": _UNREACHABLE, "user": "u", "password": "p", "namespace": "t", "database": "a"}
        SurrealDBConnectionManager.add_connection("rs_a", **settings, **first)
        conn = SurrealDBConnectionManager._manager_for("rs_a")
        client = _FakeServerClient()
        vars(conn)[_P + "clients"][loop] = client
        generation = conn._settings_generation()

        # The same settings again (an equal policy is the same policy) change nothing.
        SurrealDBConnectionManager.add_connection("rs_a", **settings, **dict(first))
        assert conn._settings_generation() == generation
        assert vars(conn)[_P + "clients"].get(loop) is client

        SurrealDBConnectionManager.add_connection("rs_a", **settings, **second)
        assert SurrealDBConnectionManager._manager_for("rs_a") is conn
        assert conn._settings_generation() == generation + 1
        assert vars(conn)[_P + "clients"].get(loop) is None
        config = SurrealDBConnectionManager.get_config("rs_a")
        assert config is not None
        expected = {"reconnect": True, "renew_access": True, **second}
        assert (config.reconnect, config.renew_access) == (expected["reconnect"], expected["renew_access"])

    async def test_set_connection_keeps_its_tolerance(self) -> None:
        """Another policy through ``set_connection()`` retires nothing and bumps no generation (v0.28.0 tolerance)."""
        loop = asyncio.get_running_loop()
        SurrealDBConnectionManager.set_connection(_UNREACHABLE, "u", "p", "t", "d")
        default = default_connection_class()
        client = _FakeServerClient()
        vars(default)[_P + "clients"][loop] = client
        generation = default._settings_generation()
        SurrealDBConnectionManager.set_connection(_UNREACHABLE, "u", "p", "t", "d", reconnect=False, renew_access=False)
        assert default._settings_generation() == generation
        assert vars(default)[_P + "clients"].get(loop) is client
        vars(default)[_P + "clients"].pop(loop)

    def test_set_connection_bumps_the_settings_epoch(self) -> None:
        default = default_connection_class()
        epoch = default._settings_epoch()
        SurrealDBConnectionManager.set_connection(_UNREACHABLE, "u", "p", "t", "d")
        assert default._settings_epoch() == epoch + 1

    @pytest.mark.parametrize(
        ("setter", "value"),
        [
            ("set_url", "ws://127.0.0.1:10/rpc"),
            ("set_user", "v"),
            ("set_password", "q"),
            ("set_namespace", "n2"),
            ("set_database", "d2"),
        ],
    )
    @pytest.mark.parametrize("valid", [None, True, False], ids=["no-reconnect", "reconnect-valid", "reconnect-invalid"])
    async def test_every_setter_bumps_the_settings_epoch(
        self, monkeypatch: pytest.MonkeyPatch, setter: str, value: str, valid: bool | None
    ) -> None:
        SurrealDBConnectionManager.set_connection(_UNREACHABLE, "u", "p", "t", "d")
        default = default_connection_class()
        validations: list[int] = []

        async def validate_connection() -> bool:
            validations.append(default._settings_epoch())
            return bool(valid)

        monkeypatch.setattr(default, "validate_connection", validate_connection)
        epoch = default._settings_epoch()
        if valid is None:
            assert await getattr(SurrealDBConnectionManager, setter)(value) is True
            assert validations == []
        else:
            assert await getattr(SurrealDBConnectionManager, setter)(value, reconnect=True) is valid
            # The validation already runs under the new epoch.
            assert validations == [epoch + 1]
        assert default._settings_epoch() > epoch

    def test_settings_epochs_are_per_connection(self) -> None:
        SurrealDBConnectionManager.add_connection("rs_a", url="mem://", namespace="t", database="a")
        SurrealDBConnectionManager.add_connection("rs_b", url="mem://", namespace="t", database="b")
        a = SurrealDBConnectionManager._manager_for("rs_a")
        b = SurrealDBConnectionManager._manager_for("rs_b")
        before = b._settings_epoch()
        SurrealDBConnectionManager.add_connection("rs_a", url="mem://", namespace="t", database="a2")
        assert b._settings_epoch() == before
        assert a._settings_epoch() > 0

    def test_unset_connection_forgets_them(self) -> None:
        SurrealDBConnectionManager.add_connection(
            "rs_a", url=_UNREACHABLE, user="u", password="p", namespace="t", database="a", reconnect=False, renew_access=False
        )
        conn = SurrealDBConnectionManager._manager_for("rs_a")
        conn._clear_settings()
        assert vars(conn)[_P + "reconnect"] is True
        assert vars(conn)[_P + "renew_access"] is True


@pytest.mark.usefixtures("unset_default")
@pytest.mark.skipif(not _embedded_engine_available(), reason="the installed surrealdb SDK has no embedded engine")
class TestEmbeddedBlockKeepsThem:
    """``embedded_database()`` points the connection at an in-process engine for its block, then puts back the
    connection's reconnect policy and renewal setting with the rest of its settings."""

    async def test_the_block_puts_them_back(self) -> None:
        policy = ReconnectPolicy(delay=0.05, max_delay=0.2)
        SurrealDBConnectionManager.set_connection(_UNREACHABLE, "u", "p", "t", "d", reconnect=policy, renew_access=False)
        default = default_connection_class()
        async with embedded_database():
            config = SurrealDBConnectionManager.get_config()
            assert config is not None
            assert (config.reconnect, config.renew_access) == (True, True)
            inside = default._settings_epoch()
        assert SurrealDBConnectionManager.get_config() == ConnectionConfig(_UNREACHABLE, "u", "p", "t", "d", policy, False)
        # Putting the outer settings back is a settings change too.
        assert default._settings_epoch() > inside


class TestResilienceState:
    """The new per-connection state has a ``_STATE`` entry each, so no connection shares it with another."""

    _NEW = {
        "reconnect": bool,
        "renew_access": bool,
        "listeners": dict,
        "listener_lock": type(threading.Lock()),
        "settings_epoch": int,
        "session_lock": type(threading.Lock()),
        "renewing": type(None),
    }

    def test_state_has_the_new_entries(self) -> None:
        factories = dict(connection_manager._STATE)
        for key, kind in self._NEW.items():
            assert key in factories, key
            assert type(factories[key]()) is kind, key
        assert factories["reconnect"]() is True
        assert factories["renew_access"]() is True
        assert factories["settings_epoch"]() == 0

    def test_each_connection_gets_its_own(self) -> None:
        SurrealDBConnectionManager.add_connection("rs_a", url="mem://", namespace="t", database="a")
        a = SurrealDBConnectionManager._manager_for("rs_a")
        default = default_connection_class()
        for key in ("listeners", "listener_lock", "session_lock"):
            assert vars(a)[_P + key] is not vars(default)[_P + key], key
            assert vars(a)[_P + key] is not vars(SurrealDBConnectionManager)[_P + key], key
