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

E2E tables are dedicated (``tr_*``), dropped on entry and exit by ``orm_client``; nodes are created
inside the ``async with orm_client(...)`` block. Same behaviour on SurrealDB 2.x and 3.x.
"""

import asyncio

import pytest
from pydantic import Field, ValidationError
from surrealdb import RecordID

from surreal_orm_lite import BaseSurrealEdge, BaseSurrealModel, Q, SurrealConfigDict, Var
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
