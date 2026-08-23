import contextlib
import logging
import math
from typing import TYPE_CHECKING, Any, Self, cast

from pydantic_core import ValidationError

from . import BaseSurrealModel, SurrealDBConnectionManager
from ._sdk import NotFoundError
from .enum import OrderBy
from .exceptions import SurrealDbError, SurrealDbNotFoundError
from .functions import Var
from .q import Q
from .utils import (
    build_filter_condition,
    coerce_record_id,
    parse_lookup,
    remove_quotes_for_variables,
    validate_alias_name,
    validate_field_name,
    validate_patch_operations,
    warn_on_multiple_statements,
)

if TYPE_CHECKING:
    from .aggregations import Aggregation

logger = logging.getLogger(__name__)


class QuerySet:
    """
    A class used to build, execute, and manage queries on a SurrealDB table associated with a specific model.

    The ``QuerySet`` class provides a fluent interface to construct complex queries using method chaining.
    It supports selecting specific fields, filtering results, ordering, limiting, and offsetting the results.
    Additionally, it allows executing custom queries and managing table-level operations such as deletion.

    Example::

        queryset = QuerySet(UserModel)
        users = await queryset.filter(age__gt=21).order_by('name').limit(10).exec()
    """

    def __init__(self, model: type[BaseSurrealModel]) -> None:
        self.model = model
        self._filters: list[tuple[str, str, Any]] = []
        self._q_filters: list[Q] = []
        self.select_item: list[str] = []
        self._limit: int | None = None
        self._offset: int | None = None
        self._order_by: str | None = None
        self._model_table: str = getattr(model, "_table_name", model.__name__)
        self._variables: dict = {}
        self._fetch_fields: list[str] = []
        self._group_by_fields: list[str] = []
        self._annotations: dict[str, Aggregation] = {}
        self._tx: Any = None

    def select(self, *fields: str) -> Self:
        """
        Specify the fields to retrieve in the query.

        By default, all fields are selected (``SELECT *``). This method allows you to specify
        a subset of fields to be retrieved.

        Args:
            *fields: Field names to select.

        Returns:
            Self: The current instance for method chaining.
        """
        self.select_item = list(fields)
        return self

    def variables(self, **kwargs: Any) -> Self:
        """
        Set variables for the query.

        Variables can be used in parameterized queries to safely inject values.

        Args:
            **kwargs: Variable names and their values.

        Returns:
            Self: The current instance for method chaining.
        """
        self._variables.update(kwargs)
        return self

    def filter(self, *args: Q, **kwargs: Any) -> Self:
        """
        Add filter conditions to the query.

        Supports both keyword arguments for simple filters and Q objects for complex queries.

        Args:
            *args: Q objects for complex OR/AND/NOT queries.
            **kwargs: Simple filter conditions in ``field__lookup=value`` format.

        Returns:
            Self: The current instance for method chaining.

        Example::

            # Simple filters (AND-joined)
            queryset.filter(age__gt=21, status='active')

            # Q objects for OR queries
            queryset.filter(Q(name="alice") | Q(name="bob"))

            # Mixed
            queryset.filter(Q(role="admin") | Q(role="mod"), status="active")
        """
        for arg in args:
            if not isinstance(arg, Q):
                raise TypeError(f"filter() positional arguments must be Q objects, got {type(arg).__name__}")
            self._q_filters.append(arg)
        for key, value in kwargs.items():
            field_name, lookup = parse_lookup(key)
            self._filters.append((field_name, lookup, value))
        return self

    def limit(self, value: int) -> Self:
        """
        Set a limit on the number of results to retrieve.

        Args:
            value: The maximum number of records to retrieve.

        Returns:
            Self: The current instance for method chaining.
        """
        self._limit = value
        return self

    def offset(self, value: int) -> Self:
        """
        Set an offset for the results.

        Args:
            value: The number of records to skip.

        Returns:
            Self: The current instance for method chaining.
        """
        self._offset = value
        return self

    def order_by(self, *fields: str | OrderBy) -> Self:
        """
        Set the field(s) and direction to order the results by.

        Supports ``-field`` prefix for descending order, multiple fields,
        and backward-compatible ``order_by("field", OrderBy.DESC)`` syntax.

        Args:
            *fields: Field names to sort by. Prefix with ``-`` for descending order.

        Returns:
            Self: The current instance for method chaining.

        Example::

            queryset.order_by("name")                    # ASC
            queryset.order_by("-created_at")              # DESC
            queryset.order_by("-age", "name")             # age DESC, name ASC
            queryset.order_by("name", OrderBy.DESC)       # backward compatible
        """
        order_parts: list[str] = []
        i = 0
        while i < len(fields):
            if isinstance(fields[i], OrderBy):
                raise TypeError(
                    f"order_by() expected a field name at position {i + 1}, but got an OrderBy value. "
                    'Pass the direction after a field name, e.g. order_by("field", OrderBy.DESC).'
                )
            field = str(fields[i])
            # Backward compat: check if next arg is an OrderBy direction
            if i + 1 < len(fields) and str(fields[i + 1]) in ("ASC", "DESC"):
                validate_field_name(field, "order_by field")
                order_parts.append(f"{field} {fields[i + 1]}")
                i += 2
            elif field.startswith("-"):
                actual_field = field[1:]
                validate_field_name(actual_field, "order_by field")
                order_parts.append(f"{actual_field} DESC")
                i += 1
            else:
                validate_field_name(field, "order_by field")
                order_parts.append(f"{field} ASC")
                i += 1

        self._order_by = ", ".join(order_parts)
        return self

    def fetch(self, *fields: str) -> Self:
        """
        Add a FETCH clause to resolve record links inline.

        This prevents N+1 queries by fetching linked records in a single query.

        Args:
            *fields: Field names to fetch/resolve.

        Returns:
            Self: The current instance for method chaining.

        Example::

            posts = await Post.objects().fetch("author", "tags").exec()
            # Generates: SELECT * FROM Post FETCH author, tags;
        """
        for field in fields:
            validate_field_name(field, "FETCH field")
        self._fetch_fields.extend(fields)
        return self

    # ==================== Internal query building ====================

    def _build_where(self) -> tuple[str, dict[str, Any]]:
        """
        Build a parameterized WHERE clause from filters and Q objects.

        Returns:
            A tuple of (where_clause_string, variables_dict).
            The where_clause_string includes the ``WHERE`` keyword, or is empty if no filters.
        """
        parts: list[str] = []
        variables: dict[str, Any] = {}
        counter = 0

        # Regular keyword filters (AND-joined)
        for field_name, lookup_name, value in self._filters:
            sql, vars_, counter = build_filter_condition(field_name, lookup_name, value, counter, self._model_table)
            parts.append(sql)
            variables.update(vars_)

        # Q object filters
        for q in self._q_filters:
            sql, vars_, counter = q.to_sql(counter, self._model_table)
            if sql:
                parts.append(sql)
                variables.update(vars_)

        if not parts:
            return "", {}

        return " WHERE " + " AND ".join(parts), variables

    def _compile_query(self) -> tuple[str, dict[str, Any]]:
        """
        Compile the QuerySet parameters into a parameterized SQL query.

        Returns:
            A tuple of (query_string, variables_dict).
        """
        where_clause, where_vars = self._build_where()

        if self.select_item:
            fields = ", ".join(self.select_item)
            query = f"SELECT {fields} FROM {self._model_table}"
        else:
            query = f"SELECT * FROM {self._model_table}"

        query += where_clause

        if self._order_by:
            query += f" ORDER BY {self._order_by}"

        if self._limit is not None:
            query += f" LIMIT {self._limit}"

        if self._offset is not None:
            query += f" START {self._offset}"

        if self._fetch_fields:
            query += f" FETCH {', '.join(self._fetch_fields)}"

        query += ";"
        all_variables = {**self._variables, **where_vars}
        return query, all_variables

    def _compile_aggregation_query(self, aggregation_expr: str, alias: str | None = None) -> tuple[str, dict[str, Any]]:
        """
        Compile an aggregation query with parameterized WHERE clause.

        Args:
            aggregation_expr: The aggregation expression (e.g., ``"count()"``, ``"math::sum(field)"``).
            alias: Optional alias for the aggregation result.

        Returns:
            A tuple of (query_string, variables_dict).
        """
        where_clause, where_vars = self._build_where()

        if alias:
            query = f"SELECT {aggregation_expr} AS {alias} FROM {self._model_table}"
        else:
            query = f"SELECT {aggregation_expr} FROM {self._model_table}"

        query += where_clause
        query += " GROUP ALL;"

        all_variables = {**self._variables, **where_vars}
        return query, all_variables

    def _compile_group_by_query(self) -> tuple[str, dict[str, Any]]:
        """
        Compile a GROUP BY query with annotations and parameterized WHERE clause.

        Returns:
            A tuple of (query_string, variables_dict).
        """
        where_clause, where_vars = self._build_where()

        select_parts = list(self._group_by_fields)
        for alias, agg in self._annotations.items():
            select_parts.append(f"{agg.to_sql()} AS {alias}")

        query = f"SELECT {', '.join(select_parts)} FROM {self._model_table}"
        query += where_clause

        if self._group_by_fields:
            query += f" GROUP BY {', '.join(self._group_by_fields)}"
        else:
            query += " GROUP ALL"

        if self._order_by:
            query += f" ORDER BY {self._order_by}"

        if self._limit is not None:
            query += f" LIMIT {self._limit}"

        if self._offset is not None:
            query += f" START {self._offset}"

        query += ";"
        all_variables = {**self._variables, **where_vars}
        return query, all_variables

    # ==================== Query execution ====================

    async def _execute_query(self, query: str, variables: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """
        Execute a read query. Routes through the transaction when one is attached
        (``objects(tx=)``), so reads participate in the transaction and see its
        uncommitted writes (interactive, 3.x). On a buffered tx, ``run_read`` raises.
        """
        vars_ = variables if variables is not None else self._variables
        compiled = remove_quotes_for_variables(query)
        if self._tx is not None:
            try:
                return await self._tx.run_read(compiled, vars_)  # type: ignore[no-any-return]
            except NotFoundError:
                return []
        client = await SurrealDBConnectionManager.get_client()
        try:
            return await client.query(compiled, vars_)  # type: ignore
        except NotFoundError:
            return []

    async def exec(self) -> Any:
        """
        Execute the compiled query and return the results.

        When ``values()`` and ``annotate()`` are used, returns a list of dictionaries with
        the grouped fields and aggregation results.

        Returns:
            list[BaseSurrealModel] | list[dict]: Model instances or dictionaries for GROUP BY queries.
        """
        if self._annotations:
            query, variables = self._compile_group_by_query()
            results = await self._execute_query(query, variables)
            return results if isinstance(results, list) else []

        query, variables = self._compile_query()
        results = await self._execute_query(query, variables)
        try:
            if isinstance(results, list):
                return self.model.from_db(results)
            data = cast(dict, results[0])
            return self.model.from_db(data.get("result", []))
        except ValidationError as e:
            logger.info(f"Pydantic invalid format for the class, returning dict value: {e}")
            return results if isinstance(results, list) else []

    async def first(self) -> Any:
        """
        Execute the query and return the first result.

        The queryset is left untouched: the ``LIMIT 1`` applies to this call only, so the
        same queryset can be reused (or re-executed) afterwards and still yields every
        matching row.

        Returns:
            The first model instance, or raises SurrealDbNotFoundError if no results.
        """
        original_limit = self._limit
        try:
            self._limit = 1
            results = await self.exec()
        finally:
            self._limit = original_limit

        if results:
            return results[0]

        raise SurrealDbNotFoundError("No result found.")

    async def get(self, id_item: Any = None) -> Any:
        """
        Retrieve a single record by its unique identifier or based on the current filters.

        Args:
            id_item: The unique identifier of the item to retrieve.

        Returns:
            The retrieved model instance or dictionary.
        """
        if id_item:
            # Same rule as filter(id=...): an int is an *integer* record id, a str a string
            # one. Building RecordID(table, str(id_item)) made get(5) unreachable on a model
            # with `id: int` (issue #159).
            record_id = coerce_record_id(id_item, self._model_table)

            if self._tx is not None:
                try:
                    rows = await self._tx.run_read("SELECT * FROM $rid;", {"rid": record_id})
                except NotFoundError:
                    raise SurrealDbNotFoundError("No result found.") from None
                data = rows[0] if isinstance(rows, list) and rows else rows
                if not data:
                    raise SurrealDbNotFoundError("No result found.")
                return self.model.from_db(data)

            client = await SurrealDBConnectionManager.get_client()
            try:
                data = await client.select(record_id)
            except NotFoundError:
                raise SurrealDbNotFoundError("No result found.") from None
            if isinstance(data, list):
                if len(data) == 0:
                    raise SurrealDbNotFoundError("No result found.")
                data = data[0]
            if data is None:
                raise SurrealDbNotFoundError("No result found.")
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
        """
        if self._tx is not None:
            try:
                rows = await self._tx.run_read(f"SELECT * FROM {self._model_table};", {})
            except NotFoundError:
                return self.model.from_db([])
            return self.model.from_db(rows if isinstance(rows, list) else [])

        client = await SurrealDBConnectionManager.get_client()
        try:
            results = await client.select(self._model_table)
        except NotFoundError:
            return self.model.from_db([])
        return self.model.from_db(results)

    async def delete_table(self) -> bool:
        """
        Delete the associated table from the SurrealDB database.

        Returns True. A missing table is treated as already-absent (no-op),
        so this is safe for idempotent cleanup on SurrealDB 3.x (which raises
        NotFoundError) as well as 2.x (which is a no-op).
        """
        client = await SurrealDBConnectionManager.get_client()
        with contextlib.suppress(NotFoundError):
            await client.delete(self._model_table)
        return True

    # ==================== Aggregation Methods ====================

    def values(self, *fields: str) -> Self:
        """
        Specify fields for GROUP BY operations.

        Args:
            *fields: Field names to group by.

        Returns:
            Self: The current QuerySet instance for method chaining.
        """
        for field in fields:
            validate_field_name(field, "GROUP BY field")
        self._group_by_fields = list(fields)
        return self

    def annotate(self, **annotations: "Aggregation") -> Self:
        """
        Add aggregation annotations to the query.

        Args:
            **annotations: Keyword arguments where the key is the alias and the value
                          is an Aggregation instance (Count, Sum, Avg, Min, Max).

        Returns:
            Self: The current QuerySet instance for method chaining.
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

        Returns:
            The number of matching records.
        """
        query, variables = self._compile_aggregation_query("count()")
        results = await self._execute_query(query, variables)

        if isinstance(results, list) and len(results) > 0:
            result = results[0]
            if isinstance(result, dict):
                return int(result.get("count", 0))
            return int(result)
        return 0

    async def sum(self, field: str) -> float | int:
        """
        Calculate the sum of a numeric field.

        Args:
            field: The name of the numeric field to sum.

        Returns:
            The sum of the field values, or 0 if no records match.
        """
        validate_field_name(field, "sum() field")
        query, variables = self._compile_aggregation_query(f"math::sum({field})", alias="sum")
        results = await self._execute_query(query, variables)

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
            The average of the field values, or 0.0 if no records match.
        """
        validate_field_name(field, "avg() field")
        query, variables = self._compile_aggregation_query(f"math::mean({field})", alias="avg")
        results = await self._execute_query(query, variables)

        if isinstance(results, list) and len(results) > 0:
            result = results[0]
            value = result.get("avg", 0.0) if isinstance(result, dict) else result
            # SurrealDB 3.x returns NaN for math::mean over an empty set; the ORM
            # contract is 0.0 for "no records".
            if value is None or (isinstance(value, float) and math.isnan(value)):
                return 0.0
            return float(value)
        return 0.0

    async def min(self, field: str) -> Any:
        """
        Find the minimum value of a field.

        Args:
            field: The name of the field to find the minimum value of.

        Returns:
            The minimum value, or None if no records match.
        """
        validate_field_name(field, "min() field")
        query, variables = self._compile_aggregation_query(f"math::min({field})", alias="min")
        results = await self._execute_query(query, variables)

        if isinstance(results, list) and len(results) > 0:
            result = results[0]
            value = result.get("min") if isinstance(result, dict) else result
            # SurrealDB 3.x returns +inf for math::min over an empty set; the ORM
            # contract is None for "no records".
            if value is None or (isinstance(value, float) and (math.isinf(value) or math.isnan(value))):
                return None
            return value
        return None

    async def max(self, field: str) -> Any:
        """
        Find the maximum value of a field.

        Args:
            field: The name of the field to find the maximum value of.

        Returns:
            The maximum value, or None if no records match.
        """
        validate_field_name(field, "max() field")
        query, variables = self._compile_aggregation_query(f"math::max({field})", alias="max")
        results = await self._execute_query(query, variables)

        if isinstance(results, list) and len(results) > 0:
            result = results[0]
            value = result.get("max") if isinstance(result, dict) else result
            # SurrealDB 3.x returns -inf for math::max over an empty set; the ORM
            # contract is None for "no records".
            if value is None or (isinstance(value, float) and (math.isinf(value) or math.isnan(value))):
                return None
            return value
        return None

    async def exists(self) -> bool:
        """
        Check if any records match the current query.

        Returns:
            True if at least one record matches, False otherwise.
        """
        original_limit = self._limit
        try:
            self._limit = 1
            query, variables = self._compile_query()
            results = await self._execute_query(query, variables)

            if isinstance(results, list):
                return len(results) > 0
            return False
        finally:
            self._limit = original_limit

    # ==================== Bulk Operations ====================

    async def bulk_create(self, models: list[BaseSurrealModel]) -> list[BaseSurrealModel]:
        """
        Create multiple records in a single operation.

        Uses the SDK's ``insert()`` method for efficient bulk insertion.

        Args:
            models: A list of model instances to create.

        Returns:
            A list of created model instances with their assigned IDs.

        Example::

            users = [User(name="Alice"), User(name="Bob"), User(name="Charlie")]
            created = await User.objects().bulk_create(users)
        """
        if not models:
            return []

        data_list = []
        for model in models:
            data = model._write_payload()
            model_id = model.get_id()
            if model_id is not None:
                data["id"] = model_id
            data_list.append(data)

        if self._tx is not None:
            rows = await self._tx.add(f"INSERT INTO {self._model_table} $data;", {"data": data_list})
            if self._tx.is_interactive and isinstance(rows, list):
                return self.model.from_db(rows)  # type: ignore
            return models  # buffered: not executed until commit; return inputs

        client = await SurrealDBConnectionManager.get_client()
        results = await client.insert(self._model_table, data_list)

        if isinstance(results, list):
            return self.model.from_db(results)  # type: ignore
        return []

    async def bulk_update(self, **kwargs: Any) -> int:
        """
        Update all records matching the current filters.

        Args:
            **kwargs: Fields and values to update.

        Returns:
            The number of updated records.

        Example::

            count = await User.objects().filter(status="pending").bulk_update(status="active")
        """
        if not kwargs:
            return 0
        self.model._reject_computed_writes(kwargs, "bulk_update()")

        where_clause, where_vars = self._build_where()

        set_parts: list[str] = []
        set_vars: dict[str, Any] = {}
        for i, (field, value) in enumerate(kwargs.items()):
            validate_field_name(field, "bulk_update field")
            var_name = f"_v{i}"
            set_parts.append(f"{field} = ${var_name}")
            set_vars[var_name] = value

        set_clause = ", ".join(set_parts)
        query = f"UPDATE {self._model_table} SET {set_clause}{where_clause};"

        all_vars = {**self._variables, **where_vars, **set_vars}
        if self._tx is not None:
            rows = await self._tx.add(query, all_vars)
            return len(rows) if isinstance(rows, list) else 0
        results = await self._execute_query(query, all_vars)
        if isinstance(results, list):
            return len(results)
        return 0

    async def bulk_delete(self) -> int:
        """
        Delete all records matching the current filters.

        Returns:
            The number of deleted records.

        Example::

            count = await User.objects().filter(status="inactive").bulk_delete()
        """
        where_clause, where_vars = self._build_where()
        query = f"DELETE {self._model_table}{where_clause} RETURN BEFORE;"
        all_vars = {**self._variables, **where_vars}
        if self._tx is not None:
            rows = await self._tx.add(query, all_vars)
            return len(rows) if isinstance(rows, list) else 0
        results = await self._execute_query(query, all_vars)
        if isinstance(results, list):
            return len(results)
        return 0

    async def patch(self, operations: list[dict[str, Any]]) -> int:
        """
        Apply a JSON Patch (RFC 6902) to every row matching the current filters.

        Whole table if unfiltered. Returns the number of affected rows. Participates in
        ``objects(tx=)`` like the other bulk operations. ``operations`` is validated then bound
        as ``$_ops`` (never string-interpolated). Emits no signals.

        Example::

            n = await User.objects().filter(status="trial").patch(
                [{"op": "replace", "path": "/plan", "value": "free"}]
            )
        """
        validate_patch_operations(operations)
        self.model._reject_computed_patch(operations, "patch()")
        where_clause, where_vars = self._build_where()
        query = f"UPDATE {self._model_table} PATCH $_ops{where_clause};"
        all_vars = {**self._variables, **where_vars, "_ops": operations}
        if self._tx is not None:
            rows = await self._tx.add(query, all_vars)
            return len(rows) if isinstance(rows, list) else 0
        results = await self._execute_query(query, all_vars)
        if isinstance(results, list):
            return len(results)
        return 0

    # ==================== Upsert / get_or_create ====================

    @staticmethod
    def _criteria_payload(criteria: dict[str, Any]) -> dict[str, Any]:
        """Field=value pairs usable as a WRITE payload, taken from EQUALITY criteria only.

        ``criteria`` keys are filter lookups (``field`` or ``field__lookup``). A literal raw
        key like ``name__contains`` must never be written as a column, so only ``exact``
        lookups contribute to the payload (their base field name → value). Non-``exact``
        lookups (``gt``/``contains``/``in``/…) still drive the lookup but cannot be written,
        so they are excluded here — mirroring Django, which only writes exact lookups on
        create.

        Values go through the same interpretation the WHERE builder applies, so the row that
        gets created is the row the next lookup finds: ``"$$literal"`` is un-escaped to
        ``"$literal"`` (otherwise ``get_or_create(name="$$admin")`` filtered on ``"$admin"``
        but stored ``"$$admin"``, never converging and creating a duplicate on every call),
        and a query-variable reference is refused outright — its value lives on the server,
        so it cannot be written back (issue #156 follow-up).
        """
        payload: dict[str, Any] = {}
        for key, value in criteria.items():
            field_name, lookup = parse_lookup(key)
            if lookup != "exact":
                continue
            if isinstance(value, Var) or (isinstance(value, str) and value.startswith("$") and value[1:2] != "$"):
                raise SurrealDbError(
                    f"Cannot create a record from the query-variable reference given for '{field_name}': "
                    "its value is only known to the database. Pass the literal value in defaults=."
                )
            if isinstance(value, str) and value.startswith("$$"):
                value = value[1:]
            payload[field_name] = value
        return payload

    async def _lookup_matches(self, criteria: dict[str, Any]) -> list[Any]:
        """Return rows matching ``criteria``, routed through the transaction when attached.

        Reuses the parameterized WHERE builder (anti-injection) via a throwaway QuerySet's
        ``filter(**criteria)``, then runs through ``_execute_query`` so the read participates
        in ``self._tx`` (``objects(tx=)``) — seeing the tx's uncommitted writes on an
        interactive transaction, and raising on a buffered one (consistent with every other
        read). ``LIMIT 2`` is enough to distinguish 0 / 1 / >1.
        """
        probe = QuerySet(self.model)
        probe.filter(**criteria)
        where_clause, where_vars = probe._build_where()
        query = f"SELECT * FROM {self._model_table}{where_clause} LIMIT 2;"
        rows = await self._execute_query(query, where_vars)
        return rows if isinstance(rows, list) else []

    def _writable_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Vet a ``*_or_create`` write payload: drop computed fields, reject undeclared ones.

        Filtering by a computed field is legitimate (it is an ordinary column to read), so a
        computed name can legitimately appear in ``criteria``. But it is server-owned and must
        not be *written*. Without this, the same call would succeed on the create path (where
        ``_write_payload`` silently drops it) and raise on the update path (where ``merge()``
        guards) — an outcome that depends only on whether the row already existed.

        A key the model does not declare used to diverge the same way, in the opposite
        direction (issue #156): Pydantic dropped it when constructing the model on the create
        path, while ``merge()`` wrote it straight to the row on the update path — so one call
        produced two different stored schemas depending on whether the record happened to
        exist. Both paths now refuse it up front, because a caller who mistypes a field name
        wants to hear about it rather than lose the value. Models that opt into extra fields
        (``model_config = ConfigDict(extra="allow")``) keep them, on both paths.
        """
        computed = self.model.get_computed_fields()
        vetted = {key: value for key, value in payload.items() if key not in computed}

        if self.model.model_config.get("extra") == "allow":
            return vetted

        declared = set(self.model.model_fields)
        undeclared = sorted(key for key in vetted if key not in declared)
        if undeclared:
            raise SurrealDbError(
                f"{self.model.__name__} does not declare {', '.join(undeclared)}; "
                "remove the key, add the field to the model, or set "
                'model_config = ConfigDict(extra="allow") to store undeclared fields.'
            )
        return vetted

    async def update_or_create(
        self,
        defaults: dict[str, Any] | None = None,
        **criteria: Any,
    ) -> tuple[Any, bool]:
        """Look up a record by ``criteria``; create it or update it; return ``(obj, created)``.

        Django-style: ``criteria`` are filters used to find the record; ``defaults`` are extra
        field values (they win on conflict). The write payload is built from the EQUALITY
        criteria fields plus ``defaults`` (non-``exact`` lookups drive the lookup only).

        - 0 matches → CREATE via ``save()`` → ``created=True``.
        - 1 match → partial UPDATE via ``merge()`` (untouched fields preserved) → ``created=False``.
        - >1 matches → ``SurrealDbError`` (the criteria are not unique).

        Writes go through ``save()``/``merge()``, so lifecycle signals fire, SDK errors are
        normalised to ``SurrealDbError``, the primary key anchors identity, and — under
        ``objects(tx=)`` — the operation participates in the transaction. The lookup and the
        write are two round-trips (no server-side locking on SurrealDB 2.6.x), so a small race
        window exists, the same non-atomic fallback Django documents. ``criteria`` must be
        non-empty.

        Create builds ``self.model(**payload)``: a key the model does not declare raises
        ``SurrealDbError`` (unless the model sets ``extra="allow"``), and a missing required
        field raises ``ValidationError`` at construction.
        """
        if not criteria:
            raise SurrealDbError("update_or_create() requires at least one lookup criteria.")
        defaults = defaults or {}
        matches = await self._lookup_matches(criteria)
        if len(matches) > 1:
            raise SurrealDbError("update_or_create() matched multiple records; the lookup criteria are not unique.")
        payload = self._writable_payload({**self._criteria_payload(criteria), **defaults})
        if not matches:
            obj: Any = self.model(**payload)
            await obj.save(tx=self._tx)
            return obj, True
        obj = self.model.from_db(matches[0])
        await obj.merge(tx=self._tx, **payload)
        return obj, False

    async def get_or_create(
        self,
        defaults: dict[str, Any] | None = None,
        **criteria: Any,
    ) -> tuple[Any, bool]:
        """Look up a record by ``criteria``; return it, or create it; return ``(obj, created)``.

        Django-style: ``criteria`` are filters. Unlike ``update_or_create``, ``defaults`` are
        applied ONLY when creating; an existing match is returned untouched.

        - 0 matches → CREATE (equality criteria + ``defaults``) via ``save()`` → ``created=True``.
        - 1 match → returned as-is (no write) → ``created=False``.
        - >1 matches → ``SurrealDbError`` (the criteria are not unique).

        The create goes through ``save()`` (signals, error normalisation, PK identity, and
        ``objects(tx=)`` participation); the lookup routes through the transaction too.
        ``criteria`` must be non-empty.

        Create builds ``self.model(**payload)``: a key the model does not declare raises
        ``SurrealDbError`` (unless the model sets ``extra="allow"``), and a missing required
        field raises ``ValidationError`` at construction.
        """
        if not criteria:
            raise SurrealDbError("get_or_create() requires at least one lookup criteria.")
        defaults = defaults or {}
        matches = await self._lookup_matches(criteria)
        if len(matches) > 1:
            raise SurrealDbError("get_or_create() matched multiple records; the lookup criteria are not unique.")
        if matches:
            return self.model.from_db(matches[0]), False
        payload = self._writable_payload({**self._criteria_payload(criteria), **defaults})
        obj = self.model(**payload)
        await obj.save(tx=self._tx)
        return obj, True

    # ==================== Custom Query ====================

    async def query(self, query: str, variables: dict[str, Any] | None = None) -> Any:
        """
        Execute a custom SQL query on the SurrealDB database.

        Args:
            query: The custom SQL query string to execute.
            variables: A dictionary of variables to substitute into the query.

        Returns:
            The result of the query, typically model instances.
        """
        if f"FROM {self._model_table}" not in query:
            raise SurrealDbError(f"The query must include 'FROM {self._model_table}' to reference the correct table.")
        # Same silent truncation raw_query() warns about: only the first statement's results
        # come back from the SDK (issue #156).
        warn_on_multiple_statements(query)
        client = await SurrealDBConnectionManager.get_client()
        try:
            # User-authored SurrealQL is sent verbatim: the caller owns that string and
            # binds their own variables, so rewriting quoted "$word" occurrences inside it
            # would corrupt their string literals (issue #156).
            results = await client.query(query, variables or {})
        except NotFoundError:
            # SurrealDB 3.x raises for a never-created table; the ORM contract
            # treats a missing table as empty.
            return self.model.from_db([])
        if isinstance(results, list):
            return self.model.from_db(results)
        data = cast(dict, results[0])
        return self.model.from_db(data.get("result", []))
