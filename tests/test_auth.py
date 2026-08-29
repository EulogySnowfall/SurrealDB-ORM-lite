"""Tests for v0.16.0 connection-level authentication.

The pure helpers (token container, payload building, error normalisation) are unit-tested;
everything that talks to a server lives in a ``…E2E`` class and runs on both DB lines.

Two facts, measured on :8001 (3.2.4) and :8002 (2.6.5), shape these tests:

- The SDK raises a *different* exception type per DB line for the same failure (a wrong
  password is ``NotFoundError`` on 3.x and ``InternalError`` on 2.6.x), so every assertion is
  on the ORM's exception **type**, never on the server's wording.
- A duplicate signin identifier makes 2.6.x fail while 3.x picks a record, so every E2E row
  uses a ``uuid4``-derived identifier and the suite stays order-independent (issue #160).
"""

import contextlib
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest

from surreal_orm_lite import SurrealDBConnectionManager
from surreal_orm_lite.auth import AuthTokens, build_auth_payload, wrap_auth_error
from surreal_orm_lite.exceptions import SurrealDbAuthenticationError, SurrealDbError


class TestAuthTokens:
    def test_carries_the_access_and_refresh_tokens(self) -> None:
        tokens = AuthTokens("jwt-value", "surreal-refresh-value")
        assert tokens.access == "jwt-value"
        assert tokens.refresh == "surreal-refresh-value"

    def test_refresh_defaults_to_none(self) -> None:
        """2.6.x never issues one, and a 3.x access method without WITH REFRESH does not either."""
        assert AuthTokens("jwt-value").refresh is None

    def test_is_frozen(self) -> None:
        tokens = AuthTokens("jwt-value")
        with pytest.raises(Exception):  # noqa: B017 — FrozenInstanceError is not public API
            tokens.access = "other"  # type: ignore[misc]

    def test_repr_redacts_both_tokens(self) -> None:
        """A repr reaches logs, tracebacks and pytest assertion diffs — a JWT must not."""
        tokens = AuthTokens("jwt-value", "surreal-refresh-value")
        rendered = repr(tokens)
        assert "jwt-value" not in rendered
        assert "surreal-refresh-value" not in rendered
        assert "redacted" in rendered

    def test_repr_still_says_whether_a_refresh_token_is_present(self) -> None:
        """Redaction must not cost the one bit of information the repr is read for."""
        assert repr(AuthTokens("jwt-value")) != repr(AuthTokens("jwt-value", "surreal-refresh-value"))

    def test_str_does_not_hand_back_the_jwt(self) -> None:
        """No ``__str__`` shortcut: an f-string must never interpolate a token by accident."""
        tokens = AuthTokens("jwt-value")
        assert str(tokens) != "jwt-value"
        assert "jwt-value" not in f"{tokens}"


class TestBuildAuthPayloadRecordAccess:
    def test_fills_namespace_and_database_from_the_configured_connection(self) -> None:
        assert build_auth_payload(
            access="acct",
            variables={"email": "a@b.c"},
            default_namespace="ns",
            default_database="db",
        ) == {
            "namespace": "ns",
            "database": "db",
            "access": "acct",
            "variables": {"email": "a@b.c"},
        }

    def test_explicit_namespace_and_database_win(self) -> None:
        payload = build_auth_payload(
            access="acct",
            variables={"email": "a@b.c"},
            namespace="other_ns",
            database="other_db",
            default_namespace="ns",
            default_database="db",
        )
        assert payload["namespace"] == "other_ns"
        assert payload["database"] == "other_db"

    def test_requires_a_namespace_and_database_from_somewhere(self) -> None:
        """The server rejects a record signin without them, so fail before the round trip."""
        with pytest.raises(ValueError):
            build_auth_payload(access="acct", variables={"email": "a@b.c"})


