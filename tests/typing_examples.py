"""Static-typing regression guard — checked by mypy, never executed by pytest.

Nothing in ``src/`` declares a computed field, so a public API that only type-checks by
accident would pass ``mypy src/`` while breaking every downstream user on the first line of
the README. This module is the canonical user-side declaration, type-checked in CI.

Deliberately named ``typing_examples`` rather than ``test_*`` so pytest does not collect it:
the assertions here are made by mypy, not at runtime.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, assert_type

from pydantic import Field
from surrealdb import RecordID

from surreal_orm_lite import (
    AccessState,
    Analyzer,
    AuthenticatedUserMixin,
    AuthResult,
    AuthTokens,
    BaseSurrealEdge,
    BaseSurrealModel,
    BoundExcluded,
    BoundIncluded,
    Computed,
    ConnectionConfig,
    Count,
    DatabaseIntrospector,
    Duration,
    Event,
    GeometryCollectionField,
    GeometryField,
    GeometryPoint,
    Index,
    IndexState,
    LineStringField,
    ModelCodeGenerator,
    MultiLineStringField,
    MultiPointField,
    MultiPolygonField,
    PointField,
    PolygonField,
    Q,
    QueryCache,
    QuerySet,
    Range,
    SchemaMode,
    SchemaState,
    SearchHighlight,
    SearchOffsets,
    SearchScore,
    Subquery,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    SurrealFunc,
    TableState,
    TableType,
    Transaction,
    Vector,
    computed,
    function_ddl,
    generate_models_from_db,
)
from surreal_orm_lite.testing import SurrealFixture, embedded_database, fixture
from surreal_orm_lite.vector import get_vector_info, is_vector_field


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


async def introspection_forms() -> None:
    """v0.30.0: the reader, the generator and ``generate_models_from_db()`` are typed; the state classes take the full
    SurrealDB-ORM's construction forms (a list for ``fields``, a pipe-joined ``relation_in``, a dict of signup fields)."""
    state = await DatabaseIntrospector().introspect()
    assert_type(state, SchemaState)
    assert_type(await DatabaseIntrospector("x", tables=["t"]).introspect(), SchemaState)
    assert_type(ModelCodeGenerator().generate(state), str)
    assert_type(await generate_models_from_db(), str)
    assert_type(await generate_models_from_db("m.py", overwrite=False), str)
    assert_type(await generate_models_from_db(Path("m.py"), "x", tables=["t"]), str)
    assert_type(state.tables["t"].fields["f"].nullable, bool)
    assert_type(state.tables["t"].fields["f"].field_type, str)
    assert_type(IndexState(name="i", fields=["a"]).fields, Sequence[str])
    assert_type(TableState(name="t", relation_in="a | b"), TableState)
    assert_type(AccessState(name="a", signup_fields={"e": "$e"}).signup_fields, Mapping[str, str])


class EventfulMember(BaseSurrealModel):
    """v0.31.0: ``SurrealConfigDict(events=[Event(...)])`` — a sequence of ``Event``, every key still optional."""

    model_config = SurrealConfigDict(
        table_name="eventful_member",
        events=[Event("audit", when="$before.name != $after.name", then="CREATE log SET kind = $event")],
    )

    id: str | None = None
    name: str


async def events_forms(tx: Transaction) -> None:
    """v0.31.0: ``Event`` and its renderer, the model's events and the manager's DDL appliers are typed; the pure
    renderers return ``str``/``list[str]``, the appliers coroutines of the statements they applied."""
    audit = Event("audit", then="CREATE log SET kind = $event")
    assert_type(audit, Event)
    assert_type(
        Event("notify", when="$event = 'CREATE'", then="NONE", async_mode=True, retry=2, max_depth=3, comment="c"),
        Event,
    )
    assert_type(audit.ddl("person"), str)
    assert_type(audit.ddl("person", overwrite=False), str)
    assert_type(EventfulMember.get_events(), tuple[Event, ...])
    assert_type(EventfulMember.event_ddl(), list[str])
    assert_type(EventfulMember.event_ddl(overwrite=False), list[str])
    assert_type(await EventfulMember.define_events(), list[str])
    assert_type(await EventfulMember.define_events(overwrite=False, tx=tx), list[str])
    assert_type(await EventfulMember.remove_event("x"), str)
    assert_type(await EventfulMember.remove_event("x", missing_ok=False, tx=tx), str)
    assert_type(await SurrealDBConnectionManager.define_event(audit, "person"), str)
    assert_type(await SurrealDBConnectionManager.define_event(audit, "person", overwrite=False, tx=tx), str)
    assert_type(await SurrealDBConnectionManager.remove_event("audit", "person"), str)
    assert_type(await SurrealDBConnectionManager.remove_event("audit", "person", missing_ok=False, tx=tx), str)
    assert_type(await SurrealDBConnectionManager.define_function("fn::greet", "RETURN 'hi';"), str)
    assert_type(
        await SurrealDBConnectionManager.define_function(
            "greet",
            "RETURN string::concat('Hello ', $name);",
            parameters={"name": "string", "times": "option<int>"},
            returns="string",
            permissions="FULL",
            comment="Greets",
            overwrite=False,
            tx=tx,
        ),
        str,
    )
    assert_type(await SurrealDBConnectionManager.remove_function("fn::greet"), str)
    assert_type(await SurrealDBConnectionManager.remove_function("greet", missing_ok=False, tx=tx), str)
    assert_type(function_ddl("fn::greet", "RETURN $name;", parameters={"name": "string"}, returns="string"), str)
    assert_type(function_ddl("greet", "RETURN 1;", permissions="WHERE $auth.admin = true", overwrite=False), str)


