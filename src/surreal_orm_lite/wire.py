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
  value and its nanoseconds); in a union, a value the union validates as it is stays as it is, as
  v0.22.2 read it.
- ``lookup_type(annotation, lookup, value)`` — what a filter value on a rich field is validated as
  (``ttl__gt="1h"`` → ``timedelta``, ``at__in=Range(…)`` → ``Range[datetime]``…) before it is bound;
  ``declared_kind(annotation)`` — a field's rich kind without ``Optional``/``Annotated``.
- ``encode_wire(value, stored=…)`` — the other direction: every value the ORM sends, turned into a
  form the SDK encodes and **both** server lines accept (a compact duration tag, a fixed-point
  decimal text, ``undefined`` for an open range side, UTC datetimes, arrays for sets, objects for
  Pydantic sub-models, the record id for a table-model instance), or refused before anything is
  sent when no such form exists (an ``int`` beyond 64 bits, a ``time``, an unsaved table-model
  instance…).
- ``decimal_text(value)`` — the fixed-point text of a ``Decimal``, rounded once the way the server
  rounds it, so the stored value never moves.
- ``sdk_decodes_zero_duration()`` — whether the installed SDK can read a zero duration back (a local
  probe, no server).
- ``restore_native(dumped, raw, read_keys=…)`` — puts back, into a ``model_dump()``, the SDK values
  (``Duration``, ``Range``, geometries) an untyped position held and the dump flattened into plain
  dicts, so a write sends them natively — a typed position keeps its dump, as in v0.22.2; with
  ``read_keys``, also keys each nested sub-model's fields so the
  stored object reads back (:func:`read_keys`: the names when the sub-model validates by name, else
  the aliases).
- ``model_object(model)`` — the object a write sends for a sub-model instance (the same walk, its
  own level re-keyed too).
- ``field_keys(model)`` / ``sub_model(annotation)`` — the keys a mapping may use for a model's fields
  (name and string aliases), and the model class an annotation declares (``Profile | None``);
  ``mapping_values(annotation)`` / ``holds_objects(annotation)`` — the ``V`` of a ``dict[K, V]``, and
  whether a value may be stored as an object (what ``merge()`` merges key by key).
"""

from __future__ import annotations

import dataclasses
import functools
from collections.abc import Callable, Mapping, MutableMapping, MutableSequence, MutableSet, Sequence
from collections.abc import Set as AbstractSet
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Context, Decimal, DivisionByZero, InvalidOperation, Overflow
from types import NoneType, UnionType
from typing import Annotated, Any, ForwardRef, TypeVar, Union, get_args, get_origin
from uuid import UUID

from pydantic import (
    AliasChoices,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    FutureDate,
    FutureDatetime,
    NaiveDatetime,
    PastDate,
    PastDatetime,
    TypeAdapter,
)
from pydantic.fields import FieldInfo
from typing_extensions import is_typeddict

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
    SdkGeometry,
    SdkGeometryCollection,
    SdkRange,
    undefined,
)
from .exceptions import InvalidDurationError
from .functions import SurrealFunc
from .types import Duration, Range

__all__ = [
    "MAX_DURATION_NS",
    "child_annotation",
    "decimal_text",
    "declared_kind",
    "duration_ns",
    "encode_wire",
    "field_input",
    "field_keys",
    "holds_objects",
    "lookup_type",
    "mapping_values",
    "model_object",
    "read_keys",
    "reads_aliases_first",
    "restore_native",
    "rich_kinds",
    "sdk_decodes_zero_duration",
    "sub_model",
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

# Pydantic's datetime and date classes → the class of their values. At run time each is a class
# of its own (MRO ``(cls, object)``), never a subclass of ``datetime`` or ``date``, so ``issubclass``
# alone misses them; every annotation check goes through :func:`_plain_class` instead.
_PLAIN_CLASSES: dict[Any, type] = {
    AwareDatetime: datetime,
    NaiveDatetime: datetime,
    PastDatetime: datetime,
    FutureDatetime: datetime,
    PastDate: date,
    FutureDate: date,
}


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


def _plain_class(annotation: Any) -> Any:
    """``datetime`` for Pydantic's ``AwareDatetime``/``NaiveDatetime``/``PastDatetime``/``FutureDatetime``,
    ``date`` for its ``PastDate``/``FutureDate``; any other annotation unchanged."""
    return _PLAIN_CLASSES.get(annotation, annotation) if isinstance(annotation, type) else annotation


def _is_subclass(annotation: Any, cls: type) -> bool:
    """``issubclass`` that answers ``False`` for what is not a plain class (generics, strings…).

    Pydantic's datetime and date classes count as the class of their values (:func:`_plain_class`):
    ``_is_subclass(AwareDatetime, date)`` is ``True``.
    """
    annotation = _plain_class(annotation)
    return isinstance(annotation, type) and get_origin(annotation) is None and issubclass(annotation, cls)


def _scalar_kind(annotation: Any) -> str | None:
    """The kind of a plain class annotation, or ``None`` when it is not a rich type.

    Pydantic's ``AwareDatetime``/``NaiveDatetime``/``PastDatetime``/``FutureDatetime`` are
    ``"datetime"``, its ``PastDate``/``FutureDate`` ``"date"`` (:func:`_plain_class`).
    """
    annotation = _plain_class(annotation)
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
    re-validating after a raw read — ``"set"``, ``"frozenset"``, ``"tuple"``. Pydantic's
    ``AwareDatetime``, ``NaiveDatetime``, ``PastDatetime`` and ``FutureDatetime`` are ``"datetime"``,
    its ``PastDate`` and ``FutureDate`` ``"date"``. The walk enters
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

    The cheap first check of rule 6, before :func:`_union_accepts`: ``Any``, ``object``, a
    ``TypeVar`` and an unresolved (string or ``ForwardRef``) member claim everything; ``Duration``
    claims an ORM or SDK ``Duration``; a bare ``str`` claims a string (an ``Annotated`` one, which may
    refuse it, is left to the union's validation).
    """
    core = _unwrap(member)
    if core is Any or core is object or isinstance(core, (TypeVar, ForwardRef, str)):
        return True
    if core is Duration or core is SdkDuration:
        return isinstance(value, (Duration, SdkDuration))
    if member is str:
        return isinstance(value, str)
    return False


