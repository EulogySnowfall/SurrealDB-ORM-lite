"""Model-level authentication — ``AuthenticatedUserMixin`` (v0.17.0).

Where v0.16.0 authenticates the *connection*, this authenticates a *model*: a user model
declares how it is signed in, renders its own ``DEFINE ACCESS`` statement, and hands back
hydrated instances instead of bare tokens.

Every call here runs on its own short-lived connection
(:meth:`SurrealDBConnectionManager.ephemeral_client`), so signing a record user in does **not**
re-identify the client every other model shares — the footgun v0.16.0 documents at length. Pass
``bind=True`` to opt into that older, process-wide behaviour.

This is a leaf module: it imports ``connection_manager`` and ``auth`` at runtime and
``model_base`` only for typing, which keeps the package acyclic (``connection_manager`` already
imports ``auth``, so the mixin could not live there).
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from ._sdk import ServerError
from .auth import AuthTokens, build_auth_payload, wrap_auth_error
from .connection_manager import SurrealDBConnectionManager
from .exceptions import SurrealDbAuthenticationError, SurrealDbError
from .transaction import Transaction
from .utils import validate_alias_name, validate_field_name

if TYPE_CHECKING:  # pragma: no cover
    from typing import Self

__all__ = ["AuthResult", "AuthenticatedUserMixin"]

_M = TypeVar("_M")

#: SurrealQL password-hashing families. The access method calls
#: ``crypto::<algorithm>::generate`` / ``::compare``, so an unknown value would only fail
#: server-side, buried in a DDL string — caught here instead.
_HASH_ALGORITHMS = frozenset({"argon2", "bcrypt", "pbkdf2", "scrypt"})


@dataclass(frozen=True, slots=True)
class AuthResult(Generic[_M]):
    """A model instance together with the tokens the server issued for it.

    ``user`` is hydrated from the record the server reports for the new session, so it carries
    the server-generated id — and, because SurrealDB returns the stored record verbatim, the
    **password hash** rather than the plaintext that was submitted.

    The repr redacts: it delegates to :class:`AuthTokens`, which prints no JWT. That keeps a
    token out of logs, tracebacks and pytest assertion diffs.
    """

    user: _M
    tokens: AuthTokens


class AuthenticatedUserMixin:
    """Give a :class:`BaseSurrealModel` subclass signup / signin / authenticate / refresh.

    Mix it in **before** the base class so its classmethods take precedence::

        class User(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(access_name="account")

            id: str | None = None
            email: str
            password: str

        await User.define_access()                       # once, at start-up
        result = await User.signup(email=…, password=…)  # → AuthResult[User]

    The model must satisfy the ORM's standing contract of declaring either an ``id`` field or
    a ``primary_key`` — otherwise the instance these methods return cannot be constructed at
    all. Prefer the ``id`` field here: SIGNUP lets the server mint the record id, and this is
    what receives it. Configure ``primary_key`` instead when the identity *is* a column (say
    the e-mail address), and the SIGNUP clause will target ``type::thing(…)`` accordingly.
    """

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @classmethod
    def _auth_setting(cls, key: str, default: Any) -> Any:
        """Read one auth key from ``model_config``, falling back to *default*."""
        config = getattr(cls, "model_config", None) or {}
        value = config.get(key, None)
        return default if value is None else value

    @classmethod
    def _model_field_names(cls) -> list[str]:
        """This model's Pydantic field names, excluding computed ones."""
        fields = getattr(cls, "model_fields", {}) or {}
        computed = getattr(cls, "__surreal_computed__", {}) or {}
        return [name for name in fields if name not in computed]

    @classmethod
    def get_access_name(cls) -> str:
        """Name of the ``DEFINE ACCESS`` method; defaults to ``<table>_access``."""
        table = cls.get_table_name()  # type: ignore[attr-defined]
        name = str(cls._auth_setting("access_name", f"{table}_access"))
        validate_alias_name(name)
        return name

    @classmethod
    def _auth_field(cls, key: str, default: str) -> str:
        """Resolve a config key naming a model field, and prove the field exists.

        A typo here would otherwise ship into a ``DEFINE ACCESS`` statement and surface much
        later as a signin that simply never matches anybody.
        """
        name = str(cls._auth_setting(key, default))
        validate_field_name(name)
        known = cls._model_field_names()
        if name not in known:
            raise ValueError(f"{key}={name!r} is not a field of {cls.__name__}. Known fields: {', '.join(known)}.")
        return name

    @classmethod
    def get_identifier_field(cls) -> str:
        """Field a user signs in with — defaults to ``email``."""
        return cls._auth_field("identifier_field", "email")

    @classmethod
    def get_password_field(cls) -> str:
        """Field holding the password hash — defaults to ``password``."""
        return cls._auth_field("password_field", "password")

    @classmethod
    def _get_algorithm(cls) -> str:
        """The ``crypto::`` hashing family the access method uses."""
        algorithm = str(cls._auth_setting("auth_algorithm", "argon2"))
        if algorithm not in _HASH_ALGORITHMS:
            raise ValueError(
                f"auth_algorithm={algorithm!r} is not a SurrealQL hashing family. "
                f"Choose one of: {', '.join(sorted(_HASH_ALGORITHMS))}."
            )
        return algorithm

    # ------------------------------------------------------------------
    # DDL
    # ------------------------------------------------------------------

    @classmethod
    def _signup_fields(cls) -> list[str]:
        """Fields the SIGNUP clause writes: every non-computed field except ``id``."""
        return [name for name in cls._model_field_names() if name != "id"]

    @classmethod
    def access_ddl(cls, *, overwrite: bool = True, with_table: bool = True) -> list[str]:
        """Render this model's authentication DDL. Pure — it touches no database.

        Returns the ``DEFINE TABLE`` statement followed by the ``DEFINE ACCESS`` one, so the
        result can be printed, diffed, or fed to a migration before anything is applied.

        The table statement is included **by default**, and deliberately so. Without
        ``PERMISSIONS FOR select WHERE id = $auth.id`` a signin succeeds and ``$auth`` is set,
        yet ``info()`` quietly returns nothing — the sharpest gotcha of connection-level auth
        (v0.16.0). A model-level API that promises an instance cannot walk into it, so the
        permission ships with the access method.

        Args:
            overwrite: ``True`` (default) emits ``OVERWRITE`` — the model is the source of
                truth, so re-running converges the database onto the code. ``False`` emits
                ``IF NOT EXISTS``, which never disturbs an existing definition.
            with_table: emit the ``DEFINE TABLE … PERMISSIONS`` statement. Pass ``False`` when
                you manage the table yourself (a ``SCHEMAFULL`` table, or wider permissions) —
                then make sure the record can still select itself.

        Returns:
            One or two SurrealQL statements, table first.

        Note:
            ``WITH REFRESH`` is rendered on both DB lines because this is a pure string
            renderer; **SurrealDB 2.6.x cannot parse it**, so applying it there fails. See
            :meth:`define_access`.

        Example:
            >>> User.access_ddl(with_table=False)
            ["DEFINE ACCESS OVERWRITE account ON DATABASE TYPE RECORD SIGNUP ( ... );"]
        """
        table = cls.get_table_name()  # type: ignore[attr-defined]
        validate_alias_name(table)
        identifier = cls.get_identifier_field()
        password = cls.get_password_field()
        algorithm = cls._get_algorithm()
        access = cls.get_access_name()
        clause = "OVERWRITE" if overwrite else "IF NOT EXISTS"

        statements: list[str] = []
        if with_table:
            statements.append(f"DEFINE TABLE {clause} {table} SCHEMALESS PERMISSIONS FOR select, update WHERE id = $auth.id;")

        # The record id follows the model's primary key, because the rest of the ORM does: a
        # random id here would leave User.objects().get(email=…) addressing a record the
        # signup never wrote.
        primary_key = cls.get_index_primary_key()  # type: ignore[attr-defined]
        if primary_key is not None:
            validate_field_name(primary_key)
            target = f"type::thing('{table}', ${primary_key})"
        else:
            target = table

        assignments = ", ".join(
            f"{name} = crypto::{algorithm}::generate(${name})" if name == password else f"{name} = ${name}"
            for name in cls._signup_fields()
        )

        parts = [
            f"DEFINE ACCESS {clause} {access} ON DATABASE TYPE RECORD",
            f"SIGNUP ( CREATE {target} SET {assignments} )",
            f"SIGNIN ( SELECT * FROM {table} WHERE {identifier} = ${identifier} "
            f"AND crypto::{algorithm}::compare({password}, ${password}) )",
        ]

        with_refresh = bool(cls._auth_setting("with_refresh", False))
        if with_refresh:
            parts.append("WITH REFRESH")

        duration = (
            f"DURATION FOR TOKEN {cls._auth_setting('auth_duration_token', '15m')}, "
            f"FOR SESSION {cls._auth_setting('auth_duration_session', '12h')}"
        )
        if with_refresh:
            duration += f", FOR GRANT {cls._auth_setting('auth_duration_grant', '30d')}"
        parts.append(f"{duration};")

        statements.append(" ".join(parts))
        return statements

    @classmethod
    async def define_access(
        cls,
        *,
        overwrite: bool = True,
        with_table: bool = True,
        tx: Transaction | None = None,
    ) -> list[str]:
        """Apply this model's authentication DDL, and return the statements applied.

        Safe at application start-up: with the default ``overwrite=True`` it is idempotent and
        converges the database onto the model.

        Statements run one per call so a failure can name the offending one. The two server
        lines raise different SDK exceptions for the same bad DDL, so both are normalised to
        :class:`SurrealDbError`.

        Args:
            overwrite: see :meth:`access_ddl`.
            with_table: see :meth:`access_ddl`.
            tx: optional transaction to run the DDL in. On a buffered transaction (HTTP or
                SurrealDB 2.6.x) the statements are deferred to commit, so a rejected clause
                surfaces there rather than here.

        Raises:
            SurrealDbError: if the server rejects a statement. The most likely cause on
                **SurrealDB 2.6.x** is ``with_refresh=True``: that line cannot parse
                ``WITH REFRESH`` at all, and the message says so.
        """
        statements = cls.access_ddl(overwrite=overwrite, with_table=with_table)
        # Cheap even inside a transaction: get_client() returns the already-connected client.
        client = await SurrealDBConnectionManager.get_client()
        for statement in statements:
            try:
                if tx is not None:
                    await tx.add(statement, None)
                else:
                    await client.query(statement, {})
            except ServerError as e:
                hint = ""
                if "REFRESH" in statement.upper():
                    hint = (
                        " Note: WITH REFRESH requires SurrealDB 3.x; 2.6.x cannot parse it. "
                        "Set with_refresh=False to support both lines."
                    )
                raise SurrealDbError(f"Can't apply access definition: {statement} -> {e}{hint}") from e
        return statements

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    @classmethod
    def _auth_variables(cls, action: str, values: dict[str, Any], *, require_all: bool) -> dict[str, Any]:
        """Validate caller kwargs against the model, and bind the access method's variables.

        Every field named in the SIGNUP clause must be bound, or the server has no value for a
        ``$variable`` it references — so absent optional fields are sent as ``None`` explicitly
        rather than omitted.
        """
        known = set(cls._signup_fields())
        unknown = sorted(set(values) - known)
        if unknown:
            raise ValueError(
                f"{cls.__name__}.{action}() got {', '.join(repr(k) for k in unknown)}, "
                f"which {'is' if len(unknown) == 1 else 'are'} not a field of the model. "
                f"Known fields: {', '.join(sorted(known))}."
            )

        identifier = cls.get_identifier_field()
        password = cls.get_password_field()
        for required in (identifier, password):
            if not values.get(required):
                raise ValueError(f"{cls.__name__}.{action}() requires {required}=.")

        if require_all:
            return {name: values.get(name) for name in cls._signup_fields()}
        return {identifier: values[identifier], password: values[password]}

    @classmethod
    async def _authenticate_session(
        cls,
        *,
        action: str,
        payload: dict[str, Any] | None,
        token: str | None,
        bind: bool,
    ) -> tuple[Any, AuthTokens | None]:
        """Run one auth exchange on a throwaway connection and read the record back.

        This is the whole isolation story in one place: the client is built, authenticated,
        queried and closed here, so nothing about the shared connection changes unless the
        caller asked for it with ``bind=True``.
        """
        async with SurrealDBConnectionManager.ephemeral_client() as client:
            try:
                tokens: AuthTokens | None = None
                if token is not None:
                    await client.authenticate(token)
                else:
                    assert payload is not None
                    raw = await getattr(client, action)(payload)
                    access = getattr(raw, "access", None)
                    if not access:
                        raise SurrealDbAuthenticationError(f"The server returned no access token for {action}.")
                    tokens = AuthTokens(access=access, refresh=getattr(raw, "refresh", None))
                record = await client.info()
            except SurrealDbAuthenticationError:
                raise
            except Exception as exc:
                raise wrap_auth_error(exc, action) from exc

        if record is None:
            raise SurrealDbAuthenticationError(
                f"{cls.__name__}.{action}() authenticated successfully but the server returned no "
                f"record, so no instance can be built. The usual cause is table PERMISSIONS: the "
                f"record cannot select itself. Grant it with 'DEFINE TABLE "
                f"{cls.get_table_name()} PERMISSIONS FOR select WHERE id = $auth.id;' "  # type: ignore[attr-defined]
                f"or let define_access() do it (with_table=True, the default)."
            )

        if bind and tokens is not None:
            # Adopted on the *shared* connection as a second, deliberate round trip: the
            # ephemeral client is already closed and was never the shared one.
            await SurrealDBConnectionManager.authenticate(tokens.access)

        return cls.from_db(dict(record)), tokens  # type: ignore[attr-defined]

    @classmethod
    async def signup(cls, *, bind: bool = False, **fields: Any) -> "AuthResult[Self]":
        """Register a new record user, and return the created instance with its tokens.

        ::

            result = await User.signup(email="ada@example.com", password="s3cret", name="Ada")
            result.user     # User instance, with the server-generated id
            result.tokens   # AuthTokens

        Runs on its own short-lived connection: the shared client keeps the identity
        ``set_connection()`` gave it. Pass ``bind=True`` to *also* adopt the new token as the
        process-wide session — convenient in a script or a notebook, wrong in a web application
        serving concurrent users.

        Args:
            bind: adopt the resulting token on the shared connection as well.
            **fields: the model's fields. The identifier and password fields are required; the
                rest are optional and reach the server as ``None``.

        Returns:
            An :class:`AuthResult` pairing the hydrated instance with its tokens. The
            instance's password field holds the **hash**, since that is what the server stores.

        Raises:
            ValueError: for an unknown field or a missing required one — before any request.
            SurrealDbAuthenticationError: if the server refuses the signup, or returns no
                record for the new session (the message says how to grant the permission).
        """
        variables = cls._auth_variables("signup", fields, require_all=True)
        payload = build_auth_payload(
            access=cls.get_access_name(),
            variables=variables,
            default_namespace=SurrealDBConnectionManager.get_namespace(),
            default_database=SurrealDBConnectionManager.get_database(),
        )
        user, tokens = await cls._authenticate_session(action="signup", payload=payload, token=None, bind=bind)
        assert tokens is not None
        return AuthResult(user=user, tokens=tokens)

    @classmethod
    async def signin(cls, *, bind: bool = False, **credentials: Any) -> "AuthResult[Self]":
        """Authenticate an existing record user, and return their instance with fresh tokens.

        ::

            result = await User.signin(email="ada@example.com", password="s3cret")

        Only the identifier and password fields are sent — the SIGNIN clause matches on those.
        Isolation and ``bind=`` behave exactly as in :meth:`signup`.

        Raises:
            ValueError: for an unknown or missing field — before any request.
            SurrealDbAuthenticationError: if the credentials are refused. Both DB lines are
                normalised here: the same wrong password surfaces as ``NotFoundError`` on 3.x
                and ``InternalError`` on 2.6.x.
        """
        variables = cls._auth_variables("signin", credentials, require_all=False)
        payload = build_auth_payload(
            access=cls.get_access_name(),
            variables=variables,
            default_namespace=SurrealDBConnectionManager.get_namespace(),
            default_database=SurrealDBConnectionManager.get_database(),
        )
        user, tokens = await cls._authenticate_session(action="signin", payload=payload, token=None, bind=bind)
        assert tokens is not None
        return AuthResult(user=user, tokens=tokens)

    @classmethod
    async def authenticate(cls, token: str, *, bind: bool = False) -> "Self":
        """Resolve a stored JWT to the model instance it belongs to.

        The per-request half of a web login: the browser sends back the token :meth:`signin`
        issued, and this turns it into the current user::

            me = await User.authenticate(request.headers["authorization"])

        Runs on its own connection, so concurrent requests for different users never share an
        identity. Returns the instance directly — there are no new tokens to hand back, since
        the caller already holds the one they passed in.

        Raises:
            SurrealDbAuthenticationError: if the token is malformed, expired or revoked. A
                malformed one is rejected by the SDK's own schema check without ever reaching
                the server, and is normalised here like any other auth failure.
        """
        user, _ = await cls._authenticate_session(action="authenticate", payload=None, token=token, bind=False)
        if bind:
            await SurrealDBConnectionManager.authenticate(token)
        return user  # type: ignore[no-any-return]

    @classmethod
    async def refresh(cls, refresh_token: str, *, bind: bool = False) -> "AuthResult[Self]":
        """Renew a session from a refresh token — **SurrealDB 3.x only**.

        Requires the model to be configured with ``with_refresh=True`` and its access method
        defined on a 3.x server; 2.6.x cannot parse ``WITH REFRESH`` at all, so no refresh
        token ever exists there and this call fails.

        .. warning::
            **Refresh tokens rotate.** A successful exchange kills the token it spent,
            immediately and permanently. Persist ``result.tokens.refresh`` before the next
            request: dropping it logs the user out for good, with nothing raised here to warn
            you.

        Raises:
            SurrealDbAuthenticationError: if the token is spent or expired, or the server does
                not support refresh.
        """
        payload = build_auth_payload(
            access=cls.get_access_name(),
            refresh=refresh_token,
            default_namespace=SurrealDBConnectionManager.get_namespace(),
            default_database=SurrealDBConnectionManager.get_database(),
        )
        user, tokens = await cls._authenticate_session(action="signin", payload=payload, token=None, bind=bind)
        assert tokens is not None
        return AuthResult(user=user, tokens=tokens)