class OrderStats(BaseSurrealModel):
    """v0.32.0: ``SurrealConfigDict(view_query=…)`` — a ``str``, every key still optional."""

    model_config = SurrealConfigDict(
        table_name="order_stats",
        view_query="SELECT status, count() AS total, math::sum(amount) AS revenue FROM orders GROUP BY status",
    )

    id: RecordID | None = None
    status: str
    total: int
    revenue: float


async def views_forms(tx: Transaction) -> None:
    """v0.32.0: a materialized view's query, its pure renderer and its appliers are typed — ``str | None`` and
    ``str``."""
    q: str | None = OrderStats.get_view_query()
    d: str = OrderStats.view_ddl()
    assert_type(q, str | None)
    assert_type(d, str)
    assert_type(OrderStats.view_ddl(overwrite=False), str)
    assert_type(OrderStats._is_view(), bool)
    assert_type(SurrealConfigDict(view_query=None), SurrealConfigDict)
    s: str = await OrderStats.define_view()
    assert_type(s, str)
    assert_type(await OrderStats.define_view(overwrite=False, tx=tx), str)
    assert_type(await OrderStats.remove_view(missing_ok=False), str)
    assert_type(await OrderStats.remove_view(tx=tx), str)


class RelationLikes(BaseSurrealEdge):
    """v0.33.0: the full SurrealDB-ORM's relation keys — a ``TableType``, a ``str`` and a list of table names, a
    ``bool``; every key still optional."""

    model_config = SurrealConfigDict(
        table_name="relation_likes",
        table_type=TableType.RELATION,
        relation_in="a",
        relation_out=["b", "c"],
        enforced=True,
    )

    stars: int = 1


def relation_forms() -> None:
    """v0.33.0: the relation getters are typed — ``tuple[str, ...]``, ``bool`` and ``TableType``."""
    ends: tuple[str, ...] = RelationLikes.get_relation_in()
    assert_type(ends, tuple[str, ...])
    assert_type(RelationLikes.get_relation_out(), tuple[str, ...])
    assert_type(RelationLikes.is_enforced(), bool)
    assert_type(RelationLikes.get_table_type(), TableType)
    assert_type(Player.get_table_type(), TableType)
    assert_type(SurrealConfigDict(relation_in=("a", "b"), relation_out=None, enforced=None), SurrealConfigDict)
    assert_type(SurrealConfigDict(table_type=TableType.USER), SurrealConfigDict)


async def relation_appliers(tx: Transaction) -> None:
    """v0.33.0: the relation statement's pure renderer and its applier return the statement — a ``str``."""
    assert_type(RelationLikes.relation_ddl(), str)
    assert_type(RelationLikes.relation_ddl(overwrite=False), str)
    s: str = await RelationLikes.define_relation()
    assert_type(s, str)
    assert_type(await RelationLikes.define_relation(overwrite=False, tx=tx), str)


class TableClauses(BaseSurrealModel):
    """v0.34.0: the full SurrealDB-ORM's table keys — a ``SchemaMode``, a change feed, a mapping of permissions, a
    comment and a list of field names; every key still optional."""

    model_config = SurrealConfigDict(
        table_name="table_clauses",
        schema_mode=SchemaMode.SCHEMAFULL,
        changefeed="7d",
        permissions={"select": "FULL"},
        comment="c",
        flexible_fields=["meta"],
    )

    id: RecordID | None = None
    meta: dict[str, Any] | None = None


