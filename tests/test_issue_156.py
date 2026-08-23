"""Regression tests for the seven findings of issue #156 (v0.14.3).

Every finding was reproduced against live SurrealDB 3.2.4 **and** 2.6.5 before the fix, so
each test here is a genuine red-then-green guard rather than a description of current
behaviour. The unit tests cover the compilation-level fixes; the ``…E2E`` classes drive a
real server, because three of the findings (#1, #4, #6) are only visible in what the
database ends up holding.
"""

import contextlib
import os
import warnings

import pytest
from pydantic import ConfigDict, Field
from surrealdb import RecordID

from src import surreal_orm_lite
from src.surreal_orm_lite import Var
from src.surreal_orm_lite.exceptions import SurrealDbError
from src.surreal_orm_lite.utils import build_filter_condition, remove_quotes_for_variables

SURREALDB_HOST = os.environ.get("SURREALDB_HOST", "localhost")
SURREALDB_PORT = os.environ.get("SURREALDB_PORT", "8000")
SURREALDB_URL = f"http://{SURREALDB_HOST}:{SURREALDB_PORT}"


class Issue156Model(surreal_orm_lite.BaseSurrealModel):
    id: str | RecordID | None = None
    name: str = Field(...)
    age: int = 0


class Issue156Extra(surreal_orm_lite.BaseSurrealModel):
    """A model that opts into undeclared fields — they must survive both write paths."""

    model_config = ConfigDict(extra="allow")

    id: str | RecordID | None = None
    name: str = Field(...)


@pytest.fixture(scope="module", autouse=True)
def setup_surrealdb() -> None:
    surreal_orm_lite.SurrealDBConnectionManager.set_connection(SURREALDB_URL, "root", "root", "ns", "db")


# ==================== #2 — literal strings starting with "$" ====================


class TestDollarStrings:
    def test_bare_dollar_string_is_still_a_variable_reference(self) -> None:
        """Documented behaviour, kept for compatibility — but it now warns."""
        with pytest.deprecated_call():
            sql, variables, _ = build_filter_condition("name", "exact", "$admin", 0)
        assert sql == "name = $admin"
        assert variables == {}

    def test_double_dollar_escapes_to_a_literal(self) -> None:
        sql, variables, counter = build_filter_condition("name", "exact", "$$admin", 0)
        assert sql == "name = $_f0"
        assert variables == {"_f0": "$admin"}
        assert counter == 1

    def test_var_compiles_to_a_reference(self) -> None:
        sql, variables, counter = build_filter_condition("age", "gte", Var("min_age"), 0)
        assert sql == "age >= $min_age"
        assert variables == {}
        assert counter == 0

    def test_var_rejects_a_malformed_name(self) -> None:
        with pytest.raises(ValueError, match="variable name"):
            Var("50 bill")

    def test_var_accepts_a_leading_dollar(self) -> None:
        assert Var("$min_age").name == "min_age"


# ==================== #3 — the unquoting helper must not touch user SQL ====================


class TestRawSqlIsVerbatim:
    def test_helper_still_unquotes_compiled_queries(self) -> None:
        assert remove_quotes_for_variables("SELECT * FROM U WHERE a = '$x';") == "SELECT * FROM U WHERE a = $x;"

    async def test_raw_query_keeps_a_quoted_dollar_literal(self) -> None:
        """``raw_query`` is user-authored SQL: it goes to the server byte for byte."""
        sent: dict[str, str] = {}

        class FakeClient:
            async def query(self, query: str, variables: dict) -> list:
                sent["query"] = query
                return []

        async def fake_get_client() -> FakeClient:
            return FakeClient()

        original = surreal_orm_lite.SurrealDBConnectionManager.get_client
        surreal_orm_lite.SurrealDBConnectionManager.get_client = fake_get_client  # type: ignore[assignment]
        try:
            await Issue156Model.raw_query("SELECT * FROM Issue156Model WHERE name = '$admin';")
        finally:
            surreal_orm_lite.SurrealDBConnectionManager.get_client = original  # type: ignore[assignment]

        assert sent["query"] == "SELECT * FROM Issue156Model WHERE name = '$admin';"


# ==================== #5 — multi-statement raw queries ====================


