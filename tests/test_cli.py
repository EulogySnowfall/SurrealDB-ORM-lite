"""The ``surreal-orm-lite`` command line (v0.39.0): the full SurrealDB-ORM's ``surreal-orm`` CLI on ``argparse``.

``TestParser`` (spec §5.1, §5.2, §5.12; AC-1–AC-7, AC-25, AC-29's offline part): the console script declared in
``pyproject.toml``, ``python -m surreal_orm_lite.cli``, the nine commands and their help, the usage errors (exit 2),
option > environment > default, the password kept out of ``--help`` and of ``repr``, the logging handler removed after
``main()``, and ``main()`` refusing to run inside a running event loop before touching the connection registry.

``TestOffline`` (spec §5.3 steps 1–3, 5–6, §5.8, §6.3; AC-8, AC-9, AC-10, AC-14, AC-27's warning text): the commands
that send no request — ``makemigrations --empty``, ``makemigrations --no-from-db`` with ``--models``, ``sqlmigrate``.

``TestCliE2E`` (spec §5.2, §5.4–§5.7; AC-6, AC-13, AC-15, AC-16, AC-17, AC-23): ``migrate``, ``upgrade``, ``rollback``
and ``status`` on the line under test, over WebSocket and HTTP, each test in a database of its own; the connection
class closed once per command, also on failure; failures reported as ``<Prefix> failed: <message>`` without a traceback.

``TestCliFromDbE2E`` (spec §5.3 steps 4 and 7, §5.10; AC-11, AC-12, AC-12b, AC-19's makemigrations part, AC-30):
``makemigrations`` against the database's schema and ``schemadiff``, over WebSocket and HTTP; the ``--no-from-db``
hint on connection errors only; ``--connection``; no connection configured when no model is registered.

``TestCliInspectdbE2E`` (spec §5.9; AC-18, AC-19's inspectdb part): ``inspectdb`` prints ``generate_models_from_db()``'s
source, writes it with ``-o`` (``--no-overwrite`` refuses an existing file), ``--table`` limits it, a named
``--connection`` shows in the generated models.

``TestShellRendering``, ``TestCliShellE2E``, ``TestCliEmbedded`` (spec §5.11; AC-20, AC-21, AC-22's ``mem://`` part):
the SurrealQL shell — one block per statement as the line answers it, the session's end and exit codes, the connection
closed once, the URL's credentials never printed.

CLI tests are sync ``def`` tests: ``main()`` runs its own ``asyncio.run()`` (spec §9.2).
"""

import asyncio
import dataclasses
import functools
import importlib
import logging
import os
import pprint
import socket
import subprocess
import sys
import textwrap
import tomllib
import uuid
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

import surreal_orm_lite
from surreal_orm_lite import SurrealDBConnectionManager
from surreal_orm_lite._sdk import RecordID
from surreal_orm_lite.cli import commands
from surreal_orm_lite.exceptions import SurrealDbAuthenticationError, SurrealDbConnectionError
from surreal_orm_lite.introspection import generate_models_from_db
from surreal_orm_lite.migrations import MigrationExecutor, introspector
from surreal_orm_lite.time_travel import cached_major
from tests.conftest import _migration_url, new_database_name

_ROOT = Path(__file__).resolve().parent.parent

_ENV_VARS = ("SURREAL_URL", "SURREAL_NAMESPACE", "SURREAL_DATABASE", "SURREAL_USER", "SURREAL_PASSWORD")

#: The nine commands and the help line of each (spec §5.12: the full ORM's docstrings).
_COMMANDS = {
    "makemigrations": "Generate migration files from model changes.",
    "migrate": "Apply pending schema migrations.",
    "upgrade": "Apply data migrations to transform records.",
    "rollback": "Rollback migrations to TARGET.",
    "status": "Show migration status.",
    "sqlmigrate": "Show SQL for MIGRATION without executing.",
    "inspectdb": "Generate Python models from existing database tables.",
    "schemadiff": "Compare Python models against the live database schema.",
    "shell": "Start an interactive SurrealDB shell.",
}

_LOOP_ERROR = "Error: surreal-orm-lite cannot run inside a running event loop (Jupyter, an async test); run it in a subprocess."

_SECRET = "s3cr3t-cli"


def _flat(text: str) -> str:
    """*text* with its whitespace runs collapsed: argparse wraps help lines to the terminal width."""
    return " ".join(text.split())


