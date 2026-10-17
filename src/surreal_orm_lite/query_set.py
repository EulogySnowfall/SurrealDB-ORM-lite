import contextlib
import logging
import math
import numbers
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Self, cast
from uuid import UUID

from pydantic import BaseModel
from pydantic_core import ValidationError

from . import BaseSurrealModel, SurrealDBConnectionManager
from . import cache as _cache
from . import encoding as _encoding
from . import geo as _geo
from . import subquery as _subquery
from . import time_travel as _time_travel
from ._sdk import NotFoundError, RecordID, SdkGeometryPoint, ServerError
from .cache import EDGES, CacheRequest, QueryCache, follows_links, statement_key, ttl_seconds
from .changefeed import ChangeModelStream, Since
from .edge import _check_edge_id, insert_edges
from .embedded import history_unsupported, keeps_history
from .enum import OrderBy
from .exceptions import SurrealDbError, SurrealDbNotFoundError
from .functions import SurrealFunc, Var
from .geo import GeoDistance, GeometryPoint
from .live import LiveModelStream, LiveStream, ReconnectCallback, missing_table_error, require_websocket
from .model_base import _snapshot_moment
from .q import Q
from .subquery import SubqueryCompiler
from .surql_literal import inline_variables
from .types import Duration
from .utils import (
    VALID_FIELD_PATTERN,
    as_record_value,
    build_filter_condition,
    coerce_record_id,
    is_select_item,
    parse_lookup,
    remove_quotes_for_variables,
    validate_alias_name,
    validate_field_name,
    validate_patch_operations,
    validate_select_item,
    warn_on_multiple_statements,
)

if TYPE_CHECKING:
    from .aggregations import Aggregation

logger = logging.getLogger(__name__)

#: The values a snapshot's fields can nest a record model in (``QuerySet._mark_snapshots()``, v0.26.0).
_SNAPSHOT_HOLDERS = (BaseModel, list, tuple, set, frozenset, dict)

