"""Typed lookups (#220): a ``str`` filter value on a top-level rich field is read as the field's type.

SurrealDB never casts a bound string to a column's type: it compares a string with a datetime, a duration or a uuid
by type order, so ``filter(at__gte="2026-01-01T00:00:00Z")`` on a ``datetime`` field matched by type order, without
an error, until this change. Now, when a lookup (``exact``, ``gt``, ``gte``, ``lt``, ``lte``, and each ``str`` item
of ``in``/``not_in``) targets a field declared ``datetime``, ``date``, ``timedelta``, ``Decimal``, ``UUID`` or the
ORM's ``Duration`` (``Optional`` and ``Annotated`` stripped), a ``str`` value is validated as that type before it is
bound, and the encoding boundary sends the native value. A string the type cannot read raises ``ValueError`` naming
the field and the lookup, before any request. Every other value is bound as before, byte for byte.

Unit tests first (no engine, no server); the ``E2E`` classes run on the embedded ``mem://`` engine and on the server
of ``SURREALDB_PORT`` (both lines in CI), the live query on the server only.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

import pytest
from pydantic import AwareDatetime, Field, FutureDate, FutureDatetime, NaiveDatetime, PastDate, PastDatetime
from surrealdb import BoundExcluded, BoundIncluded, RecordID

from surreal_orm_lite import (
    BaseSurrealModel,
    Duration,
    Q,
    QueryCache,
    Range,
    Subquery,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    Var,
)
from surreal_orm_lite.testing import embedded_database
from surreal_orm_lite.utils import build_filter_condition, rich_filter_type
from tests._readme_tables import assert_server_header_sentence, assert_server_rows, doc_part, guide
from tests.conftest import orm_client


class TlEvent(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="tl_event", populate_by_name=True)

    id: str | None = None
    at: datetime | None = None
    day: date | None = None
    ttl: timedelta | None = None
    grace: Duration | None = None
    price: Decimal | None = None
    token: UUID | None = None
    label: str | None = None
    when_: Annotated[datetime | None, Field(alias="happened")] = None
    stamps: list[datetime] = []
    loose: datetime | str | None = None
    aware: AwareDatetime | None = None


class TlNote(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="tl_note")

    id: str | None = None
    event: Any = None


_TABLES = ("tl_event", "tl_note")

_U1 = UUID("0190b6c4-7c2a-7000-8000-000000000001")
_U2 = UUID("0190b6c4-7c2a-7000-8000-000000000002")
_U3 = UUID("0190b6c4-7c2a-7000-8000-000000000003")
_CUTOFF = "2026-01-01T00:00:00Z"
_CUTOFF_NATIVE = datetime(2026, 1, 1, tzinfo=UTC)


async def _forbidden_client(*args: Any, **kwargs: Any) -> Any:
    pytest.fail("a client was acquired")


@pytest.fixture
def _no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SurrealDBConnectionManager, "get_client", _forbidden_client)


@pytest.fixture(autouse=True)
def _fresh_state() -> Iterator[None]:
    from surreal_orm_lite import live

    QueryCache.configure(default_ttl=60, max_size=1000, enabled=True)
    QueryCache.clear()
    yield
    QueryCache.clear()
    live._SUBSCRIBERS.clear()
    live._RECENTLY_KILLED.clear()


def _where(**filters: Any) -> tuple[str, dict[str, Any]]:
    return TlEvent.objects().filter(**filters)._build_where()


def _bound(**filters: Any) -> Any:
    """The one value a single filter binds."""
    _, variables = _where(**filters)
    [value] = variables.values()
    return value


#: (filter key, string, the native value it stands for)
_READ_AS_THE_FIELD_TYPE = [
    ("at__gte", _CUTOFF, _CUTOFF_NATIVE),
    ("at", "2026-01-01T10:30:00+02:00", datetime(2026, 1, 1, 8, 30, tzinfo=UTC)),
    ("at__lt", "2026-01-01", datetime(2026, 1, 1)),
    ("day__gt", "2026-03-04", date(2026, 3, 4)),
    ("ttl__lte", "PT1H30M", timedelta(hours=1, minutes=30)),
    ("ttl", "01:00:00", timedelta(hours=1)),
    ("grace__gt", "1h30m", Duration.parse("1h30m")),
    ("price__gte", "10.25", Decimal("10.25")),
    ("token", str(_U1), _U1),
    ("token", str(_U1).upper(), _U1),
    ("when___gt", _CUTOFF, _CUTOFF_NATIVE),
    ("aware__gte", _CUTOFF, _CUTOFF_NATIVE),
]


@pytest.mark.usefixtures("_no_client")
class TestStringReadAsTheFieldType:
    @pytest.mark.parametrize(("key", "text", "native"), _READ_AS_THE_FIELD_TYPE)
    def test_scalar_lookup(self, key: str, text: str, native: Any) -> None:
        value = _bound(**{key: text})
        assert type(value) is type(native)
        assert value == native
        assert _where(**{key: text})[0] == _where(**{key: native})[0]

    @pytest.mark.parametrize("lookup", ["in", "not_in"])
    def test_str_items_of_in(self, lookup: str) -> None:
        stamp = datetime(2025, 1, 1, tzinfo=UTC)
        value = _bound(**{f"token__{lookup}": [str(_U1), _U2]})
        assert value == [_U1, _U2]
        assert all(type(item) is UUID for item in value)
        assert value[1] is _U2
        assert _bound(**{f"at__{lookup}": ["2025-01-01T00:00:00Z", stamp]}) == [stamp, stamp]

    def test_aliased_field_compiles_against_its_column(self) -> None:
        sql, variables = _where(when___gte=_CUTOFF)
        assert sql == " WHERE happened >= $_f0"
        assert variables == {"_f0": _CUTOFF_NATIVE}

    def test_q(self) -> None:
        sql, variables = TlEvent.objects().filter(Q(at__gte=_CUTOFF) | ~Q(token=str(_U1)))._build_where()
        assert sql == " WHERE (at >= $_f0 OR NOT (token = $_f1))"
        assert variables == {"_f0": _CUTOFF_NATIVE, "_f1": _U1}
        assert type(variables["_f1"]) is UUID

    def test_nested_q(self) -> None:
        q = (Q(ttl__gt="PT1H") & Q(price__lt="2.5")) | ~Q(label="x", day="2026-01-02")
        sql, variables = TlEvent.objects().filter(q)._build_where()
        assert sql == " WHERE ((ttl > $_f0 AND price < $_f1) OR NOT ((label = $_f2 AND day = $_f3)))"
        assert variables == {"_f0": timedelta(hours=1), "_f1": Decimal("2.5"), "_f2": "x", "_f3": date(2026, 1, 2)}

    def test_subquery_reads_its_own_model_types(self) -> None:
        inner = TlEvent.objects().filter(at__gte=_CUTOFF)
        outer = TlNote.objects().filter(event__in=Subquery(inner))
        compiler = outer._subquery_compiler()
        _, variables = outer._build_where(subqueries=compiler)
        bound = [*compiler.variables.values(), *variables.values()]
        assert _CUTOFF_NATIVE in bound
        assert _CUTOFF not in bound

    def test_live_writes_the_native_literal(self) -> None:
        live = TlEvent.objects().filter(at__gte=_CUTOFF, token=str(_U1), ttl__lt="PT2H")._compile_live(False)
        assert live == (f"LIVE SELECT * FROM tl_event WHERE at >= d'2026-01-01T00:00:00Z' AND token = u'{_U1}' AND ttl < 2h;")

    def test_live_of_a_duration_field(self) -> None:
        live = TlEvent.objects().filter(grace__gte="90m", price="1.5")._compile_live(False)
        assert live == "LIVE SELECT * FROM tl_event WHERE grace >= 1h30m AND price = 1.5dec;"


#: (filter key, unreadable string, the type the message names)
_UNREADABLE = [
    ("at__gte", "not a date", "datetime"),
    ("at", "2026-13-01T00:00:00Z", "datetime"),
    ("day", "yesterday", "date"),
    ("ttl__gt", "1h", "timedelta"),
    ("grace", "PT1H", "Duration"),
    ("price__lt", "cheap", "Decimal"),
    ("price", "NaN", "Decimal"),
    ("token", "not-a-uuid", "UUID"),
]


@pytest.mark.usefixtures("_no_client")
class TestUnreadableString:
    @pytest.mark.parametrize(("key", "text", "kind"), _UNREADABLE)
    def test_raises_naming_field_and_lookup(self, key: str, text: str, kind: str) -> None:
        field, _, lookup = key.partition("__")
        with pytest.raises(ValueError) as raised:
            _where(**{key: text})
        message = str(raised.value)
        assert message.startswith(
            f"The filter value {text!r} given for '{field}' (lookup '{lookup or 'exact'}') is not a valid {kind}: "
        )

    def test_item_of_in(self) -> None:
        with pytest.raises(ValueError, match=r"^An item 'x' of the filter value given for 'token' \(lookup 'in'\)"):
            _where(token__in=[_U1, "x"])

    def test_aliased_field_names_the_python_field(self) -> None:
        with pytest.raises(ValueError, match=r"given for 'when_' \(lookup 'gt'\) is not a valid datetime"):
            _where(when___gt="soon")

    async def test_before_any_request(self) -> None:
        qs = TlEvent.objects().filter(at__gte="soon")
        for call in (qs.exec, qs.count, qs.exists, qs.first, qs.bulk_delete):
            with pytest.raises(ValueError, match="is not a valid datetime"):
                await call()
        with pytest.raises(ValueError, match="is not a valid datetime"):
            await TlEvent.objects().filter(at__gte="soon").bulk_update(label="x")
        with pytest.raises(ValueError, match="is not a valid UUID"):
            await TlEvent.objects().get_or_create(token="nope")
        with pytest.raises(ValueError, match="is not a valid UUID"):
            await TlEvent.objects().update_or_create(token="nope", defaults={"label": "x"})
        with pytest.raises(ValueError, match="is not a valid datetime"):
            TlEvent.objects().filter(Q(at="soon")).live()


#: Values bound as v0.22.2 bound them: compared with the filter compiled with no field types at all.
_UNCHANGED = [
    ("at", "exact", 1700000000),
    ("at", "gt", _CUTOFF_NATIVE),
    ("price", "gt", 1.5),
    ("price", "exact", float("nan")),
    ("price", "lt", float("inf")),
    ("price", "exact", 3),
    ("token", "exact", _U1),
    ("at", "exact", None),
    ("at", "exact", "$x"),
    ("at", "gt", "$auth"),
    ("token", "in", ["$x", _U1]),
    ("label", "exact", "2026-01-01T00:00:00Z"),
    ("label", "in", ["2026-01-01T00:00:00Z", "x"]),
    ("at", "startswith", "2026"),
    ("at", "contains", "2026"),
    ("at", "not_contains", "2026"),
    ("stamps", "contains", _CUTOFF),
    ("stamps", "containsany", [_CUTOFF]),
    ("loose", "gte", _CUTOFF),
    ("at.year", "exact", "2026"),
    ("at", "in", Range(BoundIncluded("2025-01-01T00:00:00Z"), BoundExcluded("2026-01-01T00:00:00Z"))),
    ("missing", "exact", _CUTOFF),
]


@pytest.mark.usefixtures("_no_client")
class TestOtherValuesUnchanged:
    @pytest.mark.parametrize(("field", "lookup", "value"), _UNCHANGED)
    def test_bound_as_before(self, field: str, lookup: str, value: Any) -> None:
        key = field if lookup == "exact" else f"{field}__{lookup}"
        typed = build_filter_condition(field, lookup, value, 0, "tl_event", field_types=TlEvent._filter_types())
        untyped = build_filter_condition(field, lookup, value, 0, "tl_event")
        assert typed[0] == untyped[0]
        assert typed[2] == untyped[2]
        assert typed[1].keys() == untyped[1].keys()
        for name, bound in typed[1].items():
            assert type(bound) is type(untyped[1][name])
            assert repr(bound) == repr(untyped[1][name])
        if "." not in field and field != "missing":
            sql, variables = TlEvent.objects().filter(**{key: value})._build_where()
            assert sql == f" WHERE {typed[0]}"
            assert variables.keys() == typed[1].keys()
            for name, bound in variables.items():
                assert type(bound) is type(typed[1][name])
                assert repr(bound) == repr(typed[1][name])

    def test_the_same_object_is_bound(self) -> None:
        for value in (1.5, _U1, _CUTOFF_NATIVE, 3):
            assert _bound(price=value) is value
        items = ["$x", _U1]
        assert _bound(token__in=items) == items

    def test_isnull_var_and_model_instance(self) -> None:
        assert _where(at__isnull=True) == (" WHERE at IS NULL", {})
        assert _where(at__gte=Var("since")) == (" WHERE at >= $since", {})
        assert TlNote.objects().filter(event=TlEvent(id="e1"))._build_where()[1] == {"_f0": RecordID("tl_event", "e1")}

    def test_record_id_column(self) -> None:
        assert _where(id="e1") == (" WHERE id = $_f0", {"_f0": RecordID("tl_event", "e1")})

    def test_q_compiled_on_its_own(self) -> None:
        sql, variables, _ = Q(at__gte=_CUTOFF).to_sql()
        assert (sql, variables) == ("at >= $_f0", {"_f0": _CUTOFF})

    def test_model_with_no_rich_field(self) -> None:
        assert TlNote._filter_types() == {}
        assert TlNote.objects().filter(event=_CUTOFF)._build_where() == (" WHERE event = $_f0", {"_f0": _CUTOFF})

    def test_filter_types(self) -> None:
        assert TlEvent._filter_types() == {
            "at": ("at", datetime),
            "day": ("day", date),
            "ttl": ("ttl", timedelta),
            "grace": ("grace", Duration),
            "price": ("price", Decimal),
            "token": ("token", UUID),
            "happened": ("when_", datetime),
            "aware": ("aware", datetime),
        }

    @pytest.mark.parametrize(
        ("annotation", "kind"),
        [
            (AwareDatetime, datetime),
            (AwareDatetime | None, datetime),
            (NaiveDatetime | None, datetime),
            (PastDatetime, datetime),
            (FutureDatetime | None, datetime),
            (Annotated[AwareDatetime | None, Field(description="x")], datetime),
            (PastDate | None, date),
            (FutureDate, date),
        ],
    )
    def test_pydantic_datetime_markers_read_as_their_base_type(self, annotation: Any, kind: type) -> None:
        """Pydantic's ``AwareDatetime`` & co. are classes of their own at run time, not ``Annotated[datetime, …]``."""
        assert rich_filter_type(annotation) is kind