# Rule 6's union validators, built with ``arbitrary_types_allowed``: a member that needs it comes from
# a model that allows it (no other model builds), where Pydantic checks such a value by ``isinstance``.
_UNION_CONFIG = ConfigDict(arbitrary_types_allowed=True)


def _build_union_adapter(members: tuple[Any, ...]) -> TypeAdapter[Any] | None:
    """The ``TypeAdapter`` of the union of ``members``, or ``None`` when Pydantic cannot build one
    (an annotation it has no schema for, a name it cannot resolve)."""
    union: Any = Union[members]  # noqa: UP007 - a tuple of members; `|` refuses a string annotation
    try:
        adapter: TypeAdapter[Any] = TypeAdapter(union, config=_UNION_CONFIG)
    except Exception:
        return None
    return adapter if adapter.pydantic_complete else None


@functools.cache
def _cached_union_adapter(members: tuple[Any, ...]) -> TypeAdapter[Any] | None:
    return _build_union_adapter(members)


def _union_accepts(members: tuple[Any, ...], value: Any) -> bool:
    """Whether the union of ``members`` validates ``value`` as it is — what v0.22.2 read it as (rule 6).

    ``True`` too when Pydantic cannot build the union's validator: what it accepts is unknown, so the
    value stays as it is, as in v0.22.2. Cached per union (built on each call for one whose members
    cannot be hashed).
    """
    try:
        adapter = _cached_union_adapter(members)
    except TypeError:  # an unhashable member (``Annotated`` metadata)
        adapter = _build_union_adapter(members)
    if adapter is None:
        return True
    try:
        adapter.validate_python(value)
    except Exception:  # a ValidationError, or a member's own validator raising something else
        return False
    return True


def _shape_matches(member: Any, value: Any) -> bool:
    """Whether a union member's origin fits a container ``value`` (rule 6)."""
    member = _unwrap(member)
    origin = get_origin(member) or member
    if isinstance(value, _SEQUENCE_VALUES):
        return origin in _SEQUENCE_ORIGINS or origin is tuple
    return origin in _MAPPING_ORIGINS or _is_model(member)


def _union_converted(members: tuple[Any, ...], value: Any) -> Any:
    """Rule 6's conversion of ``value``, before the union is asked whether it accepts ``value`` itself."""
    if isinstance(value, (*_SEQUENCE_VALUES, Mapping)):
        for member in members:
            if _shape_matches(member, value):
                return field_input(member, value)
        return value
    plain = [_unwrap(member) for member in members]
    result = value
    if any(_is_subclass(member, timedelta) for member in plain) and not any(_claims(member, value) for member in members):
        result = _to_timedelta(result)
    if any(_is_subclass(member, date) for member in plain):
        result = _datetime_text(result)
    return result


