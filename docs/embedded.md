# Embedded engine

The official SDK's in-process engine (`mem://`, `surrealkv://`) and the test fixtures built on it.

## 26. Embedded engine and test fixtures

Since v0.25.0 the ORM also runs on the official SDK's **in-process** SurrealDB engine: no server
and no Docker, the database lives inside your Python process. Since v0.50.0 the engine is an
optional extra: `pip install "surreal-orm-lite[embedded]"`. It suits tests, notebooks and
small tools, and the same models and queries run unchanged against a server. The
`surreal_orm_lite.testing` module adds the test helpers: `embedded_database()` (an isolated
database per block) and the full SurrealDB-ORM's `SurrealFixture` / `@fixture`.

### In-process databases

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

| Embedded URL                    | surreal-orm-lite (official SDK 3.0, `[embedded]`)                                        | Full SurrealDB-ORM       |
| ------------------------------- | ---------------------------------------------------------------------------------------- | ------------------------ |
| `mem://`, `memory://`, `memory` | ✅ in memory, gone when the connection closes                                            | ❌ (HTTP/WebSocket only) |
| `surrealkv://<path>`            | ✅ on disk (SurrealKV)                                                                   | ❌                       |
| `surrealkv+versioned://<path>`  | ✅ on disk, keeps history: time travel with `as_of()` (section 27)                       | ❌                       |
| `file://<path>`                 | ✅ a **SurrealKV** store, not the RocksDB store `file://` means for the SurrealDB server | ❌                       |
| `rocksdb://<path>`              | ❌ not yet supported by the official SDK (#233): the ORM raises the SDK's refusal        | ❌                       |

A bare `memory` (no `://`) is accepted too (SDK 2.0.0 refused it). The engine is SurrealDB core
**4.0.0**, bundled with SDK 3.0.0 (it was 2.3.10 with SDK 2.0.0): the ORM reads it as a 4.x server
(see below). Without the `embedded` extra, opening an embedded URL raises `SurrealDbConnectionError`
naming `surreal-orm-lite[embedded]`; importing the ORM or `surreal_orm_lite.testing` does not need it. surreal-orm-lite keeps no list of
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

### One engine per process

A server client is bound to the event loop it connected on, so the manager keeps one per loop
(see [Connections and event loops](connections.md#connections-and-event-loops)). The engine is not: the
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
  `set_namespace()`, `set_database()`), the next call selects the new pair on the same engine —
  nothing is lost — after returning the shared session to the owner if an identity was signed in;
  a stored session ends (see [Identity](#identity-on-the-embedded-engine)). A new **URL** closes the old engine and opens
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
  its live queries and its identity, even one signed in with `store=False`, and
  `is_connected()` stays `True` on an event loop that keeps one. An event loop whose server
  connection dropped while signed in with `store=False`
  ([section 21](realtime.md#21-live-queries-real-time-notifications)) still raises once the server URL is
  set back with `set_url()`, or after an `embedded_database()` block: closing the engine, or
  setting an identity on it (`signin()`, `signup()`, `authenticate()`, `invalidate()`, model auth
  with `bind=True`), does not end that state. `set_connection()` does: a new configuration is a
  choice of identity. `close_all_connections()` and `unset_connection()` close the server
  clients too.

Since v0.50.0 (SDK 3.0) closing stops the engine: a client you obtained before
`close_connection()` (or inside an `embedded_database()` block) no longer runs, so drop it
(`del client`) and get a new one with `get_client()` (with the surrealdb SDK 2.x, closing did not
stop an in-process engine, and two engines on one path missed each other's writes). The engine
locks its store. Never open one path from two processes.

### What the embedded engine does not do

Each of these raises a clear ORM error up front, never an SDK `AttributeError` or an engine
error:

- **Live queries** (`live()`, `watch()`, `subscribe_live()`): the engine has no notification
  channel. Read the table's change feed instead (`Model.objects().changes()`,
  [section 22](realtime.md#22-change-feeds-changes)), which works from now, from a cursor and,
  since v0.50.0, from a datetime.
- **Model auth needs `bind=True`**: the engine has a single session per database, so
  `AuthenticatedUserMixin` calls with `bind=False` (a session of their own) and
  `ephemeral_client()` are refused.
- **`rocksdb://`**: refused by the SDK
  ([#233](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/233)), see above.

And one difference that raises nothing:

- **Transactions are buffered**: the SDK refuses interactive transactions on the engine, so
  `transaction()` picks the buffered strategy, as on HTTP ([section 11](writing.md#11-transactions-atomic-all-or-nothing)).

Parse errors are the SDK's error classes, as on a server (with SDK 2.0.0 they were a bare
`RuntimeError`).

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

### Its SurrealQL is the 4.x line

SDK 3.0.0 bundles the SurrealDB **4.0.0** engine: write 4.x SurrealQL in raw queries and
computed fields, and expect the 4.x column of the
[behaviour table](compatibility.md#orm-behaviour-surrealdb-2x-3x-and-4x): rows in no fixed order
without `ORDER BY`, a raw query on a table that does not exist raises `NotFoundError` (the ORM
reads `[]`), and a migration's field change on a populated `SCHEMAFULL` table follows the 4.x rule
([section 39](migrations.md#surrealdb-4x-changing-a-field-on-a-populated-table)).
`client.version()` reports the engine (`surrealdb-4.0.0`). With SDK 2.0.0 (`0.40.x`) the engine was
SurrealDB core 2.3.10, a 2.x line.

### Identity on the embedded engine

- The **anonymous session is the in-process owner**: full access (DDL, writes), and
  `info()` returns `None`.
- `signin()` / `signup()` / `authenticate()` ([section 18](authentication.md#18-authentication-signin--signup--info--invalidate))
  work with the engine's record access methods and `DEFINE USER`s, and record users and table
  `PERMISSIONS` apply as on a server. A sign-in changes the session of the **whole process**:
  every event loop and thread shares the engine's single session.
- `invalidate()` returns to the in-process owner: a configured user, if any, is never signed in.
  A file-backed store is closed and opened again (data kept); `mem://` and `memory` sign back in
  as a temporary root user, `__surreal_orm_lite_owner`, removed right after: that name is reserved
  on in-memory embedded engines only
  ([authentication](authentication.md#the-embedded-engines-owner)).
- A namespace/database change returns the session to the owner and **ends** the stored session:
  a token belongs to the database it was issued for, and replaying it would move the session
  back there.
- Model auth with `bind=True` ([section 19](authentication.md#19-model-level-authentication-authenticatedusermixin))
  signs that single session in, one call at a time: concurrent `bind=True` calls, and the
  connection-level `signin()` / `signup()` / `authenticate()` / `invalidate()` that hold the
  same session, are serialised (each `bind=True` call reads back its own record and adopts its
  own token) and the last one wins. A failed `bind=True` puts the previous session back, with
  one exception, as on a server: when `signup()` or `signin()` succeeded but the record does not
  fit the model, the session stays signed in as that record (the error says so).

```python
assert await SurrealDBConnectionManager.info() is None  # anonymous: the in-process owner
```

### Test fixtures

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
pytest-asyncio gives the fixture and the test: there is one engine per process. Since v0.40.0 the opt-in plugin
`surreal_orm_lite.testing.pytest_plugin` ships this fixture ([section 41](testing.md#41-test-data-factories-modelfactory-faker-and-the-pytest-plugin)).

A fresh engine raises a retryable write conflict when several writes run concurrently on a table
that does not exist yet: define the tables your test writes to concurrently first
(`DEFINE TABLE`), or run those writes in a function decorated with `@retry_on_conflict()`
([section 14](writing.md#14-optimistic-concurrency-retry_on_conflict)).

```python
import asyncio

async with embedded_database() as client:
    await client.query("DEFINE TABLE User SCHEMALESS;")  # before the concurrent first writes
    await asyncio.gather(*(User(name=f"user {n}").save() for n in range(20)))
    assert await User.objects().count() == 20
```

### Coming from the full SurrealDB-ORM

- **The full ORM has no embedded engine**: it connects over HTTP/WebSocket only (see the
  [embedded URL table](#in-process-databases)).
- **`surreal_orm_lite.testing` mirrors `surreal_orm.testing`** for `SurrealFixture` and
  `fixture`: the same names, the same declarations, `load()` as an async context manager.
- **`load()` keeps an explicit `id`** where the full ORM resets it to `None`, so a fixture can
  name its records (and an edge its endpoints). It also **cleans up** when a save fails or is
  cancelled mid-load: the instances already saved are deleted before the error propagates.
- **`@fixture` raises `TypeError`** on a class that is not a `SurrealFixture` subclass (the full
  ORM accepts any class), and `load()` raises `ValueError` on an undecorated subclass of a
  fixture that declares instances of its own (the full ORM loads only the parent's).
- **`load()` is typed** to yield the fixture class, so `fixtures.alice` has its model's type
  (the full ORM yields `Any`).
- **Lite additions**: `SurrealDBConnectionManager.is_embedded()` and `embedded_database()`.
- **`ModelFactory` and `Faker`**, and a pytest plugin with the `surreal_db` fixture, are in
  [section 41](testing.md#41-test-data-factories-modelfactory-faker-and-the-pytest-plugin) (v0.40.0).
