"""Migration system (v0.38.0): the ``Migration`` class, migration names, ``MigrationStatementError`` and the writer.

``Migration`` is the full SurrealDB-ORM's migration-file object, frozen: its dependencies and operations are kept as
tuples and checked at construction. ``parse_migration_name()`` and ``generate_migration_name()`` are the full ORM's
helpers. ``MigrationStatementError`` is caught by ``except SurrealDbError`` (the lite's family) and by
``except RuntimeError`` (the full ORM's base), and survives ``pickle`` with its keyword-only attributes.

``MigrationGenerator`` and ``generate_empty_migration()`` (spec §5.8, ``TestGenerator``) write ``<NNNN>_<name>.py``: the
full ORM's layout, each argument equal to its default left out, each value written as Python source (escaped data for
a ``str``), each import only when used, a call longer than 120 columns wrapped; an existing file is never replaced and a
value the file cannot write raises before the file exists. A generated file imported back gives the operations it was
written from, exactly (``TestGeneratedRoundTrip``, AC-7).

``MigrationExecutor``'s pure part (spec §5.5, §5.6 "Loading and order"): the files a directory holds
(``TestLoader``: discovery, ``load_migration()`` and its texts), their application order (``TestOrder``: the dependency
graph, ties by name, an unknown dependency, a cycle), the history checks of each caller (``TestHistory``: E-H1, an
applied name without a file — raised, warned once or ``FileNotFoundError`` per spec §5.6's table, D30 as amended, C10),
the files' state (``TestMigrationState``: the file replay, ``strict=False``, C9, M28) and ``show_sql()`` (``TestShowSql``:
both lines' forms, ``backwards=True``, R14, R25, R33). ``makemigrations(from_db=False)`` (``TestMakemigrationsOffline``,
AC-5, AC-6, AC-30's offline part): the files' state diffed against the models, written once — a second call with the
same models writes nothing —, removals only with ``drop_missing=True``. Unit tests only (no server): an executor is
built without a connection call.
"""

import dataclasses
import enum
import importlib.util
import inspect
import itertools
import logging
import math
import pickle
import re
import sys
import textwrap
import types
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pytest

import surreal_orm_lite.migrations as migrations
from surreal_orm_lite import BaseSurrealModel, SurrealFunc
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
    MigrationExecutor,
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
    loader,
    makemigrations,
    state,
)
from surreal_orm_lite.migrations import executor as executor_module
from surreal_orm_lite.migrations import generator as generator_module
from surreal_orm_lite.migrations.constants import MIGRATIONS_TABLE as CONSTANTS_TABLE
from surreal_orm_lite.migrations.executor import MigrationStatementError as ExecutorError
from surreal_orm_lite.migrations.migration import generate_migration_name, parse_migration_name
from surreal_orm_lite.schema_state import FieldState, SchemaState, TableState
from tests.test_migration_diff import _MODEL_SET, MiBare, MiOther
from tests.test_migration_operations import _R33_LINES, R33


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


# --- loading the files, their order and the history checks (spec §5.5, §5.6) -----------------------------------------


def _migration_file(directory: Path, name: str, *dependencies: str, operations: str = "") -> Path:
    """Write ``<directory>/<name>.py`` by hand: a migration named *name* depending on *dependencies*, whose
    operations are the Python source *operations* (``"CreateTable('t'), AddField('t', 'a', 'int')"``)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.py"
    path.write_text(
        "from surreal_orm_lite.migrations import AddField, CreateTable, DropField, DropTable, Migration\n\n"
        f"migration = Migration(name={name!r}, dependencies={list(dependencies)!r}, operations=[{operations}])\n",
        encoding="utf-8",
    )
    return path


def _not_found(directory: Path, name: str) -> str:
    """The full ORM's text for a migration file that does not exist."""
    return f"Migration file not found: {directory / f'{name}.py'}"


