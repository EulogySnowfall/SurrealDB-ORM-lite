"""The ``surreal-orm-lite`` command line (v0.39.0): the full SurrealDB-ORM's ``surreal-orm`` CLI on ``argparse``.

``TestParser`` (spec §5.1, §5.2, §5.12; AC-1–AC-7, AC-25, AC-29's offline part): the console script declared in
``pyproject.toml``, ``python -m surreal_orm_lite.cli``, the nine commands and their help, the usage errors (exit 2),
option > environment > default, the password kept out of ``--help`` and of ``repr``, the logging handler removed after
``main()``, and ``main()`` refusing to run inside a running event loop before touching the connection registry.

CLI tests are sync ``def`` tests: ``main()`` runs its own ``asyncio.run()`` (spec §9.2).
"""

import dataclasses
import importlib
import logging
import os
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import surreal_orm_lite
from surreal_orm_lite import SurrealDBConnectionManager
from surreal_orm_lite.cli import commands

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
