"""Rich field types (v0.23.0): the ORM's ``Duration`` and ``Range[T]``.

Unit tests first (no database); the raw-SDK E2E classes that pin what the servers do with each
value are suffixed ``E2E``.
"""

import inspect
import json
import math
import os
import random
import re
import warnings
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Any, Optional
from uuid import UUID

import pytest
import surrealdb
import surrealdb.data.types.range
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticSerializationError
from surrealdb import AsyncSurreal, RecordID
from surrealdb import Range as SdkRange
from surrealdb.cbor import CBORSimpleValue, CBORTag, undefined

import surreal_orm_lite
from surreal_orm_lite import (
    BoundExcluded,
    BoundIncluded,
    Duration,
    InvalidDurationError,
    Range,
    SurrealDBConnectionManager,
    _sdk,
    encoding,
)
from surreal_orm_lite._sdk import Bound
from surreal_orm_lite.surql_literal import to_surql_literal
from surreal_orm_lite.types import (
    _TIMEDELTA_MAX_US,
    _duration_from_sdk,
    _is_open_side,
    _nanoseconds,
    _parse_range_text,
    _timedelta_from_sdk,
)
from tests.conftest import orm_client

MAX_NS = Duration.MAX_NANOSECONDS


# --- module helpers (tests/test_rich_fields.py imports them) -------------------------------------


def _http_url() -> str:
    return f"http://{os.environ.get('SURREALDB_HOST', 'localhost')}:{os.environ.get('SURREALDB_PORT', '8000')}"


async def _http_client() -> Any:
    """A fresh raw SDK client over HTTP, signed in as root on ``ns``/``db``.

    Never ``close()`` it: SDK 2.0.0's HTTP ``close()`` raises ``NotImplementedError``, and the
    client opens one ``aiohttp`` session per request, so there is nothing to release.
    """
    client = AsyncSurreal(_http_url())
    await client.signin({"username": "root", "password": "root"})
    await client.use("ns", "db")
    return client


async def _server_version() -> tuple[int, int, int]:
    """The connected server's version: ``"surrealdb-3.1.5"`` → ``(3, 1, 5)`` (ORM client, set up first)."""
    raw = str(await (await SurrealDBConnectionManager.get_client()).version())
    major, minor, patch = (int(n) for n in re.findall(r"\d+", raw)[:3])
    return major, minor, patch


