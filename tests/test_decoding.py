"""Reading what the surrealdb SDK 2.0.0 cannot decode (#235): the CBOR tag hook of ``decoding.py``.

SDK 2.0.0 raises on five kinds of values SurrealDB returns: a zero duration, a SurrealDB 3.x native
set, a 1-point (or empty) ``LineString``, a ``Polygon`` ring of fewer than 4 points and a datetime
outside years 1-9999 (#279). The unit tests drive the hook with hand-built CBOR and an embedded
client (no server); the classes suffixed ``E2E`` write each value with raw SurrealQL and read it back
through the ORM, on both server lines. The native set is a 3.x value: 2.x stores ``<set>[…]`` as an
array, so those cases skip there (capability probe). Tables are prefixed ``dc_`` and dropped before
and after each test.
"""

import asyncio
import contextlib
import inspect
import os
import types
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
import surrealdb
import surrealdb.connections.async_embedded
import surrealdb.connections.async_http
import surrealdb.connections.async_ws
import surrealdb.data.cbor
import surrealdb.data.types.constants
from pydantic import ValidationError
from surrealdb import AsyncSurreal, RecordID
from surrealdb.cbor import CBORTag

from surreal_orm_lite import (
    BaseSurrealModel,
    Duration,
    GeometryLine,
    GeometryMultiLine,
    GeometryPolygon,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    _sdk,
    decoding,
    encoding,
)
from tests.test_rich_fields import _take

# --- hand-built CBOR -----------------------------------------------------------------------------


def _point(x: float, y: float) -> CBORTag:
    return CBORTag(88, [float(x), float(y)])


def _line(*points: tuple[float, float]) -> CBORTag:
    return CBORTag(89, [_point(*p) for p in points])


def _polygon(*rings: tuple[tuple[float, float], ...]) -> CBORTag:
    return CBORTag(90, [_line(*ring) for ring in rings])


_SQUARE = ((0, 0), (1, 0), (1, 1), (0, 0))  # a valid closed ring
_THIN = ((0, 0), (1, 0), (0, 0))  # 3 points once closed: SurrealDB stores it, SDK 2.0.0 refuses it


def _datetime(seconds: int, nanoseconds: int = 0) -> CBORTag:
    """The server's compact datetime (tag 12): ``[seconds, nanoseconds]`` since 1970-01-01T00:00:00Z."""
    return CBORTag(12, [seconds, nanoseconds])


#: Unix seconds of datetimes outside years 1-9999, as SurrealDB 2.7.0 and 3.3.4 compute them
#: (``time::unix()``); ``-262143-01-01`` and ``+262142-12-31`` bound what the server stores.
_YEAR_0 = -62_154_086_400  # 0000-06-01T00:00:00Z
_BEFORE_YEAR_1 = -62_135_596_801  # 0000-12-31T23:59:59Z, the second before 0001-01-01
_YEAR_MINUS_1 = -62_198_755_200  # -0001-01-01T00:00:00Z
_LEAP_DAY_MINUS_2000 = -125_275_980_304  # -2000-02-29T12:34:56Z
_YEAR_10000 = 253_402_300_800  # +10000-01-01T00:00:00Z, the second after 9999-12-31T23:59:59Z
_SERVER_MIN = -8_334_601_228_800  # -262143-01-01T00:00:00Z
_SERVER_MAX = 8_210_266_876_799  # +262142-12-31T23:59:59Z
#: The first and the last second a Python ``datetime`` holds.
_YEAR_1 = -62_135_596_800  # 0001-01-01T00:00:00Z
_YEAR_9999_END = 253_402_300_799  # 9999-12-31T23:59:59Z


def _hooked(value: Any) -> Any:
    """*value* encoded by the SDK's codec, then decoded the way the ORM's clients decode it."""
    decode = decoding._hooked_decode()
    assert decode is not None, "the read hook is unavailable with the installed SDK"
    return decode(_sdk.cbor_encode(value))


