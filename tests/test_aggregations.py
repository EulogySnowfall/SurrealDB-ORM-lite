"""
Tests for aggregation features in SurrealDB-ORM-lite v0.3.0.

This module contains both unit tests (no database required) and
end-to-end tests (require a running SurrealDB instance) for:
- Aggregation classes (Count, Sum, Avg, Min, Max)
- QuerySet aggregation methods (count, sum, avg, min, max)
- GROUP BY support (values, annotate)
- exists() method
- raw_query() class method
"""

import os

import pytest
from pydantic import Field
from surrealdb import RecordID

from src import surreal_orm_lite
from src.surreal_orm_lite import Avg, Count, Max, Min, Sum

# =============================================================================
# Test Models
# =============================================================================


class Product(surreal_orm_lite.BaseSurrealModel):
    """Test model for aggregation tests."""

    id: str | RecordID | None = None
    name: str = Field(..., max_length=100)
    price: float = Field(..., ge=0)
    category: str = Field(..., max_length=50)
    in_stock: bool = True


class Order(surreal_orm_lite.BaseSurrealModel):
    """Test model for aggregation tests with amounts."""

    id: str | RecordID | None = None
    customer_id: str = Field(..., max_length=100)
    amount: float = Field(..., ge=0)
    status: str = Field(default="pending", max_length=50)


# =============================================================================
# Unit Tests - Aggregation Classes
# =============================================================================


class TestAggregationClasses:
    """Unit tests for aggregation class implementations."""

    def test_count_without_field(self) -> None:
        """Count without field should generate count()."""
        agg = Count()
        assert agg.to_sql() == "count()"
        assert agg.get_alias() == "count"

    def test_count_with_field(self) -> None:
        """Count with field should generate count(field)."""
        agg = Count("id")
        assert agg.to_sql() == "count(id)"
        assert agg.get_alias() == "count_id"

    def test_count_with_alias(self) -> None:
        """Count with custom alias."""
        agg = Count(alias="total_count")
        assert agg.to_sql() == "count()"
        assert agg.get_alias() == "total_count"

    def test_sum_requires_field(self) -> None:
        """Sum should require a field name."""
        with pytest.raises(ValueError) as exc:
            Sum("")  # type: ignore
        assert "Sum requires a field name" in str(exc.value)

    def test_sum_rejects_whitespace_field(self) -> None:
        """Sum should reject whitespace-only field names."""
        with pytest.raises(ValueError) as exc:
            Sum("   ")  # type: ignore
        assert "Sum requires a field name" in str(exc.value)

    def test_sum_to_sql(self) -> None:
        """Sum should generate math::sum(field)."""
        agg = Sum("amount")
        assert agg.to_sql() == "math::sum(amount)"
        assert agg.get_alias() == "sum_amount"

    def test_avg_requires_field(self) -> None:
        """Avg should require a field name."""
        with pytest.raises(ValueError) as exc:
            Avg("")  # type: ignore
        assert "Avg requires a field name" in str(exc.value)

    def test_avg_rejects_whitespace_field(self) -> None:
        """Avg should reject whitespace-only field names."""
        with pytest.raises(ValueError) as exc:
            Avg("   ")  # type: ignore
        assert "Avg requires a field name" in str(exc.value)

    def test_avg_to_sql(self) -> None:
        """Avg should generate math::mean(field)."""
        agg = Avg("price")
        assert agg.to_sql() == "math::mean(price)"
        assert agg.get_alias() == "avg_price"

    def test_min_requires_field(self) -> None:
        """Min should require a field name."""
        with pytest.raises(ValueError) as exc:
            Min("")  # type: ignore
        assert "Min requires a field name" in str(exc.value)

    def test_min_rejects_whitespace_field(self) -> None:
        """Min should reject whitespace-only field names."""
        with pytest.raises(ValueError) as exc:
            Min("   ")  # type: ignore
        assert "Min requires a field name" in str(exc.value)

    def test_min_to_sql(self) -> None:
        """Min should generate math::min(field)."""
        agg = Min("price")
        assert agg.to_sql() == "math::min(price)"
        assert agg.get_alias() == "min_price"

    def test_max_requires_field(self) -> None:
        """Max should require a field name."""
        with pytest.raises(ValueError) as exc:
            Max("")  # type: ignore
        assert "Max requires a field name" in str(exc.value)

    def test_max_rejects_whitespace_field(self) -> None:
        """Max should reject whitespace-only field names."""
        with pytest.raises(ValueError) as exc:
            Max("   ")  # type: ignore
        assert "Max requires a field name" in str(exc.value)

    def test_sum_rejects_sql_injection(self) -> None:
        """Sum should reject SQL injection attempts."""
        with pytest.raises(ValueError) as exc:
            Sum("amount); DROP TABLE--")
        assert "Invalid" in str(exc.value)

    def test_avg_rejects_sql_injection(self) -> None:
        """Avg should reject SQL injection attempts."""
        with pytest.raises(ValueError) as exc:
            Avg("price); DELETE FROM--")
        assert "Invalid" in str(exc.value)

    def test_count_rejects_sql_injection(self) -> None:
        """Count with field should reject SQL injection attempts."""
        with pytest.raises(ValueError) as exc:
            Count("id; DROP TABLE--")
        assert "Invalid" in str(exc.value)

    def test_max_to_sql(self) -> None:
        """Max should generate math::max(field)."""
        agg = Max("price")
        assert agg.to_sql() == "math::max(price)"
        assert agg.get_alias() == "max_price"


