"""Single import surface for the external `surrealdb` SDK.

Centralizing SDK imports here keeps the rest of the ORM decoupled from the
SDK's module layout and makes a future SDK upgrade a one-file change.

v0.23.0 adds the symbols the rich field types need. The SDK's own ``Duration``, ``Range`` and
``Datetime`` are aliased ``Sdk…`` so they never clash with the ORM's ``Duration``/``Range``. SDK
2.0.0 keeps the range bounds, the CBOR ``encode``/``decode`` pair and ``InvalidDurationError``'s
definition in submodules while its docs already show some of them at the top level, so those are
looked up **root first, then the submodule** (``_first_of``): a 2.x SDK release that moves them
must not break ``import surreal_orm_lite``. Type checkers see the static 2.0.0 imports instead of
``Any``.
"""

import importlib
from typing import TYPE_CHECKING, Any

from surrealdb import (  # noqa: F401
    TAG_DECIMAL_STRING,
    TAG_DURATION_COMPACT,
    AlreadyExistsError,
    AsyncSurreal,
    NotFoundError,
    RecordID,
    ServerError,
    SurrealError,
)
from surrealdb import Datetime as SdkDatetime
from surrealdb import Duration as SdkDuration
from surrealdb import Range as SdkRange
from surrealdb.cbor import CBORTag, undefined


def _first_of(attr: str, *modules: str, required: bool = True) -> Any:
    """Return ``attr`` from the first of ``modules`` that defines it.

    The modules are imported in the order given (the SDK's top level first, then the submodule
    that holds the name in SDK 2.0.0); one that cannot be imported is skipped. When none defines
    ``attr``: ``ImportError`` naming every module tried, or ``None`` when ``required`` is false.
    """
    for name in modules:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        if hasattr(module, attr):
            return getattr(module, attr)
    if required:
        raise ImportError(f"cannot import {attr!r} from any of: {', '.join(modules)}")
    return None


if TYPE_CHECKING:
    from surrealdb.data.cbor import decode as cbor_decode
    from surrealdb.data.cbor import encode as cbor_encode
    from surrealdb.data.types.geometry import Geometry as SdkGeometry
    from surrealdb.data.types.geometry import GeometryCollection as SdkGeometryCollection
    from surrealdb.data.types.range import Bound, BoundExcluded, BoundIncluded
    from surrealdb.errors import InvalidDurationError as SdkInvalidDurationError
else:
    Bound = _first_of("Bound", "surrealdb", "surrealdb.data.types.range")
    BoundIncluded = _first_of("BoundIncluded", "surrealdb", "surrealdb.data.types.range")
    BoundExcluded = _first_of("BoundExcluded", "surrealdb", "surrealdb.data.types.range")
    # Only the local zero-duration probe uses the codec: ``None`` makes it answer False.
    cbor_encode = _first_of("encode", "surrealdb", "surrealdb.data.cbor", required=False)
    cbor_decode = _first_of("decode", "surrealdb", "surrealdb.data.cbor", required=False)
    # ``None`` when the installed SDK has no such class: the ORM's class then derives from
    # ``ValueError`` alone (``exceptions.py``).
    SdkInvalidDurationError = _first_of("InvalidDurationError", "surrealdb", "surrealdb.errors", required=False)
    # The SDK's geometries, which ``wire.restore_native`` puts back at an untyped position: the base
    # class of all but the collection. ``None`` when the installed SDK has no such class.
    SdkGeometry = _first_of("Geometry", "surrealdb", "surrealdb.data.types.geometry", required=False)
    SdkGeometryCollection = _first_of("GeometryCollection", "surrealdb", "surrealdb.data.types.geometry", required=False)

__all__ = [
    "AsyncSurreal",
    "RecordID",
    "SurrealError",
    "ServerError",
    "AlreadyExistsError",
    "NotFoundError",
    # Rich field types (v0.23.0)
    "SdkDuration",
    "SdkRange",
    "SdkDatetime",
    "TAG_DURATION_COMPACT",
    "TAG_DECIMAL_STRING",
    "CBORTag",
    "undefined",
    "Bound",
    "BoundIncluded",
    "BoundExcluded",
    "cbor_encode",
    "cbor_decode",
    "SdkInvalidDurationError",
    "SdkGeometry",
    "SdkGeometryCollection",
]
