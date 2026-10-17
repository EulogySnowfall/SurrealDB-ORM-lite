"""Tests for v0.22.0 — native typed relations.

Filter values (spec §4.6):

- a model instance used as a filter value — keyword filters, ``Q``, every scalar lookup and each
  item of a sequence lookup — is replaced by its record id, so ``filter(customer=alice)`` finds
  the rows whose ``customer`` column links to alice instead of failing to encode a Pydantic model;
  ``get_or_create(customer=alice)`` stores that same record id, so the next lookup finds the row;
- the filter builder takes the queried model's **edge endpoint columns** (``in``/``out``): there a
  ``"table:id"`` string is read as the record id it names, through the same rule the edge fields
  validate with (``utils.coerce_endpoint``), and a text lookup is refused instead of silently
  matching nothing. Node columns are untouched: a string column may legitimately hold ``"a:b"``;
- a lookup on a field whose name ends in ``_`` works: ``in___in`` is the ``in`` lookup on ``in_``.

The edge model (spec §4.2): ``BaseSurrealEdge`` declares the endpoints ``in_`` (column ``in``) and
``out`` as frozen ``RecordID`` fields. A node instance, a ``RecordID`` or a ``"table:id"`` string is
read through ``utils.coerce_endpoint`` — anything else is a ``ValidationError``. The ``in_ → in``
mapping comes from the ``_implicit_aliases()`` hook of the v0.18.0 alias map, so every path that
already honours an alias (writes, filters, ``order_by``, hydration) honours it, whatever the call
order and on subclasses too. A subclass may not redeclare an endpoint.

Creating an edge (spec §2.4, §2.7, §4.3): ``save()`` writes through ``INSERT RELATION`` — the SDK's
own ``insert_relation()`` for an auto id, a guarded statement (``… RETURN VALUE IF $before != NONE
{ THROW … }``) for an explicit one, because a raw duplicate id is silently kept on 2.7.0 and 3.1.5
but raises on 3.3.3. Through the ORM a duplicate raises ``SurrealDbError`` "already exists" on every
line, server refusals become ORM errors (``SurrealDbNotFoundError`` for an ``ENFORCED`` endpoint),
and a write the table's ``PERMISSIONS`` refuse — which the server answers with no row and no error —
raises instead of returning an edge without an id.

``bulk_create()`` on an edge model (spec §4.3.5) takes the same path for a whole batch: N edges
without an id in **one** ``insert_relation()`` call; the guarded statement for the whole batch as
soon as one edge has an explicit id, so one duplicate — stored already or repeated in the batch —
writes nothing; every id checked before any call. It returns the hydrated edges (no transaction, or
an interactive one) or the inputs (buffered). A batch the ``PERMISSIONS`` partly refuse raises
"Only k of n edges came back" and keeps the accepted edges (outside a transaction it is not atomic).

Edges inside a transaction (spec §4.5) follow the v0.9.0 strategy of every write, through the
guarded statement and ``tx.add()``: on an **interactive** transaction (WebSocket + 3.x) the row comes
back at once — ``edge.id`` set, visible to ``objects(tx=)``, gone after a rollback, a duplicate or a
refused write raising inside the block; on a **buffered** one (2.x, or HTTP on any line) the edge is
queued, ``edge.id`` stays ``None`` (an auto id is allowed, unlike a node's buffered ``save()``), it
is written at commit and rolled back with the batch, and a duplicate explicit id fails the commit.
``pre_save`` fires at call time, ``post_save`` only after a successful commit.

``relate()`` (spec §4.3.2) takes an edge model class or an edge table name, in the full ORM's
parameter order ``(edge, target, reverse=False, tx=None, *, data=None, **edge_data)``. With an edge class it
creates the edge through the same ``save()`` and returns the validated, hydrated edge — source and
target bound as record ids (a node id such as ``joe@x.com`` relates like it saves), ``data`` checked
before any call (no endpoint, no computed field, no undeclared key), a ``server_fields`` key in
``data`` written. With a name it keeps the v0.6.0 ``RELATE`` and its rows. Both take ``tx=``: rows
at once on an interactive transaction, nothing (``[]``, or an edge whose ``id`` is ``None``) on a
buffered one. ``get_related``, ``remove_relation`` and ``remove_all_relations`` accept an edge class
too; any other class or value is a ``TypeError``.

Graph API parity with the full ORM (spec §2.8, §4.3.2, §4.3.6, §4.5): the four graph helpers take
their parameters in the full ORM's order, by position or by keyword (only ``data=`` is
keyword-only); ``reverse=True`` relates or unlinks ``target -> edge -> self``; edge fields may be
given as keywords (``**edge_data``), merged with ``data=``; ``remove_relation`` and
``remove_all_relations`` take ``tx=``, the latter a list of edges, and ``get_related`` a ``"both"``
direction (out-neighbours, then in-neighbours). The removals are graph-path ``DELETE`` statements
with bound record ids (``DELETE $src->e WHERE out = $dst``): exactly the edges traversal sees, a
native no-op on an edge table that was never created, the same in and out of a transaction. A
source whose own table was never created is where the lines diverge: 3.x raises "The table …
does not exist", 2.x returns ``[]``. Outside a transaction the ORM keeps it a silent no-op on both;
under ``tx=`` the 3.x failure fails the transaction (at once if interactive, at commit if buffered).
A ``remove_relation()`` target without ``:`` is a bare record id, bound verbatim and compared as a
string with ``record::id(out)``: it matches that id in any table, never an integer id.

Writes on a stored edge (spec §4.4, §2.9): SurrealDB ignores a change to an edge's endpoints on both
lines, so every write that names one — ``merge()`` and its ``server_values=``, ``patch()`` on the
instance or the QuerySet, the ``atomic_*`` helpers, ``bulk_update()``, ``update_or_create(defaults=)``
— raises ``ValueError`` "endpoints" before any call, under either name (``in_`` or the column
``in``). ``upsert()`` (a plain record no traversal sees) and ``save(server_values=)`` raise
``SurrealDbError`` and write nothing; ``update()`` keeps the endpoints in its full replace, which a
``TYPE RELATION … SCHEMAFULL`` table on 2.x requires. ``get_or_create(in_=a, out=b)`` is an idempotent
relate — instances, record ids and ``"table:id"`` strings converge on one row — and
``update_or_create`` changes the data, never the endpoints. SurrealDB's own idempotency holds through
the ORM: a deterministic array id raises "already exists" on a second save, a ``UNIQUE`` index on
``in, out`` "already contains" on a second relate (the reverse pair is another edge). An array-id
edge is updated and deleted inside a transaction like any other — ``update(tx=)`` and
``delete(tx=)`` bind the id as ``$rid`` instead of writing ``tr_follows:[RecordID(…), …]`` into the
statement, a parse error that failed the whole transaction.

Live queries and change feeds on edge tables (spec §2.5, §4.7): ``live()`` (full and diff) and
``changes()`` yield typed edges, and a live filter on an endpoint — a node instance, a ``RecordID``
or a ``"table:id"`` string — is applied by the server (proven by order: bob's edge is created first,
alice's event still comes first). SurrealDB 2.x adds ``"__": true`` to the edge record of a
``CREATE``/``UPDATE`` notification and of a change-feed update entry: the typed instance drops it
(an ``extra="allow"`` edge never grows a ``__`` attribute), ``raw`` keeps it as sent; ``DELETE``
payloads carry no marker on either line.

E2E tables are dedicated (``tr_*``), dropped on entry and exit by ``orm_client``; nodes are created
inside the ``async with orm_client(...)`` block. Same behaviour on SurrealDB 2.x and 3.x, except the
transaction strategy the server supports (interactive tests skip on 2.x; buffered ones run over HTTP
on both lines).
"""

import asyncio
import contextlib
import os
import re
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import uuid4

import pytest
from pydantic import Field, ValidationError
from surrealdb import AlreadyExistsError, NotFoundError, RecordID

from surreal_orm_lite import (
    AuthenticatedUserMixin,
    BaseSurrealEdge,
    BaseSurrealModel,
    Computed,
    Q,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    SurrealDbError,
    SurrealDbNotFoundError,
    SurrealFunc,
    Var,
    computed,
    post_save,
    pre_delete,
    pre_save,
    pre_update,
)
from surreal_orm_lite.changefeed import feed_events
from surreal_orm_lite.enum import LiveAction
from surreal_orm_lite.live import to_change_event
from surreal_orm_lite.query_set import QuerySet
from surreal_orm_lite.transaction import BufferedTransaction
from surreal_orm_lite.utils import build_filter_condition, coerce_endpoint, parse_lookup
from tests._retry import retry_write as _write
from tests.conftest import change_feed_database, orm_client

# ==================== Shared models ====================


class TrPerson(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="tr_person")

    id: str | None = None
    name: str = ""


class TrProduct(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="tr_product")

    id: str | None = None
    name: str = ""


class TrCustomer(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="tr_customer")

    id: str | None = None
    name: str = ""


class TrOrder(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="tr_order")

    id: str | None = None
    customer: RecordID
    note: str = ""


class TrPurchased(BaseSurrealEdge):
    model_config = SurrealConfigDict(table_name="tr_purchased")

    quantity: int = Field(default=1, ge=1)
    price: float = 0.0


class TrFollows(BaseSurrealEdge):
    model_config = SurrealConfigDict(table_name="tr_follows")

    weight: int = 1


ENDPOINTS = frozenset({"in", "out"})
ALICE = RecordID("tr_customer", "alice")
BOB = RecordID("tr_customer", "bob")


# ==================== Task 2 — model instances as filter values (unit) ====================


class TestFilterByInstance:
    def test_scalar_lookup_binds_record_id(self) -> None:
        alice = TrCustomer(id="alice")
        query, variables = TrOrder.objects().filter(customer=alice)._compile_query()
        assert query == "SELECT * FROM tr_order WHERE customer = $_f0;"
        assert variables == {"_f0": ALICE}
        assert isinstance(variables["_f0"], RecordID)

    @pytest.mark.parametrize("lookup", ["gt", "gte", "lt", "lte"])
    def test_comparison_lookups_bind_record_id(self, lookup: str) -> None:
        alice = TrCustomer(id="alice")
        _, variables = TrOrder.objects().filter(**{f"customer__{lookup}": alice})._compile_query()
        assert variables == {"_f0": ALICE}

    def test_id_lookup_with_an_instance(self) -> None:
        alice = TrCustomer(id="alice")
        _, variables = TrCustomer.objects().filter(id=alice)._compile_query()
        assert variables == {"_f0": ALICE}

    def test_in_lookup_maps_each_item(self) -> None:
        alice, bob = TrCustomer(id="alice"), TrCustomer(id="bob")
        query, variables = TrOrder.objects().filter(customer__in=[alice, bob])._compile_query()
        assert query == "SELECT * FROM tr_order WHERE customer IN $_f0;"
        assert variables == {"_f0": [ALICE, BOB]}

        _, mixed = TrOrder.objects().filter(customer__not_in=(alice, BOB))._compile_query()
        assert mixed == {"_f0": [ALICE, BOB]}

    def test_containsany_maps_each_item(self) -> None:
        alice = TrCustomer(id="alice")
        _, variables = TrOrder.objects().filter(customer__containsany=[alice, "x"])._compile_query()
        assert variables == {"_f0": [ALICE, "x"]}

    def test_q_object_value_is_coerced(self) -> None:
        alice, bob = TrCustomer(id="alice"), TrCustomer(id="bob")
        query, variables = TrOrder.objects().filter(Q(customer=alice) | Q(customer=bob))._compile_query()
        assert query == "SELECT * FROM tr_order WHERE (customer = $_f0 OR customer = $_f1);"
        assert variables == {"_f0": ALICE, "_f1": BOB}

    def test_unsaved_instance_raises(self) -> None:
        unsaved = TrCustomer(name="x")
        with pytest.raises(ValueError, match="unsaved"):
            TrOrder.objects().filter(customer=unsaved)._compile_query()
        with pytest.raises(ValueError, match="unsaved"):
            TrOrder.objects().filter(customer__in=[TrCustomer(id="alice"), unsaved])._compile_query()
        with pytest.raises(ValueError, match="unsaved"):
            TrOrder.objects().filter(Q(customer=unsaved))._compile_query()

    @pytest.mark.parametrize("lookup", ["in", "not_in", "containsall", "containsany"])
    def test_single_instance_in_sequence_lookup_rejected(self, lookup: str) -> None:
        alice = TrCustomer(id="alice")
        with pytest.raises(ValueError, match="wrap it in a list") as excinfo:
            TrOrder.objects().filter(**{f"customer__{lookup}": alice})._compile_query()
        assert "TrCustomer" in str(excinfo.value)

    def test_strings_are_not_coerced_on_node_columns(self) -> None:
        _, variables = TrOrder.objects().filter(note="tr_customer:alice")._compile_query()
        assert variables == {"_f0": "tr_customer:alice"}
        assert isinstance(variables["_f0"], str)

    def test_criteria_payload_coerces_instances(self) -> None:
        alice = TrCustomer(id="alice")
        payload = QuerySet._criteria_payload({"customer": alice, "note__contains": "x"})
        assert payload == {"customer": ALICE}
        assert isinstance(payload["customer"], RecordID)

    def test_criteria_payload_refuses_unsaved_instance(self) -> None:
        with pytest.raises(ValueError, match="unsaved"):
            QuerySet._criteria_payload({"customer": TrCustomer(name="x")})


# ==================== Task 2 — edge endpoint columns (unit) ====================


class TestEndpointColumns:
    """The builder applied directly, as the QuerySet of an edge model will call it."""

    def test_string_endpoint_binds_record_id(self) -> None:
        sql, variables, counter = build_filter_condition("in", "exact", "tr_person:alice", 0, "e", endpoint_columns=ENDPOINTS)
        assert (sql, counter) == ("in = $_f0", 1)
        assert variables == {"_f0": RecordID("tr_person", "alice")}
        assert isinstance(variables["_f0"], RecordID)

    def test_backticked_string_id_is_unquoted(self) -> None:
        _, variables, _ = build_filter_condition("in", "exact", "tr_person:`a-b`", 0, "e", endpoint_columns=ENDPOINTS)
        assert variables == {"_f0": RecordID("tr_person", "a-b")}

    def test_instance_and_record_id_endpoints(self) -> None:
        bob = TrPerson(id="bob")
        _, from_instance, _ = build_filter_condition("out", "exact", bob, 0, "e", endpoint_columns=ENDPOINTS)
        assert from_instance == {"_f0": RecordID("tr_person", "bob")}

        rid = RecordID("tr_person", "bob")
        _, from_rid, _ = build_filter_condition("out", "exact", rid, 0, "e", endpoint_columns=ENDPOINTS)
        assert from_rid["_f0"] is rid

    @pytest.mark.parametrize("lookup", ["gt", "gte", "lt", "lte"])
    def test_comparison_lookups_are_coerced(self, lookup: str) -> None:
        _, variables, _ = build_filter_condition("in", lookup, "tr_person:a", 0, "e", endpoint_columns=ENDPOINTS)
        assert variables == {"_f0": RecordID("tr_person", "a")}

    @pytest.mark.parametrize("lookup", ["in", "not_in"])
    def test_sequence_lookups_coerce_each_item(self, lookup: str) -> None:
        bob = TrPerson(id="bob")
        _, variables, _ = build_filter_condition("in", lookup, ["tr_person:a", bob], 0, "e", endpoint_columns=ENDPOINTS)
        assert variables == {"_f0": [RecordID("tr_person", "a"), RecordID("tr_person", "bob")]}

    @pytest.mark.parametrize(
        "lookup",
        [
            "like",
            "ilike",
            "contains",
            "not_contains",
            "containsall",
            "containsany",
            "startswith",
            "endswith",
            "match",
            "regex",
        ],
    )
    def test_text_lookups_are_refused(self, lookup: str) -> None:
        with pytest.raises(ValueError, match="edge endpoint column 'out'"):
            build_filter_condition("out", lookup, "tr_product:w", 0, "e", endpoint_columns=ENDPOINTS)

    def test_isnull_is_unchanged(self) -> None:
        assert build_filter_condition("in", "isnull", True, 0, "e", endpoint_columns=ENDPOINTS) == ("in IS NULL", {}, 0)

    def test_variable_references_keep_their_meaning(self) -> None:
        assert build_filter_condition("in", "exact", Var("x"), 0, "e", endpoint_columns=ENDPOINTS) == ("in = $x", {}, 0)

    def test_a_dollar_string_is_data_and_must_be_a_record_id(self) -> None:
        """A string value is data (v0.28.1): on an endpoint column, a 'table:id' record id."""
        with pytest.raises(ValueError):
            build_filter_condition("in", "exact", "$x", 0, "e", endpoint_columns=ENDPOINTS)

    @pytest.mark.parametrize("value", ["nocolon", "a;b:c", "tr_person:joe@x.com", 42, None, {"id": "tr_person:a"}])
    def test_invalid_endpoint_values_raise(self, value: object) -> None:
        with pytest.raises(ValueError):
            build_filter_condition("in", "exact", value, 0, "e", endpoint_columns=ENDPOINTS)

    def test_without_endpoint_columns_strings_are_bound_as_is(self) -> None:
        _, variables, _ = build_filter_condition("in", "exact", "tr_person:alice", 0, "e")
        assert variables == {"_f0": "tr_person:alice"}

    def test_dotted_path_through_an_endpoint_is_not_coerced(self) -> None:
        _, variables, _ = build_filter_condition("in.name", "exact", "tr_person:a", 0, "e", endpoint_columns=ENDPOINTS)
        assert variables == {"_f0": "tr_person:a"}

    def test_q_propagates_endpoint_columns(self) -> None:
        sql, variables, counter = Q(out="tr_product:w").to_sql(0, "e", None, ENDPOINTS)
        assert (sql, counter) == ("out = $_f0", 1)
        assert variables == {"_f0": RecordID("tr_product", "w")}

        nested = ~(Q(out="tr_product:w") | Q(in__in=["tr_person:a"]))
        sql, variables, _ = nested.to_sql(0, "e", None, ENDPOINTS)
        assert sql == "NOT ((out = $_f0 OR in IN $_f1))"
        assert variables == {"_f0": RecordID("tr_product", "w"), "_f1": [RecordID("tr_person", "a")]}

    def test_q_without_endpoint_columns_binds_strings(self) -> None:
        _, variables, _ = Q(out="tr_product:w").to_sql(0, "e")
        assert variables == {"_f0": "tr_product:w"}

    def test_node_model_declares_no_endpoint_columns(self) -> None:
        assert TrPerson._endpoint_columns() == frozenset()


class TestCoerceEndpoint:
    def test_record_id_is_returned_unchanged(self) -> None:
        rid = RecordID("tr_person", "a")
        assert coerce_endpoint(rid) is rid

    def test_instance_gives_its_record_id(self) -> None:
        assert coerce_endpoint(TrPerson(id="joe@x.com")) == RecordID("tr_person", "joe@x.com")

    def test_string_id_is_read_as_a_string(self) -> None:
        result = coerce_endpoint("tr_person:5")
        assert result == RecordID("tr_person", "5")
        assert result.id == "5"
        assert coerce_endpoint("tr_person:`a-b`").id == "a-b"

    def test_unsaved_instance_raises(self) -> None:
        with pytest.raises(ValueError, match="An unsaved TrPerson has no record id; save it first or pass a RecordID."):
            coerce_endpoint(TrPerson(name="x"))

    @pytest.mark.parametrize("value", [{"id": "tr_person:a"}, 42, True, None, ["tr_person:a"]])
    def test_other_types_raise(self, value: object) -> None:
        with pytest.raises(ValueError, match="an edge endpoint must be a model instance, a RecordID or a 'table:id'"):
            coerce_endpoint(value)

    @pytest.mark.parametrize("value", ["nocolon", "a;b:c", "tr_person:joe@x.com", "", "tr_person:"])
    def test_malformed_strings_raise(self, value: str) -> None:
        with pytest.raises(ValueError):
            coerce_endpoint(value)


# ==================== Task 2 — lookups on fields ending in "_" (unit) ====================


class TrTyped(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="tr_typed")

    id: str | None = None
    type_: int = 0


class TestParseLookupTrailingUnderscore:
    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("in___in", ("in_", "in")),
            ("type___gt", ("type_", "gt")),
            ("in___not_in", ("in_", "not_in")),
            ("in_", ("in_", "exact")),
            ("age__gt", ("age", "gt")),
            ("age", ("age", "exact")),
            ("a____lt", ("a__", "lt")),
        ],
    )
    def test_parse_lookup(self, key: str, expected: tuple[str, str]) -> None:
        assert parse_lookup(key) == expected

    def test_filter_on_trailing_underscore_field(self) -> None:
        query, variables = TrTyped.objects().filter(type___gt=1)._compile_query()
        assert query == "SELECT * FROM tr_typed WHERE type_ > $_f0;"
        assert variables == {"_f0": 1}

    def test_q_on_trailing_underscore_field(self) -> None:
        query, variables = TrTyped.objects().filter(Q(type___in=[1, 2]))._compile_query()
        assert query == "SELECT * FROM tr_typed WHERE type_ IN $_f0;"
        assert variables == {"_f0": [1, 2]}

    def test_criteria_payload_on_trailing_underscore_field(self) -> None:
        assert QuerySet._criteria_payload({"type_": 3, "type___gt": 1}) == {"type_": 3}


# ==================== Task 2 — filtering by instance (E2E, both lines) ====================


class TestFilterByInstanceE2E:
    @pytest.mark.asyncio
    async def test_filter_and_in_lookup_by_instance(self) -> None:
        async with orm_client("tr_customer", "tr_order"):
            alice = await TrCustomer(id="alice", name="Alice").save()
            bob = await TrCustomer(id="bob", name="Bob").save()
            await TrOrder(id="a1", customer=ALICE, note="a1").save()
            await TrOrder(id="a2", customer=ALICE, note="a2").save()
            await TrOrder(id="b1", customer=BOB, note="b1").save()

            mine = await TrOrder.objects().filter(customer=alice).order_by("note").exec()
            assert [order.note for order in mine] == ["a1", "a2"]
            assert all(order.customer == ALICE for order in mine)

            listed = await TrOrder.objects().filter(customer__in=[alice]).order_by("note").exec()
            assert [order.note for order in listed] == ["a1", "a2"]

            others = await TrOrder.objects().filter(customer__not_in=[alice]).exec()
            assert [order.note for order in others] == ["b1"]

            either = await TrOrder.objects().filter(Q(customer=bob) | Q(note="a2")).count()
            assert either == 2

    @pytest.mark.asyncio
    async def test_get_or_create_by_instance_converges(self) -> None:
        async with orm_client("tr_customer", "tr_order") as client:
            alice = await TrCustomer(id="alice", name="Alice").save()

            first, created = await TrOrder.objects().get_or_create(customer=alice, note="n1")
            assert created is True
            again, created_again = await TrOrder.objects().get_or_create(customer=alice, note="n1")
            assert created_again is False
            assert again.get_raw_id() == first.get_raw_id()
            assert await TrOrder.objects().count() == 1

            stored = await client.query("SELECT VALUE customer FROM tr_order WHERE note = $note;", {"note": "n1"})
            assert stored == [ALICE]
            assert isinstance(stored[0], RecordID)

    @pytest.mark.asyncio
    async def test_live_filter_by_instance(self) -> None:
        async with orm_client("tr_customer", "tr_order") as client:
            alice = await TrCustomer(id="alice", name="Alice").save()
            await TrCustomer(id="bob", name="Bob").save()
            # SurrealDB 3.x refuses a live query on an undefined table.
            await client.query("DEFINE TABLE tr_order SCHEMALESS;", {})
            async with asyncio.timeout(5):
                async with TrOrder.objects().filter(customer=alice).live() as stream:
                    # Bob's order first: if the filter let it through, it would be the first event.
                    await TrOrder(id="ob", customer=BOB, note="bob's").save()
                    await TrOrder(id="oa", customer=ALICE, note="alice's").save()
                    event = await stream.__anext__()

            assert event.action is LiveAction.CREATE
            assert event.record_id == "tr_order:oa"
            assert isinstance(event.instance, TrOrder)
            assert event.instance.customer == ALICE