class TestQuerySetAggregationMethods:
    """Unit tests for QuerySet aggregation method setup."""

    def test_values_sets_group_by_fields(self) -> None:
        """values() should set _group_by_fields."""
        qs = Product.objects().values("category")
        assert qs._group_by_fields == ["category"]

    def test_annotate_validates_aggregation_type(self) -> None:
        """annotate() should raise TypeError for non-Aggregation values."""
        with pytest.raises(TypeError) as exc:
            Product.objects().values("category").annotate(count="not_an_aggregation")  # type: ignore
        assert "must be an Aggregation instance" in str(exc.value)
        assert "count" in str(exc.value)

    async def test_sum_requires_valid_field(self) -> None:
        """sum() should raise ValueError for empty field name."""
        with pytest.raises(ValueError) as exc:
            await Product.objects().sum("")
        assert "cannot be empty" in str(exc.value)

    async def test_sum_requires_non_whitespace_field(self) -> None:
        """sum() should raise ValueError for whitespace-only field name."""
        with pytest.raises(ValueError) as exc:
            await Product.objects().sum("   ")
        assert "cannot be empty" in str(exc.value)

    async def test_sum_rejects_invalid_field_name(self) -> None:
        """sum() should reject SQL injection attempts."""
        with pytest.raises(ValueError) as exc:
            await Product.objects().sum("amount); DROP TABLE--")
        assert "Invalid" in str(exc.value)

    async def test_avg_requires_valid_field(self) -> None:
        """avg() should raise ValueError for empty field name."""
        with pytest.raises(ValueError) as exc:
            await Product.objects().avg("")
        assert "cannot be empty" in str(exc.value)

    async def test_min_requires_valid_field(self) -> None:
        """min() should raise ValueError for empty field name."""
        with pytest.raises(ValueError) as exc:
            await Product.objects().min("")
        assert "cannot be empty" in str(exc.value)

    async def test_max_requires_valid_field(self) -> None:
        """max() should raise ValueError for empty field name."""
        with pytest.raises(ValueError) as exc:
            await Product.objects().max("")
        assert "cannot be empty" in str(exc.value)

    def test_values_rejects_invalid_field_name(self) -> None:
        """values() should reject SQL injection attempts."""
        with pytest.raises(ValueError) as exc:
            Product.objects().values("status; DROP TABLE--")
        assert "Invalid" in str(exc.value)

    def test_annotate_rejects_invalid_alias(self) -> None:
        """annotate() should reject invalid alias names."""
        with pytest.raises(ValueError) as exc:
            Product.objects().values("category").annotate(**{"count; DROP": Count()})
        assert "Invalid alias" in str(exc.value)

    def test_values_multiple_fields(self) -> None:
        """values() with multiple fields."""
        qs = Product.objects().values("category", "in_stock")
        assert qs._group_by_fields == ["category", "in_stock"]

    def test_annotate_sets_annotations(self) -> None:
        """annotate() should set _annotations."""
        qs = Product.objects().values("category").annotate(count=Count())
        assert "count" in qs._annotations
        assert isinstance(qs._annotations["count"], Count)

    def test_annotate_multiple_aggregations(self) -> None:
        """annotate() with multiple aggregations."""
        qs = (
            Product.objects()
            .values("category")
            .annotate(
                count=Count(),
                avg_price=Avg("price"),
                max_price=Max("price"),
            )
        )
        assert len(qs._annotations) == 3
        assert "count" in qs._annotations
        assert "avg_price" in qs._annotations
        assert "max_price" in qs._annotations

    def test_compile_aggregation_query(self) -> None:
        """_compile_aggregation_query should generate correct SQL."""
        qs = Product.objects()
        query = qs._compile_aggregation_query("count()")
        assert "SELECT count() FROM Product" in query
        assert "GROUP ALL" in query

    def test_compile_aggregation_query_with_filter(self) -> None:
        """_compile_aggregation_query with filters."""
        qs = Product.objects().filter(in_stock=True)
        query = qs._compile_aggregation_query("count()")
        assert "WHERE in_stock = True" in query
        assert "GROUP ALL" in query

    def test_compile_group_by_query(self) -> None:
        """_compile_group_by_query should generate correct SQL."""
        qs = Product.objects().values("category").annotate(count=Count())
        query = qs._compile_group_by_query()
        assert "SELECT category, count() AS count FROM Product" in query
        assert "GROUP BY category" in query

    def test_compile_group_by_query_multiple_fields(self) -> None:
        """_compile_group_by_query with multiple group fields and annotations."""
        qs = (
            Product.objects()
            .values("category", "in_stock")
            .annotate(
                count=Count(),
                avg_price=Avg("price"),
            )
        )
        query = qs._compile_group_by_query()
        assert "category" in query
        assert "in_stock" in query
        assert "count() AS count" in query
        assert "math::mean(price) AS avg_price" in query
        assert "GROUP BY category, in_stock" in query


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
    """Setup SurrealDB connection for e2e tests."""
    surreal_orm_lite.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )


