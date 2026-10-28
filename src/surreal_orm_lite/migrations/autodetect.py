"""``makemigrations()`` (v0.38.0, lite): the full SurrealDB-ORM's ``makemigrations`` command as a coroutine.

It writes the next migration file of a directory: the operations that turn the current state into the state the models
describe (``SchemaState.diff()``), depending on every leaf of the files' dependency graph. The current state is, by
default (``from_db=True``, the full ORM's ``--from-db``), the database's — ``DatabaseIntrospector`` — with the pending
files replayed over it strictly; with ``from_db=False``, the files' — ``MigrationExecutor(migrations_dir)
.migration_state()``, no request.

Logs (logger ``surreal_orm_lite.migrations``, names and ``describe()`` texts only): the removals left out without
``drop_missing=True`` (a warning) and ``No changes detected.``.
"""

import logging
import os
from collections.abc import Iterable
from pathlib import Path

from ..connection_manager import SurrealDBConnectionManager
from ..exceptions import SurrealDbError
from ..introspection import DatabaseIntrospector
from ..model_base import BaseSurrealModel
from ..schema_state import SchemaState
from . import loader
from . import state as _state
from .constants import MIGRATIONS_TABLE
from .executor import MigrationExecutor
from .generator import MigrationGenerator, generate_empty_migration
from .introspector import ModelIntrospector
from .migration import Migration
from .operations import split_destructive

__all__ = ["makemigrations"]

logger = logging.getLogger("surreal_orm_lite.migrations")

#: The issue ``DatabaseIntrospector`` reports when the tracking table is listed but its records could not be read.
_APPLIED_UNREAD = "could not read the applied migrations"


