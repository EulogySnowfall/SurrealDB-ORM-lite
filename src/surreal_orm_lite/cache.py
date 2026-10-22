"""Query cache (v0.28.0): a client-side store of read results, ``QuerySet.cache()``'s and ``QueryCache``'s.

``QueryCache`` is the full SurrealDB-ORM's class-level singleton, same names (``configure``, ``make_key``, ``get``,
``set``, ``invalidate``, ``clear``, ``stats``): all state in class attributes, one ``threading.Lock`` (several event
loops and threads share it), never held across an ``await``, a copy or a call into the SDK. The SDK has no cache of
its own; it is only used for its CBOR encoder, which fingerprints a read's variables exactly as they are sent.

What makes a cached result right:

- **Keys** (:func:`statement_key`, :meth:`QueryCache.make_key`): the SHA-256 of one CBOR array — the domain
  (``"orm"`` or ``"user"``), the scope or the table, the kind, the statement and the variables as the ORM hands them
  to the SDK (``encoding.encode``), mapping keys sorted (:func:`_canonical`: SurrealDB objects ignore key order). Two
  keys are equal only when the requests are equivalent: ``RecordID("user", 1)``, ``RecordID("user", "1")`` and
  ``"user:1"`` never share one.
- **Scopes**: every connection identity the manager stamps gets a new, never-reused scope (:func:`bind_scope`), part
  of every ORM key; a stamp or a dropped client retires the previous scope and its entries.
- **Tickets**: a miss takes a ticket (:meth:`QueryCache._begin_read`) holding the epoch, the generations of the
  tables it reads and the count of completed writes; its result is stored (:meth:`QueryCache._finish_read`) only if
  none of them moved and no write to its tables is in flight. A write (:func:`writing`, :func:`invalidates`) puts
  its tables in flight for its whole call (no entry of them is served, by ``QueryCache.get()`` neither, and no read
  of them is stored), then bumps their generations and removes their entries; a read that follows links
  (``any_table``) is removed by every write. ``EDGES`` tags the reads of edge tables, which every
  delete invalidates (a deleted record's edges go with it).

Internal failures never fail a read: keying, copying a hit and copying a result to store fall back to the database.
Nothing is logged but the fixed warning of an SDK without a CBOR encoder: no row, variable or key.

A leaf module: it imports ``_sdk``, ``encoding``, ``exceptions``, ``routing`` and the standard library only
(``BaseSurrealModel`` under ``TYPE_CHECKING``, and lazily in :meth:`QueryCache.invalidate`).
"""

from __future__ import annotations

import builtins
import collections
import copy
import functools
import hashlib
import inspect
import itertools
import logging
import math
import numbers
import re
import threading
import time
import weakref
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Coroutine, Iterable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Final, Literal, TypeVar, cast

from . import encoding as _encoding
from . import routing as _routing
from ._sdk import cbor_encode
from .exceptions import SurrealDbError

if TYPE_CHECKING:
    from .model_base import BaseSurrealModel

__all__ = ["QueryCache"]

logger = logging.getLogger(__name__)

#: The cache's clock. Tests patch ``cache._clock``, never ``time.monotonic`` (asyncio uses it).
_clock: Callable[[], float] = time.monotonic
#: How the cache copies what it stores and what it hands out. Tests patch ``cache._deepcopy``.
_deepcopy: Callable[[Any], Any] = copy.deepcopy

#: Pseudo-table carried by every read of an edge model's table; every delete invalidates it. Never a table name.
EDGES: Final = "<edges>"

_W2: Final = "QueryCache: the installed surrealdb SDK exposes no CBOR encoder; cache() reads go to the database"
_C9: Final = (
    "QueryCache.make_key() needs the surrealdb SDK's CBOR encoder (surrealdb.data.cbor.encode), which the installed "
    "SDK does not expose"
)

# A dotted path ``a.b`` (not after a word, a dot or a parenthesis: ``)._sqv``, ``1.5``), with the method name it may
# end with (``loc.is_point(``); a graph arrow.
_PATH: Final = re.compile(r"(?<![\w.)])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)(\s*\()?")
_ARROW: Final = re.compile(r"->|<-")


@dataclass(frozen=True, slots=True)
class CacheRequest:
    """What ``QuerySet.cache()`` records: the oldest result its reads accept."""

    ttl: float | None  # None: QueryCache's default when the read runs


