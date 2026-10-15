# Surreal ORM Lite

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![SurrealDB](https://img.shields.io/badge/SurrealDB-2.7.0%20%7C%203.3.4-purple)
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

| Dependency   | Version                                                      |
| ------------ | ------------------------------------------------------------ |
| Python       | 3.11+ (tested on 3.11 to 3.15)                               |
| SurrealDB    | 2.6.x / 2.7.x or 3.2.x / 3.3.x, or the SDK's embedded engine |
| Official SDK | surrealdb[pydantic]>=2.0.0,<3.0.0                            |
| Pydantic     | >=2.13.4                                                     |

> **Note**: As of v0.7.0, Surreal ORM Lite targets the SurrealDB Python SDK 2.x (`surrealdb[pydantic]>=2.0.0,<3.0.0`), which supports the SurrealDB 3.x protocol. It is tested against SurrealDB **v2.7.0** and **v3.3.4**; up to v0.19.0 the pins were v2.6.5 and v3.2.4. The 2.x line behaves identically on 2.7.0, and 3.3.0 differs from 3.2.4 in one server behaviour, listed in the behaviour table. SurrealDB 3.1.x is no longer a supported line as of v0.14.0 — the suite is still run against 3.1.5 as a backward-compatibility check, but a regression there does not block a release.

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

### Supported Filter Lookups

- `exact` (default)
- `gt`, `gte`, `lt`, `lte`
- `in`, `not_in` (a list, or a `Range` bound as one value: see [section 24](#24-rich-field-types))
- `contains`, `not_contains` (`contains` on a range column tests membership)
- `containsall`, `containsany`
- `startswith`, `endswith`
- `like`, `ilike`
- `match`, `regex`
- `isnull`
- `inside`, `outside`, `intersects` (geometry values: see [section 25](#25-geospatial-fields))

Filter values on rich fields must be typed: `filter(trial__gte=timedelta(days=7))`, not
`"7d"`. Strings are not converted (see [section 24](#24-rich-field-types)).

#### Filtering on the record id

The `id` column holds a native `RecordID`, not a string, so the ORM coerces an `id` lookup
before binding it. All of these address the same record:

```python
await User.objects().filter(id="alice").exec()                        # bare identifier
await User.objects().filter(id="User:alice").exec()                   # full table:id form
await User.objects().filter(id=RecordID("User", "alice")).exec()      # explicit
await User.objects().get("alice")                                     # same rule
await User.objects().filter(id__in=["alice", "bob"]).exec()
```

The value's **Python type** decides which record is addressed, exactly as it does when a
record is saved: `filter(id=5)` is the _integer_ record id `User:5`, `filter(id="5")` is the
_string_ one. They are two different records — a model declared `id: int` needs the former.

A text or collection lookup (`contains`, `startswith`, `regex`, `like`, `containsall`, …) on
`id` raises `ValueError` rather than silently matching nothing, since a record id is not a
string:

```python
await User.objects().filter(id__startswith="al").exec()
# ValueError: 'startswith' cannot be applied to the record id column 'id' …
```

A model that aliases its identity through `primary_key` keeps that alias as an ordinary
column, so `filter(code="abc")` is a normal string comparison and is unaffected.

#### Filter values that start with `$`

A filter value beginning with `$` is read as a reference to a **query variable**, not as data.
Use `Var(...)` to say so explicitly, and `$$` to escape a value that genuinely starts with a
dollar sign:

```python
from surreal_orm_lite import Var

await User.objects().filter(age__gte=Var("min_age")).exec()   # … WHERE age >= $min_age
await User.objects().filter(name="$$admin").exec()            # matches the literal "$admin"
await User.objects().filter(name="$admin").exec()             # deprecated: reads as $admin
```

The bare `"$admin"` form still works for backward compatibility but emits a `DeprecationWarning`:
it silently matches nothing when the value is really meant as data, which is a trap for
user-supplied strings. Prefer `Var(...)` in new code.

#### Model instances as filter values

```python
from surrealdb import RecordID


class Order(BaseSurrealModel):
    id: str | None = None
    customer: RecordID          # a record link
    note: str = ""


alice = await Customer.objects().get("alice")
bob = await Customer.objects().get("bob")
order, created = await Order.objects().get_or_create(customer=alice, note="first")
orders = await Order.objects().filter(customer=alice).exec()        # customer = Customer:alice
orders = await Order.objects().filter(customer__in=[alice, bob]).exec()
```

Since v0.22.0 a saved model instance is accepted wherever a filter takes a value (keyword
lookups, `Q` objects, live filters, each item of `in`, `not_in`, `containsany` and
`containsall`) and is replaced by its record id. `get_or_create()` and `update_or_create()` store
that record id when they create. An unsaved instance raises `ValueError`, and so does a single
instance given to a list lookup (`customer__in=alice`: wrap it in a list). A `"table:id"` string
is **not** converted on an ordinary column, which may legitimately hold such text, and a string
never equals a record id: pass the instance or a `RecordID`. Edge endpoints are the exception
(see [section 23](#23-typed-edges-basesurrealedge)).

#### Fields ending in an underscore

```python
class Item(BaseSurrealModel):
    id: str | None = None
    type_: int = 0             # trailing underscore: `type` is a builtin


heavy = await Item.objects().filter(type___gt=1).exec()   # the gt lookup on type_
```

A lookup on a field whose name ends in `_` takes three underscores: the field's own, then the
`__` separator. `type___gt` is `gt` on `type_`, and `in___in` is `in` on an edge's `in_`. Before
v0.22.0 such a key raised "Unsupported lookup".

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

# A target can also be a "table:id" string. Its id is read as a *string* record id — the
# same rule the ORM applies to a model's own id — so these two name the same record:
await user.relate("follows", "User:1")        # -> User:`1`, i.e. User(id="1")
# Pass a RecordID to target an *integer* record id (what a model with `id: int` stores):
from surrealdb import RecordID
await user.relate("follows", RecordID("User", 1))   # -> User:1

# Remove a specific relation
await user.remove_relation("follows", other_user)

# Remove all outgoing relations of a type
await user.remove_all_relations("follows", direction="out")

# Graph traversal
friends_of_friends = await user.traverse("->follows->User->follows->User")
```

Since v0.22.0 the helpers take the full SurrealDB-ORM's parameters, in its order, by position or
by keyword, and every one of them accepts an edge model class
([section 23](#23-typed-edges-basesurrealedge)) as well as a table name:

```python
# Edge fields as keyword arguments (the full ORM's spelling); they merge with data=
await user.relate("follows", other_user, since="2025-01-01")
await user.relate("purchased", product, data={"quantity": 2}, price=29.99)

# reverse=True: target -> edge -> self
await table.relate("created", creator, reverse=True)       # creator -> created -> table
await table.relate("created", creator, True)               # same call, the full ORM's positional order
await table.remove_relation("created", creator, reverse=True)

# An edge model class (section 23) wherever a table name goes
following = await user.get_related(Follows, model_class=User)

# Both directions in one query: the out-neighbours, then the in-neighbours
everyone = await user.get_related("follows", direction="both", model_class=User)

# Several edge tables in one statement
await user.remove_all_relations(["follows", "likes", "blocks"], direction="both")

# A bare record id: that id in any table linked through the edge table
await table.remove_relation("has_player", "abc123")

# tx= on the three write helpers
async with SurrealDBConnectionManager.transaction() as tx:
    await user.relate("follows", other_user, tx=tx)
    await user.remove_relation(Follows, "User:carol", tx=tx)
    await user.remove_all_relations(["likes", "blocks"], direction="both", tx=tx)
```

- **Direction.** `reverse=True` relates or unlinks `target -> edge -> self`; the default is
  `self -> edge -> target`.
- **Edge fields.** `data=` and keyword arguments merge; a field given both ways is a `TypeError`,
  raised before anything is sent. A field named like a parameter (`edge`, `target`, `reverse`,
  `tx`, `data` or `self`) cannot be a keyword argument: pass it in `data=`
  (`data={"reverse": True}`).
- **Bare record id.** A string without `:` is a bare record id. It is compared as a string, so it
  matches that id in **any** table linked through the edge table (`Player:abc123` and
  `Bot:abc123` alike). To pin the table, or to reach an integer record id (`"7"` never matches
  `Player:7`), pass the instance or a `RecordID`. Only `remove_relation()` takes one; an empty or
  blank string raises `ValueError`.
- **Removals** are graph-path `DELETE` statements: they delete exactly the edges `get_related()`
  and `traverse()` see (a plain record that merely carries `in`/`out` fields is not an edge and
  is left alone), a self-loop once, and an edge table that was never created is a no-op on both
  server lines, in a transaction too. `remove_all_relations()` takes one edge or a list; an
  empty list raises `ValueError`.
- **`direction="both"`** returns the out-neighbours, then the in-neighbours: a neighbour linked
  by k edges appears k times, the record itself only through a self-loop (once per direction).
  The order inside each group is unspecified; it differs between server versions.
- **`tx=`** follows the transaction's strategy (section 11). On an interactive transaction
  (WebSocket + SurrealDB 3.x) the statement runs at once: the change is visible to
  `objects(tx=)` reads and a rollback undoes it. On a buffered one (2.x, or HTTP) it is queued
  and applied at commit, and the string `relate()` returns `[]`. `get_related()` has no `tx=`,
  as in the full ORM.
- The source and target are bound as record ids (only edge names, field names and the
  validated `traverse()` path are written into the statement), so a node whose id is not an
  identifier, such as `User(id="joe@x.com")`, works with every graph helper: `relate()` with an
  edge class or a table name, `get_related()`, the `remove_*` helpers and `traverse()`.

Coming from the full SurrealDB-ORM? The few remaining differences are listed in
[Migrating from SurrealDB-ORM (graph API)](#migrating-from-surrealdb-orm-graph-api).

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

### 13. Patch & atomic field/array operations

Mutate a record granularly — server-side — without reading and rewriting the whole document.

> **No signals.** `patch()` and the atomic helpers are low-level primitives and emit **no**
> `pre_*`/`post_*`/`around_*` lifecycle signals. If you rely on signals (audit, cache
> invalidation, …), use `merge()` / `save()` instead.

```python
# JSON Patch (RFC 6902) on a single record (native SDK patch()). Requires an explicit id.
await user.patch([
    {"op": "replace", "path": "/age", "value": 26},
    {"op": "add", "path": "/tags/-", "value": "premium"},
    {"op": "remove", "path": "/settings/notifications"},
])

# Ergonomic atomic helpers — each is one atomic UPDATE … SET, safe under concurrency:
await post.atomic_append("tags", "python")     # array::append — duplicates allowed
await post.atomic_set_add("editors", "alice")  # array::add     — added only if absent (set)
await post.atomic_remove("tags", "spam")       # array::complement — removes ALL "spam"
await counter.atomic_increment("views")        # += 1 (default); pass a negative to decrement
await counter.atomic_increment("score", 5)     # += 5

# List-valued variants — apply many in ONE round-trip instead of N:
await post.atomic_append_many("tags", ["python", "orm"])   # array::concat — all, dups allowed
await post.atomic_set_add_many("editors", ["alice", "bob"])  # array::add — only those absent
await post.atomic_remove_many("tags", ["spam", "draft"])     # array::complement — all matches

# Patch a filtered set (or the whole table if unfiltered); returns the affected count.
n = await User.objects().filter(status="trial").patch(
    [{"op": "replace", "path": "/plan", "value": "free"}]
)

# All of the above accept tx= and participate in a transaction:
async with SurrealDBConnectionManager.transaction() as tx:
    await counter.atomic_increment("views", tx=tx)
    await user.patch([{"op": "replace", "path": "/age", "value": 27}], tx=tx)

# atomic_increment accepts a Decimal for exact arithmetic (e.g. money):
from decimal import Decimal

await account.atomic_increment("balance", Decimal("2.25"))

# Optimistic concurrency with a JSON Patch `test` op: if the test fails, the WHOLE patch is
# aborted server-side (no op applies) and a ServerError is raised — RFC 6902 semantics.
await order.patch([
    {"op": "test", "path": "/version", "value": 7},  # only proceed if version is still 7
    {"op": "replace", "path": "/status", "value": "shipped"},
    {"op": "replace", "path": "/version", "value": 8},
])
```

These atomic ops behave **identically on SurrealDB 2.6.x and 3.x by design**: they use the
version-portable functions `array::append` / `array::add` / `array::complement` (and numeric
`+=`) rather than the bare `+=` / `-=` array operators, whose semantics differ between server
lines. `patch()` and the atomic helpers emit **no signals** (use `merge()` / `save()` if you
need lifecycle hooks). On a non-transactional or interactive (3.x) call the instance is synced
with the server's returned row; in a buffered 2.6.x transaction the result is unknown until
commit, so `refresh()` the instance if you need it (same caveat as `merge(tx=)`).

A failed JSON Patch `test` op aborts the entire patch and raises the SDK's `ServerError`
(message: `Given test operation failed…`) — none of the other ops in the list are applied.
This gives you compare-and-set / optimistic-concurrency without a transaction.

### 14. Optimistic concurrency: `retry_on_conflict`

Under SurrealDB's optimistic concurrency, a transaction is rolled back with a **retryable**
conflict when a concurrent writer changed the same data. `retry_on_conflict` re-runs the whole
function (a fresh transaction per attempt) with exponential backoff + jitter, but **only** on a
real conflict — any other error propagates immediately.

```python
from surreal_orm_lite import retry_on_conflict, SurrealDBConnectionManager

@retry_on_conflict(max_retries=3, base_delay=0.05, max_delay=2.0, backoff_factor=2.0)
async def transfer(src_id, dst_id, amount):
    async with SurrealDBConnectionManager.transaction() as tx:
        src = await Account.objects(tx=tx).get(src_id)
        dst = await Account.objects(tx=tx).get(dst_id)
        await src.merge(tx=tx, balance=src.balance - amount)
        await dst.merge(tx=tx, balance=dst.balance + amount)

await transfer("acc:a", "acc:b", 100)  # retries automatically on a version conflict
```

A conflict is exposed as `SurrealDbConflictError` (a subclass of `SurrealDbError`) on **both**
SurrealDB lines, so you can catch it yourself or test any exception with `is_conflict_error()`:

```python
from surreal_orm_lite import SurrealDbConflictError, is_conflict_error

try:
    await transfer("acc:a", "acc:b", 100)
except SurrealDbConflictError:
    ...  # still conflicting after every retry
```

- Total attempts = `max_retries + 1`; after they are exhausted the conflict is re-raised as
  `SurrealDbConflictError`. The numeric parameters are validated at decoration time.
- Detection anchors on SurrealDB's own retryable marker ("This transaction can be retried"), so
  a non-retryable failure (e.g. a duplicate-key error) is **not** retried.
- The exception type is identical on 2.6.x and 3.x; conflicts simply arise more often on 3.x
  (optimistic MVCC) than on 2.6.x (see the behaviour table), except on change-feed tables, where
  2.x refuses concurrent writes that 3.x accepts (see [section 22](#22-change-feeds-changes)).
- The decorator wraps any coroutine, not only a `transaction()`: a single `save()`, `merge()` or
  `delete()` is retried the same way (see [section 22](#22-change-feeds-changes) for change-feed
  tables on SurrealDB 2.x). The conflict such a write raises outside a transaction is not
  converted: on 2.7.0 it is the SDK's `InternalError`, which `is_conflict_error()` recognises.

### 15. Server-side values: `SurrealFunc` + `server_values=`

Some values belong to the server, not to your process: a creation timestamp should come from the
DB clock, a password hash from the DB's own crypto. Pass them as `server_values=` on `save()` or
`merge()` and the ORM compiles a `SET` clause where the expression is evaluated **by SurrealDB**:

```python
from surreal_orm_lite import SurrealFunc

await player.save(server_values={"joined_at": SurrealFunc("time::now()")})
# CREATE $rid SET seat = $_sv_seat, joined_at = time::now();
```

The instance is synced with the row the server returns, so `player.joined_at` is a real
`datetime` right after the call — no extra `refresh()`.

**User input never goes into the expression.** Reference a bound parameter and supply it through
`extra_vars=`, which is bound like any other value:

```python
from surreal_orm_lite import SurrealFunc, SurrealCryptoFunction

await user.save(
    server_values={"password_hash": SurrealFunc.call(SurrealCryptoFunction.ARGON2_GENERATE, "$password")},
    extra_vars={"password": raw_password},   # bound — never interpolated
)
```

`merge()` takes the same two arguments and stays a **partial** update (unlisted fields are
untouched); a `server_values` entry overrides a keyword of the same name:

```python
from surreal_orm_lite import SurrealTimeFunction

await user.merge(plan="pro", server_values={"updated_at": SurrealFunc.call(SurrealTimeFunction.NOW)})
```

`SurrealFunc.call(fn, *args)` builds `fn(arg, …)` from a function name — a plain string or a
member of the shipped enums, which give you autocompletion over a **curated catalog whose every
member is tested against SurrealDB 2.7.0 and 3.3.4**:

| Enum                    | Covers                                                           |
| ----------------------- | ---------------------------------------------------------------- |
| `SurrealTimeFunction`   | `time::now`, `time::floor`, `time::unix`, `time::year`, …        |
| `SurrealMathFunction`   | `math::abs`, `math::pow`, `math::mean`, `math::fixed`, …         |
| `SurrealStringFunction` | `string::concat`, `string::slug`, `string::replace`, …           |
| `SurrealArrayFunction`  | `array::append`, `array::add`, `array::distinct`, …              |
| `SurrealCryptoFunction` | `crypto::argon2::generate` / `::compare`, `crypto::bcrypt::*`, … |
| `SurrealRandFunction`   | `rand`, `rand::uuid::v7`, `rand::ulid`, `rand::enum`, …          |

Functions whose name differs between the two server lines are deliberately excluded from the
catalog (`rand::guid` is 2.6-only; `type::is::*` became `type::is_*` in 3.x) — pass those as a
plain string if you target one line. The enums are convenience, not a gate: `SurrealFunc` accepts
any expression.

> **Security**: the `SurrealFunc` expression is inserted verbatim into the query, so build it
> only from developer-controlled text. Field values and `extra_vars` are always bound parameters
> — that is the injection boundary. `SurrealFunc` rejects `;` as a guard against accidental
> statement chaining, but that is not a sanitizer.

Both calls behave **identically on SurrealDB 2.6.x and 3.x**. Inside a transaction the usual
v0.9.0 rules apply: on a buffered transaction (HTTP / 2.6.x) the computed value is unknown until
commit, so the instance keeps its previous value for that field until you `refresh()`.

---

### 16. Computed fields: `Computed[...]` → `DEFINE FIELD … VALUE`

`server_values=` computes a value for **one write**. A `Computed` field attaches the expression
to the **schema** instead, so SurrealDB recomputes it on _every_ write to the table — including
writes that never go through the ORM:

```python
from surreal_orm_lite import BaseSurrealModel, Computed, computed

class Player(BaseSurrealModel):
    id: str
    first_name: str
    last_name: str
    full_name: Computed[str] = computed("string::concat(first_name, ' ', last_name)")
    initials: Computed[str] = computed("string::uppercase(string::slice(first_name, 0, 1))")

await Player.define_computed_fields()   # DEFINE FIELD OVERWRITE full_name ON Player VALUE …

player = await Player(id="ada", first_name="Ada", last_name="Lovelace").save()
player.full_name        # "Ada Lovelace" — computed by the server, not by Python
```

`Computed[T]` is the annotation and makes the field `T | None` defaulting to `None`, so an
instance is constructible before the server has ever computed it; `computed("<expr>")` is the
default and carries the expression, which may be a plain string or a `SurrealFunc`. The two are
split — rather than one dual-use name — so that `player.full_name` resolves to `str | None`
under mypy and pyright, the same shape as SQLAlchemy's `Mapped[T] = mapped_column(...)`.

**Applying the schema.** `define_computed_fields()` is idempotent — call it at start-up:

```python
Player.computed_field_ddl()             # the statements, without touching the DB
await Player.define_computed_fields()                  # DEFINE FIELD OVERWRITE … (default)
await Player.define_computed_fields(overwrite=False)   # DEFINE FIELD IF NOT EXISTS …
```

The default `OVERWRITE` treats the model as the source of truth, so editing an expression and
redeploying takes effect. `overwrite=False` never disturbs a definition that already exists.

**The field is server-owned.** It is dropped from every write payload, and naming it in a write
raises rather than being silently discarded:

```python
await player.merge(last_name="Byron")       # ✅ full_name recomputes to "Ada Byron"
await player.merge(full_name="whatever")    # ❌ ValueError: full_name is a computed field …
```

The same guard applies to `save(server_values=)`, `patch()`, `QuerySet.patch()`,
`QuerySet.bulk_update()` and the `atomic_*` helpers. A JSON Patch is checked on its top-level
pointer segment, so `/full_name` and `/full_name/0` are both refused. This is genuinely enforced by SurrealDB, not just by the ORM — a client that bypasses
ORM-lite entirely and writes `full_name` directly still gets the expression's result.

> **Ordering caveat**: SurrealDB evaluates computed fields in **alphabetical field-name order**,
> not declaration order. A computed field that reads another must sort after it — `subtotal` →
> `total` works, but `z_sub` → `a_total` fails at write time.
>
> **Security**: like `SurrealFunc`, the expression is inlined verbatim into DDL and cannot
> reference bound parameters. Build it only from developer-controlled text, never user input.

No `TYPE` clause is emitted — SurrealDB infers an optional type on both server lines. Behaviour
is **identical on SurrealDB 2.6.x and 3.x**.

---

### 17. Stored functions: `call_function()`

Invoke a function declared server-side with `DEFINE FUNCTION fn::…`. Arguments are **bound as
query parameters**, never formatted into the statement.

```python
from surreal_orm_lite import SurrealDBConnectionManager

# Declare it once (DDL goes through query(); a define_function() helper lands in v0.31.0)
client = await SurrealDBConnectionManager.get_client()
await client.query("""
    DEFINE FUNCTION OVERWRITE fn::acquire_lock($table_id: string, $pod_id: string, $ttl: int) {
        UPSERT type::record("lock:⟨" + $table_id + "⟩") SET holder = $pod_id, ttl = $ttl;
        RETURN { acquired: true, holder: $pod_id };
    };
""", {})

# Positional — SurrealQL's own calling convention
lock = await SurrealDBConnectionManager.call_function(
    "fn::acquire_lock", ["table-1", "pod-a", 30],
)

# The fn:: prefix is optional, and nested namespaces work
total = await SurrealDBConnectionManager.call_function("billing::total", [cart_id])
```

**Named arguments.** SurrealQL function arguments are positional, so the ORM reads the
function's declared signature (`INFO FOR DB`, cached) and orders them for you — the mapping's
own order is irrelevant:

```python
lock = await SurrealDBConnectionManager.call_function(
    "fn::acquire_lock", params={"pod_id": "pod-a", "ttl": 30, "table_id": "table-1"},
)
```

Passing both `args` and `params` raises `ValueError`, as does a key that does not match the
declared parameters (the error names the ones expected).

**Typed results.** `return_type=` accepts anything Pydantic can adapt — a model, a dataclass, a
scalar, or a generic like `list[Model]`:

```python
from pydantic import BaseModel

class LockResult(BaseModel):
    acquired: bool
    holder: str

lock = await SurrealDBConnectionManager.call_function(
    "fn::acquire_lock", ["table-1", "pod-a", 30], return_type=LockResult,
)
lock.acquired  # True
```

A result that does not fit raises `SurrealDbValidationError`. Note this is Pydantic
_validation_, not a cast: an `int` asked to be a `str` is a mismatch, not a silent `str(5)`.

**Inside a transaction.** Pass `tx=` so the function runs _inside_ the transaction — without it
the call would execute outside it and silently break atomicity, which matters because stored
functions typically mutate state:

```python
async with SurrealDBConnectionManager.transaction() as tx:
    await SurrealDBConnectionManager.call_function("fn::acquire_lock", ["t1", "pod-a", 30], tx=tx)
    await Booking(id="b1", table_id="t1").save(tx=tx)
# both the function's writes and the booking commit together, or neither does
```

The **return value** depends on the transaction strategy (the v0.9.0 contract): interactive
transactions (WebSocket + SurrealDB 3.x) return the value immediately; buffered ones (SurrealDB
2.6.x or HTTP) queue the call and return `None` until commit. Combining `return_type=` with a
buffered `tx=` raises `ValueError` rather than silently returning `None`.

**From a model.** A stored function is not bound to a table, but the shortcut is convenient in
model-oriented code:

```python
lock = await GameTable.call_function("fn::acquire_lock", ["t1", "pod-a", 30])
```

Errors are normalised: a function the server does not know raises `SurrealDbNotFoundError`,
anything else raises `SurrealDbError`. A result that does not fit `return_type` raises
`SurrealDbValidationError` — inside an interactive transaction too. The one exception is a
**buffered** transaction, where the call is merely queued: a missing function cannot be detected
at call time and surfaces at commit as `SurrealDbError`.

> **Portability note** — building a record id inside a function is the one fiddly part, and the
> spelling above is the one verified on **both** DB lines. The two-argument
> `type::record($table, $id)` is 3.x-only (on 2.6.x the second argument means a _type_, not an
> id), and `type::thing` is its 2.6-only inverse. The `⟨…⟩` brackets around the id matter too:
> without them `type::record("lock:" + $id)` truncates an id containing a hyphen (`"table-1"`
> becomes `table`) on 3.x and is rejected outright on 2.6.x.

Signature caching is transparent, but two helpers are available if you redefine functions
out-of-band: `SurrealDBConnectionManager.clear_function_signature_cache()` and
`function_signature_cache_size()`. The cache is keyed by URL, namespace and database, so
`set_url()`, `set_namespace()` and `set_database()` cannot serve a signature read elsewhere;
it is cleared outright by `set_connection()` and `unset_connection()`. A signature whose
parameter _names_ changed self-heals on its own — but one redefined with the **same names in a
different order** cannot be detected, since `params=` is a mapping and carries no order to
compare against. After such a redefinition, call `clear_function_signature_cache()`, or
`params=` keeps binding in the stale order.

---

### 18. Authentication (`signin` / `signup` / `info` / `invalidate`)

Authenticate the connection as a SurrealDB **record user** (a `DEFINE ACCESS … TYPE RECORD`
method) or as a different **system user**. Declare the access method once — DDL goes through
`query()`:

```python
client = await SurrealDBConnectionManager.get_client()
await client.query("""
    DEFINE TABLE OVERWRITE app_user SCHEMALESS
      PERMISSIONS FOR select, update WHERE id = $auth.id;
""", {})
await client.query("""
    DEFINE ACCESS OVERWRITE account ON DATABASE TYPE RECORD
      SIGNUP ( CREATE app_user SET email = $email, pass = crypto::argon2::generate($pass) )
      SIGNIN ( SELECT * FROM app_user
               WHERE email = $email AND crypto::argon2::compare(pass, $pass) )
      DURATION FOR TOKEN 15m, FOR SESSION 12h;
""", {})
```

Then register and authenticate users:

```python
from surreal_orm_lite import AuthTokens, SurrealDBConnectionManager, SurrealDbAuthenticationError

tokens = await SurrealDBConnectionManager.signup(
    access="account", variables={"email": "ada@example.com", "pass": "s3cret"},
)
tokens = await SurrealDBConnectionManager.signin(
    access="account", variables={"email": "ada@example.com", "pass": "s3cret"},
)
tokens.access     # the JWT — hand it to a web client
tokens.refresh    # SurrealDB 3.x only (WITH REFRESH); always None on 2.6.x
```

`namespace`/`database` come from `set_connection()` for record access. A **system user** gets
neither unless you pass them, because a root user is defined at no level:

```python
await SurrealDBConnectionManager.signin(username="root", password="root")            # root
await SurrealDBConnectionManager.signin(username="u", password="p", namespace="ns")  # NS user
```

Read back who is signed in, optionally hydrated into a model:

```python
me = await SurrealDBConnectionManager.info()                  # dict | None
me = await SurrealDBConnectionManager.info(return_type=User)  # User | None
```

Restore an identity on a later request, and log out:

```python
await SurrealDBConnectionManager.authenticate(stored_jwt)
await SurrealDBConnectionManager.invalidate()   # back to the credentials of set_connection()
```

The identity **survives reconnects**: `get_client()` replays the stored token, so a dropped
connection or a new event loop comes back as the same user rather than silently reverting to
the configured root identity.

Every failure — wrong password, unknown access method, malformed or expired token — raises
`SurrealDbAuthenticationError` on **both** DB lines (it subclasses `SurrealDbError`). Match on
the type, never on the server's wording, which differs per line.

> ⚠️ **Auth changes the identity of the whole connection.** The manager caches one client per
> event loop and every model shares it, so an auth call affects every subsequent ORM operation
> — not just the caller's. An application serving concurrent users must not route them all
> through a single connection-manager identity.
>
> ⚠️ **`info()` returns `None` unless the table grants the record user `select` on itself.**
> The signin succeeded and `$auth` is set, but the server returns nothing. Grant something like
> `PERMISSIONS FOR select WHERE id = $auth.id`.

**Refresh tokens (SurrealDB 3.x only).** With `DEFINE ACCESS … WITH REFRESH`, renew without the
password:

```python
tokens = await SurrealDBConnectionManager.signin(access="account", refresh=stored_refresh)
persist(tokens.refresh)   # REQUIRED — see below
```

> ⚠️ **Refresh tokens rotate.** A successful exchange kills the token it spent, immediately and
> permanently. If you keep the old value and drop the new one, the user is logged out for good
> — and nothing is raised at the moment you make the mistake.

---

### 19. Model-level authentication (`AuthenticatedUserMixin`)

Where section 18 authenticates the _connection_, this authenticates a **model**: declare a user
model, let the ORM generate its `DEFINE ACCESS` statement, and get **hydrated model instances**
back instead of bare tokens.

```python
from surreal_orm_lite import AuthenticatedUserMixin, BaseSurrealModel, SurrealConfigDict

class User(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(access_name="account")

    id: str | None = None
    email: str
    password: str
    name: str = ""

# Once, at start-up — emits DEFINE TABLE … PERMISSIONS + DEFINE ACCESS …
await User.define_access()

result = await User.signup(email="ada@example.com", password="s3cret", name="Ada")
result.user     # a User instance, carrying the id the server generated
result.tokens   # AuthTokens — repr redacts, so no JWT reaches your logs

result = await User.signin(email="ada@example.com", password="s3cret")

# A later request carrying the stored JWT: token → current user
me = await User.authenticate(result.tokens.access)
```

**Each call runs on its own short-lived connection.** The process-wide client keeps the identity
`set_connection()` gave it, so a web application can authenticate concurrent users without them
trampling each other — the footgun that makes section 18's `signin()` unsuitable per-request.
Pass `bind=True` to _also_ adopt the token process-wide (handy in a script or notebook, wrong in
a concurrent server).

The model must declare either an `id` field or a `primary_key`, as every ORM model must. Prefer
`id` here so the server mints the record id during SIGNUP; configure `primary_key` when identity
_is_ a column, and the SIGNUP clause targets `type::thing(…)` to match.

#### Configuration

| Key                     | Default          | Purpose                                      |
| ----------------------- | ---------------- | -------------------------------------------- |
| `access_name`           | `<table>_access` | Name of the `DEFINE ACCESS` method           |
| `identifier_field`      | `email`          | Field a user signs in with                   |
| `password_field`        | `password`       | Field holding the password hash              |
| `auth_algorithm`        | `argon2`         | `argon2` / `bcrypt` / `pbkdf2` / `scrypt`    |
| `auth_duration_token`   | `15m`            | JWT lifetime                                 |
| `auth_duration_session` | `12h`            | Session lifetime                             |
| `with_refresh`          | `False`          | Emit `WITH REFRESH` — **SurrealDB 3.x only** |
| `auth_duration_grant`   | `30d`            | Refresh-grant lifetime                       |

#### Inspecting the DDL before applying it

`access_ddl()` is pure — it renders without touching the database, so you can print it, diff it,
or feed it to a migration:

```python
for statement in User.access_ddl():
    print(statement)
# DEFINE TABLE OVERWRITE User SCHEMALESS PERMISSIONS FOR select, update WHERE id = $auth.id;
# DEFINE ACCESS OVERWRITE account ON DATABASE TYPE RECORD SIGNUP ( ... ) SIGNIN ( ... ) ...;
```

The `DEFINE TABLE` ships **by default**, and deliberately: without `FOR select WHERE id = $auth.id`
a signin succeeds and `$auth` is set, yet the server returns no record — so no instance can be
built. `define_access(with_table=False)` opts out when you manage the table yourself.

> **The password field holds the hash.** SurrealDB returns the stored record, so
> `result.user.password` is `$argon2id$v=19$…`, never the plaintext you submitted.

#### Renewing a session (SurrealDB 3.x only)

```python
class User(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(access_name="account", with_refresh=True)
    ...

result = await User.refresh(stored_refresh_token)
persist(result.tokens.refresh)   # REQUIRED — refresh tokens rotate
```

On SurrealDB 2.6.x the `WITH REFRESH` clause does not parse at all, so `define_access()` raises a
`SurrealDbError` that says exactly that, and no refresh token ever exists.

### 20. Field aliases, `server_fields` and `merge(refresh=False)`

Three developer-experience features (v0.18.0) that let a model's Python surface differ from its
SurrealDB column surface, and let a caller skip work they do not need. All three behave
**identically on SurrealDB 2.6.x and 3.x**.

#### Field aliases

Declare the column name with plain Pydantic; the ORM honours it everywhere.

```python
from pydantic import Field
from surreal_orm_lite import BaseSurrealModel

class User(BaseSurrealModel):
    id: str
    password: str = Field(alias="password_hash")   # column is password_hash
    display: str = Field(alias="display_name")

# Build with either name — the Python one in your code, the alias when hydrating a row.
user = User(id="ada", password="secret", display="Ada")
await user.save()                                   # stores password_hash / display_name

# Query in Python names; the ORM emits the columns.
found = await User.objects().filter(password="secret").order_by("-display").exec()
await user.merge(password="rotated")
await User.objects().filter(display="Ada").bulk_update(password="rotated")
```

The translation covers every boundary the ORM owns:

- **writes** — `save`, `update`, `upsert`, `merge`, `bulk_create`, `server_values=`, and the
  `atomic_*` family (`atomic_increment`, `atomic_append`, `atomic_set_add`, `atomic_remove`
  and their `*_many` counterparts);
- **reads** — `exec()`, `refresh()`, and the row a write returns;
- **every QuerySet clause** — `select`, `filter` (including `Q` objects, nested and negated),
  `order_by`, `values`, `fetch`, `bulk_update`, the `sum`/`avg`/`min`/`max` helpers and the
  `Count`/`Sum`/`Avg`/`Min`/`Max` objects passed to `annotate()`. A grouped result comes back
  keyed by your Python names, not by columns;
- **DDL the ORM generates** — `computed_field_ddl()`, and `AuthenticatedUserMixin`'s
  `access_ddl()` (its `SIGNUP`/`SIGNIN` clauses name the column on the left and keep your
  keyword argument as the bound variable on the right);
- **signal payloads** — `update_fields` always names the model's Python attributes.

Three classmethods expose the mapping if you need it directly:

| Method                | Returns                                                   |
| --------------------- | --------------------------------------------------------- |
| `get_field_aliases()` | `{python_name: column}` for aliased fields (`{}` if none) |
| `to_db_field(name)`   | the column for `name` (unchanged when unaliased)          |
| `to_py_field(column)` | the Python attribute for `column`                         |

Only a plain, symmetric `Field(alias=…)` is treated as a column rename. A separate
`validation_alias` / `serialization_alias`, and in particular `AliasPath` / `AliasChoices`,
describe something other than a renamed column and are left entirely to Pydantic — write
payloads follow the ORM's map, not `model_dump(by_alias=True)`, so a row can never land under
a name the read paths do not look for.

The map has to be unambiguous, and the ORM checks it the first time it is used: an alias that
collides with another field's name, or two fields sharing one column, raises `ValueError`.
Either shape would make the payload's two keys collapse into one and silently drop a field.

`patch()` pointers follow the same rule. A pointer's top-level segment may name the field or
its column — `/password` and `/password_hash` both reach `password_hash`, and so does the `from`
of a `move`/`copy`. A whole-document operation (`"path": ""`) has its object value re-keyed.
Nested paths translate their root: `Q(**{"address.city": …})` and `Sum("address.n")` address
`addr.city` / `addr.n`, exactly like `filter(**{"address.city": …})`.

Result keys stay in your vocabulary too, with one rule for grouped rows: an `annotate()` alias is
your own word and is never renamed. An alias that spells a grouped field — by name or by column —
would lose one of the two values in the same row, so it raises `ValueError`.

#### `server_fields`

Mark the columns the **server** owns — filled by a `DEFINE FIELD … DEFAULT`, an event or a
trigger — so the client never volunteers them:

```python
from surreal_orm_lite import SurrealConfigDict

class Post(BaseSurrealModel):
    model_config = SurrealConfigDict(server_fields=["created_at"])
    id: str
    title: str
    created_at: datetime | None = None

post = await Post(id="a", title="First").save()
post.created_at        # populated from the row the server sent back
```

What it does, precisely:

- **Creates** (`save()`, `bulk_create()`) omit the column, which is exactly what lets the
  server's `DEFAULT` apply.
- **Replaces** (`update()`, `upsert()`) keep it. `DEFAULT` is a _create-time_ default on both DB
  lines, so on `UPDATE … CONTENT` an omitted optional column would be **deleted** and an omitted
  required one is a hard server error. The instance's value is carried instead, which for any
  record loaded or saved through the ORM is the server's own.
- After a write, the column is hydrated back onto the instance.
- An **explicit** write still works: `merge(created_at=…)`, `bulk_update(created_at=…)` and
  `server_values={"created_at": …}` all go through, because naming the column is taken as
  consent (a backfill, an admin correction). `get_or_create()` / `update_or_create()` count
  too: a server field named as a criterion or in `defaults` is stored on **both** branches, so
  `get_or_create(owner="bob", region="us")` finds the row it created on the next call instead
  of creating another under the server's default.

That last point is where `server_fields` differs from a computed field. A `Computed[...]` field
is defined by `DEFINE FIELD … VALUE`, so the server discards any client write — the ORM raises
rather than let an invisible no-op through. `get_server_fields()` returns the union of both
sets, since for payload building they are the same thing. A name in `server_fields` that the
model does not declare raises `ValueError` naming it.

#### `merge(refresh=False)`

By default `merge()` resyncs the instance from the row the `UPDATE` itself returns — one
round-trip, no follow-up read, and a merge that matched no record raises `SurrealDbError`. For
fire-and-forget updates, ask the server not to send that row at all:

```python
await user.merge(last_seen=now, refresh=False)                       # compiled with RETURN NONE
await user.merge(server_values={"seen": SurrealFunc("time::now()")}, refresh=False)
```

The write still happens and the literal keyword arguments are applied locally.

**What you give up**: the "no rows came back ⇒ record not found" check _is_ the returned row.
Under `refresh=False` a merge against a record that no longer exists is a silent no-op instead
of a `SurrealDbError`, and a field computed by `server_values` keeps its stale value until the
next `refresh()`. Keep the default whenever you need to know the write landed.

`tx`, `server_values`, `extra_vars` and `refresh` are reserved keyword names on `merge()`.

---

### 21. Live queries (real-time notifications)

A live query is a server-side subscription: SurrealDB pushes a notification every time a
record it watches is created, updated or deleted. It needs a **WebSocket** connection, and it
works on both SurrealDB 2.x (2.6 / 2.7) and 3.x (3.2 / 3.3).

#### Typed events: `live()`

Since v0.20.0, `live()` streams **model instances**, with the same API as the full
SurrealDB-ORM. The queryset's filters are applied by the server, and the live query is killed
when the block exits — even if the body raised:

```python
from surreal_orm_lite import LiveAction

async with User.objects().filter(role="admin").live() as stream:
    async for event in stream:
        match event.action:
            case LiveAction.CREATE:
                print("new admin:", event.instance.name)
            case LiveAction.UPDATE:
                print("admin updated:", event.instance)
            case LiveAction.DELETE:
                print("admin removed:", event.record_id)
```

Each item is a `ModelChangeEvent`:

| Attribute          | Type                      | Meaning                                                                                         |
| ------------------ | ------------------------- | ----------------------------------------------------------------------------------------------- |
| `action`           | `LiveAction`              | `CREATE`, `UPDATE` or `DELETE`                                                                  |
| `instance`         | your model                | the record after the change; for `DELETE`, its last state                                       |
| `record_id`        | `str`                     | the affected record, e.g. `"user:abc"`                                                          |
| `changed_fields`   | `list[str]`               | diff mode only: the top-level fields that changed, as Python field names                        |
| `raw`              | `dict / list`             | the payload as received: a dict, or the patch list in diff mode (a 2.x diff `DELETE` is a dict) |
| `validation_error` | `ValidationError \| None` | set when the record does not fit the model; `None` otherwise                                    |

A record that does not validate against the model does **not** end the stream — one bad row
written by another client, or a `fetch()`-resolved link on a field typed `str`, would otherwise
stop your `async for` and kill the subscription. The event carries the error in
`validation_error` and an instance built **without validation** from the record as received,
the way `exec()` falls back to plain rows. Check `event.validation_error` before trusting
`event.instance`; `post_live_change` handlers are not called for such events, so they only ever
see validated instances. A warning naming the failing fields (never their values) is logged once
per stream.

**What the server filters.** `filter()` (keyword lookups, `Q` objects, `Var` references with
`variables()`) and `fetch()` are honoured, with field aliases translated as everywhere else.
The filter is evaluated against each change, which has two consequences worth knowing:

- a record that **enters** the filter through an update is reported as `UPDATE`, not `CREATE`;
- a record that **leaves** it reports nothing — not even a `DELETE` — and a later `DELETE` of
  that record is not reported either, because it no longer matched.

**Filter values are written into the statement.** SurrealDB 2.x does not carry bound parameters
into a live query: `LIVE SELECT … WHERE status = $s` is accepted, then `$s` evaluates to `NONE`
at notification time and the filter matches nothing, silently. So for live queries only, the ORM
writes each value as a SurrealQL literal — the same approach as the full SurrealDB-ORM — and the
filter behaves identically on both lines. The encoder is closed: `None`, `bool`, `int`, `float`,
`str`, `Decimal`, `datetime` (written in UTC), `UUID`, `RecordID`, and lists or dicts of these;
since v0.23.0 also `timedelta` and `Duration` (`1h30m`), `date` (`d'2026-02-01T00:00:00Z'`), sets
(sorted arrays) and `Range` (`9..17`, `65..`), see [section 24](#24-rich-field-types). Anything
else, a string containing a NUL character, an integer beyond 64 bits, a `Decimal` SurrealDB would
round or a zero, negative or oversized duration raises `TypeError` when you call `live()`, before
anything is sent. A `Var` must be supplied through `variables()` — an unbound reference would read
as `NONE` on every notification, so `age > $min_age` would match every record; it raises
`SurrealDbError` instead (server parameters such as `$auth` and `$session` are allowed). Every
other query keeps binding parameters.

#### Diff mode

`live(diff=True)` asks the server for patches instead of whole records (`LIVE SELECT DIFF`):

```python
async with Product.objects().live(diff=True) as stream:
    async for event in stream:
        if event.action == LiveAction.UPDATE:
            print(event.record_id, "changed", event.changed_fields)  # ["price", "stock"]
```

A `CREATE` still carries the whole record, so `event.instance` is complete. An `UPDATE` carries
only patches: `event.instance` holds just the `id`, `changed_fields` lists what changed, and
`event.raw` is the patch list. The format is JSON Patch **extended by SurrealDB**: a changed
string arrives as `{"op": "change", "value": "<diff-match-patch text>"}` rather than a
`replace`. A `DELETE` gives an instance holding the `id` on 3.x, and its last state on 2.x (see
the behaviour table).

#### The `post_live_change` signal

Handlers connected to `post_live_change` run for every event a `live()` stream yields — changes
made by any client, unlike `post_save` and friends, which fire for this process's own writes:

```python
from surreal_orm_lite import LiveAction, post_live_change

@post_live_change.connect(Player)
async def on_player_change(sender, instance, action, record_id, changed_fields, **kwargs):
    if action == LiveAction.CREATE:
        await broadcast({"type": "player_joined", "name": instance.name})
```

Handlers run on one background task per stream, **in event order** — a slow handler for a
`CREATE` never lets the `DELETE` that follows overtake it — and never stall the iteration. One
that raises is logged and does not interrupt anything. Leaving the block normally waits for the
pending handlers (up to `stream.signal_drain_timeout`, 5 s by default) so the last event's side
effects are not lost; leaving it on an exception cancels them. The signal fires only while you
iterate a `live()` stream.

#### Surviving a dropped connection

Since v0.21.0 a `live()` or `watch()` stream survives a WebSocket that drops — a network
failure, a server restart, a proxy timing out. The ORM notices within milliseconds, opens a new
connection (signed in, session restored), sends the same live query again and carries on: an
`async for` parked on the stream simply receives the next event later. `stream.live_id` changes.

```python
async def catch_up(old_id, new_id):
    # A live query only reports what happens while it runs: re-read what the outage hid.
    await refresh_dashboard(await Order.objects().filter(status="open").all())

async with Order.objects().filter(status="open").live(on_reconnect=catch_up) as stream:
    async for event in stream:
        ...
```

- **`on_reconnect(old_id, new_id)`** — sync or async, called with both `UUID`s once the new
  subscription is already buffering, so nothing written after it is missed while it runs. An
  exception in it is logged, never raised.
- **The outage itself is not replayed.** Changes made while the connection was down are lost to
  a live query. Catch up in `on_reconnect`, or pair the stream with a change feed (below), which
  can be resumed exactly from a cursor.
- **Backoff:** `reconnect_delay` (0.5 s) doubling up to `reconnect_max_delay` (30 s), at most
  `reconnect_max_attempts` (10; `None` retries forever) — class attributes on `LiveModelStream`
  and `LiveStream`, overridable per stream. Only connection failures are retried. When the
  attempts run out, or anything else goes wrong (the table was removed, the session token was
  refused), the error is raised from the iteration and `is_active` turns `False`.
- **Never under another identity.** A live query runs with the permissions of whoever opened it,
  and a new connection comes back as the identity the manager replays (the stored session
  token, else the configured user). If the stream was opened under an identity a new connection
  cannot restore (`signin(..., store=False)`), or what a new connection would restore is no
  longer that identity (another stored token, other connection settings, a refused session
  replay), the stream ends with `SurrealDbAuthenticationError` instead of resubscribing as
  somebody else. The comparison is on the settings themselves, so changing them and changing them
  back — or `signin()` then `invalidate()` — counts as no change. It is per connection, so another
  event loop's identity changes do not leak in. Identity changes made directly on the SDK client
  are not seen by the ORM.
- **`auto_resubscribe=False`** ends the stream on a drop with `SurrealDbConnectionError`, as does
  any secondary reader from `subscribe_live(uuid)`, which is tied to one uuid. Before v0.21.0
  they all waited forever.
- **Teardowns you ask for still end the stream.** `kill()`, leaving the block,
  `close_connection()`, `reconnect()`, `set_url(reconnect=True)` — none of them triggers a
  resubscribe. `stop()` during a reconnect cancels it.
- A silent network loss with no TCP reset is noticed by the WebSocket keepalive, within about
  40 s.

The connection manager benefits too: `get_client()` now replaces a dropped WebSocket client
instead of handing back a dead one, and concurrent callers share one new connection. A request
that was already in flight when the socket dropped still fails — with the SDK's own `KeyError` —
because retrying arbitrary queries is planned for v0.40.0.

**One exception: an identity the manager does not store.** `signin()`, `signup()` and
`authenticate()` with `store=False` change the connection's own identity (that is the SDK's
behaviour) without telling the manager to restore it. If that connection drops, a new one could
only come back as the configured user — a silent change of privileges. So every call on that
event loop raises `SurrealDbAuthenticationError` until the application chooses: sign in or
authenticate again, `invalidate()`, or `reconnect()`/`close_connection()` to continue as the
configured user. Live streams on that connection end the same way. To mint tokens for other
users without touching the shared connection, use the model-level auth (section 19), which runs
on its own short-lived connections.

#### Migrating from SurrealDB-ORM

`live()`, `ModelChangeEvent`, `LiveModelStream`, `LiveAction` and `post_live_change` have the
same names, fields and arguments as in the full ORM — change the import from `surreal_orm` to
`surreal_orm_lite`. The differences:

- `auto_resubscribe=` and `on_reconnect=` are accepted with the same defaults (v0.21.0).
  `on_reconnect` receives `UUID`s rather than `str`, like lite's `live_id`; `str(old_id)` works
  with both.
- Lite **refuses** a clause a live query cannot honour (`order_by()`, `limit()`, …) where the
  full ORM ignores it, and honours `fetch()`.
- `record_id` is filled in diff mode too.

#### Raw envelopes: `watch()`

`watch()` takes the same filters and `diff=` but yields the server's **raw envelope**, a plain
dict, instead of an event:

```python
async with User.objects().filter(role="admin").watch() as stream:
    async for notif in stream:
        print(notif["action"], notif["result"])
        if notif["action"] == LiveAction.DELETE:
            break
# the live query is killed here, even if the body raised
```

| Key       | Type       | Meaning                                                                               |
| --------- | ---------- | ------------------------------------------------------------------------------------- |
| `action`  | `str`      | `CREATE`, `UPDATE` or `DELETE` — compare against `LiveAction`                         |
| `id`      | `UUID`     | the live query's own uuid                                                             |
| `record`  | `RecordID` | the affected record                                                                   |
| `result`  | `dict`     | the record after the change; for `DELETE`, its last content; patches with `diff=True` |
| `session` | `UUID`     | SurrealDB 3.x only — absent on 2.x                                                    |

Values keep their SDK types (`RecordID`, `Datetime`) rather than being coerced. With
`diff=True` the raw payload differs by line — see the behaviour table; `live(diff=True)`
normalises it.

`LiveAction` is a `StrEnum`, so it compares directly against the raw string and you never need
to quote a magic value.

#### The explicit form

When the subscription must outlive one block — read in one task, stopped from another — start
the stream yourself, hand **the stream** to the reader, and kill it by uuid from anywhere:

```python
from surreal_orm_lite import SurrealDBConnectionManager

stream = await User.objects().filter(role="admin").watch().start()  # or .live().start()

async def reader() -> None:
    async for notif in stream:  # ends when the live query is killed
        handle(notif)

task = asyncio.create_task(reader())
...
await SurrealDBConnectionManager.kill(stream.live_id)
```

A started stream buffers every notification until it is read, so always read **it** rather than
opening a second reader on its uuid and leaving the stream's own buffer to grow.
`SurrealDBConnectionManager.subscribe_live(live_id)` adds another reader to the same live query
— useful to fan one subscription out to several consumers, each of which must keep reading.

`await User.objects().live()`, the v0.19.0 way to get the uuid, still works but is deprecated
(`DeprecationWarning`). The returned stream also behaves as a coroutine for that path, so
`asyncio.create_task(qs.live())` and `asyncio.run(qs.live())` keep working too. Like a coroutine
it can be awaited once, and an awaited stream refuses to `start()` (and the reverse): the two
would be separate subscriptions, and leaving the block would kill only one of them.

`subscribe_live()` is **not** a coroutine: call it without `await`. Buffering starts the moment
you call it rather than at the first iteration, so a write you make in between is still
reported. Each reader on a uuid receives every notification.

A read that times out loses nothing, so polling with a deadline is safe:

```python
stream = SurrealDBConnectionManager.subscribe_live(live_id)
while True:
    try:
        notif = await asyncio.wait_for(anext(stream), timeout=1.0)
    except TimeoutError:
        continue  # a quiet second; the stream is still alive
    handle(notif)
```

`await stream.aclose()` stops one reader and drops its buffer without killing the live query.

`kill()` is idempotent — an unknown or already-killed uuid is a no-op — and it ends every open
stream on that uuid, so the `async for` above terminates rather than waiting forever.

#### What a live query will refuse

- **A non-WebSocket connection.** Live queries cannot run over HTTP; `live()`, `watch()` and
  `subscribe_live()` say so instead of letting the SDK raise a bare `NotImplementedError`.
- **A clause it cannot honour.** SurrealDB's `LIVE SELECT` has no `ORDER BY`, `LIMIT` or
  `START`, and a typed stream needs whole records, so `select()`, `limit()`, `offset()`,
  `order_by()`, `values()` and `annotate()` are rejected by name rather than silently ignored.
- **A transaction.** A live query is a connection-level subscription with no transaction to
  join, so `objects(tx=tx).live()` is refused rather than quietly run outside it.
- **A filter value with no SurrealQL literal form** (see above) — `TypeError` at the call.
- **A table that does not exist, on SurrealDB 3.x.** You get a `SurrealDbNotFoundError` naming
  the table. On 2.x the same call succeeds and simply never notifies. Define the table first
  if you need the two lines to behave alike.

#### The stream handles

`live()` returns a `LiveModelStream` and `watch()` a `LiveStream`, both exported from the package
root. They share these members:

| Member      | Type             | Meaning                                                       |
| ----------- | ---------------- | ------------------------------------------------------------- |
| `live_id`   | `UUID \| None`   | the live query's uuid; `None` before start and after `stop()` |
| `table`     | `str`            | the table being watched                                       |
| `is_active` | `bool`           | whether the subscription is running                           |
| `start()`   | `async` → self   | open the subscription; a no-op on a running stream            |
| `stop()`    | `async` → `None` | kill the subscription; safe before start and to repeat        |

`__aexit__` calls `stop()`, so the `async with` form needs none of these. They are there for the
cases where you hold the stream yourself.

#### Lifecycle and limitations

A reader parked on `async for` only wakes when something wakes it, so it is worth knowing
exactly what ends a stream.

- **`kill()` ends it**, and so does leaving a `live()` or `watch()` block. This works identically on both
  server lines.
- **Closing the connection ends it.** `close_connection()` releases every reader on its event
  loop before the socket goes, and `close_all_connections()` releases every reader on every
  loop, so an ordinary shutdown does not leave a wedged task behind. Leaving a `watch()` block
  afterwards does not reconnect: with the connection gone there is nothing left to kill.
- **A timed-out read does _not_ end it.** Cancelling a pending read loses nothing; the next read
  picks up where it left off.
- **A WebSocket that drops on its own does _not_ end it** (v0.21.0): the stream resubscribes —
  see [Surviving a dropped connection](#surviving-a-dropped-connection). With
  `auto_resubscribe=False`, and for `subscribe_live()` readers, it raises
  `SurrealDbConnectionError` instead.
- **Subscriptions are per event loop.** A uuid belongs to the connection that created it, on the
  loop that created it. Killing it from another loop is not supported.
- **A failed `kill()` releases the readers but keeps the uuid alive.** When the server refuses
  the kill for any reason other than an unknown uuid, the error is raised, the readers are woken,
  and the subscription is still running — so you can subscribe to it again or retry `stop()`.
- **The buffer is unbounded.** A stream you stop reading but never kill keeps accumulating
  notifications. Use the `async with` forms, or pair every uuid you start with a `kill()`.

### 22. Change feeds (`changes()`)

A change feed is SurrealDB's durable log of a table's writes, kept for a retention period you
choose. Unlike a live query, nothing is missed while nobody reads: a consumer that stops and
restarts resumes exactly where it left off. It suits event streaming, replication and audit.

```sql
DEFINE TABLE order SCHEMALESS CHANGEFEED 7d;   -- only writes made after this are recorded
```

```python
stream = Order.objects().changes(since=load_cursor())   # None = from now
async for event in stream:
    await publish(event.action, event.instance)          # ModelChangeEvent, as for live()
    save_cursor(stream.cursor)                           # after processing
```

- **`stream.cursor`** is an `int`: pass it back as `since=` to resume. It moves past a
  transaction only once that transaction's last event has been yielded, so saving it after each
  event gives **at-least-once** delivery: nothing is ever skipped, and a transaction cut short by
  a crash is delivered again in full.
- **`since=`** takes a cursor, a `datetime` or ISO-8601 string (naive means UTC), or `None` for
  changes made after the stream starts. A datetime is exact on SurrealDB 3.x and at-least-once on
  2.x, whose server also returns some earlier changes.
- **What an event says.** A change feed records writes, not their kind: a creation arrives as
  `UPDATE`, a deletion as `DELETE`. With `CHANGEFEED … INCLUDE ORIGINAL` on the table, updates
  also carry `changed_fields`, and on SurrealDB 3.3 a deletion carries the last state of the
  record (elsewhere the instance holds only its `id`).
- **`poll_interval`** (0.1 s) is the pause when the feed has nothing new; **`batch_size`** (100)
  the entries read per poll. `stop()` ends the iteration at once when it is waiting between
  polls or in a retry backoff, or as soon as a request already sent returns; `start()` fixes the
  starting position before the first read.
- **A read under `asyncio.timeout()` or `asyncio.wait_for()`** times out as asked and loses
  nothing: the stream keeps going, and the next read starts from the cursor. Before v0.22.2 a
  read cancelled just as the server answered could be retried as a dropped connection instead,
  and never time out.
- Works over **HTTP** as well as WebSocket. A dropped connection is retried with the same
  backoff as live streams, and nothing is lost, because the next read starts from the cursor.
- **Refused:** any queryset clause — `SHOW CHANGES` has no `WHERE`, so `filter()` would silently
  stream the whole table — and a table that is missing or has no change feed (on the table or
  on its database), with the `DEFINE TABLE` to run.
- **Writing to a change-feed table on SurrealDB 2.x: wrap the write in `retry_on_conflict`.**
  On 2.x, concurrent writes to a `CHANGEFEED` table conflict with each other, even on different
  records. Measured on 2.7.0 with 8 writers sharing one connection: 2.5 to 4 % of `save()` and
  `merge()` calls were refused with a retryable conflict, in every second of the run. A single
  writer was refused far more rarely: at most once per cycle of the server's change-feed garbage
  collection (`--changefeed-gc-interval`, 10 s by default), and not on every cycle. The same
  8-writer load refused nothing on a table without a change feed, nor on 3.3.4 (`save()`,
  `merge()` and `delete()`). The ORM does not retry these writes for you: apply
  `retry_on_conflict` yourself (see [section 14](#14-optimistic-concurrency-retry_on_conflict)).
  A refused write stores nothing, so replaying it is safe for the data; the `pre_save` and
  `around_save` handlers run again on each attempt. Outside a transaction the error is the SDK's
  own `InternalError` ("… This transaction can be retried"), which `is_conflict_error()`
  recognises; once the retries run out, `retry_on_conflict` raises `SurrealDbConflictError`.

  ```python
  from surreal_orm_lite import retry_on_conflict

  @retry_on_conflict()
  async def save_order(order: Order) -> Order:
      return await order.save()

  await save_order(Order(id="o1", total=42))
  ```

**Why the cursor is not the server's `versionstamp`.** SurrealDB 2.x and 3.x number their feeds
differently: resuming a 2.x feed with the stamp it returned yields nothing at all, and asking
3.x for changes since a datetime always answers empty. `ChangeModelStream` normalises both (see
the behaviour table), so the same code resumes correctly on either line.

### 23. Typed edges (`BaseSurrealEdge`)

Since v0.22.0 a graph edge can be a model: its data validated by Pydantic, created with
SurrealDB's `INSERT RELATION` (the official SDK's `insert_relation()`), and read, filtered and
watched like any other record, with the same behaviour on SurrealDB 2.x and 3.x. The string form
of section 7 (`relate("follows", …)`) is unchanged.

```python
from pydantic import Field, ValidationError
from surrealdb import RecordID
from surreal_orm_lite import BaseSurrealEdge, BaseSurrealModel, SurrealConfigDict, SurrealDbError


class User(BaseSurrealModel):
    id: str | None = None
    name: str


class Product(BaseSurrealModel):
    id: str | None = None
    name: str


class Purchased(BaseSurrealEdge):
    model_config = SurrealConfigDict(table_name="purchased")

    quantity: int = Field(ge=1)
    price: float


class Follows(BaseSurrealEdge):
    model_config = SurrealConfigDict(table_name="follows")

    weight: int = 1
```

`BaseSurrealEdge` declares the two endpoints, so a subclass declares only the edge's data:

- **`in_`** (stored in the column `in`, a Python keyword) and **`out`** hold a `RecordID`. Each
  accepts a saved model instance, a `RecordID` or a `"table:id"` string whose id is read as a
  string (`"User:1"` is `User(id="1")`, as for `relate()`). For an id with other characters
  (`joe@x.com`) or an integer id, pass the instance or a `RecordID`. Anything else, an unsaved
  instance or `"nocolon"`, is a `ValidationError`.
- `table_name` maps the model to its edge table, here the lower-case `purchased` and `follows`
  that `relate("purchased", …)` writes (see [Custom table name](#custom-table-name-table_name)).
  Without it the table is the class name.
- A subclass may not redeclare `in_` or `out` (`TypeError` at the `class` statement).

#### Creating edges

```python
alice = await User(id="alice", name="Alice").save()
bob = await User(id="bob", name="Bob").save()
carol = await User(id="carol", name="Carol").save()
widget = await Product(id="widget", name="Widget").save()
gadget = await Product(id="gadget", name="Gadget").save()

# relate() with an edge class: validated, created, returned typed
edge = await alice.relate(Purchased, widget, quantity=2, price=9.5)
assert isinstance(edge, Purchased) and edge.in_ == RecordID("User", "alice")
edge = await alice.relate(Purchased, gadget, data={"quantity": 1, "price": 4.0})  # data= works too

# save() on an edge instance
edge = await Purchased(
    in_=RecordID("User", "bob"), out=RecordID("Product", "widget"), quantity=1, price=9.5
).save()
print(edge.id)                                   # purchased:… (the server's RecordID)

# bulk_create(): the whole batch in one insert_relation() round-trip
edges = await Purchased.objects().bulk_create([
    Purchased.model_validate({"in": alice, "out": widget, "quantity": 3, "price": 9.0}),
    Purchased.model_validate({"in": bob, "out": gadget, "quantity": 1, "price": 4.0}),
])

try:
    await alice.relate(Purchased, widget, quantity=0, price=9.5)
except ValidationError:
    pass                                         # checked before anything is sent: nothing written
```

- The edge is a real relation: `->purchased->Product`, `traverse()` and
  `get_related(Purchased, …)` see it. A `CREATE` or `UPSERT` carrying `in`/`out` would store a
  plain record that no traversal sees, which is why `upsert()` is refused on an edge (below).
- `edge.id` is the server's `RecordID`; computed fields and `server_fields` are read back from
  the row, as on any create. `save()` and `relate(Edge, …)` fire the `save` signals (`pre_save`,
  `around_save`, `post_save` with `created=True`); `bulk_create()` fires none, as for nodes.
- `relate(Edge, …)` checks the edge fields before anything is sent: an endpoint (`in`, `in_`,
  `out`) or a computed field raises `ValueError`, a field the model does not declare
  `SurrealDbError` (unless the model sets `extra="allow"`). `reverse=True` and `tx=` work as in
  section 7: `alice.relate(Follows, bob, reverse=True)` creates `bob -> follows -> alice`.
- `Purchased(in_=alice, …)` works at run time, but a type checker expects a `RecordID` there:
  `Purchased.model_validate({"in": alice, …})` or `relate()` is the type-checker-clean form with
  model instances.

#### Reading edges

```python
mine = await Purchased.objects().filter(in_=alice).order_by("-quantity").exec()
same = await Purchased.objects().filter(in_="User:alice").exec()             # a "table:id" string
same = await Purchased.objects().filter(in_=RecordID("User", "alice")).exec()
both = await Purchased.objects().filter(in___in=[alice, "User:bob"]).exec()  # the in lookup on in_
products = await alice.get_related(Purchased, model_class=Product)
```

An edge model reads like any model (`get()`, `filter()`, `Q`, `count()`, aggregations). On the
endpoints a filter value may be a model instance, a `RecordID` or a `"table:id"` string, read by
the rule the fields validate with, so `filter(in_="User:alice")` finds alice's edges (a string
never equals a record id in SurrealQL, so it would otherwise match nothing). A text lookup on an
endpoint (`in___contains`, `out__startswith`, …) raises `ValueError`. `in___in` is the `in`
lookup on the field `in_` (see [Fields ending in an underscore](#fields-ending-in-an-underscore)).

#### Endpoints are immutable

SurrealDB ignores a change to an existing edge's endpoints on both lines: an `UPDATE` writes the
other fields and leaves `in`/`out` as they were. The ORM refuses the change instead of letting
it pass silently:

```python
edge = await alice.relate(Purchased, widget, quantity=1, price=9.5)

try:
    edge.out = RecordID("Product", "gadget")      # a frozen field
except ValidationError:
    pass
try:
    await edge.merge(out=gadget)                  # refused before anything is sent
except ValueError as exc:
    print(exc)  # merge(): `in`/`out` are the endpoints of edge `Purchased`; … delete the edge and relate again.

await edge.merge(quantity=3)                      # the edge's data changes as usual

# To move an edge, delete it and relate again
await edge.delete()
edge = await alice.relate(Purchased, gadget, quantity=3, price=4.0)
```

- `ValueError`, before any call, for every write that names an endpoint: `merge()` (and its
  `server_values=`), a `patch()` or `objects().patch()` pointer on `/in` or `/out`, the
  `atomic_*` helpers, `objects().bulk_update()`, and `update_or_create(defaults=…)`.
- `update()`, a full replace, sends the endpoints unchanged: SurrealDB 2.x rejects a replace
  without them on a `TYPE RELATION … SCHEMAFULL` table.
- `upsert()` and `save(server_values=…)` raise `SurrealDbError`: an `UPSERT` stores a plain
  record that no traversal sees, and `INSERT RELATION` cannot evaluate `SET` expressions (put a
  `DEFINE FIELD … DEFAULT` or `VALUE` on the edge table instead).
- `edge.model_copy(update={"out": …})` is **not** checked, because Pydantic skips validation
  there. That is legitimate for a copy meant as a new edge
  (`model_copy(update={"id": None, "out": other})`, then `save()`); on a copy that kept its id,
  `update()` or `merge()` would send an endpoint change the server silently ignores.

#### Explicit ids

```python
a, w = RecordID("User", "alice"), RecordID("Product", "widget")
await Purchased(id="p1", in_=a, out=w, quantity=1, price=9.5).save()
try:
    await Purchased(id="p1", in_=a, out=w, quantity=5, price=9.5).save()
except SurrealDbError as exc:
    print(exc)  # … Database record `purchased:p1` already exists
```

An edge with an id goes through a guarded `INSERT RELATION`: an id that already exists raises
`SurrealDbError` ("already exists") on both lines, and the stored edge is unchanged. A raw
`INSERT RELATION` does not do that everywhere: SurrealDB 2.7.0 and 3.1.5 silently keep the stored
edge and return it as if it had been inserted (see the behaviour table). `bulk_create()` takes
the same path, so one duplicate in a batch, stored already or repeated in the batch, writes
nothing. With other endpoints under an existing id, 2.7.0 and 3.1.5 report a field mismatch
("Found … which does not match the existing field value") instead of "already exists": still a
`SurrealDbError`, still nothing changed. An id that is a `RecordID` of another table raises
`ValueError` before anything is sent (SurrealDB 2.x would silently move it into the edge table).

#### Idempotent edges

`get_or_create()` is the convenient idempotent relate: it looks the pair up and creates the edge
only when there is none. Instances, `RecordID`s and `"table:id"` strings converge on one row:

```python
follow, created = await Follows.objects().get_or_create(in_=alice, out=bob)                # created: True
follow, created = await Follows.objects().get_or_create(in_="User:alice", out="User:bob")  # False, same edge
```

Between its lookup and its create, a concurrent caller can create the same edge. SurrealDB's own
mechanisms close that window on the server, and through the ORM both raise `SurrealDbError` on
2.x and 3.x alike.

**A deterministic array id**, one id per pair: a second `save()` with that id raises "already
exists" (the guard above).

```python
a, c = RecordID("User", "alice"), RecordID("User", "carol")
await Follows(id=RecordID("follows", [a, c]), in_=a, out=c).save()
try:
    await Follows(id=RecordID("follows", [a, c]), in_=a, out=c, weight=2).save()
except SurrealDbError as exc:
    print(exc)  # … Database record `follows:[User:alice, User:carol]` already exists
```

For a symmetric relation (a friendship, where alice–carol and carol–alice are one edge), sort the
pair first (`sorted([a, c], key=str)`) and use that order for the id and for `in_`/`out`. A raw
`RELATE` that names an existing edge id silently **overwrites** it on both lines, so create these
edges with `save()` or `bulk_create()`, which go through the guard. Such an edge is then updated
and deleted like any other, `update(tx=)` and `delete(tx=)` included: the ORM binds the record id
as a query parameter, never writes it into the statement.

**A `UNIQUE` index on `in, out`**: the server refuses a second edge for the same ordered pair,
whatever its id.

```python
from surreal_orm_lite import SurrealDBConnectionManager


class Likes(BaseSurrealEdge):
    model_config = SurrealConfigDict(table_name="likes")


client = await SurrealDBConnectionManager.get_client()
await client.query("DEFINE INDEX likes_pair ON likes FIELDS in, out UNIQUE;", {})

await alice.relate(Likes, bob)
try:
    await alice.relate(Likes, bob)
except SurrealDbError as exc:
    print(exc)  # … Database index `likes_pair` already contains [User:alice, User:bob], …
await bob.relate(Likes, alice)                    # the reverse pair is another edge: allowed
```

The index is directional, so the reverse pair is allowed. A second string `relate("likes", …)`
for the same pair fails as well, with the SDK's `ServerError` (the string form does not translate
server errors).

#### Refused writes and permissions

SurrealDB answers an `INSERT RELATION` that the edge table's `PERMISSIONS FOR create` refuse (or
an edge the user may not `select` afterwards) with fewer rows and no error, on both lines. The ORM
raises `SurrealDbError` instead: "Can't save data, no record returned: …" for one edge (`save()`,
`relate(Edge, …)`; `post_save` does not fire) and "Only k of n edges came back: …" for
`bulk_create()`. Outside a transaction such a batch is **not atomic**: the edges the server
accepted stay written. Inside an interactive transaction the error rolls the whole transaction
back; a buffered transaction cannot detect the refusal, since nothing comes back before commit.

#### Transactions

```python
async with SurrealDBConnectionManager.transaction() as tx:
    edge = await bob.relate(Follows, carol, tx=tx)
    await Purchased.objects(tx=tx).bulk_create([
        Purchased.model_validate({"in": carol, "out": gadget, "quantity": 2, "price": 4.0}),
    ])
print(edge.id)  # a RecordID on an interactive transaction, None on a buffered one
```

Edges follow the strategy of every write (section 11). On an **interactive** transaction
(WebSocket + SurrealDB 3.x) the row comes back at once: `edge.id` is set, `objects(tx=)` reads
see the edge, and a rollback removes it. On a **buffered** one (2.x, or HTTP) the edge is queued
and written at commit, then rolled back with the batch if the commit fails. An auto id is allowed
there, unlike a node's buffered `save()`, so `edge.id` stays `None`; `bulk_create()` returns its
inputs, and a duplicate explicit id fails the commit. `post_save` fires after the commit. The
graph helpers of section 7 take `tx=` too.

#### Live queries and change feeds

```python
async with Purchased.objects().filter(in_=alice).live() as stream:
    await alice.relate(Purchased, gadget, quantity=4, price=4.0)
    async for event in stream:
        print(event.action, event.instance.out, event.instance.quantity)  # CREATE Product:gadget 4
        break
```

`live()`, `watch()` and `changes()` work on edge models unchanged, and the typed `instance` is an
edge. A live filter on an endpoint takes the same values as `filter()`: an instance, a `RecordID`
or a `"table:id"` string. SurrealDB 2.x adds `"__": true` to the edge record of a `CREATE` or
`UPDATE` notification and of a change-feed update entry; the typed `instance` drops it, so it is
the same on both lines, while `event.raw` keeps the payload as the server sent it.

#### A `TYPE RELATION` table, defined by hand

```python
class Reviewed(BaseSurrealEdge):
    model_config = SurrealConfigDict(table_name="reviewed")

    stars: int = Field(ge=1, le=5)


await client.query("DEFINE TABLE reviewed TYPE RELATION IN User OUT Product;", {})
await alice.relate(Reviewed, widget, stars=5)
try:
    await alice.relate(Reviewed, bob, stars=1)    # out must be a Product
except SurrealDbError as exc:
    print(exc)
```

Typed edges work with whatever table exists: one created by the first edge, a `TYPE ANY` table,
or a `TYPE RELATION [IN … OUT …] [ENFORCED]` table you define. The server's refusals come back as
ORM errors on both lines: an endpoint from the wrong table raises `SurrealDbError`; with
`ENFORCED`, an endpoint record that does not exist raises `SurrealDbNotFoundError`; a
`TYPE NORMAL` table raises `SurrealDbError`. A helper that writes this DDL from the model (the
full ORM's `table_type=TableType.RELATION`, `relation_in`, `relation_out` and `enforced` keys, on
`BaseSurrealEdge`) is planned for v0.33.0.

#### Migrating from SurrealDB-ORM (graph API)

The graph helpers of section 7 have the full ORM's method names, parameter order and meaning
(`reverse=`, keyword edge fields, `tx=`, lists of edges, `direction="both"`, a record id without
its table as the `remove_relation()` target): change the import from `surreal_orm` to
`surreal_orm_lite`. What differs:

1. The string `relate()` returns a `list[dict]`, the created rows (`[]` inside a buffered
   transaction), where the full ORM returns one `dict`. Changing it would break lite's v0.6.0
   API.
2. `data=` is lite-only. The full ORM's `**edge_data` works too, and the two merge (a field in
   both is a `TypeError`). An edge field named like a parameter (`data`, `tx`, `reverse`, `edge`,
   `target` or `self`) must go through `data=`.
3. Typed edge models (`BaseSurrealEdge`, `relate(EdgeModel, …)`) are lite-only. The full ORM
   declares a relation table with `table_type=TableType.RELATION` (plus `relation_in`,
   `relation_out` and `enforced`), which is DDL only; lite gets those keys, with those names, on
   `BaseSurrealEdge` in v0.33.0.
4. `table_name` is validated as an identifier when the class is defined, because it is written
   into SurrealQL.
5. `upsert()` and `save(server_values=…)` are refused on an edge (an `UPSERT` stores a plain
   record that no traversal sees).
6. Lite's `save()` keeps create semantics, so an existing id raises "already exists", where the
   full ORM's `save()` upserts.
7. Positional calls are identical to the full ORM: `relate(edge, target, reverse, tx)`,
   `remove_relation(edge, target, reverse, tx)`, `remove_all_relations(edge, direction, tx)`,
   `get_related(edge, direction, model_class)`. Only the names of the first two parameters
   differ, `edge`/`target` in lite and `relation`/`to` in the full ORM, because renaming them
   would break existing v0.6.0 keyword calls. `data=` is keyword-only (lite-only).
8. `get_related()` without `model_class` returns the neighbours' record ids (`RecordID`), where
   the full ORM returns the records as dicts (`SELECT VALUE out.*`). Pass `model_class` to get
   instances. Changing it would break lite's v0.6.0 API.

### 24. Rich field types

Since v0.23.0 a model can declare `timedelta`, `date` and `set[X]`/`frozenset[X]` fields, and the
ORM's own `Duration` and `Range[T]`: every write stores them as SurrealDB's native values and every
read returns the declared type, with the same behaviour on SurrealDB 2.x and 3.x (one exception:
`TYPE set<…>` columns, see [Sets](#sets)). `datetime`, `Decimal`, `UUID` and `bytes` fields keep
working as before.

```python
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from surreal_orm_lite import (
    BaseSurrealModel,
    BoundExcluded,
    BoundIncluded,
    Duration,
    Range,
    SurrealConfigDict,
)


class Subscription(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="subscription")

    id: str | None = None
    started_at: datetime
    renews_on: date
    trial: timedelta
    grace: Duration | None = None
    price: Decimal
    token: UUID
    active_hours: Range[int]
    period: Range[datetime] | None = None
    logo: bytes | None = None
    tags: set[str] = set()


await Subscription(
    id="acme",
    started_at=datetime(2026, 1, 2, 9, 30, tzinfo=UTC),
    renews_on=date(2026, 2, 1),
    trial=timedelta(days=14),
    grace=Duration.parse("1h30m1ns"),                         # nanoseconds kept
    price=Decimal("9.99"),
    token=UUID("0190c3d1-5c3b-7cc6-8f8a-2d2b1f0e9a11"),
    active_hours=Range(BoundIncluded(9), BoundExcluded(17)),  # 9..17: 9 included, 17 excluded
    period=Range(BoundIncluded(datetime(2026, 1, 1, tzinfo=UTC)), None),  # open end
    logo=b"\x89PNG",
    tags={"pro", "annual"},
).save()

sub = await Subscription.objects().get("acme")
sub.trial         # timedelta(days=14): a duration on the server
sub.renews_on     # date(2026, 2, 1): a datetime at midnight UTC on the server
sub.tags          # {'annual', 'pro'}: an array on the server
sub.active_hours  # Range(begin=BoundIncluded(value=9), end=BoundExcluded(value=17))
```

| Annotation                   | Sent as                                                                      | Stored as        | Read back as                                |
| ---------------------------- | ---------------------------------------------------------------------------- | ---------------- | ------------------------------------------- |
| `datetime`                   | as before (a naive value is taken as UTC)                                    | `datetime` (UTC) | an aware UTC `datetime`, to the microsecond |
| `Decimal`                    | as before; a value written with an exponent goes out in fixed-point notation | `decimal`        | `Decimal`                                   |
| `UUID`                       | as before                                                                    | `uuid`           | `UUID`                                      |
| `bytes`                      | as before                                                                    | `bytes`          | `bytes`                                     |
| `timedelta`                  | a duration                                                                   | `duration`       | `timedelta`, floored to the microsecond     |
| `date` (subclasses included) | a `datetime` at midnight UTC                                                 | `datetime`       | `date`                                      |
| `set[X]`, `frozenset[X]`     | an array, sorted when its items can be ordered                               | `array`          | the set or frozenset (see [Sets](#sets))    |
| `Duration`                   | a duration                                                                   | `duration`       | `Duration`, to the nanosecond               |
| `Range[T]`, `Range`          | a range; an open side as SurrealDB's null bound                              | `range`          | `Range`, each bound value validated as `T`  |

Optional fields (`ttl: timedelta | None = None`) behave the same. Values nested in containers
(`list[timedelta]`, `dict[str, date]`, a sub-model's fields) are sent the same way at any depth. On
reads, a `timedelta` is converted from the SDK's duration at any depth too: a top-level field,
`list[timedelta]`, `dict[str, timedelta]`, `timedelta | str` or a sub-model's `timedelta` field
all read a `timedelta`, floored to the microsecond (declare `Duration` to keep the nanoseconds).
Your validators (any mode) and `model_post_init` still apply once to the value read, even when they
change it in place. A naive `datetime` stays naive on the instance and is read back as an aware UTC
`datetime`, as before.

#### `Duration`

```python
from datetime import timedelta

import surrealdb
from surreal_orm_lite import Duration, InvalidDurationError

d = Duration.parse("1h30m1ns")
d.elapsed                 # 5400000000001: nanoseconds
d.to_string()             # '1h30m1ns': every unit, lossless (str(d) too)
d.to_compact()            # [5400, 1]: [seconds, nanoseconds]
d.get_seconds_and_nano()  # (5400, 1)
d.to_timedelta()          # timedelta(seconds=5400): floored to the microsecond
Duration.parse(90, 5)     # Duration(elapsed=90000000005): an int is seconds, then nanoseconds
Duration.from_timedelta(timedelta(days=7))            # Duration(elapsed=604800000000000)
Duration.parse(" 1h30m ") == Duration.parse("1h30m")  # True: surrounding whitespace is ignored

try:
    Duration.parse("1h 30m")             # "1.5h" is refused too
except InvalidDurationError as exc:      # a ValueError and a surrealdb.InvalidDurationError
    print(exc)

Duration(5) == surrealdb.Duration(5)     # True
surrealdb.Duration(5) == Duration(5)     # False: keep the ORM value on the left
```

`timedelta` stops at microseconds: declare a field `Duration` to keep the server's nanoseconds.
`Duration` is the ORM's own class, not the SDK's `surrealdb.Duration`, but it mirrors the SDK
2.0.0 class: the same members under the same names and parameters (`elapsed`,
`parse(value, nanoseconds=0)` with an `int` in seconds, `get_seconds_and_nano()`, `to_compact()`,
`to_string()` and the unit properties `seconds`, `minutes`, `hours`, `days`, …), with exact
results: `get_seconds_and_nano()` uses integer arithmetic, `to_compact()` keeps the nanoseconds
(SDK 2.0.0 returns `[seconds]`) and `to_string()` writes every unit (SDK 2.0.0 writes `"1h"` for
`1h30m1ns`). `parse(str)` reads the server's syntax strictly: surrounding whitespace is accepted,
`"1h 30m"` and `"1.5h"` are refused. A refused value raises `InvalidDurationError`, which is both a
`ValueError` and a `surrealdb.InvalidDurationError`; a wrong argument type raises `TypeError`.
`Duration` adds `MAX_NANOSECONDS` (the largest SurrealDB duration), `from_timedelta()`,
`to_timedelta()`, hashing and ordering.

As a field, `Duration` accepts a `Duration`, the SDK's `Duration`, a non-negative `timedelta` or a
SurrealQL duration string (`"1h30m"`), and refuses a bare number, which does not say its unit. In
JSON it is its SurrealQL text: `model_dump_json()` writes `"1h30m1ns"`, which
`model_validate_json()` reads back.

**Equality with the SDK's values is one-way.** `Duration(5) == surrealdb.Duration(5)` is `True`,
but `surrealdb.Duration(5) == Duration(5)` is `False`: the SDK's `__eq__` answers `False` for any
other type, so Python never asks the ORM's. The same holds for `Range` and the SDK's `Range`. Keep
the ORM value on the left, or compare `elapsed`.

#### `Range[T]`

```python
from pydantic import TypeAdapter

from surreal_orm_lite import BoundExcluded, BoundIncluded, Range

str(Range(BoundIncluded(9), BoundExcluded(17)))  # '9..17'
str(Range(BoundExcluded(1), BoundIncluded(5)))   # '1>..=5'
str(Range(begin=BoundIncluded(65)))              # '65..': None is an open side
str(Range())                                     # '..'

hours = TypeAdapter(Range[int])
hours.validate_python(range(9, 17))  # Range(begin=BoundIncluded(value=9), end=BoundExcluded(value=17))
hours.validate_json('"9..17"')       # the same range: in JSON a Range is its SurrealQL text
```

A `Range` has two bounds, each `BoundIncluded(value)` or `BoundExcluded(value)` (the SDK's
classes, exported by the ORM) or `None` for an open side; `Range(1, 5)` raises `TypeError`. The
SDK's own spellings of an open side, a bare `Bound()`, `surrealdb.cbor.undefined` and a CBOR null,
are accepted too and stored as `None`. Whatever its spelling, an open side is sent as SurrealDB's
null bound, the form its protocol defines.

As a field, `Range[int]`, `Range[datetime]`, `Range[timedelta]`… validate each bound value as `T`
(a bare `Range` keeps them as given) and accept the SDK's `Range`, a builtin `range` with step 1
(`range(9, 17)` is `9..17`) or the SurrealQL text `str()` writes (`"9..17"`,
`"d'2026-01-01T00:00:00Z'.."`, `"1m..1h"`). A `Range[timedelta]` turns the SDK's duration bounds
into `timedelta`s (floored to the microsecond) and a `Range[date]` accepts midnight `datetime`
bounds. In JSON a `Range` is that text, which `model_validate_json()` reads back: every bound is
written exactly, a zero duration as `0ns` and a `Decimal` with all its digits, even where a live
query filter refuses the value. A `Range` is not hashable, like the SDK's.

#### Filters

```python
class Slot(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="slot")

    id: str | None = None
    hour: int


for hour in (8, 9, 12, 17):
    await Slot(id=f"h{hour}", hour=hour).save()

# in / not_in with a Range: bound as one value (hour IN $_f0), never expanded
working = await Slot.objects().filter(hour__in=Range(BoundIncluded(9), BoundExcluded(17))).exec()  # 9, 12
late = await Slot.objects().filter(hour__not_in=Range(end=BoundExcluded(17))).exec()              # 17

# Filter values are sent like field values
trials = await Subscription.objects().filter(trial__gte=timedelta(days=7), renews_on=date(2026, 2, 1)).exec()
cheap = await Subscription.objects().filter(price__lte=Decimal("9.99"), tags__containsall={"pro"}).exec()
open_at_noon = await Subscription.objects().filter(active_hours__contains=12).exec()  # 12 in 9..17
graced = await Subscription.objects().filter(grace__lte=Duration.parse("2h")).exec()
```

Filter values on rich fields **must be typed**: they are sent exactly like field values, and
strings are not converted. `filter(trial__gte="7d")` compares the stored duration with a string,
which does not give the result you want: pass `timedelta(days=7)`.

`in` and `not_in` take a `Range`, the ORM's or the SDK's, and bind it as one value; a builtin
`range` keeps its meaning, the list of its integers. On the record-id column `id` and on an edge's
`in`/`out` columns a `Range` raises `ValueError`, since a record id never falls inside a value
range. `contains` on a range column tests membership (`active_hours__contains=12`). Live queries
([section 21](#21-live-queries-real-time-notifications)) write these values into the statement as
SurrealQL literals (`1w`, `d'2026-02-01T00:00:00Z'`, `['pro']`, `9..17`), with the same results on
both lines.

#### Precision and `Decimal`

- `datetime`: to the microsecond. The SDK truncates the server's nanoseconds, so a stored
  `d'2026-01-02T03:04:05.123456789Z'` reads `…05.123456`.
- `timedelta`: floored to the microsecond on read (`1h30m1ns` reads `timedelta(seconds=5400)`).
- `Duration`: exact, to the nanosecond.
- `Decimal`: a value whose `str()` writes no exponent is sent as before. A value written with an
  exponent (`Decimal("1E-7")`, `Decimal("1E+3")`) goes out in fixed-point notation (`0.0000001`,
  `1000`), cut after the 29th fractional digit: the server keeps at most 28 fractional digits,
  rounding half away from zero as before, so the stored value is the same. A finite value whose
  integer part, rounded, exceeds `2**96 - 1` (the largest SurrealDB decimal) is refused before
  sending. On reads, SurrealDB 2.x keeps trailing zeros (`Decimal("12.50")` reads `12.50`) where
  3.x normalises them (`12.5`); the values are equal. `NaN` and `±Infinity` are stored as floats,
  as before: declare `Annotated[Decimal, Field(allow_inf_nan=True)]` to read them back.

#### Sets

`set[X]`/`frozenset[X]` fields are stored as arrays. On SurrealDB 3.3 a `TYPE set<…>` column
stores the array as a native set, which the field reads back as the declared set (see
[Values the SDK cannot read](#values-the-sdk-cannot-read)), but 3.1 refuses an array in it; to
support 3.1, declare such columns `array<…>`, optionally with `VALUE $value.distinct()`:

```sql
DEFINE FIELD tags ON subscription TYPE array<string> VALUE $value.distinct();
```

On a schemaless or `array<…>` column the field reads back as the declared `set` or `frozenset`, on
both lines. The array is sorted when its items can be ordered, so a set is written the same way
every time.

#### Values refused before sending

Every value the ORM sends (a field, a filter value, a `call_function()` argument, an auth
variable) goes through the same check. A value SurrealDB would reject is refused before anything
is sent, with a plain `ValueError` that says why, never re-wrapped in a `SurrealDbError`:

| Value                                                                                     | Raised                       |
| ----------------------------------------------------------------------------------------- | ---------------------------- |
| A negative duration (a `timedelta`, the SDK's `Duration`)                                 | `ValueError`                 |
| A duration of `2**64` seconds or more                                                     | `ValueError`                 |
| A zero duration, if neither the SDK nor the ORM reads one back (never with SDK 2.0.0)     | `ValueError`                 |
| A finite `Decimal` whose integer part, rounded, exceeds `2**96 - 1`                       | `ValueError`                 |
| An `int` outside the signed 64-bit range                                                  | `ValueError` (use `Decimal`) |
| A `Range` side that is neither `BoundIncluded`/`BoundExcluded` nor an open side           | `ValueError`                 |
| `id__in=Range(…)`, `id__not_in=Range(…)`, or the same on an edge's `in`/`out`             | `ValueError`                 |
| `atomic_increment(field, <a duration>)`                                                   | `TypeError` (use `merge()`)  |
| In a live filter, a negative or oversized duration, or a range bound with no literal form | `TypeError` from `live()`    |
| A `Duration` or `Range` field given a value it does not accept                            | Pydantic's `ValidationError` |

A zero duration (`timedelta(0)`, `Duration(0)`) is written, filtered and read back like any
other, on both lines: the surrealdb SDK 2.0.0 cannot read one, but the clients the ORM opens do
(see [Values the SDK cannot read](#values-the-sdk-cannot-read)); in the one case where nothing
reads it back, a live filter refuses it with `TypeError` too. Since v0.24.3 the ORM stores zero
durations, so a record it writes can hold one that a reader outside its clients cannot read: an
`AsyncSurreal` client the application builds itself, or another service on the SDK 2.0.0. Filter
"longer than zero" with `__gt=Duration(0)` or `__gte=Duration(1)`. `atomic_increment()` adds
numbers only: SurrealDB 2.x ignores `+=` with a duration, so the ORM refuses it on both lines; set
the new value with `merge()`:

```python
nonzero = await Subscription.objects().filter(trial__gte=Duration(1)).exec()  # longer than zero
await sub.merge(trial=sub.trial + timedelta(days=7))                          # not atomic_increment()
```

#### After a write that returns the row

`save()` without an id, `refresh()`, `merge()`, `upsert()`, `patch()` and the `atomic_*` helpers
apply the row the server returns to the instance. Top-level `timedelta`, `Duration` and geometry
fields (since v0.24.0, see [section 25](#25-geospatial-fields)) are converted on the way. A field
that holds a sub-model (a Pydantic model, a dataclass or a `TypedDict`, also inside `list[...]`,
`dict[..., ...]` or `X | None`) or a `timedelta` below the top level (`list[timedelta]`,
`timedelta | str`…) gets the value a read gives: the row goes through the model's validation, the
model's own validators included, and a sub-model field holds its model instance, not the row's
`dict`, with the ORM's durations, `timedelta`s, ranges and geometries inside. When the row as a
whole does not validate, each such field is validated on its own, except one that the model's own
validators could reshape (a `field_validator` on that field, a `model_validator`, a
`model_post_init`): that one, like a value that does not validate, is applied as received, so
these calls never fail on it. Other top-level `date`, `set` and `Range` fields hold the SDK's value
(a midnight `datetime`, a `list`, the SDK's `Range`) until the record is loaded again (`get()`,
`filter()`, …). Writing such an instance back is safe, sub-models included, since those values are
stored the same way, although Pydantic warns (`PydanticSerializationUnexpectedValue`) about the
top-level `date` and `set` fields while serialising them.

#### Values the SDK cannot read

The surrealdb SDK 2.0.0 cannot decode a zero duration, a SurrealDB 3.x native set (a
`TYPE set<…>` column on SurrealDB 3.3, see [Sets](#sets)) or a degenerate geometry (see
[Geometries the SDK cannot read](#geometries-the-sdk-cannot-read)): one such value fails the whole
response it is in. The clients the ORM opens read them anyway, through a hook around the SDK's own
CBOR decoder that handles only these values; everything else reads exactly as the SDK reads it:

- a zero duration reads as zero: `timedelta(0)` or `Duration(0)` in a typed field, the SDK's
  `Duration(0)` in an untyped one;
- a native set reads as a Python `set`, which `set[X]` and `frozenset[X]` fields validate, or as a
  `list` when its items are unhashable (record ids, objects, nested sets) or two of them are equal
  in Python, like `1` and `true`: every item the server holds comes back.

Written back through the ORM (`merge(v=row.v)`, or `update()`, which rewrites the whole row, the
untouched fields included), a zero duration stays a duration, but a native set is stored as an
array, like every set the ORM writes, unless its column is `TYPE set<…>` (see [Sets](#sets)), and
a degenerate geometry as a plain object (see
[Geometries the SDK cannot read](#geometries-the-sdk-cannot-read)). In an untyped field, `update()`
flattens the SDK's `Duration(0)` into an object, like any SDK value there
([#222](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/222)).

The hook is installed only while the installed SDK cannot decode these values, and only on the
ORM's own clients (`SurrealDBConnectionManager.get_client()`, `ephemeral_client()`). An
`AsyncSurreal` client the application builds itself keeps the SDK's behaviour and fails on them.
A later SDK reads zero durations and native sets itself
([#205](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/205)), and the hook then
leaves them to it.

#### Authentication

Rich values in the fields of an `AuthenticatedUserMixin` model's `signup()`, its defaults included
(`trial: timedelta = timedelta(days=7)`), and in the `variables=` of
`SurrealDBConnectionManager.signin()`/`signup()` are sent like any other write: a `date` reaches
the `SIGNUP` clause as a midnight UTC `datetime` and a `timedelta` as a duration, on both lines. A
refused value raises `ValueError` before anything is sent.

#### Coming from the full SurrealDB-ORM

Plain `datetime`, `Decimal` and `UUID` annotations work as they do there. The full ORM has no
`timedelta` or `Range` fields and its `Duration` is a string: `timedelta`, `Duration` and
`Range[T]` fields are lite additions. The full ORM also validates the rows its writes return; lite
validates the sub-model, geometry and nested `timedelta` fields of those rows and converts
top-level durations, while top-level `date`, `set` and `Range` fields keep the SDK's value (see
[After a write that returns the row](#after-a-write-that-returns-the-row)).

### 25. Geospatial fields

Since v0.24.0 a model can declare geometry fields: every write stores them as SurrealDB's native
geometries, every read returns the ORM's geometry classes, and `nearby()`, `GeoDistance` and the
`inside`, `outside` and `intersects` lookups query them, with the same results and the same
distances on SurrealDB 2.x and 3.x. The names are the full SurrealDB-ORM's (`GeoField`,
`PointField`, `nearby()`, `GeoDistance`); what differs is listed at the end of this section.

#### Declaring geometry fields

```python
from surreal_orm_lite import (
    BaseSurrealModel,
    Computed,
    GeoField,
    GeometryPoint,
    LineStringField,
    PointField,
    PolygonField,
    SurrealConfigDict,
    computed,
)


class Restaurant(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="restaurant")

    id: str | None = None
    name: str
    location: PointField = None                # GeometryPoint or None, default None: the full ORM's form
    delivery_area: GeoField["polygon"] = None  # the same form, with a kind name (see Type checkers)
    entrance: GeometryPoint | None = None      # a class annotation (required without "| None = None")


class Zone(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="zone")

    id: str | None = None
    name: str
    area: PolygonField = None
    centroid: Computed[GeometryPoint] = computed("IF area.is_polygon() THEN geo::centroid(area) END")


class Route(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="route")

    id: str | None = None
    name: str
    path: LineStringField = None
    stops: list[GeometryPoint] = []  # inside containers: the classes, not the aliases


await Zone.define_computed_fields()  # the centroid's DEFINE FIELD (see Computed geometry fields)
```

| Annotation                | Holds                      | `GeoField` kind                            |
| ------------------------- | -------------------------- | ------------------------------------------ |
| `PointField`              | `GeometryPoint`            | `"point"`                                  |
| `LineStringField`         | `GeometryLine`             | `"line"` (or `"linestring"`)               |
| `PolygonField`            | `GeometryPolygon`          | `"polygon"`                                |
| `MultiPointField`         | `GeometryMultiPoint`       | `"multipoint"`                             |
| `MultiLineStringField`    | `GeometryMultiLine`        | `"multiline"` (or `"multilinestring"`)     |
| `MultiPolygonField`       | `GeometryMultiPolygon`     | `"multipolygon"`                           |
| `GeometryCollectionField` | `GeometryCollection`       | `"collection"` (or `"geometrycollection"`) |
| `GeometryField`           | any of them (a `Geometry`) | `"feature"` (or `"geometry"`)              |

`GeoField["kind"]` and the aliases are nullable with default `None`, as in the full ORM: the field
is not required and `model_construct()` sets `None`. The kind is one case-insensitive string;
several kinds are separated by a pipe (`GeoField["point|polygon"]` holds a point or a polygon), and
an unknown kind raises `ValueError` where the class is defined. A class annotation
(`entrance: GeometryPoint`) is required unless written `GeometryPoint | None = None`, and
`Geometry` holds any kind. At nested positions (`list[GeometryPoint]`, `dict[str, GeometryPolygon]`,
a sub-model's field) use the classes: an alias inside a container validates normally, but Pydantic
warns at class definition that its default has no effect there. Geometry fields work on edge
models (`BaseSurrealEdge`) and with field aliases (`Field(default=None, alias="loc")`).
`is_geo_field()` and `get_geo_info()`, from `surreal_orm_lite.geo`, tell whether an annotation is
a geometry field and which kinds it holds (`get_geo_info(PolygonField)` is `"polygon"`).

#### Geometry values and GeoJSON

```python
from surreal_orm_lite import Geometry, GeometryLine, GeometryPolygon

times_square = GeometryPoint(-73.9855, 40.7580)  # longitude, latitude: the GeoJSON order
midtown = GeometryPolygon.parse_coordinates(
    [[(-74.0, 40.745), (-73.97, 40.745), (-73.97, 40.765), (-74.0, 40.765), (-74.0, 40.745)]]
)
broadway = GeometryLine(GeometryPoint(-73.9897, 40.74), GeometryPoint(-73.9845, 40.76))

times_square.to_geojson()  # {'type': 'Point', 'coordinates': [-73.9855, 40.758]}
Geometry.from_geojson({"type": "Point", "coordinates": [-73.9855, 40.758]}) == times_square  # True
str(times_square)          # "{type: 'Point', coordinates: [-73.9855, 40.758]}": its SurrealQL text

await Restaurant(id="pizza", name="Corner Pizza", location=GeometryPoint(-73.9877, 40.7547)).save()
await Restaurant(id="burgers", name="Park Burgers", location=(-73.9881, 40.7417)).save()  # a pair
await Restaurant(
    id="deli", name="Village Deli", location={"type": "Point", "coordinates": [-73.9969, 40.729]}
).save()  # a GeoJSON object
await Restaurant(id="bistro", name="Paris Bistro", location=GeometryPoint(2.3522, 48.8566)).save()
await Restaurant(id="truck", name="Food Truck").save()  # no location
await Zone(id="midtown", name="Midtown", area=midtown).save()
await Route(id="broadway", name="Broadway", path=broadway, stops=[times_square]).save()

deli = await Restaurant.objects().get("deli")
deli.location                             # GeometryPoint(longitude=-73.9969, latitude=40.729)
deli.model_dump()["location"]             # the same GeometryPoint
deli.model_dump(mode="json")["location"]  # {'type': 'Point', 'coordinates': [-73.9969, 40.729]}
```

The seven classes, `GeometryPoint`, `GeometryLine`, `GeometryPolygon`, `GeometryMultiPoint`,
`GeometryMultiLine`, `GeometryMultiPolygon` and `GeometryCollection`, subclass `Geometry` and have
the names, constructor signatures, attributes and methods of the SDK's classes (`surrealdb.data`):
`GeometryPoint(longitude, latitude)`, `parse_coordinates()`, `get_coordinates()`,
`geometry_points`, … They are the ORM's own classes because Pydantic's `model_dump()` turns the
SDK's dataclasses into plain dicts; the ORM converts them to the SDK's classes when it sends them,
and SurrealDB stores native geometries. They are immutable (setting an attribute raises
`AttributeError`, and an attribute holding several items is a tuple) and hashable; `copy`, `pickle`
and Pydantic's copies keep them, so a geometry works as a field default; `repr()` is the SDK's
format.

Coordinates are real numbers, stored as `float`. A `bool`, a string, `None` or a `Decimal` raises
`TypeError` (convert a `Decimal` with `float()`). A non-finite coordinate, a polygon ring of fewer
than 4 points or whose last point is not its first, and a `GeometryCollection` nested more than 32
levels deep raise `InvalidGeometryError`, which is both a `ValueError` and a
`surrealdb.InvalidGeometryError`. GeoJSON goes both ways: `to_geojson()` and `__geo_interface__`
(read by shapely and similar libraries) give the GeoJSON dict, and `Geometry.from_geojson()` reads
one (`GeometryPolygon.from_geojson()` requires a polygon); a 3D position, an unknown `type` or a
`Feature` raises `InvalidGeometryError`.

A geometry field accepts the ORM value, the SDK's, a GeoJSON object and, for points, a
`(longitude, latitude)` pair; anything else, or a refused value, raises Pydantic's
`ValidationError`. `model_dump()` returns the ORM value and `model_dump(mode="json")` its GeoJSON
object, which `model_validate_json()` reads back.

**Equality with the SDK's values is one-way**, as for `Duration`:
`GeometryPoint(1, 2) == surrealdb.data.GeometryPoint(1.0, 2.0)` is `True`, the reverse is `False`.
Keep the ORM value on the left.

#### `nearby()`

```python
near = await Restaurant.objects().nearby("location", times_square, max_distance=2_000).exec()
sorted(r.name for r in near)  # ['Corner Pizza', 'Park Burgers']: within 2 km of Times Square
```

`nearby(field, point, max_distance)` keeps the records whose field is a point within
`max_distance` metres of `point`, as SurrealDB's `geo::distance` measures it. The point is a
`GeometryPoint` (the ORM's or the SDK's), a `(longitude, latitude)` pair or a GeoJSON `Point`; it
and the distance are bound as query variables, never written into the statement. A `.is_point()`
guard leaves out the records whose field is not a point (missing, `NULL`, a polygon, a string, a
GeoJSON object), on both lines. Several calls are AND-ed, and the condition joins `filter()`
wherever that applies: `exec()`, `first()`, `get()` without an id, `count()` and the other
aggregations, `values()`/`annotate()` groups, `bulk_update()`, `bulk_delete()`, `patch()`, and
`live()`/`watch()`. `all()` ignores it, as it ignores `filter()`, and `changes()` refuses it. A
field the model declares as a geometry that holds no point (`PolygonField`, `GeoField["line"]`)
raises `ValueError`, and so does a negative or non-finite distance.

#### Distance: GeoDistance

```python
from surreal_orm_lite import GeoDistance

closest = await (
    Restaurant.objects()
    .nearby("location", times_square, max_distance=5_000)
    .annotate(dist=GeoDistance("location", times_square))
    .order_by("dist")
    .limit(3)
    .exec()
)
[(r.name, round(r.dist)) for r in closest]  # [('Corner Pizza', 411), ('Park Burgers', 1826), ('Village Deli', 3365)]

farthest_first = await Restaurant.objects().annotate(dist=GeoDistance("location", times_square)).order_by("-dist").exec()
farthest_first[0].name                             # 'Paris Bistro'
farthest_first[-1].name, farthest_first[-1].dist  # ('Food Truck', None): no location, last
```

`annotate(<alias>=GeoDistance(field, point))` gives each instance the distance as an attribute
named after the alias (`r.dist`): metres as a `float`, or `None` when the record's field is not a
point. `order_by("dist")` sorts the nearest first, after the records without a point;
`order_by("-dist")` sorts the farthest first and the records without a point last. The attribute is
never written back: `model_dump()`, `update()` and `upsert()` ignore it. Rows that do not validate
(a `select()` that omits a required field) come back as dicts carrying the alias.

The alias is a projection, not a column: `filter(dist__lt=500)` filters a stored column named
`dist`, so bound the distance with `nearby()`. Choose an alias no record stores: on an
`extra="allow"` model, a stored, undeclared column of that name is replaced by the distance in the
results and missing from the instance, so a later `update()` or `upsert()` removes it. An alias
that names a declared field or its column, `id`, `in` or `out`, starts with `_` or `model_`, or
names a model attribute (`save`, `objects`, …) raises `ValueError`. A `GeoDistance` cannot be
combined with aggregations (`Count()`, …) or `values()`, and `live()`, `watch()` and `changes()`
refuse `annotate()`.

#### Spatial lookups

```python
inside = await Restaurant.objects().filter(location__inside=midtown).exec()    # Corner Pizza
outside = await Restaurant.objects().filter(location__outside=midtown).exec()  # the four others
zones = await Zone.objects().filter(area__contains=times_square).exec()         # Midtown
crossing = await Route.objects().filter(path__intersects=midtown).exec()        # Broadway
```

`inside`, `outside` and `intersects` compare a field with a geometry through SurrealQL's `INSIDE`,
`OUTSIDE` and `INTERSECTS`; the existing `contains` lookup tells whether a polygon field contains
a point (`area__contains=times_square`). The value of a spatial lookup is an ORM or SDK geometry,
a GeoJSON object or a `Var`, bound as one value; the lookups work in `filter()`, in `Q` objects
(`~Q(...)` included) and in live queries. A list, a tuple, a string or `None` raises `ValueError`
(use `__in` for membership in a list), and so does a spatial lookup on the record id or on an
edge's `in`/`out`.

`outside` keeps SurrealQL's meaning: it is true for every record whose field is not inside the
geometry, including a record whose field is missing, `NULL`, a string or a GeoJSON object stored as
a plain object. That is why Food Truck, which has no location, is among `outside`;
`~Q(location__inside=midtown)` returns the same records.

#### Geometries in live queries

```python
import asyncio

async with Restaurant.objects().nearby("location", times_square, 2_000).live() as stream:
    await Restaurant(id="crepes", name="Seine Creperie", location=GeometryPoint(2.35, 48.86)).save()
    await Restaurant(id="cart", name="Hot Dog Cart", location=GeometryPoint(-73.9851, 40.7589)).save()
    async with asyncio.timeout(5):
        async for event in stream:
            print(event.instance.name)  # Hot Dog Cart: the Paris write is not notified
            break
```

`nearby()`, the spatial lookups and geometry filter values work in `live()` and `watch()`
([section 21](#21-live-queries-real-time-notifications)), where the ORM writes each value into the
statement as a GeoJSON object literal (`{type: 'Point', coordinates: [-73.9855, 40.758]}`), with
the same notifications on both lines. Typed live streams and change feeds
([section 22](#22-change-feeds-changes)) deliver instances holding the ORM's classes.

#### Filter values, keyword writes and assignments

```python
same_spot = await Restaurant.objects().filter(location=GeometryPoint(-73.9877, 40.7547)).exec()  # Corner Pizza

pizza = await Restaurant.objects().get("pizza")
await pizza.merge(location=GeometryPoint(-73.9876, 40.7548))  # a geometry object, not a pair
pizza.entrance = GeometryPoint(-73.9877, 40.7546)             # the same for an assignment
await pizza.update()
```

Give geometry objects wherever the field does not validate the value. The values of the other
lookups (`exact`, `contains`, …) are bound as given, and keyword writes (`merge()`, `bulk_update()`, the `atomic_*`
helpers) are not validated against the field
([#220](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/220),
[#221](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/221)): a
`(longitude, latitude)` pair or a GeoJSON dict there is sent as an array or an object, which
matches nothing or is stored as such. Models do not validate assignments, so
`pizza.entrance = (-73.9877, 40.7546)` keeps the tuple, which Pydantic warns about when it
serialises it and which is stored as an array. A geometry filter value matches a stored geometry
exactly.

#### Decimal coordinates of SDK geometries

The ORM's classes refuse a `Decimal` coordinate, and so does a geometry field given an SDK
geometry that holds one. An SDK geometry (`surrealdb.data.GeometryPoint`, …) given as a filter
value, a `raw_query()` variable or a keyword write is only checked for what SurrealDB refuses
(below), and SurrealDB converts its `Decimal` coordinates to floats. A `Decimal` coordinate written with an exponent goes out in
fixed-point notation, as a top-level `Decimal` does (see [Precision and `Decimal`](#precision-and-decimal)):
before v0.24.0, SurrealDB 2.x refused `Decimal("1E-7")` there, and both lines stored a long one
(`Decimal("1.234567890123456789012345678901E-7")`) with a wrong value; both lines now store the
value. An SDK geometry SurrealDB would refuse raises `ValueError` before anything is sent: a line
of fewer than 2 points, a polygon without a ring, a ring of fewer than 4 points once closed, a
coordinate that is a `bool`, a string, `None` or an integer beyond 64 bits, or a member that is not
a geometry of the expected kind.

#### Computed geometry fields

```python
await Zone(id="unmapped", name="Unmapped").save()

(await Zone.objects().get("midtown")).centroid   # a GeometryPoint at (-73.985, 40.755), computed by SurrealDB
(await Zone.objects().get("unmapped")).centroid  # None: the guard skips a zone without an area
```

`Zone.centroid` is a computed field ([section 16](#16-computed-fields-computed--define-field--value))
read back as an ORM `GeometryPoint`. Guard geometry functions with a type check: `geo::centroid()`
raises on a record without a polygon, so `computed("geo::centroid(area)")` would make every write
of a zone without an area fail, while `IF area.is_polygon() THEN … END` leaves its centroid
`None`.

#### Migrating stored GeoJSON

```python
for restaurant in await Restaurant.objects().all():
    await restaurant.update()  # a location read from a GeoJSON object is written back as a native point
```

SurrealDB keeps a GeoJSON dict as a plain object, not a geometry: the full SurrealDB-ORM stores its
geometry fields that way, and so does any client that writes GeoJSON dicts. `nearby()` skips such
objects, `geo::*` functions and `geometry<…>` columns refuse them, and no SurrealQL cast turns them
into geometries. A geometry field reads them (a GeoJSON object validates, in a read and in the row
a write returns), and `update()` writes the instance back with a native geometry, so loading and
updating every record migrates a table.

#### Untyped fields

A field declared `Any` or `dict`, or an undeclared column of an `extra="allow"` model, is not a
geometry field. A GeoJSON dict written there stays a plain object (see
[Migrating stored GeoJSON](#migrating-stored-geojson)); an ORM geometry written there is stored
as a native geometry but reads back as the SDK's class (`surrealdb.data.GeometryPoint`, …), which
a later write of the instance stores as a plain object
([#222](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/222)). Declare geometry fields
with the annotations above. The results of `raw_query()`, `query()` and `call_function()` are
untyped too: they hold the SDK's classes, which compare equal to the ORM's with the ORM value on
the left.

#### Type checkers

mypy sees each alias as its class or `None` (`PointField` is `GeometryPoint | None`). Without the
Pydantic mypy plugin, mypy only knows a default written in the class body: write
`location: PointField = None` (or `= Field(default=None, alias="loc")`) in code that builds the
model without that field; at runtime the default is there either way. Type checkers do not
understand `GeoField["point"]` (mypy: `"GeoField" expects no type arguments`), as in the full ORM:
in type-checked code, use the aliases or the classes.

#### Geometries the SDK cannot read

A degenerate geometry written by another client (a line of fewer than 2 points, alone or inside
another geometry, or a polygon ring of fewer than 4 points) cannot be decoded by the surrealdb SDK
2.0.0, nor by its 3.0 pre-releases. The clients the ORM opens read it as its GeoJSON dict, such as
`{"type": "LineString", "coordinates": [[1.0, 2.0]]}`; a multi-geometry or a collection holding
one reads as GeoJSON as a whole (see
[Values the SDK cannot read](#values-the-sdk-cannot-read) for where this applies):

- an untyped field (`Any`, `dict`), `raw_query()` and live notifications give that dict;
- a geometry field refuses it with a `ValidationError` naming the field, since the ORM's geometry
  classes refuse degenerate values: `get()` raises it, and `filter().exec()` and `raw_query()`
  return such rows as dicts, as for any row that does not validate.

The ORM never writes one: its classes refuse such values, and so does the check of the SDK
geometries it sends (see [Decimal coordinates of SDK geometries](#decimal-coordinates-of-sdk-geometries)).
Written back, the dict an untyped field holds is stored as a plain object, not a geometry, on both
lines, like any GeoJSON dict (see [Migrating stored GeoJSON](#migrating-stored-geojson)):
`merge(v=row.v)` does it, and so does `update()` after a change to another field, since it
rewrites the whole row.

#### Coming from the full SurrealDB-ORM

- **Field values are geometry objects, not GeoJSON dicts.** `location.to_geojson()` gives the
  dict, `model_dump(mode="json")` writes GeoJSON, and a GeoJSON dict (or a `(longitude, latitude)`
  pair for points) is accepted as input, so full-ORM-shaped data keeps loading. SurrealDB keeps a
  GeoJSON dict as a plain object, which `geo::*` functions and `geometry<…>` columns refuse; lite
  stores a native geometry (see [Migrating stored GeoJSON](#migrating-stored-geojson)).
- **`get_geo_info()` returns SurrealDB's kind names**: `"line"` for `LineStringField` (the full
  ORM: `"linestring"`), `"point|polygon"` for a union; `GeoField["linestring"]` is still accepted.
- **`nearby()`** keeps its name and argument order. The point and the distance are bound, never
  written into the statement; a `.is_point()` guard skips the records whose field is not a point;
  several calls are AND-ed (the full ORM keeps the last); a field declared as a geometry that holds
  no point raises `ValueError`; the point may also be an ORM or SDK `GeometryPoint` or a GeoJSON
  point.
- **`GeoDistance`**: its value reaches each instance as an attribute named after the alias (the
  full ORM computes it but does not attach it), `None` when the record's field is not a point;
  `.point` is an ORM `GeometryPoint` (the full ORM keeps the tuple); the aliases that would hide a
  column, the id, an edge endpoint or a model attribute are refused; it cannot be combined with
  aggregations or `values()`.
- **Lite additions**: the eight value classes, `MultiLineStringField`, `MultiPolygonField`,
  `GeometryCollectionField`, `GeometryField`, `InvalidGeometryError` and the `inside`, `outside`
  and `intersects` lookups.
- **Aliases inside containers** (`list[PointField]`) make Pydantic warn at class definition; use
  the classes there (`list[GeometryPoint]`).
- **Imports**: `from surreal_orm_lite import GeoField, PointField, GeoDistance, …` (the full ORM:
  `surreal_orm`, `surreal_orm.fields`, `surreal_orm.geo`); `is_geo_field` and `get_geo_info` come
  from `surreal_orm_lite.geo` (the full ORM: `surreal_orm.fields`). There is no
  `surreal_orm_lite.fields` module.

### 26. Embedded engine and test fixtures

Since v0.25.0 the ORM also runs on the official SDK's **in-process** SurrealDB engine: no server
and no Docker, the database lives inside your Python process. It suits tests, notebooks and
small tools, and the same models and queries run unchanged against a server. The
`surreal_orm_lite.testing` module adds the test helpers: `embedded_database()` (an isolated
database per block) and the full SurrealDB-ORM's `SurrealFixture` / `@fixture`.

#### In-process databases

```python
from surreal_orm_lite import BaseSurrealModel, SurrealDBConnectionManager

SurrealDBConnectionManager.set_connection(url="mem://", namespace="app", database="app")

class Note(BaseSurrealModel):
    id: str | None = None
    title: str
    done: bool = False

await Note(id="docs", title="Write the docs").save()
assert await Note.objects().count() == 1
assert SurrealDBConnectionManager.is_embedded()
```

Same models, no server: the first ORM call opens the engine. `user` and `password` are optional
on an embedded URL and **never sent** — the engine has no system user on a fresh database, and
its anonymous session is the in-process owner (see
[Identity on the embedded engine](#identity-on-the-embedded-engine)). They are kept, so one
configuration switches between a server and `mem://` by its URL alone.
`SurrealDBConnectionManager.is_embedded()` tells code that must run on both kinds of connection
which one is configured.

| Embedded URL                   | surreal-orm-lite (official SDK 2.0.0)                                                    | Full SurrealDB-ORM       |
| ------------------------------ | ---------------------------------------------------------------------------------------- | ------------------------ |
| `mem://`, `memory://`          | ✅ in memory, gone when the connection closes                                            | ❌ (HTTP/WebSocket only) |
| `surrealkv://<path>`           | ✅ on disk (SurrealKV)                                                                   | ❌                       |
| `surrealkv+versioned://<path>` | ✅ on disk; behaves as `surrealkv://` until time travel (v0.26.0)                        | ❌                       |
| `file://<path>`                | ✅ a **SurrealKV** store, not the RocksDB store `file://` means for the SurrealDB server | ❌                       |
| `rocksdb://<path>`             | ❌ not yet supported by the official SDK (#233): the ORM raises the SDK's refusal        | ❌                       |

A bare `memory` (no `://`) is refused by SDK 2.0.0 and accepted by the SDK 3.0 line; write
`memory://`. The engine is SurrealDB core **2.3.10**, bundled with SDK 2.0.0 — not the 2.x and
3.x server lines the ORM is tested against (see below). surreal-orm-lite keeps no list of
embedded schemes: the SDK decides, so `rocksdb://` will work with no ORM change once a released
SDK accepts it ([#233](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/233)).

A URL the installed SDK refuses is accepted by `set_connection()` and reported by the first ORM
call, before anything is opened, with a `SurrealDbConnectionError` that quotes the SDK:

```python
from surreal_orm_lite import SurrealDbConnectionError

SurrealDBConnectionManager.set_connection(url="rocksdb://app.db", namespace="app", database="app")
try:
    await Note.objects().count()
except SurrealDbConnectionError as error:
    print(error)
# The installed surrealdb SDK cannot open 'rocksdb://app.db': 'rocksdb' is not a valid UrlScheme.
# The URL schemes it accepts are: file, http, https, mem, memory, surrealkv, surrealkv+versioned,
# ws, wss. Embedded RocksDB (rocksdb://) is not supported by the official SDK yet: see
# https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/233.

# Back to mem://: the same in-memory database as above, which only a close discards.
SurrealDBConnectionManager.set_connection(url="mem://", namespace="app", database="app")
```

#### One engine per process

A server client is bound to the event loop it connected on, so the manager keeps one per loop
(see [Connections and event loops](#connections-and-event-loops)). The engine is not: the
manager keeps **one engine for the whole process**, opened on the first call and handed to every
event loop and thread, so they all see the same database. Concurrent first calls open exactly
one engine.

```python
import asyncio

async def count_notes() -> int:
    return await Note.objects().count()

# Another event loop, on another thread: the same engine, the same data.
assert await asyncio.to_thread(lambda: asyncio.run(count_notes())) == 1

await SurrealDBConnectionManager.close_connection()  # the in-memory database is discarded
assert await Note.objects().count() == 0              # a new, empty one
```

- **Namespace and database** are a selection on that engine. After a change (`set_connection()`,
  `set_namespace()`, `set_database()`), the next call resets the shared session on the same
  engine — nothing is lost — and selects the new pair; a stored session ends (see
  [Identity](#identity-on-the-embedded-engine)). A new **URL** closes the old engine and opens
  the new one.
- **Closing** — `close_connection()` (from any loop), `close_all_connections()`,
  `unset_connection()`, `reconnect()` and the exit of `async with SurrealDBConnectionManager():`
  — closes the engine: a `mem://` database is discarded with it, and the next call opens a new,
  empty one; a `surrealkv://` or `file://` database reopens with its data.
- **`validate_connection()`** opens the engine if needed and never discards it, so the
  `set_*(..., reconnect=True)` setters keep a `mem://` database.
- **Server clients are untouched** while an embedded URL is configured, and are used again when
  a server URL is configured back. `close_connection()`, `reconnect()` and the exit of
  `async with SurrealDBConnectionManager():` close only the engine then: a server client keeps
  its live queries and its identity, even one signed in with `store=False`.
  `close_all_connections()` and `unset_connection()` close the server clients too.

With the surrealdb SDK 2.x, closing does not stop an in-process engine: a client you obtained
before `close_connection()` (or inside an `embedded_database()` block) is still a running
database. Do not use it after the close, and drop it (`del client`) before reopening the same
`surrealkv://` path: two engines on one path miss each other's writes. Never open one path from
two processes.

#### What the embedded engine does not do

Each of these raises a clear ORM error up front, never an SDK `AttributeError` or an engine
error:

- **Live queries** (`live()`, `watch()`, `subscribe_live()`): the engine has no notification
  channel. Read the table's change feed instead (`Model.objects().changes()`,
  [section 22](#22-change-feeds-changes)), which works from now and from a cursor.
- **`changes(since=<datetime>)`**: the SDK 2.x engine keeps no timestamps for change-feed
  positions; pass a cursor (`ChangeModelStream.cursor`) or `since=None`.
- **Model auth needs `bind=True`**: the engine has a single session per database, so
  `AuthenticatedUserMixin` calls with `bind=False` (a session of their own) and
  `ephemeral_client()` are refused.
- **`rocksdb://`**: refused by the SDK
  ([#233](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/233)), see above.

And two differences that raise nothing:

- **Transactions are buffered**: the SDK refuses interactive transactions on the engine, so
  `transaction()` picks the buffered strategy, as on HTTP ([section 11](#11-transactions-atomic-all-or-nothing)).
- **Parse errors are a bare `RuntimeError`** with SDK 2.0.0 (a server raises the SDK's error
  classes). The DDL helpers (`define_computed_fields()`, `define_access()`) raise their usual
  `SurrealDbError`.

```python
from surreal_orm_lite import SurrealDbError

try:
    async with Note.objects().live() as stream:
        pass
except SurrealDbError as error:
    print(error)
# Live queries need a WebSocket server connection (ws:// or wss://): the embedded engine ('mem://')
# has no notification channel to deliver them. Read the table's change feed with
# Model.objects().changes() instead, or connect to a server.
```

#### Its SurrealQL is the 2.x line

SDK 2.0.0 bundles the SurrealDB **2.3.10** engine: write 2.x SurrealQL in raw queries and
computed fields (`type::is::string`, not 3.x's `type::is_string`; no `DEFINE FIELD … COMPUTED`),
and expect the 2.x column of the
[behaviour table](#orm-behaviour-surrealdb-2x-vs-3x) — a table that does not exist reads as
`[]`. `client.version()` reports the SDK's version (`surrealdb-2.0.0`), not the engine's. The SDK
3.0 line bundles a 3.x engine
([#205](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/205)).

#### Identity on the embedded engine

- The **anonymous session is the in-process owner**: full access (DDL, writes), and
  `info()` returns `None`.
- `signin()` / `signup()` / `authenticate()` ([section 18](#18-authentication-signin--signup--info--invalidate))
  work with the engine's record access methods and `DEFINE USER`s, and record users and table
  `PERMISSIONS` apply as on a server. A sign-in changes the session of the **whole process**:
  every event loop and thread shares the engine's single session.
- `invalidate()` returns to the in-process owner: a configured user, if any, is never signed in.
- A namespace/database change returns the session to the owner and **ends** the stored session:
  a token belongs to the database it was issued for, and replaying it would move the session
  back there.
- Model auth with `bind=True` ([section 19](#19-model-level-authentication-authenticatedusermixin))
  signs that single session in, one call at a time: concurrent `bind=True` calls, and the
  connection-level `signin()` / `signup()` / `authenticate()` / `invalidate()` that hold the
  same session, are serialised (each `bind=True` call reads back its own record and adopts its
  own token) and the last one wins. A failed `bind=True` puts the previous session back.

```python
assert await SurrealDBConnectionManager.info() is None  # anonymous: the in-process owner
```

#### Test fixtures

`embedded_database()` runs a block against an isolated in-memory database (`mem://` by default,
namespace and database `test`), then puts the previous connection back: its settings, stored
session and engine. Blocks nest. It yields the block's SDK client for raw SurrealQL and DDL,
valid **inside the block only**. Pass `surrealkv://<path>` for an on-disk database (with pytest,
under `tmp_path`).

`SurrealFixture` and `@fixture` declare model instances as test data: `load()` saves a fresh
copy of each, in declaration order, yields them, and deletes them on exit. They work on any
connection, a server or the engine.

```python
from surreal_orm_lite.testing import SurrealFixture, embedded_database, fixture

class User(BaseSurrealModel):
    id: str | None = None
    name: str
    role: str = "player"

@fixture
class UserFixtures(SurrealFixture):
    alice = User(name="Alice", role="admin")
    bob = User(id="bob", name="Bob", role="player")  # an explicit id is kept

async with embedded_database():  # a fresh, empty in-memory database
    async with UserFixtures.load() as fixtures:
        assert fixtures.alice.get_id() is not None
        admins = await User.objects().filter(role="admin").exec()
        assert [admin.name for admin in admins] == ["Alice"]
    assert await User.objects().count() == 0  # alice and bob are deleted
# the in-memory database is gone; the previous connection is back
```

With pytest and pytest-asyncio, one fresh in-memory database per test:

```python
import pytest_asyncio
from surreal_orm_lite.testing import embedded_database

@pytest_asyncio.fixture
async def surreal_db():
    async with embedded_database():
        yield

@pytest_asyncio.fixture
async def users(surreal_db):
    async with UserFixtures.load() as fixtures:
        yield fixtures
```

`surreal_db` yields nothing, so no test keeps a client past its block; a test that needs raw
SurrealQL calls `await SurrealDBConnectionManager.get_client()`. It works whatever loop scope
pytest-asyncio gives the fixture and the test: there is one engine per process.

A fresh engine raises a retryable write conflict when several writes run concurrently on a table
that does not exist yet: define the tables your test writes to concurrently first
(`DEFINE TABLE`), or run those writes in a function decorated with `@retry_on_conflict()`
([section 14](#14-optimistic-concurrency-retry_on_conflict)).

```python
import asyncio

async with embedded_database() as client:
    await client.query("DEFINE TABLE User SCHEMALESS;")  # before the concurrent first writes
    await asyncio.gather(*(User(name=f"user {n}").save() for n in range(20)))
    assert await User.objects().count() == 20
```

#### Coming from the full SurrealDB-ORM

- **The full ORM has no embedded engine**: it connects over HTTP/WebSocket only (see the
  [embedded URL table](#in-process-databases)).
- **`surreal_orm_lite.testing` mirrors `surreal_orm.testing`** for `SurrealFixture` and
  `fixture`: the same names, the same declarations, `load()` as an async context manager.
- **`load()` keeps an explicit `id`** where the full ORM resets it to `None`, so a fixture can
  name its records (and an edge its endpoints). It also **cleans up** when a save fails or is
  cancelled mid-load: the instances already saved are deleted before the error propagates.
- **`@fixture` raises `TypeError`** on a class that is not a `SurrealFixture` subclass (the full
  ORM accepts any class).
- **`load()` is typed** to yield the fixture class, so `fixtures.alice` has its model's type
  (the full ORM yields `Any`).
- **Lite additions**: `SurrealDBConnectionManager.is_embedded()` and `embedded_database()`.
- **`ModelFactory` and `Faker`** come in v0.39.0.

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

### Custom table name (`table_name`)

```python
from surreal_orm_lite import BaseSurrealModel, SurrealConfigDict

class OrderLine(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="order_line")

    id: str | None = None
    sku: str
    quantity: int = 1

line = await OrderLine(sku="A-1", quantity=2).save()          # stored in order_line
lines = await OrderLine.objects().filter(sku="A-1").exec()
```

By default a model's table is its class name. `table_name` (v0.22.0, the full SurrealDB-ORM's
key) overrides it, and every path follows: writes and reads, every QuerySet clause, live queries
and change feeds, `relate()` targets, and the DDL the ORM generates (`computed_field_ddl()`,
`access_ddl()`). A subclass inherits it unless it sets its own. The name is written into
SurrealQL, so it is checked when the class is defined: a value that is not an identifier
(letters, digits and underscores, not starting with a digit) raises `ValueError`, a non-string
`TypeError`. Every `SurrealConfigDict` key is optional for type checkers since v0.22.0, so
`SurrealConfigDict(table_name="order_line")` alone type-checks.

> **Deprecated:** the undocumented `_table_name: ClassVar[str] = "order_line"` class attribute
> still works and emits a `DeprecationWarning` when the class is defined; it will be removed in a
> future minor. It now applies to every path: before v0.22.0 only the `objects()` QuerySet used
> it, so `save()` and the other instance methods wrote to the class-name table. When both are
> set, `table_name` wins.

### Context Manager

```python
async with SurrealDBConnectionManager():
    users = await User.objects().all()
# Connection automatically closed
```

### Connections and event loops

The manager caches **one client per event loop**. A SurrealDB WebSocket client is bound to
the loop it connected on, so reusing it from another loop is not merely wrong — it fails with
`got Future attached to a different loop`, or hangs. Keying the cache by loop means each one
gets its own connection:

```python
# Each asyncio.run() creates and closes its own loop. Both calls work.
asyncio.run(work())
asyncio.run(work())

# Two loops alive at once (threads, multi-loop servers) each keep their own client;
# neither evicts the other.
```

An entry whose loop has been closed is dropped the next time a client is requested. The stale
client is not closed — that would have to be awaited on a loop that is already gone — and its
socket is released when the loop is finalised.

- `close_connection()` closes **the running loop's** client only.
- `close_all_connections()` tears down every cached client (used by `unset_connection()`).
- `is_connected()` answers for the loop asking; called outside a loop, it reports whether any
  loop still holds a client.

Long-lived single-loop applications are unaffected: one loop, one connection, as before.

An embedded URL is the exception: the engine runs in this process and is not bound to a loop, so
the manager keeps one client for the whole process ([section 26](#26-embedded-engine-and-test-fixtures)).
While it is configured, `close_connection()` closes that client only, not the loop's server client.

---

## Compatibility

As of v0.7.0, Surreal ORM Lite uses `surrealdb[pydantic]>=2.0.0,<3.0.0` (SurrealDB 3.x protocol) and is tested against both major SurrealDB release lines.

**Compatibility advantage over the full ORM**: ORM-lite runs on **both** SurrealDB **2.x (2.6 / 2.7) and 3.x (3.2 / 3.3)**, while the full [SurrealDB-ORM](https://github.com/EulogySnowfall/SurrealDB-ORM/) (custom SDK) targets **3.x only**. Lite stays usable on existing 2.6.x deployments without forcing a server upgrade.

| SurrealDB Version                                  | SDK Version | Status                               |
| -------------------------------------------------- | ----------- | ------------------------------------ |
| 3.3.4                                              | 2.0         | ✅ Tested                            |
| 2.7.0                                              | 2.0         | ✅ Tested                            |
| Embedded engine (SDK 2.0.0: SurrealDB 2.3.10 core) | 2.0         | ✅ Tested (in-process)               |
| 3.2.4                                              | 2.0         | ✅ Compatible (tested up to v0.19.0) |
| 2.6.5                                              | 2.0         | ✅ Compatible (tested up to v0.19.0) |
| 3.3.x / 3.2.x / 2.7.x / 2.6.x                      | 2.0         | ✅ Compatible                        |
| 3.1.5                                              | 2.0         | ⚠️ Backward-compat only              |
| < 2.6 or > 3.3                                     | —           | ⚠️ Not guaranteed                    |

### ORM behaviour: SurrealDB 2.x vs 3.x

Surreal ORM Lite runs on both lines; some capabilities differ because they rely on server
features introduced in SurrealDB 3.x. On 2.x the ORM degrades gracefully. Capabilities not
listed behave the same on both lines. The 2.x column was measured on 2.6.5 and holds unchanged
on 2.7.0. The 3.x column was measured on 3.2.4 and holds on 3.3.0, except where a row says
otherwise. The full suite and every divergence below were re-verified on both new versions.
Rows added in v0.20.0 were measured directly on 2.7.0 and 3.3.0; rows added in v0.21.0 on 2.7.0 and 3.3.2, re-verified on 3.3.3, with 3.1.5 where it differs; rows added in v0.22.0 on 2.7.0 and 3.3.3, with 3.1.5 where it differs; rows added in v0.23.0 on 2.7.0 and 3.3.4, with 3.1.5 where it differs; rows added in v0.24.0 on 2.7.0 and 3.3.4, identical on 3.1.5.
The SDK's in-process engine has a table of its own: [ORM behaviour: the embedded engine](#orm-behaviour-the-embedded-engine).

| ORM capability                                                                                                                         | SurrealDB 2.6.x / 2.7.x                                                                                                                                                       | SurrealDB 3.2.x / 3.3.x                                                                                                                                                             | Since                                |
| -------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------ |
| Transaction strategy auto-selected by `transaction()`                                                                                  | buffered batch (`BEGIN…COMMIT`)                                                                                                                                               | native interactive on WebSocket                                                                                                                                                     | v0.9.0                               |
| Reads inside a transaction (`objects(tx=)`)                                                                                            | raise (buffered cannot read)                                                                                                                                                  | see uncommitted writes                                                                                                                                                              | v0.9.0                               |
| `save(tx=)` with an auto-generated id                                                                                                  | raises — explicit id required                                                                                                                                                 | supported                                                                                                                                                                           | v0.9.0                               |
| `refresh(tx=)` inside a transaction                                                                                                    | raises                                                                                                                                                                        | works                                                                                                                                                                               | v0.9.0                               |
| `bulk_update` / `bulk_delete` / `QuerySet.patch` row count inside a tx                                                                 | returns `0` (not knowable pre-commit)                                                                                                                                         | real count                                                                                                                                                                          | v0.9.0                               |
| "Already exists" error on create                                                                                                       | normalised to `SurrealDbError`                                                                                                                                                | normalised to `SurrealDbError`                                                                                                                                                      | v0.7.0                               |
| Cleanup on a missing target (`delete_table`)                                                                                           | native no-op                                                                                                                                                                  | ORM makes it a silent no-op                                                                                                                                                         | v0.7.0                               |
| Aggregation over an empty set (`NaN` / `±inf`)                                                                                         | returns `0.0` / `None`                                                                                                                                                        | ORM normalises to `0.0` / `None`                                                                                                                                                    | v0.7.0                               |
| Namespace/db selection (`use()` ordering)                                                                                              | lenient (auto-creates)                                                                                                                                                        | strict — ORM signs in before `use()`                                                                                                                                                | v0.7.0                               |
| Writing a field a `SCHEMAFULL` table does not define (`CREATE`, `INSERT`, `INSERT RELATION`)                                           | silently dropped: the record is stored without it                                                                                                                             | rejected ("Found field …, but no such field exists"): nothing is stored                                                                                                             | v0.7.0                               |
| `upsert()` / `update_or_create()` / `get_or_create()`                                                                                  | same on both lines                                                                                                                                                            | same on both lines                                                                                                                                                                  | v0.10.0                              |
| `patch()` / `atomic_append` / `atomic_set_add` / `atomic_remove` / `atomic_increment`                                                  | same on both lines (portable `array::*` fns chosen over divergent `+=`/`-=`)                                                                                                  | same on both lines                                                                                                                                                                  | v0.11.0                              |
| `retry_on_conflict` / `SurrealDbConflictError` (retryable conflict)                                                                    | same type + decorator; conflicts rarer (engine serialises more), except on `CHANGEFEED` tables (section 22)                                                                   | same type + decorator; conflicts are the normal optimistic-MVCC failure                                                                                                             | v0.12.0                              |
| `SurrealFunc` / `server_values=` / `extra_vars=` on `save`/`merge`                                                                     | same on both lines (compiled to portable `CREATE`/`UPDATE … SET`)                                                                                                             | same on both lines                                                                                                                                                                  | v0.13.0                              |
| Shipped function-name enums (`SurrealTimeFunction`, `SurrealCryptoFunction`, …)                                                        | every catalogued member verified on 2.7.0                                                                                                                                     | every catalogued member verified on 3.3.4                                                                                                                                           | v0.13.0                              |
| `server_values` inside a transaction — when the instance sees the computed value                                                       | only after commit (buffered; `refresh()` to read it)                                                                                                                          | immediately (interactive returns the row)                                                                                                                                           | v0.13.0                              |
| `merge(server_values=)` on a missing record / never-created table                                                                      | server returns no rows → ORM raises `SurrealDbError`                                                                                                                          | server raises `NotFound` for a missing table → ORM raises the same error                                                                                                            | v0.13.0                              |
| Computed fields (`Computed[...]` → `DEFINE FIELD … VALUE`)                                                                             | same on both lines (DDL, recompute triggers, precedence over client data)                                                                                                     | same on both lines                                                                                                                                                                  | v0.14.0                              |
| DDL run inside a transaction, then rolled back                                                                                         | definition rolled back with the transaction                                                                                                                                   | same on both lines                                                                                                                                                                  | v0.14.0                              |
| Invalid computed-field expression — raw SDK exception                                                                                  | `InternalError`                                                                                                                                                               | `ValidationError`                                                                                                                                                                   | v0.14.0                              |
| Invalid computed-field expression — through the ORM                                                                                    | `SurrealDbError` (normalised)                                                                                                                                                 | `SurrealDbError` (normalised)                                                                                                                                                       | v0.14.0                              |
| Issue #156 correctness fixes (`Var`/`$$`, `first()`, `*_or_create` strictness, `created` on upsert, quoted ids, one-hop `get_related`) | same on both lines (each fix reproduced and verified on 2.6.5)                                                                                                                | same on both lines (verified on 3.2.4)                                                                                                                                              | v0.14.3                              |
| Record-id lookups (`filter(id=…)`, `id__in`, `get(…)`, `*_or_create(id=…)`) coerced to `RecordID`                                      | same on both lines (int/str typing verified on 2.6.5)                                                                                                                         | same on both lines (verified on 3.2.4)                                                                                                                                              | v0.14.4                              |
| Per-event-loop client cache (`get_client`, `close_connection`, `close_all_connections`)                                                | same on both lines (loop binding is an asyncio/SDK property, not a server one)                                                                                                | same on both lines (verified on 3.2.4)                                                                                                                                              | v0.14.5                              |
| `call_function()` — the call itself (`args`, `params`, `return_type`, nested `fn::a::b`)                                               | same on both lines (bare call form chosen so it is portable)                                                                                                                  | same on both lines (verified on 3.2.4)                                                                                                                                              | v0.15.0                              |
| `call_function(tx=)` — return value                                                                                                    | `None` (buffered: queued until commit)                                                                                                                                        | the function's value (interactive returns it immediately)                                                                                                                           | v0.15.0                              |
| `call_function(tx=, return_type=)`                                                                                                     | raises `ValueError` (no value to coerce yet)                                                                                                                                  | coerces the returned value                                                                                                                                                          | v0.15.0                              |
| Missing function called inside a transaction                                                                                           | surfaces at COMMIT as `SurrealDbError` (buffered: the call is only queued)                                                                                                    | raises `SurrealDbNotFoundError` at call time                                                                                                                                        | v0.15.0                              |
| Declared parameter name that collides with a reserved word, as echoed by `INFO FOR DB`                                                 | quoted: `` $`by` `` — parser accepts it                                                                                                                                       | bare: `$by`                                                                                                                                                                         | v0.15.0                              |
| Auth methods (`signin`, `signup`, `authenticate`, `invalidate`, `info`)                                                                | same on both lines (native SDK primitives, verified on 2.6.5)                                                                                                                 | same on both lines (verified on 3.2.4)                                                                                                                                              | v0.16.0                              |
| Auth failure — raw SDK exception for a wrong password                                                                                  | `InternalError`                                                                                                                                                               | `NotFoundError`                                                                                                                                                                     | v0.16.0                              |
| Auth failure — through the ORM                                                                                                         | `SurrealDbAuthenticationError` (normalised)                                                                                                                                   | `SurrealDbAuthenticationError` (normalised)                                                                                                                                         | v0.16.0                              |
| `DEFINE ACCESS … WITH REFRESH` and `AuthTokens.refresh`                                                                                | not supported — the clause does not parse; `refresh` is always `None`                                                                                                         | supported; `refresh` populated                                                                                                                                                      | v0.16.0                              |
| `signin(access=…, refresh=…)` renewal                                                                                                  | raises `SurrealDbAuthenticationError` (no such access method is definable)                                                                                                    | returns a fresh, rotated token pair; the spent one is rejected                                                                                                                      | v0.16.0                              |
| Session token replayed on reconnect (`get_client`, `reconnect`)                                                                        | same on both lines                                                                                                                                                            | same on both lines                                                                                                                                                                  | v0.16.0                              |
| `info()` when the record's table denies it `select` on itself                                                                          | returns `None` (no error)                                                                                                                                                     | returns `None` (no error)                                                                                                                                                           | v0.16.0                              |
| Signing in as a system user while a record session is open                                                                             | permissions change, `$auth` still points at the record — only `invalidate()` clears it                                                                                        | 3.2.x: same as 2.x. **3.3.0**: the identity is cleared and `info()` returns `None`; `invalidate()` stays the portable log-out                                                       | v0.16.0                              |
| Duplicate signin identifier in the record table                                                                                        | signin fails (`No record was returned`)                                                                                                                                       | signin succeeds, picking one record                                                                                                                                                 | v0.16.0                              |
| Model auth (`signup`/`signin`/`authenticate`, `access_ddl`/`define_access`)                                                            | same on both lines (verified on 2.6.5)                                                                                                                                        | same on both lines (verified on 3.2.4)                                                                                                                                              | v0.17.0                              |
| Model auth session isolation (each call on its own ephemeral connection)                                                               | same on both lines (an SDK/connection property, not a server one)                                                                                                             | same on both lines (verified on 3.2.4)                                                                                                                                              | v0.17.0                              |
| Hydrated instance's password field after `signup`/`signin`                                                                             | holds the stored **hash**, never the plaintext                                                                                                                                | same on both lines                                                                                                                                                                  | v0.17.0                              |
| Model config `with_refresh=True` → `define_access()`                                                                                   | raises `SurrealDbError` (clause does not parse; message names the 3.x requirement)                                                                                            | applies, and `AuthResult.tokens.refresh` is populated                                                                                                                               | v0.17.0                              |
| `Model.refresh(token)` renewal                                                                                                         | unavailable — no refresh token can exist                                                                                                                                      | returns a fresh, rotated pair; the spent token is rejected                                                                                                                          | v0.17.0                              |
| Field aliases (`Field(alias=…)`) across writes, hydration and every QuerySet clause                                                    | same on both lines (pure Pydantic + client-side name translation)                                                                                                             | same on both lines (verified on 3.2.4)                                                                                                                                              | v0.18.0                              |
| `server_fields` — excluded from creates, kept on replaces, hydrated back                                                               | same on both lines; a `DEFAULT` is a create-time default on 2.6.5                                                                                                             | same on both lines; a `DEFAULT` is a create-time default on 3.2.4                                                                                                                   | v0.18.0                              |
| A server column omitted from a REPLACE (`UPDATE`/`UPSERT … CONTENT`)                                                                   | an optional column is deleted; a required one raises `Found NONE for field …`                                                                                                 | same on both lines — which is why the ORM keeps it in replace payloads                                                                                                              | v0.18.0                              |
| `merge(refresh=False)` — skipped resync, `RETURN NONE`, forfeited missing-record check                                                 | same on both lines                                                                                                                                                            | same on both lines (verified on 3.2.4)                                                                                                                                              | v0.18.0                              |
| `merge()` on a table that was never created                                                                                            | server returns no rows → ORM raises `SurrealDbError` ("no record found")                                                                                                      | server raises `NotFoundError` → normalised to the same `SurrealDbError`                                                                                                             | v0.18.0                              |
| Live queries (`live()`, `subscribe_live()`, `kill()`) — the subscription itself                                                        | supported over WebSocket (verified on 2.6.5)                                                                                                                                  | supported over WebSocket (verified on 3.2.4)                                                                                                                                        | v0.19.0                              |
| `live()` on a table that does not exist                                                                                                | succeeds; the stream simply never fires                                                                                                                                       | server raises `NotFound` → ORM raises `SurrealDbNotFoundError`                                                                                                                      | v0.19.0                              |
| Server notification after `kill()`                                                                                                     | none is sent                                                                                                                                                                  | a final envelope whose action is `KILLED`                                                                                                                                           | v0.19.0                              |
| End of a `subscribe_live()` stream after `kill()`                                                                                      | ORM pushes its own sentinel so the `async for` ends                                                                                                                           | ends on the sentinel or the `KILLED` envelope, whichever lands first                                                                                                                | v0.19.0                              |
| `session` key in a live notification envelope                                                                                          | absent                                                                                                                                                                        | present (the SDK session uuid)                                                                                                                                                      | v0.19.0                              |
| Live queries over HTTP                                                                                                                 | refused by the ORM with a message naming the WebSocket requirement                                                                                                            | same on both lines                                                                                                                                                                  | v0.19.0                              |
| Filtered live query (`live()`/`watch()` with `filter()`, `Q`, `Var`)                                                                   | bound parameters never reach a `LIVE SELECT` (they read as `NONE`, the filter matches nothing); the ORM inlines each value as a literal, so it works                          | parameters would work; the ORM inlines too, for one code path — same result on both lines                                                                                           | v0.20.0                              |
| A record entering / leaving a live filter                                                                                              | entering → `UPDATE`; leaving → nothing (nor its later `DELETE`)                                                                                                               | same on both lines                                                                                                                                                                  | v0.20.0                              |
| `fetch()` inside a live query                                                                                                          | links resolved in every notification                                                                                                                                          | same on both lines                                                                                                                                                                  | v0.20.0                              |
| Raw diff payload (`watch(diff=True)`): root patch path                                                                                 | `"/"`                                                                                                                                                                         | `""`                                                                                                                                                                                | v0.20.0                              |
| Raw diff payload (`watch(diff=True)`): `DELETE`                                                                                        | the whole last record (a dict, not a patch list)                                                                                                                              | `[{op: replace, path: "", value: None}]`                                                                                                                                            | v0.20.0                              |
| Typed diff `DELETE` (`live(diff=True)`): `event.instance`                                                                              | the last state of the record                                                                                                                                                  | an instance holding only the `id`                                                                                                                                                   | v0.20.0                              |
| Diff patch format                                                                                                                      | extended JSON Patch: a changed string is an `op: change` with diff-match-patch text                                                                                           | same on both lines                                                                                                                                                                  | v0.20.0                              |
| A WebSocket that drops (detection, server-side cleanup)                                                                                | the SDK's receive task ends within milliseconds; the server removes the session's live queries                                                                                | same on both lines                                                                                                                                                                  | v0.21.0                              |
| Live stream after a drop (`auto_resubscribe=True`)                                                                                     | resubscribed on a new connection; `live_id` changes, `on_reconnect` runs                                                                                                      | same on both lines                                                                                                                                                                  | v0.21.0                              |
| A request in flight when the socket drops                                                                                              | fails with the SDK's `KeyError` (not normalised; the next call reconnects)                                                                                                    | same on both lines                                                                                                                                                                  | v0.21.0                              |
| Change-feed entry shape                                                                                                                | `{versionstamp, changes}`; a create is reported as `update`                                                                                                                   | same on both lines                                                                                                                                                                  | v0.21.0                              |
| Change-feed `SINCE <n>` — what `n` is                                                                                                  | the counter, `versionstamp >> 16` (the raw stamp silently returns nothing); `ChangeModelStream.cursor` normalises it                                                          | the versionstamp, inclusive; `cursor` normalises it                                                                                                                                 | v0.21.0                              |
| `changes(since=<datetime>)`                                                                                                            | the server also returns some earlier changes → at-least-once                                                                                                                  | the server returns nothing → the ORM converts to a versionstamp → exact                                                                                                             | v0.21.0                              |
| `changes(since=None)` — finding "now"                                                                                                  | the ORM searches the counter (no history replayed)                                                                                                                            | the ORM reads the server clock's millisecond, then skips the entries already stamped in it (no history replayed); before v0.22.2 the entries of that millisecond were replayed      | v0.21.0                              |
| `INCLUDE ORIGINAL` delete in a change feed                                                                                             | the id only → an instance holding only the `id`                                                                                                                               | 3.3.x: carries `original` → the last state of the record; 3.1.5: as 2.x                                                                                                             | v0.21.0                              |
| `SHOW CHANGES` on a table without a change feed                                                                                        | returns `[]`; `changes()` raises `SurrealDbError` instead                                                                                                                     | same on both lines                                                                                                                                                                  | v0.21.0                              |
| Write to a `CHANGEFEED` table (`save()`, `merge()`, `delete()`)                                                                        | measured on 2.7.0: 8 concurrent writers conflict, even on different records (2.5–4 % refused); one writer, rarely, at a change-feed GC (10 s); wrap in `retry_on_conflict`    | measured on 3.3.4: no conflict observed, with 8 concurrent writers                                                                                                                  | v0.21.0                              |
| Typed edges (`relate(Edge, …)`, edge `save()` and `bulk_create()`) through `insert_relation()`                                         | same on both lines: a real relation that graph traversal sees, `id` hydrated from the returned row                                                                            | same on both lines                                                                                                                                                                  | v0.22.0                              |
| Explicit edge id that already exists: raw `INSERT RELATION`, then through the ORM (edge `save()`, `bulk_create()`)                     | raw: 2.7.0 silently keeps the old edge and returns it. ORM: `SurrealDbError` ("already exists"), nothing written                                                              | raw: 3.3.3 raises `AlreadyExistsError`; 3.1.5 silently keeps the old edge, although the SurrealDB docs announce a record-exists error from 3.1.5. ORM: as 2.x, on 3.1.5 and 3.3.3   | v0.22.0                              |
| Explicit edge id given as a `RecordID` of another table                                                                                | raw: silently moved into the edge table. ORM: `ValueError` before anything is sent                                                                                            | raw: raises. ORM: `ValueError` before anything is sent                                                                                                                              | v0.22.0                              |
| `TYPE RELATION … ENFORCED` with an endpoint record that does not exist                                                                 | raw: `InternalError` ("The record … does not exist"). ORM: `SurrealDbNotFoundError`                                                                                           | raw: `NotFoundError`. ORM: `SurrealDbNotFoundError`                                                                                                                                 | v0.22.0                              |
| Changing `in`/`out` of an existing edge (`UPDATE … CONTENT`/`MERGE`/`SET`, `PATCH`)                                                    | silently ignored by the server (the other fields are written); the ORM refuses it (`ValueError`, frozen fields)                                                               | same on both lines                                                                                                                                                                  | v0.22.0                              |
| `UPSERT` / `CREATE` carrying `in`/`out` on an edge table                                                                               | stores a plain record that no graph traversal sees; the ORM refuses `upsert()` on an edge, and `save()` uses `INSERT RELATION`                                                | same on both lines                                                                                                                                                                  | v0.22.0                              |
| Replacing an edge without `in`/`out` on a `TYPE RELATION … SCHEMAFULL` table                                                           | raises `Found NONE for field in …`; the ORM's `update()` always sends the endpoints                                                                                           | keeps the endpoints; the ORM sends them anyway                                                                                                                                      | v0.22.0                              |
| Edge record in a `CREATE`/`UPDATE` live notification or a change-feed update entry                                                     | `raw` carries `"__": true`, the typed `instance` drops it; `DELETE` notifications and change-feed delete entries carry no marker                                              | no marker; the typed `instance` is identical                                                                                                                                        | v0.22.0                              |
| `table_name`, model instances as filter values, `"table:id"` strings on edge endpoints in filters                                      | same on both lines                                                                                                                                                            | same on both lines                                                                                                                                                                  | v0.22.0                              |
| Edge write refused by the table's `PERMISSIONS` (record user)                                                                          | fewer rows come back and no error (a batch keeps its accepted rows); the ORM raises `SurrealDbError`                                                                          | same on both lines                                                                                                                                                                  | v0.22.0                              |
| `remove_relation` / `remove_all_relations` on an edge table that was never created, with or without `tx=`                              | native no-op (graph-path `DELETE`)                                                                                                                                            | native no-op (graph-path `DELETE`); before v0.22.0 it raised "table does not exist", which the ORM suppressed outside a transaction                                                 | v0.22.0                              |
| `remove_relation` / `remove_all_relations` under `tx=` when the source record's own table does not exist                               | no-op                                                                                                                                                                         | the statement fails, and the transaction with it (outside a transaction the ORM keeps it a silent no-op on both lines)                                                              | v0.22.0                              |
| `get_related(direction="both")`                                                                                                        | out-neighbours, then in-neighbours; the order inside each group differs between versions                                                                                      | same on both lines                                                                                                                                                                  | v0.22.0                              |
| `UNIQUE` index on an edge table's `in, out`: a second edge for the same ordered pair                                                   | raises ("index … already contains"), through `insert_relation()` and `RELATE`; the reverse pair is allowed                                                                    | same on both lines                                                                                                                                                                  | v0.22.0                              |
| Rich field types (`datetime`, `date`, `timedelta`/`Duration`, `Decimal`, `UUID`, `bytes`, `Range`, `set[X]`)                           | same on both lines: stored natively (sets as arrays) and read back as the declared type; a value the server would reject is refused with `ValueError` before anything is sent | same on both lines (verified on 3.3.4 and 3.1.5)                                                                                                                                    | v0.23.0                              |
| `TYPE set<…>` columns (a `set[X]` or `frozenset[X]` field)                                                                             | an array: the ORM reads back the declared set                                                                                                                                 | 3.1: writing the array is refused. 3.3: stored as a native set, read back as the declared set (the SDK 2.0.0 alone cannot read it, section 24). Declare `array<…>` columns on 3.1.x | v0.23.0                              |
| Reading a `Decimal`                                                                                                                    | trailing zeros kept (`Decimal("12.50")` reads `12.50`)                                                                                                                        | normalised (`12.5`); the values are equal                                                                                                                                           | pre-existing (documented in v0.23.0) |
| Geometry fields, `nearby()`, `GeoDistance`, the `inside` / `outside` / `intersects` lookups                                            | same on both lines: stored as native geometries, read back as the declared class, distances in metres identical                                                               | same on both lines (verified on 3.3.4 and 3.1.5); an SDK geometry SurrealDB refuses raises `ValueError` before anything is sent                                                     | v0.24.0                              |
| A `Decimal` coordinate written with an exponent in an SDK geometry                                                                     | raw SDK value: refused, or a wrong value for a long one. ORM: sent in fixed point, stored with its value                                                                      | raw SDK value: stored, a long one with a wrong value. ORM: sent in fixed point, stored with its value                                                                               | v0.24.0                              |
| A GeoJSON dict written to an untyped field (`Any`, `dict`)                                                                             | stored as a plain object, not a geometry: `nearby()` skips it, `geo::*` functions and `geometry<…>` columns refuse it; declare a geometry field                               | same on both lines                                                                                                                                                                  | pre-existing (documented in v0.24.0) |
| Full suite and every divergence in this table, re-run on SurrealDB 2.7.0 and 3.3.0                                                     | 2.7.0 identical to 2.6.5 (same passes, same 3.x-only skips)                                                                                                                   | 3.3.0 identical to 3.2.4, except the system-user signin row                                                                                                                         | v0.19.1                              |

> **Note on record IDs**: A record loaded from the database has its `id` field set to a native `surrealdb.RecordID` object, not a plain string. Use `model.get_raw_id()` to obtain the bare identifier string (e.g. `"alice"`), or compare directly with `model.id == RecordID("User", "alice")`. In-memory instances you construct yourself retain whatever value you assign.

### ORM behaviour: the embedded engine

The official SDK's in-process engine ([section 26](#26-embedded-engine-and-test-fixtures)) is a
line of its own: SurrealDB core 2.3.10, bundled with SDK 2.0.0. Rows measured with SDK 2.0.0 on
`mem://`, and on the 2.7.0 and 3.3.4 servers. Capabilities not listed behave as on a 2.x server.

| ORM capability                                                      | Embedded engine (SDK 2.0.0)                                                                                                                                                     | Servers (2.x and 3.x)                                                                                           | Since   |
| ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- | ------- |
| Connection and credentials (`set_connection()`)                     | URL, namespace and database; `user` and `password` optional, kept and never sent                                                                                                | every setting required; the configured user signs in on each new connection                                     | v0.25.0 |
| URLs the SDK refuses (`rocksdb://`, a typo)                         | `SurrealDbConnectionError` quoting the SDK (naming #233 for `rocksdb://`), before anything is opened                                                                            | n/a                                                                                                             | v0.25.0 |
| Clients                                                             | one for the whole process, shared by every event loop and thread                                                                                                                | one per event loop                                                                                              | v0.25.0 |
| Namespace/database change                                           | the next call resets the session to the owner and selects the new pair on the same engine (no data lost); the stored session ends                                               | taken by the next connection                                                                                    | v0.25.0 |
| Closing (`close_connection()`, `unset_connection()`, `reconnect()`) | `mem://` discarded, `surrealkv://` reopened with its data; with SDK 2.x a client kept after the close is still a running engine                                                 | the connection closes                                                                                           | v0.25.0 |
| SurrealQL line                                                      | 2.x (SurrealDB 2.3.10 core); `client.version()` reports the SDK (`surrealdb-2.0.0`)                                                                                             | the server's                                                                                                    | v0.25.0 |
| `transaction()`                                                     | buffered                                                                                                                                                                        | interactive on WebSocket and SurrealDB 3.x, else buffered                                                       | v0.25.0 |
| Concurrent first writes to a table that does not exist yet          | retryable conflict, a few % per batch of 20: define the table first                                                                                                             | rare on 2.x, none measured on 3.x                                                                               | v0.25.0 |
| Live queries (`live()`, `watch()`, `subscribe_live()`)              | `SurrealDbError` before any request: use change feeds                                                                                                                           | WebSocket only                                                                                                  | v0.25.0 |
| Change feeds (`changes()`)                                          | from now and from a cursor; `since=<datetime>` refused (`SurrealDbError`)                                                                                                       | from now, a cursor or a datetime                                                                                | v0.25.0 |
| Model auth (`AuthenticatedUserMixin`)                               | `bind=True` only, on the shared session, one call at a time with connection-level auth calls (the last one wins); a failure restores the previous session                       | a session of its own; `bind=True` also adopts the token                                                         | v0.25.0 |
| `ephemeral_client()`                                                | `SurrealDbError`: the engine has a single session per database                                                                                                                  | an anonymous throwaway client                                                                                   | v0.25.0 |
| Anonymous session                                                   | the in-process owner (full access); `info()` returns `None`                                                                                                                     | not used by the ORM (the configured user signs in); an anonymous `info()` raises `SurrealDbAuthenticationError` | v0.25.0 |
| `invalidate()`                                                      | back to the in-process owner                                                                                                                                                    | signs in again as the configured user                                                                           | v0.25.0 |
| Reading a table that does not exist                                 | `[]`, raw queries included (as on 2.x)                                                                                                                                          | the ORM reads `[]`; a raw query on 3.x raises `NotFoundError`                                                   | v0.25.0 |
| Parse errors                                                        | bare `RuntimeError` (the DDL helpers raise `SurrealDbError`)                                                                                                                    | the SDK's error classes                                                                                         | v0.25.0 |
| Everything else                                                     | CRUD, QuerySet, aggregations, relations, typed edges, rich and geo fields, upsert/patch, server values, computed fields, stored functions, signals: the same as on a 2.x server | unchanged                                                                                                       | v0.25.0 |

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
| v0.26.0 – v0.29.0 | Tier 2 — Extended (time travel, subqueries)          | 📋 Planned  |
| v0.30.0 – v0.39.0 | Tier 3 — Advanced (search, DDL, migrations, CLI)     | 📋 Planned  |
| v0.40.0           | Beta Phase (API freeze, hardening)                   | 📋 Planned  |
| v2.0.0            | Production / GA (aligned with SDK 2.0)               | 📋 Planned  |

> Every roadmap feature is implementable with the **official SDK 2.0** (native methods or
> `query()` SurrealQL) — no custom SDK. GA is numbered **v2.0.0** to mirror SDK 2.0; the `1.x`
> line is intentionally skipped.

See [docs/ROADMAP.md](docs/ROADMAP.md) for full details.

---

## SurrealDB-ORM-lite vs SurrealDB-ORM

This project prioritizes **stability and compatibility** with the official SurrealDB Python SDK. The full [SurrealDB-ORM](https://github.com/EulogySnowfall/SurrealDB-ORM/) uses a custom SDK for advanced features.

Both projects target the same feature set; the difference is **how** (official SDK vs custom
SDK) and **server support**. Everything below is on the lite roadmap via the official SDK 2.0
— only the custom-SDK internals stay exclusive to the full ORM; the official SDK's in-process engine (section 26) is lite-only.

| Feature                       | ORM-lite (official SDK)                                               | ORM (custom SDK)                 |
| ----------------------------- | --------------------------------------------------------------------- | -------------------------------- |
| Supported SurrealDB           | **2.6/2.7 + 3.2/3.3**                                                 | 3.x only                         |
| CRUD & QuerySet               | ✅                                                                    | ✅                               |
| Aggregations & GROUP BY       | ✅                                                                    | ✅                               |
| Model Signals                 | ✅                                                                    | ✅                               |
| Bulk Operations               | ✅                                                                    | ✅                               |
| Q Objects (OR/AND/NOT)        | ✅                                                                    | ✅                               |
| Parameterized Filters         | ✅                                                                    | ✅                               |
| Relations & Graph             | ✅ (reverse, keyword edge fields, tx= on every write helper: v0.22.0) | ✅                               |
| FETCH clause                  | ✅                                                                    | ✅                               |
| Transactions (tx=)            | ✅ v0.8 (core), v0.9 QS                                               | ✅                               |
| Interactive tx (3.x native)   | ✅ v0.9                                                               | ✅                               |
| upsert / update_or_create     | ✅ v0.10.0                                                            | ✅                               |
| Atomic field/array operations | ✅ v0.11.0                                                            | ✅                               |
| Retry on conflict             | ✅ v0.12.0                                                            | ✅                               |
| SurrealFunc & server values   | ✅ v0.13.0                                                            | ✅                               |
| Computed fields               | ✅ v0.14.0                                                            | ✅                               |
| `call_function()` (`fn::`)    | ✅ v0.15.0                                                            | ✅                               |
| JWT Authentication            | ✅ v0.16.0 (connection)                                               | ✅                               |
| Model auth mixin              | ✅ v0.17.0                                                            | ✅                               |
| Field Aliases & DX            | ✅ v0.18.0                                                            | ✅                               |
| Live queries (raw notifs)     | ✅ v0.19.0                                                            | ✅                               |
| Typed live queries            | ✅ v0.20.0                                                            | ✅                               |
| Live auto-resubscribe / CDC   | ✅ v0.21.0                                                            | ✅                               |
| Native typed relations        | ✅ v0.22.0 (typed edge models; relation DDL in v0.33.0)               | ✅ DDL only (TableType.RELATION) |
| Rich field types              | ✅ v0.23.0                                                            | ✅                               |
| Geospatial Fields             | ✅ v0.24.0                                                            | ✅                               |
| Embedded engine               | ✅ v0.25.0 (mem://, surrealkv://; rocksdb:// #233)                    | ❌ (HTTP/WebSocket only)         |
| Subqueries & Query Cache      | v0.27 – v0.28                                                         | ✅                               |
| Multi-database                | v0.29.0                                                               | ✅                               |
| Schema Introspection          | v0.30.0                                                               | ✅                               |
| DEFINE EVENT                  | v0.31.0                                                               | ✅                               |
| Materialized views            | v0.32.0                                                               | ✅                               |
| Full-Text Search              | v0.34.0                                                               | ✅                               |
| Vector Search (KNN/HNSW)      | v0.35.0                                                               | ✅                               |
| Hybrid Search (RRF)           | v0.36.0                                                               | ✅                               |
| Migrations & CLI              | v0.37 – v0.38                                                         | ✅                               |
| Test Fixtures & Factories     | ✅ v0.25.0 SurrealFixture; factories v0.39.0                          | ✅                               |
| Retry, Logging, Metrics       | v0.40.0                                                               | ✅                               |
| Connection Pool               | post-GA (tentative)                                                   | ✅                               |
| Custom SDK / CBOR Protocol    | ❌ never                                                              | ✅                               |

**Choose ORM-lite** if you want the official SDK, minimal dependencies, support for SurrealDB
2.x (2.6 / 2.7) **and** 3.x (3.2 / 3.3), and a full feature roadmap built entirely on the official SDK.

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
