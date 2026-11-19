#!/usr/bin/env python3
r"""Generate the docs.page site's ``.mdx`` pages from the Markdown guides under ``docs/``.

The guides ``docs/**/*.md`` are the single source of the documentation: GitHub renders them, the README and
``llms.txt`` link to them, and the tests read them. The docs.page site (configured by ``docs.json`` at the repository
root) renders only ``docs/**/*.mdx``, so this script writes one ``.mdx`` mirror next to each guide the index
``docs/README.md`` links to, plus ``docs/index.mdx`` (the site's home page) from the index itself.

A mirror is the guide's text with:

- a YAML front matter: ``title`` from the first ``#`` heading, ``description`` from the first paragraph, and a YAML
  comment naming the guide to edit (docs.page shows an MDX comment as text, but never the front matter);
- its relative links rewritten for the site (outside code spans and fenced blocks only): ``models.md#custom-primary-key``
  becomes ``/models#custom-primary-key``, ``README.md`` becomes ``/``, ``ai/CLAUDE.md`` becomes ``/ai/CLAUDE``, and a
  link that leaves ``docs/`` (``../CHANGELOG.md``, ``../examples/``) becomes an absolute GitHub URL of ``main``;
- the heading a link names after ``#`` given docs.page's anchor in place of GitHub's
  (``writing.md#14-optimistic-concurrency-retry_on_conflict`` becomes
  ``/writing#14-optimistic-concurrency-retryonconflict``);
- the characters MDX reads as JSX or expressions escaped outside code: ``<`` as ``&lt;`` (docs.page turns a ``<`` that
  starts no tag into ``&lt;`` before parsing, so ``\<`` would show its backslash), ``{`` and ``}`` as ``\{`` and ``\}``;
  an HTML comment, which MDX refuses and GitHub does not show, is dropped.
- a code span holding a ``<`` written as a ``<code>`` element whose text is escaped (``&lt;``, ``&#123;``…): docs.page
  turns that ``<`` into ``&lt;`` before parsing and a code span shows the entity as written, where the element's text
  decodes it. docs.page cuts a paragraph around such an element into several, so a paragraph holding one (not a list,
  a table or a quote, which docs.page renders whole) is wrapped in a ``<div>``, which it renders whole.

A fenced block is copied as written. docs.page would show a ``<`` of one that starts no tag (``x < 0``, ``<-likes-``) as
``&lt;``, and an HTML ``<pre>`` would lose its highlighting, so such a line stops the script with an error naming the
guide and the line: the guide's line is rewritten without it (``0 > x``).

Usage:
    python scripts/sync_docs_site.py          # Check only (exit 1 if a mirror is missing, stale or orphaned)
    python scripts/sync_docs_site.py --fix    # Rewrite every mirror from its guide

The script is stdlib-only: ``scripts/sync_surrealdb_pins.py --fix`` runs it after rewriting the pins in the guides, so
an unattended pin bump carries the mirrors along.
"""

from __future__ import annotations

import html
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Where a link that leaves ``docs/`` points: the file (``blob``) or folder (``tree``) on ``main``, the branch the
#: docs.page site serves.
GITHUB_BLOB = "https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/"
GITHUB_TREE = "https://github.com/EulogySnowfall/SurrealDB-ORM-lite/tree/main/"
#: The index of the guides, rendered as the site's home page.
INDEX = "README.md"
HOME = "index.mdx"
#: A front matter description longer than this is cut at its first sentence, or at a word.
DESCRIPTION_MAX = 200

_FENCE = re.compile(r"^ {0,3}(?:> ?)*\s*(`{3,}|~{3,})")
_HEADING = re.compile(r"^#{1,6} ")
#: Outside code: an HTML comment, a link target (``](target)``, with an optional title), or a character MDX would read
#: as JSX or an expression (unless already escaped). A comment ends as HTML parses it: at the first ``-->`` or
#: ``--!>``, and ``<!-->`` and ``<!--->`` are empty comments.
_TOKEN = re.compile(
    r"<!--(?P<comment>-?>|[\s\S]*?--!?>)|\]\((?P<target>[^)\s]+)(?P<title>\s+\"[^\"]*\")?\)|(?P<char>\\?<|(?<!\\)[{}])"
)
_SCHEME = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*:")
#: In a fenced block: a ``<`` docs.page would show as ``&lt;`` (:func:`fenced_hazard`).
_FENCED_HAZARD = re.compile(r"<(?![A-Za-z/!])")
#: A block that starts a paragraph at the margin: not indented (a list item's text), not a list item, a table, a quote,
#: a heading or a thematic break.
_PARAGRAPH = re.compile(r"(?![ \t]|(?:[-*+]|\d{1,9}[.)])(?:[ \t]|$)|[|>#]|(?:[-*_][ \t]*){3,}$)\S")


