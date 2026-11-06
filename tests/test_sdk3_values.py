"""SDK 3.0 (v0.50.0): how the ORM builds its SDK clients, how the SDK's errors reach the application, and the values
SDK 3.0 reads.

Connection part: the options of every client the ORM opens (``_open_client``), and the SDK 3.0 errors that become a
``SurrealDbConnectionError`` — a server outside the versions the SDK supports, a request past its deadline, a response
the SDK cannot read, a connection that is gone. Unit tests, with fake clients.

Values part (E2E, every line): SurrealDB's ``NULL`` reads as ``None`` on every read path, a native set as a ``set``
(3.3+ and 4.x; an array on 2.x), a datetime keeps its nanoseconds through a model, a zero duration and an open range
round-trip, and a ``date`` is stored as a ``datetime`` (never SurrealDB 4.x's ``date`` kind).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest
import surrealdb
from pydantic import field_validator

from surreal_orm_lite import (
    BaseSurrealModel,
    BoundExcluded,
    BoundIncluded,
    Count,
    Duration,
    Range,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    _client,
)
from surreal_orm_lite import connection_manager as connection_manager_module
from surreal_orm_lite._sdk import (
    ConnectionUnavailableError,
    HttpStatusError,
    NotFoundError,
    RecordID,
    TransportTimeoutError,
    UnexpectedResponseError,
    UnsupportedVersionError,
)
from surreal_orm_lite.exceptions import SurrealDbConnectionError
from surreal_orm_lite.live import is_connection_failure
from tests._retry import retry_write
from tests.conftest import change_feed_database, orm_client
from tests.test_rich_types import _server_version

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
            (HttpStatusError(401, "The access method 'a' does not exist", "http://db/rpc"), False),
            (HttpStatusError(503, "", "http://db/rpc"), True),
        ],
        ids=["timeout", "undecodable", "unavailable", "http-401", "http-503"],
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
            # The server answered over HTTP: a refusal (a revoked token's 401, a 400…) would meet the same answer, a
            # gateway or an overloaded server (502, 503, 504, 408, 429) may not.
            (HttpStatusError(401, "The access method 'a' does not exist", "http://db/rpc"), False),
            (HttpStatusError(400, "", "http://db/rpc"), False),
            (HttpStatusError(500, "", "http://db/rpc"), False),
            (HttpStatusError(502, "", "http://db/rpc"), True),
            (HttpStatusError(503, "", "http://db/rpc"), True),
            (HttpStatusError(504, "", "http://db/rpc"), True),
            (HttpStatusError(408, "", "http://db/rpc"), True),
            (HttpStatusError(429, "", "http://db/rpc"), True),
        ],
        ids=["unavailable", "timeout", "undecodable", "401", "400", "500", "502", "503", "504", "408", "429"],
    )
    def test_a_raw_sdk_error_is_classified_as_the_frontier_does(self, error: BaseException, expected: bool) -> None:
        """A call outside the frontier (``signin``, ``use``, ``begin``) raises the SDK's error itself: the reconnect
        loops retry it exactly when the frontier's ``SurrealDbConnectionError`` would be retryable."""
        assert is_connection_failure(error, None) is expected


# ==================== Values SDK 3.0 reads (#205, AC-5, AC-6) ====================


class SvNote(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="sv_note")

    id: str | None = None
    note: str | None = None
    n: int | None = None


class SvRich(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="sv_rich")

    id: str | None = None
    title: str = "t"
    at: datetime | None = None
    ttl: Duration | None = None
    pause: timedelta | None = None
    tags: set[str] = set()
    window: Range[int] | None = None
    day: date | None = None


#: A datetime with nanoseconds, written in SurrealQL: the SDK reads ``…05.123456`` and ``.nanosecond == 789``.
_NANO_TEXT = "2026-01-02T03:04:05.123456789Z"
_SV_CF_DATABASE = "sv_change_feed"


