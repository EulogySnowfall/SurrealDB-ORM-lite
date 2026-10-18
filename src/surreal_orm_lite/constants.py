#: The lookups that compile to a SurrealQL infix operator (``field <op> $_f<n>``). With
#: ``GEOMETRY_LOOKUP_OPERATORS`` and ``TEXT_LOOKUP_FUNCTIONS`` it is the list of valid lookups.
LOOKUP_OPERATORS = {
    "exact": "=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "in": "IN",
    "not_in": "NOT IN",
    "contains": "CONTAINS",
    "not_contains": "CONTAINSNOT",
    "containsall": "CONTAINSALL",
    "containsany": "CONTAINSANY",
    "isnull": "IS",
}

#: The text lookups (#251): SurrealQL has no infix operator for them (``LIKE``, ``STARTSWITH``, ``REGEX``… are parse
#: errors on 2.x and 3.x), so each compiles to the string function named here, guarded by the field's type:
#: ``(name.is_string() AND string::starts_with(name, $_f0))`` (``utils._text_condition``). ``like``/``ilike`` bind
#: their pattern translated to a regular expression, ``match``/``regex`` theirs as it is.
TEXT_LOOKUP_FUNCTIONS = {
    "startswith": "string::starts_with",
    "endswith": "string::ends_with",
    "like": "string::matches",
    "ilike": "string::matches",
    "match": "string::matches",
    "regex": "string::matches",
}

#: The spatial lookups (v0.24.0): SurrealQL's geometry operators, each taking one geometry. They live
#: apart from ``LOOKUP_OPERATORS`` because their value is a geometry, never a list or a string, and
#: ``build_filter_condition`` reads this mapping first.
GEOMETRY_LOOKUP_OPERATORS = {
    "inside": "INSIDE",
    "outside": "OUTSIDE",
    "intersects": "INTERSECTS",
}

#: SurrealDB's session parameters (v0.28.1): ``$access``, ``$auth``, ``$session`` and ``$token``. The server sets them
#: for every session and refuses ``LET`` on them (its protected parameters; measured on 2.7.0 and 3.3.4), so a filter
#: reads them with ``Var("auth")`` and no ``variables()``. A ``str`` filter value ``"$auth"`` is data, with a
#: ``UserWarning`` (``utils.warn_on_variable_names``). The statement parameters (``$this``, ``$parent``, ``$value``,
#: ``$before``, ``$after``, ``$input``, ``$event``) and the 1.x ``$scope`` are not session values: not listed.
SESSION_PARAMETERS = frozenset({"access", "auth", "session", "token"})
