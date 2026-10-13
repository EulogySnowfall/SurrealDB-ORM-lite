"""Rich field types (v0.23.0): the ORM's ``Duration`` and ``Range[T]`` and the wire helpers.

Unit tests first (no database); the raw-SDK E2E classes that pin what the servers do with each
wire form are suffixed ``E2E``.
"""

import copy
import dataclasses
import enum
import inspect
import json
import math
import os
import re
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Annotated, Any, ForwardRef, Literal, Optional, TypeVar, Union
from uuid import UUID

import aiohttp
import pytest
import surrealdb
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from surrealdb import TAG_DECIMAL_STRING, TAG_DURATION_COMPACT, AsyncSurreal, RecordID
from surrealdb.cbor import CBORTag, undefined
from surrealdb.data.cbor import encode as sdk_cbor_encode

import surreal_orm_lite
from surreal_orm_lite import (
    BoundExcluded,
    BoundIncluded,
    Computed,
    Duration,
    InvalidDurationError,
    Range,
    SurrealDBConnectionManager,
    SurrealFunc,
    _sdk,
    wire,
)
from surreal_orm_lite._sdk import Bound
from surreal_orm_lite.types import _parse_range_text
from surreal_orm_lite.wire import (
    MAX_DURATION_NS,
    decimal_text,
    duration_ns,
    encode_wire,
    field_input,
    restore_native,
    rich_kinds,
    sdk_decodes_zero_duration,
)
from tests.conftest import orm_client

MAX_NS = Duration.MAX_NANOSECONDS
SdkRange = surrealdb.Range
_T = TypeVar("_T")


def _assert_invalid_duration(call: Callable[[], Any]) -> None:
    """A value refusal: an ``InvalidDurationError``, so a ``ValueError`` and the SDK's class."""
    with pytest.raises(InvalidDurationError) as info:
        call()
    assert isinstance(info.value, ValueError)
    assert isinstance(info.value, surrealdb.InvalidDurationError)


def _assert_type_error(call: Callable[[], Any]) -> None:
    """A type refusal: a plain ``TypeError``, never an ``InvalidDurationError``."""
    with pytest.raises(TypeError) as info:
        call()
    assert not isinstance(info.value, surrealdb.InvalidDurationError)


class TestDuration:
    def test_construct_and_properties(self) -> None:
        d = Duration(5400 * 10**9)
        assert d.elapsed == 5400 * 10**9
        assert d.nanoseconds == 5400 * 10**9
        assert d.seconds == 5400
        assert d.minutes == 90
        assert d.hours == 1
        assert d.days == 0
        assert d.get_seconds_and_nano() == (5400, 0)
        assert Duration().elapsed == 0
        assert Duration() == Duration(0)
        assert repr(Duration(5)) == "Duration(elapsed=5)"

    def test_seconds_and_nano_is_exact(self) -> None:
        # The ORM's own values only: the SDK's float version is never called (spec D15).
        assert Duration(10**18 - 1).get_seconds_and_nano() == (999_999_999, 999_999_999)
        assert Duration(MAX_NS).get_seconds_and_nano() == (2**64 - 1, 999_999_999)

    @pytest.mark.parametrize(
        ("elapsed", "compact"),
        [
            (0, [0, 0]),
            (1, [0, 1]),
            (5400 * 10**9 + 1, [5400, 1]),
            (MAX_NS, [2**64 - 1, 999_999_999]),
        ],
    )
    def test_to_compact(self, elapsed: int, compact: list[int]) -> None:
        d = Duration(elapsed)
        assert d.to_compact() == compact
        assert type(d.to_compact()) is list
        assert d.to_compact() is not d.to_compact()
        assert Duration.parse(*d.to_compact()) == d

    def test_constructor_refusals(self) -> None:
        _assert_invalid_duration(lambda: Duration(-1))
        _assert_invalid_duration(lambda: Duration(MAX_NS + 1))
        _assert_type_error(lambda: Duration(True))
        _assert_type_error(lambda: Duration(1.5))  # type: ignore[arg-type]
        _assert_type_error(lambda: Duration("1h"))  # type: ignore[arg-type]
        assert Duration(MAX_NS).elapsed == MAX_NS

    @pytest.mark.parametrize(
        ("text", "expected_ns"),
        [
            ("1h30m", 5400 * 10**9),
            ("90m", 5400 * 10**9),
            ("1y2w3d4h5m6s7ms8us9ns", 33019506007008009),
            ("1µs", 1_000),
            ("1us", 1_000),
            ("30m1h", 5400 * 10**9),
            ("0ns", 0),
            # Surrounding whitespace is stripped, as the server does (spec §2.3, R3-3).
            (" 1h30m ", 5400 * 10**9),
            ("1h ", 3600 * 10**9),
            ("\t1h30m\n", 5400 * 10**9),
            ("\xa01h", 3600 * 10**9),
            # The 256-character cap is on the raw input: 254 spaces + "1h" is 256 characters.
            (" " * 254 + "1h", 3600 * 10**9),
        ],
    )
    def test_parse_valid(self, text: str, expected_ns: int) -> None:
        assert Duration.parse(text) == Duration(expected_ns)
        assert type(Duration.parse(text)) is Duration

    @pytest.mark.parametrize(
        "text",
        [
            "",
            " ",
            "1.5h",
            "1h foo",
            "1h 30m",
            " 1h 30m",
            "-1h",
            "1H",
            "h",
            "1",
            "٣h",  # Arabic-Indic digit three: ASCII digits only
            "​1h",  # zero-width space: not whitespace for str.strip()
            "x" * 257,
            " " * 255 + "1h",  # 257 raw characters: the cap applies before strip()
            "584942417356y",  # above MAX_NANOSECONDS
        ],
    )
    def test_parse_invalid(self, text: str) -> None:
        _assert_invalid_duration(lambda: Duration.parse(text))

    @pytest.mark.parametrize(
        "args",
        [(-1,), (0, -1), ("1h", -1), (2**64,), (2**64 - 1, 10**9)],
    )
    def test_parse_invalid_values(self, args: tuple[Any, ...]) -> None:
        _assert_invalid_duration(lambda: Duration.parse(*args))

    @pytest.mark.parametrize(
        "args",
        [(True,), (1.5,), (None,), (b"1h",), (1, True), (1, 1.5)],
    )
    def test_parse_invalid_types(self, args: tuple[Any, ...]) -> None:
        _assert_type_error(lambda: Duration.parse(*args))

    def test_parse_int_and_nanoseconds(self) -> None:
        assert Duration.parse(5) == Duration(5 * 10**9)
        assert Duration.parse(0) == Duration(0)
        assert Duration.parse(1, 5).elapsed == 1_000_000_005
        assert Duration.parse("1h", 5).elapsed == 3600 * 10**9 + 5
        assert Duration.parse(2**64 - 1, 999_999_999) == Duration(MAX_NS)

    @pytest.mark.parametrize(
        "args",
        [(5,), (1, 5), (0,), ("1h30m",), (" 1h30m ",), ("1h30m", 7), (2**64 - 1, 999_999_999)],
    )
    def test_parse_matches_the_sdk_on_valid_inputs(self, args: tuple[Any, ...]) -> None:
        assert Duration.parse(*args).elapsed == surrealdb.Duration.parse(*args).elapsed

    def test_sdk_api_parity(self) -> None:
        # The public members of surrealdb.Duration 2.0.0 — a fixed list, never dir() of the installed
        # SDK, so an SDK release that adds a member does not fail the suite (spec R3-5, §12 item 2).
        members = [
            "elapsed",
            "parse",
            "get_seconds_and_nano",
            "to_compact",
            "to_string",
            "nanoseconds",
            "microseconds",
            "milliseconds",
            "seconds",
            "minutes",
            "hours",
            "days",
            "weeks",
            "years",
        ]
        for name in members:
            assert hasattr(Duration(5), name), name
        parameters = inspect.signature(Duration.parse).parameters
        assert list(parameters) == ["value", "nanoseconds"]
        assert parameters["nanoseconds"].default == 0
        ours, sdk = Duration(33019506007008009), surrealdb.Duration(33019506007008009)
        for unit in ["nanoseconds", "microseconds", "milliseconds", "seconds", "minutes", "hours", "days", "weeks", "years"]:
            assert getattr(ours, unit) == getattr(sdk, unit), unit

    @pytest.mark.parametrize(
        "elapsed",
        [0, 1, 999, 10**9, 5400 * 10**9 + 1, 86400 * 10**9 * 365 + 7, 10**18 - 1, MAX_NS],
    )
    def test_to_string_round_trips(self, elapsed: int) -> None:
        assert Duration.parse(str(Duration(elapsed))) == Duration(elapsed)

    def test_to_string_is_lossless(self) -> None:
        assert str(Duration.parse("1h30m1ns")) == "1h30m1ns"
        assert Duration.parse("1h30m1ns").to_string() == "1h30m1ns"
        assert Duration(0).to_string() == "0ns"
        assert Duration(MAX_NS).to_string() == "584942417355y3w5d7h15s999ms999us999ns"

    def test_timedelta_conversions(self) -> None:
        assert Duration.from_timedelta(timedelta(days=1, microseconds=5)).elapsed == 86400 * 10**9 + 5000
        big = timedelta.max
        assert Duration.from_timedelta(big).elapsed == (big.days * 86400 + big.seconds) * 10**9 + big.microseconds * 1000
        assert type(Duration.from_timedelta(timedelta(hours=1))) is Duration
        assert Duration(1500).to_timedelta() == timedelta(microseconds=1)  # floor
        assert Duration.parse("1h30m").to_timedelta() == timedelta(hours=1, minutes=30)
        _assert_invalid_duration(lambda: Duration.from_timedelta(timedelta(seconds=-1)))
        _assert_type_error(lambda: Duration.from_timedelta(5))  # type: ignore[arg-type]
        # to_timedelta() keeps its plain ValueError (spec §4.7).
        with pytest.raises(ValueError, match="timedelta"):
            Duration(MAX_NS).to_timedelta()

    def test_invalid_duration_error_class(self) -> None:
        assert issubclass(InvalidDurationError, ValueError)
        assert issubclass(InvalidDurationError, surrealdb.InvalidDurationError)
        assert InvalidDurationError.__name__ == "InvalidDurationError"
        assert InvalidDurationError.__module__ == "surreal_orm_lite.exceptions"
        assert "InvalidDurationError" in surreal_orm_lite.__all__
        assert surreal_orm_lite.InvalidDurationError is surreal_orm_lite.exceptions.InvalidDurationError
        # Code written against the SDK catches the ORM's refusals.
        with pytest.raises(surrealdb.InvalidDurationError):
            Duration.parse("1.5h")

    def test_equality_hash_ordering(self) -> None:
        assert Duration(5) == surrealdb.Duration(5)
        assert Duration(5) != surrealdb.Duration(6)
        assert (surrealdb.Duration(5) == Duration(5)) is False, (
            "the SDK's `Duration.__eq__` now defers to the ORM type: update README section 24 and spec §4.2"
        )
        assert Duration(5) != timedelta(0)
        assert Duration(0) != timedelta(0)
        assert hash(Duration(5)) == hash(Duration(5))
        assert len({Duration(5), Duration(5)}) == 1
        assert sorted([Duration(3), Duration(1)]) == [Duration(1), Duration(3)]
        assert Duration(1) < surrealdb.Duration(2)
        assert Duration(2) >= surrealdb.Duration(2)
        assert Duration(3) > Duration(2)
        with pytest.raises(TypeError):
            _ = Duration(1) < 2  # type: ignore[operator]

    def test_pydantic_field(self) -> None:
        class M(BaseModel):
            d: Duration | None = None
            ds: list[Duration] = []

        from_sdk = M(d=surrealdb.Duration(7)).d  # type: ignore[arg-type]
        assert from_sdk == Duration(7)
        assert type(from_sdk) is Duration
        assert M(d=timedelta(hours=1)).d == Duration.parse("1h")  # type: ignore[arg-type]
        assert M(d="1h30m").d == Duration.parse("1h30m")  # type: ignore[arg-type]
        assert M(d=" 1h30m ").d == Duration.parse("1h30m")  # type: ignore[arg-type]
        assert M(ds=["1h", surrealdb.Duration(1)]).ds == [Duration.parse("1h"), Duration(1)]  # type: ignore[list-item]
        for bad in [5, 1.5, True, "1.5h", "1h 30m", timedelta(seconds=-1), b"1h", surrealdb.Duration(1.5)]:  # type: ignore[arg-type]
            with pytest.raises(ValidationError):
                M(d=bad)
        with pytest.raises(ValidationError) as info:
            M(d="1.5h")  # type: ignore[arg-type]
        assert info.value.errors()[0]["type"] == "value_error"

        m = M(d="1h30m1ns", ds=["1ns", "2h"])  # type: ignore[arg-type, list-item]
        assert isinstance(m.model_dump()["d"], Duration)
        assert '"1h30m1ns"' in m.model_dump_json()
        assert m.model_dump(mode="json")["ds"] == ["1ns", "2h"]
        assert M.model_validate_json(m.model_dump_json()) == m
        schema = M.model_json_schema()["properties"]["d"]
        assert "string" in json.dumps(schema)


