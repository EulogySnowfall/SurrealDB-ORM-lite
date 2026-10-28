"""Migration system (v0.38.0): the documentation — README section 39, the rows of the README's tables, and the earlier
sections' forward references to the migrations, rewritten now that they exist (AC-26).

No server: every test reads ``README.md``. ``TestReadmeSection39`` pins section 39 — its place, its eleven parts in
order, its code blocks word for word (the generated-file example is what ``MigrationGenerator`` writes), its operations
table against the operations' own statements, and the facts each part must state. ``TestReadmeRows38`` pins the rows
v0.38.0 adds to the 2.x/3.x and embedded-engine tables, the 2.x/3.x table's header sentence, and the features, roadmap
and comparison rows. ``TestReadmeForwardReferences`` checks that each sentence promising the migrations "with v0.38.0"
is gone and its rewrite is there, the rule for removing a clause a model no longer declares stated once.
"""

import ast
import re
from pathlib import Path

import pytest

from surreal_orm_lite.migrations import (
    AddField,
    AlterField,
    AlterTable,
    CreateIndex,
    CreateTable,
    DefineAnalyzer,
    DefineEvent,
    DefineFunction,
    DropField,
    DropIndex,
    DropTable,
    MigrationGenerator,
    Operation,
    RebuildIndex,
    RemoveAccess,
    RemoveAnalyzer,
    RemoveEvent,
    RemoveFunction,
)
from tests._readme_tables import assert_embedded_rows, assert_server_header_sentence, assert_server_rows
from tests._readme_tables import normalised as _normalised
from tests._readme_tables import readme as _readme
from tests._readme_tables import readme_part as _readme_part
from tests._readme_tables import subsection as _subsection
from tests._readme_tables import table_rows as _table_rows
from tests._readme_tables import tables as _md_tables

_SECTION_38 = "### 38. Hybrid search (`hybrid_search()`, Reciprocal Rank Fusion)"
_SECTION_39 = "### 39. Migrations (`makemigrations()`, `MigrationExecutor`, operations)"
_ANCHOR_39 = "#39-migrations-makemigrations-migrationexecutor-operations"
_LINK_39 = f"[section 39]({_ANCHOR_39})"
#: The eleven parts of section 39, in order, after its untitled intro.
_PARTS_39 = [
    "Write a migration",
    "Apply it",
    "Change the models",
    "Data migrations",
    "Roll back",
    "Operations",
    "Drift",
    "Server lines",
    "Limits",
    "Coming from the full SurrealDB-ORM",
]

# --- The code blocks, word for word -----------------------------------------------------------------------------------

_BLOCK_WRITE = """from surreal_orm_lite import BaseSurrealModel, SchemaMode, SurrealConfigDict
from surreal_orm_lite.migrations import makemigrations


class Person(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="person", schema_mode=SchemaMode.SCHEMAFULL)
    name: str
    age: int = 18


class Post(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="post", schema_mode=SchemaMode.SCHEMAFULL)
    title: str
    body: str


path = await makemigrations("initial", models=[Person, Post])
print(path)
# migrations/0001_initial.py
"""
#: The generated file of the writer's documentation, its ``Generated:`` line fixed.
_GENERATED_FILE = '''"""
Migration: 0002_add_email
Generated: 2026-10-27T12:00:00+00:00

Auto-generated migration file. Review before applying.
"""

from surreal_orm_lite.migrations import Migration
from surreal_orm_lite.migrations.operations import (
    AddField,
    CreateIndex,
)


migration = Migration(
    name="0002_add_email",
    dependencies=['0001_initial'],
    operations=[
        AddField(table='person', name='email', field_type='string'),
        CreateIndex(table='person', name='person_email', fields=['email'], unique=True),
    ],
)
'''
_BLOCK_APPLY = """from surreal_orm_lite.migrations import MigrationExecutor

executor = MigrationExecutor("migrations")
print(await executor.migrate())
# ['0001_initial', '0002_add_email']
print((await executor.get_migration_status())["0002_add_email"])
# {'applied': True, 'reversible': True, 'has_data': False, 'operations': 2}
print(await executor.show_sql("0002_add_email"))
# DEFINE FIELD `email` ON `person` TYPE string;
#
# DEFINE INDEX `person_email` ON `person` FIELDS `email` UNIQUE;
"""
_BLOCK_CHANGE = """await makemigrations("add_email", models=[Person, Post])  # 0002_add_email.py
print(await makemigrations("add_email_again", models=[Person, Post]))
# None
"""
_BLOCK_DATA = """from surreal_orm_lite.migrations import AddField, DataMigration, Migration

migration = Migration(
    name="0003_person_slug",
    dependencies=["0002_add_email"],
    operations=[
        AddField(table="person", name="slug", field_type="option<string>"),
        DataMigration(
            forwards_sql="UPDATE person SET slug = string::lowercase(name);",
            backwards_sql="UPDATE person UNSET slug;",
        ),
    ],
)
"""
_BLOCK_ROLLBACK = """print(await executor.rollback("0001_initial"))
# ['0002_add_email']
await executor.rollback("")  # undo every migration
"""
_BLOCK_DRIFT = """from surreal_orm_lite import schema_diff

for operation in await schema_diff(models=[Person, Post]):
    print(operation.describe())
"""
#: Each part's blocks, in order (the parts not named hold none).
_BLOCKS_BY_PART_39 = {
    "Write a migration": [_BLOCK_WRITE, _GENERATED_FILE],
    "Apply it": [_BLOCK_APPLY],
    "Change the models": [_BLOCK_CHANGE],
    "Data migrations": [_BLOCK_DATA],
    "Roll back": [_BLOCK_ROLLBACK],
    "Drift": [_BLOCK_DRIFT],
}

