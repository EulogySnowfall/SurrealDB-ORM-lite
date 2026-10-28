"""Schema migrations for SurrealDB (v0.38.0), the full SurrealDB-ORM's ``migrations`` package on the official SDK.

A migration is a Python file of a migrations directory defining a :class:`Migration`: a name, the migrations it
depends on and its operations — :class:`CreateTable`, :class:`AddField`, :class:`CreateIndex` and the others, each
rendering its SurrealQL with the renderers ``define_table()`` and the other appliers use. :class:`MigrationStatementError`
reports a statement SurrealDB refused, without quoting it. The executor records the applied migrations in the table
:data:`MIGRATIONS_TABLE`. :func:`introspect_models` (:class:`ModelIntrospector`) builds the schema state the models
describe, which ``SchemaState.diff()`` compares with a database's. :class:`MigrationGenerator` writes a migration's file
from its operations, and :func:`generate_empty_migration` one to fill in by hand. :class:`MigrationExecutor` loads the
files of a migrations directory in their dependency order, gives the state they build and the statements of each one;
:func:`makemigrations` writes the next file from the models — by default against the database's schema, which
:class:`DatabaseIntrospector` reads (:class:`ModelCodeGenerator` turns such a read into models; both re-exported from
``surreal_orm_lite.introspection``, as the full ORM's ``migrations`` package exports them).
"""

from ..introspection import DatabaseIntrospector, ModelCodeGenerator
from .autodetect import makemigrations
from .constants import MIGRATIONS_TABLE
from .executor import MigrationExecutor, MigrationStatementError
from .generator import MigrationGenerator, generate_empty_migration
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
    "DatabaseIntrospector",
    "DefaultExpression",
    "DefineAccess",
    "DefineAnalyzer",
    "DefineEvent",
    "DefineFunction",
    "DropField",
    "DropIndex",
    "DropTable",
    "Migration",
    "MigrationExecutor",
    "MigrationGenerator",
    "MigrationStatementError",
    "ModelCodeGenerator",
    "ModelIntrospector",
    "Operation",
    "RawSQL",
    "RebuildIndex",
    "RemoveAccess",
    "RemoveAnalyzer",
    "RemoveEvent",
    "RemoveFunction",
    "generate_empty_migration",
    "introspect_models",
    "makemigrations",
    "split_destructive",
]
