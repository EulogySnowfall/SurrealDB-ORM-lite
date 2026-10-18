"""Static-typing regression guard — checked by mypy, never executed by pytest.

Nothing in ``src/`` declares a computed field, so a public API that only type-checks by
accident would pass ``mypy src/`` while breaking every downstream user on the first line of
the README. This module is the canonical user-side declaration, type-checked in CI.

Deliberately named ``typing_examples`` rather than ``test_*`` so pytest does not collect it:
the assertions here are made by mypy, not at runtime.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, assert_type

from surrealdb import RecordID

from surreal_orm_lite import (
    AuthenticatedUserMixin,
    AuthResult,
    AuthTokens,
    BaseSurrealEdge,
    BaseSurrealModel,
    BoundExcluded,
    BoundIncluded,
    Computed,
    ConnectionConfig,
    Duration,
    GeometryCollectionField,
    GeometryField,
    GeometryPoint,
    LineStringField,
    MultiLineStringField,
    MultiPointField,
    MultiPolygonField,
    PointField,
    PolygonField,
    Q,
    QueryCache,
    QuerySet,
    Range,
    Subquery,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    SurrealFunc,
    Transaction,
    computed,
)
from surreal_orm_lite.testing import SurrealFixture, embedded_database, fixture


class Player(BaseSurrealModel):
    """The README §16 declaration, verbatim."""

    id: str
    first_name: str
    last_name: str
    full_name: Computed[str] = computed("string::concat(first_name, ' ', last_name)")
    name_len: Computed[int] = computed(SurrealFunc("string::len(full_name)"))


def read_computed(player: Player) -> str | None:
    """``Computed[str]`` must resolve to ``str | None``, not to an opaque marker type."""
    return player.full_name


def read_computed_int(player: Player) -> int | None:
    return player.name_len


class Tagged(BaseSurrealModel):
    """v0.22.0: ``table_name`` alone — every ``SurrealConfigDict`` key is optional (``total=False``)."""

    model_config = SurrealConfigDict(table_name="tagged")

    id: str | None = None


class Stamped(BaseSurrealModel):
    """The README §v0.18.0 declaration: ``server_fields`` alone used to fail mypy ("Missing keys")."""

    model_config = SurrealConfigDict(server_fields=["created"])

    id: str | None = None
    created: str | None = None


def tagged_table() -> str:
    return Tagged.get_table_name()


class Purchased(BaseSurrealEdge):
    """v0.22.0: an edge model. ``in_`` (column ``in``) and ``out`` are declared by the base."""

    model_config = SurrealConfigDict(table_name="purchased")

    quantity: int = 1
    price: float = 0.0


def new_purchase() -> Purchased:
    """The endpoints are ``RecordID`` parameters named ``in_`` and ``out`` — not the keyword ``in``."""
    return Purchased(in_=RecordID("Person", "a"), out=RecordID("Product", "w"), quantity=2)


def purchase_buyer(edge: Purchased) -> RecordID:
    """An endpoint reads back as a ``RecordID``, whatever form it was given in."""
    return edge.in_


def purchase_item(edge: Purchased) -> RecordID:
    return edge.out


class Person(BaseSurrealModel):
    id: str | None = None


class Product(BaseSurrealModel):
    id: str | None = None


async def typed_relate(a: Person, p: Product) -> Purchased:
    """An edge class gives back that edge, typed (the first ``relate()`` overload)."""
    return await a.relate(Purchased, p, data={"quantity": 1})


async def typed_relate_in_tx(a: Person, p: Product, tx: Transaction) -> Purchased:
    """``tx`` by position, the full ORM's order: ``(edge, target, reverse, tx)``."""
    return await a.relate(Purchased, p, False, tx)


async def raw_relate(a: Person, b: Person) -> list[dict[str, Any]]:
    """An edge table name keeps the v0.6.0 return: the ``RELATE`` rows."""
    return await a.relate("follows", b)


async def related_through_edge_class(a: Person) -> list[Any]:
    return await a.get_related(Purchased, model_class=Product)


