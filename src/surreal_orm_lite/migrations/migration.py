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
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .operations import Operation

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
    operations: Sequence["Operation"] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        from .operations import Operation

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

    @property
    def is_reversible(self) -> bool:
        """Whether every operation is reversible, so that the whole migration can be rolled back."""
        return all(operation.reversible for operation in self.operations)

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
