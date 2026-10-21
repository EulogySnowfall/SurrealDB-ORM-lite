"""A model's columns and their SurrealQL kinds, read from its annotations (v0.34.0).

``field_kind()`` maps a field's annotation to the kind its ``DEFINE FIELD … TYPE`` writes, as a ``define_parser.Kind``
rendered by ``render_kind()``; ``model_columns()`` lists every column an ORM write of a model may send, each a
``Column``: its path, its kind, whether it holds a free-form object, the ``FLEXIBLE`` it asks for, its ``DEFAULT``
literal and, for a computed field, its expression.

**The frontier.** This module is the only reader of annotations for DDL: the table DDL of a model (``table_ddl()``,
``define_table()``) takes its columns from ``model_columns()`` and nothing else. It is pure — no connection, the same
result for the same model — and it imports no model module (``model_base``, ``edge``, ``model_auth``) nor
``connection_manager``, so each of them can import it: a record model is told by the ``__surreal_edge__`` marker every
``BaseSurrealModel`` carries.

**What is written, and how.** A kind comes from a Python type, never from text the application wrote: a literal kind
(``Literal[…]``, an enum's values) and a ``DEFAULT`` go through the closed encoder ``to_surql_literal()``, which writes
only the values it has a verified form for. A computed field's expression is application code embedded as written —
SurrealQL takes no bound parameter in DDL — after the structural check of ``define_parser.scan_fragment()``: literals and
quoted identifiers terminated, brackets balanced, no comment marker and no NUL. A comment marker would swallow the
clauses written after the expression (``ASSERT``, ``PERMISSIONS``, ``COMMENT``). The check catches mistakes; it is not
an injection defence: an expression is trusted application code, never user input.

**The statements.** ``field_statement()`` writes one column's ``DEFINE FIELD``: its path segment by segment, each name
in backticks and an array's items ``*`` (`` `subs`.*.`city` ``), the table in backticks, then ``TYPE <kind>``,
``FLEXIBLE`` on a ``SCHEMAFULL`` table when the column holds a free-form object or asks for it, its ``DEFAULT`` and a
computed field's ``VALUE``. ``field_permissions_text()`` writes a field's ``PERMISSIONS`` as SurrealDB renders them.
"""

from collections.abc import Collection, Iterable, Mapping, MutableSequence, MutableSet, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from types import NoneType, UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel
from pydantic.fields import ComputedFieldInfo, FieldInfo
from pydantic_core import PydanticUndefined

from ._sdk import RecordID, SdkDuration, SdkRange
from .define_parser import DefineParseError, Kind, KLiteral, KName, KObject, KTuple, KUnion, render_kind, scan_fragment
from .geo import _ALL_KINDS, _geo_kinds, _GeoMarker, _ordered
from .schema import _identifier_name, _overwrite_clause
from .surql_literal import _identifier, to_surql_literal
from .types import Duration, Range

__all__ = [
    "Column",
    "field_kind",
    "field_path",
    "field_permissions_text",
    "field_statement",
    "holds_any",
    "holds_object",
    "model_columns",
    "untyped_arrays",
]

_ANY = KName("any")
_NONE = KLiteral("none")
_OBJECT = KName("object")
_NUMBER_MEMBERS = frozenset({"int", "float", "decimal"})
#: The operations of a field's ``PERMISSIONS``, in the order SurrealDB renders them.
_FIELD_OPERATIONS = ("select", "create", "update")
#: The generic origins of an array: ``list[T]``, ``Sequence[T]``, ``MutableSequence[T]`` (``tuple`` apart).
_ARRAY_ORIGINS: tuple[Any, ...] = (list, Sequence, MutableSequence)
#: The generic origins of a set: ``set[T]``, ``frozenset[T]``, ``AbstractSet[T]``, ``MutableSet[T]``.
_SET_ORIGINS: tuple[Any, ...] = (set, frozenset, AbstractSet, MutableSet)
#: The default values written as a ``DEFAULT`` literal (a ``str`` or ``int`` enum member is written as its value).
_DEFAULT_TYPES = (bool, int, float, Decimal, str, datetime, date, timedelta, Duration, SdkDuration, UUID)
# ``Annotated`` subscripted with values built at run time (mypy takes it for a type expression).
_ANNOTATED: Any = Annotated


