"""``encoding.encode()``: the value-driven boundary between the ORM and the SDK (v0.23.0).

Unit tests, no database: one test per row of ``encode`` with its edges, the properties the hand-off
relies on (identity on every value v0.22.2 could send, idempotence, no mutation of the input) and
the local zero-duration probe. The raw-SDK round trips of the encoded forms, over WebSocket and
HTTP on both server lines, live in ``tests/test_rich_types.py``.

The hand-off itself — every SDK call of ``src/`` given ``_encoding.encode(…)`` of its payload, and
never inside a ``try`` that would re-wrap the ``ValueError`` of a refused value — is checked here
by an AST scan of the package (``TestBoundary``) and by fakes that stand in for the client
(``TestHandOff``). The ORM-level round trips are in ``tests/test_rich_fields.py``.
"""

import ast
import contextlib
import copy
import dataclasses
import decimal
import pathlib
import re
import subprocess
import sys
import textwrap
import time
from collections.abc import AsyncIterator, Callable, Coroutine
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

import surreal_orm_lite
from surreal_orm_lite import (
    AuthenticatedUserMixin,
    BaseSurrealModel,
    BoundExcluded,
    BoundIncluded,
    BufferedTransaction,
    Duration,
    InteractiveTransaction,
    Range,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    _sdk,
    encoding,
)
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
            # ``not``, never ``is False``: the pure-Python ``decimal`` keeps its flags as 0/1.
            assert not ctx.flags[decimal.Inexact]
            assert not ctx.flags[decimal.Rounded]

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


# --- the pure-Python ``decimal`` ------------------------------------------------------------------


class TestPurePythonDecimal:
    """The decimal unit tests also hold where ``decimal`` is ``_pydecimal``, the pure-Python fallback.

    CPython falls back to it when its ``_decimal`` C module is missing, as on the CI's Python 3.15
    runners. Its context flags are ``0``/``1``, not ``False``/``True``, and its ``Decimal(str)`` goes
    through ``int()``, so it refuses a coefficient beyond Python's 4 300-digit int-string limit. A
    local interpreter usually has ``_decimal``: this runs the unit tests that name ``decimal`` once
    more, in a fresh interpreter where ``_decimal`` cannot be imported.
    """

    def test_decimal_unit_tests_pass_on_the_pure_python_decimal(self) -> None:
        if sys.modules.get("_decimal") is None:
            pytest.skip("this interpreter already runs the pure-Python decimal: the tests themselves are the check")
        script = """
            import sys

            sys.modules["_decimal"] = None  # ``import decimal`` falls back to ``_pydecimal``
            import _pydecimal
            import decimal

            assert decimal.Decimal is _pydecimal.Decimal
            import pytest

            sys.exit(pytest.main(["--no-cov", "-p", "no:cacheprovider", "-k", "decimal and not E2E", "tests"]))
        """
        result = subprocess.run(
            [sys.executable, "-I", "-c", textwrap.dedent(script)],
            cwd=pathlib.Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-2000:]
        assert re.search(r"\b\d+ passed\b", result.stdout), result.stdout[-2000:]


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


# --- the hand-off: every SDK call of src/ goes through encode() (spec §6.2) ----------------------

#: SDK client methods that carry data to the server.
_SDK_METHODS = frozenset(
    {"query", "query_raw", "create", "update", "upsert", "merge", "patch", "insert", "insert_relation"}
    | {"signin", "signup", "run", "let"}
)
#: Their payload is the first positional argument; for every other method, the second.
_AUTH_METHODS = frozenset({"signin", "signup"})
#: Receivers that call one of those names but are not SDK clients, reviewed at ``cc2cfa3``: dict
#: ``.update()`` calls (``collected``, ``all_variables``, ``self._variables``, ``variables``,
#: ``self._annotations``) and ``obj.merge()`` on a model instance (``QuerySet.bulk_update``).
#: Extend it only after checking that the new receiver is not an SDK client.
_NON_SDK_RECEIVERS = frozenset({"collected", "all_variables", "self._variables", "variables", "self._annotations", "obj"})
#: A handler naming one of these catches a plain ``ValueError``.
_CATCHES_VALUE_ERROR = frozenset({"Exception", "BaseException", "ValueError"})
_ENCODE = "_encoding.encode"
_CLIENT = re.compile(r"client$")
_TX = re.compile(r"(^|[._])tx$")
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


