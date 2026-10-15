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

``GeometryCollection`` is a ``Geometry`` (the SDK's is not) and nests at most 32 levels of
collections, a depth every supported SurrealDB line accepts. Refusals raise ``TypeError`` for a
wrong argument type and ``InvalidGeometryError`` (a ``ValueError``) for a wrong value.
"""

from __future__ import annotations

import math
import numbers
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any, ClassVar, Self, TypeVar, cast

from ._sdk import (
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
