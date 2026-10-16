"""Versioned storage (v0.26.0): ``QuerySet.as_of()`` reads a model's records as they were at a moment.

SurrealQL's ``SELECT … VERSION $_as_of`` on every QuerySet read, with a per-record check that keeps the
rows and aggregates right on both server lines; read-only snapshots; clear ORM errors for stores that keep
no history. Unit tests first (value normalisation, bounds, messages, compiled text: no engine, no server);
the classes that open an engine or a server are suffixed ``E2E``. Most E2E classes need no server at all:
the embedded ``surrealkv+versioned://`` engine runs in-process.
"""

import enum
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest
import surrealdb
from surrealdb import RecordID

from surreal_orm_lite import (
    BaseSurrealEdge,
    BaseSurrealModel,
    Duration,
    PointField,
    SurrealConfigDict,
    SurrealFunc,
    time_travel,
)
from surreal_orm_lite import embedded as embedded_module
from surreal_orm_lite.embedded import history_unsupported, keeps_history, url_kind
from surreal_orm_lite.time_travel import MAX_MOMENT, MIN_MOMENT, AsOf, in_range, line_major, normalise_as_of

# --- shared models and moments (later tasks reuse them) --------------------------------------------


class Snap(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="Snap")

    id: str | None = None
    name: str = ""
    n: int = 0
    g: str = ""
    author: RecordID | None = None
    tags: list[str] = []


class SnapAuthor(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="SnapAuthor")

    id: str | None = None
    name: str = ""


class SnapPlace(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="SnapPlace")

    id: str | None = None
    spot: PointField = None


class SnapBought(BaseSurrealEdge):
    model_config = SurrealConfigDict(table_name="snap_bought")

    quantity: int = 1


M2020 = datetime(2020, 6, 1, tzinfo=UTC)
M2021 = datetime(2021, 6, 1, tzinfo=UTC)

# The exact texts of the messages (spec §5.5), written out once so a drift in a builder shows here.
_T3 = "as_of() takes how long ago as a duration >= 0 in whole nanoseconds, got "
_BOUNDS = "1970-01-01T00:00:00+00:00 to 2262-04-11T23:47:16.854775+00:00"


def _only(as_of: AsOf | None, field: str) -> object:
    """The one field of ``as_of`` that is set (the other two must be ``None``)."""
    assert as_of is not None
    fields = {"moment": as_of.moment, "ago": as_of.ago, "expression": as_of.expression}
    others = {name: value for name, value in fields.items() if name != field}
    assert others == dict.fromkeys(others), f"only {field} may be set: {fields}"
    return fields[field]


