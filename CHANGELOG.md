# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.37.2] - 2026-10-27

A model with a `primary_key` can have its access defined on SurrealDB 3.x. No new public API.

### Fixed

- **`access_ddl()` and `define_access()` work for a model with a `primary_key` on SurrealDB 3.x** (#338).
  The SIGNUP clause built the record id with `type::thing('<table>', $<key>)`, which 3.x refuses at
  `DEFINE`, so `define_access()` failed there; `type::record(table, key)` is no replacement, since 2.x
  reads it as a type check that fails every signup. SIGNUP now sets the id from the bound key,
  `CREATE <table> SET id = $<key>, ...`, a statement accepted by 3.x, 2.x and the embedded engine
  that gives the record id `<table>:<key>` on all of them. `access_ddl()` stays pure, with no
  version parameter, and a model without a `primary_key` is unchanged.
- **A signup leaving the `primary_key` as `None` is refused before the exchange.** `SET id = NONE`
  would have the server mint a random record id, one no `objects().get(<key>)` addresses; the
  `ValueError` names the key.
- **`parse_define_access()` reads the new form**: the `id = $<key>` item is one of `signup_fields`.
  The older `type::thing(...)` and `type::record(...)` targets are still read.

### Notes

- **Upgrading**: re-run `define_access()` for such a model to replace the old SIGNUP clause. Accesses
  already defined on 2.x keep working; on 2.x the old clause and the new one create the same records.

## [0.37.1] - 2026-10-24

Follow-ups to the review of v0.36.3 (#327): a `Literal[None]` member of a vector field's union
reads as `None`, and a vector field declared through a forward reference gets the vector checks
of the `class` statement once its name resolves. Any model a forward reference left incomplete is
completed before the ORM writes its DDL or builds a queryset. No new public API.

### Fixed

- **`VectorField[d] | Literal[None]` is read as a vector field** (#327). Since v0.36.3 a
  `Literal[None]` union member counted as a type other than `None`: `get_vector_info()` and
  `is_vector_field()` raised `ValueError` for that annotation and the `class` statement refused
  the field, although `Literal[None]` admits only `None` and `define_table()` already read it as
  `None`. Such a member now counts as `None`: the annotation gives `(d, 'F32')` wherever
  `VectorField[d] | None` does (`get_vector_info()`, `is_vector_field()`, the `class`-statement
  checks of a vector index, `similar_to()`, and `define_table()` with
  `option<array<float, d>>`). A `Literal` holding another value (`Literal[None, "x"]`) next to a
  marker is still refused.
- **A vector field declared through a forward reference is checked once its name resolves**
  (#327). A field annotated with a name defined after the class (`e: "Later" = None`, then
  `Later = VectorField[3]`) has no `Vector` marker at the `class` statement, so since v0.36.0 the
  vector checks skipped it: a vector index of another dimension or storage type, or an ambiguous
  annotation such as `Later = VectorField[3] | str`, was accepted once `model_rebuild()` or the
  model's first use resolved the name. Until then, `table_ddl()` and `define_table()` wrote the
  column as `any`, `define_indexes()` defined the index unchecked and `similar_to()` accepted a
  query vector of any length, without an error. The checks now run again when Pydantic completes
  the model (at `model_rebuild()`, or at its first instantiation, validation or JSON schema), and
  the ORM completes such a model before `table_ddl()`, `define_table()`, `index_ddl()`,
  `define_indexes()`, `get_indexes()` or a queryset (`similar_to()` included) reads it, resolving
  the name as Pydantic's first instantiation does: with the local names of the caller, then the
  names of the model's module (a caller's local name wins, and the field keeps what it resolved
  to). The refusals are those of the field written directly, naming the model, the index and
  the field, raised before anything is sent; a refused model stays incomplete, so each of these
  calls raises the same error again. A matching index is accepted, and `similar_to()` checks the
  query vector against the field's dimension. A name still undefined leaves the model incomplete
  and read as before. A model complete at its `class` statement is checked there only, in the same
  order and with the same messages.
- **Every model a forward reference left incomplete is completed before the ORM reads it**
  (#327). The completion of the previous bullet applies to any model Pydantic could not complete
  at its `class` statement, with or without a vector field: `table_ddl()`, `define_table()`,
  `index_ddl()`, `define_indexes()`, `get_indexes()` and every queryset complete it first. Until
  now such a model was read with its unresolved fields as `any` until `model_rebuild()` or its
  first instance: in `Author` with `best: "Book | None" = None` and `score: "Score" = 0`, `Book`
  and `Score = int` defined after it, `table_ddl()` and `define_table()` wrote `best TYPE any`
  and `score TYPE any DEFAULT 0`, and the cross-connection check of a queryset that follows a
  link (`fetch()`) saw no linked model in such a field. Its columns now get the resolved kinds,
  whether or not the model was instantiated first — what these calls already wrote after an
  instance: `best TYPE option<object> FLEXIBLE` and `score TYPE int DEFAULT 0` on a `SCHEMAFULL`
  table —, and its links are resolved for the cross-connection check, without `model_rebuild()`.
  **Upgrading**: `define_table()` on such a model now redefines a column it wrote `any` with its
  kind, and SurrealDB checks the values written to that column against it from then on. A name
  still undefined leaves the model read as before.

### Notes

- **CHANGELOG correction**: the `### Changed` bullet of `## [0.36.3]` said that every spelling
  v0.36.2 read as a vector field gives the same result. A field with two different `Vector`
  markers, which v0.36.2 read by its first marker, is refused since v0.36.3: the sentence now
  reads "Every spelling with a single `Vector` marker that v0.36.2 read as a vector field gives
  the same result."
- **Docs**: README section 37 (Vector search) says that `get_vector_info()` and
  `is_vector_field()` raise `ValueError` for a field or annotation with no single dimension (code
  that scans the fields of a plain Pydantic model has no `class`-statement check, so it gets the
  exception where v0.36.2 returned `False`), that a `Literal[None]` member counts as `None`, and
  which calls check a field declared through a forward reference. Section 35 (Model table DDL)
  says that such a field gets the kind of the type its name resolves to, before or after an
  instance.
- The fixes are in the ORM's model checks, built on Pydantic's `__pydantic_on_complete__` hook
  (available in the supported `pydantic>=2.13.4`): the behaviour is the same on SurrealDB 2.x and
  3.x and on the embedded engine. Server pins unchanged: tested against SurrealDB 3.3.4 and 2.7.0,
  plus 3.1.5 for backward compatibility.

## [0.37.0] - 2026-10-24

Hybrid search: a queryset fuses the records nearest to a query vector, as `similar_to()` finds
them, and the best matches of a text, as `search()` ranks them, into one ranking by Reciprocal
Rank Fusion, with the full SurrealDB-ORM's `hybrid_search()`. SurrealDB 3.x fuses the two lists
with `search::rrf()`; SurrealDB 2.x and the embedded engine have no such function, so the ORM
fuses them with the same formula: the same two lists give the same records, scores and order on
every line. The third and last minor of Tier 3's search phase.

### Added

- **`QuerySet.hybrid_search(*, vector_field, vector, vector_limit=20, text_field, text_query, text_limit=20, rrf_k=60, ef=None, dist=None)`**,
  the full SurrealDB-ORM's method: each record gets 1 / (`rrf_k` + rank) from each list it is in,
  its rank counted from 1, and the records come best fused score first. It returns a list of
  dicts, as the full ORM does, each holding the record's fields keyed by their Python names (as
  `exec()`'s dict rows are), `_rrf_score`, and `_knn_distance` and `_search_score` (`None` for a
  record that only the other search found). The query vector and the text are bound as query
  variables, never written into the statement. **`dist=`** is an addition: an exact scan that
  needs no vector index, as in `similar_to()`. The queryset applies: its conditions select the
  records of both lists, `select()` and `fetch()` apply to both (`id` always read),
  `limit()`/`offset()` page through the fused records (at most `vector_limit + text_limit`), and
  `cache()`, `objects(tx=)` and named connections apply as for `exec()`; the full ORM ignores them.
- README section 38, "Hybrid search (`hybrid_search()`, Reciprocal Rank Fusion)" (search, with the
  queryset, what SurrealDB decides, what is not supported, a note for users of the full
  SurrealDB-ORM), four rows of the "ORM behaviour: SurrealDB 2.x vs 3.x" table and one of the
  embedded engine's, and the example notebook `examples/v0.37.0_hybrid_search.ipynb`.

  ```python
  results = await Passage.objects().filter(topic="tech").hybrid_search(
      vector_field="embedding", vector=[0.0, 1.0, 0.0], vector_limit=4,
      text_field="title", text_query="programming", text_limit=4,
  )
  for row in results:  # best fused score first
      print(row["id"], row["_rrf_score"], row["_knn_distance"], row["_search_score"])
  ```

### Notes

- **SurrealDB 3.x fuses with `search::rrf()`**, given both lists in one statement; **SurrealDB
  2.x and the embedded engine get the same fusion from the ORM**: the same records, scores and
  order for the same two lists. The line is read from `client.version()`, as for `search()`'s
  index keyword. Other conditions apply to both lists; on SurrealDB 2.x and the embedded engine
  the vector list selects the records matching them first, as `similar_to()` does.
- **Equal fused scores come in a fixed order**: the vector list's first, then the full-text list's
  for the records it alone found, on every line (SurrealDB's `search::rrf()` alone returns them in
  no fixed order), so the same two lists always give the same page. Each list is SurrealDB's own:
  BM25 scores depend on the line (a term found in more than half the documents scores `0.0` on 3.x
  and below zero on 2.x and the embedded engine), and on SurrealDB 2.x and the embedded engine an
  exact scan (`dist=`) picks among records at an equal distance at the `vector_limit`-th place in
  no fixed order, as `similar_to()`'s does since v0.36.0: the vector list, and a page of the fused
  records, can then change between two identical calls. The HNSW index, and SurrealDB 3.x's exact
  scan, return the same list on every call.
- **Checked before any request**: both fields, the vector, `vector_limit` and `text_limit` (1 to
  10 000), `rrf_k` (0 to 10 000), `ef` and `dist`; the full ORM checks the field names only.
- **Refused before any request**: `order_by()`, `annotate()`, `values()`, `search()` and
  `similar_to()` on the queryset (`ValueError`), and `as_of()` (`SurrealDbError`: the indexes hold
  the records' current values). A model field stored under one of the seven names the method uses
  for its own values (`rrf_score`, `_rrf_score`, `_knn_distance`, `_search_score`, `_hybrid_pos`,
  `_hybrid_vec_pos`, `_hybrid_text_pos`) raises `ValueError`: store it under another name with
  `Field(alias=...)`. On a SCHEMALESS table, a column with one of these names that the model does
  not declare is not returned, even when `select()` names it.
- **Each search needs its index.** Without a full-text index on `text_field`, SurrealDB 3.3, 2.x
  and the embedded engine refuse the search and `hybrid_search()` raises `SurrealDbError` naming
  the field; SurrealDB 3.1 and 3.2 match nothing, so the vector list alone is fused. Without a
  vector index on `vector_field`, the vector list is empty on SurrealDB 3.3, 2.x and the embedded
  engine, so the full-text ranking alone is fused; SurrealDB 3.1 and 3.2 list the first
  `vector_limit` records by id, with no distance. `dist=` needs no index.
- **Not yet**: `search::linear()` (weights and score normalisation), several full-text fields,
  the full-text side's highlights, and search annotations next to `similar_to()` on SurrealDB 2.x
  and the embedded engine, which the ORM still refuses there; `hybrid_search()` gives each record
  found by both searches its BM25 score and its distance, on every line.
- Server pins unchanged: tested against SurrealDB 3.3.4 and 2.7.0 and on the embedded engine,
  plus 3.1.5 for backward compatibility; the new behaviour rows were also measured on 3.2.6.

## [0.36.5] - 2026-10-24

Follow-up to v0.36.1 (#315): a queryset write that the ORM refuses, and an instance write
refused for one of its arguments, now leave the transaction and the query cache as they were.
No new public API.

### Fixed

- **A refused queryset write no longer marks its table as written** (#320).
  `objects(tx=tx).bulk_create()`, `bulk_update()`, `bulk_delete()` and `patch()` ran their
  materialized-view and `as_of()` refusals, `bulk_create()`'s check of a snapshot in its list,
  `bulk_update()`'s check of a subquery value or a computed field and `patch()`'s check of its
  operations after the ORM had noted the table on the transaction. The refused call queued no
  statement but left the table noted: a caller that caught the error and kept the transaction had
  a later `define_indexes(tx=tx)` on that table refused by the v0.35.3 check (#304), and the
  commit dropped the table's cached reads although nothing was written. Outside a transaction,
  the refused call (a queryset read `as_of()` included, and `delete_table()` on a view or under
  `as_of()`) put the table in flight and dropped its cached reads. These refusals now run before
  anything is noted: the transaction keeps no statement and no table, `define_indexes(tx=tx)` is
  accepted, and no cached read is dropped, with a transaction or without.
- **An instance write refused for one of its arguments no longer marks its table as written**
  (#320). `update()` and `delete()` without an id, `patch()` refused for its operations, a
  computed field or a missing id, `merge()` refused for its fields, `server_values`/`extra_vars`
  or a missing id, `save()` refused for `server_values`/`extra_vars`, the `atomic_*()` helpers
  refused for their amount, field, values or a missing id, and an edge's `save(server_values=…)`
  or `save(extra_vars=…)` had the same effect as above, with `tx=` or without. These checks now
  run right after the model's own refusals (materialized view, snapshot, excluded signup field)
  and before anything is noted. Error types, messages and the refusal order are unchanged: the
  connection, the model's refusals, then the arguments (`update()` and `patch()` still build their
  payload before reading the id). The bookkeeping is client-side: the behaviour is the same on
  SurrealDB 2.x and 3.x and on both transaction strategies.

### Notes

- A few refusals still run after the table is noted (`upsert()` without an id, a buffered
  `save(tx=…)` without an id, the argument checks of `relate()`, `remove_relation()` and
  `remove_all_relations()`, among others); they are tracked in #325.
- **Docs**: the README sections "Snapshots" and "What a time-travel queryset refuses" (27),
  "Writes are refused" (33) and the `define_indexes(tx=…)` note of "Transactions" (36), and the
  docstrings of the affected writes, say that a refused write leaves the transaction and the query
  cache as they were. No behaviour-table row: the change is client-side and identical on both
  lines.
- Server pins unchanged: tested against SurrealDB 3.3.4 and 2.7.0, plus 3.1.5 for backward
  compatibility.

## [0.36.4] - 2026-10-24

Follow-ups to the review of v0.36.2 (#322): an index that `remove_index(tx=…)` removed earlier
in a transaction no longer blocks `remove_analyzer(tx=…)` or `define_analyzer(tx=…)` in the same
block, `define_indexes(tx=…)` no longer defines an index with an analyzer the block removed or
redefined, and the v0.36.2 fix is tested on vector (HNSW) indexes. No new public API.

### Fixed

- **`remove_analyzer(tx=…)` and `define_analyzer(tx=…)` no longer count an index the block
  removed** (#323). Both find the indexes that use the analyzer by reading `INFO FOR TABLE`
  outside the transaction, where an index that `remove_index(tx=…)` removed earlier in the block
  still stands. Once a block had removed the last index using an analyzer, `remove_analyzer(tx=…)`
  was refused on SurrealDB 2.x and the embedded engine ("in use by index …"), and
  `define_analyzer(…, overwrite=True, tx=…)` was refused on every engine as a redefinition with
  indexes to rebuild. Both now leave out the indexes the transaction noted as removed: the
  analyzer can be removed, or redefined without `REBUILD INDEX`, in the same block, on SurrealDB
  2.x and 3.x, over WebSocket and HTTP and on the embedded engine. An index still standing keeps
  the refusal, with the same message: another table's, another index of the same table, one
  removed then defined again in the block, one removed with a raw `tx.add("REMOVE INDEX …")`
  (not tracked), or any index when no `tx=` is given. SurrealDB 3.x already accepted the removal,
  since the server checks it in the transaction. An index queued earlier in a buffered block is
  still not seen.
- **`define_analyzer(tx=…)` after `remove_analyzer(tx=…)` of the same name defines the analyzer
  again** (#323). It compared the analyzer with committed state, where the removed one still
  stood, so an unchanged definition (or any definition without `overwrite=True`) applied nothing
  and the commit left the database without the analyzer. The removed analyzer now counts as
  missing: it is defined again after its removal, with no index read nor rebuilt.

### Changed

- **`define_indexes(tx=…)` refuses an index whose analyzer the block removed or redefined**
  (#323), whether the model declares the analyzer as an `Analyzer` or names it by `str`. Until
  v0.36.3 it read the analyzers from committed state only, so it defined such an index: after
  `remove_analyzer(tx=…)`, the index stood without its analyzer once the block committed, and
  every write to the table was then refused; after `define_analyzer(…, overwrite=True, tx=…)`,
  SurrealDB built the index over the table's records with the committed definition, not the new
  one (on SurrealDB 3.x a search then missed those records, without an error). A removed analyzer
  now counts as missing (an `Analyzer` is refused as one to define in a transaction, a name as one
  that does not exist), and an index whose analyzer the block redefined is refused with a new
  message. Both refusals raise `SurrealDbError` before any DDL and do not fail the block. To
  migrate an index to a new analyzer, call `define_analyzer()` without `tx=` first.

### Notes

- **Tests**: the v0.36.2 fix (#312) is now covered on a vector (HNSW) index: `remove_index(tx=…)`
  then `define_indexes(tx=…)` in one block, over WebSocket, HTTP and the embedded engine, on an
  empty table and on one holding a record. The index stands after the commit and serves
  `similar_to()`; on SurrealDB 2.x, a table holding a record fails the commit with the retryable
  `SurrealDbConflictError` and keeps the index, as the README's v0.36.2 row says.
- **Docs**: README section 36 (Transactions) and the docstrings of `define_indexes()`,
  `remove_index()`, `define_analyzer()` and `remove_analyzer()` describe these cases. No
  behaviour-table row changes: for an index removed with `remove_index(tx=…)`, the analyzer
  checks now give the same result on SurrealDB 2.x and 3.x and on the embedded engine.
- Server pins unchanged: tested against SurrealDB 3.3.4 and 2.7.0 and on the embedded engine,
  plus 3.1.5 for backward compatibility.

## [0.36.3] - 2026-10-24

Follow-up to the review of v0.36.0 (#316): a vector field declared with `| None` keeps its
`Vector` marker, so the checks v0.36.0 added for vector fields apply to it too. No new public API.

### Fixed

- **A vector field written `VectorField[d] | None`, `Optional[VectorField[d]]` or
  `Annotated[list[float], Vector(d)] | None` is read as a vector field** (#318). In these
  spellings the marker sits inside a union member, not in the field's metadata, so since v0.36.0
  `get_vector_info()` returned `None` and `is_vector_field()` `False` for them. The `class`
  statement then skipped the vector-index check: an `Index(..., hnsw=True)` of another dimension
  or storage type than the field was accepted, while `define_table()` still wrote
  `option<array<float, d>>`, so the column and the index disagreed without an error; and
  `similar_to()` took the expected length of the query vector from the index, not the field.
  `get_vector_info()` and `is_vector_field()` now read the field's annotation when its metadata
  holds no marker, as `define_table()` does: these spellings give the same column, the same
  index checks at the `class` statement, the same dimension check in `similar_to()` and the same
  `(dimension, storage type)` as `VectorField[d]`. Vectors held in a container
  (`list[VectorField[d]]`) are still not a vector field.

### Changed

- **A field with no single vector dimension is refused at the `class` statement** (#318): a
  `Vector` marker next to a type other than `None` in a union (`VectorField[3] | str`), or two
  different `Vector` markers on one field (`Annotated[VectorField[3], Vector(4)]`). The
  `ValueError` names the model and the field and says to write `VectorField[d]` alone (it is
  already optional) or `Annotated[list[float] | None, Vector(d)]`; the check runs on every model,
  whether or not it declares indexes. Until v0.36.2 such a model was accepted (the first of two
  markers decided the checks, a union with another type was not a vector field).
  `get_vector_info()` and `is_vector_field()` raise the same `ValueError` for such a field or
  annotation. Every spelling with a single `Vector` marker that v0.36.2 read as a vector field
  gives the same result.

### Notes

- **Docs**: README section 37 (Vector search) says `VectorField[d]` is already optional and
  needs no `| None`: written with it, the field needs `= None`, since Pydantic ignores a default
  inside a union member (with an `UnsupportedFieldAttributeWarning`) and the field is otherwise
  required. It also says how the union spellings are read.
- The fix is in the ORM's model checks: the behaviour is the same on SurrealDB 2.x and 3.x and on
  the embedded engine. Server pins unchanged: tested against SurrealDB 3.3.4 and 2.7.0, plus
  3.1.5 for backward compatibility.

## [0.36.2] - 2026-10-23

Follow-ups to the review of v0.35.3 (#312): an index that `remove_index(tx=…)` removed earlier in
a transaction is defined again by `define_indexes(tx=…)` in the same block instead of being lost
at the commit, and the docs say why the refusal after a write covers every kind of index. No new
public API.

### Fixed

- **`remove_index(tx=…)` then `define_indexes(tx=…)` in one block no longer loses an existing
  index** (#312). `define_indexes(tx=…)` reads `INFO FOR TABLE` outside the transaction, where an
  index removed earlier in the block still stands: it planned nothing, returned `[]`, and the
  commit applied the removal, leaving the table without the index and without an error (since
  v0.35.0, over WebSocket and HTTP and on the embedded engine, on SurrealDB 2.x and 3.x).
  `remove_index(tx=…)` now notes the removal on the transaction, and `define_indexes(tx=…)`
  counts the noted index as missing: it defines it again after the removal
  (`DEFINE INDEX OVERWRITE`, or `IF NOT EXISTS` with `overwrite=False`), so the commit keeps
  it, and a later call in the block compares it with committed state again. Only an index the
  model declares comes back: another model's index on the same table comes back with that
  model's `define_indexes(tx=…)`, and one no model declares stays removed. When the block has
  already written records to the table, the call is refused before any DDL, as since v0.35.3. A
  raw `tx.add("REMOVE INDEX …")` is not tracked.
- **The same fix also applies to vector (HNSW) indexes since v0.36.0**: on v0.36.0 and v0.36.1,
  `remove_index(tx=…)` then `define_indexes(tx=…)` lost an HNSW index at the commit on every
  engine; it is now defined again, as a full-text or `UNIQUE` index is.

### Notes

- **Removing an index and defining it again in one block on SurrealDB 2.x**: on a table that
  held records, the commit fails with a retryable `SurrealDbConflictError` that a retry meets
  again, a 2.x limitation; nothing of the block is committed and the index stands as before. On an
  empty table it commits, as on SurrealDB 3.x and the embedded engine. A row in each behaviour
  table of the README ("ORM behaviour: SurrealDB 2.x vs 3.x" and the embedded engine's) describes
  each engine's outcome, which tests pin.
- **A write after `define_indexes(tx=…)` on SurrealDB 2.x**: the v0.35.3 qualifier "to a table
  that held records" is now pinned: on an empty table, a write after the index in the same block
  commits and is indexed.
- **Docs**: README section 36 (Transactions) and the `define_indexes()` docstring say why the
  refusal after a write covers every kind of index, not only full-text ones: a search would miss
  those records, and a `UNIQUE` index missing them would not enforce uniqueness for them
  ([surrealdb/surrealdb#7618](https://github.com/surrealdb/surrealdb/issues/7618)).
- Server pins unchanged: tested against SurrealDB 3.3.4 and 2.7.0 and on the embedded engine,
  plus 3.1.5 for backward compatibility.

## [0.36.1] - 2026-10-23

Follow-ups to the review of v0.35.4 (#315): an instance write that the model refuses (a
materialized view, a snapshot read with `as_of()`, an auth model with an excluded signup field)
now leaves its transaction exactly as it was, and the docstring of `access_ddl()` lists every call
that refuses an excluded signup field. No new public API.

### Fixed

- **A refused instance write no longer marks its table as written on the transaction** (#315).
  With `tx=`, the model's own refusals ran after the ORM had noted the model's table on the
  transaction. A refused `update(tx=tx)` or `upsert(tx=tx)` (materialized view, snapshot, excluded
  signup field), a `save()`, `merge()`, `patch()`, `atomic_*()` or `delete()` refused on a view
  or a snapshot, or a `relate()`, `remove_relation()` or `remove_all_relations()` refused on a
  snapshot queued no statement but left the table noted. A caller that caught the error and kept using the
  transaction then had a later `define_indexes(tx=tx)` on that table refused by the v0.35.3 check
  (#304), and the commit dropped the table's cached reads although nothing was written. These
  refusals now run right after the connection check and before anything is noted: the transaction
  keeps no statement and no table, `define_indexes(tx=tx)` is accepted, and the refused call
  invalidates no cached read, with a transaction or without. The v0.35.4 entry's "nothing is added
  to the transaction" now holds for the transaction's bookkeeping too. Refusal order (view,
  snapshot, then excluded signup field) and messages are unchanged; the behaviour is the same on
  SurrealDB 2.x and 3.x and on both transaction strategies.

### Changed

- **The atomic helpers and an edge's `save()` refuse a view or a snapshot before checking their
  arguments** (#315). `atomic_append()`, `atomic_remove()`, `atomic_set_add()`,
  `atomic_increment()` and the `atomic_*_many()` helpers checked the field name (`atomic_increment()`
  a duration amount, the `atomic_*_many()` helpers a `values` that is not a list) before the view
  and snapshot refusals: on a view or a snapshot, an invalid argument now raises the view's
  `TypeError` or the snapshot's `SurrealDbError` instead of the argument's error. An edge read with
  `as_of()` raises the snapshot error before the `save(server_values=…)` and `save(extra_vars=…)`
  refusal. Valid calls are unchanged.

### Notes

- **Docs**: the `Raises` section of `access_ddl()` lists every call that refuses an auth model
  with an excluded signup field, as `signup()` and the error message do: `table_ddl()`,
  `define_table()`, `define_access()`, `signup()`, `signin()`, `authenticate()`, `refresh()`,
  `update()` and `upsert()`.
- Server pins unchanged: tested against SurrealDB 3.3.4 and 2.7.0, plus 3.1.5 for backward
  compatibility.

## [0.36.0] - 2026-10-23

Vector search: a queryset finds the records whose vector is nearest to a query vector with the
full SurrealDB-ORM's `similar_to()`, each record's distance set as `_knn_distance`, over the
vector fields a model declares with `VectorField` and the HNSW indexes it declares with
`Index(hnsw=True)` and applies with `define_indexes()`. SurrealDB searches the index and computes
the distances on the server; the ORM binds the query vector as a query variable. The same code
runs on SurrealDB 2.x, 3.x and the embedded engine. The second minor of Tier 3's search phase.

### Added

- **`QuerySet.similar_to(field, vector, limit=10, *, ef=None, dist=None)`**, the full
  SurrealDB-ORM's method: keeps the `limit` records whose `field` vector is nearest to `vector`,
  through the field's HNSW index (SurrealDB's KNN operator). The field is a Python field name (an
  aliased field searches its column) or a dotted path; the vector is a `list` or `tuple` of finite
  numbers, bound, never written into the statement. `exec()`, `first()` and `get()` without an id
  set each record's distance as `_knn_distance`, never written back (`model_dump()`, `save()` and
  `update()` leave it out); without `order_by()` the records come nearest first, and
  `limit()`/`offset()` page through them. A second `similar_to()` replaces the first. **`dist=`**
  is an addition: an exact, brute-force search that needs no index. `similar_to()` applies
  wherever `filter()` does, AND-ed with `filter()`, `Q`, `nearby()`, `search()` and a `Subquery`
  filter: `exec()`, `first()`, `get()` without an id, `count()`, `exists()`, the aggregations,
  `values()` groups, `bulk_update()`, `bulk_delete()`, `patch()` and a `Subquery` (the full ORM
  applies it to `exec()` only). `get(<id>)` and `all()` ignore it, as they ignore `filter()`. With
  `search()`, the records matching the text are ranked by the vector.
- **`VectorField[d]`** (or `VectorField[d, "F64"]`), the full ORM's vector field, **and its typed
  form `Vector`**: `embedding: Annotated[list[float] | None, Vector(1536)] = None`, which type
  checkers accept where they reject `VectorField[1536]`. Both spellings are one field, optional and
  `None` by default: `None` or a `list`/`tuple` of exactly `d` finite real numbers, kept as a
  `list[float]` and checked on every write and every read (a stored vector of another length makes
  `exec()` return its row as a dict), with a fixed-length array JSON schema. `define_table()`
  writes the column `option<array<float, d>>`. `get_vector_info()` and `is_vector_field()`, from
  `surreal_orm_lite.vector`, read either spelling.
- **Vector indexes: `Index(name, field, hnsw=True, dimension=, dist=, vector_type=, efc=, m=)`**,
  with the full ORM's `CreateIndex` arguments: `dimension` (required, 1 to 65535), `dist`
  (`EUCLIDEAN` by default, `COSINE`, `MANHATTAN`, `CHEBYSHEV` or `HAMMING`, in any case),
  `vector_type` (`F64`, `F32` by default, `I64`, `I32` or `I16`), `efc` (1 to 65535, 150 by
  default) and `m` (2 to 127, 12 by default). A vector index covers one field and is listed in
  `SurrealConfigDict(indexes=[...])` next to the full-text ones; its attributes hold what was
  declared. When the class is defined, two vector indexes on one column are refused, and so is a
  vector index whose dimension or storage type differs from its field's `VectorField`.
  `index_ddl()` renders the same `HNSW` statement for SurrealDB 2.x and 3.x; `define_indexes()`
  compares a declared vector index with the stored one and redefines it only when an option, the
  field or the comment changed, so an unchanged vector index is never rebuilt; `remove_index()`
  removes one by name.
- README section 37, "Vector search (`similar_to()`, `VectorField`, `Index(hnsw=True)`)"
  (declaring and applying, type checkers, finding the nearest, other conditions, what SurrealDB
  decides, what is not supported, a note for users of the full SurrealDB-ORM), eight rows of the
  "ORM behaviour: SurrealDB 2.x vs 3.x" table and one of the embedded engine's, and the example
  notebook `examples/v0.36.0_vector_search.ipynb`.

  ```python
  class Document(BaseSurrealModel):
      model_config = SurrealConfigDict(
          table_name="document",
          indexes=[Index("document_embedding", "embedding", hnsw=True, dimension=3, dist="cosine")],
      )

      id: str | None = None
      title: str
      category: str = ""
      embedding: VectorField[3]


  await Document.define_table()
  await Document.define_indexes()  # [] when nothing changed: the vector index is not rebuilt
  pets = Document.objects().filter(category="pets").similar_to("embedding", [1.0, 0.0, 0.0], 3)
  for document in await pets.exec():  # nearest first
      print(document.title, document._knn_distance)
  ```

### Notes

- **`limit` and `ef` go from 1 to 10 000**, checked before any request with the vector's numbers,
  its length (when the model declares the field's dimension, by its `VectorField` or its vector
  index) and the `Index` vector arguments; the full ORM sends any value. **`ef`** (100 by default)
  **is written raised to `limit`** on every line: SurrealDB 2.x would return at most `ef` records.
- **Every HNSW option is written** (`DIST`, `TYPE`, `EFC` and `M`, SurrealDB's defaults when not
  given), so every engine stores the same index: the embedded engine would otherwise store `F64`
  vectors where servers store `F32`. `Index` keeps the options as declared.
- **Other conditions select the records first, then `similar_to()` keeps the nearest among
  them.** SurrealDB 3.x applies them during the index search. SurrealDB 2.x and the embedded engine
  answer too few records, or none, next to many conditions (one on an indexed field among them),
  so there the ORM selects the records matching every other condition — a full-text match
  included — in a `LET` first, which is slower: upgrade to SurrealDB 3.x for large tables.
- **Search annotations combine with `similar_to()` on SurrealDB 3.x; on 2.x and the embedded
  engine the ORM refuses them, since the matches are selected first, while `count()`, `exists()`
  and the aggregations still answer.** The refusal is a `SurrealDbError` from `exec()`, `first()`
  and `get()` without an id, before the statement is sent.
- **`bulk_update()`, `bulk_delete()` and `patch()` of a `similar_to()` queryset write the `limit`
  nearest records by their ids**, selected first, and return their number: they write the records
  the same read returns, on every line. A write holding the KNN operator would act on every
  matching record on SurrealDB 2.x, and on none next to a range on an indexed field on 3.x.
- **HNSW is approximate**: with a condition that keeps few records, a search can return fewer than
  `limit` records, depending on how the index was built, more often on SurrealDB 2.x and the
  embedded engine; `dist=` is exact. Distances are computed in the index's `vector_type` (`F32`,
  single precision, by default), `dist=`'s in double precision.
- **A field without an HNSW index answers no record** (`dist=` needs none). A query vector whose
  length differs from the index's, on a model that does not declare the dimension, raises
  `SurrealDbError` naming the field and both lengths. A zero query vector with `COSINE` raises
  `ValueError`: it has no distance.
- **Refused before any request** (`SurrealDbError`): `similar_to()` with `as_of()`, in either order
  and in a subquery under it (the index holds the records' current vectors); with `live()` and
  `watch()`, in their condition or in a `Subquery`, since SurrealDB accepts a live query whose
  condition holds the vector search but never notifies it — read the nearest records with
  `similar_to()` and watch them by id; and with `changes()`.
- **Moving a table to another dimension** (a new embedding model): declare the field
  `list[float] | None` without its vector index while records of both lengths exist, then the new
  `VectorField[d]` and a matching index; README section 37 gives the steps.
- **SurrealDB 3.1** (tested with 3.1.5 for backward compatibility): a field without a vector index
  answers every record, and `similar_to()` next to `search()` every match, each with no distance.
- **Not yet**: `MTREE` indexes (SurrealDB 3.x refuses them; one stored under a declared index's
  name is redefined `HNSW`), the `MINKOWSKI`, `JACCARD` and `PEARSON` distances, `F16` vectors,
  `DISKANN` indexes, `Index(concurrently=True)`, vector functions in `annotate()` and
  `VectorField` in generated models. `hybrid_search()` comes in v0.37.0.
- **Undeclared keys in list items: fixed in SurrealDB 3.4** (#308): SurrealDB 3.x (measured on
  3.1.5 and 3.3.4) keeps a key a `SCHEMAFULL` table does not define in the items of a list whose
  items define a field, as the 0.35.2 note on #301 below says. SurrealDB 3.4 (nightly at the time
  of writing) refuses it there too, as SurrealDB's documentation of `FLEXIBLE` says. README section
  35 and the 3.x cell of the v0.34.0 row of "ORM behaviour: SurrealDB 2.x vs 3.x" now say so. The
  raw test also measures an object inside a list item that defines no key: refused on 3.x
  (`no such field exists`), emptied on 2.x and the embedded engine. No ORM behaviour changes.
- Server pins unchanged: tested against SurrealDB 3.3.4 and 2.7.0 and on the embedded engine,
  plus 3.1.5 for backward compatibility.

## [0.35.4] - 2026-10-23

`update()`, `upsert()`, `authenticate()` and `refresh()` refuse an auth model whose signup field
is declared `Field(exclude=True)`, as the DDL methods, `signup()` and `signin()` do since v0.35.2
(#310): a record signed up elsewhere and loaded through such a model no longer loses its stored
password hash at its first `update()` or `upsert()`. No new public API.

### Changed

- **`authenticate()`, `refresh()`, `update()` and `upsert()` raise the v0.35.2 `TypeError` for
  an auth model with an excluded signup field** (#310). For a model built on
  `AuthenticatedUserMixin` with a field SIGNUP writes that `Field(exclude=True)` keeps out of
  `model_dump()` (`password: str = Field(exclude=True)`), `update()` and `upsert()` now raise it
  before any request and any `pre_save`/`pre_update` signal, with or without `tx=` (nothing is
  added to the transaction), and `authenticate()` and `refresh()` before any request (a refresh
  token is not spent). The message, `schema.signup_excluded_refused()`, now names every refusing
  call: `table_ddl()`, `define_table()`, `access_ddl()`, `define_access()`, `signup()`,
  `signin()`, `authenticate()`, `refresh()`, `update()` and `upsert()`. Auth models without
  `exclude=True`, and models that are not auth models, write and authenticate as before.

### Fixed

- **A record signed up elsewhere no longer loses its password hash at `update()` or `upsert()`
  of a model with an excluded signup field** (#310). v0.35.2 refused such a model in the DDL
  methods, `signup()` and `signin()`, but not in the calls that drop the value: when the access
  was defined by hand or by an earlier deploy and the account signed up through another client,
  `SurrealDBConnectionManager.signup()` or a session opened before the upgrade, an instance
  returned by `authenticate()` or loaded with `get()` wrote the record back without the hash at
  its first `update()` or `upsert()`, with no error: the hash became `NONE` on a `SCHEMALESS`
  table or an optional column, and every later signin failed. These calls are now refused first
  (see Changed), and the stored record stays as it was. README section 19, "User models" and the
  v0.35.2 row of "ORM behaviour: SurrealDB 2.x vs 3.x" describe it; the behaviour is the same on
  both lines.

## [0.35.3] - 2026-10-23

Follow-ups to the review of v0.35.0's full-text search (#304): the warning of `define_indexes()`
names every index it rebuilds, and `define_indexes(tx=…)` refuses to define an index in a
transaction that has already written records to the model's table, which SurrealDB servers would
leave out of the index. No new public API.

### Changed

- **`define_indexes(tx=…)` raises `SurrealDbError` after an ORM write to the model's table**
  (#304). SurrealDB servers do not index the records a transaction wrote before the index was
  defined: they stay out of it after the commit, and a search misses them. When an index must be
  defined or redefined and the block has already written records to the model's table with the
  ORM (`save()`, `objects(tx=…).bulk_create()`, …), `define_indexes(tx=…)` now raises before any
  DDL, naming the model and the table; the refusal does not fail the transaction. Definitions
  (`define_table(tx=…)`, `define_relation(tx=…)`, `remove_index(tx=…)`, …), a write to another
  table and a call with nothing to apply do not count; a raw `tx.add()` is not tracked. Call
  `define_indexes()` without `tx=` first, then write in the transaction. Earlier releases accepted
  the call and the records written before it were missing from the index on SurrealDB 2.x and 3.x.

### Fixed

- **The analyzer warning of `define_indexes()` names the indexes the call redefines** (#304).
  When one call redefined an analyzer and an index that uses it, the warning counted only the
  indexes rebuilt with `REBUILD INDEX` (`Analyzer 'plain' redefined; 0 index(es) rebuilt:`,
  naming none), although `DEFINE INDEX OVERWRITE` rebuilds the redefined index too. It now names
  every index the call rebuilds, in the order applied.

### Notes

- **Reported upstream**: a SurrealDB 3.x server leaving out of an index the records the same
  transaction wrote before `DEFINE INDEX` is a server defect,
  [surrealdb/surrealdb#7618](https://github.com/surrealdb/surrealdb/issues/7618); 2.x behaves the
  same, and the embedded engine indexes those records. README section 36 (Transactions), a row of
  the "ORM behaviour: SurrealDB 2.x vs 3.x" table and one of the embedded engine's table describe
  each engine's outcome, which tests pin.
- **A write after `define_indexes(tx=…)` on SurrealDB 2.x**: in the same block, a write to a table
  that held records fails the commit with a retryable `SurrealDbConflictError` that a retry meets
  again, a 2.x limitation; SurrealDB 3.x and the embedded engine commit and index it. Prefer
  `define_indexes()` outside the block.
- Server pins unchanged: tested against SurrealDB 3.3.4 and 2.7.0 and on the embedded engine,
  plus 3.1.5 for backward compatibility.

## [0.35.2] - 2026-10-22

An auth model whose signup field is declared `Field(exclude=True)` is refused whatever its schema
mode (#299): v0.34.1 refused it only for a `SCHEMAFULL` table, and on any other table its
`update()` or `upsert()` dropped the stored password hash. No new public API.

### Changed

- **An auth model with an excluded signup field raises `TypeError` whatever its schema mode and
  `with_table`** (#299). For a model built on `AuthenticatedUserMixin` with a field SIGNUP writes
  that `Field(exclude=True)` keeps out of `model_dump()` (`password: str = Field(exclude=True)`),
  `table_ddl()`, `define_table()`, `access_ddl()` and `define_access()` (with or without the
  table) now raise `TypeError` before any request on a `SCHEMALESS` table, with no declared schema
  mode and with `with_table=False`, as v0.34.1 did for a `SCHEMAFULL` table. `signup()` and
  `signin()` raise the same error before any request, so an access defined by hand cannot leave
  such a record either. The message names the model and the fields in declaration order and keeps
  the advice: remove `exclude=True` and leave the field out where you serialise the model instead
  (`model_dump(exclude={"password"})`). A model that relied on `exclude=True` now fails at
  start-up instead of losing data later. Auth models without `exclude=True`, and models that are
  not auth models, render and apply as before.

### Fixed

- **An auth model no longer loses its stored password hash at `update()` or `upsert()`** (#299).
  Both replace the record with what `model_dump()` writes, so an auth model whose signup field was
  declared `Field(exclude=True)` lost the stored value at its first `update()` or `upsert()`, with
  no error, on a `SCHEMALESS` table (a model with neither `table_type` nor `schema_mode`) or with
  `define_access(with_table=False)` over an optional column defined by hand
  (`TYPE option<string>`): the hash became `NONE` and every later signin failed. With a required
  column (`TYPE string`), the write was refused instead. Such a model is now refused before it
  writes a record (see Changed). README section 19, "User models" and a row of "ORM behaviour:
  SurrealDB 2.x vs 3.x" describe it; the behaviour is the same on both lines.

### Notes

- **Undeclared keys in list items on SurrealDB 3.x** (#301): the 0.34.1 entry below says 3.x
  keeps a key a `SCHEMAFULL` table does not define "in the items of any list of objects,
  `FLEXIBLE` or not". It keeps it only in the items of a list whose items define a field, such as
  a list of sub-models `define_table()` writes, and in an object inside them that defines one. It
  refuses it in the items of a list that defines no item field (an `array<object>` defined by hand
  without `v.*.n`), at the top level and in a sub-model outside such a list. SurrealDB's
  documentation of `FLEXIBLE` says every object of a `SCHEMAFULL` table, list items included,
  accepts only the keys defined for it, so do not rely on 3.x keeping such a key. README section
  35, the v0.7.0 and v0.34.0 rows of "ORM behaviour: SurrealDB 2.x vs 3.x" and the embedded
  engine's "Model table DDL (v0.34.0)" row now state the exception. No ORM behaviour changes.
- Server pins unchanged: tested against SurrealDB 3.3.4 and 2.7.0 and on the embedded engine,
  plus 3.1.5 for backward compatibility.

## [0.35.0] - 2026-10-22

Full-text search: a queryset searches text with the full SurrealDB-ORM's `search()`, and scores and
highlights the matches with `SearchScore` and `SearchHighlight` (plus `SearchOffsets`), over the
analyzers and full-text indexes a model declares with `Analyzer` and `Index` and applies with
`define_indexes()`. SurrealDB tokenises, matches, scores (BM25) and highlights on the server; the
ORM binds every search text and highlight tag as a query variable. The same code runs on SurrealDB
2.x, 3.x and the embedded engine, each line getting its own index keyword. The first minor of Tier
3's search phase.

### Added

- **`QuerySet.search(**field_queries)`**, the full SurrealDB-ORM's method: keeps the records whose
  fields match. Each keyword names a field — a Python field name (an aliased field searches its
  column) or a dotted path — and the text to match, bound, never written into the statement. The
  terms of one text are AND-ed, and so are the fields; each field gets the next match reference, in
  call order and across calls. `search()` applies wherever `filter()` does, AND-ed after
  `filter()`, `Q` and `nearby()`: `exec()`, `first()`, `get()` without an id, `count()`,
  `exists()`, the aggregations, `values()` groups, `bulk_update()`, `bulk_delete()`, `patch()` and a
  `Subquery`, whose references start again at 0 (the full ORM applies it to `exec()` only).
  `get(<id>)` and `all()` ignore it, as they ignore `filter()`.
- **`SearchScore(ref)` and `SearchHighlight(open_tag, close_tag, ref)`**, the full ORM's search
  annotations, **and `SearchOffsets(ref)`**, an addition (SurrealDB's `search::offsets()`), in
  `annotate()`: the BM25 score (a `float`), the text with each matched term between the tags (bound
  as query variables), and where the matched terms sit. `exec()`, `first()` and `get()` without an
  id set each value as an attribute named after its alias, never written back (`model_dump()`,
  `save()` and `update()` leave it out); `order_by("-relevance")` sorts on an alias. A reference no
  `search()` gave, or an alias that would shadow a field, its column or an attribute of the model,
  raises `ValueError` before any request.
- **`Analyzer` and `Index`**, with the argument names of the full ORM's `DefineAnalyzer` and
  `CreateIndex`: an analyzer's name, tokenizers (`blank`, `camel`, `class`, `punct`), filters
  (`ascii`, `lowercase`, `uppercase`, `ngram`, `edgengram`, `snowball`, `mapper`), `function=` and
  `comment=`; a plain index, a `UNIQUE` one (`unique=True`) or a full-text one (`search_analyzer=`,
  an `Analyzer` or the name of an analyzer the database defines), which covers one field and takes
  `bm25=` (`True`, or a `(k1, b)` pair) and `highlights=True`. Every argument is checked at
  construction, more strictly than the server where its answer would be silently wrong.
- **`SurrealConfigDict(indexes=[Index(...)])`** declares a model's indexes, checked when the class
  is defined (distinct names, one full-text index per column, one `Analyzer` per analyzer name);
  `get_indexes()` returns them in declaration order, and `Model.index_ddl()` renders their
  `DEFINE ANALYZER` and `DEFINE INDEX` statements without a connection, for SurrealDB 3.x or, with
  `line=2`, for 2.x.
- **`await Model.define_indexes()`** applies them on the model's connection: it reads what the
  database holds (`INFO FOR DB`, `INFO FOR TABLE`) and, when a full-text index is declared, the
  server line (`client.version()`), then defines what is missing and redefines what changed, one
  statement per request, and returns the statements applied — `[]` on an unchanged database, so it
  is safe at start-up. `overwrite=False` defines only what is missing (`IF NOT EXISTS`); `tx=` joins
  a transaction. Nothing is removed: **`await Model.remove_index(name)`** removes an index by name
  (`REMOVE INDEX IF EXISTS`; `missing_ok=False` raises `SurrealDbNotFoundError` for a missing one).
- **`SurrealDBConnectionManager.define_analyzer(analyzer)` and `remove_analyzer(analyzer)`**, for
  the analyzers, which belong to the database rather than to a table: `define_analyzer()` defines
  one on the current connection, redefining it only when it changed, and returns the statements
  applied; `remove_analyzer()` removes one by name and refuses one an index uses.
- README section 36, "Full-text search (`search()`, `SearchScore`, `SearchHighlight`, `Analyzer`,
  `Index`)" (declaring and applying, searching, scoring and highlighting, what SurrealDB decides,
  changing an analyzer, transactions, live queries, what is not supported, a note for users of the
  full SurrealDB-ORM), seven rows of the "ORM behaviour: SurrealDB 2.x vs 3.x" table and one of the
  embedded engine's, and the example notebook `examples/v0.35.0_full_text_search.ipynb`.

  ```python
  ENGLISH = Analyzer("english", ["class", "blank"], ["lowercase", "ascii", "snowball(english)"])


  class Article(BaseSurrealModel):
      model_config = SurrealConfigDict(
          table_name="article",
          indexes=[Index("article_title", "title", search_analyzer=ENGLISH, bm25=True, highlights=True)],
      )

      id: str | None = None
      title: str


  await Article.define_table()
  await Article.define_indexes()  # FULLTEXT on SurrealDB 3.x, SEARCH on 2.x; [] when nothing changed
  articles = await (
      Article.objects()
      .search(title="physics")
      .annotate(relevance=SearchScore(0), snippet=SearchHighlight("<b>", "</b>", 0))
      .order_by("-relevance")
      .exec()
  )
  # each article carries .relevance (a float) and .snippet (its title, "<b>physics</b>" highlighted)
  ```

### Fixed

- **A transaction error no longer quotes a bound search text**: a failed statement whose reason
  reports a missing full-text index ("no suitable index supporting the expression") is cut after
  the expression's operator (`… the expression: slug @0@`), since SurrealDB 3.x quotes the bound
  value after it. The server's text stays reachable as the error's suppressed `__context__`, as
  for a reason that quotes the query.

### Notes

- **The index keyword follows the server line**, read from `client.version()`: `FULLTEXT` on
  SurrealDB 3.x, `SEARCH` on 2.x and the embedded engine, each of which refuses the other's. The ORM
  writes one field per full-text index on both lines.
- **Only what changed is redefined**: an unchanged index is never redefined, so start-up never
  rebuilds. A rebuild re-reads the table: seconds per thousands of records on SurrealDB 2.x and the
  embedded engine.
- **A changed analyzer rebuilds its indexes**: SurrealDB keeps the tokens of the records an index
  holds when their analyzer changes, so a search would miss them. When `define_indexes()` or
  `define_analyzer()` redefines an analyzer, it rebuilds every index of the database that uses it
  (`REBUILD INDEX`) and logs one warning naming them (logger `surreal_orm_lite.search`). In a
  transaction, `define_indexes(tx=…)` refuses an analyzer to define or redefine, and
  `define_analyzer(tx=…)` a redefinition with indexes to rebuild, before any DDL: call
  `define_analyzer()` without `tx=` first.
- **`remove_analyzer()` is safe on SurrealDB 2.x**: 2.x and the embedded engine accept removing an
  analyzer an index uses, then refuse every write to the table; the ORM refuses it first on every
  line, with SurrealDB 3.x's own message.
- **A live query's searched subquery is checked**: before a live query whose `Subquery` searches,
  the ORM checks that each searched field has a full-text index and raises `SurrealDbError` naming
  the field otherwise, before the live query starts.
- **BM25 scores depend on the line**: a term found in more than half the documents scores `0.0` on
  SurrealDB 3.x and below zero on 2.x and the embedded engine, where a text holding such a term
  therefore scores lower than without it. On 2.x and the embedded engine, BM25 statistics count an
  index's records again at each rebuild or redefinition and keep counting deleted records, so the
  scores depend on the index's history (the matches do not): a documented 2.x limitation. SurrealDB
  3.x keeps them exact; compare scores within one query, and upgrade to 3.x where scores must not
  depend on the index's history.
- **Refused before any request** (`SurrealDbError`): `search()` with `as_of()`, in either order and
  in a subquery under it (the index holds the records' current text); with `live()` and `watch()`,
  since SurrealDB accepts a live query whose condition holds a full-text match but never notifies
  it — search in a `Subquery` instead, which notifies the records created or updated to match; and
  with `changes()`. A search on a field without a full-text index raises `SurrealDbError` naming the
  field instead of the server's error, which on SurrealDB 3.x quotes the search text, in a
  transaction too. **Indexes on materialized views** are refused (`ValueError` when the class is
  defined): SurrealDB 3.x does not index the records a view gains after the index is defined.
- **Call `define_table()` before `define_indexes()`**: on a table that does not exist yet,
  SurrealDB's `DEFINE INDEX` creates it `TYPE ANY SCHEMALESS PERMISSIONS NONE`, which
  `define_table()` replaces but `define_table(overwrite=False)` leaves as it is.
- **SurrealDB 3.1** (tested with 3.1.5 for backward compatibility): two `search()` fields AND-ed
  can also return records that match only one of them, scored `0.0` on the other; a field without a
  full-text index answers no record instead of an error; and a live query whose subquery searches
  such a field starts.
- **Deferred**: `search()` with `as_of()` on a versioned SurrealDB 3.x server; indexes on
  materialized views once SurrealDB indexes the records a view gains; 3.x's boolean match operators
  (`@AND@`/`@OR@`) and `highlight_all`; `indexes=[Index(...)]` in generated models;
  `define_index()`/`remove_index()` on the connection manager for tables without a model;
  `Index(concurrently=True)`. Vector indexes come in v0.36.0, `hybrid_search()` in v0.37.0, and
  analyzers and indexes in migrations in v0.38.0.
- Server pins unchanged: tested against SurrealDB 3.3.4 and 2.7.0 and on the embedded engine,
  plus 3.1.5 for backward compatibility.

## [0.34.1] - 2026-10-22

Follow-ups to the review of v0.34.0's model table DDL (#296): `define_access()` and
`define_table()` refuse an auth model whose `SCHEMAFULL` table would leave out a field SIGNUP
writes, and the README describes three behaviours of v0.34.0 more precisely. No new public API.

### Fixed

- **`define_access()` and `define_table()` raise `TypeError` for a signup field the table would
  leave out** (#296). Since v0.34.0, a model whose schema mode is `SCHEMAFULL`
  (`table_type=TableType.USER`, or a declared `schema_mode=SchemaMode.SCHEMAFULL`) gets from
  `define_access()` and `define_table()` a `SCHEMAFULL` table with one `DEFINE FIELD` per column
  `model_dump()` writes, while SIGNUP writes every field of the model. A field declared with
  `Field(exclude=True)` (`password: str = Field(exclude=True)`) therefore had no column: SurrealDB
  3.x refused every signup ("The record access signup query failed"), and 2.x dropped the value, a
  password hash, so every signin failed. For such an auth model (`AuthenticatedUserMixin`),
  `access_ddl()` and `define_access()` with `with_table=True`, and `table_ddl()` and
  `define_table()`, now raise `TypeError` before any request, naming the model and the fields.
  Remove `exclude=True` and leave the field out where you serialise the model instead
  (`model_dump(exclude={"password"})`): a table defined by hand does not help, since `update()` and
  `upsert()` replace the record with what `model_dump()` writes, which a required column refuses
  and an optional one loses. `define_access(with_table=False)`, a `SCHEMALESS` table and a model
  that is not an auth model (whose excluded field `save()` never writes) behave as before.

### Notes

- **A regenerated `SCHEMAFULL` edge model is applied with `define_table()`**: since v0.34.0,
  `generate_models_from_db()` gives a `SCHEMAFULL` relation table with data fields an edge model
  with `schema_mode=SchemaMode.SCHEMAFULL`, for which `define_relation()` raises `SurrealDbError`
  before any request (it defines no field). Call `define_table()`, which applies the same relation
  statement, then the data fields. README sections 31 and 34 say so.
- **`define_access()` and `define_table()` write different table types for a model that declares
  no `table_type`** and whose schema mode is not `SCHEMAFULL`: `define_access()` writes no `TYPE`,
  which SurrealDB stores as `TYPE ANY`, and `define_table()` writes `TYPE NORMAL`, so a start-up
  that calls both changes the table's type each time. Declare `table_type` to keep one type. The
  schema mode, fields and permissions are the same in any call order; README "User models" and the
  docstrings no longer say that the two converge on everything.
- **Undeclared keys in `FLEXIBLE` list items on SurrealDB 2.x and the embedded engine**: the items
  of a list `define_table()` writes `FLEXIBLE` (a list of sub-models holding a `dict`, an `Any` or
  an untyped `list`) keep a key the model does not declare, written raw or by another client (the
  ORM never sends one), while a list that is not `FLEXIBLE` drops it. This is a 2.x limitation:
  `FLEXIBLE` there covers the whole item. 3.x keeps such a key in the items of any list of objects,
  `FLEXIBLE` or not, and refuses it at the top level or in a sub-model. README section 35 and the
  "ORM behaviour: SurrealDB 2.x vs 3.x" row on undeclared fields say so.
- **Enum columns**: the 0.34.0 entry's list of column kinds leaves out one case: an `enum.Flag` or
  `IntFlag` field is written `int`, since Pydantic also accepts any combination of its members, and
  zero. The README "Field types" table, which says so, is now pinned by a test.
- Server pins unchanged: tested against SurrealDB 3.3.4 and 2.7.0 and on the embedded engine,
  plus 3.1.5 for backward compatibility.

## [0.34.0] - 2026-10-21

Model table DDL: a model declares its table's schema mode, change feed, permissions and comment
with the full SurrealDB-ORM's keys, and `define_table()` writes the table's `DEFINE TABLE` and one
`DEFINE FIELD` per column from the model's annotations and applies them, keeping what the model
does not declare. The same code runs on SurrealDB 2.x, 3.x and the embedded engine. The last minor
of Tier 3's schema and DDL phase; this release also completes the roadmap renumbering below.

### Added

- **`SchemaMode`** (`SCHEMAFULL`, `SCHEMALESS`), the full SurrealDB-ORM's enum, exported from the
  package root, and the full ORM's five table keys on `SurrealConfigDict`: `schema_mode`,
  `changefeed` (a duration, optionally followed by `INCLUDE ORIGINAL`), `permissions` (a mapping of
  `select`/`create`/`update`/`delete` to `FULL`, `NONE`, `WHERE <condition>` or a bare condition;
  an operation left out is `NONE`), `comment` and `flexible_fields`. They are checked when the
  class is defined; a subclass inherits them and `None` resets one. `get_schema_mode()` (`None`
  when not declared), `get_changefeed()` (the normalised text, `"1h INCLUDE ORIGINAL"`) and
  `get_permissions()` (the declared mapping, a copy, as the full ORM's) read them back.
- **`Model.table_ddl()` and `await Model.define_table()`**: `table_ddl()` renders the model's
  `DEFINE TABLE OVERWRITE` and one `DEFINE FIELD OVERWRITE` per column as a `list[str]`, without a
  connection. `define_table()` reads the table's definition (one `INFO FOR DB`, and one
  `INFO FOR TABLE` for a table that exists), sends the statements one by one, table first and
  parents before children, and returns those applied joined with `"\n"` — the full ORM's return.
  It takes `overwrite=` (`False` writes `IF NOT EXISTS`) and `tx=`, runs on the model's connection,
  is idempotent (run it at start-up) and removes nothing. Each column's kind comes from its
  annotation (strings, numbers, `Decimal`, datetimes, durations, `UUID`, `RecordID`, geometries,
  ranges, enums, literals, lists, tuples, sets, dictionaries and sub-models, one column per
  sub-model field), `option<…>` for a nullable column; a constant default is written `DEFAULT` on a
  column that is not nullable, a computed field `VALUE`, and `FLEXIBLE` on the free-form object
  columns of a `SCHEMAFULL` table. A view model's `define_table()` applies `define_view()`; an edge
  model's applies the relation statement `define_relation()` carries, then the edge's data fields.

  ```python
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


  print(*Person.table_ddl(), sep="\n")
  # DEFINE TABLE OVERWRITE `person` TYPE NORMAL SCHEMAFULL COMMENT 'People' CHANGEFEED 7d PERMISSIONS FOR select FULL, FOR create WHERE $auth.id != NONE, FOR update, delete NONE;
  # DEFINE FIELD OVERWRITE `name` ON `person` TYPE string;
  # DEFINE FIELD OVERWRITE `age` ON `person` TYPE int DEFAULT 18;
  # DEFINE FIELD OVERWRITE `nickname` ON `person` TYPE option<string>;
  sql = await Person.define_table()  # the same statements, applied; idempotent
  ```

- **`TableType`'s other members take effect** (v0.33.0 recorded them): `TYPE NORMAL` for
  `NORMAL`, `USER`, `STREAM` and `HASH` and for a model without `table_type`, `TYPE ANY` for `ANY`
  (a table that holds records and edges). `USER` makes the table `SCHEMAFULL` and `HASH`
  `SCHEMALESS` when `schema_mode` is left out; `STREAM` adds nothing (declare `changefeed`).
- README section 35, "Model table DDL (`define_table()`, `schema_mode`, `permissions`)" (declaring
  the table, field types, applying it, views and edges, user models, what SurrealDB checks,
  SurrealDB 2.x and 3.x, transactions, connections and the cache, security, a note for users of the
  full SurrealDB-ORM), updates to sections 16, 19, 31, 33 and 34, a features row, four rows in the
  2.x/3.x behaviour table and one in the embedded-engine table, the comparison and roadmap rows,
  and the example notebook `examples/v0.34.0_model_table_ddl.ipynb`.

### Changed

- **`schema_mode`, `changefeed`, `permissions`, `comment` and `flexible_fields` are checked at the
  `class` statement** (they were accepted and ignored before 0.34.0): a value of the wrong type
  raises `TypeError` and one of the wrong form `ValueError` when the module is imported — a
  plain-string `schema_mode` such as `"SCHEMAFULL"` included: use `SchemaMode.SCHEMAFULL`.
  `schema_mode=SchemaMode.SCHEMAFULL` with `extra="allow"`, and `TableType.USER` with
  `SchemaMode.SCHEMALESS`, raise `TypeError`.
- **A materialized view model with `table_type`** (accepted and ignored in 0.33.0),
  `schema_mode` or `flexible_fields` raises `TypeError` at the `class` statement. A view takes
  `permissions`, `changefeed` and `comment`.
- **`define_view()` keeps the permissions, comment, change feed, `DROP` and `SCHEMAFULL` set by
  hand on a stored view** unless the model declares them (it reset them). When its statement
  writes permissions, a refusal that quotes the statement is reported without it.
- **`view_ddl()`, `Edge.relation_ddl()` and `Edge.define_relation()` write the clauses the model
  declares** (unchanged for a model without the keys). `define_relation()` of an edge model that
  declares `schema_mode=SchemaMode.SCHEMAFULL` and has data fields raises `SurrealDbError` before
  any request: it defines no field, so apply that edge with `define_table()`.
- **`access_ddl()` and `define_access()` follow the model's table keys**: the declared clauses
  replace v0.17.0's defaults, and a model with `table_type` gets `TYPE NORMAL` (or `TYPE ANY`) in
  its table statement. A model whose schema mode is `SCHEMAFULL` — declared, or `TableType.USER` as
  in the full ORM — gets a `SCHEMAFULL` table with one `DEFINE FIELD` per field, so signup and
  signin work on SurrealDB 2.x and 3.x and `define_table()` gives the same table in any call order;
  `TableType.USER` with `extra="allow"` raises `TypeError` there (with `with_table=True`). A 0.33.0
  `TableType.USER` model's table therefore turns `SCHEMAFULL` at its next `define_access()`: a
  record stored before is checked at its next write, where a field the model does not declare is
  refused (SurrealDB 3.x) or dropped (2.x). A model with `AuthenticatedUserMixin` and none of the
  new keys renders v0.17.0's statements unchanged.
- **`generate_models_from_db()` writes the table's clauses as keys**: `table_type=TableType.ANY`
  for a `TYPE ANY` table, `schema_mode=SchemaMode.SCHEMAFULL`, `changefeed=`, `permissions={…}`
  and `comment=` instead of comments (a comment stays where the `class` statement would refuse the
  key), and the generated module's docstring says `define_table()` may be called on its models.
  README section 31 says so; the v0.30.0, v0.32.0 and v0.33.0 notebooks are re-executed for it.
- **Docstrings that named v0.37.0 for the table DDL and v0.39.0 for `ModelFactory`** name v0.34.0
  and v0.40.0.
- **Roadmap: the model table DDL is v0.34.0.** `Model.define_table()`, as close as possible to
  the full SurrealDB-ORM's, was planned with the migrations; it now comes before the search
  versions, whose index and analyzer DDL build on it. The later versions move up one: full-text
  search v0.35.0, vector search v0.36.0, hybrid search v0.37.0, migrations v0.38.0, the CLI
  v0.39.0, test factories v0.40.0 and the Beta phase v0.41.0. `docs/ROADMAP.md` records the
  constraints the earlier versions left v0.34.0, and the README uses the new numbers.
- **Reading the released entries below**: they keep the numbers they were written with. Full-text
  search v0.34.0 is now v0.35.0; `define_table()` and `TableType`'s other members v0.37.0 are now
  v0.34.0; migrations, `schema_diff()`/`SchemaState.diff()` and the removal of events v0.37.0 are
  now v0.38.0; the `inspectdb` command v0.38.0 is now v0.39.0; `ModelFactory` and `Faker` v0.39.0
  are now v0.40.0; retrying arbitrary queries v0.40.0 is now v0.41.0 (Beta).
- **README section 33 links the upstream SurrealDB issues** behind its two view workarounds on
  3.x: `id IN` on a view table answers no record
  ([surrealdb/surrealdb#7608](https://github.com/surrealdb/surrealdb/issues/7608)), and a view's
  live notifications carry no `id`
  ([surrealdb/surrealdb#7607](https://github.com/surrealdb/surrealdb/issues/7607)). The v0.32.0
  row "Live queries and events on a view" of "ORM behaviour: SurrealDB 2.x vs 3.x" links #7607
  too, next to its 3.x workaround (filter on the view's fields, not on `id`).

### Fixed

- **The 0.33.0 notes: `relation_ddl()` and `define_relation()` are edge-model methods.** The
  [0.33.0] entry below writes `Model.relation_ddl()` and `await Model.define_relation()`. Both are
  class methods of `BaseSurrealEdge` only, `Edge.relation_ddl()` and
  `await Edge.define_relation()`; a node model has neither (`AttributeError`).
  `Model.get_table_type()` is on every model, as written. The ROADMAP's 0.33.0 entry now says so.

### Notes

- **What the model declares wins; the rest is kept from the database**: the table's schema mode,
  comment, change feed, permissions and `DROP`, and each field's `ASSERT`, `READONLY`,
  `REFERENCE`, permissions, comment and a stored `DEFAULT`, `VALUE` or `COMPUTED` the model does
  not write. Nothing is removed: records, other fields, indexes, events, the views that read the
  table and its live queries stay; a key left out never removes a stored clause.
  `schema_mode=SchemaMode.SCHEMALESS` turns a `SCHEMAFULL` table back and `permissions={}` writes
  `PERMISSIONS NONE`. Before any DDL, `define_table()` refuses a materialized view, a relation
  table (derive the model from `BaseSurrealEdge`) and a definition it cannot read back or carry.
- **SurrealDB 2.x and the embedded engine empty the objects and arrays of an `any` column and of
  an untyped array or set on a `SCHEMAFULL` table**, optional or not; `FLEXIBLE` keeps them, and
  3.x keeps them without it but refuses `FLEXIBLE` there. They also drop the `dict`, `Any` or
  untyped `list` of a sub-model in a list of sub-models (`list[M]`, at any depth: `items.*.meta`)
  from every item, then refuse the record's next write (`Found NONE for field`), unless the list is
  `FLEXIBLE` too; 3.x keeps those values without it. `define_table()` reads the line
  (`client.version()`, once, only for a `SCHEMAFULL` table with such a column or such a list) and
  writes `FLEXIBLE` on those columns and lists before 3.x, the items' typed fields still checked;
  `table_ddl()`, which needs no connection, writes the 3.x form. An untyped `list` gets an item
  definition `TYPE any` (`bag.*`) on every line, without which 3.x refuses an object item of a
  required untyped array. A union holding a list or a set together with an object or another
  container writes its lists, sets and tuples as an untyped `array` with that item definition,
  since SurrealDB derives one item type for every member of such a union.
- **A write naming a field a `SCHEMAFULL` table does not define** (a `merge()` key the model does
  not declare, `bulk_update()`, a raw query, a record stored before the table became `SCHEMAFULL`)
  is refused on SurrealDB 3.x (`no such field exists`) and the field dropped, silently, on 2.x and
  the embedded engine: declare the field on the model. SurrealDB checks the records already in the
  table at their next write, not when `define_table()` changes a kind or the schema mode.
- **`COMPUTED` fields are SurrealDB 3.x's**: `define_table()` keeps a stored `COMPUTED` field
  `COMPUTED` (replacing it by `VALUE` would leave the stored records without the field until their
  next write) and writes a new computed field `VALUE` on both lines; `define_computed_fields()`
  still writes `VALUE` alone. A computed field's expression is checked before any request.
- **No `DEFAULT` on a nullable column**: SurrealDB applies `DEFAULT` to a `NONE` on every create,
  so the `None` the ORM writes would become the default. A `server_fields` column keeps the
  database's `DEFAULT`, `VALUE` or `COMPUTED`, and the model's constant default is written only
  where the database has none; one that is not nullable and has no constant default is written
  without `DEFAULT`, so every create leaves it out and SurrealDB refuses it until the database
  gives the column a `DEFAULT` or `VALUE` (or annotate it `T | None`).
- **Each call on a table with a change feed adds one `define_table` entry** to it; `changes()`
  skips it, a raw `SHOW CHANGES` shows it.
- **Transactions**: with `tx=`, the reads run on committed state before anything is queued and
  the statements join the transaction (a refusal in a buffered one is the transaction's
  `SurrealDbError`). A definition of the same table queued earlier in the block is not seen: define
  it before the block. The reads and the definitions are a check, then the DDL, not a lock.
- **Security**: names are written in backquotes, kinds built from Python types and `DEFAULT`
  values through the ORM's closed SurrealQL encoder. The declared permission rules, the comment and
  each computed field's expression are checked for mistakes (a comment marker, which would swallow
  the clauses after it, is refused); they are trusted application code, and the checks are not an
  injection defence. A clause the model does not declare is kept from the database, re-checked, so
  applying a model never resets a table's or a field's permissions; a refusal names the table or
  the field, never the statement. Applying needs a privileged session (root, or a namespace or
  database `OWNER` or `EDITOR`).
- **Differences from the full SurrealDB-ORM** (README section 35 lists them): a model that declares
  no schema mode keeps the stored one and a new table is `SCHEMALESS`; clauses the model does not
  declare are kept; one statement per call, a refusal raising `SurrealDbError` naming the table or
  the field (the full ORM raises `MigrationStatementError` quoting the SQL); `TYPE NORMAL` written
  for a model without `table_type`; `FLEXIBLE` written after `TYPE`; kinds for the rich field
  types; no `DEFAULT` on a nullable field; a view model's `define_table()` defines no field.
- **SurrealDB 3.1.5 (informative)** behaves as 3.3.4 except that it refuses a Python `set`
  written to a `set` field and an object item of an optional untyped `list`, and that a field
  named after a SurrealQL keyword (`` `select` ``) breaks every later statement on its table — 3.1
  server defects.
- **Deferred**: declaring field clauses on the model (`ASSERT`, `READONLY`, field permissions and
  comment), typed record links (`record<table>`; `RecordID` is written `record`), refusing
  undeclared field names before a write, and `define_table(tx=…)` seeing a definition queued
  earlier in the block. Migrations (v0.38.0) bring the removal of fields and clauses a model no
  longer declares, and decide whether the lite gains `MigrationStatementError`.
- **Server pins unchanged**: tested against SurrealDB 3.3.4 and 2.7.0 and on the embedded engine,
  plus 3.1.5 for backward compatibility.

## [0.33.0] - 2026-10-20

`TYPE RELATION` enforcement: an edge model declares which tables its edges may join and whether
both ends must exist, with the full SurrealDB-ORM's keys, and `define_relation()` applies the
constraint to its table while keeping the table's other clauses. SurrealDB enforces it on every
write, and every edge creation path raises the same ORM errors on SurrealDB 2.x, 3.x and the
embedded engine. The last minor of Tier 3's schema and DDL phase.

### Added

- **`SurrealConfigDict(relation_in=…, relation_out=…, enforced=…)` on `BaseSurrealEdge`**, the
  full SurrealDB-ORM's keys for SurrealDB's `TYPE RELATION IN … OUT … ENFORCED`: `relation_in`
  names the tables an edge's `in` may come from, `relation_out` those its `out` may point to (one
  name, several separated by `|`, or a list or tuple; `None` or an empty value means any table),
  and `enforced=True` makes SurrealDB refuse an edge whose endpoint record does not exist. The keys
  are checked when the class is defined; a subclass inherits them and `None` resets one.
  `get_relation_in()` and `get_relation_out()` return tuples, in the shape of introspection's
  `TableState.relation_in`/`relation_out`, and `is_enforced()` a bool.
- **`TableType`**, the full SurrealDB-ORM's enum (`NORMAL`, `USER`, `STREAM`, `HASH`, `RELATION`,
  `ANY`, same values), exported from the package root, and `Model.get_table_type()` on every
  model: `TableType.RELATION` for an edge model, else the configured `table_type`, else
  `TableType.NORMAL`. `table_type=TableType.RELATION` is optional on an edge model; the other
  members are recorded for v0.37.0 and have no effect before it.
- **`Model.relation_ddl()` and `await Model.define_relation()`**: `relation_ddl()` renders
  ``DEFINE TABLE OVERWRITE `<table>` TYPE RELATION IN … OUT … ENFORCED;`` without a connection
  (pure, deterministic; `overwrite=False` writes `IF NOT EXISTS`). `define_relation()` reads the
  table's definition once (`INFO FOR DB`), then sends one `DEFINE TABLE OVERWRITE` that writes the
  model's `TYPE RELATION` clause and keeps the table's schema mode, `DROP`, comment, change feed
  and permissions, as well as its records, fields, indexes, events, the views that read it and
  its live queries. It returns the statement applied, is idempotent (run it at start-up), takes
  `tx=`, runs on the model's connection and is enforced at once on every connection. A model
  without the keys removes the constraint the same way.

  ```python
  class Likes(BaseSurrealEdge):
      model_config = SurrealConfigDict(
          table_name="likes", relation_in="person", relation_out=["post", "book"], enforced=True
      )


  await Likes.define_relation()
  await ada.relate(Likes, graphs)                        # accepted
  await acme.relate(Likes, graphs)                       # SurrealDbError: a company is not a person
  await ada.relate(Likes, RecordID("book", "missing"))   # SurrealDbNotFoundError (ENFORCED)
  ```

- README section 34, "Relation constraints (`relation_in`, `define_relation()`)" (declaring the
  constraint, applying it, what SurrealDB refuses, records already in the table, removing it,
  transactions, connections and the query cache, SurrealDB 2.x and 3.x, security, a note for users
  of the full SurrealDB-ORM), a features row, three rows in the 2.x/3.x behaviour table and one in
  the embedded-engine table, the comparison and roadmap rows, and the example notebook
  `examples/v0.33.0_type_relation_enforcement.ipynb`.

### Changed

- **`relation_in`, `relation_out`, `enforced` and `table_type` are checked at the `class`
  statement** (they were accepted and ignored before 0.33.0). On a node model
  (`BaseSurrealModel`), `relation_in`, `relation_out` and `enforced` (even empty or `False`) and
  `table_type=TableType.RELATION` raise `TypeError`: derive the model from `BaseSurrealEdge`. A
  `table_type` that is not a `TableType` (a plain `"relation"` string included) or an `enforced`
  that is not a bool raises `TypeError`; a table name that is not an identifier raises
  `ValueError`.
- **The string form `relate("<edge table>", …)` translates server errors**, as the typed form does
  since v0.22.0: an endpoint the table's constraint refuses raises `SurrealDbError`, a missing
  endpoint of an `ENFORCED` table `SurrealDbNotFoundError`, a duplicate pair on a `UNIQUE` index
  `SurrealDbError`, instead of the SDK's `ServerError` (kept as the `__cause__`). Inside a
  transaction nothing changes.
- **`generate_models_from_db()` writes a relation table's ends as keys**: `relation_in=`,
  `relation_out=` (a string for one name, a list for several, in the server's order) and
  `enforced=True` right after `table_name=`, instead of the `# IN … OUT … ENFORCED` comment, so a
  relation table round-trips (define → introspect → generate → define gives the same
  `TableState`). A table name that is not an identifier keeps the comment. README section 31 says
  so; the v0.30.0 introspection notebook is re-executed for it.

### Notes

- **Enforcement is SurrealDB's**: the ORM adds no check of its own before a write. An edge refused
  by the constraint, through any creation path (`relate()` typed or by table name, an edge's
  `save()`, `objects().bulk_create()`), raises `SurrealDbError` for an endpoint from a table
  outside `IN`/`OUT` and `SurrealDbNotFoundError` (a `SurrealDbError`) for a missing endpoint with
  `ENFORCED`; a batch with one refused edge writes nothing; inside a transaction the refusal is the
  transaction's `SurrealDbError`. A plain record written into the relation table, and an update of
  an edge stored before the constraint that it refuses, raise the SDK's `InternalError`.
- **Records already in the table are not checked**: a nonconforming edge stays (its updates are
  refused), a dangling edge stays and can be updated (`ENFORCED` is checked at creation), and
  `delete()` always works. README section 34 gives the two queries that find them.
- **`define_relation()` refuses before any DDL** a materialized view (the `OVERWRITE` would drop
  its query), a definition the ORM cannot read back, and `overwrite=False` on a table that exists
  and is not a relation table (`IF NOT EXISTS` would leave the constraint unapplied). The read and
  the definition are a check, then the DDL, not a lock. In a transaction the read runs on committed
  state before anything is queued: define the table's other clauses before the block.
- **SurrealDB rewrites the `in` and `out` field definitions** from `IN` and `OUT`: an `ASSERT`
  written on them by hand is lost. Turning the table back into `TYPE ANY` is not offered and does
  not lift the constraint on any line; a keyless `define_relation()` does.
- **SurrealDB 2.x and 3.x**: `define_relation()` is the same on every line and on the embedded
  engine. The refusal's wording differs (`Found … but expected a record<…>` on 2.x and the
  embedded engine, `Couldn't coerce value for field …` on 3.x), and an endpoint both outside
  `IN`/`OUT` and missing, on an `ENFORCED` table, raises `SurrealDbNotFoundError` on 2.x and the
  embedded engine and `SurrealDbError` on 3.x; `except SurrealDbError` handles both. A raw
  `ALTER TABLE … TYPE RELATION …` of a table already written to is not enforced on 2.x and the
  embedded engine until a `DEFINE TABLE OVERWRITE` of the table; the ORM never sends
  `ALTER TABLE`.
- **Security**: the table name and every `relation_in`/`relation_out` name are checked as
  identifiers and written in backquotes; no other application value reaches the statement. The
  clauses `define_relation()` keeps are the database's own definition, the comment re-quoted and
  each permission rule re-checked; a value that fails is not written. A refusal names the table,
  never the statement. The constraint is not an access control, and applying it needs a privileged
  system user (root, or a namespace or database `OWNER` or `EDITOR`).
- **Differences from the full SurrealDB-ORM**: the same keys and `TableType`, on `BaseSurrealEdge`
  (a `BaseSurrealModel` with `table_type=TableType.RELATION` raises `TypeError`);
  `relation_ddl()` and `define_relation()` where the full ORM applies the DDL through migrations or
  `define_table()`; names are identifiers in backquotes and a pipe-joined string is parsed;
  `get_relation_in()`, `get_relation_out()` and `is_enforced()` are lite additions.
- **Deferred**: migrations (`CreateTable`/`AlterTable` with the relation keys) and
  `define_table()`, which will apply an edge model through `define_relation()`, come with v0.37.0;
  `TableType`'s other members have no effect until then. The lite has no schema-mode, permissions,
  change-feed or comment keys yet, so `define_relation()` keeps those clauses from the database.
- **Server pins unchanged**: tested against SurrealDB 3.3.4 and 2.7.0 and on the embedded engine,
  plus 3.1.5 for backward compatibility.

## [0.32.1] - 2026-10-20

Two follow-ups to the materialized views of v0.32.0 (#286): a function SurrealDB lists as unusable
in a view is allowed inside the argument of each of several aggregates, and a chain of views can
be removed outer view first in one buffered transaction. No new public API.

### Fixed

- **A listed function inside the argument of each of several aggregates passes the
  `view_query` check** (#286). Since v0.32.0, a grouped view whose projection calls
  `array::distinct()`, `array::group()`, `array::join()`, `math::median()` or `vector::sum()`
  outside an aggregate's argument raises `ValueError` at the `class` statement, but only an element
  that is one whole aggregate had its calls skipped: an expression combining several aggregates,
  such as `math::sum(amount) + math::sum(array::len(array::distinct(tags)))`, was refused on every
  line. Such an expression now passes the check; a listed function beside or around an aggregate
  (`math::sum(amount) + array::len(array::distinct(tags))`) is still refused, with the same
  message. SurrealDB 3.x keeps such a view up to date; on 2.x and the embedded engine
  `define_view()` still refuses an expression around aggregates before any DDL, as in v0.32.0.
  `generate_models_from_db()` follows the check: such a view table is now generated as a view
  model instead of a class carrying a `# View: AS …` comment.
- **`remove_view()` accepts a chain of views removed outer view first in one buffered
  transaction** (#286). On SurrealDB 2.x and the embedded engine, `remove_view()` refuses with
  `SurrealDbError`, before any DDL, to remove a view that another view reads, and it read committed
  state only, so this block was refused although the server accepts both statements at commit:

  ```python
  async with SurrealDBConnectionManager.transaction() as tx:
      await OrderStatsTotals.remove_view(tx=tx)  # the outer view, queued
      await OrderStats.remove_view(tx=tx)        # refused: the outer view was still counted
  ```

  In a buffered transaction (SurrealDB 2.x, HTTP, the embedded engine), a view whose
  `remove_view(tx=tx)` the same block has already queued is no longer counted, unless the block
  names it again afterwards (a `define_view(tx=tx)` of it). A view removed in another transaction,
  or by a `REMOVE TABLE` you add to the block yourself (`tx.add(…)`), is still counted: the check
  errs on the side of the refusal. SurrealDB 3.x decides by itself, as before.

### Notes

- README section 33 and the "ORM behaviour: SurrealDB 2.x vs 3.x" row on removing a view that
  another view reads say so.
- Server pins unchanged: tested against SurrealDB 3.3.4 and 2.7.0 and on the embedded engine,
  plus 3.1.5 for backward compatibility.

## [0.32.0] - 2026-10-20

Materialized views as read-only models: a model declares the `SELECT` that SurrealDB computes a
table from (`DEFINE TABLE … AS SELECT`), applies it idempotently with `define_view()` and reads it
like any model, while every ORM write of it is refused. The same code runs on SurrealDB 2.x, 3.x
and the embedded engine; a grouped view that SurrealDB 2.x would compute wrongly is refused before
any DDL there. The third minor of Tier 3.

### Added

- **`SurrealConfigDict(view_query="SELECT …")`**: the model reads a materialized view, a table
  SurrealDB computes from that query and keeps up to date as the tables it selects from change.
  The query is one `SELECT … FROM <table> [WHERE …] [GROUP BY … | GROUP ALL]` (a final `;` is
  dropped), checked when the class is defined: a mistake raises `TypeError` or `ValueError` at the
  `class` statement. `Model.get_view_query()` returns it as checked. `Model.view_ddl()` renders
  ``DEFINE TABLE OVERWRITE `<table>` AS (<query>);`` without a connection,
  `await Model.define_view()` applies it on the model's connection (idempotent: run it at
  start-up) and returns it, and `await Model.remove_view()` writes `REMOVE TABLE IF EXISTS`.
  `overwrite=False` writes `IF NOT EXISTS`, `missing_ok=False` raises `SurrealDbNotFoundError` for
  a missing view, and both appliers take `tx=`. A subclass inherits `view_query`;
  `view_query=None` makes it an ordinary model.

  ```python
  class OrderStats(BaseSurrealModel):
      model_config = SurrealConfigDict(
          table_name="order_stats",
          view_query="SELECT status, count() AS total, math::sum(amount) AS revenue FROM orders GROUP BY status",
      )

      id: RecordID | None = None
      status: str
      total: int
      revenue: float


  await OrderStats.define_view()                   # idempotent (OVERWRITE)
  paid = await OrderStats.objects().get(["paid"])  # a grouped view's id: its group values
  await paid.merge(total=0)                        # TypeError: Cannot modify materialized view …
  ```

- **Every write of a view model raises `TypeError`** before any signal, transaction or request:
  `save()`, `upsert()`, `update()`, `merge()`, `patch()`, `delete()`, the atomic helpers
  (`atomic_append()` … `atomic_remove_many()`), and the QuerySet's `bulk_create()`,
  `bulk_update()`, `bulk_delete()`, `patch()`, `update_or_create()`, `get_or_create()` (even when
  the record exists) and `delete_table()`. The message starts with the full SurrealDB-ORM's
  `Cannot modify materialized view '<table>'` and names the call: write to the tables the view
  selects from instead. Reads, `refresh()`, the relation helpers, `define_events()` and
  `remove_event()` stay allowed, since they do not write the view's table.
- **Array record ids**: `get(["paid"])` and `filter(id=["paid"])` address a grouped view's record
  (`order_stats:['paid']`, the array of its group values); a `GROUP ALL` view's id is `[]`, read
  with `first()`.
- README section 33, "Materialized views (`view_query`, `define_view()`)" (declaring a view,
  applying and removing, reading, refused writes, transactions, connections and the query cache,
  SurrealDB 2.x and 3.x, security, a note for users of the full SurrealDB-ORM), a features row,
  eight rows in the 2.x/3.x behaviour table and one in the embedded-engine table, the comparison
  and roadmap rows, and the example notebook `examples/v0.32.0_materialized_views.ipynb`.

### Changed

- **`generate_models_from_db()` writes a view model**: a view table becomes a class with
  `view_query=…` in its `SurrealConfigDict` and `id: RecordID | None = None`, marked
  `# Read-only materialized view`, instead of a class carrying a `# View: AS …` comment. A view the
  lite cannot declare (an edge, a computed field, a query `view_query` refuses) keeps that comment.
  README section 31 says so.
- **Cached reads of a view model are dropped by every ORM write** (README section 29): reads of a
  view model, and reads whose subqueries read one, since a write to a source table changes the view
  and the ORM does not know which tables a view reads. `define_view()` and `remove_view()` drop
  cached reads like every write. Writes by raw SurrealQL or by another process are not seen, as for
  every table.
- A list or tuple given as a record id (`get()`, `filter(id=...)`, `id__in` items,
  `get_or_create()`/`update_or_create()` id criteria) is now an array record id; it used to be the
  string of its `repr()`.

### Notes

- **A view's query is trusted application code, never user input.** It is written into `AS (…)`
  as given, since SurrealDB takes no bound parameter in a definition. Its structural check
  (literals and quoted identifiers terminated, brackets balanced, no NUL, no SurrealQL comment
  marker outside a literal, one `SELECT` and no `;` outside brackets) catches mistakes —
  unbalanced brackets, a comment that would swallow the parenthesis the ORM appends, a second
  statement where one SELECT is expected — and is not an injection defence. To filter on a value,
  store it in a record or a `DEFINE PARAM` and read it from the query; never format it into the
  text. The table name is checked as an identifier and quoted. A refusal names the view's table,
  never the statement, and keeps the server's reason whole, which names a missing source table.
- **Grouped views on SurrealDB 2.x and the embedded engine**: those engines keep only whole
  aggregates (`count()`, `math::sum()`, `math::mean()`, `math::min()`, `math::max()`,
  `time::min()`, `time::max()`) and `GROUP BY` fields up to date, and compute anything else once
  per source record. So `define_view()` raises `SurrealDbError` before any DDL there for
  `math::stddev()` and `math::variance()`, an expression around an aggregate
  (`math::sum(amount) / count()`), a field outside `GROUP BY` and outside an aggregate (3.x refuses
  that one too) and a `GROUP BY` on an alias. SurrealDB 3.x keeps them all up to date.
- **Removing a view that another view reads**: SurrealDB 3.x refuses it, and `remove_view()`
  passes the refusal on as `SurrealDbError`; on 2.x and the embedded engine `remove_view()` raises
  `SurrealDbError` before any DDL, since those engines would turn each view reading it into an
  ordinary table that keeps its last records and no longer updates. Remove the outer views first.
- **The table guard**: `define_view()` never turns a table that holds records into a view, since
  SurrealDB would delete them, and `define_view(overwrite=False)` refuses any ordinary table, which
  `IF NOT EXISTS` would silently keep. An ordinary table without records, as `define_events()` or,
  on 2.x, a `watch()` run before `define_view()` leaves it, becomes the view. `remove_view()`
  refuses a table that is not a view. The guard is a check-then-act on committed state, not a
  lock.
- **SurrealDB 2.x and 3.x**: raw writes to a view (raw SurrealQL, not the ORM) are accepted by 2.x
  and 3.1, which change the view's records, and refused by 3.2 and later; a sum of integers is an
  `int` and a mean a `Decimal` on 2.x, both `float` on 3.x (declare them `float`); `count()` of an
  array field adds the arrays' lengths on 2.x and counts the records whose array is not empty on
  3.x; live queries and events see an emptied group as an `UPDATE` then a `DELETE` on 2.x, a
  `DELETE` on 3.x. The embedded engine behaves as a 2.x server, without live queries.
- **A source write missing an aggregated field is refused** by SurrealDB on every line
  ([surrealdb/surrealdb#7447](https://github.com/surrealdb/surrealdb/issues/7447)): write
  `math::sum(amount ?? 0)`.
- **`define_view()` recomputes the view** (`OVERWRITE`): each call costs one pass over the source
  and drops the events, fields and indexes defined on the view, so run `define_events()` after
  `define_view()`. A view is defined without a `PERMISSIONS` clause, so SurrealDB applies its
  default `PERMISSIONS NONE`: a record user reads nothing from it, and `define_view()` resets
  permissions set by hand. Applying needs a privileged system user (root, or a namespace or
  database `OWNER` or `EDITOR`): run `define_view()` at start-up or deployment.
- **The ORM's write refusal guards against mistakes and is not an access control**: on 2.x and
  3.1 the server accepts writes to a view from any session allowed to write the table.
- On a view model, `filter(id__in=...)` is sent as `$ids CONTAINS id`: SurrealDB 3.x answers no
  record to `id IN` on a view table, while `CONTAINS` is right on every line. Write raw SurrealQL
  over a view the same way.
- On SurrealDB 3.x, `live()` takes a view instance's id from the notification's record, since the
  server's view notifications carry none; `watch()` yields the envelopes as received (read
  `record`). A live query on a view filtered on `id` never notifies on 3.x: filter on the view's
  fields.
- On SurrealDB 3.1, tested for backward compatibility only, the server refuses to redefine a view
  that holds records: `define_view()` raises `SurrealDbError` ("… The key being inserted already
  exists") and the view stays as it was, where 3.2 and later, 2.x and the embedded engine
  recompute it. On 3.1, call `remove_view()` before `define_view()`.
- **Differences from the full SurrealDB-ORM**: the same `view_query` key and `_is_view()`, plus
  `get_view_query()`; the same `TypeError` first words on every write path (full ORM: `save()`,
  `update()`, `merge()`, `delete()` only); `define_view()`, `remove_view()` and `view_ddl()` where
  the full ORM applies a view through migrations or `define_table()`. The documented
  `MaterializedView` base class with `source_query` is not shipped by the full ORM either: declare
  `view_query` on a `BaseSurrealModel`.
- **Deferred**: applying a view through migrations (`CreateTable`/`AlterTable`) and
  `define_table()`, which will accept a view model and apply it through `view_ddl()`, come with
  v0.37.0; table permissions for record users and a view query built from a QuerySet come later.
- The 0.31.0 notes placed the `SurrealConfigDict` keys that generated classes stand in for with
  comments in v0.32.0–v0.34.0: `view_query` arrives with this release, and README section 31 now
  places the others (`table_type`, `permissions`, `changefeed`, `relation_in`, `relation_out`,
  `enforced`) in later minors.
- **Server pins unchanged**: tested against SurrealDB 3.3.4 and 2.7.0 and on the embedded engine,
  plus 3.1.5 for backward compatibility.

## [0.31.2] - 2026-10-19

Transaction errors give the server's reason and the failed statement's position, without the
server's excerpt of the statement, and a buffered commit raises `SurrealDbError` on the embedded
engine too (#278). No new public API.

### Fixed

- **Transaction errors give the server's reason and the statement's position, without the
  server's excerpt of the statement** (#278). When the server refused the query as a whole (a
  statement it cannot parse), the error read "Transaction failed: unrecognized query_raw()
  response shape: {…}" and quoted the server's raw response, its excerpt of the statement
  included. It now gives the server's reason and the position:

  - at a buffered commit (SurrealDB 2.x, HTTP, the embedded engine), "Transaction failed: the
    server refused statement 2 of 3: … (at line 1, column 7 of the statement)", the line and column
    of the statement as written, before the ORM renames its `$name` parameters for the batch; a
    delimiter left unclosed names the statement it opens in, even when the server first reports it
    in a later statement or on the batch's `COMMIT` line;
  - for a write or a definition in an interactive transaction (SurrealDB 3.x over WebSocket),
    "Transaction failed: the server refused the statement: … (at line 1, column 7 of the
    statement)";
  - a failed statement's error, "Transaction failed and rolled back: …", keeps only the first line
    of a reason that quotes the query;
  - a response of any other shape raises "Transaction failed: unrecognized query_raw() response
    shape (<type>)", without the response.

  The server's full text stays reachable as the error's `__context__`, which a formatted traceback
  does not print. A read in a transaction still raises the SDK's own error, as it does outside a
  transaction.

- **A buffered commit on the embedded engine raises `SurrealDbError`** (#278). The engine reports a
  query it cannot parse as a bare `RuntimeError`, which propagated out of the `transaction()` block;
  the commit now raises `SurrealDbError`, or `SurrealDbConflictError` for a retryable conflict, as
  on a server.
- **A statement holding conflict words is not taken for a conflict** (#278). When the server could
  not parse a statement holding words such as "write conflict", `is_conflict_error()` recognised
  the transaction's error as a retryable conflict, so `retry_on_conflict` retried it, and an
  interactive transaction's cancellation error was a `SurrealDbConflictError`. Conflicts are now
  recognised from the server's reason, not from its excerpt of the statement.

### Changed

- **The cancellation error names the failed statement's error type** (#278). Since v0.30.3,
  leaving an interactive `transaction()` block after a failed statement, or an explicit
  `tx.commit()`, raises "The transaction was cancelled: a statement in it failed; nothing was
  committed (…)", whose parenthesis repeated the failed statement's error text. The parenthesis now
  holds that error's type name, for example `(SurrealDbError)`. The error itself is still chained
  as `__cause__`, and a retryable conflict still raises `SurrealDbConflictError`, so
  `retry_on_conflict` behaves as before.
- **`is_conflict_error()` follows the chain a traceback prints** (#278): a context suppressed with
  `raise … from None` is no longer read, since the ORM keeps the server's full text there, which
  may quote the statement; `__cause__` and an implicit `__context__` are still followed. A conflict
  your code re-raises as another exception with `raise … from None` is therefore no longer
  recognised, nor retried by `retry_on_conflict`: chain it (`raise … from exc`, or `raise …`
  without `from` inside the `except` block), or raise a `SurrealDbConflictError`, to keep it
  retryable.

### Notes

- The v0.31.0 caution on the DDL appliers with `tx=` (#278), that an error raised when the
  transaction ends can carry the server's excerpt of the statement, no longer applies: that error
  now gives the server's reason and the statement's position instead. README sections 11 and 32
  and the embedded-engine table say so.
- Same behaviour on SurrealDB 2.x, 3.x and the embedded engine. Server pins unchanged: tested
  against SurrealDB 3.3.4 and 2.7.0 and on the embedded engine, plus 3.1.5 for backward
  compatibility.

## [0.31.1] - 2026-10-19

A datetime outside years 1-9999 no longer fails the response it is in (#279). SurrealDB stores
datetimes far beyond the years a Python `datetime` holds, and the ORM now reads them back as the
server's own text. No new public API.

### Fixed

- **Reading a datetime before year 1 or after year 9999** (#279). SurrealDB stores datetimes from
  year -262143 to +262142, on 2.x and 3.x, but the surrealdb SDK 2.0.0 decodes a datetime into a
  Python `datetime`, which holds years 1-9999 only: one such value failed the whole response it
  was in, whether it came from `get()`, `filter()`, `raw_query()` or a live notification. The
  ORM's clients now read it as its RFC 3339 text, the one SurrealDB gives it with `<string>`:
  `"0000-06-01T00:00:00Z"`, `"-0001-01-01T00:00:00Z"`, `"+10000-01-01T00:00:00.500Z"` (a sign and
  at least four digits outside years 0000-9999, every nanosecond kept). An untyped field holds
  that string; a `datetime` field refuses it with a `ValidationError` naming the field, so `get()`
  raises it and `filter().exec()` and `raw_query()` return such rows as dicts, as for any row
  that does not validate. Written back through the ORM, the value is stored as that string. A
  datetime within years 1-9999, before 1970 included, reads as the SDK's `datetime`, as before.

### Notes

- As for zero durations, native sets and degenerate geometries, only the clients the ORM opens
  (`SurrealDBConnectionManager.get_client()`, `ephemeral_client()`, the embedded engine's
  included) read these values; an `AsyncSurreal` client the application builds itself keeps the
  SDK's behaviour. The ORM leaves them to the SDK as soon as an installed SDK decodes them itself.
  README: "Values the SDK cannot read" and a new row of the 2.x/3.x behaviour table.
- The function catalogue test (`TestFunctionEnumsE2E`) calls `rand::time()` between 2000 and 2030
  and checks the result. Unbounded, it drew a year-0 datetime in about one call in 7,400 (27 in
  200,000 on each line), which made the test fail at random.
- Same behaviour on SurrealDB 2.x, 3.x and the embedded engine. Server pins unchanged: tested
  against SurrealDB 3.3.4 and 2.7.0 and on the embedded engine, plus 3.1.5 for backward
  compatibility.

## [0.31.0] - 2026-10-19

Server-side events and DDL helpers: the ORM writes and applies `DEFINE EVENT` and
`DEFINE FUNCTION` statements from Python. Events are declared on the model, next to its fields,
and applied idempotently; stored functions are defined for `call_function()` to call. The same
code runs on SurrealDB 2.x, 3.x and the embedded engine; asynchronous events and `$input` are 3.x
only. The second minor of Tier 3.

### Added

- **`Event(name, *, then, when=None, async_mode=False, retry=None, max_depth=None, comment=None)`**
  and **`SurrealConfigDict(events=[...])`**: a model declares its server-side events, which
  SurrealDB runs on every `CREATE`, `UPDATE` and `DELETE` of the table, whoever writes it (the
  ORM, a raw query or another client). `Model.event_ddl()` renders their `DEFINE EVENT` statements
  without a connection, `await Model.define_events()` applies them on the model's connection
  (`DEFINE EVENT OVERWRITE`: run it at start-up) and returns them, and
  `await Model.remove_event("name")` removes one by name, declared or not. `overwrite=False` writes
  `IF NOT EXISTS`, `missing_ok=False` raises `SurrealDbNotFoundError` for a missing event, and
  `Model.get_events()` lists the declared events. A subclass inherits `events` unless it sets its
  own, and typed edges take events too.

  ```python
  class Account(BaseSurrealModel):
      model_config = SurrealConfigDict(
          table_name="account",
          events=[
              Event(
                  "email_audit",
                  when="$event = 'UPDATE' AND $before.email != $after.email",
                  then="CREATE audit_log SET account = $value.id, old = $before.email, new = $after.email",
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


  Account.event_ddl()            # the DEFINE EVENT statements, no connection needed
  await Account.define_events()  # idempotent
  await Account(id="ada", email="ada@example.com", balance=-5).save()  # InternalError: refused
  ```

- **`SurrealDBConnectionManager.define_event(event, table)`** and
  **`remove_event(event, table)`** apply an `Event` to any table, with or without a model, on the
  current connection (the `using()` block's, else `"default"`); `event.ddl(table)` renders the
  statement. The `then` clause is always written as a block (`THEN { … }`), so several statements,
  `LET` and `THROW` work on both lines.
- **Asynchronous events (SurrealDB 3.x)**: `Event(..., async_mode=True, retry=3, max_depth=2)`
  writes `ASYNC RETRY 3 MAXDEPTH 2`. Such an event runs after the write's transaction commits, so
  it does not hold the write up. On SurrealDB 2.x and the embedded engine, which cannot parse
  `ASYNC`, every applier raises `SurrealDbError` before sending any DDL, and no event of the call
  is defined; `event.ddl()` renders an asynchronous event on any line.
- **`SurrealDBConnectionManager.define_function(function, body, *, parameters=None, ...)`**,
  **`remove_function(function)`** and the pure **`function_ddl()`** (exported from
  `surreal_orm_lite`): stored functions defined from Python, named as `call_function()` names them
  (`"greet"`, `"fn::greet"`, `"fn::billing::total"`). `parameters` maps each parameter name to its
  SurrealQL kind in declaration order, the order of `call_function()`'s positional `args`;
  `returns` is the result's kind, `permissions` is `"FULL"`, `"NONE"` or `"WHERE <condition>"`,
  and `comment=` and `overwrite=` work as for events.

  ```python
  await SurrealDBConnectionManager.define_function(
      "fn::greet",
      "RETURN string::concat('Hello, ', $name, '!');",
      parameters={"name": "string"},
      returns="string",
  )
  await SurrealDBConnectionManager.call_function("fn::greet", ["Ada"])  # 'Hello, Ada!'
  ```

- **Every applier takes `tx=`** and returns the statement it applied (`define_events()`: the
  list). A refusal raises
  `SurrealDbAuthenticationError` when the session may not define (a `VIEWER`, a record user, an
  anonymous session), `SurrealDbNotFoundError` for a removal of a missing definition with
  `missing_ok=False`, and `SurrealDbError` otherwise
  (`Can't apply event definition: 'email_audit' on table 'account' -> …`). Without `tx=`, a
  refusal names the event and its table, or the function, never the statement, whose `when`,
  `then` or body may hold a secret, and gives the server's reason without its excerpt of the
  statement. A buffered transaction (SurrealDB 2.x, HTTP, the embedded engine) only queues the
  statement, so a refusal comes back when the block commits, as the transaction's own error, never
  as `SurrealDbAuthenticationError` or `SurrealDbNotFoundError`, `missing_ok=False` included. When
  a definition is applied in a transaction (`tx=`), an error raised when the transaction ends can
  still carry the server's message, statement excerpt included (#278). `define_events()` applies
  one statement per event and stops at the first refusal: pass `tx=` for all or nothing. A `tx=`
  of another connection raises `SurrealDbError` before any request.
- README section 32, "Server-side events and stored functions (`Event`, `define_events()`,
  `define_function()`)" (declaring events on a model, events without a model, asynchronous events,
  stored functions, transactions and connections, SurrealDB 2.x and 3.x, security, a note for users
  of the full SurrealDB-ORM), a features row, seven rows in the 2.x/3.x behaviour table and one in
  the embedded-engine table, the comparison and roadmap rows, and the example notebook
  `examples/v0.31.0_events_ddl_helpers.ipynb`. Section 17's example declares its function with
  `define_function()`.

### Changed

- **`EventState` gains `async_mode`, `retry` and `max_depth`** (keyword-only, after `comment`;
  `False`, `None` and `None` by default), and **`DatabaseIntrospector` reads SurrealDB 3.x
  `ASYNC` events**: 0.30.x reported such an event as a `SchemaIssue`. SurrealDB 3.x renders
  `RETRY` and `MAXDEPTH` with every `ASYNC`, so an event defined without them reads back
  `retry=1, max_depth=3`.
- **`call_function(params=)` follows a redefinition made with `define_function()` or
  `remove_function()` at once**: both drop the function's cached signature on every connection of
  this process to the same namespace and database, whatever its URL (an admin connection applying
  the DDL over `http://` and the application's `ws://` connection alike), so even a reorder of the
  same parameter names is picked up. A signature read that such a drop, or
  `clear_function_signature_cache()`, overtakes is read again rather than cached. A function
  redefined with raw DDL or from another process still needs `clear_function_signature_cache()`.

### Notes

- **Names are validated and quoted, comments escaped**: event, table, parameter and function names
  are checked as identifiers before any request, event and table names are written in backticks
  (`update` or `delete` work as event names), and a comment is an escaped string literal. A
  parameter name SurrealDB defines itself (`auth`, `session`, `this`, …) is refused.
- **Expressions are trusted application code, never user input.** `when`, `then`, function
  bodies, kinds and permission conditions are written into the statement as given, since SurrealDB
  takes no bound parameter in a definition. Their structural check (strings terminated, brackets
  balanced, no NUL, no SurrealQL comment marker outside a literal, every `/` read as an operator,
  no `;` outside brackets where one expression is expected) catches mistakes — unbalanced
  brackets, a comment that would swallow the clauses the ORM appends, a second statement where one
  expression is expected — and is not an injection defence. To use a value, read it from the
  record (`$after.email`) or from a `DEFINE PARAM`. Every refusal is a `TypeError` or a
  `ValueError` raised before any request. An `Event`'s `repr()` leaves out `when` and `then`, and
  a refusal the server returns to a call without `tx=` names the event or the function, never its
  statement (with `tx=`, see #278).
- **Kinds are written in canonical form**: `none | int` and `option<int>` are both written
  `option<int>`, and a kind the ORM cannot read raises `ValueError` rather than being rewritten.
- **SurrealDB 2.x and 3.x**: `ASYNC` events are 3.x only (refused before any DDL elsewhere);
  `$input` holds the fields the write sent on 3.x and is `NONE` on 2.x and the embedded engine; a
  write that changes nothing fires no event, except on 3.x an `UPSERT` (the ORM's `upsert()` of an
  unchanged record), which fires one `UPDATE` event; read back, each line renders event and
  function definitions its own way (README section 32 and the 2.x/3.x table).
- **A write an event refuses** (a `THROW`, an error in `then`) raises the SDK's `InternalError`,
  as any server refusal of a write does, and nothing is written. Every applier takes `tx=`, and
  events work inside `transaction()` like any other statement.
- **Query cache, live queries and signals**: the query cache forgets the tables the ORM itself
  writes, not the rows an event writes, so do not cache reads of a table events write, or call
  `QueryCache.invalidate(Model)` after the write. Live queries see those rows. Applying DDL and the
  rows events write fire no ORM signal.
- **Applying a definition needs a privileged system user** (root, or a namespace or database
  `OWNER` or `EDITOR`): run `define_events()` and `define_function()` at start-up or deployment.
- **Differences from the full SurrealDB-ORM**: events are declared in
  `SurrealConfigDict(events=[...])` rather than a `class Meta`, and the lite's `Event` adds
  `max_depth=`; `then` is written `THEN { … }` where the full ORM writes `THEN (…)`, in which `LET`
  and `THROW` fail; names are quoted and comments escaped with a backslash; `define_function()`
  has no full-ORM equivalent (the full ORM uses `RawSQL`).
- **Deferred**: the migration operations (`DefineEvent`, `RemoveEvent`, a function operation) and
  the removal of events a model no longer declares come with v0.37.0 (migrations):
  `define_events()` leaves such an event defined until `remove_event()` removes it.
  `generate_models_from_db()` lists a table's events in a class comment, not as `events=[...]`.
- The `SurrealConfigDict` keys that generated classes stand in for with comments (`table_type`,
  `permissions`, `changefeed`, `view_query`, `relation_in`, `relation_out`, `enforced`), which the
  0.30.0 and 0.30.8 notes placed in v0.31.0–v0.33.0, are not part of this release: README section
  31 now places them in v0.32.0–v0.34.0.
- **Server pins unchanged**: tested against SurrealDB 3.3.4 and 2.7.0 and on the embedded engine,
  plus 3.1.5 for backward compatibility.

## [0.30.8] - 2026-10-19

Follow-ups to the v0.30.0 schema introspection (#260): a connection lost while the schema is read
raises instead of becoming schema issues, a relation table is an edge whatever the case of its
`table_type`, and generated classes carry the change feed and permissions comments the v0.30.0
notes described. No new public API.

### Fixed

- **A connection lost during `introspect()` raises** (#260). Every error of an `INFO FOR TABLE`
  became a `SchemaIssue`, so a connection that dropped after `INFO FOR DB` turned every remaining
  table into "could not be read": `introspect()` returned normally, and
  `generate_models_from_db(path)` replaced a working module with one that listed those tables as
  skipped. Only the server's answer to `INFO FOR TABLE` is an issue now; the transport's error, or
  the ORM's, propagates unchanged before anything is written, so an existing file is left as it
  was.
- **A relation table is an edge whatever the case of its `table_type`** (#260). The generator
  compared `table_type` with exact case in two places and ignored case in a third: a hand-built
  `TableState(table_type="RELATION")` gave a `BaseSurrealEdge` class that declared `in_` and `out`
  again, and the module failed to import with a `TypeError`. States read by `DatabaseIntrospector`
  hold `"relation"` and were not affected.
- **Generated classes comment their change feed and permissions** (#260), as the v0.30.0 notes
  said: `# Changefeed: 1d INCLUDE ORIGINAL` and
  `# Permissions: FOR select WHERE $auth.id = owner, FOR create, update FULL, FOR delete NONE`,
  grouped the way SurrealDB renders them (`# Permissions: FULL` when `FULL` covers the four
  operations). A table left at the default `PERMISSIONS NONE`, all four operations `NONE`, gets no
  permissions comment. The database still applies both; the `SurrealConfigDict` keys for them
  arrive with v0.31.0–v0.33.0. Unlike what those notes listed, `table_type` is not a comment: it
  shows through the base class, `BaseSurrealEdge` for a relation table.

### Changed

- **An `INFO FOR TABLE` refused after `INFO FOR DB` succeeded raises
  `SurrealDbAuthenticationError`** (#260) instead of becoming a `SchemaIssue`: the session lost its
  rights while the schema was read (for example a client closed and reopened anonymously), and the
  table is not at fault. A refused `INFO FOR DB` raises the same error, as in v0.30.0.

### Notes

- Docstrings and comments no longer cite internal design-note ids: they name the exception raised,
  the case refused or the README section that documents it. README section 31 describes the new
  class comments and which `INFO FOR TABLE` failures are schema issues.
- The linear-time tests of the definition parser keep the fastest of up to three timings, so a
  pause of a shared CI runner cannot fail them.
- Same behaviour on SurrealDB 2.x, 3.x and the embedded engine. Server pins unchanged: tested
  against SurrealDB 3.3.4 and 2.7.0 and on the embedded engine, plus 3.1.5 for backward
  compatibility.

## [0.30.7] - 2026-10-19

An instance stays in step with its stored row (#261): `save()` applies the whole row the server
returns, every call that applies a returned row validates it as a read does, and `update()` keeps
the stored nanoseconds of a datetime it did not change, so a record with a top-level `READONLY`
datetime can be updated. No new public API, but what an instance holds after a write changes for
every model: read **Changed** before upgrading.

### Fixed

- **`update()` after `save()` or `get()` on a `READONLY` top-level `datetime`** is no longer
  refused by the server (#261). SurrealDB stores nanoseconds and the SDK reads microseconds, so
  `update()` wrote back a changed value. See item 5 under Changed.
- **Sub-fields a `DEFAULT` fills** (`prefs.at DEFAULT time::now()`) are on the instance right after
  `save()` (explicit id, auto id, interactive transaction), so a following `update()` no longer
  sends them as `NONE`, which the server refused for a required sub-field and which erased an
  optional one (#261).
- **`tuple` and `set` fields** hold their declared type after `save()`, `upsert()`, `merge()` and
  `patch()`, as after `get()` (item 2 under Changed), not the SDK's `list` (#261).
- **A returned column named like a Pydantic `@computed_field` or a model method** (`json`) no
  longer replaces that attribute (#261). In 0.30.1, with a `@computed_field` (which `model_dump()`
  writes), auto-id `save()`, `refresh()` and `merge()` raised `AttributeError` after the record was
  stored.
- **`generate_models_from_db()`**: generated classes no longer carry the note about `DEFAULT`
  sub-fields, and the `READONLY` `update()` note remains only for a datetime below the top level
  or a top-level one past the 32nd (#261).
- **Buffered transactions**: a statement's parameters are renamed in one pass (#261). In 0.30.1,
  `$a` next to a variable named `t0_a` was renamed twice and bound to `t0_a`'s value, and a
  statement with thousands of bound values was slow to queue.

```python
# DEFINE FIELD created ON event TYPE datetime DEFAULT time::now() READONLY;
event = await Event.objects().get("launch")  # created read to the microsecond
event.title = "Launch day"
await event.update()  # was refused ("readonly"); the stored created keeps its nanoseconds
```

### Changed

Each item was measured against 0.30.1 on SurrealDB 3.3.4, 2.7.0 and the embedded engine
(`mem://`) and holds on all three unless stated.

1. **Explicit-id `save()`, an edge's `save()` and interactive `save(tx=)` apply the whole stored
   row** (#261). In 0.30.1, explicit-id `save()` and interactive `save(tx=)` read back only the
   `server_fields` and computed fields, and an edge's `save()` (either id) only its `id` and
   `server_fields`: the instance kept the values passed in. Now a naive `datetime` becomes the
   aware UTC `datetime` a read gives (compare it with aware values), a sub-model field holds a new
   instance (not the object passed in), and values a `VALUE` clause rewrote or the server coerced
   replace the ones sent (`VALUE string::lowercase($value)` turns `"AbC"` into `"abc"` on the
   instance). Interactive transactions exist on SurrealDB 3.x over WebSocket only; a buffered
   transaction (HTTP, 2.x, the embedded engine) still leaves the instance as it was.
2. **Every call that applies the returned row validates it like a read** (#261): `save()` with
   either id, an edge's `save()`, `refresh()`, `merge()` (default `refresh=True`), `upsert()`,
   `patch()` and the `atomic_*` helpers. An enum field holds the enum member, not the raw
   `str`/`int` (a `str` or `int` enum still compares equal to its value, a plain `Enum` member no
   longer does); `tuple`, `set`, `date` and `Range` fields hold their declared type instead of the
   SDK's `list`, midnight `datetime` and `Range`; a `float` field stored in a `decimal` column holds
   a `float`, not a `Decimal`; a `list[Duration]` holds the ORM's `Duration`s, not the SDK's. In
   0.30.1, `refresh()`, `merge()`, auto-id `save()`, `upsert()`, `patch()` and `atomic_*` kept the
   SDK's values for these fields (explicit-id `save()` kept the values passed in).
3. **The model's validators run on the returned row for every model** (#261). Field and model
   validators (any mode) and `model_post_init` run once per call listed in item 2, on a validated
   copy of the row, and their result is applied to every field. In 0.30.1 they ran only when the
   row held a sub-model field, a nested `timedelta` field or a geometry field stored as a GeoJSON
   object, and their result reached only those fields; otherwise they did not run at all. A
   validator that is not idempotent is applied again, as on `get()`, and a following `update()`
   writes that value back: with `label` validated as `v + "!"`, `Model(id="a", label="x")`,
   `save()` then `update()` stored `x!` in 0.30.1 and stores `x!!` now; `merge()` then `update()`
   gives the same difference.
4. **Under `extra="allow"`, the returned row's undeclared columns go to `model_extra`** after the
   calls listed in item 2, where a read puts them (0.30.1 dropped them), and `merge(refresh=False)`
   with an undeclared key updates `model_extra` (#261).
5. **`update()` and `upsert()` keep the stored nanoseconds of a datetime written back unchanged**
   (one that still floors to the stored value at the microsecond), for every top-level datetime up
   to 32 per record, so a record whose `READONLY` datetimes are top-level fields can be updated
   after `get()` or `save()` (#261). Below the top level (a sub-model, a `list`, a `tuple`, a set)
   this depends on the data: change such a record with `merge()`.

### Notes

- SurrealDB 2.x still refuses `update()` and `upsert()` of an existing record that has a field
  with a non-optional `TYPE` and a `VALUE` clause the model does not write, such as a generated
  `updated: Computed[datetime]` (#267): change such a record with `merge()` there. 0.30.1 behaved
  the same way before this release; SurrealDB 3.x recomputes the field. The v0.30.0 example
  notebook changes its generated `NbPerson` with `merge()` for that reason.
- The README's "After a write that returns the row", "Precision and `Decimal`" and "What the
  generator does not do" sections, and a new row of the 2.x vs 3.x behaviour table, describe these
  changes.
- Server pins unchanged: tested against SurrealDB 3.3.4 and 2.7.0 and on the embedded engine, plus
  3.1.5 for backward compatibility.

## [0.30.5] - 2026-10-19

`SurrealDBConnectionManager.kill()` no longer reports a connection lost under the request as a
KILL the server refused. No new public API.

### Fixed

- **A `kill()` caught by a dropped connection returns normally** (#266). A KILL sent as the
  WebSocket went down, before the SDK had noticed the drop, failed inside the SDK (its receive loop
  clears the pending requests, so the request's own cleanup raised a `KeyError`), and `kill()`
  turned that into `SurrealDbError: Failed to kill live query …` although the server never
  answered. `kill()` now asks the client: when its connection dropped under the request (the SDK's
  receive task has ended, after a wait of at most one second when the error came from the
  transport), it closes the dead socket and records the live query as ended, as it already did for
  a client found dead before the call, and the stream's readers end. A refusal from a live server
  still raises `SurrealDbError` and keeps the uuid alive, so a retry can re-attach to it.
- **A cancelled `kill()` raises `asyncio.CancelledError`** (#266), even when the SDK reports the
  cancelled request as an ordinary error; it used to become `SurrealDbError`. The readers are
  released either way.
- **`kill()` releases the stream's readers before it closes a dead socket** (#266), so an
  automatic resubscribe woken by the same drop cannot reopen a subscription that is being stopped.
  The window was open where closing the socket yields to the event loop, as on Python 3.11.

### Notes

- Docs: the README live-queries section says that a kill caught by a dropped connection is not a
  refusal.
- Same behaviour on SurrealDB 2.x and 3.x (live queries are WebSocket-only). Server pins
  unchanged: tested against SurrealDB 3.3.4 and 2.7.0, plus 3.1.5 for backward compatibility.

## [0.30.3] - 2026-10-19

A statement that fails inside `transaction()` fails the whole transaction on every strategy: a
transaction block now behaves the same on SurrealDB 2.x and 3.x, over WebSocket, over HTTP and on
the embedded engine. No new public API.

### Changed

- **A failed statement fails the whole `transaction()` block, on every strategy** (#268). After a
  statement fails inside the block, even when the block catches its error and carries on, the
  transaction is cancelled when the block ends, nothing of the block is committed, and leaving the
  block raises `SurrealDbError`, or `SurrealDbConflictError` when the failure was a retryable
  conflict, so `retry_on_conflict` retries the block. Interactive transactions (SurrealDB 3.x over
  WebSocket) now behave like the buffered ones (SurrealDB 2.x, HTTP, the embedded engine), which
  already failed as a whole at commit: the failed statement raises its error at once, then leaving
  the block raises "The transaction was cancelled: a statement in it failed; nothing was committed
  (…)", chained to that error.
  - An explicit `tx.commit()` after a failed statement raises the same error and cancels the
    transaction.
  - When the block's own exception propagates, that exception is the one raised, as before.
  - Reads the ORM answers itself do not fail the transaction: a table that does not exist reads as
    no rows, and `get()` raises `SurrealDbNotFoundError`.
  - A statement interrupted inside the block (a timeout or a task cancellation) fails the
    transaction too.

  Upgrade note: a block that catches a statement's error and carries on now raises when it ends,
  on every strategy, as it already did on the buffered ones. To carry on after such an error, run
  that statement in a transaction of its own or outside the transaction.

```python
async with SurrealDBConnectionManager.transaction() as tx:
    await User(id="alice", name="Alice").save(tx=tx)
    try:
        await Order(id="o2", user="User:alice", total=-1).save(tx=tx)  # refused by a field ASSERT
    except SurrealDbError:
        pass  # caught, but the transaction can no longer commit
# leaving the block raised SurrealDbError: alice was not saved either
```

### Fixed

- **The error of a failed statement, "Transaction failed and rolled back: …", now describes what
  happens on every strategy** (#268): on an interactive transaction as on a buffered one, nothing
  of the block is committed.

### Notes

- Docs: the README transactions section (11) states the rule with an example, and the "ORM
  behaviour: SurrealDB 2.x vs 3.x" table gains a row for a statement that fails inside
  `transaction()` with its error caught in the block.

## [0.30.0] - 2026-10-19

Schema introspection: the ORM reads the schema of a database as immutable typed objects and
generates `BaseSurrealModel` classes from it, with the full SurrealDB-ORM's names. It only reads
(`INFO FOR DB`, `INFO FOR TABLE`), and the same schema gives the same state and the same generated
types on SurrealDB 2.x, 3.x and the embedded engine. The first minor of Tier 3.

### Added

- **`DatabaseIntrospector(connection=None, *, tables=None).introspect()`** returns a
  `SchemaState`: the tables (`TableState`, with their `FieldState`, `IndexState` and `EventState`)
  and the database's accesses (`AccessState`), analyzers (`AnalyzerState`), functions
  (`FunctionState`, keyed `"fn::name"`) and params (`ParamState`). It sends `INFO FOR DB;`, then
  one `INFO FOR TABLE` per table, outside any transaction: no record is read, nothing is defined or
  written. A field is keyed by its path (`"address.city"`, `"tags.*"`), with `field_type` (its kind
  without the optional wrapper), `nullable` (whether it may be `NONE`) and its expressions
  (`default`, `value`, `computed`, `assertion`, permission rules) as SurrealQL text; every
  definition keeps the server's statement in `raw`, and the sections the ORM does not model are in
  `schema.other`. `connection=` is a connection name (else the current one: `using()`, then
  `"default"`), and `tables=` reads those tables only.

  ```python
  from surreal_orm_lite import DatabaseIntrospector

  schema = await DatabaseIntrospector().introspect()
  person = schema.tables["person"]
  person.fields["age"].field_type       # 'int': option<int> on SurrealDB 2.x, none | int on 3.x
  person.fields["age"].nullable         # True
  person.fields["age"].default          # '18': SurrealQL text
  person.indexes["person_name"].unique  # True
  ```

- **`ModelCodeGenerator().generate(state)`** and
  **`generate_models_from_db(output_path=None, connection=None, *, tables=None, overwrite=True)`**
  give deterministic source with one class per table: a `BaseSurrealModel`, or a `BaseSurrealEdge`
  for a `TYPE RELATION` table. Fields use the lite's types (`Duration`, `Decimal`, `UUID`,
  `RecordID`, `Range`, the geometry classes, `Literal`), sub-models for declared nested fields,
  `computed()` for `COMPUTED` and for a `VALUE` that reads neither `$value` nor `$input`, aliases
  for columns that cannot be attribute names, `extra="allow"` for `SCHEMALESS` tables, and
  `connection=` for a schema read from a named connection. A constant `DEFAULT` becomes the Python
  default (`DEFAULT 18` → `= 18`, `[]` → `Field(default_factory=list)`, `1h30m` →
  `Duration.parse("1h30m")`, `u'…'` → `UUID("…")`); any other `DEFAULT` becomes a `server_fields`
  entry. Names, comments and expressions read from the database reach the source only as escaped
  string literals, single-line comments and generated identifiers.

  ```python
  from surreal_orm_lite import generate_models_from_db

  source = await generate_models_from_db()                  # the source, as a string
  await generate_models_from_db("app/models_generated.py")  # also writes it, replacing the file
  await generate_models_from_db("app/models_generated.py", overwrite=False)  # FileExistsError
  ```

- **`generate_models_from_db()` replaces an existing file atomically**, as the full ORM replaces
  it: the source goes to a temporary file in the same directory, then `os.replace()` moves it over
  the file, so a failure leaves the previous file. `overwrite=False` refuses an existing path with
  `FileExistsError` before reading anything.
- **The state classes take the full SurrealDB-ORM's arguments**: `SchemaState`, `TableState`,
  `FieldState`, `IndexState`, `EventState`, `AccessState` and `AnalyzerState` are built by
  position or by keyword as in the full ORM, and the lite's own attributes (`raw`, `path`, `table`,
  `comment`, …) are keyword-only. They are frozen dataclasses with read-only maps and tuples, and
  `repr()` shows names and structure only, never a definition's text. `FunctionState`,
  `ParamState`, `SchemaIssue` and `tables=` are lite additions. The 13 names are exported from
  `surreal_orm_lite`.
- **Nothing is guessed**: a definition that cannot be parsed exactly, a section of an unexpected
  shape or a failed `INFO FOR TABLE` becomes a `SchemaIssue` in `schema.issues` and a WARNING on
  the `surreal_orm_lite.introspection` logger, whose message never quotes the definition.
  `introspect()` raises `SurrealDbAuthenticationError` when the session may not run
  `INFO FOR DB`, `SurrealDbError` when it fails otherwise, and `SurrealDbNotFoundError` for a
  `tables=` name the database does not define.
- README section 31, "Schema introspection (`DatabaseIntrospector`, `generate_models_from_db()`)"
  (reading the schema, generating models, how SurrealDB kinds become fields, what the generator
  does not do, SurrealDB 2.x and 3.x, permissions, a note for users of the full SurrealDB-ORM), a
  features row, rows in the 2.x/3.x and embedded-engine behaviour tables, the comparison and
  roadmap rows, and the example notebook `examples/v0.30.0_schema_introspection.ipynb`.

### Fixed

- **A stored function's parameter list is read in linear time** (`call_function()` with keyword
  arguments, and now introspection): a long `DEFINE FUNCTION`, or one holding `` $` `` or `$⟨`
  without a closing quote, no longer makes the reader copy or search the rest of the definition at
  every `$`.

### Notes

- **Kinds and field paths are normalised, expressions are kept as rendered**: `option<int>` (2.x)
  and `none | int` (3.x) both give `field_type == "int"` and `nullable is True`, and `tags[*]`
  (2.x) and `tags.*` (3.x) both give the field `"tags.*"`. Expressions (`default`, `value`, `raw`,
  a generated `computed("…")`) are kept as each line renders them and may differ:
  `VALUE IF b THEN 1 ELSE 2 END` on 2.x is `VALUE (IF b THEN 1 ELSE 2 END)` on 3.x.
- **Definitions only one line has** are read and typed on that line: on 3.x `COMPUTED`,
  `REFERENCE`, `FULLTEXT` indexes, the `file` kind, `WITH REFRESH` and `BEARER` accesses, and
  `DEFINE SEQUENCE` (kept raw in `schema.other`); on 2.x and the embedded engine `MTREE` and
  `SEARCH ANALYZER` indexes and `geometry<feature>`.
- **The `users` section is never read.** On SurrealDB before 3.2.2 (every 2.x release, 3.x before
  3.2.2, and the embedded engine of SDK 2.0.0), `INFO FOR DB` shows user password hashes to every
  role that may run `INFO`, per SurrealDB's `INFO` documentation; introspection never reads,
  stores, logs or writes them. Introspection needs a system user (root, a namespace or a database
  user): a record user or an anonymous session gets `SurrealDbAuthenticationError`, except on the
  embedded engine, whose anonymous session owns the database.
- **Tables not generated**, listed in the module's header and still in `schema.tables`: a table
  named like a SurrealQL keyword, in any case (`select`, `create`, `only`, …), and a table whose
  name is not an identifier, which the ORM's statements cannot use as a table name.
- **`update()` and `READONLY` datetimes**: an instance holding a `READONLY` `datetime` field cannot
  be changed with `update()`, after `get()` as after `save()`: Python keeps microseconds, SurrealDB
  nanoseconds, and the server refuses the changed value. This predates v0.30.0; the generated class
  says so: change such a record with `merge()`.
- **Sub-fields a `DEFAULT` fills**: a sub-field filled by a `DEFAULT` the model does not hold
  (`prefs.at DEFAULT time::now()`, or any `DEFAULT` under a `dict[str, Any]` field) stays `None` on
  the instance after `save()`, which reads back only `server_fields` and computed fields; a
  following `update()` sends it as `NONE`: the server refuses it when the sub-field is required and
  erases the value when it is optional. The generated class says so: call `refresh()` after
  `save()` before an `update()`, or change the record with `merge()`.
- **Defaults are copied when the models are generated**: generate them again after changing a
  `DEFAULT`. A value set on a `server_fields` field before `save()` is not written, as since
  v0.18.0 and in the full ORM: set it with `merge()` once the record exists.
- `array<T, N>` becomes `list[T]`, leaving the length to the server (SurrealDB 2.x accepts at most
  N items, 3.x exactly N); `null | T` becomes a required `T | None`, which the ORM sends as `NONE`
  and `null | T` refuses: declare such a field `option<T>`. `ASSERT`, `PERMISSIONS` and field
  comments are not generated, and `READONLY` is a comment. Never call `define_computed_fields()` on
  a generated model.
- **Differences from the full SurrealDB-ORM**: the state is immutable, with tuples where the full
  ORM has lists; permission values are `'FULL'`, `'NONE'` or `'WHERE <expr>'`; `default` is
  SurrealQL text; `nullable` is false for `null | T`; accesses are database-level
  (`schema.accesses`); a refused or failed `INFO FOR DB` raises and an unreadable definition
  becomes a `SchemaIssue`; `connection=` is a name, and a client object raises `TypeError`;
  generated models use comments for the `SurrealConfigDict` keys that arrive with v0.31.0–v0.33.0
  (`table_type`, `permissions`, `changefeed`, `view_query`, `relation_in`, `relation_out`,
  `enforced`); class names are not singularised. `schema_diff()` and `SchemaState.diff()` come with
  v0.37.0 (migrations), the `inspectdb` command with v0.38.0 (CLI).
- **Server pins unchanged**: tested against SurrealDB 3.3.4 and 2.7.0 and on the embedded engine,
  plus 3.1.5 for backward compatibility.

## [0.29.3] - 2026-10-18

`get_or_create()` and `update_or_create()` bind the queryset's `variables()` in their lookup, as
`filter()` does. No new public API.

### Fixed

- **`get_or_create()` and `update_or_create()` bind `variables()` in their lookup** (#252). The
  lookup ran without the queryset's `variables()`, so a `Var` criterion read `NONE` there:
  `variables(n=10).get_or_create(size__gte=Var("n"), …)` compared `size` with `NONE` and could
  match a record the caller did not mean, which `update_or_create()` then updated, and
  `variables(t="mine").get_or_create(title=Var("t"))` did not find the existing record. The lookup
  now binds the queryset's `variables()`, merged with those of its subquery criteria as `filter()`
  merges them (a subquery that binds one of these names to another value raises `ValueError`), on
  SurrealDB 2.x, 3.x and the embedded engine.
- **An `exact` `Var` criterion naming a variable of `variables()` is written as that variable's
  value on create** (#252), so the next identical call finds the record:
  `variables(t="mine").get_or_create(title=Var("t"))` behaves as `get_or_create(title="mine")`. On
  the record id and on an edge's `in` and `out`, it is compared as the record id its value names,
  as the same criterion given that value is (`variables(a="person:alice")` with
  `get_or_create(in_=Var("a"), out=bob)` finds or relates `person:alice`); a value that names no
  record id there raises `ValueError` before any request. A model instance given to `variables()`
  is bound and written as its record id, as the instance given as the criterion is; an unsaved one
  raises `ValueError` before any request. An `exact` `Var` that names a session parameter
  (`Var("auth")`) or no variable of `variables()` still raises `SurrealDbError` on create.

```python
from surreal_orm_lite import Var

qs = Room.objects().variables(t="mine", n=10)
room, created = await qs.get_or_create(title=Var("t"))  # as get_or_create(title="mine")
room, created = await qs.update_or_create(size__gte=Var("n"), title=Var("t"), defaults={"floor": 2})
```

### Changed

- **Refusals of a `"$name"` criterion in `get_or_create()` and `update_or_create()`** (#252): a
  string criterion that is exactly `$` followed by the name of a variable of a subquery criterion's
  `variables()` now raises `SurrealDbError` too, like one naming a variable of the queryset's
  `variables()`. For a variable, the message names `Var('x')` as the criterion to write, under any
  lookup but `like`/`ilike`, which take no `Var` (pass the pattern itself, or use `regex`). For a
  session parameter, it says to pass the actual value under `exact`, and that `Var('auth')` works
  under any other lookup but `like`/`ilike`, which take no `Var` (pass the pattern itself, or use
  `regex`): `members__contains=Var("auth")` is looked up, never written. On the `id` column it names
  the record id `table:⟨$name⟩` the string would be.

### Deprecated

- The `"$$"` escape of a string filter value, deprecated since v0.28.1 (#250) and listed there
  under Changed: a string filter value starting with `$$` loses one `$` (`"$$admin"` finds
  `"$admin"`) and emits a `DeprecationWarning`. A string filter value is always data, so write the
  value with one `$` less. An item of an `in` list never had the escape and is unchanged.

## [0.29.2] - 2026-10-18

The text lookups `startswith`, `endswith`, `like`, `ilike`, `match` and `regex` work: each compiles
to a SurrealQL string function. No new public API.

### Fixed

- **Text lookups compile to SurrealQL string functions** (#251). They compiled to infix operators
  SurrealQL does not have (`STARTSWITH`, `LIKE`, `REGEX`…), so every such filter was a parse error
  on SurrealDB 2.x, 3.x and the embedded engine, in reads and in live queries. Each now compiles to
  a string function guarded by the field's type, with its value bound:
  `filter(name__startswith="A")` is `((name).is_string() AND string::starts_with(name, $_f0))`.
  - `startswith` and `endswith`: `string::starts_with` and `string::ends_with`.
  - `match` and `regex`: `string::matches`, the regular expression (SurrealDB's syntax, the Rust
    `regex` crate) as given. `match` is a regex lookup, not the full-text `@@` operator: full-text
    search is planned for v0.34.0.
  - `like` and `ilike`: `string::matches` on the pattern translated to an anchored regular
    expression. `%` matches any run of characters (newlines included), `_` exactly one, every other
    character is literal; `ilike` ignores case. The whole value must match: `like="Al"` finds
    `"Al"`, not `"Alice"`.
  - A record whose field is missing (`NONE`), `NULL` or not a string never matches, and the query
    does not fail on it; the negation (`~Q(name__startswith="A")`) matches it. A dotted path
    through an array of record links or objects (`authors.name`) holds an array: it never matches
    either.

```python
await User.objects().filter(name__startswith="A").exec()
await User.objects().filter(email__ilike="%@EXAMPLE.com").exec()  # case-insensitive
await User.objects().filter(name__regex=r"^\d{3}").exec()
```

### Changed

- **The value of a text lookup must be a `str`** (#251), otherwise `ValueError`. A string starting
  with `$` is data, as for every lookup. `startswith`, `endswith`, `match` and `regex` also take a
  `Var`; `like` and `ilike` raise `ValueError` for one, since their pattern is translated before it
  is sent. These calls were parse errors before, so no call that worked changes.

### Notes

- Same behaviour on SurrealDB 2.x, 3.x and the embedded engine: the guard is written in the method
  form `(field).is_string()`, which parses on both lines (`type::is::string` parses on 2.x only,
  `type::is_string` on 3.x only). The path is in parentheses because SurrealDB 3.x applies
  `authors.name.is_string()` to each element of the array such a path gives, and the array of
  booleans let the array reach the string function, which failed the query. The README's behaviour
  table has a row for the text lookups.
- `surreal_orm_lite.constants.LOOKUP_OPERATORS` (internal) no longer lists the text lookups; they
  are in `TEXT_LOOKUP_FUNCTIONS`.

## [0.29.0] - 2026-10-18

Multiple databases: one process works with several databases at once. Named connections stand
beside the default one, each model reads and writes through its own, and `using()` redirects a
block of code. The seventh and last minor of Tier 2.

### Added

- **Named connections**:
  `SurrealDBConnectionManager.add_connection(name, *, url, user=None, password=None, namespace, database)`
  registers another database beside the default connection (`set_connection()`): another database
  on the same server, another server, or the embedded engine. It opens nothing: each connection opens its own clients on first use, as the default
  one does. `remove_connection(name)`, `get_config(name)` (a frozen `ConnectionConfig`, exported
  from `surreal_orm_lite`) and `list_connections()` complete the registry, with the full
  SurrealDB-ORM's names.

  ```python
  from surreal_orm_lite import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager

  SurrealDBConnectionManager.set_connection(url="mem://", namespace="shop", database="main")  # "default"
  SurrealDBConnectionManager.add_connection("analytics", url="mem://", namespace="shop", database="analytics")

  class Customer(BaseSurrealModel):  # no connection key: "default"
      id: str | None = None
      name: str

  class PageView(BaseSurrealModel):
      model_config = SurrealConfigDict(connection="analytics")
      id: str | None = None
      path: str

  await Customer(id="ada", name="Ada").save()  # shop/main
  await PageView(id="v1", path="/pricing").save()  # shop/analytics

  async with SurrealDBConnectionManager.using("analytics"):
      elsewhere = await Customer.objects().count()  # 0: Customer runs on "analytics" here
  ```

- **`add_connection()` checks its arguments** before registering anything: a non-empty name, URL,
  namespace and database, and a user and password for a server URL (optional on the embedded
  engine); a URL the installed SDK refuses raises `SurrealDbConnectionError`. Registering a name
  again with the same settings changes nothing, and `add_connection("default", …)` configures the
  default connection with these checks. `ConnectionConfig` keeps the password out of its `repr`.
  `remove_connection()` of a name that is not registered is a no-op, and
  `remove_connection("default")` is `unset_connection()`.
- **Model routing**: `SurrealConfigDict(connection="analytics")` sends every database call of the
  model to that connection: writes, reads and every QuerySet, relations, `raw_query()`,
  `call_function()`, model auth, live queries, change feeds and `as_of()`. A model without the key
  uses the default connection. `Model.get_connection_name()` returns the connection its calls use
  right now. The key is checked when the class is defined (`TypeError` for a value that is not a
  string, `ValueError` for an empty one), and whether its name is registered at the model's first
  database call (`ValueError`), so models can be declared before `add_connection()` runs. Edge
  models and `AuthenticatedUserMixin` models take it too; subclasses inherit it.
- **`using(name)`**: inside `async with SurrealDBConnectionManager.using("analytics"):` every model,
  whatever its key, and every manager method that talks to a database use that connection. It is
  async-safe (a `ContextVar`: a task created inside the block keeps its connection), blocks nest,
  and the outer connection is back after the block, on an exception too; a name that is not
  registered raises `ValueError` before the block runs. `get_active_connection_name()` returns the
  block's name, `None` outside any block.
- **`name=` on `get_client()`, `close_connection()` and `transaction()`**: they act on a named
  connection, and without it on the current one. `inspect.signature()` does not show the
  parameter, so `Depends(SurrealDBConnectionManager.get_client)` in FastAPI adds no `?name=` query
  parameter and behaves as in v0.28: a request never chooses the connection.
- **Each connection keeps its own state**: its clients (one per event loop; one for the whole
  process on the embedded engine), its session (`signin()` inside `using(name)` signs in that
  connection only, and a stored token is replayed on its own connection only), its live streams
  and change feeds (bound to the connection when they start), its transactions, its
  stored-function signatures and its cached reads.
- **`embedded_database(connection=name)`** isolates a named connection in tests; inside
  `using(name)` it isolates the block's connection, and naming another connection there raises
  `ValueError`.
- **Errors for operations across connections**, raised before any request: `SurrealDbError` for a
  model given another connection's transaction (its writes, `objects(tx=)`, `refresh(tx=)`,
  `call_function(tx=)`), for a subquery whose model runs on another connection than the statement
  around it, for `relate()`, `remove_relation()`, `remove_all_relations()` and `get_related()`
  across connections, and for following a link (`fetch()`, a dotted path) through a field typed
  with a model of another connection; `SurrealDbConnectionError` (not retryable) when a second
  connection opens an on-disk embedded store (`surrealkv://`, `file://`) that another connection
  of the process holds open. Inside a `using()` block every model runs on one connection, so none
  of them apply there.
- README section 30, "Multiple databases (`add_connection()`, `using()`)" (routing a model,
  `using()`, transactions and other operations across connections, what each connection keeps for
  itself, which connection a manager method uses, a note for users of the full SurrealDB-ORM), a
  features row, rows in the 2.x/3.x and embedded-engine behaviour tables, the comparison and
  roadmap rows, and the example notebook `examples/v0.29.0_multi_database.ipynb`.

### Changed

- `close_connection()` ends the live streams of its own connection only (before: every live
  stream of the event loop), and `unset_connection()` closes the default connection's clients only
  (`close_all_connections()` still closes every client, of every connection). With the default
  connection alone, both behave as before.
- A `transaction()` records the connection it was opened on: a model of another connection is
  refused (above), and a transaction whose connection is registered again with other settings, or
  removed, while it is open is cancelled when its block ends, with nothing committed
  (`SurrealDbConnectionError`: open it again).
- The manager's state now lives in one class per connection, the default one included: code that
  read or patched the private attributes of `SurrealDBConnectionManager` must follow it. A manager
  method replaced by hand, public or private, is inherited by every connection: put it back from
  `vars(SurrealDBConnectionManager)[name]`, as pytest's `monkeypatch.setattr()` and
  `unittest.mock.patch.object()` already do. Putting back `SurrealDBConnectionManager.<name>`, saved
  before the patch, worked with one connection; it is a method bound to `SurrealDBConnectionManager`
  that every connection then inherits and that acts on the current connection, so once a named
  connection exists, a model with a `connection` key reads and writes the current connection's
  database, with no error (README section 30, "Which connection a manager method uses").
- CI adds a "Mixed server lines" job, required by `CI Success`: the mixed-lines tests put a
  SurrealDB 3.x and a 2.x connection side by side in one process; they find the servers through
  `SURREALDB_PORT_3X` and `SURREALDB_PORT_2X` and skip without them.

### Notes

- **Each connection decides on its own server line**: a SurrealDB 2.x and a 3.x server work side
  by side in one process, with interactive transactions on a 3.x WebSocket connection beside
  buffered ones on a 2.x connection; time travel, change feeds and live queries follow each
  connection's server.
- **Registering a name again with other settings, or removing it**, retires its clients and ends
  what the ORM holds on the old settings: a client still opening is closed unused (a retryable
  `SurrealDbConnectionError`: retry the call), live streams end, a change feed ends at its next
  read (`SurrealDbConnectionError`: start a new one), and an open `transaction()` is cancelled
  (above). A client the application obtained with `get_client()` before the change is its own: on
  WebSocket its next request fails; an HTTP client keeps the namespace and database it was opened
  on; an embedded client follows a new namespace or database, but after a new URL or a removal it
  is the previous engine, so stop using it. `set_connection()` and the `set_*()` setters keep
  their earlier behaviour: clients opened on the previous settings keep serving.
- **Query cache**: an ORM write removes the cached reads of its table on every connection, more
  than needed but never a stale result. Entries cached with `QueryCache.make_key()` and `set()`
  are not scoped to a connection: when the same statement runs on two connections, put the
  connection's name among the variables.
- **What the ORM cannot see across connections**, so keep it on one connection yourself: a link
  followed through a field whose annotation names no model (`RecordID`, `str`, `dict`), graph
  arrows (`select("->likes->Post")`, `traverse()`), an edge saved with endpoint ids of another
  database, and raw SurrealQL (`raw_query()`, `QuerySet.query()`, `call_function()`,
  `Transaction.add()`).
- **SurrealDB runs a statement, and a transaction, in one database**: two `transaction()` blocks on
  two connections are two independent transactions, and a `USE` inside `raw_query()` applies to
  that request only (the next request reads the connection's own database).
- **Differences from the full SurrealDB-ORM**: `close_connection()` without a name closes the
  current connection only (full ORM: every connection); `list_connections()` lists `'default'`
  first; registering a name again with other settings closes its clients; the getters, the
  setters and `reconnect()` follow `using()`; `add_connection()` and the `connection` key are
  checked; `ConnectionConfig` has no `protocol` (the official SDK speaks CBOR only) and accepts
  `None` for `user` and `password`; `name` is hidden from introspection; `transaction(name)`,
  `embedded_database(connection=)` and the errors for operations across connections are lite
  additions.
- Not available yet: a per-queryset override (`QuerySet.using()`), a client-side connection pool,
  one transaction across connections.
- **Server pins unchanged**: tested against SurrealDB 3.3.4 and 2.7.0, each alone and both in one
  process, and on the embedded engine, plus 3.1.5 for backward compatibility (identical).

## [0.28.1] - 2026-10-17

Query building checks what it is given: `select()` accepts field names and paths, `limit()` and
`offset()` take a non-negative integer, and a string filter value is always data, with `Var()` as
the only variable reference. No new public API.

### Changed

- **A string filter value is data, whatever its first character** (#250), on every filter path:
  `filter()`, `Q`, `get()`, `first()`, `count()`, `exists()`, `get_or_create()`,
  `update_or_create()`, `bulk_update()` by filter, `cache()`, subqueries and live queries.
  `filter(name="$admin")` compares `name` with the string `"$admin"`, bound like any other value.
  `Var("name")` is the only reference to a query variable, whose value `variables()` gives.
  Migration: where a filter uses the `"$name"` form (deprecated since v0.14.3), write
  `Var("name")`. When a string filter value is exactly `$` followed by the name of a variable of
  `variables()` or of a session parameter (`$access`, `$auth`, `$session`, `$token`), a
  `UserWarning` names the `Var(...)` to write, once per read, pointing at the caller's line; the
  value stays data. Any other string, such as `"$x"` with no variable `x`, is data and nothing
  warns.

  ```python
  from surreal_orm_lite import Var

  await User.objects().filter(name="$admin").exec()  # the string "$admin"
  await User.objects().filter(age__gte=Var("min_age")).variables(min_age=21).exec()  # $min_age
  await Doc.objects().filter(owner=Var("auth")).exec()  # SurrealDB's $auth
  ```

- **The `"$$"` escape keeps its meaning and is deprecated** (#250): a string filter value starting
  with `$$` loses one `$` (`"$$admin"` finds `"$admin"`) and emits a `DeprecationWarning`; write
  the value with one `$` less. An item of an `in` list never had the escape and is unchanged.
- **`variables()` refuses `access`, `auth`, `session` and `token`** with a `ValueError` (#250):
  SurrealDB sets these parameters for each session and refuses a query variable of that name. To
  pass your own value, give it another name or put the value itself in the filter:
  `filter(token=Var("device_token")).variables(device_token=t)` or `filter(token=t)`.
  `Var("token")` without `variables()` reads SurrealDB's `$token`, and likewise for the three
  others, in reads and in live queries.
- **`get_or_create()` and `update_or_create()`** raise `SurrealDbError` before any request when a
  scalar string criterion is exactly `$` followed by the name of a variable of `variables()` or of
  a session parameter, whatever its lookup (#250): pass the actual value as the criterion (the
  record id, or the value itself). Any other `"$name"` string is looked up and written as it is.
  `Var("auth")` finds the signed-in user's record but cannot be written: with no record to return,
  it raises `SurrealDbError`.
- **`select()` checks its items** like `order_by()` and `values()` (#247): each item is `"*"`, a
  field name (an aliased field by its Python name), a dotted path into a nested object or through a
  record link, or such a path followed by `.*` (`"author.*"`). Anything else raises `ValueError`
  naming the item when `select()` is called, before any request, and the selection in place is
  kept; an item that is not a `str` raises `TypeError`. Write a computed projection with `query()`
  and bound parameters.
- **`limit()` and `offset()` take a non-negative integer** (#249) of any type with `__index__`
  (an `IntEnum` member, NumPy's `int64`; the parameter is annotated `SupportsIndex`), up to
  `2**63 - 1`. A `bool`, `str`, `float` or `None` raises `TypeError`, a negative value or one above
  `2**63 - 1` `ValueError`, naming the method and the value, when the method is called (for a
  subquery's `limit()` and `offset()` too), before any request; the value in place is kept.
  SurrealDB 2.x and the embedded engine refuse a value above `2**32 - 1` when the read runs;
  SurrealDB 3.x reads it.
- **Live queries**: `Var("scope")` takes its value from `variables(scope=...)` like any other name
  and raises `SurrealDbError` without it (#250). `$access`, `$auth`, `$session`, `$token`, `$this`
  and `$parent` stay references the server reads on every notification.

### Fixed

- On an edge's `in` and `out`, which hold record ids, a string that is `$` followed by the name of
  a variable of `variables()` or of a session parameter raises a `ValueError` whose message names
  the `Var(...)` to write (#250).
- Each warning of a filter value is emitted once per read: the second compilation of a `cache()`
  miss and the re-send of a subquery over a missing table stay quiet, for the `"$$"`
  `DeprecationWarning` too (#250).

### Removed

- `utils.remove_quotes_for_variables()` and `utils.VALID_VARIABLE_REF_PATTERN`, internal helpers
  that were never in `__all__`.

### Notes

- **SurrealDB 2.x vs 3.x**: string filter values and `Var()` behave the same on both lines and on
  the embedded engine. `select("meta.*")` on a nested object that is not a record link gives the
  object on 3.x and its values as a list on 2.x and the embedded engine (through a link, the linked
  record on both). A `limit()` or `offset()` above `2**32 - 1` is refused by SurrealDB 2.x and the
  embedded engine when the read runs, and read by 3.x.
- **Docs**: the README section "String filter values and `Var()`" replaces "Filter values that
  start with `$`"; new parts "What `select()` accepts" and "`limit()` and `offset()`"; the
  live-query, subquery and query-cache invalidation notes match; three rows in the 2.x/3.x behaviour
  table (string filter values, a nested object's `.*`, `LIMIT`/`START` above `2**32 - 1`). The
  `getting_started` notebook reads `SURREALDB_HOST` and `SURREALDB_PORT`; the v0.14.3 notebook says
  the `"$$"` escape is deprecated since v0.28.1.
- **Server pins unchanged**: tested against SurrealDB 3.3.4 and 2.7.0, and on the embedded engine,
  plus 3.1.5 for backward compatibility (identical).

## [0.28.0] - 2026-10-17

Query cache: a QuerySet serves its reads from a client-side cache with `cache(ttl)`, and every ORM
write removes the cached reads it can change, with nothing to set up. The sixth minor of Tier 2.

### Added

- **`QuerySet.cache(ttl=None)`** and **`QueryCache`**, exported from `surreal_orm_lite` (the full
  SurrealDB-ORM's names). The rows a cached read returned stay in this process's memory, and the
  same read is answered from there, without a request, until its TTL runs out or an ORM write
  changes what it reads.

  ```python
  from surreal_orm_lite import QueryCache

  books = await Product.objects().filter(category="book").cache(ttl=30).exec()  # the database answers
  again = await Product.objects().filter(category="book").cache(ttl=30).exec()  # the cache answers
  await Product(id="p4", name="Atlas", category="book", price=40).save()  # removes Product's cached reads
  fresh = await Product.objects().filter(category="book").cache(ttl=30).exec()  # the database answers
  QueryCache.stats()["hits"]  # 1
  ```

- **Every read of a cached queryset**: `exec()`, `first()`, `get()` by filters or by id, `all()`,
  `count()`, `sum()`, `avg()`, `min()`, `max()`, `exists()`, `values(…).annotate(…)` and
  `annotate(GeoDistance)`. A read is a hit when an earlier read sent the same statement with the
  same variables, on the same connection and with the same identity, within the TTL. Rows are
  cached, not instances: each hit builds new instances from a copy. Empty results are cached
  (`get(<id>)` of a missing record raises `SurrealDbNotFoundError` on the hit too); errors never
  are. An internal failure of the cache never fails a read: the read goes to the database.
- **Time to live**: seconds (`int` or `float`) or a `timedelta`, the oldest result the read
  accepts; `cache()` without a TTL uses `QueryCache`'s default (60 s unless configured); `0` means
  never cached.
- **Built-in invalidation**: every ORM write removes the cached reads of the tables it writes,
  before its `post_*` signals fire — `save()`, `update()`, `merge()`, `upsert()`, `patch()`,
  `delete()`, the atomic operations, `relate()` and the `remove_*()` helpers, a typed edge's
  `save()`, `bulk_create()`, `bulk_update()`, `bulk_delete()`, `QuerySet.patch()`,
  `delete_table()`, `get_or_create()`/`update_or_create()` when they write, `signup()`,
  `define_computed_fields()` and `define_access()`. Reads whose subqueries read those tables go
  too; a read that follows links (`fetch()`, a dotted path, a `select()` item that is not a plain
  field) is removed by any ORM write, and a read of an edge model by any delete. Inside
  `transaction()` the written tables are invalidated at commit (a failed commit too, nothing on
  rollback). While a write runs, reads of its tables go to the database and store nothing.
- **One cache scope per connection and identity**: a sign-in, a sign-up, `authenticate()` or
  `invalidate()` starts a new scope and drops the previous identity's results; another event loop's
  connection, a reconnect and an `embedded_database()` block start empty; closing a connection
  drops its results. Cached results never cross identities.
- **`QueryCache`**: `configure(default_ttl=60, max_size=1000, enabled=True)` sets all three
  settings at every call, as in the full ORM, and never clears; `enabled=False` hides the entries
  (`cache()` reads go to the database and calling `cache()` logs a warning). Past `max_size` the
  least recently used entry is evicted. `invalidate()` takes a model class or a table name and
  returns how many entries it removed; `clear()` drops every entry and resets the counters;
  `stats()` returns `entries`, `tables`, `max_size`, `default_ttl`, `enabled`, `hits` and
  `misses`. `make_key()`, `get()` and `set()` cache your own results (of `raw_query()`, say),
  tagged with a table so that an ORM write to it removes them; `get()` misses while such a write
  runs.
- **Refusals**: `query()` raises `SurrealDbError` on a queryset with `cache()`; `live()`, `watch()`
  and `changes()` name `cache()` among the clauses their usual error refuses; combining `cache()`
  with `as_of()` raises `SurrealDbError`, in either order. A negative or non-finite TTL raises
  `ValueError`, anything but a number or a `timedelta` (`True` included) `TypeError`.
- README section 29, "Query cache (`cache()`, `QueryCache`)" (what is cached, the time to live,
  invalidation and what the cache cannot see, one cache per connection and identity,
  configuration and statistics, a note for users of the full SurrealDB-ORM), a features row, a
  row in the 2.x/3.x behaviour table, the comparison and roadmap rows, and the example notebook
  `examples/v0.28.0_query_cache.ipynb`.

### Changed

- ORM writes now also remove the cached reads they can change, in memory, without a request.
  Without `cache()`, every read and write sends the same requests as in v0.27.x; the texts of the
  `live()`, `watch()` and `changes()` refusals are unchanged.

### Notes

- **Same on SurrealDB 2.x and 3.x**, and on the embedded engine. Reads inside a transaction
  (`objects(tx=)`) are never cached: on 3.x an interactive transaction's reads see its uncommitted
  writes, and on 2.x a buffered transaction cannot read.
- **What the cache cannot see** (the TTL bounds it; `QueryCache.invalidate(Model)` removes a
  table's entries after such a change): writes by another process or client; raw SurrealQL
  (`raw_query()`, `QuerySet.query()`, `call_function()`, `Transaction.add()`); a write whose task
  is cancelled, or that fails client-side, after its request was sent; writes made by an access
  method's own `SIGNIN`/`SIGNUP` clauses beyond the record they create; what the server derives on
  its own (`DEFINE EVENT`, `REFERENCE … ON DELETE` on 3.x, views, `PERMISSIONS` that read another
  table); values computed when the read runs (a `DEFINE PARAM` read with `Var()`,
  `<future>`/`COMPUTED` fields, time functions in a `select()` expression); a table of relations
  read through a model that is not a `BaseSurrealEdge`. A session changed behind the ORM's back
  (`raw_query("USE …")`, `signin()` or `use()` on the SDK client) is not seen either: call
  `QueryCache.clear()` after it.
- **Memory**: cached rows stay in this process's memory until they expire, are evicted,
  invalidated or cleared, or their connection closes. `configure(enabled=False)` only hides them;
  `QueryCache.clear()` drops them. Entries stored with `QueryCache.set()` are not scoped to a
  connection or an identity: put whatever tells your callers apart in the key.
- **Keys** are the SHA-256 of the CBOR bytes the official SDK sends, with the keys of mappings
  sorted: `RecordID("user", 1)` and `"user:1"` never share an entry. An SDK that exposes no CBOR
  encoder sends every `cache()` read to the database (one warning per process).
- Concurrent misses of the same read each send their own request.
- **Differences from the full SurrealDB-ORM**: every read is cached, not only `exec()`, `first()`
  and `get()` by filters; invalidation is built in and covers bulk operations, `QuerySet.patch()`,
  the atomic operations, relations and transactions, where the full ORM listens to `post_save`,
  `post_update` and `post_delete`; rows are cached instead of instances; one scope per connection
  and identity; `live()`, `watch()`, `changes()` and `query()` refuse `cache()`; the TTL may be a
  `float` or a `timedelta` and negative values are refused; LRU eviction instead of FIFO;
  `invalidate()` takes a table name too; `stats()` adds `hits` and `misses`.
- Not available yet: invalidation from other processes or clients (through live queries),
  caching `as_of()` reads, sharing cached results between event loops.
- **Server pins unchanged**: tested against SurrealDB 3.3.4 and 2.7.0, and on the embedded engine,
  plus 3.1.5 for backward compatibility (identical).

## [0.27.0] - 2026-10-16

Subqueries: a QuerySet filters another, as `Subquery(qs)` or the queryset itself, compiled to a
sub-`SELECT` the server runs inside the outer statement: one request, one consistent read, every
inner value bound. The fifth minor of Tier 2.

### Added

- **`Subquery(qs)`**, exported from `surreal_orm_lite` (the full SurrealDB-ORM's name), and a
  QuerySet given directly as a filter value, in `filter()` and in `Q` (`&`, `|`, `~`, any nesting).

  ```python
  from surreal_orm_lite import Q, Subquery

  active = Person.objects().filter(active=True)
  posts = await Post.objects().filter(author__in=Subquery(active)).exec()
  same = await Post.objects().filter(author__in=active).exec()  # a QuerySet is a subquery

  best = Post.objects().select("score").order_by("-score").limit(1)
  top = await Post.objects().filter(score=Subquery(best)).exec()  # compared with its first value

  inactive = Person.objects().filter(active=False)
  mixed = await Post.objects().filter(Q(author__in=inactive) | Q(score__gte=9)).exec()
  ```

- **Lookups.** `in`, `not_in`, `containsall` and `containsany` compare the field with all the
  subquery's values; `exact` (no suffix), `gt`, `gte`, `lt`, `lte`, `contains` and `not_contains`
  with its first value (order the subquery to choose it). A first-value lookup on an empty
  subquery matches no record, and `~Q(...)` of it every record; for `in`, an empty subquery is an
  empty list.
- **What the inner queryset selects**: its record ids without `select()` (`SELECT VALUE id`), or
  the values of the one field given to `select()` (an aliased field compiles its column). Its
  `filter()`, `Q`, `nearby()`, `order_by()`, `limit()`, `offset()` and `variables()` apply; its
  `fetch()` is ignored, as in the full ORM. Values are compared as stored, never converted: to
  match records, select `id` or a record link.
- **Where it applies**: `exec()`, `first()`, `get()` with filters, `exists()`, `count()`, `sum()`,
  `avg()`, `min()`, `max()`, `values(…).annotate(…)` and `annotate(GeoDistance)`; the writes by
  filter `bulk_update()`, `bulk_delete()` and `QuerySet.patch()`; the lookups of `get_or_create()`
  and `update_or_create()`. Subqueries nest. In `Model.objects(tx=tx)` the statement runs in the
  transaction, and an interactive transaction's subquery sees its uncommitted writes. Under
  `as_of()` every subquery reads at the same moment. `live()` and `watch()` write the sub-SELECT
  inline, and the server evaluates it again on every change. `changes()` refuses `filter()`, so
  subqueries too.
- **Refusals before any request** (`live()` and `watch()` at the call). `ValueError`: a lookup
  that takes no subquery (`isnull`, `like`, `ilike`, `startswith`, `endswith`, `match`, `regex`,
  `inside`, `outside`, `intersects`), a queryset that is its own subquery, an inner `values()`,
  `annotate()` or `select()` of several fields, an inner `limit()` or `offset()` that is not an
  `int >= 0`, an inner `order_by()` on `_sqv` it does not select, an inner `variables()` binding a
  name of the statement to another value, a subquery inside a list, `Q(...).to_sql()` called on its
  own with a subquery, and `bulk_update(score=Subquery(qs))`. `SurrealDbError`: an inner `as_of()`
  (call it on the outer queryset), an inner `objects(tx=)` whose transaction is not the outer
  one's, and a subquery as an `exact` criterion or a `defaults=` value of `get_or_create()` or
  `update_or_create()`. `TypeError`: `Subquery(x)` for anything but a QuerySet.
- README section 28, "Subqueries (`Subquery`)" (what a subquery selects, the lookups, ordered and
  limited subqueries, where subqueries apply, what they cannot do, a note for users of the full
  SurrealDB-ORM), a bullet in the supported lookups, a `Q` example in section 5, four rows in the
  2.x/3.x behaviour table, and the example notebook `examples/v0.27.0_subqueries.ipynb`.

### Changed

- A QuerySet given as a filter value is now a subquery. v0.26.x refused it: `in` and `not_in`
  raised `ValueError` ("requires an iterable") and the other lookups failed when the SDK encoded
  the request. Every call that worked in v0.26.x behaves the same.
- `Q.to_sql()` gains a keyword-only `subqueries` parameter (default `None`), the compiler of the
  statement the QuerySet sends; a `Q` compiled on its own refuses a subquery lookup.

### Notes

- **One statement per request, a block of `LET`s**:
  `{ LET $_sq0 = (SELECT VALUE id FROM Person WHERE active = $_f0); (SELECT * FROM Post WHERE author IN $_sq0) };`.
  Each subquery is bound once, innermost first, because SurrealDB 3.2 and later can return wrong
  rows for an inline sub-SELECT with `ORDER BY` and `LIMIT`
  ([surrealdb/surrealdb#7470](https://github.com/surrealdb/surrealdb/issues/7470)), and because the
  official SDK's `query()` returns the first statement's result only, so the full ORM's `LET`
  prelude (two statements) does not fit. An ordered subquery selects its column as `_sqv` with its
  order fields: SurrealDB 2.x refuses to order a one-column sub-SELECT by another field. `$_sq0`,
  `$_sq1`, … are reserved in such a statement and hide a `variables()` value of the same name.
- **A first-value lookup on an empty subquery matches no record**: SurrealDB alone would compare
  with `array::first([])`, which is `NONE`, and `score > NONE` holds for every record, so the ORM
  guards the comparison. The full SurrealDB-ORM matches every record there.
- **A subquery table that does not exist reads as empty**, like any missing table. SurrealDB 2.x
  and the embedded engine read it so in the same request. On SurrealDB 3.x the ORM asks the server
  whether the table exists and, once it confirms the table does not, sends the statement again
  with that subquery empty: three requests, and only then. Any other error, even one quoting
  "does not exist", is relayed. On 3.x a write by filter in a transaction fails with the server's
  error, and `live()` raises `SurrealDbNotFoundError` before it starts (3.x would accept the live
  query and never notify).
- **Transactions**: a buffered transaction (2.x, or HTTP) still cannot read, and applies writes by
  filter at commit. Build the inner queryset with the same `objects(tx=)`, or with none.
- **Time travel**: without versioned storage, a subquery read under `as_of()` raises v0.26.0's
  `SurrealDbError`; SurrealDB 3.1.5 raises the server's own `ThrownError` instead.
- Not available yet: `annotate(x=Subquery(...))`, correlated subqueries (`OuterRef`, `$parent`)
  and `Exists`.
- **Server pins unchanged**: tested against SurrealDB 3.3.4 and 2.7.0, in memory and on versioned
  storage, and on the embedded engine, plus 3.1.5 for backward compatibility (identical except the
  time-travel error above).

## [0.26.0] - 2026-10-16

Time travel: a QuerySet reads a model's records as they were at a past moment, with
`as_of()` (SurrealQL's `VERSION` clause), on the official SDK's embedded
`surrealkv+versioned://` engine and on SurrealDB servers started with versioned storage. The
fourth minor of Tier 2.

### Added

- **`QuerySet.as_of(at)`** adds a moment to every read of the queryset. Records updated since
  read as they were, a record deleted since is still found, and one created later is not. The
  store must keep history: the embedded `surrealkv+versioned://<path>` (no server), SurrealDB 3.x
  started on `surrealkv://<path>?versioned=true` or `rocksdb://<path>?versioned=true`, or
  SurrealDB 2.x started on `surrealkv+versioned://<path>`.

  ```python
  from surreal_orm_lite import BaseSurrealModel, SurrealDBConnectionManager

  class Product(BaseSurrealModel):
      id: str | None = None
      name: str
      price: float

  SurrealDBConnectionManager.set_connection(
      url=f"surrealkv+versioned://{path}/shop", namespace="shop", database="shop"
  )
  client = await SurrealDBConnectionManager.get_client()

  lamp = Product(id="lamp", name="Lamp", price=40.0)
  await lamp.save()
  opening = await client.query("RETURN time::now();")  # a moment, from the database's clock
  await lamp.merge(price=30.0)

  assert (await Product.objects().as_of(opening).get("lamp")).price == 40.0
  assert (await Product.objects().get("lamp")).price == 30.0
  ```

- **The moment** is an aware `datetime` (any time zone) or a naive one (UTC), an ISO-8601 string
  (parsed by the ORM; a date alone is midnight UTC), a `timedelta` or the ORM's `Duration` (that
  long before the database's clock), a `SurrealFunc` the database evaluates
  (`SurrealFunc("time::now() - 1h")`), or `None` (the current state again); called again, the last
  moment wins. The moment is always a bound parameter. A moment outside the instants SurrealDB
  versions can address (1970-01-01T00:00:00Z to 2262-04-11T23:47:16.854775Z) is refused on every
  line: `ValueError` at the call, or `SurrealDbError` before the read for a duration or an
  expression the database resolved there.
- **Every read honours it**, with `filter()`, `Q`, `nearby()`, `order_by()`, `limit()`, `offset()`,
  `select()` and `variables()`, on edge models too: `exec()`, `first()`, `get()` (with filters or by
  id), `all()`, `exists()`, `count()`, `sum()`, `avg()`, `min()`, `max()`, `values(…).annotate(…)`
  and `annotate(d=GeoDistance(…))`.
- **Snapshots.** The instances a time-travel read returns are read-only: `save()`, `update()`,
  `merge()`, `patch()`, `upsert()`, `delete()`, the `atomic_*()` methods, `relate()`,
  `remove_relation()` and `remove_all_relations()` raise `SurrealDbError` before any signal or
  request, and `bulk_create()` refuses a snapshot in its list. `refresh()` turns a snapshot back
  into the current record; `Product(**snapshot.model_dump()).update()` (or `.save()` for a record
  deleted since, or `.upsert()`) writes a past state back. `model_copy()` and `pickle` keep the
  snapshot, and a snapshot is never `==` the current instance (compare `model_dump()`s). A record
  model nested in a snapshot's fields is a snapshot too, such as a link `fetch()` resolves into a
  model-typed field on SurrealDB 3.x; its own `refresh()` makes it the current record.
- **Refusals before any request.** On a queryset with `as_of()`: `bulk_create()`, `bulk_update()`,
  `bulk_delete()`, `patch()`, `update_or_create()`, `get_or_create()`, `delete_table()`, `query()`,
  `live()`, `watch()` and `changes()` raise `SurrealDbError`; `Model.objects(tx=...).as_of(...)`
  raises at the call (history cannot join a transaction); on the embedded engine, a URL that keeps
  no history (`mem://`, `memory://`, `surrealkv://`, `file://`) raises, naming
  `surrealkv+versioned://`. A 3.x server without versioned storage refuses the read, raised as
  `SurrealDbError` with the SDK's error as its `__cause__`.
- README section 27, "Time travel (`as_of()`)" (stores that keep history, the moment, reads,
  snapshots, refusals, SurrealDB 2.x without versioned storage, record links, writing history with
  a raw `CREATE … VERSION`, a note for users of the full SurrealDB-ORM, which has no time travel),
  new rows in both behaviour tables, and the example notebook `examples/v0.26.0_time_travel.ipynb`.

### Changed

- `BaseSurrealModel` declares a private attribute, the snapshot marker, so every instance now has
  a `__pydantic_private__` dict. Two current instances compare as before; an instance pickled by
  v0.25.x unpickles as a current record, and a model's own private attributes are kept.
- `refresh()` also clears the snapshot marker of an instance read with `as_of()`.
- CI starts a second SurrealDB per job, on versioned storage, for the time-travel tests; they find
  it through `SURREALDB_VERSIONED_PORT` and skip without it.

### Notes

- **Every time-travel table read also reads each record by its id at the moment**, because a plain
  `SELECT … VERSION` is wrong on both server lines: SurrealDB 3.x's table reads return the records
  deleted before the moment, in their last state, and SurrealDB 2.x and the embedded engine
  miscount an unfiltered `count()`. It costs about one extra point read per record scanned; a read
  by id needs no check.
- **SurrealDB 2.x without versioned storage answers without an error**: in memory or on plain
  SurrealKV, it returns only the records last written at or before the moment, in their current
  state, and the ORM cannot tell which storage the server runs. Start 2.x with
  `surrealkv+versioned://` for time travel. SurrealDB 2.x on RocksDB refuses `VERSION`
  (`SurrealDbError`).
- **Record links**: `fetch()` resolves links as they were on SurrealDB 3.x, into read-only
  snapshots. On SurrealDB 2.x (servers and the SDK 2.x embedded engine) they resolve to their
  current state, so `exec()`, `first()` and `get()` without an id refuse `fetch()` under
  `as_of()`; a dotted path through a link reads the current value there.
- **Writing history at a past moment** is left to raw queries (`CREATE … VERSION d'…'`, one
  statement per `client.query()` call): stored at that moment and readable with `as_of()` on
  SurrealDB 2.x and the embedded engine; accepted but not readable at that moment on SurrealDB 3.x.
- The surrealdb SDK 2.x embedded engine ignores `?versioned=true` on `surrealkv://`: use
  `surrealkv+versioned://`. The SDK's own `surrealdb.Duration` is accepted at runtime only (the
  annotation names the ORM's types). `_as_of` is a reserved `variables()` name on a time-travel
  read: the moment is bound under it.
- A `SurrealFunc` moment is sent as written: build it in your code, never from user input, and pass
  user values through `variables()`, bound.
- **Server pins unchanged**: tested against SurrealDB 3.3.4 and 2.7.0, in memory and on versioned
  storage, plus 3.1.5 for backward compatibility (identical); the embedded tests run in every CI
  job. Every existing call behaves as in v0.25.x.

## [0.25.0] - 2026-10-15

Embedded test engine: the ORM also runs on the official SDK's in-process SurrealDB engine
(`mem://`, `surrealkv://<path>`), with no server and no Docker, and the new
`surreal_orm_lite.testing` module adds `embedded_database()` and the full SurrealDB-ORM's
`SurrealFixture` / `@fixture`. The same models and queries run unchanged against a server. The
third minor of Tier 2.

### Added

- **Embedded URLs.** With
  `SurrealDBConnectionManager.set_connection(url="mem://", namespace=…, database=…)`, every ORM
  call runs in-process: CRUD, QuerySet, aggregations, relations, typed edges, rich and geo fields,
  upsert and patch, server values, computed fields, stored functions, buffered transactions,
  change feeds and authentication. The SDK sends `mem://`, `memory://`,
  `surrealkv://<path>`, `surrealkv+versioned://<path>` and `file://<path>` (a SurrealKV store) to
  its engine; the ORM asks the SDK's own URL parser and keeps no list of schemes. `user` and
  `password` are optional on an embedded URL and never sent: the engine's anonymous session is the
  in-process owner. They are kept, so one configuration switches between a server and `mem://` by
  its URL alone:

  ```python
  from surreal_orm_lite import BaseSurrealModel, SurrealDBConnectionManager

  SurrealDBConnectionManager.set_connection(url="mem://", namespace="app", database="app")

  class Note(BaseSurrealModel):
      id: str | None = None
      title: str

  await Note(id="docs", title="Write the docs").save()
  assert await Note.objects().count() == 1
  assert SurrealDBConnectionManager.is_embedded()
  ```

- **`SurrealDBConnectionManager.is_embedded()`**: whether the configured URL addresses the
  in-process engine, for code that must run on both kinds of connection.
- **One engine per process.** The manager opens the engine on the first call and hands it to every
  event loop and thread, so they all see the same database; concurrent first calls open exactly
  one. A namespace or database change resets the shared session and selects the new pair on the
  same engine, with no data lost; a new URL closes the old engine. Closing
  (`close_connection()`, `close_all_connections()`, `unset_connection()`, `reconnect()`, the exit
  of `async with SurrealDBConnectionManager():`) discards a `mem://` database, and a
  `surrealkv://` one reopens with its data. `validate_connection()` never discards it. Server
  clients are untouched while an embedded URL is configured: `close_connection()`, `reconnect()`
  and the `async with` exit then close only the engine, so a server client keeps its live queries
  and its identity, even one signed in with `store=False`, and `is_connected()` stays true on its
  event loop; `close_all_connections()` and `unset_connection()` close it too. An event loop whose
  server connection dropped while signed in with `store=False` still raises once the server URL is
  set back with `set_url()`, or after an `embedded_database()` block: neither closing the engine
  nor an identity set on it ends that state. `set_connection()` does: a new configuration is a
  choice of identity.
- **Clear errors, before any request, for what the engine cannot do**: live queries (`live()`,
  `watch()`, `subscribe_live()`: read the table's change feed with `changes()` instead),
  `changes(since=<datetime>)` (pass a cursor or `since=None`), and `AuthenticatedUserMixin` calls
  with `bind=False` and `ephemeral_client()` (the engine has a single session per database). Each
  raises `SurrealDbError`.
- **Authentication on the engine.** `signin()`, `signup()`, `authenticate()` and `invalidate()`
  work with the engine's record access methods and `DEFINE USER`s, and record users and table
  `PERMISSIONS` apply as on a server. A sign-in changes the session of the whole process (every
  event loop and thread); `invalidate()` and a namespace or database change return it to the
  in-process owner and end the stored session. Model auth with `bind=True` signs that session in,
  one call at a time with the connection-level calls (the last one wins), and a failed call puts
  the previous session back, with one exception, as on a server: when `signup()` or `signin()`
  succeeded but the record does not fit the model, the session stays signed in as that record
  (the error says so). `refresh()` cannot succeed on the engine: its SurrealQL does not parse
  `WITH REFRESH`.
- **`surreal_orm_lite.testing`**, a new module that does not import pytest:
  - `embedded_database(url="mem://", *, namespace="test", database="test")` runs a block against
    an isolated in-process database, yields the block's SDK client (valid inside the block only),
    then puts the previous connection back: its settings, stored session and engine. Blocks nest;
    `surrealkv://<path>` gives an on-disk database. A server URL raises `ValueError`.
  - `SurrealFixture` and `@fixture`, with the full SurrealDB-ORM's names: `load()` saves a fresh
    copy of each declared instance in declaration order, yields them typed as the fixture class,
    and deletes them on exit. It works on any connection, a server or the engine. Unlike the full
    ORM, an explicit `id` is kept, a save that fails or is cancelled mid-load deletes the copies
    already saved, `@fixture` raises `TypeError` on a class that is not a `SurrealFixture`
    subclass, and `load()` raises `ValueError` on an undecorated subclass of a fixture that
    declares instances of its own (the full ORM loads only the parent's).

  ```python
  from surreal_orm_lite.testing import SurrealFixture, embedded_database, fixture

  @fixture
  class UserFixtures(SurrealFixture):
      alice = User(name="Alice", role="admin")
      bob = User(id="bob", name="Bob", role="player")  # an explicit id is kept

  async with embedded_database():  # a fresh, empty in-memory database
      async with UserFixtures.load() as fixtures:
          admins = await User.objects().filter(role="admin").exec()
          assert [admin.name for admin in admins] == ["Alice"]
      assert await User.objects().count() == 0  # alice and bob are deleted
  ```

- README section 26, "Embedded engine and test fixtures" (in-process databases and the embedded
  URL table, one engine per process, what the engine does not do, its SurrealQL line, identity,
  test fixtures with a pytest recipe, a note for users of the full SurrealDB-ORM), and a new
  behaviour table, "ORM behaviour: the embedded engine"; example notebook
  `examples/v0.25.0_embedded_test_engine.ipynb`.

### Changed

- `set_connection()`: `user`, `password`, `namespace` and `database` default to `None`, in the same
  positional order. `is_connection_set()` needs the user and password for a server URL only.
- A URL the installed SDK refuses (`rocksdb://`, a typo) is still accepted by `set_connection()`;
  the first ORM call now raises a `SurrealDbConnectionError` that quotes the SDK, before anything
  is opened, instead of "Connection not been set." or "Can't connect to the database.". For
  `rocksdb://` it points to #233.
- The DDL helpers (`define_computed_fields()`, `define_access()`) raise their usual
  `SurrealDbError` for a statement the embedded engine cannot parse, which SDK 2.0.0 reports as a
  bare `RuntimeError`.
- The `SurrealDbError` of an `AuthenticatedUserMixin` call whose record does not fit the model
  (since v0.17.0) has a new text, on every path, the server lines included: it says whether the
  shared session kept its previous identity or is now signed in as that record (for `refresh()`,
  also that the refresh token passed in is spent and whether the new pair is kept), and it names
  the fields that do not fit and Pydantic's error codes (`nickname: missing; prefs: int_parsing`),
  and quotes nothing from the record: no value, and no key the model does not declare (listed as
  `(undeclared field)`). The Pydantic `ValidationError` is its `__context__` instead of its
  `__cause__`.

### Notes

- **The engine is SurrealDB core 2.3.10**, bundled with the surrealdb SDK 2.0.0 base wheel (no
  new dependency, every supported Python version): write 2.x SurrealQL in raw queries and computed
  fields, and expect the 2.x column of the behaviour table. `client.version()` reports the SDK's
  version (`surrealdb-2.0.0`). Transactions are buffered, and parse errors in raw queries are a bare
  `RuntimeError`. The SDK 3.0 line bundles a 3.x engine in the `surrealdb[embedded]` extra (#205).
- **`rocksdb://` is not yet supported by the official SDK** (#233): the ORM relays the SDK's
  refusal and will accept the scheme with no change once a released SDK does. A bare `memory` (no
  `://`) is refused by SDK 2.0.0: write `memory://`. `surrealkv+versioned://` behaves as
  `surrealkv://` until time travel (v0.26.0).
- **With the surrealdb SDK 2.x, closing does not stop an in-process engine**: a client obtained
  before `close_connection()` or inside an `embedded_database()` block is still a running
  database. Do not use it after the close, drop it (`del client`) before reopening the same
  `surrealkv://` path, and never open one path from two processes.
- A fresh engine raises a retryable write conflict when several writes run concurrently on a table
  that does not exist yet: define the table first, or use `@retry_on_conflict()`.
- The v0.24.3 read hook (#235) also applies to the embedded engine's client: zero durations and
  degenerate geometries read as on a server. A `<set>[…]` is stored as an array there, as on 2.x.
- **Server pins unchanged**: tested against SurrealDB 3.3.4 and 2.7.0 (the CI pins), plus 3.1.5
  for backward compatibility; the embedded tests run in every CI job. `ModelFactory` and
  `Faker` come in v0.39.0.

## [0.24.3] - 2026-10-15

The clients the ORM opens now read the values the surrealdb SDK 2.0.0 cannot decode (zero
durations, SurrealDB 3.x native sets, degenerate geometries), and a zero duration can be stored.
No new public API.

### Fixed

- **Some values SurrealDB returns could not be read** (#235). The surrealdb SDK 2.0.0 cannot
  decode a zero duration, a SurrealDB 3.x native set (a `TYPE set<…>` column on SurrealDB 3.3) or
  a degenerate geometry another client wrote (a line of fewer than 2 points, alone or inside
  another geometry, or a polygon ring of fewer than 4 points): one such value failed the whole
  response it was in. The clients the ORM opens (`SurrealDBConnectionManager.get_client()`,
  `ephemeral_client()`) now read them, over WebSocket, HTTP and embedded connections, and later
  calls on the same client keep working:
  - a zero duration reads as zero: `timedelta(0)` or `Duration(0)` in a typed field, the SDK's
    `Duration(0)` in an untyped one;
  - a native set reads as a Python `set`, which `set[X]` and `frozenset[X]` fields validate, or as
    a `list` when its items are unhashable (record ids, objects, nested sets) or two of them are
    equal in Python, like `1` and `true`: every item the server holds comes back;
  - a degenerate geometry reads as its GeoJSON dict, such as
    `{"type": "LineString", "coordinates": [[1.0, 2.0]]}`, in an untyped field, `raw_query()` and
    live notifications; a geometry field refuses it with a `ValidationError` naming the field.

  Every other value reads exactly as the SDK reads it, and a malformed value still raises the
  SDK's error. The values go through a hook around the SDK's own CBOR decoder, installed only on
  the ORM's clients and only while the installed SDK cannot decode them: the SDK's modules are not
  modified, and an `AsyncSurreal` client the application builds itself keeps the SDK's behaviour.
  Written back through the ORM (`merge()`, or `update()`, which rewrites the whole row), a native
  set is stored as an array, unless its column is `TYPE set<…>`, and a degenerate geometry's dict
  as a plain object, on both lines.

- **A zero duration was refused** since v0.23.0 (`ValueError` for a field, a filter value or a
  bound parameter, `TypeError` from `live()`), because the SDK could not read it back. The ORM's
  clients now read it, so `timedelta(0)` and `Duration(0)` are written, filtered and read back like
  any other duration, on both lines; filter "longer than zero" with `__gt=Duration(0)`. The refusal
  remains only for an SDK that reads no zero duration and whose decoder the ORM cannot hook.
  Upgrade note: a record the ORM writes can now hold a zero duration, which a reader that does not
  go through the ORM's clients cannot read (an `AsyncSurreal` client the application builds itself,
  another service on the SDK 2.0.0).

### Notes

- **SurrealDB 2.x vs 3.x**: zero durations and degenerate geometries read the same on both lines.
  Native sets exist only on 3.x: on 3.3 a `TYPE set<…>` column stores a `set[X]` field's array as a
  native set, which now reads back as the declared set; 3.1 still refuses an array in such a
  column, so declare `array<…>` columns to support 3.1. On 2.x the column holds an array, as
  before.
- The README's "Values the SDK cannot read", "Geometries the SDK cannot read" and "Sets" sections,
  the refusals table and the 2.x/3.x row for `TYPE set<…>` columns describe the new behaviour; the
  v0.23.0 rich field types notebook stores and reads back a zero duration.
- A later SDK reads zero durations and native sets itself
  ([#205](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/205)); the hook then leaves
  them to it.
- Tested against SurrealDB 3.3.4 and 2.7.0 (the CI pins), plus 3.1.5 for backward compatibility.

## [0.24.2] - 2026-10-15

Sub-models keep their rich values after a write that returns the row, and a `timedelta` below the
top level reads the server's duration. No new public API.

### Fixed

- **A sub-model's durations, ranges and geometries were written back as plain objects** (#228).
  `save()` without an id, `upsert()`, `refresh()`, `merge()`, `patch()` and the `atomic_*` helpers
  applied the returned row without validation, so a sub-model field (a Pydantic model, a dataclass
  or a `TypedDict`, also inside `list[...]`, `dict[..., ...]` or `X | None`) held the row's `dict`
  with the SDK's values. The next full write (`update()`, `upsert()`) stored their fields
  (`{elapsed: 300000000000}`) instead of SurrealDB's durations, ranges and geometries, warned
  `PydanticSerializationUnexpectedValue`, and the next `get()` of the record failed validation.
  These calls now give a sub-model field the value a read gives: its model instance, with the ORM's
  `Duration`, `timedelta`, `Range` and geometry values inside, so writing the instance back is safe.
  - The returned row goes through the model's validation, as in `get()`, so the model's own
    validators (`field_validator`, `model_validator`, `model_post_init`) run on it too.
  - When the row as a whole does not validate, each sub-model field is validated on its own, except
    one that the model's own validators could reshape; that one, like a value that does not
    validate, is applied as received. These calls raise nothing new.
  - A GeoJSON object stored in a top-level geometry field (the full SurrealDB-ORM's format) now
    becomes the ORM geometry after `refresh()`, `merge()` and `patch()`, as after `get()`.
- **A `timedelta` below the top level failed to read the server's duration.** A sub-model's
  `timedelta` field, `list[timedelta]`, `dict[str, timedelta]` or `timedelta | str` refused the
  SDK's `Duration` on every read, as before v0.23.0. They now read a `timedelta`, floored to the
  microsecond (declare `Duration` to keep the nanoseconds), in a read and in the row a write
  returns. A row holding many such values is converted in one pass, without mutating the data,
  and the model's own validators (any mode) and `model_post_init` still apply once, even when they
  change the data in place.

### Notes

- **Identical on SurrealDB 2.x and 3.x**: the cause was in how the ORM applied the SDK's values,
  not in the server. The README's "Rich values inside sub-models" limitation of section 25 is
  removed, and "After a write that returns the row" describes the new behaviour; top-level `date`,
  `set` and `Range` fields still hold the SDK's value until the record is loaded again.
- Tested against SurrealDB 3.3.4 and 2.7.0 (the CI pins), plus 3.1.5 for backward compatibility.

## [0.24.0] - 2026-10-15

Geospatial fields: a model can declare geometry fields stored as SurrealDB's native geometries and
read back as the ORM's geometry classes, and query them with `nearby()`, a `GeoDistance`
annotation and the `inside`, `outside` and `intersects` lookups, with the same results and the
same distances on SurrealDB 2.x and 3.x. The names follow the full SurrealDB-ORM. The second minor
of Tier 2.

### Added

- **Geometry fields.** `GeoField["kind"]` and the aliases `PointField`, `LineStringField`,
  `PolygonField`, `MultiPointField`, `MultiLineStringField`, `MultiPolygonField`,
  `GeometryCollectionField` and `GeometryField` declare nullable geometry fields with a `None`
  default, as in the full ORM; a geometry class annotation (`entrance: GeometryPoint`) works too.
  A field accepts the ORM value, the SDK's, a GeoJSON object and, for points, a
  `(longitude, latitude)` pair. Every write stores a native geometry; `get()`, `filter()`, live
  notifications, change feeds and the rows a write returns give back the ORM's classes.
  `model_dump(mode="json")` writes GeoJSON. Edge models and field aliases are supported:

  ```python
  from surreal_orm_lite import (
      BaseSurrealModel, GeoDistance, GeometryPoint, PointField, SurrealConfigDict,
  )

  class Restaurant(BaseSurrealModel):
      model_config = SurrealConfigDict(table_name="restaurant")
      id: str | None = None
      name: str
      location: PointField = None

  times_square = GeometryPoint(-73.9855, 40.7580)  # longitude, latitude
  await Restaurant(id="pizza", name="Corner Pizza", location=(-73.9877, 40.7547)).save()

  closest = await (
      Restaurant.objects()
      .nearby("location", times_square, max_distance=5_000)  # metres
      .annotate(dist=GeoDistance("location", times_square))
      .order_by("dist")
      .exec()
  )
  round(closest[0].dist)  # 411: metres, as SurrealDB's geo::distance measures it
  ```

- **Geometry value classes**: `Geometry` and `GeometryPoint`, `GeometryLine`, `GeometryPolygon`,
  `GeometryMultiPoint`, `GeometryMultiLine`, `GeometryMultiPolygon` and `GeometryCollection`, with
  the names, constructor signatures, attributes and methods of the SDK's classes
  (`parse_coordinates()`, `get_coordinates()`, …). They are immutable and hashable, keep `float`
  coordinates, convert to and from GeoJSON (`to_geojson()`, `Geometry.from_geojson()`,
  `__geo_interface__`), and `str()` gives their SurrealQL text. They exist because Pydantic's
  `model_dump()` flattens the SDK's dataclasses into plain dicts; the ORM converts them to the
  SDK's classes when it sends them.
- **`InvalidGeometryError`**: raised for a geometry the classes refuse (a non-finite coordinate, a
  polygon ring of fewer than 4 points or not closed, a collection nested more than 32 levels,
  malformed GeoJSON). It is both a `ValueError` and a `surrealdb.InvalidGeometryError`.
- **`QuerySet.nearby(field, point, max_distance)`**: keeps the records whose field is a point within
  `max_distance` metres of `point`. The point and the distance are bound as query variables, a
  `.is_point()` guard leaves out records whose field holds no point, several calls are AND-ed, and
  the condition applies wherever `filter()` does, `live()` and `watch()` included.
- **`GeoDistance(field, point)`** for `annotate()`: each instance gets the distance in metres as an
  attribute named after the alias (`None` when its field is not a point), `order_by()` sorts by
  it, and it is never written back.
- **Spatial lookups** `inside`, `outside` and `intersects` (`filter(location__inside=zone)`),
  compiled to SurrealQL's `INSIDE`, `OUTSIDE` and `INTERSECTS` with one bound geometry; they work in
  `filter()`, `Q` objects (`~Q` included) and live queries. `outside` keeps SurrealQL's meaning: a
  record without a geometry is outside.
- **Live-query literals** for geometries (GeoJSON object form, every kind and collection member),
  so `nearby()`, the spatial lookups and geometry filter values give the same notifications on
  both lines.
- `is_geo_field()` and `get_geo_info()` in `surreal_orm_lite.geo`; the geometry classes, the field
  aliases, `GeoField`, `GeoDistance` and `InvalidGeometryError` are exported from the package root.
- README section 25, "Geospatial fields" (annotations, values and GeoJSON, `nearby()`,
  `GeoDistance`, spatial lookups, live queries, computed geometry fields, migrating stored GeoJSON,
  type checkers, a note for users of the full SurrealDB-ORM); example notebook
  `examples/v0.24.0_geospatial_fields.ipynb`.

### Changed

- **Geometries go through the encoding boundary.** An ORM geometry is sent as the equal SDK
  geometry; an SDK geometry is sent as it is once checked. An SDK geometry every SurrealDB line
  refuses (a line of fewer than 2 points, a polygon without a ring, a ring of fewer than 4 points
  once closed, a coordinate that is not a number, a member of the wrong kind) raises a plain
  `ValueError` before anything is sent.
- Rows applied after a write without validation (`save()` without an id, `refresh()`, `merge()`,
  `upsert()`, `patch()`, the `atomic_*` helpers) turn the SDK's geometries into the ORM's classes
  in top-level geometry fields, so writing the instance back stores a geometry. Sub-models keep the
  row's `dict`, as before (#228).

### Fixed

- A `Decimal` coordinate written with an exponent in an SDK geometry is now sent in fixed-point
  notation, like a top-level `Decimal`: SurrealDB 2.x refused `Decimal("1E-7")` there, and both
  lines stored a long one with a wrong value. Both lines now store the value.

### Notes

- **Same on both lines**, measured on SurrealDB 2.7.0 and 3.3.4, identical on 3.1.5: storage,
  reads, `nearby()`, distances, the spatial lookups and live notifications. The README's
  "SurrealDB 2.6.x vs 3.x" table gains three rows, one of them retroactive: a GeoJSON dict written
  to an untyped field (`Any`, `dict`) is stored as a plain object, not a geometry.
- **Migrating from the full SurrealDB-ORM**, which stores geometry fields as GeoJSON dicts (plain
  objects that `geo::*` functions refuse): a geometry field reads such a value, and `update()`
  writes it back as a native geometry, so loading and updating every record migrates a table.
- **Known limits**: the surrealdb SDK 2.0.0 cannot read a degenerate geometry another client
  wrote (a 1-point line, a ring of fewer than 4 points). Geometries inside sub-models are written
  back as plain objects after a write that syncs the instance (#228), and ORM geometries in
  untyped fields read back as the SDK's classes (#222). Filter values of other lookups and keyword
  writes are not validated against the field: pass geometry objects there (#220, #221).
  `GeoField["point"]` is not understood by type checkers, as in the full ORM; use the aliases or
  the classes in type-checked code.

## [0.23.0] - 2026-10-14

Rich field types: `timedelta`, `date`, sets and the ORM's own `Duration` and `Range[T]` are stored
as SurrealDB's native values and read back as the declared type, with the same behaviour on
SurrealDB 2.x and 3.x. Every value the ORM hands the official SDK now goes through one encoding
step. The first minor of Tier 2.

### Added

- **Rich field types.** A model can declare `timedelta`, `date` (subclasses included) and
  `set[X]`/`frozenset[X]` fields, and the ORM's `Duration` and `Range[T]`. Every write stores them
  natively (a duration, a `datetime` at midnight UTC, an array sorted when its items can be
  ordered, a range) and every read returns the declared type. Optional fields and values nested in
  containers or sub-models are sent the same way at any depth. `datetime`, `Decimal`, `UUID` and
  `bytes` fields keep working as before:

  ```python
  from datetime import date, timedelta
  from surreal_orm_lite import (
      BaseSurrealModel, BoundExcluded, BoundIncluded, Duration, Range, SurrealConfigDict,
  )

  class Subscription(BaseSurrealModel):
      model_config = SurrealConfigDict(table_name="subscription")
      id: str | None = None
      renews_on: date
      trial: timedelta
      grace: Duration | None = None
      active_hours: Range[int]
      tags: set[str] = set()

  await Subscription(
      id="acme",
      renews_on=date(2026, 2, 1),
      trial=timedelta(days=14),
      grace=Duration.parse("1h30m1ns"),                         # nanoseconds kept
      active_hours=Range(BoundIncluded(9), BoundExcluded(17)),  # 9..17
      tags={"pro", "annual"},
  ).save()
  sub = await Subscription.objects().get("acme")
  sub.trial  # timedelta(days=14): a duration on the server
  ```

- **`Duration`**: a SurrealDB duration with the server's nanosecond precision, where `timedelta`
  stops at microseconds. It mirrors the SDK 2.0.0 `surrealdb.Duration`, the same members under the
  same names and parameters (`elapsed`, `parse(value, nanoseconds=0)`, `get_seconds_and_nano()`,
  `to_compact()`, `to_string()`, the unit properties), with exact results: `to_compact()` keeps
  the nanoseconds and `to_string()` writes every unit. `parse()` reads the server's syntax strictly
  (`"1h 30m"` and `"1.5h"` are refused). It adds `MAX_NANOSECONDS`, `from_timedelta()`,
  `to_timedelta()`, hashing and ordering. As a field it accepts a `Duration`, the SDK's `Duration`,
  a non-negative `timedelta` or a SurrealQL duration string (`"1h30m"`); in JSON it is that text.
- **`InvalidDurationError`**: raised for a value `Duration` refuses. It is both a `ValueError` and
  a `surrealdb.InvalidDurationError`, so an `except` written against the SDK catches it.
- **`Range[T]`**: a SurrealDB range whose sides are the SDK's `BoundIncluded`/`BoundExcluded` (now
  exported by the ORM) or `None` for an open side; the SDK's own spellings of an open side
  (`Bound()`, `surrealdb.cbor.undefined`, a CBOR null) are accepted too. `str()` is the SurrealQL
  range text (`9..17`, `1>..=5`, `65..`), which a `Range` field reads back, and which writes every
  bound exactly, including values a live-query filter refuses. As a field it validates each bound
  value as `T` and accepts the SDK's `Range`, a builtin `range` with step 1 and that text; in JSON
  it is that text.
- **`in`/`not_in` with a `Range`** (the ORM's or the SDK's) bind it as one value (`hour IN $_f0`)
  instead of refusing it as a non-iterable; a builtin `range` keeps its meaning, the list of its
  integers. On the record-id column `id` and on an edge's `in`/`out` columns it raises
  `ValueError`, since a record id never falls inside a value range. `contains` on a range column
  tests membership (`active_hours__contains=12`).
- **Live-query literals** for durations (`1h30m1ns`, exact to the nanosecond), dates (midnight
  UTC), sets (sorted when their items can be ordered) and ranges, so `live()` and `watch()` filters
  on rich fields give the same results on both lines.
- `Duration`, `Range`, `BoundIncluded`, `BoundExcluded` and `InvalidDurationError` are exported
  from the package root.
- README section 24, "Rich field types" (annotation table, `Duration`, `Range[T]`, filters,
  precision, sets, values refused before sending, rows applied after a write, values the SDK
  cannot read, authentication, a note for users of the full SurrealDB-ORM); example notebook
  `examples/v0.23.0_rich_field_types.ipynb`.

### Changed

- **Every value the ORM sends goes through one encoding step** before it reaches the official SDK:
  the fields of every write API, filter values, `raw_query()` and `call_function()` variables,
  transaction statements and auth variables (`signup()`, and the `variables=` of
  `SurrealDBConnectionManager.signin()`/`signup()`). A value v0.22.2 already sent as it was is
  handed to the SDK unchanged.
- **Values the server would reject are refused before anything is sent**, with a plain
  `ValueError` that says why, never re-wrapped in a `SurrealDbError`: a negative duration, a
  duration of `2**64` seconds or more, a zero duration while the installed SDK cannot read one back
  (SDK 2.0.0), a finite `Decimal` whose integer part, rounded, exceeds `2**96 - 1` (the largest
  SurrealDB decimal), and an `int` outside the signed 64-bit range (one between `2**63` and
  `2**64 - 1` was stored as a negative number; use `Decimal`). For a zero duration, store `None`
  and filter "longer than zero" with `__gte=Duration(1)`.
- A `Decimal` written with an exponent (`Decimal("1E-7")`, `Decimal("1E+3")`) is sent in
  fixed-point notation (`0.0000001`, `1000`), cut after the 29th fractional digit. The server keeps
  at most 28 fractional digits, rounding half away from zero as before, so the stored value is the
  same. A value without an exponent is sent as before.
- `atomic_increment()` refuses a duration amount (a `timedelta`, the SDK's or the ORM's
  `Duration`) with `TypeError` before anything is sent: SurrealDB 2.x ignores `+=` with a duration
  where 3.x applies it, so the same call gave a different row on each line. Set the new value with
  `merge()`. Numeric and `Decimal` amounts are unchanged.
- An SDK `Range` whose open side is written `undefined` (or `Bound()`) is now sent with the
  protocol's null bound; the server reads it the same way, as an open side.
- Rows applied after a write without validation (`save()` without an id, `refresh()`, `merge()`,
  `upsert()`, `patch()`, the `atomic_*` helpers) turn the SDK's duration into a `timedelta` or a
  `Duration` in top-level fields of those types, so writing the instance back stores a duration.
  Every other value is applied as received, as before.

### Fixed

- `timedelta`, `date`, `set`/`frozenset`, SDK `Duration` and open-sided `Range` values, `Decimal`s
  written with an exponent (zeros such as `0E+30` included) and `int`s outside signed 64-bit are now
  converted or refused before sending, in writes, filter values, `call_function()` arguments and
  auth variables.
- A row holding a duration now loads into a top-level `timedelta` field (floored to the
  microsecond) through `get()`, `filter()`, `all()`, `first()`, `raw_query()`, live and
  change-feed events, model `signup()` and the rows `bulk_create()` returns; the field's validation
  refused the SDK's duration.
- The live-filter `Decimal` literal decides huge exponents before formatting: a zero with a long
  exponent is written without building its full text, and a value beyond Python's 4,300-digit
  limit raises the documented `TypeError` instead of a `ValueError`.

### Notes

- **Same on both lines**, measured on SurrealDB 2.7.0 and 3.3.4, with 3.1.5 where it differs,
  except `TYPE set<…>` columns: on 3.3 such a column holds a native set that the surrealdb SDK
  2.0.0 cannot read (reads fail with an SDK decoding error), and 3.1 refuses the array. Declare
  such columns `array<…>`, optionally with `VALUE $value.distinct()`. The README's "SurrealDB 2.6.x
  vs 3.x" table gains these two rows and a retroactive one: on reads, 2.x keeps a `Decimal`'s
  trailing zeros (`12.50`) where 3.x normalises them (`12.5`); the values are equal.
- **SDK 2.0.0 limits**: it cannot decode a zero duration or a SurrealDB 3.x native set. The ORM
  never sends either, but a record another client wrote may hold one, and reading it fails with an
  SDK decoding error. A later SDK lifts both (#205).
- **Filter values on rich fields must be typed**: strings are not converted, so
  `filter(trial__gte="7d")` compares the stored duration with a string; pass `timedelta(days=7)`
  (#220). Keyword writes (`merge(field=...)`, `bulk_update()`) are not validated against the
  field's type (#221).
- **Precision**: `datetime` to the microsecond (the SDK truncates the server's nanoseconds);
  `timedelta` floored to the microsecond on read; `Duration` exact. On reads, a `timedelta` below a
  top-level field (`list[timedelta]`, a sub-model's field) fails validation as before: declare
  `Duration` there.
- After a write that returns the row, `date`, `set` and `Range` fields hold the SDK's value (a
  midnight `datetime`, a `list`, the SDK's `Range`) until the record is loaded again; writing such
  an instance back is safe.
- **Equality with the SDK's values is one-way**: `Duration(5) == surrealdb.Duration(5)` is `True`,
  the reverse is `False` (the SDK's `__eq__` answers `False` for any other type); the same holds
  for `Range`. Keep the ORM value on the left.
- Tested against SurrealDB 3.3.4 and 2.7.0 (the CI pins), plus 3.1.5 for backward compatibility.

## [0.22.2] - 2026-10-13

Change-feed fixes: history replayed by `changes(since=None)` on SurrealDB 3.x, and a timed-out
read that could hang on both lines. No new public API.

### Fixed

- **`changes(since=None)` no longer replays history on SurrealDB 3.x** (#212). On 3.x the stream
  started at the server clock's millisecond (`unix_ms << 16`), but a 3.x versionstamp is
  `unix_ms << 16 | counter` and `SINCE` is inclusive, so every entry committed earlier in that
  same millisecond was delivered as if it were new, and `stream.cursor` did not point at the end
  of the feed. `start()` now reads the entries stamped in that millisecond, a page of
  `batch_size` at a time, and moves the cursor past the last one: usually one short read, at most
  `ceil(n / batch_size) + 1` reads for the `n` entries of that millisecond, however busy the
  table is. It goes through the same retry and `stop()` path as every other request.
  - A write committed in that millisecond while `start()` runs is history and is skipped; a write
    in a later millisecond, or one made after `start()` returns (even in the same millisecond),
    is delivered.
  - This was the cause of two intermittent change-feed test failures on SurrealDB 3.x.
- **A change-feed read under `asyncio.timeout()` or `wait_for()` could hang forever**, on both
  server lines. When the read was cancelled in the very loop iteration the server's answer
  arrived, the official SDK (surrealdb 2.0.0) raised a `KeyError` over the `CancelledError` and
  its connection died, which is what a dropped connection looks like. The stream retried it as one
  and lost the cancellation: the timeout never fired and the read polled on, for as long as the
  process ran. A cancellation of the reading task is now re-raised instead of retried, so the read
  times out as asked and loses nothing (the next read starts from the cursor). A live query's
  automatic resubscribe gets the same guard, so cancelling it (`asyncio.run()` shutting down, for
  instance) no longer has it reconnect on.

### Notes

- **`changes(since=None)` on SurrealDB 2.x is unchanged**: there "now" is the end of the feed,
  found by searching the counter, which never had this problem. The README's "SurrealDB 2.6.x vs
  3.x" table now says how the 3.x side finds "now". The timed-out read fix is the same on both
  lines: the race is in the SDK, not in the server.
- Tested against SurrealDB 3.3.4 and 2.7.0 (the CI pins), plus 3.1.5 for backward compatibility.

## [0.22.0] - 2026-10-12

Native typed relations: graph edges as Pydantic models, created through the official SDK's
`insert_relation()` and returned as typed instances, with the full SurrealDB-ORM's graph API on
both server lines. The last minor of Tier 1.

### Added

- **Typed edges — `BaseSurrealEdge`**. An edge table becomes a model whose data is validated by
  Pydantic, with frozen `in_` (column `in`) and `out` record endpoints that accept a saved
  instance, a `RecordID` or a `"table:id"` string. `relate()` takes the edge class and returns the
  created edge:

  ```python
  from pydantic import Field
  from surrealdb import RecordID
  from surreal_orm_lite import BaseSurrealEdge, SurrealConfigDict

  class Purchased(BaseSurrealEdge):
      model_config = SurrealConfigDict(table_name="purchased")
      quantity: int = Field(ge=1)
      price: float

  edge = await alice.relate(Purchased, widget, quantity=2, price=9.5)
  assert isinstance(edge, Purchased) and edge.in_ == RecordID("User", "alice")
  ```

  `relate()` is typed with two overloads: the edge class for an edge model, `list[dict]` for a
  table name (the v0.6.0 form, unchanged).

- **Edge `save()` and `QuerySet.bulk_create()` through `insert_relation()`** — a real relation that
  graph traversal sees, `id` hydrated from the returned row; `bulk_create()` of N edges without an
  id is one `insert_relation()` call. An explicit edge id that already exists raises
  `SurrealDbError` ("already exists") and writes nothing, on every measured version.
- **Edges in transactions**: `save(tx=)`, `relate(Edge, …, tx=)` and `bulk_create()` under
  `objects(tx=)`, interactive (3.x over WebSocket) and buffered (2.x, or HTTP); an auto-id edge is
  allowed in a buffered transaction (its `id` stays `None` until the commit).
- **`get_or_create(in_=a, out=b)` on an edge model** is an idempotent relate; instances,
  `RecordID`s and `"table:id"` strings converge on the same row.
- **`SurrealConfigDict(table_name=…)`** — the full ORM's key for a model's table name, honoured by
  every path (writes, reads, `QuerySet`, live queries, change feeds, DDL helpers) and validated as
  an identifier when the class is defined.
- **Model instances as filter values**, for every model and column: keyword filters, `Q`, every
  scalar lookup and each item of `in`/`not_in`/`containsany`/`containsall`. The create payload of
  `get_or_create`/`update_or_create` gets the same conversion, so
  `Order.objects().get_or_create(customer=alice)` stores a record id.
- **`"table:id"` strings on edge endpoint columns** in filters (`filter(in_="User:alice")`,
  `filter(in___in=[alice, "User:bob"])`), live queries included.
- **Graph helpers in the full SurrealDB-ORM's parameter order**, by position or by keyword:
  `relate(edge, target, reverse, tx)`, `remove_relation(edge, target, reverse, tx)`,
  `remove_all_relations(edge, direction, tx)`, `get_related(edge, direction, model_class)`; only
  the lite's `data=` stays keyword-only. With them:
  - `reverse=True` on `relate()` and `remove_relation()` (target → edge → self);
  - edge fields as keyword arguments on `relate()` (`**edge_data`), merged with `data=` (a field
    given both ways raises `TypeError` before any call);
  - `tx=` on `relate()`, `remove_relation()` and `remove_all_relations()`;
  - edge model classes accepted wherever an edge name is, in every helper;
  - a list or tuple of edges for `remove_all_relations()`;
  - `get_related(direction="both")`: out-neighbours, then in-neighbours, in one query;
  - a bare record id as `remove_relation()` target (`"abc123"`): compared as a string with the id
    of the edge's `out` (`in` with `reverse=True`), it matches that id in any table, as in
    SurrealDB-ORM. Pass an instance or a `RecordID` to pin the table.
- `BaseSurrealEdge` is exported from the package root.
- README section 23, "Typed edges (`BaseSurrealEdge`)", with "Idempotent edges" (a deterministic
  array id, a `UNIQUE` index on `in, out`) and a "Migrating from SurrealDB-ORM (graph API)" note;
  example notebook `examples/v0.22.0_native_typed_relations.ipynb`.

### Changed

- **`QuerySet` reads the table from `get_table_name()`**, so a model with the legacy
  `_table_name` now writes to that table too; before, writes went to the class-name table while
  `objects()` read the other one.
- **`remove_relation()` and `remove_all_relations()` run a graph-path `DELETE`** with bound record
  ids: exactly the edges a traversal sees. A plain record that merely carries `in`/`out` fields is
  no longer matched, and an edge table that was never created is a native no-op on 3.x too, also
  inside a transaction.
- `relate()` (with an edge class or a table name), `get_related()`, `remove_relation()`,
  `remove_all_relations()` and `traverse()` accept a node whose record id is not an identifier
  (`joe@x.com`).
- **Writes that would change an edge's endpoints are refused** with `ValueError` before any call
  (`merge()`, `patch()` on the instance and the `QuerySet`, the `atomic_*` helpers,
  `bulk_update()`, `update_or_create(defaults=)`): SurrealDB silently ignores them on both lines.
  Delete the edge and relate again.
- **`upsert()` and `save(server_values=)` are refused on an edge** with `SurrealDbError`: an
  `UPSERT` stores a plain record that no traversal sees, and `INSERT RELATION` cannot evaluate
  `SET` expressions.
- An edge write refused by the table's `PERMISSIONS` raises `SurrealDbError`; the server answers it
  with fewer rows and no error.

### Deprecated

- `_table_name` — use `model_config = SurrealConfigDict(table_name=…)`. It still works and emits a
  `DeprecationWarning` when the class is defined (`table_name` wins when both are set); it will be
  removed in a future minor.

### Fixed

- `SurrealConfigDict` keys are optional for type checkers (`total=False`).
- Filter keys on a field ending in `_` (`type___gt`, `in___in`) no longer raise
  "Unsupported lookup".
- `relate(<non-str>, …)` raises `TypeError` instead of `AttributeError`.
- A single model instance given to a sequence lookup (`customer__in=alice`) raises `ValueError`
  instead of being read as a list of `(name, value)` pairs.
- `save(tx=)`, `upsert()`, `update(tx=)` and `delete(tx=)` bind the record id instead of writing
  it into the statement, so a record with an array id (such as `pair:[User:alice, User:bob]`) can
  be saved, updated or deleted in a transaction and upserted in or out of one; before, the
  statement failed to parse on both lines, and inside a transaction it failed the whole
  transaction.

### Security

- Record ids are bound as query parameters instead of being written into the statement as
  `table:id` text: `save(tx=)`, `upsert()`, `update(tx=)`, `delete(tx=)`, `relate()` (with an edge
  class or a table name), `get_related()`, `remove_relation()`, `remove_all_relations()` and
  `traverse()`. The only text written into these statements is identifiers validated before any
  call: the table name (`table_name`, checked when the class is defined), edge names (every item
  of a list), field names and the `traverse()` path.
- An edge write denied by the table's permissions can never look like a success (see Changed).

### Notes

- **A duplicate explicit edge id differs by version, and the ORM evens it out.** A raw
  `INSERT RELATION` with an id that already exists silently keeps the old edge on 2.7.0 and on
  3.1.5 (although the SurrealDB docs announce a record-exists error from 3.1.5), and raises
  `AlreadyExistsError` on 3.3.3. The ORM sends a guarded statement, so it raises "already exists"
  and writes nothing on all three; inside a buffered transaction the commit fails.
- On 2.x, the edge record of a `CREATE`/`UPDATE` live notification or of a change-feed update entry
  carries `"__": true`. The typed `instance` drops it, so it is identical on both lines;
  `ModelChangeEvent.raw` stays as the server sent it.
- A batch of edges partly refused by permissions stays partly written outside a transaction.
- Idempotent edges: `get_or_create()` has a race window between two concurrent callers; a
  deterministic array id or a `UNIQUE` index on `in, out` closes it server-side, on both lines
  (README section 23).
- Schema-level relation tables (`DEFINE TABLE … TYPE RELATION IN … OUT …`) are defined by hand for
  now; the DDL helper, with the full ORM's `table_type`/`relation_in`/`relation_out` keys on
  `BaseSurrealEdge`, is planned for v0.33.0.
- Every behaviour above was measured on SurrealDB 2.7.0 and 3.3.3, with 3.1.5 where it differs;
  the README's "ORM behaviour: SurrealDB 2.x vs 3.x" table gains fifteen rows, one of them
  retroactive (since v0.7.0: an undeclared field on a `SCHEMAFULL` table).

## [0.21.1] - 2026-10-12

Python 3.15 support. No library code changes; the published package behaves exactly as 0.21.0.

### Added

- **Python 3.15** (#206). The CI matrix runs the full suite on Python 3.11 to 3.15 against both
  SurrealDB lines, the package declares the `Programming Language :: Python :: 3.15`
  classifier, and the README's requirements table states the tested range. Lint and the
  coverage upload stay on 3.14.

### Changed

- **`uv.lock` refreshed so the development environment installs from wheels on Python 3.15.**
  Only the locked packages with no CPython 3.15 wheel, or only a pure-Python fallback, were
  upgraded, plus those that follow from their new floors: aiohttp 3.14.5, cffi 2.1.1,
  charset-normalizer 3.5.2, coverage 7.16.2, debugpy 1.8.22, markupsafe 3.0.4, propcache
  0.5.4, pydantic 2.14.1 (pydantic-core 2.50.1), tomli 2.5.0, typing-extensions 4.16.0,
  typing-inspection 0.4.4, websockets 17.2 and yarl 1.25.1.

### Notes

- **The dependency ranges in `pyproject.toml` are unchanged** (`pydantic>=2.13.4`,
  `surrealdb[pydantic]>=2.0.0,<3.0.0`), so this release changes nothing in what your
  environment resolves; the lock only pins what CI and contributors install. The full suite
  passes on the refreshed lock, pydantic 2.14 and websockets 17 (the SDK's WebSocket transport)
  included, on SurrealDB 2.7.0 and 3.3.3.

## [0.21.0] - 2026-10-12

Live queries that survive a dropped connection, and change feeds with a cursor that resumes
exactly — the rest of the full SurrealDB-ORM's real-time API, on the official SDK and on both
server lines.

### Added

- **Auto-resubscribe** — `QuerySet.live(*, auto_resubscribe=True, diff=False, on_reconnect=None)`,
  the full ORM's signature, and the same two arguments on `watch()`. When the WebSocket drops,
  the stream opens a new connection, sends the same live query again (filters, `DIFF`, `FETCH`
  intact) and moves its buffer onto the new uuid, so an `async for` simply carries on:

  ```python
  async def catch_up(old_id, new_id):
      ...  # re-read what changed while the connection was down

  async with Order.objects().filter(status="open").live(on_reconnect=catch_up) as stream:
      async for event in stream:
          ...
  ```

  `on_reconnect(old_id, new_id)` is sync or async, receives `UUID`s, and runs once the new
  subscription is already buffering; an exception in it is logged. Connection failures are
  retried with exponential backoff (`reconnect_delay` 0.5 s, `reconnect_max_delay` 30 s,
  `reconnect_max_attempts` 10 — class attributes on `LiveStream`/`LiveModelStream`); when they
  run out, or on any other failure, the error is raised from the iteration.

- **Change feeds** — `QuerySet.changes(*, since=None, poll_interval=0.1, batch_size=100)` →
  `ChangeModelStream`, the full ORM's API: an async iterator of `ModelChangeEvent` read from
  `SHOW CHANGES`, over HTTP or WebSocket. `stream.cursor` (an `int`) resumes exactly with
  `changes(since=cursor)`, on 2.x and 3.x alike; it advances past a transaction only after its
  last event, so saving it after each event is at-least-once. `since=` also takes a `datetime`
  or ISO-8601 string. With `INCLUDE ORIGINAL`, updates carry `changed_fields`. `start()` fixes
  the starting position early; `stop()` ends the iteration at once, even during a retry
  backoff. A change feed defined on the database (`DEFINE DATABASE … CHANGEFEED`) is accepted.
- `ChangeModelStream` and `ReconnectCallback` are exported from the package root.

### Changed

- **A dropped WebSocket no longer hangs anything.** A stream with `auto_resubscribe=False`, and a
  secondary `subscribe_live()` reader, now raise `SurrealDbConnectionError` on a drop; before,
  they waited forever. Deliberate teardowns (`kill()`, `close_connection()`, …) still end streams
  cleanly and never trigger a resubscribe.
- **`get_client()` replaces a dropped WebSocket client** (signed in, session replayed) instead of
  handing back a dead one that failed every call until `reconnect()`. The dead client's socket is
  closed, which matters when the SDK's receive loop died on an undecodable frame with the socket
  still open: the server would otherwise keep that session's live queries running.
- **Opening a client is serialised per event loop.** Concurrent `get_client()` calls with no
  usable client share one new connection; before, each opened its own and the extras leaked —
  a live query started on one could never be killed.
- `kill()` on a dropped client returns without a network call instead of raising
  `SurrealDbError`, and the live queries of a replaced client are recorded as dead, so a late
  `subscribe_live()` on one of them ends at once.
- A `get_client()` cancelled while opening the connection now closes the half-open socket.
- `live()` and `watch()` no longer refuse `auto_resubscribe=` / `on_reconnect=` with `TypeError`.

### Security

- **A resubscribe never changes the identity a live query runs under.** A live query is
  evaluated with the permissions of whoever opened it, and a new connection comes back as the
  identity the manager replays. The manager stamps each connection with a fingerprint of what it
  was built from (url, namespace, database, user, stored token) whenever it sets its identity. A
  stream opened under an identity that cannot be replayed (`store=False`), or whose fingerprint
  is no longer the replay identity, ends with `SurrealDbAuthenticationError` instead of
  resubscribing — possibly as the configured root user. Settings changed and then restored count
  as no change.
- **A dropped connection never silently comes back as the configured user.** If it ran as an
  identity the manager does not store (`signin`/`signup`/`authenticate` with `store=False`),
  every call on that event loop raises `SurrealDbAuthenticationError` until the application signs
  in or authenticates again, calls `invalidate()`, or `reconnect()`/`close_connection()`.
- **A network failure while the stored session is replayed no longer erases it.** It is reported
  as a retryable `SurrealDbConnectionError`; only a token the server actually refuses is
  forgotten (the behaviour since v0.16.0).
- **Reconnect loops do not retry a server that answered "no".** `get_client()` still raises
  `SurrealDbConnectionError` when the configured credentials are rejected, but live streams and
  change feeds raise it at once instead of retrying it for minutes.

### Notes

- **The change-feed cursor differs by line.** Every entry carries a `versionstamp`, but 2.x's
  `SINCE` takes `versionstamp >> 16` — resuming with the stamp the server returned silently
  yields nothing, forever — while 3.x's takes the stamp itself, inclusively. The ORM reads the
  server version once per stream and stores the next `SINCE` value.
- **`SINCE <datetime>` is unusable as is**: always empty on 3.x, and on 2.x it also returns
  changes from before the moment. A datetime is converted to 3.x's versionstamp layout
  (`unix_ms << 16`, pinned by a test) and passed through on 2.x (at-least-once); "from now" on
  2.x is found by searching the counter.
- A change feed does not record the kind of write: a creation arrives as `UPDATE`. A table
  without a change feed answers `SHOW CHANGES` with `[]` on both lines, so `changes()` checks
  `INFO FOR DB` and refuses it (skipped when `INFO` is not permitted).
- Observed once in CI on a fresh SurrealDB 2.7.0 server, never reproduced locally: a plain write
  to a table with a change feed failed with the retryable "read or write conflict" error, with no
  concurrent writer. `retry_on_conflict` (v0.12.0) handles it; the test suite now uses it there.
- Tested against SurrealDB 2.7.0 and 3.3.3 (the pin since 0.20.1), with 3.1.5 as the
  backward-compatibility check. Every server behaviour described in this entry was measured on
  3.3.2 and re-measured on 3.3.3, with identical results.
- **What a live query misses during an outage is not replayed** — catch up in `on_reconnect`,
  or from a change feed. A request already in flight when the socket drops fails with the SDK's
  own `KeyError`; retrying arbitrary queries is v0.40.0.

## [0.20.0] - 2026-10-07

Typed live queries, with the same API as the full SurrealDB-ORM: code written against
`surreal_orm` migrates by changing the import.

### Added

- **`QuerySet.live(*, diff=False)` → `LiveModelStream`** — an async context manager and
  iterator of `ModelChangeEvent` objects carrying a **model instance**. The queryset's filters
  are applied by the server, and the live query is killed when the block exits:

  ```python
  async with User.objects().filter(role="admin").live() as stream:
      async for event in stream:
          print(event.action, event.instance.name, event.record_id)
  ```

- **`ModelChangeEvent`** — `action`, `instance`, `record_id` (`"user:abc"`), `changed_fields`
  (diff mode, Python field names) and `raw`, field for field the full ORM's event.
- **Filtered live queries** — `filter()` (lookups, `Q`, `Var` with `variables()`) and `fetch()`
  compile into `LIVE SELECT … WHERE … FETCH …`, with field aliases translated. Works identically
  on SurrealDB 2.x and 3.x.
- **Diff mode** — `live(diff=True)` / `watch(diff=True)` use `LIVE SELECT DIFF`; `changed_fields`
  lists the top-level fields each update touched.
- **`post_live_change` signal** — same name and arguments as the full ORM (`instance`, `action`,
  `record_id`, `changed_fields`), run for every event a `live()` stream yields on one background
  task per stream, in event order. A failing handler is logged and never interrupts the stream;
  a normal exit waits for pending handlers (`signal_drain_timeout`, 5 s), an exception cancels
  them.
- **`ModelChangeEvent.validation_error`** — a record that does not fit the model no longer ends
  the stream: the event carries the error and an unvalidated instance, `post_live_change` is not
  sent for it, and a warning naming the failing fields — never their values — is logged once per
  stream.
- **`watch(diff=)`**, and `watch()` now honours the same filters as `live()`.
- `LiveModelStream`, `ModelChangeEvent` and `post_live_change` are exported from the package root.

### Changed

- **`await QuerySet.live()` is deprecated.** It still starts a live query and returns its uuid
  (the v0.19.0 contract), with a `DeprecationWarning` pointing at `async with ….live()` or
  `watch()`. The stream `live()` now returns also implements the coroutine protocol, so
  `asyncio.create_task(qs.live())` and `asyncio.run(qs.live())` keep working; it can be awaited
  once, and an awaited stream refuses to `start()` (and the reverse). `inspect.iscoroutine()` is
  `False` for it. To hold a subscription across tasks, start the stream
  (`await qs.watch().start()`), read **it**, and stop it with `kill(stream.live_id)`.
- Every live query now starts through `LIVE SELECT` sent with `query()` rather than the SDK's
  table-only `live()`. `filter()`, `fetch()` and `variables()` are no longer refused; `select()`,
  `values()`, `annotate()`, `order_by()`, `limit()`, `offset()` and `objects(tx=)` still are, by
  name.

### Notes

- **SurrealDB 2.x drops bound parameters inside a live query.** `LIVE SELECT … WHERE status = $s`
  is accepted, then `$s` evaluates to `NONE` at notification time and the filter matches nothing,
  without an error. For live queries only, the ORM therefore writes each filter value as a
  SurrealQL literal — the full ORM's approach — through a closed encoder (`None`, `bool`, `int`,
  `float`, `str`, `Decimal`, `datetime` in UTC, `UUID`, `RecordID` incl. UUID keys, lists and
  dicts). Any other type, a string containing NUL, an integer beyond 64 bits or a `Decimal`
  beyond 28 significant fractional digits / a 96-bit mantissa raises `TypeError` at the call,
  before anything is sent. A reference that is neither bound through `variables()` nor a server
  parameter (`$auth`, `$session`, …) raises `SurrealDbError`: unbound, it would read as `NONE`
  on every notification. Every literal is checked against the server on both lines by the test
  suite.
- Filter semantics, measured identically on 2.7.0 and 3.3.0: a record entering the filter through
  an update arrives as `UPDATE`; one leaving it reports nothing, nor its later `DELETE`.
- **Diff mode differs by line in its raw form**: the root patch path is `"/"` on 2.x and `""` on
  3.x, and a `DELETE` carries the whole last record on 2.x but a `replace` with `None` on 3.x.
  `live(diff=True)` normalises both; `watch(diff=True)` passes them through. On both lines a
  changed string arrives as `op: change` with diff-match-patch text — JSON Patch extended by
  SurrealDB.
- Not yet: `auto_resubscribe=` / `on_reconnect=` (v0.21.0). Passing them raises `TypeError`.

## [0.19.2] - 2026-10-01

Security release for the locked dependencies. No library code changes; the published package
behaves exactly as 0.19.1.

### Security

- **urllib3 2.7.0 → 2.8.0** (GHSA-8988-9cw3-xx77, GHSA-vxq7-64xx-v4gw, GHSA-gh4c-6fx4-qh6g).
  urllib3 is reached at runtime through the SDK (`surrealdb` → `requests` → `urllib3`) and by
  the dev-only `docker` package.
- **tornado 6.5.8 → 6.5.9** (GHSA-c2m8-h5v5-343r, GHSA-chx6-46f5-w4vp, GHSA-3hv7-mjh2-fv65).
  Dev only: pulled in by `ipykernel` and `jupyter-client` for the example notebooks.

### Notes

- **This updates `uv.lock`, which protects CI and contributors, not your installation.** The ORM
  declares no bound on urllib3 (it never imports it; the SDK's `requests` does), so your
  environment keeps whatever urllib3 it resolved. If it is older than 2.8.0, upgrade it
  (`pip install -U "urllib3>=2.8.0"` or `uv lock --upgrade-package urllib3`).

## [0.19.1] - 2026-10-01

Tested against the new SurrealDB releases on both supported lines: **2.7.0** and **3.3.0**. No
library code changes; the published package behaves exactly as 0.19.0.

### Changed

- **SurrealDB test pins moved**: the 2.x line from 2.6.5 to **2.7.0**, the 3.x line from 3.2.4
  to **3.3.0**. The CI matrix, the README badge, compatibility table and behaviour table, and the
  ROADMAP follow the pins through `scripts/sync_surrealdb_pins.py`. 2.6.5 and 3.2.4 stay listed
  as compatible, tested up to v0.19.0.

### Notes

- **2.7.0 is indistinguishable from 2.6.5 for the ORM.** The full suite passes with the same
  3.x-only skips, and every divergence in the behaviour table was re-measured on it: no native
  transaction RPC, `WITH REFRESH` rejected, a live query on a missing table accepted, nothing
  sent after `kill()`, no `session` key in a live notification.
- **3.3.0 changes one server behaviour.** Signing in as a system user while a record session is
  open used to swap only the permissions, leaving `$auth`, `$access` and `$session.rd` on the
  record so `info()` kept reporting it. 3.3.0 clears all three and `info()` returns `None`.
  Nothing in the ORM depends on the old behaviour, and `invalidate()` remains the portable way
  to log a record user out. The test that pinned the old behaviour now pins each line's, keyed
  on the server's reported version so a further change goes red; it was checked on 3.3.0,
  3.2.4, 3.1.5 and 2.7.0. Every other 3.x divergence holds unchanged on 3.3.0.

### Fixed

- **The SurrealDB version monitor files its failure report again** (#188). `gh issue create`
  refuses the whole call when one label does not exist, and `needs-investigation` never did, so
  the 3.3.0 failure above was never reported. Labels are now created first, with an unlabelled
  issue as fallback.

### Dependencies

- ruff 0.16.7 → 0.16.9 (dev only, #190).

## [0.19.0] - 2026-09-30

Real-time, phase one: subscribe to a table and receive a notification whenever a record is
created, updated or deleted. This is the **raw** layer — notifications arrive as the server's
own envelope — that the typed `LiveQuerySet` planned for v0.20.0 will build on. Live queries
need a WebSocket connection and work on **both SurrealDB 2.6.x and 3.2.x**.

### Added

- **`QuerySet.watch()`** — an async context manager that subscribes to the model's table and
  kills the subscription on the way out, including when the body raises:

  ```python
  from surreal_orm_lite import LiveAction

  async with User.objects().watch() as stream:
      async for notif in stream:
          print(notif["action"], notif["result"])
          if notif["action"] == LiveAction.DELETE:
              break
  ```

  The handle exposes `live_id`, `table`, `is_active` and `stop()`, named to match the full
  ORM's `LiveModelStream` so the typed layer can present the same object.

- **`QuerySet.live()`** — the explicit form, returning the live query's `UUID` for callers that
  need to pass it around or kill it from elsewhere.

- **`SurrealDBConnectionManager.subscribe_live(uuid)`** — an async iterator over the raw
  notification envelopes (`action`, `id`, `record`, `result`, and `session` on 3.x). It is
  **not** a coroutine: call it without `await`. Buffering starts at the call rather than at the
  first iteration, so a write made in between is still reported, and several readers can
  subscribe to the same uuid.

- **`SurrealDBConnectionManager.kill(uuid)`** — stops a live query and ends every stream
  reading it. Idempotent: an unknown or already-killed uuid is a no-op, like the ORM's other
  cleanup-on-a-missing-target operations.

- **`LiveAction`** — a `StrEnum` (`CREATE`, `UPDATE`, `DELETE`, `KILLED`) that compares directly
  against the raw string in an envelope, so no magic values are needed.

- **`LiveStream`** and `LiveAction` are exported from the package root.

### Notes

- **Guards, not silent surprises.** `live()` refuses a non-WebSocket connection with a message
  naming the requirement, rather than letting the SDK's HTTP path raise a bare
  `NotImplementedError`. It also refuses a queryset carrying `filter()`, `select()`, `limit()`,
  `offset()`, `order_by()`, `fetch()`, `values()`, `annotate()` or `variables()` — a table-level
  live query cannot honour them, and quietly watching the whole table instead would be worse
  than an error. The filtered form arrives in v0.20.0. `objects(tx=)` is refused for the same
  reason: a live query has no transaction to join. `subscribe_live()` names the WebSocket
  requirement too, instead of blaming the SDK build.

- **Two measured 2.6.x / 3.x divergences, both normalised.** SurrealDB 3.x refuses to watch a
  table that does not exist while 2.6.x accepts and stays silent; the ORM turns the 3.x
  `NotFound` into a `SurrealDbNotFoundError` naming the table. After `kill()`, 3.x sends a final
  `KILLED` notification and 2.6.x sends nothing at all, which would leave a reader's `async for`
  hanging on 2.6.x; the ORM pushes its own end-of-stream marker so the loop terminates
  identically on both lines. Both rows are in the README behaviour table.

- **Why the ORM does not call the SDK's `subscribe_live()`.** Its generator body is
  `yield ret["result"]`, which discards the envelope and makes `CREATE`, `UPDATE` and `DELETE`
  indistinguishable. The ORM registers its own queue in the connection's public `live_queues`
  registry — the mechanism the SDK's own `subscribe_live()` uses — and keeps the whole envelope.
  Two tests guard this: one fails if the SDK drops the attribute, the other if the SDK ever
  starts yielding the envelope itself (at which point the ORM can drop its registry).

- **`diff` / JSON-Patch mode is deliberately not exposed.** The installed SDK accepts
  `live(diff=True)` but never puts the flag on the wire — `prep_live()` encodes only the table —
  so the argument would change nothing. v0.20.0 implements diff through `LIVE SELECT DIFF`,
  which was verified to work on both lines.

- **Polling is safe.** A read cancelled by `asyncio.wait_for(anext(stream), timeout)` loses
  nothing and leaves the stream alive, and `aclose()` stops one reader without killing the live
  query. The reader is a small iterator class rather than an async generator precisely because a
  cancelled generator finishes for good.

- **What ends a stream.** `kill()` does, leaving a `watch()` block does, and so does
  `close_connection()` / `close_all_connections()` — the first releases every reader on its
  loop before the socket goes, the second every reader on every loop, so an ordinary shutdown
  cannot leave a wedged task behind. `kill()` never opens a connection: leaving a `watch()`
  block after the connection closed does not silently reconnect. A
  WebSocket that drops on its **own** does not: the SDK's receive task absorbs the close
  without telling live-query subscribers, so the iterator stays suspended until you call
  `kill()` or close the connection. Automatic reconnect and resubscribe is v0.21.0.

- **An SDK without `live_queues` is refused, not degraded.** Yielding action-less envelopes
  would silently break every `notif["action"] == …` comparison, and a wrong answer is worse
  than a clear failure. The guard test catches such an SDK in CI instead.

- **Not yet**: notifications deserialized into model instances and filtered live queries
  (v0.20.0), and automatic resubscribe after a dropped WebSocket (v0.21.0).

## [0.18.0] - 2026-09-13

Let a model's **Python surface** differ from its **SurrealDB column surface**, mark the columns
the server owns, and ask for no row back when you do not need it. All three features are
client-side, so they behave **identically on SurrealDB 2.6.x and 3.x**.

### Added

- **Field aliases** — `Field(alias="password_hash")` renames the _column_, not the attribute:

  ```python
  class User(BaseSurrealModel):
      id: str
      password: str = Field(alias="password_hash")

  user = User(id="ada", password="secret")            # either name validates
  await user.save()                                   # stores password_hash
  await User.objects().filter(password="secret").exec()   # queries password_hash
  ```

  The alias is honoured at every boundary the ORM owns: writes (`save`/`update`/`upsert`/
  `merge`/`bulk_create`, `server_values=`, and the whole `atomic_*` family), read hydration
  (`exec()`, `refresh()`, the row a write returns), **every QuerySet clause** (`select`,
  `filter` including nested and negated `Q` objects, `order_by`, `values`, `fetch`,
  `bulk_update`, the `sum`/`avg`/`min`/`max` helpers and the aggregation objects given to
  `annotate()`), the DDL the ORM generates (`computed_field_ddl()`, and
  `AuthenticatedUserMixin`'s `access_ddl()`), and signal payloads (`update_fields` always names
  Python attributes). QuerySet filter rewriting is the piece the full ORM still documents as an
  open gap. A grouped `values()`/`annotate()` result is re-keyed to Python names on the way
  out.

  `patch()` and `QuerySet.patch()` rewrite each pointer's top-level segment (and a `move`/`copy`
  `from`) to the column, so `/password` and `/password_hash` both work; a whole-document
  operation has its object value re-keyed. Dotted paths translate their root in `Q` objects and
  aggregations exactly as in `filter(**kw)` — `Q(**{"address.city": …})` addresses `addr.city`.

- **`get_field_aliases()` / `to_db_field()` / `to_py_field()`** — the mapping, exposed as
  classmethods and cached per class.

- **`SurrealConfigDict(server_fields=[...])`** — columns the server owns (a
  `DEFINE FIELD … DEFAULT`, an event, a trigger). They are dropped from **create** payloads so
  the server's default applies, hydrated back from the row the write returns, and still
  writable when a caller names one explicitly. `get_server_fields()` returns them merged with
  the model's computed fields, which are server-owned by construction.

- **`merge(refresh=False)`** — for fire-and-forget updates, compile the write with `RETURN NONE`
  so the server sends no row back; the literal keyword arguments are applied locally instead.

### Changed

- `BaseSurrealModel` now sets `populate_by_name=True`. Strictly widening: an aliased field
  previously validated only under its alias, which made `User(password=…)` an error in the very
  code the alias exists to keep readable.

- `update()` and `upsert()` keep `server_fields` in their payload, while `save()` omits them.
  Not an inconsistency — omission means different things in the two statements. Probed on 2.6.5
  and 3.2.4 with identical results: a `DEFAULT` is a **create-time** default, so on
  `UPDATE`/`UPSERT … CONTENT` an omitted optional column is _deleted_ and an omitted required
  one fails outright (`Found NONE for field 'created_at' … but expected a datetime`). A uniform
  exclusion would have made `update()` destroy the very columns the feature protects.

- Hydration is consolidated: `_do_save()` and `refresh()` now route through `_apply_record()`,
  so the column → attribute rule lives in one place instead of four ad-hoc loops.

- **`merge()` costs one round-trip instead of two.** The SDK's `merge()` already answers with
  the merged row; the ORM used to discard it and `SELECT` the record again. The instance is now
  synced from that row, which also means server-side changes (a `VALUE` clause, an event) are
  hydrated just as before, and a missing record still raises. Inside an interactive transaction
  the row `tx.add()` returns is used the same way.

- `get_or_create()` / `update_or_create()` store a `server_fields` entry named as a criterion or
  in `defaults` on both branches. Before, the create branch let the server's `DEFAULT` win, so
  the next identical lookup missed the row and created a duplicate.

- A grouped `values()`/`annotate()` row never renames an annotation alias, and an alias that
  spells a grouped field (by name or by column) raises `ValueError` — the row could only hold
  one of the two values. A projection that fails model validation falls back to dicts keyed by
  Python names, matching the grouped branch.

### Fixed

- `merge()` on a table that was never created raised the SDK's raw `NotFoundError` on SurrealDB
  3.x, while 2.6.x raised the ORM's `SurrealDbError`. Both lines now raise `SurrealDbError`
  ("no record found").

### Notes

- **`refresh=False` forfeits the missing-record check.** That check _is_ the returned row, so a
  merge against a record that no longer exists becomes a silent no-op instead of a
  `SurrealDbError`, and a field computed by `server_values` keeps its stale value until the next
  `refresh()`. Keep the default whenever you need to know the write landed.

- Only a plain, symmetric `Field(alias=…)` is treated as a column rename. A separate
  `validation_alias` / `serialization_alias`, and in particular `AliasPath` / `AliasChoices`,
  describe something other than a renamed column and are left entirely to Pydantic. Write
  payloads are re-keyed through the ORM's own map rather than `model_dump(by_alias=True)`,
  which would honour a `serialization_alias` and write the row under a name `filter()`,
  `select()` and hydration never look for — landing the value in the database and making it
  unreachable through the ORM.

- **The alias map must be a bijection**, checked the first time it is used: an alias that
  collides with another field's name, or two fields sharing one column, raises `ValueError`.
  Either shape collapses two payload keys into one and silently drops a field.

- `AuthenticatedUserMixin`'s SIGNUP clause now also skips `server_fields`, for the same reason
  `save()` does — SIGNUP is a `CREATE`, so listing the column would overwrite the server's
  `DEFAULT` with `NONE` and force every caller to supply a value the server is meant to mint.

- `merge()` now reserves `refresh` alongside `tx`, `server_values` and `extra_vars`.

- No new SDK surface: nothing was added to `_sdk.py`. Verified on SurrealDB **2.6.5** and
  **3.2.4**; no test in this version is skipped on either line.

## [0.17.0] - 2026-09-11

Authenticate a **model**, not just the connection: declare a user model, let the ORM generate its
`DEFINE ACCESS` statement, and get **hydrated model instances** back from signup and signin —
each on its own short-lived connection, so the shared client's identity is never disturbed.

### Added

- **`AuthenticatedUserMixin`** — mix it into a `BaseSurrealModel` subclass for
  `signup()`, `signin()`, `authenticate()` and `refresh()`:

  ```python
  class User(AuthenticatedUserMixin, BaseSurrealModel):
      model_config = SurrealConfigDict(access_name="account")

      id: str | None = None
      email: str
      password: str
      name: str = ""

  await User.define_access()                 # once, at start-up

  result = await User.signup(email="ada@example.com", password="s3cret", name="Ada")
  result.user     # a User instance, with the id the server generated
  result.tokens   # AuthTokens

  me = await User.authenticate(result.tokens.access)   # token → current user
  ```

- **`AuthResult`** — a frozen, generic dataclass pairing `.user` with `.tokens`. Its repr
  delegates to `AuthTokens`, so no JWT reaches a log line, a traceback or an assertion diff.

- **`access_ddl()` / `define_access()`** — render and apply the model's authentication DDL,
  mirroring v0.14.0's `computed_field_ddl()` / `define_computed_fields()` pair. `access_ddl()`
  is pure, so it can be printed, diffed or fed to a migration; `define_access()` is idempotent
  and safe at start-up.

- **`SurrealDBConnectionManager.ephemeral_client()`** — a short-lived, anonymous client on the
  configured URL/namespace/database that never enters the per-event-loop cache.

- **Auth keys on `SurrealConfigDict`** — `access_name`, `identifier_field`, `password_field`,
  `auth_algorithm`, `auth_duration_token`, `auth_duration_session`, `auth_duration_grant` and
  `with_refresh`.

- **`SurrealDBConnectionManager.authenticate(token, *, refresh=None)`** — accepts the refresh
  token that belongs with _token_, for callers holding the matching pair. Omitted, behaviour is
  unchanged (any stored refresh token is dropped).

- **`SurrealDBConnectionManager.require_connection()`** — the "Connection not been set." check,
  now owned in one place instead of being re-spelled by each caller.

### Notes

- **Session isolation is the point.** v0.16.0's `signin()` re-identifies the process-wide client,
  which makes it unusable per-request in a concurrent server. Every method here runs on its own
  ephemeral connection instead, so the shared client keeps the identity `set_connection()` gave
  it. Pass `bind=True` to opt into the older, process-wide behaviour.

- **The `DEFINE TABLE` ships with the access method by default.** Without
  `PERMISSIONS FOR select WHERE id = $auth.id`, a signin succeeds and `$auth` is set yet the
  server returns no record — v0.16.0 documented that as this feature's sharpest gotcha, and an
  API that promises an instance cannot walk into it. When the record is unreadable anyway, the
  raised error names the missing permission instead of returning `None`.

- **The hydrated instance's password field holds the hash**, not the plaintext, because
  SurrealDB returns the stored record verbatim.

- **A configured `primary_key` drives the record id**: the SIGNUP clause targets
  `type::thing('<table>', $<pk>)`, so a signed-up record is addressable by
  `User.objects().get(...)` afterwards. Models must declare either an `id` field or a
  `primary_key`, as every ORM model must.

- **`signup()` validates the whole payload before the exchange.** A required field with no
  default used to be sent as `NONE`: SurrealDB created the account and issued tokens, and only
  then did hydration fail — leaving an orphaned record the caller never saw. Model defaults are
  now applied, and a genuinely missing field raises before any request.

- **`define_access()` normalises errors from a native interactive transaction too.** Those raise
  `SurrealDbError` rather than the SDK's `ServerError`, which previously slipped past the
  wrapper. The apply-DDL path is now shared with `define_computed_fields()`, so both benefit.

- **Same on both DB lines** for `access_ddl`, `define_access`, `signup`, `signin` and
  `authenticate`. **3.x only**: `with_refresh=True` and `refresh()` — SurrealDB 2.6.x cannot
  parse `WITH REFRESH`, so `define_access()` raises a message naming the requirement and those
  tests self-skip. Refresh tokens **rotate**: a spent one is rejected immediately.

## [0.16.0] - 2026-08-29

Authenticate the connection as a SurrealDB **record user** or a different **system user**, keep
that identity across reconnects, and read back who is signed in.

### Added

- **Five auth methods on `SurrealDBConnectionManager`** — `signin()`, `signup()`,
  `authenticate()`, `invalidate()` and `info()`, all native SDK primitives:

  ```python
  tokens = await SurrealDBConnectionManager.signup(
      access="account", variables={"email": email, "pass": password},
  )
  tokens = await SurrealDBConnectionManager.signin(
      access="account", variables={"email": email, "pass": password},
  )
  me = await SurrealDBConnectionManager.info(return_type=User)   # User | None
  await SurrealDBConnectionManager.invalidate()                  # back to the app identity
  ```

  `signin()` takes exactly one of three credential shapes: record access (`access=` +
  `variables=`), a system user (`username=` + `password=`), or a refresh exchange (`access=` +
  `refresh=`). `namespace`/`database` default to the configured connection's **for record
  access only** — a root user is defined at neither level, and sending them would turn a root
  signin into a database-user signin against a user the server does not have.

- **`AuthTokens`** — a frozen dataclass with `access` (guaranteed non-`None`) and `refresh`.
  Its `repr` redacts both values while still showing whether a refresh token is present, and
  there is deliberately **no `__str__` returning the JWT**, so a token cannot leak into a log
  line, a traceback or a pytest assertion diff by accident. Read `tokens.access` explicitly.

- **Session persistence** — the authenticated identity survives `reconnect()`, a dropped client
  and a new event loop: `get_client()` replays the stored token. Without it, a reconnect would
  silently hand back the configured root identity — a privilege change nobody asked for.

- **`SurrealDbAuthenticationError`** — one exception for every authentication failure. The SDK
  reports them inconsistently (the same wrong password is `NotFoundError` on 3.x and
  `InternalError` on 2.6.x; a malformed token is a client-side `ValueError` that never reaches
  a server), so the ORM normalises them all. It subclasses `SurrealDbError`, so existing
  `except SurrealDbError` handlers keep working.

- **Refresh-token exchange** (SurrealDB 3.x) — renew an access token without the password:

  ```python
  tokens = await SurrealDBConnectionManager.signin(access="account", refresh=stored_refresh)
  store(tokens.refresh)   # the previous one is now dead — see Notes
  ```

- **`is_authenticated()`, `get_session_token()`, `get_refresh_token()`** — introspection, for
  putting a JWT in a web session — and **`clear_session()`**, the local counterpart to
  `invalidate()`: it forgets the stored tokens without a server round trip and without
  touching the live session.

### Changed

- `_coerce_call_result` was generalised to `_coerce_result(value, return_type, subject)` so
  `info(return_type=)` reuses the one `TypeAdapter` path `call_function()` already used.
  Internal only; `call_function()`'s messages and behaviour are unchanged.

### Notes

- **Refresh tokens rotate.** A successful exchange kills the token it spent, immediately and
  permanently. Persist the new `tokens.refresh` before the next request: dropping it logs the
  user out for good, with nothing raised at the call site to warn you.
- **Refresh tokens are SurrealDB 3.x only.** `DEFINE ACCESS … WITH REFRESH` does not parse on
  2.6.x ("expected the experimental bearer access feature to be enabled"), so `tokens.refresh`
  is always `None` there and their tests self-skip. Everything else behaves identically on both
  lines.
- **`invalidate()` returns the connection to its configured identity** rather than leaving it
  anonymous. The SDK's own `invalidate()` leaves the session anonymous, and since the manager
  caches one client per event loop, every later ORM call on that connection would fail.
- **`info()` returns `None` unless the record's table grants that record user `select` on
  itself.** The signin succeeded and `$auth` is set, but the server returns nothing — quiet and
  easy to misread. Grant e.g. `PERMISSIONS FOR select WHERE id = $auth.id`. Because the cause is
  a permission, a `None` is passed through untouched even when `return_type=` is given.
- **Signing in as a system user does not end a record session.** SurrealDB swaps the
  permissions but leaves `$auth` pointing at the record, so `info()` keeps reporting it. Only
  `invalidate()` really logs a record user out. Identical on both lines.
- **`namespace=` and `database=` go together** for record access. Pairing one explicit value
  with the configured default would point the signin at a target the caller never named, so
  half-explicit raises `ValueError`.
- **`set_connection()` drops any stored session token**, the same way it already drops the
  cached stored-function signatures: a JWT is bound to a server, namespace, database and
  access method, so it must not survive a re-point.
- **Auth changes the identity of the whole connection.** One client is cached per event loop and
  every model shares it, so an auth call affects every subsequent ORM operation — not just the
  caller's.

## [0.15.0] - 2026-08-25

Call custom server-side functions (`DEFINE FUNCTION fn::…`) from the ORM.

### Added

- **`SurrealDBConnectionManager.call_function()`** — invoke a stored function, with arguments
  bound as query parameters:

  ```python
  acquired = await SurrealDBConnectionManager.call_function(
      "fn::acquire_lock", [table_id, pod_id, 30],
  )
  ```

  The `fn::` prefix is added when absent, and nested namespaces (`fn::billing::total`) work.
  The function _name_ is interpolated (SurrealQL takes no bound parameter in call position) and
  is therefore validated as an identifier path first — an invalid name raises `ValueError`
  before any query is issued. Argument _values_ are never interpolated.

- **Named arguments via `params=`** — SurrealQL function arguments are positional, so the ORM
  reads the function's declared signature (`INFO FOR DB`, cached per URL/namespace/database)
  and orders them for you. The mapping's own order is irrelevant:

  ```python
  await SurrealDBConnectionManager.call_function(
      "fn::acquire_lock", params={"pod_id": pod_id, "table_id": table_id, "ttl": 30},
  )
  ```

  A stale signature self-heals: on a key mismatch the entry is dropped and re-read once, so a
  function redefined at runtime resolves instead of failing. The one exception is a function
  redefined with the same parameter names in a **different order**, which a mapping carries no
  order to detect — call `clear_function_signature_cache()` after such a change. `args` and
  `params` together raise `ValueError`.

- **`return_type=`** — coerce the result with one `pydantic.TypeAdapter` pass, so a model, a
  dataclass, a scalar and `list[Model]` are all supported. A mismatch raises
  `SurrealDbValidationError`.

- **`tx=`** — a stored function called inside `async with transaction()` runs **inside** that
  transaction on both DB lines. Without it the call would run outside an open transaction and
  silently break atomicity, which matters because stored functions typically mutate state.

- **`BaseSurrealModel.call_function()`** — the same call for code organised around models. A
  stored function is not table-bound, so it delegates verbatim.

- **`clear_function_signature_cache()` / `function_signature_cache_size()`** on the connection
  manager, for redefining functions out-of-band. The cache is cleared by `set_connection()` and
  `unset_connection()` too, since signatures belong to the server that declared them.

### Notes

- **The ORM emits the bare call form `fn::name($_fnarg0);`, never `RETURN fn::name(…)`.** A
  `RETURN` inside a `BEGIN … COMMIT` batch terminates the transaction early **and silently**:
  the statement queued after it is reported `status: OK` and never executes. Verified on both
  2.6.5 and 3.2.4. The bare form returns the same value in every context without that hazard,
  and a regression test pins it.
- **`tx=` return value differs by strategy** (the v0.9.0 contract): interactive transactions
  (WebSocket + SurrealDB 3.x) return the function's value immediately; buffered ones
  (SurrealDB 2.6.x or HTTP) queue the call and return `None` until commit. Combining
  `return_type=` with a buffered `tx=` raises `ValueError` rather than silently returning
  `None`.
- **Signature reads bypass the transaction** — `params=` therefore works inside a buffered
  transaction, where reads are otherwise forbidden. A schema read needs no transactional
  isolation.
- **A parameter name that collides with a reserved word is quoted differently by each line.**
  SurrealDB 2.6.x reports `fn::f($by: int)` as ``$`by` `` while 3.x leaves it bare; the
  signature parser accepts `$name`, ``$`name` `` and `$⟨name⟩`.
- The call itself behaves identically on SurrealDB 2.6.x and 3.x.
- Passing a bare `str` as `args` raises `TypeError` rather than binding one argument per
  character.
- **A missing function is reported at a different moment per strategy.** Outside a transaction
  and inside an interactive one it raises `SurrealDbNotFoundError` at call time. Inside a
  buffered one the call is only queued, so the failure cannot be known until commit and arrives
  as `SurrealDbError` from the transaction layer.
- **Deferred**: a `define_function()` DDL helper. Declaring is DDL and belongs with the other
  `schema.py` helpers planned for **v0.31.0**; v0.15.0 is about calling. Declare a function with
  `client.query("DEFINE FUNCTION fn::… { … };")` meanwhile.

## [0.14.5] - 2026-08-23

Connection correctness: the client cache is now per event loop.

### Fixed

- **A cached client is no longer handed to a different event loop.**
  `SurrealDBConnectionManager` kept a single client in a class attribute with no notion of the
  loop that created it. A SurrealDB WebSocket client is bound to its loop, so the second of two
  successive `asyncio.run(...)` calls in the same process got a client belonging to a loop that
  no longer existed and failed with `got Future attached to a different loop` — or hung outright,
  which is what writing the regression test revealed
  ([#163](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/163)). The cache is keyed by
  the running loop, so each loop gets its own connection. Also reached by a sync wrapper that
  spins a loop per call, a test suite with per-test loops (how it was found), and any framework
  that runs handlers on more than one loop.
- **Two live loops no longer evict each other.** Threads or multi-loop servers each keep their
  own client instead of stealing the single cached one.

### Added

- **`close_all_connections()`** — tear down every cached client whatever loop created it.
  `unset_connection()` now uses it. A client belonging to another _live_ loop is dropped rather
  than awaited, since it cannot be closed from here.

### Changed

- **`close_connection()` closes the running loop's client only.** Closing another loop's client
  from here is both impossible and wrong. Single-loop applications see no difference.
- **`is_connected()` answers for the loop asking.** Inside a running loop it reports that loop's
  client; called outside one it reports whether any loop still holds a client.

### Notes

- Entries whose loop is closed are pruned on the next `get_client()`. The stale client is dropped
  rather than closed — `close()` would have to be awaited on a loop that is already gone — and its
  socket is released when the loop is finalised.
- The public API is unchanged apart from the new `close_all_connections()`. A long-lived
  single-loop application behaves exactly as before: one loop, one connection.
- Loop binding is an asyncio/SDK property, not a server one, so this is identical on 2.6.x and
  3.x. Example notebook: `examples/v0.14.5_event_loops.ipynb`.

## [0.14.4] - 2026-08-23

Follow-up correctness release: record-id lookups. Reproduced against live SurrealDB **3.2.4
and 2.6.5** before the fix and verified on both afterwards.

### Fixed

- **`filter(id=...)` matches again.** A record's `id` column holds a native `RecordID`, but
  the `WHERE` builder bound whatever Python value the caller passed — so `filter(id="alice")`
  compiled to `id = $_f0` with a plain string, and SurrealDB does not consider a `RecordID`
  equal to the string form of its identifier. The lookup matched nothing, with no error and
  no warning ([#159](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/159)). It now
  goes through `coerce_record_id()`, which accepts a `RecordID`, an `int`, a bare identifier
  (`"alice"`), a backticked one (``"`5`"``) and the full `"User:alice"` form — the last
  unwrapped only when the prefix names the queried table, since `"Other:d"` may genuinely be
  the stored string id. The value's **Python type** still decides which record is addressed:
  `filter(id=5)` is the integer record id, `filter(id="5")` the string one.
- **`get_or_create(id=…)` / `update_or_create(id=…)` converge.** They inherit the same lookup,
  so the criterion never matched: every call took the create path and the second one raised
  `already exists`. The v0.14.3 regression test had to work around this by filtering on `name`.
- **`get()` accepts a non-string id.** It built `RecordID(table, str(id_item))`, so `get(5)` on
  a model declared `id: int` looked for the _string_ record id `"5"` and raised
  `SurrealDbNotFoundError`. It now shares the helper with `filter()`, so the two agree.
- **A text lookup on `id` fails loudly.** `contains`, `not_contains`, `containsall`,
  `containsany`, `startswith`, `endswith`, `like`, `ilike`, `match` and `regex` raise
  `ValueError` naming the column instead of returning an empty set — a record id is not a
  string. The message points at `string::contains(record::id(id), …)` in a raw query for
  callers who really want the textual form.

### Changed

- `build_filter_condition()` and `Q.to_sql()` take an optional `record_table` argument. The
  QuerySet supplies it, so `filter()`, `Q` trees and the `*_or_create` lookups all route
  through one rule. Omitted — a `Q` compiled outside a QuerySet — nothing is coerced, so no
  existing call changes meaning.

### Notes

- An aliased primary key (`model_config = {"primary_key": "code"}`) stores that alias as an
  **ordinary column** next to the RecordID `id`, so `filter(code="abc")` is a plain string
  comparison and is deliberately left untouched. Only the column literally named `id` is coerced.
- Identical behaviour on 2.6.x and 3.x; example notebook:
  `examples/v0.14.4_record_id_lookups.ipynb`.

## [0.14.3] - 2026-08-23

Correctness release: the eight findings of [issue #156](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/156)
(seven reported, an eighth found while fixing the seventh). Every one was reproduced against a
live SurrealDB **3.2.4 and 2.6.5** before a line of the fix was written, and behaves identically
on both lines afterwards. No documented behaviour was removed.

### Added

- **`Var("name")` — an explicit reference to a bound query variable in a filter**, and the
  **`"$$literal"` escape**. `filter(name="$admin")` used to compile to `name = $admin`, an
  unbound reference that matched nothing and said nothing, which made a literal starting with
  `$` impossible to express and turned any user-supplied string into an accidental variable
  reference. Write `Var("min_age")` to reference a variable and `"$$admin"` to match the
  literal `"$admin"`. The bare `"$x"` form still works and now raises a `DeprecationWarning`,
  so the default can be flipped (strings always literal) in a later minor.

### Changed

- **SurrealDB pinned to 3.2.4** (was 3.2.3) on the primary line; the CI matrix now runs
  `v2.6.5` and `v3.2.4`. Detected and validated by the new 3.x version monitor rather than by
  hand — the full suite passes against 3.2.4 on Python 3.11 → 3.14.

### Fixed

- **`first()` no longer mutates the queryset.** It set `self._limit = 1` and never restored it,
  so every later use of that queryset — including a plain re-`exec()` — silently returned at
  most one row. It now saves and restores the limit exactly as `exists()` already did: a
  terminal operation never changes the queryset it was called on.
- **`update_or_create()` / `get_or_create()` no longer write undeclared keys.** A key absent
  from the model was dropped by Pydantic on the _create_ path but written straight into the row
  by `merge()` on the _update_ path, so one call produced two different stored schemas depending
  on whether the record happened to exist. Both paths now raise `SurrealDbError` naming the
  offending keys, before any write. Models that opt in with
  `model_config = ConfigDict(extra="allow")` keep them, on both paths.
- **User-authored SurrealQL is sent verbatim.** `raw_query()` and `objects().query()` piped the
  caller's query through a helper that rewrote `'$word'` → `$word` _anywhere_ in the text,
  including inside the caller's own string literals: `WHERE name = '$admin'` became a reference
  to an unbound variable and returned nothing. The helper now runs only on ORM-compiled queries,
  where every value is bound and no user data reaches the text.
- **`upsert()` reports `created` truthfully.** It was hardcoded to `True`, so the `post_save`
  signal claimed a creation on every replacement. The statement now returns both row states
  (`UPSERT … RETURN $before AS before, $after AS after`) and `created` is read from `$before` —
  same single round-trip, on 2.6.x and 3.x alike. REPLACE semantics are unchanged.
- **A record id that looks numeric is reachable again.** `save(id="1")` stores the _string_
  record id `"1"`, but an unquoted `Model:1` is the _integer_ one in SurrealQL — so `relate()`,
  `get_related()`, `traverse()` and `remove_relation()` addressed a record the ORM had never
  written and silently traversed nothing. Such ids are now backtick-quoted (`` Model:`1` ``).
  The quoting keys off the id's **Python type**, not its textual shape, so a model with
  `id: int` keeps the integer record id `Model:5` that `save()` actually wrote. A `"table:id"`
  string passed as a relation target is quoted by the same rule, so `a.relate(edge, "M:1")`
  and `M(id="1")` name the same record; pass a `RecordID` (or the model instance) to target an
  integer record id, and an id written with backticks is kept verbatim. A backtick inside an id
  is now rejected instead of producing malformed SurrealQL.
- **`get_related(model_class=)` costs one round-trip instead of two.** It fetched record ids and
  then issued a second `SELECT * FROM $ids`; the records are now projected in the traversal
  itself (`->edge->?.*`). Same results, verified on both lines.
- **`raw_query()` and `objects().query()` warn on a multi-statement query.** The SDK returns
  only the first statement's rows and says nothing about the rest, so half the output vanished
  silently. Detection is quote- and comment-aware (`--`, `//`, `#` and `/* … */`), so a `;`
  inside a string literal or a comment is not mistaken for a separator, and a trailing comment
  does not count as a statement of its own.
- **The `"$$literal"` escape round-trips through `get_or_create()` / `update_or_create()`.** The
  escape was decoded only when compiling the `WHERE` clause, so `get_or_create(name="$$admin")`
  looked for `"$admin"` but stored `"$$admin"` — it never converged and created a duplicate row
  on every call. The create payload now reads values exactly as the lookup does. A query-variable
  reference (`Var(...)` or `"$x"`) as an equality criterion raises `SurrealDbError` instead of a
  Pydantic error: its value lives on the server and cannot be written back — pass the literal in
  `defaults=`.
- **Warnings point at the caller.** The `DeprecationWarning` on `"$x"` filter values and the
  multi-statement warning reported a frame inside the ORM, and Python's default filters only
  surface a `DeprecationWarning` raised from `__main__` — so the deprecation runway reached
  nobody. The reported frame is now the first one outside the package.

- **Dependabot PRs can be tested again.** The readiness probe piped SurrealDB's `/health` into
  `grep -q "OK"`, but that endpoint answers `200` with an _empty body_ — the match could never
  succeed, so every probe ran its full retry budget and then declared a server that had been up
  for a minute unhealthy. Nothing acted on the verdict, so it stayed invisible: `ci.yml` merely
  `break`-ed and ran the suite anyway (passing, against the server it had just given up on), at a
  cost of ~30s on each of the eight matrix jobs. #134 then wrapped the same loop in `start_one`,
  which _returns 1_ on timeout — turning the latent bug into a hard failure on the next Dependabot
  PR (#150) and stalling the auto-merge chain. Both probes now key on the status code (`curl -sf`,
  matching `devops/wait-for-healthy.sh` in the full ORM), dump `docker logs` when a server really
  does not come up, and fail the step instead of falling through. Guarded by
  `tests/test_workflow_health_probe.py`.
- **The SurrealDB version monitors work again, and now cover both lines.** The 2.x monitor had
  failed on every scheduled run for seven weeks: it read the primary pin, compared it to the
  latest 2.x release, and so proposed a downgrade every day — wedging permanently on the
  branch left behind by the first attempt. It now reads `.surrealdb-version2x`, compares
  version-aware, refuses to emit a downgrade, and recovers from a stale branch.
- **Security patches can actually be released.** The ORM version bump rode in the same PR as
  the SurrealDB update, so the squash landed under a `security(deps):` title and `auto-tag`
  never fired — the bump reached `main` and was never published. It is now a separate
  `chore(release):` PR, as the Dependabot flow already did.
- **The 3.x line is monitored at all.** Only the legacy 2.x line was watched, so 3.2.1 through
  3.2.4 shipped unnoticed. The new monitor resolves from Docker Hub, because SurrealDB
  publishes 3.x patches as images without a matching GitHub release (the releases API stops at
  v3.2.0 while Docker Hub carries v3.2.4).

## [0.14.0] - 2026-07-26

### Added

- **Computed fields — `Computed[...]` → `DEFINE FIELD … VALUE`**: declare a field SurrealDB
  derives from other fields on **every** write. Where `SurrealFunc` (v0.13.0) evaluates an
  expression for one write, a computed field attaches it to the schema, so it applies to every
  write on the table — including ones the ORM never sees. `Computed[T]` is the annotation and
  `computed("<expr>")` the default; splitting them keeps the field statically typed as
  `T | None` under mypy and pyright.

  ```python
  from surreal_orm_lite import BaseSurrealModel, Computed, computed

  class Player(BaseSurrealModel):
      id: str
      first_name: str
      last_name: str
      full_name: Computed[str] = computed("string::concat(first_name, ' ', last_name)")

  await Player.define_computed_fields()
  player = await Player(id="ada", first_name="Ada", last_name="Lovelace").save()
  player.full_name  # "Ada Lovelace" — computed by the server
  ```

- `Model.get_computed_fields()` — `{field: expression}` for the model, inherited fields included.
- `Model.computed_field_ddl(overwrite=True)` — render the `DEFINE FIELD` statements without
  touching the database (printable, diffable, pipeable into a migration).
- `Model.define_computed_fields(overwrite=True, tx=None)` — apply them; idempotent, so it is
  safe to call at application start-up.

### Changed

- Computed fields are excluded from every write payload (`save`, `upsert`, `update`, `merge`,
  `bulk_create`) — the server owns the value.
- Naming a computed field in `merge()`, `save(server_values=)`, `patch()`, `QuerySet.patch()`,
  `QuerySet.bulk_update()` or any `atomic_*` helper now raises `ValueError` instead of being
  silently discarded by the server. JSON Patch documents are checked on the top-level pointer
  segment, and on `from` as well for `move`, which removes its source.
- **Supported SurrealDB lines are now 2.6.x and 3.2.x** — 3.1.x is dropped. The CI matrix runs
  `v2.6.5` and `v3.2.3` (was `v2.6.5` + `v3.1.3`). 3.2 is covered directly rather than assumed
  equivalent to 3.1, because SurrealDB 3.2.0 regressed uncorrelated sub-SELECTs combining
  `ORDER BY` and `LIMIT` ([SurrealDB-ORM#147](https://github.com/EulogySnowfall/SurrealDB-ORM/issues/147));
  ORM-lite never emits that shape. The suite is still run against 3.1.5 locally as a
  backward-compatibility check (`RETRO=1`), but a regression there no longer blocks a release.
- **One version pin per supported line.** `.surrealdb-version` now pins the primary (latest)
  line — `3.2.3` — and the new `.surrealdb-version2x` pins the legacy 2.x line (`2.6.5`). The
  single file could not serve both consumers: the 2.x version monitor reads and writes it (and
  saw a permanent phantom update while it held a 3.x value), while the Dependabot gate uses it
  to pick a test container. Dependabot now tests **both** lines, and the monitor's `sed` on the
  CI matrix targets only the `v2.*` entry — it assumed a single-element list and had silently
  matched nothing since the matrix gained a second line.
- **`save()` with an explicit id now reads back the model's computed fields** from the row the
  server returned; it previously discarded that row entirely, so a computed value never reached
  the instance. The sync is deliberately limited to computed fields — a model that declares none
  behaves exactly as it did in v0.13.0.

### Notes

- **Identical on SurrealDB 2.6.x and 3.x** — verified on 2.6.5 and 3.2.3 (and on 3.1.5 as a
  backward-compatibility check). No 3.x-only primitive and no capability gate, so no test in
  this feature skips on any line. The DDL syntax, the recompute triggers, the precedence over
  client-sent data and the rollback of DDL in a cancelled transaction all matched exactly. The only divergence is the raw SDK exception for an invalid expression
  (`InternalError` on 2.6.x, `ValidationError` on 3.x); both are normalised to `SurrealDbError`.
- SurrealDB evaluates computed fields in **alphabetical field-name order**, not declaration
  order: a computed field reading another must sort after it (`subtotal` → `total` works,
  `z_sub` → `a_total` fails at write time).
- A computed field is a **server-enforced** invariant: a client that sends its own value for one
  cannot override the expression, even bypassing the ORM entirely.
- No `TYPE` clause is emitted — SurrealDB infers an optional type. Explicit field types remain a
  later roadmap item.
- Expressions are inlined into DDL and cannot reference bound parameters. Build them only from
  developer-controlled text — the `SurrealFunc` trust model, whose validation they reuse.
- **`tests/typing_examples.py`** is type-checked in CI as its own mypy invocation, so the public
  API is verified to type-check from a _user's_ side — `mypy src/` alone cannot catch that, since
  no model in `src/` declares a computed field.

## [0.13.0] - 2026-07-26

### Added

- **`SurrealFunc`** — a marker wrapping a raw SurrealQL expression so a value is computed
  **by the server** instead of in Python, plus `SurrealFunc.call(fn, *args)` to build a call
  from a function name and raw argument fragments.

  ```python
  from surreal_orm_lite import SurrealFunc, SurrealTimeFunction

  SurrealFunc("time::now()")
  SurrealFunc.call(SurrealTimeFunction.NOW)  # → SurrealFunc("time::now()")
  ```

- **`server_values=` / `extra_vars=` on `save()` and `merge()`** — `server_values` maps field
  names to `SurrealFunc`s (evaluated server-side); `extra_vars` binds the query variables those
  expressions reference, so user input stays a parameter and never touches the query text.
  The write is compiled to `CREATE $rid SET …` / `UPDATE $rid SET …` and the returned row syncs
  the instance, so a server-computed value is readable right after the call.

  ```python
  await player.save(server_values={"joined_at": SurrealFunc("time::now()")})

  await user.save(
      server_values={"password_hash": SurrealFunc("crypto::argon2::generate($password)")},
      extra_vars={"password": raw_password},
  )

  # merge() stays a partial update; a server value overrides a same-named keyword
  await user.merge(plan="pro", server_values={"updated_at": SurrealFunc("time::now()")})
  ```

- **Curated function-name enums** — `SurrealTimeFunction`, `SurrealMathFunction`,
  `SurrealStringFunction`, `SurrealArrayFunction`, `SurrealCryptoFunction`,
  `SurrealRandFunction` (and the `SurrealFunction` `StrEnum` base to define your own).
  **Every catalogued member is executed against both SurrealDB 2.6.5 and 3.1.3 by the test
  suite**, so autocompletion only offers names that work on both lines.

- **`build_set_clause()` / `merge_extra_vars()`** in `utils.py` — the pure compilers behind the
  feature (functions inlined, all other values bound as `$_sv_<field>`, `extra_vars` collision
  detection), unit-testable without a database.

### Notes

- **Identical on SurrealDB 2.6.x and 3.x — by design.** The feature compiles to portable
  SurrealQL and uses only functions verified on both lines. Names that diverge between the
  lines are deliberately excluded from the enums (`rand::guid` is 2.6-only; `type::is::*` was
  renamed `type::is_*` in 3.x) — pass those as a plain string if you target a single line.
- **`CONTENT $data SET …` is not valid SurrealQL** (verified on both 2.6.5 and 3.1.3), so a
  save carrying `server_values` compiles the _whole_ write as one `SET` clause rather than
  mixing the native SDK data path with an expression.
- **Security**: the `SurrealFunc` expression is inlined verbatim — build it only from
  developer-controlled text. Field values and `extra_vars` are always bound parameters (the
  injection boundary); `server_values` keys are validated as identifiers. `SurrealFunc` rejects
  `;` as a guard against accidental statement chaining, which is not a sanitizer.
- **In a transaction**: on the interactive strategy (WebSocket + 3.x) the returned row is
  applied immediately, so the instance sees the computed value inside the block. On a buffered
  transaction (HTTP / 2.6.x) the statement runs at commit, so the instance keeps its previous
  value for computed fields until you `refresh()` — the same caveat as `merge(tx=)`. Auto-id
  `save(tx=)` still requires the interactive strategy (unchanged v0.8.0 rule).
- **`merge(server_values=)` on a record that does not exist raises `SurrealDbError`** ("Can't
  merge data, no record found."), matching the native `merge()` path — server-side an `UPDATE`
  matching nothing is not an error, and the two DB lines even disagree on the missing-table case
  (SurrealDB 3.x raises `NotFound`, 2.6.x returns no rows), so both are normalised to that one
  ORM error. Inside an interactive transaction the statement runs at `merge()` time, so the same
  error is raised there (and rolls the transaction back); a buffered transaction runs nothing
  before commit, so the miss is not knowable and the statement simply matches no record.
- Signals are unchanged: `save(server_values=)` emits `pre_save`/`around_save`/`post_save`, and
  `merge(server_values=)` emits `pre_update`/`around_update`/`post_update` with `update_fields`
  listing both the literal and the computed fields.
- `server_values` and `extra_vars` join `tx` as reserved keyword names on `merge()`.

## [0.12.0] - 2026-06-09

### Added

- **`retry_on_conflict(...)`** — an async decorator that re-runs a function on a retryable
  SurrealDB transaction conflict, with exponential backoff + jitter. Retries **only** real
  conflicts; any other exception propagates immediately. Total attempts = `max_retries + 1`;
  after exhaustion the conflict is re-raised. Mirrors the full ORM's signature.

  ```python
  from surreal_orm_lite import retry_on_conflict, SurrealDBConnectionManager

  @retry_on_conflict(max_retries=3, base_delay=0.05, max_delay=2.0, backoff_factor=2.0)
  async def transfer(src_id, dst_id, amount):
      async with SurrealDBConnectionManager.transaction() as tx:
          src = await Account.objects(tx=tx).get(src_id)
          dst = await Account.objects(tx=tx).get(dst_id)
          await src.merge(tx=tx, balance=src.balance - amount)
          await dst.merge(tx=tx, balance=dst.balance + amount)
  ```

- **`SurrealDbConflictError`** — a `SurrealDbError` subclass representing a retryable
  transaction conflict. A conflict now surfaces as this single type on **both** transaction
  strategies (buffered and interactive), so it can be caught uniformly.

- **`is_conflict_error(exc)`** — the public predicate `retry_on_conflict` uses; returns `True`
  for a retryable conflict (walking the exception's `__cause__` / `__context__` chain),
  exported for callers who want their own retry loop.

### Changed

- **`Transaction.raise_for_status`** now surfaces the real root cause of a failed transaction
  instead of the generic "The query was not executed due to a failed transaction" filler (on
  SurrealDB 3.x every rolled-back statement is tagged `NotExecuted`, so the real cause is a
  sibling statement), and raises `SurrealDbConflictError` when that cause is a retryable
  conflict. The pre-existing `already exists` / generic-rollback behaviour is unchanged.

### Notes

- **Conflict detection is anchored on SurrealDB's own retryable marker** — the phrase
  "This transaction can be retried" (present on both 2.6.5 and 3.1.3). It is deliberately
  **narrower** than the full ORM (which also matches `"failed transaction"` / bare `"conflict"`):
  those over-match the non-retryable "not executed due to a failed transaction" filler and would
  cause a duplicate-key failure inside a transaction to be retried pointlessly.
- **Same exception type on both DB lines, different conflict frequency.** A conflict is a
  `SurrealDbConflictError` on 2.6.x and 3.x alike. On 3.x (optimistic MVCC) conflicts are the
  normal failure mode; on 2.6.x the engine serialises more, so conflicts are rarer (reproducible
  under high concurrency). Verified empirically on 2.6.5 and 3.1.3.
- `retry_on_conflict` decorates **async** functions only. The `jitter` flag (default `True`,
  set `False` for deterministic tests) is the only addition to the full ORM's signature.

## [0.11.0] - 2026-06-07

### Added

- **`model.patch()`** — apply a JSON Patch (RFC 6902) to a single record, backed by the SDK's
  native `patch()` (`UPDATE rid PATCH $data`). Requires an explicit id; supports `tx=`.

  ```python
  await user.patch([
      {"op": "replace", "path": "/age", "value": 26},
      {"op": "add", "path": "/tags/-", "value": "premium"},
      {"op": "remove", "path": "/settings/notifications"},
  ])
  ```

- **Atomic field/array helpers** on the model instance — each compiled to one atomic
  server-side `UPDATE … SET`, so concurrent callers don't clobber each other. All support `tx=`:

  ```python
  await post.atomic_append("tags", "python")    # array::append — duplicates allowed
  await post.atomic_set_add("editors", "alice")  # array::add     — no duplicate (set)
  await post.atomic_remove("tags", "spam")       # array::complement — removes ALL "spam"
  await counter.atomic_increment("views")        # += 1 (default); negative to decrement
  ```

- **List-valued atomic helpers** — `atomic_append_many` / `atomic_set_add_many` /
  `atomic_remove_many` apply many values in one round-trip (compiled to `array::concat` /
  `array::add` / `array::complement`; an empty list is a safe no-op). All support `tx=`.

  ```python
  await post.atomic_append_many("tags", ["python", "orm"])
  await post.atomic_set_add_many("editors", ["alice", "bob"])
  await post.atomic_remove_many("tags", ["spam", "draft"])
  ```

- **`QuerySet.patch()`** — apply a JSON Patch to every row matching the filters (whole table if
  unfiltered); returns the affected count. Participates in `objects(tx=)`.

  ```python
  n = await User.objects().filter(status="trial").patch(
      [{"op": "replace", "path": "/plan", "value": "free"}]
  )
  ```

- **JSON Patch / JSON Pointer validators** in `utils.py` (`validate_patch_operations`,
  `validate_json_pointer`) for fast, clear errors.

### Notes

- **Identical on SurrealDB 2.6.x and 3.x — by design.** The atomic helpers deliberately use the
  version-portable SurrealQL **functions** `array::append` / `array::add` / `array::complement`
  (and numeric `+=`) rather than the bare `+=` / `-=` array operators, whose semantics diverge
  between server lines (`arr -= v` removes _all_ occurrences on 3.x but only the _first_ on
  2.6.x; array `+=` appends _with_ duplicates on both, so it is not set-add). Verified
  empirically on 2.6.5 and 3.1.3.
- `patch()` and the atomic helpers emit **no signals** (unlike `merge`/`save`) — they are
  low-level atomic primitives. Use `merge()` / `save()` when you need lifecycle hooks.
- Self-sync: non-transactional and interactive (3.x) calls apply the server's returned row to
  the instance; in a **buffered** transaction (HTTP / 2.6.x) the result is unknown until commit,
  so the instance is left stale — `refresh()` it if needed (same caveat as `merge(tx=)`).
- Operations are bound as query data (never string-interpolated); atomic-op field names are
  validated. JSON Patch validation is for clear errors, not the injection boundary.
- `atomic_increment` accepts a `decimal.Decimal` amount for exact arithmetic (e.g. money);
  adding a `Decimal` to an int/float field coerces the stored field to SurrealDB `decimal`.
- A failed JSON Patch `test` op aborts the **whole** patch server-side (no op applies) and
  raises the SDK's `ServerError` — RFC 6902 compare-and-set / optimistic concurrency without a
  transaction. Identical on 2.6.x and 3.x.

## [0.10.0] - 2026-06-07

### Added

- **`model.upsert()`** — insert-or-replace by explicit id, backed by the SDK's native
  `upsert()` (`UPSERT $record CONTENT $data`, full REPLACE). Supports `tx=` (buffered onto
  the transaction).

  ```python
  user = User(id="alice", name="Alice", status="active")
  await user.upsert()  # CREATE if absent, full REPLACE if present
  ```

- **`QuerySet.update_or_create()` / `QuerySet.get_or_create()`** — Django-style
  criteria-based create-or-update returning `(instance, created)`. Writes route through
  `save()` / `merge()`, so lifecycle signals fire, SDK errors normalise to `SurrealDbError`,
  and the primary key anchors record identity. `update_or_create` does a **partial merge** on
  update — fields outside the criteria/defaults are preserved — while `get_or_create` returns
  an existing match untouched. Non-`exact` lookups (e.g. `name__contains`) drive the lookup
  but are not written. A SELECT-guard raises an explicit `SurrealDbError` when the criteria
  match more than one record.

  ```python
  user, created = await User.objects().update_or_create(
      email="alice@example.com", defaults={"name": "Alice"}
  )
  user, created = await User.objects().get_or_create(
      email="bob@example.com", defaults={"name": "Bob"}
  )
  ```

### Notes

- Behaviour is **identical on SurrealDB 2.6.x and 3.x** (no 3.x-only primitive): the native
  `upsert()` call and the SELECT-guard work the same on both lines (verified by spike).
- `upsert()` is REPLACE, not merge: fields omitted from the model are dropped. Use `merge()`
  for partial updates.
- `update_or_create` / `get_or_create` do the lookup and the write in two round-trips
  (SurrealDB 2.6.x exposes no row-locking primitive here), so a small race window exists
  under concurrent writers — the same non-atomic fallback Django documents.
- Under `objects(tx=)`, `update_or_create` / `get_or_create` participate in the transaction on
  SurrealDB 3.x (interactive); a buffered 2.6.x transaction raises on their lookup read
  (reads inside a buffered transaction are unsupported — consistent with other QuerySet reads).

## [0.9.0] - 2026-06-07

### Added

- **Transactions ORM — QuerySet & interactive strategy**: `objects(tx=)` plumbing routes
  `QuerySet` reads (`get`, `first`, `all`, `exec`, `count`, `exists`, aggregations) and bulk
  operations (`bulk_create`, `bulk_update`, `bulk_delete`) through the transaction.
  Example:

  ```python
  async with SurrealDBConnectionManager.transaction() as tx:
      users = await User.objects(tx=tx).filter(status="active").exec()
      await User.objects(tx=tx).filter(role="guest").bulk_update(role="member")
  ```

- **Two transaction strategies** (auto-selected by `transaction()` based on URL + server):
  - `InteractiveTransaction` (WebSocket + SurrealDB 3.x): uses the SDK's native
    `begin()`/`commit()`/`cancel()` API tagged by `txn_id`. Reads inside the transaction
    see uncommitted writes; `save(tx=)` now supports **auto-generated ids** and `refresh(tx=)`
    works.
  - `BufferedTransaction` (HTTP, or WebSocket on SurrealDB 2.6.x): preserves the v0.8.0
    `BEGIN…COMMIT` batching semantics. Reads inside the transaction still raise.

- Exported `BufferedTransaction` and `InteractiveTransaction` (in addition to the `Transaction`
  base type) for typing and inspection.

### Changed

- `transaction()` now **cancels** on a commit failure (e.g. server-side rollback surfacing at
  `COMMIT`), so an interactive transaction never leaves the shared connection half-open.

### Fixed

- `QuerySet.get(id)` inside an interactive transaction now normalises the SDK's
  `NotFoundError` to `SurrealDbNotFoundError`, matching the non-tx path.

### Notes

- `bulk_update()`/`bulk_delete()` return `0` on the buffered strategy (the affected-row
  count is not knowable before commit). On the interactive strategy they return the real
  count.

## [0.8.0] - 2026-06-06

### Added

- **Transactions ORM (core)**: `async with SurrealDBConnectionManager.transaction() as tx:`
  context manager plus a `tx=` parameter on `save()`, `update()`, `merge()`, and `delete()`.
  Operations are buffered and committed atomically as a single
  `BEGIN TRANSACTION; … COMMIT TRANSACTION;` query; an exception in the block rolls
  everything back.
- Exported `Transaction`.

### Notes

- Inside a transaction, `save()` requires an explicit record id (auto-generated ids are not
  buffered in this release). Reads/`refresh()` inside a transaction raise a clear error and
  are planned for v0.9.0 (QuerySet `objects(tx=)` + savepoints).

## [0.7.0] - 2026-06-06

### Added

- **`BaseSurrealModel.get_raw_id()` helper**: Returns the bare identifier string (e.g. `"alice"`) extracted from the native `RecordID` stored in `model.id`. Use this when you need a plain string instead of a `RecordID` object.

  ```python
  user = await User.objects().get("alice")
  user.get_raw_id()  # → "alice"
  user.id            # → RecordID("User", "alice")
  ```

- **CI matrix expanded**: Integration tests now run against SurrealDB **v2.6.5** and **v3.1.3** across Python 3.11–3.14.

### Changed

- **BREAKING — Dependency updated**: Replaced `surrealdb>=1.0.8` with `surrealdb[pydantic]>=2.0.0,<3.0.0` (SurrealDB 3.x protocol). Upgrade your environment with `pip install "surrealdb[pydantic]>=2.0.0,<3.0.0"`.
- **BREAKING — Native `RecordID` on loaded records**: A record fetched from the database now has its `id` field set to a native `surrealdb.RecordID` object (e.g. `RecordID("User", "alice")`) instead of a bare string. Update comparisons to use `model.get_raw_id() == "alice"` or `model.id == RecordID("User", "alice")`. In-memory instances you construct yourself retain whatever value you pass in.
- **Structured errors from SDK**: SurrealDB SDK 2.x raises structured exceptions (e.g. `AlreadyExistsError` on 3.x, `InternalError` on 2.x) instead of returning error strings. The ORM converts the "already exists" case to `SurrealDbError` automatically.
- **Not-found-tolerant cleanup operations**: `QuerySet.delete_table()`, `BaseSurrealModel.remove_relation()`, and `remove_all_relations()` are now silent no-ops when the target does not exist (consistent with SurrealDB 2.x behaviour; SurrealDB 3.x would otherwise raise). Note: `record.delete()` on a missing record still raises `SurrealDbError`.
- **Aggregation edge-case normalisation**: `avg()`/`min()`/`max()` over an empty set still return `0.0`/`None`/`None`; SurrealDB 3.x `NaN`/`+inf`/`-inf` responses are normalised to these values.
- **Connection sign-in ordering**: The connection manager now signs in before selecting the namespace/database to comply with SurrealDB 3.x strictness (3.x no longer auto-creates the namespace on a pre-auth `use()`).

### Fixed

- **Reads on never-created tables**: SurrealDB 3.x raises `NotFoundError` ("table does not exist") for a `SELECT`/`select` on a table that was never written to, instead of returning an empty result set as SDK 1.x did. All read paths now honour the ORM's "missing table = empty" contract: `get()`/`first()` raise `SurrealDbNotFoundError` ("No result found."), `count()` returns `0`, `exists()` returns `False`, and `all()`/`exec()`/`query()` return `[]`.

## [0.6.0] - 2026-03-10

### Added

- **Relations & Graph**: Full SurrealDB graph relation support on `BaseSurrealModel`
  - `relate(edge, target, data=)` — Create graph relations (`RELATE source->edge->target`)
  - `remove_relation(edge, target)` — Remove a specific relation
  - `remove_all_relations(edge, direction=)` — Remove all relations of a type (`out`, `in`, `both`)
  - `get_related(edge, direction=, model_class=)` — Retrieve related records through an edge
  - `traverse(path)` — Graph traversal via SurrealQL path syntax (e.g. `->follows->User->follows->User`)

- **FETCH Clause**: `QuerySet.fetch(*fields)` resolves record links inline, preventing N+1 queries
  - `Post.objects().fetch("author", "tags").exec()` generates `SELECT * FROM Post FETCH author, tags;`

- **Validation Utilities**: New security validators in `utils.py`
  - `validate_edge_name()` — Validates edge/relation table names
  - `validate_graph_path()` — Validates graph traversal paths (strict arrow-segment structure)
  - `validate_thing()` — Validates `table:id` record identifiers against injection

- New test file `tests/test_relations.py` with unit + E2E tests

- **CI/CD Workflows**:
  - `.surrealdb-version` file to pin the tested SurrealDB version (2.6.0)
  - `surrealdb-security.yml` — Daily SurrealDB 2.X version monitor (test, auto-PR, auto-issue)
  - `dependabot-automerge.yml` — Auto-merge Dependabot PRs with test validation and version bump

### Changed

- `QuerySet._compile_query()` now appends `FETCH` clause when `fetch()` is used
- `QuerySet.fetch()` now accumulates fields across chained calls instead of overwriting
- `QuerySet.variables()` now merges variables across chained calls instead of overwriting
- Coverage: 92.80%

### Fixed

- **Security**: `_resolve_target_thing()` now validates string targets with `validate_thing()` to prevent SurrealQL injection
- **Security**: `_get_thing()` now validates the generated `table:id` string to prevent injection via malicious model IDs
- **Security**: `get_related()` now validates RecordIDs with `validate_thing()` before interpolation
- **Security**: `$` variable references in filters are now validated against `^\$[a-zA-Z_][a-zA-Z0-9_]*$` pattern
- **Security**: `validate_graph_path()` regex tightened to require arrow-separated segments (`->` or `<-`) — rejects arbitrary `<>-` combinations
- Removed misleading case-insensitive lookup aliases (`icontains`, `istartswith`, `iendswith`, `iregex`) that were mapped to case-sensitive SurrealDB operators

## [0.5.0] - 2026-02-11

### Added

- **Q Objects**: Django-style composable query expressions for complex filters
  - `Q(field=value)` for basic conditions
  - `Q(...) | Q(...)` for OR queries
  - `Q(...) & Q(...)` for AND queries
  - `~Q(...)` for NOT queries
  - Nested combinations: `Q(age__gte=18) & (Q(role="admin") | Q(role="mod"))`

- **Parameterized Filters**: All filter values now use parameterized variables (`$_fN`) instead of string interpolation, preventing SQL injection

- **New Lookup Operators**:
  - `not_in` - NOT IN operator
  - `not_contains` - CONTAINSNOT operator
  - `containsall` - CONTAINSALL operator
  - `containsany` - CONTAINSANY operator

- **`-field` Ordering Shorthand**: Prefix field with `-` for descending order
  - `order_by("-created_at")` instead of `order_by("created_at", OrderBy.DESC)`
  - Multi-field ordering: `order_by("-age", "name")`

- **Bulk Operations**:
  - `bulk_create(models)` - Create multiple records via SDK's `insert()`
  - `bulk_update(**kwargs)` - Update all matching records with parameterized SET clause
  - `bulk_delete()` - Delete all matching records, returns count

- New `Q` class exported from `surreal_orm_lite`
- New test file `tests/test_v050.py` for all v0.5.0 features

### Changed

- `_compile_query()`, `_compile_aggregation_query()`, `_compile_group_by_query()` now return `tuple[str, dict]` with parameterized variables
- `_execute_query()` now accepts optional `variables` parameter
- `filter()` now accepts `*args: Q` positional arguments alongside keyword filters
- `order_by()` now accepts multiple fields with `-field` prefix support
- `_build_where()` replaces `_build_where_clauses()` and returns parameterized WHERE clause
- Shared `parse_lookup()` and `build_filter_condition()` functions moved to `utils.py`
- Fixed `isnull` lookup: now correctly generates `IS NULL`/`IS NOT NULL`

## [0.4.0] - 2026-02-07

### Added

- **Model Signals**: Django-style event system for model lifecycle
  - `Signal` class for pre/post event handlers
  - `AroundSignal` class for context manager-style wrapping signals with `yield`
  - `pre_save` / `post_save` - Fired before/after `save()` operations
  - `pre_update` / `post_update` - Fired before/after `update()` and `merge()` operations
  - `pre_delete` / `post_delete` - Fired before/after `delete()` operations
  - `around_save` / `around_update` / `around_delete` - Wrap operations for timing, logging, etc.
  - `connect(model_class)` decorator for registering handlers
  - `disconnect(handler, model_class)` for removing handlers
  - `clear()` for removing all handlers
  - `has_handlers()` for checking if handlers are registered

- `post_save` signal includes `created` flag to distinguish new records
- `pre_update` / `post_update` signals include `update_fields` list
- New test file `tests/test_signals.py` with unit and e2e tests

### Changed

- `save()`, `update()`, `merge()`, `delete()` now emit signals when handlers are registered
- Internal `_do_save()` method extracted from `save()` for signal integration

## [0.3.0] - 2026-02-05

### Added

- **Aggregation Functions**: New aggregation classes for database calculations
  - `Count()` - Count records
  - `Sum(field)` - Sum numeric field values
  - `Avg(field)` - Calculate average of numeric field
  - `Min(field)` - Find minimum value
  - `Max(field)` - Find maximum value

- **QuerySet Aggregation Methods**: Shortcut methods for common aggregations
  - `count()` - Returns count as integer directly
  - `sum(field)` - Returns sum as float/int
  - `avg(field)` - Returns average as float
  - `min(field)` - Returns minimum value
  - `max(field)` - Returns maximum value

- **GROUP BY Support**: Django-style grouping with annotations
  - `values(*fields)` - Specify fields for GROUP BY
  - `annotate(**aggregations)` - Add aggregation annotations

- **exists() Method**: Efficiently check if records exist

- **raw_query() Class Method**: Execute arbitrary SurrealQL queries with variables

- New test file `tests/test_aggregations.py` with comprehensive unit and e2e tests

### Changed

- QuerySet now tracks `_group_by_fields` and `_annotations` for GROUP BY queries
- `exec()` method now handles GROUP BY queries differently, returning dicts instead of model instances

## [0.2.2] - 2026-02-05

### Fixed

- Corrected project URLs in package metadata (case sensitivity)

## [0.2.1] - 2026-02-04

### Fixed

- Fixed `refresh()` method not updating the model instance
- Fixed `refresh()` not handling SDK 1.0.8 list responses
- Fixed `save()` not updating instance with auto-generated ID
- Fixed SQL query order: `ORDER BY` now comes before `LIMIT/START` (SurrealDB requirement)
- Fixed `set_data` validator not always returning data
- Fixed typo in error message: `primirary_key` → `primary_key`
- Removed ineffective `del self` in `delete()` method
- Fixed dead test `failed_model_validation` that was never executed

### Changed

- `SurrealDbNotFoundError` now inherits from `SurrealDbError` for backward compatibility
- Test database connection now configurable via `SURREALDB_HOST` and `SURREALDB_PORT` environment variables
- Improved docstrings for `unset_connection()` and `is_password_set()`
- Translated French comments to English in `connection_manager.py`
- Renamed variable `test` → `result` in `update()` method

### Added

- Added `codecov.yml` configuration (disabled patch coverage check)
- Added test for `save()` with auto-generated ID
- Added test for `contains` lookup operator

## [0.2.0] - 2026-02-02

### Added

- Initial release of **Surreal ORM Lite**
- Support for official SurrealDB Python SDK 1.0.8
- Compatibility with SurrealDB 2.6.0
- Django-style ORM with `BaseSurrealModel` base class
- `QuerySet` with fluent query builder
- Filter lookups: `exact`, `gt`, `gte`, `lt`, `lte`, `in`, `contains`, `startswith`, etc.
- CRUD operations: `save()`, `update()`, `merge()`, `delete()`, `refresh()`
- `SurrealDBConnectionManager` for connection management
- HTTP and WebSocket connection support
- Custom primary key configuration via `SurrealConfigDict`
- Pydantic 2.x validation support
- Custom exceptions: `SurrealDbError`, `SurrealDbConnectionError`, etc.
- Full async/await support
- 97%+ test coverage

### Changed

- Migrated from custom SDK to official SurrealDB SDK 1.0.8
- Updated API to match SDK 1.0.8 response formats
- `signin()` now uses dictionary format: `{"username": ..., "password": ...}`

### Fixed

- Handle SDK 1.0.8 returning lists for single record `select()`
- Handle SDK 1.0.8 returning error strings instead of exceptions
- Escape special characters in record IDs (e.g., `@` in email addresses)

## [0.1.x] - Previous Versions

Previous versions were part of the SurrealDB-ORM project with a custom SDK implementation. This project (Surreal ORM Lite) is a fork focused on using the official SDK only.