async def typed_reverse(a: Person, p: Product) -> Purchased:
    """``reverse=True`` and keyword edge fields keep the edge-class overload."""
    return await a.relate(Purchased, p, reverse=True, quantity=1)


async def typed_reverse_positional(a: Person, p: Product) -> Purchased:
    """A positional ``reverse`` (the full ORM's order) is still typed ``Purchased``."""
    return await a.relate(Purchased, p, True, None, quantity=1)


async def raw_kwargs(a: Person, b: Person) -> list[dict[str, Any]]:
    """The string form with keyword edge fields keeps the ``RELATE`` rows."""
    return await a.relate("follows", b, reverse=True, since="2025-01-01")


async def unlink(a: Person, p: Product) -> None:
    """``reverse=``/``tx=`` on ``remove_relation``, a bare id, and a mixed list of edges."""
    await a.remove_relation(Purchased, p, reverse=True, tx=None)
    await a.remove_relation("follows", "abc123")
    await a.remove_all_relations([Purchased, "follows"], direction="both", tx=None)
    await a.remove_all_relations([Purchased, "follows"], "both", None)


async def both(a: Person) -> list[Any]:
    return await a.get_related("follows", direction="both")


async def related_positional(a: Person) -> list[Any]:
    """``get_related(edge, direction, model_class)`` by position, the full ORM's order."""
    return await a.get_related("follows", "in", Person)


class Timed(BaseSurrealModel):
    """v0.23.0: a ``Duration`` field keeps the server's nanoseconds."""

    ttl: Duration | None = None


def make_duration() -> Duration:
    """``parse`` is a classmethod returning ``Self``: typed ``Duration``, not the SDK's class."""
    return Duration.parse("1h30m")


def from_seconds() -> Duration:
    """The SDK's signature: an ``int`` of seconds, plus nanoseconds."""
    return Duration.parse(90, 5)


def compact(d: Duration) -> list[int]:
    return d.to_compact()


def as_td(d: Duration) -> timedelta:
    return d.to_timedelta()


class Shift(BaseSurrealModel):
    """v0.23.0: a ``Range[int]`` field validates its bounds as ``int``."""

    hours: Range[int] | None = None


def nine_to_five() -> Range[int]:
    """``Range`` is generic: ``Range[int](...)`` is typed ``Range[int]``."""
    return Range[int](BoundIncluded(9), BoundExcluded(17))


def one_to_five() -> Range[int]:
    return Range[int](BoundIncluded(1), BoundExcluded(5))


def open_end(start: int) -> Range[int]:
    """An open side is ``None``; the bound attributes are optional."""
    shift = Range[int](BoundIncluded(start), None)
    begin: BoundIncluded | BoundExcluded | None = shift.begin
    assert begin is not None
    return shift


class TypedPlace(BaseSurrealModel):
    """v0.24.0: geometry fields. Without the Pydantic mypy plugin, mypy only sees a default
    written in the class body, so the aliases are declared ``= None``."""

    name: str
    entrance: GeometryPoint
    location: PointField = None
    path: LineStringField = None
    area: PolygonField = None
    pins: MultiPointField = None
    routes: MultiLineStringField = None
    zones: MultiPolygonField = None
    bag: GeometryCollectionField = None
    anything: GeometryField = None
    terrace: GeometryPoint | None = None
    stops: list[GeometryPoint] = []


def make_place() -> TypedPlace:
    """No alias field is passed: each one has its ``= None`` default."""
    return TypedPlace(name="x", entrance=GeometryPoint(0, 0))


def place_longitude(store: TypedPlace) -> float:
    """An alias types as ``GeometryPoint | None``."""
    return store.location.longitude if store.location else 0.0


def entrance_latitude(store: TypedPlace) -> float:
    """A class annotation types as the class."""
    return store.entrance.latitude


def configure_embedded() -> None:
    """v0.25.0: an embedded URL needs no credentials — ``user``/``password`` are optional keywords."""
    SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="t")


def configure_server() -> None:
    """The five positional arguments of earlier versions still type-check."""
    SurrealDBConnectionManager.set_connection("ws://localhost:8000/rpc", "root", "root", "ns", "db")


