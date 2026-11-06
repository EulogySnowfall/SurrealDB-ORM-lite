# Connections

The connection manager, event loops and several databases.

## Context Manager

```python
async with SurrealDBConnectionManager():
    users = await User.objects().all()
# Connection automatically closed
```

`async with SurrealDBConnectionManager():` uses the current connection (the default, or the `using()` block's)
and closes that connection's client on exit.

## Connections and event loops

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

- `close_connection()` closes **the running loop's** client of the current connection only (the default, or the
  one a `using()` block names).
- `close_all_connections()` tears down every cached client of every connection; `unset_connection()` does that for
  the default connection only, `remove_connection(name)` for a named one.
- `is_connected()` answers for the loop asking; called outside a loop, it reports whether any
  loop still holds a client.

Each named connection keeps its own per-loop clients in the same way
([section 30](#30-multiple-databases-add_connection-using)).

Long-lived single-loop applications are unaffected: one loop, one connection, as before.

An embedded URL is the exception: the engine runs in this process and is not bound to a loop, so
the manager keeps one client for the whole process ([section 26](embedded.md#26-embedded-engine-and-test-fixtures)).
While it is configured, `close_connection()` closes that client only, not the loop's server client:
`is_connected()` stays `True` on a loop that keeps one.

## 30. Multiple databases (`add_connection()`, `using()`)

Since v0.29.0 one process can work with several databases at once. Beside the default connection
(`set_connection()`), `add_connection()` registers **named connections** — another database on the
same server, another server, or the embedded engine — and each model reads and writes through its
own. Each connection keeps its own clients, session, live streams, transactions and cached reads,
and decides on its own server line: a SurrealDB 2.x server and a 3.x server work side by side. The
examples of this section run on the embedded engine (`mem://`), with nothing to start.

```python
from surreal_orm_lite import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager

# Any URL works: ws://…/rpc or http://… (with user= and password=), mem://, surrealkv://<path>
SurrealDBConnectionManager.set_connection(url="mem://", namespace="shop", database="main")   # "default"
SurrealDBConnectionManager.add_connection("analytics", url="mem://", namespace="shop", database="analytics")

class Customer(BaseSurrealModel):                       # no connection key: "default"
    id: str | None = None
    name: str

class PageView(BaseSurrealModel):
    model_config = SurrealConfigDict(connection="analytics")
    id: str | None = None
    path: str

await Customer(id="ada", name="Ada").save()             # shop/main
await PageView(id="v1", path="/pricing").save()         # shop/analytics
names = SurrealDBConnectionManager.list_connections()   # ['default', 'analytics']
```

- `add_connection(name, *, url, user=None, password=None, namespace, database)` opens nothing:
  each connection opens its own clients on first use, as the default one does. Its arguments are
  checked before anything is registered — a non-empty name, URL, namespace and database, and a
  user and password for a server URL (optional on the embedded engine) — and a URL the installed
  SDK refuses raises `SurrealDbConnectionError`. `add_connection("default", …)` configures the
  default connection, as `set_connection()` does, with these checks.
- Registering a name again with the same settings changes nothing; with other settings, see
  [What each connection keeps for itself](#what-each-connection-keeps-for-itself).
- `get_config(name)` returns the settings as a frozen `ConnectionConfig` (`url`, `user`,
  `password`, `namespace`, `database`; the password is kept out of its `repr`), and `None` for a
  name that is not registered or for the default connection while it is not configured.
  `list_connections()` returns the registered names, `'default'` first while it is configured.
- `remove_connection(name)` unregisters a name and closes what it holds; a name that is not
  registered is a no-op, and `remove_connection("default")` is `unset_connection()`.
- `get_client(name)`, `close_connection(name)` and `transaction(name)` act on a named connection,
  and without a name on the current one
  ([Which connection a manager method uses](#which-connection-a-manager-method-uses)). For a name
  that is not registered, `get_client(name)` and `transaction(name)` raise `ValueError`, while
  `close_connection(name)` closes nothing.

### Routing a model

A model's `connection` key — `model_config = SurrealConfigDict(connection="analytics")`, the full
SurrealDB-ORM's key — sends every database call of the model to that connection: writes, reads
and every QuerySet, relations, `raw_query()`, `call_function()`, model auth, live queries, change
feeds and `as_of()`. A model without the key, or with `None`, uses the default connection.

```python
where = PageView.get_connection_name()                  # analytics
```

- `get_connection_name()` is the connection the model's calls use right now: the name of the
  `using()` block around the call, else the `connection` key, else `"default"`. It is resolved at
  every database call, so a QuerySet built outside a `using()` block and run inside it uses the
  block's connection; a live stream or a change feed resolves it once, when it starts.
- The key is checked when the class is defined (`TypeError` for a value that is not a string,
  `ValueError` for an empty one). Whether the name is registered is checked at the model's first
  database call (`ValueError`), so models can be declared before `add_connection()` runs.
- Subclasses inherit the key and may set their own; edge models (`BaseSurrealEdge`) and
  `AuthenticatedUserMixin` models take it too. `table_name` and `connection` are independent: two
  connections can each hold a table of the same name.

### Redirecting a block: `using()`

`using(name)`, the full SurrealDB-ORM's, runs a block of code against a named connection: inside
it every model, whatever its `connection` key, and every manager method that talks to a database
use that connection, with the exceptions of
[Which connection a manager method uses](#which-connection-a-manager-method-uses).

```python
async with SurrealDBConnectionManager.using("analytics"):
    elsewhere = await Customer.objects().count()        # 0
here = await Customer.objects().count()                 # 1
```

- The name is checked when the block is entered: one that is not registered raises `ValueError`
  before the block runs. `get_active_connection_name()` returns the block's name, `None` outside
  any block.
- Blocks nest, and the outer connection is back after the inner block, on an exception too.
- It is async-safe: the name lives in a `contextvars.ContextVar`, so concurrent tasks each see
  their own block. A task created inside the block keeps its connection for its whole life, even
  after the block exits; `asyncio.to_thread()` carries it, a new `threading.Thread` does not.
- Live streams and change feeds started inside the block keep its connection after it.
- `signin()` inside `using(name)` signs in that connection only.
- With the test helpers, `embedded_database()` inside `using(name)` isolates that connection, and
  `embedded_database(connection=name)` does so outside any block; inside a `using()` block, naming
  another connection raises `ValueError` (the block's models would reach their real database).

### Transactions and other operations across connections

SurrealDB runs a statement, and a transaction, in one database. So the ORM refuses, before sending
the statement, every operation that would mix two connections in one statement or one
transaction:

```python
from surreal_orm_lite import SurrealDbError

async with SurrealDBConnectionManager.transaction("analytics") as tx:
    await PageView(id="v2", path="/").save(tx=tx)

try:
    async with SurrealDBConnectionManager.transaction() as tx:      # the default connection
        await PageView(id="v3", path="/x").save(tx=tx)
except SurrealDbError as exc:
    refused = type(exc).__name__                        # SurrealDbError
```

- **A transaction is one connection's.** `transaction(name)`, or `transaction()` inside
  `using(name)`, opens it on that connection, with that connection's strategy (interactive on a
  3.x server over WebSocket, buffered otherwise). A model of another connection given that `tx=`
  raises `SurrealDbError`, before any request and any signal: its writes, `objects(tx=)`,
  `refresh(tx=)`, `call_function(tx=)`, and the manager's `call_function(tx=)`. Transactions of
  two connections are independent: nothing commits across connections.
- **Subqueries**: a `Subquery`, or a QuerySet used as a filter value, whose model runs on another
  connection than the statement around it raises `SurrealDbError`: SurrealDB would read it in the
  outer statement's database. Read it through its own connection first and filter on its values.
- **Relations**: `relate()`, `remove_relation()`, `remove_all_relations()` and `get_related()`
  raise `SurrealDbError` when the source, a target model instance, the edge model and
  `model_class` are not all on one connection. A `RecordID` or `"table:id"` target is not checked.
- **Links**: following a record link — `fetch()`, or a dotted path in `filter()`, `Q`, `select()`,
  `order_by()`, `values()` or an aggregation — through a field typed with a model of another
  connection raises `SurrealDbError`: SurrealDB resolves a link in the statement's database.
- **An on-disk embedded store** (`surrealkv://`, `file://`) is open under one name at a time in a
  process: a second name raises `SurrealDbConnectionError` until the first one closes it
  (`close_connection()`, `remove_connection()`). Two `mem://` names are two separate databases.
- Inside a `using()` block every model runs on the block's connection, so none of these refusals
  apply there.

What the ORM cannot see, and you keep on one connection yourself:

- a link followed through a field whose annotation names no model (`RecordID`, `str`, `dict`);
- graph arrows: `select("->likes->Post")`, `traverse()`;
- an edge saved with endpoint ids of another database;
- raw SurrealQL: `raw_query()`, `QuerySet.query()`, `call_function()`, `Transaction.add()`.

### What each connection keeps for itself

- **Its clients**: one per event loop, as the default connection's
  ([Connections and event loops](#connections-and-event-loops)); on the embedded engine, one for
  the whole process. `close_connection(name)` closes one connection's client on the running loop,
  `close_all_connections()` every connection's.
- **Its session**: `signin()`, `signup()`, `authenticate()`, `invalidate()`, `info()` and
  `clear_session()` act on the current connection, so sign in inside `using(name)`. A stored token
  is replayed on its own connection only, and an identity lost on one connection does not block
  another. An `AuthenticatedUserMixin` model signs in on its own connection.
- **Its live streams and change feeds**, bound to the connection when they start:
  `close_connection(name)` ends that connection's live streams only.
- **Its transactions**, with the strategy of its server: one process holds interactive
  transactions on a 3.x connection beside buffered ones on a 2.x connection.
- **Its stored-function signatures** (`call_function()`).
- **The ORM's cached reads** (`cache()`): a cached result is never served to another connection.
  An ORM write removes the cached reads of its table on every connection: more than needed, never
  a stale result.
- A WebSocket client the ORM gave up refuses every request instead of reconnecting anonymously: after its connection
  dropped the `SurrealDbConnectionError` is retryable (retry the call; the ORM opens a new client), after
  `close_connection()`, a new registration or `remove_connection()` it is not (get a new client with `get_client()`).
- Registering a name again with other settings, or removing it with `remove_connection()`, ends what the ORM holds
  on the old settings: a client still opening is closed unused (the embedded engine is closed only when its URL
  changed or the name was removed; otherwise it is kept and its session is selected again at the next call) and its
  caller gets a retryable `SurrealDbConnectionError` (retry the call), live streams end, a change feed ends at its
  next read (`SurrealDbConnectionError`: its cursor belongs to the old database, start a new one), and an open
  `transaction()` is cancelled when its block ends, with nothing committed (`SurrealDbConnectionError`: open it
  again). After `remove_connection()`, a call already under way on the name opens no new client on the old settings:
  one still waiting for the embedded engine while another thread held it raises `ValueError`
  (`Connection not been set ('<name>').`). A client you obtained yourself with `get_client()` before the change is
  yours: on WebSocket its socket is closed and its next request fails; an HTTP client keeps the namespace and database
  it was opened on, so call `get_client()` again; an embedded client is the connection's one in-process session: it
  follows a new namespace or database at the ORM's next call, but after a new URL or `remove_connection()` it is the
  previous engine (with SDK 2.x still running), so stop using it, as after `close_connection()`.
  `set_connection()` and the `set_*()` setters do none of this: clients opened on the previous
  settings keep serving, as in earlier versions.

Entries you cache yourself with `QueryCache.make_key()` and `set()` are not scoped to a connection: the key is
built from the table, the statement and the variables only. When the same statement runs on two connections, put
the connection's name among the variables (keep the table name as it is, so the ORM's writes still invalidate the
entry).

```python
from surreal_orm_lite import QueryCache

statement = "SELECT count() AS n FROM page_view GROUP ALL;"
key_main = QueryCache.make_key(statement, {"connection": "default"}, "page_view")
key_analytics = QueryCache.make_key(statement, {"connection": "analytics"}, "page_view")
distinct = key_main != key_analytics                   # True
```

### Which connection a manager method uses

A manager method acts on the **current connection** — the `name` it is given, else the innermost
`using()` block's, else the default one — except where this table says otherwise:

| Method                                                                                                                                                                                                                                                                                      | Acts on                                                                                         |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `add_connection()`, `remove_connection()`, `get_config()`, `list_connections()`, `using()`, `get_active_connection_name()`                                                                                                                                                                  | the registry, by the name given                                                                 |
| `set_connection()`, `unset_connection()`                                                                                                                                                                                                                                                    | always the default connection                                                                   |
| `close_all_connections()`                                                                                                                                                                                                                                                                   | every connection, on every event loop                                                           |
| `get_client(name)`, `close_connection(name)`, `transaction(name)`                                                                                                                                                                                                                           | _name_, else the current connection                                                             |
| `subscribe_live(uuid)`, `kill(uuid)`                                                                                                                                                                                                                                                        | the connection whose live stream started _uuid_ on this event loop, else the current connection |
| every other method: the session methods (`signin()`, `info()` …), the getters and setters (`get_url()`, `set_database()` …), `reconnect()`, `validate_connection()`, `ephemeral_client()`, `call_function()`, `is_connected()`, `is_embedded()`, `async with SurrealDBConnectionManager():` | the current connection                                                                          |

A coroutine method (`get_client()`, `signin()`, `close_connection()` …) resolves the connection
when it is awaited, and a context manager (`transaction()`, `ephemeral_client()`) when it is opened
by `async with` (strictly, when it is called), so both act on the connection of the `using()`
block they are written in.

A manager method you replace by hand, in a test for instance, is inherited by every connection, so
it intercepts the calls of every connection. Save what the class body holds,
`vars(SurrealDBConnectionManager)[name]`, and put that back: pytest's `monkeypatch.setattr()` and
`unittest.mock.patch.object()` already do. Saving `SurrealDBConnectionManager.<name>` instead, which
worked while there was one connection, saves a method bound to `SurrealDBConnectionManager`: put
back, it is what every connection inherits, and it acts on the current connection, so a model with
a `connection` key reads and writes the current connection's database, with no error.

```python
async def offline():
    raise ConnectionError("no database in this test")

saved = vars(SurrealDBConnectionManager)["get_client"]
SurrealDBConnectionManager.get_client = offline         # every connection calls offline()
try:
    await PageView.objects().count()                    # raises ConnectionError
except ConnectionError:
    pass
finally:
    SurrealDBConnectionManager.get_client = saved       # the classmethod again
views = await PageView.objects().count()                # 2
```

### Coming from the full SurrealDB-ORM

- Same names: `add_connection()`, `remove_connection()`, `get_config()`, `list_connections()`,
  `get_active_connection_name()`, `using()`, `ConnectionConfig`, `SurrealConfigDict(connection=)`,
  `get_connection_name()`, `get_client(name)`, `close_connection(name)`.
- `ConnectionConfig` has the same fields in the same order, built by position or by keyword, without `protocol`
  (the official SDK speaks CBOR only); `user` and `password` may be `None` (the embedded engine needs neither).
- `close_connection()` without a name: closing an embedded `mem://` client discards its database, so
  `close_connection()` without a name stays on the current connection; `close_all_connections()` closes every one
  (full ORM: `close_connection()` closes every connection).
- `list_connections()` lists `'default'` first while it is configured (full ORM: registration order).
- Registering a name again with other settings closes its open clients and ends what the ORM holds on the old
  settings (full ORM: drops the clients without closing them). `set_connection()` keeps its open clients, as in
  earlier lite versions.
- The getters, the setters and `reconnect()` follow `using()` (full ORM: the default connection).
- `transaction(name)`, `embedded_database(connection=)` and the errors for operations across connections are lite
  additions.
- `add_connection()` checks its arguments before registering anything (a non-empty name, URL, namespace and
  database; a user and password for a server URL, optional on the embedded engine), and a model's `connection` key
  must be a non-empty string, checked when the class is defined (full ORM: no checks). `ConnectionConfig` keeps the
  password out of its `repr`.
- `get_client(name)`, `close_connection(name)` and `transaction(name)` accept `name` in code, but
  `inspect.signature()` does not show it, so FastAPI's `Depends(SurrealDBConnectionManager.get_client)` adds no
  `?name=` query parameter: a request never chooses the connection (full ORM: `name` is visible and becomes a query
  parameter).
