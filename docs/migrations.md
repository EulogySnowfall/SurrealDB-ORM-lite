# Migrations

Migration files, their operations and the executor.

## 39. Migrations (`makemigrations()`, `MigrationExecutor`, operations)

Since v0.38.0 the ORM writes Django-style migration files from the models and applies them, with the full
SurrealDB-ORM's API: `surreal_orm_lite.migrations` holds `Migration`, the operations (`CreateTable`, `AddField`,
`CreateIndex` and the others), `MigrationGenerator`, `MigrationExecutor` and `makemigrations()`, and
`surreal_orm_lite.schema_diff()` compares the models with a database. A migration is a Python file of a migrations
directory; `MigrationExecutor.migrate()` applies each one atomically, in one SurrealDB transaction together with its
record in the `_surreal_orm_migrations` table, and `rollback()` undoes them, on SurrealDB 3.x, 2.x and the embedded
engine.

### Write a migration

```python
from surreal_orm_lite import BaseSurrealModel, SchemaMode, SurrealConfigDict
from surreal_orm_lite.migrations import makemigrations


class Person(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="person", schema_mode=SchemaMode.SCHEMAFULL)
    name: str
    age: int = 18


class Post(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="post", schema_mode=SchemaMode.SCHEMAFULL)
    title: str
    body: str


path = await makemigrations("initial", models=[Person, Post])
print(path)
# migrations/0001_initial.py
```

`makemigrations()` compares the models with the current schema and writes the next file of the migrations directory
(`migrations_dir=`, `"migrations"` by default): here a `CreateTable` and its `AddField` operations for each table. It
returns `None` and writes nothing when there is no change. By default it reads the connected database and replays on
top the migration files not yet applied (the full ORM CLI's `--from-db`), so it needs the connection; `from_db=False`
diffs against the migration files alone, with no request (offline). Without `models=`, it takes every model class
defined (`get_registered_models()`); `connection=` names the connection, whose models it keeps. A file is plain
Python, to review before applying — this one was written by `makemigrations("add_email")` once `Person` declared an
`email: str` field and `indexes=[Index("person_email", ["email"], unique=True)]`:

```python
"""
Migration: 0002_add_email
Generated: 2026-10-27T12:00:00+00:00

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
```

`generate_empty_migration("migrations", "backfill")` writes a file with no operation, to fill in by hand.

### Apply it

```python
from surreal_orm_lite.migrations import MigrationExecutor

executor = MigrationExecutor("migrations")
print(await executor.migrate())
# ['0001_initial', '0002_add_email']
print((await executor.get_migration_status())["0002_add_email"])
# {'applied': True, 'reversible': True, 'has_data': False, 'operations': 2}
print(await executor.show_sql("0002_add_email"))
# DEFINE FIELD `email` ON `person` TYPE string;
#
# DEFINE INDEX `person_email` ON `person` FIELDS `email` UNIQUE;
```

`migrate()` first reads the database's schema (`INFO FOR DB` and `INFO FOR TABLE`, no change), replays the pending
migrations over it and checks them, so that nothing is applied when one of them would be refused; then it applies
each migration in one transaction with its record, so a migration is applied entirely or not at all. Because the
statements are rendered from the database's schema, a table made by `define_table()` before any migration can be
altered by one. A migration that defines analyzers commits them first, in a transaction of their own (SurrealDB builds
an index with the committed analyzer only), and restores them to the definition the database held if the rest of the
migration fails. Another process applying the same migration at the same time is safe: the tracking table's unique
index lets one of them record it, and the other skips it with a warning. `migrate(target="0001_initial")` stops after
that migration, and `show_sql(name)` prints a migration's statements without a request (`line=2` for SurrealDB 2.x and
the embedded engine, `backwards=True` for its rollback). To adopt a database built before its migrations,
`migrate(target="0001_initial", fake=True)` records the migrations up to that one without applying them, then
`migrate()` applies the next ones. `executor = MigrationExecutor("migrations", connection="analytics")` uses a named
connection. A refused statement raises `MigrationStatementError`, which names the migration, the operation and
SurrealDB's reason, never the statement.

### Change the models

```python
await makemigrations("add_email", models=[Person, Post])  # 0002_add_email.py
print(await makemigrations("add_email_again", models=[Person, Post]))
# None
```

