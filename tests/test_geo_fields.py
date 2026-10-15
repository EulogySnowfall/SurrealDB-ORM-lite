"""Geometry fields (v0.24.0) through the ORM: models whose fields hold geometries, written and read on both server lines.

Unit tests of the read rule first (no database); the classes suffixed ``E2E`` run against SurrealDB.
Tables are prefixed ``geo_`` and created inside ``orm_client(...)``, which drops them before and
after the test; the change-feed table lives in a database of its own (``GEO_CF_DATABASE``). The
reference values come from ``tests/test_geo_types.py``.
"""

import ast
import inspect
import math
import re
from datetime import timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field, ValidationError
from surrealdb import RecordID
from surrealdb.data import GeometryCollection as SdkCollection
from surrealdb.data import GeometryLine as SdkLine
from surrealdb.data import GeometryPoint as SdkPoint
from surrealdb.data import GeometryPolygon as SdkPolygon

from surreal_orm_lite import (
    BaseSurrealEdge,
    BaseSurrealModel,
    Computed,
    Count,
    GeoDistance,
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
    Q,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    SurrealDbError,
    Var,
    computed,
    constants,
    geo,
)
from surreal_orm_lite.model_base import _read_kind
from surreal_orm_lite.surql_literal import inline_variables
from surreal_orm_lite.utils import build_filter_condition
from tests._retry import retry_write
from tests.conftest import change_feed_database, orm_client
from tests.test_geo_types import (
    AB_METRES,
    HOLE,
    KINDS,
    NEAR,
    NEAR_B_METRES,
    ORM_CLASSES,
    PARIS,
    RING,
    A,
    B,
    _bad_line,
    _bad_point,
    _bad_ring,
    _model,
    _nest,
    _orm,
    _sdk,
)
from tests.test_live_queryset import _assert_silent, _take
from tests.test_rich_types import _http_client
from tests.test_transactions import _connect_http, _native_txn_supported

# --- shared models (later tasks reuse them) --------------------------------------------------------


class GeoSub(BaseModel):
    where: GeometryPoint


class GeoPlace(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="geo_place")

    id: str | None = None
    name: str = ""
    entrance: GeometryPoint  # required
    location: PointField = None
    area: PolygonField = None
    path: LineStringField = None
    stops: MultiPointField = None
    routes: MultiLineStringField = None
    zones: MultiPolygonField = None
    bag: GeometryCollectionField = None
    anything: GeometryField = None
    many: list[GeometryPoint] = []  # nested: the class, not the alias
    sub: GeoSub | None = None
    pos: GeometryPoint | None = Field(default=None, alias="position")
    n: int = 0


class GeoLink(BaseSurrealEdge):
    model_config = SurrealConfigDict(table_name="geo_edge")

    path: LineStringField = None


class GeoMixed(BaseSurrealModel):
    """The mixed seed's model: ``location`` is untyped, so every stored row validates."""

    model_config = SurrealConfigDict(table_name="geo_mixed")

    id: str | None = None
    n: int = 0
    location: Any = None


class GeoZone(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="geo_zone")

    id: str | None = None
    area: PolygonField = None
    centroid: Computed[GeometryPoint] = computed("IF area.is_polygon() THEN geo::centroid(area) END")


#: The database of this module's change-feed table: never ``ns``/``db`` (see ``change_feed_database``).
GEO_CF_DATABASE = "geo_change_feed"


class GeoFeed(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="geo_cf")

    id: str | None = None
    location: PointField = None


class GeoLegacy(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="geo_legacy")

    id: str | None = None
    location: PointField = None
    blob: dict[str, Any] | None = None


class GeoLoose(BaseSurrealModel):
    """Keeps undeclared columns (``extra="allow"``): a distance must never land among them."""

    model_config = SurrealConfigDict(table_name="geo_loose", extra="allow")

    id: str | None = None
    location: PointField = None


class GeoStrict(BaseSurrealModel):
    """Refuses undeclared columns (``extra="forbid"``): a distance must not make its rows invalid."""

    model_config = SurrealConfigDict(table_name="geo_strict", extra="forbid")

    id: str | None = None
    location: PointField = None


#: One value per ``GeoPlace`` field, built from the reference constants.
FULL: dict[str, Any] = {
    "name": "full",
    "entrance": GeometryPoint(*A),
    "location": GeometryPoint(*B),
    "area": GeometryPolygon.parse_coordinates([RING, HOLE]),
    "path": GeometryLine.parse_coordinates([A, B, NEAR]),
    "stops": GeometryMultiPoint.parse_coordinates([A, B]),
    "routes": GeometryMultiLine.parse_coordinates([[A, B], [B, NEAR]]),
    "zones": GeometryMultiPolygon.parse_coordinates([[RING, HOLE], [RING]]),
    "bag": GeometryCollection(GeometryPoint(*A), GeometryLine.parse_coordinates([A, B])),
    "anything": GeometryCollection(GeometryCollection(GeometryPoint(*PARIS)), GeometryPolygon.parse_coordinates([RING])),
    "many": [GeometryPoint(*A), GeometryPoint(*B)],
    "sub": GeoSub(where=GeometryPoint(*NEAR)),
    "pos": GeometryPoint(*PARIS),
    "n": 1,
}
#: The top-level geometry fields of ``GeoPlace`` and the column each one is stored in.
GEO_COLUMNS: dict[str, str] = {
    "entrance": "entrance",
    "location": "location",
    "area": "area",
    "path": "path",
    "stops": "stops",
    "routes": "routes",
    "zones": "zones",
    "bag": "bag",
    "anything": "anything",
    "pos": "position",
}


def _place(id: str, **changes: Any) -> GeoPlace:
    """A ``GeoPlace`` holding ``FULL``, with ``changes`` applied."""
    return GeoPlace(id=id, **{**FULL, **changes})


def _all_orm(value: Any) -> bool:
    """True when the value and every member, at every depth, is an ORM geometry (never the SDK's)."""
    if not isinstance(value, Geometry):
        return False
    attr: str | None = getattr(type(value), "_ATTR", None)
    return attr is None or all(_all_orm(member) for member in getattr(value, attr))


def _assert_geo(place: Any, *, nested: bool = True, **changes: Any) -> None:
    """``place`` holds ``FULL`` (with ``changes``) and every top-level geometry attribute is an ORM value.

    ``nested=True`` (a validated read) also checks the ``list[GeometryPoint]`` items and the
    sub-model's point. ``nested=False`` (after a write that syncs the instance from the returned
    row) checks what ``model_dump()`` gives for the list instead, and skips the sub-model, which
    holds the row's dict there (spec §6.6). Only ``many`` is dumped: the synced ``id`` and ``sub``
    are not this check's business.
    """
    expected = {**FULL, **changes}
    assert isinstance(place, GeoPlace), place
    wrong: dict[str, Any] = {}
    for key in GEO_COLUMNS:
        value = getattr(place, key)
        if not (type(value) is type(expected[key]) and _all_orm(value) and value == expected[key]):
            wrong[key] = value
    assert not wrong, f"not the expected ORM geometries: {wrong!r}"
    assert (place.name, place.n) == (expected["name"], expected["n"])
    if nested:
        assert [type(p) for p in place.many] == [GeometryPoint] * len(expected["many"])
        assert place.many == expected["many"]
        assert isinstance(place.sub, GeoSub), place.sub
        assert type(place.sub.where) is GeometryPoint and place.sub.where == expected["sub"].where
    else:
        dumped = place.model_dump(include={"many"})["many"]
        assert [type(p) for p in dumped] == [GeometryPoint] * len(expected["many"])
        assert dumped == expected["many"]


async def _raw(rid: RecordID) -> dict[str, Any]:
    """The stored row as the SDK reads it, bypassing the model."""
    client = await SurrealDBConnectionManager.get_client()
    row = await client.query("SELECT * FROM ONLY $rid;", {"rid": rid})
    assert isinstance(row, dict), row
    return row


def _assert_native(row: dict[str, Any], **changes: Any) -> None:
    """``row``, written from ``FULL`` (with ``changes``), holds SurrealDB's native geometries as the SDK reads them."""
    expected = {**FULL, **changes}
    wrong: dict[str, Any] = {}
    for key, column in GEO_COLUMNS.items():
        value = row.get(column, "<missing>")
        if not (type(value) is expected[key]._SDK_CLASS and expected[key] == value):
            wrong[column] = value
    assert not wrong, f"not stored as native geometries: {wrong!r}"
    assert [type(p) for p in row["many"]] == [SdkPoint] * len(expected["many"])
    assert type(row["sub"]["where"]) is SdkPoint


#: The keys of the mixed seed, in the order ``_seed_mixed`` writes them; ``n`` is the 1-based position.
MIXED_KEYS: tuple[str, ...] = ("near1", "near2", "far", "none", "nullish", "text", "obj", "poly")


async def _seed_mixed(client: Any) -> None:
    """Write the mixed seed (spec §3.4) through the raw SDK client; ``n`` is each record's place in ``MIXED_KEYS``.

    ``near1`` at A (1395 m from B), ``near2`` at NEAR (698 m from B), ``far`` at PARIS, ``none`` without
    ``location``, ``nullish`` with ``NULL``, ``text`` with ``'x'``, ``obj`` with a GeoJSON object at A
    (stored as a plain object, not a geometry) and ``poly`` with the HOLE polygon.
    """
    values: dict[str, Any] = {
        "near1": SdkPoint(*A),
        "near2": SdkPoint(*NEAR),
        "far": SdkPoint(*PARIS),
        "text": "x",
        "obj": {"type": "Point", "coordinates": list(A)},
        "poly": SdkPolygon.parse_coordinates([HOLE]),
    }
    for n, key in enumerate(MIXED_KEYS, start=1):
        rid = RecordID("geo_mixed", key)
        if key == "none":
            await client.query("CREATE $rid SET n = $n;", {"rid": rid, "n": n})
        elif key == "nullish":
            await client.query("CREATE $rid SET n = $n, location = NULL;", {"rid": rid, "n": n})
        else:
            await client.query("CREATE $rid SET n = $n, location = $v;", {"rid": rid, "n": n, "v": values[key]})


def _record_key(row: Any) -> str:
    """The record key of a result: an instance's ``id`` or a dict's ``"id"`` (a ``RecordID`` or its key)."""
    value = row["id"] if isinstance(row, dict) else row.id
    return str(value.id) if isinstance(value, RecordID) else str(value)


def _ids(rows: Any) -> set[str]:
    """The record keys of a list of results (instances or dicts)."""
    return {_record_key(row) for row in rows}


class _SdkPin(SdkPoint):
    """A user subclass of the SDK's point class."""


# --- the read rule (unit, spec §6.6) ---------------------------------------------------------------------