@pytest.fixture
async def product_data() -> None:
    """Create test product data."""
    # Clean up first
    await Product.objects().delete_table()

    # Create test products
    await Product(id="p1", name="Laptop", price=999.99, category="Electronics", in_stock=True).save()
    await Product(id="p2", name="Mouse", price=29.99, category="Electronics", in_stock=True).save()
    await Product(id="p3", name="Keyboard", price=79.99, category="Electronics", in_stock=False).save()
    await Product(id="p4", name="Desk", price=299.99, category="Furniture", in_stock=True).save()
    await Product(id="p5", name="Chair", price=199.99, category="Furniture", in_stock=True).save()

    yield

    # Cleanup
    await Product.objects().delete_table()


@pytest.fixture
async def order_data() -> None:
    """Create test order data."""
    # Clean up first
    await Order.objects().delete_table()

    # Create test orders
    await Order(id="o1", customer_id="c1", amount=100.0, status="completed").save()
    await Order(id="o2", customer_id="c1", amount=200.0, status="completed").save()
    await Order(id="o3", customer_id="c2", amount=150.0, status="pending").save()
    await Order(id="o4", customer_id="c2", amount=50.0, status="completed").save()
    await Order(id="o5", customer_id="c3", amount=300.0, status="pending").save()

    yield

    # Cleanup
    await Order.objects().delete_table()


