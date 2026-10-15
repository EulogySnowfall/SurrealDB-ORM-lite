"""Geometry values (v0.24.0): SurrealDB's geometries as immutable ORM classes.

``GeometryPoint``, ``GeometryLine``, ``GeometryPolygon``, ``GeometryMultiPoint``,
``GeometryMultiLine``, ``GeometryMultiPolygon`` and ``GeometryCollection`` have the names,
constructor signatures, attributes and methods of the SDK's classes (``surrealdb.data``), plus
GeoJSON helpers (``to_geojson()``, ``Geometry.from_geojson()``, ``__geo_interface__``). They are the
ORM's own classes, not the SDK's, because Pydantic's ``model_dump()`` turns the SDK's dataclasses
into plain dicts, and the ORM writes what ``model_dump()`` returns: the encoding boundary
(``encoding.encode``) converts these values to the SDK's classes, which SurrealDB stores as native
geometries.

Values are immutable (``__slots__``; setting or deleting an attribute raises ``AttributeError``) and
hashable; an attribute holding several items is a ``tuple``. ``copy.copy()`` and ``copy.deepcopy()``
return the instance itself, like a tuple, and ``pickle`` rebuilds an equal value, so a geometry
works as a field default and through every Pydantic copy.

Equality: a value equals an ORM or SDK value of the same kind with equal coordinates
(``GeometryPoint(1, 2) == surrealdb.data.GeometryPoint(1.0, 2.0)`` is ``True``). The SDK's own
``__eq__`` answers ``False`` the other way round: keep the ORM value on the left.

``str()`` gives the value's SurrealQL text, in GeoJSON object form
(``{type: 'Point', coordinates: [-73.99, 40.75]}``), and never raises; ``repr()`` is the SDK's format.

``GeometryCollection`` is a ``Geometry`` (the SDK's is not) and nests at most 32 levels of
collections, a depth every supported SurrealDB line accepts. Refusals raise ``TypeError`` for a
wrong argument type and ``InvalidGeometryError`` (a ``ValueError``) for a wrong value.

As model fields: a class annotation (``entrance: GeometryPoint``, required unless ``X | None``) or
``GeoField["point"]`` / an alias (``location: PointField = None``: ``GeometryPoint | None``, default
``None``, as in the full SurrealDB-ORM). Both accept the ORM value, the SDK's, a GeoJSON object and,
for points, a ``(longitude, latitude)`` pair; ``model_dump()`` returns the ORM value and JSON mode
its GeoJSON object. Inside containers (``list[GeometryPoint]``) use the classes.
``is_geo_field()`` and ``get_geo_info()`` tell a geometry field's kinds.

In queries: ``QuerySet.nearby()`` keeps the records within a distance of a point, and
``GeoDistance(field, point)`` given to ``QuerySet.annotate()`` attaches each record's distance.
"""

from __future__ import annotations

import functools
import math
import numbers
import operator
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from types import UnionType
from typing import Annotated, Any, ClassVar, Self, TypeAlias, TypeVar, Union, cast, get_args, get_origin

from pydantic import Field, GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import PydanticSerializationUnexpectedValue, core_schema

from ._sdk import (
    TAG_DECIMAL_STRING,
    CBORTag,
    SdkGeometryCollection,
    SdkGeometryLine,
    SdkGeometryMultiLine,
    SdkGeometryMultiPoint,
    SdkGeometryMultiPolygon,
    SdkGeometryPoint,
    SdkGeometryPolygon,
)
from .exceptions import InvalidGeometryError

__all__ = [
    "Geometry",
    "GeometryPoint",
    "GeometryLine",
    "GeometryPolygon",
    "GeometryMultiPoint",
    "GeometryMultiLine",
    "GeometryMultiPolygon",
    "GeometryCollection",
    "GeoField",
    "GeometryField",
    "PointField",
    "LineStringField",
    "PolygonField",
    "MultiPointField",
    "MultiLineStringField",
    "MultiPolygonField",
    "GeometryCollectionField",
    "GeoDistance",
    "is_geo_field",
    "get_geo_info",
]

_G = TypeVar("_G", bound="Geometry")

#: The deepest nesting of collections an ORM ``GeometryCollection`` holds (spec D15).
_MAX_COLLECTION_LEVELS = 32

#: The seven SDK classes (subclasses included), for ``isinstance``.
_SDK_GEOMETRIES: tuple[type[Any], ...] = (
    SdkGeometryPoint,
    SdkGeometryLine,
    SdkGeometryPolygon,
    SdkGeometryMultiPoint,
    SdkGeometryMultiLine,
    SdkGeometryMultiPolygon,
    SdkGeometryCollection,
)


# --- argument checks -----------------------------------------------------------------------------


def _coordinate(value: Any, owner: str, name: str) -> float:
    """``value`` as a finite ``float``: any ``numbers.Real`` except ``bool``.

    ``TypeError`` for another type (a ``Decimal`` included: ``float()`` would not round it the way
    SurrealDB does); ``InvalidGeometryError`` for a non-finite value or one too large for a float.
    """
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        hint = (
            " Convert a Decimal with float(), or send an SDK geometry: SurrealDB converts its decimal coordinates itself."
            if isinstance(value, Decimal)
            else ""
        )
        raise TypeError(f"{owner} {name} must be a real number (int, float or Fraction), not {type(value).__name__}.{hint}")
    try:
        number = float(value)
    except OverflowError as exc:
        raise InvalidGeometryError(f"{owner} {name} is too large for a float") from exc
    if not math.isfinite(number):
        raise InvalidGeometryError(f"{owner} {name} must be a finite number, got {number!r}")
    return number


def _items(value: Any, owner: str, expected: str) -> tuple[Any, ...]:
    """The items of a coordinate array (any iterable but a string, bytes or a mapping), else ``TypeError``."""
    if isinstance(value, (str, bytes, bytearray, Mapping)) or not isinstance(value, Iterable):
        raise TypeError(f"{owner} takes {expected}, not {type(value).__name__}")
    return tuple(value)


def _component(value: Any, orm_cls: type[_G], owner: str, role: str, position: int) -> _G:
    """``value`` as an ORM ``orm_cls``: kept when it is one, converted (checked) from the SDK's class."""
    if isinstance(value, orm_cls):
        return value
    if isinstance(value, orm_cls._SDK_CLASS):
        return cast(_G, _from_sdk_checked(value, 1))
    raise TypeError(
        f"{owner} {role} {position} must be a {orm_cls.__name__} (the ORM's or the SDK's), not {type(value).__name__}"
    )


def _collection_member(value: Any, position: int) -> Geometry:
    """A ``GeometryCollection`` member: any ORM geometry, or any SDK geometry converted (checked)."""
    if isinstance(value, Geometry):
        return value
    if isinstance(value, _SDK_GEOMETRIES):
        return _from_sdk_checked(value, 1)
    raise TypeError(
        f"GeometryCollection member {position} must be a geometry (the ORM's or the SDK's), not {type(value).__name__}"
    )


def _too_deep(levels: int) -> InvalidGeometryError:
    return InvalidGeometryError(f"a GeometryCollection nests at most {_MAX_COLLECTION_LEVELS} levels, got {levels}")