class TestGeometryReadKind:
    """Spec §6.6: a top-level geometry field has the read kind ``"geometry"``, keyed by name and column."""

    def test_shared_model(self) -> None:
        assert GeoPlace._read_kinds() == {**dict.fromkeys(GEO_COLUMNS, "geometry"), "position": "geometry"}

    def test_edge_computed_and_untyped_models(self) -> None:
        assert GeoLink._read_kinds() == {"path": "geometry"}
        assert GeoZone._read_kinds() == {"area": "geometry", "centroid": "geometry"}
        assert GeoMixed._read_kinds() == {}
        assert GeoLegacy._read_kinds() == {"location": "geometry"}

    @pytest.mark.parametrize(
        "annotation",
        [
            GeometryPoint,
            GeometryPoint | None,
            PointField,
            GeometryField,
            GeoField["point|polygon"],
            GeometryPolygon | GeometryLine,
        ],
    )
    def test_geometry_annotations(self, annotation: Any) -> None:
        assert _read_kind(annotation) == "geometry"

    @pytest.mark.parametrize(
        "annotation",
        [list[GeometryPoint], dict[str, GeometryPolygon], GeometryPoint | str, SdkPoint, GeoSub, Any, dict[str, Any]],
    )
    def test_other_annotations(self, annotation: Any) -> None:
        assert _read_kind(annotation) is None

    def test_duration_kinds_unchanged(self) -> None:
        assert _read_kind(timedelta | None) == "timedelta"


class TestGeometryApplyRecord:
    """Spec §6.6: ``_apply_record`` writes a returned row's top-level SDK geometries as ORM values."""

    def test_top_level_fields_become_orm_values(self) -> None:
        # ``_apply_record`` sets only the attributes the instance has: give the required one a value.
        place = GeoPlace.model_construct(id="x", entrance=GeometryPoint(0, 0))
        row = {
            "id": RecordID("geo_place", "x"),
            "entrance": SdkPoint(*A),
            "location": SdkPoint(*B),
            "area": SdkPolygon.parse_coordinates([RING, HOLE]),
            "position": SdkPoint(*PARIS),
            "many": [SdkPoint(*A)],
            "sub": {"where": SdkPoint(*NEAR)},
            "name": "r",
            "n": 3,
        }
        place._apply_record(row)
        for key, value in (("entrance", A), ("location", B), ("pos", PARIS)):
            assert type(getattr(place, key)) is GeometryPoint and getattr(place, key) == GeometryPoint(*value)
        assert type(place.area) is GeometryPolygon and _all_orm(place.area)
        assert place.area == GeometryPolygon.parse_coordinates([RING, HOLE])
        # Containers and sub-models keep the row's values: their own serializers or validators handle them.
        assert type(place.many[0]) is SdkPoint
        assert type(place.sub) is dict and type(place.sub["where"]) is SdkPoint
        assert (place.name, place.n) == ("r", 3)

    @pytest.mark.parametrize("kind", KINDS)
    def test_every_kind(self, kind: str) -> None:
        place = GeoPlace.model_construct()
        place._apply_record({"anything": _sdk(kind)})
        assert type(place.anything) is ORM_CLASSES[kind] and _all_orm(place.anything)
        assert place.anything == _orm(kind)

    def test_only_still_filters(self) -> None:
        place = GeoPlace.model_construct()
        place._apply_record({"location": SdkPoint(*A), "area": SdkPolygon.parse_coordinates([RING])}, only={"location"})
        assert type(place.location) is GeometryPoint and place.area is None

    @pytest.mark.parametrize(
        "make",
        [
            _bad_line,
            _bad_ring,
            _bad_point,
            lambda: _nest(SdkPoint(*A), 40, SdkCollection),
            lambda: _SdkPin(1.0, 2.0),
            lambda: GeometryPoint(*A),
            lambda: {"type": "Point", "coordinates": [1.0, 2.0]},
            lambda: [1.0, 2.0],
            lambda: "x",
            lambda: None,
        ],
        ids=[
            "short-line",
            "short-ring",
            "text-coordinate",
            "too-deep",
            "sdk-subclass",
            "orm",
            "geojson",
            "pair",
            "text",
            "none",
        ],
    )
    def test_other_values_are_applied_as_received(self, make: Any) -> None:
        value = make()
        place = GeoPlace.model_construct()
        place._apply_record({"anything": value})
        assert place.anything is value
        assert geo._geometry_from_sdk(value) is value

    def test_a_valid_sdk_value_is_converted_checked(self) -> None:
        line = SdkLine(SdkPoint(*A), SdkPoint(*B))
        converted = geo._geometry_from_sdk(line)
        assert type(converted) is GeometryLine and _all_orm(converted) and converted == line


# --- models through the server (E2E) ---------------------------------------------------------------------


class TestGeoRoundTripE2E:
    """AC-8: a model with a field of each kind round-trips on every read and write path, as ORM values."""

    async def test_save_then_validated_reads(self) -> None:
        rid = RecordID("geo_place", "p1")
        async with orm_client("geo_place") as client:
            place = _place("p1")
            await place.save()
            _assert_geo(await GeoPlace.objects().get("p1"))
            _assert_native(await _raw(rid))
            assert await client.query("SELECT VALUE location.is_point() FROM ONLY $rid;", {"rid": rid}) is True
            [found] = await GeoPlace.objects().filter(n=1).exec()
            _assert_geo(found)
            _assert_geo(await GeoPlace.objects().first())
            [every] = await GeoPlace.objects().all()
            _assert_geo(every)
            await place.refresh()
            _assert_geo(place, nested=False)

    async def test_upsert_creates_then_replaces(self) -> None:
        near = GeometryPoint(*NEAR)
        async with orm_client("geo_place"):
            place = _place("u")
            await place.upsert()
            _assert_geo(place, nested=False)
            _assert_geo(await GeoPlace.objects().get("u"))
            # The replace starts from a validated read, the documented way (see the sub-model tests).
            fresh = await GeoPlace.objects().get("u")
            fresh.location = near
            await fresh.upsert()
            _assert_geo(fresh, nested=False, location=near)
            _assert_geo(await GeoPlace.objects().get("u"), location=near)
            _assert_native(await _raw(RecordID("geo_place", "u")), location=near)

    async def test_update_after_changing_a_geometry(self) -> None:
        square = GeometryPolygon.parse_coordinates([RING])
        async with orm_client("geo_place"):
            await _place("up").save()
            fresh = await GeoPlace.objects().get("up")
            fresh.area = square
            await fresh.update()
            _assert_geo(fresh, area=square)
            _assert_geo(await GeoPlace.objects().get("up"), area=square)
            _assert_native(await _raw(RecordID("geo_place", "up")), area=square)

    async def test_merge_with_a_geometry_keyword(self) -> None:
        near = GeometryPoint(*NEAR)
        async with orm_client("geo_place"):
            place = _place("m")
            await place.save()
            await place.merge(location=near)
            _assert_geo(place, nested=False, location=near)
            _assert_geo(await GeoPlace.objects().get("m"), location=near)
            _assert_native(await _raw(RecordID("geo_place", "m")), location=near)

    async def test_patch_replacing_a_geometry(self) -> None:
        paris = GeometryPoint(*PARIS)
        async with orm_client("geo_place"):
            place = _place("pa")
            await place.save()
            await place.patch([{"op": "replace", "path": "/location", "value": paris}])
            _assert_geo(place, nested=False, location=paris)
            _assert_geo(await GeoPlace.objects().get("pa"), location=paris)
            _assert_native(await _raw(RecordID("geo_place", "pa")), location=paris)

    async def test_bulk_create(self) -> None:
        async with orm_client("geo_place"):
            created = await GeoPlace.objects().bulk_create([_place("b1"), _place("b2")])
            assert sorted(str(p.id) for p in created) == ["b1", "b2"]
            for place in created:
                _assert_geo(place)
            for key in ("b1", "b2"):
                _assert_geo(await GeoPlace.objects().get(key))
                _assert_native(await _raw(RecordID("geo_place", key)))

    async def test_get_or_create_with_a_geometry_criterion(self) -> None:
        here = GeometryPoint(*A)
        async with orm_client("geo_place"):
            await _place("seed").save()  # at B: the criterion does not match it
            made, created = await GeoPlace.objects().get_or_create(location=here, defaults={"name": "x", "entrance": here})
            assert created is True
            assert type(made.location) is GeometryPoint and made.location == here
            assert type(made.entrance) is GeometryPoint and made.entrance == here
            found, created = await GeoPlace.objects().get_or_create(location=here, defaults={"name": "y", "entrance": here})
            assert created is False
            assert type(found.location) is GeometryPoint and found.location == here and found.name == "x"
            rows = await GeoPlace.objects().filter(location=here).exec()
            assert [row.id for row in rows] == [found.id]

    async def test_edge_with_a_geometry_field(self) -> None:
        async with orm_client("geo_place", "geo_edge"):
            start, end = _place("a"), _place("b")
            await start.save()
            await end.save()
            edge = await start.relate(GeoLink, end, path=GeometryLine.parse_coordinates([A, B]))
            assert type(edge.path) is GeometryLine and _all_orm(edge.path)
            [found] = await GeoLink.objects().filter(path=edge.path).exec()
            assert type(found.path) is GeometryLine and _all_orm(found.path) and found.path == edge.path

    async def test_buffered_transaction(self) -> None:
        root = await _http_client()
        await root.query("REMOVE TABLE IF EXISTS geo_place;", {})
        _connect_http()
        try:
            async with SurrealDBConnectionManager.transaction() as tx:
                assert not tx.is_interactive
                await _place("bt").save(tx=tx)
            _assert_geo(await GeoPlace.objects().get("bt"))
        finally:
            await root.query("REMOVE TABLE IF EXISTS geo_place;", {})
            await SurrealDBConnectionManager.unset_connection()

    async def test_interactive_transaction(self) -> None:
        async with orm_client("geo_place"):
            if not await _native_txn_supported():
                pytest.skip("requires SurrealDB 3.x native transactions")
            async with SurrealDBConnectionManager.transaction() as tx:
                assert tx.is_interactive
                place = _place("it")
                await place.save(tx=tx)
                _assert_geo(await GeoPlace.objects(tx=tx).get("it"))
                await place.refresh(tx=tx)
                _assert_geo(place, nested=False)
            _assert_geo(await GeoPlace.objects().get("it"))

    async def test_computed_geometry(self) -> None:
        square = GeometryPolygon.parse_coordinates([RING])
        async with orm_client("geo_zone") as client:
            for ddl in GeoZone.computed_field_ddl():
                await client.query(ddl, {})
            centroid = await client.query("RETURN geo::centroid($p);", {"p": SdkPolygon.parse_coordinates([RING])})
            zone = GeoZone(id="z", area=square)
            await zone.save()
            assert type(zone.centroid) is GeometryPoint and zone.centroid == centroid
            fetched = await GeoZone.objects().get("z")
            assert type(fetched.centroid) is GeometryPoint and fetched.centroid == centroid
            await GeoZone(id="empty").save()
            empty = await GeoZone.objects().get("empty")
            assert empty.area is None and empty.centroid is None

    async def test_get_before_update_keeps_sub_model_geometries(self) -> None:
        async with orm_client("geo_place"):
            await _place("w").upsert()
            fresh = await GeoPlace.objects().get("w")
            await fresh.update()
            _assert_geo(await GeoPlace.objects().get("w"))

    @pytest.mark.xfail(
        strict=True,
        raises=ValidationError,
        reason="pre-existing: a write sync leaves sub-models as the row's dict; tracked in #228",
    )
    async def test_update_after_upsert_keeps_sub_model_geometries(self) -> None:
        async with orm_client("geo_place"):
            place = _place("x")
            await place.upsert()
            await place.update()
            _assert_geo(await GeoPlace.objects().get("x"))


