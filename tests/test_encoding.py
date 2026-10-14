"""``encoding.encode()``: the value-driven boundary between the ORM and the SDK (v0.23.0).

Unit tests, no database: one test per row of ``encode`` with its edges, the properties the hand-off
relies on (identity on every value v0.22.2 could send, idempotence, no mutation of the input) and
the local zero-duration probe. The raw-SDK round trips of the encoded forms, over WebSocket and
HTTP on both server lines, live in ``tests/test_rich_types.py``.
"""

import copy
import decimal
import time
from datetime import UTC, date, datetime, timedelta, timezone
from datetime import time as dt_time
from decimal import Decimal
from enum import IntEnum
from typing import Any, NamedTuple
from uuid import UUID

import pytest
import surrealdb
from surrealdb import Range as SdkRange
from surrealdb import RecordID
from surrealdb.cbor import CBORSimpleValue, CBORTag, undefined

from surreal_orm_lite import BoundExcluded, BoundIncluded, Duration, Range, _sdk, encoding
from surreal_orm_lite._sdk import Bound
from tests.test_rich_types import _DECIMAL_FIXED, _decimal_corpus

MAX_NS = Duration.MAX_NANOSECONDS
_DUR = _sdk.TAG_DURATION_COMPACT
_DEC = _sdk.TAG_DECIMAL_STRING
_NULL = _sdk.CBOR_NULL
_ZEROS_29 = "0." + "0" * 29
_UUID = UUID("0190c3d1-5c3b-7cc6-8f8a-2d2b1f0e9a11")
_AT = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
_NAIVE = datetime(2026, 1, 2, 3, 4, 5)
_MIDNIGHT = datetime(2026, 1, 2, tzinfo=UTC)
# ``Range(None, BoundExcluded(5))`` as the protocol wants it: tag 49 [CBOR null, tag 51 5].
_OPEN_TO_FIVE = bytes.fromhex("d83182f6d83305")


def _encode(value: Any) -> Any:
    """``encode`` through the module attribute, as the ORM calls it."""
    return encoding.encode(value)


class _SubSimple(CBORSimpleValue):
    """A ``CBORSimpleValue`` subclass: off the fast path, unchanged through the last row."""


class _SubDate(date):
    """A ``date`` subclass (``pendulum.Date``-like)."""


class _SubDateTime(datetime):
    """A ``datetime`` subclass: never the ``date`` row."""


class _SubTimedelta(timedelta):
    """A ``timedelta`` subclass: the duration row all the same."""


class _Level(IntEnum):
    HIGH = 3


class _SubDict(dict[str, Any]):
    pass


class _SubList(list[Any]):
    pass


class _Pair(NamedTuple):
    a: Any
    b: Any


def _assert_open_side_bytes(value: Any) -> bytes:
    """The SDK's bytes for ``value``: an open side is the CBOR null, never tag-6 NONE nor ``undefined``."""
    data: bytes = _sdk.cbor_encode(value)
    assert b"\xc6\xf6" not in data, data.hex()
    assert b"\xf7" not in data, data.hex()
    return data


# --- every row of encode() ----------------------------------------------------------------------


