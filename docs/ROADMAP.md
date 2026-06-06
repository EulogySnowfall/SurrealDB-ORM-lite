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
| v0.9.0 – v0.22.0  | Tier 1 — Core (auth, live, relations, …)             | Planned |
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
| **SurrealDB-ORM-lite** (official SDK) | **2.6.x and 3.1**            |
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
| SurrealFunc & computed fields       | `query()` + `DEFINE FIELD … VALUE`                    | v0.13 – v0.14  |
| `call_function()`                   | RPC `run()` / `RETURN fn::…`                          | v0.15.0        |
| JWT / scope auth                    | `signup`/`signin`/`authenticate`/`invalidate`/`info`  | v0.16 – v0.17  |
| Field aliases & DX                  | Pydantic `Field(alias=)` + config                     | v0.18.0        |
| Live Models / Live Queries          | `live()` / `subscribe_live()` / `kill()`              | v0.19 – v0.20  |
| Change Feeds / Auto-Resubscribe     | live queries + reconnect logic                        | v0.21.0        |
| Native typed relations              | `insert_relation()`                                   | v0.22.0        |
| Rich field types                    | native `Datetime`/`Duration`/`Decimal`/`Range`/`Uuid` | v0.23.0        |
| Geospatial fields (`nearby()`)      | native `Geometry` + `geo::*`                          | v0.24.0        |
| Embedded / in-memory test engine    | `mem://` / `surrealkv://` ¹                           | v0.25.0        |
| Versioned storage (time-travel)     | `surrealkv+versioned://` ¹                            | v0.26.0        |
| Subqueries                          | nested `SELECT` via `query()`                         | v0.27.0        |
| Query cache (TTL)                   | client-side cache (SDK-independent)                   | v0.28.0        |
| Multi-database                      | multiple `AsyncSurreal` instances                     | v0.29.0        |
| Schema introspection                | `INFO FOR DB` / `INFO FOR TABLE`                      | v0.30.0        |
| `DEFINE EVENT` (triggers)           | DDL via `query()`                                     | v0.31.0        |
| Materialized views                  | `DEFINE TABLE … AS SELECT`                            | v0.32.0        |
| `TYPE RELATION` enforcement         | `DEFINE TABLE … TYPE RELATION`                        | v0.33.0        |
| Full-Text Search (BM25)             | `DEFINE ANALYZER` + `SEARCH` index + `search::*`      | v0.34.0        |
| Vector Search (KNN, HNSW/MTREE)     | `DEFINE INDEX … HNSW/MTREE` + KNN operator            | v0.35.0        |
| Hybrid Search (RRF)                 | vector + FTS fusion                                   | v0.36.0        |
| Migrations (makemigrations/migrate) | DDL orchestration + tracking table via `query()`      | v0.37.0        |
| CLI `surreal-orm-lite`              | shell / migrate / inspectdb                           | v0.38.0        |
| Test fixtures & factories           | `ModelFactory`, pytest fixtures                       | v0.39.0        |
| QueryLogger / profiling             | wraps `query()` calls                                 | v0.40.0 (Beta) |

¹ **To confirm** against the targeted SDK build (embedded extra). If unavailable on PyPI,
v0.25.0/v0.26.0 are reclassified to Future.

**Still NOT in lite** (full-ORM only, even with SDK 2.0):

- **Custom SDK (`surreal_sdk`)** — the full ORM's founding architecture.
- **Custom CBOR manipulation** — handled internally by the official SDK, not exposed.

> Note: the official SDK 2.0 is still single-connection; a true connection pool is a future
> _tentative_ (client-side pool of official connections), not an SDK-native primitive.

---

## SurrealDB-ORM vs SurrealDB-ORM-lite Comparison

| Feature                       | ORM (full) | ORM-lite         |
| ----------------------------- | ---------- | ---------------- |
| Supported SurrealDB           | 3.x only   | **2.6.x + 3.1**  |
| CRUD & QuerySet               | yes        | v0.2.0           |
| Aggregations & GROUP BY       | yes        | v0.3.0           |
| Model Signals                 | yes        | v0.4.0           |
| Bulk Operations               | yes        | v0.5.0           |
| Q Objects (OR/AND/NOT)        | yes        | v0.5.0           |
| Parameterized filters         | yes        | v0.5.0           |
| Relations & Graph             | yes        | v0.6.0           |
| FETCH clause                  | yes        | v0.6.0           |
| Transactions (`tx=`)          | yes        | ✅ v0.8, v0.9 QS |
| `upsert` / `update_or_create` | yes        | v0.10.0          |
| Atomic field/array ops        | yes        | v0.11.0          |
| Retry on conflict             | yes        | v0.12.0          |
| SurrealFunc & Computed        | yes        | v0.13 – v0.14    |
| `call_function()`             | yes        | v0.15.0          |
| JWT Authentication            | yes        | v0.16 – v0.17    |
| Field aliases & DX            | yes        | v0.18.0          |
| Live Models / CDC             | yes        | v0.19 – v0.21    |
| Native typed relations        | yes        | v0.22.0          |
| Rich field types              | yes        | v0.23.0          |
| Geospatial fields             | yes        | v0.24.0          |
| Embedded / versioned engine   | yes        | v0.25 – v0.26    |
| Subqueries                    | yes        | v0.27.0          |
| Query cache (TTL)             | yes        | v0.28.0          |
| Multi-database                | yes        | v0.29.0          |
| Schema introspection          | yes        | v0.30.0          |
| DEFINE EVENT                  | yes        | v0.31.0          |
| Materialized views            | yes        | v0.32.0          |
| TYPE RELATION enforcement     | yes        | v0.33.0          |
| Full-Text Search (BM25)       | yes        | v0.34.0          |
| Vector Search (KNN/HNSW)      | yes        | v0.35.0          |
| Hybrid Search (RRF)           | yes        | v0.36.0          |
| Migrations & CLI              | yes        | v0.37 – v0.38    |
| Test fixtures & factories     | yes        | v0.39.0          |
| QueryLogger / profiling       | yes        | v0.40.0          |
| Custom SDK (`surreal_sdk`)    | yes        | never            |
| CBOR protocol internals       | yes        | never            |

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
- CI matrix tested on SurrealDB **v2.6.0 and v3.1.3**