def _union_input(members: tuple[Any, ...], value: Any) -> Any:
    """Rule 6: a union with several members (``None`` already dropped).

    The conversion (:func:`_union_converted`) is used only when the union refuses ``value`` as it is
    (:func:`_union_accepts`): a value it accepts — ``"24h"`` for ``Literal["1h", "24h"] | timedelta``,
    ``"1d"`` for a ``StrEnum | timedelta``, ``["1h"]`` for ``list[timedelta] | list[str]`` — keeps
    the member Pydantic's union validation picks, as in v0.22.2. A conversion that fails (a duration
    beyond ``timedelta.max``) raises only when the union refuses the value too.
    """
    try:
        converted = _union_converted(members, value)
    except ValueError:
        if _union_accepts(members, value):
            return value
        raise
    if converted is value or _union_accepts(members, value):
        return value
    return converted


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


@functools.cache
def field_keys(model: type[BaseModel]) -> Mapping[str, str]:
    """Input key → field name for a Pydantic model: each field's name, then its string aliases.

    The keys a mapping may use for a field — its name, its string ``alias``/``validation_alias`` and
    the strings of an ``AliasChoices`` — as rule 10 of :func:`field_input` resolves them, and as a
    keyword write resolves the keys of a dict addressed to a sub-model. An alias spelled like another
    field's name wins over that name (the order Pydantic reads them in). Cached per class; never
    mutate the result.
    """
    keys: dict[str, str] = {name: name for name in model.model_fields}
    for name, info in model.model_fields.items():
        aliases: list[Any] = [info.alias, info.validation_alias]
        if isinstance(info.validation_alias, AliasChoices):
            aliases.extend(info.validation_alias.choices)
        for alias in aliases:
            if isinstance(alias, str):
                keys[alias] = name
    return keys


def _model_annotations(model: type[BaseModel]) -> dict[str, Any]:
    """Input key → field annotation, by :func:`field_keys`."""
    fields = model.model_fields
    return {key: fields[name].annotation for key, name in field_keys(model).items()}


def sub_model(annotation: Any) -> type[BaseModel] | None:
    """The Pydantic model class an annotation declares, ``Annotated`` and ``Optional`` stripped, else ``None``.

    ``Profile | None`` → ``Profile``; ``Annotated[Profile, …]`` → ``Profile``; a union of several
    types, a container (``list[Profile]``) or anything else → ``None``.
    """
    core = _strip_optional(annotation)
    return core if _is_model(core) else None


def mapping_values(annotation: Any) -> Any:
    """``V`` of an annotation that declares ``dict[K, V]`` or ``Mapping[K, V]``, ``Annotated`` and
    ``Optional`` stripped, else ``None`` (a bare ``dict``, a union of several types…)."""
    core = _strip_optional(annotation)
    args = get_args(core)
    if get_origin(core) in _MAPPING_ORIGINS and len(args) == 2:
        return args[1]
    return None


def holds_objects(annotation: Any) -> bool:
    """Whether a value declared ``annotation`` may be stored as an object, which SurrealDB's MERGE
    merges into the stored one (v0.23.0).

    ``Any``, ``object``, a ``TypeVar`` or an unresolved annotation, a Pydantic model, a mapping
    (``dict``, ``dict[K, V]``, ``Mapping``), a dataclass or a ``TypedDict`` — or a union with one
    such member (``Card | Cash``, ``Profile | int``). ``Annotated`` and ``Optional`` stripped.
    """
    core = _strip_optional(annotation)
    if core is Any or core is object or isinstance(core, (TypeVar, ForwardRef, str)):
        return True
    origin = get_origin(core)
    if _is_union(origin):
        return any(holds_objects(member) for member in get_args(core))
    if origin in _MAPPING_ORIGINS:
        return True
    if not isinstance(core, type):
        return False
    return core in _MAPPING_ORIGINS or _is_model(core) or dataclasses.is_dataclass(core) or is_typeddict(core)


