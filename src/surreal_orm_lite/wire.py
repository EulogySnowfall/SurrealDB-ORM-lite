"""Wire helpers for the rich field types (v0.23.0) — internal, not part of the public API.

The SDK hands the ORM SurrealDB values in its own forms (``surrealdb.Duration``,
``surrealdb.Datetime``…) that Pydantic cannot validate into the Python types a model declares.
This module converts them, **only where the annotation asks for it**:

- ``duration_ns(value)`` — the exact number of nanoseconds of a ``timedelta`` or an ORM/SDK
  ``Duration`` (integer arithmetic, never a float), shared by every duration conversion.
- ``rich_kinds(annotation)`` — which rich types an annotation contains (``"timedelta"``,
  ``"datetime"``…), so callers only walk the fields that need it.
- ``field_input(annotation, value)`` — the annotation-directed pass that runs before Pydantic
  validates a value read from the SDK: an SDK ``Duration`` becomes a ``timedelta`` at a
  ``timedelta`` position, an SDK ``Datetime`` its ISO string at a ``datetime``/``date`` position,
  and nothing changes anywhere else (an ``Any``, ``object`` or ``Duration`` position keeps the SDK
  value and its nanoseconds).
- ``encode_wire(value, stored=…)`` — the other direction: every value the ORM sends, turned into a
  form the SDK encodes and **both** server lines accept (a compact duration tag, a fixed-point
  decimal text, ``undefined`` for an open range side, UTC datetimes, arrays for sets), or refused
  before anything is sent when no such form exists (an ``int`` beyond 64 bits, a ``time``…).
- ``decimal_text(value)`` — the fixed-point text of a ``Decimal``, rounded once the way the server
  rounds it, so the stored value never moves.
- ``sdk_decodes_zero_duration()`` — whether the installed SDK can read a zero duration back (a local
  probe, no server).
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping, MutableMapping, MutableSequence, MutableSet, Sequence
from collections.abc import Set as AbstractSet
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Context, Decimal, DivisionByZero, InvalidOperation, Overflow
from types import NoneType, UnionType
from typing import Annotated, Any, ForwardRef, TypeVar, Union, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel

from . import _sdk
from ._sdk import (
    TAG_DECIMAL_STRING,
    TAG_DURATION_COMPACT,
    Bound,
    BoundExcluded,
    BoundIncluded,
    CBORTag,
    RecordID,
    SdkDatetime,
    SdkDuration,
    SdkRange,
    undefined,
)
from .exceptions import InvalidDurationError
from .functions import SurrealFunc
from .types import Duration, Range

__all__ = [
    "MAX_DURATION_NS",
    "decimal_text",
    "duration_ns",
    "encode_wire",
    "field_input",
    "rich_kinds",
    "sdk_decodes_zero_duration",
]

#: The largest duration SurrealDB stores: 2**64 - 1 seconds plus 999 999 999 nanoseconds.
MAX_DURATION_NS = Duration.MAX_NANOSECONDS

_NS_PER_SECOND = 10**9
_NS_PER_MICROSECOND = 1_000
_US_PER_DAY = 86_400 * 1_000_000
# ``timedelta`` bounds in whole microseconds (``timedelta.min`` is -999 999 999 days exactly).
_TIMEDELTA_MAX_US = timedelta.max.days * _US_PER_DAY + timedelta.max.seconds * 1_000_000 + timedelta.max.microseconds
_TIMEDELTA_MIN_US = timedelta.min.days * _US_PER_DAY

# Origins (``typing.get_origin``) of the annotations the walkers enter.
_SEQUENCE_ORIGINS: frozenset[Any] = frozenset({list, set, frozenset, Sequence, MutableSequence, AbstractSet, MutableSet})
_SET_ORIGINS: frozenset[Any] = frozenset({set, AbstractSet, MutableSet})
_MAPPING_ORIGINS: frozenset[Any] = frozenset({dict, Mapping, MutableMapping})
# The container values ``field_input`` walks into (a ``str`` is never one).
_SEQUENCE_VALUES = (list, tuple, set, frozenset)


def duration_ns(value: object) -> int:
    """The exact number of nanoseconds of a ``timedelta`` or an ORM or SDK ``Duration``.

    ``timedelta``: ``(days * 86400 + seconds) * 10**9 + microseconds * 1000`` — integer arithmetic,
    never ``total_seconds()`` (a float that loses digits above about 104 days at µs precision);
    a negative ``timedelta`` gives a negative number. ``Duration``: its ``elapsed``. No range
    check: each caller applies its own (a write refuses a negative duration, a literal too).

    ``TypeError`` for anything else, and for an SDK ``Duration`` whose ``elapsed`` is not an
    ``int`` (``surrealdb.Duration(1.5)`` builds without complaint).
    """
    if isinstance(value, timedelta):
        return (value.days * 86_400 + value.seconds) * _NS_PER_SECOND + value.microseconds * _NS_PER_MICROSECOND
    if isinstance(value, (Duration, SdkDuration)):
        elapsed: object = value.elapsed
        if isinstance(elapsed, bool) or not isinstance(elapsed, int):
            raise TypeError(f"a Duration must hold an int number of nanoseconds, not {elapsed!r}")
        return int(elapsed)
    raise TypeError(f"expected a timedelta or a Duration, not {type(value).__name__}")


def _unwrap(annotation: Any) -> Any:
    """``Annotated[X, …]`` → ``X`` (nested layers included); anything else unchanged."""
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def _is_union(origin: Any) -> bool:
    return origin is Union or origin is UnionType


def _is_model(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _is_subclass(annotation: Any, cls: type) -> bool:
    """``issubclass`` that answers ``False`` for what is not a plain class (generics, strings…)."""
    return isinstance(annotation, type) and get_origin(annotation) is None and issubclass(annotation, cls)


def _scalar_kind(annotation: Any) -> str | None:
    """The kind of a plain class annotation, or ``None`` when it is not a rich type."""
    if not isinstance(annotation, type) or get_origin(annotation) is not None:
        return None
    if annotation is Duration:
        return "duration"
    if issubclass(annotation, Range):
        return "range"
    # ``datetime`` before ``date``: a datetime is a date.
    for cls, kind in (
        (timedelta, "timedelta"),
        (datetime, "datetime"),
        (date, "date"),
        (Decimal, "decimal"),
        (UUID, "uuid"),
        (frozenset, "frozenset"),
        (set, "set"),
        (tuple, "tuple"),
    ):
        if issubclass(annotation, cls):
            return kind
    return None


def rich_kinds(annotation: Any, *, into_models: bool = True) -> frozenset[str]:
    """The rich kinds an annotation contains, at any depth.

    Kinds: ``"timedelta"``, ``"date"``, ``"datetime"``, ``"decimal"``, ``"uuid"``, ``"duration"``
    (the ORM's ``Duration``), ``"range"`` (the ORM's ``Range``) and the containers that need
    re-validating after a raw read — ``"set"``, ``"frozenset"``, ``"tuple"``. The walk enters
    ``Annotated``, unions (``Optional`` included), ``list``/``set``/``frozenset``/``tuple``/``Sequence``/
    ``AbstractSet`` items, ``dict``/``Mapping`` values (keys are never converted), the ``T`` of
    ``Range[T]`` and, with ``into_models`` (the default), the fields of a nested
    Pydantic model — a self-referencing model is entered once. ``Any``, a ``TypeVar``, a string or
    ``ForwardRef`` annotation and plain types have no kind: an empty set.
    """
    return frozenset(_walk_kinds(annotation, into_models, set()))


def _walk_kinds(annotation: Any, into_models: bool, visiting: set[type]) -> set[str]:
    annotation = _unwrap(annotation)
    origin = get_origin(annotation)
    args = get_args(annotation)
    if _is_union(origin):
        return {kind for arg in args for kind in _walk_kinds(arg, into_models, visiting)}
    if origin is not None:
        kinds: set[str] = set()
        if _is_subclass(origin, Range):
            # ``Range[T]``: the range itself, then what its bound values hold.
            kinds.add("range")
            members = args
        elif origin in _SEQUENCE_ORIGINS or origin is tuple:
            if origin is tuple:
                kinds.add("tuple")
            elif origin is frozenset:
                kinds.add("frozenset")
            elif origin in _SET_ORIGINS:
                kinds.add("set")
            members = args
        elif origin in _MAPPING_ORIGINS:
            members = args[1:2]
        else:
            # ``Literal``, ``Callable``, ``type[…]`` and other generics hold no field values.
            return kinds
        for member in members:
            if member is not Ellipsis:
                kinds |= _walk_kinds(member, into_models, visiting)
        return kinds
    kind = _scalar_kind(annotation)
    if kind is not None:
        return {kind}
    if _is_model(annotation):
        if not into_models or annotation in visiting:
            return set()
        visiting.add(annotation)
        try:
            return {kind for info in annotation.model_fields.values() for kind in _walk_kinds(info.annotation, True, visiting)}
        finally:
            visiting.discard(annotation)
    return set()


# --- field_input (spec §5.4) ------------------------------------------------------------------


def _to_timedelta(value: Any) -> Any:
    """Rule 4: an ORM/SDK ``Duration`` or a SurrealQL duration string → ``timedelta``; else ``value``.

    Floored to the microsecond (``timedelta`` has no nanoseconds). ``ValueError`` naming
    ``Duration`` when the value is beyond what a ``timedelta`` holds. An SDK ``Duration`` whose
    ``elapsed`` is not an ``int`` is left unchanged, so Pydantic reports it as a field error.
    """
    if isinstance(value, (Duration, SdkDuration)):
        try:
            nanoseconds = duration_ns(value)
        except TypeError:
            return value
    elif isinstance(value, str):
        try:
            nanoseconds = Duration.parse(value).elapsed
        except InvalidDurationError:
            # Not the SurrealQL syntax ("PT1H30M", "1.5h"…): Pydantic decides.
            return value
    else:
        return value
    microseconds = nanoseconds // _NS_PER_MICROSECOND
    if not _TIMEDELTA_MIN_US <= microseconds <= _TIMEDELTA_MAX_US:
        raise ValueError(
            f"a Duration of {nanoseconds} ns is beyond timedelta.max ({timedelta.max}): declare the field as Duration"
        )
    return timedelta(microseconds=microseconds)


def _datetime_text(value: Any) -> Any:
    """Rule 5: an SDK ``Datetime`` → its ISO string (Pydantic parses it, truncated to µs); else ``value``."""
    if isinstance(value, SdkDatetime):
        return value.dt
    return value


def _claims(member: Any, value: Any) -> bool:
    """Whether a union member takes ``value`` as it is, which forbids the ``timedelta`` conversion.

    ``Any``, ``object``, a ``TypeVar`` and an unresolved (string or ``ForwardRef``) member claim
    everything; ``Duration`` claims an ORM or SDK ``Duration``; ``str`` claims a string.
    """
    member = _unwrap(member)
    if member is Any or member is object or isinstance(member, (TypeVar, ForwardRef, str)):
        return True
    if member is Duration or member is SdkDuration:
        return isinstance(value, (Duration, SdkDuration))
    if member is str:
        return isinstance(value, str)
    return False


def _shape_matches(member: Any, value: Any) -> bool:
    """Whether a union member's origin fits a container ``value`` (rule 6)."""
    member = _unwrap(member)
    origin = get_origin(member) or member
    if isinstance(value, _SEQUENCE_VALUES):
        return origin in _SEQUENCE_ORIGINS or origin is tuple
    return origin in _MAPPING_ORIGINS or _is_model(member)