class TestLoader:
    """Spec §5.5: ``get_available_migrations()`` and ``load_migration()``, with their texts."""

    def test_the_discovery_rules(self, tmp_path: Path) -> None:
        for name in ("0002_b", "0010_c", "0001_a"):
            _migration_file(tmp_path, name)
        for other in ("__init__.py", "_draft.py", "_0003_x.py", "helper.py", "x_y.py", "notes.txt", "0004_d.txt"):
            (tmp_path / other).write_text("raise RuntimeError('never imported')\n", encoding="utf-8")
        executor = MigrationExecutor(tmp_path)
        assert executor.get_available_migrations() == ["0001_a", "0002_b", "0010_c"]
        assert executor.get_pending_migrations([]) == ["0001_a", "0002_b", "0010_c"]

    def test_a_missing_directory(self, tmp_path: Path) -> None:
        directory = tmp_path / "missing"
        executor = MigrationExecutor(directory)
        assert executor.get_available_migrations() == []
        assert executor.get_pending_migrations([]) == []
        assert executor.migration_state() == SchemaState()
        assert not directory.exists()

    def test_the_directory_as_a_str_or_a_path_like(self, tmp_path: Path) -> None:
        assert MigrationExecutor(str(tmp_path)).migrations_dir == tmp_path
        assert MigrationExecutor(tmp_path).migrations_dir == tmp_path

    def test_load_migration(self, tmp_path: Path) -> None:
        _migration_file(tmp_path, "0002_b", "0001_a", operations="CreateTable('t'), AddField('t', 'a', 'int')")
        migration = MigrationExecutor(tmp_path).load_migration("0002_b")
        assert type(migration) is Migration
        assert migration.name == "0002_b"
        assert migration.dependencies == ("0001_a",)
        assert migration.operations == (CreateTable("t"), AddField("t", "a", "int"))

    def test_the_file_is_not_inserted_in_sys_modules(self, tmp_path: Path) -> None:
        path = _migration_file(tmp_path, "0001_a")
        before = set(sys.modules)
        MigrationExecutor(tmp_path).load_migration("0001_a")
        assert set(sys.modules) == before
        assert not any(getattr(module, "__file__", None) == str(path) for module in list(sys.modules.values()))

    def test_each_load_runs_the_file_again(self, tmp_path: Path) -> None:
        _migration_file(tmp_path, "0001_a")
        executor = MigrationExecutor(tmp_path)
        assert executor.load_migration("0001_a") is not executor.load_migration("0001_a")

    def test_a_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError) as caught:
            MigrationExecutor(tmp_path).load_migration("0001_a")
        assert str(caught.value) == _not_found(tmp_path, "0001_a")

    @pytest.mark.parametrize(
        "source",
        [
            "x = 1\n",
            "migration = 1\n",
            "migration = None\n",
            # A class named like the full ORM's: the lite accepts its own Migration only.
            "class Migration:\n    name = '0001_a'\n\nmigration = Migration()\n",
        ],
        ids=["no migration", "an int", "None", "another Migration class"],
    )
    def test_a_file_without_a_migration(self, tmp_path: Path, source: str) -> None:
        (tmp_path / "0001_a.py").write_text(source, encoding="utf-8")
        with pytest.raises(ImportError) as caught:
            MigrationExecutor(tmp_path).load_migration("0001_a")
        assert str(caught.value) == "Migration file must define 'migration' variable: 0001_a"

    def test_a_file_that_cannot_be_loaded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _migration_file(tmp_path, "0001_a")
        monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda *args, **kwargs: None)
        with pytest.raises(ImportError) as caught:
            MigrationExecutor(tmp_path).load_migration("0001_a")
        assert str(caught.value) == "Could not load migration: 0001_a"

    def test_a_name_mismatch(self, tmp_path: Path) -> None:
        source = _migration_file(tmp_path, "0001_a").read_text(encoding="utf-8")
        (tmp_path / "0002_b.py").write_text(source, encoding="utf-8")
        with pytest.raises(ValueError) as caught:
            MigrationExecutor(tmp_path).load_migration("0002_b")
        assert str(caught.value) == (
            "Migration file '0002_b.py' defines migration '0001_a'; a migration's name must be its file name."
        )

    def test_an_error_of_the_file_propagates(self, tmp_path: Path) -> None:
        (tmp_path / "0001_a.py").write_text("raise LookupError('the file failed')\n", encoding="utf-8")
        with pytest.raises(LookupError, match="the file failed"):
            MigrationExecutor(tmp_path).load_migration("0001_a")
        (tmp_path / "0001_a.py").unlink()
        (tmp_path / "0002_b.py").write_text("migration = (\n", encoding="utf-8")
        with pytest.raises(SyntaxError):
            MigrationExecutor(tmp_path).load_migration("0002_b")

    @pytest.mark.parametrize("name", ["../outside", "0001_a/../../outside", "0001_a.py", "outside", "0001_a b", ""])
    def test_a_name_that_is_no_migration_name_is_refused_before_any_file_runs(self, tmp_path: Path, name: str) -> None:
        (tmp_path / "outside.py").write_text("raise RuntimeError('must not run')\n", encoding="utf-8")
        directory = tmp_path / "migrations"
        directory.mkdir()
        with pytest.raises(ValueError, match="^Invalid migration name"):
            MigrationExecutor(directory).load_migration(name)

    def test_a_name_of_another_type_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(TypeError, match="^Migration name must be a str, not int"):
            MigrationExecutor(tmp_path).load_migration(1)  # type: ignore[arg-type]

    def test_the_connection_is_checked_without_a_connection_call(self, tmp_path: Path) -> None:
        assert MigrationExecutor(tmp_path, connection="other").migrations_dir == tmp_path
        with pytest.raises(ValueError, match="Connection name must not be empty"):
            MigrationExecutor(tmp_path, connection="")
        with pytest.raises(TypeError, match="Connection name must be a str, not int"):
            MigrationExecutor(tmp_path, connection=1)  # type: ignore[arg-type]

        class _Client:
            async def query(self, text: str) -> None:
                """A client object: it has a ``query()``."""

        with pytest.raises(TypeError, match="takes the name of a registered connection, not a client object"):
            MigrationExecutor(tmp_path, connection=_Client())  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="positional argument"):
            MigrationExecutor(tmp_path, "other")  # type: ignore[misc]

    def test_the_module_exports(self) -> None:
        assert executor_module.__all__ == ["MigrationExecutor", "MigrationStatementError"]
        assert migrations.MigrationExecutor is executor_module.MigrationExecutor


class TestOrder:
    """Spec §5.6 "Loading and order": the topological order of the dependency graph, ties broken by name."""

    def test_the_dependency_order_ties_broken_by_name(self, tmp_path: Path) -> None:
        _migration_file(tmp_path, "0001_a", "0003_c")
        _migration_file(tmp_path, "0002_b")
        _migration_file(tmp_path, "0003_c")
        _migration_file(tmp_path, "0004_d", "0001_a", "0002_b")
        executor = MigrationExecutor(tmp_path)
        assert executor.get_available_migrations() == ["0001_a", "0002_b", "0003_c", "0004_d"]
        assert executor.get_pending_migrations([]) == ["0002_b", "0003_c", "0001_a", "0004_d"]
        assert executor.get_pending_migrations(["0003_c", "0002_b"]) == ["0001_a", "0004_d"]
        assert executor.get_pending_migrations(["0001_a", "0002_b", "0003_c", "0004_d"]) == []
        # Applied names without a file are not pending: they are the history checks' (TestHistory).
        assert executor.get_pending_migrations(["0009_z"]) == ["0002_b", "0003_c", "0001_a", "0004_d"]

    def test_a_dependency_listed_twice(self, tmp_path: Path) -> None:
        _migration_file(tmp_path, "0001_a")
        _migration_file(tmp_path, "0002_b", "0001_a", "0001_a")
        assert MigrationExecutor(tmp_path).get_pending_migrations([]) == ["0001_a", "0002_b"]

    def test_the_order_is_the_loader_s(self, tmp_path: Path) -> None:
        first = Migration(name="0002_b")
        second = Migration(name="0001_a", dependencies=["0002_b"])
        assert loader.order([second, first], tmp_path) == [first, second]
        assert loader.order([], tmp_path) == []

    def test_an_unknown_dependency(self, tmp_path: Path) -> None:
        _migration_file(tmp_path, "0001_a")
        _migration_file(tmp_path, "0002_b", "0001_a", "0001_x")
        with pytest.raises(ValueError) as caught:
            MigrationExecutor(tmp_path).get_pending_migrations([])
        assert str(caught.value) == f"Migration '0002_b' depends on '0001_x', which is not a migration file of {tmp_path}."

    def test_a_cycle(self, tmp_path: Path) -> None:
        _migration_file(tmp_path, "0001_a", "0003_c")
        _migration_file(tmp_path, "0002_b")
        _migration_file(tmp_path, "0003_c", "0001_a")
        with pytest.raises(ValueError) as caught:
            MigrationExecutor(tmp_path).get_pending_migrations([])
        assert str(caught.value) == "Circular dependency detected in migrations: ['0001_a', '0003_c']"

    def test_a_cycle_lists_what_could_not_be_ordered(self, tmp_path: Path) -> None:
        # The full ORM's text: the migrations left once nothing more can be ordered, by name.
        _migration_file(tmp_path, "0001_a", "0002_b")
        _migration_file(tmp_path, "0002_b", "0001_a")
        _migration_file(tmp_path, "0003_c", "0002_b")
        _migration_file(tmp_path, "0004_d")
        with pytest.raises(ValueError) as caught:
            MigrationExecutor(tmp_path).migration_state()
        assert str(caught.value) == "Circular dependency detected in migrations: ['0001_a', '0002_b', '0003_c']"

    def test_the_leaves(self) -> None:
        migrations_ = [
            Migration(name="0001_a"),
            Migration(name="0002_b", dependencies=["0001_a"]),
            Migration(name="0003_c", dependencies=["0001_a"]),
        ]
        assert loader.leaves(migrations_) == ["0002_b", "0003_c"]
        assert loader.leaves(migrations_[:1]) == ["0001_a"]
        assert loader.leaves([]) == []


