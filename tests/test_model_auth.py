"""Model-level authentication — AuthenticatedUserMixin (v0.17.0)."""

import contextlib
import logging
import os
import traceback
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

import pytest
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from surreal_orm_lite import (
    AuthenticatedUserMixin,
    AuthResult,
    AuthTokens,
    BaseSurrealModel,
    Index,
    QuerySet,
    SchemaMode,
    SurrealConfigDict,
    SurrealDbAuthenticationError,
    SurrealDbConnectionError,
    SurrealDBConnectionManager,
    SurrealDbError,
)
from surreal_orm_lite._sdk import SurrealError
from surreal_orm_lite.schema import signup_excluded_refused
from surreal_orm_lite.signals import around_save, around_update, post_save, post_update, pre_save, pre_update
from surreal_orm_lite.time_travel import snapshot_write_refused
from surreal_orm_lite.transaction import BufferedTransaction
from tests.conftest import orm_client

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
    @pytest.mark.parametrize("schema_mode", [None, SchemaMode.SCHEMALESS, SchemaMode.SCHEMAFULL], ids=str)
    @pytest.mark.parametrize("method", ["signup", "signin"])
    async def test_an_excluded_signup_field_is_refused_before_any_request(
        self, monkeypatch: pytest.MonkeyPatch, schema_mode: SchemaMode | None, method: str
    ) -> None:
        """#299: ``update()`` and ``upsert()`` replace the record with what ``model_dump()`` writes, which leaves out a
        ``Field(exclude=True)`` field SIGNUP wrote, whatever the schema mode: the stored hash would be lost and every
        later signin fail. Over an access defined by hand (no ORM DDL call), ``signup()`` and ``signin()`` refuse the
        model with the DDL methods' ``TypeError``, before any request — even before an unknown field."""

        class HiddenUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="hidden_user", schema_mode=schema_mode, connection="sg1")

            id: str | None = None
            email: str
            password: str = Field(exclude=True)
            nickname: str | None = Field(default=None, exclude=True)

        requests: list[str] = []

        def no_request(name: str) -> Any:
            def refuse(*args: Any, **kwargs: Any) -> Any:
                requests.append(name)
                raise AssertionError(f"{name}() was called")

            return refuse

        for name in ("ephemeral_client", "get_client", "_exclusive_embedded_client"):
            monkeypatch.setattr(SurrealDBConnectionManager, name, no_request(name))
        # A server nobody listens on: a request that slipped through would fail with another error.
        SurrealDBConnectionManager.add_connection(
            "sg1", url="ws://127.0.0.1:9/rpc", user="root", password="root", namespace="ns", database="db"
        )
        calls = {
            "signup": lambda: HiddenUser.signup(email="a@b.c", password=AUTH_PASSWORD, unknown=1),
            "signin": lambda: HiddenUser.signin(email="a@b.c", password=AUTH_PASSWORD),
        }
        with pytest.raises(TypeError) as raised:
            await calls[method]()
        assert requests == []
        assert str(raised.value) == signup_excluded_refused("HiddenUser", ["password", "nickname"])
        for part in (
            "HiddenUser: SIGNUP writes 'password', 'nickname',",
            "Remove exclude=True",
            "signup(), signin(), authenticate(), refresh(), update() and upsert() refuse HiddenUser.",
        ):
            assert part in str(raised.value)
        with pytest.raises(TypeError) as ddl:
            HiddenUser.access_ddl(with_table=False)
        assert str(ddl.value) == str(raised.value)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["update", "update(tx=)", "upsert", "upsert(tx=)", "authenticate", "refresh"])
    async def test_an_excluded_signup_field_refuses_the_writes_and_auth_calls_before_anything(
        self, monkeypatch: pytest.MonkeyPatch, method: str
    ) -> None:
        """#310: ``update()`` and ``upsert()`` replace the stored record with what ``model_dump()`` writes, so on an
        instance loaded with ``get()`` or returned by ``authenticate()`` they would drop the hash an access defined by
        hand (or another client) stored. They refuse the model with SG1's ``TypeError``, with or without ``tx=``, as
        ``authenticate()`` and ``refresh()`` do — before any request, any signal and any use of the transaction."""

        class HiddenWriter(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="hidden_writer", connection="sg1", with_refresh=True)

            id: str | None = None
            email: str
            password: str = Field(exclude=True)

        requests: list[str] = []

        def no_request(name: str) -> Any:
            def refuse(*args: Any, **kwargs: Any) -> Any:
                requests.append(name)
                raise AssertionError(f"{name}() was called")

            return refuse

        for name in ("ephemeral_client", "get_client", "_exclusive_embedded_client"):
            monkeypatch.setattr(SurrealDBConnectionManager, name, no_request(name))
        # A server nobody listens on: a request that slipped through would fail with another error.
        SurrealDBConnectionManager.add_connection(
            "sg1", url="ws://127.0.0.1:9/rpc", user="root", password="root", namespace="ns", database="db"
        )
        fired: list[str] = []

        async def on_signal(sender: Any, **kwargs: Any) -> None:
            fired.append("signal")

        async def around(sender: Any, **kwargs: Any) -> AsyncIterator[None]:
            fired.append("around")
            yield

        signals = (pre_save, post_save, pre_update, post_update)
        for signal in signals:
            signal.connect(HiddenWriter)(on_signal)
        for wrapper in (around_save, around_update):
            wrapper.connect(HiddenWriter)(around)
        tx = BufferedTransaction(client=None)
        user = HiddenWriter(id="ada", email="a@b.c", password="$argon2id$stored-hash")
        calls = {
            "update": lambda: user.update(),
            "update(tx=)": lambda: user.update(tx=tx),
            "upsert": lambda: user.upsert(),
            "upsert(tx=)": lambda: user.upsert(tx=tx),
            "authenticate": lambda: HiddenWriter.authenticate("token"),
            "refresh": lambda: HiddenWriter.refresh("refresh-token"),
        }
        try:
            with pytest.raises(TypeError) as raised:
                await calls[method]()
        finally:
            for signal in signals:
                signal.disconnect(on_signal, HiddenWriter)
            for wrapper in (around_save, around_update):
                wrapper.disconnect(around, HiddenWriter)
        assert requests == []
        assert fired == []
        assert tx.statements == [] and tx.variables == {}
        # #315: nor is the table noted on the transaction — for define_indexes(tx=)'s check (#304) or the query cache.
        assert tx._records_written() == frozenset()
        assert tx._cache_written() == frozenset()
        assert str(raised.value) == signup_excluded_refused("HiddenWriter", ["password"])
        for call in ("update()", "upsert()", "authenticate()", "refresh()"):
            assert call in str(raised.value)

    @pytest.mark.asyncio
    async def test_update_and_upsert_still_write_without_an_excluded_signup_field(self) -> None:
        """#310 refuses only an auth model with an excluded signup field: an auth model without one, and a model that
        is not an auth model (whatever it excludes), keep writing."""

        class PlainUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="plain_user")

            id: str | None = None
            email: str
            password: str

        class NotAuth(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="not_auth")

            id: str | None = None
            email: str
            secret: str = Field(default="", exclude=True)

        tx = BufferedTransaction(client=None)
        for instance in (PlainUser(id="a", email="a@b.c", password="hash"), NotAuth(id="b", email="b@c.d", secret="s")):
            await instance.update(tx=tx)
            await instance.upsert(tx=tx)
        assert [statement.split(" ", 1)[0] for statement in tx.statements] == ["UPDATE", "UPSERT", "UPDATE", "UPSERT"]

    @pytest.mark.asyncio
    async def test_refresh_on_a_model_without_with_refresh_says_so(self) -> None:
        """Otherwise the server answers 'No record was returned' — a wrong-password message."""
        _connect()
        try:
            with pytest.raises(ValueError, match="with_refresh=True"):
                await E2EUser.refresh("any-token")
        finally:
            await SurrealDBConnectionManager.unset_connection()