def _union_input(members: tuple[Any, ...], value: Any) -> Any:
    """Rule 6: a union with several members (``None`` already dropped)."""
    if isinstance(value, (*_SEQUENCE_VALUES, Mapping)):
        for member in members:
            if _shape_matches(member, value):
                return field_input(member, value)
        return value
    plain = [_unwrap(member) for member in members]
    result = value
    if any(_is_subclass(member, timedelta) for member in plain) and not any(_claims(member, value) for member in plain):
        result = _to_timedelta(result)
    if any(_is_subclass(member, date) for member in plain):
        result = _datetime_text(result)
    return result


def _rebuild(value: Any, items: list[Any]) -> Any:
    """``items`` in a new container of ``value``'s own builtin type."""
    if isinstance(value, list):
        return items
    if isinstance(value, tuple):
        return tuple(items)
    if isinstance(value, frozenset):
        return frozenset(items)
    return set(items)


def _items_input(value: Any, convert: Callable[[int, Any], Any]) -> Any:
    """Each item through ``convert(position, item)``; copy-on-write (the same object when nothing changes)."""
    items = list(value)
    converted = [convert(position, item) for position, item in enumerate(items)]
    if all(new is old for new, old in zip(converted, items, strict=True)):
        return value
    return _rebuild(value, converted)


