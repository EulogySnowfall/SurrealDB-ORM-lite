"""Model-level authentication — ``AuthenticatedUserMixin`` (v0.17.0).

Where v0.16.0 authenticates the *connection*, this authenticates a *model*: a user model
declares how it is signed in, renders its own ``DEFINE ACCESS`` statement, and hands back
hydrated instances instead of bare tokens.

Every call here runs on its own short-lived connection
(:meth:`SurrealDBConnectionManager.ephemeral_client`), so signing a record user in does **not**
re-identify the client every other model shares — the footgun v0.16.0 documents at length. Pass
``bind=True`` to opt into that older, process-wide behaviour.

The embedded engine (v0.25.0: ``mem://``, ``surrealkv://<path>`` …) has a single session per
database, so it has no short-lived connection to offer: there, ``bind=True`` is required, and the
exchange runs on the shared session — one call at a time (concurrent calls, and the connection-level
``signin``/``signup``/``authenticate``/``invalidate`` holding the same session, are serialised; the
last one wins), the record checked before the token is adopted, the previous session put back when
anything fails — except, as on a server, a ``signup``/``signin`` that succeeded but whose record does
not fit the model: the session stays signed in as that record, and the error says so. (A ``refresh``
cannot succeed there: the engine's SurrealQL does not parse ``WITH REFRESH``.)

This is a leaf module: it imports ``connection_manager`` and ``auth`` at runtime and
``model_base`` only for typing, which keeps the package acyclic (``connection_manager`` already
imports ``auth``, so the mixin could not live there).
"""

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from pydantic import AliasChoices, AliasPath, ValidationError

from . import cache as _cache
from . import encoding as _encoding
from .auth import AuthTokens, build_auth_payload, wrap_auth_error
from .connection_manager import SurrealDBConnectionManager
from .embedded import model_auth_unsupported
from .exceptions import SurrealDbAuthenticationError, SurrealDbError
from .transaction import Transaction
from .utils import apply_ddl_statements, validate_alias_name, validate_field_name

if TYPE_CHECKING:  # pragma: no cover
    from typing import Self

__all__ = ["AuthResult", "AuthenticatedUserMixin"]

logger = logging.getLogger(__name__)

_M = TypeVar("_M")

#: SurrealQL password-hashing families. The access method calls
#: ``crypto::<algorithm>::generate`` / ``::compare``, so an unknown value would only fail
#: server-side, buried in a DDL string — caught here instead.
_HASH_ALGORITHMS = frozenset({"argon2", "bcrypt", "pbkdf2", "scrypt"})

#: A SurrealQL duration literal (``15m``, ``12h``, ``30d``). Durations are interpolated into the
#: ``DEFINE ACCESS`` statement, which ``define_access()`` runs on the root-authenticated shared
#: client — so, like every other interpolated value here, they are validated rather than trusted.
#: A model config assembled from settings or environment is not an exotic pattern.
_DURATION_RE = re.compile(r"^\d+(ns|us|ms|s|m|h|d|w|y)$")


def _alias_keys(alias: str | AliasPath | AliasChoices | None) -> set[str]:
    """The top-level keys of a record a ``validation_alias`` reads: the string, the first key of an
    ``AliasPath``, those of each ``AliasChoices`` choice."""
    if isinstance(alias, str):
        return {alias}
    if isinstance(alias, AliasPath):
        return {alias.path[0]} if alias.path and isinstance(alias.path[0], str) else set()
    if isinstance(alias, AliasChoices):
        return {key for choice in alias.choices for key in _alias_keys(choice)}
    return set()


def _declared_keys(model: Any) -> frozenset[str]:
    """Every top-level key under which *model* reads a field: its name, ``alias`` and validation aliases.

    These come from the model, never from the record, so naming one quotes nothing from the record.
    """
    keys: set[str] = set()
    for name, field in getattr(model, "model_fields", {}).items():
        keys.add(name)
        if field.alias:
            keys.add(field.alias)
        keys |= _alias_keys(field.validation_alias)
    return frozenset(keys)


