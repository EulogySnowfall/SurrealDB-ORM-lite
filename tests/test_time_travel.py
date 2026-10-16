"""Versioned storage (v0.26.0): ``QuerySet.as_of()`` reads a model's records as they were at a moment.

SurrealQL's ``SELECT … VERSION $_as_of`` on every QuerySet read, with a per-record check that keeps the
rows and aggregates right on both server lines; read-only snapshots; clear ORM errors for stores that keep
no history. Unit tests first (value normalisation, bounds, messages, compiled text: no engine, no server);
the classes that open an engine or a server are suffixed ``E2E``. Most E2E classes need no server at all:
the embedded ``surrealkv+versioned://`` engine runs in-process. The server classes come last: the server of
``SURREALDB_PORT``, started in memory (no history), and one started with versioned storage, found through
``SURREALDB_VERSIONED_PORT`` (``SURREALDB_VERSIONED_HOST``, default ``SURREALDB_HOST``), skipped without it.
Last, what README section 27, the behaviour tables and ``docs/ROADMAP.md`` say, and the README's examples
run on the embedded engine.
"""

import ast
import asyncio
import contextlib
import enum
import inspect
import itertools
import os
import pickle
import re
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
import surrealdb
from pydantic import PrivateAttr
from surrealdb import RecordID

from surreal_orm_lite import (
    AuthenticatedUserMixin,
    BaseSurrealEdge,
    BaseSurrealModel,
    Count,
    Duration,
    GeoDistance,
    GeometryPoint,
    PointField,
    Q,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    SurrealDbError,
    SurrealDbNotFoundError,
    SurrealFunc,
    _sdk,
    pre_delete,
    pre_save,
    pre_update,
    time_travel,
)
from surreal_orm_lite import embedded as embedded_module
from surreal_orm_lite.embedded import history_unsupported, keeps_history, url_kind
from surreal_orm_lite.model_base import _snapshot_moment
from surreal_orm_lite.testing import embedded_database
from surreal_orm_lite.time_travel import MAX_MOMENT, MIN_MOMENT, AsOf, in_range, line_major, normalise_as_of
from tests.conftest import orm_client
from tests.test_embedded import _cells, _normalised, _readme_part, _subsection, _table_rows, _tables

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


class SnapMine(BaseSurrealModel):
    """A model with a private attribute of its own: the snapshot marker must not replace it (P1)."""

    model_config = SurrealConfigDict(table_name="SnapMine")

    id: str | None = None
    n: int = 0
    _mine: int = PrivateAttr(default=0)


class SnapUser(AuthenticatedUserMixin, BaseSurrealModel):
    """An auth model: the mixin's classmethod ``refresh(refresh_token)`` shadows the instance ``refresh()``."""

    model_config = SurrealConfigDict(table_name="SnapUser")

    id: str | None = None
    email: str = ""
    password: str = ""


M2020 = datetime(2020, 6, 1, tzinfo=UTC)
M2021 = datetime(2021, 6, 1, tzinfo=UTC)

# The exact texts of the messages (spec §5.5), written out once so a drift in a builder shows here.
_T3 = "as_of() takes how long ago as a duration >= 0 in whole nanoseconds, got "
_BOUNDS = "1970-01-01T00:00:00+00:00 to 2262-04-11T23:47:16.854775+00:00"


def _t11(shown: str) -> str:
    """T11's exact text for a moment whose UTC ``isoformat()`` is *shown*."""
    return (
        f"as_of() takes a moment from {_BOUNDS}, the instants SurrealDB versions can address "
        f"(SurrealDB 2.x reads an earlier moment as the current state), got {shown}"
    )


# AC-22 (a): absolute moments outside the bounds, with the UTC ``isoformat()`` T11 shows for each.
_OUT_OF_RANGE: list[tuple[object, str]] = [
    (datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=UTC), "1969-12-31T23:59:59.999999+00:00"),
    (datetime(1900, 1, 1), "1900-01-01T00:00:00+00:00"),
    ("1969-12-31T23:59:59Z", "1969-12-31T23:59:59+00:00"),
    (datetime(1970, 1, 1, 0, 30, tzinfo=timezone(timedelta(hours=1))), "1969-12-31T23:30:00+00:00"),
    (datetime(2262, 4, 11, 23, 47, 16, 854776, tzinfo=UTC), "2262-04-11T23:47:16.854776+00:00"),
    ("9999-12-31T00:00:00Z", "9999-12-31T00:00:00+00:00"),
]
_OUT_OF_RANGE_IDS = ["just-before-1970", "naive-1900", "string-1969", "plus-one-hour", "just-after-2262", "string-9999"]


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

    @pytest.mark.parametrize(("value", "shown"), _OUT_OF_RANGE, ids=_OUT_OF_RANGE_IDS)
    def test_out_of_range(self, value: object, shown: str) -> None:
        with pytest.raises(ValueError, match=r"^as_of\(\) takes a moment from") as info:
            normalise_as_of(value)
        assert str(info.value) == _t11(shown)

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


# --- QuerySet.as_of(): compiled SurrealQL and refusals at the call (no engine, no server) -----------

# The per-record check every time-travel table read carries (spec §6.5, M7).
_G = "(SELECT VALUE id FROM ONLY $parent.id VERSION $_as_of) != NONE"


class TestCompileAsOf:
    """AC-3: the SurrealQL of spec §6.5, byte for byte; without ``as_of()``, v0.25.0's text, byte for byte."""

    def test_as_of_returns_the_queryset_and_the_last_call_wins(self) -> None:
        qs = Snap.objects()
        assert qs.as_of(M2021) is qs
        assert qs.as_of(M2020) is qs
        assert qs._as_of is not None and qs._as_of.moment == M2020
        assert qs.as_of(None) is qs
        assert qs._as_of is None

    def test_read_with_every_clause(self) -> None:
        qs = Snap.objects().as_of(M2021).filter(n__gt=0).order_by("-n").limit(5).offset(2).fetch("author")
        assert qs._compile_query(M2021) == (
            "SELECT * FROM Snap WHERE n > $_f0 AND (SELECT VALUE id FROM ONLY $parent.id VERSION $_as_of) != NONE "
            "ORDER BY n DESC LIMIT 5 START 2 FETCH author VERSION $_as_of;",
            {"_f0": 0, "_as_of": M2021},
        )

    def test_read_without_a_filter(self) -> None:
        assert Snap.objects().as_of(M2021)._compile_query(M2021) == (
            "SELECT * FROM Snap WHERE (SELECT VALUE id FROM ONLY $parent.id VERSION $_as_of) != NONE VERSION $_as_of;",
            {"_as_of": M2021},
        )

    def test_count(self) -> None:
        assert Snap.objects().as_of(M2021)._compile_aggregation_query("count()", moment=M2021) == (
            "SELECT count() FROM Snap WHERE (SELECT VALUE id FROM ONLY $parent.id VERSION $_as_of) != NONE "
            "GROUP ALL VERSION $_as_of;",
            {"_as_of": M2021},
        )

    def test_sum(self) -> None:
        qs = Snap.objects().as_of(M2021)
        assert qs._compile_aggregation_query("math::sum(n)", alias="sum", moment=M2021) == (
            "SELECT math::sum(n) AS sum FROM Snap WHERE (SELECT VALUE id FROM ONLY $parent.id VERSION $_as_of) != NONE "
            "GROUP ALL VERSION $_as_of;",
            {"_as_of": M2021},
        )

    def test_values_and_annotate(self) -> None:
        qs = Snap.objects().as_of(M2021).values("g").annotate(c=Count()).order_by("g").limit(5)
        assert qs._compile_group_by_query(M2021) == (
            "SELECT g, count() AS c FROM Snap WHERE (SELECT VALUE id FROM ONLY $parent.id VERSION $_as_of) != NONE "
            "GROUP BY g ORDER BY g ASC LIMIT 5 VERSION $_as_of;",
            {"_as_of": M2021},
        )

    def test_filtered_aggregation_and_group(self) -> None:
        """The guard follows the filters in the aggregation and GROUP BY compilers too."""
        qs = Snap.objects().as_of(M2021).filter(n__gt=2)
        assert qs._compile_aggregation_query("count()", moment=M2021) == (
            f"SELECT count() FROM Snap WHERE n > $_f0 AND {_G} GROUP ALL VERSION $_as_of;",
            {"_f0": 2, "_as_of": M2021},
        )
        grouped = Snap.objects().as_of(M2021).filter(n__gt=2).values("g").annotate(c=Count())
        assert grouped._compile_group_by_query(M2021) == (
            f"SELECT g, count() AS c FROM Snap WHERE n > $_f0 AND {_G} GROUP BY g VERSION $_as_of;",
            {"_f0": 2, "_as_of": M2021},
        )

    def test_guard_after_nearby(self) -> None:
        qs = SnapPlace.objects().as_of(M2021).nearby("spot", (0, 0), 1000)
        assert qs._compile_query(M2021) == (
            f"SELECT * FROM SnapPlace WHERE (spot.is_point() AND geo::distance(spot, $_f0) <= $_f1) AND {_G} VERSION $_as_of;",
            {"_f0": GeometryPoint(0, 0), "_f1": 1000.0, "_as_of": M2021},
        )

    def test_guard_after_an_or_group(self) -> None:
        """A ``Q`` OR group is parenthesised, so the guard applies to every row it keeps."""
        qs = Snap.objects().as_of(M2021).filter(Q(name="a") | Q(name="b"))
        assert qs._compile_query(M2021) == (
            f"SELECT * FROM Snap WHERE (name = $_f0 OR name = $_f1) AND {_G} VERSION $_as_of;",
            {"_f0": "a", "_f1": "b", "_as_of": M2021},
        )

    def test_geo_distance_keeps_its_projection(self) -> None:
        qs = SnapPlace.objects().as_of(M2021).annotate(d=GeoDistance("spot", (0, 0)))
        assert qs._compile_query(M2021) == (
            f"SELECT *, IF spot.is_point() THEN geo::distance(spot, $_gd0) END AS d FROM SnapPlace WHERE {_G} VERSION $_as_of;",
            {"_gd0": GeometryPoint(0, 0), "_as_of": M2021},
        )

    @pytest.mark.parametrize(
        ("build", "expected"),
        [
            (lambda: Snap.objects()._compile_query(), ("SELECT * FROM Snap;", {})),
            (
                lambda: Snap.objects().filter(n__gt=0).order_by("-n").limit(5).offset(2).fetch("author")._compile_query(),
                ("SELECT * FROM Snap WHERE n > $_f0 ORDER BY n DESC LIMIT 5 START 2 FETCH author;", {"_f0": 0}),
            ),
            (
                lambda: Snap.objects().values("g").annotate(c=Count())._compile_group_by_query(),
                ("SELECT g, count() AS c FROM Snap GROUP BY g;", {}),
            ),
            (lambda: Snap.objects()._compile_aggregation_query("count()"), ("SELECT count() FROM Snap GROUP ALL;", {})),
            (
                lambda: Snap.objects()._compile_aggregation_query("math::sum(n)", alias="sum"),
                ("SELECT math::sum(n) AS sum FROM Snap GROUP ALL;", {}),
            ),
        ],
        ids=["no-clause", "every-clause", "values-annotate", "count", "sum"],
    )
    def test_without_as_of_the_v025_text(self, build: Callable[[], Any], expected: tuple[str, dict[str, Any]]) -> None:
        assert build() == expected

    @pytest.mark.parametrize(
        "compile_",
        [
            lambda qs, moment: qs._compile_query(moment),
            lambda qs, moment: qs._compile_aggregation_query("count()", moment=moment),
            lambda qs, moment: qs.values("g").annotate(c=Count())._compile_group_by_query(moment),
        ],
        ids=["query", "aggregation", "group-by"],
    )
    def test_compiled_on_as_of_not_on_the_argument(self, compile_: Callable[[Any, datetime | None], Any]) -> None:
        """A moment passed to a queryset without ``as_of()`` (or cleared by ``as_of(None)``) changes nothing."""
        build = lambda: Snap.objects().filter(n__gt=0)  # noqa: E731
        plain = compile_(build(), None)
        assert "VERSION" not in plain[0] and "_as_of" not in plain[1]
        assert compile_(build(), M2021) == plain
        assert compile_(build().as_of(M2021).as_of(None), None) == plain
        assert compile_(build().as_of(M2021).as_of(None), M2021) == plain

    @pytest.mark.parametrize(
        "compile_",
        [
            lambda qs: qs._compile_query(),
            lambda qs: qs._compile_aggregation_query("count()"),
            lambda qs: qs.values("g").annotate(c=Count())._compile_group_by_query(),
        ],
        ids=["query", "aggregation", "group-by"],
    )
    def test_a_time_travel_read_needs_its_moment(self, compile_: Callable[[Any], Any]) -> None:
        """A forgotten resolution must never fall back to the current data."""
        with pytest.raises(RuntimeError) as info:
            compile_(Snap.objects().as_of(M2021))
        assert str(info.value) == "internal: a time-travel read was compiled without its moment"

    def test_the_moment_overrides_a_variable_named_as_of(self) -> None:
        """``_as_of`` is a reserved ``variables()`` name: the moment wins; the other variables stay."""
        qs = Snap.objects().variables(_as_of="x", cutoff=1).as_of(M2021)
        assert qs._compile_query(M2021)[1] == {"_as_of": M2021, "cutoff": 1}
        assert qs._compile_aggregation_query("count()", moment=M2021)[1] == {"_as_of": M2021, "cutoff": 1}
        grouped = Snap.objects().variables(_as_of="x").as_of(M2021).values("g").annotate(c=Count())
        assert grouped._compile_group_by_query(M2021)[1] == {"_as_of": M2021}