class TestExcludedSignupFieldWriteE2E:
    """#310 on both server lines: the refused ``update()`` and ``upsert()`` leave the stored hash as it was."""

    @pytest.mark.asyncio
    async def test_update_and_upsert_keep_the_stored_hash(self) -> None:
        class HashKeeper(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="sg1_hash_keeper")

            id: str | None = None
            email: str
            password: str = Field(exclude=True)

        async with orm_client("sg1_hash_keeper") as client:
            # The row an access defined by hand (or another client) leaves: the hash SIGNUP stores.
            await client.query(
                "CREATE sg1_hash_keeper:ada CONTENT { email: $email, password: crypto::argon2::generate($password) };",
                {"email": "ada@example.test", "password": AUTH_PASSWORD},
            )

            async def stored_hash() -> Any:
                return await client.query("SELECT VALUE password FROM ONLY sg1_hash_keeper:ada;", {})

            stored = await stored_hash()
            assert isinstance(stored, str) and stored.startswith("$argon2")
            me = await HashKeeper.objects().get("ada")
            assert me.password == stored

            for call in ("update", "upsert"):
                refused: TypeError | None = None
                try:
                    await getattr(me, call)()
                except TypeError as exc:
                    refused = exc
                assert await stored_hash() == stored, f"{call}() changed the stored hash"
                assert refused is not None, f"{call}() did not refuse the model"
                assert str(refused) == signup_excluded_refused("HashKeeper", ["password"])