# --- Range[T] (spec §4.3, §5.5) ------------------------------------------------------------------

_JAN = datetime(2026, 1, 1, tzinfo=UTC)
_FEB = datetime(2026, 2, 1, tzinfo=UTC)
_ASYMMETRY = "the SDK's `Range.__eq__` now defers to the ORM type: update README section 24 and spec §4.3"

# (range, its SurrealQL text, the range that text parses back to when the bound types differ).
RANGE_LITERALS: list[tuple[Range[Any], str, Range[Any] | None]] = [
    (Range(BoundIncluded(1), BoundExcluded(5)), "1..5", None),
    (Range(BoundIncluded(1), BoundIncluded(5)), "1..=5", None),
    (Range(BoundExcluded(1), BoundExcluded(5)), "1>..5", None),
    (Range(BoundExcluded(1), BoundIncluded(5)), "1>..=5", None),
    (Range(None, BoundExcluded(5)), "..5", None),
    (Range(None, BoundIncluded(5)), "..=5", None),
    (Range(BoundIncluded(1), None), "1..", None),
    (Range(BoundExcluded(1), None), "1>..", None),
    (Range(), "..", None),
    (Range(BoundIncluded(-5), BoundExcluded(-1)), "-5..-1", None),
    (Range(BoundIncluded(1.5), BoundExcluded(2.5)), "1.5..2.5", None),
    (Range(BoundIncluded(1e20), BoundExcluded(1e21)), "1e+20..1e+21", None),
    (Range(BoundIncluded(1e-05), BoundExcluded(-0.0)), "1e-05..-0.0", None),
    (Range(BoundIncluded(Decimal("1.5")), BoundExcluded(Decimal("2"))), "1.5dec..2dec", None),
    (Range(BoundIncluded(_JAN), BoundExcluded(_FEB)), "d'2026-01-01T00:00:00Z'..d'2026-02-01T00:00:00Z'", None),
    (
        Range(BoundExcluded(datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)), None),
        "d'2026-01-02T03:04:05.123456Z'>..",
        None,
    ),
    (
        Range(BoundIncluded(date(2026, 1, 2)), None),
        "d'2026-01-02T00:00:00Z'..",
        Range(BoundIncluded(datetime(2026, 1, 2, tzinfo=UTC)), None),
    ),
    (Range(BoundIncluded("a"), BoundExcluded("m")), "'a'..'m'", None),
    (
        Range(BoundIncluded(timedelta(minutes=1)), BoundExcluded(Duration.parse("1h"))),
        "1m..1h",
        Range(BoundIncluded(Duration.parse("1m")), BoundExcluded(Duration.parse("1h"))),
    ),
    (
        Range(BoundIncluded(surrealdb.Duration(5400 * 10**9 + 1)), None),
        "1h30m1ns..",
        Range(BoundIncluded(Duration.parse("1h30m1ns")), None),
    ),
]


class TestRange:
    def test_construct(self) -> None:
        r = Range(BoundIncluded(1), BoundExcluded(5))
        assert r.begin == BoundIncluded(1)
        assert type(r.begin) is BoundIncluded
        assert r.end == BoundExcluded(5)
        assert type(r.end) is BoundExcluded
        both_open = Range()
        assert both_open.begin is None
        assert both_open.end is None
        end_only = Range(end=BoundIncluded(5))
        assert end_only.begin is None
        assert end_only.end == BoundIncluded(5)
        # A bare SDK ``Bound()`` (the SDK docs' spelling of an open side) is read as open.
        assert Range(Bound(), BoundExcluded(5)).begin is None  # type: ignore[arg-type]
        assert Range(BoundIncluded(1), Bound()).end is None  # type: ignore[arg-type]
        assert repr(r) == "Range(begin=BoundIncluded(value=1), end=BoundExcluded(value=5))"
        assert repr(both_open) == "Range(begin=None, end=None)"
        typed = Range[int](BoundIncluded(9), BoundExcluded(17))
        assert type(typed) is Range
        assert typed == Range(BoundIncluded(9), BoundExcluded(17))
        with pytest.raises(AttributeError):
            r.other = 1  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        "args",
        [(1, 5), ("a",), (BoundIncluded(1), 5), (None, "x"), ([1],), (SdkRange(BoundIncluded(1), None),)],
    )
    def test_constructor_refusals(self, args: tuple[Any, ...]) -> None:
        with pytest.raises(TypeError, match="BoundIncluded"):
            Range(*args)

    def test_equality(self) -> None:
        assert Range(BoundIncluded(1), BoundExcluded(5)) == Range(BoundIncluded(1), BoundExcluded(5))
        assert Range() == Range()
        assert Range(BoundIncluded(1), BoundExcluded(5)) == SdkRange(BoundIncluded(1), BoundExcluded(5))
        assert Range(BoundIncluded(1), None) == SdkRange(BoundIncluded(1), None)  # type: ignore[arg-type]
        assert Range() == SdkRange(None, None)  # type: ignore[arg-type]
        assert Range(BoundIncluded(1)) == SdkRange(BoundIncluded(1), Bound())
        assert (SdkRange(BoundIncluded(1), None) == Range(BoundIncluded(1), None)) is False, _ASYMMETRY  # type: ignore[arg-type]
        assert Range(BoundIncluded(1), BoundExcluded(5)) != Range(BoundExcluded(1), BoundExcluded(5))
        assert Range(BoundIncluded(1), BoundExcluded(5)) != Range(BoundIncluded(1), BoundIncluded(5))
        assert Range(BoundIncluded(1), None) != Range(BoundIncluded(1), BoundExcluded(5))
        assert Range(BoundIncluded(1)) != Range(BoundIncluded(2))
        assert Range(BoundIncluded(1)) != SdkRange(BoundExcluded(1), None)  # type: ignore[arg-type]
        assert Range() != ".."
        assert Range() != None  # noqa: E711
        with pytest.raises(TypeError):
            hash(Range())
        with pytest.raises(TypeError):
            _ = {Range(BoundIncluded(1))}

    @pytest.mark.parametrize(("value", "text"), [(value, text) for value, text, _ in RANGE_LITERALS])
    def test_str_is_surrealql(self, value: Range[Any], text: str) -> None:
        assert str(value) == text
        assert f"{value}" == text

    @pytest.mark.parametrize(
        "bound",
        [True, UUID("0190c4b6-0000-7000-8000-000000000000"), [1], {"a": 1}, None, math.nan, math.inf, Range()],
    )
    def test_str_never_raises(self, bound: Any) -> None:
        for value in (Range(BoundIncluded(bound), None), Range(BoundIncluded(1), BoundExcluded(bound))):
            assert str(value) == repr(value)

    @pytest.mark.parametrize(("value", "text", "parsed"), RANGE_LITERALS)
    def test_parse_range_text_reads_every_literal(self, value: Range[Any], text: str, parsed: Range[Any] | None) -> None:
        result = _parse_range_text(text)
        assert type(result) is Range
        assert result == (value if parsed is None else parsed)
        assert str(result) == text

    def test_parse_range_text_bound_types(self) -> None:
        def bounds(text: str) -> tuple[Any, Any]:
            result = _parse_range_text(text)
            return (
                None if result.begin is None else result.begin.value,
                None if result.end is None else result.end.value,
            )

        begin, end = bounds("1..5")
        assert type(begin) is int
        assert type(end) is int
        begin, end = bounds("1.5..1e+20")
        assert type(begin) is float
        assert type(end) is float
        # ``1dec`` is a decimal, never the duration ``1d`` followed by ``ec``.
        assert bounds("1dec..2dec") == (Decimal(1), Decimal(2))
        assert type(bounds("1dec..")[0]) is Decimal
        # ``1m`` is a duration, never the integer ``1`` followed by ``m``.
        assert bounds("1m..1h") == (Duration.parse("1m"), Duration.parse("1h"))
        assert type(bounds("1m..")[0]) is Duration
        assert bounds("1ms..1us") == (Duration.parse("1ms"), Duration.parse("1us"))
        # Quotes, escapes and ``..`` inside a string.
        assert bounds("'a..b'..'it\\'s'") == ("a..b", "it's")
        assert bounds("'back\\\\slash'..") == ("back\\slash", None)
        assert bounds("''..") == ("", None)
        # Datetimes are aware, in UTC.
        begin, _ = bounds("d'2026-01-02T03:04:05.123456Z'..")
        assert begin == datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
        assert begin.tzinfo is not None
        # Without an offset, a datetime is UTC (as the server reads it).
        begin, _ = bounds("d'2026-01-02'..")
        assert begin == datetime(2026, 1, 2, tzinfo=UTC)
        assert begin.tzinfo is not None
        assert bounds("..=5") == (None, 5)
        # The cap is 1024 characters.
        assert bounds("'" + "a" * 1020 + "'..") == ("a" * 1020, None)

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "1",
            "1...5",
            "1....5",
            "1..5..6",
            "1..5x",
            "1..5de",
            "1.5h..2h",
            "[1]..[5]",
            "true..false",
            "u'0190c4b6-0000-7000-8000-000000000000'..u'0190c4b6-0000-7000-8000-000000000001'",
            "1..'unterminated",
            "'unterminated..",
            "'a\\x'..",
            ">..5",
            "1..=",
            "1>",
            "-..5",
            "--1..5",
            "d'not a date'..",
            "d'notadate'..",
            "d''..",
            "d'2026-01-02",
            "d'2026-01-02'x..",
            "d'2026-01-02 03:04:05Z'..",
            "584942417356y..",
            "'" + "a" * 1021 + "'..",
            # The range parser never strips (revision 4): it reads only what the encoder emits.
            " 1..5",
            "1..5 ",
            "1 ..5",
            "1.. 5",
            " 1m..1h",
            "1m ..1h",
            "\t..",
        ],
    )
    def test_parse_range_text_refusals(self, text: str) -> None:
        with pytest.raises(ValueError):
            _parse_range_text(text)

    def test_pydantic_field(self) -> None:
        class M(BaseModel):
            r: Range[int] | None = None
            rd: Range[datetime] | None = None
            rt: Range[timedelta] | None = None
            rday: Range[date] | None = None
            rdur: Range[Duration] | None = None
            ra: Range | None = None  # type: ignore[type-arg]

        # Accepted forms: ORM Range, SDK Range, builtin range with step 1, the literal text.
        assert M(r=range(1, 5)).r == Range(BoundIncluded(1), BoundExcluded(5))  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            M(r=range(1, 5, 2))  # type: ignore[arg-type]
        from_sdk = M(r=SdkRange(BoundIncluded(1), BoundExcluded(5))).r  # type: ignore[arg-type]
        assert type(from_sdk) is Range
        assert from_sdk == Range(BoundIncluded(1), BoundExcluded(5))
        assert M(r=SdkRange(BoundIncluded(1), None)).r == Range(BoundIncluded(1), None)  # type: ignore[arg-type]
        assert M(r=SdkRange(BoundIncluded(1), Bound())).r == Range(BoundIncluded(1), None)  # type: ignore[arg-type]
        coerced = M(r=SdkRange(BoundIncluded("1"), None)).r  # type: ignore[arg-type]
        assert coerced is not None and coerced.begin is not None
        assert coerced.begin.value == 1
        assert type(coerced.begin.value) is int
        assert M(r="9..17").r == Range(BoundIncluded(9), BoundExcluded(17))  # type: ignore[arg-type]
        assert M(r="..").r == Range()  # type: ignore[arg-type]
        same = Range(BoundIncluded(1), BoundExcluded(5))
        assert M(r=same).r == same

        # Bound values validated as T.
        with pytest.raises(ValidationError):
            M(r=Range(BoundIncluded("x"), None))
        with pytest.raises(ValidationError):
            M(r="'a'..'m'")  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            M(r=Range(BoundIncluded(None), None))

        rd = M(rd=Range(BoundIncluded("2026-01-01T00:00:00Z"), BoundExcluded("2026-02-01T00:00:00+00:00"))).rd
        assert rd == Range(BoundIncluded(_JAN), BoundExcluded(_FEB))
        assert rd is not None and rd.begin is not None
        assert type(rd.begin.value) is datetime
        assert rd.begin.value.tzinfo is not None
        rd_text = M(rd="d'2026-01-01T00:00:00Z'..").rd  # type: ignore[arg-type]
        assert rd_text == Range(BoundIncluded(_JAN), None)
        sdk_dt = surrealdb.Datetime("2026-01-02T03:04:05.123456789Z")
        rd_sdk = M(rd=SdkRange(BoundIncluded(sdk_dt), None)).rd  # type: ignore[arg-type]
        assert rd_sdk is not None and rd_sdk.begin is not None
        assert rd_sdk.begin.value == datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)

        sdk_bounds = SdkRange(
            BoundIncluded(surrealdb.Duration(60 * 10**9)), BoundExcluded(surrealdb.Duration(3600 * 10**9 + 1))
        )
        rt = M(rt=sdk_bounds).rt  # type: ignore[arg-type]
        assert rt == Range(BoundIncluded(timedelta(minutes=1)), BoundExcluded(timedelta(hours=1)))
        assert rt is not None and rt.begin is not None and rt.end is not None
        assert type(rt.begin.value) is timedelta
        assert type(rt.end.value) is timedelta
        rt_text = M(rt="1m..1h").rt  # type: ignore[arg-type]
        assert rt_text == Range(BoundIncluded(timedelta(minutes=1)), BoundExcluded(timedelta(hours=1)))
        assert rt_text is not None and rt_text.begin is not None
        assert type(rt_text.begin.value) is timedelta
        assert M(rt=Range(BoundIncluded("1h"), None)).rt == Range(BoundIncluded(timedelta(hours=1)), None)

        rday = M(rday="d'2026-01-02T00:00:00Z'..").rday  # type: ignore[arg-type]
        assert rday is not None and rday.begin is not None
        assert rday.begin.value == date(2026, 1, 2)
        assert type(rday.begin.value) is date

        rdur = M(rdur=Range(BoundIncluded(surrealdb.Duration(5)), BoundExcluded("1h"))).rdur
        assert rdur == Range(BoundIncluded(Duration(5)), BoundExcluded(Duration.parse("1h")))
        assert rdur is not None and rdur.begin is not None
        assert type(rdur.begin.value) is Duration

        # A bare Range keeps the values as given.
        sdk_duration = surrealdb.Duration(5)
        ra = M(ra=Range(BoundIncluded(sdk_duration), BoundExcluded("x"))).ra
        assert ra is not None and ra.begin is not None and ra.end is not None
        assert ra.begin.value is sdk_duration
        assert ra.end.value == "x"
        ra_sdk = M(ra=SdkRange(BoundIncluded("1"), None)).ra  # type: ignore[arg-type]
        assert type(ra_sdk) is Range
        assert ra_sdk == Range(BoundIncluded("1"), None)

        for bad in [[1, 5], (1, 5), "1..5x", "x", 5, True, {"begin": 1}, SdkRange(1, 5)]:  # type: ignore[arg-type]
            with pytest.raises(ValidationError):
                M(r=bad)
            with pytest.raises(ValidationError):
                M(ra=bad)

        # Dumps: the object in Python mode, the literal text in JSON mode.
        m = M(r=range(1, 5))  # type: ignore[arg-type]
        assert isinstance(m.model_dump()["r"], Range)
        assert '"1..5"' in m.model_dump_json()
        assert m.model_dump(mode="json")["r"] == "1..5"
        schema = M.model_json_schema()
        assert "string" in json.dumps(schema["properties"]["r"])
        assert "1..5" in json.dumps(schema["properties"]["r"])

        # JSON round trip for every bound type with a literal form, and open sides.
        offset = timezone(timedelta(hours=2))
        for model in [
            M(r=Range(BoundExcluded(1), BoundIncluded(5))),
            M(r=Range(None, BoundExcluded(-5))),
            M(r=Range()),
            M(rd=Range(BoundIncluded(datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)), BoundExcluded(_FEB))),
            M(rd=Range(None, BoundIncluded(datetime(2026, 1, 2, 3, tzinfo=offset)))),
            M(rt=Range(BoundIncluded(timedelta(minutes=1)), BoundExcluded(timedelta(hours=1, microseconds=5)))),
            M(rday=Range(BoundIncluded(date(2026, 1, 2)), BoundIncluded(date(2026, 2, 1)))),
            M(rdur=Range(BoundIncluded(Duration.parse("1h30m1ns")), None)),
            M(ra=Range(BoundIncluded(Decimal("1.50")), BoundExcluded(Decimal("-2")))),
            M(ra=Range(BoundExcluded(1.5), None)),
            M(ra=Range(BoundIncluded("it's..x"), BoundExcluded("back\\slash"))),
            M(ra=Range(BoundIncluded(Duration.parse("1h30m1ns")), None)),
            M(ra=Range(BoundIncluded(7), None)),
            M(ra=Range()),
        ]:
            assert M.model_validate_json(model.model_dump_json()) == model, model.model_dump_json()


