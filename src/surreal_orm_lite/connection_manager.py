import asyncio
import contextlib
import logging
import weakref
from collections.abc import Sequence
from typing import Any

from ._sdk import AsyncSurreal, NotFoundError
from .exceptions import SurrealDbConnectionError, SurrealDbError, SurrealDbNotFoundError
from .functions import build_call_statement, normalize_function_name
from .transaction import BufferedTransaction, InteractiveTransaction, Transaction

logger = logging.getLogger(__name__)


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

    @classmethod
    async def call_function(
        cls,
        function: str,
        args: Sequence[Any] | None = None,
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

        :param function: ``"fn::name"``, ``"name"``, or a nested ``"fn::namespace::name"``.
        :param args: positional arguments, in the function's declaration order.
        :return: the function's return value; ``None`` when it returns ``NONE``.
        :raises ValueError: if the function name is not an identifier path.
        :raises SurrealDbNotFoundError: if the server has no such function.
        :raises SurrealDbError: for any other database failure.
        """
        name = normalize_function_name(function)
        statement, variables = build_call_statement(name, args or [])

        client = await cls.get_client()
        try:
            return await client.query(statement, variables)
        except Exception as exc:
            raise cls._wrap_call_error(exc, name) from exc

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
