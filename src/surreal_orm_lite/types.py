"""Rich SurrealDB value types the ORM adds on top of the SDK (v0.23.0).

``Duration`` is a SurrealDB ``duration`` with the server's nanosecond precision: declare it on a
field where ``timedelta`` (microseconds) would lose digits. It is a standalone class, not a subclass
of the SDK's ``surrealdb.Duration`` (a subclass would inherit dataclass-ness, which Pydantic then
flattens into a dict), but it has every public member of the SDK's class under the same name and
signature, converts from it and compares equal to it.
"""

from __future__ import annotations

import functools
import re
from datetime import timedelta
from typing import Any, ClassVar, Self

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import core_schema

from ._sdk import SdkDuration
from .exceptions import InvalidDurationError

__all__ = ["Duration"]

_NS_PER_SECOND = 10**9

# Units in descending size: the order ``to_string`` writes them in. ``µs`` is accepted on parse.
_UNITS: tuple[tuple[str, int], ...] = (
    ("y", 365 * 86_400 * _NS_PER_SECOND),
    ("w", 7 * 86_400 * _NS_PER_SECOND),
    ("d", 86_400 * _NS_PER_SECOND),
    ("h", 3_600 * _NS_PER_SECOND),
    ("m", 60 * _NS_PER_SECOND),
    ("s", _NS_PER_SECOND),
    ("ms", 1_000_000),
    ("us", 1_000),
    ("ns", 1),
)
_NS_PER_UNIT: dict[str, int] = {**dict(_UNITS), "µs": 1_000}

# The strict SurrealQL duration syntax: one or more <ASCII digits><unit>, in any order. ``[0-9]``,
# never ``\d`` (which also matches non-ASCII digits such as "٣"); ``ms`` is tried before ``m``.
_DURATION_TEXT = re.compile(r"(?:[0-9]+(?:ns|us|µs|ms|s|m|h|d|w|y))+")
_DURATION_PART = re.compile(r"([0-9]+)(ns|us|µs|ms|s|m|h|d|w|y)")
_MAX_TEXT_LENGTH = 256

# The largest timedelta, in whole microseconds (``to_timedelta`` refuses anything above it).
_TIMEDELTA_MAX_US = (timedelta.max.days * 86_400 + timedelta.max.seconds) * 1_000_000 + timedelta.max.microseconds


def _text_nanoseconds(text: str) -> int:
    """Nanoseconds of a SurrealQL duration string — the one grammar ``Duration`` accepts.

    At most 256 characters (the raw input, before stripping); surrounding whitespace removed with
    ``str.strip()``, as the server does; then a full match of the strict syntax (inner whitespace
    never matches). The sum is not range-checked here. ``InvalidDurationError`` on refusal.
    """
    if len(text) > _MAX_TEXT_LENGTH:
        raise InvalidDurationError(f"a duration string has at most {_MAX_TEXT_LENGTH} characters, got {len(text)}")
    stripped = text.strip()
    if not _DURATION_TEXT.fullmatch(stripped):
        raise InvalidDurationError(
            f"{text!r} is not a SurrealQL duration: expected <digits><unit> parts such as '1h30m' "
            "(units y, w, d, h, m, s, ms, us, µs, ns; lower case, no inner spaces)"
        )
    return sum(int(count) * _NS_PER_UNIT[unit] for count, unit in _DURATION_PART.findall(stripped))


def _check_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, not {type(value).__name__}")