def embedded_flag() -> bool:
    """``is_embedded()`` is a classmethod returning a real ``bool``."""
    flag = SurrealDBConnectionManager.is_embedded()
    assert_type(flag, bool)
    return flag


async def isolated() -> None:
    """``embedded_database()`` is an async context manager yielding the SDK client, typed ``Any``."""
    async with embedded_database() as client:
        assert_type(client, Any)
        await client.query("RETURN 1;")
    async with embedded_database("surrealkv://test.db", namespace="n", database="d"):
        pass


class TypedMember(BaseSurrealModel):
    """v0.25.0: the model a fixture class declares instances of."""

    id: str | None = None
    name: str


def time_travel_forms() -> None:
    """v0.26.0: ``as_of()`` takes each annotated form and gives the queryset back (``Self``).

    The SDK's own ``Duration`` is accepted at runtime only: the annotation names the ORM's types, so it is not
    called here (a type checker flags it).
    """
    assert_type(QuerySet(TypedMember).as_of(datetime(2026, 1, 1, tzinfo=UTC)), QuerySet)
    assert_type(QuerySet(TypedMember).as_of("2026-01-01T00:00:00Z"), QuerySet)
    assert_type(QuerySet(TypedMember).as_of(timedelta(hours=1)), QuerySet)
    assert_type(QuerySet(TypedMember).as_of(Duration.parse("1h")), QuerySet)
    assert_type(QuerySet(TypedMember).as_of(SurrealFunc("time::now() - 1h")), QuerySet)
    assert_type(QuerySet(TypedMember).as_of(None), QuerySet)


def subquery_forms() -> None:
    """v0.27.0: ``Subquery`` wraps a QuerySet; a ``Subquery`` or a QuerySet is a filter value in ``filter()`` and ``Q``."""
    assert_type(Subquery(QuerySet(TypedMember)), Subquery)
    assert_type(QuerySet(TypedMember).filter(id__in=Subquery(QuerySet(TypedMember))), QuerySet)
    assert_type(Q(id__in=QuerySet(TypedMember)), Q)
    assert_type(Subquery(QuerySet(TypedMember)).queryset, QuerySet)


def query_cache_examples() -> None:
    """v0.28.0: ``cache()`` keeps the queryset's type; ``QueryCache``'s class methods are typed."""
    assert_type(QuerySet(TypedMember).cache(), QuerySet)
    assert_type(QuerySet(TypedMember).cache(30), QuerySet)
    assert_type(QuerySet(TypedMember).cache(0.5), QuerySet)
    assert_type(QuerySet(TypedMember).cache(ttl=timedelta(seconds=5)), QuerySet)
    assert_type(QuerySet(TypedMember).filter(name="x").cache(30).order_by("name"), QuerySet)
    assert_type(QueryCache.stats(), dict[str, Any])
    assert_type(QueryCache.make_key("SELECT 1;", {}, "t"), str)
    assert_type(QueryCache.invalidate(TypedMember), int)
    assert_type(QueryCache.invalidate("typed_member"), int)


class _PageSize:
    """An integer type that is not an ``int``: ``__index__`` only, as NumPy's ``int64``."""

    def __index__(self) -> int:
        return 10


def paging_takes_any_integer_type() -> None:
    """v0.28.1: ``limit()`` and ``offset()`` take any ``__index__`` integer type (``SupportsIndex``)."""
    assert_type(QuerySet(TypedMember).limit(10).offset(20), QuerySet)
    assert_type(QuerySet(TypedMember).limit(_PageSize()).offset(_PageSize()), QuerySet)


