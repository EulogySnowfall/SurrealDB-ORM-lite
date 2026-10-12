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

E2E tables are dedicated (``tr_*``), dropped on entry and exit by ``orm_client``; nodes are created
inside the ``async with orm_client(...)`` block. Same behaviour on SurrealDB 2.x and 3.x.
"""

import asyncio
import contextlib
import os
import re
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from pydantic import Field, ValidationError
from surrealdb import AlreadyExistsError, NotFoundError, RecordID

from surreal_orm_lite import (
    AuthenticatedUserMixin,
    BaseSurrealEdge,
    BaseSurrealModel,
    Q,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    SurrealDbError,
    SurrealDbNotFoundError,
    Var,
    post_save,
    pre_save,
)
from surreal_orm_lite.enum import LiveAction
from surreal_orm_lite.query_set import QuerySet
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


class TestEdgePermissionsE2E:
    @pytest.mark.asyncio
    async def test_save_refused_by_permissions_raises(self) -> None:
        """The server answers a refused ``INSERT RELATION`` with no row and no error (spec §2.7)."""
        saved: list[Any] = []

        @post_save.connect(TrPermEdge)
        async def on_post(sender: Any, instance: Any, created: bool, **kwargs: Any) -> None:
            saved.append(instance.id)

        _connect_root_ws()
        try:
            root = await SurrealDBConnectionManager.get_client()
            with contextlib.suppress(Exception):
                await root.query("REMOVE TABLE tr_perm;", {})
            await root.query(
                "DEFINE TABLE tr_perm TYPE RELATION PERMISSIONS FOR select FULL, FOR create WHERE quantity > 1;", {}
            )
            await TrEdgeUser.define_access()
            await TrEdgeUser.signup(email=f"{uuid4().hex}@example.test", password="s3cret-passphrase", bind=True)

            a, w = RecordID("tr_person", "a"), RecordID("tr_product", "w")
            with pytest.raises(SurrealDbError, match="no record returned"):
                await TrPermEdge(in_=a, out=w, quantity=1).save()  # auto id: insert_relation()
            with pytest.raises(SurrealDbError, match="no record returned"):
                await TrPermEdge(id="refused", in_=a, out=w, quantity=1).save()  # explicit id: the guard
            assert saved == []

            allowed = await TrPermEdge(in_=a, out=w, quantity=2).save()  # the same user may create this one
            assert isinstance(allowed.id, RecordID)
            assert saved == [allowed.id]

            await SurrealDBConnectionManager.unset_connection()
            _connect_root_ws()
            root = await SurrealDBConnectionManager.get_client()
            assert await root.query("SELECT VALUE quantity FROM tr_perm;", {}) == [2]
        finally:
            post_save.disconnect(on_post, TrPermEdge)
            await SurrealDBConnectionManager.unset_connection()
            _connect_root_ws()
            with contextlib.suppress(Exception):
                root = await SurrealDBConnectionManager.get_client()
                await root.query("REMOVE ACCESS tr_edge_acct ON DATABASE;", {})
                await root.query("REMOVE TABLE tr_perm;", {})
                await root.query("REMOVE TABLE tr_edge_user;", {})
            await SurrealDBConnectionManager.unset_connection()
