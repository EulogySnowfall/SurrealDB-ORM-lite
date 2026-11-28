"""Connection resilience (v0.51.0): the reconnect policy and the per-connection resilience settings.

Unit tests (no server): :class:`~surreal_orm_lite.ReconnectPolicy` and its conversion to the SDK's
``ReconnectOptions``, the ``reconnect=``/``renew_access=`` settings of ``set_connection()`` and ``add_connection()``,
:class:`~surreal_orm_lite.ConnectionConfig`'s new fields and the settings epoch every setter bumps; the identity key
of a token (its claims, so a renewal is not an identity change), the identity stamp, the compare-and-set of the stored
session with its ``"auth"`` event, and the single renewal of the stored refresh token across event loops; the
attributes a connection error declares (``retryable``, ``may_have_run``, ``tokens``) and how the frontier sets
``may_have_run`` from what it knows of the request.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import contextlib
import dataclasses
import json
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from surrealdb import ReconnectOptions

import surreal_orm_lite
from surreal_orm_lite import (
    ConnectionConfig,
    ReconnectPolicy,
    SurrealDBConnectionManager,
    _client,
    _sdk,
    connection_manager,
    resilience,
)
from surreal_orm_lite.auth import AuthTokens
from surreal_orm_lite.exceptions import SurrealDbAuthenticationError, SurrealDbConnectionError
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


# ==================== Identity key, stamp, stored session, renewal (Task 2) ====================


def _jwt(**claims: Any) -> str:
    """An unsigned JWT-shaped token carrying *claims*: the identity key reads claims without verifying anything."""

    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'HS512', 'typ': 'JWT'})}.{part(claims)}.c2lnbmF0dXJl"


#: The claims SurrealDB puts in a record user's token (measured): the identity, then the token's own.
_RECORD = {"NS": "ns", "DB": "db", "AC": "account", "ID": "user:alice", "iss": "SurrealDB"}


def _record_token(**changes: Any) -> str:
    claims = {**_RECORD, "exp": 2000, "iat": 1000, "nbf": 1000, "jti": "first"}
    claims.update(changes)
    return _jwt(**claims)


class _Stampable:
    """An object the manager can stamp (weakly referenced) and nothing talks to."""


class TestIdentityKey:
    """A token's identity is its claims without the ones that change at every issue (``exp``, ``iat``, ``nbf``,
    ``jti``): a renewed token of the same user keys the same, another user, access method, namespace or database
    does not."""

    def test_a_renewed_token_keys_the_same(self) -> None:
        first = _record_token()
        renewed = _record_token(exp=9000, iat=8000, nbf=8000, jti="second")
        assert first != renewed
        assert resilience.identity_key(first) == resilience.identity_key(renewed) == _RECORD
        assert resilience.key_hash(resilience.identity_key(first)) == resilience.key_hash(resilience.identity_key(renewed))

    @pytest.mark.parametrize("claim", ["ID", "AC", "NS", "DB"])
    def test_another_identity_keys_differently(self, claim: str) -> None:
        other = _record_token(**{claim: "other"})
        assert resilience.identity_key(other) != resilience.identity_key(_record_token())
        assert resilience.key_hash(resilience.identity_key(other)) != resilience.key_hash(
            resilience.identity_key(_record_token())
        )

    @pytest.mark.parametrize(
        "token",
        [
            "not-a-token",
            "a.b",
            "a.!!!.c",
            f"x.{base64.urlsafe_b64encode(b'[1, 2]').decode()}.y",
            f"x.{base64.urlsafe_b64encode(b'not json').decode()}.y",
            f"x.{base64.urlsafe_b64encode(bytes([0xFF, 0xFE])).decode()}.y",
        ],
    )
    def test_an_unreadable_token_keys_as_itself(self, token: str) -> None:
        assert resilience.identity_key(token) == token

    def test_the_hash_tells_a_token_from_claims_that_spell_it(self) -> None:
        assert resilience.key_hash("x") != resilience.key_hash({"x": None})
        # Canonical: the order the claims came in does not matter.
        assert resilience.key_hash({"a": 1, "b": 2}) == resilience.key_hash({"b": 2, "a": 1})

    def test_the_fingerprint_keeps_across_a_renewal(self) -> None:
        fingerprint = SurrealDBConnectionManager._fingerprint
        settings = ("ws://db/rpc", "ns", "db", "root")
        first = fingerprint(*settings, _record_token())
        assert first == fingerprint(*settings, _record_token(exp=9000, iat=8000, nbf=8000, jti="second"))
        assert first != fingerprint(*settings, _record_token(ID="user:bob"))
        assert first != fingerprint(*settings, None)
        assert first != fingerprint("ws://other/rpc", "ns", "db", "root", _record_token())


@pytest.fixture
def no_session() -> Iterator[None]:
    """The default connection's stored session and ``"auth"`` listeners empty for the test, and again after it."""
    default = default_connection_class()
    listeners: dict[str, list[Any]] = vars(default)[_P + "listeners"]
    saved = dict(listeners)
    listeners.clear()
    SurrealDBConnectionManager.clear_session()
    yield
    SurrealDBConnectionManager.clear_session()
    listeners.clear()
    listeners.update(saved)
    setattr(default, _P + "renewing", None)


