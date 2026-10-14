"""Internal: the boundary between the ORM and the SDK (v0.23.0).

Not part of the public API — nothing here is exported by ``surreal_orm_lite``.

``encode(value)`` turns a value the ORM is about to hand the SDK into the form SurrealDB stores
natively, on 2.x and 3.x alike, and returns **the very object it was given** for everything
v0.22.2 already sent, so existing payloads go out byte for byte as before. It is driven by the
value alone — no field annotation, no I/O — and it is:

* **pure**: it never mutates its argument;
* **copy-on-write**: a ``dict``, ``list`` or ``tuple`` is rebuilt only when one of its items
  changed, at every depth; otherwise the object passed in comes back;
* **idempotent**: ``encode(encode(v)) is encode(v)``.

It converts a ``timedelta`` or a ``Duration`` (the ORM's or the SDK's) to the server's compact
duration (CBOR tag 14, nanosecond-exact); a ``date`` to midnight UTC; a ``set`` or ``frozenset``
to a list, sorted when its items can be; an ORM or SDK ``Range`` to an SDK ``Range`` whose open
sides are the protocol's CBOR null; and a ``Decimal`` in exponent form to its fixed-point text. It
refuses with a plain ``ValueError``, before anything is sent: an ``int`` outside signed 64-bit, a
``Decimal`` whose rounded integer part exceeds ``2**96 - 1``, a negative or too long duration, a
zero duration while the installed SDK cannot read one back, and a range side that is neither a
bound nor an open side. Anything else comes back unchanged: the SDK decides, as in v0.22.2.

The ORM calls it through this module (``_encoding.encode(x)``), never by a name imported from it.

``sdk_decodes_zero_duration()`` is the one local SDK capability the rich field types depend on:
SDK 2.0.0 cannot decode a zero duration (CBOR tag 14 with an empty compact form raises
``IndexError``), so the ORM refuses to send one — in a bound parameter or a live-query literal —
while the probe answers ``False``. A fixed SDK lifts the refusal by itself. Call it through this
module (``encoding.sdk_decodes_zero_duration()``) so a test can replace it.
"""

from __future__ import annotations

import decimal
import functools
import itertools
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any, Final, cast
from uuid import UUID

from . import _sdk
from .types import Duration, Range, _is_open_side, _nanoseconds

__all__ = ["encode", "sdk_decodes_zero_duration"]

# SurrealDB integers are signed 64-bit: a larger one would be stored wrapped, or refused.
INT64_MIN: Final = -(2**63)
INT64_MAX: Final = 2**63 - 1
# The largest SurrealDB decimal: a 96-bit mantissa.
DECIMAL_INTEGER_MAX: Final = 2**96 - 1
# A SurrealDB duration holds at most 2**64 - 1 seconds (plus its nanoseconds).
DURATION_SECONDS_LIMIT: Final = 2**64
# An exponent-form decimal goes out in fixed point, cut toward zero after this many fractional
# digits: the server rounds once, half away from zero, at 28 digits, so only the 29th can matter.
DECIMAL_TEXT_SCALE: Final = 29
DECIMAL_TEXT_STEP: Final = Decimal("1E-29")  # built from text: no context involved

_NS_PER_SECOND = 1_000_000_000

# Exact types the SDK already sends the way SurrealDB expects: returned as is and never descended
# (a ``CBORTag`` or a ``RecordID`` is the caller's explicit wire form). Subclasses take the rows.
_PASSTHROUGH: Final = frozenset(
    {
        type(None),
        str,
        bytes,
        float,
        bool,
        UUID,
        datetime,
        _sdk.RecordID,
        _sdk.CBORTag,
        _sdk.CBORSimpleValue,
        type(_sdk.undefined),
    }
)


def encode(value: Any) -> Any:
    """The form of ``value`` the ORM hands the SDK — the object itself whenever nothing changes.

    ``ValueError`` (plain, never an ORM error class) for a value SurrealDB cannot hold; nothing has
    been sent at that point. The rules are in the module docstring.
    """
    return _encode(value)