@pytest.fixture(autouse=True)
def _cli_isolation(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Spec §9.2: the default connection's settings restored after the test, ``sys.path`` a copy (the CLI may insert
    the working directory) and the five ``SURREAL_*`` variables unset (a developer's shell cannot change a default)."""
    config = SurrealDBConnectionManager.get_config("default")
    monkeypatch.setattr(sys, "path", list(sys.path))
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    yield
    if config is None:
        SurrealDBConnectionManager._manager_for("default")._clear_settings()
    else:
        SurrealDBConnectionManager.set_connection(**dataclasses.asdict(config))


class _Recorded(Exception):
    """Raised by the ``add_connection`` recorder once it has recorded its call."""


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """``SurrealDBConnectionManager.add_connection`` replaced by a recorder that raises once it has recorded: the
    command then stops with ``Error: …`` (exit 1) before any connection is opened."""
    calls: list[dict[str, Any]] = []

    def record(name: str, **kwargs: Any) -> None:
        calls.append({"name": name, **kwargs})
        raise _Recorded("recorded")

    monkeypatch.setattr(SurrealDBConnectionManager, "add_connection", record)
    return calls


class TestParser:
    """AC-1–AC-7, AC-25, AC-29 (offline): entry points, parsing, help, usage errors, settings precedence."""

    # -- AC-1, AC-2: entry points and exports ---------------------------------------------------------------------

    def test_pyproject_declares_the_console_script(self) -> None:
        data = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert data["project"]["scripts"] == {"surreal-orm-lite": "surreal_orm_lite.cli.commands:main"}
        # No new runtime dependency (D1: argparse, no click).
        assert data["project"]["dependencies"] == ["pydantic>=2.13.4", "surrealdb[pydantic]>=2.0.0,<3.0.0"]

    def test_exports(self) -> None:
        cli_package = importlib.import_module("surreal_orm_lite.cli")
        assert cli_package.__all__ == ["cli", "main"]
        assert cli_package.main is commands.main
        assert cli_package.cli is commands.cli

    def test_importing_the_package_leaves_the_cli_out(self) -> None:
        code = "import sys, surreal_orm_lite; print('surreal_orm_lite.cli' in sys.modules)"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=_ROOT)
        assert result.stdout.strip() == "False"

    def test_python_dash_m_version(self) -> None:
        env = {key: value for key, value in os.environ.items() if key not in _ENV_VARS}
        result = subprocess.run(
            [sys.executable, "-m", "surreal_orm_lite.cli", "--version"],
            capture_output=True,
            text=True,
            cwd=_ROOT,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == f"surreal-orm-lite {surreal_orm_lite.__version__}"

    def test_importing_dunder_main_runs_nothing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delitem(sys.modules, "surreal_orm_lite.cli.__main__", raising=False)
        monkeypatch.setattr(sys, "argv", ["pytest", "nope"])
        module = importlib.import_module("surreal_orm_lite.cli.__main__")
        assert module.main is commands.main
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", "")

    # -- AC-3: version, help, usage errors -------------------------------------------------------------------------

    def test_version(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            commands.main(["--version"])
        assert exc.value.code == 0
        assert capsys.readouterr().out.strip() == f"surreal-orm-lite {surreal_orm_lite.__version__}"

    def test_top_level_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            commands.main(["--help"])
        assert exc.value.code == 0
        out = _flat(capsys.readouterr().out)
        assert "surreal-orm-lite" in out
        assert "SurrealDB-ORM-lite command line: migrations, schema introspection and a SurrealQL shell." in out
        for option in ("--migrations-dir", "--url", "--namespace", "--database", "--user", "--password", "--connection"):
            assert option in out
        for command, help_line in _COMMANDS.items():
            assert command in out
            assert help_line in out

    @pytest.mark.parametrize("command", list(_COMMANDS))
    def test_command_help(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            commands.main([command, "--help"])
        assert exc.value.code == 0
        out = _flat(capsys.readouterr().out)
        assert f"surreal-orm-lite {command}" in out
        assert _COMMANDS[command] in out

    def test_command_options_in_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        expected = {
            "makemigrations": ("--name", "--empty", "--models", "--from-db", "--no-from-db", "--drop-missing"),
            "migrate": ("--target", "--fake"),
            "upgrade": ("--target",),
            "rollback": ("TARGET",),
            "sqlmigrate": ("MIGRATION", "--backwards", "--line"),
            "inspectdb": ("--output", "--table", "--no-overwrite"),
            "schemadiff": ("--models",),
        }
        for command, options in expected.items():
            with pytest.raises(SystemExit):
                commands.main([command, "--help"])
            out = capsys.readouterr().out
            for option in options:
                assert option in out, (command, option)

    @pytest.mark.parametrize("argv", [[], ["nope"], ["makemigrations"], ["rollback"], ["sqlmigrate", "x", "--line", "4"]])
    def test_usage_errors_exit_2(self, argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            commands.main(argv)
        assert exc.value.code == 2
        assert "surreal-orm-lite" in capsys.readouterr().err

    def test_the_nine_commands_parse(self) -> None:
        parser = commands._parser()
        args = parser.parse_args(
            ["-m", "mig", "-n", "ns", "-d", "db", "makemigrations", "-n", "init", "-m", "a", "-m", "b", "--no-from-db"]
        )
        assert (args.command, args.migrations_dir, args.namespace, args.database) == ("makemigrations", "mig", "ns", "db")
        assert (args.name, args.models, args.from_db, args.empty, args.drop_missing) == (
            "init",
            ["a", "b"],
            False,
            False,
            False,
        )
        args = parser.parse_args(["makemigrations", "-n", "x", "--empty", "--drop-missing"])
        assert (args.from_db, args.empty, args.drop_missing, args.models) == (True, True, True, None)
        args = parser.parse_args(["migrate", "-t", "0001_x", "--fake"])
        assert (args.command, args.target, args.fake) == ("migrate", "0001_x", True)
        args = parser.parse_args(["upgrade"])
        assert (args.command, args.target) == ("upgrade", None)
        args = parser.parse_args(["rollback", ""])
        assert (args.command, args.target) == ("rollback", "")
        assert parser.parse_args(["status"]).command == "status"
        args = parser.parse_args(["sqlmigrate", "0001_init"])
        assert (args.command, args.migration, args.backwards, args.line) == ("sqlmigrate", "0001_init", False, 3)
        args = parser.parse_args(["sqlmigrate", "0001_init", "--backwards", "--line", "2"])
        assert (args.backwards, args.line) == (True, 2)
        args = parser.parse_args(["inspectdb", "-o", "m.py", "--table", "t1", "--table", "t2", "--no-overwrite"])
        assert (args.command, args.output, args.tables, args.no_overwrite) == ("inspectdb", "m.py", ["t1", "t2"], True)
        args = parser.parse_args(["schemadiff", "-m", "app.models"])
        assert (args.command, args.models) == ("schemadiff", ["app.models"])
        assert parser.parse_args(["shell"]).command == "shell"

    def test_no_colour_in_help(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setenv("FORCE_COLOR", "1")
        monkeypatch.delenv("NO_COLOR", raising=False)
        for argv in (["--help"], ["status", "--help"]):
            with pytest.raises(SystemExit):
                commands.main(argv)
            captured = capsys.readouterr()
            assert "\x1b[" not in captured.out + captured.err
        with pytest.raises(SystemExit):
            commands.main(["nope"])
        assert "\x1b[" not in capsys.readouterr().err

    # -- AC-4: option > environment > default -----------------------------------------------------------------------

    def test_defaults(self, recorder: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]) -> None:
        assert commands.main(["-n", "ns", "-d", "db", "status"]) == 1
        assert recorder == [
            {
                "name": "default",
                "url": "http://localhost:8000",
                "user": "root",
                "password": "root",
                "namespace": "ns",
                "database": "db",
            }
        ]
        assert capsys.readouterr().err.strip() == "Error: recorded"

    def test_environment_over_default(self, monkeypatch: pytest.MonkeyPatch, recorder: list[dict[str, Any]]) -> None:
        monkeypatch.setenv("SURREAL_URL", "ws://env-host:8000/rpc")
        monkeypatch.setenv("SURREAL_NAMESPACE", "env_ns")
        monkeypatch.setenv("SURREAL_DATABASE", "env_db")
        monkeypatch.setenv("SURREAL_USER", "env_user")
        monkeypatch.setenv("SURREAL_PASSWORD", "env_password")
        assert commands.main(["status"]) == 1
        assert recorder == [
            {
                "name": "default",
                "url": "ws://env-host:8000/rpc",
                "user": "env_user",
                "password": "env_password",
                "namespace": "env_ns",
                "database": "env_db",
            }
        ]

    def test_option_over_environment(self, monkeypatch: pytest.MonkeyPatch, recorder: list[dict[str, Any]]) -> None:
        for name in _ENV_VARS:
            monkeypatch.setenv(name, "from-env")
        argv = ["-u", "ws://opt:1/rpc", "-n", "o_ns", "-d", "o_db", "--user", "o_user", "--password", "o_pw"]
        assert commands.main([*argv, "--connection", "analytics", "status"]) == 1
        assert recorder == [
            {
                "name": "analytics",
                "url": "ws://opt:1/rpc",
                "user": "o_user",
                "password": "o_pw",
                "namespace": "o_ns",
                "database": "o_db",
            }
        ]

    def test_empty_variable_counts_as_unset(self, monkeypatch: pytest.MonkeyPatch, recorder: list[dict[str, Any]]) -> None:
        for name in _ENV_VARS:
            monkeypatch.setenv(name, "")
        assert commands.main(["-n", "ns", "-d", "db", "status"]) == 1
        assert recorder[0]["url"] == "http://localhost:8000"
        assert (recorder[0]["user"], recorder[0]["password"]) == ("root", "root")

    def test_environment_read_when_main_runs(self, monkeypatch: pytest.MonkeyPatch, recorder: list[dict[str, Any]]) -> None:
        monkeypatch.setenv("SURREAL_NAMESPACE", "first")
        monkeypatch.setenv("SURREAL_DATABASE", "db")
        commands.main(["status"])
        monkeypatch.setenv("SURREAL_NAMESPACE", "second")
        commands.main(["status"])
        assert [call["namespace"] for call in recorder] == ["first", "second"]

    # -- AC-5, AC-7: the namespace/database usage error ---------------------------------------------------------------

    @pytest.mark.parametrize(
        "argv",
        [
            ["status"],
            ["-n", "ns", "status"],
            ["-d", "db", "migrate"],
            ["upgrade"],
            ["rollback", "x"],
            ["inspectdb"],
            ["schemadiff"],
            ["shell"],
            ["makemigrations", "-n", "init"],
        ],
    )
    def test_db_command_without_namespace_or_database(
        self, argv: list[str], recorder: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        before = SurrealDBConnectionManager.list_connections()
        with pytest.raises(SystemExit) as exc:
            commands.main(argv)
        assert exc.value.code == 2
        command = next(arg for arg in argv if arg in _COMMANDS)
        assert (
            f"surreal-orm-lite: error: {command} needs a namespace and a database: pass --namespace/-n and --database/-d "
            "(or set SURREAL_NAMESPACE and SURREAL_DATABASE)."
        ) in capsys.readouterr().err
        assert recorder == []
        assert SurrealDBConnectionManager.list_connections() == before

    @pytest.mark.parametrize(
        "argv",
        [["makemigrations", "-n", "init", "--empty"], ["makemigrations", "-n", "init", "--no-from-db"]],
    )
    def test_offline_commands_need_no_namespace(
        self, argv: list[str], recorder: list[dict[str, Any]], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        commands.main(["-m", str(tmp_path / "migrations"), *argv])  # no SystemExit: no usage error
        assert recorder == []
        assert "needs a namespace" not in capsys.readouterr().err

    def test_cli_exits(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            commands.cli(["--version"])
        assert exc.value.code == 0
        with pytest.raises(SystemExit) as exc:
            commands.cli(["status"])
        assert exc.value.code == 2
        capsys.readouterr()

    def test_cli_exits_with_mains_status(self, recorder: list[dict[str, Any]]) -> None:
        with pytest.raises(SystemExit) as exc:
            commands.cli(["-n", "ns", "-d", "db", "status"])
        assert exc.value.code == 1

    # -- AC-6: the password never printed -----------------------------------------------------------------------------

    def test_password_not_in_help(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setenv("SURREAL_PASSWORD", _SECRET)
        monkeypatch.setenv("SURREAL_USER", _SECRET)
        monkeypatch.setenv("SURREAL_URL", f"ws://u:{_SECRET}@h/rpc")
        for argv in (["--help"], ["status", "--help"]):
            with pytest.raises(SystemExit):
                commands.main(argv)
            captured = capsys.readouterr()
            assert _SECRET not in captured.out + captured.err

    def test_settings_repr_hides_the_password(self) -> None:
        settings = commands._settings(commands._parser().parse_args(["--password", _SECRET, "status"]))
        assert settings.password == _SECRET
        assert _SECRET not in repr(settings)

    def test_add_connection_refusal(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert commands.main(["--user", "", "-n", "ns", "-d", "db", "status"]) == 1
        err = capsys.readouterr().err
        assert err.startswith("Error: add_connection('default'): a server URL needs user and password; ")

    def test_refused_url_is_scrubbed(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert commands.main(["--url", f"htps://u:{_SECRET}@h", "-n", "ns", "-d", "db", "status"]) == 1
        captured = capsys.readouterr()
        assert captured.err.startswith("Error: ")
        assert "htps://h" in captured.err
        assert _SECRET not in captured.out + captured.err

    def test_redact(self) -> None:
        assert commands._redact("ws://u:p@h:1/rpc") == "ws://h:1/rpc"
        assert commands._redact("http://user@h") == "http://h"
        assert commands._redact("mem://") == "mem://"
        assert commands._redact("surrealkv:///tmp/db") == "surrealkv:///tmp/db"
        assert commands._redact("http://[::1") == "http://[::1"  # does not parse: shown as is

    def test_scrub(self) -> None:
        with_userinfo = commands._settings(commands._parser().parse_args(["-u", f"ws://u:{_SECRET}@h/rpc", "status"]))
        text = f"cannot open 'ws://u:{_SECRET}@h/rpc' (ws://u:{_SECRET}@h/rpc)"
        assert commands._scrub(text, with_userinfo) == "cannot open 'ws://h/rpc' (ws://h/rpc)"
        plain = commands._settings(commands._parser().parse_args(["-u", "ws://h/rpc", "status"]))
        assert commands._scrub("ws://h/rpc root", plain) == "ws://h/rpc root"

    # -- AC-25: the logging handler -------------------------------------------------------------------------------

    def test_log_handler_removed_after_main(self, capsys: pytest.CaptureFixture[str]) -> None:
        logger = logging.getLogger("surreal_orm_lite")
        before = list(logger.handlers)
        assert commands.main(["--user", "", "-n", "ns", "-d", "db", "status"]) == 1
        assert logger.handlers == before
        with pytest.raises(SystemExit):
            commands.main(["status"])
        assert logger.handlers == before
        capsys.readouterr()

    def test_log_handler_writes_warnings_and_errors(self, capsys: pytest.CaptureFixture[str]) -> None:
        settings = commands._settings(commands._parser().parse_args(["-u", f"ws://u:{_SECRET}@h/rpc", "status"]))
        handler = commands._LogHandler(settings)
        logger = logging.getLogger("surreal_orm_lite.test_cli")
        url = f"ws://u:{_SECRET}@h/rpc"
        for level, text, args in (
            (logging.INFO, "hidden", ()),
            (logging.WARNING, "careful", ()),
            (logging.ERROR, "broken %s", (url,)),
        ):
            record = logger.makeRecord(logger.name, level, __file__, 1, text, args, None)
            if record.levelno >= handler.level:
                handler.handle(record)
        assert capsys.readouterr().err.splitlines() == ["Warning: careful", "Error: broken ws://h/rpc"]

    def test_interrupt_exits_130(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        def interrupt(*args: Any, **kwargs: Any) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(SurrealDBConnectionManager, "add_connection", interrupt)
        assert commands.main(["-n", "ns", "-d", "db", "status"]) == 130
        assert capsys.readouterr().err.strip() == "Interrupted."

    # -- AC-29: a running event loop ------------------------------------------------------------------------------

    async def test_running_loop_refused(self, capsys: pytest.CaptureFixture[str]) -> None:
        before = SurrealDBConnectionManager.get_config("default")
        assert commands.main(["sqlmigrate", "x"]) == 1
        assert capsys.readouterr().err.strip() == _LOOP_ERROR
        assert SurrealDBConnectionManager.get_config("default") == before

    async def test_running_loop_leaves_the_registry_untouched(self, capsys: pytest.CaptureFixture[str]) -> None:
        SurrealDBConnectionManager.add_connection("default", url="mem://", namespace="keep_ns", database="keep_db")
        before = SurrealDBConnectionManager.get_config("default")
        names = SurrealDBConnectionManager.list_connections()
        assert commands.main(["-n", "ns", "-d", "db", "status"]) == 1
        assert capsys.readouterr().err.strip() == _LOOP_ERROR
        assert SurrealDBConnectionManager.get_config("default") == before
        assert SurrealDBConnectionManager.list_connections() == names


_APP_SOURCE = """
from typing import Any

from surreal_orm_lite import BaseSurrealModel, SchemaMode, SurrealConfigDict


class Author(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="cli_author_{suffix}", schema_mode=SchemaMode.SCHEMAFULL)
    id: str | None = None
    name: str = ""
    meta: dict[str, Any] = {{}}
    bag: Any = None
"""

_BOOK_SOURCE = """

class Book(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="cli_book_{suffix}", schema_mode=SchemaMode.SCHEMAFULL)
    id: str | None = None
    title: str = ""
"""


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., str]]:
    """Spec §9.2: ``app(with_book, extra)`` writes a model module ``cli_app_<uuid8>`` to ``tmp_path`` (the working
    directory) — ``Author``, and ``Book`` when *with_book*, then the source *extra* (formatted with the module's
    ``{suffix}``) — and returns its name. The registry is patched to the models
    of the last module written (the suite's own models are alive in-process); the modules leave ``sys.modules``
    afterwards."""
    monkeypatch.chdir(tmp_path)
    suffix = uuid.uuid4().hex[:8]
    names: list[str] = []
    registered = introspector.get_registered_models
    monkeypatch.setattr(
        introspector,
        "get_registered_models",
        lambda: [model for model in registered() if names and model.__module__ == names[-1]],
    )

    def write(with_book: bool = True, extra: str = "") -> str:
        name = f"cli_app_{uuid.uuid4().hex[:8]}"
        source = _APP_SOURCE + (_BOOK_SOURCE if with_book else "") + extra
        (tmp_path / f"{name}.py").write_text(textwrap.dedent(source.format(suffix=suffix)))
        importlib.invalidate_caches()
        names.append(name)
        return name

    yield write
    for name in names:
        sys.modules.pop(name, None)


def _lines(text: str) -> list[str]:
    return text.splitlines()


class TestOffline:
    """The commands that send no request: no namespace, no database, no connection registered."""

    # -- AC-8: makemigrations --empty ---------------------------------------------------------------------------------

    def test_empty_twice(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        directory = tmp_path / "migrations"
        names = SurrealDBConnectionManager.list_connections()
        assert commands.main(["-m", str(directory), "makemigrations", "-n", "init", "--empty"]) == 0
        assert capsys.readouterr().out == f"Created empty migration: {directory / '0001_init.py'}\n"
        assert commands.main(["-m", str(directory), "makemigrations", "-n", "more", "--empty"]) == 0
        assert capsys.readouterr().out == f"Created empty migration: {directory / '0002_more.py'}\n"
        executor = MigrationExecutor(directory)
        assert len(executor.load_migration("0001_init").operations) == 0
        assert list(executor.load_migration("0002_more").dependencies) == ["0001_init"]
        assert SurrealDBConnectionManager.list_connections() == names

    def test_empty_failure(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        directory = tmp_path / "migrations"
        directory.mkdir()
        (directory / "0001_broken.py").write_text("raise RuntimeError('broken file')\n")
        assert commands.main(["-m", str(directory), "makemigrations", "-n", "init", "--empty"]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "makemigrations failed: broken file\n"

    # -- AC-9: makemigrations --no-from-db with --models --------------------------------------------------------------

    def test_no_from_db(self, app: Callable[[bool], str], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        module = app(True)
        names = SurrealDBConnectionManager.list_connections()
        assert commands.main(["makemigrations", "-n", "init", "-m", module, "--no-from-db"]) == 0
        out = _lines(capsys.readouterr().out)
        path = Path("migrations") / "0001_init.py"
        assert (tmp_path / path).is_file()
        operations = MigrationExecutor("migrations").load_migration("0001_init").operations
        assert len(operations) > 2
        assert out == [
            f"Created migration: {path}",
            f"Operations: {len(operations)}",
            *(f"  - {operation.describe()}" for operation in operations),
        ]
        assert SurrealDBConnectionManager.list_connections() == names
        # Run again: the file is part of the current state.
        assert commands.main(["makemigrations", "-n", "again", "-m", module, "--no-from-db"]) == 0
        captured = capsys.readouterr()
        assert captured.out == "No changes detected.\n"
        assert captured.err == ""
        assert not (tmp_path / "migrations" / "0002_again.py").exists()

    def test_several_models_options(
        self, app: Callable[[bool], str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        first = app(True)
        second = app(True)
        assert commands.main(["makemigrations", "-n", "init", "-m", first, "-m", second, "--no-from-db"]) == 0
        assert first in sys.modules
        assert second in sys.modules
        assert capsys.readouterr().out.startswith("Created migration: ")

    # -- AC-10: imports and the registry ------------------------------------------------------------------------------

    def test_missing_module(self, app: Callable[[bool], str], capsys: pytest.CaptureFixture[str]) -> None:
        name = f"cli_missing_{uuid.uuid4().hex[:8]}"
        assert commands.main(["makemigrations", "-n", "init", "-m", name, "--no-from-db"]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == f"Error importing {name}: No module named '{name}'\n"

    def test_module_raising(self, app: Callable[[bool], str], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        name = f"cli_raising_{uuid.uuid4().hex[:8]}"
        (tmp_path / f"{name}.py").write_text("raise RuntimeError('boom at import')\n")
        importlib.invalidate_caches()
        try:
            assert commands.main(["makemigrations", "-n", "init", "-m", name, "--no-from-db"]) == 1
        finally:
            sys.modules.pop(name, None)
        assert capsys.readouterr().err == f"Error importing {name}: boom at import\n"
        assert not (tmp_path / "migrations").exists()

    def test_module_raising_without_message(
        self, app: Callable[[bool], str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        name = f"cli_raising_{uuid.uuid4().hex[:8]}"
        (tmp_path / f"{name}.py").write_text("raise RuntimeError\n")
        importlib.invalidate_caches()
        try:
            assert commands.main(["makemigrations", "-n", "init", "-m", name, "--no-from-db"]) == 1
        finally:
            sys.modules.pop(name, None)
        assert capsys.readouterr().err == f"Error importing {name}: RuntimeError\n"

    def test_no_models(self, app: Callable[[bool], str], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert commands.main(["makemigrations", "-n", "init", "--no-from-db"]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "No models found. Register models by importing them (--models).\n"
        assert not (tmp_path / "migrations").exists()

    def test_working_directory_inserted_when_absent(
        self, app: Callable[[bool], str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cwd = os.getcwd()
        monkeypatch.setattr(sys, "path", [entry for entry in sys.path if entry not in ("", cwd)])
        rest = list(sys.path)
        assert commands.main(["makemigrations", "-n", "init", "-m", app(True), "--no-from-db"]) == 0
        assert sys.path == [cwd, *rest]
        capsys.readouterr()

    @pytest.mark.parametrize("present", ["", "cwd"])
    def test_working_directory_not_inserted_when_present(
        self, present: str, app: Callable[[bool], str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cwd = os.getcwd()
        entry = cwd if present == "cwd" else ""
        monkeypatch.setattr(sys, "path", [*(item for item in sys.path if item not in ("", cwd)), entry])
        before = list(sys.path)
        assert commands.main(["makemigrations", "-n", "init", "-m", app(True), "--no-from-db"]) == 0
        assert sys.path == before
        capsys.readouterr()

    def test_working_directory_untouched_without_models(
        self, app: Callable[[bool], str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cwd = os.getcwd()
        monkeypatch.setattr(sys, "path", [entry for entry in sys.path if entry not in ("", cwd)])
        before = list(sys.path)
        commands.main(["makemigrations", "-n", "init", "--no-from-db"])
        assert sys.path == before
        capsys.readouterr()

    # -- §5.3 step 6, §6.3: the removals left out offline -----------------------------------------------------------

    def test_removal_skipped_then_written(
        self, app: Callable[[bool], str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert commands.main(["makemigrations", "-n", "init", "-m", app(True), "--no-from-db"]) == 0
        book = next(
            operation.describe().removeprefix("Create table ")
            for operation in MigrationExecutor("migrations").load_migration("0001_init").operations
            if operation.describe().startswith("Create table cli_book_")
        )
        capsys.readouterr()
        without_book = app(False)
        assert commands.main(["makemigrations", "-n", "drop", "-m", without_book, "--no-from-db"]) == 0
        captured = capsys.readouterr()
        assert captured.err == (
            f"Warning: Skipped 1 removal(s) with no model to justify them: Drop table {book}; "
            "pass drop_missing=True (--drop-missing) to write them.\n"
        )
        assert captured.out == "No changes detected.\n"
        assert not (tmp_path / "migrations" / "0002_drop.py").exists()
        assert commands.main(["makemigrations", "-n", "drop", "-m", without_book, "--no-from-db", "--drop-missing"]) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        assert _lines(captured.out) == [
            f"Created migration: {Path('migrations') / '0002_drop.py'}",
            "Operations: 1",
            f"  - Drop table {book}",
        ]

    # -- AC-14: sqlmigrate ------------------------------------------------------------------------------------------

    @pytest.mark.parametrize("extra", [[], ["--line", "3"], ["--line", "2"], ["--backwards"], ["--backwards", "--line", "2"]])
    def test_sqlmigrate(self, extra: list[str], app: Callable[[bool], str], capsys: pytest.CaptureFixture[str]) -> None:
        assert commands.main(["makemigrations", "-n", "init", "-m", app(True), "--no-from-db"]) == 0
        capsys.readouterr()
        names = SurrealDBConnectionManager.list_connections()
        assert commands.main(["sqlmigrate", "0001_init", *extra]) == 0
        line = 2 if "2" in extra else 3
        expected = asyncio.run(
            MigrationExecutor("migrations").show_sql("0001_init", backwards="--backwards" in extra, line=line)
        )
        assert expected
        captured = capsys.readouterr()
        assert captured.out == expected + "\n"
        assert captured.err == ""
        assert SurrealDBConnectionManager.list_connections() == names

    def test_sqlmigrate_lines_differ(self, app: Callable[[bool], str], capsys: pytest.CaptureFixture[str]) -> None:
        assert commands.main(["makemigrations", "-n", "init", "-m", app(True), "--no-from-db"]) == 0
        capsys.readouterr()
        outputs = []
        for line in ("2", "3"):
            assert commands.main(["sqlmigrate", "0001_init", "--line", line]) == 0
            outputs.append(capsys.readouterr().out)
        assert "FLEXIBLE" in outputs[0]  # line 2: an `any` column of a SCHEMAFULL table
        assert outputs[0] != outputs[1]

    def test_sqlmigrate_not_found(self, app: Callable[[bool], str], capsys: pytest.CaptureFixture[str]) -> None:
        assert commands.main(["makemigrations", "-n", "init", "-m", app(True), "--no-from-db"]) == 0
        capsys.readouterr()
        assert commands.main(["sqlmigrate", "0009_nope"]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "Migration not found: 0009_nope\n"

    def test_sqlmigrate_without_directory(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert commands.main(["-m", str(tmp_path / "none"), "sqlmigrate", "0001_init"]) == 1
        assert capsys.readouterr().err == "Migration not found: 0001_init\n"

    def test_sqlmigrate_invalid_name(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert commands.main(["-m", str(tmp_path), "sqlmigrate", "nope"]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.startswith("Error: ")
        assert "nope" in captured.err


# === E2E: the migration commands on the line under test, over WebSocket and HTTP =====================================

#: A hand-written migration file (spec §9.2): *name*, *dependencies* and the operations' source.
_MIGRATION_FILE = """from surreal_orm_lite.migrations import Migration
from surreal_orm_lite.migrations.operations import AddField, CreateTable, DataMigration

migration = Migration(name={name!r}, dependencies={dependencies!r}, operations=[{operations}])
"""

_ITEM_OPERATIONS = 'CreateTable("cli_item", "SCHEMALESS"), AddField("cli_item", "n", "int")'
_MORE_OPERATIONS = 'CreateTable("cli_more", "SCHEMALESS")'
_DATA_OPERATIONS = (
    'CreateTable("cli_item", "SCHEMALESS"), '
    'DataMigration(forwards_sql="UPDATE cli_item SET n = n + 1;", backwards_sql="UPDATE cli_item SET n = n - 1;")'
)

_RULE = "-" * 60
_LEGEND = "Legend: [X]=applied, R=reversible, D=has data migrations"


def _write_migration(directory: Path, name: str, operations: str, dependencies: Sequence[str] = ()) -> None:
    """Write the migration file ``<directory>/<name>.py``."""
    directory.mkdir(parents=True, exist_ok=True)
    source = _MIGRATION_FILE.format(name=name, dependencies=list(dependencies), operations=operations)
    (directory / f"{name}.py").write_text(source, encoding="utf-8")


def _admin(kind: str, database: str, sql: str) -> Any:
    """Run *sql* on ``ns``/*database* of the line under test through a private connection (opened, closed and
    unregistered here, in an ``asyncio.run()`` of its own) and return the answer of its last statement."""

    async def run() -> Any:
        SurrealDBConnectionManager.add_connection(
            "cli_admin", url=_migration_url(kind), user="root", password="root", namespace="ns", database=database
        )
        try:
            client = await SurrealDBConnectionManager.get_client("cli_admin")
            return await client.query(sql, {})
        finally:
            await SurrealDBConnectionManager.remove_connection("cli_admin")

    return asyncio.run(run())


@pytest.fixture
def database(request: pytest.FixtureRequest) -> Iterator[str]:
    """Spec §9.2: a database of its own on the line under test (``REMOVE DATABASE IF EXISTS x; DEFINE DATABASE
    x;``), removed afterwards. The protocol is the test's ``kind`` parameter."""
    kind: str = request.node.callspec.params["kind"]
    name = new_database_name()
    _admin(kind, name, f"REMOVE DATABASE IF EXISTS {name}; DEFINE DATABASE {name};")
    try:
        yield name
    finally:
        _admin(kind, name, f"REMOVE DATABASE IF EXISTS {name};")


@pytest.fixture
def closes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A spy on the default connection class's ``close_connection``: one entry per await (spec §5.2: once per command,
    also on failure)."""
    connection = SurrealDBConnectionManager._manager_for("default")
    original = connection.close_connection
    calls: list[str] = []

    async def spy(*args: Any, **kwargs: Any) -> None:
        calls.append("closed")
        await original(*args, **kwargs)

    monkeypatch.setattr(connection, "close_connection", spy)
    return calls


def _tables(kind: str, database: str) -> set[str]:
    """The tables ``INFO FOR DB`` lists."""
    info = _admin(kind, database, "INFO FOR DB;")
    return set(info["tables"])


def _applied(kind: str, database: str) -> list[str]:
    """The names the tracking table records."""
    rows = _admin(kind, database, "SELECT name FROM _surreal_orm_migrations ORDER BY name;")
    return [row["name"] for row in rows]


def _status(*rows: str) -> str:
    """``status``'s exact output for *rows* (spec §5.7)."""
    return "\n".join(["Migration status:", _RULE, *rows, _RULE, _LEGEND]) + "\n"


def _closed_port() -> int:
    """A freshly closed ephemeral port: a connection to it is refused at once (spec §9.3)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


@pytest.mark.parametrize("kind", ["ws", "http"])
class TestCliE2E:
    """AC-13, AC-15, AC-16, AC-17, AC-23 (spec §5.4–§5.7): ``migrate``, ``upgrade``, ``rollback`` and ``status`` on
    the line under test, over WebSocket and HTTP, each in a database of its own; the resolved connection class's
    ``close_connection`` awaited once per command, also on failure."""

    @staticmethod
    def _argv(kind: str, database: str, directory: Path, *rest: str) -> list[str]:
        return ["-u", _migration_url(kind), "-n", "ns", "-d", database, "-m", str(directory), *rest]

    def test_migrate_and_status(
        self, kind: str, database: str, tmp_path: Path, closes: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        directory = tmp_path / "migrations"
        _write_migration(directory, "0001_init", _ITEM_OPERATIONS)
        argv = functools.partial(self._argv, kind, database, directory)
        assert commands.main(argv("status")) == 0
        assert capsys.readouterr().out == _status("[ ] 0001_init (2 ops) [R-]")
        assert commands.main(argv("migrate")) == 0
        captured = capsys.readouterr()
        assert captured.out == "Applied 1 migration(s):\n  - 0001_init\n"
        assert captured.err == ""
        assert {"_surreal_orm_migrations", "cli_item"} <= _tables(kind, database)
        assert commands.main(argv("status")) == 0
        assert capsys.readouterr().out == _status("[X] 0001_init (2 ops) [R-]")
        assert commands.main(argv("migrate")) == 0
        assert capsys.readouterr().out == "No migrations to apply.\n"
        assert closes == ["closed"] * 4
        assert _applied(kind, database) == ["0001_init"]

    def test_status_of_an_empty_directory(
        self, kind: str, database: str, tmp_path: Path, closes: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert commands.main(self._argv(kind, database, tmp_path / "none", "status")) == 0
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("No migrations found.\n", "")
        assert closes == ["closed"]

    def test_migrate_fake(
        self, kind: str, database: str, tmp_path: Path, closes: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        directory = tmp_path / "migrations"
        _write_migration(directory, "0001_init", _ITEM_OPERATIONS)
        assert commands.main(self._argv(kind, database, directory, "migrate", "--fake")) == 0
        assert capsys.readouterr().out == "Applied 1 migration(s):\n  - 0001_init\n"
        tables = _tables(kind, database)
        assert "_surreal_orm_migrations" in tables
        assert "cli_item" not in tables
        assert _applied(kind, database) == ["0001_init"]
        assert closes == ["closed"]

    def test_migrate_target(
        self, kind: str, database: str, tmp_path: Path, closes: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        directory = tmp_path / "migrations"
        _write_migration(directory, "0001_init", _ITEM_OPERATIONS)
        _write_migration(directory, "0002_more", _MORE_OPERATIONS, ["0001_init"])
        argv = functools.partial(self._argv, kind, database, directory)
        assert commands.main(argv("migrate", "-t", "0001_init")) == 0
        assert capsys.readouterr().out == "Applied 1 migration(s):\n  - 0001_init\n"
        assert commands.main(argv("status")) == 0
        assert capsys.readouterr().out == _status("[X] 0001_init (2 ops) [R-]", "[ ] 0002_more (1 ops) [R-]")
        assert commands.main(argv("migrate", "--target", "0009_x")) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == f"Migration failed: Unknown migration target '0009_x': no such migration file in {directory}.\n"
        assert "Traceback" not in captured.err
        assert _applied(kind, database) == ["0001_init"]
        assert closes == ["closed"] * 3

    def test_upgrade(
        self, kind: str, database: str, tmp_path: Path, closes: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        directory = tmp_path / "migrations"
        _write_migration(directory, "0001_data", _DATA_OPERATIONS)
        argv = functools.partial(self._argv, kind, database, directory)
        assert commands.main(argv("upgrade")) == 0  # nothing applied yet
        assert capsys.readouterr().out == "No data migrations to apply.\n"
        assert commands.main(argv("migrate")) == 0  # schema only: the data operation does not run
        capsys.readouterr()
        _admin(kind, database, "CREATE cli_item:one SET n = 1;")
        assert commands.main(argv("status")) == 0
        assert capsys.readouterr().out == _status("[X] 0001_data (2 ops) [RD]")
        assert commands.main(argv("upgrade")) == 0
        captured = capsys.readouterr()
        assert captured.out == "Applied data migrations for 1 migration(s):\n  - 0001_data\n"
        assert captured.err == ""
        assert _admin(kind, database, "SELECT VALUE n FROM cli_item:one;") == [2]
        assert commands.main(argv("upgrade", "-t", "0009_x")) == 1
        captured = capsys.readouterr()
        assert captured.err == f"Upgrade failed: Unknown migration target '0009_x': no such migration file in {directory}.\n"
        assert closes == ["closed"] * 5

    def test_rollback(
        self, kind: str, database: str, tmp_path: Path, closes: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        directory = tmp_path / "migrations"
        _write_migration(directory, "0001_init", _ITEM_OPERATIONS)
        _write_migration(directory, "0002_more", _MORE_OPERATIONS, ["0001_init"])
        argv = functools.partial(self._argv, kind, database, directory)
        assert commands.main(argv("migrate")) == 0
        assert capsys.readouterr().out == "Applied 2 migration(s):\n  - 0001_init\n  - 0002_more\n"
        assert commands.main(argv("rollback", "0001_init")) == 0
        assert capsys.readouterr().out == "Rolled back 1 migration(s):\n  - 0002_more\n"
        assert _applied(kind, database) == ["0001_init"]
        assert "cli_more" not in _tables(kind, database)
        assert commands.main(argv("rollback", "0009_nope")) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "Rollback failed: Cannot roll back to '0009_nope': it is not applied.\n"
        assert commands.main(argv("rollback", "")) == 0
        assert capsys.readouterr().out == "Rolled back 1 migration(s):\n  - 0001_init\n"
        assert _applied(kind, database) == []
        assert commands.main(argv("rollback", "")) == 0
        assert capsys.readouterr().out == "No migrations to rollback.\n"
        assert commands.main(argv("status")) == 0
        assert capsys.readouterr().out == _status("[ ] 0001_init (2 ops) [R-]", "[ ] 0002_more (1 ops) [R-]")
        assert closes == ["closed"] * 6

    def test_wrong_password(
        self, kind: str, database: str, tmp_path: Path, closes: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC-6: a refused password is reported without its value (``Status failed: Can't connect …``)."""
        argv = ["--password", _SECRET, *self._argv(kind, database, tmp_path, "status")]
        assert commands.main(argv) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.splitlines()[-1] == "Status failed: Can't connect to the database."
        assert _SECRET not in captured.err
        assert "Traceback" not in captured.err
        assert closes == ["closed"]

    def test_refused_connection(
        self, kind: str, database: str, tmp_path: Path, closes: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        port = _closed_port()
        url = f"ws://127.0.0.1:{port}/rpc" if kind == "ws" else f"http://127.0.0.1:{port}"
        argv = ["-u", url, "-n", "ns", "-d", database, "-m", str(tmp_path), "migrate"]
        assert commands.main(argv) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        lines = captured.err.splitlines()
        # The lite logs the cause (a WARNING record), shown by the CLI's handler before the command's own line.
        assert lines[0].startswith("Warning: Can't get connection: ")
        assert lines[-1] == "Migration failed: Can't connect to the database."
        assert "Traceback" not in captured.err
        assert closes == ["closed"]

    # -- AC-23: an interrupt and an empty message -----------------------------------------------------------------

    @pytest.mark.parametrize(
        ("command", "method", "prefix"),
        [
            (["migrate"], "migrate", "Migration failed"),
            (["upgrade"], "upgrade", "Upgrade failed"),
            (["rollback", ""], "rollback", "Rollback failed"),
            (["status"], "get_migration_status", "Status failed"),
        ],
    )
    def test_interrupt_and_empty_message(
        self,
        kind: str,
        command: list[str],
        method: str,
        prefix: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        closes: list[str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """No request: the executor's method is replaced; the database is never reached."""
        raised: list[BaseException] = [KeyboardInterrupt(), RuntimeError("")]

        async def boom(*args: Any, **kwargs: Any) -> Any:
            raise raised.pop(0)

        monkeypatch.setattr(MigrationExecutor, method, boom)
        argv = self._argv(kind, "db", tmp_path, *command)
        assert commands.main(argv) == 130
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", "Interrupted.\n")
        assert commands.main(argv) == 1
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", f"{prefix}: RuntimeError\n")
        assert closes == ["closed"] * 2


# === E2E: makemigrations from the database, schemadiff, --connection =================================================

_HINT = "Pass --no-from-db to diff against the migration files instead (offline)."

#: A model of the ``analytics`` connection (AC-19), appended to the ``app`` module.
_METRIC_SOURCE = """

class Metric(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_name="cli_metric_{suffix}", schema_mode=SchemaMode.SCHEMAFULL, connection="analytics"
    )
    id: str | None = None
    value: int = 0
"""

#: One more field on ``Book`` (the last class of the ``app`` module): one difference.
_PAGES_SOURCE = """    pages: int = 0
"""


def _created(out: str) -> list[str]:
    """The ``  - <describe>`` lines of a ``Created migration:`` output."""
    return [line.removeprefix("  - ") for line in _lines(out) if line.startswith("  - ")]


@pytest.fixture
def analytics() -> Iterator[None]:
    """The ``analytics`` connection's settings restored (or the name removed) after the test."""
    config = SurrealDBConnectionManager.get_config("analytics")
    yield
    if config is None:
        asyncio.run(SurrealDBConnectionManager.remove_connection("analytics"))
    else:
        SurrealDBConnectionManager.add_connection("analytics", **dataclasses.asdict(config))


@pytest.mark.parametrize("kind", ["ws", "http"])
class TestCliFromDbE2E:
    """AC-11 (WebSocket and HTTP), AC-12, AC-12b, AC-19 (makemigrations), AC-30 (spec §5.3 steps 4 and 7, §5.10):
    ``makemigrations`` against the database's schema and ``schemadiff``, each test in a database of its own."""

    @staticmethod
    def _argv(kind: str, database: str, *rest: str) -> list[str]:
        return ["-u", _migration_url(kind), "-n", "ns", "-d", database, *rest]

    def test_from_db_chain(
        self,
        kind: str,
        database: str,
        app: Callable[..., str],
        tmp_path: Path,
        closes: list[str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """AC-11: makemigrations → migrate → status → makemigrations → schemadiff → rollback "" → status."""
        module = app(True)
        argv = functools.partial(self._argv, kind, database)
        assert commands.main(argv("makemigrations", "-n", "init", "-m", module)) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        path = Path("migrations") / "0001_init.py"
        operations = MigrationExecutor("migrations").load_migration("0001_init").operations
        assert _lines(captured.out) == [
            f"Created migration: {path}",
            f"Operations: {len(operations)}",
            *(f"  - {operation.describe()}" for operation in operations),
        ]
        assert (tmp_path / path).is_file()
        assert commands.main(argv("migrate")) == 0
        assert capsys.readouterr().out == "Applied 1 migration(s):\n  - 0001_init\n"
        assert commands.main(argv("status")) == 0
        assert f"[X] 0001_init ({len(operations)} ops) [R-]" in capsys.readouterr().out
        assert commands.main(argv("makemigrations", "-n", "again", "-m", module)) == 0
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("No changes detected.\n", "")
        assert not (tmp_path / "migrations" / "0002_again.py").exists()
        assert commands.main(argv("schemadiff", "-m", module)) == 0
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("Models and database are in sync.\n", "")
        assert commands.main(argv("rollback", "")) == 0
        assert capsys.readouterr().out == "Rolled back 1 migration(s):\n  - 0001_init\n"
        assert commands.main(argv("status")) == 0
        assert f"[ ] 0001_init ({len(operations)} ops) [R-]" in capsys.readouterr().out
        assert closes == ["closed"] * 7

    def test_table_defined_by_hand(
        self,
        kind: str,
        database: str,
        app: Callable[..., str],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """AC-12: a table no model declares is a removal makemigrations writes only with --drop-missing, and
        schemadiff lists apart (exit 0); a model change is one difference."""
        legacy = f"cli_legacy_{uuid.uuid4().hex[:8]}"
        _admin(kind, database, f"DEFINE TABLE {legacy} SCHEMALESS;")
        module = app(True)
        argv = functools.partial(self._argv, kind, database)
        assert commands.main(argv("schemadiff", "-m", module)) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        out = _lines(captured.out)
        assert out[0].startswith("Found ")
        assert out[-3:] == [
            "1 irreversible operation(s) with no model to justify them:",
            f"  - Drop table {legacy}",
            "makemigrations writes these only with --drop-missing.",
        ]
        assert commands.main(argv("makemigrations", "-n", "init", "-m", module)) == 0
        captured = capsys.readouterr()
        assert captured.err == (
            f"Warning: Skipped 1 removal(s) with no model to justify them: Drop table {legacy}; "
            "pass drop_missing=True (--drop-missing) to write them.\n"
        )
        assert not any(legacy in line for line in _created(captured.out))
        assert commands.main(argv("migrate")) == 0
        capsys.readouterr()
        assert commands.main(argv("schemadiff", "-m", module)) == 0
        assert _lines(capsys.readouterr().out) == [
            "1 irreversible operation(s) with no model to justify them:",
            f"  - Drop table {legacy}",
            "makemigrations writes these only with --drop-missing.",
        ]
        assert commands.main(argv("makemigrations", "-n", "drop", "-m", module, "--drop-missing")) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        assert _created(captured.out) == [f"Drop table {legacy}"]
        assert (tmp_path / "migrations" / "0002_drop.py").is_file()
        changed = app(True, _PAGES_SOURCE)  # the registry now holds this module's models
        assert commands.main(argv("schemadiff", "-m", changed)) == 0
        out = _lines(capsys.readouterr().out)
        assert len(out) == 5
        assert out[0] == "Found 1 difference(s):"
        assert out[1].startswith("  - Add field pages to cli_book_")
        assert out[2:] == [
            "1 irreversible operation(s) with no model to justify them:",
            f"  - Drop table {legacy}",
            "makemigrations writes these only with --drop-missing.",
        ]

    def test_refused_connection(
        self, kind: str, tmp_path: Path, app: Callable[..., str], closes: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC-12b: a connection error gets the --no-from-db hint; schemadiff reports its own prefix."""
        port = _closed_port()
        url = f"ws://127.0.0.1:{port}/rpc" if kind == "ws" else f"http://127.0.0.1:{port}"
        module = app(True)
        base = ["-u", url, "-n", "ns", "-d", "db"]
        assert commands.main([*base, "makemigrations", "-n", "init", "-m", module]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.splitlines()[-2:] == ["makemigrations failed: Can't connect to the database.", _HINT]
        assert "Traceback" not in captured.err
        assert not (tmp_path / "migrations").exists()
        assert commands.main([*base, "schemadiff", "-m", module]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.splitlines()[-1] == "Schema diff failed: Can't connect to the database."
        assert _HINT not in captured.err
        assert closes == ["closed"] * 2

    @pytest.mark.parametrize(
        "error",
        [PermissionError("x"), FileNotFoundError("x"), ValueError("x")],
        ids=["permission", "file-not-found", "value"],
    )
    def test_no_hint_on_other_errors(
        self,
        kind: str,
        error: Exception,
        app: Callable[..., str],
        monkeypatch: pytest.MonkeyPatch,
        closes: list[str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """AC-12b: an error that is not a connection problem gets no hint. No request: makemigrations is replaced."""

        async def boom(*args: Any, **kwargs: Any) -> Any:
            raise error

        monkeypatch.setattr(commands, "makemigrations", boom)
        assert commands.main(self._argv(kind, "db", "makemigrations", "-n", "init", "-m", app(True))) == 1
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", "makemigrations failed: x\n")
        assert closes == ["closed"]

    @pytest.mark.parametrize(
        "error",
        [
            SurrealDbConnectionError("down"),
            SurrealDbAuthenticationError("down"),
            ConnectionResetError("down"),
            TimeoutError("down"),
            socket.gaierror("down"),
        ],
        ids=["surreal-connection", "surreal-authentication", "connection", "timeout", "gaierror"],
    )
    def test_hint_on_connection_errors(
        self,
        kind: str,
        error: Exception,
        app: Callable[..., str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Spec §5.3 step 7: the five connection error types, and only with --from-db."""

        async def boom(*args: Any, **kwargs: Any) -> Any:
            raise error

        monkeypatch.setattr(commands, "makemigrations", boom)
        module = app(True)
        assert commands.main(self._argv(kind, "db", "makemigrations", "-n", "init", "-m", module)) == 1
        assert capsys.readouterr().err == f"makemigrations failed: down\n{_HINT}\n"
        assert commands.main(self._argv(kind, "db", "makemigrations", "-n", "init", "-m", module, "--no-from-db")) == 1
        assert capsys.readouterr().err == "makemigrations failed: down\n"

    def test_schemadiff_without_models(
        self,
        kind: str,
        app: Callable[..., str],
        recorder: list[dict[str, Any]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """AC-30: no registered model → exit 1 before any connection is configured."""
        assert commands.main(self._argv(kind, "db", "schemadiff")) == 1
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", "No models found. Register models by importing them (--models).\n")
        assert recorder == []

    def test_makemigrations_without_models_configures_nothing(
        self,
        kind: str,
        app: Callable[..., str],
        recorder: list[dict[str, Any]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Spec §5.3: the connection is configured after the models are found (step 4)."""
        assert commands.main(self._argv(kind, "db", "makemigrations", "-n", "init")) == 1
        assert capsys.readouterr().err == "No models found. Register models by importing them (--models).\n"
        assert recorder == []

    def test_schemadiff_import_failure(
        self, kind: str, app: Callable[..., str], recorder: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert commands.main(self._argv(kind, "db", "schemadiff", "-m", "cli_no_such_module")) == 1
        assert capsys.readouterr().err.startswith("Error importing cli_no_such_module: ")
        assert recorder == []

    def test_connection_option(
        self,
        kind: str,
        database: str,
        app: Callable[..., str],
        analytics: None,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """AC-19 (makemigrations): the settings registered under ``analytics``, the file holds that connection's
        model only, ``default``'s settings untouched."""
        default = SurrealDBConnectionManager.get_config("default")
        module = app(False, _METRIC_SOURCE)
        argv = ["--connection", "analytics", *self._argv(kind, database, "makemigrations", "-n", "init", "-m", module)]
        assert commands.main(argv) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        created = _created(captured.out)
        assert any(line.startswith("Create table cli_metric_") for line in created)
        assert not any("cli_author_" in line for line in created)
        config = SurrealDBConnectionManager.get_config("analytics")
        assert config is not None
        assert (config.url, config.namespace, config.database) == (_migration_url(kind), "ns", database)
        assert SurrealDBConnectionManager.get_config("default") == default
        assert commands.main(["--connection", "analytics", *self._argv(kind, database, "schemadiff", "-m", module)]) == 0
        out = _lines(capsys.readouterr().out)
        assert out[0] == f"Found {len(created)} difference(s):"
        assert not any("cli_author_" in line for line in out)


# === E2E: inspectdb ==================================================================================================

#: Two tables to read back (spec §5.9; AC-18).
_INSPECT_SCHEMA = (
    "DEFINE TABLE cli_t1 SCHEMAFULL; DEFINE FIELD a ON cli_t1 TYPE int; "
    "DEFINE TABLE cli_t2 SCHEMAFULL; DEFINE FIELD b ON cli_t2 TYPE string;"
)


def _generated(connection: str | None = None, **kwargs: Any) -> str:
    """``generate_models_from_db()``'s source on *connection* (already configured), in an ``asyncio.run()`` that
    closes that connection afterwards."""

    async def run() -> str:
        try:
            return await generate_models_from_db(None, connection, **kwargs)
        finally:
            await SurrealDBConnectionManager._manager_for(connection or "default").close_connection()

    return asyncio.run(run())


def _stdout_of(source: str) -> str:
    """What ``inspectdb`` prints for *source*: one trailing newline, none added when present."""
    return source if source.endswith("\n") else source + "\n"


@pytest.mark.parametrize("kind", ["ws", "http"])
class TestCliInspectdbE2E:
    """AC-18, AC-19 (inspectdb) (spec §5.9): ``inspectdb`` on the line under test, over WebSocket and HTTP, each in a
    database of its own; the resolved connection class's ``close_connection`` awaited once per command."""

    @staticmethod
    def _argv(kind: str, database: str, *rest: str) -> list[str]:
        return ["-u", _migration_url(kind), "-n", "ns", "-d", database, *rest]

    def test_stdout_is_the_generated_source(
        self, kind: str, database: str, closes: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _admin(kind, database, _INSPECT_SCHEMA)
        assert commands.main(self._argv(kind, database, "inspectdb")) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        assert closes == ["closed"]
        source = _generated()
        assert "cli_t1" in source and "cli_t2" in source
        assert captured.out == _stdout_of(source)

    def test_output_file_and_no_overwrite(
        self, kind: str, database: str, tmp_path: Path, closes: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _admin(kind, database, _INSPECT_SCHEMA)
        output = tmp_path / "models.py"
        assert commands.main(self._argv(kind, database, "inspectdb", "-o", str(output))) == 0
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == (f"Generated models written to: {output}\n", "")
        content = output.read_text(encoding="utf-8")
        assert content == _generated()
        assert commands.main(self._argv(kind, database, "inspectdb", "--output", str(output), "--no-overwrite")) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.startswith("Introspection failed: ")
        assert "already exists" in captured.err
        assert "Traceback" not in captured.err
        assert output.read_text(encoding="utf-8") == content
        assert closes == ["closed"] * 3  # two commands, then the direct call's close

    def test_table_option(self, kind: str, database: str, closes: list[str], capsys: pytest.CaptureFixture[str]) -> None:
        _admin(kind, database, _INSPECT_SCHEMA)
        assert commands.main(self._argv(kind, database, "inspectdb", "--table", "cli_t1")) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        assert "cli_t1" in captured.out
        assert "cli_t2" not in captured.out
        assert captured.out == _stdout_of(_generated(tables=["cli_t1"]))
        assert commands.main(self._argv(kind, database, "inspectdb", "--table", "cli_t1", "--table", "cli_t2")) == 0
        out = capsys.readouterr().out
        assert "cli_t1" in out and "cli_t2" in out

    def test_unknown_table(self, kind: str, database: str, closes: list[str], capsys: pytest.CaptureFixture[str]) -> None:
        _admin(kind, database, _INSPECT_SCHEMA)
        assert commands.main(self._argv(kind, database, "inspectdb", "--table", "cli_nope")) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.splitlines()[-1].startswith("Introspection failed: ")
        assert "Traceback" not in captured.err
        assert closes == ["closed"]

    def test_connection_option(self, kind: str, database: str, analytics: None, capsys: pytest.CaptureFixture[str]) -> None:
        """AC-19 (inspectdb): the generated models carry ``connection="analytics"``; ``default`` untouched."""
        _admin(kind, database, _INSPECT_SCHEMA)
        default = SurrealDBConnectionManager.get_config("default")
        assert commands.main(["--connection", "analytics", *self._argv(kind, database, "inspectdb")]) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        assert 'connection="analytics"' in captured.out
        assert captured.out == _stdout_of(_generated("analytics"))
        config = SurrealDBConnectionManager.get_config("analytics")
        assert config is not None
        assert (config.url, config.namespace, config.database) == (_migration_url(kind), "ns", database)
        assert SurrealDBConnectionManager.get_config("default") == default

    def test_refused_connection(
        self, kind: str, database: str, tmp_path: Path, closes: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        port = _closed_port()
        url = f"ws://127.0.0.1:{port}/rpc" if kind == "ws" else f"http://127.0.0.1:{port}"
        output = tmp_path / "models.py"
        assert commands.main(["-u", url, "-n", "ns", "-d", database, "inspectdb", "-o", str(output)]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.splitlines()[-1].startswith("Introspection failed: ")
        assert "Traceback" not in captured.err
        assert not output.exists()
        assert closes == ["closed"]


# === shell: rendering (no database) ==================================================================================


class TestShellRendering:
    """Spec §5.11: ``_render_answer()`` on the envelopes ``query_raw()`` returns (M1–M4), recorded on 3.3.4, 2.7.0 and
    the embedded engine; one block per statement, with ``-- [i/N] --`` headers when a line holds several."""

    def test_one_value(self) -> None:
        assert commands._render_answer({"result": [{"result": 2, "status": "OK", "time": "1µs"}]}) == ["2"]

    def test_one_list(self) -> None:
        answer = {"result": [{"result": [{"a": 1}, {"a": 2}], "status": "OK", "time": "1µs"}]}
        assert commands._render_answer(answer) == ["{'a': 1}", "{'a': 2}"]

    @pytest.mark.parametrize("value", [[], None])
    def test_empty_result(self, value: Any) -> None:
        answer = {"id": "x", "result": [{"result": value, "status": "OK", "time": "1µs", "type": None}]}
        assert commands._render_answer(answer) == ["(empty result)"]

    def test_scalar_is_pretty_printed(self) -> None:
        answer = {"result": [{"result": "text", "status": "OK", "time": "1µs"}]}
        assert commands._render_answer(answer) == ["'text'"]

    def test_several_entries_get_headers(self) -> None:
        answer = {
            "result": [
                {"result": [{"a": 1}], "status": "OK", "time": "1µs"},
                {"result": 5, "status": "OK", "time": "1µs"},
            ]
        }
        assert commands._render_answer(answer) == ["-- [1/2] --", "{'a': 1}", "-- [2/2] --", "5"]

    def test_err_entry(self) -> None:
        answer = {
            "result": [
                {
                    "details": {"details": {"name": "t"}, "kind": "Table"},
                    "kind": "NotFound",
                    "result": "The table 't' does not exist",
                    "status": "ERR",
                    "time": "1µs",
                    "type": None,
                }
            ]
        }
        assert commands._render_answer(answer) == ["Error: The table 't' does not exist"]

    def test_top_level_error(self) -> None:
        message = "Parse error: Unexpected token\n --> [1:8]\n  |        ^ \n"
        answer = {"error": {"code": -32000, "message": message}, "id": "x"}
        assert commands._render_answer(answer) == ["Error: Parse error: Unexpected token\n --> [1:8]\n  |        ^"]

    def test_top_level_error_without_message(self) -> None:
        assert commands._render_answer({"error": {"code": -32000}}) == ["Error: {'code': -32000}"]
        assert commands._render_answer({"error": "boom"}) == ["Error: boom"]

    @pytest.mark.parametrize(
        ("answer", "name"), [(None, "NoneType"), ([1], "list"), ({"id": "x"}, "dict"), ({"result": 1}, "dict")]
    )
    def test_unexpected_answer(self, answer: Any, name: str) -> None:
        assert commands._render_answer(answer) == [f"Error: unexpected answer ({name})"]

    def test_unexpected_entry(self) -> None:
        answer = {"result": [{"result": 1, "status": "OK"}, "oops"]}
        assert commands._render_answer(answer) == ["-- [1/2] --", "1", "-- [2/2] --", "Error: unexpected answer (str)"]

    def test_transaction_3x(self) -> None:
        """M3: 3.x answers one entry for ``BEGIN`` and one for ``COMMIT``."""
        answer = {
            "result": [
                {"result": None, "status": "OK", "time": "0ns", "type": None},
                {"result": [{"a": 2}], "status": "OK", "time": "1µs", "type": None},
                {"result": None, "status": "OK", "time": "1µs", "type": None},
            ]
        }
        assert commands._render_answer(answer) == [
            "-- [1/3] --",
            "(empty result)",
            "-- [2/3] --",
            "{'a': 2}",
            "-- [3/3] --",
            "(empty result)",
        ]

    def test_failed_transaction_2x(self) -> None:
        """M3: 2.x answers one entry per statement inside the transaction, none for ``BEGIN``/``COMMIT``."""
        answer = {
            "result": [
                {"result": "Database record `t:2` already exists", "status": "ERR", "time": "1µs"},
                {"result": "The query was not executed due to a failed transaction", "status": "ERR", "time": "0ns"},
            ]
        }
        assert commands._render_answer(answer) == [
            "-- [1/2] --",
            "Error: Database record `t:2` already exists",
            "-- [2/2] --",
            "Error: The query was not executed due to a failed transaction",
        ]


# === E2E: shell ======================================================================================================

_BANNER_TAIL = ["Type 'exit' or 'quit' to exit. Enter SurrealQL queries:", _RULE]


class _Prefix(str):
    """An expected output line that only has to start with this text."""


def _check(chunk: str, expected: Sequence[str]) -> None:
    """*chunk*'s lines against *expected* (a :class:`_Prefix` matches by prefix)."""
    lines = chunk.splitlines()
    assert len(lines) == len(expected), lines
    for line, want in zip(lines, expected, strict=True):
        if isinstance(want, _Prefix):
            assert line.startswith(want), (line, want)
        else:
            assert line == want


def _feed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    lines: Sequence[str],
    end: type[BaseException] = EOFError,
) -> list[str]:
    """``input()`` replaced by *lines*, then *end* raised. The returned list gets, at each prompt, the stdout written
    since the previous one: the banner first, then the answer of each line in turn."""
    feed = iter(lines)
    chunks: list[str] = []

    def fake_input(prompt: str = "") -> str:
        assert prompt == "surreal> "
        chunks.append(capsys.readouterr().out)
        try:
            return next(feed)
        except StopIteration:
            raise end() from None

    monkeypatch.setattr("builtins.input", fake_input)
    return chunks


def _major(kind: str, database: str) -> int:
    """The line under test (``cached_major()`` on a private connection)."""

    async def run() -> int:
        SurrealDBConnectionManager.add_connection(
            "cli_admin", url=_migration_url(kind), user="root", password="root", namespace="ns", database=database
        )
        try:
            return await cached_major(await SurrealDBConnectionManager.get_client("cli_admin"))
        finally:
            await SurrealDBConnectionManager.remove_connection("cli_admin")

    return asyncio.run(run())


@pytest.mark.parametrize("kind", ["ws", "http"])
class TestCliShellE2E:
    """AC-20, AC-21 (spec §5.11): the SurrealQL shell on the line under test, over WebSocket and HTTP, in a database of
    its own — one block per statement, each line's layout as the server answers it (branch by ``cached_major()``, no
    skip); the session's end on ``exit``, EOF, Ctrl+C at the prompt (exit 0) or during a query (exit 130); the resolved
    connection class's ``close_connection`` awaited once in every case."""

    @staticmethod
    def _argv(kind: str, database: str) -> list[str]:
        return ["-u", _migration_url(kind), "-n", "ns", "-d", database, "shell"]

    @staticmethod
    def _banner(url: str, database: str) -> list[str]:
        return [f"Connected to {url}", f"Namespace: ns, Database: {database}", *_BANNER_TAIL]

    def test_session(
        self,
        kind: str,
        database: str,
        closes: list[str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """AC-20."""
        script = [
            "RETURN 1+1",
            "CREATE cli_shell:1 SET a=1; RETURN 5",
            "SELEKT 1",
            "SELECT * FROM cli_shell_missing",
            "BEGIN; CREATE cli_shell:2 SET a=2; COMMIT;",
            "BEGIN; CREATE cli_shell:2 SET a=3; RETURN 5; COMMIT;",
            "   ",
            "  EXIT ",
        ]
        chunks = _feed(monkeypatch, capsys, script)
        assert commands.main(self._argv(kind, database)) == 0
        captured = capsys.readouterr()
        chunks.append(captured.out)
        assert closes == ["closed"]
        record_1 = pprint.pformat({"a": 1, "id": RecordID("cli_shell", 1)})
        record_2 = pprint.pformat({"a": 2, "id": RecordID("cli_shell", 2)})
        major = _major(kind, database)
        _check(chunks[0], self._banner(_migration_url(kind), database))
        _check(chunks[1], ["2"])
        _check(chunks[2], ["-- [1/2] --", record_1, "-- [2/2] --", "5"])
        assert chunks[3].startswith("Error: ")
        assert "Parse error" in chunks[3]
        if major >= 3:
            _check(chunks[4], ["Error: The table 'cli_shell_missing' does not exist"])
            _check(chunks[5], ["-- [1/3] --", "(empty result)", "-- [2/3] --", record_2, "-- [3/3] --", "(empty result)"])
            _check(
                chunks[6],
                [
                    "-- [1/4] --",
                    "(empty result)",
                    "-- [2/4] --",
                    _Prefix("Error: "),
                    "-- [3/4] --",
                    "Error: The query was not executed due to a cancelled transaction",
                    "-- [4/4] --",
                    _Prefix("Error: Cannot COMMIT"),
                ],
            )
            assert chunks[6].splitlines()[3].endswith("already exists")
        else:
            _check(chunks[4], ["(empty result)"])
            _check(chunks[5], [record_2])
            _check(
                chunks[6],
                [
                    "-- [1/2] --",
                    _Prefix("Error: "),
                    "-- [2/2] --",
                    "Error: The query was not executed due to a failed transaction",
                ],
            )
            assert chunks[6].splitlines()[1].endswith("already exists")
        assert chunks[7] == ""  # the blank line is skipped
        assert chunks[8] == "Goodbye!\n"
        assert len(chunks) == 9
        assert captured.err == ""

    @pytest.mark.parametrize("end", [EOFError, KeyboardInterrupt])
    def test_eof_and_ctrl_c_at_the_prompt(
        self,
        kind: str,
        database: str,
        end: type[BaseException],
        closes: list[str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """AC-21: EOF and Ctrl+C at the prompt end the session with ``Goodbye!``, exit 0."""
        chunks = _feed(monkeypatch, capsys, ["RETURN 1"], end)
        assert commands.main(self._argv(kind, database)) == 0
        captured = capsys.readouterr()
        _check(chunks[0], self._banner(_migration_url(kind), database))
        assert chunks[1:] == ["1\n"]
        assert (captured.out, captured.err) == ("Goodbye!\n", "")
        assert closes == ["closed"]

    def test_quit(
        self,
        kind: str,
        database: str,
        closes: list[str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        chunks = _feed(monkeypatch, capsys, ["Quit", "RETURN 1"])
        assert commands.main(self._argv(kind, database)) == 0
        assert len(chunks) == 1
        assert capsys.readouterr().out == "Goodbye!\n"
        assert closes == ["closed"]

    def test_refused_connection(
        self,
        kind: str,
        database: str,
        closes: list[str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """AC-21: a connection refused at start → ``Shell failed: …``, exit 1, no prompt."""
        chunks = _feed(monkeypatch, capsys, ["RETURN 1"])
        port = _closed_port()
        url = f"ws://127.0.0.1:{port}/rpc" if kind == "ws" else f"http://127.0.0.1:{port}"
        assert commands.main(["-u", url, "-n", "ns", "-d", database, "shell"]) == 1
        captured = capsys.readouterr()
        assert chunks == []
        assert captured.out == ""
        assert captured.err.splitlines()[-1] == "Shell failed: Can't connect to the database."
        assert "Traceback" not in captured.err
        assert closes == ["closed"]

    def test_ctrl_c_during_a_query(
        self,
        kind: str,
        database: str,
        closes: list[str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """AC-21, D11: Ctrl+C during a query ends the session — ``Interrupted.`` on stderr, no ``Goodbye!``, 130."""

        async def interrupted(manager: Any, line: str) -> Any:
            raise KeyboardInterrupt

        monkeypatch.setattr(commands, "_query", interrupted)
        chunks = _feed(monkeypatch, capsys, ["RETURN 1", "RETURN 2"])
        assert commands.main(self._argv(kind, database)) == 130
        captured = capsys.readouterr()
        assert len(chunks) == 1  # the banner, then no second prompt
        assert (captured.out, captured.err) == ("", "Interrupted.\n")
        assert closes == ["closed"]

    def test_query_exception_keeps_the_session(
        self,
        kind: str,
        database: str,
        closes: list[str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An exception from a query (a lost connection) is one ``Error:`` line; the next line still runs."""
        original = commands._query
        raised: list[BaseException] = [ConnectionError("connection lost"), RuntimeError("")]

        async def flaky(manager: Any, line: str) -> Any:
            if raised:
                raise raised.pop(0)
            return await original(manager, line)

        monkeypatch.setattr(commands, "_query", flaky)
        chunks = _feed(monkeypatch, capsys, ["RETURN 1", "RETURN 1", "RETURN 3"])
        assert commands.main(self._argv(kind, database)) == 0
        assert chunks[1:] == ["Error: connection lost\n", "Error: RuntimeError\n", "3\n"]
        assert capsys.readouterr().out == "Goodbye!\n"
        assert closes == ["closed"]

    def test_url_credentials_not_printed(
        self,
        kind: str,
        database: str,
        closes: list[str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """AC-21: a URL with ``user:password@`` is shown without it. The SDK's WebSocket accepts it; its HTTP client
        refuses credentials in the URL besides the sign-in (``Shell failed``) — the URL's userinfo never shows. The
        WebSocket sends them at the handshake, so they are the line's real ones."""
        clean = _migration_url(kind)
        scheme, rest = clean.split("://", 1)
        url = f"{scheme}://root:root@{rest}"
        chunks = _feed(monkeypatch, capsys, [])
        status = commands.main(["-u", url, "-n", "ns", "-d", database, "shell"])
        captured = capsys.readouterr()
        everything = "".join(chunks) + captured.out + captured.err
        assert "root:root@" not in everything
        if kind == "ws":
            assert status == 0
            _check(chunks[0], self._banner(clean, database))
        else:
            assert status == 1
            assert captured.err.splitlines()[-1].startswith("Shell failed: ")
        assert closes == ["closed"]


class TestCliEmbedded:
    """AC-22 (spec §5.11, M2): the embedded engine — ``--url mem:// shell`` answers like a 2.x server; a parse error,
    raised by the SDK as an exception there, is one ``Error:`` line."""

    def test_mem_shell(self, closes: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        chunks = _feed(monkeypatch, capsys, ["RETURN 1", "RETURN 1; SELEKT 1"])
        assert commands.main(["-u", "mem://", "-n", "ns", "-d", "db", "shell"]) == 0
        captured = capsys.readouterr()
        _check(chunks[0], ["Connected to mem://", "Namespace: ns, Database: db", *_BANNER_TAIL])
        assert chunks[1] == "1\n"
        assert chunks[2].startswith("Error: ")
        assert "Parse error" in chunks[2]
        assert (captured.out, captured.err) == ("Goodbye!\n", "")
        assert closes == ["closed"]