#: Spec §5.6's table: each caller's history checks (D30 as amended, C10) — ``(dependency, unknown)``.
_CALLERS: dict[str, tuple[Literal["raise", "warn"], Literal["raise", "warn", "missing"]]] = {
    "makemigrations(from_db=True)": ("raise", "raise"),
    "migrate()": ("raise", "warn"),
    "upgrade()": ("raise", "missing"),
    "rollback()": ("raise", "missing"),
    "get_migration_status()": ("warn", "warn"),
}


def _history() -> list[Migration]:
    """``0001_a`` ← ``0002_b`` ← ``0003_c``, in their order."""
    return [
        Migration(name="0001_a"),
        Migration(name="0002_b", dependencies=["0001_a"]),
        Migration(name="0003_c", dependencies=["0002_b"]),
    ]


def _e_h1(directory: Path) -> str:
    return (
        f"Migration '0002_b' is applied but its dependency '0001_a' is not: the database's history does not match {directory}."
    )


def _e_h2(directory: Path) -> str:
    return f"Migration '0009_z' is applied but has no file in {directory}: the database's history does not match {directory}."


def _unknown_warning(directory: Path) -> str:
    return f"Migration 0009_z is recorded as applied but has no file in {directory}; it is ignored."


def _logged(caplog: pytest.LogCaptureFixture) -> list[tuple[str, int, str]]:
    return [(record.name, record.levelno, record.getMessage()) for record in caplog.records]


class TestHistory:
    """Spec §5.6's history checks and §7's E-H1/E-H2 (D30 as amended, C10): ``loader.check_history()`` in its modes,
    and each caller's modes (``loader.HISTORY``), so a caller cannot pick the wrong one silently."""

    @pytest.mark.parametrize("applied", [[], ["0001_a"], ["0001_a", "0002_b"], ["0002_b", "0001_a", "0003_c"]])
    def test_a_consistent_history(self, tmp_path: Path, caplog: pytest.LogCaptureFixture, applied: list[str]) -> None:
        caplog.set_level(logging.DEBUG)
        for dependency, unknown in _CALLERS.values():
            loader.check_history(_history(), applied, tmp_path, dependency=dependency, unknown=unknown)
        assert caplog.records == []

    def test_e_h1_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError) as caught:
            loader.check_history(_history(), ["0002_b"], tmp_path, dependency="raise", unknown="warn")
        assert str(caught.value) == _e_h1(tmp_path)

    def test_e_h1_warns_once(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG)
        order = [Migration(name="0001_a"), Migration(name="0002_b", dependencies=["0001_a", "0001_a"])]
        assert loader.check_history(order, ["0002_b"], tmp_path, dependency="warn", unknown="raise") is None
        assert _logged(caplog) == [("surreal_orm_lite.migrations", logging.WARNING, _e_h1(tmp_path))]

    def test_e_h1_names_the_first_in_the_order(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG)
        loader.check_history(_history(), ["0003_c", "0002_b"], tmp_path, dependency="warn", unknown="warn")
        assert [message for _, _, message in _logged(caplog)] == [_e_h1(tmp_path)]
        with pytest.raises(ValueError) as caught:
            loader.check_history(_history(), ["0003_c"], tmp_path, dependency="raise", unknown="warn")
        assert str(caught.value) == (
            f"Migration '0003_c' is applied but its dependency '0002_b' is not: the database's history does not match "
            f"{tmp_path}."
        )

    def test_an_unknown_name_raises_e_h2(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError) as caught:
            loader.check_history(_history(), ["0001_a", "0009_z"], tmp_path, dependency="raise", unknown="raise")
        assert str(caught.value) == _e_h2(tmp_path)

    def test_an_unknown_name_warns_once(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG)
        applied = ["0001_a", "0009_z", "0009_z"]
        assert loader.check_history(_history(), applied, tmp_path, dependency="raise", unknown="warn") is None
        assert _logged(caplog) == [("surreal_orm_lite.migrations", logging.WARNING, _unknown_warning(tmp_path))]

    def test_an_unknown_name_s_file_is_missing(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError) as caught:
            loader.check_history(_history(), ["0009_z"], tmp_path, dependency="raise", unknown="missing")
        assert str(caught.value) == _not_found(tmp_path, "0009_z")

    def test_e_h1_comes_first(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError) as caught:
            loader.check_history(_history(), ["0009_z", "0002_b"], tmp_path, dependency="raise", unknown="missing")
        assert str(caught.value) == _e_h1(tmp_path)

    def test_the_warnings_of_one_call(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG)
        for _ in range(2):  # once per call: a second call logs them again
            loader.check_history(_history(), ["0009_z", "0002_b"], tmp_path, dependency="warn", unknown="warn")
        assert [message for _, _, message in _logged(caplog)] == [_e_h1(tmp_path), _unknown_warning(tmp_path)] * 2

    def test_every_caller_has_a_mode(self) -> None:
        assert set(loader.HISTORY) == {"makemigrations", "migrate", "upgrade", "rollback", "get_migration_status"}

    @pytest.mark.parametrize(("caller", "expected"), list(_CALLERS.items()), ids=list(_CALLERS))
    def test_each_caller(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, caller: str, expected: tuple[str, str]
    ) -> None:
        caplog.set_level(logging.DEBUG)
        mode = loader.HISTORY[caller.split("(")[0]]
        assert (mode.dependency, mode.unknown) == expected
        order = _history()
        # E-H1: an applied migration depending on a pending one.
        if mode.dependency == "raise":
            with pytest.raises(ValueError) as raised:
                loader.check_history(order, ["0002_b"], tmp_path, dependency=mode.dependency, unknown=mode.unknown)
            assert str(raised.value) == _e_h1(tmp_path)
        else:
            loader.check_history(order, ["0002_b"], tmp_path, dependency=mode.dependency, unknown=mode.unknown)
            assert [message for _, _, message in _logged(caplog)] == [_e_h1(tmp_path)]
        caplog.clear()
        # An applied name without a file.
        unknown: type[BaseException] | None = {"raise": ValueError, "missing": FileNotFoundError, "warn": None}[mode.unknown]
        if unknown is None:
            loader.check_history(order, ["0001_a", "0009_z"], tmp_path, dependency=mode.dependency, unknown=mode.unknown)
            assert [message for _, _, message in _logged(caplog)] == [_unknown_warning(tmp_path)]
        else:
            with pytest.raises(unknown) as missing:
                loader.check_history(order, ["0001_a", "0009_z"], tmp_path, dependency=mode.dependency, unknown=mode.unknown)
            assert str(missing.value) == (_e_h2(tmp_path) if unknown is ValueError else _not_found(tmp_path, "0009_z"))


