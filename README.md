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

#### What `select()` accepts

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

#### `limit()` and `offset()`

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
| Time travel (`as_of()`, versioned storage)                                    | ✅     |
| Subqueries (`Subquery`, a QuerySet as a filter value)                         | ✅     |
| Query cache (`cache()`, TTL, invalidated by ORM writes)                       | ✅     |
| Multiple databases (named connections, `using()`)                             | ✅     |
| Schema introspection (`DatabaseIntrospector`, `generate_models_from_db()`)    | ✅     |
| Server-side events & function DDL (`Event`, `define_function()`)              | ✅     |
| Materialized views (`view_query`, `define_view()`, read-only)                 | ✅     |
| Relation constraints (`relation_in`, `define_relation()`, `TYPE RELATION`)    | ✅     |
| Model table DDL (`define_table()`, `schema_mode`, `permissions`)              | ✅     |
| Full-text search (`search()`, BM25 score and highlights)                      | ✅     |
| Vector search (`similar_to()`, KNN, HNSW indexes)                             | ✅     |
| Hybrid search (`hybrid_search()`, Reciprocal Rank Fusion)                     | ✅     |

### Supported Filter Lookups

- `exact` (default)
- `gt`, `gte`, `lt`, `lte`
- `in`, `not_in` (a list, or a `Range` bound as one value: see [section 24](#24-rich-field-types))
- `contains`, `not_contains` (`contains` on a range column tests membership)
- `containsall`, `containsany`
- `startswith`, `endswith`, `like`, `ilike`, `match`, `regex` (text: see [Text lookups](#text-lookups))
- `isnull`
- `inside`, `outside`, `intersects` (geometry values: see [section 25](#25-geospatial-fields))
- a subquery — `Subquery(qs)` or the queryset itself — as the value of `in`, `not_in`, `containsall`, `containsany` (all its values) or `exact`, `gt`, `gte`, `lt`, `lte`, `contains`, `not_contains` (its first value): see [section 28](#28-subqueries-subquery)

Filter values on rich fields must be typed: `filter(trial__gte=timedelta(days=7))`, not
`"7d"`. Strings are not converted (see [section 24](#24-rich-field-types)).

#### Text lookups

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
  ([section 36](#36-full-text-search-search-searchscore-searchhighlight-analyzer-index)).
- **Values.** The value must be a `str`, otherwise `ValueError`. A string starting with `$`
  is data, as for every lookup ([String filter values and `Var()`](#string-filter-values-and-var)).
  `startswith`, `endswith`, `match` and `regex` also take a `Var`. `like` and `ilike` do not:
  their pattern is translated before it is sent, so `Var` raises `ValueError`.

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

#### String filter values and `Var()`

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

Since v0.27.0 a lookup inside a `Q` takes a subquery as `filter()` does (see
[section 28](#28-subqueries-subquery)), here with `admins = User.objects().filter(role="admin")`:

```python
posts = await Post.objects().filter(Q(author__in=Subquery(admins)) | Q(score__gte=9)).exec()
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
but are not written to the record. The lookup binds the queryset's `variables()`, as `filter()`
does: `User.objects().variables(e="bob@example.com").get_or_create(email=Var("e"))` finds the user
whose email is the variable's value, and writes that value on create (see
[String filter values and `Var()`](#string-filter-values-and-var)). Without a transaction the behaviour is identical on
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
  a non-retryable failure (e.g. a duplicate-key error) is **not** retried. `is_conflict_error()`
  reads the exception and its chain as a traceback prints it: a context suppressed with
  `raise … from None` is not read (v0.31.2), so a conflict you re-raise as another exception with
  `raise … from None` is no longer recognised: chain it (`raise … from exc`, or `raise …` without
  `from` inside the `except` block), or raise a `SurrealDbConflictError`.
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

Since v0.34.0, `define_table()` ([section 35](#35-model-table-ddl-define_table-schema_mode-permissions)) writes a computed field's `TYPE` too
(`TYPE option<T> VALUE …`), checks its expression, and keeps a field the database defines `COMPUTED` (SurrealDB 3.x)
`COMPUTED`; `define_computed_fields()` turns such a field into `VALUE`.

---

### 17. Stored functions: `call_function()`

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
_is_ a column, and the SIGNUP clause sets the record id from it to match
(`CREATE User SET id = $email, email = $email, …`): the same statement on SurrealDB 2.x, 3.x and the embedded engine,
and the record id is the key on all of them.

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
Since v0.34.0, a user model whose schema mode is `SCHEMAFULL` — `table_type=TableType.USER` (the full ORM's key for an
auth table) or `schema_mode=SchemaMode.SCHEMAFULL` — gets from `define_access()` a `SCHEMAFULL` table with one
`DEFINE FIELD` per field (section 35); every other model keeps the `SCHEMALESS` table printed above. `define_table()`
gives such a model the same table, so the two can be called in any order.

> **The password field holds the hash.** SurrealDB returns the stored record, so
> `result.user.password` is `$argon2id$v=19$…`, never the plaintext you submitted.

**Keep the password field in `model_dump()`.** SIGNUP writes every field, while `update()` and `upsert()` replace the
record with what `model_dump()` writes: a field declared `Field(exclude=True)` (`password: str = Field(exclude=True)`)
would lose its stored value, the password hash, and every later signin would fail. Such a model raises `TypeError`,
naming the fields, before any request and whatever its schema mode: `access_ddl()`, `define_access()` (with or without
the table), `table_ddl()`, `define_table()`, `signup()`, `signin()`, `authenticate()` and `refresh()` refuse it, and so
do `update()` and `upsert()` (with or without `tx=`, before any signal), whoever wrote the record — another client, an
access defined by hand, a session opened before the upgrade. Remove `exclude=True` and leave the field out where you
serialise the model instead (`model_dump(exclude={"password"})`). **Changed in v0.35.4**: `authenticate()`, `refresh()`,
`update()` and `upsert()` used to accept such a model, so a record loaded with `authenticate()` or `get()` lost its
hash at its first `update()` or `upsert()`.

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
`SurrealDbError` instead. SurrealDB's own parameters need no `variables()`: `Var("auth")` (and
`$access`, `$session`, `$token`) stays a reference the server reads on every notification, so
`filter(owner=Var("auth"))` streams the signed-in record user's own records, on both lines; the
string `"$auth"` is data, written as `'$auth'` (see
[String filter values and `Var()`](#string-filter-values-and-var)). Every other query keeps binding
parameters.

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
because retrying arbitrary queries is planned for v0.41.0.

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
  A kill caught by a dropped connection is not a refusal: the subscription died with the socket,
  so `kill()` records it as ended and returns normally.
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
for the same pair fails as well, with `SurrealDbError` (the string form translates server errors
since v0.33.0).

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
`TYPE NORMAL` table raises `SurrealDbError`. Since v0.33.0 the model declares it (`relation_in`,
`relation_out`, `enforced`) and `define_relation()` applies it: see
[section 34](#34-relation-constraints-relation_in-define_relation).

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
   `BaseSurrealEdge` (section 34).
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
change it in place. A naive `datetime` is written as UTC and read back as an aware UTC `datetime`.
The instance keeps it naive until a call applies the row the server returns (see
[After a write that returns the row](#after-a-write-that-returns-the-row)): after `save()` (since
#261 with an explicit id too), `refresh()`, `merge()` (default `refresh=True`), `upsert()`,
`patch()` or an `atomic_*` helper, the field holds that aware UTC `datetime`, so compare it with
aware values. `update()`, and `save(tx=)` in a buffered transaction, leave it naive.

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
  `d'2026-01-02T03:04:05.123456789Z'` reads `…05.123456`. `update()` and `upsert()` rewrite the
  whole record, but a `datetime` you did not change (it still floors to the stored value) keeps
  the stored nanoseconds, on both lines and the embedded engine: a record whose `READONLY`
  datetimes are top-level fields can be updated after `get()` or `save()`, even when they hold
  nanoseconds ([#261](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/261)). A
  datetime you changed is written as you set it, and a `READONLY` field still refuses it. So that
  the statement stays small whatever the data, only the top-level datetimes are sure to be kept,
  up to 32 per record. Below the top level (in a sub-model, a `list` or a `tuple`, down to three
  levels) a datetime is kept only while the values rebuilt to reach it fit in what the fields
  before it left of those 32, so it depends on the data: an `update()` that passes in a test can
  fail once a list of timestamped items before the sub-model grows. A datetime not kept, and one
  in a set (its items have no position to match a stored value by), is written as sent: a
  `READONLY` field holding one may refuse `update()`, so change such a record with `merge()`.
  SurrealDB 2.x also refuses, for now, `update()` and `upsert()` of an existing record that has
  a field with a non-optional `TYPE` and a `VALUE` clause the model does not write (a `Computed`
  field, such as a generated `updated: Computed[datetime]`): change it with `merge()` there
  ([#267](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/267)).
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

`save()` (with or without an id, an edge's too), `refresh()`, `merge()`, `upsert()`, `patch()` and
the `atomic_*` helpers apply the row the server returns to the instance. After `save()` the
instance therefore holds what the record holds: the `server_fields` and computed fields, the
sub-fields a `DEFAULT` filled (`prefs.at DEFAULT time::now()`), and the values the server coerced
or rewrote (`VALUE string::lowercase($value)`), so a following `update()` writes them back
unchanged ([#261](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/261)). An id you
gave stays as you gave it; an auto id is the server's `RecordID`. A buffered transaction (HTTP,
SurrealDB 2.x, the embedded engine) returns no row before commit: there `save(tx=)` leaves the
instance as it was, and `refresh()` after the commit reads the row.

The top-level `timedelta`, `Duration` and geometry fields (since v0.24.0, see
[section 25](#25-geospatial-fields)) are converted on the way, and every other field gets the
value a read gives (#261; before, only a field holding a sub-model, a geometry or a nested
`timedelta` did): the row goes through the model's validation, the model's own validators
included. A sub-model field holds its model instance, not the row's `dict`, with the ORM's
durations, `timedelta`s, ranges and geometries inside; an enum field holds the enum, not its `str`
or `int`; a `tuple`, set, `date` or `Range` field holds its declared type, not the SDK's `list`,
midnight `datetime` or `Range`; a `float` field stored in a `decimal` column holds a `float`, not a
`Decimal`; a `list[Duration]` holds the ORM's durations. When the row as a whole does not validate,
each field is validated on its own, except one that the model's own validators could reshape (a
`field_validator` on that field, a `model_validator`, a `model_post_init`): that one, like a value
that does not validate, is applied as received, so these calls never fail on it. The `id` keeps
the server's `RecordID`. A column the model does not declare goes where a read puts it: as the SDK
reads it into `model_extra` under `extra="allow"`, nowhere otherwise. So a column holding a Pydantic
`@computed_field` (`model_dump()` writes it) or named like a method (`json`) never replaces that
attribute.

Writing such an instance back is safe, sub-models included: a following `update()` replaces the
record with these values, as after `get()`. So a `float` field stored in a `decimal` column writes
the float's rounding (`0.12345678901234567890` becomes `0.1234567890123457`: declare `Decimal` to
keep every digit), and under `extra="allow"` the columns in `model_extra` are written back, not
removed. An enum declared `(str, Enum)` or `(int, Enum)` holds the member, as after `get()`: it
compares equal to its value, but `str()` and f-strings give `Color.RED`, not `red` (a `StrEnum` or
an `IntEnum` gives the value), so use `.value` to build a key, a URL or a log line from it.

#### Values the SDK cannot read

The surrealdb SDK 2.0.0 cannot decode a zero duration, a SurrealDB 3.x native set (a
`TYPE set<…>` column on SurrealDB 3.3, see [Sets](#sets)), a degenerate geometry (see
[Geometries the SDK cannot read](#geometries-the-sdk-cannot-read)) or a datetime outside years
1-9999, the years a Python `datetime` holds (SurrealDB stores years -262143 to +262142, on both
lines): one such value fails the whole response it is in. The clients the ORM opens read them
anyway, through a hook around the SDK's own CBOR decoder that handles only these values; everything
else reads exactly as the SDK reads it:

- a zero duration reads as zero: `timedelta(0)` or `Duration(0)` in a typed field, the SDK's
  `Duration(0)` in an untyped one;
- a native set reads as a Python `set`, which `set[X]` and `frozenset[X]` fields validate, or as a
  `list` when its items are unhashable (record ids, objects, nested sets) or two of them are equal
  in Python, like `1` and `true`: every item the server holds comes back;
- a datetime before `0001-01-01T00:00:00Z` or after `9999-12-31T23:59:59.999999999Z` reads as its
  RFC 3339 text, the one SurrealDB gives it (`<string>`): `"0000-06-01T00:00:00Z"`,
  `"-0001-01-01T00:00:00Z"`, `"+10000-01-01T00:00:00.500Z"` (a sign and at least four digits outside
  years 0000-9999, every nanosecond kept). An untyped field, `raw_query()` and live notifications
  give that string; a `datetime` field refuses it with a `ValidationError` naming the field: `get()`
  raises it, and `filter().exec()` and `raw_query()` return such rows as dicts, as for any row that
  does not validate. A datetime within years 1-9999, before 1970 included, reads as the SDK's
  `datetime`, as before.

Written back through the ORM (`merge(v=row.v)`, or `update()`, which rewrites the whole row, the
untouched fields included), a zero duration stays a duration, but a native set is stored as an
array, like every set the ORM writes, unless its column is `TYPE set<…>` (see [Sets](#sets)), a
degenerate geometry as a plain object (see
[Geometries the SDK cannot read](#geometries-the-sdk-cannot-read)), and a datetime outside years
1-9999 as its text, a string. In an untyped field, `update()` flattens the SDK's `Duration(0)` into
an object, like any SDK value there
([#222](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/222)).

The hook is installed only while the installed SDK cannot decode these values, and only on the
ORM's own clients (`SurrealDBConnectionManager.get_client()`, `ephemeral_client()`), the embedded
engine's included (see [Embedded engine and test fixtures](#26-embedded-engine-and-test-fixtures)):
there a zero duration, a degenerate geometry and a datetime outside years 1-9999 read as on a
server, and `<set>[…]` is stored as an array, as on 2.x. An `AsyncSurreal` client the application
builds itself keeps the SDK's behaviour and fails on them.
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
`Range[T]` fields are lite additions. The full ORM also validates the rows its writes return; so
does lite, every field but the `id` since #261, with top-level durations converted (see
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
| `surrealkv+versioned://<path>` | ✅ on disk, keeps history: time travel with `as_of()` (section 27)                       | ❌                       |
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
  its live queries and its identity, even one signed in with `store=False`, and
  `is_connected()` stays `True` on an event loop that keeps one. An event loop whose server
  connection dropped while signed in with `store=False`
  ([section 21](#21-live-queries-real-time-notifications)) still raises once the server URL is
  set back with `set_url()`, or after an `embedded_database()` block: closing the engine, or
  setting an identity on it (`signin()`, `signup()`, `authenticate()`, `invalidate()`, model auth
  with `bind=True`), does not end that state. `set_connection()` does: a new configuration is a
  choice of identity. `close_all_connections()` and `unset_connection()` close the server
  clients too.

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
  own token) and the last one wins. A failed `bind=True` puts the previous session back, with
  one exception, as on a server: when `signup()` or `signin()` succeeded but the record does not
  fit the model, the session stays signed in as that record (the error says so). `refresh()`
  cannot succeed on the engine: its SurrealQL does not parse `WITH REFRESH`.

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
  ORM accepts any class), and `load()` raises `ValueError` on an undecorated subclass of a
  fixture that declares instances of its own (the full ORM loads only the parent's).
- **`load()` is typed** to yield the fixture class, so `fixtures.alice` has its model's type
  (the full ORM yields `Any`).
- **Lite additions**: `SurrealDBConnectionManager.is_embedded()` and `embedded_database()`.
- **`ModelFactory` and `Faker`** come in v0.40.0.

### 27. Time travel (`as_of()`)

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

#### Stores that keep history

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

#### The moment

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

#### Reads

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

#### Snapshots

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

#### What a time-travel queryset refuses

A time-travel read is read-only and cannot be streamed: `bulk_create()`, `bulk_update()`,
`bulk_delete()`, `patch()`, `update_or_create()`, `get_or_create()`, `delete_table()`, `query()`,
`live()`, `watch()` and `changes()` raise `SurrealDbError` on a queryset with `as_of()`, before
any request and without dropping any cached read. History cannot join a transaction:
`Model.objects(tx=...).as_of(...)` raises at the call. On the embedded engine, a URL that keeps
no history raises before any request, naming `surrealkv+versioned://`.

#### SurrealDB 2.x without versioned storage

A SurrealDB 2.x server in memory or on plain SurrealKV answers a time-travel read **without an
error**: it returns only the records last written at or before the moment, in their **current**
state, and nothing tells the ORM which storage the server runs, so it cannot tell. Start 2.x with
`surrealkv+versioned://` for time travel. A 3.x server without versioned storage refuses the read
(`SurrealDbError`), and so does 2.x on RocksDB.

#### Record links

`fetch()` resolves links as they were on SurrealDB 3.x. SurrealDB 2.x (servers, and the embedded
engine of the surrealdb SDK 2.x) resolves them to their current state, so on 2.x the reads that
return the fetched records (`exec()`, `first()`, `get()` without an id) raise `SurrealDbError`;
the other reads ignore `fetch()`, as without `as_of()`. Read the linked records with their own
`as_of()` query. On 3.x, a link fetched into a model-typed field is a snapshot too, read-only. A
dotted path through a link (`author.name`) in `filter()`, `select()`, `order_by()` or `values()`
reads the linked record as it was on 3.x and as it is now on 2.x.

#### Writing history at a past moment

The ORM writes now. A raw query can write a record at a past moment, one statement per
`client.query()` call:

```python
await client.query("CREATE Product:stool SET name = 'Stool', price = 15.0 VERSION d'2020-01-01T00:00:00Z';")
# SurrealDB 2.x and the embedded engine: stored at that moment, readable with as_of()
assert await Product.objects().as_of(datetime(2021, 1, 1, tzinfo=UTC)).count() == 1
```

SurrealDB 3.x accepts the statement, but the record is not readable at that moment (the table did
not exist then), and a `CREATE` of an id that exists raises.

#### Coming from the full SurrealDB-ORM

- **The full ORM has no time travel** (no `VERSION` support): `as_of()`, snapshots and their
  errors are lite additions.
- **`as_of()`, not `version()`**: the name is temporal SQL's `AS OF`. SurrealQL's keyword is
  `VERSION` and the JavaScript SDK has `select().version()`, but on the Python SDK's connection
  `version()` returns the server's version.
- **A snapshot is never `==` the current instance**, even with identical fields: compare
  `model_dump()`s.

### 28. Subqueries (`Subquery`)

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

#### What a subquery selects

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

#### Lookups

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

#### Ordered and limited subqueries

`order_by()`, `limit()` and `offset()` choose the values a subquery gives:

```python
oldest = Person.objects().filter(active=True).order_by("-age").limit(1)
theirs = await Post.objects().filter(author__in=oldest).exec()  # p2 (Bob is the oldest active person)
```

A statement with subqueries is one block, whose `LET`s bind each subquery once, innermost first:

```sql
{ LET $_sq0 = (SELECT id AS _sqv, age FROM Person WHERE active = $_f0 ORDER BY age DESC LIMIT 1)._sqv; (SELECT * FROM Post WHERE author IN $_sq0) };
```

- **Why a `LET`**: SurrealDB 3.2 and later can return wrong rows for an inline sub-SELECT with
  `ORDER BY` and `LIMIT`, which gives its value to the first outer record only
  ([surrealdb/surrealdb#7470](https://github.com/surrealdb/surrealdb/issues/7470)). Bound once,
  the subquery is right on every line, and the server runs it once instead of once per record.
- **Why `_sqv`**: an ordered subquery selects its column as `_sqv`, with its order fields, and
  reads `._sqv` back: SurrealDB 2.x refuses to order a sub-SELECT of one column by another field.
- **Reserved names**: `$_sq0`, `$_sq1`, … (`_sq<N>`) are the block's `LET`s and hide a
  `variables()` value of the same name; a subquery can order by `_sqv` only when it selects `_sqv`
  itself (`ValueError` otherwise).
- `limit()` and `offset()` of a subquery take a non-negative integer, as everywhere (`TypeError` or
  `ValueError` when they are called).

#### Where subqueries apply

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

#### What a subquery cannot do

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

#### Coming from the full SurrealDB-ORM

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

### 29. Query cache (`cache()`, `QueryCache`)

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

#### What is cached

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

#### Time to live

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

#### Invalidation

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

#### One cache per connection and identity

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

#### Configuration and statistics

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

#### Coming from the full SurrealDB-ORM

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

### 30. Multiple databases (`add_connection()`, `using()`)

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

#### Routing a model

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

#### Redirecting a block: `using()`

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

#### Transactions and other operations across connections

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

#### What each connection keeps for itself

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

#### Which connection a manager method uses

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

#### Coming from the full SurrealDB-ORM

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

### 31. Schema introspection (`DatabaseIntrospector`, `generate_models_from_db()`)

Since v0.30.0 the ORM reads the schema of a database — its tables with their fields, indexes and events, and its
accesses, analyzers, functions and params — as immutable typed objects, and generates `BaseSurrealModel` classes from
it, as the full SurrealDB-ORM does. It only reads: it sends `INFO FOR DB` and `INFO FOR TABLE`, never a statement that
reads a record or changes the schema, so it needs a session allowed to run `INFO`: a system user
([Permissions](#permissions)). Kinds, field paths and the generated types are the same on SurrealDB 2.x, 3.x and the
embedded engine; expressions (`DEFAULT`, `VALUE`, `ASSERT` …) are kept as each line renders them
([SurrealDB 2.x and 3.x](#surrealdb-2x-and-3x)). The examples of this section run on the embedded engine (`mem://`),
with nothing to start.

#### Reading the schema

```python
from surreal_orm_lite import DatabaseIntrospector, SurrealDBConnectionManager

SurrealDBConnectionManager.set_connection(url="mem://", namespace="shop", database="main")
client = await SurrealDBConnectionManager.get_client()
await client.query(
    "DEFINE TABLE person SCHEMAFULL;"
    "DEFINE FIELD name ON person TYPE string;"
    "DEFINE FIELD age ON person TYPE option<int> DEFAULT 18;"
    "DEFINE FIELD address ON person TYPE option<object>;"
    "DEFINE FIELD address.city ON person TYPE string;"
    "DEFINE INDEX person_name ON person FIELDS name UNIQUE;",
    {},
)

schema = await DatabaseIntrospector().introspect()
person = schema.tables["person"]
person.schema_mode                    # 'SCHEMAFULL'
person.fields["age"].field_type       # 'int'
person.fields["age"].nullable         # True: option<int> on SurrealDB 2.x, none | int on 3.x
person.fields["age"].default          # '18': SurrealQL text
person.fields["address.city"].path    # ('address', 'city')
person.indexes["person_name"].unique  # True
schema.issues                         # (): everything was read and parsed
```

- `introspect()` sends `INFO FOR DB;`, then one `INFO FOR TABLE` per table in codepoint order of the names (each
  escaped as an identifier), on the connection's client and outside any transaction. No record is read, nothing is
  defined or written.
- `schema.tables` holds one `TableState` per table — mode, type (`"normal"`, `"relation"`, `"any"`), relation ends,
  view query, change feed, permissions and comment — with its `fields`, `indexes` and `events`. The database's own
  definitions are in `schema.accesses`, `schema.analyzers`, `schema.functions` (keyed `"fn::name"`) and
  `schema.params` (keyed without `$`). The sections the ORM does not model (`apis`, `configs`, `models`, and on
  SurrealDB 3.x `buckets`, `modules` and `sequences`) are kept in `schema.other` as SurrealDB returns them. The `users`
  section is never read.
- A field is keyed by its path, with `*` for array items (`"address.city"`, `"tags.*"`), and `path` holds the
  segments, unescaped. `field_type` is its kind without the optional wrapper (`"any"` without `TYPE`), and `nullable`
  says whether it may be `NONE`.
- Expressions are SurrealQL text, as the server renders them: `default`, `value`, `computed`, `assertion`, an event's
  `when` and `then`, a view's query, permission rules (`'FULL'`, `'NONE'` or `'WHERE <expr>'`). Every definition also
  keeps the server's statement in `raw`.
- The state is immutable: frozen dataclasses, read-only maps that iterate in sorted key order, and tuples. Printing or
  logging a state shows names and structure only: definition texts stay in their attributes, out of `repr()` and
  `str()`.
- Nothing is guessed. A definition that cannot be parsed exactly, a section of an unexpected shape or an
  `INFO FOR TABLE` the server answers with an error becomes a `SchemaIssue` in `schema.issues` (its `kind`, `name`,
  `table`, `message` and `statement`) and one WARNING on the `surreal_orm_lite.introspection` logger; `introspect()`
  does not raise for them. An issue's message never quotes the definition, which may hold a secret.
- Errors: `SurrealDbAuthenticationError` when the session may not run `INFO FOR DB`, or is refused an
  `INFO FOR TABLE` after `INFO FOR DB` succeeded (it lost its rights meanwhile); `SurrealDbError` when `INFO FOR DB`
  fails otherwise; and `SurrealDbNotFoundError` for a `tables=` name the database does not define. A connection lost
  during the read is not an issue: the transport's error (or the ORM's) propagates unchanged, so
  `generate_models_from_db()` never replaces a file with models of tables it could not read.

```python
SurrealDBConnectionManager.add_connection("archive", url="mem://", namespace="shop", database="archive")

just_person = await DatabaseIntrospector(tables=["person"]).introspect()  # INFO FOR TABLE `person` only
no_table = await DatabaseIntrospector(tables=[]).introspect()             # INFO FOR DB only: no_table.tables is empty
archive = await DatabaseIntrospector("archive").introspect()              # a named connection
async with SurrealDBConnectionManager.using("archive"):
    archive = await DatabaseIntrospector().introspect()                   # the block's connection
archive.connection                                                        # 'archive'
```

- `connection=` is the **name** of a connection, the one given to `add_connection()`, or `None` for the current one:
  the innermost `using()` block's, else `"default"`. It is resolved when `introspect()` runs, and `schema.connection`
  records it. A client object, such as `await SurrealDBConnectionManager.get_client()`, raises `TypeError`: it does
  not carry the connection's name, which routing and the generated models' `connection=` key need.
- `tables=` reads those tables only: `None`, the default, reads every table, and an empty list reads none (the
  database-level sections are read all the same). A `str` raises `TypeError`: write `tables=["person"]`.

#### Generating models

```python
from surreal_orm_lite import ModelCodeGenerator, generate_models_from_db

source = ModelCodeGenerator().generate(schema)          # the same text as: await generate_models_from_db()
print(source)
await generate_models_from_db("models_generated.py")    # writes the file, replacing it if it exists (as the full ORM)
try:
    await generate_models_from_db("models_generated.py", overwrite=False)
except FileExistsError as exc:
    print(exc)  # 'models_generated.py' already exists: overwrite=False refuses to replace it.
```

It prints:

```python
"""Models generated by surreal_orm_lite.generate_models_from_db() from a SurrealDB schema.

Review them before use. They describe tables that already exist: define_table()
keeps the clauses of the database they do not declare (a COMPUTED field stays
COMPUTED, a server DEFAULT stays), so it can be called on them; do not call
define_computed_fields() on them, it would replace the database's field definitions.
"""

from pydantic import BaseModel
from surreal_orm_lite import BaseSurrealModel, SchemaMode, SurrealConfigDict


class PersonAddress(BaseModel):
    city: str


class Person(BaseSurrealModel):
    # Indexes: person_name (UNIQUE)
    model_config = SurrealConfigDict(table_name="person", schema_mode=SchemaMode.SCHEMAFULL)

    id: str | None = None
    address: PersonAddress | None = None
    age: int | None = 18
    name: str
```

- `ModelCodeGenerator().generate(schema)` is pure: the same schema always gives the same source.
  `generate_models_from_db(output_path=None, connection=None, *, tables=None, overwrite=True)` reads the schema
  (`connection` and `tables` as for `DatabaseIntrospector`), generates the models and returns their source; with an
  `output_path` it also writes the file, UTF-8 with `\n` newlines, creating its parent directories.
- An existing file is replaced, as the full ORM replaces it, and atomically: the source goes to a temporary file in
  the same directory, which `os.replace()` then moves over the file. A failure leaves the previous file and no
  temporary file, and a symbolic link is replaced by the file, its target never written. `overwrite=False` refuses an
  existing path with `FileExistsError` before reading anything, and creates the file exclusively.
- One class per table: a `BaseSurrealModel`, or a `BaseSurrealEdge` for a `TYPE RELATION` table (which declares no
  `id`, `in_` or `out`: the base class provides them). Its `model_config` holds the `table_name`, `view_query` for a
  view table (since v0.32.0: a materialized view model, [section 33](#33-materialized-views-view_query-define_view)),
  `relation_in`/`relation_out`/`enforced` for a relation table (since v0.33.0,
  [section 34](#34-relation-constraints-relation_in-define_relation)), the table's clauses —
  `table_type=TableType.ANY` for a `TYPE ANY` node table, `schema_mode=SchemaMode.SCHEMAFULL`, `changefeed=`,
  `permissions={…}` and `comment=` (since v0.34.0, [section 35](#35-model-table-ddl-define_table-schema_mode-permissions)) —, `connection=`
  when the schema was read from another connection than `"default"`, then `server_fields` and `extra="allow"`
  (below). A model declares
  `id: str | None = None` first (`id: RecordID | None = None` for a view: a grouped view's ids are arrays), then one
  line per field, in codepoint order of the columns. A `SCHEMAFULL` relation table with data fields gives an edge model
  with `schema_mode=SchemaMode.SCHEMAFULL` (since v0.34.0): apply it with `define_table()`, which defines those fields,
  not `define_relation()`, which raises `SurrealDbError` before any request
  ([section 34](#34-relation-constraints-relation_in-define_relation)).
- A class is named after its table in PascalCase, without singularisation (`game_tables` → `GameTables`); a name
  that is taken — a Python keyword, a name the module imports, another table's class — gets `Model` appended (`range`
  → `RangeModel`). The table's indexes and events are comments of its class, and so are its comment, change feed
  and permissions where the class statement would refuse the key (`# Permissions: …` for a rule it cannot embed, a
  comment holding a NUL character); the module's header lists every table or field left out, with the reason. A view model's class says `# Read-only materialized view`; a view the lite cannot
  declare — an edge, a computed field, a query `view_query` refuses — keeps its query as a `# View: AS …` comment.
  A relation table whose ends the lite cannot declare (a table name that is not an identifier) keeps them as an
  `# IN … OUT …` comment.
- The source is safe to import: names, comments and expressions read from the database reach it only as escaped
  string literals, single-line ASCII comments and identifiers the generator builds, so a schema cannot inject code
  into it. It holds no timestamp, server address, namespace or database name, and no
  `from __future__ import annotations`.

#### How SurrealDB kinds become fields

| SurrealDB kind                                                     | Generated annotation                                                                                                                                                     |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `any`, or no `TYPE`                                                | `Any = None`                                                                                                                                                             |
| `bool`, `int`, `float`, `string`, `bytes`                          | `bool`, `int`, `float`, `str`, `bytes`                                                                                                                                   |
| `number`                                                           | `int`, `float` or `Decimal`: their union, so each value keeps its type                                                                                                   |
| `decimal`, `datetime`, `uuid`                                      | `Decimal`, `datetime`, `UUID`                                                                                                                                            |
| `duration`                                                         | `Duration`, nanosecond-exact ([section 24](#24-rich-field-types))                                                                                                        |
| `range`                                                            | `Range`                                                                                                                                                                  |
| `record`, `record<…>`                                              | `RecordID`                                                                                                                                                               |
| `geometry<point>`, or another single geometry kind                 | `GeometryPoint` …; optional: `PointField = None` … ([section 25](#25-geospatial-fields))                                                                                 |
| `geometry`, `geometry<feature>`                                    | `Geometry`; optional: `GeometryField = None`                                                                                                                             |
| `geometry<…>` with several kinds                                   | the union of their classes                                                                                                                                               |
| `object` with declared fields (`address.city`)                     | a sub-model (below)                                                                                                                                                      |
| `object` without declared fields, an object literal (`{ a: int }`) | `dict[str, Any]`                                                                                                                                                         |
| `array`, `array<T>`, `array<T, N>`                                 | `list[Any]`, `list[T]`                                                                                                                                                   |
| `set`, `set<T>`, `set<T, N>`                                       | `set[T]` when `T` is hashable (`str`, `int`, `float`, `bool`, `bytes`, `Decimal`, `UUID`, `datetime`, `Duration`, a `Literal`), else `list[T]`; `set` alone: `list[Any]` |
| `[A, B]` (a tuple)                                                 | `tuple[A, B]`                                                                                                                                                            |
| string, integer and boolean literals (`'a'`, `1`, `true`)          | `Literal[…]`, values in the kind's order                                                                                                                                 |
| float and decimal literals (`1.5f`, `1.5dec`)                      | `float`, `Decimal`                                                                                                                                                       |
| `null`                                                             | `None`                                                                                                                                                                   |
| a union of kinds                                                   | the union of their annotations, `None` last; `Any` when one of them is `Any`                                                                                             |
| `regex`, `function`, `file`, any other kind                        | `Any = None`                                                                                                                                                             |

- An optional kind, `option<T>` (rendered `none | T` by SurrealDB 3.x), gives `T | None = None`; a single geometry
  kind gives its field alias instead (`option<geometry<point>>` → `PointField = None`).
- **Sub-models**: an `object` field with declared fields (`address` with `address.city`) becomes a `BaseModel` named
  after its class and column (`PersonAddress`), written before the class; an `array<object>` whose items have
  declared fields (`lines.*.sku`) becomes `list[<Class><Column>Item]`. The sub-model keeps undeclared keys
  (`extra="allow"`) when the object is `FLEXIBLE` or the table `SCHEMALESS`. A nested model writes its keys by
  attribute name, so when a nested column cannot be an attribute name as it is (`first-name`, `datetime`), its parent
  is a `dict[str, Any]` instead. A nested field whose parent is not defined makes that parent
  `dict[str, Any] | None = None`, noted in the header. Item definitions (`tags.*`) add no attribute: the array's kind
  carries the item type.
- **`DEFAULT`**: a constant the model can hold exactly becomes the Python default, so the database stores the same
  value whether you set the field or not, and stores yours when you do — `DEFAULT 18` → `= 18`, `'active'` →
  `= "active"`, `true` → `= True`, `0.5f` on a `float` → `= 0.5`, `1.5dec` → `= Decimal("1.5")`, `NONE` → `= None`,
  `[]` → `= Field(default_factory=list)` (`set` for a set), `[1, 2]` → `= Field(default_factory=lambda: [1, 2])`,
  `{}` → `= Field(default_factory=dict)` or the sub-model's class, `1h30m` →
  `= Field(default_factory=lambda: Duration.parse("1h30m"))`, `d'2030-01-01T00:00:00Z'` →
  `= datetime.fromisoformat("2030-01-01T00:00:00Z")`, `u'…'` → `= UUID("…")`. The value is copied when the models are
  generated: generate them again after changing a `DEFAULT`. A `{}` whose sub-model needs a value makes the field
  required, because SurrealDB refuses a create that leaves it to that default. Any other default — an expression such
  as `time::now()`, a record id, `math::INF`, a non-empty object, a datetime with nanoseconds, a literal of another
  type — makes the field `T | None = None` and lists its attribute in `server_fields`
  ([section 20](#20-field-aliases-server_fields-and-mergerefreshfalse)): omitted on create, so that the server's
  default applies, and read back after `save()`. A value you set on such a field before `save()` is not written: set
  it with `merge()` once the record exists.
- **`VALUE` and `COMPUTED`**: `COMPUTED e` (SurrealDB 3.x), and a `VALUE e` that reads neither `$value` nor
  `$input`, become `Computed[T] = computed("e")` ([section 16](#16-computed-fields-computed--define-field--value)):
  never written, read back after `save()`. When `computed()` refuses the expression (a `;`, even inside a string, or
  a block `{ … }`) or the column needs an alias, the field takes the server form instead: `T | None = None`, listed in
  `server_fields`. A `VALUE` that reads `$value` (`string::lowercase($value)`) is an ordinary field: you write the
  value and the server transforms it.
- **`SCHEMALESS` tables** get `extra="allow"`: the fields the table does not declare are read into `model_extra` and
  written back.
- **Aliases**: a column that cannot be an attribute name as it is gets one, with `Field(alias=<column>)`
  ([section 20](#20-field-aliases-server_fields-and-mergerefreshfalse)): `first-name` → `first_name`, `class` →
  `class_`, `_private` → `private`, `9lives` → `f_9lives`. So does a column named like a type or a name the module
  uses (`datetime`, `computed`, `list`, `str`, `Field`, `Any`), like a model attribute (`save`, `objects`, `json`), like
  a class of the module, or `l`, `O` or `I` (ambiguous names for linters): it gets a `_` suffix (`datetime_`, with
  `alias="datetime"`), since the attribute would otherwise shadow that name in the annotations after it.
- **Skipped tables**, listed in the module's header: a table whose name is not an identifier (`order-items`:
  `table_name` refuses it), and a table named like a SurrealQL keyword, in any case, which the ORM's statements cannot
  use as a table name (`select`, `Select`, `create`, `only`, `none` …). They are in `schema.tables` all the same.

#### What the generator does not do

- It reads no record: a `SCHEMALESS` table's model holds the fields the table declares (with `extra="allow"`), and an
  object-literal kind (`{ a: int }`) stays a `dict[str, Any]`.
- A field's `ASSERT`, `PERMISSIONS`, `REFERENCE` (the field is a `RecordID`) and comment are not generated: the
  database still applies them, and `define_table()` keeps them. `READONLY` is a trailing `# READONLY` comment: no
  field option exists for it.
- `update()` may fail on an instance holding a `READONLY` datetime below the top level, in a sub-model (`address.at`),
  a `tuple`, an array (`array<datetime>`, `items.*.at`) or a set, after `get()` as after `save()`: Python keeps
  microseconds, SurrealDB nanoseconds, and the server refuses the changed value. `update()` keeps the top-level
  `READONLY` datetimes to the nanosecond, up to 32 per record; below the top level it keeps one only while the fields
  before it leave room, which depends on the data (see [Precision and `Decimal`](#precision-and-decimal)). The
  generated class says so, as does a class with more than 32 top-level fields that may hold a datetime: change such a
  record with `merge()`.
- `array<T, N>` becomes `list[T]`: the length is left to the server, which checks it differently on each line
  (SurrealDB 2.x accepts at most N items, 3.x exactly N).
- `null | T` becomes a required `T | None`, but the ORM sends `None` as `NONE`, which `null | T` refuses: give such a
  field a value, or declare it `option<T>`, which accepts `NONE`.
- Never call `define_computed_fields()` on a generated model: it would replace the database's field definitions with
  `DEFINE FIELD OVERWRITE … VALUE …`, without their `TYPE`, `ASSERT` or `PERMISSIONS` (the module's docstring says so).
- Long lines: the `model_config` line is wrapped the way `ruff format` keeps it — one keyword per line once it exceeds
  128 columns, and the `server_fields` names one per line once their own line does. Other long lines — long names, a
  long `Literal` union, a long view query (`view_query="…"`) — are left on one line for your formatter. When every
  line fits in 128 columns, the module passes `ruff check` and `ruff format --check` at that line length.

#### SurrealDB 2.x and 3.x

- Each line renders the same definition its own way. The reader normalises what the ORM types — kinds,
  nullability, field paths, flags and names — to one form, the 3.x one where both lines accept it: `option<int>`
  (2.x) and `none | int` (3.x) both give `field_type == "int"` and `nullable is True`; array items, `tags[*]` (2.x)
  and `tags.*` (3.x), both give the field `"tags.*"`; `FLEXIBLE TYPE object` (2.x) and `TYPE object FLEXIBLE` (3.x)
  both give `flexible is True`. The same schema gives the same state and the same generated types on every line.
- Expressions are kept as each line renders them: `VALUE IF b THEN 1 ELSE 2 END` on 2.x is
  `VALUE (IF b THEN 1 ELSE 2 END)` on 3.x, a block `{ … }` is one line on 3.x and several on 2.x, and `math::INF`
  (2.x) is `math::INFINITY` (3.x). So `value`, `default`, `raw` … and a generated `computed("…")` may differ between
  the lines; the annotations do not.
- A definition only one line has is read and typed on that line: on 3.x `COMPUTED` (`FieldState.computed`),
  `REFERENCE` (`reference`, `on_delete`), `FULLTEXT` indexes, the `file` kind, `WITH REFRESH` and `BEARER` accesses,
  and `DEFINE SEQUENCE` (kept raw in `schema.other["sequences"]`); on 2.x and the embedded engine `MTREE` indexes
  (`IndexState.mtree`), `SEARCH ANALYZER` indexes and `geometry<feature>`.
- DDL that a `transaction()` block has not committed yet is invisible: introspection reads outside the transaction,
  so it sees the committed schema, inside the block too.
- A database removed (`REMOVE DATABASE`) while the connection uses it: on 3.x introspection raises `SurrealDbError`
  (the database does not exist) until the database is defined again; 2.x answers an empty schema.
- The embedded engine reads like a 2.x server
  ([ORM behaviour: the embedded engine](#orm-behaviour-the-embedded-engine)).

#### Permissions

Introspection needs a system user: root, a namespace user or a database user, any role. A record user or an
anonymous session gets `SurrealDbAuthenticationError` ("this session may not run INFO FOR DB"); on the embedded
engine the anonymous session owns the database and may read it.

The `users` section is never read. On SurrealDB before 3.2.2 (every 2.x release, 3.x before 3.2.2, and the embedded
engine of SDK 2.0.0), `INFO FOR DB` shows user password hashes to every role that may run `INFO`
([SurrealDB's `INFO` documentation](https://surrealdb.com/docs/surrealql/statements/info)); introspection never
reads, stores, logs or writes them.

#### Coming from the full SurrealDB-ORM

- **Same names and call shapes**: `DatabaseIntrospector(connection).introspect()`,
  `ModelCodeGenerator().generate(state)`, `generate_models_from_db(output_path, connection)`, and the state classes
  `SchemaState`, `TableState`, `FieldState`, `IndexState`, `EventState`, `AccessState` and `AnalyzerState`, built by
  position or by keyword with the full ORM's arguments. Lite attributes (`raw`, `path`, `table`, `comment`, …) come
  after them and are keyword-only. `FunctionState`, `ParamState`, `SchemaIssue` and `tables=` are lite additions.
- **State is immutable**: frozen dataclasses with read-only maps, and every definition keeps the server's statement
  in `raw` (full ORM: mutable dataclasses, no statement).
- **Sequences are tuples**: compare `index.fields`, `analyzer.tokenizers`, `analyzer.filters`, `table.relation_in`
  and `table.relation_out` with tuples (`index.fields == ("email",)`); lists are accepted when constructing, and so
  is the full ORM's pipe-joined `relation_in="person | company"`. Relation ends are `()` where the full ORM has
  `None` (not a relation table, or any table).
- **Permission values are `'FULL'`, `'NONE'` or `'WHERE <expr>'`** (full ORM: the bare condition).
- **`default` is SurrealQL text**: `'18'`, `"'active'"`, `'time::now()'`, with `default_always` for
  `DEFAULT ALWAYS` (full ORM: a Python value, where `'active'` and `time::now()` both become strings). A
  `FieldState` built with a `default` that is not text raises `TypeError`.
- **Typed defaults and `server_fields`**: a constant `DEFAULT` becomes a typed Python default
  (`Field(default_factory=list)`, `Duration.parse(…)`, `datetime.fromisoformat(…)`, `UUID(…)`), and any other
  `DEFAULT` becomes a `server_fields` entry (full ORM: `Field(default=…)` holding the SurrealQL text as a Python
  string, such as `'[]'` or `'time::now()'`, and no `server_fields`).
- **`nullable` means "may be NONE"**: it is true for an optional kind (`option<T>`, rendered `none | T` by
  SurrealDB 3.x); `null | T` is not nullable, since it refuses NONE (full ORM: nullable). `field_type` is `"any"`
  for a field without `TYPE`, as in the full ORM.
- **Accesses are database-level**: they are in `schema.accesses`, and `TableState` has no `access` attribute; find
  a table's with `[a for a in schema.accesses.values() if a.table == "user"]`. `table`, `signup_fields` and
  `signin_where` are read from `SIGNUP (CREATE <table> SET …)` (the form `define_access()` uses, whose
  `id = $<key>` for a `primary_key` is one of the `signup_fields`; also `CREATE type::thing('<table>', …) SET …` and
  `CREATE type::record('<table>', …) SET …`) and `SIGNIN (SELECT … FROM <table> WHERE …)`. The full ORM reads the
  first form only, and another shape leaves them empty, with field names unescaped; durations are kept as rendered,
  `'NONE'` included.
- **`encrypted` is not provided**: the lite has no `Encrypted` type, so a hashing `VALUE`
  (`crypto::argon2::generate($value)`) stays in `FieldState.value` and the field is generated as a writable field.
- **APIs and the GraphQL config are not typed**: their definitions are in `SchemaState.other["apis"]` and
  `SchemaState.other["configs"]`, as SurrealDB returns them (full ORM: `ApiState`, `GraphQLConfigState`).
- **`SchemaState.diff()` and `schema_diff()` come with v0.38.0** (migrations), as does `applied_migrations`; the
  `inspectdb` command comes with v0.39.0.
- **A refused or failed `INFO FOR DB` raises** `SurrealDbAuthenticationError` or `SurrealDbError`, and a
  definition that cannot be read or parsed becomes a `SchemaIssue` in `schema.issues` and a logged warning (full
  ORM: an empty state, and definitions dropped with a debug log).
- **`on_delete` keeps `'THEN <expr>'`** for `REFERENCE ON DELETE THEN …` (full ORM: `'THEN'` alone).
- **`connection=` is a name**: the name a connection was added with (`add_connection()`), else the current one
  (`using()`, then `"default"`). A client object is refused with a `TypeError`, because a client does not carry the
  ORM's connection name, which routing, reconnects and the generated models' `connection=` key need (full ORM: an
  `HTTPConnection`).
- **Generated models use the full ORM's keys** (since v0.34.0): `table_type=TableType.ANY` for a `TYPE ANY` table,
  `schema_mode=SchemaMode.SCHEMAFULL`, `changefeed=…`, `permissions={…}` and `comment=…`, as the full ORM writes them
  (it writes no `schema_mode` or `comment`); a view with `view_query=…` since v0.32.0 and a relation table's ends with
  `relation_in=…`, `relation_out=…` and `enforced=True` since v0.33.0. A comment stays where the class statement would
  refuse the key (`# View: AS …`, `# IN … OUT …`, `# Permissions: …` for a rule it cannot embed). The base class says
  `RELATION` (`BaseSurrealEdge`). `READONLY` is a comment (full ORM: `Field(readonly=True)`).
- **`RecordID` instead of `ForeignKey`/`ReferencesField`**; no `VectorField` or `Encrypted`; `computed()` instead
  of `Computed()`; `Duration`, `UUID`, `Decimal` and the geometry classes instead of `str`, `dict` and `float`;
  nested fields become sub-models (full ORM: dotted attribute lines).
- **No singularisation**: `game_tables` becomes `GameTables` (full ORM: a trailing `s` is stripped, so `status`
  becomes `Statu`).
- **`overwrite=False` added**: `generate_models_from_db(path)` replaces an existing file as the full ORM does, here
  atomically; `overwrite=False` refuses it with `FileExistsError`.

### 32. Server-side events and stored functions (`Event`, `define_events()`, `define_function()`)

Since v0.31.0 the ORM defines two kinds of objects that SurrealDB runs itself. An **event** runs SurrealQL inside
the database whenever a record of its table is created, updated or deleted, whoever writes it — the ORM, a raw query
or another client: an audit row, a guard that refuses a write, a message for an outbox. A **stored function**
(`fn::…`) is SurrealQL that `call_function()` calls ([section 17](#17-stored-functions-call_function)). Events are
declared next to the model, in `SurrealConfigDict(events=[…])`; the ORM writes their `DEFINE EVENT` statements with
quoted names and expressions checked for mistakes, applies them idempotently, and section 31's introspection reads
them back ([section 31](#31-schema-introspection-databaseintrospector-generate_models_from_db)). The same code runs on
SurrealDB 2.x, 3.x and the embedded engine; asynchronous events and `$input` are 3.x only. The examples assume a
configured connection: they run as written on the embedded engine (`mem://`), a 2.x core, which is why the
asynchronous example prints a refusal.

#### Declaring events on a model

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
print(await client.query("SELECT old, new FROM audit_log;", {}))
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
  until it is removed: `define_events()` does not compare the database with the model (that comes with migrations,
  v0.38.0).
- A subclass inherits `events` and defines them on its own table, unless it sets its own (`events=[]` for none).
  Typed edges (`BaseSurrealEdge`) take events too: they fire on the edge table. Defining an event on a table that
  does not exist creates the table, `SCHEMALESS`.
- A write that an event refuses (a `THROW`, or an error in `then`) raises the SDK's `InternalError`, and nothing is
  written. The rows an event writes are written by the server: they fire no ORM signal.

#### Events without a model

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

#### Asynchronous events (SurrealDB 3.x)

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

#### Stored functions: `define_function()`

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

#### Transactions, connections and the query cache

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
  ([section 29](#29-query-cache-cache-querycache)) does not: it forgets the tables the ORM itself writes, so a cached
  read of a table that events write stays as it was until its time to live runs out. Do not cache such reads, or call
  `QueryCache.invalidate(Model)` after the write.
- Applying DDL fires no signal, and neither do the rows events write.

#### SurrealDB 2.x and 3.x

- Asynchronous events (`ASYNC`) are 3.x only: on SurrealDB 2.x and the embedded engine the ORM raises
  `SurrealDbError` before any DDL.
- `$input` holds the fields the write sent on 3.x (`{ name: 'a' }` for `SET name = 'a'`); it is `NONE` on 2.x and
  the embedded engine.
- A write that changes nothing fires no event, except on 3.x an `UPSERT` — the ORM's `upsert()` of an unchanged
  record — which fires one `UPDATE` event.
- Read back (section 31), each line renders the definitions its own way: on 3.x a block is one line and a single
  statement loses its `;`, on 2.x a block of several statements spans several lines; an optional parameter reads
  `none | int` on 3.x and `option<int>` on 2.x. The [2.x/3.x table](#orm-behaviour-surrealdb-2x-vs-3x) lists the
  differences.

#### Security

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

#### Coming from the full SurrealDB-ORM

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
- **The migration operations** (`DefineEvent`, `RemoveEvent`, `RawSQL`) and the removal of the events a model no
  longer declares **come with migrations (v0.38.0)**.

### 33. Materialized views (`view_query`, `define_view()`)

Since v0.32.0 a model can read a **materialized view**: a table SurrealDB computes from a `SELECT` and keeps up to
date as its source table changes — counts, sums, filtered copies. The view is declared on the model with
`view_query`, applied idempotently with `define_view()` and read like any model, and every write of the model is
refused: SurrealDB computes the view's records itself. The same code runs on SurrealDB 2.x, 3.x and the embedded
engine, except where the [2.x/3.x table](#orm-behaviour-surrealdb-2x-vs-3x) says otherwise. The examples assume a
configured connection: they run as written on the embedded engine (`mem://`).

```python
from surrealdb import RecordID

from surreal_orm_lite import BaseSurrealModel, SurrealConfigDict


class Order(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="orders")

    id: str | None = None
    status: str
    amount: float
    customer: str | None = None


class OrderStats(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_name="order_stats",
        view_query="SELECT status, count() AS total, math::sum(amount) AS revenue FROM orders GROUP BY status",
    )

    id: RecordID | None = None
    status: str
    total: int
    revenue: float


print(OrderStats.view_ddl())
# DEFINE TABLE OVERWRITE `order_stats` AS (SELECT status, count() AS total, math::sum(amount) AS revenue FROM orders GROUP BY status);

await Order(id="o1", status="paid", amount=10.0, customer="ada").save()  # the source table exists first
await OrderStats.define_view()  # idempotent (OVERWRITE): run it at start-up

await Order(id="o2", status="paid", amount=5.5, customer="bob").save()
await Order(id="o3", status="new", amount=1.0, customer="ada").save()
for stats in await OrderStats.objects().order_by("status").exec():
    print(stats.status, stats.total, stats.revenue)
# new 1 1.0
# paid 2 15.5

paid = await OrderStats.objects().get(["paid"])
print(paid.id, paid.total)
# order_stats:['paid'] 2

try:
    await paid.merge(total=0)
except TypeError as error:
    print(error)
# Cannot modify materialized view 'order_stats': OrderStats.merge() writes to it, and SurrealDB computes a view's records from its view_query. Write to the tables it selects from instead.
```

#### Declaring a view

```python
class PaidOrder(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_name="paid_orders", view_query="SELECT customer, amount FROM orders WHERE status = 'paid'"
    )

    id: RecordID | None = None
    customer: str | None = None
    amount: float


await PaidOrder.define_view()
print([(str(order.id), order.amount) for order in await PaidOrder.objects().order_by("amount").exec()])
# [('paid_orders:o2', 5.5), ('paid_orders:o1', 10.0)]
```

- `view_query` is one `SELECT … FROM <table> [WHERE …] [GROUP BY … | GROUP ALL]` (a final `;` is dropped), checked
  when the class is defined: a mistake raises `TypeError` or `ValueError` at the `class` statement. It takes no
  `ORDER BY`, `LIMIT` or `FETCH` (SurrealDB refuses them in a view), and it is not a `SELECT VALUE`: a view stores
  records. The model's `get_view_query()` returns it as checked.
- **A grouped view** selects its `GROUP BY` fields (or expressions of them) and aggregates SurrealDB keeps up to
  date: `count()`, `math::sum()`, `math::mean()`, `math::min()`, `math::max()`, `time::min()`, `time::max()` (and
  `math::stddev()`, `math::variance()` on 3.x), whatever their argument (`math::sum(amount ?? 0)`). `math::median()`,
  `array::group()`, `array::distinct()`, `array::join()` and `vector::sum()` are refused (inside an aggregate's
  argument they are allowed). Since v0.32.1 that holds when an expression combines several aggregates too
  (`math::sum(amount) + math::sum(array::len(array::distinct(tags)))`); beside an aggregate
  (`math::sum(amount) + array::len(array::distinct(tags))`) they are still refused. On SurrealDB 3.x an expression
  around an aggregate (`math::sum(amount) / count()`) and a `GROUP BY` on an alias work; on 2.x and the embedded
  engine `define_view()` refuses them (see "SurrealDB 2.x and 3.x" below).
- Every source write runs the view's query for that record: when it fails — a function given a value of the wrong
  type, an aggregated field missing
  ([surrealdb/surrealdb#7447](https://github.com/surrealdb/surrealdb/issues/7447)) — SurrealDB refuses the write, on
  every line.
- **Ids**: a plain view keeps the source records' ids (`paid_orders:o1`); a grouped view's id is the array of its
  group values (`order_stats:['paid']`), and `GROUP ALL` gives `[]`. Declare `id: RecordID | None = None` and look a
  group up with `get(["paid"])` or `filter(id=["paid"])`; read a `GROUP ALL` view with `first()`. Declare sums and
  means `float`: SurrealDB 2.x and the embedded engine compute an `int` sum of integers and a `Decimal` mean, which a
  `float` field reads as a float.
- A view model cannot be an edge (`BaseSurrealEdge`), use `AuthenticatedUserMixin` or declare computed fields: each
  raises `TypeError` at the `class` statement. A subclass inherits `view_query` and defines the view on its own
  table; `view_query=None` makes it an ordinary model. The `table_name` is any identifier the ORM reads (a SurrealQL
  keyword such as `select` is defined, but not read by the QuerySet, as for every model).

#### Applying and removing

- `define_view()` writes `DEFINE TABLE OVERWRITE … AS (…)`: SurrealDB fills the view from the source's records, and
  each call recomputes the view and drops the events, fields and indexes defined on it, so at start-up it costs one
  pass over the source. `overwrite=False` writes `IF NOT EXISTS` and keeps a view already defined, whatever its
  query. It returns the statement it applied; `view_ddl()` returns the statement without applying it.
- The source table must exist (a record saved, or a `DEFINE TABLE`), else `SurrealDbError` naming it
  (`Can't apply view definition: 'order_stats' -> The table 'orders' does not exist`).
- Redefining a view drops the events, fields and indexes defined on it: run `define_events()` after `define_view()`.
- `remove_view()` writes `REMOVE TABLE IF EXISTS`, which removes the view and its records; with `missing_ok=False` a
  view that does not exist raises `SurrealDbNotFoundError`.
- The table guard: when the model's table already exists as an ordinary table **without records** — SurrealDB
  creates one when `define_events()` or, on 2.x, a `watch()` runs before `define_view()` — `define_view()` turns it
  into the view. When it holds records, `define_view()` raises `SurrealDbError` before any DDL, because SurrealDB
  would delete them (the message gives the `raw_query("REMOVE TABLE …")` to run if they are not needed).
  `define_view(overwrite=False)` raises on any ordinary table, which `IF NOT EXISTS` would silently keep, and
  `remove_view()` raises on a table that is not a view.
- A view may read another view: it is updated when the inner view changes, but not recomputed when the inner one is
  redefined.
- **Remove the views that read another view first.** SurrealDB 3.x refuses to remove a table that a view reads — a
  source table or an inner view — and `remove_view()` passes that refusal on (`SurrealDbError`,
  `Cannot delete table … on which a view is defined`). On 2.x and the embedded engine `remove_view()` raises
  `SurrealDbError` before any DDL, because those engines would remove it and turn every view reading it into an
  ordinary table that keeps its last records and no longer updates; the ORM finds those views by their plain `FROM`
  targets. A raw `REMOVE TABLE` of a source table does that on 2.x and the embedded engine: such a former view then
  makes `define_view()` refuse (it holds records: run the `raw_query("REMOVE TABLE …")` the message gives) and
  `remove_view()` refuse (it is not a view).

#### Reading

- Every read works — filters, `order_by()`, aggregates, `get()`, `refresh()`, `Subquery`, `fetch()`, `using()`, and
  on WebSocket `live()`/`watch()`, which receive the records SurrealDB recomputes. `relate()` and `remove_relation()`
  from or to a view record write the edge table and are allowed.
- On a view model, `filter(id__in=...)` is sent as `$ids CONTAINS id`: SurrealDB 3.x (3.1 and 3.3 measured) answers
  no record to `id IN` on a view table
  ([surrealdb/surrealdb#7608](https://github.com/surrealdb/surrealdb/issues/7608)), while `CONTAINS` is right on every
  line; write raw SurrealQL over a view the same way.
- `live()` instances carry the record id on both lines; `watch()` yields the server's envelopes as received: on 3.x
  their `result` has no `id` ([surrealdb/surrealdb#7607](https://github.com/surrealdb/surrealdb/issues/7607)), read
  `record`.
- On SurrealDB 3.x a live query on a view filtered on `id` never notifies (the server's view notifications carry no
  id, [surrealdb/surrealdb#7607](https://github.com/surrealdb/surrealdb/issues/7607)); filter on the view's fields,
  e.g. `filter(status="paid")`.

#### Writes are refused

- Every ORM write of a view model raises `TypeError` before any signal, transaction or request: the instance
  methods `save()`, `upsert()`, `update()`, `merge()`, `patch()`, `delete()` and the atomic helpers
  `atomic_append()`, `atomic_remove()`, `atomic_set_add()`, `atomic_increment()`, `atomic_append_many()`,
  `atomic_set_add_many()`, `atomic_remove_many()`, and the QuerySet methods `bulk_create()`, `bulk_update()`,
  `bulk_delete()`, `patch()`, `update_or_create()`, `get_or_create()` (even when the record exists) and
  `delete_table()`. The message starts with the full ORM's `Cannot modify materialized view '<table>'` and names the
  call: write to the tables the view selects from instead.
- The refusal comes before the call's own argument checks (a missing id, an invalid patch operation, a snapshot in
  `bulk_create()`'s list…). A refused call, under `tx=` or `objects(tx=…)`, leaves the transaction as it was: no
  statement, and its table is not counted as written, so a later `define_indexes(tx=…)` of the model in the block is
  not refused for it and the commit drops no cached read. Outside a transaction it drops no cached read either.
- `refresh()`, `relate()`, `remove_relation()`, `remove_all_relations()`, `define_events()` and `remove_event()` stay
  allowed: they do not write the view's table.
- Raw SurrealQL (`raw_query()`, `QuerySet.query()`, the client's `query()`) is not checked: SurrealDB 3.2 and later
  refuse a write to a view, 2.x and 3.1 accept it and change the view's records.

#### Transactions, connections and the query cache

- `define_view()` and `remove_view()` take `tx=`. In a buffered transaction (SurrealDB 2.x, HTTP, the embedded
  engine) the statement is applied at commit, and the block's later source writes update the view; in an interactive
  one (WebSocket and SurrealDB 3.x) it is applied in the transaction, which reads the view and its own source writes
  (`objects(tx=tx)`), unseen outside the block until it commits. Either way, a block that raises leaves no view. The
  table guard and the 2.x checks run before anything is queued, on committed state; a refusal of a queued statement
  comes back when the block commits, as the transaction's own error.
- A chain of views can be removed in one buffered block, outer view first (since v0.32.1): on 2.x and the embedded
  engine `remove_view(tx=tx)` does not count a view whose `remove_view(tx=tx)` the same block has already queued,
  unless the block names it again afterwards (a `define_view(tx=tx)` of it). A view removed in another transaction,
  or by a `REMOVE TABLE` you add to the block yourself, is still counted.
- Both run on the model's connection (its `connection` key, or the `using()` block's); a `tx=` of another connection
  raises `SurrealDbError` before any request. A view and its source tables live in one database: give a view model
  its sources' connection.
- The query cache ([section 29](#29-query-cache-cache-querycache)): reads of a view model, and reads whose subqueries
  read one, are dropped by every ORM write, since a write to a source table changes the view and the ORM does not know
  which tables a view reads. `define_view()` and `remove_view()` drop cached reads like every write. Writes by raw
  SurrealQL or by another process are not seen, as for every table.
- Defining and removing need a system user allowed to define: root, or a namespace or database `OWNER` or `EDITOR`.
  On a server, a `VIEWER` or a record user gets `SurrealDbAuthenticationError`.
- Applying DDL fires no signal, and neither do the records SurrealDB computes for a view.

#### SurrealDB 2.x and 3.x

- Raw writes to a view are accepted by 2.x and 3.1, which change the view's records, and refused by 3.2 and later.
  The ORM refuses its own writes on every line.
- Grouped views: 2.x and the embedded engine keep up to date only whole aggregates and `GROUP BY` fields, and
  compute anything else once per source record. So `define_view()` raises `SurrealDbError` before any DDL there for
  `math::stddev()` and `math::variance()`, an expression around an aggregate (`math::sum(amount) / count()`), a field
  outside `GROUP BY` and outside an aggregate (3.x refuses that one too), and a `GROUP BY` on an alias (2.x would put
  every record in one group).
- Value types: `int` sums and `Decimal` means on 2.x, `float` on 3.x; `count()` of an array field adds the arrays'
  lengths on 2.x, and counts the records whose array is not empty on 3.x.
- Live queries and events see an emptied group as an `UPDATE` then a `DELETE` on 2.x, a `DELETE` on 3.x.
- Removing a view, or a source table, that another view reads: 3.x refuses it; on 2.x `remove_view()` raises before
  any DDL, and a raw `REMOVE TABLE` leaves the views reading it as ordinary tables keeping their last records.
- On every line, a source write missing an aggregated field is refused
  ([surrealdb/surrealdb#7447](https://github.com/surrealdb/surrealdb/issues/7447)): write `math::sum(amount ?? 0)`.
- The [2.x/3.x table](#orm-behaviour-surrealdb-2x-vs-3x) lists these differences; the embedded engine behaves as a
  2.x server.

#### Security

The table name is checked as an identifier and written quoted in every statement the ORM sends. The query is written
into `AS (…)` as it is, since SurrealDB takes no bound parameter in a definition; when the class is defined the ORM
checks its structure: string literals and quoted identifiers terminated, brackets balanced, no NUL character, no
SurrealQL comment marker outside a literal, one `SELECT` and no `;` outside brackets. The check catches mistakes —
unbalanced brackets, a comment that would swallow the parenthesis the ORM appends, a second statement where one
SELECT is expected — and is not an injection defence: a view's query is trusted application code, never user input.
To filter on a value, store it in a record or a `DEFINE PARAM` and read it from the query; never format it into the
text. A refusal names the view's table, never the statement, and keeps the server's reason whole, which names the
source table to create.

Destructive DDL is guarded: `define_view()` never turns a table that holds records into a view and never lets
`IF NOT EXISTS` keep an ordinary table, and `remove_view()` never removes a table that is not a view nor, on 2.x and
the embedded engine, a view that another view reads. The guard is a check-then-act on committed state, not a lock: a
record written or a table created by another process between its reads and the DDL is not seen. The write refusal
guards against mistakes and is not an access control: on 2.x and 3.1 the server accepts writes to a view from any
session allowed to write the table. A view takes the permissions, comment and change feed its model declares
(`permissions=`, `comment=`, `changefeed=`, since v0.34.0); `define_view()` keeps those set by hand that the model
does not declare. Without either, SurrealDB applies its default `PERMISSIONS NONE`: a record user reads nothing from
a view. Applying a view needs a privileged system user: run `define_view()` at start-up or deployment, not on a
record user's session.

#### Coming from the full SurrealDB-ORM

- **`view_query` is the same key** (`SurrealConfigDict(view_query="SELECT …")`); `get_view_query()` is added and
  `_is_view()` kept.
- **`TypeError` with the same first words** (`Cannot modify materialized view '<table>'`), on every write path above
  (full ORM: `save()`, `update()`, `merge()`, `delete()` only).
- **`define_view()`/`remove_view()`/`view_ddl()`** apply, remove and render the view, where the full ORM applies one
  through migrations (`CreateTable`/`AlterTable`) or `define_table()`. `define_table()` (since v0.34.0,
  [section 35](#35-model-table-ddl-define_table-schema_mode-permissions)) accepts a view model and applies it through `define_view()`, so
  full-ORM code calling `define_table()` on a view works. Migrations come with v0.38.0.
- **`AS (…)` as in the full ORM, the table name quoted** (``DEFINE TABLE OVERWRITE `order_stats` AS (…)``).
- **Generated models write `view_query=`**, as the full ORM's do
  ([section 31](#31-schema-introspection-databaseintrospector-generate_models_from_db)).
- **The documented `MaterializedView` base class with `source_query` is not shipped by the full ORM either**: declare
  `view_query` on a `BaseSurrealModel`.

### 34. Relation constraints (`relation_in`, `define_relation()`)

Since v0.33.0 an edge model ([section 23](#23-typed-edges-basesurrealedge)) declares which tables its edges may join
and whether both ends must exist — SurrealDB's `TYPE RELATION IN … OUT … ENFORCED` — with the full SurrealDB-ORM's
keys: `relation_in`, `relation_out`, `enforced` and `table_type=TableType.RELATION`. `define_relation()` applies the
constraint to the table and SurrealDB enforces it on every write, the same on SurrealDB 2.x, 3.x and the embedded
engine, except where the [2.x/3.x table](#orm-behaviour-surrealdb-2x-vs-3x) says otherwise. The examples assume a
configured connection: they run as written on the embedded engine (`mem://`).

```python
from surrealdb import RecordID

from surreal_orm_lite import (
    BaseSurrealEdge,
    BaseSurrealModel,
    SurrealConfigDict,
    SurrealDbError,
    SurrealDbNotFoundError,
    TableType,
)


class Person(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="person")

    id: str | None = None
    name: str


class Company(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="company")

    id: str | None = None
    name: str


class Post(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="post")

    id: str | None = None
    title: str


class Likes(BaseSurrealEdge):
    model_config = SurrealConfigDict(
        table_name="likes",
        table_type=TableType.RELATION,  # optional: an edge model's table is TYPE RELATION
        relation_in="person",
        relation_out=["post", "book"],
        enforced=True,
    )

    stars: int = 1


print(Likes.relation_ddl())
# DEFINE TABLE OVERWRITE `likes` TYPE RELATION IN `person` OUT `post` | `book` ENFORCED;
print(await Likes.define_relation())
# DEFINE TABLE OVERWRITE `likes` TYPE RELATION IN `person` OUT `post` | `book` ENFORCED;

ada = await Person(id="ada", name="Ada").save()
acme = await Company(id="acme", name="Acme").save()
graphs = await Post(id="graphs", title="Graphs").save()

edge = await ada.relate(Likes, graphs, stars=5)
print(edge.in_, edge.out)
# person:ada post:graphs
try:
    await acme.relate(Likes, graphs)  # a company is not a person
except SurrealDbError as exc:
    print(type(exc).__name__)
# SurrealDbError
try:
    await ada.relate(Likes, RecordID("book", "missing"))  # ENFORCED: the book must exist
except SurrealDbNotFoundError as exc:
    print(exc)
# There was a problem with the database: The record 'book:missing' does not exist
```

#### Declaring the constraint

- `relation_in` names the tables an edge's `in` may come from, `relation_out` the tables its `out` may point to: one
  table (`"person"`), several separated by `|` as SurrealQL writes them (`"post | book"`), or a list or tuple of names
  (`["post", "book"]`). Each name is stripped and must be an identifier (letters, digits, underscores, not starting
  with a digit); the names are written in backquotes, in the order given, and a repeated name is kept, as SurrealDB
  keeps it. `None`, `""`, `[]` and `()` mean any table: no `IN` (or `OUT`) clause.
- `enforced=True` makes SurrealDB refuse an edge whose endpoint record does not exist (`ENFORCED`); `False` and `None`
  write nothing.
- `table_type=TableType.RELATION` is optional: an edge model's table is always `TYPE RELATION`, and an edge model
  with another `TableType` raises `TypeError`. `TableType` is the full ORM's enum (`NORMAL`, `USER`, `STREAM`, `HASH`,
  `RELATION`, `ANY`); its other members take effect in `define_table()`
  ([section 35](#35-model-table-ddl-define_table-schema_mode-permissions)).
- The keys are checked when the class is defined: a `table_type` that is not a `TableType` (a plain `"relation"`
  string included), an `enforced` that is not a bool, or a value that is not a string, a list or a tuple raises
  `TypeError`; a name that is not an identifier, or a string of spaces only, raises `ValueError`. On a node model
  (`BaseSurrealModel`), `relation_in`, `relation_out` and `enforced` — even empty or `False` — and
  `table_type=TableType.RELATION` raise `TypeError`: derive the model from `BaseSurrealEdge`. A subclass inherits the
  keys; setting one to `None` resets it.
- The getters read the declaration back: `get_relation_in()` and `get_relation_out()` return tuples (`()` for any
  table), the shape of introspection's `TableState.relation_in` and `relation_out`
  ([section 31](#31-schema-introspection-databaseintrospector-generate_models_from_db)), so a model compares with the
  database; `is_enforced()` returns a bool; `get_table_type()`, on every model as in the full ORM, returns
  `TableType.RELATION` for an edge model, else the configured `table_type`, else `TableType.NORMAL`.

```python
print(Likes.get_relation_in(), Likes.get_relation_out(), Likes.is_enforced(), Likes.get_table_type())
# ('person',) ('post', 'book') True relation
```

#### Applying it

```python
from surreal_orm_lite import SurrealDBConnectionManager


class Follows(BaseSurrealEdge):
    model_config = SurrealConfigDict(table_name="follows", relation_in="person", relation_out="person")


client = await SurrealDBConnectionManager.get_client()
await client.query("DEFINE TABLE follows SCHEMAFULL CHANGEFEED 1d PERMISSIONS FOR select FULL;", {})
print(Follows.relation_ddl())
# DEFINE TABLE OVERWRITE `follows` TYPE RELATION IN `person` OUT `person`;
print(await Follows.define_relation())
# DEFINE TABLE OVERWRITE `follows` TYPE RELATION IN `person` OUT `person` SCHEMAFULL CHANGEFEED 1d PERMISSIONS FOR select FULL, FOR create, update, delete NONE;
```

- `define_relation()` reads the table's definition once (`INFO FOR DB`), then sends one `DEFINE TABLE OVERWRITE` that
  writes the model's `TYPE RELATION` clause and keeps the table's schema mode, `DROP`, comment, change feed and
  permissions — those the model declares (since v0.34.0) are written instead. It returns the statement it applied; `relation_ddl()` returns the bare statement, without a
  connection. Applied by hand, that bare `OVERWRITE` would reset the other clauses to SurrealDB's defaults —
  `SCHEMALESS`, no change feed, `PERMISSIONS NONE`, which hides every edge from record users.
- The table's records, fields, indexes and events, the views that read it
  ([section 33](#33-materialized-views-view_query-define_view)), its live queries and its change feed's history are
  kept; the views keep counting the edges written afterwards. Each `define_relation()` on a table with a change feed
  adds a `define_table` entry to it; `changes()` skips it, a raw `SHOW CHANGES` shows it.
- The constraint is enforced at once, on every connection. A missing table is defined as a relation table. The call
  is idempotent: run it at start-up.
- `overwrite=False` writes `DEFINE TABLE IF NOT EXISTS`: it defines a missing table and leaves a relation table as it
  is, whatever its ends. On a table that exists and is not a relation table it raises `SurrealDbError` before any
  DDL, because `IF NOT EXISTS` would leave the constraint unapplied: call it with `overwrite=True`, which keeps the
  other clauses.
- `define_relation()` raises `SurrealDbError` before any DDL on a materialized view (the `OVERWRITE` would drop its
  query) and on a definition the ORM cannot read back (it could not keep the other clauses).
- An edge model that declares `schema_mode=SchemaMode.SCHEMAFULL` and has data fields — the model
  `generate_models_from_db()` writes for a `SCHEMAFULL` relation table with data fields, since v0.34.0 — is applied
  with `define_table()`, not `define_relation()`: `define_relation()` defines no field, so it raises `SurrealDbError`
  before any request, and `define_table()` applies the same relation statement, then the data fields
  ([section 35](#35-model-table-ddl-define_table-schema_mode-permissions)).
- SurrealDB rewrites the `in` and `out` field definitions from `IN` and `OUT` (`TYPE record<person>`): an `ASSERT`
  written on them by hand is lost. Declare the tables with `relation_in` and `relation_out` instead.
- Why not `ALTER TABLE`, which keeps the other clauses too: on SurrealDB 2.x and the embedded engine, a raw
  `ALTER TABLE … TYPE RELATION …` of a table already written to is not enforced until a `DEFINE TABLE OVERWRITE` of
  the table. The ORM sends `DEFINE TABLE OVERWRITE` on every line.

#### What SurrealDB refuses

- **Creating an edge**, through every creation path — `relate()` with an edge model or by table name (the string
  form since v0.33.0), an edge's `save()`, `objects().bulk_create()`: an endpoint from a table outside `IN`/`OUT`
  raises `SurrealDbError`; with `ENFORCED`, an endpoint record that does not exist raises `SurrealDbNotFoundError`.
  The SDK's error is the `__cause__`. A batch with one refused edge writes nothing. Inside a transaction the refusal
  is the transaction's `SurrealDbError` ("Transaction failed and rolled back: …").
- **Writing a plain record** into the relation table — the `save()` of a node model declared on it, with or without
  an id — raises the SDK's `InternalError` ("Found record: … but expected a RELATION …"). The ORM never sends one for
  an edge.
- **Updating** an edge the constraint refuses (one stored before it, next subsection) raises the SDK's
  `InternalError` too.
- Both are `surrealdb.ServerError`s, as every update the server refuses in the lite (an event's `THROW`,
  [section 32](#32-server-side-events-and-stored-functions-event-define_events-define_function); a failed `patch()`
  test, [section 13](#13-patch--atomic-fieldarray-operations)).

`SurrealDbNotFoundError` is a `SurrealDbError`, so `except SurrealDbError` handles every refused edge creation the
same way on both lines.

#### Records already in the table

SurrealDB checks the writes made after `define_relation()`, not the records already in the table:

- a nonconforming edge (from or to a table outside `IN`/`OUT`) stays and is still traversed, but its `merge()`,
  `update()`, `patch()`, `atomic_*()` and `objects().bulk_update()` raise the SDK's `InternalError` (the mismatch
  text);
- a dangling edge (an endpoint record that no longer exists) stays and can still be updated: `ENFORCED` is checked
  when an edge is created;
- plain records stay.

`delete()` always works. Two raw queries find the edges to fix or delete, through the model's `raw_query()`: those
outside the ends (`record::tb(in) NOT IN [...]`) and the dangling ones (`in.id IS NONE OR out.id IS NONE`).

```python
from surrealdb import ServerError

await ada.relate("knows", acme)  # written before any constraint: acme is a company


class Knows(BaseSurrealEdge):
    model_config = SurrealConfigDict(table_name="knows", relation_in="person", relation_out="person")

    since: int | None = None


await Knows.define_relation()
[old] = await Knows.objects().exec()  # the edge stays
try:
    await old.merge(since=2020)
except ServerError as exc:
    print(type(exc).__name__)
# InternalError

outside = await Knows.raw_query(
    "SELECT VALUE id FROM knows WHERE record::tb(in) NOT IN ['person'] OR record::tb(out) NOT IN ['person']"
)
dangling = await Knows.raw_query("SELECT VALUE id FROM knows WHERE in.id IS NONE OR out.id IS NONE")
print(outside == [old.id], dangling)
# True []
await old.delete()
```

#### Removing the constraint

The model is the source of truth: leave `relation_in`, `relation_out` and `enforced` out (or set them to `None`;
empty ends — `""`, `[]`, `()` — and `enforced=False` do the same) and call `define_relation()` again. The table
becomes `TYPE RELATION` with no ends, at once on every connection, and keeps its other clauses, records, fields,
indexes and events:

```python
class LikesAny(BaseSurrealEdge):
    model_config = SurrealConfigDict(table_name="likes")  # no relation key: any table, not enforced


print(await LikesAny.define_relation())
# DEFINE TABLE OVERWRITE `likes` TYPE RELATION;
edge = await acme.relate(LikesAny, graphs)  # accepted now
print(edge.in_, edge.out)
# company:acme post:graphs
```

- `overwrite=False` removes nothing: `IF NOT EXISTS` leaves a relation table as it is.
- Turning the table back into `TYPE ANY` is not offered, and it does not lift the constraint: SurrealDB keeps the
  `in` and `out` field definitions (`TYPE record<person>`) through `ALTER TABLE … TYPE ANY` and
  `DEFINE TABLE OVERWRITE … TYPE ANY`, on every line. With raw SurrealQL,
  `REMOVE FIELD in ON likes; REMOVE FIELD out ON likes;` drops them.
- `REMOVE TABLE likes` removes the table and its edges. `remove_relation()` deletes edges, not the constraint.

#### Transactions, connections and the query cache

- `define_relation()` takes `tx=`. In a buffered transaction (SurrealDB 2.x, HTTP, the embedded engine) the
  statement is applied at commit; in an interactive one (WebSocket and SurrealDB 3.x) it is applied in the
  transaction. A block that raises applies nothing. An edge the constraint refuses later in the same block fails it,
  with `SurrealDbError` ("Transaction failed and rolled back: …"): inside the block when it is interactive, at its
  exit when it is buffered.
- The read of the table's definition runs before anything is queued, on committed state. So a definition of the same
  table queued earlier in the block (`tx.add("DEFINE TABLE likes … SCHEMAFULL …")`) is not seen by
  `define_relation()` and is replaced at commit by the statement it builds: define the table's other clauses before
  the block.
- The read and the definition are a check, then the DDL, not a lock: a clause another process changes between the
  two is overwritten with the value read.
- `define_relation()` runs on the model's connection (its `connection` key, or the `using()` block's); a `tx=` of
  another connection raises `SurrealDbError` before any request.
- The query cache ([section 29](#29-query-cache-cache-querycache)): `define_relation()` changes no record, so it drops
  no cached read.
- Defining needs a system user allowed to define: root, or a namespace or database `OWNER` or `EDITOR`. A session
  that may not read the definitions or define the table gets `SurrealDbAuthenticationError` (a record user, at the
  read). Applying DDL fires no signal.

#### SurrealDB 2.x and 3.x

- `define_relation()` is the same on every line and on the embedded engine: one `INFO FOR DB`, one
  `DEFINE TABLE OVERWRITE`, enforced at once.
- The wording of the server's refusals differs: `Found … but expected a record<…>` on 2.x and the embedded engine,
  `Couldn't coerce value for field …` on 3.x.
- An endpoint both outside `IN`/`OUT` and missing, on an `ENFORCED` table, raises `SurrealDbNotFoundError` on 2.x and
  the embedded engine (the server reports it missing) and `SurrealDbError` on 3.x. `SurrealDbNotFoundError` is a
  `SurrealDbError`, so `except SurrealDbError` handles every refused edge creation the same way on both lines.
- A raw `ALTER TABLE … TYPE RELATION …` of a table already written to is not enforced on 2.x and the embedded engine
  until a `DEFINE TABLE OVERWRITE` of the table; 3.x enforces it at once. The ORM never sends `ALTER TABLE`.
- The [2.x/3.x table](#orm-behaviour-surrealdb-2x-vs-3x) and the
  [embedded engine's](#orm-behaviour-the-embedded-engine) list these rows.

#### Security

The table name and every `relation_in`/`relation_out` name are checked as identifiers when the class is defined and
written in backquotes; no other value of yours reaches the statement, and a definition takes no bound parameter. The
clauses `define_relation()` keeps are the database's own definition sent back to it: the comment re-quoted, each
permission rule re-checked (literals terminated, brackets balanced, no comment marker, no `;` outside brackets). A
value that fails is not written: `define_relation()` raises before any DDL. These checks catch a definition the ORM
would misread; they are not an injection defence, since the source is the database itself. A refusal names the
table, never the statement, which may carry permission rules that hold secrets.

The constraint is SurrealDB's: the ORM adds no check of its own before a write, and the constraint is not an access
control — a session allowed to write the table writes any edge the constraint accepts. `define_relation()` needs a
privileged session (a system user allowed to define): run it at start-up or deployment, not on a record user's
session.

#### Coming from the full SurrealDB-ORM

- **The keys and `TableType` are the same**: `table_type=TableType.RELATION`, `relation_in`, `relation_out` and
  `enforced`, and `TableType`'s six members and values.
- **The relation model is `BaseSurrealEdge`**: a `BaseSurrealModel` with `table_type=TableType.RELATION` (the full
  ORM's `class Likes(BaseSurrealModel)` form) raises `TypeError`; derive it from `BaseSurrealEdge`.
- **`relation_ddl()` and `define_relation()` on the edge model** render and apply the constraint, where the full ORM
  applies the DDL through migrations or `define_table()`. `define_table()` (since v0.34.0) applies an edge model
  through the statement `define_relation()` carries, then its data fields; migrations come with v0.38.0.
- **The clauses the model declares (`schema_mode`, `permissions`, `changefeed`, `comment`, since v0.34.0) are
  written, the others kept from the database** (the full ORM renders every clause from the model and resets the
  others).
- **Names are identifiers, written in backquotes, and a pipe-joined string is parsed** (the full ORM writes the value
  verbatim).
- **A `table_type` that is not a `TableType` and an `enforced` that is not a bool raise `TypeError`** (the full ORM
  ignores the first and takes the truthiness of the second).
- **`get_table_type()` is `TableType.RELATION` for every edge model**, and the lite adds `get_relation_in()`,
  `get_relation_out()` and `is_enforced()`.
- **A refused edge creation raises the ORM's `SurrealDbError` or `SurrealDbNotFoundError`, a refused update the
  official SDK's `InternalError`** (full ORM: its SDK's `QueryError` for both).
- **Generated edge models carry the keys but no `table_type=`**
  ([section 31](#31-schema-introspection-databaseintrospector-generate_models_from_db)): their base class,
  `BaseSurrealEdge`, says it.
- **`TableType`'s other members take effect in `define_table()`**
  ([section 35](#35-model-table-ddl-define_table-schema_mode-permissions)).
- **`remove_relation()` deletes edges, not a constraint**: the name is the graph API's (section 7); a keyless
  `define_relation()` removes the constraint.
- **Upgrading from 0.32 or earlier**: these keys were accepted and ignored before 0.33.0. They are now checked at the
  `class` statement, so a node model carrying `relation_in`, `relation_out` or `enforced`, or
  `table_type=TableType.RELATION`, or a `table_type` given as a plain string (`"relation"`), raises `TypeError` at
  import: derive the model from `BaseSurrealEdge`, import `TableType`.

### 35. Model table DDL (`define_table()`, `schema_mode`, `permissions`)

Since v0.34.0 a model declares its table's schema mode, change feed, permissions and comment with the full
SurrealDB-ORM's keys — `schema_mode`, `changefeed`, `permissions`, `comment` and `flexible_fields` — and
`define_table()` writes its `DEFINE TABLE` and one `DEFINE FIELD` per column from the annotations and applies them,
keeping what the model does not declare. The same code runs on SurrealDB 2.x, 3.x and the embedded engine, except
where the [2.x/3.x table](#orm-behaviour-surrealdb-2x-vs-3x) says otherwise. The examples assume a configured
connection: they run as written on the embedded engine (`mem://`).

```python
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from surreal_orm_lite import BaseSurrealModel, SchemaMode, SurrealConfigDict


class Tier(StrEnum):
    FREE = "free"
    PRO = "pro"


class Address(BaseModel):
    city: str
    zip: str | None = None


class Person(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_name="person",
        schema_mode=SchemaMode.SCHEMAFULL,
        changefeed="7d",
        permissions={"select": "FULL", "create": "$auth.id != NONE"},
        comment="People",
    )

    id: str | None = None
    name: str
    age: int = 18
    nickname: str | None = None
    tier: Tier = Tier.FREE
    tags: list[str] = Field(default_factory=list)
    address: Address | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    password: str = Field(alias="password_hash")


print(*Person.table_ddl(), sep="\n")
# DEFINE TABLE OVERWRITE `person` TYPE NORMAL SCHEMAFULL COMMENT 'People' CHANGEFEED 7d PERMISSIONS FOR select FULL, FOR create WHERE $auth.id != NONE, FOR update, delete NONE;
# DEFINE FIELD OVERWRITE `name` ON `person` TYPE string;
# DEFINE FIELD OVERWRITE `age` ON `person` TYPE int DEFAULT 18;
# DEFINE FIELD OVERWRITE `nickname` ON `person` TYPE option<string>;
# DEFINE FIELD OVERWRITE `tier` ON `person` TYPE 'free' | 'pro' DEFAULT 'free';
# DEFINE FIELD OVERWRITE `tags` ON `person` TYPE array<string>;
# DEFINE FIELD OVERWRITE `address` ON `person` TYPE option<object>;
# DEFINE FIELD OVERWRITE `address`.`city` ON `person` TYPE string;
# DEFINE FIELD OVERWRITE `address`.`zip` ON `person` TYPE option<string>;
# DEFINE FIELD OVERWRITE `meta` ON `person` TYPE object FLEXIBLE;
# DEFINE FIELD OVERWRITE `password_hash` ON `person` TYPE string;
sql = await Person.define_table()  # idempotent: run it at start-up
print(sql == "\n".join(Person.table_ddl()))
# True

await Person(id="ada", name="Ada", address=Address(city="Paris"), meta={"plan": {"seats": 3}}, password="hash").save()
ada = await Person.objects().get("ada")
print(ada.age, ada.tier, ada.address.city, ada.meta)
# 18 free Paris {'plan': {'seats': 3}}
```

#### Declaring the table

- `schema_mode` is `SchemaMode.SCHEMAFULL` (only the fields the table defines are stored) or `SchemaMode.SCHEMALESS`
  (any field is stored, SurrealDB's default): the full ORM's enum, exported by the package. Left out, or `None`, the
  mode is not declared: `define_table()` keeps the stored one, and a new table is `SCHEMALESS`.
- `changefeed` is a duration such as `"7d"`, `"1h30m"` or `"1w2d"`, optionally followed by `INCLUDE ORIGINAL`, in any
  case (`"1h include original"`).
- `permissions` maps an operation — `"select"`, `"create"`, `"update"`, `"delete"` — to `"FULL"`, `"NONE"`,
  `"WHERE <condition>"` or a bare condition, the full ORM's form (`"$auth.id != NONE"` is written
  `WHERE $auth.id != NONE`). An operation left out is `NONE`, SurrealDB's default, and `permissions={}` declares
  `PERMISSIONS NONE`: the mapping replaces the table's permissions whole.
- `comment` is a string, written quoted (`"it's"` is written `COMMENT 'it\'s'`).
- `flexible_fields` is a list or a tuple of field names written `FLEXIBLE` on a `SCHEMAFULL` table, whose objects then
  keep the keys the model does not declare — what `Field(json_schema_extra={"flexible": True})` does for one field.
  Only a kind that holds an object takes it (a `dict`, a sub-model); another raises `ValueError` when the statements
  are built (`table_ddl()`, `define_table()`).
- `table_type` (v0.33.0's `TableType`) takes effect: `TYPE NORMAL` for `TableType.NORMAL`, `USER`, `STREAM` and
  `HASH` and for a model without `table_type`, `TYPE ANY` for `TableType.ANY` — a table that holds records and edges.
  `USER` makes the table `SCHEMAFULL` and `HASH` makes it `SCHEMALESS` when `schema_mode` is left out; `STREAM` adds
  nothing (declare `changefeed`).
- The keys are checked when the class is defined: a value of the wrong type raises `TypeError` (a plain-string
  `schema_mode`, even `"SCHEMAFULL"`, included), a value of the wrong form `ValueError` (an operation SurrealDB does
  not have, an empty rule, a change feed it does not read, a NUL character in the comment, a name that is not a field).
  `schema_mode=SchemaMode.SCHEMAFULL` with `extra="allow"`, and `TableType.USER` with `SchemaMode.SCHEMALESS`, raise
  `TypeError`. A subclass inherits the keys; setting one to `None` resets it.
- The getters read the declaration back: `get_schema_mode()` returns the declared mode, else `SCHEMAFULL` for
  `TableType.USER` and `SCHEMALESS` for `TableType.HASH`, else `None` (not declared); `get_changefeed()` returns the
  normalised text (`"1h INCLUDE ORIGINAL"` for `" 1h   include original "`), or `None`; `get_permissions()` returns the
  declared mapping as given — a copy, so changing it never changes the model —, and `{}` when the model declares none,
  and for `permissions={}`, which declares `PERMISSIONS NONE`.

```python
print(Person.get_schema_mode(), Person.get_changefeed(), Person.get_permissions())
# SCHEMAFULL 7d {'select': 'FULL', 'create': '$auth.id != NONE'}
```

#### Field types

Each column's `TYPE` comes from its annotation (`Annotated` unwrapped):

| Python annotation                                                                                                                            | SurrealQL kind                                                                                                                        |
| -------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `str`, and a `str` subclass that is not an `Enum`                                                                                            | `string`                                                                                                                              |
| `bool`, `int`, `float`, `Decimal`, `bytes`, `UUID`                                                                                           | `bool`, `int`, `float`, `decimal`, `bytes`, `uuid`                                                                                    |
| a union of `int`, `float` and `Decimal`, in any order                                                                                        | `number`                                                                                                                              |
| `datetime`, `date`                                                                                                                           | `datetime`                                                                                                                            |
| `timedelta`, `Duration` (the ORM's or the SDK's)                                                                                             | `duration`                                                                                                                            |
| `RecordID`                                                                                                                                   | `record`                                                                                                                              |
| a geometry field: `PointField`, `GeoField[…]`, a geometry class or a union of them                                                           | `geometry<point>` for one kind, the union of its kinds in SurrealDB's order for several, `geometry` for every kind                    |
| `Range`, `Range[T]`                                                                                                                          | `range`                                                                                                                               |
| `Literal["a", "b"]`, `Literal[1, 2]`                                                                                                         | the union of its values (`'a'` or `'b'`, `1` or `2`); a `bool` value gives `bool`, `None` makes it `option<…>`, any other value `any` |
| an `Enum` whose values are all `str` or `int` (`StrEnum`, `IntEnum`)                                                                         | the union of its values, in definition order                                                                                          |
| an `enum.Flag` or `IntFlag`                                                                                                                  | `int`: Pydantic also accepts any combination of its members, and zero                                                                 |
| `list[T]`, `Sequence[T]`, `tuple[T, ...]`                                                                                                    | `array<T>`                                                                                                                            |
| `list`, `list[Any]`                                                                                                                          | `array`, and an item definition (below)                                                                                               |
| `tuple[A, B]`                                                                                                                                | `[A, B]`                                                                                                                              |
| `set[T]`, `frozenset[T]`; `set`                                                                                                              | `set<T>`; `set`                                                                                                                       |
| `dict`, `dict[K, V]`, `Mapping[K, V]`                                                                                                        | `object`, free-form                                                                                                                   |
| a Pydantic model `M`, or a list of them, optional or not                                                                                     | `object`, `array<object>` (`option<…>` when optional), then one column per field of `M`                                               |
| `M` in a union with another type, a `dict` or a `tuple`; a record model (`BaseSurrealModel`, `BaseSurrealEdge`); a model already on the path | `object`, free-form                                                                                                                   |
| `Optional[X]`, a union with `None`                                                                                                           | `option<X>`                                                                                                                           |
| any other union                                                                                                                              | the union of its members' kinds, `any` when one is `any`; a list or a set with an object or another container: `array` (below)        |
| `Any`, `object`, the SDK's `Datetime`                                                                                                        | `any`                                                                                                                                 |

Anything else is `any`.

A field annotated with a name defined after the class (a forward reference: `score: "Score" = 0`, then `Score = int`)
gets the kind of the type its name resolves to. Since v0.37.1, `table_ddl()` and `define_table()` complete such a model
first, as its first instantiation does, so they write the same statements before and after an instance (`score` is
`TYPE int DEFAULT 0`, which earlier versions wrote `TYPE any DEFAULT 0` until the model was instantiated or rebuilt). A
name still undefined gives `any`.

- **Columns**: every field but `id` (and an edge's `in_` and `out`), under its column — its alias, if any
  ([section 20](#20-field-aliases-server_fields-and-mergerefreshfalse)) —, then the Pydantic `@computed_field`s; a
  `Field(exclude=True)` field is left out. A sub-model's fields follow its column (`address.city`, `subs.*.city`), its
  `@computed_field`s included; a sub-model with `extra="allow"` keeps its columns and is free-form too.
- **Optional**: a column is `option<…>` when its annotation admits `None` or its default is `None` (`x: int = None`),
  never `any`, which admits `NONE` already.
- **`DEFAULT`**: the constant default of a column that is not optional — a `bool`, `int`, `float`, `Decimal`, `str`,
  `datetime`, `date`, `timedelta`, `Duration`, `UUID` or enum member — is its `DEFAULT` (`DEFAULT 18`,
  `DEFAULT 'free'`, `DEFAULT 1.50dec`). An optional column gets none: SurrealDB applies `DEFAULT` to a `NONE` on every
  create, so a `None` the ORM writes would become the default. A container, a record, a geometry, a range and a
  `default_factory` get none either: the ORM always sends them.
- **`server_fields`** ([section 20](#20-field-aliases-server_fields-and-mergerefreshfalse)): the database's `DEFAULT`,
  `VALUE` or `COMPUTED` on the column is kept, and the model's constant default is written `DEFAULT` only where the
  database has none, since a `save()` leaves a server field out. A `server_fields` column that is not nullable and has
  no constant default (a `default_factory`, or a required field) is written `TYPE <kind>` with no `DEFAULT`: every
  create — a `save()` that creates the record, `objects().bulk_create()`, `signup()` — leaves it out, so SurrealDB
  refuses it until the database gives the column a `DEFAULT` or `VALUE`
  (`DEFINE FIELD OVERWRITE created ON note TYPE datetime DEFAULT time::now()`), which `define_table()` then keeps; or
  annotate it `T | None`.
- **Computed fields** ([section 16](#16-computed-fields-computed--define-field--value)): `Computed[T]` is
  `TYPE option<T>`. A computed field is written `VALUE`; over a field the database defines `COMPUTED` (SurrealDB 3.x),
  `define_table()` writes `COMPUTED`: replacing it by `VALUE` would leave the stored records without the field until
  their next write. The expression is checked before any request: a SurrealQL comment marker (`--`, `#`, `//`,
  `/* */`), unbalanced brackets or an unterminated literal raise `ValueError`, since a comment marker would swallow the
  clauses written after it.
- **Item definitions**: an untyped `list` — `list`, `list[Any]`, `list[list]`, `list | None` — gets one more column
  for its items, `TYPE any` (`bag.*`, `lol.*.*`), on every line: without it SurrealDB 3.x refuses an object item of a
  required untyped array (`no such field exists`). A typed list (`list[str]`, `list[dict]`, `list[M]`) and a set get
  none.
- **Unions of containers**: a union holding a list or a set together with an object (a `dict`, a model) or another
  list, set or tuple writes each of its lists, sets and tuples as an untyped `array`, which gets its item definition:
  `dict[str, Any] | list[str]` is `object | array` with `dl.*` `TYPE any`, `list[int] | list[str]` is `array`.
  SurrealDB derives a single item type from such a union and applies it to every member, so `object | array<string>`
  would refuse `{"k": 1}`, and SurrealDB 2.x `array<int> | array<string>` would refuse `["a"]`. A set in such a union is
  stored as the array the ORM writes, and reads back as the declared set. The objects of such a list become `any`
  items: `list[dict[str, Any]] | list[str]` is `array`, which holds no object, so it is not a free-form object column
  (SurrealDB 3.x refuses `FLEXIBLE` on it) and its item definition keeps the nested objects.
- **`FLEXIBLE`**, written after `TYPE`, only on a table that is `SCHEMAFULL`: on a free-form object column (a `dict`,
  a record model, a sub-model with `extra="allow"`), on the fields of `flexible_fields` or
  `json_schema_extra={"flexible": True}`, and, on SurrealDB 2.x and the embedded engine, on the columns that hold `any`
  and on each list of sub-models (`list[M]`, at any depth) holding a `FLEXIBLE` column (see "SurrealDB 2.x and 3.x"
  below). Such a column keeps the keys the model does not declare. A stored `FLEXIBLE` on an object column is kept.

#### Applying it

```python
from datetime import datetime

from surreal_orm_lite import SurrealDBConnectionManager


class Note(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="note", server_fields=["created"])

    id: str | None = None
    body: str
    email: str | None = None
    created: datetime | None = None
    done: bool = False


client = await SurrealDBConnectionManager.get_client()
await client.query(  # a table tuned by hand
    "DEFINE TABLE note SCHEMAFULL COMMENT 'keep' CHANGEFEED 1h PERMISSIONS FOR select WHERE owner = $auth.id;"
    "DEFINE FIELD email ON note TYPE string ASSERT $value != '' COMMENT 'mail';"
    "DEFINE FIELD created ON note TYPE option<datetime> DEFAULT time::now() READONLY;",
    {},
)
print(await Note.define_table())
# DEFINE TABLE OVERWRITE `note` TYPE NORMAL SCHEMAFULL COMMENT 'keep' CHANGEFEED 1h PERMISSIONS FOR select WHERE owner = $auth.id, FOR create, update, delete NONE;
# DEFINE FIELD OVERWRITE `body` ON `note` TYPE string;
# DEFINE FIELD OVERWRITE `email` ON `note` TYPE option<string> ASSERT $value != '' COMMENT 'mail';
# DEFINE FIELD OVERWRITE `created` ON `note` TYPE option<datetime> DEFAULT time::now() READONLY;
# DEFINE FIELD OVERWRITE `done` ON `note` TYPE bool DEFAULT false;
note = await Note(id="n1", body="b").save()
print(note.created is not None, note.done)
# True False
```

`define_table()` reads the table's definition — one `INFO FOR DB`, and one `INFO FOR TABLE` for a table that exists
— then sends one statement per call: the `DEFINE TABLE`, then one `DEFINE FIELD` per column, parents before children.
It returns the statements applied (queued, with a buffered `tx`), table first, joined with `"\n"` — the full ORM's
return. On a new table that is `"\n".join(Model.table_ddl())`, except on SurrealDB 2.x, where `define_table()` adds
`FLEXIBLE` to the `Any`/untyped columns of a `SCHEMAFULL` table and to its lists of sub-models holding a `FLEXIBLE`
column. `table_ddl()` renders the model's statements as a list, without a connection: applied by hand, their `OVERWRITE`
resets every clause the model does not write.

What the model declares wins; the rest is kept from the database:

| Table clause            | The model declares it                                                                            | The model leaves it out      |
| ----------------------- | ------------------------------------------------------------------------------------------------ | ---------------------------- |
| type                    | always the model's: `TYPE NORMAL` or `TYPE ANY` (`TYPE RELATION …` for an edge, none for a view) | —                            |
| schema mode             | `schema_mode`, or `TableType.USER`/`HASH`: `SCHEMAFULL` or `SCHEMALESS` written                  | the stored `SCHEMAFULL` kept |
| `COMMENT`, `CHANGEFEED` | `comment`, `changefeed`                                                                          | the stored ones kept         |
| `PERMISSIONS`           | `permissions`, written whole                                                                     | the stored ones kept         |
| `DROP`                  | —                                                                                                | the stored one kept          |

| Field clause                                                | Written from the model                                                                                                                          | Kept from the database                       |
| ----------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------- |
| `TYPE`                                                      | always                                                                                                                                          | —                                            |
| `FLEXIBLE`                                                  | as in "Field types"                                                                                                                             | a stored one, on a kind that holds an object |
| `DEFAULT`                                                   | a constant default of a column that is not optional (a `server_fields` column: only where the database has no `DEFAULT`, `VALUE` or `COMPUTED`) | when the model writes none                   |
| `VALUE`, `COMPUTED`                                         | a computed field's expression: `COMPUTED` over a stored `COMPUTED`, else `VALUE`                                                                | when the model writes none                   |
| `REFERENCE`, `READONLY`, `ASSERT`, `PERMISSIONS`, `COMMENT` | —                                                                                                                                               | always                                       |

- Nothing is removed: the table keeps its records, the fields, clauses, indexes and events the model does not declare,
  the views that read it ([section 33](#33-materialized-views-view_query-define_view)), its live queries and its
  change feed's history. A key left out never removes a stored clause: remove a field, a change feed or a comment with
  raw SurrealQL (migrations come with v0.38.0). `schema_mode=SchemaMode.SCHEMALESS` turns a `SCHEMAFULL` table back,
  and `permissions={}` writes `PERMISSIONS NONE`.
- Each call on a table with a change feed adds one `define_table` entry to it; `changes()` skips it, a raw
  `SHOW CHANGES` shows it.
- The call is idempotent: run it at start-up. A second call sends the same statements and changes nothing.
- `overwrite=False` writes `IF NOT EXISTS`: a missing table or field is defined, an existing one left as it is (the
  stored mode then decides `FLEXIBLE`).
- Before any DDL, with only the reads sent, `define_table()` raises `SurrealDbError` on a materialized view (an
  ordinary table statement would drop its query: declare `view_query`), on a relation table (`TYPE RELATION`, whose
  constraint would be dropped: derive the model from `BaseSurrealEdge`) and on a table or field definition the ORM
  cannot read back or carry (it could not keep the clauses the model does not declare).
- A refused statement raises `SurrealDbError` naming the table or the field, never the statement
  (`Can't apply field definition: 'age' on table 'person' -> …`); the statements before it stay applied: `tx=` makes
  the call atomic.

#### Views and edges

```python
from surrealdb import RecordID


class NoteCount(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_name="note_count", view_query="SELECT count() AS n FROM note GROUP ALL", permissions={"select": "FULL"}
    )

    id: RecordID | None = None
    n: int


print(await NoteCount.define_table())
# DEFINE TABLE OVERWRITE `note_count` AS (SELECT count() AS n FROM note GROUP ALL) PERMISSIONS FOR select FULL, FOR create, update, delete NONE;
```

```python
from surreal_orm_lite import BaseSurrealEdge, SurrealDbError


class Rates(BaseSurrealEdge):
    model_config = SurrealConfigDict(
        table_name="rates", relation_in="person", relation_out="note", schema_mode=SchemaMode.SCHEMAFULL
    )

    stars: int = 1


try:
    await Rates.define_relation()  # it defines no field, which this SCHEMAFULL table needs
except SurrealDbError as exc:
    print(type(exc).__name__)
# SurrealDbError
print(await Rates.define_table())
# DEFINE TABLE OVERWRITE `rates` TYPE RELATION IN `person` OUT `note` SCHEMAFULL;
# DEFINE FIELD OVERWRITE `stars` ON `rates` TYPE int DEFAULT 1;
edge = await ada.relate(Rates, note, stars=5)
print(edge.stars)
# 5
```

- A view model's `define_table()` applies `define_view()`, its guards included, and returns its statement; its
  `table_ddl()` is `[view_ddl()]`. No field is defined on a view: SurrealDB computes its records.
- An edge model's `define_table()` applies the relation statement `define_relation()` carries — its guards naming
  `define_table()` — then the edge's data fields, never `in` and `out`, which SurrealDB defines from `IN` and `OUT`;
  its `table_ddl()` is `relation_ddl()` then those fields.
- The declared clauses reach `view_ddl()` and `relation_ddl()`, so `define_view()` and `define_relation()` write them
  too; a model without the keys renders the same statements as before. A view model takes `permissions`, `changefeed`
  and `comment`: `table_type`, `schema_mode` and `flexible_fields` raise `TypeError` there.
- **Changed**: `define_view()` keeps the permissions, comment, change feed, `DROP` and `SCHEMAFULL` set by hand on a
  stored view unless the model declares them (it used to reset them), and when its statement writes permissions, a
  refusal that quotes the statement is reported without it.
- An edge model that declares `schema_mode=SchemaMode.SCHEMAFULL` and has data fields is applied with
  `define_table()`: `define_relation()` defines no field and raises before any request.

#### User models

`define_access()` ([section 19](#19-model-level-authentication-authenticatedusermixin)) and `define_table()` read the
same schema mode. A model with `table_type=TableType.USER` (the full ORM's key for an auth table) or
`schema_mode=SchemaMode.SCHEMAFULL` gets from `define_access()` a `SCHEMAFULL` table with one `DEFINE FIELD` per field
and the access's permissions, then the access: signup and signin work on SurrealDB 2.x and 3.x, `define_table()` is
optional, and any call order gives the same table. Every other model keeps v0.17.0's `SCHEMALESS` table. One that
declares no `table_type` gets no `TYPE` from `define_access()`, which SurrealDB stores as `TYPE ANY`, and `TYPE NORMAL`
from `define_table()`: calling both changes the table's type each time, so declare `table_type` to keep one. The schema
mode, fields and permissions are the same in any call order.

```python
from surreal_orm_lite import AuthenticatedUserMixin, TableType


class Member(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="member", table_type=TableType.USER, access_name="member_access")

    id: str | None = None
    email: str
    password: str


*table, access = Member.access_ddl()
print(*table, sep="\n")
# DEFINE TABLE OVERWRITE `member` TYPE NORMAL SCHEMAFULL PERMISSIONS FOR select, update WHERE id = $auth.id, FOR create, delete NONE;
# DEFINE FIELD OVERWRITE `email` ON `member` TYPE string;
# DEFINE FIELD OVERWRITE `password` ON `member` TYPE string;
```

- `define_access()` writes the access's permissions, as in v0.17.0; `define_table()` keeps them unless the model
  declares its own.
- A `TableType.USER` model with `extra="allow"` raises `TypeError` there (with `with_table=True`), since its table is
  `SCHEMAFULL`; a `server_fields` column without a constant default needs a database `DEFAULT` ("Field types" above).
- An auth model with a field SIGNUP writes that `Field(exclude=True)` keeps out of `model_dump()`
  (`password: str = Field(exclude=True)`) raises `TypeError` too, naming the fields, before any request and whatever
  its schema mode: `table_ddl()`, `define_table()`, `access_ddl()` and `define_access()` (with or without the table),
  `signup()`, `signin()`, `authenticate()` and `refresh()` refuse it, and so do `update()` and `upsert()`, with or
  without `tx=` and before any signal. `update()` and `upsert()` replace the record with what `model_dump()` writes,
  so on any table (`SCHEMALESS`, or defined by hand with `with_table=False`) they would drop the password hash, failing
  every later signin, or be refused by a required column; a `SCHEMAFULL` table would not define the field either, so
  SurrealDB 3.x would refuse every signup and 2.x drop the hash. Remove `exclude=True` and leave the field out where
  you serialise the model instead (`model_dump(exclude={"password"})`). **Changed**: such a model on a `SCHEMALESS`
  table or with `with_table=False` used to be accepted, and its first `update()` or `upsert()` dropped the hash; until
  v0.35.4, `authenticate()`, `refresh()`, `update()` and `upsert()` still accepted it, so a record signed up elsewhere
  (another client, an access defined by hand) and loaded with `authenticate()` or `get()` lost its hash the same way.
- Upgrading: a 0.33 `TableType.USER` model's table turns `SCHEMAFULL` at its next `define_access()`; records already
  there are checked at their next write.

#### What SurrealDB checks

- **Records already in the table**: SurrealDB checks a record at its next write, not when `define_table()` changes a
  kind or makes the table `SCHEMAFULL`. A record that no longer fits reads back unchanged; its next write is refused
  when a value has the wrong kind, and treated as below when it holds a field the table does not define.
- **A write naming a field the table does not define** — `merge()` with a key the model does not declare,
  `bulk_update()`, a raw query — is refused on SurrealDB 3.x
  (`Found field 'nope', but no such field exists for table 'note'`) and the field dropped on 2.x and the embedded
  engine, silently: declare the field on the model. The items of a list of objects are the exception ("SurrealDB 2.x
  and 3.x" below).
- **A node model over a table that holds edges**: `define_table()` refuses a relation table (`TYPE RELATION`), but not
  a `TYPE ANY` table holding edges, whose edges its `TYPE NORMAL` keeps while refusing every new one: declare
  `table_type=TableType.ANY` for a table that holds records and edges.

#### SurrealDB 2.x and 3.x

- On a `SCHEMAFULL` table, SurrealDB 2.x and the embedded engine empty the objects and arrays of an `any` column and of
  an untyped array or set, optional or not, silently (`{x: {y: 1}}` is stored `{}`); `FLEXIBLE` keeps them. 3.x keeps
  them without it, and refuses `FLEXIBLE` on such a column. So `define_table()` reads the line (`client.version()`,
  once, outside any transaction, and only when the table is `SCHEMAFULL` and a column holds `any` or is a list of
  sub-models as below) and writes `FLEXIBLE` on those columns before 3.x; `table_ddl()`, which needs no connection,
  writes the 3.x form.
- On a `SCHEMAFULL` table, SurrealDB 2.x and the embedded engine also drop the value of a `FLEXIBLE` column under a list
  of objects from every item — the `dict`, `Any` or untyped `list` of a sub-model in a `list[M]`, at any depth
  (`items.*.meta`) — unless the list is `FLEXIBLE` too, then refuse the record's next write (`Found NONE for field`).
  `define_table()` writes such a list `FLEXIBLE` before 3.x; the item's typed fields are still checked. 3.x keeps those
  values without it. On 2.x and the embedded engine, the items of such a list then also keep a key the model does not
  declare, written raw or by another client (the ORM never sends one), which they drop from a list that is not
  `FLEXIBLE`: a 2.x limitation, since `FLEXIBLE` there covers the whole item. 3.x keeps such a key, `FLEXIBLE` or not,
  in the items of a list of objects whose items define a field (every list of sub-models `define_table()` writes) and
  in an object inside them that defines one; it refuses it at the top level, in a sub-model outside such a list, and in
  the items of a list that defines no item field (an `array<object>` defined by hand without `v.*.n`). SurrealDB's
  [documentation of
  `FLEXIBLE`](https://surrealdb.com/docs/reference/query-language/statements/define/field#flexible-data-types) says
  every object of a `SCHEMAFULL` table, an item of a list included, accepts only the keys defined for it unless its
  field is `FLEXIBLE`: 3.x departs from it in a list whose items define a field. This is fixed in SurrealDB 3.4
  (nightly at the time of writing), which refuses such a key there too: do not rely on it.
- On SurrealDB 3.x a required untyped array refuses an object item unless its items are defined
  (`no such field exists`): `define_table()` writes the item definition `TYPE any` (`bag.*`) on every line.
- A write naming a field a `SCHEMAFULL` table does not define is refused on 3.x and dropped on 2.x and the embedded
  engine ("What SurrealDB checks" above), except in the items of a list of objects: on 3.x of a list whose items define
  a field (every list of sub-models `define_table()` writes), on 2.x and the embedded engine of a list `define_table()`
  writes `FLEXIBLE`. The 3.x exception is a gap fixed in SurrealDB 3.4 (nightly at the time of writing).
- `COMPUTED` fields are SurrealDB 3.x's: `define_table()` keeps a stored one `COMPUTED`, and writes a new computed
  field `VALUE` on both lines.
- The [2.x/3.x table](#orm-behaviour-surrealdb-2x-vs-3x) and the
  [embedded engine's](#orm-behaviour-the-embedded-engine) list these rows.

#### Transactions, connections and the cache

- `define_table()` takes `tx=`. Its reads (`INFO FOR DB`, `INFO FOR TABLE`, `client.version()`) run before anything is
  queued, on committed state, and its statements join the transaction: applied at once in an interactive one
  (WebSocket and SurrealDB 3.x), at commit in a buffered one (SurrealDB 2.x, HTTP, the embedded engine), where a
  refusal comes back as the transaction's own `SurrealDbError`. A block that raises applies nothing.
- A definition of the same table or field queued earlier in the block (`tx.add("DEFINE FIELD …")`) is not seen by
  `define_table()` and is replaced at commit by the statement it builds: define it before the block.
- The reads and the definitions are a check, then the DDL, not a lock: a clause another process changes between the
  two is overwritten with the value read.
- `define_table()` runs on the model's connection (its `connection` key, or the `using()` block's); a `tx=` of another
  connection raises `SurrealDbError` before any request.
- The query cache ([section 29](#29-query-cache-cache-querycache)): `define_table()` drops the model's cached reads,
  as every write does.
- The table's events ([section 32](#32-server-side-events-and-stored-functions-event-define_events-define_function))
  are kept: `define_events()` runs before or after. `define_computed_fields()` still writes `VALUE` alone and resets
  the field's other clauses: prefer `define_table()`.
- Defining needs a system user allowed to define: root, or a namespace or database `OWNER` or `EDITOR`. A session that
  may not read the definitions or define the table gets `SurrealDbAuthenticationError` (a record user, at the first
  read). Applying DDL fires no signal.

#### Security

Names — the table, every column and every nested segment — are written in backquotes. Kinds are built from Python
types, never from text, and literal kinds and `DEFAULT` values go through the ORM's closed SurrealQL encoder. The
declared clauses are checked when the class is defined: the comment is quoted (no NUL character), the change feed
matched in full, each permission rule `FULL`, `NONE` or one condition — literals terminated, brackets balanced, no
comment marker, no `;` outside brackets. A computed field's expression gets the same check before any request: a
comment marker would swallow the `ASSERT`, `PERMISSIONS` and `COMMENT` written after it, silently widening the
field's permissions. These checks catch mistakes; they are not an injection defence: rules and expressions are
trusted application code, never user input.

A clause the model does not declare is kept from the database — re-checked, and refused before any DDL when it fails
— so `define_table()`, `define_relation()` and `define_view()` never reset stored permissions to SurrealDB's
defaults; a declared `permissions` replaces them whole, operations left out being `NONE`. `define_access()` writes the
access's permissions, as in v0.17.0. A refusal names the table and the field, never the statement, which may carry
permission rules that hold secrets; `define_view()`'s refusal is redacted when its statement writes permissions.
Applying needs a privileged session: run `define_table()` at start-up or deployment, not on a record user's session.

#### Coming from the full SurrealDB-ORM

- **The same keys, `SchemaMode` and getters**: `schema_mode`, `changefeed`, `permissions`, `comment`,
  `flexible_fields`, `get_schema_mode()`, `get_changefeed()` and `get_permissions()`. `get_permissions()` returns the
  declared mapping, as the full ORM's; `get_changefeed()` returns the normalised text (`"1h INCLUDE ORIGINAL"`), equal
  to the declared string for a bare duration such as `"7d"`; `get_schema_mode()` may be `None` (no `schema_mode`, no
  `USER`/`HASH`).
- **Not `SCHEMAFULL` by default**: a model that declares no mode keeps the stored one, and a new table is
  `SCHEMALESS` (the full ORM makes every table `SCHEMAFULL` unless told otherwise).
- **Declared wins, the rest is kept from the database** (the full ORM resets every clause it does not render).
- **`define_table()` returns the statements joined with `"\n"`**, as the full ORM's, and also takes `overwrite=` and
  `tx=`; `table_ddl()` renders them as a list without a database.
- **One statement per call** (the full ORM sends one batch). A refused statement raises `SurrealDbError` naming the
  table or the field, never the statement, as every lite DDL applier does; the statements before it stay applied
  (`tx=` makes the whole call atomic). The full ORM raises `MigrationStatementError` (a `RuntimeError` from
  `surreal_orm.migrations`) with the SQL in its message.
- **`TYPE NORMAL` is written for `TableType.NORMAL` and for a model without `table_type`** (and for `USER`, `STREAM`
  and `HASH`). The full ORM writes no `TYPE` there, and SurrealDB then stores `TYPE NORMAL` for a `SCHEMAFULL` table
  (the full ORM's default) and `TYPE ANY` for a `SCHEMALESS` one. `TYPE NORMAL` refuses edges (`RELATE`) into the
  table: declare `TableType.ANY` for a table that holds both records and edges.
- **`FLEXIBLE` is written after `TYPE`**: the full ORM writes it before, which SurrealDB 3.x refuses ("FLEXIBLE must
  be specified after TYPE"). It is added automatically to the free-form object columns of a `SCHEMAFULL` table (the
  full ORM: only on request, `flexible_fields` or `json_schema_extra`), and on SurrealDB 2.x to the columns that hold
  `any`.
- **A view model's `define_table()` defines no field**; the full ORM also writes one `DEFINE FIELD` per model field on
  the view's table.
- **Kinds for `datetime`, `Decimal`, `UUID`, durations, geometries, enums, literals and sub-models** (the full ORM
  writes `any` or `object`); an item definition `TYPE any` for an untyped `list` (the full ORM writes `array` alone,
  whose object items SurrealDB 3.x refuses); `record` for `RecordID` (no `ForeignKey`).
- **No `DEFAULT` on a nullable field** (SurrealDB would apply it to an explicit `None`).
- **`changefeed` may carry `INCLUDE ORIGINAL`.**
- **`table_type`, `schema_mode` and `flexible_fields` are refused on a view model.**
- **`TableType.USER` makes the table `SCHEMAFULL` in `define_table()`**, as the full ORM's, and in `define_access()`
  (the lite's model-level auth), which then defines the model's fields too.
- **Upgrading from 0.33 or earlier**: the five keys were accepted and ignored; they are now checked at the `class`
  statement (a plain-string `schema_mode` included: use `SchemaMode.SCHEMAFULL`) and applied by `define_table()`,
  `define_relation()`, `define_view()` and `define_access()`. A view model with `table_type` raises `TypeError`;
  `define_view()` keeps a view's clauses set by hand; `access_ddl()`/`define_access()` of a `TableType.USER` model
  define a `SCHEMAFULL` table with its fields (and raise `TypeError` with `extra="allow"`), and write `TYPE NORMAL`
  (or `TYPE ANY`) for any model with `table_type`; generated models carry the keys
  ([section 31](#31-schema-introspection-databaseintrospector-generate_models_from_db)).

### 36. Full-text search (`search()`, `SearchScore`, `SearchHighlight`, `Analyzer`, `Index`)

Since v0.35.0 a queryset searches text with the full SurrealDB-ORM's API — `search()`, with `SearchScore` and
`SearchHighlight` in `annotate()` — plus `SearchOffsets`, over the full-text indexes a model declares with `Analyzer`
and `Index` and applies with `define_indexes()`. SurrealDB tokenises, matches, scores (BM25) and highlights on the
server; the ORM binds every search text and highlight tag as a query variable. The same code runs on SurrealDB 2.x,
3.x and the embedded engine, except where the [2.x/3.x table](#orm-behaviour-surrealdb-2x-vs-3x) says otherwise. The
examples assume a configured connection: they run as written, in order, on the embedded engine (`mem://`).

#### Declare and apply

An `Analyzer` is SurrealDB's `DEFINE ANALYZER`, with the full ORM's `DefineAnalyzer` arguments: a name, tokenizers
(`blank`, `camel`, `class`, `punct`), filters (`ascii`, `lowercase`, `uppercase`, `ngram(min,max)`,
`edgengram(min,max)`, `snowball(language)`, `mapper('path')`), and optionally `function=` and `comment=`. An `Index`
is `DEFINE INDEX`, with `CreateIndex`'s arguments: a plain index, a `UNIQUE` one (`unique=True`), or a full-text one
(`search_analyzer=`, an `Analyzer` or the name of an analyzer the database defines), which covers one field and takes
`bm25=` (`True`, or a `(k1, b)` pair) and `highlights=True`. Each argument is checked when the object is built. A
model lists its indexes in `SurrealConfigDict(indexes=[...])`, checked when the class is defined (a list or a tuple of
`Index`, distinct names, one full-text index per column, one `Analyzer` per analyzer name); `get_indexes()` returns
them in declaration order.

```python
from surreal_orm_lite import (
    Analyzer,
    BaseSurrealModel,
    Count,
    Index,
    SearchHighlight,
    SearchOffsets,
    SearchScore,
    Subquery,
    SurrealConfigDict,
    SurrealDbError,
)

ENGLISH = Analyzer("english", ["class", "blank"], ["lowercase", "ascii", "snowball(english)"])


class Article(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_name="article",
        indexes=[
            Index("article_title", "title", search_analyzer=ENGLISH, bm25=True, highlights=True),
            Index("article_body", "body", search_analyzer=ENGLISH),
            Index("article_slug", "slug", unique=True),
        ],
    )

    id: str | None = None
    title: str
    body: str = ""
    slug: str
    topic: str = ""


print(*Article.index_ddl(), sep="\n")
# DEFINE ANALYZER OVERWRITE `english` TOKENIZERS class,blank FILTERS lowercase,ascii,snowball(english);
# DEFINE INDEX OVERWRITE `article_title` ON `article` FIELDS `title` FULLTEXT ANALYZER `english` BM25 HIGHLIGHTS;
# DEFINE INDEX OVERWRITE `article_body` ON `article` FIELDS `body` FULLTEXT ANALYZER `english`;
# DEFINE INDEX OVERWRITE `article_slug` ON `article` FIELDS `slug` UNIQUE;
await Article.define_table()  # the table first
applied = await Article.define_indexes()
print(applied == Article.index_ddl(line=2))  # the embedded engine is a SurrealDB 2.x core: SEARCH
# True
print(await Article.define_indexes())  # nothing changed, nothing applied
# []
```

`define_indexes()` runs on the model's connection. It reads what the database holds (`INFO FOR DB`, `INFO FOR TABLE`)
and the line (`client.version()`, when a full-text index is declared), then applies only what is missing or changed,
one statement per request, and returns the statements applied: on an unchanged database it sends the reads alone and
returns `[]`, so it is safe at start-up. It writes the line's keyword: `FULLTEXT` on SurrealDB 3.x, `SEARCH` on 2.x and
the embedded engine, each of which refuses the other's. `overwrite=False` defines what is missing (`IF NOT EXISTS`) and
leaves a changed index as it is. `index_ddl()` renders the statements without a database, for SurrealDB 3.x, or for 2.x
with `index_ddl(line=2)` (a script for a 2.x server). Nothing is removed: `remove_index()` removes an index by name
(`Article.remove_index("article_body")` sends `REMOVE INDEX IF EXISTS`; with `missing_ok=False`, a missing index
raises `SurrealDbNotFoundError`). An analyzer an index names by `str` must exist, or `define_indexes()` raises
`SurrealDbError` before any DDL: `SurrealDBConnectionManager.define_analyzer()` defines one on the current connection
(redefining it only when it changed, and returning the statements applied), and `remove_analyzer()` removes one by
name, refusing it while an index uses it. `define_indexes()` and `define_analyzer()` drop every cached read
([section 29](#29-query-cache-cache-querycache)), `remove_index()` the model's.

Call `define_table()` (or `define_relation()` for an edge model) before `define_indexes()`: on a table that does not
exist yet, SurrealDB's `DEFINE INDEX` creates it `TYPE ANY SCHEMALESS PERMISSIONS NONE`, which `define_table()`
replaces but `define_table(overwrite=False)` leaves as it is.

#### Search, score, highlight

`search(**field_queries)` keeps the records whose fields match: each keyword names a field — a Python field name (an
aliased field searches its column) or a dotted path (`**{"meta.headline": "rust"}`) — and the text to match, which is
bound, never written into the statement, and is data whatever it holds. Each field gets the next match reference, in
call order and across calls (`search(title="a").search(body="b")`: references 0 and 1), which `SearchScore(ref)`,
`SearchHighlight(open_tag, close_tag, ref)` and `SearchOffsets(ref)` name in `annotate()`: the BM25 score, a `float`;
the text with each matched term between the tags, a `str` (a list of strings for an array field); where the terms sit,
SurrealDB's `{"<item>": [{"s": <start>, "e": <end>}]}`. `exec()`, `first()` and `get()` without an id set each value
as an attribute named after its alias, never written back (`model_dump()`, `save()` and `update()` leave it out); rows
that do not validate (a `select()` leaving out a required field) come back as dicts holding the aliases.
`order_by("-relevance")` sorts on an alias. An alias that would shadow a field, its column or an attribute of the
model raises `ValueError`, as does a reference no `search()` gave, before any request.

```python
corpus = [
    ("a1", "Quantum physics for beginners", "An introduction to quantum mechanics", "science"),
    ("a2", "Cooking with physics", "Heat and physics in the kitchen", "food"),
    ("a3", "Gardening in spring", "Plants and soil", "garden"),
    ("a4", "Quantum computing explained", "Qubits and gates", "science"),
    ("a5", "Baking bread at home", "Flour water salt", "food"),
    ("a6", "Sewing for beginners", "Needles and thread", "craft"),
]
for key, title, body, topic in corpus:
    await Article(id=key, title=title, body=body, slug=key, topic=topic).save()

articles = await (
    Article.objects()
    .search(title="physics")
    .annotate(relevance=SearchScore(0), snippet=SearchHighlight("<b>", "</b>", 0), spans=SearchOffsets(0))
    .order_by("-relevance")
    .exec()
)
for article in articles:
    print(article.id, round(article.relevance, 3), article.snippet, article.spans)
# a2 0.613 Cooking with <b>physics</b> {'0': [{'e': 20, 's': 13}]}
# a1 0.543 Quantum <b>physics</b> for beginners {'0': [{'e': 15, 's': 8}]}
```

`search()` applies wherever `filter()` does, AND-ed after `filter()`, `Q` and `nearby()`: `count()`, `exists()`, the
aggregations, `values()` groups, `bulk_update()`, `bulk_delete()`, `patch()`, and a `Subquery` of the queryset
([section 28](#28-subqueries-subquery)), whose references start again at 0. `get(<id>)` and `all()` ignore it, as they
ignore `filter()`.

```python
quantum_qubits = Article.objects().search(title="quantum", body="qubits")
both = await quantum_qubits.annotate(t=SearchScore(0), b=SearchScore(1)).exec()
print([(article.id, article.t > 0, article.b > 0) for article in both])
# [('a4', True, True)]
print(await Article.objects().search(title="quantum").count())
# 2
print(await Article.objects().filter(topic="food").search(title="physics").exists())
# True
print(await Article.objects().search(title="beginners").values("topic").annotate(n=Count()).exec())
# [{'n': 1, 'topic': 'craft'}, {'n': 1, 'topic': 'science'}]
beginners = Subquery(Article.objects().search(title="beginners"))
print(await Article.objects().filter(id__in=beginners).search(body="thread").count())
# 1
print(await Article.objects().search(body="kitchen").bulk_update(topic="cooking"))
# 1
```

#### What SurrealDB decides

- The analyzer decides what matches: its tokenizers split the text and its filters normalise each token — case,
  accents, stems (`snowball`), prefixes (`edgengram`) — and the search text goes through the same analyzer.
- Within a field, the terms of one text are AND-ed: `search(title="quantum physics")` keeps the records whose title
  holds both; an empty text matches nothing, and so does a field that holds no text (`None`, a number).
- BM25 scores depend on the line: SurrealDB 3.x gives a term found in more than half the documents no weight (a score
  of `0.0`) and keeps its statistics in step with the records, while 2.x and the embedded engine score such a term
  below zero, count an index's records again each time it is rebuilt, and keep counting deleted records. Compare
  scores within one query, and upgrade to SurrealDB 3.x where scores must not depend on the index's history.
- `SearchHighlight` and `SearchOffsets` need the index's `highlights=True` (`HIGHLIGHTS`): without it SurrealDB returns
  the text unhighlighted, and no offsets (`None`).
- A search on a field without a full-text index raises `SurrealDbError` naming the field. The server's own error,
  which on SurrealDB 3.x quotes the search text, is never shown.

```python
print([article.id for article in await Article.objects().search(title="quantum physics").exec()])
# ['a1']
print(await Article.objects().search(title="").count())
# 0
try:
    await Article.objects().search(slug="a1").exec()
except SurrealDbError as error:
    print(error)
# search(): no full-text index on field 'slug'. SurrealDB matches only a field with a FULLTEXT (3.x) or SEARCH (2.x) index: declare Index(..., search_analyzer=...) on the model and apply define_indexes().
```

#### Changing an analyzer

SurrealDB keeps the tokens of the records an index holds when their analyzer changes: a search tokenised the new way
misses them — the records written afterwards use the new analyzer — until the index is rebuilt. So when
`define_indexes()`, or `SurrealDBConnectionManager.define_analyzer()`, redefines an analyzer, it rebuilds every index of
the database that uses it (`REBUILD INDEX`, sorted by table then index; `define_indexes()` rebuilds the model's table's
last), and logs one warning naming them, an index it redefines at the same time included (logger
`surreal_orm_lite.search`). A rebuild re-reads the table: seconds per thousands of records on SurrealDB 2.x and the
embedded engine. An analyzer belongs to the database, not to a table: declare one `Analyzer` per name (a module
constant) and reference it from every index that uses it — two definitions under one name would redefine it in turn,
each time rebuilding its indexes. An unchanged index is never redefined, so start-up never rebuilds (and, on SurrealDB
2.x, never inflates its scores).

```python
PLAIN = Analyzer("plain", ["blank"], ["ascii"])


class Note(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="note", indexes=[Index("note_text", "text", search_analyzer=PLAIN)])

    id: str | None = None
    text: str


await Note.define_table()
await Note.define_indexes()
await Note(id="n1", text="Physics Rocks").save()
print(await Note.objects().search(text="physics").count())  # ascii keeps the case: "Physics" is not "physics"
# 0

LOWERED = Analyzer("plain", ["blank"], ["lowercase", "ascii"])  # the same analyzer, changed in a later release


class NoteV2(Note):
    model_config = SurrealConfigDict(table_name="note", indexes=[Index("note_text", "text", search_analyzer=LOWERED)])


print(*await NoteV2.define_indexes(), sep="\n")  # logs: Analyzer 'plain' redefined; 1 index(es) rebuilt: note.note_text
# DEFINE ANALYZER OVERWRITE `plain` TOKENIZERS blank FILTERS lowercase,ascii;
# REBUILD INDEX `note_text` ON `note`;
print(await NoteV2.objects().search(text="physics").count())
# 1
```

#### Transactions

- `define_indexes(tx=…)` reads first — committed state, outside the transaction — then its index statements join it
  ([section 11](#11-transactions-atomic-all-or-nothing)): applied at once in an interactive transaction (WebSocket,
  SurrealDB 3.x), at the commit in a buffered one (HTTP, SurrealDB 2.x, the embedded engine), where a refusal comes
  back as the transaction's own `SurrealDbError`. A block that raises applies nothing. `remove_index(tx=…)` joins it
  the same way.
- An index `remove_index(tx=…)` removed earlier in the block counts as missing, although committed state still holds
  it: `define_indexes(tx=…)` defines it again after its removal (`OVERWRITE`, or `IF NOT EXISTS` with
  `overwrite=False`), so the commit keeps it. Only an index the model declares comes back: another model's on the same
  table comes back with that model's `define_indexes(tx=…)`, and one no model declares stays removed. A raw
  `tx.add("REMOVE INDEX …")` is not tracked.
- The analyzers must be in place: SurrealDB builds an index with the analyzers committed before the transaction began.
  So `define_indexes(tx=…)` raises `SurrealDbError` before any DDL when an analyzer must be defined or redefined, and
  `define_analyzer(tx=…)` when a redefinition has indexes to rebuild (a new analyzer, or a redefinition no index uses,
  joins the transaction): call `define_analyzer()` without `tx=` first.
- The block must not have written to the model's table yet: SurrealDB servers do not index the records a transaction
  wrote before the index was defined, which stay out of it after the commit. It is a server defect
  ([surrealdb/surrealdb#7618](https://github.com/surrealdb/surrealdb/issues/7618) on 3.x, the same on 2.x); only the
  embedded engine indexes them. So `define_indexes(tx=…)` raises `SurrealDbError` before any DDL, naming the model and
  the table, when an index must be defined or redefined — one removed earlier in the block included — and the block
  has already written records to the model's table with the ORM (`save()`, `objects(tx=…).bulk_create()`, …). The
  refusal covers every kind of index, not only full-text ones: a search would miss those records, and a `UNIQUE` index
  missing them would not enforce uniqueness for them. The refusal does not fail the block. `define_table(tx=…)`,
  `define_relation(tx=…)` and a write to another table do not count, nor does a write the ORM refused before sending or
  queuing it: one refused for a materialized view, a snapshot read with `as_of()`, an auth model with a
  `Field(exclude=True)` field SIGNUP writes, a `tx=` of another connection, or one of its arguments (a missing id,
  invalid patch operations, an invalid `server_values`/`extra_vars` pair or either one on an edge's `save()`, a computed
  field or an edge endpoint among the fields written, an atomic helper's field, amount or values, a subquery value in
  `bulk_update()`, a snapshot in `bulk_create()`'s list); a raw `tx.add()` is not tracked. Until
  [EulogySnowfall/SurrealDB-ORM-lite#325](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/325) is fixed,
  some refusals made while the write is built or its values are encoded still count, although nothing was sent or
  queued: `upsert()` without an id, `save(tx=…)` without an id in a buffered transaction, a field name or an
  `extra_vars` key refused while the statement of `save(server_values=…)` or `merge(server_values=…)` is compiled (an
  invalid name, a key that collides with an internal binding), the argument checks of `relate()`, `remove_relation()`
  and `remove_all_relations()` (an invalid edge name, or an empty list of edges, counts as a write to every table, so
  every later `define_indexes(tx=…)` in the block is refused), a field name `bulk_update()` refuses, a filter refused
  while the statement of `objects(tx=…).bulk_update()`, `bulk_delete()` or `patch()` is compiled, an edge id of another
  table in an edge's `save()` or `bulk_create()`, and a value the ORM cannot send, in any write (a field, a filter, a
  patch operation, an atomic helper's value or amount, a relation's data, `extra_vars`): a `field_serializer` that
  raises, or one of the [values refused before sending](#values-refused-before-sending), such as a negative duration,
  an `int` outside the signed 64-bit range or a `Decimal` out of range. A write refused first for one of the arguments
  listed above, such as an `update()` or a `merge()` without an id, does not count.
- Prefer `define_indexes()` outside the block: on SurrealDB 2.x, a write to a table that held records, after its index
  in the same block, fails the commit with a retryable `SurrealDbConflictError` that a retry meets again; so does a
  block that removes an index of a table that held records and defines it again, where the index stands as before.
  Nothing of such a block is committed. On an empty table both commit, as on 3.x. On SurrealDB 3.x, a block that
  defines an index and then removes it fails the commit the same way, whatever the table holds; 2.x and the embedded
  engine commit it when the index was not stored before the block. An index stored before the block, redefined by
  `define_indexes(tx=…)` and then removed, fails the commit the same way on 2.x too when the table held records, where
  the index stands as before; the embedded engine commits it.
- `remove_analyzer(tx=…)` (on SurrealDB 2.x and the embedded engine; 3.x checks in the transaction itself) and
  `define_analyzer(tx=…)` read committed state to find the indexes that use the analyzer. An index `remove_index(tx=…)`
  removed earlier in the block no longer counts, although committed state still holds it: once the block has removed the
  last index that uses an analyzer, it can remove the analyzer, or redefine it without `REBUILD INDEX`, on every engine.
  An index removed then defined again in the block, or removed with a raw `tx.add("REMOVE INDEX …")`, still counts
  there.
- An index `define_indexes(tx=…)` defined earlier in the block counts too, with the analyzer the block gave it, although
  committed state does not hold it yet, or holds it with another analyzer: `remove_analyzer(tx=…)` and
  `define_analyzer(tx=…)` of that analyzer raise `SurrealDbError` before any DDL, naming the index, on every engine —
  SurrealDB 3.x included, whose own refusal of the removal used to fail the block. The refusal does not fail the block.
  An index defined then removed in the block no longer counts, and one the block defined again with another analyzer
  counts for that analyzer only. An index defined with `IF NOT EXISTS` (`define_indexes(overwrite=False, tx=…)`) keeps the
  block's earlier definition, and counts with the analyzer that one gave it. A raw `tx.add("DEFINE INDEX …")` is not
  tracked.
- Once the block has removed or redefined an analyzer, an index that uses it can no longer be defined in the block,
  whether the model declares the analyzer as an `Analyzer` or names it by `str`: `define_indexes(tx=…)` raises
  `SurrealDbError` before any DDL. A removed analyzer counts as missing, although committed state still holds it — the
  index would stand without it after the commit, and every write to the table would be refused. A redefined one would
  not be followed: SurrealDB would build the index with the committed definition. The refusal does not fail the block.
  To migrate an index to a new analyzer, call `define_analyzer()` without `tx=` first. `define_analyzer(tx=…)` after
  `remove_analyzer(tx=…)` of the same name defines the analyzer again, after its removal.
- A search in an interactive transaction sees the transaction's writes; in a buffered one, a read raises, as every
  read does there.
- In a transaction, a search on a field without a full-text index raises the same `SurrealDbError`, which the
  transaction records: the block's commit then fails with it as its cause, and neither error quotes the search text.

#### Live queries

- `live()` and `watch()` refuse `search()` — and a search annotation — before any request (`SurrealDbError`):
  SurrealDB accepts a live query whose condition holds a full-text match, but never notifies it
  ([section 21](#21-live-queries-real-time-notifications)).
- Search in a subquery instead:
  `Article.objects().filter(id__in=Subquery(Article.objects().search(title="physics"))).live()` notifies the records
  created or updated to match — not a deletion, nor an update that makes a record stop matching.
- Before a live query whose subquery searches, the ORM checks that each searched field has a full-text index and
  raises `SurrealDbError` naming the field otherwise, before the live query starts: without the index, SurrealDB 3.x
  would accept the live query and never notify. Do not remove that index while the live query runs.

#### Not supported

- `as_of()` ([section 27](#27-time-travel-as_of)): `search()` with `as_of()` raises `SurrealDbError` before any
  request, in either order and in a subquery under it. The index holds the records' current text, so a time-travel
  read would select records by their text now.
- `changes()` ([section 22](#22-change-feeds-changes)): a change feed refuses `search()` and the search annotations
  (`SurrealDbError`), and a `Subquery` refuses the search annotations.
- Indexes on materialized views ([section 33](#33-materialized-views-view_query-define_view)), refused with
  `ValueError` when the class is defined. A materialized view model cannot declare `indexes=`: SurrealDB 3.x does not
  index the records a view gains after the index is defined, and `define_view()` drops the indexes of the view's table.
- `@AND@`/`@OR@`: SurrealDB 3.x's boolean match operators are not exposed. The terms of one text, and the fields of
  `search()`, are AND-ed; there is no OR of two matches.

#### Coming from the full SurrealDB-ORM

- **Declared on the model**: `Analyzer` and `Index` take the argument names of the full ORM's `DefineAnalyzer` and
  `CreateIndex`, a model lists its indexes in `SurrealConfigDict(indexes=[...])` and `define_indexes()` applies them;
  the full ORM declares analyzers and indexes in migrations, which this library adds in v0.38.0.
- **`search()` applies wherever `filter()` applies**: `count()`, the aggregations, `values()` groups, `exists()`,
  `bulk_update()`, `bulk_delete()`, `patch()` and a `Subquery`; the full ORM applies it to `exec()` only.
- **Refused where SurrealDB cannot honour it**: `search()` with `live()`/`watch()`, `as_of()` or `changes()` raises
  before any request; the full ORM's `live()` ignores it.
- **The highlight tags are bound query variables**, and `SearchScore`, `SearchHighlight` and `SearchOffsets` have no
  public `to_surql()` (the full ORM writes the tags into the statement, escaping quotes only).
- **`SearchOffsets` is an addition** of this library: SurrealDB's `search::offsets()`, where the matched terms sit in
  the field.
- **A field without a full-text index raises `SurrealDbError` naming the field**, instead of the server's error,
  which on SurrealDB 3.x quotes the search text.
- **Checked before any request**: the text to match must be a `str`, every match reference an annotation names must
  come from `search()`, and `Index`/`Analyzer` arguments are checked at construction, more strictly than the server
  where its answer would be silently wrong (BM25 parameters out of range, an n-gram whose minimum exceeds its maximum,
  `UNIQUE` on a full-text index).
- **The index keyword follows the server line**: `FULLTEXT` on SurrealDB 3.x, `SEARCH` on 2.x and the embedded engine;
  the full ORM writes `FULLTEXT` only, which 2.x refuses.
- **A `COMMENT` is escaped with a backslash**; the full ORM's doubled quote is a parse error on both lines.
- **`Index` has no `concurrently` argument**: it covers plain, `UNIQUE`, full-text and, since v0.36.0, vector
  indexes (section 37).
- **`filter(x__match=...)` stays a regular expression**: this library's `match` lookup, not SurrealDB's `@@`;
  `search()` is the full-text path.
- **`hybrid_search()` fuses `search()`'s ranking with `similar_to()`'s** (section 38, v0.37.0).

### 37. Vector search (`similar_to()`, `VectorField`, `Index(hnsw=True)`)

Since v0.36.0 a queryset finds the records whose vector is nearest to a query vector with the full SurrealDB-ORM's
API — `similar_to()`, each record's distance set as `_knn_distance` — over the vector fields a model declares with
`VectorField` and the HNSW indexes it declares with `Index(hnsw=True)` and applies with `define_indexes()`. SurrealDB
searches the index and computes the distances on the server; the ORM binds the query vector as a query variable. The
same code runs on SurrealDB 2.x, 3.x and the embedded engine, except where the
[2.x/3.x table](#orm-behaviour-surrealdb-2x-vs-3x) says otherwise. The examples assume a configured connection: they
run as written, in order, on the embedded engine (`mem://`).

#### Declare and apply

`VectorField[d]` is the full ORM's vector field: a list of `d` numbers, optional and `None` by default, which
`define_table()` writes `option<array<float, d>>`; `VectorField[d, "F64"]` also names its storage type (`F64`, `F32` —
the default —, `I64`, `I32` or `I16`). An `Index` with `hnsw=True` is a vector index, with `CreateIndex`'s arguments:
`dimension=` (required: the vectors' length, 1 to 65535), `dist=` (`EUCLIDEAN` by default, `COSINE`, `MANHATTAN`,
`CHEBYSHEV` or `HAMMING`, in any case), `vector_type=`, `efc=` (the build's candidate list, 150 by default) and `m=`
(the links per node, 2 to 127, 12 by default). It covers one field, and the model lists it in
`SurrealConfigDict(indexes=[...])` next to the full-text indexes of
[section 36](#36-full-text-search-search-searchscore-searchhighlight-analyzer-index). Its attributes hold what was
declared (`None` for an option not given). When the class is defined, two vector indexes on one column are refused,
and so is a vector index whose dimension or storage type differs from its field's `VectorField`. A field annotated
with a name defined after the class (a forward reference) is checked once the name resolves, with the same error: at
`model_rebuild()`, at the model's first instantiation, validation or JSON schema, and before `table_ddl()`,
`define_table()`, `index_ddl()`, `define_indexes()`, `get_indexes()` or a queryset (`similar_to()` included) reads the
model. Once refused, each of these raises it again.

```python
from surreal_orm_lite import (
    Analyzer,
    BaseSurrealModel,
    Index,
    PointField,
    SearchHighlight,
    SearchScore,
    Subquery,
    SurrealConfigDict,
    SurrealDbError,
    VectorField,
)

PLAIN = Analyzer("plain", ["blank"], ["lowercase"])


class Document(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_name="document",
        indexes=[
            Index("document_embedding", "embedding", hnsw=True, dimension=3, dist="cosine"),
            Index("document_title", "title", search_analyzer=PLAIN, highlights=True),
        ],
    )

    id: str | None = None
    title: str
    category: str = ""
    featured: bool = False
    location: PointField = None
    embedding: VectorField[3]  # three numbers keep the examples short; an embedding model gives hundreds


print(*Document.index_ddl(), sep="\n")
# DEFINE ANALYZER OVERWRITE `plain` TOKENIZERS blank FILTERS lowercase;
# DEFINE INDEX OVERWRITE `document_embedding` ON `document` FIELDS `embedding` HNSW DIMENSION 3 DIST COSINE TYPE F32 EFC 150 M 12;
# DEFINE INDEX OVERWRITE `document_title` ON `document` FIELDS `title` FULLTEXT ANALYZER `plain` HIGHLIGHTS;
await Document.define_table()  # the table first, its `embedding` column an option<array<float, 3>>
applied = await Document.define_indexes()
print(applied == Document.index_ddl(line=2))  # the embedded engine is a SurrealDB 2.x core: SEARCH
# True
print(await Document.define_indexes())  # nothing changed, nothing applied: the vector index is not rebuilt
# []
```

The ORM writes every HNSW option — `DIST`, `TYPE`, `EFC` and `M`, SurrealDB's defaults when not given — so every
engine stores the same index: the embedded engine would otherwise store `F64` vectors where servers store `F32`.
`define_indexes()` compares the declared vector index with the stored one and redefines it only when an option, the
field or the comment changed — a rebuild, which reads every record of the table — and `index_ddl()` renders the same
`HNSW` statement for SurrealDB 2.x and 3.x.

A vector field's value is checked on every write and on every read: `None`, or a `list` or `tuple` of exactly `d`
real numbers (`int`, `float`, a `Decimal`; not a `bool` nor a `str`), each finite, kept as a `list[float]`; anything
else raises Pydantic's `ValidationError`. A stored vector that fails the check — another length, a `NaN` — makes
`exec()` return its row as a dict, as for any row that does not validate.

```python
try:
    Document(title="Too short", embedding=[1.0, 0.0])
except ValueError as error:  # pydantic.ValidationError
    print(error.errors()[0]["msg"])
# Value error, expected 3 numbers, got 2
```

To move a table to another dimension — a new embedding model, 384 numbers to 768 —, first declare the field
`list[float] | None` without its vector index, apply `define_table()` (the column then takes any length) and remove
the index (`remove_index()`), which refuses a vector of another length: reads keep returning instances while records
of both lengths exist. Once every record holds the new length, declare `VectorField[768]` and an index with
`dimension=768`, then apply `define_table()` and `define_indexes()`.

#### Type checkers

Type checkers do not understand `VectorField[1536]` (mypy: `"VectorField" expects no type arguments`), as in the full
ORM: in type-checked code, write `embedding: Annotated[list[float] | None, Vector(1536)] = None`, which the ORM reads
the same way. `Vector(dimension, vector_type="F32")` is the marker `VectorField` builds, so both spellings are one
field: the same checks, the same column and the same JSON schema. Without the Pydantic mypy plugin, mypy only knows a
default written in the class body: write `= None`, or `= Field(default=None, alias="…")` for an aliased field.
`get_vector_info()` and `is_vector_field()`, from `surreal_orm_lite.vector`, read either spelling.

```python
from typing import Annotated

from pydantic import Field

from surreal_orm_lite import Vector
from surreal_orm_lite.vector import get_vector_info, is_vector_field


class Passage(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="passage")

    id: str | None = None
    text: str
    embedding: Annotated[list[float] | None, Vector(1536)] = None
    summary: Annotated[list[float] | None, Vector(384, "F64")] = Field(default=None, alias="summary_vec")


passage = Passage(text="Typed vectors")  # type checkers accept it: both vectors default to None
print(passage.embedding, passage.summary)
# None None
print(get_vector_info(Passage.model_fields["embedding"]), get_vector_info(Passage.model_fields["summary"]))
# (1536, 'F32') (384, 'F64')
print(get_vector_info(Document.model_fields["embedding"]), is_vector_field(Passage.model_fields["text"]))
# (3, 'F32') False
```

`VectorField[d]` is already optional, so it needs no `| None`. Written `VectorField[d] | None` or
`Optional[VectorField[d]]`, its `None` default sits inside a union member, where Pydantic ignores it with an
`UnsupportedFieldAttributeWarning`: write `= None`, or the field is required. The ORM reads these spellings, and
`Annotated[list[float], Vector(d)] | None`, like the plain ones (a `Literal[None]` member counts as `None`): the same
column, the same checks of the vector index when the class is defined, the same dimension for `similar_to()`, the same
answer from `get_vector_info()`. A union that holds a vector next to a type other than `None`, or two different
`Vector` markers, has no single dimension: the class statement refuses it, naming the field, and `get_vector_info()`
and `is_vector_field()` raise `ValueError` for such a field or annotation.

#### Find the nearest

`similar_to(field, vector, limit=10, *, ef=None, dist=None)` keeps the `limit` records whose `field` vector is nearest
to `vector`. The field is a Python field name (an aliased field searches its column) or a dotted path; the vector is
a `list` or `tuple` of numbers, of the field's dimension when the model declares it (a NumPy array: `.tolist()`),
checked before any request and bound, never written into the statement. A second `similar_to()` replaces the first.
`exec()`, `first()` and `get()` without an id set each record's distance as `_knn_distance` — a number, in the
index's metric or `dist=`'s —, never written back (`model_dump()`, `save()` and `update()` leave it out); rows that
do not validate (a `select()` leaving out a required field) come back as dicts holding it.
`order_by("-_knn_distance")` or any field sorts them otherwise.

`similar_to(field, vector, limit)` keeps the `limit` nearest records, `limit` and `ef` going up to 10 000; `limit()`
and `offset()` then page through them, and without `order_by()` they come nearest first.

```python
corpus = [
    ("d1", "Cats and dogs", "pets", [1.0, 0.0, 0.0], (0.0, 0.0)),
    ("d2", "Cats at home", "pets", [0.9, 0.1, 0.0], (50.0, 50.0)),
    ("d3", "Rust programming", "tech", [0.0, 1.0, 0.0], (0.01, 0.01)),
    ("d4", "Python programming", "tech", [0.1, 0.9, 0.0], (0.02, 0.0)),
    ("d5", "Gardening", "home", [0.0, 0.0, 1.0], (10.0, 10.0)),
    ("d6", "Cats and code", "news", [0.5, 0.5, 0.0], (0.0, 0.02)),
]
for key, title, category, embedding, location in corpus:
    await Document(id=key, title=title, category=category, embedding=embedding, location=location).save()

query = [1.0, 0.0, 0.0]
for document in await Document.objects().similar_to("embedding", query, 3).exec():
    print(document.id, round(document._knn_distance, 4))
# d1 0.0
# d2 0.0061
# d6 0.2929
nearest = await Document.objects().similar_to("embedding", query).first()
print(nearest.id, "_knn_distance" in nearest.model_dump())
# d1 False
page = Document.objects().similar_to("embedding", query, 4).offset(2).limit(2)
print([document.id for document in await page.exec()])
# ['d6', 'd4']
```

`ef` (100 by default) sizes the HNSW search: a larger value finds the true nearest records more often, at a cost. The
ORM writes it raised to `limit`, so without other conditions every line returns `limit` records when the table holds
them.

`similar_to()` reads the field's HNSW index: on a field without one it finds no record. Pass `dist=` for an exact scan
that needs no index.

```python
print([document.id for document in await Document.objects().similar_to("embedding", query, 3, ef=40).exec()])
# ['d1', 'd2', 'd6']
exact = await Document.objects().similar_to("embedding", query, 2, dist="euclidean").exec()
print([(document.id, round(document._knn_distance, 4)) for document in exact])  # double precision
# [('d1', 0.0), ('d2', 0.1414)]
```

#### With other conditions

`similar_to()` applies wherever `filter()` does, AND-ed with `filter()`, `Q`, `nearby()`, `search()` and a
`Subquery` filter: `exec()`, `first()`, `get()` without an id, `count()`, `exists()`, the aggregations, `values()`
groups, `bulk_update()`, `bulk_delete()`, `patch()`, and a `Subquery` of the queryset
([section 28](#28-subqueries-subquery)). `get(<id>)` and `all()` ignore it, as they ignore `filter()`. The bulk
writes act on the `limit` nearest records, whose ids they select first, and return their number; their `limit()`,
`offset()` and `order_by()` are ignored, as for every bulk write. With `search()`, the records matching the text are
ranked by the vector.

Other conditions select the records first, then `similar_to()` keeps the nearest among them: SurrealDB 3.x applies
them during the index search. SurrealDB 2.x and the embedded engine answer too few records, or none, next to many
conditions (one on an indexed field among them), so there the ORM selects the records matching them in a `LET` first,
which is slower: upgrade to SurrealDB 3.x for large tables.

```python
tech = Document.objects().filter(category="tech").similar_to("embedding", query, 2)
print([document.id for document in await tech.exec()])
# ['d4', 'd3']
around = Document.objects().nearby("location", (0.0, 0.0), 3000).similar_to("embedding", query, 2)
print([document.id for document in await around.exec()])
# ['d1', 'd6']
cats = Document.objects().search(title="cats").similar_to("embedding", [0.0, 1.0, 0.0], 2)
print([document.id for document in await cats.exec()])  # the matches, ranked by the vector
# ['d6', 'd2']
print(await Document.objects().similar_to("embedding", query, 3).count())
# 3
print(await Document.objects().filter(category="pets").similar_to("embedding", query, 1).bulk_update(featured=True))
# 1
print([document.id for document in await Document.objects().filter(featured=True).exec()])
# ['d1']
nearest_three = Subquery(Document.objects().similar_to("embedding", query, 3))
print(await Document.objects().filter(id__in=nearest_three, category="pets").count())
# 2
not_pets = Subquery(Document.objects().filter(category__in=["tech", "news"]))
in_subquery = Document.objects().filter(id__in=not_pets).similar_to("embedding", query, 2)
print([document.id for document in await in_subquery.exec()])
# ['d6', 'd4']
```

On SurrealDB 3.x, search annotations (`SearchScore`, `SearchHighlight`, `SearchOffsets`) give each of the nearest
matches the score, highlight and offsets that the same `search()` gives it without `similar_to()`. On SurrealDB 2.x
and the embedded engine the ORM refuses them before the statement is sent, since there it selects the matches first
and the vector search holds no match to score, highlight or locate; `count()`, `exists()` and the aggregations, which
return no annotation, still answer. The examples run on the embedded engine, which is why this one prints the
refusal.

```python
programming = Document.objects().search(title="programming").similar_to("embedding", [0.0, 1.0, 0.0], 2)
annotated = programming.annotate(score=SearchScore(0), snippet=SearchHighlight("<b>", "</b>", 0))
try:
    for document in await annotated.exec():
        print(document.title, document.snippet, round(document.score, 4), round(document._knn_distance, 4))
except SurrealDbError as exc:  # the embedded engine is a 2.x core, like a SurrealDB 2.x server
    print(exc)
# similar_to() cannot be combined with SearchScore on SurrealDB 2.x or the embedded engine: there the ORM selects the full-text matches first, so the vector search holds no match to score, highlight or locate. Keep search() as a condition, or use SurrealDB 3.x.
print(await annotated.count(), await annotated.exists())
# 2 True
```

#### What SurrealDB decides

- HNSW is approximate: with a condition that keeps few records, a search can return fewer than `limit` records,
  depending on how the index was built. SurrealDB 2.x and the embedded engine do so more often, even with a condition
  that keeps a tenth of the table. Pass `dist=` for an exact scan.
- Distances are computed in the index's `vector_type` (`F32` by default: single precision), so they can differ from
  Python's in the last digits; `dist=` computes in double precision.
- SurrealDB 2.x's and the embedded engine's exact scan (`dist=`) picks among records at an equal distance at the
  `limit`-th place in no fixed order, so there the ORM ranks it by distance, then id: identical calls return the same
  records in the same order, the tied ones by id, and a page, a `Subquery` and the bulk writes take the same records.
  SurrealDB 3.x's own scan gives one answer.
- `COSINE` has no distance for a zero vector: `similar_to()` refuses a zero query vector when the distance is
  `COSINE` — `dist="cosine"`, or the model's `COSINE` index on the field —, and a record holding a zero vector gets a
  `nan` distance.
- A vector index refuses a record whose vector has another length. A query vector of another length is refused
  before any request when the model declares the dimension (its `VectorField` or its vector index); otherwise
  SurrealDB refuses it, and the ORM raises `SurrealDbError` naming the field and both lengths (a bulk write in a
  transaction relays SurrealDB's error).

```python
try:
    Document.objects().similar_to("embedding", [0.0, 0.0, 0.0])
except ValueError as error:
    print(error)
# similar_to(): a zero vector has no COSINE distance (SurrealDB answers NaN); use another vector or distance.
```

#### Not supported

- Search annotations with `similar_to()` on SurrealDB 2.x and the embedded engine, for the reads that return records
  (`exec()`, `first()`, `get()` without an id): `SurrealDbError` before the statement is sent. `search()` as a
  condition stays available there, and `count()`, `exists()` and the aggregations answer. `hybrid_search()` gives each
  record found by both searches its BM25 score and its distance, on every line.
- `as_of()` ([section 27](#27-time-travel-as_of)): `similar_to()` with `as_of()` raises `SurrealDbError` before any
  request, in either order and in a subquery under it. The index holds the records' current vectors, so a time-travel
  read would pick its neighbours by their vectors now.
- `live()`/`watch()` ([section 21](#21-live-queries-real-time-notifications)) refuse `similar_to()`, and a `Subquery`
  holding it, before any request: SurrealDB accepts a live query whose condition holds the vector search but never
  notifies it. Read the nearest records with `similar_to()` and watch them by id.
- `changes()` ([section 22](#22-change-feeds-changes)): a change feed refuses `similar_to()` (`SurrealDbError`).
- `MTREE` indexes (SurrealDB 3.x refuses them; one stored under a declared index's name is redefined `HNSW`), the
  `MINKOWSKI`, `JACCARD` and `PEARSON` distances, `F16` vectors and `DISKANN` indexes, and `concurrently`.

#### Coming from the full SurrealDB-ORM

- **`similar_to()` keeps the full ORM's signature and `_knn_distance`**:
  `similar_to(field, vector, limit=10, *, ef=None)`, the distance set on each instance as `_knn_distance`, nearest
  first unless `order_by()` is given.
- **`dist=` is an addition** of this library: an exact, brute-force search that needs no index (SurrealDB's
  `<|K,DISTANCE|>`).
- **`similar_to()` applies wherever `filter()` applies**: `count()`, the aggregations, `values()` groups, `exists()`,
  `bulk_update()`, `bulk_delete()`, `patch()` and a `Subquery`; the full ORM applies it to `exec()` only. The bulk
  writes act on the ids of the `limit` nearest records, selected first.
- **Refused where SurrealDB cannot honour it**: `as_of()`, `live()`/`watch()` or `changes()` with `similar_to()`,
  before any request, and search annotations with `similar_to()` on SurrealDB 2.x and the embedded engine, before the
  statement is sent; the full ORM sends them.
- **Checked before any request**: the vector's numbers, its length against the field's `VectorField` or index,
  `limit` and `ef` (1 to 10 000; the full ORM sends any value), and the `Index` vector arguments.
- **`ef` is raised to `limit`** on every line; the full ORM writes it as given, and SurrealDB 2.x then returns at most
  `ef` records.
- **Other conditions are selected first on SurrealDB 2.x** (a `LET`), since 2.x answers too few or no records next to
  many of them, one on an indexed field among them; the full ORM sends one statement.
- **Every HNSW option is written**, and `define_indexes()` compares before it applies: an unchanged vector index is
  never rebuilt.
- **`VectorField` checks its values**: their length and finite numbers, on reads too; the full ORM's accepts any list
  of floats. `Vector(d)` is its typed form, which type checkers accept.
- **No `MTREE`, `MINKOWSKI`, `JACCARD` or `PEARSON`, and no `concurrently` yet**; the full ORM writes `MINKOWSKI`
  without its order, which SurrealDB refuses.
- **Vector functions in `annotate()`** (`vector::similarity::cosine()`…) are not available yet; `hybrid_search()`
  (section 38) fuses this ranking with `search()`'s.
- **`get_vector_info()` and `is_vector_field()` are imported from `surreal_orm_lite.vector`**: the full ORM has them
  in `surreal_orm.fields`, a module this library does not have.
- **`generate_models_from_db()` does not write `VectorField` yet**, while the full ORM's generator writes it for a
  field with an HNSW index.

### 38. Hybrid search (`hybrid_search()`, Reciprocal Rank Fusion)

Since v0.37.0 a queryset fuses two rankings of its records with the full SurrealDB-ORM's `hybrid_search()`: the
records nearest to a query vector, as `similar_to()` finds them
([section 37](#37-vector-search-similar_to-vectorfield-indexhnswtrue)), and the best matches of a text, as `search()`
ranks them by BM25 score ([section 36](#36-full-text-search-search-searchscore-searchhighlight-analyzer-index)), are
fused into one ranking by Reciprocal Rank Fusion. SurrealDB 3.x fuses the two lists with its `search::rrf()`;
SurrealDB 2.x and the embedded engine have no such function, so the ORM fuses them with the same formula: the same
two lists give the same records, scores and order on every line. Each record comes back as a dict, as in the full
ORM. The examples assume a configured connection: they run as written, in order, on the embedded engine (`mem://`).

#### Search

```python
from surreal_orm_lite import Analyzer, BaseSurrealModel, Index, SurrealConfigDict, SurrealDbError, VectorField

WORDS = Analyzer("words", ["blank"], ["lowercase"])


class Passage(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_name="passage",
        indexes=[
            Index("passage_embedding", "embedding", hnsw=True, dimension=3, dist="cosine"),
            Index("passage_title", "title", search_analyzer=WORDS),
        ],
    )

    id: str | None = None
    title: str
    topic: str = ""
    embedding: VectorField[3]


await Passage.define_table()
await Passage.define_indexes()
for key, title, topic, embedding in [
    ("p1", "Cats and dogs", "pets", [1.0, 0.0, 0.0]),
    ("p2", "Cats at home", "pets", [0.9, 0.1, 0.0]),
    ("p3", "Rust programming", "tech", [0.0, 1.0, 0.0]),
    ("p4", "Python programming", "tech", [0.1, 0.9, 0.0]),
    ("p5", "Gardening", "home", [0.0, 0.0, 1.0]),
    ("p6", "Cats and code", "news", [0.5, 0.5, 0.0]),
    ("p7", "Rust and Python programming", "tech", [0.2, 0.8, 0.0]),
    ("p8", "Cooking", "home", [0.0, 0.2, 0.8]),
]:
    await Passage(id=key, title=title, topic=topic, embedding=embedding).save()

results = await Passage.objects().hybrid_search(
    vector_field="embedding", vector=[1.0, 0.0, 0.0], vector_limit=3,
    text_field="title", text_query="programming", text_limit=3,
)
for row in results:
    print(row["id"], f"{row['_rrf_score']:.4f}", row["_knn_distance"] is not None, row["_search_score"] is not None)
# passage:p1 0.0164 True False
# passage:p3 0.0164 False True
# passage:p2 0.0161 True False
# passage:p4 0.0161 False True
# passage:p6 0.0159 True False
# passage:p7 0.0159 False True
```

Each record gets 1 / (`rrf_k` + rank) from each list it is in, its rank counted from 1 (`rrf_k` is 60 by default):
`p1`, nearest, and `p3`, the best match, both score 1/61. Records with equal scores keep the vector list's order
first, then the full-text list's for the records it alone found, on every line — SurrealDB's `search::rrf()` alone
returns them in no fixed order. `_knn_distance` is `None` for a record only the full-text search found,
`_search_score` for one only the vector search found.

#### With the queryset

```python
tech = await Passage.objects().filter(topic="tech").limit(2).hybrid_search(
    vector_field="embedding", vector=[0.0, 1.0, 0.0], vector_limit=4,
    text_field="title", text_query="programming", text_limit=4,
)
for row in tech:
    print(row["id"], f"{row['_rrf_score']:.4f}")
# passage:p3 0.0328
# passage:p4 0.0323
page = await Passage.objects().offset(1).limit(2).hybrid_search(
    vector_field="embedding", vector=[1.0, 0.0, 0.0], vector_limit=3,
    text_field="title", text_query="programming", text_limit=3,
)
print([str(row["id"]) for row in page])
# ['passage:p3', 'passage:p2']
```

The queryset's conditions select the records of both lists. `select()` and `fetch()` apply to both, `id` always read;
`limit()` and `offset()` page through the fused records, of which there are at most `vector_limit + text_limit`.
`cache()`, `objects(tx=)` and named connections apply as for `exec()`, and `dist=` scans exactly, as in section 37.

#### What SurrealDB decides

```python
try:
    await Passage.objects().hybrid_search(
        vector_field="embedding", vector=[1.0, 0.0, 0.0], text_field="topic", text_query="tech",
    )
except SurrealDbError as error:
    print(error)
# hybrid_search(): no full-text index on field 'topic'. SurrealDB matches only a field with a FULLTEXT (3.x) or SEARCH (2.x) index: declare Index(..., search_analyzer=...) on the model and apply define_indexes().
```

- Each list is SurrealDB's own: the vector list as `similar_to()` finds it — HNSW is approximate (section 37) — and
  the full-text list as `search()` ranks it, with section 36's BM25 scores. Those depend on the line: a term found in
  more than half the documents scores `0.0` on SurrealDB 3.x and below zero on 2.x and the embedded engine
  ([2.x/3.x table](#orm-behaviour-surrealdb-2x-vs-3x)), so the full-text list, and the fused ranking with it, can
  differ between lines.
- **An exact scan (`dist=`) returns the same vector list on every call**, on every line. SurrealDB 2.x's and the
  embedded engine's own scan picks among records at an equal distance at the `vector_limit`-th place in no fixed order,
  so there the ORM ranks it by distance, then id, as for `similar_to()` (section 37): the tied records are kept by id,
  and a page read with `offset()` and `limit()` never skips nor repeats one. The HNSW index (no `dist=`) returns the
  same list on every call too.
- Each search needs its index. Without a full-text index on `text_field`, SurrealDB 3.3, 2.x and the embedded engine
  refuse the search, and `hybrid_search()` raises `SurrealDbError` naming the field, as above; SurrealDB 3.1 and 3.2
  match nothing, so the vector list alone is fused.
- Without a vector index on `vector_field`, the vector search finds nothing on SurrealDB 3.3, 2.x and the embedded
  engine, so the full-text ranking alone is fused, every `_knn_distance` `None`; SurrealDB 3.1 and 3.2 list the first
  `vector_limit` records by id, with no distance. Pass `dist=` for an exact scan that needs no index.

#### Not supported

```python
try:
    await Passage.objects().order_by("title").hybrid_search(
        vector_field="embedding", vector=[1.0, 0.0, 0.0], text_field="title", text_query="cats",
    )
except ValueError as error:
    print(error)
# hybrid_search() cannot be combined with order_by(): it runs its own vector and full-text searches and orders the records by their fused score. Remove them from the queryset.
```

- `order_by()`, `annotate()`, `values()`, `search()` and `similar_to()` raise `ValueError` before any request, as
  above; `as_of()` raises `SurrealDbError`: the indexes hold the records' current values.
- Several full-text fields, weights and score normalisation (`search::linear()`), and the full-text side's highlights
  are not available yet.
- **A field stored under a name `hybrid_search()` uses for its own values** — `rrf_score`, `_rrf_score`,
  `_knn_distance`, `_search_score`, `_hybrid_pos`, `_hybrid_vec_pos` or `_hybrid_text_pos` — raises `ValueError`
  before any request: store it under another name (`Field(alias=...)`). On a SCHEMALESS table, a column with one of
  these names that the model does not declare is not returned, on every line, even when `select()` names it.

#### Coming from the full SurrealDB-ORM

- **`hybrid_search()` keeps the full ORM's signature**: keyword-only `vector_field`, `vector`, `vector_limit=20`,
  `text_field`, `text_query`, `text_limit=20`, `rrf_k=60`; `ef` is `None` for 100, so that `dist=` (an exact scan,
  section 37) can be given instead.
- **It returns dicts, as the full ORM does**: each holds the record's fields, keyed by their Python names as
  `exec()`'s dict rows are, and `_rrf_score`, plus `_knn_distance` and `_search_score`, which the full ORM does not
  return.
- **It applies the queryset**: its conditions, `select()`, `fetch()`, `limit()`/`offset()`, `cache()`, transactions
  and named connections; the full ORM ignores them.
- **SurrealDB's `search::rrf()` fuses the lists on 3.x**, given both lists in one statement, and the ORM fuses them on
  2.x and the embedded engine; the full ORM calls `search::rrf()` with arguments SurrealDB 3.3 refuses, and runs on
  3.x only.
- **Equal fused scores have a fixed order** (the vector list's first), so the same two lists always give the same
  page, and an exact scan (`dist=`) gives the same vector list on every line, as above.
- **Checked before any request**: both fields, the vector, `vector_limit` and `text_limit` (1 to 10 000), `rrf_k`
  (0 to 10 000), `ef` and `dist`; the full ORM checks the field names only.
- **Reciprocal Rank Fusion only**: `search::linear()` (weights and score normalisation) is not offered yet.

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

`async with SurrealDBConnectionManager():` uses the current connection (the default, or the `using()` block's)
and closes that connection's client on exit.

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
the manager keeps one client for the whole process ([section 26](#26-embedded-engine-and-test-fixtures)).
While it is configured, `close_connection()` closes that client only, not the loop's server client:
`is_connected()` stays `True` on a loop that keeps one.

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
Rows added in v0.20.0 were measured directly on 2.7.0 and 3.3.0; rows added in v0.21.0 on 2.7.0 and 3.3.2, re-verified on 3.3.3, with 3.1.5 where it differs; rows added in v0.22.0 on 2.7.0 and 3.3.3, with 3.1.5 where it differs; rows added in v0.23.0 on 2.7.0 and 3.3.4, with 3.1.5 where it differs; rows added in v0.24.0 on 2.7.0 and 3.3.4, identical on 3.1.5; rows added in v0.26.0 on 2.7.0 and 3.3.4, in memory and on versioned storage, identical on 3.1.5; rows added in v0.27.0 on 2.7.0 and 3.3.4, with 3.1.5 where it differs; rows added in v0.28.0 on 2.7.0 and 3.3.4, identical on 3.1.5; the rows added in v0.28.1 on 2.7.0 and 3.3.4, identical on 3.1.5; rows added in v0.29.0 on 2.7.0 and 3.3.4 in one process, identical on 3.1.5. The row added in v0.29.2 was measured on 2.7.0, 3.3.4 and the embedded engine, identical on 3.1.5. Rows added in v0.30.0 were measured on 2.7.0, 3.3.4 and the embedded engine, with 3.1.5 where it differs. The row added in v0.30.3 was measured on 2.7.0 and 3.3.4, over WebSocket and HTTP, and on the embedded engine, identical on 3.1.5. The row added in v0.30.7 was measured on 2.7.0, 3.3.4 and the embedded engine, identical on 3.1.5: its parser refuses a statement that nests six objects, and `update()` nests at most four. Rows added in v0.31.0 were measured on 2.7.0, 3.3.4 and the embedded engine, identical on 3.1.5. The row added in v0.31.1 was measured on 2.7.0, 3.3.4 and the embedded engine, identical on 3.1.5. Rows added in v0.32.0 were measured on 2.7.0, 3.3.4 and the embedded engine; 3.1.5 behaves as 3.3.4 but accepts raw writes to a view. Rows added in v0.33.0 were measured on 2.7.0, 3.3.4 and the embedded engine; 3.1.5 behaves as 3.3.4. Rows added in v0.34.0 were measured on 2.7.0, 3.3.4 and the embedded engine; 3.1.5 behaves as 3.3.4 except that it refuses a Python `set` written to a `set` field, and an object item of an optional untyped `list`. Rows added in v0.35.0 were measured on 2.7.0, 3.3.4 and the embedded engine, with 3.1.5 where it differs. The row added in v0.35.2 was measured on 2.7.0 and 3.3.4, identical on 3.1.5. The row added in v0.35.3 was measured on 2.7.0 and 3.3.4, over WebSocket and HTTP, and on the embedded engine, identical on 3.1.5. Rows added in v0.36.0 were measured on 2.7.0, 3.3.4 and the embedded engine, with 3.1.5 where it differs. The row added in v0.36.2 was measured on 2.7.0 and 3.3.4, over WebSocket and HTTP, and on the embedded engine, identical on 3.1.5. Rows added in v0.37.0 were measured on 2.7.0, 3.3.4, 3.2.6 and the embedded engine, with 3.1.5 and 3.2.6 where they differ from 3.3.4. The row added in v0.37.2 was measured on 2.7.0, 3.3.4 and the embedded engine, identical on 3.1.5. The row added in v0.37.4 was measured on 2.7.0 and 3.3.4, over WebSocket and HTTP, and on the embedded engine, identical on 3.1.5. The row added in v0.37.5 was measured on 2.7.0 and 3.3.4, over WebSocket and HTTP, and on the embedded engine, identical on 3.1.5.
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
| Writing a field a `SCHEMAFULL` table does not define (`CREATE`, `INSERT`, `INSERT RELATION`)                                           | silently dropped: the record is stored without it; kept in the items of a list `define_table()` writes `FLEXIBLE` (a 2.x limitation)                                          | rejected ("Found field …, but no such field exists"): nothing is stored, except in the items of a list whose items define a field (every list of sub-models), a gap fixed in 3.4    | v0.7.0                               |
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
| A statement that fails inside `transaction()`, its error caught in the block                                                           | leaving the block raises `SurrealDbError`, nothing of the block committed (the buffered batch fails as a whole)                                                               | same on both lines; over WebSocket the statement raises at once, then the block's end cancels the transaction and raises `SurrealDbError`                                           | v0.30.3                              |
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
| Model auth with a `primary_key` (`access_ddl()`, `define_access()`)                                                                    | same on both lines: SIGNUP runs `CREATE <table> SET id = $<key>, …`, so the record id is the key (verified on 2.7.0)                                                          | same on both lines (verified on 3.3.4 and 3.1.5); v0.17.0 to v0.37.1 rendered `type::thing`, which 3.x refuses, so `define_access()` failed                                         | v0.37.2                              |
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
| Time travel (`as_of()`) on a server without versioned storage                                                                          | no error, and the ORM cannot tell: in memory or on plain SurrealKV, only records last written at or before the moment, in their current state. RocksDB: `SurrealDbError`      | `SurrealDbError` (the server refuses `VERSION`)                                                                                                                                     | v0.26.0                              |
| Time travel on versioned storage: records deleted since, counts and aggregates                                                         | right (the ORM's per-record check also corrects the server's unfiltered `count()`)                                                                                            | right (the ORM's per-record check hides the records deleted before the moment that the server's table reads return)                                                                 | v0.26.0                              |
| `fetch()` and dotted paths through record links under `as_of()`                                                                        | `fetch()` refused (`SurrealDbError`) by `exec()`, `first()`, `get()` without an id; dotted paths in `filter()`, `select()`, `order_by()`, `values()` read the current value   | as they were at the moment                                                                                                                                                          | v0.26.0                              |
| Writing history with a raw `CREATE … VERSION`                                                                                          | stored at that moment, readable with `as_of()`                                                                                                                                | accepted, not readable at that moment (the table did not exist then)                                                                                                                | v0.26.0                              |
| `as_of()` moment before 1970-01-01 or after 2262-04-11T23:47:16.854775Z                                                                | refused by the ORM before any read (`ValueError`; `SurrealDbError` for a duration or expression resolving there): the server reads an earlier moment as the current state     | refused by the ORM before any read, same errors: the server would fail with an internal error                                                                                       | v0.26.0                              |
| Subqueries (`Subquery`, or a QuerySet as a filter value)                                                                               | one statement for every read, count, aggregation, write by filter and live query; reads inside a transaction raise (buffered)                                                 | the same, and reads inside an interactive transaction (`objects(tx=)`) see its uncommitted writes                                                                                   | v0.27.0                              |
| A subquery with `order_by()` and `limit()`                                                                                             | right; the ORM also selects the order fields, without which 2.x refuses to order by a field other than the one compared                                                       | right: the ORM binds each subquery once, in a `LET`; 3.2 and later can return wrong rows for such an inline sub-SELECT (surrealdb#7470)                                             | v0.27.0                              |
| A subquery reading a table that does not exist                                                                                         | read as empty, like any missing table                                                                                                                                         | read as empty in three requests (statement, table probe, re-send); other errors, even ones quoting "does not exist", are relayed; transaction writes fail; `live()` raises on start | v0.27.0                              |
| Subqueries under `as_of()`                                                                                                             | each subquery reads at the same moment: the ORM adds `VERSION` to it (the server would read the current state)                                                                | the same (the server applies the outer `VERSION` to an inline sub-SELECT only); without versioned storage `SurrealDbError`, but 3.1.5 raises the server's own `ThrownError`         | v0.27.0                              |
| Query cache (`cache()`, `QueryCache`)                                                                                                  | same on both lines: client-side, per connection and identity; writes in a transaction invalidate at commit; reads in one are never cached (on 2.x they raise: buffered)       | the same; reads inside an interactive transaction are never cached (they see its uncommitted writes)                                                                                | v0.28.0                              |
| `select("meta.*")` on a nested object that is not a record link                                                                        | its values, as a list; through a link (`select("author.*")`), the linked record                                                                                               | the object; through a link, the linked record                                                                                                                                       | pre-existing (documented in v0.28.1) |
| `filter(f="$x")`: a string filter value starting with `$`                                                                              | same on both lines: compared with the string `"$x"`, bound like any value, in a live query too; `Var("x")` reads the variable `$x`, `Var("auth")` SurrealDB's `$auth`         | the same; on an edge's `in`/`out` (record ids) such a string raises `ValueError`, on both lines; `$access`, `$session`, `$token` read like `$auth`                                  | v0.28.1                              |
| `limit(n)` or `offset(n)` above `4294967295` (`2**32 - 1`)                                                                             | the server refuses the read: `Found <n> but the LIMIT clause must evaluate to a positive integer` (`START` likewise)                                                          | read; above `2**63 - 1` the ORM raises `ValueError` when `limit()` or `offset()` is called, on both lines                                                                           | pre-existing (documented in v0.28.1) |
| Several connections in one process (`add_connection()`, `using()`)                                                                     | each connection follows its own server: a 2.x connection's transactions are buffered, whatever the others are                                                                 | each 3.x WebSocket connection gets interactive transactions, beside 2.x connections in the same process                                                                             | v0.29.0                              |
| Text lookups (`startswith`, `endswith`, `like`, `ilike`, `match`, `regex`)                                                             | same on both lines: string functions guarded by `(field).is_string()` (`type::is::string` is 2.x-only); a `NONE`, `NULL`, non-string or array path value never matches        | the same (`type::is_string` is 3.x-only; `a.b.is_string()` tests each element of an array path); until v0.29.1: operators SurrealQL lacks (`LIKE`…), a parse error on both lines    | v0.29.2                              |
| Schema introspection (`DatabaseIntrospector`, `generate_models_from_db()`)                                                             | renders `option<T>`, array items as `tags[*]`, `FLEXIBLE` before `TYPE`, IF expressions bare: kinds and field paths are normalised to the 3.x forms                           | renders optional kinds with `none` first, items as `tags.*`, `FLEXIBLE` after `TYPE`, IF expressions in parentheses: same state and model types; expressions kept as rendered       | v0.30.0                              |
| Definitions only one line has (schema introspection)                                                                                   | `MTREE` and `SEARCH ANALYZER` indexes, `geometry<feature>`: read and typed                                                                                                    | `COMPUTED`, `REFERENCE`, `FULLTEXT`, kind `file`, `WITH REFRESH`, `BEARER`: read and typed; `DEFINE SEQUENCE`: kept raw                                                             | v0.30.0                              |
| `INFO` (schema introspection) by a record user or an anonymous session                                                                 | refused: `SurrealDbAuthenticationError` (the SDK raises `InternalError`)                                                                                                      | refused: `SurrealDbAuthenticationError` (the SDK raises `NotAllowedError`)                                                                                                          | v0.30.0                              |
| `array<T, N>` read by schema introspection                                                                                             | at most N items; the generated `list[T]` leaves the check to the server                                                                                                       | exactly N items; the generated `list[T]` leaves the check to the server                                                                                                             | v0.30.0                              |
| Schema introspection of a database removed while the connection is open                                                                | empty schema                                                                                                                                                                  | `SurrealDbError` (the database does not exist) until the database is defined again                                                                                                  | v0.30.0                              |
| DDL not yet committed in a transaction, seen by schema introspection                                                                   | invisible                                                                                                                                                                     | invisible                                                                                                                                                                           | v0.30.0                              |
| A regex with a backslash pair before a `/` (schema introspection)                                                                      | `/a\\/b/` accepted (a backslash, then an escaped slash), `/\\/` refused: such a definition is a `SchemaIssue`, not read                                                       | `/\\/` accepted (an escaped backslash, then the closing slash), `/a\\/b/` refused: such a definition is a `SchemaIssue`, not read                                                   | v0.30.0                              |
| `save()` applies the returned row (explicit id, edges); `update()`/`upsert()` keep an unchanged stored `datetime` (nanoseconds)        | stored row and read types (enums, sets…) after `save()`; a top-level `READONLY` datetime updates, a nested one may not; refused if a typed `VALUE` field is left out (#267)   | as on 2.x (verified on 3.3.4 and 3.1.5), but a typed `VALUE` field left out is recomputed; `save(tx=)` in an interactive transaction applies the row too, a buffered one does not   | v0.30.7                              |
| Server-side events (`define_events()`, `define_event()`, `remove_event()`)                                                             | same on both lines: defined, fired on `CREATE`, `UPDATE` and `DELETE`, `WHEN` honoured; `$event`, `$before`, `$after`, `$value` alike                                         | the same; names are quoted, so `update` or `delete` work as event names (3.x refuses them bare)                                                                                     | v0.31.0                              |
| Asynchronous events (`Event(async_mode=True, retry=…, max_depth=…)`)                                                                   | `SurrealDbError` before any DDL: 2.x cannot parse `ASYNC`                                                                                                                     | run after the write's transaction commits, never for a cancelled one; read back with `async_mode`, `retry`, `max_depth`                                                             | v0.31.0                              |
| `$input` in an event                                                                                                                   | `NONE`                                                                                                                                                                        | the fields the write sent (`{ name: 'a' }` for `SET name = 'a'`)                                                                                                                    | v0.31.0                              |
| A write that changes nothing, on a table with an event                                                                                 | fires no event                                                                                                                                                                | fires no event, except an `UPSERT` (the ORM's `upsert()`): one `UPDATE` event                                                                                                       | v0.31.0                              |
| An event whose `THEN` fails (a `THROW`, an error)                                                                                      | the write is refused; the SDK's `InternalError` (`An error occurred: …` for a `THROW`, the bare cause otherwise)                                                              | the write is refused; the SDK's `InternalError` (`Error while processing event <name>: …`)                                                                                          | v0.31.0                              |
| Event and function definitions read back (`EventState`, `FunctionState`)                                                               | a block on several lines, each statement ending with `;`; `option<T>` parameters as written                                                                                   | a block on one line, no `;` after a single statement; optional parameters rendered with `none` first                                                                                | v0.31.0                              |
| `define_function()`, `remove_function()` and `call_function()`                                                                         | same on both lines: the next `call_function()` sees a redefinition on every connection of this process to the same namespace and database, whatever its URL                   | the same                                                                                                                                                                            | v0.31.0                              |
| Reading a datetime before year 1 or after year 9999 (SurrealDB stores years -262143 to +262142)                                        | its RFC 3339 text, as `<string>` gives it (`"+10000-01-01T00:00:00Z"`): an untyped field holds it, a `datetime` field refuses it (`ValidationError`)                          | same on both lines                                                                                                                                                                  | v0.31.1                              |
| Writes to a materialized view (`save()`, `merge()`, `delete()`, bulk writes … of a `view_query` model)                                 | `TypeError` before any request; a raw write is accepted by the server and changes the view's records                                                                          | `TypeError` before any request; the server refuses raw writes too (3.2 and later; 3.1 accepts them)                                                                                 | v0.32.0                              |
| Aggregates in a grouped view (`view_query` with `GROUP BY` or `GROUP ALL`)                                                             | `count()`, `math::sum()`, `math::mean()`, `math::min()`, `math::max()`, `time::min()`, `time::max()`; `math::stddev()`, `math::variance()`: `SurrealDbError` before any DDL   | the same, plus `math::stddev()` and `math::variance()`                                                                                                                              | v0.32.0                              |
| An expression around an aggregate in a grouped view (`math::sum(amount) / count()`, `math::round(math::mean(amount))`, `count() * 2`)  | `SurrealDbError` before any DDL: the server would compute it once per source record (a wrong `count()`, or every source write refused)                                        | kept up to date                                                                                                                                                                     | v0.32.0                              |
| A grouped view selecting a field outside `GROUP BY` and outside an aggregate (`amount`, `math::percentile(amount, 50)`, `*`)           | `SurrealDbError` before any DDL: the server would keep the last source record's value, or refuse every source write                                                           | `SurrealDbError` when the view is defined (the server refuses it)                                                                                                                   | v0.32.0                              |
| A grouped view whose `GROUP BY` names the alias of a computed field (`time::month(at) AS month` … `GROUP BY month`)                    | `SurrealDbError` before any DDL: the server would put every source record in one group                                                                                        | one group per computed value                                                                                                                                                        | v0.32.0                              |
| Values a view computes (`math::sum()`, `math::mean()`, `count()` of an array)                                                          | a sum of integers is an `int`, a mean a `Decimal`; `count(tags)` of an array field adds the arrays' lengths                                                                   | both are `float`; `count(tags)` counts the records whose array is not empty                                                                                                         | v0.32.0                              |
| Live queries and events on a view (a group emptied by a delete; a live query filtered on `id`)                                         | an emptied group: an `UPDATE` with the emptied values (a `count()` of 0), then a `DELETE`; a filter on `id` notifies                                                          | an emptied group: a `DELETE`; a filter on `id` never notifies ([surrealdb#7607](https://github.com/surrealdb/surrealdb/issues/7607)): filter on the view's fields                   | v0.32.0                              |
| Removing a materialized view, or a source table, that another view reads (`remove_view()`, `REMOVE TABLE`)                             | `remove_view()` raises `SurrealDbError` before any DDL, unless its transaction removes those views first; a raw `REMOVE TABLE` makes them ordinary tables, last records kept  | `SurrealDbError` from the server (`Cannot delete table … on which a view is defined`): remove the views that read it first                                                          | v0.32.0                              |
| Relation constraints from an edge model (`define_relation()`: `TYPE RELATION IN … OUT … ENFORCED`)                                     | same on both lines: one `DEFINE TABLE OVERWRITE` that keeps the table's other clauses, records, fields, indexes and events, enforced at once                                  | the same                                                                                                                                                                            | v0.33.0                              |
| A raw `ALTER TABLE … TYPE RELATION …` of a table already written to                                                                    | `INFO` shows the new type, but writes keep the old constraint until a `DEFINE TABLE OVERWRITE`; the ORM never sends `ALTER TABLE`                                             | enforced at once; the ORM still sends `DEFINE TABLE OVERWRITE`                                                                                                                      | v0.33.0                              |
| An edge endpoint outside `IN`/`OUT` (`relate()`, typed or by table name, edge `save()`, `bulk_create()`)                               | `SurrealDbError` (`Found … but expected a record<…>`); with `ENFORCED` and no such record, `SurrealDbNotFoundError` (`does not exist`)                                        | `SurrealDbError` (`Couldn't coerce value for field …`), whether the record exists or not                                                                                            | v0.33.0                              |
| Model table DDL (`define_table()`: `DEFINE TABLE` + `DEFINE FIELD`)                                                                    | same on both lines: one `DEFINE TABLE OVERWRITE` and one `DEFINE FIELD OVERWRITE` per column, keeping what the model does not declare                                         | the same                                                                                                                                                                            | v0.34.0                              |
| An `Any` or untyped `list`/`set` field of a `SCHEMAFULL` model                                                                         | `define_table()` adds `FLEXIBLE` (read from `client.version()`): without it these engines empty such values, optional or not; the item definitions as on 3.x                  | kept without `FLEXIBLE` (refused there); an untyped `list` gets an item definition `TYPE any`, without which 3.x refuses an object item of a required one (`no such field exists`)  | v0.34.0                              |
| A `list` of sub-models (`list[M]`, at any depth) holding a `dict`, an `Any` or an untyped `list`, in a `SCHEMAFULL` model              | `define_table()` also writes the `list` `FLEXIBLE`: without it these engines drop such a value from every item, then refuse the record's next write (`Found NONE for field`)  | kept without `FLEXIBLE` on the `list`, which `define_table()` does not write there                                                                                                  | v0.34.0                              |
| A write naming a field a `SCHEMAFULL` table does not define (an undeclared `merge()` key, a raw query, records predating `SCHEMAFULL`) | the field is dropped, silently; kept in the items of a list `define_table()` writes `FLEXIBLE` (a 2.x limitation)                                                             | refused (`no such field exists`); kept in the items of a list whose items define a field (every list of sub-models), a gap fixed in SurrealDB 3.4 (nightly at the time of writing)  | v0.34.0                              |
| Full-text index (`define_indexes()`, `index_ddl(line=)`)                                                                               | `SEARCH ANALYZER` (`FULLTEXT` is a parse error), read from `client.version()`; several fields accepted, the ORM writes one                                                    | `FULLTEXT ANALYZER` (`SEARCH` is a parse error); one field per index                                                                                                                | v0.35.0                              |
| Removing an analyzer an index uses (`remove_analyzer()`)                                                                               | the server would accept it, then refuse every write to the table: the ORM refuses it first, with 3.x's message                                                                | refused by the server                                                                                                                                                               | v0.35.0                              |
| BM25 score of a term in more than half the documents (`SearchScore`)                                                                   | below zero: a text holding such a term scores lower than without it, and ranks differently than on 3.x                                                                        | `0.0`: the term adds nothing to the score                                                                                                                                           | v0.35.0                              |
| BM25 statistics after a rebuild, a redefinition or a deletion (`SearchScore`)                                                          | not reset: a `REBUILD INDEX`, an `OVERWRITE` or a new index of the same name counts the records again, and deleted records stay counted; the matches are unaffected           | kept in step with the records: the scores of a freshly built index                                                                                                                  | v0.35.0                              |
| Two `search()` fields AND-ed                                                                                                           | only the records matching every field, each field scored as if searched alone                                                                                                 | the same on 3.3; 3.1 can also return records that match only one of the fields, scored `0.0` on the other                                                                           | v0.35.0                              |
| `search()` on a field without a full-text index                                                                                        | `SurrealDbError` naming the field                                                                                                                                             | the same; 3.1 answers no record                                                                                                                                                     | v0.35.0                              |
| `live()` with a `Subquery` that searches a field without a full-text index                                                             | `SurrealDbError` naming the field, before the live query starts                                                                                                               | the same (the live query would never notify); 3.1 starts it                                                                                                                         | v0.35.0                              |
| An auth model with a `Field(exclude=True)` field SIGNUP writes (`password: str = Field(exclude=True)`)                                 | `TypeError` before any request, whatever the schema mode and `with_table`: the DDL methods, `signup()`, `signin()`, `authenticate()`, `refresh()`, `update()`, `upsert()`     | same on both lines (a check of the model, no request)                                                                                                                               | v0.35.2                              |
| `define_indexes(tx=)` in a block that writes to the model's table (an ORM write before the index, a write after it)                    | a write before it: refused (left out of the index); a write after it, to a table holding records, fails the commit with a retryable `SurrealDbConflictError`, again on retry  | a write before it: refused (left out of the index, [surrealdb#7618](https://github.com/surrealdb/surrealdb/issues/7618)); a write after it is committed and indexed                 | v0.35.3                              |
| Vector index (`Index(hnsw=True)`, `define_indexes()`)                                                                                  | `HNSW` as on 3.x (the ORM writes `DIST`, `TYPE`, `EFC` and `M` and one field); 2.x also accepts `MTREE`, which `similar_to()` does not use                                    | `HNSW`, one field per index; `MTREE` is a parse error                                                                                                                               | v0.36.0                              |
| `similar_to()` with `ef` below `limit`                                                                                                 | the ORM writes `ef` raised to `limit`: 2.x would return at most `ef` records                                                                                                  | the same statement: 3.x returns `limit` records either way                                                                                                                          | v0.36.0                              |
| `similar_to()` with other conditions (`filter()`, `Q`, `nearby()`, `search()`)                                                         | the ORM selects the records matching them first (a `LET`): 2.x answers too few or no records next to many of them, an indexed field's included                                | every condition stays next to the KNN operator, applied during the index search (faster)                                                                                            | v0.36.0                              |
| `similar_to()` and `search()` in one queryset                                                                                          | the ORM selects the full-text matches first (a `LET`): 2.x answers no record next to a full-text match; search annotations are refused                                        | the match stays next to the KNN operator, with its scores, highlights and offsets; 3.1 answers every match, with no distance                                                        | v0.36.0                              |
| `bulk_update()`, `bulk_delete()`, `patch()` with `similar_to()`                                                                        | the ORM writes the ids of the `limit` nearest: 2.x's exact scan in a write would act on every matching record                                                                 | the same: 3.x writes nothing when a range on an indexed field shares the KNN operator                                                                                               | v0.36.0                              |
| `similar_to()` with a condition that keeps few records                                                                                 | HNSW is approximate: fewer than `limit` records, depending on how the index was built, even with a tenth of the table kept; `dist=` is exact                                  | rarer; also seen with a `Subquery` id list keeping a few percent of the table                                                                                                       | v0.36.0                              |
| `similar_to(dist=…)` with a condition on a linked record's field                                                                       | the ORM selects the matching records first (a `LET`): 2.x's exact scan would return every matching record, ignoring `limit`                                                   | the condition stays next to the KNN operator                                                                                                                                        | v0.36.0                              |
| `similar_to()` on a field without a vector index                                                                                       | no record                                                                                                                                                                     | no record; 3.1 answers every record, with no distance                                                                                                                               | v0.36.0                              |
| `remove_index(tx=)` then `define_indexes(tx=)` in one block, the index stored before the block                                         | on a table holding records, the commit fails with a retryable `SurrealDbConflictError`, again on retry: nothing committed, the index kept; on an empty table, defined again   | the index is defined again after its removal and committed, serving the records committed before the block                                                                          | v0.36.2                              |
| `hybrid_search()`: the fusion                                                                                                          | the ORM fuses the two lists with `search::rrf()`'s formula, which SurrealDB 2.x does not have: the same records, scores and order for the same two lists                      | SurrealDB's `search::rrf()` fuses them; the ORM orders equal scores, which SurrealDB returns in no fixed order                                                                      | v0.37.0                              |
| `hybrid_search()` with other conditions                                                                                                | applied to both lists; the vector list selects the records first (a `LET`), as `similar_to()` does                                                                            | applied to both lists, during each search                                                                                                                                           | v0.37.0                              |
| `hybrid_search()` on a field without a vector or full-text index                                                                       | no vector index: the vector list is empty, so the full-text ranking alone is fused; no full-text index: `SurrealDbError` naming the field                                     | the same; 3.1 and 3.2 put the first `vector_limit` records by id in the vector list, with no distance, and match no record for the text                                             | v0.37.0                              |
| An exact scan (`similar_to(dist=…)`, `hybrid_search(dist=…)`) with records at an equal distance at the last place                      | the ORM ranks the scan by distance, then id: the same records on every call, the tied ones by id (2.x's own scan picks among them in no fixed order)                          | the same records on every call, as with the HNSW index                                                                                                                              | v0.37.4                              |
| `define_indexes(tx=)` then `remove_index(tx=)` in one block, the index not stored before the block                                     | committed: the index absent after the commit, whatever the table holds                                                                                                        | the commit fails with a retryable `SurrealDbConflictError`, whatever the table holds: nothing committed                                                                             | v0.37.5                              |
| Full suite and every divergence in this table, re-run on SurrealDB 2.7.0 and 3.3.0                                                     | 2.7.0 identical to 2.6.5 (same passes, same 3.x-only skips)                                                                                                                   | 3.3.0 identical to 3.2.4, except the system-user signin row                                                                                                                         | v0.19.1                              |

> **Note on record IDs**: A record loaded from the database has its `id` field set to a native `surrealdb.RecordID` object, not a plain string. Use `model.get_raw_id()` to obtain the bare identifier string (e.g. `"alice"`), or compare directly with `model.id == RecordID("User", "alice")`. In-memory instances you construct yourself retain whatever value you assign.

### ORM behaviour: the embedded engine

The official SDK's in-process engine ([section 26](#26-embedded-engine-and-test-fixtures)) is a
line of its own: SurrealDB core 2.3.10, bundled with SDK 2.0.0. Rows measured with SDK 2.0.0 on
`mem://` (the time-travel row on `surrealkv+versioned://`), and on the 2.7.0 and 3.3.4 servers.
Capabilities not listed behave as on a 2.x server.

| ORM capability                                                      | Embedded engine (SDK 2.0.0)                                                                                                                                                                                                                                                                                                                           | Servers (2.x and 3.x)                                                                                           | Since   |
| ------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- | ------- |
| Connection and credentials (`set_connection()`)                     | URL, namespace and database; `user` and `password` optional, kept and never sent                                                                                                                                                                                                                                                                      | every setting required; the configured user signs in on each new connection                                     | v0.25.0 |
| URLs the SDK refuses (`rocksdb://`, a typo)                         | `SurrealDbConnectionError` quoting the SDK (naming #233 for `rocksdb://`), before anything is opened                                                                                                                                                                                                                                                  | n/a                                                                                                             | v0.25.0 |
| Clients                                                             | one per connection for the whole process, shared by every event loop and thread                                                                                                                                                                                                                                                                       | one per connection and event loop                                                                               | v0.25.0 |
| Namespace/database change                                           | the next call resets the session to the owner and selects the new pair on the same engine (no data lost); the stored session ends                                                                                                                                                                                                                     | taken by the next connection                                                                                    | v0.25.0 |
| Closing (`close_connection()`, `unset_connection()`, `reconnect()`) | `mem://` discarded, `surrealkv://` reopened with its data; with SDK 2.x a client kept after the close is still a running engine; `close_connection()` and `reconnect()` leave the server clients open (`is_connected()` stays true on a loop with one)                                                                                                | the connection closes                                                                                           | v0.25.0 |
| SurrealQL line                                                      | 2.x (SurrealDB 2.3.10 core); `client.version()` reports the SDK (`surrealdb-2.0.0`)                                                                                                                                                                                                                                                                   | the server's                                                                                                    | v0.25.0 |
| `transaction()`                                                     | buffered                                                                                                                                                                                                                                                                                                                                              | interactive on WebSocket and SurrealDB 3.x, else buffered                                                       | v0.25.0 |
| Concurrent first writes to a table that does not exist yet          | retryable conflict, a few % per batch of 20: define the table first                                                                                                                                                                                                                                                                                   | rare on 2.x, none measured on 3.x                                                                               | v0.25.0 |
| Live queries (`live()`, `watch()`, `subscribe_live()`)              | `SurrealDbError` before any request: use change feeds                                                                                                                                                                                                                                                                                                 | WebSocket only                                                                                                  | v0.25.0 |
| Change feeds (`changes()`)                                          | from now and from a cursor; `since=<datetime>` refused (`SurrealDbError`)                                                                                                                                                                                                                                                                             | from now, a cursor or a datetime                                                                                | v0.25.0 |
| Model auth (`AuthenticatedUserMixin`)                               | `bind=True` only, on the shared session, one call at a time with connection-level auth calls (the last one wins); a failure restores the previous session, except a record that does not fit the model after a successful `signup()` or `signin()`: the session stays signed in as it, as on a server (`refresh()` cannot succeed: no `WITH REFRESH`) | a session of its own; `bind=True` also adopts the token                                                         | v0.25.0 |
| `ephemeral_client()`                                                | `SurrealDbError`: the engine has a single session per database                                                                                                                                                                                                                                                                                        | an anonymous throwaway client                                                                                   | v0.25.0 |
| Anonymous session                                                   | the in-process owner (full access); `info()` returns `None`                                                                                                                                                                                                                                                                                           | not used by the ORM (the configured user signs in); an anonymous `info()` raises `SurrealDbAuthenticationError` | v0.25.0 |
| `invalidate()`                                                      | back to the in-process owner                                                                                                                                                                                                                                                                                                                          | signs in again as the configured user                                                                           | v0.25.0 |
| Reading a table that does not exist                                 | `[]`, raw queries included (as on 2.x)                                                                                                                                                                                                                                                                                                                | the ORM reads `[]`; a raw query on 3.x raises `NotFoundError`                                                   | v0.25.0 |
| Parse errors                                                        | bare `RuntimeError` (the DDL helpers and the commit of `transaction()` raise `SurrealDbError`)                                                                                                                                                                                                                                                        | the SDK's error classes                                                                                         | v0.25.0 |
| Time travel (`as_of()`)                                             | `surrealkv+versioned://` only (`?versioned=true` is ignored by SDK 2.0.0); any other URL → `SurrealDbError` before any request; `fetch()` refused (2.x core)                                                                                                                                                                                          | versioned storage required (see the 2.x/3.x table)                                                              | v0.26.0 |
| Several connections (`add_connection()`)                            | `mem://`: each name is a database of its own; an on-disk store (`surrealkv://`, `file://`) is open under one name at a time — a second name raises `SurrealDbConnectionError`                                                                                                                                                                         | each name opens its own clients; two databases on one server are isolated                                       | v0.29.0 |
| Schema introspection (`DatabaseIntrospector`)                       | reads like a 2.x server (core 2.3.10): same state and same generated model types as on the servers; the anonymous owner session may run `INFO`; `COUNT` indexes cannot be defined                                                                                                                                                                     | need a system user (root, namespace or database user)                                                           | v0.30.0 |
| Server-side events and stored functions (v0.31.0)                   | as on a 2.x server: `$input` is `NONE`; `Event(async_mode=True)` raises `SurrealDbError` before any DDL                                                                                                                                                                                                                                               | `ASYNC` events on 3.x                                                                                           | v0.31.0 |
| Materialized views (v0.32.0)                                        | as on a 2.x server: `int` sums and `Decimal` means; in a grouped view, `math::stddev()`/`math::variance()`, an expression around an aggregate, a field outside `GROUP BY` and a `GROUP BY` on an alias raise `SurrealDbError` before any DDL; writes to a view refused by the ORM (the engine would accept them); no live queries                     | `float` sums and means on 3.x                                                                                   | v0.32.0 |
| TYPE RELATION constraints (v0.33.0)                                 | as on a 2.x server: a raw `ALTER TABLE … TYPE` of a table already written to is not enforced (`define_relation()` sends `DEFINE TABLE OVERWRITE`); an endpoint outside `IN`/`OUT` whose record does not exist raises `SurrealDbNotFoundError`                                                                                                         | `SurrealDbError` for that endpoint on 3.x                                                                       | v0.33.0 |
| Model table DDL (v0.34.0)                                           | as on a 2.x server: `define_table()` adds `FLEXIBLE` to the `Any` and untyped columns of a `SCHEMAFULL` table and to its lists of sub-models holding a `FLEXIBLE` column; a write naming a field the table does not define drops that field, except in the items of a list `define_table()` writes `FLEXIBLE`, which keep it                          | 3.x keeps them without `FLEXIBLE`, refuses that write except in list items defining a field, a gap fixed in 3.4 | v0.34.0 |
| Full-text search (v0.35.0)                                          | as on a 2.x server: `SEARCH ANALYZER`, a term in more than half the documents scores below zero, BM25 statistics grow with each rebuild and keep deleted records, `remove_analyzer()` refuses an analyzer an index uses; an index defined in the transaction that defines its analyzer sees it, which `define_indexes(tx=…)` refuses all the same     | 3.x writes `FULLTEXT ANALYZER`, scores such a term `0.0`, keeps BM25 statistics exact and refuses that removal  | v0.35.0 |
| `define_indexes(tx=)` in a block that writes to the model's table   | an ORM write before it: refused all the same, although this engine indexes its records; a write after it is committed and indexed                                                                                                                                                                                                                     | refused (left out of the index); on 2.x a write after it, to a table holding records, fails the commit          | v0.35.3 |
| Vector search (v0.36.0)                                             | as on a 2.x server: `MTREE` accepted, at most `ef` records returned (the ORM raises `ef` to `limit`), other conditions selected first in a `LET` (search annotations refused), writes made by the ids of the nearest, fewer records with a selective condition; an index without `TYPE` is `F64` (servers: `F32`), so the ORM always writes `TYPE`    | 3.x refuses `MTREE`, applies the conditions during the index search and keeps search annotations                | v0.36.0 |
| `remove_index(tx=)` then `define_indexes(tx=)` in one block         | the index is defined again after its removal and committed, on a table holding records too                                                                                                                                                                                                                                                            | the same on 3.x; on 2.x, on a table holding records, the commit fails with a retryable conflict                 | v0.36.2 |
| Hybrid search (v0.37.0)                                             | as on a 2.x server: the ORM fuses the two lists with `search::rrf()`'s formula, which the engine does not have, and the vector list selects the other conditions first (a `LET`); an exact scan (`dist=`) keeps the records at an equal distance at the `vector_limit`-th place by id (since v0.37.4), as `similar_to(dist=…)` does                   | 3.x fuses with `search::rrf()`, each list applying the conditions; its exact scan returns the same list         | v0.37.0 |
| Everything else                                                     | CRUD, QuerySet, aggregations, relations, typed edges, rich and geo fields, upsert/patch, server values, computed fields, stored functions, signals: the same as on a 2.x server                                                                                                                                                                       | unchanged                                                                                                       | v0.25.0 |

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
| v0.26.0           | Time travel (versioned storage)                      | ✅ Released |
| v0.27.0           | Subqueries                                           | ✅ Released |
| v0.28.0           | Query cache                                          | ✅ Released |
| v0.29.0           | Tier 2 — Multi-database                              | ✅ Released |
| v0.30.0           | Tier 3 — Schema introspection                        | ✅ Released |
| v0.31.0           | Tier 3 — Server-side events and DDL helpers          | ✅ Released |
| v0.32.0           | Tier 3 — Materialized views                          | ✅ Released |
| v0.33.0           | Tier 3 — `TYPE RELATION` constraints                 | ✅ Released |
| v0.34.0           | Tier 3 — Model table DDL                             | ✅ Released |
| v0.35.0           | Tier 3 — Full-text search                            | ✅ Released |
| v0.36.0           | Tier 3 — Vector search                               | ✅ Released |
| v0.37.0           | Tier 3 — Hybrid search                               | ✅ Released |
| v0.38.0 – v0.40.0 | Tier 3 — Advanced (migrations, CLI)                  | 📋 Planned  |
| v0.41.0           | Beta Phase (API freeze, hardening)                   | 📋 Planned  |
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

| Feature                       | ORM-lite (official SDK)                                               | ORM (custom SDK)                          |
| ----------------------------- | --------------------------------------------------------------------- | ----------------------------------------- |
| Supported SurrealDB           | **2.6/2.7 + 3.2/3.3**                                                 | 3.x only                                  |
| CRUD & QuerySet               | ✅                                                                    | ✅                                        |
| Aggregations & GROUP BY       | ✅                                                                    | ✅                                        |
| Model Signals                 | ✅                                                                    | ✅                                        |
| Bulk Operations               | ✅                                                                    | ✅                                        |
| Q Objects (OR/AND/NOT)        | ✅                                                                    | ✅                                        |
| Parameterized Filters         | ✅                                                                    | ✅                                        |
| Relations & Graph             | ✅ (reverse, keyword edge fields, tx= on every write helper: v0.22.0) | ✅                                        |
| FETCH clause                  | ✅                                                                    | ✅                                        |
| Transactions (tx=)            | ✅ v0.8 (core), v0.9 QS                                               | ✅                                        |
| Interactive tx (3.x native)   | ✅ v0.9                                                               | ✅                                        |
| upsert / update_or_create     | ✅ v0.10.0                                                            | ✅                                        |
| Atomic field/array operations | ✅ v0.11.0                                                            | ✅                                        |
| Retry on conflict             | ✅ v0.12.0                                                            | ✅                                        |
| SurrealFunc & server values   | ✅ v0.13.0                                                            | ✅                                        |
| Computed fields               | ✅ v0.14.0                                                            | ✅                                        |
| `call_function()` (`fn::`)    | ✅ v0.15.0                                                            | ✅                                        |
| JWT Authentication            | ✅ v0.16.0 (connection)                                               | ✅                                        |
| Model auth mixin              | ✅ v0.17.0                                                            | ✅                                        |
| Field Aliases & DX            | ✅ v0.18.0                                                            | ✅                                        |
| Live queries (raw notifs)     | ✅ v0.19.0                                                            | ✅                                        |
| Typed live queries            | ✅ v0.20.0                                                            | ✅                                        |
| Live auto-resubscribe / CDC   | ✅ v0.21.0                                                            | ✅                                        |
| Native typed relations        | ✅ v0.22.0 (typed edge models)                                        | ✅ DDL only (TableType.RELATION)          |
| Rich field types              | ✅ v0.23.0                                                            | ✅                                        |
| Geospatial Fields             | ✅ v0.24.0                                                            | ✅                                        |
| Embedded engine               | ✅ v0.25.0 (mem://, surrealkv://; rocksdb:// #233)                    | ❌ (HTTP/WebSocket only)                  |
| Time travel (`VERSION`)       | ✅ v0.26.0 (`as_of()`)                                                | ❌                                        |
| Subqueries                    | ✅ v0.27.0 (`Subquery`, a QuerySet in `filter()`)                     | ✅                                        |
| Query Cache                   | ✅ v0.28.0 (`cache()`, `QueryCache`)                                  | ✅                                        |
| Multi-database                | ✅ v0.29.0 (`add_connection()`, `using()`)                            | ✅                                        |
| Schema Introspection          | ✅ v0.30.0 (`DatabaseIntrospector`, `generate_models_from_db()`)      | ✅                                        |
| DEFINE EVENT                  | ✅ v0.31.0 (`Event`, `define_events()`, `ASYNC` on 3.x)               | ✅ migrations (`DefineEvent`)             |
| `define_function()` (DDL)     | ✅ v0.31.0                                                            | ❌                                        |
| Materialized views            | ✅ v0.32.0 (`view_query`, `define_view()`)                            | ✅                                        |
| TYPE RELATION enforcement     | ✅ v0.33.0 (`relation_in`, `define_relation()`)                       | ✅ DDL (`TableType.RELATION`, migrations) |
| Model table DDL               | ✅ v0.34.0 (`define_table()`)                                         | ✅                                        |
| Full-Text Search              | ✅ v0.35.0 (`search()`)                                               | ✅                                        |
| Vector Search (KNN/HNSW)      | ✅ v0.36.0 (`similar_to()`)                                           | ✅                                        |
| Hybrid Search (RRF)           | ✅ v0.37.0 (`hybrid_search()`)                                        | ✅                                        |
| Migrations & CLI              | v0.38 – v0.39                                                         | ✅                                        |
| Test Fixtures & Factories     | ✅ v0.25.0 SurrealFixture; factories v0.40.0                          | ✅                                        |
| Retry, Logging, Metrics       | v0.41.0                                                               | ✅                                        |
| Connection Pool               | post-GA (tentative)                                                   | ✅                                        |
| Custom SDK / CBOR Protocol    | ❌ never                                                              | ✅                                        |

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
