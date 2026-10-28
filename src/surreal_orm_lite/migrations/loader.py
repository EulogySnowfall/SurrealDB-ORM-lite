"""The migration files of a directory (v0.38.0) — private: discovery, import, order and the history checks.

- :func:`discover` — the migration names of a directory: the stems of its ``*.py`` files that do not start with ``_``
  and that ``parse_migration_name()`` reads, sorted (the full SurrealDB-ORM's rule).
- :func:`load` — imports ``<directory>/<name>.py`` with ``importlib.util.spec_from_file_location()`` (not inserted in
  ``sys.modules``) and returns its ``migration``. A migration file is application code: importing it runs it (spec §8),
  so a name that is not a migration name is refused before any file is looked for.
- :func:`load_all` — every file of a directory, loaded and ordered.
- :func:`order` — the application order: the topological order of the dependency graph, ties broken by name
  (spec §5.6 "Loading and order"); :func:`leaves` — the migrations no other one depends on.
- :func:`check_history` — the history checks run on the applied names before any write (spec §5.6, D30 as amended,
  C10), in the mode :data:`HISTORY` gives each caller.

Its warnings go to the logger ``surreal_orm_lite.migrations`` and name migrations and the directory only.
"""

import heapq
import importlib.util
import logging
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Literal, NamedTuple

from .migration import _NAME, Migration, parse_migration_name

__all__ = ["HISTORY", "HistoryMode", "check_history", "discover", "leaves", "load", "load_all", "order", "path_of"]

logger = logging.getLogger("surreal_orm_lite.migrations")


class HistoryMode(NamedTuple):
    """How a caller's history checks report what they find (spec §5.6's table).

    Attributes:
        dependency: An applied migration depending on a pending one (E-H1): ``"raise"`` its ``ValueError``, or
            ``"warn"`` (its text logged as a warning).
        unknown: An applied name without a file: ``"raise"`` E-H2 (``ValueError``), ``"warn"`` (the warning
            ``Migration <a> is recorded as applied but has no file in <dir>; it is ignored.``) or ``"missing"``
            (``FileNotFoundError("Migration file not found: <dir>/<a>.py")``).
    """

    dependency: Literal["raise", "warn"]
    unknown: Literal["raise", "warn", "missing"]


#: Each caller's history checks (spec §5.6's table, D30 as amended, C10): the one place a caller reads its mode.
HISTORY: Mapping[str, HistoryMode] = MappingProxyType(
    {
        "makemigrations": HistoryMode("raise", "raise"),  # with from_db=True: the current state is the database's
        "migrate": HistoryMode("raise", "warn"),  # the full ORM ignores an applied name without a file
        "upgrade": HistoryMode("raise", "missing"),  # the full ORM loads every applied migration's file
        "rollback": HistoryMode("raise", "missing"),  # its place in the order, so whether to undo it, is unknown
        "get_migration_status": HistoryMode("warn", "warn"),  # never raises on the history
    }
)


def discover(directory: Path) -> list[str]:
    """The migration names of *directory*, sorted: the stems of its ``*.py`` files that do not start with ``_`` and
    that ``parse_migration_name()`` reads. ``[]`` for a missing directory. No file is imported."""
    names: list[str] = []
    for path in directory.glob("*.py"):
        if path.stem.startswith("_"):
            continue
        try:
            parse_migration_name(path.stem)
        except ValueError:
            continue
        names.append(path.stem)
    return sorted(names)


def path_of(directory: Path, name: str) -> Path:
    """The file of the migration *name* in *directory*, ``<directory>/<name>.py`` — checked first, so that no file
    outside *directory* can be reached: a name that is not a migration name (``0001_initial``) could never be the
    name of the migration its file defines.

    :raises TypeError: *name* is not a ``str``.
    :raises ValueError: *name* is not a migration name (``Migration``'s text).
    """
    if not isinstance(name, str):
        raise TypeError(f"Migration name must be a str, not {type(name).__name__}.")
    if _NAME.fullmatch(name) is None:
        raise ValueError(
            f"Invalid migration name {name!r}: expected digits, an underscore, then letters, digits or underscores, "
            "such as '0001_initial'."
        )
    return directory / f"{name}.py"