### Version 0.8.0 — Transactions ORM (core)

- `async with SurrealDBConnectionManager.transaction() as tx:` context manager
- `tx=` on `save()` / `update()` / `merge()` / `delete()` — buffered and committed
  atomically as one `BEGIN TRANSACTION; …; COMMIT TRANSACTION;` query (rollback on error)
- Exported `Transaction`; failure detection via `query_raw` status inspection
- Note: QuerySet reads under `tx`, savepoints, and auto-id in tx are deferred to v0.9.0

---

## Planned — Tier 1: Core (SDK 2.0 strict)

> Per-version detail and acceptance criteria live in the design spec:
> [`docs/superpowers/specs/2026-06-06-roadmap-sdk-2.0-design.md`](superpowers/specs/2026-06-06-roadmap-sdk-2.0-design.md).
> Every version keeps coverage ≥ 70 %, green `ruff`/`mypy`, and E2E green on 2.6.x + 3.1.

### 🔵 Phase A — Write-path & atomicity

| Version   | Theme                                                                    | SDK 2.0 primitive         |
| --------- | ------------------------------------------------------------------------ | ------------------------- |
| ✅ v0.8.0 | Transactions ORM (core): `transaction()` ctx manager + `tx=` on CRUD     | `BEGIN`/`COMMIT`/`CANCEL` |
| v0.9.0    | Transactions ORM (QuerySet): `objects(tx=)`, bulk under `tx`, savepoints | idem                      |
| v0.10.0   | `upsert()` + `update_or_create()` / `get_or_create()`                    | `upsert()`                |
| v0.11.0   | `patch()` & atomic field/array ops (append/remove/set_add/increment)     | `patch()` (JSON Patch)    |
| v0.12.0   | `retry_on_conflict` & optimistic concurrency                             | transactions + retry      |

### 🟢 Phase B — Server-side computation

| Version | Theme                                                        | SDK 2.0 primitive      |
| ------- | ------------------------------------------------------------ | ---------------------- |
| v0.13.0 | SurrealFunc + `server_values=` / `extra_vars=` on save/merge | `query()` + native fns |
| v0.14.0 | Computed Fields (`Computed[...]` → `DEFINE FIELD … VALUE`)   | `DEFINE FIELD`         |
| v0.15.0 | `call_function()` (call defined SurrealDB functions)         | RPC `run()`            |

### 🟣 Phase C — Auth & DX

| Version | Theme                                                                       | SDK 2.0 primitive |
| ------- | --------------------------------------------------------------------------- | ----------------- |
| v0.16.0 | Connection-level auth: `signin`/`signup`/`authenticate`/`invalidate`/`info` | SDK auth methods  |
| v0.17.0 | `AuthenticatedUserMixin` (model-level signup/signin, scoped sessions)       | idem              |
| v0.18.0 | Field aliases (`Field(alias=)`) + `server_fields` + `merge(refresh=False)`  | Pydantic + config |

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
| v0.26.0 | Versioned storage / time-travel: `surrealkv+versioned://` ¹          | embedded versioned engine         |
| v0.27.0 | Subqueries (nested QuerySets in `filter`/`in`)                       | nested `SELECT` via `query()`     |
| v0.28.0 | Query cache (TTL + invalidation), client-side                        | client cache                      |
| v0.29.0 | Multi-database: named connection registry                            | N × `AsyncSurreal` instances      |

¹ To confirm against the targeted SDK build; reclassified to Future if the embedded extra is
unavailable on PyPI.

---

## Planned — Tier 3: Advanced (search, schema & DDL, migrations, CLI)

> All implementable via `query()` (SurrealQL DDL + operators). Committed to the plan per the
> 2026-06-06 decision. Index availability validated on both 2.6.x and 3.1.

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

**Completion criteria**: E2E green on 2.6.x + 3.1, coverage ≥ 75 %, full API docs, API-freeze
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
