"""Single import surface for the external `surrealdb` SDK.

Centralizing SDK imports here keeps the rest of the ORM decoupled from the
SDK's module layout and makes a future SDK upgrade a one-file change.

v0.23.0 adds the symbols the rich field types need. The SDK's own ``Duration`` and ``Range`` are
aliased ``Sdk…`` so they never clash with the ORM's ``Duration``/``Range``. SDK 2.0.0 keeps the
range bounds and ``InvalidDurationError``'s definition in submodules while its docs already show
some of them at the top level, so those are looked up **root first, then the submodule**
(``_first_of``): a 2.x SDK release that moves them must not break ``import surreal_orm_lite``. The
CBOR codec (``encode``/``decode``) is read from ``surrealdb.data.cbor`` **only**: names that
generic are never taken from the package root, where a later release could export an unrelated
symbol. Type checkers see the static 2.0.0 imports instead of ``Any``.

v0.24.0 adds the geometry symbols, aliased ``Sdk…`` so they never clash with the ORM's own geometry
classes (``geo.py``). SDK 2.0.0 exports only ``Geometry`` and ``InvalidGeometryError`` at the top
level and keeps the seven concrete classes in ``surrealdb.data.types.geometry``; they are looked up
root first, then that submodule, so a release that exports them at the top level needs no change.

#235 adds what the read hook (``decoding.py``) needs: the SDK's CBOR ``tag_decoder``, read from
``surrealdb.data.cbor`` only like ``encode``/``decode``, and the three async connection classes,
root first, then their SDK 2.0.0 submodules. Each is ``None`` when the installed SDK has no such
symbol: the hook is then not installed.

v0.25.0 adds the SDK's URL parser (``SdkUrl``, ``SdkUrlScheme``), so the ORM classifies a URL exactly as
``AsyncSurreal()`` routes it. The SDK's package root imports both but does not list them in its
``__all__`` (2.0.0 and the 3.0 betas), so type checkers see the static import from
``surrealdb.connections.url``, the module that defines them; at run time they are looked up root first,
then that module.
"""

import importlib
from typing import TYPE_CHECKING, Any, Final

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
from surrealdb import Duration as SdkDuration
from surrealdb import Geometry as SdkGeometry
from surrealdb import Range as SdkRange
from surrealdb.cbor import CBORSimpleValue, CBORTag, undefined

# The plain CBOR null (``0xf6``): SurrealDB's open range bound (protocol tag 49). ``None`` would go
# out as tag 6, NONE. Never mutated by the ORM; recognise a CBOR null with
# ``isinstance(x, CBORSimpleValue) and x.value == 22``, never with ``==`` (it equals the int 22).
# SDK 3.0 (#205): this becomes the SDK's ``Null`` sentinel.
CBOR_NULL: Final = CBORSimpleValue(22)


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
    from surrealdb import AsyncEmbeddedSurrealConnection as SdkAsyncEmbeddedConnection
    from surrealdb import AsyncHttpSurrealConnection as SdkAsyncHttpConnection
    from surrealdb import AsyncWsSurrealConnection as SdkAsyncWsConnection
    from surrealdb.connections.url import Url as SdkUrl
    from surrealdb.connections.url import UrlScheme as SdkUrlScheme
    from surrealdb.data.cbor import decode as cbor_decode
    from surrealdb.data.cbor import encode as cbor_encode
    from surrealdb.data.cbor import tag_decoder as cbor_tag_decoder
    from surrealdb.data.types.geometry import GeometryCollection as SdkGeometryCollection
    from surrealdb.data.types.geometry import GeometryLine as SdkGeometryLine
    from surrealdb.data.types.geometry import GeometryMultiLine as SdkGeometryMultiLine
    from surrealdb.data.types.geometry import GeometryMultiPoint as SdkGeometryMultiPoint
    from surrealdb.data.types.geometry import GeometryMultiPolygon as SdkGeometryMultiPolygon
    from surrealdb.data.types.geometry import GeometryPoint as SdkGeometryPoint
    from surrealdb.data.types.geometry import GeometryPolygon as SdkGeometryPolygon
    from surrealdb.data.types.range import Bound, BoundExcluded, BoundIncluded
    from surrealdb.errors import InvalidDurationError as SdkInvalidDurationError
    from surrealdb.errors import InvalidGeometryError as SdkInvalidGeometryError
