import re
from typing import Any

from .constants import LOOKUP_OPERATORS

# Pattern for valid field names: alphanumeric, underscores, dots (for nested fields)
# Must start with a letter or underscore
VALID_FIELD_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*$")

# Pattern for valid alias names: alphanumeric and underscores only
# Must start with a letter or underscore (like Python identifiers)
VALID_ALIAS_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def remove_quotes_for_variables(query: str) -> str:
    # Regex to remove single quotes around variables ($)
    return re.sub(r"'(\$[a-zA-Z_]\w*)'", r"\1", query)


def validate_field_name(field: str, context: str = "field") -> None:
    """
    Validate a field name to prevent SQL injection.

    Args:
        field: The field name to validate.
        context: Description of where the field is used (for error messages).

    Raises:
        ValueError: If the field name contains invalid characters.
    """
    if not field or not field.strip():
        raise ValueError(f"{context} name cannot be empty")
    if not VALID_FIELD_PATTERN.match(field):
        raise ValueError(
            f"Invalid {context} name '{field}': must contain only alphanumeric characters, "
            "underscores, and dots (for nested fields), and start with a letter or underscore"
        )


def validate_alias_name(alias: str) -> None:
    """
    Validate an alias name to prevent SQL injection.

    Args:
        alias: The alias name to validate.

    Raises:
        ValueError: If the alias name contains invalid characters.
    """
    if not alias or not alias.strip():
        raise ValueError("alias name cannot be empty")
    if not VALID_ALIAS_PATTERN.match(alias):
        raise ValueError(
            f"Invalid alias name '{alias}': must contain only alphanumeric characters "
            "and underscores, and start with a letter or underscore"
        )


def parse_lookup(key: str) -> tuple[str, str]:
    """
    Parse a filter key into field name and lookup type.

    Args:
        key: The filter key in the format ``field__lookup`` or just ``field``.

    Returns:
        A tuple of (field_name, lookup_type). Defaults to ``"exact"`` if no lookup is specified.
    """
    if "__" in key:
        field_name, lookup_name = key.split("__", 1)
    else:
        field_name, lookup_name = key, "exact"
    return field_name, lookup_name


def build_filter_condition(field: str, lookup: str, value: Any, counter: int) -> tuple[str, dict[str, Any], int]:
    """
    Build a single parameterized filter condition.

    Args:
        field: The field name.
        lookup: The lookup type (e.g. ``"exact"``, ``"gt"``, ``"in"``).
        value: The filter value.
        counter: The current variable counter for unique naming.

    Returns:
        A tuple of (sql_fragment, variables_dict, next_counter).

    Raises:
        ValueError: If the lookup type is not supported or the field name is invalid.
    """
    validate_field_name(field, "filter field")
    op = LOOKUP_OPERATORS.get(lookup)
    if op is None:
        raise ValueError(f"Unsupported lookup type: '{lookup}'")

    var_name = f"_f{counter}"

    if lookup == "isnull":
        if not isinstance(value, bool):
            raise ValueError(f"isnull lookup requires a boolean value, got {type(value).__name__}")
        if value:
            return f"{field} IS NULL", {}, counter
        else:
            return f"{field} IS NOT NULL", {}, counter
    elif lookup in ("in", "not_in", "containsall", "containsany"):
        if isinstance(value, (str, dict)) or not hasattr(value, "__iter__"):
            raise ValueError(f"'{lookup}' lookup requires an iterable (list, tuple, or set), got {type(value).__name__}")
        return f"{field} {op} ${var_name}", {var_name: list(value)}, counter + 1
    elif isinstance(value, str) and value.startswith("$"):
        # Backward compat: string values starting with $ are variable references
        return f"{field} {op} {value}", {}, counter
    else:
        return f"{field} {op} ${var_name}", {var_name: value}, counter + 1
