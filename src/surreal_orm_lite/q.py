from __future__ import annotations

from typing import Any

from .utils import build_filter_condition, parse_lookup


class Q:
    """
    Composable query expression for building complex filters with OR, AND, and NOT operators.

    Q objects can be combined using ``|`` (OR), ``&`` (AND), and ``~`` (NOT) operators
    to create complex query filters that go beyond simple AND-joined kwargs.

    Example::

        # OR query
        Q(name="alice") | Q(email__contains="alice")

        # NOT query
        ~Q(status="banned")

        # Complex combination
        Q(age__gte=18) & Q(age__lte=65) & (Q(role="admin") | Q(role="mod"))
    """

    AND = "AND"
    OR = "OR"

    def __init__(self, **kwargs: Any) -> None:
        self.filters: dict[str, Any] = kwargs
        self.children: list[Q] = []
        self.connector: str = self.AND
        self.negated: bool = False

    def __or__(self, other: Q) -> Q:
        if not isinstance(other, Q):
            return NotImplemented
        node = Q()
        node.children = [self, other]
        node.connector = self.OR
        return node

    def __and__(self, other: Q) -> Q:
        if not isinstance(other, Q):
            return NotImplemented
        node = Q()
        node.children = [self, other]
        node.connector = self.AND
        return node

    def __invert__(self) -> Q:
        clone = Q(**self.filters)
        clone.children = list(self.children)
        clone.connector = self.connector
        clone.negated = not self.negated
        return clone

    def __repr__(self) -> str:
        parts = []
        if self.filters:
            parts.append(f"filters={self.filters}")
        if self.children:
            parts.append(f"children={self.children}")
        if self.connector != self.AND:
            parts.append(f"connector={self.connector}")
        if self.negated:
            parts.append("negated=True")
        return f"Q({', '.join(parts)})"

    def to_sql(self, counter: int = 0, record_table: str | None = None) -> tuple[str, dict[str, Any], int]:
        """
        Generate a parameterized SQL WHERE fragment.

        Args:
            counter: The starting variable counter for unique naming.
            record_table: The table the query runs against, propagated by the QuerySet so a
                lookup on the ``id`` column can be coerced to a ``RecordID`` (issue #159).
                ``None`` — a ``Q`` compiled on its own — leaves values untouched.

        Returns:
            A tuple of (sql_fragment, variables_dict, next_counter).
        """
        all_parts: list[str] = []
        all_variables: dict[str, Any] = {}

        # Own filters (always AND-joined within a single Q)
        if self.filters:
            filter_parts: list[str] = []
            for key, value in self.filters.items():
                field, lookup = parse_lookup(key)
                sql, vars_, counter = build_filter_condition(field, lookup, value, counter, record_table)
                filter_parts.append(sql)
                all_variables.update(vars_)
            if len(filter_parts) == 1:
                all_parts.append(filter_parts[0])
            else:
                all_parts.append(f"({' AND '.join(filter_parts)})")

        # Child Q objects
        for child in self.children:
            sql, vars_, counter = child.to_sql(counter, record_table)
            if sql:
                all_parts.append(sql)
                all_variables.update(vars_)

        if not all_parts:
            return "", {}, counter

        result = all_parts[0] if len(all_parts) == 1 else f"({' {conn} '.join(all_parts)})".format(conn=self.connector)

        if self.negated:
            result = f"NOT ({result})"

        return result, all_variables, counter
