"""Migration system (v0.38.0): the ``Migration`` class, migration names, ``MigrationStatementError`` and the writer.

``Migration`` is the full SurrealDB-ORM's migration-file object, frozen: its dependencies and operations are kept as
tuples and checked at construction. ``parse_migration_name()`` and ``generate_migration_name()`` are the full ORM's
helpers. ``MigrationStatementError`` is caught by ``except SurrealDbError`` (the lite's family) and by
``except RuntimeError`` (the full ORM's base), and survives ``pickle`` with its keyword-only attributes.

``MigrationGenerator`` and ``generate_empty_migration()`` (spec §5.8, ``TestGenerator``) write ``<NNNN>_<name>.py``: the
full ORM's layout, each argument equal to its default left out, each value written as Python source (escaped data for
a ``str``), each import only when used, a call longer than 120 columns wrapped; an existing file is never replaced and a
value the file cannot write raises before the file exists. A generated file imported back gives the operations it was
written from, exactly (``TestGeneratedRoundTrip``, AC-7). Unit tests only (no server).
"""

import dataclasses
import enum
import importlib.util
import math
import pickle
import re
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import Path

import pytest

import surreal_orm_lite.migrations as migrations
from surreal_orm_lite import SurrealFunc
from surreal_orm_lite.exceptions import SurrealDbError
from surreal_orm_lite.migrations import (
    MIGRATIONS_TABLE,
    AddField,
    AlterField,
    AlterTable,
    CreateIndex,
    CreateTable,
    DataMigration,
    DefaultExpression,
    DefineAccess,
    DefineAnalyzer,
    DefineEvent,
    DefineFunction,
    DropField,
    DropIndex,
    DropTable,
    Migration,
    MigrationGenerator,
    MigrationStatementError,
    Operation,
    RawSQL,
    RebuildIndex,
    RemoveAccess,
    RemoveAnalyzer,
    RemoveEvent,
    RemoveFunction,
    generate_empty_migration,
    introspect_models,
)
from surreal_orm_lite.migrations import generator as generator_module
from surreal_orm_lite.migrations.constants import MIGRATIONS_TABLE as CONSTANTS_TABLE
from surreal_orm_lite.migrations.executor import MigrationStatementError as ExecutorError
from surreal_orm_lite.migrations.migration import generate_migration_name, parse_migration_name
from surreal_orm_lite.schema_state import FieldState, SchemaState, TableState
from tests.test_migration_diff import _MODEL_SET


class _SubclassedError(MigrationStatementError):
    """A subclass at module level, so that pickle can find it by name."""


class TestMigrationClass:
    def test_defaults(self) -> None:
        before = datetime.now(UTC)
        migration = Migration(name="0001_initial")
        after = datetime.now(UTC)
        assert migration.name == "0001_initial"
        assert migration.dependencies == ()
        assert migration.operations == ()
        assert migration.created_at.tzinfo is not None
        assert migration.created_at.utcoffset() == timedelta(0)
        assert before <= migration.created_at <= after

    def test_lists_are_kept_as_tuples(self) -> None:
        migration = Migration(name="0002_add_email", dependencies=["0001_initial"], operations=[])
        assert migration.dependencies == ("0001_initial",)
        assert isinstance(migration.dependencies, tuple)
        assert isinstance(migration.operations, tuple)

    def test_a_given_created_at_is_kept(self) -> None:
        moment = datetime(2026, 1, 1, tzinfo=UTC)
        assert Migration(name="0001_initial", created_at=moment).created_at == moment

    def test_frozen(self) -> None:
        migration = Migration(name="0001_initial")
        with pytest.raises(dataclasses.FrozenInstanceError):
            migration.name = "0002_other"  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            migration.dependencies = ("0000_x",)  # type: ignore[misc]

    @pytest.mark.parametrize("name", ["initial", "0001-initial", "0001_", "_initial", "0001_initial.py", "0001_a b", ""])
    def test_a_malformed_name_is_refused(self, name: str) -> None:
        with pytest.raises(ValueError, match="Invalid migration name"):
            Migration(name=name)

    def test_a_name_with_a_trailing_newline_is_refused(self) -> None:
        with pytest.raises(ValueError, match="Invalid migration name"):
            Migration(name="0001_initial\n")

    def test_a_non_ascii_name_is_refused(self) -> None:
        with pytest.raises(ValueError, match="Invalid migration name"):
            Migration(name="0001_café")

    def test_a_name_of_another_type_is_refused(self) -> None:
        with pytest.raises(TypeError, match="Migration name must be a str, not int"):
            Migration(name=1)  # type: ignore[arg-type]

    def test_a_dependency_equal_to_the_name_is_refused(self) -> None:
        with pytest.raises(ValueError, match="Migration '0001_a' cannot depend on itself"):
            Migration(name="0001_a", dependencies=["0001_a"])

    def test_a_malformed_dependency_is_refused(self) -> None:
        with pytest.raises(ValueError, match="Migration '0002_b': invalid dependency name 'initial'"):
            Migration(name="0002_b", dependencies=["initial"])

    def test_a_dependency_of_another_type_is_refused(self) -> None:
        with pytest.raises(TypeError, match="Migration '0002_b': dependency 1 must be a str, not int"):
            Migration(name="0002_b", dependencies=[1])  # type: ignore[list-item]

    def test_a_single_string_as_dependencies_is_refused(self) -> None:
        # A bare str is a sequence of one-character strings: almost always a forgotten list.
        with pytest.raises(TypeError, match="dependencies must be a list or tuple of migration names, not str"):
            Migration(name="0002_b", dependencies="0001_a")

    def test_dependencies_of_another_type_are_refused(self) -> None:
        with pytest.raises(TypeError, match="dependencies must be a list or tuple of migration names, not set"):
            Migration(name="0002_b", dependencies={"0001_a"})  # type: ignore[arg-type]

    def test_a_non_operation_item_is_refused(self) -> None:
        with pytest.raises(TypeError, match=r"Migration '0001_a': operation 1 is not an Operation \(got object\)"):
            Migration(name="0001_a", operations=[object()])  # type: ignore[list-item]

    def test_operations_of_another_type_are_refused(self) -> None:
        with pytest.raises(TypeError, match="operations must be a list or tuple of operations, not str"):
            Migration(name="0001_a", operations="CreateTable")  # type: ignore[arg-type]

    def test_repr(self) -> None:
        assert repr(Migration(name="0001_initial")) == "Migration(name='0001_initial', operations=0)"

    def test_describe_an_empty_migration(self) -> None:
        assert Migration(name="0001_initial").describe() == "Migration: 0001_initial\nOperations (0):"

    def test_describe_lists_the_dependencies(self) -> None:
        migration = Migration(name="0003_c", dependencies=["0001_a", "0002_b"])
        assert migration.describe() == "Migration: 0003_c\nDependencies: 0001_a, 0002_b\nOperations (0):"

    def test_an_empty_migration_is_reversible(self) -> None:
        assert Migration(name="0001_initial").is_reversible is True

    def test_equality_compares_every_field(self) -> None:
        moment = datetime(2026, 1, 1, tzinfo=UTC)
        assert Migration(name="0001_a", created_at=moment) == Migration(name="0001_a", dependencies=[], created_at=moment)
        assert Migration(name="0001_a", created_at=moment) != Migration(name="0001_b", created_at=moment)

    def test_operation_is_abstract(self) -> None:
        with pytest.raises(TypeError):
            Operation()  # type: ignore[abstract]

    def test_schema_and_data_operations(self) -> None:
        table = CreateTable(name="t")
        data = DataMigration(forwards_sql="UPDATE t SET a = 1;")
        field = AddField("t", "a", "int")
        raw = RawSQL("UPDATE t SET b = 2;")
        migration = Migration(name="0001_a", operations=[table, data, field, raw])
        assert migration.has_data_migrations is True
        assert migration.schema_operations == [table, field, raw]
        assert migration.data_operations == [data]
        assert isinstance(migration.schema_operations, list)
        schema_only = Migration(name="0001_a", operations=[table])
        assert (schema_only.has_data_migrations, schema_only.data_operations) == (False, [])
        assert Migration(name="0001_a").schema_operations == []

    def test_is_reversible_with_operations(self) -> None:
        assert Migration(name="0001_a", operations=[CreateTable(name="t"), AddField("t", "a", "int")]).is_reversible
        assert not Migration(name="0001_a", operations=[CreateTable(name="t"), DropTable("old")]).is_reversible

    def test_describe_with_operations(self) -> None:
        migration = Migration(name="0001_a", operations=[CreateTable(name="t"), DropTable("user-events")])
        assert migration.describe() == (
            "Migration: 0001_a\nOperations (2):\n  1. Create table t\n  2. Drop table 'user-events'"
        )
        assert repr(migration) == "Migration(name='0001_a', operations=2)"