# --- wire.py, first part: duration_ns, rich_kinds, field_input (spec §5.1, §5.4) ------------------


class _Node(BaseModel):
    """A self-referencing model: ``rich_kinds`` must terminate on it (cycle guard)."""

    ttl: timedelta | None = None
    child: "_Node | None" = None


_Node.model_rebuild()


class _Holder(BaseModel):
    ttl: timedelta


class _Sched(BaseModel):
    """Mixed positions in one model (AC-30): only ``ttl`` and ``when`` convert."""

    ttl: timedelta
    grace: Duration
    extra: dict[str, Any] = {}
    when: datetime | None = Field(None, alias="at")


class _ComputedHolder(BaseModel):
    span: Computed[timedelta] = None


class TestDurationNs:
    def test_exact_nanoseconds(self) -> None:
        assert duration_ns(timedelta(days=1, microseconds=5)) == 86400 * 10**9 + 5000
        assert duration_ns(Duration(7)) == 7
        assert duration_ns(surrealdb.Duration(7)) == 7
        assert duration_ns(Duration(MAX_NS)) == MAX_NS
        assert duration_ns(timedelta.max) == (timedelta.max.days * 86400 + timedelta.max.seconds) * 10**9 + 999_999_000

    def test_refusals(self) -> None:
        with pytest.raises(TypeError):
            duration_ns(surrealdb.Duration(1.5))  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            duration_ns(surrealdb.Duration(True))
        with pytest.raises(TypeError):
            duration_ns("1h")

    def test_max_duration_constant(self) -> None:
        assert MAX_DURATION_NS == Duration.MAX_NANOSECONDS


class TestRichKinds:
    @pytest.mark.parametrize(
        ("annotation", "kinds"),
        [
            (timedelta | None, {"timedelta"}),
            (Optional[timedelta], {"timedelta"}),  # noqa: UP045
            (Annotated[Decimal, "x"], {"decimal"}),
            (Computed[timedelta], {"timedelta"}),
            (list[UUID], {"uuid"}),
            (set[str], {"set"}),
            (frozenset[str], {"frozenset"}),
            (tuple[int, ...], {"tuple"}),
            (tuple[date, str], {"tuple", "date"}),
            (dict[str, date], {"date"}),
            (Mapping[str, timedelta], {"timedelta"}),
            (Sequence[datetime], {"datetime"}),
            (AbstractSet[int], {"set"}),
            (Duration, {"duration"}),
            (Range, {"range"}),
            (Range[int], {"range"}),
            (Range[timedelta] | None, {"range", "timedelta"}),
            (list[Range[datetime]], {"range", "datetime"}),
            (datetime, {"datetime"}),
            (date, {"date"}),
            (set, {"set"}),
            (list[set[timedelta]] | None, {"set", "timedelta"}),
            (int, set()),
            (str, set()),
            (Any, set()),
            (list[int], set()),
            (Literal["a"], set()),
            (_T, set()),
            ("timedelta", set()),
            (ForwardRef("timedelta"), set()),
        ],
    )
    def test_kinds(self, annotation: Any, kinds: set[str]) -> None:
        assert rich_kinds(annotation) == frozenset(kinds)

    def test_computed_as_model_fields_resolves_it(self) -> None:
        assert rich_kinds(_ComputedHolder.model_fields["span"].annotation) == {"timedelta"}

    def test_into_models(self) -> None:
        assert rich_kinds(_Holder) == {"timedelta"}
        assert rich_kinds(_Holder | None) == {"timedelta"}
        assert rich_kinds(list[_Holder]) == {"timedelta"}
        assert rich_kinds(_Holder, into_models=False) == frozenset()
        assert rich_kinds(list[_Holder], into_models=False) == frozenset()
        assert rich_kinds(_Sched) == {"timedelta", "duration", "datetime"}

    def test_self_referencing_model_terminates(self) -> None:
        assert rich_kinds(_Node) == {"timedelta"}
        assert rich_kinds(_Node | None) == {"timedelta"}
        assert rich_kinds(_Node, into_models=False) == frozenset()

    def test_returns_a_frozenset(self) -> None:
        assert type(rich_kinds(int)) is frozenset
        assert type(rich_kinds(timedelta)) is frozenset