def docs_dir(root: Path = ROOT) -> Path:
    return root / "docs"


def sources(root: Path = ROOT) -> list[Path]:
    """The index ``docs/README.md`` and every Markdown file under ``docs/`` it links to, sorted."""
    docs = docs_dir(root)
    index = docs / INDEX
    found = {index}
    for target in re.findall(r"\]\(([^)\s#]+)(?:#[^)\s]*)?\)", index.read_text(encoding="utf-8")):
        if _SCHEME.match(target) or not target.endswith(".md"):
            continue
        path = (docs / target).resolve()
        if path.is_relative_to(docs.resolve()) and path.is_file():
            found.add(docs / path.relative_to(docs.resolve()))
    return sorted(found)


def mirror(source: Path, root: Path = ROOT) -> Path:
    """The ``.mdx`` page generated from *source*: ``docs/index.mdx`` for the index, the same path otherwise."""
    docs = docs_dir(root)
    return docs / HOME if source == docs / INDEX else source.with_suffix(".mdx")


def route(source: Path, root: Path = ROOT) -> str:
    """The site path of *source*'s page: ``/`` for the index, ``/models``, ``/ai/CLAUDE``."""
    docs = docs_dir(root)
    if source == docs / INDEX:
        return "/"
    return "/" + source.relative_to(docs).with_suffix("").as_posix()


def _code_spans(text: str) -> list[tuple[int, int]]:
    """The ``(start, end)`` offsets of the inline code spans of *text* (a run of n backticks up to the next run of
    exactly n)."""
    spans = []
    position = 0
    while (opening := re.compile(r"`+").search(text, position)) is not None:
        ticks = opening.group(0)
        closing = re.compile(rf"(?<!`){ticks}(?!`)").search(text, opening.end())
        if closing is None:
            position = opening.end()
            continue
        spans.append((opening.start(), closing.end()))
        position = closing.end()
    return spans


def _outside_code(text: str, change: Callable[[str], str], every: bool = False) -> str:
    """*text* with *change* applied to each part outside its code spans, and each code span that holds a ``<`` (every
    code span if *every*) written as a ``<code>`` element (:func:`code_element`)."""
    out = []
    position = 0
    for start, end in _code_spans(text):
        span = text[start:end]
        row = text[text.rfind("\n", 0, start) + 1 :].lstrip().startswith("|")
        out += [change(text[position:start]), code_element(span, row) if every or "<" in span else span]
        position = end
    out.append(change(text[position:]))
    return "".join(out)


#: How a ``<code>`` element's text writes a character MDX or Markdown would read: ``<``, ``>`` and ``&`` as entities,
#: an expression's braces and Markdown's markup (emphasis, code, links, a table's ``|``, an escape) with a backslash.
#: docs.page reads a ``<div>``'s text as HTML (which decodes an entity) before it reads it as Markdown, and only the
#: entities of ``<``, ``>`` and ``&`` come back from that reading.
_CODE_ESCAPES = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    **{char: "\\" + char for char in "{}*_`[]\\|~"},
}


def code_element(span: str, row: bool = False) -> str:
    """The code span *span* (its backticks included) as a ``<code>`` element showing the same text on docs.page.

    The span's text is read as GitHub reads it: in a table *row*, ``\\|`` is a ``|``; line endings (and the next
    line's indentation or quote markers) become one space, and one space is stripped from each end when both ends
    have one. Every character MDX or Markdown would read is then escaped (:data:`_CODE_ESCAPES`)."""
    ticks = len(span) - len(span.lstrip("`"))
    text = re.sub(r"[ \t]*\n[ \t>]*", " ", span[ticks:-ticks])
    if row:
        text = text.replace("\\|", "|")
    if len(text) > 1 and text[0] == text[-1] == " " and text.strip(" "):
        text = text[1:-1]
    return "<code>" + "".join(_CODE_ESCAPES.get(char, char) for char in text) + "</code>"