# ==================== Task 3 — the edge model (unit) ====================


class TrIntNode(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="tr_int_node")

    id: int | None = None


PERSON_A = RecordID("tr_person", "a")
WIDGET = RecordID("tr_product", "widget")


class TestEdgeModel:
    def test_exported(self) -> None:
        import surreal_orm_lite
        from surreal_orm_lite.edge import BaseSurrealEdge as from_module

        assert "BaseSurrealEdge" in surreal_orm_lite.__all__
        assert surreal_orm_lite.BaseSurrealEdge is BaseSurrealEdge is from_module
        assert issubclass(BaseSurrealEdge, BaseSurrealModel)

    def test_declared_fields(self) -> None:
        assert set(TrPurchased.model_fields) == {"id", "in_", "out", "quantity", "price"}
        assert TrPurchased.get_table_name() == "tr_purchased"
        edge = TrPurchased(id="p1", in_=PERSON_A, out=WIDGET)
        assert edge._record_id() == RecordID("tr_purchased", "p1")

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (RecordID("tr_person", "a"), RecordID("tr_person", "a")),
            (TrPerson(id="a"), RecordID("tr_person", "a")),
            ("tr_person:a", RecordID("tr_person", "a")),
            ("tr_person:5", RecordID("tr_person", "5")),
            ("tr_person:`a-b`", RecordID("tr_person", "a-b")),
            (TrPerson(id="joe@x.com"), RecordID("tr_person", "joe@x.com")),
            (TrIntNode(id=5), RecordID("tr_int_node", 5)),
        ],
        ids=["record-id", "node", "string", "digit-string", "backticked", "non-identifier-id", "int-id-node"],
    )
    def test_endpoint_from_record_id_node_and_string(self, value: object, expected: RecordID) -> None:
        as_in = TrPurchased(in_=value, out=WIDGET)
        as_out = TrPurchased(in_=PERSON_A, out=value)
        for endpoint in (as_in.in_, as_out.out):
            assert isinstance(endpoint, RecordID)
            assert endpoint == expected
            # The id keeps its type: a string id stays a string, an integer id an integer.
            assert type(endpoint.id) is type(expected.id)

    def test_endpoint_accepts_column_name_in(self) -> None:
        edge = TrPurchased.model_validate({"in": TrPerson(id="a"), "out": "tr_product:widget", "quantity": 2})
        assert edge.in_ == PERSON_A
        assert edge.out == WIDGET
        assert edge.quantity == 2
        by_name = TrPurchased.model_validate({"in_": "tr_person:a", "out": WIDGET})
        assert by_name.in_ == PERSON_A

    @pytest.mark.parametrize(
        "value",
        [TrPerson(name="unsaved"), "nocolon", "a;b:c", "tr_person:joe@x.com", "", 42, None, {"id": "tr_person:a"}],
        ids=["unsaved-node", "no-colon", "injection", "non-identifier-id", "empty", "int", "none", "dict"],
    )
    def test_invalid_endpoints_raise_validation_error(self, value: object) -> None:
        # pytest.raises(ValidationError) also proves the SDK's InvalidRecordIdError never escapes.
        with pytest.raises(ValidationError) as as_in:
            TrPurchased(in_=value, out=WIDGET)
        assert [error["type"] for error in as_in.value.errors()] == ["value_error"]
        with pytest.raises(ValidationError):
            TrPurchased(in_=PERSON_A, out=value)
        with pytest.raises(ValidationError):
            TrPurchased.model_validate({"in": value, "out": WIDGET})

    def test_unsaved_node_message(self) -> None:
        with pytest.raises(ValidationError, match="An unsaved TrPerson has no record id"):
            TrPurchased(in_=TrPerson(name="x"), out=WIDGET)

    def test_missing_endpoint_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError) as missing_out:
            TrPurchased.model_validate({"in": PERSON_A})
        assert [error["type"] for error in missing_out.value.errors()] == ["missing"]
        with pytest.raises(ValidationError):
            TrPurchased.model_validate({"out": WIDGET})

    def test_column_mapping(self) -> None:
        assert TrPurchased.get_field_aliases() == {"in_": "in"}
        assert TrPurchased.to_db_field("in_") == "in"
        assert TrPurchased.to_db_field("out") == "out"
        assert TrPurchased.to_py_field("in") == "in_"
        assert TrPurchased.to_db_field("in_.name") == "in.name"

        payload = TrPurchased(in_=PERSON_A, out=WIDGET, quantity=2, price=9.5)._write_payload()
        assert payload == {"in": PERSON_A, "out": WIDGET, "quantity": 2, "price": 9.5}
        assert isinstance(payload["in"], RecordID)
        assert isinstance(payload["out"], RecordID)

        query, variables = TrPurchased.objects().filter(in_=PERSON_A)._compile_query()
        assert query == "SELECT * FROM tr_purchased WHERE in = $_f0;"
        assert variables == {"_f0": PERSON_A}

        query, _ = TrPurchased.objects().order_by("in_")._compile_query()
        assert query == "SELECT * FROM tr_purchased ORDER BY in ASC;"

    def test_alias_map_is_complete_whatever_the_call_order(self) -> None:
        class TrFreshEdge(BaseSurrealEdge):
            model_config = SurrealConfigDict(table_name="tr_fresh_edge")

            note: str = ""

        # The base implementation first: there is no override that could be bypassed.
        forward, reverse = BaseSurrealModel._alias_maps.__func__(TrFreshEdge)
        assert (forward, reverse) == ({"in_": "in"}, {"in": "in_"})
        assert TrFreshEdge.get_field_aliases() == {"in_": "in"}

        class TrFreshParent(BaseSurrealEdge):
            model_config = SurrealConfigDict(table_name="tr_fresh_parent")

        class TrFreshChild(TrFreshParent):
            extra: int = 0

        # The child's map is built before its parent's.
        assert TrFreshChild.get_field_aliases() == {"in_": "in"}
        assert TrFreshChild.to_py_field("in") == "in_"
        assert TrFreshParent.get_field_aliases() == {"in_": "in"}
        assert TrFreshChild.get_table_name() == "tr_fresh_parent"

    def test_subclass_of_a_concrete_edge_maps_in(self) -> None:
        class TrPurchasedSub(TrPurchased):
            gift: bool = False

        assert TrPurchasedSub.get_field_aliases() == {"in_": "in"}
        edge = TrPurchasedSub(in_=PERSON_A, out=WIDGET, gift=True)
        assert edge._write_payload() == {"in": PERSON_A, "out": WIDGET, "quantity": 1, "price": 0.0, "gift": True}
        assert TrPurchasedSub._endpoint_columns() == frozenset({"in", "out"})

    def test_user_alias_on_data_field_still_works(self) -> None:
        class TrMemoEdge(BaseSurrealEdge):
            model_config = SurrealConfigDict(table_name="tr_memo_edge")

            note: str = Field(default="", alias="memo")

        assert TrMemoEdge.get_field_aliases() == {"in_": "in", "note": "memo"}
        assert TrMemoEdge.to_py_field("memo") == "note"
        edge = TrMemoEdge(in_=PERSON_A, out=WIDGET, note="hi")
        assert edge._write_payload() == {"in": PERSON_A, "out": WIDGET, "memo": "hi"}
        row = TrMemoEdge.from_db({"id": RecordID("tr_memo_edge", "m"), "in": PERSON_A, "out": WIDGET, "memo": "x"})
        assert isinstance(row, TrMemoEdge)
        assert (row.in_, row.note) == (PERSON_A, "x")

    def test_user_alias_onto_endpoint_column_rejected(self) -> None:
        class TrAliasOut(BaseSurrealEdge):
            x: str = Field(default="", alias="out")

        with pytest.raises(ValueError, match="collides with another field"):
            TrAliasOut.get_field_aliases()

        class TrAliasIn(BaseSurrealEdge):
            x: str = Field(default="", alias="in")

        with pytest.raises(ValueError, match="in_ and x both alias the column 'in'"):
            TrAliasIn.get_field_aliases()

    def test_endpoint_filters_coerce_strings(self) -> None:
        query, variables = TrPurchased.objects().filter(in_="tr_person:alice")._compile_query()
        assert query == "SELECT * FROM tr_purchased WHERE in = $_f0;"
        assert variables == {"_f0": RecordID("tr_person", "alice")}
        assert isinstance(variables["_f0"], RecordID)

        bob = TrPerson(id="bob")
        query, variables = TrPurchased.objects().filter(in___in=["tr_person:a", bob])._compile_query()
        assert query == "SELECT * FROM tr_purchased WHERE in IN $_f0;"
        assert variables == {"_f0": [RecordID("tr_person", "a"), RecordID("tr_person", "bob")]}

        _, variables = TrPurchased.objects().filter(Q(out="tr_product:widget"))._compile_query()
        assert variables == {"_f0": WIDGET}

        with pytest.raises(ValueError, match="edge endpoint column 'in'"):
            TrPurchased.objects().filter(in___contains="x")._compile_query()

    def test_endpoints_are_frozen(self) -> None:
        edge = TrPurchased(in_=PERSON_A, out=WIDGET)
        with pytest.raises(ValidationError, match="frozen"):
            edge.out = RecordID("tr_product", "other")
        with pytest.raises(ValidationError, match="frozen"):
            edge.in_ = RecordID("tr_person", "b")
        assert (edge.in_, edge.out) == (PERSON_A, WIDGET)

        edge.quantity = 3  # data fields stay writable
        assert edge.quantity == 3

    def test_model_copy_is_not_checked(self) -> None:
        """Documented, not refused: a copy meant as a new edge (``id=None``) is legitimate."""
        edge = TrPurchased(id="p1", in_=PERSON_A, out=WIDGET)
        other = RecordID("tr_product", "other")
        copy = edge.model_copy(update={"id": None, "out": other})
        assert (copy.id, copy.in_, copy.out) == (None, PERSON_A, other)
        assert edge.out == WIDGET

    def test_redeclaring_an_endpoint_is_refused(self) -> None:
        with pytest.raises(TypeError, match="TrBadOut: 'in_'/'out' are the endpoints declared by BaseSurrealEdge"):

            class TrBadOut(BaseSurrealEdge):
                out: RecordID

        with pytest.raises(TypeError, match="cannot be redeclared"):

            class TrBadIn(BaseSurrealEdge):
                in_: RecordID | None = None

        with pytest.raises(TypeError, match="cannot be redeclared"):

            class TrBadSub(TrPurchased):
                out: RecordID = Field(frozen=True)

    def test_hydrates_from_row(self) -> None:
        edge = TrPurchased.from_db(
            {"id": RecordID("tr_purchased", "x"), "in": PERSON_A, "out": WIDGET, "quantity": 2, "price": 1.5}
        )
        assert isinstance(edge, TrPurchased)
        assert isinstance(edge.id, RecordID)
        assert edge.id == RecordID("tr_purchased", "x")
        assert (edge.in_, edge.out, edge.quantity, edge.price) == (PERSON_A, WIDGET, 2, 1.5)
        assert edge._record_id() == RecordID("tr_purchased", "x")

    def test_apply_record_still_updates_endpoints(self) -> None:
        """Hydrating a returned row bypasses validation (``object.__setattr__``), frozen or not."""
        edge = TrPurchased(in_=PERSON_A, out=WIDGET)
        edge._apply_record({"id": RecordID("tr_purchased", "x"), "in": RecordID("tr_person", "b")})
        assert edge.in_ == RecordID("tr_person", "b")
        assert edge.id == RecordID("tr_purchased", "x")

    def test_marker_and_hooks(self) -> None:
        assert BaseSurrealEdge.__surreal_edge__ is True
        assert TrPurchased.__surreal_edge__ is True
        assert BaseSurrealModel.__surreal_edge__ is False
        assert TrPerson.__surreal_edge__ is False
        assert "__surreal_edge__" not in TrPurchased.model_fields

        assert TrPurchased._endpoint_columns() == frozenset({"in", "out"})
        assert TrPerson._endpoint_columns() == frozenset()
        assert TrPurchased._write_protected_fields() == frozenset({"in_", "out"})
        assert TrPerson._write_protected_fields() == frozenset()
        assert TrPurchased._implicit_aliases() == {"in_": "in"}
        assert TrPerson._implicit_aliases() == {}
        assert TrFollows.get_field_aliases() == {"in_": "in"}


# ==================== Task 4 — creating an edge with save() (E2E, both lines) ====================

EDGE_TABLES = ("tr_person", "tr_product", "tr_purchased", "tr_follows")
SAVE_TABLES = (*EDGE_TABLES, "tr_rel_strict", "tr_rel_enf", "tr_normal", "tr_srv")
ALICE_P = RecordID("tr_person", "alice")
BOB_P = RecordID("tr_person", "bob")


class TrStrictEdge(BaseSurrealEdge):
    """Edge on ``tr_rel_strict`` (``TYPE RELATION IN tr_person OUT tr_product``)."""

    model_config = SurrealConfigDict(table_name="tr_rel_strict")


class TrEnforcedEdge(BaseSurrealEdge):
    """Edge on ``tr_rel_enf`` (``… ENFORCED``)."""

    model_config = SurrealConfigDict(table_name="tr_rel_enf")


class TrNormalEdge(BaseSurrealEdge):
    """Edge model pointed at ``tr_normal``, a ``TYPE NORMAL`` table."""

    model_config = SurrealConfigDict(table_name="tr_normal")


class TrSrvEdge(BaseSurrealEdge):
    """Edge whose ``source`` column the server fills in (``DEFINE FIELD … DEFAULT``)."""

    model_config = SurrealConfigDict(table_name="tr_srv", server_fields=["source"])

    source: str | None = None


async def _nodes() -> tuple[TrPerson, TrPerson, TrProduct]:
    """Save alice, bob and widget. Call it inside the ``orm_client`` block (it drops the tables)."""
    alice = await TrPerson(id="alice", name="Alice").save()
    bob = await TrPerson(id="bob", name="Bob").save()
    widget = await TrProduct(id="widget", name="Widget").save()
    return alice, bob, widget


async def _server_version() -> tuple[int, int, int]:
    """The connected server's version: ``"surrealdb-3.1.5"`` → ``(3, 1, 5)``."""
    raw = str(await (await SurrealDBConnectionManager.get_client()).version())
    major, minor, patch = (int(n) for n in re.findall(r"\d+", raw)[:3])
    return major, minor, patch


def _http_url() -> str:
    return f"http://{os.environ.get('SURREALDB_HOST', 'localhost')}:{os.environ.get('SURREALDB_PORT', '8000')}"


@contextlib.asynccontextmanager
async def http_orm_client(*tables: str) -> AsyncIterator[Any]:
    """``orm_client`` over HTTP: connected as root, ``tables`` dropped before and after."""
    SurrealDBConnectionManager.set_connection(url=_http_url(), user="root", password="root", namespace="ns", database="db")
    client = await SurrealDBConnectionManager.get_client()
    for table in tables:
        with contextlib.suppress(Exception):
            await client.query(f"REMOVE TABLE {table};", {})
    try:
        yield client
    finally:
        for table in tables:
            with contextlib.suppress(Exception):
                await client.query(f"REMOVE TABLE {table};", {})
        await SurrealDBConnectionManager.close_connection()


async def _define_tables(client: Any, *tables: str) -> None:
    """Define ``tables`` before a transaction meant to fail, so that it never creates one implicitly.

    Measured on 2026-10-12: on SurrealDB 2.7.0, a table first created implicitly inside a
    transaction that then rolls back is left half-known to the server — later writes store their
    rows but never define the table, so ``INFO FOR DB`` misses it and ``REMOVE TABLE`` fails ("does
    not exist"), leaving rows behind for the next test. 3.3.3 and 3.1.5 do not. Any write does it
    (``CREATE``, ``RELATE``, ``INSERT RELATION``); a ``DEFINE TABLE`` heals it.
    """
    for table in tables:
        await client.query(f"DEFINE TABLE IF NOT EXISTS {table};", {})


async def _native_txn_supported() -> bool:
    """True if the server runs native interactive transactions (SurrealDB 3.x).

    The probe of ``tests/test_transactions.py``: ``client.begin()`` then ``cancel()``.
    """
    client = await SurrealDBConnectionManager.get_client()
    try:
        txn = await client.begin()
    except Exception:
        return False
    with contextlib.suppress(Exception):
        await client.cancel(txn)
    return True


