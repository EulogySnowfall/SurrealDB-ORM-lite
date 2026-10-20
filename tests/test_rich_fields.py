"""Rich field types (v0.23.0) through the ORM: models whose fields hold durations, dates, sets,
ranges, decimals, UUIDs and bytes, written and read on both server lines.

Every class here is E2E (suffixed ``E2E``) unless it says otherwise. Tables are prefixed ``rf_``
and created inside ``orm_client(...)``, which drops them before and after the test.
"""

import ast
import asyncio
import contextlib
import copy
import dataclasses
import enum
import inspect
import re
import threading
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, Self
from uuid import UUID

import pydantic
import pytest
import surrealdb
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainValidator,
    ValidationError,
    WrapValidator,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic.dataclasses import dataclass as pydantic_dataclass
from surrealdb import RecordID
from surrealdb.cbor import undefined
from typing_extensions import TypedDict

from surreal_orm_lite import (
    AuthenticatedUserMixin,
    BaseSurrealEdge,
    BaseSurrealModel,
    BoundExcluded,
    BoundIncluded,
    BufferedTransaction,
    Computed,
    Count,
    Duration,
    Geometry,
    GeometryPoint,
    LiveAction,
    Q,
    Range,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    SurrealDbError,
    SurrealFunc,
    _sdk,
    computed,
    encoding,
    model_base,
)
from surreal_orm_lite._sdk import Bound, SurrealError
from surreal_orm_lite.live import hydrate
from surreal_orm_lite.signals import post_save
from surreal_orm_lite.testing import embedded_database
from surreal_orm_lite.utils import _KEEP_LIMIT, _KEEP_MAX_DEPTH, PLAIN_CONTENT, build_filter_condition, replace_content
from tests._readme_tables import assert_server_header_sentence, assert_server_rows
from tests._retry import retry_write
from tests.conftest import change_feed_database, orm_client
from tests.test_model_auth import AUTH_PASSWORD, _connect, _unique_email
from tests.test_rich_types import _http_client, _http_url, _server_version
from tests.test_transactions import _native_txn_supported

# --- shared fixtures (later tasks reuse them) ----------------------------------------------------


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
    durs: list[Duration] = []
    n: int = 0


#: One value of every rich kind, as a caller would write it.
FULL: dict[str, Any] = {
    "at": datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC),
    "day": date(2026, 1, 2),
    "ttl": timedelta(hours=1, minutes=30),
    "grace": Duration.parse("1h30m1ns"),
    "price": Decimal("12.50"),
    "token": UUID("0190c3d1-5c3b-7cc6-8f8a-2d2b1f0e9a11"),
    "hours": Range(BoundIncluded(9), BoundExcluded(17)),
    "period": Range(BoundIncluded(datetime(2026, 1, 1, tzinfo=UTC)), None),
    "blob": b"\x00\x01\xff",
    "tags": {"b", "a"},
    "durs": [Duration.parse("1h")],
    "n": 5,
}

#: A row written by the server from SurrealQL literals of every rich kind.
SEED = (
    "CREATE rf_event:s SET at = d'2026-01-02T03:04:05.123456789Z', day = d'2026-01-02T00:00:00Z', "
    "ttl = 1h30m1ns, grace = 1h30m1ns, price = 12.50dec, token = u'0190c3d1-5c3b-7cc6-8f8a-2d2b1f0e9a11', "
    "hours = 9..17, period = d'2026-01-01T00:00:00Z'.., blob = <bytes> 'abc', tags = ['a'], "
    "durs = [1h, 2h], n = 1;"
)


async def _raw(rid: RecordID) -> dict[str, Any]:
    """The stored row as the SDK reads it, bypassing the model."""
    client = await SurrealDBConnectionManager.get_client()
    row = await client.query("SELECT * FROM ONLY $rid;", {"rid": rid})
    assert isinstance(row, dict), row
    return row


#: How the SDK reads back each value of ``FULL`` once the server stored it natively.
_NATIVE: dict[str, Callable[[Any], bool]] = {
    "at": lambda v: type(v) is datetime and v.tzinfo is not None and v == FULL["at"],
    "day": lambda v: type(v) is datetime and v == datetime(2026, 1, 2, tzinfo=UTC),
    "ttl": lambda v: type(v) is surrealdb.Duration and v.elapsed == 5400 * 10**9,
    "grace": lambda v: type(v) is surrealdb.Duration and v.elapsed == 5400 * 10**9 + 1,
    "price": lambda v: type(v) is Decimal and v == Decimal("12.5"),  # value equality: 2.x reads 12.50
    "token": lambda v: type(v) is UUID and v == FULL["token"],
    "hours": lambda v: type(v) is surrealdb.Range and FULL["hours"] == v,
    "period": lambda v: type(v) is surrealdb.Range and v.end is None and v.begin.value == datetime(2026, 1, 1, tzinfo=UTC),
    "blob": lambda v: v == b"\x00\x01\xff",
    "tags": lambda v: v == ["a", "b"],
    "durs": lambda v: [type(d) for d in v] == [surrealdb.Duration] and v[0].elapsed == 3600 * 10**9,
    "n": lambda v: v == 5,
}


def _assert_native(row: dict[str, Any], keys: Collection[str] = tuple(FULL)) -> None:
    """*row*, written from ``FULL`` (or from its *keys* only), holds SurrealDB's native types as the SDK reads them."""
    wrong: dict[str, Any] = {}
    for key in keys:
        value = row.get(key, "<missing>")
        try:
            native = _NATIVE[key](value)
        except (AttributeError, TypeError, IndexError):
            native = False
        if not native:
            wrong[key] = value
    assert not wrong, f"not stored natively: {wrong!r}"


def _assert_declared(ev: RfEvent) -> None:
    """*ev* (read from ``SEED``) holds every field in its declared type."""
    assert type(ev.at) is datetime and ev.at.tzinfo is not None and ev.at.microsecond == 123456
    assert type(ev.day) is date and ev.day == date(2026, 1, 2)
    assert ev.ttl == timedelta(hours=1, minutes=30)
    assert type(ev.grace) is Duration and ev.grace == Duration.parse("1h30m1ns")
    assert ev.price == Decimal("12.5")  # value equality: 2.x reads 12.50
    assert type(ev.token) is UUID
    assert type(ev.hours) is Range and ev.hours == Range(BoundIncluded(9), BoundExcluded(17))
    assert ev.period is not None and ev.period.end is None
    assert ev.period.begin is not None and type(ev.period.begin.value) is datetime
    assert ev.blob == b"abc"
    assert type(ev.tags) is set and ev.tags == {"a"}
    assert ev.durs == [Duration.parse("1h"), Duration.parse("2h")]