async def _take(stream: Any, count: int, timeout: float = 10.0) -> list[Any]:
    """The next *count* events of a live or change-feed stream."""
    events: list[Any] = []
    async with asyncio.timeout(timeout):
        while len(events) < count:
            events.append(await anext(stream))
    return events


class TestNullReadsAsNoneE2E:
    """SurrealDB's ``NULL`` (the SDK's ``Null``) is ``None`` on every read path, as with SDK 2.0."""

    @pytest.mark.asyncio
    async def test_a_model_raw_query_and_values(self) -> None:
        async with orm_client("sv_note") as client:
            await _client.query(client, "CREATE sv_note:a SET note = NULL, n = NULL;", {})
            row = await _client.query(client, "SELECT * FROM ONLY sv_note:a;", {})
            assert row["note"] is None and row["n"] is None, row

            got = await SvNote.objects().get("a")
            assert (got.note, got.n) == (None, None)
            [raw] = await SvNote.raw_query("SELECT * FROM sv_note:a;")
            assert isinstance(raw, SvNote) and (raw.note, raw.n) == (None, None)
            groups = await SvNote.objects().values("note").annotate(count=Count()).exec()
            assert groups == [{"note": None, "count": 1}], groups

    @pytest.mark.asyncio
    async def test_a_live_event(self) -> None:
        async with orm_client("sv_note") as client:
            await _client.query(client, "DEFINE TABLE sv_note SCHEMALESS;", {})
            async with SvNote.objects().live() as stream:
                await _client.query(client, "CREATE sv_note:b SET note = NULL;", {})
                [event] = await _take(stream, 1)
            assert event.validation_error is None, event.validation_error
            assert event.raw["note"] is None, event.raw
            assert isinstance(event.instance, SvNote) and event.instance.note is None

    @pytest.mark.asyncio
    async def test_a_change_feed_entry(self) -> None:
        async with change_feed_database(_SV_CF_DATABASE) as client:
            await _client.query(client, "DEFINE TABLE sv_note SCHEMALESS CHANGEFEED 1h;", {})
            stream = await SvNote.objects().changes(poll_interval=0.05).start()
            try:
                await retry_write(lambda: _client.query(client, "CREATE sv_note:c SET note = NULL;", {}))
                [event] = await _take(stream, 1)
            finally:
                stream.stop()
            assert event.validation_error is None, event.validation_error
            assert event.raw["note"] is None, event.raw
            assert isinstance(event.instance, SvNote) and event.instance.note is None


class TestNativeSetsE2E:
    """A ``set[str]`` field is written as an array (every line accepts it); a ``TYPE set<string>`` column stores a
    native set on 3.3+ and 4.x, read as a ``set``. 2.x has no native set: the column is an array there, unchanged."""

    @pytest.mark.asyncio
    async def test_a_set_field_round_trips(self) -> None:
        async with orm_client("sv_rich") as client:
            version = await _server_version()
            if (3, 0, 0) <= version < (3, 3, 0):
                pytest.skip("SurrealDB 3.1/3.2 refuse an array written into a set<…> column (docs/compatibility.md)")
            native = version >= (3, 0, 0)
            kind = "set<string>" if native else "array<string>"
            await _client.query(client, f"DEFINE FIELD tags ON sv_rich TYPE {kind};", {})
            await SvRich(id="s", tags={"b", "a"}).save()

            stored = await _client.query(client, "SELECT VALUE tags FROM ONLY sv_rich:s;", {})
            if native:
                assert stored == {"a", "b"} and type(stored) is set, stored
            else:
                assert stored == ["a", "b"], stored
            got = await SvRich.objects().get("s")
            assert type(got.tags) is set and got.tags == {"a", "b"}
            raw = await SvRich.objects().select("tags").exec()
            assert raw[0].tags == {"a", "b"} if isinstance(raw[0], SvRich) else set(raw[0]["tags"]) == {"a", "b"}