@dataclass(slots=True)
class _Entry:
    data: Any  # a deep copy, never handed out
    tables: frozenset[str]  # may hold EDGES
    any_table: bool  # follows links: removed by any write
    scope: int | None  # None: an entry of QueryCache.set()
    stored_at: float
    expires_at: float


@dataclass(frozen=True, slots=True)
class _Ticket:
    """A miss that may store its result, if nothing it depends on moved meanwhile."""

    key: str
    tables: frozenset[str]
    any_table: bool
    scope: int
    ttl: float
    epoch: int
    generations: tuple[int, ...]
    writes: int


@dataclass(slots=True)
class _ScopeCell:
    """The scope of one client the manager stamped (weakly keyed on the client; never refers to it)."""

    scope: int
    finalizer: weakref.finalize[Any, Any] | None = None


#: The cell of every client stamped, read and written under ``QueryCache._lock``.
_client_scopes: weakref.WeakKeyDictionary[Any, _ScopeCell] = weakref.WeakKeyDictionary()
#: The scopes of collected clients, appended without the lock by their finalizer; drained by every locked section.
_retired: collections.deque[int] = collections.deque()


def ttl_seconds(value: object, where: str) -> float:
    """``value`` as seconds >= 0 (``0.0``: never cached); the errors name ``where``.

    An ``int`` or a ``float`` (``numbers.Real``, never a ``bool``) or a ``timedelta``.

    Raises:
        TypeError: anything else.
        ValueError: negative or not finite.
    """
    if isinstance(value, bool) or not isinstance(value, (numbers.Real, timedelta)):
        raise TypeError(f"{where} must be a number of seconds or a timedelta, not {type(value).__name__}")
    try:
        seconds = value.total_seconds() if isinstance(value, timedelta) else float(value)
    except OverflowError:
        seconds = math.inf
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"{where} must be a finite number of seconds >= 0, got {value!r}")
    return 0.0 if seconds == 0 else seconds


def follows_links(statement: str) -> bool:
    """Whether ``statement`` reads through a link: a dotted path that is not a method call, or a graph arrow.

    ``author.name`` and ``author.loc.is_point()`` are paths; ``loc.is_point()``, ``math::sum``, ``1.5``, ``1.5s`` and
    an ordered subquery's ``)._sqv`` are not. A path into an embedded object (``address.city``) counts too: it costs
    hits, never correctness.
    """
    if _ARROW.search(statement):
        return True
    for match in _PATH.finditer(statement):
        dots = match.group(1).count(".") - (1 if match.group(2) else 0)
        if dots:
            return True
    return False


def _canonical(value: Any) -> Any:
    """``value`` with the keys of every ``str``-keyed ``dict`` sorted, and lists and tuples as lists.

    SurrealDB objects ignore key order, the SDK's CBOR bytes do not; arrays keep their order (a set is sent as a
    sorted array by ``encoding.encode``). Anything else is returned as is.
    """
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _fingerprint(encoder: Callable[[Any], bytes], frame: list[Any], variables: Mapping[str, Any]) -> str:
    """The SHA-256 of ``frame`` + the canonical variables, as one CBOR array. Raises what encoding raises."""
    payload = encoder([*frame, _canonical(_encoding.encode(dict(variables)))])
    return hashlib.sha256(payload).hexdigest()


def statement_key(scope: int, kind: str, statement: str, variables: Mapping[str, Any]) -> str | None:
    """The key of an ORM read: ``["orm", scope, kind, statement, variables]``; ``None`` when it cannot be keyed.

    ``None`` (the read goes to the database) when the installed SDK exposes no CBOR encoder (a warning, once per
    process) or when anything raises, such as a value the SDK cannot send: the read raises its own error then.
    """
    encoder = cbor_encode
    if encoder is None:
        QueryCache._warn_no_encoder()
        return None
    try:
        return _fingerprint(encoder, ["orm", scope, kind, statement], variables)
    except Exception:
        return None


#: A copy that raised (``_begin_read``).
_FAILED: Final = object()


def _require_str(method: str, name: str, value: object) -> None:
    """Refuse a ``value`` that is not a ``str`` (``TypeError``)."""
    if not isinstance(value, str):
        raise TypeError(f"QueryCache.{method}() {name} must be a str, not {type(value).__name__}")