def _sdk_decodes_native_set() -> bool:
    """Whether the installed SDK decodes a native set (CBOR tag 56, SurrealDB 3.3's ``set``); 2.0.0 does not."""
    try:
        _sdk.cbor_decode(_sdk.cbor_encode(CBORTag(56, [1])))
    except Exception:
        return False
    return True


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
        # The ORM's own values only: the SDK's float version is never called.
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
            # Surrounding whitespace is stripped, as the server does.
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
        # SDK, so an SDK release that adds a member does not fail the suite.
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
        # to_timedelta() keeps its plain ValueError.
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

    def test_exports(self) -> None:
        for name in ["Duration", "BoundIncluded", "BoundExcluded", "InvalidDurationError"]:
            assert name in surreal_orm_lite.__all__, name
        assert surreal_orm_lite.Duration is surreal_orm_lite.types.Duration
        assert surreal_orm_lite.BoundIncluded is surrealdb.data.types.range.BoundIncluded
        assert surreal_orm_lite.BoundExcluded is surrealdb.data.types.range.BoundExcluded
        assert "Duration" in surreal_orm_lite.types.__all__

    def test_equality_hash_ordering(self) -> None:
        assert Duration(5) == surrealdb.Duration(5)
        assert Duration(5) != surrealdb.Duration(6)
        assert (surrealdb.Duration(5) == Duration(5)) is False, (
            "the SDK's `Duration.__eq__` now defers to the ORM type: update the README and spec §5.2"
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

    def test_pydantic_dump_of_a_raw_sdk_duration(self) -> None:
        """AC-2: an attribute holding a raw SDK ``Duration`` dumps as an ORM ``Duration`` — never a dict, no warning."""

        class M(BaseModel):
            d: Duration | None = None
            ds: list[Duration] = []

        m = M(d=Duration(5))
        object.__setattr__(m, "d", surrealdb.Duration(9))
        object.__setattr__(m, "ds", [surrealdb.Duration(1)])
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            dumped = m.model_dump()
            text = m.model_dump_json()
            json_dumped = m.model_dump(mode="json")
        assert [str(w.message) for w in caught] == []
        assert type(dumped["d"]) is Duration
        assert dumped["d"].elapsed == 9
        assert type(dumped["ds"][0]) is Duration
        assert dumped["ds"][0].elapsed == 1
        assert '"9ns"' in text
        assert json_dumped == {"d": "9ns", "ds": ["1ns"]}

        # Out of range: the python-mode serializer builds the ORM value unchecked and never raises
        # (``encode`` refuses it later); JSON mode checks it and Pydantic reports the refusal.
        object.__setattr__(m, "d", surrealdb.Duration(-5))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            dumped = m.model_dump()
        assert [str(w.message) for w in caught] == []
        assert type(dumped["d"]) is Duration
        assert dumped["d"].elapsed == -5
        with pytest.raises(PydanticSerializationError, match="negative"):
            m.model_dump_json()


class TestNanoseconds:
    """``types._nanoseconds``: the exact integer arithmetic shared by ``encode`` and the literal encoder."""

    def test_timedelta_is_exact(self) -> None:
        assert _nanoseconds(timedelta(hours=1, microseconds=1)) == 3600 * 10**9 + 1000
        assert _nanoseconds(timedelta(seconds=-1)) == -(10**9)
        big = timedelta.max
        assert _nanoseconds(big) == (big.days * 86400 + big.seconds) * 10**9 + big.microseconds * 1000

    def test_durations(self) -> None:
        assert _nanoseconds(Duration(5400 * 10**9 + 1)) == 5400 * 10**9 + 1
        assert _nanoseconds(surrealdb.Duration(10**18 - 1)) == 10**18 - 1
        assert _nanoseconds(surrealdb.Duration(-1)) == -1
        assert _nanoseconds(Duration(MAX_NS)) == MAX_NS

    @pytest.mark.parametrize("elapsed", [1.5, True, "1"])
    def test_non_int_elapsed_is_a_value_error(self, elapsed: Any) -> None:
        with pytest.raises(ValueError, match="int number of nanoseconds") as info:
            _nanoseconds(surrealdb.Duration(elapsed))
        assert type(info.value) is ValueError
        # An ORM value the serializer built unchecked is refused the same way.
        with pytest.raises(ValueError, match="int number of nanoseconds"):
            _nanoseconds(Duration._unchecked(elapsed))

    @pytest.mark.parametrize("value", [5, 1.5, "1h", None])
    def test_other_types_are_a_type_error(self, value: Any) -> None:
        with pytest.raises(TypeError):
            _nanoseconds(value)


# --- Range[T] (spec §5.3; AC-4, AC-5) -------------------------------------------------------------

_JAN = datetime(2026, 1, 1, tzinfo=UTC)
_FEB = datetime(2026, 2, 1, tzinfo=UTC)
_ASYMMETRY = "the SDK's `Range.__eq__` now defers to the ORM type: update the README and spec §5.3"
_CBOR_NULL_MARKERS: list[Any] = [None, Bound(), undefined, CBORSimpleValue(22), _sdk.CBOR_NULL]
_MARKER_IDS = ["None", "Bound()", "undefined", "CBORSimpleValue(22)", "CBOR_NULL"]

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

    def test_exports(self) -> None:
        assert "Range" in surreal_orm_lite.__all__
        assert surreal_orm_lite.Range is surreal_orm_lite.types.Range
        assert "Range" in surreal_orm_lite.types.__all__

    def test_static_signature_is_unchanged(self) -> None:
        """Confirmation 3: the other open markers are accepted at runtime only, never annotated."""
        annotations = inspect.get_annotations(Range.__init__)
        assert annotations["begin"] == "BoundIncluded | BoundExcluded | None"
        assert annotations["end"] == "BoundIncluded | BoundExcluded | None"
        assert "Also accepted at runtime" in (Range.__init__.__doc__ or "")

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
        # One-way: the SDK's own ``__eq__`` answers False to any other class.
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
            # The range parser never strips: it reads only what the encoder emits.
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

    def test_timedelta_bounds_from_durations(self) -> None:
        """``Range[timedelta]``: SDK and ORM ``Duration`` bound values become ``timedelta`` (floor to µs)."""

        class M(BaseModel):
            rt: Range[timedelta] | None = None
            rto: Range[Optional[timedelta]] | None = None  # noqa: UP045
            rta: Range[Annotated[timedelta, "meta"]] | None = None

        sdk_bounds = SdkRange(
            BoundIncluded(surrealdb.Duration(60 * 10**9)), BoundExcluded(surrealdb.Duration(3600 * 10**9 + 1))
        )
        expected = Range(BoundIncluded(timedelta(minutes=1)), BoundExcluded(timedelta(hours=1)))
        for field in ("rt", "rto", "rta"):
            rt = getattr(M(**{field: sdk_bounds}), field)
            assert rt == expected, field
            assert type(rt.begin.value) is timedelta
            assert type(rt.end.value) is timedelta
        # The literal reads ORM ``Duration`` bounds, turned into timedeltas too.
        rt_text = M(rt="1m..1h").rt  # type: ignore[arg-type]
        assert rt_text == expected
        assert rt_text is not None and rt_text.begin is not None
        assert type(rt_text.begin.value) is timedelta
        # ORM Duration bound values, floored to the microsecond.
        orm = M(rt=Range(BoundIncluded(Duration(1999)), BoundExcluded(Duration.parse("1h30m1ns")))).rt
        assert orm == Range(BoundIncluded(timedelta(microseconds=1)), BoundExcluded(timedelta(hours=1, minutes=30)))
        # Beyond ``timedelta.max`` the value is kept, and Pydantic refuses it.
        with pytest.raises(ValidationError):
            M(rt=SdkRange(BoundIncluded(surrealdb.Duration(MAX_NS)), None))  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            M(rt=Range(BoundIncluded(Duration(MAX_NS)), None))
        # A string bound is not converted (Pydantic's own timedelta rules apply).
        with pytest.raises(ValidationError):
            M(rt=Range(BoundIncluded("1h"), None))

    def test_date_bounds_from_midnight_datetimes(self) -> None:
        class M(BaseModel):
            rday: Range[date] | None = None

        rday = M(rday=SdkRange(BoundIncluded(_JAN), BoundExcluded(_FEB))).rday  # type: ignore[arg-type]
        assert rday == Range(BoundIncluded(date(2026, 1, 1)), BoundExcluded(date(2026, 2, 1)))
        assert rday is not None and rday.begin is not None and rday.end is not None
        assert type(rday.begin.value) is date
        assert type(rday.end.value) is date

    def test_pydantic_dump_of_a_raw_sdk_range(self) -> None:
        """AC-5: an attribute holding a raw SDK ``Range`` dumps as an ORM ``Range`` — never a dict, no warning."""

        class M(BaseModel):
            r: Range[int] | None = None
            ra: Range | None = None  # type: ignore[type-arg]

        m = M()
        object.__setattr__(m, "r", SdkRange(BoundIncluded(9), BoundExcluded(17)))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            dumped = m.model_dump()
            text = m.model_dump_json()
        assert [str(w.message) for w in caught] == []
        assert type(dumped["r"]) is Range
        assert dumped["r"] == Range(BoundIncluded(9), BoundExcluded(17))
        assert '"9..17"' in text

        # A side that is no bound is kept as is (``encode`` refuses it later); never an exception.
        object.__setattr__(m, "ra", SdkRange("x", undefined))  # type: ignore[arg-type]
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            dumped = m.model_dump()
        assert [str(w.message) for w in caught] == []
        assert type(dumped["ra"]) is Range
        assert dumped["ra"].begin == "x"
        assert dumped["ra"].end is None
        # Fed back to validation, that side is refused like any other bad input (never AttributeError).
        with pytest.raises(ValidationError):
            M.model_validate(dumped)


class TestOpenSides:
    """One open-side rule (revision 9, C1): ``None``, ``Bound()``, ``undefined`` and a CBOR null."""

    @pytest.mark.parametrize("side", _CBOR_NULL_MARKERS, ids=_MARKER_IDS)
    def test_open_markers(self, side: Any) -> None:
        assert _is_open_side(side) is True

    @pytest.mark.parametrize(
        "side",
        [22, (22,), CBORSimpleValue(20), BoundIncluded(None), BoundExcluded(22), 0, "", False, [], Range()],
        ids=repr,
    )
    def test_not_open(self, side: Any) -> None:
        assert _is_open_side(side) is False

    def test_constructor_stores_none(self) -> None:
        assert Range(undefined, BoundExcluded(5)).begin is None  # type: ignore[arg-type]
        assert Range(BoundIncluded(1), CBORSimpleValue(22)).end is None  # type: ignore[arg-type]
        assert Range(_sdk.CBOR_NULL, None).begin is None  # type: ignore[arg-type]
        both = Range(Bound(), Bound())  # type: ignore[arg-type]
        assert both.begin is None
        assert both.end is None

    @pytest.mark.parametrize(
        "args",
        [(22, None), (CBORSimpleValue(20),), (None, 22), (BoundIncluded(1), CBORSimpleValue(0))],
        ids=repr,
    )
    def test_no_equality_trap(self, args: tuple[Any, ...]) -> None:
        """``CBORSimpleValue(22) == 22`` is true: the rule never compares with ``==``."""
        with pytest.raises(TypeError, match="BoundIncluded"):
            Range(*args)

    @pytest.mark.parametrize("marker", _CBOR_NULL_MARKERS, ids=_MARKER_IDS)
    def test_equality_with_each_marker(self, marker: Any) -> None:
        assert Range(None, BoundExcluded(5)) == SdkRange(marker, BoundExcluded(5))
        assert Range(BoundIncluded(1)) == SdkRange(BoundIncluded(1), marker)
        assert Range(None, BoundExcluded(5)) == Range(marker, BoundExcluded(5))
        assert Range(BoundIncluded(1), BoundExcluded(5)) != SdkRange(marker, BoundExcluded(5))

    @pytest.mark.parametrize("marker", _CBOR_NULL_MARKERS, ids=_MARKER_IDS)
    def test_field_stores_none(self, marker: Any) -> None:
        class M(BaseModel):
            r: Range[int] | None = None

        r = M(r=SdkRange(marker, BoundExcluded(5))).r  # type: ignore[arg-type]
        assert type(r) is Range
        assert r.begin is None
        assert r.end == BoundExcluded(5)
        r = M(r=SdkRange(BoundIncluded(1), marker)).r  # type: ignore[arg-type]
        assert r is not None
        assert r.end is None

    @pytest.mark.parametrize("marker", [undefined, CBORSimpleValue(22), _sdk.CBOR_NULL, Bound()], ids=repr)
    def test_dump_of_a_raw_sdk_range_with_an_open_marker(self, marker: Any) -> None:
        class M(BaseModel):
            r: Range[int] | None = None

        m = M()
        object.__setattr__(m, "r", SdkRange(marker, BoundExcluded(5)))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            dumped = m.model_dump()
            text = m.model_dump_json()
        assert [str(w.message) for w in caught] == []
        assert type(dumped["r"]) is Range
        assert dumped["r"].begin is None
        assert dumped["r"].end == BoundExcluded(5)
        assert json.loads(text) == {"r": "..5"}

    def test_literal_of_sdk_ranges_with_open_markers(self) -> None:
        assert to_surql_literal(SdkRange(BoundIncluded(1), undefined)) == "1.."
        assert to_surql_literal(SdkRange(BoundIncluded(1), _sdk.CBOR_NULL)) == "1.."
        assert to_surql_literal(SdkRange(CBORSimpleValue(22), BoundExcluded(5))) == "..5"
        assert to_surql_literal(SdkRange(Bound(), BoundExcluded(5))) == "..5"
        assert to_surql_literal(SdkRange(None, None)) == ".."  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="int"):
            to_surql_literal(SdkRange(BoundIncluded(1), 22))  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="CBORSimpleValue"):
            to_surql_literal(SdkRange(CBORSimpleValue(20), None))  # type: ignore[arg-type]


