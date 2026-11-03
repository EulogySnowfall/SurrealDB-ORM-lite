"""Model factories and the pytest plugin (v0.40.0): the documentation — README section 41, the rows v0.40.0 adds to
the README's tables, and the section 26 sentences that promised the factories "in v0.40.0" (AC-18, README part).

``TestReadmeSection41E2E`` runs the section's examples as written on ``mem://`` (no server; the pytest snippets are
only compiled). ``TestReadmeSection41`` pins the section — its place, its parts, the provider table, the facts each part
states, and in its "Coming from the full SurrealDB-ORM" part one bullet per departure from the full ORM's factories.
``TestReadmeRows40`` pins the 2.x/3.x and embedded-engine rows, the 2.x/3.x table's header sentence, the features,
roadmap and comparison rows, and the re-pointed sentences of section 26.
"""

import ast
import inspect
import re
from typing import Any

import pytest

from surreal_orm_lite import SurrealDBConnectionManager
from tests._readme_tables import anchors as _anchors
from tests._readme_tables import (
    assert_embedded_rows,
    assert_guide_sections,
    assert_links_resolve,
    assert_manual_order,
    assert_server_header_sentence,
    assert_server_rows,
)
from tests._readme_tables import doc_part as _doc_part
from tests._readme_tables import guide as _guide
from tests._readme_tables import manual as _manual
from tests._readme_tables import normalised as _normalised
from tests._readme_tables import subsection as _subsection
from tests._readme_tables import table_rows as _table_rows
from tests._readme_tables import tables as _md_tables

_SECTION_26 = "## 26. Embedded engine and test fixtures"
_SECTION_40 = "## 40. Command line (`surreal-orm-lite`)"
_SECTION_41 = "## 41. Test data factories (`ModelFactory`, `Faker`) and the pytest plugin"
_ANCHOR_41 = "#41-test-data-factories-modelfactory-faker-and-the-pytest-plugin"
_LINK_41 = f"[section 41](testing.md{_ANCHOR_41})"
#: The parts of section 41, in order, after its untitled intro.
_PARTS_41 = [
    "Declare and use a factory",
    "Faker providers",
    "Server-generated values (`SurrealFunc`)",
    "Names and checks",
    "Reproducible data",
    "Edges, fixtures, transactions and connections",
    "The pytest plugin",
    "Security",
    "Server lines",
    "Coming from the full SurrealDB-ORM",
]
#: The fourteen providers, in the table's order (the full ORM's).
_PROVIDERS = [
    "name",
    "first_name",
    "last_name",
    "email",
    "random_int",
    "random_float",
    "text",
    "sentence",
    "word",
    "uuid",
    "boolean",
    "date",
    "datetime",
    "choice",
]

# --- What each part states (key phrases, whitespace normalised) -------------------------------------------------------

