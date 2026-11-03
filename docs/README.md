# Surreal ORM Lite documentation

The user guide of [Surreal ORM Lite](../README.md): a Django-style async ORM for SurrealDB built on the official
SurrealDB Python SDK. Start with [Getting started](getting-started.md), then open the guide of the feature you need.
The same code runs on SurrealDB 2.x, 3.x and the SDK's embedded engine; where they behave differently, the guide
says so and [Compatibility](compatibility.md) lists every difference.

## Guides

- **[Getting started](getting-started.md)**
  - [Why This Project?](getting-started.md#why-this-project)
  - [Requirements](getting-started.md#requirements)
  - [Installation](getting-started.md#installation)
  - [Quick Start](getting-started.md#quick-start)
    - [1. Configure the Connection](getting-started.md#1-configure-the-connection)
    - [2. Define a Model](getting-started.md#2-define-a-model)
    - [3. CRUD Operations](getting-started.md#3-crud-operations)
- **[Models](models.md)**
  - [Custom Primary Key](models.md#custom-primary-key)
  - [Custom table name (`table_name`)](models.md#custom-table-name-table_name)
  - [15. Server-side values: `SurrealFunc` + `server_values=`](models.md#15-server-side-values-surrealfunc--server_values)
  - [16. Computed fields: `Computed[...]` → `DEFINE FIELD … VALUE`](models.md#16-computed-fields-computed--define-field--value)
  - [20. Field aliases, `server_fields` and `merge(refresh=False)`](models.md#20-field-aliases-server_fields-and-mergerefreshfalse)
- **[Field types](fields.md)**
  - [24. Rich field types](fields.md#24-rich-field-types)
  - [25. Geospatial fields](fields.md#25-geospatial-fields)
- **[Querying](querying.md)**
  - [4. QuerySet Methods](querying.md#4-queryset-methods)
  - [Supported Filter Lookups](querying.md#supported-filter-lookups)
  - [5. Q Objects (Complex Queries)](querying.md#5-q-objects-complex-queries)
  - [8. FETCH Clause](querying.md#8-fetch-clause)
  - [9. Aggregations](querying.md#9-aggregations)
  - [27. Time travel (`as_of()`)](querying.md#27-time-travel-as_of)
  - [28. Subqueries (`Subquery`)](querying.md#28-subqueries-subquery)
  - [29. Query cache (`cache()`, `QueryCache`)](querying.md#29-query-cache-cache-querycache)
- **[Writing data](writing.md)**
  - [6. Bulk Operations](writing.md#6-bulk-operations)
  - [10. Model Signals](writing.md#10-model-signals)
  - [11. Transactions (atomic, all-or-nothing)](writing.md#11-transactions-atomic-all-or-nothing)
  - [12. Upsert & get_or_create / update_or_create](writing.md#12-upsert--get_or_create--update_or_create)
  - [13. Patch & atomic field/array operations](writing.md#13-patch--atomic-fieldarray-operations)
  - [14. Optimistic concurrency: `retry_on_conflict`](writing.md#14-optimistic-concurrency-retry_on_conflict)
- **[Relations and graph](relations.md)**
  - [7. Relations & Graph](relations.md#7-relations--graph)
  - [23. Typed edges (`BaseSurrealEdge`)](relations.md#23-typed-edges-basesurrealedge)
  - [34. Relation constraints (`relation_in`, `define_relation()`)](relations.md#34-relation-constraints-relation_in-define_relation)
- **[Stored functions and events](functions-and-events.md)**
  - [17. Stored functions: `call_function()`](functions-and-events.md#17-stored-functions-call_function)
  - [32. Server-side events and stored functions (`Event`, `define_events()`, `define_function()`)](functions-and-events.md#32-server-side-events-and-stored-functions-event-define_events-define_function)
- **[Authentication](authentication.md)**
  - [18. Authentication (`signin` / `signup` / `info` / `invalidate`)](authentication.md#18-authentication-signin--signup--info--invalidate)
  - [19. Model-level authentication (`AuthenticatedUserMixin`)](authentication.md#19-model-level-authentication-authenticatedusermixin)
- **[Live queries and change feeds](realtime.md)**
  - [21. Live queries (real-time notifications)](realtime.md#21-live-queries-real-time-notifications)
  - [22. Change feeds (`changes()`)](realtime.md#22-change-feeds-changes)
- **[Connections](connections.md)**
  - [Context Manager](connections.md#context-manager)
  - [Connections and event loops](connections.md#connections-and-event-loops)
  - [30. Multiple databases (`add_connection()`, `using()`)](connections.md#30-multiple-databases-add_connection-using)
- **[Embedded engine](embedded.md)**
  - [26. Embedded engine and test fixtures](embedded.md#26-embedded-engine-and-test-fixtures)
- **[Schema and DDL](schema.md)**
  - [31. Schema introspection (`DatabaseIntrospector`, `generate_models_from_db()`)](schema.md#31-schema-introspection-databaseintrospector-generate_models_from_db)
  - [33. Materialized views (`view_query`, `define_view()`)](schema.md#33-materialized-views-view_query-define_view)
  - [35. Model table DDL (`define_table()`, `schema_mode`, `permissions`)](schema.md#35-model-table-ddl-define_table-schema_mode-permissions)
- **[Search](search.md)**
  - [36. Full-text search (`search()`, `SearchScore`, `SearchHighlight`, `Analyzer`, `Index`)](search.md#36-full-text-search-search-searchscore-searchhighlight-analyzer-index)
  - [37. Vector search (`similar_to()`, `VectorField`, `Index(hnsw=True)`)](search.md#37-vector-search-similar_to-vectorfield-indexhnswtrue)
  - [38. Hybrid search (`hybrid_search()`, Reciprocal Rank Fusion)](search.md#38-hybrid-search-hybrid_search-reciprocal-rank-fusion)
- **[Migrations](migrations.md)**
  - [39. Migrations (`makemigrations()`, `MigrationExecutor`, operations)](migrations.md#39-migrations-makemigrations-migrationexecutor-operations)
- **[Command line](cli.md)**
  - [40. Command line (`surreal-orm-lite`)](cli.md#40-command-line-surreal-orm-lite)
- **[Testing](testing.md)**
  - [41. Test data factories (`ModelFactory`, `Faker`) and the pytest plugin](testing.md#41-test-data-factories-modelfactory-faker-and-the-pytest-plugin)
- **[Compatibility](compatibility.md)**
  - [ORM behaviour: SurrealDB 2.x vs 3.x](compatibility.md#orm-behaviour-surrealdb-2x-vs-3x)
  - [ORM behaviour: the embedded engine](compatibility.md#orm-behaviour-the-embedded-engine)
- **[Contributing](contributing.md)**

## Sections by number

The guides keep the section numbers of the manual, so a reference such as "section 26" names one place.

| Section                                                                                                                                                                                                 | Guide                         |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------- |
| [1. Configure the Connection](getting-started.md#1-configure-the-connection)                                                                                                                            | Getting started               |
| [2. Define a Model](getting-started.md#2-define-a-model)                                                                                                                                                | Getting started               |
| [3. CRUD Operations](getting-started.md#3-crud-operations)                                                                                                                                              | Getting started               |
| [4. QuerySet Methods](querying.md#4-queryset-methods)                                                                                                                                                   | Querying                      |
| [5. Q Objects (Complex Queries)](querying.md#5-q-objects-complex-queries)                                                                                                                               | Querying                      |
| [6. Bulk Operations](writing.md#6-bulk-operations)                                                                                                                                                      | Writing data                  |
| [7. Relations & Graph](relations.md#7-relations--graph)                                                                                                                                                 | Relations and graph           |
| [8. FETCH Clause](querying.md#8-fetch-clause)                                                                                                                                                           | Querying                      |
| [9. Aggregations](querying.md#9-aggregations)                                                                                                                                                           | Querying                      |
| [10. Model Signals](writing.md#10-model-signals)                                                                                                                                                        | Writing data                  |
| [11. Transactions (atomic, all-or-nothing)](writing.md#11-transactions-atomic-all-or-nothing)                                                                                                           | Writing data                  |
| [12. Upsert & get_or_create / update_or_create](writing.md#12-upsert--get_or_create--update_or_create)                                                                                                  | Writing data                  |
| [13. Patch & atomic field/array operations](writing.md#13-patch--atomic-fieldarray-operations)                                                                                                          | Writing data                  |
| [14. Optimistic concurrency: `retry_on_conflict`](writing.md#14-optimistic-concurrency-retry_on_conflict)                                                                                               | Writing data                  |
| [15. Server-side values: `SurrealFunc` + `server_values=`](models.md#15-server-side-values-surrealfunc--server_values)                                                                                  | Models                        |
| [16. Computed fields: `Computed[...]` → `DEFINE FIELD … VALUE`](models.md#16-computed-fields-computed--define-field--value)                                                                             | Models                        |
| [17. Stored functions: `call_function()`](functions-and-events.md#17-stored-functions-call_function)                                                                                                    | Stored functions and events   |
| [18. Authentication (`signin` / `signup` / `info` / `invalidate`)](authentication.md#18-authentication-signin--signup--info--invalidate)                                                                | Authentication                |
| [19. Model-level authentication (`AuthenticatedUserMixin`)](authentication.md#19-model-level-authentication-authenticatedusermixin)                                                                     | Authentication                |
| [20. Field aliases, `server_fields` and `merge(refresh=False)`](models.md#20-field-aliases-server_fields-and-mergerefreshfalse)                                                                         | Models                        |
| [21. Live queries (real-time notifications)](realtime.md#21-live-queries-real-time-notifications)                                                                                                       | Live queries and change feeds |
| [22. Change feeds (`changes()`)](realtime.md#22-change-feeds-changes)                                                                                                                                   | Live queries and change feeds |
| [23. Typed edges (`BaseSurrealEdge`)](relations.md#23-typed-edges-basesurrealedge)                                                                                                                      | Relations and graph           |
| [24. Rich field types](fields.md#24-rich-field-types)                                                                                                                                                   | Field types                   |
| [25. Geospatial fields](fields.md#25-geospatial-fields)                                                                                                                                                 | Field types                   |
| [26. Embedded engine and test fixtures](embedded.md#26-embedded-engine-and-test-fixtures)                                                                                                               | Embedded engine               |
| [27. Time travel (`as_of()`)](querying.md#27-time-travel-as_of)                                                                                                                                         | Querying                      |
| [28. Subqueries (`Subquery`)](querying.md#28-subqueries-subquery)                                                                                                                                       | Querying                      |
| [29. Query cache (`cache()`, `QueryCache`)](querying.md#29-query-cache-cache-querycache)                                                                                                                | Querying                      |
| [30. Multiple databases (`add_connection()`, `using()`)](connections.md#30-multiple-databases-add_connection-using)                                                                                     | Connections                   |
| [31. Schema introspection (`DatabaseIntrospector`, `generate_models_from_db()`)](schema.md#31-schema-introspection-databaseintrospector-generate_models_from_db)                                        | Schema and DDL                |
| [32. Server-side events and stored functions (`Event`, `define_events()`, `define_function()`)](functions-and-events.md#32-server-side-events-and-stored-functions-event-define_events-define_function) | Stored functions and events   |
| [33. Materialized views (`view_query`, `define_view()`)](schema.md#33-materialized-views-view_query-define_view)                                                                                        | Schema and DDL                |
| [34. Relation constraints (`relation_in`, `define_relation()`)](relations.md#34-relation-constraints-relation_in-define_relation)                                                                       | Relations and graph           |
| [35. Model table DDL (`define_table()`, `schema_mode`, `permissions`)](schema.md#35-model-table-ddl-define_table-schema_mode-permissions)                                                               | Schema and DDL                |
| [36. Full-text search (`search()`, `SearchScore`, `SearchHighlight`, `Analyzer`, `Index`)](search.md#36-full-text-search-search-searchscore-searchhighlight-analyzer-index)                             | Search                        |
| [37. Vector search (`similar_to()`, `VectorField`, `Index(hnsw=True)`)](search.md#37-vector-search-similar_to-vectorfield-indexhnswtrue)                                                                | Search                        |
| [38. Hybrid search (`hybrid_search()`, Reciprocal Rank Fusion)](search.md#38-hybrid-search-hybrid_search-reciprocal-rank-fusion)                                                                        | Search                        |
| [39. Migrations (`makemigrations()`, `MigrationExecutor`, operations)](migrations.md#39-migrations-makemigrations-migrationexecutor-operations)                                                         | Migrations                    |
| [40. Command line (`surreal-orm-lite`)](cli.md#40-command-line-surreal-orm-lite)                                                                                                                        | Command line                  |
| [41. Test data factories (`ModelFactory`, `Faker`) and the pytest plugin](testing.md#41-test-data-factories-modelfactory-faker-and-the-pytest-plugin)                                                   | Testing                       |

## Other documents

- [Roadmap](ROADMAP.md): every release, its design notes and what comes next.
- [Changelog](../CHANGELOG.md): what changed in each version.
- [Security policy](../SECURITY.md): supported versions and how to report a vulnerability.
- [Runnable examples](../examples/): one Jupyter notebook per feature release.

## Roadmap

| Version           | Theme                                                | Status      |
| ----------------- | ---------------------------------------------------- | ----------- |
| v0.2.x – v0.7.0   | Core ORM → SDK 2.0 / SurrealDB 3.x migration         | ✅ Released |
| v0.8.0            | Transactions ORM (`tx=`)                             | ✅ Released |
| v0.9.0            | Transactions — QuerySet & interactive (3.x)          | ✅ Released |
| v0.10.0           | upsert / update_or_create / get_or_create            | ✅ Released |
| v0.11.0           | patch / atomic field & array ops                     | ✅ Released |
| v0.12.0           | retry_on_conflict & optimistic concurrency           | ✅ Released |
| v0.13.0           | SurrealFunc & server-side values                     | ✅ Released |
| v0.14.0           | Computed fields (`DEFINE FIELD … VALUE`)             | ✅ Released |
| v0.14.3 – v0.14.5 | Correctness: `$`-values, record-id lookups, loops    | ✅ Released |
| v0.15.0           | `call_function()` — custom `fn::` stored functions   | ✅ Released |
| v0.16.0           | Connection auth (`signin`/`signup`/`info`)           | ✅ Released |
| v0.17.0           | Model auth (`AuthenticatedUserMixin`)                | ✅ Released |
| v0.18.0           | Field aliases, `server_fields`, `merge(refresh=)`    | ✅ Released |
| v0.19.0           | Live queries (base): `live()` / `kill()`, raw notifs | ✅ Released |
| v0.20.0           | Typed live queries: `LiveModelStream`, diff, filters | ✅ Released |
| v0.21.0           | Auto-resubscribe & change feeds (`changes()`)        | ✅ Released |
| v0.22.0           | Tier 1 — Native typed relations                      | ✅ Released |
| v0.23.0           | Rich field types                                     | ✅ Released |
| v0.24.0           | Geospatial fields                                    | ✅ Released |
| v0.25.0           | Embedded test engine                                 | ✅ Released |
| v0.26.0           | Time travel (versioned storage)                      | ✅ Released |
| v0.27.0           | Subqueries                                           | ✅ Released |
| v0.28.0           | Query cache                                          | ✅ Released |
| v0.29.0           | Tier 2 — Multi-database                              | ✅ Released |
| v0.30.0           | Tier 3 — Schema introspection                        | ✅ Released |
| v0.31.0           | Tier 3 — Server-side events and DDL helpers          | ✅ Released |
| v0.32.0           | Tier 3 — Materialized views                          | ✅ Released |
| v0.33.0           | Tier 3 — `TYPE RELATION` constraints                 | ✅ Released |
| v0.34.0           | Tier 3 — Model table DDL                             | ✅ Released |
| v0.35.0           | Tier 3 — Full-text search                            | ✅ Released |
| v0.36.0           | Tier 3 — Vector search                               | ✅ Released |
| v0.37.0           | Tier 3 — Hybrid search                               | ✅ Released |
| v0.38.0           | Tier 3 — Migration system                            | ✅ Released |
| v0.39.0           | Tier 3 — Command line (`surreal-orm-lite`)           | ✅ Released |
| v0.40.0           | Tier 3 — Test fixtures & factories                   | ✅ Released |
| v0.41.0           | Beta Phase (API freeze, hardening)                   | 📋 Planned  |
| v2.0.0            | Production / GA (aligned with SDK 2.0)               | 📋 Planned  |

> Every roadmap feature is implementable with the **official SDK 2.0** (native methods or
> `query()` SurrealQL) — no custom SDK. GA is numbered **v2.0.0** to mirror SDK 2.0; the `1.x`
> line is intentionally skipped.

See [docs/ROADMAP.md](ROADMAP.md) for full details.