def _encode(value: Any) -> Any:
    """``encode``'s rows, first match wins. Recursion stays here, so wrapping ``encode`` sees top-level calls only."""
    if type(value) in _PASSTHROUGH:
        return value
    if isinstance(value, int):  # ``bool`` took the fast path; an ``IntEnum`` member lands here
        if INT64_MIN <= value <= INT64_MAX:
            return value
        raise ValueError(
            f"Cannot send {value}: SurrealDB integers are signed 64-bit (-2**63 .. 2**63 - 1); use Decimal for larger numbers."
        )
    if isinstance(value, Decimal):
        if not value.is_finite():
            return value  # the SDK sends a float NaN or infinity, as in v0.22.2
        return _encode_decimal(value)
    if isinstance(value, (timedelta, Duration, _sdk.SdkDuration)):
        return _encode_duration(value)
    if isinstance(value, date) and not isinstance(value, datetime):  # ``date`` subclasses too
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, (Range, _sdk.SdkRange)):
        return _encode_range(value)
    if isinstance(value, dict):
        return _encode_dict(value)
    if isinstance(value, (list, tuple)):
        return _encode_sequence(value)
    if isinstance(value, (set, frozenset)):
        # Sort the original items (encoded ``CBORTag``s do not order like the values they hold).
        try:
            items = sorted(value)
        except (TypeError, ArithmeticError):  # unorderable items; a ``Decimal`` NaN raises InvalidOperation
            items = list(value)
        return [_encode(item) for item in items]
    return value


def _encode_decimal(value: Decimal) -> Any:
    """A finite ``Decimal``: itself when ``str()`` writes no exponent, else its fixed-point text.

    Never reads the caller's decimal context — its traps, precision and ``capitals`` do not apply:
    every operation that takes a context gets a private one built here (never shared, so no flag
    lands in a context another thread uses), and the notation is decided from ``as_tuple()``,
    never from ``str()``.
    """
    ctx = decimal.Context(prec=60, rounding=ROUND_HALF_UP, traps=[])
    _, digits, raw_exponent = value.as_tuple()
    exponent = cast(int, raw_exponent)  # finite: the caller checked
    # A zero is never refused, whatever its exponent. ``adjusted() >= 29`` is checked first (no big
    # computation); ``copy_abs()``, not ``abs()``, which rounds to the context precision and would
    # refuse a long ``…335.4999…`` that the server stores as ``…335``.
    if any(digits) and (
        value.adjusted() >= 29 or value.copy_abs().to_integral_value(rounding=ROUND_HALF_UP, context=ctx) > DECIMAL_INTEGER_MAX
    ):
        raise ValueError(
            f"Cannot send Decimal({str(value)!r}): its integer part, rounded, exceeds 2**96 - 1, the largest SurrealDB decimal."
        )
    if exponent <= 0 and value.adjusted() >= -6:  # exactly when ``str()`` writes no exponent
        return value
    if exponent < -DECIMAL_TEXT_SCALE:
        # Cut after the 29th fractional digit: the server stores what it stores for the full text.
        value = value.quantize(DECIMAL_TEXT_STEP, rounding=ROUND_DOWN, context=ctx)
    # ``format`` with no precision neither rounds nor writes an exponent.
    return _sdk.CBORTag(_sdk.TAG_DECIMAL_STRING, format(value, "f"))


