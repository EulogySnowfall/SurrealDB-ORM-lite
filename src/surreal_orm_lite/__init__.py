__version__ = "0.11.6"

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
from .q import Q
from .query_set import QuerySet
from .signals import (
    AroundSignal,
    Signal,
    around_delete,
    around_save,
    around_update,
    post_delete,
    post_save,
    post_update,
    pre_delete,
    pre_save,
    pre_update,
)
from .transaction import BufferedTransaction, InteractiveTransaction, Transaction

__all__ = [
    "__version__",
    # Connection
    "SurrealDBConnectionManager",
    "Transaction",
    "BufferedTransaction",
    "InteractiveTransaction",
    # Model
    "BaseSurrealModel",
    "SurrealConfigDict",
    # QuerySet
    "QuerySet",
    "Q",
    "OrderBy",
    # Aggregations
    "Aggregation",
    "Count",
    "Sum",
    "Avg",
    "Min",
    "Max",
    # Signals
    "Signal",
    "AroundSignal",
    "pre_save",
    "post_save",
    "pre_update",
    "post_update",
    "pre_delete",
    "post_delete",
    "around_save",
    "around_update",
    "around_delete",
    # Exceptions
    "SurrealORMError",
    "SurrealDbError",
    "SurrealDbConnectionError",
    "SurrealDbValidationError",
    "SurrealDbNotFoundError",
]
