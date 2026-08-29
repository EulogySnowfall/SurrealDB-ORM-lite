import asyncio
import contextlib
import logging
import weakref
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any

from pydantic import TypeAdapter, ValidationError

from ._sdk import AsyncSurreal, NotFoundError
from .auth import AuthTokens, build_auth_payload, wrap_auth_error
from .exceptions import (
    SurrealDbAuthenticationError,
    SurrealDbConflictError,
    SurrealDbConnectionError,
    SurrealDbError,
    SurrealDbNotFoundError,
    SurrealDbValidationError,
)
from .functions import build_call_statement, normalize_function_name, parse_function_parameters
from .transaction import BufferedTransaction, InteractiveTransaction, Transaction

logger = logging.getLogger(__name__)


@lru_cache(maxsize=256)
def _cached_type_adapter(return_type: Any) -> TypeAdapter[Any]:
    """Build (once) the ``TypeAdapter`` for *return_type*."""
    return TypeAdapter(return_type)


def _type_adapter(return_type: Any) -> TypeAdapter[Any]:
    """Return the ``TypeAdapter`` for *return_type*, cached when the type is hashable.

    Building an adapter compiles a validator, which is far more expensive than the call it
    guards; every realistic ``return_type`` (a model, a dataclass, ``list[Model]``, ``int``)
    is hashable and hits the cache. Unhashable annotations still work, uncached.
    """
    try:
        return _cached_type_adapter(return_type)
    except TypeError:
        return TypeAdapter(return_type)


