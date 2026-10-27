"""Migration system (v0.38.0): the ``Migration`` class, migration names and ``MigrationStatementError``.

``Migration`` is the full SurrealDB-ORM's migration-file object, frozen: its dependencies and operations are kept as
tuples and checked at construction. ``parse_migration_name()`` and ``generate_migration_name()`` are the full ORM's
helpers. ``MigrationStatementError`` is caught by ``except SurrealDbError`` (the lite's family) and by
``except RuntimeError`` (the full ORM's base), and survives ``pickle`` with its keyword-only attributes. Unit tests only
(no server).
"""

import dataclasses
import pickle
from datetime import UTC, datetime, timedelta

import pytest

import surreal_orm_lite.migrations as migrations
from surreal_orm_lite.exceptions import SurrealDbError
from surreal_orm_lite.migrations import MIGRATIONS_TABLE, Migration, MigrationStatementError, Operation
from surreal_orm_lite.migrations.constants import MIGRATIONS_TABLE as CONSTANTS_TABLE
from surreal_orm_lite.migrations.executor import MigrationStatementError as ExecutorError
from surreal_orm_lite.migrations.migration import generate_migration_name, parse_migration_name


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
    assert set(migrations.__all__) == {"MIGRATIONS_TABLE", "Migration", "MigrationStatementError", "Operation"}
    for name in migrations.__all__:
        assert getattr(migrations, name) is not None
