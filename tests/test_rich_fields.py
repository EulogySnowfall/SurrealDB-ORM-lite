"""Rich field types through the ORM (v0.23.0): every write stores native SurrealDB values, every read
hydrates the declared Python types, on SurrealDB 2.x and 3.x alike.

- **Writes** (spec §5.2): the model payload (``save``, ``update``, ``upsert``, ``bulk_create``, edges,
  ``*_or_create``, ``server_values``) and the keyword payload of ``merge()`` go through
  ``wire.encode_wire(…, stored=True)``: a ``timedelta``/``Duration`` becomes a duration, a ``date``
  midnight UTC, a ``Range`` a range, a ``set`` an array, a ``Decimal`` a fixed-point decimal. A raw
  ``SELECT`` reads them back as SDK values (``_assert_native``).
- **Statement variables** (spec §5.2, D17): ``patch``, the ``atomic_*`` helpers, ``bulk_update`` and
  ``relate`` encode their bound values for a write (``stored=True``); ``raw_query``,
  ``QuerySet.query``, ``call_function`` and filter values are encoded with ``stored=False`` — the
  same conversions, but a zero duration is sent (it may only filter). Values no SurrealDB type can
  hold are refused before anything is sent, and the client stays the same (spec §4.7).
- **Untyped positions** (spec §5.3): a ``dict[str, Any]``/``list[Any]`` field, an ``extra="allow"``
  column or the ``Any`` part of a nested model keeps the SDK ``Duration``/``Range`` it was loaded
  with through ``update()``/``save()``/``upsert()`` (``wire.restore_native``), instead of the dict
  ``model_dump()`` flattens it into.
- **Reads** (spec §5.4): ``set_data`` converts the SDK forms Pydantic cannot validate, only where the
  annotation asks for them — an SDK ``Duration`` into a ``timedelta`` field (floored to the µs), never
  into an ``Any`` or ``Duration`` position.
- **Rows returned by writes** (spec §5.4): ``_apply_record`` — the step that copies the row a write
  sends back onto the instance (``save`` auto id, ``upsert``, ``merge``, ``patch``, ``atomic_*``,
  ``refresh``, server fields) — re-validates the rich fields, so the instance keeps its declared
  types (a ``set`` stays a ``set``); ``call_function(return_type=)`` converts the SDK values too.

E2E tables are dedicated (``rf_*``), dropped on entry and exit by ``orm_client``; records are created
inside the ``async with orm_client(...)`` block.
"""

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

import pytest
import surrealdb
from pydantic import BaseModel, Field, ValidationError
from surrealdb import RecordID

from surreal_orm_lite import (
    BaseSurrealEdge,
    BaseSurrealModel,
    BoundExcluded,
    BoundIncluded,
    Computed,
    Duration,
    Q,
    Range,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    SurrealFunc,
    computed,
    retry_on_conflict,
)
from surreal_orm_lite.wire import sdk_decodes_zero_duration
from tests.conftest import orm_client
from tests.test_rich_types import _server_version

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


# ==================== Task 6 — statement variables, refusals, transactions, non-finite decimals ====================