@dataclass(frozen=True, slots=True)
class Column:
    """One column a model's table DDL defines (v0.34.0).

    ``path``: the unescaped segments, ``"*"`` for an array's items (``("address", "city")``, ``("subs", "*", "city")``,
    ``("bag", "*")``); the first is the column of a top-level field (its alias when it has one), the others the field
    names of a sub-model. ``field``: the Python name of the top-level field the column belongs to, for messages.
    ``kind``: the kind ``TYPE`` writes. ``free_object``: the kind holds a free-form object — a ``dict``, a record model,
    a sub-model where its fields are not defined — or is a sub-model with ``extra="allow"``, so a ``SCHEMAFULL`` table
    writes it ``FLEXIBLE``. ``flexible_requested``: the field is in ``flexible_fields`` or has
    ``json_schema_extra={"flexible": True}``. ``holds_any``: the kind holds ``any`` (an untyped array or set included).
    ``default``: the SurrealQL literal of its ``DEFAULT``, or ``None``. ``value``: a computed field's checked expression.
    ``server``: a top-level ``server_fields`` column. ``item``: the item definition of an untyped array (``bag.*``).
    """

    path: tuple[str, ...]
    field: str
    kind: Kind
    free_object: bool = False
    flexible_requested: bool = False
    holds_any: bool = False
    default: str | None = None
    value: str | None = None
    server: bool = False
    item: bool = False


# --- kinds -----------------------------------------------------------------------------------------------------------


def _unwrap(annotation: Any, metadata: tuple[Any, ...]) -> tuple[Any, tuple[Any, ...]]:
    """*annotation* without its ``Annotated`` layers, and *metadata* with theirs."""
    while get_origin(annotation) is Annotated:
        metadata = (*metadata, *annotation.__metadata__)
        annotation = annotation.__origin__
    return annotation, metadata


def _is_union(annotation: Any) -> bool:
    origin = get_origin(annotation)
    return origin is Union or origin is UnionType


def _is_record_model(annotation: Any) -> bool:
    """A ``BaseSurrealModel`` or ``BaseSurrealEdge`` subclass, told by the ``__surreal_edge__`` marker both carry."""
    return isinstance(annotation, type) and isinstance(getattr(annotation, "__surreal_edge__", None), bool)


def _is_sub_model(annotation: Any) -> bool:
    """A Pydantic model that is not a record model: its fields are written inside the record."""
    return isinstance(annotation, type) and issubclass(annotation, BaseModel) and not _is_record_model(annotation)


def _admits_none(annotation: Any) -> bool:
    """Whether the annotation accepts ``None``: ``X | None``, ``Optional[X]``, ``Literal[…, None]``, ``Any``, ``object``."""
    annotation, _ = _unwrap(annotation, ())
    if annotation is None or annotation is NoneType or annotation is Any or annotation is object:
        return True
    if _is_union(annotation):
        return any(_admits_none(member) for member in get_args(annotation))
    if get_origin(annotation) is Literal:
        return None in get_args(annotation)
    return False


def _members(kind: Kind) -> tuple[Kind, ...]:
    return kind.members if isinstance(kind, KUnion) else (kind,)


def _join(members: Iterable[Kind]) -> Kind:
    """One kind from *members*: flattened, de-duplicated by their text in order, a lone member alone."""
    unique: list[Kind] = []
    seen: set[str] = set()
    for member in members:
        for flat in _members(member):
            text = render_kind(flat)
            if text not in seen:
                seen.add(text)
                unique.append(flat)
    return unique[0] if len(unique) == 1 else KUnion(tuple(unique))


def _union(mapped: Iterable[tuple[Kind, bool]]) -> tuple[Kind, bool]:
    """The kind of a union: its members' kinds joined, ``any`` when one is ``any`` (``option<any>`` and ``object | any``
    are parse errors), ``number`` for exactly ``int``, ``float`` and ``decimal``."""
    kinds: list[Kind] = []
    free = False
    for kind, member_free in mapped:
        if any(member == _ANY for member in _members(kind)):
            return _ANY, False
        kinds.append(kind)
        free = free or member_free
    joined = _join(kinds)
    rest = [member for member in _members(joined) if member != _NONE]
    if len(rest) == len(_NUMBER_MEMBERS) and {render_kind(member) for member in rest} == _NUMBER_MEMBERS:
        number = KName("number")
        joined = KUnion((_NONE, number)) if len(rest) < len(_members(joined)) else number
    return joined, free


