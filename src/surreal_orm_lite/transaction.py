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

from typing import Any

from .exceptions import SurrealDbError


class Transaction:
    """Buffers SurrealQL statements to be committed atomically as one query."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.variables: dict[str, Any] = {}
        self._counter: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.statements

    def add(self, statement: str, variables: dict[str, Any] | None = None) -> None:
        """Append a statement, renaming its ``$vars`` with a per-statement prefix.

        Each variable ``$name`` becomes ``$t<N>_name`` to avoid collisions across
        buffered operations.
        """
        prefix = f"t{self._counter}_"
        renamed = statement
        if variables:
            for name, value in variables.items():
                renamed = renamed.replace(f"${name}", f"${prefix}{name}")
                self.variables[f"{prefix}{name}"] = value
        self.statements.append(renamed)
        self._counter += 1

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
        statements = raw.get("result", []) if isinstance(raw, dict) else []
        errors = [s for s in statements if isinstance(s, dict) and s.get("status") == "ERR"]
        if not errors:
            return
        root = next(
            (e for e in errors if (e.get("details") or {}).get("kind") != "NotExecuted"),
            errors[0],
        )
        message = root.get("result") or "transaction failed"
        raise SurrealDbError(f"Transaction failed and rolled back: {message}")