class TestFieldInput:
    # Rule 1
    def test_none(self) -> None:
        assert field_input(timedelta, None) is None
        assert field_input(list[timedelta], None) is None

    # Rule 2
    def test_annotated_is_unwrapped(self) -> None:
        assert field_input(Annotated[timedelta, "x"], "1h") == timedelta(hours=1)
        assert field_input(Computed[timedelta], "1h") == timedelta(hours=1)

    # Rule 4
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (surrealdb.Duration(5400 * 10**9 + 1), timedelta(hours=1, minutes=30)),
            (Duration.parse("1h30m1ns"), timedelta(hours=1, minutes=30)),
            (surrealdb.Duration(1999), timedelta(microseconds=1)),
            ("1h30m", timedelta(hours=1, minutes=30)),
            (" 1h30m ", timedelta(hours=1, minutes=30)),
            ("7d", timedelta(days=7)),
            ("0ns", timedelta(0)),
        ],
    )
    def test_timedelta_converts(self, value: Any, expected: timedelta) -> None:
        result = field_input(timedelta, value)
        assert result == expected
        assert type(result) is timedelta

    @pytest.mark.parametrize("value", ["PT1H30M", 90, 1.5, "1.5h", "1h 30m", "", timedelta(hours=1), b"1h"])
    def test_timedelta_leaves_the_rest(self, value: Any) -> None:
        assert field_input(timedelta, value) is value

    def test_timedelta_beyond_max_names_duration(self) -> None:
        with pytest.raises(ValueError, match="Duration"):
            field_input(timedelta, surrealdb.Duration(MAX_NS))
        with pytest.raises(ValueError, match="Duration"):
            field_input(timedelta, Duration(MAX_NS))
        with pytest.raises(ValueError, match="Duration"):
            field_input(timedelta, "584942417355y")
        # The largest timedelta itself still converts.
        biggest = Duration.from_timedelta(timedelta.max)
        assert field_input(timedelta, biggest) == timedelta.max

    def test_timedelta_bad_sdk_duration_is_left_to_pydantic(self) -> None:
        bad = surrealdb.Duration(1.5)  # type: ignore[arg-type]
        assert field_input(timedelta, bad) is bad

        class M(BaseModel):
            ttl: timedelta

        with pytest.raises(ValidationError):
            M.model_validate({"ttl": field_input(timedelta, bad)})

    # Rule 5
    @pytest.mark.parametrize("annotation", [datetime, date])
    def test_sdk_datetime_becomes_its_string(self, annotation: Any) -> None:
        sdk = surrealdb.Datetime("2026-01-02T03:04:05.123456789Z")
        assert field_input(annotation, sdk) == "2026-01-02T03:04:05.123456789Z"
        now = datetime(2026, 1, 2, tzinfo=UTC)
        assert field_input(annotation, now) is now
        assert field_input(annotation, "2026-01-02") == "2026-01-02"

    def test_sdk_datetime_validates_to_microseconds(self) -> None:
        class M(BaseModel):
            at: datetime

        sdk = surrealdb.Datetime("2026-01-02T03:04:05.123456789Z")
        assert M.model_validate({"at": field_input(datetime, sdk)}).at.microsecond == 123456

    # Rules 3 and 11
    @pytest.mark.parametrize(
        "annotation",
        [
            Any,
            object,
            Duration,
            Range,
            Range[int],
            Range[timedelta],
            Decimal,
            UUID,
            dict[str, Any],
            list[Any],
            int,
            str,
            _T,
            "timedelta",
            ForwardRef("timedelta"),
        ],
    )
    def test_never_converts_elsewhere(self, annotation: Any) -> None:
        sdk = surrealdb.Duration(5)
        assert field_input(annotation, sdk) is sdk
        text = "1h"
        assert field_input(annotation, text) is text

    def test_any_containers_keep_sdk_values(self) -> None:
        value = {"x": surrealdb.Duration(500), "y": [surrealdb.Duration(1)]}
        assert field_input(dict[str, Any], value) is value
        assert field_input(Any, value) is value
        listed = [surrealdb.Duration(500)]
        assert field_input(list[Any], listed) is listed
        assert field_input(list, listed) is listed

    # Rule 6
    def test_union_optional(self) -> None:
        assert field_input(timedelta | None, "1h") == timedelta(hours=1)
        assert field_input(Optional[timedelta], surrealdb.Duration(10**9)) == timedelta(seconds=1)  # noqa: UP045
        assert field_input(Union[None, timedelta], "1h") == timedelta(hours=1)  # noqa: UP007

    def test_union_str_claims_strings(self) -> None:
        text = "1h"
        assert field_input(timedelta | str, text) is text
        assert field_input(timedelta | str, surrealdb.Duration(10**9)) == timedelta(seconds=1)

    def test_union_duration_claims_durations(self) -> None:
        sdk = surrealdb.Duration(5)
        assert field_input(timedelta | Duration, sdk) is sdk
        orm = Duration(5)
        assert field_input(timedelta | Duration | None, orm) is orm

    def test_union_any_claims_everything(self) -> None:
        sdk = surrealdb.Duration(5)
        assert field_input(timedelta | Any, sdk) is sdk  # type: ignore[operator]
        assert field_input(Union[timedelta, object], sdk) is sdk  # noqa: UP007

    def test_union_other_members_do_not_claim(self) -> None:
        assert field_input(timedelta | int, surrealdb.Duration(10**9)) == timedelta(seconds=1)
        assert field_input(timedelta | int, "1h") == timedelta(hours=1)
        number = 90
        assert field_input(timedelta | int, number) is number

    def test_union_with_datetime(self) -> None:
        sdk = surrealdb.Datetime("2026-01-02T00:00:00Z")
        assert field_input(datetime | None, sdk) == "2026-01-02T00:00:00Z"
        assert field_input(date | str, sdk) == "2026-01-02T00:00:00Z"
        assert field_input(datetime | timedelta, sdk) == "2026-01-02T00:00:00Z"
        assert field_input(datetime | timedelta, "1h") == timedelta(hours=1)

    def test_union_without_rich_member_is_untouched(self) -> None:
        sdk = surrealdb.Duration(5)
        assert field_input(int | str, sdk) is sdk

    def test_union_containers_take_the_first_matching_shape(self) -> None:
        annotation = list[timedelta] | dict[str, timedelta]
        assert field_input(annotation, ["1h"]) == [timedelta(hours=1)]
        assert field_input(annotation, {"a": "1h"}) == {"a": timedelta(hours=1)}
        # The first sequence member wins, even when a later one would convert.
        assert field_input(list[str] | list[timedelta], ["1h"]) == ["1h"]
        assert field_input(Sequence[timedelta] | None, ("1h",)) == (timedelta(hours=1),)
        assert field_input(_Holder | None, {"ttl": "1h"}) == {"ttl": timedelta(hours=1)}
        listed = ["1h"]
        assert field_input(int | str, listed) is listed
        mapping = {"a": "1h"}
        assert field_input(list[timedelta] | None, mapping) is mapping

    # Rules 7 to 9
    def test_sequences(self) -> None:
        result = field_input(list[timedelta], ["1h", surrealdb.Duration(1000)])
        assert result == [timedelta(hours=1), timedelta(microseconds=1)]
        assert type(result) is list
        as_tuple = field_input(list[timedelta], ("1h",))
        assert as_tuple == (timedelta(hours=1),)
        assert type(as_tuple) is tuple
        as_set = field_input(set[timedelta], {"1h", "60m"})
        assert as_set == {timedelta(hours=1)}
        assert type(as_set) is set
        as_frozen = field_input(frozenset[timedelta], frozenset({"1h"}))
        assert as_frozen == frozenset({timedelta(hours=1)})
        assert type(as_frozen) is frozenset
        assert field_input(Sequence[timedelta], ["2h"]) == [timedelta(hours=2)]
        assert field_input(AbstractSet[timedelta], {"2h"}) == {timedelta(hours=2)}
        text = "1h"
        assert field_input(list[timedelta], text) is text

    def test_tuples(self) -> None:
        assert field_input(tuple[timedelta, ...], ("1h", "2h")) == (timedelta(hours=1), timedelta(hours=2))
        assert field_input(tuple[timedelta, ...], ["1h"]) == [timedelta(hours=1)]
        assert field_input(tuple[timedelta, str], ("1h", "1h")) == (timedelta(hours=1), "1h")
        assert field_input(tuple[timedelta, str], ["1h", "1h"]) == [timedelta(hours=1), "1h"]
        mismatch = ("1h", "1h", "1h")
        assert field_input(tuple[timedelta, str], mismatch) is mismatch
        empty: tuple[()] = ()
        assert field_input(tuple[()], empty) is empty
        bare = ("1h",)
        assert field_input(tuple, bare) is bare

    def test_mappings(self) -> None:
        result = field_input(dict[str, timedelta], {"a": "1h", "b": surrealdb.Duration(10**9)})
        assert result == {"a": timedelta(hours=1), "b": timedelta(seconds=1)}
        assert list(result) == ["a", "b"]
        assert field_input(Mapping[str, list[timedelta]], {"a": ["1h"]}) == {"a": [timedelta(hours=1)]}
        # Keys are never converted.
        assert field_input(dict[timedelta, int], {"1h": 1}) == {"1h": 1}
        listed = ["1h"]
        assert field_input(dict[str, timedelta], listed) is listed

    # Rule 10
    def test_model_mixed_positions(self) -> None:
        sdk_grace = surrealdb.Duration(5400 * 10**9 + 1)
        extra = {"x": surrealdb.Duration(500), "y": surrealdb.Duration(3600 * 10**9)}
        zz = surrealdb.Duration(1)
        raw = {
            "ttl": surrealdb.Duration(5400 * 10**9 + 1),
            "grace": sdk_grace,
            "extra": extra,
            "at": surrealdb.Datetime("2026-01-02T00:00:00Z"),
            "zz": zz,
        }
        result = field_input(_Sched, raw)
        assert result["ttl"] == timedelta(hours=1, minutes=30)
        assert type(result["ttl"]) is timedelta
        assert result["grace"] is sdk_grace
        assert result["extra"] is extra
        assert result["at"] == "2026-01-02T00:00:00Z"
        assert result["zz"] is zz
        # AC-30: no nanosecond loss at the Duration position, no timedelta(0) in the Any one.
        validated = _Sched.model_validate(result)
        assert validated.grace.elapsed == 5400 * 10**9 + 1
        assert validated.extra["x"] == surrealdb.Duration(500)
        assert validated.when == datetime(2026, 1, 2, tzinfo=UTC)

    def test_model_field_names_and_validation_alias(self) -> None:
        class M(BaseModel):
            model_config = ConfigDict(populate_by_name=True)
            when: datetime | None = Field(None, alias="at")
            ttl: timedelta | None = Field(None, validation_alias="span")

        assert field_input(M, {"when": surrealdb.Datetime("2026-01-02T00:00:00Z")}) == {"when": "2026-01-02T00:00:00Z"}
        assert field_input(M, {"span": "1h"}) == {"span": timedelta(hours=1)}

    def test_model_instance_is_unchanged(self) -> None:
        instance = _Sched(ttl=timedelta(1), grace=Duration(1))
        assert field_input(_Sched, instance) is instance
        assert field_input(_Sched | None, instance) is instance

    def test_copy_on_write(self) -> None:
        untouched = {"a": [timedelta(hours=1)], "b": [timedelta(hours=2)]}
        assert field_input(dict[str, list[timedelta]], untouched) is untouched
        model_input = {"ttl": timedelta(1), "extra": {"x": surrealdb.Duration(1)}}
        assert field_input(_Sched, model_input) is model_input

        value = {"a": [timedelta(hours=1)], "b": ["1h", surrealdb.Duration(1000)]}
        before = copy.deepcopy(value)
        result = field_input(dict[str, list[timedelta]], value)
        assert result is not value
        assert value == before
        assert value["b"] == ["1h", surrealdb.Duration(1000)]
        assert result["a"] is value["a"]
        assert result["b"] == [timedelta(hours=1), timedelta(microseconds=1)]

        nested = {"ttl": "1h", "extra": {}}
        nested_before = copy.deepcopy(nested)
        assert field_input(_Sched, nested) is not nested
        assert nested == nested_before

    def test_self_referencing_model_values(self) -> None:
        value = {"ttl": "1h", "child": {"ttl": "2h", "child": {"ttl": "3h", "child": None}}}
        result = field_input(_Node, value)
        assert result["ttl"] == timedelta(hours=1)
        assert result["child"]["ttl"] == timedelta(hours=2)
        assert result["child"]["child"]["ttl"] == timedelta(hours=3)
        assert result["child"]["child"]["child"] is None
        assert _Node.model_validate(result).child.child.ttl == timedelta(hours=3)  # type: ignore[union-attr]