def load(directory: Path, name: str) -> Migration:
    """The ``migration`` that ``<directory>/<name>.py`` defines. The file is imported — run — on each call, with
    ``importlib.util.spec_from_file_location()``, and not inserted in ``sys.modules``. An error the file raises
    propagates unchanged.

    :raises TypeError: *name* is not a ``str``.
    :raises ValueError: *name* is not a migration name (checked before any file is looked for); the file defines a
        migration of another name.
    :raises FileNotFoundError: ``Migration file not found: <path>`` (the full ORM's text).
    :raises ImportError: ``Migration file must define 'migration' variable: <name>`` (the full ORM's text) — also when
        ``migration`` is not a ``surreal_orm_lite.migrations.Migration``.
    """
    path = path_of(directory, name)
    if not path.is_file():
        raise FileNotFoundError(f"Migration file not found: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load migration: {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    migration = getattr(module, "migration", None)
    if not isinstance(migration, Migration):
        raise ImportError(f"Migration file must define 'migration' variable: {name}")
    if migration.name != name:
        raise ValueError(
            f"Migration file '{name}.py' defines migration {migration.name!r}; a migration's name must be its file name."
        )
    return migration


def order(migrations: Iterable[Migration], directory: Path) -> list[Migration]:
    """*migrations* — the migrations of the files of *directory*, each name once — in application order: the
    topological order of their dependency graph, the migration first by name among those ready (spec §5.6).

    :raises ValueError: a dependency names no migration of *migrations* (``Migration '<a>' depends on '<b>', which is
        not a migration file of <dir>.``); the graph holds a cycle (``Circular dependency detected in migrations: [...]``,
        the full ORM's text, listing by name the migrations that could not be ordered).
    """
    by_name = {migration.name: migration for migration in migrations}
    waiting: dict[str, set[str]] = {}
    dependents: dict[str, list[str]] = {name: [] for name in by_name}
    for name in sorted(by_name):
        dependencies = set(by_name[name].dependencies)
        for dependency in by_name[name].dependencies:
            if dependency not in by_name:
                raise ValueError(f"Migration {name!r} depends on {dependency!r}, which is not a migration file of {directory}.")
        for dependency in dependencies:
            dependents[dependency].append(name)
        waiting[name] = dependencies
    ready = [name for name, dependencies in waiting.items() if not dependencies]
    heapq.heapify(ready)
    result: list[Migration] = []
    while ready:
        name = heapq.heappop(ready)
        result.append(by_name[name])
        for dependent in dependents[name]:
            waiting[dependent].discard(name)
            if not waiting[dependent]:
                heapq.heappush(ready, dependent)
    if len(result) < len(by_name):
        ordered = {migration.name for migration in result}
        remaining = sorted(name for name in by_name if name not in ordered)
        raise ValueError(f"Circular dependency detected in migrations: {remaining}")
    return result


def load_all(directory: Path) -> list[Migration]:
    """Every migration file of *directory* (:func:`discover`), loaded (:func:`load`), in application order
    (:func:`order`) — before any request: a file that does not load, an unknown dependency or a cycle raises here."""
    return order([load(directory, name) for name in discover(directory)], directory)


def leaves(migrations: Iterable[Migration]) -> list[str]:
    """The names of the migrations of *migrations* that no other one depends on, sorted — the dependencies of the
    next migration written."""
    listed = list(migrations)
    needed = {dependency for migration in listed for dependency in migration.dependencies}
    return sorted(migration.name for migration in listed if migration.name not in needed)


def check_history(
    order: Sequence[Migration],
    applied: Iterable[str],
    directory: Path,
    *,
    dependency: Literal["raise", "warn"],
    unknown: Literal["raise", "warn", "missing"],
) -> None:
    """Check the applied names against the files (spec §5.6, §7 E-H1, E-H2), before any write. *order* is every
    migration of *directory* in application order; *applied* the names the database records.

    First each applied migration, in *order*, whose dependency is not applied (E-H1) — raised, or with
    ``dependency="warn"`` logged as a warning —, then each applied name that names no migration of *order*, as
    *unknown* says (:class:`HistoryMode`). A caller passes its own mode, :data:`HISTORY`'s. Each warning is logged
    once per call.

    :raises ValueError: E-H1 (``dependency="raise"``); E-H2 (``unknown="raise"``).
    :raises FileNotFoundError: an applied name without a file (``unknown="missing"``).
    """
    recorded = list(applied)
    applied_names = set(recorded)
    known = {migration.name for migration in order}
    logged: set[str] = set()

    def warn(text: str) -> None:
        if text not in logged:
            logged.add(text)
            logger.warning("%s", text)

    for migration in order:
        if migration.name not in applied_names:
            continue
        for needed in migration.dependencies:
            if needed in applied_names:
                continue
            text = (
                f"Migration {migration.name!r} is applied but its dependency {needed!r} is not: the database's history "
                f"does not match {directory}."
            )
            if dependency == "raise":
                raise ValueError(text)
            warn(text)
    for name in recorded:
        if name in known:
            continue
        if unknown == "raise":
            raise ValueError(
                f"Migration {name!r} is applied but has no file in {directory}: the database's history does not match "
                f"{directory}."
            )
        if unknown == "missing":
            raise FileNotFoundError(f"Migration file not found: {directory / f'{name}.py'}")
        warn(f"Migration {name} is recorded as applied but has no file in {directory}; it is ignored.")
