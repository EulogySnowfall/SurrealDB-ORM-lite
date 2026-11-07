# Relations and graph

Graph relations, typed edges and `TYPE RELATION` constraints.

> **The embedded engine since v0.50.0** is a SurrealDB 4.0 core (SDK 3.0), and the ORM treats it as a 4.x
> server. Where a passage below groups "SurrealDB 2.x and the embedded engine", it describes the SDK 2.0 engine
> of `0.40.x` (a 2.3.10 core); the [embedded table](compatibility.md#orm-behaviour-the-embedded-engine) lists what the
> engine does now.

## 7. Relations & Graph

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

## 23. Typed edges (`BaseSurrealEdge`)

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
  that `relate("purchased", …)` writes (see [Custom table name](models.md#custom-table-name-table_name)).
  Without it the table is the class name.
- A subclass may not redeclare `in_` or `out` (`TypeError` at the `class` statement).

### Creating edges

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

### Reading edges

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
lookup on the field `in_` (see [Fields ending in an underscore](querying.md#fields-ending-in-an-underscore)).

### Endpoints are immutable

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

### Explicit ids

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

### Idempotent edges

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

### Refused writes and permissions

SurrealDB answers an `INSERT RELATION` that the edge table's `PERMISSIONS FOR create` refuse (or
an edge the user may not `select` afterwards) with fewer rows and no error, on both lines. The ORM
raises `SurrealDbError` instead: "Can't save data, no record returned: …" for one edge (`save()`,
`relate(Edge, …)`; `post_save` does not fire) and "Only k of n edges came back: …" for
`bulk_create()`. Outside a transaction such a batch is **not atomic**: the edges the server
accepted stay written. Inside an interactive transaction the error rolls the whole transaction
back; a buffered transaction cannot detect the refusal, since nothing comes back before commit.

### Transactions

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

### Live queries and change feeds

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

### A `TYPE RELATION` table, defined by hand

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

### Migrating from SurrealDB-ORM (graph API)

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

## 34. Relation constraints (`relation_in`, `define_relation()`)

Since v0.33.0 an edge model ([section 23](#23-typed-edges-basesurrealedge)) declares which tables its edges may join
and whether both ends must exist — SurrealDB's `TYPE RELATION IN … OUT … ENFORCED` — with the full SurrealDB-ORM's
keys: `relation_in`, `relation_out`, `enforced` and `table_type=TableType.RELATION`. `define_relation()` applies the
constraint to the table and SurrealDB enforces it on every write, the same on SurrealDB 2.x, 3.x and the embedded
engine, except where the [2.x/3.x/4.x table](compatibility.md#orm-behaviour-surrealdb-2x-3x-and-4x) says otherwise. The examples assume a
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

### Declaring the constraint

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
  ([section 35](schema.md#35-model-table-ddl-define_table-schema_mode-permissions)).
- The keys are checked when the class is defined: a `table_type` that is not a `TableType` (a plain `"relation"`
  string included), an `enforced` that is not a bool, or a value that is not a string, a list or a tuple raises
  `TypeError`; a name that is not an identifier, or a string of spaces only, raises `ValueError`. On a node model
  (`BaseSurrealModel`), `relation_in`, `relation_out` and `enforced` — even empty or `False` — and
  `table_type=TableType.RELATION` raise `TypeError`: derive the model from `BaseSurrealEdge`. A subclass inherits the
  keys; setting one to `None` resets it.
- The getters read the declaration back: `get_relation_in()` and `get_relation_out()` return tuples (`()` for any
  table), the shape of introspection's `TableState.relation_in` and `relation_out`
  ([section 31](schema.md#31-schema-introspection-databaseintrospector-generate_models_from_db)), so a model compares with the
  database; `is_enforced()` returns a bool; `get_table_type()`, on every model as in the full ORM, returns
  `TableType.RELATION` for an edge model, else the configured `table_type`, else `TableType.NORMAL`.

```python
print(Likes.get_relation_in(), Likes.get_relation_out(), Likes.is_enforced(), Likes.get_table_type())
# ('person',) ('post', 'book') True relation
```

### Applying it

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
  ([section 33](schema.md#33-materialized-views-view_query-define_view)), its live queries and its change feed's history are
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
  ([section 35](schema.md#35-model-table-ddl-define_table-schema_mode-permissions)).
- SurrealDB rewrites the `in` and `out` field definitions from `IN` and `OUT` (`TYPE record<person>`): an `ASSERT`
  written on them by hand is lost. Declare the tables with `relation_in` and `relation_out` instead.
- Why not `ALTER TABLE`, which keeps the other clauses too: on SurrealDB 2.x and the embedded engine, a raw
  `ALTER TABLE … TYPE RELATION …` of a table already written to is not enforced until a `DEFINE TABLE OVERWRITE` of
  the table. The ORM sends `DEFINE TABLE OVERWRITE` on every line.

### What SurrealDB refuses

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
  [section 32](functions-and-events.md#32-server-side-events-and-stored-functions-event-define_events-define_function); a failed `patch()`
  test, [section 13](writing.md#13-patch--atomic-fieldarray-operations)).

`SurrealDbNotFoundError` is a `SurrealDbError`, so `except SurrealDbError` handles every refused edge creation the
same way on both lines.

### Records already in the table

SurrealDB checks the writes made after `define_relation()`, not the records already in the table:

- a nonconforming edge (from or to a table outside `IN`/`OUT`) stays and is still traversed, but its `merge()`,
  `update()`, `patch()`, `atomic_*()` and `objects().bulk_update()` raise the SDK's `InternalError` (the mismatch
  text) on SurrealDB 2.x and 3.x, its `ValidationError` on 4.x and the embedded engine, both `ServerError`s;
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
except ServerError as exc:  # InternalError on SurrealDB 2.x and 3.x
    print(type(exc).__name__)
# ValidationError

outside = await Knows.raw_query(
    "SELECT VALUE id FROM knows WHERE record::tb(in) NOT IN ['person'] OR record::tb(out) NOT IN ['person']"
)
dangling = await Knows.raw_query("SELECT VALUE id FROM knows WHERE in.id IS NONE OR out.id IS NONE")
print(outside == [old.id], dangling)
# True []
await old.delete()
```

### Removing the constraint

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

### Transactions, connections and the query cache

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
- The query cache ([section 29](querying.md#29-query-cache-cache-querycache)): `define_relation()` changes no record, so it drops
  no cached read.
- Defining needs a system user allowed to define: root, or a namespace or database `OWNER` or `EDITOR`. A session
  that may not read the definitions or define the table gets `SurrealDbAuthenticationError` (a record user, at the
  read). Applying DDL fires no signal.

### SurrealDB 2.x and 3.x

- `define_relation()` is the same on every line and on the embedded engine: one `INFO FOR DB`, one
  `DEFINE TABLE OVERWRITE`, enforced at once.
- The wording of the server's refusals differs: `Found … but expected a record<…>` on 2.x and the embedded engine,
  `Couldn't coerce value for field …` on 3.x.
- An endpoint both outside `IN`/`OUT` and missing, on an `ENFORCED` table, raises `SurrealDbNotFoundError` on 2.x and
  the embedded engine (the server reports it missing) and `SurrealDbError` on 3.x. `SurrealDbNotFoundError` is a
  `SurrealDbError`, so `except SurrealDbError` handles every refused edge creation the same way on both lines.
- A raw `ALTER TABLE … TYPE RELATION …` of a table already written to is not enforced on 2.x and the embedded engine
  until a `DEFINE TABLE OVERWRITE` of the table; 3.x enforces it at once. The ORM never sends `ALTER TABLE`.
- The [2.x/3.x/4.x table](compatibility.md#orm-behaviour-surrealdb-2x-3x-and-4x) and the
  [embedded engine's](compatibility.md#orm-behaviour-the-embedded-engine) list these rows.

### Security

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

### Coming from the full SurrealDB-ORM

- **The keys and `TableType` are the same**: `table_type=TableType.RELATION`, `relation_in`, `relation_out` and
  `enforced`, and `TableType`'s six members and values.
- **The relation model is `BaseSurrealEdge`**: a `BaseSurrealModel` with `table_type=TableType.RELATION` (the full
  ORM's `class Likes(BaseSurrealModel)` form) raises `TypeError`; derive it from `BaseSurrealEdge`.
- **`relation_ddl()` and `define_relation()` on the edge model** render and apply the constraint, where the full ORM
  applies the DDL through migrations or `define_table()`. `define_table()` (since v0.34.0) applies an edge model
  through the statement `define_relation()` carries, then its data fields; migrations (`CreateTable`/`AlterTable`
  with the relation keys) are in [section 39](migrations.md#39-migrations-makemigrations-migrationexecutor-operations).
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
  ([section 31](schema.md#31-schema-introspection-databaseintrospector-generate_models_from_db)): their base class,
  `BaseSurrealEdge`, says it.
- **`TableType`'s other members take effect in `define_table()`**
  ([section 35](schema.md#35-model-table-ddl-define_table-schema_mode-permissions)).
- **`remove_relation()` deletes edges, not a constraint**: the name is the graph API's (section 7); a keyless
  `define_relation()` removes the constraint.
- **Upgrading from 0.32 or earlier**: these keys were accepted and ignored before 0.33.0. They are now checked at the
  `class` statement, so a node model carrying `relation_in`, `relation_out` or `enforced`, or
  `table_type=TableType.RELATION`, or a `table_type` given as a plain string (`"relation"`), raises `TypeError` at
  import: derive the model from `BaseSurrealEdge`, import `TableType`.
