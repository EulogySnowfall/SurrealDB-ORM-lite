#!/usr/bin/env python3
"""Generate the docs.page site's ``.mdx`` pages from the Markdown guides under ``docs/``.

The guides ``docs/**/*.md`` are the single source of the documentation: GitHub renders them, the README and
``llms.txt`` link to them, and the tests read them. The docs.page site (configured by ``docs.json`` at the repository
root) renders only ``docs/**/*.mdx``, so this script writes one ``.mdx`` mirror next to each guide the index
``docs/README.md`` links to, plus ``docs/index.mdx`` (the site's home page) from the index itself.

A mirror is the guide's text with:

- a YAML front matter: ``title`` from the first ``#`` heading, ``description`` from the first paragraph;
- an MDX comment naming its source, so nobody edits the mirror;
- its relative links rewritten for the site (outside code spans and fenced blocks only): ``models.md#custom-primary-key``
  becomes ``/models#custom-primary-key``, ``README.md`` becomes ``/``, ``ai/CLAUDE.md`` becomes ``/ai/CLAUDE``, and a
  link that leaves ``docs/`` (``../CHANGELOG.md``, ``../examples/``) becomes an absolute GitHub URL of ``main``;
- the characters MDX reads as JSX or expressions (``<``, ``{``, ``}``) escaped outside code, and an HTML comment turned
  into an MDX comment.

Usage:
    python scripts/sync_docs_site.py          # Check only (exit 1 if a mirror is missing, stale or orphaned)
    python scripts/sync_docs_site.py --fix    # Rewrite every mirror from its guide

The script is stdlib-only: ``scripts/sync_surrealdb_pins.py --fix`` runs it after rewriting the pins in the guides, so
an unattended pin bump carries the mirrors along.
"""

from __future__ import annotations

import json
import re
import sys
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
#: as JSX or an expression (unless already escaped).
_TOKEN = re.compile(r"<!--(?P<comment>[\s\S]*?)-->|\]\((?P<target>[^)\s]+)(?P<title>\s+\"[^\"]*\")?\)|(?<!\\)(?P<char>[<{}])")
_SCHEME = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*:")


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


def _rewrite_target(target: str, here: Path, root: Path, pages: set[Path]) -> str:
    """The site's form of the link *target* written in the guide *here*; *pages* are the guides with a mirror."""
    if _SCHEME.match(target) or target.startswith(("#", "/")):
        return target
    path, hash_, fragment = target.partition("#")
    resolved = (here.parent / path).resolve()
    docs = docs_dir(root).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"{here}: the link {target!r} leaves the repository")
    if resolved.suffix == ".md" and resolved.is_relative_to(docs):
        page = docs_dir(root) / resolved.relative_to(docs)
        if page in pages:
            return route(page, root) + hash_ + fragment
    relative = resolved.relative_to(root.resolve()).as_posix()
    if path.endswith("/"):  # a folder (``../examples/``): written with its slash, so the output reads no disk state
        return GITHUB_TREE + relative + "/" + hash_ + fragment
    return GITHUB_BLOB + relative + hash_ + fragment


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


def _convert_paragraph(text: str, here: Path, root: Path, pages: set[Path]) -> str:
    """*text*, one block of prose, with its links rewritten and its MDX hazards escaped outside its code spans."""

    def token(match: re.Match[str]) -> str:
        if match.group("comment") is not None:
            return "{/*" + match.group("comment").replace("*/", "* /") + "*/}"
        if match.group("target") is not None:
            return f"]({_rewrite_target(match.group('target'), here, root, pages)}{match.group('title') or ''})"
        return "\\" + match.group("char")

    out = []
    position = 0
    for start, end in _code_spans(text):
        out.append(_TOKEN.sub(token, text[position:start]))
        out.append(text[start:end])
        position = end
    out.append(_TOKEN.sub(token, text[position:]))
    return "".join(out)


def _convert_prose(text: str, here: Path, root: Path, pages: set[Path]) -> str:
    """*text*, a run of lines outside fenced blocks, converted block by block: a code span never crosses a blank
    line."""
    return "".join(
        piece if index % 2 else _convert_paragraph(piece, here, root, pages)
        for index, piece in enumerate(re.split(r"(\n[ \t]*\n)", text))
    )


def convert(text: str, here: Path, root: Path = ROOT) -> str:
    """The body of *here*'s mirror: *text* with links and MDX hazards converted outside fenced code blocks."""
    pages = set(sources(root))
    out: list[str] = []
    prose: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        match = _FENCE.match(line)
        if fence is None and match is None:
            prose.append(line)
            continue
        if fence is None and match is not None:
            if prose:
                out.append(_convert_prose("\n".join(prose), here, root, pages))
                prose = []
            fence = match.group(1)
        elif match is not None and fence is not None:
            marker = match.group(1)
            if marker[0] == fence[0] and len(marker) >= len(fence) and not line[match.end() :].strip():
                fence = None
        out.append(line)
    if prose:
        out.append(_convert_prose("\n".join(prose), here, root, pages))
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
        "---",
        "",
        f"{{/* Generated from {relative} by scripts/sync_docs_site.py. Edit the .md source. */}}",
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