class TestGeoStreamsE2E:
    """AC-9: a typed live stream and a change feed deliver instances holding ORM geometries."""

    async def test_live_stream(self) -> None:
        async with orm_client("geo_place") as client:
            await client.query("DEFINE TABLE geo_place SCHEMALESS;", {})
            async with GeoPlace.objects().live() as stream:
                await _place("l").save()
                [event] = await _take(stream, 1)
        assert event.validation_error is None, event.validation_error
        assert type(event.raw["location"]) is SdkPoint
        _assert_geo(event.instance)

    async def test_change_feed(self) -> None:
        here = GeometryPoint(*A)
        async with change_feed_database(GEO_CF_DATABASE) as client:
            await client.query("DEFINE TABLE geo_cf SCHEMALESS CHANGEFEED 1h;", {})
            stream = await GeoFeed.objects().changes(poll_interval=0.05).start()
            try:
                await retry_write(lambda: GeoFeed(id="a", location=here).save())
                [event] = await _take(stream, 1)
            finally:
                stream.stop()
        assert event.validation_error is None, event.validation_error
        assert type(event.instance.location) is GeometryPoint and event.instance.location == here


class TestLegacyGeoJsonE2E:
    """AC-10: a stored GeoJSON object reads into a geometry field, and ``update()`` then stores a native geometry."""

    async def test_update_stores_a_native_geometry(self) -> None:
        geojson = {"type": "Point", "coordinates": list(A)}
        rid = RecordID("geo_legacy", "x")
        async with orm_client("geo_legacy") as client:
            await client.query("CREATE $rid SET location = $map;", {"rid": rid, "map": geojson})
            assert await client.query("SELECT VALUE location.is_point() FROM ONLY $rid;", {"rid": rid}) is False
            loaded = await GeoLegacy.objects().get("x")
            assert type(loaded.location) is GeometryPoint and loaded.location == GeometryPoint(*A)
            await loaded.update()
            assert await client.query("SELECT VALUE location.is_point() FROM ONLY $rid;", {"rid": rid}) is True
            again = await GeoLegacy.objects().get("x")
            assert type(again.location) is GeometryPoint and again.location == GeometryPoint(*A)

    async def test_a_dict_field_keeps_geojson_an_object(self) -> None:
        geojson = {"type": "Point", "coordinates": list(A)}
        rid = RecordID("geo_legacy", "d")
        async with orm_client("geo_legacy") as client:
            await GeoLegacy(id="d", blob=geojson).save()
            stored = await client.query("SELECT VALUE blob FROM ONLY $rid;", {"rid": rid})
            assert type(stored) is dict and stored == geojson
            assert await client.query("SELECT VALUE blob.is_geometry() FROM ONLY $rid;", {"rid": rid}) is False
            loaded = await GeoLegacy.objects().get("d")
            assert type(loaded.blob) is dict and loaded.blob == geojson


# --- nearby() (spec §5.3, §6.3) --------------------------------------------------------------------------


def _nearby_sql(column: str, n: int) -> str:
    """The condition ``nearby()`` adds for ``column``, its two variables numbered from ``n``."""
    return f"({column}.is_point() AND geo::distance({column}, $_f{n}) <= $_f{n + 1})"


class TestNearbyCompile:
    """AC-11 (unit): the point and the distance are bound, the condition is guarded and every refusal comes first.

    ``nearby()`` checks its arguments when it is called, before anything reaches a server.
    """

    def test_bound_point_and_distance(self) -> None:
        qs = GeoPlace.objects().filter(name="a")
        assert qs.nearby("location", B, 5000) is qs
        query, variables = qs._compile_query()
        assert query == f"SELECT * FROM geo_place WHERE name = $_f0 AND {_nearby_sql('location', 1)};"
        assert variables["_f0"] == "a"
        assert type(variables["_f1"]) is GeometryPoint and variables["_f1"] == GeometryPoint(*B)
        assert type(variables["_f2"]) is float and variables["_f2"] == 5000.0

    def test_no_coordinate_in_the_text(self) -> None:
        query, _ = GeoPlace.objects().nearby("location", B, 5000)._compile_query()
        assert query == f"SELECT * FROM geo_place WHERE {_nearby_sql('location', 0)};"
        assert not re.search(r"\d", re.sub(r"\$_f\d+", "", query)), query

    def test_aliased_field_compiles_its_column(self) -> None:
        query, _ = GeoPlace.objects().nearby("pos", B, 10)._compile_query()
        assert query == f"SELECT * FROM geo_place WHERE {_nearby_sql('position', 0)};"

    def test_dotted_path(self) -> None:
        query, _ = GeoPlace.objects().nearby("addr.loc", B, 10)._compile_query()
        assert query == f"SELECT * FROM geo_place WHERE {_nearby_sql('addr.loc', 0)};"

    def test_two_calls_are_and_ed(self) -> None:
        query, variables = GeoPlace.objects().nearby("location", B, 5000).nearby("pos", A, 10)._compile_query()
        assert query == f"SELECT * FROM geo_place WHERE {_nearby_sql('location', 0)} AND {_nearby_sql('position', 2)};"
        assert (variables["_f0"], variables["_f1"]) == (GeometryPoint(*B), 5000.0)
        assert (variables["_f2"], variables["_f3"]) == (GeometryPoint(*A), 10.0)

    def test_after_keyword_filters_and_q_objects(self) -> None:
        query, variables = GeoPlace.objects().nearby("location", B, 1.5).filter(Q(n=1) | Q(n=2), name="a")._compile_query()
        assert query.endswith(f" AND {_nearby_sql('location', 3)};"), query
        assert query.startswith("SELECT * FROM geo_place WHERE name = $_f0 AND "), query
        assert (variables["_f3"], variables["_f4"]) == (GeometryPoint(*B), 1.5)

    def test_condition_reaches_every_where(self) -> None:
        qs = GeoPlace.objects().nearby("location", B, 5000)
        query, _ = qs._compile_aggregation_query("count()")
        assert query == f"SELECT count() FROM geo_place WHERE {_nearby_sql('location', 0)} GROUP ALL;"
        query, _ = GeoPlace.objects().values("name").annotate(c=Count()).nearby("location", B, 5000)._compile_group_by_query()
        assert f" WHERE {_nearby_sql('location', 0)} GROUP BY name;" in query

    @pytest.mark.parametrize(
        "point",
        [
            GeometryPoint(*B),
            SdkPoint(*B),
            B,
            list(B),
            {"type": "Point", "coordinates": list(B)},
            (Fraction(-7399, 100), Fraction(4075, 100)),
        ],
        ids=["orm", "sdk", "tuple", "list", "geojson", "fractions"],
    )
    def test_point_forms(self, point: Any) -> None:
        converted = geo._as_point(point)
        assert type(converted) is GeometryPoint and converted == GeometryPoint(*B)
        _, variables = GeoPlace.objects().nearby("location", point, 5000)._compile_query()
        assert type(variables["_f0"]) is GeometryPoint and variables["_f0"] == GeometryPoint(*B)

    def test_an_orm_point_is_kept(self) -> None:
        point = GeometryPoint(*B)
        assert geo._as_point(point) is point

    @pytest.mark.parametrize(
        ("model", "field"),
        [
            (GeoPlace, "location"),
            (GeoPlace, "entrance"),
            (GeoPlace, "anything"),
            (GeoPlace, "pos"),
            (GeoPlace, "name"),
            (GeoPlace, "many"),
            (GeoPlace, "undeclared"),
            (GeoPlace, "area.inner"),
            (GeoMixed, "location"),
        ],
        ids=["point", "class-point", "feature", "alias", "str", "list", "undeclared", "dotted", "any"],
    )
    def test_fields_accepted(self, model: Any, field: str) -> None:
        query, _ = model.objects().nearby(field, B, 5000)._compile_query()
        assert "geo::distance(" in query

    def test_a_field_accepting_points_and_polygons(self) -> None:
        model = _model(BaseSurrealModel, GeoField["point|polygon"])
        query, _ = model.objects().nearby("loc", B, 5000)._compile_query()
        assert _nearby_sql("loc", 0) in query

    @pytest.mark.parametrize(
        ("field", "kind"),
        [("area", "polygon"), ("path", "line"), ("stops", "multipoint"), ("zones", "multipolygon"), ("bag", "collection")],
    )
    def test_a_declared_non_point_field_is_refused(self, field: str, kind: str) -> None:
        qs = GeoPlace.objects()
        with pytest.raises(
            ValueError, match=rf"nearby\(\) measures distances between points; field '{field}' is declared as {kind}"
        ):
            qs.nearby(field, B, 5000)
        assert qs._nearby == []

    @pytest.mark.parametrize("field", ["loc; DROP", "", "location--", "a..b", "$x"])
    def test_a_bad_field_name_is_refused(self, field: str) -> None:
        with pytest.raises(ValueError, match="nearby field"):
            GeoPlace.objects().nearby(field, B, 5000)

    def test_a_field_name_must_be_a_string(self) -> None:
        with pytest.raises(TypeError, match="nearby"):
            GeoPlace.objects().nearby(3, B, 5000)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("point", "error"),
        [
            ("x", TypeError),
            (b"xy", TypeError),
            (None, TypeError),
            (3, TypeError),
            ((1,), TypeError),
            ((1, 2, 3), TypeError),
            ((True, 1), TypeError),
            (("1", "2"), TypeError),
            ((Decimal("1"), 2), TypeError),
            (GeometryPolygon.parse_coordinates([RING]), TypeError),
            (SdkPolygon.parse_coordinates([RING]), TypeError),
            ({"type": "Polygon", "coordinates": [[list(p) for p in RING]]}, InvalidGeometryError),
            ({"type": "Point", "coordinates": [1.0, 2.0, 3.0]}, InvalidGeometryError),
            ({"coordinates": [1.0, 2.0]}, InvalidGeometryError),
            ((math.nan, 1.0), InvalidGeometryError),
            ((math.inf, 1.0), InvalidGeometryError),
            ((10**400, 1.0), InvalidGeometryError),
        ],
        ids=[
            "str",
            "bytes",
            "none",
            "int",
            "one-item",
            "three-items",
            "bool",
            "text-pair",
            "decimal",
            "orm-polygon",
            "sdk-polygon",
            "geojson-polygon",
            "geojson-3d",
            "geojson-no-type",
            "nan",
            "inf",
            "too-large",
        ],
    )
    def test_a_bad_point_is_refused(self, point: Any, error: type[Exception]) -> None:
        qs = GeoPlace.objects()
        with pytest.raises(error) as caught:
            qs.nearby("location", point, 5000)
        assert issubclass(error, ValueError) == isinstance(caught.value, ValueError)
        assert qs._nearby == []

    @pytest.mark.parametrize(
        ("distance", "error"),
        [
            (True, TypeError),
            ("5", TypeError),
            (None, TypeError),
            (Decimal("5"), TypeError),
            (-1, ValueError),
            (-0.5, ValueError),
            (math.nan, ValueError),
            (math.inf, ValueError),
            (-math.inf, ValueError),
            (10**400, ValueError),
        ],
        ids=["bool", "str", "none", "decimal", "negative", "negative-float", "nan", "inf", "minus-inf", "too-large"],
    )
    def test_a_bad_distance_is_refused(self, distance: Any, error: type[Exception]) -> None:
        qs = GeoPlace.objects()
        with pytest.raises(error, match="max_distance"):
            qs.nearby("location", B, distance)
        assert qs._nearby == []

    @pytest.mark.parametrize("distance", [0, 0.0, Fraction(1, 2), 10**6])
    def test_distances_accepted(self, distance: Any) -> None:
        _, variables = GeoPlace.objects().nearby("location", B, distance)._compile_query()
        assert type(variables["_f1"]) is float and variables["_f1"] == float(distance)

    def test_changes_refuses_it(self) -> None:
        with pytest.raises(SurrealDbError, match=r"nearby\(\)"):
            GeoPlace.objects().nearby("location", B, 5000).changes(since=0)

    def test_live_inlines_the_point_and_the_distance(self) -> None:
        query = GeoPlace.objects().nearby("location", B, 5000)._compile_live(False)
        assert query == (
            "LIVE SELECT * FROM geo_place WHERE (location.is_point() AND geo::distance(location, "
            "{type: 'Point', coordinates: [-73.99, 40.75]}) <= 5000.0);"
        )


