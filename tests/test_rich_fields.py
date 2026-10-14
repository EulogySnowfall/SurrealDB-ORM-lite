"""Rich field types through the ORM (v0.23.0): every write stores native SurrealDB values, every read
hydrates the declared Python types, on SurrealDB 2.x and 3.x alike.

- **Writes** (spec §5.2): the model payload (``save``, ``update``, ``upsert``, ``bulk_create``, edges,
  ``*_or_create``, ``server_values``) and the keyword payload of ``merge()`` go through
  ``wire.encode_wire(…, stored=True)``: a ``timedelta``/``Duration`` becomes a duration, a ``date``
  midnight UTC, a ``Range`` a range, a ``set`` an array, a ``Decimal`` a fixed-point decimal, a
  Pydantic sub-model an object keyed as its validation reads it (``merge`` takes a dict key by key,
  ``bulk_update``/``signup`` the whole sub-model), a table-model instance its record id. A raw
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
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import pytest
import pytest_asyncio
import surrealdb
from pydantic import AliasChoices, AwareDatetime, BaseModel, ConfigDict, Field, PastDate, ValidationError
from pydantic.alias_generators import to_camel
from surrealdb import RecordID

from surreal_orm_lite import (
    AuthenticatedUserMixin,
    BaseSurrealEdge,
    BaseSurrealModel,
    BoundExcluded,
    BoundIncluded,
    Computed,
    Duration,
    LiveAction,
    Q,
    Range,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    SurrealFunc,
    Var,
    computed,
    retry_on_conflict,
)
from surreal_orm_lite._sdk import TAG_DECIMAL_STRING, TAG_DURATION_COMPACT, CBORTag
from surreal_orm_lite.auth import build_auth_payload
from surreal_orm_lite.model_base import _set_paths
from surreal_orm_lite.wire import (
    child_annotation,
    encode_wire,
    masking_keys,
    read_keys,
    sdk_decodes_zero_duration,
)
from tests.conftest import orm_client
from tests.test_rich_types import _http_client, _server_version

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


# ==================== Task 9 — keyword writes and duration increments (spec §4.6) ====================

_MINUTE_NS = 60 * 10**9
_DAY = timedelta(days=1)


def _spy_atomic(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the ``SET`` expression of every ``_atomic_update`` call, which still runs."""
    compiled: list[str] = []
    original = BaseSurrealModel._atomic_update

    async def spy(self: BaseSurrealModel, set_expr: str, variables: dict[str, Any], tx: Any) -> Any:
        compiled.append(set_expr)
        return await original(self, set_expr, variables, tx)

    monkeypatch.setattr(BaseSurrealModel, "_atomic_update", spy)
    return compiled


async def _assert_client_usable(client_before: Any) -> None:
    assert (await SurrealDBConnectionManager.get_client()) is client_before
    assert await client_before.query("RETURN 1;", {}) == 1


class TestKeywordWritesE2E:
    """``merge``/``bulk_update``/``update_or_create`` validate keyword values on rich fields, and
    ``atomic_increment`` adds durations the same way on both lines (AC-21, AC-22)."""

    async def test_merge_validates_rich_fields(self) -> None:
        async with orm_client("rf_event"):
            ev = RfEvent(id="k")
            await ev.save()
            await ev.merge(
                ttl="1h30m",
                at="2026-01-01T00:00:00Z",
                price="1.50",
                token=str(_TOKEN),
                hours=range(1, 3),
                day="2026-01-02",
                grace="1h30m1ns",
                ttls=["1h", timedelta(hours=2)],
                tags=["b", "a"],
            )

            row = await _raw("k")
            _assert_sdk_duration(row["ttl"], 5400 * 10**9)
            assert isinstance(row["at"], datetime)
            assert row["at"] == _JAN
            assert isinstance(row["price"], Decimal)
            assert row["price"] == Decimal("1.5")
            assert type(row["token"]) is UUID
            assert row["token"] == _TOKEN
            assert isinstance(row["hours"], surrealdb.Range)
            assert Range(BoundIncluded(1), BoundExcluded(3)) == row["hours"]
            assert row["day"] == datetime(2026, 1, 2, tzinfo=UTC)
            _assert_sdk_duration(row["grace"], 5400 * 10**9 + 1)
            assert [d.elapsed for d in row["ttls"]] == [_HOUR_NS, 2 * _HOUR_NS]
            assert sorted(row["tags"]) == ["a", "b"]

            assert type(ev.ttl) is timedelta
            assert ev.ttl == timedelta(hours=1, minutes=30)
            assert type(ev.at) is datetime
            assert ev.at == _JAN
            assert type(ev.price) is Decimal
            assert ev.price == Decimal("1.5")
            assert type(ev.token) is UUID
            assert ev.token == _TOKEN
            assert type(ev.hours) is Range
            assert ev.hours == Range(BoundIncluded(1), BoundExcluded(3))
            assert type(ev.day) is date
            assert ev.day == date(2026, 1, 2)
            assert type(ev.grace) is Duration
            assert ev.grace == Duration.parse("1h30m1ns")
            assert ev.ttls == [timedelta(hours=1), timedelta(hours=2)]
            assert type(ev.tags) is set
            assert ev.tags == {"a", "b"}

    async def test_merge_refresh_false_and_buffered_apply_coerced_values(self) -> None:
        async with orm_client("rf_event"):
            ev = RfEvent(id="f")
            await ev.save()
            await ev.merge(refresh=False, ttl="2h", token=str(_TOKEN))
            assert type(ev.ttl) is timedelta
            assert ev.ttl == timedelta(hours=2)
            assert type(ev.token) is UUID
            _assert_sdk_duration((await _raw("f"))["ttl"], 2 * _HOUR_NS)

            # The server_values path (``UPDATE … SET``) validates the literal keywords too.
            await ev.merge(refresh=False, ttl="3h", server_values={"n": SurrealFunc("7")})
            assert type(ev.ttl) is timedelta
            assert ev.ttl == timedelta(hours=3)
            row = await _raw("f")
            _assert_sdk_duration(row["ttl"], 3 * _HOUR_NS)
            assert row["n"] == 7

        async with _http_orm_client("rf_event"):
            ev = RfEvent(id="b")
            await ev.save()
            async with SurrealDBConnectionManager.transaction() as tx:
                assert tx.is_interactive is False
                await ev.merge(tx=tx, ttl="2h", at="2026-01-01T00:00:00Z")
                # Buffered: nothing has run yet, the instance holds the validated keyword values.
                assert type(ev.ttl) is timedelta
                assert ev.ttl == timedelta(hours=2)
                assert type(ev.at) is datetime
                assert ev.at == _JAN

            row = await _raw("b")
            _assert_sdk_duration(row["ttl"], 2 * _HOUR_NS)
            assert isinstance(row["at"], datetime)
            assert row["at"] == _JAN

    async def test_invalid_keyword_value_raises_before_sending(self) -> None:
        async with orm_client("rf_event"):
            ev = await _saved("x")
            before = await _raw("x")
            client_before = await SurrealDBConnectionManager.get_client()

            with pytest.raises(ValueError, match=r"merge\(at=…\).*'yesterday'.*datetime"):
                await ev.merge(at="yesterday")
            with pytest.raises(ValueError, match=r"merge\(ttls=…\)"):
                await ev.merge(ttls=["1h", "soon"])
            with pytest.raises(ValueError, match=r"merge\(ttl=…\)"):
                await ev.merge(refresh=False, ttl="1.5h")
            with pytest.raises(ValueError, match=r"bulk_update\(token=…\).*'nope'.*UUID"):
                await RfEvent.objects().filter(id="x").bulk_update(token="nope")

            assert await _raw("x") == before
            assert ev.at == FULL["at"]  # the instance is untouched too
            assert ev.ttls == FULL["ttls"]
            await _assert_client_usable(client_before)

    async def test_bulk_update_validates(self) -> None:
        async with orm_client("rf_event"):
            await RfEvent(id="a", n=1).save()
            await RfEvent(id="b", n=2).save()

            updated = (
                await RfEvent.objects().filter(n=1).bulk_update(ttl="2h", token=str(_TOKEN), ttls=["1h"], day="2026-01-02")
            )
            assert updated == 1

            row = await _raw("a")
            _assert_sdk_duration(row["ttl"], 2 * _HOUR_NS)
            assert type(row["token"]) is UUID
            assert [d.elapsed for d in row["ttls"]] == [_HOUR_NS]
            assert row["day"] == datetime(2026, 1, 2, tzinfo=UTC)
            assert (await _raw("b")).get("ttl") is None
            assert (await RfEvent.objects().get("a")).ttl == timedelta(hours=2)

    async def test_update_or_create_validates_both_branches(self) -> None:
        async with orm_client("rf_event"):
            created_obj, created = await RfEvent.objects().update_or_create(id="u", defaults={"ttl": "3h"})
            assert created  # the constructor validates (set_data)
            assert created_obj.ttl == timedelta(hours=3)
            _assert_sdk_duration((await _raw("u"))["ttl"], 3 * _HOUR_NS)

            updated_obj, created_again = await RfEvent.objects().update_or_create(id="u", defaults={"ttl": "4h"})
            assert not created_again  # the merge branch validates
            assert type(updated_obj.ttl) is timedelta
            assert updated_obj.ttl == timedelta(hours=4)
            _assert_sdk_duration((await _raw("u"))["ttl"], 4 * _HOUR_NS)

    async def test_allow_inf_nan_constraint_is_honoured(self) -> None:
        async with orm_client("rf_nan"):
            obj = RfNan(id="n")
            await obj.save()
            await obj.merge(d=Decimal("NaN"))  # the field allows it: sent unchanged, as in v0.22.1
            got = (await RfNan.objects().get("n")).d
            assert type(got) is Decimal
            assert got.is_nan()

    async def test_raw_plus_equals_duration_is_a_noop_on_2x(self) -> None:
        async with orm_client("rf_event") as client:
            await client.query("CREATE rf_event:p SET ttl = 1h;", {})
            amount = encode_wire(timedelta(minutes=30), stored=True)
            await client.query("UPDATE rf_event:p SET ttl += $a;", {"a": amount})

            got = (await _raw("p"))["ttl"]
            if await _server_version() < (3, 0, 0):
                message = "2.x now applies += with a duration — update README behaviour row 4 (spec §12 item 2)"
                assert got.elapsed == _HOUR_NS, message
            else:
                assert got.elapsed == _HOUR_NS + 30 * _MINUTE_NS

    async def test_atomic_increment_duration_both_lines(self, monkeypatch: pytest.MonkeyPatch) -> None:
        compiled = _spy_atomic(monkeypatch)
        async with orm_client("rf_event") as client:
            await client.query("CREATE rf_event:t SET ttl = 1h, grace = 1h30m1ns, n = 1;", {})
            await client.query("CREATE rf_event:e SET n = 1;", {})  # no ttl, no grace
            ev = await RfEvent.objects().get("t")
            empty = await RfEvent.objects().get("e")

            await ev.atomic_increment("ttl", timedelta(minutes=30))
            assert compiled[-1] == "ttl = (ttl ?? 0ns) + $amount"
            _assert_sdk_duration((await _raw("t"))["ttl"], _HOUR_NS + 30 * _MINUTE_NS)
            assert type(ev.ttl) is timedelta
            assert ev.ttl == timedelta(hours=1, minutes=30)

            await empty.atomic_increment("ttl", timedelta(minutes=30))
            _assert_sdk_duration((await _raw("e"))["ttl"], 30 * _MINUTE_NS)  # missing column → the amount
            assert empty.ttl == timedelta(minutes=30)

            await ev.atomic_increment("grace", Duration.parse("1ns"))
            _assert_sdk_duration((await _raw("t"))["grace"], 5400 * 10**9 + 2)
            assert ev.grace == Duration.parse("1h30m2ns")

            await ev.atomic_increment("ttl", surrealdb.Duration(_HOUR_NS))  # an SDK Duration too
            _assert_sdk_duration((await _raw("t"))["ttl"], 2 * _HOUR_NS + 30 * _MINUTE_NS)

            await empty.atomic_increment("grace", Duration.parse("1h"))
            _assert_sdk_duration((await _raw("e"))["grace"], _HOUR_NS)

            await ev.atomic_increment("n", 2)
            assert compiled[-1] == "n += $amount"  # numeric amounts: the v0.11.0 statement
            assert (await _raw("t"))["n"] == 3

    async def test_atomic_increment_shifts_datetime_and_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        compiled = _spy_atomic(monkeypatch)
        async with orm_client("rf_event") as client:
            await client.query("CREATE rf_event:d SET at = d'2026-01-02T03:04:05Z', day = d'2026-01-02T00:00:00Z';", {})
            await client.query("CREATE rf_event:m SET n = 1;", {})  # no at, no day
            ev = await RfEvent.objects().get("d")
            missing = await RfEvent.objects().get("m")
            client_before = await SurrealDBConnectionManager.get_client()

            await ev.atomic_increment("at", _DAY)
            assert compiled[-1] == "at = at + $amount"  # no `?? 0ns`: never a duration in a datetime
            assert (await _raw("d"))["at"] == datetime(2026, 1, 3, 3, 4, 5, tzinfo=UTC)
            assert type(ev.at) is datetime
            assert ev.at == datetime(2026, 1, 3, 3, 4, 5, tzinfo=UTC)

            with pytest.raises(surrealdb.ServerError, match="addition"):
                await missing.atomic_increment("at", _DAY)
            assert "at" not in await _raw("m")  # nothing written, never a duration
            await _assert_client_usable(client_before)

            await ev.atomic_increment("day", _DAY)
            assert compiled[-1] == "day = day + $amount"
            assert (await _raw("d"))["day"] == datetime(2026, 1, 3, tzinfo=UTC)
            assert type(ev.day) is date
            assert ev.day == date(2026, 1, 3)
            assert (await RfEvent.objects().get("d")).day == date(2026, 1, 3)

            with pytest.raises(surrealdb.ServerError, match="addition"):
                await missing.atomic_increment("day", Duration.parse("2d"))
            assert "day" not in await _raw("m")

            before = await _raw("d")
            with pytest.raises(ValueError, match="whole number of days"):
                await ev.atomic_increment("day", timedelta(hours=1))
            assert await _raw("d") == before
            await _assert_client_usable(client_before)

    @pytest.mark.parametrize(
        "amount",
        [timedelta(0), timedelta(seconds=-1), Duration(0), surrealdb.Duration(0)],
        ids=["timedelta-0", "timedelta-negative", "Duration-0", "sdk-Duration-0"],
    )
    async def test_atomic_increment_refuses_non_positive_durations(self, amount: Any) -> None:
        async with orm_client("rf_event") as client:
            await client.query("CREATE rf_event:z SET ttl = 1h;", {})
            ev = await RfEvent.objects().get("z")
            before = await _raw("z")
            client_before = await SurrealDBConnectionManager.get_client()

            with pytest.raises(ValueError, match="positive"):
                await ev.atomic_increment("ttl", amount)

            assert await _raw("z") == before
            await _assert_client_usable(client_before)


class RfStamped(BaseSurrealModel):
    """Pydantic's own datetime and date types, rich types like the plain ``datetime`` and ``date``."""

    model_config = SurrealConfigDict(table_name="rf_stamped")

    id: str | None = None
    at: AwareDatetime | None = None
    day: PastDate | None = None
    n: int = 0