def _mapping_input(value: Mapping[Any, Any], annotation_of: Callable[[Any], Any]) -> Any:
    """Each value through ``field_input(annotation_of(key), …)``; keys kept; copy-on-write.

    ``annotation_of`` returns ``None`` for a key to leave as is. A new ``dict`` on the first change.
    """
    result: dict[Any, Any] | None = None
    for key, item in value.items():
        annotation = annotation_of(key)
        if annotation is None:
            continue
        new = field_input(annotation, item)
        if new is not item:
            if result is None:
                result = dict(value)
            result[key] = new
    return value if result is None else result


def _model_annotations(model: type[BaseModel]) -> dict[str, Any]:
    """Input key → field annotation: each field's name, then its string ``alias`` / ``validation_alias``."""
    annotations: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        annotations[name] = info.annotation
    for info in model.model_fields.values():
        for alias in (info.alias, info.validation_alias):
            if isinstance(alias, str):
                annotations[alias] = info.annotation
    return annotations


def field_input(annotation: Any, value: Any) -> Any:
    """Convert the SDK forms Pydantic cannot validate, at the positions ``annotation`` asks for them.

    Walks the annotation and the value in parallel (spec §5.4), in this order:

    1. ``None`` → ``None``.
    2. ``Annotated[X, …]`` → as ``X``.
    3. ``Any``, ``object``, a ``TypeVar``, a string or ``ForwardRef`` annotation → unchanged.
    4. ``timedelta`` → an ORM or SDK ``Duration``, or a string ``Duration.parse`` accepts (``"1h30m"``,
       ``" 7d "``), becomes a ``timedelta`` floored to the microsecond (``ValueError`` naming
       ``Duration`` beyond ``timedelta.max``); anything else is left for Pydantic.
    5. ``datetime`` / ``date`` → an SDK ``Datetime`` becomes its ISO string.
    6. A union, ``None`` dropped: one member left → as that member. Several: a scalar gets rule 4
       when ``timedelta`` is a member and no member claims the value as it is (``Any``/``object``
       claim everything, ``Duration`` an ORM/SDK ``Duration``, ``str`` a string), and rule 5 when
       ``datetime`` or ``date`` is a member; a container goes to the first member of its shape
       (sequence or mapping/model), or stays unchanged.
    7. ``list[X]``/``set[X]``/``frozenset[X]``/``Sequence[X]``/``AbstractSet[X]`` with a list, tuple,
       set or frozenset → each item as ``X``.
    8. ``tuple[X, ...]`` → each item as ``X``; ``tuple[A, B, …]`` → by position when the lengths match.
    9. ``dict[K, V]``/``Mapping[K, V]`` with a mapping → each value as ``V``, keys unchanged.
    10. A Pydantic model with a mapping → each key matched to a field by name or string
        ``alias``/``validation_alias``, its value as that field's annotation; other keys unchanged.
        A model instance is unchanged.
    11. Anything else → unchanged: ``Duration``, ``Range``/``Range[T]`` (its own validator converts
        each bound), ``Decimal``, ``UUID``, ``Literal``, a dataclass…

    Copy-on-write: when nothing changes, ``value`` itself is returned; otherwise a new container of
    the value's own type (``list``/``tuple``/``set``/``frozenset``/``dict``). The input is never mutated.
    """
    if value is None:
        return None
    annotation = _unwrap(annotation)
    if annotation is Any or annotation is object or isinstance(annotation, (TypeVar, ForwardRef, str)):
        return value
    if _is_subclass(annotation, timedelta):
        return _to_timedelta(value)
    if _is_subclass(annotation, date):
        return _datetime_text(value)
    origin = get_origin(annotation)
    args = get_args(annotation)
    if _is_union(origin):
        members = tuple(member for member in args if member is not NoneType)
        if len(members) == 1:
            return field_input(members[0], value)
        return _union_input(members, value)
    if origin is tuple and args and isinstance(value, _SEQUENCE_VALUES):
        if len(args) == 2 and args[1] is Ellipsis:
            item_annotation = args[0]
            return _items_input(value, lambda _position, item: field_input(item_annotation, item))
        if isinstance(value, (list, tuple)) and len(value) == len(args):
            return _items_input(value, lambda position, item: field_input(args[position], item))
        return value
    if origin in _SEQUENCE_ORIGINS and args and isinstance(value, _SEQUENCE_VALUES):
        item_annotation = args[0]
        return _items_input(value, lambda _position, item: field_input(item_annotation, item))
    if origin in _MAPPING_ORIGINS and len(args) == 2 and isinstance(value, Mapping):
        value_annotation = args[1]
        return _mapping_input(value, lambda _key: value_annotation)
    if _is_model(annotation) and isinstance(value, Mapping):
        annotations = _model_annotations(annotation)
        return _mapping_input(value, annotations.get)
    return value


