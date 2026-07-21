"""
Tests for v0.5.0 features: Q objects, parameterized filters, new lookups,
-field ordering, and bulk operations.
"""

import os
from collections.abc import AsyncGenerator

import pytest
from pydantic import Field
from surrealdb import RecordID

from src import surreal_orm_lite
from src.surreal_orm_lite.q import Q
from src.surreal_orm_lite.utils import build_filter_condition, parse_lookup

# =============================================================================
# Test Models
# =============================================================================


class User(surreal_orm_lite.BaseSurrealModel):
    id: str | RecordID | None = None
    name: str = Field(..., max_length=100)
    age: int = Field(..., ge=0)
    role: str = Field(default="user", max_length=50)
    email: str = Field(default="", max_length=200)
    tags: list[str] = Field(default_factory=list)


# =============================================================================
# Unit Tests - Q Objects
# =============================================================================


class TestQObject:
    """Unit tests for Q class."""

    def test_q_simple_filter(self) -> None:
        """Q with kwargs generates correct SQL."""
        q = Q(name="alice")
        sql, vars_, counter = q.to_sql(0)
        assert sql == "name = $_f0"
        assert vars_ == {"_f0": "alice"}
        assert counter == 1

    def test_q_multiple_filters(self) -> None:
        """Q with multiple kwargs ANDs them together."""
        q = Q(name="alice", age=25)
        sql, vars_, counter = q.to_sql(0)
        assert "AND" in sql
        assert "_f0" in vars_
        assert "_f1" in vars_
        assert counter == 2

    def test_q_or(self) -> None:
        """Q objects combined with | produce OR."""
        q = Q(name="alice") | Q(name="bob")
        sql, vars_, counter = q.to_sql(0)
        assert "OR" in sql
        assert vars_["_f0"] == "alice"
        assert vars_["_f1"] == "bob"

    def test_q_and(self) -> None:
        """Q objects combined with & produce AND."""
        q = Q(name="alice") & Q(age=25)
        sql, vars_, counter = q.to_sql(0)
        assert "AND" in sql
        assert vars_["_f0"] == "alice"
        assert vars_["_f1"] == 25

    def test_q_not(self) -> None:
        """~Q produces NOT."""
        q = ~Q(name="alice")
        sql, vars_, counter = q.to_sql(0)
        assert sql.startswith("NOT (")
        assert vars_["_f0"] == "alice"

    def test_q_complex_combination(self) -> None:
        """Complex Q expression with nesting."""
        q = Q(age__gte=18) & (Q(role="admin") | Q(role="mod"))
        sql, vars_, counter = q.to_sql(0)
        assert "AND" in sql
        assert "OR" in sql
        assert counter == 3

    def test_q_empty(self) -> None:
        """Empty Q returns empty SQL."""
        q = Q()
        sql, vars_, counter = q.to_sql(0)
        assert sql == ""
        assert vars_ == {}

    def test_q_counter_continuity(self) -> None:
        """Counter continues across Q objects."""
        q = Q(name="alice")
        sql, vars_, counter = q.to_sql(5)
        assert "_f5" in vars_
        assert counter == 6

    def test_q_repr(self) -> None:
        """Q repr is readable."""
        q = Q(name="alice")
        r = repr(q)
        assert "Q(" in r
        assert "filters=" in r

    def test_q_repr_or(self) -> None:
        """Q repr for OR is readable."""
        q = Q(name="alice") | Q(name="bob")
        r = repr(q)
        assert "connector=OR" in r

    def test_q_repr_negated(self) -> None:
        """Q repr for NOT is readable."""
        q = ~Q(name="alice")
        r = repr(q)
        assert "negated=True" in r

    def test_q_double_negate(self) -> None:
        """~~Q cancels out negation."""
        q = ~~Q(name="alice")
        assert q.negated is False


# =============================================================================
# Unit Tests - parse_lookup and build_filter_condition
# =============================================================================


class TestParseLookup:
    def test_exact(self) -> None:
        assert parse_lookup("name") == ("name", "exact")

    def test_with_lookup(self) -> None:
        assert parse_lookup("age__gt") == ("age", "gt")

    def test_with_double_underscore_field(self) -> None:
        # Only splits on first __
        assert parse_lookup("some__nested__field") == ("some", "nested__field")