def _check_ring(ring: GeometryLine, ring_type: str) -> None:
    """A polygon ring: at least 4 points, the first equal to the last (the SDK's checks and wording)."""
    points = ring.geometry_points
    if len(points) < 4:
        raise InvalidGeometryError(
            f"Invalid {ring_type} ring: must have at least 4 points (including closing point), got {len(points)}"
        )
    first, last = points[0], points[-1]
    if first != last:
        raise InvalidGeometryError(
            f"Invalid {ring_type} ring: first point {first.get_coordinates()} "
            f"must equal last point {last.get_coordinates()} to close the ring"
        )


# --- the classes ----------------------------------------------------------------------------------


class Geometry:
    """The abstract base of the seven ORM geometry classes (v0.24.0).

    ``isinstance(value, Geometry)`` is true for every kind, ``GeometryCollection`` included.
    ``Geometry.from_geojson(data)`` builds the kind named by the GeoJSON ``type``.
    """

    __slots__ = ()

    #: The GeoJSON ``type`` of the kind; the SDK class it converts to; the slots pickle saves.
    _GEOJSON_TYPE: ClassVar[str]
    _SDK_CLASS: ClassVar[type[Any]]
    _STATE: ClassVar[tuple[str, ...]]

    def __new__(cls, *args: Any, **kwargs: Any) -> Self:
        if cls is Geometry or cls is _Composite:
            raise TypeError(
                "Geometry is abstract: build a GeometryPoint, GeometryLine, GeometryPolygon, "
                "GeometryMultiPoint, GeometryMultiLine, GeometryMultiPolygon or GeometryCollection"
            )
        return super().__new__(cls)

    # Every concrete class overrides these (``Geometry`` itself cannot be instantiated).
    def get_coordinates(self) -> Any:  # pragma: no cover
        """The coordinates, in the SDK's shape (``(longitude, latitude)`` tuples, nested in lists)."""
        raise NotImplementedError

    def _geojson_coordinates(self) -> Any:  # pragma: no cover
        raise NotImplementedError

    def _to_sdk(self) -> Any:  # pragma: no cover
        raise NotImplementedError

    def _repr(self) -> str:  # pragma: no cover
        raise NotImplementedError

    def to_geojson(self) -> dict[str, Any]:
        """The GeoJSON geometry object: ``{"type": "Point", "coordinates": [lon, lat]}``.

        Positions are ``[longitude, latitude]`` lists; a ``GeometryCollection`` gives
        ``{"type": "GeometryCollection", "geometries": [...]}``. A new dict on each call.
        """
        return {"type": self._GEOJSON_TYPE, "coordinates": self._geojson_coordinates()}

    @property
    def __geo_interface__(self) -> dict[str, Any]:
        """The ``__geo_interface__`` protocol (shapely and other GIS libraries): ``to_geojson()``."""
        return self.to_geojson()

    @classmethod
    def from_geojson(cls, data: Mapping[str, Any]) -> Self:
        """The geometry a GeoJSON geometry object describes.

        On ``Geometry`` the kind follows ``data["type"]`` (``Point``, ``LineString``, ``Polygon``,
        ``MultiPoint``, ``MultiLineString``, ``MultiPolygon``, ``GeometryCollection``); on a subclass,
        ``data["type"]`` must be that subclass's. A position holds exactly 2 numbers
        (``[longitude, latitude]``); unknown keys (``bbox``, …) are ignored.

        ``InvalidGeometryError`` for anything else: not a mapping, a missing or unknown ``type``
        (a ``Feature`` included), a missing ``coordinates``/``geometries``, a malformed nesting, a
        non-number or non-finite coordinate, a ring the polygon rules refuse, or collections nested
        deeper than 32 levels.
        """
        expected: str | None = getattr(cls, "_GEOJSON_TYPE", None)
        if expected is not None and isinstance(data, Mapping) and data.get("type") != expected:
            raise InvalidGeometryError(
                f"{cls.__name__}.from_geojson() takes a GeoJSON {expected}, got type {data.get('type')!r}"
            )
        value = _from_geojson(data, 1)
        if not isinstance(value, cls):
            # A subclass of an ORM class: GeoJSON only builds the ORM's own classes.
            raise InvalidGeometryError(
                f"{cls.__name__}.from_geojson() builds a {type(value).__name__}, not a {cls.__name__}: "
                f"call {type(value).__name__}.from_geojson()"
            )
        return value

    # Immutable: the constructors assign through ``object.__setattr__``.
    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable: build a new value instead of setting {name!r}")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable: {name!r} cannot be deleted")

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Self:
        return self

    def __reduce__(self) -> tuple[Any, ...]:
        # No constructor runs on unpickling, so a value the serializer built unchecked pickles too.
        return (_restore, (type(self), tuple(getattr(self, name) for name in self._STATE)))

    def __eq__(self, other: object) -> bool:
        if _kind_of(self) is None or _kind_of(other) is None:
            return NotImplemented
        try:
            return bool(_key(self) == _key(other))
        except Exception:
            return False

    def __hash__(self) -> int:
        return hash(_key(self))

    def __repr__(self) -> str:
        try:
            return self._repr()
        except Exception:
            return object.__repr__(self)

    def __str__(self) -> str:
        """The value's SurrealQL text, in GeoJSON object form: ``{type: 'Point', coordinates: [1.0, 2.0]}``.

        Never raises: a value with no text (built unchecked by the serializer, holding a ``str`` or a
        non-finite coordinate…) gives its ``repr()``.
        """
        try:
            return _geometry_text(self)
        except Exception:
            return repr(self)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
        """A field annotated with the class takes that kind (``Geometry``: any kind); required unless ``X | None``.

        Accepted input: an ORM value of the kind (kept), the SDK's (converted, checked), its GeoJSON
        object, and for a point a ``(longitude, latitude)`` pair. Python mode dumps the ORM value,
        JSON mode its GeoJSON object.
        """
        return _field_schema(_class_kinds(cls))

    @classmethod
    def __get_pydantic_json_schema__(cls, schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        return _geojson_json_schema(_class_kinds(cls))


class GeometryPoint(Geometry):
    """A point: ``GeometryPoint(longitude, latitude)``.

    Coordinates are stored as ``float``: any real number but a ``bool`` (``int``, ``float``,
    ``Fraction``…), finite. ``TypeError`` for another type (``Decimal``, ``str``, ``None``, ``bool``);
    ``InvalidGeometryError`` for ``nan``, ``inf`` or a number too large for a float.
    """

    __slots__ = ("longitude", "latitude")
    __match_args__ = ("longitude", "latitude")
    _GEOJSON_TYPE = "Point"
    _SDK_CLASS = SdkGeometryPoint
    _STATE = ("longitude", "latitude")

    longitude: float
    latitude: float

    def __init__(self, longitude: float, latitude: float) -> None:
        object.__setattr__(self, "longitude", _coordinate(longitude, "GeometryPoint", "longitude"))
        object.__setattr__(self, "latitude", _coordinate(latitude, "GeometryPoint", "latitude"))

    def get_coordinates(self) -> tuple[float, float]:
        """``(longitude, latitude)``."""
        return self.longitude, self.latitude

    @staticmethod
    def parse_coordinates(coordinates: Sequence[float]) -> GeometryPoint:
        """A point from a ``(longitude, latitude)`` pair; ``TypeError`` unless it holds exactly 2 items."""
        items = _items(coordinates, "GeometryPoint.parse_coordinates()", "a (longitude, latitude) pair")
        if len(items) != 2:
            raise TypeError(f"GeometryPoint.parse_coordinates() takes a (longitude, latitude) pair, got {len(items)} items")
        return GeometryPoint(items[0], items[1])

    def _geojson_coordinates(self) -> list[float]:
        return [self.longitude, self.latitude]

    def _to_sdk(self) -> SdkGeometryPoint:
        sdk = object.__new__(SdkGeometryPoint)
        sdk.longitude = self.longitude
        sdk.latitude = self.latitude
        return sdk

    def _repr(self) -> str:
        return f"{type(self).__name__}(longitude={self.longitude}, latitude={self.latitude})"


class _Composite(Geometry):
    """The six kinds made of members (private): one tuple attribute named ``_ATTR``."""

    __slots__ = ()

    _ATTR: ClassVar[str]

    def _members(self) -> tuple[Any, ...]:
        members: tuple[Any, ...] = getattr(self, self._ATTR)
        return members

    def _geojson_coordinates(self) -> list[Any]:
        return [member._geojson_coordinates() for member in self._members()]

    def _to_sdk(self) -> Any:
        # No SDK constructor (P4): a value built unchecked converts as it is, and the encoding
        # boundary refuses what SurrealDB refuses. Members that are not ORM geometries stay as they are.
        sdk = object.__new__(self._SDK_CLASS)
        setattr(sdk, self._ATTR, [m._to_sdk() if isinstance(m, Geometry) else m for m in self._members()])
        return sdk

    def _repr(self) -> str:
        return f"{type(self).__name__}({', '.join(repr(member) for member in self._members())})"


class GeometryLine(_Composite):
    """A line through two or more points: ``GeometryLine(point1, point2, *other_points)``.

    Each point is a ``GeometryPoint`` or the SDK's ``GeometryPoint`` (converted); ``TypeError``
    naming the position for anything else.
    """

    __slots__ = ("geometry_points",)
    __match_args__ = ("geometry_points",)
    _GEOJSON_TYPE = "LineString"
    _SDK_CLASS = SdkGeometryLine
    _STATE = ("geometry_points",)
    _ATTR = "geometry_points"

    geometry_points: tuple[GeometryPoint, ...]

    def __init__(self, point1: GeometryPoint, point2: GeometryPoint, *other_points: GeometryPoint) -> None:
        points = (point1, point2, *other_points)
        object.__setattr__(
            self,
            "geometry_points",
            tuple(_component(p, GeometryPoint, "GeometryLine", "point", i) for i, p in enumerate(points, 1)),
        )

    def get_coordinates(self) -> list[tuple[float, float]]:
        """``[(longitude, latitude), …]``."""
        return [point.get_coordinates() for point in self.geometry_points]

    @staticmethod
    def parse_coordinates(coordinates: Sequence[Sequence[float]]) -> GeometryLine:
        """A line from ``(longitude, latitude)`` pairs; ``TypeError`` for fewer than 2."""
        points = [
            GeometryPoint.parse_coordinates(c)
            for c in _items(coordinates, "GeometryLine.parse_coordinates()", "a list of (longitude, latitude) pairs")
        ]
        if len(points) < 2:
            raise TypeError(f"a GeometryLine takes at least 2 points, got {len(points)}")
        return GeometryLine(*points)


class GeometryPolygon(_Composite):
    """A polygon: ``GeometryPolygon(exterior_ring, *interior_rings)``, each ring a ``GeometryLine``.

    Each ring has at least 4 points and its first point equals its last, else
    ``InvalidGeometryError`` with the SDK's wording. A ring may be the SDK's ``GeometryLine``
    (converted); ``TypeError`` naming the position for anything else.
    """

    __slots__ = ("geometry_lines",)
    __match_args__ = ("geometry_lines",)
    _GEOJSON_TYPE = "Polygon"
    _SDK_CLASS = SdkGeometryPolygon
    _STATE = ("geometry_lines",)
    _ATTR = "geometry_lines"

    geometry_lines: tuple[GeometryLine, ...]

    def __init__(self, exterior_ring: GeometryLine, *interior_rings: GeometryLine) -> None:
        rings = tuple(
            _component(r, GeometryLine, "GeometryPolygon", "ring", i) for i, r in enumerate((exterior_ring, *interior_rings), 1)
        )
        for index, ring in enumerate(rings):
            _check_ring(ring, "exterior" if index == 0 else f"interior (hole {index})")
        object.__setattr__(self, "geometry_lines", rings)

    def get_coordinates(self) -> list[list[tuple[float, float]]]:
        """One list of ``(longitude, latitude)`` pairs per ring, the exterior ring first."""
        return [ring.get_coordinates() for ring in self.geometry_lines]

    @staticmethod
    def parse_coordinates(coordinates: Sequence[Sequence[Sequence[float]]]) -> GeometryPolygon:
        """A polygon from GeoJSON-style rings (the exterior ring first); ``TypeError`` for no ring."""
        rings = [
            GeometryLine.parse_coordinates(r)
            for r in _items(coordinates, "GeometryPolygon.parse_coordinates()", "a list of rings")
        ]
        if not rings:
            raise TypeError("a GeometryPolygon takes at least 1 ring, got 0")
        return GeometryPolygon(*rings)


class GeometryMultiPoint(_Composite):
    """Zero or more points: ``GeometryMultiPoint(*geometry_points)``."""

    __slots__ = ("geometry_points",)
    __match_args__ = ("geometry_points",)
    _GEOJSON_TYPE = "MultiPoint"
    _SDK_CLASS = SdkGeometryMultiPoint
    _STATE = ("geometry_points",)
    _ATTR = "geometry_points"

    geometry_points: tuple[GeometryPoint, ...]

    def __init__(self, *geometry_points: GeometryPoint) -> None:
        object.__setattr__(
            self,
            "geometry_points",
            tuple(_component(p, GeometryPoint, "GeometryMultiPoint", "point", i) for i, p in enumerate(geometry_points, 1)),
        )

    def get_coordinates(self) -> list[tuple[float, float]]:
        """``[(longitude, latitude), …]``."""
        return [point.get_coordinates() for point in self.geometry_points]

    @staticmethod
    def parse_coordinates(coordinates: Sequence[Sequence[float]]) -> GeometryMultiPoint:
        """A multi-point from ``(longitude, latitude)`` pairs."""
        return GeometryMultiPoint(
            *(
                GeometryPoint.parse_coordinates(c)
                for c in _items(coordinates, "GeometryMultiPoint.parse_coordinates()", "a list of (longitude, latitude) pairs")
            )
        )


class GeometryMultiLine(_Composite):
    """Zero or more lines: ``GeometryMultiLine(*geometry_lines)``."""

    __slots__ = ("geometry_lines",)
    __match_args__ = ("geometry_lines",)
    _GEOJSON_TYPE = "MultiLineString"
    _SDK_CLASS = SdkGeometryMultiLine
    _STATE = ("geometry_lines",)
    _ATTR = "geometry_lines"

    geometry_lines: tuple[GeometryLine, ...]

    def __init__(self, *geometry_lines: GeometryLine) -> None:
        object.__setattr__(
            self,
            "geometry_lines",
            tuple(_component(line, GeometryLine, "GeometryMultiLine", "line", i) for i, line in enumerate(geometry_lines, 1)),
        )

    def get_coordinates(self) -> list[list[tuple[float, float]]]:
        """One list of ``(longitude, latitude)`` pairs per line."""
        return [line.get_coordinates() for line in self.geometry_lines]

    @staticmethod
    def parse_coordinates(coordinates: Sequence[Sequence[Sequence[float]]]) -> GeometryMultiLine:
        """A multi-line from one list of ``(longitude, latitude)`` pairs per line."""
        return GeometryMultiLine(
            *(
                GeometryLine.parse_coordinates(c)
                for c in _items(coordinates, "GeometryMultiLine.parse_coordinates()", "a list of lines")
            )
        )


class GeometryMultiPolygon(_Composite):
    """Zero or more polygons: ``GeometryMultiPolygon(*geometry_polygons)``."""

    __slots__ = ("geometry_polygons",)
    __match_args__ = ("geometry_polygons",)
    _GEOJSON_TYPE = "MultiPolygon"
    _SDK_CLASS = SdkGeometryMultiPolygon
    _STATE = ("geometry_polygons",)
    _ATTR = "geometry_polygons"

    geometry_polygons: tuple[GeometryPolygon, ...]

    def __init__(self, *geometry_polygons: GeometryPolygon) -> None:
        object.__setattr__(
            self,
            "geometry_polygons",
            tuple(
                _component(polygon, GeometryPolygon, "GeometryMultiPolygon", "polygon", i)
                for i, polygon in enumerate(geometry_polygons, 1)
            ),
        )

    def get_coordinates(self) -> list[list[list[tuple[float, float]]]]:
        """One list of rings per polygon."""
        return [polygon.get_coordinates() for polygon in self.geometry_polygons]

    @staticmethod
    def parse_coordinates(coordinates: Sequence[Sequence[Sequence[Sequence[float]]]]) -> GeometryMultiPolygon:
        """A multi-polygon from one list of GeoJSON-style rings per polygon."""
        return GeometryMultiPolygon(
            *(
                GeometryPolygon.parse_coordinates(c)
                for c in _items(coordinates, "GeometryMultiPolygon.parse_coordinates()", "a list of polygons")
            )
        )


class GeometryCollection(_Composite):
    """Zero or more geometries of any kind: ``GeometryCollection(*geometries)``.

    A member is any ORM geometry or any SDK geometry (converted). Collections nest at most 32
    levels (a collection whose members are not collections is 1 level); deeper raises
    ``InvalidGeometryError``. ``get_coordinates()`` lists the members' coordinates (the SDK's
    collection has no such method).
    """

    __slots__ = ("geometries", "_levels")
    __match_args__ = ("geometries",)
    _GEOJSON_TYPE = "GeometryCollection"
    _SDK_CLASS = SdkGeometryCollection
    _STATE = ("geometries", "_levels")
    _ATTR = "geometries"

    geometries: tuple[Geometry, ...]
    # Levels of collections, this one included: stored once, so the cap check is O(members).
    _levels: int

    def __init__(self, *geometries: Geometry) -> None:
        members = tuple(_collection_member(g, i) for i, g in enumerate(geometries, 1))
        levels = _levels_of(members)
        if levels > _MAX_COLLECTION_LEVELS:
            raise _too_deep(levels)
        object.__setattr__(self, "geometries", members)
        object.__setattr__(self, "_levels", levels)

    def get_coordinates(self) -> list[Any]:
        """``[member.get_coordinates() for member in geometries]``."""
        return [member.get_coordinates() for member in self.geometries]

    def to_geojson(self) -> dict[str, Any]:
        """``{"type": "GeometryCollection", "geometries": [<member GeoJSON>, …]}``."""
        return {"type": "GeometryCollection", "geometries": [member.to_geojson() for member in self.geometries]}


def _levels_of(members: Iterable[Any]) -> int:
    """The levels of a collection holding ``members``: 1 plus its deepest member collection's."""
    return 1 + max((getattr(m, "_levels", 1) for m in members if isinstance(m, GeometryCollection)), default=0)


# --- kinds, keys and pickle ------------------------------------------------------------------------

#: The SDK classes, in the order ``_kind_of`` tries them, with their GeoJSON type.
_SDK_KINDS: tuple[tuple[type[Any], str], ...] = (
    (SdkGeometryPoint, "Point"),
    (SdkGeometryLine, "LineString"),
    (SdkGeometryPolygon, "Polygon"),
    (SdkGeometryMultiPoint, "MultiPoint"),
    (SdkGeometryMultiLine, "MultiLineString"),
    (SdkGeometryMultiPolygon, "MultiPolygon"),
    (SdkGeometryCollection, "GeometryCollection"),
)
#: The ORM classes made of members, with the SDK class each one converts from.
_COMPOSITES: tuple[tuple[type[Any], type[_Composite]], ...] = (
    (SdkGeometryLine, GeometryLine),
    (SdkGeometryPolygon, GeometryPolygon),
    (SdkGeometryMultiPoint, GeometryMultiPoint),
    (SdkGeometryMultiLine, GeometryMultiLine),
    (SdkGeometryMultiPolygon, GeometryMultiPolygon),
    (SdkGeometryCollection, GeometryCollection),
)
#: GeoJSON type → the attribute holding the members (every kind but ``Point``).
_MEMBERS: dict[str, str] = {orm_cls._GEOJSON_TYPE: orm_cls._ATTR for _, orm_cls in _COMPOSITES}


def _kind_of(value: Any) -> str | None:
    """The GeoJSON type of an ORM or SDK geometry; ``None`` for anything else."""
    if isinstance(value, Geometry):
        kind: str | None = getattr(type(value), "_GEOJSON_TYPE", None)
        return kind
    for sdk_cls, kind_name in _SDK_KINDS:
        if isinstance(value, sdk_cls):
            return kind_name
    return None


def _key(value: Any) -> Any:
    """The equality and hash key of an ORM or SDK geometry: its kind and nested coordinates.

    A member that is not a geometry (only a value built unchecked holds one) is its own key.
    """
    kind = _kind_of(value)
    if kind is None:
        return value
    if kind == "Point":
        return (kind, value.longitude, value.latitude)
    return (kind, tuple(_key(member) for member in getattr(value, _MEMBERS[kind])))


def _restore(cls: type[_G], state: tuple[Any, ...]) -> _G:
    """Unpickle: an instance of ``cls`` whose slots hold ``state``, without running a constructor."""
    instance = object.__new__(cls)
    for name, value in zip(cls._STATE, state, strict=True):
        object.__setattr__(instance, name, value)
    return instance


# --- SDK conversion ------------------------------------------------------------------------------------


def _sdk_members(value: Any, attr: str) -> list[Any] | tuple[Any, ...]:
    members = getattr(value, attr, None)
    if not isinstance(members, (list, tuple)):
        raise TypeError(f"the SDK {type(value).__name__}'s {attr} must be a list, not {type(members).__name__}")
    return members


def _from_sdk_checked(value: Any, depth: int) -> Geometry:
    """An SDK geometry as the equal ORM value, through the public constructors.

    ``depth`` counts the collections above ``value``: a collection deeper than the cap is refused
    before its members are read, so a deeply nested SDK value never exhausts the stack.
    """
    if isinstance(value, SdkGeometryPoint):
        return GeometryPoint(value.longitude, value.latitude)
    if isinstance(value, SdkGeometryCollection):
        if depth > _MAX_COLLECTION_LEVELS:
            raise _too_deep(depth)
        members = [
            _from_sdk_checked(m, depth + 1) if isinstance(m, _SDK_GEOMETRIES) else m for m in _sdk_members(value, "geometries")
        ]
        return GeometryCollection(*members)
    for sdk_cls, orm_cls in _COMPOSITES:
        if isinstance(value, sdk_cls):
            members = list(_sdk_members(value, orm_cls._ATTR))
            if orm_cls is GeometryLine and len(members) < 2:
                raise InvalidGeometryError(f"a GeometryLine has at least 2 points, got {len(members)}")
            if orm_cls is GeometryPolygon and not members:
                raise InvalidGeometryError("a GeometryPolygon has at least 1 ring, got 0")
            return orm_cls(*members)
    raise TypeError(f"not an SDK geometry: {type(value).__name__}")


def _from_sdk_unchecked(value: Any) -> Any:
    """An SDK geometry as an ORM value built without any check (``object.__new__`` + slots).

    Coordinates are kept as they are, member lists become tuples, SDK members are converted the
    same way and any other value (a member included) is returned as it is.
    """
    if isinstance(value, Geometry):
        return value
    if isinstance(value, SdkGeometryPoint):
        point = object.__new__(GeometryPoint)
        object.__setattr__(point, "longitude", value.longitude)
        object.__setattr__(point, "latitude", value.latitude)
        return point
    for sdk_cls, orm_cls in _COMPOSITES:
        if isinstance(value, sdk_cls):
            members = tuple(_from_sdk_unchecked(m) for m in _sdk_members(value, orm_cls._ATTR))
            instance = object.__new__(orm_cls)
            object.__setattr__(instance, orm_cls._ATTR, members)
            if orm_cls is GeometryCollection:
                object.__setattr__(instance, "_levels", _levels_of(members))
            return instance
    return value


def _from_sdk(value: Any, *, checked: bool) -> Any:
    """An SDK geometry as the matching ORM value.

    ``checked=True`` goes through the public constructors: ``TypeError`` / ``InvalidGeometryError``
    for what they refuse, the 32-level cap included. ``checked=False`` (the serializer's path) runs
    no check and never raises: it returns ``value`` unchanged when it cannot read it (missing
    attributes, a nesting beyond Python's recursion limit…).
    """
    if checked:
        return _from_sdk_checked(value, 1)
    try:
        return _from_sdk_unchecked(value)
    except Exception:
        return value


def _geometry_from_sdk(value: Any) -> Any:
    """The read rule of a top-level geometry field (spec §6.6): an SDK geometry → the ORM value.

    Used by ``BaseSurrealModel._apply_record``, which copies a returned row onto the instance
    without validation. Only an instance of exactly one of the seven SDK classes is converted,
    through the public constructors (checked); a value they refuse, a subclass and any other type
    are returned as they are. Pure and never raising, so a write that syncs the instance never
    fails here.
    """
    if type(value) not in _SDK_GEOMETRIES:
        return value
    try:
        return _from_sdk(value, checked=True)
    except Exception:
        return value


# --- query arguments -------------------------------------------------------------------------------


def _as_point(value: Any) -> GeometryPoint:
    """The point a query takes (``QuerySet.nearby()``, ``GeoDistance``), as an ORM ``GeometryPoint``.

    Accepted: an ORM ``GeometryPoint`` (returned as it is), the SDK's (converted, checked), a
    ``(longitude, latitude)`` pair of real numbers (a ``tuple``, a ``list`` or another sequence that
    is not a string) and a GeoJSON ``Point`` object. ``TypeError`` for any other type and for a pair
    with other than 2 items or a non-number; ``InvalidGeometryError`` for a non-finite coordinate, one
    too large for a float, and a GeoJSON object that is not a valid ``Point``.
    """
    if isinstance(value, GeometryPoint):
        return value
    if isinstance(value, SdkGeometryPoint):
        return GeometryPoint(value.longitude, value.latitude)
    if isinstance(value, Mapping):
        return GeometryPoint.from_geojson(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return GeometryPoint.parse_coordinates(value)
    raise TypeError(
        "a point is a GeometryPoint (the ORM's or the SDK's), a (longitude, latitude) pair or a GeoJSON Point "
        f"object, not {type(value).__name__}"
    )


class GeoDistance:
    """The distance from a record's point to a reference point, for ``QuerySet.annotate()`` (v0.24.0).

    Same name and arguments as the full SurrealDB-ORM::

        near = await (
            Restaurant.objects()
            .nearby("location", (-73.9855, 40.7580), max_distance=2_000)
            .annotate(dist=GeoDistance("location", (-73.9855, 40.7580)))
            .order_by("dist")
            .exec()
        )
        print([(r.name, round(r.dist)) for r in near])

    Each instance ``exec()`` returns carries the value as an attribute named after the alias: SurrealDB's
    ``geo::distance`` in metres (a ``float``), or ``None`` when that record's field is not a point. The
    attribute is never written back.

    Args:
        field: A Python field name (an aliased field compiles its column) or a dotted path; ``annotate()``
            checks it against the model.
        point: The reference point: a ``GeometryPoint`` (the ORM's or the SDK's), a
            ``(longitude, latitude)`` pair or a GeoJSON ``Point`` object; kept as an ORM ``GeometryPoint``.

    Raises:
        TypeError: ``field`` is not a string; ``point`` is none of the forms above.
        InvalidGeometryError: a non-finite coordinate, or a GeoJSON object that is not a valid ``Point``.
    """

    __slots__ = ("field", "point")

    field: str
    point: GeometryPoint

    def __init__(
        self,
        field: str,
        point: GeometryPoint | SdkGeometryPoint | tuple[float, float] | Sequence[float] | Mapping[str, Any],
    ) -> None:
        self.field = _distance_field(field)
        self.point = _as_point(point)

    def __repr__(self) -> str:
        point: Any = self.point
        shown = point.get_coordinates() if isinstance(point, GeometryPoint) else point
        return f"GeoDistance({self.field!r}, {shown!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, GeoDistance):
            return NotImplemented
        return bool(self.field == other.field and self.point == other.point)

    def __hash__(self) -> int:
        return hash(("GeoDistance", self.field, self.point))


def _distance_field(field: Any) -> str:
    """``GeoDistance``'s field: a ``str`` (``TypeError`` otherwise); the name itself is checked by ``annotate()``."""
    if not isinstance(field, str):
        raise TypeError(f"GeoDistance takes a field name (str), not {type(field).__name__}")
    return field


# --- SurrealQL text --------------------------------------------------------------------------------

#: GeoJSON type → the GeoJSON type of its members (the kinds whose members are coordinate arrays).
_MEMBER_TYPES: dict[str, str] = {
    "LineString": "Point",
    "Polygon": "LineString",
    "MultiPoint": "Point",
    "MultiLineString": "LineString",
    "MultiPolygon": "Polygon",
}


def _coordinate_text(value: Any) -> str:
    """A coordinate's text: ``float.__repr__(float(value))`` for a finite ``int`` (not ``bool``) or ``float``.

    ``TypeError`` for anything else — a ``Decimal`` or a decimal ``CBORTag`` has no verified text, and
    neither has ``nan``, an infinity, an ``int`` too large for a float or a ``str``.
    """
    needed = "a geometry coordinate must be a finite int or float to be written as SurrealQL"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        # The encoding boundary sends an exponent-form ``Decimal`` as a decimal ``CBORTag``.
        if isinstance(value, Decimal) or (isinstance(value, CBORTag) and value.tag == TAG_DECIMAL_STRING):
            raise TypeError(f"{needed}, not a decimal: a decimal coordinate has no verified text, use a float")
        raise TypeError(f"{needed}, not {type(value).__name__}")
    try:
        number = float(value)
    except OverflowError:
        raise TypeError(f"{needed}: this int is too large for a float") from None
    if not math.isfinite(number):
        raise TypeError(f"{needed}, not {number!r}")
    return float.__repr__(number)


def _coordinates_text(value: Any, kind: str) -> str:
    """The GeoJSON ``coordinates`` array of a point or of a kind made of coordinate arrays, as text."""
    if kind == "Point":
        return f"[{_coordinate_text(value.longitude)}, {_coordinate_text(value.latitude)}]"
    member_type = _MEMBER_TYPES[kind]
    parts = []
    for member in _sdk_members(value, _MEMBERS[kind]):
        if _kind_of(member) != member_type:
            raise TypeError(f"a {kind} member must be a {member_type} geometry, not {type(member).__name__}")
        parts.append(_coordinates_text(member, member_type))
    return f"[{', '.join(parts)}]"


def _geometry_text(value: Any) -> str:
    """The SurrealQL text of an ORM or SDK geometry (and of their members): its GeoJSON **object** form.

    ``{type: 'Point', coordinates: [-73.99, 40.75]}``, ``{type: 'GeometryCollection', geometries:
    [{type: 'Point', coordinates: [1.0, 2.0]}, …]}``: every kind and every collection member is an
    object, the form every SurrealDB line parses as a geometry equal to the bound value. Rings are
    written as given (SurrealDB closes an open ring). Coordinates follow ``_coordinate_text``. No
    structural check: ``TypeError`` only when a part has no text (a coordinate, a member that is not a
    geometry of the expected kind, a member list that is not a ``list`` or ``tuple``).

    What ``str()`` of a geometry returns; ``surql_literal.to_surql_literal`` writes it into live
    filters (an SDK geometry checked by the encoding boundary first).
    """
    kind = _kind_of(value)
    if kind == "GeometryCollection":
        members = ", ".join(_geometry_text(member) for member in _sdk_members(value, "geometries"))
        return f"{{type: 'GeometryCollection', geometries: [{members}]}}"
    if kind == "Point" or kind in _MEMBER_TYPES:
        return f"{{type: '{kind}', coordinates: {_coordinates_text(value, kind)}}}"
    raise TypeError(f"not a geometry: {type(value).__name__}")


# --- GeoJSON -------------------------------------------------------------------------------------------

_GEOJSON_PARSERS: dict[str, Callable[[Any], Geometry]] = {
    "Point": GeometryPoint.parse_coordinates,
    "LineString": GeometryLine.parse_coordinates,
    "Polygon": GeometryPolygon.parse_coordinates,
    "MultiPoint": GeometryMultiPoint.parse_coordinates,
    "MultiLineString": GeometryMultiLine.parse_coordinates,
    "MultiPolygon": GeometryMultiPolygon.parse_coordinates,
}


def _geojson_array(data: Mapping[str, Any], key: str, kind: str) -> Sequence[Any]:
    if key not in data:
        raise InvalidGeometryError(f"a GeoJSON {kind} needs a {key!r} member")
    value = data[key]
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise InvalidGeometryError(f"the {key!r} of a GeoJSON {kind} must be an array, not {type(value).__name__}")
    return value


def _from_geojson(data: Any, depth: int) -> Geometry:
    """The geometry of a GeoJSON geometry object; ``depth`` counts the collections above it."""
    if not isinstance(data, Mapping):
        raise InvalidGeometryError(f"a GeoJSON geometry is a mapping, not {type(data).__name__}")
    kind = data.get("type")
    if kind == "GeometryCollection":
        if depth > _MAX_COLLECTION_LEVELS:
            raise _too_deep(depth)
        members = _geojson_array(data, "geometries", "GeometryCollection")
        return GeometryCollection(*(_from_geojson(member, depth + 1) for member in members))
    if not isinstance(kind, str) or kind not in _GEOJSON_PARSERS:
        raise InvalidGeometryError(
            f"unknown GeoJSON geometry type {kind!r}: expected Point, LineString, Polygon, MultiPoint, "
            "MultiLineString, MultiPolygon or GeometryCollection"
        )
    coordinates = _geojson_array(data, "coordinates", kind)
    try:
        return _GEOJSON_PARSERS[kind](coordinates)
    except TypeError as exc:
        raise InvalidGeometryError(f"malformed GeoJSON {kind}: {exc}") from exc


# --- Pydantic fields ------------------------------------------------------------------------------

#: The kinds, in SurrealDB's ``geometry<…>`` vocabulary and the SDK's order.
_KINDS: tuple[str, ...] = ("point", "line", "polygon", "multipoint", "multiline", "multipolygon", "collection")
_ALL_KINDS: frozenset[str] = frozenset(_KINDS)
_KIND_CLASSES: dict[str, type[Geometry]] = {
    "point": GeometryPoint,
    "line": GeometryLine,
    "polygon": GeometryPolygon,
    "multipoint": GeometryMultiPoint,
    "multiline": GeometryMultiLine,
    "multipolygon": GeometryMultiPolygon,
    "collection": GeometryCollection,
}
#: Every name ``GeoField[...]`` accepts (case-insensitive) and the kinds it stands for: SurrealDB's
#: names, plus the full SurrealDB-ORM's and GeoJSON's spellings.
_KIND_NAMES: dict[str, frozenset[str]] = {
    **{kind: frozenset({kind}) for kind in _KINDS},
    "linestring": frozenset({"line"}),
    "multilinestring": frozenset({"multiline"}),
    "geometrycollection": frozenset({"collection"}),
    "feature": _ALL_KINDS,
    "geometry": _ALL_KINDS,
}


def _ordered(kinds: frozenset[str]) -> tuple[str, ...]:
    return tuple(kind for kind in _KINDS if kind in kinds)


def _info(kinds: frozenset[str]) -> str:
    """``"feature"`` for every kind, else the kinds joined with ``|`` in the canonical order."""
    return "feature" if kinds == _ALL_KINDS else "|".join(_ordered(kinds))


def _class_kinds(cls: type[Geometry]) -> frozenset[str]:
    """The kinds a class annotation stands for: its own, or every kind for ``Geometry``."""
    for kind in _KINDS:
        if issubclass(cls, _KIND_CLASSES[kind]):
            return frozenset({kind})
    return frozenset(kind for kind in _KINDS if issubclass(_KIND_CLASSES[kind], cls))


def _parse_kinds(kinds: object) -> frozenset[str]:
    """The kinds of ``GeoField[kinds]``: one string, case-insensitive, ``|``-separated."""
    if not isinstance(kinds, str):
        raise TypeError(
            "GeoField takes one string of geometry kinds, such as GeoField['point'] or GeoField['point|polygon'], "
            f"not {type(kinds).__name__}"
        )
    parsed: set[str] = set()
    for part in kinds.split("|"):
        name = part.strip().lower()
        if name not in _KIND_NAMES:
            raise ValueError(
                f"unknown geometry kind {part.strip()!r} in GeoField[{kinds!r}]: expected point, line (or linestring), "
                "polygon, multipoint, multiline (or multilinestring), multipolygon, collection (or geometrycollection) "
                "or feature (or geometry: any kind), several joined with '|'"
            )
        parsed |= _KIND_NAMES[name]
    return frozenset(parsed)


class _GeoValidator:
    """The field validator of one set of kinds (spec §6.2): a refusal is a ``ValueError``.

    An ORM value of an accepted kind is returned as it is; the SDK's is converted through the
    constructors (checked); a mapping is read as GeoJSON; a 2-item ``tuple``/``list`` is a
    ``(longitude, latitude)`` pair when points are accepted. ``TypeError`` and
    ``InvalidGeometryError`` raised on the way become ``ValueError``, so Pydantic reports a
    ``ValidationError``.
    """

    __slots__ = ("_classes", "_sdk_classes", "_points", "_expected", "_forms")

    def __init__(self, kinds: frozenset[str]) -> None:
        self._classes: tuple[type[Geometry], ...] = tuple(_KIND_CLASSES[kind] for kind in _ordered(kinds))
        self._sdk_classes: tuple[type[Any], ...] = tuple(cls._SDK_CLASS for cls in self._classes)
        self._points = "point" in kinds
        names = " or ".join(cls.__name__ for cls in self._classes)
        self._expected = "a geometry" if kinds == _ALL_KINDS else f"a {names}"
        geojson = "object" if kinds == _ALL_KINDS else " or ".join(cls._GEOJSON_TYPE for cls in self._classes) + " object"
        pair = ", or a (longitude, latitude) pair" if self._points else ""
        self._forms = f"{self._expected} (the ORM's or the SDK's class), a GeoJSON {geojson}{pair}"

    def __call__(self, value: Any) -> Geometry:
        try:
            return self._validate(value)
        except (TypeError, InvalidGeometryError) as exc:
            raise ValueError(str(exc)) from exc

    def _validate(self, value: Any) -> Geometry:
        if isinstance(value, Geometry):
            if isinstance(value, self._classes):
                return value
            raise ValueError(f"expected {self._expected}, got a {type(value).__name__}")
        if isinstance(value, _SDK_GEOMETRIES):
            if isinstance(value, self._sdk_classes):
                return _from_sdk_checked(value, 1)
            raise ValueError(f"expected {self._expected}, got the SDK's {type(value).__name__}")
        if isinstance(value, Mapping):
            geometry = Geometry.from_geojson(value)
            if isinstance(geometry, self._classes):
                return geometry
            raise ValueError(f"expected {self._expected}, got a GeoJSON {geometry._GEOJSON_TYPE}")
        if self._points and isinstance(value, (tuple, list)):
            return GeometryPoint.parse_coordinates(value)
        raise ValueError(f"expected {self._forms}, not {type(value).__name__}")


@functools.cache
def _validator(kinds: frozenset[str]) -> _GeoValidator:
    """One validator per set of kinds, shared by every field that accepts them."""
    return _GeoValidator(kinds)


def _serialize_geometry(value: Any, info: core_schema.SerializationInfo[Any]) -> Any:
    """The geometry field serializer (spec §5.2).

    Python mode returns an ORM geometry: the value itself, or — when the attribute holds a raw
    SDK geometry (``model_construct``, a read sync, ``hydrate()``'s fallback) — one built
    **unchecked**, so ``model_dump()`` never raises and never hands Pydantic an SDK dataclass to
    flatten into a dict. JSON mode writes ``to_geojson()``.

    Any other value is handed back to Pydantic (``PydanticSerializationUnexpectedValue``, in both
    modes): in a union field Pydantic then tries the next member, so ``GeometryPoint | str`` writes
    a ``str`` as one; in a field that is not a union (a stray value set through
    ``model_construct``) it emits its standard serializer warning and writes the inferred value.
    """
    if isinstance(value, _SDK_GEOMETRIES):
        value = _from_sdk(value, checked=False)
    if not isinstance(value, Geometry):
        raise PydanticSerializationUnexpectedValue(f"Expected `Geometry`, got `{type(value).__name__}`")
    if info.mode_is_json():
        return value.to_geojson()
    return value


def _field_schema(kinds: frozenset[str]) -> core_schema.CoreSchema:
    """The core schema of a field of these kinds: the validator and the serializer (not nullable)."""
    return core_schema.no_info_plain_validator_function(
        _validator(kinds),
        serialization=core_schema.plain_serializer_function_ser_schema(_serialize_geometry, info_arg=True),
    )


def _geojson_json_schema(kinds: frozenset[str]) -> dict[str, Any]:
    """The JSON schema of the GeoJSON object of these kinds (``type`` lists them)."""
    classes = [_KIND_CLASSES[kind] for kind in _ordered(kinds)]
    types = [cls._GEOJSON_TYPE for cls in classes]
    title = "GeoJSON geometry" if kinds == _ALL_KINDS else f"GeoJSON {' or '.join(types)}"
    with_coordinates = any(cls is not GeometryCollection for cls in classes)
    with_geometries = GeometryCollection in classes
    properties: dict[str, Any] = {"type": {"enum": types}}
    required = ["type"]
    if with_coordinates:
        properties["coordinates"] = {"type": "array"}
    if with_geometries:
        properties["geometries"] = {"type": "array", "items": {"type": "object"}}
    if not with_geometries:
        required.append("coordinates")
    elif not with_coordinates:
        required.append("geometries")
    return {"type": "object", "title": title, "properties": properties, "required": required}


class _GeoMarker:
    """The ``Annotated`` metadata of ``GeoField[...]`` and the aliases: the kinds the field accepts.

    Its core schema is the kinds' validator and serializer made nullable; the ``None`` default
    comes from the alias's ``Field(default=None)``, so Pydantic knows the field is not required.
    """

    __slots__ = ("kinds",)

    def __init__(self, kinds: frozenset[str]) -> None:
        self.kinds = kinds

    def __repr__(self) -> str:
        return f"GeoField[{_info(self.kinds)!r}]"

    def __get_pydantic_core_schema__(self, source_type: Any, handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
        return core_schema.nullable_schema(_field_schema(self.kinds))

    def __get_pydantic_json_schema__(self, schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        return {"anyOf": [_geojson_json_schema(self.kinds), {"type": "null"}]}


# ``Annotated`` subscripted with values built at run time (mypy takes it for a type expression).
_ANNOTATED: Any = Annotated


def _annotation(kinds: frozenset[str]) -> Any:
    """``Annotated[<class or union> | None, _GeoMarker(kinds), Field(default=None)]``."""
    if kinds == _ALL_KINDS:
        inner: Any = Geometry
    else:
        inner = functools.reduce(operator.or_, (_KIND_CLASSES[kind] for kind in _ordered(kinds)))
    return _ANNOTATED[inner | None, _GeoMarker(kinds), Field(default=None)]


class GeoField:
    """A geometry field of the given kinds, nullable with default ``None`` (the full SurrealDB-ORM's form).

    ``GeoField["point"]``, ``GeoField["polygon"]``, ``GeoField["point|polygon"]``,
    ``GeoField["feature"]`` (any kind): one string, case-insensitive, ``|``-separated. The kinds are
    SurrealDB's ``geometry<…>`` names — ``point``, ``line``, ``polygon``, ``multipoint``,
    ``multiline``, ``multipolygon``, ``collection``, ``feature`` — and ``linestring``,
    ``multilinestring``, ``geometrycollection`` and ``geometry`` are accepted for ``line``,
    ``multiline``, ``collection`` and ``feature``. An unknown kind raises ``ValueError`` and a
    non-string ``TypeError``, where the class is defined.

    The result is ``Annotated[<class or union> | None, …, Field(default=None)]``: the attribute is
    a geometry object (``GeometryPoint``, …) or ``None``, the field is not required, and
    ``model_construct()`` sets ``None``. Type checkers do not understand ``GeoField[...]``: use the
    aliases (``PointField``…) with ``= None`` in the class body. Inside containers
    (``list[GeometryPoint]``), use the classes.
    """

    __slots__ = ()

    def __new__(cls, *args: Any, **kwargs: Any) -> Self:
        raise TypeError("GeoField is an annotation, not a value: write `location: GeoField['point']`")

    def __class_getitem__(cls, kinds: str) -> Any:
        return _annotation(_parse_kinds(kinds))


# The aliases: ``GeoField[...]`` spelt out, so type checkers see ``GeometryPoint | None`` and so on.
# Without the Pydantic mypy plugin, mypy only knows a default written in the class body: declare
# ``location: PointField = None``. Meant for model fields; at nested positions use the classes.

#: A point field: ``GeometryPoint | None``, default ``None`` (full SurrealDB-ORM).
PointField: TypeAlias = Annotated[GeometryPoint | None, _GeoMarker(frozenset({"point"})), Field(default=None)]
#: A line field: ``GeometryLine | None``, default ``None`` (full SurrealDB-ORM).
LineStringField: TypeAlias = Annotated[GeometryLine | None, _GeoMarker(frozenset({"line"})), Field(default=None)]
#: A polygon field: ``GeometryPolygon | None``, default ``None`` (full SurrealDB-ORM).
PolygonField: TypeAlias = Annotated[GeometryPolygon | None, _GeoMarker(frozenset({"polygon"})), Field(default=None)]
#: A multi-point field: ``GeometryMultiPoint | None``, default ``None`` (full SurrealDB-ORM).
MultiPointField: TypeAlias = Annotated[GeometryMultiPoint | None, _GeoMarker(frozenset({"multipoint"})), Field(default=None)]
#: A multi-line field: ``GeometryMultiLine | None``, default ``None``.
MultiLineStringField: TypeAlias = Annotated[GeometryMultiLine | None, _GeoMarker(frozenset({"multiline"})), Field(default=None)]
#: A multi-polygon field: ``GeometryMultiPolygon | None``, default ``None``.
MultiPolygonField: TypeAlias = Annotated[
    GeometryMultiPolygon | None, _GeoMarker(frozenset({"multipolygon"})), Field(default=None)
]
#: A geometry-collection field: ``GeometryCollection | None``, default ``None``.
GeometryCollectionField: TypeAlias = Annotated[
    GeometryCollection | None, _GeoMarker(frozenset({"collection"})), Field(default=None)
]
#: A field of any geometry kind: ``Geometry | None``, default ``None``.
GeometryField: TypeAlias = Annotated[Geometry | None, _GeoMarker(_ALL_KINDS), Field(default=None)]


def _geo_kinds(annotation: Any) -> frozenset[str] | None:
    """The geometry kinds a field annotation accepts, or ``None`` when it is not a geometry field.

    Unwraps ``Annotated`` (a ``GeoField`` marker gives its kinds), then ``X | None``, then maps an
    ORM class, or a union of ORM classes, to its kinds. Works on ``model_fields[name].annotation``
    too, where Pydantic has removed the marker (``GeometryPoint | None``). Containers, the SDK's
    classes and unions with a non-geometry member are not geometry fields.
    """
    origin = get_origin(annotation)
    if origin is Annotated:
        for metadata in getattr(annotation, "__metadata__", ()):
            if isinstance(metadata, _GeoMarker):
                return metadata.kinds
        return _geo_kinds(get_args(annotation)[0])
    if origin is Union or origin is UnionType:
        kinds: set[str] = set()
        for member in get_args(annotation):
            if member is type(None):
                continue
            member_kinds = _geo_kinds(member)
            if member_kinds is None:
                return None
            kinds |= member_kinds
        return frozenset(kinds) or None
    if isinstance(annotation, type) and issubclass(annotation, Geometry):
        return _class_kinds(annotation)
    return None


def is_geo_field(annotation: Any) -> bool:
    """Whether a field annotation is a geometry field: ``GeoField[...]``, an alias or a geometry class.

    ``GeometryPoint | None`` is one; ``list[GeometryPoint]``, ``GeometryPoint | str`` and the SDK's
    classes are not.
    """
    return _geo_kinds(annotation) is not None


def get_geo_info(annotation: Any) -> str | None:
    """The kinds of a geometry field annotation, or ``None`` when it is not one.

    SurrealDB's kind names joined with ``|`` in a fixed order (``"point"``, ``"point|polygon"``),
    or ``"feature"`` for any kind: ``get_geo_info(LineStringField)`` is ``"line"`` (the name of
    ``geometry<line>``; the full SurrealDB-ORM answers ``"linestring"``).
    """
    kinds = _geo_kinds(annotation)
    return None if kinds is None else _info(kinds)
