"""Rich field types (v0.23.0): the ORM's ``Duration`` and ``Range[T]`` and the wire helpers.

Unit tests first (no database); the raw-SDK E2E classes that pin what the servers do with each
wire form are suffixed ``E2E``.
"""

import copy
import inspect
import json
import math
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Any, ForwardRef, Literal, Optional, TypeVar, Union
from uuid import UUID

import pytest
import surrealdb
from pydantic import BaseModel, ConfigDict, Field, ValidationError

import surreal_orm_lite
from surreal_orm_lite import BoundExcluded, BoundIncluded, Computed, Duration, InvalidDurationError, Range
from surreal_orm_lite._sdk import Bound
from surreal_orm_lite.types import _parse_range_text
from surreal_orm_lite.wire import MAX_DURATION_NS, duration_ns, field_input, rich_kinds

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
