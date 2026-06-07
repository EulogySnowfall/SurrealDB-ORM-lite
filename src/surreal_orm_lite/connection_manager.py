import contextlib
import logging
from typing import Any

from ._sdk import AsyncSurreal, NotFoundError
from .exceptions import SurrealDbConnectionError
from .transaction import BufferedTransaction, InteractiveTransaction, Transaction

logger = logging.getLogger(__name__)


class SurrealDBConnectionManager:
    __url: str | None = None
    __user: str | None = None
    __password: str | None = None
    __namespace: str | None = None
    __database: str | None = None
    __client: Any = None

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
        await cls.close_connection()

    @classmethod
    def is_connection_set(cls) -> bool:
        """
        Check if the connection kwargs are set.

        :return: True if the connection kwargs are set, False otherwise.
        """
        return all([cls.__url, cls.__user, cls.__password, cls.__namespace, cls.__database])

    @classmethod
    async def get_client(cls) -> Any:
        """
        Connect to the SurrealDB instance.

        :return: The SurrealDB instance.
        """

        if cls.__client is not None:
            return cls.__client

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

            cls.__client = _client
            return cls.__client
        except Exception as e:
            logger.warning(f"Can't get connection: {e}")
            if cls.__client is not None:  # pragma: no cover
                with contextlib.suppress(NotImplementedError):
                    await cls.__client.close()
                cls.__client = None
            raise SurrealDbConnectionError("Can't connect to the database.") from None

    @classmethod
    async def close_connection(cls) -> None:
        """
        Close the connection to the SurrealDB instance.
        """
        if cls.__client is None:
            return

        with contextlib.suppress(NotImplementedError):
            await cls.__client.close()
        cls.__client = None

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
            await tx.commit()
            await tx.fire_post_commit()

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
        Check if the connection to the SurrealDB instance is established.

        :return: True if the connection is established, False otherwise.
        """

        return cls.__client is not None
