"""Command line (v0.39.0): the documentation — README section 40, the rows v0.39.0 adds to the README's tables, and the
two sentences that promised the command line "with v0.39.0", now pointing to section 40 (AC-31).

No server: every test reads ``README.md``. ``TestReadmeSection40`` pins section 40 — its place, its nine parts in
order, its fences (``bash`` commands and plain outputs, no ``python`` block), the facts each part states, and in its
"Coming from the full SurrealDB-ORM" part one phrase per difference from the full ORM's CLI. ``TestReadmeRows39`` pins
the 2.x/3.x and embedded-engine rows, the 2.x/3.x table's header sentence, the features, roadmap and comparison rows,
and the two re-pointed sentences.
"""

import re

import pytest

from tests._readme_tables import assert_embedded_rows, assert_server_header_sentence, assert_server_rows
from tests._readme_tables import normalised as _normalised
from tests._readme_tables import readme as _readme
from tests._readme_tables import readme_part as _readme_part
from tests._readme_tables import subsection as _subsection
from tests._readme_tables import table_rows as _table_rows
from tests._readme_tables import tables as _md_tables

_SECTION_37 = "### 37. Vector search (`similar_to()`, `VectorField`, `Index(hnsw=True)`)"
_SECTION_38 = "### 38. Hybrid search (`hybrid_search()`, Reciprocal Rank Fusion)"
_SECTION_39 = "### 39. Migrations (`makemigrations()`, `MigrationExecutor`, operations)"
_SECTION_40 = "### 40. Command line (`surreal-orm-lite`)"
_ANCHOR_40 = "#40-command-line-surreal-orm-lite"
_LINK_40 = f"[section 40]({_ANCHOR_40})"
#: The nine parts of section 40, in order, after its untitled intro.
_PARTS_40 = [
    "Install and run",
    "Connection options",
    "Commands",
    "The SurrealQL shell",
    "Several databases",
    "Exit codes",
    "Security",
    "Server lines",
    "Coming from the full SurrealDB-ORM",
]

# --- What each part states (key phrases, whitespace normalised) -------------------------------------------------------

