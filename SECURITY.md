# Security Policy

## Supported Versions

The following versions of Surreal-ORM-Lite are currently supported with security updates:

| Version | Supported          |
| ------- | ------------------ |
| 0.30.x  | :white_check_mark: |
| < 0.30  | :x:                |

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