class TestNormaliseAsOf:
    """AC-1: every value ``as_of()`` accepts, normalised once, and every value it refuses."""

    def test_none_clears(self) -> None:
        assert normalise_as_of(None) is None

    def test_aware_datetime_in_another_zone_is_the_same_instant_in_utc(self) -> None:
        given = datetime(2021, 6, 1, 2, tzinfo=timezone(timedelta(hours=2)))
        as_of = normalise_as_of(given)
        moment = _only(as_of, "moment")
        assert moment == M2021
        assert isinstance(moment, datetime) and moment.tzinfo is UTC
        assert as_of is not None and as_of.given is given

    def test_aware_utc_datetime(self) -> None:
        assert _only(normalise_as_of(M2021), "moment") == M2021

    def test_naive_datetime_is_utc(self) -> None:
        moment = _only(normalise_as_of(datetime(2021, 6, 1)), "moment")
        assert moment == M2021
        assert isinstance(moment, datetime) and moment.tzinfo is UTC

    @pytest.mark.parametrize(
        "text",
        ["2026-01-01T00:00:00Z", "2026-01-01T02:00:00+02:00", "2026-01-01T00:00:00", "2026-01-01"],
    )
    def test_iso_strings(self, text: str) -> None:
        """Naive strings are UTC; a date alone is that day's midnight UTC."""
        as_of = normalise_as_of(text)
        moment = _only(as_of, "moment")
        assert moment == datetime(2026, 1, 1, tzinfo=UTC)
        assert isinstance(moment, datetime) and moment.tzinfo is UTC
        assert as_of is not None and as_of.given == text

    def test_not_an_iso_string(self) -> None:
        with pytest.raises(ValueError, match="is not an ISO-8601 date-time") as info:
            normalise_as_of("yesterday")
        assert str(info.value) == "as_of() 'yesterday' is not an ISO-8601 date-time"
        assert info.value.__cause__ is None and info.value.__suppress_context__

    @pytest.mark.parametrize("ago", [timedelta(0), timedelta(hours=1)])
    def test_timedelta_is_how_long_ago(self, ago: timedelta) -> None:
        as_of = normalise_as_of(ago)
        assert _only(as_of, "ago") == ago
        assert as_of is not None and as_of.given is ago

    def test_orm_duration_is_how_long_ago(self) -> None:
        ago = Duration.parse("1h")
        assert _only(normalise_as_of(ago), "ago") is ago

    def test_sdk_duration_becomes_an_orm_duration(self) -> None:
        given = surrealdb.Duration(3_600_000_000_000)
        as_of = normalise_as_of(given)
        ago = _only(as_of, "ago")
        assert isinstance(ago, Duration)
        assert ago == Duration(3_600_000_000_000)
        assert as_of is not None and as_of.given is given

    @pytest.mark.parametrize(
        "value",
        [
            timedelta(seconds=-1),
            surrealdb.Duration(-5),
            surrealdb.Duration(1.5),
            surrealdb.Duration(True),
            surrealdb.Duration(Duration.MAX_NANOSECONDS + 1),
        ],
        ids=["negative-timedelta", "negative-sdk", "float-sdk", "bool-sdk", "too-long-sdk"],
    )
    def test_refused_durations(self, value: object) -> None:
        with pytest.raises(ValueError, match=">= 0") as info:
            normalise_as_of(value)
        assert str(info.value) == f"{_T3}{value!r}"
        assert info.value.__cause__ is None  # the ORM ``Duration``'s own error is not chained (``from None``)
        assert info.value.__suppress_context__ or info.value.__context__ is None

    def test_surreal_func_is_an_expression(self) -> None:
        func = SurrealFunc("time::floor(time::now(), 1d)")
        as_of = normalise_as_of(func)
        assert _only(as_of, "expression") is func
        assert as_of is not None and as_of.given is func

    @pytest.mark.parametrize(
        "value",
        [date(2026, 1, 1), 0, 1.5, True, b"x", object()],
        ids=["date", "int", "float", "bool", "bytes", "object"],
    )
    def test_refused_types(self, value: object) -> None:
        with pytest.raises(TypeError, match=r"as_of\(\) takes") as info:
            normalise_as_of(value)
        name = type(value).__name__
        assert str(info.value) == (
            "as_of() takes a datetime, an ISO-8601 string, a timedelta or Duration (how long ago), a SurrealFunc "
            f"or None, not {name}"
        )


