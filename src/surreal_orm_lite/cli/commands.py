"""The ``surreal-orm-lite`` command line (v0.39.0): the full SurrealDB-ORM's ``surreal-orm`` CLI on ``argparse``.

The full ORM's nine commands (``makemigrations``, ``migrate``, ``upgrade``, ``rollback``, ``status``, ``sqlmigrate``,
``inspectdb``, ``schemadiff``, ``shell``), global options, ``SURREAL_*`` environment variables, defaults and output
texts, built with the standard library (no ``click``, no extra): ``pip install surreal-orm-lite`` installs a working
``surreal-orm-lite`` script, also run as ``python -m surreal_orm_lite.cli``. Each command is a thin front end over the
lite's public API (v0.38.0's migrations, v0.30.0's ``generate_models_from_db()``).

Global options go before the command. An option wins over its environment variable, a variable over the default; an
empty variable counts as unset. Exit codes: 0 success, 1 the command failed, 2 a usage error, 130 interrupted.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import importlib
import logging
import os
import socket
import sys
import urllib.parse
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, NoReturn, TypeVar

from .. import __version__
from ..connection_manager import SurrealDBConnectionManager
from ..exceptions import SurrealDbAuthenticationError, SurrealDbConnectionError
from ..introspection import generate_models_from_db, schema_diff
from ..migrations import MigrationExecutor, ModelIntrospector, makemigrations, split_destructive

__all__ = ["cli", "main"]

_T = TypeVar("_T")

_PROG = "surreal-orm-lite"
_DESCRIPTION = "SurrealDB-ORM-lite command line: migrations, schema introspection and a SurrealQL shell."
_DEFAULT_URL = "http://localhost:8000"
_DEFAULT_USER = "root"
_DEFAULT_PASSWORD = "root"
_LOGGER_NAME = "surreal_orm_lite"
_NO_MODELS = "No models found. Register models by importing them (--models)."
_LEGEND = "Legend: [X]=applied, R=reversible, D=has data migrations"
_RULE = "-" * 60
_LOOP_ERROR = "Error: surreal-orm-lite cannot run inside a running event loop (Jupyter, an async test); run it in a subprocess."
_FROM_DB_HINT = "Pass --no-from-db to diff against the migration files instead (offline)."
#: The failures of ``makemigrations --from-db`` that come from reaching the database, and only those, get
#: :data:`_FROM_DB_HINT` (spec §5.3 step 7): not a ``PermissionError`` or ``FileNotFoundError`` writing the file.
_CONNECTION_ERRORS: tuple[type[BaseException], ...] = (
    SurrealDbConnectionError,
    SurrealDbAuthenticationError,
    ConnectionError,
    TimeoutError,
    socket.gaierror,
)


class _UsageError(Exception):
    """A usage error found after parsing (a missing namespace or database): reported by ``parser.error()``, exit 2."""


class _ConfigureError(Exception):
    """``add_connection()`` refused the settings: reported as ``Error: <message>``, exit 1."""


@dataclasses.dataclass(frozen=True)
class _Settings:
    """The connection settings and the migrations directory of one run, resolved from the options and the
    environment. The password is kept out of the ``repr``."""

    url: str
    user: str
    password: str = dataclasses.field(repr=False)
    namespace: str | None
    database: str | None
    connection: str
    migrations_dir: str


def _env(name: str) -> str | None:
    """The environment variable *name*, read now; ``None`` when unset or empty."""
    return os.environ.get(name) or None


def _resolve(option: str | None, variable: str, default: str | None) -> str | None:
    """Option > environment variable > default (an option given as ``""`` is kept: it was asked for)."""
    if option is not None:
        return option
    value = _env(variable)
    return value if value is not None else default


def _settings(args: argparse.Namespace) -> _Settings:
    """The :class:`_Settings` of the parsed *args*, with the environment read now."""
    return _Settings(
        url=_resolve(args.url, "SURREAL_URL", _DEFAULT_URL) or "",
        user=_resolve(args.user, "SURREAL_USER", _DEFAULT_USER) or "",
        password=_resolve(args.password, "SURREAL_PASSWORD", _DEFAULT_PASSWORD) or "",
        namespace=_resolve(args.namespace, "SURREAL_NAMESPACE", None),
        database=_resolve(args.database, "SURREAL_DATABASE", None),
        connection=args.connection,
        migrations_dir=args.migrations_dir,
    )


def _parser() -> argparse.ArgumentParser:
    """The command line's parser, built on each :func:`main` call. No help text shows a default taken from the
    environment (no ``%(default)s``): the password never appears in ``--help``. No colour (Python 3.14+)."""
    extra: dict[str, Any] = {}
    if sys.version_info >= (3, 14):
        extra["color"] = False
    parser = argparse.ArgumentParser(prog=_PROG, description=_DESCRIPTION, **extra)
    parser.add_argument("--version", action="version", version=f"{_PROG} {__version__}")
    parser.add_argument(
        "--migrations-dir", "-m", dest="migrations_dir", default="migrations", help="Migrations directory (default: migrations)"
    )
    parser.add_argument(
        "--url",
        "-u",
        dest="url",
        help="SurrealDB URL: ws(s)://host:port/rpc, http(s)://host:port, mem:// or surrealkv://<path> "
        "(env SURREAL_URL; default: http://localhost:8000)",
    )
    parser.add_argument("--namespace", "-n", dest="namespace", help="SurrealDB namespace (env SURREAL_NAMESPACE)")
    parser.add_argument("--database", "-d", dest="database", help="SurrealDB database (env SURREAL_DATABASE)")
    parser.add_argument("--user", dest="user", help="SurrealDB user (env SURREAL_USER; default: root)")
    parser.add_argument(
        "--password",
        dest="password",
        help="SurrealDB password (env SURREAL_PASSWORD, preferred: an option is visible in the process list; default: root)",
    )
    parser.add_argument(
        "--connection",
        dest="connection",
        default="default",
        help="Connection name the settings are registered under and every command uses (default: default)",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name: str, help_line: str) -> argparse.ArgumentParser:
        return commands.add_parser(name, help=help_line, description=help_line)

    makemigrations = add("makemigrations", "Generate migration files from model changes.")
    makemigrations.add_argument("--name", "-n", dest="name", required=True, help="Migration name")
    makemigrations.add_argument("--empty", action="store_true", help="Create empty migration for manual editing")
    makemigrations.add_argument(
        "--models", "-m", dest="models", action="append", metavar="MODULE", help="Model module to import (repeatable)"
    )
    makemigrations.add_argument(
        "--from-db",
        dest="from_db",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Diff against the live database schema (the default); --no-from-db diffs against the migration files (offline)",
    )
    makemigrations.add_argument(
        "--drop-missing",
        action="store_true",
        help="Also emit the irreversible removals (REMOVE TABLE/FIELD/ANALYZER/...) for objects that have no model",
    )

    migrate = add("migrate", "Apply pending schema migrations.")
    migrate.add_argument("--target", "-t", dest="target", help="Target migration name")
    migrate.add_argument("--fake", action="store_true", help="Mark as applied without executing")

    upgrade = add("upgrade", "Apply data migrations to transform records.")
    upgrade.add_argument("--target", "-t", dest="target", help="Target migration name")

    rollback = add("rollback", "Rollback migrations to TARGET.")
    rollback.add_argument("target", metavar="TARGET", help='Last migration to keep ("" to undo every migration)')

    add("status", "Show migration status.")

    sqlmigrate = add("sqlmigrate", "Show SQL for MIGRATION without executing.")
    sqlmigrate.add_argument("migration", metavar="MIGRATION", help="Migration name")
    sqlmigrate.add_argument("--backwards", action="store_true", help="Show the SQL that undoes the migration")
    sqlmigrate.add_argument(
        "--line", type=int, choices=(2, 3), default=3, help="SurrealDB line the SQL is rendered for (default: 3)"
    )

    inspectdb = add("inspectdb", "Generate Python models from existing database tables.")
    inspectdb.add_argument("--output", "-o", dest="output", help="Output file path (default: print to stdout)")
    inspectdb.add_argument("--table", dest="tables", action="append", metavar="NAME", help="Table to read (repeatable)")
    inspectdb.add_argument("--no-overwrite", action="store_true", help="Fail when the output file exists")

    schemadiff = add("schemadiff", "Compare Python models against the live database schema.")
    schemadiff.add_argument(
        "--models", "-m", dest="models", action="append", metavar="MODULE", help="Model module to import (repeatable)"
    )

    add("shell", "Start an interactive SurrealDB shell.")
    return parser


def _redact(url: str) -> str:
    """*url* without its ``user:password@`` part; a URL that does not parse is returned as is."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    if "@" not in parts.netloc:
        return url
    return urllib.parse.urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))