@pytest.fixture
def _no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything asks for a client: the refusals come before any request."""

    async def _forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("a request was made")

    monkeypatch.setattr(SurrealDBConnectionManager, "get_client", _forbidden)


# The eleven methods of spec §5.3 a time-travel queryset refuses (T5), each called as a caller would.
_REFUSED_METHODS: list[tuple[str, Callable[[Any], Any]]] = [
    ("bulk_create()", lambda qs: qs.bulk_create([])),
    ("bulk_create()", lambda qs: qs.bulk_create([Snap(id="x")])),
    ("bulk_update()", lambda qs: qs.bulk_update(n=1)),
    ("bulk_update()", lambda qs: qs.bulk_update()),
    ("bulk_delete()", lambda qs: qs.bulk_delete()),
    ("patch()", lambda qs: qs.patch([{"op": "replace", "path": "/n", "value": 1}])),
    ("update_or_create()", lambda qs: qs.update_or_create(name="x")),
    ("get_or_create()", lambda qs: qs.get_or_create(name="x")),
    ("delete_table()", lambda qs: qs.delete_table()),
    ("query()", lambda qs: qs.query("SELECT * FROM Snap")),
    ("live()", lambda qs: qs.live()),
    ("watch()", lambda qs: qs.watch()),
    ("changes()", lambda qs: qs.changes()),
]
_REFUSED_IDS = [
    "bulk_create-empty",
    "bulk_create",
    "bulk_update",
    "bulk_update-empty",
    "bulk_delete",
    "patch",
    "update_or_create",
    "get_or_create",
    "delete_table",
    "query",
    "live",
    "watch",
    "changes",
]


@pytest.mark.usefixtures("_no_request")
class TestTimeTravelRefusals:
    """AC-4 and AC-22 (a) at the call: T1–T5 and T11 are raised before any client is acquired."""

    def test_as_of_makes_no_request(self) -> None:
        qs = Snap.objects()
        assert qs.as_of(M2021) is qs
        assert qs.as_of(timedelta(hours=1)) is qs
        assert qs.as_of(SurrealFunc("time::now()")) is qs

    def test_a_transaction_cannot_join(self) -> None:
        with pytest.raises(SurrealDbError, match="transaction cannot join") as info:
            Snap.objects(tx=object()).as_of(M2021)
        assert str(info.value) == (
            "as_of() reads the database's history, which a transaction cannot join: run this read on "
            "Model.objects(), outside objects(tx=)."
        )

    @pytest.mark.parametrize(("method", "call"), _REFUSED_METHODS, ids=_REFUSED_IDS)
    async def test_read_only_and_not_streamed(self, method: str, call: Callable[[Any], Any]) -> None:
        with pytest.raises(SurrealDbError, match="read-only") as info:
            result = call(Snap.objects().as_of(M2021))
            if inspect.isawaitable(result):
                await result
        assert str(info.value) == time_travel.queryset_refused(method)
        assert str(info.value).startswith(f"{method} is not available on a queryset read as_of()")

    @pytest.mark.parametrize(
        ("value", "error", "text"),
        [
            (date(2026, 1, 1), TypeError, "as_of() takes"),
            ("yesterday", ValueError, "is not an ISO-8601 date-time"),
            (timedelta(seconds=-1), ValueError, ">= 0"),
        ],
        ids=["T1-date", "T2-not-iso", "T3-negative"],
    )
    def test_unusable_values(self, value: object, error: type[Exception], text: str) -> None:
        qs = Snap.objects()
        with pytest.raises(error) as info:
            qs.as_of(value)  # type: ignore[arg-type]
        assert text in str(info.value)
        assert qs._as_of is None

    @pytest.mark.parametrize(("value", "shown"), _OUT_OF_RANGE, ids=_OUT_OF_RANGE_IDS)
    def test_moment_out_of_range(self, value: object, shown: str) -> None:
        with pytest.raises(ValueError, match=r"^as_of\(\) takes a moment from") as info:
            Snap.objects().as_of(value)  # type: ignore[arg-type]
        assert str(info.value) == _t11(shown)

    @pytest.mark.parametrize("moment", [MIN_MOMENT, MAX_MOMENT], ids=["min", "max"])
    def test_moment_at_the_bounds(self, moment: datetime) -> None:
        qs = Snap.objects()
        assert qs.as_of(moment) is qs
        assert qs._as_of is not None and qs._as_of.moment == moment


# --- E2E on the embedded versioned engine: no server ---------------------------------------------------

#: AC-5's history, one statement per ``client.query()`` (the SDK keeps the first statement's result and drops
#: the others' errors, M12): a and b written in 2020, a rewritten and c created in 2021, b deleted now.
_HISTORY = (
    "CREATE Snap:a SET name='Ada', n=1, g='x' VERSION d'2020-01-01T00:00:00Z';",
    "CREATE Snap:b SET name='Bob', n=2, g='x' VERSION d'2020-01-01T00:00:00Z';",
    "CREATE Snap:a SET name='Ada', n=10, g='x' VERSION d'2021-01-01T00:00:00Z';",
    "CREATE Snap:c SET name='Cy', n=3, g='y' VERSION d'2021-01-01T00:00:00Z';",
    "DELETE Snap:b;",
)
#: ``SnapPlace:p`` at the origin since 2020, for the geo reads (AC-9).
_PLACE = "CREATE SnapPlace:p SET spot = (0.0, 0.0) VERSION d'2020-01-01T00:00:00Z';"

#: ``(id, n)`` of AC-5's records at M2020, at M2021 and now, ordered by ``n``.
_AT_2020 = [("a", 1), ("b", 2)]
_AT_2021 = [("b", 2), ("c", 3), ("a", 10)]
_CURRENT = [("c", 3), ("a", 10)]


def _versioned_url(tmp_path: Path) -> str:
    """An embedded database that keeps history, in the test's own directory."""
    return f"surrealkv+versioned://{tmp_path / 'kv'}"


@contextlib.asynccontextmanager
async def history_db(tmp_path: Path) -> AsyncIterator[Any]:
    """AC-5's history on a fresh embedded ``surrealkv+versioned://`` database; yields its client."""
    async with embedded_database(_versioned_url(tmp_path)) as client:
        for statement in _HISTORY:
            await client.query(statement)
        yield client


async def _now(client: Any) -> datetime:
    """The database's clock, never the host's (spec §9)."""
    moment = await client.query("RETURN time::now();")
    assert isinstance(moment, datetime)
    return moment


def _strictly_increasing(*moments: datetime) -> None:
    """Skip, rather than fail, when the database clock did not move forward between two writes (spec §9)."""
    if any(later <= earlier for earlier, later in itertools.pairwise(moments)):
        pytest.skip("the database clock did not move forward between two writes")


def _record_queries(monkeypatch: pytest.MonkeyPatch, client: Any) -> list[tuple[str, dict[str, Any]]]:
    """Record ``(statement, variables)`` of each ``query()`` *client* runs from now on, then run it as usual.

    Patched on the client's class and keyed on ``id(client)``: an instance attribute wrapping a bound method
    would make the client refer to itself, so dropping it would no longer end its engine (v0.25.0).
    """
    calls: list[tuple[str, dict[str, Any]]] = []
    cls = type(client)
    real = cls.query
    signature = inspect.signature(real)
    owner = id(client)

    async def recording(self: Any, *args: Any, **kwargs: Any) -> Any:
        if id(self) == owner:
            bound = signature.bind(self, *args, **kwargs).arguments
            calls.append((bound["query"], dict(bound.get("vars") or {})))
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(cls, "query", recording)
    return calls


async def _forbidden_client(*args: Any, **kwargs: Any) -> Any:
    pytest.fail("a client was acquired")


def _pairs(rows: Any) -> list[tuple[Any, Any]]:
    """``(id, n)`` of each instance, in order."""
    return [(row.id, row.n) for row in rows]


class TestEmbeddedHistoryE2E:
    """AC-5: history written at explicit past timestamps, read back at each moment on the embedded engine."""

    async def test_table_reads(self, tmp_path: Path) -> None:
        async with history_db(tmp_path):
            assert _pairs(await Snap.objects().as_of(M2020).order_by("n").exec()) == _AT_2020
            assert _pairs(await Snap.objects().as_of(M2021).order_by("n").exec()) == _AT_2021
            assert _pairs(await Snap.objects().order_by("n").exec()) == _CURRENT

    async def test_get_by_id(self, tmp_path: Path) -> None:
        """A record deleted since is still found at the moment; one created later is not."""
        async with history_db(tmp_path):
            bob = await Snap.objects().as_of(M2021).get("b")
            assert (bob.id, bob.name, bob.n) == ("b", "Bob", 2)
            with pytest.raises(SurrealDbNotFoundError):
                await Snap.objects().get("b")
            with pytest.raises(SurrealDbNotFoundError):
                await Snap.objects().as_of(M2020).get("c")

    async def test_counts_and_aggregates(self, tmp_path: Path) -> None:
        async with history_db(tmp_path):
            assert await Snap.objects().as_of(M2020).count() == 2
            assert await Snap.objects().as_of(M2021).count() == 3
            assert await Snap.objects().count() == 2
            assert await Snap.objects().as_of(M2021).filter(n__gt=2).count() == 2
            at_2021 = Snap.objects().as_of(M2021)
            assert await at_2021.sum("n") == 15
            assert await at_2021.avg("n") == 5.0
            assert await at_2021.min("n") == 2
            assert await at_2021.max("n") == 10

    async def test_exists_first_and_slices(self, tmp_path: Path) -> None:
        async with history_db(tmp_path):
            assert await Snap.objects().as_of(M2020).filter(name="Cy").exists() is False
            assert await Snap.objects().as_of(M2021).filter(name="Cy").exists() is True
            top = await Snap.objects().as_of(M2021).order_by("-n").first()
            assert (top.id, top.n) == ("a", 10)
            sliced = await Snap.objects().as_of(M2021).order_by("n").limit(1).offset(1).exec()
            assert _pairs(sliced) == [("c", 3)]

    async def test_grouped_counts(self, tmp_path: Path) -> None:
        async with history_db(tmp_path):
            rows = await Snap.objects().as_of(M2021).values("g").annotate(c=Count()).exec()
            assert {row["g"]: row["c"] for row in rows} == {"x": 2, "y": 1}

    async def test_all_and_get_by_filters(self, tmp_path: Path) -> None:
        async with history_db(tmp_path):
            assert sorted(_pairs(await Snap.objects().as_of(M2020).all())) == _AT_2020
            bob = await Snap.objects().as_of(M2021).filter(name="Bob").get()
            assert (bob.id, bob.n) == ("b", 2)