class TestMigrationNames:
    def test_parse(self) -> None:
        assert parse_migration_name("0001_initial.py") == (1, "initial")
        assert parse_migration_name("0002_add_email") == (2, "add_email")
        assert parse_migration_name("0010_a_b_c.py") == (10, "a_b_c")

    def test_parse_without_an_underscore(self) -> None:
        with pytest.raises(ValueError, match="^Invalid migration filename: initial$"):
            parse_migration_name("initial.py")

    def test_parse_without_a_number(self) -> None:
        with pytest.raises(ValueError, match="^Invalid migration number in: abc_initial$"):
            parse_migration_name("abc_initial.py")

    @pytest.mark.parametrize(
        ("number", "name", "expected"),
        [
            (2, "Add Email", "0002_add_email"),
            (1, "initial", "0001_initial"),
            (2, "add-email!", "0002_add_email"),
            (3, "Add  E-mail (v2)", "0003_add__e_mail_v2"),
            (12345, "big", "12345_big"),
            (4, "café", "0004_caf"),
        ],
    )
    def test_generate(self, number: int, name: str, expected: str) -> None:
        generated = generate_migration_name(number, name)
        assert generated == expected
        assert Migration(name=generated).name == generated  # always a name Migration accepts

    @pytest.mark.parametrize("name", ["!!!", "", "   ", "---", "éé"])
    def test_generate_without_a_letter_or_digit(self, name: str) -> None:
        with pytest.raises(ValueError) as caught:
            generate_migration_name(2, name)
        assert str(caught.value) == f"A migration name needs at least one letter or digit: {name!r}"

    def test_round_trip(self) -> None:
        assert parse_migration_name(generate_migration_name(7, "Add Email") + ".py") == (7, "add_email")


