"""Rich field types (v0.23.0) through the ORM: models whose fields hold durations, dates, sets,
ranges, decimals, UUIDs and bytes, written and read on both server lines.

Every class here is E2E (suffixed ``E2E``) unless it says otherwise. Tables are prefixed ``rf_``
and created inside ``orm_client(...)``, which drops them before and after the test.
"""

import contextlib
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
import surrealdb
from pydantic import ValidationError
from surrealdb import RecordID
from surrealdb.cbor import undefined

from surreal_orm_lite import (
    AuthenticatedUserMixin,
    BaseSurrealEdge,
    BaseSurrealModel,
    BoundExcluded,
    BoundIncluded,
    BufferedTransaction,
    Duration,
    Range,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    SurrealFunc,
    encoding,
)
from tests.conftest import orm_client
from tests.test_model_auth import AUTH_PASSWORD, _connect, _unique_email
from tests.test_rich_types import _http_client, _http_url
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


async def _stored_user(email: str) -> dict[str, Any]:
    root = await _http_client()
    rows = await root.query("SELECT * FROM rf_user WHERE email = $e;", {"e": email})
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


@contextlib.contextmanager
def _returned_ttl_not_read_yet() -> Iterator[None]:
    """``bulk_create()`` wrote its rows; hydrating the rows it returns still refuses ``ttl``.

    The server answers with an SDK ``Duration``, which a ``timedelta`` field accepts only once the
    read rule for top-level ``timedelta`` fields (spec §6.3) is in place. That rule replaces this
    pin with an assertion on the returned instances: this ``pytest.raises`` fails as soon as it lands.
    """
    with pytest.raises(ValidationError, match=r"ttl\n\s+Input should be a valid timedelta"):
        yield


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
    with _returned_ttl_not_read_yet():
        await RfEvent.objects().bulk_create([RfEvent(id="w", **FULL)])
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
    with _returned_ttl_not_read_yet():
        await RfLink.objects().bulk_create([RfLink(id="w", in_=_A, out=_B, ttl=FULL["ttl"], day=FULL["day"])])
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