def table_clause_forms() -> None:
    """v0.34.0: the table getters are typed — ``SchemaMode | None``, ``str | None`` and ``dict[str, str]``."""
    mode: SchemaMode | None = TableClauses.get_schema_mode()
    assert_type(mode, SchemaMode | None)
    assert_type(TableClauses.get_changefeed(), str | None)
    permissions: dict[str, str] = TableClauses.get_permissions()
    assert_type(permissions, dict[str, str])
    rules: Mapping[str, str] = {"select": "FULL", "create": "$auth.id != NONE"}
    assert_type(SurrealConfigDict(permissions=rules, flexible_fields=("meta",)), SurrealConfigDict)
    assert_type(
        SurrealConfigDict(schema_mode=None, changefeed=None, permissions=None, comment=None, flexible_fields=None),
        SurrealConfigDict,
    )
    assert_type(SurrealConfigDict(table_type=TableType.HASH, schema_mode=SchemaMode.SCHEMALESS), SurrealConfigDict)
    statements: list[str] = TableClauses.table_ddl()
    assert_type(statements, list[str])
    assert_type(TableClauses.table_ddl(overwrite=False), list[str])


async def table_define_forms(tx: Transaction) -> None:
    """v0.34.0: ``define_table()`` returns the statements joined with a newline — a ``str``, the full ORM's return."""
    y: str = await TableClauses.define_table()
    assert_type(y, str)
    assert_type(await TableClauses.define_table(overwrite=False, tx=tx), str)


def search_value_forms() -> None:
    """v0.35.0: the search annotations, ``Analyzer`` and ``Index`` are typed — their attributes as declared, their pure
    renderers ``str``."""
    assert_type(SearchScore(), SearchScore)
    assert_type(SearchScore(1).ref, int)
    highlight = SearchHighlight("<em>", "</em>", 0)
    assert_type(highlight, SearchHighlight)
    assert_type(highlight.open_tag, str)
    assert_type(SearchOffsets(ref=2), SearchOffsets)
    english = Analyzer("english", ["class", "blank"], ("lowercase", "snowball(english)"), function="low", comment="c")
    assert_type(english, Analyzer)
    assert_type(Analyzer("bare"), Analyzer)
    assert_type(english.tokenizers, tuple[str, ...])
    assert_type(english.filters, tuple[str, ...])
    assert_type(english.function, str | None)
    assert_type(english.ddl(), str)
    assert_type(english.ddl(overwrite=False), str)
    title = Index("post_title", "title", search_analyzer=english, bm25=True, highlights=True)
    assert_type(title, Index)
    assert_type(Index("post_body", ["body"], search_analyzer="english", bm25=(1.2, 0.75)), Index)
    assert_type(Index("post_slug", ("slug",), unique=True, comment="c"), Index)
    assert_type(Index("post_head", "meta.headline", search_analyzer=english, bm25=False), Index)
    assert_type(title.fields, tuple[str, ...])
    assert_type(title.bm25, tuple[float, float] | Literal[True] | None)
    assert_type(title.search_analyzer, str | Analyzer | None)
    assert_type(title.analyzer_name, str | None)
    assert_type(title.ddl("post"), str)
    assert_type(title.ddl("post", overwrite=False, line=2), str)


_POST_ENGLISH = Analyzer("indexed_english", ["class", "blank"], ["lowercase", "snowball(english)"])


class IndexedPost(BaseSurrealModel):
    """v0.35.0: ``SurrealConfigDict(indexes=...)`` takes a sequence of ``Index``."""

    model_config = SurrealConfigDict(
        table_name="indexed_post",
        indexes=[
            Index("indexed_post_title", "title", search_analyzer=_POST_ENGLISH, bm25=True, highlights=True),
            Index("indexed_post_slug", "slug", unique=True),
        ],
    )

    id: RecordID | None = None
    title: str = ""
    slug: str = ""


def index_forms() -> None:
    """v0.35.0: ``get_indexes()`` is a ``tuple[Index, ...]``, ``index_ddl()`` a ``list[str]``."""
    indexes: tuple[Index, ...] = IndexedPost.get_indexes()
    assert_type(indexes, tuple[Index, ...])
    statements: list[str] = IndexedPost.index_ddl()
    assert_type(statements, list[str])
    assert_type(IndexedPost.index_ddl(overwrite=False, line=2), list[str])
    declared: Sequence[Index] = (Index("indexed_post_body", "body", search_analyzer="indexed_english"),)
    assert_type(SurrealConfigDict(indexes=declared), SurrealConfigDict)
    assert_type(SurrealConfigDict(indexes=None), SurrealConfigDict)


