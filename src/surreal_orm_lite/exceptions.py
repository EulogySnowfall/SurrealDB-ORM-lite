"""
Custom exceptions for SurrealDB ORM.

These exceptions provide a consistent error handling interface
for the ORM, independent of the underlying SDK error types.
"""


class SurrealORMError(Exception):
    """Base exception for all SurrealDB ORM errors."""

    pass


class SurrealDbError(SurrealORMError):
    """
    General database error.

    Raised when a database operation fails for reasons
    like invalid data, constraint violations, etc.
    """

    pass


class SurrealDbConnectionError(SurrealORMError):
    """
    Connection error.

    Raised when the connection to SurrealDB cannot be established
    or when an existing connection is lost.
    """

    pass


class SurrealDbValidationError(SurrealORMError):
    """
    Validation error.

    Raised when data validation fails before sending to the database.
    """

    pass


class SurrealDbNotFoundError(SurrealORMError):
    """
    Not found error.

    Raised when a requested record or resource is not found.
    """

    pass
