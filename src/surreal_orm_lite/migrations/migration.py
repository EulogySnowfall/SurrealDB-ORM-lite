"""The ``Migration`` class and the migration names (the full SurrealDB-ORM's ``migrations.migration``).

A migration is a named, ordered list of operations, with the names of the migrations it depends on. Each one lives
in its own file of the migrations directory, ``<NNNN>_<name>.py``, which defines a module-level ``migration``::

    from surreal_orm_lite.migrations import Migration
    from surreal_orm_lite.migrations.operations import AddField

    migration = Migration(
        name="0002_add_email",
        dependencies=["0001_initial"],
        operations=[AddField(table="person", name="email", field_type="string")],
    )
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from ..schema_state import SchemaState
from ..search import checked_line
from .operations import AddField, AlterField, AlterTable, DataMigration, Operation, RawSQL

#: A migration's name: a number, an underscore, then ASCII letters, digits or underscores (``0001_initial``).
_NAME = re.compile(r"[0-9]+_[A-Za-z0-9_]+")


def _as_tuple(value: Any, *, migration: str, argument: str, items: str) -> tuple[Any, ...]:
    """*value*, a list or tuple, as a tuple; a ``str`` (a sequence of characters, almost always a forgotten list) and
    any other type are refused."""
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        raise TypeError(f"Migration {migration!r}: {argument} must be a list or tuple of {items}, not {type(value).__name__}.")
    return tuple(value)


@dataclass(frozen=True)
class Migration:
    """One migration: a name, the migrations it depends on and its operations (the full ORM's class, frozen).

    *dependencies* and *operations* accept any list or tuple and are kept as tuples; *created_at* defaults to the
    current UTC time. Construction checks the names and the operations (``ValueError``/``TypeError``) and does
    nothing else.

    Attributes:
        name: ``<number>_<name>``, the stem of its file (``"0001_initial"``).
        dependencies: The names of the migrations applied before this one.
        operations: The operations, applied in order.
        created_at: When the migration was created.
    """

    name: str
    dependencies: Sequence[str] = ()
    operations: Sequence[Operation] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        name = self.name
        if not isinstance(name, str):
            raise TypeError(f"Migration name must be a str, not {type(name).__name__}.")
        if _NAME.fullmatch(name) is None:
            raise ValueError(
                f"Invalid migration name {name!r}: expected digits, an underscore, then letters, digits or "
                "underscores, such as '0001_initial'."
            )
        dependencies = _as_tuple(self.dependencies, migration=name, argument="dependencies", items="migration names")
        for position, dependency in enumerate(dependencies, 1):
            if not isinstance(dependency, str):
                raise TypeError(f"Migration {name!r}: dependency {position} must be a str, not {type(dependency).__name__}.")
            if _NAME.fullmatch(dependency) is None:
                raise ValueError(
                    f"Migration {name!r}: invalid dependency name {dependency!r} (expected a migration name such as "
                    "'0001_initial')."
                )
            if dependency == name:
                raise ValueError(f"Migration {name!r} cannot depend on itself.")
        operations = _as_tuple(self.operations, migration=name, argument="operations", items="operations")
        for position, operation in enumerate(operations, 1):
            if not isinstance(operation, Operation):
                raise TypeError(
                    f"Migration {name!r}: operation {position} is not an Operation (got {type(operation).__name__})."
                )
        object.__setattr__(self, "dependencies", dependencies)
        object.__setattr__(self, "operations", operations)

    def forwards_sql(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> list[str]:
        """Each operation's ``forwards()`` that is not empty, in order — the statements that apply the migration on the
        SurrealDB major *line* (3 by default; 2 for SurrealDB 2.x and the embedded engine). Pure: no database is read.

        The operations render from the state after the migration: the **file replay** of its operations over *state*
        (``SchemaState()`` when ``None``) — so on line 2 the ``FLEXIBLE`` choices are those of the files' state, where
        ``migrate()`` writes those of the database's. An ``AlterTable`` leaves out of its ``FLEXIBLE`` redefinitions
        the fields the migration defines itself (``skip_fields``).
        """
        checked = checked_line(line)
        after = SchemaState() if state is None else state
        for operation in self.operations:
            after = operation.state_forwards(after)
        return [text for _, text in _statements(self, line=checked, state=after)]

    def backwards_sql(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> list[str]:
        """The reversible operations' ``backwards()`` that is not empty, in reverse order — the statements that undo
        the migration on the SurrealDB major *line*. *state* is the state **before** the migration (the one the
        backwards restore, read by the ``FLEXIBLE`` rule on line 2). Pure.
        """
        return [text for _, text in _statements(self, line=checked_line(line), state=state, backwards=True)]

    @property
    def is_reversible(self) -> bool:
        """Whether every operation is reversible, so that the whole migration can be rolled back."""
        return all(operation.reversible for operation in self.operations)

    @property
    def has_data_migrations(self) -> bool:
        """Whether the migration holds a ``DataMigration`` (records transformed, not schema)."""
        return any(isinstance(operation, DataMigration) for operation in self.operations)

    @property
    def schema_operations(self) -> list[Operation]:
        """Every operation but the ``DataMigration`` ones, in order."""
        return [operation for operation in self.operations if not isinstance(operation, DataMigration)]

    @property
    def data_operations(self) -> list[Operation]:
        """The ``DataMigration`` operations, in order."""
        return [operation for operation in self.operations if isinstance(operation, DataMigration)]

    def describe(self) -> str:
        """A multi-line description: the name, the dependencies and each operation's ``describe()``."""
        lines = [f"Migration: {self.name}"]
        if self.dependencies:
            lines.append(f"Dependencies: {', '.join(self.dependencies)}")
        lines.append(f"Operations ({len(self.operations)}):")
        for position, operation in enumerate(self.operations, 1):
            lines.append(f"  {position}. {operation.describe()}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"Migration(name={self.name!r}, operations={len(self.operations)})"


def _own_fields(operations: Iterable[Operation], table: str) -> set[str]:
    """The fields of *table* that *operations* define or redefine — their ``AddField`` and ``AlterField`` operations,
    those in a ``RawSQL``'s ``state_operations`` included (D29)."""
    names: set[str] = set()
    for operation in operations:
        if isinstance(operation, AddField | AlterField) and operation.table == table:
            names.add(operation.name)
        elif isinstance(operation, RawSQL):
            names |= _own_fields(operation.state_operations, table)
    return names


def _statements(
    migration: Migration, *, line: Literal[2, 3], state: SchemaState | None, backwards: bool = False
) -> list[tuple[int, str]]:
    """Each operation's index in ``migration.operations`` (0-based) and its non-empty text — ``forwards(line=…,
    state=…)`` in order, or ``backwards(…)`` in reverse order — for ``forwards_sql()``, ``backwards_sql()``,
    ``show_sql()`` and the executor's batches (spec §6.3). *state* is passed as it is: the state after the migration
    for the forwards, before it for the backwards. An ``AlterTable`` is given ``skip_fields``: the fields of its table
    the migration defines or redefines itself, which its line-2 ``FLEXIBLE`` redefinitions leave out (D29). Pure.
    """
    indexed = list(enumerate(migration.operations))
    if backwards:
        indexed.reverse()
    statements: list[tuple[int, str]] = []
    for index, operation in indexed:
        if isinstance(operation, AlterTable):
            skip = _own_fields(migration.operations, operation.name)
            if backwards:
                text = operation.backwards(line=line, state=state, skip_fields=skip)
            else:
                text = operation.forwards(line=line, state=state, skip_fields=skip)
        elif backwards:
            text = operation.backwards(line=line, state=state)
        else:
            text = operation.forwards(line=line, state=state)
        if text:
            statements.append((index, text))
    return statements


def parse_migration_name(filename: str) -> tuple[int, str]:
    """Split a migration file name into its number and its name: ``"0001_initial.py"`` → ``(1, "initial")``.

    Raises:
        ValueError: The name has no ``_``, or what precedes the first one is not a number.
    """
    if filename.endswith(".py"):
        filename = filename[:-3]
    parts = filename.split("_", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid migration filename: {filename}")
    try:
        number = int(parts[0])
    except ValueError:
        raise ValueError(f"Invalid migration number in: {filename}") from None
    return number, parts[1]


def generate_migration_name(number: int, name: str) -> str:
    """The migration name for *number* and a free-text *name*: ``(2, "Add Email")`` → ``"0002_add_email"``.

    The full ORM's rule: lower case, spaces and ``-`` become ``_``, every other character that is not an ASCII letter,
    digit or ``_`` is dropped, and the number is written on at least four digits. The result is always a name
    :class:`Migration` accepts.

    Raises:
        ValueError: Nothing of *name* is left but underscores.
    """
    safe_name = name.lower().replace(" ", "_").replace("-", "_")
    safe_name = "".join(
        character for character in safe_name if character.isascii() and (character.isalnum() or character == "_")
    )
    if not any(character.isalnum() for character in safe_name):
        raise ValueError(f"A migration name needs at least one letter or digit: {name!r}")
    return f"{number:04d}_{safe_name}"