class RfNan(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_nan")

    id: str | None = None
    d: Annotated[Decimal, Field(allow_inf_nan=True)] = Decimal(0)


_STMT_TABLES = ("rf_event", "rf_rel", "rf_nan")
_RF_ADD = "DEFINE FUNCTION OVERWRITE fn::rf_add($d: duration) { RETURN $d + 1h; };"


@contextlib.asynccontextmanager
async def _rf_add_function() -> AsyncIterator[None]:
    """``fn::rf_add($d: duration)`` (returns ``$d + 1h``) for the block, removed afterwards."""
    client = await SurrealDBConnectionManager.get_client()
    await client.query(_RF_ADD, {})
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            await (await SurrealDBConnectionManager.get_client()).query("REMOVE FUNCTION IF EXISTS fn::rf_add;", {})
        SurrealDBConnectionManager.clear_function_signature_cache()


async def _saved(rid: str = "w") -> RfEvent:
    ev = RfEvent(id=rid, **FULL)
    await ev.save()
    return ev


async def _stmt_model_patch() -> None:
    ev = await _saved()
    await ev.patch([{"op": "replace", "path": "/ttl", "value": timedelta(minutes=5)}])
    _assert_sdk_duration((await _raw("w"))["ttl"], 300 * 10**9)


async def _stmt_queryset_patch() -> None:
    await _saved()
    count = await RfEvent.objects().filter(id="w").patch([{"op": "replace", "path": "/ttl", "value": timedelta(minutes=5)}])
    assert count == 1
    _assert_sdk_duration((await _raw("w"))["ttl"], 300 * 10**9)


async def _stmt_atomic_append() -> None:
    ev = await _saved()
    await ev.atomic_append("ttls", timedelta(hours=2))
    ttls = (await _raw("w"))["ttls"]
    assert [d.elapsed for d in ttls] == [_HOUR_NS, 2 * _HOUR_NS]
    assert all(type(d) is surrealdb.Duration for d in ttls)


async def _stmt_atomic_append_many() -> None:
    ev = await _saved()
    await ev.atomic_append_many("ttls", [timedelta(hours=2), timedelta(hours=3)])
    ttls = (await _raw("w"))["ttls"]
    assert [d.elapsed for d in ttls] == [_HOUR_NS, 2 * _HOUR_NS, 3 * _HOUR_NS]
    assert all(type(d) is surrealdb.Duration for d in ttls)


async def _stmt_bulk_update() -> None:
    await _saved()
    count = await (
        RfEvent.objects()
        .filter(id="w")
        .bulk_update(ttl=timedelta(hours=3), day=date(2026, 2, 3), price=Decimal("0.0000005"))  # 5E-7: 2.x exponent case
    )
    assert count == 1
    row = await _raw("w")
    _assert_sdk_duration(row["ttl"], 3 * _HOUR_NS)
    assert row["day"] == datetime(2026, 2, 3, tzinfo=UTC)
    assert isinstance(row["price"], Decimal)
    assert row["price"] == Decimal("0.0000005")


async def _relate_and_check(**relate_kwargs: Any) -> None:
    a = await _saved("a")
    b = RfEvent(id="b")
    await b.save()
    created = await a.relate("rf_rel", b, **relate_kwargs)
    assert len(created) == 1
    client = await SurrealDBConnectionManager.get_client()
    rows = await client.query("SELECT * FROM rf_rel;", {})
    assert len(rows) == 1
    _assert_sdk_duration(rows[0]["ttl"], 60 * 10**9)
    assert isinstance(rows[0]["win"], surrealdb.Range)
    assert rows[0]["win"].end is None  # the open side
    assert Range(BoundIncluded(1), None) == rows[0]["win"]


async def _stmt_relate_data() -> None:
    await _relate_and_check(data={"ttl": timedelta(minutes=1), "win": Range(BoundIncluded(1), None)})


async def _stmt_relate_edge_kwargs() -> None:
    await _relate_and_check(ttl=timedelta(minutes=1), win=Range(BoundIncluded(1), None))


async def _stmt_merge_extra_vars_int() -> None:
    ev = await _saved()
    await ev.merge(server_values={"n": SurrealFunc("$t")}, extra_vars={"t": 7})
    assert (await _raw("w"))["n"] == 7


async def _stmt_merge_extra_vars_duration() -> None:
    ev = await _saved()
    await ev.merge(server_values={"ttl": SurrealFunc("$d")}, extra_vars={"d": timedelta(hours=1)})
    _assert_sdk_duration((await _raw("w"))["ttl"], _HOUR_NS)


async def _stmt_raw_query() -> None:
    await _saved()
    await RfEvent.raw_query("UPDATE $rid SET ttl = $t;", variables={"rid": RecordID("rf_event", "w"), "t": timedelta(hours=4)})
    _assert_sdk_duration((await _raw("w"))["ttl"], 4 * _HOUR_NS)


async def _stmt_queryset_query() -> None:
    await _saved()
    await RfEvent.objects().query(
        "UPDATE (SELECT VALUE id FROM rf_event WHERE id = $rid) SET ttl = $t;",
        variables={"rid": RecordID("rf_event", "w"), "t": timedelta(hours=4)},
    )
    _assert_sdk_duration((await _raw("w"))["ttl"], 4 * _HOUR_NS)


async def _stmt_call_function() -> None:
    async with _rf_add_function():
        _assert_sdk_duration(await SurrealDBConnectionManager.call_function("fn::rf_add", [timedelta(hours=1)]), 2 * _HOUR_NS)
        _assert_sdk_duration(
            await SurrealDBConnectionManager.call_function("fn::rf_add", params={"d": Duration.parse("1h")}), 2 * _HOUR_NS
        )
        _assert_sdk_duration(await RfEvent.call_function("rf_add", (timedelta(hours=1),)), 2 * _HOUR_NS)  # model shortcut


_STATEMENT_WRITES: list[tuple[str, Callable[[], Awaitable[None]]]] = [
    ("model-patch", _stmt_model_patch),
    ("queryset-patch", _stmt_queryset_patch),
    ("atomic_append", _stmt_atomic_append),
    ("atomic_append_many", _stmt_atomic_append_many),
    ("bulk_update", _stmt_bulk_update),
    ("relate-data", _stmt_relate_data),
    ("relate-edge-kwargs", _stmt_relate_edge_kwargs),
    ("merge-extra-vars-int", _stmt_merge_extra_vars_int),
    ("merge-extra-vars-duration", _stmt_merge_extra_vars_duration),
    ("raw_query", _stmt_raw_query),
    ("queryset-query", _stmt_queryset_query),
    ("call_function", _stmt_call_function),
]


def _resave(**changes: Any) -> Callable[[RfEvent], Awaitable[Any]]:
    """A ``save()`` of record ``r`` with ``FULL`` and ``changes`` (a full replace, were it sent)."""
    return lambda _ev: RfEvent(id="r", **{**FULL, **changes}).save()


# (label, the refused call on the saved record ``r``, the exception, needs a probe that cannot decode 0ns)
_REFUSALS: list[tuple[str, Callable[[RfEvent], Awaitable[Any]], type[Exception], bool]] = [
    ("save-zero-duration", _resave(ttl=timedelta(0)), ValueError, True),
    ("save-decimal-1E+29", _resave(price=Decimal("1E+29")), ValueError, False),
    ("save-decimal-beyond-96-bits", _resave(price=Decimal("79228162514264337593543950335.5")), ValueError, False),
    ("save-int-2**63", _resave(n=2**63), ValueError, False),
    ("merge-negative-duration", lambda ev: ev.merge(ttl=timedelta(seconds=-1)), ValueError, False),
    ("filter-int-2**70", lambda _ev: RfEvent.objects().filter(n__gt=2**70).exec(), ValueError, False),
    ("raw_query-time", lambda _ev: RfEvent.raw_query("RETURN $t;", {"t": time(1)}), TypeError, False),
    ("raw_query-int-2**64", lambda _ev: RfEvent.raw_query("RETURN $n;", {"n": 2**64}), ValueError, False),
    ("bulk_update-zero-duration", lambda _ev: RfEvent.objects().filter(id="r").bulk_update(ttl=Duration(0)), ValueError, True),
    ("atomic_append-zero-duration", lambda ev: ev.atomic_append("ttls", timedelta(0)), ValueError, True),
]


async def _native_txn_supported() -> bool:
    """True if the server exposes the SDK's native transaction RPC (SurrealDB 3.x)."""
    client = await SurrealDBConnectionManager.get_client()
    try:
        txn = await client.begin()
    except Exception:
        return False
    with contextlib.suppress(Exception):
        await client.cancel(txn)
    return True


@contextlib.asynccontextmanager
async def _http_orm_client(*tables: str) -> AsyncIterator[Any]:
    """``orm_client`` over HTTP: transactions there are buffered (sent at commit) on both lines."""
    host, port = os.environ.get("SURREALDB_HOST", "localhost"), os.environ.get("SURREALDB_PORT", "8000")
    SurrealDBConnectionManager.set_connection(
        url=f"http://{host}:{port}", user="root", password="root", namespace="ns", database="db"
    )
    client = await SurrealDBConnectionManager.get_client()
    for table in tables:
        with contextlib.suppress(Exception):
            await client.query(f"REMOVE TABLE {table};", {})
    try:
        yield client
    finally:
        for table in tables:
            with contextlib.suppress(Exception):
                await client.query(f"REMOVE TABLE {table};", {})
        await SurrealDBConnectionManager.close_connection()


def _assert_full_declared(ev: RfEvent, n: int = 5) -> None:
    """A hydrated ``FULL`` row: the declared Python types and the values written."""
    assert type(ev.at) is datetime
    assert ev.at == FULL["at"]
    assert type(ev.day) is date
    assert ev.day == FULL["day"]
    assert type(ev.ttl) is timedelta
    assert ev.ttl == FULL["ttl"]
    assert type(ev.grace) is Duration
    assert ev.grace == FULL["grace"]
    assert ev.price == Decimal("12.5")
    assert type(ev.token) is UUID
    assert ev.token == _TOKEN
    assert type(ev.hours) is Range
    assert ev.hours == FULL["hours"]
    assert type(ev.period) is Range
    assert ev.period == FULL["period"]
    assert ev.blob == FULL["blob"]
    assert type(ev.tags) is set
    assert ev.tags == {"a", "b"}
    assert ev.ttls == FULL["ttls"]
    assert all(type(d) is timedelta for d in ev.ttls)
    assert ev.n == n


class TestRichStatementWritesE2E:
    @pytest.mark.parametrize("write", [case for _, case in _STATEMENT_WRITES], ids=[name for name, _ in _STATEMENT_WRITES])
    async def test_statement_writes_store_native_types(self, write: Callable[[], Awaitable[None]]) -> None:
        async with orm_client(*_STMT_TABLES):
            await write()

    @pytest.mark.parametrize(
        ("call", "error", "needs_zero_refusal"),
        [(call, error, zero) for _, call, error, zero in _REFUSALS],
        ids=[name for name, _, _, _ in _REFUSALS],
    )
    async def test_refusals_leave_the_client_untouched(
        self, call: Callable[[RfEvent], Awaitable[Any]], error: type[Exception], needs_zero_refusal: bool
    ) -> None:
        if needs_zero_refusal and sdk_decodes_zero_duration():
            pytest.skip("the installed SDK decodes a zero duration: storing one is allowed")
        async with orm_client(*_STMT_TABLES):
            ev = await _saved("r")
            before = await _raw("r")
            client_before = await SurrealDBConnectionManager.get_client()

            with pytest.raises(error):
                await call(ev)

            assert (await SurrealDBConnectionManager.get_client()) is client_before
            assert await client_before.query("RETURN 1;", {}) == 1
            assert await _raw("r") == before

    async def test_zero_duration_filter_is_allowed(self) -> None:
        async with orm_client(*_STMT_TABLES):
            await _saved("z")
            rows = await RfEvent.objects().filter(ttl__gt=timedelta(0)).exec()  # filters are stored=False
            assert [row.id for row in rows] == ["z"]

    async def test_non_finite_decimal_round_trip(self) -> None:
        async with orm_client(*_STMT_TABLES) as client:
            for key, value in (("nan", Decimal("NaN")), ("inf", Decimal("Infinity")), ("ninf", Decimal("-Infinity"))):
                await RfNan(id=key, d=value).save()  # sent unchanged, as in v0.22.1

                got = (await RfNan.objects().get(key)).d
                assert type(got) is Decimal
                if value.is_nan():
                    assert got.is_nan()
                else:
                    assert got == value
                stored = await client.query("SELECT VALUE d FROM ONLY $rid;", {"rid": RecordID("rf_nan", key)})
                assert isinstance(stored, float)  # SurrealDB decimals have no NaN/±Infinity: a float

            with pytest.raises(ValidationError):
                RfEvent(price=Decimal("NaN"))  # a plain Decimal field still refuses it (Pydantic)

    async def test_raw_paths_send_zero_durations(self) -> None:
        async with orm_client(*_STMT_TABLES) as client:
            await _saved("z")
            rows = await RfEvent.raw_query("SELECT * FROM rf_event WHERE ttl > $z;", {"z": timedelta(0)})
            assert [row.id for row in rows if isinstance(row, RfEvent)] == ["z"]

            async with _rf_add_function():
                _assert_sdk_duration(await SurrealDBConnectionManager.call_function("fn::rf_add", [timedelta(0)]), _HOUR_NS)

            assert await RfEvent.raw_query("RETURN $x > 1;", {"x": Decimal("NaN")}) == []  # sent, no exception
            assert await client.query("RETURN 1;", {}) == 1


class TestRichTransactionsE2E:
    async def test_buffered_tx_round_trip(self) -> None:
        async with _http_orm_client(*_STMT_TABLES):
            ev, other = RfEvent(id="t", **FULL), RfEvent(id="m")
            async with SurrealDBConnectionManager.transaction() as tx:
                assert tx.is_interactive is False
                await ev.save(tx=tx)
                await other.save(tx=tx)
                await other.merge(tx=tx, ttl=timedelta(hours=2))

            _assert_native(await _raw("t"))
            _assert_sdk_duration((await _raw("m"))["ttl"], 2 * _HOUR_NS)
            _assert_full_declared(await RfEvent.objects().get("t"))
            assert (await RfEvent.objects().get("m")).ttl == timedelta(hours=2)

    async def test_interactive_tx_round_trip(self) -> None:
        async with orm_client(*_STMT_TABLES):
            if not await _native_txn_supported():
                pytest.skip("requires SurrealDB 3.x native transactions")
            ev, other = RfEvent(id="t", **FULL), RfEvent(id="m")
            async with SurrealDBConnectionManager.transaction() as tx:
                assert tx.is_interactive is True
                await ev.save(tx=tx)
                await other.save(tx=tx)
                await other.merge(tx=tx, ttl=timedelta(hours=2))
                _assert_full_declared(await RfEvent.objects(tx=tx).get("t"))  # a read inside the transaction
                assert (await RfEvent.objects(tx=tx).get("m")).ttl == timedelta(hours=2)

            _assert_native(await _raw("t"))
            _assert_sdk_duration((await _raw("m"))["ttl"], 2 * _HOUR_NS)
            _assert_full_declared(await RfEvent.objects().get("t"))


# ==================== Task 7 — rows returned by writes, return types, the full read matrix ====================


class RfServer(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_server", server_fields=["grace"])

    id: str | None = None
    grace: Duration | None = None
    note: str = ""


class RfCf(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_cf")

    id: str | None = None
    ttl: timedelta | None = None


class RfTagged(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_tagged")

    id: str | None = None
    tags: set[str] = set()


class RfComp(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_comp")

    id: str | None = None
    starts: datetime
    ends: datetime
    length: Computed[timedelta] = computed("ends - starts")


_READ_TABLES = ("rf_event", "rf_link", "rf_server", "rf_cf", "rf_tagged", "rf_comp")


async def _retrying(operation: Callable[[], Awaitable[Any]]) -> Any:
    """A write to a change-feed table, retried on SurrealDB's retryable conflicts (seen once on 2.7.0 in CI)."""

    @retry_on_conflict(max_retries=5)
    async def _run() -> Any:
        return await operation()

    return await _run()


# --- read paths: a server-written row (SEED) hydrates as the declared types (AC-12) ---


async def _read_get() -> RfEvent:
    ev: RfEvent = await RfEvent.objects().get("s")
    return ev


async def _read_filter() -> RfEvent:
    rows = await RfEvent.objects().filter(n=1).exec()
    assert len(rows) == 1
    return rows[0]


async def _read_all() -> RfEvent:
    rows = await RfEvent.objects().all()
    assert len(rows) == 1
    ev: RfEvent = rows[0]
    return ev


async def _read_first() -> RfEvent:
    ev = await RfEvent.objects().first()
    assert isinstance(ev, RfEvent)
    return ev


async def _read_get_or_create() -> RfEvent:
    ev, created = await RfEvent.objects().get_or_create(id="s")
    assert not created
    assert isinstance(ev, RfEvent)
    return ev


async def _read_refresh() -> RfEvent:
    ev = RfEvent(id="s")
    await ev.refresh()  # the row is applied by _apply_record
    return ev


async def _read_raw_query() -> RfEvent:
    rows = await RfEvent.raw_query("SELECT * FROM rf_event:s;")
    assert len(rows) == 1
    ev = rows[0]
    assert isinstance(ev, RfEvent)
    return ev


async def _read_in_transaction() -> RfEvent:
    if not await _native_txn_supported():
        pytest.skip("requires SurrealDB 3.x native transactions")
    async with SurrealDBConnectionManager.transaction() as tx:
        ev: RfEvent = await RfEvent.objects(tx=tx).get("s")
    return ev


async def _read_refresh_in_transaction() -> RfEvent:
    if not await _native_txn_supported():
        pytest.skip("requires SurrealDB 3.x native transactions")
    ev = RfEvent(id="s")
    async with SurrealDBConnectionManager.transaction() as tx:
        await ev.refresh(tx=tx)  # _apply_record, inside the transaction
    return ev


_READ_PATHS: list[tuple[str, Callable[[], Awaitable[RfEvent]]]] = [
    ("get", _read_get),
    ("filter-exec", _read_filter),
    ("all", _read_all),
    ("first", _read_first),
    ("get_or_create-existing", _read_get_or_create),
    ("refresh", _read_refresh),
    ("raw_query", _read_raw_query),
    ("objects-tx-get", _read_in_transaction),
    ("refresh-tx", _read_refresh_in_transaction),
]


# --- write paths: the row a write sends back keeps the declared types on the instance (AC-13) ---


async def _sync_save_auto_id() -> tuple[RfEvent, int]:
    ev = RfEvent(**FULL)
    await ev.save()  # auto id: the whole returned row is applied
    return ev, 5


async def _sync_save_server_values() -> tuple[RfEvent, int]:
    ev = RfEvent(id="v", **FULL)
    await ev.save(server_values={"n": SurrealFunc("1 + 1")})
    return ev, 2


async def _sync_upsert() -> tuple[RfEvent, int]:
    ev = RfEvent(id="u", **FULL)
    await ev.upsert()
    return ev, 5


async def _sync_merge() -> tuple[RfEvent, int]:
    ev = await _saved("m")
    await ev.merge(n=2)
    return ev, 2


async def _sync_patch() -> tuple[RfEvent, int]:
    ev = await _saved("p")
    await ev.patch([{"op": "replace", "path": "/n", "value": 3}])
    return ev, 3


async def _sync_atomic_increment() -> tuple[RfEvent, int]:
    ev = await _saved("i")
    await ev.atomic_increment("n", 1)
    return ev, 6


async def _sync_refresh() -> tuple[RfEvent, int]:
    ev = await _saved("r")
    await ev.refresh()
    return ev, 5


async def _sync_bulk_create() -> tuple[RfEvent, int]:
    created = await RfEvent.objects().bulk_create([RfEvent(id="b", **FULL)])
    assert len(created) == 1
    ev = created[0]
    assert isinstance(ev, RfEvent)
    return ev, 5


_SYNC_PATHS: list[tuple[str, Callable[[], Awaitable[tuple[RfEvent, int]]]]] = [
    ("save-auto-id", _sync_save_auto_id),
    ("save-server-values", _sync_save_server_values),
    ("upsert", _sync_upsert),
    ("merge", _sync_merge),
    ("patch", _sync_patch),
    ("atomic_increment", _sync_atomic_increment),
    ("refresh", _sync_refresh),
    ("bulk_create", _sync_bulk_create),
]


class TestRichReadsE2E:
    @pytest.mark.parametrize("read", [case for _, case in _READ_PATHS], ids=[name for name, _ in _READ_PATHS])
    async def test_read_path_returns_declared_types(self, read: Callable[[], Awaitable[RfEvent]]) -> None:
        async with orm_client(*_READ_TABLES) as client:
            await client.query(SEED, {})
            _assert_declared(await read())

    @pytest.mark.parametrize("write", [case for _, case in _SYNC_PATHS], ids=[name for name, _ in _SYNC_PATHS])
    async def test_returned_rows_sync_declared_types(self, write: Callable[[], Awaitable[tuple[RfEvent, int]]]) -> None:
        async with orm_client(*_READ_TABLES):
            ev, n = await write()
            assert isinstance(ev.get_id(), (str, RecordID))
            _assert_full_declared(ev, n=n)  # tags back to a set, ttl a timedelta, grace a Duration…

    async def test_server_default_hydrates(self) -> None:
        async with orm_client(*_READ_TABLES) as client:
            await client.query("DEFINE FIELD grace ON rf_server DEFAULT 3h;", {})
            ev = RfServer(id="d", note="x")
            await ev.save()  # explicit id: only the server fields are applied
            assert type(ev.grace) is Duration
            assert ev.grace == Duration.parse("3h")
            assert ev.note == "x"

    async def test_live_and_change_feed_instances_typed(self) -> None:
        async with orm_client(*_READ_TABLES) as client:
            await client.query("DEFINE TABLE rf_event SCHEMALESS;", {})
            async with asyncio.timeout(10), RfEvent.objects().live() as stream:
                await RfEvent(id="l", **FULL).save()
                event = await anext(stream)
            assert event.record_id == "rf_event:l"
            _assert_full_declared(event.instance)
            _assert_sdk_duration(event.raw["ttl"], 5400 * 10**9)  # raw keeps the SDK values
            assert isinstance(event.raw["hours"], surrealdb.Range)

            await client.query("DEFINE TABLE rf_cf SCHEMALESS CHANGEFEED 1h;", {})
            # Started before the write rather than ``since=0``: on 3.x a cursor is clock-based, and
            # ``SHOW CHANGES … SINCE 0`` returns nothing (measured on 3.3.4).
            changes = await RfCf.objects().changes(poll_interval=0.05).start()
            try:
                await _retrying(lambda: RfCf(id="c", ttl=timedelta(hours=1, minutes=30)).save())
                async with asyncio.timeout(10):
                    change = await anext(changes)
            finally:
                changes.stop()
            assert change.record_id == "rf_cf:c"
            assert isinstance(change.instance, RfCf)
            assert type(change.instance.ttl) is timedelta
            assert change.instance.ttl == timedelta(hours=1, minutes=30)

    async def test_edge_hydrates_declared_types(self) -> None:
        async with orm_client(*_READ_TABLES) as client:
            a, b = RfEvent(id="a"), RfEvent(id="b")
            await a.save()
            await b.save()

            link = RfLink(in_=a, out=b, ttl=timedelta(minutes=5), window=Range(BoundIncluded(1), BoundExcluded(5)))
            await link.save()  # auto id: the returned row is applied
            assert type(link.ttl) is timedelta
            assert link.ttl == timedelta(minutes=5)
            assert type(link.window) is Range
            assert link.window == Range(BoundIncluded(1), BoundExcluded(5))

            await client.query("RELATE rf_event:a->rf_link:r->rf_event:b SET ttl = 1h30m1ns, window = 1..=5;", {})
            got = await RfLink.objects().get("r")
            assert type(got.ttl) is timedelta
            assert got.ttl == timedelta(hours=1, minutes=30)
            assert type(got.window) is Range
            assert got.window == Range(BoundIncluded(1), BoundIncluded(5))

            got.ttl = timedelta(hours=2)
            await got.refresh()  # the stored row comes back through _apply_record
            assert type(got.ttl) is timedelta
            assert got.ttl == timedelta(hours=1, minutes=30)
            assert type(got.window) is Range

    async def test_set_field_on_array_column(self) -> None:
        async with orm_client(*_READ_TABLES) as client:
            await client.query("DEFINE FIELD tags ON rf_tagged TYPE array<string> VALUE $value.distinct();", {})
            tagged = RfTagged(tags={"b", "a"})
            await tagged.save()  # auto id: the row's list comes back as the declared set
            assert type(tagged.tags) is set
            assert tagged.tags == {"a", "b"}

            rid = tagged.get_id()
            assert isinstance(rid, RecordID)
            assert await client.query("SELECT VALUE tags FROM ONLY $rid;", {"rid": rid}) == ["a", "b"]  # a list
            got = await RfTagged.objects().get(tagged.get_raw_id())
            assert type(got.tags) is set
            assert got.tags == {"a", "b"}

            await client.query("UPDATE $rid SET tags = ['c', 'c', 'a'];", {"rid": rid})
            await tagged.refresh()
            assert type(tagged.tags) is set
            assert tagged.tags == {"a", "c"}

    async def test_precision_contract(self) -> None:
        async with orm_client(*_READ_TABLES) as client:
            exact = Duration(5400 * 10**9 + 1)
            await RfEvent(id="p", grace=exact, ttl=exact.to_timedelta()).save()
            got = await RfEvent.objects().get("p")
            assert got.grace is not None
            assert got.grace.elapsed == 5400 * 10**9 + 1  # Duration keeps the nanosecond
            assert got.grace.elapsed % 1000 == 1
            assert got.ttl == timedelta(hours=1, minutes=30)  # timedelta floors it

            await client.query("CREATE rf_event:ns SET at = d'2026-01-02T03:04:05.123456789Z';", {})  # never time::now()
            read_back = (await RfEvent.objects().get("ns")).at
            assert read_back is not None
            assert read_back.microsecond == 123456
            assert await RfEvent.objects().filter(at=read_back).exec() == []  # µs ≠ the stored ns
            assert [ev.id for ev in await RfEvent.objects().filter(at__gte=read_back).exec()] == ["ns"]

            await RfEvent(id="naive", at=datetime(2026, 1, 1)).save()
            naive = (await RfEvent.objects().get("naive")).at
            assert type(naive) is datetime
            assert naive.utcoffset() == timedelta(0)
            assert naive == datetime(2026, 1, 1, tzinfo=UTC)

    async def test_computed_and_call_function_return_types(self) -> None:
        async with orm_client(*_READ_TABLES):
            await RfComp.define_computed_fields()
            starts = datetime(2026, 1, 2, 9, tzinfo=UTC)
            comp = RfComp(id="c", starts=starts, ends=starts + timedelta(hours=1, minutes=30))
            await comp.save()  # explicit id: the computed field comes back with the server fields
            assert type(comp.length) is timedelta
            assert comp.length == timedelta(hours=1, minutes=30)
            loaded = await RfComp.objects().get("c")
            assert type(loaded.length) is timedelta
            assert loaded.length == timedelta(hours=1, minutes=30)

            async with _rf_add_function():
                as_timedelta = await SurrealDBConnectionManager.call_function(
                    "fn::rf_add", [timedelta(hours=1)], return_type=timedelta
                )
                assert type(as_timedelta) is timedelta
                assert as_timedelta == timedelta(hours=2)
                as_duration = await SurrealDBConnectionManager.call_function(
                    "fn::rf_add", [timedelta(hours=1)], return_type=Duration
                )
                assert type(as_duration) is Duration
                assert as_duration == Duration.parse("2h")
                as_list = await SurrealDBConnectionManager.call_function(
                    "fn::rf_add", [Duration.parse("1h30m1ns")], return_type=list[timedelta] | timedelta
                )
                assert as_list == timedelta(hours=2, minutes=30)  # floored
                via_model = await RfEvent.call_function("rf_add", (timedelta(hours=1),), return_type=timedelta)
                assert via_model == timedelta(hours=2)

    async def test_decimal_trailing_zeros_by_line(self) -> None:
        async with orm_client(*_READ_TABLES) as client:
            await client.query("CREATE rf_event:d SET price = 12.50dec;", {})
            got = (await RfEvent.objects().get("d")).price
            raw = (await _raw("d"))["price"]
            assert got == Decimal("12.5")
            assert raw == Decimal("12.5")
            expected = "12.50" if await _server_version() < (3, 0, 0) else "12.5"
            message = "2.x changed decimal trailing zeros — update README behaviour row 2 (spec §12 item 2)"
            assert str(got) == expected, message
            assert str(raw) == expected, message


# ==================== Task 8 — typed lookups and range membership (spec §4.5) ====================

_TOKEN_A = "0190c3d1-5c3b-7cc6-8f8a-2d2b1f0e9a0a"
_TOKEN_B = "0190c3d1-5c3b-7cc6-8f8a-2d2b1f0e9a0b"
_TOKEN_C = "0190c3d1-5c3b-7cc6-8f8a-2d2b1f0e9a0c"

# Three rows written with SurrealQL literals: native types whatever the ORM's write path does.
_LOOKUP_SEED = (
    "CREATE rf_event:a SET at = d'2026-01-01T00:00:00Z', day = d'2026-01-01T00:00:00Z', ttl = 30m, grace = 30m, "
    f"price = 5.00dec, token = u'{_TOKEN_A}', n = 1, hours = 0..8, "
    "period = d'2026-01-01T00:00:00Z'..d'2026-02-01T00:00:00Z', ttls = [30m], tags = ['x'];",
    "CREATE rf_event:b SET at = d'2026-06-01T00:00:00Z', day = d'2026-06-01T00:00:00Z', ttl = 1h30m, "
    f"grace = 1h30m1ns, price = 9.99dec, token = u'{_TOKEN_B}', n = 5, hours = 9..17, "
    "period = d'2026-06-01T00:00:00Z'.., ttls = [1h, 2h], tags = ['y'];",
    "CREATE rf_event:c SET at = d'2027-01-01T00:00:00Z', day = d'2027-01-01T00:00:00Z', ttl = 3h, grace = 3h, "
    f"price = 20dec, token = u'{_TOKEN_C}', n = 10, hours = 18..=23, "
    "period = d'2027-01-01T00:00:00Z'..d'2027-02-01T00:00:00Z', ttls = [3h], tags = ['z'];",
)

# (filter arguments, expected ids) — AC-19. A ``Q`` goes in the tuple, keywords in the dict.
_LOOKUP_CASES: list[tuple[tuple[Any, ...], dict[str, Any], list[str]]] = [
    ((), {"at__gt": "2026-03-01T00:00:00Z"}, ["b", "c"]),
    ((), {"at__lte": "2026-06-01T00:00:00Z"}, ["a", "b"]),
    ((), {"token": _TOKEN_B}, ["b"]),
    ((), {"price__lte": 9.99}, ["a", "b"]),
    ((), {"price__gt": "5"}, ["b", "c"]),
    ((), {"ttl__gt": "1h"}, ["b", "c"]),
    ((), {"ttl__lt": timedelta(hours=1)}, ["a"]),
    ((), {"grace": Duration.parse("1h30m1ns")}, ["b"]),
    ((), {"grace__gte": "1h30m1ns"}, ["b", "c"]),
    ((), {"day": date(2026, 6, 1)}, ["b"]),
    ((), {"day__gte": "2026-06-01"}, ["b", "c"]),
    ((), {"token__in": [_TOKEN_A, _TOKEN_C]}, ["a", "c"]),
    ((), {"token__not_in": [_TOKEN_A]}, ["b", "c"]),
    ((), {"n__in": Range(BoundIncluded(1), BoundExcluded(10))}, ["a", "b"]),
    ((), {"n__in": Range(BoundIncluded(5), None)}, ["b", "c"]),
    ((), {"n__in": Range(None, BoundIncluded(5))}, ["a", "b"]),
    ((), {"n__in": surrealdb.Range(BoundIncluded(5), BoundIncluded(10))}, ["b", "c"]),
    ((), {"n__not_in": Range(BoundIncluded(1), BoundIncluded(5))}, ["c"]),
    ((), {"n__not_in": Range(BoundExcluded(1), None)}, ["a"]),
    ((), {"at__in": Range(BoundIncluded("2026-03-01T00:00:00Z"), None)}, ["b", "c"]),
    ((), {"hours__contains": 12}, ["b"]),
    ((), {"hours__contains": "23"}, ["c"]),
    ((), {"hours__contains": 17}, []),
    ((), {"hours__not_contains": "4"}, ["b", "c"]),
    ((), {"period__contains": "2026-01-15T00:00:00Z"}, ["a"]),
    ((), {"period__contains": datetime(2026, 7, 1, tzinfo=UTC)}, ["b"]),
    ((), {"ttls__contains": "1h"}, ["b"]),
    ((), {"ttls__containsany": ["30m", "3h"]}, ["a", "c"]),
    ((), {"ttls__containsall": ["1h", "2h"]}, ["b"]),
    ((), {"hours": Range(BoundIncluded(9), BoundExcluded(17))}, ["b"]),
    ((), {"hours": "9..17"}, ["b"]),
    ((), {"hours": range(9, 17)}, ["b"]),
    ((), {"hours": "18..=23"}, ["c"]),
    ((Q(ttl__gt="2h") | ~Q(price__gt="5"),), {}, ["a", "c"]),
]


async def _seed_lookups(client: Any) -> None:
    for statement in _LOOKUP_SEED:
        await client.query(statement, {})


async def _ids(*args: Any, **kwargs: Any) -> list[str]:
    return sorted(str(ev.id) for ev in await RfEvent.objects().filter(*args, **kwargs).exec())


class TestTypedLookupsE2E:
    """Filter values on rich fields compare with the stored native values, on both lines (AC-19, AC-20)."""

    async def test_typed_queries_return_the_expected_rows(self) -> None:
        async with orm_client("rf_event") as client:
            await _seed_lookups(client)
            mismatches = []
            for args, kwargs, expected in _LOOKUP_CASES:
                got = await _ids(*args, **kwargs)
                if got != expected:
                    mismatches.append(f"filter({args or ''}{kwargs or ''}) -> {got}, expected {expected}")
            assert not mismatches, "\n".join(mismatches)
            assert await RfEvent.objects().filter(ttl__gte="1h").count() == 2
            assert await RfEvent.objects().filter(token=_TOKEN_C).exists()

    async def test_typed_filters_drive_queryset_writes(self) -> None:
        async with orm_client("rf_event") as client:
            await _seed_lookups(client)
            assert await RfEvent.objects().filter(ttl__gte="1h").bulk_update(n=9) == 2
            assert [(await _raw(rid))["n"] for rid in ("a", "b", "c")] == [1, 9, 9]

            patched = await RfEvent.objects().filter(price__gte=10).patch([{"op": "replace", "path": "/n", "value": 42}])
            assert patched == 1
            assert (await _raw("c"))["n"] == 42

            assert await RfEvent.objects().filter(token=_TOKEN_A).bulk_delete() == 1
            assert sorted(str(ev.id) for ev in await RfEvent.objects().all()) == ["b", "c"]

    async def test_get_or_create_with_a_uuid_string_converges(self) -> None:
        token = "0190c3d1-5c3b-7cc6-8f8a-2d2b1f0e9a99"
        async with orm_client("rf_event"):
            first, created = await RfEvent.objects().get_or_create(token=token, defaults={"n": 1})
            assert created
            assert first.token == UUID(token)
            again, created_again = await RfEvent.objects().get_or_create(token=token, defaults={"n": 1})
            assert not created_again  # the lookup binds the UUID the create stored
            assert again._record_id() == first._record_id()
            assert await RfEvent.objects().filter(token=token).count() == 1

            updated, created_update = await RfEvent.objects().update_or_create(token=token, defaults={"n": 2})
            assert not created_update
            assert updated.n == 2
            assert len(await RfEvent.objects().all()) == 1