_NEEDLES_41: dict[str, list[str]] = {
    "": [
        "Since v0.40.0 `surreal_orm_lite.testing` has the full SurrealDB-ORM's `ModelFactory` and `Faker`",
        "`build()` makes instances without a database",
        "`create()` saves them through the model's own `save()`",
        "`surreal_orm_lite.testing.pytest_plugin`",
        "`surreal_db` fixture",
    ],
    "Declare and use a factory": [
        "class PlayerFactory(ModelFactory[Player]):",
        "`ModelFactory[Player]` types `build()` and `create()` as `Player`",
        "deep-copied for each instance",
        "is refused with `TypeError` at the `class` statement",
        "`build(**overrides)` returns an unsaved instance; no database, no signal",
        "`create_batch(count, **overrides)` calls `create()` `count` times, one record after the other",
        "`Player.objects().bulk_create(PlayerFactory.build_batch(100))`",
        "a `Faker` passed as an override is generated",
        "A class assigned to a field of the model, though, raises `TypeError` at the `class` statement",
        "An override is used as is, not copied",
        "`count` is positional",
        "`build_batch(3, count=5)`",
        "A `count` that is not an `int` (or is a `bool`) raises `TypeError`, a negative one `ValueError`, and `0` returns `[]`",
    ],
    "Faker providers": [
        '`Faker("nme")` raises `ValueError("Unknown Faker provider: \'nme\'")`',
        '`Faker("random_int", minimum=1)` raises `TypeError`',
        '`Faker("choice")` without `items` raises `ValueError`',
        "returns a deep copy of the item it draws",
    ],
    "Server-generated values (`SurrealFunc`)": [
        "`create()` passes it to `save(server_values=…)`",
        "each record of a batch gets its own",
        "`build()` leaves the field out, so the model's default applies: the field must have one",
        "A `SurrealFunc` passed to `build()` raises `TypeError`",
        "`SurrealFunc` attributes are for node factories",
        "`DEFINE FIELD … DEFAULT`",
    ],
    "Names and checks": [
        "by its Python name or by its column",
        "Use one form per field: both in one class body, or in one call, raise `TypeError`",
        "a model drops an unknown keyword silently",
        'unless the model accepts extra fields (`extra="allow"`)',
        "A subclass whose own `class Meta:` does not set `model` keeps its parent's",
        "its methods raise `ValueError`",
    ],
    "Reproducible data": [
        "Every provider draws from Python's `random` module, `uuid` included",
        "`random.seed(n)`",
        "pytest-randomly reseeds `random` before each test",
        "`pytest --randomly-seed=N`",
    ],
    "Edges, fixtures, transactions and connections": [
        "class FollowsFactory(ModelFactory[Follows]):",
        "`build()` gives a `SurrealFixture` its templates",
        "`create()` takes no `tx=`",
        'inside `SurrealDBConnectionManager.using("archive")`',
    ],
    "The pytest plugin": [
        'pytest_plugins = ["surreal_orm_lite.testing.pytest_plugin"]',
        "`pytest -p surreal_orm_lite.testing.pytest_plugin`",
        "pytest and pytest-asyncio (1.0 or later)",
        "`surreal_db` yields `None`, not the client",
        "`await SurrealDBConnectionManager.get_client()`",
        '`surreal_db_url` is `"mem://"`',
        "A server URL fails the test's setup",
        "strict mode (tests marked `@pytest.mark.asyncio`) and in auto mode",
        "registers nothing else (no hook, no marker, no option)",
    ],
    "Security": [
        "The factories build no SurrealQL",
        "never build one from user input",
        "**test data, never secrets or tokens**",
        '`SurrealFunc("rand::uuid::v7()")`',
        "a test run cannot wipe a server's database through `surreal_db`",
    ],
    "Server lines": [
        "Factories behave the same on SurrealDB 3.x, 2.x and the embedded engine",
        "answers as a 2.x server (core 2.3.10)",
    ],
    # One phrase per departure from the full ORM's factories, in the order of the bullets.
    "Coming from the full SurrealDB-ORM": [
        # any static value collected and deep-copied
        "a `datetime`, a `Decimal`, an `Enum`, a `RecordID` or a nested model is deep-copied for each instance, where "
        "the full ORM keeps only primitives and containers",
        # ModelFactory[M] typing
        "`ModelFactory[Player]` types `build()` and `create()` as `Player`; the full ORM's unparametrized "
        "`ModelFactory` still works",
        # SurrealFunc attributes
        "generated by the server on a node factory's `create()`, left out by `build()`",
        # early checks
        "with `TypeError` for a name that is no field, where the full ORM raises at the first value and passes "
        "unknown names to the model",
        # choice deep copy
        "**`choice` returns a deep copy** of the item it draws, where the full ORM returns the item itself",
        # seedable uuid
        "**`uuid` follows `random.seed()`**",
        # opt-in plugin
        "**The pytest plugin is opt-in**",
        # sequential create_batch
        "**`create_batch()` saves one record at a time**",
        # Faker override generated
        "**A `Faker` override is generated**",
        # count validation
        "**`count` is checked**: `TypeError` for a non-`int` or a `bool`, `ValueError` for a negative count, `[]` for "
        "`0`; the full ORM returns `[]` for a negative count",
        # surreal_db yields None
        "**`surreal_db` yields `None`**",
        # no Sequence/LazyAttribute/SubFactory, callables refused
        "**No `Sequence`, `LazyAttribute` or `SubFactory`**",
        # Meta inheritance
        "**`Meta.model` is inherited**: a subclass whose `class Meta:` does not set `model` keeps its parent's, as in "
        "Factory Boy",
        # positional-only cls/count, with its reason
        "**`cls` and `count` are positional-only**: a model field named `count` (or `cls`) stays overridable",
        # the edge SurrealFunc refusal
        "**No `SurrealFunc` on an edge factory**",
        # name normalization
        "**Names are brought to the Python field name**",
    ],
}
#: The reason the positional-only bullet gives, in full (P1).
_POSITIONAL_REASONS = [
    "`build_batch(3, count=5)`",
    "where the full ORM raises `TypeError: got multiple values for argument 'count'`",
    "the keyword form `build_batch(count=3)`, which no full-ORM example uses",
    "Factory Boy names the batch argument `size`",
]


