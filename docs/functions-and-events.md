# Stored functions and events

Calling `fn::` stored functions, and defining server-side events and functions from Python.

## 17. Stored functions: `call_function()`

Invoke a function declared server-side with `DEFINE FUNCTION fn::…`. Arguments are **bound as
query parameters**, never formatted into the statement.

```python
from surreal_orm_lite import SurrealDBConnectionManager

# Declare it once: define_function() writes DEFINE FUNCTION OVERWRITE (section 32)
await SurrealDBConnectionManager.define_function(
    "fn::acquire_lock",
    """
    UPSERT type::record("lock:⟨" + $table_id + "⟩") SET holder = $pod_id, ttl = $ttl;
    RETURN { acquired: true, holder: $pod_id };
    """,
    parameters={"table_id": "string", "pod_id": "string", "ttl": "int"},
)

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

Signature caching is transparent. `define_function()` and `remove_function()`
([section 32](#32-server-side-events-and-stored-functions-event-define_events-define_function)) drop the function's
cached signature themselves, on every connection of this process to the same namespace and database, whatever its URL
(`ws://` or `http://`, with or without `/rpc`, `localhost` or `127.0.0.1`), so the next call sees a redefinition made
through them at once, even one that only changes the order of the same parameter names, and even on another connection
than the one that applied the DDL. For a function redefined out of band — with raw DDL
(`client.query("DEFINE FUNCTION …")`) or from another process — two helpers are available, both on the current
connection: `SurrealDBConnectionManager.clear_function_signature_cache()` and `function_signature_cache_size()`. The
cache is keyed by URL, namespace and database, so `set_url()`, `set_namespace()` and `set_database()` cannot serve a
signature read elsewhere; it is cleared outright by `set_connection()` and `unset_connection()`. After an out-of-band
redefinition, a signature whose parameter _names_ changed self-heals on its own — but one with the **same names in a
different order** cannot be detected, since `params=` is a mapping and carries no order to compare against: call
`clear_function_signature_cache()` on each connection that calls the function, or `params=` keeps binding in the stale
order.

## 32. Server-side events and stored functions (`Event`, `define_events()`, `define_function()`)

