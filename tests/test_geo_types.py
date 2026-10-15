"""Geometry types (v0.24.0): the ORM's geometry value classes, their field annotations and their error.

Unit tests first (no database); the raw-SDK E2E classes that pin what the servers do with each
value are suffixed ``E2E``. The reference values below are shared with ``tests/test_geo_fields.py``.
"""

import copy
import functools
import math
import pickle
import warnings
from collections.abc import Callable, Coroutine
from decimal import Decimal
from fractions import Fraction
from typing import Annotated, Any, Optional

import pytest
import surrealdb
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model
from surrealdb import RecordID
from surrealdb.cbor import CBORTag
from surrealdb.data import GeometryCollection as SdkCollection
from surrealdb.data import GeometryLine as SdkLine
from surrealdb.data import GeometryMultiLine as SdkMultiLine
from surrealdb.data import GeometryMultiPoint as SdkMultiPoint
from surrealdb.data import GeometryMultiPolygon as SdkMultiPolygon
from surrealdb.data import GeometryPoint as SdkPoint
from surrealdb.data import GeometryPolygon as SdkPolygon

import surreal_orm_lite
from surreal_orm_lite import (
    AuthenticatedUserMixin,
    BaseSurrealModel,
    GeoField,
    Geometry,
    GeometryCollection,
    GeometryCollectionField,
    GeometryField,
    GeometryLine,
    GeometryMultiLine,
    GeometryMultiPoint,
    GeometryMultiPolygon,
    GeometryPoint,
    GeometryPolygon,
    InvalidGeometryError,
    LineStringField,
    MultiLineStringField,
    MultiPointField,
    MultiPolygonField,
    PointField,
    PolygonField,
    SurrealConfigDict,
    encoding,
    geo,
    live,
)
from surreal_orm_lite.surql_literal import inline_variables, to_surql_literal
from tests.conftest import orm_client
from tests.test_encoding import _no_client
from tests.test_rich_types import _http_client, _server_version

# Reference points as (longitude, latitude).
A: tuple[float, float] = (-73.98, 40.74)
B: tuple[float, float] = (-73.99, 40.75)
# ``geo::distance`` of A and B, in metres, on SurrealDB 2.7.0, 3.1.5 and 3.3.4.
AB_METRES = 1395.0401142718986
TIMES_SQUARE: tuple[float, float] = (-73.9855, 40.7580)
# 697.5104962226121 metres from B.
NEAR: tuple[float, float] = (-73.985, 40.745)
PARIS: tuple[float, float] = (2.35, 48.85)
# A midtown box: A, B and NEAR are inside it, PARIS is outside.
RING: list[tuple[float, float]] = [(-74.0, 40.7), (-73.9, 40.7), (-73.9, 40.8), (-74.0, 40.8), (-74.0, 40.7)]
HOLE: list[tuple[float, float]] = [
    (-73.995, 40.745),
    (-73.990, 40.745),
    (-73.990, 40.750),
    (-73.995, 40.750),
    (-73.995, 40.745),
]
# Decimal coordinates of an SDK geometry and the float SurrealDB stores for each (every line).
DECIMAL_STORED: dict[Decimal, float] = {
    Decimal("1E-7"): 1e-07,
    Decimal("1.5E-7"): 1.5e-07,
    Decimal("-2.5E-7"): -2.5e-07,
    Decimal("1E+2"): 100.0,
    Decimal("1.2E+2"): 120.0,
    Decimal("1.234567890123456789012345678901E-7"): 1.2345678901234568e-07,
    Decimal("9.99999999999999999999999999999E-7"): 1e-06,
    Decimal("7.9228162514264337593543950335E+28"): 7.922816251426434e28,
    Decimal("1E-30"): 0.0,
    Decimal("0.1234567890123456789012345678901"): 0.12345678901234569,
}


class TestInvalidGeometryError:
    """AC-2: the ORM's geometry refusal is a ``ValueError`` and the SDK's ``InvalidGeometryError``."""

    def test_is_a_value_error_and_the_sdk_class(self) -> None:
        assert issubclass(InvalidGeometryError, ValueError)
        assert issubclass(InvalidGeometryError, surrealdb.InvalidGeometryError)
        assert InvalidGeometryError.__name__ == "InvalidGeometryError"
        assert InvalidGeometryError.__module__ == "surreal_orm_lite.exceptions"

    def test_message(self) -> None:
        assert str(InvalidGeometryError("x")) == "x"

    def test_caught_by_code_written_against_the_sdk(self) -> None:
        with pytest.raises(surrealdb.InvalidGeometryError):
            raise InvalidGeometryError("x")
        with pytest.raises(ValueError):
            raise InvalidGeometryError("x")

    def test_exported(self) -> None:
        assert "InvalidGeometryError" in surreal_orm_lite.__all__
        assert surreal_orm_lite.InvalidGeometryError is surreal_orm_lite.exceptions.InvalidGeometryError


# --- geometry corpus (Task 2) ------------------------------------------------------------------

#: The seven kinds, in the SDK's order.
KINDS = ("point", "line", "polygon", "multipoint", "multiline", "multipolygon", "collection")
#: The six kinds that have ``parse_coordinates()`` (the SDK's collection has none).
COORDINATE_KINDS = KINDS[:-1]

ORM_CLASSES: dict[str, type[Geometry]] = {
    "point": GeometryPoint,
    "line": GeometryLine,
    "polygon": GeometryPolygon,
    "multipoint": GeometryMultiPoint,
    "multiline": GeometryMultiLine,
    "multipolygon": GeometryMultiPolygon,
    "collection": GeometryCollection,
}
SDK_CLASSES: dict[str, type[Any]] = {
    "point": SdkPoint,
    "line": SdkLine,
    "polygon": SdkPolygon,
    "multipoint": SdkMultiPoint,
    "multiline": SdkMultiLine,
    "multipolygon": SdkMultiPolygon,
    "collection": SdkCollection,
}


def _orm(kind: str) -> Geometry:
    """One ORM value per kind, built from the reference constants."""
    a, b = GeometryPoint(*A), GeometryPoint(*B)
    line = GeometryLine(a, b)
    polygon = GeometryPolygon.parse_coordinates([RING, HOLE])
    values: dict[str, Geometry] = {
        "point": a,
        "line": line,
        "polygon": polygon,
        "multipoint": GeometryMultiPoint(a, b),
        "multiline": GeometryMultiLine(line, GeometryLine.parse_coordinates(RING)),
        "multipolygon": GeometryMultiPolygon(polygon, GeometryPolygon.parse_coordinates([RING])),
        "collection": GeometryCollection(a, line, polygon),
    }
    return values[kind]


def _sdk(kind: str) -> Any:
    """The SDK twin of ``_orm(kind)``, built with the SDK's own constructors."""
    a, b = SdkPoint(*A), SdkPoint(*B)
    line = SdkLine(a, b)
    polygon = SdkPolygon.parse_coordinates([RING, HOLE])
    values: dict[str, Any] = {
        "point": a,
        "line": line,
        "polygon": polygon,
        "multipoint": SdkMultiPoint(a, b),
        "multiline": SdkMultiLine(line, SdkLine.parse_coordinates(RING)),
        "multipolygon": SdkMultiPolygon(polygon, SdkPolygon.parse_coordinates([RING])),
        "collection": SdkCollection(a, line, polygon),
    }
    return values[kind]


def _coordinates(kind: str) -> Any:
    """The ``parse_coordinates()`` input of ``_orm(kind)`` (six kinds)."""
    values: dict[str, Any] = {
        "point": A,
        "line": [A, B],
        "polygon": [RING, HOLE],
        "multipoint": [A, B],
        "multiline": [[A, B], RING],
        "multipolygon": [[RING, HOLE], [RING]],
    }
    return values[kind]


def _nested_collection() -> GeometryCollection:
    """A collection holding a collection, a polygon with a hole and an empty multi-point."""
    a = GeometryPoint(*A)
    return GeometryCollection(GeometryCollection(a, GeometryLine(a, GeometryPoint(*B))), _orm("polygon"), GeometryMultiPoint())


def _corpus() -> list[Geometry]:
    """Every kind, a nested collection and the empty multi-geometries and collection."""
    return [
        *(_orm(kind) for kind in KINDS),
        _nested_collection(),
        GeometryMultiPoint(),
        GeometryMultiLine(),
        GeometryMultiPolygon(),
        GeometryCollection(),
    ]


def _nest(inner: Any, levels: int, cls: Any = GeometryCollection) -> Any:
    """``inner`` wrapped in ``levels`` collections of ``cls``, built iteratively."""
    value = inner
    for _ in range(levels):
        value = cls(value)
    return value


def _nest_geojson(levels: int) -> dict[str, Any]:
    value: dict[str, Any] = {"type": "Point", "coordinates": [1.0, 2.0]}
    for _ in range(levels):
        value = {"type": "GeometryCollection", "geometries": [value]}
    return value


def _bad_ring() -> Any:
    """An SDK polygon whose ring was mutated, after construction, to 3 closed points."""
    a, b, c = SdkPoint(0.0, 0.0), SdkPoint(1.0, 0.0), SdkPoint(1.0, 1.0)
    polygon = SdkPolygon(SdkLine(a, b, c, a))
    polygon.geometry_lines[0].geometry_points = [a, b, a]
    return polygon


def _bad_line() -> Any:
    """An SDK line mutated, after construction, to 1 point."""
    line = SdkLine(SdkPoint(0.0, 0.0), SdkPoint(1.0, 1.0))
    line.geometry_points = [SdkPoint(0.0, 0.0)]
    return line


def _bad_point() -> Any:
    """An SDK point holding a string (the SDK's dataclass constructor checks nothing)."""
    return SdkPoint("a", 1.0)


class _NoRepr:
    """A member whose ``repr()`` raises (only a value built unchecked can hold one)."""

    def __repr__(self) -> str:
        raise RuntimeError("no repr")


class _Pin(GeometryPoint):
    """A user subclass of an ORM class."""

    __slots__ = ()


def _all_lists(value: Any) -> bool:
    """True when every multi-item attribute of an SDK geometry, at every depth, is a ``list``."""
    for attr in ("geometry_points", "geometry_lines", "geometry_polygons", "geometries"):
        if hasattr(value, attr):
            members = getattr(value, attr)
            return isinstance(members, list) and all(_all_lists(m) for m in members)
    return True


def _all_sdk(value: Any) -> bool:
    """True when the value and every member, at every depth, is an instance of an SDK class."""
    if not isinstance(value, tuple(SDK_CLASSES.values())):
        return False
    for attr in ("geometry_points", "geometry_lines", "geometry_polygons", "geometries"):
        if hasattr(value, attr):
            return all(_all_sdk(m) for m in getattr(value, attr))
    return True


class _Home(BaseModel):
    """A model with a required field and a geometry default (copies and pickle, spec §3.6)."""

    # The copies below hold with or without the geometry classes' own Pydantic schema.
    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    home: GeometryPoint = GeometryPoint(0, 0)