class IndexedKeeper(AuthenticatedUserMixin, BaseSurrealModel):
    """An auth model whose excluded password field SG1 refuses in ``update()``/``upsert()``, with an index to define."""

    model_config = SurrealConfigDict(
        table_name="sg1_indexed_keeper", indexes=[Index("sg1_indexed_keeper_email", "email", unique=True)]
    )

    id: str | None = None
    email: str
    password: str = Field(exclude=True)


class TestRefusedWriteInTransactionE2E:
    """#315 on both server lines: a write the model refuses in a transaction — SG1 (#310), or the refusal of a snapshot
    read with ``as_of()`` — leaves the transaction as it was. ``define_indexes(tx=…)`` on the same table then defines
    the index instead of refusing it as if a record had been written there (#304), and the commit applies it (at once
    on SurrealDB 3.x over WebSocket, interactive; at the commit on 2.x, buffered)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("refusal", ["signup", "snapshot"])
    @pytest.mark.parametrize("call", ["update", "upsert"])
    async def test_define_indexes_follows_a_refused_write(self, refusal: str, call: str) -> None:
        moment = datetime(2021, 6, 1, tzinfo=UTC)
        me = IndexedKeeper(id="ada", email="ada@example.test", password="$argon2id$stored-hash")
        if refusal == "snapshot":
            QuerySet._mark_snapshots(me, moment)
        async with orm_client("sg1_indexed_keeper") as client:
            async with SurrealDBConnectionManager.transaction() as tx:
                with pytest.raises((TypeError, SurrealDbError)) as raised:
                    await getattr(me, call)(tx=tx)
                written = (tx._records_written(), tx._cache_written())
                applied = await IndexedKeeper.define_indexes(tx=tx)
            info = await client.query("INFO FOR TABLE sg1_indexed_keeper;", {})
            rows = await client.query("SELECT * FROM sg1_indexed_keeper;", {})
        if refusal == "signup":
            assert type(raised.value) is TypeError
            assert str(raised.value) == signup_excluded_refused("IndexedKeeper", ["password"])
        else:
            assert type(raised.value) is SurrealDbError
            assert str(raised.value) == snapshot_write_refused("IndexedKeeper", f"{call}()", moment)
        assert written == (frozenset(), frozenset())
        assert applied == IndexedKeeper.index_ddl()
        assert list(info["indexes"]) == ["sg1_indexed_keeper_email"]
        assert rows == []


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


class E2EMisfit(AuthenticatedUserMixin, BaseSurrealModel):
    """``E2EUser``'s table and access, with a required field its records lack: they do not fit this model."""

    model_config = SurrealConfigDict(table_name="E2EUser", access_name="e2e_acct")

    id: str | None = None
    email: str
    password: str
    nickname: str


class _MisfitCat(BaseModel):
    kind: Literal["cat"]
    lives: int


class _MisfitDog(BaseModel):
    kind: Literal["dog"]
    good: bool


class ShapedMisfit(AuthenticatedUserMixin, BaseSurrealModel):
    """Fields whose Pydantic errors carry record data outside ``input_value``: a dict key in ``loc``, a
    discriminator tag in ``msg``, a validator's own text in ``msg``."""

    model_config = SurrealConfigDict(table_name="E2EUser", access_name="e2e_acct")

    id: str | None = None
    email: str
    password: str
    nickname: str
    prefs: dict[str, int] = {}
    pet: _MisfitCat | _MisfitDog | None = Field(default=None, discriminator="kind")
    code: str = "ok"
    badge: str = "ok"

    @field_validator("code")
    @classmethod
    def _check_code(cls, value: str) -> str:
        if not value.startswith("ok"):
            raise ValueError(f"bad code {value!r}")
        return value

    @field_validator("badge")
    @classmethod
    def _check_badge(cls, value: str) -> str:
        if not value.startswith("ok"):
            raise PydanticCustomError("badge_refused", "badge {badge} is not on the list", {"badge": value})
        return value


class _StrictPrefs(BaseModel):
    """A sub-model that forbids keys it does not declare: its ``loc`` is ``(field, record key)``."""

    model_config = ConfigDict(extra="forbid")

    theme: str = "dark"


def _assert_not_quoted(error: BaseException, *secrets: str) -> None:
    """None of *secrets* reaches the error's text or a formatted traceback of it."""
    rendered = {"str": str(error), "traceback": "".join(traceback.format_exception(error))}
    for where, text in rendered.items():
        for secret in secrets:
            assert secret not in text, (where, secret)


