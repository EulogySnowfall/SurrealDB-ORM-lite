# Surreal ORM Lite

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![SurrealDB](https://img.shields.io/badge/SurrealDB-2.6.5%20%7C%203.1.3-purple)
![SDK](https://img.shields.io/badge/SDK-Official%202.0-green)
![License](https://img.shields.io/badge/license-MIT-blue)
[![codecov](https://codecov.io/gh/EulogySnowfall/SurrealDB-ORM-lite/graph/badge.svg)](https://codecov.io/gh/EulogySnowfall/SurrealDB-ORM-lite)

**Surreal ORM Lite** is a lightweight, Django-style ORM for [SurrealDB](https://surrealdb.com/) that uses the **official SurrealDB Python SDK**. It provides a simple and intuitive interface for database operations with full async support and Pydantic validation.

## Why This Project?

This ORM is designed to:

- Use the **official SurrealDB SDK** (`surrealdb[pydantic]>=2.0.0,<3.0.0`) for maximum compatibility
- Stay **lightweight** with minimal dependencies
- Keep **up-to-date** with SurrealDB and SDK releases
- Provide **Django-style** query syntax that developers love

---

## Requirements

| Dependency   | Version                           |
| ------------ | --------------------------------- |
| Python       | 3.11+                             |
| SurrealDB    | 2.6.x or 3.1.x                    |
| Official SDK | surrealdb[pydantic]>=2.0.0,<3.0.0 |
| Pydantic     | >=2.13.4                          |

> **Note**: As of v0.7.0, Surreal ORM Lite targets the SurrealDB Python SDK 2.x (`surrealdb[pydantic]>=2.0.0,<3.0.0`), which supports the SurrealDB 3.x protocol. It is tested against SurrealDB **v2.6.5** and **v3.1.3**.

---

## Installation

```bash
pip install surreal-orm-lite
```

Or with uv:

```bash
uv add surreal-orm-lite
```

---

## Quick Start

### 1. Configure the Connection

```python
from surreal_orm_lite import SurrealDBConnectionManager

SurrealDBConnectionManager.set_connection(
    url="http://localhost:8000",
    user="root",
    password="root",
    namespace="my_namespace",
    database="my_database",
)
```

### 2. Define a Model

```python
from surreal_orm_lite import BaseSurrealModel
from pydantic import Field

class User(BaseSurrealModel):
    id: str | None = None
    name: str = Field(..., max_length=100)
    email: str
    age: int = Field(..., ge=0)
```

### 3. CRUD Operations

```python
# Create
user = User(name="Alice", email="alice@example.com", age=30)
await user.save()

# Read
user = await User.objects().get("alice_id")
users = await User.objects().filter(age__gte=18).exec()

# Update
user.age = 31
await user.update()

# Or partial update
await user.merge(age=31)

# Delete
await user.delete()
```

### 4. QuerySet Methods

```python
# Filter with Django-style lookups
users = await User.objects().filter(
    age__gte=18,
    name__startswith="A"
).exec()

# Ordering (with -field shorthand for DESC)
users = await User.objects().order_by("name").exec()
users = await User.objects().order_by("-age", "name").exec()

# Pagination
users = await User.objects().limit(10).offset(20).exec()

# Select specific fields
results = await User.objects().select("name", "email").exec()

# Get first result
user = await User.objects().filter(name="Alice").first()

# Get all records
all_users = await User.objects().all()

# Custom query
results = await User.objects().query(
    "SELECT * FROM User WHERE age > $min_age",
    {"min_age": 21}
)
```

---

## Features

| Feature                | Status |
| ---------------------- | ------ |
| Async/await support    | ✅     |
| Pydantic validation    | ✅     |
| CRUD operations        | ✅     |
| QuerySet with filters  | ✅     |
| Django-style lookups   | ✅     |
| Custom primary keys    | ✅     |
| HTTP connections       | ✅     |
| WebSocket connections  | ✅     |
| Aggregations           | ✅     |
| GROUP BY               | ✅     |
| Model Signals          | ✅     |
| Raw SurrealQL queries  | ✅     |
| Q Objects (OR/AND/NOT) | ✅     |
| Parameterized filters  | ✅     |
| Bulk operations        | ✅     |
| `-field` ordering      | ✅     |
| Relations & Graph      | ✅     |
| FETCH clause           | ✅     |
| Transactions (`tx=`)   | ✅     |
| upsert / get_or_create | ✅     |

### Supported Filter Lookups

- `exact` (default)
- `gt`, `gte`, `lt`, `lte`
- `in`, `not_in`
- `contains`, `not_contains`
- `containsall`, `containsany`
- `startswith`, `endswith`
- `like`, `ilike`
- `match`, `regex`
- `isnull`

### 5. Q Objects (Complex Queries)

```python
from surreal_orm_lite import Q

# OR queries
users = await User.objects().filter(Q(name="Alice") | Q(name="Bob")).exec()

# NOT queries
active = await User.objects().filter(~Q(status="banned")).exec()

# Complex combinations
results = await User.objects().filter(
    Q(age__gte=18) & (Q(role="admin") | Q(role="mod"))
).exec()

# Mix Q objects with keyword filters
results = await User.objects().filter(
    Q(role="admin") | Q(role="mod"),
    age__gte=25
).exec()
```

### 6. Bulk Operations

```python
# Bulk create
users = [User(name="Alice", age=30), User(name="Bob", age=25)]
created = await User.objects().bulk_create(users)

# Bulk update (returns count of updated records)
count = await User.objects().filter(status="pending").bulk_update(status="active")

# Bulk delete (returns count of deleted records)
count = await User.objects().filter(status="inactive").bulk_delete()
```

### 7. Relations & Graph

```python
# Create a relation
await user.relate("follows", other_user)

# With data on the edge
await user.relate("purchased", product, data={"quantity": 2, "price": 29.99})

# Get related records (outgoing)
following = await user.get_related("follows", direction="out", model_class=User)

# Get related records (incoming)
followers = await user.get_related("follows", direction="in", model_class=User)

# Remove a specific relation
await user.remove_relation("follows", other_user)

# Remove all outgoing relations of a type
await user.remove_all_relations("follows", direction="out")

# Graph traversal
friends_of_friends = await user.traverse("->follows->User->follows->User")
```

### 8. FETCH Clause

```python
# Resolve record links inline (prevents N+1 queries)
posts = await Post.objects().fetch("author", "tags").exec()
# Generates: SELECT * FROM Post FETCH author, tags;
```

### 9. Aggregations

```python
from surreal_orm_lite import Count, Sum, Avg, Min, Max

# Simple aggregations
count = await User.objects().count()
total = await Order.objects().sum("amount")
avg_age = await User.objects().avg("age")
max_price = await Product.objects().max("price")
min_price = await Product.objects().min("price")

# Check existence
has_admins = await User.objects().filter(role="admin").exists()

# GROUP BY with annotations
results = await User.objects().values("status").annotate(count=Count()).exec()
# [{"status": "active", "count": 42}, {"status": "inactive", "count": 8}]

# Raw SurrealQL queries
results = await User.raw_query(
    "SELECT * FROM User WHERE age > $min_age",
    variables={"min_age": 18}
)
```

### 10. Model Signals

```python
from surreal_orm_lite import pre_save, post_save, pre_delete, post_delete

@post_save.connect(User)
async def on_user_saved(sender, instance, created, **kwargs):
    """Called after every User save."""
    if created:
        await send_welcome_email(instance.email)
    await invalidate_cache(f"user:{instance.id}")

@pre_delete.connect(User)
async def on_user_deleting(sender, instance, **kwargs):
    """Called before User deletion."""
    await archive_user_data(instance.id)
```

**Available signals:**

| Signal          | When                        | Extra kwargs    |
| --------------- | --------------------------- | --------------- |
| `pre_save`      | Before `save()`             |                 |
| `post_save`     | After `save()`              | `created`       |
| `pre_update`    | Before `update()`/`merge()` | `update_fields` |
| `post_update`   | After `update()`/`merge()`  | `update_fields` |
| `pre_delete`    | Before `delete()`           |                 |
| `post_delete`   | After `delete()`            |                 |
| `around_save`   | Wraps `save()`              |                 |
| `around_update` | Wraps `update()`/`merge()`  | `update_fields` |
| `around_delete` | Wraps `delete()`            |                 |

**Around signals** use async generators to wrap operations:

```python
from surreal_orm_lite import around_save

@around_save.connect(User)
async def time_user_save(sender, instance, **kwargs):
    import time
    start = time.time()
    yield  # save() executes here
    duration = time.time() - start
    print(f"Save took {duration:.3f}s")
```

### 11. Transactions (atomic, all-or-nothing)

```python
from surreal_orm_lite import SurrealDBConnectionManager

# All operations commit together, or none do.
async with SurrealDBConnectionManager.transaction() as tx:
    await User(id="alice", name="Alice").save(tx=tx)
    await Order(id="o1", user="User:alice", total=100).save(tx=tx)

    # v0.9.0: QuerySet reads & bulk ops participate in the transaction.
    actives = await User.objects(tx=tx).filter(status="active").exec()
    await User.objects(tx=tx).filter(role="guest").bulk_update(role="member")
    # Auto-commit on success; auto-rollback if the block raises.
```

`transaction()` picks the strategy automatically based on the connection:

- **WebSocket + SurrealDB 3.x** → `InteractiveTransaction` (native `begin()`/`commit()`/`cancel()`).
  Reads inside the tx see uncommitted writes; `save(tx=)` supports auto-generated ids;
  `refresh(tx=)` and `QuerySet.objects(tx=)` reads work.
- **HTTP, or WebSocket on SurrealDB 2.6.x** → `BufferedTransaction`. Writes are buffered and
  flushed as one `BEGIN TRANSACTION; …; COMMIT TRANSACTION;` query at commit; reads inside the
  tx raise; `save(tx=)` requires an explicit `id`. `bulk_update`/`bulk_delete` return `0`
  (the row count is not knowable before commit).

### 12. Upsert & get_or_create / update_or_create

```python
from surreal_orm_lite import BaseSurrealModel, SurrealConfigDict


class User(BaseSurrealModel):
    model_config = SurrealConfigDict(primary_key="id")
    id: str | None = None
    name: str
    email: str


# Insert-or-replace by explicit id (full REPLACE — omitted fields are dropped).
# Use merge() instead if you only want a partial update.
await User(id="alice", name="Alice", email="alice@example.com").upsert()

# Criteria-based, Django-style; returns (instance, created).
# update_or_create: on create, writes criteria + defaults; on update, MERGEs them (a partial
# update — fields outside the criteria/defaults are preserved). Lifecycle signals fire on both
# paths, and the primary key anchors the record identity.
user, created = await User.objects().update_or_create(
    email="alice@example.com", defaults={"name": "Alice"}
)

# get_or_create writes the defaults ONLY when creating; an existing match is returned as-is:
user, created = await User.objects().get_or_create(
    email="bob@example.com", defaults={"name": "Bob"}
)

# Both participate in a transaction via objects(tx=) (interactive on SurrealDB 3.x):
async with SurrealDBConnectionManager.transaction() as tx:
    user, created = await User.objects(tx=tx).get_or_create(email="z@x.io", defaults={"name": "Z"})
```

If the lookup criteria match more than one record, both methods raise `SurrealDbError`
(the criteria are not unique). Non-`exact` lookups (e.g. `name__contains`) drive the lookup
but are not written to the record. Without a transaction the behaviour is identical on
SurrealDB 2.6.x and 3.x; under `objects(tx=)` they participate in the transaction on 3.x,
while a buffered 2.6.x transaction raises on the lookup (see the behaviour table).

---

## Configuration Options

### Custom Primary Key

```python
from surreal_orm_lite import BaseSurrealModel, SurrealConfigDict

class Product(BaseSurrealModel):
    model_config = SurrealConfigDict(primary_key="sku")

    sku: str
    name: str
    price: float
```

### Context Manager

```python
async with SurrealDBConnectionManager():
    users = await User.objects().all()
# Connection automatically closed
```

---

## Compatibility

As of v0.7.0, Surreal ORM Lite uses `surrealdb[pydantic]>=2.0.0,<3.0.0` (SurrealDB 3.x protocol) and is tested against both major SurrealDB release lines.

**Compatibility advantage over the full ORM**: ORM-lite runs on **both** SurrealDB **2.6.x and 3.1**, while the full [SurrealDB-ORM](https://github.com/EulogySnowfall/SurrealDB-ORM/) (custom SDK) targets **3.x only**. Lite stays usable on existing 2.6.x deployments without forcing a server upgrade.

| SurrealDB Version | SDK Version | Status            |
| ----------------- | ----------- | ----------------- |
| 3.1.3             | 2.0         | ✅ Tested         |
| 2.6.5             | 2.0         | ✅ Tested         |
| 2.6.x             | 2.0         | ✅ Compatible     |
| < 2.6 or > 3.1    | —           | ⚠️ Not guaranteed |

### ORM behaviour: SurrealDB 2.6.x vs 3.x

Surreal ORM Lite runs on both lines; some capabilities differ because they rely on server
features introduced in SurrealDB 3.x. On 2.6.x the ORM degrades gracefully. Capabilities not
listed behave the same on both lines.

| ORM capability | SurrealDB 2.6.x | SurrealDB 3.x (3.1.3) | Since |
| -------------- | --------------- | --------------------- | ----- |
| Transaction strategy auto-selected by `transaction()` | buffered batch (`BEGIN…COMMIT`) | native interactive on WebSocket | v0.9.0 |
| Reads inside a transaction (`objects(tx=)`) | raise (buffered cannot read) | see uncommitted writes | v0.9.0 |
| `save(tx=)` with an auto-generated id | raises — explicit id required | supported | v0.9.0 |
| `refresh(tx=)` inside a transaction | raises | works | v0.9.0 |
| `bulk_update` / `bulk_delete` row count inside a tx | returns `0` (not knowable pre-commit) | real count | v0.9.0 |
| "Already exists" error on create | normalised to `SurrealDbError` | normalised to `SurrealDbError` | v0.7.0 |
| Cleanup on a missing target (`delete_table`, `remove_relation`) | native no-op | ORM makes it a silent no-op | v0.7.0 |
| Aggregation over an empty set (`NaN` / `±inf`) | returns `0.0` / `None` | ORM normalises to `0.0` / `None` | v0.7.0 |
| Namespace/db selection (`use()` ordering) | lenient (auto-creates) | strict — ORM signs in before `use()` | v0.7.0 |
| `upsert()` / `update_or_create()` / `get_or_create()` | same on both lines | same on both lines | v0.10.0 |

> **Note on record IDs**: A record loaded from the database has its `id` field set to a native `surrealdb.RecordID` object, not a plain string. Use `model.get_raw_id()` to obtain the bare identifier string (e.g. `"alice"`), or compare directly with `model.id == RecordID("User", "alice")`. In-memory instances you construct yourself retain whatever value you assign.

---

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m "Add amazing feature"`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

## Roadmap

| Version           | Theme                                            | Status      |
| ----------------- | ------------------------------------------------ | ----------- |
| v0.2.x – v0.7.0   | Core ORM → SDK 2.0 / SurrealDB 3.x migration     | ✅ Released |
| v0.8.0            | Transactions ORM (`tx=`)                         | ✅ Released |
| v0.9.0            | Transactions — QuerySet & interactive (3.x)      | ✅ Released |
| v0.10.0           | upsert / update_or_create / get_or_create        | ✅ Released |
| v0.11.0 – v0.22.0 | Tier 1 — Core (auth, live, relations, …)         | 📋 Planned  |
| v0.23.0 – v0.29.0 | Tier 2 — Extended (rich types, geo, subqueries)  | 📋 Planned  |
| v0.30.0 – v0.39.0 | Tier 3 — Advanced (search, DDL, migrations, CLI) | 📋 Planned  |
| v0.40.0           | Beta Phase (API freeze, hardening)               | 📋 Planned  |
| v2.0.0            | Production / GA (aligned with SDK 2.0)           | 📋 Planned  |

> Every roadmap feature is implementable with the **official SDK 2.0** (native methods or
> `query()` SurrealQL) — no custom SDK. GA is numbered **v2.0.0** to mirror SDK 2.0; the `1.x`
> line is intentionally skipped.

See [docs/ROADMAP.md](docs/ROADMAP.md) for full details.

---

## SurrealDB-ORM-lite vs SurrealDB-ORM

This project prioritizes **stability and compatibility** with the official SurrealDB Python SDK. The full [SurrealDB-ORM](https://github.com/EulogySnowfall/SurrealDB-ORM/) uses a custom SDK for advanced features.

Both projects target the same feature set; the difference is **how** (official SDK vs custom
SDK) and **server support**. Everything below is on the lite roadmap via the official SDK 2.0
— only the custom-SDK internals stay exclusive to the full ORM.

| Feature                       | ORM-lite (official SDK) | ORM (custom SDK) |
| ----------------------------- | ----------------------- | ---------------- |
| Supported SurrealDB           | **2.6.x + 3.1**         | 3.x only         |
| CRUD & QuerySet               | ✅                      | ✅               |
| Aggregations & GROUP BY       | ✅                      | ✅               |
| Model Signals                 | ✅                      | ✅               |
| Bulk Operations               | ✅                      | ✅               |
| Q Objects (OR/AND/NOT)        | ✅                      | ✅               |
| Parameterized Filters         | ✅                      | ✅               |
| Relations & Graph             | ✅                      | ✅               |
| FETCH clause                  | ✅                      | ✅               |
| Transactions (tx=)            | ✅ v0.8 (core), v0.9 QS | ✅               |
| Interactive tx (3.x native)   | ✅ v0.9                 | ✅               |
| upsert / update_or_create     | ✅ v0.10.0              | ✅               |
| Atomic field/array operations | v0.11.0                 | ✅               |
| Retry on conflict             | v0.12.0                 | ✅               |
| SurrealFunc & Computed        | v0.13 – v0.14           | ✅               |
| JWT Authentication            | v0.16 – v0.17           | ✅               |
| Field Aliases & DX            | v0.18.0                 | ✅               |
| Live Models / CDC             | v0.19 – v0.21           | ✅               |
| Native typed relations        | v0.22.0                 | ✅               |
| Rich field types              | v0.23.0                 | ✅               |
| Geospatial Fields             | v0.24.0                 | ✅               |
| Subqueries & Query Cache      | v0.27 – v0.28           | ✅               |
| Multi-database                | v0.29.0                 | ✅               |
| Schema Introspection          | v0.30.0                 | ✅               |
| DEFINE EVENT                  | v0.31.0                 | ✅               |
| Materialized views            | v0.32.0                 | ✅               |
| Full-Text Search              | v0.34.0                 | ✅               |
| Vector Search (KNN/HNSW)      | v0.35.0                 | ✅               |
| Hybrid Search (RRF)           | v0.36.0                 | ✅               |
| Migrations & CLI              | v0.37 – v0.38           | ✅               |
| Test Fixtures & Factories     | v0.39.0                 | ✅               |
| Retry, Logging, Metrics       | v0.40.0                 | ✅               |
| Connection Pool               | post-GA (tentative)     | ✅               |
| Custom SDK / CBOR Protocol    | ❌ never                | ✅               |

**Choose ORM-lite** if you want the official SDK, minimal dependencies, support for SurrealDB
2.6.x **and** 3.1, and a full feature roadmap built entirely on the official SDK.

**Choose ORM** if you need the custom-SDK internals (CBOR protocol, native connection pool)
or those features available today rather than on the roadmap.

- **SurrealDB-ORM GitHub**: [github.com/EulogySnowfall/SurrealDB-ORM](https://github.com/EulogySnowfall/SurrealDB-ORM/)
- **SurrealDB-ORM PyPI**: [surrealdb-orm](https://pypi.org/project/surrealdb-orm/)

---

## License

MIT License - see [LICENSE](LICENSE) for details.

---

## Author

**Yannick Croteau**
GitHub: [@EulogySnowfall](https://github.com/EulogySnowfall)

---

## Related Projects

- [SurrealDB](https://surrealdb.com/) - The database
- [surrealdb.py](https://github.com/surrealdb/surrealdb.py) - Official Python SDK
- [SurrealDB-ORM](https://github.com/EulogySnowfall/SurrealDB-ORM/) - Full-featured ORM with custom SDK