# --- The operations table ---------------------------------------------------------------------------------------------

_INDEX = CreateIndex(table="person", name="person_email", fields=["email"], unique=True)
#: The operation each row of the "Operations" table shows, by its first cell; ``None``: the row shows no statement of
#: an instance (``DataMigration``, ``RawSQL``) or an abridged one (``DefineAccess``).
_TABLE_OPERATIONS: dict[str, Operation | None] = {
    "CreateTable": CreateTable(name="log"),
    "AlterTable": AlterTable(name="person", schema_mode="SCHEMALESS", previous_schema_mode="SCHEMAFULL"),
    "DropTable": DropTable("log"),
    "AddField": AddField(table="person", name="age", field_type="int", default=18),
    "AlterField": AlterField(
        table="person", name="age", field_type="int", default=21, previous_type="int", previous_default=18
    ),
    "DropField": DropField(table="person", name="age"),
    "CreateIndex": _INDEX,
    "DropIndex": DropIndex("person", "person_email", previous=_INDEX),
    "RebuildIndex": RebuildIndex("post", "post_body"),
    "DefineAnalyzer": DefineAnalyzer(name="english", tokenizers=["class", "blank"], filters=["lowercase", "snowball(english)"]),
    "RemoveAnalyzer": RemoveAnalyzer("english"),
    "DefineEvent": DefineEvent("audit", "person", "$event = 'CREATE'", "CREATE log SET who = $after.name"),
    "RemoveEvent": RemoveEvent("audit", "person"),
    "DefineAccess": None,
    "RemoveAccess": RemoveAccess("account"),
    "DefineFunction": DefineFunction("fn::greet", "RETURN 'hi ' + $n;", parameters={"n": "string"}, returns="string"),
    "RemoveFunction": RemoveFunction("fn::greet"),
    "DataMigration": None,
    "RawSQL": None,
}
#: The "Reversible" cell of each row.
_REVERSIBLE_CELLS = {
    "CreateTable": "yes",
    "AlterTable": "with `previous_schema_mode`",
    "DropTable": "no",
    "AddField": "yes",
    "AlterField": "with `previous_type`",
    "DropField": "no",
    "CreateIndex": "yes (`overwrite=True` without `previous=`: no)",
    "DropIndex": "with `previous=`",
    "RebuildIndex": "yes",
    "DefineAnalyzer": "yes",
    "RemoveAnalyzer": "with `previous=`",
    "DefineEvent": "yes (`overwrite=True` without `previous=`: no)",
    "RemoveEvent": "with `previous=`",
    "DefineAccess": "yes (`overwrite=True` without `previous=`: no)",
    "RemoveAccess": "with `previous=`",
    "DefineFunction": "yes (`overwrite=True` without `previous=`: no)",
    "RemoveFunction": "with `previous=`",
    "DataMigration": "with one of them",
    "RawSQL": "with `reverse_sql`",
}

# --- What each part states (key phrases, whitespace normalised) -------------------------------------------------------

