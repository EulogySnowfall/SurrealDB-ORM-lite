"""The guide for AI coding assistants, ``docs/ai/CLAUDE.md`` (issue #401): its shape, and every example in it.

``TestAiGuideExamplesE2E`` runs the guide's ``python`` blocks in order, in one namespace, as a user's assistant would
read them: once against the SurrealDB line under test (a database of its own on the test server) and once on the
embedded engine (``mem://``). The guide reads its connection settings from ``SURREAL_*`` environment variables, which
the test sets; blocks that mention ``pytest`` are only compiled. The guide's examples branch on
``tx.is_interactive`` where SurrealDB 2.x and 3.x differ, so the same blocks hold on both lines.

``TestAiGuide`` pins its length, its sections and that its links are absolute (it is copied into other projects). The
links themselves, and those of ``llms.txt`` and of the README, are checked by ``tests/test_docs_links.py``.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import os
import random
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from surreal_orm_lite import SurrealDBConnectionManager
from tests._readme_tables import DOCS, headings
from tests._retry import apply_reset

GUIDE = DOCS / "ai" / "CLAUDE.md"
#: Short enough to sit in an assistant's context.
GUIDE_MAX_LINES = 300
#: The guide's ``##`` sections, in order.
SECTIONS = [
    "Setup and connection",
    "Models and idioms",
    "Querying",
    "Writing data and transactions",
    "SurrealDB 2.x, 3.x and 4.x",
    "Common mistakes",
    "Errors to catch",
    "Testing",
    "Guides",
]


def _guide() -> str:
    return GUIDE.read_text(encoding="utf-8")


def _examples() -> list[str]:
    """The guide's ``python`` blocks, in order."""
    blocks: list[str] = re.findall(r"^```python\n(.*?)^```$", _guide(), re.MULTILINE | re.DOTALL)
    assert blocks, "docs/ai/CLAUDE.md has no example"
    return blocks


@contextlib.asynccontextmanager
async def _guide_database(kind: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """``SURREAL_*`` pointing at a database of its own: ``ns``/``ai_<uuid8>`` on the test server over WebSocket, removed
    on exit (*kind* ``"server"``), or a fresh ``mem://`` engine (*kind* ``"mem"``)."""
    database = f"ai_{uuid.uuid4().hex[:8]}"
    if kind == "server":
        host = os.environ.get("SURREALDB_HOST", "localhost")
        port = os.environ.get("SURREALDB_PORT", "8000")
        url = f"ws://{host}:{port}/rpc"
    else:
        url = "mem://"
    settings = {"URL": url, "USER": "root", "PASSWORD": "root", "NAMESPACE": "ns", "DATABASE": database}
    for key, value in settings.items():
        monkeypatch.setenv(f"SURREAL_{key}", value)
    if kind == "server":
        SurrealDBConnectionManager.set_connection(url=url, user="root", password="root", namespace="ns", database=database)
        client = await SurrealDBConnectionManager.get_client()
        await apply_reset(client, f"REMOVE DATABASE IF EXISTS {database}; DEFINE DATABASE {database};")
    try:
        yield
    finally:
        if kind == "server":
            with contextlib.suppress(Exception):
                client = await SurrealDBConnectionManager.get_client()
                await client.query(f"REMOVE DATABASE IF EXISTS {database};", {})
        await SurrealDBConnectionManager.unset_connection()


class TestAiGuideExamplesE2E:
    """Every example of the guide runs as written, on the line under test and on the embedded engine."""

    @pytest.mark.parametrize("kind", ["server", "mem"])
    async def test_examples_run(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        namespace: dict[str, Any] = {"__name__": "ai_guide"}
        compiled_only = 0
        state = random.getstate()  # the guide seeds ``random``: no other test sees it
        async with _guide_database(kind, monkeypatch):
            try:
                for code in _examples():
                    compiled = compile(code, "<docs/ai/CLAUDE.md>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                    if "pytest" in code:
                        compiled_only += 1
                        continue
                    # A doctest of this repository's own guide (trusted text), top-level awaits included.
                    outcome = eval(compiled, namespace)
                    if inspect.iscoroutine(outcome):
                        await outcome
            finally:
                namespace.clear()  # no client of the examples outlives them
                random.setstate(state)
        assert compiled_only == 1

    def test_every_block_is_python_or_shell(self) -> None:
        """Only ``python`` blocks, run above, and ``bash`` blocks (installation, download)."""
        fences = re.findall(r"^```(\S*)$", _guide(), re.MULTILINE)
        assert fences and set(fences[0::2]) <= {"python", "bash"} and set(fences[1::2]) == {""}, fences

    def test_connection_comes_from_the_environment(self) -> None:
        """The first example connects with the ``SURREAL_*`` variables (those of the command line), no hard-coded
        credentials."""
        first = _examples()[0]
        assert "set_connection(" in first
        for name in ("URL", "USER", "PASSWORD", "NAMESPACE", "DATABASE"):
            assert f'os.environ["SURREAL_{name}"]' in first, name
        assert "root" not in first


class TestAiGuide:
    def test_short(self) -> None:
        lines = _guide().count("\n")
        assert lines <= GUIDE_MAX_LINES, f"docs/ai/CLAUDE.md has {lines} lines"

    def test_sections(self) -> None:
        """One ``#`` title, then the requested sections, in order."""
        found = [(level, title) for level, title, _ in headings(_guide()) if level <= 2]
        assert found[0][0] == 1 and [level for level, _ in found].count(1) == 1, found[:2]
        assert [title for _, title in found[1:]] == SECTIONS

    def test_no_relative_link(self) -> None:
        """The guide is copied into other projects: every link is absolute."""
        relative = [link for link in re.findall(r"\]\(([^)\s]+)\)", _guide()) if not re.match(r"https?://", link)]
        assert relative == []

    def test_explains_its_import(self) -> None:
        """It says how to bring it into a project: the raw file to download and the ``@`` import of Claude Code."""
        text = " ".join(_guide().split())
        assert "https://raw.githubusercontent.com/EulogySnowfall/SurrealDB-ORM-lite/main/docs/ai/CLAUDE.md" in text
        assert "`@docs/surreal-orm-lite.md`" in text