def _auth_events() -> list[Any]:
    """Register a listener for the default connection's ``"auth"`` event; the list it appends the tokens to."""
    seen: list[Any] = []
    vars(default_connection_class())[_P + "listeners"].setdefault("auth", []).append(seen.append)
    return seen


def _stored() -> AuthTokens | None:
    access = SurrealDBConnectionManager.get_session_token()
    return None if access is None else AuthTokens(access, SurrealDBConnectionManager.get_refresh_token())


@pytest.mark.usefixtures("no_session")
class TestIdentityStamp:
    """What the manager records when it sets a client's identity: who, whether a reconnect restores it, what it put
    on the client, and a serial that is new at every stamp."""

    def test_it_still_indexes_as_fingerprint_then_replayable(self) -> None:
        client = _Stampable()
        SurrealDBConnectionManager._stamp_identity(client, replayable=False, fingerprint="fp")
        stamp = SurrealDBConnectionManager._identity_of(client)
        assert isinstance(stamp, resilience.IdentityStamp)
        assert (stamp[0], stamp[1]) == ("fp", False)
        assert (stamp.fingerprint, stamp.replayable, stamp.kind) == ("fp", False, "unstored")

    def test_every_stamp_has_a_new_serial(self) -> None:
        client = _Stampable()
        SurrealDBConnectionManager._stamp_identity(client, replayable=True, fingerprint="fp")
        first = SurrealDBConnectionManager._identity_of(client)
        SurrealDBConnectionManager._stamp_identity(client, replayable=True, fingerprint="fp")
        second = SurrealDBConnectionManager._identity_of(client)
        assert first is not None and second is not None
        assert first.serial != second.serial
        assert first != second
        assert first.same_identity(second)

    def test_the_kind_is_what_the_manager_put_on_the_client(self) -> None:
        client = _Stampable()
        SurrealDBConnectionManager._stamp_identity(client, replayable=True)
        assert getattr(SurrealDBConnectionManager._identity_of(client), "kind", None) == "configured"
        SurrealDBConnectionManager._set_stored(AuthTokens(_record_token()))
        SurrealDBConnectionManager._stamp_identity(client, replayable=True)
        assert getattr(SurrealDBConnectionManager._identity_of(client), "kind", None) == "stored"
        SurrealDBConnectionManager._stamp_identity(client, replayable=False)
        assert getattr(SurrealDBConnectionManager._identity_of(client), "kind", None) == "unstored"
        SurrealDBConnectionManager._stamp_identity(client, replayable=True, fingerprint="fp", kind="configured")
        assert getattr(SurrealDBConnectionManager._identity_of(client), "kind", None) == "configured"

    def test_same_identity_reads_the_fingerprint_and_replayable_only(self) -> None:
        stamp = resilience.IdentityStamp.new("fp", True, "stored")
        assert stamp.same_identity(resilience.IdentityStamp.new("fp", True, "configured"))
        assert not stamp.same_identity(resilience.IdentityStamp.new("other", True, "stored"))
        assert not stamp.same_identity(resilience.IdentityStamp.new("fp", False, "stored"))
        assert not stamp.same_identity(None)


