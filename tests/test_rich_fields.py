"""Rich field types (v0.23.0) through the ORM: models whose fields hold durations, dates, sets,
ranges, decimals, UUIDs and bytes, written and read on both server lines.

Every class here is E2E (suffixed ``E2E``) unless it says otherwise. Tables are prefixed ``rf_``
and created inside ``orm_client(...)``, which drops them before and after the test.
"""

import ast
import asyncio
import contextlib
import dataclasses
import inspect
import re
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Optional
from uuid import UUID

import pytest
import surrealdb
from pydantic import BaseModel, Field, ValidationError
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
    LiveAction,
    Q,
    Range,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    SurrealFunc,
    _sdk,
    computed,
    encoding,
)
from surreal_orm_lite._sdk import Bound
from surreal_orm_lite.live import hydrate
from surreal_orm_lite.utils import build_filter_condition
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
    async def test_refusals_leave_the_client_untouched(self, case: str) -> None:
        build, message = _REFUSED_SAVES[case]
        if case == "zero-timedelta" and encoding.sdk_decodes_zero_duration():
            pytest.skip("the installed SDK reads a zero duration back: nothing to refuse")
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
    """§6.3 item 3 (D1): top-level durations are converted; every other value is applied as received."""

    def test_durations_converted_other_values_raw(self) -> None:
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
        assert event.day is day and event.tags is tags and event.hours is hours and event.durs is durs

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

    def test_model_without_rich_fields_sets_the_row_objects(self) -> None:
        plain = RfPlain(id="p")
        row = {"id": RecordID("rf_plain", "p"), "name": "n", "tags": ["a"], "meta": {"d": surrealdb.Duration(1)}}
        plain._apply_record(row)
        assert plain.id is row["id"] and plain.name is row["name"]
        assert plain.tags is row["tags"] and plain.meta is row["meta"]


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

    # D1 converts durations only: ``day`` and ``tags`` hold the server's datetime and list after a returned
    # row is applied, as in v0.22.2, and Pydantic warns when ``update()`` serialises them (README section 24).
    @pytest.mark.filterwarnings("ignore:Pydantic serializer warnings:UserWarning")
    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", list(_APPLY_CASES))
    async def test_returned_rows_keep_durations(self, case: str) -> None:
        async with orm_client("rf_event"):
            event = await _APPLY_CASES[case]()
            assert type(event.ttl) is timedelta and event.ttl == FULL["ttl"]
            assert type(event.grace) is Duration and event.grace == FULL["grace"]
            if case == "refresh":
                message = "D1 scope changed: update README section 24"
                assert type(event.day) is datetime, message
                assert type(event.tags) is list, message
                assert type(event.hours) is surrealdb.Range, message

            await event.update()  # the §3.7 regression: durations written back as {elapsed: …}
            rid = event._record_id()
            assert rid is not None
            row = await _raw(rid)
            assert type(row["ttl"]) is surrealdb.Duration and type(row["grace"]) is surrealdb.Duration, row
            _assert_native(row, _WRITE_BACK_KEYS)


# --- sub-models after a write that returns the row (#228) ----------------------------------------


class RfSub(BaseModel):
    """A sub-model holding rich values: a write that syncs the instance must keep them the ORM's."""

    ttl: Duration
    hours: Range[int]
    period: Range[datetime] | None = None
    label: str = ""


@dataclasses.dataclass
class RfSubData:
    """The same, as a stdlib dataclass."""

    ttl: Duration
    hours: Range[int]


class RfSubDict(TypedDict):
    """The same, as a ``TypedDict``: it validates into a ``dict`` holding the ORM's values."""

    ttl: Duration


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
    n: int = 0