async def makemigrations(
    name: str,
    *,
    migrations_dir: str | os.PathLike[str] = "migrations",
    models: Iterable[type[BaseSurrealModel]] | None = None,
    connection: str | None = None,
    empty: bool = False,
    from_db: bool = True,
    drop_missing: bool = False,
) -> Path | None:
    """Write the next migration of *migrations_dir* from the models, and return its path — ``None`` when the models
    match the current state (logged ``No changes detected.``). The full ORM's ``makemigrations`` command.

    In order (spec §5.10):

    1. *empty*: a migration without operations, to fill in by hand (a ``RawSQL``, a ``DataMigration``), depending on
       every leaf of the files' graph — no model is read and no request is sent.
    2. The models of *connection* (the current one when ``None``: the innermost ``using()`` block's, else
       ``"default"``) among *models* — every registered model (``get_registered_models()``) when ``None``.
    3. Every migration file loaded and ordered, before any request.
    4. The current state. With ``from_db=True`` (the default): the database's schema (``DatabaseIntrospector``) — the
       definitions the applied migrations, the appliers (``define_table()`` …) and hand-written statements made —,
       the history checked (an applied migration whose dependency is not applied, an applied name without a file:
       ``ValueError``), then the **pending** files replayed over it in application order, as SurrealDB would apply
       them: a creation or an alteration replaces the definition the database holds, a table definition keeps the
       table's fields, indexes and events; a removal of a definition the database does not hold raises (SurrealDB
       would refuse it, so ``migrate()`` could not apply that file either). With ``from_db=False``: the files'
       (``MigrationExecutor.migration_state()``: their operations replayed, a removal of a definition no file made a
       no-op) — no request, as Django's ``makemigrations``.
    5. Its diff against the models' state. Without *drop_missing*, the removals (``split_destructive()``: a definition
       the current state holds and no model declares) are left out and named in one warning; with it, they are
       written — offline, a removal is relative to the files: a model deleted since the last migration.
    6. The file, ``<NNNN>_<name>.py`` (``MigrationGenerator.generate()``), depending on every leaf of the files' graph.

    A second call with the same models writes nothing, even before ``migrate()``: the first file is part of the current
    state (pending, it is replayed). With ``from_db=True`` the clauses set on the database outside migrations — and
    that no model declares — are kept by the operations written.

    :param name: Free text for the file's name (``"Add Email"`` → ``0002_add_email.py``).
    :param migrations_dir: The migrations directory, created with its ``__init__.py`` when a file is written.
    :param models: The model classes, any iterable of them.
    :param connection: A connection name, or ``None`` for the current one.
    :param empty: Write a migration without operations.
    :param from_db: Read the current state from the database (the default) rather than from the files.
    :param drop_missing: Also write the removals.
    :raises TypeError: *name* is not a ``str``; *models* is a ``str`` or holds something that is not a model class;
        *connection* is a client object or neither a ``str`` nor ``None``.
    :raises ValueError: no model found (``No models found: pass models=, or import the modules that define them.``);
        *connection* is ``""`` or not registered; a dependency names no migration file; a dependency cycle; a file that
        does not load (see ``MigrationExecutor.load_migration()``); *name* holds no letter or digit. With
        ``from_db=True``, before anything is written: an applied migration whose dependency is not applied, an applied
        name without a file (the database's history does not match the directory), a pending removal of a definition
        the database does not hold.
    :raises SurrealDbError: with ``from_db=True``, the tracking table is listed but its records could not be read
        (``makemigrations() could not read the applied migrations from _surreal_orm_migrations; nothing was
        written.``); what ``DatabaseIntrospector.introspect()`` raises (``SurrealDbAuthenticationError`` …).
    """
    if not isinstance(name, str):
        raise TypeError(f"makemigrations(): name must be a str, not {type(name).__name__}.")
    executor = MigrationExecutor(migrations_dir, connection=connection)
    if empty:
        return generate_empty_migration(migrations_dir, name, loader.leaves(loader.load_all(executor.migrations_dir)))
    current_connection = connection
    if current_connection is None:
        current_connection = SurrealDBConnectionManager.get_active_connection_name() or "default"
    introspector = ModelIntrospector(models, connection=current_connection)
    if not introspector.models:
        raise ValueError("No models found: pass models=, or import the modules that define them.")
    target = introspector.introspect()
    migrations = loader.load_all(executor.migrations_dir)
    if from_db:
        current = await _database_state(executor, migrations, current_connection)
    else:
        current = _state.replay(migrations, strict=False)
    operations = current.diff(target)
    if not drop_missing:
        operations, skipped = split_destructive(operations)
        if skipped:
            logger.warning(
                "Skipped %d removal(s) with no model to justify them: %s; pass drop_missing=True to write them.",
                len(skipped),
                ", ".join(operation.describe() for operation in skipped),
            )
    if not operations:
        logger.info("No changes detected.")
        return None
    return MigrationGenerator(migrations_dir).generate(name, operations, loader.leaves(migrations))


async def _database_state(executor: MigrationExecutor, migrations: list[Migration], connection: str) -> SchemaState:
    """The current state of ``makemigrations(from_db=True)`` (spec §5.10 step 4): the database's schema, read once; the
    tracking table's unreadable records refused (E-H3: never "nothing applied", which would replay every file over a
    database that holds them); the history checked (E-H1, E-H2); the pending files replayed over the read strictly
    (E-R1)."""
    database = await DatabaseIntrospector(connection).introspect()
    if any(
        issue.kind == "table" and issue.name == MIGRATIONS_TABLE and issue.message == _APPLIED_UNREAD
        for issue in database.issues
    ):
        raise SurrealDbError(
            f"makemigrations() could not read the applied migrations from {MIGRATIONS_TABLE}; nothing was written."
        )
    mode = loader.HISTORY["makemigrations"]
    loader.check_history(
        migrations, database.applied_migrations, executor.migrations_dir, dependency=mode.dependency, unknown=mode.unknown
    )
    applied = set(database.applied_migrations)
    pending = [migration for migration in migrations if migration.name not in applied]
    return _state.replay(pending, start=database, strict=True)