def field_input(annotation: Any, value: Any) -> Any:
    """Convert the SDK forms Pydantic cannot validate, at the positions ``annotation`` asks for them.

    Walks the annotation and the value in parallel (spec §5.4), in this order:

    1. ``None`` → ``None``.
    2. ``Annotated[X, …]`` → as ``X``.
    3. ``Any``, ``object``, a ``TypeVar``, a string or ``ForwardRef`` annotation → unchanged.
    4. ``timedelta`` → an ORM or SDK ``Duration``, or a string ``Duration.parse`` accepts (``"1h30m"``,
       ``" 7d "``), becomes a ``timedelta`` floored to the microsecond (``ValueError`` naming
       ``Duration`` beyond ``timedelta.max``); anything else is left for Pydantic.
    5. ``datetime`` / ``date`` (Pydantic's ``AwareDatetime``, ``PastDate``… included) → an SDK
       ``Datetime`` becomes its ISO string.
    6. A union, ``None`` dropped: one member left → as that member. Several: a scalar gets rule 4
       when ``timedelta`` is a member and no member claims the value as it is (``Any``/``object``
       claim everything, ``Duration`` an ORM/SDK ``Duration``, ``str`` a string), and rule 5 when
       ``datetime`` or ``date`` is a member; a container goes to the first member of its shape
       (sequence or mapping/model), or stays unchanged. Whatever changed, the value stays as it is
       when the union validates it as it is (Pydantic's union validation, built with
       ``arbitrary_types_allowed``), which is what v0.22.2 read: ``"24h"`` for
       ``Literal["1h", "24h"] | timedelta``, ``"1d"`` for a ``StrEnum | timedelta``, ``["1h"]`` for
       ``list[timedelta] | list[str]`` keep the member the union picks; ``"2h"`` or an SDK ``Duration``
       there is a ``timedelta``. So does a value of a union Pydantic cannot build a validator for.
    7. ``list[X]``/``set[X]``/``frozenset[X]``/``Sequence[X]``/``AbstractSet[X]`` with a list, tuple,
       set or frozenset → each item as ``X``.
    8. ``tuple[X, ...]`` → each item as ``X``; ``tuple[A, B, …]`` → by position when the lengths match.
    9. ``dict[K, V]``/``Mapping[K, V]`` with a mapping → each value as ``V``, keys unchanged.
    10. A Pydantic model with a mapping → each key matched to a field by name or string
        ``alias``/``validation_alias`` (an ``AliasChoices``' strings included, :func:`field_keys`), its
        value as that field's annotation; other keys unchanged. A model instance is unchanged.
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


# --- lookup_type (spec §4.5) --------------------------------------------------------------------

# The rich scalars a filter value is validated as ("S" in spec §4.5).
_LOOKUP_SCALAR_KINDS = frozenset({"datetime", "date", "timedelta", "decimal", "uuid", "duration"})
_COMPARISON_LOOKUPS = frozenset({"exact", "gt", "gte", "lt", "lte"})
_MEMBERSHIP_LOOKUPS = frozenset({"in", "not_in"})
_CONTAINS_LOOKUPS = frozenset({"contains", "not_contains"})
_CONTAINS_ITEMS_LOOKUPS = frozenset({"containsall", "containsany"})


def _strip_optional(annotation: Any) -> Any:
    """``Annotated[X, …]`` → ``X``, and a union of ``X`` and ``None`` → ``X``, at every layer."""
    annotation = _unwrap(annotation)
    if _is_union(get_origin(annotation)):
        members = [member for member in get_args(annotation) if member is not NoneType]
        if len(members) == 1:
            return _strip_optional(members[0])
    return annotation


def declared_kind(annotation: Any) -> str | None:
    """The rich kind of a field's annotation once ``Annotated`` and ``Optional`` are stripped.

    ``datetime | None`` → ``"datetime"``, ``Annotated[date, …]`` → ``"date"``, ``timedelta`` →
    ``"timedelta"``, ``AwareDatetime | None`` → ``"datetime"``, ``PastDate`` → ``"date"``…; ``None``
    for a container, a union of several types, ``Any`` or a plain type.
    ``atomic_increment`` picks its duration statement from it (spec §4.6).
    """
    return _scalar_kind(_strip_optional(annotation))


def _is_lookup_scalar(annotation: Any) -> bool:
    return _scalar_kind(annotation) in _LOOKUP_SCALAR_KINDS


def _collection_item(annotation: Any) -> Any:
    """``X`` of ``list[X]``/``set[X]``/``frozenset[X]``/``Sequence[X]``/``AbstractSet[X]``/``tuple[X, ...]``, else ``None``."""
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is tuple:
        return _strip_optional(args[0]) if len(args) == 2 and args[1] is Ellipsis else None
    if origin in _SEQUENCE_ORIGINS and args:
        return _strip_optional(args[0])
    return None


def child_annotation(annotation: Any, key: str) -> Any:
    """The annotation of the value at ``key`` inside a value declared ``annotation``, else ``None`` (v0.23.0).

    ``Annotated`` and ``Optional`` stripped first. A ``list``/``set``/``Sequence``/``tuple[X, ...]``
    item (``key`` an index, or ``-``: a JSON Patch append and the item of an ``atomic_*`` helper) →
    ``X``; a ``dict[K, V]``/``Mapping[K, V]`` value → ``V``; a sub-model's field, ``key`` resolved by
    name or alias (:func:`field_keys`) → its annotation. ``None`` where nothing is declared for
    ``key``: ``Any``, a union of several types, a fixed-length tuple, an unknown key. The path of a
    ``patch()`` pointer or a dotted ``atomic_*`` field is resolved one key at a time.
    """
    core = _strip_optional(annotation)
    item = _collection_item(core)
    if item is not None:
        return item if key == "-" or key.isdigit() else None
    args = get_args(core)
    if get_origin(core) in _MAPPING_ORIGINS and len(args) == 2:
        return args[1]
    if _is_model(core):
        name = field_keys(core).get(key)
        return None if name is None else core.model_fields[name].annotation
    return None


def lookup_type(annotation: Any, lookup: str, value: Any) -> tuple[bool, Any] | None:
    """What a filter value on a field declared ``annotation`` is validated as (spec §4.5).

    ``(False, X)``: the value as ``X``; ``(True, X)``: each item of the value (an iterable) as ``X``;
    ``None``: the value is bound as given. ``Annotated`` and ``Optional`` are stripped first, so
    ``Computed[S]`` qualifies. ``S`` is a rich scalar: ``datetime``, ``date``, ``timedelta``,
    ``Decimal``, ``UUID`` or ``Duration``. Pydantic's ``AwareDatetime``, ``PastDate``… give the plain
    ``datetime`` or ``date``: a filter bound is not a value of the field (``expires__lt=now`` on a
    ``FutureDatetime``), as the field's constraints (``Field(gt=…)``) do not apply to it either.

    - ``S`` with ``exact``/``gt``/``gte``/``lt``/``lte`` → ``S``; with ``in``/``not_in`` → each item as
      ``S``, or ``Range[S]`` when ``value`` is an ORM or SDK ``Range`` (range membership);
    - ``Range[T]`` or a bare ``Range`` with ``exact`` → the annotation itself (a range, its literal
      text or a builtin ``range``); ``Range[T]`` with ``contains``/``not_contains`` → ``T``;
    - ``list[S]``, ``set[S]``, ``frozenset[S]``, ``tuple[S, ...]`` (or ``Sequence``/``AbstractSet``) with
      ``contains``/``not_contains`` → ``S``; with ``containsall``/``containsany`` → each item as ``S``;
    - anything else → ``None``: another annotation, another lookup, a ``Range`` given to ``in`` on a
      field that is not ``S``, a union of several types, ``Range[Any]`` with ``contains``.
    """
    core = _plain_class(_strip_optional(annotation))
    if _is_lookup_scalar(core):
        if lookup in _COMPARISON_LOOKUPS:
            return False, core
        if lookup in _MEMBERSHIP_LOOKUPS:
            if isinstance(value, (Range, SdkRange)):
                range_of: Any = Range
                return False, range_of[core]
            return True, core
        return None
    if _is_subclass(core, Range) or _is_subclass(get_origin(core), Range):
        if lookup == "exact":
            return False, core
        args = get_args(core)
        if lookup in _CONTAINS_LOOKUPS and args and args[0] is not Any and not isinstance(args[0], TypeVar):
            return False, _plain_class(args[0])
        return None
    item = _plain_class(_collection_item(core))
    if item is not None and _is_lookup_scalar(item):
        if lookup in _CONTAINS_LOOKUPS:
            return False, item
        if lookup in _CONTAINS_ITEMS_LOOKUPS:
            return True, item
    return None


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
      ``SurrealFunc`` and any other object not listed below: unchanged (the same object).
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
    - a table-model instance (a ``BaseSurrealModel``, at any position — a raw variable, a list item, a
      nested object): its record id, the value it stands for as a filter (v0.22.0) and in the full
      ORM; an unsaved one has none: ``ValueError``. This function reads no annotation: the write
      paths check beforehand that a declared field accepts a ``RecordID`` where the instance sits
      (keyword writes, the ``atomic_*`` helpers, ``patch()``) and refuse it otherwise.
    - any other Pydantic model (a sub-model ``bulk_update(profile={…})`` validated into, a
      ``list[Profile]`` item…): the object a model write sends for it (:func:`model_object`) — each
      sub-field under :func:`read_keys`' key, the SDK values of its untyped positions put back —
      then encoded like a mapping. The SDK has no encoder for a Pydantic model.
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
    if isinstance(value, BaseModel):
        from .utils import as_record_value  # utils imports this module

        record = as_record_value(value)
        if record is not value:
            return encode_wire(record, stored=stored)
        return encode_wire(model_object(value), stored=stored)
    return value


# --- restore_native (spec §5.3) -----------------------------------------------------------------

_MISSING = object()
# The annotation of a position nothing declares: an extra column (``extra="allow"``), a value of a
# mapping given to :func:`restore_native` directly. Untyped, like ``Any``.
_UNDECLARED = object()
# The annotation of a position inside a declared value whose type the walk does not follow (a union
# of several types, a ``TypedDict``…): typed, so the dump is kept there.
_DECLARED = object()

# The SDK values :func:`restore_native` puts back: those the SDK encodes and both server lines store
# as the value they are — a duration (once ``encode_wire`` has made it a compact one), a range and a
# geometry. A bound on its own and ``surrealdb.Tokens`` are not among them: the server drops the
# WebSocket on a bound, and the SDK has no encoder for ``Tokens``; their dumps are written, as in
# v0.22.2.
_SDK_VALUES: tuple[type, ...] = tuple(
    cls for cls in (SdkDuration, SdkRange, SdkGeometry, SdkGeometryCollection) if isinstance(cls, type)
)


def _is_sdk_dataclass(value: object) -> bool:
    """Whether ``value`` is an instance of a dataclass the ``surrealdb`` package defines.

    The SDK's ``Duration``, ``Range``, ``BoundIncluded``/``BoundExcluded``, geometries and ``Tokens``:
    the values ``model_dump()`` flattens into plain dicts. The ORM's ``Duration`` and ``Range`` are
    not dataclasses (the dump keeps them), nor are ``RecordID`` and ``Datetime``.
    """
    return dataclasses.is_dataclass(value) and not isinstance(value, type) and type(value).__module__.startswith("surrealdb.")


def _untyped(annotation: Any) -> bool:
    """Whether a position declared ``annotation`` holds any value: nothing declared, ``Any``, ``object``,
    a ``TypeVar`` or an unresolved annotation (a string, a ``ForwardRef``), ``Optional``/``Annotated``
    stripped. A union of several types is not: one of its members may be the value's class."""
    if annotation is _UNDECLARED:
        return True
    core = _strip_optional(annotation)
    return core is Any or core is object or isinstance(core, (TypeVar, ForwardRef, str))