class QueryCache:
    """The query cache (v0.28.0): a class-level singleton, as in the full SurrealDB-ORM.

    ``QuerySet.cache()`` stores and serves its reads here; every ORM write invalidates the reads it can change.
    The public class methods are the full ORM's: ``configure()``, ``make_key()``, ``get()``, ``set()``,
    ``invalidate()``, ``clear()``, ``stats()``. Defaults: ``default_ttl`` 60 s, ``max_size`` 1000, enabled.
    """

    # Class state, declared before the methods: the classmethod ``set`` below shadows the builtin in this body,
    # so every set here is spelled ``builtins.set``.
    _lock: ClassVar[threading.Lock] = threading.Lock()
    _default_ttl: ClassVar[float] = 60.0
    _max_size: ClassVar[int] = 1000
    _enabled: ClassVar[bool] = True
    _entries: ClassVar[OrderedDict[str, _Entry]] = OrderedDict()  # least recently used first
    _by_table: ClassVar[dict[str, builtins.set[str]]] = {}
    _by_scope: ClassVar[dict[int, builtins.set[str]]] = {}
    _any_table: ClassVar[builtins.set[str]] = builtins.set()
    _generation: ClassVar[dict[str, int]] = {}
    _in_flight: ClassVar[dict[str, int]] = {}
    _in_flight_all: ClassVar[int] = 0
    _writes: ClassVar[int] = 0  # completed writes, any table
    _epoch: ClassVar[int] = 0  # bumped by clear(), configure(enabled=False) and every-table writes
    _live_scopes: ClassVar[builtins.set[int]] = builtins.set()
    _scope_counter: ClassVar[Iterator[int]] = itertools.count(1)
    _hits: ClassVar[int] = 0
    _misses: ClassVar[int] = 0
    _warned_no_encoder: ClassVar[bool] = False

    # --- public API (the full ORM's names) ----------------------------------------------------------------

    @classmethod
    def configure(
        cls,
        *,
        default_ttl: float | timedelta = 60,
        max_size: int = 1000,
        enabled: bool = True,
    ) -> None:
        """Set the cache's three settings, as the full SurrealDB-ORM does: an argument left out takes its default.

        ``default_ttl``: the TTL of a ``cache()`` read without one, in seconds or a ``timedelta`` (``0``: such reads
        are never cached). ``max_size``: the most entries kept; lowering it evicts the least recently
        used entries at once. ``enabled=False`` hides the entries without removing them: ``get()`` returns ``None``,
        ``set()`` stores nothing, ``cache()`` reads go to the database, and nothing is counted; ``enabled=True``
        serves again the entries still valid. Writes, expiry and closed connections keep removing entries while
        the cache is disabled. Never clears the cache (``clear()`` does). Every argument is checked before any
        change.

        Raises:
            TypeError: ``default_ttl`` is not a number of seconds or a ``timedelta``, ``max_size`` is not an
                ``int``, ``enabled`` is not a ``bool``.
            ValueError: ``default_ttl`` is negative or not finite, ``max_size`` is below 1.
        """
        seconds = ttl_seconds(default_ttl, "QueryCache.configure() default_ttl")
        if isinstance(max_size, bool) or not isinstance(max_size, numbers.Integral):
            raise TypeError(f"QueryCache.configure() max_size must be an int, not {type(max_size).__name__}")
        if max_size < 1:
            raise ValueError(f"QueryCache.configure() max_size must be >= 1, got {max_size!r}")
        if not isinstance(enabled, bool):
            raise TypeError(f"QueryCache.configure() enabled must be a bool, not {type(enabled).__name__}")
        with cls._guard():
            cls._default_ttl = seconds
            cls._max_size = int(max_size)
            if not enabled:
                cls._epoch += 1  # a read begun before stores nothing, even after a re-enable
            cls._enabled = enabled
            cls._evict_to(cls._max_size)

    @classmethod
    def make_key(cls, query: str, variables: Mapping[str, Any], table: str) -> str:
        """A deterministic key (hex SHA-256) for caching your own results with ``get()``/``set()``.

        Built from the table, the statement and the variables as the SDK would send them (its CBOR encoding), with
        the keys of every mapping sorted (SurrealDB objects ignore key order, as the full ORM's ``sort_keys=True``
        does): two keys are equal only when the requests are equivalent. Raises ``TypeError`` for a non-``str``
        query/table or a non-mapping ``variables``, or a value the SDK cannot send; ``SurrealDbError`` when the
        installed SDK exposes no CBOR encoder.
        """
        _require_str("make_key", "query", query)
        if not isinstance(variables, Mapping):
            raise TypeError(f"QueryCache.make_key() variables must be a mapping, not {type(variables).__name__}")
        _require_str("make_key", "table", table)
        encoder = cbor_encode
        if encoder is None:
            raise SurrealDbError(_C9)
        try:
            return _fingerprint(encoder, ["user", table, query], variables)
        except Exception as exc:
            raise TypeError(f"QueryCache.make_key(): the variables hold a value the SDK cannot send ({exc})") from exc

    @classmethod
    def get(cls, key: str) -> Any | None:
        """A copy of the value cached under ``key``; ``None`` when missing, expired or the cache is disabled.

        Also ``None`` (a miss) while an ORM write to the entry's table runs, from its start to its end (its
        ``pre_*``, ``around_*`` and ``post_*`` handlers included), as a ``cache()`` read of that table then goes to
        the database: the write removes the entry when it ends, with any entry ``set()`` meanwhile. Raises
        ``TypeError`` for a ``key`` that is not a ``str``."""
        _require_str("get", "key", key)
        with cls._guard():
            if not cls._enabled:
                return None
            entry = cls._entries.get(key)
            if entry is None or _clock() >= entry.expires_at:
                if entry is not None:
                    cls._remove(key)
                cls._misses += 1
                return None
            if cls._busy(entry.tables, entry.any_table):
                cls._misses += 1
                return None
            cls._entries.move_to_end(key)
            cls._hits += 1
            data = entry.data
        return _deepcopy(data)

    @classmethod
    def set(cls, key: str, data: Any, table: str, ttl: float | timedelta | None = None) -> None:
        """Cache a copy of ``data`` under ``key`` for ``ttl`` seconds (default: ``default_ttl``), tagged with
        ``table`` so that ``invalidate()`` and the ORM's writes to that table remove it. No-op when disabled; a
        ``ttl`` of ``0`` stores nothing and removes the key's previous entry (as the full ORM's already-expired
        entry). Keys you build are yours: include whatever tells your callers apart (an entry set here is not scoped
        to an identity). Raises ``TypeError`` or ``ValueError`` on ``ttl`` (as
        ``configure()`` on ``default_ttl``), ``TypeError`` on a ``key`` or ``table`` that is not a ``str``."""
        _require_str("set", "key", key)
        _require_str("set", "table", table)
        seconds = None if ttl is None else ttl_seconds(ttl, "QueryCache.set() ttl")
        with cls._guard():
            if not cls._enabled:
                return
            if seconds is None:
                seconds = cls._default_ttl
            if seconds == 0:
                cls._remove(key)
                return
        stored = _deepcopy(data)
        with cls._guard():
            if not cls._enabled:
                return
            now = _clock()
            cls._store(key, _Entry(stored, frozenset({table}), False, None, now, now + seconds))

    @classmethod
    def invalidate(cls, model: type[BaseSurrealModel] | str) -> int:
        """Remove the cached entries of a model's table (or of the table named), and every cached read that
        follows links (``fetch()``, a dotted path); return how many entries were removed. A read of that table
        running meanwhile is not stored. Raises ``TypeError`` for anything but a model class or a ``str``."""
        from .model_base import BaseSurrealModel

        if isinstance(model, str):
            table = model
        elif isinstance(model, type) and issubclass(model, BaseSurrealModel):
            table = model.get_table_name()
        else:
            raise TypeError(f"QueryCache.invalidate() takes a model class or a table name, not {type(model).__name__}")
        with cls._guard():
            before = len(cls._entries)
            cls._invalidate_tables(frozenset({table}))
            return before - len(cls._entries)

    @classmethod
    def clear(cls) -> None:
        """Remove every entry and reset the ``hits``/``misses`` counters. A read running meanwhile is not stored.

        Call it after changing a connection's session behind the ORM's back (``raw_query("USE …")``, ``signin()``
        on the SDK client): the cache cannot see that change."""
        with cls._guard():
            cls._drop_all()
            cls._epoch += 1
            cls._hits = 0
            cls._misses = 0

    @classmethod
    def stats(cls) -> dict[str, Any]:
        """``{"entries", "tables", "max_size", "default_ttl", "enabled", "hits", "misses"}``.

        ``entries``: unexpired entries (expired ones are dropped first), hidden ones included while the cache is
        disabled; ``tables``: distinct tables they are tagged with (the internal ``EDGES`` tag not counted);
        ``default_ttl``: seconds (``float``); ``hits``: ``cache()`` reads and ``get()`` calls answered from the
        cache; ``misses``: those that were not (a ``cache()`` read outside a transaction that went to the
        database, ``cache(0)`` included). Nothing is counted while the cache is disabled. Both counters since
        start-up or ``clear()``.
        """
        with cls._guard():
            cls._drop_expired(_clock())
            return {
                "entries": len(cls._entries),
                "tables": len(cls._by_table) - (1 if EDGES in cls._by_table else 0),
                "max_size": cls._max_size,
                "default_ttl": cls._default_ttl,
                "enabled": cls._enabled,
                "hits": cls._hits,
                "misses": cls._misses,
            }

    # --- reads (QuerySet) ---------------------------------------------------------------------------------

    @classmethod
    def _default(cls) -> float:
        """The default TTL, read when a ``cache()`` read without one runs."""
        return cls._default_ttl

    @classmethod
    def _count_miss(cls) -> None:
        """Count a ``cache()`` read that went to the database without a lookup (no key, or a TTL of ``0``)."""
        with cls._guard():
            if cls._enabled:
                cls._misses += 1

    @classmethod
    def _begin_read(
        cls, key: str, tables: frozenset[str], any_table: bool, scope: int, ttl: float
    ) -> tuple[bool, Any, _Ticket | None]:
        """Look a read up: ``(True, copy, None)`` on a hit, ``(False, None, ticket)`` on a miss that may store its
        result, ``(False, None, None)`` when it must go to the database and store nothing. Never raises.

        Disabled: bypass, nothing counted. A retired scope, or a write in flight on what the read depends on: a
        miss, bypassed. A hit needs an entry younger than both its own expiry and the reader's ``ttl``.
        """
        try:
            with cls._guard():
                if not cls._enabled:
                    return False, None, None
                if scope not in cls._live_scopes or cls._busy(tables, any_table):
                    cls._misses += 1
                    return False, None, None
                now = _clock()
                entry = cls._entries.get(key)
                if entry is not None and now >= entry.expires_at:
                    cls._remove(key)
                    entry = None
                if entry is None or now - entry.stored_at >= ttl:
                    cls._misses += 1
                    ticket = _Ticket(
                        key=key,
                        tables=tables,
                        any_table=any_table,
                        scope=scope,
                        ttl=ttl,
                        epoch=cls._epoch,
                        generations=cls._generations_of(tables),
                        writes=cls._writes,
                    )
                    return False, None, ticket
                cls._entries.move_to_end(key)
                data = entry.data
        except Exception:
            return False, None, None
        try:
            copied = _deepcopy(data)
        except Exception:
            copied = _FAILED
        try:
            with cls._guard():
                if copied is _FAILED:
                    if cls._entries.get(key) is entry:
                        cls._remove(key)
                    if cls._enabled:
                        cls._misses += 1
                    return False, None, None
                if cls._enabled:
                    cls._hits += 1
        except Exception:
            return False, None, None
        return True, copied, None

    @classmethod
    def _finish_read(cls, ticket: _Ticket, data: Any) -> None:
        """Store a copy of a miss's result, unless anything its ticket depends on moved. Never raises."""
        try:
            stored = _deepcopy(data)
        except Exception:
            return
        try:
            with cls._guard():
                if (
                    not cls._enabled
                    or ticket.epoch != cls._epoch
                    or ticket.scope not in cls._live_scopes
                    or ticket.generations != cls._generations_of(ticket.tables)
                    or (ticket.any_table and ticket.writes != cls._writes)
                    or cls._busy(ticket.tables, ticket.any_table)
                ):
                    return
                now = _clock()
                cls._store(ticket.key, _Entry(stored, ticket.tables, ticket.any_table, ticket.scope, now, now + ticket.ttl))
        except Exception:
            return

    # --- writes -------------------------------------------------------------------------------------------

    @classmethod
    def _write_started(cls, tables: frozenset[str] | None) -> frozenset[str] | None:
        """Put ``tables`` (``None``: every table) in flight; return the token :meth:`_write_finished` takes."""
        with cls._guard():
            if tables is None:
                cls._in_flight_all += 1
            else:
                for table in tables:
                    cls._in_flight[table] = cls._in_flight.get(table, 0) + 1
        return tables

    @classmethod
    def _write_finished(cls, token: frozenset[str] | None) -> None:
        """End a write: its tables leave flight, their generations move and their entries go, with every entry
        that follows links; ``None`` (every table) empties the cache and refuses every read begun before."""
        with cls._guard():
            if token is None:
                cls._in_flight_all = max(0, cls._in_flight_all - 1)
                cls._epoch += 1
                cls._drop_all()
                cls._writes += 1
                return
            for table in token:
                count = cls._in_flight.get(table, 0) - 1
                if count > 0:
                    cls._in_flight[table] = count
                else:
                    cls._in_flight.pop(table, None)
            cls._invalidate_tables(token)

    # --- scopes -------------------------------------------------------------------------------------------

    @classmethod
    def _new_scope(cls, previous: int | None) -> int:
        """Retire ``previous`` (if any) and return a new live scope."""
        with cls._guard():
            return cls._mint_scope(previous)

    @classmethod
    def _retire_scope(cls, scope: int) -> None:
        """Take ``scope`` out of the live scopes and remove its entries (idempotent)."""
        with cls._guard():
            cls._retire(scope)

    # --- locked helpers (the caller holds the lock) -------------------------------------------------------

    @classmethod
    @contextmanager
    def _guard(cls) -> Iterator[None]:
        """Hold the lock, after retiring the scopes of the clients collected since the last locked section."""
        with cls._lock:
            while _retired:  # only the lock holder pops; a finalizer only appends
                cls._retire(_retired.popleft())
            yield

    @classmethod
    def _warn_no_encoder(cls) -> None:
        """Log the missing CBOR encoder once per process (``clear()`` keeps the flag)."""
        with cls._guard():
            if cls._warned_no_encoder:
                return
            cls._warned_no_encoder = True
        logger.warning(_W2)

    @classmethod
    def _busy(cls, tables: frozenset[str], any_table: bool) -> bool:
        """Whether a write in flight concerns a read of ``tables`` (any write, for a read that follows links)."""
        if cls._in_flight_all:
            return True
        if any_table:
            return bool(cls._in_flight)
        return any(table in cls._in_flight for table in tables)

    @classmethod
    def _generations_of(cls, tables: frozenset[str]) -> tuple[int, ...]:
        return tuple(cls._generation.get(table, 0) for table in sorted(tables))

    @classmethod
    def _invalidate_tables(cls, tables: frozenset[str]) -> None:
        """Move the generations of ``tables``, remove their entries and every entry that follows links."""
        for table in tables:
            cls._generation[table] = cls._generation.get(table, 0) + 1
            for key in list(cls._by_table.get(table, ())):
                cls._remove(key)
        for key in list(cls._any_table):
            cls._remove(key)
        cls._writes += 1

    @classmethod
    def _store(cls, key: str, entry: _Entry) -> None:
        """Store ``entry`` under ``key``, replacing its previous entry, making room at ``max_size``."""
        cls._remove(key)
        cls._evict_to(cls._max_size - 1)
        cls._entries[key] = entry
        for table in entry.tables:
            cls._by_table.setdefault(table, builtins.set()).add(key)
        if entry.scope is not None:
            cls._by_scope.setdefault(entry.scope, builtins.set()).add(key)
        if entry.any_table:
            cls._any_table.add(key)

    @classmethod
    def _remove(cls, key: str) -> None:
        """Remove ``key``'s entry and its index lines (no entry: nothing)."""
        entry = cls._entries.pop(key, None)
        if entry is None:
            return
        for table in entry.tables:
            _discard(cls._by_table, table, key)
        if entry.scope is not None:
            _discard(cls._by_scope, entry.scope, key)
        cls._any_table.discard(key)

    @classmethod
    def _drop_all(cls) -> None:
        cls._entries.clear()
        cls._by_table.clear()
        cls._by_scope.clear()
        cls._any_table.clear()

    @classmethod
    def _drop_expired(cls, now: float) -> None:
        for key in [key for key, entry in cls._entries.items() if now >= entry.expires_at]:
            cls._remove(key)

    @classmethod
    def _evict_to(cls, limit: int) -> None:
        """Keep at most ``limit`` entries: expired ones go first, then the least recently used."""
        if len(cls._entries) <= limit:
            return
        cls._drop_expired(_clock())
        while len(cls._entries) > limit:
            cls._remove(next(iter(cls._entries)))

    @classmethod
    def _mint_scope(cls, previous: int | None) -> int:
        if previous is not None:
            cls._retire(previous)
        scope = next(cls._scope_counter)
        cls._live_scopes.add(scope)
        return scope

    @classmethod
    def _retire(cls, scope: int) -> None:
        cls._live_scopes.discard(scope)
        for key in list(cls._by_scope.get(scope, ())):
            cls._remove(key)