class _InsertRelationSpy:
    """Stands in for the connected SDK client's ``insert_relation()``: records each call, then forwards it."""

    def __init__(self, client: Any) -> None:
        self.calls: list[tuple[Any, Any]] = []
        self._forward = client.insert_relation

    async def __call__(self, table: Any, data: Any, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((table, data))
        return await self._forward(table, data, *args, **kwargs)


def _spy_insert_relation(monkeypatch: pytest.MonkeyPatch, client: Any) -> _InsertRelationSpy:
    spy = _InsertRelationSpy(client)
    monkeypatch.setattr(client, "insert_relation", spy)
    return spy


async def _stored(client: Any, table: str) -> list[Any]:
    """Every row of ``table``, sorted by id; ``[]`` for a table never created (3.x raises, 2.x does not)."""
    try:
        rows = await client.query(f"SELECT * FROM {table} ORDER BY id;", {})
    except NotFoundError:
        return []
    return rows if isinstance(rows, list) else []


class TestEdgeSaveE2E:
    @pytest.mark.asyncio
    async def test_save_creates_traversable_edge_via_insert_relation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with orm_client(*SAVE_TABLES) as client:
            alice, _bob, widget = await _nodes()
            spy = _spy_insert_relation(monkeypatch, client)

            edge = await TrPurchased(in_=alice, out=widget, quantity=2).save()

            assert spy.calls == [("tr_purchased", {"in": ALICE_P, "out": WIDGET, "quantity": 2, "price": 0.0})]
            assert isinstance(edge.id, RecordID)
            assert edge.id.table_name == "tr_purchased"
            assert (edge.in_, edge.out, edge.quantity) == (ALICE_P, WIDGET, 2)

            traversed = await client.query("SELECT VALUE ->tr_purchased->? FROM ONLY tr_person:alice;", {})
            assert traversed == [WIDGET]
            related = await alice.get_related("tr_purchased", model_class=TrProduct)
            assert [(type(p), p.get_raw_id()) for p in related] == [(TrProduct, "widget")]

            loaded = await TrPurchased.objects().get(edge.get_raw_id())
            assert (loaded.id, loaded.in_, loaded.out, loaded.quantity) == (edge.id, ALICE_P, WIDGET, 2)

    @pytest.mark.asyncio
    async def test_save_over_http(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with http_orm_client(*SAVE_TABLES) as client:
            alice, _bob, widget = await _nodes()
            spy = _spy_insert_relation(monkeypatch, client)

            edge = await TrPurchased(in_=alice, out=widget, quantity=3).save()
            assert spy.calls == [("tr_purchased", {"in": ALICE_P, "out": WIDGET, "quantity": 3, "price": 0.0})]
            assert isinstance(edge.id, RecordID)
            assert edge.id.table_name == "tr_purchased"

            explicit = await TrPurchased(id="h1", in_=alice, out=widget).save()
            assert len(spy.calls) == 1
            assert explicit.id == RecordID("tr_purchased", "h1")

            with pytest.raises(SurrealDbError, match="already exists"):
                await TrPurchased(id="h1", in_=alice, out=widget, quantity=9).save()

            traversed = await client.query("SELECT VALUE ->tr_purchased->? FROM ONLY tr_person:alice;", {})
            assert traversed == [WIDGET, WIDGET]
            assert sorted(row["quantity"] for row in await _stored(client, "tr_purchased")) == [1, 3]

    @pytest.mark.asyncio
    async def test_explicit_id_uses_guarded_statement(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with orm_client(*SAVE_TABLES) as client:
            alice, _bob, widget = await _nodes()
            spy = _spy_insert_relation(monkeypatch, client)

            edge = await TrPurchased(id="p1", in_=alice, out=widget, quantity=2).save()
            assert spy.calls == []
            assert edge.id == RecordID("tr_purchased", "p1")

            # An id that is already a RecordID of the edge table is accepted as is.
            same_table = await TrPurchased(id=RecordID("tr_purchased", "p2"), in_=alice, out=widget).save()
            assert spy.calls == []
            assert same_table.id == RecordID("tr_purchased", "p2")

            traversed = await client.query("SELECT VALUE ->tr_purchased->? FROM ONLY tr_person:alice;", {})
            assert traversed == [WIDGET, WIDGET]
            assert [row["id"] for row in await _stored(client, "tr_purchased")] == [
                RecordID("tr_purchased", "p1"),
                RecordID("tr_purchased", "p2"),
            ]

    @pytest.mark.asyncio
    async def test_duplicate_explicit_id_raises_on_both_lines(self) -> None:
        async with orm_client(*SAVE_TABLES) as client:
            alice, bob, widget = await _nodes()
            await TrPurchased(id="p1", in_=alice, out=widget, quantity=2).save()

            with pytest.raises(SurrealDbError, match="already exists"):
                await TrPurchased(id="p1", in_=alice, out=widget, quantity=5).save()

            # Other endpoints under the same id: 2.x and 3.1.5 report a field mismatch rather
            # than "already exists" (spike 2026-10-12) — still an ORM error, still nothing changed.
            with pytest.raises(SurrealDbError):
                await TrPurchased(id="p1", in_=bob, out=widget, quantity=7).save()

            stored = await _stored(client, "tr_purchased")
            assert [(row["id"], row["in"], row["out"], row["quantity"]) for row in stored] == [
                (RecordID("tr_purchased", "p1"), ALICE_P, WIDGET, 2)
            ]
            # …and it is a relation, not a plain record carrying `in`/`out` fields.
            traversed = await client.query("SELECT VALUE ->tr_purchased->? FROM ONLY tr_person:alice;", {})
            assert traversed == [WIDGET]

    @pytest.mark.asyncio
    async def test_raw_insert_relation_duplicate_follows_version_rule(self) -> None:
        """Pin of the raw SDK call with a duplicate explicit edge id (spec §2.2, rule R6).

        Measured on 2026-10-12: SurrealDB 2.7.0 and 3.1.5 silently keep the old edge and return
        it; 3.3.3 raises AlreadyExistsError. The SurrealDB docs announce a record-exists error
        'from SurrealDB 3.1.5'; 3.1.5 measured otherwise. This is why save()/bulk_create() use
        the guarded statement. A failure means the server changed: the guard stays correct, but
        README row 2 (ORM behaviour table) must be re-measured and updated.
        """
        async with orm_client(*SAVE_TABLES) as client:
            version = await _server_version()
            keeps_old = (2, 7, 0) <= version < (3, 0, 0) or version == (3, 1, 5)
            raises = version >= (3, 3, 3)
            if not keeps_old and not raises:
                pytest.skip(f"raw duplicate behaviour not measured on {version}")

            row = {"id": "raw1", "in": ALICE_P, "out": WIDGET, "quantity": 2}
            first = await client.insert_relation("tr_purchased", row)
            assert [(r["id"], r["quantity"]) for r in first] == [(RecordID("tr_purchased", "raw1"), 2)]

            if keeps_old:
                again = await client.insert_relation("tr_purchased", {**row, "quantity": 5})
                assert [(r["id"], r["quantity"]) for r in again] == [(RecordID("tr_purchased", "raw1"), 2)]
            else:
                with pytest.raises(AlreadyExistsError):
                    await client.insert_relation("tr_purchased", {**row, "quantity": 5})

            stored = await _stored(client, "tr_purchased")
            assert [(r["id"], r["quantity"]) for r in stored] == [(RecordID("tr_purchased", "raw1"), 2)]

    @pytest.mark.asyncio
    async def test_foreign_table_record_id_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with orm_client(*SAVE_TABLES, "other") as client:
            alice, _bob, widget = await _nodes()
            spy = _spy_insert_relation(monkeypatch, client)
            query_calls: list[str] = []
            forward_query = client.query

            async def recording_query(query: str, *args: Any, **kwargs: Any) -> Any:
                query_calls.append(query)
                return await forward_query(query, *args, **kwargs)

            monkeypatch.setattr(client, "query", recording_query)

            with pytest.raises(
                ValueError, match=r"TrPurchased: id .* belongs to table 'other', not to the edge table 'tr_purchased'"
            ):
                await TrPurchased(id=RecordID("other", "x"), in_=alice, out=widget).save()

            boolean = TrPurchased(in_=alice, out=widget)
            object.__setattr__(boolean, "id", True)  # `id: str | RecordID | None` refuses True at validation
            with pytest.raises(ValueError, match="a record id cannot be a boolean"):
                await boolean.save()

            assert spy.calls == []
            assert query_calls == []
            monkeypatch.undo()
            assert await _stored(client, "tr_purchased") == []
            assert await _stored(client, "other") == []

    @pytest.mark.asyncio
    async def test_type_relation_wrong_endpoint_table(self) -> None:
        async with orm_client(*SAVE_TABLES) as client:
            _alice, bob, widget = await _nodes()
            await client.query("DEFINE TABLE tr_rel_strict TYPE RELATION IN tr_person OUT tr_product;", {})

            with pytest.raises(SurrealDbError, match="There was a problem with the database") as auto:
                await TrStrictEdge(in_=bob, out=bob).save()
            assert not isinstance(auto.value, SurrealDbNotFoundError)
            with pytest.raises(SurrealDbError, match="There was a problem with the database"):
                await TrStrictEdge(id="s1", in_=bob, out=bob).save()
            assert await _stored(client, "tr_rel_strict") == []

            # The declared tables are accepted.
            ok = await TrStrictEdge(in_=bob, out=widget).save()
            assert isinstance(ok.id, RecordID)

    @pytest.mark.asyncio
    async def test_enforced_missing_endpoint_is_not_found(self) -> None:
        async with orm_client(*SAVE_TABLES) as client:
            _alice, _bob, widget = await _nodes()
            await client.query("DEFINE TABLE tr_rel_enf TYPE RELATION IN tr_person OUT tr_product ENFORCED;", {})
            ghost = RecordID("tr_person", "ghost")

            with pytest.raises(SurrealDbNotFoundError, match="does not exist"):
                await TrEnforcedEdge(in_=ghost, out=widget).save()
            with pytest.raises(SurrealDbNotFoundError, match="does not exist"):
                await TrEnforcedEdge(id="e1", in_=ghost, out=widget).save()
            assert await _stored(client, "tr_rel_enf") == []

    @pytest.mark.asyncio
    async def test_normal_table_rejected(self) -> None:
        async with orm_client(*SAVE_TABLES) as client:
            alice, _bob, widget = await _nodes()
            await client.query("DEFINE TABLE tr_normal TYPE NORMAL;", {})

            with pytest.raises(SurrealDbError, match="There was a problem with the database"):
                await TrNormalEdge(in_=alice, out=widget).save()
            with pytest.raises(SurrealDbError, match="There was a problem with the database"):
                await TrNormalEdge(id="n1", in_=alice, out=widget).save()
            assert await _stored(client, "tr_normal") == []

    @pytest.mark.asyncio
    async def test_server_fields_hydrated_after_save(self) -> None:
        async with orm_client(*SAVE_TABLES) as client:
            alice, _bob, widget = await _nodes()
            await client.query('DEFINE TABLE tr_srv TYPE RELATION; DEFINE FIELD source ON tr_srv DEFAULT "server";', {})

            auto = await TrSrvEdge(in_=alice, out=widget, source="client").save()
            assert auto.source == "server"  # not sent on a create: the DEFAULT applies, and comes back
            explicit = await TrSrvEdge(id="s1", in_=alice, out=widget).save()
            assert explicit.source == "server"
            assert explicit.id == RecordID("tr_srv", "s1")

            assert sorted(row["source"] for row in await _stored(client, "tr_srv")) == ["server", "server"]

    @pytest.mark.asyncio
    async def test_save_signals(self) -> None:
        events: list[tuple[str, Any, bool | None]] = []

        @pre_save.connect(TrPurchased)
        async def on_pre(sender: Any, instance: Any, **kwargs: Any) -> None:
            events.append(("pre_save", instance.id, None))

        @post_save.connect(TrPurchased)
        async def on_post(sender: Any, instance: Any, created: bool, **kwargs: Any) -> None:
            events.append(("post_save", instance.id, created))

        try:
            async with orm_client(*SAVE_TABLES):
                alice, _bob, widget = await _nodes()
                edge = await TrPurchased(in_=alice, out=widget).save()
                assert events == [("pre_save", None, None), ("post_save", edge.id, True)]
                assert isinstance(edge.id, RecordID)

                events.clear()
                with pytest.raises(SurrealDbError, match="already exists"):
                    await TrPurchased(id=edge.id, in_=alice, out=widget).save()
                assert [event[0] for event in events] == ["pre_save"]  # no post_save for a refused write
        finally:
            pre_save.disconnect(on_pre, TrPurchased)
            post_save.disconnect(on_post, TrPurchased)

    @pytest.mark.asyncio
    async def test_filter_by_string_endpoint_finds_saved_edge(self) -> None:
        async with orm_client(*SAVE_TABLES):
            alice, bob, widget = await _nodes()
            saved = await TrPurchased(in_=alice, out=widget, quantity=4).save()

            found = await TrPurchased.objects().filter(in_="tr_person:alice").exec()
            assert [(edge.id, edge.in_, edge.out, edge.quantity) for edge in found] == [(saved.id, ALICE_P, WIDGET, 4)]
            assert await TrPurchased.objects().filter(in___in=["tr_person:alice", bob]).count() == 1
            assert await TrPurchased.objects().filter(Q(out="tr_product:widget")).count() == 1
            assert await TrPurchased.objects().filter(in_="tr_person:bob").count() == 0


# ==================== Task 4 — a write refused by PERMISSIONS (E2E, both lines) ====================


class TrEdgeUser(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(access_name="tr_edge_acct", table_name="tr_edge_user")

    id: str | None = None
    email: str
    password: str


class TrPermEdge(BaseSurrealEdge):
    model_config = SurrealConfigDict(table_name="tr_perm")

    quantity: int = 1


def _connect_root_ws() -> None:
    host, port = os.environ.get("SURREALDB_HOST", "localhost"), os.environ.get("SURREALDB_PORT", "8000")
    SurrealDBConnectionManager.set_connection(f"ws://{host}:{port}/rpc", "root", "root", "ns", "db")


@contextlib.asynccontextmanager
async def _perm_edge_record_user() -> AsyncIterator[None]:
    """Connected over WebSocket as a fresh record user of ``tr_edge_acct``.

    As root first: ``tr_perm`` is (re)defined as a relation table whose ``PERMISSIONS`` let a
    record user create an edge only when ``quantity > 1`` (and select every edge), and the access
    is defined. On exit, as root again: the access and both tables are removed.
    """
    _connect_root_ws()
    try:
        root = await SurrealDBConnectionManager.get_client()
        with contextlib.suppress(Exception):
            await root.query("REMOVE TABLE tr_perm;", {})
        await root.query("DEFINE TABLE tr_perm TYPE RELATION PERMISSIONS FOR select FULL, FOR create WHERE quantity > 1;", {})
        await TrEdgeUser.define_access()
        await TrEdgeUser.signup(email=f"{uuid4().hex}@example.test", password="s3cret-passphrase", bind=True)
        yield
    finally:
        await SurrealDBConnectionManager.unset_connection()
        _connect_root_ws()
        with contextlib.suppress(Exception):
            root = await SurrealDBConnectionManager.get_client()
            await root.query("REMOVE ACCESS tr_edge_acct ON DATABASE;", {})
            await root.query("REMOVE TABLE tr_perm;", {})
            await root.query("REMOVE TABLE tr_edge_user;", {})
        await SurrealDBConnectionManager.unset_connection()


async def _stored_perm_quantities_as_root() -> list[Any]:
    """Reconnect as root (the record user may not see what it did not write) and read ``tr_perm``."""
    await SurrealDBConnectionManager.unset_connection()
    _connect_root_ws()
    root = await SurrealDBConnectionManager.get_client()
    rows = await root.query("SELECT VALUE quantity FROM tr_perm;", {})
    return rows if isinstance(rows, list) else []


class TestEdgePermissionsE2E:
    @pytest.mark.asyncio
    async def test_save_refused_by_permissions_raises(self) -> None:
        """The server answers a refused ``INSERT RELATION`` with no row and no error (spec §2.7)."""
        saved: list[Any] = []

        @post_save.connect(TrPermEdge)
        async def on_post(sender: Any, instance: Any, created: bool, **kwargs: Any) -> None:
            saved.append(instance.id)

        try:
            async with _perm_edge_record_user():
                a, w = RecordID("tr_person", "a"), RecordID("tr_product", "w")
                with pytest.raises(SurrealDbError, match="no record returned"):
                    await TrPermEdge(in_=a, out=w, quantity=1).save()  # auto id: insert_relation()
                with pytest.raises(SurrealDbError, match="no record returned"):
                    await TrPermEdge(id="refused", in_=a, out=w, quantity=1).save()  # explicit id: the guard
                assert saved == []

                allowed = await TrPermEdge(in_=a, out=w, quantity=2).save()  # the same user may create this one
                assert isinstance(allowed.id, RecordID)
                assert saved == [allowed.id]

                assert await _stored_perm_quantities_as_root() == [2]
        finally:
            post_save.disconnect(on_post, TrPermEdge)

    @pytest.mark.asyncio
    async def test_interactive_tx_refused_write_raises_and_rolls_back(self) -> None:
        """Inside an interactive transaction a refused create raises in the block, rolling the batch back (§4.3.4)."""
        saved: list[Any] = []

        @post_save.connect(TrPermEdge)
        async def on_post(sender: Any, instance: Any, created: bool, **kwargs: Any) -> None:
            saved.append(instance.id)

        try:
            async with _perm_edge_record_user():
                if not await _native_txn_supported():
                    pytest.skip("requires SurrealDB 3.x native transactions")
                a, w = RecordID("tr_person", "a"), RecordID("tr_product", "w")
                reached = False
                with pytest.raises(SurrealDbError, match="no record returned"):
                    async with SurrealDBConnectionManager.transaction() as tx:
                        assert tx.is_interactive
                        allowed = await TrPermEdge(in_=a, out=w, quantity=2).save(tx=tx)
                        assert isinstance(allowed.id, RecordID)  # accepted: its row came back in the block
                        await TrPermEdge(in_=a, out=w, quantity=1).save(tx=tx)
                        reached = True
                assert not reached  # raised by save(), not at commit
                assert saved == []  # post_save waits for a commit that never happens

                assert await _stored_perm_quantities_as_root() == []  # the accepted edge is rolled back too
        finally:
            post_save.disconnect(on_post, TrPermEdge)

    @pytest.mark.asyncio
    async def test_bulk_create_partly_refused_reports_count(self) -> None:
        """A batch the ``PERMISSIONS`` partly refuse raises with the count; the accepted edge stays (§4.3.4).

        Outside a transaction a batch is not atomic: the server writes the rows it accepts and
        answers with those only, so the ORM cannot return the hydrated batch — it says how many
        came back. Same on the ``insert_relation()`` path (auto ids) and the guarded one.
        """
        async with _perm_edge_record_user():
            a, w = RecordID("tr_person", "a"), RecordID("tr_product", "w")
            with pytest.raises(SurrealDbError, match="Only 1 of 2 edges came back") as auto:
                await TrPermEdge.objects().bulk_create(
                    [TrPermEdge(in_=a, out=w, quantity=1), TrPermEdge(in_=a, out=w, quantity=2)]
                )
            assert "not atomic" in str(auto.value)
            assert await _stored_perm_quantities_as_root() == [2]

        async with _perm_edge_record_user():
            a, w = RecordID("tr_person", "a"), RecordID("tr_product", "w")
            with pytest.raises(SurrealDbError, match="Only 1 of 2 edges came back"):
                await TrPermEdge.objects().bulk_create(
                    [TrPermEdge(id="r1", in_=a, out=w, quantity=1), TrPermEdge(id="r2", in_=a, out=w, quantity=2)]
                )
            assert await _stored_perm_quantities_as_root() == [2]


# ==================== Task 5 — edges inside transactions (spec §4.5) ====================


class TestEdgeTransactionStatements:
    """What a buffered transaction queues for an edge (no server: nothing runs before commit)."""

    @pytest.mark.asyncio
    async def test_buffered_save_queues_guarded_statement(self) -> None:
        tx = BufferedTransaction()
        auto = await TrPurchased(in_=ALICE_P, out=WIDGET, quantity=2).save(tx=tx)
        explicit = await TrFollows(id="f1", in_=ALICE_P, out=BOB_P).save(tx=tx)

        assert auto.id is None  # an auto id is allowed on a buffered transaction; no row yet
        assert explicit.id == "f1"
        # The guarded statement, its `$data` renamed per statement; `$before`/`$after` are the
        # statement's own parameters and are left alone.
        assert tx.statements == [
            f"INSERT RELATION INTO {table} $t{n}_data RETURN VALUE IF $before != NONE "
            f"{{ THROW 'Database record `' + <string> $before.id + '` already exists' }} ELSE {{ $after }};"
            for n, table in enumerate(("tr_purchased", "tr_follows"))
        ]
        assert tx.variables == {
            "t0_data": {"in": ALICE_P, "out": WIDGET, "quantity": 2, "price": 0.0},
            "t1_data": {"in": ALICE_P, "out": BOB_P, "weight": 1, "id": RecordID("tr_follows", "f1")},
        }

    @pytest.mark.asyncio
    async def test_buffered_save_checks_the_id_before_queueing(self) -> None:
        tx = BufferedTransaction()
        with pytest.raises(ValueError, match="belongs to table 'other'"):
            await TrPurchased(id=RecordID("other", "x"), in_=ALICE_P, out=WIDGET).save(tx=tx)
        assert tx.statements == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("with_signals", [False, True])
    async def test_update_and_delete_bind_the_record_id(self, with_signals: bool) -> None:
        """``update(tx=)`` and ``delete(tx=)`` bind the id as ``$rid``, with or without signal handlers.

        Interpolated, an array id reads ``tr_follows:[RecordID(table_name=…), …]``: a parse error
        that fails the whole transaction (spec §2.9: the deterministic array id of an idempotent edge).
        """
        pair = RecordID("tr_follows", [ALICE_P, BOB_P])

        async def on_pre(sender: Any, instance: Any, **kwargs: Any) -> None:
            return None

        if with_signals:
            pre_update.connect(TrFollows)(on_pre)
            pre_delete.connect(TrFollows)(on_pre)
        try:
            tx = BufferedTransaction()
            edge = TrFollows(id=pair, in_=ALICE_P, out=BOB_P, weight=4)
            await edge.update(tx=tx)
            await edge.delete(tx=tx)
        finally:
            if with_signals:
                pre_update.disconnect(on_pre, TrFollows)
                pre_delete.disconnect(on_pre, TrFollows)

        assert tx.statements == ["UPDATE $t0_rid CONTENT $t0_data;", "DELETE $t1_rid;"]
        assert tx.variables == {
            "t0_rid": pair,
            "t0_data": {"in": ALICE_P, "out": BOB_P, "weight": 4},
            "t1_rid": pair,
        }


class TestEdgeTransactionsE2E:
    @pytest.mark.asyncio
    async def test_interactive_tx_returns_row_and_rolls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with orm_client(*EDGE_TABLES) as client:
            if not await _native_txn_supported():
                pytest.skip("requires SurrealDB 3.x native transactions")
            alice, bob, widget = await _nodes()
            spy = _spy_insert_relation(monkeypatch, client)

            reached = False
            with pytest.raises(RuntimeError, match="roll back"):
                async with SurrealDBConnectionManager.transaction() as tx:
                    assert tx.is_interactive
                    edge = await TrPurchased(in_=alice, out=widget, quantity=2).save(tx=tx)
                    assert isinstance(edge.id, RecordID)
                    assert edge.id.table_name == "tr_purchased"
                    seen = await TrPurchased.objects(tx=tx).filter(in_=alice).exec()
                    assert [(e.id, e.in_, e.out, e.quantity) for e in seen] == [(edge.id, ALICE_P, WIDGET, 2)]
                    reached = True
                    raise RuntimeError("roll back")
            assert reached
            assert await _stored(client, "tr_purchased") == []

            async with SurrealDBConnectionManager.transaction() as tx:
                kept = await TrFollows(id="f1", in_=alice, out=bob, weight=3).save(tx=tx)
                assert kept.id == RecordID("tr_follows", "f1")
                assert await TrFollows.objects(tx=tx).filter(out="tr_person:bob").count() == 1
            assert spy.calls == []  # under tx=, the guarded statement runs through tx.add()
            stored = await _stored(client, "tr_follows")
            assert [(row["id"], row["in"], row["out"], row["weight"]) for row in stored] == [
                (RecordID("tr_follows", "f1"), ALICE_P, BOB_P, 3)
            ]
            assert await client.query("SELECT VALUE ->tr_follows->? FROM ONLY tr_person:alice;", {}) == [BOB_P]

    @pytest.mark.asyncio
    async def test_interactive_duplicate_explicit_id_raises_in_block(self) -> None:
        async with orm_client(*EDGE_TABLES) as client:
            if not await _native_txn_supported():
                pytest.skip("requires SurrealDB 3.x native transactions")
            alice, bob, widget = await _nodes()
            await TrPurchased(id="p1", in_=alice, out=widget, quantity=2).save()

            reached = False
            with pytest.raises(SurrealDbError, match="already exists"):
                async with SurrealDBConnectionManager.transaction() as tx:
                    await TrFollows(in_=alice, out=bob).save(tx=tx)
                    await TrPurchased(id="p1", in_=alice, out=widget, quantity=5).save(tx=tx)
                    reached = True
            assert not reached  # raised by tx.add(), inside the block

            assert await _stored(client, "tr_follows") == []  # rolled back with the batch
            stored = await _stored(client, "tr_purchased")
            assert [(row["id"], row["quantity"]) for row in stored] == [(RecordID("tr_purchased", "p1"), 2)]

    @pytest.mark.asyncio
    async def test_buffered_tx_writes_at_commit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with http_orm_client(*EDGE_TABLES) as client:
            alice, bob, widget = await _nodes()
            spy = _spy_insert_relation(monkeypatch, client)

            async with SurrealDBConnectionManager.transaction() as tx:
                assert not tx.is_interactive
                auto = await TrPurchased(in_=alice, out=widget, quantity=2).save(tx=tx)
                explicit = await TrFollows(id="f1", in_=alice, out=bob, weight=3).save(tx=tx)
                assert auto.id is None  # unlike a node's buffered save(), no explicit id is needed
                assert explicit.id == "f1"
                assert await _stored(client, "tr_purchased") == []  # queued, not run
                assert await _stored(client, "tr_follows") == []

            assert spy.calls == []
            assert auto.id is None  # nothing is read back at commit
            purchased = await _stored(client, "tr_purchased")
            assert [(row["in"], row["out"], row["quantity"]) for row in purchased] == [(ALICE_P, WIDGET, 2)]
            follows = await _stored(client, "tr_follows")
            assert [(row["id"], row["in"], row["out"], row["weight"]) for row in follows] == [
                (RecordID("tr_follows", "f1"), ALICE_P, BOB_P, 3)
            ]
            # Relations, not plain records carrying `in`/`out` fields.
            assert await client.query("SELECT VALUE ->tr_purchased->? FROM ONLY tr_person:alice;", {}) == [WIDGET]
            assert await client.query("SELECT VALUE ->tr_follows->? FROM ONLY tr_person:alice;", {}) == [BOB_P]

    @pytest.mark.asyncio
    async def test_buffered_tx_rolls_back_with_failing_statement(self) -> None:
        async with http_orm_client(*EDGE_TABLES) as client:
            alice, _bob, widget = await _nodes()
            await _define_tables(client, "tr_purchased")

            with pytest.raises(SurrealDbError, match="rolled back"):
                async with SurrealDBConnectionManager.transaction() as tx:
                    await TrPurchased(in_=alice, out=widget).save(tx=tx)
                    await TrPurchased(id="p1", in_=alice, out=widget).save(tx=tx)
                    await TrPerson(id="alice", name="Again").save(tx=tx)  # alice exists: CREATE fails at commit

            assert await _stored(client, "tr_purchased") == []
            assert await client.query("SELECT VALUE ->tr_purchased->? FROM ONLY tr_person:alice;", {}) == []
            assert (await TrPerson.objects().get("alice")).name == "Alice"

    @pytest.mark.asyncio
    async def test_buffered_duplicate_explicit_id_fails_commit(self) -> None:
        async with http_orm_client(*EDGE_TABLES) as client:
            alice, bob, widget = await _nodes()
            await TrPurchased(id="p1", in_=alice, out=widget, quantity=2).save()
            await _define_tables(client, "tr_follows")

            reached = False
            with pytest.raises(SurrealDbError, match="already exists"):
                async with SurrealDBConnectionManager.transaction() as tx:
                    await TrFollows(in_=alice, out=bob).save(tx=tx)
                    await TrPurchased(id="p1", in_=alice, out=widget, quantity=5).save(tx=tx)
                    reached = True
            assert reached  # queued without error: the duplicate is found at commit

            assert await _stored(client, "tr_follows") == []  # the whole batch is rolled back
            stored = await _stored(client, "tr_purchased")
            assert [(row["id"], row["quantity"]) for row in stored] == [(RecordID("tr_purchased", "p1"), 2)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", ["ws", "http"])
    async def test_post_save_deferred_to_commit(self, transport: str) -> None:
        """``pre_save`` at call time, ``post_save`` after a successful commit only — on either strategy.

        Over WebSocket the transaction is interactive on 3.x and buffered on 2.x; over HTTP it is
        buffered on both lines.
        """
        events: list[tuple[str, Any]] = []

        @pre_save.connect(TrPurchased)
        async def on_pre(sender: Any, instance: Any, **kwargs: Any) -> None:
            events.append(("pre_save", instance.id))

        @post_save.connect(TrPurchased)
        async def on_post(sender: Any, instance: Any, created: bool, **kwargs: Any) -> None:
            events.append(("post_save", created))

        connect = orm_client if transport == "ws" else http_orm_client
        try:
            async with connect(*EDGE_TABLES) as client:
                alice, _bob, widget = await _nodes()

                async with SurrealDBConnectionManager.transaction() as tx:
                    edge = await TrPurchased(in_=alice, out=widget).save(tx=tx)
                    assert events == [("pre_save", None)]
                assert events == [("pre_save", None), ("post_save", True)]
                assert isinstance(edge.id, RecordID) is tx.is_interactive  # buffered: no row came back

                events.clear()
                with pytest.raises(RuntimeError, match="roll back"):
                    async with SurrealDBConnectionManager.transaction() as tx:
                        await TrPurchased(in_=alice, out=widget).save(tx=tx)
                        raise RuntimeError("roll back")
                assert events == [("pre_save", None)]

                await TrPurchased(id="dup", in_=alice, out=widget).save()
                events.clear()
                with pytest.raises(SurrealDbError, match="already exists"):
                    async with SurrealDBConnectionManager.transaction() as tx:
                        await TrPurchased(id="dup", in_=alice, out=widget, quantity=2).save(tx=tx)
                assert events == [("pre_save", "dup")]  # a failed write or commit never sends post_save

                assert len(await _stored(client, "tr_purchased")) == 2  # the first commit and `dup`
        finally:
            pre_save.disconnect(on_pre, TrPurchased)
            post_save.disconnect(on_post, TrPurchased)


# ==================== Task 6 — typed relate(EdgeModel, …), tx= on relate() ====================

RELATE_TABLES = (*EDGE_TABLES, "tr_srv")


class TrMemoEdge(BaseSurrealEdge):
    """Edge whose ``note`` field is stored in the column ``memo``."""

    model_config = SurrealConfigDict(table_name="tr_memo")

    note: str = Field(default="", alias="memo")


class TrLooseEdge(BaseSurrealEdge):
    """Edge that stores undeclared fields (``extra="allow"``)."""

    model_config = SurrealConfigDict(table_name="tr_loose", extra="allow")


class TrScoredEdge(BaseSurrealEdge):
    """Edge with a computed field."""

    model_config = SurrealConfigDict(table_name="tr_scored")

    quantity: int = 1
    double: Computed[int] = computed("quantity * 2")


def _queued_guarded_insert(table: str, n: int) -> str:
    """The guarded ``INSERT RELATION`` as the ``n``-th statement of a buffered transaction."""
    return (
        f"INSERT RELATION INTO {table} $t{n}_data RETURN VALUE IF $before != NONE "
        f"{{ THROW 'Database record `' + <string> $before.id + '` already exists' }} ELSE {{ $after }};"
    )


@pytest.fixture
def no_client(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the connection by one that fails the test as soon as anything asks for it."""
    calls: list[str] = []

    async def _get_client(cls: Any) -> Any:
        calls.append("get_client")
        raise AssertionError("the database was reached before the argument checks failed")

    monkeypatch.setattr(SurrealDBConnectionManager, "get_client", classmethod(_get_client))
    return calls


class TestRelateTypedValidation:
    """``relate()`` and the graph helpers check their arguments before any call (no server)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["in", "in_", "out"])
    async def test_endpoint_in_data_is_refused(self, no_client: list[str], key: str) -> None:
        alice, widget = TrPerson(id="alice"), TrProduct(id="widget")
        with pytest.raises(ValueError, match="endpoint"):
            await alice.relate(TrPurchased, widget, data={key: RecordID("tr_person", "bob")})
        assert no_client == []

    @pytest.mark.asyncio
    async def test_computed_field_in_data_is_refused(self, no_client: list[str]) -> None:
        alice, widget = TrPerson(id="alice"), TrProduct(id="widget")
        with pytest.raises(ValueError, match=r"relate\(\): double is a computed field on TrScoredEdge"):
            await alice.relate(TrScoredEdge, widget, data={"double": 4})
        assert no_client == []

    @pytest.mark.asyncio
    async def test_undeclared_key_is_refused(self, no_client: list[str]) -> None:
        alice, widget = TrPerson(id="alice"), TrProduct(id="widget")
        with pytest.raises(SurrealDbError, match="TrPurchased does not declare bogus"):
            await alice.relate(TrPurchased, widget, data={"quantity": 2, "bogus": 1})
        assert no_client == []

    @pytest.mark.asyncio
    async def test_invalid_data_raises_validation_error(self, no_client: list[str]) -> None:
        alice, widget = TrPerson(id="alice"), TrProduct(id="widget")
        with pytest.raises(ValidationError):
            await alice.relate(TrPurchased, widget, data={"quantity": 0})
        assert no_client == []

    @pytest.mark.asyncio
    async def test_non_edge_model_class_is_refused(self, no_client: list[str]) -> None:
        alice, widget = TrPerson(id="alice"), TrProduct(id="widget")
        with pytest.raises(TypeError, match="TrPerson is not an edge model: subclass BaseSurrealEdge"):
            await alice.relate(TrPerson, widget)  # type: ignore[call-overload]
        assert no_client == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "edge",
        [42, None, int, TrFollows(in_=RecordID("tr_person", "a"), out=RecordID("tr_person", "b"))],
        ids=["int", "None", "plain-class", "edge-instance"],
    )
    async def test_edge_of_another_kind_is_refused(self, no_client: list[str], edge: object) -> None:
        alice, widget = TrPerson(id="alice"), TrProduct(id="widget")
        with pytest.raises(TypeError, match="edge must be an edge model class or an edge table name"):
            await alice.relate(edge, widget)  # type: ignore[call-overload]
        assert no_client == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("edge", [TrPurchased, "tr_purchased"], ids=["typed", "string"])
    async def test_target_of_another_type_is_refused(self, no_client: list[str], edge: Any) -> None:
        alice = TrPerson(id="alice")
        with pytest.raises(TypeError, match="target must be a BaseSurrealModel instance"):
            await alice.relate(edge, 42)
        assert no_client == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("target", ["nocolon", "a;b:c", "tr_product:joe@x.com"])
    async def test_malformed_target_string_is_refused(self, no_client: list[str], target: str) -> None:
        alice = TrPerson(id="alice")
        with pytest.raises(ValueError):
            await alice.relate(TrPurchased, target)
        assert no_client == []

    @pytest.mark.asyncio
    async def test_unsaved_source_or_target_is_refused(self, no_client: list[str]) -> None:
        saved, unsaved = TrPerson(id="alice"), TrPerson()
        widget = TrProduct(id="widget")
        with pytest.raises(SurrealDbError, match="unsaved"):
            await unsaved.relate(TrPurchased, widget)
        with pytest.raises(SurrealDbError, match="unsaved"):
            await saved.relate(TrPurchased, TrProduct())
        with pytest.raises(SurrealDbError, match="unsaved"):
            await unsaved.relate("tr_purchased", widget)
        assert no_client == []

    @pytest.mark.asyncio
    async def test_graph_helpers_check_the_edge(self, no_client: list[str]) -> None:
        alice, widget = TrPerson(id="alice"), TrProduct(id="widget")
        with pytest.raises(TypeError, match="edge must be an edge model class or an edge table name, got int"):
            await alice.get_related(42)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="edge must be"):
            await alice.remove_relation(42, widget)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="edge must be"):
            await alice.remove_all_relations(None)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="TrPerson is not an edge model"):
            await alice.get_related(TrPerson)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="TrProduct is not an edge model"):
            await alice.remove_relation(TrProduct, widget)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="Invalid edge name"):
            await alice.remove_all_relations("bad-name")
        assert no_client == []

    @pytest.mark.asyncio
    async def test_data_is_keyword_only(self, no_client: list[str]) -> None:
        alice, bob = TrPerson(id="alice"), TrPerson(id="bob")
        with pytest.raises(TypeError):
            await alice.relate("tr_follows", bob, False, None, {"w": 1})  # type: ignore[call-overload]
        assert no_client == []