class TestMultiStatementWarning:
    async def _run(self, query: str) -> list:
        class FakeClient:
            async def query(self, query: str, variables: dict) -> list:
                return []

        async def fake_get_client() -> FakeClient:
            return FakeClient()

        original = surreal_orm_lite.SurrealDBConnectionManager.get_client
        surreal_orm_lite.SurrealDBConnectionManager.get_client = fake_get_client  # type: ignore[assignment]
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                await Issue156Model.raw_query(query)
            return list(caught)
        finally:
            surreal_orm_lite.SurrealDBConnectionManager.get_client = original  # type: ignore[assignment]

    async def test_two_statements_warn(self) -> None:
        caught = await self._run("SELECT * FROM Issue156Model; SELECT age FROM Issue156Model;")
        assert any("only the first" in str(w.message) for w in caught), [str(w.message) for w in caught]

    async def test_one_statement_is_silent(self) -> None:
        assert await self._run("SELECT * FROM Issue156Model;") == []

    async def test_a_semicolon_inside_a_string_is_not_a_statement(self) -> None:
        assert await self._run("SELECT * FROM Issue156Model WHERE name = 'a;b';") == []


# ==================== E2E ====================


class TestIssue156E2E:
    async def _reset(self) -> None:
        client = await surreal_orm_lite.SurrealDBConnectionManager.get_client()
        for table in ("Issue156Model", "Issue156Extra"):
            # The table may never have been created; dropping it is best-effort.
            with contextlib.suppress(Exception):
                await client.query(f"REMOVE TABLE {table};")

    # ---- #1 first() must not mutate the queryset ----

    async def test_first_does_not_limit_the_queryset(self) -> None:
        await self._reset()
        for i, name in enumerate(("a", "b", "c"), start=1):
            await Issue156Model(id=str(i), name=name, age=i).save()

        qs = Issue156Model.objects().filter(age__gte=0)
        first = await qs.first()
        assert first.name == "a"

        assert len(await qs.exec()) == 3, "first() left the queryset limited to one row"
        assert qs._limit is None

    # ---- #2 the escape works against a real server ----

    async def test_filter_matches_a_literal_dollar_value(self) -> None:
        await self._reset()
        await Issue156Model(id="1", name="$admin", age=1).save()

        found = await Issue156Model.objects().filter(name="$$admin").exec()
        assert [row.name for row in found] == ["$admin"]

    # ---- #3 raw SQL reaches the server verbatim ----

    async def test_raw_query_finds_a_quoted_dollar_literal(self) -> None:
        await self._reset()
        await Issue156Model(id="1", name="$admin", age=1).save()

        found = await Issue156Model.raw_query("SELECT * FROM Issue156Model WHERE name = '$admin';")
        assert [row.name for row in found] == ["$admin"]  # type: ignore[union-attr]

    # ---- #4 created reflects reality ----

    async def test_upsert_reports_created_truthfully(self) -> None:
        await self._reset()

        model = Issue156Model(id="1", name="a", age=30)
        _, created = await model._do_upsert()
        assert created is True, "a record that did not exist must report created=True"

        again = Issue156Model(id="1", name="b", age=31)
        _, created = await again._do_upsert()
        assert created is False, "replacing an existing record is not a creation"
        assert again.name == "b" and again.age == 31, "the instance must still sync with the server row"

    # ---- #6 undeclared keys never reach the database ----

    async def test_update_or_create_rejects_undeclared_keys_on_create(self) -> None:
        await self._reset()
        with pytest.raises(SurrealDbError, match="nope"):
            await Issue156Model.objects().update_or_create(defaults={"age": 1, "nope": "x"}, name="ghost")

    async def test_update_or_create_rejects_undeclared_keys_on_update(self) -> None:
        await self._reset()
        await Issue156Model(id="1", name="bob", age=1).save()

        with pytest.raises(SurrealDbError, match="nope"):
            await Issue156Model.objects().update_or_create(defaults={"age": 2, "nope": "x"}, name="bob")

        client = await surreal_orm_lite.SurrealDBConnectionManager.get_client()
        rows = await client.query("SELECT * FROM Issue156Model WHERE name = 'bob';")
        assert "nope" not in rows[0], "an undeclared key must never be written to the row"

    async def test_get_or_create_rejects_undeclared_keys(self) -> None:
        await self._reset()
        with pytest.raises(SurrealDbError, match="nope"):
            await Issue156Model.objects().get_or_create(defaults={"nope": "x"}, name="ghost")

    async def test_extra_allow_models_keep_their_extra_fields(self) -> None:
        await self._reset()
        obj, created = await Issue156Extra.objects().update_or_create(defaults={"nickname": "bo"}, name="bob")
        assert created is True
        assert getattr(obj, "nickname", None) == "bo"

    # ---- #7 one round-trip ----

    async def test_get_related_uses_a_single_query(self) -> None:
        await self._reset()
        client = await surreal_orm_lite.SurrealDBConnectionManager.get_client()
        with contextlib.suppress(Exception):
            await client.query("REMOVE TABLE Issue156Edge;")

        source = Issue156Model(id="alpha", name="a", age=1)
        await source.save()
        await Issue156Model(id="beta", name="b", age=2).save()
        await source.relate("Issue156Edge", "Issue156Model:beta")

        calls: list[str] = []
        original_query = client.query

        async def counting_query(query: str, variables: dict | None = None) -> object:
            calls.append(query)
            return await original_query(query, variables or {})

        client.query = counting_query  # type: ignore[method-assign]
        try:
            related = await source.get_related("Issue156Edge", model_class=Issue156Model)
        finally:
            client.query = original_query  # type: ignore[method-assign]

        assert [row.name for row in related] == ["b"]
        assert len(calls) == 1, f"expected a single round-trip, got {len(calls)}: {calls}"


