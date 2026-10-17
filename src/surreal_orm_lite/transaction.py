# src/surreal_orm_lite/transaction.py
"""ORM-level transactions for the official SurrealDB SDK 2.0.

Two strategies share one public interface:

- ``BufferedTransaction`` (HTTP, and WebSocket on SurrealDB 2.6.x): buffers each
  operation's SurrealQL statement and flushes them as a single ``BEGIN … COMMIT`` query
  at commit. Reads inside the transaction are impossible and raise. This is the v0.8.0
  model.
- ``InteractiveTransaction`` (WebSocket on SurrealDB 3.x): uses the SDK's NATIVE
  transaction API (``client.begin() -> txn_id``, then ``txn_id=`` on every operation,
  then ``client.commit(txn_id)`` / ``client.cancel(txn_id)``). Reads see uncommitted
  writes; the ``txn_id`` isolates the transaction on the shared connection.

``connection_manager.transaction()`` probes ``client.begin()`` to pick the strategy.

Query cache (v0.28.0): an ORM write that joins a transaction notes the tables it writes on it
(``_note_cache_write``); the commit puts them in flight and invalidates their cached reads when
it returns or raises (a failed commit's state is unknown). ``cancel()`` forgets them: nothing
became visible.
"""

import contextlib
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from . import cache as _cache
from . import encoding as _encoding
from .exceptions import SurrealDbConflictError, SurrealDbError

_NOT_EXECUTED_FILLER = "The query was not executed due to a failed transaction"


def _is_pure_filler(result: Any) -> bool:
    """True if a statement's message is ONLY the generic 'not executed' filler.

    On a failed transaction SurrealDB tags every skipped statement with this
    message; the real cause is a different statement. A 2.6.x conflict, by
    contrast, appends the cause to the same string, so it is NOT pure filler.
    """
    return str(result or "").strip().rstrip(".") == _NOT_EXECUTED_FILLER


def _as_conflict(exc: Exception) -> Exception:
    """Return a SurrealDbConflictError if exc is a retryable conflict, else exc itself."""
    from .concurrency import is_conflict_error

    if is_conflict_error(exc):
        return SurrealDbConflictError(str(exc))
    return exc


