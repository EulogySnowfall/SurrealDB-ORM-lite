"""Model-level authentication — AuthenticatedUserMixin (v0.17.0)."""

import contextlib
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest

from surreal_orm_lite import (
    AuthenticatedUserMixin,
    AuthResult,
    AuthTokens,
    BaseSurrealModel,
    SurrealConfigDict,
    SurrealDbAuthenticationError,
    SurrealDBConnectionManager,
    SurrealDbError,
)
from surreal_orm_lite._sdk import SurrealError

HOST = os.getenv("SURREALDB_HOST", "localhost")
PORT = os.getenv("SURREALDB_PORT", "8000")


def _connect(scheme: str = "ws") -> None:
    suffix = "/rpc" if scheme in ("ws", "wss") else ""
    SurrealDBConnectionManager.set_connection(f"{scheme}://{HOST}:{PORT}{suffix}", "root", "root", "ns", "db")


class TestEphemeralClient:
    @pytest.mark.asyncio
    async def test_raises_when_no_connection_configured(self) -> None:
        await SurrealDBConnectionManager.unset_connection()
        with pytest.raises(ValueError, match="Connection not been set"):
            async with SurrealDBConnectionManager.ephemeral_client():
                pass  # pragma: no cover

    @pytest.mark.asyncio
    async def test_yields_a_connected_but_anonymous_client(self) -> None:
        """Connected enough to talk, but carrying no identity of its own.

        Both DB lines answer an anonymous ``info()`` with an IAM permissions error rather than
        a quiet ``None`` — proof the client never inherited the configured root credentials,
        which is exactly what makes it safe to authenticate as somebody else.
        """
        _connect()
        try:
            async with SurrealDBConnectionManager.ephemeral_client() as client:
                with pytest.raises(SurrealError, match="(?i)permission"):
                    await client.info()
        finally:
            await SurrealDBConnectionManager.unset_connection()

    @pytest.mark.asyncio
    async def test_does_not_enter_the_shared_per_loop_cache(self) -> None:
        """The whole point: an ephemeral client must not become *the* client."""
        _connect()
        try:
            shared = await SurrealDBConnectionManager.get_client()
            async with SurrealDBConnectionManager.ephemeral_client() as ephemeral:
                assert ephemeral is not shared
            # The shared client is untouched and still root.
            assert await SurrealDBConnectionManager.get_client() is shared
            await shared.query("INFO FOR DB;", {})
        finally:
            await SurrealDBConnectionManager.unset_connection()

    @pytest.mark.asyncio
    async def test_closes_the_client_even_when_the_body_raises(self) -> None:
        _connect()
        captured: list[Any] = []
        try:
            with pytest.raises(RuntimeError, match="boom"):
                async with SurrealDBConnectionManager.ephemeral_client() as client:
                    captured.append(client)
                    raise RuntimeError("boom")
            # A closed client refuses further use.
            with pytest.raises(SurrealError):
                await captured[0].info()
        finally:
            await SurrealDBConnectionManager.unset_connection()


class DefaultUser(AuthenticatedUserMixin, BaseSurrealModel):
    email: str
    password: str