A second `makemigrations()`, run before `migrate()`, writes nothing: the pending file is replayed over the database,
so a new migration never repeats a pending one. The diff compares tables (schema mode, type, change feed, permissions,
comment, view query, relation ends), fields, indexes, events, analyzers and the access of a user model; functions are
not compared. It carries what a model leaves to the database: a clause the model does not declare stays as the
database has it, as with `define_table()` — [section 35](schema.md#35-model-table-ddl-define_table-schema_mode-permissions)
says how to remove one. Without `drop_missing=True`, removals (`DropTable`, `DropField`, `DropIndex`, `RemoveEvent`,
`RemoveAnalyzer`, `RemoveAccess`), reversible or not, are left out and named in a warning: an event added by hand,
which no model declares, is never removed by default. With `from_db=False` a removal is relative to the files: a model
deleted since the last migration.

A removal of something neither the database nor an earlier migration holds is refused before any change, by
`makemigrations()` and `migrate()` alike, since SurrealDB would refuse it; a `RawSQL` that defines something declares
it in `state_operations=` (`RawSQL("DEFINE TABLE log SCHEMALESS;", state_operations=[CreateTable(name="log",
schema_mode="SCHEMALESS")])`). The history of the database must match the files: a recorded migration whose dependency
is not recorded is an error everywhere but in `get_migration_status()`, which warns; a recorded migration without its
file is an error in `makemigrations()`, `upgrade()` and `rollback()`, and a warning in `migrate()` and
`get_migration_status()`.

### Data migrations

```python
from surreal_orm_lite.migrations import AddField, DataMigration, Migration

migration = Migration(
    name="0003_person_slug",
    dependencies=["0002_add_email"],
    operations=[
        AddField(table="person", name="slug", field_type="option<string>"),
        DataMigration(
            forwards_sql="UPDATE person SET slug = string::lowercase(name);",
            backwards_sql="UPDATE person UNSET slug;",
        ),
    ],
)
```

`DataMigration` takes SurrealQL (`forwards_sql`, `backwards_sql`) or `async def` functions taking no argument
(`forwards_func`, `backwards_func`). `migrate()` applies the schema only (`schema_only=True`, the default); `await
executor.upgrade()` runs the data operations of the applied migrations, and `migrate(schema_only=False)` runs a
migration's data operations right after its schema transaction. Data operations are not recorded: running `upgrade()`
again runs them again, so write them idempotent.

### Roll back

```python
print(await executor.rollback("0001_initial"))
# ['0002_add_email']
await executor.rollback("")  # undo every migration
```

`rollback(target)` undoes the migrations applied after _target_, the last first, each in one transaction that also
deletes its record; `""` undoes them all. Every migration to undo must be reversible, or nothing is undone. Creations
are undone with `REMOVE … IF EXISTS`; redefinitions and the removals of indexes, analyzers, events, accesses and
functions are reversible when they carry `previous=`, the definition they replace (the diff fills it in);
`RebuildIndex` is reversible. `DropTable` and `DropField` are irreversible (the records' values are gone), as is a
redefinition whose previous definition the ORM cannot write.

### Operations

| Operation        | Statement (SurrealDB 3.x)                                                                              | Undone by                                                          | Reversible                                     |
| ---------------- | ------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------ | ---------------------------------------------- |
| `CreateTable`    | ``DEFINE TABLE `log` SCHEMAFULL;``                                                                     | ``REMOVE TABLE IF EXISTS `log`;``                                  | yes                                            |
| `AlterTable`     | ``DEFINE TABLE OVERWRITE `person` SCHEMALESS;``                                                        | ``DEFINE TABLE OVERWRITE `person` SCHEMAFULL;``                    | with `previous_schema_mode`                    |
| `DropTable`      | ``REMOVE TABLE `log`;``                                                                                | —                                                                  | no                                             |
| `AddField`       | ``DEFINE FIELD `age` ON `person` TYPE int DEFAULT 18;``                                                | ``REMOVE FIELD IF EXISTS `age` ON `person`;``                      | yes                                            |
| `AlterField`     | ``DEFINE FIELD OVERWRITE `age` ON `person` TYPE int DEFAULT 21;``                                      | ``DEFINE FIELD OVERWRITE `age` ON `person` TYPE int DEFAULT 18;``  | with `previous_type`                           |
| `DropField`      | ``REMOVE FIELD `age` ON `person`;``                                                                    | —                                                                  | no                                             |
| `CreateIndex`    | ``DEFINE INDEX `person_email` ON `person` FIELDS `email` UNIQUE;``                                     | ``REMOVE INDEX IF EXISTS `person_email` ON `person`;``             | yes (`overwrite=True` without `previous=`: no) |
| `DropIndex`      | ``REMOVE INDEX `person_email` ON `person`;``                                                           | the `previous=` definition                                         | with `previous=`                               |
| `RebuildIndex`   | ``REBUILD INDEX `post_body` ON `post`;``                                                               | the same statement                                                 | yes                                            |
| `DefineAnalyzer` | ``DEFINE ANALYZER OVERWRITE `english` TOKENIZERS class,blank FILTERS lowercase,snowball(english);``    | ``REMOVE ANALYZER IF EXISTS `english`;``, or `previous=`           | yes                                            |
| `RemoveAnalyzer` | ``REMOVE ANALYZER `english`;``                                                                         | the `previous=` definition                                         | with `previous=`                               |
| `DefineEvent`    | ``DEFINE EVENT `audit` ON `person` WHEN $event = 'CREATE' THEN { CREATE log SET who = $after.name };`` | ``REMOVE EVENT IF EXISTS `audit` ON `person`;``, or `previous=`    | yes (`overwrite=True` without `previous=`: no) |
| `RemoveEvent`    | ``REMOVE EVENT `audit` ON `person`;``                                                                  | the `previous=` definition                                         | with `previous=`                               |
| `DefineAccess`   | `DEFINE ACCESS account ON DATABASE TYPE RECORD SIGNUP ( … ) SIGNIN ( … ) DURATION …;`                  | ``REMOVE ACCESS IF EXISTS `account` ON DATABASE;``, or `previous=` | yes (`overwrite=True` without `previous=`: no) |
| `RemoveAccess`   | ``REMOVE ACCESS `account` ON DATABASE;``                                                               | the `previous=` definition                                         | with `previous=`                               |
| `DefineFunction` | `DEFINE FUNCTION fn::greet($n: string) -> string { RETURN 'hi ' + $n; };`                              | `REMOVE FUNCTION IF EXISTS fn::greet;`, or `previous=`             | yes (`overwrite=True` without `previous=`: no) |
| `RemoveFunction` | `REMOVE FUNCTION fn::greet;`                                                                           | the `previous=` definition                                         | with `previous=`                               |
| `DataMigration`  | its `forwards_sql` or `forwards_func`                                                                  | its `backwards_sql` or `backwards_func`                            | with one of them                               |
| `RawSQL`         | its `sql`                                                                                              | its `reverse_sql`                                                  | with `reverse_sql`                             |

Names are written in backticks, values as SurrealQL literals; expressions (`value`, `assertion`, `when`, `then`,
permission rules), view queries, function bodies and the SurrealQL of `RawSQL` and `DataMigration` are application code
written as is, after a check that each is one expression or whole statements: never build an operation from user
input. Migration files are Python code, run when they are loaded: point the executor and `makemigrations()` at the
application's own directory only.

### Drift

```python
from surreal_orm_lite import schema_diff

for operation in await schema_diff(models=[Person, Post]):
    print(operation.describe())
```

`schema_diff()` lists the operations that would bring the database to the models — `[]` when they agree — and
`makemigrations()` writes them against the database. The comparison normalises what SurrealDB renders differently
from the ORM (whitespace in expressions, `1.5` and `1.5f`, `7d` and `1w`, permissions equal to SurrealDB's defaults,
`FLEXIBLE` on an `any` column, index options left at their defaults), so the diff right after `migrate()` or
`define_table()` is empty on every line. `SchemaState.diff()` between two states read from databases
(`DatabaseIntrospector`), or migration states (`executor.migration_state()`), compares every clause: carrying what a
model leaves to the database applies only to a state built from models.

### Server lines

The same migration applies on SurrealDB 3.x, 2.x and the embedded engine: the statements that differ are chosen
when they are applied. A full-text `CreateIndex` writes `FULLTEXT` on 3.x and `SEARCH` on 2.x and the embedded
engine. On SurrealDB 2.x and the embedded engine, an `AddField` or `AlterField` of an `any` column of a `SCHEMAFULL`
table writes `FLEXIBLE`, and an `AlterTable` that makes a table `SCHEMAFULL` also redefines its `any` columns,
untyped lists and lists of sub-models `FLEXIBLE` in the same transaction, so their object values are kept (3.x keeps
them without it). Those columns are the database's: `migrate()` and `rollback()` render from the database's schema
with the migrations replayed, while `show_sql()` renders from the migration files alone, so on 2.x and the embedded
engine `migrate()`'s batch may also redefine `FLEXIBLE` columns defined outside migrations that `show_sql(line=2)`
does not show. When the database's definition of a table cannot be read entirely, and the `FLEXIBLE` clauses of such
an `AlterTable`, or of a field holding `any` or a list of objects, would be chosen from that reading, `migrate()` and
`rollback()` raise `SurrealDbError` on those lines before any change
(`DatabaseIntrospector().introspect().issues` lists what could not be read). `DefineEvent(async_mode=True)`,
`DefineAccess(with_refresh=True)` and `COMPUTED` fields need SurrealDB 3.x: on 2.x and the embedded engine they raise
`SurrealDbError` before any change. `AddField(reference=True)` is left to the server: SurrealDB 2.x refuses it unless
its `record_references` capability is enabled. A `RemoveAnalyzer` of an analyzer an index still uses and a `DropTable`
of a table a view reads raise `ValueError` before any change on every line (SurrealDB 2.x would accept them, then
refuse every write). The embedded engine behaves as a 2.x server and applies each migration atomically too
([embedded table](compatibility.md#orm-behaviour-the-embedded-engine)); the [2.x/3.x table](compatibility.md#orm-behaviour-surrealdb-2x-vs-3x) lists
the differences.

### Limits

- A migration's transaction that writes records, then defines an index on the same table: SurrealDB servers do not
  index those records (surrealdb/surrealdb#7618, as for `define_indexes(tx=…)`); define the index in an earlier
  migration, or run `RebuildIndex` after.
- A migration holding `DefineAnalyzer` is atomic **except** its analyzer definitions, committed first (SurrealDB
  builds an index with the committed analyzer only): if the rest fails they are restored, and the error says whether
  that worked. Records other clients write in between are tokenised with the new analyzer (run `RebuildIndex`).
- Analyzers are written with `OVERWRITE` first, so an analyzer of the same name made outside migrations is replaced,
  and when the rest of the migration fails the definition the database held is restored (or the analyzer removed when
  the database held none). A migration that redefines an analyzer whose current definition the ORM cannot read or
  write is refused before any change: redefine that analyzer in a form `Analyzer` accepts, by hand or with
  `define_indexes()`, then migrate again.
- A redefined, removed or re-created access gets a new signing key from SurrealDB, so the tokens issued before are
  refused: sign in again (open WebSocket sessions keep working).
- Live queries on a table a migration or a rollback removes are killed by SurrealDB: 3.x ends the subscription, 2.x
  goes silent. A redefinition keeps them.
- Over HTTP the SDK gives up after 30 s, so a migration that builds a large index may be applied without an answer:
  the executor reads the tracking table, says so in a warning and does not run its data operations. Prefer WebSocket
  for long migrations.
- An `AlterTable` writes the table's whole definition, and `DEFINE TABLE OVERWRITE` resets every clause it does not
  write, so with `from_db=False` the clauses set on the database outside migrations are replaced (with the default
  `from_db=True` they are read and kept).
- `migrate()` and `rollback()` read the schema once, before their first change, so a definition another client
  changes meanwhile is not seen.
- On SurrealDB 2.x and the embedded engine, `show_sql()` shows the statements rendered from the migration files, and
  `migrate()`'s batch may hold more `FLEXIBLE` redefinitions (see "Server lines").
- Functions are not compared and there is no squashing; the command line is in [section 40](cli.md#40-command-line-surreal-orm-lite).

### Coming from the full SurrealDB-ORM

- **Import from `surreal_orm_lite.migrations`**: `from surreal_orm.migrations import …` becomes
  `from surreal_orm_lite.migrations import …`; a full-ORM migration file whose operations exist here loads after that
  change. `schema_diff`, `DefineAnalyzer`, `DefineEvent`, `RebuildIndex`, `RemoveEvent` and `get_registered_models`
  are also at the top level, as in the full ORM.
- **`makemigrations()` is a coroutine**: the full ORM's `makemigrations` command, with the same default — it reads the
  database.
- **One transaction per migration**, its record included: a failed migration leaves nothing applied (the full ORM
  sends the statements one after the other and leaves the first ones applied).
- **The same tracking table**: `_surreal_orm_migrations`, with the same fields and unique index, so a database
  migrated by the full ORM keeps its history.
- **Statements**: names in backticks, `TYPE NORMAL` where the full ORM writes no `TYPE`, `FLEXIBLE` after `TYPE`, and
  `THEN { … }` for an event, as `define_table()` and the other appliers write them.
- **`previous=`** on the redefinitions and on the removals of indexes, analyzers, events, accesses and functions makes
  them reversible; the full ORM's removals are irreversible.
- **`default=` is data**: a Python value written as a SurrealQL literal (a `str` is always a string); an expression is
  a `SurrealFunc`, and a default read from a database is a `DefaultExpression`.
- **Not provided**: `DefineBearerAccess`, `DefineApi`/`RemoveApi`, `DefineGraphQLConfig`/`RemoveGraphQLConfig`,
  `encrypted=` and `CreateIndex(concurrently=True)`.
- **`MigrationStatementError` is a `SurrealDbError`** and a `RuntimeError` (the full ORM's base): it names the
  migration, the operation and SurrealDB's reason, never the statement.
- **What the diff carries**: a clause a model does not declare stays as the database has it, as with `define_table()`
  (the full ORM resets it).
- **Creations are undone with `REMOVE … IF EXISTS`**, and removals that carry `previous=` are reversible.
- **Operations are frozen**: `reversible` is a read-only property, and an operation is changed by building another.
- **`SchemaState.applied_migrations` is filled** from the tracking table by `DatabaseIntrospector` and by
  `migration_state()` (the full ORM declares it and leaves it empty).
- **`DefineFunction` and `RemoveFunction` are added**; `parse_define_*` are not exported.
- **Porting a migration file's defaults**: in a full-ORM migration file, a string `default=` holding `::` or starting
  with `$` is an expression for the full ORM but a string literal here; rewrite it as `SurrealFunc('…')` (for example
  `default="time::now()"` becomes `default=SurrealFunc("time::now()")`).
- **Data operations run after the schema**: `migrate(schema_only=False)` runs a migration's `DataMigration`s after its
  schema transaction, where the full ORM runs them in position; a data step that reads a field a later operation of the
  same migration removes goes in a migration of its own.
- **Targets are names**: an unknown `migrate()` or `rollback()` target raises `ValueError`, where the full ORM compares
  names as strings (`n <= target`, `name > target`).
- **`split_destructive()` partitions on removals**: a `RawSQL` without `reverse_sql` or a `RebuildIndex` stays in the
  first list, where the full ORM's partition on `reversible` puts it in the second.
- **The current state**: `makemigrations()` reads the database and replays the migration files it has not applied on
  top, so a second run before `migrate()` writes nothing (the full ORM's `--from-db` writes the pending operations
  again); `from_db=False` diffs against the migration files, where the full ORM's `--no-from-db` diffs against an empty
  schema.
- **The model registry**: `get_registered_models()` is provided (the model classes alive, read from the subclasses of
  `BaseSurrealModel`; a class whose name starts with `_` is left out but its subclasses are listed, as in the full
  ORM); they come depth first — each class's subclasses right after it, in definition order —, not in the full ORM's
  global definition order; `clear_model_registry()` is not provided, as there is no registry to clear.
- **`migrate()` and `rollback()` read the schema first**: before any change they read the database's schema
  (`INFO FOR DB`, `INFO FOR TABLE`), replay the migrations over it and check them, so a removal SurrealDB would refuse,
  an analyzer still used by an index or a table a view reads (made outside migrations or not) is refused before
  anything is applied, and on SurrealDB 2.x the `FLEXIBLE` statements cover the columns the database holds; the full
  ORM sends each migration's statements as written. The session needs the right to read the schema, which every
  system user has.
