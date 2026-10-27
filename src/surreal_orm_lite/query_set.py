import contextlib
import copy
import logging
import math
import numbers
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Self, SupportsIndex, cast
from uuid import UUID

from pydantic import BaseModel
from pydantic_core import ValidationError

from . import BaseSurrealModel, SurrealDBConnectionManager
from . import cache as _cache
from . import encoding as _encoding
from . import geo as _geo
from . import hybrid as _hybrid
from . import routing as _routing
from . import search as _search
from . import subquery as _subquery
from . import time_travel as _time_travel
from . import vector as _vector
from ._sdk import NotFoundError, RecordID, SdkGeometryPoint, ServerError
from .cache import EDGES, CacheRequest, QueryCache, follows_links, statement_key, ttl_seconds
from .changefeed import ChangeModelStream, Since
from .constants import SESSION_PARAMETERS
from .edge import _check_edge_id, insert_edges
from .embedded import history_unsupported, keeps_history
from .enum import OrderBy
from .exceptions import SurrealDbError, SurrealDbNotFoundError
from .functions import SurrealFunc, Var
from .geo import GeoDistance, GeometryPoint
from .live import LiveModelStream, LiveStream, ReconnectCallback, missing_table_error
from .model_base import _snapshot_moment
from .q import Q
from .search import SearchAnnotation, SearchHighlight, SearchOffsets, SearchScore
from .subquery import SubqueryCompiler
from .surql_literal import inline_variables
from .types import Duration
from .utils import (
    RECORD_ID_COLUMN,
    VALID_FIELD_PATTERN,
    VARIABLE_NAME_VALUE,
    as_record_value,
    build_filter_condition,
    coerce_endpoint,
    coerce_record_id,
    is_select_item,
    non_negative_int,
    parse_lookup,
    unescape_filter_value,
    validate_alias_name,
    validate_field_name,
    validate_patch_operations,
    validate_select_item,
    warn_on_multiple_statements,
    warn_on_variable_names,
)

if TYPE_CHECKING:
    from .aggregations import Aggregation

logger = logging.getLogger(__name__)

#: The values a snapshot's fields can nest a record model in (``QuerySet._mark_snapshots()``, v0.26.0).
_SNAPSHOT_HOLDERS = (BaseModel, list, tuple, set, frozenset, dict)

# The query cache's refusals and warning (v0.28.0).
_CACHE_AS_OF = "cache() cannot be combined with as_of(): a time-travel read is never cached"
_CACHE_QUERY = (
    "query() runs your own SurrealQL and is never cached: drop cache(), or cache its result yourself with "
    "QueryCache.make_key(), get() and set()"
)
_CACHE_DISABLED = "QueryCache is disabled — .cache() has no effect"


def _unwritable_variable(field: str, name: str) -> str:
    """The refusal of an ``exact`` ``Var(name)`` criterion the create of ``get_or_create()``/``update_or_create()``
    cannot write (issue #252): a session parameter, or a name no ``variables()`` of the lookup gives."""
    if name in SESSION_PARAMETERS:
        why = f"it reads SurrealDB's ${name} parameter, whose value only the database knows"
        advice = "Pass the value itself as the criterion or in defaults=."
    else:
        why = f"variables() gives no {name}, so only the database knows its value"
        advice = f"Give {name} to variables(), or pass the value itself as the criterion or in defaults=."
    return f"Cannot create a record from Var('{name}') given for '{field}': {why}. {advice}"


def _queryset_refusals(method: str) -> Callable[["QuerySet"], None]:
    """The queryset's own refusals of the write *method* (``"bulk_update()"``), for ``invalidates(refuse=…)`` (#320).

    In this order: the materialized view (W1, :meth:`BaseSurrealModel._reject_view_write`, naming
    ``objects().<method>``), then a queryset read ``as_of()`` (T5, :meth:`QuerySet._reject_time_travel`). The wrapper
    calls it with the queryset before the write's tables are noted on its ``objects(tx=…)`` transaction or put in
    flight, so a refused write leaves the transaction as it was and drops no cached read. A write decorated with it
    does not repeat these checks.
    """

    def refuse(queryset: "QuerySet") -> None:
        queryset.model._reject_view_write(f"objects().{method}")
        queryset._reject_time_travel(method)

    return refuse


def _bulk_create_arguments(arguments: Mapping[str, Any]) -> None:
    """:meth:`QuerySet.bulk_create`'s argument check, for ``invalidates(refuse_args=…)`` (#320): a snapshot (an
    instance read with ``as_of()``, v0.26.0) in ``models`` is refused, naming its class, after the queryset's own
    refusals and before anything is noted on the transaction or put in flight."""
    for model in arguments["models"]:
        moment = _snapshot_moment(model) if isinstance(model, BaseSurrealModel) else None
        if moment is not None:
            raise SurrealDbError(_time_travel.snapshot_write_refused(type(model).__name__, "bulk_create()", moment))


def _bulk_update_arguments(arguments: Mapping[str, Any]) -> None:
    """:meth:`QuerySet.bulk_update`'s argument checks, for ``invalidates(refuse_args=…)`` (#320), in this order: a
    value that is a subquery (v0.27.0), then a computed field or an edge endpoint among the fields to write."""
    values: Mapping[str, Any] = arguments["kwargs"]
    for field, value in values.items():
        if _subquery.subquery_of(value) is not None:
            raise ValueError(_subquery.value_written(field))
    arguments["self"].model._reject_computed_writes(values, "bulk_update()")


def _patch_arguments(arguments: Mapping[str, Any]) -> None:
    """:meth:`QuerySet.patch`'s argument checks, for ``invalidates(refuse_args=…)`` (#320): the JSON Patch document
    (:func:`~surreal_orm_lite.utils.validate_patch_operations`), then the computed fields and edge endpoints it
    writes."""
    operations = arguments["operations"]
    validate_patch_operations(operations)
    arguments["self"].model._reject_computed_patch(operations, "patch()")