def _value_annotation(annotation: Any) -> Any:
    """The annotation of the values of a mapping declared ``annotation`` (``V`` of ``dict[K, V]``)."""
    if _untyped(annotation):
        return _UNDECLARED
    core = _strip_optional(annotation)
    if isinstance(core, type) and core in _MAPPING_ORIGINS:  # a bare ``dict``: ``dict[Any, Any]``
        return _UNDECLARED
    args = get_args(core)
    if get_origin(core) in _MAPPING_ORIGINS and len(args) == 2:
        return args[1]
    return _DECLARED


def _item_annotation(annotation: Any, position: int) -> Any:
    """The annotation of the item at ``position`` of a list or tuple declared ``annotation``."""
    if _untyped(annotation):
        return _UNDECLARED
    core = _strip_optional(annotation)
    if isinstance(core, type) and (core in _SEQUENCE_ORIGINS or core is tuple):  # a bare ``list``: ``list[Any]``
        return _UNDECLARED
    origin = get_origin(core)
    args = get_args(core)
    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return args[0]
        return args[position] if position < len(args) else _DECLARED
    if origin in _SEQUENCE_ORIGINS and args:
        return args[0]
    return _DECLARED


def _read_key(name: str, info: FieldInfo) -> str:
    """The key a sub-field's validation reads: what a write must store it under to read it back.

    Its ``validation_alias`` when that is a string; the first string of an ``AliasChoices``; its
    ``alias`` when no validation alias is set; else its name — also for an ``AliasPath``, which
    names a position, not a key. Never its ``serialization_alias``: nothing reads that key back. A
    sub-model with ``serialize_by_alias=True`` is still written under the keys its dump holds
    (:func:`model_object`), a ``serialization_alias`` among them.
    """
    alias = info.validation_alias
    if isinstance(alias, str):
        return alias
    if isinstance(alias, AliasChoices):
        return next((choice for choice in alias.choices if isinstance(choice, str)), name)
    if alias is None and isinstance(info.alias, str):
        return info.alias
    return name