class TestReadHelpers:
    """``_timedelta_from_sdk`` / ``_duration_from_sdk`` (spec §6.3 item 5): pure, never raising."""

    def test_timedelta_from_sdk(self) -> None:
        value = _timedelta_from_sdk(surrealdb.Duration(5400 * 10**9 + 1))
        assert value == timedelta(hours=1, minutes=30)
        assert type(value) is timedelta
        assert _timedelta_from_sdk(surrealdb.Duration(1999)) == timedelta(microseconds=1)  # floor
        assert _timedelta_from_sdk(surrealdb.Duration(0)) == timedelta(0)
        top = surrealdb.Duration(_TIMEDELTA_MAX_US * 1000 + 999)
        assert _timedelta_from_sdk(top) == timedelta.max

    def test_timedelta_from_sdk_passes_through(self) -> None:
        class _SubSdk(surrealdb.Duration):
            pass

        beyond = surrealdb.Duration(_TIMEDELTA_MAX_US * 1000 + 1000)
        for value in [
            _SubSdk(5),
            Duration(5),
            beyond,
            surrealdb.Duration(MAX_NS),
            surrealdb.Duration(-1),
            surrealdb.Duration(-1000),
            surrealdb.Duration(1.5),
            surrealdb.Duration(True),
            surrealdb.Duration("5"),
            timedelta(1),
            None,
            "1h",
            5,
        ]:
            assert _timedelta_from_sdk(value) is value, value

    def test_duration_from_sdk(self) -> None:
        value = _duration_from_sdk(surrealdb.Duration(5400 * 10**9 + 1))
        assert type(value) is Duration
        assert value.elapsed == 5400 * 10**9 + 1
        assert _duration_from_sdk(surrealdb.Duration(0)) == Duration(0)
        top = _duration_from_sdk(surrealdb.Duration(MAX_NS))
        assert type(top) is Duration
        assert top.elapsed == MAX_NS

    def test_duration_from_sdk_passes_through(self) -> None:
        class _SubSdk(surrealdb.Duration):
            pass

        for value in [
            _SubSdk(5),
            Duration(5),
            surrealdb.Duration(MAX_NS + 1),
            surrealdb.Duration(-1),
            surrealdb.Duration(1.5),
            surrealdb.Duration(True),
            surrealdb.Duration("5"),
            timedelta(1),
            None,
            "1h",
        ]:
            assert _duration_from_sdk(value) is value, value