else:
    Bound = _first_of("Bound", "surrealdb", "surrealdb.data.types.range")
    BoundIncluded = _first_of("BoundIncluded", "surrealdb", "surrealdb.data.types.range")
    BoundExcluded = _first_of("BoundExcluded", "surrealdb", "surrealdb.data.types.range")
    # ``surrealdb.data.cbor`` only, never the package root. ``None`` when the installed SDK has no
    # such codec: the local zero-duration probe then answers False.
    cbor_encode = _first_of("encode", "surrealdb.data.cbor", required=False)
    cbor_decode = _first_of("decode", "surrealdb.data.cbor", required=False)
    # SDK 3.0 (#205): the read hook (``decoding.py``) wraps this ``tag_decoder`` and the decoding
    # method of these three classes; re-check both against the stable SDK's layout.
    cbor_tag_decoder = _first_of("tag_decoder", "surrealdb.data.cbor", required=False)
    SdkAsyncWsConnection = _first_of("AsyncWsSurrealConnection", "surrealdb", "surrealdb.connections.async_ws", required=False)
    SdkAsyncHttpConnection = _first_of(
        "AsyncHttpSurrealConnection", "surrealdb", "surrealdb.connections.async_http", required=False
    )
    SdkAsyncEmbeddedConnection = _first_of(
        "AsyncEmbeddedSurrealConnection", "surrealdb", "surrealdb.connections.async_embedded", required=False
    )
    # ``None`` when the installed SDK has no such class: the ORM's class then derives from
    # ``ValueError`` alone (``exceptions.py``).
    SdkInvalidDurationError = _first_of("InvalidDurationError", "surrealdb", "surrealdb.errors", required=False)
    # Geometry (v0.24.0): the seven concrete classes, root first, then the submodule of SDK 2.0.0.
    _GEOMETRY_MODULE = "surrealdb.data.types.geometry"
    SdkGeometryPoint = _first_of("GeometryPoint", "surrealdb", _GEOMETRY_MODULE)
    SdkGeometryLine = _first_of("GeometryLine", "surrealdb", _GEOMETRY_MODULE)
    SdkGeometryPolygon = _first_of("GeometryPolygon", "surrealdb", _GEOMETRY_MODULE)
    SdkGeometryMultiPoint = _first_of("GeometryMultiPoint", "surrealdb", _GEOMETRY_MODULE)
    SdkGeometryMultiLine = _first_of("GeometryMultiLine", "surrealdb", _GEOMETRY_MODULE)
    SdkGeometryMultiPolygon = _first_of("GeometryMultiPolygon", "surrealdb", _GEOMETRY_MODULE)
    SdkGeometryCollection = _first_of("GeometryCollection", "surrealdb", _GEOMETRY_MODULE)
    # ``None`` when the installed SDK has no such class: the ORM's class then derives from
    # ``ValueError`` alone (``exceptions.py``).
    SdkInvalidGeometryError = _first_of("InvalidGeometryError", "surrealdb", "surrealdb.errors", required=False)
    # URL parser (v0.25.0): root first, then the module that defines it (never in the root's ``__all__``).
    SdkUrl = _first_of("Url", "surrealdb", "surrealdb.connections.url")
    SdkUrlScheme = _first_of("UrlScheme", "surrealdb", "surrealdb.connections.url")

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
    "TAG_DURATION_COMPACT",
    "TAG_DECIMAL_STRING",
    "CBORTag",
    "CBORSimpleValue",
    "CBOR_NULL",
    "undefined",
    "Bound",
    "BoundIncluded",
    "BoundExcluded",
    "cbor_encode",
    "cbor_decode",
    "SdkInvalidDurationError",
    # Read hook (#235)
    "cbor_tag_decoder",
    "SdkAsyncWsConnection",
    "SdkAsyncHttpConnection",
    "SdkAsyncEmbeddedConnection",
    # Geometry (v0.24.0)
    "SdkGeometry",
    "SdkGeometryPoint",
    "SdkGeometryLine",
    "SdkGeometryPolygon",
    "SdkGeometryMultiPoint",
    "SdkGeometryMultiLine",
    "SdkGeometryMultiPolygon",
    "SdkGeometryCollection",
    "SdkInvalidGeometryError",
    # Embedded engine (v0.25.0)
    "SdkUrl",
    "SdkUrlScheme",
]