class TestRelateTypedStatements:
    """What ``relate()`` queues on a buffered transaction (no server: nothing runs before commit)."""

    @pytest.mark.asyncio
    async def test_typed_relate_queues_the_edge_insert(self, no_client: list[str]) -> None:
        alice, bob, widget = TrPerson(id="alice"), TrPerson(id="bob"), TrProduct(id="widget")
        tx = BufferedTransaction()

        edge = await alice.relate(TrPurchased, widget, tx=tx, data={"quantity": 2, "price": 9.5})
        follow = await alice.relate(TrFollows, bob, False, tx)  # tx by position: the full ORM's order

        assert type(edge) is TrPurchased
        assert (edge.id, edge.in_, edge.out, edge.quantity, edge.price) == (None, ALICE_P, WIDGET, 2, 9.5)
        assert type(follow) is TrFollows
        assert (follow.in_, follow.out) == (ALICE_P, BOB_P)
        assert tx.statements == [_queued_guarded_insert("tr_purchased", 0), _queued_guarded_insert("tr_follows", 1)]
        assert tx.variables == {
            "t0_data": {"in": ALICE_P, "out": WIDGET, "quantity": 2, "price": 9.5},
            "t1_data": {"in": ALICE_P, "out": BOB_P, "weight": 1},
        }
        assert no_client == []

    @pytest.mark.asyncio
    async def test_typed_relate_binds_ids_as_record_ids(self, no_client: list[str]) -> None:
        """Model ids are bound, never interpolated: no identifier check on them (``joe@x.com``, int ids)."""
        joe = TrPerson(id="joe@x.com")
        tx = BufferedTransaction()

        await joe.relate(TrPurchased, RecordID("tr_product", 7), tx=tx)
        await joe.relate(TrPurchased, "tr_product:`a-b`", tx=tx)
        await joe.relate(TrPurchased, TrIntNode(id=5), tx=tx)

        int_node = RecordID(TrIntNode.get_table_name(), 5)
        assert [(v["in"], v["out"]) for v in tx.variables.values()] == [
            (RecordID("tr_person", "joe@x.com"), RecordID("tr_product", 7)),
            (RecordID("tr_person", "joe@x.com"), RecordID("tr_product", "a-b")),
            (RecordID("tr_person", "joe@x.com"), int_node),
        ]
        assert no_client == []

    @pytest.mark.asyncio
    async def test_typed_relate_maps_aliases_and_keeps_allowed_extras(self, no_client: list[str]) -> None:
        alice, widget = TrPerson(id="alice"), TrProduct(id="widget")
        tx = BufferedTransaction()

        by_column = await alice.relate(TrMemoEdge, widget, tx=tx, data={"memo": "a"})
        by_name = await alice.relate(TrMemoEdge, widget, tx=tx, data={"note": "b"})
        loose = await alice.relate(TrLooseEdge, widget, tx=tx, data={"bogus": 1})

        assert (by_column.note, by_name.note) == ("a", "b")
        assert loose.model_extra == {"bogus": 1}
        assert tx.variables == {
            "t0_data": {"in": ALICE_P, "out": WIDGET, "memo": "a"},
            "t1_data": {"in": ALICE_P, "out": WIDGET, "memo": "b"},
            "t2_data": {"in": ALICE_P, "out": WIDGET, "bogus": 1},
        }
        assert no_client == []

    @pytest.mark.asyncio
    async def test_typed_relate_writes_a_named_server_field(self, no_client: list[str]) -> None:
        alice, widget = TrPerson(id="alice"), TrProduct(id="widget")
        tx = BufferedTransaction()

        await alice.relate(TrSrvEdge, widget, tx=tx, data={"source": "client"})
        await alice.relate(TrSrvEdge, widget, tx=tx)

        assert tx.variables == {
            "t0_data": {"in": ALICE_P, "out": WIDGET, "source": "client"},
            "t1_data": {"in": ALICE_P, "out": WIDGET},  # not named: left to the server's DEFAULT
        }
        assert no_client == []

    @pytest.mark.asyncio
    async def test_string_relate_queues_the_relate_statement(self, no_client: list[str]) -> None:
        alice, bob = TrPerson(id="alice"), TrPerson(id="bob")
        tx = BufferedTransaction()

        assert await alice.relate("tr_follows", bob, tx=tx, data={"weight": 2}) == []
        assert await alice.relate("tr_follows", RecordID("tr_person", "carol"), False, tx) == []

        assert tx.statements == [
            "RELATE $t0_src->tr_follows->$t0_dst SET weight = $t0__rd0;",
            "RELATE $t1_src->tr_follows->$t1_dst;",
        ]
        assert tx.variables == {
            "t0_src": ALICE_P,
            "t0_dst": BOB_P,
            "t0__rd0": 2,
            "t1_src": ALICE_P,
            "t1_dst": RecordID("tr_person", "carol"),
        }
        assert no_client == []

    @pytest.mark.asyncio
    async def test_string_relate_binds_the_record_ids(self, no_client: list[str]) -> None:
        """Only the validated edge name is written into the statement: the record ids are bound.

        A ``RecordID`` is bound as is, whatever its table name holds, and a node id that is not an
        identifier (``joe@x.com``) relates like it saves, as with the typed form.
        """
        joe = TrPerson(id="joe@x.com")
        odd = RecordID("tr_person:x; DELETE tr_person; --", 1)
        tx = BufferedTransaction()

        await joe.relate("tr_follows", odd, tx=tx)
        await joe.relate("tr_follows", odd, reverse=True, tx=tx)
        await TrAnyIdNode(id=odd).relate("tr_follows", joe, tx=tx)

        joe_id = RecordID("tr_person", "joe@x.com")
        assert tx.statements == [
            "RELATE $t0_src->tr_follows->$t0_dst;",
            "RELATE $t1_dst->tr_follows->$t1_src;",
            "RELATE $t2_src->tr_follows->$t2_dst;",
        ]
        assert tx.variables == {
            "t0_src": joe_id,
            "t0_dst": odd,
            "t1_src": joe_id,
            "t1_dst": odd,
            "t2_src": odd,
            "t2_dst": joe_id,
        }
        assert no_client == []