_NEEDLES_40: dict[str, list[str]] = {
    "": [
        "installs a `surreal-orm-lite` console script, also run as `python -m surreal_orm_lite.cli`",
        "with no extra to install",
        "its nine commands, global options, `SURREAL_*` environment variables, defaults and output texts",
        "built on the standard library's `argparse`",
    ],
    "Install and run": [
        "surreal-orm-lite --version",
        "python -m surreal_orm_lite.cli",
        "Global options go **before** the command",
        "Run the command line from the project directory",
        "so `--models app.models` finds `app/models.py`",
    ],
    "Connection options": [
        "| `--url`, `-u` | `SURREAL_URL` | `http://localhost:8000` |",
        "| `--password` | `SURREAL_PASSWORD` | `root` |",
        "| `--connection` | none | `default` |",
        "An option wins over its environment variable, a variable over the default, and an empty variable counts as unset",
        "`ws://host:8000/rpc`, `http://host:8000`, `mem://` or `surrealkv://<path>`",
        "without them it stops with a usage error before connecting",
        "`sqlmigrate`, `makemigrations --empty` and `makemigrations --no-from-db` work offline",
    ],
    "Commands": [
        "surreal-orm-lite makemigrations -n initial -m app.models",
        "surreal-orm-lite migrate",
        "surreal-orm-lite schemadiff -m app.models # Models and database are in sync.",
        'surreal-orm-lite rollback ""',
        "Legend: [X]=applied, R=reversible, D=has data migrations",
        "`migrate --fake` records them without running them",
        "`upgrade [-t TARGET]` runs the data migrations",
        '`rollback ""` undoes them all',
        "`sqlmigrate MIGRATION` prints the statements of a migration file without a connection",
        "`inspectdb` prints the models of the database's tables",
        "`schemadiff -m MODULE` lists what separates the models from the database, read-only",
    ],
    "The SurrealQL shell": [
        "sends each line you type to the database, unchanged, and shows SurrealDB's answer per statement",
        "surreal-orm-lite -u mem:// -n app -d app shell",
        "-- [1/2] --",
        "Each input line is one request: a transaction must fit on one line (`BEGIN; …; COMMIT;`)",
        "Over HTTP no session state (`LET`, `USE`) survives to the next line",
    ],
    "Several databases": [
        "`--connection NAME` registers the settings under that connection name, and every command uses it",
        '`inspectdb` writes `connection="NAME"` in the models it generates',
    ],
    "Exit codes": [
        "`0`: success",
        "`1`: the command failed",
        "`2`: a usage error, including a missing namespace or database",
        "`130`: interrupted with Ctrl+C",
    ],
    "Security": [
        "Pass the password in `SURREAL_PASSWORD` rather than `--password`: an option is visible in the process list",
        "The `root`/`root` defaults are SurrealDB's and the full ORM's development defaults",
        "`--models` imports the modules it names and the migration commands load the migration files: both run their "
        "Python code",
        "Run the command line from your project directory",
        "The shell runs any SurrealQL with the configured identity",
    ],
    "Server lines": [
        "`migrate` and `rollback` refuse `ASYNC` events, `WITH REFRESH` accesses and `COMPUTED` fields on 2.x and the "
        "embedded engine before any change",
        "on 3.x a transaction answers one entry for `BEGIN` and one for `COMMIT`, on 2.x and the embedded engine only "
        "the statements inside it",
        "a `SELECT` on a missing table is an `Error:` line on 3.x and `(empty result)` on 2.x",
        "`mem://` starts empty at every command",
        "`surrealkv://<path>` keeps its data from one command to the next",
    ],
    # One phrase per difference from the full ORM's command line, in the order of the bullets.
    "Coming from the full SurrealDB-ORM": [
        # argparse, no click, no extra
        "the command line is built on `argparse`, so the script works after a plain `pip install surreal-orm-lite`",
        # the shell per statement
        "one block per statement, with `-- [i/N] --` headers when a line holds several statements, where the full ORM "
        "flattens every record of the line",
        # the lite-only options
        "`--connection`, `--version`, `sqlmigrate --backwards` and `--line`, and `inspectdb --table` and `--no-overwrite`",
        # makemigrations --empty, --no-from-db, --drop-missing
        "`--empty` writes a migration that depends on the latest ones; `--no-from-db` diffs against the migration files "
        "(offline), where the full ORM diffs against an empty schema, and `--drop-missing` keeps its meaning there",
        # skipped removals
        "reported as one `Warning:` line on stderr, not as a list on stdout",
        # exit codes
        "exit 2 when the namespace or the database is missing, before any connection; 130 on Ctrl+C; 0 for "
        "`schemadiff` even when it finds differences",
        # schemadiff with no model
        "**`schemadiff` with no registered model exits 1**",
        # Ctrl+C during a shell query
        "**Ctrl+C during a shell query ends the session** with exit 130; Ctrl+C at the prompt still says `Goodbye!`",
        # a running event loop
        "the command line refuses to run inside a running event loop (Jupyter, an async test) and exits 1",
        # No models found on stderr
        "`No models found. Register models by importing them (--models).` goes to stderr, with the `(--models)` hint",
        # any import exception
        "any exception raised by a `--models` import is reported as `Error importing <module>: <reason>`, not only an "
        "`ImportError`",
        # the banner without credentials
        "the shell banner never prints the URL's credentials (`user:password@` is dropped)",
    ],
}


def _section_40() -> str:
    """Section 40, as written, without the ``---`` that ends it."""
    body, separator, rest = _readme_part(_SECTION_40).rpartition("\n---\n")
    assert separator and not rest.strip(), "section 40 ends with the --- of the next part"
    return f"{body}\n"


def _part_40(heading: str) -> str:
    """One ``####`` part of section 40, as written (the intro, its heading included, for ``""``)."""
    if not heading:
        return _section_40().split("\n#### ", 1)[0]
    return _subsection(f"#### {heading}", _section_40())


def _bullets(text: str) -> list[str]:
    """The ``- `` bullets of *text*, each with its continuation lines, whitespace normalised."""
    return [_normalised(bullet) for bullet in re.findall(r"^- .*?(?=^- |^\s*$|\Z)", text, re.MULTILINE | re.DOTALL)]