# The query cache's refusals and warning (v0.28.0, spec §5.5: C3, C10, W1).
_CACHE_AS_OF = "cache() cannot be combined with as_of(): a time-travel read is never cached"
_CACHE_QUERY = (
    "query() runs your own SurrealQL and is never cached: drop cache(), or cache its result yourself with "
    "QueryCache.make_key(), get() and set()"
)
_CACHE_DISABLED = "QueryCache is disabled — .cache() has no effect"


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
        self._group_by_fields: list[str] = []
        self._annotations: dict[str, Aggregation | GeoDistance] = {}
        # GeoDistance annotations (v0.24.0), as annotate() checked them: alias -> column, reference point.
        self._geo_annotations: dict[str, tuple[str, GeometryPoint]] = {}
        # nearby() conditions (v0.24.0): column, reference point, distance in metres.
        self._nearby: list[tuple[str, GeometryPoint, float]] = []
        self._tx: Any = None
        # as_of() (v0.26.0): the moment a time-travel read is made at, normalised; None reads the current state.
        self._as_of: _time_travel.AsOf | None = None
        # cache() (v0.28.0): the oldest result this queryset's reads accept; None: no cache.
        self._cache: CacheRequest | None = None

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
        """
        return self.model.to_db_field(field)

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

        Variables can be used in parameterized queries to safely inject values.

        Args:
            **kwargs: Variable names and their values.

        Returns:
            Self: The current instance for method chaining.
        """
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

        A ``Subquery`` — or a QuerySet, read as one — is accepted as the whole value of ``in``, ``not_in``,
        ``containsall`` and ``containsany`` (compared with all its values) and of ``exact``, ``gt``, ``gte``,
        ``lt``, ``lte``, ``contains`` and ``not_contains`` (compared with its first value; no record when it is
        empty), here and in ``Q``. The inner queryset selects its record ids, or the one field given to its
        ``select()``; its ``filter()``, ``Q``, ``nearby()``, ``order_by()``, ``limit()``, ``offset()`` and
        ``variables()`` apply, its ``fetch()`` is ignored. It is read when this queryset runs, in the same
        statement: one request. A clause it cannot honour raises when this queryset compiles, before any request
        (see ``Subquery``). A subquery reading a table that does not exist reads as empty, as any missing table
        (SurrealDB 3.x raises: once the server confirms the table does not exist, the statement is sent once more
        with that subquery empty). Any other error raised while the subquery runs is the server's, relayed.
        """
        for arg in args:
            if not isinstance(arg, Q):
                raise TypeError(f"filter() positional arguments must be Q objects, got {type(arg).__name__}")
            self._q_filters.append(arg)
        for key, value in kwargs.items():
            field_name, lookup = parse_lookup(key)
            self._filters.append((self._column(field_name), lookup, value))
        return self

    def limit(self, value: int) -> Self:
        """
        Set a limit on the number of results to retrieve.

        Args:
            value: The maximum number of records to retrieve.

        Returns:
            Self: The current instance for method chaining.
        """
        self._limit = value
        return self

    def offset(self, value: int) -> Self:
        """
        Set an offset for the results.

        Args:
            value: The number of records to skip.

        Returns:
            Self: The current instance for method chaining.
        """
        self._offset = value
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
        """
        for field in fields:
            validate_field_name(field, "FETCH field")
        self._fetch_fields.extend(self._column(field) for field in fields)
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

        A time-travel queryset is read-only: ``bulk_create()``, ``bulk_update()``, ``bulk_delete()``,
        ``patch()``, ``update_or_create()``, ``get_or_create()``, ``delete_table()``, ``query()``, ``live()``,
        ``watch()`` and ``changes()`` raise ``SurrealDbError`` on it, before any request. ``_as_of`` is a
        reserved ``variables()`` name: the moment is bound under it, replacing a variable of that name.

        Returns:
            Self: the queryset, for chaining. Called again, the last moment wins.

        Raises:
            TypeError: ``at`` is none of the forms above (a ``date`` included) — T1.
            ValueError: a string that is not ISO-8601 (T2); a negative duration (T3); a moment outside
                1970-01-01T00:00:00Z … 2262-04-11T23:47:16.854775Z (T11).
            SurrealDbError: the queryset is bound to a transaction (``objects(tx=)``) — T4; a moment on a queryset
                with ``cache()`` (v0.28.0: a time-travel read is never cached) — C3. ``as_of(None)`` is accepted.
        """
        if self._tx is not None:
            raise SurrealDbError(_time_travel.transaction_refused())
        if at is not None and self._cache is not None:
            raise SurrealDbError(_CACHE_AS_OF)
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
        ``"author.*"``); any delete, when it reads an edge model (a deleted record's edges go with it).
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
        ``REFERENCE … ON DELETE``, views over other tables), values computed when the read runs (``Var()`` of a
        ``DEFINE PARAM``, ``<future>``/``COMPUTED`` fields, time functions in a ``select()`` expression): the TTL bounds
        them; call ``QueryCache.invalidate(Model)`` after such a write, ``QueryCache.clear()`` after changing the session
        behind the ORM's back.

        Returns:
            Self: the queryset, for chaining.

        Raises:
            TypeError: ``ttl`` is not a number of seconds or a ``timedelta`` (C1; ``bool`` refused).
            ValueError: ``ttl`` is negative or not finite (C2).
            SurrealDbError: the queryset reads ``as_of()`` (C3).
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

        A time-travel read is read-only and cannot be streamed: the first statement of each such method, so
        nothing is sent and no client is acquired.
        """
        if self._as_of is not None:
            raise SurrealDbError(_time_travel.queryset_refused(method))

    async def _time_travel_moment(self, *, fetches: bool = False) -> datetime | None:
        """The resolved moment of a read under ``as_of()``; ``None`` without it, and no request (v0.26.0).

        The first step of every read. In order:

        1. On the embedded engine, a URL that keeps no history (``embedded.keeps_history()``) raises T6
           before any client is acquired, so nothing is sent.
        2. A read that returns the records ``fetch()`` resolves (*fetches*), with ``fetch()`` set, asks the
           server line (one ``version()`` request, as ``changes()`` does) and raises T8 below 3: SurrealDB 2.x
           resolves fetched links to their current state. No other read asks the line.
        3. A duration or a ``SurrealFunc`` is resolved by the database (one ``RETURN`` request), and the
           resolved moment checked against the moments a version can address (``time_travel.resolve()``:
           T10 a/b/c, after which the read is never sent). An absolute moment, checked by ``as_of()``, needs
           no request.

        Args:
            fetches: whether the read returns the records ``fetch()`` resolves (``exec()`` outside
                ``values()``+``annotate()``, hence ``first()`` and ``get()`` without an id); the other reads
                ignore ``fetch()``.

        Raises:
            SurrealDbError: T6; T8; T10 (a, b, c).
        """
        if self._as_of is None:
            return None
        url = SurrealDBConnectionManager.get_connection_string()
        if SurrealDBConnectionManager.is_embedded() and not keeps_history(url):
            raise SurrealDbError(history_unsupported(url))
        client = await SurrealDBConnectionManager.get_client()
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

    def _subquery_compiler(self, empty_tables: frozenset[str] = frozenset(), *, inline: bool = False) -> SubqueryCompiler:
        """A compiler for the subqueries of one statement of this queryset (v0.27.0).

        It starts with this queryset's transaction, a copy of its ``variables()`` (the subqueries' are merged into
        it) and this queryset on the stack of the querysets being rendered, so a queryset given as its own subquery
        raises S3 before any other check of the inner queryset.

        Args:
            empty_tables: the tables whose subqueries are bound to ``[]`` (SurrealDB 3.x confirmed they do not exist).
            inline: write the sub-SELECTs inline instead of in ``LET`` s (a live query).
        """
        return SubqueryCompiler(
            inline=inline,
            empty_tables=empty_tables,
            outer_tx=self._tx,
            variables=dict(self._variables),
            _stack=[id(self)],
        )

    def _where_parts(
        self, counter: int = 0, *, as_of: bool = False, subqueries: SubqueryCompiler | None = None
    ) -> tuple[list[str], dict[str, Any], int]:
        """The AND-ed conditions of the WHERE clause, their bound values and the next ``$_f<N>`` number.

        Filters, ``Q`` objects and ``nearby()`` conditions, in that order, numbered from *counter* on: the outer
        statement starts at 0, a subquery (v0.27.0) continues the numbering of the statement it goes into. With
        ``as_of=True`` (a time-travel table read, v0.26.0) the per-record check ``time_travel.GUARD`` is the last
        condition, after the ``nearby()`` ones: every other part is a single comparison or a parenthesised group,
        so the check applies to every row they keep.

        Args:
            counter: the first ``$_f<N>`` number.
            as_of: append the time-travel guard.
            subqueries: the statement's compiler, which binds a subquery value; ``None`` refuses one (S8).
        """
        parts: list[str] = []
        variables: dict[str, Any] = {}
        endpoint_columns = self.model._endpoint_columns()

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
            )
            parts.append(sql)
            variables.update(vars_)

        # Q object filters
        for q in self._q_filters:
            sql, vars_, counter = q.to_sql(
                counter, self._model_table, self.model.to_db_field, endpoint_columns, subqueries=subqueries
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

        if as_of:
            parts.append(_time_travel.GUARD)

        return parts, variables, counter

    def _build_where(self, *, as_of: bool = False, subqueries: SubqueryCompiler | None = None) -> tuple[str, dict[str, Any]]:
        """
        Build a parameterized WHERE clause from filters, Q objects and ``nearby()`` conditions.

        With ``as_of=True`` (a time-travel table read, v0.26.0) the per-record check
        ``time_travel.GUARD`` is the last condition (:meth:`_where_parts`). A subquery value (v0.27.0) is bound
        by *subqueries*, the statement's compiler; without one it raises ``ValueError`` (S8).

        Returns:
            A tuple of (where_clause_string, variables_dict).
            The where_clause_string includes the ``WHERE`` keyword, or is empty if no filters.
        """
        parts, variables, _ = self._where_parts(0, as_of=as_of, subqueries=subqueries)
        if not parts:
            return "", {}

        return " WHERE " + " AND ".join(parts), variables

    def _subquery_select(self, compiler: SubqueryCompiler, counter: int) -> tuple[str, dict[str, Any], int]:
        """This queryset as the sub-SELECT of another queryset's filter (v0.27.0); its values and the next counter.

        ``(SELECT VALUE <column> FROM <table> [WHERE …] [LIMIT n] [START m] [VERSION $_as_of])``, the column being
        the one ``select()`` names, else ``id``. Ordered, ``(SELECT <column> AS _sqv, <order columns> FROM …
        ORDER BY … …)._sqv``: SurrealDB 2.x refuses to order a ``SELECT VALUE`` by another field and orders wrongly
        by a column it selects only under an alias, so every order column is selected by its name. Its filter
        values are bound from *counter* on, its ``variables()`` merged into the statement's, its own subqueries
        rendered first (their ``LET`` s precede its own). Under the outer ``as_of()`` (``compiler.moment``) it
        carries the per-record guard and ``VERSION $_as_of``: the server never applies the outer ``VERSION`` to a
        ``LET``. A table in ``compiler.empty_tables`` compiles to ``[]``. ``fetch()`` is ignored, as in the full
        SurrealDB-ORM: a sub-SELECT gives values, never records to resolve.

        Called by :meth:`SubqueryCompiler.reference`, which checks S3 first.

        Raises:
            ValueError: ``annotate()``, ``select()`` of several fields or ``values()`` (S4, every offender named);
                ``limit()``/``offset()`` not an ``int >= 0`` (S12); an invalid selected column; ``_sqv`` ordered on
                where the ordered form selects its alias (S13); its ``variables()`` binding a name of the statement
                to another value (S7); and its filters' own refusals.
            SurrealDbError: an ``as_of()`` of its own (S5); a transaction that is not the outer queryset's (S6).
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
        for clause, value in (("limit()", self._limit), ("offset()", self._offset)):
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise ValueError(_subquery.bad_slice(clause, value))
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
        compiler.merge_variables(self._variables)
        if table in compiler.empty_tables:
            return "[]", {}, counter
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
        self, moment: datetime | None = None, *, subqueries: SubqueryCompiler | None = None
    ) -> tuple[str, dict[str, Any]]:
        """
        Compile the QuerySet parameters into a parameterized SQL query.

        Args:
            moment: Under ``as_of()``, the resolved moment of the read (v0.26.0): the guarded WHERE and
                ``VERSION $_as_of`` are compiled, ``$_as_of`` bound to it. Ignored without ``as_of()``.
            subqueries: The compiler of the statement's subqueries (v0.27.0); a new one by default. With
                subqueries the statement is a block, ``{ LET $_sq0 = (…); (SELECT …) };``; without, the text
                and variables of v0.26.0.

        Returns:
            A tuple of (query_string, variables_dict).

        Raises:
            RuntimeError: under ``as_of()`` without a moment (an internal error).
            ValueError, SurrealDbError: a subquery the ORM cannot compile (S2–S7, S12, S13, S15).
        """
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
        query = f"SELECT {', '.join(projection)} FROM {self._model_table}"

        query += where_clause

        if self._order_by:
            query += f" ORDER BY {self._order_by}"

        if self._limit is not None:
            query += f" LIMIT {self._limit}"

        if self._offset is not None:
            query += f" START {self._offset}"

        if self._fetch_fields:
            query += f" FETCH {', '.join(self._fetch_fields)}"

        query, variables = self._terminate(query, {**compiler.variables, **where_vars, **distance_vars}, moment)
        return compiler.wrap(query), variables

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
            ValueError: a ``GeoDistance`` annotation is present: it is computed per record.
            RuntimeError: under ``as_of()`` without a moment (an internal error).
            ValueError, SurrealDbError: a subquery the ORM cannot compile, as for :meth:`_compile_query`.
        """
        if self._geo_annotations:
            raise ValueError(_GEO_DISTANCE_PER_RECORD)
        self._reject_annotation_collisions()
        moment = self._version_moment(moment)
        compiler = subqueries if subqueries is not None else self._subquery_compiler()
        compiler.moment = moment
        where_clause, where_vars = self._build_where(as_of=moment is not None, subqueries=compiler)

        select_parts = list(self._group_by_fields)
        for alias, agg in self._annotations.items():
            select_parts.append(f"{cast('Aggregation', agg).to_sql(self.model.to_db_field)} AS {alias}")

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
        self, query: str, variables: dict[str, Any] | None = None, *, subquery_tables: frozenset[str] = frozenset()
    ) -> list[dict[str, Any]]:
        """
        Execute a read query. Routes through the transaction when one is attached
        (``objects(tx=)``), so reads participate in the transaction and see its
        uncommitted writes (interactive, 3.x). On a buffered tx, ``run_read`` raises.

        A table that does not exist reads as no rows (``NotFoundError``, SurrealDB 3.x).

        On a queryset read ``as_of()`` (v0.26.0) — every read of which is a ``VERSION`` read, its writes
        and ``query()`` being refused — a server whose storage keeps no history ("does not support
        versioned queries": SurrealDB 3.x, 2.x on RocksDB) is reported as ``SurrealDbError`` (T7), chained
        to the SDK's error. Keyed on ``as_of()``, never on the variables: without it, every error is
        relayed unchanged.

        Args:
            query: the statement.
            variables: its bound values (the queryset's ``variables()`` when ``None``).
            subquery_tables: the tables a ``LET`` of the statement still reads (v0.27.0; empty: the behaviour
                above, exactly). While the set is not empty, only the server's own missing-table error reads as
                anything but itself (:meth:`_reads_as_empty`): naming one of these tables, and confirmed by the
                server, it asks :meth:`_run_compiled` to send the statement again with that subquery empty;
                naming another table, it is the outer table's and reads as no rows. Every other error is relayed.

        Raises:
            _MissingSubqueryTable: the server confirmed that a table of *subquery_tables* does not exist (caught by
                :meth:`_run_compiled`).
        """
        vars_ = variables if variables is not None else self._variables
        compiled = remove_quotes_for_variables(query)
        if self._tx is not None:
            try:
                return await self._tx.run_read(compiled, vars_)  # type: ignore[no-any-return]
            except ServerError as exc:
                if await self._reads_as_empty(exc, subquery_tables):
                    return []
                raise
        client = await SurrealDBConnectionManager.get_client()
        try:
            return await client.query(compiled, _encoding.encode(vars_))  # type: ignore
        except ServerError as exc:
            if await self._reads_as_empty(exc, subquery_tables):
                return []
            if self._as_of is not None and _time_travel.UNSUPPORTED_MARKER in str(exc):
                raise SurrealDbError(_time_travel.server_refused(exc)) from exc
            raise

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
                client = await SurrealDBConnectionManager.get_client()
                await client.query(_subquery.TABLE_PROBE, _encoding.encode(variables))
        except NotFoundError as exc:
            return _subquery.missing_table(exc) == table
        except ServerError:
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

        Args:
            compile_statement: compiles the statement with the compiler it is given; returns its text and variables.
            write: a write by filter: inside a transaction it is added to it, never sent again (the server's
                error is the transaction's).
        """
        if write or self._cache is None or self._tx is not None:
            return await self._run_uncached(compile_statement, write=write)
        return await self._run_cached(compile_statement)

    async def _run_uncached(
        self, compile_statement: Callable[[SubqueryCompiler], tuple[str, dict[str, Any]]], *, write: bool = False
    ) -> Any:
        """The loop of :meth:`_run_compiled`, as in v0.27.0: compile, send, and on a confirmed missing subquery
        table compile again with that subquery bound to ``[]`` and send again."""
        empty: frozenset[str] = frozenset()
        while True:
            compiler = self._subquery_compiler(empty)
            query, variables = compile_statement(compiler)
            if write and self._tx is not None:
                return await self._tx.add(query, variables)
            read = frozenset(compiler.tables) - empty
            if not read:
                return await self._execute_query(query, variables)
            try:
                return await self._execute_query(query, variables, subquery_tables=read)
            except _subquery._MissingSubqueryTable as missing:
                empty |= {missing.table}

    async def _run_cached(self, compile_statement: Callable[[SubqueryCompiler], tuple[str, dict[str, Any]]]) -> Any:
        """A read of a queryset with ``cache()``, outside a transaction (v0.28.0, spec §6.3).

        The statement is compiled once to key the read and decide what it depends on (spec §5.3): the model's
        table and every table a subquery reads; ``EDGES`` when one of them is an edge model's (every delete
        invalidates it); "any table" when the statement follows links (``FETCH``, a dotted path, a graph arrow) or
        a ``select()`` item is neither ``*`` nor a plain field path (``author.*``: projection semantics read the
        linked table). On a miss :meth:`_run_uncached` sends it, compiling it again
        (compilation is pure), so the missing-subquery-table re-send happens inside the read.
        """
        compiler = self._subquery_compiler()
        query, variables = compile_statement(compiler)
        tables = {self._model_table, *compiler.tables}
        if self.model.__surreal_edge__ or compiler.edge_tables:
            tables.add(EDGES)
        any_table = (
            " FETCH " in query
            or follows_links(query)
            or any(item != "*" and not VALID_FIELD_PATTERN.match(item) for item in self.select_item)
        )
        return await self._cached_read(
            "query", query, variables, frozenset(tables), any_table, lambda: self._run_uncached(compile_statement)
        )

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

        The one read frontier of the cache (spec §6.3): every compiled read (:meth:`_run_cached`), ``get(<id>)``
        and ``all()``. The rows returned — a copy of the cached ones on a hit — go through the caller's usual
        post-processing, as on a miss. A TTL of ``0`` never looks up nor stores; a read the cache cannot key (a
        client without a scope, no CBOR encoder, a value the SDK cannot send) goes to the database, as does one a
        write in flight concerns. The cache's own failures never fail the read; *run*'s errors propagate, and an
        error is never cached.

        Args:
            kind: ``"query"`` (a compiled statement) or ``"select"`` (the SDK's ``select()``, *statement* empty).
            statement: the statement as compiled, before ``remove_quotes_for_variables``.
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
        client = await SurrealDBConnectionManager.get_client()
        scope = SurrealDBConnectionManager._cache_scope(client)
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
        the grouped fields and aggregation results. With ``GeoDistance`` annotations, returns the
        model instances, each carrying its distances as attributes (see :meth:`annotate`).
        Served from the query cache with ``cache()`` (v0.28.0), as new instances.

        Returns:
            list[BaseSurrealModel] | list[dict]: Model instances or dictionaries for GROUP BY queries.
            Under ``as_of()`` the instances are snapshots (v0.26.0).
        """
        # The values()+annotate() branch compiles no FETCH: only the other two return fetched records.
        moment = await self._time_travel_moment(fetches=bool(self._geo_annotations) or not self._annotations)
        if self._geo_annotations:
            return await self._exec_with_distances(moment)

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

    async def _exec_with_distances(self, moment: datetime | None = None) -> Any:
        """Run the per-record ``SELECT`` of the ``GeoDistance`` annotations (v0.24.0).

        The alias keys are taken out of each row before validation, so they never reach a field, an
        ``extra="allow"`` model's extras or a later write, then set on the instance with
        ``object.__setattr__``. Rows that do not validate (a ``select()`` that omits a required
        field…) come back as dicts, alias keys included, as ``exec()`` returns them today. Under
        ``as_of()``, *moment* is the read's resolved moment and the instances are snapshots (v0.26.0).
        """
        self._reject_geo_distance_mixing(self._annotations, grouped=bool(self._group_by_fields))
        results = await self._run_compiled(lambda c: self._compile_query(moment, subqueries=c))
        rows = results if isinstance(results, list) else []
        stripped: list[dict[str, Any]] = []
        distances: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            distances.append({alias: record.pop(alias, None) for alias in self._geo_annotations})
            stripped.append(record)
        try:
            instances = cast(list[Any], self.model.from_db(stripped))
        except ValidationError as e:
            logger.info(f"Pydantic invalid format for the class, returning dict value: {e}")
            return [self._py_keyed(row) for row in rows]
        for instance, values in zip(instances, distances, strict=True):
            for alias, value in values.items():
                object.__setattr__(instance, alias, value)
        return self._mark_snapshots(instances, moment)

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
                except NotFoundError:
                    raise SurrealDbNotFoundError("No result found.") from None
                data = rows[0] if isinstance(rows, list) and rows else rows
                if not data:
                    raise SurrealDbNotFoundError("No result found.")
                return self.model.from_db(data)

            if self._cache is not None:
                return await self._cached_get(record_id)

            client = await SurrealDBConnectionManager.get_client()
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
            except NotFoundError:
                return self.model.from_db([])
            return self.model.from_db(rows if isinstance(rows, list) else [])

        if self._cache is not None:
            return self.model.from_db(await self._cached_select(self._model_table, frozenset({self._model_table}), []))

        client = await SurrealDBConnectionManager.get_client()
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
        """The SDK's ``select(target)`` through :meth:`_cached_read` (v0.28.0, spec §5.3): keyed on the target,
        tagged with its table (and ``EDGES`` on an edge model); *missing* when the server says it does not exist."""
        if self.model.__surreal_edge__:
            tables |= {EDGES}

        async def run() -> Any:
            client = await SurrealDBConnectionManager.get_client()
            try:
                return await client.select(target)
            except NotFoundError:
                return missing

        return await self._cached_read("select", "", {"target": target}, tables, False, run)

    @_cache.invalidates("table", deletes=True)
    async def delete_table(self) -> bool:
        """
        Delete the associated table from the SurrealDB database.

        Returns True. A missing table is treated as already-absent (no-op),
        so this is safe for idempotent cleanup on SurrealDB 3.x (which raises
        NotFoundError) as well as 2.x (which is a no-op).

        It runs at once, outside the queryset's ``objects(tx=)`` transaction (a rollback does
        not bring the rows back), so the cached reads of the table — and of every edge table,
        whose edges die with the records — are invalidated when it ends, never at a commit.
        """
        self._reject_time_travel("delete_table()")
        client = await SurrealDBConnectionManager.get_client()
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
            ValueError: a field name is invalid, or a ``GeoDistance`` annotation is present (it is
                computed per record, not per group).
        """
        for field in fields:
            validate_field_name(field, "GROUP BY field")
        self._reject_geo_distance_mixing(self._annotations, grouped=bool(fields))
        self._group_by_fields = [self._column(field) for field in fields]
        return self

    def annotate(self, **annotations: "Aggregation | GeoDistance") -> Self:
        """
        Add aggregation annotations, or per-record distances, to the query.

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

        ``exec()``, ``first()`` and ``get()`` return model instances carrying the value as an
        attribute named after the alias; it is never written back (``model_dump()``, ``update()``,
        ``upsert()`` leave it out). Rows that do not validate (a ``select()`` that omits a required
        field) come back as dicts holding the alias. ``order_by("dist")``/``order_by("-dist")`` sort
        on it (ascending puts the ``None`` values first). The alias is a projection, not a stored
        column: ``filter(dist__lt=...)`` filters a column named ``dist``; bound the distance with
        ``nearby()``. On an ``extra="allow"`` model, a stored undeclared column with the alias's
        name is replaced by the distance in the results, and a later ``update()`` removes it: choose
        an alias no record stores. The field and the point are checked and copied here: changing
        the ``GeoDistance`` afterwards does not change this queryset.

        Args:
            **annotations: The alias of each value, and an ``Aggregation`` or a ``GeoDistance``.

        Returns:
            Self: The current QuerySet instance for method chaining.

        Raises:
            TypeError: a value is neither an ``Aggregation`` nor a ``GeoDistance``.
            ValueError: an invalid alias; for a ``GeoDistance``, an alias that names a declared field
                or its column, ``id``, ``in``, ``out``, a name starting with ``_`` or ``model_`` or an
                attribute of the model (``save``, ``objects``…), an invalid field name or a field
                declared as a geometry that holds no point; a ``GeoDistance`` combined with an
                ``Aggregation`` or with ``values()``.
        """
        from .aggregations import Aggregation as AggregationClass

        distances: dict[str, tuple[str, GeometryPoint]] = {}
        for alias, value in annotations.items():
            validate_alias_name(alias)
            if isinstance(value, GeoDistance):
                distances[alias] = self._geo_distance_snapshot(alias, value)
            elif not isinstance(value, AggregationClass):
                raise TypeError(
                    f"annotate() argument '{alias}' must be an Aggregation instance or a GeoDistance, "
                    f"got {type(value).__name__}"
                )
        self._reject_geo_distance_mixing({**self._annotations, **annotations}, grouped=bool(self._group_by_fields))
        self._annotations.update(annotations)
        # An alias given again is replaced, an Aggregation included: keep the snapshots of the others.
        kept = {alias: snapshot for alias, snapshot in self._geo_annotations.items() if alias not in annotations}
        self._geo_annotations = {**kept, **distances}
        return self

    def _geo_distance_snapshot(self, alias: str, distance: GeoDistance) -> tuple[str, GeometryPoint]:
        """Check a ``GeoDistance`` annotation against the model; return its column and point.

        The alias must not shadow anything a record or an instance already has: ``SELECT *, … AS
        <alias>`` replaces a stored column of that name in the result (``AS id`` replaces the id),
        and the value is set on the instance as an attribute.
        """
        reason = self._reserved_distance_alias(alias)
        if reason is not None:
            raise ValueError(f"annotate() alias {alias!r} {reason}; choose another alias for the GeoDistance")
        field = _geo._distance_field(distance.field)
        validate_field_name(field, "GeoDistance field")
        self._check_point_field(field, "GeoDistance")
        return self._column(field), _geo._as_point(distance.point)

    def _reserved_distance_alias(self, alias: str) -> str | None:
        """Why ``alias`` cannot name a ``GeoDistance`` on this model, or ``None`` when it can."""
        if alias in ("id", "in", "out"):
            return "is reserved: it names the record's id or an edge endpoint"
        if alias.startswith(("_", "model_")):
            return "is reserved: names starting with '_' or 'model_' belong to Pydantic"
        model = self.model
        if alias in model.model_fields:
            return f"is a field of {model.__name__}: the distance would replace its value in the results"
        for name in model.model_fields:
            if model.to_db_field(name) == alias:
                return f"is the column of field {name!r}: the distance would replace its value in the results"
        if hasattr(model, alias):
            return f"is an attribute of {model.__name__}: the distance would shadow it on the instances"
        return None

    @staticmethod
    def _reject_geo_distance_mixing(annotations: Mapping[str, Any], *, grouped: bool) -> None:
        """Refuse a ``GeoDistance`` next to an ``Aggregation`` or a ``values()`` grouping."""
        kinds = {isinstance(value, GeoDistance) for value in annotations.values()}
        if True in kinds and (False in kinds or grouped):
            raise ValueError(_GEO_DISTANCE_PER_RECORD)

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

        Returns:
            True if at least one record matches, False otherwise.
        """
        moment = await self._time_travel_moment()
        original_limit = self._limit
        try:
            self._limit = 1
            results = await self._run_compiled(lambda c: self._compile_query(moment, subqueries=c))

            if isinstance(results, list):
                return len(results) > 0
            return False
        finally:
            self._limit = original_limit

    # ==================== Bulk Operations ====================

    @_cache.invalidates("queryset")
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

        Raises:
            SurrealDbError: an edge id that already exists, edges the ``PERMISSIONS`` refused, or
                any other refusal of an edge batch by the server; a model read with ``as_of()``
                (a read-only snapshot) in ``models``, before any call (v0.26.0).
            SurrealDbNotFoundError: an endpoint of an ``ENFORCED`` relation that does not exist.
            ValueError: an edge id that is a ``RecordID`` of another table, or a ``bool``.

        Example::

            users = [User(name="Alice"), User(name="Bob"), User(name="Charlie")]
            created = await User.objects().bulk_create(users)

            edges = await Purchased.objects().bulk_create([
                Purchased(in_=alice, out=widget, quantity=3),
                Purchased(in_=bob, out=widget, quantity=1),
            ])  # one insert_relation() round-trip
        """
        self._reject_time_travel("bulk_create()")
        for model in models:
            moment = _snapshot_moment(model) if isinstance(model, BaseSurrealModel) else None
            if moment is not None:
                raise SurrealDbError(_time_travel.snapshot_write_refused(type(model).__name__, "bulk_create()", moment))
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

        client = await SurrealDBConnectionManager.get_client()
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

        created = await insert_edges(self._model_table, rows, tx=self._tx)
        if self._tx is not None and not self._tx.is_interactive:
            return models  # buffered: not executed until commit; return inputs
        return cast(list[BaseSurrealModel], self.model.from_db(created))

    @_cache.invalidates("queryset")
    async def bulk_update(self, **kwargs: Any) -> int:
        """
        Update all records matching the current filters.

        A subquery in the filters (v0.27.0) is read by the server in the same statement, a block
        ``{ LET $_sq0 = (…); (UPDATE …) };``: one request, and on SurrealDB 3.x a subquery table that does not exist
        reads as empty, as on 2.x (outside a transaction; inside one, the server's error is the transaction's). A
        value to write is data, never a subquery.

        Args:
            **kwargs: Fields and values to update.

        Returns:
            The number of updated records (``0`` inside a buffered transaction, whose statements run at commit).

        Raises:
            ValueError: a value is a ``Subquery`` or a QuerySet (v0.27.0), before any request: filter with it, and
                pass the value to write.

        Example::

            count = await User.objects().filter(status="pending").bulk_update(status="active")
            count = await Post.objects().filter(author__in=Subquery(banned)).bulk_update(hidden=True)
        """
        self._reject_time_travel("bulk_update()")
        if not kwargs:
            return 0
        for field, value in kwargs.items():
            if _subquery.subquery_of(value) is not None:
                raise ValueError(_subquery.value_written(field))
        self.model._reject_computed_writes(kwargs, "bulk_update()")

        def compile_statement(compiler: SubqueryCompiler) -> tuple[str, dict[str, Any]]:
            where_clause, where_vars = self._build_where(subqueries=compiler)
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

    @_cache.invalidates("queryset", deletes=True)
    async def bulk_delete(self) -> int:
        """
        Delete all records matching the current filters.

        A subquery in the filters (v0.27.0) is read by the server in the same statement, as for
        :meth:`bulk_update`.

        Returns:
            The number of deleted records (``0`` inside a buffered transaction, whose statements run at commit).

        Example::

            count = await User.objects().filter(status="inactive").bulk_delete()
            count = await Post.objects().filter(author__in=Subquery(banned)).bulk_delete()
        """
        self._reject_time_travel("bulk_delete()")

        def compile_statement(compiler: SubqueryCompiler) -> tuple[str, dict[str, Any]]:
            where_clause, where_vars = self._build_where(subqueries=compiler)
            query = f"DELETE {self._model_table}{where_clause} RETURN BEFORE;"
            return compiler.wrap(query), {**compiler.variables, **where_vars}

        results = await self._run_compiled(compile_statement, write=True)
        return len(results) if isinstance(results, list) else 0

    @_cache.invalidates("queryset")
    async def patch(self, operations: list[dict[str, Any]]) -> int:
        """
        Apply a JSON Patch (RFC 6902) to every row matching the current filters.

        Whole table if unfiltered. Returns the number of affected rows. Participates in
        ``objects(tx=)`` like the other bulk operations. ``operations`` is validated then bound
        as ``$_ops`` (never string-interpolated). Emits no signals. A subquery in the filters
        (v0.27.0) is read by the server in the same statement, as for :meth:`bulk_update`.

        Example::

            n = await User.objects().filter(status="trial").patch(
                [{"op": "replace", "path": "/plan", "value": "free"}]
            )
        """
        self._reject_time_travel("patch()")
        validate_patch_operations(operations)
        self.model._reject_computed_patch(operations, "patch()")

        def compile_statement(compiler: SubqueryCompiler) -> tuple[str, dict[str, Any]]:
            where_clause, where_vars = self._build_where(subqueries=compiler)
            query = f"UPDATE {self._model_table} PATCH $_ops{where_clause};"
            ops = self.model._columns_for_patch(operations)
            return compiler.wrap(query), {**compiler.variables, **where_vars, "_ops": ops}

        results = await self._run_compiled(compile_statement, write=True)
        return len(results) if isinstance(results, list) else 0

    # ==================== Upsert / get_or_create ====================

    @staticmethod
    def _criteria_payload(criteria: dict[str, Any]) -> dict[str, Any]:
        """Field=value pairs usable as a WRITE payload, taken from EQUALITY criteria only.

        ``criteria`` keys are filter lookups (``field`` or ``field__lookup``). A literal raw
        key like ``name__contains`` must never be written as a column, so only ``exact``
        lookups contribute to the payload (their base field name → value). Non-``exact``
        lookups (``gt``/``contains``/``in``/…) still drive the lookup but cannot be written,
        so they are excluded here — mirroring Django, which only writes exact lookups on
        create.

        Values go through the same interpretation the WHERE builder applies, so the row that
        gets created is the row the next lookup finds: ``"$$literal"`` is un-escaped to
        ``"$literal"`` (otherwise ``get_or_create(name="$$admin")`` filtered on ``"$admin"``
        but stored ``"$$admin"``, never converging and creating a duplicate on every call),
        and a query-variable reference is refused outright — its value lives on the server,
        so it cannot be written back (issue #156 follow-up). A model instance is written as its
        record id, the value its lookup binds (v0.22.0): ``get_or_create(customer=alice)``
        stores ``customer = RecordID(...)``.
        """
        payload: dict[str, Any] = {}
        for key, value in criteria.items():
            field_name, lookup = parse_lookup(key)
            if lookup != "exact":
                continue
            if isinstance(value, Var) or (isinstance(value, str) and value.startswith("$") and value[1:2] != "$"):
                raise SurrealDbError(
                    f"Cannot create a record from the query-variable reference given for '{field_name}': "
                    "its value is only known to the database. Pass the literal value in defaults=."
                )
            if isinstance(value, str) and value.startswith("$$"):
                value = value[1:]
            payload[field_name] = as_record_value(value)
        return payload

    @staticmethod
    def _reject_subquery_writes(criteria: Mapping[str, Any], defaults: Mapping[str, Any] | None, method: str) -> None:
        """Refuse a subquery that ``get_or_create()``/``update_or_create()`` (*method*) would have to write (v0.27.0).

        A subquery is a filter value whose values only the database knows: under an ``exact`` criterion, which
        is written on create, or in ``defaults``, which is data. Another lookup (``author__in=Subquery(…)``)
        drives the lookup only and is never written. Checked before any request.

        Raises:
            SurrealDbError: an ``exact`` criterion whose value is a ``Subquery`` or a QuerySet (S9a), then such a
                ``defaults`` value (S9b).
        """
        for key, value in criteria.items():
            field_name, lookup = parse_lookup(key)
            if lookup == "exact" and _subquery.subquery_of(value) is not None:
                raise SurrealDbError(_subquery.criterion_written(field_name))
        for key, value in (defaults or {}).items():
            if _subquery.subquery_of(value) is not None:
                raise SurrealDbError(_subquery.default_written(method, key))

    async def _lookup_matches(self, criteria: dict[str, Any]) -> list[Any]:
        """Return rows matching ``criteria``, routed through the transaction when attached.

        Reuses the parameterized WHERE builder (anti-injection) via a throwaway QuerySet's
        ``filter(**criteria)``, joined to ``self._tx`` (``objects(tx=)``) so the read participates in
        it — seeing the tx's uncommitted writes on an interactive transaction, and raising on a
        buffered one (consistent with every other read). ``LIMIT 2`` is enough to distinguish
        0 / 1 / >1. A subquery criterion (v0.27.0) is read in the same statement, a block
        ``{ LET $_sq0 = (…); (SELECT … LIMIT 2) };``, run as every read is (:meth:`_run_compiled`).
        """
        probe = QuerySet(self.model)
        probe.filter(**criteria)
        probe._tx = self._tx
        table = self._model_table

        def compile_statement(compiler: SubqueryCompiler) -> tuple[str, dict[str, Any]]:
            where_clause, where_vars = probe._build_where(subqueries=compiler)
            query = f"SELECT * FROM {table}{where_clause} LIMIT 2;"
            return compiler.wrap(query), {**compiler.variables, **where_vars}

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
        """
        self._reject_time_travel("update_or_create()")
        self._reject_subquery_writes(criteria, defaults, "update_or_create")
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
        """
        self._reject_time_travel("get_or_create()")
        self._reject_subquery_writes(criteria, defaults, "get_or_create")
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
        """
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

        :raises SurrealDbError: for a clause a live query cannot honour.
        :raises TypeError: for a filter value with no SurrealQL literal form.
        :raises ValueError, SurrealDbError: for a subquery the ORM cannot compile (S2–S7, S12, S13, S15).
        """
        self._reject_live_clauses()
        compiler = subqueries if subqueries is not None else self._subquery_compiler(inline=True)
        where_clause, where_vars = self._build_where(subqueries=compiler)
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
        not exist and then never notifies, but refuses that check: the ORM raises S11 instead. 2.x
        answers it (the table reads as empty) and the live query starts. A resubscribe runs the
        check again, on the new connection.
        """
        compiler = self._subquery_compiler(inline=True)
        query = self._compile_live(diff, compiler)
        table = self._model_table
        tables = tuple(sorted(compiler.tables))
        check = "RETURN [" + ", ".join(f"(SELECT VALUE id FROM {name} LIMIT 1)" for name in tables) + "];"

        async def start(client: Any = None) -> UUID:
            # A resubscribe passes the client it is about to move the stream onto, so the live
            # query is guaranteed to run on that very connection.
            require_websocket(SurrealDBConnectionManager.get_connection_string())
            if client is None:
                client = await SurrealDBConnectionManager.get_client()
            if tables:
                try:
                    await client.query(check)
                except ServerError as exc:
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
            ``offset``/``objects(tx=)``/``cache()`` (v0.28.0: a stream is never cached), and on a
            queryset read ``as_of()`` (a live query streams the current changes, v0.26.0), here; for a
            non-WebSocket connection (the embedded engine included: read its change feed with
            :meth:`changes`), on start.
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
        :meth:`live`, and so do its refusals (``as_of()`` and ``cache()`` included, here) and its subqueries (inline,
        the tables they read checked on start: ``SurrealDbNotFoundError`` on SurrealDB 3.x for a
        missing one).
        """
        self._reject_time_travel("watch()")
        return LiveStream(
            self._model_table,
            self._live_starter(diff),
            auto_resubscribe=auto_resubscribe,
            on_reconnect=on_reconnect,
        )

    def _reject_changes_clauses(self) -> None:
        """Refuse every clause, naming each: ``SHOW CHANGES`` reads a whole table's feed.

        It has no ``WHERE``, projection, ordering or paging, so a filtered queryset would
        silently stream every record of the table — the wrong answer the ORM never gives.
        """
        unsupported = {
            "filter()": bool(self._filters or self._q_filters),
            "nearby()": bool(self._nearby),
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
        :raises SurrealDbError: for any queryset clause (``as_of()`` and ``cache()`` included), here; on start, for a
            table that does not exist or has no change feed (on the table or on its database), or for a
            datetime ``since`` on the embedded engine of the surrealdb SDK 2.x.
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
                than run on the current data; the queryset has ``cache()`` (v0.28.0, C10): your own SurrealQL is
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
        client = await SurrealDBConnectionManager.get_client()
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


#: The refusal of a ``GeoDistance`` combined with ``values()`` or an ``Aggregation``.
_GEO_DISTANCE_PER_RECORD = (
    "annotate(): a GeoDistance is computed per record; it cannot be combined with GROUP BY aggregations or values()"
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
