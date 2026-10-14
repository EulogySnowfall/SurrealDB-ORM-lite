"""Rich field types (v0.23.0) through the ORM: models whose fields hold durations, dates, sets,
ranges, decimals, UUIDs and bytes, written and read on both server lines.

Every class here is E2E (suffixed ``E2E``) unless it says otherwise. Tables are prefixed ``rf_``
and created inside ``orm_client(...)``, which drops them before and after the test.
"""

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
import surrealdb
from surrealdb import RecordID

from surreal_orm_lite import (
    AuthenticatedUserMixin,
    BaseSurrealModel,
    BoundExcluded,
    BoundIncluded,
    Duration,
    Range,
    SurrealConfigDict,
    SurrealDBConnectionManager,
)
from tests.conftest import orm_client
from tests.test_model_auth import AUTH_PASSWORD, _connect, _unique_email
from tests.test_rich_types import _http_client

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


def _assert_native(row: dict[str, Any]) -> None:
    """*row* (written from ``FULL``) holds SurrealDB's native types, as the SDK reads them back."""
    assert type(row["ttl"]) is surrealdb.Duration and row["ttl"].elapsed == 5400 * 10**9
    assert row["grace"].elapsed == 5400 * 10**9 + 1
    assert row["price"] == Decimal("12.5")
    assert type(row["token"]) is UUID
    assert type(row["hours"]) is surrealdb.Range and FULL["hours"] == row["hours"]
    assert row["period"].end is None
    assert row["blob"] == b"\x00\x01\xff"
    assert row["at"].tzinfo is not None
    assert row["day"] == datetime(2026, 1, 2, tzinfo=UTC)
    assert row["tags"] == ["a", "b"]
    assert [d.elapsed for d in row["durs"]] == [3600 * 10**9]


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