def _literal_kind(value: Any) -> Kind | None:
    """The literal kind of one ``str`` or ``int`` value (an enum member by its value), ``None`` when it has none."""
    raw = value.value if isinstance(value, Enum) else value
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        return None
    try:
        return KLiteral(to_surql_literal(raw))
    except TypeError:  # a NUL character, an int beyond 64 bits: no SurrealQL literal
        return None


def _literal(values: tuple[Any, ...]) -> Kind:
    """``Literal[…]``: ``str`` and ``int`` values as literals, ``bool`` values as ``bool``, ``None`` as ``none``; any
    other value makes the whole kind ``any``."""
    members: list[Kind] = []
    for value in values:
        if value is None:
            members.append(_NONE)
        elif isinstance(value, bool):
            members.append(KName("bool"))
        else:
            literal = _literal_kind(value)
            if literal is None:
                return _ANY
            members.append(literal)
    return _join(members) if members else _ANY


def _enum(cls: type[Enum]) -> Kind:
    """An enum whose values are all ``str``, or all ``int``: the union of its values' literals in definition order;
    any other enum: ``any``."""
    values = [member.value for member in cls]
    if not values:
        return _ANY
    if not (
        all(isinstance(value, str) for value in values)
        or all(isinstance(value, int) and not isinstance(value, bool) for value in values)
    ):
        return _ANY
    members = [_literal_kind(value) for value in values]
    return _ANY if any(member is None for member in members) else _join(member for member in members if member is not None)


def _class(cls: type) -> tuple[Kind, bool]:
    """A class used as a whole annotation (``list``, ``dict`` and ``set`` bare)."""
    if _is_record_model(cls) or issubclass(cls, BaseModel) or issubclass(cls, Mapping):
        return _OBJECT, True
    if issubclass(cls, Enum):
        return _enum(cls), False
    named: tuple[tuple[type | tuple[type, ...], str], ...] = (
        (bool, "bool"),
        (int, "int"),
        (float, "float"),
        (Decimal, "decimal"),
        (str, "string"),
        (bytes, "bytes"),
        (date, "datetime"),
        ((timedelta, Duration, SdkDuration), "duration"),
        (UUID, "uuid"),
        (RecordID, "record"),
        ((Range, SdkRange), "range"),
        ((list, tuple), "array"),
        ((set, frozenset), "set"),
    )
    for types, name in named:
        if issubclass(cls, types):
            return KName(name), False
    if cls in (Sequence, MutableSequence):
        return KName("array"), False
    if cls in (AbstractSet, MutableSet):
        return KName("set"), False
    return _ANY, False


def _container(name: str, item: Any) -> tuple[Kind, bool]:
    """``array<T>`` or ``set<T>``; ``array`` or ``set`` when the item is ``any`` (untyped)."""
    kind, free = _map(item, ())
    if kind == _ANY:
        return KName(name), False
    return KName(name, (kind,)), free


def _generic(origin: Any, args: tuple[Any, ...]) -> tuple[Kind, bool]:
    """A subscripted annotation other than a union, a literal or ``Annotated``."""
    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return _container("array", args[0])
        if not args or args == ((),):
            return KName("array"), False
        items = [_map(arg, ()) for arg in args]
        return KTuple(tuple(kind for kind, _ in items)), any(free for _, free in items)
    if origin in _ARRAY_ORIGINS:
        return _container("array", args[0] if args else Any)
    if origin in _SET_ORIGINS:
        return _container("set", args[0] if args else Any)
    if isinstance(origin, type):
        return _class(origin)
    return _ANY, False


def _map(annotation: Any, metadata: tuple[Any, ...]) -> tuple[Kind, bool]:
    """``(kind, free)`` of an annotation: its kind, and whether it holds a free-form object.

    A Pydantic model here is a free-form object: only a column's own annotation (``M``, ``M | None``, ``list[M]``,
    ``list[M] | None``) defines a sub-model's fields, which :func:`model_columns` decides.
    """
    annotation, metadata = _unwrap(annotation, metadata)
    marker = next((item for item in metadata if isinstance(item, _GeoMarker)), None)
    kinds = _geo_kinds(annotation if marker is None else _ANNOTATED[annotation, marker])
    if kinds is not None:
        geometry = KName("geometry") if kinds == _ALL_KINDS else KName("geometry", _ordered(kinds))
        return (KUnion((_NONE, geometry)) if _admits_none(annotation) else geometry), False
    if annotation is Any or annotation is object:
        return _ANY, False
    if annotation is None or annotation is NoneType:
        return _NONE, False
    origin = get_origin(annotation)
    if origin is Union or origin is UnionType:
        return _union(_map(member, ()) for member in get_args(annotation))
    if origin is Literal:
        return _literal(get_args(annotation)), False
    if origin is not None:
        return _generic(origin, get_args(annotation))
    if isinstance(annotation, type):
        return _class(annotation)
    return _ANY, False


