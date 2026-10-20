# SurrealDB-ORM-lite Roadmap

> Feature implementation roadmap inspired by [SurrealDB-ORM](https://github.com/EulogySnowfall/SurrealDB-ORM)
>
> **Main constraint**: every feature must be implementable with the official SDK
> `surrealdb[pydantic]>=2.0.0,<3.0.0` (SurrealDB 3.x protocol) — **no custom SDK**. Features
> with no native SDK method are reached through `query()`, which runs any SurrealQL (DDL included).

---

## Overview

| Version           | Theme                                               | Status  |
| ----------------- | --------------------------------------------------- | ------- |
| v0.2.x            | Core ORM (CRUD, QuerySet, Filters)                  | Done    |
| v0.3.0            | Aggregations & Utilities                            | Done    |
| v0.4.0            | Model Signals                                       | Done    |
| v0.5.0            | Bulk Operations & Q Objects                         | Done    |
| v0.6.0            | Relations & Graph                                   | Done    |
| v0.7.0            | SDK 2.0 / SurrealDB 3.x migration                   | Done    |
| v0.8.0            | Transactions ORM (core, `tx=`)                      | Done    |
| v0.9.0            | Transactions ORM (QuerySet) + interactive (3.x)     | Done    |
| v0.10.0 – v0.12.0 | Tier 1 — Core write-path (upsert, patch, retry)     | Done    |
| v0.13.0           | Tier 1 — SurrealFunc & server-side values           | Done    |
| v0.14.0           | Tier 1 — Computed Fields                            | Done    |
| v0.15.0           | Tier 1 — `call_function()` (custom `fn::`)          | Done    |
| v0.16.0           | Tier 1 — Connection-level auth (JWT / record users) | Done    |
| v0.17.0           | Tier 1 — Model auth (`AuthenticatedUserMixin`)      | Done    |
| v0.18.0           | Tier 1 — Field aliases & DX                         | Done    |
| v0.19.0           | Tier 1 — Live queries (base), raw notifications     | Done    |
| v0.20.0           | Tier 1 — Typed live queries (`LiveModelStream`)     | Done    |
| v0.21.0           | Tier 1 — Auto-resubscribe & change feeds (cursor)   | Done    |
| v0.22.0           | Tier 1 — Core (native typed relations)              | Done    |
| v0.23.0           | Tier 2 — Rich field types                           | Done    |
| v0.24.0           | Tier 2 — Geospatial fields                          | Done    |
| v0.25.0           | Tier 2 — Embedded test engine                       | Done    |
| v0.26.0           | Tier 2 — Time travel (versioned storage)            | Done    |
| v0.27.0           | Tier 2 — Subqueries                                 | Done    |
| v0.28.0           | Tier 2 — Query cache                                | Done    |
| v0.29.0           | Tier 2 — Multi-database                             | Done    |
| v0.30.0           | Tier 3 — Schema introspection                       | Done    |
| v0.31.0           | Tier 3 — Server-side events & DDL helpers           | Done    |
| v0.32.0           | Tier 3 — Materialized views                         | Done    |
| v0.33.0           | Tier 3 — `TYPE RELATION` enforcement                | Done    |
| v0.34.0 – v0.39.0 | Tier 3 — Advanced (search/migrations/CLI), 6 minors | Planned |
| v0.40.0           | Beta Phase (API freeze, hardening)                  | Planned |
| v2.0.0            | Production / GA (aligned with SDK 2.0)              | Planned |
| v2.1.0+           | Future (post-GA)                                    | Future  |

> **Versioning note**: the GA is numbered **v2.0.0** to mirror the official SurrealDB SDK 2.0
> as a branding choice. This is **independent** of the dependency pin, which stays
> `surrealdb[pydantic]>=2.0.0,<3.0.0`. The `1.x` line is intentionally skipped; the Future
> track lives on `2.x`.

---

## ⭐ Compatibility differentiator (lite vs full)

A key advantage of ORM-lite: it runs on **both** major SurrealDB lines, while the full ORM
(custom SDK) targets 3.x only.

| Project                               | Supported SurrealDB versions            |
| ------------------------------------- | --------------------------------------- |
| **SurrealDB-ORM-lite** (official SDK) | **2.x (2.6 / 2.7) and 3.x (3.2 / 3.3)** |
| **SurrealDB-ORM** (custom SDK)        | **3.x only**                            |

---

## 🆕 Everything unlocked by the official SDK 2.0

The migration to `surrealdb[pydantic]>=2.0.0,<3.0.0` exposes connection methods and native
types unavailable in SDK 1.0.8. Crucially, `query()` runs **any SurrealQL**, including DDL
(`DEFINE INDEX`, `DEFINE ANALYZER`, `DEFINE EVENT`, `DEFINE TABLE … AS SELECT`, `INFO FOR`).
As a result, **everything below is implementable in lite** — only the founding custom-SDK
pieces stay out.

| Feature                                | SDK 2.0 realization                                   | Version        |
| -------------------------------------- | ----------------------------------------------------- | -------------- |
| Native transactions (`tx=`)            | `BEGIN`/`COMMIT`/`CANCEL` via `query()`               | v0.8 – v0.9    |
| `upsert()` / `update_or_create()`      | `upsert()`                                            | v0.10.0        |
| Atomic field/array ops                 | `patch()` (JSON Patch RFC 6902)                       | v0.11.0        |
| Retry on conflict                      | transactions + retry logic                            | v0.12.0        |
| SurrealFunc & server values            | `query()` + native fns (`CREATE`/`UPDATE … SET`)      | ✅ v0.13.0     |
| Computed fields                        | `DEFINE FIELD … VALUE` via `query()`                  | ✅ v0.14.0     |
| `call_function()`                      | `query()` + `fn::name($args)` ¹                       | ✅ v0.15.0     |
| JWT / scope auth (connection)          | `signup`/`signin`/`authenticate`/`invalidate`/`info`  | ✅ v0.16.0     |
| JWT / scope auth (model mixin)         | idem, on a `BaseSurrealModel` subclass                | ✅ v0.17.0     |
| Field aliases & DX                     | Pydantic `Field(alias=)` + config                     | ✅ v0.18.0     |
| Live queries (raw notifications)       | `live()` / `subscribe_live()` / `kill()`              | ✅ v0.19.0     |
| Typed live queries (`LiveModelStream`) | idem + deserialization + `LIVE SELECT [DIFF] … WHERE` | ✅ v0.20.0     |
| Change Feeds / Auto-Resubscribe        | `recv_task` drop signal + `SHOW CHANGES … SINCE`      | ✅ v0.21.0     |
| Native typed relations                 | `insert_relation()`                                   | ✅ v0.22.0     |
| Rich field types                       | native `Datetime`/`Duration`/`Decimal`/`Range`/`Uuid` | ✅ v0.23.0     |
| Geospatial fields (`nearby()`)         | native `Geometry` + `geo::*`                          | ✅ v0.24.0     |
| Embedded / in-memory test engine       | `mem://` / `surrealkv://` ²                           | ✅ v0.25.0     |
| Versioned storage (time-travel)        | `surrealkv+versioned://` ²                            | ✅ v0.26.0     |
| Subqueries                             | nested `SELECT` via `query()`                         | ✅ v0.27.0     |
| Query cache (TTL)                      | client-side cache (SDK-independent)                   | ✅ v0.28.0     |
| Multi-database                         | multiple `AsyncSurreal` instances                     | ✅ v0.29.0     |
| Schema introspection                   | `INFO FOR DB` / `INFO FOR TABLE`                      | ✅ v0.30.0     |
| `DEFINE EVENT` (triggers)              | DDL via `query()`                                     | ✅ v0.31.0     |
| `define_function()` DDL helper         | `DEFINE FUNCTION` via `query()` (`schema.py`)         | ✅ v0.31.0     |
| Materialized views                     | `DEFINE TABLE … AS SELECT`                            | ✅ v0.32.0     |
| `TYPE RELATION` enforcement            | `DEFINE TABLE … TYPE RELATION`                        | ✅ v0.33.0     |
| Full-Text Search (BM25)                | `DEFINE ANALYZER` + `SEARCH` index + `search::*`      | v0.34.0        |
| Vector Search (KNN, HNSW/MTREE)        | `DEFINE INDEX … HNSW/MTREE` + KNN operator            | v0.35.0        |
| Hybrid Search (RRF)                    | vector + FTS fusion                                   | v0.36.0        |
| Migrations (makemigrations/migrate)    | DDL orchestration + tracking table via `query()`      | v0.37.0        |
| CLI `surreal-orm-lite`                 | shell / migrate / inspectdb                           | v0.38.0        |
| Test fixtures & factories              | `ModelFactory`, pytest fixtures                       | v0.39.0        |
| QueryLogger / profiling                | wraps `query()` calls                                 | v0.40.0 (Beta) |

¹ SDK 2.0.0 exposes **no** `run()`/`call()` method, so the call goes through `query()` with
the bare form `fn::name($args);` — never `RETURN fn::name(…)`, which silently truncates a
`BEGIN … COMMIT` batch on both DB lines.

² The embedded engine ships in the SDK 2.0.0 base wheel (engine core 2.3.10); the SDK 3.0 line
moves it to the `surrealdb[embedded]` extra.

**Still NOT in lite** (full-ORM only, even with SDK 2.0):

- **Custom SDK (`surreal_sdk`)** — the full ORM's founding architecture.
- **Custom CBOR manipulation** — handled internally by the official SDK, not exposed.

> Note: the official SDK 2.0 is still single-connection; a true connection pool is a future
> _tentative_ (client-side pool of official connections), not an SDK-native primitive.

---

## SurrealDB-ORM vs SurrealDB-ORM-lite Comparison

| Feature                       | ORM (full) | ORM-lite                    |
| ----------------------------- | ---------- | --------------------------- |
| Supported SurrealDB           | 3.x only   | **2.6/2.7 + 3.2/3.3**       |
| CRUD & QuerySet               | yes        | v0.2.0                      |
| Aggregations & GROUP BY       | yes        | v0.3.0                      |
| Model Signals                 | yes        | v0.4.0                      |
| Bulk Operations               | yes        | v0.5.0                      |
| Q Objects (OR/AND/NOT)        | yes        | v0.5.0                      |
| Parameterized filters         | yes        | v0.5.0                      |
| Relations & Graph             | yes        | v0.6.0                      |
| FETCH clause                  | yes        | v0.6.0                      |
| Transactions (`tx=`)          | yes        | ✅ v0.8 core, v0.9 QuerySet |
| `upsert` / `update_or_create` | yes        | ✅ v0.10.0                  |
| `call_function()` (`fn::`)    | yes        | ✅ v0.15.0                  |
| Atomic field/array ops        | yes        | ✅ v0.11.0                  |
| Retry on conflict             | yes        | ✅ v0.12.0                  |
| SurrealFunc & server values   | yes        | ✅ v0.13.0                  |
| Computed fields               | yes        | ✅ v0.14.0                  |
| `call_function()`             | yes        | ✅ v0.15.0                  |
| JWT Authentication            | yes        | ✅ v0.16.0 (connection)     |
| Model-level auth mixin        | yes        | ✅ v0.17.0                  |
| Field aliases & DX            | yes        | ✅ v0.18.0                  |
| Live queries (raw notifs)     | yes        | ✅ v0.19.0                  |
| Typed live queries            | yes        | ✅ v0.20.0                  |
| Live auto-resubscribe / CDC   | yes        | ✅ v0.21.0                  |
| Native typed relations        | yes        | ✅ v0.22.0                  |
| Rich field types              | yes        | ✅ v0.23.0                  |
| Geospatial fields             | yes        | ✅ v0.24.0                  |
| Embedded / versioned engine   | no         | ✅ v0.25 – v0.26            |
| Subqueries                    | yes        | ✅ v0.27.0                  |
| Query cache (TTL)             | yes        | ✅ v0.28.0                  |
| Multi-database                | yes        | ✅ v0.29.0                  |
| Schema introspection          | yes        | ✅ v0.30.0                  |
| DEFINE EVENT                  | yes        | ✅ v0.31.0                  |
| Materialized views            | yes        | ✅ v0.32.0                  |
| TYPE RELATION enforcement     | yes        | ✅ v0.33.0                  |
| Full-Text Search (BM25)       | yes        | v0.34.0                     |
| Vector Search (KNN/HNSW)      | yes        | v0.35.0                     |
| Hybrid Search (RRF)           | yes        | v0.36.0                     |
| Migrations & CLI              | yes        | v0.37 – v0.38               |
| Test fixtures & factories     | yes        | v0.25.0 + v0.39.0           |
| QueryLogger / profiling       | yes        | v0.40.0                     |
| Custom SDK (`surreal_sdk`)    | yes        | never                       |
| CBOR protocol internals       | yes        | never                       |

### Embedded URLs

The full SurrealDB-ORM has no embedded engine (HTTP/WebSocket only); surreal-orm-lite runs on the
official SDK's in-process engine from v0.25.0.

| Embedded URL                   | surreal-orm-lite (official SDK 2.0.0)                                                    | Full SurrealDB-ORM       |
| ------------------------------ | ---------------------------------------------------------------------------------------- | ------------------------ |
| `mem://`, `memory://`          | ✅ in memory, gone when the connection closes                                            | ❌ (HTTP/WebSocket only) |
| `surrealkv://<path>`           | ✅ on disk (SurrealKV)                                                                   | ❌                       |
| `surrealkv+versioned://<path>` | ✅ on disk, keeps history: time travel with `as_of()` (README section 27)                | ❌                       |
| `file://<path>`                | ✅ a **SurrealKV** store, not the RocksDB store `file://` means for the SurrealDB server | ❌                       |
| `rocksdb://<path>`             | ❌ not yet supported by the official SDK (#233)                                          | ❌                       |

The embedded engine is SurrealDB core 2.3.10 with SDK 2.0.0, not the 2.x/3.x server lines the ORM
is tested on. A bare `memory` (no `://`) is refused by SDK 2.0.0; write `memory://`. Base test
fixtures (`SurrealFixture`, `@fixture`, `embedded_database()`) ship in v0.25.0; factories
(`ModelFactory`, `Faker`) in v0.39.0.

---

## Completed versions

### Version 0.2.x — Core ORM

- Django-style ORM with `BaseSurrealModel`, `QuerySet` fluent builder
- CRUD: `save()`, `update()`, `merge()`, `delete()`, `refresh()`
- Filter lookups (`exact`, `gt`, `in`, `contains`, `like`, `regex`, `isnull`, …)
- `SurrealDBConnectionManager` (HTTP + WebSocket), custom primary keys, Pydantic 2.x

### Version 0.3.0 — Aggregations & Utilities

- `Count`, `Sum`, `Avg`, `Min`, `Max`; `count/sum/avg/min/max()`
- GROUP BY via `values()` + `annotate()`; `exists()`; `raw_query()`

### Version 0.4.0 — Model Signals

- `Signal` (`pre/post_save/update/delete`) and `AroundSignal`
- `connect/disconnect/clear/has_handlers`; `created` flag; `update_fields`

### Version 0.5.0 — Bulk Operations & Q Objects

- `Q` objects (OR/AND/NOT), parameterized filters (anti-injection)
- `bulk_create/bulk_update/bulk_delete`, extra lookups, `-field` ordering

### Version 0.6.0 — Relations & Graph

- `relate()`, `remove_relation()`, `remove_all_relations()`, `get_related()`, `traverse()`
- `fetch()` (FETCH clause)

### Version 0.7.0 — SDK 2.0 / SurrealDB 3.x migration

- Dependency `surrealdb[pydantic]>=2.0.0,<3.0.0`
- Native `RecordID`, structured SDK exceptions, signin-before-use ordering
- CI matrix tested on SurrealDB **v2.7.0 and v3.3.4**

### Version 0.8.0 — Transactions ORM (core)

- `async with SurrealDBConnectionManager.transaction() as tx:` context manager
- `tx=` on `save()` / `update()` / `merge()` / `delete()` — buffered and committed
  atomically as one `BEGIN TRANSACTION; …; COMMIT TRANSACTION;` query (rollback on error)
- Exported `Transaction`; failure detection via `query_raw` status inspection
- Note: QuerySet reads under `tx`, savepoints, and auto-id in tx are deferred to v0.9.0

### Version 0.9.0 — Transactions ORM (QuerySet)

- `objects(tx=)`: QuerySet reads, bulk_update/bulk_delete/bulk_create participate in a transaction
- Interactive transactions via the SDK's native API (`begin()`/`commit`/`cancel` + `txn_id`),
  **SurrealDB 3.x only**: reads see uncommitted writes, auto-id `save(tx=)`, working `refresh(tx=)`
- HTTP and SurrealDB 2.6.x keep the v0.8.0 buffered model (writes batched; reads in tx raise)
- Savepoints / nested transactions: NOT supported (SurrealDB only has flat BEGIN/COMMIT/CANCEL);
  nested `transaction()` opens independent transactions

### Version 0.10.0 — upsert / update_or_create / get_or_create

- `model.upsert()`: insert-or-replace by explicit id (native SDK `upsert()`, full REPLACE);
  supports `tx=`
- `QuerySet.update_or_create(defaults=, **criteria)` and `get_or_create(defaults=, **criteria)`:
  Django-style, return `(instance, created)`; raise on >1 match (criteria not unique)
- **Identical on SurrealDB 2.6.x and 3.x** — no 3.x-only primitive

### Version 0.11.0 — patch / atomic field & array ops

- `model.patch()` and `QuerySet.patch()`: JSON Patch (RFC 6902) on a single record or a
  filtered set / whole table (native SDK `patch()` / `UPDATE … PATCH`); supports `tx=`
- Atomic helpers `atomic_append` / `atomic_set_add` / `atomic_remove` / `atomic_increment`,
  each one atomic server-side `UPDATE … SET`; support `tx=`
- **Identical on SurrealDB 2.6.x and 3.x — by design**: uses the version-portable functions
  `array::append` / `array::add` / `array::complement` (and numeric `+=`) instead of the bare
  `+=` / `-=` array operators, whose semantics differ between server lines
- These primitives emit no signals (use `merge()` / `save()` for lifecycle hooks)

### Version 0.12.0 — retry_on_conflict & optimistic concurrency

- `retry_on_conflict(max_retries=3, base_delay=0.05, max_delay=2.0, backoff_factor=2.0,
jitter=True)`: async decorator that re-runs a function on a retryable transaction conflict
  with exponential backoff + jitter; retries only conflicts, re-raises everything else, and
  re-raises a `SurrealDbConflictError` after exhaustion
- New `SurrealDbConflictError` (subclass of `SurrealDbError`) + public `is_conflict_error()`
  predicate; a conflict is normalised to this one type on both transaction strategies
- `Transaction.raise_for_status()` now surfaces the real root cause (not the `NotExecuted`
  filler) and types retryable conflicts
- **Same exception type on SurrealDB 2.6.x and 3.x**; conflicts arise more often on 3.x
  (optimistic MVCC) than on 2.6.x (engine serialises more). Detection anchors on SurrealDB's
  own "This transaction can be retried" marker — deliberately narrower than the full ORM so a
  duplicate-key failure is not retried

### Version 0.13.0 — SurrealFunc & server-side values

- `SurrealFunc("time::now()")` marks a raw SurrealQL expression to be evaluated **server-side**;
  `SurrealFunc.call(fn, *args)` builds the call from a function name and raw argument fragments
- `server_values=` / `extra_vars=` on `save()` and `merge()`: the write compiles to
  `CREATE $rid SET …` / `UPDATE $rid SET …` (functions inlined, every other value bound), and
  the returned row syncs the instance — `merge()` stays a partial update
- Six curated function-name enums (`SurrealTimeFunction`, `SurrealMathFunction`,
  `SurrealStringFunction`, `SurrealArrayFunction`, `SurrealCryptoFunction`,
  `SurrealRandFunction`) — **every member is executed against 2.7.0 AND 3.3.4 by the suite**;
  names diverging between the lines (`rand::guid`, `type::is::*`) are excluded on purpose
- **Identical on SurrealDB 2.6.x and 3.x** — no 3.x-only primitive. Only the inherited v0.9.0
  transaction rule differs: on a buffered tx the computed value reaches the instance at commit
- Security: expressions are developer-controlled and inlined; user input goes through
  `extra_vars` and is always bound (the injection boundary)

### Version 0.15.0 — `call_function()` (custom `fn::` stored functions)

- `SurrealDBConnectionManager.call_function(function, args=None, *, params=, return_type=, tx=)`
  and the `BaseSurrealModel.call_function()` shortcut
- Arguments **bound** as query parameters; the function _name_ is validated as an identifier
  path before interpolation (SurrealQL takes no bound parameter in call position)
- `params=` names the arguments: the declared signature is read from `INFO FOR DB` and cached
  per namespace/database, so the mapping's order is irrelevant; a stale entry self-heals
- `return_type=` coerces the result through one `pydantic.TypeAdapter` pass (model, dataclass,
  scalar, `list[Model]`)
- `tx=` runs the function **inside** an open transaction on both DB lines
- **Same on both lines** for the call itself. `tx=` follows the v0.9.0 contract: interactive
  (WS + 3.x) returns the value, buffered (2.6.x / HTTP) returns `None` until commit;
  `return_type=` with a buffered `tx=` raises rather than returning a silent `None`
- The generated statement uses the **bare call form** `fn::name($_fnarg0);`. A `RETURN` inside
  a `BEGIN … COMMIT` batch terminates the transaction early and silently — the next statement
  is reported `status: OK` and never runs — on 2.6.5 **and** 3.2.4. A regression test pins this
- SDK 2.0.0 exposes no `run()`/`call()`; everything goes through `query()`
- `define_function()` (DDL) deliberately deferred to v0.31.0, with the other `schema.py` helpers

### Version 0.16.0 — Connection-level authentication

- `SurrealDBConnectionManager.signin()`, `signup()`, `authenticate()`, `invalidate()`, `info()`
  — all native SDK primitives, no `query()` fallback needed
- `signin()` takes exactly one of three credential shapes: record access (`access=` +
  `variables=`), a system user (`username=` + `password=`), or a refresh exchange (`access=` +
  `refresh=`). `namespace`/`database` default to the configured connection's **for record
  access only** — injecting them into a system-user signin would turn a root signin into a
  database-user signin against a user the server does not have
- `AuthTokens` — frozen, `access` guaranteed non-`None`, `repr` redacts both tokens and there is
  no `__str__` returning the JWT, so a credential cannot leak into a log or a traceback
- **Session persistence**: `get_client()` replays the stored token, so the identity survives
  `reconnect()`, a dropped client and a new event loop instead of silently reverting to root
- `SurrealDbAuthenticationError` normalises every auth failure — the SDK reports the same wrong
  password as `NotFoundError` on 3.x and `InternalError` on 2.6.x, and rejects a malformed token
  client-side as a bare `ValueError`
- `info(return_type=)` reuses `call_function()`'s `TypeAdapter` path; a `None` result passes
  through **without** coercion, because its usual cause is a table permission, not a bad model
- `invalidate()` restores the configured identity rather than leaving the shared client anonymous
- **Same on both lines** for the five methods. **3.x only**: `DEFINE ACCESS … WITH REFRESH` and
  therefore `AuthTokens.refresh` and `signin(refresh=)` — 2.6.x cannot parse the clause, and
  those tests self-skip. Refresh tokens **rotate**: a spent one is rejected immediately
- Model-level auth (`AuthenticatedUserMixin`, `User.signup()` returning an instance) is v0.17.0;
  a `define_access()` DDL helper belongs with `schema.py` at v0.31.0 — v0.31.0 ships none: the
  v0.17.0 model method `define_access()` stays the way to apply `DEFINE ACCESS`, and the full
  ORM's `DefineAccess` operation comes with migrations (v0.37.0)

### Version 0.19.0 — Live queries (base)

- `QuerySet.watch()` is the everyday form: an async context manager that subscribes to the
  model's table and **kills the subscription on exit**, including when the body raises. The
  handle exposes `live_id`, `table`, `is_active` and `stop()`, named after the full ORM's
  `LiveModelStream` so the typed v0.20.0 layer presents the same object
- `QuerySet.live()` returns the live query's `UUID`;
  `SurrealDBConnectionManager.subscribe_live()` iterates the raw envelopes and `kill()` stops
  them. `subscribe_live()` is intentionally **not** a coroutine, so buffering starts at the call
  rather than at the first iteration and a write made in between is still reported
- `LiveAction` (`StrEnum`) lets `notif["action"] == LiveAction.CREATE` work against the raw
  string, keeping the envelope a plain dict
- **The SDK's own `subscribe_live()` is unusable here**: its body is `yield ret["result"]`, which
  discards the envelope and makes CREATE, UPDATE and DELETE indistinguishable. The ORM registers
  its own queue in the connection's public `live_queues` registry — the same mechanism — and
  keeps the action. Two tests guard the coupling: one fails if the attribute disappears, the
  other if the SDK ever starts yielding the envelope itself
- **`live(diff=True)` is a no-op in the installed SDK** (`prep_live()` encodes only the table),
  so no `diff=` argument is exposed. v0.20.0 does it through `LIVE SELECT DIFF`, verified on both
  lines
- **Two measured divergences, both normalised.** 3.x refuses to watch an undefined table (→
  `SurrealDbNotFoundError`) while 2.6.x accepts and stays silent; after `kill()` 3.x sends a
  `KILLED` notification and 2.6.x sends nothing, so the ORM pushes its own end-of-stream marker
  and the `async for` terminates the same way on both lines
- Guards rather than surprises: HTTP connections are refused by name, and a queryset carrying
  `filter`/`select`/`limit`/`offset`/`order_by`/`fetch`/`annotate` is rejected instead of being
  silently watched whole

### Version 0.20.0 — Typed live queries

- `QuerySet.live(diff=)` returns a `LiveModelStream` yielding `ModelChangeEvent`s with a model
  `instance` — the full SurrealDB-ORM's API, names and fields, so migrating is an import change.
  `post_live_change` is the full ORM's signal. `auto_resubscribe`/`on_reconnect` arrived in v0.21.0
- `filter()`/`Q`/`Var`+`variables()` and `fetch()` are applied server-side through
  `LIVE SELECT [DIFF] … WHERE … FETCH`, sent with `query()`; `watch()` gains the same filters
- **SurrealDB 2.x drops bound parameters in a live query** — the filter silently matches
  nothing. Values are therefore inlined as literals by a closed encoder (`surql_literal.py`),
  as the full ORM does, but stricter: unknown types raise instead of being stringified, `Decimal`
  stays a decimal, and record ids use the backtick form both lines parse identically. Each
  literal is round-tripped against both servers in the test suite
- Diff mode: `changed_fields` from the patch paths (aliases resolved); the root path (`""` on 3.x,
  `"/"` on 2.x) and the 2.x diff `DELETE` (a whole record) are normalised by `live()`
- `await qs.live()` (uuid) is deprecated but kept, so v0.19.0 code keeps running
- Measured the same on both lines: filter enter/leave semantics, `fetch()` in notifications

### Version 0.21.0 — Auto-resubscribe & change feeds

- `live()`/`watch()` take the full ORM's `auto_resubscribe=True` and `on_reconnect=`: a dropped
  WebSocket is detected through the SDK's receive task (it ends within milliseconds on both
  lines), and the stream resubscribes on a new connection with backoff, keeping its buffer, so
  an `async for` carries on. `on_reconnect(old_id, new_id)` is the catch-up hook — a live query
  never replays an outage
- A resubscribe never changes identity: a stream opened under an identity a new connection
  cannot restore (`store=False`), or whose identity changed since, ends with
  `SurrealDbAuthenticationError` rather than reopening as someone else
- No reader hangs on a drop any more: `auto_resubscribe=False` and `subscribe_live()` readers
  raise `SurrealDbConnectionError`; deliberate teardowns still end streams cleanly
- `get_client()` replaces a dead WebSocket client, and opening is serialised per event loop
- `QuerySet.changes(since=, poll_interval=, batch_size=)` → `ChangeModelStream` (full ORM API)
  with an `int` cursor that resumes exactly on both lines — 2.x's `SINCE` takes
  `versionstamp >> 16`, 3.x's the stamp itself. `since=datetime` is exact on 3.x (converted to
  `unix_ms << 16`, since the server's own datetime `SINCE` is always empty there) and
  at-least-once on 2.x. At-least-once per transaction; works over HTTP; clauses refused; a table
  without `CHANGEFEED` refused

### Version 0.22.0 — Native typed relations

- `BaseSurrealEdge`: an edge table as a Pydantic model with frozen `in_` (column `in`) and `out`
  record endpoints; `relate(EdgeModel, target, …)` returns the typed edge, and edge `save()` and
  `bulk_create()` go through the official SDK's `insert_relation()` (one round-trip per batch).
  Edges work in interactive and buffered transactions, and in live queries and change feeds
- `SurrealConfigDict(table_name=…)`, the full ORM's key, honoured by every path; `_table_name` is
  deprecated
- The graph helpers take the full SurrealDB-ORM's parameters, in its order: `reverse=`, keyword
  edge fields, `tx=` on every write helper, edge classes and lists of edges,
  `get_related(direction="both")`, and a bare record id as `remove_relation()` target.
  `remove_relation()`/`remove_all_relations()` run a graph-path `DELETE` with bound record ids
- Filters take model instances as values everywhere, and `"table:id"` strings on edge endpoints
- **The ORM evens out the lines.** A duplicate explicit edge id is silently kept by a raw
  `INSERT RELATION` on 2.7.0 and 3.1.5 but raises on 3.3.3: the ORM's guarded statement raises
  "already exists" on all three. A write refused by `PERMISSIONS` comes back empty without an
  error on both lines: the ORM raises. Endpoint rewrites (ignored by the server) and `upsert()`
  on an edge (a plain record no traversal sees) are refused
- Native idempotency documented: a deterministic array id or a `UNIQUE` index on `in, out`.
  Relation DDL (`TYPE RELATION IN … OUT …`) stays hand-written until v0.33.0, which declares it
  on the edge model (`relation_in`, `relation_out`, `enforced`) and applies it with
  `define_relation()`

### Version 0.23.0 — Rich field types

- `timedelta`, `date` and `set[X]`/`frozenset[X]` fields, plus the ORM's own `Duration` and
  `Range[T]`: every write stores SurrealDB's native value (a duration, a `datetime` at midnight
  UTC, an array, a range) and every read returns the declared type. `datetime`, `Decimal`, `UUID`
  and `bytes` keep working as before. Nested values (`list[timedelta]`, sub-model fields) are sent
  the same way at any depth