# ==================== Found while fixing #7: string ids that look numeric ====================


class TestNumericStringIdE2E:
    """``save(id="1")`` stores the *string* record id ``"1"``.

    Unquoted, ``Model:1`` is the **integer** record id in SurrealQL, so every raw-SQL path
    built from ``_get_thing()`` (relate, get_related, traverse, remove_relation) used to
    address a record the ORM had never written — silently returning nothing.
    """

    async def test_relations_work_for_a_numeric_looking_id(self) -> None:
        client = await surreal_orm_lite.SurrealDBConnectionManager.get_client()
        for table in ("Issue156Model", "Issue156Edge"):
            with contextlib.suppress(Exception):
                await client.query(f"REMOVE TABLE {table};")

        source = Issue156Model(id="1", name="a", age=1)
        await source.save()
        await Issue156Model(id="2", name="b", age=2).save()

        assert source._get_thing() == "Issue156Model:`1`"

        # A plain "table:id" string must resolve to the same record as the model's own
        # _get_thing(): no hand-written backticks required (review follow-up).
        await source.relate("Issue156Edge", "Issue156Model:2")
        related = await source.get_related("Issue156Edge", model_class=Issue156Model)
        assert [row.name for row in related] == ["b"]


class TestUpsertSignalE2E:
    """``created`` is only visible to users through ``post_save`` — assert that surface too."""

    async def test_post_save_reports_created_truthfully(self) -> None:
        client = await surreal_orm_lite.SurrealDBConnectionManager.get_client()
        with contextlib.suppress(Exception):
            await client.query("REMOVE TABLE Issue156Model;")

        seen: list[tuple[str, bool]] = []

        @surreal_orm_lite.post_save.connect(Issue156Model)
        async def _record(sender, instance, created, **kwargs):  # type: ignore[no-untyped-def]
            seen.append((instance.name, created))

        try:
            await Issue156Model(id="tim", name="a", age=1).upsert()
            await Issue156Model(id="tim", name="b", age=2).upsert()
        finally:
            surreal_orm_lite.post_save.clear(Issue156Model)

        assert seen == [("a", True), ("b", False)]


# ==================== Review follow-ups on the #156 fixes ====================


