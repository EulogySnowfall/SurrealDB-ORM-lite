"""The operations of a migration (the full SurrealDB-ORM's ``migrations.operations``).

An operation is one schema change: ``forwards()`` renders the SurrealQL that applies it, ``backwards()`` the
SurrealQL that undoes it, and ``state_forwards()``/``state_backwards()`` give the schema state after either, without a
database. Every concrete operation is a frozen dataclass deriving from :class:`Operation`.
"""

from abc import ABC, abstractmethod
from typing import Literal

from ..schema_state import SchemaState


class Operation(ABC):
    """One schema change of a :class:`~surreal_orm_lite.migrations.Migration` (the full ORM's base class).

    ``forwards()`` with no argument renders the SurrealDB 3.x form; ``line=2`` the form for SurrealDB 2.x and the
    embedded engine. *state* is the schema the statement is rendered for, read by the rules that depend on the
    table's definition (``FLEXIBLE`` before SurrealDB 3.x); without it, those rules write nothing.

    ``reversible`` is read-only: ``backwards()`` of an irreversible operation returns ``""``. ``destructive`` is true
    for a removal (``Drop*``/``Remove*``), whether or not it can be undone. ``describe()`` names what the operation
    changes, never its SurrealQL text.
    """

    @property
    @abstractmethod
    def reversible(self) -> bool:
        """Whether ``backwards()`` undoes the operation."""

    @property
    def destructive(self) -> bool:
        """Whether the operation removes a definition (a removal overrides this)."""
        return False

    @abstractmethod
    def forwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        """The SurrealQL statement(s) that apply the operation."""

    @abstractmethod
    def backwards(self, *, line: Literal[2, 3] = 3, state: SchemaState | None = None) -> str:
        """The SurrealQL statement(s) that undo the operation; ``""`` when it is not reversible."""

    @abstractmethod
    def describe(self) -> str:
        """A one-line description naming what the operation changes (names only, never SurrealQL text)."""

    @abstractmethod
    def state_forwards(self, state: SchemaState) -> SchemaState:
        """The schema state after ``forwards()`` was applied to *state* (pure)."""

    @abstractmethod
    def state_backwards(self, state: SchemaState) -> SchemaState:
        """The schema state after ``backwards()`` was applied to *state* (pure)."""