@pytest.fixture
def fresh_caches(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear the hook's cached probes and clones before and after a test that changes what they read.

    Teardown order (#282): conftest's async autouse fixtures are torn down after this one and before the test's
    ``monkeypatch`` is undone. So the codec the test swapped is restored first, then the caches are cleared: a probe
    run in between would stay cached with the fake codec's answer.
    """
    caches = (
        decoding.sdk_decodes_zero_duration,
        decoding.sdk_decodes_native_sets,
        decoding.sdk_decodes_degenerate_geometries,
        decoding.sdk_decodes_out_of_range_datetimes,
        decoding._hooked_decode,
        decoding._hooked_methods,
    )
    for cache in caches:
        cache.cache_clear()
    yield
    monkeypatch.undo()
    for cache in caches:
        cache.cache_clear()


# --- the tag hook (unit) -------------------------------------------------------------------------

#: Each value SDK 2.0.0 cannot decode, with the exception its own codec raises.
_SDK_FAILURES: dict[str, tuple[Any, type[BaseException]]] = {
    "zero-duration": (CBORTag(14, []), IndexError),
    "native-set": (CBORTag(56, [1, 2]), BufferError),
    "line-1-point": (_line((1, 2)), TypeError),
    "polygon-3-point-ring": (_polygon(_THIN), surrealdb.InvalidGeometryError),
    "datetime-year-0": (_datetime(_YEAR_0), ValueError),
    "datetime-year-10000": (_datetime(_YEAR_10000), ValueError),
}


class TestReadTagHook:
    """Every tag goes to the SDK's ``tag_decoder`` first; only its five failures get a value instead."""

    @pytest.mark.parametrize("case", list(_SDK_FAILURES))
    def test_the_sdk_still_fails_on_each_value(self, case: str) -> None:
        """Canary: when the SDK reads one of these itself, its ``SDK 3.0 (#205)`` workaround can go."""
        value, error = _SDK_FAILURES[case]
        with pytest.raises(error):
            _sdk.cbor_decode(_sdk.cbor_encode(value))

    def test_zero_duration_reads_as_the_sdks_zero(self) -> None:
        for out in (_hooked(CBORTag(14, [])), _hooked({"v": [CBORTag(14, [])]})["v"][0]):
            assert type(out) is surrealdb.Duration and out.elapsed == 0

    def test_native_set_reads_as_a_set(self) -> None:
        out = _hooked(CBORTag(56, [1, 2]))
        assert type(out) is set and out == {1, 2}
        empty = _hooked(CBORTag(56, []))
        assert type(empty) is set and empty == set()
        # The items went through the SDK first: a decimal string tag is already a ``Decimal``.
        assert _hooked(CBORTag(56, [CBORTag(10, "1.5")])) == {Decimal("1.5")}

    def test_native_set_of_unhashable_items_reads_as_a_list(self) -> None:
        out = _hooked(CBORTag(56, [{"a": 1}, {"b": 2}]))
        assert type(out) is list and out == [{"a": 1}, {"b": 2}]
        records = _hooked(CBORTag(56, [CBORTag(8, ["t", 1]), CBORTag(8, ["t", 2])]))  # RecordID is unhashable
        assert type(records) is list and records == [RecordID("t", 1), RecordID("t", 2)]
        nested = _hooked(CBORTag(56, [CBORTag(56, [1])]))  # an inner set is unhashable too
        assert nested == [{1}]

    @pytest.mark.parametrize(
        ("items", "expected"),
        [
            pytest.param([1, True], [1, True], id="1-and-true"),
            pytest.param([0, False], [0, False], id="0-and-false"),
            pytest.param(["a", 1, True], ["a", 1, True], id="a-1-and-true"),
            pytest.param([1, 1.5, 1.0], [1, 1.5, 1.0], id="1-and-1.0"),
            pytest.param([1, CBORTag(10, "1")], [1, Decimal("1")], id="1-and-decimal-1"),
        ],
    )
    def test_native_set_of_items_python_finds_equal_reads_as_a_list(self, items: list[Any], expected: list[Any]) -> None:
        """SurrealDB already deduplicated the set: items Python finds equal (``1`` and ``true``) are distinct."""
        out = _hooked(CBORTag(56, items))
        assert type(out) is list and out == expected, out
        assert [type(item) for item in out] == [type(item) for item in expected], out

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(_line((1, 2)), {"type": "LineString", "coordinates": [[1.0, 2.0]]}, id="line-1-point"),
            pytest.param(CBORTag(89, []), {"type": "LineString", "coordinates": []}, id="line-empty"),
            pytest.param(
                CBORTag(92, [_line((1, 2)), _line((3, 4), (5, 6))]),
                {"type": "MultiLineString", "coordinates": [[[1.0, 2.0]], [[3.0, 4.0], [5.0, 6.0]]]},
                id="multiline-with-a-1-point-line",
            ),
            pytest.param(
                _polygon(_THIN),
                {"type": "Polygon", "coordinates": [[[0.0, 0.0], [1.0, 0.0], [0.0, 0.0]]]},
                id="polygon-3-point-ring",
            ),
            pytest.param(
                _polygon(_SQUARE, ((0, 0), (0, 0))),
                {
                    "type": "Polygon",
                    "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]], [[0.0, 0.0], [0.0, 0.0]]],
                },
                id="polygon-2-point-hole",
            ),
            pytest.param(
                CBORTag(90, [_line((0, 0))]),
                {"type": "Polygon", "coordinates": [[[0.0, 0.0]]]},
                id="polygon-ring-of-1-point",
            ),
            pytest.param(CBORTag(90, []), {"type": "Polygon", "coordinates": []}, id="polygon-without-ring"),
            pytest.param(
                CBORTag(93, [_polygon(_SQUARE), _polygon(_THIN)]),
                {
                    "type": "MultiPolygon",
                    "coordinates": [
                        [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
                        [[[0.0, 0.0], [1.0, 0.0], [0.0, 0.0]]],
                    ],
                },
                id="multipolygon-with-a-thin-polygon",
            ),
            pytest.param(
                CBORTag(94, [_point(1, 2), _line((3, 4)), CBORTag(94, [_line((5, 6), (7, 8))])]),
                {
                    "type": "GeometryCollection",
                    "geometries": [
                        {"type": "Point", "coordinates": [1.0, 2.0]},
                        {"type": "LineString", "coordinates": [[3.0, 4.0]]},
                        {
                            "type": "GeometryCollection",
                            "geometries": [{"type": "LineString", "coordinates": [[5.0, 6.0], [7.0, 8.0]]}],
                        },
                    ],
                },
                id="collection-with-a-1-point-line",
            ),
            pytest.param(
                CBORTag(94, [_point(0, 0), CBORTag(94, [_line((1, 2))])]),
                {
                    "type": "GeometryCollection",
                    "geometries": [
                        {"type": "Point", "coordinates": [0.0, 0.0]},
                        {"type": "GeometryCollection", "geometries": [{"type": "LineString", "coordinates": [[1.0, 2.0]]}]},
                    ],
                },
                id="collection-in-a-collection-with-a-1-point-line",
            ),
        ],
    )
    def test_degenerate_geometry_reads_as_geojson(self, value: Any, expected: dict[str, Any]) -> None:
        out = _hooked({"v": value})["v"]
        assert type(out) is dict and out == expected
        # Plain lists all the way down, as in GeoJSON (the SDK's ``get_coordinates()`` gives tuples).
        assert "(" not in repr(out)

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(_datetime(_YEAR_0), "0000-06-01T00:00:00Z", id="year-0"),
            pytest.param(_datetime(_BEFORE_YEAR_1), "0000-12-31T23:59:59Z", id="second-before-year-1"),
            pytest.param(_datetime(_YEAR_MINUS_1), "-0001-01-01T00:00:00Z", id="year-minus-1"),
            pytest.param(_datetime(_LEAP_DAY_MINUS_2000), "-2000-02-29T12:34:56Z", id="leap-day-of-year-minus-2000"),
            pytest.param(_datetime(_YEAR_10000), "+10000-01-01T00:00:00Z", id="year-10000"),
            pytest.param(_datetime(_SERVER_MIN), "-262143-01-01T00:00:00Z", id="server-minimum"),
            pytest.param(_datetime(_SERVER_MAX, 999_999_999), "+262142-12-31T23:59:59.999999999Z", id="server-maximum"),
            # The fraction keeps every nanosecond, in 3, 6 or 9 digits, as the server writes it.
            pytest.param(_datetime(_BEFORE_YEAR_1, 999_999_999), "0000-12-31T23:59:59.999999999Z", id="nanoseconds"),
            pytest.param(_datetime(_YEAR_10000, 120_000), "+10000-01-01T00:00:00.000120Z", id="microseconds"),
            pytest.param(_datetime(_YEAR_10000, 500_000_000), "+10000-01-01T00:00:00.500Z", id="milliseconds"),
            pytest.param(_datetime(_YEAR_MINUS_1, 1), "-0001-01-01T00:00:00.000000001Z", id="one-nanosecond"),
        ],
    )
    def test_out_of_range_datetime_reads_as_its_rfc3339_string(self, value: CBORTag, expected: str) -> None:
        """A datetime before year 1 or after year 9999 reads as the text SurrealDB gives it (``<string>``)."""
        for out in (_hooked(value), _hooked({"v": [value]})["v"][0]):
            assert type(out) is str and out == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(_datetime(_YEAR_1), datetime(1, 1, 1, tzinfo=UTC), id="first-second-of-year-1"),
            pytest.param(
                _datetime(_YEAR_9999_END, 999_999_999),
                datetime(9999, 12, 31, 23, 59, 59, 999_999, tzinfo=UTC),
                id="last-nanosecond-of-year-9999",
            ),
            pytest.param(_datetime(-1, 500_000_000), datetime(1969, 12, 31, 23, 59, 59, 500_000, tzinfo=UTC), id="pre-1970"),
        ],
    )
    def test_in_range_datetime_reads_as_the_sdks_datetime(self, value: CBORTag, expected: datetime) -> None:
        """The two boundaries of Python's range, and a datetime before 1970, decode exactly as the SDK decodes them."""
        data = _sdk.cbor_encode(value)
        out = decoding._hooked_decode()(data)  # type: ignore[misc]
        sdk = _sdk.cbor_decode(data)
        assert type(out) is datetime and out == expected == sdk and out.tzinfo == sdk.tzinfo

    def test_datetime_text_matches_python_in_its_range(self) -> None:
        """The hook's calendar arithmetic gives the text of Python's own ``datetime`` wherever Python has one."""
        moments = [datetime(1, 1, 1, tzinfo=UTC), datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)]
        for year in (1, 4, 99, 100, 400, 1582, 1600, 1700, 1900, 1969, 1970, 2000, 2024, 2100, 9996, 9999):
            for month, day in ((1, 1), (2, 28), (3, 1), (12, 31)):
                moments.append(datetime(year, month, day, 23, 59, 59, tzinfo=UTC))
            if year % 4 == 0 and (year % 100 or year % 400 == 0):
                moments.append(datetime(year, 2, 29, 12, 0, 0, tzinfo=UTC))
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        for moment in moments:
            seconds = (moment - epoch) // timedelta(seconds=1)
            for nanoseconds, fraction in ((0, ""), (1, ".000000001"), (7_000, ".000007"), (120_000_000, ".120")):
                text = moment.replace(tzinfo=None).isoformat(timespec="seconds") + fraction + "Z"
                assert decoding._datetime_text(seconds, nanoseconds) == text, moment

    def test_values_the_sdk_decodes_come_back_unchanged(self) -> None:
        corpus: list[Any] = [
            None,
            1,
            "text",
            [1, "a", None],
            {"nested": {"list": [1.5, True]}},
            RecordID("t", 1),
            RecordID("t", ["a", 1]),
            surrealdb.Duration(5),
            CBORTag(14, [7]),
            CBORTag(14, [7, 9]),
            datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC),
            CBORTag(12, [1_700_000_000, 123_456_789]),
            _datetime(_YEAR_1),
            _datetime(_YEAR_9999_END, 999_999_999),
            _datetime(-1, 500_000_000),
            Decimal("12.50"),
            UUID("0190c3d1-5c3b-7cc6-8f8a-2d2b1f0e9a11"),
            surrealdb.Range(_sdk.BoundIncluded(1), _sdk.BoundExcluded(5)),
            _point(1, 2),
            _line((1, 2), (3, 4)),
            _polygon(_SQUARE),
            CBORTag(91, [_point(1, 2)]),
            CBORTag(92, [_line((1, 2), (3, 4))]),
            CBORTag(93, [_polygon(_SQUARE)]),
            CBORTag(94, [_point(1, 2), _line((1, 2), (3, 4))]),
            CBORTag(91, []),
            CBORTag(94, []),
        ]
        for value in corpus:
            data = _sdk.cbor_encode(value)
            expected = _sdk.cbor_decode(data)
            out = decoding._hooked_decode()(data)  # type: ignore[misc]
            assert type(out) is type(expected) and out == expected and repr(out) == repr(expected), value

    @pytest.mark.parametrize(
        ("value", "error"),
        [
            pytest.param(CBORTag(999, 1), BufferError, id="unknown-tag"),
            pytest.param(CBORTag(56, 5), BufferError, id="set-tag-without-an-array"),
            pytest.param(CBORTag(14, ["x"]), surrealdb.InvalidDurationError, id="duration-in-another-form"),
            pytest.param(CBORTag(12, []), IndexError, id="empty-compact-datetime"),
            # A datetime outside years 1-9999 reads as text only in the server's form: two integers,
            # the nanoseconds within a second. SurrealDB never sends another.
            pytest.param(CBORTag(12, [_YEAR_0]), IndexError, id="out-of-range-datetime-without-nanoseconds"),
            pytest.param(CBORTag(12, [_YEAR_0, 0, 0]), ValueError, id="out-of-range-datetime-with-three-parts"),
            pytest.param(_datetime(_YEAR_0, 1_000_000_000), ValueError, id="out-of-range-datetime-with-a-second-of-ns"),
            pytest.param(_datetime(_YEAR_0, -1), ValueError, id="out-of-range-datetime-with-negative-ns"),
            pytest.param(CBORTag(12, [float(_YEAR_0), 0]), ValueError, id="out-of-range-datetime-in-float-seconds"),
            pytest.param(CBORTag(12, [_YEAR_0, 0.5]), ValueError, id="out-of-range-datetime-with-float-ns"),
            pytest.param(CBORTag(12, "0000-06-01T00:00:00Z"), TypeError, id="datetime-tag-holding-text"),
            pytest.param(CBORTag(8, []), IndexError, id="record-id-without-parts"),
            # A point is never degenerate, and a geometry's members are geometries, never bare numbers.
            pytest.param(CBORTag(88, [1.0]), IndexError, id="point-with-one-coordinate"),
            pytest.param(CBORTag(89, [5]), TypeError, id="line-of-bare-numbers"),
            pytest.param(CBORTag(92, 5), TypeError, id="multiline-without-an-array"),
            pytest.param(CBORTag(94, 5), TypeError, id="collection-without-an-array"),
        ],
    )
    def test_other_failures_still_raise_the_sdks_error(self, value: Any, error: type[BaseException]) -> None:
        data = _sdk.cbor_encode({"v": value})
        hooked = decoding._hooked_decode()
        assert hooked is not None
        try:
            _sdk.cbor_decode(data)
        except error as exc:
            sdk_error: BaseException = exc
        else:  # pragma: no cover - the SDK reads it: nothing to compare
            pytest.skip(f"the installed SDK reads {value!r}")
        with pytest.raises(type(sdk_error)) as info:
            hooked(data)
        assert str(info.value) == str(sdk_error)

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            pytest.param(CBORTag(92, [_line((1, 2)), _point(3, 4)]), "not a LineString", id="multiline-holding-a-point"),
            pytest.param(CBORTag(93, [_line((1, 2))]), "not a GeoJSON Polygon", id="multipolygon-holding-a-line"),
            pytest.param(CBORTag(94, [_line((1, 2)), 7]), "not a geometry", id="collection-holding-a-number"),
            pytest.param(
                CBORTag(94, [_line((1, 2)), {"type": "Unknown", "coordinates": []}]),
                "not a GeoJSON geometry this hook reads",
                id="collection-holding-an-object",
            ),
            pytest.param(
                CBORTag(94, [_line((1, 2)), {"type": "LineString"}]),
                "not a GeoJSON LineString",
                id="collection-holding-an-object-without-coordinates",
            ),
        ],
    )
    def test_degenerate_member_beside_a_member_of_another_kind_raises(self, value: Any, message: str) -> None:
        """A value SurrealDB never sends: the SDK refuses the degenerate line, the hook refuses the whole."""
        data = _sdk.cbor_encode({"v": value})
        with pytest.raises(TypeError):
            _sdk.cbor_decode(data)
        hooked = decoding._hooked_decode()
        assert hooked is not None
        with pytest.raises(TypeError, match=message):
            hooked(data)