# --- Live literals (spec §6.4; AC-6) ----------------------------------------------------------------


class _SubDate(date):
    """A ``date`` subclass (``pendulum.Date``-like, D13) whose own ``isoformat`` must not leak."""

    def isoformat(self) -> str:
        return "not-a-date"


# v0.22.2's ``surql_literal._decimal``, copied verbatim (``git show origin/main:src/surreal_orm_lite/
# surql_literal.py``) with its two constants: the oracle the pre-check is compared with.
_DECIMAL_MANTISSA_MAX = 2**96 - 1
_DECIMAL_SCALE_MAX = 28


def _v020_decimal(value: Decimal) -> str:
    text = format(value, "f")
    whole, _, fraction = text.lstrip("-").partition(".")
    if len(fraction) > _DECIMAL_SCALE_MAX:
        # Trailing zeros carry no value: 1.000…0 is exactly 1, so drop them before judging.
        fraction = fraction.rstrip("0")
        text = text.rstrip("0").rstrip(".")
    if len(fraction) > _DECIMAL_SCALE_MAX:
        raise TypeError(
            f"Cannot inline Decimal({str(value)!r}): SurrealDB decimals keep at most "
            f"{_DECIMAL_SCALE_MAX} fractional digits and would round the rest away silently."
        )
    if int(whole + fraction) > _DECIMAL_MANTISSA_MAX:
        raise TypeError(f"Cannot inline Decimal({str(value)!r}): SurrealDB decimals hold a 96-bit mantissa.")
    return f"{text}dec"


def _outcome(fn: Callable[[Decimal], str], d: Decimal) -> tuple[str, str]:
    """``("ok", text)``, ``("TypeError", message)`` or ``(<exception class name>, "")``."""
    try:
        return "ok", fn(d)
    except TypeError as exc:
        return "TypeError", str(exc)
    except Exception as exc:  # the class name is the outcome
        return type(exc).__name__, ""


_DECIMAL_FIXED = [
    "1E+3",
    "1E-28",
    "1E-29",
    "1.000000000000000000000000000000",
    "1000E-2",
    "79228162514264337593543950335",
    "79228162514264337593543950336",
    "7.9228162514264337593543950336",
    "0E-30",
    "-0E-30",
    "0E+29",
    "-0E+29",
    "0E+40",
    "0E-29",
    "0E-28",
    "0E-10",
    "-0E-10",
    "0",
    "-0",
    "1" + "0" * 40 + "E-40",
    "1230000E-34",
]


def _decimal_corpus(count: int) -> list[Decimal]:
    """Seeded corpus of spec §3.8: 1–45 digits (30 % zeros), 10 % pure zeros, exponents −60…+35, 30 % negative."""
    rng = random.Random(7)
    values = []
    for _ in range(count):
        coefficient = "".join("0" if rng.random() < 0.3 else rng.choice("0123456789") for _ in range(rng.randint(1, 45)))
        if rng.random() < 0.1:
            coefficient = "0"
        sign = "-" if rng.random() < 0.3 else ""
        values.append(Decimal(f"{sign}{coefficient}E{rng.randint(-60, 35)}"))
    return values


class _NoFormat(Decimal):
    """A ``Decimal`` that fails if anything formats it: the pre-check must decide without its text."""

    def __format__(self, spec: str) -> str:
        raise AssertionError("formatted")