Since v0.31.0 the ORM defines two kinds of objects that SurrealDB runs itself. An **event** runs SurrealQL inside
the database whenever a record of its table is created, updated or deleted, whoever writes it — the ORM, a raw query
or another client: an audit row, a guard that refuses a write, a message for an outbox. A **stored function**
(`fn::…`) is SurrealQL that `call_function()` calls ([section 17](#17-stored-functions-call_function)). Events are
declared next to the model, in `SurrealConfigDict(events=[…])`; the ORM writes their `DEFINE EVENT` statements with
quoted names and expressions checked for mistakes, applies them idempotently, and section 31's introspection reads
them back ([section 31](schema.md#31-schema-introspection-databaseintrospector-generate_models_from_db)). The same code runs on
SurrealDB 2.x, 3.x and the embedded engine; asynchronous events and `$input` are 3.x only. The examples assume a
configured connection: they run as written on the embedded engine (`mem://`), a 2.x core, which is why the
asynchronous example prints a refusal.

### Declaring events on a model

```python
from surrealdb import ServerError

from surreal_orm_lite import BaseSurrealModel, Event, SurrealConfigDict, SurrealDBConnectionManager


class Account(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_name="account",
        events=[
            Event(
                "email_audit",
                when="$event = 'UPDATE' AND $before.email != $after.email",
                then="CREATE audit_log SET account = $value.id, old = $before.email, new = $after.email",
                comment="Keeps every e-mail change",
            ),
            Event(
                "no_negative_balance",
                when="$event != 'DELETE' AND $after.balance < 0",
                then="THROW 'balance cannot be negative'",
            ),
        ],
    )

    id: str | None = None
    email: str
    balance: int = 0


for statement in Account.event_ddl():
    print(statement)
# DEFINE EVENT OVERWRITE `email_audit` ON `account` WHEN $event = 'UPDATE' AND $before.email != $after.email THEN { CREATE audit_log SET account = $value.id, old = $before.email, new = $after.email } COMMENT 'Keeps every e-mail change';
# DEFINE EVENT OVERWRITE `no_negative_balance` ON `account` WHEN $event != 'DELETE' AND $after.balance < 0 THEN { THROW 'balance cannot be negative' };

await Account.define_events()  # idempotent (OVERWRITE): run it at start-up

account = Account(id="ada", email="ada@old.example", balance=10)
await account.save()
await account.merge(email="ada@new.example")
client = await SurrealDBConnectionManager.get_client()
[rows] = await client.query("SELECT old, new FROM audit_log;", {})  # one result per statement
print(rows)
# [{'new': 'ada@new.example', 'old': 'ada@old.example'}]

try:
    await account.merge(balance=-5)
except ServerError as exc:  # the event refused the write: nothing changed
    print(type(exc).__name__)
# InternalError
```

- `then` is the SurrealQL the event runs: one statement, or several separated by `;`. The ORM always writes it as a
  block (`{ … }`), so several statements, `LET` and `THROW` work on both lines. `when` is one expression; left out,
  the event runs on every write (SurrealDB reads it back as `WHEN true`).
- Inside the event, `$event` is `'CREATE'`, `'UPDATE'` or `'DELETE'`; `$before` and `$after` are the record before
  and after the write (`NONE` before a create and after a delete), `$value` is the record (as it was, for a delete),
  and on SurrealDB 3.x `$input` holds the fields the write sent. Expressions name **columns**: a field declared with
  `Field(alias=…)` is read by its alias.
- A guard on `$after` names `$event != 'DELETE'`, as `no_negative_balance` does: after a delete `$after.balance` is
  `NONE`, and `NONE < 0` is true, so without it the guard would refuse every delete.
- Event and table names are written quoted, so `update` or `delete` work as event names; a comment is written as an
  escaped string literal.
- `define_events()` writes `DEFINE EVENT OVERWRITE`: it is idempotent, so run it at start-up or deployment.
  `overwrite=False` writes `IF NOT EXISTS` instead and keeps an event already defined. It applies one statement per
  event and stops at the first refusal, the earlier events staying defined: pass `tx=` for all or nothing. It returns
  the statements it applied, which `event_ddl()` renders without a connection.
- `Account.remove_event("email_audit")` removes one event (`REMOVE EVENT IF EXISTS`; with `missing_ok=False` an
  event that does not exist raises `SurrealDbNotFoundError`). An event the model no longer declares stays defined
  until it is removed: `define_events()` does not compare the database with the model; the migrations do, and
  `makemigrations(drop_missing=True)` writes its `RemoveEvent` ([section 39](migrations.md#39-migrations-makemigrations-migrationexecutor-operations)).
- A subclass inherits `events` and defines them on its own table, unless it sets its own (`events=[]` for none).
  Typed edges (`BaseSurrealEdge`) take events too: they fire on the edge table. Defining an event on a table that
  does not exist creates the table, `SCHEMALESS`.
- A write that an event refuses (a `THROW`, or an error in `then`) raises the SDK's `InternalError`, and nothing is
  written. The rows an event writes are written by the server: they fire no ORM signal.

### Events without a model

```python
stock_guard = Event(
    "stock_guard", when="$event != 'DELETE' AND $after.stock < 0", then="THROW 'stock cannot go below zero'"
)
print(await SurrealDBConnectionManager.define_event(stock_guard, "product"))
# DEFINE EVENT OVERWRITE `stock_guard` ON `product` WHEN $event != 'DELETE' AND $after.stock < 0 THEN { THROW 'stock cannot go below zero' };

await SurrealDBConnectionManager.remove_event("stock_guard", "product")
await Account.remove_event("no_negative_balance")  # declared or not: removal is by name
```

- `SurrealDBConnectionManager.define_event(event, table)` applies an `Event` to any table and returns its statement;
  `remove_event(event, table)` removes one by name. Both run on the current connection (the `using()` block's, else
  `"default"`) and take `overwrite=` or `missing_ok=`, and `tx=`, as the model's methods do.
- `event.ddl(table)` renders the statement without a connection (`overwrite=False` for `IF NOT EXISTS`).
- An `Event` is frozen, hashable and compared by value; its `repr()` leaves out `when` and `then`, which may hold a
  secret such as a webhook token. Without `tx=`, a refusal leaves them out too: it names the event and its table,
  never the statement (`Can't apply event definition: 'stock_guard' on table 'product' -> …`), and gives the server's
  reason without its excerpt of the statement (a parse error keeps its line and column). With `tx=`, see
  [Transactions, connections and the query cache](#transactions-connections-and-the-query-cache).

### Asynchronous events (SurrealDB 3.x)

```python
from surreal_orm_lite import SurrealDbError

notify = Event(
    "notify_shipping",
    when="$event = 'CREATE'",
    then="CREATE outbox SET shipment = $value.id, at = time::now()",
    async_mode=True,
    retry=3,
)
print(notify.ddl("shipment"))
# DEFINE EVENT OVERWRITE `notify_shipping` ON `shipment` ASYNC RETRY 3 WHEN $event = 'CREATE' THEN { CREATE outbox SET shipment = $value.id, at = time::now() };

try:
    await SurrealDBConnectionManager.define_event(notify, "shipment")
except SurrealDbError as exc:  # the embedded engine is a 2.x core, like a SurrealDB 2.x server
    print(exc)
# ASYNC events need SurrealDB 3.x, and this connection runs the embedded engine (a SurrealDB 2.x core): 'notify_shipping' cannot be defined with async_mode=True. Drop async_mode, retry and max_depth, or connect to a SurrealDB 3.x server. Nothing was defined.
```

- On SurrealDB 3.x an event with `async_mode=True` (`ASYNC`) runs after the write's transaction commits, never for a
  cancelled one, so it does not hold the write up, and a failing body does not refuse the write (its own writes are
  rolled back). Its row appears a few milliseconds after the commit: wait for it rather than reading it right after
  the write.
- `retry` (`RETRY`) is how many times SurrealDB retries a failing body, 1 by default, and `max_depth` (`MAXDEPTH`) how
  deep a chain of asynchronous events may go, 3 by default; both take `0` to `65535` and need `async_mode=True`. Read
  back, an asynchronous event has `async_mode`, `retry` and `max_depth` set on its `EventState`, the defaults filled
  in.
- On SurrealDB 2.x and the embedded engine, which cannot parse `ASYNC`, the ORM raises `SurrealDbError` before
  sending anything: no event of the call is defined. `event.ddl()` renders an asynchronous event on any line.

### Stored functions: `define_function()`

```python
await SurrealDBConnectionManager.define_function(
    "fn::greet",
    "RETURN string::concat('Hello, ', $name, '!');",
    parameters={"name": "string"},
    returns="string",
    comment="Says hello",
)
print(await SurrealDBConnectionManager.call_function("fn::greet", ["Ada"]))
# Hello, Ada!

await SurrealDBConnectionManager.define_function(
    "fn::greet",
    "RETURN string::concat($greeting, ', ', $name, '!');",
    parameters={"greeting": "string", "name": "string"},
    returns="string",
)
print(await SurrealDBConnectionManager.call_function("fn::greet", params={"name": "Ada", "greeting": "Hi"}))
# Hi, Ada!

await SurrealDBConnectionManager.remove_function("fn::greet")
```

- `define_function()` runs on the current connection and names the function as `call_function()` does: `"greet"`,
  `"fn::greet"` or `"fn::billing::total"`. The body is always written as a block; `RETURN` gives the result.
- `parameters` maps each parameter name, without the `$`, to its SurrealQL kind, in declaration order, which is the
  order of `call_function()`'s positional `args`. A name SurrealDB defines itself (`auth`, `session`, `this` …) is
  refused. Kinds are SurrealQL text, written in canonical form: `option<int>` is written `option<int>`, and so is
  `none | int`. A kind the ORM cannot read raises `ValueError`: it is never rewritten silently. `returns` is the
  result's kind.
- `permissions` is `"FULL"`, `"NONE"` or `"WHERE <condition>"`, in any case; left out, SurrealDB applies `FULL`.
- `call_function()` follows a redefinition at once: `define_function()` and `remove_function()` drop the signature
  `params=` had cached, even when only the order of the parameters changed, on every connection of this process to
  the same namespace and database, whatever its URL — the admin connection a `using()` block applies the DDL on and
  the application's connection alike, one over `http://` and the other over `ws://` included. A function redefined
  with raw DDL or from another process still needs `clear_function_signature_cache()` on each connection that calls
  it ([section 17](#17-stored-functions-call_function)).
- `remove_function("fn::greet")` writes `REMOVE FUNCTION IF EXISTS`; with `missing_ok=False` a function that does not
  exist raises `SurrealDbNotFoundError`. `overwrite=False` writes `IF NOT EXISTS` and keeps the function already
  defined.
- `function_ddl()` takes the same arguments, without `tx=`, and returns the statement without applying it. Without
  `tx=`, a body the server refuses raises `SurrealDbError` (`Can't apply function definition: 'fn::greet' -> …`),
  which names the function, never its body.

### Transactions, connections and the query cache

- Every method that applies DDL takes `tx=`. In a buffered transaction (SurrealDB 2.x, HTTP, the embedded engine)
  the statements are applied at commit, and an event defined in the block fires for the block's later writes; in an
  interactive one (WebSocket and SurrealDB 3.x) they are applied in the transaction, whose own writes fire the event.
  Either way, a block that raises leaves nothing defined, and nothing is visible outside the block until it commits.
- Inside the transaction that defines a function, call it with positional `args`: `params=` reads the signature from
  the committed schema.
- A model's methods run on the model's connection (its `connection` key, or the `using()` block's); the manager's
  methods on the current connection. A `tx=` of another connection raises `SurrealDbError` before any request.
- Defining needs a system user allowed to do it: root, or a namespace or database `OWNER` or `EDITOR`. On a server,
  a `VIEWER`, a record user or an anonymous session gets `SurrealDbAuthenticationError`. A buffered transaction
  (SurrealDB 2.x, HTTP, the embedded engine) only queues the statement, so a refusal comes back when the block
  commits, as the transaction's own error, never as `SurrealDbAuthenticationError` or `SurrealDbNotFoundError`,
  `missing_ok=False` included. When a definition is applied in a transaction (`tx=`), an error raised when the
  transaction ends gives the server's reason and the statement's position, without the server's excerpt of the
  statement (v0.31.2, [#278](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/278)).
- Live queries see the rows an event writes (`watch()` on their table). The query cache
  ([section 29](querying.md#29-query-cache-cache-querycache)) does not: it forgets the tables the ORM itself writes, so a cached
  read of a table that events write stays as it was until its time to live runs out. Do not cache such reads, or call
  `QueryCache.invalidate(Model)` after the write.
- Applying DDL fires no signal, and neither do the rows events write.

### SurrealDB 2.x and 3.x

- Asynchronous events (`ASYNC`) are 3.x only: on SurrealDB 2.x and the embedded engine the ORM raises
  `SurrealDbError` before any DDL.
- `$input` holds the fields the write sent on 3.x (`{ name: 'a' }` for `SET name = 'a'`); it is `NONE` on 2.x and
  the embedded engine.
- A write that changes nothing fires no event, except on 3.x an `UPSERT` — the ORM's `upsert()` of an unchanged
  record — which fires one `UPDATE` event.
- Read back (section 31), each line renders the definitions its own way: on 3.x a block is one line and a single
  statement loses its `;`, on 2.x a block of several statements spans several lines; an optional parameter reads
  `none | int` on 3.x and `option<int>` on 2.x. The [2.x/3.x table](compatibility.md#orm-behaviour-surrealdb-2x-vs-3x) lists the
  differences.

### Security

Event, table and parameter names are checked as identifiers before any request, and event and table names are
written quoted; a comment is written as an escaped string literal. Expressions — `when`, `then`, function bodies,
kinds and permission conditions — are written into the statement as they are, since SurrealDB takes no bound
parameter in a definition. Before any request the ORM checks their structure: string literals and quoted identifiers
terminated, brackets balanced, no NUL character, no SurrealQL comment marker (`--`, `#`, `//`, `/* */`) outside a
literal, and no `;` outside brackets where one expression is expected (`when`, a permission condition). It reads
every `/` as an operator, so a regex literal whose pattern holds an unbalanced bracket, a quote or a comment marker
is refused. The check catches mistakes — unbalanced brackets, a comment that would
swallow the clauses the ORM appends, a second statement where one expression is expected — and is not an injection
defence: expressions are trusted application code, never user input. To use a value, read it from the record
(`$after.email`) or from a `DEFINE PARAM`, never format it into the text. Kinds are parsed and written in canonical
form. An `Event`'s `repr()` leaves out its expressions, and a refusal raised without `tx=` names the event or the
function, never its statement; with `tx=`, an error raised when the transaction ends gives the server's reason and
the statement's position, without the server's excerpt of the statement (v0.31.2,
[#278](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/278)). Applying a definition needs a privileged
system user: run `define_events()` and `define_function()` at start-up or deployment, not on a record user's session.

### Coming from the full SurrealDB-ORM

- **`DefineEvent(name, table, when, then, comment)`** becomes `Event(name, when=…, then=…, comment=…).ddl(table)` to
  render it, or `await SurrealDBConnectionManager.define_event(event, table)` to apply it; `RemoveEvent(name, table)`
  becomes `await SurrealDBConnectionManager.remove_event(event, table)`, whose first parameter is named `event`.
- **The documented `Event(…, async_mode=True, retry=3)`** is the lite's `Event`, with `max_depth=` added, declared in
  `model_config = SurrealConfigDict(events=[…])` rather than in a `class Meta`.
- **`then` is a block**: the ORM writes `THEN { … }` where the full ORM writes `THEN (…)`, in which `LET` and `THROW`
  fail (SurrealDB 2.x refuses the definition, 3.x every write to the table).
- **Names are quoted, and comments escaped with a backslash** (full ORM: names as written, and a quote doubled as
  `''`, which SurrealDB refuses).
- **`define_function()` has no full-ORM equivalent**: the full ORM declares a function with `RawSQL`.
- **The migration operations** (`DefineEvent`, `RemoveEvent`, `RawSQL`) are in `surreal_orm_lite.migrations`, and
  `makemigrations(drop_missing=True)` removes the events a model no longer declares ([section 39](migrations.md#39-migrations-makemigrations-migrationexecutor-operations)).