def _anchor(heading: str) -> str:
    """GitHub's anchor of a README heading."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


class TestReadmeSection40:
    """README section 40: its place, its parts, its fences and what each part states."""

    def test_section_40_follows_section_39(self) -> None:
        """Sections 38, 39 and 40 in that order after section 37, section 40 the last before the ``---`` of
        "Configuration Options"."""
        text = _readme()
        start_37 = text.index(f"\n{_SECTION_37}\n")
        configuration = text.index("\n## Configuration Options\n")
        assert text.index(f"\n{_SECTION_40}\n") < configuration
        assert re.findall(r"^#{2,3} .*$", text[start_37 + 1 : configuration], re.MULTILINE) == [
            _SECTION_37,
            _SECTION_38,
            _SECTION_39,
            _SECTION_40,
        ]
        assert text[:configuration].rstrip().endswith("\n---"), "section 40 ends before the --- of the next part"
        assert "\n---\n" not in _readme_part(_SECTION_39), "section 39 no longer ends with the ---"

    def test_anchor(self) -> None:
        assert f"#{_anchor(_SECTION_40.removeprefix('### '))}" == _ANCHOR_40

    def test_parts_in_order(self) -> None:
        """The intro, then the nine titled parts, in order, as ``####`` headings."""
        assert re.findall(r"^#### (.*)$", _section_40(), re.MULTILINE) == _PARTS_40

    def test_fences(self) -> None:
        """``bash`` blocks for commands and plain blocks for outputs; no ``python`` block."""
        fences = re.findall(r"^```(\S*)$", _section_40(), re.MULTILINE)
        assert fences, "no code block"
        assert set(fences[0::2]) == {"bash", ""}, fences
        assert set(fences[1::2]) == {""}, fences
        assert "python" not in fences

    def test_one_chain_block(self) -> None:
        """The "Commands" part's ``bash`` block is the whole chain, from the first migration to its rollback."""
        blocks = re.findall(r"^```bash\n(.*?)^```$", _part_40("Commands"), re.MULTILINE | re.DOTALL)
        assert len(blocks) == 1, blocks
        commands = [line.split("#")[0].split() for line in blocks[0].splitlines() if line.startswith("surreal-orm-lite")]
        assert [words[1] for words in commands] == [
            "makemigrations",
            "migrate",
            "status",
            "makemigrations",
            "schemadiff",
            "rollback",
            "status",
        ], commands

    @pytest.mark.parametrize("heading", list(_NEEDLES_40))
    def test_what_each_part_states(self, heading: str) -> None:
        """Each part states its facts (key phrases, whitespace normalised)."""
        part = _normalised(_part_40(heading))
        missing = [needle for needle in _NEEDLES_40[heading] if _normalised(needle) not in part]
        assert missing == [], missing

    def test_coming_from_one_bullet_per_difference(self) -> None:
        """Twelve bullets, each holding exactly one of the twelve differences' phrases, in order."""
        bullets = _bullets(_part_40("Coming from the full SurrealDB-ORM"))
        phrases = _NEEDLES_40["Coming from the full SurrealDB-ORM"]
        assert len(phrases) == 12
        assert len(bullets) == 12, bullets
        for bullet, phrase in zip(bullets, phrases, strict=True):
            assert _normalised(phrase) in bullet, (phrase, bullet)

    def test_no_design_reference(self) -> None:
        """README prose: no decision or measure number (``D12``, ``M3``, ``P1``), no "spec", "revision", ``§`` or
        local path, no issue number."""
        section = _section_40()
        assert re.findall(r"\b[DCMFPR]\d+\b", section) == []
        for marker in (r"\bspec\b", r"\brevision\b", "§", r"\.claude"):
            assert re.search(marker, section, re.IGNORECASE) is None, marker
        assert re.findall(r"(?<![(\w/])#\d+", section) == []

    def test_one_table_without_a_raw_pipe(self) -> None:
        tables = _md_tables(_section_40())
        assert len(tables) == 1
        assert len({line.count("|") for line in tables[0]}) == 1 and "\\|" not in "".join(tables[0])

    def test_links_resolve(self) -> None:
        """Every in-page link of section 40 names a heading."""
        anchors = {_anchor(heading) for heading in re.findall(r"^#{2,4} (.*)$", _readme(), re.MULTILINE)}
        links = re.findall(r"\]\(#([^)]+)\)", _section_40())
        assert links, "no link"
        assert [link for link in links if link not in anchors] == []
        assert _ANCHOR_40.lstrip("#") in anchors


# --- The tables of the README -----------------------------------------------------------------------------------------