def _validates_by_name(model: type[BaseModel]) -> bool:
    """Whether ``model`` reads its fields under their names: ``validate_by_name=True``, its older
    spelling ``populate_by_name=True`` (Pydantic keeps only the key given), or ``validate_by_alias=False``."""
    config = model.model_config
    return (
        config.get("validate_by_name") is True
        or config.get("populate_by_name") is True
        or config.get("validate_by_alias", True) is False
    )


@functools.cache
def read_keys(model: type[BaseModel]) -> Mapping[str, str]:
    """Field name → the key a write stores it under, cached per class; never mutate it (v0.23.0).

    The key :func:`model_object` writes each sub-field of an instance under (unless the dump is
    by alias, ``serialize_by_alias=True``), and each key of a keyword write's dict resolves to —
    except the dict of a keyword write for a model a read takes the aliases of first
    (:func:`reads_aliases_first`), sent with the caller's keys as in v0.22.2 —, so a read gives it
    back:

    - every field keeps its **name** when the model validates by name — ``validate_by_name=True``,
      ``populate_by_name=True`` (a table model's own config) or ``validate_by_alias=False``: the
      format v0.22 and earlier wrote, and the one a schema of that time declares
      (``DEFINE FIELD profile.first_name``);
    - otherwise (validation by alias only) each field takes the key its validation reads
      (:func:`_read_key`): an aliased sub-field written under its name was never read back.
    """
    if _validates_by_name(model):
        return {name: name for name in model.model_fields}
    return {name: _read_key(name, info) for name, info in model.model_fields.items()}


