# src/surreal_orm_lite/transaction.py
"""ORM-level transactions for the official SurrealDB SDK 2.0.

The official SDK is single-connection and exposes no transaction object, so a
``Transaction`` buffers each operation's SurrealQL statement (with namespaced bound
variables) and flushes them as a single ``BEGIN TRANSACTION; …; COMMIT TRANSACTION;``
query. A failing statement rolls back the whole batch (verified empirically on
SurrealDB 2.6.x and 3.1); an exception before flush sends nothing at all.

IMPORTANT: the SDK's ``query()`` returns ``None`` (does NOT raise) when a transaction
fails, so commits must use ``query_raw()`` and inspect every statement's ``status`` via
``raise_for_status()``.
"""

import re
from collections.abc import Awaitable, Callable
from typing import Any

from .exceptions import SurrealDbError


class Transaction:
    """Buffers SurrealQL statements to be committed atomically as one query.

    Record ids (e.g. ``CREATE {record_id} CONTENT $data``) are interpolated into
    statements via ``str(record_id)``; only the data payload is bound through
    ``$vars``. Caller-supplied ids are therefore trusted to be either a native
    ``RecordID`` (whose ``__str__`` produces SurrealQL-escaped ``Table:⟨…⟩``) or
    an already-validated string. This matches the non-tx path, which passes the
    same ``RecordID`` object straight to the SDK.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.variables: dict[str, Any] = {}
        self._counter: int = 0
        self._post_commit: list[Callable[[], Awaitable[None]]] = []

    @property
    def is_empty(self) -> bool:
        return not self.statements

    def add(self, statement: str, variables: dict[str, Any] | None = None) -> None:
        """Append a statement, renaming its ``$vars`` with a per-statement prefix.

        Each variable ``$name`` becomes ``$t<N>_name`` to avoid collisions across
        buffered operations. The rename matches ``$name`` only at a word boundary, so a
        name that is a prefix of another (e.g. ``$id`` vs ``$identity``) is not corrupted.

        Note: only the ``variables`` payload is bound through ``$vars``; any record id
        inlined into ``statement`` is the caller's responsibility (see class docstring).
        """
        prefix = f"t{self._counter}_"
        renamed = statement
        if variables:
            for name, value in variables.items():
                renamed = re.sub(rf"\${re.escape(name)}\b", f"${prefix}{name}", renamed)
                self.variables[f"{prefix}{name}"] = value
        self.statements.append(renamed)
        self._counter += 1

    def enqueue_post_commit(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Enqueue an async callback to run after a successful commit.

        Used by model_base to defer ``post_*`` signals so they only fire when the
        write is actually durable. If the tx body raises, or the commit itself
        rolls back, queued callbacks are discarded.
        """
        self._post_commit.append(callback)

    async def fire_post_commit(self) -> None:
        """Invoke every enqueued post-commit callback in insertion order.

        The first callback that raises propagates the exception; remaining callbacks
        are not invoked. Errors from a handler do NOT undo the commit (the write is
        already durable); they surface as a normal exception from the ``async with``
        block so the caller can react.
        """
        for cb in self._post_commit:
            await cb()

    def build_query(self) -> str:
        """Return the batched ``BEGIN … COMMIT`` query string."""
        if self.is_empty:
            raise ValueError("Cannot commit an empty transaction.")
        body = "\n".join(self.statements)
        return f"BEGIN TRANSACTION;\n{body}\nCOMMIT TRANSACTION;"

    @staticmethod
    def raise_for_status(raw: Any) -> None:
        """Inspect a ``query_raw()`` response and raise if the transaction failed.

        The SDK's ``query()`` silently returns ``None`` on a failed transaction, so the
        commit path uses ``query_raw()`` (full per-statement response) and calls this.
        Any statement with ``status == "ERR"`` means the transaction rolled back. The
        raised message prefers the root cause (the first ERR whose ``details.kind`` is
        not ``"NotExecuted"``), falling back to the first ERR otherwise.
        """
        # Defensive: refuse to declare success on a response shape we don't recognise.
        # If query_raw ever returns an envelope without "result" (e.g. a top-level error
        # on a server version we haven't tested), treating "no errors found" as success
        # would silently report a failed tx as committed.
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