# --- encode_wire (spec §5.1) --------------------------------------------------------------------

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
# A SurrealDB decimal: a 96-bit coefficient and at most 28 fractional digits.
_DECIMAL_MAX_SCALE = 28
_DECIMAL_MAX_COEFFICIENT = 2**96 - 1
# At most 29 integer + 28 fractional digits (+ a carry): never inexact. Independent of the caller's
# context, whose precision or traps (``Inexact``, ``Rounded``) must not change the text sent.
_DECIMAL_CONTEXT = Context(prec=80, rounding=ROUND_HALF_UP, traps=[InvalidOperation, DivisionByZero, Overflow])

_ZERO_DURATION_REFUSED = (
    "a zero duration cannot be written: the installed surrealdb SDK cannot read a zero duration back "
    "(`0ns`): on a WebSocket the connection would be lost. Store `None` instead."
)


@functools.cache
def sdk_decodes_zero_duration() -> bool:
    """Whether the installed SDK reads back the zero duration the server sends — local, no server.

    SurrealDB sends a zero duration as an empty compact array (``CBORTag(14, [])``), which SDK 2.0.0
    cannot decode (``IndexError``; on a WebSocket the connection is lost). The probe round-trips that
    exact form through the SDK's own codec, once per process: ``True`` only when it comes back as an
    SDK ``Duration`` of zero. ``False`` on any exception, and when the SDK's ``encode``/``decode``
    could not be imported (``_sdk.cbor_encode``/``cbor_decode`` is ``None``). While it is ``False``,
    ``encode_wire(..., stored=True)`` refuses a zero duration; an SDK release that decodes it lifts
    the restriction by itself.
    """
    encode: Callable[[Any], bytes] | None = _sdk.cbor_encode
    decode: Callable[[bytes], Any] | None = _sdk.cbor_decode
    if encode is None or decode is None:
        return False
    try:
        decoded = decode(encode(CBORTag(TAG_DURATION_COMPACT, [])))
    except Exception:
        return False
    return isinstance(decoded, SdkDuration) and decoded.elapsed == 0