async def index_appliers(tx: Transaction) -> None:
    """v0.35.0: ``define_indexes()`` returns the statements applied (a ``list[str]``), ``remove_index()`` the one
    statement (a ``str``)."""
    applied: list[str] = await IndexedPost.define_indexes()
    assert_type(applied, list[str])
    assert_type(await IndexedPost.define_indexes(overwrite=False, tx=tx), list[str])
    removed: str = await IndexedPost.remove_index("indexed_post_slug")
    assert_type(removed, str)
    assert_type(await IndexedPost.remove_index("indexed_post_slug", missing_ok=False, tx=tx), str)


def search_forms() -> None:
    """v0.35.0: ``search()`` takes ``str`` texts by field and returns the queryset, a dotted path given through ``**``."""
    assert_type(QuerySet(IndexedPost).search(title="physics"), QuerySet)
    assert_type(QuerySet(IndexedPost).search(title="quantum", slug="a1").search(**{"meta.headline": "rust"}), QuerySet)
    assert_type(QuerySet(IndexedPost).filter(id__in=Subquery(QuerySet(IndexedPost).search(title="x"))), QuerySet)


def search_annotation_forms() -> None:
    """v0.35.0: ``annotate()`` takes ``SearchScore``, ``SearchHighlight`` and ``SearchOffsets`` and returns the
    queryset."""
    annotated = (
        QuerySet(IndexedPost)
        .search(title="physics")
        .annotate(relevance=SearchScore(0), snippet=SearchHighlight("<b>", "</b>", 0), spans=SearchOffsets(0))
        .order_by("-relevance")
    )
    assert_type(annotated, QuerySet)
    assert_type(QuerySet(IndexedPost).annotate(n=Count(), s=SearchScore()), QuerySet)


async def analyzer_appliers(tx: Transaction) -> None:
    """v0.35.0: the manager's ``define_analyzer()`` returns the statements applied (a ``list[str]``),
    ``remove_analyzer()`` the one statement (a ``str``)."""
    applied: list[str] = await SurrealDBConnectionManager.define_analyzer(_POST_ENGLISH)
    assert_type(applied, list[str])
    assert_type(await SurrealDBConnectionManager.define_analyzer(_POST_ENGLISH, overwrite=False, tx=tx), list[str])
    removed: str = await SurrealDBConnectionManager.remove_analyzer("indexed_english")
    assert_type(removed, str)
    assert_type(await SurrealDBConnectionManager.remove_analyzer("indexed_english", missing_ok=False, tx=tx), str)


class TypedVecDoc(BaseSurrealModel):
    """v0.36.0: the typed form of a vector field — ``VectorField[...]`` is the full ORM's spelling, which type checkers
    refuse, so it never appears here. The default is written in the class body (``= None`` or ``Field(default=None)``)."""

    title: str
    embedding: Annotated[list[float] | None, Vector(3)] = None
    summary: Annotated[list[float] | None, Vector(3, "F64")] = Field(default=None, alias="summary_vec")


def vector_field_forms() -> None:
    """v0.36.0: the model is built without its vector fields; each reads ``list[float] | None``; the marker and the
    helpers are typed."""
    doc = TypedVecDoc(title="x")
    assert_type(doc.embedding, list[float] | None)
    assert_type(doc.summary, list[float] | None)
    assert_type(Vector(3), Vector)
    assert_type(Vector(384, "F64").dimension, int)
    assert_type(Vector(384, "F64").vector_type, str)
    assert_type(get_vector_info(TypedVecDoc.model_fields["embedding"]), tuple[int, str] | None)
    assert_type(is_vector_field(TypedVecDoc.model_fields["embedding"]), bool)


def vector_index_forms() -> None:
    """v0.36.0: ``Index`` takes the vector arguments, keyword-only after ``comment``, and holds them as declared."""
    embedding = Index("typed_vec_doc_embedding", "embedding", hnsw=True, dimension=3, dist="cosine")
    assert_type(embedding, Index)
    assert_type(Index("i", "e", hnsw=True, dimension=3, vector_type="f64", efc=100, m=8, comment="c"), Index)
    assert_type(embedding.hnsw, bool)
    assert_type(embedding.dimension, int | None)
    assert_type(embedding.dist, str | None)
    assert_type(embedding.vector_type, str | None)
    assert_type(embedding.efc, int | None)
    assert_type(embedding.m, int | None)
    assert_type(embedding.ddl("typed_vec_doc"), str)


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
