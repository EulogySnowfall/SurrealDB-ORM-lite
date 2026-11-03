# Models

Primary keys, table names, values the server computes, field aliases and partial reads.

## Custom Primary Key

```python
from surreal_orm_lite import BaseSurrealModel, SurrealConfigDict

class Product(BaseSurrealModel):
    model_config = SurrealConfigDict(primary_key="sku")

    sku: str
    name: str
    price: float
```

## Custom table name (`table_name`)

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

## 15. Server-side values: `SurrealFunc` + `server_values=`

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
member is tested against SurrealDB 2.7.0 and 3.3.5**:

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

## 16. Computed fields: `Computed[...]` → `DEFINE FIELD … VALUE`

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

Since v0.34.0, `define_table()` ([section 35](schema.md#35-model-table-ddl-define_table-schema_mode-permissions)) writes a computed field's `TYPE` too
(`TYPE option<T> VALUE …`), checks its expression, and keeps a field the database defines `COMPUTED` (SurrealDB 3.x)
`COMPUTED`; `define_computed_fields()` turns such a field into `VALUE`.

## 20. Field aliases, `server_fields` and `merge(refresh=False)`

Three developer-experience features (v0.18.0) that let a model's Python surface differ from its
SurrealDB column surface, and let a caller skip work they do not need. All three behave
**identically on SurrealDB 2.6.x and 3.x**.

### Field aliases

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

### `server_fields`

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

### `merge(refresh=False)`

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