def field_kind(annotation: Any, metadata: Iterable[Any] = ()) -> Kind:
    """The SurrealQL kind of a field annotation (v0.34.0), rendered by ``render_kind()``.

    *annotation* and *metadata* as Pydantic stores them (``FieldInfo.annotation`` and ``FieldInfo.metadata``: a
    geometry alias's marker, ``Computed``'s), or one annotation, ``Annotated`` included. ``Annotated`` is unwrapped,
    then: ``str`` and its subclasses ``string``; ``bool``; ``int``; ``float``; ``Decimal`` ``decimal``; a union of
    exactly ``int``, ``float`` and ``Decimal`` ``number``; ``datetime`` and ``date`` ``datetime``; ``timedelta``, the
    ORM's and the SDK's ``Duration`` ``duration``; ``UUID`` ``uuid``; ``bytes``; ``RecordID`` ``record``; a geometry
    field or class ``geometry<…>`` (its kinds in SurrealDB's order, bare ``geometry`` for every kind); ``Range``
    ``range``; ``Literal[…]`` the union of its ``str`` and ``int`` values (``bool`` as ``bool``, ``None`` as
    ``none``, any other value ``any``); an enum whose values are all ``str``, or all ``int``, the union of its
    values; ``list[T]``, ``Sequence[T]``, ``tuple[T, ...]`` ``array<T>``; ``tuple[A, B]`` ``[A, B]``; ``set[T]``,
    ``frozenset[T]`` ``set<T>``; ``array`` and ``set`` when ``T`` is absent or ``any``; a mapping, a Pydantic model and
    a record model ``object``; ``X | None`` ``option<X>``; a union its members de-duplicated, ``any`` when one is
    ``any``; ``Any``, ``object`` and every other type (the SDK's ``Datetime`` included) ``any``. Pure.
    """
    return _map(annotation, tuple(metadata))[0]


def _kind_args(kind: KName) -> Iterable[Kind]:
    return (argument for argument in kind.args if not isinstance(argument, str))


def _children(kind: Kind) -> Iterable[Kind]:
    if isinstance(kind, KName):
        return _kind_args(kind)
    if isinstance(kind, KObject):
        return (value for _, value in kind.items)
    if isinstance(kind, KUnion):
        return kind.members
    if isinstance(kind, KTuple):
        return kind.items
    return ()


def holds_object(kind: Kind) -> bool:
    """Whether *kind* holds ``object`` anywhere: alone, in a union, as an item (``option<array<object>>``). Pure."""
    if isinstance(kind, KName) and kind.name == "object":
        return True
    return any(holds_object(child) for child in _children(kind))


def _untyped(kind: KName) -> bool:
    """An ``array`` or ``set`` written without an item kind: ``array``, ``set``."""
    return kind.name in ("array", "set") and not any(True for _ in _kind_args(kind))


def holds_any(kind: Kind) -> bool:
    """Whether *kind* holds ``any`` anywhere — an untyped ``array`` or ``set`` included (``[int, any]``,
    ``option<array>``, ``set``). Pure."""
    if isinstance(kind, KName) and (kind.name == "any" or _untyped(kind)):
        return True
    return any(holds_any(child) for child in _children(kind))


def _collect_untyped(kind: Kind, depth: int, depths: set[int]) -> None:
    if isinstance(kind, KUnion):
        for member in kind.members:
            _collect_untyped(member, depth, depths)
    elif isinstance(kind, KName) and kind.name == "array":
        items = list(_kind_args(kind))
        if not items or any(member == _ANY for member in _members(items[0])):
            depths.add(depth + 1)
        else:
            _collect_untyped(items[0], depth + 1, depths)


