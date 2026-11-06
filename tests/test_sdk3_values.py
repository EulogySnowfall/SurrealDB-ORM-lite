"""SDK 3.0 (v0.50.0): how the ORM builds its SDK clients and how the SDK's errors reach the application.

Connection part: the options of every client the ORM opens (``_open_client``), and the SDK 3.0 errors that become a
``SurrealDbConnectionError`` — a server outside the versions the SDK supports, a request past its deadline, a response
the SDK cannot read, a connection that is gone. All unit tests, with fake clients: nothing here needs a server.
"""

from __future__ import annotations

from typing import Any

import pytest

from surreal_orm_lite import SurrealDBConnectionManager, _client
from surreal_orm_lite import connection_manager as connection_manager_module
from surreal_orm_lite._sdk import (
    ConnectionUnavailableError,
    NotFoundError,
    RecordID,
    TransportTimeoutError,
    UnexpectedResponseError,
    UnsupportedVersionError,
)
from surreal_orm_lite.exceptions import SurrealDbConnectionError
from surreal_orm_lite.live import is_connection_failure

# ==================== Client construction (AC-10) ====================


class _OpenedClient:
    """Stands in for an SDK client: accepts the open handshake, records nothing else."""

    def __init__(self, *, connect_error: BaseException | None = None) -> None:
        self.connect_error = connect_error
        self.closed = False

    async def connect(self, url: str | None = None) -> None:
        if self.connect_error is not None:
            raise self.connect_error

    async def signin(self, variables: Any) -> None:
        pass

    async def use(self, namespace: str, database: str) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


def _spy_constructor(monkeypatch: pytest.MonkeyPatch, client: _OpenedClient | None = None) -> list[tuple[str, dict[str, Any]]]:
    """Replace ``AsyncSurreal`` in the connection manager: each call is recorded, and *client* (or a fresh fake) built."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def _construct(url: str, **options: Any) -> _OpenedClient:
        calls.append((url, options))
        return client if client is not None else _OpenedClient()

    monkeypatch.setattr(connection_manager_module, "AsyncSurreal", _construct)
    return calls


_SERVER_OPTIONS = {"reconnect": False, "streaming": False, "renew_access": False}


class TestClientOptionsUnit:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", ["ws://db.invalid:8000/rpc", "wss://db.invalid/rpc"])
    async def test_a_websocket_client_has_no_reconnect_streaming_renewal_or_deadline(
        self, monkeypatch: pytest.MonkeyPatch, url: str
    ) -> None:
        calls = _spy_constructor(monkeypatch)
        SurrealDBConnectionManager.set_connection(url=url, user="root", password="root", namespace="ns", database="db")

        await SurrealDBConnectionManager._open_client(signin_as_configured=True)

        assert calls == [(url, {**_SERVER_OPTIONS, "timeout": None})]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", ["http://db.invalid:8000", "https://db.invalid"])
    async def test_an_http_client_keeps_the_sdk_2_deadline_of_30_seconds(
        self, monkeypatch: pytest.MonkeyPatch, url: str
    ) -> None:
        """M32, measured with SDK 2.0.0 on 2.7.0 and 3.3.5: ``SLEEP 35s`` over HTTP raised ``TimeoutError`` after 30 s
        (its HTTP client sends every request with ``aiohttp.ClientTimeout(total=30)``). The ORM keeps that deadline."""
        calls = _spy_constructor(monkeypatch)
        SurrealDBConnectionManager.set_connection(url=url, user="root", password="root", namespace="ns", database="db")

        await SurrealDBConnectionManager._open_client(signin_as_configured=True)

        assert calls == [(url, {**_SERVER_OPTIONS, "timeout": 30.0})]

    @pytest.mark.asyncio
    async def test_the_embedded_engine_is_built_from_its_url_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _spy_constructor(monkeypatch)
        SurrealDBConnectionManager.set_connection(url="mem://", namespace="ns", database="db")

        await SurrealDBConnectionManager._open_client(signin_as_configured=True)

        assert calls == [("mem://", {})]


# ==================== Error normalisation (AC-9) ====================


class TestUnsupportedServerVersionUnit:
    @pytest.mark.asyncio
    async def test_a_server_outside_the_sdk_range_is_a_connection_error_naming_its_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        refusal = UnsupportedVersionError("1.5.4", "2.0.0", "5.0.0")
        fake = _OpenedClient(connect_error=refusal)
        _spy_constructor(monkeypatch, fake)
        SurrealDBConnectionManager.set_connection(
            url="ws://db.invalid:8000/rpc", user="root", password="root", namespace="ns", database="db"
        )

        with pytest.raises(SurrealDbConnectionError) as caught:
            await SurrealDBConnectionManager._open_client(signin_as_configured=True)

        assert str(caught.value) == "SurrealDB 1.5.4 is not supported by surrealdb SDK 3.x (2.0.0 to 5.0.0 excluded)."
        assert caught.value.__cause__ is refusal
        assert caught.value.retryable is False  # type: ignore[attr-defined]
        assert fake.closed


class _RaisingClient:
    """An SDK client double whose every data call raises *error* (the builder of ``update()`` too)."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def _raise(self, *args: Any, **kwargs: Any) -> Any:
        raise self.error

    query = query_raw = select = create = upsert = delete = insert = _raise

    def update(self, *args: Any, **kwargs: Any) -> Any:
        if len(args) > 1:
            return self._raise()
        return self

    merge = patch = _raise