#: The sub-model every ``RfHolder`` field holds.
SUB = RfSub(
    ttl=Duration.parse("1h30m1ns"),
    hours=Range(BoundIncluded(9), BoundExcluded(17)),
    period=Range(BoundIncluded(datetime(2026, 1, 1, tzinfo=UTC)), None),
    label="s",
)
#: One value per ``RfHolder`` field.
HOLDER: dict[str, Any] = {
    "sub": SUB,
    "maybe": SUB,
    "subs": [SUB, SUB.model_copy(update={"label": "t"})],
    "by_key": {"a": SUB},
    "data": RfSubData(ttl=Duration(7), hours=Range(BoundIncluded(1), None)),
    "datas": [RfSubData(ttl=Duration.parse("2m"), hours=Range(None, BoundExcluded(3)))],
    "typed": {"ttl": Duration(9)},
    "plain": RfPlainSub(name="p", n=1),
    "many": [Duration.parse("2m")],
}


def _assert_sub_models(holder: Any) -> None:
    """Every sub-model field of ``holder`` holds its declared type, with the ORM's rich values, as ``HOLDER``."""
    assert isinstance(holder, RfHolder), holder
    assert type(holder.sub) is RfSub and holder.sub == SUB, holder.sub
    assert (type(holder.sub.ttl), type(holder.sub.hours), type(holder.sub.period)) == (Duration, Range, Range)
    assert type(holder.maybe) is RfSub and holder.maybe == SUB, holder.maybe
    assert [type(sub) for sub in holder.subs] == [RfSub, RfSub] and holder.subs == HOLDER["subs"], holder.subs
    assert type(holder.by_key["a"]) is RfSub and holder.by_key == HOLDER["by_key"], holder.by_key
    assert type(holder.data) is RfSubData and holder.data == HOLDER["data"], holder.data
    assert (type(holder.data.ttl), type(holder.data.hours)) == (Duration, Range)
    assert [type(data) for data in holder.datas] == [RfSubData] and holder.datas == HOLDER["datas"], holder.datas
    assert holder.typed == HOLDER["typed"] and type(holder.typed["ttl"]) is Duration, holder.typed
    assert type(holder.plain) is RfPlainSub and holder.plain == HOLDER["plain"], holder.plain


def _assert_sub_models_native(row: dict[str, Any]) -> None:
    """The stored row keeps SurrealDB's native durations and ranges inside every sub-model (never ``{elapsed: …}``)."""
    subs = [row["sub"], row["maybe"], *row["subs"], row["by_key"]["a"], row["data"], *row["datas"]]
    for sub in subs:
        assert type(sub["ttl"]) is surrealdb.Duration and type(sub["hours"]) is surrealdb.Range, sub
    assert type(row["sub"]["period"]) is surrealdb.Range, row["sub"]
    assert type(row["typed"]["ttl"]) is surrealdb.Duration, row["typed"]


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
            await RfEvent(id="naive", at=datetime(2026, 1, 2, 3, 4, 5)).save()
            event = await RfEvent.objects().get("naive")
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
    async def test_zero_duration_filter_is_refused_before_sending(self) -> None:
        if encoding.sdk_decodes_zero_duration():
            pytest.skip("the installed SDK decodes a zero duration: the refusal does not apply")
        async with orm_client("rf_event"):
            client = await SurrealDBConnectionManager.get_client()
            with pytest.raises(ValueError, match="zero duration") as excinfo:
                await RfEvent.objects().filter(ttl__gt=timedelta(0)).exec()
            assert type(excinfo.value) is ValueError
            await _assert_client_untouched(client)


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

#: The ``TYPE set<…>`` advice of spec §11, as the README must state it (whitespace normalised).
_SET_ADVICE = (
    "`set[X]`/`frozenset[X]` fields are stored as arrays. On SurrealDB 3.x a `TYPE set<…>` column holds a "
    "native set that the surrealdb SDK 2.0.0 cannot read (every read of the column fails with an SDK decoding "
    "error), and 3.1 refuses an array in it; declare such columns `array<…>`, optionally with "
    "`VALUE $value.distinct()`"
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
        rows = _v0_23_0_rows()
        assert all(len(cells) == 4 and "\\|" not in line for line, cells in rows), rows
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
        header = _normalised(_readme_part("### ORM behaviour: SurrealDB 2.x vs 3.x").split("\n|", 1)[0])
        assert "rows added in v0.23.0 on 2.7.0 and 3.3.4, with 3.1.5 where it differs" in header