def _discard(index: dict[Any, set[str]], name: Any, key: str) -> None:
    """Remove ``key`` from ``index[name]``, and the line once empty."""
    keys = index.get(name)
    if keys is None:
        return
    keys.discard(key)
    if not keys:
        del index[name]


# --- connection scopes (the manager) --------------------------------------------------------------------------


def _scope_collected(cell: _ScopeCell) -> None:
    """Finalizer of a stamped client: queue its scope for retirement. Never takes the lock (the garbage collector
    may run it on a thread that holds it)."""
    _retired.append(cell.scope)


def bind_scope(client: Any) -> int | None:
    """Mint ``client``'s cache scope, retiring its previous one: called at every identity stamp.

    A client that cannot be weakly keyed (``1``, an unhashable object) gets no scope and mints none: ``None``, its
    reads are never cached. A collected client's scope is retired by the next locked section.
    """
    with QueryCache._guard():
        try:
            cell = _client_scopes.get(client)
        except TypeError:
            return None
        if cell is not None:
            cell.scope = QueryCache._mint_scope(cell.scope)
            return cell.scope
        cell = _ScopeCell(QueryCache._mint_scope(None))
        _client_scopes[client] = cell  # the lookup above refused every client that cannot be weakly keyed
        cell.finalizer = weakref.finalize(client, _scope_collected, cell)
        cell.finalizer.atexit = False
        return cell.scope


