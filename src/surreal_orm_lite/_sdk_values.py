"""Internal (#222): keep the SDK values that ``model_dump()`` flattens at untyped positions.

Not part of the public API — nothing here is exported by ``surreal_orm_lite``.

A write payload is ``model_dump()`` (``BaseSurrealModel._write_payload``). Pydantic serialises a dataclass it meets at
a position with no declared type — an ``Any`` field, the items of ``dict[str, Any]`` or ``list[Any]``, a model extra —
to a dict of its fields, and the SDK's ``Duration``, ``Range`` and seven geometry classes are dataclasses: a row read
with such values and written back stored ``{"elapsed": …}``, ``{"begin": …, "end": …}`` or
``{"longitude": …, "latitude": …}`` in their place, without an error.

:func:`kept` walks the instance's **values** next to what ``model_dump()`` made of them, never the annotations, and
puts the SDK value back where Pydantic's own inference flattened it: only where the dumped value is exactly what
``TypeAdapter(Any)`` makes of that SDK value. So a serializer of the user's that returns another dict wins, and a
declared rich field (the ORM's ``Duration``, ``Range`` and geometries, whose serializers return the ORM value, never a
dict) is untouched. Containers are followed as ``model_dump()`` writes them: a model by field name (or alias) and
extra, a dataclass by field name, a mapping by key, a list or tuple item by item (same length only). What a serializer
of the user's wrote is left as written: a model with a ``model_serializer`` of its own (its keys need not name its
fields), and a field with a ``field_serializer`` or an ``Annotated`` ``PlainSerializer``/``WrapSerializer``, even one
that returns the flattened dict.

Copy-on-write, like the encoding boundary (``encoding.py``): a ``dict``, ``list`` or ``tuple`` is rebuilt only when
something under it changed, so the payload of a model without SDK values is the very object ``model_dump()`` returned
and goes out byte for byte as before. No depth limit: a value nested beyond Python's recursion limit raises
``RecursionError``, as ``encoding.encode()`` does right after.
"""

from __future__ import annotations

import dataclasses
import itertools
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any, Final

from pydantic import BaseModel, PlainSerializer, TypeAdapter, WrapSerializer

from . import _sdk

__all__ = ["kept"]

#: The SDK classes Pydantic flattens into a dict at an untyped position (all dataclasses), subclasses included.
_FLATTENED: Final = (
    _sdk.SdkDuration,
    _sdk.SdkRange,
    _sdk.SdkGeometryPoint,
    _sdk.SdkGeometryLine,
    _sdk.SdkGeometryPolygon,
    _sdk.SdkGeometryMultiPoint,
    _sdk.SdkGeometryMultiLine,
    _sdk.SdkGeometryMultiPolygon,
    _sdk.SdkGeometryCollection,
)

#: Exact types that hold no other value: what ``model_dump()`` made of them is returned at once.
_LEAVES: Final = frozenset({type(None), str, bytes, int, float, bool, Decimal})

#: What ``model_dump()`` makes of a value at a position with no declared type.
_INFERRED: Final[TypeAdapter[Any]] = TypeAdapter(Any)

_MISSING: Final = object()


def kept(original: Any, dumped: Any) -> Any:
    """*dumped* (what ``model_dump()`` made of *original*) with each SDK value Pydantic flattened put back.

    *dumped* itself when nothing was flattened; otherwise a copy of the containers on the way to each value put back.
    """
    if type(original) in _LEAVES:
        return dumped
    if isinstance(original, _FLATTENED):
        if type(dumped) is dict and dumped == _INFERRED.dump_python(original):
            return original
        return dumped
    if type(dumped) is dict:
        if isinstance(original, BaseModel):
            if type(original).__pydantic_decorators__.model_serializers:
                return dumped
            return _dict_kept(dumped, lambda key: _model_value(original, key))
        if dataclasses.is_dataclass(original) and not isinstance(original, type):
            return _dict_kept(dumped, lambda key: getattr(original, key, _MISSING))
        if isinstance(original, Mapping):
            return _dict_kept(dumped, lambda key: original.get(key, _MISSING))
        return dumped
    if (
        isinstance(original, (list, tuple))
        and type(dumped) in (list, tuple)
        and len(original) == len(dumped)  # same length: item i of the dump is item i of the value
    ):
        return _sequence_kept(original, dumped)
    return dumped


def _model_value(model: BaseModel, key: Any) -> Any:
    """The value of *model* that ``model_dump()`` wrote under *key*: a field by name, an extra, a field by alias.

    ``_MISSING`` for a key that names none of them, and for a field a serializer of the user's writes.
    """
    fields = type(model).model_fields
    name: Any = key if key in fields else _MISSING
    if name is _MISSING:
        extra = model.__pydantic_extra__
        if extra is not None and key in extra:
            return extra[key]
        name = next((field for field, info in fields.items() if key in (info.serialization_alias, info.alias)), _MISSING)
    if name is _MISSING or _serialized_by_user(type(model), name):
        return _MISSING
    return getattr(model, name, _MISSING)


def _serialized_by_user(model: type[BaseModel], name: str) -> bool:
    """Whether field *name* of *model* has a serializer of its own: a ``field_serializer`` or an ``Annotated`` one."""
    for decorator in model.__pydantic_decorators__.field_serializers.values():
        if name in decorator.info.fields or "*" in decorator.info.fields:
            return True
    return any(isinstance(item, (PlainSerializer, WrapSerializer)) for item in model.model_fields[name].metadata)


def _dict_kept(dumped: dict[Any, Any], source: Callable[[Any], Any]) -> dict[Any, Any]:
    """*dumped* with each item kept from the value *source* gives for its key; a new ``dict`` only when one changed."""
    out: dict[Any, Any] | None = None
    for index, (key, item) in enumerate(dumped.items()):
        original = source(key)
        value = item if original is _MISSING else kept(original, item)
        if out is None and value is not item:
            out = dict(itertools.islice(dumped.items(), index))  # the unchanged items before this one
        if out is not None:
            out[key] = value
    return dumped if out is None else out


def _sequence_kept(original: list[Any] | tuple[Any, ...], dumped: list[Any] | tuple[Any, ...]) -> Any:
    """*dumped* with each item kept from the item of *original* at its index; a new ``list`` or ``tuple`` only when
    one changed."""
    out: list[Any] | None = None
    for index, (source, item) in enumerate(zip(original, dumped, strict=True)):
        value = kept(source, item)
        if out is None and value is not item:
            out = list(dumped[:index])
        if out is not None:
            out.append(value)
    if out is None:
        return dumped
    return tuple(out) if isinstance(dumped, tuple) else out