class TestCountE2E:
    """End-to-end tests for count() method."""

    async def test_count_all(self, product_data: None) -> None:
        """Count all records in a table."""
        count = await Product.objects().count()
        assert count == 5

    async def test_count_with_filter(self, product_data: None) -> None:
        """Count records matching a filter."""
        count = await Product.objects().filter(category="Electronics").count()
        assert count == 3

    async def test_count_with_multiple_filters(self, product_data: None) -> None:
        """Count records matching multiple filters."""
        count = await Product.objects().filter(category="Electronics", in_stock=True).count()
        assert count == 2

    async def test_count_empty_result(self, product_data: None) -> None:
        """Count should return 0 for no matching records."""
        count = await Product.objects().filter(category="NonExistent").count()
        assert count == 0


class TestSumE2E:
    """End-to-end tests for sum() method."""

    async def test_sum_all(self, order_data: None) -> None:
        """Sum all values in a field."""
        total = await Order.objects().sum("amount")
        assert total == 800.0  # 100 + 200 + 150 + 50 + 300

    async def test_sum_with_filter(self, order_data: None) -> None:
        """Sum values matching a filter."""
        total = await Order.objects().filter(status="completed").sum("amount")
        assert total == 350.0  # 100 + 200 + 50

    async def test_sum_empty_result(self, order_data: None) -> None:
        """Sum should return 0 for no matching records."""
        total = await Order.objects().filter(status="cancelled").sum("amount")
        assert total == 0


class TestAvgE2E:
    """End-to-end tests for avg() method."""

    async def test_avg_all(self, order_data: None) -> None:
        """Average of all values in a field."""
        avg = await Order.objects().avg("amount")
        assert avg == 160.0  # 800 / 5

    async def test_avg_with_filter(self, order_data: None) -> None:
        """Average of values matching a filter."""
        avg = await Order.objects().filter(customer_id="c1").avg("amount")
        assert avg == 150.0  # (100 + 200) / 2

    async def test_avg_empty_result(self, order_data: None) -> None:
        """Avg should return 0.0 for no matching records."""
        avg = await Order.objects().filter(status="cancelled").avg("amount")
        assert avg == 0.0


class TestMinMaxE2E:
    """End-to-end tests for min() and max() methods."""

    async def test_min_all(self, product_data: None) -> None:
        """Find minimum value in a field."""
        min_price = await Product.objects().min("price")
        assert min_price == 29.99

    async def test_max_all(self, product_data: None) -> None:
        """Find maximum value in a field."""
        max_price = await Product.objects().max("price")
        assert max_price == 999.99

    async def test_min_with_filter(self, product_data: None) -> None:
        """Find minimum value matching a filter."""
        min_price = await Product.objects().filter(category="Furniture").min("price")
        assert min_price == 199.99

    async def test_max_with_filter(self, product_data: None) -> None:
        """Find maximum value matching a filter."""
        max_price = await Product.objects().filter(category="Furniture").max("price")
        assert max_price == 299.99

    async def test_min_empty_result(self, product_data: None) -> None:
        """Min should return None for no matching records."""
        min_price = await Product.objects().filter(category="NonExistent").min("price")
        assert min_price is None

    async def test_max_empty_result(self, product_data: None) -> None:
        """Max should return None for no matching records."""
        max_price = await Product.objects().filter(category="NonExistent").max("price")
        assert max_price is None