class ConfiguredUser(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(
        access_name="account",
        identifier_field="login",
        password_field="secret",
        auth_algorithm="bcrypt",
        auth_duration_token="5m",
        auth_duration_session="1h",
    )

    login: str
    secret: str


class TestAuthConfig:
    def test_defaults(self) -> None:
        assert DefaultUser.get_access_name() == "DefaultUser_access"
        assert DefaultUser.get_identifier_field() == "email"
        assert DefaultUser.get_password_field() == "password"

    def test_config_overrides(self) -> None:
        assert ConfiguredUser.get_access_name() == "account"
        assert ConfiguredUser.get_identifier_field() == "login"
        assert ConfiguredUser.get_password_field() == "secret"

    def test_identifier_field_must_exist_on_the_model(self) -> None:
        class Broken(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(identifier_field="nope")
            email: str
            password: str

        with pytest.raises(ValueError, match="identifier_field='nope'"):
            Broken.get_identifier_field()

    def test_password_field_must_exist_on_the_model(self) -> None:
        class Broken2(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(password_field="nope")
            email: str
            password: str

        with pytest.raises(ValueError, match="password_field='nope'"):
            Broken2.get_password_field()

    def test_rejects_an_unknown_hash_algorithm(self) -> None:
        class BadAlgo(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(auth_algorithm="rot13")
            email: str
            password: str

        with pytest.raises(ValueError, match="auth_algorithm"):
            BadAlgo.access_ddl()


class TestAccessDDL:
    def test_renders_table_then_access(self) -> None:
        table_ddl, access_ddl = DefaultUser.access_ddl()

        assert table_ddl == (
            "DEFINE TABLE OVERWRITE DefaultUser SCHEMALESS PERMISSIONS FOR select, update WHERE id = $auth.id;"
        )
        assert access_ddl == (
            "DEFINE ACCESS OVERWRITE DefaultUser_access ON DATABASE TYPE RECORD "
            "SIGNUP ( CREATE DefaultUser SET email = $email, "
            "password = crypto::argon2::generate($password) ) "
            "SIGNIN ( SELECT * FROM DefaultUser WHERE email = $email "
            "AND crypto::argon2::compare(password, $password) ) "
            "DURATION FOR TOKEN 15m, FOR SESSION 12h;"
        )

    def test_with_table_false_returns_only_the_access_statement(self) -> None:
        statements = DefaultUser.access_ddl(with_table=False)
        assert len(statements) == 1
        assert statements[0].startswith("DEFINE ACCESS OVERWRITE")

    def test_overwrite_false_uses_if_not_exists(self) -> None:
        for statement in DefaultUser.access_ddl(overwrite=False):
            assert "IF NOT EXISTS" in statement
            assert "OVERWRITE" not in statement

    def test_honours_config(self) -> None:
        access = ConfiguredUser.access_ddl(with_table=False)[0]
        assert "DEFINE ACCESS OVERWRITE account ON DATABASE TYPE RECORD" in access
        assert "login = $login" in access
        assert "secret = crypto::bcrypt::generate($secret)" in access
        assert "crypto::bcrypt::compare(secret, $secret)" in access
        assert "DURATION FOR TOKEN 5m, FOR SESSION 1h;" in access

    def test_extra_fields_are_included_in_signup(self) -> None:
        class Profile(AuthenticatedUserMixin, BaseSurrealModel):
            email: str
            password: str
            name: str = ""

        access = Profile.access_ddl(with_table=False)[0]
        assert "name = $name" in access

    def test_primary_key_targets_the_record_id(self) -> None:
        """A configured primary key must drive the record id, not a random one."""

        class Keyed(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(primary_key="email")
            email: str
            password: str

        access = Keyed.access_ddl(with_table=False)[0]
        assert "CREATE type::thing('Keyed', $email) SET" in access

    def test_id_field_is_never_written_in_signup(self) -> None:
        class WithId(AuthenticatedUserMixin, BaseSurrealModel):
            id: str | None = None
            email: str
            password: str

        access = WithId.access_ddl(with_table=False)[0]
        assert "id = $id" not in access

    def test_with_refresh_adds_the_clause_and_the_grant_duration(self) -> None:
        class Refreshable(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(with_refresh=True, auth_duration_grant="7d")
            email: str
            password: str

        access = Refreshable.access_ddl(with_table=False)[0]
        assert "WITH REFRESH" in access
        assert "DURATION FOR TOKEN 15m, FOR SESSION 12h, FOR GRANT 7d;" in access

    def test_is_pure(self) -> None:
        """No connection configured, yet it renders: proof it touches no database."""
        assert DefaultUser.access_ddl()


AUTH_PASSWORD = "s3cret-passphrase"


def _unique_email() -> str:
    """A fresh identity per test.

    Reusing one would make the suite order-dependent: the record survives inside the access
    method's table until the teardown removes it.
    """
    return f"{uuid4().hex}@example.test"


class E2EUser(AuthenticatedUserMixin, BaseSurrealModel):
    # `id` is the ORM's standing contract for every model (a model needs either an `id` field
    # or a primary_key). Auth models want the field form: the server generates the record id
    # during SIGNUP, and this is what receives it.
    model_config = SurrealConfigDict(access_name="e2e_acct")

    id: str | None = None
    email: str
    password: str
    name: str = ""


@contextlib.asynccontextmanager
async def auth_model(model: Any = E2EUser, scheme: str = "ws") -> AsyncIterator[Any]:
    """Define *model*'s access method, then remove it again.

    An async context manager rather than a pytest fixture: the SDK's WebSocket client is bound
    to the event loop that created it, and fixtures run in the module-scoped loop while tests
    get their own — so the client must be opened inside the test's loop.
    """
    _connect(scheme)
    try:
        await model.define_access()
        yield model
    finally:
        with contextlib.suppress(Exception):
            client = await SurrealDBConnectionManager.get_client()
            await client.query(f"REMOVE ACCESS {model.get_access_name()} ON DATABASE;", {})
            await client.query(f"REMOVE TABLE {model.get_table_name()};", {})
        await SurrealDBConnectionManager.unset_connection()


class TestDefineAccessE2E:
    @pytest.mark.asyncio
    async def test_applies_and_returns_the_statements(self) -> None:
        async with auth_model() as model:
            applied = await model.define_access()
            assert len(applied) == 2
            assert applied == model.access_ddl()

    @pytest.mark.asyncio
    async def test_is_idempotent(self) -> None:
        async with auth_model() as model:
            await model.define_access()
            await model.define_access()  # must not raise

    @pytest.mark.asyncio
    async def test_the_access_method_really_exists_on_the_server(self) -> None:
        async with auth_model() as model:
            client = await SurrealDBConnectionManager.get_client()
            info = await client.query("INFO FOR DB;", {})
            assert model.get_access_name() in str(info)

    @pytest.mark.asyncio
    async def test_with_refresh_needs_surrealdb_3x(self) -> None:
        class RefreshUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(access_name="e2e_refresh", with_refresh=True)
            id: str | None = None
            email: str
            password: str

        _connect()
        try:
            try:
                await RefreshUser.define_access()
            except SurrealDbError as exc:
                pytest.skip(f"DEFINE ACCESS … WITH REFRESH requires SurrealDB 3.x: {exc}")
            assert "WITH REFRESH" in RefreshUser.access_ddl()[1]
        finally:
            with contextlib.suppress(Exception):
                client = await SurrealDBConnectionManager.get_client()
                await client.query("REMOVE ACCESS e2e_refresh ON DATABASE;", {})
                await client.query("REMOVE TABLE RefreshUser;", {})
            await SurrealDBConnectionManager.unset_connection()


class TestSignupE2E:
    @pytest.mark.asyncio
    async def test_returns_a_hydrated_instance(self) -> None:
        async with auth_model() as model:
            email = _unique_email()
            result = await model.signup(email=email, password=AUTH_PASSWORD, name="Ada")

            assert isinstance(result.user, model)
            assert result.user.email == email
            assert result.user.name == "Ada"
            assert result.user.get_id() is not None
            assert result.tokens.access

    @pytest.mark.asyncio
    async def test_the_password_field_carries_the_hash_not_the_plaintext(self) -> None:
        """SurrealDB returns the stored record; documenting that beats hiding it."""
        async with auth_model() as model:
            result = await model.signup(email=_unique_email(), password=AUTH_PASSWORD)
            assert result.user.password != AUTH_PASSWORD
            assert result.user.password.startswith("$")

    @pytest.mark.asyncio
    async def test_rejects_a_field_the_model_does_not_have(self) -> None:
        _connect()
        try:
            with pytest.raises(ValueError, match="nope"):
                await E2EUser.signup(email="a@b.c", password=AUTH_PASSWORD, nope=1)
        finally:
            await SurrealDBConnectionManager.unset_connection()

    @pytest.mark.asyncio
    async def test_requires_identifier_and_password(self) -> None:
        _connect()
        try:
            with pytest.raises(ValueError, match="password"):
                await E2EUser.signup(email="a@b.c")
        finally:
            await SurrealDBConnectionManager.unset_connection()


class TestSigninE2E:
    @pytest.mark.asyncio
    async def test_returns_the_same_record(self) -> None:
        async with auth_model() as model:
            email = _unique_email()
            created = await model.signup(email=email, password=AUTH_PASSWORD, name="Ada")
            signed_in = await model.signin(email=email, password=AUTH_PASSWORD)

            assert signed_in.user.get_id() == created.user.get_id()
            assert signed_in.user.email == email

    @pytest.mark.asyncio
    async def test_a_wrong_password_raises_the_orm_error(self) -> None:
        async with auth_model() as model:
            email = _unique_email()
            await model.signup(email=email, password=AUTH_PASSWORD)
            with pytest.raises(SurrealDbAuthenticationError):
                await model.signin(email=email, password="wrong")


class TestSessionIsolationE2E:
    """The load-bearing property of v0.17.0."""

    @pytest.mark.asyncio
    async def test_the_shared_connection_stays_root(self) -> None:
        async with auth_model() as model:
            before = SurrealDBConnectionManager.get_session_token()
            email = _unique_email()
            await model.signup(email=email, password=AUTH_PASSWORD)
            await model.signin(email=email, password=AUTH_PASSWORD)

            assert SurrealDBConnectionManager.get_session_token() == before
            # `before` is None for a root connection, so that assert alone is weak. These two
            # carry the real weight: a record user could run neither.
            client = await SurrealDBConnectionManager.get_client()
            assert await client.info() is None, "shared session became a record user"
            await client.query("INFO FOR DB;", {})

    @pytest.mark.asyncio
    async def test_bind_true_adopts_the_token(self) -> None:
        async with auth_model() as model:
            email = _unique_email()
            result = await model.signup(email=email, password=AUTH_PASSWORD, bind=True)
            assert SurrealDBConnectionManager.get_session_token() == result.tokens.access


class TestMissingSelfPermissionE2E:
    @pytest.mark.asyncio
    async def test_names_the_missing_permission(self) -> None:
        """Without self-select, info() returns None — say why instead of failing opaquely."""
        _connect()
        client = await SurrealDBConnectionManager.get_client()
        try:
            await client.query("DEFINE TABLE OVERWRITE E2EUser SCHEMALESS PERMISSIONS NONE;", {})
            await E2EUser.define_access(with_table=False)
            with pytest.raises(SurrealDbAuthenticationError, match="PERMISSIONS"):
                await E2EUser.signup(email=_unique_email(), password=AUTH_PASSWORD)
        finally:
            for statement in (
                "REMOVE ACCESS e2e_acct ON DATABASE;",
                "REMOVE TABLE E2EUser;",
            ):
                with contextlib.suppress(Exception):
                    await client.query(statement, {})
            await SurrealDBConnectionManager.unset_connection()


class TestAuthenticateE2E:
    @pytest.mark.asyncio
    async def test_a_stored_token_returns_the_user(self) -> None:
        async with auth_model() as model:
            email = _unique_email()
            result = await model.signup(email=email, password=AUTH_PASSWORD, name="Ada")

            me = await model.authenticate(result.tokens.access)
            assert isinstance(me, model)
            assert me.email == email
            assert me.get_id() == result.user.get_id()

    @pytest.mark.asyncio
    async def test_a_garbage_token_raises(self) -> None:
        async with auth_model() as model:
            with pytest.raises(SurrealDbAuthenticationError):
                await model.authenticate("not-a-jwt")

    @pytest.mark.asyncio
    async def test_does_not_disturb_the_shared_connection(self) -> None:
        async with auth_model() as model:
            result = await model.signup(email=_unique_email(), password=AUTH_PASSWORD)
            before = SurrealDBConnectionManager.get_session_token()
            await model.authenticate(result.tokens.access)
            assert SurrealDBConnectionManager.get_session_token() == before


class TestRefreshE2E:
    @pytest.mark.asyncio
    async def test_renews_the_session_and_rotates(self) -> None:
        class RefreshUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(access_name="e2e_refresh2", with_refresh=True)
            id: str | None = None
            email: str
            password: str

        _connect()
        try:
            try:
                await RefreshUser.define_access()
            except SurrealDbError as exc:
                pytest.skip(f"WITH REFRESH requires SurrealDB 3.x: {exc}")

            email = _unique_email()
            created = await RefreshUser.signup(email=email, password=AUTH_PASSWORD)
            assert created.tokens.refresh is not None

            renewed = await RefreshUser.refresh(created.tokens.refresh)
            assert renewed.user.get_id() == created.user.get_id()
            assert renewed.tokens.access

            # Rotation: the spent token is dead.
            with pytest.raises(SurrealDbAuthenticationError):
                await RefreshUser.refresh(created.tokens.refresh)
        finally:
            with contextlib.suppress(Exception):
                client = await SurrealDBConnectionManager.get_client()
                await client.query("REMOVE ACCESS e2e_refresh2 ON DATABASE;", {})
                await client.query("REMOVE TABLE RefreshUser;", {})
            await SurrealDBConnectionManager.unset_connection()


class TestPublicExports:
    def test_exported_from_the_package_root(self) -> None:
        import surreal_orm_lite

        assert "AuthenticatedUserMixin" in surreal_orm_lite.__all__
        assert "AuthResult" in surreal_orm_lite.__all__

    def test_auth_result_repr_redacts(self) -> None:
        result = AuthResult(user="whatever", tokens=AuthTokens(access="secret-jwt", refresh="r"))
        assert "secret-jwt" not in repr(result)
        assert "<redacted>" in repr(result)


class TestAuthValidation:
    """Guards that fire before any request reaches a server."""

    def test_rejects_a_duration_that_is_not_a_duration(self) -> None:
        """Durations are interpolated into DDL that define_access() runs as root."""

        class Sneaky(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(auth_duration_token="15m; DEFINE USER hacker ON ROOT PASSWORD 'p' ROLES OWNER")
            id: str | None = None
            email: str
            password: str

        with pytest.raises(ValueError, match="auth_duration_token"):
            Sneaky.access_ddl()

    def test_accepts_ordinary_duration_literals(self) -> None:
        class Durations(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(auth_duration_token="90s", auth_duration_session="7d")
            id: str | None = None
            email: str
            password: str

        assert "DURATION FOR TOKEN 90s, FOR SESSION 7d;" in Durations.access_ddl(with_table=False)[0]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["signup", "signin", "authenticate", "refresh"])
    async def test_every_method_reports_a_missing_connection_the_same_way(self, method: str) -> None:
        await SurrealDBConnectionManager.unset_connection()
        calls = {
            "signup": lambda: E2EUser.signup(email="a@b.c", password=AUTH_PASSWORD),
            "signin": lambda: E2EUser.signin(email="a@b.c", password=AUTH_PASSWORD),
            "authenticate": lambda: E2EUser.authenticate("token"),
            "refresh": lambda: E2EUser.refresh("token"),
        }
        with pytest.raises(ValueError, match="Connection not been set"):
            await calls[method]()

    @pytest.mark.asyncio
    async def test_refresh_on_a_model_without_with_refresh_says_so(self) -> None:
        """Otherwise the server answers 'No record was returned' — a wrong-password message."""
        _connect()
        try:
            with pytest.raises(ValueError, match="with_refresh=True"):
                await E2EUser.refresh("any-token")
        finally:
            await SurrealDBConnectionManager.unset_connection()


class TestDefineAccessInTransactionE2E:
    @pytest.mark.asyncio
    async def test_applies_through_a_transaction(self) -> None:
        _connect()
        try:
            async with SurrealDBConnectionManager.transaction() as tx:
                applied = await E2EUser.define_access(tx=tx)
                assert applied == E2EUser.access_ddl()

            # Committed: the access method is really there, and usable.
            result = await E2EUser.signup(email=_unique_email(), password=AUTH_PASSWORD)
            assert result.user.get_id() is not None
        finally:
            with contextlib.suppress(Exception):
                client = await SurrealDBConnectionManager.get_client()
                await client.query("REMOVE ACCESS e2e_acct ON DATABASE;", {})
                await client.query("REMOVE TABLE E2EUser;", {})
            await SurrealDBConnectionManager.unset_connection()


class TestHttpSchemeE2E:
    """The ephemeral client skips connect() on HTTP — a distinct code path."""

    @pytest.mark.asyncio
    async def test_the_full_cycle_works_over_http(self) -> None:
        async with auth_model(scheme="http") as model:
            email = _unique_email()
            created = await model.signup(email=email, password=AUTH_PASSWORD, name="Ada")
            assert created.user.email == email

            signed_in = await model.signin(email=email, password=AUTH_PASSWORD)
            assert signed_in.user.get_id() == created.user.get_id()

            me = await model.authenticate(created.tokens.access)
            assert me.get_id() == created.user.get_id()


class TestAuthenticateBindE2E:
    @pytest.mark.asyncio
    async def test_bind_true_adopts_the_token_on_the_shared_connection(self) -> None:
        async with auth_model() as model:
            created = await model.signup(email=_unique_email(), password=AUTH_PASSWORD)

            me = await model.authenticate(created.tokens.access, bind=True)
            assert me.get_id() == created.user.get_id()
            assert SurrealDBConnectionManager.get_session_token() == created.tokens.access


class TestBindKeepsTheRefreshTokenE2E:
    """`bind=True` must not drop a refresh token it actually holds — SurrealDB 3.x only."""

    @pytest.mark.asyncio
    async def test_the_shared_session_can_still_renew_itself(self) -> None:
        class BindRefreshUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(access_name="e2e_bind_refresh", with_refresh=True)
            id: str | None = None
            email: str
            password: str

        _connect()
        try:
            try:
                await BindRefreshUser.define_access()
            except SurrealDbError as exc:
                pytest.skip(f"WITH REFRESH requires SurrealDB 3.x: {exc}")

            created = await BindRefreshUser.signup(email=_unique_email(), password=AUTH_PASSWORD, bind=True)
            assert created.tokens.refresh is not None

            # CM.authenticate() clears the refresh token by design; the mixin puts the matching
            # one back, so the bound session is not left holding a live token it forgot.
            assert SurrealDBConnectionManager.get_session_token() == created.tokens.access
            assert SurrealDBConnectionManager.get_refresh_token() == created.tokens.refresh
        finally:
            with contextlib.suppress(Exception):
                await SurrealDBConnectionManager.invalidate()
                client = await SurrealDBConnectionManager.get_client()
                await client.query("REMOVE ACCESS e2e_bind_refresh ON DATABASE;", {})
                await client.query("REMOVE TABLE BindRefreshUser;", {})
            await SurrealDBConnectionManager.unset_connection()