class TestPydanticDateTypesE2E:
    """An ``AwareDatetime`` or a ``PastDate`` field is a ``datetime`` or a ``date`` one on both lines: its
    filters compare instants, its keyword writes store datetimes, ``atomic_increment`` shifts it and
    a row read back gives a ``date``. At run time these classes are not subclasses of ``datetime``
    and ``date``, so such a field used to keep v0.22's behaviour: a string filter compared texts, a
    merge stored a string, and an increment wrote a duration into the datetime field."""

    async def test_filters_writes_and_reads(self) -> None:
        async with orm_client("rf_stamped") as client:
            stamped = RfStamped(at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC), day=date(2020, 1, 2))
            await stamped.save()  # no id: the row the server sends back is applied
            assert type(stamped.day) is date
            assert stamped.day == date(2020, 1, 2)
            rid = stamped.id
            assert isinstance(rid, RecordID)

            assert await RfStamped.objects().filter(at__gt="2030-01-01T00:00:00Z").exec() == []
            assert [row.id for row in await RfStamped.objects().filter(at__lt="2030-01-01T00:00:00Z").exec()] == [rid.id]
            assert [row.id for row in await RfStamped.objects().filter(day__lte="2020-01-02").exec()] == [rid.id]

            await stamped.merge(at="2027-01-01T00:00:00Z")
            stored = (await _raw(rid))["at"]
            assert type(stored) is datetime, f"not stored as a datetime: {stored!r}"
            assert stored == datetime(2027, 1, 1, tzinfo=UTC)
            assert type(stamped.at) is datetime
            with pytest.raises(ValueError, match=r"^merge\(at=…\): .*timezone info"):
                await stamped.merge(at=datetime(2027, 1, 1))  # naive: refused before anything is sent
            assert (await _raw(rid))["at"] == datetime(2027, 1, 1, tzinfo=UTC)
            await _assert_client_usable(client)

    async def test_atomic_increment_never_writes_a_duration(self) -> None:
        async with orm_client("rf_stamped") as client:
            await client.query("CREATE rf_stamped:d SET at = d'2026-01-02T03:04:05Z', n = 1;", {})
            await client.query("CREATE rf_stamped:m SET n = 1;", {})  # no at
            stamped = await RfStamped.objects().get("d")
            missing = await RfStamped.objects().get("m")

            await stamped.atomic_increment("at", timedelta(hours=1))
            assert (await _raw("d", "rf_stamped"))["at"] == datetime(2026, 1, 2, 4, 4, 5, tzinfo=UTC)
            assert type(stamped.at) is datetime

            with pytest.raises(surrealdb.ServerError, match="addition"):
                await missing.atomic_increment("at", timedelta(hours=1))
            assert "at" not in await _raw("m", "rf_stamped")  # nothing written, never a duration
            await _assert_client_usable(client)


# ==================== Keyword writes of a sub-model holding a rich type (spec §4.1) ====================


class RfProfile(BaseModel):
    """A plain Pydantic sub-model with a rich field: stored as an object, its ``date`` midnight UTC."""

    bday: date | None = None
    city: str = ""


class RfSeen(BaseModel):
    """A sub-model whose rich sub-field is aliased: stored under ``lastSeen``, the key its validation reads."""

    seen: datetime | None = Field(default=None, alias="lastSeen")
    note: str = ""


class RfChoice(BaseModel):
    """A rich sub-field read under either string of an ``AliasChoices``: stored under the first, ``lastAt``."""

    last_at: datetime | None = Field(default=None, validation_alias=AliasChoices("lastAt", "last_at"))
    note: str = ""


class RfStrict(BaseModel):
    """Every sub-field required: a partial dict is a MERGE, never a whole sub-model."""

    bday: date
    city: str


class RfInner(BaseModel):
    when: date | None = None
    tag: str = ""


class RfOuter(BaseModel):
    """Sub-models two levels deep, and an array of them (replaced whole by a MERGE)."""

    inner: RfInner | None = None
    items: list[RfInner] = []
    label: str = ""


class RfPlain(BaseSurrealModel):
    """A table model without a rich type: the item type of ``RfPerson.plains``, a field that is not rich."""

    model_config = SurrealConfigDict(table_name="rf_plain")

    id: str | None = None
    name: str = ""