class TestRichLiterals:
    """``to_surql_literal`` rows added in v0.23.0 (spec §6.4)."""

    def test_durations(self) -> None:
        assert to_surql_literal(timedelta(hours=1, minutes=30, microseconds=1)) == "1h30m1us"
        assert to_surql_literal(Duration.parse("1h30m1ns")) == "1h30m1ns"
        assert to_surql_literal(surrealdb.Duration(5400 * 10**9 + 1)) == "1h30m1ns"
        assert to_surql_literal(Duration(MAX_NS)) == "584942417355y3w5d7h15s999ms999us999ns"
        assert to_surql_literal(surrealdb.Duration(MAX_NS)) == "584942417355y3w5d7h15s999ms999us999ns"
        assert to_surql_literal(timedelta(days=9)) == "1w2d"

    @pytest.mark.parametrize("zero", [timedelta(0), Duration(0), surrealdb.Duration(0)], ids=repr)
    def test_zero_duration_follows_the_probe(self, zero: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(encoding, "sdk_decodes_zero_duration", lambda: False)
        with pytest.raises(TypeError, match="zero duration"):
            to_surql_literal(zero)
        monkeypatch.setattr(encoding, "sdk_decodes_zero_duration", lambda: True)
        assert to_surql_literal(zero) == "0ns"

    @pytest.mark.parametrize(
        ("value", "match"),
        [
            (timedelta(seconds=-1), "negative duration"),
            (timedelta(microseconds=-1), "negative duration"),
            (surrealdb.Duration(-1), "negative duration"),
            (Duration._unchecked(-1), "negative duration"),
            (surrealdb.Duration(2**64 * 10**9), r"2\*\*64 - 1 seconds"),
            (Duration._unchecked(MAX_NS + 1), r"2\*\*64 - 1 seconds"),
            (surrealdb.Duration(1.5), "int number of nanoseconds"),
            (surrealdb.Duration(True), "int number of nanoseconds"),
        ],
        ids=repr,
    )
    def test_duration_refusals(self, value: Any, match: str) -> None:
        with pytest.raises(TypeError, match=match):
            to_surql_literal(value)

    def test_dates(self) -> None:
        assert to_surql_literal(date(2026, 1, 2)) == "d'2026-01-02T00:00:00Z'"
        assert to_surql_literal(_SubDate(2026, 1, 2)) == "d'2026-01-02T00:00:00Z'"
        # A datetime still renders as before, with its time.
        assert to_surql_literal(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)) == "d'2026-01-02T03:04:05Z'"
        assert to_surql_literal(datetime(2026, 1, 2)) == "d'2026-01-02T00:00:00Z'"

    def test_sets(self) -> None:
        assert to_surql_literal({"b", "a"}) == "['a', 'b']"
        assert to_surql_literal(frozenset({2, 1})) == "[1, 2]"
        assert to_surql_literal(set()) == "[]"
        assert to_surql_literal({1, "a"}) in ("[1, 'a']", "['a', 1]")
        assert to_surql_literal({timedelta(hours=2), timedelta(hours=1)}) == "[1h, 2h]"
        # Sorting a NaN raises ``decimal.InvalidOperation``: iteration order instead, then the NaN
        # item is refused by its own row — a ``TypeError``, never ``InvalidOperation``.
        with pytest.raises(TypeError, match="NaN"):
            to_surql_literal({Decimal("NaN"), Decimal(1)})
        # Lists and tuples keep their order (unchanged).
        assert to_surql_literal(["b", "a"]) == "['b', 'a']"

    @pytest.mark.parametrize(
        ("value", "text"),
        [
            (Range(BoundIncluded(1), BoundExcluded(5)), "1..5"),
            (Range(BoundIncluded(1), BoundIncluded(5)), "1..=5"),
            (Range(BoundExcluded(1), BoundExcluded(5)), "1>..5"),
            (Range(None, BoundExcluded(5)), "..5"),
            (Range(BoundIncluded(1)), "1.."),
            (Range(), ".."),
            (SdkRange(BoundIncluded(1), BoundExcluded(5)), "1..5"),
            (SdkRange(BoundIncluded(1), undefined), "1.."),
            (SdkRange(BoundIncluded(1), _sdk.CBOR_NULL), "1.."),
            (SdkRange(CBORSimpleValue(22), BoundExcluded(5)), "..5"),
            (SdkRange(Bound(), BoundExcluded(5)), "..5"),
            (Range(BoundIncluded(timedelta(minutes=1)), BoundExcluded(Duration.parse("1h"))), "1m..1h"),
            (SdkRange(BoundIncluded(surrealdb.Duration(60 * 10**9)), undefined), "1m.."),
            (Range(BoundIncluded(Decimal("1.5")), BoundExcluded(Decimal("2"))), "1.5dec..2dec"),
            (Range(BoundIncluded("a"), BoundExcluded("m")), "'a'..'m'"),
            (Range(BoundIncluded(_JAN)), "d'2026-01-01T00:00:00Z'.."),
            (Range(BoundIncluded(date(2026, 1, 1))), "d'2026-01-01T00:00:00Z'.."),
            (
                Range(BoundIncluded(_SubDate(2026, 1, 1)), BoundIncluded(_SubDate(2026, 2, 1))),
                "d'2026-01-01T00:00:00Z'..=d'2026-02-01T00:00:00Z'",
            ),
            (Range(BoundIncluded(-5), BoundExcluded(-1)), "-5..-1"),
            (Range(BoundIncluded(1.5), BoundExcluded(2.5)), "1.5..2.5"),
        ],
        ids=repr,
    )
    def test_ranges(self, value: Any, text: str) -> None:
        assert to_surql_literal(value) == text

    @pytest.mark.parametrize(
        ("bound", "type_name"),
        [
            (True, "bool"),
            (UUID("0190c4b6-0000-7000-8000-000000000000"), "UUID"),
            (RecordID("t", 1), "RecordID"),
            ([1], "list"),
            ({"a": 1}, "dict"),
            (None, "NoneType"),
            (Range(), "Range"),
            (SdkRange(BoundIncluded(1), BoundExcluded(2)), "Range"),
            (float("nan"), "float"),
            (float("inf"), "float"),
            (b"x", "bytes"),
            (CBORTag(14, [1, 0]), "CBORTag"),
        ],
        ids=repr,
    )
    def test_range_bound_values_are_a_closed_set(self, bound: Any, type_name: str) -> None:
        for value in (Range(BoundIncluded(bound), None), SdkRange(BoundIncluded(1), BoundExcluded(bound))):
            with pytest.raises(TypeError, match=rf"\b{type_name}\b"):
                to_surql_literal(value)
        assert str(Range(BoundIncluded(bound), None)) == repr(Range(BoundIncluded(bound), None))

    def test_range_side_that_is_not_a_bound(self) -> None:
        for side in (22, "x", CBORSimpleValue(20), 1.5):
            with pytest.raises(TypeError, match=type(side).__name__):
                to_surql_literal(SdkRange(BoundIncluded(1), side))
        assert str(Range(BoundIncluded(True), None)) == repr(Range(BoundIncluded(True), None))

    def test_inline_variables(self) -> None:
        from surreal_orm_lite.surql_literal import inline_variables

        sql = "ttl >= $a AND day = $b AND n IN $c AND tags CONTAINSALL $d"
        variables = {"a": timedelta(hours=1), "b": date(2026, 1, 2), "c": Range(BoundIncluded(1)), "d": {"y", "x"}}
        assert inline_variables(sql, variables) == (
            "ttl >= 1h AND day = d'2026-01-02T00:00:00Z' AND n IN 1.. AND tags CONTAINSALL ['x', 'y']"
        )

    def test_supported_list_names_the_new_types(self) -> None:
        from datetime import time

        with pytest.raises(TypeError) as info:
            to_surql_literal(time(1))
        message = str(info.value)
        for name in ("date", "timedelta", "Duration", "Range", "set"):
            assert name in message, name

    # --- Decimal pre-check (spec §6.4, §3.8; D8) -------------------------------------------------

    def test_decimal_decided_without_formatting(self) -> None:
        with pytest.raises(TypeError, match="28 fractional digits") as info:
            to_surql_literal(_NoFormat("1E-1000000"))
        assert info.value.args[0] == _outcome(_v020_decimal, Decimal("1E-1000000"))[1]
        with pytest.raises(TypeError, match="96-bit mantissa"):
            to_surql_literal(_NoFormat("1E+1000000"))
        assert to_surql_literal(_NoFormat("0E-999999999")) == "0dec"
        assert to_surql_literal(_NoFormat("-0E-999999999")) == "-0dec"

    @pytest.mark.parametrize("text", _DECIMAL_FIXED)
    def test_decimal_matches_v0_20_fixed(self, text: str) -> None:
        d = Decimal(text)
        assert _outcome(to_surql_literal, d) == _outcome(_v020_decimal, d)

    def test_decimal_matches_v0_20_seeded(self) -> None:
        differences = [
            (str(d), mine, oracle)
            for d in _decimal_corpus(20_000)
            if (mine := _outcome(to_surql_literal, d)) != (oracle := _outcome(_v020_decimal, d))
        ]
        assert differences == []

    def test_decimal_beyond_the_int_string_limit(self) -> None:
        """The one intended change (D8): v0.20.0 raised ``int()``'s ``ValueError`` there.

        Built from a tuple: the pure-Python ``decimal`` parses ``Decimal(str)`` through ``int()`` and
        would refuse those 5 000 digits before the ORM sees them.
        """
        d = Decimal((0, (1,) * 5000, -10))
        assert str(d) == "1" * 4990 + "." + "1" * 10
        assert _outcome(_v020_decimal, d)[0] == "ValueError"
        with pytest.raises(TypeError, match="96-bit mantissa"):
            to_surql_literal(d)