class TestBuildFilterCondition:
    def test_exact(self) -> None:
        sql, vars_, counter = build_filter_condition("name", "exact", "alice", 0)
        assert sql == "name = $_f0"
        assert vars_ == {"_f0": "alice"}
        assert counter == 1

    def test_gt(self) -> None:
        sql, vars_, counter = build_filter_condition("age", "gt", 18, 0)
        assert sql == "age > $_f0"
        assert vars_ == {"_f0": 18}

    def test_in(self) -> None:
        sql, vars_, counter = build_filter_condition("name", "in", ["a", "b"], 0)
        assert sql == "name IN $_f0"
        assert vars_ == {"_f0": ["a", "b"]}

    def test_not_in(self) -> None:
        sql, vars_, counter = build_filter_condition("name", "not_in", ["a", "b"], 0)
        assert sql == "name NOT IN $_f0"
        assert vars_ == {"_f0": ["a", "b"]}

    def test_isnull_true(self) -> None:
        sql, vars_, counter = build_filter_condition("email", "isnull", True, 0)
        assert sql == "email IS NULL"
        assert vars_ == {}
        assert counter == 0  # No variable consumed

    def test_isnull_false(self) -> None:
        sql, vars_, counter = build_filter_condition("email", "isnull", False, 0)
        assert sql == "email IS NOT NULL"
        assert vars_ == {}

    def test_containsall(self) -> None:
        sql, vars_, counter = build_filter_condition("tags", "containsall", ["a", "b"], 0)
        assert sql == "tags CONTAINSALL $_f0"
        assert vars_ == {"_f0": ["a", "b"]}

    def test_containsany(self) -> None:
        sql, vars_, counter = build_filter_condition("tags", "containsany", ["a", "b"], 0)
        assert sql == "tags CONTAINSANY $_f0"
        assert vars_ == {"_f0": ["a", "b"]}

    def test_not_contains(self) -> None:
        sql, vars_, counter = build_filter_condition("tags", "not_contains", "x", 0)
        assert sql == "tags CONTAINSNOT $_f0"
        assert vars_ == {"_f0": "x"}

    def test_unsupported_lookup(self) -> None:
        with pytest.raises(ValueError, match="Unsupported lookup type"):
            build_filter_condition("name", "invalid_lookup", "x", 0)

    def test_invalid_field_name(self) -> None:
        with pytest.raises(ValueError, match="Invalid"):
            build_filter_condition("name; DROP TABLE--", "exact", "x", 0)

    def test_like(self) -> None:
        sql, vars_, counter = build_filter_condition("name", "like", "%alice%", 0)
        assert sql == "name LIKE $_f0"
        assert vars_ == {"_f0": "%alice%"}

    def test_startswith(self) -> None:
        sql, vars_, counter = build_filter_condition("name", "startswith", "al", 0)
        assert sql == "name STARTSWITH $_f0"
        assert vars_ == {"_f0": "al"}

    def test_endswith(self) -> None:
        sql, vars_, counter = build_filter_condition("name", "endswith", "ce", 0)
        assert sql == "name ENDSWITH $_f0"
        assert vars_ == {"_f0": "ce"}

    def test_counter_increments(self) -> None:
        _, _, c = build_filter_condition("a", "exact", 1, 3)
        assert c == 4


# =============================================================================
# Unit Tests - QuerySet parameterized filters
# =============================================================================