class TestMomentBounds:
    """AC-22 (a) at the function level: the moments SurrealDB versions can address, and no other."""

    def test_bounds(self) -> None:
        assert datetime(1970, 1, 1, tzinfo=UTC) == MIN_MOMENT
        assert datetime(2262, 4, 11, 23, 47, 16, 854775, tzinfo=UTC) == MAX_MOMENT

    @pytest.mark.parametrize(
        ("value", "shown"),
        [
            (datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=UTC), "1969-12-31T23:59:59.999999+00:00"),
            (datetime(1900, 1, 1), "1900-01-01T00:00:00+00:00"),
            ("1969-12-31T23:59:59Z", "1969-12-31T23:59:59+00:00"),
            (datetime(1970, 1, 1, 0, 30, tzinfo=timezone(timedelta(hours=1))), "1969-12-31T23:30:00+00:00"),
            (datetime(2262, 4, 11, 23, 47, 16, 854776, tzinfo=UTC), "2262-04-11T23:47:16.854776+00:00"),
            ("9999-12-31T00:00:00Z", "9999-12-31T00:00:00+00:00"),
        ],
        ids=["just-before-1970", "naive-1900", "string-1969", "plus-one-hour", "just-after-2262", "string-9999"],
    )
    def test_out_of_range(self, value: object, shown: str) -> None:
        with pytest.raises(ValueError, match=r"^as_of\(\) takes a moment from") as info:
            normalise_as_of(value)
        assert str(info.value) == (
            f"as_of() takes a moment from {_BOUNDS}, the instants SurrealDB versions can address "
            f"(SurrealDB 2.x reads an earlier moment as the current state), got {shown}"
        )

    @pytest.mark.parametrize(
        "value",
        [
            datetime.max.replace(tzinfo=timezone(timedelta(hours=-2))),
            datetime.min.replace(tzinfo=timezone(timedelta(hours=1))),
        ],
        ids=["max-minus-two-hours", "min-plus-one-hour"],
    )
    def test_out_of_range_beyond_python_utc(self, value: datetime) -> None:
        """No UTC form exists (``astimezone`` overflows): still T11, showing the moment as given."""
        with pytest.raises(ValueError, match=r"^as_of\(\) takes a moment from") as info:
            normalise_as_of(value)
        assert str(info.value).endswith(f", got {value.isoformat()}")

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(MIN_MOMENT, MIN_MOMENT), (MAX_MOMENT, MAX_MOMENT), (datetime(1970, 1, 1), MIN_MOMENT)],
        ids=["min", "max", "naive-1970"],
    )
    def test_accepted_at_the_bounds(self, value: datetime, expected: datetime) -> None:
        assert _only(normalise_as_of(value), "moment") == expected

    def test_in_range(self) -> None:
        tick = timedelta(microseconds=1)
        assert in_range(MIN_MOMENT)
        assert in_range(MAX_MOMENT)
        assert not in_range(MIN_MOMENT - tick)
        assert not in_range(MAX_MOMENT + tick)


@pytest.fixture
def _fresh_url_cache() -> Iterator[None]:
    """``url_kind`` is cached: a test that fakes the SDK's parser starts and ends with an empty cache."""
    url_kind.cache_clear()
    yield
    url_kind.cache_clear()