# --- probes, layout and installation (unit) ------------------------------------------------------

#: The SDK 2.0.0 connection classes and the methods that decode a response (``decode`` global).
_DECODE_SITES = {
    surrealdb.AsyncWsSurrealConnection: {"_recv_task"},
    surrealdb.AsyncHttpSurrealConnection: {"_send"},
    surrealdb.AsyncEmbeddedSurrealConnection: {"_send", "_recv_task"},  # ``_recv_task`` inherited, never started
}


def _decode_sites(cls: type) -> set[str]:
    """The methods of *cls* (inherited ones included) whose code reads a global named ``decode``."""
    return {
        name
        for name, member in inspect.getmembers(cls, inspect.isfunction)
        if "decode" in member.__code__.co_names and member.__globals__.get("decode") is surrealdb.data.cbor.decode
    }


def _ws_url() -> str:
    return f"ws://{os.environ.get('SURREALDB_HOST', 'localhost')}:{os.environ.get('SURREALDB_PORT', '8000')}/rpc"


def _http_url() -> str:
    return f"http://{os.environ.get('SURREALDB_HOST', 'localhost')}:{os.environ.get('SURREALDB_PORT', '8000')}"


#: The capability probes of ``decoding.py``, by the name after ``sdk_decodes_``.
_PROBES = ("zero_duration", "native_sets", "degenerate_geometries", "out_of_range_datetimes")