class TestNearbyE2E:
    """AC-11, both lines: on the mixed seed, ``nearby()`` returns exactly the points within the distance."""

    async def test_exactly_the_near_points(self) -> None:
        async with orm_client("geo_mixed") as client:
            await _seed_mixed(client)
            for point in (GeometryPoint(*B), SdkPoint(*B), B, {"type": "Point", "coordinates": list(B)}):
                assert _ids(await GeoMixed.objects().nearby("location", point, 5000).exec()) == {"near1", "near2"}
            assert await GeoMixed.objects().nearby("location", B, 5000).count() == 2
            assert _ids(await GeoMixed.objects().nearby("location", B, 5000).nearby("location", B, 1000).exec()) == {"near2"}
            assert _record_key(await GeoMixed.objects().nearby("location", B, 1000).get()) == "near2"
            assert await GeoMixed.objects().nearby("location", B, 100).exec() == []

    async def test_combined_with_filter_q_order_and_limit(self) -> None:
        async with orm_client("geo_mixed") as client:
            await _seed_mixed(client)

            def near() -> Any:
                return GeoMixed.objects().nearby("location", B, 5000)

            assert _ids(await near().filter(n=1).exec()) == {"near1"}
            assert _ids(await GeoMixed.objects().filter(Q(n=2) | Q(n=3)).nearby("location", B, 5000).exec()) == {"near2"}
            assert [_record_key(r) for r in await near().order_by("-n").exec()] == ["near2", "near1"]
            assert [_record_key(r) for r in await near().order_by("n").limit(1).exec()] == ["near1"]
            assert _record_key(await near().order_by("-n").first()) == "near2"

    async def test_bulk_update(self) -> None:
        async with orm_client("geo_mixed") as client:
            await _seed_mixed(client)
            assert await GeoMixed.objects().nearby("location", B, 5000).bulk_update(n=99) == 2
            assert _ids(await GeoMixed.objects().filter(n=99).exec()) == {"near1", "near2"}
            assert await GeoMixed.objects().count() == len(MIXED_KEYS)

    async def test_queryset_patch(self) -> None:
        async with orm_client("geo_mixed") as client:
            await _seed_mixed(client)
            patch = [{"op": "replace", "path": "/n", "value": 42}]
            assert await GeoMixed.objects().nearby("location", B, 5000).patch(patch) == 2
            assert _ids(await GeoMixed.objects().filter(n=42).exec()) == {"near1", "near2"}

    async def test_bulk_delete(self) -> None:
        async with orm_client("geo_mixed") as client:
            await _seed_mixed(client)
            assert await GeoMixed.objects().nearby("location", B, 5000).bulk_delete() == 2
            assert _ids(await GeoMixed.objects().all()) == set(MIXED_KEYS) - {"near1", "near2"}

    async def test_typed_model(self) -> None:
        async with orm_client("geo_place"):
            entrance = GeometryPoint(*A)
            await GeoPlace(id="near", entrance=entrance, location=GeometryPoint(*NEAR)).save()
            await GeoPlace(id="far", entrance=entrance, location=GeometryPoint(*PARIS), pos=GeometryPoint(*NEAR)).save()
            await GeoPlace(id="nowhere", entrance=entrance).save()
            [found] = await GeoPlace.objects().nearby("location", B, 5000).exec()
            assert isinstance(found, GeoPlace) and found.id == "near"
            assert type(found.location) is GeometryPoint and found.location == GeometryPoint(*NEAR)
            [aliased] = await GeoPlace.objects().nearby("pos", B, 5000).exec()
            assert aliased.id == "far" and type(aliased.pos) is GeometryPoint


class TestNearbyLiveE2E:
    """AC-11, both lines: in a live query only a point within the distance notifies (the far write comes first)."""

    async def test_only_the_near_point_notifies(self) -> None:
        entrance = GeometryPoint(*A)
        async with orm_client("geo_place") as client:
            await client.query("DEFINE TABLE geo_place SCHEMALESS;", {})
            async with GeoPlace.objects().nearby("location", B, 5000).live() as stream:
                await GeoPlace(id="far", entrance=entrance, location=GeometryPoint(*PARIS)).save()
                await GeoPlace(id="near1", entrance=entrance, location=GeometryPoint(*A)).save()
                [event] = await _take(stream, 1)
                await _assert_silent(stream)
        assert event.validation_error is None, event.validation_error
        assert event.instance.id == "near1"
        assert type(event.instance.location) is GeometryPoint and event.instance.location == GeometryPoint(*A)


# --- GeoDistance and per-record annotations (spec §5.4, §6.4) ------------------------------------------


def _distance_sql(column: str, n: int, alias: str) -> str:
    """The projection ``annotate(<alias>=GeoDistance(...))`` adds for ``column``, its point in ``$_gd<n>``."""
    return f"IF {column}.is_point() THEN geo::distance({column}, $_gd{n}) END AS {alias}"


#: Aliases a GeoDistance may not take on ``GeoPlace``, and the reason each refusal names.
RESERVED_ALIASES: dict[str, str] = {
    "name": "is a field of GeoPlace",
    "location": "is a field of GeoPlace",
    "pos": "is a field of GeoPlace",
    "position": "is the column of field 'pos'",
    "id": "record's id or an edge endpoint",
    "in": "record's id or an edge endpoint",
    "out": "record's id or an edge endpoint",
    "_dist": "starting with '_' or 'model_'",
    "model_dist": "starting with '_' or 'model_'",
    "model_config": "starting with '_' or 'model_'",
    "save": "is an attribute of GeoPlace",
    "objects": "is an attribute of GeoPlace",
    "get_id": "is an attribute of GeoPlace",
    "update": "is an attribute of GeoPlace",
}


