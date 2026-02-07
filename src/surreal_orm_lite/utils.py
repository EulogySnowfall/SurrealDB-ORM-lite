import re

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