class TestProbes:
    """The hook is installed only when the installed SDK cannot decode these values (no version check)."""

    def test_sdk_2_0_0_needs_the_hook(self) -> None:
        for name in _PROBES:
            assert not getattr(decoding, f"sdk_decodes_{name}")(), name
        assert decoding.needed() and decoding.available() and decoding.active()
        assert encoding.reads_zero_duration()

    @pytest.mark.parametrize("readable", [name.replace("_", "-") for name in _PROBES])
    def test_needed_while_one_value_is_unreadable(self, readable: str, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in _PROBES:
            monkeypatch.setattr(decoding, f"sdk_decodes_{name}", lambda: True)
        assert not decoding.needed()
        monkeypatch.setattr(decoding, f"sdk_decodes_{readable.replace('-', '_')}", lambda: False)
        assert decoding.needed()

    @pytest.mark.usefixtures("fresh_caches")
    @pytest.mark.parametrize("name", ["cbor_decode", "cbor_encode"])
    def test_probes_answer_false_without_a_codec(self, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_sdk, name, None)
        for probe in _PROBES:
            assert not getattr(decoding, f"sdk_decodes_{probe}")(), probe

    @pytest.mark.usefixtures("fresh_caches")
    def test_probes_answer_true_when_the_codec_reads_each_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hooked = decoding._hooked_decode()  # built from the SDK's decode before the codec is swapped
        assert hooked is not None
        monkeypatch.setattr(_sdk, "cbor_decode", hooked)
        for probe in _PROBES:
            assert getattr(decoding, f"sdk_decodes_{probe}")(), probe

    @pytest.mark.usefixtures("fresh_caches")
    def test_zero_probe_answers_false_on_another_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_sdk, "cbor_decode", lambda data: surrealdb.Duration(1))
        assert not decoding.sdk_decodes_zero_duration()

    @pytest.mark.usefixtures("fresh_caches")
    @pytest.mark.parametrize("unreadable", [_BEFORE_YEAR_1, _YEAR_10000], ids=["year-0", "year-10000"])
    def test_datetime_probe_answers_false_unless_both_ends_read(self, unreadable: int, monkeypatch: pytest.MonkeyPatch) -> None:
        """A codec that reads a datetime before year 1 but not one after year 9999 (or the reverse) still needs the hook."""

        def reads_one_end(data: bytes) -> Any:
            tag = surrealdb.cbor.loads(data)  # no tag hook: the datetime stays a ``CBORTag``
            if tag.value[0] == unreadable:
                raise ValueError("year is out of range")
            return "a datetime"

        monkeypatch.setattr(_sdk, "cbor_decode", reads_one_end)
        assert not decoding.sdk_decodes_out_of_range_datetimes()