class TestGeoDistanceCompile:
    """AC-12 (unit): the distance is a guarded projection with a bound point, snapshotted by ``annotate()``.

    Every refusal comes from ``annotate()`` (or ``values()``), before anything reaches a server.
    """

    def test_projection_after_the_star(self) -> None:
        qs = GeoPlace.objects()
        assert qs.annotate(dist=GeoDistance("location", B)) is qs
        query, variables = qs.order_by("dist")._compile_query()
        assert query == f"SELECT *, {_distance_sql('location', 0, 'dist')} FROM geo_place ORDER BY dist ASC;"
        assert type(variables["_gd0"]) is GeometryPoint and variables["_gd0"] == GeometryPoint(*B)
        assert not re.search(r"\d", re.sub(r"\$_gd\d+", "", query)), query

    def test_projection_after_the_selected_fields(self) -> None:
        query, _ = GeoPlace.objects().select("name", "pos").annotate(dist=GeoDistance("location", B))._compile_query()
        assert query == f"SELECT name, position, {_distance_sql('location', 0, 'dist')} FROM geo_place;"

    def test_with_filter_nearby_order_limit_offset_and_fetch(self) -> None:
        qs = (
            GeoPlace.objects()
            .filter(name="a")
            .nearby("location", B, 5000)
            .annotate(dist=GeoDistance("location", A))
            .order_by("-dist")
            .limit(5)
            .offset(1)
            .fetch("sub")
        )
        query, variables = qs._compile_query()
        assert query == (
            f"SELECT *, {_distance_sql('location', 0, 'dist')} FROM geo_place WHERE name = $_f0 AND "
            f"{_nearby_sql('location', 1)} ORDER BY dist DESC LIMIT 5 START 1 FETCH sub;"
        )
        assert variables["_gd0"] == GeometryPoint(*A) and variables["_f1"] == GeometryPoint(*B)

    def test_several_annotations_in_order_with_their_columns(self) -> None:
        qs = GeoPlace.objects().annotate(to_b=GeoDistance("pos", B)).annotate(to_a=GeoDistance("addr.loc", A))
        query, variables = qs._compile_query()
        assert query == (
            f"SELECT *, {_distance_sql('position', 0, 'to_b')}, {_distance_sql('addr.loc', 1, 'to_a')} FROM geo_place;"
        )
        assert (variables["_gd0"], variables["_gd1"]) == (GeometryPoint(*B), GeometryPoint(*A))

    @pytest.mark.parametrize("point", [GeometryPoint(*B), SdkPoint(*B), B, {"type": "Point", "coordinates": list(B)}])
    def test_the_point_is_bound_as_an_orm_point(self, point: Any) -> None:
        _, variables = GeoPlace.objects().annotate(dist=GeoDistance("location", point))._compile_query()
        assert type(variables["_gd0"]) is GeometryPoint and variables["_gd0"] == GeometryPoint(*B)

    def test_without_annotations_nothing_changes(self) -> None:
        assert GeoPlace.objects().select("name")._compile_query() == ("SELECT name FROM geo_place;", {})
        assert GeoPlace.objects()._compile_query() == ("SELECT * FROM geo_place;", {})

    def test_snapshot(self) -> None:
        distance = GeoDistance("location", B)
        qs = GeoPlace.objects().annotate(dist=distance)
        distance.field = "area"
        distance.point = GeometryPoint(*PARIS)
        query, variables = qs._compile_query()
        assert query == f"SELECT *, {_distance_sql('location', 0, 'dist')} FROM geo_place;"
        assert variables["_gd0"] == GeometryPoint(*B)

    def test_a_mutated_geodistance_is_checked_again(self) -> None:
        distance = GeoDistance("location", B)
        distance.point = "x"  # type: ignore[assignment]
        with pytest.raises(TypeError, match="point"):
            GeoPlace.objects().annotate(dist=distance)
        distance = GeoDistance("location", B)
        distance.field = 3  # type: ignore[assignment]
        with pytest.raises(TypeError, match="GeoDistance"):
            GeoPlace.objects().annotate(dist=distance)

    @pytest.mark.parametrize(
        "value", [3, "x", None, (-73.99, 40.75), GeometryPoint(*B)], ids=["int", "str", "none", "tuple", "point"]
    )
    def test_another_type_is_refused(self, value: Any) -> None:
        with pytest.raises(TypeError) as caught:
            GeoPlace.objects().annotate(dist=value)
        message = str(caught.value)
        assert "must be an Aggregation instance" in message and "GeoDistance" in message and "'dist'" in message

    @pytest.mark.parametrize(("alias", "reason"), list(RESERVED_ALIASES.items()), ids=list(RESERVED_ALIASES))
    def test_a_reserved_alias_is_refused(self, alias: str, reason: str) -> None:
        qs = GeoPlace.objects()
        with pytest.raises(ValueError, match=re.escape(reason)) as caught:
            qs.annotate(**{alias: GeoDistance("location", B)})
        assert repr(alias) in str(caught.value)
        assert qs._annotations == {} and qs._geo_annotations == {}

    def test_an_edge_reserves_its_endpoints(self) -> None:
        for alias, reason in (("in", "an edge endpoint"), ("out", "an edge endpoint"), ("in_", "is a field of GeoLink")):
            with pytest.raises(ValueError, match=re.escape(reason)):
                GeoLink.objects().annotate(**{alias: GeoDistance("spot", B)})

    def test_an_aggregation_may_still_take_those_aliases(self) -> None:
        query, _ = GeoPlace.objects().values("n").annotate(save=Count())._compile_group_by_query()
        assert query == "SELECT n, count() AS save FROM geo_place GROUP BY n;"

    @pytest.mark.parametrize("alias", ["dist", "distance", "d2", "Dist", "naming"])
    def test_free_aliases(self, alias: str) -> None:
        query, _ = GeoPlace.objects().annotate(**{alias: GeoDistance("location", B)})._compile_query()
        assert query.endswith(f" AS {alias} FROM geo_place;"), query

    @pytest.mark.parametrize("alias", ["", "a b", "1x", "x;y", "dist--"])
    def test_a_bad_alias_is_refused(self, alias: str) -> None:
        with pytest.raises(ValueError, match="alias"):
            GeoPlace.objects().annotate(**{alias: GeoDistance("location", B)})

    @pytest.mark.parametrize("field", ["loc; DROP", "", "location--", "a..b", "$x"])
    def test_a_bad_field_name_is_refused(self, field: str) -> None:
        qs = GeoPlace.objects()
        with pytest.raises(ValueError, match="GeoDistance field"):
            qs.annotate(dist=GeoDistance(field, B))
        assert qs._annotations == {} and qs._geo_annotations == {}

    @pytest.mark.parametrize(("field", "kind"), [("area", "polygon"), ("path", "line"), ("bag", "collection")])
    def test_a_declared_non_point_field_is_refused(self, field: str, kind: str) -> None:
        with pytest.raises(
            ValueError, match=rf"GeoDistance measures distances between points; field '{field}' is declared as {kind}"
        ):
            GeoPlace.objects().annotate(dist=GeoDistance(field, B))

    @pytest.mark.parametrize(
        ("model", "field"),
        [
            (GeoPlace, "anything"),
            (GeoPlace, "name"),
            (GeoPlace, "undeclared"),
            (GeoPlace, "area.inner"),
            (GeoMixed, "location"),
        ],
        ids=["feature", "str", "undeclared", "dotted", "any"],
    )
    def test_fields_accepted(self, model: Any, field: str) -> None:
        query, _ = model.objects().annotate(dist=GeoDistance(field, B))._compile_query()
        assert _distance_sql(model.to_db_field(field), 0, "dist") in query

    def test_mixing_with_an_aggregation_is_refused(self) -> None:
        def mixed(qs: Any) -> None:
            with pytest.raises(ValueError, match="a GeoDistance is computed per record"):
                qs()

        mixed(lambda: GeoPlace.objects().annotate(n_count=Count(), dist=GeoDistance("location", B)))
        qs = GeoPlace.objects().annotate(n_count=Count())
        mixed(lambda: qs.annotate(dist=GeoDistance("location", B)))
        assert list(qs._annotations) == ["n_count"] and qs._geo_annotations == {}
        qs = GeoPlace.objects().annotate(dist=GeoDistance("location", B))
        mixed(lambda: qs.annotate(n_count=Count()))
        assert list(qs._annotations) == ["dist"] and list(qs._geo_annotations) == ["dist"]

    def test_mixing_with_values_is_refused(self) -> None:
        with pytest.raises(ValueError, match="a GeoDistance is computed per record"):
            GeoPlace.objects().values("name").annotate(dist=GeoDistance("location", B))
        qs = GeoPlace.objects().annotate(dist=GeoDistance("location", B))
        with pytest.raises(ValueError, match="a GeoDistance is computed per record"):
            qs.values("name")
        assert qs._group_by_fields == []
        with pytest.raises(ValueError, match="a GeoDistance is computed per record"):
            qs._compile_group_by_query()

    async def test_exec_refuses_a_mixed_queryset(self) -> None:
        qs = GeoPlace.objects().annotate(dist=GeoDistance("location", B))
        qs._group_by_fields = ["name"]
        with pytest.raises(ValueError, match="a GeoDistance is computed per record"):
            await qs.exec()

    def test_an_aggregation_replacing_a_distance(self) -> None:
        qs = GeoPlace.objects().annotate(dist=GeoDistance("location", B)).annotate(dist=Count())
        assert qs._geo_annotations == {}
        query, _ = qs._compile_group_by_query()
        assert query == "SELECT count() AS dist FROM geo_place GROUP ALL;"

    def test_aggregations_unchanged(self) -> None:
        query, _ = GeoPlace.objects().values("name").annotate(c=Count())._compile_group_by_query()
        assert query == "SELECT name, count() AS c FROM geo_place GROUP BY name;"

    def test_the_alias_is_never_renamed_in_a_dict_row(self) -> None:
        qs = GeoPlace.objects().annotate(dist=GeoDistance("location", B))
        assert qs._py_keyed({"position": 1, "dist": 2.0}) == {"pos": 1, "dist": 2.0}

    def test_live_and_changes_refuse_it(self) -> None:
        qs = GeoPlace.objects().annotate(dist=GeoDistance("location", B))
        with pytest.raises(SurrealDbError, match=r"annotate\(\)"):
            qs._compile_live(False)
        with pytest.raises(SurrealDbError, match=r"annotate\(\)"):
            qs.changes(since=0)