@dataclasses.dataclass
class _BoundaryReport:
    """What the checker found in one module (or, summed, in the package)."""

    offenders: list[str] = dataclasses.field(default_factory=list)
    inline: int = 0
    named: int = 0
    bare: int = 0

    def add(self, other: "_BoundaryReport") -> None:
        self.offenders.extend(other.offenders)
        self.inline += other.inline
        self.named += other.named
        self.bare += other.bare


def _is_encode_call(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Call) and ast.unparse(node.func) == _ENCODE


def _is_bare(node: ast.AST | None) -> bool:
    return node is None or (isinstance(node, ast.Dict) and not node.keys)


def _names_a_value_error_base(expr: ast.expr) -> bool:
    name = expr.id if isinstance(expr, ast.Name) else expr.attr if isinstance(expr, ast.Attribute) else None
    return name in _CATCHES_VALUE_ERROR


def _catches_value_error(handler_type: ast.expr | None) -> bool:
    """A bare ``except:``, or a type (alone or in a tuple) that a plain ``ValueError`` matches."""
    if handler_type is None:
        return True
    items = handler_type.elts if isinstance(handler_type, ast.Tuple) else [handler_type]
    return any(_names_a_value_error_base(item) for item in items)


def _suppresses_value_error(item: ast.withitem) -> bool:
    call = item.context_expr
    return (
        isinstance(call, ast.Call)
        and ast.unparse(call.func) in {"suppress", "contextlib.suppress"}
        and any(_catches_value_error(arg) for arg in call.args)
    )