_NEEDLES_39 = {
    "": (
        "with the full SurrealDB-ORM's API",
        "applies each one atomically, in one SurrealDB transaction together with its record in the "
        "`_surreal_orm_migrations` table",
        "`rollback()` undoes them",
        "on SurrealDB 3.x, 2.x and the embedded engine",
    ),
    "Write a migration": (
        "By default it reads the connected database and replays on top the migration files not yet applied (the full "
        "ORM CLI's `--from-db`), so it needs the connection",
        "`from_db=False` diffs against the migration files alone, with no request (offline)",
        "returns `None` and writes nothing when there is no change",
        "`get_registered_models()`",
        "`generate_empty_migration(",
    ),
    "Apply it": (
        "`migrate()` first reads the database's schema",
        "replays the pending migrations over it and checks them",
        "applies each migration in one transaction with its record",
        "a table made by `define_table()` before any migration can be altered by one",
        "restores them to the definition the database held if the rest of the migration fails",
        "Another process applying the same migration at the same time is safe",
        '`migrate(target="0001_initial")`',
        '`migrate(target="0001_initial", fake=True)` records the migrations up to that one without applying them, then '
        "`migrate()` applies the next ones",
        "`MigrationStatementError`",
        "never the statement",
    ),
    "Change the models": (
        "A second `makemigrations()`, run before `migrate()`, writes nothing: the pending file is replayed over the "
        "database, so a new migration never repeats a pending one",
        "a clause the model does not declare stays as the database has it, as with `define_table()`",
        "[section 35](#35-model-table-ddl-define_table-schema_mode-permissions) says how to remove one",
        "Without `drop_missing=True`, removals",
        "reversible or not, are left out and named in a warning",
        "With `from_db=False` a removal is relative to the files: a model deleted since the last migration",
        "A removal of something neither the database nor an earlier migration holds is refused before any change",
        "a `RawSQL` that defines something declares it in `state_operations=`",
        "a recorded migration whose dependency is not recorded is an error everywhere but in `get_migration_status()`, "
        "which warns",
        "a recorded migration without its file is an error in `makemigrations()`, `upgrade()` and `rollback()`, and a "
        "warning in `migrate()` and `get_migration_status()`",
    ),
    "Data migrations": ("`DataMigration`", "`upgrade()`", "`migrate(schema_only=False)`", "write them idempotent"),
    "Roll back": (
        "`rollback(target)` undoes the migrations applied after _target_",
        '`""` undoes them all',
        "Every migration to undo must be reversible, or nothing is undone",
        "Creations are undone with `REMOVE … IF EXISTS`",
        "redefinitions and the removals of indexes, analyzers, events, accesses and functions are reversible when they "
        "carry `previous=`",
        "`DropTable` and `DropField` are irreversible",
        "a redefinition whose previous definition the ORM cannot write",
    ),
    "Operations": ("never build an operation from user input", "Migration files are Python code"),
    "Drift": (
        "`schema_diff()` lists the operations that would bring the database to the models — `[]` when they agree",
        "`1.5` and `1.5f`, `7d` and `1w`",
        "the diff right after `migrate()` or `define_table()` is empty on every line",
        "`SchemaState.diff()` between two states read from databases",
        "compares every clause: carrying what a model leaves to the database applies only to a state built from models",
    ),
    "Server lines": (
        "A full-text `CreateIndex` writes `FULLTEXT` on 3.x and `SEARCH` on 2.x and the embedded engine",
        "On SurrealDB 2.x and the embedded engine, an `AddField` or `AlterField` of an `any` column of a `SCHEMAFULL` "
        "table writes `FLEXIBLE`, and an `AlterTable` that makes a table `SCHEMAFULL` also redefines its `any` columns, "
        "untyped lists and lists of sub-models `FLEXIBLE` in the same transaction, so their object values are kept",
        "`migrate()` and `rollback()` render from the database's schema with the migrations replayed, while "
        "`show_sql()` renders from the migration files alone",
        "`migrate()`'s batch may also redefine `FLEXIBLE` columns defined outside migrations that `show_sql(line=2)` "
        "does not show",
        "When the database's definition of a table cannot be read entirely, and the `FLEXIBLE` clauses of such an "
        "`AlterTable`, or of a field holding `any` or a list of objects, would be chosen from that reading, "
        "`migrate()` and `rollback()` raise `SurrealDbError` on those lines before any change",
        "`DefineEvent(async_mode=True)`, `DefineAccess(with_refresh=True)` and `COMPUTED` fields need SurrealDB 3.x",
        "`record_references`",
        "The embedded engine behaves as a 2.x server and applies each migration atomically too",
    ),
    "Limits": (
        "surrealdb/surrealdb#7618",
        "A migration holding `DefineAnalyzer` is atomic **except** its analyzer definitions, committed first",
        "if the rest fails they are restored, and the error says whether that worked",
        "Analyzers are written with `OVERWRITE` first, so an analyzer of the same name made outside migrations is "
        "replaced, and when the rest of the migration fails the definition the database held is restored (or the "
        "analyzer removed when the database held none)",
        "A migration that redefines an analyzer whose current definition the ORM cannot read or write is refused "
        "before any change: redefine that analyzer in a form `Analyzer` accepts, by hand or with `define_indexes()`, "
        "then migrate again",
        "A redefined, removed or re-created access gets a new signing key from SurrealDB, so the tokens issued before "
        "are refused",
        "Live queries on a table a migration or a rollback removes are killed by SurrealDB: 3.x ends the subscription, "
        "2.x goes silent",
        "Over HTTP the SDK gives up after 30 s",
        "with `from_db=False` the clauses set on the database outside migrations are replaced (with the default "
        "`from_db=True` they are read and kept)",
        "`migrate()` and `rollback()` read the schema once, before their first change",
        "Functions are not compared",
        "the CLI comes with v0.39.0",
    ),
}
#: What "Limits" no longer says (revision 4's analyzer and ``AlterTable`` wording).
_ABSENT_FROM_LIMITS = (
    "written from the migration files' state",
    "removed again when the migration fails and the migration files did not define it before",
    "`any` columns of a table an `AlterTable` makes `SCHEMAFULL`",
)
#: The seven bullets every reader porting full-ORM code needs, each by a phrase, in this order, among the others.
_COMING_FROM_PORTING = (
    'rewrite it as `SurrealFunc(\'…\')` (for example `default="time::now()"` becomes `default=SurrealFunc("time::now()")`)',
    "runs a migration's `DataMigration`s after its schema transaction, where the full ORM runs them in position",
    "an unknown `migrate()` or `rollback()` target raises `ValueError`, where the full ORM compares names as strings "
    "(`n <= target`, `name > target`)",
    "a `RawSQL` without `reverse_sql` or a `RebuildIndex` stays in the first list",
    "the full ORM's `--no-from-db` diffs against an empty schema",
    "`get_registered_models()` is provided",
    "**`migrate()` and `rollback()` read the schema first**",
)
#: The other "Coming from" facts, each by a phrase.
_COMING_FROM_OTHERS = (
    "`from surreal_orm_lite.migrations import …`",
    "with the same default — it reads the database",
    "**One transaction per migration**",
    "**The same tracking table**: `_surreal_orm_migrations`",
    "names in backticks, `TYPE NORMAL`",
    "**`previous=`**",
    "**`default=` is data**",
    "`DefaultExpression`",
    "**Not provided**",
    "**`MigrationStatementError` is a `SurrealDbError`** and a `RuntimeError`",
    "**What the diff carries**",
    "**Creations are undone with `REMOVE … IF EXISTS`**",
    "**Operations are frozen**",
    "**`SchemaState.applied_migrations` is filled**",
    "**`DefineFunction` and `RemoveFunction` are added**; `parse_define_*` are not exported",
)
_REGISTRY_BULLET = (
    "- **The model registry**: `get_registered_models()` is provided (the model classes alive, read from the subclasses "
    "of `BaseSurrealModel`; a class whose name starts with `_` is left out but its subclasses are listed, as in the full "
    "ORM); they come depth first — each class's subclasses right after it, in definition order —, not in the full ORM's "
    "global definition order; `clear_model_registry()` is not provided, as there is no registry to clear."
)