def _section_41() -> str:
    """Section 41, as written: the last part of the testing guide, no ``---`` in it."""
    section = _doc_part(_SECTION_41)
    assert "\n---\n" not in section
    return section


def _part_41(heading: str) -> str:
    """One ``###`` part of section 41, as written (the intro, its heading included, for ``""``)."""
    if not heading:
        return _section_41().split("\n### ", 1)[0]
    return _subsection(f"### {heading}", _section_41())


def _examples_41() -> list[str]:
    """Section 41's ``python`` blocks, in order."""
    blocks: list[str] = re.findall(r"^```python\n(.*?)^```$", _section_41(), re.MULTILINE | re.DOTALL)
    assert blocks, "README section 41 has no example"
    return blocks


def _bullets(text: str) -> list[str]:
    """The ``- `` bullets of *text*, each with its continuation lines, whitespace normalised."""
    return [_normalised(bullet) for bullet in re.findall(r"^- .*?(?=^- |^\s*$|\Z)", text, re.MULTILINE | re.DOTALL)]


def _anchor(heading: str) -> str:
    """GitHub's anchor of a README heading."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


class TestReadmeSection41E2E:
    """AC-18: section 41's examples run as written on ``mem://`` (no server); its pytest snippets are only compiled."""

    async def test_section_41_examples(self) -> None:
        namespace: dict[str, Any] = {"__name__": "readme_section_41"}
        compiled_only = 0
        try:
            for code in _examples_41():
                compiled = compile(code, "<README section 41>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                if "pytest" in code:
                    compiled_only += 1
                    continue
                # A doctest of this repository's own README (trusted text), top-level awaits included.
                outcome = eval(compiled, namespace)
                if inspect.iscoroutine(outcome):
                    await outcome
        finally:
            namespace.clear()  # no client of the examples outlives them (closed-client rule)
            await SurrealDBConnectionManager.unset_connection()
        assert compiled_only == 3
        assert not SurrealDBConnectionManager.is_connected()

    def test_main_block_runs_on_mem(self) -> None:
        """The first block opens ``embedded_database()`` (``mem://``) itself, creates through the factory and seeds."""
        main = _examples_41()[0]
        assert "async with embedded_database():" in main
        assert "embedded_database(" in main and "surrealkv" not in main
        for call in ("PlayerFactory.build(", "PlayerFactory.build_batch(", "PlayerFactory.create(", ".create_batch("):
            assert call in main, call
        assert "random.seed(" in main and 'SurrealFunc("' in main


class TestReadmeSection41:
    """README section 41: its place, its parts, its provider table and what each part states."""

    def test_section_41_follows_section_40(self) -> None:
        """Section 41 is the testing guide, ``docs/testing.md``, its only section, right after section 40 (the command
        line guide) in the manual."""
        assert_guide_sections("testing.md", [_SECTION_41])
        assert_manual_order(_SECTION_40, _SECTION_41)

    def test_anchor(self) -> None:
        assert f"#{_anchor(_SECTION_41.removeprefix('## '))}" == _ANCHOR_41

    def test_parts_in_order(self) -> None:
        assert re.findall(r"^### (.*)$", _section_41(), re.MULTILINE) == _PARTS_41

    def test_fences(self) -> None:
        """Only ``python`` blocks."""
        fences = re.findall(r"^```(\S*)$", _section_41(), re.MULTILINE)
        assert fences and set(fences[0::2]) == {"python"} and set(fences[1::2]) == {""}, fences

    def test_provider_table(self) -> None:
        """One table: the fourteen providers, in order, with their value and keywords."""
        tables = _md_tables(_part_41("Faker providers"))
        assert len(tables) == 1
        rows = [cells for _, cells in _table_rows(_part_41("Faker providers"))]
        assert [cells[0] for cells in rows] == [f"`{provider}`" for provider in _PROVIDERS]
        keywords = {cells[0].strip("`"): cells[2] for cells in rows}
        assert keywords["random_int"] == "`min` (0), `max` (100)"
        assert keywords["random_float"] == "`min` (0.0), `max` (1.0)"
        assert keywords["text"] == "`max_length` (200)"
        assert keywords["choice"] == "`items` (required, not empty)"
        assert {keywords[name] for name in _PROVIDERS if name not in ("random_int", "random_float", "text", "choice")} == {
            "none"
        }

    @pytest.mark.parametrize("heading", list(_NEEDLES_41))
    def test_what_each_part_states(self, heading: str) -> None:
        part = _normalised(_part_41(heading))
        missing = [needle for needle in _NEEDLES_41[heading] if _normalised(needle) not in part]
        assert missing == [], missing

    def test_randomly_seed_option(self) -> None:
        """pytest-randomly's option is ``--randomly-seed=N``; the wrong ``randomly_seed=`` form appears nowhere."""
        section = _section_41()
        assert "--randomly-seed=" in section
        assert "randomly_seed=" not in section

    def test_surreal_func_is_for_node_factories(self) -> None:
        """No example declares a ``SurrealFunc`` on an edge factory."""
        edges = _examples_41()[1]
        factory = edges.split("class FollowsFactory", 1)[1].split("\n\n\n", 1)[0]
        assert "SurrealFunc" not in factory

    def test_coming_from_one_bullet_per_departure(self) -> None:
        """Fifteen bullets, each holding exactly one departure's phrase, in order; the positional-only bullet gives
        its reasons."""
        bullets = _bullets(_part_41("Coming from the full SurrealDB-ORM"))
        phrases = _NEEDLES_41["Coming from the full SurrealDB-ORM"]
        assert len(phrases) == 15
        assert len(bullets) == 15, bullets
        for bullet, phrase in zip(bullets, phrases, strict=True):
            assert _normalised(phrase) in bullet, (phrase, bullet)
        [positional] = [bullet for bullet in bullets if "positional-only" in bullet]
        missing = [reason for reason in _POSITIONAL_REASONS if _normalised(reason) not in positional]
        assert missing == [], missing

    def test_no_design_reference(self) -> None:
        """README prose: no decision or measure number (``D12``, ``M3``, ``P1``), no "spec", "revision", ``§`` or
        local path, no issue number."""
        section = _section_41()
        assert re.findall(r"\b[DCMFPR]\d+\b", section) == []
        for marker in (r"\bspec\b", r"\brevision\b", "§", r"\.claude"):
            assert re.search(marker, section, re.IGNORECASE) is None, marker
        assert re.findall(r"(?<![(\w/])#\d+", section) == []

    def test_one_table_without_a_raw_pipe(self) -> None:
        tables = _md_tables(_section_41())
        assert len(tables) == 1
        assert len({line.count("|") for line in tables[0]}) == 1 and "\\|" not in "".join(tables[0])

    def test_links_resolve(self) -> None:
        """Every in-page link of section 41 names a heading."""
        assert_links_resolve(_section_41(), "testing.md")
        assert _ANCHOR_41.lstrip("#") in _anchors(_guide("testing.md"))


# --- The tables of the README and section 26 ------------------------------------------------------------------------

_HEADER_SENTENCE_40 = "The rows added in v0.40.0 were measured on 2.7.0, 3.3.5 and the embedded engine."
_SERVER_CELL_40 = (
    "same on both lines: `build()` needs no database, `create()` is the model's `save()`; a node factory's "
    "`SurrealFunc` attributes are generated by the server"
)
_SERVER_ROW_40 = [
    "Model factories (`ModelFactory`, `Faker`, `SurrealFunc` attributes)",
    _SERVER_CELL_40,
    _SERVER_CELL_40,
    "v0.40.0",
]
_EMBEDDED_ROW_40 = [
    "Model factories and the pytest plugin (v0.40.0)",
    "the plugin's `surreal_db` is a fresh `mem://` database per test (`surreal_db_url` may name "
    "`surrealkv://<path>`), answering as a 2.x server (core 2.3.10); factories build and create as on a server, "
    "`SurrealFunc` attributes included",
    "`surreal_db` refuses a server URL (embedded engines only); factories behave the same",
    "v0.40.0",
]
_FEATURES_ROW_40 = ["Test data factories (`ModelFactory`, `Faker`) & pytest plugin", "✅"]
_ROADMAP_ROWS_40 = [
    ["v0.39.0", "Tier 3 — Command line (`surreal-orm-lite`)", "✅ Released"],
    ["v0.40.0", "Tier 3 — Test fixtures & factories", "✅ Released"],
    ["v0.41.0", "Beta Phase (API freeze, hardening)", "📋 Planned"],
]
_COMPARISON_ROW_40 = ["Test Fixtures & Factories", "✅ v0.25.0 SurrealFixture; ✅ v0.40.0 factories", "✅"]
_PLUGIN_SENTENCE_26 = (
    f"Since v0.40.0 the opt-in plugin `surreal_orm_lite.testing.pytest_plugin` ships this fixture ({_LINK_41})."
)
_FACTORIES_BULLET_26 = (
    f"- **`ModelFactory` and `Faker`**, and a pytest plugin with the `surreal_db` fixture, are in {_LINK_41} (v0.40.0)."
)


class TestReadmeRows40:
    """The rows v0.40.0 adds to the README's tables, the 2.x/3.x table's header sentence and section 26."""

    def test_server_behaviour_row(self) -> None:
        """One row, the same text in both line cells, in version order before the table's last row."""
        assert_server_rows([_SERVER_ROW_40])
        assert _SERVER_ROW_40[1] == _SERVER_ROW_40[2]

    def test_server_behaviour_header_sentence(self) -> None:
        assert_server_header_sentence(_HEADER_SENTENCE_40)

    def test_embedded_table_row(self) -> None:
        assert_embedded_rows([_EMBEDDED_ROW_40])

    def test_features_row(self) -> None:
        """Right after the command line."""
        features = [row for _, row in _table_rows(_doc_part("## Features"))]
        firsts = [row[0] for row in features]
        assert _FEATURES_ROW_40 in features, features
        cli = firsts.index("Command line (`surreal-orm-lite`: migrations, inspectdb, shell)")
        assert firsts.index(_FEATURES_ROW_40[0]) == cli + 1

    def test_roadmap_rows(self) -> None:
        roadmap = [row for _, row in _table_rows(_doc_part("## Roadmap"))]
        start = [row[0] for row in roadmap].index("v0.39.0")
        assert roadmap[start : start + 3] == _ROADMAP_ROWS_40, roadmap

    def test_comparison_row(self) -> None:
        """Right after the migrations and the CLI."""
        comparison = [row for _, row in _table_rows(_doc_part("## SurrealDB-ORM-lite vs SurrealDB-ORM"))]
        firsts = [row[0] for row in comparison]
        assert comparison[firsts.index("Migrations & CLI") + 1] == _COMPARISON_ROW_40, comparison

    def test_section_26_points_to_section_41(self) -> None:
        """The hand-written ``surreal_db`` recipe stays, followed by the plugin sentence; the bullet that promised the
        factories in v0.40.0 points to section 41."""
        section = _doc_part(_SECTION_26)
        fixtures = _normalised(_subsection("### Test fixtures", section))
        assert _normalised(_PLUGIN_SENTENCE_26) in fixtures, fixtures
        coming = _subsection("### Coming from the full SurrealDB-ORM", section)
        assert _FACTORIES_BULLET_26 in _bullets(coming), coming
        assert "come in v0.40.0" not in _normalised(section)

    def test_no_section_promises_v0_40_0(self) -> None:
        """No section before section 41 still says the factories come in v0.40.0."""
        before = _normalised(_manual()[: _manual().index(f"\n{_SECTION_41}\n")])
        for promise in ("come in v0.40.0", "comes in v0.40.0", "factories v0.40.0"):
            assert promise not in before, promise

    @pytest.mark.parametrize(
        "heading",
        ["## Features", "## Roadmap", "## SurrealDB-ORM-lite vs SurrealDB-ORM"],
        ids=["features", "roadmap", "comparison"],
    )
    def test_no_pipe_in_a_cell(self, heading: str) -> None:
        for table in _md_tables(_doc_part(heading)):
            assert len({line.count("|") for line in table}) == 1, table
            assert "\\|" not in "".join(table)