class TestRelateTypedE2E:
    @pytest.mark.asyncio
    async def test_relate_returns_typed_edge(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with orm_client(*RELATE_TABLES) as client:
            alice, _bob, widget = await _nodes()
            spy = _spy_insert_relation(monkeypatch, client)

            edge = await alice.relate(TrPurchased, widget, data={"quantity": 2, "price": 9.5})

            assert type(edge) is TrPurchased
            assert isinstance(edge.id, RecordID)
            assert edge.id.table_name == "tr_purchased"
            assert (edge.in_, edge.out, edge.quantity, edge.price) == (ALICE_P, WIDGET, 2, 9.5)
            assert spy.calls == [("tr_purchased", {"in": ALICE_P, "out": WIDGET, "quantity": 2, "price": 9.5})]
            # A relation graph traversal sees, not a plain record carrying `in`/`out`.
            assert await client.query("SELECT VALUE ->tr_purchased->? FROM ONLY tr_person:alice;", {}) == [WIDGET]
            loaded = await TrPurchased.objects().get(edge.get_raw_id())
            assert (loaded.id, loaded.in_, loaded.out, loaded.quantity, loaded.price) == (
                edge.id,
                ALICE_P,
                WIDGET,
                2,
                9.5,
            )

    @pytest.mark.asyncio
    async def test_relate_fires_the_save_signals(self) -> None:
        events: list[tuple[str, Any, bool | None]] = []

        @pre_save.connect(TrPurchased)
        async def on_pre(sender: Any, instance: Any, **kwargs: Any) -> None:
            events.append(("pre_save", instance.id, None))

        @post_save.connect(TrPurchased)
        async def on_post(sender: Any, instance: Any, created: bool, **kwargs: Any) -> None:
            events.append(("post_save", instance.id, created))

        try:
            async with orm_client(*RELATE_TABLES):
                alice, _bob, widget = await _nodes()
                edge = await alice.relate(TrPurchased, widget)
                assert events == [("pre_save", None, None), ("post_save", edge.id, True)]

                events.clear()
                await alice.relate("tr_purchased", widget)
                assert events == []  # the string form fires none (unchanged)
        finally:
            pre_save.disconnect(on_pre, TrPurchased)
            post_save.disconnect(on_post, TrPurchased)

    @pytest.mark.asyncio
    async def test_relate_targets_string_and_record_id(self) -> None:
        async with orm_client(*RELATE_TABLES):
            alice, _bob, widget = await _nodes()

            by_string = await alice.relate(TrPurchased, "tr_product:widget")
            by_record_id = await alice.relate(TrPurchased, RecordID("tr_product", "widget"), data={"quantity": 3})

            assert by_string.out == by_record_id.out == WIDGET
            assert await TrPurchased.objects().filter(out=widget).count() == 2
            related = await alice.get_related("tr_purchased", model_class=TrProduct)
            assert [p.get_raw_id() for p in related] == ["widget", "widget"]

    @pytest.mark.asyncio
    async def test_relate_node_with_non_identifier_id(self) -> None:
        async with orm_client(*RELATE_TABLES):
            _alice, _bob, widget = await _nodes()
            joe = await TrPerson(id="joe@x.com", name="Joe").save()

            edge = await joe.relate(TrPurchased, widget)

            assert edge.in_ == RecordID("tr_person", "joe@x.com")
            assert await TrPurchased.objects().filter(in_=joe).count() == 1

    @pytest.mark.asyncio
    async def test_relate_writes_named_server_field(self) -> None:
        async with orm_client(*RELATE_TABLES) as client:
            alice, _bob, widget = await _nodes()
            await client.query('DEFINE TABLE tr_srv TYPE RELATION; DEFINE FIELD source ON tr_srv DEFAULT "server";', {})

            named = await alice.relate(TrSrvEdge, widget, data={"source": "client"})
            unnamed = await alice.relate(TrSrvEdge, widget)

            assert named.source == "client"  # named in data: written, not left to the DEFAULT
            assert unnamed.source == "server"  # hydrated from the row the server returned
            assert sorted(row["source"] for row in await _stored(client, "tr_srv")) == ["client", "server"]

    @pytest.mark.asyncio
    async def test_invalid_data_writes_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with orm_client(*RELATE_TABLES) as client:
            alice, _bob, widget = await _nodes()
            spy = _spy_insert_relation(monkeypatch, client)

            with pytest.raises(ValidationError):
                await alice.relate(TrPurchased, widget, data={"quantity": 0})
            with pytest.raises(SurrealDbError, match="does not declare"):
                await alice.relate(TrPurchased, widget, data={"bogus": 1})

            assert spy.calls == []
            assert await _stored(client, "tr_purchased") == []

    @pytest.mark.asyncio
    async def test_relate_with_tx_interactive(self) -> None:
        async with orm_client(*RELATE_TABLES) as client:
            if not await _native_txn_supported():
                pytest.skip("requires SurrealDB 3.x native transactions")
            alice, bob, widget = await _nodes()

            reached = False
            with pytest.raises(RuntimeError, match="roll back"):
                async with SurrealDBConnectionManager.transaction() as tx:
                    assert tx.is_interactive
                    edge = await alice.relate(TrPurchased, widget, tx=tx, data={"quantity": 2})
                    assert isinstance(edge.id, RecordID)
                    assert edge.id.table_name == "tr_purchased"
                    seen = await TrPurchased.objects(tx=tx).filter(in_=alice).exec()
                    assert [(e.id, e.quantity) for e in seen] == [(edge.id, 2)]
                    reached = True
                    raise RuntimeError("roll back")
            assert reached
            assert await _stored(client, "tr_purchased") == []

            async with SurrealDBConnectionManager.transaction() as tx:
                kept = await alice.relate(TrFollows, bob, False, tx, data={"weight": 3})
                assert isinstance(kept.id, RecordID)
            stored = await _stored(client, "tr_follows")
            assert [(row["id"], row["in"], row["out"], row["weight"]) for row in stored] == [(kept.id, ALICE_P, BOB_P, 3)]

    @pytest.mark.asyncio
    async def test_relate_with_tx_buffered(self) -> None:
        async with http_orm_client(*RELATE_TABLES) as client:
            alice, _bob, widget = await _nodes()

            async with SurrealDBConnectionManager.transaction() as tx:
                assert not tx.is_interactive
                edge = await alice.relate(TrPurchased, widget, tx=tx, data={"quantity": 2})
                assert edge.id is None  # queued: no row before commit
                assert (edge.in_, edge.out, edge.quantity) == (ALICE_P, WIDGET, 2)
                assert await _stored(client, "tr_purchased") == []

            stored = await _stored(client, "tr_purchased")
            assert [(row["in"], row["out"], row["quantity"]) for row in stored] == [(ALICE_P, WIDGET, 2)]
            assert await client.query("SELECT VALUE ->tr_purchased->? FROM ONLY tr_person:alice;", {}) == [WIDGET]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", ["ws", "http"])
    async def test_string_relate_accepts_tx(self, transport: str) -> None:
        """Over WebSocket the transaction is interactive on 3.x and buffered on 2.x; over HTTP buffered on both."""
        connect = orm_client if transport == "ws" else http_orm_client
        async with connect(*RELATE_TABLES) as client:
            alice, bob, _widget = await _nodes()

            async with SurrealDBConnectionManager.transaction() as tx:
                rows = await alice.relate("tr_follows", bob, tx=tx, data={"weight": 2})
                if tx.is_interactive:
                    assert [(row["in"], row["out"], row["weight"]) for row in rows] == [(ALICE_P, BOB_P, 2)]
                else:
                    assert rows == []
                    assert await _stored(client, "tr_follows") == []

            stored = await _stored(client, "tr_follows")
            assert [(row["in"], row["out"], row["weight"]) for row in stored] == [(ALICE_P, BOB_P, 2)]

            with pytest.raises(RuntimeError, match="roll back"):
                async with SurrealDBConnectionManager.transaction() as tx:
                    await alice.relate("tr_follows", bob, False, tx)
                    raise RuntimeError("roll back")
            assert len(await _stored(client, "tr_follows")) == 1

    @pytest.mark.asyncio
    async def test_graph_helpers_accept_edge_class(self) -> None:
        async with orm_client(*RELATE_TABLES):
            alice, bob, widget = await _nodes()
            await alice.relate(TrPurchased, widget)
            await alice.relate(TrFollows, bob)

            related = await alice.get_related(TrPurchased, model_class=TrProduct)
            assert [(type(p), p.get_raw_id()) for p in related] == [(TrProduct, "widget")]
            assert await bob.get_related(TrFollows, direction="in") == [ALICE_P]

            await alice.remove_relation(TrPurchased, widget)
            assert await alice.get_related(TrPurchased) == []

            await alice.remove_all_relations(TrFollows, direction="out")
            assert await TrFollows.objects().count() == 0

            with pytest.raises(TypeError, match="TrPerson is not an edge model"):
                await alice.get_related(TrPerson)  # type: ignore[arg-type]


# ==================== Task 6b — graph API parity with the full ORM ====================


class TrLikes(BaseSurrealEdge):
    """A second edge table, for edge lists (``remove_all_relations([...])``)."""

    model_config = SurrealConfigDict(table_name="tr_likes")


class TrGhost(BaseSurrealModel):
    """A node whose table is never created: the source of a removal from a record never saved."""

    model_config = SurrealConfigDict(table_name="tr_ghost")

    id: str | None = None


class TrAnyIdNode(BaseSurrealModel):
    """A node whose id may be a ``RecordID`` (as on an edge model): it is bound as is."""

    model_config = SurrealConfigDict(table_name="tr_any_node")

    id: str | RecordID | None = None


GRAPH_TABLES = (*EDGE_TABLES, "tr_likes", "tr_ghost")
BLANK_TARGET = (
    "remove_relation() target must not be empty or blank: pass a model instance, a RecordID, "
    "a 'table:id' string or a bare record id."
)


class _RecordingClient:
    """Stands in for the connected SDK client: records every call and runs nothing.

    ``query()`` returns ``[]``; ``insert_relation()`` returns one row per row sent, so the
    count check of ``insert_edges()`` (refused writes) passes.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def query(self, q: str, v: dict[str, Any] | None = None) -> Any:
        self.calls.append((q, v))
        return []

    async def insert_relation(self, table: str, data: Any) -> Any:
        self.calls.append(("insert_relation", table, data))
        rows = data if isinstance(data, list) else [data]
        return [{**r, "id": RecordID(table, f"stub{i}")} for i, r in enumerate(rows)]


@pytest.fixture
def recording(monkeypatch: pytest.MonkeyPatch) -> _RecordingClient:
    """Install a :class:`_RecordingClient` as the connected client."""
    stub = _RecordingClient()

    async def _get_client(cls: Any) -> Any:
        return stub

    monkeypatch.setattr(SurrealDBConnectionManager, "get_client", classmethod(_get_client))
    return stub


async def _recorded(stub: _RecordingClient, call: Awaitable[Any]) -> tuple[list[tuple[Any, ...]], Any]:
    """Run ``call`` on a cleared recording; return what it recorded and what it returned."""
    stub.calls.clear()
    result = await call
    return list(stub.calls), result


class TestGraphParityUnit:
    """The exact statements of the graph helpers, recorded by a client stub (no server)."""

    @pytest.mark.asyncio
    async def test_remove_relation_statement(self, recording: _RecordingClient) -> None:
        alice, bob = TrPerson(id="alice"), TrPerson(id="bob")

        calls, result = await _recorded(recording, alice.remove_relation("tr_follows", bob))
        assert result is None
        assert calls == [("DELETE $src->tr_follows WHERE out = $dst;", {"src": ALICE_P, "dst": BOB_P})]

        calls, _ = await _recorded(recording, alice.remove_relation("tr_follows", bob, reverse=True))
        assert calls == [("DELETE $src<-tr_follows WHERE in = $dst;", {"src": ALICE_P, "dst": BOB_P})]

        calls, _ = await _recorded(recording, alice.remove_relation(TrFollows, "tr_person:bob"))
        assert calls == [("DELETE $src->tr_follows WHERE out = $dst;", {"src": ALICE_P, "dst": BOB_P})]

        target = RecordID("tr_product", 7)
        calls, _ = await _recorded(recording, alice.remove_relation("tr_follows", target))
        assert calls == [("DELETE $src->tr_follows WHERE out = $dst;", {"src": ALICE_P, "dst": target})]
        assert calls[0][1]["dst"] is target  # bound as is: an integer id stays reachable

    @pytest.mark.asyncio
    async def test_remove_relation_bare_id_statement(self, recording: _RecordingClient) -> None:
        alice = TrPerson(id="alice")

        forward, _ = await _recorded(recording, alice.remove_relation("tr_follows", "bob"))
        assert forward == [("DELETE $src->tr_follows WHERE record::id(out) = $id;", {"src": ALICE_P, "id": "bob"})]

        backward, _ = await _recorded(recording, alice.remove_relation("tr_follows", "bob", reverse=True))
        assert backward == [("DELETE $src<-tr_follows WHERE record::id(in) = $id;", {"src": ALICE_P, "id": "bob"})]

        by_class, _ = await _recorded(recording, alice.remove_relation(TrFollows, "bob"))
        assert by_class == forward
        by_class_reversed, _ = await _recorded(recording, alice.remove_relation(TrFollows, "bob", reverse=True))
        assert by_class_reversed == backward

        # Bound verbatim: no identifier rule, no strip, no unquoting.
        for raw in ["joe@x.com", " bob", "`bob`", "7"]:
            calls, _ = await _recorded(recording, alice.remove_relation("tr_follows", raw))
            assert calls == [("DELETE $src->tr_follows WHERE record::id(out) = $id;", {"src": ALICE_P, "id": raw})]

    @pytest.mark.asyncio
    async def test_remove_all_relations_statements(self, recording: _RecordingClient) -> None:
        alice = TrPerson(id="alice")
        edges = ["tr_follows", TrLikes, "tr_follows"]  # de-duplicated, in order

        expected = {
            "out": "DELETE $src->(tr_follows, tr_likes);",
            "in": "DELETE $src<-(tr_follows, tr_likes);",
            "both": "DELETE $src->(tr_follows, tr_likes), $src<-(tr_follows, tr_likes);",
        }
        for direction, statement in expected.items():
            calls, result = await _recorded(recording, alice.remove_all_relations(edges, direction=direction))
            assert result is None
            assert calls == [(statement, {"src": ALICE_P})]

        calls, _ = await _recorded(recording, alice.remove_all_relations(("tr_follows", TrLikes)))
        assert calls == [("DELETE $src->(tr_follows, tr_likes);", {"src": ALICE_P})]
        calls, _ = await _recorded(recording, alice.remove_all_relations("tr_follows"))
        assert calls == [("DELETE $src->(tr_follows);", {"src": ALICE_P})]
        calls, _ = await _recorded(recording, alice.remove_all_relations(TrLikes, "in"))
        assert calls == [("DELETE $src<-(tr_likes);", {"src": ALICE_P})]

    @pytest.mark.asyncio
    async def test_get_related_statements(self, recording: _RecordingClient) -> None:
        alice = TrPerson(id="alice")
        expected = {
            "out": ("SELECT VALUE ->tr_follows->? FROM ONLY $src;", "SELECT VALUE ->tr_follows->?.* FROM ONLY $src;"),
            "in": ("SELECT VALUE <-tr_follows<-? FROM ONLY $src;", "SELECT VALUE <-tr_follows<-?.* FROM ONLY $src;"),
            "both": (
                "SELECT VALUE array::concat(->tr_follows->?, <-tr_follows<-?) FROM ONLY $src;",
                "SELECT VALUE array::concat(->tr_follows->?.*, <-tr_follows<-?.*) FROM ONLY $src;",
            ),
        }
        for direction, (ids_statement, records_statement) in expected.items():
            calls, result = await _recorded(recording, alice.get_related("tr_follows", direction=direction))
            assert (calls, result) == ([(ids_statement, {"src": ALICE_P})], [])
            calls, _ = await _recorded(recording, alice.get_related(TrFollows, direction=direction, model_class=TrPerson))
            assert calls == [(records_statement, {"src": ALICE_P})]

    @pytest.mark.asyncio
    async def test_string_relate_and_traverse_bind_the_record_ids(self, recording: _RecordingClient) -> None:
        """Only the validated edge name or path is written into the statement; the ids are bound.

        A ``"table:id"`` string keeps the rule of a model's own id (``"tr_person:1"`` is the string
        id ``"1"``, as ``TrPerson(id="1")``), a ``RecordID`` or an ``id: int`` node an integer id,
        and a ``RecordID`` is bound as is, whatever its table name holds — as a target, or as the
        id of the source.
        """
        alice, joe = TrPerson(id="alice"), TrPerson(id="joe@x.com")
        odd = RecordID("tr_person:x; DELETE tr_person; --", 1)
        joe_id = RecordID("tr_person", "joe@x.com")

        targets: list[tuple[Any, RecordID]] = [
            ("tr_person:1", RecordID("tr_person", "1")),
            ("tr_product:`a-b`", RecordID("tr_product", "a-b")),
            (RecordID("tr_product", 7), RecordID("tr_product", 7)),
            (TrIntNode(id=5), RecordID("tr_int_node", 5)),
            (joe, joe_id),
            (odd, odd),
        ]
        for target, bound in targets:
            calls, _ = await _recorded(recording, alice.relate("tr_follows", target))
            assert calls == [("RELATE $src->tr_follows->$dst;", {"src": ALICE_P, "dst": bound})]

        calls, _ = await _recorded(recording, joe.relate("tr_follows", odd, reverse=True, weight=2))
        assert calls == [("RELATE $dst->tr_follows->$src SET weight = $_rd0;", {"src": joe_id, "dst": odd, "_rd0": 2})]

        source = TrAnyIdNode(id=odd)
        calls, _ = await _recorded(recording, source.relate("tr_follows", alice))
        assert calls == [("RELATE $src->tr_follows->$dst;", {"src": odd, "dst": ALICE_P})]

        for node, bound in [(source, odd), (joe, joe_id), (TrIntNode(id=5), RecordID("tr_int_node", 5))]:
            calls, result = await _recorded(recording, node.traverse("->tr_follows->tr_person"))
            assert (calls, result) == ([("SELECT VALUE ->tr_follows->tr_person FROM ONLY $src;", {"src": bound})], [])

    @pytest.mark.asyncio
    async def test_positional_calls_match_keyword_forms(self, recording: _RecordingClient) -> None:
        """R2: each positional call records exactly what its keyword form records."""
        alice, bob = TrPerson(id="alice"), TrPerson(id="bob")
        pairs: list[tuple[Callable[[], Awaitable[Any]], Callable[[], Awaitable[Any]], list[tuple[Any, ...]]]] = [
            (
                lambda: alice.relate("tr_follows", bob, True),
                lambda: alice.relate("tr_follows", bob, reverse=True),
                [("RELATE $dst->tr_follows->$src;", {"src": ALICE_P, "dst": BOB_P})],
            ),
            (
                lambda: alice.relate(TrFollows, bob, True, None, weight=3),
                lambda: alice.relate(TrFollows, bob, reverse=True, tx=None, weight=3),
                [("insert_relation", "tr_follows", {"in": BOB_P, "out": ALICE_P, "weight": 3})],
            ),
            (
                lambda: alice.remove_relation("tr_follows", bob, True),
                lambda: alice.remove_relation("tr_follows", bob, reverse=True),
                [("DELETE $src<-tr_follows WHERE in = $dst;", {"src": ALICE_P, "dst": BOB_P})],
            ),
            (
                lambda: alice.remove_relation("tr_follows", "bob", True, None),
                lambda: alice.remove_relation("tr_follows", "bob", reverse=True, tx=None),
                [("DELETE $src<-tr_follows WHERE record::id(in) = $id;", {"src": ALICE_P, "id": "bob"})],
            ),
            (
                lambda: alice.remove_all_relations(["tr_follows"], "both"),
                lambda: alice.remove_all_relations(["tr_follows"], direction="both"),
                [("DELETE $src->(tr_follows), $src<-(tr_follows);", {"src": ALICE_P})],
            ),
            (
                lambda: alice.remove_all_relations(["tr_follows"], "in", None),
                lambda: alice.remove_all_relations(["tr_follows"], direction="in", tx=None),
                [("DELETE $src<-(tr_follows);", {"src": ALICE_P})],
            ),
            (
                lambda: alice.get_related("tr_follows", "in", TrPerson),
                lambda: alice.get_related("tr_follows", direction="in", model_class=TrPerson),
                [("SELECT VALUE <-tr_follows<-?.* FROM ONLY $src;", {"src": ALICE_P})],
            ),
        ]
        for positional, keyword, expected in pairs:
            by_position, positional_result = await _recorded(recording, positional())
            by_keyword, keyword_result = await _recorded(recording, keyword())
            assert by_position == by_keyword == expected
            assert type(positional_result) is type(keyword_result)

        edge = await alice.relate(TrFollows, bob, True, None, weight=3)
        assert type(edge) is TrFollows
        assert (edge.in_, edge.out, edge.weight) == (BOB_P, ALICE_P, 3)
        assert edge.id == RecordID("tr_follows", "stub0")

        recording.calls.clear()
        with pytest.raises(TypeError):
            await alice.relate("tr_follows", bob, False, None, {"w": 1})  # type: ignore[call-overload]
        assert recording.calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("call", "error", "match"),
        [
            (lambda a: a.remove_all_relations([]), ValueError, "needs at least one edge"),
            (lambda a: a.remove_all_relations(()), ValueError, "needs at least one edge"),
            (lambda a: a.remove_all_relations(["tr_follows", "bad-name"]), ValueError, "Invalid edge name"),
            (lambda a: a.remove_all_relations(["tr_follows", TrPerson]), TypeError, "TrPerson is not an edge model"),
            (lambda a: a.remove_all_relations([42]), TypeError, "edge must be an edge model class"),
            (
                lambda a: a.remove_all_relations("tr_follows", direction="sideways"),
                ValueError,
                "direction must be 'out', 'in', or 'both', got 'sideways'",
            ),
            (lambda a: a.get_related("tr_follows", direction="sideways"), ValueError, "direction must be"),
            (lambda a: a.get_related("tr_follows", "both ways"), ValueError, "direction must be"),
            (lambda a: a.remove_relation("tr_follows", 42), TypeError, "target must be"),
            (lambda a: a.remove_relation("tr_follows", ""), ValueError, re.escape(BLANK_TARGET)),
            (lambda a: a.remove_relation("tr_follows", "   "), ValueError, re.escape(BLANK_TARGET)),
            (lambda a: a.remove_relation(TrFollows, "\t", reverse=True), ValueError, "empty or blank"),
            (lambda a: a.remove_relation("tr_follows", "a;b:c"), ValueError, "record identifier"),
            (lambda a: a.remove_relation("bad-name", "bob"), ValueError, "Invalid edge name"),
            (lambda a: a.remove_relation("tr_follows", TrPerson()), SurrealDbError, "unsaved"),
            (lambda a: a.relate("tr_follows", "nocolon"), ValueError, "record identifier"),
            (lambda a: a.relate(TrFollows, "nocolon"), ValueError, "record identifier"),
            (lambda a: TrPerson().get_related("tr_follows"), SurrealDbError, "unsaved"),
            (lambda a: TrPerson().remove_relation("tr_follows", "bob"), SurrealDbError, "unsaved"),
            (lambda a: TrPerson().remove_relation("tr_follows", a), SurrealDbError, "unsaved"),
            (lambda a: TrPerson().remove_all_relations(["tr_follows"]), SurrealDbError, "unsaved"),
            (lambda a: TrPerson().relate("tr_follows", a, True), SurrealDbError, "unsaved"),
            (lambda a: a.relate("tr_follows", TrPerson()), SurrealDbError, "unsaved"),
            (lambda a: a.relate("tr_follows", 42), TypeError, "target must be"),
            (lambda a: TrPerson().traverse("->tr_follows->tr_person"), SurrealDbError, "unsaved"),
            (lambda a: a.traverse("->tr_follows; DELETE tr_person"), ValueError, "Invalid graph path"),
        ],
    )
    async def test_graph_helpers_validate_before_any_call(
        self,
        recording: _RecordingClient,
        call: Callable[[TrPerson], Awaitable[Any]],
        error: type[Exception],
        match: str,
    ) -> None:
        with pytest.raises(error, match=match):
            await call(TrPerson(id="alice"))
        assert recording.calls == []

    @pytest.mark.asyncio
    async def test_remove_helpers_queue_in_buffered_tx(self, recording: _RecordingClient) -> None:
        alice, bob = TrPerson(id="alice"), TrPerson(id="bob")
        tx = BufferedTransaction(client=None)

        assert await alice.remove_relation("tr_follows", bob, tx=tx) is None
        assert await alice.remove_all_relations(["tr_follows", "tr_never"], direction="both", tx=tx) is None
        assert await alice.remove_relation(TrFollows, "bob", reverse=True, tx=tx) is None
        await alice.remove_relation("tr_follows", "carol", False, tx)  # bare id, positional tx

        assert tx.statements == [
            "DELETE $t0_src->tr_follows WHERE out = $t0_dst;",
            "DELETE $t1_src->(tr_follows, tr_never), $t1_src<-(tr_follows, tr_never);",
            "DELETE $t2_src<-tr_follows WHERE record::id(in) = $t2_id;",
            "DELETE $t3_src->tr_follows WHERE record::id(out) = $t3_id;",
        ]
        assert tx.variables == {
            "t0_src": ALICE_P,
            "t0_dst": BOB_P,
            "t1_src": ALICE_P,
            "t2_src": ALICE_P,
            "t2_id": "bob",
            "t3_src": ALICE_P,
            "t3_id": "carol",
        }
        assert recording.calls == []

    @pytest.mark.asyncio
    async def test_relate_reverse_queues_in_buffered_tx(self, recording: _RecordingClient) -> None:
        alice, bob = TrPerson(id="alice"), TrPerson(id="bob")
        tx = BufferedTransaction(client=None)

        edge = await alice.relate(TrFollows, bob, True, tx, weight=2)
        rows = await alice.relate("tr_follows", bob, True, tx, since="2025-01-01")

        assert (edge.id, edge.in_, edge.out, edge.weight) == (None, BOB_P, ALICE_P, 2)
        assert rows == []
        assert tx.statements == [
            _queued_guarded_insert("tr_follows", 0),
            "RELATE $t1_dst->tr_follows->$t1_src SET since = $t1__rd0;",
        ]
        assert tx.variables == {
            "t0_data": {"in": BOB_P, "out": ALICE_P, "weight": 2},
            "t1_src": ALICE_P,
            "t1_dst": BOB_P,
            "t1__rd0": "2025-01-01",
        }
        assert recording.calls == []

    @pytest.mark.asyncio
    async def test_relate_edge_fields_merge_and_conflict(self, recording: _RecordingClient) -> None:
        alice, bob, widget = TrPerson(id="alice"), TrPerson(id="bob"), TrProduct(id="widget")

        refused: list[tuple[Callable[[], Awaitable[Any]], type[Exception], str]] = [
            (lambda: alice.relate("tr_follows", bob, data={"w": 1}, w=2), TypeError, "both in data= and as keyword"),
            (
                lambda: alice.relate(TrPurchased, widget, data={"quantity": 1}, quantity=2),
                TypeError,
                "both in data= and as keyword",
            ),
            (lambda: alice.relate(TrMemoEdge, widget, data={"memo": "a"}, note="b"), TypeError, "both in data="),
            (lambda: alice.relate(TrMemoEdge, widget, data={"note": "a"}, memo="b"), TypeError, "both in data="),
            (lambda: alice.relate(TrPurchased, widget, in_=bob), ValueError, "endpoints"),
            (lambda: alice.relate(TrPurchased, widget, out=bob), ValueError, "endpoints"),
            (lambda: alice.relate(TrPurchased, widget, bogus=1), SurrealDbError, "does not declare bogus"),
            (lambda: alice.relate(TrScoredEdge, widget, double=4), ValueError, "computed field"),
            (lambda: alice.relate(TrPurchased, widget, quantity=0), ValidationError, "greater than or equal to 1"),
            (lambda: alice.relate("tr_follows", bob, **{"bad-name": 1}), ValueError, "relation data field name"),
        ]
        for call, error, match in refused:
            recording.calls.clear()
            with pytest.raises(error, match=match):
                await call()
            assert recording.calls == []

        calls, rows = await _recorded(recording, alice.relate("tr_follows", bob, since="2025-01-01"))
        assert rows == []
        pair = {"src": ALICE_P, "dst": BOB_P}
        assert calls == [("RELATE $src->tr_follows->$dst SET since = $_rd0;", {**pair, "_rd0": "2025-01-01"})]

        calls, _ = await _recorded(recording, alice.relate("tr_follows", bob, reverse=True, since="2025-01-01"))
        assert calls == [("RELATE $dst->tr_follows->$src SET since = $_rd0;", {**pair, "_rd0": "2025-01-01"})]

        calls, _ = await _recorded(recording, alice.relate("tr_follows", bob, since="2025-01-01", data={"weight": 2}))
        assert calls == [
            (
                "RELATE $src->tr_follows->$dst SET weight = $_rd0, since = $_rd1;",
                {**pair, "_rd0": 2, "_rd1": "2025-01-01"},
            )
        ]

        calls, _ = await _recorded(recording, alice.relate("tr_follows", bob, data={"reverse": True}))
        assert calls == [("RELATE $src->tr_follows->$dst SET reverse = $_rd0;", {**pair, "_rd0": True})]

        calls, edge = await _recorded(recording, alice.relate(TrPurchased, widget, quantity=2, price=9.5))
        assert calls == [("insert_relation", "tr_purchased", {"in": ALICE_P, "out": WIDGET, "quantity": 2, "price": 9.5})]
        assert (type(edge), edge.quantity, edge.price) == (TrPurchased, 2, 9.5)

        calls, memo = await _recorded(recording, alice.relate(TrMemoEdge, widget, memo="m"))
        assert calls == [("insert_relation", "tr_memo", {"in": ALICE_P, "out": WIDGET, "memo": "m"})]
        assert memo.note == "m"


def _people_pairs(rows: list[Any]) -> list[tuple[str, str]]:
    return sorted((str(row["in"]), str(row["out"])) for row in rows)


async def _edge_ids(client: Any) -> list[str]:
    rows = [*await _stored(client, "tr_follows"), *await _stored(client, "tr_likes")]
    return sorted(str(row["id"]) for row in rows)


class TestGraphParityE2E:
    @pytest.mark.asyncio
    async def test_relate_reverse_string_and_typed(self) -> None:
        async with orm_client(*GRAPH_TABLES) as client:
            alice, bob, _widget = await _nodes()

            rows = await alice.relate("tr_follows", bob, reverse=True)
            assert [(row["in"], row["out"]) for row in rows] == [(BOB_P, ALICE_P)]
            assert await alice.get_related("tr_follows", direction="in") == [BOB_P]
            assert await alice.get_related("tr_follows", direction="out") == []
            assert await bob.get_related("tr_follows") == [ALICE_P]

            edge = await alice.relate(TrFollows, bob, reverse=True, weight=3)
            assert type(edge) is TrFollows
            assert isinstance(edge.id, RecordID)
            assert (edge.in_, edge.out, edge.weight) == (BOB_P, ALICE_P, 3)

            positional = await alice.relate(TrFollows, bob, True)
            assert (positional.in_, positional.out) == (BOB_P, ALICE_P)
            assert await alice.get_related("tr_follows", "out") == []
            assert await client.query("SELECT VALUE ->tr_follows->? FROM ONLY tr_person:bob;", {}) == [ALICE_P] * 3

    @pytest.mark.asyncio
    async def test_remove_relation_reverse(self) -> None:
        async with orm_client(*GRAPH_TABLES) as client:
            alice, bob, _widget = await _nodes()

            for edge in ["tr_follows", TrFollows]:
                await bob.relate("tr_follows", alice)
                await alice.remove_relation(edge, bob)  # alice -> bob: none, so nothing to remove
                assert _people_pairs(await _stored(client, "tr_follows")) == [("tr_person:bob", "tr_person:alice")]
                await alice.remove_relation(edge, bob, reverse=True)
                assert await _stored(client, "tr_follows") == []

            await bob.relate("tr_follows", alice)
            await alice.remove_relation("tr_follows", bob, True)  # positional reverse
            assert await _stored(client, "tr_follows") == []

    @pytest.mark.asyncio
    async def test_relate_keyword_edge_fields(self) -> None:
        async with orm_client(*GRAPH_TABLES) as client:
            alice, bob, widget = await _nodes()

            rows = await alice.relate("tr_follows", bob, since="2025-01-01", data={"weight": 2})
            assert [(row["since"], row["weight"]) for row in rows] == [("2025-01-01", 2)]

            edge = await alice.relate(TrPurchased, widget, quantity=2, price=9.5)
            assert (type(edge), edge.quantity, edge.price) == (TrPurchased, 2, 9.5)
            assert [(row["quantity"], row["price"]) for row in await _stored(client, "tr_purchased")] == [(2, 9.5)]

            await alice.relate("tr_likes", widget, data={"reverse": True})
            assert [(row["in"], row["out"], row["reverse"]) for row in await _stored(client, "tr_likes")] == [
                (ALICE_P, WIDGET, True)
            ]

            with pytest.raises(TypeError, match="both in data= and as keyword"):
                await alice.relate("tr_follows", bob, data={"weight": 1}, weight=2)
            with pytest.raises(TypeError, match="both in data= and as keyword"):
                await alice.relate(TrPurchased, widget, data={"quantity": 1}, quantity=2)
            assert len(await _stored(client, "tr_follows")) == 1
            assert len(await _stored(client, "tr_purchased")) == 1

    @pytest.mark.asyncio
    async def test_remove_all_relations_list_and_directions(self) -> None:
        async with orm_client(*GRAPH_TABLES) as client:
            alice, bob, widget = await _nodes()
            carol = await TrPerson(id="carol", name="Carol").save()

            async def fixture() -> None:
                for table in ("tr_follows", "tr_likes"):
                    with contextlib.suppress(NotFoundError):  # 3.x: not created yet on the first call
                        await client.query(f"DELETE {table};", {})
                await TrFollows(id="ab", in_=alice, out=bob).save()
                await TrFollows(id="ca", in_=carol, out=alice).save()
                await TrFollows(id="aa", in_=alice, out=alice).save()
                await TrLikes(id="aw", in_=alice, out=widget).save()
                await TrFollows(id="bc", in_=bob, out=carol).save()

            edges = ["tr_follows", TrLikes, "tr_never"]
            expected = {
                "out": ["tr_follows:bc", "tr_follows:ca"],
                "in": ["tr_follows:ab", "tr_follows:bc", "tr_likes:aw"],
                "both": ["tr_follows:bc"],
            }
            for direction, remaining in expected.items():
                await fixture()
                assert len(await _edge_ids(client)) == 5
                await alice.remove_all_relations(edges, direction=direction)
                assert await _edge_ids(client) == remaining, direction

            await fixture()
            await alice.remove_all_relations(edges, "both")  # positional direction
            assert await _edge_ids(client) == ["tr_follows:bc"]

            await fixture()
            with pytest.raises(TypeError, match="not an edge model"):
                await alice.remove_all_relations(["tr_follows", TrPerson], direction="both")  # type: ignore[list-item]
            with pytest.raises(ValueError, match="at least one edge"):
                await alice.remove_all_relations([], direction="both")
            assert len(await _edge_ids(client)) == 5  # nothing deleted

    @pytest.mark.asyncio
    async def test_get_related_both_is_out_then_in_neighbours(self) -> None:
        async with orm_client(*GRAPH_TABLES):
            alice, bob, _widget = await _nodes()
            carol = await TrPerson(id="carol", name="Carol").save()
            await alice.relate("tr_follows", bob)
            await carol.relate("tr_follows", alice)

            people = await alice.get_related("tr_follows", direction="both", model_class=TrPerson)
            assert all(type(p) is TrPerson for p in people)
            assert sorted(p.get_raw_id() for p in people) == ["bob", "carol"]
            ids = await alice.get_related("tr_follows", "both")
            assert Counter(map(str, ids)) == Counter(["tr_person:bob", "tr_person:carol"])

            await alice.relate(TrFollows, alice)  # a self-loop: alice once per direction, never more
            ids = await alice.get_related(TrFollows, direction="both")
            assert Counter(map(str, ids)) == Counter(["tr_person:bob", "tr_person:carol", "tr_person:alice", "tr_person:alice"])
            people = await alice.get_related("tr_follows", "both", TrPerson)
            assert sorted(p.get_raw_id() for p in people) == ["alice", "alice", "bob", "carol"]

            # Out-neighbours first, then in-neighbours (the order inside each group is unspecified).
            out = await alice.get_related("tr_follows", "out")
            incoming = await alice.get_related("tr_follows", "in")
            assert Counter(map(str, ids[: len(out)])) == Counter(map(str, out))
            assert Counter(map(str, ids[len(out) :])) == Counter(map(str, incoming))

    @pytest.mark.asyncio
    async def test_graph_helpers_for_non_identifier_id(self) -> None:
        async with orm_client(*GRAPH_TABLES):
            _alice, _bob, widget = await _nodes()
            joe = await TrPerson(id="joe@x.com", name="Joe").save()
            joe_id = RecordID("tr_person", "joe@x.com")

            edge = await joe.relate(TrPurchased, widget)
            assert edge.in_ == joe_id
            assert await joe.get_related(TrPurchased) == [WIDGET]
            assert [p.get_raw_id() for p in await joe.get_related(TrPurchased, "out", TrProduct)] == ["widget"]
            assert [p.get_raw_id() for p in await widget.get_related(TrPurchased, "in", TrPerson)] == ["joe@x.com"]

            await joe.remove_relation(TrPurchased, widget)
            assert await joe.get_related(TrPurchased) == []

            await joe.relate(TrFollows, widget)
            await widget.remove_relation("tr_follows", joe, reverse=True)
            assert await joe.get_related("tr_follows", "both") == []

            await joe.relate(TrPurchased, widget)
            await joe.remove_all_relations([TrPurchased], "both")
            assert await TrPurchased.objects().count() == 0

    @pytest.mark.asyncio
    async def test_string_relate_and_traverse_bind_the_record_ids(self) -> None:
        async with orm_client(*GRAPH_TABLES) as client:
            alice, bob, widget = await _nodes()
            odd = RecordID("tr_person:x; DELETE tr_person; --", 1)

            rows = await alice.relate("tr_follows", odd)
            assert [(row["in"], row["out"]) for row in rows] == [(ALICE_P, odd)]
            rows = await alice.relate("tr_follows", odd, reverse=True, weight=2)
            assert [(row["in"], row["out"], row["weight"]) for row in rows] == [(odd, ALICE_P, 2)]
            people = await client.query("SELECT VALUE id FROM tr_person ORDER BY id;", {})
            assert people == [ALICE_P, BOB_P]  # the table name was bound, never run

            joe = await TrPerson(id="joe@x.com", name="Joe").save()
            joe_id = RecordID("tr_person", "joe@x.com")
            rows = await joe.relate("tr_follows", bob, since="2025-01-01")
            assert [(row["in"], row["out"], row["since"]) for row in rows] == [(joe_id, BOB_P, "2025-01-01")]
            await bob.relate("tr_follows", widget)
            await bob.relate("tr_follows", alice, reverse=True)
            assert await joe.traverse("->tr_follows->tr_person") == [BOB_P]
            assert await joe.traverse("->tr_follows->tr_person->tr_follows->tr_product") == [WIDGET]
            assert sorted(map(str, await bob.traverse("<-tr_follows<-tr_person"))) == sorted(map(str, [ALICE_P, joe_id]))

            source = TrAnyIdNode(id=ALICE_P)  # a RecordID id is bound as is
            assert await source.traverse("->tr_follows->tr_person") == [BOB_P]  # not odd: another table

    @pytest.mark.asyncio
    async def test_missing_edge_table_is_a_silent_noop(self) -> None:
        async with orm_client(*GRAPH_TABLES):
            alice, bob, _widget = await _nodes()

            assert await alice.remove_relation("tr_never", bob) is None
            assert await alice.remove_relation("tr_never", bob, reverse=True) is None
            assert await alice.remove_relation("tr_never", "bob") is None
            assert await alice.remove_relation("tr_never", "bob", reverse=True) is None
            assert await alice.remove_all_relations(["tr_never"], direction="both") is None
            assert await alice.remove_all_relations("tr_never") is None
            assert await alice.get_related("tr_never", direction="both") == []

    @pytest.mark.asyncio
    async def test_missing_source_table_is_a_silent_noop(self) -> None:
        """A source whose own table was never created: the server diverges, the ORM does not.

        The graph-path ``DELETE`` raises ``NotFoundError`` ("The table … does not exist") on 3.x
        and returns ``[]`` on 2.x. Outside a transaction the ORM keeps the v0.7.0 contract on both
        lines: a silent no-op that removes nothing.
        """
        async with orm_client(*GRAPH_TABLES) as client:
            alice, bob, _widget = await _nodes()
            await alice.relate("tr_follows", bob)  # the edge table exists, only tr_ghost does not
            ghost = TrGhost(id="g")  # never saved: its table is never created

            statement = "DELETE $src->tr_follows WHERE out = $dst;"
            variables = {"src": RecordID("tr_ghost", "g"), "dst": BOB_P}
            if (await _server_version())[0] >= 3:
                with pytest.raises(NotFoundError, match="does not exist"):
                    await client.query(statement, variables)
            else:
                assert await client.query(statement, variables) == []

            assert await ghost.remove_relation("tr_follows", bob) is None
            assert await ghost.remove_relation("tr_follows", "bob") is None
            assert await ghost.remove_relation("tr_follows", bob, reverse=True) is None
            assert await ghost.remove_relation(TrFollows, "bob", reverse=True) is None
            assert await ghost.remove_all_relations(["tr_follows"], direction="both") is None
            assert await ghost.remove_all_relations(TrFollows, "in") is None
            assert _people_pairs(await _stored(client, "tr_follows")) == [("tr_person:alice", "tr_person:bob")]

    @pytest.mark.asyncio
    async def test_remove_relation_bare_id_any_table(self) -> None:
        async with orm_client(*GRAPH_TABLES) as client:
            alice, bob, _widget = await _nodes()
            carol = await TrPerson(id="carol", name="Carol").save()
            pbob = await TrProduct(id="bob", name="Bob the product").save()
            await alice.relate("tr_follows", bob)
            await alice.relate("tr_follows", pbob)
            await alice.relate("tr_follows", carol)
            await client.query("CREATE tr_product:7; RELATE tr_person:alice->tr_follows->tr_product:7;", {})

            async def outs() -> list[str]:
                rows = await client.query("SELECT VALUE out FROM tr_follows WHERE in = $a;", {"a": ALICE_P})
                return sorted(map(str, rows))

            assert len(await outs()) == 4
            await alice.remove_relation("tr_follows", "bob")  # tr_person:bob and tr_product:bob
            assert await outs() == sorted([str(RecordID("tr_person", "carol")), "tr_product:7"])

            await alice.remove_relation("tr_follows", "7")  # a string never matches an integer id
            assert await outs() == sorted([str(RecordID("tr_person", "carol")), "tr_product:7"])

            await alice.remove_relation(TrFollows, RecordID("tr_product", 7))  # a RecordID reaches it
            assert await outs() == [str(RecordID("tr_person", "carol"))]

    @pytest.mark.asyncio
    async def test_remove_relation_bare_id_reverse(self) -> None:
        async with orm_client(*GRAPH_TABLES) as client:
            alice, bob, _widget = await _nodes()
            carol = await TrPerson(id="carol", name="Carol").save()
            await bob.relate("tr_follows", alice)
            await carol.relate("tr_follows", alice)
            await alice.relate("tr_follows", bob)

            await alice.remove_relation("tr_follows", "bob", reverse=True)

            assert _people_pairs(await _stored(client, "tr_follows")) == [
                ("tr_person:alice", "tr_person:bob"),
                ("tr_person:carol", "tr_person:alice"),
            ]


class TestGraphParityTransactionsE2E:
    @pytest.mark.asyncio
    async def test_remove_in_interactive_tx_rolls_back_and_commits(self) -> None:
        async with orm_client(*GRAPH_TABLES) as client:
            if not await _native_txn_supported():
                pytest.skip("requires SurrealDB 3.x native transactions")
            alice, bob, _widget = await _nodes()
            carol = await TrPerson(id="carol", name="Carol").save()
            await alice.relate("tr_follows", bob)
            await carol.relate("tr_follows", alice)

            reached = False
            with pytest.raises(RuntimeError, match="roll back"):
                async with SurrealDBConnectionManager.transaction() as tx:
                    assert tx.is_interactive
                    await alice.remove_relation("tr_follows", bob, tx=tx)
                    assert await TrFollows.objects(tx=tx).filter(in_=alice).count() == 0
                    assert await TrFollows.objects().filter(in_=alice).count() == 1  # outside: not yet
                    reached = True
                    raise RuntimeError("roll back")
            assert reached
            assert _people_pairs(await _stored(client, "tr_follows")) == [
                ("tr_person:alice", "tr_person:bob"),
                ("tr_person:carol", "tr_person:alice"),
            ]

            async with SurrealDBConnectionManager.transaction() as tx:
                await alice.remove_all_relations(["tr_follows", "tr_never"], direction="both", tx=tx)
                assert await TrFollows.objects(tx=tx).count() == 0
            assert await _stored(client, "tr_follows") == []

            await alice.relate("tr_follows", bob)
            async with SurrealDBConnectionManager.transaction() as tx:
                await alice.remove_relation(TrFollows, "bob", False, tx)  # bare id, positional tx
            assert await _stored(client, "tr_follows") == []

    @pytest.mark.asyncio
    async def test_remove_in_buffered_tx_applies_at_commit(self) -> None:
        async with http_orm_client(*GRAPH_TABLES) as client:
            alice, bob, _widget = await _nodes()
            carol = await TrPerson(id="carol", name="Carol").save()
            await alice.relate("tr_follows", bob)
            await carol.relate("tr_follows", alice)

            async with SurrealDBConnectionManager.transaction() as tx:
                assert not tx.is_interactive
                assert await alice.remove_relation("tr_follows", bob, tx=tx) is None
                assert await TrFollows.objects().count() == 2  # queued, not run
            assert _people_pairs(await _stored(client, "tr_follows")) == [("tr_person:carol", "tr_person:alice")]

            async with SurrealDBConnectionManager.transaction() as tx:
                await alice.remove_all_relations(["tr_follows", "tr_never"], direction="both", tx=tx)
                assert await TrFollows.objects().count() == 1
            assert await _stored(client, "tr_follows") == []

    @pytest.mark.asyncio
    async def test_remove_in_buffered_tx_rolled_back_with_failing_statement(self) -> None:
        async with http_orm_client(*GRAPH_TABLES) as client:
            alice, bob, _widget = await _nodes()
            await alice.relate("tr_follows", bob)

            with pytest.raises(SurrealDbError, match="rolled back"):
                async with SurrealDBConnectionManager.transaction() as tx:
                    await alice.remove_relation("tr_follows", bob, tx=tx)
                    await alice.remove_all_relations(["tr_follows"], "both", tx)
                    await TrPerson(id="alice", name="Again").save(tx=tx)  # alice exists: CREATE fails at commit

            assert _people_pairs(await _stored(client, "tr_follows")) == [("tr_person:alice", "tr_person:bob")]
            assert await alice.get_related("tr_follows") == [BOB_P]

    @pytest.mark.asyncio
    async def test_relate_reverse_in_buffered_tx(self) -> None:
        async with http_orm_client(*GRAPH_TABLES) as client:
            alice, bob, _widget = await _nodes()

            async with SurrealDBConnectionManager.transaction() as tx:
                edge = await alice.relate(TrFollows, bob, reverse=True, tx=tx, weight=4)
                assert (edge.id, edge.in_, edge.out, edge.weight) == (None, BOB_P, ALICE_P, 4)
                assert await alice.relate("tr_follows", bob, True, tx) == []
                assert await _stored(client, "tr_follows") == []

            stored = await _stored(client, "tr_follows")
            assert _people_pairs(stored) == [("tr_person:bob", "tr_person:alice")] * 2
            assert sorted(row.get("weight", 0) for row in stored) == [0, 4]
            assert await bob.get_related("tr_follows") == [ALICE_P, ALICE_P]

    @pytest.mark.asyncio
    async def test_remove_bare_id_in_buffered_tx(self) -> None:
        async with http_orm_client(*GRAPH_TABLES) as client:
            alice, bob, _widget = await _nodes()
            pbob = await TrProduct(id="bob", name="Bob the product").save()
            await alice.relate("tr_follows", bob)
            await alice.relate("tr_follows", pbob)

            async def count() -> int:
                rows = await client.query("SELECT VALUE id FROM tr_follows WHERE in = $a;", {"a": ALICE_P})
                return len(rows)

            async with SurrealDBConnectionManager.transaction() as tx:
                await alice.remove_relation("tr_follows", "bob", tx=tx)
                assert await count() == 2  # queued, not run
            assert await count() == 0

    @pytest.mark.asyncio
    async def test_missing_source_table_in_buffered_tx(self) -> None:
        """Under ``tx=`` the server's divergence stands: 3.x fails the commit, 2.x commits."""
        async with http_orm_client(*GRAPH_TABLES) as client:
            alice, bob, _widget = await _nodes()
            carol = await TrPerson(id="carol", name="Carol").save()
            await alice.relate("tr_follows", bob)
            await alice.relate("tr_follows", carol)
            ghost = TrGhost(id="g")  # never saved: its table is never created

            async def remove() -> None:
                async with SurrealDBConnectionManager.transaction() as tx:
                    assert not tx.is_interactive
                    await alice.remove_relation("tr_follows", bob, tx=tx)
                    await ghost.remove_relation("tr_follows", carol, tx=tx)
                    await ghost.remove_all_relations(["tr_follows"], direction="both", tx=tx)

            if (await _server_version())[0] >= 3:
                with pytest.raises(SurrealDbError, match="does not exist"):
                    await remove()
                assert _people_pairs(await _stored(client, "tr_follows")) == [
                    ("tr_person:alice", "tr_person:bob"),
                    ("tr_person:alice", "tr_person:carol"),
                ]  # alice's removal is rolled back with the batch
            else:
                await remove()
                assert _people_pairs(await _stored(client, "tr_follows")) == [("tr_person:alice", "tr_person:carol")]

    @pytest.mark.asyncio
    async def test_missing_source_table_in_interactive_tx(self) -> None:
        """3.x over WebSocket: the statement fails at once, and the transaction with it."""
        async with orm_client(*GRAPH_TABLES) as client:
            if not await _native_txn_supported():
                pytest.skip("requires SurrealDB 3.x native transactions")
            alice, bob, _widget = await _nodes()
            await alice.relate("tr_follows", bob)
            ghost = TrGhost(id="g")

            reached = False
            with pytest.raises(SurrealDbError, match="does not exist"):
                async with SurrealDBConnectionManager.transaction() as tx:
                    assert tx.is_interactive
                    await alice.remove_relation("tr_follows", bob, tx=tx)
                    reached = True
                    await ghost.remove_relation("tr_follows", bob, tx=tx)
            assert reached
            assert _people_pairs(await _stored(client, "tr_follows")) == [("tr_person:alice", "tr_person:bob")]