def _scrub(text: str, settings: _Settings) -> str:
    """*text* with every occurrence of the URL in use replaced by :func:`_redact`'s form when it carries a
    ``userinfo`` part, so no password reaches the terminal through a message that quotes the URL."""
    redacted = _redact(settings.url)
    if redacted == settings.url:
        return text
    for form in (settings.url, repr(settings.url)[1:-1]):
        text = text.replace(form, redacted)
    return text


def _echo(text: str) -> None:
    """Write *text* to stdout."""
    print(text)


def _err(text: str, settings: _Settings) -> None:
    """Write *text* to stderr, scrubbed (:func:`_scrub`)."""
    print(_scrub(text, settings), file=sys.stderr)


def _message(exc: BaseException) -> str:
    """``str(exc)``, or the exception's class name when that is empty."""
    return str(exc) or type(exc).__name__


def _fail(prefix: str, exc: BaseException, settings: _Settings) -> int:
    """Report a failed command as ``<prefix> failed: <message>`` on stderr (scrubbed) and return the exit status 1."""
    _err(f"{prefix} failed: {_message(exc)}", settings)
    return 1


class _LogHandler(logging.Handler):
    """For the duration of :func:`main`: the ``surreal_orm_lite`` logger's ``WARNING`` and ``ERROR`` records on stderr
    (resolved at emit time) as ``Warning: <message>`` / ``Error: <message>``, scrubbed."""

    def __init__(self, settings: _Settings) -> None:
        super().__init__(level=logging.WARNING)
        self._settings = settings

    def emit(self, record: logging.LogRecord) -> None:
        try:
            prefix = "Error" if record.levelno >= logging.ERROR else "Warning"
            _err(f"{prefix}: {record.getMessage()}", self._settings)
        except Exception:  # pragma: no cover - logging's own error path
            self.handleError(record)