@functools.total_ordering
class Duration:
    """A SurrealDB ``duration``: a non-negative length of time with nanosecond precision (v0.23.0).

    ``timedelta`` stops at microseconds; declare ``Duration`` to keep the server's nanoseconds.
    Not the SDK's ``surrealdb.Duration`` (which SDK 2.0.0 cannot send), but converts from it.

    ``Duration(elapsed)`` takes a number of **nanoseconds**; ``Duration.parse("1h30m")`` a SurrealQL
    duration string, or ``Duration.parse(seconds, nanoseconds)`` numbers. ``str()`` gives the lossless
    SurrealQL text (``"1h30m1ns"``). Equal to an ORM or SDK ``Duration`` with the same ``elapsed``
    (``Duration(5) == surrealdb.Duration(5)``; the SDK's own ``__eq__`` answers ``False`` the other
    way round, so keep the ORM value on the left), hashable, ordered.

    Value refusals raise ``InvalidDurationError`` (a ``ValueError`` and a
    ``surrealdb.InvalidDurationError``); a wrong argument type raises ``TypeError``.

    As a Pydantic field it accepts a ``Duration``, an SDK ``Duration``, a ``timedelta`` (not
    negative) and a SurrealQL string; a bare number is refused (its unit would be a guess). JSON
    mode writes the string form.
    """

    __slots__ = ("elapsed",)

    #: The server's maximum: 2**64 - 1 seconds plus 999 999 999 nanoseconds.
    MAX_NANOSECONDS: ClassVar[int] = (2**64 - 1) * _NS_PER_SECOND + 999_999_999

    elapsed: int

    def __init__(self, elapsed: int = 0) -> None:
        """A duration of ``elapsed`` nanoseconds.

        ``TypeError`` for a non-``int`` (``bool`` included); ``InvalidDurationError`` when negative
        or above ``MAX_NANOSECONDS``.
        """
        _check_int("Duration() elapsed nanoseconds", elapsed)
        if elapsed < 0:
            raise InvalidDurationError(f"a duration cannot be negative, got {elapsed} ns")
        if elapsed > self.MAX_NANOSECONDS:
            raise InvalidDurationError(f"{elapsed} ns is above the largest SurrealDB duration ({self.MAX_NANOSECONDS} ns)")
        self.elapsed = int(elapsed)

    @classmethod
    def parse(cls, value: str | int, nanoseconds: int = 0) -> Self:
        """Build a duration from a SurrealQL string or a number of seconds, plus ``nanoseconds``.

        The SDK's signature (``surrealdb.Duration.parse``): an ``int`` is a number of **seconds**,
        and ``nanoseconds`` is added in both cases — ``parse(5) == Duration(5 * 10**9)``,
        ``parse(1, 5) == Duration(1_000_000_005)``, ``parse("1h", 5)`` is 1 h + 5 ns and
        ``parse(*d.to_compact()) == d``.

        A string: at most 256 characters (the raw input); surrounding whitespace is removed with
        ``str.strip()``, as the server does (``" 1h30m "`` is ``1h30m``); then the strict SurrealQL
        syntax must match in full — one or more ``<ASCII digits><unit>``, units ``y w d h m s ms us
        µs ns`` (lower case), in any order, summed: ``"1h30m"``, ``"90m"``, ``"0ns"``.

        Unlike ``surrealdb.Duration.parse`` (SDK 2.0.0), which lower-cases, accepts any Unicode
        digit and ignores what it cannot read (``"1.5h"`` is 5 h there, ``"1H"`` 1 h, ``"1h 30m"``
        1h30m), anything outside that syntax is refused, and so are a ``bool``, a negative number
        and a total above ``MAX_NANOSECONDS``. It is a classmethod (the SDK's is a staticmethod;
        the call syntax is the same).

        Raises ``InvalidDurationError`` (a ``ValueError`` and a ``surrealdb.InvalidDurationError``)
        for a refused string, more than 256 characters, a negative ``value`` or ``nanoseconds``,
        or a total above ``MAX_NANOSECONDS``; ``TypeError`` when ``value`` is neither a ``str`` nor
        an ``int`` or ``nanoseconds`` is not an ``int`` (a ``bool`` is refused for both).
        """
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise TypeError(f"Duration.parse() takes a str or an int of seconds, not {type(value).__name__}")
        _check_int("Duration.parse() nanoseconds", nanoseconds)
        if isinstance(value, int) and value < 0:
            raise InvalidDurationError(f"a duration cannot be negative, got {value} s")
        if nanoseconds < 0:
            raise InvalidDurationError(f"a duration cannot be negative, got {nanoseconds} ns")
        total = (_text_nanoseconds(value) if isinstance(value, str) else value * _NS_PER_SECOND) + nanoseconds
        return cls(total)

    @classmethod
    def from_timedelta(cls, value: timedelta) -> Self:
        """The same length of time, exact to the microsecond (integer arithmetic, never a float).

        ``InvalidDurationError`` for a negative ``timedelta``; ``TypeError`` for anything else.
        """
        if not isinstance(value, timedelta):
            raise TypeError(f"Duration.from_timedelta() takes a timedelta, not {type(value).__name__}")
        elapsed = (value.days * 86_400 + value.seconds) * _NS_PER_SECOND + value.microseconds * 1_000
        if elapsed < 0:
            raise InvalidDurationError(f"a duration cannot be negative, got {value!r}")
        return cls(elapsed)

    def to_timedelta(self) -> timedelta:
        """A ``timedelta`` of the same length, floored to the microsecond (``timedelta`` has no ns).

        ``ValueError`` when the duration is beyond ``timedelta.max`` (about 2.7 million years).
        """
        microseconds = self.elapsed // 1_000
        if microseconds > _TIMEDELTA_MAX_US:
            raise ValueError(f"{self} is beyond timedelta.max ({timedelta.max}): keep it as a Duration")
        return timedelta(microseconds=microseconds)

    def get_seconds_and_nano(self) -> tuple[int, int]:
        """``(seconds, nanoseconds)``, computed with integer ``divmod`` — exact for every value.

        The SDK's ``surrealdb.Duration.get_seconds_and_nano()`` (2.0.0) divides in floating point
        and returns a negative nanosecond part above about 2**53 ns.
        """
        seconds, nanos = divmod(self.elapsed, _NS_PER_SECOND)
        return seconds, nanos

    def to_compact(self) -> list[int]:
        """``[seconds, nanoseconds]`` — SurrealDB's compact (CBOR tag 14) form, exact; a new list each call.

        The SDK's ``surrealdb.Duration.to_compact()`` (2.0.0) returns ``[seconds]`` and loses the
        nanoseconds. ``Duration.parse(*d.to_compact()) == d``.
        """
        return list(divmod(self.elapsed, _NS_PER_SECOND))

    def to_string(self) -> str:
        """The lossless SurrealQL text, every unit written: ``"1h30m1ns"``; ``"0ns"`` for zero.

        The SDK's ``surrealdb.Duration.to_string()`` (2.0.0) keeps only the largest unit
        (``5400 s`` → ``"1h"``). ``Duration.parse(d.to_string()) == d``.
        """
        if not self.elapsed:
            return "0ns"
        parts: list[str] = []
        rest = self.elapsed
        for unit, size in _UNITS:
            count, rest = divmod(rest, size)
            if count:
                parts.append(f"{count}{unit}")
        return "".join(parts)

    # Whole units, floored, as in the SDK (a year is 365 days).
    @property
    def nanoseconds(self) -> int:
        return self.elapsed

    @property
    def microseconds(self) -> int:
        return self.elapsed // _NS_PER_UNIT["us"]

    @property
    def milliseconds(self) -> int:
        return self.elapsed // _NS_PER_UNIT["ms"]

    @property
    def seconds(self) -> int:
        return self.elapsed // _NS_PER_UNIT["s"]

    @property
    def minutes(self) -> int:
        return self.elapsed // _NS_PER_UNIT["m"]

    @property
    def hours(self) -> int:
        return self.elapsed // _NS_PER_UNIT["h"]

    @property
    def days(self) -> int:
        return self.elapsed // _NS_PER_UNIT["d"]

    @property
    def weeks(self) -> int:
        return self.elapsed // _NS_PER_UNIT["w"]

    @property
    def years(self) -> int:
        return self.elapsed // _NS_PER_UNIT["y"]

    def __str__(self) -> str:
        return self.to_string()

    def __repr__(self) -> str:
        return f"Duration(elapsed={self.elapsed})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (Duration, SdkDuration)):
            return bool(self.elapsed == other.elapsed)
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        if isinstance(other, (Duration, SdkDuration)):
            return bool(self.elapsed < other.elapsed)
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.elapsed)

    @classmethod
    def _pydantic_validate(cls, value: Any) -> Duration:
        """The field validator: a refusal is a ``ValueError``, so Pydantic reports a ``ValidationError``."""
        if isinstance(value, Duration):
            return value
        if isinstance(value, SdkDuration):
            elapsed = value.elapsed
            if isinstance(elapsed, bool) or not isinstance(elapsed, int):
                raise ValueError(f"an SDK Duration must hold an int number of nanoseconds, not {elapsed!r}")
            return cls(elapsed)
        if isinstance(value, timedelta):
            return cls.from_timedelta(value)
        if isinstance(value, str):
            return cls.parse(value)
        raise ValueError(
            "a Duration takes a Duration, a timedelta or a SurrealQL duration string such as '1h30m', "
            f"not {type(value).__name__}"
        )

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
        return core_schema.no_info_plain_validator_function(
            cls._pydantic_validate,
            serialization=core_schema.plain_serializer_function_ser_schema(str, when_used="json"),
        )

    @classmethod
    def __get_pydantic_json_schema__(cls, schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        return {"type": "string", "examples": ["1h30m"]}