class TestMigrationStatementError:
    def _error(self) -> MigrationStatementError:
        return MigrationStatementError(
            "x", migration="0001_a", operation=2, description="Drop table t", analyzers_restored=True
        )

    def test_the_tracking_table_name(self) -> None:
        assert MIGRATIONS_TABLE == "_surreal_orm_migrations"
        assert CONSTANTS_TABLE is MIGRATIONS_TABLE

    def test_one_class_everywhere(self) -> None:
        assert ExecutorError is MigrationStatementError

    def test_caught_as_the_lite_s_and_the_full_orm_s_base(self) -> None:
        error = self._error()
        assert isinstance(error, SurrealDbError)
        assert isinstance(error, RuntimeError)
        with pytest.raises(SurrealDbError):
            raise error
        with pytest.raises(RuntimeError):
            raise error

    def test_attributes_and_message(self) -> None:
        error = self._error()
        assert str(error) == "x"
        assert error.args == ("x",)
        assert error.migration == "0001_a"
        assert error.operation == 2
        assert error.description == "Drop table t"
        assert error.analyzers_restored is True

    def test_defaults(self) -> None:
        error = MigrationStatementError("y", migration="0001_a")
        assert (error.operation, error.description, error.analyzers_restored) == (None, None, None)

    def test_migration_is_keyword_only_and_required(self) -> None:
        with pytest.raises(TypeError):
            MigrationStatementError("x", "0001_a")  # type: ignore[misc]
        with pytest.raises(TypeError):
            MigrationStatementError("x")  # type: ignore[call-arg]

    @pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
    def test_pickle(self, protocol: int) -> None:
        error = self._error()
        error.add_note("a note")
        clone = pickle.loads(pickle.dumps(error, protocol=protocol))
        assert type(clone) is MigrationStatementError
        assert str(clone) == "x"
        assert clone.args == ("x",)
        assert (clone.migration, clone.operation, clone.description, clone.analyzers_restored) == (
            "0001_a",
            2,
            "Drop table t",
            True,
        )
        assert clone.__notes__ == ["a note"]

    def test_pickle_with_the_defaults(self) -> None:
        clone = pickle.loads(pickle.dumps(MigrationStatementError("y", migration="0001_a")))
        assert (str(clone), clone.migration, clone.operation, clone.description, clone.analyzers_restored) == (
            "y",
            "0001_a",
            None,
            None,
            None,
        )

    def test_pickle_keeps_a_subclass(self) -> None:
        clone = pickle.loads(pickle.dumps(_SubclassedError("z", migration="0001_a", operation=1)))
        assert type(clone) is _SubclassedError
        assert (str(clone), clone.migration, clone.operation) == ("z", "0001_a", 1)


def test_the_package_exports() -> None:
    # The names that exist after Task 6 of v0.38.0's plan; the later tasks add the rest of spec §5.1.
    assert set(migrations.__all__) == {
        "MIGRATIONS_TABLE",
        "Migration",
        "MigrationStatementError",
        "Operation",
        "CreateTable",
        "AlterTable",
        "DropTable",
        "AddField",
        "AlterField",
        "DropField",
        "CreateIndex",
        "DropIndex",
        "RebuildIndex",
        "DefineAnalyzer",
        "RemoveAnalyzer",
        "DefineEvent",
        "RemoveEvent",
        "DefineAccess",
        "RemoveAccess",
        "DefineFunction",
        "RemoveFunction",
        "DataMigration",
        "RawSQL",
        "DefaultExpression",
        "split_destructive",
        "ModelIntrospector",
        "introspect_models",
        "MigrationGenerator",
        "generate_empty_migration",
    }
    for name in migrations.__all__:
        assert getattr(migrations, name) is not None


# --- the writer: MigrationGenerator, generate_empty_migration() --------------------------------------------------------