@pytest.mark.usefixtures("no_session")
class TestStoredSession:
    """Every write of the stored session goes through ``_set_stored``: compare-and-set, one ``"auth"`` event per
    change and none when nothing changed."""

    def test_an_unconditional_set_stores_and_announces(self) -> None:
        events = _auth_events()
        pair = AuthTokens("access", "refresh")
        assert SurrealDBConnectionManager._set_stored(pair) is True
        assert _stored() == pair
        assert events == [pair]
        assert SurrealDBConnectionManager._set_stored(pair) is True
        assert events == [pair]  # nothing changed: nothing announced

    def test_a_compare_and_set_replaces_only_the_expected_pair(self) -> None:
        events = _auth_events()
        first, second, third = AuthTokens("a1", "r1"), AuthTokens("a2", "r2"), AuthTokens("a3", "r3")
        assert SurrealDBConnectionManager._set_stored(first, expected=None) is True
        assert SurrealDBConnectionManager._set_stored(third, expected=second) is False
        assert _stored() == first
        assert SurrealDBConnectionManager._set_stored(second, expected=first) is True
        assert _stored() == second
        assert events == [first, second]

    def test_forgetting_announces_none_once(self) -> None:
        events = _auth_events()
        SurrealDBConnectionManager._set_stored(AuthTokens("a", "r"))
        SurrealDBConnectionManager.clear_session()
        SurrealDBConnectionManager.clear_session()
        assert _stored() is None
        assert events == [AuthTokens("a", "r"), None]

    def test_only_set_stored_writes_the_two_fields(self) -> None:
        source = Path(connection_manager.__file__).read_text(encoding="utf-8")
        writers = set()
        for function in ast.walk(ast.parse(source)):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
                if not isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                    continue
                for target in targets:
                    for leaf in ast.walk(target) if target is not None else ():
                        if isinstance(leaf, ast.Attribute) and leaf.attr in {"__session_token", "__refresh_token"}:
                            writers.add(function.name)
        assert writers == {"_set_stored"}

    async def test_a_listener_that_raises_is_logged_and_the_others_still_run(self, caplog: pytest.LogCaptureFixture) -> None:
        listeners = vars(default_connection_class())[_P + "listeners"].setdefault("auth", [])

        def broken(tokens: Any) -> None:
            raise RuntimeError("listener bug")

        listeners.append(broken)
        events = _auth_events()
        with caplog.at_level("ERROR", logger="surreal_orm_lite.connection_manager"):
            SurrealDBConnectionManager._set_stored(AuthTokens("a"))
        assert events == [AuthTokens("a")]
        assert "listener bug" in caplog.text

    async def test_a_coroutine_listener_runs_as_a_task_of_the_loop(self) -> None:
        seen: list[Any] = []
        done = asyncio.Event()

        async def listener(tokens: Any) -> None:
            seen.append(tokens)
            done.set()

        vars(default_connection_class())[_P + "listeners"].setdefault("auth", []).append(listener)
        SurrealDBConnectionManager._set_stored(AuthTokens("a"))
        assert seen == []  # scheduled, not awaited by the writer
        async with asyncio.timeout(2):
            await done.wait()
        assert seen == [AuthTokens("a")]

    def test_a_coroutine_listener_without_a_loop_is_dropped_quietly(self, caplog: pytest.LogCaptureFixture) -> None:
        async def listener(tokens: Any) -> None:
            raise AssertionError("never run")

        vars(default_connection_class())[_P + "listeners"].setdefault("auth", []).append(listener)
        with caplog.at_level("WARNING", logger="surreal_orm_lite.connection_manager"):
            SurrealDBConnectionManager._set_stored(AuthTokens("a"))
        assert "no running event loop" in caplog.text


class _FakeRefresher:
    """The short-lived client ``_renew_stored`` exchanges the refresh token on: counts the exchanges, waits for
    ``release`` when asked to, then answers ``answer`` (a pair) or raises it (an exception)."""

    def __init__(self, answer: Any, *, hold: bool = False, before_answer: Any = None) -> None:
        self.answer = answer
        self.calls: list[Any] = []
        self.started = threading.Event()
        self.release = threading.Event()
        if not hold:
            self.release.set()
        self.before_answer = before_answer
        self.lock = threading.Lock()

    async def refresh(self, tokens: Any) -> Any:
        with self.lock:
            self.calls.append(tokens)
        self.started.set()
        while not self.release.is_set():
            await asyncio.sleep(0.005)
        if self.before_answer is not None:
            self.before_answer()
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


@pytest.fixture
def fake_refresher(monkeypatch: pytest.MonkeyPatch, no_session: None) -> Any:
    """Install a :class:`_FakeRefresher` as the default connection's short-lived client; returns a setter."""
    default = default_connection_class()

    def install(refresher: _FakeRefresher, *, opening: BaseException | None = None) -> _FakeRefresher:
        @contextlib.asynccontextmanager
        async def ephemeral(cls: Any) -> AsyncIterator[Any]:
            if opening is not None:
                raise opening
            yield refresher

        monkeypatch.setattr(default, "ephemeral_client", classmethod(ephemeral))
        return refresher

    return install


