# Surreal ORM Lite

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![SurrealDB](https://img.shields.io/badge/SurrealDB-2.6.0-purple)
![SDK](https://img.shields.io/badge/SDK-Official%201.0.8-green)
![License](https://img.shields.io/badge/license-MIT-blue)
[![codecov](https://codecov.io/gh/EulogySnowfall/SurrealDB-ORM-lite/graph/badge.svg)](https://codecov.io/gh/EulogySnowfall/SurrealDB-ORM-lite)

**Surreal ORM Lite** is a lightweight, Django-style ORM for [SurrealDB](https://surrealdb.com/) that uses the **official SurrealDB Python SDK**. It provides a simple and intuitive interface for database operations with full async support and Pydantic validation.

## Why This Project?

This ORM is designed to:

- Use the **official SurrealDB SDK** (`surrealdb>=1.0.8`) for maximum compatibility
- Stay **lightweight** with minimal dependencies
- Keep **up-to-date** with SurrealDB and SDK releases
- Provide **Django-style** query syntax that developers love

---

## Requirements

| Dependency   | Version          |
| ------------ | ---------------- |
| Python       | 3.11+            |
| SurrealDB    | 2.6.0+           |
| Official SDK | surrealdb>=1.0.8 |
| Pydantic     | >=2.12.5         |

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

# Ordering
users = await User.objects().order_by("name").exec()
users = await User.objects().order_by("age", OrderBy.DESC).exec()

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

| Feature               | Status |
| --------------------- | ------ |
| Async/await support   | ✅     |
| Pydantic validation   | ✅     |
| CRUD operations       | ✅     |
| QuerySet with filters | ✅     |
| Django-style lookups  | ✅     |
| Custom primary keys   | ✅     |
| HTTP connections      | ✅     |
| WebSocket connections | ✅     |
| Aggregations          | ✅     |
| GROUP BY              | ✅     |
| Model Signals         | ✅     |
| Raw SurrealQL queries | ✅     |

### Supported Filter Lookups

- `exact` (default)
- `gt`, `gte`, `lt`, `lte`
- `in`
- `contains`, `icontains`
- `startswith`, `istartswith`
- `endswith`, `iendswith`
- `like`, `ilike`
- `match`, `regex`, `iregex`
- `isnull`

### 5. Aggregations

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

### 6. Model Signals

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

| Signal          | When                        | Extra kwargs     |
| --------------- | --------------------------- | ---------------- |
| `pre_save`      | Before `save()`             |                  |
| `post_save`     | After `save()`              | `created`        |
| `pre_update`    | Before `update()`/`merge()` | `update_fields`  |
| `post_update`   | After `update()`/`merge()`  | `update_fields`  |
| `pre_delete`    | Before `delete()`           |                  |
| `post_delete`   | After `delete()`            |                  |
| `around_save`   | Wraps `save()`              |                  |
| `around_update` | Wraps `update()`/`merge()`  | `update_fields`  |
| `around_delete` | Wraps `delete()`            |                  |

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

This ORM is tested and compatible with:

| SurrealDB Version | SDK Version | Status        |
| ----------------- | ----------- | ------------- |
| 2.6.0             | 1.0.8       | ✅ Tested     |
| 2.5.x             | 1.0.8       | ✅ Compatible |

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

| Version | Theme                         | Status      |
| ------- | ----------------------------- | ----------- |
| v0.2.x  | Core ORM (CRUD, QuerySet)     | ✅ Released |
| v0.3.0  | Aggregations & Utilities      | ✅ Released |
| v0.4.0  | Model Signals                 | ✅ Released |
| v0.5.0  | Bulk Operations & Q Objects   | 📋 Next    |
| v0.6.0  | Relations & Graph             | 📋 Planned |
| v0.7.0  | Transactions ORM              | 📋 Planned |
| v0.8.0  | SurrealFunc & Computed Fields | 📋 Planned |
| v0.9.0  | Field Aliases & DX            | 📋 Planned |
| v1.0.0  | Production Ready              | 📋 Planned |

### v0.5.0 Preview

- **Q Objects** — Complex queries with `Q(a=1) | Q(b=2)`, `~Q(status="banned")`
- **Parameterized filters** — All filter values as `$_fN` variables (SQL injection prevention)
- **Bulk operations** — `bulk_create()`, `bulk_update()`, `bulk_delete()`
- **New lookups** — `not_in`, `not_contains`, `containsall`, `containsany`
- **`-field` ordering** — Shorthand for `order_by("-created_at")`

See [docs/ROADMAP.md](docs/ROADMAP.md) for full details.

---

## SurrealDB-ORM-lite vs SurrealDB-ORM

This project prioritizes **stability and compatibility** with the official SurrealDB Python SDK. The full [SurrealDB-ORM](https://github.com/EulogySnowfall/SurrealDB-ORM/) uses a custom SDK for advanced features.

| Feature                  | ORM-lite (official SDK) | ORM (custom SDK) |
| ------------------------ | ----------------------- | ---------------- |
| CRUD & QuerySet          | ✅                      | ✅               |
| Aggregations & GROUP BY  | ✅                      | ✅               |
| Model Signals            | ✅                      | ✅               |
| Bulk Operations          | v0.5.0                  | ✅               |
| Q Objects (OR/AND/NOT)   | v0.5.0                  | ✅               |
| Relations & Graph        | v0.6.0                  | ✅               |
| Transactions             | v0.7.0                  | ✅               |
| Live Models / CDC        | -                       | ✅               |
| Vector / Full-Text Search| -                       | ✅               |
| Migrations & CLI         | -                       | ✅               |
| JWT Authentication       | -                       | ✅               |
| Schema Introspection     | -                       | ✅               |
| Connection Pool          | -                       | ✅               |
| CBOR Protocol            | -                       | ✅               |

**Choose ORM-lite** if you want the official SDK, minimal dependencies, and core ORM features.

**Choose ORM** if you need live queries, migrations, authentication, vector search, or advanced features.

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