class TestEncodeRows:
    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "text",
            b"",
            b"\x00\xff",
            1.5,
            float("nan"),
            float("inf"),
            True,
            False,
            _UUID,
            _AT,
            _NAIVE,
            RecordID("t", 1),
            RecordID("t", "k"),
            CBORTag(99, [timedelta(1)]),
            CBORSimpleValue(22),
            _NULL,
            undefined,
        ],
        ids=repr,
    )
    def test_fast_path_unchanged(self, value: Any) -> None:
        assert type(value) in encoding._PASSTHROUGH
        assert _encode(value) is value

    @pytest.mark.parametrize("value", [0, 1, -1, 2**63 - 1, -(2**63), True, _Level.HIGH], ids=repr)
    def test_int64(self, value: Any) -> None:
        assert _encode(value) is value

    @pytest.mark.parametrize("value", [2**63, -(2**63) - 1, 2**70, -(2**70)], ids=repr)
    def test_int64_refused(self, value: int) -> None:
        with pytest.raises(ValueError, match="signed 64-bit") as info:
            _encode(value)
        assert type(info.value) is ValueError

    @pytest.mark.parametrize("text", ["NaN", "-NaN", "sNaN", "Infinity", "-Infinity"])
    def test_decimal_non_finite_unchanged(self, text: str) -> None:
        value = Decimal(text)
        assert _encode(value) is value

    @pytest.mark.parametrize(
        "value",
        [
            Decimal("12.50"),
            Decimal("-0"),
            Decimal("0.05") / 3,
            Decimal("79228162514264337593543950335.4"),
            Decimal("-79228162514264337593543950335"),
            Decimal("1.000000000000000000000000000000"),
            Decimal("0.000001"),
        ],
        ids=str,
    )
    def test_decimal_without_exponent_unchanged(self, value: Decimal) -> None:
        assert "E" not in str(value)
        assert _encode(value) is value

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("1E+3", "1000"),
            ("1E-7", "0.0000001"),
            ("-5E-7", "-0.0000005"),
            ("0E-10", "0.0000000000"),
            ("-0E-10", "-0.0000000000"),
            ("7E+28", "70000000000000000000000000000"),
            ("1E-31", _ZEROS_29),
            ("1.234567890123456789012345E-7", "0.00000012345678901234567890123"),
            ("-1.234567890123456789012345E-7", "-0.00000012345678901234567890123"),
            ("1E-29", "0.00000000000000000000000000001"),
            ("1000E-12", "0.000000001000"),
        ],
    )
    def test_decimal_exponent_texts(self, text: str, expected: str) -> None:
        out = _encode(Decimal(text))
        assert type(out) is CBORTag
        assert out == CBORTag(_DEC, expected)

    def test_decimal_tiny_exponent_is_fast(self) -> None:
        started = time.perf_counter()
        out = _encode(Decimal("1E-1000000"))
        assert time.perf_counter() - started < 0.1
        assert out == CBORTag(_DEC, _ZEROS_29)

    @pytest.mark.parametrize(
        "text",
        ["8E+28", "1E+999999999", "79228162514264337593543950335.5", "-79228162514264337593543950336", "1E+29"],
    )
    def test_decimal_refusals(self, text: str) -> None:
        with pytest.raises(ValueError, match=r"2\*\*96 - 1") as info:
            _encode(Decimal(text))
        assert type(info.value) is ValueError

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (Decimal("0E+29"), "0"),
            (Decimal("0E+40"), "0"),
            (Decimal(0) * Decimal("1E+30"), "0"),
            (Decimal("-0E+29"), "-0"),
            (Decimal("0E-30"), _ZEROS_29),
            (Decimal("-0E-30"), "-" + _ZEROS_29),
            (Decimal("0E+999999999"), "0"),
        ],
        ids=str,
    )
    def test_decimal_zeros_never_refused(self, value: Decimal, expected: str) -> None:
        assert _encode(value) == CBORTag(_DEC, expected)

    def test_decimal_zero_with_a_huge_negative_exponent_is_fast(self) -> None:
        started = time.perf_counter()
        out = _encode(Decimal("0E-1000000"))
        assert time.perf_counter() - started < 0.1
        assert out == CBORTag(_DEC, _ZEROS_29)

    @pytest.mark.parametrize("text", ["0", "-0", "0.000000"])
    def test_decimal_plain_zeros_unchanged(self, text: str) -> None:
        value = Decimal(text)
        assert _encode(value) is value

    def test_decimal_ignores_caller_context(self) -> None:
        seventy_digits = Decimal("79228162514264337593543950335.4" + "9" * 40)
        with decimal.localcontext() as ctx:
            ctx.clear_flags()  # ``localcontext()`` copies the flags already raised in this thread
            ctx.traps[decimal.Inexact] = True
            ctx.traps[decimal.Rounded] = True
            ctx.capitals = 0
            ctx.prec = 3
            assert _encode(Decimal("1E+3")) == CBORTag(_DEC, "1000")
            assert _encode(Decimal("1.234567890123456789012345E-7")) == CBORTag(_DEC, "0.00000012345678901234567890123")
            assert _encode(Decimal("0E-30")) == CBORTag(_DEC, _ZEROS_29)
            assert _encode(seventy_digits) is seventy_digits  # no false refusal (``copy_abs()``)
            with pytest.raises(ValueError, match=r"2\*\*96 - 1"):
                _encode(Decimal("79228162514264337593543950335.5"))
            assert ctx.flags[decimal.Inexact] is False
            assert ctx.flags[decimal.Rounded] is False

    def test_decimal_notation_matches_str(self) -> None:
        """Returned unchanged exactly when ``str()`` writes no exponent (default context)."""
        values = [Decimal(text) for text in _DECIMAL_FIXED] + _decimal_corpus(20_000)
        checked = 0
        with decimal.localcontext(decimal.Context()):
            for value in values:
                try:
                    out = _encode(value)
                except ValueError:
                    continue
                checked += 1
                assert (out is value) == ("E" not in str(value)), str(value)
                if out is not value:
                    assert type(out) is CBORTag and out.tag == _DEC
                    assert "E" not in out.value and "e" not in out.value
                    assert Decimal(out.value) == value or len(out.value.partition(".")[2]) == 29
        assert checked > 10_000

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (timedelta(hours=1, microseconds=1), [3600, 1000]),
            (_SubTimedelta(minutes=1), [60, 0]),
            (timedelta.max, [86_399_999_999_999, 999_999_000]),
            (Duration(1), [0, 1]),
            (Duration(5400 * 10**9 + 1), [5400, 1]),
            (surrealdb.Duration(10**18 - 1), [999_999_999, 999_999_999]),
            (Duration(MAX_NS), [2**64 - 1, 999_999_999]),
            (surrealdb.Duration(MAX_NS), [2**64 - 1, 999_999_999]),
        ],
        ids=repr,
    )
    def test_duration_rows(self, value: Any, expected: list[int]) -> None:
        out = _encode(value)
        assert type(out) is CBORTag
        assert out == CBORTag(_DUR, expected)

    @pytest.mark.parametrize(
        ("value", "match"),
        [
            (surrealdb.Duration(2**64 * 10**9), r"2\*\*64 - 1 seconds"),
            (surrealdb.Duration(MAX_NS + 1), r"2\*\*64 - 1 seconds"),
            (timedelta(seconds=-1), "negative duration"),
            (timedelta(microseconds=-1), "negative duration"),
            (surrealdb.Duration(-1), "negative duration"),
            (surrealdb.Duration(1.5), "int number of nanoseconds"),
            (surrealdb.Duration(True), "int number of nanoseconds"),
            (Duration._unchecked(-5), "negative duration"),
            (Duration._unchecked("5"), "int number of nanoseconds"),
        ],
        ids=repr,
    )
    def test_duration_refusals(self, value: Any, match: str) -> None:
        with pytest.raises(ValueError, match=match) as info:
            _encode(value)
        assert type(info.value) is ValueError

    @pytest.mark.parametrize("zero", [timedelta(0), Duration(0), surrealdb.Duration(0)], ids=repr)
    @pytest.mark.parametrize("probe", ["real", "true"])
    def test_zero_duration_follows_probe(self, zero: Any, probe: str, monkeypatch: pytest.MonkeyPatch) -> None:
        if probe == "true":
            monkeypatch.setattr(encoding, "sdk_decodes_zero_duration", lambda: True)
            assert _encode(zero) == CBORTag(_DUR, [0, 0])
            return
        if encoding.sdk_decodes_zero_duration():
            pytest.skip("the installed SDK reads a zero duration back: nothing is refused")
        with pytest.raises(ValueError, match="zero duration") as info:
            _encode(zero)
        assert type(info.value) is ValueError

    def test_date_rows(self) -> None:
        out = _encode(date(2026, 1, 2))
        assert type(out) is datetime
        assert out == _MIDNIGHT
        assert out.tzinfo is UTC
        sub = _encode(_SubDate(2026, 1, 2))
        assert type(sub) is datetime
        assert sub == _MIDNIGHT
        aware = datetime(2026, 1, 2, tzinfo=timezone(timedelta(hours=2)))
        sub_dt = _SubDateTime(2026, 1, 2, 3, tzinfo=UTC)
        for value in (_AT, _NAIVE, aware, sub_dt):
            assert _encode(value) is value

    def test_set_rows(self) -> None:
        assert _encode({"b", "a"}) == ["a", "b"]
        assert _encode(frozenset({2, 1})) == [1, 2]
        assert _encode({timedelta(hours=2), timedelta(hours=1)}) == [CBORTag(_DUR, [3600, 0]), CBORTag(_DUR, [7200, 0])]
        mixed = _encode({1, "a"})
        assert type(mixed) is list
        assert sorted(mixed, key=repr) == sorted([1, "a"], key=repr)
        nan = Decimal("NaN")
        with_nan = _encode({nan, Decimal(1)})
        assert type(with_nan) is list
        assert len(with_nan) == 2
        assert any(item is nan for item in with_nan)
        assert Decimal(1) in with_nan
        assert _encode(set()) == []
        assert type(_encode(frozenset())) is list
        assert _encode({date(2026, 1, 2)}) == [_MIDNIGHT]

    @pytest.mark.parametrize(
        ("value", "begin", "end"),
        [
            (Range(BoundIncluded(1), BoundExcluded(5)), BoundIncluded(1), BoundExcluded(5)),
            (Range(BoundExcluded(1), BoundIncluded(5)), BoundExcluded(1), BoundIncluded(5)),
            (Range(None, BoundExcluded(5)), None, BoundExcluded(5)),
            (Range(BoundIncluded(1)), BoundIncluded(1), None),
            (Range(), None, None),
        ],
        ids=repr,
    )
    def test_range_rows(self, value: Range[Any], begin: Any, end: Any) -> None:
        """An ORM ``Range`` becomes an SDK ``Range``; an open side is the CBOR null, sent as ``f6``."""
        out = _encode(value)
        assert type(out) is SdkRange
        for side, expected in ((out.begin, begin), (out.end, end)):
            if expected is None:
                assert side is _NULL
            else:
                assert type(side) is type(expected)
                assert side == expected
        data = _assert_open_side_bytes(out)
        if value == Range(None, BoundExcluded(5)):
            assert data == _OPEN_TO_FIVE
        if value == Range():
            assert data == bytes.fromhex("d83182f6f6")
        assert _encode(out) is out

    def test_range_rows_keep_the_bound_objects(self) -> None:
        low, high = BoundIncluded(1), BoundExcluded(5)
        out = _encode(Range(low, high))
        assert out.begin is low
        assert out.end is high

    @pytest.mark.parametrize("marker", [None, Bound(), undefined], ids=["None", "Bound()", "undefined"])
    def test_range_rows_sdk_open_markers_become_the_cbor_null(self, marker: Any) -> None:
        end = BoundExcluded(5)
        value = SdkRange(marker, end)
        out = _encode(value)
        assert type(out) is SdkRange
        assert out is not value
        assert out.begin is _NULL
        assert out.end is end
        assert _assert_open_side_bytes(out) == _OPEN_TO_FIVE
        assert value.begin is marker  # the input is never mutated
        assert _encode(out) is out

    def test_range_rows_sdk_unchanged_is_kept(self) -> None:
        open_begin = SdkRange(_NULL, BoundExcluded(5))
        assert _encode(open_begin) is open_begin
        null = CBORSimpleValue(22)
        open_end = SdkRange(BoundIncluded(1), null)
        assert _encode(open_end) is open_end
        assert _encode(open_end).end is null  # the caller's CBOR null, not swapped for the constant
        low, high = BoundIncluded(1), BoundIncluded(5)
        closed = SdkRange(low, high)
        assert _encode(closed) is closed
        assert closed.begin is low and closed.end is high
        both = SdkRange(_NULL, _NULL)
        assert _encode(both) is both

    def test_range_rows_bound_values_are_encoded(self) -> None:
        value = SdkRange(BoundIncluded(timedelta(hours=1)), undefined)
        out = _encode(value)
        assert type(out.begin) is BoundIncluded
        assert out.begin.value == CBORTag(_DUR, [3600, 0])
        assert out.end is _NULL
        assert type(value.begin.value) is timedelta  # untouched
        orm = _encode(Range(BoundIncluded(timedelta(minutes=1)), BoundExcluded(Duration.parse("1h"))))
        assert orm == SdkRange(BoundIncluded(CBORTag(_DUR, [60, 0])), BoundExcluded(CBORTag(_DUR, [3600, 0])))
        dates = _encode(Range(BoundIncluded(date(2026, 1, 2)), BoundExcluded(_SubDate(2026, 2, 1))))
        assert type(dates.begin.value) is datetime and dates.begin.value == _MIDNIGHT
        assert type(dates.end) is BoundExcluded and dates.end.value == datetime(2026, 2, 1, tzinfo=UTC)
        decimals = _encode(Range(BoundIncluded(Decimal("1E-7")), None))
        assert decimals.begin == BoundIncluded(CBORTag(_DEC, "0.0000001"))
        assert decimals.end is _NULL
        for out_value in (out, orm, dates, decimals):
            assert _encode(out_value) is out_value

    def test_range_rows_bound_value_refusals_propagate(self) -> None:
        with pytest.raises(ValueError, match="negative duration"):
            _encode(Range(BoundIncluded(timedelta(seconds=-1)), None))
        with pytest.raises(ValueError, match="signed 64-bit"):
            _encode(SdkRange(BoundIncluded(2**63), _NULL))

    @pytest.mark.parametrize("side", ["x", 22, CBORSimpleValue(20), 1.5, BoundIncluded], ids=repr)
    def test_range_rows_bad_sides(self, side: Any) -> None:
        for value in (SdkRange(side, BoundExcluded(5)), SdkRange(BoundIncluded(1), side), Range._unchecked(side, None)):
            with pytest.raises(ValueError, match="Range bound") as info:
                _encode(value)
            assert type(info.value) is ValueError

    def test_range_rows_cbor_null_alone(self) -> None:
        null = CBORSimpleValue(22)
        assert _encode(null) is null
        assert _encode(_NULL) is _NULL

    def test_containers_copy_on_write(self) -> None:
        untouched = [1, "a", _AT]
        inner: list[Any] = [untouched, {"c": timedelta(hours=1)}]
        value = {"a": 1, "u": untouched, "b": inner}
        out = _encode(value)
        assert type(out) is dict
        assert out is not value
        assert list(out) == ["a", "u", "b"]
        assert out["a"] is value["a"]
        assert out["u"] is untouched
        assert out["b"] is not inner
        assert type(out["b"]) is list
        assert out["b"][0] is untouched
        assert out["b"][1]["c"] == CBORTag(_DUR, [3600, 0])
        assert value["b"][1]["c"] == timedelta(hours=1)  # input untouched
        same = {"a": [1, {"b": (2, "c")}], "d": None}
        assert _encode(same) is same
        pair = _encode((1, date(2026, 1, 2)))
        assert type(pair) is tuple and pair == (1, _MIDNIGHT)
        named = _encode(_Pair(1, date(2026, 1, 2)))
        assert type(named) is tuple and named == (1, _MIDNIGHT)
        unchanged_named = _Pair(1, 2)
        assert _encode(unchanged_named) is unchanged_named
        sub = _SubDict(z=1, y=date(2026, 1, 2))
        sub_out = _encode(sub)
        assert type(sub_out) is dict and list(sub_out) == ["z", "y"] and sub_out["y"] == _MIDNIGHT
        unchanged_sub = _SubDict(z=1)
        assert _encode(unchanged_sub) is unchanged_sub
        sub_list = _encode(_SubList([timedelta(seconds=1)]))
        assert type(sub_list) is list and sub_list == [CBORTag(_DUR, [1, 0])]
        assert _encode([]) == [] and _encode({}) == {} and _encode(()) == ()

    @pytest.mark.parametrize(
        "value",
        [CBORTag(99, [timedelta(1)]), RecordID("t", 2**70), RecordID("t", [date(2026, 1, 2)]), dt_time(1), object()],
        ids=repr,
    )
    def test_not_descended(self, value: Any) -> None:
        assert _encode(value) is value

    def test_cbor_simple_value_subclass(self) -> None:
        s = _SubSimple(22)
        assert type(s) is not CBORSimpleValue
        assert not isinstance(s, int)
        assert type(s) not in encoding._PASSTHROUGH
        assert _encode(s) is s  # the last row: unchanged
        r = SdkRange(BoundIncluded(1), s)
        assert _encode(r) is r
        assert _encode(r).end is s  # kept, never swapped for the constant
        assert _sdk.cbor_encode(_encode(SdkRange(_SubSimple(22), BoundExcluded(5)))) == _OPEN_TO_FIVE
        orm = Range(BoundIncluded(1), s)
        assert orm.end is None
        out = _encode(orm)
        assert type(out) is SdkRange
        assert out.end is _NULL
        other = _SubSimple(20)
        with pytest.raises(ValueError, match="Range bound"):
            _encode(SdkRange(other, BoundExcluded(5)))

    def test_messages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The refusal texts, verbatim."""
        cases: list[tuple[Any, str]] = [
            (
                timedelta(seconds=-1),
                "Cannot send a negative duration (datetime.timedelta(days=-1, seconds=86399)): "
                "SurrealDB durations are never negative.",
            ),
            (
                surrealdb.Duration(2**64 * 10**9),
                f"Cannot send Duration(elapsed={2**64 * 10**9}): a SurrealDB duration holds at most 2**64 - 1 seconds.",
            ),
            (
                surrealdb.Duration(1.5),
                "Cannot send Duration(elapsed=1.5): a duration must hold an int number of nanoseconds.",
            ),
            (
                Decimal("8E+28"),
                "Cannot send Decimal('8E+28'): its integer part, rounded, exceeds 2**96 - 1, the largest SurrealDB decimal.",
            ),
            (
                2**63,
                "Cannot send 9223372036854775808: SurrealDB integers are signed 64-bit (-2**63 .. 2**63 - 1); "
                "use Decimal for larger numbers.",
            ),
            (
                SdkRange("x", BoundExcluded(5)),
                "Cannot send a Range bound of type str: use BoundIncluded(value), BoundExcluded(value) or None "
                "for an open side.",
            ),
        ]
        for value, message in cases:
            with pytest.raises(ValueError) as info:
                _encode(value)
            assert str(info.value) == message
        monkeypatch.setattr(encoding, "sdk_decodes_zero_duration", lambda: False)
        with pytest.raises(ValueError) as info:
            _encode(Duration(0))
        assert str(info.value) == (
            "Cannot send a zero duration (Duration(elapsed=0)): the installed surrealdb SDK cannot read a zero "
            "duration back. Store None instead, or filter with __gte=Duration(1)."
        )
        assert "connection" not in str(info.value)

    def test_constants(self) -> None:
        assert encoding.INT64_MIN == -(2**63)
        assert encoding.INT64_MAX == 2**63 - 1
        assert encoding.DECIMAL_INTEGER_MAX == 2**96 - 1
        assert encoding.DURATION_SECONDS_LIMIT == 2**64
        assert encoding.DECIMAL_TEXT_SCALE == 29
        assert encoding.DECIMAL_TEXT_STEP.as_tuple() == (0, (1,), -29)
        assert encoding.__all__ == ["encode", "sdk_decodes_zero_duration"]


# --- properties ---------------------------------------------------------------------------------


def _v0_22_2_corpus() -> list[Any]:
    """Every value kind v0.22.2 could hand the SDK (spec §9.1)."""
    return [
        None,
        True,
        False,
        0,
        2**63 - 1,
        -(2**63 - 1),
        -(2**63),
        1.5,
        float("nan"),
        float("inf"),
        float("-inf"),
        "",
        "text",
        b"",
        b"\x00\x01\xff",
        _UUID,
        _AT,
        _NAIVE,
        Decimal("12.50"),
        Decimal("-0"),
        Decimal("0"),
        Decimal("0.05") / 3,
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal("79228162514264337593543950335"),
        RecordID("t", "k"),
        RecordID("t", 1),
        RecordID("t", [1, "a"]),
        RecordID("t", {"a": 1}),
        CBORTag(_DEC, "1.5"),
        CBORTag(_DUR, [5400, 1]),
        CBORSimpleValue(22),
        _NULL,
        undefined,
        SdkRange(BoundIncluded(1), BoundExcluded(5)),
        SdkRange(BoundExcluded(-3), BoundIncluded(3)),
        SdkRange(_NULL, BoundExcluded(5)),
        SdkRange(BoundIncluded(1), CBORSimpleValue(22)),
    ]


def _encodable_outputs() -> list[Any]:
    """One non-error input per row; ``encode`` of each is checked for idempotence."""
    return [
        *_v0_22_2_corpus(),
        2**63 - 1,
        _Level.HIGH,
        Decimal("1E+3"),
        Decimal("1E-7"),
        Decimal("-0E-30"),
        Decimal("0E+40"),
        Decimal("1.234567890123456789012345E-7"),
        timedelta(hours=1, microseconds=1),
        Duration(MAX_NS),
        surrealdb.Duration(5400 * 10**9 + 1),
        date(2026, 1, 2),
        _SubDate(2026, 1, 2),
        {"b", "a"},
        frozenset({2, 1}),
        {timedelta(hours=2), timedelta(hours=1)},
        {1, "a"},
        {Decimal("NaN"), Decimal(1)},
        Range(BoundIncluded(1), BoundExcluded(5)),
        Range(None, BoundExcluded(5)),
        Range(),
        Range(BoundIncluded(timedelta(minutes=1)), BoundExcluded(Duration.parse("1h"))),
        SdkRange(None, BoundExcluded(5)),
        SdkRange(Bound(), BoundExcluded(5)),
        SdkRange(BoundIncluded(timedelta(hours=1)), undefined),
        SdkRange(BoundIncluded(1), _SubSimple(22)),
        _SubSimple(22),
        {"a": 1, "b": [1, {"c": timedelta(hours=1)}]},
        (1, date(2026, 1, 2)),
        _Pair(1, date(2026, 1, 2)),
        _SubDict(z=1, y=date(2026, 1, 2)),
        [{"deep": ({"set": {"b", "a"}},)}],
    ]


class TestEncodeProperties:
    def test_v0_22_2_corpus_identity(self) -> None:
        corpus = _v0_22_2_corpus()
        nested = {"all": list(corpus), "tuple": tuple(corpus), "deep": [{"x": tuple(corpus), "y": [list(corpus)]}]}
        for value in [*corpus, nested, list(corpus), tuple(corpus)]:
            out = _encode(value)
            assert out is value, repr(value)
            assert _sdk.cbor_encode(out) == _sdk.cbor_encode(value)
        out = _encode(nested)
        assert out["all"] is nested["all"]
        assert out["deep"][0]["x"] is nested["deep"][0]["x"]
        for original, item in zip(corpus, out["deep"][0]["y"][0], strict=True):
            assert item is original

    @pytest.mark.parametrize("value", _encodable_outputs(), ids=repr)
    def test_idempotent(self, value: Any) -> None:
        out = _encode(value)
        assert _encode(out) is out

    def test_never_mutates(self) -> None:
        value = {
            "tags": {"b", "a"},
            "frozen": frozenset({2, 1}),
            "day": date(2026, 1, 2),
            "ttl": timedelta(hours=1),
            "grace": Duration.parse("1h30m1ns"),
            "sdk": surrealdb.Duration(7),
            "hours": Range(BoundIncluded(9), BoundExcluded(17)),
            "open": SdkRange(None, BoundIncluded(timedelta(minutes=1))),
            "price": Decimal("1E+3"),
            "list": [date(2026, 1, 3), {"inner": {timedelta(seconds=1)}}],
            "tuple": (Range(), _SubDate(2026, 1, 4)),
        }
        snapshot = copy.deepcopy(value)
        sides = (value["open"].begin, value["open"].end)
        _encode(value)
        assert value == snapshot
        assert type(value["tags"]) is set and type(value["frozen"]) is frozenset
        assert type(value["day"]) is date
        assert value["open"].begin is sides[0] and value["open"].end is sides[1]
        assert type(value["open"].end.value) is timedelta
        assert type(value["list"][1]["inner"]) is set


# --- the local zero-duration probe ----------------------------------------------------------------


class TestZeroProbe:
    def test_cached(self) -> None:
        probe = encoding.sdk_decodes_zero_duration
        probe.cache_clear()
        try:
            first = probe()
            second = probe()
            info = probe.cache_info()
            assert (info.misses, info.hits) == (1, 1)
            assert first is second
            assert isinstance(first, bool)
        finally:
            probe.cache_clear()

    @pytest.mark.parametrize("name", ["cbor_decode", "cbor_encode"])
    def test_false_without_codec(self, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
        probe = encoding.sdk_decodes_zero_duration
        probe.cache_clear()
        try:
            monkeypatch.setattr(_sdk, name, None)
            assert probe() is False
        finally:
            monkeypatch.undo()
            probe.cache_clear()

    def test_false_when_decode_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(data: bytes) -> Any:
            raise RuntimeError("cannot decode")

        probe = encoding.sdk_decodes_zero_duration
        probe.cache_clear()
        try:
            monkeypatch.setattr(_sdk, "cbor_decode", boom)
            assert probe() is False
        finally:
            monkeypatch.undo()
            probe.cache_clear()

    def test_true_when_the_codec_reads_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        probe = encoding.sdk_decodes_zero_duration
        probe.cache_clear()
        try:
            monkeypatch.setattr(_sdk, "cbor_decode", lambda data: surrealdb.Duration(0))
            assert probe() is True
        finally:
            monkeypatch.undo()
            probe.cache_clear()