# --------------------------------------------------------------------------- E2E


@contextlib.asynccontextmanager
async def events_db(kind: str) -> AsyncIterator[Any]:
    """The ORM on *kind*: a fresh ``mem://`` engine, or the server of ``SURREALDB_PORT`` over WebSocket.

    Seeded through ``save()``, so every rich value is stored with its SurrealDB type: ``e1`` in 2025 and ``e2`` in
    2026, each with a duration, a decimal and a uuid.
    """
    context = embedded_database("mem://") if kind == "mem" else orm_client(*_TABLES)
    async with context as client:
        await TlEvent(
            id="e1",
            at=datetime(2025, 6, 1, tzinfo=UTC),
            day=date(2025, 6, 1),
            ttl=timedelta(minutes=30),
            grace=Duration.parse("30m"),
            price=Decimal("1.5"),
            token=_U1,
            label="old",
        ).save()
        await TlEvent(
            id="e2",
            at=datetime(2026, 6, 1, tzinfo=UTC),
            day=date(2026, 6, 1),
            ttl=timedelta(hours=2),
            grace=Duration.parse("2h"),
            price=Decimal("10"),
            token=_U2,
            label="new",
        ).save()
        yield client


def _ids(rows: list[Any]) -> list[str]:
    return sorted(str(row.id) for row in rows)


