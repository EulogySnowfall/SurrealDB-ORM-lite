"""The opt-in pytest plugin ``surreal_orm_lite.testing.pytest_plugin`` (v0.40.0): ``surreal_db`` and ``surreal_db_url``.

The tests are **sync**: each one writes a small inner project (``pytest.ini`` in pytest-asyncio strict or auto
mode, a ``conftest.py`` that opts into the plugin, an inner test file) and runs it with
``pytester.runpytest_inprocess()``. The inner sessions only open embedded engines (``mem://`` or ``surrealkv://``
under the inner ``tmp_path``), so no server is needed; the outer default connection is configured with
never-connected server settings to prove the plugin puts them back.
"""

import importlib
import inspect
import sys
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from surreal_orm_lite import SurrealDBConnectionManager

pytest_plugins = ["pytester"]

_MODES = ["strict", "auto"]
_SERVER = ("ws://127.0.0.1:1/rpc", "root", "root", "ns", "db")
_INNER_ARGS = ("-p", "no:randomly", "-p", "no:cacheprovider", "-p", "no:cov")
_PLUGIN = "surreal_orm_lite.testing.pytest_plugin"

_NOTE_MODEL = """
import pytest
from surreal_orm_lite import BaseSurrealModel, SurrealConfigDict


class Note(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="fx_plugin_note")

    id: str | None = None
    text: str = ""
"""

_TWO_EMPTY_DATABASES = (
    _NOTE_MODEL
    + """

@pytest.mark.asyncio
async def test_first(surreal_db):
    assert await Note.objects().count() == 0
    await Note(text="first").save()
    assert await Note.objects().count() == 1


@pytest.mark.asyncio
async def test_second(surreal_db):
    assert await Note.objects().count() == 0
    await Note(text="second").save()
    assert await Note.objects().count() == 1
"""
)


def _project(pytester: pytest.Pytester, mode: str, test_source: str, conftest_extra: str = "") -> None:
    """Write the inner project: ``pytest.ini`` in *mode*, a conftest opting into the plugin, one test file."""
    pytester.makeini(
        f"""
        [pytest]
        asyncio_mode = {mode}
        asyncio_default_fixture_loop_scope = function
        """
    )
    pytester.makeconftest(f'pytest_plugins = ["{_PLUGIN}"]\n{conftest_extra}')
    pytester.makepyfile(test_inner=test_source)


def _run(pytester: pytest.Pytester) -> pytest.RunResult:
    return pytester.runpytest_inprocess(*_INNER_ARGS)


def _settings() -> dict[str, Any]:
    """The outer default connection's settings and state, as public getters report them."""
    manager = SurrealDBConnectionManager
    return {
        "kwargs": manager.get_connection_kwargs(),
        "password_set": manager.is_password_set(),
        "session": manager.get_session_token(),
        "refresh": manager.get_refresh_token(),
        "embedded": manager.is_embedded(),
        "set": manager.is_connection_set(),
    }


@pytest_asyncio.fixture
async def outer_server_settings() -> AsyncIterator[dict[str, Any]]:
    """Configure (never connect) the outer default connection; yield its snapshot; unset it afterwards."""
    SurrealDBConnectionManager.set_connection(*_SERVER)
    try:
        yield _settings()
    finally:
        await SurrealDBConnectionManager.unset_connection()


class TestPytestPlugin:
    """AC-16: the plugin's fixtures in inner pytest sessions, in pytest-asyncio strict and auto modes."""

    @pytest.mark.parametrize("mode", _MODES)
    def test_each_test_gets_a_fresh_empty_database(self, pytester: pytest.Pytester, mode: str) -> None:
        _project(pytester, mode, _TWO_EMPTY_DATABASES)
        _run(pytester).assert_outcomes(passed=2)

    @pytest.mark.parametrize("mode", _MODES)
    def test_surreal_db_yields_none(self, pytester: pytest.Pytester, mode: str) -> None:
        _project(
            pytester,
            mode,
            """
import pytest
from surreal_orm_lite import SurrealDBConnectionManager


@pytest.mark.asyncio
async def test_none(surreal_db):
    assert surreal_db is None
    assert SurrealDBConnectionManager.is_embedded()
    client = await SurrealDBConnectionManager.get_client()
    assert client is not None
""",
        )
        _run(pytester).assert_outcomes(passed=1)

    @pytest.mark.parametrize("mode", _MODES)
    def test_surreal_db_url_can_be_overridden(self, pytester: pytest.Pytester, mode: str) -> None:
        _project(
            pytester,
            mode,
            _TWO_EMPTY_DATABASES
            + """

def test_the_store_is_on_disk(surreal_db_url, tmp_path):
    assert surreal_db_url == f"surrealkv://{tmp_path}/db"
""",
            conftest_extra="""
import pytest


@pytest.fixture
def surreal_db_url(tmp_path):
    return f"surrealkv://{tmp_path}/db"
""",
        )
        _run(pytester).assert_outcomes(passed=3)

    @pytest.mark.parametrize("mode", _MODES)
    def test_a_server_url_is_a_setup_error(self, pytester: pytest.Pytester, mode: str) -> None:
        _project(
            pytester,
            mode,
            """
import pytest


@pytest.mark.asyncio
async def test_never_runs(surreal_db):
    raise AssertionError("the test body must not run")
""",
            conftest_extra="""
import pytest


@pytest.fixture
def surreal_db_url():
    return "ws://localhost:1/rpc"
""",
        )
        result = _run(pytester)
        result.assert_outcomes(errors=1)
        result.stdout.fnmatch_lines(["*embedded_database() needs an embedded URL*"])
        result.stdout.no_fnmatch_line("*the test body must not run*")

    @pytest.mark.parametrize("mode", _MODES)
    def test_the_outer_connection_settings_come_back(
        self, pytester: pytest.Pytester, mode: str, outer_server_settings: dict[str, Any]
    ) -> None:
        _project(pytester, mode, _TWO_EMPTY_DATABASES)
        _run(pytester).assert_outcomes(passed=2)
        assert _settings() == outer_server_settings
        assert not SurrealDBConnectionManager.is_embedded()

    def test_the_plugin_registers_only_its_two_fixtures(self) -> None:
        plugin = importlib.import_module(_PLUGIN)
        hooks = [name for name, value in vars(plugin).items() if name.startswith("pytest_") and inspect.isfunction(value)]
        assert hooks == []
        assert sorted(name for name in vars(plugin) if name.startswith("surreal_")) == ["surreal_db", "surreal_db_url"]
        assert plugin.__doc__ is not None and f'pytest_plugins = ["{_PLUGIN}"]' in plugin.__doc__

    def test_importing_it_without_pytest_asyncio_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "pytest_asyncio", None)
        monkeypatch.delitem(sys.modules, _PLUGIN, raising=False)
        with pytest.raises(ImportError) as excinfo:
            importlib.import_module(_PLUGIN)
        assert str(excinfo.value) == f"{_PLUGIN} needs pytest-asyncio: pip install pytest-asyncio"
        assert isinstance(excinfo.value.__cause__, ImportError)
        assert _PLUGIN not in sys.modules