class TestGeometryClasses:
    """AC-3: the seven ORM geometry classes — constructors, components, immutability, equality, repr."""

    # --- coordinates ---------------------------------------------------------------------------

    def test_coordinates_are_stored_as_floats(self) -> None:
        point = GeometryPoint(1, 2)
        assert point.longitude == 1.0
        assert point.latitude == 2.0
        assert type(point.longitude) is float
        assert type(point.latitude) is float

    def test_fraction_accepted(self) -> None:
        point = GeometryPoint(Fraction(1, 2), Fraction(-3, 4))
        assert (point.longitude, point.latitude) == (0.5, -0.75)
        assert type(point.longitude) is float

    @pytest.mark.parametrize("bad", [True, False, "1", None, Decimal("1")], ids=repr)
    def test_coordinate_of_the_wrong_type_is_a_type_error(self, bad: Any) -> None:
        with pytest.raises(TypeError):
            GeometryPoint(bad, 1.0)
        with pytest.raises(TypeError):
            GeometryPoint(1.0, bad)

    def test_decimal_coordinate_message_mentions_float(self) -> None:
        with pytest.raises(TypeError, match=r"float\(\)"):
            GeometryPoint(Decimal("1"), 2.0)

    @pytest.mark.parametrize("bad", [(1.0, 2.0, 3.0), (1.0,), (), 5, "ab"], ids=repr)
    def test_parse_coordinates_needs_two_numbers(self, bad: Any) -> None:
        with pytest.raises(TypeError):
            GeometryPoint.parse_coordinates(bad)

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, 10**400], ids=["nan", "inf", "-inf", "10**400"])
    def test_non_finite_or_too_large_coordinate_is_refused(self, bad: Any) -> None:
        with pytest.raises(InvalidGeometryError):
            GeometryPoint(bad, 1.0)
        with pytest.raises(InvalidGeometryError):
            GeometryPoint(1.0, bad)
        with pytest.raises(InvalidGeometryError):
            GeometryPoint.parse_coordinates([1.0, bad])

    # --- components ----------------------------------------------------------------------------

    def test_sdk_component_is_converted(self) -> None:
        line = GeometryLine(GeometryPoint(*A), SdkPoint(*B))
        assert type(line.geometry_points[1]) is GeometryPoint
        assert line.geometry_points[1] == GeometryPoint(*B)

    def test_sdk_components_at_every_position_are_converted(self) -> None:
        polygon = GeometryPolygon(SdkLine.parse_coordinates(RING), GeometryLine.parse_coordinates(HOLE))
        assert all(type(ring) is GeometryLine for ring in polygon.geometry_lines)
        assert all(type(p) is GeometryPoint for ring in polygon.geometry_lines for p in ring.geometry_points)
        multi = GeometryMultiPolygon(_sdk("polygon"), _orm("polygon"))
        assert all(type(member) is GeometryPolygon for member in multi.geometry_polygons)
        collection = GeometryCollection(SdkPoint(*A), _sdk("collection"))
        assert type(collection.geometries[0]) is GeometryPoint
        assert type(collection.geometries[1]) is GeometryCollection
        assert collection.geometries[1] == _orm("collection")

    def test_wrong_component_names_its_position_and_class(self) -> None:
        with pytest.raises(TypeError, match=r"point 2 .*GeometryPoint"):
            GeometryLine(GeometryPoint(*A), (1.0, 2.0))  # type: ignore[arg-type]
        with pytest.raises(TypeError, match=r"ring 2 .*GeometryLine"):
            GeometryPolygon(GeometryLine.parse_coordinates(RING), GeometryPoint(*A))  # type: ignore[arg-type]
        with pytest.raises(TypeError, match=r"point 1 .*GeometryPoint"):
            GeometryMultiPoint(GeometryLine(GeometryPoint(*A), GeometryPoint(*B)))  # type: ignore[arg-type]
        with pytest.raises(TypeError, match=r"line 1 .*GeometryLine"):
            GeometryMultiLine(SdkPoint(*A))  # type: ignore[arg-type]
        with pytest.raises(TypeError, match=r"polygon 1 .*GeometryPolygon"):
            GeometryMultiPolygon(GeometryLine.parse_coordinates(RING))  # type: ignore[arg-type]

    def test_three_point_ring_is_refused_with_the_sdk_wording(self) -> None:
        a, b = GeometryPoint(0.0, 0.0), GeometryPoint(1.0, 0.0)
        with pytest.raises(InvalidGeometryError) as orm_error:
            GeometryPolygon(GeometryLine(a, b, a))
        assert str(orm_error.value) == "Invalid exterior ring: must have at least 4 points (including closing point), got 3"
        sa, sb = SdkPoint(0.0, 0.0), SdkPoint(1.0, 0.0)
        with pytest.raises(surrealdb.InvalidGeometryError) as sdk_error:
            SdkPolygon(SdkLine(sa, sb, sa))
        assert str(orm_error.value) == str(sdk_error.value)

    @pytest.mark.parametrize("hole", [False, True], ids=["exterior", "hole"])
    def test_open_ring_is_refused_with_the_sdk_wording(self, hole: bool) -> None:
        open_ring = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)]
        closed_ring = [*open_ring, (0.0, 0.0)]
        rings = [closed_ring, open_ring] if hole else [open_ring]
        with pytest.raises(InvalidGeometryError) as orm_error:
            GeometryPolygon.parse_coordinates(rings)
        with pytest.raises(surrealdb.InvalidGeometryError) as sdk_error:
            SdkPolygon.parse_coordinates(rings)
        assert str(orm_error.value) == str(sdk_error.value)
        assert ("interior (hole 1)" if hole else "exterior") in str(orm_error.value)
        assert "to close the ring" in str(orm_error.value)

    def test_empty_multi_geometries_and_collection_are_accepted(self) -> None:
        assert GeometryMultiPoint().geometry_points == ()
        assert GeometryMultiLine().geometry_lines == ()
        assert GeometryMultiPolygon().geometry_polygons == ()
        assert GeometryCollection().geometries == ()

    def test_nested_collection_is_accepted(self) -> None:
        inner = GeometryCollection(GeometryPoint(*A))
        outer = GeometryCollection(inner, _orm("polygon"))
        assert outer.geometries == (inner, _orm("polygon"))

    @pytest.mark.parametrize("bad", [{"type": "Point", "coordinates": [1.0, 2.0]}, (1.0, 2.0), 3, None], ids=repr)
    def test_collection_member_that_is_not_a_geometry_is_a_type_error(self, bad: Any) -> None:
        with pytest.raises(TypeError, match="GeometryCollection"):
            GeometryCollection(GeometryPoint(*A), bad)

    def test_collections_nest_at_most_32_levels(self) -> None:
        point = GeometryPoint(*A)
        deepest = _nest(point, 32)
        assert isinstance(deepest, GeometryCollection)
        with pytest.raises(InvalidGeometryError, match="nests at most 32 levels"):
            GeometryCollection(deepest)
        with pytest.raises(InvalidGeometryError, match="nests at most 32 levels"):
            _nest(point, 33)

    def test_nesting_cap_applies_through_an_sdk_member(self) -> None:
        sdk_32 = _nest(SdkPoint(*A), 32, SdkCollection)
        assert GeometryCollection(_nest(SdkPoint(*A), 31, SdkCollection)) == _nest(GeometryPoint(*A), 32)
        with pytest.raises(InvalidGeometryError, match="nests at most 32 levels"):
            GeometryCollection(sdk_32)
        with pytest.raises(InvalidGeometryError, match="nests at most 32 levels"):
            geo._from_sdk(_nest(SdkPoint(*A), 33, SdkCollection), checked=True)

    def test_checked_conversion_stops_before_descending_a_deep_sdk_collection(self) -> None:
        with pytest.raises(InvalidGeometryError, match="nests at most 32 levels"):
            geo._from_sdk(_nest(SdkPoint(*A), 2000, SdkCollection), checked=True)

    # --- attributes and immutability ------------------------------------------------------------

    @pytest.mark.parametrize("kind", KINDS[1:])
    def test_multi_item_attributes_are_tuples(self, kind: str) -> None:
        value = _orm(kind)
        attr = {
            "line": "geometry_points",
            "polygon": "geometry_lines",
            "multipoint": "geometry_points",
            "multiline": "geometry_lines",
            "multipolygon": "geometry_polygons",
            "collection": "geometries",
        }[kind]
        assert type(getattr(value, attr)) is tuple

    def test_values_are_immutable(self) -> None:
        point = GeometryPoint(1, 2)
        with pytest.raises(AttributeError):
            point.longitude = 3  # type: ignore[misc]
        with pytest.raises(AttributeError):
            del point.latitude
        with pytest.raises(AttributeError):
            point.altitude = 3  # type: ignore[attr-defined]
        line = GeometryLine(point, GeometryPoint(3, 4))
        with pytest.raises(AttributeError):
            line.geometry_points = ()  # type: ignore[misc]
        assert (point.longitude, point.latitude) == (1.0, 2.0)
        assert len(line.geometry_points) == 2

    @pytest.mark.parametrize("kind", KINDS)
    def test_every_class_is_a_geometry(self, kind: str) -> None:
        assert issubclass(ORM_CLASSES[kind], Geometry)
        assert isinstance(_orm(kind), Geometry)
        assert isinstance(GeometryCollection(), Geometry)

    def test_geometry_itself_is_abstract(self) -> None:
        with pytest.raises(TypeError):
            Geometry()

    def test_classes_are_exported(self) -> None:
        for name in ("Geometry", *(cls.__name__ for cls in ORM_CLASSES.values())):
            assert name in surreal_orm_lite.__all__, name
            assert getattr(surreal_orm_lite, name) is getattr(geo, name)

    # --- equality and hashing --------------------------------------------------------------------

    @pytest.mark.parametrize("kind", KINDS)
    def test_equal_to_an_equal_orm_value_and_to_the_sdk_twin(self, kind: str) -> None:
        assert _orm(kind) == _orm(kind)
        assert _orm(kind) == _sdk(kind)
        assert (_orm(kind) != _sdk(kind)) is False
        assert hash(_orm(kind)) == hash(_orm(kind))

    def test_not_equal_to_other_coordinates_or_another_kind(self) -> None:
        point = GeometryPoint(*A)
        assert point != GeometryPoint(*B)
        assert point != SdkPoint(*B)
        assert point != GeometryMultiPoint(point)
        assert point != SdkMultiPoint(SdkPoint(*A))
        assert GeometryMultiPoint(point) != GeometryCollection(point)
        assert GeometryMultiPoint() != GeometryMultiLine()
        assert _orm("polygon") != GeometryPolygon.parse_coordinates([RING])

    def test_comparison_with_another_type_is_false(self) -> None:
        assert (GeometryPoint(1, 2) == "x") is False
        assert (GeometryPoint(1, 2) != "x") is True
        assert (GeometryPoint(1, 2) == (1.0, 2.0)) is False

    def test_hashable_in_a_set(self) -> None:
        assert len({GeometryPoint(1, 2), GeometryPoint(1.0, 2.0)}) == 1
        assert len({*_corpus(), *_corpus()}) == len(_corpus())

    def test_comparison_with_an_unreadable_sdk_value_is_false(self) -> None:
        hollow = object.__new__(SdkPoint)
        assert (GeometryPoint(1, 2) == hollow) is False
        assert (GeometryPoint(1, 2) != hollow) is True

    def test_unchecked_values_compare_by_what_they_hold(self) -> None:
        sdk = SdkMultiPoint()
        sdk.geometry_points = [(1.0, 2.0)]
        first, second = geo._from_sdk(sdk, checked=False), geo._from_sdk(sdk, checked=False)
        assert type(first) is GeometryMultiPoint
        assert first.geometry_points == ((1.0, 2.0),)
        assert first == second
        assert hash(first) == hash(second)
        assert first != GeometryMultiPoint(GeometryPoint(1, 2))

    # --- repr and coordinates --------------------------------------------------------------------

    @pytest.mark.parametrize("kind", KINDS)
    def test_repr_is_the_sdk_format(self, kind: str) -> None:
        assert repr(_orm(kind)) == repr(_sdk(kind))

    def test_repr_of_nested_and_empty_values(self) -> None:
        assert repr(GeometryPoint(-73.98, 40.74)) == "GeometryPoint(longitude=-73.98, latitude=40.74)"
        assert repr(GeometryCollection()) == "GeometryCollection()"
        sdk_nested = SdkCollection(SdkCollection(SdkPoint(*A)), SdkMultiPoint())
        assert repr(GeometryCollection(GeometryCollection(GeometryPoint(*A)), GeometryMultiPoint())) == repr(sdk_nested)

    def test_repr_of_an_unchecked_value_never_raises(self) -> None:
        unchecked = geo._from_sdk(_bad_point(), checked=False)
        assert isinstance(unchecked, GeometryPoint)
        assert repr(unchecked) == repr(_bad_point())
        holder = geo._from_sdk(SdkCollection(_NoRepr()), checked=False)
        assert isinstance(holder, GeometryCollection)
        assert "GeometryCollection" in repr(holder)

    @pytest.mark.parametrize("kind", COORDINATE_KINDS)
    def test_get_coordinates_is_the_sdk_shape(self, kind: str) -> None:
        assert _orm(kind).get_coordinates() == _sdk(kind).get_coordinates()

    def test_collection_coordinates_are_its_members_coordinates(self) -> None:
        collection = _orm("collection")
        assert collection.get_coordinates() == [member.get_coordinates() for member in collection.geometries]
        assert GeometryCollection().get_coordinates() == []

    @pytest.mark.parametrize("kind", COORDINATE_KINDS)
    def test_parse_coordinates_mirrors_the_sdk(self, kind: str) -> None:
        parsed = ORM_CLASSES[kind].parse_coordinates(_coordinates(kind))  # type: ignore[attr-defined]
        assert type(parsed) is ORM_CLASSES[kind]
        assert parsed == SDK_CLASSES[kind].parse_coordinates(_coordinates(kind))
        assert parsed == _orm(kind)

    # --- SDK conversion ----------------------------------------------------------------------------

    @pytest.mark.parametrize("kind", KINDS)
    def test_to_sdk_returns_the_sdk_twin(self, kind: str) -> None:
        sdk = _orm(kind)._to_sdk()
        assert type(sdk) is SDK_CLASSES[kind]
        assert _sdk(kind) == sdk
        assert _all_sdk(sdk)
        assert _all_lists(sdk)

    def test_to_sdk_of_nested_and_empty_values(self) -> None:
        for value in _corpus():
            sdk = value._to_sdk()
            assert value == sdk
            assert _all_sdk(sdk)
            assert _all_lists(sdk)

    @pytest.mark.parametrize("kind", KINDS)
    def test_checked_from_sdk_returns_the_orm_twin(self, kind: str) -> None:
        converted = geo._from_sdk(_sdk(kind), checked=True)
        assert type(converted) is ORM_CLASSES[kind]
        assert converted == _orm(kind)

    def test_checked_from_sdk_converts_members(self) -> None:
        converted = geo._from_sdk(_sdk("collection"), checked=True)
        assert isinstance(converted, GeometryCollection)
        point, line, polygon = converted.geometries
        assert type(point) is GeometryPoint
        assert type(line) is GeometryLine and all(type(p) is GeometryPoint for p in line.geometry_points)
        assert type(polygon) is GeometryPolygon and all(type(r) is GeometryLine for r in polygon.geometry_lines)

    def test_checked_from_sdk_refuses_malformed_values(self) -> None:
        with pytest.raises(InvalidGeometryError):
            geo._from_sdk(_bad_ring(), checked=True)
        with pytest.raises(InvalidGeometryError):
            geo._from_sdk(_bad_line(), checked=True)
        with pytest.raises(TypeError):
            geo._from_sdk(_bad_point(), checked=True)
        with pytest.raises(TypeError):
            geo._from_sdk(SdkPoint(Decimal("1"), 2.0), checked=True)
        no_ring = SdkPolygon.parse_coordinates([RING])
        no_ring.geometry_lines = []
        with pytest.raises(InvalidGeometryError, match="at least 1 ring"):
            geo._from_sdk(no_ring, checked=True)
        not_a_list = SdkLine(SdkPoint(*A), SdkPoint(*B))
        not_a_list.geometry_points = None
        with pytest.raises(TypeError, match="must be a list"):
            geo._from_sdk(not_a_list, checked=True)
        with pytest.raises(TypeError, match="not an SDK geometry"):
            geo._from_sdk({"type": "Point", "coordinates": [1.0, 2.0]}, checked=True)

    def test_unchecked_from_sdk_keeps_what_it_finds(self) -> None:
        unchecked = geo._from_sdk(_bad_point(), checked=False)
        assert type(unchecked) is GeometryPoint
        assert unchecked.longitude == "a"
        ring = geo._from_sdk(_bad_ring(), checked=False)
        assert type(ring) is GeometryPolygon
        assert len(ring.geometry_lines[0].geometry_points) == 3
        assert type(ring.geometry_lines[0]) is GeometryLine
        orm_point = GeometryPoint(*A)
        holder = SdkMultiPoint(SdkPoint(*B))
        holder.geometry_points.append(orm_point)
        converted = geo._from_sdk(holder, checked=False)
        assert type(converted.geometry_points[0]) is GeometryPoint
        assert converted.geometry_points[1] is orm_point

    def test_unchecked_from_sdk_returns_what_it_cannot_read(self) -> None:
        hollow = object.__new__(SdkPoint)
        assert geo._from_sdk(hollow, checked=False) is hollow
        plain = {"type": "Point", "coordinates": [1.0, 2.0]}
        assert geo._from_sdk(plain, checked=False) is plain
        deep = _nest(SdkPoint(*A), 2000, SdkCollection)
        geo._from_sdk(deep, checked=False)


