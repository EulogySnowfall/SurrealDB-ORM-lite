# Querying

The QuerySet: filters and lookups, `Q` objects, `FETCH`, aggregations, time travel, subqueries and the query cache.

## 4. QuerySet Methods

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

### What `select()` accepts

```python
await Book.objects().select("title", "price").exec()        # field names
await Book.objects().select("title", "author.name").exec()  # [{"title": "Dune", "author": {"name": "Ada"}}, …]
await Book.objects().select("title", "author.*").exec()     # every field of the linked author
await Book.objects().select("*", "author.*").exec()         # every field, the author's record included
```

Each item is `*`, a field name (an aliased field by its Python name: its column is selected), a
dotted path into a nested object or through a record link, or such a path followed by `.*`. Names
are letters, digits and underscores, each part starting with a letter or an underscore, as in
`order_by()` and `values()`. Anything else — an expression, a function call, `AS`, `{…}` or
`[…]` — raises `ValueError` naming the item when `select()` is called, before any request; write a
computed projection with `query()` and bound parameters. Rows that do not validate as the model (a
link read as a record, a required field left out) come back as dicts keyed by Python names. On a
nested object that is not a link, `"meta.*"` gives the object on SurrealDB 3.x and its values as a
list on 2.x and the embedded engine.

### `limit()` and `offset()`

```python
users = await User.objects().order_by("name").limit(10).offset(20).exec()
User.objects().limit(int(size))   # a size read as text: convert it first
User.objects().limit("10")        # TypeError: limit() takes a non-negative integer, got '10'
User.objects().offset(-1)         # ValueError: offset() takes a non-negative integer, got -1
```