class TestGeoDistanceE2E:
    """AC-12, both lines: distances in metres, ``None`` off points, ordered by the alias, never written back."""

    async def test_with_nearby_ordered_by_distance(self) -> None:
        async with orm_client("geo_mixed") as client:
            await _seed_mixed(client)
            rows = (
                await GeoMixed.objects()
                .annotate(dist=GeoDistance("location", B))
                .nearby("location", B, 5000)
                .order_by("dist")
                .exec()
            )
        assert [type(r) for r in rows] == [GeoMixed, GeoMixed]
        assert [_record_key(r) for r in rows] == ["near2", "near1"]
        assert rows[0].dist == pytest.approx(NEAR_B_METRES, abs=1e-6)
        assert rows[1].dist == pytest.approx(AB_METRES, abs=1e-6)
        assert all(type(r.dist) is float for r in rows)

    async def test_descending_puts_the_non_points_last(self) -> None:
        async with orm_client("geo_mixed") as client:
            await _seed_mixed(client)
            rows = await GeoMixed.objects().annotate(dist=GeoDistance("location", B)).order_by("-dist").exec()
            ascending = await GeoMixed.objects().annotate(dist=GeoDistance("location", B)).order_by("dist").exec()
        assert [_record_key(r) for r in rows[:3]] == ["far", "near1", "near2"]
        assert rows[0].dist > 5_000_000 and rows[1].dist == pytest.approx(AB_METRES, abs=1e-6)
        assert {_record_key(r) for r in rows[3:]} == {"none", "nullish", "text", "obj", "poly"}
        assert all(r.dist is None for r in rows[3:])
        assert all(r.dist is None for r in ascending[:5])
        assert [_record_key(r) for r in ascending[5:]] == ["near2", "near1", "far"]

    async def test_first_and_get_carry_the_distance(self) -> None:
        async with orm_client("geo_mixed") as client:
            await _seed_mixed(client)
            qs = GeoMixed.objects().annotate(dist=GeoDistance("location", B)).nearby("location", B, 5000).order_by("dist")
            first = await qs.first()
            only = await GeoMixed.objects().annotate(dist=GeoDistance("location", B)).filter(n=1).get()
        assert _record_key(first) == "near2" and first.dist == pytest.approx(NEAR_B_METRES, abs=1e-6)
        assert _record_key(only) == "near1" and only.dist == pytest.approx(AB_METRES, abs=1e-6)

    async def test_typed_instances_and_no_write_back(self) -> None:
        async with orm_client("geo_place"):
            entrance = GeometryPoint(*A)
            await GeoPlace(id="near", name="near", entrance=entrance, location=GeometryPoint(*NEAR)).save()
            await GeoPlace(id="a", name="a", entrance=entrance, location=GeometryPoint(*A)).save()
            near, at_a = await GeoPlace.objects().annotate(dist=GeoDistance("location", B)).order_by("dist").exec()
            assert isinstance(near, GeoPlace) and (near.id, at_a.id) == ("near", "a")
            assert type(near.location) is GeometryPoint and near.location == GeometryPoint(*NEAR)
            assert type(near.dist) is float and near.dist == pytest.approx(NEAR_B_METRES, abs=1e-6)
            assert at_a.dist == pytest.approx(AB_METRES, abs=1e-6)
            assert "dist" not in near.model_dump() and "dist" not in near.model_dump(mode="json")
            near.name = "renamed"
            await near.update()
            row = await _raw(RecordID("geo_place", "near"))
            assert row["name"] == "renamed" and "dist" not in row
            await at_a.upsert()
            assert "dist" not in await _raw(RecordID("geo_place", "a"))

    async def test_extra_allow_keeps_the_distance_off_the_record(self) -> None:
        async with orm_client("geo_loose") as client:
            await client.query("CREATE geo_loose:p SET location = $v, note = 'kept';", {"v": SdkPoint(*A)})
            [loose] = await GeoLoose.objects().annotate(dist=GeoDistance("location", B)).exec()
            assert isinstance(loose, GeoLoose) and loose.dist == pytest.approx(AB_METRES, abs=1e-6)
            assert loose.model_extra == {"note": "kept"}
            assert "dist" not in loose.model_dump()
            await loose.update()
            row = await _raw(RecordID("geo_loose", "p"))
            assert row["note"] == "kept" and "dist" not in row

    async def test_extra_forbid_validates(self) -> None:
        async with orm_client("geo_strict"):
            await GeoStrict(id="p", location=GeometryPoint(*NEAR)).save()
            [strict] = await GeoStrict.objects().annotate(dist=GeoDistance("location", B)).exec()
            assert isinstance(strict, GeoStrict) and strict.dist == pytest.approx(NEAR_B_METRES, abs=1e-6)
            assert strict.model_extra is None
            await strict.update()
            assert "dist" not in await _raw(RecordID("geo_strict", "p"))

    async def test_rows_that_do_not_validate_come_back_as_dicts(self) -> None:
        async with orm_client("geo_place"):
            entrance = GeometryPoint(*A)
            await GeoPlace(id="near", name="near", entrance=entrance, location=GeometryPoint(*NEAR)).save()
            await GeoPlace(id="a", name="a", entrance=entrance, location=GeometryPoint(*A)).save()
            await GeoPlace(id="nowhere", name="nowhere", entrance=entrance).save()
            rows = await GeoPlace.objects().select("name").annotate(dist=GeoDistance("location", B)).order_by("-dist").exec()
        assert [type(r) for r in rows] == [dict, dict, dict]
        assert [set(r) for r in rows] == [{"name", "dist"}] * 3
        assert [r["name"] for r in rows] == ["a", "near", "nowhere"]
        assert rows[0]["dist"] == pytest.approx(AB_METRES, abs=1e-6)
        assert rows[1]["dist"] == pytest.approx(NEAR_B_METRES, abs=1e-6)
        assert rows[2]["dist"] is None


# --- spatial lookups inside / outside / intersects (spec §5.5, §6.8) ------------------------------------

#: The RING polygon (the midtown box) in the three forms a lookup takes, and its live-query literal.
ZONE = GeometryPolygon.parse_coordinates([RING])
ZONE_TEXT = "{type: 'Polygon', coordinates: [[[-74.0, 40.7], [-73.9, 40.7], [-73.9, 40.8], [-74.0, 40.8], [-74.0, 40.7]]]}"
#: A north–south line at longitude -73.993: it crosses the HOLE polygon and no seeded point.
CROSSING = [(-73.993, 40.70), (-73.993, 40.76)]
#: The sets the spatial operators give on the mixed seed, measured on 2.7.0, 3.1.5 and 3.3.4 (spec §3.4).
INSIDE_KEYS = {"near1", "near2", "poly"}
OUTSIDE_KEYS = {"far", "none", "nullish", "obj", "text"}
#: ``LOOKUP_OPERATORS`` as v0.23.0 shipped it: the spatial lookups live in a mapping of their own.
LOOKUP_OPERATORS_V0_23 = {
    "exact": "=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "in": "IN",
    "not_in": "NOT IN",
    "like": "LIKE",
    "ilike": "ILIKE",
    "contains": "CONTAINS",
    "not_contains": "CONTAINSNOT",
    "containsall": "CONTAINSALL",
    "containsany": "CONTAINSANY",
    "startswith": "STARTSWITH",
    "endswith": "ENDSWITH",
    "match": "MATCH",
    "regex": "REGEX",
    "isnull": "IS",
}
SPATIAL = [("inside", "INSIDE"), ("outside", "OUTSIDE"), ("intersects", "INTERSECTS")]


def _forms(value: Geometry) -> list[Any]:
    """``value`` as an ORM geometry, the SDK's equal value and its GeoJSON object."""
    return [value, value._to_sdk(), value.to_geojson()]


class TestSpatialLookupCompile:
    """AC-13 (unit): the spatial lookups bind one geometry, refuse what is not one and write no ``$`` of their own."""

    def test_the_operator_mappings(self) -> None:
        assert constants.GEOMETRY_LOOKUP_OPERATORS == {"inside": "INSIDE", "outside": "OUTSIDE", "intersects": "INTERSECTS"}
        assert constants.LOOKUP_OPERATORS == LOOKUP_OPERATORS_V0_23

    @pytest.mark.parametrize(("lookup", "op"), SPATIAL)
    def test_one_bound_geometry(self, lookup: str, op: str) -> None:
        query, variables = GeoPlace.objects().filter(**{f"location__{lookup}": ZONE})._compile_query()
        assert query == f"SELECT * FROM geo_place WHERE location {op} $_f0;"
        assert variables == {"_f0": ZONE} and variables["_f0"] is ZONE

    @pytest.mark.parametrize(("lookup", "op"), SPATIAL)
    def test_after_other_filters(self, lookup: str, op: str) -> None:
        query, variables = GeoPlace.objects().filter(name="a", **{f"area__{lookup}": ZONE}, n=2)._compile_query()
        assert query == f"SELECT * FROM geo_place WHERE name = $_f0 AND area {op} $_f1 AND n = $_f2;"
        assert variables == {"_f0": "a", "_f1": ZONE, "_f2": 2}

    def test_aliased_field_compiles_its_column(self) -> None:
        query, _ = GeoPlace.objects().filter(pos__inside=ZONE)._compile_query()
        assert query == "SELECT * FROM geo_place WHERE position INSIDE $_f0;"

    @pytest.mark.parametrize("kind", KINDS)
    def test_a_geojson_value_becomes_the_orm_value(self, kind: str) -> None:
        expected = _orm(kind)
        _, variables = GeoPlace.objects().filter(anything__intersects=expected.to_geojson())._compile_query()
        assert type(variables["_f0"]) is ORM_CLASSES[kind] and variables["_f0"] == expected

    @pytest.mark.parametrize("kind", KINDS)
    def test_orm_and_sdk_values_are_kept(self, kind: str) -> None:
        for value in (_orm(kind), _sdk(kind)):
            assert geo._as_geometry(value, "inside") is value
            _, variables = GeoPlace.objects().filter(anything__inside=value)._compile_query()
            assert variables["_f0"] is value

    def test_a_variable_reference(self) -> None:
        qs = GeoPlace.objects().filter(location__inside=Var("z")).variables(z=ZONE)
        query, variables = qs._compile_query()
        assert query == "SELECT * FROM geo_place WHERE location INSIDE $z;"
        assert variables == {"z": ZONE}

    def test_q_objects(self) -> None:
        query, variables = GeoPlace.objects().filter(Q(location__inside=ZONE) | Q(n=3))._compile_query()
        assert query == "SELECT * FROM geo_place WHERE (location INSIDE $_f0 OR n = $_f1);"
        assert variables == {"_f0": ZONE, "_f1": 3}
        query, variables = GeoPlace.objects().filter(~Q(location__inside=ZONE.to_geojson()))._compile_query()
        assert query == "SELECT * FROM geo_place WHERE NOT (location INSIDE $_f0);"
        assert type(variables["_f0"]) is GeometryPolygon and variables["_f0"] == ZONE
        query, _ = GeoPlace.objects().filter(Q(pos__outside=ZONE))._compile_query()
        assert query == "SELECT * FROM geo_place WHERE position OUTSIDE $_f0;"

    @pytest.mark.parametrize("lookup", ["inside", "outside", "intersects"])
    @pytest.mark.parametrize(
        "value",
        [[1, 2], (1, 2), "x", "$z", None, 3, 1.5, b"xy", {1, 2}, RecordID("geo_zone", "z")],
        ids=["list", "tuple", "str", "dollar-str", "none", "int", "float", "bytes", "set", "record-id"],
    )
    def test_a_value_that_is_not_a_geometry_is_refused(self, lookup: str, value: Any) -> None:
        message = rf"'{lookup}' needs a geometry .*use __in for membership in a list"
        with pytest.raises(ValueError, match=message):
            GeoPlace.objects().filter(**{f"location__{lookup}": value})._compile_query()
        with pytest.raises(ValueError, match=message):
            Q(**{f"location__{lookup}": value}).to_sql()

    def test_a_model_instance_is_refused(self) -> None:
        zone = GeoZone(id="z", area=ZONE)
        with pytest.raises(ValueError, match="'inside' needs a geometry"):
            GeoPlace.objects().filter(location__inside=zone)._compile_query()

    @pytest.mark.parametrize(
        "geojson",
        [
            {"type": "Polygon", "coordinates": []},
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [1.0, 2.0]}},
            {"coordinates": [1.0, 2.0]},
            {"type": "Point", "coordinates": [math.nan, 2.0]},
        ],
        ids=["empty-polygon", "feature", "no-type", "nan"],
    )
    def test_a_bad_geojson_object_is_refused(self, geojson: dict[str, Any]) -> None:
        with pytest.raises(InvalidGeometryError):
            GeoPlace.objects().filter(location__inside=geojson)._compile_query()

    @pytest.mark.parametrize("lookup", ["inside", "outside", "intersects"])
    @pytest.mark.parametrize("value", [ZONE, Var("z")], ids=["geometry", "var"])
    def test_refused_on_the_record_id_and_the_endpoints(self, lookup: str, value: Any) -> None:
        with pytest.raises(ValueError, match=rf"'{lookup}' cannot be applied to the record id column 'id': .*not a geometry"):
            GeoPlace.objects().filter(**{f"id__{lookup}": value})._compile_query()
        with pytest.raises(ValueError, match="not a geometry"):
            Q(**{f"id__{lookup}": value}).to_sql()
        for key, column in (("in_", "in"), ("out", "out")):
            with pytest.raises(
                ValueError, match=rf"'{lookup}' cannot be applied to the edge endpoint column '{column}': .*not a geometry"
            ):
                GeoLink.objects().filter(**{f"{key}__{lookup}": value})._compile_query()

    def test_an_edge_geometry_column_is_accepted(self) -> None:
        query, _ = GeoLink.objects().filter(path__intersects=ZONE)._compile_query()
        assert query == "SELECT * FROM geo_edge WHERE path INTERSECTS $_f0;"

    def test_a_bad_field_name_is_refused(self) -> None:
        with pytest.raises(ValueError, match="Invalid filter field"):
            build_filter_condition("loc; DROP", "inside", ZONE, 0)

    def test_an_unknown_lookup_is_still_refused(self) -> None:
        with pytest.raises(ValueError, match="Unsupported lookup type: 'within'"):
            build_filter_condition("location", "within", ZONE, 0)
        with pytest.raises(ValueError, match="Unsupported lookup type: 'inside_of'"):
            GeoPlace.objects().filter(location__inside_of=ZONE)._compile_query()

    @pytest.mark.parametrize(("lookup", "op"), SPATIAL)
    def test_live_inlines_the_geometry(self, lookup: str, op: str) -> None:
        for value in _forms(ZONE):
            query = GeoPlace.objects().filter(**{f"location__{lookup}": value})._compile_live(False)
            assert query == f"LIVE SELECT * FROM geo_place WHERE location {op} {ZONE_TEXT};"
        query = GeoPlace.objects().filter(~Q(location__inside=ZONE))._compile_live(False)
        assert query == f"LIVE SELECT * FROM geo_place WHERE NOT (location INSIDE {ZONE_TEXT});"

    def test_where_builders_emit_dollar_only_as_references(self) -> None:
        """The invariant of ``tests/test_surql_literal.py`` (every lookup writes ``$`` only as a reference), with geometries."""
        reference = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")
        bound = re.compile(r"\$_f\d+")
        for lookup in constants.GEOMETRY_LOOKUP_OPERATORS:
            for value in [*_forms(ZONE), *_forms(GeometryLine.parse_coordinates(CROSSING))]:
                sql, variables, counter = build_filter_condition("field", lookup, value, 0, "T")
                assert reference.findall(sql) == ["$_f0"] and counter == 1, (lookup, sql)
                assert "$" not in reference.sub("", sql), (lookup, sql)
                inlined = inline_variables(sql, variables)
                assert not bound.search(inlined) and "$" not in inlined, (lookup, inlined)
        sql, variables, _ = (Q(field__inside=ZONE) | ~Q(field__outside=ZONE.to_geojson())).to_sql(0, "T")
        assert reference.findall(sql) == ["$_f0", "$_f1"]
        assert "$" not in inline_variables(sql, variables)


