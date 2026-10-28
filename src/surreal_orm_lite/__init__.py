__version__ = "0.37.2"

from ._sdk import BoundExcluded, BoundIncluded
from .aggregations import Aggregation, Avg, Count, Max, Min, Sum
from .auth import AuthTokens
from .cache import QueryCache
from .changefeed import ChangeModelStream
from .concurrency import is_conflict_error, retry_on_conflict
from .connection_config import ConnectionConfig
from .connection_manager import SurrealDBConnectionManager
from .edge import BaseSurrealEdge
from .enum import LiveAction, OrderBy
from .exceptions import (
    InvalidDurationError,
    InvalidGeometryError,
    SurrealDbAuthenticationError,
    SurrealDbConflictError,
    SurrealDbConnectionError,
    SurrealDbError,
    SurrealDbNotFoundError,
    SurrealDbValidationError,
    SurrealORMError,
)
from .functions import (
    Computed,
    SurrealArrayFunction,
    SurrealCryptoFunction,
    SurrealFunc,
    SurrealFunction,
    SurrealMathFunction,
    SurrealRandFunction,
    SurrealStringFunction,
    SurrealTimeFunction,
    Var,
    computed,
)
from .geo import (
    GeoDistance,
    GeoField,
    Geometry,
    GeometryCollection,
    GeometryCollectionField,
    GeometryField,
    GeometryLine,
    GeometryMultiLine,
    GeometryMultiPoint,
    GeometryMultiPolygon,
    GeometryPoint,
    GeometryPolygon,
    LineStringField,
    MultiLineStringField,
    MultiPointField,
    MultiPolygonField,
    PointField,
    PolygonField,
)
from .introspection import (
    AccessState,
    AnalyzerState,
    DatabaseIntrospector,
    EventState,
    FieldState,
    FunctionState,
    IndexState,
    ModelCodeGenerator,
    ParamState,
    SchemaIssue,
    SchemaState,
    TableState,
    generate_models_from_db,
    schema_diff,
)
from .live import LiveModelStream, LiveStream, ModelChangeEvent, ReconnectCallback
from .model_auth import AuthenticatedUserMixin, AuthResult
from .model_base import BaseSurrealModel, SurrealConfigDict, get_registered_models
from .q import Q
from .query_set import QuerySet
from .schema import Event, function_ddl
from .search import Analyzer, Index, SearchHighlight, SearchOffsets, SearchScore
from .signals import (
    AroundSignal,
    Signal,
    around_delete,
    around_save,
    around_update,
    post_delete,
    post_live_change,
    post_save,
    post_update,
    pre_delete,
    pre_save,
    pre_update,
)
from .subquery import Subquery
from .transaction import BufferedTransaction, InteractiveTransaction, Transaction
from .types import Duration, Range, SchemaMode, TableType
from .vector import Vector, VectorField

# isort: split
# The migrations (v0.38.0) last, after every other import: their modules import the core modules above, which must be
# fully initialised first; no core module imports them at module level (no import cycle).
from .migrations import DefineAnalyzer, DefineEvent, RebuildIndex, RemoveEvent

__all__ = [
    "__version__",
    # Connection
    "SurrealDBConnectionManager",
    "ConnectionConfig",
    "Transaction",
    "BufferedTransaction",
    "InteractiveTransaction",
    # Model
    "BaseSurrealModel",
    "BaseSurrealEdge",
    "SurrealConfigDict",
    "TableType",
    "SchemaMode",
    "get_registered_models",
    # QuerySet
    "QuerySet",
    "Q",
    "Subquery",
    "OrderBy",
    # Live queries
    "LiveAction",
    "LiveStream",
    "LiveModelStream",
    "ModelChangeEvent",
    "ReconnectCallback",
    "ChangeModelStream",
    # Aggregations
    "Aggregation",
    "Count",
    "Sum",
    "Avg",
    "Min",
    "Max",
    # Query cache (v0.28.0)
    "QueryCache",
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
    "post_live_change",
    # Authentication
    "AuthTokens",
    "AuthResult",
    "AuthenticatedUserMixin",
    # Concurrency
    "retry_on_conflict",
    "is_conflict_error",
    # Server-side functions
    "Computed",
    "computed",
    "SurrealFunc",
    "SurrealFunction",
    "SurrealTimeFunction",
    "SurrealMathFunction",
    "SurrealStringFunction",
    "SurrealArrayFunction",
    "SurrealCryptoFunction",
    "SurrealRandFunction",
    # Query variables
    "Var",
    # Rich field types
    "Duration",
    "Range",
    "BoundIncluded",
    "BoundExcluded",
    # Geometry (v0.24.0)
    "Geometry",
    "GeometryPoint",
    "GeometryLine",
    "GeometryPolygon",
    "GeometryMultiPoint",
    "GeometryMultiLine",
    "GeometryMultiPolygon",
    "GeometryCollection",
    "GeoField",
    "GeometryField",
    "PointField",
    "LineStringField",
    "PolygonField",
    "MultiPointField",
    "MultiLineStringField",
    "MultiPolygonField",
    "GeometryCollectionField",
    "GeoDistance",
    # Schema introspection (v0.30.0)
    "DatabaseIntrospector",
    "ModelCodeGenerator",
    "generate_models_from_db",
    "SchemaState",
    "TableState",
    "FieldState",
    "IndexState",
    "EventState",
    "AccessState",
    "AnalyzerState",
    "FunctionState",
    "ParamState",
    "SchemaIssue",
    # Migrations (v0.38.0): the rest is in surreal_orm_lite.migrations
    "schema_diff",
    "DefineAnalyzer",
    "DefineEvent",
    "RebuildIndex",
    "RemoveEvent",
    # Server-side events and DDL (v0.31.0)
    "Event",
    "function_ddl",
    # Full-text search (v0.35.0)
    "SearchScore",
    "SearchHighlight",
    "SearchOffsets",
    "Analyzer",
    "Index",
    # Vector search (v0.36.0)
    "VectorField",
    "Vector",
    # Exceptions
    "SurrealORMError",
    "SurrealDbError",
    "SurrealDbConflictError",
    "SurrealDbConnectionError",
    "SurrealDbValidationError",
    "SurrealDbNotFoundError",
    "SurrealDbAuthenticationError",
    "InvalidDurationError",
    "InvalidGeometryError",
]