class TestKeepsHistory:
    """AC-2: an embedded URL keeps history when the SDK names its scheme ``…+versioned`` (a rule, not a list)."""

    def test_versioned_surrealkv(self) -> None:
        assert keeps_history("surrealkv+versioned:///tmp/x") is True

    @pytest.mark.parametrize(
        "url",
        [
            "mem://",
            "memory://",
            "surrealkv:///tmp/x",
            "surrealkv:///tmp/x?versioned=true",
            "file:///tmp/x",
            "ws://h/rpc",
            "http://h",
            "rocksdb:///x",
            None,
            "",
        ],
    )
    def test_no_history(self, url: str | None) -> None:
        assert keeps_history(url) is False

    @pytest.mark.usefixtures("_fresh_url_cache")
    def test_a_versioned_scheme_the_sdk_adds_keeps_history(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_scheme = enum.Enum(
            "FakeScheme",
            {"HTTP": "http", "HTTPS": "https", "WS": "ws", "WSS": "wss", "FOO": "foo+versioned", "MEM": "mem"},
        )

        class FakeUrl:
            def __init__(self, url: str) -> None:
                self.scheme = fake_scheme(urlparse(url).scheme)

        monkeypatch.setattr(embedded_module, "SdkUrlScheme", fake_scheme)
        monkeypatch.setattr(embedded_module, "SdkUrl", FakeUrl)
        assert keeps_history("foo+versioned:///x") is True
        assert keeps_history("mem://") is False
        assert keeps_history("ws://h") is False

    def test_history_unsupported_text(self) -> None:
        assert history_unsupported("mem://") == (
            "as_of() needs a store that keeps history, and the embedded engine at 'mem://' keeps none. Open a "
            "surrealkv+versioned://<path> database (the surrealdb SDK 2.x ignores ?versioned=true), or connect "
            "to a server started with versioned storage."
        )


class TestLineMajor:
    """The server line from ``client.version()``, read as ``changes()`` does (3 when unreadable)."""

    @pytest.mark.parametrize(
        ("version", "major"),
        [("surrealdb-3.3.4", 3), ("surrealdb-2.0.0", 2), ("2.7.0", 2), ("weird", 3)],
    )
    def test_line_major(self, version: str, major: int) -> None:
        assert line_major(version) == major


class TestMessages:
    """Spec §5.5: each message builder gives its exact text."""

    def test_t4_transaction(self) -> None:
        assert time_travel.transaction_refused() == (
            "as_of() reads the database's history, which a transaction cannot join: run this read on "
            "Model.objects(), outside objects(tx=)."
        )

    def test_t5_queryset_method(self) -> None:
        assert time_travel.queryset_refused("bulk_update()") == (
            "bulk_update() is not available on a queryset read as_of(): a time-travel read is read-only and cannot "
            "be streamed. Call it on a queryset without as_of()."
        )

    def test_t7_server(self) -> None:
        assert time_travel.server_refused(Exception("boom")) == (
            "as_of() needs a store that keeps history, and this SurrealDB server's storage does not (boom). Start "
            "SurrealDB 3.x with surrealkv://<path>?versioned=true or rocksdb://<path>?versioned=true, SurrealDB "
            "2.x with surrealkv+versioned://<path>."
        )

    def test_t8_fetch(self) -> None:
        assert time_travel.fetch_refused() == (
            "fetch() under as_of() needs SurrealDB 3.x: SurrealDB 2.x (servers and the embedded engine of the "
            "surrealdb SDK 2.x) resolves fetched links to their current state, not as they were at that moment. "
            "Drop fetch() and read the linked records with their own as_of() query."
        )

    def test_t9_snapshot_write(self) -> None:
        text = time_travel.snapshot_write_refused("Snap", "save()", M2021)
        assert text == (
            "Snap.save() cannot run on an instance read with as_of(): it holds the record as it was at "
            "2021-06-01T00:00:00+00:00, not the current record. To restore that state, build a new instance "
            "from it (Snap(**snapshot.model_dump())) and save() or update() it; refresh() turns the snapshot "
            "back into the current record."
        )
        assert M2021.isoformat() in text

    def test_t10a_not_a_datetime(self) -> None:
        spec = repr(SurrealFunc("'not a date'"))
        assert time_travel.not_a_datetime(spec, "not a date") == (
            "as_of(SurrealFunc(\"'not a date'\")) must give a datetime; the database returned 'not a date'"
        )

    def test_t10b_evaluation_failed(self) -> None:
        spec = repr(SurrealFunc("time::nope()"))
        assert time_travel.evaluation_failed(spec, RuntimeError("Parse error")) == (
            "as_of(SurrealFunc('time::nope()')) could not be evaluated by the database: Parse error"
        )

    def test_t10c_resolved_out_of_range(self) -> None:
        spec = repr(SurrealFunc("time::now() - 600y"))
        text = time_travel.resolved_out_of_range(spec, datetime(1427, 3, 11, tzinfo=UTC))
        assert text == (
            "as_of(SurrealFunc('time::now() - 600y')) gave 1427-03-11T00:00:00+00:00, outside the moments "
            f"SurrealDB versions can address ({_BOUNDS}): SurrealDB 2.x would read an earlier moment as the "
            "current state."
        )
        assert "1427-03-11" in text
        assert MIN_MOMENT.isoformat() in text and MAX_MOMENT.isoformat() in text

    def test_t11_moment_out_of_range(self) -> None:
        assert time_travel.moment_out_of_range(datetime(1900, 1, 1, tzinfo=UTC)) == (
            f"as_of() takes a moment from {_BOUNDS}, the instants SurrealDB versions can address (SurrealDB 2.x "
            "reads an earlier moment as the current state), got 1900-01-01T00:00:00+00:00"
        )

    def test_constants(self) -> None:
        """The SurrealQL fragments every time-travel read shares (spec §6.1, §6.5)."""
        assert time_travel.GUARD == "(SELECT VALUE id FROM ONLY $parent.id VERSION $_as_of) != NONE"
        assert time_travel.VERSION_CLAUSE == " VERSION $_as_of"
        assert time_travel.MOMENT_VARIABLE == "_as_of"
        assert time_travel.AGO_VARIABLE == "_as_of_ago"
        assert time_travel.UNSUPPORTED_MARKER == "does not support versioned queries"