class TestGeometryCopies:
    """AC-3 / D9: copies are the instance itself; pickle and Pydantic copies of defaults work."""

    @pytest.mark.parametrize("value", _corpus(), ids=repr)
    def test_copy_and_deepcopy_return_the_instance(self, value: Geometry) -> None:
        assert copy.copy(value) is value
        assert copy.deepcopy(value) is value
        assert copy.deepcopy({"v": [value]})["v"][0] is value

    @pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
    def test_pickle_round_trips_every_kind(self, protocol: int) -> None:
        for value in _corpus():
            restored = pickle.loads(pickle.dumps(value, protocol))
            assert type(restored) is type(value)
            assert restored == value

    @pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
    def test_pickle_keeps_the_nesting_cap(self, protocol: int) -> None:
        restored = pickle.loads(pickle.dumps(_nest(GeometryPoint(*A), 32), protocol))
        with pytest.raises(InvalidGeometryError, match="nests at most 32 levels"):
            GeometryCollection(restored)

    @pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
    def test_pickle_of_an_unchecked_value_runs_no_constructor(self, protocol: int) -> None:
        unchecked = geo._from_sdk(_bad_point(), checked=False)
        restored = pickle.loads(pickle.dumps(unchecked, protocol))
        assert type(restored) is GeometryPoint
        assert (restored.longitude, restored.latitude) == ("a", 1.0)

    @pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
    def test_pickle_of_an_unchecked_collection_keeps_its_levels(self, protocol: int) -> None:
        unchecked = geo._from_sdk(_nest(SdkPoint(*A), 32, SdkCollection), checked=False)
        assert type(unchecked) is GeometryCollection
        restored = pickle.loads(pickle.dumps(unchecked, protocol))
        assert restored == _nest(GeometryPoint(*A), 32)
        with pytest.raises(InvalidGeometryError, match="nests at most 32 levels"):
            GeometryCollection(restored)

    def test_restored_values_stay_immutable(self) -> None:
        restored = pickle.loads(pickle.dumps(GeometryPoint(1, 2)))
        with pytest.raises(AttributeError):
            restored.longitude = 3  # type: ignore[misc]

    def test_pydantic_copies_of_a_geometry_default(self) -> None:
        point = GeometryPoint(*A)
        assert _Home(name="x").home == GeometryPoint(0, 0)
        assert _Home.model_construct(name="x").home == GeometryPoint(0, 0)
        assert _Home.model_fields["home"].get_default(call_default_factory=True) == GeometryPoint(0, 0)
        assert _Home(name="x", home=point).model_copy(deep=True).home is point
        assert copy.deepcopy(_Home(name="x", home=point)).home == point
        restored = pickle.loads(pickle.dumps(_Home(name="x", home=point)))
        assert restored.home == point
        assert type(restored.home) is GeometryPoint


GEOJSON: dict[str, dict[str, Any]] = {
    "point": {"type": "Point", "coordinates": [-73.98, 40.74]},
    "line": {"type": "LineString", "coordinates": [[-73.98, 40.74], [-73.99, 40.75]]},
    "polygon": {"type": "Polygon", "coordinates": [[list(p) for p in RING], [list(p) for p in HOLE]]},
    "multipoint": {"type": "MultiPoint", "coordinates": [[-73.98, 40.74], [-73.99, 40.75]]},
    "multiline": {
        "type": "MultiLineString",
        "coordinates": [[[-73.98, 40.74], [-73.99, 40.75]], [list(p) for p in RING]],
    },
    "multipolygon": {
        "type": "MultiPolygon",
        "coordinates": [[[list(p) for p in RING], [list(p) for p in HOLE]], [[list(p) for p in RING]]],
    },
    "collection": {
        "type": "GeometryCollection",
        "geometries": [
            {"type": "Point", "coordinates": [-73.98, 40.74]},
            {"type": "LineString", "coordinates": [[-73.98, 40.74], [-73.99, 40.75]]},
            {"type": "Polygon", "coordinates": [[list(p) for p in RING], [list(p) for p in HOLE]]},
        ],
    },
}


class TestGeoJSON:
    """AC-3: ``to_geojson()`` / ``from_geojson()`` and ``__geo_interface__``."""

    @pytest.mark.parametrize("kind", KINDS)
    def test_to_geojson_exact(self, kind: str) -> None:
        assert _orm(kind).to_geojson() == GEOJSON[kind]

    def test_to_geojson_of_empty_values(self) -> None:
        assert GeometryCollection().to_geojson() == {"type": "GeometryCollection", "geometries": []}
        assert GeometryMultiPoint().to_geojson() == {"type": "MultiPoint", "coordinates": []}
        assert GeometryMultiLine().to_geojson() == {"type": "MultiLineString", "coordinates": []}
        assert GeometryMultiPolygon().to_geojson() == {"type": "MultiPolygon", "coordinates": []}

    @pytest.mark.parametrize("value", _corpus(), ids=repr)
    def test_round_trip(self, value: Geometry) -> None:
        through_base = Geometry.from_geojson(value.to_geojson())
        assert type(through_base) is type(value)
        assert through_base == value
        through_class = type(value).from_geojson(value.to_geojson())
        assert type(through_class) is type(value)
        assert through_class == value

    def test_round_trip_through_tuples(self) -> None:
        assert Geometry.from_geojson({"type": "LineString", "coordinates": (A, B)}) == _orm("line")

    def test_subclass_requires_its_own_type(self) -> None:
        with pytest.raises(InvalidGeometryError, match="Polygon"):
            GeometryPoint.from_geojson(GEOJSON["polygon"])
        with pytest.raises(InvalidGeometryError):
            GeometryCollection.from_geojson(GEOJSON["point"])

    def test_a_subclass_of_an_orm_class_is_not_built_from_geojson(self) -> None:
        assert _Pin(1, 2) == GeometryPoint(1, 2)
        with pytest.raises(InvalidGeometryError, match="builds a GeometryPoint, not a _Pin"):
            _Pin.from_geojson({"type": "Point", "coordinates": [1.0, 2.0]})

    @pytest.mark.parametrize(
        "bad",
        [
            [1.0, 2.0],
            "Point",
            None,
            {"coordinates": [1.0, 2.0]},
            {"type": "Circle", "coordinates": [1.0, 2.0]},
            {"type": 3, "coordinates": [1.0, 2.0]},
            {"type": "Point"},
            {"type": "GeometryCollection"},
            {"type": "GeometryCollection", "geometries": "x"},
            {"type": "Point", "coordinates": [1.0, 2.0, 3.0]},
            {"type": "Point", "coordinates": [1.0]},
            {"type": "Point", "coordinates": ["x", 2.0]},
            {"type": "Point", "coordinates": [True, 2.0]},
            {"type": "Point", "coordinates": [math.nan, 2.0]},
            {"type": "Point", "coordinates": [math.inf, 2.0]},
            {"type": "Point", "coordinates": "12"},
            {"type": "LineString", "coordinates": [[1.0, 2.0]]},
            {"type": "LineString", "coordinates": [1.0, 2.0]},
            {"type": "Polygon", "coordinates": []},
            {"type": "Polygon", "coordinates": [[[0.0, 0.0], [1.0, 0.0], [0.0, 0.0]]]},
            {"type": "Polygon", "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]]},
            {"type": "MultiPoint", "coordinates": [[1.0, 2.0], [1.0, 2.0, 3.0]]},
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [1.0, 2.0]}, "properties": {}},
            {"type": "GeometryCollection", "geometries": [{"type": "Point"}]},
            {"type": "GeometryCollection", "geometries": [[1.0, 2.0]]},
        ],
        ids=repr,
    )
    def test_malformed_geojson_is_refused(self, bad: Any) -> None:
        with pytest.raises(InvalidGeometryError):
            Geometry.from_geojson(bad)

    def test_collections_nest_at_most_32_levels(self) -> None:
        assert Geometry.from_geojson(_nest_geojson(32)) == _nest(GeometryPoint(1.0, 2.0), 32)
        with pytest.raises(InvalidGeometryError, match="nests at most 32 levels"):
            Geometry.from_geojson(_nest_geojson(33))
        with pytest.raises(InvalidGeometryError, match="nests at most 32 levels"):
            Geometry.from_geojson(_nest_geojson(2000))

    def test_unknown_keys_are_ignored(self) -> None:
        data = {"type": "Point", "coordinates": [-73.98, 40.74], "bbox": [-74.0, 40.0, -73.0, 41.0]}
        assert Geometry.from_geojson(data) == GeometryPoint(*A)

    @pytest.mark.parametrize("value", _corpus(), ids=repr)
    def test_geo_interface(self, value: Geometry) -> None:
        assert value.__geo_interface__ == value.to_geojson()


class TestToSdkNeverValidates:
    """P4: ``_to_sdk()`` builds the SDK value without any SDK constructor and never raises."""

    @pytest.mark.parametrize("make", [_bad_ring, _bad_line, _bad_point], ids=["ring", "line", "point"])
    def test_unchecked_malformed_value_converts_without_raising(self, make: Any) -> None:
        bad = make()
        unchecked = geo._from_sdk(bad, checked=False)
        assert isinstance(unchecked, Geometry)
        sdk = unchecked._to_sdk()
        assert type(sdk) is type(bad)

    def test_unchecked_malformed_values_keep_their_shape(self) -> None:
        ring = geo._from_sdk(_bad_ring(), checked=False)._to_sdk()
        assert len(ring.geometry_lines[0].geometry_points) == 3
        line = geo._from_sdk(_bad_line(), checked=False)._to_sdk()
        assert len(line.geometry_points) == 1
        point = geo._from_sdk(_bad_point(), checked=False)._to_sdk()
        assert (point.longitude, point.latitude) == ("a", 1.0)

    def test_no_sdk_constructor_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        values = [*_corpus(), GeometryCollection(_orm("multiline"), _orm("multipolygon"), _orm("multipoint"))]
        twins = [value._to_sdk() for value in values]

        def _refuse(self: Any, *args: Any, **kwargs: Any) -> None:
            raise AssertionError(f"{type(self).__name__}.__init__ called")

        for cls in SDK_CLASSES.values():
            monkeypatch.setattr(cls, "__init__", _refuse)
        for value, twin in zip(values, twins, strict=True):
            sdk = value._to_sdk()
            assert sdk == twin
            assert _all_sdk(sdk)


# --- Pydantic integration (Task 3) ---------------------------------------------------------------

#: Each alias and the one kind it accepts.
ALIASES: dict[str, Any] = {
    "point": PointField,
    "line": LineStringField,
    "polygon": PolygonField,
    "multipoint": MultiPointField,
    "multiline": MultiLineStringField,
    "multipolygon": MultiPolygonField,
    "collection": GeometryCollectionField,
}
#: A plain Pydantic model and an ORM model that is never saved: geometry fields behave the same.
BASES = [pytest.param(BaseModel, id="plain"), pytest.param(BaseSurrealModel, id="surreal")]