def _escape(text: str) -> str:
    """*text* with every ``<``, ``{`` and ``}`` outside code escaped as a page writes them (:func:`_escaped`), and its
    code spans that hold a ``<`` written as elements."""
    return _outside_code(text, lambda part: re.sub(r"\\?<|(?<!\\)[{}]", _escaped, part))


def _escaped(match: re.Match[str]) -> str:
    """How a page writes a ``<`` (escaped or not), ``{`` or ``}`` of prose: ``&lt;``, ``\\{``, ``\\}``."""
    return "&lt;" if match.group(0).endswith("<") else "\\" + match.group(0)


# --- Heading anchors -------------------------------------------------------------------------------------------------
#
# The guides link to a heading by GitHub's anchor; docs.page gives the same heading its own: it drops what is not
# ``[a-z0-9]``, a space or ``-`` (so ``server_values`` becomes ``servervalues``) and turns each run of spaces into one
# ``-`` (so ``Relations & Graph`` becomes ``relations-graph``). A link of a page carries docs.page's anchor of the
# heading the guide's link names.


def heading_titles(text: str) -> list[str]:
    """The title of every heading of *text*, fenced blocks skipped, as docs.page reads them (``extractHeadingNodes``
    in its ``packages/mdx-bundler/src/markdown/headings.ts``)."""
    titles = []
    fence: tuple[str, int] | None = None
    for line in text.split("\n"):
        run = re.match(r"^\s*(`{3,}|~{3,})", line)
        if run:
            marker = (run.group(1)[0], len(run.group(1)))
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and marker[1] >= fence[1]:
                fence = None
            continue
        if fence is not None:
            continue
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if match:
            titles.append(re.sub(r"\s+#+\s*$", "", match.group(1)).strip())
    return titles


def github_anchor(title: str) -> str:
    """GitHub's anchor of a heading's title (without its ``#`` and its ``-1`` suffix)."""
    return re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")


def site_anchor(title: str) -> str:
    """docs.page's anchor of a heading's title (without its ``#`` and its ``-1`` suffix): ``cleanHeadingTitle`` then
    ``slugifyHeading`` of ``headings.ts``."""
    title = re.sub(r"!\[([^]]*)\]\([^)]*\)", r"\1", title.strip())
    title = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", title)
    title = re.sub(r"`([^`]+)`", r"\1", title)
    title = re.sub(r"<[^>]+>", "", title)
    title = html.unescape(title)
    title = re.sub(r"(\*\*|__)(.+?)\1", r"\2", title)
    title = re.sub(r"(\*|_)(.+?)\1", r"\2", title)
    title = re.sub(r"~~(.+?)~~", r"\1", title)
    slug = re.sub(r"\s+", "-", re.sub(r"[^a-z0-9\s-]", "", " ".join(title.split()).lower()).strip())
    return slug or "section"


def _numbered(slugs: list[str]) -> list[str]:
    """*slugs* with a repeated one numbered from its second: ``-1``, ``-2``."""
    seen: dict[str, int] = {}
    out = []
    for slug in slugs:
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        out.append(slug if count == 0 else f"{slug}-{count}")
    return out


def site_anchors(text: str) -> list[str]:
    """docs.page's anchor of every heading of *text*, a page's body, in order."""
    return _numbered([site_anchor(title) for title in heading_titles(text)])


def anchor_map(text: str) -> dict[str, str]:
    """GitHub's anchor of each heading of *text*, a guide, to docs.page's anchor of the same heading in its page."""
    titles = heading_titles(text)
    github = _numbered([github_anchor(title) for title in titles])
    site = _numbered([site_anchor(_escape(title)) for title in titles])
    return dict(zip(github, site, strict=True))


# --- Conversion -------------------------------------------------------------------------------------------------------


