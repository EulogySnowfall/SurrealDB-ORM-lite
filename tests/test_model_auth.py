"""Model-level authentication — AuthenticatedUserMixin (v0.17.0)."""

import os
from typing import Any

import pytest

from surreal_orm_lite import (
    AuthenticatedUserMixin,
    BaseSurrealModel,
    SurrealConfigDict,
    SurrealDBConnectionManager,
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