def _ids(base: type[BaseModel]) -> dict[str, Any]:
    """The ``id`` field an ORM model needs (a plain model has none)."""
    return {"id": (str | None, None)} if issubclass(base, BaseSurrealModel) else {}


def _model(base: type[BaseModel], annotation: Any) -> Any:
    """A model of ``base`` with the field ``loc: annotation`` (no default written in the body)."""
    return create_model("GeoUnit", __base__=base, loc=annotation, **_ids(base))


def _open_polygon_geojson() -> dict[str, Any]:
    """A GeoJSON polygon whose 4-point ring is not closed."""
    return {"type": "Polygon", "coordinates": [[list(p) for p in RING[:-1]]]}


class _GeoSub(BaseModel):
    """A sub-model with a geometry field."""

    where: GeometryPoint


def _dumped_model(base: type[BaseModel]) -> Any:
    """Geometry fields at every position ``model_dump()`` writes: top level, containers, sub-model, aliases."""
    return create_model(
        "GeoDumped",
        __base__=base,
        loc=GeometryPoint,
        many=(list[GeometryPoint], []),
        areas=(dict[str, GeometryPolygon], {}),
        sub=(_GeoSub | None, None),
        area=PolygonField,
        bag=GeometryCollectionField,
        **_ids(base),
    )


class _PlainDefaults(BaseModel):
    """D13: alias fields with no default written in the body, one of them aliased."""

    location: PointField
    aliased: PointField = Field(alias="where")


class _SurrealDefaults(BaseSurrealModel):
    """D13 on an ORM model."""

    model_config = SurrealConfigDict(table_name="geo_unit")

    id: str | None = None
    location: PointField
    aliased: PointField = Field(alias="where")


class _PointOrText(BaseModel):
    """A union whose other member is not a geometry."""

    loc: GeometryPoint | str


class _GeoAuth(AuthenticatedUserMixin, BaseSurrealModel):
    """An auth model omitting a ``PointField`` field and with a geometry default (spec §7)."""

    model_config = SurrealConfigDict(table_name="geo_auth", access_name="geo_auth")

    id: str | None = None
    email: str
    password: str
    home: PointField
    door: GeometryPoint = GeometryPoint(0, 0)


class _GeoLive(BaseSurrealModel):
    """Every field has a default: the live fallbacks build it from any row."""

    model_config = SurrealConfigDict(table_name="geo_live")

    id: str | None = None
    location: PointField
    area: PolygonField
    n: int = 0


#: Annotations the helpers know, with the kinds of each (``get_geo_info`` joins them with ``|``,
#: or answers ``"feature"`` for all seven).
GEO_ANNOTATIONS: list[Any] = [
    pytest.param(PointField, "point", id="PointField"),
    pytest.param(LineStringField, "line", id="LineStringField"),
    pytest.param(PolygonField, "polygon", id="PolygonField"),
    pytest.param(MultiPointField, "multipoint", id="MultiPointField"),
    pytest.param(MultiLineStringField, "multiline", id="MultiLineStringField"),
    pytest.param(MultiPolygonField, "multipolygon", id="MultiPolygonField"),
    pytest.param(GeometryCollectionField, "collection", id="GeometryCollectionField"),
    pytest.param(GeometryField, "feature", id="GeometryField"),
    pytest.param(GeoField["Point|POLYGON"], "point|polygon", id="GeoField-union"),
    pytest.param(GeoField["polygon|point"], "point|polygon", id="GeoField-union-order"),
    pytest.param(GeoField["linestring"], "line", id="GeoField-linestring"),
    pytest.param(GeoField["MultiLineString"], "multiline", id="GeoField-multilinestring"),
    pytest.param(GeoField["geometrycollection"], "collection", id="GeoField-geometrycollection"),
    pytest.param(GeoField["geometry"], "feature", id="GeoField-geometry"),
    pytest.param(GeoField["point|line|polygon|multipoint|multiline|multipolygon|collection"], "feature", id="GeoField-all"),
    pytest.param(GeometryPoint, "point", id="GeometryPoint"),
    pytest.param(GeometryPoint | None, "point", id="GeometryPoint-or-None"),
    pytest.param(Optional[GeometryPolygon], "polygon", id="Optional-GeometryPolygon"),  # noqa: UP045
    pytest.param(GeometryPoint | GeometryPolygon, "point|polygon", id="class-union"),
    pytest.param(GeometryCollection, "collection", id="GeometryCollection"),
    pytest.param(Geometry, "feature", id="Geometry"),
    pytest.param(Geometry | None, "feature", id="Geometry-or-None"),
    pytest.param(Annotated[GeometryPoint, "doc"], "point", id="Annotated-without-marker"),
    pytest.param(Annotated[GeometryLine | None, "doc"], "line", id="Annotated-optional-without-marker"),
]
#: Annotations that are not geometry fields.
NOT_GEO_ANNOTATIONS: list[Any] = [
    pytest.param(str, id="str"),
    pytest.param(dict, id="dict"),
    pytest.param(dict[str, Any], id="dict-str-any"),
    pytest.param(Any, id="Any"),
    pytest.param(None, id="None"),
    pytest.param(int | None, id="int-or-None"),
    pytest.param(GeometryPoint | str, id="point-or-str"),
    pytest.param(list[GeometryPoint], id="list-of-points"),
    pytest.param(SdkPoint, id="sdk-point"),
    pytest.param(SdkPoint | None, id="sdk-point-or-None"),
    pytest.param(Annotated[str, "doc"], id="Annotated-str"),
    pytest.param(type(None), id="NoneType"),
]


def _kinds(info: str) -> frozenset[str]:
    return frozenset(KINDS) if info == "feature" else frozenset(info.split("|"))