class TestEmbeddedOrmWritesE2E:
    """AC-6: records written by the ORM, then read at moments taken from the database clock between the writes."""

    async def test_node(self, tmp_path: Path) -> None:
        async with embedded_database(_versioned_url(tmp_path)) as client:
            record = Snap(id="p", n=1)
            await record.save()
            t1 = await _now(client)
            await record.merge(n=5)
            t2 = await _now(client)
            await record.delete()
            t3 = await _now(client)
            _strictly_increasing(t1, t2, t3)
            assert (await Snap.objects().as_of(t1).get("p")).n == 1
            assert (await Snap.objects().as_of(t2).get("p")).n == 5
            with pytest.raises(SurrealDbNotFoundError):
                await Snap.objects().as_of(t3).get("p")
            assert [await Snap.objects().as_of(t).count() for t in (t1, t2, t3)] == [1, 1, 0]

    async def test_edge(self, tmp_path: Path) -> None:
        async with embedded_database(_versioned_url(tmp_path)) as client:
            alice = await SnapAuthor(id="alice").save()
            widget = await SnapAuthor(id="widget").save()
            edge = await SnapBought(in_=alice, out=widget, quantity=1).save()
            t1 = await _now(client)
            await edge.merge(quantity=3)
            t2 = await _now(client)
            _strictly_increasing(t1, t2)
            then = await SnapBought.objects().as_of(t1).filter(in_=alice).exec()
            later = await SnapBought.objects().as_of(t2).filter(in_=alice).exec()
            assert [(bought.id, bought.quantity) for bought in then] == [(edge.id, 1)]
            assert [(bought.id, bought.quantity) for bought in later] == [(edge.id, 3)]


class TestAsOfValueFormsE2E:
    """AC-7: every form ``as_of()`` takes, on AC-5's history; durations computed from the database clock."""

    async def test_absolute_forms_read_the_same_moment(self, tmp_path: Path) -> None:
        forms: list[Any] = [
            "2021-06-01T00:00:00Z",
            datetime(2021, 6, 1),
            datetime(2021, 6, 1, 2, tzinfo=timezone(timedelta(hours=2))),
            "2021-06-01",
        ]
        async with history_db(tmp_path):
            for form in forms:
                assert _pairs(await Snap.objects().as_of(form).order_by("n").exec()) == _AT_2021, form

    async def test_how_long_ago(self, tmp_path: Path) -> None:
        async with history_db(tmp_path) as client:
            assert await Snap.objects().as_of(timedelta(0)).count() == 2
            ago19 = await _now(client) - datetime(2019, 6, 1, tzinfo=UTC)
            assert await Snap.objects().as_of(ago19).count() == 0
            assert await Snap.objects().as_of(Duration.from_timedelta(ago19)).count() == 0
            sdk_ago19 = surrealdb.Duration(Duration.from_timedelta(ago19).elapsed)
            assert await Snap.objects().as_of(sdk_ago19).count() == 0  # type: ignore[arg-type]
            # M2021 plus the milliseconds between the two requests: months away from any write of the history.
            ago21 = await _now(client) - M2021
            assert await Snap.objects().as_of(ago21).count() == 3

    async def test_expressions(self, tmp_path: Path) -> None:
        async with history_db(tmp_path):
            assert await Snap.objects().as_of(SurrealFunc("d'2021-06-01T00:00:00Z'")).count() == 3
            # b was deleted less than an hour ago; a and c were last written in 2021.
            assert await Snap.objects().as_of(SurrealFunc("time::now() - 1h")).count() == 3
            assert await Snap.objects().variables(cutoff=M2020).as_of(SurrealFunc("$cutoff")).count() == 2

    async def test_an_expression_that_gives_no_datetime(self, tmp_path: Path) -> None:
        func = SurrealFunc("'not a date'")
        async with history_db(tmp_path):
            with pytest.raises(SurrealDbError, match="must give a datetime") as info:
                await Snap.objects().as_of(func).count()
        assert str(info.value) == time_travel.not_a_datetime(repr(func), "not a date")

    async def test_an_expression_the_database_cannot_evaluate(self, tmp_path: Path) -> None:
        func = SurrealFunc("time::nope()")
        async with history_db(tmp_path):
            with pytest.raises(SurrealDbError, match="could not be evaluated by the database") as info:
                await Snap.objects().as_of(func).count()
        assert str(info.value).startswith(f"as_of({func!r}) could not be evaluated by the database: ")
        assert info.value.__cause__ is not None

    async def test_a_future_moment_reads_the_current_state(self, tmp_path: Path) -> None:
        """A day after the database's clock — never a literal future date (spec §9)."""
        async with history_db(tmp_path) as client:
            tomorrow = await _now(client) + timedelta(days=1)
            assert _pairs(await Snap.objects().as_of(tomorrow).order_by("n").exec()) == _CURRENT
            assert await Snap.objects().as_of(tomorrow).count() == 2