# --- the files' state and show_sql() (spec §5.5) --------------------------------------------------------------------


def _unmarked(value: SchemaState) -> bool:
    """Whether no definition of *value* carries a mark (spec §5.9: only ``ModelIntrospector`` sets them)."""
    for table in value.tables.values():
        if table.undeclared or any(field.undeclared or field.server for field in table.fields.values()):
            return False
    return True


class TestMigrationState:
    """Spec §5.5: ``migration_state(target=None)`` — the state the files build, by file replay (``strict=False``)."""

    def test_two_files(self, tmp_path: Path) -> None:
        generator = MigrationGenerator(tmp_path)
        generator.generate("initial", [CreateTable("person"), AddField("person", "name", "string")])
        index = CreateIndex(table="person", name="person_email", fields=["email"], unique=True)
        generator.generate("email", [AddField("person", "email", "string"), index], ["0001_initial"])
        executor = MigrationExecutor(tmp_path)
        everything = executor.migration_state()
        assert everything.applied_migrations == ("0001_initial", "0002_email")
        assert sorted(everything.tables) == ["person"]
        assert sorted(everything.tables["person"].fields) == ["email", "name"]
        assert list(everything.tables["person"].indexes) == ["person_email"]
        assert _unmarked(everything)
        loaded = [executor.load_migration(name) for name in ("0001_initial", "0002_email")]
        assert everything == state.replay(loaded, strict=False)
        assert executor.migration_state("0002_email") == everything
        first = executor.migration_state("0001_initial")
        assert first.applied_migrations == ("0001_initial",)
        assert sorted(first.tables["person"].fields) == ["name"]
        assert first.tables["person"].indexes == {}

    def test_the_target_stops_in_the_order(self, tmp_path: Path) -> None:
        _migration_file(tmp_path, "0001_a", "0002_b", operations="AddField('t', 'a', 'int')")
        _migration_file(tmp_path, "0002_b", operations="CreateTable('t')")
        _migration_file(tmp_path, "0003_c", operations="AddField('t', 'c', 'int')")
        executor = MigrationExecutor(tmp_path)
        assert executor.migration_state("0002_b").applied_migrations == ("0002_b",)
        assert executor.migration_state("0001_a").applied_migrations == ("0002_b", "0001_a")
        assert sorted(executor.migration_state("0001_a").tables["t"].fields) == ["a"]
        assert executor.migration_state().applied_migrations == ("0002_b", "0001_a", "0003_c")

    def test_an_unknown_target(self, tmp_path: Path) -> None:
        _migration_file(tmp_path, "0001_a")
        with pytest.raises(ValueError) as caught:
            MigrationExecutor(tmp_path).migration_state("0002_b")
        assert str(caught.value) == f"Unknown migration target '0002_b': no such migration file in {tmp_path}."

    def test_a_removal_of_a_definition_no_file_made(self, tmp_path: Path) -> None:
        # A file replay cannot know what was defined outside migrations: the removal is a no-op (C9).
        _migration_file(tmp_path, "0001_a", operations="CreateTable('person'), AddField('person', 'name', 'string')")
        _migration_file(tmp_path, "0002_b", "0001_a", operations="DropField('person', 'age'), DropTable('ghost')")
        result = MigrationExecutor(tmp_path).migration_state()
        assert result.applied_migrations == ("0001_a", "0002_b")
        assert sorted(result.tables) == ["person"]
        assert sorted(result.tables["person"].fields) == ["name"]

    def test_a_field_on_a_table_no_file_creates(self, tmp_path: Path) -> None:
        # SurrealDB creates the table TYPE ANY SCHEMALESS (M28).
        _migration_file(tmp_path, "0001_a", operations="AddField('log', 'msg', 'string')")
        table = MigrationExecutor(tmp_path).migration_state().tables["log"]
        assert (table.schema_mode, table.table_type) == ("SCHEMALESS", "any")
        assert sorted(table.fields) == ["msg"]

    def test_no_file(self, tmp_path: Path) -> None:
        assert MigrationExecutor(tmp_path).migration_state() == SchemaState()


