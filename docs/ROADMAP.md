# SurrealDB-ORM-lite Roadmap

> Feature implementation roadmap inspired by [SurrealDB-ORM](https://github.com/EulogySnowfall/SurrealDB-ORM)
>
> **Main constraint**: every feature must be implementable with the official SDK
> `surrealdb[pydantic]>=2.0.0,<3.0.0` (SurrealDB 3.x protocol) — **no custom SDK**. Features
> with no native SDK method are reached through `query()`, which runs any SurrealQL (DDL included).

---

## Overview

| Version           | Theme                                                | Status  |
| ----------------- | ---------------------------------------------------- | ------- |
| v0.2.x            | Core ORM (CRUD, QuerySet, Filters)                   | Done    |
| v0.3.0            | Aggregations & Utilities                             | Done    |
| v0.4.0            | Model Signals                                        | Done    |
| v0.5.0            | Bulk Operations & Q Objects                          | Done    |
| v0.6.0            | Relations & Graph                                    | Done    |
| v0.7.0            | SDK 2.0 / SurrealDB 3.x migration                    | Done    |
| v0.8.0            | Transactions ORM (core, `tx=`)                       | Done    |
| v0.9.0            | Transactions ORM (QuerySet) + interactive (3.x)      | Done    |
| v0.10.0 – v0.12.0 | Tier 1 — Core write-path (upsert, patch, retry)      | Done    |
| v0.13.0           | Tier 1 — SurrealFunc & server-side values            | Done    |
| v0.14.0           | Tier 1 — Computed Fields                             | Done    |
| v0.15.0           | Tier 1 — `call_function()` (custom `fn::`)           | Done    |
| v0.16.0           | Tier 1 — Connection-level auth (JWT / record users)  | Done    |
| v0.17.0 – v0.22.0 | Tier 1 — Core (model auth, live, relations)          | Planned |
| v0.23.0 – v0.29.0 | Tier 2 — Extended (SDK-2.0-native), 7 minors         | Planned |
| v0.30.0 – v0.39.0 | Tier 3 — Advanced (search/DDL/migrations), 10 minors | Planned |
| v0.40.0           | Beta Phase (API freeze, hardening)                   | Planned |
| v2.0.0            | Production / GA (aligned with SDK 2.0)               | Planned |
| v2.1.0+           | Future (post-GA)                                     | Future  |

> **Versioning note**: the GA is numbered **v2.0.0** to mirror the official SurrealDB SDK 2.0
> as a branding choice. This is **independent** of the dependency pin, which stays
> `surrealdb[pydantic]>=2.0.0,<3.0.0`. The `1.x` line is intentionally skipped; the Future
> track lives on `2.x`.

---

## ⭐ Compatibility differentiator (lite vs full)

A key advantage of ORM-lite: it runs on **both** major SurrealDB lines, while the full ORM
(custom SDK) targets 3.x only.

| Project                               | Supported SurrealDB versions |
| ------------------------------------- | ---------------------------- |
| **SurrealDB-ORM-lite** (official SDK) | **2.6.x and 3.2.x**          |
| **SurrealDB-ORM** (custom SDK)        | **3.x only**                 |

---

## 🆕 Everything unlocked by the official SDK 2.0

The migration to `surrealdb[pydantic]>=2.0.0,<3.0.0` exposes connection methods and native
types unavailable in SDK 1.0.8. Crucially, `query()` runs **any SurrealQL**, including DDL
(`DEFINE INDEX`, `DEFINE ANALYZER`, `DEFINE EVENT`, `DEFINE TABLE … AS SELECT`, `INFO FOR`).
As a result, **everything below is implementable in lite** — only the founding custom-SDK
pieces stay out.

| Feature                             | SDK 2.0 realization                                   | Version        |
| ----------------------------------- | ----------------------------------------------------- | -------------- |
| Native transactions (`tx=`)         | `BEGIN`/`COMMIT`/`CANCEL` via `query()`               | v0.8 – v0.9    |
| `upsert()` / `update_or_create()`   | `upsert()`                                            | v0.10.0        |
| Atomic field/array ops              | `patch()` (JSON Patch RFC 6902)                       | v0.11.0        |
| Retry on conflict                   | transactions + retry logic                            | v0.12.0        |
| SurrealFunc & server values         | `query()` + native fns (`CREATE`/`UPDATE … SET`)      | ✅ v0.13.0     |
| Computed fields                     | `DEFINE FIELD … VALUE` via `query()`                  | ✅ v0.14.0     |
| `call_function()`                   | `query()` + `fn::name($args)` ¹                       | ✅ v0.15.0     |
| JWT / scope auth (connection)       | `signup`/`signin`/`authenticate`/`invalidate`/`info`  | ✅ v0.16.0     |
| JWT / scope auth (model mixin)      | idem, on a `BaseSurrealModel` subclass                | v0.17.0        |
| Field aliases & DX                  | Pydantic `Field(alias=)` + config                     | v0.18.0        |
| Live Models / Live Queries          | `live()` / `subscribe_live()` / `kill()`              | v0.19 – v0.20  |
| Change Feeds / Auto-Resubscribe     | live queries + reconnect logic                        | v0.21.0        |
| Native typed relations              | `insert_relation()`                                   | v0.22.0        |
| Rich field types                    | native `Datetime`/`Duration`/`Decimal`/`Range`/`Uuid` | v0.23.0        |
| Geospatial fields (`nearby()`)      | native `Geometry` + `geo::*`                          | v0.24.0        |
| Embedded / in-memory test engine    | `mem://` / `surrealkv://` ²                           | v0.25.0        |
| Versioned storage (time-travel)     | `surrealkv+versioned://` ²                            | v0.26.0        |
| Subqueries                          | nested `SELECT` via `query()`                         | v0.27.0        |
| Query cache (TTL)                   | client-side cache (SDK-independent)                   | v0.28.0        |
| Multi-database                      | multiple `AsyncSurreal` instances                     | v0.29.0        |
| Schema introspection                | `INFO FOR DB` / `INFO FOR TABLE`                      | v0.30.0        |
| `DEFINE EVENT` (triggers)           | DDL via `query()`                                     | v0.31.0        |
| `define_function()` DDL helper      | `DEFINE FUNCTION` via `query()` (`schema.py`)         | v0.31.0        |
| Materialized views                  | `DEFINE TABLE … AS SELECT`                            | v0.32.0        |
| `TYPE RELATION` enforcement         | `DEFINE TABLE … TYPE RELATION`                        | v0.33.0        |
| Full-Text Search (BM25)             | `DEFINE ANALYZER` + `SEARCH` index + `search::*`      | v0.34.0        |
| Vector Search (KNN, HNSW/MTREE)     | `DEFINE INDEX … HNSW/MTREE` + KNN operator            | v0.35.0        |
| Hybrid Search (RRF)                 | vector + FTS fusion                                   | v0.36.0        |
| Migrations (makemigrations/migrate) | DDL orchestration + tracking table via `query()`      | v0.37.0        |
| CLI `surreal-orm-lite`              | shell / migrate / inspectdb                           | v0.38.0        |
| Test fixtures & factories           | `ModelFactory`, pytest fixtures                       | v0.39.0        |
| QueryLogger / profiling             | wraps `query()` calls                                 | v0.40.0 (Beta) |

¹ SDK 2.0.0 exposes **no** `run()`/`call()` method, so the call goes through `query()` with
the bare form `fn::name($args);` — never `RETURN fn::name(…)`, which silently truncates a
`BEGIN … COMMIT` batch on both DB lines.

² **To confirm** against the targeted SDK build (embedded extra). If unavailable on PyPI,
v0.25.0/v0.26.0 are reclassified to Future.

**Still NOT in lite** (full-ORM only, even with SDK 2.0):

- **Custom SDK (`surreal_sdk`)** — the full ORM's founding architecture.
- **Custom CBOR manipulation** — handled internally by the official SDK, not exposed.

> Note: the official SDK 2.0 is still single-connection; a true connection pool is a future
> _tentative_ (client-side pool of official connections), not an SDK-native primitive.

---

## SurrealDB-ORM vs SurrealDB-ORM-lite Comparison

| Feature                       | ORM (full) | ORM-lite                    |
| ----------------------------- | ---------- | --------------------------- |
| Supported SurrealDB           | 3.x only   | **2.6.x + 3.2.x**           |
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
| Field aliases & DX            | yes        | v0.18.0                     |
| Live Models / CDC             | yes        | v0.19 – v0.21               |
| Native typed relations        | yes        | v0.22.0                     |
| Rich field types              | yes        | v0.23.0                     |
| Geospatial fields             | yes        | v0.24.0                     |
| Embedded / versioned engine   | yes        | v0.25 – v0.26               |
| Subqueries                    | yes        | v0.27.0                     |
| Query cache (TTL)             | yes        | v0.28.0                     |
| Multi-database                | yes        | v0.29.0                     |
| Schema introspection          | yes        | v0.30.0                     |
| DEFINE EVENT                  | yes        | v0.31.0                     |
| Materialized views            | yes        | v0.32.0                     |
| TYPE RELATION enforcement     | yes        | v0.33.0                     |
| Full-Text Search (BM25)       | yes        | v0.34.0                     |
| Vector Search (KNN/HNSW)      | yes        | v0.35.0                     |
| Hybrid Search (RRF)           | yes        | v0.36.0                     |
| Migrations & CLI              | yes        | v0.37 – v0.38               |
| Test fixtures & factories     | yes        | v0.39.0                     |
| QueryLogger / profiling       | yes        | v0.40.0                     |
| Custom SDK (`surreal_sdk`)    | yes        | never                       |
| CBOR protocol internals       | yes        | never                       |

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
- CI matrix tested on SurrealDB **v2.6.5 and v3.2.4**

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
  `SurrealRandFunction`) — **every member is executed against 2.6.5 AND 3.2.4 by the suite**;
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
  a `define_access()` DDL helper belongs with `schema.py` at v0.31.0

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
- Every finding reproduced on live SurrealDB 3.2.4 **and** 2.6.5 before the fix, and verified on
  both afterwards; no line-specific behaviour