class TestGroupByE2E:
    """End-to-end tests for GROUP BY with values() and annotate()."""

    async def test_group_by_count(self, product_data: None) -> None:
        """Group by a field and count."""
        results = await Product.objects().values("category").annotate(count=Count()).exec()
        assert len(results) == 2

        # Convert to dict for easier testing
        results_dict = {r["category"]: r["count"] for r in results}
        assert results_dict["Electronics"] == 3
        assert results_dict["Furniture"] == 2

    async def test_group_by_multiple_aggregations(self, product_data: None) -> None:
        """Group by with multiple aggregations."""
        results = (
            await Product.objects()
            .values("category")
            .annotate(
                count=Count(),
                avg_price=Avg("price"),
                min_price=Min("price"),
                max_price=Max("price"),
            )
            .exec()
        )
        assert len(results) == 2

        for r in results:
            assert "count" in r
            assert "avg_price" in r
            assert "min_price" in r
            assert "max_price" in r

    async def test_group_by_with_filter(self, product_data: None) -> None:
        """Group by with filter applied first."""
        results = await Product.objects().filter(in_stock=True).values("category").annotate(count=Count()).exec()
        # Electronics has 2 in stock, Furniture has 2 in stock
        results_dict = {r["category"]: r["count"] for r in results}
        assert results_dict["Electronics"] == 2
        assert results_dict["Furniture"] == 2

    async def test_group_by_order_amounts(self, order_data: None) -> None:
        """Group orders by customer and sum amounts."""
        results = (
            await Order.objects()
            .values("customer_id")
            .annotate(
                total=Sum("amount"),
                order_count=Count(),
            )
            .exec()
        )
        assert len(results) == 3

        results_dict = {r["customer_id"]: r for r in results}
        assert results_dict["c1"]["total"] == 300.0
        assert results_dict["c1"]["order_count"] == 2
        assert results_dict["c2"]["total"] == 200.0
        assert results_dict["c3"]["total"] == 300.0


class TestExistsE2E:
    """End-to-end tests for exists() method."""

    async def test_exists_returns_true(self, product_data: None) -> None:
        """exists() should return True when records match."""
        exists = await Product.objects().filter(category="Electronics").exists()
        assert exists is True

    async def test_exists_returns_false(self, product_data: None) -> None:
        """exists() should return False when no records match."""
        exists = await Product.objects().filter(category="NonExistent").exists()
        assert exists is False

    async def test_exists_without_filter(self, product_data: None) -> None:
        """exists() without filter should return True if table has data."""
        exists = await Product.objects().exists()
        assert exists is True

    async def test_exists_empty_table(self, order_data: None) -> None:
        """exists() should return False for empty table."""
        # Clean up all orders - the fixture creates them first
        await Order.objects().delete_table()
        exists = await Order.objects().exists()
        assert exists is False


class TestRawQueryE2E:
    """End-to-end tests for raw_query() class method."""

    async def test_raw_query_simple(self, product_data: None) -> None:
        """Execute a simple raw query."""
        results = await Product.raw_query("SELECT * FROM Product WHERE category = 'Electronics'")
        assert len(results) == 3
        for r in results:
            assert r.category == "Electronics"

    async def test_raw_query_with_variables(self, product_data: None) -> None:
        """Execute a raw query with variables."""
        results = await Product.raw_query(
            "SELECT * FROM Product WHERE price > $min_price",
            variables={"min_price": 100},
        )
        assert len(results) == 3  # Laptop, Desk, Chair
        for r in results:
            assert r.price > 100

    async def test_raw_query_aggregation(self, product_data: None) -> None:
        """Execute a raw aggregation query."""
        results = await Product.raw_query("SELECT count() FROM Product GROUP ALL")
        assert len(results) == 1
        assert results[0]["count"] == 5

    async def test_raw_query_complex(self, order_data: None) -> None:
        """Execute a complex raw query."""
        results = await Order.raw_query(
            """
            SELECT customer_id, math::sum(amount) AS total
            FROM Order
            WHERE status = $status
            GROUP BY customer_id
            """,
            variables={"status": "completed"},
        )
        assert len(results) == 2  # c1 and c2 have completed orders
