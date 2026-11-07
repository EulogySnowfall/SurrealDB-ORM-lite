# Search

Full-text, vector and hybrid search.

> **The embedded engine since v0.50.0** is a SurrealDB 4.0 core (SDK 3.0), and the ORM treats it as a 4.x
> server. Where a passage below groups "SurrealDB 2.x and the embedded engine", it describes the SDK 2.0 engine
> of `0.40.x` (a 2.3.10 core); the [embedded table](compatibility.md#orm-behaviour-the-embedded-engine) lists what the
> engine does now.

## 36. Full-text search (`search()`, `SearchScore`, `SearchHighlight`, `Analyzer`, `Index`)

Since v0.35.0 a queryset searches text with the full SurrealDB-ORM's API — `search()`, with `SearchScore` and
`SearchHighlight` in `annotate()` — plus `SearchOffsets`, over the full-text indexes a model declares with `Analyzer`
and `Index` and applies with `define_indexes()`. SurrealDB tokenises, matches, scores (BM25) and highlights on the
server; the ORM binds every search text and highlight tag as a query variable. The same code runs on SurrealDB 2.x,
3.x and the embedded engine, except where the [2.x/3.x/4.x table](compatibility.md#orm-behaviour-surrealdb-2x-3x-and-4x) says otherwise. The
examples assume a configured connection: they run as written, in order, on the embedded engine (`mem://`).

### Declare and apply

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
print(applied == Article.index_ddl())  # the embedded engine is a SurrealDB 4.0 core: FULLTEXT
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
([section 29](querying.md#29-query-cache-cache-querycache)), `remove_index()` the model's.

Call `define_table()` (or `define_relation()` for an edge model) before `define_indexes()`: on a table that does not
exist yet, SurrealDB's `DEFINE INDEX` creates it `TYPE ANY SCHEMALESS PERMISSIONS NONE`, which `define_table()`
replaces but `define_table(overwrite=False)` leaves as it is.

### Search, score, highlight

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
# a2 1.074 Cooking with <b>physics</b> {'0': [{'e': 20, 's': 13}]}
# a1 0.952 Quantum <b>physics</b> for beginners {'0': [{'e': 15, 's': 8}]}
```

`search()` applies wherever `filter()` does, AND-ed after `filter()`, `Q` and `nearby()`: `count()`, `exists()`, the
aggregations, `values()` groups, `bulk_update()`, `bulk_delete()`, `patch()`, and a `Subquery` of the queryset
([section 28](querying.md#28-subqueries-subquery)), whose references start again at 0. `get(<id>)` and `all()` ignore it, as they
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

### What SurrealDB decides

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

### Changing an analyzer

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

### Transactions

- `define_indexes(tx=…)` reads first — committed state, outside the transaction — then its index statements join it
  ([section 11](writing.md#11-transactions-atomic-all-or-nothing)): applied at once in an interactive transaction (WebSocket,
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
  `Field(exclude=True)` field SIGNUP writes, a `tx=` of another connection, one of its arguments (a missing id, invalid
  patch operations, an invalid `server_values`/`extra_vars` pair, a computed field or an edge endpoint among the fields
  written, an atomic helper's field, amount or values, a subquery value in `bulk_update()`, a snapshot in
  `bulk_create()`'s list), or while the write is built: `upsert()` without an id, `save(tx=…)` without an id in a
  buffered transaction, a field name or an `extra_vars` key the statement cannot take, the checks of `relate()`,
  `remove_relation()` and `remove_all_relations()` (an invalid edge name, an empty list of edges, an unsaved record, an
  invalid direction or target), a field or a filter refused while a QuerySet write is compiled, an edge id of another
  table, a `field_serializer` that raises, and a value the ORM cannot send in any write (a field, a filter, a patch
  operation, an atomic helper's value, a relation's data, `extra_vars`), such as one of the
  [values refused before sending](fields.md#values-refused-before-sending); a raw `tx.add()` is not tracked. A write counts once
  it reached the database: when it raises after its statement was queued or sent, or after it, or one of its `pre_*`
  handlers, asked the connection for a client, its table stays counted.
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

### Live queries

- `live()` and `watch()` refuse `search()` — and a search annotation — before any request (`SurrealDbError`):
  SurrealDB accepts a live query whose condition holds a full-text match, but never notifies it
  ([section 21](realtime.md#21-live-queries-real-time-notifications)).
- Search in a subquery instead:
  `Article.objects().filter(id__in=Subquery(Article.objects().search(title="physics"))).live()` notifies the records
  created or updated to match — not a deletion, nor an update that makes a record stop matching.
- Before a live query whose subquery searches, the ORM checks that each searched field has a full-text index and
  raises `SurrealDbError` naming the field otherwise, before the live query starts: without the index, SurrealDB 3.x
  would accept the live query and never notify. Do not remove that index while the live query runs.

### Not supported

- `as_of()` ([section 27](querying.md#27-time-travel-as_of)): `search()` with `as_of()` raises `SurrealDbError` before any
  request, in either order and in a subquery under it. The index holds the records' current text, so a time-travel
  read would select records by their text now.
- `changes()` ([section 22](realtime.md#22-change-feeds-changes)): a change feed refuses `search()` and the search annotations
  (`SurrealDbError`), and a `Subquery` refuses the search annotations.
- Indexes on materialized views ([section 33](schema.md#33-materialized-views-view_query-define_view)), refused with
  `ValueError` when the class is defined. A materialized view model cannot declare `indexes=`: SurrealDB 3.x does not
  index the records a view gains after the index is defined, and `define_view()` drops the indexes of the view's table.
- `@AND@`/`@OR@`: SurrealDB 3.x's boolean match operators are not exposed. The terms of one text, and the fields of
  `search()`, are AND-ed; there is no OR of two matches.

### Coming from the full SurrealDB-ORM

- **Declared on the model**: `Analyzer` and `Index` take the argument names of the full ORM's `DefineAnalyzer` and
  `CreateIndex`, a model lists its indexes in `SurrealConfigDict(indexes=[...])` and `define_indexes()` applies them;
  the full ORM declares analyzers and indexes in migrations, which this library also has (`DefineAnalyzer`,
  `CreateIndex`, [section 39](migrations.md#39-migrations-makemigrations-migrationexecutor-operations)).
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

## 37. Vector search (`similar_to()`, `VectorField`, `Index(hnsw=True)`)

Since v0.36.0 a queryset finds the records whose vector is nearest to a query vector with the full SurrealDB-ORM's
API — `similar_to()`, each record's distance set as `_knn_distance` — over the vector fields a model declares with
`VectorField` and the HNSW indexes it declares with `Index(hnsw=True)` and applies with `define_indexes()`. SurrealDB
searches the index and computes the distances on the server; the ORM binds the query vector as a query variable. The
same code runs on SurrealDB 2.x, 3.x and the embedded engine, except where the
[2.x/3.x/4.x table](compatibility.md#orm-behaviour-surrealdb-2x-3x-and-4x) says otherwise. The examples assume a configured connection: they
run as written, in order, on the embedded engine (`mem://`).

### Declare and apply

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
print(applied == Document.index_ddl())  # the embedded engine is a SurrealDB 4.0 core: FULLTEXT
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

### Type checkers

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

### Find the nearest

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

### With other conditions

`similar_to()` applies wherever `filter()` does, AND-ed with `filter()`, `Q`, `nearby()`, `search()` and a
`Subquery` filter: `exec()`, `first()`, `get()` without an id, `count()`, `exists()`, the aggregations, `values()`
groups, `bulk_update()`, `bulk_delete()`, `patch()`, and a `Subquery` of the queryset
([section 28](querying.md#28-subqueries-subquery)). `get(<id>)` and `all()` ignore it, as they ignore `filter()`. The bulk
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

On SurrealDB 3.x, 4.x and the embedded engine, search annotations (`SearchScore`, `SearchHighlight`,
`SearchOffsets`) give each of the nearest matches the score, highlight and offsets that the same `search()` gives it
without `similar_to()`. On SurrealDB 2.x the ORM refuses them before the statement is sent, since there it selects the
matches first and the vector search holds no match to score, highlight or locate; `count()`, `exists()` and the
aggregations, which return no annotation, still answer. The examples run on the embedded engine, a SurrealDB 4.0 core
since v0.50.0, so this one prints the annotations; on 2.x it prints the refusal.

```python
programming = Document.objects().search(title="programming").similar_to("embedding", [0.0, 1.0, 0.0], 2)
annotated = programming.annotate(score=SearchScore(0), snippet=SearchHighlight("<b>", "</b>", 0))
try:
    for document in await annotated.exec():
        print(document.title, document.snippet, round(document.score, 4), round(document._knn_distance, 4))
except SurrealDbError as exc:  # SurrealDB 2.x: the ORM refuses the annotations
    print(exc)
# Rust programming Rust <b>programming</b> 1.0935 0.0
# Python programming Python <b>programming</b> 1.0935 0.0061
print(await annotated.count(), await annotated.exists())
# 2 True
```

### What SurrealDB decides

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

### Not supported

- Search annotations with `similar_to()` on SurrealDB 2.x and the embedded engine, for the reads that return records
  (`exec()`, `first()`, `get()` without an id): `SurrealDbError` before the statement is sent. `search()` as a
  condition stays available there, and `count()`, `exists()` and the aggregations answer. `hybrid_search()` gives each
  record found by both searches its BM25 score and its distance, on every line.
- `as_of()` ([section 27](querying.md#27-time-travel-as_of)): `similar_to()` with `as_of()` raises `SurrealDbError` before any
  request, in either order and in a subquery under it. The index holds the records' current vectors, so a time-travel
  read would pick its neighbours by their vectors now.
- `live()`/`watch()` ([section 21](realtime.md#21-live-queries-real-time-notifications)) refuse `similar_to()`, and a `Subquery`
  holding it, before any request: SurrealDB accepts a live query whose condition holds the vector search but never
  notifies it. Read the nearest records with `similar_to()` and watch them by id.
- `changes()` ([section 22](realtime.md#22-change-feeds-changes)): a change feed refuses `similar_to()` (`SurrealDbError`).
- `MTREE` indexes (SurrealDB 3.x refuses them; one stored under a declared index's name is redefined `HNSW`), the
  `MINKOWSKI`, `JACCARD` and `PEARSON` distances, `F16` vectors and `DISKANN` indexes, and `concurrently`.

### Coming from the full SurrealDB-ORM

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

## 38. Hybrid search (`hybrid_search()`, Reciprocal Rank Fusion)

Since v0.37.0 a queryset fuses two rankings of its records with the full SurrealDB-ORM's `hybrid_search()`: the
records nearest to a query vector, as `similar_to()` finds them
([section 37](#37-vector-search-similar_to-vectorfield-indexhnswtrue)), and the best matches of a text, as `search()`
ranks them by BM25 score ([section 36](#36-full-text-search-search-searchscore-searchhighlight-analyzer-index)), are
fused into one ranking by Reciprocal Rank Fusion. SurrealDB 3.x fuses the two lists with its `search::rrf()`;
SurrealDB 2.x and the embedded engine have no such function, so the ORM fuses them with the same formula: the same
two lists give the same records, scores and order on every line. Each record comes back as a dict, as in the full
ORM. The examples assume a configured connection: they run as written, in order, on the embedded engine (`mem://`).

### Search

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

### With the queryset

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

### What SurrealDB decides

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
  ([2.x/3.x/4.x table](compatibility.md#orm-behaviour-surrealdb-2x-3x-and-4x)), so the full-text list, and the fused ranking with it, can
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

### Not supported

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

### Coming from the full SurrealDB-ORM

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