def client_scope(client: Any) -> int | None:
    """``client``'s live cache scope; ``None`` when the manager never stamped it (or it cannot be weakly keyed)."""
    with QueryCache._guard():
        try:
            cell = _client_scopes.get(client)
        except TypeError:
            return None
        return None if cell is None else cell.scope


def retire_client(client: Any) -> None:
    """Retire ``client``'s scope: its entries leave memory now. No scope: nothing."""
    with QueryCache._guard():
        try:
            cell = _client_scopes.pop(client, None)
        except TypeError:
            return
        if cell is None:
            return
        if cell.finalizer is not None:
            cell.finalizer.detach()
        QueryCache._retire(cell.scope)


# --- writes (the ORM's write methods, transactions) -----------------------------------------------------------


@asynccontextmanager
async def writing(tables: Iterable[str] | None, tx: Any = None) -> AsyncIterator[None]:
    """Around one ORM write of ``tables`` (``None``: every table).

    Outside a transaction, the tables are in flight for the whole block (a cached read of them goes to the database
    and stores nothing), then invalidated when it ends, also on an exception. With a transaction (``tx`` whose class
    has ``_note_cache_write``), the tables are noted on it and invalidated at its commit. No table: nothing.

    The transaction is recognised by its class, never by asking the object (no instance ``__getattr__`` runs), so a
    write refused before it reaches its transaction leaves the object untouched.
    """
    resolved = None if tables is None else frozenset(tables)
    if resolved is not None and not resolved:
        yield
        return
    if tx is not None and callable(getattr(type(tx), "_note_cache_write", None)):
        tx._note_cache_write(resolved)
        yield
        return
    token = QueryCache._write_started(resolved)
    try:
        yield
    finally:
        QueryCache._write_finished(token)


