"""Rich SurrealDB value types the ORM adds on top of the SDK (v0.23.0).

``Duration`` is a SurrealDB ``duration`` with the server's nanosecond precision: declare it on a
field where ``timedelta`` (microseconds) would lose digits. It is a standalone class, not a subclass
of the SDK's ``surrealdb.Duration`` (a subclass would inherit dataclass-ness, which Pydantic then
flattens into a dict), but it has every public member of the SDK's class under the same name and
signature, converts from it and compares equal to it.

``Range[T]`` is a SurrealDB ``range`` (``9..17``, ``d'2026-01-01T00:00:00Z'..``): two bounds, each
the SDK's ``BoundIncluded``/``BoundExcluded`` or ``None`` for an open side. As a Pydantic field it
validates each bound value as ``T`` and reads the SurrealQL literal text it writes in JSON mode.
"""

from __future__ import annotations

import functools
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar, Generic, Self, TypeVar, get_args

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler, TypeAdapter
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import core_schema

from ._sdk import Bound, BoundExcluded, BoundIncluded, SdkDuration, SdkRange
from .exceptions import InvalidDurationError

__all__ = ["Duration", "Range"]

T = TypeVar("T")

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


# --- Range[T] (spec §4.3) ------------------------------------------------------------------------


def _side(value: object, name: str) -> BoundIncluded | BoundExcluded | None:
    """A range side as ``Range`` stores it: an included or excluded bound, or ``None`` when open.

    A bare SDK ``Bound()`` (the SDK docs' spelling of an open side) is read as ``None``.
    ``TypeError`` for anything else.
    """
    if value is None or isinstance(value, (BoundIncluded, BoundExcluded)):
        return value
    if type(value) is Bound:
        return None
    raise TypeError(
        f"Range() {name} must be BoundIncluded(value), BoundExcluded(value) or None (open), "
        f"not {type(value).__name__}: write BoundIncluded({value!r})"
    )


