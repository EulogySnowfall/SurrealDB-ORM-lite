"""Rich field types (v0.23.0): the ORM's ``Duration`` and ``Range[T]`` and the wire helpers.

Unit tests first (no database); the raw-SDK E2E classes that pin what the servers do with each
wire form are suffixed ``E2E``.
"""

import inspect
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
import surrealdb
from pydantic import BaseModel, ValidationError

import surreal_orm_lite
from surreal_orm_lite import Duration, InvalidDurationError

MAX_NS = Duration.MAX_NANOSECONDS


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
