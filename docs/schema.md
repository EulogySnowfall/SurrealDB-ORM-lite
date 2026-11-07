# Schema and DDL

Schema introspection, materialized views and the DDL of a model's table.

> **The embedded engine since v0.50.0** is a SurrealDB 4.0 core (SDK 3.0), and the ORM treats it as a 4.x
> server. Where a passage below groups "SurrealDB 2.x and the embedded engine", it describes the SDK 2.0 engine
> of `0.40.x` (a 2.3.10 core); the [embedded table](compatibility.md#orm-behaviour-the-embedded-engine) lists what the
> engine does now.

## 31. Schema introspection (`DatabaseIntrospector`, `generate_models_from_db()`)

Since v0.30.0 the ORM reads the schema of a database — its tables with their fields, indexes and events, and its
accesses, analyzers, functions and params — as immutable typed objects, and generates `BaseSurrealModel` classes from
it, as the full SurrealDB-ORM does. It only reads: it sends `INFO FOR DB` and `INFO FOR TABLE`, never a statement that
reads a record or changes the schema, so it needs a session allowed to run `INFO`: a system user
([Permissions](#permissions)). Kinds, field paths and the generated types are the same on SurrealDB 2.x, 3.x and the
embedded engine; expressions (`DEFAULT`, `VALUE`, `ASSERT` …) are kept as each line renders them
([SurrealDB 2.x and 3.x](#surrealdb-2x-and-3x)). The examples of this section run on the embedded engine (`mem://`),
with nothing to start.

### Reading the schema

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

### Generating models

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
  [section 34](relations.md#34-relation-constraints-relation_in-define_relation)), the table's clauses —
  `table_type=TableType.ANY` for a `TYPE ANY` node table, `schema_mode=SchemaMode.SCHEMAFULL`, `changefeed=`,
  `permissions={…}` and `comment=` (since v0.34.0, [section 35](#35-model-table-ddl-define_table-schema_mode-permissions)) —, `connection=`
  when the schema was read from another connection than `"default"`, then `server_fields` and `extra="allow"`
  (below). A model declares
  `id: str | None = None` first (`id: RecordID | None = None` for a view: a grouped view's ids are arrays), then one
  line per field, in codepoint order of the columns. A `SCHEMAFULL` relation table with data fields gives an edge model
  with `schema_mode=SchemaMode.SCHEMAFULL` (since v0.34.0): apply it with `define_table()`, which defines those fields,
  not `define_relation()`, which raises `SurrealDbError` before any request
  ([section 34](relations.md#34-relation-constraints-relation_in-define_relation)).
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

### How SurrealDB kinds become fields

| SurrealDB kind                                                     | Generated annotation                                                                                                                                                     |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `any`, or no `TYPE`                                                | `Any = None`                                                                                                                                                             |
| `bool`, `int`, `float`, `string`, `bytes`                          | `bool`, `int`, `float`, `str`, `bytes`                                                                                                                                   |
| `number`                                                           | `int`, `float` or `Decimal`: their union, so each value keeps its type                                                                                                   |
| `decimal`, `datetime`, `uuid`                                      | `Decimal`, `datetime`, `UUID`                                                                                                                                            |
| `duration`                                                         | `Duration`, nanosecond-exact ([section 24](fields.md#24-rich-field-types))                                                                                               |
| `range`                                                            | `Range`                                                                                                                                                                  |
| `record`, `record<…>`                                              | `RecordID`                                                                                                                                                               |
| `geometry<point>`, or another single geometry kind                 | `GeometryPoint` …; optional: `PointField = None` … ([section 25](fields.md#25-geospatial-fields))                                                                        |
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
  ([section 20](models.md#20-field-aliases-server_fields-and-mergerefreshfalse)): omitted on create, so that the server's
  default applies, and read back after `save()`. A value you set on such a field before `save()` is not written: set
  it with `merge()` once the record exists.
- **`VALUE` and `COMPUTED`**: `COMPUTED e` (SurrealDB 3.x), and a `VALUE e` that reads neither `$value` nor
  `$input`, become `Computed[T] = computed("e")` ([section 16](models.md#16-computed-fields-computed--define-field--value)):
  never written, read back after `save()`. When `computed()` refuses the expression (a `;`, even inside a string, or
  a block `{ … }`) or the column needs an alias, the field takes the server form instead: `T | None = None`, listed in
  `server_fields`. A `VALUE` that reads `$value` (`string::lowercase($value)`) is an ordinary field: you write the
  value and the server transforms it.
- **`SCHEMALESS` tables** get `extra="allow"`: the fields the table does not declare are read into `model_extra` and
  written back.
- **Aliases**: a column that cannot be an attribute name as it is gets one, with `Field(alias=<column>)`
  ([section 20](models.md#20-field-aliases-server_fields-and-mergerefreshfalse)): `first-name` → `first_name`, `class` →
  `class_`, `_private` → `private`, `9lives` → `f_9lives`. So does a column named like a type or a name the module
  uses (`datetime`, `computed`, `list`, `str`, `Field`, `Any`), like a model attribute (`save`, `objects`, `json`), like
  a class of the module, or `l`, `O` or `I` (ambiguous names for linters): it gets a `_` suffix (`datetime_`, with
  `alias="datetime"`), since the attribute would otherwise shadow that name in the annotations after it.
- **Skipped tables**, listed in the module's header: a table whose name is not an identifier (`order-items`:
  `table_name` refuses it), and a table named like a SurrealQL keyword, in any case, which the ORM's statements cannot
  use as a table name (`select`, `Select`, `create`, `only`, `none` …). They are in `schema.tables` all the same.

### What the generator does not do

- It reads no record: a `SCHEMALESS` table's model holds the fields the table declares (with `extra="allow"`), and an
  object-literal kind (`{ a: int }`) stays a `dict[str, Any]`.
- A field's `ASSERT`, `PERMISSIONS`, `REFERENCE` (the field is a `RecordID`) and comment are not generated: the
  database still applies them, and `define_table()` keeps them. `READONLY` is a trailing `# READONLY` comment: no
  field option exists for it.
- `update()` may fail on an instance holding a `READONLY` datetime below the top level, in a sub-model (`address.at`),
  a `tuple`, an array (`array<datetime>`, `items.*.at`) or a set, after `get()` as after `save()`: Python keeps
  microseconds, SurrealDB nanoseconds, and the server refuses the changed value. `update()` keeps the top-level
  `READONLY` datetimes to the nanosecond, up to 32 per record; below the top level it keeps one only while the fields
  before it leave room, which depends on the data (see [Precision and `Decimal`](fields.md#precision-and-decimal)). The
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

### SurrealDB 2.x and 3.x

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
  ([ORM behaviour: the embedded engine](compatibility.md#orm-behaviour-the-embedded-engine)).

### Permissions

Introspection needs a system user: root, a namespace user or a database user, any role. A record user or an
anonymous session gets `SurrealDbAuthenticationError` ("this session may not run INFO FOR DB"); on the embedded
engine the anonymous session owns the database and may read it.

The `users` section is never read. On SurrealDB before 3.2.2 (every 2.x release, 3.x before 3.2.2, and the embedded
engine of SDK 2.0.0), `INFO FOR DB` shows user password hashes to every role that may run `INFO`
([SurrealDB's `INFO` documentation](https://surrealdb.com/docs/surrealql/statements/info)); introspection never
reads, stores, logs or writes them.

### Coming from the full SurrealDB-ORM

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
- **`SchemaState.diff()`, `schema_diff()` and `applied_migrations`** come with the migrations ([section 39](migrations.md#39-migrations-makemigrations-migrationexecutor-operations)):
  `applied_migrations` is filled from the tracking table when the database has one (full ORM: declared, never
  filled; it is the 2nd attribute, positional as in the full ORM); the `inspectdb` command is in
  [section 40](cli.md#40-command-line-surreal-orm-lite).
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

## 33. Materialized views (`view_query`, `define_view()`)

Since v0.32.0 a model can read a **materialized view**: a table SurrealDB computes from a `SELECT` and keeps up to
date as its source table changes — counts, sums, filtered copies. The view is declared on the model with
`view_query`, applied idempotently with `define_view()` and read like any model, and every write of the model is
refused: SurrealDB computes the view's records itself. The same code runs on SurrealDB 2.x, 3.x and the embedded
engine, except where the [2.x/3.x/4.x table](compatibility.md#orm-behaviour-surrealdb-2x-3x-and-4x) says otherwise. The examples assume a
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

### Declaring a view

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

### Applying and removing

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

### Reading

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

### Writes are refused

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

### Transactions, connections and the query cache

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
- The query cache ([section 29](querying.md#29-query-cache-cache-querycache)): reads of a view model, and reads whose subqueries
  read one, are dropped by every ORM write, since a write to a source table changes the view and the ORM does not know
  which tables a view reads. `define_view()` and `remove_view()` drop cached reads like every write. Writes by raw
  SurrealQL or by another process are not seen, as for every table.
- Defining and removing need a system user allowed to define: root, or a namespace or database `OWNER` or `EDITOR`.
  On a server, a `VIEWER` or a record user gets `SurrealDbAuthenticationError`.
- Applying DDL fires no signal, and neither do the records SurrealDB computes for a view.

### SurrealDB 2.x and 3.x

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
- The [2.x/3.x/4.x table](compatibility.md#orm-behaviour-surrealdb-2x-3x-and-4x) lists these differences; the embedded engine behaves as a
  2.x server.

### Security

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

### Coming from the full SurrealDB-ORM

- **`view_query` is the same key** (`SurrealConfigDict(view_query="SELECT …")`); `get_view_query()` is added and
  `_is_view()` kept.
- **`TypeError` with the same first words** (`Cannot modify materialized view '<table>'`), on every write path above
  (full ORM: `save()`, `update()`, `merge()`, `delete()` only).
- **`define_view()`/`remove_view()`/`view_ddl()`** apply, remove and render the view, where the full ORM applies one
  through migrations (`CreateTable`/`AlterTable`) or `define_table()`. `define_table()` (since v0.34.0,
  [section 35](#35-model-table-ddl-define_table-schema_mode-permissions)) accepts a view model and applies it through `define_view()`, so
  full-ORM code calling `define_table()` on a view works. Migrations (`CreateTable`/`AlterTable` with `view_query`)
  are in [section 39](migrations.md#39-migrations-makemigrations-migrationexecutor-operations).
- **`AS (…)` as in the full ORM, the table name quoted** (``DEFINE TABLE OVERWRITE `order_stats` AS (…)``).
- **Generated models write `view_query=`**, as the full ORM's do
  ([section 31](#31-schema-introspection-databaseintrospector-generate_models_from_db)).
- **The documented `MaterializedView` base class with `source_query` is not shipped by the full ORM either**: declare
  `view_query` on a `BaseSurrealModel`.

## 35. Model table DDL (`define_table()`, `schema_mode`, `permissions`)

Since v0.34.0 a model declares its table's schema mode, change feed, permissions and comment with the full
SurrealDB-ORM's keys — `schema_mode`, `changefeed`, `permissions`, `comment` and `flexible_fields` — and
`define_table()` writes its `DEFINE TABLE` and one `DEFINE FIELD` per column from the annotations and applies them,
keeping what the model does not declare. The same code runs on SurrealDB 2.x, 3.x and the embedded engine, except
where the [2.x/3.x/4.x table](compatibility.md#orm-behaviour-surrealdb-2x-3x-and-4x) says otherwise. The examples assume a configured
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

### Declaring the table

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

### Field types

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
name still undefined gives `any`, and the attempt is remembered: the ORM tries again only once a name the model's
string annotations mention changes — those of its fields and the return annotations of its `@computed_field`s and
serializers (`@field_serializer`, `@model_serializer`), which Pydantic evaluates too — (in the calling function's local
names or in the model's module: added, removed or bound to another object), or a linked model of another module, still
incomplete, completes or sees a name its own annotations mention change — a link reached through a type alias
(`type BA = B | None`) or the field of a dataclass or a `TypedDict` included. Other names do not count, so a name
imported only under `TYPE_CHECKING` costs one `model_rebuild()`, not one per call, even from a loop.

- **Columns**: every field but `id` (and an edge's `in_` and `out`), under its column — its alias, if any
  ([section 20](models.md#20-field-aliases-server_fields-and-mergerefreshfalse)) —, then the Pydantic `@computed_field`s; a
  `Field(exclude=True)` field is left out. A sub-model's fields follow its column (`address.city`, `subs.*.city`), its
  `@computed_field`s included; a sub-model with `extra="allow"` keeps its columns and is free-form too.
- **Optional**: a column is `option<…>` when its annotation admits `None` or its default is `None` (`x: int = None`),
  never `any`, which admits `NONE` already.
- **`DEFAULT`**: the constant default of a column that is not optional — a `bool`, `int`, `float`, `Decimal`, `str`,
  `datetime`, `date`, `timedelta`, `Duration`, `UUID` or enum member — is its `DEFAULT` (`DEFAULT 18`,
  `DEFAULT 'free'`, `DEFAULT 1.50dec`). An optional column gets none: SurrealDB applies `DEFAULT` to a `NONE` on every
  create, so a `None` the ORM writes would become the default. A container, a record, a geometry, a range and a
  `default_factory` get none either: the ORM always sends them.
- **`server_fields`** ([section 20](models.md#20-field-aliases-server_fields-and-mergerefreshfalse)): the database's `DEFAULT`,
  `VALUE` or `COMPUTED` on the column is kept, and the model's constant default is written `DEFAULT` only where the
  database has none, since a `save()` leaves a server field out. A `server_fields` column that is not nullable and has
  no constant default (a `default_factory`, or a required field) is written `TYPE <kind>` with no `DEFAULT`: every
  create — a `save()` that creates the record, `objects().bulk_create()`, `signup()` — leaves it out, so SurrealDB
  refuses it until the database gives the column a `DEFAULT` or `VALUE`
  (`DEFINE FIELD OVERWRITE created ON note TYPE datetime DEFAULT time::now()`), which `define_table()` then keeps; or
  annotate it `T | None`.
- **Computed fields** ([section 16](models.md#16-computed-fields-computed--define-field--value)): `Computed[T]` is
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

### Applying it

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

| Field clause                                                | Written from the model                                                                                                                          | Kept from the database                                                |
| ----------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| `TYPE`                                                      | always                                                                                                                                          | —                                                                     |
| `FLEXIBLE`                                                  | as in "Field types"                                                                                                                             | a stored one, on a kind that holds an object                          |
| `DEFAULT`                                                   | a constant default of a column that is not optional (a `server_fields` column: only where the database has no `DEFAULT`, `VALUE` or `COMPUTED`) | when the model writes none; a stored `ALWAYS` even when it writes one |
| `VALUE`, `COMPUTED`                                         | a computed field's expression: `COMPUTED` over a stored `COMPUTED`, else `VALUE`                                                                | when the model writes none                                            |
| `REFERENCE`, `READONLY`, `ASSERT`, `PERMISSIONS`, `COMMENT` | —                                                                                                                                               | always                                                                |

- Nothing is removed: the table keeps its records, the fields, clauses, indexes and events the model does not declare,
  the views that read it ([section 33](#33-materialized-views-view_query-define_view)), its live queries and its
  change feed's history. A key left out never removes a stored clause, and the migrations' diff leaves it too
  ([section 39](migrations.md#39-migrations-makemigrations-migrationexecutor-operations)): to remove a clause the model no longer declares, write an `AlterTable` or
  `AlterField` migration by hand; a field the model no longer declares is removed by the `DropField` that
  `makemigrations(drop_missing=True)` writes. `schema_mode=SchemaMode.SCHEMALESS` turns a `SCHEMAFULL` table back,
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

### Views and edges

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

### User models

`define_access()` ([section 19](authentication.md#19-model-level-authentication-authenticatedusermixin)) and `define_table()` read the
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

### What SurrealDB checks

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

### SurrealDB 2.x and 3.x

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
- The [2.x/3.x/4.x table](compatibility.md#orm-behaviour-surrealdb-2x-3x-and-4x) and the
  [embedded engine's](compatibility.md#orm-behaviour-the-embedded-engine) list these rows.

### Transactions, connections and the cache

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
- The query cache ([section 29](querying.md#29-query-cache-cache-querycache)): `define_table()` drops the model's cached reads,
  as every write does.
- The table's events ([section 32](functions-and-events.md#32-server-side-events-and-stored-functions-event-define_events-define_function))
  are kept: `define_events()` runs before or after. `define_computed_fields()` still writes `VALUE` alone and resets
  the field's other clauses: prefer `define_table()`.
- Defining needs a system user allowed to define: root, or a namespace or database `OWNER` or `EDITOR`. A session that
  may not read the definitions or define the table gets `SurrealDbAuthenticationError` (a record user, at the first
  read). Applying DDL fires no signal.

### Security

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

### Coming from the full SurrealDB-ORM

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
  `surreal_orm.migrations`) with the SQL in its message; this library's `MigrationStatementError`
  ([section 39](migrations.md#39-migrations-makemigrations-migrationexecutor-operations)) is raised by the migrations only, and `define_table()` keeps `SurrealDbError`.
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