# ==================== Task 7 — bulk_create() on an edge model (spec §4.3.5) ====================


def _purchase_rows(edges: list[Any]) -> list[tuple[str, str, int]]:
    """``(in, out, quantity)`` of each edge or row, endpoints as strings, in the given order."""
    return [
        (str(e["in"]), str(e["out"]), e["quantity"]) if isinstance(e, dict) else (str(e.in_), str(e.out), e.quantity)
        for e in edges
    ]


class TestEdgeBulkCreateStatements:
    """What ``bulk_create()`` sends for an edge model before any server runs it (no server)."""

    @pytest.mark.asyncio
    async def test_empty_list_makes_no_call(self, no_client: list[str]) -> None:
        assert await TrPurchased.objects().bulk_create([]) == []
        tx = BufferedTransaction()
        assert await TrPurchased.objects(tx=tx).bulk_create([]) == []
        assert tx.statements == []
        assert no_client == []

    @pytest.mark.asyncio
    async def test_buffered_bulk_create_queues_one_guarded_statement(self) -> None:
        tx = BufferedTransaction()
        inputs: list[BaseSurrealModel] = [
            TrPurchased(in_=ALICE_P, out=WIDGET, quantity=2),
            TrPurchased(id="b1", in_=BOB_P, out=WIDGET),
        ]

        out = await TrPurchased.objects(tx=tx).bulk_create(inputs)

        assert out == inputs and all(a is b for a, b in zip(out, inputs, strict=True))  # the inputs, unchanged
        assert tx.statements == [_queued_guarded_insert("tr_purchased", 0)]  # one statement for the batch
        assert tx.variables == {
            "t0_data": [
                {"in": ALICE_P, "out": WIDGET, "quantity": 2, "price": 0.0},
                {"in": BOB_P, "out": WIDGET, "quantity": 1, "price": 0.0, "id": RecordID("tr_purchased", "b1")},
            ]
        }

    @pytest.mark.asyncio
    async def test_every_id_is_checked_before_any_call(self, no_client: list[str]) -> None:
        valid = TrPurchased(in_=ALICE_P, out=WIDGET)
        foreign = TrPurchased(id=RecordID("other", "x"), in_=ALICE_P, out=WIDGET)
        with pytest.raises(ValueError, match="belongs to table 'other'"):
            await TrPurchased.objects().bulk_create([valid, foreign])

        boolean = TrPurchased(in_=ALICE_P, out=WIDGET)
        object.__setattr__(boolean, "id", True)  # `id: str | RecordID | None` refuses True at validation
        with pytest.raises(ValueError, match="a record id cannot be a boolean"):
            await TrPurchased.objects().bulk_create([valid, boolean])

        tx = BufferedTransaction()
        with pytest.raises(ValueError, match="belongs to table 'other'"):
            await TrPurchased.objects(tx=tx).bulk_create([valid, foreign])
        assert tx.statements == []
        assert no_client == []


