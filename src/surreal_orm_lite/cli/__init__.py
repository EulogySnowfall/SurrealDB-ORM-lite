"""
SurrealDB-ORM-lite Command Line Interface (v0.39.0): the ``surreal-orm-lite`` console script.

Provides Django-style migration commands:
- makemigrations: Generate migration files from model changes
- migrate: Apply schema migrations to the database
- upgrade: Apply data migrations to transform records
- rollback: Revert migrations to a specific point
- status: Show migration status
- sqlmigrate: Show SQL for a migration without executing
- inspectdb: Generate Python models from existing database tables
- schemadiff: Compare Python models against the live database schema
- shell: Start an interactive SurrealQL shell

Also run as ``python -m surreal_orm_lite.cli``. ``import surreal_orm_lite`` does not import this package.
"""

from .commands import cli, main

__all__ = ["cli", "main"]