def _in_running_loop() -> bool:
    """Whether an event loop is running in this thread (``main()`` then refuses: each command runs ``asyncio.run()``)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _require_database(settings: _Settings, command: str) -> tuple[str, str]:
    """The settings' namespace and database (a command that reaches the database needs both).

    :raises _UsageError: no namespace or no database.
    """
    if not (settings.namespace and settings.database):
        raise _UsageError(
            f"{command} needs a namespace and a database: pass --namespace/-n and --database/-d "
            "(or set SURREAL_NAMESPACE and SURREAL_DATABASE)."
        )
    return settings.namespace, settings.database


def _configure(settings: _Settings, command: str) -> type[SurrealDBConnectionManager]:
    """Register the settings under ``settings.connection`` and return its connection class.

    :raises _UsageError: no namespace or no database.
    :raises _ConfigureError: ``add_connection()`` refused the settings.
    """
    namespace, database = _require_database(settings, command)
    try:
        SurrealDBConnectionManager.add_connection(
            settings.connection,
            url=settings.url,
            user=settings.user,
            password=settings.password,
            namespace=namespace,
            database=database,
        )
        return SurrealDBConnectionManager._manager_for(settings.connection)
    except Exception as exc:
        raise _ConfigureError(_message(exc)) from exc


def _run(coro_factory: Callable[[], Awaitable[_T]], manager: type[SurrealDBConnectionManager]) -> _T:
    """Run ``coro_factory()`` in one ``asyncio.run()`` whose last step, in a ``finally``, closes the connection
    *manager* — once per command, also when the command fails or is interrupted."""

    async def run() -> _T:
        try:
            return await coro_factory()
        finally:
            await manager.close_connection()

    return asyncio.run(run())


def _echo_names(header: str, names: Sequence[str]) -> None:
    """Print *header* then one ``  - <name>`` line per name."""
    _echo(header)
    for name in names:
        _echo(f"  - {name}")


class _ImportFailure(Exception):
    """A ``--models`` module raised at import: reported as ``Error importing <module>: <message>``, exit 1."""

    def __init__(self, module: str, cause: Exception) -> None:
        super().__init__(f"Error importing {module}: {_message(cause)}")


def _import_models(modules: Sequence[str]) -> None:
    """Import each of *modules*, in order (their models then register themselves). Before the first import, the
    working directory is inserted at ``sys.path[0]`` when neither it nor ``""`` is already there: a console script's
    ``sys.path[0]`` is its ``bin/`` directory, where ``python -m`` puts the working directory itself.

    :raises _ImportFailure: an import raised an ``Exception``.
    """
    if not modules:
        return
    cwd = os.getcwd()
    if cwd not in sys.path and "" not in sys.path:
        sys.path.insert(0, cwd)
    for module in modules:
        try:
            importlib.import_module(module)
        except Exception as exc:
            raise _ImportFailure(module, exc) from exc


def _load_models(modules: Sequence[str], settings: _Settings) -> bool:
    """Import the ``--models`` *modules* and check that the connection has a registered model; report a failure on
    stderr and return ``False`` (spec §5.3 steps 2–3)."""
    try:
        _import_models(modules)
    except _ImportFailure as exc:
        _err(str(exc), settings)
        return False
    if not ModelIntrospector(None, connection=settings.connection).models:
        _err(_NO_MODELS, settings)
        return False
    return True


def _cmd_makemigrations(args: argparse.Namespace, settings: _Settings) -> int:
    """``makemigrations``: the next migration file of the migrations directory (spec §5.3).

    ``--empty`` writes a file without operations, to fill in by hand. Otherwise the ``--models`` modules are imported
    and every registered model of the connection is diffed — by default against the database's schema (the
    connection is configured once the models are found), with ``--no-from-db`` against the migration files' state,
    offline. The removals left out without ``--drop-missing`` are named by ``makemigrations()``'s warning, shown on
    stderr. A failure to reach the database adds a hint towards ``--no-from-db``.
    """
    from_db = not args.empty and args.from_db
    if from_db:
        _require_database(settings, args.command)
    directory = settings.migrations_dir
    if args.empty:
        try:
            path = asyncio.run(makemigrations(args.name, migrations_dir=directory, empty=True))
        except Exception as exc:
            return _fail("makemigrations", exc, settings)
        _echo(f"Created empty migration: {path}")
        return 0
    if not _load_models(args.models or [], settings):
        return 1

    async def generate() -> Path | None:
        return await makemigrations(
            args.name,
            migrations_dir=directory,
            connection=settings.connection,
            from_db=from_db,
            drop_missing=args.drop_missing,
        )

    try:
        path = _run(generate, _configure(settings, args.command)) if from_db else asyncio.run(generate())
        if path is None:
            _echo("No changes detected.")
            return 0
        operations = MigrationExecutor(directory).load_migration(path.stem).operations
    except Exception as exc:
        status = _fail("makemigrations", exc, settings)
        if from_db and isinstance(exc, _CONNECTION_ERRORS):
            _err(_FROM_DB_HINT, settings)
        return status
    _echo(f"Created migration: {path}")
    _echo(f"Operations: {len(operations)}")
    for operation in operations:
        _echo(f"  - {operation.describe()}")
    return 0


def _executor(settings: _Settings) -> MigrationExecutor:
    """The migrations directory's executor, on the command's connection."""
    return MigrationExecutor(settings.migrations_dir, connection=settings.connection)


def _cmd_migrate(args: argparse.Namespace, settings: _Settings) -> int:
    """``migrate``: apply the pending migrations, up to ``--target`` — schema only (data operations: ``upgrade``);
    ``--fake`` records them without running them (spec §5.4)."""
    manager = _configure(settings, args.command)
    executor = _executor(settings)
    try:
        applied = _run(lambda: executor.migrate(target=args.target, fake=args.fake, schema_only=True), manager)
    except Exception as exc:
        return _fail("Migration", exc, settings)
    if applied:
        _echo_names(f"Applied {len(applied)} migration(s):", applied)
    else:
        _echo("No migrations to apply.")
    return 0


def _cmd_upgrade(args: argparse.Namespace, settings: _Settings) -> int:
    """``upgrade``: run the data operations of the applied migrations, up to ``--target`` (spec §5.5)."""
    manager = _configure(settings, args.command)
    executor = _executor(settings)
    try:
        upgraded = _run(lambda: executor.upgrade(target=args.target), manager)
    except Exception as exc:
        return _fail("Upgrade", exc, settings)
    if upgraded:
        _echo_names(f"Applied data migrations for {len(upgraded)} migration(s):", upgraded)
    else:
        _echo("No data migrations to apply.")
    return 0


def _cmd_rollback(args: argparse.Namespace, settings: _Settings) -> int:
    """``rollback``: undo the applied migrations after ``TARGET`` (``""``: every one), newest first (spec §5.6)."""
    manager = _configure(settings, args.command)
    executor = _executor(settings)
    try:
        undone = _run(lambda: executor.rollback(args.target), manager)
    except Exception as exc:
        return _fail("Rollback", exc, settings)
    if undone:
        _echo_names(f"Rolled back {len(undone)} migration(s):", undone)
    else:
        _echo("No migrations to rollback.")
    return 0


def _cmd_status(args: argparse.Namespace, settings: _Settings) -> int:
    """``status``: every migration file, applied or not, in the full ORM's layout (spec §5.7). Defines the tracking
    table when it is missing."""
    manager = _configure(settings, args.command)
    executor = _executor(settings)
    try:
        status = _run(executor.get_migration_status, manager)
    except Exception as exc:
        return _fail("Status", exc, settings)
    if not status:
        _echo("No migrations found.")
        return 0
    _echo("Migration status:")
    _echo(_RULE)
    for name, info in status.items():
        applied = "[X]" if info["applied"] else "[ ]"
        reversible = "R" if info["reversible"] else "-"
        has_data = "D" if info["has_data"] else "-"
        _echo(f"{applied} {name} ({info['operations']} ops) [{reversible}{has_data}]")
    _echo(_RULE)
    _echo(_LEGEND)
    return 0


def _cmd_schemadiff(args: argparse.Namespace, settings: _Settings) -> int:
    """``schemadiff``: what separates the models of the connection from the database's schema, read-only (spec
    §5.10) — the reversible differences, then the removals ``makemigrations`` writes only with ``--drop-missing``.
    Exit 0 whatever the differences. With no registered model it refuses before configuring the connection: every
    table of the database would otherwise be listed as a removal."""
    _require_database(settings, args.command)
    if not _load_models(args.models or [], settings):
        return 1
    manager = _configure(settings, args.command)
    try:
        operations = _run(lambda: schema_diff(connection=settings.connection), manager)
    except Exception as exc:
        return _fail("Schema diff", exc, settings)
    reversible, destructive = split_destructive(operations)
    if reversible:
        _echo_names(f"Found {len(reversible)} difference(s):", [operation.describe() for operation in reversible])
    elif not destructive:
        _echo("Models and database are in sync.")
    if destructive:
        _echo_names(
            f"{len(destructive)} irreversible operation(s) with no model to justify them:",
            [operation.describe() for operation in destructive],
        )
        _echo("makemigrations writes these only with --drop-missing.")
    return 0


def _cmd_inspectdb(args: argparse.Namespace, settings: _Settings) -> int:
    """``inspectdb``: the models of the database's tables — ``generate_models_from_db()`` on the command's connection
    (spec §5.9). With ``-o`` the module is written (``--no-overwrite``: an existing file is refused, left as it was);
    without, the source goes to stdout with one trailing newline. ``--table`` (repeatable) reads those tables only;
    a named ``--connection`` puts ``connection="<name>"`` in the generated models."""
    manager = _configure(settings, args.command)
    try:
        source = _run(
            lambda: generate_models_from_db(
                args.output, settings.connection, tables=args.tables, overwrite=not args.no_overwrite
            ),
            manager,
        )
    except Exception as exc:
        _err(f"Introspection failed: {_message(exc)}", settings)
        return 1
    if args.output is not None:
        _echo(f"Generated models written to: {args.output}")
    else:
        print(source, end="" if source.endswith("\n") else "\n")
    return 0


def _cmd_database(args: argparse.Namespace, settings: _Settings) -> int:
    """A command that reaches the database and is not written yet: ``shell``."""
    _configure(settings, args.command)
    return 0


def _cmd_sqlmigrate(args: argparse.Namespace, settings: _Settings) -> int:
    """``sqlmigrate``: the statements of a migration, rendered for ``--line`` (``--backwards``: those that undo it) —
    ``MigrationExecutor.show_sql()``, offline, no connection."""
    executor = MigrationExecutor(settings.migrations_dir)
    try:
        sql = asyncio.run(executor.show_sql(args.migration, backwards=args.backwards, line=args.line))
    except FileNotFoundError:
        _err(f"Migration not found: {args.migration}", settings)
        return 1
    except Exception as exc:
        _err(f"Error: {_message(exc)}", settings)
        return 1
    _echo(sql)
    return 0


_COMMANDS: dict[str, Callable[[argparse.Namespace, _Settings], int]] = {
    "makemigrations": _cmd_makemigrations,
    "migrate": _cmd_migrate,
    "upgrade": _cmd_upgrade,
    "rollback": _cmd_rollback,
    "status": _cmd_status,
    "sqlmigrate": _cmd_sqlmigrate,
    "inspectdb": _cmd_inspectdb,
    "schemadiff": _cmd_schemadiff,
    "shell": _cmd_database,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``surreal-orm-lite`` command line on *argv* (``sys.argv[1:]`` when ``None``) and return its exit
    status — the console script's entry point (the full ORM's ``main()``, which returns ``None`` and exits through
    ``click``). Never calls ``sys.exit()`` itself, except through ``argparse`` for ``--help``, ``--version`` and a
    usage error (``SystemExit`` with status 0, 0 and 2). Not callable from a running event loop (each command runs
    its own ``asyncio.run()``): there, once the arguments are parsed and before any connection is configured, it
    prints ``Error: surreal-orm-lite cannot run inside a running event loop (Jupyter, an async test); run it in a
    subprocess.`` on stderr and returns 1 — the connection registry is left untouched.

    Configures the connection it is given — ``add_connection(<--connection>, …)`` — for the process: called
    in-process, it replaces that connection's settings, as the full ORM's CLI calls ``set_connection()``.
    """
    parser = _parser()
    args = parser.parse_args(None if argv is None else list(argv))
    settings = _settings(args)
    if _in_running_loop():
        _err(_LOOP_ERROR, settings)
        return 1
    logger = logging.getLogger(_LOGGER_NAME)
    handler = _LogHandler(settings)
    logger.addHandler(handler)
    try:
        return _COMMANDS[args.command](args, settings)
    except _UsageError as exc:
        parser.error(str(exc))
    except _ConfigureError as exc:
        _err(f"Error: {exc}", settings)
        return 1
    except KeyboardInterrupt:
        _err("Interrupted.", settings)
        return 130
    finally:
        logger.removeHandler(handler)


def cli(args: Sequence[str] | None = None) -> NoReturn:
    """The full ORM's ``cli`` name: ``raise SystemExit(main(args))`` — as calling the full ORM's ``click`` group
    exits."""
    raise SystemExit(main(args))
