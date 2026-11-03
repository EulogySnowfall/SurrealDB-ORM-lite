# Writing data

Bulk operations, signals, transactions, upserts, patches and optimistic concurrency.

## 6. Bulk Operations

```python
# Bulk create
users = [User(name="Alice", age=30), User(name="Bob", age=25)]
created = await User.objects().bulk_create(users)

# Bulk update (returns count of updated records)
count = await User.objects().filter(status="pending").bulk_update(status="active")

# Bulk delete (returns count of deleted records)
count = await User.objects().filter(status="inactive").bulk_delete()
```

## 10. Model Signals

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

## 11. Transactions (atomic, all-or-nothing)

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

**A failed statement fails the whole transaction, on every strategy.** After a statement fails
inside the block — even when the block catches its error and carries on — the transaction is
cancelled when the block ends, nothing of the block is committed, and leaving the block raises
`SurrealDbError` (`SurrealDbConflictError` for a retryable conflict, so `retry_on_conflict` still
retries the block). A buffered transaction fails as a whole at commit; an interactive one raises
the failed statement's error at once, then, when the block ends, "The transaction was cancelled: a
statement in it failed; nothing was committed (SurrealDbError)", which names the type of that
error and is chained to it. An explicit `tx.commit()` after a failure raises the same error. When
the block's own exception propagates, that exception wins, as before. Reads the ORM answers itself
are not failed statements: a table that does not exist reads as no rows, and `get()` raises
`SurrealDbNotFoundError`.

```python
async with SurrealDBConnectionManager.transaction() as tx:
    await User(id="alice", name="Alice").save(tx=tx)
    try:
        await Order(id="o2", user="User:alice", total=-1).save(tx=tx)  # refused by a field ASSERT
    except SurrealDbError:
        pass  # caught, but the transaction can no longer commit
# leaving the block raised SurrealDbError: alice was not saved either
```

**Transaction errors leave out the server's excerpt of the statement** (v0.31.2). When the server
refuses the query as a whole (a statement it cannot parse), the error gives the server's reason and
the position, without the server's excerpt of the statement: a buffered commit raises "Transaction
failed: the server refused statement 2 of 3: … (at line 1, column 7 of the statement)", the
embedded engine included, and a write or a definition in an interactive transaction raises
"Transaction failed: the server refused the statement: … (at line 1, column 7 of the statement)".
The line and column are those of the statement as written, before the ORM renames its `$name`
parameters for the batch, and a delimiter left unclosed names the statement it opens in, even when
the server stops in a later statement. The server's full text stays reachable as the error's
`__context__`, which a traceback does not print. A read in a transaction is not affected: it raises
the SDK's own error, as it does outside a transaction.

## 12. Upsert & get_or_create / update_or_create

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
but are not written to the record. The lookup binds the queryset's `variables()`, as `filter()`
does: `User.objects().variables(e="bob@example.com").get_or_create(email=Var("e"))` finds the user
whose email is the variable's value, and writes that value on create (see
[String filter values and `Var()`](querying.md#string-filter-values-and-var)). Without a transaction the behaviour is identical on
SurrealDB 2.6.x and 3.x; under `objects(tx=)` they participate in the transaction on 3.x,
while a buffered 2.6.x transaction raises on the lookup (see the behaviour table).

## 13. Patch & atomic field/array operations

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

## 14. Optimistic concurrency: `retry_on_conflict`

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
  a non-retryable failure (e.g. a duplicate-key error) is **not** retried. `is_conflict_error()`
  reads the exception and its chain as a traceback prints it: a context suppressed with
  `raise … from None` is not read (v0.31.2), so a conflict you re-raise as another exception with
  `raise … from None` is no longer recognised: chain it (`raise … from exc`, or `raise …` without
  `from` inside the `except` block), or raise a `SurrealDbConflictError`.
- The exception type is identical on 2.6.x and 3.x; conflicts simply arise more often on 3.x
  (optimistic MVCC) than on 2.6.x (see the behaviour table), except on change-feed tables, where
  2.x refuses concurrent writes that 3.x accepts (see [section 22](realtime.md#22-change-feeds-changes)).
- The decorator wraps any coroutine, not only a `transaction()`: a single `save()`, `merge()` or
  `delete()` is retried the same way (see [section 22](realtime.md#22-change-feeds-changes) for change-feed
  tables on SurrealDB 2.x). The conflict such a write raises outside a transaction is not
  converted: on 2.7.0 it is the SDK's `InternalError`, which `is_conflict_error()` recognises.
