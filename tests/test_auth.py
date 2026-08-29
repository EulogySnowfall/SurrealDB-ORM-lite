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

import pytest

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