class TestQuerySetParameterized:
    def test_compile_query_simple_filter(self) -> None:
        """Simple filter produces parameterized WHERE."""
        qs = User.objects().filter(name="Test")
        query, variables = qs._compile_query()
        assert "WHERE name = $_f0" in query
        assert variables["_f0"] == "Test"

    def test_compile_query_multiple_filters(self) -> None:
        """Multiple filters all parameterized."""
        qs = User.objects().filter(name="Test", age__gt=18)
        query, variables = qs._compile_query()
        assert "$_f0" in query
        assert "$_f1" in query
        assert len(variables) == 2

    def test_compile_query_with_q_object(self) -> None:
        """Q object in filter produces parameterized WHERE."""
        qs = User.objects().filter(Q(name="alice") | Q(name="bob"))
        query, variables = qs._compile_query()
        assert "OR" in query
        assert variables["_f0"] == "alice"
        assert variables["_f1"] == "bob"

    def test_compile_query_mixed_q_and_kwargs(self) -> None:
        """Mix of Q and kwargs both parameterized."""
        qs = User.objects().filter(Q(role="admin") | Q(role="mod"), age__gte=18)
        query, variables = qs._compile_query()
        assert "OR" in query
        assert "AND" in query
        assert len(variables) == 3

    def test_filter_rejects_non_q_positional_args(self) -> None:
        """filter() rejects non-Q positional arguments."""
        with pytest.raises(TypeError, match="must be Q objects"):
            User.objects().filter("not a Q object")  # type: ignore

    def test_compile_query_no_filters(self) -> None:
        """No filters produces no WHERE clause."""
        qs = User.objects()
        query, variables = qs._compile_query()
        assert "WHERE" not in query
        assert variables == {}

    def test_compile_query_with_limit_offset(self) -> None:
        """Limit and offset in query."""
        qs = User.objects().filter(age__gt=18).limit(10).offset(5)
        query, variables = qs._compile_query()
        assert "LIMIT 10" in query
        assert "START 5" in query

    def test_user_variables_merged(self) -> None:
        """User-set variables merged with filter variables."""
        qs = User.objects().filter(age__lte="$max_age").variables(max_age=50)
        query, variables = qs._compile_query()
        assert "max_age" in variables
        assert variables["max_age"] == 50


# =============================================================================
# Unit Tests - order_by with -field shorthand
# =============================================================================


class TestOrderBy:
    def test_ascending(self) -> None:
        qs = User.objects().order_by("name")
        assert qs._order_by == "name ASC"

    def test_descending_shorthand(self) -> None:
        qs = User.objects().order_by("-name")
        assert qs._order_by == "name DESC"

    def test_multiple_fields(self) -> None:
        qs = User.objects().order_by("-age", "name")
        assert qs._order_by == "age DESC, name ASC"

    def test_backward_compat_with_orderby_enum(self) -> None:
        qs = User.objects().order_by("name", surreal_orm_lite.OrderBy.DESC)
        assert qs._order_by == "name DESC"

    def test_order_by_in_query(self) -> None:
        qs = User.objects().order_by("-age", "name")
        query, _ = qs._compile_query()
        assert "ORDER BY age DESC, name ASC" in query

    def test_order_by_rejects_invalid_field(self) -> None:
        with pytest.raises(ValueError, match="Invalid"):
            User.objects().order_by("name; DROP TABLE--")

    def test_order_by_rejects_invalid_desc_field(self) -> None:
        with pytest.raises(ValueError, match="Invalid"):
            User.objects().order_by("-name; DROP TABLE--")


# =============================================================================
# Unit Tests - Q export
# =============================================================================


class TestExports:
    def test_q_exported(self) -> None:
        """Q is available from the main package."""
        assert hasattr(surreal_orm_lite, "Q")
        assert surreal_orm_lite.Q is Q

    def test_version_bumped(self) -> None:
        """Version is 0.9.0."""
        assert surreal_orm_lite.__version__ == "0.11.15"


# =============================================================================
# End-to-End Tests - Require SurrealDB
# =============================================================================

SURREALDB_HOST = os.environ.get("SURREALDB_HOST", "localhost")
SURREALDB_PORT = os.environ.get("SURREALDB_PORT", "8000")
SURREALDB_URL = f"http://{SURREALDB_HOST}:{SURREALDB_PORT}"
SURREALDB_USER = "root"
SURREALDB_PASS = "root"
SURREALDB_NAMESPACE = "ns"
SURREALDB_DATABASE = "db"


@pytest.fixture(scope="module", autouse=True)
def setup_surrealdb() -> None:
    surreal_orm_lite.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )


