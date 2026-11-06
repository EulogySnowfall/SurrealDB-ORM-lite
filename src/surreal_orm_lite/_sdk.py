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

#235 adds what the read hook (``decoding.py``) needs: the three async connection classes, root first,
then their submodules. Each is ``None`` when the installed SDK has no such symbol: the hook is then
not installed.

v0.25.0 adds the SDK's URL parser (``SdkUrl``, ``SdkUrlScheme``), so the ORM classifies a URL exactly as
``AsyncSurreal()`` routes it. The SDK's package root imports both but does not list them in its
``__all__`` (2.0.0 and the 3.0 betas), so type checkers see the static import from
``surrealdb.connections.url``, the module that defines them; at run time they are looked up root first,
then that module.

v0.27.0 adds ``ThrownError`` (a top-level export of SDK 2.0.0): SurrealDB 3.1 reports a table that does not
exist, read by a ``LET`` inside a block, as a ``ThrownError`` instead of a ``NotFoundError``.

#279 adds ``TAG_DATETIME_COMPACT`` (a top-level export of SDK 2.0.0, like ``TAG_DURATION_COMPACT``): the read
hook reads a datetime outside years 1-9999, which the SDK's decoder refuses.

v0.50.0 (SDK 3.0) moves to the official SDK 3.0 (``surrealdb[pydantic]>=3.0.0,<4.0.0``). Its package root no longer
exports the CBOR tag numbers: they are read from ``surrealdb.data.types.constants``, the module that defines them (the
compact datetime and duration tags, the decimal string and the seven geometry tags). New root symbols: ``Null`` and
``NullType`` (SurrealDB's ``NULL``, distinct from ``None``, which is ``NONE``), ``SurrealSet``, ``PreciseDatetime``,
``Table`` (the CRUD target of a whole table), ``StringRecordId``, ``Features`` (the server-version gates) and the
transport and SDK-side errors. ``Null`` is also the plain CBOR null (``0xf6``), SurrealDB's open range bound: it
replaces the ORM's former ``CBOR_NULL`` constant. The read hook takes the SDK's CBOR decoder class (``SdkDecoder``)
and its tag readers (``SDK_TAG_READERS``) from ``surrealdb.data._decoder``; both are ``None`` when a later SDK moves
them, and the hook is then not installed. The embedded engine is an optional extra (``surreal-orm-lite[embedded]``):
``SdkAsyncEmbeddedConnection`` is ``None`` without it, and importing the ORM never needs it.
"""

import importlib
from typing import TYPE_CHECKING, Any

from surrealdb import (  # noqa: F401
    AlreadyExistsError,
    AsyncSurreal,
    ConnectionUnavailableError,
    Features,
    NotFoundError,
    Null,
    NullType,
    PreciseDatetime,
    RecordID,
    ServerError,
    StringRecordId,
    SurrealError,
    SurrealSet,
    Table,
    ThrownError,
    TransportError,
    TransportTimeoutError,
    UnexpectedResponseError,
    UnsupportedEngineError,
    UnsupportedFeatureError,
    UnsupportedVersionError,
)
from surrealdb import Duration as SdkDuration
from surrealdb import Geometry as SdkGeometry
from surrealdb import Range as SdkRange
from surrealdb.cbor import CBORSimpleValue, CBORTag, undefined
from surrealdb.data.types.constants import (
    TAG_DATETIME_COMPACT,
    TAG_DECIMAL_STRING,
    TAG_DURATION_COMPACT,
    TAG_GEOMETRY_COLLECTION,
    TAG_GEOMETRY_LINE,
    TAG_GEOMETRY_MULTI_LINE,
    TAG_GEOMETRY_MULTI_POINT,
    TAG_GEOMETRY_MULTI_POLYGON,
    TAG_GEOMETRY_POINT,
    TAG_GEOMETRY_POLYGON,
)


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
    from surrealdb.data._decoder import _TAGS as SDK_TAG_READERS
    from surrealdb.data._decoder import Decoder as SdkDecoder
    from surrealdb.data.cbor import decode as cbor_decode
    from surrealdb.data.cbor import encode as cbor_encode
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
    # The CBOR decoder class and its tag readers (``{tag: reader}``), from the private module that defines them, and
    # the decoding methods of these three classes: ``tests/test_decoding.py::TestSdkLayout`` pins the layout the read
    # hook (``decoding.py``) relies on.
    SdkDecoder = _first_of("Decoder", "surrealdb.data._decoder", required=False)
    SDK_TAG_READERS = _first_of("_TAGS", "surrealdb.data._decoder", required=False)
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
    # Subqueries (v0.27.0): SurrealDB 3.1's form of the missing-table error inside a block
    "ThrownError",
    # SDK 3.0 (v0.50.0)
    "Null",
    "NullType",
    "SurrealSet",
    "PreciseDatetime",
    "Table",
    "StringRecordId",
    "Features",
    "UnsupportedFeatureError",
    "UnsupportedVersionError",
    "UnexpectedResponseError",
    "ConnectionUnavailableError",
    "TransportError",
    "TransportTimeoutError",
    "UnsupportedEngineError",
    "SdkDecoder",
    "SDK_TAG_READERS",
    "TAG_GEOMETRY_POINT",
    "TAG_GEOMETRY_LINE",
    "TAG_GEOMETRY_POLYGON",
    "TAG_GEOMETRY_MULTI_POINT",
    "TAG_GEOMETRY_MULTI_LINE",
    "TAG_GEOMETRY_MULTI_POLYGON",
    "TAG_GEOMETRY_COLLECTION",
    # Rich field types (v0.23.0)
    "SdkDuration",
    "SdkRange",
    "TAG_DURATION_COMPACT",
    "TAG_DECIMAL_STRING",
    "CBORTag",
    "CBORSimpleValue",
    "undefined",
    "Bound",
    "BoundIncluded",
    "BoundExcluded",
    "cbor_encode",
    "cbor_decode",
    "SdkInvalidDurationError",
    # Read hook (#235); a datetime outside years 1-9999 (#279)
    "TAG_DATETIME_COMPACT",
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