_HEADER_SENTENCE_39 = (
    "The row added in v0.39.0 was measured on 2.7.0 and 3.3.4, over WebSocket and HTTP, and on the embedded engine."
)
_SERVER_ROW_39 = [
    "CLI `surreal-orm-lite` `shell`: a transaction, a `SELECT` on a missing table",
    "no entry for `BEGIN` and `COMMIT`, one per statement inside the transaction; a missing table answers `(empty result)`",
    "one entry each for `BEGIN` and `COMMIT` (a failed transaction's `COMMIT` is an `Error:` line); a missing table is "
    "an `Error:` line",
    "v0.39.0",
]
_EMBEDDED_ROW_39 = [
    "CLI `surreal-orm-lite` (v0.39.0)",
    "each command opens and closes the engine: `mem://` starts empty at every command (use it for `shell` and the "
    "offline commands), `surrealkv://<path>` keeps its data from one command to the next; a parse error in a shell "
    "line is raised by the SDK and shown as one `Error:` line; the shell answers as a 2.x server",
    "a server keeps its data between commands; a parse error comes back as an error answer, shown the same way",
    "v0.39.0",
]
_FEATURES_ROW_39 = ["Command line (`surreal-orm-lite`: migrations, inspectdb, shell)", "✅"]
_ROADMAP_ROWS_39 = [
    ["v0.37.0", "Tier 3 — Hybrid search", "✅ Released"],
    ["v0.38.0", "Tier 3 — Migration system", "✅ Released"],
    ["v0.39.0", "Tier 3 — Command line (`surreal-orm-lite`)", "✅ Released"],
    ["v0.40.0", "Tier 3 — Test fixtures & factories", "📋 Planned"],
    ["v0.41.0", "Beta Phase (API freeze, hardening)", "📋 Planned"],
]
_COMPARISON_ROW_39 = ["Migrations & CLI", "✅ v0.38.0 (migrations), ✅ v0.39.0 (CLI)", "✅"]
#: The two sentences that promised the command line "with v0.39.0", by section: the old text, then the new one.
_REPOINTED = [
    (
        "### 31. Schema introspection (`DatabaseIntrospector`, `generate_models_from_db()`)",
        "positional as in the full ORM); the `inspectdb` command comes with v0.39.0.",
        f"positional as in the full ORM); the `inspectdb` command is in {_LINK_40}.",
    ),
    (
        _SECTION_39,
        "- Functions are not compared; there is no squashing and no command line yet (the CLI comes with v0.39.0).",
        f"- Functions are not compared and there is no squashing; the command line is in {_LINK_40}.",
    ),
]


class TestReadmeRows39:
    """The rows v0.39.0 adds to the README's tables, the 2.x/3.x table's header sentence and the re-pointed
    sentences."""

    def test_server_behaviour_row(self) -> None:
        """One row, Since v0.39.0, in version order before the table's last row; the table keeps its widths."""
        assert_server_rows([_SERVER_ROW_39])
        assert [len(cell) for cell in _SERVER_ROW_39] == [76, 117, 130, 7]

    def test_server_behaviour_header_sentence(self) -> None:
        assert_server_header_sentence(_HEADER_SENTENCE_39)

    def test_embedded_table_row(self) -> None:
        """One row before the final ``Everything else`` row; the table keeps its widths."""
        assert_embedded_rows([_EMBEDDED_ROW_39])
        assert [len(cell) for cell in _EMBEDDED_ROW_39] == [32, 310, 105, 7]

    def test_features_row(self) -> None:
        """Right after the migrations."""
        features = [row for _, row in _table_rows(_readme_part("## Features"))]
        firsts = [row[0] for row in features]
        assert _FEATURES_ROW_39 in features, features
        migrations = firsts.index("Migrations (makemigrations / migrate / rollback)")
        assert firsts.index(_FEATURES_ROW_39[0]) == migrations + 1

    def test_roadmap_rows(self) -> None:
        """v0.39.0 released, v0.40.0 planned on its own row, the old range gone."""
        roadmap = [row for _, row in _table_rows(_readme_part("## Roadmap"))]
        start = [row[0] for row in roadmap].index("v0.37.0")
        assert roadmap[start : start + 5] == _ROADMAP_ROWS_39, roadmap
        assert "v0.39.0 – v0.40.0" not in [row[0] for row in roadmap], roadmap

    def test_comparison_row(self) -> None:
        """Right after hybrid search, before the fixtures."""
        comparison = [row for _, row in _table_rows(_readme_part("## SurrealDB-ORM-lite vs SurrealDB-ORM"))]
        firsts = [row[0] for row in comparison]
        start = firsts.index("Hybrid Search (RRF)")
        assert comparison[start + 1] == _COMPARISON_ROW_39, comparison
        assert firsts[start + 2] == "Test Fixtures & Factories", firsts

    @pytest.mark.parametrize(("section", "old", "new"), _REPOINTED, ids=["section-31", "section-39"])
    def test_repointed_sentences(self, section: str, old: str, new: str) -> None:
        """The old sentence is gone, the new one is in its section."""
        text = _normalised(_readme_part(section))
        assert _normalised(old) not in text
        assert _normalised(new) in text

    def test_no_section_promises_v0_39_0(self) -> None:
        """No section before section 40 still says the command line comes with v0.39.0."""
        before = _normalised(_readme()[: _readme().index(f"\n{_SECTION_40}\n")])
        for promise in ("comes with v0.39.0", "CLI comes with", "command line yet"):
            assert promise not in before, promise

    @pytest.mark.parametrize(
        "heading",
        ["## Features", "## Roadmap", "## SurrealDB-ORM-lite vs SurrealDB-ORM"],
        ids=["features", "roadmap", "comparison"],
    )
    def test_no_pipe_in_a_cell(self, heading: str) -> None:
        """The tables touched keep one column count per line and no escaped pipe."""
        for table in _md_tables(_readme_part(heading)):
            assert len({line.count("|") for line in table}) == 1, table
            assert "\\|" not in "".join(table)
