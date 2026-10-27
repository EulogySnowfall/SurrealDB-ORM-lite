"""Applying and undoing migrations (the full SurrealDB-ORM's ``migrations.executor``).

:class:`MigrationExecutor` works on a migrations directory: its files (``get_available_migrations()``,
``load_migration()``), their application order (``get_pending_migrations()``), the state they build
(``migration_state()``) and the statements of one of them (``show_sql()``) — all pure: no database is read.
``MigrationStatementError`` is raised when SurrealDB refuses a statement of a migration.
"""

import os
from pathlib import Path
from typing import Any, Literal

from ..exceptions import SurrealDbError
from ..routing import check_name
from ..schema_state import SchemaState
from ..search import checked_line
from . import loader
from . import state as _state
from .migration import Migration

__all__ = ["MigrationExecutor", "MigrationStatementError"]

#: The refusal of a client object as ``connection=`` (v0.30.0's rule).
_CLIENT_OBJECT = (
    "connection= takes the name of a registered connection, not a client object: pass the name it was added with "
    "(add_connection()), or leave it None and make the call inside SurrealDBConnectionManager.using(name)."
)


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


class MigrationExecutor:
    """Applies, undoes and inspects the migrations of a directory (the full SurrealDB-ORM's class).

    *migrations_dir*: the directory of the migration files (``<NNNN>_<name>.py``). *connection*: the name of a
    registered connection, or ``None`` for the current one — the innermost ``using()`` block's, else ``"default"`` —,
    resolved at each call that reaches the database. Building an executor reads nothing.

    The migration files are application code: loading them runs them (point an executor at the application's own
    directory only).

    Attributes:
        migrations_dir: The migrations directory.

    :raises TypeError: *connection* is a client object, or neither a ``str`` nor ``None``.
    :raises ValueError: *connection* is ``""``.
    """

    migrations_dir: Path

    def __init__(self, migrations_dir: str | os.PathLike[str], *, connection: str | None = None) -> None:
        if connection is not None and not isinstance(connection, str) and hasattr(connection, "query"):
            raise TypeError(_CLIENT_OBJECT)
        self.migrations_dir = Path(migrations_dir)
        self._connection = None if connection is None else check_name(connection)

    def get_available_migrations(self) -> list[str]:
        """The names of the directory's migration files, sorted: the stems of its ``*.py`` files that do not start
        with ``_`` and that ``parse_migration_name()`` reads (the full ORM's rule); ``[]`` for a missing directory. No
        file is imported."""
        return loader.discover(self.migrations_dir)

    def get_pending_migrations(self, applied: list[str]) -> list[str]:
        """The names of the migration files not in *applied*, in application order (every file is loaded and ordered).

        :raises ValueError: a dependency names no migration file; a dependency cycle; see :meth:`load_migration`.
        """
        recorded = set(applied)
        return [migration.name for migration in self._ordered() if migration.name not in recorded]

    def load_migration(self, name: str) -> Migration:
        """The ``migration`` that ``<migrations_dir>/<name>.py`` defines. The file is imported — run — on each call,
        and not inserted in ``sys.modules``; an error it raises propagates unchanged.

        :raises TypeError: *name* is not a ``str``.
        :raises ValueError: *name* is not a migration name, such as ``"0001_initial"`` (checked before any file is looked
            for); the file defines a migration of another name.
        :raises FileNotFoundError: ``Migration file not found: <path>``.
        :raises ImportError: ``Migration file must define 'migration' variable: <name>`` — also when ``migration`` is
            not a :class:`~surreal_orm_lite.migrations.Migration`.
        """
        return loader.load(self.migrations_dir, name)

    def migration_state(self, target: str | None = None) -> SchemaState:
        """The schema state the migration files build, up to and including *target* in application order (every file
        when ``None``) — pure (lite; Django's project state): no database is read.

        Each migration's operations are replayed in order from an empty state (the **file replay**): a removal of a
        definition no file made is a no-op, since what was defined outside migrations is unknown here. The state's
        ``applied_migrations`` lists the names replayed, in order; its definitions carry no mark.

        :raises ValueError: *target* names no migration file (``Unknown migration target '<t>': no such migration file
            in <dir>.``); see :meth:`get_pending_migrations`.
        """
        migrations = self._ordered()
        if target is not None and all(migration.name != target for migration in migrations):
            raise ValueError(f"Unknown migration target {target!r}: no such migration file in {self.migrations_dir}.")
        return _state.replay(migrations, until=target, strict=False)

    async def show_sql(self, name: str, *, backwards: bool = False, line: Literal[2, 3] = 3) -> str:
        """The statements of the migration *name*, joined by blank lines: ``forwards_sql()``'s, or with
        *backwards* ``backwards_sql()``'s, for the SurrealDB major *line* (3 by default; 2 for SurrealDB 2.x and the
        embedded engine) — pure despite ``async`` (the full ORM's signature): no request.

        The operations render from the files' state before the migration (:meth:`migration_state` of the migrations
        before it), so on line 2 the ``FLEXIBLE`` choices are those of the files, where ``migrate()`` makes them from
        the database's schema.

        :raises TypeError: *line* is not an ``int``; see :meth:`load_migration`.
        :raises ValueError: *line* is neither 2 nor 3; see :meth:`get_pending_migrations`.
        :raises FileNotFoundError: no file of the directory defines *name*.
        """
        checked = checked_line(line)
        path = loader.path_of(self.migrations_dir, name)
        migrations = self._ordered()
        names = [migration.name for migration in migrations]
        if name not in names:
            raise FileNotFoundError(f"Migration file not found: {path}")
        position = names.index(name)
        migration = migrations[position]
        before = _state.replay(migrations[:position], strict=False)
        if backwards:
            return "\n\n".join(migration.backwards_sql(line=checked, state=before))
        return "\n\n".join(migration.forwards_sql(line=checked, state=before))

    def _ordered(self) -> list[Migration]:
        """Every migration file of the directory, loaded, in application order (spec §5.6 "Loading and order")."""
        return loader.load_all(self.migrations_dir)