class TestSdkLayout:
    """Canary: the SDK 2.0.0 internals the hook relies on. A failure here means the SDK's layout changed:
    re-check ``decoding.py`` (``SDK 3.0 (#205)`` comments) before anything else."""

    @pytest.mark.parametrize("cls", list(_DECODE_SITES), ids=lambda cls: cls.__name__)
    def test_decode_sites_of_each_connection_class(self, cls: type) -> None:
        assert _decode_sites(cls) == _DECODE_SITES[cls], (
            f"{cls.__name__} decodes responses elsewhere now: update decoding._DECODING_METHODS"
        )
        assert set(decoding._DECODING_METHODS) >= _DECODE_SITES[cls]

    def test_the_sdk_decode_reads_its_tag_decoder_global(self) -> None:
        decode = surrealdb.data.cbor.decode
        assert _sdk.cbor_decode is decode
        assert _sdk.cbor_tag_decoder is surrealdb.data.cbor.tag_decoder
        assert "tag_decoder" in decode.__code__.co_names
        assert decode.__globals__["tag_decoder"] is _sdk.cbor_tag_decoder

    def test_the_connection_modules_bind_the_sdk_decode(self) -> None:
        for module in (
            surrealdb.connections.async_ws,
            surrealdb.connections.async_http,
            surrealdb.connections.async_embedded,
        ):
            assert module.decode is surrealdb.data.cbor.decode, module.__name__

    def test_the_protocol_tags_match_the_sdks(self) -> None:
        sdk_tags = {
            surrealdb.TAG_GEOMETRY_POINT: "Point",
            surrealdb.TAG_GEOMETRY_LINE: "LineString",
            surrealdb.TAG_GEOMETRY_POLYGON: "Polygon",
            surrealdb.TAG_GEOMETRY_MULTI_POINT: "MultiPoint",
            surrealdb.TAG_GEOMETRY_MULTI_LINE: "MultiLineString",
            surrealdb.TAG_GEOMETRY_MULTI_POLYGON: "MultiPolygon",
            surrealdb.TAG_GEOMETRY_COLLECTION: "GeometryCollection",
        }
        assert sdk_tags == decoding._GEOMETRY_TYPES
        assert decoding.TAG_SET == 56
        assert decoding.TAG_SET not in vars(surrealdb.data.types.constants).values()  # SDK 2.0.0 has none
        assert _sdk.TAG_DATETIME_COMPACT == surrealdb.TAG_DATETIME_COMPACT == 12


class TestInstall:
    """The hook goes on one client at a time: the SDK's modules and classes are never changed."""

    @pytest.mark.parametrize("url", ["ws://localhost:1/rpc", "http://localhost:1", "mem://"])
    def test_installs_the_sdks_own_code_on_the_instance(self, url: str) -> None:
        client = AsyncSurreal(url)
        assert decoding.install(client)
        cls = type(client)
        expected = _DECODE_SITES[cls]
        assert expected <= set(vars(client))
        hooked = decoding._hooked_decode()
        for name in expected:
            bound = vars(client)[name]
            assert isinstance(bound, types.MethodType) and bound.__self__ is client
            original = getattr(cls, name)
            assert bound.__func__.__code__ is original.__code__, "not the SDK's own code"
            assert bound.__func__.__globals__["decode"] is hooked
            # The SDK's class and module still decode with the SDK's ``decode``.
            assert original.__globals__["decode"] is surrealdb.data.cbor.decode
        assert inspect.getmodule(cls).decode is surrealdb.data.cbor.decode  # type: ignore[union-attr]
        assert surrealdb.data.cbor.tag_decoder is _sdk.cbor_tag_decoder
        # Another client built by the application keeps the SDK's behaviour.
        assert not expected & set(vars(AsyncSurreal(url)))

    def test_not_installed_when_the_sdk_decodes_every_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in _PROBES:
            monkeypatch.setattr(decoding, f"sdk_decodes_{name}", lambda: True)
        client = AsyncSurreal("http://localhost:1")
        assert not decoding.active()
        assert not decoding.install(client)
        assert "_send" not in vars(client)

    @pytest.mark.usefixtures("fresh_caches")
    def test_not_installed_when_the_sdk_layout_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_sdk, "cbor_tag_decoder", None)
        client = AsyncSurreal("http://localhost:1")
        assert decoding.needed() and not decoding.available()
        assert not decoding.install(client)
        assert "_send" not in vars(client)
        # Nothing reads a zero duration back then: the write refusal is back.
        assert not encoding.reads_zero_duration()
        with pytest.raises(ValueError, match="zero duration"):
            encoding.encode(timedelta(0))

    @pytest.mark.usefixtures("fresh_caches")
    def test_not_installed_on_a_client_of_another_kind(self) -> None:
        class Fake:
            async def _send(self) -> Any:  # reads no ``decode`` global
                return None

        fake = Fake()
        assert not decoding.install(fake)
        assert "_send" not in vars(fake)