class TestBuildAuthPayloadSystemUser:
    def test_does_not_inject_the_configured_namespace_or_database(self) -> None:
        """A root user is defined at neither level. Injecting ns/db would turn a root signin
        into a database-user signin, which fails against a server that has no such user."""
        assert build_auth_payload(
            username="root",
            password="root",
            default_namespace="ns",
            default_database="db",
        ) == {"username": "root", "password": "root"}

    def test_a_namespace_user_passes_its_namespace_explicitly(self) -> None:
        assert build_auth_payload(
            username="ns_user",
            password="pw",
            namespace="ns",
            default_namespace="ignored",
        ) == {"username": "ns_user", "password": "pw", "namespace": "ns"}

    def test_a_database_user_passes_both_levels_explicitly(self) -> None:
        assert build_auth_payload(
            username="db_user",
            password="pw",
            namespace="ns",
            database="db",
        ) == {"username": "db_user", "password": "pw", "namespace": "ns", "database": "db"}


class TestBuildAuthPayloadRefreshExchange:
    def test_puts_the_refresh_token_at_the_top_level(self) -> None:
        """Measured on 3.2.4: nesting it under ``variables`` fails with NotAllowedError."""
        assert build_auth_payload(
            access="acct",
            refresh="surreal-refresh-x",
            default_namespace="ns",
            default_database="db",
        ) == {
            "namespace": "ns",
            "database": "db",
            "access": "acct",
            "refresh": "surreal-refresh-x",
        }


class TestBuildAuthPayloadValidation:
    def test_rejects_no_credentials_at_all(self) -> None:
        with pytest.raises(ValueError):
            build_auth_payload(default_namespace="ns", default_database="db")

    def test_rejects_mixing_a_system_user_with_record_access(self) -> None:
        with pytest.raises(ValueError):
            build_auth_payload(
                username="root",
                password="root",
                access="acct",
                variables={"email": "a@b.c"},
                default_namespace="ns",
                default_database="db",
            )

    def test_rejects_an_access_method_with_neither_variables_nor_refresh(self) -> None:
        with pytest.raises(ValueError):
            build_auth_payload(access="acct", default_namespace="ns", default_database="db")

    def test_rejects_variables_together_with_refresh(self) -> None:
        """One proves an identity from scratch, the other renews it — never both."""
        with pytest.raises(ValueError):
            build_auth_payload(
                access="acct",
                variables={"email": "a@b.c"},
                refresh="surreal-refresh-x",
                default_namespace="ns",
                default_database="db",
            )

    def test_rejects_a_refresh_token_without_an_access_method(self) -> None:
        with pytest.raises(ValueError):
            build_auth_payload(refresh="surreal-refresh-x", default_namespace="ns", default_database="db")

    def test_rejects_variables_without_an_access_method(self) -> None:
        with pytest.raises(ValueError):
            build_auth_payload(variables={"email": "a@b.c"}, default_namespace="ns", default_database="db")

    @pytest.mark.parametrize(
        ("username", "password"),
        [("root", None), (None, "root")],
    )
    def test_rejects_half_a_system_user(self, username: str | None, password: str | None) -> None:
        with pytest.raises(ValueError):
            build_auth_payload(username=username, password=password)

    def test_rejects_a_database_without_a_namespace(self) -> None:
        """SurrealDB scopes a database inside a namespace; the pair is meaningless alone."""
        with pytest.raises(ValueError):
            build_auth_payload(username="db_user", password="pw", database="db")


class TestWrapAuthError:
    @pytest.mark.parametrize(
        "raised",
        [
            ValueError("Invalid schema for Cbor WS encoding for authenticate"),
            RuntimeError("There was a problem with the database: No record was returned"),
            SurrealDbError("already an ORM error"),
        ],
    )
    def test_every_failure_becomes_one_authentication_error(self, raised: Exception) -> None:
        """These calls do nothing but authenticate, so any failure is an auth failure. Matching
        on wording was rejected: the same wrong password is ``NotFoundError`` on 3.x and
        ``InternalError`` on 2.6.x, and a malformed token never reaches the server at all."""
        assert isinstance(wrap_auth_error(raised, "signin"), SurrealDbAuthenticationError)

    def test_names_the_operation_that_failed(self) -> None:
        assert "signup" in str(wrap_auth_error(RuntimeError("boom"), "signup"))

    def test_keeps_the_original_message(self) -> None:
        assert "boom" in str(wrap_auth_error(RuntimeError("boom"), "signin"))

    def test_is_caught_by_existing_surreal_db_error_handlers(self) -> None:
        with pytest.raises(SurrealDbError):
            raise wrap_auth_error(RuntimeError("boom"), "signin")


# ---------------------------------------------------------------------------
# E2E — everything below talks to a real server and runs on both DB lines.
# ---------------------------------------------------------------------------