# Every literal of spec §3.3: (value, literal, the parameter the server must find equal). Open range
# sides go out as the protocol's CBOR null (spec §3.9), never ``undefined``.
_N = _sdk.CBOR_NULL
_TAG = _sdk.TAG_DURATION_COMPACT
_LONG = Duration.parse("1y2w3d4h5m6s7ms8us9ns")
LIVE_LITERALS: list[tuple[Any, str, Any]] = [
    (Duration.parse("1h30m1ns"), "1h30m1ns", CBORTag(_TAG, [5400, 1])),
    (_LONG, "1y2w3d4h5m6s7ms8us9ns", CBORTag(_TAG, list(divmod(_LONG.elapsed, 10**9)))),
    (Duration(MAX_NS), "584942417355y3w5d7h15s999ms999us999ns", CBORTag(_TAG, [2**64 - 1, 999_999_999])),
    (timedelta(hours=1, minutes=30, microseconds=1), "1h30m1us", CBORTag(_TAG, [5400, 1000])),
    (surrealdb.Duration(5400 * 10**9 + 1), "1h30m1ns", CBORTag(_TAG, [5400, 1])),
    (date(2026, 1, 2), "d'2026-01-02T00:00:00Z'", datetime(2026, 1, 2, tzinfo=UTC)),
    (_SubDate(2026, 1, 2), "d'2026-01-02T00:00:00Z'", datetime(2026, 1, 2, tzinfo=UTC)),
    (Range(BoundIncluded(1), BoundExcluded(5)), "1..5", SdkRange(BoundIncluded(1), BoundExcluded(5))),
    (Range(BoundIncluded(1), BoundIncluded(5)), "1..=5", SdkRange(BoundIncluded(1), BoundIncluded(5))),
    (Range(BoundExcluded(1), BoundExcluded(5)), "1>..5", SdkRange(BoundExcluded(1), BoundExcluded(5))),
    (Range(None, BoundExcluded(5)), "..5", SdkRange(_N, BoundExcluded(5))),
    (Range(None, BoundIncluded(5)), "..=5", SdkRange(_N, BoundIncluded(5))),
    (Range(BoundIncluded(1)), "1..", SdkRange(BoundIncluded(1), _N)),
    (Range(BoundExcluded(1)), "1>..", SdkRange(BoundExcluded(1), _N)),
    (Range(), "..", SdkRange(_N, _N)),
    (SdkRange(BoundIncluded(1), undefined), "1..", SdkRange(BoundIncluded(1), _N)),
    (
        Range(BoundIncluded(timedelta(minutes=1)), BoundExcluded(Duration.parse("1h"))),
        "1m..1h",
        SdkRange(BoundIncluded(CBORTag(_TAG, [60, 0])), BoundExcluded(CBORTag(_TAG, [3600, 0]))),
    ),
    (
        Range(BoundIncluded(Decimal("1.5")), BoundExcluded(Decimal("2"))),
        "1.5dec..2dec",
        SdkRange(BoundIncluded(Decimal("1.5")), BoundExcluded(Decimal("2"))),
    ),
    (Range(BoundIncluded(_JAN)), "d'2026-01-01T00:00:00Z'..", SdkRange(BoundIncluded(_JAN), _N)),
    (Range(BoundIncluded(date(2026, 1, 1))), "d'2026-01-01T00:00:00Z'..", SdkRange(BoundIncluded(_JAN), _N)),
    (Range(BoundIncluded("a"), BoundExcluded("m")), "'a'..'m'", SdkRange(BoundIncluded("a"), BoundExcluded("m"))),
    ({"b", "a"}, "['a', 'b']", ["a", "b"]),
    (frozenset({2, 1}), "[1, 2]", [1, 2]),
    (Decimal("0E-30"), "0dec", CBORTag(_sdk.TAG_DECIMAL_STRING, "0")),
    (Decimal("-0E-30"), "-0dec", CBORTag(_sdk.TAG_DECIMAL_STRING, "-0")),
]


