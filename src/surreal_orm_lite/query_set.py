import logging
from typing import TYPE_CHECKING, Any, Self, cast

from pydantic_core import ValidationError

from . import BaseSurrealModel, SurrealDBConnectionManager
from .constants import LOOKUP_OPERATORS
from .enum import OrderBy
from .exceptions import SurrealDbError, SurrealDbNotFoundError
from .utils import remove_quotes_for_variables, validate_alias_name, validate_field_name

if TYPE_CHECKING:
    from .aggregations import Aggregation

logger = logging.getLogger(__name__)


class QuerySet:
    """
    A class used to build, execute, and manage queries on a SurrealDB table associated with a specific model.

    The `QuerySet` class provides a fluent interface to construct complex queries using method chaining.
    It supports selecting specific fields, filtering results, ordering, limiting, and offsetting the results.
    Additionally, it allows executing custom queries and managing table-level operations such as deletion.

    Example:
        ```python
        queryset = QuerySet(UserModel)
        users = await queryset.filter(age__gt=21).order_by('name').limit(10).all()
        ```
    """

    def __init__(self, model: type[BaseSurrealModel]) -> None:
        """
        Initialize the QuerySet with a specific model.

        This constructor sets up the initial state of the QuerySet, including the model it operates on,
        default filters, selected fields, and other query parameters.

        Args:
            model (type[BaseSurrealModel]): The model class associated with the table. This model should
                inherit from `BaseSurrealModel` and define the table name either via a `_table_name` attribute
                or by defaulting to the class name.

        Attributes:
            model (type[BaseSurrealModel]): The model class associated with the table.
            _filters (list[tuple[str, str, Any]]): A list of filter conditions as tuples of (field, lookup, value).
            select_item (list[str]): A list of field names to be selected in the query.
            _limit (int | None): The maximum number of records to retrieve.
            _offset (int | None): The number of records to skip before starting to return records.
            _order_by (str | None): The field and direction to order the results by.
            _model_table (str): The name of the table in SurrealDB.
            _variables (dict): A dictionary of variables to be used in the query.
        """
        self.model = model
        self._filters: list[tuple[str, str, Any]] = []
        self.select_item: list[str] = []
        self._limit: int | None = None
        self._offset: int | None = None
        self._order_by: str | None = None
        self._model_table: str = getattr(model, "_table_name", model.__name__)
        self._variables: dict = {}
        self._group_by_fields: list[str] = []
        self._annotations: dict[str, Aggregation] = {}

    def select(self, *fields: str) -> Self:
        """
        Specify the fields to retrieve in the query.

        By default, all fields are selected (`SELECT *`). This method allows you to specify
        a subset of fields to be retrieved, which can improve performance by fetching only necessary data.

        Args:
            *fields (str): Variable length argument list of field names to select.

        Returns:
            Self: The current instance of QuerySet to allow method chaining.

        Example:
            ```python
            queryset.select('id', 'name', 'email')
            ```
        """
        # Store the list of fields to retrieve
        self.select_item = list(fields)
        return self

    def variables(self, **kwargs: Any) -> Self:
        """
        Set variables for the query.

        Variables can be used in parameterized queries to safely inject values without risking SQL injection.

        Args:
            **kwargs (Any): Arbitrary keyword arguments representing variable names and their corresponding values.

        Returns:
            Self: The current instance of QuerySet to allow method chaining.

        Example:
            ```python
            queryset.variables(status='active', role='admin')
            ```
        """
        self._variables = dict(kwargs.items())
        return self

    def filter(self, **kwargs: Any) -> Self:
        """
        Add filter conditions to the query.

        This method allows adding one or multiple filter conditions to narrow down the query results.
        Filters are added using keyword arguments where the key represents the field and the lookup type,
        and the value represents the value to filter by.

        Supported lookup types include:
            - exact
            - contains
            - gt (greater than)
            - lt (less than)
            - gte (greater than or equal)
            - lte (less than or equal)
            - in (within a list of values)

        Args:
            **kwargs (Any): Arbitrary keyword arguments representing filter conditions. The key should be in the format
                `field__lookup` (e.g., `age__gt=30`). If no lookup is provided, `exact` is assumed.

        Returns:
            Self: The current instance of QuerySet to allow method chaining.

        Example:
            ```python
            queryset.filter(age__gt=21, status='active')
            ```
        """
        for key, value in kwargs.items():
            field_name, lookup = self._parse_lookup(key)
            self._filters.append((field_name, lookup, value))
        return self

    def _parse_lookup(self, key: str) -> tuple[str, str]:
        """
        Parse the lookup type from the filter key.

        This helper method splits the filter key into the field name and the lookup type.
        If no lookup type is specified, it defaults to `exact`.

        Args:
            key (str): The filter key in the format `field__lookup` or just `field`.

        Returns:
            tuple[str, str]: A tuple containing the field name and the lookup type.

        Example:
            ```python
            _parse_lookup('age__gt')  # Returns ('age', 'gt')
            _parse_lookup('status')    # Returns ('status', 'exact')
            ```
        """
        if "__" in key:
            field_name, lookup_name = key.split("__", 1)
        else:
            field_name, lookup_name = key, "exact"
        return field_name, lookup_name

    def limit(self, value: int) -> Self:
        """
        Set a limit on the number of results to retrieve.

        This method restricts the number of records returned by the query, which is useful for pagination
        or when only a subset of results is needed.

        Args:
            value (int): The maximum number of records to retrieve.

        Returns:
            Self: The current instance of QuerySet to allow method chaining.

        Example:
            ```python
            queryset.limit(10)
            ```
        """
        self._limit = value
        return self

    def offset(self, value: int) -> Self:
        """
        Set an offset for the results.

        This method skips a specified number of records before starting to return records.
        It is commonly used in conjunction with `limit` for pagination purposes.

        Args:
            value (int): The number of records to skip.

        Returns:
            Self: The current instance of QuerySet to allow method chaining.

        Example:
            ```python
            queryset.offset(20)
            ```
        """
        self._offset = value
        return self

    def order_by(self, field_name: str, type: OrderBy = OrderBy.ASC) -> Self:
        """
        Set the field and direction to order the results by.

        This method allows sorting the query results based on a specified field and direction
        (ascending or descending).

        Args:
            field_name (str): The name of the field to sort by.
            type (OrderBy, optional): The direction to sort by. Defaults to `OrderBy.ASC`.

        Returns:
            Self: The current instance of QuerySet to allow method chaining.

        Example:
            ```python
            queryset.order_by('name', OrderBy.DESC)
            ```
        """
        self._order_by = f"{field_name} {type}"
        return self

    def _build_where_clauses(self) -> list[str]:
        """
        Build WHERE clause conditions from the current filters.

        Returns:
            list[str]: A list of WHERE clause condition strings.
        """
        where_clauses = []
        for field_name, lookup_name, value in self._filters:
            op = LOOKUP_OPERATORS.get(lookup_name, "=")
            if lookup_name == "in":
                formatted_values = ", ".join(repr(v) for v in value)
                where_clauses.append(f"{field_name} {op} [{formatted_values}]")
            else:
                where_clauses.append(f"{field_name} {op} {repr(value)}")
        return where_clauses

    def _compile_query(self) -> str:
        """
        Compile the QuerySet parameters into a SQL query string.

        This method constructs the final SQL query by combining the selected fields, filters,
        ordering, limit, and offset parameters.

        Returns:
            str: The compiled SQL query string.

        Example:
            ```python
            query = queryset._compile_query()
            # Returns something like:
            # "SELECT id, name FROM users WHERE age > 21 AND status = 'active' ORDER BY name ASC LIMIT 10 START 20;"
            ```
        """
        where_clauses = self._build_where_clauses()

        # Construct the SELECT clause
        if self.select_item:
            fields = ", ".join(self.select_item)
            query = f"SELECT {fields} FROM {self._model_table}"
        else:
            query = f"SELECT * FROM {self._model_table}"

        # Append WHERE clauses if any
        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)

        # Append ORDER BY if set (must come before LIMIT/START in SurrealDB)
        if self._order_by:
            query += f" ORDER BY {self._order_by}"

        # Append LIMIT if set
        if self._limit is not None:
            query += f" LIMIT {self._limit}"

        # Append OFFSET (START) if set
        if self._offset is not None:
            query += f" START {self._offset}"

        query += ";"
        return query

    async def exec(self) -> Any:
        """
        Execute the compiled query and return the results.

        This method runs the constructed SQL query against the SurrealDB database and processes
        the results. If the data conforms to the model schema, it returns a list of model instances;
        otherwise, it returns a list of dictionaries.

        When `values()` and `annotate()` are used, returns a list of dictionaries with
        the grouped fields and aggregation results.

        Returns:
            list[BaseSurrealModel] | list[dict]: A list of model instances if validation is successful,
            otherwise a list of dictionaries representing the raw data. For GROUP BY queries,
            always returns a list of dictionaries.

        Raises:
            SurrealDbError: If there is an issue executing the query.

        Example:
            ```python
            # Regular query
            results = await queryset.exec()

            # GROUP BY query
            results = await User.objects().values("status").annotate(count=Count()).exec()
            # Returns: [{"status": "active", "count": 42}, ...]
            ```
        """
        # Handle GROUP BY queries with annotations
        if self._annotations:
            query = self._compile_group_by_query()
            results = await self._execute_query(query)
            # GROUP BY queries always return dicts, not model instances
            return results if isinstance(results, list) else []

        query = self._compile_query()
        results = await self._execute_query(query)
        try:
            # SDK 1.0.8 returns list directly from query()
            if isinstance(results, list):
                return self.model.from_db(results)
            # Fallback for older response format
            data = cast(dict, results[0])
            return self.model.from_db(data.get("result", []))
        except ValidationError as e:
            logger.info(f"Pydantic invalid format for the class, returning dict value: {e}")
            return results if isinstance(results, list) else []

    async def first(self) -> Any:
        """
        Execute the query and return the first result.

        This method modifies the QuerySet to limit the results to one and retrieves the first record.
        If no records are found, it returns `None`.

        Returns:
            BaseSurrealModel | dict | None: The first model instance if available, a dictionary if
            model validation fails, or `None` if no results are found.

        Raises:
            SurrealDbError: If there is an issue executing the query.

        Example:
            ```python
            first_user = await queryset.filter(name='Alice').first()
            ```
        """
        self._limit = 1
        results = await self.exec()
        if results:
            return results[0]

        raise SurrealDbNotFoundError("No result found.")

    async def get(self, id_item: Any = None) -> Any:
        """
        Retrieve a single record by its unique identifier or based on the current QuerySet filters.

        This method fetches a specific record by its ID if provided. If no ID is provided, it attempts
        to retrieve a single record based on the existing filters. It raises an error if multiple or
        no records are found when no ID is specified.

        Args:
            id_item (str | None, optional): The unique identifier of the item to retrieve. Defaults to `None`.

        Returns:
            BaseSurrealModel | dict[str, Any]: The retrieved model instance or a dictionary representing the raw data.

        Raises:
            SurrealDbError: If multiple records are found when `id_item` is not provided or if no records are found.

        Example:
            ```python
            user = await queryset.get(id_item='user:123')
            ```
        """
        if id_item:
            client = await SurrealDBConnectionManager.get_client()
            data = await client.select(f"{self._model_table}:{id_item}")
            # SDK 1.0.8 returns a list even for single record select
            if isinstance(data, list):
                if len(data) == 0:
                    raise SurrealDbNotFoundError("No result found.")
                data = data[0]
            return self.model.from_db(data)
        else:
            result = await self.exec()
            if len(result) > 1:
                raise SurrealDbError("More than one result found.")

            if len(result) == 0:
                raise SurrealDbNotFoundError("No result found.")
            return result[0]

    async def all(self) -> Any:
        """
        Fetch all records from the associated table.

        This method retrieves every record from the table without applying any filters, limits, or ordering.

        Returns:
            list[BaseSurrealModel]: A list of model instances representing all records in the table.

        Raises:
            SurrealDbError: If there is an issue executing the query.

        Example:
            ```python
            all_users = await queryset.all()
            ```
        """
        client = await SurrealDBConnectionManager.get_client()
        results = await client.select(self._model_table)
        return self.model.from_db(results)

    async def _execute_query(self, query: str) -> list[dict[str, Any]]:
        """
        Execute the given SQL query using the SurrealDB client.

        This internal method handles the execution of the compiled SQL query and returns the raw results
        from the database.

        Args:
            query (str): The SQL query string to execute.

        Returns:
            list[QueryResponse]: A list of `QueryResponse` objects containing the query results.

        Raises:
            SurrealDbError: If there is an issue executing the query.

        Example:
            ```python
            results = await self._execute_query("SELECT * FROM users;")
            ```
        """
        client = await SurrealDBConnectionManager.get_client()
        return await self._run_query_on_client(client, query)

    async def _run_query_on_client(self, client: Any, query: str) -> list[dict[str, Any]]:
        """
        Run the SQL query on the provided SurrealDB client.

        This internal method sends the query to the SurrealDB client along with any predefined variables
        and returns the raw query responses.

        Args:
            client (AsyncSurrealDB): The active SurrealDB client instance.
            query (str): The SQL query string to execute.

        Returns:
            list[QueryResponse]: A list of `QueryResponse` objects containing the query results.

        Raises:
            SurrealDbError: If there is an issue executing the query.

        Example:
            ```python
            results = await self._run_query_on_client(client, "SELECT * FROM users;")
            ```
        """
        return await client.query(remove_quotes_for_variables(query), self._variables)  # type: ignore

    async def delete_table(self) -> bool:
        """
        Delete the associated table from the SurrealDB database.

        This method performs a destructive operation by removing the entire table from the database.
        Use with caution, especially in production environments.

        Returns:
            bool: `True` if the table was successfully deleted.

        Raises:
            SurrealDbError: If there is an issue deleting the table.

        Example:
            ```python
            success = await queryset.delete_table()
            ```
        """
        client = await SurrealDBConnectionManager.get_client()
        await client.delete(self._model_table)
        return True

    # ==================== Aggregation Methods ====================

    def values(self, *fields: str) -> Self:
        """
        Specify fields for GROUP BY operations.

        This method sets up the fields that will be used for grouping when
        combined with `annotate()`. Similar to Django's `values()` method.

        Args:
            *fields: Field names to group by.

        Returns:
            Self: The current QuerySet instance for method chaining.

        Example:
            ```python
            # Group users by status and count them
            results = await User.objects().values("status").annotate(count=Count()).exec()
            # Returns: [{"status": "active", "count": 42}, {"status": "inactive", "count": 8}]
            ```
        """
        for field in fields:
            validate_field_name(field, "GROUP BY field")
        self._group_by_fields = list(fields)
        return self

    def annotate(self, **annotations: "Aggregation") -> Self:
        """
        Add aggregation annotations to the query.

        This method adds aggregation functions to the query. When combined with
        `values()`, it performs GROUP BY operations. Similar to Django's `annotate()`.

        Args:
            **annotations: Keyword arguments where the key is the alias and the value
                          is an Aggregation instance (Count, Sum, Avg, Min, Max).

        Returns:
            Self: The current QuerySet instance for method chaining.

        Raises:
            TypeError: If any annotation value is not an Aggregation instance.

        Example:
            ```python
            from surreal_orm_lite import Count, Sum, Avg

            # Group by status and count
            results = await User.objects().values("status").annotate(count=Count()).exec()

            # Multiple aggregations
            results = await Order.objects().values("customer_id").annotate(
                total=Sum("amount"),
                avg_order=Avg("amount"),
                order_count=Count()
            ).exec()
            ```
        """
        from .aggregations import Aggregation as AggregationClass

        for alias, agg in annotations.items():
            validate_alias_name(alias)
            if not isinstance(agg, AggregationClass):
                raise TypeError(f"annotate() argument '{alias}' must be an Aggregation instance, got {type(agg).__name__}")
        self._annotations.update(annotations)
        return self

    async def count(self) -> int:
        """
        Count the number of records matching the query.

        This is a shortcut method that returns the count as an integer directly.

        Returns:
            int: The number of matching records.

        Example:
            ```python
            # Count all users
            total = await User.objects().count()

            # Count with filters
            active = await User.objects().filter(status="active").count()
            ```
        """
        query = self._compile_aggregation_query("count()")
        results = await self._execute_query(query)

        if isinstance(results, list) and len(results) > 0:
            result = results[0]
            if isinstance(result, dict):
                # Handle GROUP ALL result format
                return int(result.get("count", 0))
            return int(result)
        return 0

    async def sum(self, field: str) -> float | int:
        """
        Calculate the sum of a numeric field.

        Args:
            field: The name of the numeric field to sum.

        Returns:
            float | int: The sum of the field values, or 0 if no records match.

        Raises:
            ValueError: If field name is empty or invalid.

        Example:
            ```python
            # Sum of all order amounts
            total = await Order.objects().sum("amount")

            # Sum with filter
            completed_total = await Order.objects().filter(status="completed").sum("amount")
            ```
        """
        validate_field_name(field, "sum() field")
        query = self._compile_aggregation_query(f"math::sum({field})", alias="sum")
        results = await self._execute_query(query)

        if isinstance(results, list) and len(results) > 0:
            result = results[0]
            if isinstance(result, dict):
                value = result.get("sum", 0)
                return value if value is not None else 0
            return result if result is not None else 0
        return 0

    async def avg(self, field: str) -> float:
        """
        Calculate the average of a numeric field.

        Args:
            field: The name of the numeric field to average.

        Returns:
            float: The average of the field values, or 0.0 if no records match.

        Raises:
            ValueError: If field name is empty or invalid.

        Example:
            ```python
            # Average age of all users
            avg_age = await User.objects().avg("age")

            # Average with filter
            avg_active = await User.objects().filter(status="active").avg("age")
            ```
        """
        validate_field_name(field, "avg() field")
        query = self._compile_aggregation_query(f"math::mean({field})", alias="avg")
        results = await self._execute_query(query)

        if isinstance(results, list) and len(results) > 0:
            result = results[0]
            if isinstance(result, dict):
                value = result.get("avg", 0.0)
                return float(value) if value is not None else 0.0
            return float(result) if result is not None else 0.0
        return 0.0

    async def min(self, field: str) -> Any:
        """
        Find the minimum value of a field.

        Args:
            field: The name of the field to find the minimum value of.

        Returns:
            Any: The minimum value, or None if no records match.

        Raises:
            ValueError: If field name is empty or invalid.

        Example:
            ```python
            # Minimum price
            min_price = await Product.objects().min("price")

            # Minimum with filter
            min_active = await Product.objects().filter(active=True).min("price")
            ```
        """
        validate_field_name(field, "min() field")
        query = self._compile_aggregation_query(f"math::min({field})", alias="min")
        results = await self._execute_query(query)

        if isinstance(results, list) and len(results) > 0:
            result = results[0]
            if isinstance(result, dict):
                return result.get("min")
            return result
        return None

    async def max(self, field: str) -> Any:
        """
        Find the maximum value of a field.

        Args:
            field: The name of the field to find the maximum value of.

        Returns:
            Any: The maximum value, or None if no records match.

        Raises:
            ValueError: If field name is empty or invalid.

        Example:
            ```python
            # Maximum price
            max_price = await Product.objects().max("price")

            # Maximum with filter
            max_active = await Product.objects().filter(active=True).max("price")
            ```
        """
        validate_field_name(field, "max() field")
        query = self._compile_aggregation_query(f"math::max({field})", alias="max")
        results = await self._execute_query(query)

        if isinstance(results, list) and len(results) > 0:
            result = results[0]
            if isinstance(result, dict):
                return result.get("max")
            return result
        return None

    async def exists(self) -> bool:
        """
        Check if any records match the current query.

        This method is more efficient than `count()` when you only need to know
        if any matching records exist.

        Returns:
            bool: True if at least one record matches, False otherwise.

        Example:
            ```python
            # Check if any admins exist
            has_admin = await User.objects().filter(role="admin").exists()

            # Check if user exists
            user_exists = await User.objects().filter(email="alice@example.com").exists()
            ```
        """
        # Use LIMIT 1 for efficiency without permanently mutating the QuerySet
        original_limit = self._limit
        try:
            self._limit = 1
            query = self._compile_query()
            results = await self._execute_query(query)

            if isinstance(results, list):
                return len(results) > 0
            return False
        finally:
            self._limit = original_limit

    def _compile_aggregation_query(self, aggregation_expr: str, alias: str | None = None) -> str:
        """
        Compile an aggregation query.

        This internal method builds the SQL query for aggregation operations.

        Args:
            aggregation_expr: The aggregation expression (e.g., "count()", "math::sum(field)").
            alias: Optional alias for the aggregation result.

        Returns:
            str: The compiled SQL query string.
        """
        where_clauses = self._build_where_clauses()

        # Build SELECT clause with aggregation
        if alias:
            query = f"SELECT {aggregation_expr} AS {alias} FROM {self._model_table}"
        else:
            query = f"SELECT {aggregation_expr} FROM {self._model_table}"

        # Append WHERE clauses
        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)

        # GROUP ALL is required for aggregations without GROUP BY in SurrealDB
        query += " GROUP ALL"
        query += ";"
        return query

    def _compile_group_by_query(self) -> str:
        """
        Compile a GROUP BY query with annotations.

        This internal method builds the SQL query for GROUP BY operations
        with aggregation annotations.

        Returns:
            str: The compiled SQL query string.
        """
        where_clauses = self._build_where_clauses()

        # Build SELECT clause with group fields and annotations
        select_parts = list(self._group_by_fields)
        for alias, agg in self._annotations.items():
            select_parts.append(f"{agg.to_sql()} AS {alias}")

        query = f"SELECT {', '.join(select_parts)} FROM {self._model_table}"

        # Append WHERE clauses
        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)

        # Append GROUP BY
        if self._group_by_fields:
            query += f" GROUP BY {', '.join(self._group_by_fields)}"
        else:
            query += " GROUP ALL"

        # Append ORDER BY if set
        if self._order_by:
            query += f" ORDER BY {self._order_by}"

        # Append LIMIT if set
        if self._limit is not None:
            query += f" LIMIT {self._limit}"

        # Append OFFSET if set
        if self._offset is not None:
            query += f" START {self._offset}"

        query += ";"
        return query

    async def query(self, query: str, variables: dict[str, Any] | None = None) -> Any:
        """
        Execute a custom SQL query on the SurrealDB database.

        This method allows running arbitrary SQL queries, provided they operate on the correct table
        associated with the current model. It ensures that the query includes the `FROM` clause referencing
        the correct table to maintain consistency and security.

        Args:
            query (str): The custom SQL query string to execute.
            variables (dict[str, Any], optional): A dictionary of variables to substitute into the query.
                Defaults to an empty dictionary.

        Returns:
            Any: The result of the query, typically a model instance or a list of model instances.

        Raises:
            SurrealDbError: If the query does not include the correct `FROM` clause or if there is an issue executing the query.

        Example:
            ```python
            custom_query = "SELECT name, email FROM UserModel WHERE status = $status;"
            results = await queryset.query(custom_query, variables={'status': 'active'})
            ```
        """
        if f"FROM {self.model.__name__}" not in query:
            raise SurrealDbError(f"The query must include 'FROM {self.model.__name__}' to reference the correct table.")
        client = await SurrealDBConnectionManager.get_client()
        results = await client.query(remove_quotes_for_variables(query), variables or {})
        # SDK 1.0.8 returns list directly from query()
        if isinstance(results, list):
            return self.model.from_db(results)
        # Fallback for older response format
        data = cast(dict, results[0])
        return self.model.from_db(data.get("result", []))
