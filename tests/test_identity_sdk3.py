"""SDK 3.0 (v0.50.0): no request of the ORM's ever runs under an identity nobody chose — client part.

SDK 3.0's WebSocket client opens a new socket for any request made once its socket is gone, and that socket is
anonymous: the ORM signs in itself and never hands the SDK credentials (measured, M26). A client the ORM no longer
serves — dropped, closed by ``close_connection()``, of a connection's previous settings — is **retired**: it refuses
every request with a ``SurrealDbConnectionError`` (not retryable) and opens no socket. That holds for every holder of
the client: a ``get_client()`` result the application kept, an interactive transaction begun before the drop, a live
stream started on it.

The SDK's token renewal is off on the ORM's clients: the SDK would spend the refresh token the ORM stored (they are
single use), and the application's ``signin(refresh=…)`` would then fail.

E2E tests run through :class:`tests._proxy.CuttableProxy`, whose connection count shows whether a client opened a
socket. Nothing here is version-gated except the 3.x/4.x features (interactive transactions, refresh tokens), which
skip on 2.x by capability.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import os
import textwrap
from typing import Any

import pytest

from surreal_orm_lite import SurrealDBConnectionManager, _client
from surreal_orm_lite._sdk import AsyncSurreal, Features, SdkAsyncWsConnection
from surreal_orm_lite.exceptions import SurrealDbConnectionError
from surreal_orm_lite.live import is_dead
from tests._proxy import CuttableProxy
from tests.test_auth import AUTH_TABLE, PASSWORD, REFRESH_ACCESS, _unique_email, auth_client
from tests.test_reconnect import Reconnected, _wait_dead, proxied

# ==================== The SDK layout retire() relies on (C7) ====================


def _ws_methods() -> dict[str, set[str]]:
    """For each method of ``AsyncWsSurrealConnection``, the names of the ``self.<name>(…)`` calls it makes."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(SdkAsyncWsConnection)))
    klass = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    methods: dict[str, set[str]] = {}
    for node in klass.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            calls = methods.setdefault(node.name, set())
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and isinstance(inner.func.value, ast.Name)
                    and inner.func.value.id == "self"
                ):
                    calls.add(inner.func.attr)
    return methods


def _reached_from(start: str, methods: dict[str, set[str]]) -> set[str]:
    """The transitive closure of the ``self.<m>()`` calls made from *start*."""
    reached: set[str] = set()
    todo = [start]
    while todo:
        for name in methods.get(todo.pop(), set()):
            if name not in reached:
                reached.add(name)
                todo.append(name)
    return reached


class TestSdkLayout:
    """What ``_client.retire()`` relies on in the SDK's WebSocket client: every request path goes through
    ``connect()``, and ``close()`` never does."""

    def test_the_request_paths_call_connect(self) -> None:
        for method in ("_send", "refresh"):
            assert "connect" in getattr(SdkAsyncWsConnection, method).__code__.co_names, method
        methods = _ws_methods()
        for method in ("kill", "query_raw", "begin", "commit", "cancel", "signin", "use", "authenticate"):
            assert "connect" in _reached_from(method, methods), method

    def test_only_connect_opens_a_socket(self) -> None:
        methods = _ws_methods()
        openers = {name for name, calls in methods.items() if "_open_socket" in calls}
        assert openers == {"_connect_locked", "_reopen"}

    def test_close_never_reaches_connect(self) -> None:
        reached = _reached_from("close", _ws_methods())
        assert not reached & {"connect", "_connect_locked", "_open_socket", "_reopen"}, (
            "SDK close() now reconnects: retire() must close before installing the connect override"
        )


# ==================== retire() ====================


def _unconnected_ws_client() -> Any:
    """A real SDK WebSocket client, never connected, on a port nothing listens on."""
    return AsyncSurreal("ws://127.0.0.1:9/rpc", reconnect=False, streaming=False, renew_access=False, timeout=None)