class TestEdgeBulkCreateE2E:
    @pytest.mark.asyncio
    async def test_bulk_create_one_insert_relation_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with orm_client(*EDGE_TABLES) as client:
            alice, bob, widget = await _nodes()
            spy = _spy_insert_relation(monkeypatch, client)

            created = await TrPurchased.objects().bulk_create(
                [
                    TrPurchased(in_=alice, out=widget, quantity=1),
                    TrPurchased(in_=bob, out=widget, quantity=2, price=4.5),
                    TrPurchased.model_validate({"in": alice, "out": widget, "quantity": 3}),
                ]
            )

            assert spy.calls == [
                (
                    "tr_purchased",
                    [
                        {"in": ALICE_P, "out": WIDGET, "quantity": 1, "price": 0.0},
                        {"in": BOB_P, "out": WIDGET, "quantity": 2, "price": 4.5},
                        {"in": ALICE_P, "out": WIDGET, "quantity": 3, "price": 0.0},
                    ],
                )
            ]
            assert [type(edge) for edge in created] == [TrPurchased] * 3
            ids = [edge.id for edge in created]
            assert all(isinstance(i, RecordID) and i.table_name == "tr_purchased" for i in ids)
            assert len({str(i) for i in ids}) == 3
            assert _purchase_rows(created) == [
                ("tr_person:alice", "tr_product:widget", 1),
                ("tr_person:bob", "tr_product:widget", 2),
                ("tr_person:alice", "tr_product:widget", 3),
            ]
            assert [edge.price for edge in created] == [0.0, 4.5, 0.0]  # type: ignore[attr-defined]

            stored = await _stored(client, "tr_purchased")
            assert sorted(str(row["id"]) for row in stored) == sorted(str(i) for i in ids)
            # Relations, not plain records carrying `in`/`out` fields.
            assert await client.query("SELECT VALUE ->tr_purchased->? FROM ONLY tr_person:alice;", {}) == [WIDGET] * 2
            assert await client.query("SELECT VALUE ->tr_purchased->? FROM ONLY tr_person:bob;", {}) == [WIDGET]
            related = await bob.get_related(TrPurchased, model_class=TrProduct)
            assert [(type(p), p.get_raw_id()) for p in related] == [(TrProduct, "widget")]

    @pytest.mark.asyncio
    async def test_bulk_create_explicit_ids_guarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with orm_client(*EDGE_TABLES) as client:
            alice, bob, widget = await _nodes()
            spy = _spy_insert_relation(monkeypatch, client)

            created = await TrPurchased.objects().bulk_create(
                [
                    TrPurchased(id="b1", in_=alice, out=widget, quantity=1),
                    TrPurchased(id=RecordID("tr_purchased", "b2"), in_=bob, out=widget, quantity=2),
                    TrPurchased(in_=alice, out=widget, quantity=3),  # an auto id in the same batch
                ]
            )

            assert spy.calls == []  # one explicit id sends the whole batch through the guarded statement
            assert [edge.id for edge in created][:2] == [RecordID("tr_purchased", "b1"), RecordID("tr_purchased", "b2")]
            auto_id = created[2].id
            assert isinstance(auto_id, RecordID) and auto_id.table_name == "tr_purchased"
            assert _purchase_rows(created) == [
                ("tr_person:alice", "tr_product:widget", 1),
                ("tr_person:bob", "tr_product:widget", 2),
                ("tr_person:alice", "tr_product:widget", 3),
            ]

            stored = await _stored(client, "tr_purchased")
            assert sorted(str(row["id"]) for row in stored) == sorted(str(edge.id) for edge in created)
            assert await client.query("SELECT VALUE ->tr_purchased->? FROM ONLY tr_person:alice;", {}) == [WIDGET] * 2
            assert await client.query("SELECT VALUE ->tr_purchased->? FROM ONLY tr_person:bob;", {}) == [WIDGET]

    @pytest.mark.asyncio
    async def test_bulk_create_duplicate_writes_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with orm_client(*EDGE_TABLES) as client:
            alice, bob, widget = await _nodes()
            await TrPurchased(id="b1", in_=alice, out=widget, quantity=2).save()
            spy = _spy_insert_relation(monkeypatch, client)

            with pytest.raises(SurrealDbError, match="already exists"):
                await TrPurchased.objects().bulk_create(
                    [
                        TrPurchased(id="b3", in_=bob, out=widget, quantity=3),
                        TrPurchased(in_=bob, out=widget, quantity=4),
                        TrPurchased(id="b1", in_=alice, out=widget, quantity=9),
                    ]
                )

            # The same id twice in one batch: the second row meets the first one (measured on
            # 2.7.0, 3.1.5 and 3.3.3 on 2026-10-12).
            with pytest.raises(SurrealDbError, match="already exists"):
                await TrPurchased.objects().bulk_create(
                    [
                        TrPurchased(id="b4", in_=bob, out=widget, quantity=4),
                        TrPurchased(id="b4", in_=bob, out=widget, quantity=5),
                    ]
                )

            assert spy.calls == []
            stored = await _stored(client, "tr_purchased")
            assert [(row["id"], row["quantity"]) for row in stored] == [(RecordID("tr_purchased", "b1"), 2)]
            assert await client.query("SELECT VALUE ->tr_purchased->? FROM ONLY tr_person:bob;", {}) == []

    @pytest.mark.asyncio
    async def test_bulk_create_foreign_table_id_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with orm_client(*EDGE_TABLES, "other") as client:
            alice, _bob, widget = await _nodes()
            spy = _spy_insert_relation(monkeypatch, client)

            with pytest.raises(ValueError, match="belongs to table 'other', not to the edge table 'tr_purchased'"):
                await TrPurchased.objects().bulk_create(
                    [TrPurchased(in_=alice, out=widget), TrPurchased(id=RecordID("other", "x"), in_=alice, out=widget)]
                )

            assert spy.calls == []
            assert await _stored(client, "tr_purchased") == []
            assert await _stored(client, "other") == []

    @pytest.mark.asyncio
    async def test_bulk_create_in_interactive_tx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with orm_client(*EDGE_TABLES) as client:
            if not await _native_txn_supported():
                pytest.skip("requires SurrealDB 3.x native transactions")
            alice, bob, widget = await _nodes()
            spy = _spy_insert_relation(monkeypatch, client)

            reached = False
            with pytest.raises(RuntimeError, match="roll back"):
                async with SurrealDBConnectionManager.transaction() as tx:
                    assert tx.is_interactive
                    created = await TrPurchased.objects(tx=tx).bulk_create(
                        [TrPurchased(in_=alice, out=widget, quantity=1), TrPurchased(id="t1", in_=bob, out=widget, quantity=2)]
                    )
                    assert [type(edge) for edge in created] == [TrPurchased] * 2
                    assert isinstance(created[0].id, RecordID)
                    assert created[1].id == RecordID("tr_purchased", "t1")
                    assert _purchase_rows(created) == [
                        ("tr_person:alice", "tr_product:widget", 1),
                        ("tr_person:bob", "tr_product:widget", 2),
                    ]
                    assert await TrPurchased.objects(tx=tx).count() == 2
                    reached = True
                    raise RuntimeError("roll back")
            assert reached
            assert await _stored(client, "tr_purchased") == []

            async with SurrealDBConnectionManager.transaction() as tx:
                created = await TrPurchased.objects(tx=tx).bulk_create(
                    [TrPurchased(in_=alice, out=widget, quantity=5), TrPurchased(in_=bob, out=widget, quantity=6)]
                )
                assert all(isinstance(edge.id, RecordID) for edge in created)
            assert spy.calls == []  # under tx=, the guarded statement runs through tx.add()
            stored = await _stored(client, "tr_purchased")
            assert sorted(str(row["id"]) for row in stored) == sorted(str(edge.id) for edge in created)
            assert await client.query("SELECT VALUE ->tr_purchased->? FROM ONLY tr_person:bob;", {}) == [WIDGET]

    @pytest.mark.asyncio
    async def test_bulk_create_in_buffered_tx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with http_orm_client(*EDGE_TABLES) as client:
            alice, bob, widget = await _nodes()
            spy = _spy_insert_relation(monkeypatch, client)
            inputs: list[BaseSurrealModel] = [
                TrPurchased(in_=alice, out=widget, quantity=1),
                TrPurchased(id="t1", in_=bob, out=widget, quantity=2),
            ]

            async with SurrealDBConnectionManager.transaction() as tx:
                assert not tx.is_interactive
                out = await TrPurchased.objects(tx=tx).bulk_create(inputs)
                assert all(a is b for a, b in zip(out, inputs, strict=True))  # the inputs, unchanged
                assert [edge.id for edge in out] == [None, "t1"]
                assert await _stored(client, "tr_purchased") == []  # queued, not run

            assert spy.calls == []
            stored = await _stored(client, "tr_purchased")
            assert sorted(_purchase_rows(stored)) == [
                ("tr_person:alice", "tr_product:widget", 1),
                ("tr_person:bob", "tr_product:widget", 2),
            ]
            assert RecordID("tr_purchased", "t1") in [row["id"] for row in stored]
            assert await client.query("SELECT VALUE ->tr_purchased->? FROM ONLY tr_person:alice;", {}) == [WIDGET]

            # A duplicate in a buffered batch fails the commit; nothing of the batch is written.
            with pytest.raises(SurrealDbError, match="already exists"):
                async with SurrealDBConnectionManager.transaction() as tx:
                    await TrPurchased.objects(tx=tx).bulk_create(
                        [TrPurchased(in_=alice, out=widget, quantity=7), TrPurchased(id="t1", in_=bob, out=widget)]
                    )
            assert len(await _stored(client, "tr_purchased")) == 2


# ==================== Task 8 — writes on an existing edge, *_or_create (spec §4.4, §2.9) ====================


class TrFullEdge(BaseSurrealEdge):
    """Edge on ``tr_rel_full`` (``TYPE RELATION IN tr_person OUT tr_product SCHEMAFULL``)."""

    model_config = SurrealConfigDict(table_name="tr_rel_full")

    quantity: int = 1
    price: float = 0.0


class TrFollowsUq(BaseSurrealEdge):
    """Edge on ``tr_follows_uq``, made idempotent by a ``UNIQUE`` index on ``in, out``."""

    model_config = SurrealConfigDict(table_name="tr_follows_uq")


def _endpoint_guard(context: str, edge: str = "TrPurchased") -> str:
    """The §4.4 refusal for ``context`` (a regex anchored on its ``{context}:`` prefix)."""
    return rf"^{re.escape(context)}: `in`/`out` are the endpoints of edge `{edge}`; SurrealDB ignores a change to them"


def _saved_edge() -> TrPurchased:
    """An edge as a read would hydrate it (an id, both endpoints), without a server."""
    return TrPurchased(id="p1", in_=ALICE_P, out=WIDGET, quantity=2)


_ATOMIC_ENDPOINT_CALLS: dict[str, Callable[[TrPurchased], Awaitable[Any]]] = {
    "append-out": lambda edge: edge.atomic_append("out", BOB_P),
    "remove-in": lambda edge: edge.atomic_remove("in", BOB_P),
    "set_add-in_": lambda edge: edge.atomic_set_add("in_", BOB_P),
    "increment-in_": lambda edge: edge.atomic_increment("in_"),
    "append_many-out": lambda edge: edge.atomic_append_many("out", [BOB_P]),
    "set_add_many-out": lambda edge: edge.atomic_set_add_many("out", [BOB_P]),
    "remove_many-in": lambda edge: edge.atomic_remove_many("in", [BOB_P]),
    "append-out.nested": lambda edge: edge.atomic_append("out.tags", 1),
}


class TestEdgeWriteGuards:
    """A write that would change an existing edge's endpoints is refused before any call (no server)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "data",
        [{"in_": BOB_P}, {"out": BOB_P}, {"in": BOB_P}, {"quantity": 3, "out": BOB_P}],
        ids=["in_", "out", "in-column", "with-a-data-field"],
    )
    async def test_merge_naming_an_endpoint_is_refused(self, no_client: list[str], data: dict[str, Any]) -> None:
        edge = _saved_edge()
        with pytest.raises(ValueError, match=_endpoint_guard("merge()")) as raised:
            await edge.merge(**data)
        assert str(raised.value).endswith("so the ORM refuses it — delete the edge and relate again.")
        assert (edge.in_, edge.out, edge.quantity) == (ALICE_P, WIDGET, 2)  # nothing applied locally
        assert no_client == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["in_", "in", "out"])
    async def test_merge_server_values_naming_an_endpoint_is_refused(self, no_client: list[str], key: str) -> None:
        with pytest.raises(ValueError, match=_endpoint_guard("merge(server_values=)")):
            await _saved_edge().merge(server_values={key: SurrealFunc("NONE")})
        assert no_client == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "operations",
        [
            [{"op": "replace", "path": "/out", "value": BOB_P}],
            [{"op": "replace", "path": "/in", "value": BOB_P}],
            [{"op": "replace", "path": "/in_", "value": BOB_P}],
            [{"op": "add", "path": "/in/x", "value": 1}],
            [{"op": "remove", "path": "/out"}],
            [{"op": "replace", "path": "/quantity", "value": 3}, {"op": "move", "from": "/out", "path": "/old_out"}],
            [{"op": "replace", "path": "", "value": {"in": BOB_P, "out": WIDGET, "quantity": 3}}],
            [{"op": "remove", "path": ""}],
        ],
        ids=[
            "replace-out",
            "replace-in",
            "replace-in_",
            "add-in-child",
            "remove-out",
            "move-from-out",
            "whole-doc",
            "remove-doc",
        ],
    )
    async def test_patch_on_an_endpoint_is_refused(self, no_client: list[str], operations: list[dict[str, Any]]) -> None:
        with pytest.raises(ValueError, match=_endpoint_guard("patch()")):
            await _saved_edge().patch(operations)
        with pytest.raises(ValueError, match=_endpoint_guard("patch()")):
            await TrPurchased.objects().patch(operations)
        with pytest.raises(ValueError, match=_endpoint_guard("patch()")):
            await TrPurchased.objects().filter(in_=ALICE_P).patch(operations)
        assert no_client == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call", list(_ATOMIC_ENDPOINT_CALLS.values()), ids=list(_ATOMIC_ENDPOINT_CALLS))
    async def test_atomic_operation_on_an_endpoint_is_refused(
        self, no_client: list[str], call: Callable[[TrPurchased], Awaitable[Any]]
    ) -> None:
        with pytest.raises(ValueError, match=_endpoint_guard("atomic operation")):
            await call(_saved_edge())
        assert no_client == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "data",
        [{"out": BOB_P}, {"in_": BOB_P}, {"in": BOB_P}, {"quantity": 3, "in_": BOB_P}],
        ids=["out", "in_", "in-column", "with-a-data-field"],
    )
    async def test_bulk_update_naming_an_endpoint_is_refused(self, no_client: list[str], data: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match=_endpoint_guard("bulk_update()")):
            await TrPurchased.objects().filter(quantity=2).bulk_update(**data)
        assert no_client == []

    @pytest.mark.asyncio
    async def test_upsert_is_refused_before_any_call_or_signal(self, no_client: list[str]) -> None:
        events: list[str] = []

        @pre_save.connect(TrPurchased)
        async def on_pre(sender: Any, instance: Any, **kwargs: Any) -> None:
            events.append("pre_save")

        try:
            message = (
                r"^upsert\(\) is not available on an edge: UPSERT stores a plain record that no graph traversal "
                r"sees\. Use save\(\)/relate\(\) to create an edge, merge\(\)/update\(\) to change its data\.$"
            )
            with pytest.raises(SurrealDbError, match=message):
                await _saved_edge().upsert()
            with pytest.raises(SurrealDbError, match=message):
                await TrPurchased(in_=ALICE_P, out=WIDGET).upsert()
            tx = BufferedTransaction(client=None)
            with pytest.raises(SurrealDbError, match=message):
                await _saved_edge().upsert(tx=tx)
            assert tx.statements == []
        finally:
            pre_save.disconnect(on_pre, TrPurchased)
        assert events == []
        assert no_client == []

    @pytest.mark.asyncio
    async def test_save_server_values_is_refused(self, no_client: list[str]) -> None:
        message = r"^save\(server_values=\) is not available on an edge: INSERT RELATION cannot evaluate SET"
        with pytest.raises(SurrealDbError, match=message):
            await TrPurchased(in_=ALICE_P, out=WIDGET).save(server_values={"quantity": SurrealFunc("1")})
        with pytest.raises(SurrealDbError, match=message):
            await _saved_edge().save(server_values={"price": SurrealFunc("math::max([1, 2])")}, extra_vars={"x": 1})
        assert no_client == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["out", "in_", "in"])
    async def test_update_or_create_refuses_endpoint_defaults_before_the_lookup(self, no_client: list[str], key: str) -> None:
        with pytest.raises(ValueError, match=_endpoint_guard("update_or_create(defaults=)", "TrFollows")):
            await TrFollows.objects().update_or_create(in_=ALICE_P, out=BOB_P, defaults={key: WIDGET, "weight": 2})
        assert no_client == []  # refused before the lookup: no connection was asked for

    def test_data_fields_and_reads_of_an_endpoint_pass_the_guards(self) -> None:
        TrPurchased._reject_computed_writes(["quantity", "price"], "merge()")
        assert TrPurchased._validate_atomic_field("quantity") == "quantity"
        TrPurchased._reject_computed_patch(
            [
                {"op": "replace", "path": "/quantity", "value": 3},
                {"op": "copy", "from": "/out", "path": "/old_out"},  # copy only reads its source
            ],
            "patch()",
        )
        # A whole-document replace that leaves the endpoints out does not name them.
        TrPurchased._reject_computed_patch([{"op": "replace", "path": "", "value": {"quantity": 3}}], "patch()")

    def test_node_models_protect_nothing(self) -> None:
        TrOrder._reject_computed_writes(["in", "in_", "out", "customer"], "merge()")
        assert TrOrder._validate_atomic_field("out") == "out"
        TrOrder._reject_computed_patch([{"op": "replace", "path": "/out", "value": 1}, {"op": "remove", "path": ""}], "patch()")


WRITE_GUARD_TABLES = (*EDGE_TABLES, "tr_rel_full")


def _edge_rows(rows: list[Any], *fields: str) -> list[tuple[Any, ...]]:
    """``(in, out, *fields)`` of each stored row, endpoints as strings."""
    return [(str(row["in"]), str(row["out"]), *(row.get(field) for field in fields)) for row in rows]


class TestEdgeWriteGuardsE2E:
    @pytest.mark.asyncio
    async def test_upsert_on_edge_refused_and_writes_nothing(self) -> None:
        async with orm_client(*WRITE_GUARD_TABLES) as client:
            alice, _bob, widget = await _nodes()

            with pytest.raises(SurrealDbError, match=r"upsert\(\) is not available on an edge"):
                await TrPurchased(id="u1", in_=alice, out=widget, quantity=2).upsert()
            assert await _stored(client, "tr_purchased") == []

            edge = await alice.relate(TrPurchased, widget, quantity=3)
            edge.quantity = 4
            with pytest.raises(SurrealDbError, match=r"upsert\(\) is not available on an edge"):
                await edge.upsert()
            stored = await _stored(client, "tr_purchased")
            assert [row["id"] for row in stored] == [edge.id]
            assert _edge_rows(stored, "quantity") == [("tr_person:alice", "tr_product:widget", 3)]

    @pytest.mark.asyncio
    async def test_save_server_values_refused_and_writes_nothing(self) -> None:
        async with orm_client(*WRITE_GUARD_TABLES) as client:
            alice, _bob, widget = await _nodes()
            with pytest.raises(SurrealDbError, match=r"save\(server_values=\) is not available on an edge"):
                await TrPurchased(in_=alice, out=widget).save(server_values={"quantity": SurrealFunc("1")})
            with pytest.raises(SurrealDbError, match=r"save\(server_values=\) is not available on an edge"):
                await TrPurchased(id="s1", in_=alice, out=widget).save(server_values={"quantity": SurrealFunc("1")})
            assert await _stored(client, "tr_purchased") == []

    @pytest.mark.asyncio
    async def test_merge_data_field_works(self) -> None:
        async with orm_client(*WRITE_GUARD_TABLES) as client:
            alice, bob, widget = await _nodes()
            edge = await alice.relate(TrPurchased, widget, quantity=2)

            await edge.merge(quantity=5)
            assert (edge.in_, edge.out, edge.quantity) == (ALICE_P, WIDGET, 5)
            await edge.merge(server_values={"price": SurrealFunc("math::max([1.5, 2.5])")})
            assert edge.price == 2.5

            with pytest.raises(ValueError, match="endpoints"):
                await edge.merge(out=bob, quantity=9)
            with pytest.raises(ValueError, match="endpoints"):
                await edge.merge(server_values={"in": SurrealFunc("tr_person:bob")})

            stored = await _stored(client, "tr_purchased")
            assert _edge_rows(stored, "quantity", "price") == [("tr_person:alice", "tr_product:widget", 5, 2.5)]
            assert await client.query("SELECT VALUE ->tr_purchased->? FROM ONLY tr_person:alice;", {}) == [WIDGET]

    @pytest.mark.asyncio
    async def test_patch_atomic_and_bulk_update_on_data_fields_work(self) -> None:
        async with orm_client(*WRITE_GUARD_TABLES) as client:
            alice, bob, widget = await _nodes()
            edge = await alice.relate(TrPurchased, widget, quantity=2)

            await edge.patch([{"op": "replace", "path": "/quantity", "value": 4}])
            assert edge.quantity == 4
            await edge.atomic_increment("quantity")
            assert edge.quantity == 5
            assert await TrPurchased.objects().filter(in_=alice).patch([{"op": "replace", "path": "/price", "value": 1.5}]) == 1
            assert await TrPurchased.objects().filter(in_="tr_person:alice").bulk_update(price=2.5) == 1

            # The endpoint forms of the same calls write nothing.
            with pytest.raises(ValueError, match="endpoints"):
                await edge.patch([{"op": "replace", "path": "/out", "value": bob.get_id()}])
            with pytest.raises(ValueError, match="endpoints"):
                await TrPurchased.objects().filter(in_=alice).patch([{"op": "replace", "path": "/in", "value": BOB_P}])
            with pytest.raises(ValueError, match="endpoints"):
                await edge.atomic_set_add("out", BOB_P)
            with pytest.raises(ValueError, match="endpoints"):
                await TrPurchased.objects().filter(in_=alice).bulk_update(out=BOB_P)

            stored = await _stored(client, "tr_purchased")
            assert _edge_rows(stored, "quantity", "price") == [("tr_person:alice", "tr_product:widget", 5, 2.5)]
            assert await client.query("SELECT VALUE ->tr_purchased->? FROM ONLY tr_person:alice;", {}) == [WIDGET]

    @pytest.mark.asyncio
    async def test_update_keeps_endpoints_on_schemafull_relation(self) -> None:
        async with orm_client(*WRITE_GUARD_TABLES) as client:
            alice, _bob, widget = await _nodes()
            await client.query(
                "DEFINE TABLE tr_rel_full TYPE RELATION IN tr_person OUT tr_product SCHEMAFULL; "
                "DEFINE FIELD quantity ON tr_rel_full TYPE int; "
                "DEFINE FIELD price ON tr_rel_full TYPE float;",
                {},
            )
            edge = await alice.relate(TrFullEdge, widget, quantity=2, price=1.5)

            # The full replace keeps the endpoints: SurrealDB 2.x refuses a TYPE RELATION … SCHEMAFULL
            # replace without them ("Found NONE for field `in`"); 3.x keeps them either way.
            assert set(edge._write_payload(replace=True)) == {"in", "out", "quantity", "price"}
            edge.quantity = 7
            await edge.update()

            stored = await _stored(client, "tr_rel_full")
            assert [row["id"] for row in stored] == [edge.id]
            assert _edge_rows(stored, "quantity", "price") == [("tr_person:alice", "tr_product:widget", 7, 1.5)]
            assert await client.query("SELECT VALUE ->tr_rel_full->? FROM ONLY tr_person:alice;", {}) == [WIDGET]

    @pytest.mark.asyncio
    async def test_get_or_create_is_idempotent_relate(self) -> None:
        async with orm_client(*WRITE_GUARD_TABLES) as client:
            alice, bob, _widget = await _nodes()

            edge, created = await TrFollows.objects().get_or_create(in_=alice, out=bob)
            assert created is True
            assert isinstance(edge, TrFollows) and isinstance(edge.id, RecordID)
            assert (edge.in_, edge.out) == (ALICE_P, BOB_P)

            again, created_again = await TrFollows.objects().get_or_create(in_=alice, out=bob)
            assert created_again is False
            assert isinstance(again, TrFollows) and again.id == edge.id

            assert [row["id"] for row in await _stored(client, "tr_follows")] == [edge.id]
            assert await client.query("SELECT VALUE ->tr_follows->? FROM ONLY tr_person:alice;", {}) == [BOB_P]

    @pytest.mark.asyncio
    async def test_get_or_create_with_string_endpoints_is_idempotent(self) -> None:
        async with orm_client(*WRITE_GUARD_TABLES) as client:
            alice, bob, _widget = await _nodes()

            edge, created = await TrFollows.objects().get_or_create(in_="tr_person:alice", out="tr_person:bob")
            assert created is True
            assert (edge.in_, edge.out) == (ALICE_P, BOB_P)

            again, created_again = await TrFollows.objects().get_or_create(in_="tr_person:alice", out="tr_person:bob")
            assert (created_again, again.id) == (False, edge.id)

            # Instances, record ids and strings converge on the one row.
            by_instance, created_by_instance = await TrFollows.objects().get_or_create(in_=alice, out=bob)
            assert (created_by_instance, by_instance.id) == (False, edge.id)
            by_record_id, created_by_record_id = await TrFollows.objects().get_or_create(in_=ALICE_P, out="tr_person:bob")
            assert (created_by_record_id, by_record_id.id) == (False, edge.id)

            stored = await _stored(client, "tr_follows")
            assert [row["id"] for row in stored] == [edge.id]
            assert [(row["in"], row["out"]) for row in stored] == [(ALICE_P, BOB_P)]  # record ids, not strings
            assert await client.query("SELECT VALUE ->tr_follows->? FROM ONLY tr_person:alice;", {}) == [BOB_P]

    @pytest.mark.asyncio
    async def test_get_or_create_defaults_may_name_an_endpoint(self) -> None:
        async with orm_client(*WRITE_GUARD_TABLES) as client:
            alice, bob, widget = await _nodes()

            edge, created = await TrFollows.objects().get_or_create(in_=alice, defaults={"out": bob, "weight": 2})
            assert created is True
            assert (edge.out, edge.weight) == (BOB_P, 2)

            # On a match the defaults are not applied: the endpoint in them is never a write.
            again, created_again = await TrFollows.objects().get_or_create(in_=alice, defaults={"out": widget})
            assert (created_again, again.id, again.out) == (False, edge.id, BOB_P)
            assert _edge_rows(await _stored(client, "tr_follows"), "weight") == [("tr_person:alice", "tr_person:bob", 2)]

    @pytest.mark.asyncio
    async def test_update_or_create_changes_data_not_endpoints(self) -> None:
        async with orm_client(*WRITE_GUARD_TABLES) as client:
            alice, bob, _widget = await _nodes()
            existing = await alice.relate(TrFollows, bob, weight=1)

            edge, created = await TrFollows.objects().update_or_create(in_=alice, out=bob, defaults={"weight": 3})
            assert created is False
            assert (edge.id, edge.in_, edge.out, edge.weight) == (existing.id, ALICE_P, BOB_P, 3)

            by_string, created_by_string = await TrFollows.objects().update_or_create(
                in_="tr_person:alice", out="tr_person:bob", defaults={"weight": 4}
            )
            assert (created_by_string, by_string.id, by_string.weight) == (False, existing.id, 4)
            # Endpoints only: nothing is left to write once they are dropped from the merge.
            same, created_same = await TrFollows.objects().update_or_create(in_=alice, out=bob)
            assert (created_same, same.id, same.in_, same.out, same.weight) == (False, existing.id, ALICE_P, BOB_P, 4)
            assert _edge_rows(await _stored(client, "tr_follows"), "weight") == [("tr_person:alice", "tr_person:bob", 4)]

            # No match: the create branch relates the endpoints of the criteria.
            new, created_new = await TrFollows.objects().update_or_create(in_=bob, out=alice, defaults={"weight": 2})
            assert created_new is True
            assert (new.in_, new.out, new.weight) == (BOB_P, ALICE_P, 2)
            assert await client.query("SELECT VALUE ->tr_follows->? FROM ONLY tr_person:bob;", {}) == [ALICE_P]
            assert len(await _stored(client, "tr_follows")) == 2

    @pytest.mark.asyncio
    async def test_update_or_create_refuses_endpoint_defaults_either_way(self) -> None:
        async with orm_client(*WRITE_GUARD_TABLES) as client:
            alice, bob, widget = await _nodes()

            with pytest.raises(ValueError, match=_endpoint_guard("update_or_create(defaults=)", "TrFollows")):
                await TrFollows.objects().update_or_create(in_=alice, out=bob, defaults={"out": widget})
            assert await _stored(client, "tr_follows") == []

            await alice.relate(TrFollows, bob)
            with pytest.raises(ValueError, match=_endpoint_guard("update_or_create(defaults=)", "TrFollows")):
                await TrFollows.objects().update_or_create(in_=alice, out=bob, defaults={"out": widget, "weight": 9})
            assert _edge_rows(await _stored(client, "tr_follows"), "weight") == [("tr_person:alice", "tr_person:bob", 1)]

    @pytest.mark.asyncio
    async def test_delete_and_refresh_edge(self) -> None:
        async with orm_client(*WRITE_GUARD_TABLES) as client:
            alice, _bob, widget = await _nodes()
            edge = await alice.relate(TrPurchased, widget, quantity=2)
            await client.query("UPDATE $rid SET quantity = 9;", {"rid": edge.id})

            await edge.refresh()
            assert (edge.in_, edge.out, edge.quantity) == (ALICE_P, WIDGET, 9)

            await edge.delete()
            assert await client.query("SELECT VALUE ->tr_purchased->? FROM ONLY tr_person:alice;", {}) == []
            assert await _stored(client, "tr_purchased") == []


class TestNativeIdempotencyE2E:
    """SurrealDB's own idempotency mechanisms for edges, through the ORM (spec §2.9, §8.14; both lines)."""

    @pytest.mark.asyncio
    async def test_deterministic_array_id_second_save_raises(self) -> None:
        async with orm_client(*EDGE_TABLES) as client:
            await _nodes()
            pair = RecordID("tr_follows", [ALICE_P, BOB_P])

            edge = await TrFollows(id=RecordID("tr_follows", [ALICE_P, BOB_P]), in_=ALICE_P, out=BOB_P, weight=1).save()
            assert edge.id == pair
            assert await client.query("SELECT VALUE ->tr_follows->? FROM ONLY tr_person:alice;", {}) == [BOB_P]

            # A raw INSERT RELATION would silently keep the stored edge on 2.7.0 and 3.1.5: the
            # guarded statement raises on every line.
            with pytest.raises(SurrealDbError, match="already exists"):
                await TrFollows(id=RecordID("tr_follows", [ALICE_P, BOB_P]), in_=ALICE_P, out=BOB_P, weight=2).save()

            stored = await _stored(client, "tr_follows")
            assert [(row["id"], row["weight"]) for row in stored] == [(pair, 1)]

    @pytest.mark.asyncio
    async def test_array_id_edge_update_and_delete_in_interactive_tx(self) -> None:
        """An array-id edge is updated and deleted inside an interactive transaction (WebSocket + 3.x).

        The id is bound (``$rid``), never written into the statement, where an array id is a parse
        error that fails the whole transaction.
        """
        async with orm_client(*EDGE_TABLES) as client:
            if not await _native_txn_supported():
                pytest.skip("requires SurrealDB 3.x native transactions")
            await _nodes()
            pair = RecordID("tr_follows", [ALICE_P, BOB_P])
            edge = await TrFollows(id=pair, in_=ALICE_P, out=BOB_P, weight=1).save()

            edge.weight = 5
            async with SurrealDBConnectionManager.transaction() as tx:
                assert tx.is_interactive
                await edge.update(tx=tx)
                seen = await TrFollows.objects(tx=tx).filter(in_=ALICE_P).exec()
                assert [(e.id, e.weight) for e in seen] == [(pair, 5)]
            stored = await _stored(client, "tr_follows")
            assert [(row["id"], row["in"], row["out"], row["weight"]) for row in stored] == [(pair, ALICE_P, BOB_P, 5)]
            assert await client.query("SELECT VALUE ->tr_follows->? FROM ONLY tr_person:alice;", {}) == [BOB_P]

            with pytest.raises(RuntimeError, match="roll back"):
                async with SurrealDBConnectionManager.transaction() as tx:
                    await edge.delete(tx=tx)
                    assert await TrFollows.objects(tx=tx).count() == 0
                    raise RuntimeError("roll back")
            assert [row["id"] for row in await _stored(client, "tr_follows")] == [pair]

            async with SurrealDBConnectionManager.transaction() as tx:
                await edge.delete(tx=tx)
            assert await _stored(client, "tr_follows") == []
            assert await client.query("SELECT VALUE ->tr_follows->? FROM ONLY tr_person:alice;", {}) == []

    @pytest.mark.asyncio
    async def test_array_id_edge_update_and_delete_in_buffered_tx(self) -> None:
        """An array-id edge is updated and deleted inside a buffered transaction (HTTP, both lines)."""
        async with http_orm_client(*EDGE_TABLES) as client:
            await _nodes()
            pair = RecordID("tr_follows", [ALICE_P, BOB_P])
            edge = await TrFollows(id=pair, in_=ALICE_P, out=BOB_P, weight=1).save()

            edge.weight = 5
            async with SurrealDBConnectionManager.transaction() as tx:
                assert not tx.is_interactive
                await edge.update(tx=tx)
                assert [row["weight"] for row in await _stored(client, "tr_follows")] == [1]  # queued, not run
            stored = await _stored(client, "tr_follows")
            assert [(row["id"], row["in"], row["out"], row["weight"]) for row in stored] == [(pair, ALICE_P, BOB_P, 5)]
            assert await client.query("SELECT VALUE ->tr_follows->? FROM ONLY tr_person:alice;", {}) == [BOB_P]

            async with SurrealDBConnectionManager.transaction() as tx:
                await edge.delete(tx=tx)
                assert [row["id"] for row in await _stored(client, "tr_follows")] == [pair]  # queued, not run
            assert await _stored(client, "tr_follows") == []
            assert await client.query("SELECT VALUE ->tr_follows->? FROM ONLY tr_person:alice;", {}) == []

    @pytest.mark.asyncio
    async def test_unique_in_out_index_second_relate_raises(self) -> None:
        async with orm_client(*EDGE_TABLES, "tr_follows_uq") as client:
            alice, bob, _widget = await _nodes()
            await client.query("DEFINE INDEX tr_follows_uq_pair ON tr_follows_uq FIELDS in, out UNIQUE;", {})

            first = await alice.relate(TrFollowsUq, bob)
            assert isinstance(first.id, RecordID)
            with pytest.raises(SurrealDbError, match="already contains"):
                await alice.relate(TrFollowsUq, bob)
            assert [row["id"] for row in await _stored(client, "tr_follows_uq")] == [first.id]

            # The index is directional: the reverse pair is another edge.
            reverse = await bob.relate(TrFollowsUq, alice)
            stored = await _stored(client, "tr_follows_uq")
            assert sorted(str(row["id"]) for row in stored) == sorted([str(first.id), str(reverse.id)])
            assert await client.query("SELECT VALUE ->tr_follows_uq->? FROM ONLY tr_person:bob;", {}) == [ALICE_P]