class _Site:
    """The guides of *root* that get a page, and their anchors (read once)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.docs = docs_dir(root)
        self.pages = set(sources(root))
        self._anchors: dict[Path, dict[str, str]] = {}

    def anchor(self, page: Path, fragment: str) -> str:
        """docs.page's anchor, in *page*'s page, of the heading GitHub calls *fragment* (unchanged if none is)."""
        if page not in self._anchors:
            self._anchors[page] = anchor_map(page.read_text(encoding="utf-8")) if page.is_file() else {}
        return self._anchors[page].get(fragment, fragment)

    def target(self, target: str, here: Path) -> str:
        """The site's form of the link *target* written in the guide *here*."""
        if _SCHEME.match(target) or target.startswith("/"):
            return target
        if target.startswith("#"):
            return "#" + self.anchor(here, target[1:])
        path, hash_, fragment = target.partition("#")
        resolved = (here.parent / path).resolve()
        docs = self.docs.resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            raise ValueError(f"{here}: the link {target!r} leaves the repository")
        if resolved.suffix == ".md" and resolved.is_relative_to(docs):
            page = self.docs / resolved.relative_to(docs)
            if page in self.pages:
                return route(page, self.root) + (hash_ + self.anchor(page, fragment) if hash_ else "")
        relative = resolved.relative_to(self.root.resolve()).as_posix()
        if path.endswith("/"):  # a folder (``../examples/``): written with its slash, so the output reads no disk state
            return GITHUB_TREE + relative + "/" + hash_ + fragment
        return GITHUB_BLOB + relative + hash_ + fragment

    def paragraph(self, text: str, here: Path, every: bool = False) -> str:
        """*text*, one block of prose, with its links rewritten and its MDX hazards escaped outside its code spans; a
        code span holding a ``<`` (every code span if *every*) is written as a ``<code>`` element."""

        def token(match: re.Match[str]) -> str:
            if match.group("comment") is not None:
                return ""
            if match.group("target") is not None:
                return f"]({self.target(match.group('target'), here)}{match.group('title') or ''})"
            return _escaped(match)

        return _outside_code(text, lambda part: _TOKEN.sub(token, part), every)

    def prose(self, text: str, here: Path) -> str:
        """*text*, a run of lines outside fenced blocks, converted block by block: a code span never crosses a blank
        line."""
        return "".join(
            piece if index % 2 else self.block(piece, here) for index, piece in enumerate(re.split(r"(\n[ \t]*\n)", text))
        )

    def block(self, text: str, here: Path) -> str:
        """*text*, one block between blank lines, converted; a paragraph holding a ``<code>`` element is wrapped in a
        ``<div>`` (docs.page would cut the paragraph around the element). docs.page reads a ``<div>`` as HTML before
        it reads the Markdown inside, which would turn an ``&lt;`` of a code span into ``&#x3C;``: every code span of a
        wrapped paragraph is an element."""
        converted = self.paragraph(text, here)
        if "<code>" not in converted or not _PARAGRAPH.match(text.lstrip("\n")):
            return converted
        converted = self.paragraph(text, here, every=True)
        core = converted.strip("\n")
        start = converted.index(core)
        return f"{converted[:start]}<div>\n\n{core}\n\n</div>{converted[start + len(core) :]}"


def fenced_hazard(line: str) -> bool:
    """Whether *line*, a line of a fenced block, holds a ``<`` that docs.page would show as ``&lt;``: one not followed
    by a letter, ``/`` or ``!`` (``a < 0``, ``a <= b``, ``<-likes-``, ``<|2|>``). docs.page turns a ``<`` that starts no
    tag into ``&lt;`` before parsing, and a fenced block shows the entity as written. The rule is conservative: a ``<``
    followed by a letter (``<string>``, ``<future>``) shows as written."""
    return _FENCED_HAZARD.search(line) is not None


def convert(text: str, here: Path, root: Path = ROOT) -> str:
    """The body of *here*'s mirror: *text* with links and MDX hazards converted outside fenced code blocks.

    A fenced block is copied as written, so a line of one that docs.page would show wrongly (:func:`fenced_hazard`)
    raises :class:`ValueError` naming the guide and the line: rewrite the guide's line without it
    (``0 > $after.balance`` for ``$after.balance < 0``)."""
    site = _Site(root)
    out: list[str] = []
    prose: list[str] = []
    fence: str | None = None
    for number, line in enumerate(text.split("\n"), start=1):
        match = _FENCE.match(line)
        if fence is None and match is None:
            prose.append(line)
            continue
        if fence is None and match is not None:
            if prose:
                out.append(site.prose("\n".join(prose), here))
                prose = []
            fence = match.group(1)
        elif (
            match is not None
            and fence is not None
            and match.group(1)[0] == fence[0]
            and len(match.group(1)) >= len(fence)
            and not line[match.end() :].strip()
        ):
            fence = None
        elif fenced_hazard(line):
            name = here.relative_to(root).as_posix() if here.is_relative_to(root) else str(here)
            raise ValueError(
                f"{name}:{number}: a '<' in a fenced block that starts no tag, which docs.page shows as '&lt;': "
                f"rewrite the line without it (e.g. '0 > x' for 'x < 0'): {line.strip()}"
            )
        out.append(line)
    if prose:
        out.append(site.prose("\n".join(prose), here))
    return "\n".join(out)