#: What the error says about the shared session when the record does not fit the model.
_NOW_BOUND = "The shared session is now signed in as that record"
_UNCHANGED = "The shared session keeps its previous identity"


class TestMisfitRecordMessage:
    """The error of a record that does not fit the model says what happened to the shared session."""

    def test_a_bound_exchange_says_the_session_is_signed_in(self) -> None:
        with pytest.raises(SurrealDbError) as raised:
            E2EMisfit._hydrate_record({"email": "a@b.c", "password": "x"}, "signin", bound=True)
        message = str(raised.value)
        assert "E2EMisfit.signin() succeeded but the returned record does not fit the model" in message
        assert _NOW_BOUND in message
        assert _UNCHANGED not in message
        assert "has been created" not in message
        assert "reconcile the model with the table rather than retrying" in message

    def test_a_bound_signup_also_says_the_record_exists(self) -> None:
        with pytest.raises(SurrealDbError) as raised:
            E2EMisfit._hydrate_record({"email": "a@b.c", "password": "x"}, "signup", bound=True)
        assert f"{_NOW_BOUND}, and the record has been created" in str(raised.value)

    def test_an_unbound_signup_says_the_session_is_unchanged_but_the_record_exists(self) -> None:
        with pytest.raises(SurrealDbError) as raised:
            E2EMisfit._hydrate_record({"email": "a@b.c", "password": "x"}, "signup", bound=False)
        assert f"{_UNCHANGED}, but the record has been created" in str(raised.value)

    def test_an_authenticate_says_the_session_is_unchanged(self) -> None:
        with pytest.raises(SurrealDbError) as raised:
            E2EMisfit._hydrate_record({"email": "a@b.c", "password": "x"}, "authenticate", bound=False)
        message = str(raised.value)
        assert _UNCHANGED in message
        assert _NOW_BOUND not in message
        assert "has been created" not in message
        assert "The session is authenticated" not in message

    def test_the_failing_fields_are_listed_without_the_record_values(self) -> None:
        """Pydantic's own text quotes the input (``input_value=…``): for a missing field, the whole record,
        password hash included. The error names the fields that do not fit and Pydantic's error codes, and
        quotes no value from the record."""
        record = {"id": "misfit-id-4821", "email": 987654321, "password": "$argon2id$v=19$m=19456,t=2,p=1$c2VjcmV0"}
        with pytest.raises(SurrealDbError) as raised:
            E2EMisfit._hydrate_record(record, "signin", bound=False)
        message = str(raised.value)
        assert "(email: string_type; nickname: missing)" in message
        for value in ("misfit-id-4821", "987654321", "$argon2id", "c2VjcmV0"):
            assert value not in message, value
        assert "input_value" not in message and "errors.pydantic.dev" not in message
        # Nor in a logged traceback: the ValidationError is not chained as the cause.
        rendered = "".join(traceback.format_exception(raised.value))
        assert "c2VjcmV0" not in rendered and "987654321" not in rendered
        assert raised.value.__cause__ is None
        assert isinstance(raised.value.__context__, ValidationError)

    def test_record_data_in_pydantic_locations_and_messages_is_not_quoted(self, caplog: pytest.LogCaptureFixture) -> None:
        """Beyond ``input_value``, Pydantic's errors carry record data in ``loc`` (a dict key) and in ``msg`` (an
        unknown discriminator tag, a validator's own text). The error lists each failing top-level field once,
        with Pydantic's error codes only: none of these reach the message, a formatted traceback or a log."""
        record = {
            "id": "misfit-id-4821",
            "email": "a@b.c",
            "password": "$argon2id$v=19$m=19456,t=2,p=1$c2VjcmV0",
            "prefs": {"SECRETKEYNAME": "not-a-number", "OTHERKEYNAME": "nan-either"},
            "pet": {"kind": "SECRETTAG"},
            "code": "xSECRETCODE",
            "badge": "xSECRETBADGE",
        }
        with pytest.raises(SurrealDbError) as raised:
            ShapedMisfit._hydrate_record(record, "signin", bound=False)
        message = str(raised.value)
        assert (
            "(nickname: missing; prefs: int_parsing; pet: union_tag_invalid; code: value_error; badge: badge_refused)"
            in message
        )

        with caplog.at_level(logging.ERROR, logger="tests.misfit"):
            try:
                raise raised.value
            except SurrealDbError:
                logging.getLogger("tests.misfit").exception("signin failed")
        rendered = {
            "str": message,
            "traceback": "".join(traceback.format_exception(raised.value)),
            "log": caplog.text,
        }
        assert "union_tag_invalid" in rendered["log"]
        for where, text in rendered.items():
            for secret in ("SECRETKEYNAME", "OTHERKEYNAME", "SECRETTAG", "SECRETCODE", "SECRETBADGE", "c2VjcmV0", "a@b.c"):
                assert secret not in text, (where, secret)
            for wording in ("Input tag", "Value error", "not on the list", "unable to parse"):
                assert wording not in text, (where, wording)

    def test_an_error_of_the_whole_record_is_listed_as_the_record(self) -> None:
        """A model-level validator has no field: its error is listed as ``(record)``, with its code only."""

        class WholeMisfit(E2EMisfit):
            @model_validator(mode="after")
            def _refuse(self) -> "WholeMisfit":
                raise ValueError(f"refused {self.password}")

        with pytest.raises(SurrealDbError) as raised:
            WholeMisfit._hydrate_record({"email": "a@b.c", "password": "c2VjcmV0", "nickname": "n"}, "signin", bound=False)
        message = str(raised.value)
        assert "((record): value_error)" in message
        assert "c2VjcmV0" not in message and "refused" not in message

    def test_a_forbidden_key_is_listed_without_its_name(self) -> None:
        """With ``extra="forbid"``, Pydantic's ``loc`` is the record's own key: it is not quoted either."""

        class StrictMisfit(E2EMisfit):
            model_config = SurrealConfigDict(table_name="E2EUser", access_name="e2e_acct", extra="forbid")

        record = {"email": "a@b.c", "password": "x", "SECRETCOLUMN": 1, "OTHERCOLUMN": 2}
        with pytest.raises(SurrealDbError) as raised:
            StrictMisfit._hydrate_record(record, "signin", bound=False)
        message = str(raised.value)
        assert "(nickname: missing; (undeclared field): extra_forbidden)" in message
        assert "SECRETCOLUMN" not in message and "OTHERCOLUMN" not in message

    def test_a_typed_extra_is_listed_without_its_key(self) -> None:
        """With ``extra="allow"`` and typed extras, Pydantic's ``loc`` of an extra that does not fit is the
        record's own key: a key the model does not declare is never named."""

        class TypedExtraMisfit(E2EMisfit):
            model_config = SurrealConfigDict(table_name="E2EUser", access_name="e2e_acct", extra="allow")
            __pydantic_extra__: dict[str, int] = Field(init=False)

        record = {"email": "a@b.c", "password": "c2VjcmV0", "nickname": "n", "SECRETCOLUMN": "SECRETVALUE"}
        with pytest.raises(SurrealDbError) as raised:
            TypedExtraMisfit._hydrate_record(record, "signin", bound=False)
        assert "((undeclared field): int_parsing)" in str(raised.value)
        _assert_not_quoted(raised.value, "SECRETCOLUMN", "SECRETVALUE", "c2VjcmV0", "a@b.c")

    def test_a_model_validator_validating_a_dict_does_not_name_its_keys(self) -> None:
        """A model validator that validates a dict itself raises errors whose ``loc`` starts with that dict's
        key, not with a field of the model: it is listed as an undeclared field."""

        class DictCheckMisfit(E2EMisfit):
            prefs: dict[str, Any] = {}

            @model_validator(mode="after")
            def _check_prefs(self) -> "DictCheckMisfit":
                TypeAdapter(dict[str, int]).validate_python(self.prefs)
                return self

        record = {"email": "a@b.c", "password": "c2VjcmV0", "nickname": "n", "prefs": {"SECRETKEYNAME": "SECRETVALUE"}}
        with pytest.raises(SurrealDbError) as raised:
            DictCheckMisfit._hydrate_record(record, "signin", bound=False)
        assert "((undeclared field): int_parsing)" in str(raised.value)
        _assert_not_quoted(raised.value, "SECRETKEYNAME", "SECRETVALUE", "c2VjcmV0", "a@b.c")

    def test_a_forbidden_key_of_a_sub_model_is_listed_under_its_field(self) -> None:
        """A sub-model that forbids a key reports ``(field, record key)``: the declared field is named, the key
        is not."""

        class SubStrictMisfit(E2EMisfit):
            prefs: _StrictPrefs | None = None

        record = {
            "email": "a@b.c",
            "password": "c2VjcmV0",
            "nickname": "n",
            "prefs": {"theme": "light", "SECRETKEYNAME": "SECRETVALUE"},
        }
        with pytest.raises(SurrealDbError) as raised:
            SubStrictMisfit._hydrate_record(record, "signin", bound=False)
        assert "(prefs: extra_forbidden)" in str(raised.value)
        _assert_not_quoted(raised.value, "SECRETKEYNAME", "SECRETVALUE", "c2VjcmV0", "a@b.c", "(undeclared field)")

    def test_an_aliased_field_is_listed_under_the_name_it_was_read_by(self) -> None:
        """Pydantic reports an aliased field under the key it read: an ``alias``, the first key of an
        ``AliasPath``, the choice of an ``AliasChoices`` (an ``AliasPath`` among them too). Each is a name the
        model declares, so it is listed; the values are not."""

        class AliasedMisfit(E2EMisfit):
            nick: int = Field(default=0, alias="nickName")
            level: int = Field(default=0, validation_alias=AliasPath("meta", "level"))
            score: int = Field(default=0, validation_alias=AliasChoices("score", "points"))
            rank: int = Field(default=0, validation_alias=AliasChoices("rank", AliasPath("stats", "rank")))

        record = {
            "email": "a@b.c",
            "password": "c2VjcmV0",
            "nickname": "n",
            "nickName": "SECRETNICK",
            "meta": {"level": "SECRETLEVEL"},
            "points": "SECRETPOINTS",
            "stats": {"rank": "SECRETRANK"},
        }
        with pytest.raises(SurrealDbError) as raised:
            AliasedMisfit._hydrate_record(record, "signin", bound=False)
        message = str(raised.value)
        assert "(nickName: int_parsing; meta: int_parsing; points: int_parsing; stats: int_parsing)" in message
        assert "(undeclared field)" not in message
        _assert_not_quoted(raised.value, "SECRETNICK", "SECRETLEVEL", "SECRETPOINTS", "SECRETRANK", "c2VjcmV0", "a@b.c")

    def test_another_failure_is_named_by_its_type_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def refuse(record: Any) -> Any:
            raise RuntimeError(f"cannot build from {record['password']}")

        monkeypatch.setattr(E2EMisfit, "from_db", refuse)
        with pytest.raises(SurrealDbError) as raised:
            E2EMisfit._hydrate_record({"email": "a@b.c", "password": "hash-c2VjcmV0"}, "signin", bound=False)
        message = str(raised.value)
        assert "so no instance could be built (RuntimeError)" in message
        assert "c2VjcmV0" not in message and "a@b.c" not in message

    def test_an_unbound_refresh_says_the_tokens_are_not_kept(self) -> None:
        """The server rotated the token before the record was read: the one passed in is spent, and the new
        pair is neither returned nor stored."""
        with pytest.raises(SurrealDbError) as raised:
            E2EMisfit._hydrate_record({"email": "a@b.c", "password": "x"}, "refresh", bound=False)
        expected = f"{_UNCHANGED}, but the refresh token passed in is spent and the new token pair is not kept"
        assert expected in str(raised.value)

    def test_a_bound_refresh_says_the_manager_stores_the_new_pair(self) -> None:
        with pytest.raises(SurrealDbError) as raised:
            E2EMisfit._hydrate_record({"email": "a@b.c", "password": "x"}, "refresh", bound=True)
        expected = f"{_NOW_BOUND}, the refresh token passed in is spent, and the manager stores the new token pair"
        assert expected in str(raised.value)