# --- wire.py, second part: encode_wire, decimal_text, the zero-duration probe (spec §3, §5.1) -----

_INT64_MAX = 2**63 - 1
_INT64_MIN = -(2**63)
_UUID = UUID("0190c3d1-5c3b-7cc6-8f8a-2d2b1f0e9a11")
_PLUS_TWO = timezone(timedelta(hours=2))
_BOTH_MODES = pytest.mark.parametrize("stored", [True, False], ids=["stored", "filter"])


def _tag14(seconds: int, nanoseconds: int) -> CBORTag:
    return CBORTag(TAG_DURATION_COMPACT, [seconds, nanoseconds])


def _tag10(text: str) -> CBORTag:
    return CBORTag(TAG_DECIMAL_STRING, text)


class _SubDate(date):
    """A ``date`` subclass, as ``pendulum.Date`` is: still a calendar date."""


class _Level(enum.IntEnum):
    HIGH = 3


class TestEncodeWire:
    @_BOTH_MODES
    @pytest.mark.parametrize(
        "value",
        [
            None,
            True,
            False,
            1.5,
            float("nan"),
            float("inf"),
            "x",
            b"x",
            _UUID,
            SurrealFunc("time::now()"),
            CBORTag(TAG_DURATION_COMPACT, [1, 0]),
            CBORTag(TAG_DECIMAL_STRING, "1.5"),
            undefined,
            object(),
        ],
        ids=repr,
    )
    def test_unchanged_scalars(self, value: Any, stored: bool) -> None:
        assert encode_wire(value, stored=stored) is value

    # ints: signed 64-bit, both modes
    @_BOTH_MODES
    @pytest.mark.parametrize("value", [0, -1, 1, _INT64_MAX, _INT64_MIN, _Level.HIGH])
    def test_int64_unchanged(self, value: int, stored: bool) -> None:
        assert encode_wire(value, stored=stored) is value

    @_BOTH_MODES
    @pytest.mark.parametrize(
        "value",
        [
            2**63,
            -(2**63) - 1,
            2**64,
            2**70,
            -(2**70),
            # Beyond Python's int-to-str limit: the message must not fail to format.
            pytest.param(10**5000, id="10**5000"),
            pytest.param(-(10**5000), id="-10**5000"),
        ],
    )
    def test_int_beyond_64_bits_refused(self, value: int, stored: bool) -> None:
        with pytest.raises(ValueError, match="64-bit") as info:
            encode_wire(value, stored=stored)
        assert "Decimal" in str(info.value)

    def test_record_id_keys_are_checked(self) -> None:
        for rid in (RecordID("t", 5), RecordID("t", "x"), RecordID("t", [1, {"k": 2}, True, _INT64_MIN])):
            assert encode_wire(rid, stored=True) is rid
        for bad in (
            RecordID("t", 2**63),
            RecordID("t", [1, {"k": 2**70}]),
            RecordID("t", {"a": (-(2**63) - 1,)}),
        ):
            with pytest.raises(ValueError, match="64-bit"):
                encode_wire(bad, stored=True)

    @pytest.mark.parametrize(
        "value",
        [
            [1, 2**63],
            (2**64,),
            {"k": 2**70},
            {2**63},
            {"rid": RecordID("t", 2**63)},
            Range(BoundIncluded(2**70), None),
            surrealdb.Range(BoundIncluded(1), BoundExcluded(2**63)),
        ],
        ids=repr,
    )
    def test_ints_checked_inside_containers_and_bounds(self, value: Any) -> None:
        with pytest.raises(ValueError, match="64-bit"):
            encode_wire(value, stored=True)

    # datetimes and dates
    def test_aware_datetime_moves_to_utc(self) -> None:
        value = datetime(2026, 1, 2, 5, 4, 5, 123456, tzinfo=_PLUS_TWO)
        result = encode_wire(value, stored=True)
        assert result == value
        assert result.tzinfo is UTC
        assert (result.hour, result.microsecond) == (3, 123456)

    def test_naive_datetime_is_taken_as_utc(self) -> None:
        value = datetime(2026, 1, 2, 3, 4, 5, 123456)
        result = encode_wire(value, stored=True)
        assert result == datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
        assert result.tzinfo is UTC

    @pytest.mark.parametrize("value", [date(2026, 1, 2), _SubDate(2026, 1, 2)], ids=["date", "subclass"])
    def test_date_becomes_midnight_utc(self, value: date) -> None:
        result = encode_wire(value, stored=True)
        assert type(result) is datetime
        assert result == datetime(2026, 1, 2, tzinfo=UTC)
        assert result.tzinfo is UTC

    def test_datetime_is_not_read_as_a_date(self) -> None:
        value = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        result = encode_wire(value, stored=True)
        assert result == value
        assert result.hour == 3

    @_BOTH_MODES
    def test_time_is_refused(self, stored: bool) -> None:
        with pytest.raises(TypeError, match="time"):
            encode_wire(time(1), stored=stored)
        with pytest.raises(TypeError, match="time"):
            encode_wire([time(1)], stored=stored)

    # durations
    @_BOTH_MODES
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (timedelta(hours=1, microseconds=5), _tag14(3600, 5000)),
            (timedelta.max, _tag14(999_999_999 * 86_400 + 86_399, 999_999_000)),
            (Duration(1), _tag14(0, 1)),
            (Duration.parse("1h30m1ns"), _tag14(5400, 1)),
            (surrealdb.Duration(5400 * 10**9 + 1), _tag14(5400, 1)),
            (Duration(10**18 - 1), _tag14(999_999_999, 999_999_999)),
            (surrealdb.Duration(10**18 - 1), _tag14(999_999_999, 999_999_999)),
            (Duration(MAX_NS), _tag14(2**64 - 1, 999_999_999)),
            (surrealdb.Duration(MAX_NS), _tag14(2**64 - 1, 999_999_999)),
        ],
        ids=repr,
    )
    def test_durations_become_compact_tags(self, value: Any, expected: CBORTag, stored: bool) -> None:
        result = encode_wire(value, stored=stored)
        assert result == expected
        assert type(result.value) is list
        # Never the SDK's float ``get_seconds_and_nano()``: the nanosecond part is never negative.
        assert 0 <= result.value[1] < 10**9

    @_BOTH_MODES
    @pytest.mark.parametrize(
        "value",
        [
            timedelta(seconds=-1),
            timedelta(microseconds=-1),
            surrealdb.Duration(-1),
            surrealdb.Duration(MAX_NS + 1),
            pytest.param(surrealdb.Duration(10**5000), id="Duration(10**5000)"),
            pytest.param(surrealdb.Duration(-(10**5000)), id="Duration(-10**5000)"),
        ],
        ids=repr,
    )
    def test_durations_out_of_range_refused(self, value: Any, stored: bool) -> None:
        with pytest.raises(ValueError, match="duration"):
            encode_wire(value, stored=stored)

    def test_sdk_duration_without_int_elapsed_refused(self) -> None:
        with pytest.raises(TypeError):
            encode_wire(surrealdb.Duration(1.5), stored=True)  # type: ignore[arg-type]

    # decimals: one rounding step at the server's final scale (spec §5.1, revision 3)
    @_BOTH_MODES
    @pytest.mark.parametrize(
        ("value", "text"),
        [
            (Decimal("12.345"), "12.345"),
            (Decimal("1E-7"), "0.0000001"),
            (Decimal("-5E-7"), "-0.0000005"),
            (Decimal("0E-10"), "0.0000000000"),
            (Decimal("-0"), "-0"),
            (Decimal("0E+50"), "0"),
            (Decimal("1E+3"), "1000"),
            (Decimal("1E+28"), "1" + "0" * 28),
            (Decimal("1.50"), "1.50"),
            (Decimal("0." + "1" * 29), "0." + "1" * 28),
            (Decimal("0." + "0" * 26 + "125"), "0." + "0" * 26 + "13"),
            (Decimal("-0." + "0" * 26 + "125"), "-0." + "0" * 26 + "13"),
            (Decimal("1E-1000000"), "0." + "0" * 28),
            (Decimal("0.05") / 3, "0.0166666666666666666666666667"),
            (Decimal("8." + "0" * 27 + "49"), "8." + "0" * 27),
            (Decimal("-8." + "0" * 27 + "49"), "-8." + "0" * 27),
            (Decimal("12." + "0" * 27 + "49"), "12." + "0" * 27),
            (Decimal("8." + "0" * 27 + "5"), "8." + "0" * 26 + "1"),
            (Decimal("7.9228162514264337593543950336"), "7.922816251426433759354395034"),
            (Decimal("7922816251426433759354395033.55"), "7922816251426433759354395034"),
            (Decimal("7922816251426433759354395033.549"), "7922816251426433759354395033.5"),
            (Decimal("0.792281625142643375935439503355"), "0.7922816251426433759354395034"),
            (Decimal("9.99999999999999999999999999995"), "10." + "0" * 27),
            (Decimal("79228162514264337593543950335.4"), "79228162514264337593543950335"),
            (Decimal("-79228162514264337593543950335"), "-79228162514264337593543950335"),
        ],
        ids=str,
    )
    def test_decimal_text_one_step(self, value: Decimal, text: str, stored: bool) -> None:
        assert decimal_text(value) == text
        assert encode_wire(value, stored=stored) == _tag10(text)
        assert "E" not in text and "e" not in text

    @_BOTH_MODES
    @pytest.mark.parametrize(
        "value",
        [
            Decimal("79228162514264337593543950335.5"),
            Decimal("-79228162514264337593543950335.5"),
            Decimal("79228162514264337593543950336"),
            Decimal("1E+29"),
            Decimal("9" * 40),
        ],
        ids=str,
    )
    def test_decimal_beyond_96_bits_refused(self, value: Decimal, stored: bool) -> None:
        with pytest.raises(ValueError, match="decimal"):
            encode_wire(value, stored=stored)
        with pytest.raises(ValueError, match="decimal"):
            decimal_text(value)

    def test_decimal_text_needs_a_finite_value(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            decimal_text(Decimal("NaN"))

    def test_decimal_text_ignores_the_callers_context(self) -> None:
        import decimal

        third = Decimal("0.05") / 3
        # A low precision and trapped rounding signals in the caller's context change nothing.
        with decimal.localcontext(prec=5, traps=[decimal.Inexact, decimal.Rounded]):
            assert decimal_text(Decimal("8." + "0" * 27 + "49")) == "8." + "0" * 27
            assert decimal_text(third) == "0.0166666666666666666666666667"
            assert decimal_text(Decimal("0." + "1" * 29)) == "0." + "1" * 28

    @_BOTH_MODES
    @pytest.mark.parametrize(
        "value",
        [Decimal("NaN"), Decimal("-Infinity"), Decimal("Infinity"), Decimal("sNaN")],
        ids=str,
    )
    def test_non_finite_decimal_unchanged(self, value: Decimal, stored: bool) -> None:
        assert encode_wire(value, stored=stored) is value
        assert encode_wire([value], stored=stored)[0] is value
        assert encode_wire({"k": value}, stored=stored)["k"] is value

    # ranges
    @_BOTH_MODES
    def test_open_sides_become_undefined(self, stored: bool) -> None:
        result = encode_wire(Range(BoundIncluded(1), None), stored=stored)
        assert type(result) is surrealdb.Range
        assert result.begin == BoundIncluded(1)
        assert result.end is undefined
        both_open = encode_wire(Range(), stored=stored)
        assert both_open.begin is undefined and both_open.end is undefined
        for open_side in (None, Bound(), undefined):
            sdk = encode_wire(surrealdb.Range(open_side, BoundExcluded(5)), stored=stored)
            assert sdk.begin is undefined
            assert type(sdk.end) is BoundExcluded and sdk.end.value == 5

    def test_bound_values_are_encoded(self) -> None:
        result = encode_wire(surrealdb.Range(None, BoundIncluded(timedelta(minutes=1))), stored=True)
        assert result.begin is undefined
        assert type(result.end) is BoundIncluded
        assert result.end.value == _tag14(60, 0)
        mixed = encode_wire(Range(BoundExcluded(Decimal("1E-7")), BoundIncluded(date(2026, 1, 2))), stored=True)
        assert type(mixed.begin) is BoundExcluded and mixed.begin.value == _tag10("0.0000001")
        assert type(mixed.end) is BoundIncluded and mixed.end.value == datetime(2026, 1, 2, tzinfo=UTC)

    def test_bound_values_are_not_restricted(self) -> None:
        # Bound paths accept any bound value on both lines (spec §2.2); only live literals restrict them.
        result = encode_wire(Range(BoundIncluded(_UUID), BoundExcluded([1, {"a": True}])), stored=True)
        assert result.begin.value is _UUID
        assert result.end.value == [1, {"a": True}]
        assert encode_wire(Range(BoundIncluded(None), None), stored=True).begin == BoundIncluded(None)

    def test_a_side_that_is_not_a_bound_is_refused(self) -> None:
        with pytest.raises(TypeError, match="bound"):
            encode_wire(surrealdb.Range("x", None), stored=True)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="bound"):
            encode_wire(surrealdb.Range(BoundIncluded(1), 5), stored=True)  # type: ignore[arg-type]

    def test_range_input_is_not_mutated(self) -> None:
        sdk = surrealdb.Range(None, BoundIncluded(timedelta(minutes=1)))
        encode_wire(sdk, stored=True)
        assert sdk.begin is None
        assert sdk.end.value == timedelta(minutes=1)

    # containers
    def test_sets_become_sorted_lists(self) -> None:
        assert encode_wire({"b", "a"}, stored=True) == ["a", "b"]
        assert encode_wire(frozenset({"b", "a"}), stored=True) == ["a", "b"]
        # Sorted before encoding (encoded CBOR tags are not orderable): deterministic.
        assert encode_wire({timedelta(hours=2), timedelta(hours=1)}, stored=True) == [_tag14(3600, 0), _tag14(7200, 0)]
        unorderable = encode_wire({1, "a"}, stored=True)
        assert type(unorderable) is list
        assert sorted(unorderable, key=str) == [1, "a"]

    def test_nested_containers(self) -> None:
        value = {"k": [timedelta(1), {"d": date(2026, 1, 2)}], "t": (Decimal("1E+3"), 2), "s": {"x"}}
        assert encode_wire(value, stored=True) == {
            "k": [_tag14(86_400, 0), {"d": datetime(2026, 1, 2, tzinfo=UTC)}],
            "t": [_tag10("1000"), 2],
            "s": ["x"],
        }

    def test_tuples_and_mappings_become_lists_and_dicts(self) -> None:
        assert encode_wire((1, "a"), stored=True) == [1, "a"]
        assert type(encode_wire((1, "a"), stored=True)) is list
        result = encode_wire(MappingProxyType({"a": timedelta(seconds=1)}), stored=True)
        assert type(result) is dict
        assert result == {"a": _tag14(1, 0)}

    def test_idempotent_and_never_mutates(self) -> None:
        value = {
            "none": None,
            "bool": True,
            "float": 1.5,
            "str": "x",
            "bytes": b"x",
            "uuid": _UUID,
            "func": SurrealFunc("time::now()"),
            "tag": _tag14(1, 0),
            "undefined": undefined,
            "int": _INT64_MAX,
            "rid": RecordID("t", [1, 2]),
            "aware": datetime(2026, 1, 2, 5, tzinfo=_PLUS_TWO),
            "naive": datetime(2026, 1, 2, 3),
            "date": date(2026, 1, 2),
            "timedelta": timedelta(hours=1, microseconds=5),
            "duration": Duration.parse("1h30m1ns"),
            "sdk_duration": surrealdb.Duration(5),
            "decimal": Decimal("1E-7"),
            "range": Range(BoundIncluded(1), None),
            "sdk_range": surrealdb.Range(None, BoundExcluded(timedelta(minutes=1))),
            "list": [timedelta(hours=1)],
            "tuple": (1, "a"),
            "set": {"b", "a"},
            "nested": {"k": [{"d": date(2026, 1, 2)}]},
        }
        before = copy.deepcopy(value)
        once = encode_wire(value, stored=True)
        assert encode_wire(once, stored=True) == once
        assert encode_wire(once, stored=False) == once
        assert value == before