def _encode_duration(value: timedelta | Duration | _sdk.SdkDuration) -> Any:
    """The server's compact duration ``[seconds, nanoseconds]`` (CBOR tag 14), in exact integers.

    Never the SDK's ``get_seconds_and_nano()`` (float error above ~2**53 ns) nor its
    ``to_compact()`` (drops the nanoseconds).
    """
    ns = _nanoseconds(value)  # ValueError for a duration whose ``elapsed`` is not an int
    if ns < 0:
        raise ValueError(f"Cannot send a negative duration ({value!r}): SurrealDB durations are never negative.")
    seconds, nanoseconds = divmod(ns, _NS_PER_SECOND)
    if seconds >= DURATION_SECONDS_LIMIT:
        raise ValueError(f"Cannot send {value!r}: a SurrealDB duration holds at most 2**64 - 1 seconds.")
    if ns == 0 and not sdk_decodes_zero_duration():
        raise ValueError(
            f"Cannot send a zero duration ({value!r}): the installed surrealdb SDK cannot read a zero "
            "duration back. Store None instead, or filter with __gte=Duration(1)."
        )
    return _sdk.CBORTag(_sdk.TAG_DURATION_COMPACT, [seconds, nanoseconds])


def _encode_range_side(side: Any) -> Any:
    """One range side: a CBOR null kept as is, another open marker as the CBOR null, a bound's value encoded."""
    if isinstance(side, _sdk.CBORSimpleValue) and side.value == 22:
        return side  # already the protocol's open bound: the caller's own object (a subclass too)
    if _is_open_side(side):  # ``None``, a bare SDK ``Bound()``, ``undefined``
        return _sdk.CBOR_NULL
    if isinstance(side, (_sdk.BoundIncluded, _sdk.BoundExcluded)):
        bound_value = _encode(side.value)
        return side if bound_value is side.value else type(side)(bound_value)
    raise ValueError(
        f"Cannot send a Range bound of type {type(side).__name__}: use BoundIncluded(value), "
        "BoundExcluded(value) or None for an open side."
    )


def _encode_range(value: Range[Any] | _sdk.SdkRange) -> Any:
    """An SDK ``Range``; an SDK one whose two sides come back unchanged is returned as is."""
    begin = _encode_range_side(value.begin)
    end = _encode_range_side(value.end)
    if isinstance(value, _sdk.SdkRange) and begin is value.begin and end is value.end:
        return value
    return _sdk.SdkRange(begin, end)


def _encode_dict(value: dict[Any, Any]) -> Any:
    """Values encoded, keys as is; a new plain ``dict`` (same key order) only when a value changed."""
    out: dict[Any, Any] | None = None
    for index, (key, item) in enumerate(value.items()):
        encoded = _encode(item)
        if out is None and encoded is not item:
            out = dict(itertools.islice(value.items(), index))  # the unchanged items before this one
        if out is not None:
            out[key] = encoded
    return value if out is None else out


def _encode_sequence(value: list[Any] | tuple[Any, ...]) -> Any:
    """Items encoded; a new ``list``, or a plain ``tuple``, only when an item changed."""
    out: list[Any] | None = None
    for index, item in enumerate(value):
        encoded = _encode(item)
        if out is None and encoded is not item:
            out = list(value[:index])
        if out is not None:
            out.append(encoded)
    if out is None:
        return value
    return tuple(out) if isinstance(value, tuple) else out


@functools.cache
def sdk_decodes_zero_duration() -> bool:
    """Whether the installed SDK reads a zero duration back (computed once per process, no I/O).

    Round-trips ``CBORTag(TAG_DURATION_COMPACT, [])`` — the server's compact form of ``0ns`` —
    through the SDK's own CBOR codec and answers ``True`` only when that gives an SDK ``Duration``
    equal to zero. ``False`` on any exception, and when the shim found no codec in the installed SDK
    (``_sdk.cbor_encode``/``_sdk.cbor_decode`` is ``None``); both are read at call time.
    """
    encode = _sdk.cbor_encode
    decode = _sdk.cbor_decode
    if encode is None or decode is None:
        return False
    try:
        value = decode(encode(_sdk.CBORTag(_sdk.TAG_DURATION_COMPACT, [])))
        return bool(isinstance(value, _sdk.SdkDuration) and value.elapsed == 0)
    except Exception:  # any failure means "cannot read it back"
        return False
