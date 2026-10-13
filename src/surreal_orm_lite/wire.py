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
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping, MutableSequence, MutableSet, Sequence
from collections.abc import Set as AbstractSet
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import NoneType, UnionType
from typing import Annotated, Any, ForwardRef, TypeVar, Union, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel

from ._sdk import SdkDatetime, SdkDuration
from .exceptions import InvalidDurationError
from .types import Duration, Range

__all__ = ["MAX_DURATION_NS", "duration_ns", "field_input", "rich_kinds"]

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
