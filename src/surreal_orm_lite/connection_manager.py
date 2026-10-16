import asyncio
import contextlib
import dataclasses
import hashlib
import logging
import threading
import weakref
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from functools import lru_cache
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter, ValidationError

from . import decoding as _decoding
from . import encoding as _encoding
from ._sdk import AsyncSurreal, NotFoundError
from .auth import AuthTokens, build_auth_payload, wrap_auth_error
from .embedded import ephemeral_unsupported, is_embedded_url, require_supported_url
from .exceptions import (
    SurrealDbAuthenticationError,
    SurrealDbConflictError,
    SurrealDbConnectionError,
    SurrealDbError,
    SurrealDbNotFoundError,
    SurrealDbValidationError,
)
from .functions import build_call_statement, normalize_function_name, parse_function_parameters
from .live import (
    LiveIterator,
    close_all_subscribers,
    is_connection_failure,
    is_dead,
    mark_killed,
    open_stream,
    release_subscribers,
    require_websocket,
)
from .transaction import BufferedTransaction, InteractiveTransaction, Transaction

logger = logging.getLogger(__name__)

# Strong references to fire-and-forget closes, which the event loop alone would let be collected.
_BACKGROUND: set[asyncio.Task[Any]] = set()


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


@dataclasses.dataclass(frozen=True)
class _ManagerState:
    """The connection state ``embedded_database()`` puts back after its block. Secrets stay out of the repr.

    Taken and restored by :meth:`SurrealDBConnectionManager._enter_embedded_block` and
    :meth:`SurrealDBConnectionManager._exit_embedded_block`: the settings, the stored session, the event loops in
    the lost-identity state and the embedded slot (an outer engine is detached for the block, not closed).
    """

    url: str | None
    user: str | None
    password: str | None = dataclasses.field(repr=False)
    namespace: str | None
    database: str | None
    session_token: str | None = dataclasses.field(repr=False)
    refresh_token: str | None = dataclasses.field(repr=False)
    lost_identity: tuple[asyncio.AbstractEventLoop, ...]
    embedded_client: Any
    embedded_url: str | None
    embedded_selection: tuple[str | None, str | None] | None


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

    # One lock per event loop around opening a client. Without it, N callers that find the cache
    # empty at once — at start-up, or every live stream resubscribing after the same drop — each
    # open a connection and the last one wins the cache; the others leak their socket, and a live
    # query started on one of them can never be killed. A lock belongs to its loop, hence per loop.
    __open_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()

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

    # Identity bookkeeping (v0.21.0). A dropped WebSocket is now replaced by a new connection,
    # which comes back as the identity the manager *replays*: the stored session token, else the
    # configured user. That is only sound when it is the identity the dead connection had.
    #
    # * The replay identity is a fingerprint of what a new connection is built from — url,
    #   namespace, database, user and stored token — computed live, so a change that restores
    #   the same values (an idempotent set_connection(), signin then invalidate()) is no change.
    # * Each client carries a stamp ``(fingerprint, replayable)``, set whenever the manager sets
    #   its identity: opened (and replayed), signed in, authenticated, invalidated.
    #   ``replayable`` is False after ``store=False``: the client runs as someone a reconnect
    #   will not restore.
    #
    # A live stream resubscribes only if its client's stamp was replayable and still matches the
    # replay identity. And when a client whose stamp is *not* replayable dies, its loop enters a
    # "lost identity" state in which get_client() raises until the application re-establishes an
    # identity — rather than silently answering as the configured (root) user. Per client and per
    # loop rather than global: clients are per event loop (issue #163).
    __client_identities: "weakref.WeakKeyDictionary[Any, tuple[str, bool]]" = weakref.WeakKeyDictionary()
    __lost_identity: "weakref.WeakSet[asyncio.AbstractEventLoop]" = weakref.WeakSet()

    # The SDK's in-process engine (v0.25.0): ONE client for the whole process, in this slot beside
    # the per-loop cache. Measured with SDK 2.0.0 (spec M6-M8): an embedded client is not bound to a
    # loop — one client serves every loop and thread — while each new client on ``mem://`` is a new,
    # empty database, and two clients on one ``surrealkv://`` path are two engines that miss each
    # other's writes. The per-loop rule of issue #163 would give every loop and thread a database of
    # its own. The lock serialises opening and resetting across loops and threads; it is taken
    # cooperatively (_embedded_guard), never by blocking a loop.
    #
    # A namespace/database change is a new *selection* on that one engine, and it resets the shared
    # session — invalidate(), back to the in-process owner, then use() — instead of replaying the
    # stored token (spec M28, M29): a record session cannot write in another database and reads
    # nothing there, and replaying a token moves the session back to the database it was issued for,
    # silently undoing the selection. ``__embedded_selection`` is ``None`` while a client is in the
    # slot when the selection is *stale* (a reset failed, or is under way): the next get_client()
    # resets again before handing the client out.
    __embedded_client: Any = None
    __embedded_url: str | None = None
    __embedded_selection: tuple[str | None, str | None] | None = None
    __embedded_lock: threading.Lock = threading.Lock()

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await SurrealDBConnectionManager.close_connection()

    async def __aenter__(self) -> Any:
        return await SurrealDBConnectionManager.get_client()

    @classmethod
    def set_connection(
        cls,
        url: str,
        user: str | None = None,
        password: str | None = None,
        namespace: str | None = None,
        database: str | None = None,
    ) -> None:
        """
        Set the connection kwargs for the SurrealDB instance.

        Nothing is validated here: a missing setting makes :meth:`is_connection_set` false and the next
        :meth:`get_client` raise ``ValueError("Connection not been set.")``.

        A server URL (``ws://``, ``wss://``, ``http://``, ``https://``) needs every setting. An **embedded** URL
        — the official SDK's in-process engine: ``mem://``, ``surrealkv://<path>`` and the other schemes the SDK
        routes to it (:meth:`is_embedded`) — needs only the namespace and database: ``user`` and ``password``
        are optional there and **never sent** (the engine has no system user on a fresh database, and its
        anonymous session is the in-process owner). They are kept, so one configuration switches between a
        server and ``mem://`` by its URL alone.

        A URL the installed SDK refuses (``rocksdb://`` with SDK 2.0.0, a typo) is accepted here too and
        reported by the next :meth:`get_client` with a ``SurrealDbConnectionError`` quoting the SDK.

        :param url: The URL of the SurrealDB server, or of the SDK's in-process engine.
        :param user: The user to sign in as (server URLs; ignored by the embedded engine).
        :param password: That user's password (server URLs; ignored by the embedded engine).
        :param namespace: The namespace to use.
        :param database: The database to use.
        """
        cls.__url = url
        cls.__user = user
        cls.__password = password
        cls.__namespace = namespace
        cls.__database = database
        # Re-pointing the manager is an explicit choice of identity: it ends a lost-identity state.
        cls.__lost_identity.clear()
        # Signatures belong to the server that declared them; re-pointing the connection must
        # not hand stale ones to the new one. A JWT is bound even more tightly — to a server,
        # namespace, database and access method — so it is dropped for the same reason, and
        # the alternative is a replay failure surfacing from an unrelated ORM call later.
        cls.clear_session()
        cls.clear_function_signature_cache()

    @classmethod
    async def unset_connection(cls) -> None:
        """
        Clear all connection settings and close the connection (every loop's, and the embedded
        engine's: a ``mem://`` database is discarded).
        """
        cls.__url = None
        cls.__user = None
        cls.__password = None
        cls.__namespace = None
        cls.__database = None
        cls.clear_session()
        cls.clear_function_signature_cache()
        cls.__lost_identity.clear()
        await cls.close_all_connections()

    @classmethod
    def is_connection_set(cls) -> bool:
        """
        Check if the connection kwargs are set: URL, namespace and database, and — for a server URL — user
        and password (the embedded engine signs nobody in).

        :return: True if the connection kwargs are set, False otherwise.
        """
        url = cls.__url
        # bool() on each side: a bare ``and`` chain would return one of the strings, not a bool.
        return bool(url and cls.__namespace and cls.__database) and (
            is_embedded_url(url) or bool(cls.__user and cls.__password)
        )

    @classmethod
    def is_embedded(cls) -> bool:
        """Whether the configured URL addresses the SDK's in-process engine (``mem://``, ``surrealkv://<path>`` …).

        False when no URL is configured. The capability check for code that must run on both kinds of connection
        (live queries, model auth without ``bind``, ``changes(since=<datetime>)`` are not available embedded).
        """
        return is_embedded_url(cls.__url)

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
    def require_connection(cls) -> None:
        """Raise the canonical error if no connection has been configured.

        One owner for this check, so the wording stays identical everywhere it is raised —
        :meth:`get_client`, :meth:`ephemeral_client` and the model-level auth entry points all
        call it, and the tests match the literal string.

        A URL the installed SDK refuses is reported first, whatever else is missing: the
        ``SurrealDbConnectionError`` quoting the SDK says what to fix, "not been set" would not.

        :raises SurrealDbConnectionError: the configured URL is one the SDK refuses (never retryable).
        :raises ValueError: a setting is missing.
        """
        require_supported_url(cls.__url)
        if not cls.is_connection_set():
            raise ValueError("Connection not been set.")

    @classmethod
    async def _open_client(cls, *, signin_as_configured: bool) -> Any:
        """Open one SDK client on the configured URL/namespace/database.

        The single owner of the open handshake, so its quirks — ``connect()`` only for
        WebSocket, ``use()`` after signin — live in one place rather than being re-derived by
        every caller that needs a connection.

        A failure closes whatever was half-opened before raising: ``connect()`` can succeed and
        ``use()`` still fail, and leaving that socket behind leaks one live connection per
        attempt against a flaky server.

        Every client opened here — WebSocket, HTTP and embedded — reads the values SDK 2.0.0 cannot
        decode (zero durations, native sets, degenerate geometries) through the read hook of
        ``decoding.py`` (#235), installed as soon as the client is built, before ``connect()``. Only
        these clients get it: a client the application builds itself with ``AsyncSurreal`` keeps the
        SDK's behaviour.

        On an **embedded** URL the SDK's constructor opens the in-process engine and ``connect()``
        takes no argument; nobody is signed in, whatever *signin_as_configured* says: the engine
        has no system user on a fresh database, and its anonymous session is the in-process owner.

        :param signin_as_configured: sign in as the configured user (server URLs). ``False`` yields
            an **anonymous** client, ready to be authenticated as somebody else.
        :raises SurrealDbConnectionError: the client cannot be opened; on an embedded URL the error
            quotes the engine's own message and is never retryable (a bad path stays bad).
        """
        assert cls.__url is not None
        assert cls.__namespace is not None
        assert cls.__database is not None

        # Every setting is read before the first await, so the client is built from one consistent
        # snapshot — the one _connect_for_loop() fingerprints just before calling this.
        url, namespace, database = cls.__url, cls.__namespace, cls.__database
        user, password = cls.__user, cls.__password
        embedded = is_embedded_url(url)
        client: Any = None
        credentials = _encoding.encode({"username": user, "password": password})
        try:
            client = AsyncSurreal(url)
            # SDK 3.0 (#205): the read hook is a workaround for SDK 2.0.0's decoder; ``install`` is a
            # no-op once the SDK reads every value itself (capability probes), or has another layout.
            _decoding.install(client)
            if embedded:
                await client.connect()
            # WebSocket connections require explicit connect()
            elif url.startswith(("ws://", "wss://")):
                await client.connect(url)
            if signin_as_configured and not embedded:
                assert user is not None
                assert password is not None
                await client.signin(credentials)
            await client.use(namespace, database)
        except Exception as e:
            if embedded:
                if client is not None:
                    with contextlib.suppress(Exception):
                        await client.close()
                error = SurrealDbConnectionError(f"Can't open the embedded database {url!r}: {e}")
                error.retryable = False  # type: ignore[attr-defined]
                raise error from e
            logger.warning(f"Can't get connection: {e}")
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.close()
            error = SurrealDbConnectionError("Can't connect to the database.")
            # Kept as SurrealDbConnectionError for every caller, but a server that *answered*
            # — the configured credentials rejected, the namespace refused — will answer the same
            # on the next attempt, so the reconnect loops (v0.21.0) must not retry it.
            error.retryable = is_connection_failure(e, client)  # type: ignore[attr-defined]
            raise error from None
        except BaseException:
            # A cancellation (a stream stopped mid-reconnect) must not leave a socket behind.
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.close()
            raise
        return client

    @classmethod
    async def get_client(cls) -> Any:
        """
        Connect to the SurrealDB instance, reusing this event loop's client.

        Each event loop gets its own connection: a client belongs to the loop it was created
        on, so a cached one is only ever handed back to that same loop (issue #163).

        A dropped WebSocket is replaced by a new connection, signed in and with the stored
        session replayed (v0.21.0) — unless the dead one ran as an identity a reconnect cannot
        restore (``signin``/``signup``/``authenticate`` with ``store=False``). Then nothing on this
        loop is answered as the configured user until the application chooses: sign in or
        authenticate again, ``invalidate()``, or ``reconnect()``/``close_connection()``.

        On an **embedded** URL (:meth:`is_embedded`) the engine runs in this process and is not
        bound to a loop, so there is **one client for the whole process**, handed to every event
        loop and thread; concurrent first calls open exactly one engine. It is opened without
        signing anybody in (the anonymous session is the in-process owner), and the stored session,
        if any, is replayed as on a server. When the configured namespace or database differs from
        the one the engine last selected, the shared session is reset on the same engine — back to
        the owner, then the new selection — and the stored session ends (a token belongs to the
        database it was issued for): no data is lost. When the configured embedded URL changes, the
        previous engine is closed and the new one opened.

        :return: The SurrealDB instance.
        :raises SurrealDbConnectionError: the configured URL is one the installed SDK refuses
            (``rocksdb://`` with SDK 2.0.0, a typo) — checked before any cached client is handed back,
            and never retryable — or the connection cannot be opened (embedded: "Can't open the
            embedded database …"), or the embedded selection cannot be reset ("Can't select namespace
            …": the engine is kept, and the next call tries again).
        :raises SurrealDbAuthenticationError: in that lost-identity state (server URLs), or when the
            stored session is refused on a new connection (it is then forgotten).
        :raises ValueError: if no connection has been configured.
        """
        return await cls._acquire_client()

    @classmethod
    async def _acquire_client(
        cls,
        *,
        restoring_identity: bool = False,
        still_wanted: Callable[[], bool] | None = None,
    ) -> Any:
        """:meth:`get_client`, with the two knobs the ORM's own callers need.

        :param restoring_identity: the caller is about to set the connection's identity itself
            (signin, signup, authenticate, invalidate), so a lost-identity state does not block it.
        :param still_wanted: for a background reconnect — checked again just before a new client
            is published, so a connection the application closed meanwhile is not reopened behind
            its back. Not used on an embedded URL: an in-process client never drops.
        """
        loop = asyncio.get_running_loop()
        cls.__prune_dead_loops()
        # Before the per-loop fast path: once the URL changed to one the SDK refuses (set_url()),
        # the client cached for the previous URL must not be handed back as if it were its client.
        require_supported_url(cls.__url)
        if is_embedded_url(cls.__url):
            # The lost-identity checks do not apply: an in-process client never drops.
            client = await cls._acquire_embedded_client()
            if client is not None:
                return client
            # The URL was re-pointed at a server while this call waited for the embedded guard:
            # serve the configuration as it is now.
            return await cls._acquire_client(restoring_identity=restoring_identity, still_wanted=still_wanted)
        if not restoring_identity and loop in cls.__lost_identity:
            raise cls._lost_identity_error()

        existing = cls.__clients.get(loop)
        if existing is not None and not is_dead(existing):
            return existing

        lock = cls.__open_locks.get(loop)
        if lock is None:
            lock = cls.__open_locks[loop] = asyncio.Lock()
        async with lock:
            # Whoever held the lock before us may have opened the client already.
            existing = cls.__clients.get(loop)
            if existing is not None and not is_dead(existing):
                return existing
            if existing is not None:
                stamp = cls._identity_of(existing)
                await cls._discard_dead_client(loop, existing)
                if stamp is not None and not stamp[1]:
                    cls.__lost_identity.add(loop)
            if not restoring_identity and loop in cls.__lost_identity:
                raise cls._lost_identity_error()
            return await cls._connect_for_loop(loop, still_wanted)

    @staticmethod
    def _lost_identity_error() -> SurrealDbAuthenticationError:
        return SurrealDbAuthenticationError(
            "The SurrealDB connection on this event loop dropped while it was authenticated as an "
            "identity the connection manager does not store (signin/signup/authenticate with "
            "store=False), so a new connection cannot restore it — and answering as the configured "
            "user instead would silently change privileges. Sign in or authenticate again, call "
            "invalidate(), or reconnect()/close_connection() to continue as the configured user."
        )

    @classmethod
    def _identity_restored(cls) -> None:
        """The application set this loop's server identity itself: a lost-identity state is over.

        Not called for an identity set on the embedded engine's session: it says nothing about the server identity
        the loop should come back as.
        """
        with contextlib.suppress(RuntimeError):
            cls.__lost_identity.discard(asyncio.get_running_loop())

    @classmethod
    async def _discard_dead_client(cls, loop: asyncio.AbstractEventLoop, client: Any) -> None:
        """Forget a client whose WebSocket dropped, so the next caller gets a working one.

        Live-query readers are deliberately **not** released here: each one already learnt of
        the drop from the SDK's receive task, and decides on its own whether to resubscribe. Their
        uuids are recorded as dead, so a late ``subscribe_live()`` on one ends at once instead of
        waiting on the new connection for a subscription that no longer exists.

        The SDK client is deliberately **not** closed: its ``close()`` would reset it to "never
        connected", after which any stale reference to it (an open transaction, say) would
        silently open a fresh, anonymous, uncached socket on its next call. Only its transport is
        closed (:meth:`_close_dead_socket`), so such a call fails loudly instead.
        """
        if cls.__clients.get(loop) is client:
            cls.__clients.pop(loop, None)
        logger.info("The SurrealDB WebSocket connection dropped; opening a new one.")
        await cls._close_dead_socket(client)
        for candidate in (client, getattr(client, "_connection", None)):
            queues = getattr(candidate, "live_queues", None)
            if isinstance(queues, dict):
                for key in list(queues):
                    mark_killed(key)

    @staticmethod
    async def _close_dead_socket(client: Any) -> None:
        """Close the transport of a client whose receive task ended, if it is still open.

        The SDK's receive loop also ends — leaving the socket **open** — on a frame it cannot
        decode or an error frame with no request id. The server then keeps the session, and its
        live queries, running on a socket nobody reads; each resubscribe would add a duplicate.
        Closing the transport makes the server drop them. A no-op on a socket that is already gone.
        """
        for candidate in (client, getattr(client, "_connection", None)):
            socket = getattr(candidate, "socket", None)
            if socket is not None and hasattr(socket, "close"):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(socket.close(), 2.0)
                return

    @staticmethod
    def _fingerprint(url: str | None, namespace: str | None, database: str | None, user: str | None, token: str | None) -> str:
        """The identity a connection built from these settings comes back as — hashed, so no
        credential is kept twice."""
        material = "\x00".join(value or "" for value in (url, namespace, database, user, token))
        return hashlib.sha256(material.encode()).hexdigest()

    @classmethod
    def _replay_identity(cls) -> str:
        """The fingerprint of the identity a new connection would come back as, right now."""
        return cls._fingerprint(cls.__url, cls.__namespace, cls.__database, cls.__user, cls.__session_token)

    @classmethod
    async def _connect_for_loop(cls, loop: asyncio.AbstractEventLoop, still_wanted: Callable[[], bool] | None = None) -> Any:
        """Open, sign in, replay the session and cache a client for *loop*. Caller holds the lock."""
        cls.require_connection()

        # After require_connection(), these are guaranteed to be non-None
        assert cls.__url is not None
        assert cls.__namespace is not None
        assert cls.__database is not None
        assert cls.__user is not None
        assert cls.__password is not None

        # Fingerprinted from the very settings _open_client() reads before its first await: if
        # they change while this client is being built (a signin on another loop,
        # set_connection()…), the stamp describes what the client was really built from, and no
        # longer matches the replay identity.
        replayed = cls._fingerprint(cls.__url, cls.__namespace, cls.__database, cls.__user, cls.__session_token)
        configured = cls._fingerprint(cls.__url, cls.__namespace, cls.__database, cls.__user, None)

        # Establish the connection
        try:
            _client = await cls._open_client(signin_as_configured=True)
        except SurrealDbConnectionError:
            stale = cls.__clients.pop(loop, None)
            if stale is not None:  # pragma: no cover
                with contextlib.suppress(NotImplementedError):
                    await stale.close()
            raise

        return await cls._replay_then_publish(
            _client,
            replayed=replayed,
            configured=configured,
            publish=lambda client, fingerprint: cls._publish(loop, client, fingerprint, still_wanted),
        )

    @classmethod
    async def _replay_then_publish(
        cls, client: Any, *, replayed: str, configured: str, publish: Callable[[Any, str], None]
    ) -> Any:
        """Replay the stored session on a freshly opened *client*, then publish it with the stamp of
        the identity it ended up with. Shared by the per-loop cache and the embedded slot.

        :param replayed: the fingerprint of the configured identity *with* the stored token.
        :param configured: the same without it — what the client is when the token is refused.
        :param publish: stamps and caches the client under the given fingerprint.
        """
        # Deliberately after the client is opened and OUTSIDE the except of the open. A dead token
        # is not a connection failure — the socket is up, the configured credentials worked,
        # only the identity is gone — and collapsing the two would send callers debugging
        # their network. The client stays cached and usable at the configured identity, so
        # the caller can simply sign in again.
        # Published only once the replay has settled, so a rejected token still leaves a usable
        # client behind. Caching it *before* awaiting the replay would let a concurrent
        # get_client() take the fast path and run statements as the configured root user while
        # the record identity is still being established.
        try:
            await cls._replay_session(client)
        except SurrealDbConnectionError:
            # The connection itself failed during the replay: nothing is known about the client's
            # identity, and the stored session was kept for the next attempt.
            with contextlib.suppress(Exception):
                await client.close()
            raise
        except Exception:
            # Refused: the token was forgotten and the client stayed at the configured identity.
            publish(client, configured)
            raise
        except BaseException:
            # Cancelled mid-replay: nobody knows which identity the client ended up with, so it is
            # neither published nor trusted — closed, and the next caller opens a fresh one.
            with contextlib.suppress(Exception):
                await client.close()
            raise
        publish(client, replayed)
        return client

    @classmethod
    def _publish(
        cls, loop: asyncio.AbstractEventLoop, client: Any, fingerprint: str, still_wanted: Callable[[], bool] | None
    ) -> None:
        """Stamp and cache a freshly built client — unless the reconnect that asked for it was
        abandoned meanwhile (the application closed the connection, stopped the stream)."""
        if still_wanted is not None and not still_wanted():
            closing = asyncio.get_running_loop().create_task(client.close())
            _BACKGROUND.add(closing)
            closing.add_done_callback(_BACKGROUND.discard)
            error = SurrealDbConnectionError("The reconnect was abandoned: its stream was torn down.")
            error.retryable = False  # type: ignore[attr-defined]
            raise error
        cls._stamp_identity(client, fingerprint=fingerprint, replayable=True)
        cls.__clients[loop] = client

    @classmethod
    async def _replay_session(cls, client: Any) -> None:
        """Re-establish the stored identity on a freshly built connection.

        Clients are per event loop and are dropped whenever a loop dies (issue #163), so
        without this a ``reconnect()`` would silently hand back the configured root identity
        — a privilege change nobody asked for, and one that fails open rather than closed.

        A rejected token is forgotten before raising, so the next call connects normally
        instead of failing forever on a credential the caller can no longer see.
        """
        token = cls.__session_token
        if token is None:
            return
        # See authenticate(): on HTTP the SDK stamps the token before sending, so a rejected
        # one must be rolled back or the freshly built client is born poisoned.
        previous = getattr(client, "token", None)
        try:
            await client.authenticate(token)
        except Exception as exc:
            with contextlib.suppress(Exception):
                client.token = previous
            if is_connection_failure(exc, client):
                # The network failed, not the token: keep the session for the next attempt. A
                # brief outage during a reconnect must not log the application out for good.
                error = SurrealDbConnectionError("The connection dropped while restoring the session.")
                error.retryable = True  # type: ignore[attr-defined]
                raise error from exc
            cls.__session_token = None
            cls.__refresh_token = None
            raise wrap_auth_error(exc, "restoring the session on a new connection") from exc

    # ------------------------------------------------------------------
    # The embedded engine's slot (v0.25.0)
    # ------------------------------------------------------------------

    @classmethod
    async def _acquire_embedded_client(cls) -> Any:
        """The process-wide client of the embedded engine: opened, re-selected or replaced as needed.

        :return: the client, or ``None`` when the configured URL stopped being embedded while this
            call waited for the guard (the caller then serves the configuration as it is now).
        """
        # Lock-free fast path. The slot is published URL and selection first, client last, and
        # emptied client first; reading the client again last means the URL and selection read in
        # between belong to it, even if another thread replaced the slot meanwhile.
        client = cls.__embedded_client
        if (
            client is not None
            and cls.__embedded_url == cls.__url
            and cls.__embedded_selection == (cls.__namespace, cls.__database)
            and cls.__embedded_client is client
        ):
            return client
        async with cls._embedded_guard():
            return await cls._held_embedded_client()

    @classmethod
    async def _held_embedded_client(cls) -> Any:
        """:meth:`_acquire_embedded_client` for a caller that holds the guard: the slot's client, opened,
        re-selected or replaced as needed, or ``None`` when the configured URL is no longer embedded."""
        if not is_embedded_url(cls.__url):
            return None
        cls.require_connection()
        client = cls.__embedded_client
        if client is not None and cls.__embedded_url == cls.__url:
            selection = cls.__embedded_selection
            if selection == (cls.__namespace, cls.__database):
                return client  # selected by whoever held the guard before us
            if selection is not None:
                # A real change of namespace/database, not a stale marker: the stored session
                # belongs to the previous selection, and replaying it would move the session back.
                cls.clear_session()
            await cls._reset_embedded_session(client)
            return client
        await cls._forget_embedded()
        return await cls._open_embedded()

    @classmethod
    @contextlib.asynccontextmanager
    async def _exclusive_embedded_client(cls) -> AsyncIterator[Any]:
        """Hold the embedded engine's shared session for one identity exchange: model-level ``bind=True``, or a
        connection-level ``signin``/``signup``/``authenticate``/``invalidate`` (:meth:`_identity_client`).

        Yields the slot's client as :meth:`get_client` hands it out, with the guard held for the whole block:
        the exchange, the read-back of its record, the adoption of its token — or the restore after a failure —
        run as one step. Two exchanges on the one session would otherwise interleave, one reading back the
        record the other signed in, or storing a token the session no longer holds; so would a selection reset
        or an ``embedded_database()`` block. Yields ``None`` when the configured URL stopped being embedded while
        this call waited (the caller then serves the configuration as it is now).

        The guard is not re-entrant: inside the block, put a session back with
        :meth:`_reset_embedded_session`, never with a guarded method such as :meth:`get_client`.
        """
        async with cls._embedded_guard():
            yield await cls._held_embedded_client()

    @classmethod
    async def _open_embedded(cls) -> Any:
        """Open the configured embedded URL, replay the stored session and publish the client in the
        slot. Caller holds the guard, and the slot is empty."""
        url, namespace, database = cls.__url, cls.__namespace, cls.__database
        # Fingerprinted from the settings _open_client() reads before its first await, as in
        # _connect_for_loop().
        replayed = cls._fingerprint(url, namespace, database, cls.__user, cls.__session_token)
        configured = cls._fingerprint(url, namespace, database, cls.__user, None)
        client = await cls._open_client(signin_as_configured=False)

        def publish(opened: Any, fingerprint: str) -> None:
            cls._stamp_identity(opened, fingerprint=fingerprint, replayable=True)
            cls.__embedded_url = url
            cls.__embedded_selection = (namespace, database)
            cls.__embedded_client = opened  # last: see the fast path of _acquire_embedded_client()

        return await cls._replay_then_publish(client, replayed=replayed, configured=configured, publish=publish)

    @classmethod
    async def _reset_embedded_session(cls, client: Any) -> None:
        """Re-identify the shared embedded session on the configured selection. Caller holds the guard.

        The one place that does it: back to the in-process owner (``invalidate()``), select the
        configured namespace and database (``use()``), then replay the stored session, if any. Until
        that succeeds the slot's selection is stale, so nobody takes the fast path meanwhile and the
        next :meth:`get_client` resets again: the client is never handed out on a new selection with
        the previous identity, and nothing is closed (no ``mem://`` data lost). Model-level auth calls
        it inside :meth:`_exclusive_embedded_client` to put the stored identity back after a failed
        ``bind=True``.

        :raises SurrealDbConnectionError: ``invalidate()`` or ``use()`` failed (never retryable), or
            the replay hit a connection failure; the selection stays stale.
        :raises SurrealDbAuthenticationError: the stored session was refused; it is forgotten, and
            the session is the owner on the configured selection.
        """
        url, namespace, database = cls.__url, cls.__namespace, cls.__database
        assert namespace is not None
        assert database is not None
        cls.__embedded_selection = None  # stale until done
        replayed = cls._fingerprint(url, namespace, database, cls.__user, cls.__session_token)
        configured = cls._fingerprint(url, namespace, database, cls.__user, None)
        try:
            await client.invalidate()  # the in-process owner
            await client.use(namespace, database)
        except Exception as exc:
            error = SurrealDbConnectionError(
                f"Can't select namespace {namespace!r} and database {database!r} on the embedded database {url!r}: {exc}"
            )
            error.retryable = False  # type: ignore[attr-defined]
            raise error from exc
        try:
            await cls._replay_session(client)
        except SurrealDbConnectionError:
            raise  # the selection stays stale
        except Exception:
            # Refused: the token is forgotten and the session is the owner on the selection — as at open.
            cls._stamp_identity(client, fingerprint=configured, replayable=True)
            cls.__embedded_selection = (namespace, database)
            raise
        cls._stamp_identity(client, fingerprint=replayed, replayable=True)
        cls.__embedded_selection = (namespace, database)

    @classmethod
    @contextlib.asynccontextmanager
    async def _embedded_guard(cls) -> AsyncIterator[None]:
        """Hold the embedded slot's lock.

        A ``threading.Lock``, because the slot is shared by every loop and thread, acquired
        **cooperatively** — without blocking, retried after a short sleep — so a second coroutine
        on the same loop waits without freezing the loop (a blocking acquire would deadlock it).
        Not re-entrant: no guarded section calls another guarded method.
        """
        lock = cls.__embedded_lock
        while not lock.acquire(blocking=False):
            await asyncio.sleep(0.001)
        try:
            yield
        finally:
            lock.release()

    @classmethod
    async def _forget_embedded(cls) -> None:
        """Close the embedded slot's client and empty the slot. Caller holds the guard.

        A no-op when the slot is empty, so a close with no engine open changes nothing it did not
        change before (the stored-function signature cache in particular). Otherwise the slot is
        emptied first — nobody is handed the client while it closes — and the signature cache is
        cleared: a new ``mem://`` database may define the same function name differently.
        """
        client = cls.__embedded_client
        if client is None:
            return
        cls.__embedded_client = None
        cls.__embedded_url = None
        cls.__embedded_selection = None
        with contextlib.suppress(Exception):
            await client.close()
        cls.clear_function_signature_cache()

    @classmethod
    async def _close_embedded(cls) -> None:
        """Close the embedded engine's client, if the slot holds one (from any loop or thread)."""
        if cls.__embedded_client is None:
            return
        async with cls._embedded_guard():
            await cls._forget_embedded()

    @classmethod
    async def _enter_embedded_block(cls, url: str, namespace: str, database: str) -> _ManagerState:
        """Point the manager at an isolated embedded database for ``embedded_database()``; return what to put back.

        One guarded step: snapshot the settings, the stored session, the lost-identity loops and the embedded
        slot; empty the slot **without closing** its client (an outer engine and its data come back after the
        block); then :meth:`set_connection` (which forgets the stored session, the lost-identity loops and the
        stored-function signatures for the block). Doing the detach and the re-point under the guard at once
        means a concurrent first :meth:`get_client` on another thread either completes before the block — its
        client is detached with the rest — or waits and then opens the block's engine: it can never publish an
        engine for the outer settings into the block's slot. Per-loop server clients are neither closed nor used,
        even by a :meth:`close_connection` or :meth:`reconnect` inside the block (they close the engine only).

        Private, for :mod:`surreal_orm_lite.testing`; the caller validates the URL, namespace and database.
        """
        async with cls._embedded_guard():
            state = _ManagerState(
                url=cls.__url,
                user=cls.__user,
                password=cls.__password,
                namespace=cls.__namespace,
                database=cls.__database,
                session_token=cls.__session_token,
                refresh_token=cls.__refresh_token,
                lost_identity=tuple(cls.__lost_identity),
                embedded_client=cls.__embedded_client,
                embedded_url=cls.__embedded_url,
                embedded_selection=cls.__embedded_selection,
            )
            cls.__embedded_client = None  # first: see the fast path of _acquire_embedded_client()
            cls.__embedded_url = None
            cls.__embedded_selection = None
            cls.set_connection(url=url, namespace=namespace, database=database)
        return state

    @classmethod
    async def _exit_embedded_block(cls, state: _ManagerState) -> None:
        """Close the block's engine and put back everything :meth:`_enter_embedded_block` snapshotted.

        One guarded step. The event loops that were in the lost-identity state before the block are put back in
        it (unless closed since): the block's :meth:`set_connection` must not silently lift that guard. The
        stored-function signatures are forgotten (the block's belong to its discarded database). Everything is
        restored before the block's client is closed, so even an interrupted close leaves the previous state in
        place; with the surrealdb SDK 2.x the engine itself stops once nothing refers to that client.
        """
        async with cls._embedded_guard():
            block_client = cls.__embedded_client  # whatever the block left in the slot
            cls.__url = state.url
            cls.__user = state.user
            cls.__password = state.password
            cls.__namespace = state.namespace
            cls.__database = state.database
            cls.__session_token = state.session_token
            cls.__refresh_token = state.refresh_token
            cls.__lost_identity.clear()
            for loop in state.lost_identity:
                if not loop.is_closed():
                    cls.__lost_identity.add(loop)
            cls.__embedded_url = state.embedded_url
            cls.__embedded_selection = state.embedded_selection
            cls.__embedded_client = state.embedded_client  # last: see the fast path of _acquire_embedded_client()
            cls.clear_function_signature_cache()
            if block_client is not None and block_client is not state.embedded_client:
                with contextlib.suppress(Exception):
                    await block_client.close()

    @classmethod
    async def close_connection(cls) -> None:
        """
        Close this event loop's connection to the SurrealDB instance.

        Only the running loop's client is closed: another loop may still be using its own,
        and closing that one from here is both impossible and wrong. Use
        :meth:`close_all_connections` to tear everything down.

        The embedded engine's client — one for the whole process (:meth:`get_client`) — is closed
        too, from whichever loop or thread calls this: a ``mem://`` database is discarded with it
        and the next call opens a new, empty one; a file-backed one (``surrealkv://``, ``file://``)
        reopens with its data. With the surrealdb SDK 2.x, closing does not stop an in-process
        engine: a client obtained before the close is still a running database. Do not use it after
        the close, and drop it (``del client``) before reopening the same ``surrealkv://`` path —
        two engines on one path miss each other's writes.

        While an embedded URL is configured, **only the engine** is closed: the running loop's
        server client, its live queries and its identity (even one set with ``store=False``) are
        left as they are, for when a server URL is configured back (so :meth:`is_connected` stays
        true on a loop that keeps one), and so is a lost-identity state of the loop's server
        connection. :meth:`close_all_connections` and :meth:`unset_connection` close the server
        clients too, and end that state.
        """
        if cls.is_embedded():
            # Server clients are untouched while an embedded URL is configured: closing the engine says
            # nothing about the server connection, its live readers or the identity it should come back as.
            await cls._close_embedded()
            return
        cls.__prune_dead_loops()
        # Before the socket goes: a live-query reader is parked on a queue nothing will ever
        # feed once the connection is gone, so releasing them has to happen here.
        close_all_subscribers()
        # Closing is an explicit choice: the next connection is the configured identity.
        cls._identity_restored()
        await cls._close_embedded()
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

        The embedded engine's client is closed too, as by :meth:`close_connection` (a ``mem://``
        database is discarded with it).
        """
        loop = asyncio.get_running_loop()
        # Every loop's client is dropped below, so every loop's readers are released too —
        # another loop's through its own thread, since only that loop may wake its queues.
        close_all_subscribers(every_loop=True)
        cls.__lost_identity.clear()
        await cls._close_embedded()
        client = cls.__clients.pop(loop, None)
        if client is not None:
            with contextlib.suppress(NotImplementedError):
                await client.close()
        cls.__clients.clear()

    @classmethod
    @contextlib.asynccontextmanager
    async def ephemeral_client(cls) -> AsyncIterator[Any]:
        """A short-lived, **anonymous** client on the configured URL/namespace/database.

        Unlike :meth:`get_client`, this never enters the per-event-loop cache and never signs
        in as the configured user — it is handed to the caller anonymous, ready to be
        authenticated as somebody else. That is what lets model-level auth (v0.17.0) sign a
        record user in without re-identifying the connection every other model shares.

        Closed on exit, including when the body raises. Closing is best-effort: a connection
        the server already dropped must not turn into an error the caller never caused.

        Not available on the embedded engine (``mem://``, ``surrealkv://<path>`` …): every
        in-process client is a database of its own, so a second one could not see the configured
        database. Refused before anything is opened; use :meth:`get_client`.

        :raises ValueError: if no connection has been configured.
        :raises SurrealDbConnectionError: if the configured URL is one the installed SDK refuses, or the
            connection cannot be opened.
        :raises SurrealDbError: if the configured URL addresses the embedded engine.
        """
        cls.require_connection()
        if cls.is_embedded():
            raise SurrealDbError(ephemeral_unsupported(cls.__url))
        client = await cls._open_client(signin_as_configured=False)
        try:
            yield client
        finally:
            with contextlib.suppress(Exception):
                await client.close()

    @classmethod
    @contextlib.asynccontextmanager
    async def transaction(cls) -> Any:
        """Atomic transaction. Strategy depends on server capability:

        - WebSocket + SurrealDB 3.x (native ``begin()`` available) →
          ``InteractiveTransaction``: operations run immediately inside the server-side
          transaction (tagged by ``txn_id``); reads inside the transaction see
          uncommitted writes.
        - HTTP, the embedded engine, or WebSocket on SurrealDB 2.x (native ``begin()`` absent)
          → ``BufferedTransaction``: writes are buffered and flushed as one BEGIN…COMMIT at
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
    @contextlib.asynccontextmanager
    async def _identity_client(cls) -> AsyncIterator[tuple[Any, bool]]:
        """The client whose identity :meth:`signin`, :meth:`signup`, :meth:`authenticate` or :meth:`invalidate`
        sets, and whether it is the embedded engine's shared session.

        On a server URL, this event loop's client; a lost-identity state does not block it, since the caller is
        setting the identity. On an embedded URL, the engine's one session, **held** for the whole block
        (:meth:`_exclusive_embedded_client`): the SDK exchange and the adoption of its token — for
        ``invalidate``, the return to the owner and the re-selection — run as one step. A model-level ``bind=True``
        exchange holds the same session from its sign-in to the adoption of its token, so neither can land in the
        middle of the other: without that, the exchange would read back the record signed in here, or store a
        token the session no longer holds. When the URL was re-pointed at a server while this call waited, that
        server is served, as :meth:`get_client` does.

        The guard is not re-entrant: inside the block, call no guarded method (:meth:`get_client`,
        :meth:`close_connection` …).
        """
        while is_embedded_url(cls.__url):
            async with cls._exclusive_embedded_client() as shared:
                if shared is not None:
                    yield shared, True
                    return
        yield await cls._acquire_client(restoring_identity=True), False

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
            connection manager identity. On the embedded engine (``mem://``,
            ``surrealkv://<path>`` …) it is the identity of the whole process — every event
            loop and thread — since one in-process client serves them all. There, the call
            holds that session from the sign-in to the adoption of the token, so it runs
            one at a time with model-level ``bind=True`` calls and the other connection-level
            identity methods: the last one wins, and the stored token is the session's.

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
        payload = _encoding.encode(
            build_auth_payload(
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
        )
        async with cls._identity_client() as (client, embedded):
            try:
                tokens = await client.signin(payload)
            except Exception as exc:
                raise wrap_auth_error(exc, "signin") from exc
            return cls._adopt_tokens(tokens, store=store, operation="signin", client=client, embedded=embedded)

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
        :meth:`signin` leaves it; the warnings there apply here too — on the embedded engine,
        for the whole process (every event loop and thread).

        ``access`` and ``variables`` are declared optional in the signature so that omitting one
        raises a :class:`ValueError` explaining what is missing, rather than a bare
        ``TypeError``. Both are required in practice.

        :raises ValueError: if ``access`` or ``variables`` is missing — before any request.
        :raises SurrealDbAuthenticationError: if the server refuses the signup.
        """
        payload = _encoding.encode(
            build_auth_payload(
                access=access,
                variables=variables,
                namespace=namespace,
                database=database,
                default_namespace=cls.__namespace,
                default_database=cls.__database,
            )
        )
        async with cls._identity_client() as (client, embedded):
            try:
                tokens = await client.signup(payload)
            except Exception as exc:
                raise wrap_auth_error(exc, "signup") from exc
            return cls._adopt_tokens(tokens, store=store, operation="signup", client=client, embedded=embedded)

    @classmethod
    def _adopt_tokens(cls, tokens: Any, *, store: bool, operation: str, client: Any, embedded: bool) -> AuthTokens:
        """Convert the SDK's ``Tokens`` to :class:`AuthTokens`, optionally remembering it.

        The SDK types ``access`` as optional; the ORM does not hand back a token-shaped object
        with no token in it, so an empty one is an authentication failure rather than a
        surprise ``None`` three call frames later.

        :param embedded: whether *client* is the embedded engine's shared session. An identity set
            there leaves this loop's lost-identity state as it is (:meth:`_identity_restored` is a
            server matter), as :meth:`close_connection` on an embedded URL does.
        """
        access = getattr(tokens, "access", None)
        if not access:
            raise SurrealDbAuthenticationError(f"The server returned no access token for {operation}.")

        adopted = AuthTokens(access=access, refresh=getattr(tokens, "refresh", None))
        if store:
            cls.__session_token = adopted.access
            cls.__refresh_token = adopted.refresh
        cls._stamp_identity(client, replayable=store)
        if not embedded:
            cls._identity_restored()
        return adopted

    @classmethod
    def _stamp_identity(cls, client: Any, *, replayable: bool, fingerprint: str | None = None) -> None:
        """Record that the manager just set *client*'s identity, and whether a reconnect restores it.

        *fingerprint* is what the client was built from when that was read before an await; by
        default, the current replay identity.
        """
        stamp = (cls._replay_identity() if fingerprint is None else fingerprint, replayable)
        with contextlib.suppress(TypeError):  # an object that cannot be weakly referenced
            cls.__client_identities[client] = stamp

    @classmethod
    def _identity_of(cls, client: Any) -> tuple[str, bool] | None:
        """*client*'s identity stamp, or ``None`` for a client the manager did not identify."""
        try:
            return cls.__client_identities.get(client)
        except TypeError:
            return None

    @classmethod
    async def authenticate(cls, token: str, *, store: bool = True, refresh: str | None = None) -> None:
        """Authenticate the connection with a JWT obtained earlier.

        The other half of handing ``tokens.access`` to a web client: give it back here on the
        next request and the connection resumes that identity.

        .. warning::
            Like :meth:`signin`, this changes the identity of the whole connection — every
            model shares it. On the embedded engine, of the whole process — every event loop
            and thread — and, as there, the call runs one at a time with model-level
            ``bind=True`` calls and the other connection-level identity methods.

        A failure changes nothing: the previously stored token is left in place, because the
        call that failed never took effect. (Only the reconnect replay drops a stored token,
        and only after the server has rejected the one it was holding.)

        :param store: whether to adopt this token as the identity replayed on reconnect.
        :param refresh: the refresh token that belongs with *token*, when the caller holds the
            matching pair (model-level auth does). Omitted, any previously stored refresh token
            is dropped — a JWT handed in from outside brings none, and pairing a stale one with
            a new identity is worse than having none.
        :raises SurrealDbAuthenticationError: if the token is malformed, expired or revoked.
            A malformed one never reaches the server — the SDK rejects its shape with a plain
            ``ValueError``, which is normalised here like any other authentication failure.
        """
        async with cls._identity_client() as (client, embedded):
            # The SDK's HTTP connection assigns `self.token` BEFORE sending, unlike the WebSocket
            # one. So a rejected token stays stamped on the client and every later request comes
            # back 401 — and since the manager caches that client for the life of the event loop,
            # one bad JWT would break the application permanently. Restore what was there.
            previous = getattr(client, "token", None)
            try:
                await client.authenticate(token)
            except Exception as exc:
                with contextlib.suppress(Exception):
                    client.token = previous
                raise wrap_auth_error(exc, "authenticate") from exc

            if store:
                cls.__session_token = token
                # A JWT handed in from outside brings no refresh token with it, so the previous
                # session's one is dropped rather than left paired with an identity it cannot
                # renew. A caller that *does* hold the matching pair passes refresh=.
                cls.__refresh_token = refresh
            cls._stamp_identity(client, replayable=store)
            if not embedded:
                cls._identity_restored()

    @classmethod
    async def invalidate(cls) -> None:
        """End the current session and return the connection to its configured identity.

        ::

            await SurrealDBConnectionManager.invalidate()   # log the record user out

        This is the only portable way to end a record session. Up to SurrealDB 3.2.x (and on
        the whole 2.x line) signing in as a system user swaps the permissions but leaves
        ``$auth`` pointing at the record, so ``info()`` would keep reporting it; 3.3.0 clears
        the identity on such a signin, but code that must run on every supported line should
        not rely on that.

        The SDK's ``invalidate()`` on its own leaves the session **anonymous**, and since the
        manager caches one client per event loop, every later ORM call on that connection —
        one the caller never closed — would fail. So the stored token is forgotten and the
        credentials given to :meth:`set_connection` are signed in again: "log the record user
        out, back to the application's own identity", not "break the connection".

        On the embedded engine (``mem://``, ``surrealkv://<path>`` …) nobody is signed in: the
        anonymous session *is* the in-process owner, with full access, so the stored token is
        forgotten, the configured namespace and database are selected again, and :meth:`info`
        then returns ``None``. It is the session of the whole process — every event loop and
        thread — held from the ``invalidate()`` to the new selection, so the call runs one at a
        time with model-level ``bind=True`` calls and the other connection-level identity
        methods. If that selection fails, nothing is closed (no ``mem://`` data is lost): the
        session is already the owner, and the next :meth:`get_client` selects again. Only the
        engine's session changes: an event loop whose server connection dropped while signed in
        with ``store=False`` still raises once a server URL is configured back — as after
        :meth:`close_connection` on the engine, or any identity set on it.

        :raises SurrealDbAuthenticationError: if the server refuses either step.
        """
        # The kind of session to restore is the one of the connection the client came from.
        async with cls._identity_client() as (client, embedded):
            try:
                await client.invalidate()
            except Exception as exc:
                raise wrap_auth_error(exc, "invalidate") from exc

            cls.clear_session()

            if embedded:
                # Still holding the session: nobody signs in between the owner and the selection.
                await cls._reselect_after_invalidate(client)
                return

        credentials = _encoding.encode({"username": cls.__user, "password": cls.__password})
        try:
            await client.signin(credentials)
            if cls.__namespace is not None and cls.__database is not None:
                await client.use(cls.__namespace, cls.__database)
            # Back at the configured identity — exactly what a reconnect restores.
            cls._stamp_identity(client, replayable=True)
            cls._identity_restored()
        except Exception as exc:
            # The session is anonymous at this point — exactly the state this method exists to
            # avoid. Drop the client so the next get_client() rebuilds at the configured
            # identity, rather than handing every later call a dead connection.
            with contextlib.suppress(Exception):
                await cls.close_connection()
            raise wrap_auth_error(exc, "invalidate (restoring the configured identity)") from exc

    @classmethod
    async def _reselect_after_invalidate(cls, client: Any) -> None:
        """The embedded half of :meth:`invalidate`, once the SDK's ``invalidate()`` succeeded: the
        session is the in-process owner, and only the selection is left to restore. Caller holds
        the guard (:meth:`_identity_client`), from the ``invalidate()`` on: the guard is not
        re-entrant.

        No configured sign-in (the engine has no system user on a fresh database, and the owner is
        what a reconnect restores). The slot's selection is stale until ``use()`` succeeds, so
        nobody takes the fast path meanwhile; on failure it stays stale — the next
        :meth:`get_client` selects again — and nothing is closed: :meth:`close_connection` would
        discard a ``mem://`` database (spec D18).
        """
        held = cls.__embedded_client is client
        if held:
            cls.__embedded_selection = None
        namespace, database = cls.__namespace, cls.__database
        try:
            if namespace is not None and database is not None:
                await client.use(namespace, database)
        except Exception as exc:
            raise wrap_auth_error(exc, "invalidate (restoring the configured identity)") from exc
        if held:
            cls.__embedded_selection = (namespace, database)
        # Back at the configured identity — exactly what a reopen restores. A server loop's
        # lost-identity state is left as it is: the engine's owner says nothing about it.
        cls._stamp_identity(client, replayable=True)

    @classmethod
    def clear_session(cls) -> None:
        """Forget the stored tokens without touching the server or the connection settings.

        The local counterpart to :meth:`invalidate`: that one ends the session on the server
        and costs two round trips, while this simply stops the manager replaying an identity
        on the next connection. Useful when an application drops a user locally, and in tests
        that must not leak a token into the next one.

        The **current** connection keeps whatever identity it already has; only the replay is
        forgotten. Call :meth:`invalidate` when you mean to log out for real.
        """
        cls.__session_token = None
        cls.__refresh_token = None

    @classmethod
    def is_authenticated(cls) -> bool:
        """Whether the manager holds a session token it will replay on reconnect.

        This answers "has someone signed in through the ORM", **not** "is the connection
        usable": a connection with no session token is still authenticated as the configured
        user and works normally.

        :return: True if a session token is stored, False otherwise.
        """
        return cls.__session_token is not None

    @classmethod
    def get_session_token(cls) -> str | None:
        """The stored access token (JWT), or ``None``.

        Useful for putting the token in a web session so a later request can restore the
        identity with :meth:`authenticate`. It is a credential — do not log it.

        :return: The stored access token, or None if no session is held.
        """
        return cls.__session_token

    @classmethod
    def get_refresh_token(cls) -> str | None:
        """The stored refresh token, or ``None``.

        Populated only on SurrealDB 3.x, and only when the access method declares
        ``WITH REFRESH``; 2.6.x cannot parse that clause, so it is always ``None`` there.

        :return: The stored refresh token, or None if there is none.
        """
        return cls.__refresh_token

    @classmethod
    async def info(cls, *, return_type: Any = None) -> Any:
        """Return the record the connection is authenticated as, or ``None``.

        ::

            me = await SurrealDBConnectionManager.info()                    # dict | None
            me = await SurrealDBConnectionManager.info(return_type=User)    # User | None

        ``None`` means "no record to report", and it has **two** causes worth telling apart:

        - the session is a system user (root, namespace or database), which is not a record —
          or, on the embedded engine, the anonymous in-process owner (no sign-in, or after
          :meth:`invalidate`);
        - the session *is* a record user, but the record's table does not grant it ``select``
          on itself, so the server returns nothing. This one is quiet and catches people out:
          the signin succeeded and ``$auth`` is set, yet ``info()`` reports nothing. Grant the
          table something like ``PERMISSIONS FOR select WHERE id = $auth.id``.

        Because that second case is a permissions problem, a ``None`` is passed straight
        through even when *return_type* is given, rather than failing validation against a
        model that was never the culprit.

        .. note::
            Up to SurrealDB 3.2.x, and on the 2.x line, signing in as a system user swaps the
            permissions but leaves ``$auth`` in place, so this can still report the record from
            an earlier record session. SurrealDB 3.3.0 clears the identity on that signin and
            this returns ``None``. :meth:`invalidate` ends a record session on every line.

        :param return_type: a type annotation to coerce the record into — a Pydantic model, a
            dataclass, or anything else Pydantic can adapt. ``None`` returns the raw mapping.
        :raises SurrealDbAuthenticationError: if the session is anonymous on a server (the
            embedded engine's anonymous session returns ``None``), or the server otherwise refuses.
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
        variables = _encoding.encode(variables)

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

    # ------------------------------------------------------------------
    # Live queries (v0.19.0)
    # ------------------------------------------------------------------

    @classmethod
    def subscribe_live(cls, query_uuid: str | UUID) -> LiveIterator:
        """Iterate the raw notification envelopes of a running live query.

        Not a coroutine: call it without ``await`` and iterate the result directly::

            stream = await User.objects().watch().start()
            async for notif in SurrealDBConnectionManager.subscribe_live(stream.live_id):
                print(notif["action"], notif["result"])

        Prefer reading ``stream`` itself: it already buffers every notification, so a second
        reader here is only for fanning one live query out to several consumers, each of which
        must keep reading.

        Buffering starts the moment this is called, not at the first iteration, so a write
        made between the two is still reported. Each envelope is the server's own dict —
        ``action``, ``id``, ``record``, ``result``, plus ``session`` on SurrealDB 3.x — with
        its values left in SDK types (``RecordID``, ``Datetime``). For model instances, use
        :meth:`QuerySet.live`.

        The iteration ends when the live query is killed or the connection is closed, on
        either server line. A read cancelled by a timeout loses nothing, so
        ``asyncio.wait_for(anext(stream), timeout)`` is a safe way to poll; ``aclose()`` stops
        this reader without killing the live query.

        :param query_uuid: the uuid of a running live query, e.g. ``stream.live_id``.
        :raises SurrealDbError: on a non-WebSocket connection (the embedded engine included), or
            if no client is connected on this event loop.
        """
        require_websocket(cls.__url)
        return open_stream(cls._live_client(), query_uuid)

    @classmethod
    def _cached_client(cls) -> Any:
        """This event loop's cached client, or ``None`` — never opens a connection."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        return cls.__clients.get(loop)

    @classmethod
    def _live_client(cls) -> Any:
        """The client already cached for this event loop.

        ``subscribe_live()`` is deliberately synchronous so that registration happens before
        the caller's next write, which rules out awaiting ``get_client()``. A live query uuid
        cannot exist without a connection, so requiring the cached one costs nothing and keeps
        the no-await guarantee.
        """
        client = cls._cached_client()
        if client is None:
            raise SurrealDbError(
                "No SurrealDB client is connected on this event loop. Start the live query "
                "first, e.g. `stream = await Model.objects().watch().start()`, then subscribe "
                "to `stream.live_id`."
            )
        return client

    @classmethod
    async def kill(cls, query_uuid: str | UUID) -> None:
        """Stop a live query and end every stream reading it.

        Idempotent: an unknown or already-killed uuid is a no-op, matching the ORM's existing
        treatment of cleanup against a missing target (``delete_table``, ``remove_relation``).

        Never opens a connection. A live query dies with the WebSocket that carried it, so with
        no client cached on this loop there is nothing left to kill: the readers are released
        and the call returns. Reaching for ``get_client()`` here would silently reconnect —
        sign in, replay the session, cache a socket nobody asked for — only to send a KILL the
        new connection could not own, and it would turn the ``__aexit__`` of a ``watch()`` block
        that outlived its connection into a network call.

        Never refused on an embedded URL (``mem://`` …), where no live query can start: a server
        client still cached on this loop — a stream started on a server, stopped after switching
        to the engine — receives the KILL; otherwise the call is the no-op above.

        :param query_uuid: the uuid of a running live query, e.g. ``stream.live_id``.
        :raises SurrealDbError: if the server refuses the kill for a reason other than an
            unknown uuid. The readers are released anyway, but the uuid is **not** recorded as
            dead — the subscription is still running, so a retry can re-attach to it.
        """
        client = cls._cached_client()
        try:
            # A dropped socket took its live queries with it (the server removes them with the
            # session), so a dead client is as good as none — and sending on it would raise. A
            # receive loop that died on a bad frame leaves the socket open, though, and the server
            # still running them: closing the transport is what ends them then.
            if client is None or is_dead(client):
                if client is not None:
                    await cls._close_dead_socket(client)
                mark_killed(query_uuid)
                return
            if not is_embedded_url(cls.__url):
                require_websocket(cls.__url)
            try:
                await client.kill(query_uuid)
            except Exception as exc:
                if not cls._is_unknown_live_query(exc):
                    raise SurrealDbError(f"Failed to kill live query {query_uuid}: {exc}") from exc
            mark_killed(query_uuid)
        finally:
            # In a `finally` so that every exit — success, failure, cancellation — wakes the
            # readers; nothing else ever would.
            release_subscribers(query_uuid)

    @staticmethod
    def _is_unknown_live_query(exc: Exception) -> bool:
        """Whether *exc* is the server refusing to kill a live query it does not know.

        Both lines raise ``InternalError`` with different wording — 3.x says ``Cannot execute
        KILL statement using id``, 2.6.x prefixes it with ``There was a problem with the
        database: Can not …`` — so the stable substring is matched rather than the sentence.
        """
        return "KILL statement using id" in str(exc)

    @classmethod
    async def reconnect(cls) -> Any:
        """
        Reconnect to the SurrealDB instance.

        On an embedded URL this closes the engine first (:meth:`close_connection`): a ``mem://``
        database starts empty again, and server clients are left as they are.
        :meth:`validate_connection` checks an embedded connection without discarding it.
        """
        await cls.close_connection()
        return await cls.get_client()

    @classmethod
    async def validate_connection(cls) -> bool:
        """
        Validate the connection to the SurrealDB instance.

        On a server URL the connection is rebuilt (:meth:`reconnect`). On an embedded URL the
        engine is opened if needed and **kept**: a ``mem://`` database is never discarded here, so
        the ``set_*(..., reconnect=True)`` setters keep it too.

        :return: True if the connection is valid, False otherwise — it cannot be opened, the URL is
            one the installed SDK refuses, or the embedded selection cannot be set.
        """
        if cls.is_embedded():
            try:
                await cls.get_client()
                return True
            except SurrealDbConnectionError:
                return False
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

        On an embedded URL, a value different from the current one also **ends the stored session**:
        a token belongs to the namespace and database it was issued for, and the next call resets the
        engine's shared session to the in-process owner on the new selection (:meth:`get_client`).
        On a server URL the stored session is kept, as before.

        :param namespace: The namespace to use.
        """

        if not cls.is_connection_set():
            raise ValueError("You can't change the namespace when the others setting are not already set.")

        if is_embedded_url(cls.__url) and namespace != cls.__namespace:
            cls.clear_session()
        cls.__namespace = namespace

        if reconnect and not await cls.validate_connection():  # pragma: no cover
            cls.__namespace = None
            return False

        return True

    @classmethod
    async def set_database(cls, database: str, reconnect: bool = False) -> bool:
        """
        Set the database to use.

        On an embedded URL, a value different from the current one also **ends the stored session**
        (see :meth:`set_namespace`). On a server URL the stored session is kept, as before.

        :param database: The database to use.
        """
        if not cls.is_connection_set():
            raise ValueError("You can't change the database when the others setting are not already set.")

        if is_embedded_url(cls.__url) and database != cls.__database:
            cls.clear_session()
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

        The embedded engine's client belongs to the whole process: while it is open, this is
        true from every loop, and outside any loop. A loop's server client counts too, even
        while an embedded URL is configured: after :meth:`close_connection` on the engine, this
        stays true on a loop that keeps one.

        :return: True if the connection is established, False otherwise.
        """
        cls.__prune_dead_loops()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return len(cls.__clients) > 0 or cls.__embedded_client is not None

        return cls.__clients.get(loop) is not None or cls.__embedded_client is not None
