"""Render Python values as SurrealQL literals — for the one place parameters cannot reach (v0.20.0).

SurrealDB 2.x does not carry bound parameters into a live query: ``LIVE SELECT … WHERE status =
$s`` is accepted, then evaluates ``$s`` as ``NONE`` at notification time and matches nothing,
without an error. Writing the value into the statement as a literal is the only form both
server lines honour, and it is what the full SurrealDB-ORM's custom SDK does too. Everywhere
else the ORM keeps binding parameters.

The encoder is deliberately **closed**: a type without a verified literal form raises
``TypeError`` instead of being stringified, because a guessed literal that parses but means
something else is a filter that silently watches the wrong records. Every form below was
checked as ``RETURN <literal> == $p`` against SurrealDB 2.7.0, 3.3.0 and 3.1.5, and
``tests/test_surql_literal.py`` re-checks it on every run. v0.23.0 adds durations
(``timedelta``, ``Duration``: ``1h30m1ns``), dates (``d'2026-01-02T00:00:00Z'``), sets (an array,
sorted when the items can be) and ranges (``1..5``, ``1>..=5``, ``d'2026-01-01T00:00:00Z'..``),
whose bound values form a closed set too; ``tests/test_rich_types.py`` re-checks those.
v0.24.0 adds geometries, the ORM's and the SDK's, written as GeoJSON objects for every kind and
every collection member (``{type: 'Point', coordinates: [-73.99, 40.75]}``) with ``float``
coordinates, checked on 2.7.0, 3.1.5 and 3.3.4; ``tests/test_geo_types.py`` re-checks them. An SDK
geometry is first checked by the encoding boundary, so one SurrealDB refuses has no literal, and
neither has a decimal or non-finite coordinate. That form is also a geometry's ``str()``: its text.

A ``Range`` also has a text form, ``_range_text``: what its ``str()`` and its JSON write and a
``Range`` field reads back. It never reaches the server, so it writes every bound value exactly and
drops the refusals that only protect a live filter: a zero duration is ``0ns`` whatever the ORM
reads back, a ``Decimal`` keeps all its digits, an ``int`` may exceed 64 bits and a string may hold
``\\x00``.

Two choices worth knowing:

* Strings use single quotes with backslash and quote escaped — backslash first, so an escape
  already in the value can never combine with one the encoder adds.
* Record ids use the backtick form on both lines (table and string key each in backticks). The
  angle-bracket form is not used: escaping a closing bracket inside it parses on 2.x and is a
  syntax error on 3.x.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from . import encoding as _encoding
from ._sdk import BoundExcluded, BoundIncluded, RecordID, SdkDuration, SdkRange
from .exceptions import SurrealDbError
from .geo import _SDK_GEOMETRIES, Geometry, _geometry_text
from .types import _MAX_RANGE_TEXT, Duration, Range, _is_open_side, _nanoseconds

__all__ = ["inline_variables", "to_surql_literal"]

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1

# SurrealDB decimals (2.x and 3.x alike) hold a 96-bit mantissa and at most 28 fractional
# digits. A 29th digit is rounded away without an error, which would silently move a filter
# boundary, so both limits are refused up front.
_DECIMAL_MANTISSA_MAX = 2**96 - 1
_DECIMAL_SCALE_MAX = 28

# A ``$name`` reference. Greedy, so ``$_f10`` is never read as ``$_f1`` followed by ``0``.
_REFERENCE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")

# Parameters the server itself defines while it evaluates a live query. They are the only
# references allowed to stay unbound: any other one would read as NONE on every notification.
_SERVER_PARAMETERS = frozenset({"auth", "session", "token", "access", "scope", "this", "parent"})

_SUPPORTED = (
    "None, bool, int, float, str, Decimal, datetime, date, timedelta, Duration, UUID, RecordID, Range, "
    "geometries (the ORM's or the SDK's), and lists, sets or dicts of these"
)

# A SurrealDB duration holds at most 2**64 - 1 seconds (plus its nanoseconds).
_DURATION_SECONDS_LIMIT = 2**64

# The values a range bound may hold (v0.23.0), each verified as a bound on both lines. ``bool`` is an
# ``int`` and is refused separately, and so are non-finite floats.
_RANGE_BOUND_TYPES = (int, float, Decimal, str, date, timedelta, Duration, SdkDuration)
_RANGE_BOUNDS = "int, finite float, Decimal, str, datetime, date, timedelta, Duration"


def _reject_nul(text: str) -> str:
    if "\x00" in text:
        raise TypeError(
            "A value containing a NUL character (\\x00) cannot be written into SurrealQL source — "
            "both server lines reject it — so it cannot be used in a live query filter."
        )
    return text


def _quoted(text: str) -> str:
    """``'<text>'`` with backslash, then quote, escaped (no ``\\x00`` check: see ``_string``)."""
    escaped = text.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _string(text: str) -> str:
    return _quoted(_reject_nul(text))


def _identifier(text: str) -> str:
    escaped = _reject_nul(text).replace("\\", "\\\\").replace("`", "\\`")
    return f"`{escaped}`"


def _record_id(value: RecordID) -> str:
    key = value.id
    if isinstance(key, str):
        id_part = _identifier(key)
    elif isinstance(key, UUID):
        # ``CREATE user:uuid()`` keys come back from the SDK as ``uuid.UUID``.
        id_part = to_surql_literal(key)
    elif (isinstance(key, int) and not isinstance(key, bool)) or isinstance(key, list | tuple | Mapping):
        id_part = to_surql_literal(key)
    else:
        raise TypeError(
            f"Cannot inline a record id whose key is a {type(key).__name__}; supported keys: str, int, UUID, list, dict."
        )
    return f"{_identifier(value.table_name)}:{id_part}"


def _decimal_precheck(value: Decimal) -> str | None:
    """Decide the cases v0.20.0's ``_decimal`` decided only after writing the whole text (v0.23.0).

    From ``as_tuple()``, without formatting: a zero with more than 28 fractional digits is
    ``"0dec"``/``"-0dec"`` (what v0.20.0 returned once it had stripped them), and a non-zero value
    with more than 28 significant fractional digits or more than 29 integer digits gets the same
    ``TypeError`` v0.20.0 raised. ``None`` otherwise: the v0.20.0 code then runs on a text of at most
    57 digits. So ``Decimal("1E-1000000")`` no longer builds a million-character text, and a value
    beyond Python's 4 300-digit int-string limit gets the contract's ``TypeError`` instead of
    ``int()``'s ``ValueError``.
    """
    sign, digits, raw_exponent = value.as_tuple()
    exponent = int(raw_exponent)  # a finite decimal: always an int
    if not any(digits):
        if exponent < -_DECIMAL_SCALE_MAX:
            return "-0dec" if sign else "0dec"
        return None
    trailing_zeros = len(digits) - len("".join(map(str, digits)).rstrip("0"))
    if exponent < 0 and -exponent - trailing_zeros > _DECIMAL_SCALE_MAX:
        raise TypeError(
            f"Cannot inline Decimal({str(value)!r}): SurrealDB decimals keep at most "
            f"{_DECIMAL_SCALE_MAX} fractional digits and would round the rest away silently."
        )
    if len(digits) + exponent > 29:
        raise TypeError(f"Cannot inline Decimal({str(value)!r}): SurrealDB decimals hold a 96-bit mantissa.")
    return None


def _decimal(value: Decimal) -> str:
    early = _decimal_precheck(value)
    if early is not None:
        return early
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


def _decimal_text(value: Decimal) -> str:
    """A range bound's text form: any finite ``Decimal`` in full, ``<fixed-point>dec``.

    No 28-fractional-digit or 96-bit limit: those keep a live-filter boundary from being rounded,
    and this text never reaches the server. ``TypeError`` for a non-finite value, and — decided from
    ``as_tuple()``, before any text is built — for one whose text would pass the 1024 characters the
    range parser reads (``Decimal("1E-1000000")`` would be a million). A zero with a positive exponent
    is ``0``, so its exponent does not count.
    """
    if not value.is_finite():
        raise TypeError(f"Decimal({str(value)!r}) has no range text: only finite decimals have one.")
    _, digits, raw_exponent = value.as_tuple()
    exponent = int(raw_exponent)  # a finite decimal: always an int
    if (any(digits) or exponent < 0) and max(len(digits), abs(exponent)) > _MAX_RANGE_TEXT:
        raise TypeError(f"Decimal({str(value)!r}) has no range text: it would exceed {_MAX_RANGE_TEXT} characters.")
    return f"{format(value, 'f')}dec"


def _duration(value: timedelta | Duration | SdkDuration, *, text: bool = False) -> str:
    """Every unit, largest first, from the exact nanoseconds: ``1h30m1ns`` (ASCII ``us``).

    With ``text``, a zero duration is ``0ns`` whatever the ORM reads back: the range text form never
    reaches the server.
    """
    try:
        nanoseconds = _nanoseconds(value)
    except ValueError:
        raise TypeError(f"Cannot inline {value!r}: a duration must hold an int number of nanoseconds.") from None
    if nanoseconds < 0:
        raise TypeError(f"Cannot inline a negative duration ({value!r}): SurrealDB durations are never negative.")
    if nanoseconds // 10**9 >= _DURATION_SECONDS_LIMIT:
        raise TypeError(f"Cannot inline {value!r}: a SurrealDB duration holds at most 2**64 - 1 seconds.")
    # SDK 3.0 (#205): goes with ``encoding.reads_zero_duration()`` once the SDK reads a zero duration itself.
    if nanoseconds == 0 and not text and not _encoding.reads_zero_duration():
        raise TypeError(
            f"Cannot inline a zero duration ({value!r}): the installed surrealdb SDK cannot read a zero "
            "duration back. Filter with __gte=Duration(1) instead."
        )
    return Duration(nanoseconds).to_string()


def _date(value: date) -> str:
    # ``date.isoformat`` itself, so a subclass cannot change the text; midnight UTC, as the ORM sends it.
    return f"d'{date.isoformat(value)}T00:00:00Z'"


def _range_side(side: object, name: str) -> BoundIncluded | BoundExcluded | None:
    if isinstance(side, (BoundIncluded, BoundExcluded)):
        return side
    if _is_open_side(side):
        return None
    raise TypeError(
        f"Cannot inline a range whose {name} is of type {type(side).__name__}: "
        "each side is BoundIncluded, BoundExcluded or open (None)."
    )


def _range_bound(value: Any, *, text: bool = False) -> str:
    """A bound value's literal; with ``text``, its text form (see ``_range_text``)."""
    if isinstance(value, bool) or not isinstance(value, _RANGE_BOUND_TYPES):
        raise TypeError(f"Cannot inline a range bound of type {type(value).__name__}; supported bounds: {_RANGE_BOUNDS}.")
    if isinstance(value, float) and not math.isfinite(value):
        raise TypeError(f"Cannot inline the range bound {value!r} (type float): a float bound must be finite.")
    if text:
        if isinstance(value, int):
            # Past 4096 bits (1233 digits) the text could not be read back, and ``int.__repr__``
            # itself refuses past 4300 digits: refused before any text is built.
            if value.bit_length() > 4 * _MAX_RANGE_TEXT:
                raise TypeError(f"An int of {value.bit_length()} bits has no range text: over {_MAX_RANGE_TEXT} characters.")
            return int.__repr__(value)
        if isinstance(value, str):
            return _quoted(value)
        if isinstance(value, Decimal):
            return _decimal_text(value)
        if isinstance(value, timedelta | Duration | SdkDuration):
            return _duration(value, text=True)
    return to_surql_literal(value)


