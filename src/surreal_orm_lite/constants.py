LOOKUP_OPERATORS = {
    "exact": "=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "in": "IN",
    "not_in": "NOT IN",
    "like": "LIKE",
    "ilike": "ILIKE",
    "contains": "CONTAINS",
    "not_contains": "CONTAINSNOT",
    "containsall": "CONTAINSALL",
    "containsany": "CONTAINSANY",
    "startswith": "STARTSWITH",
    "endswith": "ENDSWITH",
    "match": "MATCH",
    "regex": "REGEX",
    "isnull": "IS",
}

#: The spatial lookups (v0.24.0): SurrealQL's geometry operators, each taking one geometry. They live
#: apart from ``LOOKUP_OPERATORS`` because their value is a geometry, never a list or a string, and
#: ``build_filter_condition`` reads this mapping first.
GEOMETRY_LOOKUP_OPERATORS = {
    "inside": "INSIDE",
    "outside": "OUTSIDE",
    "intersects": "INTERSECTS",
}
