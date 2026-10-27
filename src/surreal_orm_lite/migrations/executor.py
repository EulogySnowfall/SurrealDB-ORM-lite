"""Applying and undoing migrations (the full SurrealDB-ORM's ``migrations.executor``).

``MigrationStatementError`` is raised when SurrealDB refuses a statement of a migration.
"""

from typing import Any

from ..exceptions import SurrealDbError


def _rebuild_statement_error(
    cls: type["MigrationStatementError"],
    message: str,
    migration: str,
    operation: int | None,
    description: str | None,
    analyzers_restored: bool | None,
) -> "MigrationStatementError":
    """Rebuild a pickled :class:`MigrationStatementError` (its keyword-only arguments included)."""
    return cls(
        message,
        migration=migration,
        operation=operation,
        description=description,
        analyzers_restored=analyzers_restored,
    )


class MigrationStatementError(SurrealDbError, RuntimeError):
    """SurrealDB refused a statement of a migration, or its tracking record.

    Caught by ``except SurrealDbError`` (the lite's errors) and by ``except RuntimeError`` (the full SurrealDB-ORM's
    base for this error). The message names the migration, the operation and the server's reason; it never quotes
    the statement.

    Attributes:
        migration: The migration's name.
        operation: The failing operation's position in the migration (1-based), or ``None`` when no operation is to
            blame (the tracking record, a batch refused as a whole).
        description: That operation's ``describe()`` text, or ``None``.
        analyzers_restored: Whether the analyzer definitions the migration committed before its transaction were
            restored after the failure; ``None`` when it committed none.
    """

    def __init__(
        self,
        message: str,
        *,
        migration: str,
        operation: int | None = None,
        description: str | None = None,
        analyzers_restored: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.migration = migration
        self.operation = operation
        self.description = description
        self.analyzers_restored = analyzers_restored

    def __reduce__(self) -> tuple[Any, ...]:
        """Pickle with the keyword-only arguments (``Exception.__reduce__`` would call ``cls(*args)`` and fail)."""
        arguments = (type(self), self.args[0], self.migration, self.operation, self.description, self.analyzers_restored)
        return _rebuild_statement_error, arguments, self.__dict__