## Planned — Tier 1: Core (SDK 2.0 strict)

> Per-version detail and acceptance criteria live in the design spec:
> [`docs/superpowers/specs/2026-06-06-roadmap-sdk-2.0-design.md`](superpowers/specs/2026-06-06-roadmap-sdk-2.0-design.md).
> Every version keeps coverage ≥ 70 %, green `ruff`/`mypy`, and E2E green on 2.6.x + 3.2.x.

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
| v0.17.0    | `AuthenticatedUserMixin` (model-level signup/signin, scoped sessions)       | idem              |
| v0.18.0    | Field aliases (`Field(alias=)`) + `server_fields` + `merge(refresh=False)`  | Pydantic + config |

### 🟡 Phase D — Real-time

| Version | Theme                                                                 | SDK 2.0 primitive              |
| ------- | --------------------------------------------------------------------- | ------------------------------ |
| v0.19.0 | Live Queries (base): `live()`/`subscribe_live()`/`kill()`, raw notifs | `live`/`subscribe_live`/`kill` |
| v0.20.0 | `LiveQuerySet` typed: filters + notifications deserialized, diff mode | idem + diff mode               |
| v0.21.0 | Change Feeds / Auto-Resubscribe: WS reconnect + resubscribe + cursor  | live + reconnect               |