class TestRichLiteralsE2E:
    """Each new literal means the value it renders (``RETURN <lit> == $p``), raw SDK over HTTP, both lines."""

    @pytest.mark.parametrize(
        ("value", "text", "param"), LIVE_LITERALS, ids=[f"{type(v).__name__}:{t}" for v, t, _ in LIVE_LITERALS]
    )
    async def test_literal_equals_the_bound_parameter(self, value: Any, text: str, param: Any) -> None:
        assert to_surql_literal(value) == text
        client = await _http_client()
        assert await client.query(f"RETURN {text} == $p;", {"p": param}) is True

    async def test_zero_literal_when_the_sdk_reads_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The ``0ns`` a fixed SDK would allow means the compact zero (the result is a bool: nothing to decode)."""
        monkeypatch.setattr(encoding, "sdk_decodes_zero_duration", lambda: True)
        text = to_surql_literal(Duration(0))
        client = await _http_client()
        assert await client.query(f"RETURN {text} == $p;", {"p": CBORTag(_TAG, [0, 0])}) is True

    async def test_range_literal_precedence(self) -> None:
        one_to_five = to_surql_literal(Range(BoundIncluded(1), BoundExcluded(5)))
        up_to_five = to_surql_literal(Range(None, BoundIncluded(5)))
        client = await _http_client()
        assert await client.query(f"RETURN 3 IN {one_to_five} AND 9 NOT IN {one_to_five};", {}) is True
        assert await client.query(f"RETURN 5 IN {up_to_five} OR false;", {}) is True
        assert await client.query(f"RETURN 7 IN {to_surql_literal(Range(BoundIncluded(5)))};", {}) is True


# --- encode(): what the servers store (spec §6.1; AC-9, AC-10, AC-12) -------------------------------

_SD = surrealdb.Duration
_EXPONENT_DECIMALS = ["1E+3", "1E-7", "-5E-7", "0E-10", "1E-31", "1.234567890123456789012345E-7", "0E+30", "-0E-30"]

# (id, value given to ``encode``, what the server gives back, ``RETURN <string> $v`` or None)
_WIRE_FORMS: list[tuple[str, Any, Any, str | None]] = [
    ("Duration(1)", Duration(1), _SD(1), "1ns"),
    ("timedelta", timedelta(hours=1, minutes=30), _SD(5400 * 10**9), "1h30m"),
    ("Duration(MAX)", Duration(MAX_NS), _SD(MAX_NS), "584942417355y3w5d7h15s999ms999µs999ns"),
    ("sdk-Duration", surrealdb.Duration(5400 * 10**9 + 1), _SD(5400 * 10**9 + 1), "1h30m1ns"),
    ("1E+3", Decimal("1E+3"), Decimal("1000"), None),
    ("1E-7", Decimal("1E-7"), Decimal("0.0000001"), None),
    ("-5E-7", Decimal("-5E-7"), Decimal("-0.0000005"), None),
    ("0E-10", Decimal("0E-10"), Decimal(0), None),
    ("1E-31", Decimal("1E-31"), Decimal(0), None),
    ("cut", Decimal("1.234567890123456789012345E-7"), Decimal("0.0000001234567890123456789012"), None),
    ("0E+30", Decimal("0E+30"), Decimal(0), None),
    ("-0E-30", Decimal("-0E-30"), Decimal(0), None),
    (
        "70-digits",
        Decimal("79228162514264337593543950335.4" + "9" * 40),
        Decimal("79228162514264337593543950335"),
        None,
    ),
    ("date", date(2026, 1, 2), datetime(2026, 1, 2, tzinfo=UTC), None),
    ("set", {"b", "a"}, ["a", "b"], None),
    ("1..5", Range(BoundIncluded(1), BoundExcluded(5)), SdkRange(BoundIncluded(1), BoundExcluded(5)), "1..5"),
    ("..5", Range(None, BoundExcluded(5)), SdkRange(None, BoundExcluded(5)), "..5"),
    ("1..", Range(BoundIncluded(1)), SdkRange(BoundIncluded(1), None), "1.."),
    ("..", Range(), SdkRange(None, None), ".."),
    ("1>..", Range(BoundExcluded(1)), SdkRange(BoundExcluded(1), None), "1>.."),
    ("..=5", Range(None, BoundIncluded(5)), SdkRange(None, BoundIncluded(5)), "..=5"),
    (
        "1m..1h",
        Range(BoundIncluded(timedelta(minutes=1)), BoundExcluded(timedelta(hours=1))),
        SdkRange(BoundIncluded(_SD(60 * 10**9)), BoundExcluded(_SD(3600 * 10**9))),
        "1m..1h",
    ),
    ("1m..", Range(BoundIncluded(timedelta(minutes=1))), SdkRange(BoundIncluded(_SD(60 * 10**9)), None), "1m.."),
]


class TestWireFormsE2E:
    """Each encoded value is stored and read back as the server's own type — raw SDK, WS and HTTP, both lines."""

    @pytest.mark.parametrize("transport", ["ws", "http"])
    @pytest.mark.parametrize(("value", "expected", "text"), [case[1:] for case in _WIRE_FORMS], ids=[c[0] for c in _WIRE_FORMS])
    async def test_round_trip(self, transport: str, value: Any, expected: Any, text: str | None) -> None:
        async with orm_client("rf_wire") as ws:
            client = ws if transport == "ws" else await _http_client()
            encoded = encoding.encode(value)
            assert await client.query("CREATE rf_wire:1 SET v = $v RETURN NONE;", {"v": encoded}) == []
            read = await client.query("SELECT VALUE v FROM ONLY rf_wire:1;", {})
            assert type(read) is type(expected)
            assert read == expected
            if isinstance(expected, SdkRange):
                assert (read.begin is None) == (expected.begin is None)
                assert (read.end is None) == (expected.end is None)
            if text is not None:
                assert await client.query("RETURN <string> $v;", {"v": encoded}) == text
            assert await client.query("RETURN 1;", {}) == 1

    @pytest.mark.parametrize("text", _EXPONENT_DECIMALS)
    async def test_decimal_cut_agrees_with_server(self, text: str) -> None:
        """The server reads the encoded (cut) text exactly as it reads the full fixed-point text."""
        value = Decimal(text)
        encoded = encoding.encode(value)
        assert isinstance(encoded, CBORTag)
        full = CBORTag(_sdk.TAG_DECIMAL_STRING, format(value, "f"))
        client = await _http_client()
        ours = await client.query("RETURN <string> $v;", {"v": encoded})
        theirs = await client.query("RETURN <string> $v;", {"v": full})
        assert ours == theirs