class TestSpatialLookupE2E:
    """AC-13, both lines: on the mixed seed the spatial lookups give the sets SurrealDB's operators give (spec §3.4).

    ``outside`` keeps SurrealQL's semantics: it also holds for a record without a geometry.
    """

    async def test_inside(self) -> None:
        async with orm_client("geo_mixed") as client:
            await _seed_mixed(client)
            for zone in _forms(ZONE):
                assert _ids(await GeoMixed.objects().filter(location__inside=zone).exec()) == INSIDE_KEYS
            assert await GeoMixed.objects().filter(location__inside=ZONE).count() == len(INSIDE_KEYS)

    async def test_outside_includes_the_rows_without_a_geometry(self) -> None:
        async with orm_client("geo_mixed") as client:
            await _seed_mixed(client)
            for zone in _forms(ZONE):
                assert _ids(await GeoMixed.objects().filter(location__outside=zone).exec()) == OUTSIDE_KEYS

    async def test_intersects(self) -> None:
        async with orm_client("geo_mixed") as client:
            await _seed_mixed(client)
            for zone in _forms(ZONE):
                assert _ids(await GeoMixed.objects().filter(location__intersects=zone).exec()) == INSIDE_KEYS
            for line in _forms(GeometryLine.parse_coordinates(CROSSING)):
                assert _ids(await GeoMixed.objects().filter(location__intersects=line).exec()) == {"poly"}

    async def test_contains_a_point(self) -> None:
        async with orm_client("geo_mixed") as client:
            await _seed_mixed(client)
            for point in (GeometryPoint(-73.992, 40.747), SdkPoint(-73.992, 40.747)):
                assert _ids(await GeoMixed.objects().filter(location__contains=point).exec()) == {"poly"}

    async def test_q_objects(self) -> None:
        async with orm_client("geo_mixed") as client:
            await _seed_mixed(client)
            for zone in _forms(ZONE):
                assert _ids(await GeoMixed.objects().filter(~Q(location__inside=zone)).exec()) == OUTSIDE_KEYS
            either = Q(location__inside=ZONE) | Q(n=MIXED_KEYS.index("far") + 1)
            assert _ids(await GeoMixed.objects().filter(either).exec()) == INSIDE_KEYS | {"far"}
            assert _ids(await GeoMixed.objects().filter(Q(location__inside=ZONE), n=1).exec()) == {"near1"}

    async def test_a_variable_reference(self) -> None:
        async with orm_client("geo_mixed") as client:
            await _seed_mixed(client)
            qs = GeoMixed.objects().filter(location__inside=Var("zone")).variables(zone=ZONE)
            assert _ids(await qs.exec()) == INSIDE_KEYS

    async def test_combined_with_nearby_and_bulk_update(self) -> None:
        async with orm_client("geo_mixed") as client:
            await _seed_mixed(client)
            assert _ids(await GeoMixed.objects().filter(location__inside=ZONE).nearby("location", B, 1000).exec()) == {"near2"}
            assert await GeoMixed.objects().filter(location__intersects=ZONE).bulk_update(n=77) == len(INSIDE_KEYS)
            assert _ids(await GeoMixed.objects().filter(n=77).exec()) == INSIDE_KEYS

    async def test_typed_model(self) -> None:
        async with orm_client("geo_place", "geo_zone"):
            entrance = GeometryPoint(*A)
            await GeoPlace(id="in", entrance=entrance, location=GeometryPoint(*NEAR)).save()
            await GeoPlace(id="out", entrance=entrance, location=GeometryPoint(*PARIS)).save()
            [found] = await GeoPlace.objects().filter(location__inside=ZONE).exec()
            assert isinstance(found, GeoPlace) and found.id == "in"
            assert type(found.location) is GeometryPoint and found.location == GeometryPoint(*NEAR)
            await GeoZone(id="midtown", area=ZONE).save()
            await GeoZone(
                id="paris", area=GeometryPolygon.parse_coordinates([[(2.3, 48.8), (2.4, 48.8), (2.4, 48.9), (2.3, 48.8)]])
            ).save()
            [zone] = await GeoZone.objects().filter(area__contains=found.location).exec()
            assert zone.id == "midtown" and type(zone.area) is GeometryPolygon and zone.area == ZONE
            [crossed] = await GeoZone.objects().filter(area__intersects=GeometryLine.parse_coordinates(CROSSING)).exec()
            assert crossed.id == "midtown"


class TestSpatialLookupLiveE2E:
    """AC-13, both lines: in a live query only a point inside the polygon notifies (the outside write comes first)."""

    async def test_only_the_inside_point_notifies(self) -> None:
        entrance = GeometryPoint(*A)
        async with orm_client("geo_place") as client:
            await client.query("DEFINE TABLE geo_place SCHEMALESS;", {})
            async with GeoPlace.objects().filter(location__inside=ZONE).live() as stream:
                await GeoPlace(id="far", entrance=entrance, location=GeometryPoint(*PARIS)).save()
                await GeoPlace(id="near1", entrance=entrance, location=GeometryPoint(*A)).save()
                [event] = await _take(stream, 1)
                await _assert_silent(stream)
        assert event.validation_error is None, event.validation_error
        assert event.instance.id == "near1"
        assert type(event.instance.location) is GeometryPoint and event.instance.location == GeometryPoint(*A)


# --- README (spec §11, AC-15; P1, P2) --------------------------------------------------------------------

_README = Path(__file__).resolve().parents[1] / "README.md"

#: Section 25's example tables and the test tables they run against: everything else runs as written.
_README_TABLES = {"restaurant": "geo_readme_restaurant", "zone": "geo_readme_zone", "route": "geo_readme_route"}

#: D16: the public docs never describe what happens to the client when the SDK cannot read a value.
#: The phrases v0.23.0's README test bans, joined here so that the source itself never spells them.
_D16_PHRASES = tuple(
    " ".join(words) for words in (("connection", "lost"), ("connection", "is", "lost"), ("drops", "the", "connection"))
)

#: The last cell of the behaviour-table rows v0.24.0 adds.
_V0_24_0_SINCE = ("v0.24.0", "pre-existing (documented in v0.24.0)")

#: The subsections of section 25 that section 24 links (P2): each heading exists exactly once.
_LINKED_HEADINGS = ("#### Rich values inside sub-models", "#### Geometries the SDK cannot read")

#: What the README's distance example shows: SurrealDB's ``geo::distance`` from Times Square, rounded.
_CLOSEST = [("Corner Pizza", 411), ("Park Burgers", 1826), ("Village Deli", 3365)]


def _readme() -> str:
    return _README.read_text(encoding="utf-8")


def _readme_part(heading: str) -> str:
    """The README text from the ``heading`` line up to the next ``##``/``###`` heading."""
    match = re.search(rf"^{re.escape(heading)}\n.*?(?=^#{{2,3}} )", _readme(), re.MULTILINE | re.DOTALL)
    assert match, f"README has no {heading!r} section"
    return match.group(0)


def _section_25() -> str:
    return _readme_part("### 25. Geospatial fields")


def _subsection(heading: str, text: str | None = None) -> str:
    """The ``heading`` line of ``text`` (the README by default) up to the next heading of level 2 to 4, or the end."""
    source = _readme() if text is None else text
    match = re.search(rf"^{re.escape(heading)}\n.*?(?=^#{{2,4}} |\Z)", source, re.MULTILINE | re.DOTALL)
    assert match, f"README has no {heading!r} part"
    return match.group(0)


def _normalised(text: str) -> str:
    return " ".join(text.split())


def _table_rows(text: str) -> list[tuple[str, list[str]]]:
    """The body rows of the first table in ``text``: each raw line and its cells."""
    tables = re.findall(r"(?:^\|.*\n)+", text, re.MULTILINE)
    assert tables, "no table"
    lines = [line.strip() for line in tables[0].splitlines()]
    return [(line, [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip("|"))]) for line in lines[2:]]


