"""Read the README and the guides under ``docs/``, and check the structure of the two behaviour tables.

Shared by the documentation tests. The README is the project's front page; the user guide is ``docs/*.md`` (index
``docs/README.md``), one guide per topic, each holding numbered sections of the manual (``## 40. Command line…``).
:func:`doc_part` finds a heading in the README or in a guide, wherever it lives, so a test names the heading it pins,
not the file.

Each release adds rows to "ORM behaviour: SurrealDB 2.x, 3.x and 4.x" (and maybe to "ORM behaviour: the embedded engine"),
both in ``docs/compatibility.md``, and a sentence to the intro paragraph of the first table; the test of that release
owns them. It checks their exact text, that the releases are in version order and that the table keeps its column
widths and its final row (:func:`assert_server_rows`, :func:`assert_embedded_rows`,
:func:`assert_server_header_sentence`).

No test checks which release sits next to its own: a release prepared in parallel with another inserts its rows and
its sentence in version order without changing the older tests (issue #272).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
DOCS = ROOT / "docs"
#: The development roadmap: read by its own helpers, not a guide.
ROADMAP = DOCS / "ROADMAP.md"
#: Where the README links to the guides: PyPI shows the README too, so its links are absolute.
GITHUB_BLOB = "https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/"
GITHUB_TREE = "https://github.com/EulogySnowfall/SurrealDB-ORM-lite/tree/main/"

SERVER_BEHAVIOUR = "## ORM behaviour: SurrealDB 2.x, 3.x and 4.x"
EMBEDDED_BEHAVIOUR = "## ORM behaviour: the embedded engine"

#: Every line of the 2.x/3.x/4.x table is 786 characters, its columns 136, 175, 181, 250 and 38 wide between the
#: pipes: a new row is padded to them.
SERVER_TABLE_LINE = 786
SERVER_TABLE_WIDTHS = [136, 175, 181, 250, 38]
#: The 2.x/3.x/4.x table's header cells. Its 4.x column (index 3) was added in v0.50.0: :func:`table_rows` leaves it
#: out of that table's rows, so each release's test keeps reading its capability, 2.x, 3.x and Since cells; the
#: v0.50.0 tests read the whole rows (:func:`server_rows_4x`).
SERVER_HEADER = ["ORM capability", "SurrealDB 2.6.x / 2.7.x", "SurrealDB 3.2.x / 3.3.x", "SurrealDB 4.0.x", "Since"]
SERVER_4X_COLUMN = 3
#: Every line of the embedded table is 539 characters, its columns 96, 316, 113 and 9 wide (re-measured in v0.50.0).
EMBEDDED_TABLE_LINE = 539
EMBEDDED_TABLE_WIDTHS = [96, 316, 113, 9]

#: The 2.x/3.x table is in version order from its first v0.20.0 row, right after its last row of an earlier release,
#: to the row before its final one. The rows above it are not checked: v0.9.0's come before v0.7.0's, and the v0.30.3
#: row is grouped by topic among v0.15.0's transaction rows.
SERVER_ORDERED_FROM = (0, 20, 0)

#: A "Since" cell, or the release a sentence names: ``v0.30.7``, ``pre-existing (documented in v0.28.1)``.
_RELEASE = re.compile(r"v(\d+)\.(\d+)\.(\d+)|pre-existing \(documented in v(\d+)\.(\d+)\.(\d+)\)")
#: How a sentence of the 2.x/3.x table's intro paragraph names its release.
_ADDED_IN = re.compile(r"\badded in (v\d+\.\d+\.\d+)")
_HEADING = re.compile(r"^(#{1,6}) (.*)$")


# --- Reading the README and the guides ------------------------------------------------------------------------------


def readme() -> str:
    """The README, the project's front page (also PyPI's long description)."""
    return README.read_text(encoding="utf-8")


def guide_paths() -> list[Path]:
    """Every guide of ``docs/``, the index ``docs/README.md`` included, the roadmap left out."""
    return sorted(path for path in DOCS.glob("*.md") if path != ROADMAP)


def guide(name: str) -> str:
    """The text of the guide ``docs/<name>``."""
    return (DOCS / name).read_text(encoding="utf-8")


def headings(text: str) -> list[tuple[int, str, int]]:
    """``(level, title, line index)`` of every heading of *text*, code blocks skipped."""
    found = []
    fence = False
    for index, line in enumerate(text.split("\n")):
        if line.startswith("```"):
            fence = not fence
            continue
        match = _HEADING.match(line)
        if match and not fence:
            found.append((len(match.group(1)), match.group(2), index))
    return found


def part(heading: str, text: str) -> str:
    """The ``heading`` line of *text* (``"## 40. Command line…"``, its level included) up to the next heading of the
    same level or above, or the end."""
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.split("\n")
    found = [(lvl, index) for lvl, title, index in headings(text) if lvl == level and f"{'#' * lvl} {title}" == heading]
    assert len(found) == 1, f"{len(found)} {heading!r} headings"
    start = found[0][1]
    end = next((index for lvl, _, index in headings(text) if index > start and lvl <= level), len(lines))
    return "\n".join(lines[start:end]) + ("\n" if end < len(lines) else "")


def doc_file(heading: str) -> Path:
    """The README or the guide holding ``heading`` (its level included); exactly one does."""
    holders = [
        path
        for path in [README, *guide_paths()]
        if any(f"{'#' * lvl} {title}" == heading for lvl, title, _ in headings(path.read_text(encoding="utf-8")))
    ]
    assert len(holders) == 1, f"{heading!r} is in {[path.name for path in holders]}"
    return holders[0]


def doc_part(heading: str) -> str:
    """The ``heading`` part of the README or of the guide holding it, up to the next heading of its level or above."""
    return part(heading, doc_file(heading).read_text(encoding="utf-8"))


def subsection(heading: str, text: str) -> str:
    """The ``heading`` part of ``text`` (a part :func:`doc_part` returned), up to the next heading of its level or
    above, or the end."""
    return part(heading, text)


def anchor(heading: str) -> str:
    """GitHub's anchor of a heading's title (without its ``#``)."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def anchors(text: str) -> set[str]:
    """Every anchor GitHub gives the headings of *text*: a repeated title gets ``-1``, ``-2``… after the first."""
    seen: dict[str, int] = {}
    found = set()
    for _, title, _ in headings(text):
        base = anchor(title)
        count = seen.get(base, 0)
        seen[base] = count + 1
        found.add(base if count == 0 else f"{base}-{count}")
    return found


def guide_order() -> list[str]:
    """The guides in the order of the index's "Guides" list (``docs/README.md``)."""
    guides_part = part("## Guides", guide("README.md"))
    return re.findall(r"^- \*\*\[[^]]+\]\(([\w.-]+\.md)\)\*\*$", guides_part, re.MULTILINE)


def manual() -> str:
    """Every guide, in the index's order, as one text: what a reader goes through from section 1 to the end."""
    return "\n".join(guide(name) for name in guide_order())


def unresolved_links(text: str, here: Path) -> list[str]:
    """The relative links of *text* (written in the file *here*) that name no file, or no heading of their file.

    ``(#anchor)`` names a heading of *here*, ``(other.md#anchor)`` one of ``other.md`` next to it; a link to a
    directory or to a file other than Markdown only needs the path to exist. Absolute URLs are not checked here."""
    broken = []
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if re.match(r"[a-z]+:", target):
            continue
        path, _, fragment = target.partition("#")
        file = (here.parent / path).resolve() if path else here
        if (
            not file.exists()
            or fragment
            and (file.suffix != ".md" or fragment not in anchors(file.read_text(encoding="utf-8")))
        ):
            broken.append(target)
    return broken


def assert_guide_sections(name: str, sections: list[str]) -> None:
    """The guide ``docs/<name>`` has one ``#`` title, then exactly *sections* as its ``##`` headings, in order."""
    found = [(level, title) for level, title, _ in headings(guide(name)) if level <= 2]
    assert [level for level, _ in found].count(1) == 1 and found[0][0] == 1, found[:2]
    assert [f"## {title}" for _, title in found[1:]] == sections


def assert_manual_order(*sections: str) -> None:
    """*sections* (headings, level included) come in this order in the manual (:func:`manual`)."""
    text = manual()
    positions = [text.index(f"\n{section}\n") for section in sections]
    assert positions == sorted(positions), list(zip(sections, positions, strict=True))


def assert_links_resolve(text: str, name: str) -> None:
    """*text*, a part of the guide ``docs/<name>``, has relative links and each names a file and a heading."""
    assert re.search(r"\]\(([\w-]*\.md)?#", text), "no link"
    assert unresolved_links(text, DOCS / name) == []


def normalised(text: str) -> str:
    return " ".join(text.split())


def cells(line: str) -> list[str]:
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def tables(text: str) -> list[list[str]]:
    """Every table of ``text``: its lines, header and separator included."""
    return [block.splitlines() for block in re.findall(r"(?:^\|.*\n)+", text, re.MULTILINE)]


def table_rows(text: str, header: str | None = None, *, every_column: bool = False) -> list[tuple[str, list[str]]]:
    """The body rows of the first table of ``text`` (whose header line contains ``header``, when given).

    The rows of the 2.x/3.x/4.x table come without their 4.x cell, unless *every_column* (see :data:`SERVER_HEADER`)."""
    found = [lines for lines in tables(text) if header is None or header in lines[0]]
    assert found, f"no table with {header!r} in its header"
    release_view = not every_column and cells(found[0][0]) == SERVER_HEADER
    rows = [(line, cells(line)) for line in found[0][2:]]
    if release_view:
        return [(line, row[:SERVER_4X_COLUMN] + row[SERVER_4X_COLUMN + 1 :]) for line, row in rows]
    return rows


# --- Releases and their order ---------------------------------------------------------------------------------------


def release(text: str) -> tuple[int, int, int]:
    """The release a "Since" cell names (``v0.30.7``, ``pre-existing (documented in v0.28.1)``), as a comparable
    triple; any other text fails the test."""
    match = _RELEASE.fullmatch(text)
    assert match is not None, f"not a release: {text!r}"
    major, minor, patch = (int(part) for part in match.groups() if part is not None)
    return major, minor, patch


def assert_release_rows_in_order(
    rows: list[list[str]],
    block: list[list[str]],
    *,
    tail: int,
    ordered_from: tuple[int, int, int] | None = None,
    shared: bool = False,
) -> None:
    """*block*, rows of one release cell by cell (their last cell names it), is in *rows* once and contiguous, inside
    the part of the table that is in version order: from the first row (with *ordered_from*, the first row after the
    last row of a release before *ordered_from*, which must be a row of *ordered_from*) to the row before the *tail*
    final rows, which are exempt.

    That part is in version order: the rows of a release stay together, the rows above them name earlier releases,
    the rows below them later ones. *block* is every row of its release, unless *shared*: several tests own the
    release's rows (v0.28.1's three rows belong to three tests), and *block* is among them.

    Which release sits right before or after *block* is not checked: another release inserts its rows in version order
    without changing this test.
    """
    assert block, "no row to check"
    ours = release(block[0][-1])
    assert all(release(row[-1]) == ours for row in block), block
    end = len(rows) - tail
    first = 0
    if ordered_from is not None:
        first = 1 + max((index for index, row in enumerate(rows[:end]) if release(row[-1]) < ordered_from), default=-1)
        assert first < end and release(rows[first][-1]) == ordered_from, f"the ordered part starts with {rows[first:end][:1]}"
    assert all(rows.count(row) == 1 for row in block), [row for row in block if rows.count(row) != 1]
    starts = [index for index in range(len(rows)) if rows[index : index + len(block)] == block]
    assert len(starts) == 1, f"the rows are not together, in this order: {block}"
    start = starts[0]
    assert first <= start and start + len(block) <= end, (
        f"rows {start} to {start + len(block) - 1} are outside {first}-{end - 1}"
    )
    ordered = rows[first:end]
    disorder = [
        (above, below) for above, below in zip(ordered, ordered[1:], strict=False) if release(above[-1]) > release(below[-1])
    ]
    assert not disorder, f"rows out of version order (the first above the second): {disorder}"
    if not shared:
        same = [row for row in rows[:end] if release(row[-1]) == ours]
        assert same == block, f"the release has other rows: {[row for row in same if row not in block]}"


def assert_header_sentence(header: str, sentence: str) -> None:
    """*sentence*, the one sentence of a release (it names it once, as ``added in vX.Y.Z``), is in *header* once with
    its exact text, and the releases *header* names that way are strictly increasing. Which sentence comes right
    before or after it is not checked.

    Its exact text includes its bounds: it starts right after ``. `` or ``; ``, and ends right before a space or the
    end of *header* when it is a sentence (its own full stop ends it), right before ``;`` or ``.`` then a space or the
    end when it is a clause. A character changed just outside it (``3.1.5;`` becoming ``3.1.50;``, ``;`` becoming
    ``,``, ``rows`` becoming ``arrows``) fails as one changed inside it does."""
    named = _ADDED_IN.findall(sentence)
    assert len(named) == 1, f"a sentence names one release with 'added in vX.Y.Z': {sentence!r}"
    assert header.count(sentence) == 1, f"{header.count(sentence)} copies of {sentence!r} in {header!r}"
    end = r"(?= |\Z)" if sentence.endswith(".") else r"(?=[.;](?: |\Z))"
    bounded = re.findall(rf"(?<=[.;] ){re.escape(sentence)}{end}", header)
    assert len(bounded) == 1, (
        f"{sentence!r} does not start right after '. ' or '; ' and end right before ';', '.' or a space in {header!r}"
    )
    releases = [release(name) for name in _ADDED_IN.findall(header)]
    disorder = [(above, below) for above, below in zip(releases, releases[1:], strict=False) if above >= below]
    assert not disorder, f"sentences out of version order: {disorder}"


# --- The two behaviour tables ---------------------------------------------------------------------------------------


def _assert_widths(heading: str, line_length: int, widths: list[int]) -> None:
    [lines] = tables(doc_part(heading))
    assert {len(line) for line in lines} == {line_length}, [line for line in lines if len(line) != line_length]
    for line in lines:
        assert [len(cell) for cell in line.split("|")[1:-1]] == widths, line


def server_rows() -> list[list[str]]:
    """The cells of every body row of the 2.x/3.x table, in order."""
    return [row for _, row in table_rows(doc_part(SERVER_BEHAVIOUR))]


def server_rows_4x() -> list[list[str]]:
    """The cells of every body row of the 2.x/3.x/4.x table, its 4.x cell included, in order."""
    return [row for _, row in table_rows(doc_part(SERVER_BEHAVIOUR), every_column=True)]


def embedded_rows() -> list[list[str]]:
    """The cells of every body row of the embedded table, in order."""
    return [row for _, row in table_rows(doc_part(EMBEDDED_BEHAVIOUR))]


def server_header() -> str:
    """The 2.x/3.x table's heading and intro paragraph, whitespace normalised."""
    return normalised(doc_part(SERVER_BEHAVIOUR).split("\n|", 1)[0])


def assert_server_table() -> None:
    """The 2.x/3.x/4.x table keeps its widths (no line re-padded) and its final row, v0.19.1's re-run of the suite."""
    _assert_widths(SERVER_BEHAVIOUR, SERVER_TABLE_LINE, SERVER_TABLE_WIDTHS)
    last = server_rows()[-1]
    assert last[0].startswith("Full suite and every divergence") and last[-1] == "v0.19.1", last


def assert_embedded_table() -> None:
    """The embedded table keeps its widths and its final, catch-all row."""
    _assert_widths(EMBEDDED_BEHAVIOUR, EMBEDDED_TABLE_LINE, EMBEDDED_TABLE_WIDTHS)
    last = embedded_rows()[-1]
    assert last[0] == "Everything else", last


def assert_server_rows(block: list[list[str]], *, shared: bool = False) -> None:
    """*block*, a release's rows of the 2.x/3.x table cell by cell, is there, together and in version order
    (:func:`assert_release_rows_in_order`), in a table that keeps its widths and its final row."""
    assert_server_table()
    assert_release_rows_in_order(server_rows(), block, tail=1, ordered_from=SERVER_ORDERED_FROM, shared=shared)


def assert_embedded_rows(block: list[list[str]], *, shared: bool = False) -> None:
    """*block*, a release's rows of the embedded table cell by cell, is there, together and in version order, in a
    table that keeps its widths and its final row."""
    assert_embedded_table()
    assert_release_rows_in_order(embedded_rows(), block, tail=1, shared=shared)


def assert_server_header_sentence(sentence: str) -> None:
    """*sentence*, a release's sentence of the 2.x/3.x table's intro paragraph, is there once with its exact text,
    among sentences naming their releases in increasing version order (:func:`assert_header_sentence`)."""
    assert_header_sentence(server_header(), sentence)