#: Spec §5.8's file, without its ``Generated:`` line.
_SPEC_FILE = '''"""
Migration: 0002_add_email

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

_GENERATED = re.compile(r"^Generated: (.*)\n", re.MULTILINE)


def _without_generated(text: str) -> str:
    """*text* without its one ``Generated:`` line, which holds the current UTC time to the second."""
    found = _GENERATED.findall(text)
    assert len(found) == 1
    stamp = datetime.fromisoformat(found[0])
    assert stamp.utcoffset() == timedelta(0)
    assert stamp.microsecond == 0
    assert abs(datetime.now(UTC) - stamp) < timedelta(minutes=5)
    return _GENERATED.sub("", text)


def _generate(directory: Path, *operations: Operation, name: str = "x") -> str:
    """The text of the file ``generate()`` writes for *operations* in *directory*, without its ``Generated:`` line."""
    return _without_generated(MigrationGenerator(directory).generate(name, list(operations)).read_text(encoding="utf-8"))


def _imports(text: str) -> list[str]:
    """The import lines of a generated file (between its docstring and ``migration = …``), blank lines included."""
    return text.split('"""\n\n', 1)[1].split("\n\n\nmigration = Migration(", 1)[0].splitlines()


def _calls(text: str) -> str:
    """The operations of a generated file: the lines between ``operations=[`` and its closing ``],``."""
    return text.split("    operations=[\n", 1)[1].rsplit("\n    ],\n)\n", 1)[0]


def _load(path: Path) -> Migration:
    """The ``migration`` of the file at *path*, imported as the loader imports it (not inserted in ``sys.modules``)."""
    spec = importlib.util.spec_from_file_location(f"generated_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    migration = module.migration
    assert isinstance(migration, Migration)
    return migration


def _add_field_x(field_type: str, source: str) -> str:
    """The source of ``AddField("person", "x", field_type, <default>)`` in a file's operations: one line when it fits
    within 120 columns, else one argument per line."""
    arguments = ["table='person'", "name='x'", f"field_type='{field_type}'", f"default={source}"]
    flat = f"        AddField({', '.join(arguments)}),"
    if len(flat) <= 120:
        return flat
    return "        AddField(\n" + "".join(f"            {argument},\n" for argument in arguments) + "        ),"


#: The import lines of a file whose operations are ``AddField``s only.
_ADD_FIELD_IMPORTS = [
    "from surreal_orm_lite.migrations import Migration",
    "from surreal_orm_lite.migrations.operations import (",
    "    AddField,",
    ")",
]


class _Color(enum.StrEnum):
    RED = "red"


class _Rank(enum.IntEnum):
    ONE = 1


class _Ratio(float):
    """A ``float`` subclass, written as the ``float`` it is."""


class _Stamp(datetime):
    """A ``datetime`` subclass, written as the ``datetime`` it is."""


class _Plus2(tzinfo):
    """A fixed offset that is not a ``datetime.timezone``: its ``repr()`` does not load back."""

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return timedelta(hours=2)

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "+02"


class _OwnRawSQL(RawSQL):
    """An operation class the writer does not know (not one of ``migrations.operations``)."""


async def _backfill() -> None:
    """A data migration's function."""


class TestGenerator:
    """Spec §5.8: the directory, the number, the file's text, each value's source, the wrapping and the refusals."""

    def test_the_directory_and_its_package(self, tmp_path: Path) -> None:
        directory = tmp_path / "app" / "migrations"
        generator = MigrationGenerator(directory)
        assert generator.migrations_dir == directory
        generator.ensure_directory()
        init = directory / "__init__.py"
        assert init.read_text(encoding="utf-8") == '"""Auto-generated migrations package."""\n'
        init.write_text("# edited\n", encoding="utf-8")
        generator.ensure_directory()
        assert init.read_text(encoding="utf-8") == "# edited\n"

    def test_the_directory_as_a_str_or_a_path_like(self, tmp_path: Path) -> None:
        assert MigrationGenerator(str(tmp_path)).migrations_dir == tmp_path
        assert isinstance(MigrationGenerator(str(tmp_path)).migrations_dir, Path)
        assert MigrationGenerator(tmp_path).migrations_dir == tmp_path

    def test_the_next_number_of_a_missing_directory(self, tmp_path: Path) -> None:
        directory = tmp_path / "missing"
        assert MigrationGenerator(directory).get_next_number() == 1
        assert not directory.exists()

    def test_the_next_number_of_an_empty_directory(self, tmp_path: Path) -> None:
        assert MigrationGenerator(tmp_path).get_next_number() == 1

    def test_the_next_number_follows_the_highest(self, tmp_path: Path) -> None:
        for name in ("0001_a.py", "0007_b.py", "_x.py", "__init__.py", "notes.txt", "0042_c.txt", "abc_d.py", "readme.py"):
            (tmp_path / name).write_text("", encoding="utf-8")
        assert MigrationGenerator(tmp_path).get_next_number() == 8

    def test_the_spec_file(self, tmp_path: Path) -> None:
        (tmp_path / "0001_initial.py").write_text("", encoding="utf-8")
        operations = [
            AddField("person", "email", "string"),
            CreateIndex("person", "person_email", ["email"], unique=True),
        ]
        path = MigrationGenerator(tmp_path).generate("Add Email", operations, ["0001_initial"])
        assert path == tmp_path / "0002_add_email.py"
        assert _without_generated(path.read_text(encoding="utf-8")) == _SPEC_FILE

    def test_the_numbers_follow_each_other(self, tmp_path: Path) -> None:
        generator = MigrationGenerator(tmp_path)
        first = generator.generate("initial", [CreateTable("person")])
        second = generator.generate("Add Email", [AddField("person", "email", "string")], [first.stem])
        assert (first.name, second.name) == ("0001_initial.py", "0002_add_email.py")
        assert _load(second).dependencies == ("0001_initial",)

    def test_an_empty_migration(self, tmp_path: Path) -> None:
        (tmp_path / "0001_initial.py").write_text("", encoding="utf-8")
        path = generate_empty_migration(tmp_path, "custom", ["0001_initial"])
        assert path == tmp_path / "0002_custom.py"
        assert _without_generated(path.read_text(encoding="utf-8")) == (
            '"""\n'
            "Migration: 0002_custom\n"
            "\n"
            "Auto-generated migration file. Review before applying.\n"
            '"""\n'
            "\n"
            "from surreal_orm_lite.migrations import Migration\n"
            "\n"
            "\n"
            "migration = Migration(\n"
            '    name="0002_custom",\n'
            "    dependencies=['0001_initial'],\n"
            "    operations=[],\n"
            ")\n"
        )
        migration = _load(path)
        assert (migration.name, migration.dependencies, migration.operations) == ("0002_custom", ("0001_initial",), ())

    def test_an_empty_migration_without_dependencies(self, tmp_path: Path) -> None:
        path = generate_empty_migration(str(tmp_path / "migrations"), "Data fix")
        assert path == tmp_path / "migrations" / "0001_data_fix.py"
        assert (tmp_path / "migrations" / "__init__.py").exists()
        assert "    dependencies=[],\n" in path.read_text(encoding="utf-8")
        assert _load(path).dependencies == ()

    def test_arguments_equal_to_their_default_are_left_out(self, tmp_path: Path) -> None:
        text = _generate(
            tmp_path,
            CreateTable("person"),
            CreateTable("bare", schema_mode=None),
            AddField("person", "age", "int", None, None, flexible=False, nullable=False, path=["age"]),
            AddField("person", "a.b", "int", path=["a.b"]),
            DropField("person", "address.city"),
            DefineFunction("greet", "RETURN 1;"),
        )
        assert _calls(text) == (
            "        CreateTable(name='person'),\n"
            "        CreateTable(name='bare', schema_mode=None),\n"
            "        AddField(table='person', name='age', field_type='int'),\n"
            "        AddField(table='person', name='a.b', field_type='int', path=['a.b']),\n"
            "        DropField(table='person', name='address.city'),\n"
            "        DefineFunction(name='fn::greet', body='RETURN 1;'),"
        )

    @pytest.mark.parametrize(
        ("field_type", "default", "source", "imports"),
        [
            ("int", 18, "18", _ADD_FIELD_IMPORTS),
            ("string", "a;b", "'a;b'", _ADD_FIELD_IMPORTS),
            ("bool", True, "True", _ADD_FIELD_IMPORTS),
            ("float", 1.5, "1.5", _ADD_FIELD_IMPORTS),
            ("decimal", Decimal("1.10"), "Decimal('1.10')", ["from decimal import Decimal", "", *_ADD_FIELD_IMPORTS]),
            (
                "datetime",
                datetime(2026, 1, 1, tzinfo=UTC),
                "datetime.datetime(2026, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)",
                ["import datetime", "", *_ADD_FIELD_IMPORTS],
            ),
            (
                "datetime",
                SurrealFunc("time::now()"),
                "SurrealFunc('time::now()')",
                ["from surreal_orm_lite import SurrealFunc", *_ADD_FIELD_IMPORTS],
            ),
            (
                "int",
                DefaultExpression("{ LET $x = 1; RETURN $x; }"),
                "DefaultExpression('{ LET $x = 1; RETURN $x; }')",
                [*_ADD_FIELD_IMPORTS[:3], "    DefaultExpression,", ")"],
            ),
        ],
        ids=["int", "str", "bool", "float", "decimal", "datetime", "surreal_func", "default_expression"],
    )
    def test_a_default(self, tmp_path: Path, field_type: str, default: object, source: str, imports: list[str]) -> None:
        operation = AddField("person", "x", field_type, default)
        text = _generate(tmp_path, operation)
        assert _calls(text) == _add_field_x(field_type, source)
        assert _imports(text) == imports
        assert _load(tmp_path / "0001_x.py").operations == (operation,)

    def test_every_import_in_order(self, tmp_path: Path) -> None:
        text = _generate(
            tmp_path,
            AddField("person", "at", "datetime", datetime(2026, 1, 1, tzinfo=UTC)),
            AlterField("person", "price", "decimal", Decimal("1.10"), previous_type="float", previous_default=1.5),
            AddField("person", "made", "datetime", SurrealFunc("time::now()")),
            AddField("person", "n", "int", DefaultExpression("1 + 1")),
            CreateIndex("person", "by_n", ["n"]),
        )
        assert _imports(text) == [
            "import datetime",
            "from decimal import Decimal",
            "",
            "from surreal_orm_lite import SurrealFunc",
            "from surreal_orm_lite.migrations import Migration",
            "from surreal_orm_lite.migrations.operations import (",
            "    AddField,",
            "    AlterField,",
            "    CreateIndex,",
            "    DefaultExpression,",
            ")",
        ]

    def test_a_nested_operation_s_imports(self, tmp_path: Path) -> None:
        operation = RawSQL("DEFINE PARAM $x VALUE 1;", state_operations=[AddField("person", "y", "int", Decimal("2.5"))])
        text = _generate(tmp_path, operation)
        assert _imports(text) == [
            "from decimal import Decimal",
            "",
            "from surreal_orm_lite.migrations import Migration",
            "from surreal_orm_lite.migrations.operations import (",
            "    AddField,",
            "    RawSQL,",
            ")",
        ]
        assert _calls(text) == (
            "        RawSQL(\n"
            "            sql='DEFINE PARAM $x VALUE 1;',\n"
            "            state_operations=[AddField(table='person', name='y', field_type='int', default=Decimal('2.5'))],\n"
            "        ),"
        )

    def test_previous_as_a_nested_call(self, tmp_path: Path) -> None:
        text = _generate(
            tmp_path,
            DropIndex("person", "by_age", previous=CreateIndex("person", "by_age", ["age"])),
            RemoveEvent("gone", "person", previous=DefineEvent("gone", "person", None, "CREATE log SET at = time::now()")),
        )
        assert _calls(text) == (
            "        DropIndex(table='person', name='by_age', previous=CreateIndex(table='person', name='by_age', fields=['age'])),\n"
            "        RemoveEvent(\n"
            "            name='gone',\n"
            "            table='person',\n"
            "            previous=DefineEvent(name='gone', table='person', when=None, then='CREATE log SET at = time::now()'),\n"
            "        ),"
        )

    def test_a_long_call_wraps_at_120_columns(self, tmp_path: Path) -> None:
        previous = CreateIndex(
            "person",
            "person_embedding",
            ["embedding"],
            hnsw=True,
            dimension=1536,
            dist="COSINE",
            vector_type="F32",
            efc=150,
            m=12,
        )
        permissions = {"select": "WHERE published = true OR owner = $auth.id", "create": "WHERE $auth.id != NONE"}
        text = _generate(
            tmp_path,
            DropIndex("person", "person_embedding", previous=previous),
            CreateTable("post", "SCHEMALESS", permissions=permissions, comment="Posts"),
        )
        assert _calls(text) == (
            "        DropIndex(\n"
            "            table='person',\n"
            "            name='person_embedding',\n"
            "            previous=CreateIndex(\n"
            "                table='person',\n"
            "                name='person_embedding',\n"
            "                fields=['embedding'],\n"
            "                hnsw=True,\n"
            "                dimension=1536,\n"
            "                dist='COSINE',\n"
            "                vector_type='F32',\n"
            "                efc=150,\n"
            "                m=12,\n"
            "            ),\n"
            "        ),\n"
            "        CreateTable(\n"
            "            name='post',\n"
            "            schema_mode='SCHEMALESS',\n"
            "            permissions={\n"
            "                'select': 'WHERE published = true OR owner = $auth.id',\n"
            "                'create': 'WHERE $auth.id != NONE',\n"
            "                'update': 'NONE',\n"
            "                'delete': 'NONE',\n"
            "            },\n"
            "            comment='Posts',\n"
            "        ),"
        )
        assert max(len(line) for line in text.splitlines()) <= 120

    def test_long_dependencies_wrap(self, tmp_path: Path) -> None:
        dependencies = [f"{number:04d}_a_rather_long_branch_name" for number in range(1, 6)]
        path = MigrationGenerator(tmp_path).generate("merge", [], dependencies)
        text = path.read_text(encoding="utf-8")
        assert "    dependencies=[\n        '0001_a_rather_long_branch_name',\n" in text
        assert "        '0005_a_rather_long_branch_name',\n    ],\n    operations=[],\n" in text
        assert _load(path).dependencies == tuple(dependencies)

    def test_a_tuple_the_operation_needs(self, tmp_path: Path) -> None:
        bm25 = CreateIndex("post", "post_body", ["body"], search_analyzer="english", bm25=(1.2, 0.75))
        tags = AddField("post", "tags", "array<string>", ("a",))
        empty = AddField("post", "none", "array<string>", ())
        text = _generate(tmp_path, bm25, tags, empty)
        assert _calls(text) == (
            "        CreateIndex(table='post', name='post_body', fields=['body'], search_analyzer='english', bm25=(1.2, 0.75)),\n"
            "        AddField(table='post', name='tags', field_type='array<string>', default=('a',)),\n"
            "        AddField(table='post', name='none', field_type='array<string>', default=()),"
        )
        assert _load(tmp_path / "0001_x.py").operations == (bm25, tags, empty)

    def test_nested_containers_keep_their_types(self, tmp_path: Path) -> None:
        pairs = AddField("person", "pairs", "array", [["a", 1], ("b", None)])
        doc = AddField("person", "doc", "object", {"a": (1,), "b": {"c": [True]}})
        text = _generate(tmp_path, pairs, doc)
        assert _calls(text) == (
            "        AddField(table='person', name='pairs', field_type='array', default=[['a', 1], ('b', None)]),\n"
            "        AddField(table='person', name='doc', field_type='object', default={'a': (1,), 'b': {'c': [True]}}),"
        )
        assert _load(tmp_path / "0001_x.py").operations == (pairs, doc)

    def test_a_string_is_written_as_escaped_data(self, tmp_path: Path) -> None:
        comment = 'it\'s "quoted"\nand \\ escaped """'
        operation = AddField("person", "x", "string", comment=comment)
        text = _generate(tmp_path, operation)
        assert _calls(text) == (
            "        AddField(table='person', name='x', field_type='string', comment='it\\'s \"quoted\"\\nand \\\\ escaped \"\"\"'),"
        )
        assert _load(tmp_path / "0001_x.py").operations == (operation,)

    @pytest.mark.parametrize(
        ("field_type", "default", "source"),
        [
            ("string", _Color.RED, "'red'"),
            ("int", _Rank.ONE, "1"),
            ("float", _Ratio(1.5), "1.5"),
            (
                "datetime",
                datetime(2026, 1, 1, 2, tzinfo=_Plus2()),
                "datetime.datetime(2026, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)",
            ),
            ("datetime", _Stamp(2026, 1, 1, tzinfo=UTC), "datetime.datetime(2026, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)"),
            (
                "datetime",
                datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=2))),
                "datetime.datetime(2026, 1, 1, 0, 0, tzinfo=datetime.timezone(datetime.timedelta(seconds=7200)))",
            ),
            ("datetime", datetime(2026, 1, 1), "datetime.datetime(2026, 1, 1, 0, 0)"),
        ],
        ids=["str_enum", "int_enum", "float_subclass", "other_tzinfo", "datetime_subclass", "fixed_offset", "naive"],
    )
    def test_a_value_is_written_as_its_base_type(self, tmp_path: Path, field_type: str, default: object, source: str) -> None:
        operation = AddField("person", "x", field_type, default)
        text = _generate(tmp_path, operation)
        assert _calls(text) == _add_field_x(field_type, source)
        loaded = _load(tmp_path / "0001_x.py").operations
        assert loaded == (operation,)
        assert type(loaded[0].default) in (str, int, float, datetime)  # type: ignore[attr-defined]

    def test_an_existing_file_is_never_replaced(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        existing = tmp_path / "0001_initial.py"
        existing.write_text("# mine\n", encoding="utf-8")
        generator = MigrationGenerator(tmp_path)
        monkeypatch.setattr(generator, "get_next_number", lambda: 1)  # another writer took the number meanwhile
        with pytest.raises(FileExistsError):
            generator.generate("initial", [CreateTable("person")])
        assert existing.read_text(encoding="utf-8") == "# mine\n"

    @pytest.mark.parametrize(
        ("operation", "message"),
        [
            (
                DataMigration(forwards_func=_backfill),
                "MigrationGenerator: operation 2 (Data migration): DataMigration.forwards_func= holds a function, which "
                "a migration file cannot write.",
            ),
            (
                AddField("person", "x", "float", math.nan),
                "MigrationGenerator: operation 2 (Add field x to person): AddField.default= holds a non-finite float, "
                "which a migration file cannot write.",
            ),
            (
                AddField("person", "x", "duration", timedelta(days=1)),
                "MigrationGenerator: operation 2 (Add field x to person): AddField.default= holds a timedelta, which a "
                "migration file cannot write.",
            ),
            (
                RawSQL("DEFINE PARAM $x VALUE 1;", state_operations=[AddField("person", "x", "set<int>", {1, 2})]),
                "MigrationGenerator: operation 2 (Raw SQL): AddField.default= holds a set, which a migration file "
                "cannot write.",
            ),
            (
                _OwnRawSQL("DEFINE PARAM $x VALUE 1;"),
                "MigrationGenerator: operation 2 (Raw SQL) is a _OwnRawSQL, not an operation of "
                "surreal_orm_lite.migrations.operations; a migration file writes those only.",
            ),
        ],
        ids=["function", "nan", "timedelta", "nested_set", "own_class"],
    )
    def test_an_unwritable_operation_raises_before_the_file_exists(
        self, tmp_path: Path, operation: Operation, message: str
    ) -> None:
        directory = tmp_path / "migrations"
        with pytest.raises(TypeError) as caught:
            MigrationGenerator(directory).generate("x", [CreateTable("person"), operation])
        assert str(caught.value) == message
        assert not directory.exists()

    def test_an_operation_that_does_not_load_back_equal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        operation = RebuildIndex("person", "by_age")
        monkeypatch.setattr(RebuildIndex, "__eq__", lambda self, other: False)
        with pytest.raises(TypeError) as caught:
            MigrationGenerator(tmp_path).generate("x", [operation])
        assert str(caught.value) == (
            "MigrationGenerator: operation 1 (Rebuild index by_age on person) does not load back equal from its "
            "arguments, so a migration file cannot write it."
        )
        assert list(tmp_path.iterdir()) == []

    def test_the_name_and_the_migration_are_checked_first(self, tmp_path: Path) -> None:
        generator = MigrationGenerator(tmp_path)
        with pytest.raises(TypeError, match=r"^MigrationGenerator: name must be a str, not int\.$"):
            generator.generate(1, [])  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="A migration name needs at least one letter or digit"):
            generator.generate("!!!", [])
        with pytest.raises(ValueError, match="invalid dependency name 'initial'"):
            generator.generate("x", [], ["initial"])
        with pytest.raises(TypeError, match=r"operation 1 is not an Operation \(got str\)"):
            generator.generate("x", ["CreateTable"])  # type: ignore[list-item]
        assert list(tmp_path.iterdir()) == []

    def test_the_module_exports(self) -> None:
        assert generator_module.__all__ == ["MigrationGenerator", "generate_empty_migration"]
        assert migrations.MigrationGenerator is generator_module.MigrationGenerator
        assert migrations.generate_empty_migration is generator_module.generate_empty_migration


# --- AC-7: a generated file loads back to the operations it was written from -----------------------------------------

_SIGNUP = {"id": "$email", "email": "$email", "password": "crypto::argon2::generate($password)"}
_SIGNIN = "email = $email AND crypto::argon2::compare(password, $password)"
_ACCOUNT = DefineAccess("account", "users", _SIGNUP, _SIGNIN)

#: Every operation, with every argument it takes set at least once.
_OPERATIONS: list[Operation] = [
    CreateTable(
        "person",
        "SCHEMAFULL",
        "normal",
        changefeed="1h INCLUDE ORIGINAL",
        permissions={"select": "WHERE $auth.id = id", "create": "FULL"},
        comment="People",
        drop=True,
    ),
    CreateTable("adults", None, view_query="SELECT * FROM person WHERE age >= 18", comment="it's a view"),
    CreateTable("likes", "SCHEMALESS", "relation", relation_in="person", relation_out=["post", "comment"], enforced=True),
    AlterTable(
        "person",
        "SCHEMAFULL",
        "normal",
        changefeed="1d",
        comment="People",
        previous_schema_mode="SCHEMALESS",
        previous_table_type="any",
        previous_permissions={"select": "FULL"},
        previous_comment="Old",
        drop=True,
    ),
    AlterTable(
        "likes",
        "SCHEMALESS",
        "relation",
        relation_in="person",
        relation_out="post | comment",
        enforced=True,
        previous_schema_mode="SCHEMALESS",
        previous_table_type="relation",
        previous_relation_in="person",
        previous_relation_out="post",
        previous_changefeed="2d INCLUDE ORIGINAL",
        previous_drop=True,
    ),
    AlterTable("adults", view_query="SELECT * FROM person", previous_view_query="SELECT * FROM person WHERE age >= 18"),
    DropTable("user-events"),
    AddField(
        "person",
        "email",
        "string",
        None,
        "string::len($value) > 3",
        readonly=True,
        comment="Mail",
        permissions={"select": "FULL", "update": "WHERE $auth.id = id"},
        overwrite=True,
    ),
    AddField("person", "address.city", "string", "Paris", value="string::trim($value)", nullable=True),
    AddField("person", "tags.*", "string"),
    AddField("person", "a.b", "int", 18, path=["a.b"], default_always=True),
    AddField("person", "full", "string", computed="string::concat(name, ' ', email)"),
    AddField("person", "owner", "record<person>", reference=True, on_delete="THEN UPDATE $this SET gone = true"),
    AddField("person", "prefs", "object", flexible=True),
    AddField("person", "made", "datetime", SurrealFunc("time::now()")),
    AddField("person", "seven", "int", DefaultExpression("{ LET $x = 7; RETURN $x; }")),
    AddField("person", "sep", "string", "a;b"),
    AddField("person", "ratio", "float", 1.5),
    AddField("person", "price", "decimal", Decimal("1.10")),
    AddField("person", "joined", "datetime", datetime(2026, 1, 1, tzinfo=UTC)),
    AddField("person", "flag", "bool", True),
    AddField("person", "big", "int", -(2**63)),
    AddField("person", "pairs", "array", [["a", 1], ("b", 2.5)]),
    AddField("person", "doc", "object", {"a": (1, 2), "b": {"c": None, "d": [Decimal("0.5")]}}),
    AlterField(
        "person",
        "price",
        "decimal",
        Decimal("1.10"),
        "$value > 0",
        flexible=False,
        readonly=True,
        nullable=True,
        comment="Price",
        permissions={"select": "WHERE $auth.id != NONE"},
        default_always=True,
        previous_type="float",
        previous_default=1.5,
        previous_assertion="$value >= 0",
        previous_readonly=True,
        previous_nullable=True,
        previous_comment="Old price",
        previous_permissions={"update": "NONE"},
        previous_default_always=True,
    ),
    AlterField(
        "person",
        "owner",
        "record<person>",
        reference=True,
        on_delete="cascade",
        value="$value",
        previous_type="record<person>",
        previous_reference=True,
        previous_on_delete="REJECT",
        previous_value="$before",
        previous_flexible=True,
    ),
    AlterField("person", "full", computed="string::uppercase(name)", previous_computed="string::concat(name, ' ', email)"),
    AlterField("person", "at", "datetime", previous_type="datetime", previous_default=DefaultExpression("time::now()")),
    DropField("user-events", "first-name"),
    DropField("person", "a.b", path=["a.b"]),
    CreateIndex("person", "person_email", ["email"], unique=True, comment="it's unique"),
    CreateIndex("post", "post_body", ["body"], search_analyzer="english", bm25=(1.2, 0.75), highlights=True),
    CreateIndex("post", "post_title", ["title"], search_analyzer="english", bm25=True),
    CreateIndex("person", "person_emb", ["emb"], hnsw=True, dimension=1536, dist="COSINE", vector_type="F32", efc=150, m=12),
    CreateIndex("person", "by_name", ("last", "first")),
    CreateIndex("person", "person_email", ["email"], unique=True, previous=CreateIndex("person", "person_email", ["email"])),
    CreateIndex("person", "by_age", ["age"], overwrite=True),
    DropIndex("person", "by_age", previous=CreateIndex("person", "by_age", ["age"])),
    DropIndex("user-events", "idx-x"),
    RebuildIndex("post", "post_body"),
    RebuildIndex("post", "post_body", if_exists=True),
    DefineAnalyzer(
        "english",
        ["class", "blank"],
        ["lowercase", "snowball(english)"],
        comment="English",
        previous=DefineAnalyzer("english", ["blank"]),
    ),
    DefineAnalyzer("custom", function="fn::clean"),
    RemoveAnalyzer("english", previous=DefineAnalyzer("english", ["blank"], ["lowercase"])),
    RemoveAnalyzer("2024_sales"),
    DefineEvent(
        "audit",
        "person",
        "$event = 'CREATE'",
        "CREATE log SET who = $after.id",
        "Audit",
        previous=DefineEvent("audit", "person", "$event = 'UPDATE'", "CREATE log SET who = $after.id"),
    ),
    DefineEvent("asy", "person", None, "CREATE log SET a = 1; CREATE log SET b = 2", async_mode=True, retry=1, max_depth=3),
    DefineEvent("again", "person", None, "CREATE log", overwrite=True),
    RemoveEvent("gone", "person", previous=DefineEvent("gone", "person", None, "CREATE log")),
    RemoveEvent("user-events", "first-name"),
    DefineAccess(
        "account",
        "users",
        _SIGNUP,
        _SIGNIN,
        "1h",
        "24h",
        "Users",
        with_refresh=True,
        duration_grant="30d",
        previous=_ACCOUNT,
    ),
    DefineAccess("account", "users", {"email": "$email"}, "email = $email", overwrite=True),
    RemoveAccess("account", previous=_ACCOUNT),
    RemoveAccess("old-access"),
    DefineFunction(
        "greet",
        "RETURN 'hi ' + $n;",
        parameters={"n": "string", "times": "int"},
        returns="string",
        permissions="WHERE $auth.id != NONE",
        comment="Greets",
    ),
    DefineFunction("fn::greet", "RETURN 2;", overwrite=True),
    RemoveFunction("fn::greet", previous=DefineFunction("greet", "RETURN 1;")),
    RemoveFunction("old"),
    DataMigration(forwards_sql="UPDATE person SET a = 1;", backwards_sql="UPDATE person SET a = NONE;", description="Backfill"),
    DataMigration("UPDATE person SET b = 2;"),
    RawSQL(
        "DEFINE PARAM $x VALUE 1; DEFINE PARAM $y VALUE 'a;b';",
        "REMOVE PARAM $x; REMOVE PARAM $y;",
        "Params",
        state_operations=[AddField("person", "y", "int"), CreateIndex("person", "by_y", ["y"])],
    ),
    RawSQL("UPDATE person SET c = 3;"),
]


def _field(name: str, kind: str, **changes: object) -> FieldState:
    nullable = bool(changes.pop("nullable", False))
    return FieldState(name=name, field_type=kind, nullable=nullable, table="person", **changes)  # type: ignore[arg-type]


class TestGeneratedRoundTrip:
    """AC-7 (spec §5.8): the file ``generate()`` writes, imported, holds a ``Migration`` whose operations equal those
    it was written from — exact dataclass equality, every value included."""

    @pytest.mark.parametrize("operation", _OPERATIONS, ids=lambda operation: type(operation).__name__)
    def test_each_operation(self, tmp_path: Path, operation: Operation) -> None:
        path = MigrationGenerator(tmp_path).generate("one", [operation])
        assert _load(path).operations == (operation,)

    def test_every_operation_in_one_file(self, tmp_path: Path) -> None:
        (tmp_path / "0001_initial.py").write_text("", encoding="utf-8")
        path = MigrationGenerator(tmp_path).generate("Everything", _OPERATIONS, ["0001_initial"])
        migration = _load(path)
        assert migration.name == path.stem == "0002_everything"
        assert migration.dependencies == ("0001_initial",)
        assert migration.operations == tuple(_OPERATIONS)
        assert max(len(line) for line in path.read_text(encoding="utf-8").splitlines()) <= 120

    def test_the_diff_of_the_models(self, tmp_path: Path) -> None:
        # What makemigrations() writes for a new database: every model kind v0.34.0 writes.
        operations = SchemaState().diff(introspect_models(_MODEL_SET))
        assert len(operations) > 40
        path = MigrationGenerator(tmp_path).generate("initial", operations)
        assert _load(path).operations == tuple(operations)

    def test_the_diff_between_two_states(self, tmp_path: Path) -> None:
        # Defaults read from the database: Python values when the literal reads back, DefaultExpression otherwise.
        current = SchemaState(
            {
                "person": TableState(
                    "person",
                    "SCHEMALESS",
                    "normal",
                    {
                        "at": _field("at", "datetime", default="time::now()"),
                        "price": _field("price", "decimal", default="1.10dec"),
                        "ratio": _field("ratio", "float", default="1.5f"),
                        "gone": _field("gone", "string"),
                    },
                )
            }
        )
        target = SchemaState(
            {
                "person": TableState(
                    "person",
                    "SCHEMAFULL",
                    "normal",
                    {
                        "at": _field("at", "datetime", default="d'2026-01-01T00:00:00Z'"),
                        "price": _field("price", "decimal", default="1.2dec"),
                        "ratio": _field("ratio", "float", default="NONE", nullable=True),
                        "sep": _field("sep", "string", default="'a;b'"),
                    },
                    comment="People",
                )
            }
        )
        operations = current.diff(target)
        kinds = [type(operation).__name__ for operation in operations]
        assert kinds.count("AlterField") == 3 and "AlterTable" in kinds and "AddField" in kinds and "DropField" in kinds
        path = MigrationGenerator(tmp_path).generate("changes", operations)
        assert _load(path).operations == tuple(operations)
