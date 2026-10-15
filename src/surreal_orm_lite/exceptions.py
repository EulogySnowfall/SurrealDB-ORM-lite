"""
Custom exceptions for SurrealDB ORM.

These exceptions provide a consistent error handling interface
for the ORM, independent of the underlying SDK error types.

``InvalidDurationError`` (v0.23.0) and ``InvalidGeometryError`` (v0.24.0) are the two exceptions
built on SDK classes: each carries the SDK's name and derives from the SDK's class
(``surrealdb.InvalidDurationError``, ``surrealdb.InvalidGeometryError``), so code written against
the SDK's ``Duration`` or geometries catches the ORM's refusals too.
"""

from typing import TYPE_CHECKING

from ._sdk import SdkInvalidDurationError, SdkInvalidGeometryError


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


# The SDK's class comes first: a 2.x SDK release may declare its own class a ``ValueError`` too, and
# with ``ValueError`` first no consistent MRO would exist (``import surreal_orm_lite`` would fail).
if TYPE_CHECKING or SdkInvalidDurationError is not None:

    class InvalidDurationError(SdkInvalidDurationError, ValueError):
        """A value ``Duration`` refuses: a ``ValueError`` and a ``surrealdb.InvalidDurationError``.

        Raised by ``Duration(...)``, ``Duration.parse(...)`` and ``Duration.from_timedelta(...)`` for a
        refused string, more than 256 characters, a negative value or a total beyond
        ``Duration.MAX_NANOSECONDS``. A wrong argument *type* stays a ``TypeError``.
        """

else:  # pragma: no cover - only with an SDK that has no InvalidDurationError

    class InvalidDurationError(ValueError):
        """A value ``Duration`` refuses (the installed SDK has no ``InvalidDurationError``)."""


# Same construction as ``InvalidDurationError``: the SDK's class first, for the same MRO reason.
if TYPE_CHECKING or SdkInvalidGeometryError is not None:

    class InvalidGeometryError(SdkInvalidGeometryError, ValueError):
        """A geometry the ORM refuses: a ``ValueError`` and a ``surrealdb.InvalidGeometryError``.

        Raised by the geometry classes (``GeometryPoint(...)`` … ``GeometryCollection(...)`` and their
        ``parse_coordinates()``) for a non-finite coordinate or one too large for a ``float``, a polygon
        ring with fewer than 4 points or whose first point is not its last, and collections nested
        deeper than 32 levels; by ``Geometry.from_geojson(...)`` for malformed GeoJSON; when an SDK
        geometry given as a component holds such a value; and for such a point given to
        ``QuerySet.nearby()`` or ``GeoDistance``. A wrong argument *type* stays a ``TypeError``, and a
        geometry field reports both as a ``pydantic.ValidationError``. The SDK geometries SurrealDB
        refuses are refused by the encoding boundary with a plain ``ValueError``, not this class.
        """

else:  # pragma: no cover - only with an SDK that has no InvalidGeometryError

    class InvalidGeometryError(ValueError):
        """A geometry the ORM refuses (the installed SDK has no ``InvalidGeometryError``)."""
