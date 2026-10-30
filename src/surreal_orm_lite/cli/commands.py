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
import logging
import os
import sys
import urllib.parse
from collections.abc import Callable, Sequence
from typing import Any, NoReturn

from .. import __version__
from ..connection_manager import SurrealDBConnectionManager

__all__ = ["cli", "main"]

_PROG = "surreal-orm-lite"
_DESCRIPTION = "SurrealDB-ORM-lite command line: migrations, schema introspection and a SurrealQL shell."
_DEFAULT_URL = "http://localhost:8000"
_DEFAULT_USER = "root"
_DEFAULT_PASSWORD = "root"
_LOGGER_NAME = "surreal_orm_lite"
_LOOP_ERROR = "Error: surreal-orm-lite cannot run inside a running event loop (Jupyter, an async test); run it in a subprocess."


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


def _err(text: str, settings: _Settings) -> None:
    """Write *text* to stderr, scrubbed (:func:`_scrub`)."""
    print(_scrub(text, settings), file=sys.stderr)


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


def _configure(settings: _Settings, command: str) -> type[SurrealDBConnectionManager]:
    """Register the settings under ``settings.connection`` and return its connection class.

    :raises _UsageError: no namespace or no database.
    :raises _ConfigureError: ``add_connection()`` refused the settings.
    """
    if not (settings.namespace and settings.database):
        raise _UsageError(
            f"{command} needs a namespace and a database: pass --namespace/-n and --database/-d "
            "(or set SURREAL_NAMESPACE and SURREAL_DATABASE)."
        )
    try:
        SurrealDBConnectionManager.add_connection(
            settings.connection,
            url=settings.url,
            user=settings.user,
            password=settings.password,
            namespace=settings.namespace,
            database=settings.database,
        )
        return SurrealDBConnectionManager._manager_for(settings.connection)
    except Exception as exc:
        raise _ConfigureError(str(exc) or type(exc).__name__) from exc


def _cmd_makemigrations(args: argparse.Namespace, settings: _Settings) -> int:
    """``makemigrations``: the database is reached unless ``--empty`` or ``--no-from-db``."""
    if not args.empty and args.from_db:
        _configure(settings, args.command)
    return 0


def _cmd_database(args: argparse.Namespace, settings: _Settings) -> int:
    """A command that reaches the database: ``migrate``, ``upgrade``, ``rollback``, ``status``, ``inspectdb``,
    ``schemadiff``, ``shell``."""
    _configure(settings, args.command)
    return 0


def _cmd_sqlmigrate(args: argparse.Namespace, settings: _Settings) -> int:
    """``sqlmigrate``: offline, no connection."""
    return 0


_COMMANDS: dict[str, Callable[[argparse.Namespace, _Settings], int]] = {
    "makemigrations": _cmd_makemigrations,
    "migrate": _cmd_database,
    "upgrade": _cmd_database,
    "rollback": _cmd_database,
    "status": _cmd_database,
    "sqlmigrate": _cmd_sqlmigrate,
    "inspectdb": _cmd_database,
    "schemadiff": _cmd_database,
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