F = TypeVar("F", bound=Callable[..., Coroutine[Any, Any, Any]])

_Of = Literal["self", "cls", "queryset", "table", "edge", "all"]
_RESOLVERS: Final = frozenset({"self", "cls", "queryset", "table", "edge", "all"})
#: The resolvers whose first argument is a model instance (``"self"``, ``"edge"``) or class (``"cls"``).
_MODEL_RESOLVERS: Final = frozenset({"self", "cls", "edge"})


def _resolve(
    of: _Of, first: str, bound: inspect.BoundArguments, has_tx: bool, deletes: bool
) -> tuple[frozenset[str] | None, Any]:
    """The tables a call writes and the transaction it joins; every table when resolving raises."""
    tx: Any = None
    try:
        bound.apply_defaults()
        if has_tx:
            tx = bound.arguments.get("tx")
        if of == "all":
            return None, None
        arg0 = bound.arguments[first]
        if of == "self":
            tables = {type(arg0).get_table_name()}
        elif of == "cls":
            tables = {arg0.get_table_name()}
        elif of == "queryset":
            tables = {arg0._model_table}
            tx = arg0._tx
        elif of == "table":
            tables = {arg0._model_table}
            tx = None
        else:  # "edge"
            tables = set(type(arg0)._edge_names(bound.arguments["edge"]))
        if deletes:
            tables.add(EDGES)
        return frozenset(tables), tx
    except Exception:
        return None, tx