_SPENT = AuthTokens("spent-access", "spent-refresh")
_NEW = AuthTokens("new-access", "new-refresh")


def _renewing() -> Any:
    return vars(default_connection_class())[_P + "renewing"]


class TestRenewStored:
    """The stored refresh token has one owner: ``_renew_stored`` spends it once per connection, whatever the number
    of callers and event loops, and stores the outcome with a compare-and-set."""

    async def test_concurrent_callers_on_two_loops_share_one_exchange(self, fake_refresher: Any) -> None:
        refresher = fake_refresher(_FakeRefresher(_sdk.Tokens(_NEW.access, _NEW.refresh), hold=True))
        events = _auth_events()
        SurrealDBConnectionManager._set_stored(_SPENT)
        first = asyncio.ensure_future(SurrealDBConnectionManager._renew_stored(_SPENT))
        await asyncio.to_thread(refresher.started.wait, 5)
        second = asyncio.ensure_future(SurrealDBConnectionManager._renew_stored(_SPENT))
        other_loop: list[Any] = []

        def on_another_loop() -> None:
            try:
                other_loop.append(asyncio.run(SurrealDBConnectionManager._renew_stored(_SPENT)))
            except BaseException as exc:  # reported by the assertion below
                other_loop.append(exc)

        thread = threading.Thread(target=on_another_loop)
        thread.start()
        pending = _renewing()
        assert pending is not None
        async with asyncio.timeout(5):
            # Both waiters (this loop's second call, the other loop's call) wait on the renewal in progress.
            while len(getattr(pending, "_done_callbacks", ())) < 2:
                await asyncio.sleep(0.005)
        refresher.release.set()
        results = [await first, await second]
        await asyncio.to_thread(thread.join, 5)

        assert results + other_loop == [_NEW, _NEW, _NEW]
        assert len(refresher.calls) == 1
        assert (refresher.calls[0].access, refresher.calls[0].refresh) == (_SPENT.access, _SPENT.refresh)
        assert _stored() == _NEW
        assert events == [_SPENT, _NEW]
        assert _renewing() is None

    async def test_a_pair_already_renewed_is_returned_without_an_exchange(self, fake_refresher: Any) -> None:
        refresher = fake_refresher(_FakeRefresher(AssertionError("no exchange")))
        SurrealDBConnectionManager._set_stored(_NEW)
        assert await SurrealDBConnectionManager._renew_stored(_SPENT) == _NEW
        assert refresher.calls == []

    @pytest.mark.parametrize("stored", [None, AuthTokens("spent-access")])
    async def test_a_forgotten_pair_raises_without_an_exchange(self, fake_refresher: Any, stored: Any) -> None:
        refresher = fake_refresher(_FakeRefresher(AssertionError("no exchange")))
        SurrealDBConnectionManager._set_stored(stored)
        with pytest.raises(SurrealDbAuthenticationError):
            await SurrealDBConnectionManager._renew_stored(_SPENT)
        assert refresher.calls == []
        assert _stored() == stored

    async def test_a_refusal_forgets_the_refresh_token(self, fake_refresher: Any) -> None:
        refused = _sdk.ServerError("NotAllowed", "There was a problem with authentication")
        fake_refresher(_FakeRefresher(refused))
        SurrealDBConnectionManager._set_stored(_SPENT)
        events = _auth_events()
        with pytest.raises(SurrealDbAuthenticationError) as caught:
            await SurrealDBConnectionManager._renew_stored(_SPENT)
        assert caught.value.__cause__ is refused
        assert _stored() == AuthTokens(_SPENT.access, None)
        assert events == [AuthTokens(_SPENT.access, None)]
        assert _renewing() is None

    async def test_a_refusal_after_another_rotation_keeps_the_rotated_pair(self, fake_refresher: Any) -> None:
        rotated = AuthTokens("rotated-access", "rotated-refresh")
        fake_refresher(
            _FakeRefresher(
                _sdk.ServerError("NotAllowed", "There was a problem with authentication"),
                before_answer=lambda: SurrealDBConnectionManager._set_stored(rotated),
            )
        )
        SurrealDBConnectionManager._set_stored(_SPENT)
        with pytest.raises(SurrealDbAuthenticationError):
            await SurrealDBConnectionManager._renew_stored(_SPENT)
        assert _stored() == rotated
        assert _renewing() is None

    @pytest.mark.parametrize("failure", ["exchange", "opening"])
    async def test_a_connection_failure_keeps_the_pair(self, fake_refresher: Any, failure: str) -> None:
        if failure == "exchange":
            fake_refresher(_FakeRefresher(_sdk.ConnectionUnavailableError("the connection is gone")))
        else:
            opening = SurrealDbConnectionError("Can't connect to the database.")
            fake_refresher(_FakeRefresher(AssertionError("no exchange")), opening=opening)
        SurrealDBConnectionManager._set_stored(_SPENT)
        events = _auth_events()
        with pytest.raises(SurrealDbConnectionError):
            await SurrealDBConnectionManager._renew_stored(_SPENT)
        assert _stored() == _SPENT
        assert events == []
        assert _renewing() is None

    async def test_a_server_without_refresh_tokens_raises_an_authentication_error(self, fake_refresher: Any) -> None:
        unavailable = _sdk.UnavailableFeatureError("refresh-tokens", "2.7.0", "3.0.0")
        fake_refresher(_FakeRefresher(unavailable))
        SurrealDBConnectionManager._set_stored(_SPENT)
        with pytest.raises(SurrealDbAuthenticationError) as caught:
            await SurrealDBConnectionManager._renew_stored(_SPENT)
        assert caught.value.__cause__ is unavailable
        assert _stored() == _SPENT
        assert _renewing() is None

    async def test_a_cancelled_owner_releases_the_waiters_and_keeps_the_pair(self, fake_refresher: Any) -> None:
        refresher = fake_refresher(_FakeRefresher(_sdk.Tokens(_NEW.access, _NEW.refresh), hold=True))
        SurrealDBConnectionManager._set_stored(_SPENT)
        owner = asyncio.ensure_future(SurrealDBConnectionManager._renew_stored(_SPENT))
        await asyncio.to_thread(refresher.started.wait, 5)
        waiter = asyncio.ensure_future(SurrealDBConnectionManager._renew_stored(_SPENT))
        await asyncio.sleep(0.05)
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        with pytest.raises(SurrealDbConnectionError):
            await waiter
        assert _stored() == _SPENT
        assert _renewing() is None

    async def test_a_cancelled_waiter_does_not_cancel_the_renewal(self, fake_refresher: Any) -> None:
        refresher = fake_refresher(_FakeRefresher(_sdk.Tokens(_NEW.access, _NEW.refresh), hold=True))
        SurrealDBConnectionManager._set_stored(_SPENT)
        owner = asyncio.ensure_future(SurrealDBConnectionManager._renew_stored(_SPENT))
        await asyncio.to_thread(refresher.started.wait, 5)
        waiter = asyncio.ensure_future(SurrealDBConnectionManager._renew_stored(_SPENT))
        await asyncio.sleep(0.05)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        refresher.release.set()
        assert await owner == _NEW
        assert _stored() == _NEW