### 🟠 Phase E — Graph

| Version | Theme                                                               | SDK 2.0 primitive   |
| ------- | ------------------------------------------------------------------- | ------------------- |
| v0.22.0 | Native typed relations: `insert_relation()`, `TYPE RELATION` tables | `insert_relation()` |

---

## Planned — Tier 2: Extended (SDK-2.0-native)

> Still official-SDK only; also absorbs spillover from the heavy core themes.

| Version | Theme                                                                | SDK 2.0 realization               |
| ------- | -------------------------------------------------------------------- | --------------------------------- |
| v0.23.0 | Rich field types: `Datetime`, `Duration`, `Decimal`, `Range`, `Uuid` | typed Pydantic ↔ native SDK types |
| v0.24.0 | Geospatial fields: `Geometry` + `nearby()` / distance                | native `Geometry` + `geo::*`      |
| v0.25.0 | Embedded test engine: `mem://` / `surrealkv://` + base fixtures ¹    | SDK embedded connection           |
| v0.26.0 | Versioned storage / time-travel: `surrealkv+versioned://` ²          | embedded versioned engine         |
| v0.27.0 | Subqueries (nested QuerySets in `filter`/`in`)                       | nested `SELECT` via `query()`     |
| v0.28.0 | Query cache (TTL + invalidation), client-side                        | client cache                      |
| v0.29.0 | Multi-database: named connection registry                            | N × `AsyncSurreal` instances      |

¹ To confirm against the targeted SDK build; reclassified to Future if the embedded extra is
unavailable on PyPI.

---

## Planned — Tier 3: Advanced (search, schema & DDL, migrations, CLI)

> All implementable via `query()` (SurrealQL DDL + operators). Committed to the plan per the
> 2026-06-06 decision. Index availability validated on both 2.6.x and 3.1 (the then-supported 3.x line).

### 🔴 Phase F — Schema & DDL

| Version | Theme                                           | SDK 2.0 realization              |
| ------- | ----------------------------------------------- | -------------------------------- |
| v0.30.0 | Schema introspection (inspectdb, read-only)     | `INFO FOR DB` / `INFO FOR TABLE` |
| v0.31.0 | `DEFINE EVENT` (server-side triggers)           | DDL via `query()`                |
| v0.32.0 | Materialized views (read-only models)           | `DEFINE TABLE … AS SELECT`       |
| v0.33.0 | `TYPE RELATION` enforcement (graph constraints) | `DEFINE TABLE … TYPE RELATION`   |

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

**Completion criteria**: E2E green on 2.6.x + 3.2.x, coverage ≥ 75 %, full API docs, API-freeze
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
- Design spec: [`docs/superpowers/specs/2026-06-06-roadmap-sdk-2.0-design.md`](superpowers/specs/2026-06-06-roadmap-sdk-2.0-design.md)