def _misfit_details(exc: Exception, model: Any) -> str:
    """Which fields of a record do not fit *model*, without anything taken from the record.

    Each top-level field a Pydantic ``ValidationError`` reports, once and in order, with Pydantic's
    error codes (``nickname: missing; prefs: int_parsing``). A field is named only by a key *model*
    declares (a field name, ``alias`` or validation alias, :func:`_declared_keys`): the first part of
    ``loc`` is otherwise a key of the record — an extra the model forbids (``extra="forbid"``), a typed
    extra (``extra="allow"`` with ``__pydantic_extra__``), a dict a model validator validates itself —
    and is listed as ``(undeclared field)``. A key the model forbids is that placeholder too, even one
    spelled like a declared name; inside a declared field (a sub-model that forbids it), the field is
    named. ``(record)`` is an error of the whole record (a model validator). Nothing else is read from
    the error: a deeper ``loc`` part can be a key of the record, and a message can quote one of its
    values (an unknown discriminator tag, a validator's own text). Any other exception is named by its
    type alone, since its text may quote the record too.
    """
    if isinstance(exc, ValidationError):
        declared = _declared_keys(model)
        codes: dict[str, list[str]] = {}
        for error in exc.errors(include_url=False, include_context=False, include_input=False):
            loc = error["loc"]
            if not loc:
                field = "(record)"
            elif loc[0] in declared and not (error["type"] == "extra_forbidden" and len(loc) == 1):
                field = str(loc[0])
            else:
                field = "(undeclared field)"
            field_codes = codes.setdefault(field, [])
            if error["type"] not in field_codes:
                field_codes.append(error["type"])
        if codes:
            return "; ".join(f"{field}: {', '.join(types)}" for field, types in codes.items())
    return type(exc).__name__