@pytest.fixture
async def user_data() -> AsyncGenerator[None, None]:
    """Create test user data."""
    await User.objects().delete_table()

    await User(
        id="u1",
        name="Alice",
        age=30,
        role="admin",
        email="alice@test.com",
        tags=["python", "rust"],
    ).save()
    await User(id="u2", name="Bob", age=25, role="mod", email="bob@test.com", tags=["python"]).save()
    await User(
        id="u3",
        name="Charlie",
        age=35,
        role="user",
        email="charlie@test.com",
        tags=["rust", "go"],
    ).save()
    await User(
        id="u4",
        name="Diana",
        age=22,
        role="user",
        email="diana@test.com",
        tags=["python", "go"],
    ).save()
    await User(id="u5", name="Eve", age=28, role="admin", email="eve@test.com", tags=["rust"]).save()

    yield

    await User.objects().delete_table()


class TestQObjectE2E:
    """E2E tests for Q objects."""

    async def test_q_or_filter(self, user_data: None) -> None:
        """Q OR filter returns correct results."""
        results = await User.objects().filter(Q(name="Alice") | Q(name="Bob")).exec()
        assert len(results) == 2
        names = {r.name for r in results}
        assert names == {"Alice", "Bob"}

    async def test_q_and_filter(self, user_data: None) -> None:
        """Q AND filter returns correct results."""
        results = await User.objects().filter(Q(role="admin") & Q(age__gte=30)).exec()
        assert len(results) == 1
        assert results[0].name == "Alice"

    async def test_q_not_filter(self, user_data: None) -> None:
        """Q NOT filter returns correct results."""
        results = await User.objects().filter(~Q(role="user")).exec()
        assert len(results) == 3
        for r in results:
            assert r.role != "user"

    async def test_q_complex(self, user_data: None) -> None:
        """Complex Q expression works correctly."""
        # admins OR (users older than 25)
        results = await User.objects().filter(Q(role="admin") | (Q(role="user") & Q(age__gt=25))).exec()
        assert len(results) == 3  # Alice(admin), Eve(admin), Charlie(user,35)
        names = {r.name for r in results}
        assert names == {"Alice", "Eve", "Charlie"}

    async def test_q_mixed_with_kwargs(self, user_data: None) -> None:
        """Q objects mixed with kwargs."""
        results = await User.objects().filter(Q(role="admin") | Q(role="mod"), age__gte=25).exec()
        assert len(results) == 3  # Alice(admin,30), Bob(mod,25), Eve(admin,28)
        names = {r.name for r in results}
        assert names == {"Alice", "Bob", "Eve"}


class TestNewLookupsE2E:
    """E2E tests for new lookup operators."""

    async def test_not_in(self, user_data: None) -> None:
        """NOT IN lookup."""
        results = await User.objects().filter(role__not_in=["admin", "mod"]).exec()
        assert len(results) == 2
        for r in results:
            assert r.role == "user"

    async def test_containsall(self, user_data: None) -> None:
        """CONTAINSALL lookup."""
        results = await User.objects().filter(tags__containsall=["python", "rust"]).exec()
        assert len(results) == 1
        assert results[0].name == "Alice"

    async def test_containsany(self, user_data: None) -> None:
        """CONTAINSANY lookup."""
        results = await User.objects().filter(tags__containsany=["go"]).exec()
        assert len(results) == 2
        names = {r.name for r in results}
        assert names == {"Charlie", "Diana"}


class TestOrderByE2E:
    """E2E tests for -field ordering shorthand."""

    async def test_desc_shorthand(self, user_data: None) -> None:
        """-field produces DESC ordering."""
        results = await User.objects().order_by("-age").exec()
        assert results[0].name == "Charlie"  # age 35
        assert results[-1].name == "Diana"  # age 22

    async def test_multi_field_order(self, user_data: None) -> None:
        """Multiple fields with mixed ASC/DESC."""
        results = await User.objects().order_by("role", "-age").exec()
        # admin: Alice(30), Eve(28); mod: Bob(25); user: Charlie(35), Diana(22)
        admin_results = [r for r in results if r.role == "admin"]
        assert admin_results[0].name == "Alice"  # older admin first
        assert admin_results[1].name == "Eve"

    async def test_asc_shorthand(self, user_data: None) -> None:
        """Field without - produces ASC ordering."""
        results = await User.objects().order_by("age").exec()
        assert results[0].name == "Diana"  # age 22
        assert results[-1].name == "Charlie"  # age 35