class TestRecordIdQuoting:
    """The backtick quoting of #7 must key off the id's Python *type*, not its text shape."""

    def test_integer_id_stays_an_integer_record_id(self) -> None:
        class IntIdModel(surreal_orm_lite.BaseSurrealModel):
            id: int
            name: str = "x"

        model = IntIdModel(id=5)
        # Quoting a genuine int id would send relations to a record save() never wrote.
        assert model._get_thing() == "IntIdModel:5"
        assert str(model._record_id()) == model._get_thing()

    def test_string_id_that_looks_numeric_is_quoted(self) -> None:
        assert Issue156Model(id="5", name="a")._get_thing() == "Issue156Model:`5`"

    def test_target_string_is_quoted_like_a_model_id(self) -> None:
        source = Issue156Model(id="5", name="a")
        assert Issue156Model._resolve_target_thing("Issue156Model:5") == source._get_thing()

    def test_already_quoted_target_is_kept_verbatim(self) -> None:
        assert Issue156Model._resolve_target_thing("Issue156Model:`5`") == "Issue156Model:`5`"

    def test_record_id_target_keeps_the_integer_form(self) -> None:
        assert Issue156Model._resolve_target_thing(RecordID("Issue156Model", 5)) == "Issue156Model:5"

    def test_a_backtick_in_an_id_is_refused(self) -> None:
        with pytest.raises(ValueError):
            Issue156Model(id="a`b", name="a")._get_thing()


class TestWarningsReachUserCode:
    """A fixed stacklevel pointed inside the ORM, so Python's default filters hid the warning."""

    def test_deprecation_warning_points_at_the_caller(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            Issue156Model.objects().filter(name="$admin")._build_where()
        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert deprecations, [str(w.message) for w in caught]
        assert deprecations[0].filename == __file__

    async def test_queryset_query_warns_on_multiple_statements(self) -> None:
        class FakeClient:
            async def query(self, query: str, variables: dict) -> list:
                return []

        async def fake_get_client() -> FakeClient:
            return FakeClient()

        original = surreal_orm_lite.SurrealDBConnectionManager.get_client
        surreal_orm_lite.SurrealDBConnectionManager.get_client = fake_get_client  # type: ignore[assignment]
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                await Issue156Model.objects().query("SELECT * FROM Issue156Model; SELECT age FROM Issue156Model;")
        finally:
            surreal_orm_lite.SurrealDBConnectionManager.get_client = original  # type: ignore[assignment]
        assert any("only the first" in str(w.message) for w in caught), [str(w.message) for w in caught]


class TestSplitStatementsComments:
    async def test_double_slash_comment_is_not_a_statement(self) -> None:
        from src.surreal_orm_lite.utils import split_statements

        assert len(split_statements("SELECT * FROM M; // note; end")) == 1
        # An apostrophe inside a // comment must not open a string state and swallow the
        # rest of the query.
        assert len(split_statements("SELECT * FROM M; // don't\nSELECT 1; SELECT 2;")) == 3


class TestCriteriaPayloadInterpretation:
    """``_criteria_payload`` must read values the same way the WHERE builder does."""

    def test_double_dollar_is_unescaped_in_the_write_payload(self) -> None:
        payload = surreal_orm_lite.QuerySet(Issue156Model)._criteria_payload({"name": "$$admin"})
        assert payload == {"name": "$admin"}

    def test_variable_reference_cannot_be_written(self) -> None:
        qs = surreal_orm_lite.QuerySet(Issue156Model)
        with pytest.raises(SurrealDbError, match="query-variable reference"):
            qs._criteria_payload({"name": Var("admin")})
        with pytest.raises(SurrealDbError, match="query-variable reference"):
            qs._criteria_payload({"name": "$admin"})


class TestDollarEscapeRoundTripE2E:
    async def test_get_or_create_converges_on_an_escaped_literal(self) -> None:
        client = await surreal_orm_lite.SurrealDBConnectionManager.get_client()
        with contextlib.suppress(Exception):
            await client.query("REMOVE TABLE Issue156Model;")

        # `id` stays out of the criteria: it is a RecordID on the row, so an equality
        # lookup on the plain string would never match — unrelated to the escape itself.
        first, created_first = await Issue156Model.objects().get_or_create(defaults={"id": "dollar", "age": 1}, name="$$admin")
        second, created_second = await Issue156Model.objects().get_or_create(
            defaults={"id": "dollar", "age": 1}, name="$$admin"
        )
        assert created_first is True
        assert created_second is False, "the escaped literal must round-trip, not duplicate"
        assert first.name == "$admin"
        assert len(await Issue156Model.objects().all()) == 1