@dataclass(frozen=True, slots=True)
class AuthResult(Generic[_M]):
    """A model instance together with the tokens the server issued for it.

    ``user`` is hydrated from the record the server reports for the new session, so it carries
    the server-generated id — and, because SurrealDB returns the stored record verbatim, the
    **password hash** rather than the plaintext that was submitted.

    The repr keeps the **JWT** out of logs, tracebacks and pytest assertion diffs, by
    delegating to :class:`AuthTokens`. It does not redact ``user``: that renders the model's own
    repr, so the password field's stored hash is visible there. A hash is not a plaintext, but
    do not treat this repr as safe to publish wholesale.
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
        """This model's Pydantic field names, excluding computed ones.

        Computed fields come from ``get_computed_fields()`` — model_base calls it "the single
        source of truth every write path consults", and reading the backing attribute directly
        would let a future rename silently start writing computed fields into SIGNUP.
        """
        fields = getattr(cls, "model_fields", {}) or {}
        computed = cls.get_computed_fields()  # type: ignore[attr-defined]
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
    def _auth_duration(cls, key: str, default: str) -> str:
        """Resolve a duration config key, and prove it is a bare SurrealQL duration literal."""
        value = str(cls._auth_setting(key, default))
        if not _DURATION_RE.match(value):
            raise ValueError(f"{key}={value!r} is not a SurrealQL duration literal (e.g. '15m', '12h', '30d').")
        return value

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
        """Fields the SIGNUP clause writes: every client-owned field except ``id``.

        Server-owned columns are excluded for the same reason ``save()`` omits them: SIGNUP is
        a ``CREATE``, so listing ``created_at = $created_at`` would overwrite the column's
        server-side ``DEFAULT`` with whatever the caller passed — usually ``NONE`` — and
        ``_auth_variables(require_all=True)`` would additionally force every caller to supply a
        value the server is supposed to mint. ``get_server_fields()`` covers computed fields
        too, so this subsumes the exclusion it replaces.
        """
        server_owned = cls.get_server_fields()  # type: ignore[attr-defined]
        return [name for name in cls._model_field_names() if name != "id" and name not in server_owned]

    @classmethod
    def _signup_primary_key(cls) -> str | None:
        """The primary key SIGNUP builds the record id from, or ``None`` for a server id.

        The key has to be *bindable*: SIGNUP renders ``type::thing(table, $pk)``, so a key that
        is not among the fields signup sends would evaluate to ``type::thing(table, NONE)`` and
        fail server-side with an opaque error. ``primary_key="id"`` is the trap — ``id`` is
        deliberately excluded from the signup payload (the server mints it), so it can never be
        bound. Caught here, at render time, rather than on every signup.
        """
        primary_key = cls.get_index_primary_key()  # type: ignore[attr-defined]
        if primary_key is None:
            return None
        validate_field_name(primary_key)
        if primary_key not in cls._signup_fields():
            if primary_key == "id":
                raise ValueError(
                    f"{cls.__name__} sets primary_key='id', which model-level auth cannot use: "
                    f"SIGNUP lets the server mint the record id, so $id is never bound. Drop "
                    f"primary_key and declare an `id` field instead, or point primary_key at a "
                    f"real column such as the identifier field."
                )
            raise ValueError(
                f"{cls.__name__} sets primary_key={primary_key!r}, which is not a field signup "
                f"can bind. Known signup fields: {', '.join(cls._signup_fields())}."
            )
        return str(primary_key)

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
        primary_key = cls._signup_primary_key()
        target = f"type::thing('{table}', ${primary_key})" if primary_key is not None else table

        # Column on the left of each assignment, bound variable on the right: the variable
        # keeps the Python name the caller passes as a keyword argument, while the column is
        # what the rest of the ORM reads and writes (v0.18.0). Rendering the Python name on
        # both sides would make SIGNUP write `password` while save()/merge() write
        # `password_hash` — signin would then keep validating against a hash no ORM write ever
        # updates.
        to_column = cls.to_db_field  # type: ignore[attr-defined]
        assignments = ", ".join(
            f"{to_column(name)} = crypto::{algorithm}::generate(${name})"
            if name == password
            else f"{to_column(name)} = ${name}"
            for name in cls._signup_fields()
        )

        parts = [
            f"DEFINE ACCESS {clause} {access} ON DATABASE TYPE RECORD",
            f"SIGNUP ( CREATE {target} SET {assignments} )",
            f"SIGNIN ( SELECT * FROM {table} WHERE {to_column(identifier)} = ${identifier} "
            f"AND crypto::{algorithm}::compare({to_column(password)}, ${password}) )",
        ]

        with_refresh = bool(cls._auth_setting("with_refresh", False))
        if with_refresh:
            parts.append("WITH REFRESH")

        duration = (
            f"DURATION FOR TOKEN {cls._auth_duration('auth_duration_token', '15m')}, "
            f"FOR SESSION {cls._auth_duration('auth_duration_session', '12h')}"
        )
        if with_refresh:
            duration += f", FOR GRANT {cls._auth_duration('auth_duration_grant', '30d')}"
        parts.append(f"{duration};")

        statements.append(" ".join(parts))
        return statements

    @classmethod
    @_cache.invalidates("cls")
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

        Unlike every other method here, this runs on the **shared** connection and is DDL, so it
        needs an identity allowed to define access methods — typically the root user
        ``set_connection()`` configured. Calling it after a ``bind=True`` signin has re-identified
        that connection as a record user will fail on permissions.

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
        # Gated on the model's own flag, not on the rendered text: a model with a field named
        # `refresh_token` puts "refresh" in the SIGNUP clause without asking for the feature,
        # and would otherwise collect a misleading "3.x required" hint on any rejection.
        hint = ""
        if cls._auth_setting("with_refresh", False):
            hint = (
                " Note: WITH REFRESH requires SurrealDB 3.x; 2.6.x cannot parse it. "
                "Set with_refresh=False to support both lines."
            )
        # Cheap even inside a transaction: get_client() returns the already-connected client.
        client = await cls._connection_manager().get_client()  # type: ignore[attr-defined]
        return await apply_ddl_statements(statements, client=client, tx=tx, what="access", hint=hint)

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
            # `is None`, not falsiness: 0, "" and False are values a caller may legitimately
            # have passed (an integer identifier field, say), and refusing them as "missing"
            # would reject a credential that was in fact supplied.
            if values.get(required) is None:
                raise ValueError(f"{cls.__name__}.{action}() requires {required}=.")

        if not require_all:
            return {identifier: values[identifier], password: values[password]}

        # Every field the SIGNUP clause names must be bound. Resolving that here — against the
        # model's own defaults, and refusing an omitted required field *before* the exchange —
        # is what stops signup from creating the account server-side and only then failing to
        # build the instance, which would leave the caller with neither tokens nor a record
        # they knew was created.
        model_fields = getattr(cls, "model_fields", {}) or {}
        payload: dict[str, Any] = {}
        missing: list[str] = []
        for name in cls._signup_fields():
            if name in values:
                payload[name] = values[name]
                continue
            field = model_fields.get(name)
            if field is None:
                payload[name] = None
            elif field.is_required():
                missing.append(name)
            else:
                payload[name] = field.get_default(call_default_factory=True)
        if missing:
            raise ValueError(
                f"{cls.__name__}.{action}() is missing required field"
                f"{'s' if len(missing) > 1 else ''}: {', '.join(missing)}. "
                f"They have no default, so the instance could not be built from the record "
                f"the server would create."
            )
        return payload

    @classmethod
    async def _authenticate_session(
        cls,
        *,
        action: str,
        payload: dict[str, Any] | None,
        token: str | None,
        bind: bool,
        manager: type[SurrealDBConnectionManager],
        label: str | None = None,
    ) -> tuple[Any, AuthTokens | None]:
        """Run one auth exchange on a throwaway connection and read the record back.

        This is the whole isolation story in one place: the client is built, authenticated,
        queried and closed here, so nothing about the shared connection changes unless the
        caller asked for it with ``bind=True`` — which then adopts the issued tokens, or, for
        ``authenticate``, the caller's token once it resolved to an instance.

        The embedded engine has a single session per database, so there is no throwaway
        connection to run on: ``bind=False`` is refused, and ``bind=True`` runs the exchange on
        the shared session, held exclusively for the whole exchange (:meth:`_authenticate_shared`).

        Args:
            action: the **SDK method** to call (``signup`` / ``signin``), or ``authenticate``.
            manager: the model's connection (v0.29.0), resolved once by the public method: the
                exchange, the shared session it may bind and every check run there.
            label: the **ORM method** to name in error messages, when it differs from *action*.
                :meth:`refresh` rides the SDK's ``signin``, and a caller who asked to refresh
                should not be told that "signin" failed.
        """
        label = label or action
        if manager.is_embedded():
            manager.require_connection()
            if not bind:
                raise SurrealDbError(model_auth_unsupported(cls.__name__, label, manager.get_connection_string()))
            # One exchange at a time on the one shared session, end to end: see _authenticate_shared().
            async with manager._exclusive_embedded_client() as shared:
                if shared is not None:
                    return await cls._authenticate_shared(
                        shared, action=action, payload=payload, token=token, label=label, manager=manager
                    )
            # The URL was re-pointed at a server while this call waited for the engine: serve the
            # configuration as it is now, as get_client() does.
            return await cls._authenticate_session(
                action=action, payload=payload, token=token, bind=bind, manager=manager, label=label
            )
        encoded = _encoding.encode(payload)
        async with manager.ephemeral_client() as client:
            try:
                tokens: AuthTokens | None = None
                if token is not None:
                    await client.authenticate(token)
                else:
                    assert encoded is not None
                    raw = await getattr(client, action)(encoded)
                    access = getattr(raw, "access", None)
                    if not access:
                        raise SurrealDbAuthenticationError(f"The server returned no access token for {label}.")
                    tokens = AuthTokens(access=access, refresh=getattr(raw, "refresh", None))
                record = await client.info()
            except SurrealDbAuthenticationError:
                raise
            except Exception as exc:
                raise wrap_auth_error(exc, label) from exc

        cls._require_record(record, label)

        if bind and tokens is not None:
            # Adopted on the *shared* connection as a second, deliberate round trip: the
            # ephemeral client is already closed and was never the shared one.
            # refresh= because we hold the *matching* pair: without it the shared session
            # would be left unable to renew itself, holding a live rotated token it had
            # silently dropped.
            await manager.authenticate(tokens.access, refresh=tokens.refresh)

        # Bound only when a signup/signin/refresh token was adopted above; authenticate() adopts below.
        user = cls._hydrate_record(record, label, bound=bind and tokens is not None)
        if bind and token is not None:
            # authenticate(): the caller's own token, adopted once it resolved to an instance.
            await manager.authenticate(token)
        return user, tokens

    @classmethod
    async def _authenticate_shared(
        cls,
        client: Any,
        *,
        action: str,
        payload: dict[str, Any] | None,
        token: str | None,
        label: str,
        manager: type[SurrealDBConnectionManager],
    ) -> tuple[Any, AuthTokens | None]:
        """``bind=True`` on the embedded engine: the exchange runs on the shared session itself.

        The caller holds the session exclusively
        (:meth:`SurrealDBConnectionManager._exclusive_embedded_client`), so concurrent ``bind=True``
        calls — any event loop or thread — run one after the other, each reading back the record
        *it* signed in and adopting the token that matches the session; the last one wins. The
        connection-level ``signin``/``signup``/``authenticate``/``invalidate`` hold the same session,
        so they wait for the exchange too, and it for them. Without that, one call's ``info()``
        could read the session another call had just signed in, and the token adopted would not
        be the session's.

        A failed ``bind=True`` must leave the process as it was — what the server path gets for
        free by running the exchange on a throwaway session. So the record is checked **before**
        any token is adopted (for ``authenticate()``, built into the instance too, as on a server),
        and on any failure (cancellation included) the previous session is put back before the
        error propagates: the stored identity, else the in-process owner. An identity set with
        ``store=False`` before the call is not restored: the manager does not know it. One
        exception, as on a server: a ``signup()``/``signin()`` exchange that succeeded adopts its
        token first, so when the record then does not fit the model the session stays signed in as
        that record (the error says so). A ``refresh()`` would too, but cannot succeed on the
        engine: its SurrealQL does not parse ``WITH REFRESH``.
        """
        payload = _encoding.encode(payload)
        tokens: AuthTokens | None = None
        try:
            try:
                if token is not None:
                    await client.authenticate(token)  # the raw SDK call: nothing stored yet
                else:
                    raw = await getattr(client, action)(payload)
                    access = getattr(raw, "access", None)
                    if not access:
                        raise SurrealDbAuthenticationError(f"The server returned no access token for {label}.")
                    tokens = AuthTokens(access=access, refresh=getattr(raw, "refresh", None))
                record = await client.info()
            except SurrealDbAuthenticationError:
                raise
            except Exception as exc:
                raise wrap_auth_error(exc, label) from exc
            cls._require_record(record, label)
            # authenticate(): as on a server, the caller's token is adopted only once it resolved to an
            # instance, so a record that does not fit the model puts the previous session back too.
            user = cls._hydrate_record(record, label, bound=False) if token is not None else None
        except BaseException:
            await cls._restore_shared_session(client, label, manager)
            raise
        adopted = tokens
        if adopted is None:
            assert token is not None  # the authenticate() path: the caller's token is the session's
            adopted = AuthTokens(access=token, refresh=None)
        manager._adopt_tokens(adopted, store=True, operation=label, client=client, embedded=True)
        if token is not None:
            return user, tokens
        # signup()/signin(): as on a server, a record that does not fit the model leaves the session
        # bound: the exchange itself succeeded, and for a signup the account exists (the error says so).
        return cls._hydrate_record(record, label, bound=True), tokens

    @classmethod
    async def _restore_shared_session(cls, client: Any, label: str, manager: type[SurrealDBConnectionManager]) -> None:
        """Put the embedded engine's shared session back after a failed ``bind=True``.

        The owner, the configured selection and the **stored** token — the identity before the
        call, since nothing was adopted. If that fails too, the slot's selection is stale, so the
        next ``get_client()`` resets again before handing the client out: the caller gets the
        original error, and the process never keeps running as the refused user.

        Runs inside the caller's exclusive hold of the session, so it resets directly: the guard
        is not re-entrant. The reset marks the selection stale before its first await, so a
        cancellation during the restore still leaves the next ``get_client()`` to finish it.
        """
        try:
            await manager._reset_embedded_session(client)
        except Exception as exc:
            # Never the token: only the type of what failed.
            logger.warning(
                "%s.%s() failed and the previous embedded session could not be restored (%s); the next get_client() retries",
                cls.__name__,
                label,
                type(exc).__name__,
            )

    @classmethod
    def _require_record(cls, record: Any, label: str) -> None:
        """Refuse an exchange whose session reports no record: no instance could be built from it."""
        if record is None:
            raise SurrealDbAuthenticationError(
                f"{cls.__name__}.{label}() authenticated successfully but the server returned no "
                f"record, so no instance can be built. The usual cause is table PERMISSIONS: the "
                f"record cannot select itself. Grant it with 'DEFINE TABLE "
                f"{cls.get_table_name()} PERMISSIONS FOR select WHERE id = $auth.id;' "  # type: ignore[attr-defined]
                f"or let define_access() do it (with_table=True, the default)."
            )

    @classmethod
    def _hydrate_record(cls, record: Any, label: str, *, bound: bool) -> Any:
        """Build the model instance from the record the session reports.

        A record that does not fit raises a :class:`SurrealDbError` that names the fields that do
        not fit and Pydantic's error codes (``nickname: missing; prefs: int_parsing``), and quotes
        **nothing from the record** — no value, and no key the model does not declare (listed as
        ``(undeclared field)``): Pydantic's own text does (``input_value=…``, the whole record for a
        missing field; a record key in ``loc``; an unknown discriminator tag or a validator's own
        text in the message), and a user's record carries the password hash. For the same reason
        the validation error is not chained as the cause, so a logged traceback does not print it;
        it stays reachable as ``__context__``.

        :param bound: whether the shared session holds the exchange's new identity when the record
            turns out not to fit: ``signup()``/``signin()``/``refresh()`` with ``bind=True``, whose
            token is adopted first. ``authenticate()`` adopts its token only once the instance is
            built (on the embedded engine, a failure puts the previous session back), and
            ``bind=False`` never touches the shared session: both pass ``False``.
        """
        try:
            return cls.from_db(dict(record))  # type: ignore[attr-defined]
        except Exception as exc:
            # The exchange already succeeded, so on signup the account now exists and a token
            # was issued — both of which a bare pydantic ValidationError would hide. Say so, or
            # the caller retries a signup that will then fail as a duplicate; and say what the
            # shared session holds, since that differs from one path to the other. A refresh
            # spent the token passed in: the server rotated it before the record was read.
            if bound:
                session = "The shared session is now signed in as that record"
                aftermath = {
                    "signup": ", and the record has been created",
                    "refresh": ", the refresh token passed in is spent, and the manager stores the new token pair",
                }.get(label, "")
            else:
                session = "The shared session keeps its previous identity"
                aftermath = {
                    "signup": ", but the record has been created",
                    "refresh": ", but the refresh token passed in is spent and the new token pair is not kept",
                }.get(label, "")
            raise SurrealDbError(
                f"{cls.__name__}.{label}() succeeded but the returned record does not fit the "
                f"model, so no instance could be built ({_misfit_details(exc, cls)}). {session}{aftermath} "
                f"— reconcile the model with the table rather than retrying."
            ) from None

    @classmethod
    async def _exchange(
        cls,
        *,
        action: str,
        payload_kwargs: dict[str, Any],
        bind: bool,
        manager: type[SurrealDBConnectionManager],
        label: str | None = None,
    ) -> "AuthResult[Self]":
        """Build the payload, run the exchange, and pair the instance with its tokens.

        The shared body of :meth:`signup`, :meth:`signin` and :meth:`refresh`, which differ only
        in the credentials they send and the name they answer to. Three copies of these five
        steps had already started to drift; one owner makes the next change a decision instead
        of an omission.
        """
        payload = build_auth_payload(
            access=cls.get_access_name(),
            default_namespace=manager.get_namespace(),
            default_database=manager.get_database(),
            **payload_kwargs,
        )
        user, tokens = await cls._authenticate_session(
            action=action, payload=payload, token=None, bind=bind, manager=manager, label=label
        )
        assert tokens is not None, "a successful exchange always yields tokens"
        return AuthResult(user=user, tokens=tokens)

    @classmethod
    @_cache.invalidates("cls")
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

        On the embedded engine (``mem://``, ``surrealkv://<path>`` …) there is no own connection
        to run on — one session per database — so ``bind=True`` is required (without it, a
        :class:`SurrealDbError` before any request): the exchange runs on the shared session,
        for every event loop and thread of the process. Concurrent ``bind=True`` calls — and the
        connection-level ``signin()``/``signup()``/``authenticate()``/``invalidate()`` of
        :class:`SurrealDBConnectionManager`, which hold the same session — are **serialised**:
        each one signs in, reads back its own record and adopts its own token before the next
        starts, so the returned instance is always the caller's, the stored token matches the
        session, and the last call wins. The record is checked before
        the token is adopted, and a failure puts the stored identity back (else the in-process
        owner) before raising; an identity set with ``store=False`` is not restored. One
        exception, as on a server: when the exchange succeeded but the record does not fit the
        model, the session stays signed in as that record (the error says so).

        Args:
            bind: adopt the resulting token on the shared connection as well. On the embedded
                engine it is required, and concurrent ``bind=True`` calls (connection-level auth
                calls included) run one at a time: the last one wins.
            **fields: the model's fields. The identifier and password fields are required; the
                rest are optional and reach the server as ``None``.

        Returns:
            An :class:`AuthResult` pairing the hydrated instance with its tokens. The
            instance's password field holds the **hash**, since that is what the server stores.

        Raises:
            ValueError: for an unknown field or a missing required one — before any request.
            SurrealDbAuthenticationError: if the server refuses the signup, or returns no
                record for the new session (the message says how to grant the permission).
            SurrealDbError: on the embedded engine without ``bind=True``.
        """
        manager: type[SurrealDBConnectionManager] = cls._connection_manager()  # type: ignore[attr-defined]
        manager.require_connection()
        variables = cls._auth_variables("signup", fields, require_all=True)
        return await cls._exchange(action="signup", payload_kwargs={"variables": variables}, bind=bind, manager=manager)

    @classmethod
    async def signin(cls, *, bind: bool = False, **credentials: Any) -> "AuthResult[Self]":
        """Authenticate an existing record user, and return their instance with fresh tokens.

        ::

            result = await User.signin(email="ada@example.com", password="s3cret")

        Only the identifier and password fields are sent — the SIGNIN clause matches on those.
        Isolation and ``bind=`` behave exactly as in :meth:`signup`, on the embedded engine too.

        Raises:
            ValueError: for an unknown or missing field — before any request.
            SurrealDbAuthenticationError: if the credentials are refused. Both DB lines are
                normalised here: the same wrong password surfaces as ``NotFoundError`` on 3.x
                and ``InternalError`` on 2.6.x.
            SurrealDbError: on the embedded engine without ``bind=True``.
        """
        manager: type[SurrealDBConnectionManager] = cls._connection_manager()  # type: ignore[attr-defined]
        manager.require_connection()
        variables = cls._auth_variables("signin", credentials, require_all=False)
        return await cls._exchange(action="signin", payload_kwargs={"variables": variables}, bind=bind, manager=manager)

    @classmethod
    async def authenticate(cls, token: str, *, bind: bool = False) -> "Self":
        """Resolve a stored JWT to the model instance it belongs to.

        The per-request half of a web login: the browser sends back the token :meth:`signin`
        issued, and this turns it into the current user::

            me = await User.authenticate(request.headers["authorization"])

        Runs on its own connection, so concurrent requests for different users never share an
        identity. Returns the instance directly — there are no new tokens to hand back, since
        the caller already holds the one they passed in. ``bind=`` behaves as in :meth:`signup`,
        on the embedded engine too.

        Raises:
            SurrealDbAuthenticationError: if the token is malformed, expired or revoked. A
                malformed one is rejected by the SDK's own schema check without ever reaching
                the server, and is normalised here like any other auth failure.
            SurrealDbError: on the embedded engine without ``bind=True``.
        """
        # On a server, bind=True adopts the token on the shared connection once it resolved to an
        # instance; on the embedded engine the exchange runs on the shared session and adopts the
        # token itself — one SDK authenticate(), not a second one through the manager.
        manager: type[SurrealDBConnectionManager] = cls._connection_manager()  # type: ignore[attr-defined]
        user, _ = await cls._authenticate_session(action="authenticate", payload=None, token=token, bind=bind, manager=manager)
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
            ValueError: if no connection is configured, or the model is not declared with
                ``with_refresh=True`` — both before any request.
            SurrealDbAuthenticationError: if the token is spent or expired, or the server does
                not support refresh.
            SurrealDbError: on the embedded engine without ``bind=True`` (``bind=`` behaves as
                in :meth:`signup`); or when the exchange succeeded but the record does not fit
                the model. The token passed in is then spent all the same: without ``bind`` the
                new pair is not kept either (sign in again), with ``bind=True`` the manager stores
                it (:meth:`SurrealDBConnectionManager.get_refresh_token`) and the shared session
                is signed in as that record. The error says which.
        """
        manager: type[SurrealDBConnectionManager] = cls._connection_manager()  # type: ignore[attr-defined]
        manager.require_connection()
        if not cls._auth_setting("with_refresh", False):
            # Without this the exchange reaches the server and comes back "No record was
            # returned" — indistinguishable from a wrong password, for what is actually a
            # model misconfiguration.
            raise ValueError(
                f"{cls.__name__} is not configured for refresh tokens. Set "
                f"with_refresh=True in its model_config (SurrealDB 3.x only) and re-run "
                f"define_access() before calling refresh()."
            )
        # The SDK renews through signin(); only the wording should say "refresh".
        return await cls._exchange(
            action="signin", payload_kwargs={"refresh": refresh_token}, bind=bind, manager=manager, label="refresh"
        )