class SurrealDBConnectionManager:
    __url: str | None = None
    __user: str | None = None
    __password: str | None = None
    __namespace: str | None = None
    __database: str | None = None

    # One client per event loop, never one client shared between loops. A WebSocket client
    # is bound to the loop it connected on, and handing it to another loop fails with
    # "got Future attached to a different loop" — or, worse, hangs (issue #163). Two
    # successive asyncio.run() calls were enough to hit it. A WeakKeyDictionary means a loop
    # that is garbage-collected takes its entry with it; loops that are merely closed are
    # pruned on the next get_client().
    __clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Any]" = weakref.WeakKeyDictionary()

    # Declared parameter names of stored functions, keyed by (url, namespace, database, fn
    # name). SurrealQL function arguments are positional, so `params=` needs the declaration
    # order; reading it costs one INFO FOR DB, which is worth caching. The url is part of the
    # key because set_url() repoints the manager at another server without clearing the cache:
    # without it, a signature read from one server would silently order `params=` for another.
    __function_signatures: dict[tuple[str | None, str | None, str | None, str], tuple[str, ...]] = {}

    # The identity the manager re-establishes on every new connection. Clients are per event
    # loop and are dropped whenever a loop dies, so without replaying the token a reconnect
    # would silently hand the caller back the configured root identity — a privilege change
    # nobody asked for. The refresh token rides along so an application can renew without
    # keeping the password.
    __session_token: str | None = None
    __refresh_token: str | None = None

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await SurrealDBConnectionManager.close_connection()

    async def __aenter__(self) -> Any:
        return await SurrealDBConnectionManager.get_client()

    @classmethod
    def set_connection(cls, url: str, user: str, password: str, namespace: str, database: str) -> None:
        """
        Set the connection kwargs for the SurrealDB instance.

        :param kwargs: The connection kwargs for the SurrealDB instance.
        """
        cls.__url = url
        cls.__user = user
        cls.__password = password
        cls.__namespace = namespace
        cls.__database = database
        # Signatures belong to the server that declared them; re-pointing the connection must
        # not hand stale ones to the new one.
        cls.clear_function_signature_cache()

    @classmethod
    async def unset_connection(cls) -> None:
        """
        Clear all connection settings and close the connection.
        """
        cls.__url = None
        cls.__user = None
        cls.__password = None
        cls.__namespace = None
        cls.__database = None
        cls.__session_token = None
        cls.__refresh_token = None
        cls.clear_function_signature_cache()
        await cls.close_all_connections()

    @classmethod
    def is_connection_set(cls) -> bool:
        """
        Check if the connection kwargs are set.

        :return: True if the connection kwargs are set, False otherwise.
        """
        return all([cls.__url, cls.__user, cls.__password, cls.__namespace, cls.__database])

    @classmethod
    def __prune_dead_loops(cls) -> None:
        """Drop cached clients whose loop is closed.

        The client cannot be closed from here — ``close()`` would have to be awaited on the
        loop that is already gone — so the reference is dropped and the socket released when
        the loop is finalised.
        """
        dead = [loop for loop in list(cls.__clients) if loop.is_closed()]
        for loop in dead:
            cls.__clients.pop(loop, None)
            logger.debug("Dropped the cached SurrealDB client of a closed event loop.")

    @classmethod
    async def get_client(cls) -> Any:
        """
        Connect to the SurrealDB instance, reusing this event loop's client.

        Each event loop gets its own connection: a client belongs to the loop it was created
        on, so a cached one is only ever handed back to that same loop (issue #163).

        :return: The SurrealDB instance.
        """
        loop = asyncio.get_running_loop()
        cls.__prune_dead_loops()

        existing = cls.__clients.get(loop)
        if existing is not None:
            return existing

        if not cls.is_connection_set():
            raise ValueError("Connection not been set.")

        # After is_connection_set(), these are guaranteed to be non-None
        assert cls.__url is not None
        assert cls.__namespace is not None
        assert cls.__database is not None
        assert cls.__user is not None
        assert cls.__password is not None

        # Establish the connection
        try:
            url = cls.__url
            _client = AsyncSurreal(url)

            # WebSocket connections require explicit connect()
            if url.startswith(("ws://", "wss://")):
                await _client.connect(url)

            await _client.signin({"username": cls.__user, "password": cls.__password})
            await _client.use(cls.__namespace, cls.__database)

            cls.__clients[loop] = _client
            return _client
        except Exception as e:
            logger.warning(f"Can't get connection: {e}")
            stale = cls.__clients.pop(loop, None)
            if stale is not None:  # pragma: no cover
                with contextlib.suppress(NotImplementedError):
                    await stale.close()
            raise SurrealDbConnectionError("Can't connect to the database.") from None

    @classmethod
    async def close_connection(cls) -> None:
        """
        Close this event loop's connection to the SurrealDB instance.

        Only the running loop's client is closed: another loop may still be using its own,
        and closing that one from here is both impossible and wrong. Use
        :meth:`close_all_connections` to tear everything down.
        """
        cls.__prune_dead_loops()
        client = cls.__clients.pop(asyncio.get_running_loop(), None)
        if client is None:
            return

        with contextlib.suppress(NotImplementedError):
            await client.close()

    @classmethod
    async def close_all_connections(cls) -> None:
        """
        Close every cached connection, whatever loop created it.

        A client belonging to another *live* loop cannot be closed from here, so it is
        dropped rather than awaited — the socket is released when that loop is finalised.
        Only the running loop's client is closed properly.
        """
        loop = asyncio.get_running_loop()
        client = cls.__clients.pop(loop, None)
        if client is not None:
            with contextlib.suppress(NotImplementedError):
                await client.close()
        cls.__clients.clear()

    @classmethod
    @contextlib.asynccontextmanager
    async def transaction(cls) -> Any:
        """Atomic transaction. Strategy depends on server capability:

        - WebSocket + SurrealDB 3.x (native ``begin()`` available) →
          ``InteractiveTransaction``: operations run immediately inside the server-side
          transaction (tagged by ``txn_id``); reads inside the transaction see
          uncommitted writes.
        - HTTP, or WebSocket on SurrealDB 2.6.x (native ``begin()`` absent) →
          ``BufferedTransaction``: writes are buffered and flushed as one BEGIN…COMMIT at
          commit (v0.8.0); reads inside the transaction raise.

        Exception semantics: an exception in the body triggers ``cancel()`` and re-raises
        (rollback). A server-side rollback surfaces as ``SurrealDbError`` from
        ``commit()`` BEFORE deferred ``post_*`` signals fire. An exception from a deferred
        handler surfaces AFTER a durable commit (not a rollback).

        SurrealDB has no savepoints/nested transactions; nested ``transaction()`` calls
        open INDEPENDENT transactions, not savepoints.

        Example::

            async with SurrealDBConnectionManager.transaction() as tx:
                users = await User.objects(tx=tx).filter(status="active").exec()
                await User.objects(tx=tx).filter(role="guest").bulk_update(role="member")
        """
        client = await cls.get_client()
        url = cls.__url or ""
        tx: Transaction
        if url.startswith(("ws://", "wss://")):
            try:
                txn_id = await client.begin()
            except NotFoundError:
                txn_id = None
            tx = InteractiveTransaction(client, txn_id) if txn_id is not None else BufferedTransaction(client)
        else:
            tx = BufferedTransaction(client)

        try:
            yield tx
        except Exception:
            await tx.cancel()
            raise
        else:
            # A commit failure (e.g. a server-side conflict surfacing at COMMIT) must also
            # cancel: on the interactive strategy the txn rides the shared connection, so we
            # never leave it half-open. cancel() is best-effort/idempotent on both strategies.
            try:
                await tx.commit()
            except Exception:
                await tx.cancel()
                raise
            await tx.fire_post_commit()

    # ------------------------------------------------------------------
    # Authentication (v0.16.0)
    # ------------------------------------------------------------------

    @classmethod
    async def signin(
        cls,
        *,
        username: str | None = None,
        password: str | None = None,
        access: str | None = None,
        variables: Mapping[str, Any] | None = None,
        refresh: str | None = None,
        namespace: str | None = None,
        database: str | None = None,
        store: bool = True,
    ) -> AuthTokens:
        """Authenticate the connection, and remember the identity for later reconnects.

        Exactly one of three credential shapes::

            # a record user, against a DEFINE ACCESS … TYPE RECORD method
            await SurrealDBConnectionManager.signin(
                access="account", variables={"email": email, "pass": password},
            )

            # a system user (root, or namespace/database level)
            await SurrealDBConnectionManager.signin(username="root", password="root")

            # renew an existing record session — SurrealDB 3.x only
            await SurrealDBConnectionManager.signin(access="account", refresh=stored_refresh)

        ``namespace``/``database`` default to the configured connection's **for record
        access**, which the server requires. A system user gets neither unless you pass them:
        a root user is defined at no level, and sending them would turn a root signin into a
        database-user signin against a user that does not exist.

        .. warning::
            **This changes the identity of the whole connection.** The manager caches one
            client per event loop and every model shares it, so every subsequent ORM
            operation runs as the identity signed in here — not just the caller's. An
            application serving concurrent users must not route them through a single
            connection manager identity.

        .. warning::
            **Refresh tokens rotate.** A successful ``refresh=`` exchange kills the token it
            spent, immediately and permanently. Persist ``tokens.refresh`` before the next
            request: dropping it logs the user out for good, with no error raised at the call
            site to warn you.

        :param store: whether the resulting token becomes the identity replayed on reconnect.
            Pass ``False`` to mint a token for someone else — issuing a JWT for a web client —
            without changing what this process re-authenticates as. The connection's *current*
            session changes either way; that is the SDK's behaviour and the ORM does not hide it.
        :return: the access token, plus the refresh token on SurrealDB 3.x when the access
            method declares ``WITH REFRESH`` (always ``None`` on 2.6.x).
        :raises ValueError: for an invalid credential combination — raised **before** any
            request is issued.
        :raises SurrealDbAuthenticationError: if the server refuses the credentials.
        """
        payload = build_auth_payload(
            username=username,
            password=password,
            access=access,
            variables=variables,
            refresh=refresh,
            namespace=namespace,
            database=database,
            default_namespace=cls.__namespace,
            default_database=cls.__database,
        )
        client = await cls.get_client()
        try:
            tokens = await client.signin(payload)
        except Exception as exc:
            raise wrap_auth_error(exc, "signin") from exc
        return cls._adopt_tokens(tokens, store=store, operation="signin")

    @classmethod
    async def signup(
        cls,
        *,
        access: str | None = None,
        variables: Mapping[str, Any] | None = None,
        namespace: str | None = None,
        database: str | None = None,
        store: bool = True,
    ) -> AuthTokens:
        """Register a new record user through a ``DEFINE ACCESS … TYPE RECORD`` SIGNUP clause.

        ::

            tokens = await SurrealDBConnectionManager.signup(
                access="account", variables={"email": email, "pass": password},
            )

        Only record access is accepted — SurrealDB has no signup for system users. On success
        the connection is left authenticated as the newly created record, exactly as
        :meth:`signin` leaves it; the warnings there apply here too.

        ``access`` and ``variables`` are keyword arguments with no default so that omitting one
        raises a :class:`ValueError` explaining what is missing, rather than a bare ``TypeError``.

        :raises ValueError: if ``access`` or ``variables`` is missing — before any request.
        :raises SurrealDbAuthenticationError: if the server refuses the signup.
        """
        payload = build_auth_payload(
            access=access,
            variables=variables,
            namespace=namespace,
            database=database,
            default_namespace=cls.__namespace,
            default_database=cls.__database,
        )
        client = await cls.get_client()
        try:
            tokens = await client.signup(payload)
        except Exception as exc:
            raise wrap_auth_error(exc, "signup") from exc
        return cls._adopt_tokens(tokens, store=store, operation="signup")

    @classmethod
    def _adopt_tokens(cls, tokens: Any, *, store: bool, operation: str) -> AuthTokens:
        """Convert the SDK's ``Tokens`` to :class:`AuthTokens`, optionally remembering it.

        The SDK types ``access`` as optional; the ORM does not hand back a token-shaped object
        with no token in it, so an empty one is an authentication failure rather than a
        surprise ``None`` three call frames later.
        """
        access = getattr(tokens, "access", None)
        if not access:
            raise SurrealDbAuthenticationError(f"The server returned no access token for {operation}.")

        adopted = AuthTokens(access=access, refresh=getattr(tokens, "refresh", None))
        if store:
            cls.__session_token = adopted.access
            cls.__refresh_token = adopted.refresh
        return adopted

    @classmethod
    async def info(cls, *, return_type: Any = None) -> Any:
        """Return the record the connection is authenticated as, or ``None``.

        ::

            me = await SurrealDBConnectionManager.info()                    # dict | None
            me = await SurrealDBConnectionManager.info(return_type=User)    # User | None

        ``None`` means "no record to report", and it has **two** causes worth telling apart:

        - the session is a system user (root, namespace or database), which is not a record;
        - the session *is* a record user, but the record's table does not grant it ``select``
          on itself, so the server returns nothing. This one is quiet and catches people out:
          the signin succeeded and ``$auth`` is set, yet ``info()`` reports nothing. Grant the
          table something like ``PERMISSIONS FOR select WHERE id = $auth.id``.

        Because that second case is a permissions problem, a ``None`` is passed straight
        through even when *return_type* is given, rather than failing validation against a
        model that was never the culprit.

        .. note::
            After signing in as a system user, this can still report the record from an
            earlier record session: SurrealDB swaps the permissions but leaves ``$auth`` in
            place. Only :meth:`invalidate` really ends a record session.

        :param return_type: a type annotation to coerce the record into — a Pydantic model, a
            dataclass, or anything else Pydantic can adapt. ``None`` returns the raw mapping.
        :raises SurrealDbAuthenticationError: if the session is anonymous (after
            ``invalidate()``), or the server otherwise refuses.
        :raises SurrealDbValidationError: if a non-``None`` record does not fit *return_type*.
        """
        client = await cls.get_client()
        try:
            value = await client.info()
        except Exception as exc:
            raise wrap_auth_error(exc, "info") from exc

        if value is None:
            return None
        # Outside the try: SurrealDbValidationError must not be downgraded to an auth error.
        return cls._coerce_result(value, return_type, "info()")

    @classmethod
    async def call_function(
        cls,
        function: str,
        args: Sequence[Any] | None = None,
        *,
        params: Mapping[str, Any] | None = None,
        return_type: Any = None,
        tx: Transaction | None = None,
    ) -> Any:
        """Call a custom server-side function declared with ``DEFINE FUNCTION fn::…``.

        Arguments are **bound as query parameters**, never formatted into the statement::

            total = await SurrealDBConnectionManager.call_function("fn::cart_total", [cart_id])

        The ``fn::`` prefix is added when absent, so ``"cart_total"`` and ``"fn::cart_total"``
        are the same call. SurrealQL function arguments are **positional**, so *args* is a
        sequence in declaration order.

        The name itself is interpolated (SurrealQL takes no bound parameter in call position)
        and is therefore validated first — an invalid name raises ``ValueError`` before any
        query is issued.

        Pass ``return_type`` to convert the result — any annotation Pydantic can adapt works,
        so a model, a dataclass, a scalar and a generic are all one code path::

            lock = await SurrealDBConnectionManager.call_function(
                "fn::acquire_lock", [table_id, pod_id], return_type=LockResult,
            )
            locks = await SurrealDBConnectionManager.call_function(
                "fn::all_locks", return_type=list[LockResult],
            )

        Or pass ``params`` to name the arguments instead of ordering them. SurrealQL has no
        named arguments, so the ORM reads the function's declared signature (via
        ``INFO FOR DB``, cached) and orders them for you — the mapping's own order is
        irrelevant::

            await SurrealDBConnectionManager.call_function(
                "fn::acquire_lock", params={"pod_id": pod_id, "table_id": table_id},
            )

        :param function: ``"fn::name"``, ``"name"``, or a nested ``"fn::namespace::name"``.
        :param args: positional arguments, in the function's declaration order.
        :param params: named arguments, ordered by the declared signature. Mutually
            exclusive with *args*.
        :param return_type: a type annotation to coerce the result into; ``None`` returns it raw.
        :return: the function's return value; ``None`` when it returns ``NONE``.
        :raises ValueError: if the function name is not an identifier path, if both *args* and
            *params* are given, or if *params* does not match the declared parameters.
        :raises SurrealDbNotFoundError: if the server has no such function.
        :raises SurrealDbValidationError: if the result does not fit ``return_type``.
        :raises SurrealDbError: for any other database failure.
        """
        if args is not None and params is not None:
            raise ValueError(
                "Pass either 'args' (positional) or 'params' (named), not both — they are two "
                "ways to supply the same arguments."
            )
        if return_type is not None and tx is not None and not tx.is_interactive:
            raise ValueError(
                "return_type= cannot be used with a buffered transaction: the call is queued "
                "and returns no value until commit. Drop return_type=, or use a WebSocket "
                "connection to SurrealDB 3.x (interactive transactions)."
            )

        if isinstance(args, str | bytes):
            raise TypeError(
                f"'args' must be a sequence of arguments, not {type(args).__name__!r} — a string "
                "would be bound one character per argument. Wrap it: args=[value]."
            )

        name = normalize_function_name(function)

        client = await cls.get_client()
        values: Sequence[Any] = args if args is not None else []
        if params is not None:
            values = await cls._resolve_named_args(client, name, params)

        statement, variables = build_call_statement(name, values)

        if tx is not None:
            try:
                # Buffered: queued, returns None. Interactive: runs now, returns the value.
                raw = await tx.add(statement, variables)
            except SurrealDbConflictError:
                # A retryable conflict must keep its type: retry_on_conflict dispatches on it.
                raise
            except Exception as exc:
                # Includes the SurrealDbError the transaction layer already raised, so a call
                # to a missing function is still classified as SurrealDbNotFoundError here.
                raise cls._wrap_call_error(exc, name) from exc
            # Coercion happens OUTSIDE the try: SurrealDbValidationError does not subclass
            # SurrealDbError, so catching it here would silently downgrade it.
            return cls._coerce_result(raw, return_type, repr(name))
        try:
            value = await client.query(statement, variables)
        except Exception as exc:
            raise cls._wrap_call_error(exc, name) from exc

        return cls._coerce_result(value, return_type, repr(name))

    @staticmethod
    def _coerce_result(value: Any, return_type: Any, subject: str) -> Any:
        """Convert a server value to *return_type*, or return it untouched.

        One ``TypeAdapter`` covers every shape a caller might ask for — a Pydantic model, a
        dataclass, ``list[Model]``, ``int``, ``datetime`` — so there is no per-kind branching
        to keep in sync. Shared by ``call_function()`` and ``info()``; *subject* is what the
        error message names as the source, already formatted (``repr(name)`` for a stored
        function, ``"info()"`` for the session record).
        """
        if return_type is None:
            return value
        try:
            return _type_adapter(return_type).validate_python(value)
        except ValidationError as exc:
            raise SurrealDbValidationError(f"The value returned by {subject} does not fit {return_type!r}: {exc}") from exc

    @classmethod
    async def _resolve_named_args(cls, client: Any, function: str, params: Mapping[str, Any]) -> list[Any]:
        """Order *params* by the function's declared signature.

        A stale cached signature self-heals: when the keys do not match, the entry is dropped,
        the signature re-read **once**, and only a second mismatch raises. That way a function
        redefined at runtime resolves instead of failing on a cache the caller cannot see.

        The one redefinition this cannot catch is a **reorder** of the same parameter names:
        *params* is a mapping and carries no order of its own, so there is nothing to compare
        the cached order against, and re-reading on every call would defeat the cache. Callers
        that reorder a function's parameters must call
        :meth:`clear_function_signature_cache`.
        """
        key = (cls.__url, cls.__namespace, cls.__database, function)
        was_cached = key in cls.__function_signatures
        expected = await cls._function_signature(client, function)
        if set(params) != set(expected) and was_cached:
            # Only worth a second round trip if the first answer came from the cache; a freshly
            # read signature that already disagrees will not change on re-reading.
            expected = await cls._function_signature(client, function, refresh=True)
        if set(params) != set(expected):
            raise ValueError(
                f"Arguments for {function!r} do not match its declared parameters. "
                f"Expected {list(expected)}, got {sorted(params)}."
            )
        return [params[name] for name in expected]

    @classmethod
    async def _function_signature(cls, client: Any, function: str, refresh: bool = False) -> tuple[str, ...]:
        """Return the declared parameter names of *function*, in order, caching the result.

        The read goes through ``client.query()`` directly and never through an open
        transaction: buffered transactions forbid reads, and routing it through one would make
        ``params=`` unusable inside a transaction for no benefit — a schema read needs no
        transactional isolation.
        """
        key = (cls.__url, cls.__namespace, cls.__database, function)
        if refresh:
            cls.__function_signatures.pop(key, None)
        elif key in cls.__function_signatures:
            return cls.__function_signatures[key]

        try:
            info = await client.query("INFO FOR DB;", {})
        except Exception as exc:
            raise SurrealDbError(f"Could not read the signature of {function!r}: {exc}") from exc

        functions = info.get("functions", {}) if isinstance(info, dict) else {}
        # INFO FOR DB keys functions WITHOUT the fn:: prefix.
        define = functions.get(function[len("fn::") :])
        if not define:
            raise SurrealDbNotFoundError(f"Stored function {function!r} does not exist.")

        signature = parse_function_parameters(str(define))
        cls.__function_signatures[key] = signature
        return signature

    @classmethod
    def clear_function_signature_cache(cls) -> None:
        """Forget every cached stored-function signature.

        Called by :meth:`unset_connection`; also useful in tests, and **required** after
        redefining a function with the same parameter names in a different order — the only
        staleness :meth:`_resolve_named_args` cannot self-heal.
        """
        cls.__function_signatures.clear()

    @classmethod
    def function_signature_cache_size(cls) -> int:
        """Number of cached stored-function signatures (introspection aid)."""
        return len(cls.__function_signatures)

    @staticmethod
    def _wrap_call_error(exc: Exception, function: str) -> Exception:
        """Map an SDK exception raised by a stored-function call to the ORM hierarchy.

        A missing function is reported differently by each DB line — SurrealDB 3.x says
        ``Function 'fn::x' not found: The function 'fn::x' does not exist`` while 2.6.x says
        only ``The function 'fn::x' does not exist`` — so the shared substring is what is
        matched, and callers assert on the exception type rather than the wording.
        """
        message = str(exc)
        missing = ("does not exist" in message or "not found" in message.lower()) and function in message
        if missing:
            return SurrealDbNotFoundError(f"Stored function {function!r} does not exist: {message}")
        return SurrealDbError(f"Call to stored function {function!r} failed: {message}")

    @classmethod
    async def reconnect(cls) -> Any:
        """
        Reconnect to the SurrealDB instance.
        """
        await cls.close_connection()
        return await cls.get_client()

    @classmethod
    async def validate_connection(cls) -> bool:
        """
        Validate the connection to the SurrealDB instance.

        :return: True if the connection is valid, False otherwise.
        """
        try:
            await cls.reconnect()
            return True
        except SurrealDbConnectionError:
            return False

    @classmethod
    def get_connection_string(cls) -> str | None:
        """
        Get the connection string for the SurrealDB instance.

        :return: The connection string for the SurrealDB instance.
        """
        return cls.__url

    @classmethod
    def get_connection_kwargs(cls) -> dict[str, str | None]:
        """
        Get the connection kwargs for the SurrealDB instance.

        :return: The connection kwargs for the SurrealDB instance.
        """
        return {
            "url": cls.__url,
            "user": cls.__user,
            "namespace": cls.__namespace,
            "database": cls.__database,
        }

    @classmethod
    async def set_url(cls, url: str, reconnect: bool = False) -> bool:
        """
        Set the URL for the SurrealDB instance.

        :param url: The URL of the SurrealDB instance.
        """

        if not cls.is_connection_set():
            raise ValueError("You can't change the URL when the others setting are not already set.")

        cls.__url = url

        if reconnect and not await cls.validate_connection():  # pragma: no cover
            cls.__url = None
            return False

        return True

    @classmethod
    async def set_user(cls, user: str, reconnect: bool = False) -> bool:
        """
        Set the username for authentication.

        :param user: The username for authentication.
        """

        if not cls.is_connection_set():
            raise ValueError("You can't change the User when the others setting are not already set.")

        cls.__user = user

        if reconnect and not await cls.validate_connection():  # pragma: no cover
            cls.__user = None
            return False

        return True

    @classmethod
    async def set_password(cls, password: str, reconnect: bool = False) -> bool:
        """
        Set the password for authentication.

        :param password: The password for authentication.
        """

        if not cls.is_connection_set():
            raise ValueError("You can't change the password when the others setting are not already set.")

        cls.__password = password

        if reconnect and not await cls.validate_connection():  # pragma: no cover
            cls.__password = None
            return False

        return True

    @classmethod
    async def set_namespace(cls, namespace: str, reconnect: bool = False) -> bool:
        """
        Set the namespace to use.

        :param namespace: The namespace to use.
        """

        if not cls.is_connection_set():
            raise ValueError("You can't change the namespace when the others setting are not already set.")

        cls.__namespace = namespace

        if reconnect and not await cls.validate_connection():  # pragma: no cover
            cls.__namespace = None
            return False

        return True

    @classmethod
    async def set_database(cls, database: str, reconnect: bool = False) -> bool:
        """
        Set the database to use.

        :param database: The database to use.
        """
        if not cls.is_connection_set():
            raise ValueError("You can't change the database when the others setting are not already set.")

        cls.__database = database

        if reconnect and not await cls.validate_connection():  # pragma: no cover
            cls.__database = None
            return False

        return True

    @classmethod
    def get_url(cls) -> str | None:
        """
        Get the URL of the SurrealDB instance.

        :return: The URL of the SurrealDB instance.
        """
        return cls.__url

    @classmethod
    def get_user(cls) -> str | None:
        """
        Get the username for authentication.

        :return: The username for authentication.
        """
        return cls.__user

    @classmethod
    def get_namespace(cls) -> str | None:
        """
        Get the namespace to use.

        :return: The namespace to use.
        """
        return cls.__namespace

    @classmethod
    def get_database(cls) -> str | None:
        """
        Get the database to use.

        :return: The database to use.
        """
        return cls.__database

    @classmethod
    def is_password_set(cls) -> bool:
        """
        Check if the password is set.

        :return: True if the password is set, False otherwise.
        """
        return cls.__password is not None

    @classmethod
    def is_connected(cls) -> bool:
        """
        Check whether a connection to the SurrealDB instance is established.

        Connections are per event loop (issue #163), so "connected" is answered for the loop
        asking: inside a running loop this reports *that* loop's client, and outside one it
        reports whether any loop still holds a client.

        :return: True if the connection is established, False otherwise.
        """
        cls.__prune_dead_loops()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return len(cls.__clients) > 0

        return cls.__clients.get(loop) is not None