def untyped_arrays(kind: Kind) -> list[int]:
    """The item depths of the untyped array positions of *kind*, in increasing order (v0.34.0).

    An untyped array — ``array``, or one whose item kind is ``any`` — alone, in a union, in ``option<…>`` or as the item
    of another array, gets an item definition whose path ends with one ``*`` per level: ``array`` → ``[1]``
    (``col.*``), ``array<array>`` → ``[2]`` (``col.*.*``), ``option<array> | int`` → ``[1]``. Typed arrays
    (``array<string>``, ``array<object>``) and sets give none. Pure.
    """
    depths: set[int] = set()
    _collect_untyped(kind, 0, depths)
    return sorted(depths)


# --- columns ---------------------------------------------------------------------------------------------------------


def _sub_model_shape(
    annotation: Any, metadata: tuple[Any, ...], trail: tuple[type, ...]
) -> tuple[type[BaseModel], bool] | None:
    """``(M, star)`` when the annotation is ``M``, ``M | None``, ``list[M]`` or ``list[M] | None`` for a sub-model ``M``
    that is not already on the *trail* (``star``: its fields are under ``*``); ``None`` otherwise."""
    annotation, _ = _unwrap(annotation, metadata)
    if _is_union(annotation):
        args = get_args(annotation)
        rest = [member for member in args if member is not NoneType]
        if len(rest) != 1 or len(rest) == len(args):
            return None
        annotation, _ = _unwrap(rest[0], ())
    star = False
    origin = get_origin(annotation)
    args = get_args(annotation)
    if (origin is tuple and len(args) == 2 and args[1] is Ellipsis) or (origin in _ARRAY_ORIGINS and len(args) == 1):
        annotation, _ = _unwrap(args[0], ())
        star = True
    if _is_sub_model(annotation) and annotation not in trail:
        return annotation, star
    return None


def _default_text(value: Any) -> str | None:
    """The ``DEFAULT`` literal of a constant default: a scalar of :data:`_DEFAULT_TYPES` or a ``str``/``int`` enum
    member's value; ``None`` for anything else or a value with no SurrealQL literal."""
    if isinstance(value, Enum):
        value = value.value
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return None
    elif not isinstance(value, _DEFAULT_TYPES):
        return None
    try:
        return to_surql_literal(value)
    except TypeError:  # no verified literal (a NUL character, an int beyond 64 bits): no DEFAULT
        return None


def _flexible_extra(info: FieldInfo | ComputedFieldInfo) -> bool:
    extra = info.json_schema_extra
    return isinstance(extra, dict) and extra.get("flexible") is True


@dataclass(frozen=True, slots=True)
class _Field:
    """One field to turn into columns: where it is and what Pydantic knows of it."""

    model: str
    field: str
    path: tuple[str, ...]
    annotation: Any
    metadata: tuple[Any, ...]
    required: bool
    default: Any
    factory: bool
    value: str | None = None
    server: bool = False
    flexible: bool = False


def _from_field_info(
    model: str,
    field: str,
    path: tuple[str, ...],
    info: FieldInfo,
    *,
    value: str | None = None,
    server: bool = False,
    flexible: bool = False,
) -> _Field:
    return _Field(
        model=model,
        field=field,
        path=path,
        annotation=info.annotation,
        metadata=tuple(info.metadata),
        required=info.is_required(),
        default=info.default,
        factory=info.default_factory is not None,
        value=value,
        server=server,
        flexible=flexible,
    )


def _from_computed_info(
    model: str, field: str, path: tuple[str, ...], info: ComputedFieldInfo, *, flexible: bool = False
) -> _Field:
    """A Pydantic ``@computed_field``: always dumped, so always present; its kind from its return type."""
    return _Field(
        model=model,
        field=field,
        path=path,
        annotation=info.return_type,
        metadata=(),
        required=True,
        default=PydanticUndefined,
        factory=False,
        flexible=flexible,
    )


def _sub_fields(model: str, field: str, base: tuple[str, ...], sub: type[BaseModel]) -> list[_Field]:
    """A sub-model's fields (but ``Field(exclude=True)``), then its Pydantic computed fields, under *base*."""
    fields = [
        _from_field_info(model, field, (*base, name), info)
        for name, info in sub.model_fields.items()
        if info.exclude is not True
    ]
    fields.extend(_from_computed_info(model, field, (*base, name), info) for name, info in sub.model_computed_fields.items())
    return fields