class Transaction(ABC):
    """Base transaction: shared post-commit machinery + the call-site interface.

    Call sites use ``await tx.add(sql, vars)`` for writes and
    ``await tx.run_read(sql, vars)`` for reads, and never care which concrete strategy
    is in use. ``is_interactive`` lets the few read-dependent paths (auto-id save, merge
    refresh, refresh) adapt.
    """

    #: The settings generation of the connection the transaction was opened on (v0.29.0), set by
    #: ``SurrealDBConnectionManager.transaction()``: a connection removed or registered again with other
    #: settings while the block is open is cancelled at its exit instead of committing.
    _connection_generation: int | None = None

    def __init__(self) -> None:
        self._post_commit: list[Callable[[], Awaitable[None]]] = []
        # Query cache: the tables written in this transaction, invalidated at commit.
        self._cache_tables: set[str] = set()
        self._cache_all = False

    def _note_cache_write(self, tables: Iterable[str] | None) -> None:
        """Note the tables an ORM write in this transaction changes (``None``: every table).

        ``cache.writing()`` calls it instead of invalidating at once: the rows only become
        visible at commit. ``cache.EDGES`` is noted like a table (a delete in the transaction
        invalidates the edge reads at commit).
        """
        if tables is None:
            self._cache_all = True
        else:
            self._cache_tables |= set(tables)

    def _cache_written(self) -> frozenset[str] | None:
        """The tables noted so far: ``None`` when a write noted every table."""
        return None if self._cache_all else frozenset(self._cache_tables)

    def _forget_cache_writes(self) -> None:
        """Clear the tables noted (after a commit, or on cancel without invalidating)."""
        self._cache_tables.clear()
        self._cache_all = False

    @property
    @abstractmethod
    def is_interactive(self) -> bool:
        """True for the native interactive strategy (reads supported)."""
        ...

    @abstractmethod
    async def add(self, statement: str, variables: dict[str, Any] | None = None) -> Any:
        """Register a write. Buffered: queue (returns None). Interactive: run now
        (returns the statement's result rows)."""
        ...

    @abstractmethod
    async def run_read(self, statement: str, variables: dict[str, Any] | None = None) -> Any:
        """Run a read and return rows. Buffered: raises (reads need WS + SurrealDB 3.x)."""
        ...

    @abstractmethod
    async def commit(self) -> None: ...

    @abstractmethod
    async def cancel(self) -> None: ...

    def enqueue_post_commit(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Enqueue an async callback to run after a successful commit (deferred signals)."""
        self._post_commit.append(callback)

    async def fire_post_commit(self) -> None:
        """Invoke every enqueued post-commit callback in insertion order (FIFO).

        The first callback that raises propagates; the rest are NOT invoked. A raised
        exception does NOT undo the commit (the write is already durable).
        """
        for cb in self._post_commit:
            await cb()

    @staticmethod
    def raise_for_status(raw: Any) -> None:
        """Inspect a ``query_raw()`` response and raise if any statement failed.

        ``query()`` silently returns ``None`` on a failed transaction, so the
        commit/write paths use ``query_raw()`` and call this. Any statement with
        ``status == "ERR"`` means the transaction rolled back. Prefers the root cause
        (the first ERR whose ``details.kind`` is not ``"NotExecuted"``).
        """
        if not isinstance(raw, dict) or not isinstance(raw.get("result"), list):
            raise SurrealDbError(f"Transaction failed: unrecognized query_raw() response shape: {raw!r}")
        statements = raw["result"]
        errors = [s for s in statements if isinstance(s, dict) and s.get("status") == "ERR"]
        if not errors:
            return
        # Prefer a real error (kind != NotExecuted, and not the generic "not executed"
        # filler); else the first ERR whose message is not that filler (the real cause is
        # sometimes a NotExecuted statement, e.g. "Cannot COMMIT: Transaction conflict ...",
        # and some servers omit `details` on filler statements); else the first ERR.
        root = (
            next(
                (
                    e
                    for e in errors
                    if (e.get("details") or {}).get("kind") != "NotExecuted" and not _is_pure_filler(e.get("result"))
                ),
                None,
            )
            or next((e for e in errors if not _is_pure_filler(e.get("result"))), None)
            or errors[0]
        )
        message = root.get("result") or "transaction failed"
        full = f"Transaction failed and rolled back: {message}"
        # Type retryable conflicts so callers / retry_on_conflict can catch them.
        if isinstance(_as_conflict(SurrealDbError(full)), SurrealDbConflictError):
            raise SurrealDbConflictError(full)
        raise SurrealDbError(full)


class BufferedTransaction(Transaction):
    """HTTP / WS-2.6.x strategy (v0.8.0): buffer statements, flush as one BEGIN…COMMIT.

    Requires a shared client at commit time, injected by the connection manager. Record
    ids inlined into statements are the caller's responsibility (see save()).
    """

    def __init__(self, client: Any = None) -> None:
        super().__init__()
        self._client = client
        self.statements: list[str] = []
        self.variables: dict[str, Any] = {}
        self._counter: int = 0

    @property
    def is_interactive(self) -> bool:
        return False

    @property
    def is_empty(self) -> bool:
        return not self.statements

    async def add(self, statement: str, variables: dict[str, Any] | None = None) -> None:
        """Append a statement, renaming its ``$vars`` with a per-statement prefix.

        Each ``$name`` becomes ``$t<N>_name`` (matched at a word boundary so a name that
        is a prefix of another is not corrupted). Returns None: nothing executes until
        commit, so buffered writes cannot return rows.
        """
        variables = _encoding.encode(variables)
        prefix = f"t{self._counter}_"
        renamed = statement
        if variables:
            for name, value in variables.items():
                renamed = re.sub(rf"\${re.escape(name)}\b", f"${prefix}{name}", renamed)
                self.variables[f"{prefix}{name}"] = value
        self.statements.append(renamed)
        self._counter += 1
        return None

    async def run_read(self, statement: str, variables: dict[str, Any] | None = None) -> Any:
        raise SurrealDbError(
            "Reads inside a transaction require a WebSocket connection to SurrealDB 3.x "
            "(native interactive transactions); the current transaction is buffered "
            "(HTTP or SurrealDB 2.6.x)."
        )

    def build_query(self) -> str:
        """Return the batched ``BEGIN … COMMIT`` query string."""
        if self.is_empty:
            raise ValueError("Cannot commit an empty transaction.")
        body = "\n".join(self.statements)
        return f"BEGIN TRANSACTION;\n{body}\nCOMMIT TRANSACTION;"

    async def commit(self) -> None:
        # The tables noted are in flight during the commit and invalidated when it returns
        # or raises (a failed commit's state is unknown).
        try:
            async with _cache.writing(self._cache_written()):
                if self.is_empty:
                    return
                # query_raw (NOT query): query() returns None on a failed tx; query_raw gives
                # per-statement status for raise_for_status.
                raw = await self._client.query_raw(self.build_query(), _encoding.encode(self.variables))
                self.raise_for_status(raw)
        finally:
            self._forget_cache_writes()

    async def cancel(self) -> None:
        # Nothing was sent (statements buffered) → rollback is free; nothing became visible,
        # so the tables noted are forgotten, not invalidated.
        self._forget_cache_writes()
        self.statements = []
        self.variables = {}


class InteractiveTransaction(Transaction):
    """WebSocket / SurrealDB 3.x strategy using the SDK's native transaction API.

    Built with ``(client, txn_id)`` where ``txn_id`` comes from ``client.begin()``
    (done by the connection manager). Every operation passes ``txn_id=`` so it joins the
    server-side transaction; reads see uncommitted writes and are isolated from other
    operations on the same connection.
    """

    def __init__(self, client: Any, txn_id: Any) -> None:
        super().__init__()
        self._client = client
        self._txn_id = txn_id

    @property
    def is_interactive(self) -> bool:
        return True

    async def add(self, statement: str, variables: dict[str, Any] | None = None) -> Any:
        """Run a write inside the transaction; inspect status; return its rows."""
        payload = _encoding.encode(variables or {})
        try:
            raw = await self._client.query_raw(statement, payload, txn_id=self._txn_id)
        except Exception as exc:
            raise _as_conflict(exc) from exc
        self.raise_for_status(raw)
        statements = raw["result"]
        return statements[-1]["result"] if statements else []

    async def run_read(self, statement: str, variables: dict[str, Any] | None = None) -> Any:
        """Run a read inside the transaction and return rows directly."""
        payload = _encoding.encode(variables or {})
        try:
            return await self._client.query(statement, payload, txn_id=self._txn_id)
        except Exception as exc:
            raise _as_conflict(exc) from exc

    async def commit(self) -> None:
        # The tables noted are in flight during the commit and invalidated when it returns
        # or raises (a failed commit's state is unknown).
        try:
            async with _cache.writing(self._cache_written()):
                try:
                    await self._client.commit(self._txn_id)
                except Exception as exc:
                    raise _as_conflict(exc) from exc
        finally:
            self._forget_cache_writes()

    async def cancel(self) -> None:
        # Nothing became visible: the tables noted are forgotten, not invalidated.
        self._forget_cache_writes()
        with contextlib.suppress(Exception):
            await self._client.cancel(self._txn_id)
