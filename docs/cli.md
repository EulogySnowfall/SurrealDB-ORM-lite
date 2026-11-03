# Command line

The `surreal-orm-lite` command line: migrations, introspection and a SurrealQL shell.

## 40. Command line (`surreal-orm-lite`)

Since v0.39.0 the package installs a `surreal-orm-lite` console script, also run as `python -m surreal_orm_lite.cli`,
with no extra to install: it is the full SurrealDB-ORM's command line — its nine commands, global options,
`SURREAL_*` environment variables, defaults and output texts — built on the standard library's `argparse`. Each
command is a thin front end over the migrations of [section 39](migrations.md#39-migrations-makemigrations-migrationexecutor-operations)
and the `generate_models_from_db()` of
[section 31](schema.md#31-schema-introspection-databaseintrospector-generate_models_from_db): the command line decides nothing
about the schema.

### Install and run

```bash
pip install surreal-orm-lite
surreal-orm-lite --version
surreal-orm-lite --help
surreal-orm-lite migrate --help
python -m surreal_orm_lite.cli status --help
```

Global options go **before** the command (`surreal-orm-lite -n app -d app migrate`); after `makemigrations`, `-n` and
`-m` mean `--name` and `--models`, as in the full ORM. Run the command line from the project directory: the migrations
directory (`--migrations-dir`/`-m`, `migrations` by default) is relative to it, and it is put on the import path
before the `--models` modules are imported, so `--models app.models` finds `app/models.py`.

### Connection options

| Option                   | Environment variable | Default                 |
| ------------------------ | -------------------- | ----------------------- |
| `--url`, `-u`            | `SURREAL_URL`        | `http://localhost:8000` |
| `--namespace`, `-n`      | `SURREAL_NAMESPACE`  | none                    |
| `--database`, `-d`       | `SURREAL_DATABASE`   | none                    |
| `--user`                 | `SURREAL_USER`       | `root`                  |
| `--password`             | `SURREAL_PASSWORD`   | `root`                  |
| `--migrations-dir`, `-m` | none                 | `migrations`            |
| `--connection`           | none                 | `default`               |

An option wins over its environment variable, a variable over the default, and an empty variable counts as unset. The
URL is any URL `set_connection()` accepts: `ws://host:8000/rpc`, `http://host:8000`, `mem://` or `surrealkv://<path>`
(the user and password are never sent to the embedded engine). Every command that reaches the database needs a
namespace and a database; without them it stops with a usage error before connecting. `sqlmigrate`,
`makemigrations --empty` and `makemigrations --no-from-db` work offline and need neither.

### Commands

A model module (`app/models.py` declaring the models of [section 39](migrations.md#39-migrations-makemigrations-migrationexecutor-operations)),
from its first migration to its rollback:

```bash
export SURREAL_URL=ws://localhost:8000/rpc SURREAL_NAMESPACE=app SURREAL_DATABASE=app
export SURREAL_PASSWORD='…'
surreal-orm-lite makemigrations -n initial -m app.models
surreal-orm-lite migrate
surreal-orm-lite status
surreal-orm-lite makemigrations -n again -m app.models   # No changes detected.
surreal-orm-lite schemadiff -m app.models                # Models and database are in sync.
surreal-orm-lite rollback ""
surreal-orm-lite status
```

`makemigrations` prints the file it wrote and its operations, `migrate` the migrations it applied, and `status` each
migration file, applied (`[X]`) or not:

```
Created migration: migrations/0001_initial.py
Operations: 3
  - Create table person
  - Add field age to person
  - Add field name to person
Applied 1 migration(s):
  - 0001_initial
Migration status:
------------------------------------------------------------
[X] 0001_initial (3 ops) [R-]
------------------------------------------------------------
Legend: [X]=applied, R=reversible, D=has data migrations
```

The other commands, one line each:

- `makemigrations -n NAME --empty` writes a migration with no operation, to fill in by hand.
- `makemigrations -n NAME -m MODULE --no-from-db` diffs the models against the migration files, with no request
  (offline); `--drop-missing` also writes the removals of what no model declares any more.
- `migrate -t TARGET` applies the migrations up to _TARGET_; `migrate --fake` records them without running them.
- `upgrade [-t TARGET]` runs the data migrations (`DataMigration`) of the applied migrations.
- `rollback TARGET` undoes the migrations applied after _TARGET_; `rollback ""` undoes them all.
- `sqlmigrate MIGRATION` prints the statements of a migration file without a connection; `--backwards` prints those
  that undo it, `--line 2` those of SurrealDB 2.x and the embedded engine.
- `inspectdb` prints the models of the database's tables; `-o FILE` writes them to a file, `--table NAME` (repeatable)
  reads only those tables, `--no-overwrite` refuses to replace an existing file.
- `schemadiff -m MODULE` lists what separates the models from the database, read-only, the irreversible removals
  apart.

### The SurrealQL shell

`surreal-orm-lite shell` sends each line you type to the database, unchanged, and shows SurrealDB's answer per
statement: a line that holds several statements gets a `-- [i/N] --` header before each one, an error is an `Error:`
line, and the session goes on until `exit`, `quit` or end of input.

```bash
surreal-orm-lite -u mem:// -n app -d app shell
```

```
Connected to mem://
Namespace: app, Database: app
Type 'exit' or 'quit' to exit. Enter SurrealQL queries:
------------------------------------------------------------
surreal> CREATE person:ada SET name = 'Ada'; RETURN 1 + 1
-- [1/2] --
{'id': RecordID(table_name=person, record_id='ada'), 'name': 'Ada'}
-- [2/2] --
2
surreal> SELECT name FROM person
{'name': 'Ada'}
surreal> exit
Goodbye!
```

Each input line is one request: a transaction must fit on one line (`BEGIN; …; COMMIT;`). Over HTTP no session state
(`LET`, `USE`) survives to the next line; use a `ws://` URL to keep it.

### Several databases

`--connection NAME` registers the settings under that connection name, and every command uses it: `makemigrations`
writes the models declared with `connection="NAME"`, and `inspectdb` writes `connection="NAME"` in the models it
generates ([section 30](connections.md#30-multiple-databases-add_connection-using)). Without it the connection is `default`.

### Exit codes

- `0`: success, including `No changes detected.`, `No migrations to apply.` and the differences `schemadiff` finds.
- `1`: the command failed, with one `<Command> failed: <reason>` line on stderr and no traceback.
- `2`: a usage error, including a missing namespace or database.
- `130`: interrupted with Ctrl+C (`Interrupted.` on stderr).

### Security

- Pass the password in `SURREAL_PASSWORD` rather than `--password`: an option is visible in the process list. The
  command line never prints it — not in `--help`, not in an error, not in the shell's banner, which drops the URL's
  `user:password@`.
- The `root`/`root` defaults are SurrealDB's and the full ORM's development defaults: set a real user in production.
- `--models` imports the modules it names and the migration commands load the migration files: both run their Python
  code. Run the command line from your project directory, on modules and migration files you trust.
- The shell runs any SurrealQL with the configured identity, as `surreal sql` does: give it a user with the rights
  you mean to use.

### Server lines

The commands behave the same on SurrealDB 3.x, 2.x and the embedded engine; what differs is what the API they call
does on each line. `migrate` and `rollback` refuse `ASYNC` events, `WITH REFRESH` accesses and `COMPUTED` fields on 2.x
and the embedded engine before any change (exit 1). The shell shows each line's answers as they come: on 3.x a
transaction answers one entry for `BEGIN` and one for `COMMIT`, on 2.x and the embedded engine only the statements
inside it; a `SELECT` on a missing table is an `Error:` line on 3.x and `(empty result)` on 2.x. On the embedded
engine each command opens and closes the database: `mem://` starts empty at every command (use it for the shell and
the offline commands), while `surrealkv://<path>` keeps its data from one command to the next. The
[2.x/3.x table](compatibility.md#orm-behaviour-surrealdb-2x-vs-3x) and the [embedded table](compatibility.md#orm-behaviour-the-embedded-engine) list
the differences.

### Coming from the full SurrealDB-ORM

- **No `click`, no extra**: the command line is built on `argparse`, so the script works after a plain
  `pip install surreal-orm-lite` (the full ORM needs `pip install surreal-orm[cli]`). Commands, options, environment
  variables, defaults and texts are the same; the script is `surreal-orm-lite` instead of `surreal-orm`.
- **The shell answers per statement**: one block per statement, with `-- [i/N] --` headers when a line holds several
  statements, where the full ORM flattens every record of the line into one list.
- **Options of this library**: `--connection`, `--version`, `sqlmigrate --backwards` and `--line`, and
  `inspectdb --table` and `--no-overwrite`, each a parameter of the function the command calls.
- **`makemigrations` keeps this library's meaning**: `--empty` writes a migration that depends on the latest ones;
  `--no-from-db` diffs against the migration files (offline), where the full ORM diffs against an empty schema, and
  `--drop-missing` keeps its meaning there.
- **Skipped removals are one warning**: the removals left out without `--drop-missing` are reported as one `Warning:`
  line on stderr, not as a list on stdout.
- **Exit codes**: exit 2 when the namespace or the database is missing, before any connection; 130 on Ctrl+C; 0 for
  `schemadiff` even when it finds differences, as in the full ORM.
- **`schemadiff` with no registered model exits 1**, where the full ORM lists every table of the database as a removal.
- **Ctrl+C during a shell query ends the session** with exit 130; Ctrl+C at the prompt still says `Goodbye!`.
- **Not inside an event loop**: the command line refuses to run inside a running event loop (Jupyter, an async test)
  and exits 1; run it in a subprocess.
- **No models is an error on stderr**: `No models found. Register models by importing them (--models).` goes to stderr,
  with the `(--models)` hint.
- **Any import failure is reported**: any exception raised by a `--models` import is reported as
  `Error importing <module>: <reason>`, not only an `ImportError`.
- **The banner hides credentials**: the shell banner never prints the URL's credentials (`user:password@` is dropped).