- `Duration` keeps the server's nanoseconds and mirrors the SDK 2.0.0 `surrealdb.Duration` API
  with exact results and a strict `parse()`; `InvalidDurationError` is both a `ValueError` and a
  `surrealdb.InvalidDurationError`. `Range[T]` takes the SDK's `BoundIncluded`/`BoundExcluded`
  (now exported) or `None` for an open side, validates each bound value as `T` and has a SurrealQL
  text form in JSON
- **One encoding boundary**: every payload the ORM hands the official SDK (fields, filter values,
  `call_function()` arguments, transaction statements, auth variables) goes through one internal
  function, checked over `src/` by an AST test. A value the server would reject (a negative, zero
  or oversized duration, a `Decimal` beyond `2**96 - 1`, an `int` outside signed 64-bit) is refused
  with a plain `ValueError` before anything is sent; a `Decimal` written with an exponent goes out
  in fixed-point notation
- Filters: `in`/`not_in` bind a `Range` as one value (refused on `id` and edge endpoints);
  `contains` on a range column tests membership; filter values on rich fields must be typed. Live
  queries write durations, dates, sets and ranges as SurrealQL literals
- Reads: SDK durations become `timedelta` or `Duration` in top-level fields, including on rows
  applied after a write, so an instance written back stores a duration. `atomic_increment()`
  refuses a duration amount (2.x ignores `+=` with a duration)
