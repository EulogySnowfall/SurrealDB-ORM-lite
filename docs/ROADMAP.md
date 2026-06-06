# SurrealDB-ORM-lite Roadmap

> Feature implementation roadmap inspired by [SurrealDB-ORM](https://github.com/EulogySnowfall/SurrealDB-ORM)
>
> **Main constraint**: All features must be compatible with the official SDK `surrealdb[pydantic]>=2.0.0,<3.0.0` (SurrealDB 3.x protocol)

---

## Overview

| Version | Theme                              | Status      |
| ------- | ---------------------------------- | ----------- |
| v0.2.x  | Core ORM (CRUD, QuerySet, Filters) | ✅ Done     |
| v0.3.0  | Aggregations & Utilities           | ✅ Done     |
| v0.4.0  | Model Signals                      | ✅ Done     |
| v0.5.0  | Bulk Operations & Q Objects        | ✅ Done     |
| v0.6.0  | Relations & Graph                  | ✅ Done     |
| v0.7.0  | SDK 2.0 / SurrealDB 3.x migration  | ✅ Done     |
| v0.8.0  | Transactions ORM                   | 📋 Planned  |
| v0.9.0  | SurrealFunc & Computed Fields      | 📋 Planned  |
| v0.10.0 | FETCH, Field Aliases & DX          | 📋 Planned  |
| v0.11.0 | Beta Phase                         | 📋 Planned  |

---

## SurrealDB-ORM vs SurrealDB-ORM-lite Comparison

### Features implementable in Lite (official SDK)

| Feature                    | ORM (full) | ORM-lite  | Target version |
| -------------------------- | ---------- | --------- | -------------- |
| CRUD (save, update, merge) | ✅ v0.2+   | ✅ v0.2.0 | -              |
| QuerySet & Filters         | ✅ v0.2+   | ✅ v0.2.0 | -              |
| Aggregations & GROUP BY    | ✅ v0.3+   | ✅ v0.3.0 | -              |
| raw_query()                | ✅ v0.5.4  | ✅ v0.3.0 | -              |
| Model Signals (pre/post)   | ✅ v0.5.7  | ✅ v0.4.0 | -              |
| Around Signals             | ✅ v0.5.8  | ✅ v0.4.0 | -              |
| Bulk Operations            | ✅ v0.4+   | ✅ v0.5.0 | -              |
| Q Objects (OR/AND/NOT)     | ✅ v0.6.0  | ✅ v0.5.0 | -              |
| Additional lookups         | ✅ v0.5.9  | ✅ v0.5.0 | -              |
| Parameterized filters      | ✅ v0.6.0  | ✅ v0.5.0 | -              |
| `-field` ordering (DESC)   | ✅ v0.6.0  | ✅ v0.5.0 | -              |
| Relations & Graph (relate) | ✅ v0.4.0  | ✅ v0.6.0 | -              |
| get_related() / traverse() | ✅ v0.4.0  | ✅ v0.6.0 | -              |
| FETCH clause               | ✅ v0.7.0  | ✅ v0.6.0 | -              |
| remove_all_relations()     | ✅ v0.6.0  | ✅ v0.6.0 | -              |
| Transactions ORM (tx=)     | ✅ v0.6+   | ❌        | v0.8.0         |
| SurrealFunc (time::now())  | ✅ v0.6.0  | ❌        | v0.9.0         |
| Computed Fields            | ✅ v0.8.0  | ❌        | v0.9.0         |
| server_values on save()    | ✅ v0.7.0  | ❌        | v0.9.0         |
| Field Aliases              | ✅ v0.5.5  | ❌        | v0.10.0        |
| call_function()            | ✅ v0.7.0  | ❌        | v0.10.0        |
| Retry, Logging, Metrics    | ✅ v0.7+   | ❌        | v0.11.0        |

### Features exclusive to SurrealDB-ORM (custom SDK required)

These features **cannot** be implemented with the current official SDK and will remain exclusive to the full ORM:

| Feature                      | ORM (full) | Reason                                    |
| ---------------------------- | ---------- | ----------------------------------------- |
| Custom SDK (`surreal_sdk`)   | ✅         | Core architecture                         |
| CBOR Protocol                | ✅ v0.5.5  | Official SDK handles internally           |
| Connection Pool              | ✅ v0.5+   | Not available in official SDK             |
| Live Models (ORM-level)      | ✅ v0.9.0  | Requires advanced WebSocket management    |
| Auto-Resubscribe             | ✅ v0.9.0  | Custom WebSocket reconnection handling    |
| Change Feeds (CDC)           | ✅ v0.9.0  | Requires polling + cursor tracking        |
| Schema Introspection         | ✅ v0.10.0 | Full inspectdb/schemadiff system          |
| Multi-Database Support       | ✅ v0.10.0 | Named connection registry                 |
| Migrations (makemigrations)  | ✅ v0.10.0 | Full Django-style migration system        |
| CLI (`surreal-orm` commands) | ✅ v0.10.0 | Shell, migrate, inspectdb                 |
| Subqueries                   | ✅ v0.11.0 | Nested QuerySets                          |
| Query Cache (TTL)            | ✅ v0.11.0 | Cache with automatic invalidation         |
| Vector Search (KNN/HNSW)     | ✅ v0.12.0 | VectorField + similar_to()                |
| Full-Text Search (BM25)      | ✅ v0.12.0 | search() + SearchScore/Highlight          |
| Hybrid Search (RRF)          | ✅ v0.12.0 | Combined vector + FTS                     |
| DEFINE EVENT                 | ✅ v0.13.0 | Server-side triggers in migrations        |
| Geospatial Fields            | ✅ v0.13.0 | PointField, PolygonField, nearby()        |
| Materialized Views           | ✅ v0.13.0 | DEFINE TABLE ... AS SELECT                |
| TYPE RELATION                | ✅ v0.13.0 | Graph constraints in migrations           |
| JWT Authentication           | ✅ v0.8.0  | AuthenticatedUserMixin, signup/signin     |
| Test Fixtures & Factories    | ✅ v0.14.0 | SurrealFixture, ModelFactory              |
| QueryLogger (debug)          | ✅ v0.14.0 | ORM query profiling                       |
| Atomic Array Operations      | ✅ v0.5.9  | atomic_append/remove/set_add              |
| Retry on Conflict            | ✅ v0.5.9  | retry_on_conflict() decorator             |

---

## Completed versions

### Version 0.2.x - Core ORM ✅

- Django-style ORM with `BaseSurrealModel`
- `QuerySet` with fluent builder
- CRUD: `save()`, `update()`, `merge()`, `delete()`, `refresh()`
- Filter lookups: `exact`, `gt`, `gte`, `lt`, `lte`, `in`, `contains`, `icontains`, `startswith`, `istartswith`, `endswith`, `iendswith`, `like`, `ilike`, `match`, `regex`, `iregex`, `isnull`
- `SurrealDBConnectionManager` (HTTP + WebSocket)
- Custom primary keys via `SurrealConfigDict`
- Pydantic 2.x validation
- Coverage 97%+

### Version 0.3.0 - Aggregations & Utilities ✅

- Aggregation classes: `Count`, `Sum`, `Avg`, `Min`, `Max`
- QuerySet methods: `count()`, `sum()`, `avg()`, `min()`, `max()`
- GROUP BY: `values()` + `annotate()`
- `exists()` to check record existence
- `raw_query()` class method for arbitrary SurrealQL
- Coverage 94%+

### Version 0.4.0 - Model Signals ✅

- `Signal` class: `pre_save`, `post_save`, `pre_update`, `post_update`, `pre_delete`, `post_delete`
- `AroundSignal` class: `around_save`, `around_update`, `around_delete`
- `connect()` / `disconnect()` / `clear()` / `has_handlers()`
- `created` flag on `post_save`
- `update_fields` on `pre_update` / `post_update`

---

## Version 0.5.0 - Bulk Operations & Q Objects ✅

**Goal**: Bulk operations and complex queries

### Features

#### 1. Q Objects for complex queries (Priority: High)

Support for OR, AND, NOT operators to combine filters flexibly.

```python
from surreal_orm_lite import Q

# OR query
users = await User.objects().filter(
    Q(name__contains="alice") | Q(email__contains="alice"),
).exec()

# NOT query
users = await User.objects().filter(
    ~Q(status="banned"),
    role="admin",
).exec()

# AND combined with OR
users = await User.objects().filter(
    Q(age__gte=18) & Q(age__lte=65),
    Q(role="admin") | Q(role="moderator"),
).exec()
```

**Technical implementation**:

```python
class Q:
    """Composable query expression."""
    AND = "AND"
    OR = "OR"

    def __init__(self, **kwargs):
        self.filters = kwargs
        self.children = []
        self.connector = self.AND
        self.negated = False

    def __or__(self, other): ...
    def __and__(self, other): ...
    def __invert__(self): ...

    def to_sql(self, table_name: str) -> tuple[str, dict]:
        """Generate WHERE clause with parameterized variables."""
```

**Files created/modified**:

- [x] `src/surreal_orm_lite/q.py` - Q class
- [x] `src/surreal_orm_lite/query_set.py` - Q support in filter()
- [x] `src/surreal_orm_lite/__init__.py` - Export Q
- [x] `tests/test_v050.py` - Tests

#### 2. Parameterized filters - Security (Priority: High)

All filter values are now parameterized variables (`$_fN`) instead of being
interpolated in the query string. Prevents SQL injection.

```python
# Before (direct interpolation - injection risk)
# SELECT * FROM User WHERE name = 'Alice'

# After (parameterized variables)
# SELECT * FROM User WHERE name = $_f0  {_f0: "Alice"}
```

**Files modified**:

- [x] `src/surreal_orm_lite/query_set.py` - Refactored \_build_where() to use variables
- [x] `src/surreal_orm_lite/utils.py` - Shared build_filter_condition()
- [x] `tests/test_v050.py` - Parameterized filter tests

#### 3. bulk_create() (Priority: High)

```python
users = [
    User(name="Alice", email="alice@example.com"),
    User(name="Bob", email="bob@example.com"),
    User(name="Charlie", email="charlie@example.com"),
]

# Bulk creation (single INSERT query)
created_users = await User.objects().bulk_create(users)
```

**Technical implementation**:

```sql
-- Uses INSERT INTO (supported by SDK via query())
INSERT INTO User [
    { name: "Alice", email: "alice@example.com" },
    { name: "Bob", email: "bob@example.com" },
    { name: "Charlie", email: "charlie@example.com" }
];
```

**Files to modify**:

- [x] `src/surreal_orm_lite/query_set.py` - bulk_create() method
- [x] `tests/test_v050.py` - Tests

#### 4. bulk_update() (Priority: Medium)

```python
# Update all filtered records
count = await User.objects().filter(status="pending").bulk_update(status="active")

# With multiple fields
count = await User.objects().filter(role="guest").bulk_update(
    role="member",
    updated_at=datetime.now()
)
```

**Technical implementation**:

```sql
-- Generates UPDATE with WHERE
UPDATE User SET status = $_v0 WHERE status = $_f0;
```

**Files to modify**:

- [x] `src/surreal_orm_lite/query_set.py` - bulk_update() method
- [x] `tests/test_v050.py` - Tests

#### 5. bulk_delete() (Priority: Medium)

```python
# Delete all filtered records
deleted_count = await User.objects().filter(status="inactive").bulk_delete()
```

**Technical implementation**:

```sql
-- Generates DELETE with WHERE
DELETE User WHERE status = $_f0;
```

**Files to modify**:

- [x] `src/surreal_orm_lite/query_set.py` - bulk_delete() method
- [x] `tests/test_v050.py` - Tests

#### 6. Additional lookups (Priority: Medium)

New filter operators to complete coverage.

```python
# NOT IN
users = await User.objects().filter(status__not_in=["banned", "deleted"]).exec()

# CONTAINSNOT
events = await Event.objects().filter(tags__not_contains="spam").exec()

# CONTAINSALL
posts = await Post.objects().filter(tags__containsall=["python", "surreal"]).exec()

# CONTAINSANY
posts = await Post.objects().filter(tags__containsany=["python", "rust"]).exec()
```

**Files to modify**:

- [x] `src/surreal_orm_lite/constants.py` - Added operators
- [x] `tests/test_v050.py` - Tests for new lookups

#### 7. `-field` ordering shorthand (Priority: Low)

```python
# Shorthand for DESC ordering
users = await User.objects().order_by("-created_at").exec()

# Equivalent to
users = await User.objects().order_by("created_at", OrderBy.DESC).exec()

# Multiple columns
users = await User.objects().order_by("-age", "name").exec()
```

**Files to modify**:

- [x] `src/surreal_orm_lite/query_set.py` - Improved order_by()
- [x] `tests/test_v050.py` - Tests

### Completion criteria v0.5.0

- [x] Q Objects working with OR/AND/NOT
- [x] Parameterized filters on all QuerySets
- [x] Bulk operations (create, update, delete) working
- [x] New lookups (not_in, not_contains, containsall, containsany)
- [x] `-field` ordering shorthand
- [x] Coverage >= 70% (94.28%)

---

## Version 0.6.0 - Relations & Graph

**Goal**: SurrealDB relations support and graph traversal

### Features

#### 1. relate() and remove_relation() (Priority: High)

```python
# Create a relation
await user.relate("follows", other_user)
await post.relate("authored_by", user)

# With data on the relation
await user.relate("purchased", product, data={"quantity": 2, "price": 29.99})

# Remove a relation
await user.remove_relation("follows", other_user)
await user.remove_relation("follows", "users:other_id")  # By ID

# Remove all relations of a type
await user.remove_all_relations("follows", direction="out")
```

**Technical implementation**:

```sql
RELATE users:alice->follows->users:bob;
RELATE users:alice->purchased->products:widget SET quantity = 2, price = 29.99;
DELETE follows WHERE in = users:alice AND out = users:bob;
DELETE follows WHERE in = users:alice;
```

#### 2. get_related() (Priority: High)

```python
# Get followed users
following = await user.get_related("follows", direction="out", model_class=User)

# Get followers
followers = await user.get_related("follows", direction="in", model_class=User)
```

#### 3. FETCH clause (Priority: High)

```python
# Resolve record links inline (prevents N+1)
posts = await Post.objects().fetch("author", "tags").exec()
# Generates: SELECT * FROM posts FETCH author, tags;
```

#### 4. Basic graph traversal (Priority: Medium)

```python
friends_of_friends = await user.traverse("->follows->User->follows->User")
```

**Files created/modified**:

- [x] `src/surreal_orm_lite/model_base.py` - relate(), remove_relation(), get_related(), remove_all_relations(), traverse()
- [x] `src/surreal_orm_lite/query_set.py` - fetch()
- [x] `src/surreal_orm_lite/utils.py` - validate_edge_name(), validate_graph_path()
- [x] `tests/test_relations.py` - Unit + E2E tests

### Completion criteria v0.6.0

- [x] Relation CRUD working
- [x] FETCH clause
- [x] Basic graph traversal
- [x] Tests with linked models
- [x] Coverage >= 70% (92.87%)

---

## Version 0.8.0 - Transactions ORM

**Goal**: ORM-level transaction support

### Features

#### 1. Transaction Context Manager (Priority: High)

```python
from surreal_orm_lite import SurrealDBConnectionManager

async with SurrealDBConnectionManager.transaction() as tx:
    user = User(name="Alice", email="alice@example.com")
    await user.save(tx=tx)

    order = Order(user_id=user.id, total=100)
    await order.save(tx=tx)
    # Auto-commit on success
    # Auto-rollback on exception
```

#### 2. tx= parameter on all operations (Priority: High)

```python
async with SurrealDBConnectionManager.transaction() as tx:
    await user.save(tx=tx)
    await user.merge(age=30, tx=tx)
    await user.delete(tx=tx)
    users = await User.objects(tx=tx).filter(status="active").exec()
```

**Note**: Implementation via `BEGIN TRANSACTION` / `COMMIT` / `CANCEL` in SurrealQL, compatible with the official SDK via `query()`.

**Files to create/modify**:

- [ ] `src/surreal_orm_lite/transaction.py` - Transaction context manager
- [ ] `src/surreal_orm_lite/model_base.py` - tx= parameter on CRUD
- [ ] `src/surreal_orm_lite/query_set.py` - tx= parameter on QuerySet
- [ ] `tests/test_transactions.py` - Atomicity tests

---

## Version 0.9.0 - SurrealFunc & Computed Fields

**Goal**: Server-side functions and computed fields

### Features

#### 1. SurrealFunc (Priority: High)

```python
from surreal_orm_lite import SurrealFunc

# Use SurrealDB functions in save/merge
await player.save(server_values={"joined_at": SurrealFunc("time::now()")})
await player.merge(last_ping=SurrealFunc("time::now()"))

# With extra variables
await user.save(
    server_values={"password_hash": SurrealFunc("crypto::argon2::generate($password)")},
    extra_vars={"password": raw_password},
)
```

#### 2. Computed Fields (Priority: Medium)

```python
from surreal_orm_lite import Computed

class User(BaseSurrealModel):
    first_name: str
    last_name: str
    full_name: Computed[str] = Computed("string::concat(first_name, ' ', last_name)")

class Order(BaseSurrealModel):
    items: list[dict]
    subtotal: Computed[float] = Computed("math::sum(items.*.price * items.*.qty)")
```

#### 3. call_function() (Priority: Medium)

```python
# Call custom SurrealDB functions
result = await SurrealDBConnectionManager.call_function(
    "acquire_game_lock", params={"table_id": tid, "pod_id": pid},
)
```

**Files to create/modify**:

- [ ] `src/surreal_orm_lite/functions.py` - SurrealFunc, Computed, call_function()
- [ ] `src/surreal_orm_lite/model_base.py` - server_values, extra_vars
- [ ] `tests/test_functions.py` - Tests

---

## Version 0.10.0 - FETCH, Field Aliases & DX

**Goal**: Developer experience improvements

### Features

#### 1. Field Aliases (Priority: Medium)

```python
from pydantic import Field

class User(BaseSurrealModel):
    password: str = Field(alias="password_hash")
```

#### 2. server_fields config (Priority: Medium)

```python
class User(BaseSurrealModel):
    model_config = SurrealConfigDict(
        server_fields=["created_at", "updated_at"],  # Excluded from save()
    )
```

#### 3. merge(refresh=False) (Priority: Low)

```python
# Skip the refresh SELECT for fire-and-forget updates
await user.merge(last_seen=SurrealFunc("time::now()"), refresh=False)
```

---

## Version 1.0.0 - Production Ready

**Goal**: Stable production-ready version

### Features

- [ ] Robust error handling
- [ ] Automatic retry on disconnect
- [ ] Configurable logging
- [ ] Performance metrics
- [ ] Complete documentation (docstrings, README, migration guide)
- [ ] Performance benchmarks

### Completion criteria v1.0.0

- [ ] Coverage >= 80%
- [ ] All e2e tests passing
- [ ] Complete documentation
- [ ] No breaking changes since v0.9.0
- [ ] Documented performance benchmarks

---

## Features exclusive to SurrealDB-ORM (out of scope)

These features will **not** be implemented in ORM-lite and require the [full ORM](https://github.com/EulogySnowfall/SurrealDB-ORM):

| Feature                    | Reason                                           |
| -------------------------- | ------------------------------------------------ |
| Custom SDK (`surreal_sdk`) | Core architecture of the full ORM                |
| CBOR Protocol              | Official SDK handles protocol internally         |
| Connection Pool            | Not supported by the official SDK                |
| Live Models (ORM-level)    | Requires advanced WebSocket management           |
| Auto-Resubscribe           | Custom WebSocket reconnection handling           |
| Change Feeds (CDC)         | Complex polling + cursor tracking                |
| Schema Introspection       | Full inspectdb/schemadiff system                 |
| Multi-Database Support     | Named connection registry + contextvars          |
| Migration system           | Django-style makemigrations/migrate/rollback     |
| CLI commands               | Interactive shell, admin commands                |
| Subqueries                 | Nested QuerySets in filters                      |
| Query Cache (TTL)          | Cache with automatic invalidation                |
| Vector Search (KNN/HNSW)   | VectorField + similar_to() + indexes             |
| Full-Text Search (BM25)    | search() + SearchScore/Highlight                 |
| Hybrid Search (RRF)        | Combined vector + FTS                            |
| DEFINE EVENT               | Server-side triggers in migrations               |
| Geospatial Fields          | PointField, PolygonField, nearby()               |
| Materialized Views         | DEFINE TABLE ... AS SELECT (read-only models)    |
| TYPE RELATION enforcement  | Graph constraints in migrations                  |
| JWT Authentication         | AuthenticatedUserMixin, signup/signin            |
| Test Fixtures & Factories  | SurrealFixture, ModelFactory, QueryLogger        |
| Atomic Array Operations    | atomic_append/remove/set_add (concurrent safety) |
| Retry on Conflict          | retry_on_conflict() decorator                    |

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
