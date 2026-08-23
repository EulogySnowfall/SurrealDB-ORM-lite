"""Regression tests for issue #159 — record-id lookups (v0.14.4).

A record's ``id`` column holds a ``RecordID``, but the ``WHERE`` builder bound whatever
Python value the caller passed. ``filter(id="d")`` therefore compiled to ``id = $_f0`` with
``_f0 = "d"``, a plain string, and SurrealDB does not consider a ``RecordID`` equal to the
string form of its identifier — so the lookup matched nothing, with no error and no warning.

Every case below was reproduced against live SurrealDB 3.2.4 **and** 2.6.5 before the fix.
"""

import contextlib
import os

import pytest
from surrealdb import RecordID

from src import surreal_orm_lite
from src.surreal_orm_lite import BaseSurrealModel, Q, Var
from src.surreal_orm_lite.exceptions import SurrealDbNotFoundError
from src.surreal_orm_lite.utils import build_filter_condition, coerce_record_id

SURREALDB_HOST = os.environ.get("SURREALDB_HOST", "localhost")
SURREALDB_PORT = os.environ.get("SURREALDB_PORT", "8000")
SURREALDB_URL = f"http://{SURREALDB_HOST}:{SURREALDB_PORT}"


class Issue159Str(BaseSurrealModel):
    id: str | RecordID | None = None
    name: str
    age: int = 0


class Issue159Int(BaseSurrealModel):
    id: int | RecordID | None = None
    name: str
    age: int = 0


class Issue159Pk(BaseSurrealModel):
    """A model whose identity is an aliased column.

    The stored row carries *both* ``code`` (an ordinary string column) and ``id`` (the
    RecordID built from it). Only the latter may be coerced — coercing the alias would break
    a lookup that has always worked.
    """

    model_config = {"primary_key": "code"}

    code: str
    name: str


@pytest.fixture(scope="module", autouse=True)
def setup_surrealdb() -> None:
    surreal_orm_lite.SurrealDBConnectionManager.set_connection(SURREALDB_URL, "root", "root", "ns", "db")


async def _reset(*tables: str) -> None:
    client = await surreal_orm_lite.SurrealDBConnectionManager.get_client()
    for table in tables:
        with contextlib.suppress(Exception):
            await client.query(f"REMOVE TABLE {table};")


# ==================== The helper ====================


class TestCoerceRecordId:
    """``coerce_record_id`` is the single rule every id lookup goes through."""

    def test_record_id_passes_through(self) -> None:
        rid = RecordID("T", "d")
        assert coerce_record_id(rid, "T") is rid

    def test_int_stays_an_integer_record_id(self) -> None:
        result = coerce_record_id(5, "T")
        assert result == RecordID("T", 5)
        assert result.id == 5 and not isinstance(result.id, str)

    def test_str_becomes_a_string_record_id(self) -> None:
        result = coerce_record_id("5", "T")
        assert result.id == "5"

    def test_backticks_are_stripped(self) -> None:
        assert coerce_record_id("`5`", "T").id == "5"

    def test_matching_table_prefix_is_unwrapped(self) -> None:
        assert coerce_record_id("T:d", "T") == RecordID("T", "d")

    def test_backticked_id_behind_a_table_prefix_is_unwrapped(self) -> None:
        assert coerce_record_id("T:`5`", "T").id == "5"

    def test_foreign_table_prefix_is_kept_as_a_literal_id(self) -> None:
        """``"Other:d"`` may genuinely BE the string id, so it is not unwrapped."""
        assert coerce_record_id("Other:d", "T").id == "Other:d"

    def test_bool_is_refused(self) -> None:
        with pytest.raises(ValueError, match="boolean"):
            coerce_record_id(True, "T")


# ==================== Compilation ====================


class TestIdFilterCompilation:
    def test_exact_id_binds_a_record_id(self) -> None:
        sql, variables, _ = build_filter_condition("id", "exact", "d", 0, record_table="T")
        assert sql == "id = $_f0"
        assert variables == {"_f0": RecordID("T", "d")}

    def test_in_coerces_every_element(self) -> None:
        _, variables, _ = build_filter_condition("id", "in", ["a", 2], 0, record_table="T")
        assert variables == {"_f0": [RecordID("T", "a"), RecordID("T", 2)]}

    def test_comparison_lookups_are_coerced(self) -> None:
        _, variables, _ = build_filter_condition("id", "gt", "d", 0, record_table="T")
        assert variables == {"_f0": RecordID("T", "d")}

    def test_other_columns_are_untouched(self) -> None:
        _, variables, _ = build_filter_condition("name", "exact", "d", 0, record_table="T")
        assert variables == {"_f0": "d"}

    def test_isnull_is_untouched(self) -> None:
        sql, variables, _ = build_filter_condition("id", "isnull", True, 0, record_table="T")
        assert sql == "id IS NULL"
        assert variables == {}

    def test_variable_reference_is_not_coerced(self) -> None:
        """The value lives on the server; the ORM has nothing to coerce."""
        sql, variables, _ = build_filter_condition("id", "exact", Var("rid"), 0, record_table="T")
        assert sql == "id = $rid"
        assert variables == {}

    @pytest.mark.parametrize(
        "lookup",
        ["contains", "not_contains", "containsall", "containsany", "startswith", "endswith", "like", "ilike", "match", "regex"],
    )
    def test_string_lookups_on_the_id_column_are_refused(self, lookup: str) -> None:
        with pytest.raises(ValueError, match="record id"):
            build_filter_condition("id", lookup, "abc", 0, record_table="T")

    def test_string_lookups_still_work_on_other_columns(self) -> None:
        sql, _, _ = build_filter_condition("name", "contains", "abc", 0, record_table="T")
        assert sql == "name CONTAINS $_f0"

    def test_no_table_context_means_no_coercion(self) -> None:
        """A bare ``Q`` compiled outside a QuerySet keeps its previous behaviour."""
        _, variables, _ = build_filter_condition("id", "exact", "d", 0)
        assert variables == {"_f0": "d"}

    def test_queryset_supplies_the_table_to_q_objects(self) -> None:
        qs = Issue159Str.objects()
        qs.filter(Q(id="d") | Q(id="e"))
        _, variables = qs._build_where()
        assert sorted(str(v) for v in variables.values()) == ["Issue159Str:d", "Issue159Str:e"]