class TestRetireUnit:
    @pytest.mark.asyncio
    async def test_a_retired_client_refuses_every_request_and_never_connects(self) -> None:
        client = _unconnected_ws_client()

        await _client.retire(client, "retired by the test")

        for request in (client.query("RETURN 1"), client.query_raw("RETURN 1"), client.connect(), client.version()):
            with pytest.raises(SurrealDbConnectionError, match="retired by the test") as caught:
                await request
            assert caught.value.retryable is False  # type: ignore[attr-defined]
        assert client.socket is None
        assert _client.is_retired(client)
        assert is_dead(client)

    @pytest.mark.asyncio
    async def test_the_retirement_s_own_close_raises_nothing(self) -> None:
        client = _unconnected_ws_client()
        outcomes: list[BaseException | None] = []
        close = client.close

        async def _recording_close() -> None:
            try:
                await close()
            except BaseException as exc:
                outcomes.append(exc)
                raise
            outcomes.append(None)

        client.close = _recording_close

        await _client.retire(client, "retired")

        assert outcomes == [None]

    @pytest.mark.asyncio
    async def test_the_first_reason_stays(self) -> None:
        client = _unconnected_ws_client()
        _client.refuse(client, "first")
        await _client.retire(client, "second")

        with pytest.raises(SurrealDbConnectionError, match="first"):
            await client.query("RETURN 1")

    @pytest.mark.asyncio
    async def test_an_http_client_is_closed_but_not_refused(self) -> None:
        """M33: an HTTP client holds no socket and sends the token it has; nothing to refuse."""
        client = AsyncSurreal("http://127.0.0.1:9", reconnect=False, streaming=False, renew_access=False, timeout=30.0)

        await _client.retire(client, "retired")

        assert not _client.is_retired(client)


# ==================== Retired clients, end to end (AC-18) ====================


class TestRetiredClientsE2E:
    @pytest.mark.asyncio
    async def test_a_kept_client_refuses_its_next_request_after_a_drop(self) -> None:
        async with proxied() as proxy:
            kept = await SurrealDBConnectionManager.get_client()
            proxy.cut()
            await _wait_dead(kept)
            opened = proxy.connections

            with pytest.raises(SurrealDbConnectionError) as caught:
                await kept.query("RETURN 1")

            assert caught.value.retryable is False  # type: ignore[attr-defined]
            assert proxy.connections == opened
            # The ORM itself carries on, on a new connection of its own.
            assert await Reconnected.objects().all() == []
            assert proxy.connections == opened + 1
            with pytest.raises(SurrealDbConnectionError):
                await kept.query("RETURN 1")
            assert proxy.connections == opened + 1

    @pytest.mark.asyncio
    async def test_a_kept_client_refuses_after_close_connection(self) -> None:
        async with proxied() as proxy:
            kept = await SurrealDBConnectionManager.get_client()
            await SurrealDBConnectionManager.close_connection()
            opened = proxy.connections

            with pytest.raises(SurrealDbConnectionError, match=r"close_connection\(\)") as caught:
                await kept.query("RETURN 1")

            assert caught.value.retryable is False  # type: ignore[attr-defined]
            assert proxy.connections == opened

    @pytest.mark.asyncio
    async def test_a_kept_client_of_a_removed_connection_refuses(self) -> None:
        async with CuttableProxy() as proxy:
            SurrealDBConnectionManager.add_connection(
                "retired_named", url=proxy.url(), user="root", password="root", namespace="ns", database="db"
            )
            kept = await SurrealDBConnectionManager.get_client("retired_named")
            await SurrealDBConnectionManager.remove_connection("retired_named")
            # The close runs in the background on this loop.
            await asyncio.sleep(0.05)
            opened = proxy.connections

            with pytest.raises(SurrealDbConnectionError, match="retired_named") as caught:
                await kept.query("RETURN 1")

            assert caught.value.retryable is False  # type: ignore[attr-defined]
            assert proxy.connections == opened

    @pytest.mark.asyncio
    async def test_an_interactive_transaction_begun_before_a_drop_refuses(self) -> None:
        async with proxied() as proxy:
            client = await SurrealDBConnectionManager.get_client()
            if not client.is_feature_supported(Features.TRANSACTIONS):
                pytest.skip("interactive transactions need SurrealDB 3.x or later")
            opened = 0
            with pytest.raises(SurrealDbConnectionError) as caught:
                async with SurrealDBConnectionManager.transaction() as tx:
                    assert tx.is_interactive
                    await Reconnected(id="before", name="a").save(tx=tx)
                    proxy.cut()
                    await _wait_dead(client)
                    opened = proxy.connections
                    await Reconnected(id="after", name="b").save(tx=tx)

            assert caught.value.retryable is False  # type: ignore[attr-defined]
            assert proxy.connections == opened
            assert await Reconnected.objects().all() == []

    @pytest.mark.asyncio
    async def test_a_kept_live_stream_opens_no_socket_after_a_drop(self) -> None:
        async with proxied() as proxy:
            stream = Reconnected.objects().live(auto_resubscribe=False)
            await stream.start()
            client = await SurrealDBConnectionManager.get_client()
            proxy.cut()
            await _wait_dead(client)
            opened = proxy.connections

            await stream.stop()

            assert proxy.connections == opened
            assert not stream.is_active

    @pytest.mark.asyncio
    async def test_a_kept_live_stream_opens_no_socket_after_close_connection(self) -> None:
        async with proxied() as proxy:
            stream = Reconnected.objects().live(auto_resubscribe=False)
            await stream.start()
            await SurrealDBConnectionManager.close_connection()
            opened = proxy.connections

            await stream.stop()

            assert proxy.connections == opened
            assert not stream.is_active


