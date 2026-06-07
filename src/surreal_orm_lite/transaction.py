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
"""

import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

from .exceptions import SurrealDbError


class Transaction(ABC):
    """Base transaction: shared post-commit machinery + the call-site interface.

    Call sites use ``await tx.add(sql, vars)`` for writes and
    ``await tx.run_read(sql, vars)`` for reads, and never care which concrete strategy
    is in use. ``is_interactive`` lets the few read-dependent paths (auto-id save, merge
    refresh, refresh) adapt.
    """

    def __init__(self) -> None:
        self._post_commit: list[Callable[[], Awaitable[None]]] = []

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
        root = next(
            (e for e in errors if (e.get("details") or {}).get("kind") != "NotExecuted"),
            errors[0],
        )
        message = root.get("result") or "transaction failed"
        raise SurrealDbError(f"Transaction failed and rolled back: {message}")


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
        if self.is_empty:
            return
        # query_raw (NOT query): query() returns None on a failed tx; query_raw gives
        # per-statement status for raise_for_status.
        raw = await self._client.query_raw(self.build_query(), self.variables)
        self.raise_for_status(raw)

    async def cancel(self) -> None:
        # Nothing was sent (statements buffered) → rollback is free.
        self.statements = []
        self.variables = {}