class RfPerson(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_person")

    id: str | None = None
    profile: RfProfile | None = None
    history: list[RfProfile] = []
    n: int = 0
    activity: RfSeen | None = None
    strict: RfStrict | None = None
    outer: RfOuter | None = None
    author: RecordID | None = None
    choice: RfChoice | None = None
    plains: list[RfPlain] = []
    plain_map: dict[str, RfPlain] = {}
    links: list[RecordID] = []


class RfOwner(BaseSurrealModel):
    """A table model also used as a sub-model type (``RfHolder.owner``)."""

    model_config = SurrealConfigDict(table_name="rf_owner")

    id: str | None = None
    since: date | None = None
    name: str = ""


class RfHolder(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_holder")

    id: str | None = None
    owner: RfOwner | None = None
    owners: list[RfOwner] = []


_BDAY = date(1990, 1, 2)
_PROFILE: dict[str, Any] = {"bday": "1990-01-02", "city": "x"}
_PROFILE_FORMS = pytest.mark.parametrize("profile", [_PROFILE, RfProfile(bday=_BDAY, city="x")], ids=["dict", "instance"])


def _assert_stored_profile(stored: Any, city: str = "x") -> None:
    """An ``RfProfile`` as the SDK reads it back: an object whose ``bday`` is a datetime, not text."""
    assert isinstance(stored, dict), f"not an object: {stored!r}"
    assert isinstance(stored["bday"], datetime), f"bday not stored as a datetime: {stored['bday']!r}"
    assert stored["bday"] == datetime(1990, 1, 2, tzinfo=UTC)
    assert stored["city"] == city


def _assert_profile(profile: Any, city: str = "x") -> None:
    """An ``RfProfile`` on an instance: the sub-model, its ``bday`` a ``date``."""
    assert type(profile) is RfProfile, f"not an RfProfile: {profile!r}"
    assert type(profile.bday) is date
    assert profile.bday == _BDAY
    assert profile.city == city


class TestNestedModelKeywordWritesE2E:
    """A keyword write on a field typed with a sub-model that holds a rich type, on both lines. In
    ``merge()`` a dict is converted key by key (each value validated as its sub-field) and an instance
    is the whole sub-object; ``bulk_update()`` validates a dict as the whole sub-model. Either way the
    object is sent with its values encoded — ``encode_wire`` dumps a Pydantic model, which the SDK
    cannot encode.

    The instance then holds what the write applied: the value sent, converted, when it is applied
    locally (``refresh=False``: the dict, or the sub-model instance), the returned row's object when
    the row is synced (``_apply_record`` does not enter nested models, v0.18.0); a read hydrates the
    sub-model."""

    @_PROFILE_FORMS
    @pytest.mark.parametrize("refresh", [True, False], ids=["refresh", "no-refresh"])
    async def test_merge(self, profile: Any, refresh: bool) -> None:
        async with orm_client("rf_person") as client:
            person = RfPerson(id="m")
            await person.save()
            await person.merge(refresh=refresh, profile=profile)

            _assert_stored_profile((await _raw("m", "rf_person"))["profile"])
            if refresh:
                _assert_stored_profile(person.profile)  # the row's object
            elif isinstance(profile, dict):
                assert person.profile == {"bday": _BDAY, "city": "x"}  # the dict sent, converted
                assert type(person.profile["bday"]) is date
            else:
                assert person.profile is profile  # the sub-model instance sent
            _assert_profile((await RfPerson.objects().get("m")).profile)
            await _assert_client_usable(client)

    async def test_merge_a_list_of_sub_models(self) -> None:
        async with orm_client("rf_person") as client:
            person = RfPerson(id="l")
            await person.save()
            await person.merge(history=[_PROFILE, RfProfile(bday=_BDAY, city="y")])

            stored = (await _raw("l", "rf_person"))["history"]
            assert len(stored) == 2
            _assert_stored_profile(stored[0])
            _assert_stored_profile(stored[1], city="y")
            loaded = (await RfPerson.objects().get("l")).history
            assert len(loaded) == 2
            _assert_profile(loaded[0])
            _assert_profile(loaded[1], city="y")
            await _assert_client_usable(client)

    async def test_bulk_update(self) -> None:
        async with orm_client("rf_person") as client:
            await RfPerson(id="a", n=1).save()
            await RfPerson(id="b", n=2).save()

            updated = await RfPerson.objects().filter(n=1).bulk_update(profile=_PROFILE, history=[_PROFILE])
            assert updated == 1

            row = await _raw("a", "rf_person")
            _assert_stored_profile(row["profile"])
            assert len(row["history"]) == 1
            _assert_stored_profile(row["history"][0])
            assert (await _raw("b", "rf_person")).get("profile") is None
            _assert_profile((await RfPerson.objects().get("a")).profile)
            await _assert_client_usable(client)

    async def test_update_or_create_both_branches(self) -> None:
        async with orm_client("rf_person") as client:
            created_obj, created = await RfPerson.objects().update_or_create(id="u", defaults={"profile": _PROFILE})
            assert created
            _assert_profile(created_obj.profile)
            _assert_stored_profile((await _raw("u", "rf_person"))["profile"])

            updated_obj, created_again = await RfPerson.objects().update_or_create(
                id="u", defaults={"profile": {**_PROFILE, "city": "z"}}
            )
            assert not created_again  # the merge branch: the instance holds the returned row's object
            _assert_stored_profile(updated_obj.profile, city="z")
            _assert_stored_profile((await _raw("u", "rf_person"))["profile"], city="z")
            _assert_profile((await RfPerson.objects().get("u")).profile, city="z")
            await _assert_client_usable(client)


class TestSubModelKeywordValues:
    """``_coerce_field_value`` on a field typed with a sub-model, no database.

    ``merge`` (MERGE: SurrealDB merges objects and replaces arrays) converts a dict key by key — only
    the caller's keys, under their own names, each resolved to a sub-field by name or alias and
    validated as that sub-field; a dict under a sub-model sub-field recursively; an array whole; no
    default added, no required sub-field asked for; an unknown key as given. ``bulk_update`` and
    ``signup`` replace the field, so a dict is validated as the whole sub-model there. A table-model
    instance is its record id where the field accepts a ``RecordID``, a ``ValueError`` elsewhere."""

    def test_merge_dict_sends_only_the_given_keys(self) -> None:
        coerce = RfPerson._coerce_field_value
        assert coerce("profile", {"city": "y"}) == {"city": "y"}
        # The defect this fixes: the default ``bday=None`` was sent too, and NONE removed the stored date.
        assert RfPerson._wire_columns({"profile": coerce("profile", {"city": "y"})}) == {"profile": {"city": "y"}}
        converted = coerce("profile", {"bday": "1995-05-05"})
        assert converted == {"bday": date(1995, 5, 5)}
        assert type(converted["bday"]) is date
        assert RfPerson._wire_columns({"profile": converted}) == {"profile": {"bday": datetime(1995, 5, 5, tzinfo=UTC)}}
        assert coerce("strict", {"bday": "1991-01-01"}) == {"bday": date(1991, 1, 1)}  # city is not asked for
        assert coerce("profile", {"bday": None}) == {"bday": None}  # None as given: NONE removes the key
        assert coerce("profile", {"zip": "H0H", "city": "y"}) == {"zip": "H0H", "city": "y"}  # unknown: as given
        given = {"bday": "1995-05-05"}
        coerce("profile", given)
        assert given == {"bday": "1995-05-05"}  # the caller's dict is never mutated

    def test_merge_dict_keys_resolve_by_name_or_alias_and_take_the_read_key(self) -> None:
        """Each key the dict resolves is sent under the key the sub-field's validation reads, the key
        ``save()`` writes (``model_object``): sent as ``seen``, the value was stored where no read looks."""
        coerce = RfPerson._coerce_field_value
        assert coerce("activity", {"lastSeen": "2026-01-01T00:00:00Z"}) == {"lastSeen": _JAN}
        assert coerce("activity", {"seen": "2026-01-01T00:00:00Z"}) == {"lastSeen": _JAN}
        assert coerce("choice", {"lastAt": "2026-01-01T00:00:00Z"}) == {"lastAt": _JAN}
        assert coerce("choice", {"last_at": "2026-01-01T00:00:00Z"}) == {"lastAt": _JAN}  # AliasChoices: the first
        assert coerce("activity", {"seen": None, "zip": 1}) == {"lastSeen": None, "zip": 1}  # unknown: as given

    @pytest.mark.parametrize(
        ("field", "value", "named"),
        [
            ("activity", {"seen": _JAN, "lastSeen": _JAN}, r"'seen' and 'lastSeen' both name the sub-field seen"),
            ("choice", {"lastAt": _JAN, "last_at": _JAN}, r"'lastAt' and 'last_at' both name the sub-field last_at"),
        ],
        ids=["alias", "alias-choices"],
    )
    def test_merge_dict_naming_a_sub_field_twice_raises(self, field: str, value: dict[str, Any], named: str) -> None:
        with pytest.raises(ValueError, match=rf"^merge\({field}=…\): {named}"):
            RfPerson._coerce_field_value(field, value)

    def test_merge_dict_recurses_into_sub_models_and_takes_arrays_whole(self) -> None:
        coerce = RfPerson._coerce_field_value
        assert coerce("outer", {"inner": {"when": "2026-01-02"}}) == {"inner": {"when": date(2026, 1, 2)}}
        items = coerce("outer", {"items": [{"when": "2026-01-02"}]})
        assert items == {"items": [RfInner(when=date(2026, 1, 2))]}  # an array is replaced: validated whole
        sent = RfPerson._wire_columns({"outer": items})
        assert sent == {"outer": {"items": [{"when": datetime(2026, 1, 2, tzinfo=UTC), "tag": ""}]}}

    def test_merge_instance_is_the_whole_sub_object(self) -> None:
        profile = RfProfile(city="y")
        assert RfPerson._coerce_field_value("profile", profile) is profile
        assert RfPerson._wire_columns({"profile": profile}) == {"profile": {"bday": None, "city": "y"}}

    @pytest.mark.parametrize(
        ("field", "value", "named"),
        [
            ("profile", {"bday": "someday"}, r"profile\.bday=…\): 'someday' is not a valid date"),
            ("activity", {"lastSeen": "soon"}, r"activity\.lastSeen=…\): 'soon' is not a valid datetime"),
            ("outer", {"inner": {"when": "x"}}, r"outer\.inner\.when=…\): 'x' is not a valid date"),
            ("outer", {"items": [{"when": "x"}]}, r"outer\.items=…\): \[\{'when': 'x'\}\] is not a valid list\[RfInner\]"),
            ("profile", {"city": 5}, r"profile\.city=…\): 5 is not a valid str"),
        ],
        ids=["bday", "alias", "nested", "array", "plain-sub-field"],
    )
    def test_merge_dict_invalid_value_names_the_sub_field(self, field: str, value: Any, named: str) -> None:
        with pytest.raises(ValueError, match=rf"^merge\({named}"):
            RfPerson._coerce_field_value(field, value)

    @pytest.mark.parametrize("context", ["bulk_update", "signup"])
    def test_replacing_writes_validate_the_whole_sub_model(self, context: str) -> None:
        coerce = RfPerson._coerce_field_value
        with pytest.raises(ValueError, match=rf"^{context}\(strict=…\): .*Field required"):
            coerce("strict", {"bday": "1991-01-01"}, context=context)
        assert coerce("profile", {"city": "y"}, context=context) == RfProfile(city="y")
        assert RfPerson._wire_columns({"activity": coerce("activity", {"lastSeen": _JAN}, context=context)}) == {
            "activity": {"lastSeen": _JAN, "note": ""}
        }

    @pytest.mark.parametrize("context", ["merge", "bulk_update", "signup"])
    def test_table_instance_is_its_record_id_where_the_field_takes_one(self, context: str) -> None:
        coerce = RfPerson._coerce_field_value
        alice = RfPerson(id="alice")
        rid = RecordID("rf_person", "alice")
        assert coerce("author", alice, context=context) == rid
        assert coerce("undeclared", alice, context=context) is alice  # encode_wire writes its record id
        assert encode_wire(coerce("undeclared", alice, context=context), stored=True) == rid
        # A field that cannot hold a RecordID — a sub-model, a field without a rich type — refuses it.
        for field in ("profile", "n"):
            with pytest.raises(
                ValueError, match=rf"^{context}\({field}=…\): the RfPerson instance is written as its record id"
            ):
                coerce(field, alice, context=context)
        with pytest.raises(ValueError, match=rf"^{context}\(author=…\): .*unsaved RfPerson has no record id"):
            coerce("author", RfPerson(), context=context)

    @pytest.mark.parametrize("context", ["merge", "bulk_update", "signup", "signin"])
    def test_table_instance_inside_a_field_that_is_not_rich(self, context: str) -> None:
        """At any position ``_link_instances`` walks — a list or tuple item, a mapping value — whatever
        the field: ``plains: list[RfPlain]`` holds no rich type, and its record id still does not fit."""
        coerce = RfPerson._coerce_field_value
        plain = RfPlain(id="o1", name="x")
        for field, value, label in (
            ("plains", [plain], r"list\[RfPlain\]"),
            ("plains", (plain,), r"list\[RfPlain\]"),
            ("plain_map", {"a": plain}, r"dict\[str, RfPlain\]"),
        ):
            with pytest.raises(
                ValueError,
                match=rf"^{context}\({field}=…\): .* is not a valid {label} .*; a table-model instance in it is written as its record id",
            ):
                coerce(field, value, context=context)
        alice = RfPerson(id="alice")
        assert coerce("links", [alice], context=context) == [RecordID("rf_person", "alice")]
        given = [{"name": "x"}]
        assert coerce("plains", given, context=context) is given  # no instance, no rich type: as given
        with pytest.raises(ValueError, match=rf"^{context}\(links=…\): .*unsaved RfPerson has no record id"):
            coerce("links", [RfPerson()], context=context)

    def test_table_instance_in_a_merge_dict_follows_the_same_rule(self) -> None:
        alice = RfPerson(id="alice")
        with pytest.raises(ValueError, match=r"^merge\(profile\.city=…\): the RfPerson instance is written as its record id"):
            RfPerson._coerce_field_value("profile", {"city": alice})

    def test_table_model_built_from_a_dict_stays_an_object(self) -> None:
        """The sub-model the ORM validated from the caller's dict is the object to write, even when
        its class is a table model; only the caller's own instance is a record id."""
        for context in ("bulk_update", "signup"):
            built = RfHolder._coerce_field_value("owner", {"since": "2026-01-02", "name": "o"}, context=context)
            sent = encode_wire(built, stored=True)
            assert isinstance(sent, dict), sent
            assert sent["since"] == datetime(2026, 1, 2, tzinfo=UTC)
            assert sent["name"] == "o"
        assert RfHolder._coerce_field_value("owner", {"since": "2026-01-02"}) == {"since": date(2026, 1, 2)}
        owners = RfHolder._coerce_field_value("owners", [{"since": "2026-01-02"}])
        sent_items = encode_wire(owners, stored=True)
        assert isinstance(sent_items[0], dict)
        assert sent_items[0]["since"] == datetime(2026, 1, 2, tzinfo=UTC)
        # The caller's own instance inside the array: its record id, which list[RfOwner] cannot hold.
        with pytest.raises(ValueError, match=r"^merge\(owners=…\): .*record id"):
            RfHolder._coerce_field_value("owners", [{"since": "2026-01-02"}, RfOwner(id="o1")])

    def test_write_payload_nests_aliased_sub_fields_under_their_alias(self) -> None:
        """``save()``, ``update()``, ``upsert()``, ``bulk_create()`` and the create branch of
        ``*_or_create`` write the same sub-object as the keyword writes (top-level columns unchanged)."""
        person = RfPerson(id="s", activity=RfSeen(lastSeen=_JAN, note="n"), outer=RfOuter(inner=RfInner(when=date(2026, 1, 2))))
        payload = person._write_payload()
        assert payload["activity"] == {"lastSeen": _JAN, "note": "n"}
        assert payload["outer"]["inner"] == {"when": datetime(2026, 1, 2, tzinfo=UTC), "tag": ""}


_SEED_PERSON = (
    "CREATE rf_person:p SET profile = {bday: d'1990-01-02T00:00:00Z', city: 'x'}, "
    "strict = {bday: d'1990-01-02T00:00:00Z', city: 'x'}, n = 1;"
)
_PARTIAL_PROFILES = pytest.mark.parametrize(
    ("change", "stored", "local", "loaded"),
    [
        (
            {"city": "y"},
            {"bday": datetime(1990, 1, 2, tzinfo=UTC), "city": "y"},
            {"city": "y"},
            RfProfile(bday=_BDAY, city="y"),
        ),
        (
            {"bday": "1995-05-05"},
            {"bday": datetime(1995, 5, 5, tzinfo=UTC), "city": "x"},
            {"bday": date(1995, 5, 5)},
            RfProfile(bday=date(1995, 5, 5), city="x"),
        ),
    ],
    ids=["city", "bday"],
)
_REFRESH = pytest.mark.parametrize("refresh", [True, False], ids=["refresh", "no-refresh"])


async def _seeded_person(client: Any) -> RfPerson:
    """``rf_person:p`` with ``profile`` and ``strict`` set to ``{bday: 1990-01-02, city: 'x'}``, loaded."""
    await client.query(_SEED_PERSON, {})
    person: RfPerson = await RfPerson.objects().get("p")
    return person


def _assert_loaded(profile: Any, expected: BaseModel) -> None:
    """A sub-model read back: the declared class, its ``date`` a ``date`` (never the stored datetime)."""
    assert type(profile) is type(expected), f"not a {type(expected).__name__}: {profile!r}"
    assert profile == expected
    assert type(profile.bday) is date


class TestSubModelMergeE2E:
    """``merge()`` and the update branch of ``update_or_create()`` send a sub-model dict key by key, so
    SurrealDB's MERGE keeps the other stored keys (before, the defaults the validation added were
    sent too, and ``None`` removed the stored date); ``bulk_update()`` replaces the field, so a dict
    missing a required sub-field is refused before anything is sent. Both lines."""

    @_PARTIAL_PROFILES
    @_REFRESH
    async def test_merge_a_partial_dict_keeps_the_other_keys(
        self, change: dict[str, Any], stored: dict[str, Any], local: dict[str, Any], loaded: RfProfile, refresh: bool
    ) -> None:
        async with orm_client("rf_person") as client:
            person = await _seeded_person(client)
            await person.merge(refresh=refresh, profile=change)

            assert (await _raw("p", "rf_person"))["profile"] == stored
            assert person.profile == (stored if refresh else local)  # the row's object, or the dict sent
            _assert_loaded((await RfPerson.objects().get("p")).profile, loaded)
            await _assert_client_usable(client)

    @_PARTIAL_PROFILES
    async def test_merge_in_a_transaction(
        self, change: dict[str, Any], stored: dict[str, Any], local: dict[str, Any], loaded: RfProfile
    ) -> None:
        async with orm_client("rf_person") as client:
            person = await _seeded_person(client)
            async with SurrealDBConnectionManager.transaction() as tx:
                await person.merge(tx=tx, profile=change)

            assert (await _raw("p", "rf_person"))["profile"] == stored
            _assert_loaded((await RfPerson.objects().get("p")).profile, loaded)
            await _assert_client_usable(client)

    @_PARTIAL_PROFILES
    async def test_update_or_create_update_branch(
        self, change: dict[str, Any], stored: dict[str, Any], local: dict[str, Any], loaded: RfProfile
    ) -> None:
        async with orm_client("rf_person") as client:
            await _seeded_person(client)
            obj, created = await RfPerson.objects().update_or_create(id="p", defaults={"profile": change})

            assert not created
            assert (await _raw("p", "rf_person"))["profile"] == stored
            assert obj.profile == stored  # the returned row's object
            _assert_loaded((await RfPerson.objects().get("p")).profile, loaded)
            await _assert_client_usable(client)

    @_REFRESH
    async def test_merge_a_dict_missing_a_required_sub_field(self, refresh: bool) -> None:
        """A partial MERGE, as in v0.22.x: the stored city stays, and the row reads back."""
        async with orm_client("rf_person") as client:
            person = await _seeded_person(client)
            await person.merge(refresh=refresh, strict={"bday": "1991-01-01"})

            assert (await _raw("p", "rf_person"))["strict"] == {"bday": datetime(1991, 1, 1, tzinfo=UTC), "city": "x"}
            if not refresh:
                assert person.strict == {"bday": date(1991, 1, 1)}
            assert (await RfPerson.objects().get("p")).strict == RfStrict(bday=date(1991, 1, 1), city="x")
            await _assert_client_usable(client)

    async def test_bulk_update_refuses_a_dict_missing_a_required_sub_field(self) -> None:
        """``bulk_update()`` compiles a SET, which replaces the object: it would not read back."""
        async with orm_client("rf_person") as client:
            await _seeded_person(client)
            with pytest.raises(ValueError, match=r"^bulk_update\(strict=…\): .*Field required"):
                await RfPerson.objects().filter(n=1).bulk_update(strict={"bday": "1991-01-01"})

            assert (await _raw("p", "rf_person"))["strict"] == {"bday": datetime(1990, 1, 2, tzinfo=UTC), "city": "x"}
            await _assert_client_usable(client)


_BUMP = {"n": SurrealFunc("n + 1")}


class TestSubModelMergeServerValuesE2E:
    """``merge(server_values=…)`` compiles an ``UPDATE … SET``, which replaces each field it assigns: a
    sub-model dict is compiled into one assignment per key (``profile.city = $…``, nested for a dict
    under a sub-model sub-field), so the other stored keys stay, as with ``MERGE``. Before, the dict
    replaced the stored object: ``bday`` was lost, and a dict missing a required sub-field left a row
    that no longer read back (``strict.city Field required``). Both lines."""

    @_PARTIAL_PROFILES
    @_REFRESH
    async def test_a_partial_dict_keeps_the_other_keys(
        self, change: dict[str, Any], stored: dict[str, Any], local: dict[str, Any], loaded: RfProfile, refresh: bool
    ) -> None:
        async with orm_client("rf_person") as client:
            person = await _seeded_person(client)
            await person.merge(refresh=refresh, server_values=_BUMP, profile=change)

            row = await _raw("p", "rf_person")
            assert row["profile"] == stored
            assert row["n"] == 2
            assert person.profile == (stored if refresh else local)  # the row's object, or the dict sent
            _assert_loaded((await RfPerson.objects().get("p")).profile, loaded)
            await _assert_client_usable(client)

    @_PARTIAL_PROFILES
    async def test_in_a_transaction(
        self, change: dict[str, Any], stored: dict[str, Any], local: dict[str, Any], loaded: RfProfile
    ) -> None:
        async with orm_client("rf_person") as client:
            person = await _seeded_person(client)
            async with SurrealDBConnectionManager.transaction() as tx:
                await person.merge(tx=tx, server_values=_BUMP, profile=change)

            assert (await _raw("p", "rf_person"))["profile"] == stored
            _assert_loaded((await RfPerson.objects().get("p")).profile, loaded)
            await _assert_client_usable(client)

    @_REFRESH
    async def test_a_dict_missing_a_required_sub_field(self, refresh: bool) -> None:
        async with orm_client("rf_person") as client:
            person = await _seeded_person(client)
            await person.merge(refresh=refresh, server_values=_BUMP, strict={"bday": "1991-01-01"})

            row = await _raw("p", "rf_person")
            assert row["strict"] == {"bday": datetime(1991, 1, 1, tzinfo=UTC), "city": "x"}
            assert row["n"] == 2
            assert (await RfPerson.objects().get("p")).strict == RfStrict(bday=date(1991, 1, 1), city="x")
            await _assert_client_usable(client)

    async def test_nested_dicts_and_an_absent_object(self) -> None:
        """A dict under a sub-model sub-field is assigned key by key too; an object not stored yet is
        created by the first assignment into it (both lines)."""
        async with orm_client("rf_person") as client:
            await client.query(
                "CREATE rf_person:p SET outer = {inner: {when: d'2020-01-02T00:00:00Z', tag: 't'}, label: 'l'}, n = 1;",
                {},
            )
            person = await RfPerson.objects().get("p")
            await person.merge(server_values=_BUMP, outer={"inner": {"when": "2026-01-02"}}, profile={"city": "y"})

            row = await _raw("p", "rf_person")
            assert row["outer"] == {"inner": {"when": datetime(2026, 1, 2, tzinfo=UTC), "tag": "t"}, "label": "l"}
            assert row["profile"] == {"city": "y"}
            loaded = await RfPerson.objects().get("p")
            assert loaded.outer == RfOuter(inner=RfInner(when=date(2026, 1, 2), tag="t"), label="l")
            assert loaded.profile == RfProfile(city="y")
            await _assert_client_usable(client)

    async def test_a_server_value_for_the_field_wins_over_the_dict(self) -> None:
        async with orm_client("rf_person") as client:
            person = await _seeded_person(client)
            await person.merge(server_values={"profile": SurrealFunc("{city: 'z'}")}, profile={"bday": "1995-05-05"})

            assert (await _raw("p", "rf_person"))["profile"] == {"city": "z"}
            await _assert_client_usable(client)

    async def test_a_key_that_is_not_an_identifier_is_refused_before_sending(self) -> None:
        """The path of a ``SET`` assignment is SurrealQL text: each key must be an identifier."""
        async with orm_client("rf_person") as client:
            person = await _seeded_person(client)
            with pytest.raises(ValueError, match=r"^merge\(profile=…\): the key 'zip-code' is not an identifier"):
                await person.merge(server_values=_BUMP, profile={"zip-code": "H0H"})

            row = await _raw("p", "rf_person")
            assert row["n"] == 1
            assert row["profile"] == {"bday": datetime(1990, 1, 2, tzinfo=UTC), "city": "x"}
            await _assert_client_usable(client)


_SEEN = {"lastSeen": "2026-01-01T00:00:00Z", "note": "n"}
_SEEN_FORMS = pytest.mark.parametrize("activity", [_SEEN, RfSeen(lastSeen=_JAN, note="n")], ids=["dict", "instance"])


def _assert_stored_seen(stored: Any) -> None:
    """An ``RfSeen`` as stored: under ``lastSeen`` (the key its validation reads), a datetime."""
    assert isinstance(stored, dict), f"not an object: {stored!r}"
    assert "seen" not in stored, f"stored under the Python name: {stored!r}"
    assert isinstance(stored["lastSeen"], datetime), f"lastSeen not a datetime: {stored!r}"
    assert stored["lastSeen"] == _JAN
    assert stored["note"] == "n"


def _assert_seen(activity: Any) -> None:
    assert type(activity) is RfSeen, f"not an RfSeen: {activity!r}"
    assert activity.seen == _JAN
    assert activity.note == "n"


class TestAliasedSubFieldsE2E:
    """A sub-field declared ``Field(alias="lastSeen")`` is written under ``lastSeen`` by every write —
    the key its validation reads — so a read gives it back (it used to be written under ``seen``,
    which nothing reads: ``get()`` returned ``None``). Both lines."""

    @_SEEN_FORMS
    async def test_merge(self, activity: Any) -> None:
        async with orm_client("rf_person") as client:
            person = RfPerson(id="a")
            await person.save()
            await person.merge(activity=activity)

            _assert_stored_seen((await _raw("a", "rf_person"))["activity"])
            _assert_seen((await RfPerson.objects().get("a")).activity)
            await _assert_client_usable(client)

    @_SEEN_FORMS
    async def test_bulk_update(self, activity: Any) -> None:
        async with orm_client("rf_person") as client:
            await RfPerson(id="b", n=1).save()
            assert await RfPerson.objects().filter(n=1).bulk_update(activity=activity) == 1

            _assert_stored_seen((await _raw("b", "rf_person"))["activity"])
            _assert_seen((await RfPerson.objects().get("b")).activity)
            await _assert_client_usable(client)

    async def test_save_update_and_upsert(self) -> None:
        async with orm_client("rf_person") as client:
            person = RfPerson(id="s", activity=RfSeen(lastSeen=_JAN, note="n"))
            await person.save()
            _assert_stored_seen((await _raw("s", "rf_person"))["activity"])
            loaded = await RfPerson.objects().get("s")
            _assert_seen(loaded.activity)

            loaded.n = 2
            await loaded.update()  # a full replace from the loaded instance
            _assert_stored_seen((await _raw("s", "rf_person"))["activity"])
            await RfPerson(id="u", activity=RfSeen(lastSeen=_JAN, note="n")).upsert()
            _assert_stored_seen((await _raw("u", "rf_person"))["activity"])
            _assert_seen((await RfPerson.objects().get("u")).activity)
            await _assert_client_usable(client)

    @pytest.mark.parametrize("write", ["merge", "merge-server-values", "update_or_create"])
    async def test_a_dict_naming_the_python_name_updates_the_stored_alias(self, write: str) -> None:
        """``{"seen": …}`` (the Python name) and ``{"last_at": …}`` (the second string of an
        ``AliasChoices``) are written under ``lastSeen`` and ``lastAt``, the keys a read takes them from.
        Sent under their own spelling, the stored value was not touched and ``get()`` gave it back."""
        async with orm_client("rf_person") as client:
            old = datetime(2020, 1, 1, tzinfo=UTC)
            await RfPerson(id="a", activity=RfSeen(lastSeen=old, note="n"), choice=RfChoice(lastAt=old, note="c")).save()
            change: dict[str, Any] = {
                "activity": {"seen": "2026-01-01T00:00:00Z"},
                "choice": {"last_at": "2026-01-01T00:00:00Z"},
            }
            if write == "update_or_create":
                await RfPerson.objects().update_or_create(id="a", defaults=change)
            else:
                person = await RfPerson.objects().get("a")
                await person.merge(server_values=_BUMP if write == "merge-server-values" else None, **change)

            row = await _raw("a", "rf_person")
            assert row["activity"] == {"lastSeen": _JAN, "note": "n"}
            assert row["choice"] == {"lastAt": _JAN, "note": "c"}
            loaded = await RfPerson.objects().get("a")
            assert loaded.activity == RfSeen(lastSeen=_JAN, note="n")
            assert loaded.choice == RfChoice(lastAt=_JAN, note="c")
            await _assert_client_usable(client)

    @_SEEN_FORMS
    async def test_signup_then_signin(self, activity: Any) -> None:
        async with _auth_access(RfProfiled) as client:
            email = f"{UUID(int=4).hex}@example.test"
            result = await RfProfiled.signup(email=email, password=_AUTH_PASSWORD, activity=activity)
            _assert_seen(result.user.activity)

            row = await client.query("SELECT * FROM ONLY rf_profiled WHERE email = $email LIMIT 1;", {"email": email})
            _assert_stored_seen(row["activity"])
            again = await RfProfiled.signin(email=email, password=_AUTH_PASSWORD)
            _assert_seen(again.user.activity)
            await _assert_client_usable(client)


class RfStreet(BaseModel):
    """camelCase aliases only: a read takes ``streetName``, never ``street_name``."""

    model_config = ConfigDict(alias_generator=to_camel)

    street_name: str = ""


class RfCamel(BaseModel):
    """camelCase aliases, validated by name too: stored under the names, the format of v0.22 and before."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    first_name: str = ""
    home_address: RfStreet | None = None


class RfCamelByName(BaseModel):
    """The same, with Pydantic 2.11's spelling of the setting."""

    model_config = ConfigDict(alias_generator=to_camel, validate_by_name=True)

    first_name: str = ""


class RfCamelOnly(BaseModel):
    """camelCase aliases only: the names are never read back, so a write stores the aliases."""

    model_config = ConfigDict(alias_generator=to_camel)

    first_name: str = ""


class RfCamelDated(BaseModel):
    """``RfCamel`` with a rich sub-field: a ``merge()`` dict is converted key by key."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    first_name: str = ""
    seen_at: datetime | None = None


class RfNicknamed(BaseModel):
    """Validated by name and by alias: an ``AliasChoices`` (both read before the name) and an alias
    that is not an identifier (no ``SET`` path can name it)."""

    model_config = ConfigDict(populate_by_name=True)

    nick: str = Field(default="", validation_alias=AliasChoices("nickName", "handle"))
    home_town: str = Field(default="", alias="home-town")


class RfNameOnly(BaseModel):
    """Validated by name only (``validate_by_alias=False``): a read never takes the aliases."""

    model_config = ConfigDict(alias_generator=to_camel, validate_by_name=True, validate_by_alias=False)

    first_name: str = ""


class RfNamed(BaseSurrealModel):
    """Aliased sub-models, rich or not: their stored keys follow how they validate."""

    model_config = SurrealConfigDict(table_name="rf_named")

    id: str | None = None
    profile: RfCamel | None = None
    by_name: RfCamelByName | None = None
    alias_only: RfCamelOnly | None = None
    dated: RfCamelDated | None = None
    nicknamed: RfNicknamed | None = None
    name_only: RfNameOnly | None = None
    n: int = 0


class RfNamedStrict(BaseSurrealModel):
    """``RfNamed``'s ``profile`` on a SCHEMAFULL table (``TestSubModelStoredKeysE2E``)."""

    model_config = SurrealConfigDict(table_name="rf_named_strict")

    id: str | None = None
    profile: RfCamel | None = None
    n: int = 0


class TestSubModelStoredKeys:
    """The keys a write stores a sub-model's fields under, no database. A sub-model that validates by
    name too (``populate_by_name``/``validate_by_name``) keeps each sub-field under its name, the
    format v0.22 wrote and the format a ``DEFINE FIELD profile.first_name`` schema declares; one that
    validates by alias only stores each aliased sub-field under its alias, the only key a read takes.
    A keyword write's dict for a sub-model is re-keyed the same way, rich or not."""

    def test_read_keys(self) -> None:
        assert read_keys(RfCamel) == {"first_name": "first_name", "home_address": "home_address"}
        assert read_keys(RfCamelByName) == {"first_name": "first_name"}
        assert read_keys(RfCamelOnly) == {"first_name": "firstName"}
        assert read_keys(RfStreet) == {"street_name": "streetName"}
        assert read_keys(RfSeen) == {"seen": "lastSeen", "note": "note"}
        # A table model used as a sub-model validates by name (BaseSurrealModel's populate_by_name).
        assert read_keys(RfAliasedOwner) == {"id": "id", "since": "since", "label": "label"}

    def test_masking_keys(self) -> None:
        """The other keys a read takes before the stored one: only by name **and** by alias."""
        assert masking_keys(RfCamel) == {"first_name": ("firstName",), "home_address": ("homeAddress",)}
        assert masking_keys(RfCamelByName) == {"first_name": ("firstName",)}
        assert masking_keys(RfNicknamed) == {"nick": ("nickName", "handle"), "home_town": ("home-town",)}
        assert masking_keys(RfAliasedOwner) == {"id": (), "since": (), "label": ("Label",)}
        assert masking_keys(RfNameOnly) == {"first_name": ()}  # no read takes an alias
        assert masking_keys(RfCamelOnly) == {"first_name": ()}  # stored under the alias a read takes
        assert masking_keys(RfSeen) == {"seen": (), "note": ()}

    def test_model_writes(self) -> None:
        person = RfNamed(
            id="s",
            profile=RfCamel(first_name="a", home_address=RfStreet(streetName="x")),
            by_name=RfCamelByName(first_name="b"),
            alias_only=RfCamelOnly(firstName="c"),
        )
        payload = person._write_payload()
        assert payload["profile"] == {"first_name": "a", "home_address": {"streetName": "x"}}
        assert payload["by_name"] == {"first_name": "b"}
        assert payload["alias_only"] == {"firstName": "c"}
        assert encode_wire(RfCamel(first_name="a"), stored=True) == {"first_name": "a", "home_address": None}
        # Each object written reads back into the same sub-model.
        assert RfCamel.model_validate(payload["profile"]) == person.profile
        assert RfCamelOnly.model_validate(payload["alias_only"]) == person.alias_only

    @pytest.mark.parametrize("context", ["merge", "bulk_update", "signup", "signin"])
    def test_keyword_dicts_are_re_keyed(self, context: str) -> None:
        """Keys only: the values of a sub-model without a rich type are sent as given, as in v0.22.
        Only ``merge()`` also removes the other spellings of a sub-model that validates by name and
        by alias (``test_merge_removes_the_other_spellings``): the other writes replace the field."""

        def removed(*keys: str) -> dict[str, None]:
            return dict.fromkeys(keys) if context == "merge" else {}

        coerce = RfNamed._coerce_field_value
        assert coerce("profile", {"firstName": "a"}, context=context) == {"first_name": "a", **removed("firstName")}
        assert coerce("profile", {"first_name": "a"}, context=context) == {"first_name": "a", **removed("firstName")}
        assert coerce("alias_only", {"first_name": "a"}, context=context) == {"firstName": "a"}
        assert coerce("alias_only", {"firstName": "a"}, context=context) == {"firstName": "a"}
        assert coerce("by_name", {"firstName": "a"}, context=context) == {"first_name": "a", **removed("firstName")}
        nested = coerce("profile", {"homeAddress": {"street_name": "x"}}, context=context)
        # A dict under a sub-model sub-field.
        assert nested == {"home_address": {"streetName": "x"}, **removed("homeAddress")}
        assert coerce("profile", {"firstName": 5, "zip": "H0H"}, context=context) == {
            "first_name": 5,
            **removed("firstName"),
            "zip": "H0H",
        }
        given = {"firstName": "a"}
        coerce("profile", given, context=context)
        assert given == {"firstName": "a"}  # the caller's dict is never mutated

    def test_merge_removes_the_other_spellings(self) -> None:
        """In ``merge()``, a sub-model that validates by name **and** by alias sends ``None`` (NONE: the
        key is removed) for every other spelling of each sub-field its dict names. v0.22's ``merge()``
        kept the caller's key, so a row can hold ``{"first_name": …, "firstName": …}``, and a read takes
        the alias first: the name just merged would be hidden."""
        coerce = RfNamed._coerce_field_value
        # A rich sub-model, converted key by key.
        assert coerce("dated", {"seenAt": "2026-01-02T03:04:05Z", "firstName": "a"}) == {
            "seen_at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
            "seenAt": None,
            "first_name": "a",
            "firstName": None,
        }
        # Every string of an AliasChoices, and an alias that is not an identifier.
        assert coerce("nicknamed", {"handle": "a", "home_town": "b"}) == {
            "nick": "a",
            "nickName": None,
            "handle": None,
            "home_town": "b",
            "home-town": None,
        }
        # A read never takes the aliases of a sub-model that validates by name only; one that
        # validates by alias only stores each sub-field under the first key a read takes.
        assert coerce("name_only", {"firstName": "a"}) == {"first_name": "a"}
        assert coerce("alias_only", {"first_name": "a"}) == {"firstName": "a"}

    def test_server_values_assign_the_removals(self) -> None:
        """Under ``server_values`` each removal is its own ``SET`` assignment (``nicknamed.nickName =
        NONE``); a spelling that is not an identifier cannot be a ``SET`` path, so it is skipped, where
        a caller's key that is not one still raises."""
        value = RfNamed._coerce_field_value("nicknamed", {"handle": "a", "home-town": "b"})
        assert _set_paths("nicknamed", ("nicknamed",), RfNicknamed, value) == [
            (("nicknamed", "nick"), "a"),
            (("nicknamed", "nickName"), None),
            (("nicknamed", "handle"), None),
            (("nicknamed", "home_town"), "b"),
        ]
        nested = RfNamed._coerce_field_value("profile", {"homeAddress": {"street_name": "x"}})
        assert _set_paths("profile", ("profile",), RfCamel, nested) == [
            (("profile", "home_address", "streetName"), "x"),
            (("profile", "homeAddress"), None),
        ]
        with pytest.raises(ValueError, match=r"^merge\(nicknamed=…\): the key 'zip-code' is not an identifier"):
            _set_paths("nicknamed", ("nicknamed",), RfNicknamed, {"zip-code": "H0H"})

    @pytest.mark.parametrize(
        ("field", "value", "named"),
        [
            (
                "profile",
                {"first_name": "a", "firstName": "b"},
                r"'first_name' and 'firstName' both name the sub-field first_name",
            ),
            (
                "alias_only",
                {"firstName": "a", "first_name": "b"},
                r"'firstName' and 'first_name' both name the sub-field first_name",
            ),
            ("profile", {"homeAddress": {"streetName": "a", "street_name": "b"}}, r"'streetName' and 'street_name' both name"),
        ],
        ids=["by-name", "alias-only", "nested"],
    )
    def test_a_sub_field_named_twice_raises(self, field: str, value: dict[str, Any], named: str) -> None:
        with pytest.raises(ValueError, match=rf"^merge\({field}(\.homeAddress)?=…\): {named}"):
            RfNamed._coerce_field_value(field, value)


class RfAliasedOwner(BaseSurrealModel):
    """A table model with an aliased column, used as a sub-model by nothing but ``read_keys``."""

    model_config = SurrealConfigDict(table_name="rf_aliased_owner")

    id: str | None = None
    since: date | None = None
    label: str = Field(default="", alias="Label")


def _stored_keys(stored: Any) -> set[str]:
    assert isinstance(stored, dict), f"not an object: {stored!r}"
    return set(stored)


class TestSubModelStoredKeysE2E:
    """What a sub-model's keys look like in the row, on both lines (see ``TestSubModelStoredKeys``).

    A sub-model that validates by name too used to be stored under its aliases by ``save()`` while a
    ``merge()`` dict kept the caller's keys: ``{"firstName": "old", "first_name": "new"}``, and
    ``get()`` read the alias first, so the merged value was lost."""

    @pytest.mark.parametrize("server_values", [False, True], ids=["merge", "merge-server-values"])
    async def test_validated_by_name_keeps_the_names(self, server_values: bool) -> None:
        async with orm_client("rf_named") as client:
            person = RfNamed(id="a", profile=RfCamel(first_name="old"))
            await person.save()
            stored = (await _raw("a", "rf_named"))["profile"]
            assert stored["first_name"] == "old"
            assert "firstName" not in _stored_keys(stored)

            for given, expected in (({"first_name": "new"}, "new"), ({"firstName": "new2"}, "new2")):
                await person.merge(server_values=_BUMP if server_values else None, profile=given)
                stored = (await _raw("a", "rf_named"))["profile"]
                assert stored["first_name"] == expected
                assert "firstName" not in _stored_keys(stored), stored
                loaded = (await RfNamed.objects().get("a")).profile
                assert loaded is not None
                assert loaded.first_name == expected
            await _assert_client_usable(client)

    @pytest.mark.parametrize("server_values", [False, True], ids=["merge", "merge-server-values"])
    async def test_a_v022_row_with_both_spellings(self, server_values: bool) -> None:
        """v0.22's ``merge()`` kept the caller's key: ``save()`` then ``merge(profile={"firstName": …})``
        left both spellings in the row, and a read takes the alias first. Writing the name alone, the
        next ``merge()`` was hidden behind that alias and lost without an error; it now removes the
        other spellings of each sub-field it names, so the value merged is the value read."""
        async with orm_client("rf_named") as client:
            before, after = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
            await client.query(
                "CREATE $rid CONTENT $row;",
                {
                    "rid": RecordID("rf_named", "d"),
                    "row": {
                        "n": 0,
                        "profile": {
                            "first_name": "Ann",
                            "firstName": "Anna",
                            "home_address": {"streetName": "x"},
                            "homeAddress": {"streetName": "y"},
                        },
                        "dated": {"first_name": "Ann", "seen_at": before, "seenAt": before},
                        "nicknamed": {"nick": "n", "nickName": "nn", "handle": "h"},
                    },
                },
            )
            person = await RfNamed.objects().get("d")
            assert person.profile == RfCamel(first_name="Anna", home_address=RfStreet(streetName="y"))  # the aliases

            await person.merge(
                server_values=_BUMP if server_values else None,
                profile={"firstName": "Annie", "homeAddress": {"street_name": "z"}},
                dated={"seenAt": after},
                nicknamed={"handle": "h2"},
            )
            expected = {
                "profile": RfCamel(first_name="Annie", home_address=RfStreet(streetName="z")),
                "dated": RfCamelDated(first_name="Ann", seen_at=after),
                "nicknamed": RfNicknamed(nick="h2"),
            }
            loaded = await RfNamed.objects().get("d")
            for field, value in expected.items():
                assert getattr(loaded, field) == value
                # The instance is resynced from the row the write returned (a nested model as its dict).
                assert type(value).model_validate(getattr(person, field)) == value
            assert loaded.n == person.n == (1 if server_values else 0)
            row = await _raw("d", "rf_named")
            assert row["profile"] == {"first_name": "Annie", "home_address": {"streetName": "z"}}
            assert row["dated"] == {"first_name": "Ann", "seen_at": after}
            assert row["nicknamed"] == {"nick": "h2"}
            await _assert_client_usable(client)

    @pytest.mark.parametrize("server_values", [False, True], ids=["merge", "merge-server-values"])
    async def test_an_instance_on_a_v022_row_with_both_spellings(self, server_values: bool) -> None:
        """An instance is the whole sub-object. ``MERGE`` merges its object into the stored one, so it
        removes every alias a read takes before a name, at each level, as a dict does for its keys;
        ``UPDATE … SET`` (``server_values``) assigns it whole."""
        async with orm_client("rf_named") as client:
            await client.query(
                "CREATE $rid CONTENT $row;",
                {
                    "rid": RecordID("rf_named", "g"),
                    "row": {
                        "n": 0,
                        "profile": {"first_name": "Ann", "firstName": "Anna", "homeAddress": {"streetName": "y"}},
                    },
                },
            )
            person = await RfNamed.objects().get("g")
            profile = RfCamel(first_name="Annie", home_address=RfStreet(streetName="z"))
            await person.merge(server_values=_BUMP if server_values else None, profile=profile)

            assert (await _raw("g", "rf_named"))["profile"] == {"first_name": "Annie", "home_address": {"streetName": "z"}}
            assert (await RfNamed.objects().get("g")).profile == profile
            assert RfCamel.model_validate(person.profile) == profile
            await _assert_client_usable(client)

    @pytest.mark.parametrize("server_values", [False, True], ids=["merge", "merge-server-values"])
    @pytest.mark.parametrize("in_tx", [False, True], ids=["no-refresh", "transaction"])
    async def test_a_local_apply_holds_no_removal(self, server_values: bool, in_tx: bool) -> None:
        """Applied locally (``refresh=False``, a buffered transaction on 2.x), the instance takes the
        dict sent, as converted, without the ``None`` that removes an alias from the row: that is not a
        value. An interactive transaction (3.x) syncs the row's object, the same dict."""
        async with orm_client("rf_named") as client:
            person = RfNamed(id="f", profile=RfCamel(first_name="Ann"))
            await person.save()
            change: dict[str, Any] = {
                "server_values": _BUMP if server_values else None,
                "profile": {"firstName": "Annie", "homeAddress": {"street_name": "z"}},
                "nicknamed": {"handle": "h"},
            }
            if in_tx:
                async with SurrealDBConnectionManager.transaction() as tx:
                    await person.merge(tx=tx, **change)
            else:
                await person.merge(refresh=False, **change)

            profile = {"first_name": "Annie", "home_address": {"streetName": "z"}}
            assert person.profile == profile
            assert person.nicknamed == {"nick": "h"}
            assert RfCamel.model_validate(person.profile) == RfCamel(first_name="Annie", home_address=RfStreet(streetName="z"))
            row = await _raw("f", "rf_named")
            assert (row["profile"], row["nicknamed"]) == (profile, {"nick": "h"})
            await _assert_client_usable(client)

    @pytest.mark.parametrize("server_values", [False, True], ids=["merge", "merge-server-values"])
    async def test_a_spelling_that_is_not_an_identifier(self, server_values: bool) -> None:
        """``merge()`` removes it from the object it merges; an ``UPDATE … SET`` path cannot name it, so
        ``merge(server_values=)`` leaves it in place (and a read still takes it first) without failing."""
        async with orm_client("rf_named") as client:
            await client.query(
                "CREATE $rid CONTENT $row;",
                {"rid": RecordID("rf_named", "e"), "row": {"n": 0, "nicknamed": {"home_town": "a", "home-town": "b"}}},
            )
            person = await RfNamed.objects().get("e")
            await person.merge(server_values=_BUMP if server_values else None, nicknamed={"home-town": "c"})
            row = await _raw("e", "rf_named")
            if server_values:
                assert row["nicknamed"] == {"home_town": "c", "home-town": "b"}
                assert (await RfNamed.objects().get("e")).nicknamed == RfNicknamed(home_town="b")
            else:
                assert row["nicknamed"] == {"home_town": "c"}
                assert (await RfNamed.objects().get("e")).nicknamed == RfNicknamed(home_town="c")
            await _assert_client_usable(client)

    @pytest.mark.parametrize("server_values", [False, True], ids=["merge", "merge-server-values"])
    async def test_alias_only_takes_the_aliases(self, server_values: bool) -> None:
        async with orm_client("rf_named") as client:
            person = RfNamed(id="b", alias_only=RfCamelOnly(firstName="old"))
            await person.save()
            assert (await _raw("b", "rf_named"))["alias_only"] == {"firstName": "old"}

            for given, expected in (({"first_name": "new"}, "new"), ({"firstName": "new2"}, "new2")):
                await person.merge(server_values=_BUMP if server_values else None, alias_only=given)
                assert (await _raw("b", "rf_named"))["alias_only"] == {"firstName": expected}
                loaded = (await RfNamed.objects().get("b")).alias_only
                assert loaded is not None
                assert loaded.first_name == expected
            await _assert_client_usable(client)

    async def test_schemafull_sub_field_definitions_by_name(self) -> None:
        """A schema written for v0.22's rows declares the names: ``DEFINE FIELD profile.first_name``.
        Keyed by the aliases, the object was refused by 3.x and stored empty by 2.x."""
        async with orm_client("rf_named_strict") as client:
            for statement in (
                "DEFINE TABLE rf_named_strict SCHEMAFULL;",
                "DEFINE FIELD n ON rf_named_strict TYPE int DEFAULT 0;",
                "DEFINE FIELD profile ON rf_named_strict TYPE option<object>;",
                "DEFINE FIELD profile.first_name ON rf_named_strict TYPE option<string>;",
                "DEFINE FIELD profile.home_address ON rf_named_strict TYPE option<object>;",
                "DEFINE FIELD profile.home_address.streetName ON rf_named_strict TYPE option<string>;",
            ):
                await client.query(statement, {})
            person = RfNamedStrict(id="c", profile=RfCamel(first_name="old", home_address=RfStreet(streetName="x")))
            await person.save()
            row = await _raw("c", "rf_named_strict")
            assert row["profile"] == {"first_name": "old", "home_address": {"streetName": "x"}}

            await person.merge(profile={"firstName": "new"})
            await person.merge(profile={"homeAddress": {"street_name": "y"}}, server_values=_BUMP)
            row = await _raw("c", "rf_named_strict")
            assert row["profile"] == {"first_name": "new", "home_address": {"streetName": "y"}}
            assert row["n"] == 1
            loaded = (await RfNamedStrict.objects().get("c")).profile
            assert loaded == RfCamel(first_name="new", home_address=RfStreet(streetName="y"))
            await _assert_client_usable(client)


class RfCode(BaseModel):
    """A union member a ``firstName`` dict does not validate as: ``code`` is required."""

    code: int


class RfCircle(BaseModel):
    """A sub-model with a dict of sub-models: MERGE merges the dict's values into the stored ones too."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    members: dict[str, RfCamelDated] = {}


class RfNamedDeep(BaseSurrealModel):
    """Sub-models validated by name and by alias at the other object positions MERGE merges: a dict's
    values, the union member validated, a dict under a sub-model, a table-model class the validation
    builds from a dict. An array is replaced whole."""

    model_config = SurrealConfigDict(table_name="rf_named_deep")

    id: str | None = None
    contacts: dict[str, RfCamelDated] = {}
    either: RfCode | RfCamelDated | None = None
    circle: RfCircle | None = None
    owners: dict[str, RfAliasedOwner] = {}
    crowd: list[RfCamelDated] = []
    n: int = 0


class RfZip(BaseModel):
    """camelCase aliases, validated by name too, dumped by name: stored under the names."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    zip_code: str = ""


class RfDumpedByAlias(BaseModel):
    """Validated by name and by alias, and **dumped** by alias (``serialize_by_alias=True``): ``save()``
    and an instance write each sub-field under the key Pydantic dumps it under, its alias, as v0.22
    did. A sub-field the dump excludes is never written, so a value stored for it stays. ``seen_at``
    is rich, so a dict in a ``dict[str, RfDumpedByAlias]`` is validated into an instance."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True)

    first_name: str = ""
    seen_at: datetime | None = None
    home_addr: RfZip | None = None
    secret_note: str = Field(default="", exclude=True)


class RfSplitAlias(BaseModel):
    """Dumped by a ``serialization_alias`` that no read takes: the dump's key names no sub-field."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    first_name: str = Field(default="", validation_alias="fn", serialization_alias="firstName")


class RfDumped(BaseSurrealModel):
    """Sub-models dumped by alias, as a field and as the values of a dict."""

    model_config = SurrealConfigDict(table_name="rf_dumped")

    id: str | None = None
    profile: RfDumpedByAlias | None = None
    contacts: dict[str, RfDumpedByAlias] = {}
    split: RfSplitAlias | None = None
    n: int = 0


class _CapturingClient:
    """Records the variables of each statement ``merge(refresh=False)`` sends; answers no row."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def sent(self) -> Any:
        """The ``$data`` of the last ``UPDATE … MERGE $data``."""
        return self.calls[-1]["data"]

    async def query(self, _statement: str, variables: dict[str, Any]) -> None:
        self.calls.append(variables)


_REMOVED = {"firstName": None, "seenAt": None}


class TestMergeRemovalsAtObjectPositions:
    """What ``merge()`` sends for a sub-model reached through another object position than a sub-model
    field, no database. SurrealDB's MERGE merges an object into the stored one at every depth and
    replaces an array whole, so each sub-model instance at an object position is sent with the aliases
    a read takes before its names as ``None`` (NONE: the key is removed), as for a sub-model field.
    Before, only an instance or a dict sent to a field typed by the sub-model itself had them."""

    @pytest.fixture
    def client(self, monkeypatch: pytest.MonkeyPatch) -> _CapturingClient:
        fake = _CapturingClient()

        async def get_client() -> _CapturingClient:
            return fake

        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", get_client)
        return fake

    async def test_dicts_unions_and_nested_dicts(self, client: _CapturingClient) -> None:
        person = RfNamedDeep(id="x")
        await person.merge(
            refresh=False,
            contacts={"a": {"firstName": "Annie"}},
            either={"first_name": "Annie"},
            circle={"members": {"a": {"firstName": "Annie"}}},
            owners={"a": {"label": "Annie"}},
            crowd=[{"firstName": "Annie"}],
        )
        assert client.sent == {
            "contacts": {"a": {"first_name": "Annie", "seen_at": None, **_REMOVED}},
            "either": {"first_name": "Annie", "seen_at": None, **_REMOVED},
            "circle": {"members": {"a": {"first_name": "Annie", "seen_at": None, **_REMOVED}}},
            # A table-model class validated from the caller's dict is written as an object.
            "owners": {"a": {"id": None, "since": None, "label": "Annie", "Label": None}},
            "crowd": [{"first_name": "Annie", "seen_at": None}],  # an array is replaced whole
        }
        # The local apply takes the values merged, never a removal.
        assert person.contacts == {"a": RfCamelDated(first_name="Annie")}
        assert person.either == RfCamelDated(first_name="Annie")
        assert person.circle == {"members": {"a": RfCamelDated(first_name="Annie")}}
        assert person.owners == {"a": {"id": None, "since": None, "label": "Annie"}}
        assert person.crowd == [RfCamelDated(first_name="Annie")]

    async def test_instances(self, client: _CapturingClient) -> None:
        """An instance anywhere at an object position, at every depth: the class of the instance says
        which aliases to remove, so a union member is the one validated, not the first declared."""
        annie = RfCamelDated(first_name="Annie")
        person = RfNamedDeep(id="x")
        await person.merge(refresh=False, contacts={"a": annie}, either=annie, circle=RfCircle(members={"a": annie}))
        assert client.sent == {
            "contacts": {"a": {"first_name": "Annie", "seen_at": None, **_REMOVED}},
            "either": {"first_name": "Annie", "seen_at": None, **_REMOVED},
            "circle": {"members": {"a": {"first_name": "Annie", "seen_at": None, **_REMOVED}}},
        }
        assert (person.contacts, person.either, person.circle) == ({"a": annie}, annie, RfCircle(members={"a": annie}))

        await person.merge(refresh=False, either={"code": 3})
        assert client.sent == {"either": {"code": 3}}  # RfCode has no alias: nothing to remove

    async def test_server_values_assign_them_whole(self, client: _CapturingClient) -> None:
        """``UPDATE … SET`` replaces each field it assigns: a dict field is assigned whole, no removal."""
        await RfNamedDeep(id="x").merge(refresh=False, server_values=_BUMP, contacts={"a": {"firstName": "Annie"}})
        assert {"a": {"first_name": "Annie", "seen_at": None}} in client.calls[-1].values()

    async def test_an_instance_dumped_by_alias_keeps_the_keys_it_writes(self, client: _CapturingClient) -> None:
        """``serialize_by_alias=True``: the dump writes the aliases, which are also the keys a read
        takes first. The walk never overwrites a key the object carries with a removal, never removes a
        spelling of a sub-field the dump omits (``exclude=True``: its stored value stays), and goes
        down into a sub-model written under its alias (``homeAddr``)."""
        sent = {"firstName": "Annie", "seenAt": None, "homeAddr": {"zip_code": "2", "zipCode": None}}
        annie = RfDumpedByAlias(first_name="Annie", home_addr=RfZip(zip_code="2"))
        holder = RfDumped(id="x")
        await holder.merge(refresh=False, profile=annie)
        assert client.sent == {"profile": sent}

        for contact in ({"firstName": "Annie", "homeAddr": {"zipCode": "2"}}, annie):
            await holder.merge(refresh=False, contacts={"a": contact})
            assert client.sent == {"contacts": {"a": sent}}
            assert holder.contacts == {"a": annie}

        # A dict for the field itself is converted key by key: the name, its alias removed.
        await holder.merge(refresh=False, profile={"firstName": "Annie"})
        assert client.sent == {"profile": {"first_name": "Annie", "firstName": None}}

    async def test_a_serialization_alias_no_read_takes_removes_nothing(self, client: _CapturingClient) -> None:
        """The dump's ``firstName`` names no sub-field (a read takes ``fn`` or ``first_name``): nothing
        is removed for that sub-field, and ``fn`` is not sent."""
        await RfDumped(id="x").merge(refresh=False, split=RfSplitAlias(fn="Annie"))
        assert client.sent == {"split": {"firstName": "Annie"}}


class TestMergeRemovalsAtObjectPositionsE2E:
    """A v0.22 row whose objects hold both spellings, at the object positions
    ``TestMergeRemovalsAtObjectPositions`` covers, on both lines. v0.22's ``merge()`` stored a dict's
    keys as given, so ``save()`` then ``merge(contacts={"a": {"firstName": "Anna"}})`` left
    ``{"first_name": "Ann", "firstName": "Anna"}``; v0.23.0 writes the name, and without the removal
    every later merge of that sub-field was hidden behind the alias and lost without an error."""

    @pytest.mark.parametrize("given", ["dict", "instance"])
    @pytest.mark.parametrize("mode", ["merge", "no-refresh", "transaction"])
    async def test_a_v022_row_with_both_spellings(self, mode: str, given: str) -> None:
        async with orm_client("rf_named_deep") as client:
            both = {"first_name": "Ann", "firstName": "Anna"}
            await client.query(
                "CREATE $rid CONTENT $row;",
                {
                    "rid": RecordID("rf_named_deep", "v"),
                    "row": {
                        "n": 0,
                        "contacts": {"a": both, "b": {"first_name": "Bob"}},
                        "either": both,
                        "circle": {"members": {"a": both}},
                        "owners": {"a": {"label": "Ann", "Label": "Anna"}},
                        "crowd": [both],
                    },
                },
            )
            person = await RfNamedDeep.objects().get("v")
            assert person.contacts["a"].first_name == "Anna"  # the alias, under v0.22 and v0.23.0 alike
            assert person.either == RfCamelDated(first_name="Anna")

            annie = RfCamelDated(first_name="Annie")
            change: dict[str, Any] = {
                "contacts": {"a": {"firstName": "Annie"}},
                "either": {"first_name": "Annie"},
                "circle": {"members": {"a": {"firstName": "Annie"}}},
                "owners": {"a": {"label": "Annie"}},
                "crowd": [{"first_name": "Annie"}],
            }
            if given == "instance":  # an RfAliasedOwner instance would be its record id
                change.update(contacts={"a": annie}, either=annie, circle=RfCircle(members={"a": annie}), crowd=[annie])
            if mode == "transaction":
                async with SurrealDBConnectionManager.transaction() as tx:
                    await person.merge(tx=tx, **change)
            else:
                await person.merge(refresh=mode == "merge", **change)

            row = await _raw("v", "rf_named_deep")
            assert row["contacts"] == {"a": {"first_name": "Annie"}, "b": {"first_name": "Bob"}}
            assert row["either"] == {"first_name": "Annie"}
            assert row["circle"] == {"members": {"a": {"first_name": "Annie"}}}
            assert row["owners"] == {"a": {"label": "Annie"}}
            loaded = await RfNamedDeep.objects().get("v")
            assert (loaded.contacts, loaded.either) == ({"a": annie, "b": RfCamelDated(first_name="Bob")}, annie)
            assert (loaded.circle, loaded.crowd) == (RfCircle(members={"a": annie}), [annie])
            assert loaded.owners["a"].label == "Annie"

            # The instance holds the value merged: the row it was resynced from, or the values applied
            # locally (refresh=False, a buffered transaction on 2.x), which hold no removal.
            assert RfCamelDated.model_validate(person.contacts["a"]) == annie
            assert RfCamelDated.model_validate(person.either) == annie
            assert RfCircle.model_validate(person.circle) == RfCircle(members={"a": annie})
            assert RfAliasedOwner.model_validate(person.owners["a"]).label == "Annie"
            await _assert_client_usable(client)

    async def test_schemafull_takes_the_removals(self) -> None:
        """A schema written for the names declares no alias: NONE for an undeclared key, at any depth,
        is accepted by both lines (a value there is refused by 3.x)."""
        async with orm_client("rf_named_deep") as client:
            for statement in (
                "DEFINE TABLE rf_named_deep SCHEMAFULL;",
                "DEFINE FIELD n ON rf_named_deep TYPE int DEFAULT 0;",
                "DEFINE FIELD contacts ON rf_named_deep TYPE object DEFAULT {};",
                "DEFINE FIELD contacts.* ON rf_named_deep TYPE object;",
                "DEFINE FIELD contacts.*.first_name ON rf_named_deep TYPE option<string>;",
                "DEFINE FIELD contacts.*.seen_at ON rf_named_deep TYPE option<datetime>;",
                "DEFINE FIELD either ON rf_named_deep TYPE option<object>;",
                "DEFINE FIELD either.first_name ON rf_named_deep TYPE option<string>;",
                "DEFINE FIELD either.seen_at ON rf_named_deep TYPE option<datetime>;",
                "DEFINE FIELD circle ON rf_named_deep TYPE option<object>;",
                "DEFINE FIELD owners ON rf_named_deep TYPE object DEFAULT {};",
                "DEFINE FIELD crowd ON rf_named_deep TYPE array DEFAULT [];",
            ):
                await client.query(statement, {})
            person = RfNamedDeep(id="s", contacts={"a": RfCamelDated(first_name="Ann")})
            await person.save()
            at = datetime(2026, 1, 2, tzinfo=UTC)
            await person.merge(contacts={"b": {"firstName": "Bob", "seenAt": at}}, either={"firstName": "E"})
            row = await _raw("s", "rf_named_deep")
            assert row["contacts"] == {"a": {"first_name": "Ann"}, "b": {"first_name": "Bob", "seen_at": at}}
            assert row["either"] == {"first_name": "E"}
            loaded = await RfNamedDeep.objects().get("s")
            assert loaded.contacts["b"] == RfCamelDated(first_name="Bob", seen_at=at)
            await _assert_client_usable(client)

    @pytest.mark.parametrize("given", ["dict", "instance"])
    @pytest.mark.parametrize("mode", ["merge", "no-refresh", "transaction"])
    async def test_a_sub_model_dumped_by_alias_keeps_its_keys(self, mode: str, given: str) -> None:
        """``serialize_by_alias=True``: the aliases ``save()`` writes are the keys a read takes, and a
        ``merge()`` of an instance writes them again, never removed by the same statement; a value
        stored under the alias of a sub-field the dump excludes (``secretNote``) stays."""
        async with orm_client("rf_dumped") as client:
            holder = RfDumped(
                id="k",
                profile=RfDumpedByAlias(first_name="Ann", home_addr=RfZip(zip_code="1")),
                contacts={"a": RfDumpedByAlias(first_name="Ann")},
            )
            await holder.save()
            row = await _raw("k", "rf_dumped")
            assert row["profile"] == {"firstName": "Ann", "homeAddr": {"zip_code": "1"}}
            assert row["contacts"] == {"a": {"firstName": "Ann"}}
            await client.query(
                "UPDATE $rid MERGE $data;",
                {
                    "rid": RecordID("rf_dumped", "k"),
                    "data": {"profile": {"secretNote": "keep"}, "contacts": {"a": {"secretNote": "keep"}}},
                },
            )

            annie = RfDumpedByAlias(first_name="Annie", home_addr=RfZip(zip_code="2"))
            contact = {"firstName": "Annie", "homeAddr": {"zipCode": "2"}} if given == "dict" else annie
            if mode == "transaction":
                async with SurrealDBConnectionManager.transaction() as tx:
                    await holder.merge(tx=tx, profile=annie)
                    await holder.merge(tx=tx, contacts={"a": contact})
            else:
                await holder.merge(refresh=mode == "merge", profile=annie)
                await holder.merge(refresh=mode == "merge", contacts={"a": contact})

            row = await _raw("k", "rf_dumped")
            stored = {"firstName": "Annie", "homeAddr": {"zip_code": "2"}, "secretNote": "keep"}
            assert row["profile"] == stored
            assert row["contacts"] == {"a": stored}
            loaded = await RfDumped.objects().get("k")
            assert loaded.profile is not None
            for sub in (loaded.profile, loaded.contacts["a"]):
                assert (sub.first_name, sub.secret_note, sub.home_addr) == ("Annie", "keep", RfZip(zip_code="2"))

            # The instance holds the value merged: the row it was resynced from, or the values applied
            # locally (refresh=False, a buffered transaction on 2.x).
            assert RfDumpedByAlias.model_validate(holder.profile).first_name == "Annie"
            assert RfDumpedByAlias.model_validate(holder.contacts["a"]).first_name == "Annie"
            await _assert_client_usable(client)


# ==================== Models that allow arbitrary types ====================


class RfOpaque:
    """A class Pydantic has no schema for: only a model with ``arbitrary_types_allowed`` declares it."""


class RfArbitrary(BaseSurrealModel):
    """Rich fields next to an arbitrary type: their ``TypeAdapter`` needs the model's config."""

    model_config = SurrealConfigDict(table_name="rf_arbitrary", arbitrary_types_allowed=True)

    id: str | None = None
    span: tuple[int, int] | RfOpaque | None = None
    at: datetime | RfOpaque | None = None


_ARBITRARY_ROW = {"span": [1, 2], "at": datetime(2026, 1, 2, tzinfo=UTC)}


class _ArbitraryClient:
    """The SDK calls behind ``save()`` without id, ``upsert()``, ``refresh()`` and ``merge()``: each answers a row."""

    def __init__(self) -> None:
        self.rid = RecordID("rf_arbitrary", "x")

    def _row(self, **changes: Any) -> dict[str, Any]:
        return {"id": self.rid, **_ARBITRARY_ROW, **changes}

    async def create(self, _what: Any, _data: Any) -> dict[str, Any]:
        return self._row()

    async def query(self, _statement: str, _variables: Any) -> list[dict[str, Any]]:
        return [{"before": None, "after": self._row()}]

    async def select(self, _rid: Any) -> dict[str, Any]:
        return self._row()

    async def merge(self, _rid: Any, data: dict[str, Any]) -> dict[str, Any]:
        return self._row(**data)


class TestArbitraryTypeFields:
    """A model with ``arbitrary_types_allowed`` builds its rich fields' ``TypeAdapter`` with that
    setting (no database): without it, Pydantic could not build one for ``tuple[int, int] | RfOpaque``
    and ``save()`` without id raised after writing the row, as did ``upsert()``, ``refresh()`` and
    ``merge(at=…)``. When an adapter still cannot be built, nothing is validated: the value is kept,
    sent or bound as given, as before v0.23.0."""

    @pytest.fixture
    def client(self, monkeypatch: pytest.MonkeyPatch) -> _ArbitraryClient:
        fake = _ArbitraryClient()

        async def get_client() -> _ArbitraryClient:
            return fake

        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", get_client)
        return fake

    @staticmethod
    def _assert_typed(item: RfArbitrary) -> None:
        assert type(item.span) is tuple, item.span
        assert item.span == (1, 2)
        assert item.at == datetime(2026, 1, 2, tzinfo=UTC)

    async def test_save_upsert_refresh_and_merge(self, client: _ArbitraryClient) -> None:
        saved = RfArbitrary(span=(1, 2))
        await saved.save()  # no id: the row the server sends back is applied
        assert saved.id == client.rid
        self._assert_typed(saved)

        upserted = RfArbitrary(id="x", span=(1, 2))
        await upserted.upsert()
        self._assert_typed(upserted)

        refreshed = RfArbitrary(id="x")
        await refreshed.refresh()
        self._assert_typed(refreshed)

        later = datetime(2027, 1, 1, tzinfo=UTC)
        await refreshed.merge(at=later)
        assert refreshed.at == later
        assert RfArbitrary._coerce_field_value("at", "2027-01-01T00:00:00Z") == later
        assert RfArbitrary._coerce_field_value("span", [3, 4], context="bulk_update") == (3, 4)

    def test_adapters_take_the_model_setting_but_not_on_their_own_config(self) -> None:
        """Pydantic refuses a ``config`` for a type that has its own (a model, a dataclass, a
        ``TypedDict``): such an annotation is built without it, so it still validates."""

        class Holder(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="rf_arbitrary_holder", arbitrary_types_allowed=True)

            id: str | None = None
            profile: RfProfile = RfProfile()
            plains: list[RfPlain] = []

        assert Holder._coerce_field_value("profile", {"bday": "2026-01-02"}, context="bulk_update") == RfProfile(
            bday=date(2026, 1, 2)
        )
        with pytest.raises(ValueError, match=r"^atomic_append\(plains=…\): the RfPlain instance is written as its record id"):
            Holder._check_item_links("atomic_append", "plains", [RfPlain(id="o1")])

    def test_an_adapter_that_cannot_be_built_validates_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The four fallbacks: ``_apply_record`` keeps the raw value, a keyword write sends the value
        as given, the ``atomic_*`` check accepts it (as for ``Any``), a filter binds it as given."""
        from surreal_orm_lite import model_base

        plain = RfPlain(id="o1")
        event = RfArbitrary(id="x")

        def refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("no schema")

        for cache in ("_APPLY_ADAPTERS", "_LOOKUP_ADAPTERS", "_WRITE_ADAPTERS"):
            monkeypatch.setattr(model_base, cache, {})
        monkeypatch.setattr(model_base.pydantic, "TypeAdapter", refuse)

        event._apply_record({"span": [1, 2]})
        assert event.span == [1, 2]  # type: ignore[comparison-overlap]  # kept as it came
        assert RfArbitrary._coerce_field_value("at", "soon") == "soon"
        assert RfEvent._coerce_field_value("ttl", "not a duration") == "not a duration"
        RfPerson._check_item_links("atomic_append", "plains", [plain])  # nothing checked
        assert RfEvent._coerce_lookup_value("at", "gt", "soon") == "soon"


class TestArbitraryTypeFieldsE2E:
    """``save()`` without id, ``upsert()``, ``refresh()`` and ``merge()`` on a model that allows
    arbitrary types, on both lines."""

    async def test_writes_and_reads(self) -> None:
        async with orm_client("rf_arbitrary") as client:
            item = RfArbitrary(span=(1, 2), at=datetime(2026, 1, 2, tzinfo=UTC))
            await item.save()
            assert isinstance(item.id, RecordID)
            assert type(item.span) is tuple
            assert item.span == (1, 2)

            later = datetime(2027, 1, 1, tzinfo=UTC)
            await item.merge(at=later)
            assert item.at == later
            row = await _raw(item.id)
            assert row["at"] == later
            assert row["span"] == [1, 2]

            await item.refresh()
            assert item.span == (1, 2)
            other = RfArbitrary(id="u", span=(3, 4))
            await other.upsert()
            assert other.span == (3, 4)
            assert (await RfArbitrary.objects().get("u")).span == (3, 4)
            await _assert_client_usable(client)


class TestTableInstanceValuesE2E:
    """A table-model instance given as a value is its record id (v0.22.0's filter rule, the full
    ORM's link rule): stored as a record link where the field accepts a ``RecordID``, refused before
    anything is sent elsewhere — never embedded as an object no read accepts. Both lines."""

    async def test_merge_and_bulk_update_store_the_record_id(self) -> None:
        async with orm_client("rf_person") as client:
            alice = RfPerson(id="alice")
            await alice.save()
            rid = RecordID("rf_person", "alice")
            bob = RfPerson(id="bob")
            await bob.save()
            await bob.merge(author=alice)
            assert bob.author == rid
            await RfPerson(id="carol", n=7).save()
            assert await RfPerson.objects().filter(n=7).bulk_update(author=alice) == 1

            for name in ("bob", "carol"):
                assert (await _raw(name, "rf_person"))["author"] == rid
                loaded = await RfPerson.objects().get(name)
                assert type(loaded.author) is RecordID
                assert loaded.author == rid
            everyone = await RfPerson.objects().all()
            assert sorted(str(person.get_id()) for person in everyone) == ["alice", "bob", "carol"]
            linked = await RfPerson.objects().filter(author=alice).exec()
            assert sorted(str(person.get_id()) for person in linked) == ["bob", "carol"]
            await _assert_client_usable(client)

    async def test_raw_query_variable(self) -> None:
        async with orm_client("rf_person") as client:
            alice = RfPerson(id="alice")
            await alice.save()
            await RfPerson(id="bob", author=RecordID("rf_person", "alice")).save()
            rows = await RfPerson.raw_query("SELECT * FROM rf_person WHERE author = $who;", variables={"who": alice})
            assert len(rows) == 1
            assert isinstance(rows[0], RfPerson)
            assert rows[0].get_id() == "bob"
            await _assert_client_usable(client)

    @pytest.mark.parametrize("write", ["merge", "bulk_update"])
    async def test_a_field_that_cannot_hold_a_record_id_refuses(self, write: str) -> None:
        async with orm_client("rf_person") as client:
            alice = RfPerson(id="alice", n=1)
            await alice.save()
            with pytest.raises(ValueError, match=r"\(profile=…\): the RfPerson instance is written as its record id"):
                if write == "merge":
                    await alice.merge(profile=alice)
                else:
                    await RfPerson.objects().filter(n=1).bulk_update(profile=alice)

            assert (await _raw("alice", "rf_person")).get("profile") is None
            await _assert_client_usable(client)


class TestTableInstanceChecks:
    """The checks the writes that do not validate their values as a field run, no database:
    ``child_annotation`` resolves the declared position, ``patch()`` and the ``atomic_*`` helpers refuse
    a table-model instance where that position cannot hold a ``RecordID``."""

    def test_child_annotation(self) -> None:
        assert child_annotation(list[RfPlain] | None, "-") is RfPlain
        assert child_annotation(list[RfPlain], "3") is RfPlain
        assert child_annotation(list[RfPlain], "name") is None
        assert child_annotation(tuple[int, ...], "0") is int
        assert child_annotation(tuple[int, str], "0") is None
        assert child_annotation(dict[str, RfPlain], "a") is RfPlain
        assert child_annotation(RfSeen | None, "lastSeen") == datetime | None
        assert child_annotation(RfSeen, "seen") == datetime | None
        assert child_annotation(RfSeen, "zip") is None
        assert child_annotation(Any, "a") is None
        assert child_annotation(int | str, "a") is None

    def test_patch_checks_the_declared_position(self) -> None:
        plain = RfPlain(id="o1")
        alice = RfPerson(id="alice")
        rid = RecordID("rf_person", "alice")
        for op in (
            {"op": "add", "path": "/plains/-", "value": plain},
            {"op": "replace", "path": "/plains/0", "value": plain},
            {"op": "replace", "path": "/plain_map", "value": {"a": plain}},
            {"op": "add", "path": "/profile/city", "value": plain},
            {"op": "replace", "path": "", "value": {"n": 1, "plains": [plain]}},
        ):
            with pytest.raises(ValueError, match=r"^patch\(/(plains|plain_map|profile/city)[-/0-9]*=…\): .*record id"):
                RfPerson._columns_for_patch([op])
        sent = RfPerson._columns_for_patch(
            [
                {"op": "add", "path": "/links/-", "value": alice},
                {"op": "add", "path": "/undeclared", "value": [alice]},  # nothing declared: its record id
                {"op": "test", "path": "/plains/0", "value": plain},  # compared, not written
                {"op": "replace", "path": "", "value": {"author": alice}},
            ]
        )
        assert [op["value"] for op in sent] == [rid, [rid], RecordID("rf_plain", "o1"), {"author": rid}]
        with pytest.raises(ValueError, match=r"^patch\(/links/-=…\): .*unsaved RfPerson has no record id"):
            RfPerson._columns_for_patch([{"op": "add", "path": "/links/-", "value": RfPerson()}])

    def test_atomic_items_check_the_item_type(self) -> None:
        plain = RfPlain(id="o1")
        with pytest.raises(ValueError, match=r"^atomic_append\(plains=…\): the RfPlain instance is written as its record id"):
            RfPerson._check_item_links("atomic_append", "plains", [plain])
        RfPerson._check_item_links("atomic_append", "links", [RfPerson(id="alice")])  # list[RecordID]: accepted
        RfPerson._check_item_links("atomic_append", "undeclared", [plain])  # nothing declared: its record id
        RfPerson._check_item_links("atomic_append", "n", [plain])  # not an array: nothing to check
        RfPerson._check_item_links("atomic_append", "plains", [{"name": "x"}])  # no instance


class TestTableInstanceInPlainFieldsE2E:
    """A table-model instance inside a field that holds no rich type (``plains: list[RfPlain]``,
    ``plain_map: dict[str, RfPlain]``) is its record id like anywhere else, which these fields cannot
    hold: every keyword write, the atomic helpers and ``patch()`` refuse it before anything is sent.
    Before, the record id was written and the row no longer read back (``plains.0 Input should be a
    valid dictionary or instance of RfPlain``). A field that holds record ids stores them. Both lines."""

    @pytest.mark.parametrize(
        "write",
        [
            "merge",
            "merge-dict",
            "merge-server-values",
            "update_or_create",
            "bulk_update",
            "atomic_append",
            "atomic_set_add",
            "atomic_remove",
            "atomic_append_many",
            "atomic_set_add_many",
            "patch",
            "patch-field",
            "queryset-patch",
        ],
    )
    async def test_refused_before_sending(self, write: str) -> None:
        async with orm_client("rf_person") as client:
            person = RfPerson(id="q", n=5)
            await person.save()
            plain = RfPlain(id="o1", name="x")
            add = [{"op": "add", "path": "/plains/-", "value": plain}]
            calls: dict[str, Callable[[], Awaitable[Any]]] = {
                "merge": lambda: person.merge(plains=[plain]),
                "merge-dict": lambda: person.merge(plain_map={"a": plain}),
                "merge-server-values": lambda: person.merge(server_values=_BUMP, plains=[plain]),
                "update_or_create": lambda: RfPerson.objects().update_or_create(id="q", defaults={"plains": [plain]}),
                "bulk_update": lambda: RfPerson.objects().filter(n=5).bulk_update(plains=[plain]),
                "atomic_append": lambda: person.atomic_append("plains", plain),
                "atomic_set_add": lambda: person.atomic_set_add("plains", plain),
                "atomic_remove": lambda: person.atomic_remove("plains", plain),
                "atomic_append_many": lambda: person.atomic_append_many("plains", [plain]),
                "atomic_set_add_many": lambda: person.atomic_set_add_many("plains", [plain]),
                "patch": lambda: person.patch(add),
                "patch-field": lambda: person.patch([{"op": "replace", "path": "/plains", "value": [plain]}]),
                "queryset-patch": lambda: RfPerson.objects().filter(n=5).patch(add),
            }
            with pytest.raises(ValueError, match=r"\(/?plain(s|_map)[-/0-9]*=…\): .*record id"):
                await calls[write]()

            row = await _raw("q", "rf_person")
            assert row["plains"] == []
            assert row["plain_map"] == {}
            assert row["n"] == 5
            loaded = await RfPerson.objects().get("q")
            assert loaded.plains == []
            await _assert_client_usable(client)

    async def test_a_field_that_holds_record_ids_stores_them(self) -> None:
        async with orm_client("rf_person") as client:
            people = {name: RfPerson(id=name) for name in ("alice", "bob", "carol", "dan")}
            for other in people.values():
                await other.save()
            person = RfPerson(id="q")
            await person.save()
            await person.merge(links=[people["alice"]])
            await person.atomic_append("links", people["bob"])
            await person.patch([{"op": "add", "path": "/links/-", "value": people["carol"]}])
            await person.atomic_append_many("links", [people["dan"]])

            expected = [RecordID("rf_person", name) for name in ("alice", "bob", "carol", "dan")]
            assert (await _raw("q", "rf_person"))["links"] == expected
            assert (await RfPerson.objects().get("q")).links == expected
            await person.atomic_remove("links", people["bob"])
            assert (await _raw("q", "rf_person"))["links"] == [expected[0], *expected[2:]]
            await _assert_client_usable(client)


# ==================== Task 10 — live-query filters on rich fields (spec §5.5) ====================

_FEB = datetime(2026, 2, 1, tzinfo=UTC)

# (filter arguments, the row that must not match, the row that must) — AC-24. The non-matching row is
# saved first, so the first event is the matching row only if the server applied the filter, and
# none arrives (timeout) if the filter matched nothing.
_LIVE_CASES = [
    pytest.param(
        (), {"ttl__gte": timedelta(hours=1)}, {"ttl": timedelta(minutes=30)}, {"ttl": timedelta(hours=1)}, id="ttl__gte"
    ),
    pytest.param(
        (), {"ttl__gt": "1h"}, {"ttl": timedelta(hours=1)}, {"ttl": timedelta(hours=1, microseconds=1)}, id="ttl__gt-str"
    ),
    pytest.param(
        (),
        {"grace": Duration.parse("1h30m1ns")},
        {"grace": Duration.parse("1h30m")},
        {"grace": Duration.parse("1h30m1ns")},
        id="grace-ns",
    ),
    pytest.param(
        (),
        {"hours__contains": 12},
        {"hours": Range(BoundIncluded(1), BoundExcluded(5))},
        {"hours": Range(BoundIncluded(9), BoundExcluded(17))},
        id="hours__contains",
    ),
    pytest.param((), {"n__in": Range(BoundIncluded(3), None)}, {"n": 2}, {"n": 3}, id="n__in-open-end"),
    pytest.param((), {"n__not_in": Range(None, BoundIncluded(5))}, {"n": 5}, {"n": 6}, id="n__not_in-open-begin"),
    pytest.param((), {"day": date(2026, 1, 2)}, {"day": date(2026, 1, 3)}, {"day": date(2026, 1, 2)}, id="day"),
    pytest.param((), {"at__in": Range(BoundIncluded(_JAN), BoundExcluded(_FEB))}, {"at": _FEB}, {"at": _JAN}, id="at__in"),
    pytest.param(
        (),
        {"period__contains": "2026-01-15T00:00:00Z"},
        {"period": Range(BoundIncluded(_FEB), None)},
        {"period": Range(BoundIncluded(_JAN), BoundExcluded(_FEB))},
        id="period__contains-str",
    ),
    pytest.param(
        (), {"ttls__contains": "1h"}, {"ttls": [timedelta(hours=2)]}, {"ttls": [timedelta(hours=1)]}, id="ttls__contains"
    ),
    # An open range end followed by ``AND``, and ranges inside an ``OR``.
    pytest.param(
        (),
        {"n__in": Range(BoundIncluded(3), None), "ttl__gte": timedelta(hours=1)},
        {"n": 5, "ttl": timedelta(minutes=59)},
        {"n": 5, "ttl": timedelta(hours=1)},
        id="open-end-then-and",
    ),
    pytest.param(
        (Q(n__in=Range(None, BoundExcluded(0))) | Q(day__in=Range(BoundIncluded(date(2026, 1, 2)), None)),),
        {},
        {"day": date(2026, 1, 1)},
        {"day": date(2026, 1, 2)},
        id="q-or",
    ),
]

_LIVE_ARGS = ("args", "kwargs", "miss", "hit")


async def _define_rf_event(client: Any) -> None:
    # SurrealDB 3.x refuses a live query on an undefined table.
    await client.query("DEFINE TABLE rf_event SCHEMALESS;", {})


async def _save_miss_then_hit(miss: dict[str, Any], hit: dict[str, Any]) -> None:
    await RfEvent(id="miss", **miss).save()
    await RfEvent(id="hit", **hit).save()


class TestRichLiveE2E:
    """A live query filtered on rich fields reports the matching write and not the other, on both
    lines (AC-24): the coerced filter values are inlined as duration, date and range literals."""

    @pytest.mark.parametrize(_LIVE_ARGS, _LIVE_CASES)
    async def test_live_reports_only_the_matching_write(
        self, args: tuple[Any, ...], kwargs: dict[str, Any], miss: dict[str, Any], hit: dict[str, Any]
    ) -> None:
        async with orm_client("rf_event") as client:
            await _define_rf_event(client)
            async with asyncio.timeout(10), RfEvent.objects().filter(*args, **kwargs).live() as stream:
                await _save_miss_then_hit(miss, hit)
                event = await anext(stream)
        assert event.action is LiveAction.CREATE
        assert event.record_id == "rf_event:hit"
        assert event.validation_error is None
        assert {name: getattr(event.instance, name) for name in hit} == hit

    @pytest.mark.parametrize(_LIVE_ARGS, _LIVE_CASES)
    async def test_watch_reports_only_the_matching_write(
        self, args: tuple[Any, ...], kwargs: dict[str, Any], miss: dict[str, Any], hit: dict[str, Any]
    ) -> None:
        async with orm_client("rf_event") as client:
            await _define_rf_event(client)
            async with asyncio.timeout(10), RfEvent.objects().filter(*args, **kwargs).watch() as stream:
                await _save_miss_then_hit(miss, hit)
                notif = await anext(stream)
        assert str(notif["record"]) == "rf_event:hit"


class TestRichLiveCompile:
    """The live statement inlines the coerced Python values (``_build_where(encode=False)``), never
    the encoded wire forms, and the range bounds stay a closed set."""

    def test_rich_filters_compile_to_literals(self) -> None:
        qs = RfEvent.objects().filter(
            Q(n__in=Range(BoundIncluded(3), None)) | ~Q(hours__contains="12"),
            ttl__gte=timedelta(hours=1),
            day="2026-01-02",
            grace__lte="1h30m1ns",
            n__not_in=Range(),
        )
        assert qs._compile_live(False) == (
            "LIVE SELECT * FROM rf_event WHERE ttl >= 1h AND day = d'2026-01-02T00:00:00Z' AND grace <= 1h30m1ns "
            "AND n NOT IN .. AND (n IN 3.. OR NOT (hours CONTAINS 12));"
        )

    def test_variables_are_inlined_too(self) -> None:
        qs = RfEvent.objects().filter(ttl__gte=Var("t"), period__contains=Var("p"))
        qs = qs.variables(t=Duration.parse("1h30m1ns"), p=date(2026, 1, 2))
        assert qs._compile_live(False) == (
            "LIVE SELECT * FROM rf_event WHERE ttl >= 1h30m1ns AND period CONTAINS d'2026-01-02T00:00:00Z';"
        )

    @pytest.mark.parametrize(
        ("kwargs", "named"),
        [
            ({"n__in": Range(BoundIncluded(True), None)}, "bool"),
            ({"token__in": Range(BoundIncluded(_TOKEN), None)}, "UUID"),  # coerced to UUID bounds, still refused
            ({"n__in": Range(None, BoundExcluded(float("nan")))}, "finite"),
        ],
        ids=["bool", "UUID", "nan"],
    )
    def test_range_bound_outside_the_closed_set_raises_at_the_call(self, kwargs: dict[str, Any], named: str) -> None:
        qs = RfEvent.objects().filter(**kwargs)
        for subscribe in (qs.live, qs.watch):
            with pytest.raises(TypeError, match=rf"\$_f0: Cannot inline .*range bound.*{named}"):
                subscribe()


# ==================== Upgrading from v0.22 (AC-27) ====================

# The README's statements, character for character (table and column aside): the unit check below
# finds the ``event``/``at`` instance of both templates in README section 24, and the E2E test runs them.
UPGRADE_SELECT = "SELECT id, {col} FROM {table} WHERE {col}.is_string();"
UPGRADE_UPDATE = "UPDATE (SELECT VALUE id FROM {table} WHERE {col}.is_string()) SET {col} = <{cast}> {col};"
UPGRADE_COLUMNS = [("at", "datetime"), ("day", "datetime"), ("token", "uuid"), ("price", "decimal"), ("ttl", "duration")]
UPGRADE_SENTENCE = (
    "SurrealDB runs each statement in its own transaction: a value the cast cannot read makes its `UPDATE` "
    "report an error and convert nothing in that column, while the statements for the other columns still run. "
    "Fix or clear that value, then run the statements again. They only touch values still stored as strings, "
    "so re-running is safe."
)
_UPGRADE_PIN = (
    "SurrealDB no longer rolls back a failing UPDATE statement: update README section 24 "
    "'Upgrading from v0.22' and the CHANGELOG note"
)
_UPGRADE_SEED = (
    "REMOVE TABLE IF EXISTS rf_upg;",
    "CREATE rf_upg:1 SET at = '2026-01-02T03:04:05Z', day = '2026-01-02', "
    "token = '0190c3d1-5c3b-7cc6-8f8a-2d2b1f0e9a11', price = '1.50', ttl = '1h30m';",
    "CREATE rf_upg:2 SET at = d'2026-01-02T03:04:05Z', day = d'2026-01-02T00:00:00Z', "
    "token = u'0190c3d1-5c3b-7cc6-8f8a-2d2b1f0e9a11', price = 1.50dec, ttl = 1h30m;",
    "CREATE rf_upg:3 SET note = 'x';",
    "CREATE rf_upg:4 SET at = 'yesterday';",
)


async def _statement(client: Any, sql: str) -> dict[str, Any]:
    """One statement through ``query_raw``: its ``status`` and ``result``, an ``ERR`` never raised."""
    raw = await client.query_raw(sql)
    (stmt,) = raw["result"]
    assert isinstance(stmt, dict), f"{sql}: {raw!r}"
    return stmt


async def _ok(client: Any, sql: str) -> Any:
    stmt = await _statement(client, sql)
    assert stmt["status"] == "OK", f"{sql}: {stmt!r}"
    return stmt["result"]


async def _readme_statements(client: Any) -> tuple[list[Any], list[str]]:
    """The README's statements as printed, one request each: the listed rows, then each ``UPDATE`` status."""
    listed = await _ok(client, UPGRADE_SELECT.format(table="rf_upg", col="at"))
    statuses = [
        (await _statement(client, UPGRADE_UPDATE.format(table="rf_upg", col=col, cast=cast)))["status"]
        for col, cast in UPGRADE_COLUMNS
    ]
    return listed, statuses


def _record_keys(rows: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    """``(table, id)`` of each row, sorted: the record ids a raw ``SELECT`` returned."""
    return sorted((row["id"].table_name, row["id"].id) for row in rows)


class TestUpgradeFromV022E2E:
    """README section 24, "Upgrading from v0.22": the one-off conversion of the strings v0.22's keyword
    writes stored in rich fields (spec §2.12, §4.6; AC-27). The statements are the README's; the server's
    per-statement rollback the README states is pinned (D15): a failing ``UPDATE`` converts nothing in its
    column, the other columns' statements still run, and re-running after a fix converts the rest."""

    def test_readme_prints_the_tested_statements(self) -> None:
        text = " ".join((Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8").split())
        assert UPGRADE_SELECT.format(table="event", col="at") in text
        assert UPGRADE_UPDATE.format(table="event", col="at", cast="datetime") in text
        assert UPGRADE_SENTENCE in text

    async def test_upgrade_statements_convert_v022_strings(self) -> None:
        client = await _http_client()  # over HTTP: one aiohttp session per request, never closed (SDK 2.0.0)
        try:
            for sql in _UPGRADE_SEED:
                await _ok(client, sql)

            # First pass: the unreadable 'yesterday' makes the <datetime> UPDATE of `at` fail.
            listed, statuses = await _readme_statements(client)
            assert _record_keys(listed) == [("rf_upg", 1), ("rf_upg", 4)]
            assert statuses == ["ERR", "OK", "OK", "OK", "OK"]
            assert await _ok(client, "SELECT VALUE at FROM ONLY rf_upg:4;") == "yesterday"
            still_strings = await _ok(client, "SELECT id FROM rf_upg WHERE at.is_string();")
            assert _record_keys(still_strings) == [("rf_upg", 1), ("rf_upg", 4)], _UPGRADE_PIN
            row1 = await _ok(client, "SELECT * FROM ONLY rf_upg:1;")
            assert (type(row1["at"]), type(row1["day"]), type(row1["token"]), type(row1["price"]), type(row1["ttl"])) == (
                str,
                datetime,
                UUID,
                Decimal,
                surrealdb.Duration,
            ), _UPGRADE_PIN

            # Fix the value (still a string), then run the README statements again, as printed.
            await _ok(client, "UPDATE rf_upg:4 SET at = '2026-01-03T00:00:00Z';")
            listed, statuses = await _readme_statements(client)
            assert _record_keys(listed) == [("rf_upg", 1), ("rf_upg", 4)]
            assert statuses == ["OK"] * len(UPGRADE_COLUMNS)

            assert await _ok(client, "SELECT id FROM rf_upg WHERE at.is_string();") == []
            rows = {row["id"].id: row for row in await _ok(client, "SELECT * FROM rf_upg;")}
            assert sorted(rows) == [1, 2, 3, 4]
            columns = [col for col, _ in UPGRADE_COLUMNS]
            for n in (1, 2):  # the types the SDK decodes — never `x.is_datetime()`, true on an ISO string
                row = rows[n]
                assert type(row["at"]) is datetime
                assert type(row["day"]) is datetime
                assert row["day"] == datetime(2026, 1, 2, tzinfo=UTC)
                assert type(row["token"]) is UUID
                assert type(row["price"]) is Decimal
                assert type(row["ttl"]) is surrealdb.Duration
            assert [rows[1][col] for col in columns] == [rows[2][col] for col in columns]
            assert set(rows[4]) == {"id", "at"}
            assert rows[4]["at"] == datetime(2026, 1, 3, tzinfo=UTC)
            assert set(rows[3]) == {"id", "note"}
        finally:
            await client.query_raw("REMOVE TABLE IF EXISTS rf_upg;")


# ==================== Auth variables: signup / signin ====================


class RfMember(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_member", access_name="rf_member_access")

    id: str | None = None
    email: str
    password: str
    bday: date | None = None
    trial: timedelta | None = None
    credit: Decimal | None = None
    joined: datetime | None = None


class RfBadge(AuthenticatedUserMixin, BaseSurrealModel):
    """Signs in with a ``UUID`` identifier."""

    model_config = SurrealConfigDict(table_name="rf_badge", access_name="rf_badge_access", identifier_field="badge")

    id: str | None = None
    badge: UUID
    password: str


class RfProfiled(AuthenticatedUserMixin, BaseSurrealModel):
    """Signs up with sub-models that hold a rich type."""

    model_config = SurrealConfigDict(table_name="rf_profiled", access_name="rf_profiled_access")

    id: str | None = None
    email: str
    password: str
    profile: RfProfile | None = None
    history: list[RfProfile] = []
    activity: RfSeen | None = None


_AUTH_PASSWORD = "s3cret-passphrase"
_TWO_WEEKS_NS = 14 * 86400 * 10**9


@contextlib.asynccontextmanager
async def _auth_access(model: Any) -> AsyncIterator[Any]:
    """``orm_client`` on the model's table with its access method defined; root again on exit."""
    async with orm_client(model.get_table_name()) as client:
        await model.define_access()
        try:
            yield client
        finally:
            # A connection-level signup leaves the shared session as the record user.
            with contextlib.suppress(Exception):
                await SurrealDBConnectionManager.invalidate()
            with contextlib.suppress(Exception):
                await client.query(f"REMOVE ACCESS {model.get_access_name()} ON DATABASE;", {})


@pytest_asyncio.fixture(loop_scope="function")
async def no_connection(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """A configured connection that must never be opened: the call is refused before any request."""

    async def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("no connection may be opened for a value refused before sending")

    monkeypatch.setattr(SurrealDBConnectionManager, "_open_client", _forbidden)
    SurrealDBConnectionManager.set_connection(
        url="ws://localhost:1/rpc", user="root", password="root", namespace="ns", database="db"
    )
    yield
    await SurrealDBConnectionManager.unset_connection()


class TestRichAuthVariables:
    """``build_auth_payload`` encodes the access method's variables (``stored=False``, like
    ``raw_query``); a model's ``signup()``/``signin()`` validate each value addressed to a rich field
    first, as ``merge()`` does — no request is sent for a value that does not fit."""

    def test_build_auth_payload_encodes_the_variables(self) -> None:
        variables = {
            "email": "a@b.c",
            "bday": date(1990, 1, 2),
            "trial": timedelta(days=14, microseconds=1),
            "credit": Decimal("1E+3"),
            "zero": timedelta(0),  # a raw path: the access method may only compare it
            "tags": {"b", "a"},
        }
        payload = build_auth_payload(access="acct", variables=variables, default_namespace="ns", default_database="db")
        sent = payload["variables"]
        assert sent["email"] == "a@b.c"
        assert sent["bday"] == datetime(1990, 1, 2, tzinfo=UTC)
        assert sent["trial"] == CBORTag(TAG_DURATION_COMPACT, [14 * 86400, 1000])
        assert sent["credit"] == CBORTag(TAG_DECIMAL_STRING, "1000")
        assert sent["zero"] == CBORTag(TAG_DURATION_COMPACT, [0, 0])
        assert sent["tags"] == ["a", "b"]
        assert variables["bday"] == date(1990, 1, 2)  # the caller's mapping is not mutated

    @pytest.mark.parametrize(("value", "error"), [(2**64, ValueError), (time(1), TypeError)], ids=["int", "time"])
    async def test_connection_signup_refuses_before_sending(self, no_connection: None, value: Any, error: type) -> None:
        with pytest.raises(error):
            await SurrealDBConnectionManager.signup(access="acct", variables={"email": "a@b.c", "n": value})

    def test_signup_variables_are_validated_and_encoded(self) -> None:
        sent = RfMember._auth_variables(
            "signup",
            {
                "email": "a@b.c",
                "password": "pw",
                "bday": "1990-01-02",
                "trial": "2w",
                "credit": Decimal("1E+3"),
                "joined": "2026-01-01T00:00:00Z",
            },
            require_all=True,
        )
        assert sent == {
            "email": "a@b.c",
            "password": "pw",
            "bday": datetime(1990, 1, 2, tzinfo=UTC),
            "trial": CBORTag(TAG_DURATION_COMPACT, [14 * 86400, 0]),
            "credit": CBORTag(TAG_DECIMAL_STRING, "1000"),
            "joined": _JAN,
        }

    @_PROFILE_FORMS
    def test_signup_sub_models_are_validated_and_sent_as_objects(self, profile: Any) -> None:
        sent = RfProfiled._auth_variables(
            "signup",
            {"email": "a@b.c", "password": "pw", "profile": profile, "history": [_PROFILE]},
            require_all=True,
        )
        stored = {"bday": datetime(1990, 1, 2, tzinfo=UTC), "city": "x"}
        assert sent == {"email": "a@b.c", "password": "pw", "profile": stored, "history": [stored], "activity": None}

    def test_signin_variables_are_validated(self) -> None:
        sent = RfBadge._auth_variables("signin", {"badge": str(_TOKEN), "password": "pw"}, require_all=False)
        assert sent == {"badge": _TOKEN, "password": "pw"}
        assert type(sent["badge"]) is UUID

    async def test_signup_refuses_a_value_that_does_not_fit(self, no_connection: None) -> None:
        with pytest.raises(ValueError, match=r"signup\(bday=…\): 'someday' is not a valid date"):
            await RfMember.signup(email="a@b.c", password="pw", bday="someday")

    async def test_signin_refuses_a_value_that_does_not_fit(self, no_connection: None) -> None:
        with pytest.raises(ValueError, match=r"signin\(badge=…\): 'nope' is not a valid UUID"):
            await RfBadge.signin(badge="nope", password="pw")

    async def test_signup_refuses_a_zero_duration(self, no_connection: None) -> None:
        if sdk_decodes_zero_duration():
            pytest.skip("this surrealdb SDK reads a zero duration back: nothing to refuse")
        with pytest.raises(ValueError, match="zero duration"):
            await RfMember.signup(email="a@b.c", password="pw", trial=timedelta(0))


class TestRichAuthE2E:
    """Rich values reach the server natively through every auth path, on both lines, and the shared
    WebSocket stays up (before v0.23.0 a ``date`` variable dropped it)."""

    async def test_model_signup_stores_native_values_and_hydrates_declared_types(self) -> None:
        async with _auth_access(RfMember) as client:
            email = f"{UUID(int=1).hex}@example.test"
            result = await RfMember.signup(
                email=email,
                password=_AUTH_PASSWORD,
                bday=date(1990, 1, 2),
                trial=timedelta(days=14),
                credit=Decimal("1E+3"),
                joined="2026-01-01T00:00:00Z",
            )
            user = result.user
            assert type(user.bday) is date
            assert user.bday == date(1990, 1, 2)
            assert type(user.trial) is timedelta
            assert user.trial == timedelta(days=14)
            assert type(user.credit) is Decimal
            assert user.credit == Decimal(1000)
            assert type(user.joined) is datetime
            assert user.joined == _JAN

            row = await client.query("SELECT * FROM ONLY rf_member WHERE email = $email LIMIT 1;", {"email": email})
            assert row["bday"] == datetime(1990, 1, 2, tzinfo=UTC)
            _assert_sdk_duration(row["trial"], _TWO_WEEKS_NS)
            assert isinstance(row["credit"], Decimal)
            assert row["credit"] == Decimal(1000)
            assert isinstance(row["joined"], datetime)  # a datetime, not the string passed
            assert row["joined"] == _JAN

            again = await RfMember.signin(email=email, password=_AUTH_PASSWORD)
            assert again.user.get_id() == user.get_id()
            assert again.user.trial == timedelta(days=14)
            await _assert_client_usable(client)

    @_PROFILE_FORMS
    async def test_model_signup_writes_sub_models(self, profile: Any) -> None:
        """A sub-model holding a ``date`` is stored as an object, its ``bday`` a datetime — not a
        failed authentication (``no encoder for type``) — and hydrates back into the sub-model."""
        async with _auth_access(RfProfiled) as client:
            email = f"{UUID(int=3).hex}@example.test"
            result = await RfProfiled.signup(email=email, password=_AUTH_PASSWORD, profile=profile, history=[_PROFILE])
            _assert_profile(result.user.profile)
            assert len(result.user.history) == 1
            _assert_profile(result.user.history[0])

            row = await client.query("SELECT * FROM ONLY rf_profiled WHERE email = $email LIMIT 1;", {"email": email})
            _assert_stored_profile(row["profile"])
            assert len(row["history"]) == 1
            _assert_stored_profile(row["history"][0])

            again = await RfProfiled.signin(email=email, password=_AUTH_PASSWORD)
            assert again.user.get_id() == result.user.get_id()
            _assert_profile(again.user.profile)
            await _assert_client_usable(client)

    async def test_model_signin_validates_the_identifier(self) -> None:
        """A ``UUID`` column matched with the text of the UUID: a string never equals a uuid."""
        async with _auth_access(RfBadge) as client:
            created = await RfBadge.signup(badge=_TOKEN, password=_AUTH_PASSWORD)
            row = await client.query("SELECT * FROM ONLY rf_badge LIMIT 1;", {})
            assert type(row["badge"]) is UUID

            again = await RfBadge.signin(badge=str(_TOKEN), password=_AUTH_PASSWORD)
            assert again.user.get_id() == created.user.get_id()
            assert again.user.badge == _TOKEN

    async def test_connection_signup_keeps_the_shared_client(self) -> None:
        async with _auth_access(RfMember) as client:
            email = f"{UUID(int=2).hex}@example.test"
            tokens = await SurrealDBConnectionManager.signup(
                access=RfMember.get_access_name(),
                variables={
                    "email": email,
                    "password": _AUTH_PASSWORD,
                    "bday": date(1990, 1, 2),
                    "trial": timedelta(days=14),
                    "credit": Decimal("1E+3"),
                },
            )
            assert tokens.access
            await _assert_client_usable(client)

            record = await client.info()  # the record user's own row, as the SDK decodes it
            assert record["email"] == email
            assert record["bday"] == datetime(1990, 1, 2, tzinfo=UTC)
            _assert_sdk_duration(record["trial"], _TWO_WEEKS_NS)
            assert isinstance(record["credit"], Decimal)
            assert record["credit"] == Decimal(1000)