def _guard_around(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    """The innermost ``try`` body (or ``suppress`` block) around *node* that swallows a ``ValueError``."""
    child, parent = node, parents.get(node)
    while parent is not None and not isinstance(parent, _FUNCTIONS):
        in_body = any(child is statement for statement in getattr(parent, "body", ()))
        catching = isinstance(parent, ast.Try | ast.TryStar) and any(
            _catches_value_error(handler.type) for handler in parent.handlers
        )
        suppressing = isinstance(parent, ast.With | ast.AsyncWith) and any(
            _suppresses_value_error(item) for item in parent.items
        )
        if in_body and (catching or suppressing):
            return parent
        child, parent = parent, parents.get(parent)
    return None


def _binds(target: ast.AST, name: str) -> bool:
    if isinstance(target, ast.Name):
        return target.id == name
    if isinstance(target, ast.Tuple | ast.List):
        return any(_binds(element, name) for element in target.elts)
    if isinstance(target, ast.Starred):
        return _binds(target.value, name)
    return False


def _end(node: ast.AST) -> tuple[int, int]:
    return (getattr(node, "end_lineno", 0) or 0, getattr(node, "end_col_offset", 0) or 0)


def _walk_scope(scope: ast.AST) -> list[ast.AST]:
    """Every node of *scope*, without descending into the functions or classes it defines."""
    found: list[ast.AST] = []
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        found.append(node)
        if not isinstance(node, _NESTED_SCOPES):
            pending.extend(ast.iter_child_nodes(node))
    return found


def _bindings(scope: ast.AST, name: str) -> list[tuple[tuple[int, int], ast.AST]]:
    """Every binding of *name* in *scope*, keyed by the position where it takes effect."""
    found: list[tuple[tuple[int, int], ast.AST]] = []
    if isinstance(scope, _SCOPES):
        args = scope.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
            if arg is not None and arg.arg == name:
                found.append(((arg.lineno, arg.col_offset), arg))
    for node in _walk_scope(scope):
        if isinstance(node, ast.Assign):
            targets: list[ast.AST] = list(node.targets)
        elif isinstance(node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
            targets = [node.target]
        else:
            targets = []
        if any(_binds(target, name) for target in targets):
            # An assignment takes effect once its whole statement has run.
            found.append((_end(node), node))
        elif isinstance(node, ast.For | ast.AsyncFor) and _binds(node.target, name):
            found.append((_end(node.target), node))
        elif isinstance(node, ast.withitem) and node.optional_vars is not None and _binds(node.optional_vars, name):
            found.append((_end(node.optional_vars), node))
        elif isinstance(node, ast.ExceptHandler) and node.name == name:
            found.append(((node.lineno, node.col_offset), node))
    return found


def _boundary_report(source: str, filename: str) -> _BoundaryReport:
    """Check one module against every rule of spec §6.2, with ``ast`` only.

    1. A *data call* is ``<R>.<m>(…)`` with ``m`` in ``_SDK_METHODS`` and ``R`` ending in
       ``client`` (payload: 1st argument for ``signin``/``signup``, else the 2nd), or
       ``getattr(<R>, …)(…)`` with ``R`` ending in ``client`` (every positional argument). It
       passes no ``*args`` and no keyword but ``txn_id=``.
    2. Each payload is **bare** (absent, or ``{}``), **inline** (``_encoding.encode(…)``) or an
       **encoded name** (a name whose last binding before the call, in the same function, is
       ``name = _encoding.encode(…)``).
    3. No ``_encoding.encode(…)`` sits in the body of a ``try`` catching ``ValueError`` (bare
       ``except:``, ``Exception``, ``BaseException``, ``ValueError``) or of a ``with suppress(…)``
       naming one of them: the refusal would be re-wrapped.
    4. A ``<R>.add``/``<R>.run_read`` call with ``R`` ending in ``tx`` inside such a ``try`` passes
       nothing, ``{}`` or an encoded name.
    5. A call of one of the methods on a receiver that does not end in ``client`` must be on a
       reviewed non-SDK receiver; a ``getattr(<R>, …)(…)`` call must be on a client.
    """
    tree = ast.parse(source, filename)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    report = _BoundaryReport()

    def offend(node: ast.AST, reason: str) -> None:
        report.offenders.append(f"{filename}:{getattr(node, 'lineno', 0)}: {reason}")

    def scope_of(node: ast.AST) -> ast.AST:
        parent = parents.get(node)
        while parent is not None and not isinstance(parent, _SCOPES):
            parent = parents.get(parent)
        return parent if parent is not None else tree

    def encoded_name(arg: ast.AST, call: ast.Call) -> bool:
        if not isinstance(arg, ast.Name):
            return False
        at = (call.lineno, call.col_offset)
        before = [binding for binding in _bindings(scope_of(call), arg.id) if binding[0] <= at]
        if not before:
            return False
        last = max(before, key=lambda binding: binding[0])[1]
        return isinstance(last, ast.Assign | ast.AnnAssign) and _is_encode_call(last.value)

    def well_formed(call: ast.Call, what: str) -> bool:
        ok = True
        if any(isinstance(arg, ast.Starred) for arg in call.args):
            offend(call, f"shape: {what} passes *args")
            ok = False
        for keyword in call.keywords:
            if keyword.arg != "txn_id":
                offend(call, f"shape: {what} passes the keyword {keyword.arg or '**'}= (only txn_id= is allowed)")
                ok = False
        return ok

    def check_payload(call: ast.Call, arg: ast.AST | None, what: str) -> None:
        if _is_bare(arg):
            report.bare += 1
        elif _is_encode_call(arg):
            report.inline += 1
        elif arg is not None and encoded_name(arg, call):
            report.named += 1
        else:
            offend(
                call,
                f"payload: {what} sends {ast.unparse(arg) if arg is not None else '?'!s}, which is neither bare, "
                f"{_ENCODE}(…) nor a name last assigned from {_ENCODE}(…)",
            )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if _is_encode_call(node):
            guard = _guard_around(node, parents)
            if guard is not None:
                offend(
                    node,
                    f"placement: {_ENCODE}(…) inside the try/suppress of line {getattr(guard, 'lineno', 0)}, "
                    "which catches ValueError: encode into a name before it",
                )
        elif isinstance(func, ast.Attribute):
            receiver = ast.unparse(func.value)
            what = f"{receiver}.{func.attr}()"
            if func.attr in _SDK_METHODS:
                if not _CLIENT.search(receiver):
                    if receiver not in _NON_SDK_RECEIVERS:
                        offend(node, f"guard: {what} is on a receiver that is not a client nor a reviewed non-SDK receiver")
                elif well_formed(node, what):
                    index = 0 if func.attr in _AUTH_METHODS else 1
                    check_payload(node, node.args[index] if len(node.args) > index else None, what)
            elif func.attr in {"add", "run_read"} and _TX.search(receiver) and _guard_around(node, parents) is not None:
                given = [kw.value for kw in node.keywords if kw.arg == "variables"]
                arg = node.args[1] if len(node.args) > 1 else given[0] if given else None
                starred = any(isinstance(a, ast.Starred) for a in node.args) or any(kw.arg is None for kw in node.keywords)
                if starred or not (_is_bare(arg) or (arg is not None and encoded_name(arg, node))):
                    offend(node, f"transaction: {what} inside a try catching ValueError must pass an encoded name")
        elif isinstance(func, ast.Call) and isinstance(func.func, ast.Name) and func.func.id == "getattr":
            receiver = ast.unparse(func.args[0]) if func.args else ""
            what = f"getattr({receiver}, …)()"
            if not _CLIENT.search(receiver):
                offend(node, f"guard: {what} is not on a client")
            elif well_formed(node, what):
                if not node.args:
                    report.bare += 1
                for arg in node.args:
                    check_payload(node, arg, what)
    return report


def _package_report() -> _BoundaryReport:
    report = _BoundaryReport()
    package = pathlib.Path(surreal_orm_lite.__file__).parent
    for path in sorted(package.glob("*.py")):
        report.add(_boundary_report(path.read_text(encoding="utf-8"), path.name))
    return report


def _report(snippet: str) -> _BoundaryReport:
    return _boundary_report(textwrap.dedent(snippet), "snippet.py")


_REJECTED = {
    "inline encode in try/except Exception": (
        """
        async def f(client, v):
            try:
                await client.query("q", _encoding.encode(v))
            except Exception as exc:
                raise RuntimeError() from exc
        """,
        "placement",
    ),
    "inline encode in except (KeyError, ValueError)": (
        """
        async def f(client, v):
            try:
                await client.query("q", _encoding.encode(v))
            except (KeyError, ValueError):
                pass
        """,
        "placement",
    ),
    "inline encode in a bare except": (
        """
        async def f(client, v):
            try:
                await client.query("q", _encoding.encode(v))
            except:
                pass
        """,
        "placement",
    ),
    "inline encode in contextlib.suppress(Exception)": (
        """
        async def f(client, v):
            with contextlib.suppress(Exception):
                await client.query("q", _encoding.encode(v))
        """,
        "placement",
    ),
    "a name last assigned from something else": (
        """
        async def f(client, x, y):
            payload = _encoding.encode(x)
            payload = y
            await client.query("q", payload)
        """,
        "payload",
    ),
    "a receiver that is not a client": (
        """
        async def f(v):
            await (await get_client()).query("q", _encoding.encode(v))
        """,
        "guard",
    ),
    "a connection that is not called client": (
        """
        async def f(self, v):
            await self._conn.query("q", _encoding.encode(v))
        """,
        "guard",
    ),
    "getattr on something else": (
        """
        async def f(conn, v):
            await getattr(conn, "query")("q", _encoding.encode(v))
        """,
        "guard",
    ),
    "a payload given by keyword": (
        """
        async def f(client, v):
            await client.query("q", vars=_encoding.encode(v))
        """,
        "shape",
    ),
    "*args": (
        """
        async def f(client, args):
            await client.query(*args)
        """,
        "shape",
    ),
    "an unwrapped signin payload": (
        """
        async def f(client):
            await client.signin({"a": 1})
        """,
        "payload",
    ),
    "an unencoded tx.add inside try/except Exception": (
        """
        async def f(tx, s, variables):
            try:
                await tx.add(s, variables)
            except Exception as exc:
                raise RuntimeError() from exc
        """,
        "transaction",
    ),
}

_ACCEPTED = {
    "inline outside any try": (
        """
        async def f(client, v):
            await client.query("q", _encoding.encode(v))
        """,
        (1, 0, 0),
    ),
    "inline in try/except ServerError": (
        """
        async def f(client, v):
            try:
                await client.query("q", _encoding.encode(v))
            except ServerError:
                pass
        """,
        (1, 0, 0),
    ),
    "an encoded name before try/except Exception": (
        """
        async def f(client, v):
            payload = _encoding.encode(v)
            try:
                await client.signin(payload)
            except Exception as exc:
                raise RuntimeError() from exc
        """,
        (0, 1, 0),
    ),
    "an empty dict": (
        """
        async def f(client):
            await client.query("q", {})
        """,
        (0, 0, 1),
    ),
    "no payload": (
        """
        async def f(client):
            await client.query("q")
        """,
        (0, 0, 1),
    ),
    "query_raw with txn_id": (
        """
        async def f(self, s, variables):
            payload = _encoding.encode(variables or {})
            try:
                return await self._client.query_raw(s, payload, txn_id=self._txn_id)
            except Exception as exc:
                raise RuntimeError() from exc
        """,
        (0, 1, 0),
    ),
    "getattr on a client": (
        """
        async def f(client, action, payload):
            payload = _encoding.encode(payload)
            try:
                return await getattr(client, action)(payload)
            except Exception as exc:
                raise RuntimeError() from exc
        """,
        (0, 1, 0),
    ),
    "an encoded tx.add inside try/except Exception": (
        """
        async def f(tx, s, variables):
            variables = _encoding.encode(variables)
            try:
                await tx.add(s, variables)
            except Exception as exc:
                raise RuntimeError() from exc
        """,
        (0, 0, 0),
    ),
    "a dict update": (
        """
        def f(collected, x):
            collected.update(x)
        """,
        (0, 0, 0),
    ),
}


class TestBoundary:
    """Spec §6.2 (AC-13): the AST scan of ``src/surreal_orm_lite/*.py``."""

    def test_every_sdk_call_is_encoded(self) -> None:
        report = _package_report()
        assert report.offenders == [], "SDK calls outside the encoding boundary:\n" + "\n".join(report.offenders)
        # The inventory of spec §6.2 at cc2cfa3: the scan is not vacuous.
        counts = f"inline={report.inline} named={report.named} bare={report.bare}"
        assert report.inline >= 22, counts
        assert report.named >= 8, counts
        assert report.inline + report.named >= 30, counts
        assert report.bare >= 6, counts

    @pytest.mark.parametrize(("snippet", "rule"), list(_REJECTED.values()), ids=list(_REJECTED))
    def test_checker_rejects(self, snippet: str, rule: str) -> None:
        offenders = _report(snippet).offenders
        assert offenders, "the checker accepted a form the boundary forbids"
        assert any(f": {rule}:" in offender for offender in offenders), offenders

    @pytest.mark.parametrize(("snippet", "counts"), list(_ACCEPTED.values()), ids=list(_ACCEPTED))
    def test_checker_accepts(self, snippet: str, counts: tuple[int, int, int]) -> None:
        report = _report(snippet)
        assert report.offenders == []
        assert (report.inline, report.named, report.bare) == counts

    def test_encode_is_never_imported_by_name(self) -> None:
        package = pathlib.Path(surreal_orm_lite.__file__).parent
        wrapping = []
        for path in sorted(package.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), path.name)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == "encoding":
                    assert not any(alias.name in {"encode", "*"} for alias in node.names), (
                        f"{path.name}:{node.lineno} imports encode by name: call it as {_ENCODE}(…)"
                    )
            if any(_is_encode_call(node) for node in ast.walk(tree)):
                wrapping.append(path.name)
                assert any(
                    isinstance(node, ast.ImportFrom)
                    and node.level == 1
                    and node.module is None
                    and any(alias.name == "encoding" and alias.asname == "_encoding" for alias in node.names)
                    for node in ast.walk(tree)
                ), f"{path.name} wraps payloads without 'from . import encoding as _encoding'"
        assert len(wrapping) >= 6, wrapping


# --- the hand-off at run time, with fakes standing in for the client ----------------------------

_NEG = timedelta(seconds=-1)


class _RecordingClient:
    """Records what reaches the SDK: ``query_raw`` answers like one successful statement."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any, Any]] = []

    async def query_raw(self, statement: str, variables: Any = None, txn_id: Any = None) -> Any:
        self.calls.append(("query_raw", statement, variables, txn_id))
        return {"result": [{"status": "OK", "result": []}]}

    async def query(self, statement: str, variables: Any = None, txn_id: Any = None) -> Any:
        self.calls.append(("query", statement, variables, txn_id))
        return []


class _UntouchableClient:
    """A client that must never be reached."""

    async def query(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("nothing may reach the client")

    async def query_raw(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("nothing may reach the client")


class _AuthUnit(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rf_unit", access_name="rf_unit")

    id: str | None = None
    email: str
    password: str
    grace: Duration | None = None


async def _refused(call: Callable[[], Coroutine[Any, Any, Any]]) -> None:
    """*call* raises a plain ``ValueError`` (never a subclass the ORM re-wrapped it into)."""
    with pytest.raises(ValueError, match="negative duration") as excinfo:
        await call()
    assert type(excinfo.value) is ValueError, f"re-wrapped into {type(excinfo.value).__name__}"


def _no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    async def get_client() -> Any:
        return _UntouchableClient()

    monkeypatch.setattr(SurrealDBConnectionManager, "get_client", staticmethod(get_client))


async def _call_function_without_tx(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_client(monkeypatch)
    await _refused(lambda: SurrealDBConnectionManager.call_function("fn::x", [_NEG]))


async def _call_function_buffered(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_client(monkeypatch)
    tx = BufferedTransaction(client=None)
    await _refused(lambda: SurrealDBConnectionManager.call_function("fn::x", [_NEG], tx=tx))
    assert tx.statements == [] and tx.variables == {}


async def _call_function_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_client(monkeypatch)
    fake = _RecordingClient()
    await _refused(lambda: SurrealDBConnectionManager.call_function("fn::x", [_NEG], tx=InteractiveTransaction(fake, "x")))
    assert fake.calls == []


async def _no_acquire(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("no client may be acquired")


async def _manager_signin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SurrealDBConnectionManager, "_acquire_client", staticmethod(_no_acquire))
    await _refused(lambda: SurrealDBConnectionManager.signin(access="a", variables={"g": _NEG}, namespace="ns", database="db"))


async def _manager_signup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SurrealDBConnectionManager, "_acquire_client", staticmethod(_no_acquire))
    await _refused(lambda: SurrealDBConnectionManager.signup(access="a", variables={"g": _NEG}, namespace="ns", database="db"))


@contextlib.asynccontextmanager
async def _no_ephemeral_client() -> AsyncIterator[Any]:
    raise AssertionError("no connection may be opened")
    yield  # pragma: no cover


async def _model_signup(monkeypatch: pytest.MonkeyPatch) -> None:
    SurrealDBConnectionManager.set_connection("ws://127.0.0.1:1/rpc", "root", "root", "ns", "db")  # no I/O
    try:
        monkeypatch.setattr(SurrealDBConnectionManager, "ephemeral_client", staticmethod(_no_ephemeral_client))
        await _refused(lambda: _AuthUnit.signup(email="a@b.c", password="pw", grace=_NEG))
    finally:
        await SurrealDBConnectionManager.unset_connection()


_REFUSAL_PATHS: dict[str, Callable[[pytest.MonkeyPatch], Coroutine[Any, Any, None]]] = {
    "call_function": _call_function_without_tx,
    "call_function-buffered-tx": _call_function_buffered,
    "call_function-interactive-tx": _call_function_interactive,
    "manager-signin": _manager_signin,
    "manager-signup": _manager_signup,
    "model-signup": _model_signup,
}


class TestHandOff:
    """Spec §5.7, §6.2 (AC-13, D11): encoded at the hand-off, refused before anything is sent."""

    @pytest.mark.asyncio
    async def test_buffered_add_encodes_at_entry(self) -> None:
        tx = BufferedTransaction(client=None)
        await tx.add("UPDATE $rid SET v = $v;", {"rid": RecordID("t", 1), "v": timedelta(hours=1)})
        assert tx.variables["t0_v"] == CBORTag(_DUR, [3600, 0])
        assert tx.variables["t0_rid"] == RecordID("t", 1)

        with pytest.raises(ValueError, match="negative duration") as excinfo:
            await tx.add("UPDATE $rid SET v = $v;", {"v": _NEG})
        assert type(excinfo.value) is ValueError
        assert len(tx.statements) == 1
        assert tx._counter == 1

    @pytest.mark.asyncio
    async def test_interactive_add_and_read_encode(self) -> None:
        fake = _RecordingClient()
        tx = InteractiveTransaction(fake, "x")
        await tx.add("UPDATE t:1 SET d = $d;", {"d": date(2026, 1, 2)})
        await tx.run_read("SELECT * FROM t WHERE s = $s;", {"s": {"b", "a"}})
        assert fake.calls == [
            ("query_raw", "UPDATE t:1 SET d = $d;", {"d": _MIDNIGHT}, "x"),
            ("query", "SELECT * FROM t WHERE s = $s;", {"s": ["a", "b"]}, "x"),
        ]
        assert type(fake.calls[0][2]["d"]) is datetime

        fake.calls.clear()
        await _refused(lambda: tx.add("UPDATE t:1 SET v = $v;", {"v": _NEG}))
        await _refused(lambda: tx.run_read("SELECT * FROM t WHERE v = $v;", {"v": _NEG}))
        assert fake.calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", list(_REFUSAL_PATHS))
    async def test_refusals_are_plain_value_errors(self, path: str, monkeypatch: pytest.MonkeyPatch) -> None:
        await _REFUSAL_PATHS[path](monkeypatch)