def _url() -> str:
    host = os.environ.get("SURREALDB_HOST", "localhost")
    port = os.environ.get("SURREALDB_PORT", "8000")
    return f"ws://{host}:{port}/rpc"


def _connect() -> None:
    SurrealDBConnectionManager.set_connection(url=_url(), user="root", password="root", namespace="ns", database="db")


def _unique_email() -> str:
    """A fresh identifier per test row.

    Two records sharing the signin identifier make SurrealDB 2.6.x fail the signin outright
    ("No record was returned") while 3.x picks one — so a reused address would turn this suite
    order-dependent on one DB line only. Measured, not hypothetical.
    """
    return f"{uuid4().hex}@example.test"


AUTH_TABLE = "auth_user"
AUTH_ACCESS = "auth_acct"
PASSWORD = "s3cret-passphrase"

#: The record table grants the record user SELECT on itself. Without that permission `info()`
#: silently returns None on both DB lines — the single sharpest gotcha of this feature, and
#: the reason one test below asserts that None on purpose.
DEFINE_TABLE = f"DEFINE TABLE OVERWRITE {AUTH_TABLE} SCHEMALESS PERMISSIONS FOR select, update WHERE id = $auth.id;"

DEFINE_ACCESS = f"""
DEFINE ACCESS OVERWRITE {AUTH_ACCESS} ON DATABASE TYPE RECORD
  SIGNUP ( CREATE {AUTH_TABLE} SET email = $email, pass = crypto::argon2::generate($pass) )
  SIGNIN ( SELECT * FROM {AUTH_TABLE} WHERE email = $email AND crypto::argon2::compare(pass, $pass) )
  DURATION FOR TOKEN 15m, FOR SESSION 12h;
"""


@contextlib.asynccontextmanager
async def auth_client() -> AsyncIterator[Any]:
    """Connected ORM client with a record-access method defined, removed again afterwards.

    An async context manager rather than a pytest fixture: the SDK's WebSocket client is bound
    to the event loop that created it, and fixtures run in the module-scoped loop while tests
    get their own — so the client must be opened inside the test's loop.

    The teardown re-signs in as root *before* cleaning up, because a test leaves the shared
    session authenticated as a record user (or anonymous), and neither can drop a table. It
    then calls ``unset_connection()`` so no session token survives into the next test.
    """
    _connect()
    client = await SurrealDBConnectionManager.get_client()
    await client.query(DEFINE_TABLE, {})
    await client.query(DEFINE_ACCESS, {})
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            await client.signin({"username": "root", "password": "root"})
            await client.use("ns", "db")
        for statement in (f"REMOVE ACCESS {AUTH_ACCESS} ON DATABASE;", f"REMOVE TABLE {AUTH_TABLE};"):
            with contextlib.suppress(Exception):
                await client.query(statement, {})
        await SurrealDBConnectionManager.unset_connection()


async def _signup(email: str) -> AuthTokens:
    return await SurrealDBConnectionManager.signup(access=AUTH_ACCESS, variables={"email": email, "pass": PASSWORD})


class TestSignupE2E:
    @pytest.mark.asyncio
    async def test_creates_the_record_and_returns_an_access_token(self) -> None:
        async with auth_client() as client:
            email = _unique_email()
            tokens = await _signup(email)

            assert isinstance(tokens, AuthTokens)
            assert tokens.access, "the server must issue an access token"
            assert await client.query("RETURN $auth;", {}) is not None

    @pytest.mark.asyncio
    async def test_a_plain_access_method_issues_no_refresh_token(self) -> None:
        """Only `DEFINE ACCESS … WITH REFRESH` yields one, and only on 3.x."""
        async with auth_client():
            assert (await _signup(_unique_email())).refresh is None