# ==================== E2E ====================


class TestIdLookupsE2E:
    async def test_string_id_is_found(self) -> None:
        await _reset("Issue159Str")
        await Issue159Str(id="d", name="a").save()

        assert len(await Issue159Str.objects().filter(id="d").exec()) == 1
        assert len(await Issue159Str.objects().filter(id="Issue159Str:d").exec()) == 1
        assert len(await Issue159Str.objects().filter(id=RecordID("Issue159Str", "d")).exec()) == 1

    async def test_numeric_string_id_is_not_confused_with_an_integer_one(self) -> None:
        await _reset("Issue159Str")
        await Issue159Str(id="5", name="a").save()

        assert len(await Issue159Str.objects().filter(id="5").exec()) == 1
        # The integer record id 5 is a *different* record and must not match.
        assert await Issue159Str.objects().filter(id=5).exec() == []

    async def test_integer_id_is_found(self) -> None:
        await _reset("Issue159Int")
        await Issue159Int(id=5, name="a").save()

        assert len(await Issue159Int.objects().filter(id=5).exec()) == 1
        assert await Issue159Int.objects().filter(id="5").exec() == []

    async def test_get_accepts_an_integer_id(self) -> None:
        """``get()`` built ``RecordID(table, str(id))``, so an int id was unreachable too."""
        await _reset("Issue159Int")
        await Issue159Int(id=5, name="a").save()

        assert (await Issue159Int.objects().get(5)).name == "a"
        with pytest.raises(SurrealDbNotFoundError):
            await Issue159Int.objects().get("5")

    async def test_in_lookup(self) -> None:
        await _reset("Issue159Str")
        await Issue159Str(id="a", name="a").save()
        await Issue159Str(id="b", name="b").save()
        await Issue159Str(id="c", name="c").save()

        rows = await Issue159Str.objects().filter(id__in=["a", "c"]).exec()
        assert sorted(row.name for row in rows) == ["a", "c"]

    async def test_q_objects_and_negation(self) -> None:
        await _reset("Issue159Str")
        await Issue159Str(id="a", name="a").save()
        await Issue159Str(id="b", name="b").save()

        rows = await Issue159Str.objects().filter(Q(id="a") | Q(id="b")).exec()
        assert sorted(row.name for row in rows) == ["a", "b"]
        assert [row.name for row in await Issue159Str.objects().filter(~Q(id="a")).exec()] == ["b"]

    async def test_aliased_primary_key_column_is_untouched(self) -> None:
        """``code`` is an ordinary column on the row; only ``id`` is a RecordID."""
        await _reset("Issue159Pk")
        await Issue159Pk(code="abc", name="p").save()

        assert len(await Issue159Pk.objects().filter(code="abc").exec()) == 1
        assert len(await Issue159Pk.objects().filter(id="abc").exec()) == 1

    async def test_get_or_create_converges_on_an_id_criterion(self) -> None:
        """The bug that motivated the issue: the lookup never matched, so every call
        took the create path and the second one hit ``already exists``."""
        await _reset("Issue159Str")

        first, created_first = await Issue159Str.objects().get_or_create(defaults={"name": "a"}, id="d")
        second, created_second = await Issue159Str.objects().get_or_create(defaults={"name": "a"}, id="d")

        assert created_first is True
        assert created_second is False
        assert str(first.id).endswith("d")
        assert len(await Issue159Str.objects().all()) == 1

    async def test_update_or_create_on_an_id_criterion(self) -> None:
        await _reset("Issue159Str")

        await Issue159Str.objects().update_or_create(defaults={"name": "a", "age": 1}, id="d")
        obj, created = await Issue159Str.objects().update_or_create(defaults={"age": 2}, id="d")

        assert created is False
        assert obj.age == 2 and obj.name == "a"
        assert len(await Issue159Str.objects().all()) == 1

    async def test_string_lookup_on_id_raises_instead_of_returning_nothing(self) -> None:
        """Compilation is lazy, so the error surfaces when the query is built."""
        with pytest.raises(ValueError, match="record id"):
            await Issue159Str.objects().filter(id__contains="d").exec()