def _section_39() -> str:
    """Section 39, as written, without the ``---`` that ends it."""
    body, separator, rest = _readme_part(_SECTION_39).rpartition("\n---\n")
    assert separator and not rest.strip(), "section 39 ends with the --- of the next part"
    return f"{body}\n"


def _part_39(heading: str) -> str:
    """One ``####`` part of section 39, as written (the intro, its heading included, for ``""``)."""
    if not heading:
        return _section_39().split("\n#### ", 1)[0]
    return _subsection(f"#### {heading}", _section_39())


def _blocks(text: str) -> list[str]:
    """The ``python`` blocks of *text*, in order."""
    found: list[str] = re.findall(r"^```python\n(.*?)^```$", text, re.MULTILINE | re.DOTALL)
    return found


def _bullets(text: str) -> list[str]:
    """The ``- `` bullets of *text*, each with its continuation lines, whitespace normalised."""
    return [_normalised(bullet) for bullet in re.findall(r"^- .*?(?=^- |^\s*$|\Z)", text, re.MULTILINE | re.DOTALL)]


def _anchor(heading: str) -> str:
    """GitHub's anchor of a README heading."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def _code(cell: str) -> str:
    """The text of a cell that is one code span (single or double backticks)."""
    match = re.fullmatch(r"(`+) ?(.*?) ?\1", cell)
    assert match, f"not one code span: {cell!r}"
    return match.group(2)


class TestReadmeSection39:
    """README section 39: its place, its parts, its blocks, its operations table and what each part states."""

    def test_section_39_follows_section_38(self) -> None:
        """Right after section 38 (after its "Coming from" bullets), the last section before the ``---`` of
        "Configuration Options"."""
        text = _readme()
        start_38 = text.index(f"\n{_SECTION_38}\n")
        start_39 = text.index(f"\n{_SECTION_39}\n")
        configuration = text.index("\n## Configuration Options\n")
        assert start_38 < start_39 < configuration
        assert re.findall(r"^#{2,3} .*$", text[start_38 + 1 : configuration], re.MULTILINE) == [_SECTION_38, _SECTION_39]
        assert text[:configuration].rstrip().endswith("\n---"), "section 39 ends before the --- of the next part"

    def test_parts_in_order(self) -> None:
        """The intro, then the ten titled parts of the design, in order, as ``####`` headings."""
        assert re.findall(r"^#### (.*)$", _section_39(), re.MULTILINE) == _PARTS_39

    def test_the_blocks_word_for_word(self) -> None:
        """Each part's blocks exactly and in order; the intro and the other parts hold none; ``python`` fences only."""
        expected = [block for heading in _PARTS_39 for block in _BLOCKS_BY_PART_39.get(heading, [])]
        assert _blocks(_section_39()) == expected
        assert _blocks(_part_39("")) == []
        for heading in _PARTS_39:
            assert _blocks(_part_39(heading)) == _BLOCKS_BY_PART_39.get(heading, []), heading
        fences = re.findall(r"^```(\S*)$", _section_39(), re.MULTILINE)
        assert fences == ["python", ""] * len(expected), fences

    def test_the_blocks_are_python(self) -> None:
        """Every block parses as Python, top-level ``await`` included."""
        for block in _blocks(_section_39()):
            compile(block, "<README section 39>", "exec", flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)

    def test_the_generated_file_is_the_writers(self, tmp_path: Path) -> None:
        """The generated-file example is what ``MigrationGenerator.generate()`` writes for its operations, but for the
        time on its ``Generated:`` line."""
        (tmp_path / "0001_initial.py").write_text(
            'from surreal_orm_lite.migrations import Migration\nmigration = Migration(name="0001_initial")\n'
        )
        path = MigrationGenerator(tmp_path).generate(
            "add_email",
            [
                AddField(table="person", name="email", field_type="string"),
                CreateIndex(table="person", name="person_email", fields=["email"], unique=True),
            ],
            dependencies=["0001_initial"],
        )
        written = re.sub(r"^Generated: .*$", "Generated: 2026-10-27T12:00:00+00:00", path.read_text(), flags=re.MULTILINE)
        assert path.name == "0002_add_email.py"
        assert written == _GENERATED_FILE

    @pytest.mark.parametrize("heading", list(_NEEDLES_39))
    def test_what_each_part_states(self, heading: str) -> None:
        """Each part states its facts (key phrases, whitespace normalised)."""
        part = _normalised(_part_39(heading))
        missing = [needle for needle in _NEEDLES_39[heading] if _normalised(needle) not in part]
        assert missing == [], missing

    def test_limits_no_longer_say(self) -> None:
        """No analyzer "written from the migration files' state" nor "removed again" item, no item on the ``any``
        columns of an ``AlterTable`` (both lines now render from the database's schema)."""
        limits = _normalised(_part_39("Limits"))
        for phrase in _ABSENT_FROM_LIMITS:
            assert _normalised(phrase) not in limits, phrase

    def test_coming_from_the_full_orm(self) -> None:
        """Every bullet a fact, the seven porting bullets (a)–(g) among them in that order, the registry bullet word for
        word."""
        bullets = _bullets(_part_39("Coming from the full SurrealDB-ORM"))
        assert _REGISTRY_BULLET in bullets
        found = []
        for phrase in _COMING_FROM_PORTING:
            holders = [index for index, bullet in enumerate(bullets) if _normalised(phrase) in bullet]
            assert len(holders) == 1, (phrase, holders)
            found.append(holders[0])
        assert found == sorted(found), found
        text = " ".join(bullets)
        missing = [phrase for phrase in _COMING_FROM_OTHERS if _normalised(phrase) not in text]
        assert missing == [], missing

    def test_no_design_reference(self) -> None:
        """README prose: no decision or measure number (``D12``, ``C8``, ``M27``, ``F3``), no "spec", "revision", ``§``
        or local path, no issue number but SurrealDB's."""
        section = _section_39()
        assert re.findall(r"\b[DCMFPR]\d+\b", section) == []
        for marker in (r"\bspec\b", r"\brevision\b", "§", r"\.claude"):
            assert re.search(marker, section, re.IGNORECASE) is None, marker
        assert re.findall(r"(?<![(\w/])#\d+", section) == []

    def test_operations_table(self) -> None:
        """One row per operation of ``surreal_orm_lite.migrations``, in the package's order; each statement shown is
        the operation's own ``forwards()`` (SurrealDB 3.x), each "Undone by" its ``backwards()`` when one is shown,
        and a "Reversible" cell that says ``yes`` or ``no`` is the instance's ``reversible``."""
        rows = [cells for _, cells in _table_rows(_part_39("Operations"), "Operation")]
        assert [_code(cells[0]) for cells in rows] == list(_TABLE_OPERATIONS)
        for cells in rows:
            name = _code(cells[0])
            assert cells[3] == _REVERSIBLE_CELLS[name], cells
            operation = _TABLE_OPERATIONS[name]
            if operation is None:
                continue
            assert _code(cells[1]) == operation.forwards(), cells
            undone = cells[2].removesuffix(", or `previous=`")
            if undone.startswith("`"):
                assert _code(undone) == operation.backwards(), cells
            if cells[3] == "no" or cells[3].startswith("yes"):
                assert operation.reversible == (cells[3] != "no"), cells
        assert rows[13][1].startswith("`DEFINE ACCESS account ON DATABASE TYPE RECORD SIGNUP ( … ) SIGNIN ( … )")

    def test_one_table_without_a_raw_pipe(self) -> None:
        tables = _md_tables(_section_39())
        assert len(tables) == 1
        assert len({line.count("|") for line in tables[0]}) == 1 and "\\|" not in "".join(tables[0])

    def test_links_resolve(self) -> None:
        """Every in-page link of section 39 names a heading: section 35, the embedded table, the 2.x/3.x table."""
        anchors = {_anchor(heading) for heading in re.findall(r"^#{2,4} (.*)$", _readme(), re.MULTILINE)}
        links = re.findall(r"\]\(#([^)]+)\)", _section_39())
        assert links == [
            "35-model-table-ddl-define_table-schema_mode-permissions",
            "orm-behaviour-the-embedded-engine",
            "orm-behaviour-surrealdb-2x-vs-3x",
        ], links
        assert [link for link in links if link not in anchors] == []
        assert _ANCHOR_39.lstrip("#") in anchors


