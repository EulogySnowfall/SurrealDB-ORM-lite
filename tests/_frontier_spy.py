"""Runtime check of the SDK call frontier (v0.50.0), registered for the whole session by ``tests/conftest.py``.

``tests/test_sdk_frontier.py`` checks the source: it cannot see a client passed under another name, or a method reached
with ``getattr``. This plugin checks the calls themselves. For the session it wraps the data methods (``METHODS``) of
the SDK's three async connection classes; each wrapper looks at the frame that called it and records a violation when
that frame belongs to a ``surreal_orm_lite`` module other than ``surreal_orm_lite._client``. Application and test code
calling the SDK directly is allowed, and so is every call the frontier makes.

The mode comes from the ``ORM_FRONTIER_SPY`` environment variable:

- ``enforce``: the session fails when a violation was recorded, and lists them;
- ``report``: the violations are listed in the terminal summary, the session outcome is unchanged;
- ``off``: nothing is wrapped.

The default is ``enforce``: every call site of the ORM goes through the frontier.
"""

from __future__ import annotations

import functools
import os
import sys
from collections.abc import Callable, Iterable
from typing import Any

import pytest

#: The SDK data methods the wrappers watch (``merge``/``patch``/``insert_relation`` no longer exist on SDK 3.0 clients).
METHODS = ("query", "query_raw", "select", "create", "update", "upsert", "insert", "delete")
#: The package whose modules must call the SDK through the frontier, and the frontier itself.
PACKAGE = "surreal_orm_lite"
FRONTIER = "surreal_orm_lite._client"
MODES = ("enforce", "report", "off")
DEFAULT_MODE = "enforce"
ENV = "ORM_FRONTIER_SPY"


def mode_from_env(environ: dict[str, str] | os._Environ[str] = os.environ) -> str:
    """The spy's mode: ``ORM_FRONTIER_SPY`` when it names one of ``MODES``, else ``DEFAULT_MODE``."""
    value = environ.get(ENV, "").strip().lower()
    return value if value in MODES else DEFAULT_MODE


def is_violation(caller_module: str) -> bool:
    """Whether a call from *caller_module* bypasses the frontier."""
    inside = caller_module == PACKAGE or caller_module.startswith(PACKAGE + ".")
    return inside and caller_module != FRONTIER


class FrontierSpy:
    """Wraps SDK connection classes' data methods and records the ORM calls that bypass ``_client``."""

    def __init__(self) -> None:
        self.violations: dict[str, None] = {}  # insertion-ordered set of report lines
        self._originals: list[tuple[type[Any], str, Any]] = []

    def instrument(self, classes: Iterable[type[Any] | None]) -> None:
        """Wrap ``METHODS`` on each class (``None`` entries, an SDK class not installed, are skipped)."""
        for cls in classes:
            if cls is None:
                continue
            for name in METHODS:
                original = cls.__dict__.get(name)
                if original is None or not callable(original):
                    continue
                self._originals.append((cls, name, original))
                setattr(cls, name, self._wrap(cls, name, original))

    def restore(self) -> None:
        """Put every wrapped method back, in reverse order."""
        while self._originals:
            cls, name, original = self._originals.pop()
            setattr(cls, name, original)

    def _wrap(self, cls: type[Any], name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        spy = self

        @functools.wraps(original)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            caller = sys._getframe(1)
            module = str(caller.f_globals.get("__name__", ""))
            if is_violation(module):
                spy.violations[
                    f"{module}.{caller.f_code.co_name} ({caller.f_code.co_filename}:{caller.f_lineno}) "
                    f"called {cls.__name__}.{name}()"
                ] = None
            return original(*args, **kwargs)

        return wrapper


def _sdk_classes() -> tuple[type[Any] | None, ...]:
    from surreal_orm_lite import _sdk

    return (_sdk.SdkAsyncWsConnection, _sdk.SdkAsyncHttpConnection, _sdk.SdkAsyncEmbeddedConnection)


class FrontierSpyPlugin:
    """The pytest side: instrument at configure, report at the terminal summary, fail the session in ``enforce``."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.spy = FrontierSpy()

    def pytest_configure(self, config: pytest.Config) -> None:
        if self.mode != "off":
            self.spy.instrument(_sdk_classes())

    def pytest_unconfigure(self, config: pytest.Config) -> None:
        self.spy.restore()

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        if self.mode == "enforce" and self.spy.violations and exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        if not self.spy.violations:
            return
        title = "SDK calls outside surreal_orm_lite._client"
        if self.mode != "enforce":
            title += f" ({ENV}={self.mode}: not failing the session)"
        terminalreporter.section(title, sep="=")
        for line in self.spy.violations:
            terminalreporter.line(line)


def register(config: pytest.Config) -> FrontierSpyPlugin:
    """Register the plugin on *config* with the mode of the environment (called from ``tests/conftest.py``)."""
    plugin = FrontierSpyPlugin(mode_from_env())
    config.pluginmanager.register(plugin, "orm_frontier_spy")
    return plugin