class TestNanosecondDatetimesE2E:
    """A datetime read from the server is a ``PreciseDatetime`` (a ``datetime``) and keeps its nanoseconds when the
    model writes it back: ``save()`` and ``update()`` store the very value read (SDK 2.0 floored it)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("write", ["save", "update", "merge"])
    async def test_read_write_read_keeps_the_nanoseconds(self, write: str) -> None:
        """``save()`` creates: the loaded instance is saved as a new record, ``update()``/``merge()`` rewrite it."""
        async with orm_client("sv_rich") as client:
            await _client.query(client, f"CREATE sv_rich:n SET title = 't', at = d'{_NANO_TEXT}';", {})
            got = await SvRich.objects().get("n")
            assert type(got.at) is surrealdb.PreciseDatetime
            assert got.at is not None and (got.at.microsecond, got.at.nanosecond) == (123456, 789)

            key = "n"
            if write == "save":
                key = "n2"
                await got.model_copy(update={"id": key, "title": "u"}).save()
            elif write == "update":
                got.title = "u"
                await got.update()
            else:
                await got.merge(title="u", at=got.at)
            stored = await _client.query(client, f"RETURN <string> sv_rich:{key}.at;", {})
            assert stored == _NANO_TEXT, stored
            again = await SvRich.objects().get(key)
            assert again.title == "u"
            assert again.at is not None and again.at.nanosecond == 789

    @pytest.mark.asyncio
    async def test_a_python_datetime_with_microseconds_is_stored_as_given(self) -> None:
        async with orm_client("sv_rich") as client:
            at = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
            await SvRich(id="p", at=at).save()
            assert await _client.query(client, "RETURN <string> sv_rich:p.at;", {}) == "2026-01-02T03:04:05.123456Z"
            got = await SvRich.objects().get("p")
            assert got.at == at and got.at is not None and got.at.nanosecond == 0


class SvConverted(BaseSurrealModel):
    """A validator that converts the datetime it is given: ``astimezone()`` to another zone and ``replace()``."""

    model_config = SurrealConfigDict(table_name="sv_conv")

    id: str | None = None
    utc: datetime | None = None
    shifted: datetime | None = None
    replaced: datetime | None = None

    @field_validator("utc")
    @classmethod
    def _to_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else value.astimezone(UTC)

    @field_validator("shifted")
    @classmethod
    def _to_plus_two(cls, value: datetime | None) -> datetime | None:
        return None if value is None else value.astimezone(timezone(timedelta(hours=2)))

    @field_validator("replaced")
    @classmethod
    def _replace_tz(cls, value: datetime | None) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


class TestValidatorsOnPreciseDatetimesE2E:
    """What ``astimezone()``/``replace()`` in a field validator do with a ``PreciseDatetime`` (docs/fields.md):
    the model accepts the ``datetime`` they return; ``astimezone()`` to the zone the value already has keeps the
    nanoseconds, ``astimezone()`` to another zone and ``replace()`` drop them (the value is then to the
    microsecond). The stored top-level value is unchanged by ``update()``: it still floors to what was sent."""

    @pytest.mark.asyncio
    async def test_the_model_accepts_the_converted_value(self) -> None:
        async with orm_client("sv_conv") as client:
            await _client.query(
                client,
                f"CREATE sv_conv:v SET utc = d'{_NANO_TEXT}', shifted = d'{_NANO_TEXT}', replaced = d'{_NANO_TEXT}';",
                {},
            )
            got = await SvConverted.objects().get("v")
            assert got.utc is not None and got.shifted is not None and got.replaced is not None
            for value in (got.utc, got.shifted, got.replaced):
                assert isinstance(value, datetime) and value == datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
            assert got.utc.nanosecond == 789
            assert got.shifted.utcoffset() == timedelta(hours=2) and got.shifted.nanosecond == 0
            assert got.replaced.nanosecond == 0

            await got.update()
            stored = await _client.query(
                client, "RETURN [<string> sv_conv:v.utc, <string> sv_conv:v.shifted, <string> sv_conv:v.replaced];", {}
            )
            assert stored == [_NANO_TEXT] * 3, stored


class TestZeroDurationsAndOpenRangesE2E:
    """A zero duration and a range with an open side round-trip through a model, and an open range read raw
    (``None`` side) binds back as a parameter."""

    @pytest.mark.asyncio
    async def test_a_zero_duration(self) -> None:
        async with orm_client("sv_rich") as client:
            await SvRich(id="z", ttl=Duration(0), pause=timedelta(0)).save()
            assert await _client.query(client, "RETURN [sv_rich:z.ttl == 0ns, sv_rich:z.pause == 0ns];", {}) == [True, True]
            got = await SvRich.objects().get("z")
            assert type(got.ttl) is Duration and got.ttl == Duration(0)
            assert type(got.pause) is timedelta and got.pause == timedelta(0)
            await got.update()
            assert (await SvRich.objects().get("z")).ttl == Duration(0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("window", "text"),
        [
            (Range(BoundIncluded(1)), "1.."),
            (Range(None, BoundExcluded(5)), "..5"),
            (Range(), ".."),
        ],
        ids=["1..", "..5", ".."],
    )
    async def test_an_open_range(self, window: Range[int], text: str) -> None:
        async with orm_client("sv_rich") as client:
            await SvRich(id="r", window=window).save()
            assert await _client.query(client, "RETURN <string> sv_rich:r.window;", {}) == text
            got = await SvRich.objects().get("r")
            assert type(got.window) is Range and got.window == window

            raw = await _client.query(client, "SELECT VALUE window FROM ONLY sv_rich:r;", {})
            assert type(raw) is surrealdb.Range
            assert (raw.begin is None) == (window.begin is None) and (raw.end is None) == (window.end is None)
            # The raw value, ``None`` side included, binds back: the SDK's ``Null`` goes out as the open side.
            rows = await SvRich.raw_query("SELECT * FROM sv_rich WHERE window = $w;", {"w": raw})
            assert [row.id for row in rows] == ["r"]
            await got.update()
            assert await _client.query(client, "RETURN <string> sv_rich:r.window;", {}) == text

    @pytest.mark.asyncio
    async def test_an_sdk_range_with_a_null_side_binds(self) -> None:
        async with orm_client("sv_rich") as client:
            value = surrealdb.Range(BoundIncluded(1), surrealdb.Null)
            assert await _client.query(client, "RETURN <string> $r;", {"r": value}) == "1.."
            await SvRich(id="q", window=Range(BoundIncluded(1))).save()
            rows = await SvRich.raw_query("SELECT * FROM sv_rich WHERE window = $w;", {"w": value})
            assert [row.id for row in rows] == ["q"]


class TestDateStorageE2E:
    """D13: a ``date`` field is stored as a ``datetime`` at midnight UTC on every line, never SurrealDB 4.x's ``date``
    kind (which SDK 3.0 would send for a ``date`` over ``cbor.2``)."""

    @pytest.mark.asyncio
    async def test_a_date_is_stored_as_a_datetime(self) -> None:
        async with orm_client("sv_rich") as client:
            await SvRich(id="d", day=date(2026, 1, 2)).save()
            stored = await _client.query(client, "SELECT VALUE day FROM ONLY sv_rich:d;", {})
            assert type(stored) is surrealdb.PreciseDatetime and stored == datetime(2026, 1, 2, tzinfo=UTC)
            if await _server_version() >= (3, 0, 0):
                assert await _client.query(client, "RETURN type::of(sv_rich:d.day);", {}) == "datetime"
            else:
                assert await _client.query(client, "RETURN type::is::datetime(sv_rich:d.day);", {}) is True
            got = await SvRich.objects().get("d")
            assert type(got.day) is date and got.day == date(2026, 1, 2)