async def invalidating_writes_keep_their_types(member: TypedMember, edge: Purchased, tx: Transaction) -> None:
    """v0.28.0: every write the query cache wraps (``cache.invalidates()``) keeps its own type, ``Self`` included."""
    assert_type(await member.save(), TypedMember)
    assert_type(await member.save(tx=tx), TypedMember)
    assert_type(await member.upsert(), TypedMember)
    assert_type(await member.update(), Any)
    assert_type(await member.merge(name="x"), Any)
    assert_type(await member.patch([{"op": "replace", "path": "/name", "value": "x"}]), TypedMember)
    assert_type(await member.atomic_append("tags", 1), TypedMember)
    assert_type(await member.atomic_increment("n", 2, tx), TypedMember)
    assert_type(await member.atomic_remove_many("tags", [1]), TypedMember)
    await member.delete(tx=tx)
    assert_type(await edge.save(), Purchased)
    assert_type(await Player.define_computed_fields(tx=tx), list[str])
    assert_type(await QuerySet(TypedMember).bulk_create([member]), list[BaseSurrealModel])
    assert_type(await QuerySet(TypedMember).bulk_update(name="x"), int)
    assert_type(await QuerySet(TypedMember).bulk_delete(), int)
    assert_type(await QuerySet(TypedMember).patch([]), int)
    assert_type(await QuerySet(TypedMember).delete_table(), bool)
    assert_type(await AuthenticatedUserMixin.define_access(tx=tx), list[str])
    assert_type(await AuthenticatedUserMixin.signup(email="a@b.c", password="p"), AuthResult[AuthenticatedUserMixin])
    assert_type(await SurrealDBConnectionManager.signup(access="a", variables={}), AuthTokens)


class RoutedMember(BaseSurrealModel):
    """v0.29.0: a model routed to a named connection (``SurrealConfigDict`` keys are all optional)."""

    model_config = SurrealConfigDict(connection="x")

    id: str | None = None


def multi_database_forms() -> None:
    """v0.29.0: the registry and model routing are typed; ``add_connection()`` takes keywords and returns ``None``."""
    SurrealDBConnectionManager.add_connection("x", url="mem://", namespace="t", database="t")
    SurrealDBConnectionManager.add_connection(
        "y", url="ws://localhost:8000/rpc", user="root", password="root", namespace="n", database="d"
    )
    assert_type(SurrealDBConnectionManager.get_config("x"), ConnectionConfig | None)
    assert_type(SurrealDBConnectionManager.get_config(), ConnectionConfig | None)
    assert_type(SurrealDBConnectionManager.list_connections(), list[str])
    assert_type(SurrealDBConnectionManager.get_active_connection_name(), str | None)
    assert_type(RoutedMember.get_connection_name(), str)
    assert_type(ConnectionConfig("mem://", None, None, "t", "t"), ConnectionConfig)
    assert_type(ConnectionConfig(url="ws://h/rpc", user="u", password="p", namespace="n", database="d").url, str)


async def multi_database_blocks() -> None:
    """v0.29.0: ``using()``, ``name=`` and ``embedded_database(connection=)`` are async context managers / coroutines."""
    async with SurrealDBConnectionManager.using("x"):
        assert_type(await SurrealDBConnectionManager.get_client(), Any)
    assert_type(await SurrealDBConnectionManager.get_client("x"), Any)
    assert_type(await SurrealDBConnectionManager.get_client(name="x"), Any)
    async with SurrealDBConnectionManager.transaction("x") as tx:
        await tx.add("RETURN 1;")
    await SurrealDBConnectionManager.close_connection("x")
    await SurrealDBConnectionManager.remove_connection("x")
    async with embedded_database(connection="x"):
        pass
    async with embedded_database(connection=None):
        pass


@fixture
class Typed(SurrealFixture):
    """``@fixture`` gives the class back unchanged: ``Typed.load()`` stays typed ``Typed``."""

    alice = TypedMember(name="Alice")


def fixture_returns_its_argument() -> None:
    """Called directly, ``fixture()`` returns the class it was given.

    mypy ignores a class decorator's return type, so the decorated form above cannot catch a ``fixture()``
    annotated to return ``type[SurrealFixture]``; this call can.
    """
    assert_type(fixture(Typed), type[Typed])


async def uses_fixture() -> str:
    """``load()`` yields the subclass (``Self``), so a declared instance keeps its model type."""
    async with Typed.load() as data:
        assert_type(data, Typed)
        assert_type(data.alice, TypedMember)
        return data.alice.name