def _check_expression(model: str, field: str, expression: str) -> None:
    """Refuse a computed field's expression ``scan_fragment()`` does not accept, with its reason."""
    try:
        scan_fragment(expression)
    except DefineParseError as error:
        raise ValueError(f"{model}: the computed field {field!r} is not SurrealQL the ORM can embed: {error}.") from error


def _field_columns(spec: _Field, trail: tuple[type, ...]) -> list[Column]:
    """The column of one field, its item definitions, then the columns of its sub-model's fields (depth first)."""
    if spec.value is not None:
        _check_expression(spec.model, spec.field, spec.value)
    kind, free = _map(spec.annotation, spec.metadata)
    shape = _sub_model_shape(spec.annotation, spec.metadata, trail)
    if shape is not None:
        free = shape[0].model_config.get("extra") == "allow"
    nullable = _admits_none(spec.annotation) or (not spec.required and spec.default is None)
    if nullable and kind != _ANY and _NONE not in _members(kind):
        kind = KUnion((_NONE, *_members(kind)))
    if spec.flexible and not holds_object(kind):
        raise ValueError(
            f"{spec.model}: the field {spec.field!r} is FLEXIBLE (flexible_fields or json_schema_extra), but its kind "
            f"{render_kind(kind)} holds no object; SurrealDB accepts FLEXIBLE on object kinds only."
        )
    constant = not (nullable or spec.required or spec.factory or spec.value is not None)
    columns = [
        Column(
            path=spec.path,
            field=spec.field,
            kind=kind,
            free_object=free,
            flexible_requested=spec.flexible,
            holds_any=holds_any(kind),
            default=_default_text(spec.default) if constant else None,
            value=spec.value,
            server=spec.server,
        )
    ]
    columns.extend(
        Column(path=(*spec.path, *("*",) * depth), field=spec.field, kind=_ANY, holds_any=True, item=True)
        for depth in untyped_arrays(kind)
    )
    if shape is not None:
        sub, star = shape
        base = (*spec.path, "*") if star else spec.path
        for child in _sub_fields(spec.model, spec.field, base, sub):
            columns.extend(_field_columns(child, (*trail, sub)))
    return columns


def model_columns(
    model: type[BaseModel],
    *,
    columns: Mapping[str, str],
    computed: Mapping[str, str],
    flexible: Collection[str],
    exclude: Collection[str],
    server: Collection[str],
) -> tuple[Column, ...]:
    """Every column an ORM write of *model* may send, in declaration order (v0.34.0). Pure.

    What ``model_dump()`` writes: each field of ``model_fields`` but those named in *exclude* (``id``, an edge's
    ``in_``/``out``) and those with ``Field(exclude=True)``, then each Pydantic ``@computed_field`` (its kind from its
    return type). A top-level column is named by *columns* (Python name → column, the model's alias map), else by the
    field's name. Each column is followed by the item definition of each untyped array position (``bag.*``,
    ``lol.*.*``: kind ``any``, ``item=True``) and, for a sub-model ``M`` annotated ``M``, ``M | None``, ``list[M]`` or
    ``list[M] | None``, by the columns of its fields and computed fields (``address.city``, ``subs.*.city``) by the
    same rules, depth first. ``M`` anywhere else, a record model and a sub-model already on the path are free-form
    objects without nested columns.

    A column is ``option<…>`` when its annotation admits ``None`` or it is not required and defaults to ``None``, never
    when its kind is ``any``. Its ``default`` is the ``to_surql_literal()`` text of a constant default (a ``bool``,
    ``int``, ``float``, ``Decimal``, ``str``, ``datetime``, ``date``, ``timedelta``, ``Duration``, ``UUID`` or a
    ``str``/``int`` enum member's value) of a column that admits no ``None``, is not computed and has no
    ``default_factory``. *computed* maps a ``Computed`` field's Python name to its expression, written as the column's
    ``value``; *flexible* names the top-level fields declared in ``flexible_fields`` (a field's
    ``json_schema_extra={"flexible": True}`` asks the same); *server* the top-level ``server_fields``.

    :raises ValueError: a field asks for ``FLEXIBLE`` and its kind holds no object; a computed field's expression is
        not SurrealQL ``scan_fragment()`` accepts (a comment marker, unbalanced brackets, an unterminated literal or
        identifier, a NUL character). The messages name the model and the field, in declaration order: the first
        field at fault raises.
    """
    name = model.__name__
    trail: tuple[type, ...] = (model,)
    result: list[Column] = []
    for field, info in model.model_fields.items():
        if field in exclude or info.exclude is True:
            continue
        spec = _from_field_info(
            name,
            field,
            (columns.get(field, field),),
            info,
            value=computed.get(field),
            server=field in server,
            flexible=field in flexible or _flexible_extra(info),
        )
        result.extend(_field_columns(spec, trail))
    for field, computed_info in model.model_computed_fields.items():
        spec = _from_computed_info(
            name, field, (columns.get(field, field),), computed_info, flexible=_flexible_extra(computed_info)
        )
        result.extend(_field_columns(spec, trail))
    return tuple(result)