# --- The tables of the README -----------------------------------------------------------------------------------------

_HEADER_SENTENCE_38 = "Rows added in v0.38.0 were measured on 2.7.0, 3.3.4 and the embedded engine."
#: Exactly the seven rows v0.38.0 adds to the 2.x/3.x table, cell by cell.
_MIGRATION_ROWS = [
    ["Migrations: full-text index in `CreateIndex`", "`SEARCH`", "`FULLTEXT`", "v0.38.0"],
    [
        "Migrations: `any` column of a `SCHEMAFULL` table, also when an `AlterTable` makes the table `SCHEMAFULL`",
        "`FLEXIBLE` written; the `AlterTable` redefines the columns the database holds in its transaction, without which "
        "2.x empties their object values at the next write",
        "not written: 3.x keeps the values and refuses `FLEXIBLE` on `any`",
        "v0.38.0",
    ],
    ["Migrations: `ASYNC` events, `WITH REFRESH`", "refused before any DDL (`SurrealDbError`)", "applied", "v0.38.0"],
    [
        "Migrations: `REFERENCE`",
        "refused by the server unless `record_references` is enabled (`MigrationStatementError`)",
        "applied",
        "v0.38.0",
    ],
    [
        "Migrations: `COMPUTED` fields in `AddField`/`AlterField`",
        "refused before any DDL (`SurrealDbError`)",
        "applied",
        "v0.38.0",
    ],
    ["Migrations: a live query on a table a migration removes", "no further notification", "the subscription ends", "v0.38.0"],
    [
        "Migrations: a migration's atomicity, concurrent `migrate()`",
        "same on both lines: each migration and its record in one transaction; a concurrent run skips a migration the "
        "other recorded",
        "same on both lines",
        "v0.38.0",
    ],
]
_EMBEDDED_MIGRATION_ROW = [
    "Migrations (v0.38.0)",
    "as on a 2.x server: `SEARCH` for a full-text `CreateIndex`, `FLEXIBLE` on `any` columns (also in the redefinitions "
    "of an `AlterTable` that makes a table `SCHEMAFULL`), `ASYNC` events, `WITH REFRESH` and `COMPUTED` fields refused "
    "before any DDL; each migration atomic, its analyzers committed first and compensated as on the servers",
    "3.x writes `FULLTEXT`, no `FLEXIBLE` on `any`, and applies `ASYNC`, `WITH REFRESH` and `COMPUTED`",
    "v0.38.0",
]
_FEATURES_ROW_39 = ["Migrations (makemigrations / migrate / rollback)", "✅"]
_ROADMAP_ROWS_39 = [
    ["v0.37.0", "Tier 3 — Hybrid search", "✅ Released"],
    ["v0.38.0", "Tier 3 — Migration system", "✅ Released"],
    ["v0.39.0 – v0.40.0", "Tier 3 — Advanced (CLI, test factories)", "📋 Planned"],
    ["v0.41.0", "Beta Phase (API freeze, hardening)", "📋 Planned"],
]
_COMPARISON_ROW_39 = ["Migrations & CLI", "✅ v0.38.0 (migrations), CLI v0.39.0", "✅"]


