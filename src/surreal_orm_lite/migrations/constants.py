"""Constants shared across the migrations package (the full SurrealDB-ORM's ``migrations.constants``).

A leaf module: the executor writes the tracking table, the diff and the model generator skip it, and none of them has
to import another for its name.
"""

#: The table where the executor records the applied migrations (the full ORM's name, so a database migrated by one ORM
#: reads the same history from the other).
MIGRATIONS_TABLE = "_surreal_orm_migrations"