class TestMisfitRecordE2E:
    """On a server: what the error says about the shared session is what the session holds."""

    @pytest.mark.asyncio
    async def test_authenticate_bind_true_keeps_the_previous_identity(self) -> None:
        async with auth_model() as model:
            created = await model.signup(email=_unique_email(), password=AUTH_PASSWORD)
            before = SurrealDBConnectionManager.get_session_token()
            with pytest.raises(SurrealDbError, match=_UNCHANGED):
                await E2EMisfit.authenticate(created.tokens.access, bind=True)
            assert SurrealDBConnectionManager.get_session_token() == before

    @pytest.mark.asyncio
    async def test_signin_bind_false_keeps_the_previous_identity(self) -> None:
        async with auth_model() as model:
            email = _unique_email()
            await model.signup(email=email, password=AUTH_PASSWORD)
            before = SurrealDBConnectionManager.get_session_token()
            with pytest.raises(SurrealDbError, match=_UNCHANGED):
                await E2EMisfit.signin(email=email, password=AUTH_PASSWORD)
            assert SurrealDBConnectionManager.get_session_token() == before

    @pytest.mark.asyncio
    async def test_signin_bind_true_says_the_session_is_signed_in(self) -> None:
        async with auth_model() as model:
            email = _unique_email()
            await model.signup(email=email, password=AUTH_PASSWORD)
            before = SurrealDBConnectionManager.get_session_token()
            try:
                with pytest.raises(SurrealDbError, match=_NOW_BOUND):
                    await E2EMisfit.signin(email=email, password=AUTH_PASSWORD, bind=True)
                assert SurrealDBConnectionManager.get_session_token() not in (None, before)
                me = await SurrealDBConnectionManager.info()
                assert me is not None
                assert me["email"] == email
            finally:
                with contextlib.suppress(Exception):
                    await SurrealDBConnectionManager.invalidate()

    @pytest.mark.asyncio
    async def test_the_error_quotes_no_value_of_the_record(self) -> None:
        async with auth_model() as model:
            email = _unique_email()
            created = await model.signup(email=email, password=AUTH_PASSWORD)
            stored_hash = created.user.password
            with pytest.raises(SurrealDbError) as raised:
                await E2EMisfit.signin(email=email, password=AUTH_PASSWORD)
            message = str(raised.value)
            assert "(nickname: missing)" in message
            assert email not in message and stored_hash not in message
            assert str(created.user.get_id()) not in message