# ==================== Connection errors: retryable, may_have_run, tokens ====================


class TestErrorAttributes:
    """The flags a caller reads on a connection or authentication error are declared on the classes, with defaults."""

    def test_a_connection_error_is_retryable_did_not_run_and_carries_no_tokens(self) -> None:
        error = SurrealDbConnectionError("the connection is gone")
        assert error.retryable is True
        assert error.may_have_run is False
        assert error.tokens is None

    @pytest.mark.parametrize("name", ["retryable", "may_have_run", "tokens"])
    def test_the_connection_error_class_declares_it(self, name: str) -> None:
        assert name in vars(SurrealDbConnectionError)

    def test_an_authentication_error_carries_no_tokens(self) -> None:
        assert SurrealDbAuthenticationError("refused").tokens is None
        assert "tokens" in vars(SurrealDbAuthenticationError)

    def test_an_instance_value_leaves_the_class_default(self) -> None:
        error = SurrealDbConnectionError("in flight")
        error.may_have_run = True
        error.retryable = False
        assert SurrealDbConnectionError("other").may_have_run is False
        assert SurrealDbConnectionError("other").retryable is True


class _WsClient:
    """A client the frontier does not know as HTTP: whether a request ran is what the call itself recorded."""


# Local classes named as aiohttp's "never connected" errors: the frontier matches them by name, in the cause's MRO.
class ClientConnectorError(OSError):
    pass