class TestReadmeRows38:
    """The rows v0.38.0 adds to the README's tables and the 2.x/3.x table's header sentence."""

    def test_server_behaviour_rows(self) -> None:
        """Exactly seven rows, Since v0.38.0, together and in version order before the table's last row; the
        ``FLEXIBLE`` row names the ``AlterTable`` case."""
        assert_server_rows(_MIGRATION_ROWS)
        assert "`AlterTable`" in _MIGRATION_ROWS[1][0] and "`AlterTable`" in _MIGRATION_ROWS[1][1]

    def test_server_behaviour_header_sentence(self) -> None:
        assert_server_header_sentence(_HEADER_SENTENCE_38)

    def test_embedded_table_row(self) -> None:
        assert_embedded_rows([_EMBEDDED_MIGRATION_ROW])

    def test_features_row(self) -> None:
        """Right after hybrid search."""
        features = [row for _, row in _table_rows(_readme_part("## Features"))]
        firsts = [row[0] for row in features]
        assert _FEATURES_ROW_39 in features, features
        hybrid = firsts.index("Hybrid search (`hybrid_search()`, Reciprocal Rank Fusion)")
        assert firsts.index(_FEATURES_ROW_39[0]) == hybrid + 1

    def test_roadmap_rows(self) -> None:
        roadmap = [row for _, row in _table_rows(_readme_part("## Roadmap"))]
        start = [row[0] for row in roadmap].index("v0.37.0")
        assert roadmap[start : start + 4] == _ROADMAP_ROWS_39, roadmap
        assert "v0.38.0 – v0.40.0" not in [row[0] for row in roadmap], roadmap

    def test_comparison_row(self) -> None:
        """Right after hybrid search, before the fixtures."""
        comparison = [row for _, row in _table_rows(_readme_part("## SurrealDB-ORM-lite vs SurrealDB-ORM"))]
        firsts = [row[0] for row in comparison]
        start = firsts.index("Hybrid Search (RRF)")
        assert comparison[start + 1] == _COMPARISON_ROW_39, comparison
        assert firsts[start + 2] == "Test Fixtures & Factories", firsts