# --- statements ------------------------------------------------------------------------------------------------------


def field_path(path: Sequence[str]) -> str:
    """A column's path as a ``DEFINE FIELD`` writes it: each segment an identifier in backticks, an array's items ``*``
    bare, joined with ``.`` (`` `address`.`city` ``, `` `subs`.*.`city` ``, `` `a.b` `` for one segment holding a dot).
    Pure.
    """
    return ".".join("*" if segment == "*" else _identifier(segment) for segment in path)


def field_statement(table: str, column: Column, *, overwrite: bool, schemafull: bool, before_3: bool = False) -> str:
    """``DEFINE FIELD {OVERWRITE | IF NOT EXISTS} <path> ON `<table>` TYPE <kind>[ FLEXIBLE][ DEFAULT <literal>][ VALUE
    <expression>];`` — the statement of a new column (v0.34.0).

    The path through :func:`field_path`, *table* checked as an identifier and written in backticks, the kind through
    ``render_kind()``. ``FLEXIBLE`` (written after ``TYPE``, which both lines accept) only when *schemafull* — the
    table's mode once its statement is applied is ``SCHEMAFULL``: SurrealDB 3.x refuses ``FLEXIBLE`` elsewhere — and the
    column holds a free-form object (``free_object``), asks for it (``flexible_requested``) or, with *before_3* (a
    SurrealDB 2.x server or the embedded engine), holds ``any``. ``DEFAULT`` writes the column's constant default (a
    ``server_fields`` column's too: a new column has no rule of the database's to keep), ``VALUE`` a computed field's
    expression, checked when the columns were listed. ``overwrite=True`` redefines an existing field, ``False`` keeps it
    (``IF NOT EXISTS``); a non-``bool`` is a ``TypeError``. Pure.
    """
    parts = [
        f"DEFINE FIELD {_overwrite_clause(overwrite)} {field_path(column.path)} ON "
        f"{_identifier(_identifier_name(table, what='Table'))} TYPE {render_kind(column.kind)}"
    ]
    if schemafull and (column.free_object or column.flexible_requested or (before_3 and column.holds_any)):
        parts.append("FLEXIBLE")
    if column.default is not None:
        parts.append(f"DEFAULT {column.default}")
    if column.value is not None:
        parts.append(f"VALUE {column.value}")
    return " ".join(parts) + ";"


def field_permissions_text(permissions: Mapping[str, str]) -> str | None:
    """A field's ``PERMISSIONS`` as SurrealDB renders them, without the keyword (v0.34.0).

    *permissions* maps ``select``, ``create`` and ``update`` to ``"FULL"``, ``"NONE"`` or ``"WHERE <expr>"`` (a stored
    field's ``FieldState.permissions``); an operation left out is ``FULL``, a field's default. ``None`` when the three
    are ``FULL`` — nothing to write —, ``"NONE"`` when the three are ``NONE``, else the ``FOR`` groups in the order the
    rules first appear, ``FULL`` groups included, each listing its operations in the order select, create, update:
    ``FOR select WHERE $auth.id = id, FOR create, update FULL``. Pure.
    """
    rules = {operation: permissions.get(operation, "FULL") for operation in _FIELD_OPERATIONS}
    if all(rule == "FULL" for rule in rules.values()):
        return None
    if all(rule == "NONE" for rule in rules.values()):
        return "NONE"
    groups: dict[str, list[str]] = {}
    for operation, rule in rules.items():
        groups.setdefault(rule, []).append(operation)
    return ", ".join("FOR " + ", ".join(operations) + " " + rule for rule, operations in groups.items())
