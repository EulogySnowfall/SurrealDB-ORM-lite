"""The docs.page documentation site (issue #371): ``docs.json`` and the ``.mdx`` pages generated from the guides.

docs.page renders only ``docs/**/*.mdx``, so ``scripts/sync_docs_site.py`` writes one ``.mdx`` page next to each guide
the index ``docs/README.md`` links to, and ``docs/index.mdx`` from the index; the ``.md`` guides stay the single
source. These tests check that every page is in step with its guide, that ``docs.json``'s sidebar lists exactly the
generated pages, that every link of a page opens a page of the site (and its heading, after ``#``) or a file of the
repository, and that no page holds what MDX would read as JSX or an expression. No server, no network, no node.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests._readme_tables import DOCS, GITHUB_BLOB, GITHUB_TREE, ROOT, anchors, guide_order

SCRIPT = ROOT / "scripts" / "sync_docs_site.py"
PINS_SCRIPT = ROOT / "scripts" / "sync_surrealdb_pins.py"
DOCS_JSON = ROOT / "docs.json"
#: The documents the index lists besides its guides (its "Other documents" part).
OTHER_PAGES = ["ROADMAP.md", "ai/CLAUDE.md"]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sync_docs_site", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


site = _load()


def _pages() -> list[Path]:
    return sorted(DOCS.rglob("*.mdx"))


def _prose(text: str) -> str:
    """*text* without its fenced blocks and inline code: what MDX parses as Markdown and JSX."""
    text = re.sub(r"^ *(`{3,}|~{3,}).*?^ *\1[ \t]*$", "", text, flags=re.MULTILINE | re.DOTALL)
    return re.sub(r"(`+)(?!`).*?(?<!`)\1(?!`)", "", text, flags=re.DOTALL)


def _links(text: str) -> list[str]:
    return re.findall(r"\]\(([^)\s]+)\)", _prose(text))


def _routes() -> dict[str, Path]:
    """Each page of the site by its path (``/``, ``/models``, ``/ai/CLAUDE``)."""
    return {site.route(source): site.mirror(source) for source in site.sources()}


def _hrefs(entries: list[Any]) -> list[str]:
    """Every ``href`` of a ``docs.json`` sidebar, nested groups included, in order."""
    found = []
    for entry in entries:
        if "href" in entry:
            found.append(entry["href"])
        found += _hrefs(entry.get("pages", []))
    return found


class TestPages:
    def test_in_sync(self) -> None:
        """The check mode finds no missing, stale or orphaned page."""
        result = subprocess.run([sys.executable, "-I", str(SCRIPT)], capture_output=True, text=True, timeout=60, cwd=ROOT)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_every_listed_guide_has_a_page(self) -> None:
        """The index, every guide of its "Guides" list and its other documents each have a page; every Markdown file
        under ``docs/`` is one of them."""
        sources = site.sources()
        assert set(sources) == set(DOCS.rglob("*.md")), sorted(set(DOCS.rglob("*.md")) ^ set(sources))
        assert (DOCS / "index.mdx").is_file()
        for name in [*guide_order(), *OTHER_PAGES]:
            assert (DOCS / name).with_suffix(".mdx").is_file(), name
        assert sorted(site.mirror(source) for source in sources) == _pages()

    def test_front_matter_and_source_note(self) -> None:
        """A page opens with its title and description, then names the guide to edit."""
        for source in site.sources():
            text = site.mirror(source).read_text(encoding="utf-8")
            relative = source.relative_to(ROOT).as_posix()
            match = re.match(r'---\ntitle: (".*")\ndescription: (".*")\n---\n\n\{/\* (.*) \*/\}\n\n# ', text)
            assert match is not None, relative
            title, description, note = json.loads(match.group(1)), json.loads(match.group(2)), match.group(3)
            assert title == site.title(source.read_text(encoding="utf-8")), relative
            assert title and not re.search(r"[`*\[\]]", title), (relative, title)
            assert 0 < len(description) <= site.DESCRIPTION_MAX + 1, (relative, description)
            assert note == f"Generated from {relative} by scripts/sync_docs_site.py. Edit the .md source.", note

    @pytest.mark.parametrize("page", _pages(), ids=lambda path: path.relative_to(DOCS).as_posix())
    def test_no_relative_link(self, page: Path) -> None:
        """A page links to a site path (``/…``), a heading of itself (``#…``) or an absolute URL, never to a ``.md``
        file next to it."""
        relative = [link for link in _links(page.read_text(encoding="utf-8")) if not re.match(r"(/|#|https?://)", link)]
        assert relative == []

    @pytest.mark.parametrize("page", _pages(), ids=lambda path: path.relative_to(DOCS).as_posix())
    def test_links_resolve(self, page: Path) -> None:
        """A site link names a page and, after ``#``, one of its headings (GitHub's slugs); a repository link names a
        file or folder of the checkout."""
        routes = _routes()
        text = page.read_text(encoding="utf-8")
        broken = []
        for link in _links(text):
            if link.startswith(("/", "#")):
                path, _, fragment = link.partition("#")
                target = routes.get(path) if path else page
                if target is None or fragment and fragment not in anchors(target.read_text(encoding="utf-8")):
                    broken.append(link)
            elif link.startswith((GITHUB_BLOB, GITHUB_TREE)):
                path = link.removeprefix(GITHUB_BLOB).removeprefix(GITHUB_TREE).partition("#")[0]
                if not (ROOT / path).exists():
                    broken.append(link)
        assert broken == []

    def test_cross_page_links_are_checked(self) -> None:
        """The index links to the guides' headings: the check above has links with a fragment to resolve."""
        links = _links((DOCS / "index.mdx").read_text(encoding="utf-8"))
        assert "/models#custom-primary-key" in links and "/getting-started#1-configure-the-connection" in links
        assert f"{GITHUB_BLOB}CHANGELOG.md" in links and f"{GITHUB_TREE}examples/" in links

    @pytest.mark.parametrize("page", _pages(), ids=lambda path: path.relative_to(DOCS).as_posix())
    def test_no_mdx_hazard(self, page: Path) -> None:
        """Outside code, every ``<``, ``{`` and ``}`` is escaped, but for the page's one comment, ``{/* … */}``."""
        text = page.read_text(encoding="utf-8")
        body = _prose(text.split("\n---\n", 1)[1])
        assert body.count("{/*") == 1
        body = re.sub(r"\{/\*.*?\*/\}", "", body, count=1)
        hazards = [line for line in body.split("\n") if re.search(r"(?<!\\)[<{}]", line)]
        assert hazards == []


class TestDocsJson:
    def test_shape(self) -> None:
        config = json.loads(DOCS_JSON.read_text(encoding="utf-8"))
        assert config["$schema"] == "https://docs.page/schema.json"
        assert config["name"] == "Surreal ORM Lite" and config["description"]
        assert config["social"] == {"github": "EulogySnowfall/SurrealDB-ORM-lite"}
        assert all(group["group"] and group["pages"] for group in config["sidebar"])

    def test_sidebar_lists_exactly_the_pages(self) -> None:
        """Every generated page once, and nothing else."""
        hrefs = _hrefs(json.loads(DOCS_JSON.read_text(encoding="utf-8"))["sidebar"])
        assert len(hrefs) == len(set(hrefs)), hrefs
        assert set(hrefs) == set(_routes()), sorted(set(hrefs) ^ set(_routes()))


class TestConversion:
    """``scripts/sync_docs_site.py`` on short texts: links, code, MDX hazards, front matter."""

    def test_links(self) -> None:
        text = (
            "[a](models.md#custom-primary-key) [b](README.md) [c](ai/CLAUDE.md) [d](../CHANGELOG.md#unreleased) "
            '[e](../examples/) [f](https://example.com/x.md) [g](#local) [h](models.md "Models")'
        )
        assert site.convert(text, DOCS / "querying.md") == (
            f"[a](/models#custom-primary-key) [b](/) [c](/ai/CLAUDE) [d]({GITHUB_BLOB}CHANGELOG.md#unreleased) "
            f'[e]({GITHUB_TREE}examples/) [f](https://example.com/x.md) [g](#local) [h](/models "Models")'
        )

    def test_links_from_a_subfolder(self) -> None:
        text = "[a](../models.md#custom-primary-key) [b](../README.md) [c](../../SECURITY.md) [d](CLAUDE.md#top)"
        assert site.convert(text, DOCS / "ai" / "CLAUDE.md") == (
            f"[a](/models#custom-primary-key) [b](/) [c]({GITHUB_BLOB}SECURITY.md) [d](/ai/CLAUDE#top)"
        )

    def test_a_link_out_of_the_repository_fails(self) -> None:
        with pytest.raises(ValueError, match="leaves the repository"):
            site.convert("[a](../../../elsewhere.md)", DOCS / "models.md")

    def test_code_is_left_as_written(self) -> None:
        """Fenced blocks (backticks or tildes, indented in a list) and code spans keep their text; prose around them
        is converted."""
        text = "\n".join(
            [
                "Use `a < b` and ``x `{y}` z`` but not a < b or {c}: [m](models.md).",
                "```python",
                "x = {'a': 1} if a < b else fns[0](models.md)",
                "```",
                "- item",
                "  ~~~~",
                "  <tag> {x}",
                "  ~~~",
                "  still {code}",
                "  ~~~~",
                "after {d}",
            ]
        )
        assert site.convert(text, DOCS / "querying.md").split("\n") == [
            "Use `a < b` and ``x `{y}` z`` but not a \\< b or \\{c\\}: [m](/models).",
            "```python",
            "x = {'a': 1} if a < b else fns[0](models.md)",
            "```",
            "- item",
            "  ~~~~",
            "  <tag> {x}",
            "  ~~~",
            "  still {code}",
            "  ~~~~",
            "after \\{d\\}",
        ]

    def test_a_code_span_never_crosses_a_blank_line(self) -> None:
        """A lone backtick does not open a span that would hide the next paragraph's hazards."""
        assert site.convert("a ` b\n\nc <d", DOCS / "models.md") == "a ` b\n\nc \\<d"

    def test_escaped_characters_stay_escaped(self) -> None:
        assert site.convert("\\{a\\} \\< b", DOCS / "models.md") == "\\{a\\} \\< b"

    def test_html_comment_becomes_an_mdx_comment(self) -> None:
        assert site.convert("a <!-- note */ here --> b", DOCS / "models.md") == "a {/* note * / here */} b"

    def test_title_and_description(self) -> None:
        text = "# The `Q` guide\n\n> **Short** [summary](models.md) of `Q`.\n> Second line.\n\n## Part\n"
        assert site.title(text) == "The Q guide"
        assert site.description(text) == "Short summary of Q. Second line."

    def test_a_long_description_is_cut(self) -> None:
        sentence = "word " * 35 + "end."
        assert site.description(f"# T\n\n{sentence} More text here to pass the limit.\n") == " ".join(sentence.split())
        long = "word " * 80
        cut = site.description(f"# T\n\n{long}\n")
        assert cut.endswith("…") and len(cut) <= site.DESCRIPTION_MAX + 1

    def test_a_table_or_heading_is_not_the_description(self) -> None:
        assert site.description("# T\n\n## Part\n\n| a | b |\n\nThe text.\n") == "The text."


class TestCheckAndFix:
    """The check mode reports each missing, stale and orphaned page; ``--fix`` rewrites and removes them."""

    def test_drift_then_fix(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        shutil.copytree(DOCS, tmp_path / "docs")
        assert site.main([], root=tmp_path) == 0
        with (tmp_path / "docs" / "models.md").open("a", encoding="utf-8") as handle:
            handle.write("\nA new line {added}.\n")
        (tmp_path / "docs" / "cli.mdx").unlink()
        (tmp_path / "docs" / "extra.mdx").write_text("# Extra\n", encoding="utf-8")
        capsys.readouterr()
        assert site.main([], root=tmp_path) == 1
        out = capsys.readouterr().out
        for entry in ("stale docs/models.mdx", "missing docs/cli.mdx", "orphan docs/extra.mdx", "--fix"):
            assert entry in out, out
        assert site.main(["--fix"], root=tmp_path) == 0
        assert site.drift(tmp_path) == []
        assert not (tmp_path / "docs" / "extra.mdx").exists()
        assert "A new line \\{added\\}." in (tmp_path / "docs" / "models.mdx").read_text(encoding="utf-8")

    def test_a_pin_bump_carries_the_pages(self, tmp_path: Path) -> None:
        """``sync_surrealdb_pins.py --fix`` (what the unattended version monitor runs) rewrites the pages it changes the
        guides of, so its pull request stays in step."""
        for name in ("README.md", ".surrealdb-version", ".surrealdb-version2x", ".surrealdb-version4x"):
            shutil.copy2(ROOT / name, tmp_path / name)
        for folder in ("docs", "scripts"):
            shutil.copytree(ROOT / folder, tmp_path / folder, ignore=shutil.ignore_patterns("__pycache__"))
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
        shutil.copy2(ROOT / ".github" / "workflows" / "ci.yml", tmp_path / ".github" / "workflows" / "ci.yml")
        (tmp_path / ".surrealdb-version4x").write_text("4.0.99\n", encoding="utf-8")
        script = tmp_path / "scripts" / PINS_SCRIPT.name
        result = subprocess.run([sys.executable, "-I", str(script), "--fix"], capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "**v4.0.99**" in (tmp_path / "docs" / "getting-started.md").read_text(encoding="utf-8")
        assert "**v4.0.99**" in (tmp_path / "docs" / "getting-started.mdx").read_text(encoding="utf-8")
        assert site.drift(tmp_path) == []
