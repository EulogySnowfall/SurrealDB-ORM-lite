"""Schema migrations for SurrealDB (v0.38.0), the full SurrealDB-ORM's ``migrations`` package on the official SDK.

A migration is a Python file of a migrations directory defining a :class:`Migration`: a name, the migrations it
depends on and its operations. :class:`MigrationStatementError` reports a statement SurrealDB refused, without
quoting it. The executor records the applied migrations in the table :data:`MIGRATIONS_TABLE`.
"""

from .constants import MIGRATIONS_TABLE
from .executor import MigrationStatementError
from .migration import Migration
from .operations import Operation

__all__ = [
    "MIGRATIONS_TABLE",
    "Migration",
    "MigrationStatementError",
    "Operation",
]