def _http_url() -> str:
    return f"http://{os.environ.get('SURREALDB_HOST', 'localhost')}:{os.environ.get('SURREALDB_PORT', '8000')}"


class TestHttpClientAfterTeardownE2E:
    @pytest.mark.asyncio
    async def test_a_kept_http_client_keeps_its_own_identity(self) -> None:
        """M33 (SDK 3.0.0): ``close()`` of an HTTP client closes its pooled session only; the client keeps its token,
        namespace and database. A reference kept after ``close_connection()`` sends as the identity it had — never as
        another one — so the ORM does not retire HTTP clients, as with SDK 2.0."""
        SurrealDBConnectionManager.set_connection(url=_http_url(), user="root", password="root", namespace="ns", database="db")
        kept = await SurrealDBConnectionManager.get_client()
        before = await _client.query(kept, "RETURN [$session.ns, $session.db, $token.ID]")

        await SurrealDBConnectionManager.close_connection()

        assert not _client.is_retired(kept)
        assert await _client.query(kept, "RETURN [$session.ns, $session.db, $token.ID]") == before
        # Still the configured root user: a root-only statement is accepted.
        assert isinstance(await _client.query(kept, "INFO FOR DB"), dict)


# ==================== Token renewal is off (AC-19) ====================

#: A refreshable access method whose access token lives 3 seconds: SDK 3.0 would renew it ≈ 2.7 s after signin.
_SHORT_REFRESH_ACCESS = f"""
DEFINE ACCESS OVERWRITE {REFRESH_ACCESS} ON DATABASE TYPE RECORD
  SIGNUP ( CREATE {AUTH_TABLE} SET email = $email, pass = crypto::argon2::generate($pass) )
  SIGNIN ( SELECT * FROM {AUTH_TABLE} WHERE email = $email AND crypto::argon2::compare(pass, $pass) )
  WITH REFRESH DURATION FOR TOKEN 3s, FOR SESSION 12h, FOR GRANT 30d;
"""


class TestRefreshTokenRenewalE2E:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("scheme", ["ws", "http"])
    async def test_a_stored_refresh_token_is_still_valid_past_the_renewal_point(self, scheme: str) -> None:
        async with auth_client(scheme=scheme) as client:
            if not client.is_feature_supported(Features.REFRESH_TOKENS):
                pytest.skip("refresh tokens need SurrealDB 3.x or later")
            await _client.query(client, _SHORT_REFRESH_ACCESS)
            email = _unique_email()
            await SurrealDBConnectionManager.signup(access=REFRESH_ACCESS, variables={"email": email, "pass": PASSWORD})
            tokens = await SurrealDBConnectionManager.signin(
                access=REFRESH_ACCESS, variables={"email": email, "pass": PASSWORD}
            )
            stored = SurrealDBConnectionManager.get_refresh_token()
            assert stored is not None and stored == tokens.refresh

            await asyncio.sleep(3.5)
            # The HTTP client renews before a request, when its token is due: make one (its token has expired, so it
            # may be refused — what matters is what happened to the refresh token).
            with contextlib.suppress(Exception):
                await _client.query(client, "RETURN 1")

            renewed = await SurrealDBConnectionManager.signin(access=REFRESH_ACCESS, refresh=stored)

            assert renewed.access
            assert renewed.refresh and renewed.refresh != stored