def _range(value: Range[Any] | SdkRange, *, text: bool = False) -> str:
    """``<begin>..<end>``: an excluded begin adds ``>``, an included end ``=``; an open side is empty."""
    begin = _range_side(value.begin, "begin")
    end = _range_side(value.end, "end")
    rendered = ""
    if begin is not None:
        rendered = _range_bound(begin.value, text=text) + (">" if isinstance(begin, BoundExcluded) else "")
    rendered += ".."
    if end is not None:
        rendered += ("=" if isinstance(end, BoundIncluded) else "") + _range_bound(end.value, text=text)
    return rendered


def _range_text(value: Range[Any] | SdkRange) -> str:
    """The text form of a range: what ``str(Range)`` and a ``Range`` field's JSON write (v0.23.0).

    Exactly the grammar ``types._parse_range_text`` reads, so the text reads back to an equal
    ``Range``. Unlike the live-filter literal it never reaches the server, so it keeps every bound
    value whole: a zero duration is ``0ns`` whatever the ORM reads back, a ``Decimal`` is
    written in full (no 28-fractional-digit or 96-bit limit), an ``int`` may exceed 64 bits and a
    string may hold ``\\x00``. Otherwise the same text as ``to_surql_literal``.

    :raises TypeError: for a side that is not a bound, a bound value outside the closed set (or a
        ``bool``, a non-finite ``float``, a negative or too long duration) — none has a text — and
        for a text past the 1024 characters the parser reads.
    """
    rendered = _range(value, text=True)
    if len(rendered) > _MAX_RANGE_TEXT:
        raise TypeError(f"This range has no text form: {len(rendered)} characters, the parser reads {_MAX_RANGE_TEXT}.")
    return rendered