#: Each value SDK 2.0.0 cannot decode, as raw SurrealQL, and whether it exists on SurrealDB 2.x.
_VALUES: dict[str, tuple[str, bool]] = {
    "zero-0s": ("0s", True),
    "zero-0ns": ("0ns", True),
    "native-set": ("<set>[1, 2]", False),
    "line-1-point": ("{type: 'LineString', coordinates: [[1, 2]]}", True),
    "multiline-with-a-1-point-line": ("{type: 'MultiLineString', coordinates: [[[1, 2]], [[3, 4], [5, 6]]]}", True),
    "polygon-3-point-ring": ("{type: 'Polygon', coordinates: [[[0, 0], [1, 0], [0, 0]]]}", True),
    "datetime-year-0": ("d'0000-06-01T00:00:00Z'", True),
    "datetime-year-10000": ("d'9999-12-31T23:59:59Z' + 1s", True),  # 2.x does not parse d'+10000-…'
}

#: What an untyped field reads for each value.
_UNTYPED: dict[str, Callable[[Any], bool]] = {
    "zero-0s": lambda v: type(v) is surrealdb.Duration and v.elapsed == 0,
    "zero-0ns": lambda v: type(v) is surrealdb.Duration and v.elapsed == 0,
    "native-set": lambda v: type(v) is set and v == {1, 2},
    "line-1-point": lambda v: v == {"type": "LineString", "coordinates": [[1, 2]]},
    "multiline-with-a-1-point-line": lambda v: v == {"type": "MultiLineString", "coordinates": [[[1, 2]], [[3, 4], [5, 6]]]},
    "polygon-3-point-ring": lambda v: v == {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [0, 0]]]},
    "datetime-year-0": lambda v: type(v) is str and v == "0000-06-01T00:00:00Z",
    "datetime-year-10000": lambda v: type(v) is str and v == "+10000-01-01T00:00:00Z",
}


class TestEmbeddedClient:
    """The embedded engine (``mem://``, no server): the hook covers the client's ``_send``."""

    @pytest.mark.asyncio
    async def test_embedded_client_reads_each_value(self) -> None:
        client = AsyncSurreal("mem://")
        await client.connect()
        try:
            await client.use("ns", "db")
            # Without the hook, the embedded client fails like the others.
            with pytest.raises(IndexError):
                await client.query("RETURN 0ns;", {})
            assert decoding.install(client)
            for case, (literal, _) in _VALUES.items():
                out = await client.query(f"RETURN {literal};", {})
                if case == "native-set":  # the embedded engine stores ``<set>[…]`` as an array, like 2.x
                    assert sorted(out) == [1, 2], out
                    continue
                assert _UNTYPED[case](out), (case, out)
            assert await client.query("RETURN 1;", {}) == 1
        finally:
            await client.close()


# --- reads through the ORM (E2E) -----------------------------------------------------------------


class DcAny(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="dc_value")

    id: str | None = None
    name: str = ""
    v: Any = None


class DcDelta(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="dc_value")

    id: str | None = None
    name: str = ""
    v: timedelta | None = None


class DcDuration(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="dc_value")

    id: str | None = None
    name: str = ""
    v: Duration | None = None


class DcSet(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="dc_value")

    id: str | None = None
    name: str = ""
    v: set[int] | None = None


class DcLine(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="dc_value")

    id: str | None = None
    name: str = ""
    v: GeometryLine | None = None


class DcMultiLine(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="dc_value")

    id: str | None = None
    name: str = ""
    v: GeometryMultiLine | None = None


class DcPolygon(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="dc_value")

    id: str | None = None
    name: str = ""
    v: GeometryPolygon | None = None


class DcDatetime(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="dc_value")

    id: str | None = None
    name: str = ""
    v: datetime | None = None


#: The typed models of each value and what they read: the value, or ``ValidationError`` for a
#: degenerate geometry (the ORM's geometry classes refuse those by design) and for a datetime outside
#: years 1-9999 (a Python ``datetime`` cannot hold it).
_TYPED: dict[str, list[tuple[type[BaseSurrealModel], Any]]] = {
    "zero-0s": [(DcDelta, timedelta(0)), (DcDuration, Duration(0))],
    "zero-0ns": [(DcDelta, timedelta(0)), (DcDuration, Duration(0))],
    "native-set": [(DcSet, {1, 2})],
    "line-1-point": [(DcLine, ValidationError)],
    "multiline-with-a-1-point-line": [(DcMultiLine, ValidationError)],
    "polygon-3-point-ring": [(DcPolygon, ValidationError)],
    "datetime-year-0": [(DcDatetime, ValidationError)],
    "datetime-year-10000": [(DcDatetime, ValidationError)],
}


async def _read_get(model: type[BaseSurrealModel]) -> Any:
    return await model.objects().get("one")