class TestZeroDuration:
    def test_zero_duration_follows_stored_and_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        zeros = (timedelta(0), Duration(0), surrealdb.Duration(0))
        monkeypatch.setattr(wire, "sdk_decodes_zero_duration", lambda: False)
        for zero in zeros:
            with pytest.raises(ValueError, match="0ns") as info:
                encode_wire(zero, stored=True)
            assert "None" in str(info.value)
            assert encode_wire(zero, stored=False) == _tag14(0, 0)
        with pytest.raises(ValueError, match="0ns"):
            encode_wire(Range(BoundIncluded(timedelta(0)), None), stored=True)
        with pytest.raises(ValueError, match="0ns"):
            encode_wire({"ttls": [timedelta(0)]}, stored=True)
        assert encode_wire(Range(BoundIncluded(timedelta(0)), None), stored=False).begin.value == _tag14(0, 0)

        monkeypatch.setattr(wire, "sdk_decodes_zero_duration", lambda: True)
        for zero in zeros:
            assert encode_wire(zero, stored=True) == _tag14(0, 0)
        assert encode_wire(Range(BoundIncluded(timedelta(0)), None), stored=True).begin.value == _tag14(0, 0)

    def test_probe_is_local_and_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def no_server(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the zero-duration probe must not reach a server")

        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", no_server)
        sdk_decodes_zero_duration.cache_clear()
        try:
            assert isinstance(sdk_decodes_zero_duration(), bool)
            first = sdk_decodes_zero_duration.cache_info()
            assert (first.misses, first.hits) == (1, 0)
            sdk_decodes_zero_duration()
            second = sdk_decodes_zero_duration.cache_info()
            assert (second.misses, second.hits) == (1, 1)
        finally:
            sdk_decodes_zero_duration.cache_clear()

    def test_probe_false_without_codec(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sdk_decodes_zero_duration.cache_clear()
        try:
            monkeypatch.setattr(_sdk, "cbor_decode", None)
            assert sdk_decodes_zero_duration() is False
        finally:
            sdk_decodes_zero_duration.cache_clear()

    @pytest.mark.parametrize(
        ("decoded", "expected"),
        [(surrealdb.Duration(0), True), (surrealdb.Duration(1), False), (0, False), ([], False)],
        ids=repr,
    )
    def test_probe_reads_what_the_codec_returns(self, monkeypatch: pytest.MonkeyPatch, decoded: Any, expected: bool) -> None:
        sdk_decodes_zero_duration.cache_clear()
        try:
            monkeypatch.setattr(_sdk, "cbor_decode", lambda _data: decoded)
            assert sdk_decodes_zero_duration() is expected
        finally:
            sdk_decodes_zero_duration.cache_clear()

    def test_probe_matches_the_installed_sdk(self) -> None:
        try:
            decoded = surrealdb.data.cbor.decode(sdk_cbor_encode(CBORTag(TAG_DURATION_COMPACT, [])))
        except Exception:
            expected = False
        else:
            expected = isinstance(decoded, surrealdb.Duration) and decoded.elapsed == 0
        sdk_decodes_zero_duration.cache_clear()
        assert sdk_decodes_zero_duration() is expected


def _sdk_decodes_native_set() -> bool:
    """Whether the installed SDK decodes CBOR tag 56 (a 3.x native set) — local, no server.

    A test helper only (spec §3): the ORM's behaviour never depends on it.
    """
    try:
        _sdk.cbor_decode(_sdk.cbor_encode(CBORTag(56, [1])))
    except Exception:
        return False
    return True


class TestSdkCanaries:
    """Pins on SDK 2.0.0 behaviour that public text depends on (spec D15, §12): they fail on the SDK bump."""

    def test_sdk_still_cannot_decode_native_sets(self) -> None:
        assert not _sdk_decodes_native_set(), (
            "the SDK now decodes native sets (CBOR tag 56): update README behaviour row 10 and the "
            "`array<…>` advice of section 24 (spec §12 item 5)"
        )

    def test_sdk_still_encodes_duration_under_tag_13(self) -> None:
        # CBOR tag 13 is the single byte 0xcd (major type 6, value 13).
        assert sdk_cbor_encode(surrealdb.Duration(1)).startswith(b"\xcd"), (
            "the SDK now encodes Duration correctly (tag 14): expected on an SDK bump — encode_wire stays "
            "correct; update this canary and open the clean-up issue (spec §12 item 2)"
        )


# --- restore_native and model inputs (spec §5.3, §5.4 set_data) ---------------------------------


@dataclasses.dataclass
class _PlainPoint:
    """A dataclass that is not the SDK's: its dump stays a dict."""

    x: int


class _Loose(BaseModel):
    """Untyped positions: ``model_dump()`` flattens the SDK dataclasses they hold (spec §2.9)."""

    model_config = ConfigDict(extra="allow")

    meta: dict[str, Any] = {}
    items: list[Any] = []
    pair: tuple[Any, ...] = ()
    child: "_Loose | None" = None
    note: str = ""


_Loose.model_rebuild()


class TestRestoreNative:
    def test_sdk_duration_replaces_its_dump(self) -> None:
        raw = surrealdb.Duration(5)
        dumped = {"x": {"elapsed": 5}}
        result = restore_native(dumped, {"x": raw})
        assert result == {"x": raw}
        assert result["x"] is raw
        assert dumped == {"x": {"elapsed": 5}}  # the dump is not mutated

    def test_sdk_ranges_and_bounds_keep_inclusivity(self) -> None:
        closed = SdkRange(BoundIncluded(1), BoundIncluded(5))
        half_open = SdkRange(None, BoundExcluded(5))
        bound = BoundExcluded(3)
        loose = _Loose(meta={"win": closed, "open": half_open, "bound": bound})
        dumped = loose.model_dump()
        assert dumped["meta"]["win"] == {"begin": {"value": 1}, "end": {"value": 5}}  # what Pydantic does
        result = restore_native(dumped, loose)
        assert result["meta"]["win"] is closed
        assert result["meta"]["open"] is half_open
        assert result["meta"]["bound"] is bound

    def test_list_and_tuple_positions(self) -> None:
        first, second = surrealdb.Duration(1), surrealdb.Duration(2)
        loose = _Loose(items=["a", first, {"d": second}], pair=(second, 7))
        result = restore_native(loose.model_dump(), loose)
        assert result["items"][0] == "a"
        assert result["items"][1] is first
        assert result["items"][2]["d"] is second
        assert type(result["pair"]) is tuple
        assert result["pair"][0] is second
        assert result["pair"][1] == 7

    def test_nested_model_raw_against_dict_dump(self) -> None:
        inner = surrealdb.Duration(500)
        loose = _Loose(child=_Loose(meta={"x": inner}, note="c"))
        dumped = loose.model_dump()
        assert dumped["child"]["meta"]["x"] == {"elapsed": 500}
        result = restore_native(dumped, loose)
        assert result["child"]["meta"]["x"] is inner
        assert result["child"]["note"] == "c"

    def test_extra_columns(self) -> None:
        raw = surrealdb.Duration(5400 * 10**9)
        span = SdkRange(BoundIncluded(1), BoundExcluded(5))
        loose = _Loose.model_validate({"wait": raw, "span": span})
        result = restore_native(loose.model_dump(), loose)
        assert result["wait"] is raw
        assert result["span"] is span

    def test_other_values_keep_their_dump(self) -> None:
        # A dataclass that is not the SDK's, and the ORM's Duration (not a dataclass, kept by the dump).
        point = _PlainPoint(1)
        orm = Duration(7)
        loose = _Loose(meta={"p": point, "d": orm, "rid": RecordID("t", 1)})
        dumped = loose.model_dump()
        result = restore_native(dumped, loose)
        assert result["meta"]["p"] == {"x": 1}
        assert result["meta"]["d"] is dumped["meta"]["d"]
        assert result["meta"]["rid"] == RecordID("t", 1)

    @pytest.mark.parametrize(
        ("dumped", "raw"),
        [
            ({"x": [1, {"elapsed": 5}]}, {"x": {"0": 1}}),  # list dump, dict raw
            ({"x": {"elapsed": 5}}, {"x": [surrealdb.Duration(5)]}),  # dict dump, list raw
            ({"x": [{"elapsed": 5}]}, {"x": [surrealdb.Duration(5), 2]}),  # lengths differ
            ({"x": [{"elapsed": 5}]}, {"x": {surrealdb.Duration(5).elapsed}}),  # a set: no positions
            ({"x": {"elapsed": 5}}, {"y": surrealdb.Duration(5)}),  # key missing from raw
            ({"x": {"elapsed": 5}}, "not a container"),
        ],
        ids=["list-vs-dict", "dict-vs-list", "length", "set", "missing-key", "scalar"],
    )
    def test_mismatched_shapes_keep_the_dump(self, dumped: Any, raw: Any) -> None:
        assert restore_native(dumped, raw) is dumped

    def test_copy_on_write(self) -> None:
        loose = _Loose(meta={"a": 1}, items=[1, 2], note="n")
        dumped = loose.model_dump()
        assert restore_native(dumped, loose) is dumped
        raw = surrealdb.Duration(5)
        changed = _Loose(meta={"a": 1, "d": raw}, items=[1, 2])
        dumped = changed.model_dump()
        before = copy.deepcopy(dumped)
        result = restore_native(dumped, changed)
        assert result is not dumped
        assert result["items"] is dumped["items"]  # untouched branches are shared
        assert dumped == before


class TestConstructorInputs:
    """``set_data`` runs ``field_input`` on every validation, the constructor included (spec §5.4)."""

    def test_duration_strings_and_sdk_values_become_timedeltas(self) -> None:
        from tests.test_rich_fields import RfEvent

        ev = RfEvent(ttl="1h30m", ttls=["1h", surrealdb.Duration(1000)])  # type: ignore[arg-type]
        assert ev.ttl == timedelta(hours=1, minutes=30)
        assert type(ev.ttl) is timedelta
        assert ev.ttls == [timedelta(hours=1), timedelta(microseconds=1)]

    def test_sdk_duration_is_floored_to_the_microsecond(self) -> None:
        from tests.test_rich_fields import RfEvent

        data = {"ttl": surrealdb.Duration(5400 * 10**9 + 1), "ttls": [surrealdb.Duration(1999)]}
        before = copy.deepcopy(data)
        ev = RfEvent.model_validate(data)
        assert ev.ttl == timedelta(hours=1, minutes=30)
        assert ev.ttls == [timedelta(microseconds=1)]
        assert data == before  # the caller's dict is never mutated
        assert type(data["ttl"]) is surrealdb.Duration

    def test_invalid_duration_text_is_a_validation_error(self) -> None:
        from tests.test_rich_fields import RfEvent

        with pytest.raises(ValidationError):
            RfEvent(ttl="1.5h")  # type: ignore[arg-type]

    def test_duration_beyond_timedelta_is_a_validation_error(self) -> None:
        from tests.test_rich_fields import RfEvent

        with pytest.raises(ValidationError, match="Duration"):
            RfEvent.model_validate({"ttl": surrealdb.Duration(MAX_NS)})

    def test_sdk_datetime_keeps_microseconds(self) -> None:
        from tests.test_rich_fields import RfEvent

        ev = RfEvent(at=surrealdb.Datetime("2026-01-02T03:04:05.123456789Z"))  # type: ignore[arg-type]
        assert type(ev.at) is datetime
        assert ev.at == datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)

    def test_duration_field_keeps_nanoseconds(self) -> None:
        from tests.test_rich_fields import RfEvent

        ev = RfEvent.model_validate({"grace": surrealdb.Duration(5400 * 10**9 + 1)})
        assert type(ev.grace) is Duration
        assert ev.grace.elapsed == 5400 * 10**9 + 1

    def test_aliased_column_is_converted(self) -> None:
        from surreal_orm_lite import BaseSurrealModel, SurrealConfigDict

        class _AliasedTtl(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="rf_alias_unit")

            id: str | None = None
            ttl: timedelta | None = Field(None, alias="ttl_col")

        assert _AliasedTtl.model_validate({"ttl_col": surrealdb.Duration(60 * 10**9)}).ttl == timedelta(minutes=1)
        assert _AliasedTtl.from_db({"ttl_col": "2h"}).ttl == timedelta(hours=2)  # type: ignore[union-attr]
        assert _AliasedTtl.model_validate({"ttl": "3h"}).ttl == timedelta(hours=3)  # the Python name too

        class _ValidationAliasedTtl(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="rf_valias_unit")

            id: str | None = None
            ttl: timedelta | None = Field(None, validation_alias="ttl_in")

        assert _ValidationAliasedTtl.model_validate({"ttl_in": "1h"}).ttl == timedelta(hours=1)

    def test_record_id_handling_is_unchanged(self) -> None:
        from tests.test_rich_fields import RfEvent

        ev = RfEvent.from_db({"id": RecordID("rf_event", "x"), "ttl": surrealdb.Duration(60 * 10**9)})
        assert ev.id == "x"  # type: ignore[union-attr]
        assert ev.ttl == timedelta(minutes=1)  # type: ignore[union-attr]


class TestApplyRecord:
    """``_apply_record`` re-validates the rich fields of a returned row, and never raises (spec §5.4)."""

    def test_rich_fields_get_declared_types(self) -> None:
        from tests.test_rich_fields import RfEvent

        ev = RfEvent(id="x")
        ev._apply_record(
            {
                "id": RecordID("rf_event", "x"),
                "ttl": surrealdb.Duration(5400 * 10**9 + 1),
                "grace": surrealdb.Duration(5400 * 10**9 + 1),
                "day": datetime(2026, 1, 2, tzinfo=UTC),
                "hours": SdkRange(BoundIncluded(9), BoundExcluded(17)),
                "period": SdkRange(BoundIncluded(datetime(2026, 1, 1, tzinfo=UTC)), None),
                "tags": ["b", "a"],
                "ttls": [surrealdb.Duration(3600 * 10**9)],
                "n": 1,
            }
        )
        assert ev.id == RecordID("rf_event", "x")  # the id is kept native, as before
        assert type(ev.ttl) is timedelta
        assert ev.ttl == timedelta(hours=1, minutes=30)  # floored to the µs
        assert type(ev.grace) is Duration
        assert ev.grace.elapsed == 5400 * 10**9 + 1
        assert type(ev.day) is date
        assert ev.day == date(2026, 1, 2)
        assert type(ev.hours) is Range
        assert ev.hours == Range(BoundIncluded(9), BoundExcluded(17))
        assert type(ev.period) is Range
        assert ev.period.end is None
        assert type(ev.tags) is set
        assert ev.tags == {"a", "b"}
        assert ev.ttls == [timedelta(hours=1)]
        assert ev.n == 1

    def test_other_fields_are_untouched(self) -> None:
        from tests.test_rich_fields import RfEvent, RfNest

        ev = RfEvent(id="x")
        ev._apply_record({"n": "not an int", "price": Decimal("12.50"), "token": "not a uuid"})
        assert ev.n == "not an int"  # type: ignore[comparison-overlap]  # no validation outside the rich kinds
        assert str(ev.price) == "12.50"
        assert ev.token == "not a uuid"  # type: ignore[comparison-overlap]

        nest = RfNest(id="x")
        sched = {"ttl": surrealdb.Duration(60 * 10**9), "grace": surrealdb.Duration(1)}
        nest._apply_record({"sched": sched})
        assert nest.sched is sched  # nested models are not entered: the dict, as since v0.18.0

    def test_only_and_aliases_are_respected(self) -> None:
        from surreal_orm_lite import BaseSurrealModel, SurrealConfigDict
        from tests.test_rich_fields import RfEvent

        ev = RfEvent(id="x", ttl=timedelta(minutes=1))
        ev._apply_record({"ttl": surrealdb.Duration(3600 * 10**9), "grace": surrealdb.Duration(1)}, only=["grace"])
        assert ev.ttl == timedelta(minutes=1)
        assert type(ev.grace) is Duration

        class _AliasedRow(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="rf_alias_row_unit")

            id: str | None = None
            ttl: timedelta | None = Field(None, alias="ttl_col")

        aliased = _AliasedRow(id="x")
        aliased._apply_record({"ttl_col": surrealdb.Duration(60 * 10**9)})
        assert type(aliased.ttl) is timedelta
        assert aliased.ttl == timedelta(minutes=1)

    def test_apply_record_never_raises(self) -> None:
        from tests.test_rich_fields import RfEvent

        ev = RfEvent(id="x")
        too_long = surrealdb.Duration(MAX_NS)  # beyond timedelta.max
        not_midnight = datetime(2026, 1, 2, 5, tzinfo=UTC)  # not a date
        ev._apply_record({"ttl": too_long, "tags": 5, "day": not_midnight, "n": 1})
        assert ev.ttl is too_long  # the raw values are kept
        assert ev.tags == 5
        assert ev.day is not_midnight
        assert ev.n == 1


class TestReturnType:
    """``call_function(return_type=)`` / ``info(return_type=)`` convert the SDK forms first (spec §5.4)."""

    def test_sdk_values_fit_rich_return_types(self) -> None:
        coerce = SurrealDBConnectionManager._coerce_result
        assert coerce(surrealdb.Duration(5400 * 10**9 + 1), timedelta, "x") == timedelta(hours=1, minutes=30)
        assert coerce([surrealdb.Duration(1000)], list[timedelta], "x") == [timedelta(microseconds=1)]
        assert coerce(surrealdb.Datetime("2026-01-02T03:04:05.123456789Z"), datetime, "x") == datetime(
            2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC
        )
        as_duration = coerce(surrealdb.Duration(7), Duration, "x")
        assert type(as_duration) is Duration
        assert as_duration.elapsed == 7
        sdk_value = surrealdb.Duration(7)
        assert coerce(sdk_value, None, "x") is sdk_value  # no return_type: untouched

        class _Lock(BaseModel):
            ttl: timedelta
            meta: dict[str, Any] = {}

        lock = coerce({"ttl": surrealdb.Duration(60 * 10**9), "meta": {"d": sdk_value}}, _Lock, "info()")
        assert lock.ttl == timedelta(minutes=1)
        assert lock.meta["d"] is sdk_value  # an Any position keeps the SDK value

    def test_unfit_value_is_a_validation_error(self) -> None:
        from surreal_orm_lite import SurrealDbValidationError

        with pytest.raises(SurrealDbValidationError, match="does not fit"):
            SurrealDBConnectionManager._coerce_result(surrealdb.Duration(MAX_NS), timedelta, "'fn::x'")
        with pytest.raises(SurrealDbValidationError, match="does not fit"):
            SurrealDBConnectionManager._coerce_result("1.5h", timedelta, "'fn::x'")


# --- raw SDK E2E: what both server lines do with each encoded form (AC-6, AC-29, AC-32 b) ---------


def _http_url() -> str:
    return f"http://{os.environ.get('SURREALDB_HOST', 'localhost')}:{os.environ.get('SURREALDB_PORT', '8000')}"


async def _http_client() -> Any:
    """A fresh HTTP client signed in on ``ns``/``db``.

    Never closed: SDK 2.0.0's HTTP ``close()`` raises ``NotImplementedError``, and the client opens
    one ``aiohttp`` session per request, so there is nothing to release.
    """
    client = AsyncSurreal(_http_url())
    await client.signin({"username": "root", "password": "root"})
    await client.use("ns", "db")
    return client


async def _server_version() -> tuple[int, int, int]:
    """The connected server's version: ``"surrealdb-3.3.4"`` → ``(3, 3, 4)`` (ORM client, set up first)."""
    raw = str(await (await SurrealDBConnectionManager.get_client()).version())
    major, minor, patch = (int(n) for n in re.findall(r"\d+", raw)[:3])
    return major, minor, patch


# (label, value encoded with stored=True, expected read-back, expected type). Ranges are compared with
# the ORM ``Range`` on the left: the SDK's ``Range.__eq__`` answers False to anything else.
_WIRE_CASES: list[tuple[str, Any, Any, type]] = [
    ("1ns ORM", Duration(1), surrealdb.Duration(1), surrealdb.Duration),
    ("1ns SDK", surrealdb.Duration(1), surrealdb.Duration(1), surrealdb.Duration),
    ("1h30m timedelta", timedelta(hours=1, minutes=30), surrealdb.Duration(5400 * 10**9), surrealdb.Duration),
    ("1h30m ORM", Duration.parse("1h30m"), surrealdb.Duration(5400 * 10**9), surrealdb.Duration),
    ("1h30m SDK", surrealdb.Duration(5400 * 10**9), surrealdb.Duration(5400 * 10**9), surrealdb.Duration),
    ("1e18-1 ORM", Duration(10**18 - 1), surrealdb.Duration(10**18 - 1), surrealdb.Duration),
    ("1e18-1 SDK", surrealdb.Duration(10**18 - 1), surrealdb.Duration(10**18 - 1), surrealdb.Duration),
    ("max ORM", Duration(MAX_NS), surrealdb.Duration(MAX_NS), surrealdb.Duration),
    ("max SDK", surrealdb.Duration(MAX_NS), surrealdb.Duration(MAX_NS), surrealdb.Duration),
    ("[1,5)", Range(BoundIncluded(1), BoundExcluded(5)), Range(BoundIncluded(1), BoundExcluded(5)), surrealdb.Range),
    ("[1,5]", Range(BoundIncluded(1), BoundIncluded(5)), Range(BoundIncluded(1), BoundIncluded(5)), surrealdb.Range),
    ("(1,5]", Range(BoundExcluded(1), BoundIncluded(5)), Range(BoundExcluded(1), BoundIncluded(5)), surrealdb.Range),
    ("..5", Range(None, BoundExcluded(5)), Range(None, BoundExcluded(5)), surrealdb.Range),
    ("1..", Range(BoundIncluded(1), None), Range(BoundIncluded(1), None), surrealdb.Range),
    ("..", Range(), Range(), surrealdb.Range),
    ("..=5 SDK", surrealdb.Range(None, BoundIncluded(5)), Range(None, BoundIncluded(5)), surrealdb.Range),
    ("float", Range(BoundIncluded(1.5), BoundExcluded(2.5)), Range(BoundIncluded(1.5), BoundExcluded(2.5)), surrealdb.Range),
    (
        "decimal bounds",
        Range(BoundIncluded(Decimal("1.5")), BoundExcluded(Decimal("1E+3"))),
        Range(BoundIncluded(Decimal("1.5")), BoundExcluded(Decimal(1000))),
        surrealdb.Range,
    ),
    ("str", Range(BoundIncluded("a"), BoundExcluded("m")), Range(BoundIncluded("a"), BoundExcluded("m")), surrealdb.Range),
    (
        "datetime bounds",
        Range(BoundIncluded(_JAN), BoundExcluded(datetime(2026, 2, 1, 2, tzinfo=_PLUS_TWO))),
        Range(BoundIncluded(_JAN), BoundExcluded(_FEB)),
        surrealdb.Range,
    ),
    (
        "duration bounds",
        Range(BoundIncluded(timedelta(minutes=1)), BoundExcluded(Duration.parse("1h"))),
        Range(BoundIncluded(surrealdb.Duration(60 * 10**9)), BoundExcluded(surrealdb.Duration(3600 * 10**9))),
        surrealdb.Range,
    ),
    ("date", date(2026, 1, 2), datetime(2026, 1, 2, tzinfo=UTC), datetime),
    ("naive datetime", datetime(2026, 1, 2, 3, 4, 5, 123456), datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC), datetime),
    ("aware +02:00", datetime(2026, 1, 2, 5, tzinfo=_PLUS_TWO), datetime(2026, 1, 2, 3, tzinfo=UTC), datetime),
    ("1E+3", Decimal("1E+3"), Decimal(1000), Decimal),
    ("1E-7", Decimal("1E-7"), Decimal("1E-7"), Decimal),
    ("-5E-7", Decimal("-5E-7"), Decimal("-5E-7"), Decimal),
    ("0E-10", Decimal("0E-10"), Decimal(0), Decimal),
    ("1.50", Decimal("1.50"), Decimal("1.5"), Decimal),
    ("28 digits", Decimal("123456789.0123456789012345678"), Decimal("123456789.0123456789012345678"), Decimal),
    ("0.05/3", Decimal("0.05") / 3, Decimal("0.0166666666666666666666666667"), Decimal),
    ("...335.4", Decimal("79228162514264337593543950335.4"), Decimal("79228162514264337593543950335"), Decimal),
    ("8.<27 zeros>49", Decimal("8." + "0" * 27 + "49"), Decimal(8), Decimal),
    ("7.92...336", Decimal("7.9228162514264337593543950336"), Decimal("7.922816251426433759354395034"), Decimal),
    ("uuid", _UUID, _UUID, UUID),
    ("bytes", b"\x00\x01\xff", b"\x00\x01\xff", bytes),
    ("set", {"b", "a"}, ["a", "b"], list),
    ("int64 max", _INT64_MAX, _INT64_MAX, int),
    ("int64 min", _INT64_MIN, _INT64_MIN, int),
]