class QuerySet:
    """
    A class used to build, execute, and manage queries on a SurrealDB table associated with a specific model.

    The ``QuerySet`` class provides a fluent interface to construct complex queries using method chaining.
    It supports selecting specific fields, filtering results, ordering, limiting, and offsetting the results.
    Additionally, it allows executing custom queries and managing table-level operations such as deletion.

    Example::

        queryset = QuerySet(UserModel)
        users = await queryset.filter(age__gt=21).order_by('name').limit(10).exec()
    """

    def __init__(self, model: type[BaseSurrealModel]) -> None:
        # Issue #327: a model a forward reference left incomplete at its class statement is completed, and its vector
        # checks run, before a query reads its fields — their refusal raises here.
        model._complete_pending_checks()
        self.model = model
        self._filters: list[tuple[str, str, Any]] = []
        self._q_filters: list[Q] = []
        self.select_item: list[str] = []
        self._limit: int | None = None
        self._offset: int | None = None
        self._order_by: str | None = None
        self._model_table: str = model.get_table_name()
        self._variables: dict = {}
        self._fetch_fields: list[str] = []
        # The first segment of every dotted column a clause names (v0.29.0, _column()): the links it may follow,
        # checked against the model's connection before a statement is sent.
        self._link_heads: set[str] = set()
        self._group_by_fields: list[str] = []
        # Every annotation, by alias: the aggregations and the per-record ones (GeoDistance, the search annotations).
        self._annotations: dict[str, Aggregation | GeoDistance | SearchAnnotation] = {}
        # GeoDistance annotations (v0.24.0), as annotate() checked them: alias -> column, reference point.
        self._geo_annotations: dict[str, tuple[str, GeometryPoint]] = {}
        # Search annotations (v0.35.0), in annotate() order: alias -> SearchScore, SearchHighlight or SearchOffsets.
        self._search_annotations: dict[str, SearchAnnotation] = {}
        # nearby() conditions (v0.24.0): column, reference point, distance in metres.
        self._nearby: list[tuple[str, GeometryPoint, float]] = []
        # search() conditions (v0.35.0): field as given, column, text; the match reference is the position.
        self._search: list[tuple[str, str, str]] = []
        # similar_to() (v0.36.0): the KNN search, checked; one per queryset (a second call replaces the first).
        self._similar: _vector.KnnSearch | None = None
        self._tx: Any = None
        # as_of() (v0.26.0): the moment a time-travel read is made at, normalised; None reads the current state.
        self._as_of: _time_travel.AsOf | None = None
        # cache() (v0.28.0): the oldest result this queryset's reads accept; None: no cache.
        self._cache: CacheRequest | None = None
        # hybrid_search() (v0.37.0): the method this queryset's read errors name; set only on the private copy it runs.
        self._refusal_subject: str | None = None

    def _manager(self) -> type[SurrealDBConnectionManager]:
        """The connection class the model's database calls use right now (v0.29.0, ``get_connection_name()``).

        Resolved when a read or a write runs, never when the queryset is built: a queryset built outside a
        ``using()`` block and run inside it uses the block's connection.

        :raises ValueError: the model's connection is not registered.
        """
        return self.model._connection_manager()

    def _py_keyed(self, row: Any) -> Any:
        """Re-key a raw result row from columns back to Python field names (v0.18.0).

        A GROUP BY row, or a projection that failed model validation, is a plain dict rather
        than a model, so nothing else would translate it: the caller wrote ``values("display")``
        or ``select("display")`` in Python names and would otherwise get ``{"display_name": …}``
        back. Annotation aliases are the caller's own words and are never renamed, even when
        one happens to spell an aliased column.
        """
        if not isinstance(row, dict):
            return row
        return {key if key in self._annotations else self.model.to_py_field(key): value for key, value in row.items()}

    def _reject_annotation_collisions(self) -> None:
        """Raise if an annotation alias shares a name with a grouped field.

        ``SELECT display_name, count() AS display_name`` returns a single key, so one of the
        two values is silently lost — and the same happens after re-keying when the alias
        spells the grouped field's Python name instead. Either spelling is refused.
        """
        for column in self._group_by_fields:
            name = self.model.to_py_field(column)
            clashing = sorted({column, name} & set(self._annotations))
            if clashing:
                raise ValueError(
                    f"annotation alias {', '.join(clashing)} collides with the grouped field "
                    f"{name!r} (column {column!r}); choose a different alias."
                )

    def _column(self, field: str) -> str:
        """Translate a Python field name to the SurrealDB column it is stored under (v0.18.0).

        Every clause this class builds names columns; every argument a caller passes names
        Python fields. This is the one boundary between the two, so an aliased model is
        addressable by its Python names throughout the query API. A model with no aliases gets
        the name back unchanged.

        The first segment of a dotted path (``author.name``) is recorded (v0.29.0): a path may follow a link, which
        :meth:`_check_links` checks before the statement is sent.
        """
        column = self.model.to_db_field(field)
        head, dot, _ = column.partition(".")
        if dot:
            self._link_heads.add(head)
        return column

    def _check_links(self) -> None:
        """Refuse a statement that follows a link to a model of another connection than this one's (v0.29.0).

        The links are the ``fetch()`` paths and the dotted paths of every other clause (:meth:`_column`, which also
        translates the ``Q`` objects and the aggregations as they compile). Called once the statement is compiled,
        before anything is sent.

        :raises SurrealDbError: a link to a model of another connection (``routing.check_links()``).
        """
        if self._fetch_fields or self._link_heads:
            _routing.check_links(self.model, [*self._fetch_fields, *sorted(self._link_heads)])

    def select(self, *fields: str) -> Self:
        """
        Specify the fields to retrieve in the query.

        By default, all fields are selected (``SELECT *``). Each item is one of:

        - ``"*"``: every field;
        - a field name, the Python one for an aliased field (its column is selected): ``"name"``, ``"id"``;
        - a dotted path into a nested object or through a record link: ``"author.name"``;
        - a path followed by ``.*``: every field of the nested object or of the linked record: ``"author.*"``.

        Names are letters, digits and underscores, each part starting with a letter or an underscore, as in
        ``order_by()`` and ``values()``. Anything else — an expression, a function call, ``AS``, ``{…}`` or
        ``[…]`` — is refused when ``select()`` is called, before any request; write a computed projection
        with :meth:`query`. Rows that do not validate as the model (a link read as a record, a required field
        left out) come back as dicts keyed by Python names. On a nested object that is not a link,
        ``"meta.*"`` gives the object on SurrealDB 3.x and its values as a list on 2.x and the embedded engine.

        Example::

            await User.objects().select("name", "email").exec()
            await Book.objects().select("title", "author.name").exec()   # [{"title": …, "author": {"name": …}}]
            await Book.objects().select("title", "author.*").exec()      # the author's whole record

        Args:
            *fields: The items to select; none selects every field.

        Returns:
            Self: The current instance for method chaining.

        Raises:
            TypeError: an item is not a ``str``.
            ValueError: an item is none of the above, or an aliased field's column is not a field name; the
                message names it. The selection in place is kept.
        """
        self.select_item = [self._select_column(field) for field in fields]
        return self

    def _select_column(self, field: str) -> str:
        """The column ``select()`` writes for *field*, both checked: *field* as given, then its column."""
        column = self._column(validate_select_item(field))
        if column != field and not is_select_item(column):
            raise ValueError(f"select() cannot select {field!r}: its column {column!r} is not a field name")
        return column

    def variables(self, **kwargs: Any) -> Self:
        """
        Set variables for the query.

        A filter reads one with ``Var("name")``: ``filter(age__gte=Var("min_age")).variables(min_age=21)``.
        A string filter value is data, whatever it starts with: ``filter(name="$min_age")`` compares ``name``
        with the string ``"$min_age"``, with a ``UserWarning`` naming ``Var("min_age")`` when ``variables()``
        gives ``min_age`` (until v0.28.1 that string read the variable).

        SurrealDB's session parameters — ``access``, ``auth``, ``session`` and ``token`` — are not variables: the
        server sets them for each session and refuses a query variable of that name, so ``variables()`` refuses them
        (v0.28.1). To pass your own value, give it another name —
        ``filter(token=Var("device_token")).variables(device_token=t)`` — or pass the value itself in the filter
        (``filter(token=t)``). ``Var("token")`` without ``variables()`` reads SurrealDB's ``$token``.

        Args:
            **kwargs: Variable names and their values.

        Returns:
            Self: The current instance for method chaining.

        Raises:
            ValueError: a name is one of the session parameters; the variables in place are kept.
        """
        for name in kwargs:
            if name in SESSION_PARAMETERS:
                raise ValueError(
                    f"variables() cannot set '{name}': SurrealDB sets ${name} for each session and refuses a query "
                    f"variable of that name. To pass your own value, give it another name (variables(my_{name}=...) "
                    f"with Var('my_{name}')) or pass the value itself in the filter; Var('{name}') without "
                    f"variables() reads SurrealDB's ${name} parameter."
                )
        self._variables.update(kwargs)
        return self

    def filter(self, *args: Q, **kwargs: Any) -> Self:
        """
        Add filter conditions to the query.

        Supports both keyword arguments for simple filters and Q objects for complex queries.

        Args:
            *args: Q objects for complex OR/AND/NOT queries.
            **kwargs: Simple filter conditions in ``field__lookup=value`` format.

        Returns:
            Self: The current instance for method chaining.

        Example::

            # Simple filters (AND-joined)
            queryset.filter(age__gt=21, status='active')

            # Q objects for OR queries
            queryset.filter(Q(name="alice") | Q(name="bob"))

            # Mixed
            queryset.filter(Q(role="admin") | Q(role="mod"), status="active")

            # A subquery (v0.27.0): another queryset's values, read by the server in the same statement
            active = Person.objects().filter(active=True)
            Post.objects().filter(author__in=Subquery(active))         # or author__in=active
            best = Post.objects().select("score").order_by("-score").limit(1)
            Post.objects().filter(score=Subquery(best))                # compared with its first value

        A ``str`` value is data, whatever it starts with: ``filter(name="$admin")`` compares ``name`` with the
        string ``"$admin"``. ``Var("name")`` is the only reference to a query variable (given with ``variables()``).
        A ``str`` that is ``$`` and the name of a variable ``variables()`` gives — ``age__lte="$max_age"`` with
        ``variables(max_age=25)``, which read ``$max_age`` until v0.28.1 — is still the string, with a
        ``UserWarning`` naming ``Var("max_age")``, once per read, when the statement is compiled.

        ``Var("auth")`` also reads SurrealDB's own session parameters, without ``variables()``:
        ``filter(owner=Var("auth"))`` keeps the records of the signed-in record user (``$auth``; ``$access``,
        ``$session`` and ``$token`` likewise), in a read and in a live query. The string ``"$auth"`` is data:
        ``filter(owner="$auth")`` compares ``owner`` with the string ``"$auth"``, with a ``UserWarning`` naming
        ``Var("auth")``. On an edge's ``in``/``out`` a ``str`` starting with ``$`` is not a record id: ``ValueError``,
        whose message names the ``Var(...)`` to use only when the value is ``$`` and the name of a variable
        ``variables()`` gives or of a session parameter (``"$auth"``).

        A ``Subquery`` — or a QuerySet, read as one — is accepted as the whole value of ``in``, ``not_in``,
        ``containsall`` and ``containsany`` (compared with all its values) and of ``exact``, ``gt``, ``gte``,
        ``lt``, ``lte``, ``contains`` and ``not_contains`` (compared with its first value; no record when it is
        empty), here and in ``Q``. The inner queryset selects its record ids, or the one field given to its
        ``select()``; its ``filter()``, ``Q``, ``nearby()``, ``search()``, ``order_by()``, ``limit()``, ``offset()``
        and ``variables()`` apply, its ``fetch()`` is ignored. It is read when this queryset runs, in the same
        statement: one request. A clause it cannot honour raises when this queryset compiles, before any request
        (see ``Subquery``). A subquery reading a table that does not exist reads as empty, as any missing table
        (SurrealDB 3.x raises: once the server confirms the table does not exist, the statement is sent once more
        with that subquery empty). Any other error raised while the subquery runs is the server's, relayed.

        The server runs a subquery in the database of the statement around it (v0.29.0): a subquery whose model runs
        on another connection than this model's raises ``SurrealDbError`` when this queryset compiles, before any
        request — read it through its own connection first, or run both inside one ``using()`` block.
        """
        for arg in args:
            if not isinstance(arg, Q):
                raise TypeError(f"filter() positional arguments must be Q objects, got {type(arg).__name__}")
            self._q_filters.append(arg)
        for key, value in kwargs.items():
            field_name, lookup = parse_lookup(key)
            self._filters.append((self._column(field_name), lookup, value))
        return self

    def limit(self, value: SupportsIndex) -> Self:
        """
        Set a limit on the number of results to retrieve.

        Args:
            value: The maximum number of records to retrieve: an integer from ``0`` (reads none) to ``2**63 - 1``,
                SurrealQL's largest integer. Any integer type gives its value: an ``int`` subclass such as an
                ``IntEnum`` member, or a type with ``__index__`` such as NumPy's ``int64``.

        Returns:
            Self: The current instance for method chaining.

        Raises:
            TypeError: *value* is a ``bool``, or not an integer (no ``__index__``: a ``str`` such as ``"10"``, a
                ``float``, ``None``) — raised when ``limit()`` is called, before any request; the limit in place is
                kept.
            ValueError: *value* is negative or above ``2**63 - 1``, likewise. SurrealDB 2.x and the embedded engine
                refuse a limit above ``2**32 - 1`` when the read runs (the server's error); SurrealDB 3.x reads it.
        """
        self._limit = non_negative_int("limit()", value)
        return self

    def offset(self, value: SupportsIndex) -> Self:
        """
        Set an offset for the results.

        Args:
            value: The number of records to skip: an integer from ``0`` to ``2**63 - 1``, SurrealQL's largest
                integer. Any integer type gives its value: an ``int`` subclass such as an ``IntEnum`` member, or a
                type with ``__index__`` such as NumPy's ``int64``.

        Returns:
            Self: The current instance for method chaining.

        Raises:
            TypeError: *value* is a ``bool``, or not an integer (no ``__index__``: a ``str`` such as ``"10"``, a
                ``float``, ``None``) — raised when ``offset()`` is called, before any request; the offset in place
                is kept.
            ValueError: *value* is negative or above ``2**63 - 1``, likewise. SurrealDB 2.x and the embedded engine
                refuse an offset above ``2**32 - 1`` when the read runs (the server's error); SurrealDB 3.x reads it.
        """
        self._offset = non_negative_int("offset()", value)
        return self

    def order_by(self, *fields: str | OrderBy) -> Self:
        """
        Set the field(s) and direction to order the results by.

        Supports ``-field`` prefix for descending order, multiple fields,
        and backward-compatible ``order_by("field", OrderBy.DESC)`` syntax.

        Args:
            *fields: Field names to sort by. Prefix with ``-`` for descending order.

        Returns:
            Self: The current instance for method chaining.

        Example::

            queryset.order_by("name")                    # ASC
            queryset.order_by("-created_at")              # DESC
            queryset.order_by("-age", "name")             # age DESC, name ASC
            queryset.order_by("name", OrderBy.DESC)       # backward compatible
        """
        order_parts: list[str] = []
        i = 0
        while i < len(fields):
            if isinstance(fields[i], OrderBy):
                raise TypeError(
                    f"order_by() expected a field name at position {i + 1}, but got an OrderBy value. "
                    'Pass the direction after a field name, e.g. order_by("field", OrderBy.DESC).'
                )
            field = str(fields[i])
            # Backward compat: check if next arg is an OrderBy direction
            if i + 1 < len(fields) and str(fields[i + 1]) in ("ASC", "DESC"):
                validate_field_name(field, "order_by field")
                order_parts.append(f"{self._column(field)} {fields[i + 1]}")
                i += 2
            elif field.startswith("-"):
                actual_field = field[1:]
                validate_field_name(actual_field, "order_by field")
                order_parts.append(f"{self._column(actual_field)} DESC")
                i += 1
            else:
                validate_field_name(field, "order_by field")
                order_parts.append(f"{self._column(field)} ASC")
                i += 1

        self._order_by = ", ".join(order_parts)
        return self

    def fetch(self, *fields: str) -> Self:
        """
        Add a FETCH clause to resolve record links inline.

        This prevents N+1 queries by fetching linked records in a single query.

        Args:
            *fields: Field names to fetch/resolve.

        Returns:
            Self: The current instance for method chaining.

        Example::

            posts = await Post.objects().fetch("author", "tags").exec()
            # Generates: SELECT * FROM Post FETCH author, tags;

        Under ``as_of()`` (v0.26.0) SurrealDB 3.x resolves the links as they were at the moment. SurrealDB 2.x
        (servers and the embedded engine of the surrealdb SDK 2.x) resolves them to their current state, so on
        2.x the reads that return the fetched records (``exec()``, ``first()``, ``get()`` without an id) raise
        ``SurrealDbError``; the other reads ignore ``fetch()``, as they do without ``as_of()``. A dotted path
        through a link in ``filter()``, ``select()``, ``order_by()`` or ``values()`` reads the linked record as
        it is now on 2.x.

        A link is resolved in the database of the statement (v0.29.0): a field typed with a model of another
        connection than this model's raises ``SurrealDbError`` when the queryset runs, before anything is sent — so
        does a dotted path through it in ``filter()``, ``Q``, ``select()``, ``order_by()`` or ``values()``. A field
        typed ``RecordID`` names no model and is not checked.
        """
        for field in fields:
            validate_field_name(field, "FETCH field")
        # Recorded whole, not through _column(): a subquery ignores its fetch(), so it must follow no link there.
        self._fetch_fields.extend(self.model.to_db_field(field) for field in fields)
        return self

    def nearby(
        self,
        field: str,
        point: GeometryPoint | SdkGeometryPoint | tuple[float, float] | Sequence[float] | Mapping[str, Any],
        max_distance: float,
    ) -> Self:
        """Keep the records whose ``field`` is a point within ``max_distance`` metres of ``point`` (v0.24.0).

        Same name and arguments as the full SurrealDB-ORM::

            restaurants = await Restaurant.objects().nearby(
                "location", (-73.98, 40.74), max_distance=5000,
            ).exec()

        ``point`` is a ``GeometryPoint`` (the ORM's or the SDK's), a ``(longitude, latitude)`` pair
        (GeoJSON order) or a GeoJSON ``Point`` object; the distance is SurrealDB's ``geo::distance``,
        in metres. Both are bound as query variables, never written into the statement. A record
        whose ``field`` is not a point (missing, ``NULL``, another geometry, a string, a GeoJSON
        object stored as a plain object) is left out, on SurrealDB 2.x and 3.x alike.

        Several calls are AND-ed, and the condition joins ``filter()`` wherever it applies:
        ``exec()``, ``first()``, ``get()`` without an id, ``count()`` and the other aggregations,
        ``values()``/``annotate()`` groups, ``bulk_update()``, ``bulk_delete()``, ``patch()``, and
        ``live()``/``watch()``. ``all()`` ignores it, as it ignores ``filter()``; ``changes()``
        refuses it.

        Args:
            field: A Python field name (an aliased field compiles its column) or a dotted path.
            point: The reference point.
            max_distance: The largest distance kept, in metres: finite and ``>= 0``.

        Returns:
            Self: The current instance for method chaining.

        Raises:
            TypeError: ``field`` is not a string; ``point`` is none of the forms above, or a pair
                that does not hold 2 real numbers; ``max_distance`` is not a real number (``bool``
                included).
            ValueError: ``field`` is not a valid field name, or the model declares it as a
                geometry field that holds no point (``PolygonField``, ``GeoField["line"]``…);
                ``max_distance`` is negative, not finite or too large for a float;
                ``InvalidGeometryError`` for a non-finite coordinate or a GeoJSON object that is
                not a valid ``Point``.
        """
        if not isinstance(field, str):
            raise TypeError(f"nearby() takes a field name (str), not {type(field).__name__}")
        validate_field_name(field, "nearby field")
        self._check_point_field(field, "nearby()")
        reference = _geo._as_point(point)
        distance = _metres(max_distance)
        self._nearby.append((self._column(field), reference, distance))
        return self

    def _check_point_field(self, field: str, caller: str) -> None:
        """Refuse a top-level field the model declares as a geometry field that holds no point.

        ``geo::distance`` measures between points. Any other declaration (``Any``, ``dict``,
        ``str``…), an undeclared name and a dotted path are accepted: the ``is_point()`` guard
        leaves out their values that are not points, as a lookup on a mismatched type matches
        nothing.
        """
        info = self.model.model_fields.get(field)
        kinds = None if info is None else _geo._geo_kinds(info.annotation)
        if kinds is not None and "point" not in kinds:
            raise ValueError(f"{caller} measures distances between points; field {field!r} is declared as {_geo._info(kinds)}")

    def search(self, **field_queries: str) -> Self:
        """Keep the records whose fields match full-text queries (v0.35.0) — the full SurrealDB-ORM's method.

        ::

            physics = await Article.objects().search(title="quantum physics").exec()
            recent = await Article.objects().filter(views__gt=10).search(title="quantum", body="physics").exec()
            rust = await Article.objects().search(**{"meta.headline": "rust"}).count()

        Each keyword maps a field — a Python field name (an aliased field compiles its column) or a dotted path — to
        the text to match. The text is bound as a query variable, never written into the statement, and is data
        whatever it holds (``"$title"`` is the text ``$title``). Each field gets the next match reference, in call
        order and across calls (``search(title="a").search(body="b")``: ``title @0@ $_f0 AND body @1@ $_f1``), the
        reference ``SearchScore(ref)`` and ``SearchHighlight(…, ref)`` name; the same field may be searched twice; at
        most 256 fields per queryset (SurrealDB's match references are 0 to 255). Without a keyword nothing changes.

        The conditions are AND-ed with each other and after ``filter()``, ``Q`` and ``nearby()``, wherever those
        apply: ``exec()``, ``first()``, ``get()`` without an id, ``count()`` and the other aggregations, ``exists()``,
        ``values()``/``annotate()`` groups, ``bulk_update()``, ``bulk_delete()``, ``patch()``, and a ``Subquery`` of
        this queryset. ``get(<id>)`` and ``all()`` ignore it, as they ignore ``filter()``. SurrealDB's analyzer decides
        what matches (case, stemming, prefixes); the terms of one text are AND-ed; an empty text matches nothing. The
        records matched are the same on every line; the scores are not always (see ``SearchScore``).

        The field needs a full-text index — ``Index(..., search_analyzer=...)`` declared on the model and applied by
        ``define_indexes()``: without one, the read raises ``SurrealDbError`` naming the field, never the server's
        text (SurrealDB 3.x quotes the search text in it). In a transaction the commit then fails with that error as
        its cause. ``live()``/``watch()`` refuse ``search()`` — search in a ``Subquery`` of the live query instead —,
        as ``as_of()`` and ``changes()`` do.

        Args:
            **field_queries: Each field to search, and the text to match in it.

        Returns:
            Self: The current instance for method chaining.

        Raises:
            TypeError: a text is not a ``str``.
            ValueError: a field is not a field name or a dotted path; it is the record id (``id``) or, on an edge
                model, an endpoint (``in``/``out``); the queryset would search more than 256 fields.
            SurrealDbError: the queryset reads ``as_of()``.
            Each raised before any request, the queryset left unchanged.
        """
        endpoint_columns = self.model._endpoint_columns()
        conditions: list[tuple[str, str]] = []
        for field, text in field_queries.items():
            _search.checked_search_field(field)
            column = self.model.to_db_field(field)
            if column == RECORD_ID_COLUMN or column in endpoint_columns:
                raise ValueError(_search.field_refused(field, endpoint=column in endpoint_columns))
            if not isinstance(text, str):
                raise TypeError(_search.text_refused(field, text))
            conditions.append((field, text))
        if len(self._search) + len(conditions) > _search.MAX_SEARCH_FIELDS:
            raise ValueError(_search.too_many_fields())
        if conditions and self._as_of is not None:
            raise SurrealDbError(_search.time_travel_refused())
        self._search.extend((field, self._column(field), text) for field, text in conditions)
        return self

    def similar_to(
        self,
        field: str,
        vector: Sequence[float],
        limit: int = 10,
        *,
        ef: int | None = None,
        dist: str | None = None,
    ) -> Self:
        """Keep the ``limit`` records whose ``field`` vector is nearest to ``vector`` (v0.36.0) — the full
        SurrealDB-ORM's method.

        ::

            docs = await Document.objects().similar_to("embedding", query_vector, limit=5).exec()
            for doc in docs:
                print(doc.title, doc._knn_distance)

            exact = await Document.objects().similar_to("embedding", query_vector, 5, dist="cosine").exec()
            tech = await Document.objects().filter(category="tech").similar_to("embedding", query_vector, 3).exec()

        SurrealDB's KNN operator, ``<column> <|K,EF|> $vector``: an HNSW search through the field's vector index
        (``Index(..., hnsw=True, dimension=...)`` declared on the model and applied by ``define_indexes()``; on a field
        without one it finds no record). With ``dist=`` it is ``<column> <|K,DIST|> $vector`` instead, an exact scan of
        every record by that distance, which needs no index. The query vector is bound as a query variable, never
        written into the statement.

        ``exec()``, ``first()`` and ``get()`` without an id return the records nearest first — unless ``order_by()`` is
        given, which replaces that order (``order_by("-_knn_distance")`` included) — each instance carrying its
        distance from ``vector`` as ``_knn_distance`` (a ``float``; an ``int`` with ``HAMMING``), which is never written
        back: ``model_dump()`` and ``save()`` leave it out, on an ``extra="allow"`` model too. Rows that do not validate
        (a ``select()`` that leaves out a required field) come back as dicts holding it. ``limit()`` and ``offset()``
        then page through the ``limit`` nearest records.

        HNSW is approximate: SurrealDB explores ``ef`` candidates (100 by default), and the ORM writes ``ef`` raised to
        ``limit``, so without other conditions every line returns ``limit`` records when the table holds them. With a
        condition that keeps few records the search can return fewer than ``limit``, more often on SurrealDB 2.x and the
        embedded engine; ``dist=`` is exact. Distances are computed in the index's storage type (``F32`` by default),
        ``dist=`` in double precision. Records at an equal distance at the ``limit``-th place are kept, and returned, by
        id on SurrealDB 2.x and the embedded engine, whose own exact scan picks among them in no fixed order: the ORM
        ranks the scan by distance then id there (SurrealDB 3.x's scan gives one answer), so identical calls, a page of
        them and the bulk writes take the same records.

        The other conditions — ``filter()``, ``Q``, ``nearby()``, ``search()`` and a ``Subquery`` filter — select the
        records first, then the ``limit`` nearest among them are kept: SurrealDB 3.x applies them during the index
        search; on SurrealDB 2.x and the embedded engine the ORM selects the records matching them first, in a ``LET``
        of the statement. The search annotations (``SearchScore``, ``SearchHighlight``, ``SearchOffsets``) of a
        ``search()`` are accepted in either order; on SurrealDB 3.x each of the nearest matches carries the values the
        plain search gives it, while on SurrealDB 2.x and the embedded engine ``exec()``, ``first()`` and ``get()``
        without an id refuse them before the statement is sent. ``count()``, the other aggregations, ``exists()`` and
        ``values()``/``annotate()`` groups count or group the ``limit`` nearest records, with no distance;
        ``bulk_update()``, ``bulk_delete()`` and ``patch()`` write them, by their ids; a ``Subquery`` of this queryset
        gives them. ``get(<id>)`` and ``all()`` ignore it, as they ignore ``filter()``. ``as_of()``, ``live()``,
        ``watch()`` and ``changes()`` refuse it.

        When the model does not declare the field's dimension, SurrealDB checks ``vector`` against the vector index: a
        vector of another length raises ``SurrealDbError`` naming the field and both lengths, caused by the server's
        error — on a read, in a transaction too, and on a bulk write outside a transaction. Inside a transaction a bulk
        write relays the server's error, as the transaction reports it.

        Args:
            field: A Python field name (an aliased field compiles its column) or a dotted path; not the record id nor,
                on an edge model, an endpoint.
            vector: The query vector: a ``list`` or ``tuple`` of real numbers (``int``, ``float``, any
                ``numbers.Real``, a ``Decimal``; not a ``bool`` nor a ``str``), not empty, each finite — a NumPy array
                with ``.tolist()``. When the model declares the field's dimension (a ``VectorField[d]`` or
                ``Vector(d)`` on it, or a vector ``Index`` on its column), exactly ``d`` numbers.
            limit: ``K``, the number of nearest records kept: an ``int`` from 1 to 10000.
            ef: The HNSW search's candidate list: ``None`` (100) or an ``int`` from 1 to 10000.
            dist: An exact scan by this distance instead of the index's search: ``EUCLIDEAN``, ``COSINE``,
                ``MANHATTAN``, ``CHEBYSHEV`` or ``HAMMING``, in any case; not with ``ef``.

        Returns:
            Self: The current instance for method chaining. Called again, the last call wins.

        Raises:
            TypeError: ``field`` is not a ``str``; ``vector`` is not a ``list`` or a ``tuple``, or holds an item that is
                not a number; ``limit`` or ``ef`` is not an ``int`` (a ``bool`` included); ``dist`` is not a ``str``.
            ValueError: ``field`` is not a field name or a dotted path, or is the record id or an edge endpoint; the
                model defines ``_knn_distance``; ``vector`` is empty, holds an item that is not finite, or its length
                is not the field's declared dimension; ``limit`` or ``ef`` is outside 1 to 10000; both ``ef`` and
                ``dist``; a distance the ORM does not write; a zero vector with ``COSINE`` (``dist="cosine"``, or no
                ``dist`` and the field's vector index is ``COSINE``), which has no distance.
            SurrealDbError: the queryset reads ``as_of()`` (checked last).
            Each raised before any request, the queryset left unchanged.
        """
        _vector.checked_knn_field(field)
        column = self.model.to_db_field(field)
        endpoint_columns = self.model._endpoint_columns()
        if column == RECORD_ID_COLUMN or column in endpoint_columns:
            raise ValueError(_vector.field_refused(field, endpoint=column in endpoint_columns))
        if hasattr(self.model, _vector.DISTANCE_ALIAS):
            raise ValueError(_vector.distance_attribute_refused(self.model.__name__))
        knn = self._knn_search(field, column, vector, limit, ef, dist)
        if self._as_of is not None:
            raise SurrealDbError(_vector.time_travel_refused())
        self._column(field)
        self._similar = knn
        return self

    def _knn_search(
        self,
        field: str,
        column: str,
        vector: Sequence[float],
        limit: int,
        ef: int | None,
        dist: str | None,
        *,
        subject: str = "similar_to()",
        limit_arg: str = "limit",
    ) -> _vector.KnnSearch:
        """The vector search of *field* (stored as *column*, already checked) by its arguments, each checked in a fixed
        order — the vector, its ``limit``, ``ef``, ``ef`` with ``dist``, ``dist``, the declared dimension, a zero vector
        with ``COSINE`` — for ``similar_to()`` (v0.36.0) and ``hybrid_search()`` (v0.37.0). Changes nothing: the caller
        records *field*'s link head and keeps the search.

        Args:
            field: the field as the caller gave it, which the refusals name.
            column: the column it is stored under.
            vector: the query vector.
            limit: ``K``, the number of nearest records.
            ef: the HNSW search's candidate list, or ``None``.
            dist: the distance of an exact scan, or ``None``.
            subject: the method each refusal starts with.
            limit_arg: the name the refusal of *limit* gives it.

        Raises:
            TypeError: ``vector`` is not a ``list`` or a ``tuple``, or holds an item that is not a number; *limit* or
                *ef* is not an ``int`` (a ``bool`` included); *dist* is not a ``str``.
            ValueError: ``vector`` is empty, holds an item that is not finite, or its length is not the field's
                declared dimension; *limit* or *ef* is outside 1 to 10000; both *ef* and *dist*; a distance the ORM
                does not write; a zero vector with ``COSINE`` (*dist*, or else the field's vector index).
        """
        values = _vector.checked_query_vector(vector, subject=subject)
        k = _vector.checked_limit(limit, subject=subject, arg=limit_arg)
        candidates = _vector.checked_ef(ef, subject=subject)
        if candidates is not None and dist is not None:
            raise ValueError(_vector.ef_and_dist_refused(subject=subject))
        distance = None if dist is None else _vector.checked_distance(dist, subject=subject)
        index = self._vector_index(column)
        info = self.model.model_fields.get(field)
        declared = None if info is None else _vector.get_vector_info(info)
        if declared is not None:
            dimension, source = declared[0], f"VectorField[{declared[0]}]"
        elif index is not None:
            dimension, source = index._hnsw_options()[0], f"index {index.name!r}"
        else:
            dimension, source = len(values), ""
        if len(values) != dimension:
            raise ValueError(_vector.dimension_refused(field, len(values), dimension, source, subject=subject))
        metric = distance if distance is not None or index is None else index._hnsw_options()[1]
        if metric == "COSINE" and all(item == 0.0 for item in values):
            raise ValueError(_vector.zero_vector_refused(subject=subject))
        return _vector.KnnSearch(field, column, tuple(values), k, candidates, distance)

    def _vector_index(self, column: str) -> _search.Index | None:
        """The vector index the model declares on *column*, or ``None`` (v0.36.0; at most one, checked when the class
        was defined)."""
        for index in self.model.get_indexes():
            if index.hnsw and self.model.to_db_field(index.fields[0]) == column:
                return index
        return None

    def as_of(self, at: datetime | str | timedelta | Duration | SurrealFunc | None) -> Self:
        """Read the records as they were at a moment: SurrealQL's ``VERSION`` (v0.26.0).

        ::

            from datetime import UTC, datetime, timedelta

            then = await Order.objects().as_of(datetime(2026, 1, 1, tzinfo=UTC)).filter(status="open").count()
            yesterday = await User.objects().as_of(timedelta(days=1)).get("alice")   # deleted since? still found
            history = await User.objects().as_of("2026-09-30T18:00:00Z").order_by("name").exec()

        ``at`` is an aware ``datetime`` (any time zone), a naive one (UTC — the rule for every datetime the ORM
        sends), an ISO-8601 string (parsed here by ``datetime.fromisoformat``; naive means UTC, and a date alone
        such as ``"2026-01-01"`` is that day's midnight UTC — a ``date`` object is refused), a ``timedelta`` or
        an ORM ``Duration`` >= 0 meaning *that long before the database's clock* (the surrealdb SDK's
        ``Duration`` is accepted too, at runtime only: the annotation names the ORM's types, so a type checker
        flags it — convert it with ``Duration(sdk_duration.elapsed)``), a ``SurrealFunc`` the database
        evaluates to a datetime
        (``SurrealFunc("time::floor(time::now(), 1d)")``; it may reference ``variables()``), or ``None`` to
        read the current state again. A duration or an expression is resolved once per read, by the database,
        just before it.

        The moment must lie between 1970-01-01T00:00:00Z and 2262-04-11T23:47:16.854775Z, the instants
        SurrealDB versions can address (nanoseconds since 1970 in a signed 64-bit integer). SurrealDB 2.x reads
        an earlier moment as the *current* state and every line fails after the later one, so the ORM refuses
        both: an absolute moment here (``ValueError``), a resolved one before the read (``SurrealDbError``).

        Every read honours it: ``exec()``, ``first()``, ``get()`` (by id too: a record deleted since is still
        found, one created later is not), ``all()``, ``exists()``, ``count()``, ``sum()``/``avg()``/``min()``/
        ``max()``, ``values()``+``annotate()`` and ``GeoDistance`` annotations, with ``filter()``, ``Q``,
        ``nearby()``, ``order_by()``, ``limit()``, ``offset()``, ``select()`` and ``variables()``. Every subquery
        of the filter (``Subquery``, v0.27.0) reads at the same moment — the ORM gives each its own ``VERSION``,
        which the server would not — and a subquery cannot carry its own ``as_of()`` (``SurrealDbError``). The
        returned instances are **snapshots**: their writes raise ``SurrealDbError``; ``refresh()`` loads the
        current record into one (on a model mixing ``AuthenticatedUserMixin``, whose classmethod
        ``refresh(token)`` takes the name, call ``BaseSurrealModel.refresh(snapshot)``).

        The store must keep history: on the embedded engine a ``surrealkv+versioned://<path>`` database; on a
        server, versioned storage (3.x: ``surrealkv://…?versioned=true`` or ``rocksdb://…?versioned=true``;
        2.x: ``surrealkv+versioned://…``). Otherwise the embedded engine raises before any request and a 3.x
        server's refusal is raised as ``SurrealDbError`` (with a subquery, SurrealDB 3.1 fails first with an
        error of its own that does not say why, relayed as is); a 2.x server without versioned storage answers
        without an error and only returns records last written at or before the moment, in their current state
        (it cannot be detected: see the README).

        ``fetch()`` resolves links as they were on SurrealDB 3.x; on 2.x (servers and the embedded engine of the
        surrealdb SDK 2.x) the reads that return the fetched records (``exec()``, ``first()``, ``get()``
        without an id) refuse it, the server resolving them to their current state. A dotted path through a
        link in ``filter()``, ``select()``, ``order_by()`` or ``values()`` reads the linked record as it was on
        3.x and as it is now on 2.x (see the README).

        ``search()`` (v0.35.0) is refused with a moment, whichever comes first: a full-text match reads the index,
        which holds the records' current text, so the read would select records by their text now. A ``Subquery``
        holding ``search()`` is refused under an outer ``as_of()`` when the statement compiles, before any request.
        ``similar_to()`` (v0.36.0) is refused the same ways: the vector index holds the records' current vectors, so
        the read would pick its neighbours by their vectors now.

        A time-travel queryset is read-only: ``bulk_create()``, ``bulk_update()``, ``bulk_delete()``,
        ``patch()``, ``update_or_create()``, ``get_or_create()``, ``delete_table()``, ``query()``, ``live()``,
        ``watch()`` and ``changes()`` raise ``SurrealDbError`` on it, before any request. ``_as_of`` is a
        reserved ``variables()`` name: the moment is bound under it, replacing a variable of that name.

        Returns:
            Self: the queryset, for chaining. Called again, the last moment wins.

        Raises:
            TypeError: ``at`` is none of the forms above (a ``date`` included).
            ValueError: a string that is not ISO-8601; a negative duration; a moment outside
                1970-01-01T00:00:00Z … 2262-04-11T23:47:16.854775Z.
            SurrealDbError: the queryset is bound to a transaction (``objects(tx=)``); a moment on a queryset
                with ``cache()`` (v0.28.0: a time-travel read is never cached), with ``search()`` (v0.35.0) or with
                ``similar_to()`` (v0.36.0). ``as_of(None)`` is accepted.
        """
        if self._tx is not None:
            raise SurrealDbError(_time_travel.transaction_refused())
        if at is not None and self._cache is not None:
            raise SurrealDbError(_CACHE_AS_OF)
        if at is not None and self._search:
            raise SurrealDbError(_search.time_travel_refused())
        if at is not None and self._similar is not None:
            raise SurrealDbError(_vector.time_travel_refused())
        self._as_of = _time_travel.normalise_as_of(at)
        return self

    def cache(self, ttl: float | timedelta | None = None) -> Self:
        """Serve this queryset's reads from the query cache (v0.28.0) — same name and argument as the full SurrealDB-ORM.

        ::

            books = await Product.objects().filter(category="book").cache(ttl=30).exec()   # the database answers
            again = await Product.objects().filter(category="book").cache(ttl=30).exec()   # the cache answers
            n = await Product.objects().filter(category="book").cache().count()            # default TTL

        Every read of the queryset is cached: ``exec()``, ``first()``, ``get()`` (with or without an id), ``all()``,
        ``count()``, ``sum()``, ``avg()``, ``min()``, ``max()``, ``exists()``, ``values()``+``annotate()``. A read is
        answered from the cache when an earlier read sent the same statement with the same variables on the same
        connection, with the same identity, less than ``ttl`` seconds ago, and no ORM write that can change its result
        has completed since: a write to a table it reads; any write, when it follows links (``fetch()``, a dotted path
        such as ``filter(**{"author.name": …})``, a ``select()`` item that is not a plain field, such as
        ``"author.*"``) or a materialized view model (``view_query``, v0.32.0), directly or through a ``Subquery``;
        any delete, when it reads an edge model (a deleted record's edges go with it).
        Each hit builds new instances from a copy of the cached rows: changing one never changes the cache.

        ``ttl`` is the oldest result this read accepts, in seconds (an ``int`` or a ``float`` >= 0) or a ``timedelta``;
        ``0`` means never cached (every read goes to the database); ``None`` uses ``QueryCache``'s default when the read
        runs (60 s unless configured). A result stored by this read lives for its ``ttl``. Called again, the last
        ``ttl`` wins. While the cache is disabled (``QueryCache.configure(enabled=False)``) a warning is logged and the
        request is kept: it applies if the cache is enabled again before the read.

        Not cached: reads inside a transaction (``objects(tx=…)``, which may see its uncommitted writes). ``live()``,
        ``watch()``, ``changes()`` and ``query()`` raise on a queryset with ``cache()``, as ``as_of()`` does. A write
        (``bulk_*()``, ``patch()``, ``delete_table()``, ``get_or_create()``…) never reads the cache: it runs as without
        ``cache()``, and invalidates. A ``Subquery`` is read in the outer statement: the outer queryset's ``cache()``
        applies, the inner one's is ignored (as its ``fetch()`` is).

        Invalidation: every write through the ORM (``save()``, ``update()``, ``merge()``, ``upsert()``, ``patch()``,
        ``delete()``, the atomic operations, relations, ``bulk_*()``, ``QuerySet.patch()``, ``delete_table()``,
        ``get_or_create()``/``update_or_create()`` when they write, ``signup()``, the DDL helpers) removes the cached
        reads it can change before its ``post_*`` signals fire; inside a transaction, at the commit. Not seen: writes by
        another process or client, raw SurrealQL (``raw_query()``, ``QuerySet.query()``, ``call_function()``,
        ``Transaction.add()``), a write cancelled or failed client-side after its request was sent, writes made by an
        access method's own ``SIGNIN``/``SIGNUP`` clauses, what the server derives on its own (``DEFINE EVENT``,
        ``REFERENCE … ON DELETE``, views over other tables read by a model without ``view_query``), values computed
        when the read runs (``Var()`` of a ``DEFINE PARAM``, ``<future>``/``COMPUTED`` fields, time functions in a
        ``select()`` expression): the TTL bounds them; call ``QueryCache.invalidate(Model)`` after such a write,
        ``QueryCache.clear()`` after changing the session behind the ORM's back.

        Returns:
            Self: the queryset, for chaining.

        Raises:
            TypeError: ``ttl`` is not a number of seconds or a ``timedelta`` (``bool`` refused).
            ValueError: ``ttl`` is negative or not finite.
            SurrealDbError: the queryset reads ``as_of()``.
        """
        request = CacheRequest(None if ttl is None else ttl_seconds(ttl, "cache() ttl"))
        if self._as_of is not None:
            raise SurrealDbError(_CACHE_AS_OF)
        if not QueryCache._enabled:
            logger.warning(_CACHE_DISABLED)
        self._cache = request
        return self

    def _reject_time_travel(self, method: str) -> None:
        """Refuse *method* (a write or a stream, named with its ``()``) on a queryset read ``as_of()`` (v0.26.0).

        A time-travel read is read-only and cannot be streamed, so nothing is sent and no client is acquired: each
        such write runs it right after its materialized-view check (v0.32.0), and each stream (and ``query()``) at
        its start. ``bulk_create()``, ``bulk_update()``, ``bulk_delete()``, ``patch()`` and ``delete_table()`` run
        both in their ``invalidates(refuse=…)`` (:func:`_queryset_refusals`, #320), before their tables are noted on
        the transaction or put in flight: a refused write leaves the transaction as it was and drops no cached read.
        ``update_or_create()`` and ``get_or_create()`` run both at their start, before any lookup or write.
        """
        if self._as_of is not None:
            raise SurrealDbError(_time_travel.queryset_refused(method))

    async def _time_travel_moment(self, *, fetches: bool = False) -> datetime | None:
        """The resolved moment of a read under ``as_of()``; ``None`` without it, and no request (v0.26.0).

        The first step of every read. In order:

        1. On the embedded engine, a URL that keeps no history (``embedded.keeps_history()``) raises
           before any client is acquired, so nothing is sent.
        2. A read that returns the records ``fetch()`` resolves (*fetches*), with ``fetch()`` set, asks the
           server line (one ``version()`` request, as ``changes()`` does) and raises below 3: SurrealDB 2.x
           resolves fetched links to their current state. No other read asks the line.
        3. A duration or a ``SurrealFunc`` is resolved by the database (one ``RETURN`` request), and the
           resolved moment checked against the moments a version can address (``time_travel.resolve()``,
           which raises, after which the read is never sent). An absolute moment, checked by ``as_of()``, needs
           no request.

        Args:
            fetches: whether the read returns the records ``fetch()`` resolves (``exec()`` outside
                ``values()``+``annotate()``, hence ``first()`` and ``get()`` without an id); the other reads
                ignore ``fetch()``.

        Raises:
            SurrealDbError: a store that keeps no history; ``fetch()`` on SurrealDB 2.x; a duration or an
                expression the database cannot resolve to a moment a version can address.
        """
        if self._as_of is None:
            return None
        manager = self._manager()
        url = manager.get_connection_string()
        if manager.is_embedded() and not keeps_history(url):
            raise SurrealDbError(history_unsupported(url))
        client = await manager.get_client()
        if fetches and self._fetch_fields and _time_travel.line_major(await client.version()) < 3:
            raise SurrealDbError(_time_travel.fetch_refused())
        return await _time_travel.resolve(self._as_of, client, self._variables)

    @staticmethod
    def _mark_snapshots(result: Any, moment: datetime | None) -> Any:
        """Mark the instances of *result* (a list, or one instance) as snapshots read at *moment*; return it (v0.26.0).

        The record models nested in their fields are marked too, at any depth: in a sub-model, a list, a tuple,
        a set or a dict's values. A link ``fetch()`` resolves into a model-typed field (``author: Author |
        RecordID | None``, SurrealDB 3.x) holds the linked record as it was, and its writes would put that past
        state back over the current record. Each object is visited once, so a cycle ends the walk.

        Nothing happens without a moment (a current read). Rows that did not validate stay dicts, untouched; a
        plain Pydantic sub-model is walked but carries no marker. The marker is read with
        ``model_base._snapshot_moment()``.
        """
        if moment is None:
            return result
        rows = result if isinstance(result, list) else [result]
        pending: list[Any] = [row for row in rows if isinstance(row, BaseSurrealModel)]
        seen: set[int] = set()
        while pending:
            value = pending.pop()
            if id(value) in seen:
                continue
            seen.add(id(value))
            children: Iterable[Any]
            if isinstance(value, BaseModel):
                if isinstance(value, BaseSurrealModel):
                    value._surreal_as_of = moment
                children = [*value.__dict__.values(), *(value.__pydantic_extra__ or {}).values()]
            elif isinstance(value, dict):
                children = value.values()
            else:
                children = value
            pending.extend(child for child in children if isinstance(child, _SNAPSHOT_HOLDERS))
        return result

    # ==================== Internal query building ====================

    def _subquery_compiler(
        self,
        empty_tables: frozenset[str] = frozenset(),
        *,
        inline: bool = False,
        warn: bool = True,
        knn_prefilter: bool = False,
    ) -> SubqueryCompiler:
        """A compiler for the subqueries of one statement of this queryset (v0.27.0).

        It starts with this queryset's transaction, a copy of its ``variables()`` (the subqueries' are merged into
        it) and this queryset on the stack of the querysets being rendered, so a queryset given as its own subquery
        raises before any other check of the inner queryset.

        It also carries this queryset's model (v0.29.0): every subquery of the statement must run on its connection.

        Args:
            empty_tables: the tables whose subqueries are bound to ``[]`` (SurrealDB 3.x confirmed they do not exist).
            inline: write the sub-SELECTs inline instead of in ``LET`` s (a live query).
            warn: emit the warnings of the filter values (v0.28.1); ``False`` for a statement this read compiled once.
            knn_prefilter: compile the form below SurrealDB 3 (v0.36.0): the conditions next to a KNN selected first,
                in a ``LET`` (:meth:`_where_parts`).
        """
        return SubqueryCompiler(
            inline=inline,
            empty_tables=empty_tables,
            outer_tx=self._tx,
            outer_model=self.model,
            variables=dict(self._variables),
            warn=warn,
            knn_prefilter=knn_prefilter,
            _stack=[id(self)],
        )

    def _where_parts(
        self, counter: int = 0, *, as_of: bool = False, subqueries: SubqueryCompiler | None = None
    ) -> tuple[list[str], dict[str, Any], int]:
        """The AND-ed conditions of the WHERE clause, their bound values and the next ``$_f<N>`` number.

        Filters, ``Q`` objects, ``nearby()`` and ``search()`` conditions (v0.35.0), then the KNN condition of
        ``similar_to()`` (v0.36.0), in that order, numbered from *counter* on: the outer statement starts at 0, a
        subquery (v0.27.0) continues the numbering of the statement it goes into. A ``search()`` condition is
        ``<column> @<ref>@ $_f<N>``, its text bound; the match reference is the condition's position in this queryset,
        so a subquery's references start again at 0 while its texts continue the statement's numbering. The KNN
        condition is ``<column> <|K,EF|> $_f<N>`` or ``<column> <|K,DIST|> $_f<N>``, the query vector bound as a
        ``list[float]``; it comes last, AND-ed at the top level of the ``WHERE``, where SurrealDB requires it.

        With a statement's compiler, the parts next to the KNN that are not one of the ORM's id subqueries
        (``vector.id_subquery_part()``: ``id IN $_sq<N>``, ``$_sq<N> CONTAINS id`` on a view) — a comparison, a ``Q``,
        a ``nearby()`` or ``search()`` condition — set ``subqueries.knn_conditions``: the caller reads the line. In the
        form for SurrealDB 3.x they stay inline. In the form below 3 (``subqueries.knn_prefilter``: SurrealDB 2.x, the
        embedded engine) they go into ``LET $_sq<N> = (SELECT VALUE id FROM <table> WHERE <parts AND-ed>);`` and the
        ``WHERE`` keeps the id subqueries, then ``id IN $_sq<N>`` (``$_sq<N> CONTAINS id`` on a view model, as
        ``id__in`` writes it), then the KNN: SurrealDB 2.x answers wrong records for a KNN next to a condition on an
        indexed column, a function call, a negation, a group, a dotted path or a full-text match, while it answers
        right through ids selected first — and the ORM cannot know a column's indexes. The ``LET`` reads the
        statement's own table and follows the ``LET`` s of the parts' own subqueries; inside a ``Subquery``, it
        precedes the subquery's own. An exact scan (``dist=``) sets ``subqueries.knn_conditions`` too, with or without
        other parts: below 3 its ``LET`` holds the ids of the ``K`` nearest records matching every part, the id
        subqueries included, ranked by distance then id (``vector.exact_scan_select()``), and the ``WHERE`` keeps
        ``id IN $_sq<N>`` and the KNN — SurrealDB 2.x's and the embedded engine's own exact scan picks among records at
        an equal distance at the ``K``-th place in no fixed order. Without a compiler the KNN is written inline. With
        ``as_of=True`` (a time-travel table read, v0.26.0) the per-record check ``time_travel.GUARD`` is the last
        condition, after the ``nearby()`` ones (``search()`` never reads at a moment): every other part is a single
        comparison or a parenthesised group, so the check applies to every row they keep.

        Args:
            counter: the first ``$_f<N>`` number.
            as_of: append the time-travel guard.
            subqueries: the statement's compiler, which binds a subquery value; ``None`` refuses one.
        """
        parts: list[str] = []
        variables: dict[str, Any] = {}
        endpoint_columns = self.model._endpoint_columns()
        # A view model's id__in compiles "$_f0 CONTAINS id" (v0.32.0): SurrealDB 3.x answers no record to "id IN".
        view = self.model._is_view()

        # Regular keyword filters (AND-joined)
        for field_name, lookup_name, value in self._filters:
            sql, vars_, counter = build_filter_condition(
                field_name,
                lookup_name,
                value,
                counter,
                self._model_table,
                endpoint_columns=endpoint_columns,
                subqueries=subqueries,
                view=view,
            )
            parts.append(sql)
            variables.update(vars_)

        # Q object filters
        for q in self._q_filters:
            sql, vars_, counter = q.to_sql(
                counter, self._model_table, self._column, endpoint_columns, subqueries=subqueries, view=view
            )
            if sql:
                parts.append(sql)
                variables.update(vars_)

        # nearby() conditions: the point and the distance are bound. The is_point() guard keeps
        # geo::distance away from values that are not points: it refuses a string or an object,
        # and returns NONE for another geometry, which a "<=" comparison would keep.
        for column, point, distance in self._nearby:
            point_var, distance_var = f"_f{counter}", f"_f{counter + 1}"
            parts.append(f"({column}.is_point() AND geo::distance({column}, ${point_var}) <= ${distance_var})")
            variables[point_var] = point
            variables[distance_var] = distance
            counter += 2

        # search() conditions (v0.35.0): the text is bound, never written into the statement.
        for reference, (_, column, text) in enumerate(self._search):
            variable = f"_f{counter}"
            parts.append(f"{column} @{reference}@ ${variable}")
            variables[variable] = text
            counter += 1

        # similar_to() (v0.36.0): the query vector is bound; K and EF are checked integers the ORM writes itself.
        if self._similar is not None:
            variable = f"_f{counter}"
            knn = self._similar.condition(variable)
            variables[variable] = list(self._similar.vector)
            counter += 1
            others = [part for part in parts if not _vector.id_subquery_part(part)]
            exact = self._similar.dist is not None
            if subqueries is None or not (others or exact):
                parts.append(knn)
            elif not subqueries.knn_prefilter:
                # The form for SurrealDB 3.x, which applies every condition during the index search and gives its exact
                # scan one answer.
                subqueries.knn_conditions = True
                parts.append(knn)
            else:
                subqueries.knn_conditions = True
                name = f"{_subquery.LET_PREFIX}{len(subqueries.lets)}"
                if exact:
                    # Below SurrealDB 3, an exact scan: its K nearest ranked by distance then id, every condition first.
                    select = _vector.exact_scan_select(self._model_table, self._similar, variable, parts)
                    kept = []
                else:
                    # Below SurrealDB 3: the records matching the other conditions selected first, by their ids.
                    select = f"(SELECT VALUE id FROM {self._model_table} WHERE {' AND '.join(others)})"
                    kept = [part for part in parts if _vector.id_subquery_part(part)]
                subqueries.lets.append(f"LET ${name} = {select};")
                parts = [*kept, f"${name} CONTAINS id" if view else f"id IN ${name}", knn]

        if as_of:
            parts.append(_time_travel.GUARD)

        return parts, variables, counter

    def _build_where(self, *, as_of: bool = False, subqueries: SubqueryCompiler | None = None) -> tuple[str, dict[str, Any]]:
        """
        Build a parameterized WHERE clause from filters, Q objects and ``nearby()`` conditions.

        With ``as_of=True`` (a time-travel table read, v0.26.0) the per-record check
        ``time_travel.GUARD`` is the last condition (:meth:`_where_parts`). A subquery value (v0.27.0) is bound
        by *subqueries*, the statement's compiler; without one it raises ``ValueError``.

        Once every condition is compiled, a scalar ``str`` filter value that is ``$`` and the name of one of the
        statement's ``variables()`` — this queryset's or its subqueries' — or of SurrealDB's session parameters
        (``$auth``…) gives a ``UserWarning`` naming ``Var(...)`` (:func:`utils.warn_on_variable_names`, v0.28.1),
        unless ``subqueries.warn`` is false. Without *subqueries* nothing is checked: only a statement has variables.

        Returns:
            A tuple of (where_clause_string, variables_dict).
            The where_clause_string includes the ``WHERE`` keyword, or is empty if no filters.
        """
        parts, variables, _ = self._where_parts(0, as_of=as_of, subqueries=subqueries)
        if subqueries is not None:
            if subqueries.warn:
                warn_on_variable_names(subqueries.variable_like, subqueries.variables)
            subqueries.variable_like.clear()
        if not parts:
            return "", {}

        return " WHERE " + " AND ".join(parts), variables

    def _write_where(self, compiler: SubqueryCompiler) -> tuple[str, dict[str, Any]]:
        """The ``WHERE`` clause of a bulk write by filter (:meth:`bulk_update`, :meth:`bulk_delete`, :meth:`patch`) and
        its values, compiled with the statement's *compiler*.

        Without ``similar_to()`` it is :meth:`_build_where`'s, unchanged. With it (v0.36.0) the write goes by the ids
        of the nearest records: ``LET $_sq<N> = (SELECT VALUE id FROM <table> WHERE <conditions> AND <knn>);`` joins
        the statement's block — after the ``LET`` s of its conditions (its subqueries', and below SurrealDB 3 the
        pre-filter's, :meth:`_where_parts`), so ``N`` is taken once they are compiled — and the write says ``WHERE id
        IN $_sq<N>``: it writes as many records as the same read returns, on every line. A write never carries the KNN
        operator: there an exact search (``dist=``) writes every matching record on SurrealDB 2.x and the embedded
        engine, and a range on an indexed column next to the KNN writes nothing on every engine.

        The ``LET`` reads the model's own table, registered in ``compiler.tables`` as a subquery's: when SurrealDB 3.x
        confirms that table missing (``compiler.empty_tables``), the ids are bound to ``[]`` and nothing else is
        compiled, so the write reads it as empty, as it does without ``similar_to()``. A view model never gets here
        (its writes are refused first), so the write always says ``id IN``. ``limit()``, ``offset()`` and
        ``order_by()`` are not written, as for every bulk write.
        """
        if self._similar is None:
            return self._build_where(subqueries=compiler)
        table = self._model_table
        compiler.tables.add(table)
        if table in compiler.empty_tables:
            values: dict[str, Any] = {}
            select = "[]"
        else:
            where, values = self._build_where(subqueries=compiler)
            select = f"(SELECT VALUE id FROM {table}{where})"
        name = f"{_subquery.LET_PREFIX}{len(compiler.lets)}"
        compiler.lets.append(f"LET ${name} = {select};")
        return f" WHERE id IN ${name}", values

    def _subquery_select(self, compiler: SubqueryCompiler, counter: int) -> tuple[str, dict[str, Any], int]:
        """This queryset as the sub-SELECT of another queryset's filter (v0.27.0); its values and the next counter.

        ``(SELECT VALUE <column> FROM <table> [WHERE …] [LIMIT n] [START m] [VERSION $_as_of])``, the column being
        the one ``select()`` names, else ``id``. Ordered, ``(SELECT <column> AS _sqv, <order columns> FROM …
        ORDER BY … …)._sqv``: SurrealDB 2.x refuses to order a ``SELECT VALUE`` by another field and orders wrongly
        by a column it selects only under an alias, so every order column is selected by its name. Its filter
        values are bound from *counter* on, its ``variables()`` merged into the statement's, its own subqueries
        rendered first (their ``LET`` s precede its own). Under the outer ``as_of()`` (``compiler.moment``) it
        carries the per-record guard and ``VERSION $_as_of``: the server never applies the outer ``VERSION`` to a
        ``LET``. A table in ``compiler.empty_tables`` compiles to ``[]``. Each of its ``search()`` conditions is
        recorded in ``compiler.searched`` (v0.35.0), its ``similar_to()`` in ``compiler.knn_fields`` (v0.36.0).
        ``fetch()`` is ignored, as in the full SurrealDB-ORM: a sub-SELECT gives values, never records to resolve.

        Called by :meth:`SubqueryCompiler.reference`, which first refuses a queryset that is its own subquery.

        Raises:
            ValueError: ``annotate()``, ``select()`` of several fields or ``values()`` (every offender named); an
                invalid selected column; ``_sqv`` ordered on where the ordered form selects its alias; its
                ``variables()`` binding a name of the statement to another value; and its filters' own refusals.
                Its ``limit()`` and ``offset()`` were checked when they were called (a non-negative integer).
            SurrealDbError: an ``as_of()`` of its own; a transaction that is not the outer queryset's; ``search()``
                under the outer ``as_of()`` (v0.35.0); ``similar_to()`` under the outer ``as_of()`` or in a live query
                (v0.36.0).
        """
        offenders = {
            "annotate()": bool(self._annotations),
            "select() of several fields": len(self.select_item) > 1,
            "values()": bool(self._group_by_fields),
        }
        refused = sorted(name for name, present in offenders.items() if present)
        if refused:
            raise ValueError(_subquery.clauses_refused(refused))
        if self._as_of is not None:
            raise SurrealDbError(_subquery.moment_refused())
        if self._tx is not None and self._tx is not compiler.outer_tx:
            raise SurrealDbError(_subquery.transaction_refused())
        if self._search and compiler.moment is not None:
            raise SurrealDbError(_search.time_travel_refused())
        if self._similar is not None and compiler.moment is not None:
            raise SurrealDbError(_vector.time_travel_refused())
        if self._similar is not None and compiler.inline:
            raise SurrealDbError(_vector.live_subquery_refused())
        column = self.select_item[0] if self.select_item else "id"
        validate_field_name(column, "subquery select field")
        order_columns: list[str] = []
        if self._order_by:
            order_columns = list(dict.fromkeys(part.rsplit(" ", 1)[0] for part in self._order_by.split(", ")))
        alias = _subquery.VALUE_ALIAS
        for order_column in order_columns:
            if order_column.split(".")[0] == alias and not order_column == column == alias:
                raise ValueError(_subquery.reserved_alias())
        table = self._model_table
        compiler.tables.add(table)
        if self.model.__surreal_edge__:
            compiler.edge_tables.add(table)
        if self.model._is_view():
            compiler.view_tables.add(table)
        compiler.merge_variables(self._variables)
        if table in compiler.empty_tables:
            return "[]", {}, counter
        for field, search_column, _ in self._search:
            compiler.searched[(table, search_column)] = field
        if self._similar is not None:
            compiler.knn_fields[(table, self._similar.column)] = self._similar.field
        parts, values, counter = self._where_parts(counter, as_of=compiler.moment is not None, subqueries=compiler)
        where = " WHERE " + " AND ".join(parts) if parts else ""
        tail = ""
        if self._limit is not None:
            tail += f" LIMIT {self._limit}"
        if self._offset is not None:
            tail += f" START {self._offset}"
        if compiler.moment is not None:
            tail += _time_travel.VERSION_CLAUSE
        if not order_columns:
            return f"(SELECT VALUE {column} FROM {table}{where}{tail})", values, counter
        selected = ", ".join([f"{column} AS {alias}", *order_columns])
        return f"(SELECT {selected} FROM {table}{where} ORDER BY {self._order_by}{tail}).{alias}", values, counter

    def _version_moment(self, moment: datetime | None) -> datetime | None:
        """The moment a compile function writes ``VERSION $_as_of`` for; ``None`` without ``as_of()`` (v0.26.0).

        Keyed on ``as_of()``, never on the argument: without it, a compile function returns v0.25.0's text
        byte for byte, whatever *moment* says. With it, the resolved moment is required — a read compiled
        without one must never fall back to the current data.
        """
        if self._as_of is None:
            return None
        if moment is None:
            raise RuntimeError("internal: a time-travel read was compiled without its moment")
        return moment

    @staticmethod
    def _terminate(query: str, variables: dict[str, Any], moment: datetime | None) -> tuple[str, dict[str, Any]]:
        """End *query* with ``;`` — after ``VERSION $_as_of``, its moment bound last, for a time-travel read."""
        if moment is None:
            return query + ";", variables
        return f"{query}{_time_travel.VERSION_CLAUSE};", {**variables, _time_travel.MOMENT_VARIABLE: moment}

    def _compile_query(
        self,
        moment: datetime | None = None,
        *,
        subqueries: SubqueryCompiler | None = None,
        for_exists: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        """
        Compile the QuerySet parameters into a parameterized SQL query.

        Args:
            moment: Under ``as_of()``, the resolved moment of the read (v0.26.0): the guarded WHERE and
                ``VERSION $_as_of`` are compiled, ``$_as_of`` bound to it. Ignored without ``as_of()``.
            subqueries: The compiler of the statement's subqueries (v0.27.0); a new one by default. With
                subqueries the statement is a block, ``{ LET $_sq0 = (…); (SELECT …) };``; without, the text
                and variables of v0.26.0.
            for_exists: private, ``exists()`` only (v0.36.0): below SurrealDB 3 the search annotations are left out of
                a ``similar_to()`` statement instead of refused — a boolean answer needs none. Elsewhere it changes
                nothing.

        With ``similar_to()`` (v0.36.0) the distance is projected as ``vector::distance::knn() AS _knn_distance``,
        after the ``GeoDistance`` and search annotations, and without ``order_by()`` the statement ends
        ``ORDER BY _knn_distance`` (nearest first) — ``ORDER BY _knn_distance, id`` for an exact scan (``dist=``) in the
        form below SurrealDB 3 when the projection reads ``id`` (``*``, or ``select()`` naming it), so its records at
        one distance come in one order on every call. Its search annotations are projected in the form for SurrealDB
        3.x only: the form below 3 (``subqueries.knn_prefilter``) selects the full-text matches in a ``LET``, so its
        outer statement holds no match to score, highlight or locate, and SurrealDB 2.x would answer ``NONE`` for each.

        Returns:
            A tuple of (query_string, variables_dict).

        Raises:
            RuntimeError: under ``as_of()`` without a moment (an internal error).
            ValueError, SurrealDbError: a subquery the ORM cannot compile (README section 28).
            ValueError: a search annotation whose match reference ``search()`` did not give this queryset
                (v0.35.0, :meth:`_check_search_references`).
            SurrealDbError: this queryset's own ``similar_to()`` with a search annotation, compiled in the form below
                SurrealDB 3 (v0.36.0), never with ``for_exists``; raised before any ``LET`` is written.
        """
        self._check_search_references()
        drop_search = False
        if subqueries is not None and subqueries.knn_prefilter and self._similar is not None and self._search_annotations:
            if not for_exists:
                first = next(iter(self._search_annotations.values()))
                raise SurrealDbError(_vector.search_annotation_refused(type(first).__name__))
            drop_search = True
        moment = self._version_moment(moment)
        compiler = subqueries if subqueries is not None else self._subquery_compiler()
        compiler.moment = moment
        where_clause, where_vars = self._build_where(as_of=moment is not None, subqueries=compiler)

        projection = list(self.select_item) or ["*"]
        # GeoDistance annotations (v0.24.0): one projection each, the point bound. The is_point() guard
        # gives NONE for a value that is not a point: geo::distance refuses a string or an object.
        distance_vars: dict[str, Any] = {}
        for index, (alias, (column, point)) in enumerate(self._geo_annotations.items()):
            variable = f"_gd{index}"
            projection.append(f"IF {column}.is_point() THEN geo::distance({column}, ${variable}) END AS {alias}")
            distance_vars[variable] = point
        search_projection, search_vars = ([], {}) if drop_search else self._search_projection()
        projection += search_projection
        # similar_to() (v0.36.0): the distance of the KNN, after every other per-record value.
        if self._similar is not None:
            projection.append(f"vector::distance::knn() AS {_vector.DISTANCE_ALIAS}")
        query = f"SELECT {', '.join(projection)} FROM {self._model_table}"

        query += where_clause

        if self._order_by:
            query += f" ORDER BY {self._order_by}"
        elif self._similar is not None:
            query += f" ORDER BY {_vector.DISTANCE_ALIAS}"
            # Below SurrealDB 3 the exact scan's records at one distance come by id; SurrealDB 2.x refuses to order by a
            # column the projection does not read.
            if compiler.knn_prefilter and self._similar.dist is not None and self._reads_id():
                query += ", id"

        if self._limit is not None:
            query += f" LIMIT {self._limit}"

        if self._offset is not None:
            query += f" START {self._offset}"

        if self._fetch_fields:
            query += f" FETCH {', '.join(self._fetch_fields)}"

        query, variables = self._terminate(query, {**compiler.variables, **where_vars, **distance_vars, **search_vars}, moment)
        return compiler.wrap(query), variables

    def _reads_id(self) -> bool:
        """Whether the projection of :meth:`_compile_query` reads ``id``: ``*`` (no ``select()``, or ``select("*")``) or a
        ``select()`` naming it."""
        return not self.select_item or "*" in self.select_item or RECORD_ID_COLUMN in self.select_item

    def _search_projection(self) -> tuple[list[str], dict[str, Any]]:
        """The projections of the search annotations, in ``annotate()`` order, and the highlight tags they bind
        (v0.35.0).

        ``search::score(<ref>) AS <alias>``, ``search::highlight($_sh<2k>, $_sh<2k+1>, <ref>) AS <alias>`` (*k* the
        highlight's position among the highlights, its tags bound, never written into the statement) and
        ``search::offsets(<ref>) AS <alias>``; the reference is an ``int`` checked 0..255 when the annotation was
        built, the alias checked by ``annotate()``.
        """
        projection: list[str] = []
        tags: dict[str, str] = {}
        for alias, annotation in self._search_annotations.items():
            if isinstance(annotation, SearchHighlight):
                opening, closing = f"_sh{len(tags)}", f"_sh{len(tags) + 1}"
                projection.append(f"search::highlight(${opening}, ${closing}, {annotation.ref}) AS {alias}")
                tags = {**tags, opening: annotation.open_tag, closing: annotation.close_tag}
            elif isinstance(annotation, SearchScore):
                projection.append(f"search::score({annotation.ref}) AS {alias}")
            else:
                projection.append(f"search::offsets({annotation.ref}) AS {alias}")
        return projection, tags

    def _check_search_references(self) -> None:
        """Refuse a search annotation whose match reference ``search()`` did not give this queryset (v0.35.0).

        Checked when the queryset runs — ``annotate()`` may come before ``search()`` —, before any request: first by
        ``exec()`` (hence ``first()`` and ``get()`` without an id) and ``exists()``, ahead of an ``as_of()``
        moment's resolution, then by :meth:`_compile_query`. ``count()`` and the aggregations ignore every
        annotation, as they ignore ``GeoDistance``.

        Raises:
            ValueError: a search annotation on a queryset without ``search()``, or naming a reference beyond the
                fields ``search()`` gave it (``0`` to ``n - 1``, in call order).
        """
        given = len(self._search)
        for annotation in self._search_annotations.values():
            if not given:
                raise ValueError(_search.no_search_refused(annotation))
            if annotation.ref >= given:
                raise ValueError(_search.reference_refused(annotation, given))

    def _hybrid_runner(self, knn: _vector.KnnSearch, text_field: str, text_query: str) -> Self:
        """The private copy of this queryset that runs ``hybrid_search()`` (v0.37.0), this queryset never changed.

        A shallow copy holding the vector search *knn* as its ``similar_to()`` and the match of *text_query* in
        *text_field* as its only ``search()`` condition, so the existing read path serves it: its ``_check_links()``,
        the fields its missing-index and dimension errors name. It has its own set of link heads — the first segment of
        each dotted path it names, the two fields' here and the ``Q`` objects' as they compile —, so this queryset's
        set stays as it was; every other attribute is shared and only read. Its refusals name ``hybrid_search()``
        (``_refusal_subject``).

        Args:
            knn: the vector search, its arguments checked.
            text_field: the full-text field as the caller gave it, checked.
            text_query: the text to match.
        """
        runner = copy.copy(self)
        runner._link_heads = set(self._link_heads)
        runner._refusal_subject = _hybrid.SUBJECT
        runner._column(knn.field)
        runner._similar = knn
        runner._search = [(text_field, runner._column(text_field), text_query)]
        return runner

    def _compile_hybrid(
        self, compiler: SubqueryCompiler, *, below_3: bool, text_limit: int, rrf_k: int
    ) -> tuple[str, dict[str, Any]]:
        """The statement of ``hybrid_search()`` and its variables (v0.37.0), compiled on its runner
        (:meth:`_hybrid_runner`) with the statement's *compiler*.

        Two lists of the queryset's records, each with the queryset's conditions (:meth:`_where_parts`, numbered on from
        one list to the next; a ``Subquery`` filter bound once per list): the vector search's, ``(SELECT <projection>,
        vector::distance::knn() AS _knn_distance FROM <table> WHERE <conditions AND> <knn> ORDER BY _knn_distance, id
        LIMIT <vector_limit>)``, and the full-text search's, ``(SELECT <projection>, search::score(0) AS _search_score
        FROM <table> WHERE <conditions AND> <column> @0@ $_f<n> ORDER BY _search_score DESC, id LIMIT <text_limit>)``,
        each ending with ``FETCH`` under ``fetch()``. Each list is ordered by its value then by ``id``, so equal values
        rank alike every time, and bounded by its ``LIMIT`` on every line. The projection is ``*``, or ``select()``'s
        items without those naming a reserved column (``hybrid.reserved()``: its key holds the method's own value), with
        ``id`` first when absent — the lists are fused by it.

        On SurrealDB 3.x one ``SELECT`` fuses them: both lists written inline in ``search::rrf([…], <vector_limit +
        text_limit>, <rrf_k>)``, so it never cuts, each list followed by a ``.map()`` putting its index on its rows and
        leaving out the other list's value (``hybrid.KNN_OMITTED``, ``hybrid.TEXT_OMITTED`` in an ``OMIT``), so a
        column a record stores under one of those names never replaces it; ``ORDER BY rrf_score DESC, _hybrid_pos``
        ranks equal scores by position — the vector list's records first —, then ``limit()``/``offset()`` page. No list
        is a ``LET``: inline, every 3.x line reports a vector of the wrong length and a missing table as errors the ORM
        reads. Below 3 (SurrealDB 2.x, the embedded engine: no ``search::rrf()``) each list is a ``LET`` of the block,
        named once its own conditions are compiled — the vector list's conditions selected first under
        ``compiler.knn_prefilter``, and an exact scan's nearest ranked by distance then id, as for ``similar_to()`` —,
        without ``OMIT``, and the block's value is both lists:
        ``hybrid.fuse()`` fuses them, which drops the same keys, and the ORM pages.

        The filter values' warnings are given once for the statement, as :meth:`_build_where` gives them. Every number
        written is a checked ``int``; the vector and the text are bound.
        """
        knn = cast(_vector.KnnSearch, self._similar)
        knn_view = copy.copy(self)
        knn_view._search = []
        text_view = copy.copy(self)
        text_view._similar = None
        if self.select_item:
            items = [item for item in self.select_item if not _hybrid.reserved(item)]
            projection = ", ".join(items if "id" in items or "*" in items else ["id", *items])
        else:
            projection = "*"
        fetch = f" FETCH {', '.join(self._fetch_fields)}" if self._fetch_fields else ""
        searches = (
            (
                knn_view,
                f"vector::distance::knn() AS {_vector.DISTANCE_ALIAS}",
                _hybrid.KNN_OMITTED,
                _vector.DISTANCE_ALIAS,
                knn.k,
            ),
            (
                text_view,
                f"search::score(0) AS {_hybrid.SEARCH_SCORE}",
                _hybrid.TEXT_OMITTED,
                f"{_hybrid.SEARCH_SCORE} DESC",
                text_limit,
            ),
        )
        lists: list[str] = []
        variables: dict[str, Any] = {}
        counter = 0
        for view, value, omitted, order, limit in searches:
            parts, values, counter = view._where_parts(counter, subqueries=compiler)
            variables.update(values)
            omit = "" if below_3 else f" OMIT {', '.join(omitted)}"
            select = (
                f"(SELECT {projection}, {value}{omit} FROM {self._model_table} WHERE {' AND '.join(parts)} "
                f"ORDER BY {order}, id LIMIT {limit}{fetch})"
            )
            if below_3:
                name = f"${_subquery.LET_PREFIX}{len(compiler.lets)}"
                compiler.lets.append(f"LET {name} = {select};")
                select = name
            lists.append(select)
        if compiler.warn:
            warn_on_variable_names(compiler.variable_like, compiler.variables)
        compiler.variable_like.clear()
        if below_3:
            statement = f"[{', '.join(lists)}];"
        else:
            vector_position, text_position = _hybrid.VECTOR_POSITION, _hybrid.TEXT_POSITION
            statement = (
                f"SELECT *, IF {vector_position} != NONE THEN {vector_position} ELSE {knn.k} + {text_position} END AS "
                f"{_hybrid.POSITION} FROM search::rrf([{lists[0]}.map(|$r, $i| object::extend($r, {{{vector_position}: "
                f"$i}})), {lists[1]}.map(|$r, $i| object::extend($r, {{{text_position}: $i}}))], {knn.k + text_limit}, "
                f"{rrf_k}) ORDER BY {_hybrid.SERVER_SCORE} DESC, {_hybrid.POSITION}"
            )
            if self._limit is not None:
                statement += f" LIMIT {self._limit}"
            if self._offset is not None:
                statement += f" START {self._offset}"
            statement += ";"
        return compiler.wrap(statement), {**compiler.variables, **variables}

    def _compile_aggregation_query(
        self,
        aggregation_expr: str,
        alias: str | None = None,
        moment: datetime | None = None,
        *,
        subqueries: SubqueryCompiler | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """
        Compile an aggregation query with parameterized WHERE clause.

        Args:
            aggregation_expr: The aggregation expression (e.g., ``"count()"``, ``"math::sum(field)"``).
            alias: Optional alias for the aggregation result.
            moment: Under ``as_of()``, the resolved moment of the read (v0.26.0), as for
                :meth:`_compile_query`; ``VERSION $_as_of`` follows ``GROUP ALL``.
            subqueries: The compiler of the statement's subqueries (v0.27.0), as for :meth:`_compile_query`.

        Returns:
            A tuple of (query_string, variables_dict).

        Raises:
            RuntimeError: under ``as_of()`` without a moment (an internal error).
            ValueError, SurrealDbError: a subquery the ORM cannot compile, as for :meth:`_compile_query`.
        """
        moment = self._version_moment(moment)
        compiler = subqueries if subqueries is not None else self._subquery_compiler()
        compiler.moment = moment
        where_clause, where_vars = self._build_where(as_of=moment is not None, subqueries=compiler)

        if alias:
            query = f"SELECT {aggregation_expr} AS {alias} FROM {self._model_table}"
        else:
            query = f"SELECT {aggregation_expr} FROM {self._model_table}"

        query += where_clause
        query += " GROUP ALL"

        query, variables = self._terminate(query, {**compiler.variables, **where_vars}, moment)
        return compiler.wrap(query), variables

    def _compile_group_by_query(
        self, moment: datetime | None = None, *, subqueries: SubqueryCompiler | None = None
    ) -> tuple[str, dict[str, Any]]:
        """
        Compile a GROUP BY query with annotations and parameterized WHERE clause.

        Args:
            moment: Under ``as_of()``, the resolved moment of the read (v0.26.0), as for
                :meth:`_compile_query`; ``VERSION $_as_of`` follows ``START``.
            subqueries: The compiler of the statement's subqueries (v0.27.0), as for :meth:`_compile_query`.

        Returns:
            A tuple of (query_string, variables_dict).

        Raises:
            ValueError: a per-record annotation is present — a ``GeoDistance``, or a search annotation (v0.35.0) —,
                named by the first of them.
            RuntimeError: under ``as_of()`` without a moment (an internal error).
            ValueError, SurrealDbError: a subquery the ORM cannot compile, as for :meth:`_compile_query`.
        """
        if self._geo_annotations or self._search_annotations:
            per_record = [value for value in self._annotations.values() if isinstance(value, _PER_RECORD)]
            raise ValueError(_per_record_refused(per_record[0]))
        self._reject_annotation_collisions()
        moment = self._version_moment(moment)
        compiler = subqueries if subqueries is not None else self._subquery_compiler()
        compiler.moment = moment
        where_clause, where_vars = self._build_where(as_of=moment is not None, subqueries=compiler)

        select_parts = list(self._group_by_fields)
        for alias, agg in self._annotations.items():
            select_parts.append(f"{cast('Aggregation', agg).to_sql(self._column)} AS {alias}")

        query = f"SELECT {', '.join(select_parts)} FROM {self._model_table}"
        query += where_clause

        if self._group_by_fields:
            query += f" GROUP BY {', '.join(self._group_by_fields)}"
        else:
            query += " GROUP ALL"

        if self._order_by:
            query += f" ORDER BY {self._order_by}"

        if self._limit is not None:
            query += f" LIMIT {self._limit}"

        if self._offset is not None:
            query += f" START {self._offset}"

        query, variables = self._terminate(query, {**compiler.variables, **where_vars}, moment)
        return compiler.wrap(query), variables

    # ==================== Query execution ====================

    async def _execute_query(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        subquery_tables: frozenset[str] = frozenset(),
        searched: Mapping[str, str] | None = None,
        knn_fields: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Execute a read query. Routes through the transaction when one is attached
        (``objects(tx=)``), so reads participate in the transaction and see its
        uncommitted writes (interactive, 3.x). On a buffered tx, ``run_read`` raises.

        A table that does not exist reads as no rows (``NotFoundError``, SurrealDB 3.x).

        On a queryset read ``as_of()`` (v0.26.0) — every read of which is a ``VERSION`` read, its writes
        and ``query()`` being refused — a server whose storage keeps no history ("does not support
        versioned queries": SurrealDB 3.x, 2.x on RocksDB) is reported as ``SurrealDbError``, chained
        to the SDK's error. Keyed on ``as_of()``, never on the variables: without it, every error is
        relayed unchanged.

        A search on a field without a full-text index (v0.35.0) raises ``SurrealDbError`` naming the field, from
        ``None``: the server's text, which on SurrealDB 3.x quotes the search text, stays its suppressed
        ``__context__`` (``search.missing_index_refusal()``). In a transaction that error replaces the SDK's in the
        transaction's failures, so the commit that fails afterwards chains it, never the server's text.

        A query vector whose length is not its vector index's dimension (v0.36.0) raises ``SurrealDbError`` naming the
        field and both lengths, from the server's error, whose text holds nothing else (``vector.dimension_refusal()``):
        a read, and a write by filter outside a transaction — its ``LET`` selects the nearest records' ids. In a
        transaction that error replaces the SDK's in the transaction's failures, as above.

        Both errors name ``search()`` and ``similar_to()``, or the method that set ``_refusal_subject`` on this
        queryset (``hybrid_search()``'s private copy, v0.37.0).

        Args:
            query: the statement.
            variables: its bound values (the queryset's ``variables()`` when ``None``).
            subquery_tables: the tables a ``LET`` of the statement still reads (v0.27.0; empty: the behaviour
                above, exactly). While the set is not empty, only the server's own missing-table error reads as
                anything but itself (:meth:`_reads_as_empty`): naming one of these tables, and confirmed by the
                server, it asks :meth:`_run_compiled` to send the statement again with that subquery empty;
                naming another table, it is the outer table's and reads as no rows. Every other error is relayed.
            searched: the columns the statement searches, each mapped to its field as given — this queryset's and
                its subqueries' (:meth:`_searched_columns`); ``None``: this queryset's own.
            knn_fields: the columns the statement searches with a KNN, each mapped to its field as given — this
                queryset's and its subqueries' (:meth:`_knn_columns`); ``None``: this queryset's own.

        Raises:
            _MissingSubqueryTable: the server confirmed that a table of *subquery_tables* does not exist (caught by
                :meth:`_run_compiled`).
            SurrealDbError: a searched field has no full-text index; a query vector's length is not its vector
                index's dimension.
        """
        vars_ = variables if variables is not None else self._variables
        columns = searched if searched is not None else self._searched_columns()
        vectors = knn_fields if knn_fields is not None else self._knn_columns()
        # Each refusal's own subject (search(), similar_to()) unless this queryset names its caller (v0.37.0).
        subject = {} if self._refusal_subject is None else {"subject": self._refusal_subject}
        if self._tx is not None:
            try:
                return await self._tx.run_read(query, vars_)  # type: ignore[no-any-return]
            except ServerError as exc:
                # A read the ORM answers itself (no rows, or sent again without the missing subquery table) does not
                # fail the transaction (issue #268); an error relayed does.
                try:
                    empty = await self._reads_as_empty(exc, subquery_tables)
                except _subquery._MissingSubqueryTable:
                    self._tx._absorb_failure(exc)
                    raise
                if empty:
                    self._tx._absorb_failure(exc)
                    return []
                refusal = _search.missing_index_refusal(exc, columns, **subject)
                if refusal is not None:
                    # The transaction still fails as a whole, with the ORM's error as its cause, never the server's.
                    self._tx._absorb_failure(exc)
                    self._tx._note_failure(refusal)
                    raise refusal from None
                refusal = _vector.dimension_refusal(exc, vectors, **subject)
                if refusal is not None:
                    self._tx._absorb_failure(exc)
                    self._tx._note_failure(refusal)
                    raise refusal from exc
                raise
        client = await self._manager().get_client()
        try:
            return await client.query(query, _encoding.encode(vars_))  # type: ignore
        except ServerError as exc:
            if await self._reads_as_empty(exc, subquery_tables):
                return []
            refusal = _search.missing_index_refusal(exc, columns, **subject)
            if refusal is not None:
                raise refusal from None
            refusal = _vector.dimension_refusal(exc, vectors, **subject)
            if refusal is not None:
                raise refusal from exc
            if self._as_of is not None and _time_travel.UNSUPPORTED_MARKER in str(exc):
                raise SurrealDbError(_time_travel.server_refused(exc)) from exc
            raise

    def _searched_columns(self, compiler: SubqueryCompiler | None = None) -> dict[str, str]:
        """The columns a statement of this queryset searches, each mapped to its field as given (v0.35.0): those of
        its subqueries (``compiler.searched``), then this queryset's own ``search()`` conditions."""
        columns = {} if compiler is None else {column: field for (_, column), field in compiler.searched.items()}
        return {**columns, **{column: field for field, column, _ in self._search}}

    def _knn_columns(self, compiler: SubqueryCompiler | None = None) -> dict[str, str]:
        """The columns a statement of this queryset searches with a KNN, each mapped to its field as given (v0.36.0):
        this queryset's own ``similar_to()``, then its subqueries' (``compiler.knn_fields``)."""
        columns = {} if self._similar is None else {self._similar.column: self._similar.field}
        for (_, column), field in ({} if compiler is None else compiler.knn_fields).items():
            columns.setdefault(column, field)
        return columns

    async def _reads_as_empty(self, exc: ServerError, subquery_tables: frozenset[str]) -> bool:
        """Whether the server's error *exc* reads as no rows; ``False`` relays it.

        Without *subquery_tables*, every ``NotFoundError`` reads as no rows (a table that does not exist, SurrealDB
        3.x), as before v0.27.0. While a ``LET`` reads *subquery_tables* (v0.27.0), only the server's own
        missing-table error is read (:func:`subquery.missing_table`): the sentence inside another error, which may
        quote a value the statement read, is relayed. That error:

        - naming a table of *subquery_tables*: the server is asked whether that table exists
          (:meth:`_table_is_missing`); only when it confirms it does not is ``_MissingSubqueryTable`` raised, so
          :meth:`_run_compiled` sends the statement again with that subquery empty. Otherwise the error is relayed.
          The outer table is not set apart: when a subquery reads it too, the ``LET`` runs first and its error
          names it (SurrealDB 3.1 as a ``ThrownError``).
        - naming another table, as a ``NotFoundError``: the outer table's own, which reads as no rows. Once no
          ``LET`` reads the outer table, an error naming it is that.

        Raises:
            _MissingSubqueryTable: the server confirmed that a table of *subquery_tables* does not exist.
        """
        if not subquery_tables:
            return isinstance(exc, NotFoundError)
        table = _subquery.missing_table(exc)
        if table is None:
            return False
        if table in subquery_tables:
            if await self._table_is_missing(table):
                raise _subquery._MissingSubqueryTable(table) from exc
            return False
        return isinstance(exc, NotFoundError)

    async def _table_is_missing(self, table: str) -> bool:
        """Whether the server confirms that *table* does not exist (v0.27.0): ``subquery.TABLE_PROBE``, the name
        bound, answered with the server's own ``NotFoundError`` naming that very table.

        Sent where the failed read ran: inside the queryset's transaction (the same view of the tables, which a
        3.x interactive transaction keeps open after a failed read), else on the shared client. Rows, ``[]`` (the
        table exists) or any other server error mean "not confirmed", and the read's own error is relayed; an
        error that is not the server's (a lost connection) propagates.
        """
        variables = {"table": table}
        try:
            if self._tx is not None:
                await self._tx.run_read(_subquery.TABLE_PROBE, variables)
            else:
                client = await self._manager().get_client()
                await client.query(_subquery.TABLE_PROBE, _encoding.encode(variables))
        except NotFoundError as exc:
            # The check's answer, never a failed statement of the transaction (issue #268).
            if self._tx is not None:
                self._tx._absorb_failure(exc)
            return _subquery.missing_table(exc) == table
        except ServerError as exc:
            if self._tx is not None:
                self._tx._absorb_failure(exc)
            return False
        return False

    async def _run_compiled(
        self, compile_statement: Callable[[SubqueryCompiler], tuple[str, dict[str, Any]]], *, write: bool = False
    ) -> Any:
        """Compile a statement of this queryset with a new subquery compiler and run it (v0.27.0).

        A statement whose ``LET`` s read no table — no subquery, or every subquery table already bound to ``[]`` —
        is today's call, ``_execute_query(query, variables)``. Otherwise, when SurrealDB 3.x says that a subquery
        table does not exist and confirms it when asked (:meth:`_reads_as_empty`), the statement is compiled again
        with every subquery of that table bound to ``[]`` and sent again: a missing subquery table reads as empty,
        as on 2.x, where the server reads it so. At most two more requests per subquery table (the server's
        confirmation, the statement again), and only then; any other error is the server's, relayed.

        With ``cache()`` (v0.28.0), a read outside a transaction is answered from the query cache when it can be
        (:meth:`_run_cached`); a write, a read inside a transaction or a read without ``cache()`` runs as above.

        In both, the statement's links are checked once it is compiled (v0.29.0, :meth:`_check_links`), before it is
        sent, added to a transaction or looked up in the cache; its subqueries were checked while it compiled.

        Args:
            compile_statement: compiles the statement with the compiler it is given; returns its text and variables.
            write: a write by filter: inside a transaction it is added to it, never sent again (the server's
                error is the transaction's).
        """
        if write or self._cache is None or self._tx is not None:
            return await self._run_uncached(compile_statement, write=write)
        return await self._run_cached(compile_statement)

    async def _run_uncached(
        self,
        compile_statement: Callable[[SubqueryCompiler], tuple[str, dict[str, Any]]],
        *,
        write: bool = False,
        warn: bool = True,
        knn_prefilter: bool | None = None,
    ) -> Any:
        """The loop of :meth:`_run_compiled`, as in v0.27.0: compile, send, and on a confirmed missing subquery
        table compile again with that subquery bound to ``[]`` and send again.

        The filter values' warnings are given by the first compilation only, and only with *warn* (v0.28.1):
        ``False`` when :meth:`_run_cached` compiled the statement for this read already. The fields the statement's
        subqueries search are passed on (v0.35.0, ``searched=``), so a missing-index error names them, and so are the
        fields their ``similar_to()`` searches (v0.36.0, ``knn_fields=``), so a dimension error names them.

        A KNN next to other conditions (v0.36.0), or an exact scan (``compiler.knn_conditions``), is compiled for the
        line: *knn_prefilter* ``None`` decides it once, right after the first compilation — the line read once per
        client (``time_travel.cached_major()``), the form below SurrealDB 3 compiled again before anything is checked,
        queued or sent — and every later compilation of this read or write (a re-send for a missing subquery table
        included) uses that decision; :meth:`_run_cached` passes the one it took."""
        empty: frozenset[str] = frozenset()
        while True:
            compiler = self._subquery_compiler(empty, warn=warn, knn_prefilter=bool(knn_prefilter))
            warn = False
            query, variables = compile_statement(compiler)
            if knn_prefilter is None:
                knn_prefilter = await self._below_3(compiler)
                if knn_prefilter:
                    continue
            self._check_links()
            if write and self._tx is not None:
                return await self._tx.add(query, variables)
            read = frozenset(compiler.tables) - empty
            if not read:
                return await self._execute_query(query, variables)
            # The fields the subqueries search (v0.35.0), which a missing-index error names; _execute_query() maps
            # this queryset's own. Only a subquery that searches adds the argument: the call is otherwise v0.27.0's.
            # The same for the fields their similar_to() searches (v0.36.0), which a dimension error names.
            searched: dict[str, Any] = {"searched": self._searched_columns(compiler)} if compiler.searched else {}
            if compiler.knn_fields:
                searched["knn_fields"] = self._knn_columns(compiler)
            try:
                return await self._execute_query(query, variables, subquery_tables=read, **searched)
            except _subquery._MissingSubqueryTable as missing:
                empty |= {missing.table}

    async def _run_cached(self, compile_statement: Callable[[SubqueryCompiler], tuple[str, dict[str, Any]]]) -> Any:
        """A read of a queryset with ``cache()``, outside a transaction (v0.28.0).

        The statement is compiled once to key the read and decide what it depends on: the model's
        table and every table a subquery reads; ``EDGES`` when one of them is an edge model's (every delete
        invalidates it); "any table" when the statement follows links (``FETCH``, a dotted path, a graph arrow) or
        a ``select()`` item is neither ``*`` nor a plain field path (``author.*``: projection semantics read the
        linked table), or it reads a materialized view model (v0.32.0), its own or a subquery's: SurrealDB recomputes
        the view from tables the ORM does not track. On a miss :meth:`_run_uncached` sends it, compiling it again
        (compilation is pure), so the missing-subquery-table re-send happens inside the read; only the first
        compilation gives the filter values' warnings, once per read.

        A KNN next to other conditions (v0.36.0), or an exact scan, is keyed in its line's form: the line is read after
        the first compilation and, below SurrealDB 3, the statement compiled again for the key; the decision is passed
        on to :meth:`_run_uncached`, so a read the compilation refuses stores nothing.
        """
        compiler = self._subquery_compiler()
        query, variables = compile_statement(compiler)
        knn_prefilter = await self._below_3(compiler)
        if knn_prefilter:
            compiler = self._subquery_compiler(warn=False, knn_prefilter=True)
            query, variables = compile_statement(compiler)
        self._check_links()
        tables = {self._model_table, *compiler.tables}
        if self.model.__surreal_edge__ or compiler.edge_tables:
            tables.add(EDGES)
        any_table = (
            " FETCH " in query
            or follows_links(query)
            or any(item != "*" and not VALID_FIELD_PATTERN.match(item) for item in self.select_item)
            or self.model._is_view()
            or bool(compiler.view_tables)
        )
        return await self._cached_read(
            "query",
            query,
            variables,
            frozenset(tables),
            any_table,
            lambda: self._run_uncached(compile_statement, warn=False, knn_prefilter=knn_prefilter),
        )

    async def _below_3(self, compiler: SubqueryCompiler) -> bool:
        """Whether the statement *compiler* compiled is for a line below SurrealDB 3 and needs the form with the
        conditions next to its KNN selected first (v0.36.0), or an exact scan's nearest ranked by distance then id: only
        when it has such conditions or such a scan (``compiler.knn_conditions``) is the line read, once per client
        (``time_travel.cached_major()``) — on the connection's client, an interactive transaction's included. A version
        that cannot be read counts as 3."""
        if not compiler.knn_conditions:
            return False
        return await _time_travel.cached_major(await self._manager().get_client()) < 3

    async def _cached_read(
        self,
        kind: str,
        statement: str,
        variables: Mapping[str, Any],
        tables: frozenset[str],
        any_table: bool,
        run: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Answer a read of a queryset with ``cache()`` from the query cache, or *run* it and store its rows (v0.28.0).

        The one read frontier of the cache: every compiled read (:meth:`_run_cached`), ``get(<id>)``
        and ``all()``. The rows returned — a copy of the cached ones on a hit — go through the caller's usual
        post-processing, as on a miss. A TTL of ``0`` never looks up nor stores; a read the cache cannot key (a
        client without a scope, no CBOR encoder, a value the SDK cannot send) goes to the database, as does one a
        write in flight concerns. The cache's own failures never fail the read; *run*'s errors propagate, and an
        error is never cached.

        Args:
            kind: ``"query"`` (a compiled statement) or ``"select"`` (the SDK's ``select()``, *statement* empty).
            statement: the statement as compiled, as it is sent.
            variables: its bound values (``{"target": …}`` for a ``select``).
            tables: what the read depends on, ``EDGES`` included.
            any_table: the read follows links: any write invalidates it.
            run: sends the read and returns its rows.
        """
        request = cast(CacheRequest, self._cache)
        ttl = request.ttl if request.ttl is not None else QueryCache._default()
        if ttl == 0:
            QueryCache._count_miss()
            return await run()
        manager = self._manager()
        client = await manager.get_client()
        scope = manager._cache_scope(client)
        key = None if scope is None else statement_key(scope, kind, statement, variables)
        if scope is None or key is None:
            QueryCache._count_miss()
            return await run()
        hit, data, ticket = QueryCache._begin_read(key, tables, any_table, scope, ttl)
        if hit:
            return data
        rows = await run()
        if ticket is not None:
            QueryCache._finish_read(ticket, rows)
        return rows

    async def exec(self) -> Any:
        """
        Execute the compiled query and return the results.

        When ``values()`` and ``annotate()`` are used, returns a list of dictionaries with
        the grouped fields and aggregation results. With ``GeoDistance`` annotations, or the search
        annotations (``SearchScore``, ``SearchHighlight``, ``SearchOffsets``, v0.35.0), returns the
        model instances, each carrying its values as attributes (see :meth:`annotate`). With
        ``similar_to()`` (v0.36.0) and no aggregation — ``values()`` alone included —, the same: each
        instance carries its distance as ``_knn_distance`` (see :meth:`similar_to`).
        Served from the query cache with ``cache()`` (v0.28.0), as new instances.

        Returns:
            list[BaseSurrealModel] | list[dict]: Model instances or dictionaries for GROUP BY queries.
            Under ``as_of()`` the instances are snapshots (v0.26.0).

        Raises:
            ValueError: a search annotation whose match reference ``search()`` did not give this queryset, before
                any request (v0.35.0).
            SurrealDbError: a search annotation with ``similar_to()`` on SurrealDB 2.x or the embedded engine
                (v0.36.0), once the line is read and before the statement is sent.
        """
        self._check_search_references()
        # A similar_to() statement projects the distance: without an aggregation (values() alone compiles no GROUP BY)
        # it takes the per-record path, so _knn_distance never becomes an extra="allow" model's extra.
        per_record = bool(
            self._geo_annotations or self._search_annotations or (self._similar is not None and not self._annotations)
        )
        # The values()+annotate() branch compiles no FETCH: only the other two return fetched records.
        moment = await self._time_travel_moment(fetches=per_record or not self._annotations)
        if per_record:
            return await self._exec_with_annotations(moment)

        if self._annotations:
            results = await self._run_compiled(lambda c: self._compile_group_by_query(moment, subqueries=c))
            if not isinstance(results, list):
                return []
            return [self._py_keyed(row) for row in results]

        results = await self._run_compiled(lambda c: self._compile_query(moment, subqueries=c))
        try:
            if isinstance(results, list):
                return self._mark_snapshots(self.model.from_db(results), moment)
            data = cast(dict, results[0])
            return self._mark_snapshots(self.model.from_db(data.get("result", [])), moment)
        except ValidationError as e:
            logger.info(f"Pydantic invalid format for the class, returning dict value: {e}")
            return [self._py_keyed(row) for row in results] if isinstance(results, list) else []

    async def _exec_with_annotations(self, moment: datetime | None = None) -> Any:
        """Run the per-record ``SELECT`` of the ``GeoDistance`` annotations (v0.24.0), of the search annotations
        (v0.35.0) and of ``similar_to()``'s distance, ``_knn_distance`` (v0.36.0).

        The alias keys are taken out of each row before validation, so they never reach a field, an
        ``extra="allow"`` model's extras or a later write, then set on the instance with
        ``object.__setattr__``. Rows that do not validate (a ``select()`` that omits a required
        field…) come back as dicts, alias keys included, as ``exec()`` returns them today. Under
        ``as_of()``, *moment* is the read's resolved moment and the instances are snapshots (v0.26.0).
        """
        self._reject_per_record_mixing(self._annotations, grouped=bool(self._group_by_fields))
        results = await self._run_compiled(lambda c: self._compile_query(moment, subqueries=c))
        rows = results if isinstance(results, list) else []
        aliases = [*self._geo_annotations, *self._search_annotations]
        if self._similar is not None:
            aliases.append(_vector.DISTANCE_ALIAS)
        stripped: list[dict[str, Any]] = []
        annotated: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            annotated.append({alias: record.pop(alias, None) for alias in aliases})
            stripped.append(record)
        try:
            instances = cast(list[Any], self.model.from_db(stripped))
        except ValidationError as e:
            logger.info(f"Pydantic invalid format for the class, returning dict value: {e}")
            return [self._py_keyed(row) for row in rows]
        for instance, values in zip(instances, annotated, strict=True):
            for alias, value in values.items():
                object.__setattr__(instance, alias, value)
        return self._mark_snapshots(instances, moment)

    async def hybrid_search(
        self,
        *,
        vector_field: str,
        vector: Sequence[float],
        vector_limit: int = _hybrid.DEFAULT_LIMIT,
        text_field: str,
        text_query: str,
        text_limit: int = _hybrid.DEFAULT_LIMIT,
        rrf_k: int = _hybrid.DEFAULT_RRF_K,
        ef: int | None = None,
        dist: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fuse a vector search and a full-text search into one ranking by Reciprocal Rank Fusion (v0.37.0) — the full
        SurrealDB-ORM's method.

        ::

            results = await Document.objects().hybrid_search(
                vector_field="embedding", vector=query_vector, vector_limit=20,
                text_field="content", text_query="machine learning", text_limit=20,
            )
            for row in results:
                print(row["title"], row["_rrf_score"])

        Two ranked lists of the queryset's records: the ``vector_limit`` nearest to ``vector`` by ``vector_field`` (as
        ``similar_to()`` finds them: its HNSW index, or with ``dist=`` an exact scan), and the ``text_limit`` best
        matches of ``text_query`` in ``text_field`` by BM25 score (as ``search()`` and ``SearchScore`` find them: its
        full-text index). Each record gets the fused score Σ 1/(``rrf_k`` + rank) over the lists it is in, its rank
        counted from 1, and the records come back best first; equal scores keep the vector list's order first, then
        the full-text list's for the records it alone found. SurrealDB 3.x fuses the lists with its ``search::rrf()``;
        SurrealDB 2.x and the embedded engine have no such function, so the ORM fuses them with the same formula: the
        records, scores and order are the same on every line. The vector and the text are bound as query variables,
        never written into the statement.

        Each record is a dict — the full SurrealDB-ORM's result: its fields keyed by their Python names and
        ``_rrf_score``, plus ``_knn_distance`` (its distance, ``None`` when only the full-text search found it) and
        ``_search_score`` (its BM25 score, ``None`` when only the vector search found it). An exact scan (``dist=``)
        gives identical calls one vector list on every line: records at an equal distance at the ``vector_limit``-th
        place are kept by id on SurrealDB 2.x and the embedded engine, as ``similar_to(dist=...)`` keeps them.

        Each search needs its index. Without a full-text index on ``text_field``, SurrealDB 3.3, 2.x and the embedded
        engine refuse the search (``SurrealDbError`` naming the field), while SurrealDB 3.1 and 3.2 match nothing.
        Without a vector index on ``vector_field``, the vector search finds nothing on SurrealDB 3.3, 2.x and the
        embedded engine, and lists records with no distance on 3.1 and 3.2: pass ``dist=`` for an exact scan that needs
        no index.

        The queryset's conditions — ``filter()``, ``Q``, ``nearby()`` and a ``Subquery`` filter — select the records of
        both lists; ``select()`` and ``fetch()`` apply to both (``id`` is always read: the lists are fused by it);
        ``limit()`` and ``offset()`` page through the fused records, of which there are at most ``vector_limit +
        text_limit``. ``cache()``, ``objects(tx=)`` (reads need an interactive transaction: SurrealDB 3.x over
        WebSocket), named connections and ``variables()`` apply as for ``exec()``. ``order_by()``, ``annotate()``,
        ``values()``, ``search()``, ``similar_to()`` and ``as_of()`` are refused: the method runs its own searches and
        orders the records itself. The queryset is not changed.

        Args:
            vector_field: The vector field: a Python field name (an aliased field compiles its column) or a dotted path;
                not the record id nor, on an edge model, an endpoint.
            vector: The query vector: a ``list`` or ``tuple`` of real numbers (not a ``bool`` nor a ``str``), not empty,
                each finite — a NumPy array with ``.tolist()``. When the model declares the field's dimension (a
                ``VectorField[d]`` or ``Vector(d)`` on it, or a vector ``Index`` on its column), exactly ``d`` numbers.
            vector_limit: ``K``, the number of nearest records the vector list holds: an ``int`` from 1 to 10000.
            text_field: The full-text field, as ``vector_field``.
            text_query: The text to match in ``text_field``; an empty text matches nothing, so the vector list alone is
                fused.
            text_limit: The number of best matches the full-text list holds: an ``int`` from 1 to 10000.
            rrf_k: The constant added to each rank: an ``int`` from 0 to 10000.
            ef: The HNSW search's candidate list: ``None`` (100) or an ``int`` from 1 to 10000, raised to
                ``vector_limit``.
            dist: An exact scan by this distance instead of the index's search: ``EUCLIDEAN``, ``COSINE``,
                ``MANHATTAN``, ``CHEBYSHEV`` or ``HAMMING``, in any case; not with ``ef``.

        Returns:
            list[dict[str, Any]]: The fused records, best first, each a dict of its fields and ``_rrf_score``,
            ``_knn_distance`` and ``_search_score``.

        Raises:
            TypeError: ``vector_field`` or ``text_field`` is not a ``str``; ``vector`` is not a ``list`` or a ``tuple``,
                or holds an item that is not a number; ``vector_limit``, ``text_limit``, ``rrf_k`` or ``ef`` is not an
                ``int`` (a ``bool`` included); ``dist`` or ``text_query`` is not a ``str``.
            ValueError: the queryset holds ``annotate()``, ``order_by()``, ``search()``, ``similar_to()`` or
                ``values()``; a model field is stored as ``rrf_score``, ``_rrf_score``, ``_knn_distance``,
                ``_search_score``, ``_hybrid_pos``, ``_hybrid_vec_pos`` or ``_hybrid_text_pos``, names the method uses
                for its own values; ``vector_field`` or ``text_field`` is not a field name or a dotted path, or is the
                record id or an edge endpoint; ``vector`` is empty, holds an item that is not finite, or its length is
                not the field's declared dimension; ``vector_limit``, ``text_limit`` or ``ef`` is outside 1 to 10000,
                ``rrf_k`` outside 0 to 10000; both ``ef`` and ``dist``; a distance the ORM does not write; a zero vector
                with ``COSINE`` (``dist="cosine"``, or no ``dist`` and the field's vector index is ``COSINE``). Each
                raised before any request, the queryset left unchanged.
            SurrealDbError: the queryset reads ``as_of()``, before any request. After the request: ``text_field`` has
                no full-text index (SurrealDB 3.3, 2.x and the embedded engine); the model declares no dimension for
                ``vector_field`` and the vector's length is not its vector index's — in a transaction, the commit then
                fails with that error as its cause.
        """
        clauses = {
            "annotate()": bool(self._annotations),
            "order_by()": self._order_by is not None,
            "search()": bool(self._search),
            "similar_to()": self._similar is not None,
            # values() is the only public setter of _group_by_fields.
            "values()": bool(self._group_by_fields),
        }
        offenders = [name for name, present in clauses.items() if present]
        if offenders:
            raise ValueError(_hybrid.clauses_refused(offenders))
        if self._as_of is not None:
            raise SurrealDbError(_hybrid.time_travel_refused())
        for name in self.model.model_fields:
            stored = self.model.to_db_field(name)
            if stored in _hybrid.RESERVED_COLUMNS:
                raise ValueError(_hybrid.column_refused(self.model.__name__, name, stored))
        # Every column is read with to_db_field(), never _column(), which records a dotted path's head on this queryset.
        endpoint_columns = self.model._endpoint_columns()
        _hybrid.checked_field(vector_field, "vector_field")
        column = self.model.to_db_field(vector_field)
        if column == RECORD_ID_COLUMN or column in endpoint_columns:
            endpoint = column in endpoint_columns
            raise ValueError(_vector.field_refused(vector_field, endpoint=endpoint, subject=_hybrid.SUBJECT))
        knn = self._knn_search(
            vector_field, column, vector, vector_limit, ef, dist, subject=_hybrid.SUBJECT, limit_arg="vector_limit"
        )
        _hybrid.checked_field(text_field, "text_field")
        column = self.model.to_db_field(text_field)
        if column == RECORD_ID_COLUMN or column in endpoint_columns:
            endpoint = column in endpoint_columns
            raise ValueError(_search.field_refused(text_field, endpoint=endpoint, subject=_hybrid.SUBJECT))
        if not isinstance(text_query, str):
            raise TypeError(_search.text_refused(text_field, text_query, subject=_hybrid.SUBJECT))
        listed = _vector.checked_limit(text_limit, subject=_hybrid.SUBJECT, arg="text_limit")
        constant = _hybrid.checked_rrf_k(rrf_k)
        runner = self._hybrid_runner(knn, text_field, text_query)
        below_3 = await _time_travel.cached_major(await runner._manager().get_client()) < 3
        results = await runner._run_compiled(
            lambda compiler: runner._compile_hybrid(compiler, below_3=below_3, text_limit=listed, rrf_k=constant)
        )
        rows = results if isinstance(results, list) else []
        if below_3:
            # No search::rrf() below SurrealDB 3: the ORM fuses the two lists, then pages the fused records.
            fused = _hybrid.fuse(rows, k=constant, vector_limit=knn.k)
            start = self._offset or 0
            rows = fused[start:] if self._limit is None else fused[start : start + self._limit]
        return runner._hybrid_rows(rows)

    def _hybrid_rows(self, rows: Sequence[Any]) -> list[dict[str, Any]]:
        """The dicts ``hybrid_search()`` returns (v0.37.0), built from the fused *rows* of either line — the 3.x
        statement's, or ``hybrid.fuse()``'s below 3 —, which are never changed.

        Each row that is an object gives a new dict: SurrealDB's ``rrf_score`` becomes ``_rrf_score`` (replacing a
        stored one), the ORM's positions are dropped, ``_knn_distance`` and ``_search_score`` are ``None`` when the list
        that computes them did not find the record, and the columns are keyed by their Python names, as ``exec()``'s
        dict rows (:meth:`_py_keyed`). The values are SurrealDB's: no validation, no instance. Anything else is
        skipped.
        """
        built: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            record = dict(row)
            record[_hybrid.RRF_SCORE] = record.pop(_hybrid.SERVER_SCORE, None)
            for position in (_hybrid.POSITION, _hybrid.VECTOR_POSITION, _hybrid.TEXT_POSITION):
                record.pop(position, None)
            record.setdefault(_vector.DISTANCE_ALIAS, None)
            record.setdefault(_hybrid.SEARCH_SCORE, None)
            built.append(cast(dict[str, Any], self._py_keyed(record)))
        return built

    async def first(self) -> Any:
        """
        Execute the query and return the first result.

        The queryset is left untouched: the ``LIMIT 1`` applies to this call only, so the
        same queryset can be reused (or re-executed) afterwards and still yields every
        matching row.

        Returns:
            The first model instance, or raises SurrealDbNotFoundError if no results.
        """
        original_limit = self._limit
        try:
            self._limit = 1
            results = await self.exec()
        finally:
            self._limit = original_limit

        if results:
            return results[0]

        raise SurrealDbNotFoundError("No result found.")

    async def get(self, id_item: Any = None) -> Any:
        """
        Retrieve a single record by its unique identifier or based on the current filters.

        Under ``as_of()`` (v0.26.0) the record is read as it was at the moment, and returned as a
        snapshot: by id, a record deleted since is still found, one created later is not
        (``SurrealDbNotFoundError``). Served from the query cache with ``cache()`` (v0.28.0), by id
        or not; a record found missing is cached too.

        Args:
            id_item: The unique identifier of the item to retrieve.

        Returns:
            The retrieved model instance or dictionary.
        """
        if id_item:
            # Same rule as filter(id=...): an int is an *integer* record id, a str a string
            # one. Building RecordID(table, str(id_item)) made get(5) unreachable on a model
            # with `id: int` (issue #159).
            record_id = coerce_record_id(id_item, self._model_table)

            if self._as_of is not None:
                # A record-id read at a moment is right on every versioned store: no per-record check.
                moment = await self._time_travel_moment()
                rows = await self._execute_query(
                    f"SELECT * FROM $rid{_time_travel.VERSION_CLAUSE};",
                    {"rid": record_id, _time_travel.MOMENT_VARIABLE: moment},
                )
                if not isinstance(rows, list) or not rows or not rows[0]:
                    raise SurrealDbNotFoundError("No result found.")
                return self._mark_snapshots(self.model.from_db(rows[0]), moment)

            if self._tx is not None:
                try:
                    rows = await self._tx.run_read("SELECT * FROM $rid;", {"rid": record_id})
                except NotFoundError as exc:
                    # "No record" is an answer, not a failed statement of the transaction (issue #268).
                    self._tx._absorb_failure(exc)
                    raise SurrealDbNotFoundError("No result found.") from None
                data = rows[0] if isinstance(rows, list) and rows else rows
                if not data:
                    raise SurrealDbNotFoundError("No result found.")
                return self.model.from_db(data)

            if self._cache is not None:
                return await self._cached_get(record_id)

            client = await self._manager().get_client()
            try:
                data = await client.select(record_id)
            except NotFoundError:
                raise SurrealDbNotFoundError("No result found.") from None
            if isinstance(data, list):
                if len(data) == 0:
                    raise SurrealDbNotFoundError("No result found.")
                data = data[0]
            if data is None:
                raise SurrealDbNotFoundError("No result found.")
            return self.model.from_db(data)
        else:
            result = await self.exec()
            if len(result) > 1:
                raise SurrealDbError("More than one result found.")

            if len(result) == 0:
                raise SurrealDbNotFoundError("No result found.")
            return result[0]

    async def all(self) -> Any:
        """
        Fetch all records from the associated table.

        ``filter()`` and the other clauses are ignored; ``as_of()`` is not (v0.26.0): every record of the
        table as it was at the moment, returned as snapshots. Served from the query cache with ``cache()``
        (v0.28.0).
        """
        if self._as_of is not None:
            moment = await self._time_travel_moment()
            rows = await self._execute_query(
                f"SELECT * FROM {self._model_table} WHERE {_time_travel.GUARD}{_time_travel.VERSION_CLAUSE};",
                {_time_travel.MOMENT_VARIABLE: moment},
            )
            return self._mark_snapshots(self.model.from_db(rows if isinstance(rows, list) else []), moment)

        if self._tx is not None:
            try:
                rows = await self._tx.run_read(f"SELECT * FROM {self._model_table};", {})
            except NotFoundError as exc:
                # A missing table reads as no rows: not a failed statement of the transaction (issue #268).
                self._tx._absorb_failure(exc)
                return self.model.from_db([])
            return self.model.from_db(rows if isinstance(rows, list) else [])

        if self._cache is not None:
            return self.model.from_db(await self._cached_select(self._model_table, frozenset({self._model_table}), []))

        client = await self._manager().get_client()
        try:
            results = await client.select(self._model_table)
        except NotFoundError:
            return self.model.from_db([])
        return self.model.from_db(results)

    async def _cached_get(self, record_id: RecordID) -> Any:
        """``get(<id>)`` with ``cache()``, outside a transaction (v0.28.0): the SDK's ``select()`` of the record,
        answered from the query cache when it can be, then today's post-processing. "No record" is cached too: the
        hit raises ``SurrealDbNotFoundError`` as the miss does."""
        data = await self._cached_select(record_id, frozenset({record_id.table_name}), None)
        if isinstance(data, list):
            if len(data) == 0:
                raise SurrealDbNotFoundError("No result found.")
            data = data[0]
        if data is None:
            raise SurrealDbNotFoundError("No result found.")
        return self.model.from_db(data)

    async def _cached_select(self, target: RecordID | str, tables: frozenset[str], missing: Any) -> Any:
        """The SDK's ``select(target)`` through :meth:`_cached_read` (v0.28.0): keyed on the target,
        tagged with its table (and ``EDGES`` on an edge model; "any table" on a materialized view model, v0.32.0);
        *missing* when the server says it does not exist."""
        if self.model.__surreal_edge__:
            tables |= {EDGES}

        async def run() -> Any:
            client = await self._manager().get_client()
            try:
                return await client.select(target)
            except NotFoundError:
                return missing

        return await self._cached_read("select", "", {"target": target}, tables, self.model._is_view(), run)

    @_cache.invalidates("table", deletes=True, refuse=_queryset_refusals("delete_table()"))
    async def delete_table(self) -> bool:
        """
        Delete the associated table from the SurrealDB database.

        Returns True. A missing table is treated as already-absent (no-op),
        so this is safe for idempotent cleanup on SurrealDB 3.x (which raises
        NotFoundError) as well as 2.x (which is a no-op).

        It runs at once, outside the queryset's ``objects(tx=)`` transaction (a rollback does
        not bring the rows back), so the cached reads of the table — and of every edge table,
        whose edges die with the records — are invalidated when it ends, never at a commit. A
        materialized view (``TypeError``) and a queryset read ``as_of()`` (``SurrealDbError``) are
        refused before any request, and drop no cached read (#320).
        """
        client = await self._manager().get_client()
        with contextlib.suppress(NotFoundError):
            await client.delete(self._model_table)
        return True

    # ==================== Aggregation Methods ====================

    def values(self, *fields: str) -> Self:
        """
        Specify fields for GROUP BY operations.

        Args:
            *fields: Field names to group by.

        Returns:
            Self: The current QuerySet instance for method chaining.

        Raises:
            ValueError: a field name is invalid, or a per-record annotation is present — a ``GeoDistance``, or a
                search annotation (v0.35.0) — (it is computed per record, not per group).
        """
        for field in fields:
            validate_field_name(field, "GROUP BY field")
        self._reject_per_record_mixing(self._annotations, grouped=bool(fields))
        self._group_by_fields = [self._column(field) for field in fields]
        return self

    def annotate(self, **annotations: "Aggregation | GeoDistance | SearchScore | SearchHighlight | SearchOffsets") -> Self:
        """
        Add aggregation annotations, or per-record values — distances, search scores, highlights and offsets —, to
        the query.

        An ``Aggregation`` (``Count``, ``Sum``, ``Avg``, ``Min``, ``Max``) groups the records with
        ``values()``, and ``exec()`` returns one dict per group.

        A ``GeoDistance`` (v0.24.0, same API as the full SurrealDB-ORM) is computed per record::

            near = await (
                Restaurant.objects()
                .nearby("location", (-73.9855, 40.7580), max_distance=2_000)
                .annotate(dist=GeoDistance("location", (-73.9855, 40.7580)))
                .order_by("dist")
                .exec()
            )
            near[0].dist  # metres, a float; None for a record whose field is not a point

        So are the search annotations (v0.35.0, the full SurrealDB-ORM's ``SearchScore`` and ``SearchHighlight``,
        plus ``SearchOffsets``), each naming the match reference a :meth:`search` field was given (``@0@``,
        ``@1@`` …, in call order)::

            articles = await (
                Article.objects()
                .search(title="physics")
                .annotate(relevance=SearchScore(0), snippet=SearchHighlight("<b>", "</b>", 0))
                .order_by("-relevance")
                .exec()
            )
            articles[0].relevance  # BM25, a float
            articles[0].snippet    # 'Cooking with <b>physics</b>'

        ``SearchScore`` gives the field's BM25 score, ``SearchHighlight`` its text with each matched term wrapped in
        the tags (a list of texts for an array field; the text unchanged when the index has no ``HIGHLIGHTS``),
        ``SearchOffsets`` where the matched terms sit (``{"0": [{"s": 13, "e": 20}]}``). The tags are bound query
        variables. The references are checked when the queryset runs (``annotate()`` may come before ``search()``):
        a reference ``search()`` did not give, or a search annotation without ``search()``, raises before any
        request. The scores depend on the line: SurrealDB 3.x gives a term found in more than half the documents no
        weight, while 2.x and the embedded engine score it below zero, count an index's records again each time it
        is rebuilt and keep counting deleted records (README section 36): compare scores within one query.

        ``exec()``, ``first()`` and ``get()`` return model instances carrying the value as an
        attribute named after the alias; it is never written back (``model_dump()``, ``update()``,
        ``upsert()`` leave it out). Rows that do not validate (a ``select()`` that omits a required
        field) come back as dicts holding the alias. ``order_by("dist")``/``order_by("-dist")`` sort
        on it (ascending puts the ``None`` values first). The alias is a projection, not a stored
        column: ``filter(dist__lt=...)`` filters a column named ``dist``; bound the distance with
        ``nearby()``. On an ``extra="allow"`` model, a stored undeclared column with the alias's
        name is replaced by the distance in the results, and a later ``update()`` removes it: choose
        an alias no record stores. The field and the point are checked and copied here: changing
        the ``GeoDistance`` afterwards does not change this queryset. ``count()`` and the aggregations
        ignore every annotation; ``live()``, ``watch()``, ``changes()`` and a ``Subquery`` refuse a queryset
        holding one.

        Args:
            **annotations: The alias of each value, and an ``Aggregation``, a ``GeoDistance``, a ``SearchScore``,
                a ``SearchHighlight`` or a ``SearchOffsets``.

        Returns:
            Self: The current QuerySet instance for method chaining.

        Raises:
            TypeError: a value is none of those.
            ValueError: an invalid alias; for a per-record annotation, an alias that names a declared field
                or its column, ``id``, ``in``, ``out``, a name starting with ``_`` or ``model_`` or an
                attribute of the model (``save``, ``objects``…); for a ``GeoDistance``, an invalid field name or
                a field declared as a geometry that holds no point; a per-record annotation combined with an
                ``Aggregation`` or with ``values()``.
        """
        from .aggregations import Aggregation as AggregationClass

        distances: dict[str, tuple[str, GeometryPoint]] = {}
        searches: dict[str, SearchAnnotation] = {}
        for alias, value in annotations.items():
            validate_alias_name(alias)
            if isinstance(value, GeoDistance):
                distances[alias] = self._geo_distance_snapshot(alias, value)
            elif isinstance(value, SearchScore | SearchHighlight | SearchOffsets):
                self._check_annotation_alias(alias, type(value).__name__, _search.annotation_noun(value))
                searches[alias] = value
            elif not isinstance(value, AggregationClass):
                raise TypeError(
                    f"annotate() argument {alias!r} must be an Aggregation instance, a GeoDistance, a SearchScore, "
                    f"a SearchHighlight or a SearchOffsets, got {type(value).__name__}"
                )
        self._reject_per_record_mixing({**self._annotations, **annotations}, grouped=bool(self._group_by_fields))
        self._annotations.update(annotations)
        # An alias given again is replaced, whatever its kind: each per-record dict keeps only the others.
        kept = {alias: snapshot for alias, snapshot in self._geo_annotations.items() if alias not in annotations}
        self._geo_annotations = {**kept, **distances}
        kept_searches = {alias: value for alias, value in self._search_annotations.items() if alias not in annotations}
        self._search_annotations = {**kept_searches, **searches}
        return self

    def _geo_distance_snapshot(self, alias: str, distance: GeoDistance) -> tuple[str, GeometryPoint]:
        """Check a ``GeoDistance`` annotation against the model; return its column and point.

        The alias must not shadow anything a record or an instance already has: ``SELECT *, … AS
        <alias>`` replaces a stored column of that name in the result (``AS id`` replaces the id),
        and the value is set on the instance as an attribute.
        """
        self._check_annotation_alias(alias, "GeoDistance", "distance")
        field = _geo._distance_field(distance.field)
        validate_field_name(field, "GeoDistance field")
        self._check_point_field(field, "GeoDistance")
        return self._column(field), _geo._as_point(distance.point)

    def _check_annotation_alias(self, alias: str, kind: str, noun: str) -> None:
        """Refuse *alias* for a per-record annotation of class *kind*, whose value is a *noun* (``distance``,
        ``score``, ``highlight``, ``offsets``), when it would shadow what a record or an instance already has."""
        reason = self._reserved_annotation_alias(alias, noun)
        if reason is not None:
            raise ValueError(f"annotate() alias {alias!r} {reason}; choose another alias for the {kind}")

    def _reserved_annotation_alias(self, alias: str, noun: str) -> str | None:
        """Why ``alias`` cannot name a per-record annotation whose value is a *noun* on this model, or ``None`` when
        it can (v0.24.0's rule for ``GeoDistance``, v0.35.0's for the search annotations)."""
        if alias in ("id", "in", "out"):
            return "is reserved: it names the record's id or an edge endpoint"
        if alias.startswith(("_", "model_")):
            return "is reserved: names starting with '_' or 'model_' belong to Pydantic"
        model = self.model
        if alias in model.model_fields:
            return f"is a field of {model.__name__}: the {noun} would replace its value in the results"
        for name in model.model_fields:
            if model.to_db_field(name) == alias:
                return f"is the column of field {name!r}: the {noun} would replace its value in the results"
        if hasattr(model, alias):
            return f"is an attribute of {model.__name__}: the {noun} would shadow it on the instances"
        return None

    @staticmethod
    def _reject_per_record_mixing(annotations: Mapping[str, Any], *, grouped: bool) -> None:
        """Refuse a per-record annotation — a ``GeoDistance``, or a search annotation (v0.35.0) — next to an
        ``Aggregation`` or a ``values()`` grouping, naming the first per-record one in alias order."""
        per_record = [value for value in annotations.values() if isinstance(value, _PER_RECORD)]
        if per_record and (grouped or len(per_record) < len(annotations)):
            raise ValueError(_per_record_refused(per_record[0]))

    async def count(self) -> int:
        """
        Count the number of records matching the query.

        Served from the query cache with ``cache()`` (v0.28.0), as every aggregation is.

        Returns:
            The number of matching records.
        """
        moment = await self._time_travel_moment()
        results = await self._run_compiled(lambda c: self._compile_aggregation_query("count()", moment=moment, subqueries=c))

        if isinstance(results, list) and len(results) > 0:
            result = results[0]
            if isinstance(result, dict):
                return int(result.get("count", 0))
            return int(result)
        return 0

    async def sum(self, field: str) -> float | int:
        """
        Calculate the sum of a numeric field.

        Args:
            field: The name of the numeric field to sum.

        Returns:
            The sum of the field values, or 0 if no records match.
        """
        validate_field_name(field, "sum() field")
        moment = await self._time_travel_moment()
        expression = f"math::sum({self._column(field)})"
        results = await self._run_compiled(
            lambda c: self._compile_aggregation_query(expression, alias="sum", moment=moment, subqueries=c)
        )

        if isinstance(results, list) and len(results) > 0:
            result = results[0]
            if isinstance(result, dict):
                value = result.get("sum", 0)
                return value if value is not None else 0
            return result if result is not None else 0
        return 0

    async def avg(self, field: str) -> float:
        """
        Calculate the average of a numeric field.

        Args:
            field: The name of the numeric field to average.

        Returns:
            The average of the field values, or 0.0 if no records match.
        """
        validate_field_name(field, "avg() field")
        moment = await self._time_travel_moment()
        expression = f"math::mean({self._column(field)})"
        results = await self._run_compiled(
            lambda c: self._compile_aggregation_query(expression, alias="avg", moment=moment, subqueries=c)
        )

        if isinstance(results, list) and len(results) > 0:
            result = results[0]
            value = result.get("avg", 0.0) if isinstance(result, dict) else result
            # SurrealDB 3.x returns NaN for math::mean over an empty set; the ORM
            # contract is 0.0 for "no records".
            if value is None or (isinstance(value, float) and math.isnan(value)):
                return 0.0
            return float(value)
        return 0.0

    async def min(self, field: str) -> Any:
        """
        Find the minimum value of a field.

        Args:
            field: The name of the field to find the minimum value of.

        Returns:
            The minimum value, or None if no records match.
        """
        validate_field_name(field, "min() field")
        moment = await self._time_travel_moment()
        expression = f"math::min({self._column(field)})"
        results = await self._run_compiled(
            lambda c: self._compile_aggregation_query(expression, alias="min", moment=moment, subqueries=c)
        )

        if isinstance(results, list) and len(results) > 0:
            result = results[0]
            value = result.get("min") if isinstance(result, dict) else result
            # SurrealDB 3.x returns +inf for math::min over an empty set; the ORM
            # contract is None for "no records".
            if value is None or (isinstance(value, float) and (math.isinf(value) or math.isnan(value))):
                return None
            return value
        return None

    async def max(self, field: str) -> Any:
        """
        Find the maximum value of a field.

        Args:
            field: The name of the field to find the maximum value of.

        Returns:
            The maximum value, or None if no records match.
        """
        validate_field_name(field, "max() field")
        moment = await self._time_travel_moment()
        expression = f"math::max({self._column(field)})"
        results = await self._run_compiled(
            lambda c: self._compile_aggregation_query(expression, alias="max", moment=moment, subqueries=c)
        )

        if isinstance(results, list) and len(results) > 0:
            result = results[0]
            value = result.get("max") if isinstance(result, dict) else result
            # SurrealDB 3.x returns -inf for math::max over an empty set; the ORM
            # contract is None for "no records".
            if value is None or (isinstance(value, float) and (math.isinf(value) or math.isnan(value))):
                return None
            return value
        return None

    async def exists(self) -> bool:
        """
        Check if any records match the current query.

        It compiles the full projection, as ``exec()`` does: a search annotation (v0.35.0) is sent with its
        ``LIMIT 1`` and its match reference checked first, before any request; with ``similar_to()`` on SurrealDB 2.x
        or the embedded engine, where ``exec()`` refuses search annotations, they are left out (v0.36.0): the answer
        needs none.

        Returns:
            True if at least one record matches, False otherwise.

        Raises:
            ValueError: a search annotation whose match reference ``search()`` did not give this queryset.
        """
        self._check_search_references()
        moment = await self._time_travel_moment()
        original_limit = self._limit
        try:
            self._limit = 1
            results = await self._run_compiled(lambda c: self._compile_query(moment, subqueries=c, for_exists=True))

            if isinstance(results, list):
                return len(results) > 0
            return False
        finally:
            self._limit = original_limit

    # ==================== Bulk Operations ====================

    @_cache.invalidates("queryset", refuse=_queryset_refusals("bulk_create()"), refuse_args=_bulk_create_arguments)
    async def bulk_create(self, models: list[BaseSurrealModel]) -> list[BaseSurrealModel]:
        """
        Create multiple records in a single operation.

        Uses the SDK's ``insert()`` method for efficient bulk insertion.

        On an **edge model** (:class:`~surreal_orm_lite.edge.BaseSurrealEdge`) the batch is
        created with ``INSERT RELATION``, so every edge is a relation that graph traversal sees,
        on SurrealDB 2.x and 3.x alike: N edges without an id go through **one** call of the SDK's
        ``insert_relation()``; as soon as one edge has an explicit id, the whole batch goes
        through the guarded statement of :meth:`BaseSurrealEdge.save`, so an id that already
        exists raises :class:`SurrealDbError` ("already exists") and **nothing** of the batch is
        written. Every id is checked before any call (a ``RecordID`` of another table, or a
        ``bool``, is a ``ValueError``). The batch is one statement, but outside a transaction it
        is not atomic against the table's ``PERMISSIONS``: when they refuse some edges, the
        server writes the others and returns them only, and this raises :class:`SurrealDbError`
        ("Only k of n edges came back …") — the accepted edges stay written. No signals fire, as
        for nodes.

        Args:
            models: A list of model instances to create.

        Returns:
            A list of created model instances with their assigned IDs. Under
            ``objects(tx=…)``: the hydrated instances on an interactive transaction (WebSocket +
            SurrealDB 3.x), the input models unchanged on a buffered one (written at commit; an
            edge without an explicit id keeps ``id=None``). ``[]`` for an empty list, without any
            call.

        The materialized view, a queryset read ``as_of()``, then a snapshot in ``models`` are
        refused in this order, before any call and before the table is noted on the
        ``objects(tx=…)`` transaction or put in flight: a refused call leaves the transaction as it
        was and drops no cached read (#320).

        Raises:
            SurrealDbError: an edge id that already exists, edges the ``PERMISSIONS`` refused, or
                any other refusal of an edge batch by the server; the queryset reads ``as_of()``; a
                model read with ``as_of()`` (a read-only snapshot) in ``models``, before any call
                (v0.26.0).
            SurrealDbNotFoundError: an endpoint of an ``ENFORCED`` relation that does not exist.
            TypeError: the model is a materialized view (v0.32.0), before any call.
            ValueError: an edge id that is a ``RecordID`` of another table, or a ``bool``.

        Example::

            users = [User(name="Alice"), User(name="Bob"), User(name="Charlie")]
            created = await User.objects().bulk_create(users)

            edges = await Purchased.objects().bulk_create([
                Purchased(in_=alice, out=widget, quantity=3),
                Purchased(in_=bob, out=widget, quantity=1),
            ])  # one insert_relation() round-trip
        """
        if not models:
            return []

        if self.model.__surreal_edge__:
            return await self._bulk_create_edges(models)

        data_list = []
        for model in models:
            data = model._write_payload()
            model_id = model.get_id()
            if model_id is not None:
                data["id"] = model_id
            data_list.append(data)

        if self._tx is not None:
            rows = await self._tx.add(f"INSERT INTO {self._model_table} $data;", {"data": data_list})
            if self._tx.is_interactive and isinstance(rows, list):
                return self.model.from_db(rows)  # type: ignore
            return models  # buffered: not executed until commit; return inputs

        client = await self._manager().get_client()
        results = await client.insert(self._model_table, _encoding.encode(data_list))

        if isinstance(results, list):
            return self.model.from_db(results)  # type: ignore
        return []

    async def _bulk_create_edges(self, models: list[BaseSurrealModel]) -> list[BaseSurrealModel]:
        """The edge route of :meth:`bulk_create`: one :func:`~surreal_orm_lite.edge.insert_edges` call.

        Each row is what :meth:`BaseSurrealEdge.save` sends — the column-keyed write payload
        (``in``, ``out``, the data) plus ``id`` as a ``RecordID`` when the edge has one — and
        every id is checked before the call. ``insert_edges`` picks the statement, normalises
        the server's errors and raises when fewer rows come back than were sent.
        """
        rows: list[dict[str, Any]] = []
        for model in models:
            _check_edge_id(model)
            row = model._write_payload()
            record_id = model._record_id()
            if record_id is not None:
                row["id"] = record_id
            rows.append(row)

        created = await insert_edges(self._model_table, rows, tx=self._tx, manager=self._manager())
        if self._tx is not None and not self._tx.is_interactive:
            return models  # buffered: not executed until commit; return inputs
        return cast(list[BaseSurrealModel], self.model.from_db(created))

    @_cache.invalidates("queryset", refuse=_queryset_refusals("bulk_update()"), refuse_args=_bulk_update_arguments)
    async def bulk_update(self, **kwargs: Any) -> int:
        """
        Update all records matching the current filters.

        A subquery in the filters (v0.27.0) is read by the server in the same statement, a block
        ``{ LET $_sq0 = (…); (UPDATE …) };``: one request, and on SurrealDB 3.x a subquery table that does not exist
        reads as empty, as on 2.x (outside a transaction; inside one, the server's error is the transaction's). A
        value to write is data, never a subquery. With ``similar_to()`` (v0.36.0) it updates the ``limit`` nearest
        records among those the filters select, by their ids (:meth:`_write_where`).

        Args:
            **kwargs: Fields and values to update.

        Returns:
            The number of updated records (``0`` inside a buffered transaction, whose statements run at commit).

        The materialized view, a queryset read ``as_of()``, then a subquery value and a computed field or an edge
        endpoint among the fields are refused in this order, before any request and before the table is noted on the
        ``objects(tx=…)`` transaction or put in flight: a refused call leaves the transaction as it was and drops no
        cached read (#320).

        Raises:
            TypeError: the model is a materialized view (v0.32.0), before any request.
            SurrealDbError: the queryset reads ``as_of()``, before any request.
            ValueError: a value is a ``Subquery`` or a QuerySet (v0.27.0), before any request: filter with it, and
                pass the value to write; a field is a computed field or an edge endpoint.

        Example::

            count = await User.objects().filter(status="pending").bulk_update(status="active")
            count = await Post.objects().filter(author__in=Subquery(banned)).bulk_update(hidden=True)
        """
        if not kwargs:
            return 0

        def compile_statement(compiler: SubqueryCompiler) -> tuple[str, dict[str, Any]]:
            where_clause, where_vars = self._write_where(compiler)
            set_parts: list[str] = []
            set_vars: dict[str, Any] = {}
            for i, (field, value) in enumerate(kwargs.items()):
                validate_field_name(field, "bulk_update field")
                var_name = f"_v{i}"
                set_parts.append(f"{self._column(field)} = ${var_name}")
                set_vars[var_name] = value
            query = f"UPDATE {self._model_table} SET {', '.join(set_parts)}{where_clause};"
            return compiler.wrap(query), {**compiler.variables, **where_vars, **set_vars}

        results = await self._run_compiled(compile_statement, write=True)
        return len(results) if isinstance(results, list) else 0

    @_cache.invalidates("queryset", deletes=True, refuse=_queryset_refusals("bulk_delete()"))
    async def bulk_delete(self) -> int:
        """
        Delete all records matching the current filters.

        A subquery in the filters (v0.27.0) is read by the server in the same statement, as for
        :meth:`bulk_update`. With ``similar_to()`` (v0.36.0) it deletes the ``limit`` nearest records among those the
        filters select, by their ids (:meth:`_write_where`).

        Returns:
            The number of deleted records (``0`` inside a buffered transaction, whose statements run at commit).

        The materialized view (``TypeError``), then a queryset read ``as_of()`` (``SurrealDbError``), are refused
        before any request and before the table and the edges are noted on the ``objects(tx=…)`` transaction or put in
        flight: a refused call leaves the transaction as it was and drops no cached read (#320).

        Example::

            count = await User.objects().filter(status="inactive").bulk_delete()
            count = await Post.objects().filter(author__in=Subquery(banned)).bulk_delete()
        """

        def compile_statement(compiler: SubqueryCompiler) -> tuple[str, dict[str, Any]]:
            where_clause, where_vars = self._write_where(compiler)
            query = f"DELETE {self._model_table}{where_clause} RETURN BEFORE;"
            return compiler.wrap(query), {**compiler.variables, **where_vars}

        results = await self._run_compiled(compile_statement, write=True)
        return len(results) if isinstance(results, list) else 0

    @_cache.invalidates("queryset", refuse=_queryset_refusals("patch()"), refuse_args=_patch_arguments)
    async def patch(self, operations: list[dict[str, Any]]) -> int:
        """
        Apply a JSON Patch (RFC 6902) to every row matching the current filters.

        Whole table if unfiltered. Returns the number of affected rows. Participates in
        ``objects(tx=)`` like the other bulk operations. ``operations`` is validated then bound
        as ``$_ops`` (never string-interpolated). Emits no signals. A subquery in the filters
        (v0.27.0) is read by the server in the same statement, as for :meth:`bulk_update`. With ``similar_to()``
        (v0.36.0) it patches the ``limit`` nearest records among those the filters select, by their ids
        (:meth:`_write_where`).

        The materialized view (``TypeError``), a queryset read ``as_of()`` (``SurrealDbError``), invalid operations,
        then a computed field or an edge endpoint they write (``ValueError``) are refused in this order, before any
        request and before the table is noted on the ``objects(tx=…)`` transaction or put in flight: a refused call
        leaves the transaction as it was and drops no cached read (#320).

        Example::

            n = await User.objects().filter(status="trial").patch(
                [{"op": "replace", "path": "/plan", "value": "free"}]
            )
        """

        def compile_statement(compiler: SubqueryCompiler) -> tuple[str, dict[str, Any]]:
            where_clause, where_vars = self._write_where(compiler)
            query = f"UPDATE {self._model_table} PATCH $_ops{where_clause};"
            ops = self.model._columns_for_patch(operations)
            return compiler.wrap(query), {**compiler.variables, **where_vars, "_ops": ops}

        results = await self._run_compiled(compile_statement, write=True)
        return len(results) if isinstance(results, list) else 0

    # ==================== Upsert / get_or_create ====================

    def _criteria_payload(self, criteria: Mapping[str, Any]) -> dict[str, Any]:
        """Field=value pairs usable as a WRITE payload, taken from EQUALITY criteria only.

        ``criteria`` keys are filter lookups (``field`` or ``field__lookup``). A literal raw
        key like ``name__contains`` must never be written as a column, so only ``exact``
        lookups contribute to the payload (their base field name → value). Non-``exact``
        lookups (``gt``/``contains``/``in``/…) still drive the lookup but cannot be written,
        so they are excluded here — mirroring Django, which only writes exact lookups on
        create.

        Values go through the same interpretation the WHERE builder applies, so the row that
        gets created is the row the next lookup finds: a ``str`` is data, ``"$admin"`` included;
        the deprecated ``"$$admin"`` escape is un-escaped to ``"$admin"`` (otherwise it filtered on
        ``"$admin"`` but stored ``"$$admin"``, never converging and creating a duplicate on every
        call), without a second warning (the lookup warns). A model instance is written as its
        record id, the value its lookup binds (v0.22.0): ``get_or_create(customer=alice)`` stores
        ``customer = RecordID(...)``. A string ``"$auth"`` naming a session parameter or a variable
        the lookup binds never gets here: the caller refused it before the lookup
        (:meth:`_reject_string_references`).

        A ``Var("t")`` criterion naming a variable the lookup binds (:meth:`_lookup_variables`: this
        queryset's ``variables()`` and those of its subquery criteria) is written as that variable's value,
        through :func:`~surreal_orm_lite.utils.as_record_value` — data, never un-escaped (issue #252): the lookup
        compared the column with that value, so the next identical call finds the record.
        ``get_or_create(title=Var("t"))`` with ``variables(t="mine")`` writes ``title = "mine"``. Any other
        ``Var`` — a session parameter (``Var("auth")``), a name no ``variables()`` gives — is refused: its value
        is only known to the database, so it cannot be written back (issue #156 follow-up).

        Raises:
            SurrealDbError: an ``exact`` ``Var`` criterion naming no variable the lookup binds.
            ValueError: an unsaved model instance; and what compiling the criteria raises (:meth:`_lookup_variables`).
        """
        payload: dict[str, Any] = {}
        variables: dict[str, Any] | None = None
        for key, value in criteria.items():
            field_name, lookup = parse_lookup(key)
            if lookup != "exact":
                continue
            if isinstance(value, Var):
                if variables is None:
                    variables = self._lookup_variables(criteria)
                if value.name not in variables:
                    raise SurrealDbError(_unwritable_variable(field_name, value.name))
                payload[field_name] = as_record_value(variables[value.name])
                continue
            payload[field_name] = as_record_value(unescape_filter_value(value, warn=False))
        return payload

    @staticmethod
    def _reject_subquery_writes(criteria: Mapping[str, Any], defaults: Mapping[str, Any] | None, method: str) -> None:
        """Refuse a subquery that ``get_or_create()``/``update_or_create()`` (*method*) would have to write (v0.27.0).

        A subquery is a filter value whose values only the database knows: under an ``exact`` criterion, which
        is written on create, or in ``defaults``, which is data. Another lookup (``author__in=Subquery(…)``)
        drives the lookup only and is never written. Checked before any request.

        Raises:
            SurrealDbError: an ``exact`` criterion whose value is a ``Subquery`` or a QuerySet, then such a
                ``defaults`` value.
        """
        for key, value in criteria.items():
            field_name, lookup = parse_lookup(key)
            if lookup == "exact" and _subquery.subquery_of(value) is not None:
                raise SurrealDbError(_subquery.criterion_written(field_name))
        for key, value in (defaults or {}).items():
            if _subquery.subquery_of(value) is not None:
                raise SurrealDbError(_subquery.default_written(method, key))

    def _reject_string_references(self, criteria: Mapping[str, Any]) -> None:
        """Refuse a criterion ``"$<name>"`` naming a variable or a session parameter, whatever its lookup (v0.28.1).

        A ``str`` is data: ``get_or_create()`` and ``update_or_create()`` look a criterion up as the string, and write
        an ``exact`` one on create, so ``update_or_create(owner="$auth", …)`` would find and write the record whose
        owner is the string ``"$auth"``, the same record for every record user, and ``members__contains="$auth"``
        would look for the string. A criterion whose read would warn
        (:func:`~surreal_orm_lite.utils.warn_on_variable_names`) — a scalar ``str`` that is ``$`` and the name of a
        variable the lookup binds (:meth:`_lookup_variables`: this queryset's ``variables()`` and, since issue #252,
        those of its subquery criteria, which its read counted already), or of one of SurrealDB's session
        parameters — is refused instead, before any request. For a variable, the message names ``Var(...)`` as the
        criterion to pass: the lookup reads the variable, and the create writes its value. For a session parameter
        it says to pass the actual value under ``exact`` (``Var("auth")`` is looked up but cannot be written), and
        that ``Var("auth")`` works under any other lookup, which is only looked up. ``like``/``ilike`` take no
        ``Var`` (their pattern is translated to a regular expression before it is sent): under them, for a variable
        and a session parameter alike, the message says neither, but to pass the actual pattern as the criterion,
        or to use ``regex`` with a ``Var`` holding a regular expression. On the ``id`` column the string is the
        record id ``table:⟨$name⟩``, which the message names. Any other ``"$name"`` is data, looked up and written as
        it is, and so is an item of a list lookup. A criterion the lookup refuses with a ``ValueError`` (an edge
        endpoint, which holds a record id; a text lookup on ``id``) raises that error here, as the lookup would: on
        an edge endpoint its message names the same ``Var(...)``.

        Raises:
            SurrealDbError: the first such criterion.
            ValueError: what compiling the criteria raises (:meth:`_lookup_variables`).
        """
        candidates = [
            (key, value, match.group(1))
            for key, value in criteria.items()
            if isinstance(value, str) and (match := VARIABLE_NAME_VALUE.fullmatch(value)) is not None
        ]
        if not candidates:
            return
        variables = self._lookup_variables(criteria)
        for key, value, name in candidates:
            if name in variables:
                source, variable = f"the variable {name} given to variables()", True
            elif name in SESSION_PARAMETERS:
                source, variable = f"SurrealDB's ${name} parameter", False
            else:
                continue
            field_name, lookup = parse_lookup(key)
            written = lookup == "exact"
            how = "looked up and written" if written else "looked up"
            if self._column(field_name) == RECORD_ID_COLUMN:
                what = f"the record id {coerce_record_id(value, self._model_table)}"
            else:
                what = f"the string '${name}'"
            if lookup in ("like", "ilike"):
                advice = (
                    f"'{lookup}' cannot take Var('{name}'): its pattern is translated to a regular expression before "
                    "it is sent. Pass the actual pattern as the criterion, or use 'regex' with a Var holding a regular "
                    "expression."
                )
            elif variable:
                advice = f"To read the variable, pass Var('{name}') as the criterion" + (
                    ": the create writes the variable's value." if written else "."
                )
            elif written:
                advice = (
                    f"Pass the actual value as the criterion (the record id, or the value itself); Var('{name}') reads "
                    "the parameter in the lookup but cannot be written."
                )
            else:
                advice = (
                    f"To read the parameter, pass Var('{name}') as the criterion: {key}=Var('{name}') is looked up, "
                    "never written."
                )
            raise SurrealDbError(
                f"Cannot look up or create a record from the string '${name}' given for '{key}': it names {source}, "
                f"but a string criterion is data, {how} as {what}. {advice}"
            )

    def _lookup_probe(self, criteria: Mapping[str, Any]) -> "QuerySet":
        """The queryset the lookup of ``get_or_create()``/``update_or_create()`` reads with: *criteria* as its
        filters, a copy of this queryset's ``variables()`` (issue #252) and its transaction (``objects(tx=)``); no
        ``cache()``, no ``as_of()``."""
        probe = QuerySet(self.model)
        probe.filter(**criteria)
        probe._variables = dict(self._variables)
        probe._tx = self._tx
        return probe

    def _lookup_variables(self, criteria: Mapping[str, Any]) -> dict[str, Any]:
        """The variables the lookup of *criteria* binds (issue #252), by name.

        This queryset's ``variables()``, then those of each subquery criterion (nested ones included), merged as
        in ``filter()``: the statement :meth:`_lookup_matches` sends binds them all. Compiled, never sent, without
        a warning.

        Raises:
            ValueError, SurrealDbError: what the lookup raises compiling *criteria*, before any request — a
                subquery binding one of these names to another value, an edge endpoint that is no record id…
        """
        probe = self._lookup_probe(criteria)
        compiler = probe._subquery_compiler(warn=False)
        probe._where_parts(subqueries=compiler)
        return compiler.variables

    def _lookup_criteria(self, criteria: Mapping[str, Any]) -> dict[str, Any]:
        """*criteria* as the lookup compares them (issue #252).

        An ``exact`` ``Var("k")`` criterion on the record id column or on an edge endpoint (``in``/``out``), naming
        a variable the lookup binds, is compared with the record id that variable's value names — read as the same
        criterion given that value is (:func:`~surreal_orm_lite.utils.coerce_record_id`,
        :func:`~surreal_orm_lite.utils.coerce_endpoint`): ``id=Var("k")`` with ``variables(k="r1")`` compares
        ``id`` with ``<table>:r1``, the record the create writes. Compared with the variable as it is, a string never
        equals a record id: the lookup would never find the record it created. Every other criterion is unchanged,
        a ``Var`` reading its variable as ``filter()`` does.

        Raises:
            ValueError: the variable's value names no record id there (``in_=Var("a")`` with ``variables(a="x")``,
                a ``bool`` for ``id``), before any request; and what :meth:`_lookup_variables` raises.
        """
        endpoint_columns = self.model._endpoint_columns()
        looked_up = dict(criteria)
        variables: dict[str, Any] | None = None
        for key, value in criteria.items():
            field_name, lookup = parse_lookup(key)
            if lookup != "exact" or not isinstance(value, Var):
                continue
            column = self._column(field_name)
            if column != RECORD_ID_COLUMN and column not in endpoint_columns:
                continue
            if variables is None:
                variables = self._lookup_variables(criteria)
            if value.name not in variables:
                continue
            try:
                record = as_record_value(variables[value.name])
                if column == RECORD_ID_COLUMN:
                    looked_up[key] = coerce_record_id(record, self._model_table)
                else:
                    looked_up[key] = coerce_endpoint(record)
            except ValueError as exc:
                raise ValueError(
                    f"Var('{value.name}') given for '{key}' is compared as the record id its value names: {exc}"
                ) from exc
        return looked_up

    async def _lookup_matches(self, criteria: dict[str, Any]) -> list[Any]:
        """Return rows matching ``criteria``, routed through the transaction when attached.

        Reuses the parameterized WHERE builder (anti-injection) via a throwaway QuerySet's
        ``filter(**criteria)`` (:meth:`_lookup_probe`), joined to ``self._tx`` (``objects(tx=)``) so the read
        participates in it — seeing the tx's uncommitted writes on an interactive transaction, and raising on a
        buffered one (consistent with every other read). ``LIMIT 2`` is enough to distinguish
        0 / 1 / >1. A subquery criterion (v0.27.0) is read in the same statement, a block
        ``{ LET $_sq0 = (…); (SELECT … LIMIT 2) };``, run as every read is (:meth:`_run_compiled`), never cached.

        The lookup binds this queryset's ``variables()`` (issue #252; until then it bound none, and a ``Var``
        criterion read ``NONE``), merged with its subquery criteria's as ``filter()`` merges them (``ValueError`` when
        a subquery binds a name to another value), so ``variables(n=10).get_or_create(size__gte=Var("n"))`` compares
        ``size`` with 10. An ``exact`` ``Var`` criterion on the record id or an edge endpoint is compared with the
        record id its value names (:meth:`_lookup_criteria`). A criterion ``"$name"`` that would warn never gets
        here (:meth:`_reject_string_references`).

        Each variable is bound as the value the create writes (:func:`~surreal_orm_lite.utils.as_record_value`): a
        model instance as its record id, whether a criterion reads it or not — bound as it is, the SDK has no
        encoder for it. The name clash check compares the values given to ``variables()``, before that conversion.

        Raises:
            ValueError: an unsaved model instance in the variables (it has no record id), before any request.
        """
        probe = self._lookup_probe(self._lookup_criteria(criteria))
        table = self._model_table

        def compile_statement(compiler: SubqueryCompiler) -> tuple[str, dict[str, Any]]:
            where_clause, where_vars = probe._build_where(subqueries=compiler)
            query = f"SELECT * FROM {table}{where_clause} LIMIT 2;"
            bound = {name: as_record_value(value) for name, value in compiler.variables.items()}
            return compiler.wrap(query), {**bound, **where_vars}

        rows = await probe._run_compiled(compile_statement)
        return rows if isinstance(rows, list) else []

    def _writable_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Vet a ``*_or_create`` write payload: drop computed fields, reject undeclared ones.

        ``server_fields`` entries are kept. Naming one — as a criterion or in ``defaults`` — is
        an explicit write, which ``SurrealConfigDict`` promises stays possible: the update
        branch passes it to ``merge()`` and the create branch to :meth:`_save_naming`, so both
        store it and the next identical lookup finds the row.

        Filtering by a computed field is legitimate (it is an ordinary column to read), so a
        computed name can legitimately appear in ``criteria``. But it is server-owned and must
        not be *written*. Without this, the same call would succeed on the create path (where
        ``_write_payload`` silently drops it) and raise on the update path (where ``merge()``
        guards) — an outcome that depends only on whether the row already existed.

        A key the model does not declare used to diverge the same way, in the opposite
        direction (issue #156): Pydantic dropped it when constructing the model on the create
        path, while ``merge()`` wrote it straight to the row on the update path — so one call
        produced two different stored schemas depending on whether the record happened to
        exist. Both paths now refuse it up front, because a caller who mistypes a field name
        wants to hear about it rather than lose the value. Models that opt into extra fields
        (``model_config = ConfigDict(extra="allow")``) keep them, on both paths.
        """
        computed = self.model.get_computed_fields()
        vetted = {key: value for key, value in payload.items() if key not in computed}
        self.model._reject_undeclared_writes(vetted)
        return vetted

    async def update_or_create(
        self,
        defaults: dict[str, Any] | None = None,
        **criteria: Any,
    ) -> tuple[Any, bool]:
        """Look up a record by ``criteria``; create it or update it; return ``(obj, created)``.

        Django-style: ``criteria`` are filters used to find the record; ``defaults`` are extra
        field values (they win on conflict). The write payload is built from the EQUALITY
        criteria fields plus ``defaults`` (non-``exact`` lookups drive the lookup only).

        - 0 matches → CREATE via ``save()`` → ``created=True``.
        - 1 match → partial UPDATE via ``merge()`` (untouched fields preserved) → ``created=False``.
        - >1 matches → ``SurrealDbError`` (the criteria are not unique).

        Writes go through ``save()``/``merge()``, so lifecycle signals fire, SDK errors are
        normalised to ``SurrealDbError``, the primary key anchors identity, and — under
        ``objects(tx=)`` — the operation participates in the transaction. The lookup and the
        write are two round-trips (no server-side locking on SurrealDB 2.6.x), so a small race
        window exists, the same non-atomic fallback Django documents. ``criteria`` must be
        non-empty.

        Create builds ``self.model(**payload)``: a key the model does not declare raises
        ``SurrealDbError`` (unless the model sets ``extra="allow"``), and a missing required
        field raises ``ValidationError`` at construction.

        On an edge model (v0.22.0) the endpoints are lookup criteria, never data:
        ``update_or_create(in_=a, out=b, defaults={"weight": 3})`` finds or relates ``a -> b``
        and writes ``weight``. ``defaults`` naming an endpoint (``in_``/``in``/``out``) raises
        ``ValueError`` before the lookup — it would be written on create and silently ignored by
        the server on update. On update, the endpoints the criteria named (they matched) are left
        out of the ``merge()``.

        A subquery criterion (v0.27.0) drives the lookup, read by the server in the same request:
        ``update_or_create(author__in=Subquery(active), title="one", defaults={"score": 6})``. It is
        never written; one the create would have to write — under an ``exact`` lookup, or in
        ``defaults`` — raises ``SurrealDbError`` before any request.

        The lookup binds this queryset's ``variables()``, as ``filter()`` does (issue #252; until then a ``Var``
        criterion read ``NONE`` there, and a comparison could match, then update, a record the caller did not
        mean): ``objects().variables(n=10).update_or_create(size__gte=Var("n"), title="mine", defaults=…)``
        compares ``size`` with 10. A subquery criterion's ``variables()`` share the statement: one binding a name of
        this queryset's to another value raises ``ValueError`` before any request. An ``exact`` ``Var``
        criterion naming a variable the lookup binds is written as that variable's value on create
        (``title=Var("t")`` with ``variables(t="mine")`` writes ``title = "mine"``), so the next identical call
        finds the record; on the record id and an edge's ``in``/``out`` it is compared as the record id its value
        names, as the same criterion given that value is. A model instance in ``variables()`` is bound and written as
        its record id (an unsaved one raises ``ValueError`` before any request). A ``Var`` naming a session
        parameter (``Var("auth")``) or a name no ``variables()`` gives is looked up but cannot be written:
        ``SurrealDbError`` before any write.

        A ``str`` criterion is data, looked up and written as it is (``owner="$admin"``). A scalar criterion, whatever
        its lookup, that is ``$`` and the name of a variable the lookup binds or of one of SurrealDB's session
        parameters — ``update_or_create(owner="$auth", …)`` — raises ``SurrealDbError`` before any request
        (v0.28.1): it would find and write the record whose owner is the string ``"$auth"``, the same for every
        record user. For a variable, the message names ``Var(...)`` as the criterion to pass, under any lookup but
        ``like``/``ilike``, which take no ``Var`` (pass the pattern itself, or use ``regex``); for a session
        parameter, it says to pass the actual value (the record id, or the value itself) under ``exact`` and that
        ``Var("auth")`` works under any other lookup but ``like``/``ilike``, which take no ``Var`` (pass the pattern
        itself, or use ``regex``): ``members__contains=Var("auth")``. On an edge endpoint such a string raises the
        lookup's ``ValueError``.
        """
        self.model._reject_view_write("objects().update_or_create()")
        self._reject_time_travel("update_or_create()")
        self._reject_subquery_writes(criteria, defaults, "update_or_create")
        self._reject_string_references(criteria)
        if not criteria:
            raise SurrealDbError("update_or_create() requires at least one lookup criteria.")
        defaults = defaults or {}
        self.model._reject_endpoint_writes(defaults, "update_or_create(defaults=)")
        matches = await self._lookup_matches(criteria)
        if len(matches) > 1:
            raise SurrealDbError("update_or_create() matched multiple records; the lookup criteria are not unique.")
        payload = self._writable_payload({**self._criteria_payload(criteria), **defaults})
        if not matches:
            obj: Any = self.model(**payload)
            await obj._save_naming(payload, tx=self._tx)
            return obj, True
        obj = self.model.from_db(matches[0])
        # Only the criteria can name an endpoint here (defaults were checked above).
        changes = {key: value for key, value in payload.items() if not self.model._is_write_protected(key)}
        await obj.merge(tx=self._tx, **changes)
        return obj, False

    async def get_or_create(
        self,
        defaults: dict[str, Any] | None = None,
        **criteria: Any,
    ) -> tuple[Any, bool]:
        """Look up a record by ``criteria``; return it, or create it; return ``(obj, created)``.

        Django-style: ``criteria`` are filters. Unlike ``update_or_create``, ``defaults`` are
        applied ONLY when creating; an existing match is returned untouched.

        - 0 matches → CREATE (equality criteria + ``defaults``) via ``save()`` → ``created=True``.
        - 1 match → returned as-is (no write) → ``created=False``.
        - >1 matches → ``SurrealDbError`` (the criteria are not unique).

        The create goes through ``save()`` (signals, error normalisation, PK identity, and
        ``objects(tx=)`` participation); the lookup routes through the transaction too.
        ``criteria`` must be non-empty.

        Create builds ``self.model(**payload)``: a key the model does not declare raises
        ``SurrealDbError`` (unless the model sets ``extra="allow"``), and a missing required
        field raises ``ValidationError`` at construction.

        A subquery criterion (v0.27.0) drives the lookup, read by the server in the same request:
        ``get_or_create(author__in=Subquery(active), title="one")``. It is never written; one the
        create would have to write — under an ``exact`` lookup, or in ``defaults`` — raises
        ``SurrealDbError`` before any request.

        The lookup binds this queryset's ``variables()``, as ``filter()`` does (issue #252; until then a ``Var``
        criterion read ``NONE`` there): ``objects().variables(n=10).get_or_create(size__gte=Var("n"), …)`` compares
        ``size`` with 10. A subquery criterion's ``variables()`` share the statement: one binding a name of this
        queryset's to another value raises ``ValueError`` before any request. An ``exact`` ``Var`` criterion
        naming a variable the lookup binds is written as that variable's value on create:
        ``objects().variables(t="mine").get_or_create(title=Var("t"))`` behaves as ``get_or_create(title="mine")``,
        and the next identical call returns ``created=False``. On the record id and an edge's ``in``/``out`` it is
        compared as the record id its value names, as the same criterion given that value is:
        ``variables(a="person:alice").get_or_create(in_=Var("a"), out=bob)`` finds or relates ``person:alice``. A
        model instance in ``variables()`` is bound and written as its record id (``variables(a=alice)``); an unsaved
        one raises ``ValueError`` before any request.

        A ``str`` criterion is data, looked up and written as it is (``owner="$admin"``). A scalar criterion, whatever
        its lookup, that is ``$`` and the name of a variable the lookup binds or of one of SurrealDB's session
        parameters — ``get_or_create(owner="$auth")``, ``get_or_create(members__contains="$auth", …)`` — raises
        ``SurrealDbError`` before any request (v0.28.1). For a variable, the message names ``Var(...)`` as the
        criterion to pass, under any lookup but ``like``/``ilike``, which take no ``Var`` (pass the pattern itself,
        or use ``regex``); for a session parameter, it says to pass the actual value (the record id, or the value
        itself) under ``exact`` and that ``Var("auth")`` works under any other lookup but ``like``/``ilike``, which
        take no ``Var`` (pass the pattern itself, or use ``regex``).
        ``get_or_create(owner=Var("auth"))`` returns the signed-in record user's record; when there is none it raises
        ``SurrealDbError``: a session parameter's value is only known to the database and cannot be written, nor can
        a name no ``variables()`` gives. On an edge endpoint such a string raises the lookup's ``ValueError``.
        """
        self.model._reject_view_write("objects().get_or_create()")
        self._reject_time_travel("get_or_create()")
        self._reject_subquery_writes(criteria, defaults, "get_or_create")
        self._reject_string_references(criteria)
        if not criteria:
            raise SurrealDbError("get_or_create() requires at least one lookup criteria.")
        defaults = defaults or {}
        matches = await self._lookup_matches(criteria)
        if len(matches) > 1:
            raise SurrealDbError("get_or_create() matched multiple records; the lookup criteria are not unique.")
        if matches:
            return self.model.from_db(matches[0]), False
        payload = self._writable_payload({**self._criteria_payload(criteria), **defaults})
        obj = self.model(**payload)
        await obj._save_naming(payload, tx=self._tx)
        return obj, True

    # ==================== Custom Query ====================

    # ------------------------------------------------------------------
    # Live queries (v0.19.0 raw, v0.20.0 filtered + typed)
    # ------------------------------------------------------------------

    def _reject_live_clauses(self) -> None:
        """Refuse a clause a live query cannot honour, rather than silently dropping it.

        ``LIVE SELECT`` has no ``ORDER BY``, ``LIMIT`` or ``START`` (both server lines reject
        them at parse time), a typed stream needs whole records, and a live query is a
        connection-level subscription with no transaction to join. Each offender is named so
        the caller fixes them all at once.

        ``search()`` (v0.35.0) is refused first, on its own: SurrealDB accepts a live query whose condition holds a
        full-text match and never notifies it; the refusal names the subquery form, which notifies. ``similar_to()``
        (v0.36.0) next, on its own too: SurrealDB accepts a live query whose condition holds a KNN operator and never
        notifies it.
        """
        if self._search:
            raise SurrealDbError(_search.live_refused())
        if self._similar is not None:
            raise SurrealDbError(_vector.live_refused())
        unsupported = {
            "select()": bool(self.select_item),
            "limit()": self._limit is not None,
            "offset()": self._offset is not None,
            "order_by()": self._order_by is not None,
            "annotate()": bool(self._annotations),
            # values() is the only public setter of _group_by_fields.
            "values()": bool(self._group_by_fields),
            "objects(tx=)": self._tx is not None,
            # A live query streams changes: nothing to cache (v0.28.0).
            "cache()": self._cache is not None,
        }
        offenders = sorted(name for name, present in unsupported.items() if present)
        if offenders:
            raise SurrealDbError(
                f"A live query cannot honour {', '.join(offenders)}: SurrealDB's LIVE SELECT has "
                f"no ORDER BY, LIMIT or START, streams whole records, and runs outside any "
                f"transaction. Drop the clause — filter(), variables() and fetch() are supported."
            )

    def _compile_live(self, diff: bool, subqueries: SubqueryCompiler | None = None) -> str:
        """Compile this queryset into a ``LIVE SELECT`` with every bound value inlined.

        The WHERE is the ordinary parameterised one from :meth:`_build_where` — identifiers,
        operators and ``$name`` references only — and the values are then written in as
        literals, because SurrealDB 2.x evaluates a live query's parameters as ``NONE`` at
        notification time and the filter would silently match nothing (see ``surql_literal``).

        A subquery (v0.27.0) stays an inline sub-SELECT, its values and ``variables()`` inlined too: a
        ``LIVE SELECT`` cannot sit in a block, and the server evaluates the sub-SELECT on every notification.
        *subqueries* is the inline compiler to use (a new one by default), which records the tables they read.

        :raises SurrealDbError: for a clause a live query cannot honour; for a subquery or a link of another
            connection (v0.29.0).
        :raises TypeError: for a filter value with no SurrealQL literal form.
        :raises ValueError, SurrealDbError: for a subquery the ORM cannot compile (README section 28).
        """
        self._reject_live_clauses()
        compiler = subqueries if subqueries is not None else self._subquery_compiler(inline=True)
        where_clause, where_vars = self._build_where(subqueries=compiler)
        self._check_links()
        # Only the WHERE fragment is rewritten: it is the one place references live, and the
        # table name and FETCH list never pass through the inliner.
        where_clause = inline_variables(where_clause, {**compiler.variables, **where_vars})
        projection = "DIFF" if diff else "*"
        query = f"LIVE SELECT {projection} FROM {self._model_table}{where_clause}"
        if self._fetch_fields:
            query += f" FETCH {', '.join(self._fetch_fields)}"
        return query + ";"

    def _live_starter(self, diff: bool) -> Callable[..., Awaitable[UUID]]:
        """Compile now, and return the coroutine function that starts the live query.

        Compiling eagerly makes a bad clause or value fail at the ``live()``/``watch()`` call,
        before any network traffic, and snapshots the query: changing the queryset afterwards
        does not change a stream already built from it.

        With subqueries (v0.27.0) the coroutine first reads every table they read, in one request
        (``RETURN [(SELECT VALUE id FROM <table> LIMIT 1), …];``, the tables sorted), then sends the
        ``LIVE SELECT``. SurrealDB 3.x accepts a live query whose subquery reads a table that does
        not exist and then never notifies, but refuses that check: the ORM raises instead. 2.x
        answers it (the table reads as empty) and the live query starts. A resubscribe runs the
        check again, on the new connection.

        The same request checks (v0.35.0) that each column a subquery searches has a full-text index — one empty
        search per column, ``(SELECT VALUE id FROM <table> WHERE <column> @0@ '' LIMIT 1)``, after the tables, the
        pairs sorted — and raises ``SurrealDbError`` naming the field otherwise, before the live query starts:
        without the index, SurrealDB 3.x would accept the live query and never notify.

        The coroutine runs on the client it is given (v0.29.0): the stream passes its connection's — at start, the
        client it opened; at a resubscribe, the one it is about to move onto — so the live query runs on that very
        connection, and the stream has already checked the transport.
        """
        compiler = self._subquery_compiler(inline=True)
        query = self._compile_live(diff, compiler)
        table = self._model_table
        items = [f"(SELECT VALUE id FROM {name} LIMIT 1)" for name in sorted(compiler.tables)]
        items += [_search.live_index_probe(name, column) for name, column in sorted(compiler.searched)]
        tables = tuple(sorted(compiler.tables))
        fields = {column: field for (_, column), field in compiler.searched.items()}
        check = "RETURN [" + ", ".join(items) + "];"

        async def start(client: Any) -> UUID:
            if items:
                try:
                    await client.query(check)
                except ServerError as exc:
                    refusal = _search.missing_index_refusal(exc, fields)
                    if refusal is not None:
                        raise refusal from None
                    missing = _subquery.missing_table(exc)
                    if missing is None or missing not in tables:
                        raise
                    raise SurrealDbNotFoundError(_subquery.live_table_missing(missing, exc)) from exc
            try:
                live_id = await client.query(query)
            except NotFoundError as exc:
                raise missing_table_error(exc, table) from exc
            return live_id if isinstance(live_id, UUID) else UUID(str(live_id))

        return start

    def live(
        self,
        *,
        auto_resubscribe: bool = True,
        diff: bool = False,
        on_reconnect: ReconnectCallback | None = None,
    ) -> LiveModelStream[Any]:
        """Subscribe to the records this queryset matches, as typed model events.

        Same API as the full SurrealDB-ORM::

            async with User.objects().filter(role="admin").live() as stream:
                async for event in stream:
                    match event.action:
                        case LiveAction.CREATE: print("new", event.instance.name)
                        case LiveAction.UPDATE: print("updated", event.instance)
                        case LiveAction.DELETE: print("removed", event.record_id)

        ``filter()``, ``nearby()``, ``variables()`` and ``fetch()`` are applied by the server,
        identically on SurrealDB 2.x and 3.x (values are inlined — see ``surql_literal``). With
        ``diff=True`` the server sends patches: ``changed_fields`` lists what changed, and only ``CREATE``
        carries a whole record. The live query is killed when the ``async with`` exits.

        **Subqueries (v0.27.0).** A subquery in the filter (``author__in=Subquery(active)``) stays an
        inline sub-SELECT, which the server evaluates on every change: a change of the inner table
        changes what notifies from then on (an ordered and limited subquery included). Before the
        ``LIVE SELECT`` the stream checks, in one request, that every table the subqueries read
        exists — again on each resubscribe — because SurrealDB 3.x would accept the live query on a
        missing one and never notify; 2.x reads a missing table as empty, as everywhere else.

        **Full-text search (v0.35.0).** ``search()`` is refused: SurrealDB accepts a live query whose condition holds
        a full-text match and never notifies it. Search in a subquery instead —
        ``filter(id__in=Subquery(Model.objects().search(title="physics")))`` —, which notifies the records created
        or updated to match, not their deletion. Before such a live query starts, and again on each resubscribe, the
        ORM checks in the same request that each field a subquery searches has a full-text index, and raises
        ``SurrealDbError`` naming the field otherwise: without the index, SurrealDB 3.x would accept the live query
        and never notify. Do not remove that index while the live query runs.

        **Vector search (v0.36.0).** ``similar_to()`` is refused: SurrealDB accepts a live query whose condition holds
        a KNN operator and never notifies it. So is a subquery holding ``similar_to()``: the nearest records change
        with every write. Read them with ``similar_to()`` and watch them by id.

        ``await qs.live()`` — the v0.19.0 form returning the uuid — still works and is
        deprecated, and never resubscribes: the caller owns that uuid.

        **Dropped connections (v0.21.0).** With ``auto_resubscribe=True`` (the default, as in the
        full ORM) a stream whose WebSocket drops resubscribes on a new connection, with backoff,
        and the ``async for`` carries on; ``on_reconnect(old_id, new_id)`` — sync or async — is
        then called, the place to catch up on what changed during the outage, which a live query
        cannot replay. With ``auto_resubscribe=False`` the iteration raises
        ``SurrealDbConnectionError`` instead. A teardown you ask for (``kill()``,
        ``close_connection()``…) ends the stream either way.

        :raises SurrealDbError: for ``select``/``values``/``annotate``/``order_by``/``limit``/
            ``offset``/``objects(tx=)``/``cache()`` (v0.28.0: a stream is never cached), for ``search()``
            (v0.35.0), for ``similar_to()`` and a subquery holding it (v0.36.0), and on a queryset read ``as_of()``
            (a live query streams the current changes, v0.26.0), here; for a non-WebSocket connection (the embedded engine included: read its change feed with
            :meth:`changes`), or a field a subquery searches without a full-text index (v0.35.0), on start.
        :raises TypeError: for a filter value with no SurrealQL literal form, here.
        :raises ValueError, SurrealDbError: for a subquery the ORM cannot compile, here.
        :raises SurrealDbNotFoundError: on SurrealDB 3.x, on start, if the table does not exist, or a
            table a subquery reads.
        """
        self._reject_time_travel("live()")
        return LiveModelStream(
            self.model,
            self._model_table,
            self._live_starter(diff),
            diff=diff,
            auto_resubscribe=auto_resubscribe,
            on_reconnect=on_reconnect,
            connection=self.model._connection_manager,
        )

    def watch(
        self,
        *,
        diff: bool = False,
        auto_resubscribe: bool = True,
        on_reconnect: ReconnectCallback | None = None,
    ) -> LiveStream:
        """Watch the records this queryset matches, killing the subscription when the block exits.

        Yields the **raw** notification envelopes (``action``, ``record``, ``result``)::

            async with User.objects().filter(status="active").watch() as stream:
                async for notif in stream:
                    print(notif["action"], notif["result"])

        ``filter()``, ``nearby()``, ``variables()`` and ``fetch()`` are applied by the server. With
        ``diff=True`` each ``result`` is a list of patches instead of a record. For model
        instances, use :meth:`live`. ``auto_resubscribe`` and ``on_reconnect`` behave as for
        :meth:`live`, and so do its refusals (``as_of()``, ``cache()``, ``search()`` and ``similar_to()`` included,
        here) and its subqueries (inline, the tables they read checked on start: ``SurrealDbNotFoundError`` on
        SurrealDB 3.x for a missing one; the fields they search checked for a full-text index: ``SurrealDbError``
        naming one without).
        """
        self._reject_time_travel("watch()")
        return LiveStream(
            self._model_table,
            self._live_starter(diff),
            auto_resubscribe=auto_resubscribe,
            on_reconnect=on_reconnect,
            connection=self.model._connection_manager,
        )

    def _reject_changes_clauses(self) -> None:
        """Refuse every clause, naming each: ``SHOW CHANGES`` reads a whole table's feed.

        It has no ``WHERE``, projection, ordering or paging, so a filtered queryset would
        silently stream every record of the table — the wrong answer the ORM never gives.
        """
        unsupported = {
            "filter()": bool(self._filters or self._q_filters),
            "nearby()": bool(self._nearby),
            "search()": bool(self._search),
            "similar_to()": self._similar is not None,
            "variables()": bool(self._variables),
            "select()": bool(self.select_item),
            "limit()": self._limit is not None,
            "offset()": self._offset is not None,
            "order_by()": self._order_by is not None,
            "annotate()": bool(self._annotations),
            "values()": bool(self._group_by_fields),
            "fetch()": bool(self._fetch_fields),
            "objects(tx=)": self._tx is not None,
            # A change feed streams changes: nothing to cache (v0.28.0).
            "cache()": self._cache is not None,
        }
        offenders = sorted(name for name, present in unsupported.items() if present)
        if offenders:
            raise SurrealDbError(
                f"A change feed cannot honour {', '.join(offenders)}: SurrealDB's SHOW CHANGES "
                f"reads the whole table's log, with no WHERE, projection, ordering or paging, "
                f"outside any transaction. Call changes() on Model.objects() and filter the events."
            )

    def changes(
        self,
        *,
        since: Since = None,
        poll_interval: float = 0.1,
        batch_size: int = 100,
    ) -> ChangeModelStream[Any]:
        """Stream the table's change feed — the full SurrealDB-ORM's API, with a usable cursor::

            stream = Order.objects().changes(since=saved_cursor)   # None = from now
            async for event in stream:
                await publish(event.action, event.instance)
                save(stream.cursor)

        The table must be defined with a change feed (``DEFINE TABLE … CHANGEFEED 7d``). Unlike a
        live query, nothing is missed while nobody reads: resuming from ``stream.cursor`` delivers
        every later change, on SurrealDB 2.x and 3.x alike (their cursor units differ; the ORM
        normalises them). Works over HTTP as well as WebSocket.

        :param since: a cursor from :attr:`ChangeModelStream.cursor`; a ``datetime`` or ISO-8601
            string (naive means UTC; exact on 3.x, at-least-once on 2.x, whose server also returns
            some earlier changes; refused on the embedded engine of the surrealdb SDK 2.x, which
            keeps no timestamps for its feed); or ``None`` for changes made after the stream starts.
        :param poll_interval: seconds between two polls when the feed has nothing new.
        :param batch_size: entries read per poll.
        :raises SurrealDbError: for any queryset clause (``as_of()``, ``cache()``, ``search()`` and ``similar_to()``
            included), here; on start, for a table that does not exist or has no change feed (on the table or on its
            database), or for a datetime ``since`` on the embedded engine of the surrealdb SDK 2.x.
        :raises TypeError, ValueError: for an unusable ``since``/``poll_interval``/``batch_size``.
        """
        self._reject_time_travel("changes()")
        self._reject_changes_clauses()
        return ChangeModelStream(
            self.model,
            self._model_table,
            since=since,
            poll_interval=poll_interval,
            batch_size=batch_size,
        )

    async def query(self, query: str, variables: dict[str, Any] | None = None) -> Any:
        """
        Execute a custom SQL query on the SurrealDB database.

        Args:
            query: The custom SQL query string to execute.
            variables: A dictionary of variables to substitute into the query.

        Returns:
            The result of the query, typically model instances.

        Raises:
            SurrealDbError: the query does not read ``FROM`` the model's table; the queryset is read
                ``as_of()`` (v0.26.0): the custom statement would ignore the moment, so it is refused rather
                than run on the current data; the queryset has ``cache()`` (v0.28.0): your own SurrealQL is
                never cached — cache its result yourself with ``QueryCache.make_key()``, ``get()`` and ``set()``.
        """
        self._reject_time_travel("query()")
        if self._cache is not None:
            raise SurrealDbError(_CACHE_QUERY)
        if f"FROM {self._model_table}" not in query:
            raise SurrealDbError(f"The query must include 'FROM {self._model_table}' to reference the correct table.")
        # Same silent truncation raw_query() warns about: only the first statement's results
        # come back from the SDK (issue #156).
        warn_on_multiple_statements(query)
        client = await self._manager().get_client()
        try:
            # User-authored SurrealQL is sent verbatim: the caller owns that string and
            # binds their own variables, so rewriting quoted "$word" occurrences inside it
            # would corrupt their string literals (issue #156).
            results = await client.query(query, _encoding.encode(variables or {}))
        except NotFoundError:
            # SurrealDB 3.x raises for a never-created table; the ORM contract
            # treats a missing table as empty.
            return self.model.from_db([])
        if isinstance(results, list):
            return self.model.from_db(results)
        data = cast(dict, results[0])
        return self.model.from_db(data.get("result", []))


#: The annotations computed per record: ``GeoDistance`` (v0.24.0) and the search annotations (v0.35.0).
_PER_RECORD = (GeoDistance, SearchScore, SearchHighlight, SearchOffsets)


def _per_record_refused(annotation: object) -> str:
    """The refusal of *annotation*, computed per record, combined with ``values()`` or an ``Aggregation``."""
    return (
        f"annotate(): a {type(annotation).__name__} is computed per record; it cannot be combined with GROUP BY "
        "aggregations or values()"
    )


def _metres(value: Any) -> float:
    """``nearby()``'s ``max_distance`` as a float: a real number (not ``bool``), finite and ``>= 0``."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise TypeError(f"nearby() max_distance must be a real number of metres, not {type(value).__name__}")
    try:
        distance = float(value)
    except OverflowError as exc:
        raise ValueError(
            "nearby() max_distance must be a finite number of metres, >= 0; this one is too large for a float"
        ) from exc
    if not math.isfinite(distance) or distance < 0:
        raise ValueError(f"nearby() max_distance must be a finite number of metres, >= 0, got {distance!r}")
    return distance