class TestSigninE2E:
    @pytest.mark.asyncio
    async def test_authenticates_an_existing_record_user(self) -> None:
        async with auth_client() as client:
            email = _unique_email()
            await _signup(email)
            # Drop back to root so the signin below is what actually proves the identity.
            await SurrealDBConnectionManager.signin(username="root", password="root")

            tokens = await SurrealDBConnectionManager.signin(access=AUTH_ACCESS, variables={"email": email, "pass": PASSWORD})

            assert tokens.access
            assert await client.query("RETURN $auth.email;", {}) == email

    @pytest.mark.asyncio
    async def test_authenticates_a_system_user(self) -> None:
        """Proven by regained privilege, not by ``$auth``.

        See the test below: a system-user signin restores root's permissions but leaves the
        record identity sitting in the session, so ``$auth`` is the wrong thing to assert on.
        """
        async with auth_client() as client:
            await _signup(_unique_email())  # leaves the session as a record user
            with pytest.raises(Exception):  # noqa: B017 — per-line type, asserted below
                await client.query("DEFINE TABLE OVERWRITE root_only SCHEMALESS;", {})

            tokens = await SurrealDBConnectionManager.signin(username="root", password="root")

            assert tokens.access
            await client.query("DEFINE TABLE OVERWRITE root_only SCHEMALESS;", {})
            await client.query("REMOVE TABLE root_only;", {})

    @pytest.mark.asyncio
    async def test_a_system_user_signin_does_not_clear_the_record_identity(self) -> None:
        """Pins a surprising server behaviour, identical on 2.6.5 and 3.2.4.

        Signing in as root swaps the *permissions* but leaves ``$auth``, ``$access`` and
        ``$session.rd`` pointing at the record user. Only ``invalidate()`` really ends a record
        session — which is exactly why "log out" is :meth:`invalidate`, never a root re-signin,
        and why ``info()`` can still report a record after switching to a system user.
        """
        async with auth_client() as client:
            email = _unique_email()
            await _signup(email)

            await SurrealDBConnectionManager.signin(username="root", password="root")

            assert await client.query("RETURN $auth.email;", {}) == email

    @pytest.mark.asyncio
    async def test_a_wrong_password_raises_an_authentication_error(self) -> None:
        """Asserted on type only: 3.x raises NotFoundError here and 2.6.x InternalError."""
        async with auth_client():
            email = _unique_email()
            await _signup(email)

            with pytest.raises(SurrealDbAuthenticationError):
                await SurrealDBConnectionManager.signin(
                    access=AUTH_ACCESS, variables={"email": email, "pass": "wrong-passphrase"}
                )

    @pytest.mark.asyncio
    async def test_an_unknown_access_method_raises_an_authentication_error(self) -> None:
        async with auth_client():
            with pytest.raises(SurrealDbAuthenticationError):
                await SurrealDBConnectionManager.signin(
                    access="no_such_access", variables={"email": _unique_email(), "pass": PASSWORD}
                )

    @pytest.mark.asyncio
    async def test_the_failure_is_caught_by_existing_surreal_db_error_handlers(self) -> None:
        async with auth_client():
            with pytest.raises(SurrealDbError):
                await SurrealDBConnectionManager.signin(
                    access="no_such_access", variables={"email": _unique_email(), "pass": PASSWORD}
                )


class TestAuthArgumentsAreValidatedBeforeAnyRequest:
    """Validation lives in ``build_auth_payload``, ahead of ``get_client()``.

    The spy makes that observable: a bad call must not even reach for a connection, so a
    typo never costs a round trip nor half-changes a live session.
    """

    @pytest.fixture
    def no_client_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _forbidden() -> Any:
            raise AssertionError("get_client() must not be reached for an invalid call")

        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", _forbidden)

    @pytest.mark.asyncio
    async def test_signup_without_variables(self, no_client_allowed: None) -> None:
        with pytest.raises(ValueError):
            await SurrealDBConnectionManager.signup(access=AUTH_ACCESS)

    @pytest.mark.asyncio
    async def test_signup_without_an_access_method(self, no_client_allowed: None) -> None:
        with pytest.raises(ValueError):
            await SurrealDBConnectionManager.signup(variables={"email": "a@b.c"})

    @pytest.mark.asyncio
    async def test_signin_with_no_credentials_at_all(self, no_client_allowed: None) -> None:
        with pytest.raises(ValueError):
            await SurrealDBConnectionManager.signin()

    @pytest.mark.asyncio
    async def test_signin_mixing_a_system_user_with_record_access(self, no_client_allowed: None) -> None:
        with pytest.raises(ValueError):
            await SurrealDBConnectionManager.signin(
                username="root", password="root", access=AUTH_ACCESS, variables={"email": "a@b.c"}
            )