class TestBulkCreateE2E:
    """E2E tests for bulk_create."""

    async def test_bulk_create(self, user_data: None) -> None:
        """bulk_create creates multiple records."""
        new_users = [
            User(name="Frank", age=40, role="user"),
            User(name="Grace", age=33, role="mod"),
            User(name="Hank", age=29, role="user"),
        ]
        created = await User.objects().bulk_create(new_users)
        assert len(created) == 3
        names = {u.name for u in created}
        assert names == {"Frank", "Grace", "Hank"}

        # Verify they're in the DB
        total = await User.objects().count()
        assert total == 8  # 5 original + 3 new

    async def test_bulk_create_empty(self, user_data: None) -> None:
        """bulk_create with empty list returns empty."""
        result = await User.objects().bulk_create([])
        assert result == []

    async def test_bulk_create_with_ids(self, user_data: None) -> None:
        """bulk_create with explicit IDs."""
        new_users = [
            User(id="bc1", name="Ivan", age=50, role="admin"),
            User(id="bc2", name="Judy", age=45, role="user"),
        ]
        created = await User.objects().bulk_create(new_users)
        assert len(created) == 2

        fetched = await User.objects().get("bc1")
        assert fetched.name == "Ivan"


class TestBulkUpdateE2E:
    """E2E tests for bulk_update."""

    async def test_bulk_update(self, user_data: None) -> None:
        """bulk_update updates matching records."""
        count = await User.objects().filter(role="user").bulk_update(role="member")
        assert count == 2  # Charlie and Diana

        # Verify the update
        members = await User.objects().filter(role="member").exec()
        assert len(members) == 2

    async def test_bulk_update_no_kwargs(self, user_data: None) -> None:
        """bulk_update with no kwargs returns 0."""
        count = await User.objects().bulk_update()
        assert count == 0

    async def test_bulk_update_all(self, user_data: None) -> None:
        """bulk_update without filter updates all records."""
        count = await User.objects().bulk_update(email="updated@test.com")
        assert count == 5

        results = await User.objects().all()
        for r in results:
            assert r.email == "updated@test.com"


class TestBulkDeleteE2E:
    """E2E tests for bulk_delete."""

    async def test_bulk_delete(self, user_data: None) -> None:
        """bulk_delete deletes matching records."""
        count = await User.objects().filter(role="user").bulk_delete()
        assert count == 2  # Charlie and Diana

        remaining = await User.objects().count()
        assert remaining == 3

    async def test_bulk_delete_no_match(self, user_data: None) -> None:
        """bulk_delete with no matches returns 0."""
        count = await User.objects().filter(role="nonexistent").bulk_delete()
        assert count == 0

    async def test_bulk_delete_all(self, user_data: None) -> None:
        """bulk_delete without filter deletes all records."""
        count = await User.objects().bulk_delete()
        assert count == 5

        remaining = await User.objects().count()
        assert remaining == 0


class TestParameterizedFiltersE2E:
    """E2E tests for parameterized filter security."""

    async def test_filters_are_parameterized(self, user_data: None) -> None:
        """Verify filters work with parameterized variables."""
        results = await User.objects().filter(name="Alice", age__gte=25).exec()
        assert len(results) == 1
        assert results[0].name == "Alice"

    async def test_in_filter_parameterized(self, user_data: None) -> None:
        """IN filter with parameterized list."""
        results = await User.objects().filter(name__in=["Alice", "Bob", "Eve"]).exec()
        assert len(results) == 3

    async def test_isnull_filter(self, user_data: None) -> None:
        """isnull filter generates IS NULL / IS NOT NULL."""
        # All users have emails, so isnull=True should return 0
        results = await User.objects().filter(email__isnull=False).exec()
        assert len(results) == 5
