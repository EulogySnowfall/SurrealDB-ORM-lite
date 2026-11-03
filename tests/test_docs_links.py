"""The documentation's shape and links (issue #400): a short README pointing to the guides under ``docs/``.

No server (the quick start runs on ``mem://``). The README is also PyPI's long description, where a relative link leads nowhere: it links to the guides by
absolute GitHub URLs, and each must name a file of the repository and, after ``#``, one of its headings. Every
relative link of ``docs/**/*.md`` must name a file and a heading in the same way. The index ``docs/README.md`` reaches
every guide and lists the manual's numbered sections, 1 to the last, each once and in order.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path
from typing import Any

import pytest

from surreal_orm_lite import SurrealDBConnectionManager
from tests._readme_tables import (
    DOCS,
    GITHUB_BLOB,
    GITHUB_TREE,
    README,
    ROOT,
    anchors,
    doc_part,
    guide,
    guide_order,
    guide_paths,
    headings,
    readme,
    unresolved_links,
)

#: The README stays a front page: about 300 lines at most.
README_MAX_LINES = 300
#: The last numbered section of the manual.
LAST_SECTION = 41
#: Where ``llms.txt`` and the AI guide's download point: the raw Markdown of a file of ``main``.
GITHUB_RAW = "https://raw.githubusercontent.com/EulogySnowfall/SurrealDB-ORM-lite/main/"
#: The guide for AI coding assistants (issue #401) and the llms.txt index at the repository root.
AI_GUIDE = DOCS / "ai" / "CLAUDE.md"
LLMS_TXT = ROOT / "llms.txt"


def _markdown_files() -> list[Path]:
    return sorted(DOCS.rglob("*.md"))


def _links(text: str) -> list[str]:
    return re.findall(r"\]\(([^)\s]+)\)", text)


def _repository_links(text: str) -> list[str]:
    """The links of *text* into this repository's ``main``: a page (``blob``), a folder (``tree``) or a raw file."""
    return [link for link in _links(text) if link.startswith((GITHUB_BLOB, GITHUB_TREE, GITHUB_RAW))]


def _broken(links: list[str]) -> list[str]:
    """The repository *links* that name no file (or folder) of the checkout, or, after ``#``, no heading of it."""
    broken = []
    for link in links:
        path, _, fragment = link.removeprefix(GITHUB_BLOB).removeprefix(GITHUB_TREE).removeprefix(GITHUB_RAW).partition("#")
        target = ROOT / path
        if not target.exists() or fragment and fragment not in anchors(target.read_text(encoding="utf-8")):
            broken.append(link)
    return broken


def _quick_start() -> str:
    """The README's quick start, the one ``python`` block of its "Quick start" part."""
    blocks = re.findall(r"^```python\n(.*?)^```$", doc_part("## Quick start"), re.MULTILINE | re.DOTALL)
    assert len(blocks) == 1, blocks
    return blocks[0]