# --- The forward references -------------------------------------------------------------------------------------------

#: Each sentence that promised the migrations "with v0.38.0", by the section that held it, and its rewrite
#: (whitespace normalised).
_FORWARD_REFERENCES = [
    (
        "### 31. Schema introspection (`DatabaseIntrospector`, `generate_models_from_db()`)",
        "- **`SchemaState.diff()` and `schema_diff()` come with v0.38.0** (migrations), as does `applied_migrations`; "
        "the `inspectdb` command comes with v0.39.0.",
        f"- **`SchemaState.diff()`, `schema_diff()` and `applied_migrations`** come with the migrations ({_LINK_39}): "
        "`applied_migrations` is filled from the tracking table when the database has one (full ORM: declared, never "
        "filled; it is the 2nd attribute, positional as in the full ORM); the `inspectdb` command comes with v0.39.0.",
    ),
    (
        "### 32. Server-side events and stored functions (`Event`, `define_events()`, `define_function()`)",
        "`define_events()` does not compare the database with the model (that comes with migrations, v0.38.0).",
        "`define_events()` does not compare the database with the model; the migrations do, and "
        f"`makemigrations(drop_missing=True)` writes its `RemoveEvent` ({_LINK_39}).",
    ),
    (
        "### 32. Server-side events and stored functions (`Event`, `define_events()`, `define_function()`)",
        "- **The migration operations** (`DefineEvent`, `RemoveEvent`, `RawSQL`) and the removal of the events a model "
        "no longer declares **come with migrations (v0.38.0)**.",
        "- **The migration operations** (`DefineEvent`, `RemoveEvent`, `RawSQL`) are in `surreal_orm_lite.migrations`, "
        f"and `makemigrations(drop_missing=True)` removes the events a model no longer declares ({_LINK_39}).",
    ),
    (
        "### 33. Materialized views (`view_query`, `define_view()`)",
        "full-ORM code calling `define_table()` on a view works. Migrations come with v0.38.0.",
        "full-ORM code calling `define_table()` on a view works. Migrations (`CreateTable`/`AlterTable` with "
        f"`view_query`) are in {_LINK_39}.",
    ),
    (
        "### 34. Relation constraints (`relation_in`, `define_relation()`)",
        "then its data fields; migrations come with v0.38.0.",
        f"then its data fields; migrations (`CreateTable`/`AlterTable` with the relation keys) are in {_LINK_39}.",
    ),
    (
        "### 35. Model table DDL (`define_table()`, `schema_mode`, `permissions`)",
        "A key left out never removes a stored clause: remove a field, a change feed or a comment with raw SurrealQL "
        "(migrations come with v0.38.0).",
        f"A key left out never removes a stored clause, and the migrations' diff leaves it too ({_LINK_39}): to remove "
        "a clause the model no longer declares, write an `AlterTable` or `AlterField` migration by hand; a field the "
        "model no longer declares is removed by the `DropField` that `makemigrations(drop_missing=True)` writes.",
    ),
    (
        "### 35. Model table DDL (`define_table()`, `schema_mode`, `permissions`)",
        "The full ORM raises `MigrationStatementError` (a `RuntimeError` from `surreal_orm.migrations`) with the SQL in "
        "its message.",
        "The full ORM raises `MigrationStatementError` (a `RuntimeError` from `surreal_orm.migrations`) with the SQL in "
        f"its message; this library's `MigrationStatementError` ({_LINK_39}) is raised by the migrations only, and "
        "`define_table()` keeps `SurrealDbError`.",
    ),
    (
        "### 36. Full-text search (`search()`, `SearchScore`, `SearchHighlight`, `Analyzer`, `Index`)",
        "the full ORM declares analyzers and indexes in migrations, which this library adds in v0.38.0.",
        "the full ORM declares analyzers and indexes in migrations, which this library also has (`DefineAnalyzer`, "
        f"`CreateIndex`, {_LINK_39}).",
    ),
]


