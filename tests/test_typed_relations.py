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
``remove_relation()`` target without ``:`` is a bare record id, bound verbatim and compared as a
string with ``record::id(out)``: it matches that id in any table, never an integer id.

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
    Var,
    computed,
    post_save,
    pre_save,
)
from surreal_orm_lite.enum import LiveAction
from surreal_orm_lite.query_set import QuerySet
from surreal_orm_lite.transaction import BufferedTransaction
from surreal_orm_lite.utils import build_filter_condition, coerce_endpoint, parse_lookup
from tests.conftest import orm_client

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
        with pytest.warns(DeprecationWarning, match=r"\$x"):
            sql, variables, _ = build_filter_condition("in", "exact", "$x", 0, "e", endpoint_columns=ENDPOINTS)
        assert (sql, variables) == ("in = $x", {})

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
            "RELATE tr_person:alice->tr_follows->tr_person:bob SET weight = $t0__rd0;",
            "RELATE tr_person:alice->tr_follows->tr_person:carol;",
        ]
        assert tx.variables == {"t0__rd0": 2}
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


GRAPH_TABLES = (*EDGE_TABLES, "tr_likes")
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
    async def test_positional_calls_match_keyword_forms(self, recording: _RecordingClient) -> None:
        """R2: each positional call records exactly what its keyword form records."""
        alice, bob = TrPerson(id="alice"), TrPerson(id="bob")
        pairs: list[tuple[Callable[[], Awaitable[Any]], Callable[[], Awaitable[Any]], list[tuple[Any, ...]]]] = [
            (
                lambda: alice.relate("tr_follows", bob, True),
                lambda: alice.relate("tr_follows", bob, reverse=True),
                [("RELATE tr_person:bob->tr_follows->tr_person:alice;", {})],
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
            "RELATE tr_person:bob->tr_follows->tr_person:alice SET since = $t1__rd0;",
        ]
        assert tx.variables == {"t0_data": {"in": BOB_P, "out": ALICE_P, "weight": 2}, "t1__rd0": "2025-01-01"}
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
        assert calls == [("RELATE tr_person:alice->tr_follows->tr_person:bob SET since = $_rd0;", {"_rd0": "2025-01-01"})]

        calls, _ = await _recorded(recording, alice.relate("tr_follows", bob, reverse=True, since="2025-01-01"))
        assert calls == [("RELATE tr_person:bob->tr_follows->tr_person:alice SET since = $_rd0;", {"_rd0": "2025-01-01"})]

        calls, _ = await _recorded(recording, alice.relate("tr_follows", bob, since="2025-01-01", data={"weight": 2}))
        assert calls == [
            (
                "RELATE tr_person:alice->tr_follows->tr_person:bob SET weight = $_rd0, since = $_rd1;",
                {"_rd0": 2, "_rd1": "2025-01-01"},
            )
        ]

        calls, _ = await _recorded(recording, alice.relate("tr_follows", bob, data={"reverse": True}))
        assert calls == [("RELATE tr_person:alice->tr_follows->tr_person:bob SET reverse = $_rd0;", {"_rd0": True})]

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
