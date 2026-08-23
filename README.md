# Surreal ORM Lite

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![SurrealDB](https://img.shields.io/badge/SurrealDB-2.6.5%20%7C%203.2.4-purple)
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
| SurrealDB    | 2.6.x or 3.2.x                    |
| Official SDK | surrealdb[pydantic]>=2.0.0,<3.0.0 |
| Pydantic     | >=2.13.4                          |

> **Note**: As of v0.7.0, Surreal ORM Lite targets the SurrealDB Python SDK 2.x (`surrealdb[pydantic]>=2.0.0,<3.0.0`), which supports the SurrealDB 3.x protocol. It is tested against SurrealDB **v2.6.5** and **v3.2.4**. SurrealDB 3.1.x is no longer a supported line as of v0.14.0 — the suite is still run against 3.1.5 as a backward-compatibility check, but a regression there does not block a release.

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
| patch / atomic ops     | ✅     |
| Retry on conflict      | ✅     |
| Server-side functions  | ✅     |
| Computed fields        | ✅     |

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
  (optimistic MVCC) than on 2.6.x (see the behaviour table).

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
member is tested against SurrealDB 2.6.5 and 3.2.4**:

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

**Compatibility advantage over the full ORM**: ORM-lite runs on **both** SurrealDB **2.6.x and 3.2.x**, while the full [SurrealDB-ORM](https://github.com/EulogySnowfall/SurrealDB-ORM/) (custom SDK) targets **3.x only**. Lite stays usable on existing 2.6.x deployments without forcing a server upgrade.

| SurrealDB Version | SDK Version | Status                  |
| ----------------- | ----------- | ----------------------- |
| 3.2.4             | 2.0         | ✅ Tested               |
| 2.6.5             | 2.0         | ✅ Tested               |
| 3.2.x / 2.6.x     | 2.0         | ✅ Compatible           |
| 3.1.5             | 2.0         | ⚠️ Backward-compat only |
| < 2.6 or > 3.2    | —           | ⚠️ Not guaranteed       |

### ORM behaviour: SurrealDB 2.6.x vs 3.x

Surreal ORM Lite runs on both lines; some capabilities differ because they rely on server
features introduced in SurrealDB 3.x. On 2.6.x the ORM degrades gracefully. Capabilities not
listed behave the same on both lines.

| ORM capability                                                                                                                         | SurrealDB 2.6.x                                                              | SurrealDB 3.2.x                                                          | Since   |
| -------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------ | ------- |
| Transaction strategy auto-selected by `transaction()`                                                                                  | buffered batch (`BEGIN…COMMIT`)                                              | native interactive on WebSocket                                          | v0.9.0  |
| Reads inside a transaction (`objects(tx=)`)                                                                                            | raise (buffered cannot read)                                                 | see uncommitted writes                                                   | v0.9.0  |
| `save(tx=)` with an auto-generated id                                                                                                  | raises — explicit id required                                                | supported                                                                | v0.9.0  |
| `refresh(tx=)` inside a transaction                                                                                                    | raises                                                                       | works                                                                    | v0.9.0  |
| `bulk_update` / `bulk_delete` / `QuerySet.patch` row count inside a tx                                                                 | returns `0` (not knowable pre-commit)                                        | real count                                                               | v0.9.0  |
| "Already exists" error on create                                                                                                       | normalised to `SurrealDbError`                                               | normalised to `SurrealDbError`                                           | v0.7.0  |
| Cleanup on a missing target (`delete_table`, `remove_relation`)                                                                        | native no-op                                                                 | ORM makes it a silent no-op                                              | v0.7.0  |
| Aggregation over an empty set (`NaN` / `±inf`)                                                                                         | returns `0.0` / `None`                                                       | ORM normalises to `0.0` / `None`                                         | v0.7.0  |
| Namespace/db selection (`use()` ordering)                                                                                              | lenient (auto-creates)                                                       | strict — ORM signs in before `use()`                                     | v0.7.0  |
| `upsert()` / `update_or_create()` / `get_or_create()`                                                                                  | same on both lines                                                           | same on both lines                                                       | v0.10.0 |
| `patch()` / `atomic_append` / `atomic_set_add` / `atomic_remove` / `atomic_increment`                                                  | same on both lines (portable `array::*` fns chosen over divergent `+=`/`-=`) | same on both lines                                                       | v0.11.0 |
| `retry_on_conflict` / `SurrealDbConflictError` (retryable conflict)                                                                    | same type + decorator; conflicts rarer (engine serialises more)              | same type + decorator; conflicts are the normal optimistic-MVCC failure  | v0.12.0 |
| `SurrealFunc` / `server_values=` / `extra_vars=` on `save`/`merge`                                                                     | same on both lines (compiled to portable `CREATE`/`UPDATE … SET`)            | same on both lines                                                       | v0.13.0 |
| Shipped function-name enums (`SurrealTimeFunction`, `SurrealCryptoFunction`, …)                                                        | every catalogued member verified on 2.6.5                                    | every catalogued member verified on 3.2.4                                | v0.13.0 |
| `server_values` inside a transaction — when the instance sees the computed value                                                       | only after commit (buffered; `refresh()` to read it)                         | immediately (interactive returns the row)                                | v0.13.0 |
| `merge(server_values=)` on a missing record / never-created table                                                                      | server returns no rows → ORM raises `SurrealDbError`                         | server raises `NotFound` for a missing table → ORM raises the same error | v0.13.0 |
| Computed fields (`Computed[...]` → `DEFINE FIELD … VALUE`)                                                                             | same on both lines (DDL, recompute triggers, precedence over client data)    | same on both lines                                                       | v0.14.0 |
| DDL run inside a transaction, then rolled back                                                                                         | definition rolled back with the transaction                                  | same on both lines                                                       | v0.14.0 |
| Invalid computed-field expression — raw SDK exception                                                                                  | `InternalError`                                                              | `ValidationError`                                                        | v0.14.0 |
| Invalid computed-field expression — through the ORM                                                                                    | `SurrealDbError` (normalised)                                                | `SurrealDbError` (normalised)                                            | v0.14.0 |
| Issue #156 correctness fixes (`Var`/`$$`, `first()`, `*_or_create` strictness, `created` on upsert, quoted ids, one-hop `get_related`) | same on both lines (each fix reproduced and verified on 2.6.5)               | same on both lines (verified on 3.2.4)                                   | v0.14.3 |
| Record-id lookups (`filter(id=…)`, `id__in`, `get(…)`, `*_or_create(id=…)`) coerced to `RecordID`                                      | same on both lines (int/str typing verified on 2.6.5)                        | same on both lines (verified on 3.2.4)                                   | v0.14.4 |

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
| v0.11.0           | patch / atomic field & array ops                 | ✅ Released |
| v0.12.0           | retry_on_conflict & optimistic concurrency       | ✅ Released |
| v0.13.0           | SurrealFunc & server-side values                 | ✅ Released |
| v0.14.0           | Computed fields (`DEFINE FIELD … VALUE`)         | ✅ Released |
| v0.14.3 – v0.14.4 | Correctness: `$`-values, record-id lookups       | ✅ Released |
| v0.15.0 – v0.22.0 | Tier 1 — Core (auth, live, relations)            | 📋 Planned  |
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
| Supported SurrealDB           | **2.6.x + 3.2.x**       | 3.x only         |
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
| Atomic field/array operations | ✅ v0.11.0              | ✅               |
| Retry on conflict             | ✅ v0.12.0              | ✅               |
| SurrealFunc & server values   | ✅ v0.13.0              | ✅               |
| Computed fields               | ✅ v0.14.0              | ✅               |
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
2.6.x **and** 3.2.x, and a full feature roadmap built entirely on the official SDK.

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
