# Security Policy

## Supported Versions

The following versions of Surreal-ORM-Lite are currently supported with security updates:

| Version | Supported          |
| ------- | ------------------ |
| 0.32.x  | :white_check_mark: |
| < 0.32  | :x:                |

## Embedded Engine

Since 0.25.0, Surreal-ORM-Lite can run on the official SDK's in-process SurrealDB engine
(`mem://`, `surrealkv://<path>`). That engine has no authentication of its own: its anonymous
session owns every namespace and database, and the ORM never sends the configured user and
password to it. **An embedded database is protected by the file-system permissions of its path,
not by SurrealDB users.** Record users and table `PERMISSIONS` still apply to a session signed in
as a record user.

## Schema introspection

Since 0.30.0, `DatabaseIntrospector` and `generate_models_from_db()` only read the schema
(`INFO FOR DB`, `INFO FOR TABLE`): no record is read and nothing is defined or written. On a server
they need a system user (root, a namespace or a database user): a record user or an anonymous
session is refused. The `users` section of `INFO FOR DB` is never read. On SurrealDB before
3.2.2 (every 2.x release, 3.x before 3.2.2, and the embedded engine of SDK 2.0.0), `INFO FOR DB`
shows user password hashes to every role that may run `INFO`
([SurrealDB's `INFO` documentation](https://surrealdb.com/docs/surrealql/statements/info));
introspection never reads, stores, logs or writes them.

The generated model source treats every name, comment and expression read from the database as
data: they reach it only as escaped string literals, sanitised single-line comments and
identifiers the generator builds, so a schema cannot inject code into the module. A definition
that cannot be parsed is reported without its text, which may hold a secret: neither the
`SchemaIssue` message nor the logged warning quotes it (it stays in the issue's `statement`), and
the state classes keep definition texts out of `repr()`. Files written by
`generate_models_from_db()` are replaced atomically; pass `overwrite=False` to refuse an existing
file.

## Server-side events and stored functions

Since 0.31.0, `Event` expressions, `define_function()` bodies, kinds and permission conditions are
SurrealQL written into DDL as given: trusted application code, never user input. Their structural
check catches mistakes — unbalanced brackets, a comment that would swallow the clauses the ORM
appends, a second statement where one expression is expected — and is not an injection defence.
Event, table, parameter and function names are validated as identifiers (event and table names
also quoted), comments are escaped literals; applying needs a privileged system user.

## Materialized views

Since 0.32.0, a model's `view_query` is SurrealQL written into `DEFINE TABLE … AS (…)` as given:
trusted application code, never user input. Its structural check catches mistakes and is not an
injection defence; the table name is validated and quoted. `define_view()` refuses to turn a table
that holds records into a view and `remove_view()` refuses to remove a table that is not a view,
since SurrealDB would delete its records (and, on SurrealDB 2.x and the embedded engine, a view
that other views read, which would turn them into stale ordinary tables); the ORM's refusal to
write to a view guards against mistakes in application code and is not an access control
(SurrealDB 2.x and 3.1 accept writes to a view from any session allowed to write it). A view is
defined with SurrealDB's default permissions (`NONE`); applying needs a privileged system user.

## Reporting a Vulnerability

If you discover a security vulnerability in Surreal-ORM-Lite, please report it responsibly:

1. **Do NOT** open a public GitHub issue for security vulnerabilities
2. **Email** the maintainer directly at: <croteau.yannick@gmail.com>
3. Include:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

### Response Timeline

- **Acknowledgment**: Within 48 hours
- **Initial Assessment**: Within 7 days
- **Fix Timeline**: Depends on severity (critical: ASAP, high: 14 days, medium: 30 days)

### What to Expect

- We will acknowledge receipt of your report
- We will investigate and validate the vulnerability
- We will work on a fix and coordinate disclosure
- Credit will be given to reporters (unless anonymity is requested)