class TestWireFormsE2E:
    @pytest.mark.parametrize("scheme", ["ws", "http"])
    async def test_every_encoded_form_round_trips(self, scheme: str) -> None:
        async with orm_client("rf_wire") as ws_client:
            client = ws_client if scheme == "ws" else await _http_client()
            for index, (label, value, expected, kind) in enumerate(_WIRE_CASES):
                rid = RecordID("rf_wire", index)
                await client.query("CREATE $rid SET v = $v RETURN NONE;", {"rid": rid, "v": encode_wire(value, stored=True)})
                got = await client.query("SELECT VALUE v FROM ONLY $rid;", {"rid": rid})
                assert isinstance(got, kind), f"{label}: read back {got!r}"
                assert expected == got, f"{label}: read back {got!r}, expected {expected!r}"
            # Nothing above dropped the connection: the same client still answers.
            assert await client.query("RETURN 1;", {}) == 1
            if scheme == "ws":
                assert await SurrealDBConnectionManager.get_client() is ws_client


_AGREEMENT_TEXTS = [
    "8." + "0" * 27 + "49",
    "8." + "0" * 27 + "5",
    "12." + "0" * 27 + "49",
    "-8." + "0" * 27 + "49",
    "7.9228162514264337593543950336",
    "7922816251426433759354395033.55",
    "7922816251426433759354395033.549",
    "0.792281625142643375935439503355",
    "9.99999999999999999999999999995",
    "0." + "0" * 26 + "125",
    "79228162514264337593543950335.5",
    "79228162514264337593543950336",
]
_REFUSED = object()