def _key(row: Any) -> str:
    """The key of *row*'s record id, whether ``id`` holds the key or the ``RecordID`` a write returned."""
    return str(row.id).rpartition(":")[2]


#: (string filter, native filter, the ids both find)
_SAME_ROWS = [
    ({"at__gte": _CUTOFF}, {"at__gte": _CUTOFF_NATIVE}, ["e2"]),
    ({"at__lt": _CUTOFF}, {"at__lt": _CUTOFF_NATIVE}, ["e1"]),
    ({"at": "2025-06-01T00:00:00Z"}, {"at": datetime(2025, 6, 1, tzinfo=UTC)}, ["e1"]),
    ({"day__gte": "2026-01-01"}, {"day__gte": date(2026, 1, 1)}, ["e2"]),
    ({"ttl__gt": "PT1H"}, {"ttl__gt": timedelta(hours=1)}, ["e2"]),
    ({"ttl__lte": "00:30:00"}, {"ttl__lte": timedelta(minutes=30)}, ["e1"]),
    ({"grace__gt": "1h"}, {"grace__gt": Duration.parse("1h")}, ["e2"]),
    ({"price__gt": "2"}, {"price__gt": Decimal("2")}, ["e2"]),
    ({"token": str(_U1)}, {"token": _U1}, ["e1"]),
    ({"token__in": [str(_U1), str(_U2)]}, {"token__in": [_U1, _U2]}, ["e1", "e2"]),
    ({"token__not_in": [str(_U1)]}, {"token__not_in": [_U1]}, ["e2"]),
]