_CALLS = {
    "query": lambda c: _client.query(c, "RETURN 1"),
    "query_all": lambda c: _client.query_all(c, "RETURN 1"),
    "query_raw": lambda c: _client.query_raw(c, "RETURN 1"),
    "select": lambda c: _client.select(c, RecordID("t", 1)),
    "create": lambda c: _client.create(c, "t", {}),
    "update": lambda c: _client.update(c, RecordID("t", 1), {}),
    "upsert": lambda c: _client.upsert(c, RecordID("t", 1), {}),
    "merge": lambda c: _client.merge(c, RecordID("t", 1), {}),
    "patch": lambda c: _client.patch(c, RecordID("t", 1), []),
    "insert": lambda c: _client.insert(c, "t", {}),
    "insert_relation": lambda c: _client.insert_relation(c, "t", {}),
    "delete": lambda c: _client.delete(c, RecordID("t", 1)),
}


class TestSdkErrorsUnit:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("call", sorted(_CALLS))
    @pytest.mark.parametrize(
        ("error", "retryable"),
        [
            (TransportTimeoutError("timed out while querying"), False),
            (UnexpectedResponseError("the response could not be read"), False),
            (ConnectionUnavailableError("the connection to ws://db/rpc failed"), True),
        ],
        ids=["timeout", "undecodable", "unavailable"],
    )
    async def test_an_sdk_transport_or_response_error_is_a_connection_error(
        self, call: str, error: BaseException, retryable: bool
    ) -> None:
        with pytest.raises(SurrealDbConnectionError) as caught:
            await _CALLS[call](_RaisingClient(error))

        assert caught.value.__cause__ is error
        assert str(caught.value) == str(error)
        assert caught.value.retryable is retryable  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_a_server_error_passes_through_unchanged(self) -> None:
        error = NotFoundError("NotFound", "The table 't' does not exist")

        with pytest.raises(NotFoundError) as caught:
            await _client.query(_RaisingClient(error), "SELECT * FROM t")

        assert caught.value is error

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (ConnectionUnavailableError("could not connect"), True),
            (TransportTimeoutError("timed out"), False),
            (UnexpectedResponseError("unreadable"), False),
        ],
        ids=["unavailable", "timeout", "undecodable"],
    )
    def test_a_raw_sdk_error_is_classified_as_the_frontier_does(self, error: BaseException, expected: bool) -> None:
        """A call outside the frontier (``signin``, ``use``, ``begin``) raises the SDK's error itself: the reconnect
        loops retry it exactly when the frontier's ``SurrealDbConnectionError`` would be retryable."""
        assert is_connection_failure(error, None) is expected