class ConnectionTimeoutError(TimeoutError):
    pass


class _RefusedConnection(ClientConnectorError):
    pass


class ServerDisconnectedError(OSError):
    pass


def _http_client() -> Any:
    """A real SDK HTTP client, never used to send anything."""
    return _sdk.SdkAsyncHttpConnection("http://127.0.0.1:9")


def _with_cause(error: BaseException, cause: BaseException | None) -> BaseException:
    error.__cause__ = cause
    return error


async def _failed(client: Any, error: BaseException, *, reached: bool = False) -> SurrealDbConnectionError:
    """The error a frontier call on *client* raises when the SDK raises *error*; *reached*: the request had been let
    through to the transport."""

    async def operation(_c: Any) -> None:
        if reached:
            _client._reached_transport.set(True)
        raise error

    with pytest.raises(SurrealDbConnectionError) as caught:
        await _client._sdk_call(client, operation)
    assert caught.value.__cause__ is error
    return caught.value


class TestMayHaveRun:
    """``may_have_run`` is ``True`` only for a request handed to the transport whose answer never came."""

    @pytest.mark.parametrize("reached", [True, False])
    async def test_an_unavailable_connection_follows_whether_the_request_reached_the_transport(self, reached: bool) -> None:
        error = await _failed(_WsClient(), _sdk.ConnectionUnavailableError("the connection is gone"), reached=reached)
        assert error.may_have_run is reached
        assert error.retryable is True

    @pytest.mark.parametrize("reached", [True, False])
    async def test_a_timeout_follows_whether_the_request_reached_the_transport(self, reached: bool) -> None:
        error = await _failed(_WsClient(), _sdk.TransportTimeoutError("timed out while querying"), reached=reached)
        assert error.may_have_run is reached
        assert error.retryable is False

    async def test_a_previous_call_that_reached_the_transport_does_not_count(self) -> None:
        _client._reached_transport.set(True)
        error = await _failed(_WsClient(), _sdk.ConnectionUnavailableError("the connection is gone"))
        assert error.may_have_run is False

    @pytest.mark.parametrize("http", [True, False], ids=["http", "ws"])
    async def test_an_http_401_did_not_run(self, http: bool) -> None:
        client = _http_client() if http else _WsClient()
        refused = _sdk.HttpStatusError(401, "The access method 'a' does not exist", "http://db/rpc")
        error = await _failed(client, refused, reached=True)
        assert error.may_have_run is False
        assert error.retryable is False

    async def test_an_unreadable_response_did_not_run(self) -> None:
        error = await _failed(_WsClient(), _sdk.UnexpectedResponseError("unreadable"), reached=True)
        assert error.may_have_run is False

    @pytest.mark.parametrize(
        "error_class", [_sdk.ConnectionUnavailableError, _sdk.TransportTimeoutError], ids=["unavailable", "timeout"]
    )
    @pytest.mark.parametrize(
        ("cause", "may_have_run"),
        [
            (ClientConnectorError("connect call failed"), False),
            (ConnectionTimeoutError("connection timeout"), False),
            (_RefusedConnection("refused"), False),
            (ServerDisconnectedError("server disconnected"), True),
            (TimeoutError("no answer"), True),
            (None, True),
        ],
        ids=["connector", "connect-timeout", "connector-subclass", "disconnected", "read-timeout", "no-cause"],
    )
    async def test_over_http_a_request_ran_unless_the_client_never_connected(
        self, error_class: type[BaseException], cause: BaseException | None, may_have_run: bool
    ) -> None:
        sdk_error = _with_cause(error_class("could not reach http://db/rpc"), cause)
        error = await _failed(_http_client(), sdk_error)
        assert error.may_have_run is may_have_run
        assert error.retryable is (error_class is _sdk.ConnectionUnavailableError)

    async def test_over_http_the_context_is_not_read(self) -> None:
        cause = ClientConnectorError("connect call failed")
        sdk_error = _with_cause(_sdk.ConnectionUnavailableError("could not reach http://db/rpc"), cause)
        error = await _failed(_http_client(), sdk_error, reached=True)
        assert error.may_have_run is False

    def test_connection_error_alone_says_nothing_ran(self) -> None:
        error = _client.connection_error(_sdk.ConnectionUnavailableError("the connection is gone"))
        assert error.may_have_run is False
        assert error.retryable is True