#: (field, a string standing for e1's value, that value, the SurrealQL method that checks its stored type)
_CRITERIA_OF_E1 = [
    ("at", "2025-06-01T00:00:00Z", datetime(2025, 6, 1, tzinfo=UTC), "is_datetime"),
    ("token", str(_U1), _U1, "is_uuid"),
    ("ttl", "PT30M", timedelta(minutes=30), "is_duration"),
]

#: (field, a string no seeded row holds, the SurrealQL method that checks its stored type)
_CRITERIA_OF_NO_ROW = [
    ("at", "2027-01-01T00:00:00Z", "is_datetime"),
    ("token", str(_U3), "is_uuid"),
    ("ttl", "PT1H", "is_duration"),
]


async def _typed_ids(client: Any, field: str, is_kind: str) -> list[str]:
    """The ids of the rows whose *field* is stored with the SurrealDB type *is_kind* checks (``is_datetime``…)."""
    result = await client.query(f"SELECT VALUE id FROM tl_event WHERE {field}.{is_kind}();", {})
    rows = result[0] if result and isinstance(result[0], list) else result
    return sorted(str(row).partition(":")[2] for row in rows)


@pytest.mark.parametrize("kind", ["mem", "ws"])
class TestTypedLookupsE2E:
    @pytest.mark.parametrize(("text", "native", "expected"), _SAME_ROWS)
    async def test_filter_finds_the_rows_of_the_native_value(
        self, kind: str, text: dict[str, Any], native: dict[str, Any], expected: list[str]
    ) -> None:
        async with events_db(kind):
            assert _ids(await TlEvent.objects().filter(**native).exec()) == expected
            assert _ids(await TlEvent.objects().filter(**text).exec()) == expected
            assert await TlEvent.objects().filter(**text).count() == len(expected)

    async def test_q(self, kind: str) -> None:
        async with events_db(kind):
            assert _ids(await TlEvent.objects().filter(Q(at__lt=_CUTOFF) | Q(token=str(_U2))).exec()) == ["e1", "e2"]
            assert _ids(await TlEvent.objects().filter(~Q(token=str(_U1))).exec()) == ["e2"]

    async def test_get_or_create(self, kind: str) -> None:
        async with events_db(kind):
            found, created = await TlEvent.objects().get_or_create(token=str(_U1))
            assert (found.id, created) == ("e1", False)
            made, created = await TlEvent.objects().get_or_create(token=str(_U3), defaults={"id": "e3"})
            assert (made.token, created) == (_U3, True)
            again, created = await TlEvent.objects().get_or_create(token=str(_U3))
            assert (again.id, created) == ("e3", False)

    async def test_update_or_create(self, kind: str) -> None:
        async with events_db(kind):
            updated, created = await TlEvent.objects().update_or_create(
                at="2025-06-01T00:00:00Z", defaults={"label": "touched"}
            )
            assert (updated.label, created) == ("touched", False)
            assert {row.id: row.label for row in await TlEvent.objects().all()} == {"e1": "touched", "e2": "new"}

    @pytest.mark.parametrize(("field", "text", "native", "is_kind"), _CRITERIA_OF_E1)
    async def test_update_or_create_keeps_the_stored_type(
        self, kind: str, field: str, text: str, native: Any, is_kind: str
    ) -> None:
        """The ``str`` criterion of the found row is written back as the field's type: the row keeps its native
        value, a native lookup still finds it and the next identical call updates it again (no duplicate)."""
        async with events_db(kind) as client:
            for label in ("touched", "again"):
                updated, created = await TlEvent.objects().update_or_create(**{field: text}, defaults={"label": label})
                assert (_key(updated), updated.label, created) == ("e1", label, False)
                assert await _typed_ids(client, field, is_kind) == ["e1", "e2"]
                assert _ids(await TlEvent.objects().filter(**{field: native}).exec()) == ["e1"]
            assert await TlEvent.objects().count() == 2

    @pytest.mark.parametrize(("field", "text", "is_kind"), _CRITERIA_OF_NO_ROW)
    async def test_update_or_create_creates_once_with_the_field_type(
        self, kind: str, field: str, text: str, is_kind: str
    ) -> None:
        async with events_db(kind) as client:
            made, created = await TlEvent.objects().update_or_create(**{field: text}, defaults={"id": "e3"})
            assert (_key(made), created) == ("e3", True)
            again, created = await TlEvent.objects().update_or_create(**{field: text}, defaults={"label": "again"})
            assert (_key(again), again.label, created) == ("e3", "again", False)
            assert await _typed_ids(client, field, is_kind) == ["e1", "e2", "e3"]
            assert await TlEvent.objects().count() == 3

    async def test_bulk_update(self, kind: str) -> None:
        async with events_db(kind):
            assert await TlEvent.objects().filter(at__gte=_CUTOFF).bulk_update(label="hit") == 1
            assert {row.id: row.label for row in await TlEvent.objects().all()} == {"e1": "old", "e2": "hit"}

    async def test_bulk_delete(self, kind: str) -> None:
        async with events_db(kind):
            assert await TlEvent.objects().filter(token__in=[str(_U1)]).bulk_delete() == 1
            assert _ids(await TlEvent.objects().all()) == ["e2"]

    async def test_rows_stored_as_strings_and_the_upgrade_statement(self, kind: str) -> None:
        """A datetime stored as a string (``merge()``/``bulk_update()`` up to v0.22.x, #221) no longer matches a
        string lookup; the one-off statement of the upgrade note converts it on every line."""
        async with events_db(kind) as client:
            await client.query("CREATE tl_event:legacy SET at = '2026-03-01T00:00:00Z', label = 'legacy';")
            assert _ids(await TlEvent.objects().filter(at__gte=_CUTOFF).exec()) == ["e2"]
            await client.query("UPDATE (SELECT VALUE id FROM tl_event WHERE at.is_string()) SET at = <datetime> at;")
            assert _ids(await TlEvent.objects().filter(at__gte=_CUTOFF).exec()) == ["e2", "legacy"]