def _note_records(tables: frozenset[str] | None, tx: Any) -> None:
    """Note *tables* (``None``: every table) as written with records on *tx* (issue #304), when its class has
    ``_note_record_write`` — recognised by its class, as :func:`writing` recognises a transaction."""
    if callable(getattr(type(tx), "_note_record_write", None)):
        tx._note_record_write(tables)


def invalidates(of: _Of, *, deletes: bool = False, records: bool = True) -> Callable[[F], F]:
    """Decorate an ORM write method so that it invalidates the cached reads it can change.

    ``of`` says which tables the call writes, from its first argument: ``"self"`` (the instance's model table),
    ``"cls"`` (the class's), ``"queryset"`` (the queryset's table; its ``objects(tx=)`` is the transaction),
    ``"table"`` (the queryset's table, never deferred: the write runs outside the transaction), ``"edge"`` (the edge
    tables named by the ``edge`` argument), ``"all"`` (every table). ``deletes`` adds ``EDGES``. The transaction is
    the call's ``tx`` argument when the method has one. A resolver that raises means every table. Under
    ``@classmethod``, this is the inner decorator. The wrapper keeps the signature, the docstring and
    ``__wrapped__``, sends nothing, and carries ``__surreal_cache_writes__``.

    With a transaction, the tables are also noted on it as written with records (issue #304:
    ``Transaction._note_record_write``, which ``define_indexes(tx=…)`` reads) — unless ``records=False``, which marks
    a method that applies definitions (``define_table()``, ``remove_index()``…) and writes no record: its tables are
    noted for the query cache only.

    The one error of its own (v0.29.0): with ``"self"``, ``"edge"`` or ``"cls"``, a ``tx`` of another connection than
    the model's raises ``SurrealDbError`` (``routing.check_transaction()``) before the write is put in flight or noted
    on the transaction, so a refused write invalidates nothing. The transaction is read statically, never asked. A
    queryset's transaction is checked by ``objects(tx=)``.
    """
    if of not in _RESOLVERS:
        raise ValueError(f"invalidates() takes one of {sorted(_RESOLVERS)}, not {of!r}")

    def decorate(func: F) -> F:
        signature = inspect.signature(func)
        first = next(iter(signature.parameters))
        has_tx = "tx" in signature.parameters

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                bound = signature.bind(*args, **kwargs)
            except TypeError:
                return await func(*args, **kwargs)  # Python raises the function's own error
            tables, tx = _resolve(of, first, bound, has_tx, deletes)
            if tx is not None and of in _MODEL_RESOLVERS and first in bound.arguments:
                arg0 = bound.arguments[first]
                model = arg0 if isinstance(arg0, type) else type(arg0)
                _routing.check_transaction(model, tx, f"{model.__name__}.{func.__name__}()")
            if records and tx is not None:
                _note_records(tables, tx)
            async with writing(tables, tx):
                return await func(*args, **kwargs)

        wrapper.__surreal_cache_writes__ = True  # type: ignore[attr-defined]
        return cast(F, wrapper)

    return decorate