class TestGeometryFields:
    """AC-4: class annotations, ``GeoField[...]`` and the aliases as Pydantic fields."""

    # --- accepted input -----------------------------------------------------------------------

    @pytest.mark.parametrize("base", BASES)
    @pytest.mark.parametrize("kind", KINDS)
    def test_class_annotation_accepts_every_input_form(self, base: type[BaseModel], kind: str) -> None:
        model = _model(base, ORM_CLASSES[kind])
        orm = _orm(kind)
        assert model(loc=orm).loc is orm
        for value in (_sdk(kind), GEOJSON[kind]):
            loc = model(loc=value).loc
            assert type(loc) is ORM_CLASSES[kind]
            assert loc == orm

    @pytest.mark.parametrize("base", BASES)
    @pytest.mark.parametrize("pair", [A, list(A)], ids=["tuple", "list"])
    def test_point_field_accepts_a_longitude_latitude_pair(self, base: type[BaseModel], pair: Any) -> None:
        loc = _model(base, GeometryPoint)(loc=pair).loc
        assert type(loc) is GeometryPoint
        assert loc == GeometryPoint(*A)

    # --- refused input ------------------------------------------------------------------------

    @pytest.mark.parametrize("base", BASES)
    @pytest.mark.parametrize(
        "bad",
        [
            pytest.param(_orm("line"), id="another-kind"),
            pytest.param(_sdk("line"), id="another-kind-sdk"),
            pytest.param(GEOJSON["line"], id="another-kind-geojson"),
            pytest.param("text", id="text"),
            pytest.param((1, 2, 3), id="three-numbers"),
            pytest.param((1,), id="one-number"),
            pytest.param((True, 1), id="bool"),
            pytest.param(("1", "2"), id="strings"),
            pytest.param({"type": "Point", "coordinates": [math.nan, 1]}, id="nan-geojson"),
            pytest.param({"type": "Feature", "geometry": None}, id="feature-geojson"),
            pytest.param(SdkPoint(Decimal("1"), 1.0), id="sdk-decimal"),
            pytest.param(None, id="none-on-a-required-field"),
            pytest.param(42, id="int"),
        ],
    )
    def test_point_field_refusals_are_validation_errors(self, base: type[BaseModel], bad: Any) -> None:
        with pytest.raises(ValidationError):
            _model(base, GeometryPoint)(loc=bad)

    @pytest.mark.parametrize("base", BASES)
    def test_polygon_field_refuses_an_open_ring(self, base: type[BaseModel]) -> None:
        with pytest.raises(ValidationError, match="must equal last point"):
            _model(base, GeometryPolygon)(loc=_open_polygon_geojson())

    @pytest.mark.parametrize("base", BASES)
    @pytest.mark.parametrize("kind", ["line", "multipoint", "polygon"])
    def test_only_point_fields_take_a_bare_sequence(self, base: type[BaseModel], kind: str) -> None:
        with pytest.raises(ValidationError):
            _model(base, ORM_CLASSES[kind])(loc=[A, B])

    def test_refusal_message_names_the_accepted_forms(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _model(BaseModel, GeometryPoint)(loc="text")
        message = str(excinfo.value)
        assert "GeometryPoint" in message
        assert "GeoJSON" in message
        assert "(longitude, latitude)" in message

    # --- GeoField and the aliases -------------------------------------------------------------

    @pytest.mark.parametrize("base", BASES)
    @pytest.mark.parametrize("spelling", ["point", "Point", "POINT", " point "])
    def test_geofield_point_accepts_points_only(self, base: type[BaseModel], spelling: str) -> None:
        model = _model(base, GeoField[spelling])
        point = _orm("point")
        assert model(loc=point).loc is point
        assert model(loc=A).loc == point
        assert model(loc=None).loc is None
        with pytest.raises(ValidationError):
            model(loc=_orm("line"))

    @pytest.mark.parametrize("base", BASES)
    @pytest.mark.parametrize("spelling", ["linestring", "LineString", "line"])
    def test_geofield_linestring_is_line(self, base: type[BaseModel], spelling: str) -> None:
        model = _model(base, GeoField[spelling])
        line = _orm("line")
        assert model(loc=line).loc is line
        assert model(loc=GEOJSON["line"]).loc == line
        assert geo.get_geo_info(GeoField[spelling]) == "line"
        with pytest.raises(ValidationError):
            model(loc=_orm("point"))

    @pytest.mark.parametrize("base", BASES)
    def test_geofield_union_of_kinds(self, base: type[BaseModel]) -> None:
        model = _model(base, GeoField["point|polygon"])
        point, polygon = _orm("point"), _orm("polygon")
        assert model(loc=point).loc is point
        assert model(loc=A).loc == point
        assert model(loc=GEOJSON["polygon"]).loc == polygon
        assert type(model(loc=_sdk("polygon")).loc) is GeometryPolygon
        for bad in (_orm("line"), GEOJSON["line"], _sdk("line")):
            with pytest.raises(ValidationError):
                model(loc=bad)

    @pytest.mark.parametrize("base", BASES)
    @pytest.mark.parametrize(
        "annotation",
        [
            pytest.param(GeoField["feature"], id="feature"),
            pytest.param(GeoField["geometry"], id="geometry"),
            pytest.param(GeometryField, id="GeometryField"),
            pytest.param(Geometry, id="Geometry"),
        ],
    )
    def test_any_kind_fields_accept_all_seven(self, base: type[BaseModel], annotation: Any) -> None:
        model = _model(base, annotation)
        for kind in KINDS:
            orm = _orm(kind)
            assert model(loc=orm).loc is orm
            for value in (_sdk(kind), GEOJSON[kind]):
                loc = model(loc=value).loc
                assert type(loc) is ORM_CLASSES[kind]
                assert loc == orm
        with pytest.raises(ValidationError):
            model(loc="text")

    @pytest.mark.parametrize("base", BASES)
    @pytest.mark.parametrize("kind", KINDS)
    def test_each_alias_accepts_its_kind(self, base: type[BaseModel], kind: str) -> None:
        model = _model(base, ALIASES[kind])
        orm = _orm(kind)
        assert model(loc=orm).loc is orm
        assert model(loc=_sdk(kind)).loc == orm
        assert model(loc=GEOJSON[kind]).loc == orm
        assert model().loc is None
        other = _orm("point" if kind != "point" else "line")
        with pytest.raises(ValidationError):
            model(loc=other)

    @pytest.mark.parametrize("bad", ["foo", "", "point|", "|point", "point|foo", "point||line", "Feature|x"])
    def test_unknown_kind_is_a_value_error(self, bad: str) -> None:
        with pytest.raises(ValueError, match="kind"):
            GeoField[bad]

    @pytest.mark.parametrize("bad", [3, None, ("point",), GeometryPoint])
    def test_non_string_kinds_are_a_type_error(self, bad: Any) -> None:
        with pytest.raises(TypeError, match="GeoField"):
            GeoField[bad]

    def test_unknown_kind_raises_at_class_definition(self) -> None:
        kind = "foo"
        with pytest.raises(ValueError, match="kind"):

            class Bad(BaseModel):
                loc: GeoField[kind]  # type: ignore[valid-type]

    def test_geofield_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError, match="GeoField"):
            GeoField()

    # --- defaults (D13) -----------------------------------------------------------------------

    @pytest.mark.parametrize("model", [_PlainDefaults, _SurrealDefaults], ids=["plain", "surreal"])
    def test_alias_fields_default_to_none(self, model: Any) -> None:
        instance = model()
        assert instance.location is None
        assert instance.aliased is None
        for name in ("location", "aliased"):
            field = model.model_fields[name]
            assert field.is_required() is False
            assert field.default is None
            assert field.get_default(call_default_factory=True) is None
        constructed = model.model_construct()
        assert constructed.location is None
        assert constructed.aliased is None
        assert set(model.model_json_schema().get("required", [])).isdisjoint({"location", "aliased", "where"})
        assert model(where=A).aliased == GeometryPoint(*A)

    # --- nested positions ---------------------------------------------------------------------

    @pytest.mark.parametrize("base", BASES)
    def test_containers_of_classes_validate_to_orm_values(self, base: type[BaseModel]) -> None:
        model = create_model(
            "GeoNested", __base__=base, many=(list[GeometryPoint], []), areas=(dict[str, GeometryPolygon], {}), **_ids(base)
        )
        point, polygon = _orm("point"), _orm("polygon")
        instance = model(
            many=[A, list(A), GEOJSON["point"], _sdk("point"), point],
            areas={"geojson": GEOJSON["polygon"], "sdk": _sdk("polygon"), "orm": polygon},
        )
        assert [type(p) for p in instance.many] == [GeometryPoint] * 5
        assert instance.many == [point] * 5
        assert {name: type(p) for name, p in instance.areas.items()} == dict.fromkeys(
            ("geojson", "sdk", "orm"), GeometryPolygon
        )
        assert all(p == polygon for p in instance.areas.values())
        assert model().many == []

    def test_aliases_inside_a_container_validate_and_dump(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")

            class Loose(BaseModel):
                many: list[PointField] = []

        instance = Loose(many=[A, _sdk("point"), GEOJSON["point"]])
        assert [type(p) for p in instance.many] == [GeometryPoint] * 3
        dumped = instance.model_dump()["many"]
        assert [type(p) for p in dumped] == [GeometryPoint] * 3
        assert dumped == [GeometryPoint(*A)] * 3
        assert instance.model_dump(mode="json")["many"] == [GEOJSON["point"]] * 3

    # --- serialisation ------------------------------------------------------------------------

    @pytest.mark.parametrize("base", BASES)
    def test_python_dump_returns_orm_values_everywhere(self, base: type[BaseModel]) -> None:
        model = _dumped_model(base)
        instance = model(
            loc=A,
            many=[A, B],
            areas={"x": GEOJSON["polygon"]},
            sub=_GeoSub(where=NEAR),
            area=_sdk("polygon"),
            bag=_orm("collection"),
        )
        dumped = instance.model_dump()
        assert type(dumped["loc"]) is GeometryPoint
        assert dumped["loc"] == GeometryPoint(*A)
        assert [type(p) for p in dumped["many"]] == [GeometryPoint, GeometryPoint]
        assert type(dumped["areas"]["x"]) is GeometryPolygon
        assert type(dumped["sub"]["where"]) is GeometryPoint
        assert dumped["sub"]["where"] == GeometryPoint(*NEAR)
        assert type(dumped["area"]) is GeometryPolygon
        assert type(dumped["bag"]) is GeometryCollection
        assert dumped["bag"] == _orm("collection")

    @pytest.mark.parametrize("base", BASES)
    def test_python_dump_converts_raw_sdk_values_without_warning(self, base: type[BaseModel]) -> None:
        model = _dumped_model(base)
        constructed = model.model_construct(
            loc=_sdk("point"), many=[_sdk("point"), _sdk("point")], area=_sdk("polygon"), bag=_sdk("collection")
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            dumped = constructed.model_dump()
            as_json = constructed.model_dump(mode="json")
        assert type(dumped["loc"]) is GeometryPoint
        assert dumped["loc"] == _orm("point")
        assert [type(p) for p in dumped["many"]] == [GeometryPoint, GeometryPoint]
        assert type(dumped["area"]) is GeometryPolygon
        assert type(dumped["bag"]) is GeometryCollection
        assert dumped["bag"] == _orm("collection")
        assert as_json["loc"] == GEOJSON["point"]
        assert as_json["bag"] == GEOJSON["collection"]

    def test_python_dump_of_a_malformed_sdk_value_never_raises(self) -> None:
        constructed = _dumped_model(BaseModel).model_construct(loc=_bad_point(), area=_bad_ring())
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            dumped = constructed.model_dump()
        assert type(dumped["loc"]) is GeometryPoint
        assert dumped["loc"].longitude == "a"
        assert type(dumped["area"]) is GeometryPolygon

    @pytest.mark.parametrize("base", BASES)
    def test_json_dump_writes_geojson_and_reads_it_back(self, base: type[BaseModel]) -> None:
        model = _dumped_model(base)
        instance = model(
            loc=A,
            many=[A, B],
            areas={"x": GEOJSON["polygon"]},
            sub=_GeoSub(where=NEAR),
            area=_orm("polygon"),
            bag=_orm("collection"),
        )
        dumped = instance.model_dump(mode="json")
        assert dumped["loc"] == GEOJSON["point"]
        assert dumped["many"] == [GeometryPoint(*A).to_geojson(), GeometryPoint(*B).to_geojson()]
        assert dumped["areas"] == {"x": GEOJSON["polygon"]}
        assert dumped["sub"] == {"where": GeometryPoint(*NEAR).to_geojson()}
        assert dumped["area"] == GEOJSON["polygon"]
        assert dumped["bag"] == GEOJSON["collection"]
        restored = model.model_validate_json(instance.model_dump_json())
        assert restored == instance
        assert type(restored.bag) is GeometryCollection
        assert model.model_validate_json(model(loc=A).model_dump_json()).area is None

    @pytest.mark.parametrize("base", BASES)
    def test_json_schema(self, base: type[BaseModel]) -> None:
        schema = _dumped_model(base).model_json_schema()
        loc = schema["properties"]["loc"]
        assert loc["title"] == "GeoJSON Point"
        assert loc["type"] == "object"
        assert loc["properties"]["type"] == {"enum": ["Point"]}
        assert loc["required"] == ["type", "coordinates"]
        assert "loc" in schema["required"]
        assert {"area", "bag"}.isdisjoint(schema["required"])
        area_choices = schema["properties"]["area"]["anyOf"]
        assert {"type": "null"} in area_choices
        assert any(choice.get("title") == "GeoJSON Polygon" for choice in area_choices)
        bag = next(choice for choice in schema["properties"]["bag"]["anyOf"] if choice.get("type") == "object")
        assert bag["required"] == ["type", "geometries"]

    def test_json_schema_of_several_kinds_lists_them(self) -> None:
        schema = _model(BaseModel, GeoField["polygon|point"]).model_json_schema()["properties"]["loc"]
        geojson = next(choice for choice in schema["anyOf"] if choice.get("type") == "object")
        assert geojson["properties"]["type"] == {"enum": ["Point", "Polygon"]}
        schema = _model(BaseModel, Geometry).model_json_schema()["properties"]["loc"]
        assert schema["properties"]["type"]["enum"] == [ORM_CLASSES[kind]._GEOJSON_TYPE for kind in KINDS]
        assert schema["required"] == ["type"]

    def test_union_with_a_non_geometry_dumps_each_member_as_itself(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            text = _PointOrText(loc="x")
            point = _PointOrText(loc=A)
            assert text.loc == "x"
            assert text.model_dump() == {"loc": "x"}
            assert text.model_dump(mode="json") == {"loc": "x"}
            assert type(point.model_dump()["loc"]) is GeometryPoint
            assert point.model_dump(mode="json") == {"loc": GEOJSON["point"]}

    # --- helpers --------------------------------------------------------------------------------

    @pytest.mark.parametrize(("annotation", "info"), GEO_ANNOTATIONS)
    def test_geo_helpers_on_geometry_annotations(self, annotation: Any, info: str) -> None:
        assert geo.is_geo_field(annotation) is True
        assert geo.get_geo_info(annotation) == info
        assert geo._geo_kinds(annotation) == _kinds(info)

    @pytest.mark.parametrize("annotation", NOT_GEO_ANNOTATIONS)
    def test_geo_helpers_on_other_annotations(self, annotation: Any) -> None:
        assert geo.is_geo_field(annotation) is False
        assert geo.get_geo_info(annotation) is None
        assert geo._geo_kinds(annotation) is None

    def test_geo_kinds_of_model_field_annotations(self) -> None:
        fields = _dumped_model(BaseSurrealModel).model_fields
        assert geo._geo_kinds(fields["loc"].annotation) == frozenset({"point"})
        assert geo._geo_kinds(fields["area"].annotation) == frozenset({"polygon"})
        assert geo._geo_kinds(fields["bag"].annotation) == frozenset({"collection"})
        assert geo._geo_kinds(fields["many"].annotation) is None
        assert geo._geo_kinds(_SurrealDefaults.model_fields["aliased"].annotation) == frozenset({"point"})
        union = _model(BaseModel, GeoField["point|polygon"]).model_fields["loc"].annotation
        assert geo.get_geo_info(union) == "point|polygon"

    def test_field_info_shows_the_kinds(self) -> None:
        assert "GeoField['point|polygon']" in repr(_model(BaseModel, GeoField["polygon|point"]).model_fields["loc"])
        assert "GeoField['feature']" in repr(_model(BaseModel, GeometryField).model_fields["loc"])

    def test_exports(self) -> None:
        for name in (
            "GeoField",
            "GeometryField",
            "PointField",
            "LineStringField",
            "PolygonField",
            "MultiPointField",
            "MultiLineStringField",
            "MultiPolygonField",
            "GeometryCollectionField",
        ):
            assert name in surreal_orm_lite.__all__
            assert getattr(surreal_orm_lite, name) is getattr(geo, name)
            assert name in geo.__all__
        for name in ("is_geo_field", "get_geo_info"):
            assert name in geo.__all__
            assert name not in surreal_orm_lite.__all__
            assert not hasattr(surreal_orm_lite, name)

    # --- model auth and live fallbacks (spec §7) -------------------------------------------------

    def test_signup_binds_none_for_an_omitted_alias_field_and_the_geometry_default(self) -> None:
        bound = _GeoAuth._auth_variables("signup", {"email": "a@b.c", "password": "x"}, require_all=True)
        assert bound["home"] is None
        assert type(bound["door"]) is GeometryPoint
        assert bound["door"] == GeometryPoint(0, 0)

    def test_minimal_instance_sets_alias_fields_to_none(self) -> None:
        instance = live.minimal_instance(_GeoLive, RecordID("geo_live", "x"))
        assert instance.location is None
        assert instance.area is None

    def test_hydrate_fallback_never_leaves_an_alias_field_unset(self) -> None:
        instance, error = live.hydrate(_GeoLive, {"id": RecordID("geo_live", "x"), "location": "not a point"})
        assert isinstance(error, ValidationError)
        assert instance.location == "not a point"
        assert instance.area is None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            dumped = instance.model_dump()
        assert dumped["location"] == "not a point"
        assert dumped["area"] is None


# --- the encoding boundary (Task 4) --------------------------------------------------------------


class GeoProbe(BaseSurrealModel):
    """Untyped: keyword writes and raw queries carry SDK values unvalidated."""

    model_config = SurrealConfigDict(table_name="geo_raw")

    id: str | None = None
    location: Any = None


class GeoTypedProbe(BaseSurrealModel):
    """Typed geometry fields, filled through ``model_construct()`` (no validation, P4)."""

    model_config = SurrealConfigDict(table_name="geo_raw")

    id: str | None = None
    location: PointField = None
    area: PolygonField = None


class _SdkPin(SdkPoint):
    """A user subclass of the SDK's point."""


def _square() -> tuple[Any, Any, Any, Any]:
    """Four SDK points of a unit square, for the rings below."""
    return SdkPoint(0.0, 0.0), SdkPoint(1.0, 0.0), SdkPoint(1.0, 1.0), SdkPoint(0.0, 1.0)


def _with(value: Any, attr: str, members: Any) -> Any:
    """``value`` whose member attribute was replaced after construction."""
    setattr(value, attr, members)
    return value


def _ring(*points: Any) -> Any:
    """An SDK polygon whose exterior ring holds exactly ``points`` (mutated after construction)."""
    polygon = SdkPolygon.parse_coordinates([RING])
    polygon.geometry_lines[0].geometry_points = list(points)
    return polygon


def _open_hole() -> Any:
    """An SDK polygon whose hole was mutated, after construction, to leave out its closing point."""
    polygon = SdkPolygon.parse_coordinates([RING, HOLE])
    polygon.geometry_lines[1].geometry_points = polygon.geometry_lines[1].geometry_points[:-1]
    return polygon


def _short_hole() -> Any:
    """An SDK polygon whose hole was mutated to 3 closed points."""
    polygon = SdkPolygon.parse_coordinates([RING, HOLE])
    h0, h1 = polygon.geometry_lines[1].geometry_points[:2]
    polygon.geometry_lines[1].geometry_points = [h0, h1, h0]
    return polygon


#: Valid SDK geometries of spec §3.8's "stored and read back" list: ``encode()`` returns each as is.
KEPT: dict[str, Callable[[], Any]] = {
    **{kind: functools.partial(_sdk, kind) for kind in KINDS},
    "nested-collection": lambda: SdkCollection(SdkCollection(_sdk("point"), _sdk("line")), _sdk("polygon"), SdkMultiPoint()),
    "empty-multipoint": SdkMultiPoint,
    "empty-multiline": SdkMultiLine,
    "empty-multipolygon": SdkMultiPolygon,
    "empty-collection": SdkCollection,
    "open-ring-4-points": lambda: _ring(*_square()),
    "open-ring-3-points": lambda: _ring(*_square()[:3]),
    "open-hole": _open_hole,
    "multipolygon-open-ring": lambda: SdkMultiPolygon(_ring(*_square()), _sdk("polygon")),
    "collection-open-ring": lambda: SdkCollection(_ring(*_square()[:3])),
    "line-two-equal-points": lambda: SdkLine(SdkPoint(1.0, 2.0), SdkPoint(1.0, 2.0)),
    "int64-max": lambda: SdkPoint(2**63 - 1, 2**63 - 1),
    "int64-min": lambda: SdkPoint(-(2**63), -(2**63)),
    "float-nan": lambda: SdkPoint(math.nan, 1.0),
    "float-inf": lambda: SdkPoint(math.inf, -math.inf),
    "decimal": lambda: SdkPoint(Decimal("1.5"), Decimal("-2")),
    "decimal-nan": lambda: SdkPoint(Decimal("NaN"), 1.0),
    "decimal-positional-long": lambda: SdkPoint(Decimal("0.1234567890123456789012345678901"), 1.0),
    "decimal-2**96-1": lambda: SdkPoint(Decimal("7.9228162514264337593543950335E+28"), 1.0),
    "cbor-tag": lambda: SdkPoint(CBORTag(surrealdb.TAG_DECIMAL_STRING, "1.5"), 1.0),
    "sdk-subclass": lambda: _SdkPin(*A),
    "tuple-line": lambda: _with(SdkLine(*_square()[:2]), "geometry_points", tuple(_square()[:2])),
    "tuple-polygon": lambda: _with(_sdk("polygon"), "geometry_lines", tuple(_sdk("polygon").geometry_lines)),
    "tuple-multipoint": lambda: _with(SdkMultiPoint(), "geometry_points", (SdkPoint(*A), SdkPoint(*B))),
    "tuple-collection": lambda: _with(SdkCollection(), "geometries", (_sdk("point"), _sdk("line"))),
}

#: Decimal coordinates whose ``str()`` writes an exponent: ``encode()`` rewrites them in fixed point.
EXPONENT_DECIMALS = [d for d in DECIMAL_STORED if "E" in str(d)]

_TINY = Decimal("1E-7")


def _rewritten() -> dict[str, Callable[[], Any]]:
    """SDK geometries ``encode()`` rebuilds: exponent-form decimal coordinates, ORM members."""
    a, b, c, _ = _square()
    return {
        **{f"point-{d}": functools.partial(SdkPoint, d, 1.0) for d in EXPONENT_DECIMALS},
        "line": lambda: SdkLine(SdkPoint(_TINY, 0.0), b),
        "open-ring": lambda: _ring(SdkPoint(_TINY, 0.0), b, c),
        "collection": lambda: SdkCollection(SdkPoint(_TINY, 0.0), _sdk("line")),
        "sdk-subclass": lambda: _SdkPin(_TINY, 1.0),
        "orm-in-multipoint": lambda: SdkMultiPoint(GeometryPoint(*A), SdkPoint(*B)),
        "orm-in-collection": lambda: SdkCollection(_orm("collection"), _sdk("point")),
    }


#: Refused SDK geometries (spec §3.8 "refused by every line") and a part of each message.
REFUSED: dict[str, tuple[Callable[[], Any], str]] = {
    "coordinate-bool": (lambda: SdkPoint(True, 1.0), "Cannot send GeometryPoint: a coordinate must be a number, got bool."),
    "coordinate-str": (lambda: SdkPoint(1.0, "a"), "Cannot send GeometryPoint: a coordinate must be a number, got str."),
    "coordinate-none": (lambda: SdkPoint(None, 1.0), "Cannot send GeometryPoint: a coordinate must be a number, got NoneType."),
    "coordinate-2**64": (lambda: SdkPoint(2**64, 1.0), "SurrealDB integers are signed 64-bit"),
    "coordinate-decimal-8E+28": (lambda: SdkPoint(Decimal("8E+28"), 1.0), "exceeds 2**96 - 1"),
    "coordinate-decimal-1E+30": (lambda: SdkPoint(1.0, Decimal("1E+30")), "exceeds 2**96 - 1"),
    "line-1-point": (_bad_line, "Cannot send GeometryLine: a line needs at least 2 points, got 1."),
    "line-0-points": (
        lambda: _with(SdkLine(*_square()[:2]), "geometry_points", []),
        "Cannot send GeometryLine: a line needs at least 2 points, got 0.",
    ),
    "polygon-no-ring": (
        lambda: _with(_sdk("polygon"), "geometry_lines", []),
        "Cannot send GeometryPolygon: a polygon needs at least 1 ring, got 0.",
    ),
    "ring-2-open-points": (
        lambda: _ring(*_square()[:2]),
        "Cannot send GeometryPolygon: ring 1 has 3 points once closed; SurrealDB needs at least 4.",
    ),
    "ring-3-closed-points": (_bad_ring, "ring 1 has 3 points once closed; SurrealDB needs at least 4."),
    "ring-2-equal-points": (lambda: _ring(SdkPoint(0.0, 0.0), SdkPoint(0.0, 0.0)), "ring 1 has 2 points once closed"),
    "ring-1-point": (lambda: _ring(SdkPoint(0.0, 0.0)), "ring 1 has 1 point once closed"),
    "ring-0-points": (lambda: _ring(), "ring 1 has 0 points once closed"),
    "hole-3-closed-points": (_short_hole, "ring 2 has 3 points once closed"),
    "multiline-1-point-line": (lambda: SdkMultiLine(_sdk("line"), _bad_line()), "a line needs at least 2 points, got 1."),
    "multipolygon-ringless-polygon": (
        lambda: SdkMultiPolygon(_with(_sdk("polygon"), "geometry_lines", [])),
        "a polygon needs at least 1 ring, got 0.",
    ),
    "line-of-tuples": (
        lambda: _with(SdkLine(*_square()[:2]), "geometry_points", [(0.0, 0.0), (1.0, 1.0)]),
        "Cannot send GeometryLine: member 1 is a tuple, not a GeometryPoint.",
    ),
    "line-with-a-tuple": (
        lambda: _with(SdkLine(*_square()[:2]), "geometry_points", [SdkPoint(0.0, 0.0), (1.0, 1.0)]),
        "Cannot send GeometryLine: member 2 is a tuple, not a GeometryPoint.",
    ),
    "ring-of-tuples": (
        lambda: _ring((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 0.0)),
        "Cannot send GeometryPolygon: ring 1 point 1 is a tuple, not a GeometryPoint.",
    ),
    "ring-as-a-list": (
        lambda: _with(_sdk("polygon"), "geometry_lines", [list(_square())]),
        "Cannot send GeometryPolygon: ring 1 is a list, not a GeometryLine.",
    ),
    "multipoint-of-tuples": (
        lambda: _with(SdkMultiPoint(), "geometry_points", [(0.0, 0.0)]),
        "Cannot send GeometryMultiPoint: member 1 is a tuple, not a GeometryPoint.",
    ),
    "multipolygon-with-a-line": (
        lambda: SdkMultiPolygon(_sdk("line")),
        "Cannot send GeometryMultiPolygon: member 1 is a GeometryLine, not a GeometryPolygon.",
    ),
    "multiline-with-an-orm-point": (
        lambda: SdkMultiLine(GeometryPoint(*A)),
        "Cannot send GeometryMultiLine: member 1 is a GeometryPoint, not a GeometryLine.",
    ),
    "collection-dict": (
        lambda: SdkCollection({"type": "Point", "coordinates": [0.0, 0.0]}),
        "Cannot send GeometryCollection: member 1 is a dict, not a geometry.",
    ),
    "collection-tuple": (
        lambda: SdkCollection(_sdk("point"), (0.0, 0.0)),
        "Cannot send GeometryCollection: member 2 is a tuple, not a geometry.",
    ),
    "collection-int": (lambda: SdkCollection(1), "Cannot send GeometryCollection: member 1 is an int, not a geometry."),
    "collection-none": (lambda: SdkCollection(None), "Cannot send GeometryCollection: member 1 is None, not a geometry."),
    "nested-bad-coordinate": (lambda: SdkCollection(SdkCollection(SdkPoint("a", 1.0))), "got str."),
    "members-not-a-sequence": (
        lambda: _with(SdkLine(*_square()[:2]), "geometry_points", "ab"),
        "Cannot send GeometryLine: geometry_points must be a list, got str.",
    ),
}

_SDK_OF: dict[type[Geometry], type[Any]] = {ORM_CLASSES[kind]: SDK_CLASSES[kind] for kind in KINDS}


def _corpus_ids() -> list[str]:
    return [f"{index}-{type(value).__name__}" for index, value in enumerate(_corpus())]


def _assert_plain_refusal(info: pytest.ExceptionInfo[ValueError]) -> None:
    """A plain ``ValueError`` (never an ORM or SDK error class) whose message is neutral."""
    assert type(info.value) is ValueError, f"raised as {type(info.value).__name__}"
    assert not isinstance(info.value, surrealdb.errors.SurrealError)
    assert "connection" not in str(info.value).lower()


class TestEncodeGeometry:
    """AC-5: the two geometry rows of ``encode()`` (spec §6.1; D7, D15)."""

    @pytest.mark.parametrize("value", _corpus(), ids=_corpus_ids())
    def test_orm_geometry_becomes_the_equal_sdk_geometry(self, value: Geometry) -> None:
        out = encoding.encode(value)
        assert type(out) is _SDK_OF[type(value)]
        assert value == out
        assert _all_sdk(out)
        assert _all_lists(out)

    def test_orm_geometry_inside_a_payload(self) -> None:
        point = GeometryPoint(*A)
        value = {"a": [point], "b": 1}
        out = encoding.encode(value)
        assert out is not value and out["a"] is not value["a"]
        assert value == {"a": [point], "b": 1} and value["a"][0] is point
        assert type(out["a"][0]) is SdkPoint and out["a"][0] == SdkPoint(*A)
        assert encoding.encode(out) is out

    def test_orm_subclass_becomes_the_sdk_class(self) -> None:
        out = encoding.encode(_Pin(*A))
        assert type(out) is SdkPoint and out == SdkPoint(*A)

    @pytest.mark.parametrize("make", list(KEPT.values()), ids=list(KEPT))
    def test_valid_sdk_geometry_is_the_same_object(self, make: Callable[[], Any]) -> None:
        value = make()
        assert encoding.encode(value) is value
        assert encoding.encode({"v": [value]})["v"][0] is value

    def test_bare_sdk_geometry_is_unchanged(self) -> None:
        value = surrealdb.Geometry()
        assert encoding.encode(value) is value

    def test_encode_sdk_geometry_leaves_anything_else_alone(self) -> None:
        value = object()
        assert encoding._encode_sdk_geometry(value) is value

    @pytest.mark.parametrize("d", EXPONENT_DECIMALS, ids=str)
    def test_exponent_decimal_coordinate_is_rewritten_in_fixed_point(self, d: Decimal) -> None:
        point = SdkPoint(d, 1.0)
        out = encoding.encode(point)
        assert type(out) is SdkPoint and out is not point
        assert out.longitude == encoding._encode_decimal(d)
        assert isinstance(out.longitude, CBORTag) and out.longitude.tag == surrealdb.TAG_DECIMAL_STRING
        assert "E" not in out.longitude.value
        assert out.latitude == 1.0
        assert point.longitude is d  # the original is untouched
        flipped = encoding.encode(SdkPoint(1.0, d))
        assert flipped.longitude == 1.0 and flipped.latitude == encoding._encode_decimal(d)

    @pytest.mark.parametrize("d", [d for d in DECIMAL_STORED if d not in EXPONENT_DECIMALS], ids=str)
    def test_positional_decimal_coordinate_is_kept(self, d: Decimal) -> None:
        point = SdkPoint(d, 1.0)
        assert encoding.encode(point) is point

    def test_rewritten_members_rebuild_their_containers(self) -> None:
        fixed = encoding._encode_decimal(_TINY)
        a, b, c, _ = _square()
        line = SdkLine(SdkPoint(_TINY, 0.0), b)
        out = encoding.encode(line)
        assert type(out) is SdkLine and out is not line
        assert out.geometry_points[0].longitude == fixed and out.geometry_points[1] is b
        assert line.geometry_points[0].longitude is _TINY

        polygon = _ring(SdkPoint(_TINY, 0.0), b, c)  # open: SurrealDB closes it
        out = encoding.encode(polygon)
        ring = out.geometry_lines[0]
        assert type(out) is SdkPolygon and type(ring) is SdkLine
        assert len(ring.geometry_points) == 3
        assert ring.geometry_points[0].longitude == fixed
        assert ring.geometry_points[1] is b and ring.geometry_points[2] is c
        assert polygon.geometry_lines[0].geometry_points[0].longitude is _TINY

        kept = _sdk("line")
        collection = SdkCollection(SdkPoint(_TINY, 0.0), kept)
        out = encoding.encode(collection)
        assert type(out) is SdkCollection
        assert out.geometries[0].longitude == fixed and out.geometries[1] is kept
        assert collection.geometries[0].longitude is _TINY

        multi = SdkMultiPolygon(_sdk("polygon"), _ring(SdkPoint(_TINY, 0.0), b, c, a))
        out = encoding.encode(multi)
        assert out.geometry_polygons[0] is multi.geometry_polygons[0]
        assert out.geometry_polygons[1].geometry_lines[0].geometry_points[0].longitude == fixed

    def test_a_ring_of_signalling_nan_points_is_compared_without_raising(self) -> None:
        snan = SdkPoint(Decimal("sNaN"), 0.0)
        polygon = _ring(snan, SdkPoint(1.0, 0.0), snan)  # not provably closed: 4 points once closed
        assert encoding.encode(polygon) is polygon

    def test_a_rebuilt_tuple_container_stays_a_tuple(self) -> None:
        b = SdkPoint(*B)
        line = _with(SdkLine(SdkPoint(*A), b), "geometry_points", (SdkPoint(_TINY, 0.0), b))
        out = encoding.encode(line)
        assert type(out.geometry_points) is tuple
        assert out.geometry_points[1] is b

    def test_an_sdk_subclass_keeps_its_type(self) -> None:
        out = encoding.encode(_SdkPin(_TINY, 1.0))
        assert type(out) is _SdkPin
        assert out.longitude == encoding._encode_decimal(_TINY)

    def test_orm_members_of_sdk_geometries_become_sdk_values(self) -> None:
        a, b = SdkPoint(*A), SdkPoint(*B)
        cases: list[tuple[Any, Any]] = [
            (SdkMultiPoint(GeometryPoint(*A), b), SdkMultiPoint(a, b)),
            (SdkLine(GeometryPoint(*A), b), SdkLine(a, b)),
            (
                _with(SdkPolygon.parse_coordinates([RING]), "geometry_lines", [GeometryLine.parse_coordinates(RING)]),
                SdkPolygon.parse_coordinates([RING]),
            ),
            (SdkMultiLine(GeometryLine(GeometryPoint(*A), GeometryPoint(*B))), SdkMultiLine(SdkLine(a, b))),
            (SdkMultiPolygon(_orm("polygon")), SdkMultiPolygon(_sdk("polygon"))),
            (SdkCollection(_orm("collection"), b), SdkCollection(_sdk("collection"), b)),
        ]
        for value, expected in cases:
            out = encoding.encode(value)
            assert type(out) is type(expected)
            assert out == expected
            assert _all_sdk(out)
            assert encoding.encode(out) is out
        multi = cases[0][0]
        assert type(multi.geometry_points[0]) is GeometryPoint  # the original is untouched
        assert encoding.encode(multi).geometry_points[1] is b

    def test_idempotent(self) -> None:
        values: list[Any] = [
            *(make() for make in KEPT.values()),
            *(make() for make in _rewritten().values()),
            *_corpus(),
            {"a": [GeometryPoint(*A), SdkPoint(_TINY, 1.0)]},
        ]
        for value in values:
            once = encoding.encode(value)
            assert encoding.encode(once) is once, repr(value)

    @pytest.mark.parametrize(("make", "message"), list(REFUSED.values()), ids=list(REFUSED))
    def test_refused_with_a_plain_value_error(self, make: Callable[[], Any], message: str) -> None:
        value = make()
        with pytest.raises(ValueError) as info:
            encoding.encode(value)
        _assert_plain_refusal(info)
        assert message in str(info.value)
        with pytest.raises(ValueError) as nested:
            encoding.encode({"v": [value]})
        _assert_plain_refusal(nested)

    @pytest.mark.parametrize(
        "make",
        [_bad_line, functools.partial(SdkPoint, 2**64, 1.0), _bad_ring],
        ids=["line-1-point", "coordinate-2**64", "ring-3-closed-points"],
    )
    async def test_refusals_never_reach_the_client(self, make: Callable[[], Any], monkeypatch: pytest.MonkeyPatch) -> None:
        _no_client(monkeypatch)
        calls: list[Callable[[], Coroutine[Any, Any, Any]]] = [
            lambda: GeoProbe.objects().filter(location=make()).exec(),
            lambda: GeoProbe.raw_query("RETURN $p;", {"p": make()}),
            lambda: GeoProbe(id="x").merge(location=make(), refresh=False),
        ]
        for call in calls:
            with pytest.raises(ValueError) as info:
                await call()
            _assert_plain_refusal(info)


class TestUncheckedOrmGeometry:
    """P4: an ORM geometry built unchecked from a malformed SDK value is refused by ``encode()``."""

    @pytest.mark.parametrize("make", [_bad_ring, _bad_line, _bad_point], ids=["ring", "line", "point"])
    def test_encode_refuses_with_a_plain_value_error(self, make: Callable[[], Any]) -> None:
        unchecked = geo._from_sdk(make(), checked=False)
        assert isinstance(unchecked, Geometry)
        with pytest.raises(ValueError) as info:
            encoding.encode(unchecked)
        _assert_plain_refusal(info)
        assert unchecked._to_sdk() is not None  # still converts without raising

    @pytest.mark.parametrize(
        ("field", "make", "message"),
        [("area", _bad_ring, "ring 1 has 3 points once closed"), ("location", _bad_point, "got str")],
        ids=["area", "location"],
    )
    async def test_upsert_of_a_constructed_model_is_refused_before_anything_is_sent(
        self, field: str, make: Callable[[], Any], message: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_client(monkeypatch)
        probe = GeoTypedProbe.model_construct(id="x", **{field: make()})
        with pytest.raises(ValueError, match=message) as info:
            await probe.upsert()
        _assert_plain_refusal(info)


#: Each SDK value the raw round trip stores and reads back as the same class (spec §3.1).
ROUND_TRIP: dict[str, Callable[[], Any]] = {
    **{kind: functools.partial(_sdk, kind) for kind in KINDS},
    "nested-collection": KEPT["nested-collection"],
    "empty-multipoint": SdkMultiPoint,
    "empty-multiline": SdkMultiLine,
    "empty-multipolygon": SdkMultiPolygon,
    "empty-collection": SdkCollection,
    "tiny-floats": lambda: SdkPoint(1e-05, -2.5e-07),
}


class TestRawGeometryE2E:
    """AC-6: what SurrealDB does with SDK geometries sent by the raw SDK client (both lines)."""

    @pytest.mark.parametrize("make", list(ROUND_TRIP.values()), ids=list(ROUND_TRIP))
    async def test_round_trip_over_websocket_and_http(self, make: Callable[[], Any]) -> None:
        value = make()
        async with orm_client("geo_raw") as client:
            rid = RecordID("geo_raw", "ws")
            await client.query("CREATE $rid SET v = $v;", {"rid": rid, "v": value})
            back = await client.query("SELECT VALUE v FROM ONLY $rid;", {"rid": rid})
            assert type(back) is type(value)
            assert back == value
            http = await _http_client()
            rid = RecordID("geo_raw", "http")
            await http.query("CREATE $rid SET v = $v;", {"rid": rid, "v": value})
            back = await http.query("SELECT VALUE v FROM ONLY $rid;", {"rid": rid})
            assert type(back) is type(value)
            assert back == value

    async def test_a_geojson_map_stays_an_object(self) -> None:
        geojson = {"type": "Point", "coordinates": [-73.98, 40.74]}
        async with orm_client("geo_raw") as client:
            rid = RecordID("geo_raw", "map")
            await client.query("CREATE $rid SET v = $v;", {"rid": rid, "v": geojson})
            back = await client.query("SELECT VALUE v FROM ONLY $rid;", {"rid": rid})
            assert type(back) is dict and back == geojson
            assert await client.query("RETURN $v.is_geometry();", {"v": geojson}) is False
            assert await client.query("RETURN $v.is_geometry();", {"v": SdkPoint(*A)}) is True
            await client.query("DEFINE FIELD loc ON geo_raw TYPE option<geometry<point>>;", {})
            with pytest.raises(surrealdb.ServerError, match="geometry<point>"):
                await client.query("CREATE geo_raw:x SET loc = $map;", {"map": geojson})

    async def test_distance_of_the_reference_points(self) -> None:
        async with orm_client("geo_raw") as client:
            metres = await client.query("RETURN geo::distance($a, $b);", {"a": SdkPoint(*A), "b": SdkPoint(*B)})
            assert metres == pytest.approx(AB_METRES, abs=1e-6)


DECIMAL_CASES = [pytest.param(k, d, stored, id=str(d)) for k, (d, stored) in enumerate(DECIMAL_STORED.items())]


class TestDecimalCoordinatesE2E:
    """AC-6, D7: decimal coordinates of SDK geometries sent through the ORM, as every line stores them."""

    @pytest.mark.parametrize(("k", "d", "stored"), DECIMAL_CASES)
    async def test_raw_query_variable(self, k: int, d: Decimal, stored: float) -> None:
        async with orm_client("geo_raw") as client:
            rid = RecordID("geo_raw", k)
            await GeoProbe.raw_query("CREATE $rid SET v = $v;", {"rid": rid, "v": SdkPoint(d, 1.0)})
            back = await client.query("SELECT VALUE v FROM ONLY $rid;", {"rid": rid})
            assert type(back) is SdkPoint
            assert type(back.longitude) is float
            assert back.longitude == stored
            assert back.latitude == 1.0

    @pytest.mark.parametrize(("k", "d", "stored"), DECIMAL_CASES)
    async def test_merge_keyword(self, k: int, d: Decimal, stored: float) -> None:
        async with orm_client("geo_raw") as client:
            probe = GeoProbe(id=f"m{k}")
            await probe.save()
            await probe.merge(location=SdkPoint(d, 1.0))
            back = await client.query("SELECT VALUE location FROM ONLY $rid;", {"rid": RecordID("geo_raw", f"m{k}")})
            assert type(back) is SdkPoint
            assert back.longitude == stored

    async def test_short_exponent_forms_match_the_raw_value(self) -> None:
        async with orm_client("geo_raw") as client:
            if await _server_version() < (3, 0, 0):
                pytest.skip("SurrealDB 2.x refuses the raw exponent form")
            for k, text in enumerate(("1E-7", "1.5E-7", "1E+2", "1.2E+2")):
                d = Decimal(text)
                orm_rid, raw_rid = RecordID("geo_raw", f"orm{k}"), RecordID("geo_raw", f"raw{k}")
                await GeoProbe.raw_query("CREATE $rid SET v = $v;", {"rid": orm_rid, "v": SdkPoint(d, 1.0)})
                await client.query("CREATE $rid SET v = $v;", {"rid": raw_rid, "v": SdkPoint(d, 1.0)})
                via_orm = await client.query("SELECT VALUE v FROM ONLY $rid;", {"rid": orm_rid})
                raw = await client.query("SELECT VALUE v FROM ONLY $rid;", {"rid": raw_rid})
                assert via_orm == raw, text
                assert via_orm.longitude == DECIMAL_STORED[d], text


# --- text and live literals (Task 5) -------------------------------------------------------------

_SQUARE_TEXT = "[[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0], [0.0, 0.0]]"
_HOLE_TEXT = "[[1.0, 1.0], [2.0, 1.0], [2.0, 2.0], [1.0, 2.0], [1.0, 1.0]]"


def _origin_line() -> GeometryLine:
    return GeometryLine(GeometryPoint(0, 0), GeometryPoint(1, 1))


def _square_polygon(*, hole: bool = False) -> GeometryPolygon:
    rings = [[(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)]]
    if hole:
        rings.append([(1, 1), (2, 1), (2, 2), (1, 2), (1, 1)])
    return GeometryPolygon.parse_coordinates(rings)


#: Exact texts (spec §6.5): the GeoJSON object form for every kind and every collection member.
LITERAL_TEXTS: dict[str, tuple[Callable[[], Geometry], str]] = {
    "point": (lambda: GeometryPoint(-73.99, 40.75), "{type: 'Point', coordinates: [-73.99, 40.75]}"),
    "line": (_origin_line, "{type: 'LineString', coordinates: [[0.0, 0.0], [1.0, 1.0]]}"),
    "polygon-with-a-hole": (
        lambda: _square_polygon(hole=True),
        f"{{type: 'Polygon', coordinates: [{_SQUARE_TEXT}, {_HOLE_TEXT}]}}",
    ),
    "multipoint": (
        lambda: GeometryMultiPoint(GeometryPoint(0, 0), GeometryPoint(1, 1)),
        "{type: 'MultiPoint', coordinates: [[0.0, 0.0], [1.0, 1.0]]}",
    ),
    "multiline": (
        lambda: GeometryMultiLine(_origin_line(), GeometryLine.parse_coordinates([(2, 2), (3, 3)])),
        "{type: 'MultiLineString', coordinates: [[[0.0, 0.0], [1.0, 1.0]], [[2.0, 2.0], [3.0, 3.0]]]}",
    ),
    "multipolygon": (
        lambda: GeometryMultiPolygon(_square_polygon(hole=True), _square_polygon()),
        f"{{type: 'MultiPolygon', coordinates: [[{_SQUARE_TEXT}, {_HOLE_TEXT}], [{_SQUARE_TEXT}]]}}",
    ),
    "collection": (
        lambda: GeometryCollection(GeometryPoint(1, 2), _origin_line()),
        "{type: 'GeometryCollection', geometries: [{type: 'Point', coordinates: [1.0, 2.0]}, "
        "{type: 'LineString', coordinates: [[0.0, 0.0], [1.0, 1.0]]}]}",
    ),
    "nested-collection": (
        lambda: GeometryCollection(GeometryCollection(GeometryPoint(1, 2)), GeometryMultiPoint()),
        "{type: 'GeometryCollection', geometries: [{type: 'GeometryCollection', geometries: "
        "[{type: 'Point', coordinates: [1.0, 2.0]}]}, {type: 'MultiPoint', coordinates: []}]}",
    ),
    "empty-multipoint": (GeometryMultiPoint, "{type: 'MultiPoint', coordinates: []}"),
    "empty-multiline": (GeometryMultiLine, "{type: 'MultiLineString', coordinates: []}"),
    "empty-multipolygon": (GeometryMultiPolygon, "{type: 'MultiPolygon', coordinates: []}"),
    "empty-collection": (GeometryCollection, "{type: 'GeometryCollection', geometries: []}"),
    "tiny-floats": (lambda: GeometryPoint(1e-05, -2.5e-07), "{type: 'Point', coordinates: [1e-05, -2.5e-07]}"),
    "large-floats": (lambda: GeometryPoint(1e16, -1e300), "{type: 'Point', coordinates: [1e+16, -1e+300]}"),
    "orm-subclass": (lambda: _Pin(1, 2), "{type: 'Point', coordinates: [1.0, 2.0]}"),
}

#: SDK coordinates with no verified literal (spec §6.5): decimals, other CBOR tags and non-finite numbers.
NO_LITERAL_COORDINATES: dict[str, Any] = {
    "decimal": Decimal("1.5"),
    "decimal-exponent": Decimal("1E-7"),
    "decimal-nan": Decimal("NaN"),
    "cbor-tag": CBORTag(surrealdb.TAG_DECIMAL_STRING, "1.5"),
    "cbor-tag-not-a-decimal": CBORTag(surrealdb.TAG_DURATION_COMPACT, [1, 0]),
    "nan": math.nan,
    "inf": math.inf,
    "-inf": -math.inf,
}


def _unchecked(make: Callable[[], Any]) -> Geometry:
    """An ORM value the serializer built unchecked from a (malformed) SDK value."""
    value = geo._from_sdk(make(), checked=False)
    assert isinstance(value, Geometry)
    return value


#: ORM values built unchecked whose text cannot be written: ``str()`` falls back to ``repr()``.
NO_TEXT: dict[str, Callable[[], Geometry]] = {
    "point-holding-a-str": lambda: _unchecked(_bad_point),
    "point-holding-nan": lambda: _unchecked(lambda: SdkPoint(math.nan, 1.0)),
    "point-holding-a-decimal": lambda: _unchecked(lambda: SdkPoint(Decimal("1.5"), 1.0)),
    "point-holding-a-huge-int": lambda: _unchecked(lambda: SdkPoint(10**400, 1.0)),
    "multipolygon-holding-a-line": lambda: _unchecked(lambda: SdkMultiPolygon(_sdk("line"))),
    "collection-holding-a-dict": lambda: _unchecked(lambda: SdkCollection({"type": "Point", "coordinates": [0.0, 0.0]})),
    "collection-holding-a-member-without-repr": lambda: _unchecked(lambda: SdkCollection(_NoRepr())),
}


class TestGeometryLiteral:
    """AC-7 and the ``str()`` part of AC-3: GeoJSON object literals and text (spec §6.5)."""

    @pytest.mark.parametrize(("make", "text"), list(LITERAL_TEXTS.values()), ids=list(LITERAL_TEXTS))
    def test_exact_text(self, make: Callable[[], Geometry], text: str) -> None:
        assert to_surql_literal(make()) == text

    @pytest.mark.parametrize(("make", "text"), list(LITERAL_TEXTS.values()), ids=list(LITERAL_TEXTS))
    def test_sdk_value_of_the_boundary_renders_the_same_text(self, make: Callable[[], Geometry], text: str) -> None:
        sdk = encoding.encode(make())
        assert _all_sdk(sdk)
        assert to_surql_literal(sdk) == text

    @pytest.mark.parametrize("kind", KINDS)
    def test_sdk_twin_renders_the_same_text(self, kind: str) -> None:
        assert to_surql_literal(_sdk(kind)) == to_surql_literal(_orm(kind))

    def test_sdk_subclass_renders_its_kind(self) -> None:
        assert to_surql_literal(_SdkPin(*A)) == "{type: 'Point', coordinates: [-73.98, 40.74]}"

    def test_sdk_int_coordinates_render_as_floats(self) -> None:
        assert to_surql_literal(SdkPoint(1, -2)) == "{type: 'Point', coordinates: [1.0, -2.0]}"
        assert to_surql_literal(SdkPoint(2**63 - 1, -(2**63))) == (
            "{type: 'Point', coordinates: [9.223372036854776e+18, -9.223372036854776e+18]}"
        )

    def test_sdk_open_ring_renders_as_given(self) -> None:
        assert to_surql_literal(_ring(*_square())) == (
            "{type: 'Polygon', coordinates: [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]]}"
        )
        assert (
            to_surql_literal(_ring(*_square()[:3])) == "{type: 'Polygon', coordinates: [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]]}"
        )

    def test_sdk_tuple_members_render_like_lists(self) -> None:
        assert to_surql_literal(KEPT["tuple-line"]()) == "{type: 'LineString', coordinates: [[0.0, 0.0], [1.0, 0.0]]}"
        assert to_surql_literal(KEPT["tuple-collection"]()) == to_surql_literal(SdkCollection(_sdk("point"), _sdk("line")))

    def test_orm_members_of_an_sdk_value_render_and_stay_untouched(self) -> None:
        orm = GeometryPoint(*A)
        value = SdkMultiPoint(orm, SdkPoint(*B))
        assert to_surql_literal(value) == "{type: 'MultiPoint', coordinates: [[-73.98, 40.74], [-73.99, 40.75]]}"
        assert value.geometry_points[0] is orm

    def test_inside_lists_and_dicts(self) -> None:
        point = GeometryPoint(1, 2)
        text = "{type: 'Point', coordinates: [1.0, 2.0]}"
        assert to_surql_literal([point, SdkPoint(1.0, 2.0)]) == f"[{text}, {text}]"
        assert to_surql_literal({"at": point}) == f"{{'at': {text}}}"

    @pytest.mark.parametrize("value", _corpus(), ids=_corpus_ids())
    def test_str_is_the_literal(self, value: Geometry) -> None:
        assert str(value) == to_surql_literal(value)
        assert f"{value}" == str(value)

    @pytest.mark.parametrize("make", list(NO_TEXT.values()), ids=list(NO_TEXT))
    def test_str_never_raises_and_falls_back_to_repr(self, make: Callable[[], Geometry]) -> None:
        value = make()
        assert str(value) == repr(value)

    def test_str_of_an_unchecked_point_holding_a_str_is_its_repr(self) -> None:
        value = _unchecked(_bad_point)
        assert str(value) == "GeometryPoint(longitude=a, latitude=1.0)"

    @pytest.mark.parametrize("make", list(NO_TEXT.values()), ids=list(NO_TEXT))
    def test_unchecked_orm_value_without_text_has_no_literal(self, make: Callable[[], Geometry]) -> None:
        with pytest.raises(TypeError, match="Cannot inline this Geometry"):
            to_surql_literal(make())

    @pytest.mark.parametrize("coordinate", list(NO_LITERAL_COORDINATES.values()), ids=list(NO_LITERAL_COORDINATES))
    def test_sdk_coordinate_without_literal_raises_type_error(self, coordinate: Any) -> None:
        for value in (SdkPoint(coordinate, 1.0), SdkPoint(1.0, coordinate), SdkLine(SdkPoint(coordinate, 1.0), SdkPoint(*A))):
            with pytest.raises(TypeError, match=f"Cannot inline this {type(value).__name__}") as info:
                to_surql_literal(value)
            assert "connection" not in str(info.value).lower()

    def test_decimal_coordinate_message_names_the_decimal(self) -> None:
        # ``Decimal("1E-7")`` reaches the renderer as the boundary's decimal ``CBORTag``.
        for coordinate in (Decimal("1.5"), Decimal("1E-7"), CBORTag(surrealdb.TAG_DECIMAL_STRING, "1.5")):
            with pytest.raises(TypeError, match="not a decimal: a decimal coordinate has no verified text, use a float"):
                to_surql_literal(SdkPoint(coordinate, 1.0))
        with pytest.raises(TypeError, match="finite int or float to be written as SurrealQL, not CBORTag.$"):
            to_surql_literal(SdkPoint(CBORTag(surrealdb.TAG_DURATION_COMPACT, [1, 0]), 1.0))
        with pytest.raises(TypeError, match="this int is too large for a float"):
            to_surql_literal(_unchecked(lambda: SdkPoint(10**400, 1.0)))

    @pytest.mark.parametrize(("make", "message"), list(REFUSED.values()), ids=list(REFUSED))
    def test_malformed_sdk_geometry_raises_type_error(self, make: Callable[[], Any], message: str) -> None:
        value = make()
        with pytest.raises(TypeError, match=f"Cannot inline this {type(value).__name__}: SurrealDB refuses it") as info:
            to_surql_literal(value)
        assert message in str(info.value)
        assert type(info.value.__cause__) is ValueError
        assert "connection" not in str(info.value).lower()

    def test_inline_variables_writes_the_literal(self) -> None:
        text = "{type: 'Point', coordinates: [-73.99, 40.75]}"
        assert inline_variables("loc = $p", {"p": GeometryPoint(-73.99, 40.75)}) == f"loc = {text}"
        assert inline_variables("loc = $p", {"p": SdkPoint(-73.99, 40.75)}) == f"loc = {text}"
        with pytest.raises(TypeError, match=r"^\$p: Cannot inline this GeometryLine"):
            inline_variables("loc = $p", {"p": _bad_line()})

    def test_the_unsupported_type_message_lists_geometries(self) -> None:
        with pytest.raises(TypeError, match="geometries"):
            to_surql_literal(object())


def _closed(polygon: Any) -> Any:
    """The SDK polygon SurrealDB stores for ``polygon``: every open ring closed with its first point."""
    rings = []
    for ring in polygon.geometry_lines:
        points = list(ring.geometry_points)
        rings.append(SdkLine(*points) if points[0] == points[-1] else SdkLine(*points, points[0]))
    return SdkPolygon(*rings)


#: Each value of the E2E corpus and the SDK value ``RETURN <literal>`` gives back (``None``: ``encode(value)``).
LITERAL_CORPUS: dict[str, tuple[Callable[[], Any], Callable[[], Any] | None]] = {
    **{f"orm-{kind}": (functools.partial(_orm, kind), None) for kind in KINDS},
    **{f"sdk-{kind}": (functools.partial(_sdk, kind), None) for kind in KINDS},
    **{name: (make, None) for name, (make, _) in LITERAL_TEXTS.items() if name not in ("point", "line")},
    "corpus-nested-collection": (_nested_collection, None),
    "sdk-int-coordinates": (lambda: SdkPoint(1, -2), lambda: SdkPoint(1.0, -2.0)),
    "sdk-int64-bounds": (lambda: SdkPoint(2**63 - 1, -(2**63)), lambda: SdkPoint(float(2**63 - 1), float(-(2**63)))),
    "sdk-open-ring-4-points": (lambda: _ring(*_square()), lambda: _closed(_ring(*_square()))),
    "sdk-open-ring-3-points": (lambda: _ring(*_square()[:3]), lambda: _closed(_ring(*_square()[:3]))),
    "sdk-open-hole": (_open_hole, lambda: _closed(_open_hole())),
    "sdk-orm-member": (lambda: SdkMultiPoint(GeometryPoint(*A), SdkPoint(*B)), lambda: _sdk("multipoint")),
}


class TestGeometryLiteralE2E:
    """AC-7 on both lines: every literal means exactly the value it came from (``RETURN <literal> == $v``)."""

    @pytest.mark.parametrize(("make", "expected"), list(LITERAL_CORPUS.values()), ids=list(LITERAL_CORPUS))
    async def test_literal_equals_the_bound_value(self, make: Callable[[], Any], expected: Callable[[], Any] | None) -> None:
        value = make()
        bound = encoding.encode(value)
        literal = to_surql_literal(value)
        async with orm_client("geo_raw") as client:
            equal, back = await client.query(f"RETURN [{literal} == $v, {literal}];", {"v": bound})
        assert equal is True, literal
        twin = bound if expected is None else expected()
        assert type(back) is type(twin), literal
        assert back == twin, literal