def _v0_24_0_rows() -> list[tuple[str, list[str]]]:
    """The behaviour-table rows v0.24.0 adds: the two ``v0.24.0`` rows and the retroactive untyped-field row."""
    rows = _table_rows(_readme_part("### ORM behaviour: SurrealDB 2.x vs 3.x"))
    return [(line, cells) for line, cells in rows if cells[-1] in _V0_24_0_SINCE]


def _section_25_examples() -> list[str]:
    """Section 25's ``python`` blocks in order, on the ``geo_readme_`` tables."""
    blocks: list[str] = re.findall(r"^```python\n(.*?)^```$", _section_25(), re.MULTILINE | re.DOTALL)
    assert blocks, "README section 25 has no example"
    examples: list[str] = []
    renamed = dict.fromkeys(_README_TABLES, 0)
    for code in blocks:
        for table, test_table in _README_TABLES.items():
            code, count = re.subn(rf'table_name="{table}"', f'table_name="{test_table}"', code)
            renamed[table] += count
        examples.append(code)
    assert all(renamed.values()), f"section 25 no longer names these example tables: {renamed!r}"
    return examples


def _names(rows: Any) -> list[str]:
    return sorted(row.name for row in rows)


class TestReadmeGeoExamplesE2E:
    """AC-15: every example of README section 25 runs as written (its table names aside), on both lines."""

    async def test_section_25_examples(self) -> None:
        namespace: dict[str, Any] = {"__name__": "readme_section_25"}
        async with orm_client(*_README_TABLES.values()) as client:
            for code in _section_25_examples():
                compiled = compile(code, "<README section 25>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                # A doctest of this repository's own README (trusted text), top-level awaits included.
                outcome = eval(compiled, namespace)
                if inspect.iscoroutine(outcome):
                    await outcome

            # What the examples' comments say.
            restaurant, zone, route = namespace["Restaurant"], namespace["Zone"], namespace["Route"]
            times_square = namespace["times_square"]
            assert times_square.to_geojson() == {"type": "Point", "coordinates": [-73.9855, 40.758]}
            assert str(times_square) == "{type: 'Point', coordinates: [-73.9855, 40.758]}"
            assert Geometry.from_geojson({"type": "Point", "coordinates": [-73.9855, 40.758]}) == times_square

            # Every way of giving a point stored a native one, read back as the ORM's class.
            for key in ("pizza", "burgers", "deli", "bistro"):
                loaded = await restaurant.objects().get(key)
                assert type(loaded.location) is GeometryPoint, key
                rid = loaded._record_id()
                assert await client.query("SELECT VALUE location.is_point() FROM ONLY $rid;", {"rid": rid}) is True, key
            deli = await restaurant.objects().get("deli")
            assert deli.location == GeometryPoint(-73.9969, 40.729)
            assert deli.model_dump()["location"] is deli.location
            assert deli.model_dump(mode="json")["location"] == {"type": "Point", "coordinates": [-73.9969, 40.729]}
            assert (await restaurant.objects().get("truck")).location is None

            assert _names(namespace["near"]) == ["Corner Pizza", "Park Burgers"]
            closest = namespace["closest"]
            assert [(r.name, round(r.dist)) for r in closest] == _CLOSEST
            for found in closest:
                # The raw client sends SDK values only: the ORM's are converted by the ORM's own calls.
                points = {"a": SdkPoint(*times_square.get_coordinates()), "b": SdkPoint(*found.location.get_coordinates())}
                metres = await client.query("RETURN geo::distance($a, $b);", points)
                assert type(found.dist) is float and found.dist == pytest.approx(metres, abs=1e-6)
            farthest_first = namespace["farthest_first"]
            assert farthest_first[0].name == "Paris Bistro"
            assert (farthest_first[-1].name, farthest_first[-1].dist) == ("Food Truck", None)

            assert _names(namespace["inside"]) == ["Corner Pizza"]
            assert _names(namespace["outside"]) == ["Food Truck", "Paris Bistro", "Park Burgers", "Village Deli"]
            assert _names(namespace["zones"]) == ["Midtown"]
            assert _names(namespace["crossing"]) == ["Broadway"]

            event = namespace["event"]
            assert event.validation_error is None and event.instance.name == "Hot Dog Cart"
            assert type(event.instance.location) is GeometryPoint

            assert _names(namespace["same_spot"]) == ["Corner Pizza"]
            pizza = await restaurant.objects().get("pizza")
            assert pizza.location == GeometryPoint(-73.9876, 40.7548)
            assert type(pizza.entrance) is GeometryPoint and pizza.entrance == GeometryPoint(-73.9877, 40.7546)

            broadway = await route.objects().get("broadway")
            assert type(broadway.path) is GeometryLine and broadway.path == namespace["broadway"]
            assert [type(stop) for stop in broadway.stops] == [GeometryPoint]

            midtown = await zone.objects().get("midtown")
            assert type(midtown.centroid) is GeometryPoint
            assert midtown.centroid.get_coordinates() == pytest.approx((-73.985, 40.755), abs=1e-9)
            assert (await zone.objects().get("unmapped")).centroid is None

            # The migration loop left every stored location a native point.
            truck = RecordID("geo_readme_restaurant", "truck")
            points = await client.query(
                "SELECT VALUE location.is_point() FROM geo_readme_restaurant WHERE id != $truck;", {"truck": truck}
            )
            assert len(points) == 6 and all(points), points


class TestReadmeGeoWording:
    """Unit: what README must (and must not) say about v0.24.0 (spec §11, D16, P1)."""

    def test_section_25_never_describes_the_connection(self) -> None:
        texts = {"section 25": _section_25(), **{cells[0]: line for line, cells in _v0_24_0_rows()}}
        for where, text in texts.items():
            lowered = text.lower()
            assert not [phrase for phrase in _D16_PHRASES if phrase in lowered], where
            assert not re.search(r"\bconnections?\b", text, re.IGNORECASE), f"D16: {where} uses a banned word"

    def test_behaviour_table_rows(self) -> None:
        rows = _v0_24_0_rows()
        assert all(len(cells) == 4 and "\\|" not in line for line, cells in rows), rows
        added = [cells for _, cells in rows if cells[-1] == "v0.24.0"]
        assert len(added) == 2, added
        assert any(cells[0].startswith("Geometry fields") for cells in added), added
        assert any("`Decimal` coordinate" in cells[0] for cells in added), added
        [untyped] = [cells for _, cells in rows if cells[-1] == "pre-existing (documented in v0.24.0)"]
        assert "GeoJSON" in untyped[0] and "untyped" in untyped[0], untyped

    def test_behaviour_table_header_names_the_versions(self) -> None:
        header = _normalised(_readme_part("### ORM behaviour: SurrealDB 2.x vs 3.x").split("\n|", 1)[0])
        assert "rows added in v0.23.0 on 2.7.0 and 3.3.4, with 3.1.5 where it differs" in header
        assert "rows added in v0.24.0 on 2.7.0 and 3.3.4, identical on 3.1.5" in header

    def test_filter_lookups_list_names_the_spatial_lookups(self) -> None:
        bullets = [line for line in _readme_part("### Supported Filter Lookups").splitlines() if line.startswith("- ")]
        for lookup in ("inside", "outside", "intersects"):
            assert any(f"`{lookup}`" in bullet for bullet in bullets), lookup

    def test_features_roadmap_and_comparison_rows(self) -> None:
        features = [cells for _, cells in _table_rows(_readme_part("## Features"))]
        assert any(cells[0].startswith("Geospatial fields") and cells[1] == "✅" for cells in features), features
        roadmap = [cells for _, cells in _table_rows(_readme_part("## Roadmap"))]
        assert ["v0.24.0", "Geospatial fields", "✅ Released"] in roadmap, roadmap
        comparison = [cells for _, cells in _table_rows(_readme_part("## SurrealDB-ORM-lite vs SurrealDB-ORM"))]
        assert ["Geospatial Fields", "✅ v0.24.0", "✅"] in comparison, comparison

    def test_section_25_tables_have_no_raw_pipe_in_a_cell(self) -> None:
        tables = re.findall(r"(?:^\|.*\n)+", _section_25(), re.MULTILINE)
        assert tables, "section 25 has no table"
        for table in tables:
            widths = {len(re.split(r"(?<!\\)\|", line.strip().strip("|"))) for line in table.splitlines()}
            assert len(widths) == 1 and "\\|" not in table, table

    def test_section_25_examples_are_all_python(self) -> None:
        """Every fenced block of section 25 is a ``python`` block, so the E2E test runs them all."""
        fences = re.findall(r"^```(\S*)$", _section_25(), re.MULTILINE)
        assert len(fences) % 2 == 0 and fences[0::2] == ["python"] * (len(fences) // 2), fences

    def test_coming_from_the_full_orm(self) -> None:
        """P1: section 25 ends with what a full SurrealDB-ORM user meets."""
        section = _section_25()
        assert re.search(r"^#### Coming from the full SurrealDB-ORM$", section, re.MULTILINE), "P1 heading missing"
        text = _subsection("#### Coming from the full SurrealDB-ORM", section)
        for needle in ("to_geojson", '"line"', "nearby()"):
            assert needle in text, needle


class TestReadmeSection24Consistency:
    """P2: section 24 says what v0.24.0 converts and links section 25's notes (prose only)."""

    def test_after_a_write_names_geometries_and_links_the_sub_model_note(self) -> None:
        text = _normalised(_subsection("#### After a write that returns the row"))
        assert "geometry" in text, text
        assert "top-level" in text, text
        assert "(#rich-values-inside-sub-models)" in text, text

    def test_values_the_sdk_cannot_read_links_the_geometry_note(self) -> None:
        text = _normalised(_subsection("#### Values the SDK cannot read"))
        assert "(#geometries-the-sdk-cannot-read)" in text, text

    def test_linked_headings_exist_once_in_section_25(self) -> None:
        readme, section = _readme(), _section_25()
        for heading in _LINKED_HEADINGS:
            assert len(re.findall(rf"^{re.escape(heading)}$", readme, re.MULTILINE)) == 1, heading
            assert re.search(rf"^{re.escape(heading)}$", section, re.MULTILINE), heading

    def test_section_25_follows_section_24(self) -> None:
        readme = _readme()
        start_24 = readme.index("\n### 24. Rich field types\n")
        start_25 = readme.index("\n### 25. Geospatial fields\n")
        configuration = readme.index("\n## Configuration Options\n")
        assert start_24 < start_25 < configuration
        # Section 24 keeps all its subsections, and section 25 runs up to "Configuration Options".
        assert re.findall(r"^#{2,3} .*$", readme[start_24 + 1 : start_25], re.MULTILINE) == ["### 24. Rich field types"]
        assert re.findall(r"^#{2,3} .*$", readme[start_25 + 1 : configuration], re.MULTILINE) == ["### 25. Geospatial fields"]