Each takes a non-negative integer (`0` reads no record; an `IntEnum` member or an integer type
such as NumPy's `int64` gives its value). A value that is not an integer — a `bool`, a `str` such
as `"10"`, a `float`, `None` — raises `TypeError`, and a negative one or one above `2**63 - 1`
(SurrealQL's largest integer) `ValueError`, naming the method and the value, when `limit()` or
`offset()` is called, before any request; the value in place is kept. The same holds wherever the
queryset is read: `exec()`, `first()`, `get()`, `values(…).annotate(…)`, `cache()`, `as_of()` and
a subquery. The server may still refuse a large value when the read runs: SurrealDB 2.x and the
embedded engine refuse one above `4294967295` (`2**32 - 1`), SurrealDB 3.x reads it.

## Supported Filter Lookups

- `exact` (default)
- `gt`, `gte`, `lt`, `lte`
- `in`, `not_in` (a list, or a `Range` bound as one value: see [section 24](fields.md#24-rich-field-types))
- `contains`, `not_contains` (`contains` on a range column tests membership)
- `containsall`, `containsany`
- `startswith`, `endswith`, `like`, `ilike`, `match`, `regex` (text: see [Text lookups](#text-lookups))
- `isnull`
- `inside`, `outside`, `intersects` (geometry values: see [section 25](fields.md#25-geospatial-fields))
- a subquery — `Subquery(qs)` or the queryset itself — as the value of `in`, `not_in`, `containsall`, `containsany` (all its values) or `exact`, `gt`, `gte`, `lt`, `lte`, `contains`, `not_contains` (its first value): see [section 28](#28-subqueries-subquery)

Filter values on rich fields must be typed: `filter(trial__gte=timedelta(days=7))`, not
`"7d"`. Strings are not converted (see [section 24](fields.md#24-rich-field-types)).

### Text lookups

SurrealQL has no `LIKE` or `STARTSWITH` operator, so each text lookup compiles to a string
function, with its value bound:

| Lookup       | Value                      | Compiles to                                                                      |
| ------------ | -------------------------- | -------------------------------------------------------------------------------- |
| `startswith` | a prefix                   | `string::starts_with(field, $v)`                                                 |
| `endswith`   | a suffix                   | `string::ends_with(field, $v)`                                                   |
| `like`       | an SQL `LIKE` pattern      | `string::matches(field, $v)`, the pattern translated to an anchored `(?s)` regex |
| `ilike`      | the same, case-insensitive | the same, with `(?i)` added                                                      |
| `regex`      | a regular expression       | `string::matches(field, $v)`                                                     |
| `match`      | a regular expression       | `string::matches(field, $v)`: the same as `regex`                                |

```python
await User.objects().filter(name__startswith="A").exec()
await User.objects().filter(email__ilike="%@EXAMPLE.com").exec()   # case-insensitive
await User.objects().filter(code__like="AB_-%").exec()            # "AB", any one character, "-", anything
await User.objects().filter(name__regex=r"^\d{3}").exec()          # SurrealDB's regex syntax (Rust regex)
```

- **Non-string fields.** Each condition is guarded by the field's type:
  `filter(name__startswith="A")` is `((name).is_string() AND string::starts_with(name, $_f0))`.
  A record whose field is missing (`NONE`), `NULL` or not a string never matches, and the
  query does not fail on it. The negation (`~Q(name__startswith="A")`) matches it. A dotted
  path through an array of record links or objects (`authors.name`) holds an array, so it
  never matches either, on both lines: the parentheses make SurrealDB 3.x test that array,
  not each of its elements.
- **`like` / `ilike` patterns.** `%` matches any run of characters (newlines included) and `_`
  exactly one character. Every other character is literal, regex metacharacters included
  (`.`, `*`, `(`, `\`, …). There is no escape character, so a pattern cannot match a literal
  `%` or `_` alone: use `regex` for that. The whole value must match, so `like="Al"` finds
  `"Al"` but not `"Alice"`.
- **`match` and `regex`.** Both take a regular expression in SurrealDB's syntax (the Rust
  `regex` crate) and match anywhere in the value unless it is anchored (`^…$`). Prefix it
  with `(?i)` to ignore case. Here `match` is a regex lookup, not the full SurrealDB-ORM's
  full-text `@@` operator: full-text search is the separate `search()` API
  ([section 36](search.md#36-full-text-search-search-searchscore-searchhighlight-analyzer-index)).
- **Values.** The value must be a `str`, otherwise `ValueError`. A string starting with `$`
  is data, as for every lookup ([String filter values and `Var()`](#string-filter-values-and-var)).
  `startswith`, `endswith`, `match` and `regex` also take a `Var`. `like` and `ilike` do not:
  their pattern is translated before it is sent, so `Var` raises `ValueError`.

### Filtering on the record id

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

### String filter values and `Var()`

```python
from surreal_orm_lite import Var

await User.objects().filter(name="$admin").exec()                                  # the string "$admin"
await User.objects().filter(age__gte=Var("min_age")).variables(min_age=21).exec()  # … WHERE age >= $min_age
```

A string filter value is data, whatever it starts with: `filter(name="$admin")` compares `name`
with the string `"$admin"`, bound like any other value, wherever a filter applies: `filter()`, `Q`,
`get()`, `get_or_create()`, `update_or_create()`, `bulk_update()` by filter, subqueries and live
queries. `Var(...)` is the only way to reference a query variable, whose value `variables()`
gives.

Until v0.28.1 a value starting with `$` was read as a variable reference (deprecated since
v0.14.3), and `"$$admin"` was how to write the string `"$admin"`. That escape keeps its meaning,
deprecated: a filter value starting with `$$` loses one `$` (`"$$admin"` finds `"$admin"`: write
`"$admin"`), with a `DeprecationWarning`; an item of an `in` list never had the escape.

A call written for the old reading, such as `filter(age__lte="$max_age").variables(max_age=25)`,
now compares `age` with the string `"$max_age"`: a number sorts below a string, so `lt` and `lte`
match every number, and `gt`, `gte` and `exact` none. When a string filter value is exactly `$`
followed by the name of a variable that `variables()` gives, the ORM keeps it as data and emits a
`UserWarning` that says to write `Var("max_age")`, once per read, pointing at your line.

`Var()` also reads the parameters SurrealDB sets for each session, without `variables()`: `$auth`
(the signed-in record user), `$access`, `$session` and `$token`, in reads and in live queries.

```python
mine = await Doc.objects().filter(owner=Var("auth")).exec()  # … WHERE owner = $auth
```

The string `"$auth"` is data: `filter(owner="$auth")` compares `owner` with the string `"$auth"`,
so a read keeps only the records whose `owner` is that string, and a live query notifies only for
those. A string filter value that is exactly `"$auth"`, `"$access"`, `"$session"` or `"$token"`
emits a `UserWarning` naming the `Var(...)` that reads the parameter, once per read, pointing at
your line. Any other string, such as `"$x"` with no variable `x`, is data the caller meant: no
warning. On an edge's `in` and `out`, which hold record ids, such a string is not a record id and
raises `ValueError`; when it names a variable of `variables()` or one of these parameters, the
message names the `Var(...)` to write.

`get_or_create()` and `update_or_create()` look up their criteria and write the `exact` ones on
create. Their lookup binds `variables()`, as `filter()` does, so a `Var` criterion reads the
variable there too, and an `exact` one is written as the variable's value on create: the next
identical call finds the record.

```python
qs = Room.objects().variables(t="mine", n=10)
room, created = await qs.get_or_create(title=Var("t"))  # as get_or_create(title="mine")
room, created = await qs.update_or_create(size__gte=Var("n"), title=Var("t"), defaults={"floor": 2})
```

On the record id and on an edge's `in` and `out`, such a criterion is compared as the record id
its value names, as the same criterion given that value is: with `variables(a="person:alice")`,
`get_or_create(in_=Var("a"), out=bob)` finds or relates `person:alice`. A value that names no record
id there raises `ValueError` before any request. A model instance given to `variables()` is bound
and written as its record id, as the instance given as the criterion is (`variables(a=alice)`); an
unsaved one raises `ValueError` before any request. The `variables()` of a subquery criterion share
the lookup, as in `filter()`: one that binds a name of the queryset's `variables()` to another
value raises `ValueError`. `Var("auth")` finds the signed-in user's record
(`get_or_create(owner=Var("auth"))` returns it) but cannot be written: with no record to return, it
raises `SurrealDbError`, and so does an `exact` `Var` that names no variable of `variables()`.

A string criterion is refused where it would be a trap: `update_or_create(owner="$auth", defaults=…)`
would find and write the record whose `owner` is the string `"$auth"`, the same record for every
record user, and `members__contains="$auth"` would look for the string. A criterion that is exactly
`$` followed by the name of one of these parameters or of a variable the lookup binds (of
`variables()`, the queryset's or a subquery criterion's) raises `SurrealDbError` before any request,
whatever its lookup. For a variable, the message names `Var("x")` as the criterion to write, under
any lookup but `like`/`ilike`, which take no `Var` (pass the pattern itself, or use `regex`). For a
parameter, it says to pass the actual value as an `exact` criterion (the record id, or the value
itself), and that `Var("auth")` works under any other lookup but `like`/`ilike`, which take no `Var`
(pass the pattern itself, or use `regex`): `members__contains=Var("auth")` is looked up, never
written. Any other string, such as `"$admin"`, is looked up and written as it is. On an edge's `in`
and `out`, such a criterion raises the `ValueError` above.

`variables()` refuses these four names with a `ValueError`: SurrealDB sets them for each session
and refuses a query variable of that name. To pass your own value, give it another name, or pass
the value itself in the filter: `filter(token=Var("device_token")).variables(device_token=t)` or
`filter(token=t)`, in a read or a live query. `Var("token")` without `variables()` reads SurrealDB's
`$token`.

### Model instances as filter values

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
(see [section 23](relations.md#23-typed-edges-basesurrealedge)).

### Fields ending in an underscore

```python
class Item(BaseSurrealModel):
    id: str | None = None
    type_: int = 0             # trailing underscore: `type` is a builtin


heavy = await Item.objects().filter(type___gt=1).exec()   # the gt lookup on type_
```

A lookup on a field whose name ends in `_` takes three underscores: the field's own, then the
`__` separator. `type___gt` is `gt` on `type_`, and `in___in` is `in` on an edge's `in_`. Before
v0.22.0 such a key raised "Unsupported lookup".

## 5. Q Objects (Complex Queries)

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

Since v0.27.0 a lookup inside a `Q` takes a subquery as `filter()` does (see
[section 28](#28-subqueries-subquery)), here with `admins = User.objects().filter(role="admin")`:

```python
posts = await Post.objects().filter(Q(author__in=Subquery(admins)) | Q(score__gte=9)).exec()
```

## 8. FETCH Clause

```python
# Resolve record links inline (prevents N+1 queries)
posts = await Post.objects().fetch("author", "tags").exec()
# Generates: SELECT * FROM Post FETCH author, tags;
```

## 9. Aggregations

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

## 27. Time travel (`as_of()`)

Since v0.26.0 a QuerySet can read a model's records **as they were at a moment**: `as_of()` adds
SurrealQL's `VERSION` clause to every read of the queryset. The store must keep history (see
[Stores that keep history](#stores-that-keep-history)): the embedded engine's
`surrealkv+versioned://` keeps it on disk, with no server.

```python
import tempfile

from surreal_orm_lite import BaseSurrealModel, SurrealDBConnectionManager, SurrealDbNotFoundError


class Product(BaseSurrealModel):
    id: str | None = None
    name: str
    price: float


path = tempfile.mkdtemp()
SurrealDBConnectionManager.set_connection(
    url=f"surrealkv+versioned://{path}/shop", namespace="shop", database="shop"
)
client = await SurrealDBConnectionManager.get_client()

lamp = Product(id="lamp", name="Lamp", price=40.0)
desk = Product(id="desk", name="Desk", price=250.0)
await lamp.save()
await desk.save()
opening = await client.query("RETURN time::now();")  # a moment, from the database's clock
await lamp.merge(price=30.0)
sale = await client.query("RETURN time::now();")
await desk.delete()

then = await Product.objects().as_of(opening).order_by("name").exec()
assert [(product.name, product.price) for product in then] == [("Desk", 250.0), ("Lamp", 40.0)]
assert await Product.objects().as_of(sale).count() == 2
assert await Product.objects().count() == 1

# A record deleted since is still found at that moment; one created later is not.
assert (await Product.objects().as_of(sale).get("desk")).price == 250.0
try:
    await Product.objects().get("desk")
except SurrealDbNotFoundError:
    print("no desk now")
```

### Stores that keep history

| Where the read runs                                                                               | `as_of()`                                                                                |
| ------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Embedded `surrealkv+versioned://<path>`                                                           | ✅ (SDK 2.0.0 ignores `?versioned=true` on `surrealkv://`)                               |
| Embedded `mem://`, `memory://`, `surrealkv://`, `file://`                                         | `SurrealDbError` before any request                                                      |
| SurrealDB 3.x started on `surrealkv://<path>?versioned=true` or `rocksdb://<path>?versioned=true` | ✅                                                                                       |
| SurrealDB 3.x in memory, or on `surrealkv://` or `rocksdb://` without `versioned=true`            | `SurrealDbError`: the server refuses `VERSION` (the SDK's error is its `__cause__`)      |
| SurrealDB 2.x started on `surrealkv+versioned://<path>`                                           | ✅                                                                                       |
| SurrealDB 2.x on RocksDB                                                                          | `SurrealDbError`: the server refuses `VERSION`                                           |
| SurrealDB 2.x in memory or on plain `surrealkv://`                                                | no error, and the ORM cannot tell (see [below](#surrealdb-2x-without-versioned-storage)) |

```bash
surreal start --user root --pass root "surrealkv:///var/lib/surrealdb?versioned=true"  # SurrealDB 3.x
surreal start --user root --pass root "surrealkv+versioned:///var/lib/surrealdb"       # SurrealDB 2.x
```

### The moment

`as_of()` takes a moment, "how long ago", a server expression, or `None`:

```python
from datetime import UTC, datetime, timedelta

from surreal_orm_lite import Duration, SurrealFunc

Product.objects().as_of(datetime(2026, 1, 1, 9, 30, tzinfo=UTC))      # an aware datetime, any time zone
Product.objects().as_of(datetime(2026, 1, 1, 9, 30))                  # a naive one is UTC
Product.objects().as_of("2026-01-01T09:30:00+02:00")                  # ISO-8601, parsed by the ORM
Product.objects().as_of("2026-01-01")                                 # a date alone: midnight UTC
Product.objects().as_of(timedelta(hours=1))                           # an hour before the database's clock
Product.objects().as_of(Duration.parse("1h"))                         # the ORM's Duration too
Product.objects().as_of(SurrealFunc("time::floor(time::now(), 1d)"))  # evaluated by the database
Product.objects().variables(cutoff=opening).as_of(SurrealFunc("$cutoff"))
Product.objects().as_of(opening).as_of(None)                          # None: the current state again

assert await Product.objects().as_of(timedelta(0)).count() == 1                     # now
assert await Product.objects().as_of(SurrealFunc("time::now() - 1h")).count() == 0  # before the shop
```

- A naive `datetime` or ISO-8601 string is UTC, as for every datetime the ORM sends; a date alone
  is that day's midnight UTC, while a `date` object is refused (`TypeError`), and so are numbers,
  `bool` and `bytes`. A string that is not ISO-8601 raises `ValueError`.
- A `timedelta` or an ORM `Duration` (>= 0) means that long before the **database's** clock, and a
  `SurrealFunc` is an expression the database evaluates to a datetime: both are resolved by the
  database just before each read (one `RETURN` request). A `SurrealFunc` is written into that
  `RETURN` as is: build it in your code, never from user input, and pass user values through
  `variables()` (bound), as `$cutoff` above. `_as_of` is a reserved `variables()` name: the
  moment is bound under it.
- The surrealdb SDK's own `Duration` (`surrealdb.Duration`) is accepted at runtime only: the
  annotation names the ORM's types, so a type checker flags it. `Duration(sdk_duration.elapsed)`
  converts it.
- Called again, the last moment wins. A moment after the last write reads the current state; a
  moment before the table existed reads no records.

**The moment must lie between 1970-01-01T00:00:00Z and 2262-04-11T23:47:16.854775Z**, the
instants SurrealDB versions can address (nanoseconds since 1970 in a signed 64-bit integer).
SurrealDB 2.x reads an earlier moment as the **current** state, without an error, and every line
fails after the later one, so the ORM refuses both, on every line: an absolute moment at the call
(`ValueError`), a duration or an expression once the database has resolved it, before the read is
sent (`SurrealDbError`).

```python
try:
    Product.objects().as_of(datetime(1900, 1, 1))
except ValueError as error:
    print(error)
# as_of() takes a moment from 1970-01-01T00:00:00+00:00 to 2262-04-11T23:47:16.854775+00:00, the
# instants SurrealDB versions can address (SurrealDB 2.x reads an earlier moment as the current
# state), got 1900-01-01T00:00:00+00:00
```

### Reads

Every read of the queryset honours the moment, with `filter()`, `Q`, `nearby()`, `order_by()`,
`limit()`, `offset()`, `select()` and `variables()`, on edge models too:

| Read                                                      | Under `as_of()`                                                                            |
| --------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `exec()`, `first()`, `get()` with filters                 | the records as they were, as snapshots (a row that does not validate comes back as a dict) |
| `get("id")`                                               | the record as it was: found if deleted since, `SurrealDbNotFoundError` if created later    |
| `all()`                                                   | every record as it was, as snapshots (`all()` still ignores `filter()`)                    |
| `exists()`, `count()`, `sum()`, `avg()`, `min()`, `max()` | the values at that moment                                                                  |
| `values(…).annotate(…)`                                   | the groups at that moment, as dicts                                                        |
| `annotate(d=GeoDistance(…))`                              | snapshots carrying their distances                                                         |

A plain `SELECT … VERSION` is wrong on both server lines, so the ORM never sends one on its own.
SurrealDB 3.x's table reads return the records deleted before the moment, in their last state
(so filtered counts, sums and groups count them too); SurrealDB 2.x and the embedded engine
miscount an unfiltered `count()`. Every time-travel table read therefore also reads each record
by its id at the moment, which is right on every versioned store:

```sql
SELECT * FROM Product WHERE (SELECT VALUE id FROM ONLY $parent.id VERSION $_as_of) != NONE VERSION $_as_of;
```

It costs about one extra point read per record scanned; a read by id (`get("lamp")`) needs no
check. The moment is always a bound parameter.

### Snapshots

The instances a time-travel read returns are **snapshots**, read-only: `save()`, `update()`,
`merge()`, `patch()`, `upsert()`, `delete()`, the `atomic_*()` methods, `relate()`,
`remove_relation()` and `remove_all_relations()` raise `SurrealDbError` before any signal or
request, and `bulk_create()` refuses a snapshot in its list. A refused write leaves a transaction
as it was (no statement, its table not counted as written) and drops no cached read. To restore a
past state, build a new instance from the snapshot:

```python
from surreal_orm_lite import SurrealDbError

old_lamp = await Product.objects().as_of(opening).get("lamp")
try:
    await old_lamp.save()
except SurrealDbError as error:
    print(str(error).split(":")[0])
# Product.save() cannot run on an instance read with as_of()

# The record exists: update() writes the past state back.
await Product(**old_lamp.model_dump()).update()
assert (await Product.objects().get("lamp")).price == 40.0

# It was deleted since: save() creates it again (an "undelete").
old_desk = await Product.objects().as_of(sale).get("desk")
await Product(**old_desk.model_dump()).save()
assert await Product.objects().count() == 2

# refresh() turns a snapshot back into the current record: its writes work again.
await old_lamp.refresh()
await old_lamp.merge(price=35.0)

# A snapshot is never == the current instance, even with the same fields.
current_desk = await Product.objects().get("desk")
assert old_desk != current_desk and old_desk.model_dump() == current_desk.model_dump()
```

- **Auth models**: on a model mixing `AuthenticatedUserMixin`, `refresh` is the mixin's
  token-exchange classmethod; call `await BaseSurrealModel.refresh(snapshot)`.
- **Equality**: Pydantic compares private attributes, and the snapshot's moment is one: compare
  `model_dump()`s.
- **`server_fields`**: `save()` leaves them out, so an undelete by `save()` lets the server's
  `DEFAULT` fill them again; `update()` and `upsert()` write them back.
  `await Product(**snapshot.model_dump()).upsert()` restores the past state exactly, whether the
  record still exists or not (not on edge models, whose `upsert()` always raises). Computed fields
  are recomputed by the server in every case.
- **Copies**: `model_copy()` and `pickle` keep the snapshot; an instance pickled by v0.25.x
  unpickles as a current record.
- **Nested records**: a record model nested in a snapshot's fields is a snapshot too, such as a
  link `fetch()` resolves into a model-typed field (`author: Author | RecordID | None`, see
  [Record links](#record-links)): it holds the linked record as it was. Its own `refresh()` makes
  it the current record; the snapshot's `refresh()` re-reads that record only.
- **Graph reads** stay available: `get_related()` and `traverse()` read the current graph.
- When the record was deleted since, `refresh()` raises its usual `SurrealDbError` and the
  instance stays a snapshot.

### What a time-travel queryset refuses

A time-travel read is read-only and cannot be streamed: `bulk_create()`, `bulk_update()`,
`bulk_delete()`, `patch()`, `update_or_create()`, `get_or_create()`, `delete_table()`, `query()`,
`live()`, `watch()` and `changes()` raise `SurrealDbError` on a queryset with `as_of()`, before
any request and without dropping any cached read. History cannot join a transaction:
`Model.objects(tx=...).as_of(...)` raises at the call. On the embedded engine, a URL that keeps
no history raises before any request, naming `surrealkv+versioned://`.

### SurrealDB 2.x without versioned storage

A SurrealDB 2.x server in memory or on plain SurrealKV answers a time-travel read **without an
error**: it returns only the records last written at or before the moment, in their **current**
state, and nothing tells the ORM which storage the server runs, so it cannot tell. Start 2.x with
`surrealkv+versioned://` for time travel. A 3.x server without versioned storage refuses the read
(`SurrealDbError`), and so does 2.x on RocksDB.

### Record links

`fetch()` resolves links as they were on SurrealDB 3.x. SurrealDB 2.x (servers, and the embedded
engine of the surrealdb SDK 2.x) resolves them to their current state, so on 2.x the reads that
return the fetched records (`exec()`, `first()`, `get()` without an id) raise `SurrealDbError`;
the other reads ignore `fetch()`, as without `as_of()`. Read the linked records with their own
`as_of()` query. On 3.x, a link fetched into a model-typed field is a snapshot too, read-only. A
dotted path through a link (`author.name`) in `filter()`, `select()`, `order_by()` or `values()`
reads the linked record as it was on 3.x and as it is now on 2.x.

### Writing history at a past moment

The ORM writes now. A raw query can write a record at a past moment, one statement per
`client.query()` call:

```python
await client.query("CREATE Product:stool SET name = 'Stool', price = 15.0 VERSION d'2020-01-01T00:00:00Z';")
# SurrealDB 2.x and the embedded engine: stored at that moment, readable with as_of()
assert await Product.objects().as_of(datetime(2021, 1, 1, tzinfo=UTC)).count() == 1
```

SurrealDB 3.x accepts the statement, but the record is not readable at that moment (the table did
not exist then), and a `CREATE` of an id that exists raises.

### Coming from the full SurrealDB-ORM

- **The full ORM has no time travel** (no `VERSION` support): `as_of()`, snapshots and their
  errors are lite additions.
- **`as_of()`, not `version()`**: the name is temporal SQL's `AS OF`. SurrealQL's keyword is
  `VERSION` and the JavaScript SDK has `select().version()`, but on the Python SDK's connection
  `version()` returns the server's version.
- **A snapshot is never `==` the current instance**, even with identical fields: compare
  `model_dump()`s.

## 28. Subqueries (`Subquery`)

Since v0.27.0 a QuerySet can filter another: `Subquery(qs)`, or the queryset itself, is the value
of a lookup in `filter()` or `Q`. The ORM sends **one request**: the server runs the inner
queryset inside the outer statement, so both read the same state (one consistent read), and the
inner queryset's values are bound like any filter value.

```python
from surrealdb import RecordID

from surreal_orm_lite import BaseSurrealModel, Subquery


class Person(BaseSurrealModel):
    id: str | None = None
    name: str
    active: bool = True
    age: int = 0


class Post(BaseSurrealModel):
    id: str | None = None
    title: str
    author: RecordID | None = None  # a record link to a Person
    score: int = 0


people = [("ada", "Ada", True, 36), ("bob", "Bob", True, 50), ("cy", "Cy", False, 20)]
for key, name, is_active, age in people:
    await Person(id=key, name=name, active=is_active, age=age).save()
for key, title, author, score in [
    ("p1", "one", "ada", 5),
    ("p2", "two", "bob", 8),
    ("p3", "three", "cy", 2),
    ("p4", "four", "ada", 9),
    ("p5", "five", None, 1),  # no author
]:
    link = RecordID("Person", author) if author else None
    await Post(id=key, title=title, author=link, score=score).save()

active = Person.objects().filter(active=True)
posts = await Post.objects().filter(author__in=Subquery(active)).exec()  # p1, p2, p4
same = await Post.objects().filter(author__in=active).exec()  # p1, p2, p4 (a QuerySet is a subquery)
```

### What a subquery selects

The inner queryset gives the outer filter **one list of values**: without `select()`, the ids of
its records (`SELECT VALUE id`); with `select("field")`, the values of that one field (an aliased
field compiles its column). Here the subquery gives the record links the posts hold:

```python
top_authors = Post.objects().filter(score__gt=8).select("author")
writers = await Person.objects().filter(id__in=top_authors).exec()  # ada
```

`filter()`, `Q`, `nearby()`, `order_by()`, `limit()`, `offset()` and `variables()` apply to the
inner queryset, and `fetch()` on the inner queryset is ignored, as in the full ORM. `select()` of
several fields, `values()` and `annotate()` raise `ValueError`: a list of objects would match
nothing.

Values are compared as stored, never converted: to match records, select `id` or a record link (a
`"table:key"` string never equals a record id). A record without the selected field gives `NONE`,
which `in` matches with a record whose field is missing, as a list holding `None` does:
`Post.objects().filter(author__in=Post.objects().select("author"))` returns p5 too, and
`author__not_in=active` keeps it.

### Lookups

| Lookup                                        | Compares the field with                                     |
| --------------------------------------------- | ----------------------------------------------------------- |
| `in`, `not_in`                                | all the subquery's values: equal to one of them, or to none |
| `containsall`, `containsany`                  | all its values: the array field holds all of them, or any   |
| `exact` (no suffix), `gt`, `gte`, `lt`, `lte` | its **first** value                                         |
| `contains`, `not_contains`                    | its first value: the array field holds it, or lacks it      |

Order the subquery to choose its first value. A first-value lookup on an empty subquery matches
**no record**, and `~Q(...)` of it every record: SurrealDB alone would compare with
`array::first([])`, which is `NONE`, and `score > NONE` holds for every record, so the ORM guards
the comparison (`$_sq0 != [] AND …`). For `in`, an empty subquery is an empty list: `in` matches
no record, `not_in` every record. `isnull`, `like`, `ilike`, `startswith`, `endswith`, `match`,
`regex`, `inside`, `outside` and `intersects` raise `ValueError` with a subquery.

```python
from surreal_orm_lite import Q

others = await Post.objects().filter(author__not_in=active).exec()  # p3, p5 (p5 has no author)
how_many = await Post.objects().filter(author__not_in=active).count()  # 2

best = Post.objects().select("score").order_by("-score").limit(1)
top = await Post.objects().filter(score=Subquery(best)).exec()  # p4 (the top score, 9)

nothing = Post.objects().filter(score__gt=100).select("score")
above = await Post.objects().filter(score__gt=Subquery(nothing)).exec()  # []
not_above = await Post.objects().filter(~Q(score__gt=Subquery(nothing))).exec()  # p1, p2, p3, p4, p5
```

### Ordered and limited subqueries

`order_by()`, `limit()` and `offset()` choose the values a subquery gives:

```python
oldest = Person.objects().filter(active=True).order_by("-age").limit(1)
theirs = await Post.objects().filter(author__in=oldest).exec()  # p2 (Bob is the oldest active person)
```

A statement with subqueries is one block, whose `LET`s bind each subquery once, innermost first:

```sql
{ LET $_sq0 = (SELECT id AS _sqv, age FROM Person WHERE active = $_f0 ORDER BY age DESC LIMIT 1)._sqv; (SELECT * FROM Post WHERE author IN $_sq0) };
```

- **Why a `LET`**: SurrealDB 3.2.0 to 3.3.4 can return wrong rows for an inline sub-SELECT with
  `ORDER BY` and `LIMIT`, which gives its value to the first outer record only
  ([surrealdb/surrealdb#7470](https://github.com/surrealdb/surrealdb/issues/7470), fixed in 3.3.5).
  Bound once, the subquery is right on every release, and the server runs it once instead of once
  per record.
- **Why `_sqv`**: an ordered subquery selects its column as `_sqv`, with its order fields, and
  reads `._sqv` back: SurrealDB 2.x refuses to order a sub-SELECT of one column by another field.
- **Reserved names**: `$_sq0`, `$_sq1`, … (`_sq<N>`) are the block's `LET`s and hide a
  `variables()` value of the same name; a subquery can order by `_sqv` only when it selects `_sqv`
  itself (`ValueError` otherwise).
- `limit()` and `offset()` of a subquery take a non-negative integer, as everywhere (`TypeError` or
  `ValueError` when they are called).

### Where subqueries apply

Every read honours a subquery (`exec()`, `first()`, `get()` with filters, `exists()`, `count()`,
`sum()`, `avg()`, `min()`, `max()`, `values(…).annotate(…)`, `annotate(GeoDistance)`), and so do
the writes by filter (`bulk_update()`, `bulk_delete()`, `QuerySet.patch()`) and the lookups of
`get_or_create()` and `update_or_create()`. Subqueries nest, and combine with `Q` (`&`, `|`, `~`):

```python
from surreal_orm_lite import Count

wrote_a_top_post = Person.objects().filter(id__in=top_authors)
by_writers = await Post.objects().filter(author__in=wrote_a_top_post).exec()  # p1, p4

inactive = Person.objects().filter(active=False)
mixed = await Post.objects().filter(Q(author__in=inactive) | Q(score__gte=9)).exec()  # p3, p4

total = await Post.objects().filter(author__in=active).sum("score")  # 22
per_author = Post.objects().filter(author__in=active).values("author").annotate(n=Count())
groups = await per_author.exec()  # ada: 2, bob: 1

updated = await Post.objects().filter(author__in=inactive).bulk_update(score=0)  # 1
post, created = await Post.objects().get_or_create(author__in=active, title="one")  # p1, False
```

- **Transactions**: in `Model.objects(tx=tx)` the statement runs in the transaction. An
  interactive transaction (SurrealDB 3.x over WebSocket) reads its own uncommitted writes in the
  subquery too; a buffered one (2.x, or HTTP) still cannot read, and applies writes by filter at
  commit. Build the inner queryset with the same `objects(tx=)`, or with none.
- **Live queries**: `live()` and `watch()` write the sub-SELECT inline in the `LIVE SELECT`, its
  values as literals, and the server evaluates it again on every change: a post created once its
  author is active notifies, even when the author became active after the live query started.
  Before it starts, a live query reads each subquery table (one request): SurrealDB 3.x would
  accept a live query whose subquery table does not exist and never notify, so the ORM raises
  `SurrealDbNotFoundError` there; 2.x reads that table as empty.
- **Time travel**: under `as_of()` every subquery reads at the same moment (the ORM gives it the
  moment's `VERSION` and the per-record check of [section 27](#27-time-travel-as_of)). Without
  versioned storage the read raises section 27's `SurrealDbError`; SurrealDB 3.1.5 raises its own
  `ThrownError` ("Failed to scan record") instead.
- **`changes()`** refuses `filter()`, so subqueries too.
- **A subquery table that does not exist** reads as empty, as the ORM reads any missing table.
  SurrealDB 2.x and the embedded engine read it so in the same request. SurrealDB 3.x raises: the
  ORM then asks the server whether that table exists (a table probe) and, once the server confirms
  it does not, sends the statement again with that subquery empty: three requests (statement,
  table probe, re-send) for one missing table, and only then. Any other error, even one whose
  text quotes "The table '…' does not exist", is the server's own: it is relayed, never read as
  empty. On 3.x a write by filter inside a transaction fails with the server's error
  (`SurrealDbError`), as any failed write in a transaction does.

### What a subquery cannot do

Each of these raises when the outer queryset compiles, before any request (`live()` and `watch()`
at the call):

- a lookup other than the eleven above (`isnull`, `like`, `startswith`, `inside`, …): `ValueError`;
- a queryset that is its own subquery, directly or through another one: `ValueError`;
- an inner `values()`, `annotate()` or `select()` of several fields: `ValueError`;
- an inner `as_of()`: `SurrealDbError` (call `as_of()` on the outer queryset);
- an inner `objects(tx=)` whose transaction is not the outer queryset's: `SurrealDbError`;
- an inner `variables()` binding a name of the statement to another value: `ValueError` (the two
  values must have the same type and be equal: `1` and `True`, or `1` and `1.0`, differ);
- an inner `order_by()` on `_sqv` (above): `ValueError`;
- a subquery inside a list (`author__in=[Subquery(qs)]`): `ValueError`, give it as the whole value;
- `Q(...).to_sql()` called on its own with a subquery lookup: `ValueError` (only a QuerySet binds
  a subquery);
- a subquery written as data: `bulk_update(score=Subquery(qs))` raises `ValueError`, and an
  `exact` criterion or a `defaults=` value of `get_or_create()` or `update_or_create()` raises
  `SurrealDbError` (use a lookup such as `author__in`, and pass the value to write in `defaults=`);
- `Subquery(x)` for anything but a QuerySet: `TypeError`.

`annotate(x=Subquery(...))` (a subquery's value on each row), correlated subqueries and `Exists`
are not available yet.

### Coming from the full SurrealDB-ORM

- **The same `Subquery(qs)`**, imported from `surreal_orm_lite`; a QuerySet given as the value is
  accepted too (`author__in=active`).
- **No `select()` means the record ids** (`SELECT VALUE id`): the full ORM writes `SELECT *`, which
  `in` never matches. `select()` of several fields is refused instead of matching nothing.
- **One statement**: a block whose `LET`s bind the subqueries, where the full ORM sends a `LET`
  prelude, two statements, whose rows the official SDK's `query()` cannot read (it returns the
  first statement's result only).
- **A first-value lookup on an empty subquery matches nothing**: the full ORM compares with
  `array::first()` of it, `NONE`, so `gt` matches every record.
- **`isnull`, the text lookups and the spatial lookups are refused** with a subquery.
- **`annotate(x=Subquery(...))` is not available yet.**
- **`live()` keeps the sub-SELECT inline**, in both.
- **A subquery table that does not exist reads as empty**, like any missing table in the lite; the
  full ORM raises `TableNotFoundError` on SurrealDB 3.x.
- **`Subquery()` raises `TypeError`** for anything but a QuerySet, and has no public `to_surql()`.
- **The inner queryset's `variables()` are merged** into the statement (a clash raises
  `ValueError`) and its **`nearby()` applies**, where the full ORM's subquery drops both.
- **The inner `fetch()` is ignored**, as in the full ORM, while `values()` and `annotate()` are
  refused.

## 29. Query cache (`cache()`, `QueryCache`)

Since v0.28.0 a QuerySet can serve its reads from a **client-side cache**: with `cache(ttl)`, the
rows a read returned stay in this process's memory, and the same read is answered from there,
without a request, until its TTL runs out or an ORM write changes what it reads. Invalidation is
built in: every ORM write removes the cached reads it can change, with nothing to set up. The
cache behaves the same on SurrealDB 2.x, 3.x and the embedded engine.

```python
from surreal_orm_lite import BaseSurrealModel, QueryCache


class Product(BaseSurrealModel):
    id: str | None = None
    name: str
    category: str
    price: int


for key, name, category, price in [("p1", "Dune", "book", 12), ("p2", "Lamp", "home", 30), ("p3", "Emma", "book", 9)]:
    await Product(id=key, name=name, category=category, price=price).save()

books = await Product.objects().filter(category="book").order_by("name").cache(ttl=30).exec()  # Dune, Emma
again = await Product.objects().filter(category="book").order_by("name").cache(ttl=30).exec()  # Dune, Emma
hits = QueryCache.stats()["hits"]  # 1 (the second read was answered from memory, without a request)

await Product(id="p4", name="Atlas", category="book", price=40).save()  # an ORM write to Product
fresh = await Product.objects().filter(category="book").order_by("name").cache(ttl=30).exec()  # Atlas, Dune, Emma
```

### What is cached

Every read of a queryset with `cache()`: `exec()`, `first()`, `get()` by filters or by id
(`get(<id>)`), `all()`, `count()`, `sum()`, `avg()`, `min()`, `max()`, `exists()`,
`values(…).annotate(…)` and `annotate(GeoDistance)`. A read is answered from the cache when an
earlier read sent **the same statement with the same variables**, on the same connection and with
the same identity, within the TTL, and no ORM write that can change its result has completed
since.

- **Rows are cached, not instances**: each hit builds new instances from a copy of the cached
  rows, so changing a result never changes the cache.
- **Empty results are cached too**: `[]`, a count of `0`, a missing record (`get(<id>)` raises
  `SurrealDbNotFoundError` on the hit as on the first read). Errors never are.
- **Not cached**: reads inside a transaction (`objects(tx=)`, which on SurrealDB 3.x may see its
  uncommitted writes), `refresh()`, `get_related()`, `traverse()`, `raw_query()` and
  `call_function()`.
- **Refused with `cache()`**: `query()` runs your own SurrealQL and raises `SurrealDbError` (cache
  its result yourself, see [Configuration and statistics](#configuration-and-statistics));
  `live()`, `watch()` and `changes()` name `cache()` among the clauses their usual error refuses;
  `as_of()` raises `SurrealDbError` in either order (a time-travel read is never cached).
- **Writes on a cached queryset** (`bulk_update()`, `bulk_delete()`, `QuerySet.patch()`,
  `delete_table()`, `get_or_create()`…) run as without `cache()`: they never read the cache, and
  they invalidate it.
- **A `Subquery`** is read inside the outer statement: the outer queryset's `cache()` applies, the
  inner one's is ignored, as its `fetch()` is.
- **An internal failure of the cache** (a value it cannot copy, say) never fails a read: the read
  goes to the database.
- It works the same on the embedded engine (`mem://`, `surrealkv://`).

### Time to live

`cache(ttl)` takes the TTL in seconds (an `int` or a `float`) or a `timedelta`. It is the **oldest
result this read accepts**: an entry stored by a read with a longer TTL is not served to it once
older than its own, and a result it stores lives for its TTL. `cache()` without a TTL uses
`QueryCache`'s default when the read runs: 60 seconds unless configured. A TTL of `0` means
**never cached**: every read goes to the database. Negative or non-finite values raise
`ValueError`, anything but a number or a `timedelta` (`True` included) `TypeError`.

```python
from datetime import timedelta

n = await Product.objects().filter(category="book").cache(timedelta(minutes=5)).count()  # 3
```

### Invalidation

Every ORM write removes the cached reads it can change, **before** its `post_*` signals fire:
`save()`, `update()`, `merge()`, `upsert()`, `patch()`, `delete()`, the atomic operations,
`relate()`, `remove_relation()`, `remove_all_relations()`, a typed edge's `save()`,
`bulk_create()`, `bulk_update()`, `bulk_delete()`, `QuerySet.patch()`, `delete_table()`,
`get_or_create()` and `update_or_create()` when they write, `signup()`, and the DDL helpers
`define_computed_fields()` and `define_access()`.

- A write removes the reads of the tables it writes, the reads whose **subqueries** read them
  included.
- A read that **follows links** is removed by **any** ORM write: `fetch()`, a dotted path such as
  `filter(**{"author.name": …})` or `order_by("author.name")` (in a subquery too), and a
  `select()` item that is not a plain field, such as `"author.*"` (the projection reads the linked
  record).
- A read of an **edge model** (`BaseSurrealEdge`) is removed by any delete: deleting a record
  deletes the edges that point at it or from it.
- Inside `transaction()` the tables written are invalidated **at commit** (a failed commit too,
  nothing on rollback); until then, reads outside the transaction see the committed state.
  `delete_table()` runs at once, outside `objects(tx=)`, and invalidates at once.
- `SurrealDBConnectionManager.signup()` writes into a table the ORM does not map: it empties the
  cache.
- While a write runs, cached reads of its tables go to the database and store nothing: a read
  answered before the write completed is returned to its caller, never stored.
- A write the ORM refuses before it reaches the database — for its arguments, while it is built
  (an `upsert()` without an id, a field name the statement cannot take…), or for a value it cannot
  send — removes nothing, and in a transaction notes nothing for the commit to invalidate. Once its
  statement is queued or sent, or it asked the connection for a client, it counts as a write,
  whatever it raises.

What the cache **cannot see** — the TTL bounds it, and `QueryCache.invalidate(Model)` removes a
table's entries after such a change:

- writes by another process or client;
- raw SurrealQL: `raw_query()`, `QuerySet.query()`, `call_function()`, `Transaction.add()`;
- a write whose task is cancelled, or that fails client-side, after its request was sent: the
  server may apply it after the invalidation;
- writes made by an access method's own `SIGNIN`/`SIGNUP` clauses beyond the record they create;
- what the server derives on its own: `DEFINE EVENT`, `REFERENCE … ON DELETE` (3.x), views
  (`DEFINE TABLE … AS SELECT`), `PERMISSIONS` that read another table;
- values computed when the read runs: a `DEFINE PARAM` read with `Var()`, `<future>`/`COMPUTED`
  fields (over another table, the graph such as `count(<-likes)`, or the time);
- a table of relations read through a model that is not a `BaseSurrealEdge`.

```python
lamp = await Product.objects().cache().get("p2")  # Lamp (price 30, now cached)
await Product.raw_query("UPDATE Product:p2 SET price = 35")  # raw SurrealQL: the cache is not told
stale = (await Product.objects().cache().get("p2")).price  # 30
QueryCache.invalidate(Product)  # removes Product's entries (and every read that follows links)
price = (await Product.objects().cache().get("p2")).price  # 35
```

### One cache per connection and identity

Cached results never cross identities. Each connection the manager opens has its own cache scope,
and every identity change on it starts a new one: after a sign-in, a sign-up, `authenticate()` or
`invalidate()`, the previous identity's results leave memory. The connection of another event
loop, a reconnect and an `embedded_database()` block start empty for the same reason, and closing
a connection (`close_connection()`, `close_all_connections()`, or the manager dropping a dead one)
drops its results.

- A session changed **behind the ORM's back** — `raw_query("USE …")`, or `signin()`/`use()`
  called on the SDK client itself — is not seen: call `QueryCache.clear()` after it.
- Cached rows stay in this process's memory until they expire, are evicted, invalidated or
  cleared, or their connection closes. `configure(enabled=False)` only hides them;
  `QueryCache.clear()` drops them.
- Entries you store with `QueryCache.set()` are not scoped to a connection or an identity: put
  whatever tells your callers apart in the key.

### Configuration and statistics

`QueryCache` is one store for the whole process, shared by its event loops and threads, with the
full SurrealDB-ORM's three settings: `QueryCache.configure(default_ttl=60, max_size=1000,
enabled=True)`.

- **Every call sets all three settings**: an argument you leave out takes its default, as in the
  full ORM. `configure()` never clears the cache.
- `default_ttl` is the TTL of a `cache()` read without one (seconds or a `timedelta`; `0`: never
  cached). `max_size` is the most entries kept: past it, the least recently used entry is evicted,
  and lowering it evicts at once.
- `enabled=False` **hides** the entries without removing them: `cache()` reads go to the database
  (calling `cache()` logs a warning), `QueryCache.get()` returns `None`, `QueryCache.set()` stores
  nothing and nothing is counted, while ORM writes, expiry and closed connections keep removing
  entries. `enabled=True` serves again the entries still valid. `clear()` is the way to drop the
  rows from memory; it also resets `hits` and `misses`.
- `stats()` returns `entries` (unexpired entries, hidden ones included), `tables` (the distinct
  tables they are tagged with), `max_size`, `default_ttl` (seconds), `enabled`, and `hits` and
  `misses` (the `cache()` reads and `get()` calls answered from the cache, and those that were not,
  since start-up or `clear()`).
- `invalidate()` takes a model class or a table name and returns how many entries it removed.

```python
QueryCache.configure(default_ttl=timedelta(seconds=30), max_size=500)  # enabled=True: its default
ttl = QueryCache.stats()["default_ttl"]  # 30.0

QueryCache.configure(enabled=False)  # hides the entries; default_ttl and max_size take their defaults
QueryCache.configure()  # enabled again: the entries still valid are served
QueryCache.clear()  # drops every entry from memory and resets hits and misses
```

To cache your own results, of `raw_query()` for instance, build a key with `make_key()` and use
`get()` and `set()`. The key comes from the statement, its variables as the SDK sends them and the
table; the entry is tagged with the table, so an ORM write to it removes the entry. From the
moment the write starts, its `pre_*`, `around_*` and `post_*` handlers included, `get()` returns
`None` for the entry (a miss), and what `set()` stores for that table meanwhile leaves with the
write:

```python
async def total_price() -> int:
    statement = "SELECT math::sum(price) AS total FROM Product GROUP ALL"
    key = QueryCache.make_key(statement, {}, "Product")
    rows = QueryCache.get(key)  # a copy, or None: missing, expired, or the cache is disabled
    if rows is None:
        rows = await Product.raw_query(statement)
        QueryCache.set(key, rows, "Product", ttl=60)
    return rows[0]["total"]


total = await total_price()  # 96 (the database answers)
cached_total = await total_price()  # 96 (the cache answers)
```

### Coming from the full SurrealDB-ORM

- **The same names**: `cache(ttl=…)` on a QuerySet, and `QueryCache` with `configure()`,
  `make_key()`, `get()`, `set()`, `invalidate()`, `clear()` and `stats()`, imported from
  `surreal_orm_lite`.
- **Every read is cached**, not only `exec()` (with `first()` and `get()` by filters):
  `get(<id>)`, `all()`, `count()`, the aggregations, `exists()` and `values()`+`annotate()` too.
- **Invalidation is built in** and needs no `configure()` call. It covers `bulk_*()`,
  `QuerySet.patch()`, the atomic operations, relations and transactions (at commit), where the
  full ORM listens to `post_save`, `post_update` and `post_delete`. Reads through links
  (`fetch()`, dotted paths) and edge reads after a delete are invalidated too.
- **Rows are cached**, and each hit builds new instances: the full ORM caches the instances.
- **Keys come from the CBOR bytes the SDK sends**, with the keys of mappings sorted as the full
  ORM's `sort_keys=True` does: `RecordID("user", 1)` and `"user:1"` never share an entry, where the
  full ORM's JSON with `default=str` writes them alike.
- **One scope per connection and identity**: a sign-in never reads another identity's results.
- **`live()`, `watch()`, `changes()` and `query()` refuse `cache()`**, which the full ORM ignores
  there; `as_of()` reads are not cached (the full ORM has no time travel).
- **`ttl`** may be a `float` or a `timedelta`; `0` is never cached, as in the full ORM; negative or
  non-finite values are refused; it is the oldest result a read accepts.
- **LRU eviction** instead of FIFO.
- **`invalidate()` takes a table name too**, and **`stats()` adds `hits` and `misses`**.