# ==================== Task 9 — live queries and change feeds on edge tables (E2E, both lines) ====================


class TrLiveEdge(BaseSurrealEdge):
    """Edge on ``tr_live`` that keeps undeclared fields (``extra="allow"``): a stray ``__`` would show."""

    model_config = SurrealConfigDict(table_name="tr_live", extra="allow")

    quantity: int = 1


class TrCfEdge(BaseSurrealEdge):
    """Edge on ``tr_cf`` (``TYPE RELATION CHANGEFEED 1h``) that keeps undeclared fields (``extra="allow"``)."""

    model_config = SurrealConfigDict(table_name="tr_cf", extra="allow")

    quantity: int = 1


LIVE_TABLES = (*EDGE_TABLES, "tr_live")
#: The database of ``tr_cf`` (``change_feed_database``): there ``since=0`` replays this module's changes only.
CF_DATABASE = "tr_edge_cf"
WIDGET_P = RecordID("tr_product", "widget")


async def _next_events(stream: Any, count: int) -> list[Any]:
    """The next ``count`` events of a live or change-feed stream, within 5 seconds."""
    events: list[Any] = []
    async with asyncio.timeout(5):
        while len(events) < count:
            events.append(await anext(stream))
    return events


def _assert_no_marker_in_instance(event: Any) -> None:
    """The typed instance never carries SurrealDB 2.x's ``"__"`` edge marker, even with ``extra="allow"``."""
    # ``warnings=False``: an unvalidated fallback instance may hold values of the wrong type.
    assert "__" not in event.instance.model_dump(warnings=False)
    assert "__" not in (event.instance.model_extra or {})


async def _first_event_of_filtered_live(lookup: Callable[[TrPerson], dict[str, Any]]) -> tuple[Any, TrPurchased]:
    """Watch ``TrPurchased.objects().filter(**lookup(alice)).live()``; create bob's edge, then alice's.

    Returns the first event received and alice's edge. Bob's edge is created first, so the first
    event would be his had the filter let it through, and none would come had the filter matched
    nothing: filtering is proven by order, never by waiting for nothing to arrive. Call it inside an
    ``orm_client(*EDGE_TABLES)`` block.
    """
    client = await SurrealDBConnectionManager.get_client()
    alice, bob, widget = await _nodes()
    # SurrealDB 3.x refuses a live query on an undefined table.
    await client.query("DEFINE TABLE tr_purchased TYPE RELATION;", {})
    async with asyncio.timeout(5):
        async with TrPurchased.objects().filter(**lookup(alice)).live() as stream:
            await bob.relate(TrPurchased, widget)
            mine = await alice.relate(TrPurchased, widget, data={"quantity": 2})
            event = await anext(stream)
    return event, mine


def _assert_alices_edge_created(event: Any, mine: TrPurchased) -> None:
    assert event.action is LiveAction.CREATE
    assert isinstance(event.instance, TrPurchased)
    assert event.validation_error is None
    assert (event.instance.in_, event.instance.out, event.instance.quantity) == (ALICE_P, WIDGET_P, 2)
    assert event.instance.id == mine.id
    assert event.record_id == str(mine.id)
    _assert_no_marker_in_instance(event)


class TrLooseNode(BaseSurrealModel):
    """Node that keeps undeclared fields: a ``__`` column there is the user's, never stripped."""

    model_config = SurrealConfigDict(table_name="tr_loose_node", extra="allow")

    id: str | None = None


EDGE_LIVE_ID = RecordID("tr_live", "e1")


def _marked_edge_record(**fields: Any) -> dict[str, Any]:
    """An edge record as SurrealDB 2.x sends it in a live notification or a change-feed update."""
    return {"__": True, "id": EDGE_LIVE_ID, "in": ALICE_P, "out": WIDGET_P, **fields}


class TestEdgeLiveHydration:
    """``live.hydrate()`` drops 2.x's ``"__": true`` for edge models only, and never touches ``raw`` (no server)."""

    @pytest.mark.parametrize("action", ["CREATE", "UPDATE"])
    def test_live_event_instance_drops_the_marker_raw_keeps_it(self, action: str) -> None:
        result = _marked_edge_record(quantity=2)
        event = to_change_event(
            TrLiveEdge, {"action": action, "record": EDGE_LIVE_ID, "result": result, "id": "lq"}, diff=False
        )

        assert isinstance(event.instance, TrLiveEdge)
        assert event.validation_error is None
        assert (event.instance.id, event.instance.in_, event.instance.quantity) == (EDGE_LIVE_ID, ALICE_P, 2)
        _assert_no_marker_in_instance(event)
        assert event.raw is result
        assert result == _marked_edge_record(quantity=2)

    def test_diff_create_root_value_is_stripped_for_the_instance_only(self) -> None:
        patches = [{"op": "replace", "path": "/", "value": _marked_edge_record(quantity=2)}]
        event = to_change_event(
            TrLiveEdge, {"action": "CREATE", "record": EDGE_LIVE_ID, "result": patches, "id": "lq"}, diff=True
        )

        assert event.instance.quantity == 2
        _assert_no_marker_in_instance(event)
        assert patches[0]["value"]["__"] is True

    def test_invalid_record_falls_back_without_the_marker(self) -> None:
        """The ``model_construct`` branch (a record that does not validate) drops it too."""
        result = _marked_edge_record(quantity="many")
        event = to_change_event(
            TrLiveEdge, {"action": "UPDATE", "record": EDGE_LIVE_ID, "result": result, "id": "lq"}, diff=False
        )

        assert isinstance(event.validation_error, ValidationError)
        assert isinstance(event.instance, TrLiveEdge)
        assert event.instance.quantity == "many"
        _assert_no_marker_in_instance(event)
        assert result["__"] is True

    def test_change_feed_update_entry(self) -> None:
        update = _marked_edge_record(quantity=3)
        [event] = feed_events(TrLiveEdge, {"changes": [{"update": update}], "versionstamp": 1})

        assert event.action is LiveAction.UPDATE
        assert (event.instance.id, event.instance.quantity) == (EDGE_LIVE_ID, 3)
        _assert_no_marker_in_instance(event)
        assert event.raw is update
        assert update["__"] is True

    def test_node_models_keep_a_double_underscore_column(self) -> None:
        rid = RecordID("tr_loose_node", "n1")
        event = to_change_event(
            TrLooseNode, {"action": "CREATE", "record": rid, "result": {"__": True, "id": rid}, "id": "lq"}, diff=False
        )

        assert event.instance.model_extra == {"__": True}

    def test_only_the_boolean_marker_is_dropped(self) -> None:
        """An edge column ``__`` holding anything but ``true`` is data, not the 2.x marker."""
        result = {**_marked_edge_record(), "__": "kept"}
        event = to_change_event(
            TrLiveEdge, {"action": "CREATE", "record": EDGE_LIVE_ID, "result": result, "id": "lq"}, diff=False
        )

        assert event.instance.model_extra == {"__": "kept"}


class TestEdgeLiveE2E:
    """Typed live queries and change feeds on edge tables (spec §2.5, §4.7; both lines).

    Measured on 2026-10-12: SurrealDB 2.7.0 adds ``"__": true`` to the edge record of a ``CREATE`` or
    ``UPDATE`` live notification (full and diff) and of a change-feed ``update`` entry; ``DELETE``
    notifications and change-feed ``delete`` entries never carry it; 3.3.3 never sends it. The typed
    instance drops it, ``raw`` keeps it as the server sent it.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "lookup",
        [lambda alice: {"in_": alice}, lambda _alice: {"in_": ALICE_P}],
        ids=["instance", "record_id"],
    )
    async def test_typed_live_on_edge_filtered_by_node(self, lookup: Callable[[TrPerson], dict[str, Any]]) -> None:
        async with orm_client(*EDGE_TABLES):
            event, mine = await _first_event_of_filtered_live(lookup)

        _assert_alices_edge_created(event, mine)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "lookup",
        [lambda _alice: {"in_": "tr_person:alice"}, lambda _alice: {"in___in": ["tr_person:alice"]}],
        ids=["exact", "in-list"],
    )
    async def test_typed_live_filter_with_string_endpoint(self, lookup: Callable[[TrPerson], dict[str, Any]]) -> None:
        """A ``"table:id"`` string on an endpoint column is read as the record id it names."""
        async with orm_client(*EDGE_TABLES):
            event, mine = await _first_event_of_filtered_live(lookup)

        _assert_alices_edge_created(event, mine)

    @pytest.mark.asyncio
    async def test_changes_on_edge_table(self) -> None:
        """``changes()`` on a ``TYPE RELATION CHANGEFEED`` table yields typed edges (in a database of its own)."""
        async with change_feed_database(CF_DATABASE) as client:
            alice, bob, widget = await _nodes()
            await client.query("DEFINE TABLE tr_cf TYPE RELATION CHANGEFEED 1h;", {})
            major = (await _server_version())[0]
            first = await _write(lambda: alice.relate(TrCfEdge, widget, data={"quantity": 2}))
            second = await _write(lambda: bob.relate(TrCfEdge, widget))
            await _write(lambda: first.delete())
            stream = TrCfEdge.objects().changes(since=0, poll_interval=0.05)
            try:
                events = await _next_events(stream, 3)
            finally:
                stream.stop()

        # The table definition is not a record change: skipped.
        assert [e.action for e in events] == [LiveAction.UPDATE, LiveAction.UPDATE, LiveAction.DELETE]
        assert [e.record_id for e in events] == [str(first.id), str(second.id), str(first.id)]
        created_alice, created_bob, deleted = events
        for event in (created_alice, created_bob):
            assert isinstance(event.instance, TrCfEdge)
            assert event.validation_error is None
            assert event.instance.id == event.raw["id"]
            # SurrealDB 2.x marks the edge record of a change-feed update entry; 3.x does not.
            assert ("__" in event.raw) is (major == 2)
            if major == 2:
                assert event.raw["__"] is True
        alice_edge = created_alice.instance
        assert (alice_edge.in_, alice_edge.out, alice_edge.quantity) == (ALICE_P, WIDGET_P, 2)
        assert (created_bob.instance.in_, created_bob.instance.quantity) == (BOB_P, 1)
        # A delete entry is ``{id}`` only, on both lines: an instance holding the id.
        assert deleted.raw == {"id": first.id}
        assert deleted.instance.id == first.id
        for event in events:
            _assert_no_marker_in_instance(event)

    @pytest.mark.asyncio
    async def test_edge_marker_scope(self) -> None:
        """``"__"`` never reaches the instance; ``raw`` keeps it on 2.x ``CREATE``/``UPDATE`` only."""
        async with orm_client(*LIVE_TABLES) as client:
            alice, _bob, widget = await _nodes()
            await client.query("DEFINE TABLE tr_live TYPE RELATION;", {})
            major = (await _server_version())[0]
            async with asyncio.timeout(5):
                async with TrLiveEdge.objects().live() as stream:
                    edge = await alice.relate(TrLiveEdge, widget, data={"quantity": 2})
                    await edge.merge(quantity=3)
                    await edge.delete()
                    created, updated, deleted = await _next_events(stream, 3)

        assert [e.action for e in (created, updated, deleted)] == [LiveAction.CREATE, LiveAction.UPDATE, LiveAction.DELETE]
        for event in (created, updated, deleted):
            assert isinstance(event.instance, TrLiveEdge)
            assert event.validation_error is None
            assert event.instance.id == edge.id
            assert (event.instance.in_, event.instance.out) == (ALICE_P, WIDGET_P)
            _assert_no_marker_in_instance(event)
        assert [e.instance.quantity for e in (created, updated, deleted)] == [2, 3, 3]
        for event in (created, updated):
            assert ("__" in event.raw) is (major == 2)
            if major == 2:
                assert event.raw["__"] is True
        assert "__" not in (deleted.raw or {})

    @pytest.mark.asyncio
    async def test_diff_mode_on_edge(self) -> None:
        """Diff mode: the ``CREATE`` root value carries the 2.x marker, the instance does not."""
        async with orm_client(*LIVE_TABLES) as client:
            alice, _bob, widget = await _nodes()
            await client.query("DEFINE TABLE tr_live TYPE RELATION;", {})
            major = (await _server_version())[0]
            async with asyncio.timeout(5):
                async with TrLiveEdge.objects().live(diff=True) as stream:
                    edge = await alice.relate(TrLiveEdge, widget, data={"quantity": 2})
                    await edge.merge(quantity=3)
                    await edge.delete()
                    created, updated, deleted = await _next_events(stream, 3)

        assert [e.action for e in (created, updated, deleted)] == [LiveAction.CREATE, LiveAction.UPDATE, LiveAction.DELETE]
        assert isinstance(created.instance, TrLiveEdge)
        assert (created.instance.in_, created.instance.out, created.instance.quantity) == (ALICE_P, WIDGET_P, 2)
        [root] = created.raw
        assert ("__" in root["value"]) is (major == 2)
        assert updated.changed_fields == ["quantity"]
        assert updated.instance.id == edge.id
        assert deleted.instance.id == edge.id
        for event in (created, updated, deleted):
            _assert_no_marker_in_instance(event)