class TestReadmeQuickStartE2E:
    """The quick start runs as written, on ``mem://`` in place of its server URL, ``main()`` awaited in place of
    ``asyncio.run()`` (the test already runs in an event loop)."""

    async def test_runs(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = _quick_start()
        server = 'url="http://localhost:8000", user="root", password="root"'
        assert code.count(server) == 1 and code.rstrip().endswith("asyncio.run(main())")
        code = code.replace(server, 'url="mem://"').replace("asyncio.run(main())", "await main()")
        namespace: dict[str, Any] = {"__name__": "readme_quick_start"}
        try:
            compiled = compile(code, "<README quick start>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
            # A doctest of this repository's own README (trusted text), top-level awaits included.
            outcome = eval(compiled, namespace)
            if inspect.iscoroutine(outcome):
                await outcome
        finally:
            namespace.clear()
            await SurrealDBConnectionManager.unset_connection()
        printed = capsys.readouterr().out
        assert printed.startswith("[User(id='alice'") and printed.rstrip().endswith(" 23.5"), printed

    def test_about_forty_lines(self) -> None:
        assert 25 <= _quick_start().count("\n") <= 45


class TestReadme:
    def test_short(self) -> None:
        lines = readme().count("\n")
        assert lines <= README_MAX_LINES, f"README.md has {lines} lines"

    def test_no_relative_link(self) -> None:
        """PyPI renders the README away from the repository: every link is absolute."""
        relative = [link for link in _links(readme()) if not re.match(r"https?://", link)]
        assert relative == []

    def test_repository_links_resolve(self) -> None:
        """A link into this repository names a file (or folder) of ``main`` and, after ``#``, one of its headings."""
        links = _repository_links(readme())
        assert len(links) >= 18, links
        assert _broken(links) == []

    def test_links_every_guide(self) -> None:
        linked = {link.removeprefix(f"{GITHUB_BLOB}docs/").partition("#")[0] for link in _links(readme())}
        assert set(guide_order()) - {"contributing.md"} <= linked, sorted(set(guide_order()) - linked)
        assert {"README.md", "contributing.md", "compatibility.md"} <= linked


class TestGuides:
    @pytest.mark.parametrize("path", _markdown_files(), ids=lambda path: path.name)
    def test_relative_links_resolve(self, path: Path) -> None:
        """Every relative link names a file, and a heading of it after ``#``."""
        text = path.read_text(encoding="utf-8")
        assert unresolved_links(text, path) == []

    def test_unresolved_links_finds_a_broken_link(self, tmp_path: Path) -> None:
        """The check itself: a missing file, a missing heading and a missing local heading are each reported."""
        page = tmp_path / "page.md"
        (tmp_path / "other.md").write_text("# Other\n\n## Part two\n", encoding="utf-8")
        text = "[a](other.md#part-two) [b](other.md#part-three) [c](missing.md) [d](#nowhere) [e](#page)"
        page.write_text(f"# Page\n\n{text}\n", encoding="utf-8")
        assert unresolved_links(text, page) == ["other.md#part-three", "missing.md", "#nowhere"]

    def test_repeated_heading_anchors(self) -> None:
        """GitHub numbers a repeated heading's anchor from the second one: ``-1``, ``-2``."""
        assert anchors("## Security\n\n## Security\n\n```\n## code\n```\n\n## Security\n") == {
            "security",
            "security-1",
            "security-2",
        }

    @pytest.mark.parametrize("path", guide_paths(), ids=lambda path: path.name)
    def test_one_title(self, path: Path) -> None:
        """One ``#`` title, on the first line; no heading skips a level."""
        found = headings(path.read_text(encoding="utf-8"))
        assert [level for level, _, _ in found].count(1) == 1 and found[0][0] == 1 and found[0][2] == 0, found[:2]
        skips = [(above, below) for above, below in zip(found, found[1:], strict=False) if below[0] > above[0] + 1]
        assert skips == []

    def test_index_reaches_every_guide(self) -> None:
        """The index's "Guides" list names every guide, once."""
        order = guide_order()
        assert len(order) == len(set(order))
        assert sorted(order) == sorted(path.name for path in guide_paths() if path.name != "README.md")

    def test_index_lists_the_sections_in_order(self) -> None:
        """ "Sections by number" lists sections 1 to the last, in order, each linked to the one guide holding it."""
        rows = re.findall(r"^\| \[(\d+)\. .+?\]\(([\w-]+\.md)#([^)]+)\) +\|", guide("README.md"), re.MULTILINE)
        assert [int(number) for number, _, _ in rows] == list(range(1, LAST_SECTION + 1))
        for number, name, fragment in rows:
            holders = [
                path.name
                for path in guide_paths()
                if any(re.match(rf"{number}\. ", title) for level, title, _ in headings(guide(path.name)) if level > 1)
            ]
            assert holders == [name], (number, holders)
            assert fragment in anchors(guide(name)), (number, fragment)

    def test_readme_is_not_a_guide(self) -> None:
        assert README not in guide_paths()


class TestAiGuideAndLlmsTxt:
    """The guide for AI coding assistants, ``llms.txt`` and the README part that points to them (issue #401)."""

    def test_ai_guide_links_resolve(self) -> None:
        """Every link of the guide into this repository names a file of ``main`` and, after ``#``, one of its
        headings; the guide links to every guide and to the compatibility tables."""
        text = AI_GUIDE.read_text(encoding="utf-8")
        links = _repository_links(text)
        assert _broken(links) == []
        linked = {link.removeprefix(f"{GITHUB_BLOB}docs/").partition("#")[0] for link in links}
        assert set(guide_order()) | {"README.md"} <= linked, sorted(set(guide_order()) - linked)
        assert f"{GITHUB_BLOB}docs/compatibility.md#orm-behaviour-surrealdb-2x-vs-3x" in links

    def test_llms_txt_shape(self) -> None:
        """The llms.txt convention: a ``#`` title, a ``>`` summary, then ``##`` sections of ``- [name](url): notes``
        items."""
        lines = LLMS_TXT.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "# Surreal ORM Lite"
        assert lines[1] == "" and lines[2].startswith("> "), lines[:3]
        sections = [line for line in lines if line.startswith("## ")]
        assert sections[:1] == ["## Docs"] and sections[-1] == "## Optional", sections
        items = [line for line in lines if line.startswith("- ")]
        assert items and all(re.fullmatch(r"- \[[^]]+\]\(https://\S+\)(: .+)?", item) for item in items), items

    def test_llms_txt_links_resolve(self) -> None:
        """Its links are raw Markdown files of ``main`` that exist: the README, every guide and the AI guide."""
        text = LLMS_TXT.read_text(encoding="utf-8")
        links = _links(text)
        assert links and _broken(_repository_links(text)) == []
        assert {f"{GITHUB_RAW}README.md", f"{GITHUB_RAW}docs/ai/CLAUDE.md", f"{GITHUB_RAW}docs/README.md"} <= set(links)
        guides = {link.removeprefix(f"{GITHUB_RAW}docs/") for link in links}
        assert set(guide_order()) <= guides, sorted(set(guide_order()) - guides)

    def test_readme_part(self) -> None:
        """A few lines in the README: what the guide is, how to import it, and ``llms.txt``."""
        section = doc_part("## Using with AI assistants")
        assert section.count("\n") <= 15, section
        assert f"{GITHUB_BLOB}docs/ai/CLAUDE.md" in section and f"{GITHUB_BLOB}llms.txt" in section
        assert "@docs/surreal-orm-lite.md" in section

    def test_index_links_the_ai_guide(self) -> None:
        """The documentation index lists the AI guide among its other documents."""
        assert "(ai/CLAUDE.md)" in doc_part("## Other documents")