class RefreshAcct(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="RefreshAcct", access_name="e2e_refresh_misfit", with_refresh=True)

    id: str | None = None
    email: str
    password: str


class RefreshMisfit(AuthenticatedUserMixin, BaseSurrealModel):
    """``RefreshAcct``'s table and access, with a required field its records lack."""

    model_config = SurrealConfigDict(table_name="RefreshAcct", access_name="e2e_refresh_misfit", with_refresh=True)

    id: str | None = None
    email: str
    password: str
    nickname: str


class TestRefreshMisfitE2E:
    """``refresh()`` of a record that does not fit the model — SurrealDB 3.x only: the token passed in is spent
    either way, and only ``bind=True`` keeps the new pair (the manager stores it)."""

    @contextlib.asynccontextmanager
    async def _refresh_model(self) -> AsyncIterator[Any]:
        _connect()
        try:
            try:
                await RefreshAcct.define_access()
            except SurrealDbError as exc:
                pytest.skip(f"WITH REFRESH requires SurrealDB 3.x: {exc}")
            yield RefreshAcct
        finally:
            with contextlib.suppress(Exception):
                await SurrealDBConnectionManager.invalidate()
            with contextlib.suppress(Exception):
                client = await SurrealDBConnectionManager.get_client()
                await client.query("REMOVE ACCESS e2e_refresh_misfit ON DATABASE;", {})
                await client.query("REMOVE TABLE RefreshAcct;", {})
            await SurrealDBConnectionManager.unset_connection()

    @pytest.mark.asyncio
    async def test_bind_false_spends_the_token_and_keeps_no_pair(self) -> None:
        async with self._refresh_model() as model:
            created = await model.signup(email=_unique_email(), password=AUTH_PASSWORD)
            assert created.tokens.refresh is not None
            with pytest.raises(SurrealDbError, match="the new token pair is not kept"):
                await RefreshMisfit.refresh(created.tokens.refresh)
            assert SurrealDBConnectionManager.get_refresh_token() is None
            with pytest.raises(SurrealDbAuthenticationError):
                await model.refresh(created.tokens.refresh)  # spent

    @pytest.mark.asyncio
    async def test_bind_true_stores_the_new_pair(self) -> None:
        async with self._refresh_model() as model:
            created = await model.signup(email=_unique_email(), password=AUTH_PASSWORD)
            assert created.tokens.refresh is not None
            with pytest.raises(SurrealDbError, match="the manager stores the new token pair"):
                await RefreshMisfit.refresh(created.tokens.refresh, bind=True)
            stored = SurrealDBConnectionManager.get_refresh_token()
            assert stored not in (None, created.tokens.refresh)
            renewed = await model.refresh(stored)  # the stored pair is live
            assert renewed.tokens.access


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


