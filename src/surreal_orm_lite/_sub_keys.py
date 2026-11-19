"""Internal: the keys a sub-model's fields are stored under (issue #218).

Not part of the public API — nothing here is exported by ``surreal_orm_lite``.

A sub-model (a Pydantic ``BaseModel`` used as the type of a field, at any depth, also inside a
``list``, a ``dict`` or ``X | None``) is read back by Pydantic's validation, which takes each
sub-field from the first of its **read keys** present in the stored object: its validation
aliases in order (an ``AliasChoices`` gives several), then its name under ``validate_by_name``
(``populate_by_name``). ``model_dump()`` writes it under its **dumped key**: the name, or its
serialization alias with ``serialize_by_alias=True``. The ORM writes each sub-field under one
**key** (``SubField.key``): the dumped key when a read takes it, so ``save()`` keeps writing what
it always wrote, else the first read key (a ``serialization_alias`` no read takes, case (a) of
#218: the value would otherwise be stored where no read finds it).

Three walkers apply that rule:

* :func:`rekeyed` — a value that **replaces** what is stored: ``save()``, ``update()``,
  ``upsert()`` (the dump of the instance), ``bulk_update()`` and ``merge(server_values=…)``
  (``SET field = $value``). Every sub-field goes under its key; the caller may name it by any
  read key, its name or its dumped key, but not twice.
* :func:`merged` — a value ``MERGE`` deep-merges into the stored object (``merge()``). The
  server keeps every key the payload does not name, and the ORM does not see the stored row, so
  a sub-field another spelling of which is stored may be hidden by it. A scalar (anything but an
  object) goes under its key, and every read key a read tries before it is removed (``NONE``):
  the read then takes the value just written. ``None`` removes every read key. An object goes
  under the read key the stored object already uses — the first one present, chosen by the
  server (:class:`Pick`) — so it is merged into the object a read takes, and no spelling holding
  an object is ever removed.
* :func:`content` — the ``MERGE`` expression of a payload holding a :class:`Pick`, every key and
  value bound.

A model whose sub-models all read and write each sub-field under its name only (no alias, no
``validate_by_name``…) has no shape (:func:`field_shape` gives ``None``): its payloads are left
exactly as they were.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import sys
import types
import typing
from collections.abc import Mapping
from typing import Any, Final, TypeAlias, Union, get_args, get_origin

from pydantic import AliasChoices, AliasPath, BaseModel

__all__ = [
    "MAX_PICKS",
    "Choice",
    "Entries",
    "Items",
    "Model",
    "Pick",
    "SubField",
    "SubKeys",
    "Values",
    "content",
    "field_shape",
    "holds_model",
    "keys_of",
    "merged",
    "rekeyed",
    "shapes_of",
]

#: The most :class:`Pick` nested on one path of a ``MERGE``: each one repeats the path above it twice or more in the
#: statement, and SurrealDB refuses a statement nested too deep (8 levels on 3.3.5 and 4.0.1, measured).
MAX_PICKS: Final = 3


@dataclasses.dataclass(frozen=True, slots=True)
class SubField:
    """One field of a sub-model, as the ORM writes it.

    ``reads``: the keys a read takes, in the order it tries them. ``key``: the key the ORM writes — the key
    ``model_dump()`` writes when ``reads`` holds it, else ``reads[0]``; ``None`` when no read takes a plain key (an
    ``AliasPath`` of several steps only). ``annotation``: the field's annotation, for the shape below it.
    """

    name: str
    reads: tuple[str, ...]
    key: str | None
    annotation: Any


@dataclasses.dataclass(frozen=True, slots=True)
class SubKeys:
    """The keys of a sub-model class: its fields by name, the field each dumped key and each spelling names.

    ``spellings`` maps every key that names a field — its read keys, its name and its dumped key — to the field's
    name, or to ``None`` when two fields claim it. ``plain``: every field is read and written under its name only.
    """

    fields: Mapping[str, SubField]
    dumped: Mapping[str, str]
    spellings: Mapping[str, str | None]
    plain: bool


@dataclasses.dataclass(frozen=True, slots=True)
class Model:
    """A sub-model class whose keys, or those of a sub-model below it, the ORM rewrites."""

    model: type[BaseModel]


@dataclasses.dataclass(frozen=True, slots=True)
class Items:
    """A ``list``, ``tuple`` or set whose items have a shape."""

    item: Shape


@dataclasses.dataclass(frozen=True, slots=True)
class Values:
    """A ``dict`` whose values have a shape."""

    value: Shape


@dataclasses.dataclass(frozen=True, slots=True)
class Choice:
    """A union of several shapes, told apart by the value an instance holds."""

    members: tuple[Shape, ...]


Shape: TypeAlias = Model | Items | Values | Choice | None


@dataclasses.dataclass(frozen=True, slots=True)
class Pick:
    """A key the server chooses: the first of ``keys`` present in the stored object at ``path``, else ``fallback``."""

    keys: tuple[str, ...]
    fallback: str
    path: tuple[str | Pick, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class Entries:
    """An object ``MERGE`` builds with ``object::from_entries()``: it holds a :class:`Pick` at some depth."""

    items: tuple[tuple[str | Pick, Any], ...]


_MISSING: Final = object()
_KEYS: dict[type, SubKeys] = {}
_RELEVANT: dict[type, bool] = {}
_FIELD_SHAPES: dict[type, dict[str, Shape]] = {}
_MODEL_SHAPES: dict[type, dict[str, Shape]] = {}
_SEQUENCES: Final = frozenset(
    {
        list,
        tuple,
        set,
        frozenset,
        collections.abc.Sequence,
        collections.abc.MutableSequence,
        collections.abc.Set,
        collections.abc.MutableSet,
        collections.abc.Collection,
        collections.abc.Iterable,
    }
)
_MAPPINGS: Final = frozenset({dict, collections.abc.Mapping, collections.abc.MutableMapping})


# --- keys ------------------------------------------------------------------------------------------------------------


def _alias_keys(alias: object) -> tuple[tuple[str, ...], bool]:
    """The plain keys a validation alias reads, and whether it also reads a path of several steps."""
    if isinstance(alias, str):
        return (alias,), False
    choices: list[object] = list(alias.choices) if isinstance(alias, AliasChoices) else [alias]
    keys: list[str] = []
    path = False
    for choice in choices:
        if isinstance(choice, str):
            keys.append(choice)
        elif isinstance(choice, AliasPath) and len(choice.path) == 1 and isinstance(choice.path[0], str):
            keys.append(choice.path[0])
        else:
            path = True
    return tuple(keys), path


def keys_of(model: type[BaseModel]) -> SubKeys:
    """The :class:`SubKeys` of *model*, cached once Pydantic has completed the class.

    Read from ``model_fields`` and ``model_config``: ``validate_by_alias`` (default ``True``) and ``validate_by_name``
    or ``populate_by_name`` for the read keys, ``serialize_by_alias`` for the dumped key — the sub-model's own config,
    which is what ``model_dump()`` of the model holding it follows.
    """
    cached = _KEYS.get(model)
    if cached is not None:
        return cached
    config = model.model_config
    by_alias = config.get("validate_by_alias", True) is not False
    by_name = bool(config.get("validate_by_name") or config.get("populate_by_name"))
    by_alias_out = bool(config.get("serialize_by_alias"))
    fields: dict[str, SubField] = {}
    dumped: dict[str, str] = {}
    claims: dict[str, set[str]] = {}
    plain = True
    for name, info in model.model_fields.items():
        reads: list[str] = []
        alias = info.validation_alias if info.validation_alias is not None else info.alias
        if by_alias and alias is not None:
            reads.extend(_alias_keys(alias)[0])
        if by_name or not by_alias or alias is None:
            reads.append(name)
        reads = list(dict.fromkeys(reads))
        written = (info.serialization_alias or info.alias or name) if by_alias_out else name
        key = written if written in reads else (reads[0] if reads else None)
        fields[name] = SubField(name, tuple(reads), key, info.annotation)
        dumped[written] = name
        for spelling in (*reads, name, written):
            claims.setdefault(spelling, set()).add(name)
        plain = plain and reads == [name] and written == name
    spellings = {spelling: next(iter(names)) if len(names) == 1 else None for spelling, names in claims.items()}
    keys = SubKeys(fields, dumped, spellings, plain)
    if model.__pydantic_complete__:
        _KEYS[model] = keys
    return keys


# --- shapes ----------------------------------------------------------------------------------------------------------


def _alias_types() -> tuple[type, ...]:
    """The ``TypeAliasType`` classes this interpreter has: ``typing``'s (Python 3.12+) and that of
    ``typing_extensions`` when something loaded it (never imported here)."""
    found: list[type] = []
    for module in (typing, sys.modules.get("typing_extensions")):
        alias = getattr(module, "TypeAliasType", None)
        if isinstance(alias, type) and alias not in found:
            found.append(alias)
    return tuple(found)


def _unwrapped(annotation: Any) -> Any:
    """*annotation* without its ``Annotated`` layers and ``type`` aliases."""
    aliases = _alias_types()
    while True:
        if get_origin(annotation) is typing.Annotated:
            annotation = annotation.__origin__
        elif aliases and isinstance(annotation, aliases):
            annotation = typing.cast(Any, annotation).__value__
        else:
            return annotation


def _shape(annotation: Any, active: frozenset[type]) -> Shape:
    """The shape of *annotation*: ``None`` when no sub-model below it has keys the ORM rewrites."""
    annotation = _unwrapped(annotation)
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        members = [shape for arg in get_args(annotation) if (shape := _shape(arg, active)) is not None]
        if not members:
            return None
        return members[0] if len(members) == 1 else Choice(tuple(members))
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return Model(annotation) if _relevant(annotation, active) else None
    args = get_args(annotation)
    if origin in _SEQUENCES and args:
        if origin is tuple and not (len(args) == 2 and args[1] is Ellipsis) and len(set(map(id, args))) != 1:
            return None  # a fixed tuple of different types: no item shape
        item = _shape(args[0], active)
        return None if item is None else Items(item)
    if origin in _MAPPINGS and len(args) == 2:
        value = _shape(args[1], active)
        return None if value is None else Values(value)
    return None


def _relevant(model: type[BaseModel], active: frozenset[type]) -> bool:
    """Whether *model*, or a sub-model below it, has keys the ORM rewrites. Cached for a top-level answer only."""
    if model in active:
        return False  # a cycle adds nothing: the classes on it are answered by the call that started it
    cached = _RELEVANT.get(model)
    if cached is not None:
        return cached
    keys = keys_of(model)
    inner = active | {model}
    relevant = not keys.plain or any(_shape(field.annotation, inner) is not None for field in keys.fields.values())
    if not active and model.__pydantic_complete__:
        _RELEVANT[model] = relevant
    return relevant


def field_shape(model: type[BaseModel], name: str) -> Shape:
    """The shape of field *name* of *model*, cached once Pydantic has completed the class; ``None`` for no field."""
    shapes = _FIELD_SHAPES.get(model)
    if shapes is not None and name in shapes:
        return shapes[name]
    info = model.model_fields.get(name)
    shape = None if info is None else _shape(info.annotation, frozenset())
    if model.__pydantic_complete__:
        _FIELD_SHAPES.setdefault(model, {})[name] = shape
    return shape


def shapes_of(model: type[BaseModel]) -> Mapping[str, Shape]:
    """The fields of *model* that have a shape, with it: ``{}`` for most models. Cached once the class is complete;
    never mutate the result."""
    cached = _MODEL_SHAPES.get(model)
    if cached is not None:
        return cached
    shapes: dict[str, Shape] = {name: shape for name in model.model_fields if (shape := field_shape(model, name)) is not None}
    if model.__pydantic_complete__:
        _MODEL_SHAPES[model] = shapes
    return shapes


def holds_model(value: Any) -> bool:
    """Whether *value* is or holds a Pydantic model instance, which the SDK cannot send (``list``, ``tuple``, set and
    ``dict`` values are looked into)."""
    if isinstance(value, BaseModel):
        return True
    if isinstance(value, dict):
        return any(holds_model(item) for item in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(holds_model(item) for item in value)
    return False


# --- walkers ---------------------------------------------------------------------------------------------------------


def _chosen(choice: Choice, original: Any) -> Shape:
    """The member of *choice* that *original* takes: the first model class it is an instance of, a list for a list…;
    for a ``dict`` the caller wrote, the only member that takes a ``dict``, else ``None`` (left as given)."""
    if isinstance(original, BaseModel):
        return next((m for m in choice.members if isinstance(m, Model) and isinstance(original, m.model)), None)
    if isinstance(original, (list, tuple, set, frozenset)):
        return next((m for m in choice.members if isinstance(m, Items)), None)
    if isinstance(original, Mapping):
        takers = [m for m in choice.members if isinstance(m, (Model, Values))]
        return takers[0] if len(takers) == 1 else None
    return None


def _sub_original(original: Any, key: Any, name: str) -> Any:
    """The part of *original* (an instance or the caller's ``dict``) under *key*, field *name*; ``None`` if unknown."""
    if isinstance(original, BaseModel):
        return getattr(original, name, None)
    if isinstance(original, Mapping):
        return original.get(key)
    return None


def _field(model: type[BaseModel], keys: SubKeys, key: Any, original: Any) -> SubField | None:
    """The field *key* names, ``None`` for a key that names none. A dump (*original* an instance) is looked up by
    dumped key, a ``dict`` of the caller's by any spelling; a key two fields claim raises."""
    lookup = keys.dumped if isinstance(original, BaseModel) else keys.spellings
    name = lookup.get(key, _MISSING)
    if name is _MISSING:
        return None
    if name is None:
        claimed = sorted(f.name for f in keys.fields.values() if key in (*f.reads, f.name))
        raise ValueError(
            f"{model.__name__}: the key {key!r} names more than one field ({', '.join(claimed)}): "
            "a read could take it into either, so the ORM cannot tell where to write it. Give those fields distinct aliases."
        )
    field = keys.fields[str(name)]
    if field.key is None:
        raise ValueError(
            f"{model.__name__}.{field.name} is read only through an AliasPath of several steps: the ORM has no key "
            "a read takes to write it under. Give it a validation alias that names a key, or validate it by name."
        )
    return field


def _twice(model: type[BaseModel], field: SubField) -> ValueError:
    return ValueError(
        f"{model.__name__}.{field.name} is named twice in the same object (by two of its spellings: "
        f"{', '.join(dict.fromkeys((*field.reads, field.name)))}); pass it once."
    )


def rekeyed(shape: Shape, original: Any, value: Any, caller: bool = True) -> Any:
    """*value* with every sub-field of the sub-models *shape* describes under its key; *value* itself when no key
    changes (copy-on-write, at every depth).

    *value* replaces what is stored: the dump of *original* (a model instance, or a value holding some), or the
    caller's own value (then *original* is *value*). A key that names no field (an extra or a computed field) is kept
    as given. ``ValueError`` for a field named twice, a key two fields claim, or a field no plain key reads.

    ``caller=False`` (``save()``): only the dump of a model instance is rewritten; a ``dict`` an instance holds where
    a sub-model is declared (a row applied as received after a write, see ``_apply_record()``) is written as it is,
    as before #218.
    """
    if shape is None:
        return value
    if isinstance(shape, Choice):
        chosen = _chosen(shape, original)
        return value if chosen is None else rekeyed(chosen, original, value, caller)
    if isinstance(shape, Items):
        if not isinstance(value, (list, tuple)):
            return value
        originals = original if isinstance(original, (list, tuple)) and len(original) == len(value) else value
        items = [rekeyed(shape.item, sub, item, caller) for sub, item in zip(originals, value, strict=True)]
        if all(new is old for new, old in zip(items, value, strict=True)):
            return value
        return tuple(items) if isinstance(value, tuple) else items
    if isinstance(shape, Values):
        if not isinstance(value, dict):
            return value
        out = {key: rekeyed(shape.value, _sub_original(original, key, ""), item, caller) for key, item in value.items()}
        return value if all(out[key] is item for key, item in value.items()) else out
    if not isinstance(value, dict) or (not caller and not isinstance(original, BaseModel)):
        return value
    keys = keys_of(shape.model)
    out = {}
    written: set[str] = set()
    changed = False
    for key, item in value.items():
        field = _field(shape.model, keys, key, original)
        if field is None:
            new_key, new = key, item
        else:
            if field.name in written:
                raise _twice(shape.model, field)
            written.add(field.name)
            new_key = typing.cast(str, field.key)
            sub_shape = field_shape(shape.model, field.name)
            new = rekeyed(sub_shape, _sub_original(original, key, field.name), item, caller)
        out[new_key] = new
        changed = changed or new_key != key or new is not item
    return out if changed else value


def merged(shape: Shape, original: Any, value: Any, path: tuple[str | Pick, ...], picks: int = 0) -> Any:
    """*value* as ``MERGE`` must send it into the object stored at *path* (from ``$this``) — plain, or an
    :class:`Entries` holding a :class:`Pick`.

    The rules are in the module docstring. A list or a set replaces what is stored (``MERGE`` replaces an array):
    :func:`rekeyed`. A ``dict`` whose values are sub-models has each entry merged into the stored entry of that key.
    ``ValueError`` as :func:`rekeyed`, for a read key two fields claim (removing it could remove another field's
    value), and for an object more than :data:`MAX_PICKS` picked keys deep.
    """
    if shape is None:
        return value
    if isinstance(shape, Choice):
        chosen = _chosen(shape, original)
        return value if chosen is None else merged(chosen, original, value, path, picks)
    if isinstance(shape, Items) or not isinstance(value, dict):
        return rekeyed(shape, original, value)
    items: list[tuple[str | Pick, Any]] = []
    if isinstance(shape, Values):
        for key, item in value.items():
            sub = _sub_original(original, key, "")
            if isinstance(item, dict):
                items.append((key, merged(shape.value, sub, item, (*path, key), picks)))
            else:
                items.append((key, rekeyed(shape.value, sub, item)))
        return _collapsed(items, value)
    model = shape.model
    keys = keys_of(model)
    written: set[str] = set()
    for key, item in value.items():
        field = _field(model, keys, key, original)
        if field is None:
            items.append((key, item))
            continue
        if field.name in written:
            raise _twice(model, field)
        written.add(field.name)
        shared = [read for read in field.reads if keys.spellings.get(read) is None]
        if shared:
            raise ValueError(
                f"{model.__name__}.{field.name}: its key {shared[0]!r} also names another field, so merge() cannot "
                "tell which stored value a read takes. Give those fields distinct aliases."
            )
        field_key = typing.cast(str, field.key)
        sub_shape = field_shape(model, field.name)
        sub = _sub_original(original, key, field.name)
        if item is None:
            items.extend((read, None) for read in dict.fromkeys((*field.reads, field_key)))
        elif isinstance(item, dict):
            if len(field.reads) > 1:
                if picks >= MAX_PICKS:
                    raise ValueError(
                        f"{model.__name__}.{field.name}: merge() resolves the stored spelling of at most {MAX_PICKS} "
                        "nested sub-models that a read takes under several keys. Merge a shallower dict, or write the "
                        "whole value with update() or bulk_update()."
                    )
                pick = Pick(field.reads, field_key, path)
                items.append((pick, merged(sub_shape, sub, item, (*path, pick), picks + 1)))
            else:
                items.append((field_key, merged(sub_shape, sub, item, (*path, field_key), picks)))
        else:
            items.append((field_key, rekeyed(sub_shape, sub, item)))
            for read in field.reads:
                if read == field_key:
                    break
                items.append((read, None))
    return _collapsed(items, value)


def _collapsed(items: list[tuple[str | Pick, Any]], value: dict[Any, Any]) -> Any:
    """The object *items* build: *value* itself when they are its own items, a plain ``dict`` when no :class:`Pick`
    is among them, else :class:`Entries`."""
    if not any(isinstance(key, Pick) or isinstance(item, Entries) for key, item in items):
        if len(items) == len(value) and all(key in value and value[key] is item for key, item in items if isinstance(key, str)):
            return value
        return dict(items)
    return Entries(tuple(items))


def content(data: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """The ``MERGE`` expression of *data* (column-keyed, values from :func:`merged`) and its variables.

    ``object::from_entries()`` at every level that holds a :class:`Pick`, each key and value bound (``$m0``,
    ``$m1``…): nothing of the data is written into the statement. A :class:`Pick` is
    ``IF <stored>[$a] != NONE THEN $a ELSE IF <stored>[$b] != NONE THEN $b ELSE <fallback> END``, ``<stored>`` its
    path in ``$this``, the record before the write (``NULL`` counts as present, as for a read). Measured on SurrealDB
    2.7.0, 3.3.5 and 4.0.1.
    """
    variables: dict[str, Any] = {}

    def bind(value: Any) -> str:
        name = f"m{len(variables)}"
        variables[name] = value
        return f"${name}"

    def key_text(key: str | Pick) -> str:
        return bind(key) if isinstance(key, str) else pick_text(key)

    def pick_text(pick: Pick) -> str:
        stored = "$this" + "".join(f"[{key_text(segment)}]" for segment in pick.path)
        branches = []
        for read in pick.keys:
            bound = bind(read)
            branches.append(f"IF {stored}[{bound}] != NONE THEN {bound}")
        return " ELSE ".join(branches) + f" ELSE {bind(pick.fallback)} END"

    def value_text(value: Any) -> str:
        if isinstance(value, Entries):
            return entries_text(value.items)
        return bind(value)

    def entries_text(items: tuple[tuple[str | Pick, Any], ...]) -> str:
        return "object::from_entries([" + ", ".join(f"[{key_text(k)}, {value_text(v)}]" for k, v in items) + "])"

    return entries_text(tuple(data.items())), variables