def _show_int(value: int) -> str:
    """An ``int`` for an error message: its digits, or its size when Python would refuse to print it."""
    if value.bit_length() <= 256:
        return str(value)
    return f"an integer of {value.bit_length()} bits"


def _check_int(value: int) -> None:
    """``ValueError`` for an ``int`` outside signed 64 bits (the server wraps or rejects it)."""
    if not _INT64_MIN <= value <= _INT64_MAX:
        raise ValueError(
            f"SurrealDB integers are signed 64-bit: {_show_int(value)} is outside [-2**63, 2**63 - 1]; "
            "use Decimal for larger numbers"
        )


def _check_key(key: object) -> None:
    """The ``int`` rule on a record ID key, including the ints inside an array or object key."""
    if isinstance(key, bool):
        return
    if isinstance(key, int):
        _check_int(key)
    elif isinstance(key, (list, tuple)):
        for item in key:
            _check_key(item)
    elif isinstance(key, Mapping):
        for item in key.values():
            _check_key(item)


def decimal_text(value: Decimal) -> str:
    """The fixed-point text of a finite ``Decimal`` the server stores exactly as sent.

    A SurrealDB decimal holds a 96-bit coefficient and at most 28 fractional digits, and the server
    rounds a longer text **once**, half away from zero, at the largest scale whose coefficient fits.
    This does the same, so the value stored is the value v0.22.1 stored, while the text stays
    bounded and never uses an exponent (``str(Decimal)`` writes ``1E+3`` or ``1E-7``, which 2.x
    rejects): each candidate scale, from ``min(28, digits after the point)`` down to ``0``, rounds
    the **original** value (rounding a rounded value twice moves it: ``8.<27 zeros>49`` would become
    ``…5`` then ``…1``); the first whose coefficient fits wins. Trailing zeros within 28 digits are
    kept (``"1.50"``); ``Decimal("1E+3")`` → ``"1000"``; ``Decimal("1E-1000000")`` → ``"0." + 28 zeros``.

    ``ValueError`` when no scale fits — the integer part, rounded, exceeds ``2**96 - 1``
    (``79228162514264337593543950335.5``, ``1E+29``): the server would reject it and drop the
    WebSocket — and for a non-finite value (``NaN``/``Infinity`` have no fixed-point text).
    """
    if not value.is_finite():
        raise ValueError(f"{value} is not a finite decimal: it has no fixed-point text")
    out_of_range = ValueError(
        f"{value} does not fit a SurrealDB decimal: its integer part exceeds 2**96 - 1 "
        "(79228162514264337593543950335) once rounded"
    )
    # Cheap first bound on the work (a zero with a large exponent is still a zero).
    if value and value.adjusted() > _DECIMAL_MAX_SCALE:
        raise out_of_range
    exponent = value.as_tuple().exponent
    assert isinstance(exponent, int)  # a finite value's exponent is an int
    for scale in range(min(_DECIMAL_MAX_SCALE, max(0, -exponent)), -1, -1):
        rounded = value.quantize(Decimal(1).scaleb(-scale, _DECIMAL_CONTEXT), context=_DECIMAL_CONTEXT)
        if abs(int(rounded.scaleb(scale, _DECIMAL_CONTEXT))) <= _DECIMAL_MAX_COEFFICIENT:
            return format(rounded, "f")
    raise out_of_range


