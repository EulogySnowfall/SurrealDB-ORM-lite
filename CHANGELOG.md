# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.13.0] - 2026-07-22

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
  ORM error.
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