class TestZeroProbeE2E:
    async def test_probe_matches_server(self) -> None:
        """``RETURN 0ns`` fails to decode (``IndexError``) exactly when the local probe answers ``False``."""
        client = await _http_client()
        if encoding.sdk_decodes_zero_duration():
            result = await client.query("RETURN 0ns;", {})
            assert isinstance(result, surrealdb.Duration) and result.elapsed == 0
        else:
            with pytest.raises(IndexError):
                await client.query("RETURN 0ns;", {})


class TestNativeSetsE2E:
    """A Python set goes out as a sorted array; what a ``TYPE set<…>`` column does with it depends on the line."""

    def test_sdk_still_cannot_decode_native_sets(self) -> None:
        assert not _sdk_decodes_native_set(), (
            "the SDK now decodes native sets (CBOR tag 56): update README behaviour row "
            "'TYPE set<…> columns' and section 24's array<…> advice"
        )

    async def test_set_column_by_line(self) -> None:
        async with orm_client("rf_set"):
            version = await _server_version()
            client = await _http_client()
            await client.query("DEFINE FIELD tags ON rf_set TYPE set<string>;", {})
            tags = encoding.encode({"b", "a"})
            assert tags == ["a", "b"]
            write = "CREATE rf_set:1 SET tags = $t RETURN NONE;"
            read = "SELECT VALUE tags FROM ONLY rf_set:1;"
            if version < (3, 0, 0):
                await client.query(write, {"t": tags})
                assert await client.query(read, {}) == ["a", "b"]
            elif version < (3, 3, 0):
                with pytest.raises(_sdk.ServerError, match=r"\bset\b"):
                    await client.query(write, {"t": tags})
            else:
                await client.query(write, {"t": tags})
                if _sdk_decodes_native_set():
                    assert sorted(await client.query(read, {})) == ["a", "b"]
                else:
                    with pytest.raises(BufferError):
                        await client.query(read, {})

    async def test_array_column_everywhere(self) -> None:
        async with orm_client("rf_arr"):
            client = await _http_client()
            await client.query("DEFINE FIELD tags ON rf_arr TYPE array<string> VALUE $value.distinct();", {})
            await client.query("CREATE rf_arr:1 SET tags = $t RETURN NONE;", {"t": encoding.encode({"b", "a"})})
            assert await client.query("SELECT VALUE tags FROM ONLY rf_arr:1;", {}) == ["a", "b"]
