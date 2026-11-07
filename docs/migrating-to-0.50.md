# Migrating to 0.50 (official SDK 3.0, SurrealDB 4.x)

Surreal ORM Lite 0.50.0 moves to the official SurrealDB Python SDK 3.0 (`surrealdb[pydantic]>=3.0.0,<4.0.0`) and
supports the server lines that SDK supports: SurrealDB 2.x, 3.x and 4.x. The ORM's minor series follows the SDK
major: `0.50.x` is the SDK 3.0 line, `0.40.x` the SDK 2.0 line. The SDK 2.0 line (`0.40.x`) is deprecated and receives
security fixes only; move to `0.50.x`, or pin `pip install "surreal-orm-lite<0.50"` if you must stay on SDK 2.0.

Most applications upgrade without a code change: models, querysets, transactions, live queries and migrations keep
their API. This note lists what changes, with the code before and after. The examples run as written on the embedded
engine (`mem://`), each printed line shown as a `# ` comment under its code; the
[compatibility guide](compatibility.md#orm-behaviour-surrealdb-2x-3x-and-4x) lists every behaviour by server line.

## Installing

- The ORM requires `surrealdb[pydantic]>=3.0.0,<4.0.0` (it was `>=2.0.0,<3.0.0`); Python 3.11 or later, as before.
  `pip install -U surreal-orm-lite` brings the SDK along.
- The SDK's in-process engine is now an optional extra: `pip install "surreal-orm-lite[embedded]"`. Without it,
  opening `mem://`, `memory` or `surrealkv://` raises `SurrealDbConnectionError` naming the extra. The testing helpers
  (`surreal_orm_lite.testing`: `embedded_database()`, the pytest plugin's `surreal_db` fixture) need it too; importing
  the ORM or `surreal_orm_lite.testing` does not.

## Supported servers

SurrealDB 2.x, 3.x and **4.x** (tested on 2.7.0, 3.3.5 and 4.0.0). The SDK refuses a server outside 2.0.0 to 5.0.0
(excluded) at connect time, and the ORM raises it as `SurrealDbConnectionError` naming the version, not retryable.
A capability that needs SurrealDB 3.x still degrades on 2.x as before; SurrealDB 4.x behaves as 3.x except where the
compatibility guide says otherwise (a column of its own in the behaviour table).

## Raw queries with several statements

`raw_query()` and `QuerySet.query()` now run every statement and return the rows of every one, in order, as the full
SurrealDB-ORM does. Before, only the first statement's rows came back, with a `UserWarning` (the warning is gone) and
a failing later statement was ignored; now it raises. A statement that returns no list (`LET`, `RETURN`, `DEFINE`)
adds no row.

```python
from surreal_orm_lite import BaseSurrealModel, SurrealDBConnectionManager


class Reader(BaseSurrealModel):
    id: str | None = None
    name: str
    age: int


await Reader(id="ada", name="Ada", age=36).save()
await Reader(id="bob", name="Bob", age=17).save()
rows = await Reader.raw_query(
    "LET $min = 18; SELECT * FROM Reader WHERE age >= $min; SELECT * FROM Reader WHERE age < $min;"
)
print([reader.name for reader in rows])  # 0.40.x: ['Ada'] and a warning
# ['Ada', 'Bob']
```

With `SELECT`s on several tables, the rows are hydrated together, with the usual rule: model instances when every row
validates, otherwise dicts. Select the model's own table, or read dicts with the client of `get_client()`.

## The client of `get_client()`

`SurrealDBConnectionManager.get_client()` returns an SDK 3.0 client. Code that calls it directly meets the SDK's own
changes: `query()` returns one result per statement (unpack it), `merge()`, `patch()` and `insert_relation()` are
gone (`update(record).merge(…)`, `update(record).patch(…)`, `insert(table, data, relation=True)`), the other methods
are awaitable builders, a plain `str` target is always a table name (pass a `RecordID` for a record), and a `NULL`
reads as the SDK's `Null`. The ORM's own methods (`Model.merge()`, `Model.patch()`, `QuerySet.patch()`) keep their
names and read `NULL` as `None`.

```python
from surrealdb import RecordID

client = await SurrealDBConnectionManager.get_client()
[adults] = await client.query("SELECT VALUE name FROM Reader WHERE age >= 18;")  # 0.40.x: adults = await …
print(adults)
# ['Ada']
await client.update(RecordID("Reader", "ada")).merge({"age": 37})  # 0.40.x: await client.merge(…)
```

A client the ORM gave up refuses every request instead of reconnecting: after its WebSocket connection dropped, a
kept client (an `InteractiveTransaction` begun before, a `LiveStream`) raises a retryable `SurrealDbConnectionError`
(retry the call; the ORM opens a new client), and after `close_connection()`, a new registration or
`remove_connection()` a non-retryable one: get a new client with `get_client()`. The ORM builds its clients without
the SDK's own reconnection, query streaming and token renewal, and with no request timeout over WebSocket
([connections guide](connections.md#the-clients-the-orm-builds)).

## Datetimes keep their nanoseconds

A datetime read from the database is a `PreciseDatetime`, a `datetime` subclass that keeps the nanoseconds SurrealDB
stores, and writing it back keeps them (0.40.x truncated them to microseconds). A `datetime` field accepts it as any
`datetime`; a validator receives it as such.

```python
from datetime import datetime


class Stamp(BaseSurrealModel):
    id: str | None = None
    at: datetime


await client.query("CREATE Stamp:s SET at = d'2026-11-06T12:00:00.123456789Z';")
stamp = await Stamp.objects().get("s")
print(type(stamp.at).__name__, stamp.at.microsecond, stamp.at.nanosecond)
# PreciseDatetime 123456 789
await stamp.update()  # written back with its nanoseconds
print((await Stamp.objects().get("s")).at.nanosecond)
# 789
```

Zero durations and native sets now read through the SDK itself (`Duration(0)`, a Python `set`); degenerate
geometries and out-of-range datetimes are read as in 0.40.x, and an undecodable value raises
`SurrealDbConnectionError`.

## Order of `first()` and of pages

On SurrealDB 3.x, 4.x and the embedded engine, `first()` without `order_by()` now ends its statement with
`ORDER BY id`: it returns the matching record with the smallest id. Before, it returned whatever the server sent
first: id order for a plain table scan, but index order for a filter on an indexed field or a full-text match, so a
3.x `first()` with such a filter may now return another record. On SurrealDB 2.x the statement is unchanged.
`order_by()`, `similar_to()` and the `values()`/`annotate()` aggregations keep their own order.

```python
for n in (3, 1, 2):
    await Reader(id=f"r{n}", name=f"R{n}", age=20 + n).save()
print((await Reader.objects().filter(name__startswith="R").first()).get_raw_id())
# r1
```

`all()`, `values()` and `limit()`/`offset()` pages without `order_by()` stay unordered: SurrealDB 4.x returns rows in
no fixed order without `ORDER BY`. Add `order_by()` to page deterministically. The
[querying guide](querying.md#4-queryset-methods) gives the measured cost of the implicit order.

## The embedded engine is SurrealDB 4.0

The SDK 3.0 in-process engine is a SurrealDB **4.0** core (it was 2.3.10 with SDK 2.0): embedded URLs take the
ORM's 4.x branches (`FULLTEXT` indexes, `ASYNC` events, `fetch()` under `as_of()`, change feeds from a datetime) and
the 4.x semantics (rows in no fixed order without `ORDER BY`, the migration rule below). Write 4.x SurrealQL in raw
queries on it. A bare `memory` URL is accepted.

- **Data written by the old engine**: a `surrealkv://` or `file://` directory written with SDK 2.0 must be exported
  with the old version and imported into the new engine.
- **Sessions**: the engine's session starts as the in-process owner. After a record user signs in
  (`signin(..., bind=True)`, `signup()`), `invalidate()` returns to the owner. On a file-backed engine the ORM reopens
  the store; on `mem://` and `memory` it signs back in as a temporary root user, `__surreal_orm_lite_owner`, defined
  before the first sign-in and removed right after the return. `__surreal_orm_lite_owner` is reserved on in-memory
  embedded engines only: if your application defines a root user of that name there, the next sign-in raises
  `SurrealDbConnectionError` before anything is exchanged, and your user is left as it is. Servers and file-backed
  engines never get that user ([authentication guide](authentication.md#18-authentication-signin--signup--info--invalidate)).
- `close()` now stops the engine: a client kept after `close_connection()` no longer runs.

## Migrations on SurrealDB 4.x

SurrealDB 4.x creates its databases at compatibility level 4, where a field of a populated `SCHEMAFULL` table can only
be redefined with a type that accepts every old value, and a new field must accept `NONE`. A migration that narrows a
field's type (an `AlterField` from `int` to `string`), or adds a field refusing `NONE`, on such a table raises
`FieldChangeRefusedError`, a `MigrationStatementError` from `surreal_orm_lite.migrations` naming the `table` and the
`field`: refused inside the migration's transaction, before the field change runs; nothing is applied. Empty tables,
databases at compatibility level 3, SurrealDB 2.x and 3.x apply the same migration as before.

Write such a change as a `RawSQL` that converts the existing rows, with SurrealDB 4.x's `MIGRATE EXISTING ROWS`
clause or the portable form that runs on every line; the
[migrations guide](migrations.md#surrealdb-4x-changing-a-field-on-a-populated-table) gives both. A `MigrateRows`
operation is planned for v0.56.0. Outside migrations, `define_table(overwrite=True)` meets the server's own refusal.

## Staying on SDK 2.0

Pin the SDK 2.0 line, which receives security fixes only: `pip install "surreal-orm-lite<0.50"`. Its last release
is 0.40.x; new features come in `0.50.x` and later.