class TestMomentBoundsE2E:
    """AC-22 (b): the bounds read as SurrealDB addresses them; a resolved moment outside them is never read."""

    async def test_min_moment(self, tmp_path: Path) -> None:
        async with history_db(tmp_path):
            assert await Snap.objects().as_of(MIN_MOMENT).exec() == []
            assert await Snap.objects().as_of(MIN_MOMENT).count() == 0
            with pytest.raises(SurrealDbNotFoundError):
                await Snap.objects().as_of(MIN_MOMENT).get("a")

    async def test_max_moment(self, tmp_path: Path) -> None:
        async with history_db(tmp_path):
            assert _pairs(await Snap.objects().as_of(MAX_MOMENT).order_by("n").exec()) == _CURRENT
            assert await Snap.objects().as_of(MAX_MOMENT).count() == 2

    async def test_a_duration_reaching_before_1970(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        async with history_db(tmp_path) as client:
            ago = await _now(client) - datetime(1969, 1, 1, tzinfo=UTC)
            calls = _record_queries(monkeypatch, client)
            with pytest.raises(SurrealDbError, match="outside the moments SurrealDB versions can address") as info:
                await Snap.objects().as_of(ago).count()
        assert f"as_of({ago!r}) gave 1969-" in str(info.value)
        assert [statement for statement, _ in calls] == ["RETURN time::now() - $_as_of_ago;"]

    @pytest.mark.parametrize(
        ("expression", "resolved"),
        [
            ("d'1900-01-01T00:00:00Z'", datetime(1900, 1, 1, tzinfo=UTC)),
            ("d'2300-01-01T00:00:00Z'", datetime(2300, 1, 1, tzinfo=UTC)),
        ],
        ids=["1900", "2300"],
    )
    async def test_an_expression_outside_the_bounds(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, expression: str, resolved: datetime
    ) -> None:
        func = SurrealFunc(expression)
        async with history_db(tmp_path) as client:
            calls = _record_queries(monkeypatch, client)
            with pytest.raises(SurrealDbError, match="outside the moments SurrealDB versions can address") as info:
                await Snap.objects().as_of(func).exec()
        assert str(info.value) == time_travel.resolved_out_of_range(repr(func), resolved)
        assert [statement for statement, _ in calls] == [f"RETURN {expression};"]

    async def test_an_expression_that_overflows(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        func = SurrealFunc("time::now() - 1000000y")
        async with history_db(tmp_path) as client:
            calls = _record_queries(monkeypatch, client)
            with pytest.raises(SurrealDbError, match="could not be evaluated by the database"):
                await Snap.objects().as_of(func).count()
        statements = [statement for statement, _ in calls]
        assert statements[-1].startswith("RETURN")
        assert not [statement for statement in statements if statement.startswith("SELECT")]


def _refusing_client(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """Serve a server client (not embedded) whose every ``query()`` raises *error*."""

    class _Refusing:
        async def query(self, *args: Any, **kwargs: Any) -> Any:
            raise error

    async def _client(*args: Any, **kwargs: Any) -> Any:
        return _Refusing()

    monkeypatch.setattr(SurrealDBConnectionManager, "get_client", _client)
    monkeypatch.setattr(SurrealDBConnectionManager, "is_embedded", lambda *args: False)


def _no_history_error() -> Exception:
    """The SDK error a SurrealDB 3.x server without versioned storage answers a ``VERSION`` read with (M3)."""
    return _sdk.ServerError(
        "Configuration",
        "There was a problem with the key-value store: The underlying datastore does not support versioned queries",
    )


_T7_READS: list[tuple[str, Callable[[Any], Any]]] = [
    ("exec", lambda qs: qs.exec()),
    ("count", lambda qs: qs.count()),
    ("get-id", lambda qs: qs.get("a")),
    ("all", lambda qs: qs.all()),
]


class TestT7KeyedOnAsOf:
    """T7 is keyed on ``as_of()``, never on the variables: without it the SDK's error is relayed unchanged."""

    async def test_variables_named_as_of_change_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        error = _no_history_error()
        _refusing_client(monkeypatch, error)
        with pytest.raises(_sdk.ServerError) as info:
            await Snap.objects().variables(_as_of=M2021).exec()
        assert info.value is error

    @pytest.mark.parametrize(("name", "read"), _T7_READS, ids=[name for name, _ in _T7_READS])
    async def test_a_time_travel_read_raises_t7(
        self, monkeypatch: pytest.MonkeyPatch, name: str, read: Callable[[Any], Any]
    ) -> None:
        error = _no_history_error()
        _refusing_client(monkeypatch, error)
        with pytest.raises(SurrealDbError, match="keeps history") as info:
            await read(Snap.objects().as_of(M2021))
        assert str(info.value) == time_travel.server_refused(error)
        assert "versioned=true" in str(info.value)
        assert info.value.__cause__ is error

    async def test_another_server_error_is_relayed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        error = _sdk.ServerError("Internal", "boom")
        _refusing_client(monkeypatch, error)
        with pytest.raises(_sdk.ServerError) as info:
            await Snap.objects().as_of(M2021).count()
        assert info.value is error


_NO_HISTORY_URLS: list[Callable[[Path], str]] = [
    lambda _: "mem://",
    lambda _: "memory://",
    lambda tmp: f"surrealkv://{tmp / 'a'}",
    lambda tmp: f"surrealkv://{tmp / 'b'}?versioned=true",
    lambda tmp: f"file://{tmp / 'c'}",
]
_NO_HISTORY_IDS = ["mem", "memory", "surrealkv", "surrealkv-versioned-query", "file"]

_FIVE_READS: list[tuple[str, Callable[[Any], Any]]] = [
    ("exec", lambda qs: qs.exec()),
    ("count", lambda qs: qs.count()),
    ("get-id", lambda qs: qs.get("a")),
    ("all", lambda qs: qs.all()),
    ("exists", lambda qs: qs.exists()),
]


class TestNoHistoryEmbeddedE2E:
    """AC-8: an embedded store that keeps no history is refused by its URL (T6), before any request."""

    @pytest.mark.parametrize("make_url", _NO_HISTORY_URLS, ids=_NO_HISTORY_IDS)
    async def test_refused_before_any_request(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, make_url: Callable[[Path], str]
    ) -> None:
        url = make_url(tmp_path)
        async with embedded_database(url) as client:
            await Snap(id="a").save()
            calls = _record_queries(monkeypatch, client)
            with monkeypatch.context() as patched:
                patched.setattr(SurrealDBConnectionManager, "get_client", _forbidden_client)
                for name, read in _FIVE_READS:
                    with pytest.raises(SurrealDbError, match="keeps none") as info:
                        await read(Snap.objects().as_of(M2021))
                    assert str(info.value) == history_unsupported(url), name
            assert calls == []
            # The same reads without as_of() work.
            assert [row.id for row in await Snap.objects().exec()] == ["a"]
            assert await Snap.objects().count() == 1
            assert (await Snap.objects().get("a")).id == "a"
            assert [row.id for row in await Snap.objects().all()] == ["a"]
            assert await Snap.objects().exists() is True


_EVERY_READ: list[tuple[str, type[BaseSurrealModel], Callable[[Any], Any], Callable[[Any], bool]]] = [
    ("exec", Snap, lambda qs: qs.order_by("n").exec(), lambda r: _pairs(r) == _AT_2021),
    ("first", Snap, lambda qs: qs.order_by("-n").first(), lambda r: (r.id, r.n) == ("a", 10)),
    ("get", Snap, lambda qs: qs.filter(name="Bob").get(), lambda r: (r.id, r.n) == ("b", 2)),
    ("get-id", Snap, lambda qs: qs.get("a"), lambda r: (r.id, r.n) == ("a", 10)),
    ("all", Snap, lambda qs: qs.all(), lambda r: sorted(_pairs(r)) == sorted(_AT_2021)),
    ("exists", Snap, lambda qs: qs.filter(name="Bob").exists(), lambda r: r is True),
    ("count", Snap, lambda qs: qs.count(), lambda r: r == 3),
    ("sum", Snap, lambda qs: qs.sum("n"), lambda r: r == 15),
    ("avg", Snap, lambda qs: qs.avg("n"), lambda r: r == 5.0),
    ("min", Snap, lambda qs: qs.min("n"), lambda r: r == 2),
    ("max", Snap, lambda qs: qs.max("n"), lambda r: r == 10),
    (
        "values-annotate",
        Snap,
        lambda qs: qs.values("g").annotate(c=Count()).exec(),
        lambda r: {row["g"]: row["c"] for row in r} == {"x": 2, "y": 1},
    ),
    (
        "geo-distance",
        SnapPlace,
        lambda qs: qs.annotate(d=GeoDistance("spot", (0, 0))).exec(),
        lambda r: [(place.id, place.d) for place in r] == [("p", 0.0)],
    ),
    ("nearby", SnapPlace, lambda qs: qs.nearby("spot", (0, 0), 1000).exec(), lambda r: [place.id for place in r] == ["p"]),
]


class TestEveryReadPathE2E:
    """AC-9: every read sends one ``… VERSION $_as_of;`` statement, bound to the moment, guarded on a table."""

    @pytest.mark.parametrize(("name", "model", "read", "expected"), _EVERY_READ, ids=[row[0] for row in _EVERY_READ])
    async def test_version_and_guard(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        name: str,
        model: type[BaseSurrealModel],
        read: Callable[[Any], Any],
        expected: Callable[[Any], bool],
    ) -> None:
        async with history_db(tmp_path) as client:
            await client.query(_PLACE)
            calls = _record_queries(monkeypatch, client)
            result = await read(model.objects().as_of(M2021))
            assert expected(result), result
        assert len(calls) == 1, calls  # an absolute moment needs no resolution request
        statement, variables = calls[0]
        assert statement.endswith(" VERSION $_as_of;"), statement
        assert variables["_as_of"] == M2021
        if name == "get-id":
            assert statement == "SELECT * FROM $rid VERSION $_as_of;"
        else:
            assert _G in statement


class TestSnapshotMarkerE2E:
    """AC-11, first sentence: every instance a time-travel read returns carries its moment; current reads none."""

    async def test_time_travel_reads_mark_their_instances(self, tmp_path: Path) -> None:
        async with history_db(tmp_path) as client:
            await client.query(_PLACE)
            at = Snap.objects().as_of
            instances = [
                *(await at(M2021).exec()),
                await at(M2021).order_by("n").first(),
                await at(M2021).filter(name="Ada").get(),
                await at(M2021).get("a"),
                *(await at(M2021).all()),
                *(await SnapPlace.objects().as_of(M2021).annotate(d=GeoDistance("spot", (0, 0))).exec()),
            ]
        assert len(instances) == 10
        assert [_snapshot_moment(instance) for instance in instances] == [M2021] * 10

    async def test_a_resolved_moment_marks_too(self, tmp_path: Path) -> None:
        async with history_db(tmp_path) as client:
            before = await _now(client)
            instances = await Snap.objects().as_of(timedelta(0)).exec()
            after = await _now(client)
        moments = {_snapshot_moment(instance) for instance in instances}
        assert len(instances) == 2 and len(moments) == 1
        (moment,) = moments
        assert isinstance(moment, datetime) and before <= moment <= after

    async def test_current_reads_carry_none(self, tmp_path: Path) -> None:
        async with history_db(tmp_path):
            instances = [
                *(await Snap.objects().exec()),
                await Snap.objects().order_by("n").first(),
                await Snap.objects().filter(name="Ada").get(),
                await Snap.objects().get("a"),
                *(await Snap.objects().all()),
            ]
        assert [_snapshot_moment(instance) for instance in instances] == [None] * 7

    async def test_rows_that_do_not_validate_stay_dicts(self, tmp_path: Path) -> None:
        async with history_db(tmp_path) as client:
            await client.query("CREATE Snap:z SET name='Zed', n='not a number' VERSION d'2020-01-01T00:00:00Z';")
            rows = await Snap.objects().as_of(M2021).order_by("name").exec()
        assert [row["name"] for row in rows] == ["Ada", "Bob", "Cy", "Zed"]
        assert all(isinstance(row, dict) and "_surreal_as_of" not in row for row in rows)


def _fresh_snap() -> BaseSurrealModel:
    return Snap(id="p1", n=1)


def _fresh_mine() -> BaseSurrealModel:
    mine = SnapMine(id="m1", n=1)
    mine._mine = 1
    return mine


#: v0.25.x states: the model, a fresh equal instance, the private dict v0.25.x pickled, the one restored.
_V025_STATES: list[tuple[type[BaseSurrealModel], Callable[[], BaseSurrealModel], dict[str, Any] | None, dict[str, Any]]] = [
    (Snap, _fresh_snap, None, {"_surreal_as_of": None}),
    (SnapMine, _fresh_mine, {"_mine": 1}, {"_mine": 1, "_surreal_as_of": None}),
]


def _by_setstate(model: type[BaseSurrealModel], fresh: BaseSurrealModel, private: dict[str, Any] | None) -> Any:
    """Restore *fresh*'s state with its private dict as v0.25.x pickled it, through ``__setstate__``."""
    state = fresh.__getstate__()
    state["__pydantic_private__"] = None if private is None else dict(private)
    restored = model.__new__(model)
    restored.__setstate__(state)
    return restored


def _by_pickle(model: type[BaseSurrealModel], fresh: BaseSurrealModel, private: dict[str, Any] | None) -> Any:
    """Pickle *fresh* with its private dict emptied as by v0.25.x, then unpickle it."""
    object.__setattr__(fresh, "__pydantic_private__", None if private is None else dict(private))
    return pickle.loads(pickle.dumps(fresh))


_RESTORES = [_by_setstate, _by_pickle]
_RESTORE_IDS = ["setstate", "pickle"]


class TestPicklesFromV025E2E:
    """AC-11 (b), P1: an instance pickled by v0.25.x (no private state) unpickles as a current record that writes."""

    @pytest.mark.parametrize("restore", _RESTORES, ids=_RESTORE_IDS)
    @pytest.mark.parametrize(("model", "fresh", "private", "restored_private"), _V025_STATES, ids=["Snap", "SnapMine"])
    def test_a_current_record(
        self,
        restore: Callable[..., Any],
        model: type[BaseSurrealModel],
        fresh: Callable[[], BaseSurrealModel],
        private: dict[str, Any] | None,
        restored_private: dict[str, Any],
    ) -> None:
        restored = restore(model, fresh(), private)
        assert _snapshot_moment(restored) is None
        assert restored.__pydantic_private__ == restored_private
        assert restored == fresh()

    @pytest.mark.parametrize("restore", _RESTORES, ids=_RESTORE_IDS)
    @pytest.mark.parametrize(("model", "fresh", "private", "restored_private"), _V025_STATES, ids=["Snap", "SnapMine"])
    async def test_its_writes_work(
        self,
        restore: Callable[..., Any],
        model: type[BaseSurrealModel],
        fresh: Callable[[], BaseSurrealModel],
        private: dict[str, Any] | None,
        restored_private: dict[str, Any],
    ) -> None:
        async with embedded_database("mem://"):
            created = restore(model, fresh(), private)
            await created.save()
            assert (await model.objects().get(created.id)).n == 1
            updated = restore(model, fresh(), private)
            updated.n = 3
            await updated.update()
            assert (await model.objects().get(updated.id)).n == 3
            merged = restore(model, fresh(), private)
            await merged.merge(n=2)
            assert (await model.objects().get(merged.id)).n == 2
            deleted = restore(model, fresh(), private)
            await deleted.delete()
            with pytest.raises(SurrealDbNotFoundError):
                await model.objects().get(deleted.id)

    async def test_a_v026_snapshot_keeps_its_moment(self, tmp_path: Path) -> None:
        async with history_db(tmp_path):
            snapshot = await Snap.objects().as_of(M2021).get("a")
        restored = pickle.loads(pickle.dumps(snapshot))
        assert _snapshot_moment(restored) == M2021
        assert restored.model_dump() == snapshot.model_dump()


# --- read-only snapshots (AC-11, D5) ------------------------------------------------------------------

#: Every instance write (spec §5.4), named as T9 names it, called on a snapshot with a transaction or none.
_SNAPSHOT_WRITES: list[tuple[str, str, Callable[[Any, Any], Awaitable[Any]]]] = [
    ("save", "save()", lambda s, tx: s.save(tx=tx)),
    ("update", "update()", lambda s, tx: s.update(tx=tx)),
    ("merge", "merge()", lambda s, tx: s.merge(tx=tx, n=1)),
    ("patch", "patch()", lambda s, tx: s.patch([{"op": "replace", "path": "/n", "value": 1}], tx=tx)),
    ("upsert", "upsert()", lambda s, tx: s.upsert(tx=tx)),
    ("delete", "delete()", lambda s, tx: s.delete(tx=tx)),
    ("atomic_append", "atomic_append()", lambda s, tx: s.atomic_append("tags", "x", tx=tx)),
    ("atomic_remove", "atomic_remove()", lambda s, tx: s.atomic_remove("tags", "x", tx=tx)),
    ("atomic_set_add", "atomic_set_add()", lambda s, tx: s.atomic_set_add("tags", "x", tx=tx)),
    ("atomic_increment", "atomic_increment()", lambda s, tx: s.atomic_increment("n", tx=tx)),
    ("atomic_append_many", "atomic_append_many()", lambda s, tx: s.atomic_append_many("tags", ["x"], tx=tx)),
    ("atomic_set_add_many", "atomic_set_add_many()", lambda s, tx: s.atomic_set_add_many("tags", ["x"], tx=tx)),
    ("atomic_remove_many", "atomic_remove_many()", lambda s, tx: s.atomic_remove_many("tags", ["x"], tx=tx)),
    ("relate", "relate()", lambda s, tx: s.relate("likes", SnapAuthor(id="z"), tx=tx)),
    ("relate-typed", "relate()", lambda s, tx: s.relate(SnapBought, SnapAuthor(id="z"), tx=tx)),
    ("remove_relation", "remove_relation()", lambda s, tx: s.remove_relation("likes", SnapAuthor(id="z"), tx=tx)),
    ("remove_all_relations", "remove_all_relations()", lambda s, tx: s.remove_all_relations("likes", tx=tx)),
]

#: The SDK's write methods: a snapshot write must reach none of them.
_CLIENT_WRITES = ("create", "insert", "insert_relation", "update", "merge", "patch", "upsert", "delete")


def _record_writes(monkeypatch: pytest.MonkeyPatch, client: Any) -> list[str]:
    """Record the name of each SDK write method *client* calls from now on, then run it as usual.

    Patched on the client's class and keyed on ``id(client)``, as ``_record_queries()`` does.
    """
    calls: list[str] = []
    cls = type(client)
    owner = id(client)

    def recording(name: str, real: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        async def method(self: Any, *args: Any, **kwargs: Any) -> Any:
            if id(self) == owner:
                calls.append(name)
            return await real(self, *args, **kwargs)

        return method

    for name in _CLIENT_WRITES:
        monkeypatch.setattr(cls, name, recording(name, getattr(cls, name)))
    return calls


@contextlib.contextmanager
def _pre_signals(model: type[BaseSurrealModel]) -> Iterator[list[str]]:
    """Count the ``pre_save``/``pre_update``/``pre_delete`` calls for *model*; the handlers leave on exit."""
    fired: list[str] = []

    async def on_save(sender: type, **kwargs: Any) -> None:
        fired.append("pre_save")

    async def on_update(sender: type, **kwargs: Any) -> None:
        fired.append("pre_update")

    async def on_delete(sender: type, **kwargs: Any) -> None:
        fired.append("pre_delete")

    pre_save.connect(model)(on_save)
    pre_update.connect(model)(on_update)
    pre_delete.connect(model)(on_delete)
    try:
        yield fired
    finally:
        pre_save.disconnect(on_save, model)
        pre_update.disconnect(on_update, model)
        pre_delete.disconnect(on_delete, model)


class _UntouchableTx:
    """A transaction a snapshot write must never reach: any use of it fails the test."""

    def __getattr__(self, name: str) -> Any:
        pytest.fail(f"the transaction was used ({name})")


class _ReadingTx:
    """An interactive transaction whose reads answer *rows*: ``refresh(tx=)``'s branch, without a 3.x server."""

    is_interactive = True

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def run_read(self, statement: str, variables: dict[str, Any]) -> list[dict[str, Any]]:
        return self.rows


#: ``SnapUser:u`` created in 2020 with an old e-mail, changed now (one statement per ``client.query()``).
_USER_HISTORY = (
    "CREATE SnapUser:u SET email='old@x', password='p' VERSION d'2020-01-01T00:00:00Z';",
    "UPDATE SnapUser:u SET email='new@x';",
)


class TestSnapshotWritesE2E:
    """AC-11: an instance read with ``as_of()`` is a read-only snapshot until ``refresh()``; a past state is restored
    through a new instance.

    Every write raises T9 before any signal, transaction or request. Every marker assertion reads it with
    ``_snapshot_moment()`` (P1).
    """

    @pytest.mark.parametrize("in_tx", [False, True], ids=["no-tx", "tx"])
    @pytest.mark.parametrize(("name", "method", "write"), _SNAPSHOT_WRITES, ids=[row[0] for row in _SNAPSHOT_WRITES])
    async def test_every_write_is_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        in_tx: bool,
        name: str,
        method: str,
        write: Callable[[Any, Any], Awaitable[Any]],
    ) -> None:
        async with history_db(tmp_path) as client:
            snapshot = await Snap.objects().as_of(M2021).get("a")
            calls = _record_queries(monkeypatch, client)
            writes = _record_writes(monkeypatch, client)
            refused = pytest.raises(SurrealDbError, match=r"cannot run on an instance read with as_of\(\)")
            with _pre_signals(Snap) as fired, refused as info:
                await write(snapshot, _UntouchableTx() if in_tx else None)
            assert str(info.value) == time_travel.snapshot_write_refused("Snap", method, M2021)
            assert fired == []
            assert calls == []
            assert writes == []
            assert _snapshot_moment(snapshot) == M2021
            current = await Snap.objects().get("a")
        assert (current.n, current.tags) == (10, [])

    async def test_bulk_create_refuses_a_snapshot(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        async with history_db(tmp_path) as client:
            snapshot = await Snap.objects().as_of(M2021).get("a")
            calls = _record_queries(monkeypatch, client)
            writes = _record_writes(monkeypatch, client)
            with pytest.raises(SurrealDbError, match=r"read with as_of\(\)") as info:
                await Snap.objects().bulk_create([Snap(id="new", n=5), snapshot])
            assert str(info.value) == time_travel.snapshot_write_refused("Snap", "bulk_create()", M2021)
            assert calls == []
            assert writes == []
            with pytest.raises(SurrealDbNotFoundError):
                await Snap.objects().get("new")

    async def test_a_copy_is_a_snapshot(self, tmp_path: Path) -> None:
        async with history_db(tmp_path):
            snapshot = await Snap.objects().as_of(M2021).get("a")
            copy = snapshot.model_copy()
            assert _snapshot_moment(copy) == M2021
            with pytest.raises(SurrealDbError) as info:
                await copy.save()
            assert str(info.value) == time_travel.snapshot_write_refused("Snap", "save()", M2021)

    async def test_a_pickled_snapshot_stays_a_snapshot(self, tmp_path: Path) -> None:
        """AC-11 (b)'s last sentence: unlike a v0.25.x pickle, a v0.26.0 snapshot unpickles as a snapshot."""
        async with history_db(tmp_path):
            snapshot = await Snap.objects().as_of(M2021).get("a")
            restored = pickle.loads(pickle.dumps(snapshot))
            assert _snapshot_moment(restored) == M2021
            with pytest.raises(SurrealDbError) as info:
                await restored.save()
            assert str(info.value) == time_travel.snapshot_write_refused("Snap", "save()", M2021)

    async def test_refresh_turns_it_into_the_current_record(self, tmp_path: Path) -> None:
        async with history_db(tmp_path):
            snapshot = await Snap.objects().as_of(M2020).get("a")
            assert (snapshot.n, _snapshot_moment(snapshot)) == (1, M2020)
            await snapshot.refresh()
            assert (snapshot.n, _snapshot_moment(snapshot)) == (10, None)
            await snapshot.merge(n=7)
            assert (await Snap.objects().get("a")).n == 7

    async def test_refresh_of_a_record_deleted_since(self, tmp_path: Path) -> None:
        """The existing error, and the instance stays a snapshot."""
        async with history_db(tmp_path):
            snapshot = await Snap.objects().as_of(M2021).get("b")
            with pytest.raises(SurrealDbError, match="Can't refresh data, no record found."):
                await snapshot.refresh()
        assert _snapshot_moment(snapshot) == M2021

    async def test_refresh_in_a_transaction(self, tmp_path: Path) -> None:
        """``refresh(tx=)`` clears the marker too, and keeps it when the transaction finds no record."""
        async with history_db(tmp_path):
            snapshot = await Snap.objects().as_of(M2020).get("a")
            gone = await Snap.objects().as_of(M2021).get("b")
        row = {"id": RecordID("Snap", "a"), "name": "Ada", "n": 10, "g": "x"}
        await snapshot.refresh(tx=_ReadingTx([row]))
        assert (snapshot.n, _snapshot_moment(snapshot)) == (10, None)
        with pytest.raises(SurrealDbError, match="Can't refresh data, no record found."):
            await gone.refresh(tx=_ReadingTx([]))
        assert _snapshot_moment(gone) == M2021

    async def test_restore_a_live_record_with_update(self, tmp_path: Path) -> None:
        async with history_db(tmp_path):
            old = await Snap.objects().as_of(M2020).get("a")
            await Snap(**old.model_dump()).update()
            assert (await Snap.objects().get("a")).n == 1

    async def test_undelete_with_save(self, tmp_path: Path) -> None:
        async with history_db(tmp_path):
            old_b = await Snap.objects().as_of(M2021).get("b")
            await Snap(**old_b.model_dump()).save()
            current = await Snap.objects().get("b")
        assert (current.name, current.n, _snapshot_moment(current)) == ("Bob", 2, None)

    async def test_upsert_restores_both(self, tmp_path: Path) -> None:
        """``upsert()`` restores a past state whether the record still exists (a) or was deleted since (b)."""
        async with history_db(tmp_path):
            old_a = await Snap.objects().as_of(M2020).get("a")
            old_b = await Snap.objects().as_of(M2021).get("b")
            await Snap(**old_a.model_dump()).upsert()
            await Snap(**old_b.model_dump()).upsert()
            assert _pairs(await Snap.objects().order_by("n").exec()) == [("a", 1), ("b", 2), ("c", 3)]

    async def test_a_snapshot_is_never_equal_to_the_current_instance(self, tmp_path: Path) -> None:
        """a's last write is 2021-01-01: same fields at M2021 and now, yet ``==`` compares the marker too."""
        async with history_db(tmp_path):
            snapshot_a = await Snap.objects().as_of(M2021).get("a")
            current_a = await Snap.objects().get("a")
        assert snapshot_a != current_a
        assert snapshot_a.model_dump() == current_a.model_dump()

    async def test_an_auth_model_refreshes_through_the_base_class(self, tmp_path: Path) -> None:
        """On an ``AuthenticatedUserMixin`` model ``refresh`` is the mixin's token exchange: the base class's one."""
        async with history_db(tmp_path) as client:
            for statement in _USER_HISTORY:
                await client.query(statement)
            snap = await SnapUser.objects().as_of(M2021).get("u")
            assert (snap.email, _snapshot_moment(snap)) == ("old@x", M2021)
            with pytest.raises(SurrealDbError) as info:
                await snap.save()
            assert str(info.value) == time_travel.snapshot_write_refused("SnapUser", "save()", M2021)
            await BaseSurrealModel.refresh(snap)
            assert (snap.email, _snapshot_moment(snap)) == ("new@x", None)

    async def test_an_edge_snapshot(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        async with embedded_database(_versioned_url(tmp_path)) as client:
            alice = await SnapAuthor(id="alice").save()
            widget = await SnapAuthor(id="widget").save()
            await SnapBought(in_=alice, out=widget, quantity=1).save()
            (snapshot,) = await SnapBought.objects().as_of(MAX_MOMENT).filter(in_=alice).exec()
            assert _snapshot_moment(snapshot) == MAX_MOMENT
            calls = _record_queries(monkeypatch, client)
            writes = _record_writes(monkeypatch, client)
            for method, write in [
                ("save()", snapshot.save),
                ("merge()", lambda: snapshot.merge(quantity=3)),
                ("delete()", snapshot.delete),
            ]:
                with pytest.raises(SurrealDbError) as info:
                    await write()
                assert str(info.value) == time_travel.snapshot_write_refused("SnapBought", method, MAX_MOMENT)
            with pytest.raises(SurrealDbError) as info:
                await SnapBought.objects().bulk_create([snapshot])
            assert str(info.value) == time_travel.snapshot_write_refused("SnapBought", "bulk_create()", MAX_MOMENT)
            assert calls == []
            assert writes == []
            assert [bought.quantity for bought in await SnapBought.objects().exec()] == [1]


# --- fetch() under as_of(), dispatched by line (AC-10, D6) ---------------------------------------------

#: ``SnapAuthor:1`` written in 2020, linked from ``Snap:a`` now (one statement per ``client.query()``).
_AUTHOR = (
    "CREATE SnapAuthor:1 SET name='old' VERSION d'2020-01-01T00:00:00Z';",
    "UPDATE Snap:a SET author = SnapAuthor:1;",
)


@contextlib.asynccontextmanager
async def fetch_history_db(tmp_path: Path) -> AsyncIterator[Any]:
    """AC-5's history, ``SnapPlace:p`` and AC-10's author link on a fresh embedded versioned database."""
    async with history_db(tmp_path) as client:
        for statement in (_PLACE, *_AUTHOR):
            await client.query(statement)
        yield client


def _count_versions(monkeypatch: pytest.MonkeyPatch, client: Any, answer: str | None = None) -> list[str]:
    """Record each ``version()`` request *client* makes from now on; answer *answer* instead of it when given.

    Patched on the client's class and keyed on ``id(client)``, as ``_record_queries()`` does. Each entry is the
    answer the caller received.
    """
    calls: list[str] = []
    cls = type(client)
    real = cls.version
    owner = id(client)

    async def counting(self: Any, *args: Any, **kwargs: Any) -> Any:
        if id(self) != owner:
            return await real(self, *args, **kwargs)
        version = answer if answer is not None else await real(self, *args, **kwargs)
        calls.append(version)
        return version

    monkeypatch.setattr(cls, "version", counting)
    return calls


#: The reads that return the records ``fetch()`` resolves (spec §4): ``(id, model, fetched field, read, rows
#: expected when the line is 3.x)``.
_FETCHING_READS: list[tuple[str, type[BaseSurrealModel], str, Callable[[Any], Any], Callable[[Any], bool]]] = [
    ("exec", Snap, "author", lambda qs: qs.order_by("n").exec(), lambda r: _pairs(r) == _AT_2021),
    ("first", Snap, "author", lambda qs: qs.order_by("-n").first(), lambda r: (r.id, r.n) == ("a", 10)),
    ("get", Snap, "author", lambda qs: qs.filter(name="Ada").get(), lambda r: (r.id, r.n) == ("a", 10)),
    (
        "geo-distance",
        SnapPlace,
        "spot",
        lambda qs: qs.annotate(d=GeoDistance("spot", (0, 0))).exec(),
        lambda r: [(place.id, place.d) for place in r] == [("p", 0.0)],
    ),
]
_FETCHING_IDS = [row[0] for row in _FETCHING_READS]

#: The reads that ignore ``fetch()`` (spec §4): no T8 and no ``version()`` request, on any line.
_NON_FETCHING_READS: list[tuple[str, Callable[[Any], Any], Callable[[Any], bool]]] = [
    ("count", lambda qs: qs.count(), lambda r: r == 3),
    ("exists", lambda qs: qs.exists(), lambda r: r is True),
    ("sum", lambda qs: qs.sum("n"), lambda r: r == 15),
    ("avg", lambda qs: qs.avg("n"), lambda r: r == 5.0),
    ("min", lambda qs: qs.min("n"), lambda r: r == 2),
    ("max", lambda qs: qs.max("n"), lambda r: r == 10),
    (
        "values-annotate",
        lambda qs: qs.values("g").annotate(c=Count()).exec(),
        lambda r: {row["g"]: row["c"] for row in r} == {"x": 2, "y": 1},
    ),
    ("get-id", lambda qs: qs.get("a"), lambda r: (r.id, r.n) == ("a", 10) and _snapshot_moment(r) == M2021),
    (
        "all",
        lambda qs: qs.all(),
        lambda r: sorted(_pairs(r)) == sorted(_AT_2021) and {_snapshot_moment(x) for x in r} == {M2021},
    ),
]


class TestFetchByLineE2E:
    """AC-10: ``fetch()`` under ``as_of()`` is refused on SurrealDB 2.x (T8), only by the reads returning fetched records.

    The embedded engine of the surrealdb SDK 2.x answers ``version()`` with a 2.x version (its core is 2.3):
    T8. Patched to answer ``"surrealdb-3.3.4"``, the same reads run. The line is asked for only when a read
    returns the fetched records and ``fetch()`` is set.
    """

    @pytest.mark.parametrize(("name", "model", "field", "read", "expected"), _FETCHING_READS, ids=_FETCHING_IDS)
    async def test_refused_on_2x(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        name: str,
        model: type[BaseSurrealModel],
        field: str,
        read: Callable[[Any], Any],
        expected: Callable[[Any], bool],
    ) -> None:
        async with fetch_history_db(tmp_path) as client:
            versions = _count_versions(monkeypatch, client)
            calls = _record_queries(monkeypatch, client)
            with pytest.raises(SurrealDbError, match=r"fetch\(\) under as_of\(\) needs SurrealDB 3\.x") as info:
                await read(model.objects().as_of(M2021).fetch(field))
        assert str(info.value) == time_travel.fetch_refused()
        assert len(versions) == 1 and line_major(versions[0]) == 2, versions
        assert calls == []  # refused after the one version() request: the read is never sent

    @pytest.mark.parametrize(("name", "model", "field", "read", "expected"), _FETCHING_READS, ids=_FETCHING_IDS)
    async def test_runs_on_3x(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        name: str,
        model: type[BaseSurrealModel],
        field: str,
        read: Callable[[Any], Any],
        expected: Callable[[Any], bool],
    ) -> None:
        async with fetch_history_db(tmp_path) as client:
            versions = _count_versions(monkeypatch, client, answer="surrealdb-3.3.4")
            calls = _record_queries(monkeypatch, client)
            result = await read(model.objects().as_of(M2021).fetch(field))
        assert expected(result), result
        assert versions == ["surrealdb-3.3.4"]
        assert len(calls) == 1, calls
        statement, variables = calls[0]
        assert statement.endswith(f" FETCH {field} VERSION $_as_of;"), statement
        assert variables["_as_of"] == M2021

    @pytest.mark.parametrize(("name", "read", "expected"), _NON_FETCHING_READS, ids=[row[0] for row in _NON_FETCHING_READS])
    async def test_reads_ignoring_fetch_ask_no_line(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        name: str,
        read: Callable[[Any], Any],
        expected: Callable[[Any], bool],
    ) -> None:
        async with fetch_history_db(tmp_path) as client:
            versions = _count_versions(monkeypatch, client)
            result = await read(Snap.objects().as_of(M2021).fetch("author"))
        assert expected(result), result
        assert versions == []

    @pytest.mark.parametrize(("name", "model", "field", "read", "expected"), _FETCHING_READS, ids=_FETCHING_IDS)
    async def test_as_of_without_fetch_asks_no_line(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        name: str,
        model: type[BaseSurrealModel],
        field: str,
        read: Callable[[Any], Any],
        expected: Callable[[Any], bool],
    ) -> None:
        async with fetch_history_db(tmp_path) as client:
            versions = _count_versions(monkeypatch, client)
            result = await read(model.objects().as_of(M2021))
        assert expected(result), result
        assert versions == []

    async def test_fetch_without_as_of_unchanged(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A current read resolves the link as before, with no ``version()`` request."""
        async with fetch_history_db(tmp_path) as client:
            versions = _count_versions(monkeypatch, client)
            calls = _record_queries(monkeypatch, client)
            rows = await Snap.objects().order_by("n").fetch("author").exec()
        assert versions == []
        assert calls == [("SELECT * FROM Snap ORDER BY n ASC FETCH author;", {})]
        # The fetched author is a record, not a RecordID: the rows do not validate and come back as dicts.
        assert [(row["id"].id, row.get("author")) for row in rows] == [
            ("c", None),
            ("a", {"id": RecordID("SnapAuthor", 1), "name": "old"}),
        ]

    async def test_refused_before_the_resolution(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Spec §5.5 order: T8 comes before a duration is resolved, so no ``RETURN`` request is sent."""
        async with fetch_history_db(tmp_path) as client:
            versions = _count_versions(monkeypatch, client)
            calls = _record_queries(monkeypatch, client)
            with pytest.raises(SurrealDbError, match=r"fetch\(\) under as_of\(\)"):
                await Snap.objects().as_of(timedelta(0)).fetch("author").exec()
        assert len(versions) == 1
        assert calls == []

    async def test_no_history_comes_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Spec §5.5 order: T6 (no request) before T8: a store without history never asks the line."""
        async with embedded_database("mem://") as client:
            versions = _count_versions(monkeypatch, client)
            with pytest.raises(SurrealDbError, match="keeps none"):
                await Snap.objects().as_of(M2021).fetch("author").exec()
        assert versions == []


# --- server lines: in memory (CI as today) and on versioned storage (AC-12 to AC-15, AC-22 (c) and (d)) ----


def _server_host() -> str:
    return os.environ.get("SURREALDB_HOST", "localhost")


def _http_url() -> str:
    """The in-memory server of ``SURREALDB_PORT`` over HTTP (``_ws_url()`` is the same server over WebSocket)."""
    return f"http://{_server_host()}:{os.environ.get('SURREALDB_PORT', '8000')}"


def _versioned_host() -> str:
    return os.environ.get("SURREALDB_VERSIONED_HOST") or _server_host()


def _versioned_ws_url() -> str | None:
    """A server started with versioned storage (``SURREALDB_VERSIONED_PORT``); ``None`` when there is none."""
    port = os.environ.get("SURREALDB_VERSIONED_PORT")
    return f"ws://{_versioned_host()}:{port}/rpc" if port else None


def _versioned_http_url() -> str | None:
    """The same versioned server over HTTP; ``None`` when there is none."""
    port = os.environ.get("SURREALDB_VERSIONED_PORT")
    return f"http://{_versioned_host()}:{port}" if port else None


async def _line(client: Any) -> int:
    """The server's major version (``changes()``'s rule)."""
    return line_major(await client.version())


@contextlib.asynccontextmanager
async def connected(url: str, database: str, *tables: str) -> AsyncIterator[Any]:
    """The ORM connected to *url* (``ns``/*database*), *tables* removed before and after.

    Ends with ``unset_connection()``, never ``close_connection()`` alone: the process-wide settings must not
    stay on this server or database for the next test, in any order (spec §9).
    """
    SurrealDBConnectionManager.set_connection(url=url, user="root", password="root", namespace="ns", database=database)
    try:
        client = await SurrealDBConnectionManager.get_client()
        for table in tables:
            await client.query(f"REMOVE TABLE IF EXISTS {table};")
        try:
            yield client
        finally:
            for table in tables:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(client.query(f"REMOVE TABLE IF EXISTS {table};"), 10)
    finally:
        await SurrealDBConnectionManager.unset_connection()


async def _assert_refused_by_the_server(read: Awaitable[Any]) -> None:
    """T7: the server's refusal of a ``VERSION`` read as an ORM error, chained to the SDK's ``ServerError``."""
    with pytest.raises(SurrealDbError, match="keeps history") as info:
        await read
    cause = info.value.__cause__
    assert isinstance(cause, _sdk.ServerError), repr(cause)
    assert time_travel.UNSUPPORTED_MARKER in str(cause)
    assert str(info.value) == time_travel.server_refused(cause)


#: A record saved now, then the three reads AC-12 checks (each resolves ``timedelta(0)`` on the database first).
_REFUSED_BY_3X: list[tuple[str, Callable[[Any], Awaitable[Any]]]] = [
    ("exec", lambda qs: qs.exec()),
    ("count", lambda qs: qs.count()),
    ("get-id", lambda qs: qs.get("a")),
]


class TestInMemoryServerE2E:
    """AC-12, AC-13, AC-22 (c): the server of ``SURREALDB_PORT``, started in memory (no history), on each line."""

    async def test_3x_refuses(self) -> None:
        """AC-12: SurrealDB 3.x refuses ``VERSION`` without versioned storage: T7, over WebSocket and HTTP."""
        async with orm_client("Snap") as client:
            if await _line(client) < 3:
                pytest.skip("SurrealDB 3.x only: a 2.x server answers without an error (AC-13)")
            await Snap(id="a", n=1).save()
            for _, read in _REFUSED_BY_3X:
                await _assert_refused_by_the_server(read(Snap.objects().as_of(timedelta(0))))
        async with connected(_http_url(), "db", "Snap"):
            await Snap(id="a", n=1).save()
            for _, read in _REFUSED_BY_3X:
                await _assert_refused_by_the_server(read(Snap.objects().as_of(timedelta(0))))

    async def test_2x_answers_without_history(self) -> None:
        """AC-13 (documented, undetectable): a record last written after the moment is missing, no error."""
        async with orm_client("Snap") as client:
            if await _line(client) >= 3:
                pytest.skip("SurrealDB 2.x only: a 3.x server refuses (AC-12)")
            a = await Snap(id="a", n=1).save()
            await Snap(id="b", n=2).save()
            t1 = await _now(client)
            await a.merge(n=9)
            t2 = await _now(client)
            _strictly_increasing(t1, t2)
            assert _pairs(await Snap.objects().as_of(t1).order_by("n").exec()) == [("b", 2)]
            # At or after a record's last write, it is there in its current state.
            assert _pairs(await Snap.objects().as_of(t2).order_by("n").exec()) == [("b", 2), ("a", 9)]

    @pytest.mark.parametrize(
        ("expression", "resolved"),
        [
            ("d'1900-01-01T00:00:00Z'", datetime(1900, 1, 1, tzinfo=UTC)),
            ("d'2300-01-01T00:00:00Z'", datetime(2300, 1, 1, tzinfo=UTC)),
        ],
        ids=["1900", "2300"],
    )
    async def test_resolved_moment_out_of_range(
        self, monkeypatch: pytest.MonkeyPatch, expression: str, resolved: datetime
    ) -> None:
        """AC-22 (c), both lines: T10c after the one ``RETURN``; the read is never sent."""
        func = SurrealFunc(expression)
        async with orm_client("Snap") as client:
            await Snap(id="a", n=1).save()
            calls = _record_queries(monkeypatch, client)
            with pytest.raises(SurrealDbError, match="outside the moments SurrealDB versions can address") as info:
                await Snap.objects().as_of(func).exec()
            sent = [statement for statement, _ in calls]  # before the cleanup's own statements
        assert str(info.value) == time_travel.resolved_out_of_range(repr(func), resolved)
        assert sent == [f"RETURN {expression};"]

    async def test_min_moment_2x_no_current_row(self) -> None:
        """AC-22 (c), 2.x: 1970-01-01 is addressed as such — no current row (one microsecond earlier would be)."""
        async with orm_client("Snap") as client:
            if await _line(client) >= 3:
                pytest.skip("SurrealDB 2.x only")
            await Snap(id="a", n=1).save()
            assert await Snap.objects().as_of(MIN_MOMENT).exec() == []

    async def test_min_moment_3x_relayed(self) -> None:
        """AC-22 (c), 3.x: the in-memory server's refusal is T7, an ORM error — never its raw ``InternalError``."""
        async with orm_client("Snap") as client:
            if await _line(client) < 3:
                pytest.skip("SurrealDB 3.x only")
            await Snap(id="a", n=1).save()
            await _assert_refused_by_the_server(Snap.objects().as_of(MIN_MOMENT).count())


#: The tables the versioned-server tests write; removed before and after each test.
_VERSIONED_TABLES = ("Snap", "SnapAuthor", "snap_bought")


@contextlib.asynccontextmanager
async def versioned_client(transport: str = "ws") -> AsyncIterator[Any]:
    """The ORM on the versioned server, database ``tt``, over *transport* (``"ws"`` or ``"http"``)."""
    url = _versioned_ws_url() if transport == "ws" else _versioned_http_url()
    assert url is not None
    async with connected(url, "tt", *_VERSIONED_TABLES) as client:
        yield client


_TRANSPORTS = ["ws", "http"]


@pytest.mark.skipif(_versioned_ws_url() is None, reason="no versioned SurrealDB server (SURREALDB_VERSIONED_PORT)")
class TestVersionedServerE2E:
    """AC-14, AC-15, AC-22 (d): a server started with versioned storage (``SURREALDB_VERSIONED_PORT``), each line.

    These reads are where the servers' own answers are wrong without the ORM's per-record check (spec M5: 3.x
    table reads return the records deleted before the moment; M6: 2.x miscounts an unfiltered ``count()``).
    Moments come from the database clock between ORM writes.
    """

    @pytest.mark.parametrize("transport", _TRANSPORTS)
    async def test_orm_writes_node(self, transport: str) -> None:
        """AC-6's flow on a server."""
        async with versioned_client(transport) as client:
            record = await Snap(id="p", n=1).save()
            t1 = await _now(client)
            await record.merge(n=5)
            t2 = await _now(client)
            await record.delete()
            t3 = await _now(client)
            _strictly_increasing(t1, t2, t3)
            assert (await Snap.objects().as_of(t1).get("p")).n == 1
            assert (await Snap.objects().as_of(t2).get("p")).n == 5
            with pytest.raises(SurrealDbNotFoundError):
                await Snap.objects().as_of(t3).get("p")
            assert [await Snap.objects().as_of(t).count() for t in (t1, t2, t3)] == [1, 1, 0]

    async def test_orm_writes_edge(self) -> None:
        """AC-6's edge flow on a server."""
        async with versioned_client() as client:
            alice = await SnapAuthor(id="alice").save()
            widget = await SnapAuthor(id="widget").save()
            edge = await SnapBought(in_=alice, out=widget, quantity=1).save()
            t1 = await _now(client)
            await edge.merge(quantity=3)
            t2 = await _now(client)
            _strictly_increasing(t1, t2)
            then = await SnapBought.objects().as_of(t1).filter(in_=alice).exec()
            later = await SnapBought.objects().as_of(t2).filter(in_=alice).exec()
        assert [(bought.id, bought.quantity) for bought in then] == [(edge.id, 1)]
        assert [(bought.id, bought.quantity) for bought in later] == [(edge.id, 3)]

    @pytest.mark.parametrize("transport", _TRANSPORTS)
    async def test_deleted_and_updated_since(self, transport: str) -> None:
        """AC-14: rows, counts and aggregates right at each moment, a record deleted since included."""
        async with versioned_client(transport) as client:
            a = await Snap(id="a", name="Ada", n=1, g="x").save()
            await Snap(id="b", name="Bob", n=2, g="x").save()
            await Snap(id="c", name="Cy", n=3, g="y").save()
            t1 = await _now(client)
            await Snap(id="b").delete()
            await a.merge(n=10)
            t2 = await _now(client)
            _strictly_increasing(t1, t2)

            assert _pairs(await Snap.objects().as_of(t2).order_by("n").exec()) == [("c", 3), ("a", 10)]
            assert await Snap.objects().as_of(t2).count() == 2
            assert await Snap.objects().as_of(t2).filter(n__gt=0).count() == 2
            assert await Snap.objects().as_of(t2).sum("n") == 13
            groups = await Snap.objects().as_of(t2).values("g").annotate(c=Count()).exec()
            assert {row["g"]: row["c"] for row in groups} == {"x": 1, "y": 1}
            with pytest.raises(SurrealDbNotFoundError):
                await Snap.objects().as_of(t2).get("b")

            assert _pairs(await Snap.objects().as_of(t1).order_by("n").exec()) == [("a", 1), ("b", 2), ("c", 3)]
            assert await Snap.objects().as_of(t1).count() == 3
            assert await Snap.objects().as_of(t1).filter(n__gt=1).count() == 2
            assert await Snap.objects().as_of(t1).sum("n") == 6
            groups = await Snap.objects().as_of(t1).values("g").annotate(c=Count()).exec()
            assert {row["g"]: row["c"] for row in groups} == {"x": 2, "y": 1}
            bob = await Snap.objects().as_of(t1).get("b")
            assert (bob.name, bob.n) == ("Bob", 2)

            assert await Snap.objects().as_of(timedelta(0)).count() == 2
            assert await Snap.objects().as_of(SurrealFunc("time::now()")).count() == 2

    @pytest.mark.parametrize("transport", _TRANSPORTS)
    async def test_moment_bounds(self, monkeypatch: pytest.MonkeyPatch, transport: str) -> None:
        """AC-22 (d): both bounds read as SurrealDB addresses them; a duration reaching 1969 is never read."""
        async with versioned_client(transport) as client:
            await Snap(id="a", n=1).save()
            await Snap(id="b", n=2).save()
            assert await Snap.objects().as_of(MIN_MOMENT).count() == 0
            assert await Snap.objects().as_of(MAX_MOMENT).count() == 2
            ago = await _now(client) - datetime(1969, 1, 1, tzinfo=UTC)
            calls = _record_queries(monkeypatch, client)
            with pytest.raises(SurrealDbError, match="outside the moments SurrealDB versions can address") as info:
                await Snap.objects().as_of(ago).count()
            sent = [statement for statement, _ in calls]  # before the cleanup's own statements
        assert f"as_of({ago!r}) gave 1969-" in str(info.value)
        assert sent == ["RETURN time::now() - $_as_of_ago;"]

    async def test_fetch_as_it_was_on_3x(self) -> None:
        """AC-15, 3.x: ``fetch()`` resolves the link as it was at the moment."""
        async with versioned_client() as client:
            if await _line(client) < 3:
                pytest.skip("SurrealDB 3.x only: 2.x refuses fetch() under as_of() (T8)")
            author = await SnapAuthor(id="w", name="old").save()
            await Snap(id="p", n=1, author=RecordID("SnapAuthor", "w")).save()
            t1 = await _now(client)
            await author.merge(name="new")
            t2 = await _now(client)
            _strictly_increasing(t1, t2)
            then = await Snap.objects().as_of(t1).fetch("author").exec()
            later = await Snap.objects().as_of(t2).fetch("author").exec()
            current = await Snap.objects().fetch("author").exec()
        # The fetched author is a record, not a RecordID: the rows do not validate and come back as dicts.
        assert [row["author"]["name"] for row in then] == ["old"]
        assert [row["author"]["name"] for row in later] == ["new"]
        assert [row["author"]["name"] for row in current] == ["new"]

    async def test_fetch_refused_on_2x(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AC-15, 2.x: T8 after the one ``version()`` request; the read is never sent."""
        async with versioned_client() as client:
            if await _line(client) >= 3:
                pytest.skip("SurrealDB 2.x only: 3.x honours fetch() under as_of()")
            await SnapAuthor(id="w", name="old").save()
            await Snap(id="p", n=1, author=RecordID("SnapAuthor", "w")).save()
            t1 = await _now(client)
            versions = _count_versions(monkeypatch, client)
            calls = _record_queries(monkeypatch, client)
            with pytest.raises(SurrealDbError, match=r"fetch\(\) under as_of\(\) needs SurrealDB 3\.x") as info:
                await Snap.objects().as_of(t1).fetch("author").exec()
            sent = list(calls)  # before the cleanup's own statements
        assert str(info.value) == time_travel.fetch_refused()
        assert len(versions) == 1 and line_major(versions[0]) == 2, versions
        assert sent == []


# --- README section 27, the behaviour tables and the ROADMAP (spec §11; AC-18, AC-19; P2, P4, P5, D14) ----

_REPO = Path(__file__).resolve().parents[1]
_SECTION_27 = "### 27. Time travel (`as_of()`)"
_SERVER_BEHAVIOUR = "### ORM behaviour: SurrealDB 2.x vs 3.x"
_EMBEDDED_BEHAVIOUR = "### ORM behaviour: the embedded engine"
_COMING_FROM = "#### Coming from the full SurrealDB-ORM"

#: P5: the five rows v0.26.0 adds to the 2.x/3.x table, cell by cell, exactly as spec §11 writes them.
_TIME_TRAVEL_ROWS = [
    [
        "Time travel (`as_of()`) on a server without versioned storage",
        "no error, and the ORM cannot tell: in memory or on plain SurrealKV, only records last written at or "
        "before the moment, in their current state. RocksDB: `SurrealDbError`",
        "`SurrealDbError` (the server refuses `VERSION`)",
        "v0.26.0",
    ],
    [
        "Time travel on versioned storage: records deleted since, counts and aggregates",
        "right (the ORM's per-record check also corrects the server's unfiltered `count()`)",
        "right (the ORM's per-record check hides the records deleted before the moment that the server's table reads return)",
        "v0.26.0",
    ],
    [
        "`fetch()` and dotted paths through record links under `as_of()`",
        "`fetch()` refused (`SurrealDbError`) by `exec()`, `first()`, `get()` without an id; dotted paths in "
        "`filter()`, `select()`, `order_by()`, `values()` read the current value",
        "as they were at the moment",
        "v0.26.0",
    ],
    [
        "Writing history with a raw `CREATE … VERSION`",
        "stored at that moment, readable with `as_of()`",
        "accepted, not readable at that moment (the table did not exist then)",
        "v0.26.0",
    ],
    [
        "`as_of()` moment before 1970-01-01 or after 2262-04-11T23:47:16.854775Z",
        "refused by the ORM before any read (`ValueError`; `SurrealDbError` for a duration or expression resolving "
        "there): the server reads an earlier moment as the current state",
        "refused by the ORM before any read, same errors: the server would fail with an internal error",
        "v0.26.0",
    ],
]
#: The widths between the pipes of the 2.x/3.x table (535 characters a line): the new rows keep them (P5).
_SERVER_TABLE_WIDTHS = [136, 175, 181, 38]


def _section_27() -> str:
    return _readme_part(_SECTION_27)


def _section_27_examples() -> list[str]:
    """Section 27's ``python`` blocks, in order."""
    blocks: list[str] = re.findall(r"^```python\n(.*?)^```$", _section_27(), re.MULTILINE | re.DOTALL)
    assert blocks, "README section 27 has no example"
    return blocks


def _roadmap_part(heading: str) -> str:
    """``docs/ROADMAP.md`` from the ``heading`` line up to the next ``##``/``###`` heading."""
    roadmap = (_REPO / "docs" / "ROADMAP.md").read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(heading)}\n.*?(?=^#{{2,3}} )", roadmap, re.MULTILINE | re.DOTALL)
    assert match, f"docs/ROADMAP.md has no {heading!r} part"
    return match.group(0)


class TestTimeTravelDocs:
    """Unit: what README section 27, the behaviour tables, the comparison tables and ``docs/ROADMAP.md`` say
    about time travel (spec §11, AC-18, AC-19)."""

    def test_section_27_has_an_as_of_example(self) -> None:
        assert any(".as_of(" in block for block in _section_27_examples())

    def test_section_27_ends_with_coming_from_the_full_orm(self) -> None:
        """P4: the heading, found with the regex of ``tests/test_geo_fields.py``, closes the section."""
        section = _section_27()
        assert re.search(r"^#### Coming from the full SurrealDB-ORM$", section, re.MULTILINE), "P4 heading missing"
        assert re.findall(r"^#### .*$", section, re.MULTILINE)[-1] == _COMING_FROM
        text = _normalised(_subsection(_COMING_FROM, section))
        for needle in ("The full ORM has no time travel", "`as_of()`", "`version()`", "`==`", "`model_dump()`"):
            assert needle in text, needle

    def test_section_27_links_no_upstream_issue(self) -> None:
        """D14: the server bugs are stated as facts; reporting them upstream is the user's call."""
        assert "github.com/surrealdb/surrealdb/issues" not in _section_27()

    def test_sdk_duration_is_accepted_at_runtime_only(self) -> None:
        """P2: the annotation names the ORM's types; the SDK ``Duration`` works but a type checker flags it."""
        section = _normalised(_section_27())
        assert "`surrealdb.Duration`" in section and "accepted at runtime only" in section, section

    def test_section_27_names(self) -> None:
        section = _normalised(_section_27())
        for name in (
            "`VERSION`",
            "surrealkv+versioned://",
            "?versioned=true",
            "1970-01-01T00:00:00Z",
            "2262-04-11T23:47:16.854775Z",
            "midnight UTC",
            "a `date` object is refused",
            "one extra point read per record scanned",
            "await BaseSurrealModel.refresh(snapshot)",
            "model_dump()",
            "server_fields",
            "upsert()",
            "`get_related()`",
            "the current graph",
            "cannot tell",
            "objects(tx=",
        ):
            assert name in section, name

    def test_server_behaviour_rows(self) -> None:
        """P5: the five rows hold spec §11's texts and sit right before the table's last row."""
        rows = [cells for _, cells in _table_rows(_readme_part(_SERVER_BEHAVIOUR))]
        assert rows[-6:-1] == _TIME_TRAVEL_ROWS, rows[-6:-1]
        assert rows[-1][0].startswith("Full suite and every divergence") and rows[-1][3] == "v0.19.1", rows[-1]
        assert [cells for cells in rows if cells[3] == "v0.26.0"] == _TIME_TRAVEL_ROWS

    def test_server_behaviour_table_keeps_its_widths(self) -> None:
        """P5: no line of the table is re-padded; the new rows fit the existing columns."""
        [lines] = _tables(_readme_part(_SERVER_BEHAVIOUR))
        assert {len(line) for line in lines} == {535}
        for line in lines:
            assert [len(cell) for cell in line.split("|")[1:-1]] == _SERVER_TABLE_WIDTHS, line

    def test_server_behaviour_header_names_v0_26_0(self) -> None:
        header = _normalised(_readme_part(_SERVER_BEHAVIOUR).split("\n|", 1)[0])
        assert "rows added in v0.26.0 on 2.7.0 and 3.3.4, in memory and on versioned storage, identical on 3.1.5" in header, (
            header
        )

    def test_embedded_behaviour_row(self) -> None:
        rows = [cells for _, cells in _table_rows(_readme_part(_EMBEDDED_BEHAVIOUR))]
        [row] = [cells for cells in rows if cells[0] == "Time travel (`as_of()`)"]
        assert row == [
            "Time travel (`as_of()`)",
            "`surrealkv+versioned://` only (`?versioned=true` is ignored by SDK 2.0.0); any other URL → "
            "`SurrealDbError` before any request; `fetch()` refused (2.x core)",
            "versioned storage required (see the 2.x/3.x table)",
            "v0.26.0",
        ], row
        assert rows[-1][0] == "Everything else", "the catch-all row stays last"

    def test_section_26_url_row_points_to_section_27(self) -> None:
        rows = _table_rows(_readme_part("### 26. Embedded engine and test fixtures"), "Embedded URL")
        [versioned] = [cells for _, cells in rows if cells[0] == "`surrealkv+versioned://<path>`"]
        assert versioned[1] == "✅ on disk, keeps history: time travel with `as_of()` (section 27)", versioned

    def test_features_comparison_and_roadmap_rows(self) -> None:
        features = [cells for _, cells in _table_rows(_readme_part("## Features"))]
        assert ["Time travel (`as_of()`, versioned storage)", "✅"] in features, features
        comparison = [cells for _, cells in _table_rows(_readme_part("## SurrealDB-ORM-lite vs SurrealDB-ORM"))]
        assert ["Time travel (`VERSION`)", "✅ v0.26.0 (`as_of()`)", "❌"] in comparison, comparison
        firsts = [cells[0] for cells in comparison]
        assert firsts.index("Time travel (`VERSION`)") == firsts.index("Embedded engine") + 1
        roadmap = [cells[:2] for _, cells in _table_rows(_readme_part("## Roadmap"))]
        assert ["v0.26.0", "Time travel (versioned storage)"] in roadmap, roadmap
        assert ["v0.27.0 – v0.29.0", "Tier 2 — Extended (subqueries, cache, multi-db)"] in roadmap, roadmap
        assert not [cells for cells in roadmap if cells[0] == "v0.26.0 – v0.29.0"], roadmap

    def test_tables_have_no_raw_pipe_in_a_cell(self) -> None:
        """AC-19: every table v0.26.0 touches keeps one cell count per line and no escaped pipe."""
        parts = [
            _section_27(),
            _readme_part("### 26. Embedded engine and test fixtures"),
            _readme_part(_SERVER_BEHAVIOUR),
            _readme_part(_EMBEDDED_BEHAVIOUR),
            _readme_part("## Features"),
            _readme_part("## Roadmap"),
            _readme_part("## SurrealDB-ORM-lite vs SurrealDB-ORM"),
            _roadmap_part("### Embedded URLs"),
            _roadmap_part("## Planned — Tier 2: Extended (SDK-2.0-native)"),
        ]
        tables = [lines for part in parts for lines in _tables(part)]
        assert len(tables) >= len(parts) + 1, "section 27 has tables of its own"
        for lines in tables:
            assert len({len(_cells(line)) for line in lines}) == 1 and "\\|" not in "".join(lines), lines

    def test_roadmap_content(self) -> None:
        """Footnote ² of the Tier 2 table is defined below it; the embedded-URL parity row names ``as_of()``."""
        tier_2 = _roadmap_part("## Planned — Tier 2: Extended (SDK-2.0-native)")
        [lines] = _tables(tier_2)
        # The release marks the row ✅: it reads the same with or without the marker.
        [row] = [line for line in lines if line.startswith(("| v0.26.0", "| ✅ v0.26.0"))]
        assert "²" in row, row
        below = _normalised(tier_2.split(lines[-1], 1)[1])
        assert "¹ The embedded engine ships in the SDK 2.0.0 base wheel" in below, below
        assert (
            "² The versioned engine is the SDK 2.0.0 embedded engine's `surrealkv+versioned://`; versioned servers "
            "(3.x `?versioned=true`, 2.x `surrealkv+versioned://`) work too." in below
        ), below
        rows = _table_rows(_roadmap_part("### Embedded URLs"), "Embedded URL")
        [versioned] = [cells for _, cells in rows if cells[0] == "`surrealkv+versioned://<path>`"]
        assert versioned[1].startswith("✅") and "`as_of()`" in versioned[1], versioned

    def test_typing_example(self) -> None:
        """AC-18: each annotated form, and no SDK ``Duration`` (P2: accepted at runtime only)."""
        source = (_REPO / "tests" / "typing_examples.py").read_text(encoding="utf-8")
        match = re.search(r"^def time_travel_forms\(\) -> None:\n(.*?)(?=^\S|\Z)", source, re.MULTILINE | re.DOTALL)
        assert match, "tests/typing_examples.py has no time_travel_forms()"
        body = match.group(1)
        calls = re.findall(r"assert_type\(QuerySet\(TypedMember\)\.as_of\((.*)\), QuerySet\)$", body, re.MULTILINE)
        assert [call.split("(", 1)[0] for call in calls] == [
            "datetime",
            '"2026-01-01T00:00:00Z"',
            "timedelta",
            "Duration.parse",
            "SurrealFunc",
            "None",
        ], calls
        assert "surrealdb.Duration" not in body and "SdkDuration" not in body


class TestReadmeTimeTravelExamplesE2E:
    """AC-19: every example of README section 27 runs as written on the embedded versioned engine (no server),
    in the test's own directory."""

    async def test_section_27_examples(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(tempfile, "mkdtemp", lambda *args, **kwargs: str(tmp_path))
        namespace: dict[str, Any] = {"__name__": "readme_section_27"}
        try:
            for code in _section_27_examples():
                compiled = compile(code, "<README section 27>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                # A doctest of this repository's own README (trusted text), top-level awaits included.
                outcome = eval(compiled, namespace)
                if inspect.iscoroutine(outcome):
                    await outcome
        except AssertionError:
            moments = [namespace.get(name) for name in ("opening", "sale")]
            if all(isinstance(moment, datetime) for moment in moments):
                _strictly_increasing(*moments)  # a clock step skips; a wrong result fails
            raise
        finally:
            namespace.clear()  # no client of the examples outlives them (closed-client rule)
            await SurrealDBConnectionManager.unset_connection()

        printed = capsys.readouterr().out
        assert time_travel.moment_out_of_range(datetime(1900, 1, 1, tzinfo=UTC)) in printed
        assert "Product.save() cannot run on an instance read with as_of()" in printed
        assert not SurrealDBConnectionManager.is_connected()