def reads_aliases_first(model: type[BaseModel]) -> bool:
    """Whether ``model`` validates by name **and** by alias, so a read takes a field's aliases before
    its name (v0.23.0).

    ``populate_by_name=True`` or ``validate_by_name=True``, ``validate_by_alias`` left on — a table
    model used as a sub-model among them. A write stores each field under its name
    (:func:`read_keys`), or under its alias with ``serialize_by_alias=True``, so the key a read takes
    first is not always the key a write stores: a keyword write's dict for such a model is sent with
    the caller's keys, as in v0.22.2 (:func:`~surreal_orm_lite.model_base._stored_keys`).
    """
    return _validates_by_name(model) and model.model_config.get("validate_by_alias", True) is not False


@functools.cache
def masking_keys(model: type[BaseModel]) -> Mapping[str, tuple[str, ...]]:
    """Field name → the other keys a read takes it from **before** the key a write stores it under (v0.23.0).

    Only a model that validates by name **and** by alias has any (:func:`reads_aliases_first`): a
    write stores each field under its name (:func:`read_keys`), but Pydantic reads its aliases
    first. They are the other keys :func:`field_keys` resolves to the field — its string
    ``alias``/``validation_alias``, the strings of an ``AliasChoices`` —, never a key another field
    is stored under. v0.22's ``merge()`` wrote a dict's keys as given, so a row can hold one of them
    next to the name, and a read gives that one: a ``merge()`` of a sub-model **instance** removes
    those of each sub-field its object writes (:func:`~surreal_orm_lite.model_base._with_removals`).

    Empty for every field of a model that validates by alias only (each field is stored under the
    first key a read takes) or by name only (``validate_by_alias=False``: no read takes an alias).
    Cached per class; never mutate the result.
    """
    if not reads_aliases_first(model):
        return dict.fromkeys(model.model_fields, ())
    stored = read_keys(model)
    taken = set(stored.values())
    others: dict[str, list[str]] = {name: [] for name in model.model_fields}
    for key, name in field_keys(model).items():
        if key != stored[name] and key not in taken:
            others[name].append(key)
    return {name: tuple(keys) for name, keys in others.items()}


def _raw_item(raw: Any, key: Any, annotation: Any) -> tuple[Any, Any]:
    """The value under ``key`` in ``raw`` (``_MISSING`` when there is none) and its annotation.

    A model: the field of that name, else the field that key aliases (:func:`field_keys` — a dump
    keyed by the read keys still finds the raw values behind them), with the field's annotation;
    else an extra, which nothing declares. A mapping declared ``annotation``: the value and the
    annotation of the mapping's values.
    """
    if isinstance(raw, BaseModel):
        if isinstance(key, str):
            fields = type(raw).model_fields
            name = key if key in fields else field_keys(type(raw)).get(key)
            if name is not None:
                return getattr(raw, name), fields[name].annotation
        extra = raw.__pydantic_extra__
        if extra is not None and key in extra:
            return extra[key], _UNDECLARED
        return _MISSING, _UNDECLARED
    if key in raw:
        return raw[key], _value_annotation(annotation)
    return _MISSING, _UNDECLARED


