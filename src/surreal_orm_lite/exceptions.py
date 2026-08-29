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


class SurrealDbNotFoundError(SurrealDbError):
    """
    Not found error.

    Raised when a requested record or resource is not found.
    Inherits from SurrealDbError for backward compatibility.
    """

    pass


class SurrealDbConflictError(SurrealDbError):
    """
    Retryable transaction conflict.

    Raised when a transaction is rolled back by SurrealDB because a concurrent
    writer modified the same data (optimistic-concurrency / version conflict).
    SurrealDB marks these as retryable ("This transaction can be retried");
    ``retry_on_conflict`` re-runs the transaction when it sees this error.

    Inherits from SurrealDbError, so existing ``except SurrealDbError`` handlers
    continue to catch it.
    """

    pass


class SurrealDbAuthenticationError(SurrealDbError):
    """
    Authentication failure.

    Raised by every connection-level auth operation — ``signin``, ``signup``,
    ``authenticate``, ``invalidate``, ``info`` — when the server (or the SDK) refuses:
    wrong credentials, an unknown access method, a malformed or expired token, a spent
    refresh token, or a request made while the session is anonymous.

    The SDK reports these inconsistently: the same wrong password surfaces as
    ``NotFoundError`` on SurrealDB 3.x and ``InternalError`` on 2.6.x, and a malformed token
    is rejected client-side as a plain ``ValueError`` before it ever reaches a server. This
    exception is the single type callers match on, so their handling does not depend on which
    DB line they happen to run against.

    Inherits from SurrealDbError, so existing ``except SurrealDbError`` handlers
    continue to catch it.
    """

    pass
