"""Schema migrations for SurrealDB (v0.38.0), the full SurrealDB-ORM's ``migrations`` package on the official SDK.

A migration is a Python file of a migrations directory defining a :class:`Migration`: a name, the migrations it
depends on and its operations — :class:`CreateTable`, :class:`AddField`, :class:`CreateIndex` and the others, each
rendering its SurrealQL with the renderers ``define_table()`` and the other appliers use. :class:`MigrationStatementError`
reports a statement SurrealDB refused, without quoting it. The executor records the applied migrations in the table
:data:`MIGRATIONS_TABLE`. :func:`introspect_models` (:class:`ModelIntrospector`) builds the schema state the models
describe, which ``SchemaState.diff()`` compares with a database's.
"""

from .constants import MIGRATIONS_TABLE
from .executor import MigrationStatementError
from .introspector import ModelIntrospector, introspect_models
from .migration import Migration
from .operations import (
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
    Operation,
    RawSQL,
    RebuildIndex,
    RemoveAccess,
    RemoveAnalyzer,
    RemoveEvent,
    RemoveFunction,
    split_destructive,
)

__all__ = [
    "MIGRATIONS_TABLE",
    "AddField",
    "AlterField",
    "AlterTable",
    "CreateIndex",
    "CreateTable",
    "DataMigration",
    "DefaultExpression",
    "DefineAccess",
    "DefineAnalyzer",
    "DefineEvent",
    "DefineFunction",
    "DropField",
    "DropIndex",
    "DropTable",
    "Migration",
    "MigrationStatementError",
    "ModelIntrospector",
    "Operation",
    "RawSQL",
    "RebuildIndex",
    "RemoveAccess",
    "RemoveAnalyzer",
    "RemoveEvent",
    "RemoveFunction",
    "introspect_models",
    "split_destructive",
]
