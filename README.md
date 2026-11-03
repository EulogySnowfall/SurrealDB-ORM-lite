# Surreal ORM Lite

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![SurrealDB](https://img.shields.io/badge/SurrealDB-2.7.0%20%7C%203.3.5-purple)
![SDK](https://img.shields.io/badge/SDK-Official%202.0-green)
![License](https://img.shields.io/badge/license-MIT-blue)
[![codecov](https://codecov.io/gh/EulogySnowfall/SurrealDB-ORM-lite/graph/badge.svg)](https://codecov.io/gh/EulogySnowfall/SurrealDB-ORM-lite)

**Surreal ORM Lite** is a lightweight, Django-style ORM for [SurrealDB](https://surrealdb.com/) that uses the **official SurrealDB Python SDK**. It provides a simple and intuitive interface for database operations with full async support and Pydantic validation.

- **Official SDK only**: built on `surrealdb[pydantic]`, no custom protocol, few dependencies.
- **Two server lines**: the same code runs on SurrealDB **2.x and 3.x**, and on the SDK's in-process engine
  (`mem://`, `surrealkv://`) for tests and notebooks.
- **Django-style queries**: `filter(age__gte=18)`, `Q` objects, `order_by("-age")`, aggregations, subqueries.
- **SurrealDB-native**: graph relations and typed edges, live queries, change feeds, time travel, full-text, vector
  and hybrid search, events, views and schema DDL.
- **Tooling**: migrations, a `surreal-orm-lite` command line, schema introspection, test factories and a pytest
  plugin.

**Documentation**: [user guide](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/README.md) ·
[changelog](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/CHANGELOG.md) ·
[roadmap](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/ROADMAP.md) ·
[examples](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/tree/main/examples)

## Installation

```bash
pip install surreal-orm-lite
# or
uv add surreal-orm-lite
```

| Dependency   | Version                                                      |
| ------------ | ------------------------------------------------------------ |
| Python       | 3.11+ (tested on 3.11 to 3.15)                               |
| SurrealDB    | 2.6.x / 2.7.x or 3.2.x / 3.3.x, or the SDK's embedded engine |
| Official SDK | surrealdb[pydantic]>=2.0.0,<3.0.0                            |
| Pydantic     | >=2.13.4                                                     |

## Quick start

```python
import asyncio

from pydantic import Field

from surreal_orm_lite import BaseSurrealModel, Q, SurrealDBConnectionManager


class User(BaseSurrealModel):
    id: str | None = None
    name: str = Field(..., max_length=100)
    email: str
    age: int = Field(..., ge=0)


async def main() -> None:
    # A server URL (ws://localhost:8000/rpc, http://localhost:8000) or an in-process engine (mem://).
    SurrealDBConnectionManager.set_connection(
        url="http://localhost:8000", user="root", password="root", namespace="app", database="app"
    )

    alice = await User(id="alice", name="Alice", email="alice@example.com", age=30).save()
    await User(id="bob", name="Bob", email="bob@example.com", age=17).save()

    adults = await User.objects().filter(age__gte=18).order_by("-age").exec()
    a_or_minor = await User.objects().filter(Q(name__startswith="A") | Q(age__lt=18)).exec()
    average = await User.objects().avg("age")

    await alice.merge(age=31)  # partial update
    await alice.delete()
    print(adults, a_or_minor, average)


asyncio.run(main())
```

The [getting started guide](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/getting-started.md)
walks through it, and the [querying guide](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/querying.md)
lists every QuerySet method and filter lookup.

## Documentation

| Guide                                                                                                                      | What it covers                                                                                             |
| -------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| [Getting started](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/getting-started.md)                  | Requirements, installation, connection, a first model and CRUD                                             |
| [Models](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/models.md)                                    | Primary keys, table names, server-side values, computed fields, field aliases                              |
| [Field types](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/fields.md)                               | Datetimes, durations, decimals, sets, ranges, record links, geospatial fields                              |
| [Querying](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/querying.md)                                | QuerySet methods, filter lookups, `Q` objects, `FETCH`, aggregations, time travel, subqueries, query cache |
| [Writing data](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/writing.md)                             | Bulk operations, signals, transactions, upserts, patches, retry on conflict                                |
| [Relations and graph](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/relations.md)                    | Graph relations, typed edges, `TYPE RELATION` constraints                                                  |
| [Stored functions and events](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/functions-and-events.md) | `call_function()`, `DEFINE EVENT` and `DEFINE FUNCTION` from Python                                        |
| [Authentication](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/authentication.md)                    | `signin` / `signup`, record access, `AuthenticatedUserMixin`                                               |
| [Live queries and change feeds](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/realtime.md)           | Live queries, typed streams, auto-resubscribe, change feeds                                                |
| [Connections](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/connections.md)                          | The connection manager, event loops, several databases                                                     |
| [Embedded engine](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/embedded.md)                         | `mem://` and `surrealkv://` in-process databases, test fixtures                                            |
| [Schema and DDL](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/schema.md)                            | Schema introspection, materialized views, table DDL and permissions                                        |
| [Search](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/search.md)                                    | Full-text (BM25), vector (KNN, HNSW) and hybrid search                                                     |
| [Migrations](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/migrations.md)                            | `makemigrations()`, operations, `MigrationExecutor`                                                        |
| [Command line](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/cli.md)                                 | `surreal-orm-lite`: migrate, rollback, inspectdb, schemadiff, shell                                        |
| [Testing](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/testing.md)                                  | `ModelFactory`, `Faker` and the pytest plugin                                                              |
| [Compatibility](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/compatibility.md)                      | Tested versions and every behaviour that differs between SurrealDB 2.x, 3.x and the embedded engine        |

The [documentation index](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/README.md) lists every section of the guides.

## Features

| Feature                                                                       | Status |
| ----------------------------------------------------------------------------- | ------ |
| Async/await support                                                           | ✅     |
| Pydantic validation                                                           | ✅     |
| CRUD operations                                                               | ✅     |
| QuerySet with filters                                                         | ✅     |
| Django-style lookups                                                          | ✅     |
| Custom primary keys                                                           | ✅     |
| HTTP connections                                                              | ✅     |
| WebSocket connections                                                         | ✅     |
| Aggregations                                                                  | ✅     |
| GROUP BY                                                                      | ✅     |
| Model Signals                                                                 | ✅     |
| Raw SurrealQL queries                                                         | ✅     |
| Q Objects (OR/AND/NOT)                                                        | ✅     |
| Parameterized filters                                                         | ✅     |
| Bulk operations                                                               | ✅     |
| `-field` ordering                                                             | ✅     |
| Relations & Graph                                                             | ✅     |
| FETCH clause                                                                  | ✅     |
| Transactions (`tx=`)                                                          | ✅     |
| upsert / get_or_create                                                        | ✅     |
| patch / atomic ops                                                            | ✅     |
| Retry on conflict                                                             | ✅     |
| Server-side functions                                                         | ✅     |
| Computed fields                                                               | ✅     |
| Stored `fn::` calls                                                           | ✅     |
| JWT / record auth                                                             | ✅     |
| Model-level auth mixin                                                        | ✅     |
| Field aliases & DX                                                            | ✅     |
| Live queries (raw)                                                            | ✅     |
| Typed live queries                                                            | ✅     |
| Live auto-resubscribe                                                         | ✅     |
| Change feeds (CDC)                                                            | ✅     |
| Typed edges (`insert_relation`)                                               | ✅     |
| Custom table name                                                             | ✅     |
| Rich field types (datetime, date, duration, decimal, uuid, bytes, set, range) | ✅     |
| Geospatial fields (nearby, distance, spatial lookups)                         | ✅     |
| Embedded engine (mem://, surrealkv://) & test fixtures                        | ✅     |
| Time travel (`as_of()`, versioned storage)                                    | ✅     |
| Subqueries (`Subquery`, a QuerySet as a filter value)                         | ✅     |
| Query cache (`cache()`, TTL, invalidated by ORM writes)                       | ✅     |
| Multiple databases (named connections, `using()`)                             | ✅     |
| Schema introspection (`DatabaseIntrospector`, `generate_models_from_db()`)    | ✅     |
| Server-side events & function DDL (`Event`, `define_function()`)              | ✅     |
| Materialized views (`view_query`, `define_view()`, read-only)                 | ✅     |
| Relation constraints (`relation_in`, `define_relation()`, `TYPE RELATION`)    | ✅     |
| Model table DDL (`define_table()`, `schema_mode`, `permissions`)              | ✅     |
| Full-text search (`search()`, BM25 score and highlights)                      | ✅     |
| Vector search (`similar_to()`, KNN, HNSW indexes)                             | ✅     |
| Hybrid search (`hybrid_search()`, Reciprocal Rank Fusion)                     | ✅     |
| Migrations (makemigrations / migrate / rollback)                              | ✅     |
| Command line (`surreal-orm-lite`: migrations, inspectdb, shell)               | ✅     |
| Test data factories (`ModelFactory`, `Faker`) & pytest plugin                 | ✅     |

## Supported versions

Surreal ORM Lite runs on **both** SurrealDB release lines, 2.x (2.6 / 2.7) and 3.x (3.2 / 3.3), and on the official
SDK's in-process engine. It is tested against SurrealDB **v2.7.0** and **v3.3.5**; SurrealDB
3.1.5 is run as a backward-compatibility check. A few capabilities rely on server features introduced in SurrealDB
3.x: on 2.x the ORM degrades gracefully or says so before sending anything. The
[compatibility guide](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/compatibility.md) lists every tested version and every difference between the two lines
and the embedded engine.

## SurrealDB-ORM-lite vs SurrealDB-ORM

This project prioritizes **stability and compatibility** with the official SurrealDB Python SDK. The full [SurrealDB-ORM](https://github.com/EulogySnowfall/SurrealDB-ORM/) uses a custom SDK for advanced features.

Both projects target the same feature set; the difference is **how** (official SDK vs custom
SDK) and **server support**. Everything below is on the lite roadmap via the official SDK 2.0
— only the custom-SDK internals stay exclusive to the full ORM; the official SDK's in-process engine (section 26) is lite-only.

| Feature                       | ORM-lite (official SDK)                                               | ORM (custom SDK)                          |
| ----------------------------- | --------------------------------------------------------------------- | ----------------------------------------- |
| Supported SurrealDB           | **2.6/2.7 + 3.2/3.3**                                                 | 3.x only                                  |
| CRUD & QuerySet               | ✅                                                                    | ✅                                        |
| Aggregations & GROUP BY       | ✅                                                                    | ✅                                        |
| Model Signals                 | ✅                                                                    | ✅                                        |
| Bulk Operations               | ✅                                                                    | ✅                                        |
| Q Objects (OR/AND/NOT)        | ✅                                                                    | ✅                                        |
| Parameterized Filters         | ✅                                                                    | ✅                                        |
| Relations & Graph             | ✅ (reverse, keyword edge fields, tx= on every write helper: v0.22.0) | ✅                                        |
| FETCH clause                  | ✅                                                                    | ✅                                        |
| Transactions (tx=)            | ✅ v0.8 (core), v0.9 QS                                               | ✅                                        |
| Interactive tx (3.x native)   | ✅ v0.9                                                               | ✅                                        |
| upsert / update_or_create     | ✅ v0.10.0                                                            | ✅                                        |
| Atomic field/array operations | ✅ v0.11.0                                                            | ✅                                        |
| Retry on conflict             | ✅ v0.12.0                                                            | ✅                                        |
| SurrealFunc & server values   | ✅ v0.13.0                                                            | ✅                                        |
| Computed fields               | ✅ v0.14.0                                                            | ✅                                        |
| `call_function()` (`fn::`)    | ✅ v0.15.0                                                            | ✅                                        |
| JWT Authentication            | ✅ v0.16.0 (connection)                                               | ✅                                        |
| Model auth mixin              | ✅ v0.17.0                                                            | ✅                                        |
| Field Aliases & DX            | ✅ v0.18.0                                                            | ✅                                        |
| Live queries (raw notifs)     | ✅ v0.19.0                                                            | ✅                                        |
| Typed live queries            | ✅ v0.20.0                                                            | ✅                                        |
| Live auto-resubscribe / CDC   | ✅ v0.21.0                                                            | ✅                                        |
| Native typed relations        | ✅ v0.22.0 (typed edge models)                                        | ✅ DDL only (TableType.RELATION)          |
| Rich field types              | ✅ v0.23.0                                                            | ✅                                        |
| Geospatial Fields             | ✅ v0.24.0                                                            | ✅                                        |
| Embedded engine               | ✅ v0.25.0 (mem://, surrealkv://; rocksdb:// #233)                    | ❌ (HTTP/WebSocket only)                  |
| Time travel (`VERSION`)       | ✅ v0.26.0 (`as_of()`)                                                | ❌                                        |
| Subqueries                    | ✅ v0.27.0 (`Subquery`, a QuerySet in `filter()`)                     | ✅                                        |
| Query Cache                   | ✅ v0.28.0 (`cache()`, `QueryCache`)                                  | ✅                                        |
| Multi-database                | ✅ v0.29.0 (`add_connection()`, `using()`)                            | ✅                                        |
| Schema Introspection          | ✅ v0.30.0 (`DatabaseIntrospector`, `generate_models_from_db()`)      | ✅                                        |
| DEFINE EVENT                  | ✅ v0.31.0 (`Event`, `define_events()`, `ASYNC` on 3.x)               | ✅ migrations (`DefineEvent`)             |
| `define_function()` (DDL)     | ✅ v0.31.0                                                            | ❌                                        |
| Materialized views            | ✅ v0.32.0 (`view_query`, `define_view()`)                            | ✅                                        |
| TYPE RELATION enforcement     | ✅ v0.33.0 (`relation_in`, `define_relation()`)                       | ✅ DDL (`TableType.RELATION`, migrations) |
| Model table DDL               | ✅ v0.34.0 (`define_table()`)                                         | ✅                                        |
| Full-Text Search              | ✅ v0.35.0 (`search()`)                                               | ✅                                        |
| Vector Search (KNN/HNSW)      | ✅ v0.36.0 (`similar_to()`)                                           | ✅                                        |
| Hybrid Search (RRF)           | ✅ v0.37.0 (`hybrid_search()`)                                        | ✅                                        |
| Migrations & CLI              | ✅ v0.38.0 (migrations), ✅ v0.39.0 (CLI)                             | ✅                                        |
| Test Fixtures & Factories     | ✅ v0.25.0 SurrealFixture; ✅ v0.40.0 factories                       | ✅                                        |
| Retry, Logging, Metrics       | v0.41.0                                                               | ✅                                        |
| Connection Pool               | post-GA (tentative)                                                   | ✅                                        |
| Custom SDK / CBOR Protocol    | ❌ never                                                              | ✅                                        |

**Choose ORM-lite** if you want the official SDK, minimal dependencies, support for SurrealDB
2.x (2.6 / 2.7) **and** 3.x (3.2 / 3.3), and a full feature roadmap built entirely on the official SDK.

**Choose ORM** if you need the custom-SDK internals (CBOR protocol, native connection pool)
or those features available today rather than on the roadmap.

- **SurrealDB-ORM GitHub**: [github.com/EulogySnowfall/SurrealDB-ORM](https://github.com/EulogySnowfall/SurrealDB-ORM/)
- **SurrealDB-ORM PyPI**: [surrealdb-orm](https://pypi.org/project/surrealdb-orm/)

## Project status

Surreal ORM Lite is in **alpha** (0.x): every Tier 1 to Tier 3 feature of the roadmap has shipped, and the next
release, v0.41.0, opens the **beta** phase (API freeze and hardening) on the way to GA as v2.0.0, numbered after the
official SDK 2.0. The [release overview](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/README.md#roadmap) lists every release, and
[docs/ROADMAP.md](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/ROADMAP.md) details each one.

## Contributing

Contributions are welcome: see the [contributing guide](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/contributing.md). Report a vulnerability as the
[security policy](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/SECURITY.md) describes, not in a public issue.

## License

MIT License - see [LICENSE](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/LICENSE) for details.

## Author

**Yannick Croteau**
GitHub: [@EulogySnowfall](https://github.com/EulogySnowfall)

## Related Projects

- [SurrealDB](https://surrealdb.com/) - The database
- [surrealdb.py](https://github.com/surrealdb/surrealdb.py) - Official Python SDK
- [SurrealDB-ORM](https://github.com/EulogySnowfall/SurrealDB-ORM/) - Full-featured ORM with custom SDK