async def _read_filter(model: type[BaseSurrealModel]) -> Any:
    rows = await model.objects().filter(name="x").exec()
    assert len(rows) == 1, rows
    return rows[0]


async def _read_raw_query(model: type[BaseSurrealModel]) -> Any:
    rows = await model.raw_query("SELECT * FROM dc_value:one;")
    assert len(rows) == 1, rows
    return rows[0]


_READS: dict[str, Callable[[type[BaseSurrealModel]], Awaitable[Any]]] = {
    "get": _read_get,
    "filter-exec": _read_filter,
    "raw-query": _read_raw_query,
}


async def _native_sets() -> bool:
    """Whether the server stores ``<set>[…]`` as a native set (3.x) rather than an array (2.x)."""
    client = await SurrealDBConnectionManager.get_client()
    text = await client.query("RETURN <string> <set>[1, 2];", {})  # 3.x: "{1, 2}"; 2.x: "[1, 2]"
    return isinstance(text, str) and text.startswith("{")


@contextlib.asynccontextmanager
async def _orm(transport: str) -> AsyncIterator[Any]:
    """An ORM client over *transport* (``ws`` or ``http``), with ``dc_value`` dropped before and after.

    The cleanup asks the manager for its client again, so it runs on a usable client whatever the
    test did.
    """
    url = _ws_url() if transport == "ws" else _http_url()
    SurrealDBConnectionManager.set_connection(url=url, user="root", password="root", namespace="ns", database="db")
    client = await SurrealDBConnectionManager.get_client()
    await client.query("REMOVE TABLE IF EXISTS dc_value;", {})
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            cleanup = await SurrealDBConnectionManager.get_client()
            await asyncio.wait_for(cleanup.query("REMOVE TABLE IF EXISTS dc_value;", {}), 10)
        await SurrealDBConnectionManager.close_connection()


async def _write(client: Any, case: str) -> None:
    literal, on_2x = _VALUES[case]
    if not on_2x and not await _native_sets():
        pytest.skip("SurrealDB 2.x stores <set>[…] as an array: no native set to read")
    await client.query(f"CREATE dc_value:one SET name = 'x', v = {literal} RETURN NONE;", {})


async def _assert_client_keeps_working(client: Any) -> None:
    """Later calls on the same client keep working: ``get_client()`` returns it and it still answers."""
    assert (await SurrealDBConnectionManager.get_client()) is client
    task = getattr(client, "recv_task", None)
    assert task is None or not task.done(), "the client stopped working after the read"
    assert await client.query("RETURN 1;", {}) == 1