class RfUser(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_user", access_name="rf_acct")

    id: str | None = None
    email: str
    password: str
    birthday: date | None = None
    grace: Duration = Duration.parse("7d")


@contextlib.asynccontextmanager
async def _auth_model(model: Any, scheme: str) -> AsyncIterator[Any]:
    """Define *model*'s access method over *scheme*, then remove it and its table as root.

    The removal runs on a fresh root client: a manager ``signup()`` leaves the shared
    connection signed in as the new record user, who may not remove anything.
    """
    _connect(scheme)
    try:
        await model.define_access()
        yield model
    finally:
        try:
            root = await _http_client()
            await root.query(f"REMOVE ACCESS IF EXISTS {model.get_access_name()} ON DATABASE;", {})
            await root.query(f"REMOVE TABLE IF EXISTS {model.get_table_name()};", {})
        finally:
            await SurrealDBConnectionManager.unset_connection()


async def _stored_user(email: str, table: str = "rf_user") -> dict[str, Any]:
    root = await _http_client()
    rows = await root.query(f"SELECT * FROM {table} WHERE email = $e;", {"e": email})
    assert isinstance(rows, list) and len(rows) == 1, rows
    row: dict[str, Any] = rows[0]
    return row


# --- the hand-off (spec §6.2) --------------------------------------------------------------------


class TestHandOffE2E:
    """AC-13/AC-15 (first case): a model save sends every rich value in its native form."""

    @pytest.mark.asyncio
    async def test_save_stores_native_types(self) -> None:
        async with orm_client("rf_event"):
            await RfEvent(id="a", **FULL).save()
            _assert_native(await _raw(RecordID("rf_event", "a")))


class TestAuthHandOffE2E:
    """AC-27 (first half): auth variables are encoded too, over WebSocket and HTTP."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("scheme", ["ws", "http"])
    async def test_signup_with_rich_fields(self, scheme: str) -> None:
        async with _auth_model(RfUser, scheme):
            email = _unique_email()
            result = await RfUser.signup(email=email, password=AUTH_PASSWORD, birthday=date(1990, 1, 2))
            user = result.user
            assert type(user.birthday) is date and user.birthday == date(1990, 1, 2)
            assert type(user.grace) is Duration and user.grace == Duration.parse("7d")

            row = await _stored_user(email)
            assert row["birthday"] == datetime(1990, 1, 2, tzinfo=UTC)
            assert type(row["grace"]) is surrealdb.Duration and row["grace"].elapsed == 7 * 86400 * 10**9

            again = await RfUser.signin(email=email, password=AUTH_PASSWORD)
            assert again.user.get_id() == user.get_id()
            assert again.user.birthday == date(1990, 1, 2)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("scheme", ["ws", "http"])
    async def test_manager_signup_variables(self, scheme: str) -> None:
        async with _auth_model(RfUser, scheme):
            email = _unique_email()
            tokens = await SurrealDBConnectionManager.signup(
                access="rf_acct",
                variables={"email": email, "password": AUTH_PASSWORD, "birthday": date(1990, 1, 3)},
                store=False,
            )
            assert tokens.access
            row = await _stored_user(email)
            assert row["birthday"] == datetime(1990, 1, 3, tzinfo=UTC)


# --- the write matrix (spec §6.2, AC-15) ---------------------------------------------------------


class RfLink(BaseSurrealEdge):
    model_config = SurrealConfigDict(table_name="rf_link")

    ttl: timedelta | None = None
    day: date | None = None


#: What a write case hands back: the record it wrote, and which ``FULL`` keys that record holds.
_Written = tuple[RecordID, Collection[str]]

_W = RecordID("rf_event", "w")
_A = RecordID("rf_event", "a")
_B = RecordID("rf_event", "b")
_EDGE_KEYS = ("ttl", "day")


async def _nodes() -> tuple[RfEvent, RfEvent]:
    """Two saved nodes for the edge cases."""
    a, b = RfEvent(id="a"), RfEvent(id="b")
    await a.save()
    await b.save()
    return a, b


async def _save_explicit_id() -> _Written:
    await RfEvent(id="w", **FULL).save()
    return _W, FULL


async def _save_auto_id() -> _Written:
    event = RfEvent(**FULL)
    await event.save()
    rid = event._record_id()
    assert rid is not None and rid.table_name == "rf_event"
    return rid, FULL


async def _update() -> _Written:
    await RfEvent(id="w").save()
    await RfEvent(id="w", **FULL).update()
    return _W, FULL


async def _upsert() -> _Written:
    await RfEvent(id="w", **FULL).upsert()
    return _W, FULL


async def _merge() -> _Written:
    event = RfEvent(id="w")
    await event.save()
    await event.merge(**FULL)
    return _W, FULL


async def _merge_server_values() -> _Written:
    """``UPDATE … SET`` with a server expression reading a bound duration, plus every literal."""
    event = RfEvent(id="w")
    await event.save()
    sent = datetime.now(UTC)
    await event.merge(
        server_values={"expires": SurrealFunc("time::now() + $ttl")},
        extra_vars={"ttl": timedelta(hours=1)},
        **FULL,
    )
    expires = (await _raw(_W))["expires"]
    assert type(expires) is datetime
    assert timedelta(minutes=50) < expires - sent < timedelta(minutes=70), expires  # $ttl arrived as 1h
    return _W, FULL


async def _patch() -> _Written:
    event = RfEvent(id="w", ttl=timedelta(minutes=1))
    await event.save()
    operations: list[dict[str, Any]] = [{"op": "replace", "path": "/ttl", "value": FULL["ttl"]}]
    operations += [{"op": "add", "path": f"/{key}", "value": value} for key, value in FULL.items() if key != "ttl"]
    await event.patch(operations)
    return _W, FULL


async def _atomic_append() -> _Written:
    event = RfEvent(id="w", durs=[Duration.parse("1h")])
    await event.save()
    await event.atomic_append("durs", Duration.parse("2h"))
    durs = (await _raw(_W))["durs"]
    assert [type(d) for d in durs] == [surrealdb.Duration] * 2
    assert [d.elapsed for d in durs] == [3600 * 10**9, 7200 * 10**9]
    return _W, ()


async def _bulk_create() -> _Written:
    [created] = await RfEvent.objects().bulk_create([RfEvent(id="w", **FULL)])
    # The returned rows are hydrated through the model: the read rule gives ``ttl`` back as a timedelta.
    assert isinstance(created, RfEvent)
    assert type(created.ttl) is timedelta and created.ttl == FULL["ttl"]
    assert type(created.day) is date and created.day == FULL["day"]
    return _W, FULL


async def _bulk_update() -> _Written:
    await RfEvent(id="w", n=5).save()
    await RfEvent(id="other", n=6).save()
    changes = {key: value for key, value in FULL.items() if key != "n"}
    assert await RfEvent.objects().filter(n=5).bulk_update(**changes) == 1
    return _W, FULL


async def _get_or_create() -> _Written:
    _, created = await RfEvent.objects().get_or_create(id="w", defaults=FULL)
    assert created
    return _W, FULL


async def _update_or_create() -> _Written:
    _, created = await RfEvent.objects().update_or_create(id="w", defaults=FULL)
    assert created
    return _W, FULL


async def _edge_save() -> _Written:
    await _nodes()
    await RfLink(id="w", in_=_A, out=_B, ttl=FULL["ttl"], day=FULL["day"]).save()
    return RecordID("rf_link", "w"), _EDGE_KEYS


async def _edge_bulk_create() -> _Written:
    await _nodes()
    [created] = await RfLink.objects().bulk_create([RfLink(id="w", in_=_A, out=_B, ttl=FULL["ttl"], day=FULL["day"])])
    assert isinstance(created, RfLink)
    assert type(created.ttl) is timedelta and created.ttl == FULL["ttl"]
    assert type(created.day) is date and created.day == FULL["day"]
    return RecordID("rf_link", "w"), _EDGE_KEYS


async def _relate() -> _Written:
    a, b = await _nodes()
    rows = await a.relate("rf_link", b, data={"ttl": FULL["ttl"], "day": FULL["day"]})
    assert isinstance(rows, list) and len(rows) == 1, rows
    return rows[0]["id"], _EDGE_KEYS


async def _raw_query() -> _Written:
    await RfEvent.raw_query("UPSERT $rid CONTENT $c;", variables={"rid": _W, "c": FULL})
    return _W, FULL


async def _queryset_query() -> _Written:
    # QuerySet.query() requires the statement to name its table with FROM.
    await RfEvent(id="w", n=7).save()
    await RfEvent.objects().query(
        "UPDATE rf_event MERGE $c WHERE id IN (SELECT VALUE id FROM rf_event WHERE n = $n) RETURN NONE;",
        variables={"c": FULL, "n": 7},
    )
    return _W, FULL


async def _interactive_transaction() -> _Written:
    if not await _native_txn_supported():
        pytest.skip("requires SurrealDB 3.x native transactions")
    async with SurrealDBConnectionManager.transaction() as tx:
        assert tx.is_interactive
        await RfEvent(id="w", **FULL).save(tx=tx)
    return _W, FULL


_WRITE_CASES: dict[str, Callable[[], Awaitable[_Written]]] = {
    "save-explicit-id": _save_explicit_id,
    "save-auto-id": _save_auto_id,
    "update": _update,
    "upsert": _upsert,
    "merge": _merge,
    "merge-server-values": _merge_server_values,
    "patch": _patch,
    "atomic-append": _atomic_append,
    "bulk-create": _bulk_create,
    "bulk-update": _bulk_update,
    "get-or-create": _get_or_create,
    "update-or-create": _update_or_create,
    "edge-save": _edge_save,
    "edge-bulk-create": _edge_bulk_create,
    "relate-data": _relate,
    "raw-query": _raw_query,
    "queryset-query": _queryset_query,
    "interactive-transaction": _interactive_transaction,
}


class TestWriteMatrixE2E:
    """AC-15: every write API sends rich values in their native form (WebSocket unless named)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", list(_WRITE_CASES))
    async def test_write_api_stores_native_types(self, case: str) -> None:
        async with orm_client("rf_event", "rf_link"):
            rid, keys = await _WRITE_CASES[case]()
            _assert_native(await _raw(rid), keys)

    @pytest.mark.asyncio
    async def test_call_function_args(self) -> None:
        async with orm_client("rf_event") as client:
            await client.query("REMOVE FUNCTION IF EXISTS fn::rf_add;", {})
            await client.query("DEFINE FUNCTION fn::rf_add($d: duration) { RETURN $d + 1h; };", {})
            try:
                out = await SurrealDBConnectionManager.call_function("fn::rf_add", [timedelta(minutes=30)])
                assert type(out) is surrealdb.Duration and out.elapsed == 5400 * 10**9
            finally:
                await client.query("REMOVE FUNCTION IF EXISTS fn::rf_add;", {})

    @pytest.mark.asyncio
    async def test_buffered_transaction_over_http(self) -> None:
        root = await _http_client()
        await root.query("REMOVE TABLE IF EXISTS rf_event;", {})
        SurrealDBConnectionManager.set_connection(_http_url(), "root", "root", "ns", "db")
        try:
            async with SurrealDBConnectionManager.transaction() as tx:
                assert not tx.is_interactive
                await RfEvent(id="w", **FULL).save(tx=tx)
            _assert_native(await _raw(_W))
        finally:
            await root.query("REMOVE TABLE IF EXISTS rf_event;", {})
            await SurrealDBConnectionManager.unset_connection()


# --- refusals (spec §5.7, AC-11, D11) ------------------------------------------------------------

_NEG = timedelta(seconds=-1)

#: Each instance holds one value the boundary refuses; ``model_construct`` skips the validation the
#: ORM's own types would apply first, so the value reaches ``encode`` as a caller could build it.
_REFUSED_SAVES: dict[str, tuple[Callable[[], RfEvent], str]] = {
    "negative-timedelta": (lambda: RfEvent.model_construct(id="bad", ttl=_NEG), "negative duration"),
    "zero-timedelta": (lambda: RfEvent(id="bad", ttl=timedelta(0)), "zero duration"),
    "sdk-duration-2**64-seconds": (
        lambda: RfEvent.model_construct(id="bad", grace=surrealdb.Duration(2**64 * 10**9)),
        re.escape("at most 2**64 - 1 seconds"),
    ),
    "decimal-above-2**96": (lambda: RfEvent(id="bad", price=Decimal("79228162514264337593543950336")), re.escape("2**96 - 1")),
    "decimal-exponent-above-2**96": (lambda: RfEvent(id="bad", price=Decimal("8E+28")), re.escape("2**96 - 1")),
    "int-above-int64": (lambda: RfEvent(id="bad", n=2**63), "signed 64-bit"),
    "int-below-int64": (lambda: RfEvent(id="bad", n=-(2**63) - 1), "signed 64-bit"),
    "range-text-side": (lambda: RfEvent.model_construct(id="bad", hours=surrealdb.Range("x", undefined)), "Range bound"),
    "negative-sdk-duration": (lambda: RfEvent.model_construct(id="bad", grace=surrealdb.Duration(-1)), "negative duration"),
}


async def _assert_client_untouched(client: Any) -> None:
    """The shared client survived the refusal: same object, still answering."""
    assert (await SurrealDBConnectionManager.get_client()) is client
    assert await client.query("RETURN 1;", {}) == 1


async def _refused_by_manager_signin() -> None:
    await SurrealDBConnectionManager.signin(access="rf_acct", variables={"email": "x@y.z", "g": _NEG})


async def _refused_in_transaction() -> None:
    async with SurrealDBConnectionManager.transaction() as tx:  # interactive on 3.x, buffered on 2.x
        await SurrealDBConnectionManager.call_function("fn::rf_add", [_NEG], tx=tx)


_REFUSED_PATHS: dict[str, Callable[[], Awaitable[Any]]] = {
    "call-function": lambda: SurrealDBConnectionManager.call_function("fn::rf_add", [_NEG]),
    "call-function-in-transaction": _refused_in_transaction,
    "manager-signin": _refused_by_manager_signin,
    "model-signup": lambda: RfUser.signup(email=_unique_email(), password=AUTH_PASSWORD, grace=surrealdb.Duration(-1)),
}


class TestRefusalsE2E:
    """AC-11: a refused value is a plain ``ValueError`` raised before anything is sent, on every path."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", list(_REFUSED_SAVES))
    async def test_refusals_leave_the_client_untouched(self, case: str, monkeypatch: pytest.MonkeyPatch) -> None:
        build, message = _REFUSED_SAVES[case]
        if case == "zero-timedelta":
            # The ORM reads a zero duration back since #235: the refusal is for an SDK nothing reads it with.
            monkeypatch.setattr(encoding, "reads_zero_duration", lambda: False)
        async with orm_client("rf_event") as client:
            await RfEvent(id="witness").save()
            with pytest.raises(ValueError, match=message) as excinfo:
                await build().save()
            assert type(excinfo.value) is ValueError, f"re-wrapped into {type(excinfo.value).__name__}"
            await _assert_client_untouched(client)
            assert await client.query("SELECT VALUE id FROM rf_event;", {}) == [RecordID("rf_event", "witness")]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", list(_REFUSED_PATHS))
    async def test_other_paths_refuse_with_value_error(self, path: str) -> None:
        async with _auth_model(RfUser, "ws"):
            client = await SurrealDBConnectionManager.get_client()
            with pytest.raises(ValueError, match="negative duration") as excinfo:
                await _REFUSED_PATHS[path]()
            assert type(excinfo.value) is ValueError, f"re-wrapped into {type(excinfo.value).__name__}"
            await _assert_client_untouched(client)
            assert await RfUser.objects().count() == 0


# --- atomic_increment with a duration (spec §6.6, AC-16, D4) -------------------------------------

_DURATION_AMOUNTS = [timedelta(minutes=30), surrealdb.Duration(1), Duration(1)]
_GUARD_MESSAGE = re.escape("Set the new value with merge(ttl=…) instead.")


class TestAtomicIncrementGuard:
    """D4: a duration amount is refused before any call — SurrealDB 2.x ignores ``+=`` with one."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("amount", _DURATION_AMOUNTS, ids=["timedelta", "sdk-duration", "orm-duration"])
    async def test_duration_amount_raises_before_any_call(self, amount: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        async def no_client() -> Any:
            raise AssertionError("no client may be acquired")

        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", staticmethod(no_client))
        with pytest.raises(TypeError, match=_GUARD_MESSAGE):
            await RfEvent(id="g").atomic_increment("ttl", amount)

        tx = BufferedTransaction(client=None)
        with pytest.raises(TypeError, match=_GUARD_MESSAGE):
            await RfEvent(id="g").atomic_increment("ttl", amount, tx=tx)
        assert tx.statements == [] and tx.variables == {}


class TestAtomicIncrementGuardE2E:
    """AC-16: the row is untouched by a refused increment; numbers still add on both lines."""

    @pytest.mark.asyncio
    async def test_row_unchanged_and_numbers_still_add(self) -> None:
        async with orm_client("rf_event"):
            event = RfEvent(id="g", ttl=timedelta(hours=1), grace=Duration.parse("1h"), price=Decimal("12.50"), n=5)
            await event.save()
            rid = RecordID("rf_event", "g")
            before = await _raw(rid)
            for field, amount in [("ttl", timedelta(minutes=30)), ("ttl", surrealdb.Duration(1)), ("grace", Duration(1))]:
                with pytest.raises(TypeError, match=re.escape(f"merge({field}=…) instead")):
                    await event.atomic_increment(field, amount)
            assert await _raw(rid) == before

            await event.atomic_increment("n", 2)
            await event.atomic_increment("price", Decimal("2.25"))
            after = await _raw(rid)
            assert after["n"] == 7 and event.n == 7
            assert type(after["price"]) is Decimal and after["price"] == Decimal("14.75")
            assert after["ttl"] == before["ttl"] and after["grace"] == before["grace"]


# --- the read path (spec §6.3, AC-17 … AC-21, AC-27 second half; D1) ----------------------------


class _SubTimedelta(timedelta):
    """A ``timedelta`` subclass: not exactly ``timedelta``, so no read rule applies to its field."""


class RfKinds(BaseSurrealModel):
    """Every annotation shape the read rules must recognise — or leave alone."""

    model_config = SurrealConfigDict(table_name="rf_plain", arbitrary_types_allowed=True)

    id: str | None = None
    ttl: Optional[Annotated[timedelta, Field(ge=timedelta(0))]] = None  # noqa: UP045 (typing.Union on purpose)
    pause: timedelta | None = Field(default=None, alias="pauseCol")
    grace: Duration | None = Field(default=None, alias="graceTime")
    computed_ttl: Computed[timedelta] = computed("1h")
    either: timedelta | str | None = None
    many: list[timedelta] = []
    by_key: dict[str, timedelta] = {}
    sub: _SubTimedelta | None = None
    window: Range[timedelta] | None = None


class RfPlain(BaseSurrealModel):
    """A model without any rich field: both read hooks are the identity on it (AC-20)."""

    model_config = SurrealConfigDict(table_name="rf_plain")

    id: str | None = None
    name: str = ""
    tags: list[str] = []
    meta: dict[str, Any] = {}


class RfCf(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_cf")

    id: str | None = None
    ttl: timedelta | None = None


class RfUserTd(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_user_td", access_name="rf_acct_td")

    id: str | None = None
    email: str
    password: str
    trial: timedelta = timedelta(days=7)


class RfNan(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_nan")

    id: str | None = None
    price: Annotated[Decimal, Field(allow_inf_nan=True)]


class RfArr(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_arr")

    id: str | None = None
    tags: set[str] = set()


#: A duration beyond ``timedelta.max`` that SurrealDB still stores (its maximum is ``584942417355y3w5d…``).
_BEYOND_TIMEDELTA = "584942417355y"
_SDK_BEYOND_TIMEDELTA = surrealdb.Duration(86400 * 10**18)  # 10**9 days: 1 µs past timedelta.max


#: The database of this module's change-feed tables: never ``ns``/``db`` (see ``change_feed_database``).
RF_CF_DATABASE = "rf_change_feed"


async def _take(stream: Any, count: int, timeout: float = 10.0) -> list[Any]:
    """The next *count* events of a live or change-feed stream, under ``asyncio.timeout``."""
    events: list[Any] = []
    async with asyncio.timeout(timeout):
        while len(events) < count:
            events.append(await anext(stream))
    return events


class TestReadKinds:
    """§6.3 item 4: the fields the read rules apply to, keyed by Python name and by column."""

    def test_shared_model(self) -> None:
        assert RfEvent._read_kinds() == {"ttl": "timedelta", "grace": "duration"}

    def test_unwrapping_aliases_and_refused_shapes(self) -> None:
        # Optional[Annotated[...]] and Computed[...] unwrap; an alias adds the column. Unions with another
        # type, containers, a subclass and Range[timedelta] get no entry.
        assert RfKinds._read_kinds() == {
            "ttl": "timedelta",
            "pause": "timedelta",
            "pauseCol": "timedelta",
            "grace": "duration",
            "graceTime": "duration",
            "computed_ttl": "timedelta",
        }

    def test_edge_model(self) -> None:
        assert RfLink._read_kinds() == {"ttl": "timedelta"}

    def test_model_without_rich_fields(self) -> None:
        assert RfPlain._read_kinds() == {}

    def test_cached_per_class(self) -> None:
        assert RfEvent._read_kinds() is RfEvent._read_kinds()

        class RfEventChild(RfEvent):
            pause: timedelta | None = None

        assert RfEventChild._read_kinds() == {"ttl": "timedelta", "grace": "duration", "pause": "timedelta"}
        assert RfEvent._read_kinds() == {"ttl": "timedelta", "grace": "duration"}


class TestSetDataRule:
    """§6.3 item 2: an SDK ``Duration`` in a top-level ``timedelta`` field becomes a ``timedelta``."""

    def test_sdk_duration_becomes_a_floored_timedelta(self) -> None:
        data = {"id": "x", "ttl": surrealdb.Duration(5400 * 10**9 + 999)}
        before = dict(data)
        out = RfEvent.set_data(data)
        assert type(out["ttl"]) is timedelta and out["ttl"] == timedelta(hours=1, minutes=30)
        assert out is not data, "the dict is copied on the first change"
        assert data == before and type(data["ttl"]) is surrealdb.Duration, "the caller's dict is not mutated"

    def test_column_key(self) -> None:
        out = RfKinds.set_data({"pauseCol": surrealdb.Duration(60 * 10**9)})
        assert out == {"pauseCol": timedelta(minutes=1)}
        row = {"id": RecordID("rf_plain", "k"), "pauseCol": surrealdb.Duration(60 * 10**9)}
        kinds = RfKinds.from_db(row)
        assert isinstance(kinds, RfKinds) and kinds.pause == timedelta(minutes=1)

    @pytest.mark.parametrize(
        "value",
        ["1h", Duration(5), _SDK_BEYOND_TIMEDELTA, surrealdb.Duration(-1), surrealdb.Duration(1.5), None],
        ids=["text", "orm-duration", "beyond-timedelta-max", "negative", "float-elapsed", "none"],
    )
    def test_other_values_are_left_as_received(self, value: Any) -> None:
        data = {"ttl": value}
        out = RfEvent.set_data(data)
        assert out is data and out["ttl"] is value

    def test_duration_fields_are_left_to_their_validator(self) -> None:
        data = {"grace": surrealdb.Duration(5), "durs": [surrealdb.Duration(1)]}
        assert RfEvent.set_data(data) is data

    def test_model_without_rich_fields_is_the_identity(self) -> None:
        data = {"id": "p", "name": "n", "tags": ["a"], "meta": {"d": surrealdb.Duration(1)}}
        assert RfPlain.set_data(data) is data
        assert data["meta"] == {"d": surrealdb.Duration(1)}

    def test_not_a_dict(self) -> None:
        event = RfEvent(id="x")
        assert RfEvent.set_data(event) is event


class TestApplyRecordRule:
    """§6.3 item 3 (D1): top-level durations are converted; since #261 every other field takes the value a read gives
    (a ``date``, a set, the ORM's ``Range`` and durations…), the row itself unchanged."""

    def test_durations_converted_other_values_validated(self) -> None:
        event = RfEvent(id="x")
        day = datetime(2026, 1, 2, tzinfo=UTC)
        tags = ["a"]
        hours = surrealdb.Range(BoundIncluded(9), BoundExcluded(17))
        durs = [surrealdb.Duration(1)]
        event._apply_record(
            {
                "id": RecordID("rf_event", "x"),
                "ttl": surrealdb.Duration(5400 * 10**9 + 999),
                "grace": surrealdb.Duration(5400 * 10**9 + 1),
                "day": day,
                "tags": tags,
                "hours": hours,
                "durs": durs,
            }
        )
        assert type(event.ttl) is timedelta and event.ttl == timedelta(hours=1, minutes=30)
        assert type(event.grace) is Duration and event.grace.elapsed == 5400 * 10**9 + 1
        assert type(event.day) is date and event.day == date(2026, 1, 2)
        assert type(event.tags) is set and event.tags == {"a"}
        assert type(event.hours) is Range and event.hours == Range(BoundIncluded(9), BoundExcluded(17))
        assert [type(d) for d in event.durs] == [Duration] and event.durs == [Duration(1)]
        assert day == datetime(2026, 1, 2, tzinfo=UTC) and tags == ["a"], "the row is not changed"
        assert [type(d) for d in durs] == [surrealdb.Duration]

    def test_out_of_range_values_are_kept(self) -> None:
        event = RfEvent(id="x")
        negative = surrealdb.Duration(-1)
        event._apply_record({"ttl": _SDK_BEYOND_TIMEDELTA, "grace": negative})
        assert event.ttl is _SDK_BEYOND_TIMEDELTA and event.grace is negative

    def test_only_still_filters(self) -> None:
        event = RfEvent(id="x", ttl=timedelta(minutes=1))
        event._apply_record({"ttl": surrealdb.Duration(3600 * 10**9), "grace": surrealdb.Duration(1)}, only={"grace"})
        assert event.ttl == timedelta(minutes=1) and event.grace == Duration(1)

    def test_column_key(self) -> None:
        kinds = RfKinds(id="k")
        kinds._apply_record({"pauseCol": surrealdb.Duration(60 * 10**9), "graceTime": surrealdb.Duration(7)})
        assert kinds.pause == timedelta(minutes=1) and type(kinds.grace) is Duration and kinds.grace.elapsed == 7

    def test_model_without_rich_fields_sets_the_row_values(self) -> None:
        plain = RfPlain(id="p")
        row = {"id": RecordID("rf_plain", "p"), "name": "n", "tags": ["a"], "meta": {"d": surrealdb.Duration(1)}}
        plain._apply_record(row)
        assert plain.id is row["id"], "the id stays the row's RecordID"
        # #261: validated as a read validates them, on a copy: equal values, the row's objects left alone.
        assert (plain.name, plain.tags, plain.meta) == ("n", ["a"], {"d": surrealdb.Duration(1)})
        assert plain.tags is not row["tags"] and plain.meta is not row["meta"]


class TestHydrateNeverRaises:
    """AC-19: a duration a ``timedelta`` cannot hold leaves the instance unvalidated, with the error."""

    def test_out_of_range_timedelta(self) -> None:
        instance, error = hydrate(RfEvent, {"id": RecordID("rf_event", "x"), "ttl": _SDK_BEYOND_TIMEDELTA})
        assert isinstance(instance, RfEvent) and isinstance(error, ValidationError)
        assert [e["loc"] for e in error.errors()] == [("ttl",)]
        assert instance.id == "x" and instance.ttl is _SDK_BEYOND_TIMEDELTA

    def test_valid_row(self) -> None:
        instance, error = hydrate(RfEvent, {"id": RecordID("rf_event", "y"), "ttl": surrealdb.Duration(60 * 10**9)})
        assert error is None and type(instance.ttl) is timedelta and instance.ttl == timedelta(minutes=1)


class TestHydrateNeverRaisesE2E:
    """AC-19: a live stream and a change feed both carry on past such a row."""

    @pytest.mark.asyncio
    async def test_live_stream_goes_on(self) -> None:
        async with orm_client("rf_event") as client:
            await client.query("DEFINE TABLE rf_event SCHEMALESS;", {})
            async with RfEvent.objects().live() as stream:
                await client.query(f"CREATE rf_event:big SET ttl = {_BEYOND_TIMEDELTA};", {})
                await client.query("CREATE rf_event:ok SET ttl = 1h;", {})
                big, ok = await _take(stream, 2)
        assert big.record_id == "rf_event:big" and isinstance(big.validation_error, ValidationError)
        assert type(big.instance.ttl) is surrealdb.Duration
        assert ok.record_id == "rf_event:ok" and ok.validation_error is None
        assert type(ok.instance.ttl) is timedelta and ok.instance.ttl == timedelta(hours=1)

    @pytest.mark.asyncio
    async def test_change_feed_moves_its_cursor_past_it(self) -> None:
        async with change_feed_database(RF_CF_DATABASE) as client:
            await client.query("DEFINE TABLE rf_cf SCHEMALESS CHANGEFEED 1h;", {})
            stream = await RfCf.objects().changes(poll_interval=0.05).start()
            try:
                await retry_write(lambda: client.query(f"CREATE rf_cf:big SET ttl = {_BEYOND_TIMEDELTA};", {}))
                await retry_write(lambda: client.query("CREATE rf_cf:ok SET ttl = 1h;", {}))
                [big] = await _take(stream, 1)
                past_big = stream.cursor
                [ok] = await _take(stream, 1)
            finally:
                stream.stop()
            assert big.record_id == "rf_cf:big" and isinstance(big.validation_error, ValidationError)
            assert ok.record_id == "rf_cf:ok" and ok.validation_error is None
            assert type(ok.instance.ttl) is timedelta

            assert isinstance(past_big, int)
            resumed = RfCf.objects().changes(since=past_big, poll_interval=0.05)
            try:
                [again] = await _take(resumed, 1)
            finally:
                resumed.stop()
            assert again.record_id == "rf_cf:ok", "resuming from the cursor replayed the refused entry"


async def _read_get(client: Any) -> Any:
    await client.query(SEED, {})
    return await RfEvent.objects().get("s")


async def _read_filter_exec(client: Any) -> Any:
    await client.query(SEED, {})
    rows = await RfEvent.objects().filter(n=1).exec()
    assert len(rows) == 1, rows
    return rows[0]


async def _read_all(client: Any) -> Any:
    await client.query(SEED, {})
    rows = await RfEvent.objects().all()
    assert len(rows) == 1, rows
    return rows[0]


async def _read_first(client: Any) -> Any:
    await client.query(SEED, {})
    return await RfEvent.objects().first()


async def _read_get_or_create(client: Any) -> Any:
    await client.query(SEED, {})
    event, created = await RfEvent.objects().get_or_create(id="s", defaults={})
    assert not created
    return event


async def _read_raw_query(client: Any) -> Any:
    await client.query(SEED, {})
    rows = await RfEvent.raw_query("SELECT * FROM rf_event:s;")
    assert len(rows) == 1, rows
    return rows[0]


async def _read_in_transaction(client: Any) -> Any:
    if not await _native_txn_supported():
        pytest.skip("requires SurrealDB 3.x native transactions")
    await client.query(SEED, {})
    async with SurrealDBConnectionManager.transaction() as tx:
        return await RfEvent.objects(tx=tx).get("s")


async def _read_live_event(client: Any) -> Any:
    await client.query("DEFINE TABLE rf_event SCHEMALESS;", {})
    async with RfEvent.objects().live() as stream:
        await client.query(SEED, {})
        [event] = await _take(stream, 1)
    assert event.validation_error is None, event.validation_error
    assert type(event.raw["ttl"]) is surrealdb.Duration and type(event.raw["grace"]) is surrealdb.Duration
    return event.instance


async def _read_change_feed(client: Any) -> Any:
    # In ``RF_CF_DATABASE``, never ``ns``/``db``: see ``_read_context``.
    await client.query("DEFINE TABLE rf_event SCHEMALESS CHANGEFEED 1h;", {})
    stream = await RfEvent.objects().changes(poll_interval=0.05).start()
    try:
        await retry_write(lambda: client.query(SEED, {}))
        [event] = await _take(stream, 1)
    finally:
        stream.stop()
    assert event.validation_error is None, event.validation_error
    assert type(event.raw["ttl"]) is surrealdb.Duration and type(event.raw["grace"]) is surrealdb.Duration
    return event.instance


_READ_CASES: dict[str, Callable[[Any], Awaitable[Any]]] = {
    "get": _read_get,
    "filter-exec": _read_filter_exec,
    "all": _read_all,
    "first": _read_first,
    "get-or-create-existing": _read_get_or_create,
    "raw-query": _read_raw_query,
    "transaction-get": _read_in_transaction,
    "live-event": _read_live_event,
    "change-feed-entry": _read_change_feed,
}


def _read_context(case: str) -> contextlib.AbstractAsyncContextManager[Any]:
    """The client a read case runs on: a change feed in a database of its own, the rest in ``ns``/``db``."""
    if case == "change-feed-entry":
        return change_feed_database(RF_CF_DATABASE)
    return orm_client("rf_event")


class TestReadMatrixE2E:
    """AC-17: a row the server wrote from literals hydrates the declared types on every read path."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", list(_READ_CASES))
    async def test_read_path_returns_declared_types(self, case: str) -> None:
        async with _read_context(case) as client:
            event = await _READ_CASES[case](client)
            assert isinstance(event, RfEvent), event
            _assert_declared(event)

    @pytest.mark.asyncio
    async def test_edge_model_get(self) -> None:
        async with orm_client("rf_event", "rf_link") as client:
            await client.query("RELATE rf_event:a->rf_link:l->rf_event:b SET ttl = 1h, day = d'2026-01-02T00:00:00Z';", {})
            link = await RfLink.objects().get("l")
        assert isinstance(link, RfLink)
        assert type(link.ttl) is timedelta and link.ttl == timedelta(hours=1)
        assert type(link.day) is date and link.day == date(2026, 1, 2)

    @pytest.mark.asyncio
    async def test_grouped_values_keep_sdk_values(self) -> None:
        async with orm_client("rf_event") as client:
            await client.query(SEED, {})
            rows = await RfEvent.objects().values("ttl", "grace").annotate(count=Count("id")).exec()
        assert len(rows) == 1, rows
        [row] = rows
        assert type(row["ttl"]) is surrealdb.Duration and row["ttl"].elapsed == 5400 * 10**9 + 1
        assert type(row["grace"]) is surrealdb.Duration and row["grace"].elapsed == 5400 * 10**9 + 1
        assert row["count"] == 1


#: Every ``FULL`` key that a write-back must keep native (``n`` and ``durs`` change in some cases).
_WRITE_BACK_KEYS = tuple(key for key in FULL if key not in ("n", "durs"))


async def _saved(**extra: Any) -> RfEvent:
    event = RfEvent(id="w", **FULL, **extra)
    await event.save()
    return event


async def _apply_save_without_id() -> RfEvent:
    event = RfEvent(**FULL)
    await event.save()
    return event


async def _apply_refresh() -> RfEvent:
    event = await _saved()
    await event.refresh()
    return event


async def _apply_merge() -> RfEvent:
    event = await _saved()
    await event.merge(n=2)
    return event


async def _apply_upsert() -> RfEvent:
    event = RfEvent(id="w", **FULL)
    await event.upsert()
    return event


async def _apply_patch() -> RfEvent:
    event = await _saved()
    await event.patch([{"op": "replace", "path": "/n", "value": 2}])
    return event


async def _apply_atomic_append() -> RfEvent:
    event = await _saved()
    await event.atomic_append("durs", Duration(1))
    return event


_APPLY_CASES: dict[str, Callable[[], Awaitable[RfEvent]]] = {
    "save-without-id": _apply_save_without_id,
    "refresh": _apply_refresh,
    "merge": _apply_merge,
    "upsert": _apply_upsert,
    "patch": _apply_patch,
    "atomic-append": _apply_atomic_append,
}


class TestApplyRecordE2E:
    """AC-18 (D1): rows applied without validation keep durations, so a write-back stays native."""

    # #261: ``day``, ``tags``, ``hours`` and ``period`` hold the type a read gives after a returned row is applied
    # (before, the SDK's midnight datetime, list and Range, and Pydantic warned when ``update()`` serialised them).
    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", list(_APPLY_CASES))
    async def test_returned_rows_keep_durations(self, case: str) -> None:
        async with orm_client("rf_event"):
            event = await _APPLY_CASES[case]()
            assert type(event.ttl) is timedelta and event.ttl == FULL["ttl"]
            assert type(event.grace) is Duration and event.grace == FULL["grace"]
            assert type(event.day) is date and event.day == FULL["day"]
            assert type(event.tags) is set and event.tags == FULL["tags"]
            assert type(event.hours) is Range and event.hours == FULL["hours"]
            assert type(event.period) is Range and event.period == FULL["period"]

            with warnings.catch_warnings():
                warnings.simplefilter("error", UserWarning)  # no PydanticSerializationUnexpectedValue any more
                await event.update()  # the §3.7 regression: durations written back as {elapsed: …}
            rid = event._record_id()
            assert rid is not None
            row = await _raw(rid)
            assert type(row["ttl"]) is surrealdb.Duration and type(row["grace"]) is surrealdb.Duration, row
            _assert_native(row, _WRITE_BACK_KEYS)


# --- sub-models after a write that returns the row (#228) ----------------------------------------


class RfSub(BaseModel):
    """A sub-model holding rich values (``Duration``, ``timedelta``, ``Range``, geometry): a write sync keeps them the ORM's."""

    ttl: Duration
    hours: Range[int]
    period: Range[datetime] | None = None
    td: timedelta | None = None
    where: GeometryPoint | None = None
    label: str = ""


@dataclasses.dataclass
class RfSubData:
    """The same, as a stdlib dataclass."""

    ttl: Duration
    hours: Range[int]
    td: timedelta


class RfSubDict(TypedDict):
    """The same, as a ``TypedDict``: it validates into a ``dict`` holding the ORM's values."""

    ttl: Duration
    td: timedelta


class RfPlainSub(BaseModel):
    """A sub-model without rich values: it comes back an instance too, as after ``get()``."""

    name: str
    n: int = 0


class RfHolder(BaseSurrealModel):
    """Every shape a sub-model field takes: alone, optional, in a list, in a dict, as a dataclass or a ``TypedDict``."""

    model_config = SurrealConfigDict(table_name="rf_holder")

    id: str | None = None
    sub: RfSub
    maybe: RfSub | None = None
    subs: list[RfSub] = []
    by_key: dict[str, RfSub] = {}
    data: RfSubData | None = None
    datas: list[RfSubData] = []
    typed: RfSubDict | None = None
    plain: RfPlainSub | None = None
    many: list[Duration] = []
    tds: list[timedelta] = []
    n: int = 0


#: The sub-model every ``RfHolder`` field holds.
SUB = RfSub(
    ttl=Duration.parse("1h30m1ns"),
    hours=Range(BoundIncluded(9), BoundExcluded(17)),
    period=Range(BoundIncluded(datetime(2026, 1, 1, tzinfo=UTC)), None),
    td=timedelta(minutes=5, microseconds=1),
    where=GeometryPoint(-73.57, 45.5),
    label="s",
)
#: One value per ``RfHolder`` field.
HOLDER: dict[str, Any] = {
    "sub": SUB,
    "maybe": SUB,
    "subs": [SUB, SUB.model_copy(update={"label": "t"})],
    "by_key": {"a": SUB},
    "data": RfSubData(ttl=Duration(7), hours=Range(BoundIncluded(1), None), td=timedelta(seconds=3)),
    "datas": [RfSubData(ttl=Duration.parse("2m"), hours=Range(None, BoundExcluded(3)), td=timedelta(hours=2))],
    "typed": {"ttl": Duration(9), "td": timedelta(seconds=9)},
    "plain": RfPlainSub(name="p", n=1),
    "many": [Duration.parse("2m")],
    "tds": [timedelta(seconds=90), timedelta(microseconds=1)],
}


def _assert_sub_models(holder: Any) -> None:
    """Every sub-model field of ``holder`` holds its declared type, with the ORM's rich values, as ``HOLDER``."""
    assert isinstance(holder, RfHolder), holder
    assert type(holder.sub) is RfSub and holder.sub == SUB, holder.sub
    assert (type(holder.sub.ttl), type(holder.sub.hours), type(holder.sub.period)) == (Duration, Range, Range)
    assert (type(holder.sub.td), type(holder.sub.where)) == (timedelta, GeometryPoint), holder.sub
    assert type(holder.maybe) is RfSub and holder.maybe == SUB, holder.maybe
    assert [type(sub) for sub in holder.subs] == [RfSub, RfSub] and holder.subs == HOLDER["subs"], holder.subs
    assert type(holder.by_key["a"]) is RfSub and holder.by_key == HOLDER["by_key"], holder.by_key
    assert type(holder.data) is RfSubData and holder.data == HOLDER["data"], holder.data
    assert (type(holder.data.ttl), type(holder.data.hours), type(holder.data.td)) == (Duration, Range, timedelta)
    assert [type(data) for data in holder.datas] == [RfSubData] and holder.datas == HOLDER["datas"], holder.datas
    assert holder.typed == HOLDER["typed"] and type(holder.typed["ttl"]) is Duration, holder.typed
    assert type(holder.typed["td"]) is timedelta, holder.typed
    assert type(holder.plain) is RfPlainSub and holder.plain == HOLDER["plain"], holder.plain
    assert [type(td) for td in holder.tds] == [timedelta, timedelta] and holder.tds == HOLDER["tds"], holder.tds


def _assert_sub_models_native(row: dict[str, Any]) -> None:
    """The stored row keeps SurrealDB's native durations, ranges and geometries inside every sub-model (never ``{elapsed: …}``)."""
    subs = [row["sub"], row["maybe"], *row["subs"], row["by_key"]["a"], row["data"], *row["datas"]]
    for sub in subs:
        assert type(sub["ttl"]) is surrealdb.Duration and type(sub["hours"]) is surrealdb.Range, sub
        assert type(sub["td"]) is surrealdb.Duration, sub
    assert type(row["sub"]["period"]) is surrealdb.Range, row["sub"]
    assert type(row["sub"]["where"]) is _sdk.SdkGeometryPoint, row["sub"]
    assert type(row["typed"]["ttl"]) is surrealdb.Duration and type(row["typed"]["td"]) is surrealdb.Duration, row["typed"]
    assert [type(td) for td in row["tds"]] == [surrealdb.Duration] * len(HOLDER["tds"]), row["tds"]


async def _holder_saved() -> RfHolder:
    holder = RfHolder(id="h", **HOLDER)
    await holder.save()
    return holder


async def _sync_save_without_id() -> RfHolder:
    holder = RfHolder(**HOLDER)
    await holder.save()
    return holder


async def _sync_upsert() -> RfHolder:
    holder = RfHolder(id="h", **HOLDER)
    await holder.upsert()
    return holder


async def _sync_refresh() -> RfHolder:
    holder = await _holder_saved()
    await holder.refresh()
    return holder


async def _sync_merge() -> RfHolder:
    holder = await _holder_saved()
    await holder.merge(n=2)
    return holder


async def _sync_patch() -> RfHolder:
    holder = await _holder_saved()
    await holder.patch([{"op": "replace", "path": "/n", "value": 2}])
    return holder


async def _sync_atomic_increment() -> RfHolder:
    holder = await _holder_saved()
    await holder.atomic_increment("n", 2)
    return holder


#: The calls that apply the row the server returns, each from a fresh table.
_SYNC_CASES: dict[str, Callable[[], Awaitable[RfHolder]]] = {
    "save-without-id": _sync_save_without_id,
    "upsert": _sync_upsert,
    "refresh": _sync_refresh,
    "merge": _sync_merge,
    "patch": _sync_patch,
    "atomic-increment": _sync_atomic_increment,
}


def _sdk_values(value: Any) -> Any:
    """``value`` with the ORM's durations, ranges and geometries as the SDK reads them back, at any depth."""
    if isinstance(value, Duration):
        return surrealdb.Duration(value.elapsed)
    if isinstance(value, timedelta):
        return surrealdb.Duration(value // timedelta(microseconds=1) * 1000)
    if isinstance(value, Geometry):
        return value._to_sdk()
    if isinstance(value, Range):
        return surrealdb.Range(value.begin, value.end)
    if isinstance(value, dict):
        return {key: _sdk_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sdk_values(item) for item in value]
    return value


def _sdk_row(holder: BaseSurrealModel) -> dict[str, Any]:
    """The row a write of ``holder`` returns, as the SDK reads it: sub-models as dicts holding the SDK's values."""
    row: dict[str, Any] = _sdk_values(holder.model_dump(exclude={"id"}))
    return row


class RfTdSub(BaseModel):
    """A sub-model ``timedelta``: a read converts the SDK's duration there (README section 24), and so does a write sync."""

    td: timedelta


class RfAliasedSub(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ttl: Duration = Field(alias="ttlCol")


class RfStrictAliasedSub(BaseModel):
    ttl: Duration = Field(alias="ttlCol")


class RfBoomSub(BaseModel):
    """A sub-model whose validator raises something else than a ``ValueError``."""

    n: int

    @field_validator("n")
    @classmethod
    def _boom(cls, value: int) -> int:
        raise RuntimeError("boom")


@dataclasses.dataclass
class RfStdData:
    """A stdlib dataclass: it takes the config of the model it sits in, in a read and in a write sync alike."""

    name: str


class RfCat(BaseModel):
    kind: Literal["cat"] = "cat"
    ttl: Duration


class RfDog(BaseModel):
    kind: Literal["dog"]
    ttl: Duration


@pydantic_dataclass
class RfPydData:
    """A Pydantic dataclass: a dataclass too, with its own config."""

    ttl: Duration


class RfOddHolder(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_odd", str_strip_whitespace=True)

    id: str | None = None
    td_sub: RfTdSub | None = None
    aliased: RfAliasedSub | None = None
    strict_aliased: RfStrictAliasedSub | None = None
    boom: RfBoomSub | None = None
    std: RfStdData | None = None
    std_column: RfStdData | None = Field(default=None, alias="stdCol")
    picked: Annotated[RfPlainSub | RfSub | None, Field(union_mode="left_to_right")] = None
    pet: RfCat | RfDog | None = Field(default=None, discriminator="kind")
    n: int = 0


#: Row additions for the tests that cover both paths of a write sync: the whole row validates (``{}``), or it
#: does not (``n``) and each sub-model field is validated alone, against its own annotation.
_ROW_PATHS = pytest.mark.parametrize("extra", [{}, {"n": "x"}], ids=["whole-row", "field-alone"])


class TestSubModelSyncRule:
    """#228 (unit): the fields a write sync validates, and the fallback to the value as received."""

    def test_fields_validated_on_sync(self) -> None:
        # #261: every declared field but ``id`` and the top-level ``timedelta``/``Duration`` fields, which have their
        # own rule (#228 validated only the fields holding a sub-model, a geometry or a nested ``timedelta``).
        expected = {"sub", "maybe", "subs", "by_key", "data", "datas", "typed", "plain", "many", "tds", "n"}
        assert set(RfHolder._sync_fields()) == expected
        assert RfHolder._sync_fields() is RfHolder._sync_fields(), "cached per class"
        assert set(RfPlain._sync_fields()) == {"name", "tags", "meta"}
        assert set(RfEvent._sync_fields()) == set(RfEvent.model_fields) - {"id", "ttl", "grace"}
        # Never a ``timedelta`` the top-level rule converts (a subclass is not one), nor a computed one.
        assert set(RfKinds._sync_fields()) == {"either", "many", "by_key", "sub", "window"}
        # Python names only: ``_apply_record`` translates each column first.
        assert "stdCol" not in RfOddHolder._sync_fields() and "std_column" in RfOddHolder._sync_fields()

    def test_a_returned_row_gives_the_declared_instances(self) -> None:
        holder = RfHolder.model_construct(id="h", sub=None)
        row = _sdk_row(RfHolder(id="h", **HOLDER))
        assert type(row["sub"]["ttl"]) is surrealdb.Duration and type(row["data"]["hours"]) is surrealdb.Range
        holder._apply_record(row)
        _assert_sub_models(holder)
        # #261: a typed container of scalars holds what a read gives too, the ORM's durations.
        assert [type(d) for d in holder.many] == [Duration]
        assert holder.model_dump()["many"] == HOLDER["many"]

    def test_only_still_filters(self) -> None:
        holder = RfHolder(id="h", **HOLDER)
        before = holder.sub
        holder._apply_record({"sub": {"ttl": surrealdb.Duration(1)}, "n": 4}, only={"n"})
        assert holder.sub is before and holder.n == 4

    @pytest.mark.parametrize(
        "raw",
        [{"ttl": "1h"}, {"hours": surrealdb.Range(None, None)}, "text", 3, None],
        ids=["bad-duration", "missing-ttl", "text", "number", "none"],
    )
    def test_a_row_that_does_not_validate_is_applied_as_received(self, raw: Any) -> None:
        holder = RfHolder(id="h", **HOLDER)
        holder._apply_record({"sub": raw, "maybe": raw})
        assert holder.sub is raw
        assert holder.maybe is None if raw is None else holder.maybe is raw

    @_ROW_PATHS
    def test_a_sub_model_timedelta_follows_the_read(self, extra: dict[str, Any]) -> None:
        """README section 24: a sub-model ``timedelta`` reads the SDK's duration, floored to the microsecond, and so does the sync."""
        raw = {"td": surrealdb.Duration(5 * 10**9 + 1999)}
        expected = RfTdSub(td=timedelta(seconds=5, microseconds=1))
        assert RfOddHolder.model_validate({"td_sub": raw}).td_sub == expected
        odd = RfOddHolder()
        odd._apply_record({"td_sub": raw, **extra})
        assert type(odd.td_sub) is RfTdSub and odd.td_sub == expected
        assert type(odd.td_sub.td) is timedelta
        assert type(raw["td"]) is surrealdb.Duration, "the row is not mutated"

    # ``pause`` refuses ``"x"``: the row fails and each field is validated alone.
    @pytest.mark.parametrize("extra", [{}, {"pauseCol": "x"}], ids=["whole-row", "field-alone"])
    def test_a_timedelta_below_the_top_level_follows_the_read(self, extra: dict[str, Any]) -> None:
        """``list[timedelta]``, ``dict[str, timedelta]``, ``timedelta | str`` and ``Range[timedelta]`` hold what a read gives."""
        sdk = surrealdb.Duration(90 * 10**9 + 1)
        row = {
            "many": [sdk, surrealdb.Duration(1000)],
            "by_key": {"a": sdk},
            "either": sdk,
            "window": surrealdb.Range(BoundIncluded(sdk), None),
        }
        read = RfKinds.model_validate(row)
        kinds = RfKinds(id="k")
        kinds._apply_record({**row, **extra})
        for got in (read, kinds):
            assert got.many == [timedelta(seconds=90), timedelta(microseconds=1)]
            assert [type(td) for td in got.many] == [timedelta, timedelta]
            assert type(got.by_key["a"]) is timedelta and got.by_key == {"a": timedelta(seconds=90)}
            assert type(got.either) is timedelta and got.either == timedelta(seconds=90)
            assert type(got.window) is Range and got.window == Range(BoundIncluded(timedelta(seconds=90)), None)
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # the full write serialises timedeltas, not the SDK's dataclass
            dumped = kinds.model_dump(include={"many", "by_key", "either"})
        assert dumped == {"many": read.many, "by_key": read.by_key, "either": read.either}

    @_ROW_PATHS
    @pytest.mark.parametrize("key", ["ttl", "ttlCol"])
    def test_aliased_sub_model_keys_follow_the_read(self, key: str, extra: dict[str, Any]) -> None:
        """What ``get()`` accepts in a sub-model, under its alias or its name, the sync accepts; nothing else."""
        raw = {key: surrealdb.Duration(5)}
        for field in ("aliased", "strict_aliased"):
            try:
                expected: Any = getattr(RfOddHolder.model_validate({field: raw}), field)
            except ValidationError:
                expected = raw
            odd = RfOddHolder()
            odd._apply_record({field: raw, **extra})
            got = getattr(odd, field)
            assert got == expected and type(got) is type(expected), (field, got)
            if type(got) is not dict:
                assert type(got.ttl) is Duration and got.ttl == Duration(5)
        assert type(RfOddHolder.model_validate({"strict_aliased": {"ttlCol": surrealdb.Duration(5)}}).strict_aliased) is (
            RfStrictAliasedSub
        )

    def test_a_validator_that_raises_anything_never_escapes(self) -> None:
        raw = {"n": 1}
        with pytest.raises(RuntimeError):
            RfOddHolder.model_validate({"boom": raw})
        odd = RfOddHolder()
        odd._apply_record({"boom": raw})
        assert odd.boom is raw

    @_ROW_PATHS
    def test_the_model_config_reaches_a_stdlib_dataclass(self, extra: dict[str, Any]) -> None:
        """A stdlib dataclass takes the model's config (``str_strip_whitespace``) in a read and in a write sync alike."""
        assert RfOddHolder.model_validate({"std": {"name": "  ada  "}}).std == RfStdData("ada")
        odd = RfOddHolder()
        odd._apply_record({"std": {"name": "  ada  "}, "stdCol": {"name": " bob "}, **extra})
        assert odd.std == RfStdData("ada") and odd.std_column == RfStdData("bob")

    @_ROW_PATHS
    def test_field_metadata_is_kept(self, extra: dict[str, Any]) -> None:
        """The field's own metadata reaches the adapter: ``left_to_right`` picks the first member that validates.

        The smart mode would pick ``RfSub``, which sets more fields from the row.
        """
        assert RfOddHolder.model_fields["picked"].metadata, "the union mode lives in the field's metadata"
        raw = {"name": "p", "ttl": surrealdb.Duration(5), "hours": surrealdb.Range(None, None)}
        odd = RfOddHolder()
        odd._apply_record({"picked": raw, **extra})
        assert type(odd.picked) is RfPlainSub and odd.picked == RfOddHolder.model_validate({"picked": raw}).picked

    @_ROW_PATHS
    def test_the_discriminator_is_kept(self, extra: dict[str, Any]) -> None:
        """A tagged union reads the tag, as in a read: the smart mode would take an untagged row for a cat."""
        dog = {"kind": "dog", "ttl": surrealdb.Duration(5)}
        odd = RfOddHolder()
        odd._apply_record({"pet": dog, **extra})
        assert type(odd.pet) is RfDog and type(odd.pet.ttl) is Duration
        untagged = {"ttl": surrealdb.Duration(5)}
        with pytest.raises(ValidationError):
            RfOddHolder.model_validate({"pet": untagged})
        odd._apply_record({"pet": untagged, **extra})
        assert odd.pet is untagged

    def test_adapters_are_built_once_on_first_use(self) -> None:
        class RfLazy(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="rf_lazy")

            id: str | None = None
            sub: RfPlainSub | None = None
            n: int = 0

        lazy = RfLazy()
        lazy._apply_record({"sub": {"name": "a"}})
        assert type(lazy.sub) is RfPlainSub and lazy.sub == RfPlainSub(name="a")
        assert RfLazy not in model_base._SYNC_ADAPTERS, "a row that validates needs no adapter"
        lazy._apply_record({"sub": {"name": "b"}, "n": "x"})  # the row fails (``n``): ``sub`` is validated alone
        assert type(lazy.sub) is RfPlainSub and lazy.sub == RfPlainSub(name="b") and lazy.n == "x"
        adapter = model_base._SYNC_ADAPTERS[RfLazy]["sub"]
        assert adapter is not None and RfLazy._sync_adapter("sub") is adapter
        assert RfLazy._sync_adapter("id") is None, "a field the sync does not validate has no adapter"

    def test_an_adapter_that_cannot_be_built_keeps_the_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class RfUnbuildable(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="rf_unbuildable")

            id: str | None = None
            sub: RfPlainSub | None = None
            n: int = 0

        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise TypeError("no adapter")

        monkeypatch.setattr(model_base, "TypeAdapter", refuse)
        raw = {"name": "a"}
        unbuildable = RfUnbuildable()
        unbuildable._apply_record({"sub": raw, "n": "x"})
        assert unbuildable.sub is raw and RfUnbuildable._sync_adapter("sub") is None

    def test_an_unresolved_sub_model_keeps_the_value(self) -> None:
        class RfLater(BaseModel):
            y: "RfUndefined"  # type: ignore[name-defined]  # noqa: F821 (never defined: the model stays incomplete)

        class RfIncomplete(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="rf_incomplete")

            id: str | None = None
            later: RfLater | None = None

        assert not RfIncomplete.__pydantic_complete__
        incomplete = RfIncomplete.model_construct()
        raw = {"y": 1}
        incomplete._apply_record({"later": raw})
        assert incomplete.later is raw
        assert RfIncomplete not in model_base._SYNC_FIELDS and RfIncomplete not in model_base._SYNC_ADAPTERS


class TestSubModelWriteSyncE2E:
    """#228: after a write that syncs the instance, sub-models hold their declared types and a full write keeps them native."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("write", ["update", "upsert"])
    @pytest.mark.parametrize("case", list(_SYNC_CASES))
    async def test_sub_models_survive_a_write_sync(self, case: str, write: str) -> None:
        async with orm_client("rf_holder"):
            holder = await _SYNC_CASES[case]()
            _assert_sub_models(holder)
            with warnings.catch_warnings():
                warnings.simplefilter("error")  # no PydanticSerializationUnexpectedValue on the full write
                await getattr(holder, write)()
            rid = holder._record_id()
            assert rid is not None
            _assert_sub_models_native(await _raw(rid))
            _assert_sub_models(await RfHolder.objects().get(holder.get_raw_id()))

    @pytest.mark.asyncio
    async def test_the_issue_reproduction(self) -> None:
        """#228's own reproduction: ``upsert()``, then ``update()``, then ``get()``."""
        sub = RfIssueSub(ttl=Duration.parse("1h"), td=timedelta(minutes=5))
        async with orm_client("rf_issue_holder"):
            holder = RfIssueHolder(id="h", sub=sub, many=[Duration.parse("2m")])
            await holder.upsert()
            assert type(holder.sub) is RfIssueSub and holder.sub == sub, holder.sub
            with warnings.catch_warnings():
                warnings.simplefilter("error")  # no PydanticSerializationUnexpectedValue
                await holder.update()
            row = await _raw(RecordID("rf_issue_holder", "h"))
            assert type(row["sub"]["ttl"]) is surrealdb.Duration and type(row["sub"]["td"]) is surrealdb.Duration, row
            got = await RfIssueHolder.objects().get("h")
            assert got.sub == sub and got.many == [Duration.parse("2m")], got
            assert (type(got.sub.ttl), type(got.sub.td)) == (Duration, timedelta)


class RfIssueSub(BaseModel):
    """The sub-model of #228's reproduction."""

    ttl: Duration
    td: timedelta


class RfIssueHolder(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_issue_holder")

    id: str | None = None
    sub: RfIssueSub
    many: list[Duration]


# --- a ``timedelta`` below the top level reads the SDK's duration (#228) ---------------------------


@dataclasses.dataclass
class RfTdData:
    td: timedelta


class RfTdDict(TypedDict):
    td: timedelta


class RfTdAliasedSub(BaseModel):
    # A write stores a sub-model under its field names: ``populate_by_name`` reads them back.
    model_config = ConfigDict(populate_by_name=True)

    td: timedelta = Field(alias="tdCol")


class RfTdHolder(BaseSurrealModel):
    """Every position below the top level where a ``timedelta`` reads the SDK's duration."""

    model_config = SurrealConfigDict(table_name="rf_td_holder")

    id: str | None = None
    sub: RfTdSub | None = None
    subs: list[RfTdSub] = []
    data: RfTdData | None = None
    typed: RfTdDict | None = None
    aliased: RfTdAliasedSub | None = None
    picked: RfPlainSub | RfTdSub | None = None  # a union of models: Pydantic tags the error with the member
    tds: list[timedelta] = []
    pair: tuple[timedelta, ...] = ()
    by_key: dict[str, timedelta] = {}
    either: timedelta | str | None = None  # a union: Pydantic tags the error with ``timedelta``
    n: int = 0


#: An SDK duration that is not a whole number of microseconds, and the ``timedelta`` it reads as (floored).
_SDK_TD = surrealdb.Duration(5400 * 10**9 + 1999)
_TD = timedelta(hours=1, minutes=30, microseconds=1)

#: ``field: (value in the row, value read)`` for every position of ``RfTdHolder``.
_TD_POSITIONS: dict[str, tuple[Any, Any]] = {
    "sub": ({"td": _SDK_TD}, RfTdSub(td=_TD)),
    "subs": (
        [{"td": _SDK_TD}, {"td": surrealdb.Duration(1000)}],
        [RfTdSub(td=_TD), RfTdSub(td=timedelta(microseconds=1))],
    ),
    "data": ({"td": _SDK_TD}, RfTdData(td=_TD)),
    "typed": ({"td": _SDK_TD}, {"td": _TD}),
    "aliased": ({"tdCol": _SDK_TD}, RfTdAliasedSub(tdCol=_TD)),
    "picked": ({"td": _SDK_TD}, RfTdSub(td=_TD)),
    "tds": ([_SDK_TD, surrealdb.Duration(1000)], [_TD, timedelta(microseconds=1)]),
    "pair": ([_SDK_TD], (_TD,)),
    "by_key": ({"a": _SDK_TD}, {"a": _TD}),
    "either": (_SDK_TD, _TD),
}


def _assert_td_positions(holder: Any) -> None:
    """Every ``RfTdHolder`` position holds its declared type with ``timedelta`` values, as ``_TD_POSITIONS`` says."""
    assert isinstance(holder, RfTdHolder), holder
    for field, (_, expected) in _TD_POSITIONS.items():
        got = getattr(holder, field)
        assert got == expected and type(got) is type(expected), (field, got)


class TestNestedTimedeltaRead:
    """#228 (unit): a ``timedelta`` below the top level reads the SDK's duration, floored to the microsecond."""

    @pytest.mark.parametrize("field", list(_TD_POSITIONS))
    def test_each_position(self, field: str) -> None:
        raw, expected = _TD_POSITIONS[field]
        got = getattr(RfTdHolder.model_validate({field: raw}), field)
        assert got == expected and type(got) is type(expected), got

    def test_every_position_at_once_without_touching_the_row(self) -> None:
        row = {field: raw for field, (raw, _) in _TD_POSITIONS.items()}
        before = copy.deepcopy(row)
        _assert_td_positions(RfTdHolder.model_validate(row))
        assert row == before and row["sub"]["td"] is _SDK_TD, "the caller's row is not mutated"

    def test_every_way_to_build_the_model(self) -> None:
        row = {"id": RecordID("rf_td_holder", "k"), "sub": {"td": _SDK_TD}, "tds": [_SDK_TD]}
        for holder in (RfTdHolder.from_db(dict(row)), RfTdHolder(**row), RfTdHolder.model_validate(row)):
            assert isinstance(holder, RfTdHolder) and holder.id == "k", holder
            assert holder.sub == RfTdSub(td=_TD) and holder.tds == [_TD], holder

    @pytest.mark.parametrize(
        "value",
        [surrealdb.Duration(-1), _SDK_BEYOND_TIMEDELTA, surrealdb.Duration(1.5), Duration(5), "x"],
        ids=["negative", "beyond-timedelta-max", "float-elapsed", "orm-duration", "text"],
    )
    def test_a_value_the_rule_cannot_convert_is_refused_as_before(self, value: Any) -> None:
        with pytest.raises(ValidationError) as caught:
            RfTdHolder.model_validate({"sub": {"td": value}, "tds": [value]})
        assert [(e["loc"], e["input"] is value) for e in caught.value.errors()] == [(("sub", "td"), True), (("tds", 0), True)]

    def test_another_error_is_reported_alone(self) -> None:
        """The durations convert and the row still fails: the error names what is really wrong."""
        with pytest.raises(ValidationError) as caught:
            RfTdHolder.model_validate({"sub": {"td": _SDK_TD}, "tds": [_SDK_TD], "n": "x"})
        assert [e["loc"] for e in caught.value.errors()] == [("n",)]

    def test_a_row_a_validator_rebuilt_is_refused_as_before(self) -> None:
        """A ``mode="before"`` validator that rebuilds the row's sub-dicts hides the SDK's values: refused, never guessed."""

        class RfRebuilt(RfTdHolder):
            @model_validator(mode="before")
            @classmethod
            def _rebuild(cls, data: Any) -> Any:
                return copy.deepcopy(data)

        with pytest.raises(ValidationError):
            RfRebuilt.model_validate({"sub": {"td": _SDK_TD}})
        assert RfRebuilt.model_validate({"sub": {"td": _TD}}).sub == RfTdSub(td=_TD)

    def test_a_shared_duration_converts_where_a_timedelta_is_declared_only(self) -> None:
        """One SDK object at two positions: a ``Duration`` keeps its nanoseconds, the ``timedelta`` is floored."""

        class RfBoth(BaseModel):
            ttl: Duration
            td: timedelta

        class RfShared(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="rf_shared")

            id: str | None = None
            both: RfBoth | None = None

        shared = RfShared.model_validate({"both": {"ttl": _SDK_TD, "td": _SDK_TD}})
        assert shared.both is not None and shared.both.ttl == Duration(_SDK_TD.elapsed) and shared.both.td == _TD

    def test_the_path_must_end_on_the_refused_value(self) -> None:
        value = surrealdb.Duration(5)
        assert model_base._path_to({"f": [value]}, ("f", 0), value) == ["f", 0]
        assert model_base._path_to({"f": {"td": value}}, ("f", "RfTdSub", "td"), value) == ["f", "td"]
        assert model_base._path_to({"f": value}, ("f", "timedelta"), value) == ["f"]
        assert model_base._path_to((value,), (0,), value) == [0]
        # A union tag that happens to be a key leads elsewhere; an index out of range; a scalar on the way.
        assert model_base._path_to({"f": {"RfTdSub": {"td": 1}, "td": value}}, ("f", "RfTdSub", "td"), value) is None
        assert model_base._path_to({"f": [value]}, ("f", 1), value) is None
        assert model_base._path_to({"f": 3}, ("f", 0), value) is None

    def test_the_conversion_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*args: Any) -> Any:
            raise RuntimeError("boom")

        monkeypatch.setattr(model_base, "_path_to", boom)
        with pytest.raises(ValidationError):
            RfTdHolder.model_validate({"sub": {"td": _SDK_TD}})

    def test_assignment_validation_is_unchanged(self) -> None:
        class RfAssigned(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="rf_assigned", validate_assignment=True)

            id: str | None = None
            sub: RfTdSub | None = None

        assigned = RfAssigned(sub={"td": _SDK_TD})  # type: ignore[arg-type]
        assert assigned.sub == RfTdSub(td=_TD)
        assigned.sub = RfTdSub(td=timedelta(seconds=1))
        assert assigned.sub == RfTdSub(td=timedelta(seconds=1))


class RfTdOther(BaseModel):
    """A second sub-model with a ``timedelta`` named ``td``: in a union with ``RfTdSub``, both refuse the same value."""

    td: timedelta
    label: str = ""


class RfTdMany(BaseSurrealModel):
    """Many SDK durations below the top level: in a ``list[timedelta]`` and in sub-models."""

    model_config = SurrealConfigDict(table_name="rf_td_many")

    id: str | None = None
    tds: list[timedelta] = []
    subs: list[RfTdSub] = []
    either: RfTdSub | RfTdOther | None = None


def _count_copies(monkeypatch: pytest.MonkeyPatch) -> list[tuple[type, int]]:
    """Record ``(type, id)`` of each container the duration conversion copies (``model_base._copy_with``)."""
    copied: list[tuple[type, int]] = []
    real = model_base._copy_with

    def counting(node: Any, items: Any) -> Any:
        copied.append((type(node), id(node)))
        return real(node, items)

    monkeypatch.setattr(model_base, "_copy_with", counting)
    return copied


def _sdk_durations(n: int) -> list[Any]:
    """``n`` distinct SDK durations, of 1 to ``n`` microseconds."""
    return [surrealdb.Duration(i * 1000) for i in range(1, n + 1)]


class TestNestedTimedeltaCopies:
    """#228 (unit): the conversion copies each container on a converted path once, whatever the number of durations."""

    def test_a_read_copies_each_container_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        copied = _count_copies(monkeypatch)
        counts: dict[int, int] = {}
        for n in (10, 2000):
            copied.clear()
            tds = _sdk_durations(n)
            subs = [{"td": value} for value in _sdk_durations(3)]
            row = {"tds": tds, "subs": subs}
            got = RfTdMany.model_validate(row)
            # The row, ``tds``, ``subs`` and the 3 sub-models: each copied once, nothing else.
            containers = {id(row), id(tds), id(subs), *(id(sub) for sub in subs)}
            assert sorted(i for _, i in copied) == sorted(containers), copied
            counts[n] = len(copied)
            assert got.tds == [timedelta(microseconds=i) for i in range(1, n + 1)]
            assert got.subs == [RfTdSub(td=timedelta(microseconds=i)) for i in range(1, 4)]
            assert row["tds"] is tds and type(tds[0]) is surrealdb.Duration, "the caller's row is not mutated"
        assert counts == {10: 6, 2000: 6}

    def test_a_field_validated_alone_copies_the_tuple_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        copied = _count_copies(monkeypatch)
        for n in (10, 2000):
            copied.clear()
            tds = _sdk_durations(n)
            got = RfTdMany._validated_on_sync("tds", tds)
            # The 1-tuple that holds the value and the list: each copied once, nothing else.
            assert len(copied) == 2 and (list, id(tds)) in copied and [t for t, _ in copied].count(tuple) == 1, copied
            assert got == [timedelta(microseconds=i) for i in range(1, n + 1)]
            assert type(tds[0]) is surrealdb.Duration, "the value is not mutated"

    def test_one_position_refused_twice_is_converted_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A union of two models with the same field: both errors lead to one position, converted once."""
        copied = _count_copies(monkeypatch)
        either = {"td": _SDK_TD}
        row = {"either": either}
        got = RfTdMany.model_validate(row)
        assert isinstance(got.either, RfTdSub | RfTdOther) and got.either.td == _TD, got
        assert sorted(i for _, i in copied) == sorted([id(row), id(either)]), copied
        assert row == {"either": {"td": _SDK_TD}} and either["td"] is _SDK_TD


class TestNestedTimedeltaReadE2E:
    """#228: a ``timedelta`` below the top level reads the duration the server stores, and a write keeps it native."""

    @pytest.mark.asyncio
    async def test_read_then_write_back(self) -> None:
        rid = RecordID("rf_td_holder", "k")
        async with orm_client("rf_td_holder") as client:
            # ``encoding.encode``: the SDK's own encoder sends its ``Duration`` in a form the server refuses.
            row = encoding.encode({field: raw for field, (raw, _) in _TD_POSITIONS.items()})
            await client.query("CREATE $rid CONTENT $c RETURN NONE;", {"rid": rid, "c": row})
            got = await RfTdHolder.objects().get("k")
            [listed] = await RfTdHolder.objects().all()
            for holder in (got, listed):
                _assert_td_positions(holder)
            await got.merge(n=1)  # a write sync: every position keeps what a read gives
            _assert_td_positions(got)
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                await got.update()
            stored = await _raw(rid)
            assert type(stored["sub"]["td"]) is surrealdb.Duration and type(stored["tds"][0]) is surrealdb.Duration
            assert type(stored["by_key"]["a"]) is surrealdb.Duration and type(stored["either"]) is surrealdb.Duration
            _assert_td_positions(await RfTdHolder.objects().get("k"))


# --- the duration conversion never runs the user's code twice on one input (#228) -----------------


def _read_cents(data: Any) -> Any:
    """Read stored cents as a price, in the dict itself: a ``mode="before"`` validator that is not idempotent."""
    if isinstance(data, dict) and "price" in data:
        data["price"] = Decimal(data["price"]) / 100
    return data


def _prepend_all(value: Any) -> Any:
    """Insert ``"all"`` in the list itself: a ``mode="before"`` validator that is not idempotent."""
    if isinstance(value, list):
        value.insert(0, "all")
    return value


class RfCentsSub(BaseModel):
    """A sub-model whose own ``mode="before"`` validator reads its cents in place, next to a ``timedelta``."""

    price: Decimal = Decimal(0)
    td: timedelta
    qty: int = 0

    @model_validator(mode="before")
    @classmethod
    def _cents(cls, data: Any) -> Any:
        return _read_cents(data)


class RfCents(BaseSurrealModel):
    """Code of the user's that changes its input in place, at each level a read runs it."""

    model_config = SurrealConfigDict(table_name="rf_cents")

    id: str | None = None
    price: Decimal = Decimal(0)
    tags: list[str] = []
    labels: Annotated[list[str], BeforeValidator(_prepend_all)] = []
    sub: RfCentsSub | None = None
    tds: list[timedelta] = []
    anything: Any = None

    @model_validator(mode="before")
    @classmethod
    def _cents(cls, data: Any) -> Any:
        return _read_cents(data)

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, value: Any) -> Any:
        return _prepend_all(value)


class RfCentsHolder(BaseSurrealModel):
    """The user's code sits in a sub-model only: a write sync may validate the field alone (``_validated_on_sync``)."""

    model_config = SurrealConfigDict(table_name="rf_cents_holder")

    id: str | None = None
    sub: RfCentsSub | None = None
    n: int = 0


class RfAfterOnly(BaseSurrealModel):
    """The user's code runs on validated values only: an ``after`` validator, a serializer."""

    model_config = SurrealConfigDict(table_name="rf_after_only")

    id: str | None = None
    sub: RfTdSub | None = None
    name: str = ""

    @field_validator("name", mode="after")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _check(self) -> Self:
        return self

    @field_serializer("name")
    def _upper(self, value: str) -> str:
        return value.upper()


def _cents_row(td: Any) -> dict[str, Any]:
    """A fresh ``RfCents`` row (the validators change it in place) whose durations below the top level are ``td``."""
    return {"price": 1999, "tags": ["a"], "labels": ["b"], "sub": {"price": 250, "td": td}, "tds": [td]}


def _assert_sub_read_once(sub: Any) -> None:
    """``sub`` is the ``RfCentsSub`` of ``_cents_row``: its validator was applied once (the row stores 250 cents)."""
    assert type(sub) is RfCentsSub and sub == RfCentsSub(price=250, td=_TD), sub
    assert sub.price == Decimal("2.5") and sub.td == _TD, sub


def _assert_cents_read_once(cents: RfCents) -> None:
    """Each validator of ``RfCents`` was applied once to what the instance holds."""
    assert cents.price == Decimal("19.99") and cents.tags == ["all", "a"] and cents.labels == ["all", "b"], cents
    assert cents.tds == [_TD], cents
    _assert_sub_read_once(cents.sub)


# Code of the user's that runs on a value Pydantic passes through (``Any``): the row's own object.


class RfStep(BaseModel):
    """A ``timedelta`` below the top level: the SDK's duration fails the first run there."""

    wait: timedelta | None = None


def _append_x(value: Any) -> None:
    """Append ``"x"`` to a list in place: not idempotent."""
    if isinstance(value, list):
        value.append("x")


def _bump(value: Any) -> Any:
    """Count in a dict in place: not idempotent."""
    if isinstance(value, dict):
        value["n"] = value.get("n", 0) + 1
    return value


class RfTaggedSub(BaseModel):
    """A sub-model whose ``after`` validator changes the row's own list (``Any`` passes it through)."""

    extra: Any = None

    @model_validator(mode="after")
    def _tag(self) -> Self:
        _append_x(self.extra)
        return self


class RfTagged(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_tagged")

    id: str | None = None
    sub: RfTaggedSub | None = None
    steps: list[RfStep] = []
    n: int = 0


class RfCountedSub(BaseModel):
    """A sub-model whose ``model_post_init`` changes the row's own dict."""

    extra: Any = None

    def model_post_init(self, context: Any, /) -> None:
        _bump(self.extra)


class RfCounted(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_counted")

    id: str | None = None
    sub: RfCountedSub | None = None
    steps: list[RfStep] = []
    n: int = 0


class RfBumped(BaseSurrealModel):
    """An ``AfterValidator`` on the holder's own ``Any`` field."""

    model_config = SurrealConfigDict(table_name="rf_bumped")

    id: str | None = None
    meta: Annotated[Any, pydantic.AfterValidator(_bump)] = None
    steps: list[RfStep] = []


class RfMarkA(BaseModel):
    kind: Literal["a"] = "a"
    marks: list[int] = []


class RfMarkB(BaseModel):
    kind: Literal["b"] = "b"


def _pick_and_mark(value: Any) -> str:
    """A callable discriminator that changes its input in place: not idempotent."""
    if isinstance(value, dict):
        value["marks"].append(1)
        return str(value.get("kind", "a"))
    return str(getattr(value, "kind", "a"))


class RfMarked(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_marked")

    id: str | None = None
    pick: (
        Annotated[
            Annotated[RfMarkA, pydantic.Tag("a")] | Annotated[RfMarkB, pydantic.Tag("b")],
            pydantic.Discriminator(_pick_and_mark),
        ]
        | None
    ) = None
    steps: list[RfStep] = []


def _derived_from(data: dict[str, Any]) -> dict[str, Any]:
    """A ``default_factory`` that takes the validated data and changes a value of it in place."""
    _bump(data.get("meta"))
    return {}


class RfFactory(BaseSurrealModel):
    """The factory field is declared before the failing one, so the first run calls it."""

    model_config = SurrealConfigDict(table_name="rf_factory")

    id: str | None = None
    meta: Any = None
    derived: dict[str, Any] = Field(default_factory=_derived_from)
    steps: list[RfStep] = []


@dataclasses.dataclass
class RfPostDc:
    """A dataclass whose ``__post_init__`` changes the row's own list."""

    extra: Any = None

    def __post_init__(self) -> None:
        _append_x(self.extra)


class RfPostDcHolder(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_post_dc_holder")

    id: str | None = None
    dc: RfPostDc | None = None
    steps: list[RfStep] = []


class RfTaggedStep(BaseModel):
    """An ``after`` validator and a ``timedelta`` in one sub-model, held in a list."""

    extra: Any = None
    wait: timedelta | None = None

    @model_validator(mode="after")
    def _tag(self) -> Self:
        _append_x(self.extra)
        return self


class RfTaggedSteps(BaseSurrealModel):
    """No validator of its own: when the row fails, ``subs`` is validated alone (``_validated_on_sync``)."""

    model_config = SurrealConfigDict(table_name="rf_tagged_steps")

    id: str | None = None
    subs: list[RfTaggedStep] = []
    n: int = 0


class RfPrivSub(BaseModel):
    _seen: int = pydantic.PrivateAttr(0)
    a: int = 0


class RfPrivate(BaseSurrealModel):
    """Pydantic's own ``model_post_init`` (private attributes), on the holder and on a sub-model."""

    model_config = SurrealConfigDict(table_name="rf_private")

    id: str | None = None
    sub: RfPrivSub | None = None
    tds: list[timedelta] = []
    _cache: dict[str, Any] = pydantic.PrivateAttr(default_factory=dict)


class RfSerializedOnly(BaseSurrealModel):
    """Code of the user's that only serializes."""

    model_config = SurrealConfigDict(table_name="rf_serialized_only")

    id: str | None = None
    tds: list[timedelta] = []
    name: str = ""

    @field_serializer("name")
    def _upper(self, value: str) -> str:
        return value.upper()


class RfOwner(BaseModel):
    name: str = ""


class RfOwned(BaseSurrealModel):
    """No ``timedelta`` below the top level, and code of the user's on its input."""

    model_config = SurrealConfigDict(table_name="rf_owned")

    id: str | None = None
    owner: RfOwner | None = None
    meta: Any = None
    ttl: Duration | None = None

    @field_validator("meta", mode="before")
    @classmethod
    def _keep(cls, value: Any) -> Any:
        return value


def _steps_row(model: type[BaseSurrealModel], **fields: Any) -> dict[str, Any]:
    """A fresh row of ``model`` whose ``steps`` hold the SDK's duration, which fails the first run."""
    return {
        "id": RecordID(model.get_table_name(), "k"),
        **copy.deepcopy(fields),
        "steps": [{"wait": surrealdb.Duration(10**9)}],
    }


#: ``(model, fields of the row, how to read the value the user's code changed, the value read)``.
_ONCE_CASES = [
    pytest.param(RfTagged, {"sub": {"extra": []}}, lambda m: m.sub.extra, ["x"], id="sub-model-after-validator"),
    pytest.param(RfCounted, {"sub": {"extra": {}}}, lambda m: m.sub.extra, {"n": 1}, id="sub-model-post-init"),
    pytest.param(RfBumped, {"meta": {}}, lambda m: m.meta, {"n": 1}, id="holder-after-validator"),
    pytest.param(RfMarked, {"pick": {"kind": "a", "marks": []}}, lambda m: m.pick.marks, [1], id="callable-discriminator"),
    pytest.param(RfFactory, {"meta": {}}, lambda m: m.meta, {"n": 1}, id="default-factory-taking-data"),
    pytest.param(RfPostDcHolder, {"dc": {"extra": []}}, lambda m: m.dc.extra, ["x"], id="dataclass-post-init"),
]


class TestNestedTimedeltaRunsUserCodeOnce:
    """#228 (unit): reading the SDK's duration below the top level never applies the user's code twice."""

    def test_the_row_reads_as_without_the_sdk_duration(self) -> None:
        expected = RfCents.model_validate(_cents_row(_TD))
        _assert_cents_read_once(expected)
        for got in (RfCents.model_validate(_cents_row(_SDK_TD)), RfCents.from_db(_cents_row(_SDK_TD))):
            assert isinstance(got, RfCents) and got.model_dump() == expected.model_dump(), got
            _assert_cents_read_once(got)

    def test_a_sub_model_whose_validator_changes_its_input(self) -> None:
        """No code of the user's on the holder itself: the sub-model's validator changes its own dict."""
        _assert_sub_read_once(RfCentsHolder.model_validate({"sub": {"price": 250, "td": _SDK_TD}}).sub)

    @_ROW_PATHS
    def test_a_write_sync_applies_the_sub_model_validator_once(self, extra: dict[str, Any]) -> None:
        holder = RfCentsHolder(id="k")
        holder._apply_record({"id": RecordID("rf_cents_holder", "k"), "sub": {"price": 250, "td": _SDK_TD}, **extra})
        _assert_sub_read_once(holder.sub)

    @pytest.mark.parametrize(
        "value",
        [
            {"price": 1999, "td": "soon"},  # refused before any conversion
            {"price": 1999, "td": _SDK_TD, "qty": "x"},  # the duration converts, the value is still refused
        ],
        ids=["no-conversion", "after-the-conversion"],
    )
    def test_a_value_that_does_not_validate_is_applied_as_received(self, value: dict[str, Any]) -> None:
        received = copy.deepcopy(value)
        assert RfCentsHolder._validated_on_sync("sub", value) is value
        assert value == received, "the sub-model's validator did not change the value applied"

    def test_a_row_that_cannot_be_copied_is_refused_as_before(self) -> None:
        """No copy to validate first, so no second run: the SDK's duration is refused, as without the conversion."""
        with pytest.raises(ValidationError) as caught:
            RfCents.model_validate({"sub": {"td": _SDK_TD}, "anything": threading.Lock()})
        assert [e["loc"] for e in caught.value.errors()] == [("sub", "td")]

    def test_a_duration_a_validator_moved_is_refused_as_before(self) -> None:
        """The second run starts from the row as received: a value the first run found elsewhere is not guessed."""

        class RfShifted(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="rf_shifted")

            id: str | None = None
            tds: list[timedelta] = []

            @field_validator("tds", mode="before")
            @classmethod
            def _shift(cls, value: Any) -> Any:
                value.insert(0, timedelta(0))
                return value

        with pytest.raises(ValidationError):
            RfShifted.model_validate({"tds": [_SDK_TD, surrealdb.Duration(1000)]})
        assert RfShifted.model_validate({"tds": [_TD]}).tds == [timedelta(0), _TD]

    def test_only_a_model_running_input_code_on_an_sdk_duration_copies_the_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        copied: list[int] = []
        real = copy.deepcopy

        def counting(value: Any, *args: Any) -> Any:
            copied.append(id(value))
            return real(value, *args)

        monkeypatch.setattr(model_base.copy, "deepcopy", counting)
        plain = {"sub": {"td": _SDK_TD}, "tds": [_SDK_TD]}
        assert RfTdHolder.model_validate(plain).tds == [_TD] and id(plain) not in copied
        without_duration = _cents_row(_TD)
        _assert_cents_read_once(RfCents.model_validate(without_duration))
        assert id(without_duration) not in copied
        with_duration = _cents_row(_SDK_TD)
        _assert_cents_read_once(RfCents.model_validate(with_duration))
        assert copied.count(id(with_duration)) == 1

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            (RfTdHolder, False),
            (RfIssueHolder, False),  # the ORM's ``Duration`` validator is the ORM's own
            (RfLink, False),  # so is an edge's endpoint validator
            (RfPrivate, False),  # Pydantic's own ``model_post_init`` (private attributes)
            (RfSerializedOnly, False),  # a serializer never sees the input
            (RfAfterOnly, True),  # an ``after`` validator gets the input itself when Pydantic passes it through
            (RfCents, True),  # a model ``mode="before"`` validator, a field one, a ``BeforeValidator``
            (RfCentsHolder, True),  # a sub-model's ``mode="before"`` validator
            (RfTagged, True),  # a sub-model's ``after`` validator
            (RfCounted, True),  # a sub-model's ``model_post_init``
            (RfBumped, True),  # an ``AfterValidator``
            (RfMarked, True),  # a callable discriminator
            (RfFactory, True),  # a ``default_factory`` that takes the data
            (RfPostDcHolder, True),  # a dataclass ``__post_init__``
        ],
    )
    def test_models_that_run_code_on_their_input(self, model: type[BaseSurrealModel], expected: bool) -> None:
        assert model._runs_input_code() is expected

    @pytest.mark.parametrize(
        "annotation",
        [
            Annotated[list[str], BeforeValidator(_prepend_all)],
            Annotated[list[str], WrapValidator(lambda value, handler: handler(value))],
            Annotated[list[str], PlainValidator(_prepend_all)],
            Annotated[list[str], pydantic.AfterValidator(_prepend_all)],
        ],
        ids=["before", "wrap", "plain", "after"],
    )
    def test_each_kind_of_input_code(self, annotation: Any) -> None:
        model = pydantic.create_model("RfInputCode", __base__=BaseSurrealModel, id=(str | None, None), f=(annotation, []))
        assert model._runs_input_code() is True

    def test_anything_unexpected_counts_as_input_code(self) -> None:
        assert model_base._schema_runs_input_code({"type": "function-before", "function": None}) is True
        assert model_base._schema_runs_input_code({"type": "function-after", "function": {"function": _bump}}) is True
        assert model_base._schema_runs_input_code("not a schema") is True
        assert model_base._schema_runs_input_code({"type": "model", "cls": RfOwner, "post_init": "missing"}) is True
        assert model_base._schema_runs_input_code({"type": "model", "cls": RfOwner, "custom_init": True}) is True
        assert model_base._schema_runs_input_code({"type": "dataclass", "cls": RfOwner, "post_init": True}) is True
        assert model_base._schema_runs_input_code({"type": "tagged-union", "discriminator": "kind"}) is False
        assert model_base._schema_runs_input_code({"type": "default", "default_factory": _derived_from}) is False

    def test_an_incomplete_model_is_not_cached(self) -> None:
        class RfIncompleteInput(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="rf_incomplete_input")

            id: str | None = None
            later: "RfMissingInput | None" = None  # type: ignore[name-defined]  # noqa: F821

        assert RfIncompleteInput._runs_input_code() is True
        assert RfIncompleteInput not in model_base._INPUT_CODE

    # Code of the user's that receives the row's own object: ``after`` code on a passed-through value.

    @pytest.mark.parametrize(("model", "fields", "read", "expected"), _ONCE_CASES)
    def test_a_read_applies_the_code_once(
        self, model: type[BaseSurrealModel], fields: dict[str, Any], read: Callable[[Any], Any], expected: Any
    ) -> None:
        got = model.from_db(_steps_row(model, **fields))
        assert read(got) == expected
        assert got.steps == [RfStep(wait=timedelta(seconds=1))]  # type: ignore[union-attr]

    @pytest.mark.parametrize(("model", "fields", "read", "expected"), _ONCE_CASES[:2])
    def test_a_write_sync_of_the_row_applies_the_code_once(
        self, model: type[BaseSurrealModel], fields: dict[str, Any], read: Callable[[Any], Any], expected: Any
    ) -> None:
        row = _steps_row(model, **fields)
        received = copy.deepcopy(row)
        assert read(model._validated_row(row)) == expected
        holder = model(id="k")
        holder._apply_record(row)
        assert read(holder) == expected and row == received, "the row stays as received"

    def test_a_list_of_sub_models_validated_alone_applies_the_code_once(self) -> None:
        value = [{"extra": []}, {"wait": surrealdb.Duration(10**9)}]
        received = copy.deepcopy(value)
        expected = [(RfTaggedStep, ["x"], None), (RfTaggedStep, None, timedelta(seconds=1))]
        got = RfTaggedSteps._validated_on_sync("subs", value)
        assert [(type(s), s.extra, s.wait) for s in got] == expected
        assert value == received, "the value stays as received"
        holder = RfTaggedSteps(id="k")
        holder._apply_record({"id": RecordID("rf_tagged_steps", "k"), "subs": value, "n": "x"})  # the row fails
        assert [(type(s), s.extra, s.wait) for s in holder.subs] == expected and value == received

    def test_a_pydantic_post_init_is_not_the_users(self) -> None:
        schema = RfPrivate.__pydantic_core_schema__
        assert model_base._schema_runs_input_code(schema) is False
        assert RfPrivate._may_refuse_sdk_durations() is True and RfPrivate.model_validate({"tds": [_SDK_TD]}).tds == [_TD]

    # Only a model whose validation can refuse the SDK's duration takes the retry (#228).

    @pytest.mark.parametrize(
        ("annotation", "expected"),
        [
            (timedelta, False),  # ``set_data`` converts a top-level one
            (timedelta | None, False),
            (Optional[Annotated[timedelta, Field(ge=timedelta(0))]], False),  # noqa: UP045 (typing.Union on purpose)
            (Duration | None, False),  # the ORM's ``Duration`` reads the SDK's own
            (RfOwner | None, False),
            (timedelta | str, True),
            (list[timedelta], True),
            (dict[str, timedelta], True),
            (RfStep | None, True),  # a sub-model with a ``timedelta``
            (RfTdData | None, True),  # a dataclass
            (RfTdDict | None, True),  # a ``TypedDict``
            (Annotated[timedelta, pydantic.AfterValidator(lambda value: value)], True),  # not reached through defaults only
        ],
    )
    def test_models_that_may_refuse_the_sdk_duration(self, annotation: Any, expected: bool) -> None:
        model = pydantic.create_model("RfRefuses", __base__=BaseSurrealModel, id=(str | None, None), f=(annotation, None))
        assert model._may_refuse_sdk_durations() is expected
        assert model._may_refuse_sdk_durations() is model_base._MAY_REFUSE_DURATIONS[model], "cached per class"

    def test_an_aliased_top_level_timedelta_does_not_refuse(self) -> None:
        model = pydantic.create_model(
            "RfAliasedTop", __base__=BaseSurrealModel, id=(str | None, None), t=(timedelta | None, Field(None, alias="tCol"))
        )
        assert model._may_refuse_sdk_durations() is False
        assert model.model_validate({"tCol": _SDK_TD}).t == _TD  # type: ignore[attr-defined]

    def test_a_timedelta_in_a_sub_model_aliased_column(self) -> None:
        assert RfTdHolder._may_refuse_sdk_durations() is True and RfKinds._may_refuse_sdk_durations() is True

    def test_anything_unexpected_may_refuse(self) -> None:
        assert model_base._schema_refuses_sdk_durations("not a schema", RfOwned, frozenset()) is True
        assert model_base._schema_refuses_sdk_durations({"type": "timedelta"}, RfOwned, frozenset()) is True
        assert model_base._schema_refuses_sdk_durations({"type": "any"}, RfOwned, frozenset()) is False
        # The class's ``model-fields`` cannot be found, or its node is not what Pydantic builds.
        td: dict[str, Any] = {"type": "timedelta"}
        fields = {"type": "model-fields", "fields": {"t": {"type": "model-field", "schema": td}}}
        model = {"type": "model", "cls": RfOwned, "schema": {"type": "function-wrap", "schema": fields}}
        assert model_base._schema_refuses_sdk_durations(model, RfOwned, frozenset({"t"})) is True
        assert model_base._schema_refuses_sdk_durations({**model, "schema": fields}, RfOwned, frozenset({"t"})) is False
        assert model_base._schema_refuses_sdk_durations({"type": "model", "cls": RfOwned}, RfOwned, frozenset({"t"})) is True

    def test_an_incomplete_model_may_refuse_and_is_not_cached(self) -> None:
        class RfIncompleteRefuses(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="rf_incomplete_refuses")

            id: str | None = None
            later: "RfMissingRefuses | None" = None  # type: ignore[name-defined]  # noqa: F821

        assert RfIncompleteRefuses._may_refuse_sdk_durations() is True
        assert RfIncompleteRefuses not in model_base._MAY_REFUSE_DURATIONS

    def test_a_model_that_cannot_refuse_keeps_what_the_caller_passed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        owner, meta = RfOwner(name="o"), {"k": [1]}
        owned = RfOwned(owner=owner, meta=meta, ttl=surrealdb.Duration(10**9))  # type: ignore[arg-type]
        assert owned.owner is owner and owned.meta is meta and owned.ttl == Duration(10**9)
        calls: list[Any] = []
        real = copy.deepcopy

        def counting(value: Any, *args: Any) -> Any:
            calls.append(value)
            return real(value, *args)

        monkeypatch.setattr(model_base.copy, "deepcopy", counting)
        row = {"id": RecordID("rf_owned", "k"), "owner": {"name": "o"}, "meta": {"k": [1]}, "ttl": surrealdb.Duration(10**9)}
        read = RfOwned.from_db(row)
        assert isinstance(read, RfOwned) and read.ttl == Duration(10**9) and read.owner == RfOwner(name="o")
        assert calls == [], "a read copies nothing"


class TestNestedTimedeltaRunsUserCodeOnceE2E:
    """#228: a model whose validators change their input in place reads a stored duration below the top level."""

    @pytest.mark.asyncio
    async def test_get_and_a_write_sync(self) -> None:
        rid = RecordID("rf_cents", "k")
        async with orm_client("rf_cents") as client:
            # ``encoding.encode``: the SDK's own encoder sends its ``Duration`` in a form the server refuses.
            await client.query("CREATE $rid CONTENT $c RETURN NONE;", {"rid": rid, "c": encoding.encode(_cents_row(_SDK_TD))})
            got = await RfCents.objects().get("k")
            [listed] = await RfCents.objects().all()
            for cents in (got, listed):
                _assert_cents_read_once(cents)
            await got.merge(anything=1)  # a write sync: the sub-model gets what a read gives
            _assert_sub_read_once(got.sub)
            stored = await _raw(rid)
            assert stored["price"] == 1999 and stored["tags"] == ["a"] and type(stored["sub"]["td"]) is surrealdb.Duration

    @pytest.mark.asyncio
    async def test_an_after_validator_on_a_passed_through_value(self) -> None:
        rid = RecordID("rf_tagged", "k")
        async with orm_client("rf_tagged") as client:
            row = {k: v for k, v in _steps_row(RfTagged, sub={"extra": []}).items() if k != "id"}
            await client.query("CREATE $rid CONTENT $c RETURN NONE;", {"rid": rid, "c": encoding.encode(row)})
            got = await RfTagged.objects().get("k")
            assert got.sub is not None and got.sub.extra == ["x"], got
            assert got.steps == [RfStep(wait=timedelta(seconds=1))]
            await got.merge(n=1)  # a write sync: the sub-model gets what a read gives
            assert got.sub is not None and got.sub.extra == ["x"] and got.n == 1, got
            stored = await _raw(rid)
            assert stored["sub"] == {"extra": []} and type(stored["steps"][0]["wait"]) is surrealdb.Duration


# --- a write sync runs the model's own validators, as a read does (#228) --------------------------


class RfSettings(BaseModel):
    """Settings stored under ``theme`` by an older version of the app; the holding model migrates them."""

    color_theme: str = "light"
    font: int = 12


class RfProfile(BaseSurrealModel):
    """A model that migrates its sub-model's old shape in a ``mode="before"`` validator, in place, as is common."""

    model_config = SurrealConfigDict(table_name="rf_profile")

    id: str | None = None
    settings: RfSettings = RfSettings()
    name: str = ""

    @model_validator(mode="before")
    @classmethod
    def _migrate_theme(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("settings"), dict) and "theme" in data["settings"]:
            data["settings"]["color_theme"] = data["settings"].pop("theme")
        return data


class RfClamped(BaseModel):
    n: int


class RfClamping(BaseSurrealModel):
    """A model whose own ``field_validator`` reshapes its sub-model after Pydantic built it."""

    model_config = SurrealConfigDict(table_name="rf_clamping")

    id: str | None = None
    sub: RfClamped | None = None
    name: str = ""

    @field_validator("sub", mode="after")
    @classmethod
    def _clamp(cls, value: RfClamped | None) -> RfClamped | None:
        return value if value is None or value.n <= 10 else RfClamped(n=10)


class RfNamed(BaseSurrealModel):
    """A model whose own validators touch other fields only: its sub-model is validated alone when the row fails."""

    model_config = SurrealConfigDict(table_name="rf_named")

    id: str | None = None
    sub: RfPlainSub | None = None
    name: str = ""

    @field_validator("name")
    @classmethod
    def _no_bad_name(cls, value: str) -> str:
        if value == "bad":
            raise ValueError("bad name")
        return value


class RfStarred(BaseSurrealModel):
    """A ``field_validator("*")`` reaches the sub-model field too."""

    model_config = SurrealConfigDict(table_name="rf_starred")

    id: str | None = None
    sub: RfPlainSub | None = None
    n: int = 0

    @field_validator("*")
    @classmethod
    def _keep(cls, value: Any) -> Any:
        return value


class RfPostInit(BaseSurrealModel):
    """A ``model_post_init`` hook may reshape any field, as a model validator does."""

    model_config = SurrealConfigDict(table_name="rf_post_init")

    id: str | None = None
    sub: RfPlainSub | None = None
    n: int = 0

    def model_post_init(self, context: Any, /) -> None:
        if self.sub is not None and not self.sub.name:
            self.sub = self.sub.model_copy(update={"name": "unnamed"})


#: A stored ``RfProfile`` row in the old shape.
_OLD_SETTINGS: dict[str, Any] = {"theme": "dark", "font": 14}


class TestSubModelSyncFollowsTheRead:
    """#228 (unit): the sync gives what ``get()`` gives, and keeps the value as received when it cannot know it."""

    def test_a_migrating_model_validator_runs(self) -> None:
        profile = RfProfile(id="p")
        row = {"id": RecordID("rf_profile", "p"), "settings": dict(_OLD_SETTINGS), "name": "x"}
        profile._apply_record(row)
        expected = RfProfile.model_validate({"settings": dict(_OLD_SETTINGS)}).settings
        assert profile.settings == RfSettings(color_theme="dark", font=14) == expected
        # The row is neither mutated by the validators nor rid of its native id.
        assert row == {"id": RecordID("rf_profile", "p"), "settings": _OLD_SETTINGS, "name": "x"}
        assert type(profile.id) is RecordID and profile.name == "x"

    def test_a_field_validator_of_the_model_runs(self) -> None:
        clamping = RfClamping()
        clamping._apply_record({"sub": {"n": 50}})
        assert clamping.sub == RfClamped(n=10) == RfClamping.model_validate({"sub": {"n": 50}}).sub

    def test_a_row_that_fails_keeps_what_a_model_validator_may_reshape(self) -> None:
        """The row fails (``name``), and the model's own ``mode="before"`` validator may reshape anything: as received."""
        profile = RfProfile()
        profile._apply_record({"settings": dict(_OLD_SETTINGS), "name": 3})
        assert profile.settings == _OLD_SETTINGS and profile.name == 3

    def test_a_row_that_fails_keeps_what_a_field_validator_may_reshape(self) -> None:
        clamping = RfClamping()
        clamping._apply_record({"sub": {"n": 50}, "name": 3})
        assert clamping.sub == {"n": 50}
        starred = RfStarred()
        starred._apply_record({"sub": {"name": "a"}, "n": "x"})
        assert starred.sub == {"name": "a"}

    def test_a_row_that_fails_keeps_what_model_post_init_may_reshape(self) -> None:
        assert RfPostInit.model_validate({"sub": {"name": ""}}).sub == RfPlainSub(name="unnamed")
        post_init = RfPostInit()
        post_init._apply_record({"sub": {"name": ""}})
        assert post_init.sub == RfPlainSub(name="unnamed"), "the row validates: the hook ran"
        post_init._apply_record({"sub": {"name": ""}, "n": "x"})
        assert post_init.sub == {"name": ""}

    def test_a_row_that_fails_still_validates_the_other_sub_models(self) -> None:
        """Validators on other fields, and the ORM's own (``set_data``, ``check_config``), leave the field to its adapter."""
        named = RfNamed()
        named._apply_record({"sub": {"name": "a"}, "name": "bad"})
        assert named.sub == RfPlainSub(name="a") and named.name == "bad"
        holder = RfHolder(id="h", **HOLDER)
        holder._apply_record({"id": RecordID("rf_holder", "h"), "plain": {"name": "q"}, "n": "x"})
        assert holder.plain == RfPlainSub(name="q") and holder.n == "x"

    def test_reshaping_validators_are_found_once_per_class(self) -> None:
        assert RfProfile._sync_reshaped() is model_base._ALL_FIELDS
        assert RfPostInit._sync_reshaped() is model_base._ALL_FIELDS
        assert RfStarred._sync_reshaped() is model_base._ALL_FIELDS
        assert RfClamping._sync_reshaped() == frozenset({"sub"})
        assert RfNamed._sync_reshaped() == frozenset({"name"})
        assert RfHolder._sync_reshaped() == frozenset() and RfHolder._sync_reshaped() is RfHolder._sync_reshaped()


class TestSubModelSyncValidatorsE2E:
    """#228: a write sync gives what ``get()`` gives when the model's own validators reshape a sub-model."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sync", ["refresh", "merge"])
    async def test_a_migrating_model_validator_keeps_the_stored_choice(self, sync: str) -> None:
        async with orm_client("rf_profile") as client:
            rid = RecordID("rf_profile", "p")
            await client.query("CREATE $rid CONTENT $c RETURN NONE;", {"rid": rid, "c": {"settings": _OLD_SETTINGS}})
            profile = await RfProfile.objects().get("p")
            assert profile.settings == RfSettings(color_theme="dark", font=14)
            if sync == "refresh":
                await profile.refresh()
            else:
                await profile.merge(name="x")
            assert profile.settings == RfSettings(color_theme="dark", font=14)
            await profile.update()
            assert (await _raw(rid))["settings"] == {"color_theme": "dark", "font": 14}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sync", ["refresh", "merge"])
    async def test_a_field_validator_of_the_model_gives_what_a_read_gives(self, sync: str) -> None:
        async with orm_client("rf_clamping") as client:
            rid = RecordID("rf_clamping", "c")
            await client.query("CREATE $rid CONTENT $c RETURN NONE;", {"rid": rid, "c": {"sub": {"n": 50}}})
            clamping = await RfClamping.objects().get("c")
            if sync == "refresh":
                await clamping.refresh()
            else:
                await clamping.merge(name="x")
            assert clamping.sub == (await RfClamping.objects().get("c")).sub == RfClamped(n=10)


class TestAuthReadE2E:
    """AC-27 (second half): a ``timedelta`` default goes out as a duration and comes back a ``timedelta``."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("scheme", ["ws", "http"])
    async def test_signup_timedelta_default(self, scheme: str) -> None:
        async with _auth_model(RfUserTd, scheme):
            email = _unique_email()
            result = await RfUserTd.signup(email=email, password=AUTH_PASSWORD)
            assert type(result.user.trial) is timedelta and result.user.trial == timedelta(days=7)
            row = await _stored_user(email, "rf_user_td")
            assert type(row["trial"]) is surrealdb.Duration and row["trial"].elapsed == 7 * 86400 * 10**9


class TestNativeContractE2E:
    """AC-21: what the SDK and SurrealDB keep or change in each type, pinned on both lines."""

    @pytest.mark.asyncio
    async def test_precision_of_server_literals(self) -> None:
        async with orm_client("rf_event") as client:
            await client.query(SEED, {})
            event = await RfEvent.objects().get("s")
        assert type(event.ttl) is timedelta and event.ttl == timedelta(hours=1, minutes=30), "1h30m1ns floors to µs"
        assert type(event.grace) is Duration and event.grace.elapsed == 5400 * 10**9 + 1, "a Duration keeps the ns"
        assert event.at == datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC), "nanoseconds floor to µs"

    @pytest.mark.asyncio
    async def test_naive_datetime_reads_back_aware_utc(self) -> None:
        async with orm_client("rf_event"):
            saved = await RfEvent(id="naive", at=datetime(2026, 1, 2, 3, 4, 5)).save()
            event = await RfEvent.objects().get("naive")
        assert saved.at is not None and saved.at.tzinfo is not None and saved.at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        assert event.at is not None and event.at.tzinfo is not None
        assert event.at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_decimal_trailing_zeros_by_line(self) -> None:
        async with orm_client("rf_event"):
            await RfEvent(id="d", price=Decimal("12.50")).save()
            event = await RfEvent.objects().get("d")
            major = (await _server_version())[0]
        expected = "12.50" if major < 3 else "12.5"
        assert str(event.price) == expected, "SurrealDB changed decimal trailing zeros: update README section 24"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity"])
    async def test_non_finite_decimals_round_trip(self, text: str) -> None:
        async with orm_client("rf_nan"):
            await RfNan(id="x", price=Decimal(text)).save()
            got = await RfNan.objects().get("x")
        assert type(got.price) is Decimal
        assert got.price.is_nan() if text == "NaN" else got.price == Decimal(text)

    @pytest.mark.asyncio
    async def test_set_field_on_an_array_column(self) -> None:
        async with orm_client("rf_arr") as client:
            await client.query("DEFINE FIELD tags ON rf_arr TYPE array<string> VALUE $value.distinct();", {})
            await RfArr(id="x", tags={"b", "a"}).save()
            got = await RfArr.objects().get("x")
        assert type(got.tags) is set and got.tags == {"a", "b"}


# --- save() reflects the stored row, update() keeps the stored datetimes (#261) -------------------


@contextlib.asynccontextmanager
async def _rf_engine(kind: str, ddl: str, *tables: str) -> AsyncIterator[Any]:
    """*ddl* applied on the line under test (``ws``, *tables* dropped before and after) or on a fresh ``mem://``."""
    if kind == "mem":
        async with embedded_database() as client:
            await client.query(ddl, {})
            yield client
        return
    async with orm_client(*tables) as client:
        await client.query(ddl, {})
        yield client


async def _replace(instance: BaseSurrealModel, write: str, tx: bool) -> None:
    """``instance.update()`` or ``.upsert()``, directly or in a transaction (interactive on 3.x, buffered otherwise)."""
    if not tx:
        await getattr(instance, write)()
        return
    async with SurrealDBConnectionManager.transaction() as transaction:
        await getattr(instance, write)(tx=transaction)


class RfStampMeta(BaseModel):
    at: datetime | None = None
    seen: datetime | None = None


class RfStamp(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_stamp", server_fields=["created", "log"])

    id: str | None = None
    title: str
    created: datetime | None = None
    meta: RfStampMeta = Field(default_factory=RfStampMeta)
    log: list[datetime] | None = None


class RfStampLink(BaseSurrealEdge):
    model_config = SurrealConfigDict(table_name="rf_stamp_link")

    meta: RfStampMeta = Field(default_factory=RfStampMeta)


#: #261: ``READONLY`` datetimes the server fills on create (top level, in a sub-object, in an array), and an optional
#: sub-field it fills.
_RF_STAMP_DDL = (
    "DEFINE TABLE rf_stamp SCHEMAFULL;"
    "DEFINE FIELD title ON rf_stamp TYPE string;"
    "DEFINE FIELD created ON rf_stamp TYPE datetime DEFAULT time::now() READONLY;"
    "DEFINE FIELD log ON rf_stamp TYPE array<datetime> DEFAULT [time::now()] READONLY;"
    "DEFINE FIELD meta ON rf_stamp TYPE object DEFAULT {};"
    "DEFINE FIELD meta.at ON rf_stamp TYPE datetime DEFAULT time::now() READONLY;"
    "DEFINE FIELD meta.seen ON rf_stamp TYPE option<datetime> DEFAULT time::now();"
    "DEFINE TABLE rf_stamp_link TYPE RELATION SCHEMAFULL;"
    "DEFINE FIELD meta ON rf_stamp_link TYPE object DEFAULT {};"
    "DEFINE FIELD meta.at ON rf_stamp_link TYPE datetime DEFAULT time::now() READONLY;"
    "DEFINE FIELD meta.seen ON rf_stamp_link TYPE option<datetime> DEFAULT time::now();"
)
#: A row whose datetimes hold nanoseconds, which the SDK floors to the microsecond.
_RF_STAMP_SEED = (
    "CREATE rf_stamp:seed SET title = 't', created = d'2026-01-02T03:04:05.123456789Z', "
    "meta = {at: d'2026-01-02T03:04:05.987654321Z', seen: d'2026-01-02T03:04:05.000000001Z'}, "
    "log = [d'2026-01-02T03:04:05.000000999Z', d'2026-01-02T03:04:05.000001001Z'];"
)
_RF_STAMP_SEEDED = ["2026-01-02T03:04:05.123456789Z", "2026-01-02T03:04:05.987654321Z", "2026-01-02T03:04:05.000000001Z"]
#: Whether the seeded record's ``log`` still holds its nanoseconds.
_RF_LOG_SEEDED = "RETURN rf_stamp:seed.log == [d'2026-01-02T03:04:05.000000999Z', d'2026-01-02T03:04:05.000001001Z'];"
_RF_SEED = RecordID("rf_stamp", "seed")


async def _stamps(client: Any, rid: RecordID) -> object:
    """The stored datetimes of *rid* as the server writes them, nanoseconds included (``log`` as a whole)."""
    return await client.query(
        "SELECT VALUE [<string> created, <string> meta.at, <string> meta.seen, <string> log] FROM ONLY $rid;", {"rid": rid}
    )


async def _seed_kept(client: Any) -> bool:
    """Whether ``rf_stamp:seed`` still holds the datetimes ``_RF_STAMP_SEED`` wrote, to the nanosecond."""
    stamps = await _stamps(client, _RF_SEED)
    assert isinstance(stamps, list), stamps
    return stamps[:3] == _RF_STAMP_SEEDED and await client.query(_RF_LOG_SEEDED, {}) is True


class TestReplaceKeepsStoredDatetimesE2E:
    """#261 case 1: ``update()`` and ``upsert()`` of another field pass on a record whose ``READONLY`` datetimes hold
    nanoseconds (the SDK reads microseconds), after ``save()`` and after ``get()``, directly and in a transaction, on
    the line under test and on ``mem://``. The stored datetimes keep their nanoseconds; a changed one is still refused."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["ws", "mem"])
    @pytest.mark.parametrize("write", ["update", "upsert"])
    @pytest.mark.parametrize("tx", [False, True], ids=["direct", "tx"])
    async def test_readonly_datetimes_are_kept(self, kind: str, write: str, tx: bool) -> None:
        async with _rf_engine(kind, _RF_STAMP_DDL, "rf_stamp", "rf_stamp_link") as client:
            await client.query(_RF_STAMP_SEED, {})
            saved = await RfStamp(id="saved", title="t").save()
            seeded = await RfStamp.objects().get("seed")
            for instance in (saved, seeded):
                rid = instance._record_id()
                assert rid is not None
                before = await _stamps(client, rid)
                instance.title = write
                await _replace(instance, write, tx)
                assert await _stamps(client, rid) == before, rid
                assert (await RfStamp.objects().get(rid.id)).title == write
            assert await _seed_kept(client)
            assert seeded.log is not None and [d.microsecond for d in seeded.log] == [0, 1]

            assert seeded.created is not None
            seeded.created += timedelta(microseconds=1)
            with pytest.raises((SurrealError, SurrealDbError), match="readonly"):
                await _replace(seeded, write, tx)
            assert await _seed_kept(client)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["ws", "mem"])
    async def test_an_edge_update_keeps_them(self, kind: str) -> None:
        async with _rf_engine(kind, _RF_STAMP_DDL, "rf_stamp", "rf_stamp_link") as client:
            await client.query(_RF_STAMP_SEED, {})
            link = await RfStampLink(in_=_RF_SEED, out=_RF_SEED).save()
            assert isinstance(link.meta.at, datetime) and isinstance(link.meta.seen, datetime), link.meta
            rid = link._record_id()
            assert rid is not None
            query = "SELECT VALUE [<string> meta.at, <string> meta.seen] FROM ONLY $rid;"
            before = await client.query(query, {"rid": rid})
            await link.update()
            assert await client.query(query, {"rid": rid}) == before


class TestSaveReadsBackTheStoredRowE2E:
    """#261 case 2: right after ``save()`` the instance holds what the server stored, sub-fields a ``DEFAULT`` filled
    included, with an explicit id, an auto id and in an interactive transaction; a following ``update()`` keeps them.
    Before #261 only the explicit-id ``save()`` (directly or in a transaction) missed them; the auto-id one already
    applied the whole row. A buffered transaction (``mem://``, 2.x) has no row before commit: the instance keeps its
    values, as documented."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["ws", "mem"])
    @pytest.mark.parametrize("how", ["explicit-id", "auto-id", "tx"])
    async def test_default_sub_fields(self, kind: str, how: str) -> None:
        async with _rf_engine(kind, _RF_STAMP_DDL, "rf_stamp", "rf_stamp_link") as client:
            if how == "tx":
                async with SurrealDBConnectionManager.transaction() as tx:
                    stamp = await RfStamp(id="x", title="t").save(tx=tx)
                    interactive = tx.is_interactive
                if not interactive:
                    assert (stamp.created, stamp.meta.at, stamp.meta.seen, stamp.log) == (None, None, None, None)
                    return
            else:
                stamp = await RfStamp(id="x" if how == "explicit-id" else None, title="t").save()
            assert isinstance(stamp.meta, RfStampMeta), stamp.meta
            assert stamp.log is not None and len(stamp.log) == 1, stamp.log
            for value in (stamp.created, stamp.meta.at, stamp.meta.seen, stamp.log[0]):
                assert type(value) is datetime and value.tzinfo is not None, stamp
            rid = stamp._record_id()
            assert rid is not None
            if how != "auto-id":
                assert stamp.id == "x"  # an explicit id stays as given
            got = await RfStamp.objects().get(rid.id)
            assert stamp.model_dump(exclude={"id"}) == got.model_dump(exclude={"id"})

            before = await _stamps(client, rid)
            stamp.title = "u"
            await stamp.update()
            assert await _stamps(client, rid) == before


class RfShapes(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_shapes")

    id: str | None = None
    pair: tuple[int, str] = (0, "")
    tags: set[str] = set()
    day: date | None = None
    hours: Range[int] | None = None
    nested: list[tuple[int, int]] = []
    n: int = 0


#: Values whose type the SDK does not read back: a ``list``, a ``list``, a midnight ``datetime``, the SDK's ``Range``.
_SHAPES: dict[str, Any] = {
    "pair": (1, "a"),
    "tags": {"b", "a"},
    "day": date(2026, 1, 2),
    "hours": Range(BoundIncluded(9), BoundExcluded(17)),
    "nested": [(1, 2)],
}


async def _shapes_after(write: str) -> RfShapes:
    """An ``RfShapes`` holding ``_SHAPES``, after *write*."""
    if write == "save-auto-id":
        return await RfShapes(**_SHAPES).save()
    if write == "upsert":
        return await RfShapes(id="s", **_SHAPES).upsert()
    if write == "tx":
        async with SurrealDBConnectionManager.transaction() as tx:
            if not tx.is_interactive:
                pytest.skip("a buffered transaction returns no row before commit")
            return await RfShapes(id="s", **_SHAPES).save(tx=tx)
    shapes = await RfShapes(id="s", **_SHAPES).save()
    if write == "merge":
        await shapes.merge(n=1)
    elif write == "patch":
        await shapes.patch([{"op": "replace", "path": "/n", "value": 2}])
    elif write == "refresh":
        await shapes.refresh()
    return shapes


class TestWriteSyncGivesTheReadTypesE2E:
    """#261 case 3: after a write that returns the row, a ``tuple``, ``set``, ``date`` or ``Range`` field (also inside
    a container) holds the type ``get()`` gives, on the line under test and on ``mem://``."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["ws", "mem"])
    @pytest.mark.parametrize("write", ["save-auto-id", "save-explicit-id", "upsert", "merge", "patch", "refresh", "tx"])
    async def test_read_types(self, kind: str, write: str) -> None:
        async with _rf_engine(kind, "DEFINE TABLE rf_shapes SCHEMALESS;", "rf_shapes"):
            shapes = await _shapes_after(write)
            rid = shapes._record_id()
            assert rid is not None
            got = await RfShapes.objects().get(rid.id)
            for name, value in _SHAPES.items():
                assert type(getattr(got, name)) is type(value) and getattr(got, name) == value, name
                assert type(getattr(shapes, name)) is type(value) and getattr(shapes, name) == value, name
            assert type(shapes.nested[0]) is tuple
            with warnings.catch_warnings():
                warnings.simplefilter("error")  # no PydanticSerializationUnexpectedValue on the full write
                await shapes.update()


class RfShapesRejected(BaseSurrealModel):
    """Its own validator refuses every row whose ``n`` is negative."""

    model_config = SurrealConfigDict(table_name="rf_shapes_rejected")

    id: str | None = None
    pair: tuple[int, str] = (0, "")
    n: int = 0

    @model_validator(mode="after")
    def _non_negative(self) -> Self:
        if self.n < 0:
            raise ValueError("n must not be negative")
        return self


class TestWriteSyncGivesTheReadTypes:
    """#261 case 3 (unit): the rule behind the E2E above, and its fallback when the row does not validate."""

    def test_sync_fields(self) -> None:
        assert set(RfShapes._sync_fields()) == {"pair", "tags", "day", "hours", "nested", "n"}

    def test_a_row_that_fails_validates_each_field_alone(self) -> None:
        shapes = RfShapes()
        row = {
            "pair": [1, "a"],
            "tags": ["a"],
            "day": datetime(2026, 1, 2, tzinfo=UTC),
            "hours": surrealdb.Range(BoundIncluded(9), BoundExcluded(17)),
            "n": "x",
        }
        shapes._apply_record(row)
        assert shapes.pair == (1, "a") and shapes.tags == {"a"} and type(shapes.day) is date
        assert type(shapes.hours) is Range and shapes.n == "x"

    def test_a_rejecting_model_validator_never_raises(self) -> None:
        rejected = RfShapesRejected()
        rejected._apply_record({"pair": [1, "a"], "n": -1})
        assert rejected.pair == [1, "a"] and rejected.n == -1  # as received: the model's validator may reshape anything
        rejected._apply_record({"pair": [2, "b"], "n": 1})
        assert rejected.pair == (2, "b") and rejected.n == 1


class RfStatus(enum.StrEnum):
    ACTIVE = "active"
    IDLE = "idle"


class RfLevel(enum.IntEnum):
    LOW = 1
    HIGH = 2


class RfTyped(BaseSurrealModel):
    """Scalars whose read type is not the SDK's value: a ``StrEnum``, an ``IntEnum``, a list of enums and a ``float``
    stored in a ``decimal`` column (the SDK reads a ``str``, an ``int``, ``str`` items and a ``Decimal``)."""

    model_config = SurrealConfigDict(table_name="rf_typed")

    id: str | None = None
    status: RfStatus = RfStatus.ACTIVE
    level: RfLevel = RfLevel.HIGH
    statuses: list[RfStatus] = Field(default_factory=lambda: [RfStatus.IDLE])
    amount: float = 1.5


class RfTypedLink(BaseSurrealEdge):
    model_config = SurrealConfigDict(table_name="rf_typed_link")

    status: RfStatus = RfStatus.ACTIVE
    level: RfLevel = RfLevel.HIGH
    statuses: list[RfStatus] = Field(default_factory=lambda: [RfStatus.IDLE])
    amount: float = 1.5


_RF_TYPED_DDL = (
    "DEFINE TABLE rf_typed SCHEMALESS;"
    "DEFINE FIELD amount ON rf_typed TYPE decimal;"
    "DEFINE TABLE rf_typed_link TYPE RELATION SCHEMALESS;"
    "DEFINE FIELD amount ON rf_typed_link TYPE decimal;"
)


async def _typed_after(write: str) -> RfTyped | RfTypedLink:
    """An ``RfTyped`` (an ``RfTypedLink`` for ``edge``) after *write*, each a write that applies the returned row."""
    if write == "save-auto-id":
        return await RfTyped().save()
    if write == "tx":
        async with SurrealDBConnectionManager.transaction() as tx:
            if not tx.is_interactive:
                pytest.skip("a buffered transaction returns no row before commit")
            return await RfTyped(id="t").save(tx=tx)
    typed = await RfTyped(id="t").save()
    if write == "edge":
        return await RfTypedLink(in_=typed, out=typed).save()
    if write == "upsert":
        await typed.upsert()
    elif write == "merge":
        await typed.merge(amount=2.5)
    elif write == "refresh":
        await typed.refresh()
    return typed


class TestWriteSyncGivesTheReadScalarsE2E:
    """#261 review: after a write that applies the returned row, every field holds the type a read gives, not the
    SDK's value (an enum is the enum, not its ``str``/``int``; a ``float`` stored as ``decimal`` is a ``float``), so
    ``model_dump()``, ``update()`` and arithmetic behave as after ``get()``, on the line under test and on ``mem://``."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["ws", "mem"])
    @pytest.mark.parametrize("write", ["save-explicit-id", "save-auto-id", "tx", "edge", "upsert", "merge", "refresh"])
    async def test_read_types(self, kind: str, write: str) -> None:
        async with _rf_engine(kind, _RF_TYPED_DDL, "rf_typed", "rf_typed_link"):
            typed = await _typed_after(write)
            assert type(typed.status) is RfStatus and typed.status is RfStatus.ACTIVE
            assert type(typed.level) is RfLevel and typed.level is RfLevel.HIGH
            assert [type(item) for item in typed.statuses] == [RfStatus]
            assert type(typed.amount) is float and typed.amount * 1.1 > 1
            if isinstance(typed, RfTyped):
                rid = typed._record_id()
                assert rid is not None
                got = await RfTyped.objects().get(rid.id)
                assert typed.model_dump(exclude={"id"}) == got.model_dump(exclude={"id"})
                names = ("status", "level", "amount")
                assert [type(getattr(typed, name)) for name in names] == [type(getattr(got, name)) for name in names]
            with warnings.catch_warnings():
                warnings.simplefilter("error")  # no PydanticSerializationUnexpectedValue ("Expected `enum`")
                typed.model_dump(exclude={"id"})  # a returned id stays the native RecordID (README, record IDs)
                await typed.update()


class TestWriteSyncGivesTheReadScalars:
    """#261 review (unit): the whole returned row is validated as a read validates it; when it does not validate,
    each field alone, and a value that still does not validate is applied as received."""

    def test_every_field_takes_the_read_type(self) -> None:
        typed = RfTyped(id="t")
        row = {"id": RecordID("rf_typed", "t"), "status": "idle", "level": 1, "statuses": ["active"], "amount": Decimal("2.5")}
        typed._apply_record(row)
        assert typed.status is RfStatus.IDLE and typed.level is RfLevel.LOW and typed.statuses == [RfStatus.ACTIVE]
        assert type(typed.amount) is float and typed.amount == 2.5
        assert typed.id == RecordID("rf_typed", "t"), "the id stays the native RecordID"

    def test_a_row_that_fails_validates_each_field_alone(self) -> None:
        typed = RfTyped(id="t")
        typed._apply_record({"status": "idle", "level": "nope", "amount": Decimal("2.5")})
        assert typed.status is RfStatus.IDLE and type(typed.amount) is float
        assert typed.level == "nope", "a value that does not validate is applied as received"


class RfFullName(BaseSurrealModel):
    """A Pydantic ``@computed_field``: ``model_dump()`` writes the property's value, so the row a write returns holds
    that column."""

    model_config = SurrealConfigDict(table_name="rf_full_name")

    id: str | None = None
    first: str = "Ada"
    last: str = "Lovelace"

    @pydantic.computed_field  # type: ignore[prop-decorator]
    @property
    def full(self) -> str:
        return f"{self.first} {self.last}"


class RfFullNameLink(BaseSurrealEdge):
    model_config = SurrealConfigDict(table_name="rf_full_name_link")

    first: str = "Ada"

    @pydantic.computed_field  # type: ignore[prop-decorator]
    @property
    def full(self) -> str:
        return self.first.upper()


_RF_FULL_NAME_DDL = "DEFINE TABLE rf_full_name SCHEMALESS; DEFINE TABLE rf_full_name_link TYPE RELATION SCHEMALESS;"


async def _full_name_after(write: str) -> RfFullName | RfFullNameLink:
    """An ``RfFullName`` (an ``RfFullNameLink`` for ``edge``) after *write*, each a write that returns the row."""
    if write == "save-auto-id":
        return await RfFullName().save()
    if write == "tx":
        async with SurrealDBConnectionManager.transaction() as tx:
            return await RfFullName(id="p").save(tx=tx)  # interactive: the row is applied; buffered: none yet
    person = await RfFullName(id="p").save()
    if write == "edge":
        return await RfFullNameLink(in_=person, out=person).save()
    if write == "merge":
        await person.merge(first="Grace")
    elif write == "refresh":
        await person.refresh()
    elif write == "upsert":
        await person.upsert()
    return person


class RfShadow(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_shadow")

    id: str | None = None
    n: int = 0


class RfShadowExtra(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_shadow_extra", extra="allow")

    id: str | None = None
    n: int = 0


#: Columns the models do not declare, filled by a ``DEFAULT``: one named like a ``BaseModel`` method.
_RF_SHADOW_DDL = (
    "DEFINE TABLE rf_shadow SCHEMALESS; DEFINE FIELD json ON rf_shadow DEFAULT 'shadow';"
    "DEFINE TABLE rf_shadow_extra SCHEMALESS; DEFINE FIELD json ON rf_shadow_extra DEFAULT 'shadow';"
    "DEFINE FIELD note ON rf_shadow_extra DEFAULT 'kept';"
)


class TestWriteSyncAppliesDeclaredFieldsE2E:
    """#261 review: the row a write returns is applied to the fields the model declares and, under ``extra="allow"``,
    to its extras, as a read holds them. A column holding a Pydantic ``@computed_field`` (``model_dump()`` writes it)
    or named like a ``BaseModel`` method never becomes an attribute of the instance: the write does not raise once the
    record has changed, ``post_save`` fires, and the method stays a method; on the line under test and on ``mem://``."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["ws", "mem"])
    @pytest.mark.parametrize("write", ["save-explicit-id", "save-auto-id", "tx", "edge", "merge", "refresh", "upsert"])
    async def test_a_computed_field_column(self, kind: str, write: str) -> None:
        saved: list[str] = []

        async def on_save(sender: Any, instance: Any, created: bool, **kwargs: Any) -> None:
            saved.append(type(instance).__name__)

        post_save.connect(RfFullName)(on_save)
        post_save.connect(RfFullNameLink)(on_save)
        try:
            async with _rf_engine(kind, _RF_FULL_NAME_DDL, "rf_full_name", "rf_full_name_link") as client:
                instance = await _full_name_after(write)
                rid = instance._record_id()
                assert rid is not None
                if isinstance(instance, RfFullName):
                    assert instance.full == f"{instance.first} {instance.last}"
                    assert (await RfFullName.objects().get(rid.id)).full == instance.full
                else:
                    assert instance.full == "ADA"
                assert "full" not in instance.__dict__
                stored = await client.query("SELECT VALUE full FROM ONLY $rid;", {"rid": rid})
                assert stored == ("Ada Lovelace" if isinstance(instance, RfFullName) else "ADA"), stored
        finally:
            post_save.clear(RfFullName)
            post_save.clear(RfFullNameLink)
        assert saved[-1] == type(instance).__name__, saved

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["ws", "mem"])
    @pytest.mark.parametrize("write", ["save-explicit-id", "save-auto-id", "refresh"])
    async def test_a_column_named_like_a_method(self, kind: str, write: str) -> None:
        async with _rf_engine(kind, _RF_SHADOW_DDL, "rf_shadow", "rf_shadow_extra"):
            for model in (RfShadow, RfShadowExtra):
                instance = await model(id=None if write == "save-auto-id" else "x").save()
                if write == "refresh":
                    await instance.refresh()
                assert callable(instance.json) and "json" not in instance.__dict__, model
                rid = instance._record_id()
                assert rid is not None
                assert instance.model_extra == (await model.objects().get(rid.id)).model_extra, model
            assert instance.model_extra == {"json": "shadow", "note": "kept"}
            instance.n = 1
            await instance.update()  # the extras are written back: the record keeps them
            assert (await RfShadowExtra.objects().get(rid.id)).model_extra == {"json": "shadow", "note": "kept"}


# --- what an update() after a synced write writes back (#261 review) ------------------------------


class RfMixedColor(str, enum.Enum):  # noqa: UP042 (the mixed-in form is the point: it is not a StrEnum)
    RED = "red"


class RfMixedRank(int, enum.Enum):
    TOP = 1


class RfWriteBack(BaseSurrealModel):
    """A ``float`` field over a ``decimal`` column, ``(str, Enum)`` and ``(int, Enum)`` fields next to a ``StrEnum``,
    and, under ``extra="allow"``, an undeclared column a ``DEFAULT`` fills."""

    model_config = SurrealConfigDict(table_name="rf_write_back", extra="allow")

    id: str | None = None
    n: int = 0
    price: float = 0.0
    color: RfMixedColor = RfMixedColor.RED
    rank: RfMixedRank = RfMixedRank.TOP
    status: RfStatus = RfStatus.ACTIVE


_RF_WRITE_BACK_DDL = (
    "DEFINE TABLE rf_write_back SCHEMALESS; DEFINE FIELD price ON rf_write_back TYPE decimal;"
    "DEFINE FIELD note ON rf_write_back DEFAULT 'srv';"
)
#: More digits than a ``float`` holds: a ``decimal`` column stores them all.
_RF_EXACT = Decimal("0.12345678901234567890")


async def _write_back_after(client: Any, write: str) -> RfWriteBack:
    """An ``RfWriteBack`` after *write*, a call that applies the returned row. Except for ``save()``, the record is
    created first by a query, with the exact ``decimal`` and the enums' raw values, and the call does not send them."""
    if write == "save-explicit-id":
        return await RfWriteBack(id="w").save()
    if write == "save-auto-id":
        return await RfWriteBack().save()
    await client.query(
        "CREATE rf_write_back:w SET n = 0, price = $price, color = 'red', rank = 1, status = 'active';",
        {"price": _RF_EXACT},
    )
    instance = RfWriteBack(id="w")
    if write == "refresh":
        await instance.refresh()
    elif write == "merge":
        await instance.merge(n=1)
    elif write == "patch":
        await instance.patch([{"op": "replace", "path": "/n", "value": 1}])
    elif write == "atomic":
        await instance.atomic_increment("n")
    return instance


async def _stored_after_update(client: Any, instance: RfWriteBack) -> dict[str, Any]:
    """The row ``update()`` leaves once *instance* changed only ``n``."""
    rid = instance._record_id()
    assert rid is not None
    instance.n = 5
    await instance.update()
    row = await client.query("SELECT * FROM ONLY $rid;", {"rid": rid})
    assert isinstance(row, dict), row
    return row


class TestUpdateAfterASyncedWriteE2E:
    """#261 review: after a call that applies the returned row, the instance holds the values a read gives, and a
    following ``update()`` (a full replace) writes those values back, as after ``get()``. On the line under test and
    on ``mem://``; in 0.30.1 these calls kept the SDK's values and dropped the undeclared columns."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["ws", "mem"])
    @pytest.mark.parametrize("write", ["refresh", "merge", "patch", "atomic"])
    async def test_a_float_field_writes_back_its_rounding(self, kind: str, write: str) -> None:
        async with _rf_engine(kind, _RF_WRITE_BACK_DDL, "rf_write_back") as client:
            instance = await _write_back_after(client, write)
            stored = (await _stored_after_update(client, instance))["price"]
        assert type(stored) is Decimal and stored != _RF_EXACT, "declare Decimal to keep every digit"
        assert abs(stored - _RF_EXACT) < Decimal("1e-15"), stored
        assert type(instance.price) is float, instance.price

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["ws", "mem"])
    @pytest.mark.parametrize("write", ["save-explicit-id", "save-auto-id", "refresh", "merge", "patch", "atomic"])
    async def test_undeclared_columns_are_written_back(self, kind: str, write: str) -> None:
        async with _rf_engine(kind, _RF_WRITE_BACK_DDL, "rf_write_back") as client:
            instance = await _write_back_after(client, write)
            stored = await _stored_after_update(client, instance)
        assert stored.get("note") == "srv" and stored["n"] == 5, stored
        assert instance.model_extra == {"note": "srv"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["ws", "mem"])
    @pytest.mark.parametrize("write", ["save-auto-id", "refresh", "merge", "patch", "atomic"])
    async def test_a_mixed_in_enum_formats_as_its_member(self, kind: str, write: str) -> None:
        async with _rf_engine(kind, _RF_WRITE_BACK_DDL, "rf_write_back") as client:
            instance = await _write_back_after(client, write)
            assert instance.color == "red" and instance.rank == 1, "a mixed-in enum still equals its value"
            assert (f"{instance.color}", str(instance.rank)) == ("RfMixedColor.RED", "RfMixedRank.TOP")
            assert (f"{instance.status}", f"{instance.color.value}") == ("active", "red")  # StrEnum: the value
            stored = await _stored_after_update(client, instance)
        assert (stored["color"], stored["rank"], stored["status"]) == ("red", 1, "active"), stored


# --- replace_content stays bounded (#261 review) ---------------------------------------------------

_DT = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)


def _nested(depth: int, leaf: Any, through_lists: bool = False) -> Any:
    """*leaf* inside *depth* sub-objects (each one in a one-item list too when *through_lists*)."""
    value = leaf
    for _ in range(depth):
        value = [{"k": value}] if through_lists else {"k": value}
    return value


class TestReplaceContentBounds:
    """#261 review (unit): the compiled ``CONTENT`` of a full replace stays bounded whatever the shape of the data."""

    def test_no_datetime_sends_data(self) -> None:
        data = {"title": "t", "sub": {"n": 1}}
        assert replace_content(data) == (PLAIN_CONTENT, {"data": data})

    def test_a_large_list_is_sent_as_is(self) -> None:
        log = [_DT] * 10_000
        content, bound = replace_content({"title": "t", "created": _DT, "log": log})
        assert content.count("$this") == 3, "only `created` keeps its stored value"
        assert len(bound) == 6 and any(value is log for value in bound.values())
        assert len(content) < 300

    def test_kept_datetimes_are_capped(self) -> None:
        content, bound = replace_content({f"d{i}": _DT for i in range(200)})
        assert content.count("$this") == 3 * _KEEP_LIMIT
        assert len(bound) == 2 * 200

    def test_top_level_datetimes_come_first(self) -> None:
        content, _ = replace_content({"log": [_DT] * _KEEP_LIMIT, "created": _DT})
        assert content.count("$this") == 3, "`created` is kept, `log` (no budget left for all of it) is sent as is"
        content, _ = replace_content({"log": [_DT] * (_KEEP_LIMIT - 1), "created": _DT})
        assert content.count("$this") == 3 * _KEEP_LIMIT

    def test_depth_is_capped(self) -> None:
        content, _ = replace_content({"deep": _nested(_KEEP_MAX_DEPTH, _DT)})
        assert content.count("$this") == 3 and content.count("object::from_entries") == 1 + _KEEP_MAX_DEPTH
        assert replace_content({"deep": _nested(_KEEP_MAX_DEPTH + 1, _DT)})[0] == PLAIN_CONTENT
        content, _ = replace_content({"created": _DT, "deep": _nested(60, _DT, through_lists=True)})
        assert content.count("$this") == 3 and content.count("object::from_entries") == 1


class RfDeep(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_deep")

    id: str | None = None
    title: str = "t"
    created: datetime | None = None
    doc: dict[str, Any] = {}
    history: list[RfStampMeta] = []


#: ``READONLY`` datetimes at the top level, three levels down (kept) and four levels down (sent as read).
_RF_DEEP_DDL = (
    "DEFINE TABLE rf_deep SCHEMALESS;"
    "DEFINE FIELD created ON rf_deep TYPE option<datetime> READONLY;"
    "DEFINE FIELD doc ON rf_deep TYPE object DEFAULT {};"
    "DEFINE FIELD doc.a ON rf_deep TYPE option<object>;"
    "DEFINE FIELD doc.a.b ON rf_deep TYPE option<object>;"
    "DEFINE FIELD doc.a.b.at ON rf_deep TYPE option<datetime> READONLY;"
    "DEFINE FIELD doc.a.b.c ON rf_deep TYPE option<object>;"
    "DEFINE FIELD doc.a.b.c.at ON rf_deep TYPE option<datetime> READONLY;"
)
_RF_DEEP_SEED = (
    "CREATE rf_deep:seed CONTENT {title: 't', created: d'2026-01-02T03:04:05.123456789Z', "
    "doc: {a: {b: {at: d'2026-01-02T03:04:05.987654321Z', c: {}}}}};"
)
_RF_DEEP_STAMPS = "SELECT VALUE [<string> created, <string> doc.a.b.at] FROM ONLY rf_deep:seed;"
#: The same record with a datetime four levels down too.
_RF_DEEPER_SEED = _RF_DEEP_SEED.replace("c: {}", "c: {at: d'2026-01-02T03:04:05.000000001Z'}")


class TestReplaceContentBoundsE2E:
    """#261 review: ``update()`` of data nested past the servers' parse and computation limits, or holding long lists
    of datetimes, passes as before #261, directly and in a transaction, on the line under test and on ``mem://``. The
    stored datetimes it keeps are the top-level ones first, then, while the budget the fields before them left allows,
    those down to three levels (see ``TestReplaceKeepsTopLevelDatetimesE2E``)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["ws", "mem"])
    @pytest.mark.parametrize("tx", [False, True], ids=["direct", "tx"])
    async def test_deep_and_long_payloads(self, kind: str, tx: bool) -> None:
        async with _rf_engine(kind, "DEFINE TABLE rf_deep SCHEMALESS;", "rf_deep") as client:
            deep = await RfDeep(
                id="x",
                created=_DT,
                doc={"objects": _nested(30, _DT), "lists": _nested(20, _DT, through_lists=True)},
                history=[RfStampMeta(at=_DT, seen=_DT) for _ in range(500)],
            ).save()
            deep.title = "u"
            await _replace(deep, "update", tx)
            got = await RfDeep.objects().get("x")
            assert got.title == "u" and got.doc == deep.doc and got.history == deep.history
            assert await client.query("RETURN array::len(rf_deep:x.history);", {}) == 500

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["ws", "mem"])
    async def test_readonly_datetimes_within_the_bounds_are_kept(self, kind: str) -> None:
        """``doc`` comes before ``history`` in the data: its datetime three levels down still fits in the budget."""
        async with _rf_engine(kind, _RF_DEEP_DDL, "rf_deep") as client:
            await client.query(_RF_DEEP_SEED, {})
            before = await client.query(_RF_DEEP_STAMPS, {})
            seed = await RfDeep.objects().get("seed")
            seed.title = "u"
            seed.history = [RfStampMeta(at=_DT) for _ in range(_KEEP_LIMIT)]  # no budget left for all of it: sent as is
            await seed.update()
            assert await client.query(_RF_DEEP_STAMPS, {}) == before
            assert (await RfDeep.objects().get("seed")).title == "u"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["ws", "mem"])
    async def test_a_readonly_datetime_past_the_depth_is_refused(self, kind: str) -> None:
        """The documented limit: four levels down, the datetime is sent as the SDK read it and ``READONLY`` refuses it."""
        async with _rf_engine(kind, _RF_DEEP_DDL, "rf_deep") as client:
            await client.query(_RF_DEEPER_SEED, {})
            seed = await RfDeep.objects().get("seed")
            seed.title = "u"
            with pytest.raises((SurrealError, SurrealDbError), match="readonly"):
                await seed.update()
            await seed.merge(title="u")  # the way the docs give
            assert (await RfDeep.objects().get("seed")).title == "u"


class RfHistory(BaseSurrealModel):
    """A list of timestamped items before a sub-object's datetime and a top-level one (#261 review)."""

    model_config = SurrealConfigDict(table_name="rf_history")

    id: str | None = None
    title: str = "t"
    history: list[RfStampMeta] = []
    meta: RfStampMeta = Field(default_factory=RfStampMeta)
    created: datetime | None = None


def _rf_history_ddl(meta_at: str) -> str:
    """``rf_history`` with a ``READONLY`` top-level ``created``, and ``meta.at`` defined with *meta_at* clauses."""
    return (
        "DEFINE TABLE rf_history SCHEMALESS;"
        "DEFINE FIELD created ON rf_history TYPE datetime READONLY;"
        "DEFINE FIELD meta ON rf_history TYPE object;"
        f"DEFINE FIELD meta.at ON rf_history TYPE datetime {meta_at};"
    )


#: A record whose datetimes hold nanoseconds, which the SDK floors to the microsecond.
_RF_HISTORY_SEED = (
    "CREATE rf_history:x CONTENT {title: 't', history: [], created: d'2026-01-02T03:04:05.123456789Z', "
    "meta: {at: d'2026-01-02T03:04:05.987654321Z', seen: d'2026-01-02T03:04:05.000000001Z'}};"
)
_RF_HISTORY_STAMPS = "SELECT VALUE [<string> created, <string> meta.at] FROM ONLY rf_history:x;"


class TestReplaceKeepsTopLevelDatetimesE2E:
    """#261 review: ``update()`` keeps every stored top-level datetime it did not change (up to ``_KEEP_LIMIT``),
    whatever the fields before it hold. Below the top level it keeps one only while the values it rebuilds fit in what
    the fields before it left of that budget, which depends on the data: a ``READONLY`` datetime there may be refused,
    the limit the README and the generator's class comment state. On the line under test and on ``mem://``."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["ws", "mem"])
    @pytest.mark.parametrize("events", [0, 10, 11, 500])
    async def test_a_top_level_readonly_datetime_is_kept(self, kind: str, events: int) -> None:
        async with _rf_engine(kind, _rf_history_ddl(""), "rf_history") as client:
            await client.query(_RF_HISTORY_SEED, {})
            stamp = await RfHistory.objects().get("x")
            stamp.title = "u"
            stamp.history = [RfStampMeta(at=_DT, seen=_DT) for _ in range(events)]
            await stamp.update()
            stamps = await client.query(_RF_HISTORY_STAMPS, {})
            assert isinstance(stamps, list) and stamps[0] == "2026-01-02T03:04:05.123456789Z", stamps
            got = await RfHistory.objects().get("x")
            assert got.title == "u" and len(got.history) == events

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["ws", "mem"])
    async def test_a_readonly_datetime_below_the_top_level_depends_on_the_data(self, kind: str) -> None:
        """``meta.at`` is kept with no event, then refused once ten timestamped events, before it, use the budget up."""
        async with _rf_engine(kind, _rf_history_ddl("READONLY"), "rf_history") as client:
            await client.query(_RF_HISTORY_SEED, {})
            before = await client.query(_RF_HISTORY_STAMPS, {})
            stamp = await RfHistory.objects().get("x")
            stamp.title = "u"
            await stamp.update()
            assert await client.query(_RF_HISTORY_STAMPS, {}) == before
            stamp.history = [RfStampMeta(at=_DT, seen=_DT) for _ in range(10)]
            with pytest.raises((SurrealError, SurrealDbError), match="readonly"):
                await stamp.update()
            await stamp.merge(title="v")  # the way the docs give
            assert await client.query(_RF_HISTORY_STAMPS, {}) == before
            assert (await RfHistory.objects().get("x")).title == "v"


# --- filters and live queries (spec §5.5, §5.6, §6.5; AC-22, AC-23; D12) --------------------------

_WORKING_AGE = Range(BoundIncluded(18), BoundExcluded(65))
_SDK_WORKING_AGE = surrealdb.Range(BoundIncluded(18), BoundExcluded(65))
_ID_REFUSAL = "'{lookup}' with a Range cannot be applied to the record id column 'id': a record id never falls inside a value range. Filter on a regular column."
_ENDPOINT_REFUSAL = (
    "'{lookup}' with a Range cannot be applied to the edge endpoint column '{field}': it holds a record id, "
    "which never falls inside a value range."
)


class _RecordingClient:
    """Stands in for the SDK client: records what ``query()`` is handed, answers no rows."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query(self, statement: str, variables: dict[str, Any] | None = None) -> list[Any]:
        self.calls.append((statement, dict(variables or {})))
        return []


class TestRangeLookup:
    """AC-22 (unit), D12: a ``Range`` given to ``in``/``not_in`` is one bound value, never expanded."""

    @pytest.mark.parametrize("value", [_WORKING_AGE, _SDK_WORKING_AGE], ids=["orm", "sdk"])
    @pytest.mark.parametrize(("lookup", "op"), [("in", "IN"), ("not_in", "NOT IN")])
    def test_bound_as_is(self, value: Any, lookup: str, op: str) -> None:
        sql, variables, counter = build_filter_condition("age", lookup, value, 0)
        assert (sql, counter) == (f"age {op} $_f0", 1)
        assert list(variables) == ["_f0"] and variables["_f0"] is value

    def test_counter_names_the_variable(self) -> None:
        sql, variables, counter = build_filter_condition("age", "in", _WORKING_AGE, 3)
        assert (sql, counter) == ("age IN $_f3", 4) and variables["_f3"] is _WORKING_AGE

    def test_through_q(self) -> None:
        sql, variables, counter = Q(age__in=_WORKING_AGE).to_sql()
        assert (sql, counter) == ("age IN $_f0", 1) and variables["_f0"] is _WORKING_AGE
        sql, variables, _ = (~Q(age__not_in=_SDK_WORKING_AGE)).to_sql()
        assert "age NOT IN $_f0" in sql and variables["_f0"] is _SDK_WORKING_AGE

    def test_through_the_queryset(self) -> None:
        where, variables = RfEvent.objects().filter(n__in=_WORKING_AGE)._build_where()
        assert where == " WHERE n IN $_f0" and variables == {"_f0": _WORKING_AGE}
        assert variables["_f0"] is _WORKING_AGE
        where, variables = RfEvent.objects().filter(Q(n__not_in=_SDK_WORKING_AGE))._build_where()
        assert where == " WHERE n NOT IN $_f0" and variables["_f0"] is _SDK_WORKING_AGE

    def test_builtin_range_is_still_a_list(self) -> None:
        _, variables = RfEvent.objects().filter(n__in=range(1, 5))._build_where()
        assert variables == {"_f0": [1, 2, 3, 4]} and type(variables["_f0"]) is list

    @pytest.mark.parametrize("lookup", ["containsall", "containsany"])
    @pytest.mark.parametrize("value", [_WORKING_AGE, _SDK_WORKING_AGE], ids=["orm", "sdk"])
    def test_collection_lookups_keep_their_refusal(self, value: Any, lookup: str) -> None:
        with pytest.raises(ValueError, match=rf"'{lookup}' lookup requires an iterable \(list, tuple, or set\), got Range"):
            build_filter_condition("tags", lookup, value, 0)

    def test_live_literals(self) -> None:
        live = RfEvent.objects().filter(n__in=_WORKING_AGE)._compile_live(False)
        assert live == "LIVE SELECT * FROM rf_event WHERE n IN 18..65;"
        live = RfEvent.objects().filter(n__not_in=Range(BoundIncluded(65)))._compile_live(False)
        assert live == "LIVE SELECT * FROM rf_event WHERE n NOT IN 65..;"
        live = RfEvent.objects().filter(n__in=surrealdb.Range(_sdk.CBOR_NULL, BoundIncluded(5)))._compile_live(False)
        assert live == "LIVE SELECT * FROM rf_event WHERE n IN ..=5;"

    # D12: a value range never contains a record id.

    @pytest.mark.parametrize("value", [_WORKING_AGE, _SDK_WORKING_AGE], ids=["orm", "sdk"])
    @pytest.mark.parametrize("lookup", ["in", "not_in"])
    @pytest.mark.parametrize("record_table", ["rf_event", None], ids=["queryset", "bare-q"])
    def test_refused_on_the_record_id_column(self, value: Any, lookup: str, record_table: str | None) -> None:
        with pytest.raises(ValueError, match=re.escape(_ID_REFUSAL.format(lookup=lookup))):
            build_filter_condition("id", lookup, value, 0, record_table)

    @pytest.mark.parametrize("value", [_WORKING_AGE, _SDK_WORKING_AGE], ids=["orm", "sdk"])
    @pytest.mark.parametrize(("field", "lookup"), [("in", "in"), ("out", "not_in")])
    def test_refused_on_edge_endpoint_columns(self, value: Any, field: str, lookup: str) -> None:
        message = re.escape(_ENDPOINT_REFUSAL.format(lookup=lookup, field=field))
        with pytest.raises(ValueError, match=message):
            build_filter_condition(field, lookup, value, 0, "rf_link", endpoint_columns=("in", "out"))

    def test_refused_when_the_query_is_built(self) -> None:
        queryset = RfEvent.objects().filter(id__in=_WORKING_AGE)  # filter() only records the lookup
        with pytest.raises(ValueError, match="record id column"):
            queryset._build_where()
        with pytest.raises(ValueError, match="record id column"):
            Q(id__not_in=_SDK_WORKING_AGE).to_sql()
        with pytest.raises(ValueError, match="edge endpoint column 'in'"):
            RfLink.objects().filter(in___in=_WORKING_AGE)._build_where()
        with pytest.raises(ValueError, match="record id column"):
            RfLink.objects().filter(id__in=_WORKING_AGE)._build_where()

    def test_builtin_range_on_the_id_column_keeps_its_coercion(self) -> None:
        _, variables = RfEvent.objects().filter(id__in=range(1, 3))._build_where()
        assert variables == {"_f0": [RecordID("rf_event", 1), RecordID("rf_event", 2)]}

    # AC-22, C1: what the SDK client is handed.

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("value", "open_side"),
        [
            (Range(BoundExcluded(1), None), "end"),
            (surrealdb.Range(undefined, BoundIncluded(5)), "begin"),
            (surrealdb.Range(None, BoundIncluded(5)), "begin"),
            (surrealdb.Range(Bound(), BoundIncluded(5)), "begin"),
        ],
        ids=["orm-none-end", "sdk-undefined-begin", "sdk-none-begin", "sdk-bound-begin"],
    )
    async def test_exec_hands_the_sdk_a_cbor_null_open_side(
        self, value: Any, open_side: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _RecordingClient()

        async def get_client() -> Any:
            return fake

        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", staticmethod(get_client))
        assert await RfEvent.objects().filter(n__in=value).exec() == []

        [(statement, variables)] = fake.calls
        assert "WHERE n IN $_f0" in statement
        sent = variables["_f0"]
        assert type(sent) is surrealdb.Range
        closed_side = "begin" if open_side == "end" else "end"
        assert getattr(sent, open_side) is _sdk.CBOR_NULL
        expected_bound = BoundExcluded(1) if open_side == "end" else BoundIncluded(5)
        assert getattr(sent, closed_side) == expected_bound
        assert _sdk.cbor_encode(sent).count(b"\xf6") == 1 and b"\xc6\xf6" not in _sdk.cbor_encode(sent)


_FILTER_ROWS: dict[str, dict[str, Any]] = {
    "e1": {
        "n": 1,
        "ttl": timedelta(minutes=30),
        "grace": Duration.parse("10m"),
        "day": date(2026, 1, 1),
        "price": Decimal("1.50"),
        "token": UUID(int=1),
        "at": datetime(2026, 1, 1, tzinfo=UTC),
        "tags": {"a"},
        "hours": Range(BoundIncluded(0), BoundExcluded(8)),
    },
    "e5": {
        "n": 5,
        "ttl": timedelta(hours=1),
        "grace": Duration.parse("1h30m1ns"),
        "day": date(2026, 1, 2),
        "price": Decimal("12.50"),
        "token": UUID(int=5),
        "at": datetime(2026, 1, 2, tzinfo=UTC),
        "tags": {"a", "b"},
        "hours": Range(BoundIncluded(9), BoundExcluded(17)),
    },
    "e9": {
        "n": 9,
        "ttl": timedelta(hours=2),
        "grace": Duration.parse("1h30m2ns"),
        "day": date(2026, 1, 3),
        "price": Decimal("99"),
        "token": UUID(int=9),
        "at": datetime(2026, 1, 3, tzinfo=UTC),
        "tags": {"b", "c"},
        "hours": Range(BoundExcluded(12), None),
    },
}

_ALL = {"e1", "e5", "e9"}

#: (label, positional ``Q`` filters, keyword filters, the ids they must return).
_FILTER_CASES: list[tuple[str, tuple[Q, ...], dict[str, Any], set[str]]] = [
    ("ttl__gt=timedelta", (), {"ttl__gt": timedelta(minutes=45)}, {"e5", "e9"}),
    ("grace__lte=Duration (ns)", (), {"grace__lte": Duration.parse("1h30m1ns")}, {"e1", "e5"}),
    ("day=date", (), {"day": date(2026, 1, 2)}, {"e5"}),
    ("price__lte=Decimal", (), {"price__lte": Decimal("12.5")}, {"e1", "e5"}),
    ("token=UUID", (), {"token": UUID(int=5)}, {"e5"}),
    ("at__gte=datetime", (), {"at__gte": datetime(2026, 1, 2, tzinfo=UTC)}, {"e5", "e9"}),
    ("tags__containsall=set", (), {"tags__containsall": {"b", "a"}}, {"e5"}),
    ("hours=Range", (), {"hours": Range(BoundIncluded(9), BoundExcluded(17))}, {"e5"}),
    ("hours__contains=12", (), {"hours__contains": 12}, {"e5"}),
    ("n__in 1..5", (), {"n__in": Range(BoundIncluded(1), BoundExcluded(5))}, {"e1"}),
    ("n__in 1..=5", (), {"n__in": Range(BoundIncluded(1), BoundIncluded(5))}, {"e1", "e5"}),
    ("n__in 1>..", (), {"n__in": Range(BoundExcluded(1), None)}, {"e5", "e9"}),
    ("n__in ..", (), {"n__in": Range()}, _ALL),
    ("n__in sdk 1>..9", (), {"n__in": surrealdb.Range(BoundExcluded(1), BoundExcluded(9))}, {"e5"}),
    ("n__in sdk None..=5", (), {"n__in": surrealdb.Range(None, BoundIncluded(5))}, {"e1", "e5"}),
    ("n__in sdk undefined..=5", (), {"n__in": surrealdb.Range(undefined, BoundIncluded(5))}, {"e1", "e5"}),
    ("n__in sdk CBOR_NULL..=5", (), {"n__in": surrealdb.Range(_sdk.CBOR_NULL, BoundIncluded(5))}, {"e1", "e5"}),
    ("n__in sdk Bound()..=5", (), {"n__in": surrealdb.Range(Bound(), BoundIncluded(5))}, {"e1", "e5"}),
    ("n__in sdk None..None", (), {"n__in": surrealdb.Range(None, None)}, _ALL),
    ("n__not_in 5..", (), {"n__not_in": Range(BoundIncluded(5))}, {"e1"}),
    ("n__not_in ..", (), {"n__not_in": Range()}, set()),
    ("n__not_in sdk 1..5", (), {"n__not_in": surrealdb.Range(BoundIncluded(1), BoundExcluded(5))}, {"e5", "e9"}),
    ("n__not_in sdk CBOR_NULL..=5", (), {"n__not_in": surrealdb.Range(_sdk.CBOR_NULL, BoundIncluded(5))}, {"e9"}),
    (
        "Q(n__in) | Q(n__in)",
        (Q(n__in=Range(BoundIncluded(1), BoundExcluded(5))) | Q(n__in=Range(BoundExcluded(8), None)),),
        {},
        {"e1", "e9"},
    ),
    ("n__in=range(1, 5)", (), {"n__in": range(1, 5)}, {"e1"}),
]


class TestFiltersE2E:
    """AC-22: bound filter values of every rich type, and ``Range`` lookups, on both lines."""

    @pytest.mark.asyncio
    async def test_bound_filters_match(self) -> None:
        async with orm_client("rf_event"):
            for rid, values in _FILTER_ROWS.items():
                await RfEvent(id=rid, **values).save()
            wrong: dict[str, set[str]] = {}
            for label, args, kwargs, expected in _FILTER_CASES:
                got = {event.id for event in await RfEvent.objects().filter(*args, **kwargs).exec()}
                if got != expected:
                    wrong[label] = got
        assert not wrong, f"filters returned the wrong rows: {wrong!r}"

    @pytest.mark.asyncio
    async def test_zero_duration_filter_is_refused_before_sending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(encoding, "reads_zero_duration", lambda: False)  # an SDK nothing reads a zero with
        async with orm_client("rf_event"):
            client = await SurrealDBConnectionManager.get_client()
            with pytest.raises(ValueError, match="zero duration") as excinfo:
                await RfEvent.objects().filter(ttl__gt=timedelta(0)).exec()
            assert type(excinfo.value) is ValueError
            await _assert_client_untouched(client)


@contextlib.asynccontextmanager
async def _scheme_client(scheme: str, *tables: str) -> AsyncIterator[Any]:
    """``orm_client(*tables)`` over WebSocket, or the same ORM client over HTTP."""
    if scheme == "ws":
        async with orm_client(*tables) as client:
            yield client
        return
    root = await _http_client()
    for table in tables:
        await root.query(f"REMOVE TABLE IF EXISTS {table};", {})
    SurrealDBConnectionManager.set_connection(_http_url(), "root", "root", "ns", "db")
    try:
        yield await SurrealDBConnectionManager.get_client()
    finally:
        for table in tables:
            await root.query(f"REMOVE TABLE IF EXISTS {table};", {})
        await SurrealDBConnectionManager.unset_connection()


class TestZeroDurationE2E:
    """#235: a zero duration is written, filtered and read back on both lines, now that the clients the
    ORM opens read it (SDK 2.0.0 cannot: the read hook of ``decoding.py``)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("scheme", ["ws", "http"])
    async def test_zero_durations_round_trip(self, scheme: str) -> None:
        assert encoding.reads_zero_duration()
        async with _scheme_client(scheme, "rf_event"):
            await RfEvent(id="zero", ttl=timedelta(0), grace=Duration(0), durs=[Duration(0), Duration(1)]).save()
            await RfEvent(id="long", ttl=timedelta(hours=1), grace=Duration(1)).save()
            raw = await _raw(RecordID("rf_event", "zero"))
            assert type(raw["ttl"]) is surrealdb.Duration and raw["ttl"].elapsed == 0
            assert type(raw["grace"]) is surrealdb.Duration and raw["grace"].elapsed == 0
            loaded = await RfEvent.objects().get("zero")
            assert type(loaded.ttl) is timedelta and loaded.ttl == timedelta(0)
            assert type(loaded.grace) is Duration and loaded.grace == Duration(0)
            assert loaded.durs == [Duration(0), Duration(1)]
            assert [e.id for e in await RfEvent.objects().filter(ttl=timedelta(0)).exec()] == ["zero"]
            assert [e.id for e in await RfEvent.objects().filter(grace__gt=Duration(0)).exec()] == ["long"]
            await loaded.merge(ttl=timedelta(0), grace=Duration.parse("1s"))
            assert loaded.ttl == timedelta(0)
            assert (await RfEvent.objects().get("zero")).grace == Duration.parse("1s")
            client = await SurrealDBConnectionManager.get_client()
            assert await client.query("RETURN 1;", {}) == 1

    @pytest.mark.asyncio
    async def test_zero_duration_live_filter(self) -> None:
        async with orm_client("rf_event") as client:
            await client.query("DEFINE TABLE rf_event SCHEMALESS;", {})
            async with RfEvent.objects().filter(ttl=timedelta(0)).live() as stream:
                await RfEvent(id="long", ttl=timedelta(hours=1)).save()
                await RfEvent(id="zero", ttl=timedelta(0)).save()
                [event] = await _take(stream, 1)
        assert event.record_id == "rf_event:zero", event.record_id
        assert event.validation_error is None, event.validation_error
        assert type(event.instance.ttl) is timedelta and event.instance.ttl == timedelta(0)


class TestSetColumnE2E:
    """#235: a ``set[X]`` field in a ``TYPE set<…>`` column reads back as the declared set on 2.x and 3.3."""

    @pytest.mark.asyncio
    async def test_set_column(self) -> None:
        async with orm_client("rf_event") as client:
            version = await _server_version()
            if (3, 0, 0) <= version < (3, 3, 0):
                pytest.skip("SurrealDB 3.1 refuses an array in a set<…> column")
            await client.query("DEFINE FIELD tags ON rf_event TYPE set<string>;", {})
            await RfEvent(id="s", tags={"b", "a"}).save()
            stored = await client.query("SELECT VALUE <string> tags FROM ONLY rf_event:s;", {})
            assert stored == ("{'a', 'b'}" if version >= (3, 0, 0) else "['a', 'b']"), stored
            loaded = await RfEvent.objects().get("s")
            assert type(loaded.tags) is set and loaded.tags == {"a", "b"}
            raw = await _raw(RecordID("rf_event", "s"))
            assert raw["tags"] == ({"a", "b"} if version >= (3, 0, 0) else ["a", "b"]), raw["tags"]
            assert await client.query("RETURN 1;", {}) == 1


class TestLiveFiltersE2E:
    """AC-23: a live filter inlines duration, range and date literals; order proves the filtering."""

    @pytest.mark.asyncio
    async def test_stream_reports_the_matching_write_only(self) -> None:
        hit: dict[str, Any] = {
            "ttl": timedelta(hours=1),
            "hours": Range(BoundIncluded(9), BoundExcluded(17)),
            "n": 5,
            "day": date(2026, 1, 2),
        }
        misses = {  # each row fails exactly one condition
            "short": {**hit, "ttl": timedelta(minutes=5)},
            "no12": {**hit, "hours": Range(BoundIncluded(0), BoundExcluded(8))},
            "n10": {**hit, "n": 10},
            "day3": {**hit, "day": date(2026, 1, 3)},
        }
        queryset = RfEvent.objects().filter(
            ttl__gte=timedelta(hours=1),
            hours__contains=12,
            n__in=Range(BoundIncluded(1), BoundExcluded(10)),
            day=date(2026, 1, 2),
        )
        async with orm_client("rf_event") as client:
            await client.query("DEFINE TABLE rf_event SCHEMALESS;", {})
            async with queryset.live() as stream:
                for rid, values in misses.items():
                    await RfEvent(id=rid, **values).save()
                await RfEvent(id="hit", **hit).save()
                [event] = await _take(stream, 1)
        assert event.action == LiveAction.CREATE and event.record_id == "rf_event:hit", (
            f"a non-matching write reached the stream first: {event.record_id}"
        )
        assert event.validation_error is None, event.validation_error
        instance = event.instance
        assert type(instance.ttl) is timedelta and instance.ttl == timedelta(hours=1)
        assert type(instance.day) is date and instance.day == date(2026, 1, 2)
        assert type(instance.hours) is Range and instance.hours == hit["hours"]


# --- README (spec §11, AC-25; D9, D10) -----------------------------------------------------------

_README = Path(__file__).resolve().parents[1] / "README.md"

#: Section 24's example tables and the test tables they run against: everything else runs as written.
_README_TABLES = {"subscription": "rf_subscription", "slot": "rf_slot"}

#: D10: the public docs never describe what happens to the client when the SDK cannot read a value.
_D10_PHRASES = ("connection lost", "connection is lost", "drops the connection")

#: The ``TYPE set<…>`` advice of spec §11, as the README must state it (whitespace normalised); since
#: #235 the ORM reads the native set of a 3.3 column back.
_SET_ADVICE = (
    "`set[X]`/`frozenset[X]` fields are stored as arrays. On SurrealDB 3.3 a `TYPE set<…>` column stores the "
    "array as a native set, which the field reads back as the declared set (see "
    "[Values the SDK cannot read](#values-the-sdk-cannot-read)), but 3.1 refuses an array in it; to support "
    "3.1, declare such columns `array<…>`, optionally with `VALUE $value.distinct()`"
)

_D9_SINCE = "pre-existing (documented in v0.23.0)"


def _readme_part(heading: str) -> str:
    """The README text from the ``heading`` line up to the next ``##``/``###`` heading."""
    text = _README.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(heading)}\n.*?(?=^#{{2,3}} )", text, re.MULTILINE | re.DOTALL)
    assert match, f"README has no {heading!r} section"
    return match.group(0)


def _section_24() -> str:
    return _readme_part("### 24. Rich field types")


def _normalised(text: str) -> str:
    return " ".join(text.split())


def _behaviour_rows() -> list[tuple[str, list[str]]]:
    """Each row of the "ORM behaviour: SurrealDB 2.x vs 3.x" table: its raw line and its cells."""
    table = _readme_part("### ORM behaviour: SurrealDB 2.x vs 3.x")
    lines = [line.strip() for line in table.splitlines() if line.startswith("|")]
    return [(line, [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip("|"))]) for line in lines[2:]]


def _v0_23_0_rows() -> list[tuple[str, list[str]]]:
    """The behaviour-table rows v0.23.0 adds: the two ``v0.23.0`` rows and the retroactive D9 row."""
    return [(line, cells) for line, cells in _behaviour_rows() if cells[-1] in ("v0.23.0", _D9_SINCE)]


def _section_24_examples() -> list[tuple[str, str]]:
    """Section 24's ``python`` and ``sql`` blocks in order, ``(language, code)``, on the ``rf_`` tables."""
    blocks: list[tuple[str, str]] = re.findall(r"^```(python|sql)\n(.*?)^```$", _section_24(), re.MULTILINE | re.DOTALL)
    assert blocks, "README section 24 has no example"
    examples: list[tuple[str, str]] = []
    renamed = dict.fromkeys(_README_TABLES, 0)
    for language, code in blocks:
        for table, test_table in _README_TABLES.items():
            pattern = rf'table_name="{table}"' if language == "python" else rf"\bON {table}\b"
            replacement = f'table_name="{test_table}"' if language == "python" else f"ON {test_table}"
            code, count = re.subn(pattern, replacement, code)
            renamed[table] += count
        examples.append((language, code))
    assert all(renamed.values()), f"section 24 no longer names these example tables: {renamed!r}"
    return examples


class TestReadmeExamplesE2E:
    """AC-25: every example of README section 24 runs as written (its table names aside), on both lines."""

    @pytest.mark.asyncio
    async def test_section_24_examples(self) -> None:
        namespace: dict[str, Any] = {"__name__": "readme_section_24"}
        async with orm_client(*_README_TABLES.values()) as client:
            for language, code in _section_24_examples():
                if language == "sql":
                    await client.query(code, {})
                    continue
                compiled = compile(code, "<README section 24>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                # A doctest of this repository's own README (trusted text), top-level awaits included.
                outcome = eval(compiled, namespace)
                if inspect.iscoroutine(outcome):
                    await outcome

            # What the examples' comments say.
            subscription = namespace["Subscription"]
            loaded = await subscription.objects().get("acme")
            assert type(loaded.started_at) is datetime and loaded.started_at.tzinfo is not None
            assert type(loaded.renews_on) is date and loaded.renews_on == date(2026, 2, 1)
            assert type(loaded.trial) is timedelta
            assert type(loaded.grace) is Duration and loaded.grace == Duration.parse("1h30m1ns")
            assert type(loaded.price) is Decimal and loaded.price == Decimal("9.99")
            assert type(loaded.token) is UUID
            assert type(loaded.active_hours) is Range
            assert loaded.active_hours == Range(BoundIncluded(9), BoundExcluded(17))
            assert loaded.period is not None and loaded.period.end is None
            assert type(loaded.logo) is bytes
            assert type(loaded.tags) is set and loaded.tags == {"annual", "pro"}
            assert namespace["sub"].trial == timedelta(days=21) and type(namespace["sub"].trial) is timedelta

            assert sorted(slot.hour for slot in namespace["working"]) == [9, 12]
            assert [slot.hour for slot in namespace["late"]] == [17]
            for name in ("trials", "cheap", "open_at_noon", "graced", "nonzero"):
                assert [found.get_raw_id() for found in namespace[name]] == ["acme"], name

            # The array<…> column of the sets example keeps the declared set (3.1 would refuse a set<…> one).
            await loaded.update()
            stored = await client.query("SELECT VALUE tags FROM ONLY $rid;", {"rid": loaded._record_id()})
            assert sorted(stored) == ["annual", "pro"]
            assert (await subscription.objects().get("acme")).tags == {"annual", "pro"}


class TestReadmeWording:
    """Unit: what README must (and must not) say about v0.23.0 (spec §11, D9, D10)."""

    def test_section_24_never_describes_the_connection(self) -> None:
        texts = {"section 24": _section_24(), **{cells[0]: line for line, cells in _v0_23_0_rows()}}
        for where, text in texts.items():
            lowered = text.lower()
            assert not [phrase for phrase in _D10_PHRASES if phrase in lowered], where
            assert not re.search(r"\bconnections?\b", text, re.IGNORECASE), f"D10: {where} mentions the connection"

    def test_set_columns_advice_and_array_example(self) -> None:
        section = _section_24()
        assert _SET_ADVICE in _normalised(section)
        sql = re.findall(r"^```sql\n(.*?)^```$", section, re.MULTILINE | re.DOTALL)
        assert any(
            re.fullmatch(r"DEFINE FIELD \w+ ON \w+ TYPE array<\w+> VALUE \$value\.distinct\(\);\n", block) for block in sql
        ), sql

    def test_behaviour_table_rows(self) -> None:
        """v0.23.0's rows (two added, one documented), together and in version order before the table's last row."""
        rows = _v0_23_0_rows()
        assert all(len(cells) == 4 and "\\|" not in line for line, cells in rows), rows
        assert_server_rows([cells for _, cells in rows])
        added = [cells for _, cells in rows if cells[-1] == "v0.23.0"]
        assert len(added) == 2, added
        assert any(cells[0].startswith("Rich field types") for cells in added), added
        assert any("`TYPE set<…>`" in cells[0] for cells in added), added
        [decimal] = [cells for _, cells in rows if cells[-1] == _D9_SINCE]
        assert decimal[0] == "Reading a `Decimal`", decimal
        assert "`12.50`" in decimal[1] and "`12.5`" in decimal[2], decimal

    def test_section_24_tables_have_no_raw_pipe_in_a_cell(self) -> None:
        tables = re.findall(r"(?:^\|.*\n)+", _section_24(), re.MULTILINE)
        assert len(tables) == 2, tables  # the annotation table and the refusal table
        for table in tables:
            widths = {len(re.split(r"(?<!\\)\|", line.strip().strip("|"))) for line in table.splitlines()}
            assert len(widths) == 1 and "\\|" not in table, table

    def test_behaviour_table_header_names_the_v0_23_0_versions(self) -> None:
        assert_server_header_sentence("rows added in v0.23.0 on 2.7.0 and 3.3.4, with 3.1.5 where it differs")
