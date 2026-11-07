# Surreal ORM Lite: guide for AI coding assistants

[Surreal ORM Lite](https://github.com/EulogySnowfall/SurrealDB-ORM-lite) (`pip install surreal-orm-lite`, imported as
`surreal_orm_lite`) is an async, Django-style ORM for SurrealDB with Pydantic models, built on the official SurrealDB
Python SDK 3.0 (`surrealdb[pydantic]>=3.0.0,<4.0.0`, since v0.50.0; the SDK 2.0 line `0.40.x` is deprecated). This file tells an AI coding assistant how to write code with it. Download it into your project and import
it from your `CLAUDE.md` with the line `@docs/surreal-orm-lite.md` (Claude Code), or paste it into your assistant's
instructions. To match the release you installed, replace `main` in the URL with its tag (`v<version>`).

```bash
curl -fsSL -o docs/surreal-orm-lite.md https://raw.githubusercontent.com/EulogySnowfall/SurrealDB-ORM-lite/main/docs/ai/CLAUDE.md
```

Every example below is run by the project's test suite, on SurrealDB 2.x, 3.x, 4.x and the embedded engine, in order: a
later example uses the models and records of an earlier one.

## Setup and connection

```python
import os

from surreal_orm_lite import SurrealDBConnectionManager

# The library reads no environment variable: read your settings and pass them.
SurrealDBConnectionManager.set_connection(
    url=os.environ["SURREAL_URL"],  # ws://host:8000/rpc, http://host:8000, mem:// or surrealkv://<path>
    user=os.environ["SURREAL_USER"],
    password=os.environ["SURREAL_PASSWORD"],
    namespace=os.environ["SURREAL_NAMESPACE"],
    database=os.environ["SURREAL_DATABASE"],
)
```

- `set_connection()` opens nothing: the first database call connects. Call it once, at startup. The
  `surreal-orm-lite` command line reads these same `SURREAL_*` variables.
- Prefer a WebSocket URL (`ws://…/rpc`): live queries need it, and on SurrealDB 3.x it gives interactive
  transactions. HTTP works for everything else. `mem://` and `surrealkv://<path>` run the SDK's in-process engine.
