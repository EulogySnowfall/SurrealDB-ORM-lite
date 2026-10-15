"""Geometry fields (v0.24.0) through the ORM: models whose fields hold geometries, written and read on both server lines.

Unit tests of the read rule first (no database); the classes suffixed ``E2E`` run against SurrealDB.
Tables are prefixed ``geo_`` and created inside ``orm_client(...)``, which drops them before and
after the test; the change-feed table lives in a database of its own (``GEO_CF_DATABASE``). The
reference values come from ``tests/test_geo_types.py``.
"""

from datetime import timedelta
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
    LineStringField,
    MultiLineStringField,
    MultiPointField,
    MultiPolygonField,
    PointField,
    PolygonField,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    computed,
    geo,
)
from surreal_orm_lite.model_base import _read_kind
from tests._retry import retry_write
from tests.conftest import change_feed_database, orm_client
from tests.test_geo_types import (
    HOLE,
    KINDS,
    NEAR,
    ORM_CLASSES,
    PARIS,
    RING,
    A,
    B,
    _bad_line,
    _bad_point,
    _bad_ring,
    _nest,
    _orm,
    _sdk,
)
from tests.test_live_queryset import _take
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
        reason="pre-existing: a write sync leaves sub-models as the row's dict; tracked in a follow-up issue",
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