def restore_native(dumped: Any, raw: Any, *, read_keys: bool = False) -> Any:
    """``dumped`` with the SDK values of ``raw`` put back where ``model_dump()`` flattened them.

    ``model_dump()`` turns a dataclass found at an untyped position (a ``dict[str, Any]`` or
    ``list[Any]`` field, an ``extra="allow"`` column, the ``Any`` part of a nested model) into a plain
    dict: an SDK ``Duration`` loaded from the database becomes ``{"elapsed": 5400000000000}``, an SDK
    ``Range`` ``{"begin": {"value": 1}, "end": …}``, and a write would store those objects instead of
    the duration and the range. This walks the dump and the instance's own values (``raw``) in
    parallel, with the annotation each position is declared with (spec §5.3):

    - where the raw value is an SDK ``Duration``, ``Range`` or geometry at an **untyped** position —
      nothing declared (an extra, a value of a mapping given directly), ``Any``, ``object``, a
      ``TypeVar``, an unresolved annotation, or the values or items of a bare ``dict``/``list`` —, the
      raw value replaces its dump;
    - a dict dump recurses by key into a raw mapping (its values declared as the mapping's ``V``), or
      into a raw Pydantic model by field name (or a key that aliases it, declared as the field), then
      its extras (``__pydantic_extra__``, untyped);
    - a list or tuple dump recurses by position into a raw list or tuple of the same length (each item
      declared as the sequence's item type), and keeps its own container type;
    - anything else keeps the dump, as in v0.22.2: an SDK value at a **typed** position (a field
      declared ``surrealdb.Duration``, ``GeometryPoint``…, a union of several types), an SDK value the
      server does not store as one (a ``BoundIncluded``/``BoundExcluded`` on its own, which drops the
      WebSocket) or the SDK cannot encode (``surrealdb.Tokens``), a dataclass that is not the SDK's,
      the ORM's ``Duration``/``Range`` (not dataclasses: the dump keeps them as they are), mismatched
      shapes, a key missing from ``raw``, a raw ``set`` (no positions).

    ``read_keys=True`` (a model write, v0.23.0) also re-keys every **nested** model level — a raw
    Pydantic model below the top: each declared field goes under :func:`read_keys`' key (its name
    when the sub-model validates by name, else the key its validation reads, see
    :func:`model_object`), so the stored object reads back into the sub-model; extras and the keys
    of a plain dict are data and keep theirs. The top level keeps its names: a table model's columns
    come from ``_columns_for``.

    Copy-on-write: when nothing changes, ``dumped`` itself is returned; otherwise new containers along
    the changed paths only (untouched branches are shared). Neither argument is mutated.
    """
    return _restore(dumped, raw, False, read_keys, _UNDECLARED)


def model_object(model: BaseModel) -> dict[str, Any]:
    """The object a write sends for a sub-model instance, before encoding (v0.23.0).

    ``model_dump()``, then :func:`restore_native` with ``read_keys`` — the top level re-keyed too:
    each sub-field, at every depth, under :func:`read_keys`' key. A sub-model that validates by name
    (``populate_by_name``/``validate_by_name``, or ``validate_by_alias=False``) keeps the names, as
    v0.22 wrote them. Otherwise each sub-field goes under the key its validation reads — its
    ``validation_alias`` when that is a string, the first string of an ``AliasChoices``, else its
    ``alias``, else its name (also for an ``AliasPath``), never its ``serialization_alias``: a key
    nothing reads back would be a value lost. A sub-model with ``serialize_by_alias=True`` keeps
    the keys Pydantic dumps it under, as v0.22 wrote them and as ``model_dump(by_alias=True)``
    does: its aliases, which a read takes first, but also a ``serialization_alias`` no read takes,
    which does not read back. Extras keep their key; the SDK values of the untyped positions are
    put back.
    """
    result: dict[str, Any] = _restore(model.model_dump(), model, True, True, _UNDECLARED)
    return result


def _restore(dumped: Any, raw: Any, rekey: bool, rekey_nested: bool, annotation: Any) -> Any:
    """:func:`restore_native`'s walk; ``rekey``: re-key this level when ``raw`` is a model, ``rekey_nested``
    the levels below; ``annotation``: what this position is declared as."""
    if _is_sdk_dataclass(raw):
        return raw if isinstance(raw, _SDK_VALUES) and _untyped(annotation) else dumped
    if isinstance(dumped, dict):
        if not isinstance(raw, (BaseModel, Mapping)):
            return dumped
        keys = read_keys(type(raw)) if rekey and isinstance(raw, BaseModel) else None
        changed = False
        items: list[tuple[Any, Any]] = []
        for key, item in dumped.items():
            raw_item, item_annotation = _raw_item(raw, key, annotation)
            new = item if raw_item is _MISSING else _restore(item, raw_item, rekey_nested, rekey_nested, item_annotation)
            new_key = keys.get(key, key) if keys is not None and isinstance(key, str) else key
            changed = changed or new is not item or new_key != key
            items.append((new_key, new))
        return dict(items) if changed else dumped
    if isinstance(dumped, (list, tuple)) and isinstance(raw, (list, tuple)) and len(dumped) == len(raw):
        restored = [
            _restore(item, raw_item, rekey, rekey_nested, _item_annotation(annotation, position))
            for position, (item, raw_item) in enumerate(zip(dumped, raw, strict=True))
        ]
        if all(new is old for new, old in zip(restored, dumped, strict=True)):
            return dumped
        return restored if isinstance(dumped, list) else tuple(restored)
    return dumped