#: A migration with a full-text index (R14) and an ``any`` column on a SCHEMAFULL table (R25).
_POSTS: list[Operation] = [
    CreateTable("post"),
    AddField("post", "body", "string"),
    AddField("post", "bag", "any"),
    CreateIndex(table="post", name="post_body", fields=["body"], search_analyzer="english", bm25=True, highlights=True),
]


class TestShowSql:
    """Spec §5.5: ``show_sql(name, *, backwards=False, line=3)`` — pure despite ``async``: the statements of
    ``forwards_sql()`` (``backwards_sql()``) from the files' state before the migration, joined by blank lines."""

    async def test_the_3x_form(self, tmp_path: Path) -> None:
        MigrationGenerator(tmp_path).generate("posts", _POSTS)
        assert await MigrationExecutor(tmp_path).show_sql("0001_posts") == (
            "DEFINE TABLE `post` SCHEMAFULL;\n\n"
            "DEFINE FIELD `body` ON `post` TYPE string;\n\n"
            "DEFINE FIELD `bag` ON `post` TYPE any;\n\n"
            "DEFINE INDEX `post_body` ON `post` FIELDS `body` FULLTEXT ANALYZER `english` BM25 HIGHLIGHTS;"
        )

    async def test_the_2x_form(self, tmp_path: Path) -> None:
        MigrationGenerator(tmp_path).generate("posts", _POSTS)
        assert await MigrationExecutor(tmp_path).show_sql("0001_posts", line=2) == (
            "DEFINE TABLE `post` SCHEMAFULL;\n\n"
            "DEFINE FIELD `body` ON `post` TYPE string;\n\n"
            "DEFINE FIELD `bag` ON `post` TYPE any FLEXIBLE;\n\n"
            "DEFINE INDEX `post_body` ON `post` FIELDS `body` SEARCH ANALYZER `english` BM25 HIGHLIGHTS;"
        )

    async def test_backwards(self, tmp_path: Path) -> None:
        MigrationGenerator(tmp_path).generate("posts", _POSTS)
        expected = (
            "REMOVE INDEX IF EXISTS `post_body` ON `post`;\n\n"
            "REMOVE FIELD IF EXISTS `bag` ON `post`;\n\n"
            "REMOVE FIELD IF EXISTS `body` ON `post`;\n\n"
            "REMOVE TABLE IF EXISTS `post`;"
        )
        executor = MigrationExecutor(tmp_path)
        assert await executor.show_sql("0001_posts", backwards=True) == expected
        assert await executor.show_sql("0001_posts", backwards=True, line=2) == expected

    async def test_the_state_before_is_the_files(self, tmp_path: Path) -> None:
        # R33: the AlterTable making `t` SCHEMAFULL redefines its `any` columns FLEXIBLE on line 2 — those the files
        # hold before it —, not the one the migration adds itself (skip_fields, AC-2).
        generator = MigrationGenerator(tmp_path)
        generator.generate(
            "t",
            [
                CreateTable(name="t", schema_mode="SCHEMALESS", table_type="normal"),
                AddField("t", "name", "string"),
                AddField("t", "bag", "any"),
                AddField("t", "tags", "array"),
                AddField("t", "tags.*", "any"),
            ],
        )
        generator.generate("strict", [R33, AddField("t", "bag2", "any")], ["0001_t"])
        executor = MigrationExecutor(tmp_path)
        assert await executor.show_sql("0002_strict", line=2) == (
            "\n".join(_R33_LINES) + "\n\nDEFINE FIELD `bag2` ON `t` TYPE any FLEXIBLE;"
        )
        assert await executor.show_sql("0002_strict") == _R33_LINES[0] + "\n\nDEFINE FIELD `bag2` ON `t` TYPE any;"
        before = executor.migration_state("0001_t")
        migration = executor.load_migration("0002_strict")
        assert await executor.show_sql("0002_strict", line=2) == "\n\n".join(migration.forwards_sql(line=2, state=before))
        assert await executor.show_sql("0002_strict", backwards=True, line=2) == "\n\n".join(
            migration.backwards_sql(line=2, state=before)
        )

    async def test_an_empty_migration(self, tmp_path: Path) -> None:
        generate_empty_migration(tmp_path, "nothing")
        assert await MigrationExecutor(tmp_path).show_sql("0001_nothing") == ""

    async def test_an_unknown_name(self, tmp_path: Path) -> None:
        MigrationGenerator(tmp_path).generate("posts", _POSTS)
        with pytest.raises(FileNotFoundError) as caught:
            await MigrationExecutor(tmp_path).show_sql("0002_other")
        assert str(caught.value) == _not_found(tmp_path, "0002_other")
        with pytest.raises(ValueError, match="^Invalid migration name"):
            await MigrationExecutor(tmp_path).show_sql("../posts")

    async def test_the_line_is_checked_first(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="line= must be 2 or 3"):
            await MigrationExecutor(tmp_path).show_sql("0001_posts", line=4)  # type: ignore[arg-type]


# --- makemigrations(from_db=False) (spec §5.10, AC-5, AC-6, AC-30) ---------------------------------------------------

_MODULES = itertools.count()

#: The imports of the models each test defines in a module of its own.
_MODELS_HEADER = """
from typing import Any

from surreal_orm_lite import (
    Analyzer,
    AuthenticatedUserMixin,
    BaseSurrealEdge,
    BaseSurrealModel,
    Event,
    Index,
    SchemaMode,
    SurrealConfigDict,
    TableType,
)
"""