async def _take(stream: Any, count: int, timeout: float = 10.0) -> list[Any]:
    async def _read() -> list[Any]:
        seen: list[Any] = []
        async for item in stream:
            seen.append(item)
            if len(seen) >= count:
                break
        return seen

    return await asyncio.wait_for(_read(), timeout=timeout)


class TestTypedLiveQueryE2E:
    """A live query (server only: the embedded engine has none) with string values on rich fields."""

    async def test_live(self) -> None:
        async with orm_client(*_TABLES) as client:
            await client.query("DEFINE TABLE tl_event SCHEMALESS;", {})
            async with TlEvent.objects().filter(at__gte=_CUTOFF, token__in=[str(_U2)]).live() as stream:
                await TlEvent(id="old", at=datetime(2025, 6, 1, tzinfo=UTC), token=_U2).save()
                await TlEvent(id="other", at=datetime(2026, 6, 1, tzinfo=UTC), token=_U1).save()
                await TlEvent(id="hit", at=datetime(2026, 6, 1, tzinfo=UTC), token=_U2).save()
                [event] = await _take(stream, 1)
                assert (event.instance.id, event.instance.token) == ("hit", _U2)


# --------------------------------------------------------------------------- docs

_SERVER_ROW = [
    "A `str` filter value on a `datetime`, `date`, `timedelta`/`Duration`, `Decimal` or `UUID` field",
    "same on both lines: read as the field's type before it is bound, so it finds the rows the native value finds; an "
    "unreadable string raises `ValueError`",
    "same on both lines (verified on 3.3.5)",
    "v0.50.14",
]
_HEADER_SENTENCE = "The row added in v0.50.14 was measured on 2.7.0, 3.3.5, 4.0.1 and the embedded engine."


class TestTypedLookupsDocs:
    def test_behaviour_table(self) -> None:
        assert_server_rows([_SERVER_ROW])
        assert_server_header_sentence(_HEADER_SENTENCE)

    def test_querying_guide_section(self) -> None:
        part = " ".join(doc_part("### String values on rich fields").split())
        for text in (
            'filter(at__gte="2026-01-01T00:00:00Z")',
            "SurrealDB never casts a bound string to a column's type",
            "each `str` item of `in` and `not_in`",
            "raises `ValueError` naming the field and the lookup, before any request",
            "`Q(...).to_sql()`",
            "**Upgrading: rows stored as strings.**",
            "UPDATE (SELECT VALUE id FROM event WHERE at.is_string()) SET at = <datetime> at;",
        ):
            assert text in part, text

    def test_no_guide_says_strings_are_not_converted(self) -> None:
        for name in ("querying.md", "fields.md"):
            text = " ".join(guide(name).split())
            assert "strings are not converted" not in text.lower(), name