class TestServerAgreementE2E:
    """HTTP only: a refused value or an undecodable result must not cost a WebSocket."""

    async def test_decimal_text_agrees_with_server(self) -> None:
        client = await _http_client()
        message = "decimal_text no longer matches the server's rounding — see spec §2.3 / §5.1"
        for text in _AGREEMENT_TEXTS:
            raw: Any
            try:
                raw = await client.query("RETURN $v;", {"v": _tag10(text)})
            except aiohttp.ClientResponseError as exc:
                assert exc.status == 400, f"{text}: {exc!r}"
                raw = _REFUSED
            encoded: Any
            try:
                encoded = encode_wire(Decimal(text), stored=True)
            except ValueError:
                encoded = _REFUSED
            if raw is _REFUSED or encoded is _REFUSED:
                assert raw is encoded, (
                    f"{message}: {text} is {'refused' if raw is _REFUSED else 'accepted'} by the server "
                    f"but {'refused' if encoded is _REFUSED else 'accepted'} by encode_wire"
                )
                continue
            got = await client.query("RETURN $v;", {"v": encoded})
            assert isinstance(raw, Decimal) and isinstance(got, Decimal), f"{text}: {raw!r} / {got!r}"
            assert got == raw, f"{message}: {text} → server {raw}, encode_wire {got}"

    async def test_probe_matches_server(self) -> None:
        client = await _http_client()
        sdk_decodes_zero_duration.cache_clear()
        if sdk_decodes_zero_duration():
            assert await client.query("RETURN 0ns;", {}) == surrealdb.Duration(0)
        else:
            with pytest.raises(IndexError):
                await client.query("RETURN 0ns;", {})


class TestNativeSetsE2E:
    """3.x native sets (CBOR tag 56): HTTP only — a decode failure on a WebSocket loses the connection."""

    async def test_native_set_reads_by_line(self) -> None:
        async with orm_client("rf_setcol"):
            version = await _server_version()
            probe = _sdk_decodes_native_set()
            client = await _http_client()

            if version >= (3, 0, 0):
                if probe:
                    assert sorted(await client.query("RETURN <set>[2, 1, 1];", {})) == [1, 2]
                else:
                    with pytest.raises(BufferError):
                        await client.query("RETURN <set>[2, 1, 1];", {})
            else:
                result = await client.query("RETURN <set>[2, 1, 1];", {})
                assert isinstance(result, list)
                assert sorted(result) == [1, 2]

            await client.query("DEFINE FIELD tags ON rf_setcol TYPE set<string>;", {})
            tags = encode_wire({"b", "a"}, stored=True)
            assert tags == ["a", "b"]
            create = "CREATE rf_setcol:1 SET tags = $t RETURN NONE;"
            if version >= (3, 0, 0):
                try:
                    await client.query(create, {"t": tags})
                except surrealdb.SurrealError as exc:
                    # 3.1.5 refuses an array into a ``set<…>`` column (measured); 3.3.x stores a set.
                    assert "set" in str(exc), repr(exc)
                    assert version < (3, 3, 0), f"{version} refused the array: {exc!r}"
                    return
                if probe:
                    rows = await client.query("SELECT * FROM rf_setcol;", {})
                    assert sorted(rows[0]["tags"]) == ["a", "b"]
                else:
                    with pytest.raises(BufferError):
                        await client.query("SELECT * FROM rf_setcol;", {})
            else:
                await client.query(create, {"t": tags})
                assert await client.query("SELECT VALUE tags FROM ONLY rf_setcol:1;", {}) == ["a", "b"]