- Same on both lines, measured on 2.7.0, 3.1.5 and 3.3.4, with one divergence: a `TYPE set<…>`
  column holds a native set on 3.3 that the surrealdb SDK 2.0.0 cannot read (3.1 refuses the
  array), so such columns are declared `array<…>`. The SDK 2.0.0 limits (zero durations, 3.x
  native sets) wait for SDK 3.0 (#205), and are read by the ORM's clients since v0.24.3 (#235);
  typed string filters (#220), validated keyword writes (#221) and validating returned rows like
  the full ORM are follow-ups

### Version 0.24.0 — Geospatial fields

- Geometry fields with the full ORM's names: `GeoField["kind"]` and the aliases `PointField`,
  `LineStringField`, `PolygonField`, `MultiPointField`, `MultiLineStringField`,
  `MultiPolygonField`, `GeometryCollectionField` and `GeometryField` (nullable, default `None`), or
  a geometry class annotation. Every write stores a native geometry and every read path (`get()`,
  `filter()`, live notifications, change feeds, rows a write returns) gives back the ORM's classes.
  A field also accepts the SDK's value, a GeoJSON object and a `(longitude, latitude)` pair for
  points, so full-ORM GeoJSON data loads and `update()` migrates it to a native geometry
- Seven immutable, hashable value classes mirroring the SDK's (`GeometryPoint` …
  `GeometryCollection`) with GeoJSON in both directions and `InvalidGeometryError` (a `ValueError`
  and a `surrealdb.InvalidGeometryError`); they exist because `model_dump()` flattens the SDK's
  dataclasses. The encoding boundary sends them as SDK geometries and refuses, before anything is
  sent, the SDK geometries every SurrealDB line refuses
- `QuerySet.nearby(field, point, max_distance)` with the point and distance bound and an
  `.is_point()` guard; `GeoDistance` for `annotate()` and `order_by()` (metres, never written back);
  the `inside`, `outside` and `intersects` lookups in `filter()`, `Q` and live queries, which get a
  GeoJSON literal for every geometry kind
- Same on both lines, measured on 2.7.0 and 3.3.4, identical on 3.1.5. Geometries inside
  sub-models after a write that syncs the instance (#228), SDK values in untyped fields (#222) and
  typed filter values / validated keyword writes (#220, #221) are follow-ups

### Version 0.25.0 — Embedded test engine

- The ORM runs on the official SDK's in-process engine:
  `set_connection(url="mem://", namespace=…, database=…)`, credentials optional and never sent,
  and every ORM call (CRUD, QuerySet, relations, typed edges, rich and geo fields, upsert/patch,
  server values, computed fields, stored functions, buffered transactions, change feeds,
  authentication) runs with no server. `mem://`, `memory://`,
  `surrealkv://`, `surrealkv+versioned://` and `file://` (SurrealKV) are routed by the SDK's own URL
  parser; `SurrealDBConnectionManager.is_embedded()` tells which kind is configured
- One engine per process, shared by every event loop and thread; a namespace/database change
  reselects on the same engine and ends the stored session; closing discards a `mem://` database
  and keeps a `surrealkv://` one
- Clear ORM errors before any request for what the engine cannot do (live queries,
  `changes(since=<datetime>)`, model auth with `bind=False`, `ephemeral_client()`) and for a URL
  the SDK refuses (`rocksdb://`, #233, quoting the SDK)
- `surreal_orm_lite.testing`: `embedded_database()` (an isolated in-process database per block,
  nestable) and the full ORM's `SurrealFixture` / `@fixture`; `ModelFactory` and `Faker` stay in
  v0.39.0
- The engine is SurrealDB core 2.3.10 (SDK 2.0.0), so it speaks 2.x SurrealQL; its behaviour is a
  table of its own in the README. Server lines unchanged (2.7.0 and 3.3.4, 3.1.5 retro). Time travel
  on `surrealkv+versioned://` is v0.26.0; the SDK 3.0 engine (3.x core, `surrealdb[embedded]`) is #205

### Version 0.26.0 — Time travel (versioned storage)

- `QuerySet.as_of(at)` reads a model's records as they were at a moment: SurrealQL's `VERSION`
  clause through `query()` (no SDK method takes a version), the moment always bound. It takes a
  `datetime` (naive = UTC), an ISO-8601 string, a `timedelta` or `Duration` before the database's
  clock, a `SurrealFunc` the database evaluates, or `None`; moments SurrealDB versions cannot
  address (before 1970, after 2262-04-11) are refused on every line
- Every read honours it (`exec()`, `first()`, `get()`, `all()`, `exists()`, `count()` and the
  aggregates, `values().annotate()`, `GeoDistance`), and each table read checks every record by
  its id at the moment: a plain `SELECT … VERSION` returns records deleted before the moment on
  3.x and miscounts an unfiltered `count()` on 2.x and the embedded engine
- The instances it returns are read-only snapshots (their writes raise before any signal or
  request; `refresh()` makes them current again); writes, bulk operations, `query()`, live queries,
  `changes()` and transactions are refused on a time-travel queryset
- Stores: the embedded `surrealkv+versioned://` engine, 3.x servers on `?versioned=true` storage,
  2.x servers on `surrealkv+versioned://`. The embedded engine refuses a URL without history before
  any request and a 3.x server refuses `VERSION`; a 2.x server in memory answers without an error
  and cannot be detected (documented). `fetch()` under `as_of()` is refused on 2.x, whose links
  resolve to their current state
- No equivalent in the full SurrealDB-ORM. CI adds a versioned server per job; server lines
  unchanged (2.7.0 and 3.3.4, 3.1.5 retro, identical)

### Version 0.27.0 — Subqueries

- `Subquery(qs)`, or the queryset itself, is the value of a lookup in `filter()` and `Q`: `in`,
  `not_in`, `containsall` and `containsany` compare with all its values; `exact`, `gt`, `gte`,
  `lt`, `lte`, `contains` and `not_contains` with its first value, and match no record when it is
  empty. The inner queryset selects its record ids (`SELECT VALUE id`) or the one field given to
  `select()`; its `filter()`, `Q`, `nearby()`, `order_by()`, `limit()`, `offset()` and
  `variables()` apply, its `fetch()` is ignored
- One statement per request: a block whose `LET`s bind each subquery once, innermost first,
  through `query()` (the SDK 2.0.0 `query()` returns the first statement's result only, so the full
  ORM's `LET` prelude does not fit). An ordered and limited subquery is right on every line: an
  inline one can return wrong rows on SurrealDB 3.2 and later (surrealdb#7470), and 2.x needs the
  order fields selected, which the ORM does (`_sqv`)
- Every read, count, aggregation, `values().annotate()` group, write by filter (`bulk_update()`,
  `bulk_delete()`, `QuerySet.patch()`), `get_or_create()`/`update_or_create()` lookup,
  `objects(tx=)` statement and `as_of()` read honours it; subqueries nest; `live()` and `watch()`
  write the sub-SELECT inline, which the server evaluates again on every change
- A subquery table that does not exist reads as empty on both lines (3.x: after a table probe
  confirms it, the statement is sent again with that subquery empty); every combination that
  cannot compile correctly raises before any request
- Deferred: `annotate(x=Subquery(...))`, correlated subqueries (`OuterRef`) and `Exists`. Server
  lines unchanged (2.7.0 and 3.3.4, 3.1.5 retro, which differs only in the error a time-travel
  subquery raises without versioned storage)

### Version 0.28.0 — Query cache

- `QuerySet.cache(ttl)` and `QueryCache` (`configure()`, `make_key()`, `get()`, `set()`,
  `invalidate()`, `clear()`, `stats()`), the full SurrealDB-ORM's names. Every read of a cached
  queryset is served from this process's memory: `exec()`, `first()`, `get()` by filters or by id,
  `all()`, `count()`, the aggregates, `exists()`, `values().annotate()` and `GeoDistance`. Rows
  are cached, and each hit builds new instances from a copy
- Keys are the SHA-256 of the CBOR bytes the SDK sends (statement and variables, mapping keys
  sorted), so `RecordID("user", 1)` and `"user:1"` never share an entry. The TTL is the oldest
  result a read accepts (seconds or a `timedelta`, `0` never cached, default 60 s); at most
  `max_size` entries (1000 by default), least recently used evicted first; `stats()` adds `hits`
  and `misses`
- Invalidation is built in: every ORM write removes the reads of the tables it writes before its
  `post_*` signals, the reads whose subqueries read them included; a read that follows links
  (`fetch()`, a dotted path, a `select()` item that is not a plain field) by any write; a read of
  an edge model by any delete. Inside `transaction()` at commit; while a write runs, reads of its
  tables go to the database and store nothing
- One cache scope per connection and identity, retired with the connection or at its next
  identity change (sign-in, sign-up, `authenticate()`, `invalidate()`): results never cross
  identities. Reads inside a transaction are never cached; `query()`,
  `live()`, `watch()`, `changes()` and `as_of()` refuse `cache()`
- Same on both lines (2.7.0 and 3.3.4, 3.1.5 retro identical) and on the embedded engine. Deferred:
  invalidation from other processes (LIVE SELECT), caching `as_of()` reads, sharing results
  between event loops, coalescing concurrent misses

### Version 0.29.0 — Multiple databases

- `add_connection()`, `remove_connection()`, `get_config()` (`ConnectionConfig`),
  `list_connections()`, `using()`, `get_active_connection_name()`,
  `SurrealConfigDict(connection=)` and `get_connection_name()`, the full SurrealDB-ORM's names;
  `name=` on `get_client()`, `close_connection()` and `transaction()`, hidden from
  `inspect.signature()` so that FastAPI's `Depends()` behaves as before
- Each named connection is a connection class of its own behind a routing manager, with its own
  per-loop clients, session, live streams, change feeds, transactions, stored-function signatures
  and query-cache scope. A call runs on the `using()` block's connection, else the model's
  `connection` key, else `"default"`, resolved at every database call through a `ContextVar`
- Each connection decides on its own server line: a 2.x and a 3.x server side by side in one
  process (interactive transactions on 3.x over WebSocket, buffered on 2.x); CI adds a job with
  both pinned servers
- Errors before any request for what SurrealDB runs in one database: a transaction given another
  connection's model, a subquery, a relation or a typed link across connections, and one on-disk
  embedded store under two connections; `embedded_database(connection=)` for tests. Not detected,
  and documented: untyped links, graph arrows, raw SurrealQL
- Deferred: `QuerySet.using()`, a client-side connection pool, several connections over one
  WebSocket (3.x sessions). Server lines unchanged (2.7.0 and 3.3.4, 3.1.5 retro identical)

### Version 0.30.0 — Schema introspection

- `DatabaseIntrospector(connection, *, tables=).introspect()`, `ModelCodeGenerator().generate()`,
  `generate_models_from_db()` and the state classes (`SchemaState`, `TableState`, `FieldState`,
  `IndexState`, `EventState`, `AccessState`, `AnalyzerState`), the full SurrealDB-ORM's names and
  arguments, plus `FunctionState`, `ParamState` and `SchemaIssue`. Read-only: `INFO FOR DB` and
  `INFO FOR TABLE` through `query()`, outside any transaction, on a named connection or the current
  one
- The state is immutable and keeps every definition's statement in `raw`; kinds, nullability and
  field paths are normalised across the lines, expressions are kept as each line renders them. A
  definition that cannot be parsed becomes a `SchemaIssue`, never a guess; the `users` section is
  never read (password hashes before SurrealDB 3.2.2)
- Generated models use the lite's types (`Duration`, `Decimal`, `UUID`, `RecordID`, geometries,
  `Literal`), sub-models for nested fields, `computed()`, `server_fields`, aliases and typed
  constant defaults; names and expressions from the database reach the source as escaped data
  only. `generate_models_from_db()` replaces a file atomically, `overwrite=False` refuses it
- Same state and same generated types on 2.7.0, 3.3.4 and the embedded engine (3.1.5 retro
  identical); 3.x-only (`COMPUTED`, `REFERENCE`, `FULLTEXT`, sequences) and 2.x-only (`MTREE`,
  `SEARCH ANALYZER`) definitions are read on their line. Deferred: `schema_diff()` /
  `SchemaState.diff()` → v0.37.0 (migrations), the `inspectdb` command → v0.38.0 (CLI),
  object-literal kinds as sub-models, typed 3.x sequences, APIs and configs

### Version 0.31.0 — Server-side events and DDL helpers

- `Event(name, *, then, when=, async_mode=, retry=, max_depth=, comment=)` declared in
  `SurrealConfigDict(events=[...])`, the full SurrealDB-ORM's `Event` plus `max_depth=`;
  `Model.event_ddl()` (pure), `Model.define_events()` (idempotent, `OVERWRITE`) and
  `Model.remove_event()`; `SurrealDBConnectionManager.define_event(event, table)` and
  `remove_event(event, table)` for any table. `then` is always written as a block, so several
  statements, `LET` and `THROW` work on both lines
- `SurrealDBConnectionManager.define_function()`, `remove_function()` and the pure
  `function_ddl()`: stored functions for `call_function()`, whose cached signature they drop on
  every connection of the process to the same database, so a redefinition is seen at once
- Every applier takes `overwrite=` (`OVERWRITE` / `IF NOT EXISTS`) or `missing_ok=`, and `tx=`;
  refusals are classified (`SurrealDbAuthenticationError`, `SurrealDbNotFoundError`,
  `SurrealDbError`). Names are validated and quoted, comments escaped; expressions are trusted
  application code whose structural check catches mistakes and is not an injection defence; kinds
  are written in canonical form (`option<int>`)
- `ASYNC` events (`RETRY`, `MAXDEPTH`) are SurrealDB 3.x only: on 2.x and the embedded engine the
  ORM raises `SurrealDbError` before any DDL. `$input` is 3.x only, and on 3.x an unchanged
  `UPSERT` fires one `UPDATE` event; read back, each line renders definitions its own way.
  `EventState` gains `async_mode`, `retry` and `max_depth`, so `DatabaseIntrospector` reads
  `ASYNC` events (a `SchemaIssue` in 0.30.x)
- Same code on 2.7.0, 3.3.4 and the embedded engine (3.1.5 retro identical). Deferred: the
  migration operations and the removal of events a model no longer declares → v0.37.0
  (migrations), declarative `events=[...]` in generated models, query-cache invalidation of the
  tables events write

### Version 0.32.0 — Materialized views

- `SurrealConfigDict(view_query="SELECT …")`, the full SurrealDB-ORM's key, declares a read-only
  model over a table SurrealDB computes from that query; `get_view_query()` and `_is_view()`;
  `Model.view_ddl()` (pure, ``DEFINE TABLE OVERWRITE `<table>` AS (…)``), `Model.define_view()`
  (idempotent, `OVERWRITE`; `overwrite=False` for `IF NOT EXISTS`) and `Model.remove_view()`,
  both with `tx=`. The query is checked when the class is defined (one `SELECT`, no `ORDER BY`,
  `LIMIT`, `FETCH` or `SELECT VALUE`); it is trusted application code, and the check catches
  mistakes and is not an injection defence
- The ORM refuses: every write of a view model (`TypeError` before any request, the full ORM's
  first words, on every write path); on SurrealDB 2.x and the embedded engine, the grouped
  projections those engines compute per source record (`math::stddev()`/`math::variance()`, an
  expression around an aggregate, a field outside `GROUP BY`, a `GROUP BY` on an alias), before
  any DDL; turning a table that holds records into a view, `IF NOT EXISTS` over an ordinary table
  and removing a table that is not a view (the table guard); and, on 2.x and the embedded engine,
  removing a view that another view reads (3.x refuses it itself)
- Grouped views' ids are arrays: `get(["paid"])` and `filter(id=[...])` address them (a list or
  tuple given as a record id is an array id for every model). `generate_models_from_db()` writes
  view models with `view_query=`; reads of a view model are dropped from the query cache by every
  ORM write; on a view model `id__in` is sent as `CONTAINS`, and on 3.x `live()` instances take
  their id from the notification's record
- Measured differences: raw writes to a view accepted by 2.x and 3.1, refused by 3.2+; `int` sums
  and `Decimal` means on 2.x, `float` on 3.x; `count()` of an array field; an emptied group
  notified as `UPDATE` then `DELETE` on 2.x, `DELETE` on 3.x; on 3.x a live query on a view
  filtered on `id` never notifies; 3.1 refuses to redefine a view that holds records. Same code
  on 2.7.0, 3.3.4 and the embedded engine (3.1.5 retro)
- Deferred: migrations (`CreateTable`/`AlterTable` with `view_query`) and `define_table()` →
  v0.37.0, whose `define_table()` will accept a view model and apply it through `view_ddl()`;
  table permissions for record users (a view is defined with SurrealDB's default
  `PERMISSIONS NONE`) and a view query built from a QuerySet

### Version 0.33.0 — `TYPE RELATION` enforcement

- An edge model declares its graph constraints with the full SurrealDB-ORM's keys:
  `SurrealConfigDict(relation_in=…, relation_out=…, enforced=…, table_type=TableType.RELATION)`
  on `BaseSurrealEdge` (`table_type` optional; `TableType` exported, its six members and values).
  Ends are one name, a `|`-separated string or a list or tuple; each is an identifier, written in
  backquotes. The keys are checked at the `class` statement (on a node model they raise
  `TypeError`); `get_relation_in()`/`get_relation_out()` return tuples in the shape of
  `TableState`, `is_enforced()` a bool, `get_table_type()` the table's `TableType`
- `Model.relation_ddl()` renders ``DEFINE TABLE OVERWRITE `<table>` TYPE RELATION IN … OUT …``
  (pure); `Model.define_relation()` applies it (idempotent; `overwrite=False` for `IF NOT EXISTS`;
  `tx=`): one `INFO FOR DB`, then one `DEFINE TABLE OVERWRITE` that keeps the table's schema mode,
  `DROP`, comment, change feed and permissions, and its records, fields, indexes, events, views
  and live queries. Enforced at once on every connection; a keyless model removes the
  constraint. Refused before any DDL: a materialized view, a definition the ORM cannot read back,
  `overwrite=False` on a table that is not a relation table
- Enforcement is SurrealDB's: every edge creation path (`relate()` typed or by table name, an
  edge's `save()`, `objects().bulk_create()`) raises `SurrealDbError` for an endpoint outside
  `IN`/`OUT` and `SurrealDbNotFoundError` for a missing endpoint with `ENFORCED`; the string
  `relate("<edge table>", …)` now translates server errors too. `generate_models_from_db()` writes
  `relation_in`/`relation_out`/`enforced` instead of the `# IN … OUT …` comment, so a relation
  table round-trips through introspection
- Measured differences: the refusal wording (`Found … but expected a record<…>` on 2.x,
  `Couldn't coerce value for field …` on 3.x); an endpoint both outside `IN`/`OUT` and missing on
  an `ENFORCED` table is `SurrealDbNotFoundError` on 2.x and the embedded engine,
  `SurrealDbError` on 3.x; a raw `ALTER TABLE … TYPE RELATION …` of a table already written to is
  not enforced on 2.x and the embedded engine until a `DEFINE TABLE OVERWRITE` (the ORM never
  sends `ALTER TABLE`). Same code on 2.7.0, 3.3.4 and the embedded engine (3.1.5 retro identical)
- Deferred: migrations (`CreateTable`/`AlterTable` with the relation keys) and `define_table()`
  → v0.37.0, which will apply an edge model through `define_relation()`; until then `TableType`'s
  other members have no effect, and the table's other clauses are kept from the database (no
  schema-mode, permissions, change-feed or comment keys yet)

### Version 0.18.0 — Field aliases, `server_fields` & `merge(refresh=False)`

- `Field(alias="password_hash")` renames the **column**, not the attribute. The alias is
  honoured on writes (`patch()` pointers and the `atomic_*` family included), on read
  hydration, in the DDL the ORM generates, and in **every QuerySet clause** — `select`, `filter`
  (nested and negated `Q` objects, dotted paths included), `order_by`, `values`, `fetch`,
  `bulk_update`, the aggregation helpers and `annotate()`. Filter rewriting is the piece the full
  ORM still lists as an open gap, so lite is ahead of it here
- `get_field_aliases()` / `to_db_field()` / `to_py_field()` expose the mapping; a single
  boundary (`_columns_for()` on the model, `_column()` on the QuerySet) keeps Python names on
  one side of the wire and columns on the other
- `BaseSurrealModel` gains `populate_by_name=True`, so an aliased field validates under either
  name — strictly widening
- `SurrealConfigDict(server_fields=[…])` marks columns the server owns. `get_server_fields()`
  merges them with the model's computed fields, since both are server-owned for payload building
- **Create and replace exclude differently, and the server is why.** Probed on 2.6.5 and 3.2.4
  with identical results: a `DEFINE FIELD … DEFAULT` is a _create-time_ default, so on
  `UPDATE`/`UPSERT … CONTENT` an omitted optional column is deleted and an omitted required one
  raises. `server_fields` are therefore dropped from creates (letting the default apply) and
  kept on replaces (so the column survives). Computed fields stay excluded on both — a `VALUE`
  clause re-evaluates every write
- An **explicit** write to a `server_fields` entry is allowed (`merge`, `bulk_update`,
  `server_values`); a computed field still raises. Naming the column is taken as consent
- `merge()` resyncs from the row the `UPDATE` returns — one round-trip instead of the former
  write-then-`SELECT` — and `merge(refresh=False)` compiles `RETURN NONE` so no row comes back.
  It forfeits the missing-record check, which _is_ the returned row
- **Same on both lines**, in and out of a transaction — no capability probe, and no test in this
  version skips on either server

### Version 0.17.0 — Model-level authentication

- `AuthenticatedUserMixin` turns a `BaseSurrealModel` subclass into a user model: `signup()`,
  `signin()`, `authenticate()` and `refresh()` return **hydrated instances**
  (`AuthResult.user`) instead of bare tokens, paired with the v0.16.0 `AuthTokens`
- **Session isolation is the headline.** Every call runs on its own short-lived connection via
  `SurrealDBConnectionManager.ephemeral_client()`, so the process-wide client keeps the identity
  `set_connection()` gave it. v0.16.0's connection-level `signin()` re-identifies everything
  sharing that client, which makes it unusable per-request in a concurrent server; `bind=True`
  opts back into that behaviour deliberately
- `access_ddl()` / `define_access()` render and apply `DEFINE ACCESS … TYPE RECORD` from the
  model, mirroring v0.14.0's `computed_field_ddl()` / `define_computed_fields()` pair. The
  `DEFINE TABLE … PERMISSIONS FOR select WHERE id = $auth.id` ships with it by default, because
  without it a signin succeeds yet returns no record — v0.16.0's sharpest gotcha
- A configured `primary_key` makes SIGNUP target `type::thing('<table>', $<pk>)`, so the record
  stays addressable by the ORM afterwards instead of landing on a random id
- The hydrated instance's password field holds the **hash**; SurrealDB returns the stored record
- New `SurrealConfigDict` keys: `access_name`, `identifier_field`, `password_field`,
  `auth_algorithm`, `auth_duration_token`, `auth_duration_session`, `auth_duration_grant`,
  `with_refresh`
- **Same on both lines** except `with_refresh=True` / `refresh()`, which are **3.x only**: 2.6.x
  cannot parse `WITH REFRESH`, so `define_access()` raises a message naming the requirement and
  those tests self-skip. Refresh tokens rotate

### Version 0.14.0 — Computed fields

- `Computed[T] = computed("<expr>")` declares a field SurrealDB derives from other fields on
  **every** write, via `DEFINE FIELD … VALUE`. Where v0.13.0's `SurrealFunc` evaluates an
  expression for one write, `Computed` attaches it to the **schema** — so it applies to writes
  the ORM never sees, making it a server-enforced invariant rather than a convention
- `computed_field_ddl(overwrite=True)` renders the statements without touching the database;
  `define_computed_fields(overwrite=True, tx=)` applies them and is idempotent (safe at start-up)
- Computed fields are excluded from every write payload; naming one in `merge()`,
  `save(server_values=)`, `bulk_update()` or an `atomic_*` helper raises `ValueError`
- `save()` with an explicit id now syncs the row the server returned (it previously discarded
  it, unlike every other write path), which is what lets server-owned values reach the instance
- **Identical on SurrealDB 2.6.x and 3.x** — no capability gate, no skipped tests. Only the raw
  SDK exception for an invalid expression differs (`InternalError` vs `ValidationError`, both
  `ServerError`), normalised to `SurrealDbError`
- Caveat: SurrealDB evaluates computed fields in **alphabetical field-name order**, not
  declaration order — a computed field reading another must sort after it
- No `TYPE` clause is emitted (the server infers an optional type); explicit field types stay
  a later roadmap item

---

### Version 0.14.5 — Per-event-loop connections (issue #163)

- The client cache is keyed by the running event loop. A WebSocket client is bound to its
  loop, so the single cached client made the second of two `asyncio.run()` calls fail with
  `got Future attached to a different loop` — or hang
- Two live loops (threads, multi-loop servers) each keep their own connection instead of
  evicting one another
- `close_connection()` closes the running loop's client only; new `close_all_connections()`
  tears everything down; `is_connected()` answers for the loop asking
- Entries for closed loops are pruned on the next `get_client()`
- Identical on 2.6.5 and 3.2.4 — loop binding is an asyncio/SDK property, not a server one

---

### Version 0.14.4 — Record-id lookups (issue #159)

- `filter(id=...)` matched nothing: the row's `id` is a native `RecordID`, but the `WHERE`
  builder bound the caller's raw Python value. Now coerced through `coerce_record_id()`,
  which accepts `RecordID`, `int`, a bare identifier, a backticked one and the full
  `"Table:id"` form (unwrapped only when the prefix names the queried table)
- The same lookup drives `get_or_create(id=…)` / `update_or_create(id=…)`, which therefore
  never converged — every call took the create path and the second raised `already exists`
- `get()` shares the helper, fixing `get(5)` on a model declared `id: int`
- A text/collection lookup on `id` (`contains`, `startswith`, `regex`, …) now raises instead
  of silently returning nothing
- An aliased `primary_key` column stays an ordinary string column — untouched by design
- Identical on 2.6.5 and 3.2.4

---

### Version 0.14.3 — Correctness release (issue #156)

- Eight findings closed: `first()` no longer leaves `LIMIT 1` on the queryset;
  `update_or_create`/`get_or_create` refuse undeclared keys on **both** paths (they used to be
  dropped on create and written to the row on update); user-authored SurrealQL is sent verbatim;
  `upsert()` reports `created` from `UPSERT … RETURN $before`; a numeric-looking string id is
  backtick-quoted so relations reach it (an `id: int` keeps its integer record id, and a
  `"table:id"` relation target is quoted by the same rule); `get_related(model_class=)` is one
  round-trip; `raw_query()` **and `objects().query()`** warn on multi-statement queries; the
  `"$$literal"` escape round-trips through `get_or_create`/`update_or_create`; warnings report
  the caller's frame, not one inside the ORM
- New: `Var("name")` for an explicit query-variable reference in a filter, plus the `"$$literal"`
  escape. The bare `"$x"` string form is deprecated — **flipping the default so strings are
  always literals is planned for a later minor**, and the deprecation warning is the runway
- Done in v0.28.1 (#250): a string filter value is always data, `Var()` is the only variable
  reference, and the `"$$"` escape is deprecated
- Every finding reproduced on live SurrealDB 3.2.4 **and** 2.6.5 before the fix, and verified on
  both afterwards; no line-specific behaviour

## Planned — Tier 1: Core (SDK 2.0 strict)

> Per-version detail and acceptance criteria are worked out in a design spec before the code,
> kept with the maintainer's working notes rather than in the repository.
> Every version keeps coverage ≥ 70 %, green `ruff`/`mypy`, and E2E green on the pinned 2.x and 3.x lines.

### 🔵 Phase A — Write-path & atomicity

| Version    | Theme                                                                                   | SDK 2.0 primitive         |
| ---------- | --------------------------------------------------------------------------------------- | ------------------------- |
| ✅ v0.8.0  | Transactions ORM (core): `transaction()` ctx manager + `tx=` on CRUD                    | `BEGIN`/`COMMIT`/`CANCEL` |
| ✅ v0.9.0  | Transactions ORM (QuerySet): `objects(tx=)`, bulk under `tx`, no savepoints (SurrealDB) | idem                      |
| ✅ v0.10.0 | `upsert()` + `update_or_create()` / `get_or_create()`                                   | `upsert()`                |
| ✅ v0.11.0 | `patch()` & atomic field/array ops (append/remove/set_add/increment)                    | `patch()` (JSON Patch)    |
| ✅ v0.12.0 | `retry_on_conflict` & optimistic concurrency                                            | transactions + retry      |

### 🟢 Phase B — Server-side computation

| Version    | Theme                                                        | SDK 2.0 primitive      |
| ---------- | ------------------------------------------------------------ | ---------------------- |
| ✅ v0.13.0 | SurrealFunc + `server_values=` / `extra_vars=` on save/merge | `query()` + native fns |
| ✅ v0.14.0 | Computed Fields (`Computed[...]` → `DEFINE FIELD … VALUE`)   | `DEFINE FIELD`         |
| ✅ v0.15.0 | `call_function()` (call defined SurrealDB functions)         | `query()` + `fn::…`    |

### 🟣 Phase C — Auth & DX

| Version    | Theme                                                                       | SDK 2.0 primitive |
| ---------- | --------------------------------------------------------------------------- | ----------------- |
| ✅ v0.16.0 | Connection-level auth: `signin`/`signup`/`authenticate`/`invalidate`/`info` | SDK auth methods  |
| ✅ v0.17.0 | `AuthenticatedUserMixin` (model-level signup/signin, isolated sessions)     | idem              |
| ✅ v0.18.0 | Field aliases (`Field(alias=)`) + `server_fields` + `merge(refresh=False)`  | Pydantic + config |

### 🟡 Phase D — Real-time

| Version    | Theme                                                                           | SDK 2.0 primitive             |
| ---------- | ------------------------------------------------------------------------------- | ----------------------------- |
| ✅ v0.19.0 | Live Queries (base): `live()`/`watch()`/`subscribe_live()`/`kill()`, raw notifs | `live`/`kill` + `live_queues` |
| ✅ v0.20.0 | Typed live queries: `live()` → `LiveModelStream`, filters, diff, signal         | `LIVE SELECT [DIFF] … WHERE`  |
| ✅ v0.21.0 | Change Feeds / Auto-Resubscribe: WS reconnect + resubscribe + cursor            | `recv_task` + `SHOW CHANGES`  |

### 🟠 Phase E — Graph

| Version    | Theme                                                               | SDK 2.0 primitive   |
| ---------- | ------------------------------------------------------------------- | ------------------- |
| ✅ v0.22.0 | Native typed relations: `insert_relation()`, `TYPE RELATION` tables | `insert_relation()` |

---

## Planned — Tier 2: Extended (SDK-2.0-native)

> Still official-SDK only; also absorbs spillover from the heavy core themes.

| Version    | Theme                                                                | SDK 2.0 realization               |
| ---------- | -------------------------------------------------------------------- | --------------------------------- |
| ✅ v0.23.0 | Rich field types: `Datetime`, `Duration`, `Decimal`, `Range`, `Uuid` | typed Pydantic ↔ native SDK types |
| ✅ v0.24.0 | Geospatial fields: `Geometry` + `nearby()` / distance                | native `Geometry` + `geo::*`      |
| ✅ v0.25.0 | Embedded test engine: `mem://` / `surrealkv://` + base fixtures ¹    | SDK embedded connection           |
| ✅ v0.26.0 | Versioned storage / time-travel: `surrealkv+versioned://` ²          | embedded versioned engine         |
| ✅ v0.27.0 | Subqueries (nested QuerySets in `filter`/`in`)                       | nested `SELECT` via `query()`     |
| ✅ v0.28.0 | Query cache (TTL + invalidation), client-side                        | client cache                      |
| ✅ v0.29.0 | Multi-database: named connection registry                            | N × `AsyncSurreal` instances      |

¹ The embedded engine ships in the SDK 2.0.0 base wheel (engine core 2.3.10); the SDK 3.0 line
moves it to the `surrealdb[embedded]` extra.

² The versioned engine is the SDK 2.0.0 embedded engine's `surrealkv+versioned://`; versioned
servers (3.x `?versioned=true`, 2.x `surrealkv+versioned://`) work too.

---

## Planned — Tier 3: Advanced (search, schema & DDL, migrations, CLI)

> All implementable via `query()` (SurrealQL DDL + operators). Committed to the plan per the
> 2026-06-06 decision. Index availability validated on both 2.6.x and 3.1 (the then-supported 3.x line).

### 🔴 Phase F — Schema & DDL

| Version    | Theme                                                                          | SDK 2.0 realization              |
| ---------- | ------------------------------------------------------------------------------ | -------------------------------- |
| ✅ v0.30.0 | Schema introspection (inspectdb, read-only)                                    | `INFO FOR DB` / `INFO FOR TABLE` |
| ✅ v0.31.0 | `DEFINE EVENT` (server-side triggers)                                          | DDL via `query()`                |
| ✅ v0.32.0 | Materialized views (read-only models)                                          | `DEFINE TABLE … AS SELECT`       |
| ✅ v0.33.0 | `TYPE RELATION` enforcement (graph constraints), building on `BaseSurrealEdge` | `DEFINE TABLE … TYPE RELATION`   |

### 🔎 Phase G — Advanced search

| Version | Theme                                               | SDK 2.0 realization                              |
| ------- | --------------------------------------------------- | ------------------------------------------------ |
| v0.34.0 | Full-Text Search (`search()`, BM25 score/highlight) | `DEFINE ANALYZER` + `SEARCH` index + `search::*` |
| v0.35.0 | Vector Search (`similar_to()`, KNN)                 | `DEFINE INDEX … HNSW/MTREE` + KNN operator       |
| v0.36.0 | Hybrid Search (RRF)                                 | vector + FTS fusion                              |

### 🛠️ Phase H — Tooling

| Version | Theme                                                  | SDK 2.0 realization                        |
| ------- | ------------------------------------------------------ | ------------------------------------------ |
| v0.37.0 | Migration system (makemigrations / migrate / rollback) | DDL orchestration + tracking via `query()` |
| v0.38.0 | CLI `surreal-orm-lite` (shell, migrate, inspectdb)     | entry point over the ORM                   |
| v0.39.0 | Test fixtures & factories (`ModelFactory`, pytest)     | tooling over the ORM                       |

---

## Planned — Tier 4: Stabilization

### Version 0.40.0 — Beta Phase (API freeze)

- Connection-level retry/reconnect (network resilience, distinct from `retry_on_conflict`)
- Configurable logging; **QueryLogger / profiling**
- Metrics (request/error/latency counters, export hooks)
- Documented performance benchmarks
- Complete documentation (docstrings, migration guide, examples)
- **Public API freeze** — no breaking changes until GA

**Completion criteria**: E2E green on the pinned 2.x and 3.x lines, coverage ≥ 75 %, full API docs, API-freeze
changelog published, no regression since v0.39.0.

### Version 2.0.0 — Production / GA

- Production stability, coverage ≥ **80 %**
- Complete documentation + `0.x → 2.0` migration guide
- Published performance benchmarks
- No breaking change since v0.40.0 (the Beta-frozen API is honored)
- `v2.0.0-rcN` release candidates tagged before GA

---

## 🔮 Future (post-GA, v2.1.0+)

The SDK-2.0-feasible scope is now **fully committed** across Tiers 1–3. One tentative candidate
remains for after GA:

- **Connection pool (client-side)** — the official SDK is single-connection; a pool of
  `AsyncSurreal` instances is possible but low-priority, to be evaluated after GA.

---

## Out of scope (full-ORM only, even with SDK 2.0)

| Feature                    | Reason                                 |
| -------------------------- | -------------------------------------- |
| Custom SDK (`surreal_sdk`) | Founding architecture of the full ORM  |
| Custom CBOR manipulation   | Handled internally by the official SDK |

---

## Contributing

1. Pick a feature from the roadmap
2. Create an issue for discussion
3. Fork and create a `feature/xxx` branch
4. Implement with tests
5. Submit a PR

---

## References

- [SurrealDB Documentation](https://surrealdb.com/docs)
- [SurrealDB Python SDK](https://surrealdb.com/docs/sdk/python/methods)
- [SurrealDB-ORM (full)](https://github.com/EulogySnowfall/SurrealDB-ORM)
- [Pydantic v2](https://docs.pydantic.dev/latest/)