- The manager keeps **one client per event loop**, made on first use. Never pass a client to another loop or thread.
- Close at shutdown: `await SurrealDBConnectionManager.close_connection()` (this loop's client),
  `close_all_connections()` (every client), or `async with SurrealDBConnectionManager():` around the work.
- Several databases: `add_connection(name, url=…, …)`, a model's `SurrealConfigDict(connection=name)`, and
  `async with SurrealDBConnectionManager.using(name):` to redirect a block.

## Models and idioms

```python
from pydantic import Field

from surreal_orm_lite import BaseSurrealModel, SurrealConfigDict


class Author(BaseSurrealModel):
    id: str | None = None  # the record is Author:<id>; None lets the server generate one
    name: str = Field(..., max_length=100)
    email: str
    age: int = Field(..., ge=0)
    tags: list[str] = []


class Book(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="book", primary_key="isbn")

    isbn: str
    title: str
    price: float


ada = await Author(id="ada", name="Ada", email="ada@example.com", age=36).save()  # CREATE
await ada.merge(age=37)  # partial update: only `age` is written
ada.tags = ["math"]
await ada.update()  # full replace: the record becomes exactly the instance
await Book(isbn="978-0", title="Notes", price=9.5).save()
same = await Author.objects().get("ada")
assert same.age == 37 and same.tags == ["math"]
```

- **Async everywhere**: every call that reaches the database is a coroutine; `await` it inside `async def`. There is
  no synchronous API.
- **Writes**: `save()` creates (an id that exists raises `SurrealDbError`); `update()` replaces the whole record, so a
  field the instance does not hold is removed; `merge(**fields)` writes only those fields; `upsert()` creates or
  replaces (explicit id required); `delete()` removes. After a write the instance holds the stored row.
- **Tables and ids**: the table is the class name unless `table_name` says otherwise; `primary_key` names the field
  that holds the id. `get("ada")`, `filter(id="Author:ada")` and a `RecordID` name the same record, but the Python
  type matters: `5` and `"5"` are two different ids.
- **Fields** are Pydantic fields: validation happens when the instance is built. `datetime`, `date`, `timedelta`,
  `Decimal`, `UUID`, `bytes`, `set[...]` and geometries are stored natively and read back as declared.
- **Server values**: `save(server_values={"created": SurrealFunc("time::now()")})` lets the server compute a field;
  `Computed[...]` declares a `DEFINE FIELD … VALUE` field.

## Querying

```python
from surreal_orm_lite import Q

await Author(id="bob", name="Bob", email="bob@example.com", age=17).save()

adults = await Author.objects().filter(age__gte=18).order_by("-age").limit(10).exec()
either = await Author.objects().filter(Q(name__startswith="A") | ~Q(age__gte=18)).exec()
first = await Author.objects().filter(email__ilike="%@EXAMPLE.com").order_by("name").first()
both = await Author.objects().filter(id__in=["ada", "bob"]).exec()
assert [author.name for author in adults] == ["Ada"] and len(either) == len(both) == 2 and first is not None
assert await Author.objects().count() == 2 and await Author.objects().filter(age__lt=0).exists() is False
assert await Author.objects().avg("age") == 27

# Your own SurrealQL: values go in bound $parameters, never into the string.
older = await Author.objects().query("SELECT * FROM Author WHERE age > $min", {"min": 30})
assert [author.name for author in older] == ["Ada"]
```

- `Model.objects()` returns a new, lazy QuerySet; nothing is sent until `exec()`, `first()`, `get()`, `all()`,
  `count()`, `exists()` or an aggregation (`sum`, `avg`, `min`, `max`, `GROUP BY` with `values(…).annotate(…)`).
- Lookups: `exact`, `gt`, `gte`, `lt`, `lte`, `in`, `not_in`, `contains`, `not_contains`, `containsall`,
  `containsany`, `startswith`, `endswith`, `like`, `ilike`, `regex`, `match` (a regex too), `isnull`, and geometry
  lookups. Every filter value is bound as a parameter.
- `get()` raises `SurrealDbNotFoundError` when nothing matches; `first()` returns `None`.
- More: `fetch()` (record links), `select()`, `as_of()` (time travel), `cache()`, `search()`, `similar_to()`,
  `hybrid_search()`, `live()` and `changes()` each have a section in the guides.

## Writing data and transactions

```python
from surreal_orm_lite import SurrealDBConnectionManager

async with SurrealDBConnectionManager.transaction() as tx:
    await Author(id="cy", name="Cy", email="cy@example.com", age=40).save(tx=tx)
    await ada.merge(age=38, tx=tx)
    if tx.is_interactive:  # WebSocket + SurrealDB 3.x: reads in the block see its writes
        assert await Author.objects(tx=tx).count() == 3
# Committed when the block ends; nothing is written if it raises.

await Author.objects().bulk_create(
    [Author(id=f"x{i}", name=f"X{i}", email=f"x{i}@example.com", age=20 + i) for i in range(3)]
)
updated = await Author.objects().filter(id__in=["x0", "x1", "x2"]).bulk_update(age=30)
dan, created = await Author.objects().get_or_create(
    id="dan", defaults={"name": "Dan", "email": "dan@example.com", "age": 50}
)
deleted = await Author.objects().filter(id__in=["x0", "x1", "x2"]).bulk_delete()
assert updated == 3 and created and deleted == 3
```

- Every write takes `tx=`: `save`, `update`, `merge`, `upsert`, `delete`, `relate`, `call_function`, and QuerySets
  through `objects(tx=tx)`. A statement that fails inside the block fails the whole transaction, even if you catch it.
- `update_or_create()`, `patch()` and the atomic helpers (`atomic_increment`, `atomic_append`, …) avoid a
  read-modify-write; `@retry_on_conflict()` re-runs a function on `SurrealDbConflictError`.
- Relations: `await ada.relate("wrote", book)`, `await ada.get_related("wrote")`, or typed edges with
  `BaseSurrealEdge`. Signals (`pre_save`, `post_save`, …) fire on the model's writes, not on raw SurrealQL.

## SurrealDB 2.x, 3.x and 4.x

The same code runs on SurrealDB 2.6/2.7, 3.2/3.3, 4.0 and the embedded engine (a SurrealDB 4.0 core, which needs
`pip install "surreal-orm-lite[embedded]"`). Where a capability needs 3.x, the ORM degrades on 2.x or raises before
sending anything; 4.x behaves as 3.x except where the tables below say otherwise. Do not branch on the server version;
where it matters, test the capability (`tx.is_interactive`).

- **Transactions**: interactive on 3.x over WebSocket. On 2.x, or over HTTP, they are buffered and sent at commit:
  reads inside the block raise, `save(tx=)` needs an explicit id, and `bulk_update`/`bulk_delete` return `0`.
- **3.x only**: refresh tokens (`DEFINE ACCESS … WITH REFRESH`, `with_refresh=True`; `AuthTokens.refresh` is `None`
  on 2.x), asynchronous events (`Event(async_mode=True)`), `COMPUTED` fields in migrations.
- **Differs**: a field a `SCHEMAFULL` table does not define is dropped on 2.x and refused on 3.x and 4.x; a `Decimal`
  keeps its trailing zeros on 2.x; raw error types differ, the ORM's do not.
- **Order**: `first()` without `order_by()` returns the smallest matching id on 3.x and 4.x (`ORDER BY id`), the
  server's first row on 2.x; `exec()`, `all()` and pages are unordered without `order_by()` (4.x really is): add
  `order_by()` to page.
- **Raw queries**: `raw_query()` and `QuerySet.query()` return the rows of every statement, in order (a `LET` adds
  none); a failing statement raises. `get_client()` is the SDK 3.0 client: its `query()` returns one result per
  statement.
- **4.x migrations**: at compatibility level 4, narrowing a field's type, or adding a field refusing `NONE`, on a
  populated `SCHEMAFULL` table raises `FieldChangeRefusedError`, nothing applied: write a `RawSQL` that converts the
  rows.
- **Embedded engine**: no live queries (use change feeds), and `as_of()` needs `surrealkv+versioned://`.

Every difference is a row of the
[ORM behaviour tables](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/compatibility.md#orm-behaviour-surrealdb-2x-3x-and-4x)
and of the
[embedded engine table](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/compatibility.md#orm-behaviour-the-embedded-engine).

## Common mistakes

- **A missing `await`**, or `asyncio.run()` inside a running loop (Jupyter, FastAPI): await the coroutine instead.
- **SurrealQL built from strings.** Never put a value into SurrealQL with an f-string, `%` or `+`: pass it as a
  `$parameter` in `query(sql, {"name": value})` or `raw_query(sql, {...})`. Table and field names cannot be
  parameters: take them from your models, never from user input.
- **Raw SurrealQL where the QuerySet already does it**: filters, ordering, paging, counts, aggregations, `GROUP BY`
  (`values().annotate()`), bulk writes, upserts, relations and patches all have a method. `query()` must read
  `FROM` the model's table.
- **`update()` for a partial change**: it replaces the record and removes the fields the instance does not hold.
  Use `merge()`. `save()` never updates: it creates.
- **Untyped filter values on rich fields**: `filter(trial__gte=timedelta(days=7))`, not `"7d"`.
- **A client shared between event loops or threads**: let the manager make one per loop.
- **Full SurrealDB-ORM code**: this project is `surreal_orm_lite`, not `surreal_orm`. It has no custom SDK, no CBOR
  protocol and no connection pool; its factories have no `Sequence`, `LazyAttribute` or `SubFactory`; its `match`
  lookup is a regex, and full-text search is `search()`.

## Errors to catch

```python
from surreal_orm_lite import SurrealDbError, SurrealDbNotFoundError

try:
    await Author.objects().get("nobody")
except SurrealDbNotFoundError:
    missing = True
try:
    await Author(id="ada", name="Ada", email="ada@example.com", age=36).save()  # the id exists
except SurrealDbError:
    duplicate = True
assert missing and duplicate
```

- `SurrealORMError` is the base of every ORM error.
- `SurrealDbError`: the database refused or failed an operation. Subclasses: `SurrealDbNotFoundError` (no such
  record or resource), `SurrealDbConflictError` (a retryable transaction conflict) and `SurrealDbAuthenticationError`
  (sign-in, sign-up, tokens).
- `SurrealDbConnectionError`: the connection cannot be made, or a URL the SDK refuses.
- `SurrealDbValidationError`: data refused before it is sent. Pydantic's `ValidationError` comes from building a
  model; `ValueError` and `TypeError` from a wrong argument, before any request.

## Testing

```python
import random

from surreal_orm_lite.testing import Faker, ModelFactory, embedded_database


class AuthorFactory(ModelFactory[Author]):
    class Meta:
        model = Author

    name = Faker("name")
    email = Faker("email")
    age = Faker("random_int", min=18, max=80)


draft = AuthorFactory.build()  # no database
assert draft.get_id() is None and 18 <= draft.age <= 80

async with embedded_database():  # a fresh, empty in-memory database
    await AuthorFactory.create_batch(3)
    assert await Author.objects().count() == 3

random.seed(7)  # factories draw from `random`: seed it to repeat the data
```

The pytest plugin gives each test a fresh `mem://` database through the `surreal_db` fixture (it needs pytest and
pytest-asyncio). Enable it in the root `conftest.py` with
`pytest_plugins = ["surreal_orm_lite.testing.pytest_plugin"]`:

```python
import pytest


@pytest.mark.asyncio  # not needed with asyncio_mode = "auto"
async def test_authors(surreal_db):
    await AuthorFactory.create_batch(2)
    assert await Author.objects().count() == 2
```

`surreal_db` yields `None`: call `await SurrealDBConnectionManager.get_client()` for raw SurrealQL. It refuses a server
URL, so a test run cannot wipe a real database. Never put secrets or tokens in factories.

## Guides

- [Documentation index](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/README.md): every
  numbered section.
- [Getting started](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/getting-started.md),
  [Models](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/models.md),
  [Field types](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/fields.md),
  [Querying](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/querying.md),
  [Writing data](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/writing.md).
- [Relations and graph](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/relations.md),
  [Stored functions and events](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/functions-and-events.md),
  [Authentication](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/authentication.md),
  [Live queries and change feeds](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/realtime.md).
- [Connections](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/connections.md),
  [Embedded engine](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/embedded.md),
  [Schema and DDL](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/schema.md),
  [Search](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/search.md).
- [Migrations](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/migrations.md),
  [Command line](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/cli.md),
  [Testing](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/testing.md),
  [Compatibility](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/compatibility.md),
  [Contributing](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/contributing.md).
- [Migrating to 0.50](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/docs/migrating-to-0.50.md): the
  SDK 3.0 changes, from 0.40.x.
- [Changelog](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/blob/main/CHANGELOG.md) and
  [runnable examples](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/tree/main/examples).
