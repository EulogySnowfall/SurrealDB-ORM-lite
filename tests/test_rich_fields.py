"""Rich field types through the ORM (v0.23.0): every write stores native SurrealDB values, every read
hydrates the declared Python types, on SurrealDB 2.x and 3.x alike.

- **Writes** (spec §5.2): the model payload (``save``, ``update``, ``upsert``, ``bulk_create``, edges,
  ``*_or_create``, ``server_values``) and the keyword payload of ``merge()`` go through
  ``wire.encode_wire(…, stored=True)``: a ``timedelta``/``Duration`` becomes a duration, a ``date``
  midnight UTC, a ``Range`` a range, a ``set`` an array, a ``Decimal`` a fixed-point decimal. A raw
  ``SELECT`` reads them back as SDK values (``_assert_native``).
- **Untyped positions** (spec §5.3): a ``dict[str, Any]``/``list[Any]`` field, an ``extra="allow"``
  column or the ``Any`` part of a nested model keeps the SDK ``Duration``/``Range`` it was loaded
  with through ``update()``/``save()``/``upsert()`` (``wire.restore_native``), instead of the dict
  ``model_dump()`` flattens it into.
- **Reads** (spec §5.4): ``set_data`` converts the SDK forms Pydantic cannot validate, only where the
  annotation asks for them — an SDK ``Duration`` into a ``timedelta`` field (floored to the µs), never
  into an ``Any`` or ``Duration`` position.

E2E tables are dedicated (``rf_*``), dropped on entry and exit by ``orm_client``; records are created
inside the ``async with orm_client(...)`` block.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
import surrealdb
from pydantic import BaseModel
from surrealdb import RecordID

from surreal_orm_lite import (
    BaseSurrealEdge,
    BaseSurrealModel,
    BoundExcluded,
    BoundIncluded,
    Duration,
    Range,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    SurrealFunc,
)
from tests.conftest import orm_client

# ==================== Shared models and values ====================


class RfEvent(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_event")

    id: str | None = None
    at: datetime | None = None
    day: date | None = None
    ttl: timedelta | None = None
    grace: Duration | None = None
    price: Decimal | None = None
    token: UUID | None = None
    hours: Range[int] | None = None
    period: Range[datetime] | None = None
    blob: bytes | None = None
    tags: set[str] = set()
    ttls: list[timedelta] = []
    n: int = 0


_TOKEN = UUID("0190c3d1-5c3b-7cc6-8f8a-2d2b1f0e9a11")
_JAN = datetime(2026, 1, 1, tzinfo=UTC)

FULL: dict[str, Any] = {
    "at": datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC),
    "day": date(2026, 1, 2),
    "ttl": timedelta(hours=1, minutes=30),
    "grace": Duration.parse("1h30m1ns"),
    "price": Decimal("12.50"),
    "token": _TOKEN,
    "hours": Range(BoundIncluded(9), BoundExcluded(17)),
    "period": Range(BoundIncluded(_JAN), None),
    "blob": b"\x00\x01\xff",
    "tags": {"b", "a"},
    "ttls": [timedelta(hours=1)],
    "n": 5,
}

SEED = (
    "CREATE rf_event:s SET at = d'2026-01-02T03:04:05.123456789Z', day = d'2026-01-02T00:00:00Z', "
    "ttl = 1h30m1ns, grace = 1h30m1ns, price = 12.50dec, token = u'0190c3d1-5c3b-7cc6-8f8a-2d2b1f0e9a11', "
    "hours = 9..17, period = d'2026-01-01T00:00:00Z'.., blob = <bytes> 'abc', tags = ['a'], ttls = [1h, 2h], "
    "n = 1;"
)

_HOUR_NS = 3600 * 10**9


async def _raw(id_: Any, table: str = "rf_event") -> dict[str, Any]:
    """The stored row as the SDK decodes it — SDK values, no ORM in between."""
    rid = id_ if isinstance(id_, RecordID) else RecordID(table, id_)
    client = await SurrealDBConnectionManager.get_client()
    row = await client.query("SELECT * FROM ONLY $rid;", {"rid": rid})
    assert isinstance(row, dict), f"no row for {rid}: {row!r}"
    return row


def _assert_native(row: dict[str, Any], n: int = 5) -> None:
    """A ``FULL`` row as stored: native SurrealDB types, decoded by the SDK."""
    assert isinstance(row["at"], datetime)
    assert row["at"].utcoffset() == timedelta(0)
    assert row["at"] == FULL["at"]
    assert row["day"] == datetime(2026, 1, 2, tzinfo=UTC)  # a date is stored as midnight UTC
    assert isinstance(row["ttl"], surrealdb.Duration)
    assert row["ttl"].elapsed == 5400 * 10**9
    assert isinstance(row["grace"], surrealdb.Duration)
    assert row["grace"].elapsed == 5400 * 10**9 + 1
    assert isinstance(row["price"], Decimal)
    assert row["price"] == Decimal("12.5")
    assert type(row["token"]) is UUID
    assert row["token"] == _TOKEN
    assert isinstance(row["hours"], surrealdb.Range)
    assert Range(BoundIncluded(9), BoundExcluded(17)) == row["hours"]
    assert isinstance(row["period"], surrealdb.Range)
    assert row["period"].end is None  # the open side
    assert Range(BoundIncluded(_JAN), None) == row["period"]
    assert row["blob"] == b"\x00\x01\xff"
    assert row["tags"] == ["a", "b"]  # an array, sorted
    assert [type(d) for d in row["ttls"]] == [surrealdb.Duration]
    assert row["ttls"][0].elapsed == _HOUR_NS
    assert row["n"] == n


def _assert_declared(ev: RfEvent) -> None:
    """A hydrated ``SEED`` row: the declared Python types."""
    assert type(ev.at) is datetime
    assert ev.at.utcoffset() == timedelta(0)
    assert ev.at.microsecond == 123456
    assert ev.day == date(2026, 1, 2)
    assert type(ev.day) is date
    assert ev.ttl == timedelta(hours=1, minutes=30)  # 1h30m1ns floored to the µs
    assert type(ev.ttl) is timedelta
    assert ev.grace == Duration.parse("1h30m1ns")  # the nanosecond kept
    assert type(ev.grace) is Duration
    assert ev.price == Decimal("12.5")
    assert type(ev.token) is UUID
    assert ev.hours == Range(BoundIncluded(9), BoundExcluded(17))
    assert type(ev.hours) is Range
    assert ev.period is not None
    assert ev.period.end is None
    assert ev.period.begin is not None
    assert type(ev.period.begin.value) is datetime
    assert ev.blob == b"abc"
    assert ev.tags == {"a"}
    assert type(ev.tags) is set
    assert ev.ttls == [timedelta(hours=1), timedelta(hours=2)]
    assert all(type(d) is timedelta for d in ev.ttls)


# ==================== Task 5 — model payload writes, untyped fields, set_data ====================


class RfLink(BaseSurrealEdge):
    model_config = SurrealConfigDict(table_name="rf_link")

    ttl: timedelta | None = None
    window: Range[int] | None = None


class RfLoose(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_loose")

    id: str | None = None
    meta: dict[str, Any] = {}
    items: list[Any] = []
    note: str = ""


class RfExtra(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_extra", extra="allow")

    id: str | None = None
    note: str = ""


class RfSched(BaseModel):
    ttl: timedelta
    grace: Duration
    extra: dict[str, Any] = {}


class RfNest(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_nest")

    id: str | None = None
    sched: RfSched | None = None
    note: str = ""


_TABLES = ("rf_event", "rf_link", "rf_loose", "rf_extra", "rf_nest")


async def _via_save(rid: str) -> Any:
    await RfEvent(id=rid, **FULL).save()
    return rid


async def _via_save_auto_id(_rid: str) -> Any:
    ev = RfEvent(**FULL)
    await ev.save()
    assert isinstance(ev.get_id(), RecordID)
    return ev.get_id()


async def _via_update_after_load(rid: str) -> Any:
    await RfEvent(id=rid, **{**FULL, "n": 0}).save()
    loaded = await RfEvent.objects().get(rid)
    loaded.n = 5
    await loaded.update()
    return rid


async def _via_upsert(rid: str) -> Any:
    await RfEvent(id=rid, **FULL).upsert()
    return rid


async def _via_merge(rid: str) -> Any:
    ev = RfEvent(id=rid)
    await ev.save()
    await ev.merge(**FULL)
    return rid


async def _via_bulk_create(rid: str) -> Any:
    created = await RfEvent.objects().bulk_create([RfEvent(id=rid, **FULL)])
    assert len(created) == 1
    return rid


async def _via_get_or_create(rid: str) -> Any:
    _obj, created = await RfEvent.objects().get_or_create(id=rid, defaults=FULL)
    assert created
    return rid


async def _via_update_or_create(rid: str) -> Any:
    await RfEvent(id=rid, **{**FULL, "n": 0}).save()
    _obj, created = await RfEvent.objects().update_or_create(id=rid, defaults=FULL)
    assert not created
    return rid


async def _via_server_values(rid: str) -> Any:
    await RfEvent(id=rid, **FULL).save(server_values={"n": SurrealFunc("1 + 1")})
    return rid


_WRITE_APIS: list[tuple[str, Callable[[str], Awaitable[Any]], int]] = [
    ("save", _via_save, 5),
    ("save-auto-id", _via_save_auto_id, 5),
    ("update-after-load", _via_update_after_load, 5),
    ("upsert", _via_upsert, 5),
    ("merge", _via_merge, 5),
    ("bulk_create", _via_bulk_create, 5),
    ("get_or_create", _via_get_or_create, 5),
    ("update_or_create", _via_update_or_create, 5),
    ("save-server-values", _via_server_values, 2),
]


def _assert_sdk_duration(value: Any, elapsed: int) -> None:
    assert isinstance(value, surrealdb.Duration), f"not an SDK Duration: {value!r}"
    assert value.elapsed == elapsed


class TestRichWritesE2E:
    @pytest.mark.parametrize(("write", "n"), [(api, n) for _, api, n in _WRITE_APIS], ids=[name for name, _, _ in _WRITE_APIS])
    async def test_write_api_stores_native_types(self, write: Callable[[str], Awaitable[Any]], n: int) -> None:
        async with orm_client(*_TABLES):
            rid = await write("w")
            _assert_native(await _raw(rid), n=n)

    async def test_loaded_rows_hydrate(self) -> None:
        async with orm_client(*_TABLES) as client:
            await client.query(SEED, {})
            _assert_declared(await RfEvent.objects().get("s"))

    async def test_edge_save_stores_native_types(self) -> None:
        async with orm_client(*_TABLES):
            a, b = RfEvent(id="a"), RfEvent(id="b")
            await a.save()
            await b.save()

            link = RfLink(in_=a, out=b, ttl=timedelta(minutes=5), window=Range(BoundIncluded(1), BoundExcluded(5)))
            await link.save()
            row = await _raw(link.get_id(), "rf_link")
            _assert_sdk_duration(row["ttl"], 300 * 10**9)
            assert isinstance(row["window"], surrealdb.Range)
            assert Range(BoundIncluded(1), BoundExcluded(5)) == row["window"]

            created = await RfLink.objects().bulk_create(
                [RfLink(in_=b, out=a, ttl=timedelta(hours=1), window=Range(None, BoundIncluded(3)))]
            )
            assert len(created) == 1
            assert created[0].ttl == timedelta(hours=1)  # hydrated through set_data
            assert created[0].window == Range(None, BoundIncluded(3))
            row = await _raw(created[0].get_id(), "rf_link")
            _assert_sdk_duration(row["ttl"], _HOUR_NS)
            assert isinstance(row["window"], surrealdb.Range)
            assert row["window"].begin is None
            assert Range(None, BoundIncluded(3)) == row["window"]

    async def test_untyped_values_survive_write_back(self) -> None:
        def check(row: dict[str, Any]) -> None:
            meta, items = row["meta"], row["items"]
            _assert_sdk_duration(meta["ttl"], 5400 * 10**9)
            assert isinstance(meta["win"], surrealdb.Range)
            assert Range(BoundIncluded(1), BoundExcluded(5)) == meta["win"]
            assert isinstance(meta["open"], surrealdb.Range)
            assert meta["open"].begin is None
            assert Range(None, BoundExcluded(5)) == meta["open"]
            assert meta["at"] == _JAN
            _assert_sdk_duration(items[0], 2 * _HOUR_NS)
            assert isinstance(items[1], surrealdb.Range)
            assert Range(BoundExcluded(1), BoundIncluded(3)) == items[1]

        async with orm_client(*_TABLES) as client:
            await client.query(
                "CREATE rf_loose:x SET meta = {ttl: 1h30m, win: 1..5, open: ..5, at: d'2026-01-01T00:00:00Z'}, "
                "items = [2h, 1>..=3], note = 'a';",
                {},
            )
            loaded = await RfLoose.objects().get("x")
            _assert_sdk_duration(loaded.meta["ttl"], 5400 * 10**9)  # Any positions keep the SDK values

            loaded.note = "b"
            await loaded.update()
            row = await _raw("x", "rf_loose")
            assert row["note"] == "b"
            check(row)

            await RfLoose(id="y", meta=loaded.meta, items=loaded.items, note="c").save()
            check(await _raw("y", "rf_loose"))

            loaded.note = "d"
            await loaded.upsert()
            row = await _raw("x", "rf_loose")
            assert row["note"] == "d"
            check(row)

    async def test_extra_allow_columns_survive_write_back(self) -> None:
        def check(row: dict[str, Any], note: str) -> None:
            assert row["note"] == note
            _assert_sdk_duration(row["wait"], 5400 * 10**9)
            assert isinstance(row["span"], surrealdb.Range)
            assert Range(BoundIncluded(1), BoundExcluded(5)) == row["span"]

        async with orm_client(*_TABLES) as client:
            await client.query("CREATE rf_extra:x SET wait = 1h30m, span = 1..5, note = 'a';", {})
            loaded = await RfExtra.objects().get("x")
            assert type(loaded.model_extra) is dict
            _assert_sdk_duration(loaded.model_extra["wait"], 5400 * 10**9)  # an extra keeps the SDK value

            loaded.note = "b"
            await loaded.update()
            check(await _raw("x", "rf_extra"), "b")

            await RfExtra(id="y", note="c", **loaded.model_extra).save()
            check(await _raw("y", "rf_extra"), "c")

            loaded.note = "d"
            await loaded.upsert()
            check(await _raw("x", "rf_extra"), "d")

    async def test_nested_model_mixed_positions_survive(self) -> None:
        async with orm_client(*_TABLES) as client:
            await client.query(
                "CREATE rf_nest:x SET sched = {ttl: 1h30m1ns, grace: 1h30m1ns, extra: {x: 500ns, y: 1h}};",
                {},
            )
            got = await RfNest.objects().get("x")
            assert got.sched is not None
            assert got.sched.ttl == timedelta(hours=1, minutes=30)
            assert type(got.sched.ttl) is timedelta
            assert got.sched.grace.elapsed == 5400 * 10**9 + 1
            assert got.sched.extra["x"] == surrealdb.Duration(500)  # the SDK object, kept in Any
            assert type(got.sched.extra["x"]) is surrealdb.Duration

            got.note = "b"
            await got.update()  # not refused
            sched = (await _raw("x", "rf_nest"))["sched"]
            _assert_sdk_duration(sched["grace"], 5400 * 10**9 + 1)
            _assert_sdk_duration(sched["extra"]["x"], 500)
            _assert_sdk_duration(sched["extra"]["y"], _HOUR_NS)
            _assert_sdk_duration(sched["ttl"], 5400 * 10**9)