class Range(Generic[T]):
    """A SurrealDB ``range``: two bounds, each included, excluded or open (v0.23.0).

    ``Range(BoundIncluded(9), BoundExcluded(17))`` is SurrealQL's ``9..17``; ``None`` is an open
    side (``Range(BoundIncluded(1))`` is ``1..``, ``Range()`` is ``..``). The bound classes are the
    SDK's, re-exported by ``surreal_orm_lite``. ``str()`` gives the SurrealQL literal (``"1..5"``,
    ``"1..=5"``, ``"1>..5"``, ``"1m..1h"``), or ``repr()`` when a bound has no literal form.

    Equal to an ORM or SDK ``Range`` with the same bounds (the SDK's own ``__eq__`` answers
    ``False`` the other way round, so keep the ORM value on the left); not hashable, like the SDK's.

    As a Pydantic field, ``Range[int]``, ``Range[datetime]``, ``Range[timedelta]``… validate each bound
    value as ``T``; a bare ``Range`` keeps them as given. Accepted: a ``Range``, an SDK ``Range``, a
    builtin ``range`` with step 1 (``range(9, 17)`` is ``9..17``) and the SurrealQL text ``str()``
    writes (``"9..17"``). JSON mode writes that text.
    """

    __slots__ = ("begin", "end")

    begin: BoundIncluded | BoundExcluded | None
    end: BoundIncluded | BoundExcluded | None

    def __init__(
        self,
        begin: BoundIncluded | BoundExcluded | None = None,
        end: BoundIncluded | BoundExcluded | None = None,
    ) -> None:
        """A range from ``begin`` to ``end``; ``None`` (or a bare SDK ``Bound()``) is an open side.

        ``TypeError`` for any other bound: ``Range(1, 5)`` is an error, write
        ``Range(BoundIncluded(1), BoundExcluded(5))``.
        """
        self.begin = _side(begin, "begin")
        self.end = _side(end, "end")

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (Range, SdkRange)):
            return bool(self.begin == _open_side(other.begin) and self.end == _open_side(other.end))
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"Range(begin={self.begin!r}, end={self.end!r})"

    def __str__(self) -> str:
        """The SurrealQL literal (``"1..5"``); ``repr(self)`` when a bound has no literal form."""
        from .surql_literal import to_surql_literal

        try:
            return to_surql_literal(self)
        except TypeError:
            return repr(self)

    @classmethod
    def _from_input(cls, value: Any) -> Range[Any]:
        """A ``Range`` from what a field accepts, bounds not yet validated; ``ValueError`` otherwise."""
        if isinstance(value, Range):
            return value
        if isinstance(value, SdkRange):
            try:
                return cls(_side(value.begin, "begin"), _side(value.end, "end"))
            except TypeError as exc:
                raise ValueError(str(exc)) from None
        if isinstance(value, range):
            if value.step != 1:
                raise ValueError(f"only a range with step 1 is a SurrealDB range, got step {value.step}")
            return cls(BoundIncluded(value.start), BoundExcluded(value.stop))
        if isinstance(value, str):
            return _parse_range_text(value)
        raise ValueError(
            "a Range takes a Range, a builtin range with step 1 or a SurrealQL range such as '9..17', "
            f"not {type(value).__name__}"
        )

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
        args = get_args(source_type)
        bound_type: Any = args[0] if args else Any
        validate = _RangeValidator(bound_type)
        return core_schema.no_info_plain_validator_function(
            validate,
            serialization=core_schema.plain_serializer_function_ser_schema(str, when_used="json"),
        )

    @classmethod
    def __get_pydantic_json_schema__(cls, schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        return {"type": "string", "examples": ["1..5"]}


def _open_side(side: object) -> object:
    """A bare SDK ``Bound()`` compares as an open side (``None``); any other side unchanged."""
    return None if type(side) is Bound else side


class _RangeValidator:
    """The field validator of one ``Range[T]`` annotation: each bound value validated as ``T``.

    The ``TypeAdapter`` for ``T`` is built on first use and kept. Each bound value first goes through
    ``wire.field_input`` (an SDK ``Duration`` or a parsed ``1h`` becomes a ``timedelta`` at
    ``Range[timedelta]``). ``Any``, a ``TypeVar`` or a bare ``Range`` keep the bound values as given.
    """

    __slots__ = ("_adapter", "_bound_type")

    def __init__(self, bound_type: Any) -> None:
        self._bound_type = bound_type
        self._adapter: TypeAdapter[Any] | None = None

    def __call__(self, value: Any) -> Range[Any]:
        result = Range._from_input(value)
        if self._bound_type is Any or isinstance(self._bound_type, TypeVar):
            return result
        return Range(self._validate_side(result.begin), self._validate_side(result.end))

    def _validate_side(self, side: BoundIncluded | BoundExcluded | None) -> BoundIncluded | BoundExcluded | None:
        if side is None:
            return None
        from .wire import field_input

        if self._adapter is None:
            self._adapter = TypeAdapter(self._bound_type)
        bound_value = self._adapter.validate_python(field_input(self._bound_type, side.value))
        return BoundIncluded(bound_value) if isinstance(side, BoundIncluded) else BoundExcluded(bound_value)


# --- Reading the literal back (spec §5.5, D12) ---------------------------------------------------

_MAX_RANGE_TEXT = 1024

# Each bound literal ``to_surql_literal`` writes, tried in exactly this order at the current
# position: datetime, string, decimal, duration, float, int — so ``1dec`` is never read as the
# duration ``1d`` followed by ``ec``, nor ``1m`` as the integer ``1``. ASCII digits only.
_DECIMAL_LITERAL = re.compile(r"-?[0-9]+(?:\.[0-9]+)?dec")
_FLOAT_LITERAL = re.compile(r"-?[0-9]+\.[0-9]+(?:[eE][+-]?[0-9]+)?|-?[0-9]+[eE][+-]?[0-9]+")
_INT_LITERAL = re.compile(r"-?[0-9]+")
_STRING_ESCAPES = {"\\": "\\", "'": "'"}


def _read_datetime(text: str, start: int) -> tuple[datetime, int]:
    """``d'<ISO-8601>'`` at ``start`` (which holds ``d'``) → an aware datetime (UTC when naive)."""
    close = text.find("'", start + 2)
    if close < 0:
        raise ValueError(f"unterminated datetime literal at position {start}")
    iso = text[start + 2 : close]
    if not iso or any(char.isspace() for char in iso):
        raise ValueError(f"{iso!r} is not a datetime literal")
    try:
        moment = datetime.fromisoformat(iso[:-1] + "+00:00" if iso.endswith("Z") else iso)
    except ValueError:
        raise ValueError(f"{iso!r} is not an ISO-8601 datetime") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment, close + 1


def _read_string(text: str, start: int) -> tuple[str, int]:
    """``'<text>'`` at ``start`` with the encoder's two escapes, ``\\\\`` and ``\\'``."""
    chars: list[str] = []
    position = start + 1
    while position < len(text):
        char = text[position]
        if char == "'":
            return "".join(chars), position + 1
        if char == "\\":
            escaped = text[position + 1 : position + 2]
            if escaped not in _STRING_ESCAPES:
                raise ValueError(f"unknown escape \\{escaped} at position {position}")
            chars.append(_STRING_ESCAPES[escaped])
            position += 2
            continue
        chars.append(char)
        position += 1
    raise ValueError(f"unterminated string literal at position {start}")


def _read_bound(text: str, start: int) -> tuple[Any, int] | None:
    """The bound literal at ``start`` and the position after it, or ``None`` when there is none."""
    if text.startswith("d'", start):
        return _read_datetime(text, start)
    if text.startswith("'", start):
        return _read_string(text, start)
    match = _DECIMAL_LITERAL.match(text, start)
    if match:
        return Decimal(match.group()[:-3]), match.end()
    match = _DURATION_TEXT.match(text, start)
    if match:
        # The strict grammar, never stripped: the match holds no whitespace.
        return Duration.parse(match.group()), match.end()
    match = _FLOAT_LITERAL.match(text, start)
    if match:
        return float(match.group()), match.end()
    match = _INT_LITERAL.match(text, start)
    if match:
        return int(match.group()), match.end()
    return None


def _parse_range_text(text: str) -> Range[Any]:
    """The ``Range`` a SurrealQL range literal means — exactly the grammar the encoder writes.

    ``[<lit>][>]..[=][<lit>]``: ``>`` excludes the begin (after a begin literal only), ``=``
    includes the end (before an end literal only). A ``<lit>`` is ``d'<ISO-8601>'`` (an aware
    ``datetime``), ``'<text>'`` with ``\\\\`` and ``\\'`` escapes (a ``str``), ``<n>dec`` (a ``Decimal``),
    the strict duration syntax (an ORM ``Duration``), a float (``1.5``, ``1e+20``) or an int.
    No whitespace anywhere — unlike ``Duration.parse``, nothing is stripped — and at most 1024
    characters. ``ValueError`` for anything else, trailing text included.
    """
    if len(text) > _MAX_RANGE_TEXT:
        raise ValueError(f"a range literal has at most {_MAX_RANGE_TEXT} characters, got {len(text)}")
    begin_literal = _read_bound(text, 0)
    position = 0
    begin: BoundIncluded | BoundExcluded | None = None
    if begin_literal is not None:
        value, position = begin_literal
        if text.startswith(">", position):
            begin = BoundExcluded(value)
            position += 1
        else:
            begin = BoundIncluded(value)
    if not text.startswith("..", position):
        raise ValueError(f"{text!r} is not a SurrealQL range: expected '..' at position {position}")
    position += 2
    end_included = text.startswith("=", position)
    if end_included:
        position += 1
    end: BoundIncluded | BoundExcluded | None = None
    end_literal = _read_bound(text, position) if position < len(text) else None
    if end_literal is not None:
        value, position = end_literal
        end = BoundIncluded(value) if end_included else BoundExcluded(value)
    elif end_included:
        raise ValueError(f"{text!r} is not a SurrealQL range: '..=' needs an end value")
    if position != len(text):
        raise ValueError(f"{text!r} is not a SurrealQL range: unexpected text at position {position}")
    return Range(begin, end)