def _encode_duration(value: timedelta | Duration | SdkDuration, stored: bool) -> CBORTag:
    """A ``timedelta`` or ORM/SDK ``Duration`` → ``CBORTag(TAG_DURATION_COMPACT, [seconds, nanoseconds])``.

    Exact integers (``divmod``), never the SDK's ``get_seconds_and_nano()`` (a float, wrong above
    about 2**53 ns) nor its ``to_compact()`` (drops the nanoseconds).
    """
    nanoseconds = duration_ns(value)
    if nanoseconds < 0:
        raise ValueError(f"SurrealDB durations cannot be negative: got {_show_int(nanoseconds)} ns")
    if nanoseconds > MAX_DURATION_NS:
        raise ValueError(
            f"a duration of {_show_int(nanoseconds)} ns is beyond the largest SurrealDB duration "
            f"({Duration(MAX_DURATION_NS)}, {MAX_DURATION_NS} ns)"
        )
    if nanoseconds == 0 and stored and not sdk_decodes_zero_duration():
        raise ValueError(_ZERO_DURATION_REFUSED)
    seconds, rest = divmod(nanoseconds, _NS_PER_SECOND)
    return CBORTag(TAG_DURATION_COMPACT, [seconds, rest])


def _encode_side(side: object, stored: bool) -> Any:
    """A range side: open (``None``, a bare ``Bound()``, ``undefined``) → ``undefined``; a bound → its value encoded."""
    if side is None or side is undefined or type(side) is Bound:
        return undefined
    if isinstance(side, BoundIncluded):
        return BoundIncluded(encode_wire(side.value, stored=stored))
    if isinstance(side, BoundExcluded):
        return BoundExcluded(encode_wire(side.value, stored=stored))
    raise TypeError(
        f"a range side must be BoundIncluded(value), BoundExcluded(value) or None (open), not {type(side).__name__}: "
        f"{side!r} is not a bound"
    )