@pytest.fixture
def define_models(monkeypatch: pytest.MonkeyPatch) -> Any:
    """``define_models(source)``: runs *source* in a module created for the test (registered in ``sys.modules`` while
    the test runs) and returns the model classes it defines, in their order — models redefined with the same table
    names as an earlier call's."""

    def define(source: str) -> list[type[BaseSurrealModel]]:
        module = types.ModuleType(f"mig_files_models_{next(_MODULES)}")
        monkeypatch.setitem(sys.modules, module.__name__, module)
        exec(textwrap.dedent(_MODELS_HEADER) + textwrap.dedent(source), module.__dict__)
        return [
            value
            for value in vars(module).values()
            if isinstance(value, type) and issubclass(value, BaseSurrealModel) and value.__module__ == module.__name__
        ]

    return define


def _node(config: str = "", fields: str = "age: int = 0") -> str:
    """The source of a SCHEMAFULL node model ``M`` of table ``mk_t``: *config* appended to its ``SurrealConfigDict``,
    *fields* its fields separated by ``;``."""
    lines = "".join(f"    {line}\n" for line in fields.split(";"))
    return (
        "class M(BaseSurrealModel):\n"
        f'    model_config = SurrealConfigDict(table_name="mk_t", schema_mode=SchemaMode.SCHEMAFULL{config})\n'
        f"    id: str | None = None\n{lines}"
    )


def _view(query: str) -> str:
    return (
        "class V(BaseSurrealModel):\n"
        f'    model_config = SurrealConfigDict(table_name="mk_v", view_query="{query}")\n'
        "    id: str | None = None\n"
        "    n: int = 0\n"
    )


def _edge(out: str) -> str:
    return (
        "class E(BaseSurrealEdge):\n"
        "    model_config = SurrealConfigDict(\n"
        f'        table_name="mk_e", table_type=TableType.RELATION, relation_in="mk_t", relation_out={out}\n'
        "    )\n"
        "    weight: int = 1\n"
    )


def _user(session: str) -> str:
    return (
        "class U(AuthenticatedUserMixin, BaseSurrealModel):\n"
        "    model_config = SurrealConfigDict(\n"
        f'        table_name="mk_user", access_name="mk_account", auth_duration_session="{session}"\n'
        "    )\n"
        "    email: str\n"
        "    password: str\n"
    )


def _analyzer(filters: str, highlights: str) -> str:
    indexes = (
        ', indexes=[Index("mk_t_a", "a", search_analyzer=EN, bm25=True), '
        f'Index("mk_t_b", "b", search_analyzer=EN, bm25=True{highlights})]'
    )
    return f'EN = Analyzer("mk_en", ["blank"], {filters})\n' + _node(indexes, "a: str = '';b: str = ''")


_INDEX = ', indexes=[Index("mk_t_age", "age")]'
_EVENT = ', events=[Event("mk_audit", then="CREATE mk_log SET a = 1")]'
_COUNT = "SELECT count() AS n FROM mk_t GROUP ALL"

#: AC-6: a model change of each kind — the models before, after, and the operations ``0002`` holds (``describe()``),
#: in spec §5.7's order. A removal is written with ``drop_missing=True`` only (``TestMakemigrationsOffline``).
_CHANGES: dict[str, tuple[str, str, list[str]]] = {
    "a field added": (_node(), _node(fields="age: int = 0;nick: str = ''"), ["Add field nick to mk_t"]),
    "a field retyped": (_node(), _node(fields="age: float = 0.0"), ["Alter field age on mk_t"]),
    "a field made optional": (_node(fields="nick: str"), _node(fields="nick: str | None = None"), ["Alter field nick on mk_t"]),
    "a field given a default": (_node(fields="nick: str"), _node(fields="nick: str = 'x'"), ["Alter field nick on mk_t"]),
    "a field removed": (_node(fields="age: int = 0;nick: str"), _node(), ["Drop field nick from mk_t"]),
    "the schema mode": (_node(), _node().replace("SCHEMAFULL", "SCHEMALESS"), ["Alter table mk_t"]),
    "the comment": (_node(), _node(', comment="People"'), ["Alter table mk_t"]),
    "the change feed": (_node(), _node(', changefeed="1d"'), ["Alter table mk_t"]),
    "the permissions": (_node(), _node(', permissions={"select": "FULL"}'), ["Alter table mk_t"]),
    "an index added": (_node(), _node(_INDEX), ["Create index mk_t_age on mk_t"]),
    "an index changed": (
        _node(_INDEX),
        _node(_INDEX.replace('"age")', '"age", unique=True)')),
        ["Create index mk_t_age on mk_t"],
    ),
    "an index removed": (_node(_INDEX), _node(), ["Drop index mk_t_age from mk_t"]),
    "an analyzer changed": (
        _analyzer('["lowercase"]', ""),
        _analyzer('["lowercase", "ascii"]', ", highlights=True"),
        ["Define analyzer mk_en", "Create index mk_t_b on mk_t", "Rebuild index mk_t_a on mk_t"],
    ),
    "an event added": (_node(), _node(_EVENT), ["Define event mk_audit on mk_t"]),
    "an event changed": (_node(_EVENT), _node(_EVENT.replace("a = 1", "a = 2")), ["Define event mk_audit on mk_t"]),
    "an event removed": (_node(_EVENT), _node(), ["Remove event mk_audit from mk_t"]),
    "a view's query": (
        _node() + _view(_COUNT),
        _node() + _view(_COUNT.replace(" GROUP", " WHERE age > 1 GROUP")),
        ["Alter table mk_v"],
    ),
    "a relation's ends": (_node() + _edge('"mk_t"'), _node() + _edge('["mk_t", "mk_u"]'), ["Alter table mk_e"]),
    "a user model's durations": (_user("1h"), _user("2h"), ["Define access mk_account for mk_user"]),
}

#: The models of the flows below: a node holding a default with a ``;`` (AC-30).
_PERSON = """
class P(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="mk_person", schema_mode=SchemaMode.SCHEMAFULL)
    id: str | None = None
    name: str
    sep: str = "a;b"
"""
_OTHER = """
class O(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="mk_other")
    id: str | None = None
    label: str = ""
"""

_LOGGER = "surreal_orm_lite.migrations"


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == _LOGGER]