def _plain(text: str) -> str:
    """*text* without Markdown markup: links reduced to their text, emphasis and code ticks dropped, whitespace
    collapsed."""
    text = re.sub(r"!?\[([^]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\*\*|__|`", "", text)
    return " ".join(text.split())


def title(text: str) -> str:
    """The guide's title: its first ``#`` heading, without markup."""
    for line in text.split("\n"):
        if line.startswith("# "):
            return _plain(line[2:])
    raise ValueError("no '# ' title")


def description(text: str) -> str:
    """The guide's summary: its first paragraph after the title, without markup, cut to a sentence or a few words
    when long."""
    lines = text.split("\n")
    start = next(index for index, line in enumerate(lines) if line.startswith("# ")) + 1
    paragraph: list[str] = []
    for line in lines[start:]:
        stripped = re.sub(r"^(?:> ?)+", "", line).strip()
        if not stripped:
            if paragraph:
                break
            continue
        if _HEADING.match(stripped) or _FENCE.match(stripped) or stripped.startswith("|"):
            if paragraph:
                break
            continue
        paragraph.append(stripped)
    summary = _plain(" ".join(paragraph))
    if len(summary) <= DESCRIPTION_MAX:
        return summary
    sentence = re.match(r"(.+?[.!?])(?= )", summary)
    if sentence and len(sentence.group(1)) <= DESCRIPTION_MAX:
        return sentence.group(1)
    return summary[: summary.rfind(" ", 0, DESCRIPTION_MAX)].rstrip(",;:") + "…"


def render(source: Path, root: Path = ROOT) -> str:
    """The full ``.mdx`` page generated from the guide *source*."""
    text = source.read_text(encoding="utf-8")
    relative = source.relative_to(root).as_posix()
    head = [
        "---",
        f"title: {json.dumps(title(text), ensure_ascii=False)}",
        f"description: {json.dumps(description(text), ensure_ascii=False)}",
        f"# Generated from {relative} by scripts/sync_docs_site.py. Edit the .md source.",
        "---",
        "",
    ]
    return "\n".join(head) + "\n" + convert(text, source, root)


def expected(root: Path = ROOT) -> dict[Path, str]:
    """Every mirror the guides call for, with its content."""
    return {mirror(source, root): render(source, root) for source in sources(root)}


def drift(root: Path = ROOT) -> list[str]:
    """What differs from the guides: ``missing``, ``stale`` or ``orphan`` (an ``.mdx`` no guide generates), one entry
    per file."""
    pages = expected(root)
    found = []
    for path, content in pages.items():
        name = path.relative_to(root).as_posix()
        if not path.exists():
            found.append(f"missing {name}")
        elif path.read_text(encoding="utf-8") != content:
            found.append(f"stale {name}")
    for path in sorted(docs_dir(root).rglob("*.mdx")):
        if path not in pages:
            found.append(f"orphan {path.relative_to(root).as_posix()}")
    return found


def fix(root: Path = ROOT) -> list[str]:
    """Write every mirror and remove the orphans; returns what changed (as :func:`drift` names it)."""
    changed = drift(root)
    pages = expected(root)
    for path, content in pages.items():
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
    for path in sorted(docs_dir(root).rglob("*.mdx")):
        if path not in pages:
            path.unlink()
    return changed


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--fix" in args:
        changed = fix(root)
        for entry in changed:
            print(f"  FIXED  {entry}")
        print("docs.page mirrors rewritten from the guides." if changed else "The docs.page mirrors match the guides.")
        return 0
    found = drift(root)
    for entry in found:
        print(f"  OUT OF SYNC  {entry}")
    if found:
        print("Drift detected. Run: uv run python scripts/sync_docs_site.py --fix")
        return 1
    print("The docs.page mirrors match the guides.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
