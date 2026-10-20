# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.31.1] - 2026-10-19

Transaction errors give the server's reason and the failed statement's position, without the
server's excerpt of the statement, and a buffered commit raises `SurrealDbError` on the embedded
engine too (#278). No new public API.

### Fixed

- **Transaction errors give the server's reason and the statement's position, without the
  server's excerpt of the statement** (#278). When the server refused the query as a whole (a statement it cannot parse), the error read "Transaction failed: unrecognized
  query_raw() response shape: {…}" and quoted the server's raw response, its excerpt of the
  statement included. It now gives the server's reason and the position:
  - at a buffered commit (SurrealDB 2.x, HTTP, the embedded engine), "Transaction failed: the
    server refused statement 2 of 3: … (at line 1, column 7 of the statement)", the line and column
    of the statement as written, before the ORM renames its `$name` parameters for the batch; when
    the server first reports a statement left unclosed on the batch's `COMMIT` line, the statement
    is still named;
  - for a write or a definition in an interactive transaction (SurrealDB 3.x over WebSocket),
    "Transaction failed: the server refused the statement: … (at line 1, column 7 of the
    statement)";
  - a failed statement's error, "Transaction failed and rolled back: …", keeps only the first line
    of a reason that quotes the query;
  - a response of any other shape raises "Transaction failed: unrecognized query_raw() response
    shape (<type>)", without the response.

  The server's full text stays reachable as the error's `__context__`, which a formatted traceback
  does not print. A read in a transaction still raises the SDK's own error, as it does outside a
  transaction.