class TestReadmeForwardReferences:
    """The sentences of sections 31 to 36 that promised the migrations for v0.38.0, each rewritten in its section."""

    @pytest.mark.parametrize(("section", "old", "new"), _FORWARD_REFERENCES, ids=range(len(_FORWARD_REFERENCES)))
    def test_rewritten(self, section: str, old: str, new: str) -> None:
        """The old sentence is gone, the new one is in its section."""
        text = _normalised(_readme_part(section))
        assert _normalised(old) not in text
        assert _normalised(new) in text

    def test_no_section_promises_v0_38_0(self) -> None:
        """No section before section 39 still says the migrations come with v0.38.0."""
        before = _normalised(_readme()[: _readme().index(f"\n{_SECTION_39}\n")])
        for promise in ("come with v0.38.0", "comes with migrations", "come with migrations", "adds in v0.38.0"):
            assert promise not in before, promise

    def test_the_removal_rule_is_stated_once(self) -> None:
        """How to remove a clause a model no longer declares is said in section 35 only; section 39 links to it."""
        text = _normalised(_readme())
        assert text.count("write an `AlterTable` or `AlterField` migration by hand") == 1
        assert text.count("A key left out never removes a stored clause") == 1
        assert "[section 35](#35-model-table-ddl-define_table-schema_mode-permissions) says how to remove one" in text
