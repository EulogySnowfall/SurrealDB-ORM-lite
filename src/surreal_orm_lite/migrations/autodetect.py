"""``makemigrations()`` (v0.38.0, lite): the full SurrealDB-ORM's ``makemigrations`` command as a coroutine.

It writes the next migration file of a directory: the operations that turn the current state into the state the models
describe (``SchemaState.diff()``), depending on every leaf of the files' dependency graph. The current state is the
files' — ``MigrationExecutor(migrations_dir).migration_state()``, no request — with ``from_db=False``; reading it from
the database (``from_db=True``, the default, the full ORM's ``--from-db``) comes with ``schema_diff()``.

Logs (logger ``surreal_orm_lite.migrations``, names and ``describe()`` texts only): the removals left out without
``drop_missing=True`` (a warning) and ``No changes detected.``.
"""

import logging
import os
from collections.abc import Iterable
from pathlib import Path

from ..connection_manager import SurrealDBConnectionManager
from ..model_base import BaseSurrealModel
from . import loader
from . import state as _state
from .executor import MigrationExecutor
from .generator import MigrationGenerator, generate_empty_migration
from .introspector import ModelIntrospector
from .operations import split_destructive

__all__ = ["makemigrations"]

logger = logging.getLogger("surreal_orm_lite.migrations")


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
    4. The current state: with ``from_db=False``, the files' (``MigrationExecutor.migration_state()``: their operations
       replayed, a removal of a definition no file made a no-op) — no request, as Django's ``makemigrations``.
    5. Its diff against the models' state. Without *drop_missing*, the removals (``split_destructive()``: a definition
       the current state holds and no model declares) are left out and named in one warning; with it, they are
       written — offline, a removal is relative to the files: a model deleted since the last migration.
    6. The file, ``<NNNN>_<name>.py`` (``MigrationGenerator.generate()``), depending on every leaf of the files' graph.

    A second call with the same models writes nothing: the first file is part of the current state.

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
        *connection* is ``""``; a dependency names no migration file; a dependency cycle; a file that does not load
        (see ``MigrationExecutor.load_migration()``); *name* holds no letter or digit.
    :raises NotImplementedError: ``from_db=True`` without *empty* (not available yet: pass ``from_db=False``).
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
        raise NotImplementedError("makemigrations(from_db=True) is not available yet: pass from_db=False.")
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