def _set_items(value: set[Any] | frozenset[Any]) -> list[Any]:
    """A set's items, sorted when they are mutually orderable (deterministic), else in iteration order."""
    try:
        return sorted(value)
    except TypeError:
        return list(value)


def encode_wire(value: Any, *, stored: bool) -> Any:
    """``value`` in a form the SDK encodes and both SurrealDB lines (2.x and 3.x) accept.

    Recursive, idempotent (``encode_wire(encode_wire(v)) == encode_wire(v)``) and never mutates its
    argument. ``stored`` is ``True`` for the values of a statement the ORM builds to **write**, ``False``
    for filter values and raw paths; the two modes differ only for a zero duration (below).

    - ``None``, ``bool``, ``float``, ``str``, ``bytes``, ``UUID``, ``CBORTag``, ``undefined``,
      ``SurrealFunc`` and any other object: unchanged (the same object).
    - ``int``: unchanged in signed 64 bits, else ``ValueError`` (the server silently wraps ``2**63``
      and rejects ``2**64``, dropping the WebSocket). ``RecordID``: unchanged, its key checked the same way.
    - ``datetime``: in UTC (a naive value is taken as UTC, as the SDK sends it). ``date``: midnight UTC
      (the SDK's date tag drops the WebSocket). ``time``: ``TypeError`` (no time-of-day type).
    - ``timedelta``, ORM or SDK ``Duration``: ``CBORTag(TAG_DURATION_COMPACT, [s, ns])`` from the exact
      nanoseconds (the SDK's own tag 13 is rejected). Negative or beyond ``Duration.MAX_NANOSECONDS``:
      ``ValueError``. Zero with ``stored=True`` while ``sdk_decodes_zero_duration()`` is ``False``:
      ``ValueError`` (the row could not be read back).
    - finite ``Decimal``: ``CBORTag(TAG_DECIMAL_STRING, decimal_text(value))``; ``NaN``/``±Infinity``:
      unchanged (the SDK sends a float, as before).
    - ORM or SDK ``Range``: an SDK ``Range`` with ``undefined`` for an open side and each bound value
      encoded (bound values are not restricted); a side that is not a bound: ``TypeError``.
    - ``list``/``tuple``: a ``list`` of encoded items. ``set``/``frozenset``: a ``list`` (an array, never
      a native set), sorted first when the items are orderable. A mapping: a ``dict``, same keys,
      encoded values.
    """
    if value is None or value is undefined or isinstance(value, (bool, float, str, bytes, UUID, CBORTag, SurrealFunc)):
        return value
    if isinstance(value, int):
        _check_int(value)
        return value
    if isinstance(value, RecordID):
        _check_key(value.id)
        return value
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, time):
        raise TypeError(f"SurrealDB has no time-of-day type: cannot send the time {value!r}")
    if isinstance(value, (timedelta, Duration, SdkDuration)):
        return _encode_duration(value, stored)
    if isinstance(value, Decimal):
        if not value.is_finite():
            return value
        return CBORTag(TAG_DECIMAL_STRING, decimal_text(value))
    if isinstance(value, (Range, SdkRange)):
        return SdkRange(_encode_side(value.begin, stored), _encode_side(value.end, stored))
    if isinstance(value, (list, tuple)):
        return [encode_wire(item, stored=stored) for item in value]
    if isinstance(value, (set, frozenset)):
        return [encode_wire(item, stored=stored) for item in _set_items(value)]
    if isinstance(value, Mapping):
        return {key: encode_wire(item, stored=stored) for key, item in value.items()}
    return value