class TestUndecodableReadsE2E:
    """Each value, written with raw SurrealQL, reads back through the ORM on every read path."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("read", list(_READS))
    @pytest.mark.parametrize("case", list(_VALUES))
    async def test_untyped_field(self, case: str, read: str) -> None:
        async with _orm("ws") as client:
            await _write(client, case)
            row = await _READS[read](DcAny)
            assert isinstance(row, DcAny), row
            assert _UNTYPED[case](row.v), row.v
            await _assert_client_keeps_working(client)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("read", list(_READS))
    async def test_native_set_of_items_python_finds_equal(self, read: str) -> None:
        """``<set>[1, true]`` holds two items on SurrealDB 3.x, which Python finds equal: both come back."""
        async with _orm("ws") as client:
            if not await _native_sets():
                pytest.skip("SurrealDB 2.x stores <set>[…] as an array: no native set to read")
            await client.query("CREATE dc_value:one SET name = 'x', v = <set>[1, true] RETURN NONE;", {})
            row = await _READS[read](DcAny)
            assert isinstance(row, DcAny), row
            assert type(row.v) is list and sorted(row.v, key=repr) == [1, True], row.v
            assert sorted(type(item).__name__ for item in row.v) == ["bool", "int"], row.v
            await _assert_client_keeps_working(client)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("read", list(_READS))
    @pytest.mark.parametrize("case", list(_VALUES))
    async def test_typed_field(self, case: str, read: str) -> None:
        async with _orm("ws") as client:
            await _write(client, case)
            for model, expected in _TYPED[case]:
                if expected is not ValidationError:
                    row = await _READS[read](model)
                    assert isinstance(row, model), row
                    assert row.v == expected and type(row.v) is type(expected), row.v
                elif read in ("filter-exec", "raw-query"):
                    # exec() and raw_query() hand back the rows a model cannot validate, as dicts.
                    row = await _READS[read](model)
                    assert type(row) is dict and _UNTYPED[case](row["v"]), row
                else:
                    with pytest.raises(ValidationError) as info:
                        await _READS[read](model)
                    assert [error["loc"] for error in info.value.errors()] == [("v",)], info.value
                    assert model.__name__ in str(info.value)
            await _assert_client_keeps_working(client)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", list(_VALUES))
    async def test_http_client(self, case: str) -> None:
        async with _orm("http") as client:
            await _write(client, case)
            row = await DcAny.objects().get("one")
            assert _UNTYPED[case](row.v), row.v
            await _assert_client_keeps_working(client)

    @pytest.mark.asyncio
    async def test_ephemeral_client(self) -> None:
        async with _orm("ws") as client:
            await _write(client, "zero-0ns")
            async with SurrealDBConnectionManager.ephemeral_client() as anonymous:
                await anonymous.signin({"username": "root", "password": "root"})
                await anonymous.use("ns", "db")
                assert _UNTYPED["zero-0ns"](await anonymous.query("SELECT VALUE v FROM ONLY dc_value:one;", {}))
                assert not anonymous.recv_task.done()


#: Datetimes outside years 1-9999 in SurrealQL that both lines parse (2.x has no ``d'+10000-…'``).
_OUT_OF_RANGE_DATETIMES = (
    "d'0000-06-01T00:00:00Z'",
    "d'0001-01-01T00:00:00Z' - 1ns",
    "d'0001-01-01T00:00:00Z' - 500ms",
    "d'-0001-01-01T00:00:00.000001Z'",
    "d'-2000-02-29T12:34:56Z'",
    "d'9999-12-31T23:59:59.999999999Z' + 1ns",
    "d'9999-12-31T23:59:59Z' + 1000y + 120us",
)


class TestOutOfRangeDatetimeTextE2E:
    """A datetime outside years 1-9999 reads as the text the server itself gives it (``<string>``)."""

    @pytest.mark.asyncio
    async def test_the_text_is_the_servers(self) -> None:
        async with _orm("ws") as client:
            for literal in _OUT_OF_RANGE_DATETIMES:
                value, text = await client.query(f"RETURN [{literal}, <string> ({literal})];", {})
                assert type(value) is str and value == text, (literal, value, text)
            # In range, the SDK's datetime as before: the first and the last second Python holds.
            first, last = await client.query("RETURN [d'0001-01-01T00:00:00Z', d'9999-12-31T23:59:59.999999999Z'];", {})
            assert first == datetime(1, 1, 1, tzinfo=UTC)
            assert last == datetime(9999, 12, 31, 23, 59, 59, 999_999, tzinfo=UTC)
            await _assert_client_keeps_working(client)


async def _stored_is(client: Any, kind: str) -> bool:
    """Whether ``dc_value:one.v`` is stored as a value of *kind* (``geometry``, ``object``, ``array``, ``string``).

    SurrealDB 3.x names the check ``type::is_<kind>``, 2.x ``type::is::<kind>``.
    """
    version = str(await client.version()).removeprefix("surrealdb-")
    function = f"type::is::{kind}" if version.startswith("2.") else f"type::is_{kind}"
    return await client.query(f"RETURN {function}((SELECT VALUE v FROM ONLY dc_value:one));", {}) is True


#: The values written back below, and the type the ORM's write stores them as.
_WRITTEN_BACK_AS: dict[str, str] = {
    "line-1-point": "object",
    "multiline-with-a-1-point-line": "object",
    "polygon-3-point-ring": "object",
    "native-set": "array",
    "datetime-year-0": "string",
    "datetime-year-10000": "string",
}


class TestWriteBackE2E:
    """Written back through the ORM, a value the hook read is stored the way the ORM writes it: a
    degenerate geometry's GeoJSON dict as a plain object, a native set as an array, a datetime outside
    years 1-9999 as its text (the ORM never writes a degenerate geometry, a native set or such a
    datetime)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("write", ["merge", "update"])
    @pytest.mark.parametrize("case", list(_WRITTEN_BACK_AS))
    async def test_written_back_value_changes_its_stored_type(self, case: str, write: str) -> None:
        async with _orm("ws") as client:
            await _write(client, case)
            kind = _WRITTEN_BACK_AS[case]
            assert not await _stored_is(client, kind)
            assert await _stored_is(client, "geometry") is (kind == "object")
            row = await DcAny.objects().get("one")
            if write == "merge":
                await row.merge(v=row.v)
            else:
                row.name = "y"  # update() rewrites the whole row, the untouched ``v`` included
                await row.update()
            assert await _stored_is(client, kind)
            assert not await _stored_is(client, "geometry")
            again = await DcAny.objects().get("one")
            if kind == "array":
                assert type(again.v) is list and sorted(again.v) == [1, 2], again.v
            else:
                assert _UNTYPED[case](again.v), again.v


class TestUndecodableLiveE2E:
    """A live query whose notification carries such a value delivers it, and later calls on the same
    client keep working."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", list(_VALUES))
    async def test_live_event_delivers_the_value(self, case: str) -> None:
        async with _orm("ws") as client:
            literal, on_2x = _VALUES[case]
            if not on_2x and not await _native_sets():
                pytest.skip("SurrealDB 2.x stores <set>[…] as an array: no native set to read")
            await client.query("DEFINE TABLE dc_value SCHEMALESS;", {})
            async with DcAny.objects().live() as stream:
                await client.query(f"CREATE dc_value:one SET name = 'x', v = {literal} RETURN NONE;", {})
                [event] = await _take(stream, 1)
            assert event.validation_error is None, event.validation_error
            assert _UNTYPED[case](event.instance.v), event.instance.v
            await _assert_client_keeps_working(client)


class TestScopeE2E:
    """Only the clients the ORM opens get the hook."""

    @pytest.mark.asyncio
    async def test_a_client_the_application_builds_keeps_the_sdks_behaviour(self) -> None:
        async with _orm("ws") as client:
            await _write(client, "zero-0ns")
            assert _UNTYPED["zero-0ns"](await client.query("SELECT VALUE v FROM ONLY dc_value:one;", {}))
            own = AsyncSurreal(_http_url())
            await own.signin({"username": "root", "password": "root"})
            await own.use("ns", "db")
            with pytest.raises(IndexError):
                await own.query("SELECT VALUE v FROM ONLY dc_value:one;", {})

    @pytest.mark.asyncio
    async def test_not_installed_the_orm_client_keeps_the_sdks_behaviour(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(decoding, "needed", lambda: False)
        async with _orm("http") as client:
            assert "_send" not in vars(client)
            await _write(client, "zero-0ns")
            with pytest.raises(IndexError):
                await client.query("SELECT VALUE v FROM ONLY dc_value:one;", {})

    @pytest.mark.asyncio
    async def test_ws_concurrent_reads_all_return_the_value(self) -> None:
        """Several concurrent reads on one WebSocket client each return the value."""
        async with _orm("ws") as client:
            await _write(client, "zero-0ns")
            reads = [client.query("SELECT VALUE v FROM ONLY dc_value:one;", {}) for _ in range(5)]
            results = await asyncio.wait_for(asyncio.gather(*reads), 10)
            assert all(_UNTYPED["zero-0ns"](value) for value in results), results
            await _assert_client_keeps_working(client)