class TestMakemigrationsOffline:
    """Spec §5.10 with ``from_db=False``: the files' state (``migration_state()``) diffed against the models, removals
    only with ``drop_missing=True``; nothing written when nothing changed. Every call passes ``from_db=False`` (no
    server), but ``empty=True``'s, which returns before reading any state."""

    async def test_twice_in_a_row(self, tmp_path: Path, caplog: pytest.LogCaptureFixture, define_models: Any) -> None:
        # AC-5: the second call writes nothing.
        caplog.set_level(logging.INFO, logger=_LOGGER)
        models = define_models(_PERSON)
        path = await makemigrations("initial", migrations_dir=tmp_path, models=models, from_db=False)
        assert path == tmp_path / "0001_initial.py"
        files = sorted(path.name for path in tmp_path.iterdir() if path.is_file())
        assert files == ["0001_initial.py", "__init__.py"]
        migration = MigrationExecutor(tmp_path).load_migration("0001_initial")
        assert migration.operations == tuple(SchemaState().diff(introspect_models(models)))
        assert migration.dependencies == ()
        caplog.clear()
        assert await makemigrations("again", migrations_dir=tmp_path, models=models, from_db=False) is None
        assert _messages(caplog) == ["No changes detected."]
        assert sorted(path.name for path in tmp_path.iterdir() if path.is_file()) == files

    async def test_a_default_holding_a_semicolon(self, tmp_path: Path, define_models: Any) -> None:
        # AC-30's offline part: the default is data, written as such; the file loads; nothing to write after it.
        models = define_models(_PERSON)
        path = await makemigrations("initial", migrations_dir=tmp_path, models=models, from_db=False)
        assert path is not None
        assert "AddField(table='mk_person', name='sep', field_type='string', default='a;b')," in path.read_text(
            encoding="utf-8"
        )
        operations = MigrationExecutor(tmp_path).load_migration("0001_initial").operations
        assert AddField(table="mk_person", name="sep", field_type="string", default="a;b") in operations
        assert await makemigrations("again", migrations_dir=tmp_path, models=models, from_db=False) is None

    @pytest.mark.parametrize(("before", "after", "expected"), list(_CHANGES.values()), ids=list(_CHANGES))
    async def test_each_model_change(
        self, tmp_path: Path, define_models: Any, before: str, after: str, expected: list[str]
    ) -> None:
        # AC-6: models redefined with the same table names.
        assert await makemigrations("initial", migrations_dir=tmp_path, models=define_models(before), from_db=False)
        changed = define_models(after)
        path = await makemigrations("change", migrations_dir=tmp_path, models=changed, from_db=False, drop_missing=True)
        assert path == tmp_path / "0002_change.py"
        migration = MigrationExecutor(tmp_path).load_migration("0002_change")
        assert migration.dependencies == ("0001_initial",)
        assert [operation.describe() for operation in migration.operations] == expected
        current = MigrationExecutor(tmp_path).migration_state("0001_initial")
        assert list(migration.operations) == current.diff(introspect_models(changed))
        again = await makemigrations("again", migrations_dir=tmp_path, models=changed, from_db=False, drop_missing=True)
        assert again is None

    async def test_a_change_keeps_the_previous_definitions(self, tmp_path: Path, define_models: Any) -> None:
        # The redefinitions carry the files' definition as previous= (reversible), read back from the files.
        await makemigrations("initial", migrations_dir=tmp_path, models=define_models(_node(_EVENT)), from_db=False)
        changed = define_models(_node(_EVENT.replace("a = 1", "a = 2")))
        await makemigrations("change", migrations_dir=tmp_path, models=changed, from_db=False)
        (event,) = MigrationExecutor(tmp_path).load_migration("0002_change").operations
        assert isinstance(event, DefineEvent)
        assert isinstance(event.previous, DefineEvent)
        assert event.previous.then == "CREATE mk_log SET a = 1"
        assert event.reversible

    @pytest.mark.parametrize(
        ("before", "after", "skipped"),
        [
            (_node(fields="age: int = 0;nick: str"), _node(), "Drop field nick from mk_t"),
            (_node(_EVENT), _node(), "Remove event mk_audit from mk_t"),
            (_node(_INDEX), _node(), "Drop index mk_t_age from mk_t"),
        ],
        ids=["a field", "an event", "an index"],
    )
    async def test_a_removal_is_left_out_by_default(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, define_models: Any, before: str, after: str, skipped: str
    ) -> None:
        caplog.set_level(logging.INFO, logger=_LOGGER)
        await makemigrations("initial", migrations_dir=tmp_path, models=define_models(before), from_db=False)
        changed = define_models(after)
        caplog.clear()
        assert await makemigrations("change", migrations_dir=tmp_path, models=changed, from_db=False) is None
        assert _messages(caplog) == [
            f"Skipped 1 removal(s) with no model to justify them: {skipped}; pass drop_missing=True to write them.",
            "No changes detected.",
        ]
        assert not (tmp_path / "0002_change.py").exists()

    async def test_the_removals_left_out_beside_a_change(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, define_models: Any
    ) -> None:
        caplog.set_level(logging.INFO, logger=_LOGGER)
        await makemigrations("initial", migrations_dir=tmp_path, models=define_models(_node(_EVENT + _INDEX)), from_db=False)
        changed = define_models(_node(', comment="People"', fields="nick: str = ''"))
        caplog.clear()
        path = await makemigrations("change", migrations_dir=tmp_path, models=changed, from_db=False)
        assert path is not None
        operations = MigrationExecutor(tmp_path).load_migration("0002_change").operations
        assert [operation.describe() for operation in operations] == ["Alter table mk_t", "Add field nick to mk_t"]
        assert _messages(caplog) == [
            "Skipped 3 removal(s) with no model to justify them: Drop field age from mk_t, Drop index mk_t_age from mk_t, "
            "Remove event mk_audit from mk_t; pass drop_missing=True to write them."
        ]

    async def test_a_removal_with_drop_missing_carries_its_previous(self, tmp_path: Path, define_models: Any) -> None:
        await makemigrations("initial", migrations_dir=tmp_path, models=define_models(_node(_EVENT)), from_db=False)
        path = await makemigrations(
            "change", migrations_dir=tmp_path, models=define_models(_node()), from_db=False, drop_missing=True
        )
        assert path is not None
        (removal,) = MigrationExecutor(tmp_path).load_migration("0002_change").operations
        assert isinstance(removal, RemoveEvent)
        assert isinstance(removal.previous, DefineEvent)
        assert removal.previous.then == "CREATE mk_log SET a = 1"

    async def test_a_model_deleted_since_the_first_migration(self, tmp_path: Path, define_models: Any) -> None:
        both = define_models(_PERSON + _OTHER)
        await makemigrations("initial", migrations_dir=tmp_path, models=both, from_db=False)
        assert await makemigrations("change", migrations_dir=tmp_path, models=both[:1], from_db=False) is None
        path = await makemigrations("change", migrations_dir=tmp_path, models=both[:1], from_db=False, drop_missing=True)
        assert path == tmp_path / "0002_change.py"
        assert MigrationExecutor(tmp_path).load_migration("0002_change").operations == (DropTable("mk_other"),)

    async def test_the_dependencies_are_every_leaf(self, tmp_path: Path, define_models: Any) -> None:
        models = define_models(_PERSON)
        await makemigrations("initial", migrations_dir=tmp_path, models=models, from_db=False)
        # A hand-written branch: two migrations after 0001_initial.
        _migration_file(tmp_path, "0003_b", "0001_initial", operations="AddField('mk_person', 'b', 'int')")
        _migration_file(tmp_path, "0002_a", "0001_initial", operations="AddField('mk_person', 'a', 'int')")
        path = await makemigrations("merge", migrations_dir=tmp_path, models=models, from_db=False, drop_missing=True)
        assert path == tmp_path / "0004_merge.py"
        migration = MigrationExecutor(tmp_path).load_migration("0004_merge")
        assert migration.dependencies == ("0002_a", "0003_b")
        # The diff's order: the extra fields children before parents (reverse name order).
        assert migration.operations == (DropField("mk_person", "b"), DropField("mk_person", "a"))

    async def test_empty(self, tmp_path: Path) -> None:
        # No model, no request — with the default from_db=True too.
        path = await makemigrations("first", migrations_dir=tmp_path, empty=True)
        assert path == tmp_path / "0001_first.py"
        _migration_file(tmp_path, "0002_a", "0001_first")
        _migration_file(tmp_path, "0003_b", "0001_first")
        path = await makemigrations("data", migrations_dir=tmp_path, empty=True, models=[])
        assert path == tmp_path / "0004_data.py"
        migration = MigrationExecutor(tmp_path).load_migration("0004_data")
        assert (migration.dependencies, migration.operations) == (("0002_a", "0003_b"), ())

    async def test_empty_checks_the_files(self, tmp_path: Path) -> None:
        _migration_file(tmp_path, "0001_a", "0000_missing")
        with pytest.raises(ValueError, match="depends on '0000_missing', which is not a migration file"):
            await makemigrations("first", migrations_dir=tmp_path, empty=True)
        assert not (tmp_path / "0002_first.py").exists()

    async def test_no_models(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError) as caught:
            await makemigrations("initial", migrations_dir=tmp_path, models=[], from_db=False)
        assert str(caught.value) == "No models found: pass models=, or import the modules that define them."
        assert not tmp_path.joinpath("0001_initial.py").exists()

    async def test_the_models_of_the_connection(self, tmp_path: Path) -> None:
        # MiOther is of the connection "other": left out of the current connection's ("default") models.
        path = await makemigrations("initial", migrations_dir=tmp_path, models=[MiBare, MiOther], from_db=False)
        assert path is not None
        tables = {
            operation.name
            for operation in MigrationExecutor(tmp_path).load_migration("0001_initial").operations
            if isinstance(operation, CreateTable)
        }
        assert tables == {"mi_bare"}
        with pytest.raises(ValueError, match="^No models found"):
            await makemigrations("other", migrations_dir=tmp_path / "x", models=[MiBare], connection="other", from_db=False)
        path = await makemigrations(
            "other", migrations_dir=tmp_path / "x", models=[MiBare, MiOther], connection="other", from_db=False
        )
        assert path is not None
        operations = MigrationExecutor(tmp_path / "x").load_migration("0001_other").operations
        assert {operation.name for operation in operations if isinstance(operation, CreateTable)} == {"mi_other"}

    async def test_the_files_are_checked_before_the_diff(self, tmp_path: Path, define_models: Any) -> None:
        _migration_file(tmp_path, "0001_a", "0002_b")
        _migration_file(tmp_path, "0002_b", "0001_a")
        with pytest.raises(ValueError, match="^Circular dependency detected in migrations"):
            await makemigrations("initial", migrations_dir=tmp_path, models=define_models(_PERSON), from_db=False)

    async def test_the_arguments_are_checked(self, tmp_path: Path, define_models: Any) -> None:
        models = define_models(_PERSON)
        with pytest.raises(TypeError, match=r"^makemigrations\(\): name must be a str, not int"):
            await makemigrations(1, migrations_dir=tmp_path, models=models, from_db=False)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="Connection name must not be empty"):
            await makemigrations("x", migrations_dir=tmp_path, models=models, connection="", from_db=False)
        with pytest.raises(TypeError, match="takes an iterable of model classes, not a str"):
            await makemigrations("x", migrations_dir=tmp_path, models="P", from_db=False)  # type: ignore[arg-type]
        assert not tmp_path.joinpath("0001_x.py").exists()

    def test_the_signature(self) -> None:
        signature = inspect.signature(makemigrations)
        assert list(signature.parameters) == [
            "name",
            "migrations_dir",
            "models",
            "connection",
            "empty",
            "from_db",
            "drop_missing",
        ]
        defaults = {name: parameter.default for name, parameter in signature.parameters.items()}
        assert defaults["migrations_dir"] == "migrations"
        assert (defaults["models"], defaults["connection"]) == (None, None)
        assert (defaults["empty"], defaults["from_db"], defaults["drop_missing"]) == (False, True, False)
        kinds = {parameter.kind for name, parameter in signature.parameters.items() if name != "name"}
        assert kinds == {inspect.Parameter.KEYWORD_ONLY}
