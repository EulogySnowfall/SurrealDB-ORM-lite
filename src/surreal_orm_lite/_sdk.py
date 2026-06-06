"""Single import surface for the external `surrealdb` SDK.

Centralizing SDK imports here keeps the rest of the ORM decoupled from the
SDK's module layout and makes a future SDK upgrade a one-file change.
"""

from surrealdb import (  # noqa: F401
    AlreadyExistsError,
    AsyncSurreal,
    NotFoundError,
    RecordID,
    ServerError,
    SurrealError,
)

__all__ = [
    "AsyncSurreal",
    "RecordID",
    "SurrealError",
    "ServerError",
    "AlreadyExistsError",
    "NotFoundError",
]