# SDK 3.0 (#205): keep ``geo._geometry_text`` for live-query literals, never the SDK's ``str()``
# (surrealdb/surrealdb.py#403: its ``GeometryCollection`` writes point members as ``(x, y)``).
def _geometry(value: Any) -> str:
    """An ORM or SDK geometry's GeoJSON object literal, from ``geo._geometry_text`` (v0.24.0).

    An SDK geometry goes through the encoding boundary's geometry row first: one SurrealDB refuses
    has no literal either. An ORM geometry was checked when it was built.
    """
    name = type(value).__name__
    if not isinstance(value, Geometry):
        try:
            value = _encoding._encode_sdk_geometry(value)
        except ValueError as exc:
            raise TypeError(f"Cannot inline this {name}: SurrealDB refuses it. {exc}") from exc
    try:
        return _geometry_text(value)
    except TypeError as exc:
        raise TypeError(f"Cannot inline this {name}: {exc}.") from exc


def to_surql_literal(value: Any) -> str:
    """Return the SurrealQL literal that means exactly *value*.

    :raises TypeError: for a type with no verified literal form, a string containing ``\\x00``,
        an ``int`` outside signed 64-bit, a ``Decimal`` that is non-finite or beyond SurrealDB's
        28 fractional digits / 96-bit mantissa, a non-``str`` dict key, a negative duration, one
        of 2**64 seconds or more, a zero duration while nothing reads one back
        (``encoding.reads_zero_duration()``),
        or a range whose side is not a bound or whose bound value is outside the closed set
        (``int`` but not ``bool``, finite ``float``, ``Decimal``, ``str``, ``datetime``, ``date``,
        ``timedelta``, ``Duration``), an SDK geometry SurrealDB refuses, and a geometry with a
        coordinate that is not a finite ``int`` or ``float``.
    """
    if value is None:
        return "NONE"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if not _INT64_MIN <= value <= _INT64_MAX:
            raise TypeError(f"Cannot inline {value}: SurrealDB integers are signed 64-bit.")
        return int.__repr__(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "math::inf" if value > 0 else "-math::inf"
        return float.__repr__(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise TypeError(f"Cannot inline Decimal({str(value)!r}): only finite decimals have a literal form.")
        return _decimal(value)
    if isinstance(value, datetime):
        # Always UTC: an offset with seconds (historic local mean time, e.g. +00:09:21) is a parse
        # error on 2.x and compared wrongly on 3.x. A naive datetime is what the SDK itself sends
        # as UTC, so it is taken as UTC too.
        utc = value.astimezone(UTC) if value.tzinfo is not None else value
        return f"d'{utc.replace(tzinfo=None).isoformat()}Z'"
    if isinstance(value, date):
        # A ``datetime`` (a ``date`` subclass) took the row above; any other ``date`` subclass is a date.
        return _date(value)
    if isinstance(value, UUID):
        return f"u'{value}'"
    if isinstance(value, RecordID):
        return _record_id(value)
    if isinstance(value, timedelta | Duration | SdkDuration):
        return _duration(value)
    if isinstance(value, Range | SdkRange):
        return _range(value)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(to_surql_literal(item) for item in value) + "]"
    if isinstance(value, set | frozenset):
        # Sorted when the items allow it (a stable statement text); iteration order otherwise —
        # mixed types raise ``TypeError``, a ``Decimal`` NaN ``decimal.InvalidOperation``.
        try:
            items = sorted(value)
        except (TypeError, ArithmeticError):
            items = list(value)
        return "[" + ", ".join(to_surql_literal(item) for item in items) + "]"
    if isinstance(value, (Geometry, *_SDK_GEOMETRIES)):
        return _geometry(value)
    if isinstance(value, Mapping):
        parts = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"Cannot inline a dict with a {type(key).__name__} key; object keys must be str.")
            parts.append(f"{_string(key)}: {to_surql_literal(item)}")
        return "{" + ", ".join(parts) + "}"
    raise TypeError(
        f"Cannot inline a value of type {type(value).__name__} into a live query filter. SurrealDB 2.x "
        f"ignores bound parameters there, so the ORM writes each value as a SurrealQL literal, "
        f"and {type(value).__name__} has no verified literal form. Supported: {_SUPPORTED}."
    )


def inline_variables(sql: str, variables: Mapping[str, Any]) -> str:
    """Replace every bound ``$name`` in *sql* with the literal of its value, in one pass.

    The replacement is a callable, so the rendered text is inserted verbatim (``re.sub`` would
    otherwise read backslashes in a replacement *string* as escapes) and is never scanned again
    (a value that spells ``$other`` stays a string). Only the referenced values are rendered, so
    an unused variable of an unsupported type is harmless.

    A reference with no entry in *variables* is refused, except the parameters the server
    defines itself (``$auth``, ``$session``, ``$token``, ``$access``, ``$scope``, ``$this``,
    ``$parent``): an unbound ``$min_age`` reads as ``NONE`` on every notification, so
    ``age > $min_age`` would silently match every record and ``age = $min_age`` none.

    :raises TypeError: naming the variable, if a referenced value cannot be inlined.
    :raises SurrealDbError: naming the variable, if a reference is neither bound nor a server
        parameter.
    """

    def render(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            if name in _SERVER_PARAMETERS:
                return match.group(0)
            raise SurrealDbError(
                f"${name} is not bound: pass it with .variables({name}=...). In a live query an "
                f"unbound reference reads as NONE on every notification, so the filter would "
                f"silently match everything or nothing."
            )
        try:
            return to_surql_literal(variables[name])
        except TypeError as exc:
            raise TypeError(f"${name}: {exc}") from exc

    return _REFERENCE.sub(render, sql)