class TestReviewRegressions:
    """One test per correctness issue raised in the v0.17.0 PR review."""

    def test_primary_key_id_is_refused_at_render_time(self) -> None:
        """SIGNUP never binds $id, so type::thing(table, NONE) would fail server-side."""

        class PkId(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(primary_key="id")
            id: str | None = None
            email: str
            password: str

        with pytest.raises(ValueError, match="primary_key='id'"):
            PkId.access_ddl()

    def test_primary_key_must_be_a_field_signup_binds(self) -> None:
        class PkGhost(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(primary_key="slug")
            id: str | None = None
            slug: str
            email: str
            password: str

        # `slug` IS a signup field, so this one renders.
        assert "type::thing('PkGhost', $slug)" in PkGhost.access_ddl(with_table=False)[0]

    def test_falsy_credentials_are_not_treated_as_missing(self) -> None:
        """0, '' and False are values, not omissions."""

        class Numbered(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(identifier_field="user_number")
            id: str | None = None
            user_number: int
            password: str

        bound = Numbered._auth_variables("signin", {"user_number": 0, "password": "pw"}, require_all=False)
        assert bound == {"user_number": 0, "password": "pw"}

        with pytest.raises(ValueError, match="requires user_number="):
            Numbered._auth_variables("signin", {"password": "pw"}, require_all=False)

    def test_signup_refuses_a_missing_required_field_before_the_exchange(self) -> None:
        """Otherwise the account is created server-side and hydration then fails."""

        class Required(AuthenticatedUserMixin, BaseSurrealModel):
            id: str | None = None
            email: str
            password: str
            name: str  # required, no default

        with pytest.raises(ValueError, match="missing required field"):
            Required._auth_variables("signup", {"email": "a@b.c", "password": "pw"}, require_all=True)

    def test_signup_sends_model_defaults_not_none(self) -> None:
        class Defaulted(AuthenticatedUserMixin, BaseSurrealModel):
            id: str | None = None
            email: str
            password: str
            role: str = "member"

        bound = Defaulted._auth_variables("signup", {"email": "a@b.c", "password": "pw"}, require_all=True)
        assert bound["role"] == "member"

    @pytest.mark.asyncio
    async def test_a_required_field_never_reaches_the_server(self) -> None:
        """The load-bearing half: no orphaned account is left behind."""

        class E2ERequired(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(access_name="e2e_required")
            id: str | None = None
            email: str
            password: str
            name: str

        async with auth_model(E2ERequired) as model:
            email = _unique_email()
            with pytest.raises(ValueError, match="missing required field"):
                await model.signup(email=email, password=AUTH_PASSWORD)

            client = await SurrealDBConnectionManager.get_client()
            rows = await client.query(f"SELECT email FROM {model.get_table_name()};", {})
            assert not rows, "signup created a record despite refusing the call"

    def test_the_refresh_hint_follows_the_flag_not_the_ddl_text(self) -> None:
        """A `refresh_token` field puts 'refresh' in the DDL without asking for the feature."""

        class HasRefreshField(AuthenticatedUserMixin, BaseSurrealModel):
            id: str | None = None
            email: str
            password: str
            refresh_token: str = ""

        assert "REFRESH" in HasRefreshField.access_ddl(with_table=False)[0].upper()
        assert not HasRefreshField._auth_setting("with_refresh", False)

    @pytest.mark.asyncio
    async def test_bad_ddl_is_wrapped_inside_an_interactive_transaction(self) -> None:
        """An interactive tx raises SurrealDbError, which `except ServerError` used to miss."""

        class BadDDL(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(access_name="e2e_bad_ddl")
            id: str | None = None
            email: str
            password: str

        BadDDL.access_ddl = classmethod(  # type: ignore[method-assign]
            lambda cls, **kw: ["DEFINE ACCESS e2e_bad_ddl ON DATABASE TYPE RECORD SIGNUP ( NOT SURREALQL );"]
        )
        _connect()
        try:
            async with SurrealDBConnectionManager.transaction() as probe:
                interactive = probe.is_interactive
            if not interactive:
                # On a buffered transaction (HTTP, or SurrealDB 2.6.x) the statements are only
                # queued, so a rejected one surfaces at COMMIT rather than inside
                # define_access() — documented behaviour, and a different code path.
                pytest.skip("native interactive transactions require SurrealDB 3.x")

            with pytest.raises(SurrealDbError, match="Can't apply access definition"):
                async with SurrealDBConnectionManager.transaction() as tx:
                    await BadDDL.define_access(tx=tx)
        finally:
            with contextlib.suppress(Exception):
                client = await SurrealDBConnectionManager.get_client()
                await client.query("REMOVE ACCESS e2e_bad_ddl ON DATABASE;", {})
            await SurrealDBConnectionManager.unset_connection()

    @pytest.mark.asyncio
    async def test_ephemeral_client_closes_the_socket_when_use_fails(self) -> None:
        """connect() can succeed and use() still fail; that socket must not leak."""
        _connect()
        opened: list[Any] = []
        real_open = SurrealDBConnectionManager._open_client
        saved_open = vars(SurrealDBConnectionManager)["_open_client"]

        async def failing_use(*, signin_as_configured: bool) -> Any:
            client = await real_open(signin_as_configured=signin_as_configured)
            opened.append(client)
            raise SurrealDbConnectionError("Can't connect to the database.")

        try:
            SurrealDBConnectionManager._open_client = failing_use  # type: ignore[method-assign]
            with pytest.raises(SurrealDbConnectionError):
                async with SurrealDBConnectionManager.ephemeral_client():
                    pass  # pragma: no cover
        finally:
            SurrealDBConnectionManager._open_client = saved_open  # type: ignore[method-assign]
            for client in opened:
                with contextlib.suppress(Exception):
                    await client.close()
            await SurrealDBConnectionManager.unset_connection()
