"""
Aggregation classes for SurrealDB-ORM-lite.

This module provides Django-style aggregation functions that can be used
with QuerySet to perform aggregate calculations on database fields.

Example:
    ```python
    from surreal_orm_lite import Count, Sum, Avg, Min, Max

    # Simple aggregations
    count = await User.objects().count()
    total = await Order.objects().sum("amount")

    # With GROUP BY
    results = await User.objects().values("status").annotate(count=Count()).exec()
    ```
"""

from abc import ABC, abstractmethod


class Aggregation(ABC):
    """
    Base class for all aggregation functions.

    Aggregations are used to compute summary values from a set of records.
    Each aggregation must implement the `to_sql()` method that returns
    the SurrealDB SQL expression for the aggregation.
    """

    def __init__(self, field: str | None = None, alias: str | None = None) -> None:
        """
        Initialize an aggregation.

        Args:
            field: The field name to aggregate. Some aggregations (like Count)
                   don't require a field.
            alias: Optional alias for the result. If not provided, a default
                   alias will be generated.
        """
        self.field = field
        self.alias = alias

    @abstractmethod
    def to_sql(self) -> str:
        """
        Convert the aggregation to a SurrealDB SQL expression.

        Returns:
            str: The SQL expression for this aggregation.
        """
        pass  # pragma: no cover

    def get_alias(self) -> str:
        """
        Get the alias for this aggregation result.

        Returns:
            str: The alias name for the aggregation result.
        """
        if self.alias:
            return self.alias
        if self.field:
            return f"{self.__class__.__name__.lower()}_{self.field}"
        return self.__class__.__name__.lower()


class Count(Aggregation):
    """
    Count aggregation function.

    Counts the number of records in a query result.

    Example:
        ```python
        # Count all users
        count = await User.objects().count()

        # Count with filter
        active_count = await User.objects().filter(status="active").count()

        # Count with GROUP BY
        results = await User.objects().values("status").annotate(count=Count()).exec()
        ```
    """

    def __init__(self, field: str | None = None, alias: str | None = None) -> None:
        """
        Initialize a Count aggregation.

        Args:
            field: Optional field name. If not provided, counts all records.
            alias: Optional alias for the result.
        """
        super().__init__(field, alias)

    def to_sql(self) -> str:
        """
        Convert to SurrealDB SQL.

        Returns:
            str: "count()" or "count(field)" expression.
        """
        if self.field:
            return f"count({self.field})"
        return "count()"


class Sum(Aggregation):
    """
    Sum aggregation function.

    Calculates the sum of a numeric field.

    Example:
        ```python
        # Sum of all order amounts
        total = await Order.objects().sum("amount")

        # Sum with filter
        total_completed = await Order.objects().filter(status="completed").sum("amount")

        # Sum with GROUP BY
        results = await Order.objects().values("customer_id").annotate(total=Sum("amount")).exec()
        ```
    """

    def __init__(self, field: str, alias: str | None = None) -> None:
        """
        Initialize a Sum aggregation.

        Args:
            field: The numeric field to sum.
            alias: Optional alias for the result.
        """
        if not field:
            raise ValueError("Sum requires a field name")
        super().__init__(field, alias)

    def to_sql(self) -> str:
        """
        Convert to SurrealDB SQL.

        Returns:
            str: "math::sum(field)" expression.
        """
        return f"math::sum({self.field})"


class Avg(Aggregation):
    """
    Average aggregation function.

    Calculates the average of a numeric field.

    Example:
        ```python
        # Average age of all users
        avg_age = await User.objects().avg("age")

        # Average with filter
        avg_active = await User.objects().filter(status="active").avg("age")

        # Average with GROUP BY
        results = await User.objects().values("department").annotate(avg_salary=Avg("salary")).exec()
        ```
    """

    def __init__(self, field: str, alias: str | None = None) -> None:
        """
        Initialize an Avg aggregation.

        Args:
            field: The numeric field to average.
            alias: Optional alias for the result.
        """
        if not field:
            raise ValueError("Avg requires a field name")
        super().__init__(field, alias)

    def to_sql(self) -> str:
        """
        Convert to SurrealDB SQL.

        Returns:
            str: "math::mean(field)" expression.
        """
        return f"math::mean({self.field})"


class Min(Aggregation):
    """
    Minimum aggregation function.

    Finds the minimum value of a field.

    Example:
        ```python
        # Minimum price
        min_price = await Product.objects().min("price")

        # Minimum with filter
        min_active = await Product.objects().filter(active=True).min("price")

        # Minimum with GROUP BY
        results = await Product.objects().values("category").annotate(min_price=Min("price")).exec()
        ```
    """

    def __init__(self, field: str, alias: str | None = None) -> None:
        """
        Initialize a Min aggregation.

        Args:
            field: The field to find the minimum value of.
            alias: Optional alias for the result.
        """
        if not field:
            raise ValueError("Min requires a field name")
        super().__init__(field, alias)

    def to_sql(self) -> str:
        """
        Convert to SurrealDB SQL.

        Returns:
            str: "math::min(field)" expression.
        """
        return f"math::min({self.field})"


class Max(Aggregation):
    """
    Maximum aggregation function.

    Finds the maximum value of a field.

    Example:
        ```python
        # Maximum price
        max_price = await Product.objects().max("price")

        # Maximum with filter
        max_active = await Product.objects().filter(active=True).max("price")

        # Maximum with GROUP BY
        results = await Product.objects().values("category").annotate(max_price=Max("price")).exec()
        ```
    """

    def __init__(self, field: str, alias: str | None = None) -> None:
        """
        Initialize a Max aggregation.

        Args:
            field: The field to find the maximum value of.
            alias: Optional alias for the result.
        """
        if not field:
            raise ValueError("Max requires a field name")
        super().__init__(field, alias)

    def to_sql(self) -> str:
        """
        Convert to SurrealDB SQL.

        Returns:
            str: "math::max(field)" expression.
        """
        return f"math::max({self.field})"
