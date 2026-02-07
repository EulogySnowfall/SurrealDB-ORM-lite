__version__ = "0.3.0"

from .aggregations import Aggregation, Avg, Count, Max, Min, Sum
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
    # Connection
    "SurrealDBConnectionManager",
    # Model
    "BaseSurrealModel",
    "SurrealConfigDict",
    # QuerySet
    "QuerySet",
    "OrderBy",
    # Aggregations
    "Aggregation",
    "Count",
    "Sum",
    "Avg",
    "Min",
    "Max",
    # Exceptions
    "SurrealORMError",
    "SurrealDbError",
    "SurrealDbConnectionError",
    "SurrealDbValidationError",
    "SurrealDbNotFoundError",
]
