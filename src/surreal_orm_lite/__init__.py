__version__ = "0.2.2"

from .connection_manager import SurrealDBConnectionManager
from .enum import OrderBy
from .exceptions import (
    SurrealDbConnectionError,
    SurrealDbError,
    SurrealDbNotFoundError,
    SurrealDbValidationError,
    SurrealORMError,
)
from .model_base import BaseSurrealModel, SurrealConfigDict
from .query_set import QuerySet

__all__ = [
    "__version__",
    "SurrealDBConnectionManager",
    "BaseSurrealModel",
    "QuerySet",
    "OrderBy",
    "SurrealConfigDict",
    "SurrealORMError",
    "SurrealDbError",
    "SurrealDbConnectionError",
    "SurrealDbValidationError",
    "SurrealDbNotFoundError",
]
