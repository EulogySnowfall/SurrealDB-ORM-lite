"""The docs.page documentation site (issue #371): ``docs.json`` and the ``.mdx`` pages generated from the guides.

docs.page renders only ``docs/**/*.mdx``, so ``scripts/sync_docs_site.py`` writes one ``.mdx`` page next to each guide
the index ``docs/README.md`` links to, and ``docs/index.mdx`` from the index; the ``.md`` guides stay the single
source. These tests check that every page is in step with its guide, that ``docs.json``'s sidebar lists exactly the
generated pages, that every link of a page opens a page of the site (and its heading, after ``#``) or a file of the
repository, and that no page holds what MDX would read as JSX or an expression. No server, no network, no node.

docs.page anchors a heading its own way, not GitHub's (``server_values`` gives ``servervalues``, ``Relations & Graph``
gives ``relations-graph``): the pages' links carry docs.page's anchors, checked with the script's port of its slugger,
which :class:`TestAnchors` pins to the ids the live site gave.
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

from tests._readme_tables import DOCS, GITHUB_BLOB, GITHUB_TREE, ROOT, guide_order

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
        """A page opens with its front matter: its title, its description and a YAML comment naming the guide to edit
        (docs.page would show an MDX comment as text)."""
        for source in site.sources():
            text = site.mirror(source).read_text(encoding="utf-8")
            relative = source.relative_to(ROOT).as_posix()
            match = re.match(r'---\ntitle: (".*")\ndescription: (".*")\n# (.*)\n---\n\n# ', text)
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
        """A site link names a page and, after ``#``, one of its headings (docs.page's anchors); a repository link
        names a file or folder of the checkout."""
        routes = _routes()
        text = page.read_text(encoding="utf-8")
        broken = []
        for link in _links(text):
            if link.startswith(("/", "#")):
                path, _, fragment = link.partition("#")
                target = routes.get(path) if path else page
                if target is None or fragment and fragment not in site.site_anchors(target.read_text(encoding="utf-8")):
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
        assert "/writing#14-optimistic-concurrency-retryonconflict" in links, "a GitHub anchor left in a page"
        assert f"{GITHUB_BLOB}CHANGELOG.md" in links and f"{GITHUB_TREE}examples/" in links

    @pytest.mark.parametrize("page", _pages(), ids=lambda path: path.relative_to(DOCS).as_posix())
    def test_no_mdx_hazard(self, page: Path) -> None:
        """Outside code, no ``<`` (written ``&lt;``) and no unescaped ``{`` or ``}``: nothing MDX would read as JSX, an
        expression or an HTML comment."""
        text = page.read_text(encoding="utf-8")
        body = _prose(text.split("\n---\n", 1)[1])
        hazards = [line for line in body.split("\n") if re.search(r"<|(?<!\\)[{}]", line)]
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
            "Use `a < b` and ``x `{y}` z`` but not a &lt; b or \\{c\\}: [m](/models).",
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
        assert site.convert("a ` b\n\nc <d", DOCS / "models.md") == "a ` b\n\nc &lt;d"

    def test_escaped_characters(self) -> None:
        """An escaped brace stays escaped; an escaped ``<`` becomes ``&lt;``, as docs.page would show ``\\&lt;``."""
        assert site.convert("\\{a\\} \\< b <c", DOCS / "models.md") == "\\{a\\} &lt; b &lt;c"

    def test_html_comment_is_dropped(self) -> None:
        """MDX refuses an HTML comment and GitHub does not show it: the page leaves it out."""
        assert site.convert("a <!-- note\nhere --> b `<!-- kept -->`", DOCS / "models.md") == "a  b `<!-- kept -->`"

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


class TestAnchors:
    """The port of docs.page's heading anchors, pinned to the ids its site gave these headings (fetched on 2026-11-19),
    and the translation of a guide's GitHub anchors."""

    @pytest.mark.parametrize(
        ("title", "anchor"),
        [
            ("Custom Primary Key", "custom-primary-key"),
            ("Custom table name (`table_name`)", "custom-table-name-tablename"),
            ("15. Server-side values: `SurrealFunc` + `server_values=`", "15-server-side-values-surrealfunc-servervalues"),
            ("16. Computed fields: `Computed[...]` → `DEFINE FIELD … VALUE`", "16-computed-fields-computed-define-field-value"),
            (
                "20. Field aliases, `server_fields` and `merge(refresh=False)`",
                "20-field-aliases-serverfields-and-mergerefreshfalse",
            ),
            ("7. Relations & Graph", "7-relations-graph"),
            ("12. Upsert & get_or_create / update_or_create", "12-upsert-getorcreate-updateorcreate"),
            ("13. Patch & atomic field/array operations", "13-patch-atomic-fieldarray-operations"),
            ("_underscore_ emphasis", "underscore-emphasis"),
            ("!!!", "section"),
        ],
    )
    def test_site_anchor(self, title: str, anchor: str) -> None:
        assert site.site_anchor(title) == anchor

    def test_repeated_and_fenced_headings(self) -> None:
        text = "# A\n\n## Part\n\n```\n## code\n```\n\n~~~~\n```\n## still code\n~~~~\n\n## Part\n"
        assert site.site_anchors(text) == ["a", "part", "part-1"]

    def test_anchor_map(self) -> None:
        text = "# Guide\n\n## 1. `snake_case` & more\n\n## Part\n\n## Part\n"
        assert site.anchor_map(text) == {
            "guide": "guide",
            "1-snake_case--more": "1-snakecase-more",
            "part": "part",
            "part-1": "part-1",
        }

    def test_links_carry_the_site_anchor(self) -> None:
        """A link to a heading of another guide or of the same one carries docs.page's anchor; an unknown fragment is
        left as written (the page test then reports it)."""
        text = "[a](writing.md#14-optimistic-concurrency-retry_on_conflict) [b](#custom-table-name-table_name) [c](models.md#nowhere)"
        assert site.convert(text, DOCS / "models.md") == (
            "[a](/writing#14-optimistic-concurrency-retryonconflict) [b](#custom-table-name-tablename) [c](/models#nowhere)"
        )


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