- **A buffered commit on the embedded engine raises `SurrealDbError`** (#278). The engine reports a
  query it cannot parse as a bare `RuntimeError`, which propagated out of the `transaction()` block;
  the commit now raises `SurrealDbError`, or `SurrealDbConflictError` for a retryable conflict, as
  on a server.
- **A statement holding conflict words is not taken for a conflict** (#278). A query the server
  could not parse, whose statement held words such as "write conflict", was typed
  `SurrealDbConflictError`, so `retry_on_conflict` retried it. The typing now reads the server's
  reason, not its excerpt of the statement.

### Changed

- **The cancellation error names the failed statement's error type** (#278). Since v0.30.3,
  leaving an interactive `transaction()` block after a failed statement, or an explicit
  `tx.commit()`, raises "The transaction was cancelled: a statement in it failed; nothing was
  committed (…)", whose parenthesis repeated the failed statement's error text. The parenthesis now
  holds that error's type name, for example `(SurrealDbError)`. The error itself is still chained
  as `__cause__`, and a retryable conflict still raises `SurrealDbConflictError`, so
  `retry_on_conflict` behaves as before.
- **`is_conflict_error()` follows the chain a traceback prints** (#278): a context suppressed with
  `raise … from None` is no longer read. The ORM keeps the server's full text there, which may
  quote the statement.

### Notes

- The v0.31.0 note on the DDL appliers with `tx=` (#278) no longer applies: an error raised when
  the transaction ends gives the server's reason and the statement's position, without the
  server's excerpt of the statement. README sections 11 and 32 and the embedded-engine table say
  so.
- Same behaviour on SurrealDB 2.x, 3.x and the embedded engine. Server pins unchanged: tested
  against SurrealDB 3.3.4 and 2.7.0 and on the embedded engine, plus 3.1.5 for backward
  compatibility.

## [0.31.1] - 2026-10-19

A datetime outside years 1-9999 no longer fails the response it is in (#279). SurrealDB stores
datetimes far beyond the years a Python `datetime` holds, and the ORM now reads them back as the
server's own text. No new public API.

### Fixed

- **Reading a datetime before year 1 or after year 9999** (#279). SurrealDB stores datetimes from
  year -262143 to +262142, on 2.x and 3.x, but the surrealdb SDK 2.0.0 decodes a datetime into a
  Python `datetime`, which holds years 1-9999 only: one such value failed the whole response it
  was in, whether it came from `get()`, `filter()`, `raw_query()` or a live notification. The
  ORM's clients now read it as its RFC 3339 text, the one SurrealDB gives it with `<string>`:
  `"0000-06-01T00:00:00Z"`, `"-0001-01-01T00:00:00Z"`, `"+10000-01-01T00:00:00.500Z"` (a sign and
  at least four digits outside years 0000-9999, every nanosecond kept). An untyped field holds
  that string; a `datetime` field refuses it with a `ValidationError` naming the field, so `get()`
  raises it and `filter().exec()` and `raw_query()` return such rows as dicts, as for any row
  that does not validate. Written back through the ORM, the value is stored as that string. A
  datetime within years 1-9999, before 1970 included, reads as the SDK's `datetime`, as before.

### Notes

- As for zero durations, native sets and degenerate geometries, only the clients the ORM opens
  (`SurrealDBConnectionManager.get_client()`, `ephemeral_client()`, the embedded engine's
  included) read these values; an `AsyncSurreal` client the application builds itself keeps the
  SDK's behaviour. The ORM leaves them to the SDK as soon as an installed SDK decodes them itself.
  README: "Values the SDK cannot read" and a new row of the 2.x/3.x behaviour table.
- The function catalogue test (`TestFunctionEnumsE2E`) calls `rand::time()` between 2000 and 2030
  and checks the result. Unbounded, it drew a year-0 datetime in about one call in 7,400 (27 in
  200,000 on each line), which made the test fail at random.
- Same behaviour on SurrealDB 2.x, 3.x and the embedded engine. Server pins unchanged: tested
  against SurrealDB 3.3.4 and 2.7.0 and on the embedded engine, plus 3.1.5 for backward
  compatibility.

## [0.31.0] - 2026-10-19

Server-side events and DDL helpers: the ORM writes and applies `DEFINE EVENT` and
`DEFINE FUNCTION` statements from Python. Events are declared on the model, next to its fields,
and applied idempotently; stored functions are defined for `call_function()` to call. The same
code runs on SurrealDB 2.x, 3.x and the embedded engine; asynchronous events and `$input` are 3.x
only. The second minor of Tier 3.

### Added

- **`Event(name, *, then, when=None, async_mode=False, retry=None, max_depth=None, comment=None)`**
  and **`SurrealConfigDict(events=[...])`**: a model declares its server-side events, which
  SurrealDB runs on every `CREATE`, `UPDATE` and `DELETE` of the table, whoever writes it (the
  ORM, a raw query or another client). `Model.event_ddl()` renders their `DEFINE EVENT` statements
  without a connection, `await Model.define_events()` applies them on the model's connection
  (`DEFINE EVENT OVERWRITE`: run it at start-up) and returns them, and
  `await Model.remove_event("name")` removes one by name, declared or not. `overwrite=False` writes
  `IF NOT EXISTS`, `missing_ok=False` raises `SurrealDbNotFoundError` for a missing event, and
  `Model.get_events()` lists the declared events. A subclass inherits `events` unless it sets its
  own, and typed edges take events too.

  ```python
  class Account(BaseSurrealModel):
      model_config = SurrealConfigDict(
          table_name="account",
          events=[
              Event(
                  "email_audit",
                  when="$event = 'UPDATE' AND $before.email != $after.email",
                  then="CREATE audit_log SET account = $value.id, old = $before.email, new = $after.email",
              ),
              Event(
                  "no_negative_balance",
                  when="$event != 'DELETE' AND $after.balance < 0",
                  then="THROW 'balance cannot be negative'",
              ),
          ],
      )

      id: str | None = None
      email: str
      balance: int = 0


  Account.event_ddl()            # the DEFINE EVENT statements, no connection needed
  await Account.define_events()  # idempotent
  await Account(id="ada", email="ada@example.com", balance=-5).save()  # InternalError: refused
  ```

- **`SurrealDBConnectionManager.define_event(event, table)`** and
  **`remove_event(event, table)`** apply an `Event` to any table, with or without a model, on the
  current connection (the `using()` block's, else `"default"`); `event.ddl(table)` renders the
  statement. The `then` clause is always written as a block (`THEN { … }`), so several statements,
  `LET` and `THROW` work on both lines.
- **Asynchronous events (SurrealDB 3.x)**: `Event(..., async_mode=True, retry=3, max_depth=2)`
  writes `ASYNC RETRY 3 MAXDEPTH 2`. Such an event runs after the write's transaction commits, so
  it does not hold the write up. On SurrealDB 2.x and the embedded engine, which cannot parse
  `ASYNC`, every applier raises `SurrealDbError` before sending any DDL, and no event of the call
  is defined; `event.ddl()` renders an asynchronous event on any line.
- **`SurrealDBConnectionManager.define_function(function, body, *, parameters=None, ...)`**,
  **`remove_function(function)`** and the pure **`function_ddl()`** (exported from
  `surreal_orm_lite`): stored functions defined from Python, named as `call_function()` names them
  (`"greet"`, `"fn::greet"`, `"fn::billing::total"`). `parameters` maps each parameter name to its
  SurrealQL kind in declaration order, the order of `call_function()`'s positional `args`;
  `returns` is the result's kind, `permissions` is `"FULL"`, `"NONE"` or `"WHERE <condition>"`,
  and `comment=` and `overwrite=` work as for events.

  ```python
  await SurrealDBConnectionManager.define_function(
      "fn::greet",
      "RETURN string::concat('Hello, ', $name, '!');",
      parameters={"name": "string"},
      returns="string",
  )
  await SurrealDBConnectionManager.call_function("fn::greet", ["Ada"])  # 'Hello, Ada!'
  ```

- **Every applier takes `tx=`** and returns the statement it applied (`define_events()`: the
  list). A refusal raises
  `SurrealDbAuthenticationError` when the session may not define (a `VIEWER`, a record user, an
  anonymous session), `SurrealDbNotFoundError` for a removal of a missing definition with
  `missing_ok=False`, and `SurrealDbError` otherwise
  (`Can't apply event definition: 'email_audit' on table 'account' -> …`). Without `tx=`, a
  refusal names the event and its table, or the function, never the statement, whose `when`,
  `then` or body may hold a secret, and gives the server's reason without its excerpt of the
  statement. A buffered transaction (SurrealDB 2.x, HTTP, the embedded engine) only queues the
  statement, so a refusal comes back when the block commits, as the transaction's own error, never
  as `SurrealDbAuthenticationError` or `SurrealDbNotFoundError`, `missing_ok=False` included. When
  a definition is applied in a transaction (`tx=`), an error raised when the transaction ends can
  still carry the server's message, statement excerpt included (#278). `define_events()` applies
  one statement per event and stops at the first refusal: pass `tx=` for all or nothing. A `tx=`
  of another connection raises `SurrealDbError` before any request.
- README section 32, "Server-side events and stored functions (`Event`, `define_events()`,
  `define_function()`)" (declaring events on a model, events without a model, asynchronous events,
  stored functions, transactions and connections, SurrealDB 2.x and 3.x, security, a note for users
  of the full SurrealDB-ORM), a features row, seven rows in the 2.x/3.x behaviour table and one in
  the embedded-engine table, the comparison and roadmap rows, and the example notebook
  `examples/v0.31.0_events_ddl_helpers.ipynb`. Section 17's example declares its function with
  `define_function()`.

### Changed

- **`EventState` gains `async_mode`, `retry` and `max_depth`** (keyword-only, after `comment`;
  `False`, `None` and `None` by default), and **`DatabaseIntrospector` reads SurrealDB 3.x
  `ASYNC` events**: 0.30.x reported such an event as a `SchemaIssue`. SurrealDB 3.x renders
  `RETRY` and `MAXDEPTH` with every `ASYNC`, so an event defined without them reads back
  `retry=1, max_depth=3`.
- **`call_function(params=)` follows a redefinition made with `define_function()` or
  `remove_function()` at once**: both drop the function's cached signature on every connection of
  this process to the same namespace and database, whatever its URL (an admin connection applying
  the DDL over `http://` and the application's `ws://` connection alike), so even a reorder of the
  same parameter names is picked up. A signature read that such a drop, or
  `clear_function_signature_cache()`, overtakes is read again rather than cached. A function
  redefined with raw DDL or from another process still needs `clear_function_signature_cache()`.

### Notes

- **Names are validated and quoted, comments escaped**: event, table, parameter and function names
  are checked as identifiers before any request, event and table names are written in backticks
  (`update` or `delete` work as event names), and a comment is an escaped string literal. A
  parameter name SurrealDB defines itself (`auth`, `session`, `this`, …) is refused.
- **Expressions are trusted application code, never user input.** `when`, `then`, function
  bodies, kinds and permission conditions are written into the statement as given, since SurrealDB
  takes no bound parameter in a definition. Their structural check (strings terminated, brackets
  balanced, no NUL, no SurrealQL comment marker outside a literal, every `/` read as an operator,
  no `;` outside brackets where one expression is expected) catches mistakes — unbalanced
  brackets, a comment that would swallow the clauses the ORM appends, a second statement where one
  expression is expected — and is not an injection defence. To use a value, read it from the
  record (`$after.email`) or from a `DEFINE PARAM`. Every refusal is a `TypeError` or a
  `ValueError` raised before any request. An `Event`'s `repr()` leaves out `when` and `then`, and
  a refusal the server returns to a call without `tx=` names the event or the function, never its
  statement (with `tx=`, see #278).
- **Kinds are written in canonical form**: `none | int` and `option<int>` are both written
  `option<int>`, and a kind the ORM cannot read raises `ValueError` rather than being rewritten.
- **SurrealDB 2.x and 3.x**: `ASYNC` events are 3.x only (refused before any DDL elsewhere);
  `$input` holds the fields the write sent on 3.x and is `NONE` on 2.x and the embedded engine; a
  write that changes nothing fires no event, except on 3.x an `UPSERT` (the ORM's `upsert()` of an
  unchanged record), which fires one `UPDATE` event; read back, each line renders event and
  function definitions its own way (README section 32 and the 2.x/3.x table).
- **A write an event refuses** (a `THROW`, an error in `then`) raises the SDK's `InternalError`,
  as any server refusal of a write does, and nothing is written. Every applier takes `tx=`, and
  events work inside `transaction()` like any other statement.
- **Query cache, live queries and signals**: the query cache forgets the tables the ORM itself
  writes, not the rows an event writes, so do not cache reads of a table events write, or call
  `QueryCache.invalidate(Model)` after the write. Live queries see those rows. Applying DDL and the
  rows events write fire no ORM signal.
- **Applying a definition needs a privileged system user** (root, or a namespace or database
  `OWNER` or `EDITOR`): run `define_events()` and `define_function()` at start-up or deployment.
- **Differences from the full SurrealDB-ORM**: events are declared in
  `SurrealConfigDict(events=[...])` rather than a `class Meta`, and the lite's `Event` adds
  `max_depth=`; `then` is written `THEN { … }` where the full ORM writes `THEN (…)`, in which `LET`
  and `THROW` fail; names are quoted and comments escaped with a backslash; `define_function()`
  has no full-ORM equivalent (the full ORM uses `RawSQL`).
- **Deferred**: the migration operations (`DefineEvent`, `RemoveEvent`, a function operation) and
  the removal of events a model no longer declares come with v0.37.0 (migrations):
  `define_events()` leaves such an event defined until `remove_event()` removes it.
  `generate_models_from_db()` lists a table's events in a class comment, not as `events=[...]`.
- The `SurrealConfigDict` keys that generated classes stand in for with comments (`table_type`,
  `permissions`, `changefeed`, `view_query`, `relation_in`, `relation_out`, `enforced`), which the
  0.30.0 and 0.30.8 notes placed in v0.31.0–v0.33.0, are not part of this release: README section
  31 now places them in v0.32.0–v0.34.0.
- **Server pins unchanged**: tested against SurrealDB 3.3.4 and 2.7.0 and on the embedded engine,
  plus 3.1.5 for backward compatibility.

## [0.30.8] - 2026-10-19

Follow-ups to the v0.30.0 schema introspection (#260): a connection lost while the schema is read
raises instead of becoming schema issues, a relation table is an edge whatever the case of its
`table_type`, and generated classes carry the change feed and permissions comments the v0.30.0
notes described. No new public API.

### Fixed

- **A connection lost during `introspect()` raises** (#260). Every error of an `INFO FOR TABLE`
  became a `SchemaIssue`, so a connection that dropped after `INFO FOR DB` turned every remaining
  table into "could not be read": `introspect()` returned normally, and
  `generate_models_from_db(path)` replaced a working module with one that listed those tables as
  skipped. Only the server's answer to `INFO FOR TABLE` is an issue now; the transport's error, or
  the ORM's, propagates unchanged before anything is written, so an existing file is left as it
  was.
- **A relation table is an edge whatever the case of its `table_type`** (#260). The generator
  compared `table_type` with exact case in two places and ignored case in a third: a hand-built
  `TableState(table_type="RELATION")` gave a `BaseSurrealEdge` class that declared `in_` and `out`
  again, and the module failed to import with a `TypeError`. States read by `DatabaseIntrospector`
  hold `"relation"` and were not affected.
- **Generated classes comment their change feed and permissions** (#260), as the v0.30.0 notes
  said: `# Changefeed: 1d INCLUDE ORIGINAL` and
  `# Permissions: FOR select WHERE $auth.id = owner, FOR create, update FULL, FOR delete NONE`,
  grouped the way SurrealDB renders them (`# Permissions: FULL` when `FULL` covers the four
  operations). A table left at the default `PERMISSIONS NONE`, all four operations `NONE`, gets no
  permissions comment. The database still applies both; the `SurrealConfigDict` keys for them
  arrive with v0.31.0–v0.33.0. Unlike what those notes listed, `table_type` is not a comment: it
  shows through the base class, `BaseSurrealEdge` for a relation table.

### Changed

- **An `INFO FOR TABLE` refused after `INFO FOR DB` succeeded raises
  `SurrealDbAuthenticationError`** (#260) instead of becoming a `SchemaIssue`: the session lost its
  rights while the schema was read (for example a client closed and reopened anonymously), and the
  table is not at fault. A refused `INFO FOR DB` raises the same error, as in v0.30.0.

### Notes

- Docstrings and comments no longer cite internal design-note ids: they name the exception raised,
  the case refused or the README section that documents it. README section 31 describes the new
  class comments and which `INFO FOR TABLE` failures are schema issues.
- The linear-time tests of the definition parser keep the fastest of up to three timings, so a
  pause of a shared CI runner cannot fail them.
- Same behaviour on SurrealDB 2.x, 3.x and the embedded engine. Server pins unchanged: tested
  against SurrealDB 3.3.4 and 2.7.0 and on the embedded engine, plus 3.1.5 for backward
  compatibility.

## [0.30.7] - 2026-10-19

An instance stays in step with its stored row (#261): `save()` applies the whole row the server
returns, every call that applies a returned row validates it as a read does, and `update()` keeps
the stored nanoseconds of a datetime it did not change, so a record with a top-level `READONLY`
datetime can be updated. No new public API, but what an instance holds after a write changes for
every model: read **Changed** before upgrading.

### Fixed

- **`update()` after `save()` or `get()` on a `READONLY` top-level `datetime`** is no longer
  refused by the server (#261). SurrealDB stores nanoseconds and the SDK reads microseconds, so
  `update()` wrote back a changed value. See item 5 under Changed.
- **Sub-fields a `DEFAULT` fills** (`prefs.at DEFAULT time::now()`) are on the instance right after
  `save()` (explicit id, auto id, interactive transaction), so a following `update()` no longer
  sends them as `NONE`, which the server refused for a required sub-field and which erased an
  optional one (#261).
- **`tuple` and `set` fields** hold their declared type after `save()`, `upsert()`, `merge()` and
  `patch()`, as after `get()` (item 2 under Changed), not the SDK's `list` (#261).
- **A returned column named like a Pydantic `@computed_field` or a model method** (`json`) no
  longer replaces that attribute (#261). In 0.30.1, with a `@computed_field` (which `model_dump()`
  writes), auto-id `save()`, `refresh()` and `merge()` raised `AttributeError` after the record was
  stored.
- **`generate_models_from_db()`**: generated classes no longer carry the note about `DEFAULT`
  sub-fields, and the `READONLY` `update()` note remains only for a datetime below the top level
  or a top-level one past the 32nd (#261).
- **Buffered transactions**: a statement's parameters are renamed in one pass (#261). In 0.30.1,
  `$a` next to a variable named `t0_a` was renamed twice and bound to `t0_a`'s value, and a
  statement with thousands of bound values was slow to queue.

```python
# DEFINE FIELD created ON event TYPE datetime DEFAULT time::now() READONLY;
event = await Event.objects().get("launch")  # created read to the microsecond
event.title = "Launch day"
await event.update()  # was refused ("readonly"); the stored created keeps its nanoseconds
```

### Changed

Each item was measured against 0.30.1 on SurrealDB 3.3.4, 2.7.0 and the embedded engine
(`mem://`) and holds on all three unless stated.

1. **Explicit-id `save()`, an edge's `save()` and interactive `save(tx=)` apply the whole stored
   row** (#261). In 0.30.1, explicit-id `save()` and interactive `save(tx=)` read back only the
   `server_fields` and computed fields, and an edge's `save()` (either id) only its `id` and
   `server_fields`: the instance kept the values passed in. Now a naive `datetime` becomes the
   aware UTC `datetime` a read gives (compare it with aware values), a sub-model field holds a new
   instance (not the object passed in), and values a `VALUE` clause rewrote or the server coerced
   replace the ones sent (`VALUE string::lowercase($value)` turns `"AbC"` into `"abc"` on the
   instance). Interactive transactions exist on SurrealDB 3.x over WebSocket only; a buffered
   transaction (HTTP, 2.x, the embedded engine) still leaves the instance as it was.
2. **Every call that applies the returned row validates it like a read** (#261): `save()` with
   either id, an edge's `save()`, `refresh()`, `merge()` (default `refresh=True`), `upsert()`,
   `patch()` and the `atomic_*` helpers. An enum field holds the enum member, not the raw
   `str`/`int` (a `str` or `int` enum still compares equal to its value, a plain `Enum` member no
   longer does); `tuple`, `set`, `date` and `Range` fields hold their declared type instead of the
   SDK's `list`, midnight `datetime` and `Range`; a `float` field stored in a `decimal` column holds
   a `float`, not a `Decimal`; a `list[Duration]` holds the ORM's `Duration`s, not the SDK's. In
   0.30.1, `refresh()`, `merge()`, auto-id `save()`, `upsert()`, `patch()` and `atomic_*` kept the
   SDK's values for these fields (explicit-id `save()` kept the values passed in).
3. **The model's validators run on the returned row for every model** (#261). Field and model
   validators (any mode) and `model_post_init` run once per call listed in item 2, on a validated
   copy of the row, and their result is applied to every field. In 0.30.1 they ran only when the
   row held a sub-model field, a nested `timedelta` field or a geometry field stored as a GeoJSON
   object, and their result reached only those fields; otherwise they did not run at all. A
   validator that is not idempotent is applied again, as on `get()`, and a following `update()`
   writes that value back: with `label` validated as `v + "!"`, `Model(id="a", label="x")`,
   `save()` then `update()` stored `x!` in 0.30.1 and stores `x!!` now; `merge()` then `update()`
   gives the same difference.
4. **Under `extra="allow"`, the returned row's undeclared columns go to `model_extra`** after the
   calls listed in item 2, where a read puts them (0.30.1 dropped them), and `merge(refresh=False)`
   with an undeclared key updates `model_extra` (#261).
5. **`update()` and `upsert()` keep the stored nanoseconds of a datetime written back unchanged**
   (one that still floors to the stored value at the microsecond), for every top-level datetime up
   to 32 per record, so a record whose `READONLY` datetimes are top-level fields can be updated
   after `get()` or `save()` (#261). Below the top level (a sub-model, a `list`, a `tuple`, a set)
   this depends on the data: change such a record with `merge()`.

### Notes

- SurrealDB 2.x still refuses `update()` and `upsert()` of an existing record that has a field
  with a non-optional `TYPE` and a `VALUE` clause the model does not write, such as a generated
  `updated: Computed[datetime]` (#267): change such a record with `merge()` there. 0.30.1 behaved
  the same way before this release; SurrealDB 3.x recomputes the field. The v0.30.0 example
  notebook changes its generated `NbPerson` with `merge()` for that reason.
- The README's "After a write that returns the row", "Precision and `Decimal`" and "What the
  generator does not do" sections, and a new row of the 2.x vs 3.x behaviour table, describe these
  changes.
- Server pins unchanged: tested against SurrealDB 3.3.4 and 2.7.0 and on the embedded engine, plus
  3.1.5 for backward compatibility.

## [0.30.5] - 2026-10-19

`SurrealDBConnectionManager.kill()` no longer reports a connection lost under the request as a
KILL the server refused. No new public API.

### Fixed

- **A `kill()` caught by a dropped connection returns normally** (#266). A KILL sent as the
  WebSocket went down, before the SDK had noticed the drop, failed inside the SDK (its receive loop
  clears the pending requests, so the request's own cleanup raised a `KeyError`), and `kill()`
  turned that into `SurrealDbError: Failed to kill live query …` although the server never
  answered. `kill()` now asks the client: when its connection dropped under the request (the SDK's
  receive task has ended, after a wait of at most one second when the error came from the
  transport), it closes the dead socket and records the live query as ended, as it already did for
  a client found dead before the call, and the stream's readers end. A refusal from a live server
  still raises `SurrealDbError` and keeps the uuid alive, so a retry can re-attach to it.
- **A cancelled `kill()` raises `asyncio.CancelledError`** (#266), even when the SDK reports the
  cancelled request as an ordinary error; it used to become `SurrealDbError`. The readers are
  released either way.
- **`kill()` releases the stream's readers before it closes a dead socket** (#266), so an
  automatic resubscribe woken by the same drop cannot reopen a subscription that is being stopped.
  The window was open where closing the socket yields to the event loop, as on Python 3.11.

### Notes

- Docs: the README live-queries section says that a kill caught by a dropped connection is not a
  refusal.
- Same behaviour on SurrealDB 2.x and 3.x (live queries are WebSocket-only). Server pins
  unchanged: tested against SurrealDB 3.3.4 and 2.7.0, plus 3.1.5 for backward compatibility.

## [0.30.3] - 2026-10-19

A statement that fails inside `transaction()` fails the whole transaction on every strategy: a
transaction block now behaves the same on SurrealDB 2.x and 3.x, over WebSocket, over HTTP and on
the embedded engine. No new public API.

### Changed

- **A failed statement fails the whole `transaction()` block, on every strategy** (#268). After a
  statement fails inside the block, even when the block catches its error and carries on, the
  transaction is cancelled when the block ends, nothing of the block is committed, and leaving the
  block raises `SurrealDbError`, or `SurrealDbConflictError` when the failure was a retryable
  conflict, so `retry_on_conflict` retries the block. Interactive transactions (SurrealDB 3.x over
  WebSocket) now behave like the buffered ones (SurrealDB 2.x, HTTP, the embedded engine), which
  already failed as a whole at commit: the failed statement raises its error at once, then leaving
  the block raises "The transaction was cancelled: a statement in it failed; nothing was committed
  (…)", chained to that error.
  - An explicit `tx.commit()` after a failed statement raises the same error and cancels the
    transaction.
  - When the block's own exception propagates, that exception is the one raised, as before.
  - Reads the ORM answers itself do not fail the transaction: a table that does not exist reads as
    no rows, and `get()` raises `SurrealDbNotFoundError`.
  - A statement interrupted inside the block (a timeout or a task cancellation) fails the
    transaction too.

  Upgrade note: a block that catches a statement's error and carries on now raises when it ends,
  on every strategy, as it already did on the buffered ones. To carry on after such an error, run
  that statement in a transaction of its own or outside the transaction.

```python
async with SurrealDBConnectionManager.transaction() as tx:
    await User(id="alice", name="Alice").save(tx=tx)
    try:
        await Order(id="o2", user="User:alice", total=-1).save(tx=tx)  # refused by a field ASSERT
    except SurrealDbError:
        pass  # caught, but the transaction can no longer commit
# leaving the block raised SurrealDbError: alice was not saved either
```

### Fixed

- **The error of a failed statement, "Transaction failed and rolled back: …", now describes what
  happens on every strategy** (#268): on an interactive transaction as on a buffered one, nothing
  of the block is committed.

### Notes

- Docs: the README transactions section (11) states the rule with an example, and the "ORM
  behaviour: SurrealDB 2.x vs 3.x" table gains a row for a statement that fails inside
  `transaction()` with its error caught in the block.

## [0.30.0] - 2026-10-19

Schema introspection: the ORM reads the schema of a database as immutable typed objects and
generates `BaseSurrealModel` classes from it, with the full SurrealDB-ORM's names. It only reads
(`INFO FOR DB`, `INFO FOR TABLE`), and the same schema gives the same state and the same generated
types on SurrealDB 2.x, 3.x and the embedded engine. The first minor of Tier 3.

### Added

- **`DatabaseIntrospector(connection=None, *, tables=None).introspect()`** returns a
  `SchemaState`: the tables (`TableState`, with their `FieldState`, `IndexState` and `EventState`)
  and the database's accesses (`AccessState`), analyzers (`AnalyzerState`), functions
  (`FunctionState`, keyed `"fn::name"`) and params (`ParamState`). It sends `INFO FOR DB;`, then
  one `INFO FOR TABLE` per table, outside any transaction: no record is read, nothing is defined or
  written. A field is keyed by its path (`"address.city"`, `"tags.*"`), with `field_type` (its kind
  without the optional wrapper), `nullable` (whether it may be `NONE`) and its expressions
  (`default`, `value`, `computed`, `assertion`, permission rules) as SurrealQL text; every
  definition keeps the server's statement in `raw`, and the sections the ORM does not model are in
  `schema.other`. `connection=` is a connection name (else the current one: `using()`, then
  `"default"`), and `tables=` reads those tables only.

  ```python
  from surreal_orm_lite import DatabaseIntrospector

  schema = await DatabaseIntrospector().introspect()
  person = schema.tables["person"]
  person.fields["age"].field_type       # 'int': option<int> on SurrealDB 2.x, none | int on 3.x
  person.fields["age"].nullable         # True
  person.fields["age"].default          # '18': SurrealQL text
  person.indexes["person_name"].unique  # True
  ```

- **`ModelCodeGenerator().generate(state)`** and
  **`generate_models_from_db(output_path=None, connection=None, *, tables=None, overwrite=True)`**
  give deterministic source with one class per table: a `BaseSurrealModel`, or a `BaseSurrealEdge`
  for a `TYPE RELATION` table. Fields use the lite's types (`Duration`, `Decimal`, `UUID`,
  `RecordID`, `Range`, the geometry classes, `Literal`), sub-models for declared nested fields,
  `computed()` for `COMPUTED` and for a `VALUE` that reads neither `$value` nor `$input`, aliases
  for columns that cannot be attribute names, `extra="allow"` for `SCHEMALESS` tables, and
  `connection=` for a schema read from a named connection. A constant `DEFAULT` becomes the Python
  default (`DEFAULT 18` → `= 18`, `[]` → `Field(default_factory=list)`, `1h30m` →
  `Duration.parse("1h30m")`, `u'…'` → `UUID("…")`); any other `DEFAULT` becomes a `server_fields`
  entry. Names, comments and expressions read from the database reach the source only as escaped
  string literals, single-line comments and generated identifiers.

  ```python
  from surreal_orm_lite import generate_models_from_db

  source = await generate_models_from_db()                  # the source, as a string
  await generate_models_from_db("app/models_generated.py")  # also writes it, replacing the file
  await generate_models_from_db("app/models_generated.py", overwrite=False)  # FileExistsError
  ```

- **`generate_models_from_db()` replaces an existing file atomically**, as the full ORM replaces
  it: the source goes to a temporary file in the same directory, then `os.replace()` moves it over
  the file, so a failure leaves the previous file. `overwrite=False` refuses an existing path with
  `FileExistsError` before reading anything.
- **The state classes take the full SurrealDB-ORM's arguments**: `SchemaState`, `TableState`,
  `FieldState`, `IndexState`, `EventState`, `AccessState` and `AnalyzerState` are built by
  position or by keyword as in the full ORM, and the lite's own attributes (`raw`, `path`, `table`,
  `comment`, …) are keyword-only. They are frozen dataclasses with read-only maps and tuples, and
  `repr()` shows names and structure only, never a definition's text. `FunctionState`,
  `ParamState`, `SchemaIssue` and `tables=` are lite additions. The 13 names are exported from
  `surreal_orm_lite`.
- **Nothing is guessed**: a definition that cannot be parsed exactly, a section of an unexpected
  shape or a failed `INFO FOR TABLE` becomes a `SchemaIssue` in `schema.issues` and a WARNING on
  the `surreal_orm_lite.introspection` logger, whose message never quotes the definition.
  `introspect()` raises `SurrealDbAuthenticationError` when the session may not run
  `INFO FOR DB`, `SurrealDbError` when it fails otherwise, and `SurrealDbNotFoundError` for a
  `tables=` name the database does not define.
- README section 31, "Schema introspection (`DatabaseIntrospector`, `generate_models_from_db()`)"
  (reading the schema, generating models, how SurrealDB kinds become fields, what the generator
  does not do, SurrealDB 2.x and 3.x, permissions, a note for users of the full SurrealDB-ORM), a
  features row, rows in the 2.x/3.x and embedded-engine behaviour tables, the comparison and
  roadmap rows, and the example notebook `examples/v0.30.0_schema_introspection.ipynb`.

### Fixed

- **A stored function's parameter list is read in linear time** (`call_function()` with keyword
  arguments, and now introspection): a long `DEFINE FUNCTION`, or one holding `` $` `` or `$⟨`
  without a closing quote, no longer makes the reader copy or search the rest of the definition at
  every `$`.

### Notes

- **Kinds and field paths are normalised, expressions are kept as rendered**: `option<int>` (2.x)
  and `none | int` (3.x) both give `field_type == "int"` and `nullable is True`, and `tags[*]`
  (2.x) and `tags.*` (3.x) both give the field `"tags.*"`. Expressions (`default`, `value`, `raw`,
  a generated `computed("…")`) are kept as each line renders them and may differ:
  `VALUE IF b THEN 1 ELSE 2 END` on 2.x is `VALUE (IF b THEN 1 ELSE 2 END)` on 3.x.
- **Definitions only one line has** are read and typed on that line: on 3.x `COMPUTED`,
  `REFERENCE`, `FULLTEXT` indexes, the `file` kind, `WITH REFRESH` and `BEARER` accesses, and
  `DEFINE SEQUENCE` (kept raw in `schema.other`); on 2.x and the embedded engine `MTREE` and
  `SEARCH ANALYZER` indexes and `geometry<feature>`.
- **The `users` section is never read.** On SurrealDB before 3.2.2 (every 2.x release, 3.x before
  3.2.2, and the embedded engine of SDK 2.0.0), `INFO FOR DB` shows user password hashes to every
  role that may run `INFO`, per SurrealDB's `INFO` documentation; introspection never reads,
  stores, logs or writes them. Introspection needs a system user (root, a namespace or a database
  user): a record user or an anonymous session gets `SurrealDbAuthenticationError`, except on the
  embedded engine, whose anonymous session owns the database.
- **Tables not generated**, listed in the module's header and still in `schema.tables`: a table
  named like a SurrealQL keyword, in any case (`select`, `create`, `only`, …), and a table whose
  name is not an identifier, which the ORM's statements cannot use as a table name.
- **`update()` and `READONLY` datetimes**: an instance holding a `READONLY` `datetime` field cannot
  be changed with `update()`, after `get()` as after `save()`: Python keeps microseconds, SurrealDB
  nanoseconds, and the server refuses the changed value. This predates v0.30.0; the generated class
  says so: change such a record with `merge()`.
- **Sub-fields a `DEFAULT` fills**: a sub-field filled by a `DEFAULT` the model does not hold
  (`prefs.at DEFAULT time::now()`, or any `DEFAULT` under a `dict[str, Any]` field) stays `None` on
  the instance after `save()`, which reads back only `server_fields` and computed fields; a
  following `update()` sends it as `NONE`: the server refuses it when the sub-field is required and
  erases the value when it is optional. The generated class says so: call `refresh()` after
  `save()` before an `update()`, or change the record with `merge()`.
- **Defaults are copied when the models are generated**: generate them again after changing a
  `DEFAULT`. A value set on a `server_fields` field before `save()` is not written, as since
  v0.18.0 and in the full ORM: set it with `merge()` once the record exists.
- `array<T, N>` becomes `list[T]`, leaving the length to the server (SurrealDB 2.x accepts at most
  N items, 3.x exactly N); `null | T` becomes a required `T | None`, which the ORM sends as `NONE`
  and `null | T` refuses: declare such a field `option<T>`. `ASSERT`, `PERMISSIONS` and field
  comments are not generated, and `READONLY` is a comment. Never call `define_computed_fields()` on
  a generated model.
- **Differences from the full SurrealDB-ORM**: the state is immutable, with tuples where the full
  ORM has lists; permission values are `'FULL'`, `'NONE'` or `'WHERE <expr>'`; `default` is
  SurrealQL text; `nullable` is false for `null | T`; accesses are database-level
  (`schema.accesses`); a refused or failed `INFO FOR DB` raises and an unreadable definition
  becomes a `SchemaIssue`; `connection=` is a name, and a client object raises `TypeError`;
  generated models use comments for the `SurrealConfigDict` keys that arrive with v0.31.0–v0.33.0
  (`table_type`, `permissions`, `changefeed`, `view_query`, `relation_in`, `relation_out`,
  `enforced`); class names are not singularised. `schema_diff()` and `SchemaState.diff()` come with
  v0.37.0 (migrations), the `inspectdb` command with v0.38.0 (CLI).
- **Server pins unchanged**: tested against SurrealDB 3.3.4 and 2.7.0 and on the embedded engine,
  plus 3.1.5 for backward compatibility.

## [0.29.3] - 2026-10-18

`get_or_create()` and `update_or_create()` bind the queryset's `variables()` in their lookup, as
`filter()` does. No new public API.

### Fixed

- **`get_or_create()` and `update_or_create()` bind `variables()` in their lookup** (#252). The
  lookup ran without the queryset's `variables()`, so a `Var` criterion read `NONE` there:
  `variables(n=10).get_or_create(size__gte=Var("n"), …)` compared `size` with `NONE` and could
  match a record the caller did not mean, which `update_or_create()` then updated, and
  `variables(t="mine").get_or_create(title=Var("t"))` did not find the existing record. The lookup
  now binds the queryset's `variables()`, merged with those of its subquery criteria as `filter()`
  merges them (a subquery that binds one of these names to another value raises `ValueError`), on
  SurrealDB 2.x, 3.x and the embedded engine.
- **An `exact` `Var` criterion naming a variable of `variables()` is written as that variable's
  value on create** (#252), so the next identical call finds the record:
  `variables(t="mine").get_or_create(title=Var("t"))` behaves as `get_or_create(title="mine")`. On
  the record id and on an edge's `in` and `out`, it is compared as the record id its value names,
  as the same criterion given that value is (`variables(a="person:alice")` with
  `get_or_create(in_=Var("a"), out=bob)` finds or relates `person:alice`); a value that names no
  record id there raises `ValueError` before any request. A model instance given to `variables()`
  is bound and written as its record id, as the instance given as the criterion is; an unsaved one
  raises `ValueError` before any request. An `exact` `Var` that names a session parameter
  (`Var("auth")`) or no variable of `variables()` still raises `SurrealDbError` on create.

```python
from surreal_orm_lite import Var

qs = Room.objects().variables(t="mine", n=10)
room, created = await qs.get_or_create(title=Var("t"))  # as get_or_create(title="mine")
room, created = await qs.update_or_create(size__gte=Var("n"), title=Var("t"), defaults={"floor": 2})
```

### Changed

- **Refusals of a `"$name"` criterion in `get_or_create()` and `update_or_create()`** (#252): a
  string criterion that is exactly `$` followed by the name of a variable of a subquery criterion's
  `variables()` now raises `SurrealDbError` too, like one naming a variable of the queryset's
  `variables()`. For a variable, the message names `Var('x')` as the criterion to write, under any
  lookup but `like`/`ilike`, which take no `Var` (pass the pattern itself, or use `regex`). For a
  session parameter, it says to pass the actual value under `exact`, and that `Var('auth')` works
  under any other lookup but `like`/`ilike`, which take no `Var` (pass the pattern itself, or use
  `regex`): `members__contains=Var("auth")` is looked up, never written. On the `id` column it names
  the record id `table:⟨$name⟩` the string would be.

### Deprecated

- The `"$$"` escape of a string filter value, deprecated since v0.28.1 (#250) and listed there
  under Changed: a string filter value starting with `$$` loses one `$` (`"$$admin"` finds
  `"$admin"`) and emits a `DeprecationWarning`. A string filter value is always data, so write the
  value with one `$` less. An item of an `in` list never had the escape and is unchanged.

## [0.29.2] - 2026-10-18

The text lookups `startswith`, `endswith`, `like`, `ilike`, `match` and `regex` work: each compiles
to a SurrealQL string function. No new public API.

### Fixed

- **Text lookups compile to SurrealQL string functions** (#251). They compiled to infix operators
  SurrealQL does not have (`STARTSWITH`, `LIKE`, `REGEX`…), so every such filter was a parse error
  on SurrealDB 2.x, 3.x and the embedded engine, in reads and in live queries. Each now compiles to
  a string function guarded by the field's type, with its value bound:
  `filter(name__startswith="A")` is `((name).is_string() AND string::starts_with(name, $_f0))`.
  - `startswith` and `endswith`: `string::starts_with` and `string::ends_with`.
  - `match` and `regex`: `string::matches`, the regular expression (SurrealDB's syntax, the Rust
    `regex` crate) as given. `match` is a regex lookup, not the full-text `@@` operator: full-text
    search is planned for v0.34.0.
  - `like` and `ilike`: `string::matches` on the pattern translated to an anchored regular
    expression. `%` matches any run of characters (newlines included), `_` exactly one, every other
    character is literal; `ilike` ignores case. The whole value must match: `like="Al"` finds
    `"Al"`, not `"Alice"`.
  - A record whose field is missing (`NONE`), `NULL` or not a string never matches, and the query
    does not fail on it; the negation (`~Q(name__startswith="A")`) matches it. A dotted path
    through an array of record links or objects (`authors.name`) holds an array: it never matches
    either.

```python
await User.objects().filter(name__startswith="A").exec()
await User.objects().filter(email__ilike="%@EXAMPLE.com").exec()  # case-insensitive
await User.objects().filter(name__regex=r"^\d{3}").exec()
```

### Changed

- **The value of a text lookup must be a `str`** (#251), otherwise `ValueError`. A string starting
  with `$` is data, as for every lookup. `startswith`, `endswith`, `match` and `regex` also take a
  `Var`; `like` and `ilike` raise `ValueError` for one, since their pattern is translated before it
  is sent. These calls were parse errors before, so no call that worked changes.

### Notes

- Same behaviour on SurrealDB 2.x, 3.x and the embedded engine: the guard is written in the method
  form `(field).is_string()`, which parses on both lines (`type::is::string` parses on 2.x only,
  `type::is_string` on 3.x only). The path is in parentheses because SurrealDB 3.x applies
  `authors.name.is_string()` to each element of the array such a path gives, and the array of
  booleans let the array reach the string function, which failed the query. The README's behaviour
  table has a row for the text lookups.
- `surreal_orm_lite.constants.LOOKUP_OPERATORS` (internal) no longer lists the text lookups; they
  are in `TEXT_LOOKUP_FUNCTIONS`.

## [0.29.0] - 2026-10-18

Multiple databases: one process works with several databases at once. Named connections stand
beside the default one, each model reads and writes through its own, and `using()` redirects a
block of code. The seventh and last minor of Tier 2.

### Added

- **Named connections**:
  `SurrealDBConnectionManager.add_connection(name, *, url, user=None, password=None, namespace, database)`
  registers another database beside the default connection (`set_connection()`): another database
  on the same server, another server, or the embedded engine. It opens nothing: each connection opens its own clients on first use, as the default
  one does. `remove_connection(name)`, `get_config(name)` (a frozen `ConnectionConfig`, exported
  from `surreal_orm_lite`) and `list_connections()` complete the registry, with the full
  SurrealDB-ORM's names.

  ```python
  from surreal_orm_lite import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager

  SurrealDBConnectionManager.set_connection(url="mem://", namespace="shop", database="main")  # "default"
  SurrealDBConnectionManager.add_connection("analytics", url="mem://", namespace="shop", database="analytics")

  class Customer(BaseSurrealModel):  # no connection key: "default"
      id: str | None = None
      name: str

  class PageView(BaseSurrealModel):
      model_config = SurrealConfigDict(connection="analytics")
      id: str | None = None
      path: str

  await Customer(id="ada", name="Ada").save()  # shop/main
  await PageView(id="v1", path="/pricing").save()  # shop/analytics

  async with SurrealDBConnectionManager.using("analytics"):
      elsewhere = await Customer.objects().count()  # 0: Customer runs on "analytics" here
  ```

- **`add_connection()` checks its arguments** before registering anything: a non-empty name, URL,
  namespace and database, and a user and password for a server URL (optional on the embedded
  engine); a URL the installed SDK refuses raises `SurrealDbConnectionError`. Registering a name
  again with the same settings changes nothing, and `add_connection("default", …)` configures the
  default connection with these checks. `ConnectionConfig` keeps the password out of its `repr`.
  `remove_connection()` of a name that is not registered is a no-op, and
  `remove_connection("default")` is `unset_connection()`.
- **Model routing**: `SurrealConfigDict(connection="analytics")` sends every database call of the
  model to that connection: writes, reads and every QuerySet, relations, `raw_query()`,
  `call_function()`, model auth, live queries, change feeds and `as_of()`. A model without the key
  uses the default connection. `Model.get_connection_name()` returns the connection its calls use
  right now. The key is checked when the class is defined (`TypeError` for a value that is not a
  string, `ValueError` for an empty one), and whether its name is registered at the model's first
  database call (`ValueError`), so models can be declared before `add_connection()` runs. Edge
  models and `AuthenticatedUserMixin` models take it too; subclasses inherit it.
- **`using(name)`**: inside `async with SurrealDBConnectionManager.using("analytics"):` every model,
  whatever its key, and every manager method that talks to a database use that connection. It is
  async-safe (a `ContextVar`: a task created inside the block keeps its connection), blocks nest,
  and the outer connection is back after the block, on an exception too; a name that is not
  registered raises `ValueError` before the block runs. `get_active_connection_name()` returns the
  block's name, `None` outside any block.
- **`name=` on `get_client()`, `close_connection()` and `transaction()`**: they act on a named
  connection, and without it on the current one. `inspect.signature()` does not show the
  parameter, so `Depends(SurrealDBConnectionManager.get_client)` in FastAPI adds no `?name=` query
  parameter and behaves as in v0.28: a request never chooses the connection.
- **Each connection keeps its own state**: its clients (one per event loop; one for the whole
  process on the embedded engine), its session (`signin()` inside `using(name)` signs in that
  connection only, and a stored token is replayed on its own connection only), its live streams
  and change feeds (bound to the connection when they start), its transactions, its
  stored-function signatures and its cached reads.
- **`embedded_database(connection=name)`** isolates a named connection in tests; inside
  `using(name)` it isolates the block's connection, and naming another connection there raises
  `ValueError`.
- **Errors for operations across connections**, raised before any request: `SurrealDbError` for a
  model given another connection's transaction (its writes, `objects(tx=)`, `refresh(tx=)`,
  `call_function(tx=)`), for a subquery whose model runs on another connection than the statement
  around it, for `relate()`, `remove_relation()`, `remove_all_relations()` and `get_related()`
  across connections, and for following a link (`fetch()`, a dotted path) through a field typed
  with a model of another connection; `SurrealDbConnectionError` (not retryable) when a second
  connection opens an on-disk embedded store (`surrealkv://`, `file://`) that another connection
  of the process holds open. Inside a `using()` block every model runs on one connection, so none
  of them apply there.
- README section 30, "Multiple databases (`add_connection()`, `using()`)" (routing a model,
  `using()`, transactions and other operations across connections, what each connection keeps for
  itself, which connection a manager method uses, a note for users of the full SurrealDB-ORM), a
  features row, rows in the 2.x/3.x and embedded-engine behaviour tables, the comparison and
  roadmap rows, and the example notebook `examples/v0.29.0_multi_database.ipynb`.

### Changed

- `close_connection()` ends the live streams of its own connection only (before: every live
  stream of the event loop), and `unset_connection()` closes the default connection's clients only
  (`close_all_connections()` still closes every client, of every connection). With the default
  connection alone, both behave as before.
- A `transaction()` records the connection it was opened on: a model of another connection is
  refused (above), and a transaction whose connection is registered again with other settings, or
  removed, while it is open is cancelled when its block ends, with nothing committed
  (`SurrealDbConnectionError`: open it again).
- The manager's state now lives in one class per connection, the default one included: code that
  read or patched the private attributes of `SurrealDBConnectionManager` must follow it. A manager
  method replaced by hand, public or private, is inherited by every connection: put it back from
  `vars(SurrealDBConnectionManager)[name]`, as pytest's `monkeypatch.setattr()` and
  `unittest.mock.patch.object()` already do. Putting back `SurrealDBConnectionManager.<name>`, saved
  before the patch, worked with one connection; it is a method bound to `SurrealDBConnectionManager`
  that every connection then inherits and that acts on the current connection, so once a named
  connection exists, a model with a `connection` key reads and writes the current connection's
  database, with no error (README section 30, "Which connection a manager method uses").
- CI adds a "Mixed server lines" job, required by `CI Success`: the mixed-lines tests put a
  SurrealDB 3.x and a 2.x connection side by side in one process; they find the servers through
  `SURREALDB_PORT_3X` and `SURREALDB_PORT_2X` and skip without them.

### Notes

- **Each connection decides on its own server line**: a SurrealDB 2.x and a 3.x server work side
  by side in one process, with interactive transactions on a 3.x WebSocket connection beside
  buffered ones on a 2.x connection; time travel, change feeds and live queries follow each
  connection's server.
- **Registering a name again with other settings, or removing it**, retires its clients and ends
  what the ORM holds on the old settings: a client still opening is closed unused (a retryable
  `SurrealDbConnectionError`: retry the call), live streams end, a change feed ends at its next
  read (`SurrealDbConnectionError`: start a new one), and an open `transaction()` is cancelled
  (above). A client the application obtained with `get_client()` before the change is its own: on
  WebSocket its next request fails; an HTTP client keeps the namespace and database it was opened
  on; an embedded client follows a new namespace or database, but after a new URL or a removal it
  is the previous engine, so stop using it. `set_connection()` and the `set_*()` setters keep
  their earlier behaviour: clients opened on the previous settings keep serving.
- **Query cache**: an ORM write removes the cached reads of its table on every connection, more
  than needed but never a stale result. Entries cached with `QueryCache.make_key()` and `set()`
  are not scoped to a connection: when the same statement runs on two connections, put the
  connection's name among the variables.
- **What the ORM cannot see across connections**, so keep it on one connection yourself: a link
  followed through a field whose annotation names no model (`RecordID`, `str`, `dict`), graph
  arrows (`select("->likes->Post")`, `traverse()`), an edge saved with endpoint ids of another
  database, and raw SurrealQL (`raw_query()`, `QuerySet.query()`, `call_function()`,
  `Transaction.add()`).
- **SurrealDB runs a statement, and a transaction, in one database**: two `transaction()` blocks on
  two connections are two independent transactions, and a `USE` inside `raw_query()` applies to
  that request only (the next request reads the connection's own database).
- **Differences from the full SurrealDB-ORM**: `close_connection()` without a name closes the
  current connection only (full ORM: every connection); `list_connections()` lists `'default'`
  first; registering a name again with other settings closes its clients; the getters, the
  setters and `reconnect()` follow `using()`; `add_connection()` and the `connection` key are
  checked; `ConnectionConfig` has no `protocol` (the official SDK speaks CBOR only) and accepts
  `None` for `user` and `password`; `name` is hidden from introspection; `transaction(name)`,
  `embedded_database(connection=)` and the errors for operations across connections are lite
  additions.
- Not available yet: a per-queryset override (`QuerySet.using()`), a client-side connection pool,
  one transaction across connections.
- **Server pins unchanged**: tested against SurrealDB 3.3.4 and 2.7.0, each alone and both in one
  process, and on the embedded engine, plus 3.1.5 for backward compatibility (identical).

## [0.28.1] - 2026-10-17

Query building checks what it is given: `select()` accepts field names and paths, `limit()` and
`offset()` take a non-negative integer, and a string filter value is always data, with `Var()` as
the only variable reference. No new public API.

### Changed

- **A string filter value is data, whatever its first character** (#250), on every filter path:
  `filter()`, `Q`, `get()`, `first()`, `count()`, `exists()`, `get_or_create()`,
  `update_or_create()`, `bulk_update()` by filter, `cache()`, subqueries and live queries.
  `filter(name="$admin")` compares `name` with the string `"$admin"`, bound like any other value.
  `Var("name")` is the only reference to a query variable, whose value `variables()` gives.
  Migration: where a filter uses the `"$name"` form (deprecated since v0.14.3), write
  `Var("name")`. When a string filter value is exactly `$` followed by the name of a variable of
  `variables()` or of a session parameter (`$access`, `$auth`, `$session`, `$token`), a
  `UserWarning` names the `Var(...)` to write, once per read, pointing at the caller's line; the
  value stays data. Any other string, such as `"$x"` with no variable `x`, is data and nothing
  warns.

  ```python
  from surreal_orm_lite import Var

  await User.objects().filter(name="$admin").exec()  # the string "$admin"
  await User.objects().filter(age__gte=Var("min_age")).variables(min_age=21).exec()  # $min_age
  await Doc.objects().filter(owner=Var("auth")).exec()  # SurrealDB's $auth
  ```

- **The `"$$"` escape keeps its meaning and is deprecated** (#250): a string filter value starting
  with `$$` loses one `$` (`"$$admin"` finds `"$admin"`) and emits a `DeprecationWarning`; write
  the value with one `$` less. An item of an `in` list never had the escape and is unchanged.
- **`variables()` refuses `access`, `auth`, `session` and `token`** with a `ValueError` (#250):
  SurrealDB sets these parameters for each session and refuses a query variable of that name. To
  pass your own value, give it another name or put the value itself in the filter:
  `filter(token=Var("device_token")).variables(device_token=t)` or `filter(token=t)`.
  `Var("token")` without `variables()` reads SurrealDB's `$token`, and likewise for the three
  others, in reads and in live queries.
- **`get_or_create()` and `update_or_create()`** raise `SurrealDbError` before any request when a
  scalar string criterion is exactly `$` followed by the name of a variable of `variables()` or of
  a session parameter, whatever its lookup (#250): pass the actual value as the criterion (the
  record id, or the value itself). Any other `"$name"` string is looked up and written as it is.
  `Var("auth")` finds the signed-in user's record but cannot be written: with no record to return,
  it raises `SurrealDbError`.
- **`select()` checks its items** like `order_by()` and `values()` (#247): each item is `"*"`, a
  field name (an aliased field by its Python name), a dotted path into a nested object or through a
  record link, or such a path followed by `.*` (`"author.*"`). Anything else raises `ValueError`
  naming the item when `select()` is called, before any request, and the selection in place is
  kept; an item that is not a `str` raises `TypeError`. Write a computed projection with `query()`
  and bound parameters.
- **`limit()` and `offset()` take a non-negative integer** (#249) of any type with `__index__`
  (an `IntEnum` member, NumPy's `int64`; the parameter is annotated `SupportsIndex`), up to
  `2**63 - 1`. A `bool`, `str`, `float` or `None` raises `TypeError`, a negative value or one above
  `2**63 - 1` `ValueError`, naming the method and the value, when the method is called (for a
  subquery's `limit()` and `offset()` too), before any request; the value in place is kept.
  SurrealDB 2.x and the embedded engine refuse a value above `2**32 - 1` when the read runs;
  SurrealDB 3.x reads it.
- **Live queries**: `Var("scope")` takes its value from `variables(scope=...)` like any other name
  and raises `SurrealDbError` without it (#250). `$access`, `$auth`, `$session`, `$token`, `$this`
  and `$parent` stay references the server reads on every notification.

### Fixed

- On an edge's `in` and `out`, which hold record ids, a string that is `$` followed by the name of
  a variable of `variables()` or of a session parameter raises a `ValueError` whose message names
  the `Var(...)` to write (#250).
- Each warning of a filter value is emitted once per read: the second compilation of a `cache()`
  miss and the re-send of a subquery over a missing table stay quiet, for the `"$$"`
  `DeprecationWarning` too (#250).

### Removed

- `utils.remove_quotes_for_variables()` and `utils.VALID_VARIABLE_REF_PATTERN`, internal helpers
  that were never in `__all__`.

### Notes

- **SurrealDB 2.x vs 3.x**: string filter values and `Var()` behave the same on both lines and on
  the embedded engine. `select("meta.*")` on a nested object that is not a record link gives the
  object on 3.x and its values as a list on 2.x and the embedded engine (through a link, the linked
  record on both). A `limit()` or `offset()` above `2**32 - 1` is refused by SurrealDB 2.x and the
  embedded engine when the read runs, and read by 3.x.
- **Docs**: the README section "String filter values and `Var()`" replaces "Filter values that
  start with `$`"; new parts "What `select()` accepts" and "`limit()` and `offset()`"; the
  live-query, subquery and query-cache invalidation notes match; three rows in the 2.x/3.x behaviour
  table (string filter values, a nested object's `.*`, `LIMIT`/`START` above `2**32 - 1`). The
  `getting_started` notebook reads `SURREALDB_HOST` and `SURREALDB_PORT`; the v0.14.3 notebook says
  the `"$$"` escape is deprecated since v0.28.1.
- **Server pins unchanged**: tested against SurrealDB 3.3.4 and 2.7.0, and on the embedded engine,
  plus 3.1.5 for backward compatibility (identical).

## [0.28.0] - 2026-10-17

Query cache: a QuerySet serves its reads from a client-side cache with `cache(ttl)`, and every ORM
write removes the cached reads it can change, with nothing to set up. The sixth minor of Tier 2.

### Added

- **`QuerySet.cache(ttl=None)`** and **`QueryCache`**, exported from `surreal_orm_lite` (the full
  SurrealDB-ORM's names). The rows a cached read returned stay in this process's memory, and the
  same read is answered from there, without a request, until its TTL runs out or an ORM write
  changes what it reads.

  ```python
  from surreal_orm_lite import QueryCache

  books = await Product.objects().filter(category="book").cache(ttl=30).exec()  # the database answers
  again = await Product.objects().filter(category="book").cache(ttl=30).exec()  # the cache answers
  await Product(id="p4", name="Atlas", category="book", price=40).save()  # removes Product's cached reads
  fresh = await Product.objects().filter(category="book").cache(ttl=30).exec()  # the database answers
  QueryCache.stats()["hits"]  # 1
  ```

- **Every read of a cached queryset**: `exec()`, `first()`, `get()` by filters or by id, `all()`,
  `count()`, `sum()`, `avg()`, `min()`, `max()`, `exists()`, `values(…).annotate(…)` and
  `annotate(GeoDistance)`. A read is a hit when an earlier read sent the same statement with the
  same variables, on the same connection and with the same identity, within the TTL. Rows are
  cached, not instances: each hit builds new instances from a copy. Empty results are cached
  (`get(<id>)` of a missing record raises `SurrealDbNotFoundError` on the hit too); errors never
  are. An internal failure of the cache never fails a read: the read goes to the database.
- **Time to live**: seconds (`int` or `float`) or a `timedelta`, the oldest result the read
  accepts; `cache()` without a TTL uses `QueryCache`'s default (60 s unless configured); `0` means
  never cached.
- **Built-in invalidation**: every ORM write removes the cached reads of the tables it writes,
  before its `post_*` signals fire — `save()`, `update()`, `merge()`, `upsert()`, `patch()`,
  `delete()`, the atomic operations, `relate()` and the `remove_*()` helpers, a typed edge's
  `save()`, `bulk_create()`, `bulk_update()`, `bulk_delete()`, `QuerySet.patch()`,
  `delete_table()`, `get_or_create()`/`update_or_create()` when they write, `signup()`,
  `define_computed_fields()` and `define_access()`. Reads whose subqueries read those tables go
  too; a read that follows links (`fetch()`, a dotted path, a `select()` item that is not a plain
  field) is removed by any ORM write, and a read of an edge model by any delete. Inside
  `transaction()` the written tables are invalidated at commit (a failed commit too, nothing on
  rollback). While a write runs, reads of its tables go to the database and store nothing.
- **One cache scope per connection and identity**: a sign-in, a sign-up, `authenticate()` or
  `invalidate()` starts a new scope and drops the previous identity's results; another event loop's
  connection, a reconnect and an `embedded_database()` block start empty; closing a connection
  drops its results. Cached results never cross identities.
- **`QueryCache`**: `configure(default_ttl=60, max_size=1000, enabled=True)` sets all three
  settings at every call, as in the full ORM, and never clears; `enabled=False` hides the entries
  (`cache()` reads go to the database and calling `cache()` logs a warning). Past `max_size` the
  least recently used entry is evicted. `invalidate()` takes a model class or a table name and
  returns how many entries it removed; `clear()` drops every entry and resets the counters;
  `stats()` returns `entries`, `tables`, `max_size`, `default_ttl`, `enabled`, `hits` and
  `misses`. `make_key()`, `get()` and `set()` cache your own results (of `raw_query()`, say),
  tagged with a table so that an ORM write to it removes them; `get()` misses while such a write
  runs.
- **Refusals**: `query()` raises `SurrealDbError` on a queryset with `cache()`; `live()`, `watch()`
  and `changes()` name `cache()` among the clauses their usual error refuses; combining `cache()`
  with `as_of()` raises `SurrealDbError`, in either order. A negative or non-finite TTL raises
  `ValueError`, anything but a number or a `timedelta` (`True` included) `TypeError`.
- README section 29, "Query cache (`cache()`, `QueryCache`)" (what is cached, the time to live,
  invalidation and what the cache cannot see, one cache per connection and identity,
  configuration and statistics, a note for users of the full SurrealDB-ORM), a features row, a
  row in the 2.x/3.x behaviour table, the comparison and roadmap rows, and the example notebook
  `examples/v0.28.0_query_cache.ipynb`.

### Changed

- ORM writes now also remove the cached reads they can change, in memory, without a request.
  Without `cache()`, every read and write sends the same requests as in v0.27.x; the texts of the
  `live()`, `watch()` and `changes()` refusals are unchanged.

### Notes

- **Same on SurrealDB 2.x and 3.x**, and on the embedded engine. Reads inside a transaction
  (`objects(tx=)`) are never cached: on 3.x an interactive transaction's reads see its uncommitted
  writes, and on 2.x a buffered transaction cannot read.
- **What the cache cannot see** (the TTL bounds it; `QueryCache.invalidate(Model)` removes a
  table's entries after such a change): writes by another process or client; raw SurrealQL
  (`raw_query()`, `QuerySet.query()`, `call_function()`, `Transaction.add()`); a write whose task
  is cancelled, or that fails client-side, after its request was sent; writes made by an access
  method's own `SIGNIN`/`SIGNUP` clauses beyond the record they create; what the server derives on
  its own (`DEFINE EVENT`, `REFERENCE … ON DELETE` on 3.x, views, `PERMISSIONS` that read another
  table); values computed when the read runs (a `DEFINE PARAM` read with `Var()`,
  `<future>`/`COMPUTED` fields, time functions in a `select()` expression); a table of relations
  read through a model that is not a `BaseSurrealEdge`. A session changed behind the ORM's back
  (`raw_query("USE …")`, `signin()` or `use()` on the SDK client) is not seen either: call
  `QueryCache.clear()` after it.
- **Memory**: cached rows stay in this process's memory until they expire, are evicted,
  invalidated or cleared, or their connection closes. `configure(enabled=False)` only hides them;
  `QueryCache.clear()` drops them. Entries stored with `QueryCache.set()` are not scoped to a
  connection or an identity: put whatever tells your callers apart in the key.
- **Keys** are the SHA-256 of the CBOR bytes the official SDK sends, with the keys of mappings
  sorted: `RecordID("user", 1)` and `"user:1"` never share an entry. An SDK that exposes no CBOR
  encoder sends every `cache()` read to the database (one warning per process).
- Concurrent misses of the same read each send their own request.
- **Differences from the full SurrealDB-ORM**: every read is cached, not only `exec()`, `first()`
  and `get()` by filters; invalidation is built in and covers bulk operations, `QuerySet.patch()`,
  the atomic operations, relations and transactions, where the full ORM listens to `post_save`,
  `post_update` and `post_delete`; rows are cached instead of instances; one scope per connection
  and identity; `live()`, `watch()`, `changes()` and `query()` refuse `cache()`; the TTL may be a
  `float` or a `timedelta` and negative values are refused; LRU eviction instead of FIFO;
  `invalidate()` takes a table name too; `stats()` adds `hits` and `misses`.
- Not available yet: invalidation from other processes or clients (through live queries),
  caching `as_of()` reads, sharing cached results between event loops.
- **Server pins unchanged**: tested against SurrealDB 3.3.4 and 2.7.0, and on the embedded engine,
  plus 3.1.5 for backward compatibility (identical).

## [0.27.0] - 2026-10-16

Subqueries: a QuerySet filters another, as `Subquery(qs)` or the queryset itself, compiled to a
sub-`SELECT` the server runs inside the outer statement: one request, one consistent read, every
inner value bound. The fifth minor of Tier 2.

### Added

- **`Subquery(qs)`**, exported from `surreal_orm_lite` (the full SurrealDB-ORM's name), and a
  QuerySet given directly as a filter value, in `filter()` and in `Q` (`&`, `|`, `~`, any nesting).

  ```python
  from surreal_orm_lite import Q, Subquery

  active = Person.objects().filter(active=True)
  posts = await Post.objects().filter(author__in=Subquery(active)).exec()
  same = await Post.objects().filter(author__in=active).exec()  # a QuerySet is a subquery

  best = Post.objects().select("score").order_by("-score").limit(1)
  top = await Post.objects().filter(score=Subquery(best)).exec()  # compared with its first value

  inactive = Person.objects().filter(active=False)
  mixed = await Post.objects().filter(Q(author__in=inactive) | Q(score__gte=9)).exec()
  ```

- **Lookups.** `in`, `not_in`, `containsall` and `containsany` compare the field with all the
  subquery's values; `exact` (no suffix), `gt`, `gte`, `lt`, `lte`, `contains` and `not_contains`
  with its first value (order the subquery to choose it). A first-value lookup on an empty
  subquery matches no record, and `~Q(...)` of it every record; for `in`, an empty subquery is an
  empty list.
- **What the inner queryset selects**: its record ids without `select()` (`SELECT VALUE id`), or
  the values of the one field given to `select()` (an aliased field compiles its column). Its
  `filter()`, `Q`, `nearby()`, `order_by()`, `limit()`, `offset()` and `variables()` apply; its
  `fetch()` is ignored, as in the full ORM. Values are compared as stored, never converted: to
  match records, select `id` or a record link.
- **Where it applies**: `exec()`, `first()`, `get()` with filters, `exists()`, `count()`, `sum()`,
  `avg()`, `min()`, `max()`, `values(…).annotate(…)` and `annotate(GeoDistance)`; the writes by
  filter `bulk_update()`, `bulk_delete()` and `QuerySet.patch()`; the lookups of `get_or_create()`
  and `update_or_create()`. Subqueries nest. In `Model.objects(tx=tx)` the statement runs in the
  transaction, and an interactive transaction's subquery sees its uncommitted writes. Under
  `as_of()` every subquery reads at the same moment. `live()` and `watch()` write the sub-SELECT
  inline, and the server evaluates it again on every change. `changes()` refuses `filter()`, so
  subqueries too.
- **Refusals before any request** (`live()` and `watch()` at the call). `ValueError`: a lookup
  that takes no subquery (`isnull`, `like`, `ilike`, `startswith`, `endswith`, `match`, `regex`,
  `inside`, `outside`, `intersects`), a queryset that is its own subquery, an inner `values()`,
  `annotate()` or `select()` of several fields, an inner `limit()` or `offset()` that is not an
  `int >= 0`, an inner `order_by()` on `_sqv` it does not select, an inner `variables()` binding a
  name of the statement to another value, a subquery inside a list, `Q(...).to_sql()` called on its
  own with a subquery, and `bulk_update(score=Subquery(qs))`. `SurrealDbError`: an inner `as_of()`
  (call it on the outer queryset), an inner `objects(tx=)` whose transaction is not the outer
  one's, and a subquery as an `exact` criterion or a `defaults=` value of `get_or_create()` or
  `update_or_create()`. `TypeError`: `Subquery(x)` for anything but a QuerySet.
- README section 28, "Subqueries (`Subquery`)" (what a subquery selects, the lookups, ordered and
  limited subqueries, where subqueries apply, what they cannot do, a note for users of the full
  SurrealDB-ORM), a bullet in the supported lookups, a `Q` example in section 5, four rows in the
  2.x/3.x behaviour table, and the example notebook `examples/v0.27.0_subqueries.ipynb`.

### Changed

- A QuerySet given as a filter value is now a subquery. v0.26.x refused it: `in` and `not_in`
  raised `ValueError` ("requires an iterable") and the other lookups failed when the SDK encoded
  the request. Every call that worked in v0.26.x behaves the same.
- `Q.to_sql()` gains a keyword-only `subqueries` parameter (default `None`), the compiler of the
  statement the QuerySet sends; a `Q` compiled on its own refuses a subquery lookup.

### Notes

- **One statement per request, a block of `LET`s**:
  `{ LET $_sq0 = (SELECT VALUE id FROM Person WHERE active = $_f0); (SELECT * FROM Post WHERE author IN $_sq0) };`.
  Each subquery is bound once, innermost first, because SurrealDB 3.2 and later can return wrong
  rows for an inline sub-SELECT with `ORDER BY` and `LIMIT`
  ([surrealdb/surrealdb#7470](https://github.com/surrealdb/surrealdb/issues/7470)), and because the
  official SDK's `query()` returns the first statement's result only, so the full ORM's `LET`
  prelude (two statements) does not fit. An ordered subquery selects its column as `_sqv` with its
  order fields: SurrealDB 2.x refuses to order a one-column sub-SELECT by another field. `$_sq0`,
  `$_sq1`, … are reserved in such a statement and hide a `variables()` value of the same name.
- **A first-value lookup on an empty subquery matches no record**: SurrealDB alone would compare
  with `array::first([])`, which is `NONE`, and `score > NONE` holds for every record, so the ORM
  guards the comparison. The full SurrealDB-ORM matches every record there.
- **A subquery table that does not exist reads as empty**, like any missing table. SurrealDB 2.x
  and the embedded engine read it so in the same request. On SurrealDB 3.x the ORM asks the server
  whether the table exists and, once it confirms the table does not, sends the statement again
  with that subquery empty: three requests, and only then. Any other error, even one quoting
  "does not exist", is relayed. On 3.x a write by filter in a transaction fails with the server's
  error, and `live()` raises `SurrealDbNotFoundError` before it starts (3.x would accept the live
  query and never notify).
- **Transactions**: a buffered transaction (2.x, or HTTP) still cannot read, and applies writes by
  filter at commit. Build the inner queryset with the same `objects(tx=)`, or with none.
- **Time travel**: without versioned storage, a subquery read under `as_of()` raises v0.26.0's
  `SurrealDbError`; SurrealDB 3.1.5 raises the server's own `ThrownError` instead.
- Not available yet: `annotate(x=Subquery(...))`, correlated subqueries (`OuterRef`, `$parent`)
  and `Exists`.
- **Server pins unchanged**: tested against SurrealDB 3.3.4 and 2.7.0, in memory and on versioned
  storage, and on the embedded engine, plus 3.1.5 for backward compatibility (identical except the
  time-travel error above).

## [0.26.0] - 2026-10-16

Time travel: a QuerySet reads a model's records as they were at a past moment, with
`as_of()` (SurrealQL's `VERSION` clause), on the official SDK's embedded
`surrealkv+versioned://` engine and on SurrealDB servers started with versioned storage. The
fourth minor of Tier 2.

### Added

- **`QuerySet.as_of(at)`** adds a moment to every read of the queryset. Records updated since
  read as they were, a record deleted since is still found, and one created later is not. The
  store must keep history: the embedded `surrealkv+versioned://<path>` (no server), SurrealDB 3.x
  started on `surrealkv://<path>?versioned=true` or `rocksdb://<path>?versioned=true`, or
  SurrealDB 2.x started on `surrealkv+versioned://<path>`.

  ```python
  from surreal_orm_lite import BaseSurrealModel, SurrealDBConnectionManager

  class Product(BaseSurrealModel):
      id: str | None = None
      name: str
      price: float

  SurrealDBConnectionManager.set_connection(
      url=f"surrealkv+versioned://{path}/shop", namespace="shop", database="shop"
  )
  client = await SurrealDBConnectionManager.get_client()

  lamp = Product(id="lamp", name="Lamp", price=40.0)
  await lamp.save()
  opening = await client.query("RETURN time::now();")  # a moment, from the database's clock
  await lamp.merge(price=30.0)

  assert (await Product.objects().as_of(opening).get("lamp")).price == 40.0
  assert (await Product.objects().get("lamp")).price == 30.0
  ```

- **The moment** is an aware `datetime` (any time zone) or a naive one (UTC), an ISO-8601 string
  (parsed by the ORM; a date alone is midnight UTC), a `timedelta` or the ORM's `Duration` (that
  long before the database's clock), a `SurrealFunc` the database evaluates
  (`SurrealFunc("time::now() - 1h")`), or `None` (the current state again); called again, the last
  moment wins. The moment is always a bound parameter. A moment outside the instants SurrealDB
  versions can address (1970-01-01T00:00:00Z to 2262-04-11T23:47:16.854775Z) is refused on every
  line: `ValueError` at the call, or `SurrealDbError` before the read for a duration or an
  expression the database resolved there.
- **Every read honours it**, with `filter()`, `Q`, `nearby()`, `order_by()`, `limit()`, `offset()`,
  `select()` and `variables()`, on edge models too: `exec()`, `first()`, `get()` (with filters or by
  id), `all()`, `exists()`, `count()`, `sum()`, `avg()`, `min()`, `max()`, `values(…).annotate(…)`
  and `annotate(d=GeoDistance(…))`.
- **Snapshots.** The instances a time-travel read returns are read-only: `save()`, `update()`,
  `merge()`, `patch()`, `upsert()`, `delete()`, the `atomic_*()` methods, `relate()`,
  `remove_relation()` and `remove_all_relations()` raise `SurrealDbError` before any signal or
  request, and `bulk_create()` refuses a snapshot in its list. `refresh()` turns a snapshot back
  into the current record; `Product(**snapshot.model_dump()).update()` (or `.save()` for a record
  deleted since, or `.upsert()`) writes a past state back. `model_copy()` and `pickle` keep the
  snapshot, and a snapshot is never `==` the current instance (compare `model_dump()`s). A record
  model nested in a snapshot's fields is a snapshot too, such as a link `fetch()` resolves into a
  model-typed field on SurrealDB 3.x; its own `refresh()` makes it the current record.
- **Refusals before any request.** On a queryset with `as_of()`: `bulk_create()`, `bulk_update()`,
  `bulk_delete()`, `patch()`, `update_or_create()`, `get_or_create()`, `delete_table()`, `query()`,
  `live()`, `watch()` and `changes()` raise `SurrealDbError`; `Model.objects(tx=...).as_of(...)`
  raises at the call (history cannot join a transaction); on the embedded engine, a URL that keeps
  no history (`mem://`, `memory://`, `surrealkv://`, `file://`) raises, naming
  `surrealkv+versioned://`. A 3.x server without versioned storage refuses the read, raised as
  `SurrealDbError` with the SDK's error as its `__cause__`.
- README section 27, "Time travel (`as_of()`)" (stores that keep history, the moment, reads,
  snapshots, refusals, SurrealDB 2.x without versioned storage, record links, writing history with
  a raw `CREATE … VERSION`, a note for users of the full SurrealDB-ORM, which has no time travel),
  new rows in both behaviour tables, and the example notebook `examples/v0.26.0_time_travel.ipynb`.

### Changed

- `BaseSurrealModel` declares a private attribute, the snapshot marker, so every instance now has
  a `__pydantic_private__` dict. Two current instances compare as before; an instance pickled by
  v0.25.x unpickles as a current record, and a model's own private attributes are kept.
- `refresh()` also clears the snapshot marker of an instance read with `as_of()`.
- CI starts a second SurrealDB per job, on versioned storage, for the time-travel tests; they find
  it through `SURREALDB_VERSIONED_PORT` and skip without it.

### Notes

- **Every time-travel table read also reads each record by its id at the moment**, because a plain
  `SELECT … VERSION` is wrong on both server lines: SurrealDB 3.x's table reads return the records
  deleted before the moment, in their last state, and SurrealDB 2.x and the embedded engine
  miscount an unfiltered `count()`. It costs about one extra point read per record scanned; a read
  by id needs no check.
- **SurrealDB 2.x without versioned storage answers without an error**: in memory or on plain
  SurrealKV, it returns only the records last written at or before the moment, in their current
  state, and the ORM cannot tell which storage the server runs. Start 2.x with
  `surrealkv+versioned://` for time travel. SurrealDB 2.x on RocksDB refuses `VERSION`
  (`SurrealDbError`).
- **Record links**: `fetch()` resolves links as they were on SurrealDB 3.x, into read-only
  snapshots. On SurrealDB 2.x (servers and the SDK 2.x embedded engine) they resolve to their
  current state, so `exec()`, `first()` and `get()` without an id refuse `fetch()` under
  `as_of()`; a dotted path through a link reads the current value there.
- **Writing history at a past moment** is left to raw queries (`CREATE … VERSION d'…'`, one
  statement per `client.query()` call): stored at that moment and readable with `as_of()` on
  SurrealDB 2.x and the embedded engine; accepted but not readable at that moment on SurrealDB 3.x.
- The surrealdb SDK 2.x embedded engine ignores `?versioned=true` on `surrealkv://`: use
  `surrealkv+versioned://`. The SDK's own `surrealdb.Duration` is accepted at runtime only (the
  annotation names the ORM's types). `_as_of` is a reserved `variables()` name on a time-travel
  read: the moment is bound under it.
- A `SurrealFunc` moment is sent as written: build it in your code, never from user input, and pass
  user values through `variables()`, bound.
- **Server pins unchanged**: tested against SurrealDB 3.3.4 and 2.7.0, in memory and on versioned
  storage, plus 3.1.5 for backward compatibility (identical); the embedded tests run in every CI
  job. Every existing call behaves as in v0.25.x.

## [0.25.0] - 2026-10-15

Embedded test engine: the ORM also runs on the official SDK's in-process SurrealDB engine
(`mem://`, `surrealkv://<path>`), with no server and no Docker, and the new
`surreal_orm_lite.testing` module adds `embedded_database()` and the full SurrealDB-ORM's
`SurrealFixture` / `@fixture`. The same models and queries run unchanged against a server. The
third minor of Tier 2.

### Added

- **Embedded URLs.** With
  `SurrealDBConnectionManager.set_connection(url="mem://", namespace=…, database=…)`, every ORM
  call runs in-process: CRUD, QuerySet, aggregations, relations, typed edges, rich and geo fields,
  upsert and patch, server values, computed fields, stored functions, buffered transactions,
  change feeds and authentication. The SDK sends `mem://`, `memory://`,
  `surrealkv://<path>`, `surrealkv+versioned://<path>` and `file://<path>` (a SurrealKV store) to
  its engine; the ORM asks the SDK's own URL parser and keeps no list of schemes. `user` and
  `password` are optional on an embedded URL and never sent: the engine's anonymous session is the
  in-process owner. They are kept, so one configuration switches between a server and `mem://` by
  its URL alone:

  ```python
  from surreal_orm_lite import BaseSurrealModel, SurrealDBConnectionManager

  SurrealDBConnectionManager.set_connection(url="mem://", namespace="app", database="app")

  class Note(BaseSurrealModel):
      id: str | None = None
      title: str

  await Note(id="docs", title="Write the docs").save()
  assert await Note.objects().count() == 1
  assert SurrealDBConnectionManager.is_embedded()
  ```

- **`SurrealDBConnectionManager.is_embedded()`**: whether the configured URL addresses the
  in-process engine, for code that must run on both kinds of connection.
- **One engine per process.** The manager opens the engine on the first call and hands it to every
  event loop and thread, so they all see the same database; concurrent first calls open exactly
  one. A namespace or database change resets the shared session and selects the new pair on the
  same engine, with no data lost; a new URL closes the old engine. Closing
  (`close_connection()`, `close_all_connections()`, `unset_connection()`, `reconnect()`, the exit
  of `async with SurrealDBConnectionManager():`) discards a `mem://` database, and a
  `surrealkv://` one reopens with its data. `validate_connection()` never discards it. Server
  clients are untouched while an embedded URL is configured: `close_connection()`, `reconnect()`
  and the `async with` exit then close only the engine, so a server client keeps its live queries
  and its identity, even one signed in with `store=False`, and `is_connected()` stays true on its
  event loop; `close_all_connections()` and `unset_connection()` close it too. An event loop whose
  server connection dropped while signed in with `store=False` still raises once the server URL is
  set back with `set_url()`, or after an `embedded_database()` block: neither closing the engine
  nor an identity set on it ends that state. `set_connection()` does: a new configuration is a
  choice of identity.
- **Clear errors, before any request, for what the engine cannot do**: live queries (`live()`,
  `watch()`, `subscribe_live()`: read the table's change feed with `changes()` instead),
  `changes(since=<datetime>)` (pass a cursor or `since=None`), and `AuthenticatedUserMixin` calls
  with `bind=False` and `ephemeral_client()` (the engine has a single session per database). Each
  raises `SurrealDbError`.
- **Authentication on the engine.** `signin()`, `signup()`, `authenticate()` and `invalidate()`
  work with the engine's record access methods and `DEFINE USER`s, and record users and table
  `PERMISSIONS` apply as on a server. A sign-in changes the session of the whole process (every
  event loop and thread); `invalidate()` and a namespace or database change return it to the
  in-process owner and end the stored session. Model auth with `bind=True` signs that session in,
  one call at a time with the connection-level calls (the last one wins), and a failed call puts
  the previous session back, with one exception, as on a server: when `signup()` or `signin()`
  succeeded but the record does not fit the model, the session stays signed in as that record
  (the error says so). `refresh()` cannot succeed on the engine: its SurrealQL does not parse
  `WITH REFRESH`.
- **`surreal_orm_lite.testing`**, a new module that does not import pytest:
  - `embedded_database(url="mem://", *, namespace="test", database="test")` runs a block against
    an isolated in-process database, yields the block's SDK client (valid inside the block only),
    then puts the previous connection back: its settings, stored session and engine. Blocks nest;
    `surrealkv://<path>` gives an on-disk database. A server URL raises `ValueError`.
  - `SurrealFixture` and `@fixture`, with the full SurrealDB-ORM's names: `load()` saves a fresh
    copy of each declared instance in declaration order, yields them typed as the fixture class,
    and deletes them on exit. It works on any connection, a server or the engine. Unlike the full
    ORM, an explicit `id` is kept, a save that fails or is cancelled mid-load deletes the copies
    already saved, `@fixture` raises `TypeError` on a class that is not a `SurrealFixture`
    subclass, and `load()` raises `ValueError` on an undecorated subclass of a fixture that
    declares instances of its own (the full ORM loads only the parent's).

  ```python
  from surreal_orm_lite.testing import SurrealFixture, embedded_database, fixture

  @fixture
  class UserFixtures(SurrealFixture):
      alice = User(name="Alice", role="admin")
      bob = User(id="bob", name="Bob", role="player")  # an explicit id is kept

  async with embedded_database():  # a fresh, empty in-memory database
      async with UserFixtures.load() as fixtures:
          admins = await User.objects().filter(role="admin").exec()
          assert [admin.name for admin in admins] == ["Alice"]
      assert await User.objects().count() == 0  # alice and bob are deleted
  ```

- README section 26, "Embedded engine and test fixtures" (in-process databases and the embedded
  URL table, one engine per process, what the engine does not do, its SurrealQL line, identity,
  test fixtures with a pytest recipe, a note for users of the full SurrealDB-ORM), and a new
  behaviour table, "ORM behaviour: the embedded engine"; example notebook
  `examples/v0.25.0_embedded_test_engine.ipynb`.

### Changed

- `set_connection()`: `user`, `password`, `namespace` and `database` default to `None`, in the same
  positional order. `is_connection_set()` needs the user and password for a server URL only.
- A URL the installed SDK refuses (`rocksdb://`, a typo) is still accepted by `set_connection()`;
  the first ORM call now raises a `SurrealDbConnectionError` that quotes the SDK, before anything
  is opened, instead of "Connection not been set." or "Can't connect to the database.". For
  `rocksdb://` it points to #233.
- The DDL helpers (`define_computed_fields()`, `define_access()`) raise their usual
  `SurrealDbError` for a statement the embedded engine cannot parse, which SDK 2.0.0 reports as a
  bare `RuntimeError`.
- The `SurrealDbError` of an `AuthenticatedUserMixin` call whose record does not fit the model
  (since v0.17.0) has a new text, on every path, the server lines included: it says whether the
  shared session kept its previous identity or is now signed in as that record (for `refresh()`,
  also that the refresh token passed in is spent and whether the new pair is kept), and it names
  the fields that do not fit and Pydantic's error codes (`nickname: missing; prefs: int_parsing`),
  and quotes nothing from the record: no value, and no key the model does not declare (listed as
  `(undeclared field)`). The Pydantic `ValidationError` is its `__context__` instead of its
  `__cause__`.

### Notes

- **The engine is SurrealDB core 2.3.10**, bundled with the surrealdb SDK 2.0.0 base wheel (no
  new dependency, every supported Python version): write 2.x SurrealQL in raw queries and computed
  fields, and expect the 2.x column of the behaviour table. `client.version()` reports the SDK's
  version (`surrealdb-2.0.0`). Transactions are buffered, and parse errors in raw queries are a bare
  `RuntimeError`. The SDK 3.0 line bundles a 3.x engine in the `surrealdb[embedded]` extra (#205).
- **`rocksdb://` is not yet supported by the official SDK** (#233): the ORM relays the SDK's
  refusal and will accept the scheme with no change once a released SDK does. A bare `memory` (no
  `://`) is refused by SDK 2.0.0: write `memory://`. `surrealkv+versioned://` behaves as
  `surrealkv://` until time travel (v0.26.0).
- **With the surrealdb SDK 2.x, closing does not stop an in-process engine**: a client obtained
  before `close_connection()` or inside an `embedded_database()` block is still a running
  database. Do not use it after the close, drop it (`del client`) before reopening the same
  `surrealkv://` path, and never open one path from two processes.
- A fresh engine raises a retryable write conflict when several writes run concurrently on a table
  that does not exist yet: define the table first, or use `@retry_on_conflict()`.
- The v0.24.3 read hook (#235) also applies to the embedded engine's client: zero durations and
  degenerate geometries read as on a server. A `<set>[…]` is stored as an array there, as on 2.x.
- **Server pins unchanged**: tested against SurrealDB 3.3.4 and 2.7.0 (the CI pins), plus 3.1.5
  for backward compatibility; the embedded tests run in every CI job. `ModelFactory` and
  `Faker` come in v0.39.0.

## [0.24.3] - 2026-10-15

The clients the ORM opens now read the values the surrealdb SDK 2.0.0 cannot decode (zero
durations, SurrealDB 3.x native sets, degenerate geometries), and a zero duration can be stored.
No new public API.

### Fixed

- **Some values SurrealDB returns could not be read** (#235). The surrealdb SDK 2.0.0 cannot
  decode a zero duration, a SurrealDB 3.x native set (a `TYPE set<…>` column on SurrealDB 3.3) or
  a degenerate geometry another client wrote (a line of fewer than 2 points, alone or inside
  another geometry, or a polygon ring of fewer than 4 points): one such value failed the whole
  response it was in. The clients the ORM opens (`SurrealDBConnectionManager.get_client()`,
  `ephemeral_client()`) now read them, over WebSocket, HTTP and embedded connections, and later
  calls on the same client keep working:
  - a zero duration reads as zero: `timedelta(0)` or `Duration(0)` in a typed field, the SDK's
    `Duration(0)` in an untyped one;
  - a native set reads as a Python `set`, which `set[X]` and `frozenset[X]` fields validate, or as
    a `list` when its items are unhashable (record ids, objects, nested sets) or two of them are
    equal in Python, like `1` and `true`: every item the server holds comes back;
  - a degenerate geometry reads as its GeoJSON dict, such as
    `{"type": "LineString", "coordinates": [[1.0, 2.0]]}`, in an untyped field, `raw_query()` and
    live notifications; a geometry field refuses it with a `ValidationError` naming the field.

  Every other value reads exactly as the SDK reads it, and a malformed value still raises the
  SDK's error. The values go through a hook around the SDK's own CBOR decoder, installed only on
  the ORM's clients and only while the installed SDK cannot decode them: the SDK's modules are not
  modified, and an `AsyncSurreal` client the application builds itself keeps the SDK's behaviour.
  Written back through the ORM (`merge()`, or `update()`, which rewrites the whole row), a native
  set is stored as an array, unless its column is `TYPE set<…>`, and a degenerate geometry's dict
  as a plain object, on both lines.

- **A zero duration was refused** since v0.23.0 (`ValueError` for a field, a filter value or a
  bound parameter, `TypeError` from `live()`), because the SDK could not read it back. The ORM's
  clients now read it, so `timedelta(0)` and `Duration(0)` are written, filtered and read back like
  any other duration, on both lines; filter "longer than zero" with `__gt=Duration(0)`. The refusal
  remains only for an SDK that reads no zero duration and whose decoder the ORM cannot hook.
  Upgrade note: a record the ORM writes can now hold a zero duration, which a reader that does not
  go through the ORM's clients cannot read (an `AsyncSurreal` client the application builds itself,
  another service on the SDK 2.0.0).

### Notes

- **SurrealDB 2.x vs 3.x**: zero durations and degenerate geometries read the same on both lines.
  Native sets exist only on 3.x: on 3.3 a `TYPE set<…>` column stores a `set[X]` field's array as a
  native set, which now reads back as the declared set; 3.1 still refuses an array in such a
  column, so declare `array<…>` columns to support 3.1. On 2.x the column holds an array, as
  before.
- The README's "Values the SDK cannot read", "Geometries the SDK cannot read" and "Sets" sections,
  the refusals table and the 2.x/3.x row for `TYPE set<…>` columns describe the new behaviour; the
  v0.23.0 rich field types notebook stores and reads back a zero duration.
- A later SDK reads zero durations and native sets itself
  ([#205](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/205)); the hook then leaves
  them to it.
- Tested against SurrealDB 3.3.4 and 2.7.0 (the CI pins), plus 3.1.5 for backward compatibility.

## [0.24.2] - 2026-10-15

Sub-models keep their rich values after a write that returns the row, and a `timedelta` below the
top level reads the server's duration. No new public API.

### Fixed

- **A sub-model's durations, ranges and geometries were written back as plain objects** (#228).
  `save()` without an id, `upsert()`, `refresh()`, `merge()`, `patch()` and the `atomic_*` helpers
  applied the returned row without validation, so a sub-model field (a Pydantic model, a dataclass
  or a `TypedDict`, also inside `list[...]`, `dict[..., ...]` or `X | None`) held the row's `dict`
  with the SDK's values. The next full write (`update()`, `upsert()`) stored their fields
  (`{elapsed: 300000000000}`) instead of SurrealDB's durations, ranges and geometries, warned
  `PydanticSerializationUnexpectedValue`, and the next `get()` of the record failed validation.
  These calls now give a sub-model field the value a read gives: its model instance, with the ORM's
  `Duration`, `timedelta`, `Range` and geometry values inside, so writing the instance back is safe.
  - The returned row goes through the model's validation, as in `get()`, so the model's own
    validators (`field_validator`, `model_validator`, `model_post_init`) run on it too.
  - When the row as a whole does not validate, each sub-model field is validated on its own, except
    one that the model's own validators could reshape; that one, like a value that does not
    validate, is applied as received. These calls raise nothing new.
  - A GeoJSON object stored in a top-level geometry field (the full SurrealDB-ORM's format) now
    becomes the ORM geometry after `refresh()`, `merge()` and `patch()`, as after `get()`.
- **A `timedelta` below the top level failed to read the server's duration.** A sub-model's
  `timedelta` field, `list[timedelta]`, `dict[str, timedelta]` or `timedelta | str` refused the
  SDK's `Duration` on every read, as before v0.23.0. They now read a `timedelta`, floored to the
  microsecond (declare `Duration` to keep the nanoseconds), in a read and in the row a write
  returns. A row holding many such values is converted in one pass, without mutating the data,
  and the model's own validators (any mode) and `model_post_init` still apply once, even when they
  change the data in place.

### Notes

- **Identical on SurrealDB 2.x and 3.x**: the cause was in how the ORM applied the SDK's values,
  not in the server. The README's "Rich values inside sub-models" limitation of section 25 is
  removed, and "After a write that returns the row" describes the new behaviour; top-level `date`,
  `set` and `Range` fields still hold the SDK's value until the record is loaded again.
- Tested against SurrealDB 3.3.4 and 2.7.0 (the CI pins), plus 3.1.5 for backward compatibility.

## [0.24.0] - 2026-10-15

Geospatial fields: a model can declare geometry fields stored as SurrealDB's native geometries and
read back as the ORM's geometry classes, and query them with `nearby()`, a `GeoDistance`
annotation and the `inside`, `outside` and `intersects` lookups, with the same results and the
same distances on SurrealDB 2.x and 3.x. The names follow the full SurrealDB-ORM. The second minor
of Tier 2.

### Added

- **Geometry fields.** `GeoField["kind"]` and the aliases `PointField`, `LineStringField`,
  `PolygonField`, `MultiPointField`, `MultiLineStringField`, `MultiPolygonField`,
  `GeometryCollectionField` and `GeometryField` declare nullable geometry fields with a `None`
  default, as in the full ORM; a geometry class annotation (`entrance: GeometryPoint`) works too.
  A field accepts the ORM value, the SDK's, a GeoJSON object and, for points, a
  `(longitude, latitude)` pair. Every write stores a native geometry; `get()`, `filter()`, live
  notifications, change feeds and the rows a write returns give back the ORM's classes.
  `model_dump(mode="json")` writes GeoJSON. Edge models and field aliases are supported:

  ```python
  from surreal_orm_lite import (
      BaseSurrealModel, GeoDistance, GeometryPoint, PointField, SurrealConfigDict,
  )

  class Restaurant(BaseSurrealModel):
      model_config = SurrealConfigDict(table_name="restaurant")
      id: str | None = None
      name: str
      location: PointField = None

  times_square = GeometryPoint(-73.9855, 40.7580)  # longitude, latitude
  await Restaurant(id="pizza", name="Corner Pizza", location=(-73.9877, 40.7547)).save()

  closest = await (
      Restaurant.objects()
      .nearby("location", times_square, max_distance=5_000)  # metres
      .annotate(dist=GeoDistance("location", times_square))
      .order_by("dist")
      .exec()
  )
  round(closest[0].dist)  # 411: metres, as SurrealDB's geo::distance measures it
  ```

- **Geometry value classes**: `Geometry` and `GeometryPoint`, `GeometryLine`, `GeometryPolygon`,
  `GeometryMultiPoint`, `GeometryMultiLine`, `GeometryMultiPolygon` and `GeometryCollection`, with
  the names, constructor signatures, attributes and methods of the SDK's classes
  (`parse_coordinates()`, `get_coordinates()`, …). They are immutable and hashable, keep `float`
  coordinates, convert to and from GeoJSON (`to_geojson()`, `Geometry.from_geojson()`,
  `__geo_interface__`), and `str()` gives their SurrealQL text. They exist because Pydantic's
  `model_dump()` flattens the SDK's dataclasses into plain dicts; the ORM converts them to the
  SDK's classes when it sends them.
- **`InvalidGeometryError`**: raised for a geometry the classes refuse (a non-finite coordinate, a
  polygon ring of fewer than 4 points or not closed, a collection nested more than 32 levels,
  malformed GeoJSON). It is both a `ValueError` and a `surrealdb.InvalidGeometryError`.
- **`QuerySet.nearby(field, point, max_distance)`**: keeps the records whose field is a point within
  `max_distance` metres of `point`. The point and the distance are bound as query variables, a
  `.is_point()` guard leaves out records whose field holds no point, several calls are AND-ed, and
  the condition applies wherever `filter()` does, `live()` and `watch()` included.
- **`GeoDistance(field, point)`** for `annotate()`: each instance gets the distance in metres as an
  attribute named after the alias (`None` when its field is not a point), `order_by()` sorts by
  it, and it is never written back.
- **Spatial lookups** `inside`, `outside` and `intersects` (`filter(location__inside=zone)`),
  compiled to SurrealQL's `INSIDE`, `OUTSIDE` and `INTERSECTS` with one bound geometry; they work in
  `filter()`, `Q` objects (`~Q` included) and live queries. `outside` keeps SurrealQL's meaning: a
  record without a geometry is outside.
- **Live-query literals** for geometries (GeoJSON object form, every kind and collection member),
  so `nearby()`, the spatial lookups and geometry filter values give the same notifications on
  both lines.
- `is_geo_field()` and `get_geo_info()` in `surreal_orm_lite.geo`; the geometry classes, the field
  aliases, `GeoField`, `GeoDistance` and `InvalidGeometryError` are exported from the package root.
- README section 25, "Geospatial fields" (annotations, values and GeoJSON, `nearby()`,
  `GeoDistance`, spatial lookups, live queries, computed geometry fields, migrating stored GeoJSON,
  type checkers, a note for users of the full SurrealDB-ORM); example notebook
  `examples/v0.24.0_geospatial_fields.ipynb`.

### Changed

- **Geometries go through the encoding boundary.** An ORM geometry is sent as the equal SDK
  geometry; an SDK geometry is sent as it is once checked. An SDK geometry every SurrealDB line
  refuses (a line of fewer than 2 points, a polygon without a ring, a ring of fewer than 4 points
  once closed, a coordinate that is not a number, a member of the wrong kind) raises a plain
  `ValueError` before anything is sent.
- Rows applied after a write without validation (`save()` without an id, `refresh()`, `merge()`,
  `upsert()`, `patch()`, the `atomic_*` helpers) turn the SDK's geometries into the ORM's classes
  in top-level geometry fields, so writing the instance back stores a geometry. Sub-models keep the
  row's `dict`, as before (#228).

### Fixed

- A `Decimal` coordinate written with an exponent in an SDK geometry is now sent in fixed-point
  notation, like a top-level `Decimal`: SurrealDB 2.x refused `Decimal("1E-7")` there, and both
  lines stored a long one with a wrong value. Both lines now store the value.

### Notes

- **Same on both lines**, measured on SurrealDB 2.7.0 and 3.3.4, identical on 3.1.5: storage,
  reads, `nearby()`, distances, the spatial lookups and live notifications. The README's
  "SurrealDB 2.6.x vs 3.x" table gains three rows, one of them retroactive: a GeoJSON dict written
  to an untyped field (`Any`, `dict`) is stored as a plain object, not a geometry.
- **Migrating from the full SurrealDB-ORM**, which stores geometry fields as GeoJSON dicts (plain
  objects that `geo::*` functions refuse): a geometry field reads such a value, and `update()`
  writes it back as a native geometry, so loading and updating every record migrates a table.
- **Known limits**: the surrealdb SDK 2.0.0 cannot read a degenerate geometry another client
  wrote (a 1-point line, a ring of fewer than 4 points). Geometries inside sub-models are written
  back as plain objects after a write that syncs the instance (#228), and ORM geometries in
  untyped fields read back as the SDK's classes (#222). Filter values of other lookups and keyword
  writes are not validated against the field: pass geometry objects there (#220, #221).
  `GeoField["point"]` is not understood by type checkers, as in the full ORM; use the aliases or
  the classes in type-checked code.

## [0.23.0] - 2026-10-14

Rich field types: `timedelta`, `date`, sets and the ORM's own `Duration` and `Range[T]` are stored
as SurrealDB's native values and read back as the declared type, with the same behaviour on
SurrealDB 2.x and 3.x. Every value the ORM hands the official SDK now goes through one encoding
step. The first minor of Tier 2.

### Added

- **Rich field types.** A model can declare `timedelta`, `date` (subclasses included) and
  `set[X]`/`frozenset[X]` fields, and the ORM's `Duration` and `Range[T]`. Every write stores them
  natively (a duration, a `datetime` at midnight UTC, an array sorted when its items can be
  ordered, a range) and every read returns the declared type. Optional fields and values nested in
  containers or sub-models are sent the same way at any depth. `datetime`, `Decimal`, `UUID` and
  `bytes` fields keep working as before:

  ```python
  from datetime import date, timedelta
  from surreal_orm_lite import (
      BaseSurrealModel, BoundExcluded, BoundIncluded, Duration, Range, SurrealConfigDict,
  )

  class Subscription(BaseSurrealModel):
      model_config = SurrealConfigDict(table_name="subscription")
      id: str | None = None
      renews_on: date
      trial: timedelta
      grace: Duration | None = None
      active_hours: Range[int]
      tags: set[str] = set()

  await Subscription(
      id="acme",
      renews_on=date(2026, 2, 1),
      trial=timedelta(days=14),
      grace=Duration.parse("1h30m1ns"),                         # nanoseconds kept
      active_hours=Range(BoundIncluded(9), BoundExcluded(17)),  # 9..17
      tags={"pro", "annual"},
  ).save()
  sub = await Subscription.objects().get("acme")
  sub.trial  # timedelta(days=14): a duration on the server
  ```

- **`Duration`**: a SurrealDB duration with the server's nanosecond precision, where `timedelta`
  stops at microseconds. It mirrors the SDK 2.0.0 `surrealdb.Duration`, the same members under the
  same names and parameters (`elapsed`, `parse(value, nanoseconds=0)`, `get_seconds_and_nano()`,
  `to_compact()`, `to_string()`, the unit properties), with exact results: `to_compact()` keeps
  the nanoseconds and `to_string()` writes every unit. `parse()` reads the server's syntax strictly
  (`"1h 30m"` and `"1.5h"` are refused). It adds `MAX_NANOSECONDS`, `from_timedelta()`,
  `to_timedelta()`, hashing and ordering. As a field it accepts a `Duration`, the SDK's `Duration`,
  a non-negative `timedelta` or a SurrealQL duration string (`"1h30m"`); in JSON it is that text.
- **`InvalidDurationError`**: raised for a value `Duration` refuses. It is both a `ValueError` and
  a `surrealdb.InvalidDurationError`, so an `except` written against the SDK catches it.
- **`Range[T]`**: a SurrealDB range whose sides are the SDK's `BoundIncluded`/`BoundExcluded` (now
  exported by the ORM) or `None` for an open side; the SDK's own spellings of an open side
  (`Bound()`, `surrealdb.cbor.undefined`, a CBOR null) are accepted too. `str()` is the SurrealQL
  range text (`9..17`, `1>..=5`, `65..`), which a `Range` field reads back, and which writes every
  bound exactly, including values a live-query filter refuses. As a field it validates each bound
  value as `T` and accepts the SDK's `Range`, a builtin `range` with step 1 and that text; in JSON
  it is that text.
- **`in`/`not_in` with a `Range`** (the ORM's or the SDK's) bind it as one value (`hour IN $_f0`)
  instead of refusing it as a non-iterable; a builtin `range` keeps its meaning, the list of its
  integers. On the record-id column `id` and on an edge's `in`/`out` columns it raises
  `ValueError`, since a record id never falls inside a value range. `contains` on a range column
  tests membership (`active_hours__contains=12`).
- **Live-query literals** for durations (`1h30m1ns`, exact to the nanosecond), dates (midnight
  UTC), sets (sorted when their items can be ordered) and ranges, so `live()` and `watch()` filters
  on rich fields give the same results on both lines.
- `Duration`, `Range`, `BoundIncluded`, `BoundExcluded` and `InvalidDurationError` are exported
  from the package root.
- README section 24, "Rich field types" (annotation table, `Duration`, `Range[T]`, filters,
  precision, sets, values refused before sending, rows applied after a write, values the SDK
  cannot read, authentication, a note for users of the full SurrealDB-ORM); example notebook
  `examples/v0.23.0_rich_field_types.ipynb`.

### Changed

- **Every value the ORM sends goes through one encoding step** before it reaches the official SDK:
  the fields of every write API, filter values, `raw_query()` and `call_function()` variables,
  transaction statements and auth variables (`signup()`, and the `variables=` of
  `SurrealDBConnectionManager.signin()`/`signup()`). A value v0.22.2 already sent as it was is
  handed to the SDK unchanged.
- **Values the server would reject are refused before anything is sent**, with a plain
  `ValueError` that says why, never re-wrapped in a `SurrealDbError`: a negative duration, a
  duration of `2**64` seconds or more, a zero duration while the installed SDK cannot read one back
  (SDK 2.0.0), a finite `Decimal` whose integer part, rounded, exceeds `2**96 - 1` (the largest
  SurrealDB decimal), and an `int` outside the signed 64-bit range (one between `2**63` and
  `2**64 - 1` was stored as a negative number; use `Decimal`). For a zero duration, store `None`
  and filter "longer than zero" with `__gte=Duration(1)`.
- A `Decimal` written with an exponent (`Decimal("1E-7")`, `Decimal("1E+3")`) is sent in
  fixed-point notation (`0.0000001`, `1000`), cut after the 29th fractional digit. The server keeps
  at most 28 fractional digits, rounding half away from zero as before, so the stored value is the
  same. A value without an exponent is sent as before.
- `atomic_increment()` refuses a duration amount (a `timedelta`, the SDK's or the ORM's
  `Duration`) with `TypeError` before anything is sent: SurrealDB 2.x ignores `+=` with a duration
  where 3.x applies it, so the same call gave a different row on each line. Set the new value with
  `merge()`. Numeric and `Decimal` amounts are unchanged.
- An SDK `Range` whose open side is written `undefined` (or `Bound()`) is now sent with the
  protocol's null bound; the server reads it the same way, as an open side.
- Rows applied after a write without validation (`save()` without an id, `refresh()`, `merge()`,
  `upsert()`, `patch()`, the `atomic_*` helpers) turn the SDK's duration into a `timedelta` or a
  `Duration` in top-level fields of those types, so writing the instance back stores a duration.
  Every other value is applied as received, as before.

### Fixed

- `timedelta`, `date`, `set`/`frozenset`, SDK `Duration` and open-sided `Range` values, `Decimal`s
  written with an exponent (zeros such as `0E+30` included) and `int`s outside signed 64-bit are now
  converted or refused before sending, in writes, filter values, `call_function()` arguments and
  auth variables.
- A row holding a duration now loads into a top-level `timedelta` field (floored to the
  microsecond) through `get()`, `filter()`, `all()`, `first()`, `raw_query()`, live and
  change-feed events, model `signup()` and the rows `bulk_create()` returns; the field's validation
  refused the SDK's duration.
- The live-filter `Decimal` literal decides huge exponents before formatting: a zero with a long
  exponent is written without building its full text, and a value beyond Python's 4,300-digit
  limit raises the documented `TypeError` instead of a `ValueError`.

### Notes

- **Same on both lines**, measured on SurrealDB 2.7.0 and 3.3.4, with 3.1.5 where it differs,
  except `TYPE set<…>` columns: on 3.3 such a column holds a native set that the surrealdb SDK
  2.0.0 cannot read (reads fail with an SDK decoding error), and 3.1 refuses the array. Declare
  such columns `array<…>`, optionally with `VALUE $value.distinct()`. The README's "SurrealDB 2.6.x
  vs 3.x" table gains these two rows and a retroactive one: on reads, 2.x keeps a `Decimal`'s
  trailing zeros (`12.50`) where 3.x normalises them (`12.5`); the values are equal.
- **SDK 2.0.0 limits**: it cannot decode a zero duration or a SurrealDB 3.x native set. The ORM
  never sends either, but a record another client wrote may hold one, and reading it fails with an
  SDK decoding error. A later SDK lifts both (#205).
- **Filter values on rich fields must be typed**: strings are not converted, so
  `filter(trial__gte="7d")` compares the stored duration with a string; pass `timedelta(days=7)`
  (#220). Keyword writes (`merge(field=...)`, `bulk_update()`) are not validated against the
  field's type (#221).
- **Precision**: `datetime` to the microsecond (the SDK truncates the server's nanoseconds);
  `timedelta` floored to the microsecond on read; `Duration` exact. On reads, a `timedelta` below a
  top-level field (`list[timedelta]`, a sub-model's field) fails validation as before: declare
  `Duration` there.
- After a write that returns the row, `date`, `set` and `Range` fields hold the SDK's value (a
  midnight `datetime`, a `list`, the SDK's `Range`) until the record is loaded again; writing such
  an instance back is safe.
- **Equality with the SDK's values is one-way**: `Duration(5) == surrealdb.Duration(5)` is `True`,
  the reverse is `False` (the SDK's `__eq__` answers `False` for any other type); the same holds
  for `Range`. Keep the ORM value on the left.
- Tested against SurrealDB 3.3.4 and 2.7.0 (the CI pins), plus 3.1.5 for backward compatibility.

## [0.22.2] - 2026-10-13

Change-feed fixes: history replayed by `changes(since=None)` on SurrealDB 3.x, and a timed-out
read that could hang on both lines. No new public API.

### Fixed

- **`changes(since=None)` no longer replays history on SurrealDB 3.x** (#212). On 3.x the stream
  started at the server clock's millisecond (`unix_ms << 16`), but a 3.x versionstamp is
  `unix_ms << 16 | counter` and `SINCE` is inclusive, so every entry committed earlier in that
  same millisecond was delivered as if it were new, and `stream.cursor` did not point at the end
  of the feed. `start()` now reads the entries stamped in that millisecond, a page of
  `batch_size` at a time, and moves the cursor past the last one: usually one short read, at most
  `ceil(n / batch_size) + 1` reads for the `n` entries of that millisecond, however busy the
  table is. It goes through the same retry and `stop()` path as every other request.
  - A write committed in that millisecond while `start()` runs is history and is skipped; a write
    in a later millisecond, or one made after `start()` returns (even in the same millisecond),
    is delivered.
  - This was the cause of two intermittent change-feed test failures on SurrealDB 3.x.
- **A change-feed read under `asyncio.timeout()` or `wait_for()` could hang forever**, on both
  server lines. When the read was cancelled in the very loop iteration the server's answer
  arrived, the official SDK (surrealdb 2.0.0) raised a `KeyError` over the `CancelledError` and
  its connection died, which is what a dropped connection looks like. The stream retried it as one
  and lost the cancellation: the timeout never fired and the read polled on, for as long as the
  process ran. A cancellation of the reading task is now re-raised instead of retried, so the read
  times out as asked and loses nothing (the next read starts from the cursor). A live query's
  automatic resubscribe gets the same guard, so cancelling it (`asyncio.run()` shutting down, for
  instance) no longer has it reconnect on.

### Notes

- **`changes(since=None)` on SurrealDB 2.x is unchanged**: there "now" is the end of the feed,
  found by searching the counter, which never had this problem. The README's "SurrealDB 2.6.x vs
  3.x" table now says how the 3.x side finds "now". The timed-out read fix is the same on both
  lines: the race is in the SDK, not in the server.
- Tested against SurrealDB 3.3.4 and 2.7.0 (the CI pins), plus 3.1.5 for backward compatibility.

## [0.22.0] - 2026-10-12

Native typed relations: graph edges as Pydantic models, created through the official SDK's
`insert_relation()` and returned as typed instances, with the full SurrealDB-ORM's graph API on
both server lines. The last minor of Tier 1.

### Added

- **Typed edges — `BaseSurrealEdge`**. An edge table becomes a model whose data is validated by
  Pydantic, with frozen `in_` (column `in`) and `out` record endpoints that accept a saved
  instance, a `RecordID` or a `"table:id"` string. `relate()` takes the edge class and returns the
  created edge:

  ```python
  from pydantic import Field
  from surrealdb import RecordID
  from surreal_orm_lite import BaseSurrealEdge, SurrealConfigDict

  class Purchased(BaseSurrealEdge):
      model_config = SurrealConfigDict(table_name="purchased")
      quantity: int = Field(ge=1)
      price: float

  edge = await alice.relate(Purchased, widget, quantity=2, price=9.5)
  assert isinstance(edge, Purchased) and edge.in_ == RecordID("User", "alice")
  ```

  `relate()` is typed with two overloads: the edge class for an edge model, `list[dict]` for a
  table name (the v0.6.0 form, unchanged).

- **Edge `save()` and `QuerySet.bulk_create()` through `insert_relation()`** — a real relation that
  graph traversal sees, `id` hydrated from the returned row; `bulk_create()` of N edges without an
  id is one `insert_relation()` call. An explicit edge id that already exists raises
  `SurrealDbError` ("already exists") and writes nothing, on every measured version.
- **Edges in transactions**: `save(tx=)`, `relate(Edge, …, tx=)` and `bulk_create()` under
  `objects(tx=)`, interactive (3.x over WebSocket) and buffered (2.x, or HTTP); an auto-id edge is
  allowed in a buffered transaction (its `id` stays `None` until the commit).
- **`get_or_create(in_=a, out=b)` on an edge model** is an idempotent relate; instances,
  `RecordID`s and `"table:id"` strings converge on the same row.
- **`SurrealConfigDict(table_name=…)`** — the full ORM's key for a model's table name, honoured by
  every path (writes, reads, `QuerySet`, live queries, change feeds, DDL helpers) and validated as
  an identifier when the class is defined.
- **Model instances as filter values**, for every model and column: keyword filters, `Q`, every
  scalar lookup and each item of `in`/`not_in`/`containsany`/`containsall`. The create payload of
  `get_or_create`/`update_or_create` gets the same conversion, so
  `Order.objects().get_or_create(customer=alice)` stores a record id.
- **`"table:id"` strings on edge endpoint columns** in filters (`filter(in_="User:alice")`,
  `filter(in___in=[alice, "User:bob"])`), live queries included.
- **Graph helpers in the full SurrealDB-ORM's parameter order**, by position or by keyword:
  `relate(edge, target, reverse, tx)`, `remove_relation(edge, target, reverse, tx)`,
  `remove_all_relations(edge, direction, tx)`, `get_related(edge, direction, model_class)`; only
  the lite's `data=` stays keyword-only. With them:
  - `reverse=True` on `relate()` and `remove_relation()` (target → edge → self);
  - edge fields as keyword arguments on `relate()` (`**edge_data`), merged with `data=` (a field
    given both ways raises `TypeError` before any call);
  - `tx=` on `relate()`, `remove_relation()` and `remove_all_relations()`;
  - edge model classes accepted wherever an edge name is, in every helper;
  - a list or tuple of edges for `remove_all_relations()`;
  - `get_related(direction="both")`: out-neighbours, then in-neighbours, in one query;
  - a bare record id as `remove_relation()` target (`"abc123"`): compared as a string with the id
    of the edge's `out` (`in` with `reverse=True`), it matches that id in any table, as in
    SurrealDB-ORM. Pass an instance or a `RecordID` to pin the table.
- `BaseSurrealEdge` is exported from the package root.
- README section 23, "Typed edges (`BaseSurrealEdge`)", with "Idempotent edges" (a deterministic
  array id, a `UNIQUE` index on `in, out`) and a "Migrating from SurrealDB-ORM (graph API)" note;
  example notebook `examples/v0.22.0_native_typed_relations.ipynb`.

### Changed

- **`QuerySet` reads the table from `get_table_name()`**, so a model with the legacy
  `_table_name` now writes to that table too; before, writes went to the class-name table while
  `objects()` read the other one.
- **`remove_relation()` and `remove_all_relations()` run a graph-path `DELETE`** with bound record
  ids: exactly the edges a traversal sees. A plain record that merely carries `in`/`out` fields is
  no longer matched, and an edge table that was never created is a native no-op on 3.x too, also
  inside a transaction.
- `relate()` (with an edge class or a table name), `get_related()`, `remove_relation()`,
  `remove_all_relations()` and `traverse()` accept a node whose record id is not an identifier
  (`joe@x.com`).
- **Writes that would change an edge's endpoints are refused** with `ValueError` before any call
  (`merge()`, `patch()` on the instance and the `QuerySet`, the `atomic_*` helpers,
  `bulk_update()`, `update_or_create(defaults=)`): SurrealDB silently ignores them on both lines.
  Delete the edge and relate again.
- **`upsert()` and `save(server_values=)` are refused on an edge** with `SurrealDbError`: an
  `UPSERT` stores a plain record that no traversal sees, and `INSERT RELATION` cannot evaluate
  `SET` expressions.
- An edge write refused by the table's `PERMISSIONS` raises `SurrealDbError`; the server answers it
  with fewer rows and no error.

### Deprecated

- `_table_name` — use `model_config = SurrealConfigDict(table_name=…)`. It still works and emits a
  `DeprecationWarning` when the class is defined (`table_name` wins when both are set); it will be
  removed in a future minor.

### Fixed

- `SurrealConfigDict` keys are optional for type checkers (`total=False`).
- Filter keys on a field ending in `_` (`type___gt`, `in___in`) no longer raise
  "Unsupported lookup".
- `relate(<non-str>, …)` raises `TypeError` instead of `AttributeError`.
- A single model instance given to a sequence lookup (`customer__in=alice`) raises `ValueError`
  instead of being read as a list of `(name, value)` pairs.
- `save(tx=)`, `upsert()`, `update(tx=)` and `delete(tx=)` bind the record id instead of writing
  it into the statement, so a record with an array id (such as `pair:[User:alice, User:bob]`) can
  be saved, updated or deleted in a transaction and upserted in or out of one; before, the
  statement failed to parse on both lines, and inside a transaction it failed the whole
  transaction.

### Security

- Record ids are bound as query parameters instead of being written into the statement as
  `table:id` text: `save(tx=)`, `upsert()`, `update(tx=)`, `delete(tx=)`, `relate()` (with an edge
  class or a table name), `get_related()`, `remove_relation()`, `remove_all_relations()` and
  `traverse()`. The only text written into these statements is identifiers validated before any
  call: the table name (`table_name`, checked when the class is defined), edge names (every item
  of a list), field names and the `traverse()` path.
- An edge write denied by the table's permissions can never look like a success (see Changed).

### Notes

- **A duplicate explicit edge id differs by version, and the ORM evens it out.** A raw
  `INSERT RELATION` with an id that already exists silently keeps the old edge on 2.7.0 and on
  3.1.5 (although the SurrealDB docs announce a record-exists error from 3.1.5), and raises
  `AlreadyExistsError` on 3.3.3. The ORM sends a guarded statement, so it raises "already exists"
  and writes nothing on all three; inside a buffered transaction the commit fails.
- On 2.x, the edge record of a `CREATE`/`UPDATE` live notification or of a change-feed update entry
  carries `"__": true`. The typed `instance` drops it, so it is identical on both lines;
  `ModelChangeEvent.raw` stays as the server sent it.
- A batch of edges partly refused by permissions stays partly written outside a transaction.
- Idempotent edges: `get_or_create()` has a race window between two concurrent callers; a
  deterministic array id or a `UNIQUE` index on `in, out` closes it server-side, on both lines
  (README section 23).
- Schema-level relation tables (`DEFINE TABLE … TYPE RELATION IN … OUT …`) are defined by hand for
  now; the DDL helper, with the full ORM's `table_type`/`relation_in`/`relation_out` keys on
  `BaseSurrealEdge`, is planned for v0.33.0.
- Every behaviour above was measured on SurrealDB 2.7.0 and 3.3.3, with 3.1.5 where it differs;
  the README's "ORM behaviour: SurrealDB 2.x vs 3.x" table gains fifteen rows, one of them
  retroactive (since v0.7.0: an undeclared field on a `SCHEMAFULL` table).

## [0.21.1] - 2026-10-12

Python 3.15 support. No library code changes; the published package behaves exactly as 0.21.0.

### Added

- **Python 3.15** (#206). The CI matrix runs the full suite on Python 3.11 to 3.15 against both
  SurrealDB lines, the package declares the `Programming Language :: Python :: 3.15`
  classifier, and the README's requirements table states the tested range. Lint and the
  coverage upload stay on 3.14.

### Changed

- **`uv.lock` refreshed so the development environment installs from wheels on Python 3.15.**
  Only the locked packages with no CPython 3.15 wheel, or only a pure-Python fallback, were
  upgraded, plus those that follow from their new floors: aiohttp 3.14.5, cffi 2.1.1,
  charset-normalizer 3.5.2, coverage 7.16.2, debugpy 1.8.22, markupsafe 3.0.4, propcache
  0.5.4, pydantic 2.14.1 (pydantic-core 2.50.1), tomli 2.5.0, typing-extensions 4.16.0,
  typing-inspection 0.4.4, websockets 17.2 and yarl 1.25.1.

### Notes

- **The dependency ranges in `pyproject.toml` are unchanged** (`pydantic>=2.13.4`,
  `surrealdb[pydantic]>=2.0.0,<3.0.0`), so this release changes nothing in what your
  environment resolves; the lock only pins what CI and contributors install. The full suite
  passes on the refreshed lock, pydantic 2.14 and websockets 17 (the SDK's WebSocket transport)
  included, on SurrealDB 2.7.0 and 3.3.3.

## [0.21.0] - 2026-10-12

Live queries that survive a dropped connection, and change feeds with a cursor that resumes
exactly — the rest of the full SurrealDB-ORM's real-time API, on the official SDK and on both
server lines.

### Added

- **Auto-resubscribe** — `QuerySet.live(*, auto_resubscribe=True, diff=False, on_reconnect=None)`,
  the full ORM's signature, and the same two arguments on `watch()`. When the WebSocket drops,
  the stream opens a new connection, sends the same live query again (filters, `DIFF`, `FETCH`
  intact) and moves its buffer onto the new uuid, so an `async for` simply carries on:

  ```python
  async def catch_up(old_id, new_id):
      ...  # re-read what changed while the connection was down

  async with Order.objects().filter(status="open").live(on_reconnect=catch_up) as stream:
      async for event in stream:
          ...
  ```

  `on_reconnect(old_id, new_id)` is sync or async, receives `UUID`s, and runs once the new
  subscription is already buffering; an exception in it is logged. Connection failures are
  retried with exponential backoff (`reconnect_delay` 0.5 s, `reconnect_max_delay` 30 s,
  `reconnect_max_attempts` 10 — class attributes on `LiveStream`/`LiveModelStream`); when they
  run out, or on any other failure, the error is raised from the iteration.

- **Change feeds** — `QuerySet.changes(*, since=None, poll_interval=0.1, batch_size=100)` →
  `ChangeModelStream`, the full ORM's API: an async iterator of `ModelChangeEvent` read from
  `SHOW CHANGES`, over HTTP or WebSocket. `stream.cursor` (an `int`) resumes exactly with
  `changes(since=cursor)`, on 2.x and 3.x alike; it advances past a transaction only after its
  last event, so saving it after each event is at-least-once. `since=` also takes a `datetime`
  or ISO-8601 string. With `INCLUDE ORIGINAL`, updates carry `changed_fields`. `start()` fixes
  the starting position early; `stop()` ends the iteration at once, even during a retry
  backoff. A change feed defined on the database (`DEFINE DATABASE … CHANGEFEED`) is accepted.
- `ChangeModelStream` and `ReconnectCallback` are exported from the package root.

### Changed

- **A dropped WebSocket no longer hangs anything.** A stream with `auto_resubscribe=False`, and a
  secondary `subscribe_live()` reader, now raise `SurrealDbConnectionError` on a drop; before,
  they waited forever. Deliberate teardowns (`kill()`, `close_connection()`, …) still end streams
  cleanly and never trigger a resubscribe.
- **`get_client()` replaces a dropped WebSocket client** (signed in, session replayed) instead of
  handing back a dead one that failed every call until `reconnect()`. The dead client's socket is
  closed, which matters when the SDK's receive loop died on an undecodable frame with the socket
  still open: the server would otherwise keep that session's live queries running.
- **Opening a client is serialised per event loop.** Concurrent `get_client()` calls with no
  usable client share one new connection; before, each opened its own and the extras leaked —
  a live query started on one could never be killed.
- `kill()` on a dropped client returns without a network call instead of raising
  `SurrealDbError`, and the live queries of a replaced client are recorded as dead, so a late
  `subscribe_live()` on one of them ends at once.
- A `get_client()` cancelled while opening the connection now closes the half-open socket.
- `live()` and `watch()` no longer refuse `auto_resubscribe=` / `on_reconnect=` with `TypeError`.

### Security

- **A resubscribe never changes the identity a live query runs under.** A live query is
  evaluated with the permissions of whoever opened it, and a new connection comes back as the
  identity the manager replays. The manager stamps each connection with a fingerprint of what it
  was built from (url, namespace, database, user, stored token) whenever it sets its identity. A
  stream opened under an identity that cannot be replayed (`store=False`), or whose fingerprint
  is no longer the replay identity, ends with `SurrealDbAuthenticationError` instead of
  resubscribing — possibly as the configured root user. Settings changed and then restored count
  as no change.
- **A dropped connection never silently comes back as the configured user.** If it ran as an
  identity the manager does not store (`signin`/`signup`/`authenticate` with `store=False`),
  every call on that event loop raises `SurrealDbAuthenticationError` until the application signs
  in or authenticates again, calls `invalidate()`, or `reconnect()`/`close_connection()`.
- **A network failure while the stored session is replayed no longer erases it.** It is reported
  as a retryable `SurrealDbConnectionError`; only a token the server actually refuses is
  forgotten (the behaviour since v0.16.0).
- **Reconnect loops do not retry a server that answered "no".** `get_client()` still raises
  `SurrealDbConnectionError` when the configured credentials are rejected, but live streams and
  change feeds raise it at once instead of retrying it for minutes.

### Notes

- **The change-feed cursor differs by line.** Every entry carries a `versionstamp`, but 2.x's
  `SINCE` takes `versionstamp >> 16` — resuming with the stamp the server returned silently
  yields nothing, forever — while 3.x's takes the stamp itself, inclusively. The ORM reads the
  server version once per stream and stores the next `SINCE` value.
- **`SINCE <datetime>` is unusable as is**: always empty on 3.x, and on 2.x it also returns
  changes from before the moment. A datetime is converted to 3.x's versionstamp layout
  (`unix_ms << 16`, pinned by a test) and passed through on 2.x (at-least-once); "from now" on
  2.x is found by searching the counter.
- A change feed does not record the kind of write: a creation arrives as `UPDATE`. A table
  without a change feed answers `SHOW CHANGES` with `[]` on both lines, so `changes()` checks
  `INFO FOR DB` and refuses it (skipped when `INFO` is not permitted).
- Observed once in CI on a fresh SurrealDB 2.7.0 server, never reproduced locally: a plain write
  to a table with a change feed failed with the retryable "read or write conflict" error, with no
  concurrent writer. `retry_on_conflict` (v0.12.0) handles it; the test suite now uses it there.
- Tested against SurrealDB 2.7.0 and 3.3.3 (the pin since 0.20.1), with 3.1.5 as the
  backward-compatibility check. Every server behaviour described in this entry was measured on
  3.3.2 and re-measured on 3.3.3, with identical results.
- **What a live query misses during an outage is not replayed** — catch up in `on_reconnect`,
  or from a change feed. A request already in flight when the socket drops fails with the SDK's
  own `KeyError`; retrying arbitrary queries is v0.40.0.

## [0.20.0] - 2026-10-07

Typed live queries, with the same API as the full SurrealDB-ORM: code written against
`surreal_orm` migrates by changing the import.

### Added

- **`QuerySet.live(*, diff=False)` → `LiveModelStream`** — an async context manager and
  iterator of `ModelChangeEvent` objects carrying a **model instance**. The queryset's filters
  are applied by the server, and the live query is killed when the block exits:

  ```python
  async with User.objects().filter(role="admin").live() as stream:
      async for event in stream:
          print(event.action, event.instance.name, event.record_id)
  ```

- **`ModelChangeEvent`** — `action`, `instance`, `record_id` (`"user:abc"`), `changed_fields`
  (diff mode, Python field names) and `raw`, field for field the full ORM's event.
- **Filtered live queries** — `filter()` (lookups, `Q`, `Var` with `variables()`) and `fetch()`
  compile into `LIVE SELECT … WHERE … FETCH …`, with field aliases translated. Works identically
  on SurrealDB 2.x and 3.x.
- **Diff mode** — `live(diff=True)` / `watch(diff=True)` use `LIVE SELECT DIFF`; `changed_fields`
  lists the top-level fields each update touched.
- **`post_live_change` signal** — same name and arguments as the full ORM (`instance`, `action`,
  `record_id`, `changed_fields`), run for every event a `live()` stream yields on one background
  task per stream, in event order. A failing handler is logged and never interrupts the stream;
  a normal exit waits for pending handlers (`signal_drain_timeout`, 5 s), an exception cancels
  them.
- **`ModelChangeEvent.validation_error`** — a record that does not fit the model no longer ends
  the stream: the event carries the error and an unvalidated instance, `post_live_change` is not
  sent for it, and a warning naming the failing fields — never their values — is logged once per
  stream.
- **`watch(diff=)`**, and `watch()` now honours the same filters as `live()`.
- `LiveModelStream`, `ModelChangeEvent` and `post_live_change` are exported from the package root.

### Changed

- **`await QuerySet.live()` is deprecated.** It still starts a live query and returns its uuid
  (the v0.19.0 contract), with a `DeprecationWarning` pointing at `async with ….live()` or
  `watch()`. The stream `live()` now returns also implements the coroutine protocol, so
  `asyncio.create_task(qs.live())` and `asyncio.run(qs.live())` keep working; it can be awaited
  once, and an awaited stream refuses to `start()` (and the reverse). `inspect.iscoroutine()` is
  `False` for it. To hold a subscription across tasks, start the stream
  (`await qs.watch().start()`), read **it**, and stop it with `kill(stream.live_id)`.
- Every live query now starts through `LIVE SELECT` sent with `query()` rather than the SDK's
  table-only `live()`. `filter()`, `fetch()` and `variables()` are no longer refused; `select()`,
  `values()`, `annotate()`, `order_by()`, `limit()`, `offset()` and `objects(tx=)` still are, by
  name.

### Notes

- **SurrealDB 2.x drops bound parameters inside a live query.** `LIVE SELECT … WHERE status = $s`
  is accepted, then `$s` evaluates to `NONE` at notification time and the filter matches nothing,
  without an error. For live queries only, the ORM therefore writes each filter value as a
  SurrealQL literal — the full ORM's approach — through a closed encoder (`None`, `bool`, `int`,
  `float`, `str`, `Decimal`, `datetime` in UTC, `UUID`, `RecordID` incl. UUID keys, lists and
  dicts). Any other type, a string containing NUL, an integer beyond 64 bits or a `Decimal`
  beyond 28 significant fractional digits / a 96-bit mantissa raises `TypeError` at the call,
  before anything is sent. A reference that is neither bound through `variables()` nor a server
  parameter (`$auth`, `$session`, …) raises `SurrealDbError`: unbound, it would read as `NONE`
  on every notification. Every literal is checked against the server on both lines by the test
  suite.
- Filter semantics, measured identically on 2.7.0 and 3.3.0: a record entering the filter through
  an update arrives as `UPDATE`; one leaving it reports nothing, nor its later `DELETE`.
- **Diff mode differs by line in its raw form**: the root patch path is `"/"` on 2.x and `""` on
  3.x, and a `DELETE` carries the whole last record on 2.x but a `replace` with `None` on 3.x.
  `live(diff=True)` normalises both; `watch(diff=True)` passes them through. On both lines a
  changed string arrives as `op: change` with diff-match-patch text — JSON Patch extended by
  SurrealDB.
- Not yet: `auto_resubscribe=` / `on_reconnect=` (v0.21.0). Passing them raises `TypeError`.

## [0.19.2] - 2026-10-01

Security release for the locked dependencies. No library code changes; the published package
behaves exactly as 0.19.1.

### Security

- **urllib3 2.7.0 → 2.8.0** (GHSA-8988-9cw3-xx77, GHSA-vxq7-64xx-v4gw, GHSA-gh4c-6fx4-qh6g).
  urllib3 is reached at runtime through the SDK (`surrealdb` → `requests` → `urllib3`) and by
  the dev-only `docker` package.
- **tornado 6.5.8 → 6.5.9** (GHSA-c2m8-h5v5-343r, GHSA-chx6-46f5-w4vp, GHSA-3hv7-mjh2-fv65).
  Dev only: pulled in by `ipykernel` and `jupyter-client` for the example notebooks.

### Notes

- **This updates `uv.lock`, which protects CI and contributors, not your installation.** The ORM
  declares no bound on urllib3 (it never imports it; the SDK's `requests` does), so your
  environment keeps whatever urllib3 it resolved. If it is older than 2.8.0, upgrade it
  (`pip install -U "urllib3>=2.8.0"` or `uv lock --upgrade-package urllib3`).

## [0.19.1] - 2026-10-01

Tested against the new SurrealDB releases on both supported lines: **2.7.0** and **3.3.0**. No
library code changes; the published package behaves exactly as 0.19.0.

### Changed

- **SurrealDB test pins moved**: the 2.x line from 2.6.5 to **2.7.0**, the 3.x line from 3.2.4
  to **3.3.0**. The CI matrix, the README badge, compatibility table and behaviour table, and the
  ROADMAP follow the pins through `scripts/sync_surrealdb_pins.py`. 2.6.5 and 3.2.4 stay listed
  as compatible, tested up to v0.19.0.

### Notes

- **2.7.0 is indistinguishable from 2.6.5 for the ORM.** The full suite passes with the same
  3.x-only skips, and every divergence in the behaviour table was re-measured on it: no native
  transaction RPC, `WITH REFRESH` rejected, a live query on a missing table accepted, nothing
  sent after `kill()`, no `session` key in a live notification.
- **3.3.0 changes one server behaviour.** Signing in as a system user while a record session is
  open used to swap only the permissions, leaving `$auth`, `$access` and `$session.rd` on the
  record so `info()` kept reporting it. 3.3.0 clears all three and `info()` returns `None`.
  Nothing in the ORM depends on the old behaviour, and `invalidate()` remains the portable way
  to log a record user out. The test that pinned the old behaviour now pins each line's, keyed
  on the server's reported version so a further change goes red; it was checked on 3.3.0,
  3.2.4, 3.1.5 and 2.7.0. Every other 3.x divergence holds unchanged on 3.3.0.

### Fixed

- **The SurrealDB version monitor files its failure report again** (#188). `gh issue create`
  refuses the whole call when one label does not exist, and `needs-investigation` never did, so
  the 3.3.0 failure above was never reported. Labels are now created first, with an unlabelled
  issue as fallback.

### Dependencies

- ruff 0.16.7 → 0.16.9 (dev only, #190).

## [0.19.0] - 2026-09-30

Real-time, phase one: subscribe to a table and receive a notification whenever a record is
created, updated or deleted. This is the **raw** layer — notifications arrive as the server's
own envelope — that the typed `LiveQuerySet` planned for v0.20.0 will build on. Live queries
need a WebSocket connection and work on **both SurrealDB 2.6.x and 3.2.x**.

### Added

- **`QuerySet.watch()`** — an async context manager that subscribes to the model's table and
  kills the subscription on the way out, including when the body raises:

  ```python
  from surreal_orm_lite import LiveAction

  async with User.objects().watch() as stream:
      async for notif in stream:
          print(notif["action"], notif["result"])
          if notif["action"] == LiveAction.DELETE:
              break
  ```

  The handle exposes `live_id`, `table`, `is_active` and `stop()`, named to match the full
  ORM's `LiveModelStream` so the typed layer can present the same object.

- **`QuerySet.live()`** — the explicit form, returning the live query's `UUID` for callers that
  need to pass it around or kill it from elsewhere.

- **`SurrealDBConnectionManager.subscribe_live(uuid)`** — an async iterator over the raw
  notification envelopes (`action`, `id`, `record`, `result`, and `session` on 3.x). It is
  **not** a coroutine: call it without `await`. Buffering starts at the call rather than at the
  first iteration, so a write made in between is still reported, and several readers can
  subscribe to the same uuid.

- **`SurrealDBConnectionManager.kill(uuid)`** — stops a live query and ends every stream
  reading it. Idempotent: an unknown or already-killed uuid is a no-op, like the ORM's other
  cleanup-on-a-missing-target operations.

- **`LiveAction`** — a `StrEnum` (`CREATE`, `UPDATE`, `DELETE`, `KILLED`) that compares directly
  against the raw string in an envelope, so no magic values are needed.

- **`LiveStream`** and `LiveAction` are exported from the package root.

### Notes

- **Guards, not silent surprises.** `live()` refuses a non-WebSocket connection with a message
  naming the requirement, rather than letting the SDK's HTTP path raise a bare
  `NotImplementedError`. It also refuses a queryset carrying `filter()`, `select()`, `limit()`,
  `offset()`, `order_by()`, `fetch()`, `values()`, `annotate()` or `variables()` — a table-level
  live query cannot honour them, and quietly watching the whole table instead would be worse
  than an error. The filtered form arrives in v0.20.0. `objects(tx=)` is refused for the same
  reason: a live query has no transaction to join. `subscribe_live()` names the WebSocket
  requirement too, instead of blaming the SDK build.

- **Two measured 2.6.x / 3.x divergences, both normalised.** SurrealDB 3.x refuses to watch a
  table that does not exist while 2.6.x accepts and stays silent; the ORM turns the 3.x
  `NotFound` into a `SurrealDbNotFoundError` naming the table. After `kill()`, 3.x sends a final
  `KILLED` notification and 2.6.x sends nothing at all, which would leave a reader's `async for`
  hanging on 2.6.x; the ORM pushes its own end-of-stream marker so the loop terminates
  identically on both lines. Both rows are in the README behaviour table.

- **Why the ORM does not call the SDK's `subscribe_live()`.** Its generator body is
  `yield ret["result"]`, which discards the envelope and makes `CREATE`, `UPDATE` and `DELETE`
  indistinguishable. The ORM registers its own queue in the connection's public `live_queues`
  registry — the mechanism the SDK's own `subscribe_live()` uses — and keeps the whole envelope.
  Two tests guard this: one fails if the SDK drops the attribute, the other if the SDK ever
  starts yielding the envelope itself (at which point the ORM can drop its registry).

- **`diff` / JSON-Patch mode is deliberately not exposed.** The installed SDK accepts
  `live(diff=True)` but never puts the flag on the wire — `prep_live()` encodes only the table —
  so the argument would change nothing. v0.20.0 implements diff through `LIVE SELECT DIFF`,
  which was verified to work on both lines.

- **Polling is safe.** A read cancelled by `asyncio.wait_for(anext(stream), timeout)` loses
  nothing and leaves the stream alive, and `aclose()` stops one reader without killing the live
  query. The reader is a small iterator class rather than an async generator precisely because a
  cancelled generator finishes for good.

- **What ends a stream.** `kill()` does, leaving a `watch()` block does, and so does
  `close_connection()` / `close_all_connections()` — the first releases every reader on its
  loop before the socket goes, the second every reader on every loop, so an ordinary shutdown
  cannot leave a wedged task behind. `kill()` never opens a connection: leaving a `watch()`
  block after the connection closed does not silently reconnect. A
  WebSocket that drops on its **own** does not: the SDK's receive task absorbs the close
  without telling live-query subscribers, so the iterator stays suspended until you call
  `kill()` or close the connection. Automatic reconnect and resubscribe is v0.21.0.

- **An SDK without `live_queues` is refused, not degraded.** Yielding action-less envelopes
  would silently break every `notif["action"] == …` comparison, and a wrong answer is worse
  than a clear failure. The guard test catches such an SDK in CI instead.

- **Not yet**: notifications deserialized into model instances and filtered live queries
  (v0.20.0), and automatic resubscribe after a dropped WebSocket (v0.21.0).

## [0.18.0] - 2026-09-13

Let a model's **Python surface** differ from its **SurrealDB column surface**, mark the columns
the server owns, and ask for no row back when you do not need it. All three features are
client-side, so they behave **identically on SurrealDB 2.6.x and 3.x**.

### Added

- **Field aliases** — `Field(alias="password_hash")` renames the _column_, not the attribute:

  ```python
  class User(BaseSurrealModel):
      id: str
      password: str = Field(alias="password_hash")

  user = User(id="ada", password="secret")            # either name validates
  await user.save()                                   # stores password_hash
  await User.objects().filter(password="secret").exec()   # queries password_hash
  ```

  The alias is honoured at every boundary the ORM owns: writes (`save`/`update`/`upsert`/
  `merge`/`bulk_create`, `server_values=`, and the whole `atomic_*` family), read hydration
  (`exec()`, `refresh()`, the row a write returns), **every QuerySet clause** (`select`,
  `filter` including nested and negated `Q` objects, `order_by`, `values`, `fetch`,
  `bulk_update`, the `sum`/`avg`/`min`/`max` helpers and the aggregation objects given to
  `annotate()`), the DDL the ORM generates (`computed_field_ddl()`, and
  `AuthenticatedUserMixin`'s `access_ddl()`), and signal payloads (`update_fields` always names
  Python attributes). QuerySet filter rewriting is the piece the full ORM still documents as an
  open gap. A grouped `values()`/`annotate()` result is re-keyed to Python names on the way
  out.

  `patch()` and `QuerySet.patch()` rewrite each pointer's top-level segment (and a `move`/`copy`
  `from`) to the column, so `/password` and `/password_hash` both work; a whole-document
  operation has its object value re-keyed. Dotted paths translate their root in `Q` objects and
  aggregations exactly as in `filter(**kw)` — `Q(**{"address.city": …})` addresses `addr.city`.

- **`get_field_aliases()` / `to_db_field()` / `to_py_field()`** — the mapping, exposed as
  classmethods and cached per class.

- **`SurrealConfigDict(server_fields=[...])`** — columns the server owns (a
  `DEFINE FIELD … DEFAULT`, an event, a trigger). They are dropped from **create** payloads so
  the server's default applies, hydrated back from the row the write returns, and still
  writable when a caller names one explicitly. `get_server_fields()` returns them merged with
  the model's computed fields, which are server-owned by construction.

- **`merge(refresh=False)`** — for fire-and-forget updates, compile the write with `RETURN NONE`
  so the server sends no row back; the literal keyword arguments are applied locally instead.

### Changed

- `BaseSurrealModel` now sets `populate_by_name=True`. Strictly widening: an aliased field
  previously validated only under its alias, which made `User(password=…)` an error in the very
  code the alias exists to keep readable.

- `update()` and `upsert()` keep `server_fields` in their payload, while `save()` omits them.
  Not an inconsistency — omission means different things in the two statements. Probed on 2.6.5
  and 3.2.4 with identical results: a `DEFAULT` is a **create-time** default, so on
  `UPDATE`/`UPSERT … CONTENT` an omitted optional column is _deleted_ and an omitted required
  one fails outright (`Found NONE for field 'created_at' … but expected a datetime`). A uniform
  exclusion would have made `update()` destroy the very columns the feature protects.

- Hydration is consolidated: `_do_save()` and `refresh()` now route through `_apply_record()`,
  so the column → attribute rule lives in one place instead of four ad-hoc loops.

- **`merge()` costs one round-trip instead of two.** The SDK's `merge()` already answers with
  the merged row; the ORM used to discard it and `SELECT` the record again. The instance is now
  synced from that row, which also means server-side changes (a `VALUE` clause, an event) are
  hydrated just as before, and a missing record still raises. Inside an interactive transaction
  the row `tx.add()` returns is used the same way.

- `get_or_create()` / `update_or_create()` store a `server_fields` entry named as a criterion or
  in `defaults` on both branches. Before, the create branch let the server's `DEFAULT` win, so
  the next identical lookup missed the row and created a duplicate.

- A grouped `values()`/`annotate()` row never renames an annotation alias, and an alias that
  spells a grouped field (by name or by column) raises `ValueError` — the row could only hold
  one of the two values. A projection that fails model validation falls back to dicts keyed by
  Python names, matching the grouped branch.

### Fixed

- `merge()` on a table that was never created raised the SDK's raw `NotFoundError` on SurrealDB
  3.x, while 2.6.x raised the ORM's `SurrealDbError`. Both lines now raise `SurrealDbError`
  ("no record found").

### Notes

- **`refresh=False` forfeits the missing-record check.** That check _is_ the returned row, so a
  merge against a record that no longer exists becomes a silent no-op instead of a
  `SurrealDbError`, and a field computed by `server_values` keeps its stale value until the next
  `refresh()`. Keep the default whenever you need to know the write landed.

- Only a plain, symmetric `Field(alias=…)` is treated as a column rename. A separate
  `validation_alias` / `serialization_alias`, and in particular `AliasPath` / `AliasChoices`,
  describe something other than a renamed column and are left entirely to Pydantic. Write
  payloads are re-keyed through the ORM's own map rather than `model_dump(by_alias=True)`,
  which would honour a `serialization_alias` and write the row under a name `filter()`,
  `select()` and hydration never look for — landing the value in the database and making it
  unreachable through the ORM.

- **The alias map must be a bijection**, checked the first time it is used: an alias that
  collides with another field's name, or two fields sharing one column, raises `ValueError`.
  Either shape collapses two payload keys into one and silently drops a field.

- `AuthenticatedUserMixin`'s SIGNUP clause now also skips `server_fields`, for the same reason
  `save()` does — SIGNUP is a `CREATE`, so listing the column would overwrite the server's
  `DEFAULT` with `NONE` and force every caller to supply a value the server is meant to mint.

- `merge()` now reserves `refresh` alongside `tx`, `server_values` and `extra_vars`.

- No new SDK surface: nothing was added to `_sdk.py`. Verified on SurrealDB **2.6.5** and
  **3.2.4**; no test in this version is skipped on either line.

## [0.17.0] - 2026-09-11

Authenticate a **model**, not just the connection: declare a user model, let the ORM generate its
`DEFINE ACCESS` statement, and get **hydrated model instances** back from signup and signin —
each on its own short-lived connection, so the shared client's identity is never disturbed.

### Added

- **`AuthenticatedUserMixin`** — mix it into a `BaseSurrealModel` subclass for
  `signup()`, `signin()`, `authenticate()` and `refresh()`:

  ```python
  class User(AuthenticatedUserMixin, BaseSurrealModel):
      model_config = SurrealConfigDict(access_name="account")

      id: str | None = None
      email: str
      password: str
      name: str = ""

  await User.define_access()                 # once, at start-up

  result = await User.signup(email="ada@example.com", password="s3cret", name="Ada")
  result.user     # a User instance, with the id the server generated
  result.tokens   # AuthTokens

  me = await User.authenticate(result.tokens.access)   # token → current user
  ```

- **`AuthResult`** — a frozen, generic dataclass pairing `.user` with `.tokens`. Its repr
  delegates to `AuthTokens`, so no JWT reaches a log line, a traceback or an assertion diff.

- **`access_ddl()` / `define_access()`** — render and apply the model's authentication DDL,
  mirroring v0.14.0's `computed_field_ddl()` / `define_computed_fields()` pair. `access_ddl()`
  is pure, so it can be printed, diffed or fed to a migration; `define_access()` is idempotent
  and safe at start-up.

- **`SurrealDBConnectionManager.ephemeral_client()`** — a short-lived, anonymous client on the
  configured URL/namespace/database that never enters the per-event-loop cache.

- **Auth keys on `SurrealConfigDict`** — `access_name`, `identifier_field`, `password_field`,
  `auth_algorithm`, `auth_duration_token`, `auth_duration_session`, `auth_duration_grant` and
  `with_refresh`.

- **`SurrealDBConnectionManager.authenticate(token, *, refresh=None)`** — accepts the refresh
  token that belongs with _token_, for callers holding the matching pair. Omitted, behaviour is
  unchanged (any stored refresh token is dropped).

- **`SurrealDBConnectionManager.require_connection()`** — the "Connection not been set." check,
  now owned in one place instead of being re-spelled by each caller.

### Notes

- **Session isolation is the point.** v0.16.0's `signin()` re-identifies the process-wide client,
  which makes it unusable per-request in a concurrent server. Every method here runs on its own
  ephemeral connection instead, so the shared client keeps the identity `set_connection()` gave
  it. Pass `bind=True` to opt into the older, process-wide behaviour.

- **The `DEFINE TABLE` ships with the access method by default.** Without
  `PERMISSIONS FOR select WHERE id = $auth.id`, a signin succeeds and `$auth` is set yet the
  server returns no record — v0.16.0 documented that as this feature's sharpest gotcha, and an
  API that promises an instance cannot walk into it. When the record is unreadable anyway, the
  raised error names the missing permission instead of returning `None`.

- **The hydrated instance's password field holds the hash**, not the plaintext, because
  SurrealDB returns the stored record verbatim.

- **A configured `primary_key` drives the record id**: the SIGNUP clause targets
  `type::thing('<table>', $<pk>)`, so a signed-up record is addressable by
  `User.objects().get(...)` afterwards. Models must declare either an `id` field or a
  `primary_key`, as every ORM model must.

- **`signup()` validates the whole payload before the exchange.** A required field with no
  default used to be sent as `NONE`: SurrealDB created the account and issued tokens, and only
  then did hydration fail — leaving an orphaned record the caller never saw. Model defaults are
  now applied, and a genuinely missing field raises before any request.

- **`define_access()` normalises errors from a native interactive transaction too.** Those raise
  `SurrealDbError` rather than the SDK's `ServerError`, which previously slipped past the
  wrapper. The apply-DDL path is now shared with `define_computed_fields()`, so both benefit.

- **Same on both DB lines** for `access_ddl`, `define_access`, `signup`, `signin` and
  `authenticate`. **3.x only**: `with_refresh=True` and `refresh()` — SurrealDB 2.6.x cannot
  parse `WITH REFRESH`, so `define_access()` raises a message naming the requirement and those
  tests self-skip. Refresh tokens **rotate**: a spent one is rejected immediately.

## [0.16.0] - 2026-08-29

Authenticate the connection as a SurrealDB **record user** or a different **system user**, keep
that identity across reconnects, and read back who is signed in.

### Added

- **Five auth methods on `SurrealDBConnectionManager`** — `signin()`, `signup()`,
  `authenticate()`, `invalidate()` and `info()`, all native SDK primitives:

  ```python
  tokens = await SurrealDBConnectionManager.signup(
      access="account", variables={"email": email, "pass": password},
  )
  tokens = await SurrealDBConnectionManager.signin(
      access="account", variables={"email": email, "pass": password},
  )
  me = await SurrealDBConnectionManager.info(return_type=User)   # User | None
  await SurrealDBConnectionManager.invalidate()                  # back to the app identity
  ```

  `signin()` takes exactly one of three credential shapes: record access (`access=` +
  `variables=`), a system user (`username=` + `password=`), or a refresh exchange (`access=` +
  `refresh=`). `namespace`/`database` default to the configured connection's **for record
  access only** — a root user is defined at neither level, and sending them would turn a root
  signin into a database-user signin against a user the server does not have.

- **`AuthTokens`** — a frozen dataclass with `access` (guaranteed non-`None`) and `refresh`.
  Its `repr` redacts both values while still showing whether a refresh token is present, and
  there is deliberately **no `__str__` returning the JWT**, so a token cannot leak into a log
  line, a traceback or a pytest assertion diff by accident. Read `tokens.access` explicitly.

- **Session persistence** — the authenticated identity survives `reconnect()`, a dropped client
  and a new event loop: `get_client()` replays the stored token. Without it, a reconnect would
  silently hand back the configured root identity — a privilege change nobody asked for.

- **`SurrealDbAuthenticationError`** — one exception for every authentication failure. The SDK
  reports them inconsistently (the same wrong password is `NotFoundError` on 3.x and
  `InternalError` on 2.6.x; a malformed token is a client-side `ValueError` that never reaches
  a server), so the ORM normalises them all. It subclasses `SurrealDbError`, so existing
  `except SurrealDbError` handlers keep working.

- **Refresh-token exchange** (SurrealDB 3.x) — renew an access token without the password:

  ```python
  tokens = await SurrealDBConnectionManager.signin(access="account", refresh=stored_refresh)
  store(tokens.refresh)   # the previous one is now dead — see Notes
  ```

- **`is_authenticated()`, `get_session_token()`, `get_refresh_token()`** — introspection, for
  putting a JWT in a web session — and **`clear_session()`**, the local counterpart to
  `invalidate()`: it forgets the stored tokens without a server round trip and without
  touching the live session.

### Changed

- `_coerce_call_result` was generalised to `_coerce_result(value, return_type, subject)` so
  `info(return_type=)` reuses the one `TypeAdapter` path `call_function()` already used.
  Internal only; `call_function()`'s messages and behaviour are unchanged.

### Notes

- **Refresh tokens rotate.** A successful exchange kills the token it spent, immediately and
  permanently. Persist the new `tokens.refresh` before the next request: dropping it logs the
  user out for good, with nothing raised at the call site to warn you.
- **Refresh tokens are SurrealDB 3.x only.** `DEFINE ACCESS … WITH REFRESH` does not parse on
  2.6.x ("expected the experimental bearer access feature to be enabled"), so `tokens.refresh`
  is always `None` there and their tests self-skip. Everything else behaves identically on both
  lines.
- **`invalidate()` returns the connection to its configured identity** rather than leaving it
  anonymous. The SDK's own `invalidate()` leaves the session anonymous, and since the manager
  caches one client per event loop, every later ORM call on that connection would fail.
- **`info()` returns `None` unless the record's table grants that record user `select` on
  itself.** The signin succeeded and `$auth` is set, but the server returns nothing — quiet and
  easy to misread. Grant e.g. `PERMISSIONS FOR select WHERE id = $auth.id`. Because the cause is
  a permission, a `None` is passed through untouched even when `return_type=` is given.
- **Signing in as a system user does not end a record session.** SurrealDB swaps the
  permissions but leaves `$auth` pointing at the record, so `info()` keeps reporting it. Only
  `invalidate()` really logs a record user out. Identical on both lines.
- **`namespace=` and `database=` go together** for record access. Pairing one explicit value
  with the configured default would point the signin at a target the caller never named, so
  half-explicit raises `ValueError`.
- **`set_connection()` drops any stored session token**, the same way it already drops the
  cached stored-function signatures: a JWT is bound to a server, namespace, database and
  access method, so it must not survive a re-point.
- **Auth changes the identity of the whole connection.** One client is cached per event loop and
  every model shares it, so an auth call affects every subsequent ORM operation — not just the
  caller's.

## [0.15.0] - 2026-08-25

Call custom server-side functions (`DEFINE FUNCTION fn::…`) from the ORM.

### Added

- **`SurrealDBConnectionManager.call_function()`** — invoke a stored function, with arguments
  bound as query parameters:

  ```python
  acquired = await SurrealDBConnectionManager.call_function(
      "fn::acquire_lock", [table_id, pod_id, 30],
  )
  ```

  The `fn::` prefix is added when absent, and nested namespaces (`fn::billing::total`) work.
  The function _name_ is interpolated (SurrealQL takes no bound parameter in call position) and
  is therefore validated as an identifier path first — an invalid name raises `ValueError`
  before any query is issued. Argument _values_ are never interpolated.

- **Named arguments via `params=`** — SurrealQL function arguments are positional, so the ORM
  reads the function's declared signature (`INFO FOR DB`, cached per URL/namespace/database)
  and orders them for you. The mapping's own order is irrelevant:

  ```python
  await SurrealDBConnectionManager.call_function(
      "fn::acquire_lock", params={"pod_id": pod_id, "table_id": table_id, "ttl": 30},
  )
  ```

  A stale signature self-heals: on a key mismatch the entry is dropped and re-read once, so a
  function redefined at runtime resolves instead of failing. The one exception is a function
  redefined with the same parameter names in a **different order**, which a mapping carries no
  order to detect — call `clear_function_signature_cache()` after such a change. `args` and
  `params` together raise `ValueError`.

- **`return_type=`** — coerce the result with one `pydantic.TypeAdapter` pass, so a model, a
  dataclass, a scalar and `list[Model]` are all supported. A mismatch raises
  `SurrealDbValidationError`.

- **`tx=`** — a stored function called inside `async with transaction()` runs **inside** that
  transaction on both DB lines. Without it the call would run outside an open transaction and
  silently break atomicity, which matters because stored functions typically mutate state.

- **`BaseSurrealModel.call_function()`** — the same call for code organised around models. A
  stored function is not table-bound, so it delegates verbatim.

- **`clear_function_signature_cache()` / `function_signature_cache_size()`** on the connection
  manager, for redefining functions out-of-band. The cache is cleared by `set_connection()` and
  `unset_connection()` too, since signatures belong to the server that declared them.

### Notes

- **The ORM emits the bare call form `fn::name($_fnarg0);`, never `RETURN fn::name(…)`.** A
  `RETURN` inside a `BEGIN … COMMIT` batch terminates the transaction early **and silently**:
  the statement queued after it is reported `status: OK` and never executes. Verified on both
  2.6.5 and 3.2.4. The bare form returns the same value in every context without that hazard,
  and a regression test pins it.
- **`tx=` return value differs by strategy** (the v0.9.0 contract): interactive transactions
  (WebSocket + SurrealDB 3.x) return the function's value immediately; buffered ones
  (SurrealDB 2.6.x or HTTP) queue the call and return `None` until commit. Combining
  `return_type=` with a buffered `tx=` raises `ValueError` rather than silently returning
  `None`.
- **Signature reads bypass the transaction** — `params=` therefore works inside a buffered
  transaction, where reads are otherwise forbidden. A schema read needs no transactional
  isolation.
- **A parameter name that collides with a reserved word is quoted differently by each line.**
  SurrealDB 2.6.x reports `fn::f($by: int)` as ``$`by` `` while 3.x leaves it bare; the
  signature parser accepts `$name`, ``$`name` `` and `$⟨name⟩`.
- The call itself behaves identically on SurrealDB 2.6.x and 3.x.
- Passing a bare `str` as `args` raises `TypeError` rather than binding one argument per
  character.
- **A missing function is reported at a different moment per strategy.** Outside a transaction
  and inside an interactive one it raises `SurrealDbNotFoundError` at call time. Inside a
  buffered one the call is only queued, so the failure cannot be known until commit and arrives
  as `SurrealDbError` from the transaction layer.
- **Deferred**: a `define_function()` DDL helper. Declaring is DDL and belongs with the other
  `schema.py` helpers planned for **v0.31.0**; v0.15.0 is about calling. Declare a function with
  `client.query("DEFINE FUNCTION fn::… { … };")` meanwhile.

## [0.14.5] - 2026-08-23

Connection correctness: the client cache is now per event loop.

### Fixed

- **A cached client is no longer handed to a different event loop.**
  `SurrealDBConnectionManager` kept a single client in a class attribute with no notion of the
  loop that created it. A SurrealDB WebSocket client is bound to its loop, so the second of two
  successive `asyncio.run(...)` calls in the same process got a client belonging to a loop that
  no longer existed and failed with `got Future attached to a different loop` — or hung outright,
  which is what writing the regression test revealed
  ([#163](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/163)). The cache is keyed by
  the running loop, so each loop gets its own connection. Also reached by a sync wrapper that
  spins a loop per call, a test suite with per-test loops (how it was found), and any framework
  that runs handlers on more than one loop.
- **Two live loops no longer evict each other.** Threads or multi-loop servers each keep their
  own client instead of stealing the single cached one.

### Added

- **`close_all_connections()`** — tear down every cached client whatever loop created it.
  `unset_connection()` now uses it. A client belonging to another _live_ loop is dropped rather
  than awaited, since it cannot be closed from here.

### Changed

- **`close_connection()` closes the running loop's client only.** Closing another loop's client
  from here is both impossible and wrong. Single-loop applications see no difference.
- **`is_connected()` answers for the loop asking.** Inside a running loop it reports that loop's
  client; called outside one it reports whether any loop still holds a client.

### Notes

- Entries whose loop is closed are pruned on the next `get_client()`. The stale client is dropped
  rather than closed — `close()` would have to be awaited on a loop that is already gone — and its
  socket is released when the loop is finalised.
- The public API is unchanged apart from the new `close_all_connections()`. A long-lived
  single-loop application behaves exactly as before: one loop, one connection.
- Loop binding is an asyncio/SDK property, not a server one, so this is identical on 2.6.x and
  3.x. Example notebook: `examples/v0.14.5_event_loops.ipynb`.

## [0.14.4] - 2026-08-23

Follow-up correctness release: record-id lookups. Reproduced against live SurrealDB **3.2.4
and 2.6.5** before the fix and verified on both afterwards.

### Fixed

- **`filter(id=...)` matches again.** A record's `id` column holds a native `RecordID`, but
  the `WHERE` builder bound whatever Python value the caller passed — so `filter(id="alice")`
  compiled to `id = $_f0` with a plain string, and SurrealDB does not consider a `RecordID`
  equal to the string form of its identifier. The lookup matched nothing, with no error and
  no warning ([#159](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/159)). It now
  goes through `coerce_record_id()`, which accepts a `RecordID`, an `int`, a bare identifier
  (`"alice"`), a backticked one (``"`5`"``) and the full `"User:alice"` form — the last
  unwrapped only when the prefix names the queried table, since `"Other:d"` may genuinely be
  the stored string id. The value's **Python type** still decides which record is addressed:
  `filter(id=5)` is the integer record id, `filter(id="5")` the string one.
- **`get_or_create(id=…)` / `update_or_create(id=…)` converge.** They inherit the same lookup,
  so the criterion never matched: every call took the create path and the second one raised
  `already exists`. The v0.14.3 regression test had to work around this by filtering on `name`.
- **`get()` accepts a non-string id.** It built `RecordID(table, str(id_item))`, so `get(5)` on
  a model declared `id: int` looked for the _string_ record id `"5"` and raised
  `SurrealDbNotFoundError`. It now shares the helper with `filter()`, so the two agree.
- **A text lookup on `id` fails loudly.** `contains`, `not_contains`, `containsall`,
  `containsany`, `startswith`, `endswith`, `like`, `ilike`, `match` and `regex` raise
  `ValueError` naming the column instead of returning an empty set — a record id is not a
  string. The message points at `string::contains(record::id(id), …)` in a raw query for
  callers who really want the textual form.

### Changed

- `build_filter_condition()` and `Q.to_sql()` take an optional `record_table` argument. The
  QuerySet supplies it, so `filter()`, `Q` trees and the `*_or_create` lookups all route
  through one rule. Omitted — a `Q` compiled outside a QuerySet — nothing is coerced, so no
  existing call changes meaning.

### Notes

- An aliased primary key (`model_config = {"primary_key": "code"}`) stores that alias as an
  **ordinary column** next to the RecordID `id`, so `filter(code="abc")` is a plain string
  comparison and is deliberately left untouched. Only the column literally named `id` is coerced.
- Identical behaviour on 2.6.x and 3.x; example notebook:
  `examples/v0.14.4_record_id_lookups.ipynb`.

## [0.14.3] - 2026-08-23

Correctness release: the eight findings of [issue #156](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/156)
(seven reported, an eighth found while fixing the seventh). Every one was reproduced against a
live SurrealDB **3.2.4 and 2.6.5** before a line of the fix was written, and behaves identically
on both lines afterwards. No documented behaviour was removed.

### Added

- **`Var("name")` — an explicit reference to a bound query variable in a filter**, and the
  **`"$$literal"` escape**. `filter(name="$admin")` used to compile to `name = $admin`, an
  unbound reference that matched nothing and said nothing, which made a literal starting with
  `$` impossible to express and turned any user-supplied string into an accidental variable
  reference. Write `Var("min_age")` to reference a variable and `"$$admin"` to match the
  literal `"$admin"`. The bare `"$x"` form still works and now raises a `DeprecationWarning`,
  so the default can be flipped (strings always literal) in a later minor.

### Changed

- **SurrealDB pinned to 3.2.4** (was 3.2.3) on the primary line; the CI matrix now runs
  `v2.6.5` and `v3.2.4`. Detected and validated by the new 3.x version monitor rather than by
  hand — the full suite passes against 3.2.4 on Python 3.11 → 3.14.

### Fixed

- **`first()` no longer mutates the queryset.** It set `self._limit = 1` and never restored it,
  so every later use of that queryset — including a plain re-`exec()` — silently returned at
  most one row. It now saves and restores the limit exactly as `exists()` already did: a
  terminal operation never changes the queryset it was called on.
- **`update_or_create()` / `get_or_create()` no longer write undeclared keys.** A key absent
  from the model was dropped by Pydantic on the _create_ path but written straight into the row
  by `merge()` on the _update_ path, so one call produced two different stored schemas depending
  on whether the record happened to exist. Both paths now raise `SurrealDbError` naming the
  offending keys, before any write. Models that opt in with
  `model_config = ConfigDict(extra="allow")` keep them, on both paths.
- **User-authored SurrealQL is sent verbatim.** `raw_query()` and `objects().query()` piped the
  caller's query through a helper that rewrote `'$word'` → `$word` _anywhere_ in the text,
  including inside the caller's own string literals: `WHERE name = '$admin'` became a reference
  to an unbound variable and returned nothing. The helper now runs only on ORM-compiled queries,
  where every value is bound and no user data reaches the text.
- **`upsert()` reports `created` truthfully.** It was hardcoded to `True`, so the `post_save`
  signal claimed a creation on every replacement. The statement now returns both row states
  (`UPSERT … RETURN $before AS before, $after AS after`) and `created` is read from `$before` —
  same single round-trip, on 2.6.x and 3.x alike. REPLACE semantics are unchanged.
- **A record id that looks numeric is reachable again.** `save(id="1")` stores the _string_
  record id `"1"`, but an unquoted `Model:1` is the _integer_ one in SurrealQL — so `relate()`,
  `get_related()`, `traverse()` and `remove_relation()` addressed a record the ORM had never
  written and silently traversed nothing. Such ids are now backtick-quoted (`` Model:`1` ``).
  The quoting keys off the id's **Python type**, not its textual shape, so a model with
  `id: int` keeps the integer record id `Model:5` that `save()` actually wrote. A `"table:id"`
  string passed as a relation target is quoted by the same rule, so `a.relate(edge, "M:1")`
  and `M(id="1")` name the same record; pass a `RecordID` (or the model instance) to target an
  integer record id, and an id written with backticks is kept verbatim. A backtick inside an id
  is now rejected instead of producing malformed SurrealQL.
- **`get_related(model_class=)` costs one round-trip instead of two.** It fetched record ids and
  then issued a second `SELECT * FROM $ids`; the records are now projected in the traversal
  itself (`->edge->?.*`). Same results, verified on both lines.
- **`raw_query()` and `objects().query()` warn on a multi-statement query.** The SDK returns
  only the first statement's rows and says nothing about the rest, so half the output vanished
  silently. Detection is quote- and comment-aware (`--`, `//`, `#` and `/* … */`), so a `;`
  inside a string literal or a comment is not mistaken for a separator, and a trailing comment
  does not count as a statement of its own.
- **The `"$$literal"` escape round-trips through `get_or_create()` / `update_or_create()`.** The
  escape was decoded only when compiling the `WHERE` clause, so `get_or_create(name="$$admin")`
  looked for `"$admin"` but stored `"$$admin"` — it never converged and created a duplicate row
  on every call. The create payload now reads values exactly as the lookup does. A query-variable
  reference (`Var(...)` or `"$x"`) as an equality criterion raises `SurrealDbError` instead of a
  Pydantic error: its value lives on the server and cannot be written back — pass the literal in
  `defaults=`.
- **Warnings point at the caller.** The `DeprecationWarning` on `"$x"` filter values and the
  multi-statement warning reported a frame inside the ORM, and Python's default filters only
  surface a `DeprecationWarning` raised from `__main__` — so the deprecation runway reached
  nobody. The reported frame is now the first one outside the package.

- **Dependabot PRs can be tested again.** The readiness probe piped SurrealDB's `/health` into
  `grep -q "OK"`, but that endpoint answers `200` with an _empty body_ — the match could never
  succeed, so every probe ran its full retry budget and then declared a server that had been up
  for a minute unhealthy. Nothing acted on the verdict, so it stayed invisible: `ci.yml` merely
  `break`-ed and ran the suite anyway (passing, against the server it had just given up on), at a
  cost of ~30s on each of the eight matrix jobs. #134 then wrapped the same loop in `start_one`,
  which _returns 1_ on timeout — turning the latent bug into a hard failure on the next Dependabot
  PR (#150) and stalling the auto-merge chain. Both probes now key on the status code (`curl -sf`,
  matching `devops/wait-for-healthy.sh` in the full ORM), dump `docker logs` when a server really
  does not come up, and fail the step instead of falling through. Guarded by
  `tests/test_workflow_health_probe.py`.
- **The SurrealDB version monitors work again, and now cover both lines.** The 2.x monitor had
  failed on every scheduled run for seven weeks: it read the primary pin, compared it to the
  latest 2.x release, and so proposed a downgrade every day — wedging permanently on the
  branch left behind by the first attempt. It now reads `.surrealdb-version2x`, compares
  version-aware, refuses to emit a downgrade, and recovers from a stale branch.
- **Security patches can actually be released.** The ORM version bump rode in the same PR as
  the SurrealDB update, so the squash landed under a `security(deps):` title and `auto-tag`
  never fired — the bump reached `main` and was never published. It is now a separate
  `chore(release):` PR, as the Dependabot flow already did.
- **The 3.x line is monitored at all.** Only the legacy 2.x line was watched, so 3.2.1 through
  3.2.4 shipped unnoticed. The new monitor resolves from Docker Hub, because SurrealDB
  publishes 3.x patches as images without a matching GitHub release (the releases API stops at
  v3.2.0 while Docker Hub carries v3.2.4).

## [0.14.0] - 2026-07-26

### Added

- **Computed fields — `Computed[...]` → `DEFINE FIELD … VALUE`**: declare a field SurrealDB
  derives from other fields on **every** write. Where `SurrealFunc` (v0.13.0) evaluates an
  expression for one write, a computed field attaches it to the schema, so it applies to every
  write on the table — including ones the ORM never sees. `Computed[T]` is the annotation and
  `computed("<expr>")` the default; splitting them keeps the field statically typed as
  `T | None` under mypy and pyright.

  ```python
  from surreal_orm_lite import BaseSurrealModel, Computed, computed

  class Player(BaseSurrealModel):
      id: str
      first_name: str
      last_name: str
      full_name: Computed[str] = computed("string::concat(first_name, ' ', last_name)")

  await Player.define_computed_fields()
  player = await Player(id="ada", first_name="Ada", last_name="Lovelace").save()
  player.full_name  # "Ada Lovelace" — computed by the server
  ```

- `Model.get_computed_fields()` — `{field: expression}` for the model, inherited fields included.
- `Model.computed_field_ddl(overwrite=True)` — render the `DEFINE FIELD` statements without
  touching the database (printable, diffable, pipeable into a migration).
- `Model.define_computed_fields(overwrite=True, tx=None)` — apply them; idempotent, so it is
  safe to call at application start-up.

### Changed

- Computed fields are excluded from every write payload (`save`, `upsert`, `update`, `merge`,
  `bulk_create`) — the server owns the value.
- Naming a computed field in `merge()`, `save(server_values=)`, `patch()`, `QuerySet.patch()`,
  `QuerySet.bulk_update()` or any `atomic_*` helper now raises `ValueError` instead of being
  silently discarded by the server. JSON Patch documents are checked on the top-level pointer
  segment, and on `from` as well for `move`, which removes its source.
- **Supported SurrealDB lines are now 2.6.x and 3.2.x** — 3.1.x is dropped. The CI matrix runs
  `v2.6.5` and `v3.2.3` (was `v2.6.5` + `v3.1.3`). 3.2 is covered directly rather than assumed
  equivalent to 3.1, because SurrealDB 3.2.0 regressed uncorrelated sub-SELECTs combining
  `ORDER BY` and `LIMIT` ([SurrealDB-ORM#147](https://github.com/EulogySnowfall/SurrealDB-ORM/issues/147));
  ORM-lite never emits that shape. The suite is still run against 3.1.5 locally as a
  backward-compatibility check (`RETRO=1`), but a regression there no longer blocks a release.
- **One version pin per supported line.** `.surrealdb-version` now pins the primary (latest)
  line — `3.2.3` — and the new `.surrealdb-version2x` pins the legacy 2.x line (`2.6.5`). The
  single file could not serve both consumers: the 2.x version monitor reads and writes it (and
  saw a permanent phantom update while it held a 3.x value), while the Dependabot gate uses it
  to pick a test container. Dependabot now tests **both** lines, and the monitor's `sed` on the
  CI matrix targets only the `v2.*` entry — it assumed a single-element list and had silently
  matched nothing since the matrix gained a second line.
- **`save()` with an explicit id now reads back the model's computed fields** from the row the
  server returned; it previously discarded that row entirely, so a computed value never reached
  the instance. The sync is deliberately limited to computed fields — a model that declares none
  behaves exactly as it did in v0.13.0.

### Notes

- **Identical on SurrealDB 2.6.x and 3.x** — verified on 2.6.5 and 3.2.3 (and on 3.1.5 as a
  backward-compatibility check). No 3.x-only primitive and no capability gate, so no test in
  this feature skips on any line. The DDL syntax, the recompute triggers, the precedence over
  client-sent data and the rollback of DDL in a cancelled transaction all matched exactly. The only divergence is the raw SDK exception for an invalid expression
  (`InternalError` on 2.6.x, `ValidationError` on 3.x); both are normalised to `SurrealDbError`.
- SurrealDB evaluates computed fields in **alphabetical field-name order**, not declaration
  order: a computed field reading another must sort after it (`subtotal` → `total` works,
  `z_sub` → `a_total` fails at write time).
- A computed field is a **server-enforced** invariant: a client that sends its own value for one
  cannot override the expression, even bypassing the ORM entirely.
- No `TYPE` clause is emitted — SurrealDB infers an optional type. Explicit field types remain a
  later roadmap item.
- Expressions are inlined into DDL and cannot reference bound parameters. Build them only from
  developer-controlled text — the `SurrealFunc` trust model, whose validation they reuse.
- **`tests/typing_examples.py`** is type-checked in CI as its own mypy invocation, so the public
  API is verified to type-check from a _user's_ side — `mypy src/` alone cannot catch that, since
  no model in `src/` declares a computed field.

## [0.13.0] - 2026-07-26

### Added

- **`SurrealFunc`** — a marker wrapping a raw SurrealQL expression so a value is computed
  **by the server** instead of in Python, plus `SurrealFunc.call(fn, *args)` to build a call
  from a function name and raw argument fragments.

  ```python
  from surreal_orm_lite import SurrealFunc, SurrealTimeFunction

  SurrealFunc("time::now()")
  SurrealFunc.call(SurrealTimeFunction.NOW)  # → SurrealFunc("time::now()")
  ```

- **`server_values=` / `extra_vars=` on `save()` and `merge()`** — `server_values` maps field
  names to `SurrealFunc`s (evaluated server-side); `extra_vars` binds the query variables those
  expressions reference, so user input stays a parameter and never touches the query text.
  The write is compiled to `CREATE $rid SET …` / `UPDATE $rid SET …` and the returned row syncs
  the instance, so a server-computed value is readable right after the call.

  ```python
  await player.save(server_values={"joined_at": SurrealFunc("time::now()")})

  await user.save(
      server_values={"password_hash": SurrealFunc("crypto::argon2::generate($password)")},
      extra_vars={"password": raw_password},
  )

  # merge() stays a partial update; a server value overrides a same-named keyword
  await user.merge(plan="pro", server_values={"updated_at": SurrealFunc("time::now()")})
  ```

- **Curated function-name enums** — `SurrealTimeFunction`, `SurrealMathFunction`,
  `SurrealStringFunction`, `SurrealArrayFunction`, `SurrealCryptoFunction`,
  `SurrealRandFunction` (and the `SurrealFunction` `StrEnum` base to define your own).
  **Every catalogued member is executed against both SurrealDB 2.6.5 and 3.1.3 by the test
  suite**, so autocompletion only offers names that work on both lines.

- **`build_set_clause()` / `merge_extra_vars()`** in `utils.py` — the pure compilers behind the
  feature (functions inlined, all other values bound as `$_sv_<field>`, `extra_vars` collision
  detection), unit-testable without a database.

### Notes

- **Identical on SurrealDB 2.6.x and 3.x — by design.** The feature compiles to portable
  SurrealQL and uses only functions verified on both lines. Names that diverge between the
  lines are deliberately excluded from the enums (`rand::guid` is 2.6-only; `type::is::*` was
  renamed `type::is_*` in 3.x) — pass those as a plain string if you target a single line.
- **`CONTENT $data SET …` is not valid SurrealQL** (verified on both 2.6.5 and 3.1.3), so a
  save carrying `server_values` compiles the _whole_ write as one `SET` clause rather than
  mixing the native SDK data path with an expression.
- **Security**: the `SurrealFunc` expression is inlined verbatim — build it only from
  developer-controlled text. Field values and `extra_vars` are always bound parameters (the
  injection boundary); `server_values` keys are validated as identifiers. `SurrealFunc` rejects
  `;` as a guard against accidental statement chaining, which is not a sanitizer.
- **In a transaction**: on the interactive strategy (WebSocket + 3.x) the returned row is
  applied immediately, so the instance sees the computed value inside the block. On a buffered
  transaction (HTTP / 2.6.x) the statement runs at commit, so the instance keeps its previous
  value for computed fields until you `refresh()` — the same caveat as `merge(tx=)`. Auto-id
  `save(tx=)` still requires the interactive strategy (unchanged v0.8.0 rule).
- **`merge(server_values=)` on a record that does not exist raises `SurrealDbError`** ("Can't
  merge data, no record found."), matching the native `merge()` path — server-side an `UPDATE`
  matching nothing is not an error, and the two DB lines even disagree on the missing-table case
  (SurrealDB 3.x raises `NotFound`, 2.6.x returns no rows), so both are normalised to that one
  ORM error. Inside an interactive transaction the statement runs at `merge()` time, so the same
  error is raised there (and rolls the transaction back); a buffered transaction runs nothing
  before commit, so the miss is not knowable and the statement simply matches no record.
- Signals are unchanged: `save(server_values=)` emits `pre_save`/`around_save`/`post_save`, and
  `merge(server_values=)` emits `pre_update`/`around_update`/`post_update` with `update_fields`
  listing both the literal and the computed fields.
- `server_values` and `extra_vars` join `tx` as reserved keyword names on `merge()`.

## [0.12.0] - 2026-06-09

### Added

- **`retry_on_conflict(...)`** — an async decorator that re-runs a function on a retryable
  SurrealDB transaction conflict, with exponential backoff + jitter. Retries **only** real
  conflicts; any other exception propagates immediately. Total attempts = `max_retries + 1`;
  after exhaustion the conflict is re-raised. Mirrors the full ORM's signature.

  ```python
  from surreal_orm_lite import retry_on_conflict, SurrealDBConnectionManager

  @retry_on_conflict(max_retries=3, base_delay=0.05, max_delay=2.0, backoff_factor=2.0)
  async def transfer(src_id, dst_id, amount):
      async with SurrealDBConnectionManager.transaction() as tx:
          src = await Account.objects(tx=tx).get(src_id)
          dst = await Account.objects(tx=tx).get(dst_id)
          await src.merge(tx=tx, balance=src.balance - amount)
          await dst.merge(tx=tx, balance=dst.balance + amount)
  ```

- **`SurrealDbConflictError`** — a `SurrealDbError` subclass representing a retryable
  transaction conflict. A conflict now surfaces as this single type on **both** transaction
  strategies (buffered and interactive), so it can be caught uniformly.

- **`is_conflict_error(exc)`** — the public predicate `retry_on_conflict` uses; returns `True`
  for a retryable conflict (walking the exception's `__cause__` / `__context__` chain),
  exported for callers who want their own retry loop.

### Changed

- **`Transaction.raise_for_status`** now surfaces the real root cause of a failed transaction
  instead of the generic "The query was not executed due to a failed transaction" filler (on
  SurrealDB 3.x every rolled-back statement is tagged `NotExecuted`, so the real cause is a
  sibling statement), and raises `SurrealDbConflictError` when that cause is a retryable
  conflict. The pre-existing `already exists` / generic-rollback behaviour is unchanged.

### Notes

- **Conflict detection is anchored on SurrealDB's own retryable marker** — the phrase
  "This transaction can be retried" (present on both 2.6.5 and 3.1.3). It is deliberately
  **narrower** than the full ORM (which also matches `"failed transaction"` / bare `"conflict"`):
  those over-match the non-retryable "not executed due to a failed transaction" filler and would
  cause a duplicate-key failure inside a transaction to be retried pointlessly.
- **Same exception type on both DB lines, different conflict frequency.** A conflict is a
  `SurrealDbConflictError` on 2.6.x and 3.x alike. On 3.x (optimistic MVCC) conflicts are the
  normal failure mode; on 2.6.x the engine serialises more, so conflicts are rarer (reproducible
  under high concurrency). Verified empirically on 2.6.5 and 3.1.3.
- `retry_on_conflict` decorates **async** functions only. The `jitter` flag (default `True`,
  set `False` for deterministic tests) is the only addition to the full ORM's signature.

## [0.11.0] - 2026-06-07

### Added

- **`model.patch()`** — apply a JSON Patch (RFC 6902) to a single record, backed by the SDK's
  native `patch()` (`UPDATE rid PATCH $data`). Requires an explicit id; supports `tx=`.

  ```python
  await user.patch([
      {"op": "replace", "path": "/age", "value": 26},
      {"op": "add", "path": "/tags/-", "value": "premium"},
      {"op": "remove", "path": "/settings/notifications"},
  ])
  ```

- **Atomic field/array helpers** on the model instance — each compiled to one atomic
  server-side `UPDATE … SET`, so concurrent callers don't clobber each other. All support `tx=`:

  ```python
  await post.atomic_append("tags", "python")    # array::append — duplicates allowed
  await post.atomic_set_add("editors", "alice")  # array::add     — no duplicate (set)
  await post.atomic_remove("tags", "spam")       # array::complement — removes ALL "spam"
  await counter.atomic_increment("views")        # += 1 (default); negative to decrement
  ```

- **List-valued atomic helpers** — `atomic_append_many` / `atomic_set_add_many` /
  `atomic_remove_many` apply many values in one round-trip (compiled to `array::concat` /
  `array::add` / `array::complement`; an empty list is a safe no-op). All support `tx=`.

  ```python
  await post.atomic_append_many("tags", ["python", "orm"])
  await post.atomic_set_add_many("editors", ["alice", "bob"])
  await post.atomic_remove_many("tags", ["spam", "draft"])
  ```

- **`QuerySet.patch()`** — apply a JSON Patch to every row matching the filters (whole table if
  unfiltered); returns the affected count. Participates in `objects(tx=)`.

  ```python
  n = await User.objects().filter(status="trial").patch(
      [{"op": "replace", "path": "/plan", "value": "free"}]
  )
  ```

- **JSON Patch / JSON Pointer validators** in `utils.py` (`validate_patch_operations`,
  `validate_json_pointer`) for fast, clear errors.

### Notes

- **Identical on SurrealDB 2.6.x and 3.x — by design.** The atomic helpers deliberately use the
  version-portable SurrealQL **functions** `array::append` / `array::add` / `array::complement`
  (and numeric `+=`) rather than the bare `+=` / `-=` array operators, whose semantics diverge
  between server lines (`arr -= v` removes _all_ occurrences on 3.x but only the _first_ on
  2.6.x; array `+=` appends _with_ duplicates on both, so it is not set-add). Verified
  empirically on 2.6.5 and 3.1.3.
- `patch()` and the atomic helpers emit **no signals** (unlike `merge`/`save`) — they are
  low-level atomic primitives. Use `merge()` / `save()` when you need lifecycle hooks.
- Self-sync: non-transactional and interactive (3.x) calls apply the server's returned row to
  the instance; in a **buffered** transaction (HTTP / 2.6.x) the result is unknown until commit,
  so the instance is left stale — `refresh()` it if needed (same caveat as `merge(tx=)`).
- Operations are bound as query data (never string-interpolated); atomic-op field names are
  validated. JSON Patch validation is for clear errors, not the injection boundary.
- `atomic_increment` accepts a `decimal.Decimal` amount for exact arithmetic (e.g. money);
  adding a `Decimal` to an int/float field coerces the stored field to SurrealDB `decimal`.
- A failed JSON Patch `test` op aborts the **whole** patch server-side (no op applies) and
  raises the SDK's `ServerError` — RFC 6902 compare-and-set / optimistic concurrency without a
  transaction. Identical on 2.6.x and 3.x.

## [0.10.0] - 2026-06-07

### Added

- **`model.upsert()`** — insert-or-replace by explicit id, backed by the SDK's native
  `upsert()` (`UPSERT $record CONTENT $data`, full REPLACE). Supports `tx=` (buffered onto
  the transaction).

  ```python
  user = User(id="alice", name="Alice", status="active")
  await user.upsert()  # CREATE if absent, full REPLACE if present
  ```

- **`QuerySet.update_or_create()` / `QuerySet.get_or_create()`** — Django-style
  criteria-based create-or-update returning `(instance, created)`. Writes route through
  `save()` / `merge()`, so lifecycle signals fire, SDK errors normalise to `SurrealDbError`,
  and the primary key anchors record identity. `update_or_create` does a **partial merge** on
  update — fields outside the criteria/defaults are preserved — while `get_or_create` returns
  an existing match untouched. Non-`exact` lookups (e.g. `name__contains`) drive the lookup
  but are not written. A SELECT-guard raises an explicit `SurrealDbError` when the criteria
  match more than one record.

  ```python
  user, created = await User.objects().update_or_create(
      email="alice@example.com", defaults={"name": "Alice"}
  )
  user, created = await User.objects().get_or_create(
      email="bob@example.com", defaults={"name": "Bob"}
  )
  ```

### Notes

- Behaviour is **identical on SurrealDB 2.6.x and 3.x** (no 3.x-only primitive): the native
  `upsert()` call and the SELECT-guard work the same on both lines (verified by spike).
- `upsert()` is REPLACE, not merge: fields omitted from the model are dropped. Use `merge()`
  for partial updates.
- `update_or_create` / `get_or_create` do the lookup and the write in two round-trips
  (SurrealDB 2.6.x exposes no row-locking primitive here), so a small race window exists
  under concurrent writers — the same non-atomic fallback Django documents.
- Under `objects(tx=)`, `update_or_create` / `get_or_create` participate in the transaction on
  SurrealDB 3.x (interactive); a buffered 2.6.x transaction raises on their lookup read
  (reads inside a buffered transaction are unsupported — consistent with other QuerySet reads).

## [0.9.0] - 2026-06-07

### Added

- **Transactions ORM — QuerySet & interactive strategy**: `objects(tx=)` plumbing routes
  `QuerySet` reads (`get`, `first`, `all`, `exec`, `count`, `exists`, aggregations) and bulk
  operations (`bulk_create`, `bulk_update`, `bulk_delete`) through the transaction.
  Example:

  ```python
  async with SurrealDBConnectionManager.transaction() as tx:
      users = await User.objects(tx=tx).filter(status="active").exec()
      await User.objects(tx=tx).filter(role="guest").bulk_update(role="member")
  ```

- **Two transaction strategies** (auto-selected by `transaction()` based on URL + server):
  - `InteractiveTransaction` (WebSocket + SurrealDB 3.x): uses the SDK's native
    `begin()`/`commit()`/`cancel()` API tagged by `txn_id`. Reads inside the transaction
    see uncommitted writes; `save(tx=)` now supports **auto-generated ids** and `refresh(tx=)`
    works.
  - `BufferedTransaction` (HTTP, or WebSocket on SurrealDB 2.6.x): preserves the v0.8.0
    `BEGIN…COMMIT` batching semantics. Reads inside the transaction still raise.

- Exported `BufferedTransaction` and `InteractiveTransaction` (in addition to the `Transaction`
  base type) for typing and inspection.

### Changed

- `transaction()` now **cancels** on a commit failure (e.g. server-side rollback surfacing at
  `COMMIT`), so an interactive transaction never leaves the shared connection half-open.

### Fixed

- `QuerySet.get(id)` inside an interactive transaction now normalises the SDK's
  `NotFoundError` to `SurrealDbNotFoundError`, matching the non-tx path.

### Notes

- `bulk_update()`/`bulk_delete()` return `0` on the buffered strategy (the affected-row
  count is not knowable before commit). On the interactive strategy they return the real
  count.

## [0.8.0] - 2026-06-06

### Added

- **Transactions ORM (core)**: `async with SurrealDBConnectionManager.transaction() as tx:`
  context manager plus a `tx=` parameter on `save()`, `update()`, `merge()`, and `delete()`.
  Operations are buffered and committed atomically as a single
  `BEGIN TRANSACTION; … COMMIT TRANSACTION;` query; an exception in the block rolls
  everything back.
- Exported `Transaction`.

### Notes

- Inside a transaction, `save()` requires an explicit record id (auto-generated ids are not
  buffered in this release). Reads/`refresh()` inside a transaction raise a clear error and
  are planned for v0.9.0 (QuerySet `objects(tx=)` + savepoints).

## [0.7.0] - 2026-06-06

### Added

- **`BaseSurrealModel.get_raw_id()` helper**: Returns the bare identifier string (e.g. `"alice"`) extracted from the native `RecordID` stored in `model.id`. Use this when you need a plain string instead of a `RecordID` object.

  ```python
  user = await User.objects().get("alice")
  user.get_raw_id()  # → "alice"
  user.id            # → RecordID("User", "alice")
  ```

- **CI matrix expanded**: Integration tests now run against SurrealDB **v2.6.5** and **v3.1.3** across Python 3.11–3.14.

### Changed

- **BREAKING — Dependency updated**: Replaced `surrealdb>=1.0.8` with `surrealdb[pydantic]>=2.0.0,<3.0.0` (SurrealDB 3.x protocol). Upgrade your environment with `pip install "surrealdb[pydantic]>=2.0.0,<3.0.0"`.
- **BREAKING — Native `RecordID` on loaded records**: A record fetched from the database now has its `id` field set to a native `surrealdb.RecordID` object (e.g. `RecordID("User", "alice")`) instead of a bare string. Update comparisons to use `model.get_raw_id() == "alice"` or `model.id == RecordID("User", "alice")`. In-memory instances you construct yourself retain whatever value you pass in.
- **Structured errors from SDK**: SurrealDB SDK 2.x raises structured exceptions (e.g. `AlreadyExistsError` on 3.x, `InternalError` on 2.x) instead of returning error strings. The ORM converts the "already exists" case to `SurrealDbError` automatically.
- **Not-found-tolerant cleanup operations**: `QuerySet.delete_table()`, `BaseSurrealModel.remove_relation()`, and `remove_all_relations()` are now silent no-ops when the target does not exist (consistent with SurrealDB 2.x behaviour; SurrealDB 3.x would otherwise raise). Note: `record.delete()` on a missing record still raises `SurrealDbError`.
- **Aggregation edge-case normalisation**: `avg()`/`min()`/`max()` over an empty set still return `0.0`/`None`/`None`; SurrealDB 3.x `NaN`/`+inf`/`-inf` responses are normalised to these values.
- **Connection sign-in ordering**: The connection manager now signs in before selecting the namespace/database to comply with SurrealDB 3.x strictness (3.x no longer auto-creates the namespace on a pre-auth `use()`).

### Fixed

- **Reads on never-created tables**: SurrealDB 3.x raises `NotFoundError` ("table does not exist") for a `SELECT`/`select` on a table that was never written to, instead of returning an empty result set as SDK 1.x did. All read paths now honour the ORM's "missing table = empty" contract: `get()`/`first()` raise `SurrealDbNotFoundError` ("No result found."), `count()` returns `0`, `exists()` returns `False`, and `all()`/`exec()`/`query()` return `[]`.

## [0.6.0] - 2026-03-10

### Added

- **Relations & Graph**: Full SurrealDB graph relation support on `BaseSurrealModel`
  - `relate(edge, target, data=)` — Create graph relations (`RELATE source->edge->target`)
  - `remove_relation(edge, target)` — Remove a specific relation
  - `remove_all_relations(edge, direction=)` — Remove all relations of a type (`out`, `in`, `both`)
  - `get_related(edge, direction=, model_class=)` — Retrieve related records through an edge
  - `traverse(path)` — Graph traversal via SurrealQL path syntax (e.g. `->follows->User->follows->User`)

- **FETCH Clause**: `QuerySet.fetch(*fields)` resolves record links inline, preventing N+1 queries
  - `Post.objects().fetch("author", "tags").exec()` generates `SELECT * FROM Post FETCH author, tags;`

- **Validation Utilities**: New security validators in `utils.py`
  - `validate_edge_name()` — Validates edge/relation table names
  - `validate_graph_path()` — Validates graph traversal paths (strict arrow-segment structure)
  - `validate_thing()` — Validates `table:id` record identifiers against injection

- New test file `tests/test_relations.py` with unit + E2E tests

- **CI/CD Workflows**:
  - `.surrealdb-version` file to pin the tested SurrealDB version (2.6.0)
  - `surrealdb-security.yml` — Daily SurrealDB 2.X version monitor (test, auto-PR, auto-issue)
  - `dependabot-automerge.yml` — Auto-merge Dependabot PRs with test validation and version bump

### Changed

- `QuerySet._compile_query()` now appends `FETCH` clause when `fetch()` is used
- `QuerySet.fetch()` now accumulates fields across chained calls instead of overwriting
- `QuerySet.variables()` now merges variables across chained calls instead of overwriting
- Coverage: 92.80%

### Fixed

- **Security**: `_resolve_target_thing()` now validates string targets with `validate_thing()` to prevent SurrealQL injection
- **Security**: `_get_thing()` now validates the generated `table:id` string to prevent injection via malicious model IDs
- **Security**: `get_related()` now validates RecordIDs with `validate_thing()` before interpolation
- **Security**: `$` variable references in filters are now validated against `^\$[a-zA-Z_][a-zA-Z0-9_]*$` pattern
- **Security**: `validate_graph_path()` regex tightened to require arrow-separated segments (`->` or `<-`) — rejects arbitrary `<>-` combinations
- Removed misleading case-insensitive lookup aliases (`icontains`, `istartswith`, `iendswith`, `iregex`) that were mapped to case-sensitive SurrealDB operators

## [0.5.0] - 2026-02-11

### Added

- **Q Objects**: Django-style composable query expressions for complex filters
  - `Q(field=value)` for basic conditions
  - `Q(...) | Q(...)` for OR queries
  - `Q(...) & Q(...)` for AND queries
  - `~Q(...)` for NOT queries
  - Nested combinations: `Q(age__gte=18) & (Q(role="admin") | Q(role="mod"))`

- **Parameterized Filters**: All filter values now use parameterized variables (`$_fN`) instead of string interpolation, preventing SQL injection

- **New Lookup Operators**:
  - `not_in` - NOT IN operator
  - `not_contains` - CONTAINSNOT operator
  - `containsall` - CONTAINSALL operator
  - `containsany` - CONTAINSANY operator

- **`-field` Ordering Shorthand**: Prefix field with `-` for descending order
  - `order_by("-created_at")` instead of `order_by("created_at", OrderBy.DESC)`
  - Multi-field ordering: `order_by("-age", "name")`

- **Bulk Operations**:
  - `bulk_create(models)` - Create multiple records via SDK's `insert()`
  - `bulk_update(**kwargs)` - Update all matching records with parameterized SET clause
  - `bulk_delete()` - Delete all matching records, returns count

- New `Q` class exported from `surreal_orm_lite`
- New test file `tests/test_v050.py` for all v0.5.0 features

### Changed

- `_compile_query()`, `_compile_aggregation_query()`, `_compile_group_by_query()` now return `tuple[str, dict]` with parameterized variables
- `_execute_query()` now accepts optional `variables` parameter
- `filter()` now accepts `*args: Q` positional arguments alongside keyword filters
- `order_by()` now accepts multiple fields with `-field` prefix support
- `_build_where()` replaces `_build_where_clauses()` and returns parameterized WHERE clause
- Shared `parse_lookup()` and `build_filter_condition()` functions moved to `utils.py`
- Fixed `isnull` lookup: now correctly generates `IS NULL`/`IS NOT NULL`

## [0.4.0] - 2026-02-07

### Added

- **Model Signals**: Django-style event system for model lifecycle
  - `Signal` class for pre/post event handlers
  - `AroundSignal` class for context manager-style wrapping signals with `yield`
  - `pre_save` / `post_save` - Fired before/after `save()` operations
  - `pre_update` / `post_update` - Fired before/after `update()` and `merge()` operations
  - `pre_delete` / `post_delete` - Fired before/after `delete()` operations
  - `around_save` / `around_update` / `around_delete` - Wrap operations for timing, logging, etc.
  - `connect(model_class)` decorator for registering handlers
  - `disconnect(handler, model_class)` for removing handlers
  - `clear()` for removing all handlers
  - `has_handlers()` for checking if handlers are registered

- `post_save` signal includes `created` flag to distinguish new records
- `pre_update` / `post_update` signals include `update_fields` list
- New test file `tests/test_signals.py` with unit and e2e tests

### Changed

- `save()`, `update()`, `merge()`, `delete()` now emit signals when handlers are registered
- Internal `_do_save()` method extracted from `save()` for signal integration

## [0.3.0] - 2026-02-05

### Added

- **Aggregation Functions**: New aggregation classes for database calculations
  - `Count()` - Count records
  - `Sum(field)` - Sum numeric field values
  - `Avg(field)` - Calculate average of numeric field
  - `Min(field)` - Find minimum value
  - `Max(field)` - Find maximum value

- **QuerySet Aggregation Methods**: Shortcut methods for common aggregations
  - `count()` - Returns count as integer directly
  - `sum(field)` - Returns sum as float/int
  - `avg(field)` - Returns average as float
  - `min(field)` - Returns minimum value
  - `max(field)` - Returns maximum value

- **GROUP BY Support**: Django-style grouping with annotations
  - `values(*fields)` - Specify fields for GROUP BY
  - `annotate(**aggregations)` - Add aggregation annotations

- **exists() Method**: Efficiently check if records exist

- **raw_query() Class Method**: Execute arbitrary SurrealQL queries with variables

- New test file `tests/test_aggregations.py` with comprehensive unit and e2e tests

### Changed

- QuerySet now tracks `_group_by_fields` and `_annotations` for GROUP BY queries
- `exec()` method now handles GROUP BY queries differently, returning dicts instead of model instances

## [0.2.2] - 2026-02-05

### Fixed

- Corrected project URLs in package metadata (case sensitivity)

## [0.2.1] - 2026-02-04

### Fixed

- Fixed `refresh()` method not updating the model instance
- Fixed `refresh()` not handling SDK 1.0.8 list responses
- Fixed `save()` not updating instance with auto-generated ID
- Fixed SQL query order: `ORDER BY` now comes before `LIMIT/START` (SurrealDB requirement)
- Fixed `set_data` validator not always returning data
- Fixed typo in error message: `primirary_key` → `primary_key`
- Removed ineffective `del self` in `delete()` method
- Fixed dead test `failed_model_validation` that was never executed

### Changed

- `SurrealDbNotFoundError` now inherits from `SurrealDbError` for backward compatibility
- Test database connection now configurable via `SURREALDB_HOST` and `SURREALDB_PORT` environment variables
- Improved docstrings for `unset_connection()` and `is_password_set()`
- Translated French comments to English in `connection_manager.py`
- Renamed variable `test` → `result` in `update()` method

### Added

- Added `codecov.yml` configuration (disabled patch coverage check)
- Added test for `save()` with auto-generated ID
- Added test for `contains` lookup operator

## [0.2.0] - 2026-02-02

### Added

- Initial release of **Surreal ORM Lite**
- Support for official SurrealDB Python SDK 1.0.8
- Compatibility with SurrealDB 2.6.0
- Django-style ORM with `BaseSurrealModel` base class
- `QuerySet` with fluent query builder
- Filter lookups: `exact`, `gt`, `gte`, `lt`, `lte`, `in`, `contains`, `startswith`, etc.
- CRUD operations: `save()`, `update()`, `merge()`, `delete()`, `refresh()`
- `SurrealDBConnectionManager` for connection management
- HTTP and WebSocket connection support
- Custom primary key configuration via `SurrealConfigDict`
- Pydantic 2.x validation support
- Custom exceptions: `SurrealDbError`, `SurrealDbConnectionError`, etc.
- Full async/await support
- 97%+ test coverage

### Changed

- Migrated from custom SDK to official SurrealDB SDK 1.0.8
- Updated API to match SDK 1.0.8 response formats
- `signin()` now uses dictionary format: `{"username": ..., "password": ...}`

### Fixed

- Handle SDK 1.0.8 returning lists for single record `select()`
- Handle SDK 1.0.8 returning error strings instead of exceptions
- Escape special characters in record IDs (e.g., `@` in email addresses)

## [0.1.x] - Previous Versions

Previous versions were part of the SurrealDB-ORM project with a custom SDK implementation. This project (Surreal ORM Lite) is a fork focused on using the official SDK only.
