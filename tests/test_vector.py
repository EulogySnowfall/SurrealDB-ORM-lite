"""Vector search (v0.36.0): the full SurrealDB-ORM's ``VectorField`` and its typed form, the ``Vector`` marker.

The field type (``TestVectorField``): ``VectorField[d]`` / ``VectorField[d, type]`` — the full ORM's spelling — and
``Annotated[list[float] | None, Vector(d)] = None`` — the typed form, which type checkers accept — are one field:
optional, ``None`` by default, a ``list`` or ``tuple`` of exactly ``d`` real numbers (``int``, ``float``, any
``numbers.Real``, a ``Decimal``; never a ``bool`` nor a ``str``), each finite, stored as a ``list[float]``. The value
is checked on a write and on a read (``from_db()``, what makes ``exec()`` return a row that does not validate as a
dict), each refusal a ``ValidationError`` carrying its exact text; ``model_dump()`` gives the list, in JSON mode too,
and ``model_json_schema()`` a fixed-length array of numbers, or null, for both spellings and an aliased field.
``VectorField[...]`` checks its parameters where the class is defined (the shape, the dimension 1 to 65535, the
storage type ``F64``, ``F32``, ``I64``, ``I32`` or ``I16`` in any case) and is never instantiated. ``VectorField`` and
``Vector`` are exported from the package root and from ``surreal_orm_lite.vector``, which imports no model, queryset,
search or connection module.

The marker (``TestVectorMarker``): ``Vector(dimension, vector_type="F32")`` holds the dimension and the storage type
upper case, equal and hashed by both, with the ``repr`` ``Vector(3, 'F32')``, read-only, copied and pickled as an
equal marker; its arguments are checked with exact texts. ``get_vector_info()`` reads ``(dimension, type)`` from a
model field of either spelling or from an ``Annotated`` annotation, ``None`` from anything else;
``is_vector_field()`` tells which. Both read a union of either spelling with ``None`` (``VectorField[3] | None``,
``Optional[VectorField[3]]``, ``Annotated[list[float], Vector(3)] | None``: issue #318) as the plain spelling, never
the vectors a container holds (``list[VectorField[3]]``), and refuse with an exact text a field or an annotation holding
two different markers, or a marker next to a type other than ``None``. ``VectorField[3] | None`` needs its ``= None``
(Pydantic ignores the default of a union member, with a warning), and checks its values as ``VectorField[3]`` does.

The checks of the vector search's arguments (``TestCheckers``): the distances (``EUCLIDEAN``, ``COSINE``,
``MANHATTAN``, ``CHEBYSHEV``, ``HAMMING`` in any case; ``MINKOWSKI``, ``JACCARD`` and ``PEARSON`` each refused with its
reason), the storage types, the bounded integers, the query vector (converted to a ``list[float]``) and the ``limit``
and ``ef`` of ``similar_to()``, 1 to 10000 — each refusal with its exact text and class. These are pure: no client is
involved, no request is sent.

The renderers (``TestRenderers``): ``hnsw_clause()`` writes every HNSW option of an index; ``knn_condition()`` writes
the KNN operator with an ``EF`` of at least ``K`` (100 by default), or the distance of an exact search; both write the
ORM's own constants and checked integers only.

The vector index (``TestIndexVector``): ``Index(..., hnsw=True, dimension=, dist=, vector_type=, efc=, m=)`` — the full
ORM's ``CreateIndex`` arguments — holds what was declared (``None`` for an option not given, the words upper case),
is equal, hashed and shown by those declared attributes, and keeps v0.35.0's seven positional match arguments; its
statement writes every HNSW option, SurrealDB's documented default for each one not declared, the same on both lines;
``index_matches()`` compares those effective options with what ``INFO FOR TABLE`` renders, so a bare declaration and
one spelling out the defaults both match the same stored index, and any other dimension, metric, storage type,
``EFC``, ``M``, comment, field or index kind does not. Every argument is checked when the index is built, each refusal
with its exact text, and no out-of-range option is ever applied. ``TestIndexVectorE2E`` applies the statements on the
line under test over WebSocket and HTTP and on the embedded engine: what ``INFO FOR TABLE`` returns matches each
declaration, and the introspector reads the options the ORM wrote.

The model's vector indexes (``TestClassChecks``): at the ``class`` statement, after v0.35.0's checks, two vector
indexes on one column (an aliased field counted under its column) are refused, then a vector index on a vector field
whose dimension or storage type differs from the field's — the index's effective type compared, ``F32`` when it
declares none —, each with its exact text, the first fault raising; a vector index on a field that is not a vector
field is accepted, and an inherited index is checked again against the subclass's field. The same refusal applies to a
vector field written as a union with ``None``; and before the indexes, on every model, a field whose ``Vector`` markers
are ambiguous is refused with an exact text naming the model and the field, a subclass redeclaring it too.

The table's vector columns (``TestFieldKind``, ``TestDefineTableE2E``): a vector field of either spelling, or its
union with ``None``, is written ``option<array<float, d>>`` by ``table_ddl()`` and ``define_table()``, whatever its
storage type; on every engine the column reads back as written, so a second ``define_table()`` changes nothing,
integers are stored as floats, and a vector of another length is refused.

The model's vector indexes applied (``TestDefineVectorIndexesE2E``), on every engine: ``define_indexes()`` applies the
analyzer and every index of a fresh table, the full-text one with the line's keyword; called again it sends ``INFO FOR
DB``, the line and ``INFO FOR TABLE`` and nothing else — an equal vector index is never rebuilt; one changed option
redefines that index alone, and ``overwrite=False`` keeps the stored one; an index declared without ``dist`` and the
same declaring ``dist="euclidean"`` each match the index the other defined; a model whose indexes are all vector
indexes reads no line; an index of another kind stored under the declared name — an ``MTREE`` index on SurrealDB 2.x
and the embedded engine, a plain one on every engine — is redefined ``HNSW``; ``remove_index()`` removes a vector index
by name and the next ``define_indexes()`` defines only what is missing; with a transaction the vector index joins the
block, and a block that raises applies none.

The engines' own vector DDL, pinned with raw statements (``TestVectorDdlPinsE2E``): an index without ``TYPE`` stores
``F64`` vectors on the embedded engine and ``F32`` on the servers, while the ORM's statement stores ``F32`` everywhere;
SurrealDB 3.x refuses ``MTREE`` (a parse error), which 2.x and the embedded engine accept; ``HNSW … UNIQUE`` stores a
plain ``UNIQUE`` index on every engine.

The vector search (``TestSimilarTo``): ``similar_to(field, vector, limit=10, *, ef=None, dist=None)`` checks its
arguments in a fixed order, each refusal with its exact text and class, before any request and leaving the queryset
unchanged — the field (a name, not the record id nor an edge endpoint, its model not defining ``_knn_distance``), the
vector (a list or tuple of finite real numbers, of the field's declared dimension, not a zero vector with ``COSINE``),
``limit`` and ``ef`` (1 to 10000), ``ef`` or ``dist`` (one of the ORM's distances); a second call replaces the first.
The statements are pinned: ``<column> <|K,EF|> $_f<N>`` (``EF`` at least ``K``, 100 by default) or
``<column> <|K,DIST|> $_f<N>`` AND-ed after the other conditions, the vector bound as a ``list[float]``, the distance
projected as ``_knn_distance`` after the other per-record values, ``ORDER BY _knn_distance`` unless ``order_by()`` is
given; an aggregation and a ``Subquery`` carry the condition without the distance. ``TestSimilarToE2E`` reads the
six-record corpus on every engine: the nearest records in order with their distances (the index's metric, or the exact
scan of ``dist=``), on a field, an aliased field and a dotted path; the distance set on each instance and never
written back, on an ``extra="allow"`` model too; dicts holding it when a row does not validate; ``first()``, ``get()``
without an id, ``order_by()``, ``limit()``/``offset()`` over the ``limit`` nearest; an ``ef`` below ``limit`` and the
largest ``limit``.

The clauses ``similar_to()`` cannot honour (``TestKnnRefusals``): ``as_of()`` — either way round, and a ``Subquery``
holding ``similar_to()`` under the outer one —, ``live()`` and ``watch()`` — its own condition, or a ``Subquery``
holding it — and ``changes()`` refuse it with exact texts before any request, the queryset left unchanged, after
``search()``'s refusals of the same clauses; the statement's compiler records each subquery's vector field. Its
combinations built (``TestSearchWithKnn``): ``search()``, its search annotations and a ``GeoDistance`` annotation are
accepted in either order, nothing sent. ``TestCombinationsE2E`` reads, on every engine, ``count()``, ``exists()``,
the other aggregations and a ``values()`` group restricted to the ``limit`` nearest; a ``Subquery`` holding
``similar_to()`` in another queryset's filter; ``cache()`` hits, misses and ``define_indexes()``'s invalidation; a
model of a named connection and a model inside ``using()`` of another one.

The other conditions next to the vector search (``TestPrefilter``): the statements are pinned in the form for
SurrealDB 3.x — every condition inline — and in the form below 3 — every condition but an id subquery selected first in
a ``LET``, the KNN searching those ids (``$_sq<N> CONTAINS id`` on a view model), a ``Subquery``'s ``LET`` before its
own —; the parts kept next to the KNN on every line are exactly the ORM's id subqueries; a statement without such a
condition is the same on every line and needs no line. The line is read once per client and decided once per read, a
re-send for a missing subquery table keeping it. ``TestPrefilterE2E`` reads, on every engine, a comparison, a function
lookup, a ``Q`` group, a negation, ``nearby()``, ``search()``, a condition on an indexed column and one through a
record link next to ``similar_to()`` (an index search and an exact scan): the same records everywhere, the form sent
being the line's, the line read once and only for such a condition; ``exists()``, ``count()`` and a ``values()`` group
through the ``LET``.

The search annotations next to the vector search (``TestSearchAnnotationsWithKnn``): projected before the distance in
the form for SurrealDB 3.x; below 3, ``exec()``, ``first()`` and ``get()`` without an id refuse them with an exact
text after the line is read and before anything is sent, queued in a transaction or cached, while ``exists()`` leaves
them out of its statement and answers; a queryset without its own ``similar_to()`` compiles as before.
``TestSearchAnnotationsWithKnnE2E``: on SurrealDB 3.x each of the nearest matches carries the score, highlight and
offsets the plain search gives it; below 3 the refusals, ``exists()`` and ``count()`` on every engine.

The bulk writes of a vector search (``TestWriteByIds``): ``bulk_update()``, ``bulk_delete()`` and ``patch()`` of a
``similar_to()`` queryset bind the ids of the nearest records in a ``LET`` of the statement's block — after the
``LET`` s of the other conditions, the pre-filter's below SurrealDB 3 included — and write by those ids, so the write
itself never carries the KNN operator; that ``LET`` reads the model's table, bound to ``[]`` once SurrealDB 3.x confirms
it missing; a view model's write is refused first; the line is read once per write and only next to another
condition, and a transaction queues the block of its line. ``TestWriteByIdsE2E`` writes, on every engine, exactly the
records the same read returns — next to a range on an indexed column, with an exact search, next to a comparison,
alone — and returns their number, a table that does not exist written as empty; ``TestWriteByIdsTxE2E`` writes them in
an interactive transaction and in a buffered one.

The dimension of the vector index (``TestDimensionRefusal``): SurrealDB's refusal of a query vector whose length is not
the index's dimension becomes ``SurrealDbError`` naming the field — the statement's own, its subqueries', several as
"one of the fields" — and both lengths read from the server's text, chained to the server's error; in a transaction
that error takes the server's place among the transaction's failures. ``TestDimensionE2E``, on a model that does not
declare the dimension (a hand-made index on a plain list field): a read and a count on every engine, and a write by
the ids of the nearest outside a transaction (SurrealDB 3.1 relays its own error from the ``LET``); a declared
dimension refuses the vector before any request. ``TestTransactionsE2E``: in an interactive transaction (SurrealDB 3.x
over WebSocket) the vector search sees the transaction's write, and a read refused for its dimension fails the commit,
caused by the ORM's error; in a buffered one (HTTP, SurrealDB 2.x, the embedded engine) a read raises, as every read
does there.

The engines' own answers to KNN statements the ORM does not send, pinned with raw statements on six records
(``TestLinePinsE2E``), each test naming the README row or sentence it backs, each line's answer asserted: ``<|K|>`` on
an HNSW index (SurrealDB 3.x refuses it, 2.x and the embedded engine answer no record); an ``EF`` below ``K`` (2.x
and the embedded engine return at most ``EF`` records, 3.x ``K`` in most index builds); two KNN operators; a field
without a vector index (no record; 3.1 every record, with no distance); a function call, a condition on an indexed
field, one through a record link and a full-text match next to the KNN (right on 3.x; too few records, none, or every
matching one on 2.x and the embedded engine), each right when its records are selected first in a ``LET``; writes
whose ``WHERE`` holds the KNN (2.x's exact scan writes every record; next to a range on an indexed field no record is
written, on any engine); a search score in the ``LET`` form (``None`` below 3, an error on 3.x); a live query whose
condition holds the KNN, which never notifies.

The documentation (``TestReadmeSection37``): README section 37 — its place after section 36, its seven subsections,
its examples (run on ``mem://`` as written, each printed line equal to its comment; the search annotations' block
printing the ORM's refusal; no ``limit`` nor ``ef`` outside 1 to 10000), its nine sentences, the paragraph on the union spellings and thirteen "Coming from"
bullets word for word —, the eight rows of the 2.x/3.x table and the row of the embedded engine's, cell by cell within
the tables' widths, the header sentence, and the features, roadmap and comparison rows. ``TestRoadmap36``:
``docs/ROADMAP.md`` records v0.36.0 as planned, or — once its Phase G row carries ✅ — as released.
"""

import ast
import asyncio
import contextlib
import copy
import inspect
import math
import os
import pickle
import re
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, Union

import pytest
from pydantic import BaseModel, Field, ValidationError, create_model
from pydantic.warnings import UnsupportedFieldAttributeWarning
from surrealdb import InternalError, RecordID, ServerError

import surreal_orm_lite
from surreal_orm_lite import (
    Analyzer,
    BaseSurrealEdge,
    BaseSurrealModel,
    Count,
    DatabaseIntrospector,
    FieldState,
    GeoDistance,
    Index,
    IndexState,
    LiveAction,
    PointField,
    Q,
    QueryCache,
    SearchHighlight,
    SearchOffsets,
    SearchScore,
    Subquery,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    SurrealDbError,
    Vector,
    VectorField,
    model_ddl,
    search,
    time_travel,
    vector,
)
from surreal_orm_lite.define_parser import normalise_kind, parse_define_field, parse_define_index, render_kind
from surreal_orm_lite.schema import view_write_refused
from surreal_orm_lite.subquery import TABLE_PROBE, SubqueryCompiler, _MissingSubqueryTable
from surreal_orm_lite.testing import embedded_database
from surreal_orm_lite.utils import validate_field_name
from surreal_orm_lite.vector import get_vector_info, is_vector_field
from tests._readme_tables import assert_embedded_rows, assert_server_header_sentence, assert_server_rows
from tests._readme_tables import normalised as _normalised
from tests._readme_tables import readme as _readme
from tests._readme_tables import readme_part as _readme_part
from tests._readme_tables import subsection as _subsection
from tests._readme_tables import table_rows as _table_rows
from tests._readme_tables import tables as _md_tables

_EXPORTS = ["VectorField", "Vector"]

_ARRAY_3 = {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}


class VecField(BaseSurrealModel):
    """Both spellings on one model, one of them aliased."""

    model_config = SurrealConfigDict(table_name="vec_t")

    id: str | None = None
    embedding: VectorField[3]
    typed: Annotated[list[float] | None, Vector(3)] = None
    aliased: Annotated[list[float] | None, Vector(3, "f64")] = Field(default=None, alias="aliased_vec")


class VecAssigned(BaseSurrealModel):
    """A model that validates assignments."""

    model_config = SurrealConfigDict(table_name="vec_assigned", validate_assignment=True)

    id: str | None = None
    embedding: VectorField[3]


with warnings.catch_warnings():
    # ``VectorField[3] | None`` holds VectorField's ``Field(default=None)`` in a union member, where Pydantic ignores it
    # with a warning (``test_a_union_with_none_needs_its_default``): each union field below writes its ``= None``.
    warnings.simplefilter("ignore", UnsupportedFieldAttributeWarning)

    class VecUnion(BaseSurrealModel):
        """The union spellings of a vector field (issue #318), one aliased, none indexed; and vectors held in
        containers, which are not vector fields."""

        model_config = SurrealConfigDict(table_name="vec_union")

        id: str | None = None
        optional: VectorField[3] | None = None
        legacy: Optional[VectorField[3]] = None  # noqa: UP045 - the spelling under test
        typed: Annotated[list[float], Vector(3)] | None = None
        wide: Annotated[list[float], Vector(3, "F64")] | None = None
        aliased: Annotated[list[float], Vector(3)] | None = Field(default=None, alias="aliased_vec")
        noted: Annotated[VectorField[3] | None, "a note"] = None
        listed: list[VectorField[3]] = Field(default_factory=list)
        keyed: dict[str, VectorField[3]] = Field(default_factory=dict)
        maybe_listed: list[VectorField[3]] | None = None


#: The union fields of ``VecUnion`` and the ``(dimension, storage type)`` each declares.
_UNION_FIELDS = [
    ("optional", (3, "F32")),
    ("legacy", (3, "F32")),
    ("typed", (3, "F32")),
    ("wide", (3, "F64")),
    ("aliased", (3, "F32")),
    ("noted", (3, "F32")),
]

#: The refusal of a field or an annotation whose ``Vector`` markers are ambiguous: its subject, then what it holds.
_AMBIGUOUS = (
    "{} holds {}; a vector field holds one Vector marker, and None at most. Write VectorField[d] alone (it is already "
    "optional) or Annotated[list[float] | None, Vector(d)]."
)
_NEXT_TO = "next to a type other than None"

#: Annotations whose markers are ambiguous, and what the refusal says they hold.
_AMBIGUOUS_ANNOTATIONS = [
    (VectorField[3] | str, f"Vector(3, 'F32') {_NEXT_TO}"),
    (VectorField[3] | Any, f"Vector(3, 'F32') {_NEXT_TO}"),
    (Annotated[list[float], Vector(3)] | int | None, f"Vector(3, 'F32') {_NEXT_TO}"),
    (Annotated[VectorField[3] | None, "a note"] | list[float], f"Vector(3, 'F32') {_NEXT_TO}"),
    (VectorField[3] | VectorField[4], "Vector(3, 'F32') and Vector(4, 'F32')"),
    (VectorField[3] | VectorField[3, "F64"] | None, "Vector(3, 'F32') and Vector(3, 'F64')"),
    (VectorField[2] | VectorField[3] | VectorField[4], "Vector(2, 'F32'), Vector(3, 'F32') and Vector(4, 'F32')"),
    (VectorField[2] | VectorField[3] | list[float], f"Vector(2, 'F32') and Vector(3, 'F32') {_NEXT_TO}"),
    (Annotated[list[float] | None, Vector(3), Vector(4)], "Vector(3, 'F32') and Vector(4, 'F32')"),
]
_AMBIGUOUS_IDS = [
    "str",
    "any",
    "typed-int-none",
    "noted-list",
    "two-dimensions",
    "two-types",
    "three-markers",
    "two-and-a-type",
    "two-in-one-annotated",
]

#: An ambiguous union as a name (a string in an annotation reads as a forward reference).
_MIXED: Any = VectorField[3] | str


def _model_of(name: str, base: type[BaseModel] = BaseSurrealModel, **fields: Any) -> type[BaseModel]:
    """A model *name* of *base* built by ``create_model()`` with *fields* — its ``class`` statement —, Pydantic's warning
    about a default in a union member silenced."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UnsupportedFieldAttributeWarning)
        return create_model(name, __base__=base, **fields)


# The field name each spelling is given under, for the refusals.
_SPELLINGS = [("embedding", "embedding"), ("typed", "typed"), ("aliased_vec", "aliased_vec")]

# A value VF3 refuses, and its exact text.
_VF3 = [
    ("1,2,3", "expected a list or tuple of 3 numbers, got str"),
    ({"x": 1.0}, "expected a list or tuple of 3 numbers, got dict"),
    ([1.0, 2.0], "expected 3 numbers, got 2"),
    ([1.0, 2.0, 3.0, 4.0], "expected 3 numbers, got 4"),
    ([True, 2.0, 3.0], "item 0 is bool, not a number"),
    ([1.0, "1.5", 3.0], "item 1 is str, not a number"),
    ([1.0, 2.0, None], "item 2 is NoneType, not a number"),
    ([1.0, 2.0, float("nan")], "item 2 is not finite (nan)"),
    ([float("inf"), 2.0, 3.0], "item 0 is not finite (inf)"),
    ([1.0, float("-inf"), 3.0], "item 1 is not finite (-inf)"),
    ([Decimal("NaN"), 2.0, 3.0], "item 0 is not finite (Decimal('NaN'))"),
    ([Decimal("1e400"), 2.0, 3.0], "item 0 is not finite (Decimal('1E+400'))"),
]


def _value_error(exc: ValidationError) -> tuple[tuple[Any, ...], str]:
    """The location and message of a ``ValidationError``'s only error."""
    errors = exc.errors()
    assert len(errors) == 1, errors
    return tuple(errors[0]["loc"]), errors[0]["msg"]


class TestVectorField:
    """``VectorField[d]`` and its typed form ``Annotated[list[float] | None, Vector(d)] = None``: one field."""

    def test_optional_and_none_by_default(self) -> None:
        doc = VecField()
        assert doc.embedding is None
        assert doc.typed is None
        assert doc.aliased is None
        for name in ("embedding", "typed", "aliased"):
            assert not VecField.model_fields[name].is_required(), name
            assert VecField.model_fields[name].default is None, name

    def test_a_union_with_none_needs_its_default(self) -> None:
        """``VectorField[3]`` is already optional. Written ``VectorField[3] | None``, its default sits in a union
        member, which Pydantic ignores with an ``UnsupportedFieldAttributeWarning``: without ``= None`` the field is
        required (what README section 37 says)."""
        with pytest.warns(UnsupportedFieldAttributeWarning):
            model = create_model("VecRequired", __base__=BaseSurrealModel, id=(str | None, None), e=VectorField[3] | None)
        assert model.model_fields["e"].is_required()
        with pytest.raises(ValidationError):
            model()
        for name, _ in _UNION_FIELDS:
            assert not VecUnion.model_fields[name].is_required(), name
        assert VecUnion().optional is None

    @pytest.mark.parametrize(("name", "info"), _UNION_FIELDS)
    def test_a_union_spelling_checks_its_values(self, name: str, info: tuple[int, str]) -> None:
        assert getattr(VecUnion(**{name: (1, 2, Decimal("3.5"))}), name) == [1.0, 2.0, 3.5]
        assert getattr(VecUnion(**{name: None}), name) is None
        with pytest.raises(ValidationError):
            VecUnion(**{name: [1.0, 2.0]})

    def test_ints_stored_as_floats(self) -> None:
        doc = VecField(embedding=[1, 2, 3], typed=[1, 2, 3], aliased_vec=[1, 2, 3])
        for value in (doc.embedding, doc.typed, doc.aliased):
            assert value == [1.0, 2.0, 3.0]
            assert isinstance(value, list)
            assert [type(item) for item in value] == [float, float, float]

    def test_a_tuple_is_accepted_and_stored_as_a_list(self) -> None:
        doc = VecField(embedding=(1.0, 0.0, 0.5), typed=(0, 1, 0))
        assert doc.embedding == [1.0, 0.0, 0.5]
        assert doc.typed == [0.0, 1.0, 0.0]
        assert isinstance(doc.embedding, list)
        assert isinstance(doc.typed, list)

    def test_decimal_and_real_items_are_accepted(self) -> None:
        doc = VecField(embedding=[Decimal("0.5"), Fraction(1, 2), 1], typed=[Fraction(1, 4), Decimal("-2"), 0.25])
        assert doc.embedding == [0.5, 0.5, 1.0]
        assert doc.typed == [0.25, -2.0, 0.25]
        assert [type(item) for item in doc.embedding] == [float, float, float]

    def test_the_aliased_field_by_alias_and_by_name(self) -> None:
        assert VecField(aliased_vec=[1, 0, 0]).aliased == [1.0, 0.0, 0.0]
        assert VecField(aliased=[0, 1, 0]).aliased == [0.0, 1.0, 0.0]
        assert VecField.model_validate({"aliased_vec": (0, 0, 1)}).aliased == [0.0, 0.0, 1.0]

    def test_none_is_accepted(self) -> None:
        doc = VecField(embedding=None, typed=None, aliased_vec=None)
        assert (doc.embedding, doc.typed, doc.aliased) == (None, None, None)

    @pytest.mark.parametrize(("field", "loc"), _SPELLINGS)
    @pytest.mark.parametrize(("value", "text"), _VF3)
    def test_vf3_a_value_refused(self, field: str, loc: str, value: Any, text: str) -> None:
        with pytest.raises(ValidationError) as caught:
            VecField.model_validate({field: value})
        assert _value_error(caught.value) == ((loc,), f"Value error, {text}")

    def test_vf3_on_a_read(self) -> None:
        with pytest.raises(ValidationError) as caught:
            VecField.from_db([{"id": "vec_t:1", "embedding": [1.0, 2.0]}])
        assert _value_error(caught.value) == (("embedding",), "Value error, expected 3 numbers, got 2")
        with pytest.raises(ValidationError) as caught:
            VecField.from_db({"id": "vec_t:1", "aliased_vec": [1.0, float("nan"), 0.0]})
        assert _value_error(caught.value) == (("aliased_vec",), "Value error, item 1 is not finite (nan)")

    def test_a_read_converts_decimals(self) -> None:
        doc = VecField.from_db({"id": "vec_t:1", "embedding": [Decimal("1"), Decimal("0.5"), 0], "typed": None})
        assert isinstance(doc, VecField)
        assert doc.embedding == [1.0, 0.5, 0.0]
        assert [type(item) for item in doc.embedding] == [float, float, float]
        assert doc.typed is None

    def test_an_assignment_is_checked_on_a_model_validating_assignments(self) -> None:
        doc = VecAssigned(embedding=[1, 2, 3])
        doc.embedding = (0, 1, 0)
        assert doc.embedding == [0.0, 1.0, 0.0]
        with pytest.raises(ValidationError) as caught:
            doc.embedding = [1.0]
        assert _value_error(caught.value) == (("embedding",), "Value error, expected 3 numbers, got 1")
        doc.embedding = None
        assert doc.embedding is None

    def test_model_dump_gives_the_list(self) -> None:
        doc = VecField(embedding=(1, 2, 3), typed=[0.5, 0, 0], aliased_vec=[Decimal("1"), 0, 0])
        dumped = doc.model_dump()
        assert dumped["embedding"] == [1.0, 2.0, 3.0]
        assert dumped["typed"] == [0.5, 0.0, 0.0]
        assert dumped["aliased"] == [1.0, 0.0, 0.0]
        assert doc.model_dump(by_alias=True)["aliased_vec"] == [1.0, 0.0, 0.0]
        as_json = doc.model_dump(mode="json")
        assert as_json["embedding"] == [1.0, 2.0, 3.0]
        assert as_json["typed"] == [0.5, 0.0, 0.0]
        assert VecField().model_dump(mode="json")["embedding"] is None
        assert '"embedding":[1.0,2.0,3.0]' in doc.model_dump_json()

    def test_json_schema_of_both_spellings(self) -> None:
        properties = VecField.model_json_schema()["properties"]
        for key, title in (("embedding", "Embedding"), ("typed", "Typed"), ("aliased_vec", "Aliased Vec")):
            assert properties[key] == {"anyOf": [_ARRAY_3, {"type": "null"}], "default": None, "title": title}, key
        assert "aliased" not in properties
        assert "embedding" not in VecField.model_json_schema().get("required", [])

    def test_the_storage_type_is_upper_case_and_f32_by_default(self) -> None:
        assert get_vector_info(VectorField[3]) == (3, "F32")
        assert get_vector_info(VectorField[3, "f64"]) == (3, "F64")
        assert get_vector_info(VectorField[1, "i16"]) == (1, "I16")
        assert get_vector_info(VectorField[65535, "I32"]) == (65535, "I32")
        assert get_vector_info(VectorField[2, "I64"]) == (2, "I64")
        assert get_vector_info(VectorField[2, "F32"]) == (2, "F32")

    def test_the_annotation(self) -> None:
        annotation = VectorField[3, "F64"]
        assert annotation.__origin__ == list[float] | None
        markers = [item for item in annotation.__metadata__ if isinstance(item, Vector)]
        assert markers == [Vector(3, "F64")]

    def test_vector_field_is_never_instantiated(self) -> None:
        with pytest.raises(TypeError) as caught:
            VectorField(3)  # type: ignore[call-arg]
        assert str(caught.value) == "VectorField is an annotation, not a value: write `embedding: VectorField[1536]`."

    @pytest.mark.parametrize(
        ("build", "offending"),
        [
            (lambda: VectorField[True], "bool"),
            (lambda: VectorField["3"], "str"),
            (lambda: VectorField[3.0], "float"),
            (lambda: VectorField[3, 64], "int"),
            (lambda: VectorField[True, "F32"], "bool"),
            (lambda: VectorField["3", "F32"], "str"),
            (lambda: VectorField[(3, "F32", 1)], "tuple"),
            (lambda: VectorField[(3,)], "tuple"),
            (lambda: VectorField[None], "NoneType"),
        ],
        ids=["bool", "str", "float", "int-type", "bool-dimension", "str-dimension", "three-items", "one-item", "none"],
    )
    def test_vf1(self, build: Any, offending: str) -> None:
        with pytest.raises(TypeError) as caught:
            build()
        assert str(caught.value) == (
            "VectorField takes a dimension (an int), or a dimension and a storage type, such as VectorField[1536] or "
            f'VectorField[384, "F64"], not {offending}.'
        )

    @pytest.mark.parametrize(
        ("build", "text"),
        [
            (lambda: VectorField[0], "VectorField dimension must be between 1 and 65535 (SurrealDB's DIMENSION), got 0."),
            (
                lambda: VectorField[65536],
                "VectorField dimension must be between 1 and 65535 (SurrealDB's DIMENSION), got 65536.",
            ),
            (
                lambda: VectorField[-3, "F32"],
                "VectorField dimension must be between 1 and 65535 (SurrealDB's DIMENSION), got -3.",
            ),
            (
                lambda: VectorField[3, "F16"],
                "VectorField storage type must be one of F64, F32, I64, I32 and I16, got 'F16'.",
            ),
            (lambda: VectorField[3, "x"], "VectorField storage type must be one of F64, F32, I64, I32 and I16, got 'x'."),
        ],
        ids=["zero", "65536", "negative", "F16", "x"],
    )
    def test_vf2(self, build: Any, text: str) -> None:
        with pytest.raises(ValueError) as caught:
            build()
        assert str(caught.value) == text

    def test_exported(self) -> None:
        for name in _EXPORTS:
            assert name in surreal_orm_lite.__all__, name
            assert name in vector.__all__, name
            assert getattr(surreal_orm_lite, name) is getattr(vector, name)
        assert vector.__all__ == ["VectorField", "Vector", "get_vector_info", "is_vector_field"]
        names = surreal_orm_lite.__all__
        assert names[names.index("VectorField") : names.index("VectorField") + 2] == _EXPORTS

    def test_the_module_imports_no_model_queryset_search_or_connection_module(self) -> None:
        tree = ast.parse(Path(vector.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(f"{node.module or ''}.{alias.name}".lstrip(".") for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        for module in ("search", "model_base", "query_set", "edge", "model_auth", "connection_manager"):
            assert not any(
                name == module or name.endswith(f".{module}") or name.startswith(f"{module}.") for name in imported
            ), module


class TestVectorMarker:
    """``Vector(dimension, vector_type="F32")``, the marker of both spellings, and the helpers that read it."""

    def test_attributes(self) -> None:
        marker = Vector(1536)
        assert (marker.dimension, marker.vector_type) == (1536, "F32")
        assert Vector(384, "f64").vector_type == "F64"
        assert Vector(dimension=2, vector_type="i32") == Vector(2, "I32")
        assert not hasattr(marker, "__dict__")

    def test_equality_hash_and_repr(self) -> None:
        assert Vector(3) == Vector(3, "F32") == Vector(3, "f32")
        assert hash(Vector(3)) == hash(Vector(3, "F32"))
        assert repr(Vector(3)) == "Vector(3, 'F32')"
        assert repr(Vector(3, "f64")) == "Vector(3, 'F64')"
        assert Vector(3) != Vector(3, "F64")
        assert Vector(3) != Vector(4)
        assert Vector(3) != (3, "F32")
        assert len({Vector(3), Vector(3, "F32"), Vector(3, "F64")}) == 2

    def test_read_only(self) -> None:
        marker = Vector(3)
        with pytest.raises(AttributeError):
            marker.dimension = 4  # type: ignore[misc]
        with pytest.raises(AttributeError):
            del marker.vector_type
        assert marker == Vector(3)

    def test_copied_and_pickled_as_an_equal_marker(self) -> None:
        marker = Vector(3, "F64")
        assert copy.copy(marker) == marker
        assert copy.deepcopy(marker) == marker
        assert pickle.loads(pickle.dumps(marker)) == marker

    def test_the_marker_of_each_spelling(self) -> None:
        assert Vector(3) in VecField.model_fields["embedding"].metadata
        assert Vector(3) in VecField.model_fields["typed"].metadata
        assert Vector(3, "F64") in VecField.model_fields["aliased"].metadata

    @pytest.mark.parametrize(
        ("build", "offending"),
        [
            (lambda: Vector(True), "bool"),
            (lambda: Vector("3"), "str"),
            (lambda: Vector(3.0), "float"),
            (lambda: Vector(3, 64), "int"),
            (lambda: Vector(3, None), "NoneType"),
            (lambda: Vector(0, 64), "int"),
        ],
        ids=["bool", "str", "float", "int-type", "none-type", "types-first"],
    )
    def test_vf4(self, build: Any, offending: str) -> None:
        with pytest.raises(TypeError) as caught:
            build()
        assert str(caught.value) == (
            "Vector takes a dimension (an int) and an optional storage type (a str), such as Vector(1536) or "
            f'Vector(384, "F64"), not {offending}.'
        )

    @pytest.mark.parametrize(
        ("build", "text"),
        [
            (lambda: Vector(0), "Vector dimension must be between 1 and 65535 (SurrealDB's DIMENSION), got 0."),
            (lambda: Vector(65536), "Vector dimension must be between 1 and 65535 (SurrealDB's DIMENSION), got 65536."),
            (lambda: Vector(3, "F16"), "Vector storage type must be one of F64, F32, I64, I32 and I16, got 'F16'."),
            (lambda: Vector(3, ""), "Vector storage type must be one of F64, F32, I64, I32 and I16, got ''."),
        ],
        ids=["zero", "65536", "F16", "empty"],
    )
    def test_vf2(self, build: Any, text: str) -> None:
        with pytest.raises(ValueError) as caught:
            build()
        assert str(caught.value) == text

    def test_bounds_accepted(self) -> None:
        assert Vector(1).dimension == 1
        assert Vector(65535).dimension == 65535

    def test_get_vector_info_on_model_fields(self) -> None:
        fields = VecField.model_fields
        assert get_vector_info(fields["embedding"]) == (3, "F32")
        assert get_vector_info(fields["typed"]) == (3, "F32")
        assert get_vector_info(fields["aliased"]) == (3, "F64")
        assert get_vector_info(fields["id"]) is None

    def test_get_vector_info_on_annotations(self) -> None:
        assert get_vector_info(Annotated[list[float] | None, Vector(3)]) == (3, "F32")
        assert get_vector_info(Annotated[list[float] | None, "doc", Vector(5, "i16")]) == (5, "I16")
        assert get_vector_info(Annotated[Annotated[list[float] | None, Vector(2)], "doc"]) == (2, "F32")
        assert get_vector_info(list[float]) is None
        assert get_vector_info(list[float] | None) is None
        assert get_vector_info(Annotated[list[float], "doc"]) is None
        assert get_vector_info(None) is None
        assert get_vector_info(3) is None
        assert get_vector_info(Vector(3)) is None

    def test_is_vector_field(self) -> None:
        fields = VecField.model_fields
        assert is_vector_field(fields["embedding"]) is True
        assert is_vector_field(fields["typed"]) is True
        assert is_vector_field(fields["aliased"]) is True
        assert is_vector_field(VectorField[3]) is True
        assert is_vector_field(Annotated[list[float] | None, Vector(3)]) is True
        assert is_vector_field(fields["id"]) is False
        assert is_vector_field(list[float]) is False

    @pytest.mark.parametrize(("name", "info"), _UNION_FIELDS)
    def test_the_union_spellings_of_a_model_field(self, name: str, info: tuple[int, str]) -> None:
        """The marker sits in a union member, not in the field's metadata: the helpers read the annotation."""
        field = VecUnion.model_fields[name]
        assert not any(isinstance(item, Vector) for item in field.metadata)
        assert get_vector_info(field) == info
        assert is_vector_field(field) is True

    @pytest.mark.parametrize(
        ("annotation", "info"),
        [
            (VectorField[3] | None, (3, "F32")),
            (Optional[VectorField[3]], (3, "F32")),  # noqa: UP045 - the spelling under test
            (Union[None, VectorField[3]], (3, "F32")),  # noqa: UP007 - the spelling under test
            (VectorField[384, "i16"] | None, (384, "I16")),
            (Annotated[list[float], Vector(3)] | None, (3, "F32")),
            (Annotated[list[float], Vector(3, "F64")] | None, (3, "F64")),
            (Annotated[list[float] | None, Vector(3)] | None, (3, "F32")),
            (Annotated[VectorField[3] | None, "a note"], (3, "F32")),
            (Annotated[list[float], Vector(2)] | Annotated[list[float] | None, Vector(2)], (2, "F32")),
            (Annotated[VectorField[4] | None, Vector(3)], (3, "F32")),
        ],
        ids=[
            "or-none",
            "optional",
            "union-none-first",
            "i16",
            "typed",
            "typed-f64",
            "typed-optional-or-none",
            "noted",
            "one-marker-twice",
            "outer-marker",
        ],
    )
    def test_the_union_spellings_of_an_annotation(self, annotation: Any, info: tuple[int, str]) -> None:
        assert get_vector_info(annotation) == info
        assert is_vector_field(annotation) is True

    @pytest.mark.parametrize("name", ["listed", "keyed", "maybe_listed"])
    def test_vectors_in_a_container_field_are_not_a_vector_field(self, name: str) -> None:
        field = VecUnion.model_fields[name]
        assert get_vector_info(field) is None
        assert is_vector_field(field) is False

    @pytest.mark.parametrize(
        "annotation",
        [
            list[VectorField[3]],
            dict[str, VectorField[3]],
            list[VectorField[3]] | None,
            tuple[VectorField[3], ...],
            Annotated[list[VectorField[3]], "a note"],
            list[float] | dict[str, VectorField[3]],
        ],
        ids=["list", "dict", "list-or-none", "tuple", "noted-list", "union-of-containers"],
    )
    def test_vectors_in_a_container_are_not_a_vector_field(self, annotation: Any) -> None:
        assert get_vector_info(annotation) is None
        assert is_vector_field(annotation) is False

    @pytest.mark.parametrize(("annotation", "held"), _AMBIGUOUS_ANNOTATIONS, ids=_AMBIGUOUS_IDS)
    def test_an_ambiguous_annotation_is_refused(self, annotation: Any, held: str) -> None:
        text = _AMBIGUOUS.format("The annotation", held)
        assert _raised(ValueError, get_vector_info, annotation) == text
        assert _raised(ValueError, is_vector_field, annotation) == text

    def test_an_ambiguous_field_of_a_pydantic_model_is_refused(self) -> None:
        """A plain Pydantic model is not checked at its ``class`` statement: the helpers refuse its field."""
        field = _model_of("VecPlain", BaseModel, mixed=(_MIXED, None)).model_fields["mixed"]
        text = _AMBIGUOUS.format("The field", f"Vector(3, 'F32') {_NEXT_TO}")
        assert _raised(ValueError, get_vector_info, field) == text
        assert _raised(ValueError, is_vector_field, field) == text


class TestCheckers:
    """The pure checks of the vector search's arguments: exact texts, no client, no request."""

    def test_constants(self) -> None:
        assert vector.MAX_K == 10_000
        assert vector.MAX_EF == 10_000
        assert vector.DEFAULT_EF == 100
        assert vector.MAX_DIMENSION == 65535
        assert vector.MAX_EFC == 65535
        assert vector.M_RANGE == (2, 127)
        assert vector.DISTANCES == ("EUCLIDEAN", "COSINE", "MANHATTAN", "CHEBYSHEV", "HAMMING")
        assert vector.VECTOR_TYPES == ("F64", "F32", "I64", "I32", "I16")
        assert vector.DEFAULTS == {"dist": "EUCLIDEAN", "vector_type": "F32", "efc": 150, "m": 12}
        assert vector.DISTANCE_ALIAS == "_knn_distance"

    @pytest.mark.parametrize("name", ["EUCLIDEAN", "COSINE", "MANHATTAN", "CHEBYSHEV", "HAMMING"])
    def test_checked_distance_in_any_case(self, name: str) -> None:
        for spelling in (name, name.lower(), name.capitalize()):
            assert vector.checked_distance(spelling, subject="Index 'i'") == name
        assert vector.checked_distance(name.lower(), subject="similar_to()") in vector.DISTANCES

    @pytest.mark.parametrize(
        ("value", "text"),
        [
            ("dot", "unknown distance 'dot'; the ORM's are EUCLIDEAN, COSINE, MANHATTAN, CHEBYSHEV and HAMMING."),
            ("", "unknown distance ''; the ORM's are EUCLIDEAN, COSINE, MANHATTAN, CHEBYSHEV and HAMMING."),
            (
                "minkowski",
                "MINKOWSKI needs its order, which this version does not write; use EUCLIDEAN (order 2) or MANHATTAN (order 1).",
            ),
            (
                "jaccard",
                "JACCARD is not a distance SurrealDB ranks by: identical vectors are not the nearest with it. Use COSINE.",
            ),
            (
                "Pearson",
                "PEARSON is not a distance SurrealDB ranks by: identical vectors are not the nearest with it. Use COSINE.",
            ),
        ],
        ids=["unknown", "empty", "minkowski", "jaccard", "pearson"],
    )
    def test_ix14_and_v10_values(self, value: str, text: str) -> None:
        for subject in ("Index 'i'", "similar_to()"):
            with pytest.raises(ValueError) as caught:
                vector.checked_distance(value, subject=subject)
            assert str(caught.value) == f"{subject}: {text}"

    @pytest.mark.parametrize(("value", "offending"), [(3, "int"), (None, "NoneType"), (b"COSINE", "bytes")])
    def test_ix14_and_v10_types(self, value: Any, offending: str) -> None:
        for subject in ("Index 'i'", "similar_to()"):
            with pytest.raises(TypeError) as caught:
                vector.checked_distance(value, subject=subject)
            assert str(caught.value) == f"{subject}: dist= must be a str, got {offending}."

    def test_checked_vector_type(self) -> None:
        for name in ("F64", "F32", "I64", "I32", "I16"):
            assert vector.checked_vector_type(name.lower(), subject="Index 'i'") == name
        with pytest.raises(ValueError) as caught:
            vector.checked_vector_type("F16", subject="Index 'i'")
        assert str(caught.value) == "Index 'i': vector_type= must be one of F64, F32, I64, I32 and I16, got 'F16'."
        with pytest.raises(TypeError) as caught_type:
            vector.checked_vector_type(64, subject="Index 'i'")
        assert str(caught_type.value) == "Index 'i': vector_type= must be a str, got int."

    def test_checked_count(self) -> None:
        bounds: dict[str, Any] = {"subject": "Index 'i'", "arg": "dimension", "low": 1, "high": 65535}
        assert vector.checked_count(1, **bounds) == 1
        assert vector.checked_count(65535, **bounds) == 65535
        for value in (0, 65536, -1):
            with pytest.raises(ValueError) as caught:
                vector.checked_count(value, **bounds)
            assert str(caught.value) == f"Index 'i': dimension= must be between 1 and 65535, got {value!r}."
        for value, offending in ((True, "bool"), ("3", "str"), (3.0, "float"), (None, "NoneType")):
            with pytest.raises(TypeError) as caught_type:
                vector.checked_count(value, **bounds)
            assert str(caught_type.value) == f"Index 'i': dimension= must be an int, got {offending}."

    def test_checked_count_of_m(self) -> None:
        low, high = vector.M_RANGE
        bounds: dict[str, Any] = {"subject": "Index 'i'", "arg": "m", "low": low, "high": high}
        assert vector.checked_count(2, **bounds) == 2
        assert vector.checked_count(127, **bounds) == 127
        for value in (1, 128):
            with pytest.raises(ValueError) as caught:
                vector.checked_count(value, **bounds)
            assert str(caught.value) == f"Index 'i': m= must be between 2 and 127, got {value}."

    def test_checked_query_vector_converts_to_floats(self) -> None:
        assert vector.checked_query_vector([1, 0, 0]) == [1.0, 0.0, 0.0]
        converted = vector.checked_query_vector((Decimal("0.5"), Fraction(1, 4), 2.5, -1))
        assert converted == [0.5, 0.25, 2.5, -1.0]
        assert isinstance(converted, list)
        assert [type(item) for item in converted] == [float, float, float, float]
        assert vector.checked_query_vector([0.0]) == [0.0]

    @pytest.mark.parametrize(
        ("value", "text"),
        [
            (
                {"a": 1.0},
                "similar_to() takes the vector as a list or tuple of numbers, got dict; convert an array with .tolist().",
            ),
            (
                "1,0,0",
                "similar_to() takes the vector as a list or tuple of numbers, got str; convert an array with .tolist().",
            ),
            (
                None,
                "similar_to() takes the vector as a list or tuple of numbers, got NoneType; convert an array with .tolist().",
            ),
            ([True, 0.0, 0.0], "similar_to(): vector item 0 is bool, not a number."),
            ([1.0, "0", 0.0], "similar_to(): vector item 1 is str, not a number."),
            ([1.0, 0.0, [0.0]], "similar_to(): vector item 2 is list, not a number."),
        ],
        ids=["dict", "str", "none", "bool-item", "str-item", "list-item"],
    )
    def test_v4(self, value: Any, text: str) -> None:
        with pytest.raises(TypeError) as caught:
            vector.checked_query_vector(value)
        assert str(caught.value) == text

    @pytest.mark.parametrize(
        ("value", "text"),
        [
            ([], "similar_to(): the vector is empty."),
            ((), "similar_to(): the vector is empty."),
            ([1.0, float("nan")], "similar_to(): vector item 1 is not finite (nan)."),
            ([float("inf"), 0.0], "similar_to(): vector item 0 is not finite (inf)."),
            ([0.0, Decimal("-Infinity")], "similar_to(): vector item 1 is not finite (Decimal('-Infinity'))."),
            ([0.0, Decimal("sNaN")], "similar_to(): vector item 1 is not finite (Decimal('sNaN'))."),
        ],
        ids=["empty-list", "empty-tuple", "nan", "inf", "decimal-infinity", "decimal-snan"],
    )
    def test_v5(self, value: Any, text: str) -> None:
        with pytest.raises(ValueError) as caught:
            vector.checked_query_vector(value)
        assert str(caught.value) == text

    def test_checked_limit(self) -> None:
        for value in (1, 10, 10_000):
            assert vector.checked_limit(value) == value
        for value, offending in ((True, "bool"), (2.0, "float"), ("2", "str"), (None, "NoneType")):
            with pytest.raises(TypeError) as caught_type:
                vector.checked_limit(value)
            assert str(caught_type.value) == f"similar_to() limit must be an int, got {offending}."
        for value in (0, -1, 10_001, 2**32):
            with pytest.raises(ValueError) as caught:
                vector.checked_limit(value)
            assert str(caught.value) == f"similar_to() limit must be between 1 and 10000, got {value!r}."

    def test_checked_ef(self) -> None:
        assert vector.checked_ef(None) is None
        for value in (1, 40, 10_000):
            assert vector.checked_ef(value) == value
        for value, offending in ((True, "bool"), (False, "bool"), (40.0, "float"), ("40", "str")):
            with pytest.raises(TypeError) as caught_type:
                vector.checked_ef(value)
            assert str(caught_type.value) == f"similar_to() ef must be an int or None, got {offending}."
        for value in (0, -1, 10_001, 2**32):
            with pytest.raises(ValueError) as caught:
                vector.checked_ef(value)
            assert str(caught.value) == f"similar_to() ef must be between 1 and 10000, got {value!r}."


class TestRenderers:
    """The pure renderers: the HNSW clause of an index and the KNN condition of a query."""

    def test_hnsw_clause(self) -> None:
        assert vector.hnsw_clause(3, "COSINE", "F32", 150, 12) == "HNSW DIMENSION 3 DIST COSINE TYPE F32 EFC 150 M 12"
        assert vector.hnsw_clause(2, "MANHATTAN", "F64", 1, 2) == "HNSW DIMENSION 2 DIST MANHATTAN TYPE F64 EFC 1 M 2"
        assert (
            vector.hnsw_clause(65535, "HAMMING", "I16", 65535, 127)
            == "HNSW DIMENSION 65535 DIST HAMMING TYPE I16 EFC 65535 M 127"
        )

    def test_hnsw_clause_writes_checked_values_only(self) -> None:
        with pytest.raises(ValueError):
            vector.hnsw_clause(3, "MINKOWSKI", "F32", 150, 12)
        with pytest.raises(ValueError):
            vector.hnsw_clause(3, "COSINE", "F16", 150, 12)
        with pytest.raises(TypeError):
            vector.hnsw_clause(True, "COSINE", "F32", 150, 12)
        with pytest.raises(ValueError):
            vector.hnsw_clause(0, "COSINE", "F32", 150, 12)
        with pytest.raises(ValueError):
            vector.hnsw_clause(3, "COSINE", "F32", 0, 12)
        with pytest.raises(ValueError):
            vector.hnsw_clause(3, "COSINE", "F32", 150, 128)

    def test_knn_condition(self) -> None:
        assert vector.knn_condition("embedding", 2, None, None, "_f0") == "embedding <|2,100|> $_f0"
        assert vector.knn_condition("embedding", 50, 10, None, "_f0") == "embedding <|50,50|> $_f0"
        assert vector.knn_condition("embedding", 3, 40, None, "_f1") == "embedding <|3,40|> $_f1"
        assert vector.knn_condition("embedding", 200, None, None, "_f2") == "embedding <|200,200|> $_f2"
        assert vector.knn_condition("embedding", 2, None, "MANHATTAN", "_f0") == "embedding <|2,MANHATTAN|> $_f0"
        assert vector.knn_condition("meta.vec", 1, None, "cosine", "_f3") == "meta.vec <|1,COSINE|> $_f3"
        assert vector.knn_condition("e", 10_000, 10_000, None, "_f0") == "e <|10000,10000|> $_f0"

    def test_knn_condition_writes_checked_values_only(self) -> None:
        with pytest.raises(ValueError) as caught:
            vector.knn_condition("embedding", 0, None, None, "_f0")
        assert str(caught.value) == "similar_to() limit must be between 1 and 10000, got 0."
        with pytest.raises(ValueError) as caught:
            vector.knn_condition("embedding", 2, 0, None, "_f0")
        assert str(caught.value) == "similar_to() ef must be between 1 and 10000, got 0."
        with pytest.raises(ValueError):
            vector.knn_condition("embedding", 2, None, "PEARSON", "_f0")
        with pytest.raises(ValueError) as caught:
            vector.knn_condition("embedding", 2, 40, "COSINE", "_f0")
        assert str(caught.value) == "knn_condition() takes ef or dist, not both."

    def test_defaults(self) -> None:
        assert vector.DEFAULTS == {"dist": "EUCLIDEAN", "vector_type": "F32", "efc": 150, "m": 12}
        assert vector.DEFAULTS["dist"] in vector.DISTANCES
        assert vector.DEFAULTS["vector_type"] in vector.VECTOR_TYPES


# === Index: the vector arguments ===================================================================================

VEC_PLAIN = Analyzer("vec_plain", ["blank"], ["lowercase"])


class VecDoc(BaseSurrealModel):
    """The model of the goldens and of the E2E tests: a vector index on each spelling of the field (the typed one
    aliased), and a full-text index."""

    model_config = SurrealConfigDict(
        table_name="vec_doc",
        indexes=[
            Index("vec_doc_embedding", "embedding", hnsw=True, dimension=3, dist="cosine"),
            Index(
                "vec_doc_summary",
                "summary_vec",
                hnsw=True,
                dimension=3,
                vector_type="f64",
                efc=100,
                m=8,
                comment="it's a summary",
            ),
            Index("vec_doc_title", "title", search_analyzer=VEC_PLAIN, highlights=True),
        ],
    )

    id: str | None = None
    title: str
    category: str = ""
    n: int = 0
    embedding: VectorField[3]
    summary_vec: Annotated[list[float] | None, Vector(3, "F64")] = Field(default=None, alias="summary")
    loc: PointField = None


#: The vector index of ``meta.vec`` on ``vec_t``: every option declared, the smallest ``EFC`` and ``M``.
_META = Index("vec_meta", "meta.vec", hnsw=True, dimension=2, dist="Manhattan", efc=1, m=2)

_H1 = (
    "DEFINE INDEX OVERWRITE `vec_doc_embedding` ON `vec_doc` FIELDS `embedding` HNSW DIMENSION 3 DIST COSINE TYPE F32 "
    "EFC 150 M 12;"
)
_H_SUMMARY = (
    "DEFINE INDEX OVERWRITE `vec_doc_summary` ON `vec_doc` FIELDS `summary` HNSW DIMENSION 3 DIST EUCLIDEAN TYPE F64 "
    "EFC 100 M 8 COMMENT 'it\\'s a summary';"
)
_H_TITLE = "DEFINE INDEX OVERWRITE `vec_doc_title` ON `vec_doc` FIELDS `title` FULLTEXT ANALYZER `vec_plain` HIGHLIGHTS;"
_A_PLAIN = "DEFINE ANALYZER OVERWRITE `vec_plain` TOKENIZERS blank FILTERS lowercase;"
_H2 = [_A_PLAIN, _H1, _H_SUMMARY, _H_TITLE]
_H3 = [_A_PLAIN, _H1, _H_SUMMARY, _H_TITLE.replace("FULLTEXT", "SEARCH")]
_H4 = [statement.replace("OVERWRITE", "IF NOT EXISTS") for statement in _H2]
_H5 = "DEFINE INDEX OVERWRITE `vec_meta` ON `vec_t` FIELDS `meta`.`vec` HNSW DIMENSION 2 DIST MANHATTAN TYPE F32 EFC 1 M 2;"

#: A vector index declared with ``dimension`` only, and its statement: every other option is SurrealDB's default.
_BARE = "DEFINE INDEX OVERWRITE `i` ON `t` FIELDS `e` HNSW DIMENSION 3 DIST EUCLIDEAN TYPE F32 EFC 150 M 12;"

#: What ``INFO FOR TABLE`` renders for these indexes — the same text on SurrealDB 3.3.4, 2.7.0, 3.1.5 and the embedded
#: engine: every option, ``M0`` and ``LM`` derived from ``M``, the comment in double quotes.
_STORED = {
    "vec_doc_embedding": (
        "DEFINE INDEX vec_doc_embedding ON vec_doc FIELDS embedding HNSW DIMENSION 3 DIST COSINE TYPE F32 EFC 150 M 12 "
        "M0 24 LM 0.40242960438184466f"
    ),
    "vec_doc_summary": (
        "DEFINE INDEX vec_doc_summary ON vec_doc FIELDS summary HNSW DIMENSION 3 DIST EUCLIDEAN TYPE F64 EFC 100 M 8 "
        'M0 16 LM 0.48089834696298783f COMMENT "it\'s a summary"'
    ),
    "vec_meta": (
        "DEFINE INDEX vec_meta ON vec_t FIELDS meta.vec HNSW DIMENSION 2 DIST MANHATTAN TYPE F32 EFC 1 M 2 M0 4 "
        "LM 1.4426950408889634f"
    ),
}
_STORED_BARE = (
    "DEFINE INDEX i ON t FIELDS e HNSW DIMENSION 3 DIST EUCLIDEAN TYPE F32 EFC 150 M 12 M0 24 LM 0.40242960438184466f"
)

_IX11 = "Index 'i': dimension=, dist=, vector_type=, efc= and m= need hnsw=True (a vector index)."
_IX12 = "Index 'i': hnsw=True needs dimension= (the vectors' length, 1 to 65535)."
_IX17 = "Index 'i': a vector index cannot be UNIQUE (SurrealDB would keep a plain UNIQUE index and drop HNSW)."
_IX18 = "Index 'i': an index is either full-text (search_analyzer=) or vector (hnsw=True), not both."


def _raised(error: type[Exception], call: Any, *args: Any, **kwargs: Any) -> str:
    """The text of the *error* ``call(*args, **kwargs)`` raises, its class checked exactly."""
    with pytest.raises(error) as raised:
        call(*args, **kwargs)
    assert type(raised.value) is error
    return str(raised.value)


def _doc_columns(index: Index) -> list[str]:
    """The columns of *index*'s fields on ``VecDoc`` (``summary_vec`` → ``summary``)."""
    return [VecDoc.to_db_field(field) for field in index.fields]


class TestIndexVector:
    """``Index(..., hnsw=True, ...)``: a vector index — its arguments held as declared and checked when it is built,
    its statement written with every HNSW option, its comparison with what the database stores through the effective
    options."""

    def test_the_attributes_hold_what_was_declared(self) -> None:
        bare = Index("i", "e", hnsw=True, dimension=3)
        assert (bare.hnsw, bare.dimension, bare.dist, bare.vector_type, bare.efc, bare.m) == (
            True,
            3,
            None,
            None,
            None,
            None,
        )
        given = Index("i", "e", hnsw=True, dimension=3, dist="cosine", vector_type="f64", efc=100, m=8)
        assert (given.dist, given.vector_type, given.efc, given.m) == ("COSINE", "F64", 100, 8)
        for dist in ("euclidean", "Cosine", "MANHATTAN", "chebyshev", "Hamming"):
            assert Index("i", "e", hnsw=True, dimension=3, dist=dist).dist == dist.upper()
        for vector_type in ("f64", "F32", "i64", "I32", "i16"):
            assert Index("i", "e", hnsw=True, dimension=3, vector_type=vector_type).vector_type == vector_type.upper()
        plains = (Index("i", "e"), Index("i", "e", hnsw=False), Index("i", "e", unique=True), VecDoc.get_indexes()[2])
        for plain in plains:
            assert plain.hnsw is False
            assert (plain.dimension, plain.dist, plain.vector_type, plain.efc, plain.m) == (None, None, None, None, None)

    def test_equal_and_hashed_by_the_declared_attributes(self) -> None:
        bare = Index("i", "e", hnsw=True, dimension=3)
        spelled = Index("i", "e", hnsw=True, dimension=3, dist="euclidean")
        assert bare != spelled
        assert hash(bare) != hash(spelled)
        assert bare.ddl("t") == spelled.ddl("t") == _BARE
        assert bare._hnsw_options() == spelled._hnsw_options() == (3, "EUCLIDEAN", "F32", 150, 12)
        assert Index("i", "e", hnsw=True, dimension=3, m=8) != Index("i", "e", hnsw=True, dimension=3, m=9)
        same = Index("i", ["e"], hnsw=True, dimension=3, dist="COSINE")
        assert Index("i", "e", hnsw=True, dimension=3, dist="cosine") == same
        assert hash(Index("i", "e", hnsw=True, dimension=3, dist="cosine")) == hash(same)
        assert Index("i", "e", hnsw=True, dimension=3) != Index("i", "e")
        declared: dict[str, Any] = {"dimension": 3, "dist": "cosine", "vector_type": "f64", "efc": 100, "m": 8}
        base = Index("i", "e", hnsw=True, **declared)
        for key, other in (("dimension", 4), ("dist", "manhattan"), ("vector_type", "f32"), ("efc", 101), ("m", 9)):
            assert base != Index("i", "e", hnsw=True, **{**declared, key: other}), key
        assert base != Index("i", "e", hnsw=True, comment="c", **declared)

    def test_repr_shows_the_declared_arguments_only(self) -> None:
        assert repr(Index("i", "e", hnsw=True, dimension=3)) == "Index('i', ('e',), hnsw=True, dimension=3)"
        embedding, summary, _ = VecDoc.get_indexes()
        assert repr(embedding) == "Index('vec_doc_embedding', ('embedding',), hnsw=True, dimension=3, dist='COSINE')"
        assert repr(summary) == (
            "Index('vec_doc_summary', ('summary_vec',), comment=\"it's a summary\", hnsw=True, dimension=3, "
            "vector_type='F64', efc=100, m=8)"
        )
        assert repr(Index("i", "e", hnsw=False)) == "Index('i', ('e',))"

    def test_the_match_arguments_keep_their_seven_positions(self) -> None:
        assert Index.__match_args__[:7] == ("name", "fields", "unique", "search_analyzer", "bm25", "highlights", "comment")
        assert Index.__match_args__[7:] == ("hnsw", "dimension", "dist", "vector_type", "efc", "m")
        assert len(Index.__match_args__) == 13
        match Index("i", "e", unique=True, comment="c"):
            case Index(name, fields, unique, analyzer, bm25, highlights, comment):
                assert (name, fields, unique, analyzer, bm25, highlights, comment) == (
                    "i",
                    ("e",),
                    True,
                    None,
                    None,
                    False,
                    "c",
                )
            case _:
                pytest.fail("a v0.35.0 positional pattern no longer matches")
        match VecDoc.get_indexes()[1]:
            case Index("vec_doc_summary", _, _, _, _, _, comment, True, dimension, dist, vector_type, efc, m):
                assert (comment, dimension, dist, vector_type, efc, m) == ("it's a summary", 3, None, "F64", 100, 8)
            case _:
                pytest.fail("the vector arguments do not match by position")

    def test_frozen_copied_and_picklable(self) -> None:
        index = VecDoc.get_indexes()[1]
        assert not hasattr(index, "__dict__")
        with pytest.raises(AttributeError):
            index.dist = "COSINE"  # type: ignore[misc]
        assert pickle.loads(pickle.dumps(index)) == index
        assert copy.deepcopy(index) == index

    def test_goldens(self) -> None:
        assert VecDoc.get_indexes()[0].ddl("vec_doc") == _H1
        assert _META.ddl("vec_t") == _H5
        assert VecDoc.index_ddl() == _H2
        assert VecDoc.index_ddl(line=2) == _H3
        assert VecDoc.index_ddl(overwrite=False) == _H4

    def test_the_statement_is_the_same_on_both_lines(self) -> None:
        for index in (*VecDoc.get_indexes()[:2], _META, Index("i", "e", hnsw=True, dimension=3)):
            assert index.ddl("t", line=2) == index.ddl("t", line=3) == index.ddl("t")
            assert index.ddl("t", overwrite=False, line=2) == index.ddl("t", overwrite=False, line=3)
        assert Index("i", "e", hnsw=True, dimension=3).ddl("t", overwrite=False) == _BARE.replace("OVERWRITE", "IF NOT EXISTS")
        assert Index("i", "e", hnsw=True, dimension=3, comment="c").ddl("t") == _BARE.replace(";", " COMMENT 'c';")
        assert search.index_statement(VecDoc.get_indexes()[1], "vec_doc", ("summary",), overwrite=True, keyword="SEARCH") == (
            _H_SUMMARY
        )

    def test_the_effective_options(self) -> None:
        embedding, summary, title = VecDoc.get_indexes()
        assert embedding._hnsw_options() == (3, "COSINE", "F32", 150, 12)
        assert summary._hnsw_options() == (3, "EUCLIDEAN", "F64", 100, 8)
        assert _META._hnsw_options() == (2, "MANHATTAN", "F32", 1, 2)
        # The attributes keep what the model declared.
        assert (embedding.vector_type, embedding.efc, embedding.m, summary.dist) == (None, None, None, None)
        for plain in (Index("i", "e"), title):
            assert _raised(ValueError, plain._hnsw_options) == f"Index {plain.name!r} is not a vector index (hnsw=True)."

    @pytest.mark.parametrize("value", [1, "yes", None])
    def test_hnsw_not_a_bool(self, value: object) -> None:
        assert _raised(TypeError, Index, "i", "e", hnsw=value, dimension=3) == (
            f"Index 'i': hnsw= must be a bool, got {type(value).__name__}."
        )

    @pytest.mark.parametrize(
        "arguments",
        [
            {"dimension": 3},
            {"dist": "cosine"},
            {"vector_type": "f32"},
            {"efc": 150},
            {"m": 12},
            {"dimension": 0},
            {"dist": "nope"},
            {"m": 1},
        ],
    )
    def test_the_vector_arguments_need_hnsw(self, arguments: dict[str, Any]) -> None:
        assert _raised(ValueError, Index, "i", "e", **arguments) == _IX11
        assert _raised(ValueError, Index, "i", "e", hnsw=False, **arguments) == _IX11
        assert _raised(ValueError, Index, "i", "e", unique=True, **arguments) == _IX11
        assert _raised(ValueError, Index, "i", "e", search_analyzer="vec_plain", **arguments) == _IX11

    def test_hnsw_needs_a_dimension(self) -> None:
        assert _raised(ValueError, Index, "i", "e", hnsw=True) == _IX12
        assert _raised(ValueError, Index, "i", "e", hnsw=True, dist="cosine", m=8) == _IX12
        assert _raised(ValueError, Index, "i", "e", hnsw=True, dist="nope", efc=0) == _IX12

    @pytest.mark.parametrize(("arg", "low", "high"), [("dimension", 1, 65535), ("efc", 1, 65535), ("m", 2, 127)])
    def test_the_counts(self, arg: str, low: int, high: int) -> None:
        # Built only: an index outside SurrealDB's own ranges is never applied.
        declared: dict[str, Any] = {"dimension": 3}
        for value in (low, high):
            assert getattr(Index("i", "e", hnsw=True, **{**declared, arg: value}), arg) == value
        for value in (low - 1, high + 1, -1):
            assert _raised(ValueError, Index, "i", "e", hnsw=True, **{**declared, arg: value}) == (
                f"Index 'i': {arg}= must be between {low} and {high}, got {value!r}."
            )
        for value, type_name in ((True, "bool"), ("3", "str"), (3.0, "float")):
            assert _raised(TypeError, Index, "i", "e", hnsw=True, **{**declared, arg: value}) == (
                f"Index 'i': {arg}= must be an int, got {type_name}."
            )

    def test_the_distance(self) -> None:
        assert _raised(TypeError, Index, "i", "e", hnsw=True, dimension=3, dist=1) == (
            "Index 'i': dist= must be a str, got int."
        )
        for value in ("nope", "MINKOWSKI 3", ""):
            assert _raised(ValueError, Index, "i", "e", hnsw=True, dimension=3, dist=value) == (
                f"Index 'i': unknown distance {value!r}; the ORM's are EUCLIDEAN, COSINE, MANHATTAN, CHEBYSHEV and HAMMING."
            )
        assert _raised(ValueError, Index, "i", "e", hnsw=True, dimension=3, dist="minkowski") == (
            "Index 'i': MINKOWSKI needs its order, which this version does not write; use EUCLIDEAN (order 2) or "
            "MANHATTAN (order 1)."
        )
        for value in ("jaccard", "PEARSON"):
            assert _raised(ValueError, Index, "i", "e", hnsw=True, dimension=3, dist=value) == (
                f"Index 'i': {value.upper()} is not a distance SurrealDB ranks by: identical vectors are not the "
                "nearest with it. Use COSINE."
            )

    def test_the_storage_type(self) -> None:
        assert _raised(TypeError, Index, "i", "e", hnsw=True, dimension=3, vector_type=32) == (
            "Index 'i': vector_type= must be a str, got int."
        )
        for value in ("F16", "float", ""):
            assert _raised(ValueError, Index, "i", "e", hnsw=True, dimension=3, vector_type=value) == (
                f"Index 'i': vector_type= must be one of F64, F32, I64, I32 and I16, got {value!r}."
            )

    def test_one_field(self) -> None:
        assert _raised(ValueError, Index, "i", ["a", "b.c"], hnsw=True, dimension=3) == (
            "Index 'i': a vector index covers one field (SurrealDB 3.x refuses several); got 2: 'a', 'b.c'. Define "
            "one index per field."
        )

    def test_never_unique(self) -> None:
        assert _raised(ValueError, Index, "i", "e", hnsw=True, dimension=3, unique=True) == _IX17

    @pytest.mark.parametrize(
        "arguments",
        [
            {"search_analyzer": "vec_plain"},
            {"search_analyzer": VEC_PLAIN},
            {"search_analyzer": "vec_plain", "bm25": True},
            {"search_analyzer": VEC_PLAIN, "highlights": True},
            {"search_analyzer": "vec_plain", "bm25": (1.2, 0.75), "highlights": True},
        ],
    )
    def test_never_full_text(self, arguments: dict[str, Any]) -> None:
        assert _raised(ValueError, Index, "i", "e", hnsw=True, dimension=3, **arguments) == _IX18

    def test_the_order_of_the_checks(self) -> None:
        # v0.35.0's checks first, unchanged.
        assert _raised(TypeError, Index, "i", "e", hnsw="x", unique=1) == "Index 'i': unique= must be a bool, got int."
        assert _raised(ValueError, Index, "i", "e", hnsw=True, dimension=3, bm25=True) == (
            "Index 'i': bm25= and highlights= need search_analyzer= (a full-text index)."
        )
        assert _raised(ValueError, Index, "i", ["a", "b"], hnsw=True, dimension=3, search_analyzer="vec_plain") == (
            "Index 'i': a full-text index covers one field (SurrealDB 3.x refuses several); got 2: 'a', 'b'. Define "
            "one index per field."
        )
        # Then hnsw=, the arguments without it, the dimension, the counts in order, the words, the field, UNIQUE.
        assert _raised(TypeError, Index, "i", "e", hnsw=1, dist="nope") == "Index 'i': hnsw= must be a bool, got int."
        assert _raised(ValueError, Index, "i", ["a", "b"], unique=True, dimension=0) == _IX11
        assert _raised(ValueError, Index, "i", "e", hnsw=True, dimension=0, efc=0, m=1) == (
            "Index 'i': dimension= must be between 1 and 65535, got 0."
        )
        assert _raised(ValueError, Index, "i", "e", hnsw=True, dimension=3, efc=0, m=1) == (
            "Index 'i': efc= must be between 1 and 65535, got 0."
        )
        assert _raised(ValueError, Index, "i", "e", hnsw=True, dimension=3, m=1, dist="nope") == (
            "Index 'i': m= must be between 2 and 127, got 1."
        )
        assert _raised(ValueError, Index, "i", "e", hnsw=True, dimension=3, dist="nope", vector_type="F16") == (
            "Index 'i': unknown distance 'nope'; the ORM's are EUCLIDEAN, COSINE, MANHATTAN, CHEBYSHEV and HAMMING."
        )
        assert _raised(ValueError, Index, "i", ["a", "b"], hnsw=True, dimension=3, vector_type="F16") == (
            "Index 'i': vector_type= must be one of F64, F32, I64, I32 and I16, got 'F16'."
        )
        assert _raised(ValueError, Index, "i", ["a", "b"], hnsw=True, dimension=3, unique=True).startswith(
            "Index 'i': a vector index covers one field"
        )

    def test_matches_what_every_engine_renders(self) -> None:
        for index in VecDoc.get_indexes()[:2]:
            assert search.index_matches(index, _doc_columns(index), parse_define_index(_STORED[index.name])), index.name
        assert search.index_matches(_META, ["meta.vec"], parse_define_index(_STORED["vec_meta"]))
        stored = parse_define_index(_STORED_BARE)
        for declared in (
            Index("i", "e", hnsw=True, dimension=3),
            Index("i", "e", hnsw=True, dimension=3, dist="euclidean"),
            Index("i", "e", hnsw=True, dimension=3, dist="EUCLIDEAN", vector_type="f32", efc=150, m=12),
        ):
            assert search.index_matches(declared, ["e"], stored), declared
        # A word written in lower case by hand is read in SurrealDB's spelling.
        hand = parse_define_index("DEFINE INDEX i ON t FIELDS e HNSW DIMENSION 3 DIST cosine TYPE f64 EFC 150 M 12")
        assert search.index_matches(Index("i", "e", hnsw=True, dimension=3, dist="cosine", vector_type="F64"), ["e"], hand)

    @pytest.mark.parametrize(
        ("declared", "stored"),
        [
            # The storage type: F64 stored where F32 is declared, or not declared.
            ({}, _STORED_BARE.replace("TYPE F32", "TYPE F64")),
            ({"vector_type": "f32"}, _STORED_BARE.replace("TYPE F32", "TYPE F64")),
            ({"vector_type": "f64"}, _STORED_BARE),
            ({}, _STORED_BARE.replace("EFC 150", "EFC 100")),
            ({}, _STORED_BARE.replace("M 12 M0 24 LM 0.40242960438184466f", "M 8 M0 16 LM 0.48089834696298783f")),
            ({}, _STORED_BARE.replace("DIMENSION 3", "DIMENSION 4")),
            ({}, _STORED_BARE.replace("DIST EUCLIDEAN", "DIST COSINE")),
            ({"dist": "cosine"}, _STORED_BARE),
            ({}, _STORED_BARE + ' COMMENT "c"'),
            ({"comment": "c"}, _STORED_BARE),
            ({"comment": "c"}, _STORED_BARE + ' COMMENT "d"'),
            ({}, _STORED_BARE.replace("FIELDS e", "FIELDS f")),
            (
                {},
                "DEFINE INDEX i ON t FIELDS e MTREE DIMENSION 3 DIST EUCLIDEAN TYPE F32 CAPACITY 40 DOC_IDS_ORDER 100 "
                "DOC_IDS_CACHE 100 MTREE_CACHE 100",
            ),
            ({}, "DEFINE INDEX i ON t FIELDS e UNIQUE"),
            ({}, "DEFINE INDEX i ON t FIELDS e"),
            ({}, "DEFINE INDEX i ON t FIELDS e FULLTEXT ANALYZER vec_plain BM25(1.2,0.75)"),
            ({}, "DEFINE INDEX i ON t COUNT"),
        ],
    )
    def test_anything_else_does_not_match(self, declared: dict[str, Any], stored: str) -> None:
        index = Index("i", "e", hnsw=True, dimension=3, **declared)
        assert not search.index_matches(index, ["e"], parse_define_index(stored))

    def test_an_incomplete_or_missing_definition_does_not_match(self) -> None:
        index = Index("i", "e", hnsw=True, dimension=3)
        assert not search.index_matches(index, ["e"], None)
        options: dict[str, Any] = {"dimension": 3, "dist": "EUCLIDEAN", "vector_type": "F32", "efc": 150, "m": 12}
        assert search.index_matches(index, ["e"], IndexState("i", ("e",), hnsw=True, **options))
        assert not search.index_matches(index, ["e"], IndexState("i", ("e",), unique=True, hnsw=True, **options))
        assert not search.index_matches(index, ["e"], IndexState("i", ("e",), hnsw=True, mtree=True, **options))
        for key in options:
            assert not search.index_matches(index, ["e"], IndexState("i", ("e",), hnsw=True, **{**options, key: None}))
        # A plain index declared never matches a stored vector one (v0.35.0's rule, kept).
        assert not search.index_matches(Index("i", "e"), ["e"], parse_define_index(_STORED_BARE))


# === The indexes key: the vector checks at the class statement ======================================================

#: ``VectorField[3, "F64"]`` and ``VectorField[4]`` as names (a string in an annotation reads as a forward reference).
_WIDE: Any = VectorField[3, "F64"]
_FOUR: Any = VectorField[4]

_IC4 = (
    "VecBad: two full-text indexes cover column {!r} ({!r}, {!r}); SurrealDB would use either for search(), with "
    "different scores. Keep one."
)
_IC5 = (
    "VecBad: model_config['indexes'] holds two definitions of analyzer {!r}; declare one Analyzer object and reference "
    "it from every index."
)
_IC7 = "{}: two vector indexes cover column {!r} ({!r}, {!r}); similar_to() would use either. Keep one."
_IC8 = "{}: vector index {!r} has dimension {} and type {}, but field {!r} is VectorField[{}, {!r}]. Make them equal."


def _vec_bad(indexes: object) -> type[BaseSurrealModel]:
    """A model ``VecBad`` with ``indexes=`` *indexes*, built by its ``class`` statement: a vector field of each spelling
    and storage type (one aliased), a list of floats and an ``Any`` field, and two text fields."""

    class VecBad(BaseSurrealModel):
        model_config = SurrealConfigDict(table_name="vec_bad", indexes=indexes)  # type: ignore[typeddict-item]

        id: str | None = None
        title: str = ""
        body: str = ""
        embedding: VectorField[3]
        wide: _WIDE
        typed: Annotated[list[float] | None, Vector(3, "F64")] = None
        aliased: Annotated[list[float] | None, Vector(3)] = Field(default=None, alias="aliased_vec")
        plain: list[float] | None = None
        loose: Any = None

    return VecBad


def _hnsw(name: str, field: str, dimension: int = 3, **options: Any) -> Index:
    """A vector index *name* on *field*."""
    return Index(name, field, hnsw=True, dimension=dimension, **options)


def _vec_union_bad(indexes: object) -> type[BaseSurrealModel]:
    """A model ``VecUnionBad`` with ``indexes=`` *indexes*, built by its ``class`` statement: a vector field of each
    union spelling (issue #318), one aliased."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UnsupportedFieldAttributeWarning)

        class VecUnionBad(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="vec_union_bad", indexes=indexes)  # type: ignore[typeddict-item]

            id: str | None = None
            optional: VectorField[3] | None = None
            legacy: Optional[VectorField[3]] = None  # noqa: UP045 - the spelling under test
            typed: Annotated[list[float], Vector(3)] | None = None
            wide: Annotated[list[float], Vector(3, "F64")] | None = None
            aliased: Annotated[list[float], Vector(3)] | None = Field(default=None, alias="aliased_vec")

    return VecUnionBad


class TestClassChecks:
    """``SurrealConfigDict(indexes=[...])`` with vector indexes, checked at the ``class`` statement after v0.35.0's
    checks: one vector index per column (an aliased field counted under its column), and a vector index on a vector
    field of the same dimension and effective storage type (``F32`` when the index declares none), whichever spelling
    declares the field — a union with ``None`` included. A vector index on a field that is not a vector field is the
    application's choice. Before the indexes, on every model, a field whose ``Vector`` markers are ambiguous is
    refused, naming it."""

    def test_the_golden_model_is_accepted(self) -> None:
        assert [index.name for index in VecDoc.get_indexes()] == ["vec_doc_embedding", "vec_doc_summary", "vec_doc_title"]

    @pytest.mark.parametrize(
        ("indexes", "text"),
        [
            (
                [_hnsw("vec_a", "embedding"), _hnsw("vec_b", "embedding", dist="cosine")],
                _IC7.format("VecBad", "embedding", "vec_a", "vec_b"),
            ),
            (
                [_hnsw("vec_a", "aliased"), _hnsw("vec_b", "aliased_vec", dist="cosine")],
                _IC7.format("VecBad", "aliased_vec", "vec_a", "vec_b"),
            ),
            (
                [_hnsw("vec_a", "plain"), Index("vec_s", "plain"), _hnsw("vec_b", "plain", m=8)],
                _IC7.format("VecBad", "plain", "vec_a", "vec_b"),
            ),
            (
                [_hnsw("vec_a", "embedding", dimension=4)],
                _IC8.format("VecBad", "vec_a", 4, "F32", "embedding", 3, "F32"),
            ),
            (
                [_hnsw("vec_a", "embedding", vector_type="f64")],
                _IC8.format("VecBad", "vec_a", 3, "F64", "embedding", 3, "F32"),
            ),
            (
                [_hnsw("vec_a", "typed", vector_type="F32")],
                _IC8.format("VecBad", "vec_a", 3, "F32", "typed", 3, "F64"),
            ),
            (
                [_hnsw("vec_a", "wide")],
                _IC8.format("VecBad", "vec_a", 3, "F32", "wide", 3, "F64"),
            ),
            (
                [_hnsw("vec_a", "aliased_vec", dimension=2)],
                _IC8.format("VecBad", "vec_a", 2, "F32", "aliased_vec", 3, "F32"),
            ),
            (
                [_hnsw("vec_a", "aliased", vector_type="i16")],
                _IC8.format("VecBad", "vec_a", 3, "I16", "aliased", 3, "F32"),
            ),
        ],
        ids=[
            "ic7",
            "ic7-alias",
            "ic7-not-a-vector-field",
            "ic8-dimension",
            "ic8-type",
            "ic8-typed-form",
            "ic8-effective-type",
            "ic8-alias-column",
            "ic8-alias-name",
        ],
    )
    def test_refused_at_the_class_statement(self, indexes: list[Index], text: str) -> None:
        assert _raised(ValueError, _vec_bad, indexes) == text

    @pytest.mark.parametrize(
        ("indexes", "text"),
        [
            # IC4 before IC7: two vector indexes on one column, then two full-text indexes on another.
            (
                [
                    _hnsw("vec_a", "embedding"),
                    _hnsw("vec_b", "embedding"),
                    Index("vec_t1", "title", search_analyzer=VEC_PLAIN),
                    Index("vec_t2", "title", search_analyzer=VEC_PLAIN),
                ],
                _IC4.format("title", "vec_t1", "vec_t2"),
            ),
            # IC5 before IC7: two vector indexes on one column, then two definitions of one analyzer name.
            (
                [
                    _hnsw("vec_a", "embedding"),
                    _hnsw("vec_b", "embedding"),
                    Index("vec_t", "title", search_analyzer=Analyzer("vec_plain", ["blank"])),
                    Index("vec_u", "body", search_analyzer=Analyzer("vec_plain", ["punct"])),
                ],
                _IC5.format("vec_plain"),
            ),
            # IC7 before IC8: the second vector index on the column is also of another dimension.
            (
                [_hnsw("vec_a", "embedding", dimension=4), _hnsw("vec_b", "embedding", dimension=5)],
                _IC7.format("VecBad", "embedding", "vec_a", "vec_b"),
            ),
            # IC7 on one column before IC8 on an earlier one.
            (
                [_hnsw("vec_a", "typed"), _hnsw("vec_b", "plain"), _hnsw("vec_c", "plain")],
                _IC7.format("VecBad", "plain", "vec_b", "vec_c"),
            ),
            # IC8: the first faulty index in declaration order.
            (
                [_hnsw("vec_a", "embedding"), _hnsw("vec_b", "typed", dimension=2), _hnsw("vec_c", "wide", dimension=4)],
                _IC8.format("VecBad", "vec_b", 2, "F32", "typed", 3, "F64"),
            ),
        ],
        ids=["ic4-before-ic7", "ic5-before-ic7", "ic7-before-ic8", "ic7-before-ic8-elsewhere", "ic8-first-index"],
    )
    def test_the_first_fault_raises(self, indexes: list[Index], text: str) -> None:
        assert _raised(ValueError, _vec_bad, indexes) == text

    def test_matching_vector_indexes_accepted(self) -> None:
        indexes = [
            _hnsw("vec_a", "embedding", dist="cosine"),
            _hnsw("vec_b", "wide", vector_type="f64"),
            _hnsw("vec_c", "typed", vector_type="F64", efc=100, m=8),
            _hnsw("vec_d", "aliased_vec", vector_type="f32"),
            Index("vec_e", "embedding"),
            Index("vec_t", "title", search_analyzer=VEC_PLAIN),
        ]
        assert _vec_bad(indexes).get_indexes() == tuple(indexes)

    def test_a_vector_index_on_a_field_that_is_not_a_vector_field_accepted(self) -> None:
        indexes = [_hnsw("vec_a", "plain", dimension=8, vector_type="i64"), _hnsw("vec_b", "loose", dimension=2)]
        assert _vec_bad(indexes).get_indexes() == tuple(indexes)

    def test_an_inherited_index_is_checked_again_naming_the_subclass(self) -> None:
        with pytest.raises(ValueError) as raised:

            class VecDocWide(VecDoc):
                model_config = SurrealConfigDict(table_name="vec_doc_wide")

                embedding: _FOUR

        assert str(raised.value) == _IC8.format("VecDocWide", "vec_doc_embedding", 3, "F32", "embedding", 4, "F32")

    @pytest.mark.parametrize(
        ("index", "text"),
        [
            (_hnsw("vec_a", "optional", dimension=4), _IC8.format("VecUnionBad", "vec_a", 4, "F32", "optional", 3, "F32")),
            (
                _hnsw("vec_a", "optional", vector_type="f64"),
                _IC8.format("VecUnionBad", "vec_a", 3, "F64", "optional", 3, "F32"),
            ),
            (_hnsw("vec_a", "legacy", dimension=4), _IC8.format("VecUnionBad", "vec_a", 4, "F32", "legacy", 3, "F32")),
            (
                _hnsw("vec_a", "legacy", vector_type="i16"),
                _IC8.format("VecUnionBad", "vec_a", 3, "I16", "legacy", 3, "F32"),
            ),
            (_hnsw("vec_a", "typed", dimension=2), _IC8.format("VecUnionBad", "vec_a", 2, "F32", "typed", 3, "F32")),
            (
                _hnsw("vec_a", "typed", vector_type="F64"),
                _IC8.format("VecUnionBad", "vec_a", 3, "F64", "typed", 3, "F32"),
            ),
            (_hnsw("vec_a", "wide"), _IC8.format("VecUnionBad", "vec_a", 3, "F32", "wide", 3, "F64")),
            (
                _hnsw("vec_a", "wide", dimension=4, vector_type="f64"),
                _IC8.format("VecUnionBad", "vec_a", 4, "F64", "wide", 3, "F64"),
            ),
            (
                _hnsw("vec_a", "aliased_vec", dimension=4),
                _IC8.format("VecUnionBad", "vec_a", 4, "F32", "aliased_vec", 3, "F32"),
            ),
            (
                _hnsw("vec_a", "aliased", vector_type="i32"),
                _IC8.format("VecUnionBad", "vec_a", 3, "I32", "aliased", 3, "F32"),
            ),
        ],
        ids=[
            "or-none-dimension",
            "or-none-type",
            "optional-dimension",
            "optional-type",
            "typed-dimension",
            "typed-type",
            "typed-f64-effective-type",
            "typed-f64-dimension",
            "alias-column",
            "alias-name",
        ],
    )
    def test_ic8_on_each_union_spelling(self, index: Index, text: str) -> None:
        """The same refusal, word for word, as for ``VectorField[3]`` (issue #318)."""
        assert _raised(ValueError, _vec_union_bad, [index]) == text

    def test_matching_vector_indexes_on_union_spellings_accepted(self) -> None:
        indexes = [
            _hnsw("vec_a", "optional", dist="cosine"),
            _hnsw("vec_b", "legacy"),
            _hnsw("vec_c", "typed", vector_type="f32"),
            _hnsw("vec_d", "wide", vector_type="F64", efc=100, m=8),
            _hnsw("vec_e", "aliased_vec"),
        ]
        assert _vec_union_bad(indexes).get_indexes() == tuple(indexes)

    def test_an_inherited_index_is_checked_against_a_union_spelling(self) -> None:
        with pytest.raises(ValueError) as raised, warnings.catch_warnings():
            warnings.simplefilter("ignore", UnsupportedFieldAttributeWarning)

            class VecDocUnion(VecDoc):
                model_config = SurrealConfigDict(table_name="vec_doc_union")

                embedding: _FOUR | None = None

        assert str(raised.value) == _IC8.format("VecDocUnion", "vec_doc_embedding", 3, "F32", "embedding", 4, "F32")

    @pytest.mark.parametrize(("annotation", "held"), _AMBIGUOUS_ANNOTATIONS, ids=_AMBIGUOUS_IDS)
    def test_an_ambiguous_vector_field_is_refused_without_indexes(self, annotation: Any, held: str) -> None:
        text = _raised(ValueError, _model_of, "VecAmbiguous", id=(str | None, None), mixed=(annotation, None))
        assert text == _AMBIGUOUS.format("VecAmbiguous: field 'mixed'", held)

    def test_an_ambiguous_vector_field_is_refused_before_the_indexes(self) -> None:
        with pytest.raises(ValueError) as raised, warnings.catch_warnings():
            warnings.simplefilter("ignore", UnsupportedFieldAttributeWarning)

            class VecMixed(BaseSurrealModel):
                model_config = SurrealConfigDict(
                    table_name="vec_mixed", indexes=[_hnsw("vec_a", "mixed", dimension=4), _hnsw("vec_b", "mixed")]
                )

                id: str | None = None
                mixed: _MIXED = None

        assert str(raised.value) == _AMBIGUOUS.format("VecMixed: field 'mixed'", f"Vector(3, 'F32') {_NEXT_TO}")

    def test_a_field_redeclared_ambiguous_is_refused_naming_the_subclass(self) -> None:
        with pytest.raises(ValueError) as raised, warnings.catch_warnings():
            warnings.simplefilter("ignore", UnsupportedFieldAttributeWarning)

            class VecFieldMixed(VecField):
                model_config = SurrealConfigDict(table_name="vec_field_mixed")

                typed: _MIXED = None

        assert str(raised.value) == _AMBIGUOUS.format("VecFieldMixed: field 'typed'", f"Vector(3, 'F32') {_NEXT_TO}")


# === define_table(): the vector columns =============================================================================

#: ``VecDoc.table_ddl()``: the two vector columns (the typed one under its alias) written ``option<array<float, 3>>``.
_D1 = [
    "DEFINE TABLE OVERWRITE `vec_doc` TYPE NORMAL;",
    "DEFINE FIELD OVERWRITE `title` ON `vec_doc` TYPE string;",
    "DEFINE FIELD OVERWRITE `category` ON `vec_doc` TYPE string DEFAULT '';",
    "DEFINE FIELD OVERWRITE `n` ON `vec_doc` TYPE int DEFAULT 0;",
    "DEFINE FIELD OVERWRITE `embedding` ON `vec_doc` TYPE option<array<float, 3>>;",
    "DEFINE FIELD OVERWRITE `summary` ON `vec_doc` TYPE option<array<float, 3>>;",
    "DEFINE FIELD OVERWRITE `loc` ON `vec_doc` TYPE option<geometry<point>>;",
]


class TestFieldKind:
    """``model_ddl.field_kind()``: a vector field, either spelling, is ``option<array<float, d>>`` — the marker accepts
    ``None`` —, whatever its storage type; ``table_ddl()`` writes it."""

    @pytest.mark.parametrize(
        ("annotation", "text"),
        [
            (VectorField[3], "option<array<float, 3>>"),
            (VectorField[384, "i16"], "option<array<float, 384>>"),
            (Annotated[list[float] | None, Vector(3)], "option<array<float, 3>>"),
            (Annotated[list[float] | None, Vector(65535, "F64")], "option<array<float, 65535>>"),
            (Annotated[list[float], Vector(2)], "option<array<float, 2>>"),
            (list[VectorField[3]], "array<option<array<float, 3>>>"),
            (list[float] | None, "option<array<float>>"),
        ],
        ids=["vector-field", "vector-field-i16", "typed", "typed-f64", "typed-not-optional", "list-of-vectors", "plain"],
    )
    def test_the_kind_of_an_annotation(self, annotation: Any, text: str) -> None:
        assert render_kind(model_ddl.field_kind(annotation)) == text

    @pytest.mark.parametrize("name", ["embedding", "typed", "aliased"])
    def test_the_kind_of_a_model_field(self, name: str) -> None:
        info = VecField.model_fields[name]
        assert render_kind(model_ddl.field_kind(info.annotation, info.metadata)) == "option<array<float, 3>>"

    def test_the_table_statements(self) -> None:
        assert VecDoc.table_ddl() == _D1
        assert VecField.table_ddl()[1:] == [
            f"DEFINE FIELD OVERWRITE `{column}` ON `vec_t` TYPE option<array<float, 3>>;"
            for column in ("embedding", "typed", "aliased_vec")
        ]

    def test_the_union_spellings_write_the_same_column(self) -> None:
        """The union spellings (issue #318) are written as the plain ones, and a container of vectors as before."""
        assert VecUnion.table_ddl()[1:] == [
            *(
                f"DEFINE FIELD OVERWRITE `{column}` ON `vec_union` TYPE option<array<float, 3>>;"
                for column in ("optional", "legacy", "typed", "wide", "aliased_vec", "noted")
            ),
            "DEFINE FIELD OVERWRITE `listed` ON `vec_union` TYPE array<option<array<float, 3>>>;",
            "DEFINE FIELD OVERWRITE `keyed` ON `vec_union` TYPE object;",
            "DEFINE FIELD OVERWRITE `maybe_listed` ON `vec_union` TYPE option<array<option<array<float, 3>>>>;",
        ]


# === E2E: the connection "vec" on the database vec_db, over WebSocket and HTTP, and mem:// =========================

#: The engines every E2E class runs on: the line under test over WebSocket and HTTP, and the embedded engine.
_KINDS = ["ws", "http", "mem"]

#: The reset before each E2E test, in one query, of the database ``vec_db`` (or of another database of these tests).
_VEC_RESET = "REMOVE DATABASE IF EXISTS {0}; DEFINE DATABASE {0};"


def _url(kind: str) -> str:
    """The URL of *kind*: ``ws`` or ``http`` on the line under test (``SURREALDB_PORT``), or ``mem``."""
    if kind == "mem":
        return "mem://"
    host = os.environ.get("SURREALDB_HOST", "localhost")
    port = os.environ.get("SURREALDB_PORT", "8000")
    return f"ws://{host}:{port}/rpc" if kind == "ws" else f"http://{host}:{port}"


async def _apply_ddl(client: Any, ddl: str) -> None:
    """Run *ddl* and check every statement: ``query()`` checks only the first one."""
    response = await client.query_raw(ddl, {})
    assert "error" not in response, response["error"]
    refused = [result for result in response["result"] if result["status"] != "OK"]
    assert response["result"] and not refused, refused


@contextlib.asynccontextmanager
async def _vec(kind: str, name: str = "vec", database: str = "vec_db") -> AsyncIterator[Any]:
    """The connection *name* (``"vec"``) on *kind*, its database *database* (``vec_db``, a constant of these tests)
    reset in one query; removed, then unregistered, on exit."""
    if kind == "mem":
        SurrealDBConnectionManager.add_connection(name, url="mem://", namespace="ns", database=database)
    else:
        SurrealDBConnectionManager.add_connection(
            name, url=_url(kind), user="root", password="root", namespace="ns", database=database
        )
    client = await SurrealDBConnectionManager.get_client(name)
    await _apply_ddl(client, _VEC_RESET.format(database))
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            cleanup = await SurrealDBConnectionManager.get_client(name)
            await cleanup.query(f"REMOVE DATABASE IF EXISTS {database};", {})
        await SurrealDBConnectionManager.remove_connection(name)


async def _line(client: Any) -> tuple[int, int]:
    """``(major, minor)`` of the engine *client* talks to: ``"surrealdb-3.1.5"`` → ``(3, 1)``; ``mem://`` →
    ``(2, 0)`` (the SDK's version, whose core is 2.x)."""
    match = re.search(r"(\d+)\.(\d+)", await client.version())
    assert match is not None
    return int(match.group(1)), int(match.group(2))


async def _major(client: Any) -> int:
    """The major version of the engine *client* talks to: ``"surrealdb-3.3.4"`` → 3; ``mem://`` → 2."""
    return (await _line(client))[0]


async def _keyword_line(client: Any) -> Literal[2, 3]:
    """The ``line=`` of the renderers for the engine *client* talks to: 3 from SurrealDB 3.x on, else 2."""
    return 3 if await _major(client) >= 3 else 2


async def _table_info(client: Any, table: str) -> dict[str, Any]:
    """``INFO FOR TABLE`` *table* (a constant name of these tests)."""
    info: dict[str, Any] = await client.query(f"INFO FOR TABLE {table};", {})
    return info


@contextlib.asynccontextmanager
async def _vec_models(kind: str) -> AsyncIterator[Any]:
    """:func:`_vec`, the models routed to the connection ``"vec"`` for the block (``using("vec")``)."""
    async with _vec(kind) as client, SurrealDBConnectionManager.using("vec"):
        yield client


def _spy(monkeypatch: pytest.MonkeyPatch, client: Any) -> list[tuple[str, tuple[object, ...]]]:
    """Record every ``query``, ``query_raw`` and ``version`` call *client* receives, with its arguments (and let it
    through) — v0.31.0's spy."""
    calls: list[tuple[str, tuple[object, ...]]] = []

    def recording(method: str) -> Any:
        real = getattr(client, method)

        async def call(*args: object, **kwargs: object) -> object:
            calls.append((method, args))
            return await real(*args, **kwargs)

        return call

    for method in ("query", "query_raw", "version"):
        monkeypatch.setattr(client, method, recording(method))
    return calls


def _calls(calls: list[tuple[str, tuple[object, ...]]]) -> list[tuple[str, object]]:
    """What *calls* (:func:`_spy`'s) sent: each method with its first argument (``None`` for ``version()``)."""
    return [(method, args[0] if args else None) for method, args in calls]


def _query(statement: str) -> list[tuple[str, object]]:
    """The calls of one ``client.query(statement, {})``: the SDK's ``query()`` sends it through ``query_raw()``."""
    return [("query", statement), ("query_raw", statement)]


def _sent(statements: Sequence[str]) -> list[tuple[str, object]]:
    """The calls of ``client.query(statement, {})`` for each of *statements*, in order."""
    return [call for statement in statements for call in _query(statement)]


def _field_states(info: Mapping[str, Any]) -> dict[tuple[str, ...], FieldState]:
    """The fields of an ``INFO FOR TABLE`` answer, each parsed, keyed by path (an item field ``e.*`` or ``e[*]`` under
    ``("e", "*")``)."""
    states = [parse_define_field(text).state for text in info["fields"].values()]
    return {tuple(state.path): state for state in states}


async def _refused(client: Any, statement: str) -> bool:
    """Whether the engine refuses *statement* (one statement, a constant of these tests)."""
    response = await client.query_raw(statement, {})
    assert "error" not in response, response["error"]
    return [result["status"] for result in response["result"]] != ["OK"]


class TestIndexVectorE2E:
    """The vector indexes' statements applied on every engine: what ``INFO FOR TABLE`` returns matches each
    declaration — the embedded engine stores the ``TYPE`` written, as the servers do —, and the introspector reads the
    options the ORM wrote."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_statements_are_applied_and_match(self, kind: str) -> None:
        bare = Index("vec_bare", "e", hnsw=True, dimension=3)
        async with _vec(kind) as client:
            line = await _keyword_line(client)
            statements = VecDoc.index_ddl(line=line)
            assert statements == (_H2 if line == 3 else _H3)
            for statement in [*statements, _H5, bare.ddl("vec_t")]:
                await _apply_ddl(client, statement)
            doc_info = await _table_info(client, "vec_doc")
            t_info = await _table_info(client, "vec_t")
        doc = {name: parse_define_index(text) for name, text in doc_info["indexes"].items()}
        assert sorted(doc) == ["vec_doc_embedding", "vec_doc_summary", "vec_doc_title"]
        for index in VecDoc.get_indexes():
            assert search.index_matches(index, _doc_columns(index), doc[index.name]), index.name
        assert (doc["vec_doc_embedding"].vector_type, doc["vec_doc_summary"].vector_type) == ("F32", "F64")
        stored = {name: parse_define_index(text) for name, text in t_info["indexes"].items()}
        assert sorted(stored) == ["vec_bare", "vec_meta"]
        assert search.index_matches(_META, ["meta.vec"], stored["vec_meta"])
        for declared in (bare, Index("vec_bare", "e", hnsw=True, dimension=3, dist="euclidean")):
            assert search.index_matches(declared, ["e"], stored["vec_bare"]), declared
        assert not search.index_matches(
            Index("vec_bare", "e", hnsw=True, dimension=3, vector_type="f64"), ["e"], stored["vec_bare"]
        )

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_introspector_reads_the_options_written(self, kind: str) -> None:
        async with _vec(kind) as client:
            for statement in VecDoc.index_ddl(line=await _keyword_line(client)):
                await _apply_ddl(client, statement)
            schema = await DatabaseIntrospector("vec", tables=["vec_doc"]).introspect()
        assert schema.issues == ()
        indexes = schema.tables["vec_doc"].indexes
        assert sorted(indexes) == ["vec_doc_embedding", "vec_doc_summary", "vec_doc_title"]
        for index in VecDoc.get_indexes()[:2]:
            read = indexes[index.name]
            assert read.fields == tuple(_doc_columns(index)), index.name
            assert (read.hnsw, read.dimension, read.dist, read.vector_type, read.efc, read.m) == (
                True,
                *index._hnsw_options(),
            ), index.name
            assert (read.mtree, read.unique, read.comment) == (False, False, index.comment), index.name


_INFO_DB = "INFO FOR DB;"
_INFO_DOC = "INFO FOR TABLE `vec_doc`;"


class TestDefineTableE2E:
    """``define_table()`` on every engine: the vector columns — both spellings, the typed one under its alias — are
    defined ``option<array<float, 3>>`` and read back as written (SurrealDB 3.x renders ``none | array<float, 3>`` with
    an item field ``e.*``, 2.x and the embedded engine ``option<array<float, 3>>`` with ``e[*]``), so a second call
    sends the same statements and changes nothing. A record's integers are stored as floats; a vector longer than the
    dimension is refused by the field, and one of another length once the vector indexes are applied."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_vector_columns_read_back_unchanged(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _vec_models(kind) as client:
            calls = _spy(monkeypatch, client)
            result = await VecDoc.define_table()
            assert result == "\n".join(_D1)
            assert _calls(calls) == _sent([_INFO_DB, *_D1])
            info = await _table_info(client, "vec_doc")
            stored = _field_states(info)
            changed = [
                column.path
                for column in VecDoc._table_columns()
                if (stored[column.path].field_type, stored[column.path].nullable) != normalise_kind(render_kind(column.kind))
            ]
            assert changed == []
            for column in ("embedding", "summary"):
                vector_field = stored[(column,)]
                assert (vector_field.field_type, vector_field.nullable) == ("array<float, 3>", True), column
                item = stored[(column, "*")]
                assert (item.field_type, item.nullable) == ("float", False), column

            calls.clear()
            assert await VecDoc.define_table() == result
            assert _calls(calls) == _sent([_INFO_DB, _INFO_DOC, *_D1])
            assert await _table_info(client, "vec_doc") == info

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_values_stored_as_floats_and_a_wrong_length_refused(self, kind: str) -> None:
        async with _vec_models(kind) as client:
            await VecDoc.define_table()
            await _apply_ddl(
                client, "CREATE vec_doc:d1 SET title = 'Cats and dogs', embedding = [1, 0, 0], summary = [0, 1, 0];"
            )
            rows = await client.query("SELECT embedding, summary FROM vec_doc:d1;", {})
            assert rows == [{"embedding": [1.0, 0.0, 0.0], "summary": [0.0, 1.0, 0.0]}]
            assert {type(item) for row in rows for value in row.values() for item in value} == {float}
            doc = await VecDoc.objects().get("d1")
            assert (doc.embedding, doc.summary_vec) == ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0])

            assert await _refused(client, "CREATE vec_doc:d2 SET title = 'Long', embedding = [1.0, 0.0, 0.0, 0.0];")
            assert await _refused(client, "CREATE vec_doc:d3 SET title = 'Long', summary = [1.0, 0.0, 0.0, 0.0];")
            for statement in VecDoc.index_ddl(line=await _keyword_line(client)):
                await _apply_ddl(client, statement)
            assert await _refused(client, "CREATE vec_doc:d4 SET title = 'Short', embedding = [1.0, 0.0];")
            assert await _refused(client, "CREATE vec_doc:d5 SET title = 'Short', summary = [1.0, 0.0];")
            assert await client.query("SELECT VALUE record::id(id) FROM vec_doc;", {}) == ["d1"]


# === define_indexes() and remove_index(): the vector indexes on every engine ========================================


class VecDocEuclidean(VecDoc):
    """``VecDoc`` on the same table, ``vec_doc_embedding`` redeclared with another metric."""

    model_config = SurrealConfigDict(
        table_name="vec_doc",
        indexes=[
            Index("vec_doc_embedding", "embedding", hnsw=True, dimension=3, dist="euclidean"),
            *VecDoc.get_indexes()[1:],
        ],
    )


class VecOnly(BaseSurrealModel):
    """Vector indexes only (no full-text index): ``define_indexes()`` reads no line. Its index declares the dimension
    alone."""

    model_config = SurrealConfigDict(
        table_name="vec_only", indexes=[Index("vec_only_embedding", "embedding", hnsw=True, dimension=3)]
    )

    id: str | None = None
    embedding: VectorField[3]


class VecOnlyEuclidean(VecOnly):
    """``VecOnly`` on the same table, its index spelling out SurrealDB's default metric."""

    model_config = SurrealConfigDict(
        table_name="vec_only",
        indexes=[Index("vec_only_embedding", "embedding", hnsw=True, dimension=3, dist="euclidean")],
    )


_INFO_ONLY = "INFO FOR TABLE `vec_only`;"

#: ``VecOnly``'s statement — and ``VecOnlyEuclidean``'s: every option written, SurrealDB's defaults included.
_ONLY = (
    "DEFINE INDEX OVERWRITE `vec_only_embedding` ON `vec_only` FIELDS `embedding` HNSW DIMENSION 3 DIST EUCLIDEAN TYPE "
    "F32 EFC 150 M 12;"
)

#: ``VecDocEuclidean``'s ``vec_doc_embedding``.
_H1_EUCLIDEAN = (
    "DEFINE INDEX OVERWRITE `vec_doc_embedding` ON `vec_doc` FIELDS `embedding` HNSW DIMENSION 3 DIST EUCLIDEAN TYPE "
    "F32 EFC 150 M 12;"
)

#: The reads of ``VecDoc.define_indexes()``: ``INFO FOR DB``, the line (a full-text index is declared), ``INFO FOR
#: TABLE``.
_DOC_READS = [*_query(_INFO_DB), ("version", None), *_query(_INFO_DOC)]

#: The reads of ``VecOnly.define_indexes()``: no line read.
_ONLY_READS = _sent([_INFO_DB, _INFO_ONLY])

#: ``index_ddl()``'s statements by ``(overwrite, line)``.
_INDEX_DDL = {
    (True, 3): _H2,
    (True, 2): _H3,
    (False, 3): _H4,
    (False, 2): [statement.replace("FULLTEXT", "SEARCH") for statement in _H4],
}

#: An ``MTREE`` index under ``VecOnly``'s index name (SurrealDB 2.x and the embedded engine accept it).
_MTREE_ONLY = "DEFINE INDEX vec_only_embedding ON vec_only FIELDS embedding MTREE DIMENSION 3;"

#: A plain index under ``VecOnly``'s index name.
_PLAIN_ONLY = "DEFINE INDEX vec_only_embedding ON vec_only FIELDS embedding;"


async def _indexes(client: Any, table: str) -> dict[str, IndexState | None]:
    """The indexes of *table*, as ``read_table_indexes()`` reads them."""
    return await search.read_table_indexes(client, table, what="index", kind="definition")


async def _refusals(client: Any, query: str) -> list[str]:
    """Run *query* raw: the text of every statement SurrealDB refused, ``[]`` when it accepts them all (a parse error
    refuses the whole query; the embedded engine raises it)."""
    try:
        response = await client.query_raw(query, {})
    except RuntimeError as error:
        return [str(error)]
    if "error" in response:
        return [str(response["error"])]
    return [str(result["result"]) for result in response["result"] if result["status"] != "OK"]


class _Rollback(Exception):
    """Raised inside a ``transaction()`` block to roll it back."""


class TestDefineVectorIndexesE2E:
    """``define_indexes()`` with vector indexes on every engine (the line under test over WebSocket and HTTP, and the
    embedded engine): the first call applies the analyzer and every index, the full-text one with the line's keyword;
    the second sends the reads alone and applies nothing — a vector index equal to the stored one is never rebuilt;
    one changed option redefines that index only (``overwrite=False`` keeps it); a declaration without ``dist`` and the
    same with ``dist="euclidean"`` each match the index the other defined; a model with vector indexes only reads no
    line; an index of another kind stored under the name (an ``MTREE`` one on SurrealDB 2.x and the embedded engine, a
    plain one) is redefined ``HNSW``; ``remove_index()`` removes a vector index by name; with a transaction the vector
    index joins the block."""

    @pytest.mark.parametrize("overwrite", [True, False])
    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_fresh_database_then_nothing_changed(
        self, kind: str, overwrite: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with _vec_models(kind) as client:
            line = await _keyword_line(client)
            await VecDoc.define_table()
            calls = _spy(monkeypatch, client)
            applied = await VecDoc.define_indexes(overwrite=overwrite)
            first = _calls(calls)
            calls.clear()
            again = await VecDoc.define_indexes(overwrite=overwrite)
            second = _calls(calls)
            monkeypatch.undo()
            stored = await _indexes(client, "vec_doc")
        expected = _INDEX_DDL[(overwrite, line)]
        assert applied == expected
        assert applied == VecDoc.index_ddl(overwrite=overwrite, line=line)
        assert first == [*_DOC_READS, *_sent(expected)]
        assert again == []
        assert second == _DOC_READS
        assert sorted(stored) == ["vec_doc_embedding", "vec_doc_summary", "vec_doc_title"]
        for index in VecDoc.get_indexes():
            assert search.index_matches(index, _doc_columns(index), stored[index.name]), index.name

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_only_a_changed_vector_index_is_redefined(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _vec_models(kind) as client:
            await VecDoc.define_table()
            await VecDoc.define_indexes()
            calls = _spy(monkeypatch, client)
            kept = await VecDocEuclidean.define_indexes(overwrite=False)
            kept_sent = _calls(calls)
            monkeypatch.undo()
            kept_state = (await _indexes(client, "vec_doc"))["vec_doc_embedding"]
            calls = _spy(monkeypatch, client)
            changed = await VecDocEuclidean.define_indexes()
            changed_sent = _calls(calls)
            monkeypatch.undo()
            stored = await _indexes(client, "vec_doc")
            # The model wins again: VecDoc redefines its own index, nothing else.
            back = await VecDoc.define_indexes()
            restored = (await _indexes(client, "vec_doc"))["vec_doc_embedding"]
        assert kept == []
        assert kept_sent == _DOC_READS
        assert kept_state is not None and kept_state.dist == "COSINE"
        assert changed == [_H1_EUCLIDEAN]
        assert changed_sent == [*_DOC_READS, *_query(_H1_EUCLIDEAN)]
        state = stored["vec_doc_embedding"]
        assert state is not None and state.dist == "EUCLIDEAN"
        assert search.index_matches(VecDocEuclidean.get_indexes()[0], ["embedding"], state)
        for index in VecDoc.get_indexes()[1:]:
            assert search.index_matches(index, _doc_columns(index), stored[index.name]), index.name
        assert back == [_H1]
        assert search.index_matches(VecDoc.get_indexes()[0], ["embedding"], restored)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_without_dist_and_with_the_default_dist_both_unchanged(
        self, kind: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``Index(…, hnsw=True, dimension=3)`` and the same with ``dist="euclidean"`` are two declarations (not equal)
        of one statement: each matches the index the other defined, so neither sends a ``DEFINE``. Neither model
        declares a full-text index: ``define_indexes()`` reads no line."""
        async with _vec_models(kind) as client:
            calls = _spy(monkeypatch, client)
            bare_first = await VecOnly.define_indexes()
            spelled_then = await VecOnlyEuclidean.define_indexes()
            first_round = _calls(calls)
            monkeypatch.undo()
            bare_state = (await _indexes(client, "vec_only"))["vec_only_embedding"]
            removed = await VecOnly.remove_index("vec_only_embedding")
            after_removal = await _indexes(client, "vec_only")
            calls = _spy(monkeypatch, client)
            spelled_first = await VecOnlyEuclidean.define_indexes()
            bare_then = await VecOnly.define_indexes()
            second_round = _calls(calls)
            monkeypatch.undo()
            spelled_state = (await _indexes(client, "vec_only"))["vec_only_embedding"]
        assert VecOnly.get_indexes() != VecOnlyEuclidean.get_indexes()
        assert bare_first == spelled_first == [_ONLY]
        assert spelled_then == bare_then == []
        assert first_round == second_round == [*_ONLY_READS, *_query(_ONLY), *_ONLY_READS]
        assert removed == "REMOVE INDEX IF EXISTS `vec_only_embedding` ON `vec_only`;"
        assert after_removal == {}
        for state in (bare_state, spelled_state):
            assert state is not None and state.hnsw is True
            # Every engine stores the TYPE written: the embedded engine's own default would be F64.
            assert (state.dimension, state.dist, state.vector_type, state.efc, state.m) == (3, "EUCLIDEAN", "F32", 150, 12)
            for model in (VecOnly, VecOnlyEuclidean):
                assert search.index_matches(model.get_indexes()[0], ["embedding"], state), model.__name__

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_stored_mtree_index_is_redefined_hnsw(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """SurrealDB 2.x and the embedded engine accept an ``MTREE`` index, which ``similar_to()`` does not use: one
        stored under the declared name is redefined ``HNSW`` (``overwrite=False`` leaves it). SurrealDB 3.x refuses
        the ``MTREE`` statement, so the name holds nothing and the ``HNSW`` index is defined."""
        async with _vec_models(kind) as client:
            major = await _major(client)
            refusals = await _refusals(client, _MTREE_ONLY)
            before = await _indexes(client, "vec_only")
            calls = _spy(monkeypatch, client)
            kept = await VecOnly.define_indexes(overwrite=False)
            kept_sent = _calls(calls)
            monkeypatch.undo()
            kept_state = await _indexes(client, "vec_only")
            applied = await VecOnly.define_indexes()
            after = await _indexes(client, "vec_only")
            again = await VecOnly.define_indexes()
        state = after["vec_only_embedding"]
        assert state is not None and (state.hnsw, state.mtree) == (True, False)
        assert search.index_matches(VecOnly.get_indexes()[0], ["embedding"], state)
        assert again == []
        if major >= 3:
            assert len(refusals) == 1 and "Parse error" in refusals[0], refusals
            assert before == {}
            # Nothing stored: overwrite=False defines the index (IF NOT EXISTS), the next call finds it equal.
            assert kept == [_ONLY.replace("OVERWRITE", "IF NOT EXISTS")]
            assert applied == []
            return
        assert refusals == []
        mtree = before["vec_only_embedding"]
        assert mtree is not None and (mtree.hnsw, mtree.mtree) == (False, True)
        assert kept == []
        assert kept_sent == _ONLY_READS
        assert kept_state == before
        assert applied == [_ONLY]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_stored_plain_index_is_redefined_hnsw(self, kind: str) -> None:
        async with _vec_models(kind) as client:
            await _apply_ddl(client, _PLAIN_ONLY)
            before = (await _indexes(client, "vec_only"))["vec_only_embedding"]
            kept = await VecOnly.define_indexes(overwrite=False)
            applied = await VecOnly.define_indexes()
            state = (await _indexes(client, "vec_only"))["vec_only_embedding"]
        assert before is not None and (before.hnsw, before.mtree, before.unique) == (False, False, False)
        assert kept == []
        assert applied == [_ONLY]
        assert state is not None and state.hnsw is True
        assert search.index_matches(VecOnly.get_indexes()[0], ["embedding"], state)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_remove_index_of_a_vector_index(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _vec_models(kind) as client:
            await VecDoc.define_table()
            await VecDoc.define_indexes()
            removed = await VecDoc.remove_index("vec_doc_embedding")
            strict = await VecDoc.remove_index("vec_doc_summary", missing_ok=False)
            left = await _indexes(client, "vec_doc")
            calls = _spy(monkeypatch, client)
            applied = await VecDoc.define_indexes()
            sent = _calls(calls)
            monkeypatch.undo()
            stored = await _indexes(client, "vec_doc")
        assert removed == "REMOVE INDEX IF EXISTS `vec_doc_embedding` ON `vec_doc`;"
        assert strict == "REMOVE INDEX `vec_doc_summary` ON `vec_doc`;"
        assert sorted(left) == ["vec_doc_title"]
        # Only the missing vector indexes are defined again.
        assert applied == [_H1, _H_SUMMARY]
        assert sent == [*_DOC_READS, *_sent([_H1, _H_SUMMARY])]
        for index in VecDoc.get_indexes():
            assert search.index_matches(index, _doc_columns(index), stored[index.name]), index.name

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_vector_index_joins_a_transaction(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """No analyzer to define: the vector index statement joins the block — applied at once in an interactive
        transaction (SurrealDB 3.x over WebSocket), queued and applied at the commit in a buffered one (HTTP, SurrealDB
        2.x, the embedded engine); the reads see the committed state."""
        async with _vec_models(kind) as client:
            interactive = kind == "ws" and await _major(client) >= 3
            async with SurrealDBConnectionManager.transaction("vec") as tx:
                assert tx.is_interactive is interactive
                calls = _spy(monkeypatch, client)
                applied = await VecOnly.define_indexes(tx=tx)
                sent = _calls(calls)
                monkeypatch.undo()
                queued = getattr(tx, "statements", None)
                before = await _indexes(client, "vec_only")
            after = await _indexes(client, "vec_only")
        assert applied == [_ONLY]
        if interactive:
            assert sent == [*_ONLY_READS, ("query_raw", _ONLY)]
        else:
            assert sent == _ONLY_READS
            assert queued == [_ONLY]
        assert before == {}
        assert sorted(after) == ["vec_only_embedding"]
        assert search.index_matches(VecOnly.get_indexes()[0], ["embedding"], after["vec_only_embedding"])

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_block_that_raises_applies_no_vector_index(self, kind: str) -> None:
        async with _vec_models(kind) as client:
            with pytest.raises(_Rollback):
                async with SurrealDBConnectionManager.transaction("vec") as tx:
                    assert await VecOnly.define_indexes(tx=tx) == [_ONLY]
                    raise _Rollback
            indexes = await _indexes(client, "vec_only")
        assert indexes == {}


# === Pinned DDL facts ===============================================================================================

#: An index without ``TYPE``, and the same declared through the ORM (which writes ``TYPE F32``).
_PIN_BARE = "DEFINE INDEX vec_pin_t ON vec_pin FIELDS embedding HNSW DIMENSION 3;"
_PIN_ORM = Index("vec_pin_o", "embedding", hnsw=True, dimension=3)

#: What ``INFO FOR TABLE`` renders for ``_PIN_BARE`` on a server, and on the embedded engine.
_PIN_BARE_SERVER = (
    "DEFINE INDEX vec_pin_t ON vec_pin FIELDS embedding HNSW DIMENSION 3 DIST EUCLIDEAN TYPE F32 EFC 150 M 12 M0 24 "
    "LM 0.40242960438184466f"
)
_PIN_BARE_EMBEDDED = (
    "DEFINE INDEX vec_pin_t ON vec_pin FIELDS embedding HNSW DIMENSION 3 DIST EUCLIDEAN TYPE F64 EFC 150 M 12 M0 24 "
    "LM 0.40242960438184466f"
)

_PIN_MTREE = "DEFINE INDEX vec_pin_m ON vec_pin FIELDS embedding MTREE DIMENSION 3;"
_PIN_MTREE_STORED = (
    "DEFINE INDEX vec_pin_m ON vec_pin FIELDS embedding MTREE DIMENSION 3 DIST EUCLIDEAN TYPE F64 CAPACITY 40 "
    "DOC_IDS_ORDER 100 DOC_IDS_CACHE 100 MTREE_CACHE 100"
)

_PIN_UNIQUE = "DEFINE INDEX vec_pin_u ON vec_pin FIELDS embedding HNSW DIMENSION 3 UNIQUE;"

#: The README row these pins back.
_VECTOR_INDEX_ROW = "README row 'Vector index (`Index(hnsw=True)`, `define_indexes()`)'"


class TestVectorDdlPinsE2E:
    """What each engine does with vector index statements the ORM never writes, pinned with raw statements on every
    engine: an index without ``TYPE`` stores ``F64`` vectors on the embedded engine and ``F32`` on the servers — hence
    the ORM writes every option (README section 37, ``_S37_TYPE``); SurrealDB 3.x refuses ``MTREE``, which 2.x and the
    embedded engine accept (the README row "Vector index"); ``HNSW … UNIQUE`` stores a plain ``UNIQUE`` index, ``HNSW``
    dropped, on every engine — hence ``Index(hnsw=True, unique=True)`` is refused."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_index_without_type_is_f64_on_the_embedded_engine(self, kind: str) -> None:
        async with _vec(kind) as client:
            await _apply_ddl(client, _PIN_BARE)
            await _apply_ddl(client, _PIN_ORM.ddl("vec_pin"))
            info = await _table_info(client, "vec_pin")
        stored = info["indexes"]
        if kind == "mem":
            assert stored["vec_pin_t"] == _PIN_BARE_EMBEDDED, (
                "README section 37, _S37_TYPE: the embedded engine stores F64 vectors for an index without TYPE"
            )
        else:
            assert stored["vec_pin_t"] == _PIN_BARE_SERVER, (
                "README section 37, _S37_TYPE: a SurrealDB server stores F32 vectors for an index without TYPE"
            )
        written = parse_define_index(stored["vec_pin_o"])
        assert written.vector_type == "F32", "README section 37, _S37_TYPE: the ORM writes TYPE on every engine"
        assert search.index_matches(_PIN_ORM, ["embedding"], written)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_mtree_is_a_parse_error_on_3x(self, kind: str) -> None:
        async with _vec(kind) as client:
            major = await _major(client)
            refusals = await _refusals(client, _PIN_MTREE)
            info = await _table_info(client, "vec_pin")
        if major >= 3:
            assert len(refusals) == 1 and "Parse error" in refusals[0], f"{_VECTOR_INDEX_ROW}: 3.x refuses MTREE"
            assert info["indexes"] == {}
        else:
            assert refusals == [], f"{_VECTOR_INDEX_ROW}: 2.x and the embedded engine accept MTREE"
            assert info["indexes"] == {"vec_pin_m": _PIN_MTREE_STORED}, _VECTOR_INDEX_ROW
            state = parse_define_index(info["indexes"]["vec_pin_m"])
            assert (state.mtree, state.hnsw) == (True, False)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_hnsw_unique_stores_a_plain_unique_index(self, kind: str) -> None:
        async with _vec(kind) as client:
            await _apply_ddl(client, _PIN_UNIQUE)
            info = await _table_info(client, "vec_pin")
        message = "Index(hnsw=True, unique=True) is refused: SurrealDB keeps a plain UNIQUE index and drops HNSW"
        assert info["indexes"] == {"vec_pin_u": "DEFINE INDEX vec_pin_u ON vec_pin FIELDS embedding UNIQUE"}, message
        state = parse_define_index(info["indexes"]["vec_pin_u"])
        assert (state.unique, state.hnsw, state.dimension) == (True, False, None), message


# === similar_to(): the KNN, the distance, the order =================================================================

#: The query vector of the corpus's assertions.
_Q = [1.0, 0.0, 0.0]


class VecIndexed(BaseSurrealModel):
    """A vector index without ``dist`` (its effective metric is ``EUCLIDEAN``) on a field that is not a vector field."""

    model_config = SurrealConfigDict(
        table_name="vec_indexed", indexes=[Index("vec_indexed_embedding", "embedding", hnsw=True, dimension=3)]
    )

    id: str | None = None
    embedding: list[float] | None = None


class VecShadow(BaseSurrealModel):
    """A model that defines ``_knn_distance`` itself."""

    model_config = SurrealConfigDict(table_name="vec_shadow")

    id: str | None = None
    embedding: VectorField[3]
    _knn_distance: float = 0.0


class VecLink(BaseSurrealEdge):
    """An edge model: its endpoints are not vector fields."""

    model_config = SurrealConfigDict(table_name="vec_link")

    embedding: VectorField[3]


#: K1 … K8 and K13, the statements of the read goldens.
_K1 = "SELECT *, vector::distance::knn() AS _knn_distance FROM vec_doc WHERE embedding <|2,100|> $_f0 ORDER BY _knn_distance;"
_K2 = (
    "SELECT *, vector::distance::knn() AS _knn_distance FROM vec_doc WHERE category = $_f0 AND embedding <|3,40|> "
    "$_f1 ORDER BY _knn_distance;"
)
_K3 = "SELECT *, vector::distance::knn() AS _knn_distance FROM vec_doc WHERE embedding <|50,50|> $_f0 ORDER BY _knn_distance;"
_K4 = (
    "SELECT *, vector::distance::knn() AS _knn_distance FROM vec_doc WHERE embedding <|2,MANHATTAN|> $_f0 ORDER BY "
    "_knn_distance;"
)
_K5 = "SELECT *, vector::distance::knn() AS _knn_distance FROM vec_doc WHERE embedding <|2,100|> $_f0 ORDER BY n DESC;"
_K6 = (
    "SELECT title, vector::distance::knn() AS _knn_distance FROM vec_doc WHERE summary <|10,100|> $_f0 ORDER BY _knn_distance;"
)
_K7 = "SELECT count() FROM vec_doc WHERE embedding <|2,100|> $_f0 GROUP ALL;"
_K8 = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_doc WHERE embedding <|2,100|> $_f0); (SELECT * FROM vec_doc WHERE id IN $_sq0) };"
)
_K13 = (
    "SELECT *, vector::distance::knn() AS _knn_distance FROM vec_doc WHERE (loc.is_point() AND geo::distance(loc, "
    "$_f0) <= $_f1) AND embedding <|2,100|> $_f2 ORDER BY _knn_distance;"
)

#: K1's statement with another ``K`` and ``EF``, another order or a page.
_K1_3 = "SELECT *, vector::distance::knn() AS _knn_distance FROM vec_doc WHERE embedding <|3,100|> $_f0 ORDER BY _knn_distance;"
_K1_5_5 = "SELECT *, vector::distance::knn() AS _knn_distance FROM vec_doc WHERE embedding <|5,5|> $_f0 ORDER BY _knn_distance;"
_K1_LARGEST_LIMIT = (
    "SELECT *, vector::distance::knn() AS _knn_distance FROM vec_doc WHERE embedding <|10000,10000|> $_f0 ORDER BY "
    "_knn_distance;"
)
_K1_LARGEST_EF = (
    "SELECT *, vector::distance::knn() AS _knn_distance FROM vec_doc WHERE embedding <|1,10000|> $_f0 ORDER BY _knn_distance;"
)
_K1_FARTHEST = (
    "SELECT *, vector::distance::knn() AS _knn_distance FROM vec_doc WHERE embedding <|2,100|> $_f0 ORDER BY "
    "_knn_distance DESC;"
)
_K1_PAGE = (
    "SELECT *, vector::distance::knn() AS _knn_distance FROM vec_doc WHERE embedding <|3,100|> $_f0 ORDER BY "
    "_knn_distance LIMIT 1 START 1;"
)

_V4_CONTAINER = "similar_to() takes the vector as a list or tuple of numbers, got {}; convert an array with .tolist()."
_V7_RANGE = "similar_to() limit must be between 1 and 10000, got {!r}."
_V8_RANGE = "similar_to() ef must be between 1 and 10000, got {!r}."
_V9 = "similar_to() takes ef= (the HNSW index's search) or dist= (an exact scan without an index), not both."
_V11 = "similar_to() sets each record's distance as _knn_distance, which VecShadow defines; rename that attribute."
_V12 = "similar_to(): a zero vector has no COSINE distance (SurrealDB answers NaN); use another vector or distance."


async def _forbidden_client(*args: Any, **kwargs: Any) -> Any:
    pytest.fail("a client was acquired")


@pytest.fixture
def _no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """No client may be acquired: a refusal comes before any request."""
    monkeypatch.setattr(SurrealDBConnectionManager, "get_client", _forbidden_client)


def _refused_unchanged(error: type[Exception], *args: Any, **kwargs: Any) -> str:
    """The text of the *error* ``similar_to(*args, **kwargs)`` raises on a ``VecDoc`` queryset that already holds a
    ``similar_to()``, its class checked exactly; the queryset is checked unchanged after it."""
    queryset = VecDoc.objects().filter(category="news").similar_to("embedding", _Q, 2)
    before = (queryset._similar, queryset._compile_query(), set(queryset._link_heads))
    text = _raised(error, queryset.similar_to, *args, **kwargs)
    assert (queryset._similar, queryset._compile_query(), set(queryset._link_heads)) == before
    return text


@pytest.mark.usefixtures("_no_client")
class TestSimilarTo:
    """``similar_to()`` checks every argument in a fixed order — the field (V1–V3), the model (V11), the vector (V4,
    V5), ``limit`` (V7), ``ef`` (V8), ``ef`` with ``dist`` (V9), ``dist`` (V10), the declared dimension (V6), a zero
    vector with ``COSINE`` (V12) — each refusal with its exact text and class, before any request, the queryset left
    unchanged; a second call replaces the first. The statements it compiles (K1–K8, K13) are pinned exactly."""

    @pytest.mark.parametrize(("field", "kind"), [(3, "int"), (None, "NoneType"), (b"embedding", "bytes")])
    def test_v1_a_field_that_is_not_a_str(self, field: object, kind: str) -> None:
        assert _refused_unchanged(TypeError, field, _Q) == f"similar_to() takes a field name (str), not {kind}."

    @pytest.mark.parametrize("field", ["a b", "meta..vec", "1embedding", "embedding;", "", " "])
    def test_v2_a_field_that_is_not_a_name(self, field: str) -> None:
        assert _refused_unchanged(ValueError, field, _Q) == _raised(ValueError, validate_field_name, field, "similar_to field")

    def test_v2_the_name_is_matched_in_full(self) -> None:
        # validate_field_name()'s pattern accepts a trailing newline; similar_to() refuses it with the same text.
        assert _refused_unchanged(ValueError, "embedding\n", _Q) == (
            "Invalid similar_to field name 'embedding\n': must contain only alphanumeric characters, underscores, and "
            "dots (for nested fields), and start with a letter or underscore"
        )

    def test_v3_the_record_id(self) -> None:
        assert _refused_unchanged(ValueError, "id", _Q) == (
            "similar_to() compares vectors: 'id' is the record id, not a vector field."
        )

    @pytest.mark.parametrize("field", ["in_", "in", "out"])
    def test_v3_an_edge_endpoint(self, field: str) -> None:
        queryset = VecLink.objects().similar_to("embedding", _Q, 2)
        before = queryset._compile_query()
        assert _raised(ValueError, queryset.similar_to, field, _Q) == (
            f"similar_to() compares vectors: {field!r} is an edge endpoint, not a vector field."
        )
        assert queryset._compile_query() == before

    def test_v11_a_model_defining_the_distance_attribute(self) -> None:
        assert _raised(ValueError, VecShadow.objects().similar_to, "embedding", _Q) == _V11
        # V11 comes before the vector's checks.
        assert _raised(ValueError, VecShadow.objects().similar_to, "embedding", []) == _V11

    @pytest.mark.parametrize(
        ("value", "text"),
        [
            ({"x": 1.0}, _V4_CONTAINER.format("dict")),
            ("1,0,0", _V4_CONTAINER.format("str")),
            (None, _V4_CONTAINER.format("NoneType")),
            ([True, 0.0, 0.0], "similar_to(): vector item 0 is bool, not a number."),
            ([1.0, "0", 0.0], "similar_to(): vector item 1 is str, not a number."),
        ],
    )
    def test_v4_the_vector_and_its_items(self, value: object, text: str) -> None:
        assert _refused_unchanged(TypeError, "embedding", value) == text

    @pytest.mark.parametrize(
        ("value", "text"),
        [
            ([], "similar_to(): the vector is empty."),
            ((), "similar_to(): the vector is empty."),
            ([1.0, math.nan, 0.0], "similar_to(): vector item 1 is not finite (nan)."),
            ([1.0, 0.0, math.inf], "similar_to(): vector item 2 is not finite (inf)."),
        ],
    )
    def test_v5_an_empty_vector_or_an_item_not_finite(self, value: object, text: str) -> None:
        assert _refused_unchanged(ValueError, "embedding", value) == text

    @pytest.mark.parametrize(("limit", "kind"), [(True, "bool"), ("3", "str"), (2.0, "float"), (None, "NoneType")])
    def test_v7_a_limit_that_is_not_an_int(self, limit: object, kind: str) -> None:
        assert _refused_unchanged(TypeError, "embedding", _Q, limit) == f"similar_to() limit must be an int, got {kind}."

    @pytest.mark.parametrize("limit", [0, -1, 10_001, 2**32])
    def test_v7_a_limit_out_of_range(self, limit: int) -> None:
        assert _refused_unchanged(ValueError, "embedding", _Q, limit) == _V7_RANGE.format(limit)

    @pytest.mark.parametrize(("ef", "kind"), [(True, "bool"), ("40", "str"), (40.0, "float")])
    def test_v8_an_ef_that_is_not_an_int(self, ef: object, kind: str) -> None:
        assert (
            _refused_unchanged(TypeError, "embedding", _Q, 2, ef=ef) == f"similar_to() ef must be an int or None, got {kind}."
        )

    @pytest.mark.parametrize("ef", [0, -1, 10_001, 2**32])
    def test_v8_an_ef_out_of_range(self, ef: int) -> None:
        assert _refused_unchanged(ValueError, "embedding", _Q, 2, ef=ef) == _V8_RANGE.format(ef)

    def test_the_largest_limit_and_ef_are_accepted(self) -> None:
        queryset = VecDoc.objects().similar_to("embedding", _Q, 10_000)
        assert queryset._compile_query()[0] == _K1_LARGEST_LIMIT
        queryset = VecDoc.objects().similar_to("embedding", _Q, 1, ef=10_000)
        assert queryset._compile_query()[0] == _K1_LARGEST_EF

    def test_v9_ef_and_dist(self) -> None:
        assert _refused_unchanged(ValueError, "embedding", _Q, 2, ef=40, dist="cosine") == _V9
        # V8 comes first, V10 after.
        assert _refused_unchanged(ValueError, "embedding", _Q, 2, ef=0, dist="cosine") == _V8_RANGE.format(0)
        assert _refused_unchanged(ValueError, "embedding", _Q, 2, ef=40, dist="jaccard") == _V9

    @pytest.mark.parametrize(
        ("dist", "error", "text"),
        [
            (3, TypeError, "similar_to(): dist= must be a str, got int."),
            (
                "hamming2",
                ValueError,
                "similar_to(): unknown distance 'hamming2'; the ORM's are EUCLIDEAN, COSINE, MANHATTAN, CHEBYSHEV and HAMMING.",
            ),
            (
                "minkowski",
                ValueError,
                "similar_to(): MINKOWSKI needs its order, which this version does not write; use EUCLIDEAN (order 2) or "
                "MANHATTAN (order 1).",
            ),
            (
                "Jaccard",
                ValueError,
                "similar_to(): JACCARD is not a distance SurrealDB ranks by: identical vectors are not the nearest with "
                "it. Use COSINE.",
            ),
            (
                "PEARSON",
                ValueError,
                "similar_to(): PEARSON is not a distance SurrealDB ranks by: identical vectors are not the nearest with "
                "it. Use COSINE.",
            ),
        ],
    )
    def test_v10_a_distance_the_orm_does_not_write(self, dist: object, error: type[Exception], text: str) -> None:
        assert _refused_unchanged(error, "embedding", _Q, 2, dist=dist) == text

    @pytest.mark.parametrize(
        ("dist", "condition"),
        [
            ("euclidean", "embedding <|2,EUCLIDEAN|> $_f0"),
            ("COSINE", "embedding <|2,COSINE|> $_f0"),
            ("Manhattan", "embedding <|2,MANHATTAN|> $_f0"),
            ("chebyshev", "embedding <|2,CHEBYSHEV|> $_f0"),
            ("hamming", "embedding <|2,HAMMING|> $_f0"),
        ],
    )
    def test_v10_every_distance_in_any_case(self, dist: str, condition: str) -> None:
        queryset = VecDoc.objects().similar_to("embedding", _Q, 2, dist=dist)
        assert queryset._where_parts() == ([condition], {"_f0": [1.0, 0.0, 0.0]}, 1)

    @pytest.mark.parametrize(
        ("field", "value", "text"),
        [
            (
                "embedding",
                [1.0, 0.0],
                "similar_to(): the vector has 2 dimensions; field 'embedding' holds vectors of 3 (VectorField[3]).",
            ),
            (
                "summary_vec",
                [1.0, 0.0, 0.0, 0.0],
                "similar_to(): the vector has 4 dimensions; field 'summary_vec' holds vectors of 3 (VectorField[3]).",
            ),
            (
                "summary",
                [1.0],
                "similar_to(): the vector has 1 dimensions; field 'summary' holds vectors of 3 (index 'vec_doc_summary').",
            ),
        ],
    )
    def test_v6_the_declared_dimension(self, field: str, value: list[float], text: str) -> None:
        assert _refused_unchanged(ValueError, field, value) == text

    def test_v6_an_index_on_a_field_that_is_not_a_vector_field(self) -> None:
        assert _raised(ValueError, VecIndexed.objects().similar_to, "embedding", [1.0, 0.0]) == (
            "similar_to(): the vector has 2 dimensions; field 'embedding' holds vectors of 3 (index 'vec_indexed_embedding')."
        )

    def test_v6_nothing_declared(self) -> None:
        queryset = VecDoc.objects().similar_to("meta.vec", [1.0, 0.0])
        assert queryset._compile_query()[0] == (
            "SELECT *, vector::distance::knn() AS _knn_distance FROM vec_doc WHERE meta.vec <|10,100|> $_f0 ORDER BY "
            "_knn_distance;"
        )

    @pytest.mark.parametrize(("field", "info"), _UNION_FIELDS)
    def test_v6_a_union_spelling_declares_the_dimension(self, field: str, info: tuple[int, str]) -> None:
        """No vector index on these fields (issue #318): the dimension comes from the union's marker."""
        for value in ([1.0, 0.0, 0.0, 0.0], [1.0, 0.0]):
            assert _raised(ValueError, VecUnion.objects().similar_to, field, value) == (
                f"similar_to(): the vector has {len(value)} dimensions; field {field!r} holds vectors of 3 (VectorField[3])."
            )
        queryset = VecUnion.objects().similar_to(field, (1, 0, 0), 2, dist="euclidean")
        assert queryset._compile_query()[1] == {"_f0": [1.0, 0.0, 0.0]}

    def test_the_limit_then_the_dimension_then_the_zero_vector(self) -> None:
        assert _refused_unchanged(ValueError, "embedding", [1.0], 0) == _V7_RANGE.format(0)
        assert _refused_unchanged(ValueError, "embedding", [0.0, 0.0], 2, dist="cosine") == (
            "similar_to(): the vector has 2 dimensions; field 'embedding' holds vectors of 3 (VectorField[3])."
        )

    @pytest.mark.parametrize(
        ("field", "value", "dist"),
        [
            ("embedding", [0.0, 0.0, 0.0], None),
            ("embedding", [0, -0.0, Decimal(0)], None),
            ("summary_vec", [0.0, 0.0, 0.0], "cosine"),
            ("meta.vec", [0.0], "COSINE"),
        ],
    )
    def test_v12_a_zero_vector_with_cosine(self, field: str, value: list[float], dist: str | None) -> None:
        assert _refused_unchanged(ValueError, field, value, 2, dist=dist) == _V12

    @pytest.mark.parametrize(
        ("model", "field", "dist"),
        [
            (VecDoc, "embedding", "euclidean"),
            (VecDoc, "summary_vec", None),
            (VecDoc, "meta.vec", None),
            (VecIndexed, "embedding", None),
        ],
    )
    def test_a_zero_vector_without_cosine(self, model: type[BaseSurrealModel], field: str, dist: str | None) -> None:
        queryset = model.objects().similar_to(field, [0.0, 0.0, 0.0], 2, dist=dist)
        assert queryset._compile_query()[1] == {"_f0": [0.0, 0.0, 0.0]}

    def test_a_second_call_replaces_the_first(self) -> None:
        queryset = VecDoc.objects().similar_to("summary_vec", [0.0, 1.0, 0.0], 5, dist="hamming")
        assert queryset.similar_to("embedding", _Q, 2) is queryset
        assert queryset._compile_query() == (_K1, {"_f0": [1.0, 0.0, 0.0]})
        assert queryset._similar == vector.KnnSearch("embedding", "embedding", (1.0, 0.0, 0.0), 2, None, None)

    def test_the_search_held(self) -> None:
        queryset = VecDoc.objects().similar_to("summary_vec", (1, Decimal("0.5"), Fraction(1, 4)), 3, ef=40)
        assert queryset._similar == vector.KnnSearch("summary_vec", "summary", (1.0, 0.5, 0.25), 3, 40, None)
        assert {type(item) for item in queryset._similar.vector} == {float}
        with pytest.raises(AttributeError):
            queryset._similar.k = 4  # type: ignore[misc]
        hash(queryset._similar)

    def test_k1(self) -> None:
        assert VecDoc.objects().similar_to("embedding", [1.0, 0.0, 0.0], 2)._compile_query() == (
            _K1,
            {"_f0": [1.0, 0.0, 0.0]},
        )

    def test_k2_the_vector_bound_as_floats(self) -> None:
        query, variables = (
            VecDoc.objects().filter(category="news").similar_to("embedding", [1, 0, 0], 3, ef=40)._compile_query()
        )
        assert (query, variables) == (_K2, {"_f0": "news", "_f1": [1.0, 0.0, 0.0]})
        assert type(variables["_f1"]) is list
        assert {type(item) for item in variables["_f1"]} == {float}

    def test_k3_ef_raised_to_the_limit(self) -> None:
        assert VecDoc.objects().similar_to("embedding", _Q, 50, ef=10)._compile_query()[0] == _K3

    def test_k4_an_exact_scan(self) -> None:
        assert VecDoc.objects().similar_to("embedding", _Q, 2, dist="manhattan")._compile_query()[0] == _K4

    def test_k5_order_by_replaces_the_default_order(self) -> None:
        assert VecDoc.objects().similar_to("embedding", _Q, 2).order_by("-n")._compile_query()[0] == _K5
        assert VecDoc.objects().order_by("-n").similar_to("embedding", _Q, 2)._compile_query()[0] == _K5
        assert VecDoc.objects().similar_to("embedding", _Q, 2).order_by("-_knn_distance")._compile_query()[0] == (_K1_FARTHEST)

    def test_k6_a_selection_and_an_aliased_field(self) -> None:
        assert VecDoc.objects().select("title").similar_to("summary_vec", _Q)._compile_query() == (
            _K6,
            {"_f0": [1.0, 0.0, 0.0]},
        )

    def test_k7_an_aggregation_projects_no_distance(self) -> None:
        assert VecDoc.objects().similar_to("embedding", _Q, 2)._compile_aggregation_query("count()") == (
            _K7,
            {"_f0": [1.0, 0.0, 0.0]},
        )

    def test_a_group_projects_no_distance(self) -> None:
        queryset = VecDoc.objects().similar_to("embedding", _Q, 2).values("category").annotate(c=Count())
        assert queryset._compile_group_by_query() == (
            "SELECT category, count() AS c FROM vec_doc WHERE embedding <|2,100|> $_f0 GROUP BY category;",
            {"_f0": [1.0, 0.0, 0.0]},
        )

    def test_k8_a_subquery(self) -> None:
        inner = VecDoc.objects().similar_to("embedding", _Q, 2)
        assert VecDoc.objects().filter(id__in=Subquery(inner))._compile_query() == (_K8, {"_f0": [1.0, 0.0, 0.0]})

    def test_k13_after_nearby(self) -> None:
        queryset = VecDoc.objects().nearby("loc", (0.0, 0.0), 1000).similar_to("embedding", _Q, 2)
        query, variables = queryset._compile_query()
        assert query == _K13
        assert (variables["_f1"], variables["_f2"]) == (1000.0, [1.0, 0.0, 0.0])

    def test_the_distance_after_the_geo_distances(self) -> None:
        queryset = VecDoc.objects().similar_to("embedding", _Q, 2).annotate(away=GeoDistance("loc", (0.0, 0.0)))
        assert queryset._compile_query()[0] == (
            "SELECT *, IF loc.is_point() THEN geo::distance(loc, $_gd0) END AS away, vector::distance::knn() AS "
            "_knn_distance FROM vec_doc WHERE embedding <|2,100|> $_f0 ORDER BY _knn_distance;"
        )

    def test_limit_and_offset_page_the_nearest(self) -> None:
        queryset = VecDoc.objects().similar_to("embedding", _Q, 3).limit(1).offset(1)
        assert queryset._compile_query()[0] == _K1_PAGE

    def test_the_where_without_a_compiler(self) -> None:
        queryset = VecDoc.objects().similar_to("embedding", _Q, 2)
        assert queryset._build_where() == (" WHERE embedding <|2,100|> $_f0", {"_f0": [1.0, 0.0, 0.0]})
        assert queryset._where_parts() == (["embedding <|2,100|> $_f0"], {"_f0": [1.0, 0.0, 0.0]}, 1)
        filtered = VecDoc.objects().filter(category="news").similar_to("embedding", _Q, 3, ef=40)
        assert filtered._where_parts() == (
            ["category = $_f0", "embedding <|3,40|> $_f1"],
            {"_f0": "news", "_f1": [1.0, 0.0, 0.0]},
            2,
        )
        assert filtered._where_parts(5)[0] == ["category = $_f5", "embedding <|3,40|> $_f6"]

    def test_a_dotted_path_records_its_link_head(self) -> None:
        queryset = VecDoc.objects().similar_to("meta.vec", [1.0, 0.0], 2)
        assert queryset._link_heads == {"meta"}
        assert queryset._similar is not None and queryset._similar.column == "meta.vec"


# === similar_to() with as_of(), live(), watch(), changes(); its combinations ========================================

_VT1 = (
    "similar_to() cannot be combined with as_of(): the vector index holds the records' current vectors, so a "
    "time-travel read would pick its neighbours by their vectors now. Run it without as_of()."
)
_VL1 = (
    "A live query cannot honour similar_to(): SurrealDB accepts a LIVE SELECT whose condition holds a KNN operator but "
    "never notifies it."
)
_VL2 = (
    "A live query cannot use a Subquery that holds similar_to(): the nearest records change with every write, and "
    "SurrealDB 2.x applies a live subquery's other conditions to a KNN wrongly. Read them with similar_to() and watch "
    "them by id."
)
_VC1 = (
    "A change feed cannot honour {}: SurrealDB's SHOW CHANGES reads the whole table's log, with no WHERE, projection, "
    "ordering or paging, outside any transaction. Call changes() on Model.objects() and filter the events."
)
#: ``search()``'s refusals of the same clauses (v0.35.0), which come first.
_TT1 = (
    "search() cannot be combined with as_of(): a full-text match reads the index, which holds the records' current "
    "text, so a time-travel read would select records by their text now. Run the search without as_of()."
)
_LV1 = (
    "A live query cannot honour search(): SurrealDB accepts a LIVE SELECT whose condition holds a full-text match but "
    "never notifies it. Search in a subquery instead — filter(id__in=Subquery(Model.objects().search(...))) — which "
    "notifies the records created or updated to match, not their deletion."
)
_MOMENT = datetime(2026, 1, 1, tzinfo=UTC)

#: The query vector of the ``search()`` combinations: the matches are not the nearest records.
_Q2 = [0.0, 1.0, 0.0]

#: ``search(title="cats")`` with ``similar_to("embedding", _Q2, 2)``, either way round, compiled.
_SEARCH_KNN = (
    "SELECT *, vector::distance::knn() AS _knn_distance FROM vec_doc WHERE title @0@ $_f0 AND embedding <|2,100|> $_f1 "
    "ORDER BY _knn_distance;"
)
_GEO_KNN = (
    "SELECT *, IF loc.is_point() THEN geo::distance(loc, $_gd0) END AS away, vector::distance::knn() AS _knn_distance "
    "FROM vec_doc WHERE embedding <|2,100|> $_f0 ORDER BY _knn_distance;"
)

#: The search annotations (v0.35.0).
_SEARCH_ANNOTATIONS = [SearchScore(0), SearchHighlight("<b>", "</b>", 0), SearchOffsets(0)]


#: K8's statement with ``similar_to()`` on the aliased field, ``K`` 3.
_K8_SUMMARY = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_doc WHERE summary <|3,100|> $_f0); (SELECT * FROM vec_doc WHERE id IN $_sq0) };"
)


def _knn_subquery(field: str = "embedding", k: int = 2) -> Any:
    """``VecDoc.objects().filter(id__in=Subquery(<similar_to() on field>))`` — K8's queryset."""
    return VecDoc.objects().filter(id__in=Subquery(VecDoc.objects().similar_to(field, _Q, k)))


@pytest.mark.usefixtures("_no_client")
class TestKnnRefusals:
    """``similar_to()`` with ``as_of()`` — either way round, and in a ``Subquery`` under the outer one —, in a live
    query (its own or a subquery's) and in a change feed: each refused with its exact text before any request, the
    queryset left unchanged; ``search()``'s refusals of the same clauses come first. The statement's compiler records
    each subquery's vector field."""

    def test_vt1_similar_to_after_as_of(self) -> None:
        queryset = VecDoc.objects().as_of(_MOMENT)
        assert _raised(SurrealDbError, queryset.similar_to, "embedding", _Q, 2) == _VT1
        assert queryset._similar is None
        assert queryset._as_of is not None and queryset._as_of.moment == _MOMENT
        assert queryset._link_heads == set()

    def test_vt1_comes_after_the_arguments_checks(self) -> None:
        queryset = VecDoc.objects().as_of(_MOMENT)
        assert _raised(ValueError, queryset.similar_to, "embedding", _Q, 0) == _V7_RANGE.format(0)
        assert _raised(ValueError, queryset.similar_to, "embedding", [0.0, 0.0, 0.0], 2) == _V12
        assert queryset._similar is None

    def test_vt1_not_after_as_of_none(self) -> None:
        queryset = VecDoc.objects().as_of(_MOMENT).as_of(None).similar_to("embedding", _Q, 2)
        assert queryset._compile_query() == (_K1, {"_f0": [1.0, 0.0, 0.0]})

    @pytest.mark.parametrize("moment", [_MOMENT, timedelta(days=1), "2026-01-01T00:00:00Z"])
    def test_vt1_as_of_after_similar_to(self, moment: Any) -> None:
        queryset = VecDoc.objects().similar_to("embedding", _Q, 2)
        assert _raised(SurrealDbError, queryset.as_of, moment) == _VT1
        assert queryset._as_of is None
        assert queryset.as_of(None) is queryset
        assert queryset._compile_query() == (_K1, {"_f0": [1.0, 0.0, 0.0]})

    def test_vt1_after_search_s_own_refusal(self) -> None:
        queryset = VecDoc.objects().search(title="cats").similar_to("embedding", _Q2, 2)
        assert _raised(SurrealDbError, queryset.as_of, _MOMENT) == _TT1

    def test_vt1_a_subquery_under_the_outer_as_of(self) -> None:
        queryset = VecDoc.objects().as_of(_MOMENT).filter(id__in=Subquery(VecDoc.objects().similar_to("embedding", _Q, 2)))
        assert _raised(SurrealDbError, queryset._compile_query, _MOMENT) == _VT1
        assert _raised(SurrealDbError, queryset._compile_aggregation_query, "count()", moment=_MOMENT) == _VT1

    @pytest.mark.parametrize("method", ["live", "watch"])
    def test_vl1(self, method: str) -> None:
        assert _raised(SurrealDbError, getattr(VecDoc.objects().similar_to("embedding", _Q, 2), method)) == _VL1
        assert _raised(SurrealDbError, getattr(VecDoc.objects().similar_to("embedding", _Q, 2, dist="cosine"), method)) == (
            _VL1
        )

    def test_vl1_after_lv1_and_before_the_other_clauses(self) -> None:
        searched = VecDoc.objects().search(title="cats").similar_to("embedding", _Q2, 2)
        assert _raised(SurrealDbError, searched.live) == _LV1
        paged = VecDoc.objects().similar_to("embedding", _Q, 2).order_by("n").limit(3)
        assert _raised(SurrealDbError, paged.live) == _VL1

    @pytest.mark.parametrize("method", ["live", "watch"])
    def test_vl2_a_subquery_holding_similar_to(self, method: str) -> None:
        assert _raised(SurrealDbError, getattr(_knn_subquery(), method)) == _VL2
        assert _raised(SurrealDbError, _knn_subquery()._compile_live, False) == _VL2

    def test_vc1(self) -> None:
        assert _raised(SurrealDbError, VecDoc.objects().similar_to("embedding", _Q, 2).changes) == _VC1.format("similar_to()")
        every = (
            VecDoc.objects()
            .filter(category="news")
            .nearby("loc", (0.0, 0.0), 1.0)
            .search(title="cats")
            .similar_to("embedding", _Q, 2)
        )
        assert _raised(SurrealDbError, every.changes) == _VC1.format("filter(), nearby(), search(), similar_to()")

    @pytest.mark.parametrize(("field", "column"), [("embedding", "embedding"), ("summary_vec", "summary")])
    def test_the_compiler_records_each_subquery_s_vector_field(self, field: str, column: str) -> None:
        queryset = _knn_subquery(field)
        compiler = queryset._subquery_compiler()
        queryset._compile_query(subqueries=compiler)
        assert compiler.knn_fields == {("vec_doc", column): field}

    def test_a_subquery_on_the_aliased_field(self) -> None:
        assert _knn_subquery("summary_vec", 3)._compile_query() == (_K8_SUMMARY, {"_f0": [1.0, 0.0, 0.0]})

    def test_the_statement_s_own_vector_field_is_not_a_subquery_s(self) -> None:
        queryset = VecDoc.objects().similar_to("embedding", _Q, 2)
        compiler = queryset._subquery_compiler()
        queryset._compile_query(subqueries=compiler)
        assert compiler.knn_fields == {}


@pytest.mark.usefixtures("_no_client")
class TestSearchWithKnn:
    """``search()``, its search annotations (``SearchScore``, ``SearchHighlight``, ``SearchOffsets``) and a
    ``GeoDistance`` annotation combine with ``similar_to()`` in either order: nothing is refused when the queryset is
    built, and nothing is sent."""

    @pytest.mark.parametrize("annotation", _SEARCH_ANNOTATIONS, ids=lambda a: type(a).__name__)
    @pytest.mark.parametrize("knn_first", [True, False], ids=["similar_to-then-annotate", "annotate-then-similar_to"])
    def test_a_search_annotation_is_accepted(self, annotation: Any, knn_first: bool) -> None:
        searched = VecDoc.objects().search(title="programming")
        if knn_first:
            queryset = searched.similar_to("embedding", _Q2, 2).annotate(s=annotation)
        else:
            queryset = searched.annotate(s=annotation).similar_to("embedding", _Q2, 2)
        assert queryset._similar == vector.KnnSearch("embedding", "embedding", (0.0, 1.0, 0.0), 2, None, None)
        assert queryset._search_annotations == {"s": annotation}
        assert queryset._annotations == {"s": annotation}

    def test_search_in_either_order(self) -> None:
        after = VecDoc.objects().search(title="cats").similar_to("embedding", _Q2, 2)
        before = VecDoc.objects().similar_to("embedding", _Q2, 2).search(title="cats")
        for queryset in (after, before):
            assert queryset._compile_query() == (_SEARCH_KNN, {"_f0": "cats", "_f1": [0.0, 1.0, 0.0]})

    def test_a_geo_distance_in_either_order(self) -> None:
        away = GeoDistance("loc", (0.0, 0.0))
        after = VecDoc.objects().similar_to("embedding", _Q, 2).annotate(away=away)
        before = VecDoc.objects().annotate(away=away).similar_to("embedding", _Q, 2)
        for queryset in (after, before):
            assert queryset._compile_query()[0] == _GEO_KNN


# The six records of the corpus: id, title, category, embedding (also the summary and the nested vector), point.
_CORPUS = [
    ("d1", "Cats and dogs", "pets", [1.0, 0.0, 0.0], (0.0, 0.0)),
    ("d2", "Cats at home", "pets", [0.9, 0.1, 0.0], (50.0, 50.0)),
    ("d3", "Rust programming", "tech", [0.0, 1.0, 0.0], (0.01, 0.01)),
    ("d4", "Python programming", "tech", [0.1, 0.9, 0.0], (0.02, 0.0)),
    ("d5", "Gardening", "home", [0.0, 0.0, 1.0], (10.0, 10.0)),
    ("d6", "Cats and code", "news", [0.5, 0.5, 0.0], (0.0, 0.02)),
]
_EMBEDDINGS = {record_id: embedding for record_id, _, _, embedding, _ in _CORPUS}


def _cosine(record_id: str, query: Sequence[float] = _Q) -> float:
    """The ``COSINE`` distance of *record_id*'s embedding from *query*, in double precision."""
    vector_ = _EMBEDDINGS[record_id]
    dot = sum(a * b for a, b in zip(vector_, query, strict=True))
    return 1.0 - dot / (math.sqrt(sum(a * a for a in vector_)) * math.sqrt(sum(b * b for b in query)))


def _euclidean(record_id: str, query: Sequence[float] = _Q) -> float:
    """The ``EUCLIDEAN`` distance of *record_id*'s embedding from *query*, in double precision."""
    return math.dist(_EMBEDDINGS[record_id], query)


def _seed_statements(
    table: str, *, doc: bool = False, nested: bool = False, numbered: bool = False, only: Sequence[str] | None = None
) -> str:
    """The ``CREATE`` of the six records in *table* (constants of these tests) — or of those *only* names —:
    ``VecDoc``'s every column with *doc*, the embedding under ``meta.vec`` with *nested*, else the title, the category
    and the embedding, and the number ``n`` (1 to 6) with *numbered*."""
    statements = []
    for number, (record_id, title, category, embedding, (lon, lat)) in enumerate(_CORPUS, start=1):
        if only is not None and record_id not in only:
            continue
        if nested:
            columns = f"meta = {{ vec: {embedding} }}"
        else:
            columns = f"title = '{title}', category = '{category}', embedding = {embedding}"
        if doc:
            columns += f", n = {number}, summary = {embedding}, loc = ({lon}, {lat})"
        elif numbered:
            columns += f", n = {number}"
        statements.append(f"CREATE {table}:{record_id} SET {columns};")
    return " ".join(statements)


async def _seed(client: Any) -> None:
    """``VecDoc``'s indexes applied (``define_indexes()``), then its six records."""
    await VecDoc.define_indexes()
    await _apply_ddl(client, _seed_statements("vec_doc", doc=True))


class VecExtra(BaseSurrealModel):
    """An ``extra="allow"`` model with a vector index."""

    model_config = SurrealConfigDict(
        table_name="vec_extra",
        extra="allow",
        indexes=[Index("vec_extra_embedding", "embedding", hnsw=True, dimension=3, dist="cosine")],
    )

    id: str | None = None
    title: str
    category: str = ""
    embedding: VectorField[3]


class VecNested(BaseSurrealModel):
    """A schemaless model whose vectors sit in an object: the index is defined by hand on ``meta.vec``."""

    model_config = SurrealConfigDict(table_name="vec_nested")

    id: str | None = None
    meta: dict[str, Any] | None = None


#: The index of ``VecNested``, written by hand.
_NESTED_INDEX = "DEFINE INDEX vec_nested_vec ON vec_nested FIELDS meta.vec HNSW DIMENSION 3 DIST EUCLIDEAN TYPE F32;"


def _ids(records: Sequence[Any]) -> list[str]:
    """The record ids of *records*, instances or dicts, in order."""
    return [str(record["id"] if isinstance(record, dict) else record.id) for record in records]


def _distances(records: Sequence[Any]) -> list[Any]:
    """The ``_knn_distance`` of each of *records*, in order."""
    return [record["_knn_distance"] if isinstance(record, dict) else record._knn_distance for record in records]


class TestSimilarToE2E:
    """``similar_to()`` on the six-record corpus, on every engine (the line under test over WebSocket and HTTP, and the
    embedded engine): the nearest records in order with their distances — the index's metric and storage type, or the
    exact scan of ``dist=`` —, on a field, an aliased field and a dotted path; the distance set on each instance and
    never written back, on an ``extra="allow"`` model too; dicts holding it when a row does not validate; ``first()``,
    ``get()`` without an id, ``order_by()``, ``limit()``/``offset()`` over the nearest; an ``ef`` below ``limit`` still
    returns ``limit`` records; the largest ``limit`` returns the table."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_nearest_in_order_with_their_distances(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _vec_models(kind) as client:
            await _seed(client)
            calls = _spy(monkeypatch, client)
            nearest = await VecDoc.objects().similar_to("embedding", _Q, 3).exec()
            sent = _calls(calls)
            monkeypatch.undo()
            summary = await VecDoc.objects().similar_to("summary_vec", _Q, 3).exec()
            every = await VecDoc.objects().similar_to("embedding", _Q, 6).exec()
            ten = await VecDoc.objects().similar_to("embedding", _Q, 10).exec()
        assert sent == _query(_K1_3)
        assert all(isinstance(doc, VecDoc) for doc in nearest)
        assert _ids(nearest) == ["d1", "d2", "d6"]
        assert _distances(nearest) == pytest.approx([_cosine(i) for i in ("d1", "d2", "d6")], rel=1e-5)
        assert _ids(summary) == ["d1", "d2", "d6"]
        assert _distances(summary) == pytest.approx([_euclidean(i) for i in ("d1", "d2", "d6")], rel=1e-5)
        assert _ids(every)[:4] == ["d1", "d2", "d6", "d4"]
        assert sorted(_ids(every)[4:]) == ["d3", "d5"]
        assert _distances(every) == pytest.approx([_cosine(i) for i in _ids(every)], rel=1e-5)
        assert sorted(_ids(ten)) == ["d1", "d2", "d3", "d4", "d5", "d6"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_dist_is_an_exact_scan(self, kind: str) -> None:
        async with _vec_models(kind) as client:
            await _seed(client)
            euclidean = await VecDoc.objects().similar_to("embedding", _Q, 3, dist="euclidean").exec()
            cosine = await VecDoc.objects().similar_to("embedding", _Q, 4, dist="cosine").exec()
        assert _ids(euclidean) == ["d1", "d2", "d6"]
        assert _distances(euclidean) == pytest.approx([0.0, math.sqrt(0.02), math.sqrt(0.5)], rel=1e-12)
        assert _ids(cosine) == ["d1", "d2", "d6", "d4"]
        assert _distances(cosine) == pytest.approx([_cosine(i) for i in ("d1", "d2", "d6", "d4")], rel=1e-9)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_distance_is_never_written_back(self, kind: str) -> None:
        async with _vec_models(kind) as client:
            await _seed(client)
            doc = (await VecDoc.objects().similar_to("embedding", _Q, 2).exec())[1]
            assert (doc.id, doc._knn_distance) == ("d2", pytest.approx(_cosine("d2"), rel=1e-5))
            assert "_knn_distance" not in doc.model_dump()
            assert "_knn_distance" not in doc.model_dump(mode="json", by_alias=True)
            # The point is cleared: this module's writes send no value the encoding boundary rewrites.
            doc.loc = None
            doc.n = 20
            await doc.update()
            updated = await client.query("SELECT * FROM vec_doc:d2;", {})
            await doc.delete()
            await doc.save()
            saved = await client.query("SELECT * FROM vec_doc:d2;", {})
        for stored in (updated, saved):
            assert (stored[0]["n"], stored[0]["embedding"]) == (20, [0.9, 0.1, 0.0])
            assert "_knn_distance" not in stored[0]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_extra_allow_model_never_keeps_the_distance(self, kind: str) -> None:
        async with _vec_models(kind) as client:
            await VecExtra.define_indexes()
            await _apply_ddl(client, _seed_statements("vec_extra"))
            docs = await VecExtra.objects().values("category").similar_to("embedding", _Q, 2).exec()
            assert [type(doc) for doc in docs] == [VecExtra, VecExtra]
            assert _ids(docs) == ["d1", "d2"]
            assert _distances(docs) == pytest.approx([_cosine("d1"), _cosine("d2")], rel=1e-5)
            for doc in docs:
                assert doc.model_extra == {}
                assert "_knn_distance" not in doc.model_dump()
            first = docs[0]
            first.title = "Cats and dogs, again"
            await first.update()
            updated = await client.query("SELECT * FROM vec_extra:d1;", {})
            await first.delete()
            await first.save()
            saved = await client.query("SELECT * FROM vec_extra:d1;", {})
        for stored in (updated, saved):
            assert stored[0]["title"] == "Cats and dogs, again"
            assert sorted(stored[0]) == ["category", "embedding", "id", "title"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_rows_that_do_not_validate_are_dicts_holding_the_distance(self, kind: str) -> None:
        async with _vec_models(kind) as client:
            await _seed(client)
            rows = await VecDoc.objects().select("category").similar_to("embedding", _Q, 2).exec()
        assert rows == [
            {"category": "pets", "_knn_distance": pytest.approx(_cosine("d1"), rel=1e-5)},
            {"category": "pets", "_knn_distance": pytest.approx(_cosine("d2"), rel=1e-5)},
        ]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_first_get_order_by_and_paging(self, kind: str) -> None:
        async with _vec_models(kind) as client:
            await _seed(client)
            first = await VecDoc.objects().similar_to("embedding", _Q, 3).first()
            got = await VecDoc.objects().similar_to("embedding", _Q, 1).get()
            by_n = await VecDoc.objects().similar_to("embedding", _Q, 3).order_by("-n").exec()
            farthest = await VecDoc.objects().similar_to("embedding", _Q, 3).order_by("-_knn_distance").exec()
            page = await VecDoc.objects().similar_to("embedding", _Q, 3).limit(1).offset(1).exec()
        assert (first.id, first._knn_distance) == ("d1", pytest.approx(0.0))
        assert (got.id, got._knn_distance) == ("d1", pytest.approx(0.0))
        assert _ids(by_n) == ["d6", "d2", "d1"]
        assert _distances(by_n) == pytest.approx([_cosine("d6"), _cosine("d2"), 0.0], rel=1e-5)
        assert _ids(farthest) == ["d6", "d2", "d1"]
        assert _ids(page) == ["d2"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_ef_below_the_limit_still_returns_limit_records(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _vec_models(kind) as client:
            await _seed(client)
            calls = _spy(monkeypatch, client)
            five = await VecDoc.objects().similar_to("embedding", _Q, 5, ef=1).exec()
            sent = _calls(calls)
        assert sent == _query(_K1_5_5)
        assert len(five) == 5
        assert len(set(_ids(five))) == 5
        distances = _distances(five)
        assert distances == sorted(distances)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_largest_limit_returns_the_table(self, kind: str) -> None:
        async with _vec_models(kind) as client:
            await _seed(client)
            every = await VecDoc.objects().similar_to("embedding", _Q, 10_000).exec()
        assert sorted(_ids(every)) == ["d1", "d2", "d3", "d4", "d5", "d6"]
        assert _ids(every)[:4] == ["d1", "d2", "d6", "d4"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_dotted_path(self, kind: str) -> None:
        async with _vec_models(kind) as client:
            await _apply_ddl(client, _NESTED_INDEX)
            await _apply_ddl(client, _seed_statements("vec_nested", nested=True))
            nearest = await VecNested.objects().similar_to("meta.vec", _Q, 3).exec()
        assert _ids(nearest) == ["d1", "d2", "d6"]
        assert _distances(nearest) == pytest.approx([_euclidean(i) for i in ("d1", "d2", "d6")], rel=1e-5)
        assert nearest[0].meta == {"vec": [1.0, 0.0, 0.0]}


@pytest.fixture
def empty_cache() -> Iterator[None]:
    """An empty query cache around the test."""
    QueryCache.clear()
    yield
    QueryCache.clear()


class VecRouted(BaseSurrealModel):
    """A model on the connection ``"vec"`` (``SurrealConfigDict(connection=…)``), with a vector index."""

    model_config = SurrealConfigDict(
        table_name="vec_routed",
        connection="vec",
        indexes=[Index("vec_routed_embedding", "embedding", hnsw=True, dimension=3, dist="cosine")],
    )

    id: str | None = None
    title: str
    category: str = ""
    embedding: VectorField[3]


class TestCombinationsE2E:
    """``similar_to()`` without other conditions restricts every read to the ``limit`` nearest records, on every engine:
    ``count()``, ``exists()``, ``sum()`` and the other aggregations, a ``values()`` group; a ``Subquery`` holding it
    gives their ids to another queryset's filter (one block, the KNN in its ``LET``); ``cache()`` answers a second
    identical read, not another vector, and ``define_indexes()`` drops it; a model of a named connection and a model
    inside ``using()`` apply their indexes and search on that connection. None of these reads asks the line."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_aggregations(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _vec_models(kind) as client:
            await _seed(client)

            def nearest() -> Any:
                return VecDoc.objects().similar_to("embedding", _Q, 3)

            calls = _spy(monkeypatch, client)
            count = await nearest().count()
            sent = _calls(calls)
            exists = await nearest().exists()
            total = await nearest().sum("n")
            average = await nearest().avg("n")
            lowest = await nearest().min("n")
            highest = await nearest().max("n")
            groups = await nearest().values("category").annotate(c=Count()).exec()
            exact = await VecDoc.objects().similar_to("embedding", _Q, 3, dist="euclidean").sum("n")
            every = await VecDoc.objects().similar_to("embedding", _Q, 10).count()
            versions = [call for call in _calls(calls) if call[0] == "version"]
        assert sent == _query("SELECT count() FROM vec_doc WHERE embedding <|3,100|> $_f0 GROUP ALL;")
        assert (count, exists, total, lowest, highest) == (3, True, 9, 1, 6)
        assert average == pytest.approx(3.0)
        assert sorted(groups, key=lambda row: row["category"]) == [{"category": "news", "c": 1}, {"category": "pets", "c": 2}]
        assert exact == 9
        assert every == 6
        assert versions == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_subquery(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _vec_models(kind) as client:
            await _seed(client)
            calls = _spy(monkeypatch, client)
            rows = await _knn_subquery().exec()
            sent = _calls(calls)
            count = await _knn_subquery().count()
            ordered = await _knn_subquery().order_by("-n").exec()
            summary = await _knn_subquery("summary_vec", 3).exec()
            exact = (
                await VecDoc.objects()
                .filter(id__in=Subquery(VecDoc.objects().similar_to("embedding", _Q, 2, dist="cosine")))
                .count()
            )
            versions = [call for call in _calls(calls) if call[0] == "version"]
        assert sent == _query(_K8)
        assert all(isinstance(row, VecDoc) for row in rows)
        assert sorted(_ids(rows)) == ["d1", "d2"]
        assert all(not hasattr(row, "_knn_distance") for row in rows)
        assert count == 2
        assert _ids(ordered) == ["d2", "d1"]
        assert sorted(_ids(summary)) == ["d1", "d2", "d6"]
        assert exact == 2
        assert versions == []

    @pytest.mark.usefixtures("empty_cache")
    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_cache(self, kind: str) -> None:
        async with _vec_models(kind) as client:
            await _seed(client)

            async def cached(query: list[float]) -> list[Any]:
                docs: list[Any] = await VecDoc.objects().similar_to("embedding", query, 2).cache(ttl=30).exec()
                return docs

            before = QueryCache.stats()
            first = await cached(_Q)
            again = await cached(_Q)
            other = await cached(_Q2)
            middle = QueryCache.stats()
            assert await VecDoc.define_indexes() == [], "unchanged: nothing applied, the cache dropped all the same"
            dropped = await cached(_Q)
            after = QueryCache.stats()
        assert _ids(first) == _ids(again) == _ids(dropped) == ["d1", "d2"]
        assert all(isinstance(doc, VecDoc) for doc in again)
        assert again[0] is not first[0]
        assert _distances(again) == _distances(first)
        assert _distances(first) == pytest.approx([_cosine("d1"), _cosine("d2")], rel=1e-5)
        assert _ids(other) == ["d3", "d4"]
        assert (middle["hits"] - before["hits"], middle["misses"] - before["misses"]) == (1, 2)
        assert (after["hits"] - middle["hits"], after["misses"] - middle["misses"]) == (0, 1)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_named_connections(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _vec(kind) as client, _vec(kind, "vec_two", "vec_db_two") as other:
            applied = await VecRouted.define_indexes()
            await _apply_ddl(client, _seed_statements("vec_routed"))
            routed = await VecRouted.objects().similar_to("embedding", _Q, 2).exec()
            calls = _spy(monkeypatch, client)
            async with SurrealDBConnectionManager.using("vec_two"):
                defined = await VecDoc.define_indexes()
                await _apply_ddl(other, _seed_statements("vec_doc", doc=True, only=("d3", "d4", "d6")))
                nearest = await VecDoc.objects().similar_to("embedding", _Q, 2).exec()
                count = await VecDoc.objects().similar_to("embedding", _Q, 10).count()
            sent = _calls(calls)
            monkeypatch.undo()
            line = await _keyword_line(other)
        assert applied == VecRouted.index_ddl()
        assert _ids(routed) == ["d1", "d2"]
        assert _distances(routed) == pytest.approx([_cosine("d1"), _cosine("d2")], rel=1e-5)
        assert defined == VecDoc.index_ddl(line=line)
        assert _ids(nearest) == ["d6", "d4"]
        assert count == 3
        assert sent == [], "the reads inside using() never reach the connection 'vec'"


# === Below SurrealDB 3: every condition next to a KNN selected first in a LET; the search annotations ================

#: K9: an id subquery only next to the KNN — the same statement on every line.
_K9 = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_doc WHERE title @0@ $_f0); (SELECT *, vector::distance::knn() AS "
    "_knn_distance FROM vec_doc WHERE id IN $_sq0 AND embedding <|2,100|> $_f1 ORDER BY _knn_distance) };"
)
#: K10: a comparison and a function lookup next to the KNN, in the form for SurrealDB 3.x.
_K10 = (
    "SELECT *, vector::distance::knn() AS _knn_distance FROM vec_doc WHERE category = $_f0 AND ((title).is_string() AND "
    "string::starts_with(title, $_f1)) AND embedding <|2,100|> $_f2 ORDER BY _knn_distance;"
)
#: K11: K10's queryset in the form below SurrealDB 3, both conditions in the LET.
_K11 = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_doc WHERE category = $_f0 AND ((title).is_string() AND "
    "string::starts_with(title, $_f1))); (SELECT *, vector::distance::knn() AS _knn_distance FROM vec_doc WHERE id IN "
    "$_sq0 AND embedding <|2,100|> $_f2 ORDER BY _knn_distance) };"
)
#: K11b: K2's queryset (a plain comparison) in the form below SurrealDB 3.
_K11B = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_doc WHERE category = $_f0); (SELECT *, vector::distance::knn() AS "
    "_knn_distance FROM vec_doc WHERE id IN $_sq0 AND embedding <|3,40|> $_f1 ORDER BY _knn_distance) };"
)
#: K14: a Q OR group and an exact scan, in the form below SurrealDB 3.
_K14 = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_doc WHERE (category = $_f0 OR category = $_f1)); (SELECT *, "
    "vector::distance::knn() AS _knn_distance FROM vec_doc WHERE id IN $_sq0 AND embedding <|2,COSINE|> $_f2 ORDER BY "
    "_knn_distance) };"
)
#: K17: search() next to the KNN (K16, ``_SEARCH_KNN``) in the form below SurrealDB 3.
_K17 = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_doc WHERE title @0@ $_f0); (SELECT *, vector::distance::knn() AS "
    "_knn_distance FROM vec_doc WHERE id IN $_sq0 AND embedding <|2,100|> $_f1 ORDER BY _knn_distance) };"
)
#: K18: a search score next to the KNN, in the form for SurrealDB 3.x.
_K18 = (
    "SELECT *, search::score(0) AS s, vector::distance::knn() AS _knn_distance FROM vec_doc WHERE title @0@ $_f0 AND "
    "embedding <|2,100|> $_f1 ORDER BY _knn_distance;"
)
#: K18b: K18's queryset as exists() compiles it — below SurrealDB 3 without the search projection, and on 3.x.
_K18B = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_doc WHERE title @0@ $_f0); (SELECT *, vector::distance::knn() AS "
    "_knn_distance FROM vec_doc WHERE id IN $_sq0 AND embedding <|2,100|> $_f1 ORDER BY _knn_distance LIMIT 1) };"
)
_K18B_3X = (
    "SELECT *, search::score(0) AS s, vector::distance::knn() AS _knn_distance FROM vec_doc WHERE title @0@ $_f0 AND "
    "embedding <|2,100|> $_f1 ORDER BY _knn_distance LIMIT 1;"
)
#: A Subquery holding similar_to() and a condition (K8 with filter(category="tech") inside), in either form.
_K8_TECH = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_doc WHERE category = $_f0 AND embedding <|2,100|> $_f1); (SELECT * FROM "
    "vec_doc WHERE id IN $_sq0) };"
)
_K8_TECH_PREFILTERED = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_doc WHERE category = $_f0); LET $_sq1 = (SELECT VALUE id FROM vec_doc WHERE "
    "id IN $_sq0 AND embedding <|2,100|> $_f1); (SELECT * FROM vec_doc WHERE id IN $_sq1) };"
)
#: An outer queryset scoring its own search() while a Subquery filter holds the KNN, in the form below SurrealDB 3.
_SCORED_OUTER_PREFILTERED = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_doc WHERE category = $_f0); LET $_sq1 = (SELECT VALUE id FROM vec_doc WHERE "
    "id IN $_sq0 AND embedding <|2,100|> $_f1); (SELECT *, search::score(0) AS s FROM vec_doc WHERE id IN $_sq1 AND "
    "title @0@ $_f2) };"
)
#: A view model's form below SurrealDB 3: the LET's ids are matched as id__in matches them on a view (v0.32.0).
_VIEW_PREFILTERED = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_view WHERE category = $_f0); (SELECT *, vector::distance::knn() AS "
    "_knn_distance FROM vec_view WHERE $_sq0 CONTAINS id AND embedding <|2,100|> $_f1 ORDER BY _knn_distance) };"
)
_VIEW_BY_IDS = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_doc WHERE category = $_f0); (SELECT *, vector::distance::knn() AS "
    "_knn_distance FROM vec_view WHERE $_sq0 CONTAINS id AND embedding <|2,100|> $_f1 ORDER BY _knn_distance) };"
)
#: The read the missing-table test sends first, then again with the subquery's table bound to [].
_MISSING_FIRST = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_ranked WHERE n > $_f1); LET $_sq1 = (SELECT VALUE id FROM vec_doc WHERE "
    "category = $_f0); (SELECT *, vector::distance::knn() AS _knn_distance FROM vec_doc WHERE id IN $_sq0 AND id IN "
    "$_sq1 AND embedding <|2,100|> $_f2 ORDER BY _knn_distance) };"
)
_MISSING_AGAIN = (
    "{ LET $_sq0 = []; LET $_sq1 = (SELECT VALUE id FROM vec_doc WHERE category = $_f0); (SELECT *, "
    "vector::distance::knn() AS _knn_distance FROM vec_doc WHERE id IN $_sq0 AND id IN $_sq1 AND embedding <|2,100|> "
    "$_f1 ORDER BY _knn_distance) };"
)

_VS1 = (
    "similar_to() cannot be combined with {} on SurrealDB 2.x or the embedded engine: there the ORM selects the "
    "full-text matches first, so the vector search holds no match to score, highlight or locate. Keep search() as a "
    "condition, or use SurrealDB 3.x."
)
_SR2 = "annotate(): {} needs search(): SurrealDB scores, highlights and locates the fields search() matches."


class VecRanked(BaseSurrealModel):
    """The six records with indexed columns next to the vector index: SurrealDB 2.x answers a KNN next to a condition
    on an indexed column wrongly, so the ORM selects those records first."""

    model_config = SurrealConfigDict(
        table_name="vec_ranked",
        indexes=[
            Index("vec_ranked_embedding", "embedding", hnsw=True, dimension=3, dist="cosine"),
            Index("vec_ranked_n", "n", unique=True),
            Index("vec_ranked_category", "category"),
        ],
    )

    id: str | None = None
    title: str
    category: str = ""
    n: int = 0
    hit: bool = False
    embedding: VectorField[3]


class VecOwner(BaseSurrealModel):
    """The owner of a ``VecOwned`` record: a condition through a record link."""

    model_config = SurrealConfigDict(table_name="vec_owner")

    id: str | None = None
    team: str = ""


class VecOwned(BaseSurrealModel):
    """Six vectors, each linked to its owner."""

    model_config = SurrealConfigDict(
        table_name="vec_owned",
        indexes=[Index("vec_owned_embedding", "embedding", hnsw=True, dimension=3, dist="cosine")],
    )

    id: str | None = None
    embedding: VectorField[3]
    owner: VecOwner | RecordID | None = None


class VecView(BaseSurrealModel):
    """A materialized view over ``vec_doc``, searched with ``similar_to()``."""

    model_config = SurrealConfigDict(table_name="vec_view", view_query="SELECT title, category, embedding FROM vec_doc")

    id: RecordID | None = None
    title: str = ""
    category: str = ""
    embedding: list[float] | None = None


#: The owner of each ``vec_owned`` record, d1 to d6.
_OWNERS = {"d1": "a", "d2": "a", "d3": "a", "d4": "b", "d5": "a", "d6": "b"}


async def _seed_ranked(client: Any) -> None:
    """``VecRanked``'s indexes applied (``define_indexes()``), then its six records (``n`` 1 to 6)."""
    await VecRanked.define_indexes()
    await _apply_ddl(client, _seed_statements("vec_ranked", numbered=True))


async def _seed_owned(client: Any) -> None:
    """``VecOwned``'s index applied, then the owners ``a`` (team ``x``) and ``b`` (``y``) and the six records."""
    await VecOwned.define_indexes()
    owned = [f"CREATE vec_owned:{i} SET embedding = {_EMBEDDINGS[i]}, owner = vec_owner:{o};" for i, o in _OWNERS.items()]
    await _apply_ddl(client, " ".join(["CREATE vec_owner:a SET team = 'x';", "CREATE vec_owner:b SET team = 'y';", *owned]))


def _compiled(queryset: Any, *, knn_prefilter: bool = False, **kwargs: Any) -> tuple[tuple[str, dict[str, Any]], Any]:
    """*queryset*'s statement compiled with a new compiler (``knn_prefilter`` given), and that compiler."""
    compiler = queryset._subquery_compiler(knn_prefilter=knn_prefilter)
    return queryset._compile_query(subqueries=compiler, **kwargs), compiler


def _k10() -> Any:
    """K10's queryset: a comparison and a function lookup next to the KNN."""
    return VecDoc.objects().filter(category="pets", title__startswith="Cats").similar_to("embedding", _Q, 2)


def _k9() -> Any:
    """K9's queryset: an id subquery only next to the KNN."""
    return VecDoc.objects().filter(id__in=Subquery(VecDoc.objects().search(title="cats"))).similar_to("embedding", _Q, 2)


def _k8_tech() -> Any:
    """K8's queryset, its ``Subquery`` holding ``similar_to()`` next to ``filter(category="tech")``."""
    inner = VecDoc.objects().filter(category="tech").similar_to("embedding", _Q, 2)
    return VecDoc.objects().filter(id__in=Subquery(inner))


class _LineClient:
    """The client a unit test's read acquires: nothing may be sent through it."""

    async def query(self, *args: Any, **kwargs: Any) -> Any:
        pytest.fail("a statement reached the client")

    async def query_raw(self, *args: Any, **kwargs: Any) -> Any:
        pytest.fail("a statement reached the client")

    async def version(self) -> str:
        pytest.fail("the line was read past time_travel.cached_major()")


def _line_answers(monkeypatch: pytest.MonkeyPatch, major: int) -> list[Any]:
    """Every read acquires one ``_LineClient``, and ``time_travel.cached_major()`` answers *major*: returns the clients
    it was awaited for, in order."""
    client = _LineClient()
    reads: list[Any] = []

    async def get_client(*args: Any, **kwargs: Any) -> Any:
        return client

    async def cached_major(line_client: Any) -> int:
        reads.append(line_client)
        return major

    monkeypatch.setattr(SurrealDBConnectionManager, "get_client", get_client)
    monkeypatch.setattr(time_travel, "cached_major", cached_major)
    return reads


def _executed(monkeypatch: pytest.MonkeyPatch, queryset: Any, rows: Any = None) -> list[tuple[str, dict[str, Any]]]:
    """Replace *queryset*'s ``_execute_query`` with a fake recording each ``(query, variables)``: it raises
    ``_MissingSubqueryTable`` for the greatest table of a non-empty ``subquery_tables``, else returns *rows*
    (``[]`` by default)."""
    calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(query: str, variables: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        calls.append((query, dict(variables or {})))
        tables = kwargs.get("subquery_tables", frozenset())
        if tables:
            raise _MissingSubqueryTable(max(tables))
        return [] if rows is None else rows

    monkeypatch.setattr(queryset, "_execute_query", execute)
    return calls


class _VersionClient:
    """A client whose ``version()`` answers *version* and counts its calls."""

    def __init__(self, version: str) -> None:
        self.answer = version
        self.calls = 0

    async def version(self) -> str:
        self.calls += 1
        return self.answer


class _SlottedClient:
    """A client that cannot be weakly referenced."""

    __slots__ = ("calls",)

    def __init__(self) -> None:
        self.calls = 0

    async def version(self) -> str:
        self.calls += 1
        return "surrealdb-2.7.0"


class TestPrefilter:
    """Below SurrealDB 3 every condition AND-ed with the KNN but an id subquery is selected first, in a ``LET`` of the
    statement, and the KNN searches the ids it holds; SurrealDB 3.x gets the conditions inline. The compiler records
    whether the statement needs the line (``knn_conditions``) and compiles the form below 3 on ``knn_prefilter``. The
    line is read once per client (``time_travel.cached_major()``), and decided once per read."""

    @pytest.mark.parametrize("part", ["id IN $_sq0", "id IN $_sq12", "$_sq1 CONTAINS id"])
    def test_an_id_subquery_part(self, part: str) -> None:
        assert vector.id_subquery_part(part) is True

    @pytest.mark.parametrize(
        "part",
        [
            "id IN $_f0",
            "(id IN $_sq0)",
            "category = $_f0",
            "id IN $_sq0 OR n = $_f1",
            "title @0@ $_f0",
            "owner.id IN $_sq0",
            "ID IN $_sq0",
            "",
        ],
    )
    def test_not_an_id_subquery_part(self, part: str) -> None:
        assert vector.id_subquery_part(part) is False

    def test_k10_the_3x_form(self) -> None:
        statement, compiler = _compiled(_k10())
        assert statement == (_K10, {"_f0": "pets", "_f1": "Cats", "_f2": [1.0, 0.0, 0.0]})
        assert compiler.knn_conditions is True
        assert compiler.knn_prefilter is False

    def test_k11_every_condition_in_the_let(self) -> None:
        statement, compiler = _compiled(_k10(), knn_prefilter=True)
        assert statement == (_K11, {"_f0": "pets", "_f1": "Cats", "_f2": [1.0, 0.0, 0.0]})
        assert compiler.knn_prefilter is True
        assert compiler.tables == set()

    def test_k11b_a_plain_comparison(self) -> None:
        def k2() -> Any:
            return VecDoc.objects().filter(category="news").similar_to("embedding", [1, 0, 0], 3, ef=40)

        variables = {"_f0": "news", "_f1": [1.0, 0.0, 0.0]}
        inline, compiler = _compiled(k2())
        assert inline == (_K2, variables)
        assert compiler.knn_conditions is True
        assert _compiled(k2(), knn_prefilter=True)[0] == (_K11B, variables)

    @pytest.mark.parametrize("knn_prefilter", [False, True])
    def test_k11c_an_id_subquery_only(self, knn_prefilter: bool) -> None:
        statement, compiler = _compiled(_k9(), knn_prefilter=knn_prefilter)
        assert statement == (_K9, {"_f0": "cats", "_f1": [1.0, 0.0, 0.0]})
        assert compiler.knn_conditions is False

    @pytest.mark.parametrize("knn_prefilter", [False, True])
    def test_similar_to_alone_needs_no_line(self, knn_prefilter: bool) -> None:
        statement, compiler = _compiled(VecDoc.objects().similar_to("embedding", _Q, 2), knn_prefilter=knn_prefilter)
        assert statement == (_K1, {"_f0": [1.0, 0.0, 0.0]})
        assert compiler.knn_conditions is False

    def test_k13_nearby_needs_the_line(self) -> None:
        statement, compiler = _compiled(VecDoc.objects().nearby("loc", (0.0, 0.0), 1000).similar_to("embedding", _Q, 2))
        assert statement[0] == _K13
        assert compiler.knn_conditions is True

    def test_k14_a_q_or_group(self) -> None:
        queryset = (
            VecDoc.objects().filter(Q(category="tech") | Q(category="news")).similar_to("embedding", _Q, 2, dist="cosine")
        )
        assert _compiled(queryset, knn_prefilter=True)[0] == (
            _K14,
            {"_f0": "tech", "_f1": "news", "_f2": [1.0, 0.0, 0.0]},
        )

    @pytest.mark.parametrize("knn_first", [False, True], ids=["search-then-similar_to", "similar_to-then-search"])
    def test_k16_and_k17_search(self, knn_first: bool) -> None:
        def k16() -> Any:
            if knn_first:
                return VecDoc.objects().similar_to("embedding", _Q2, 2).search(title="cats")
            return VecDoc.objects().search(title="cats").similar_to("embedding", _Q2, 2)

        variables = {"_f0": "cats", "_f1": [0.0, 1.0, 0.0]}
        inline, compiler = _compiled(k16())
        assert inline == (_SEARCH_KNN, variables)
        assert compiler.knn_conditions is True
        assert _compiled(k16(), knn_prefilter=True)[0] == (_K17, variables)

    def test_a_view_model(self) -> None:
        variables = {"_f0": "tech", "_f1": [1.0, 0.0, 0.0]}
        compared = VecView.objects().filter(category="tech").similar_to("embedding", _Q, 2)
        assert _compiled(compared, knn_prefilter=True)[0] == (_VIEW_PREFILTERED, variables)
        by_ids = VecView.objects().filter(id__in=Subquery(VecDoc.objects().filter(category="tech")))
        for knn_prefilter in (False, True):
            statement, compiler = _compiled(by_ids.similar_to("embedding", _Q, 2), knn_prefilter=knn_prefilter)
            assert statement == (_VIEW_BY_IDS, variables)
            assert compiler.knn_conditions is False

    def test_a_subquery_holding_similar_to_and_a_condition(self) -> None:
        variables = {"_f0": "tech", "_f1": [1.0, 0.0, 0.0]}
        inline, compiler = _compiled(_k8_tech())
        assert inline == (_K8_TECH, variables)
        assert compiler.knn_conditions is True
        assert compiler.tables == {"vec_doc"}
        assert _compiled(_k8_tech(), knn_prefilter=True)[0] == (_K8_TECH_PREFILTERED, variables)

    def test_without_a_compiler_the_knn_is_inline(self) -> None:
        parts, variables, counter = _k10()._where_parts()
        assert parts == [
            "category = $_f0",
            "((title).is_string() AND string::starts_with(title, $_f1))",
            "embedding <|2,100|> $_f2",
        ]
        assert (variables, counter) == ({"_f0": "pets", "_f1": "Cats", "_f2": [1.0, 0.0, 0.0]}, 3)
        assert _k10()._build_where()[0] == " WHERE " + " AND ".join(parts)

    def test_the_compiler_defaults(self) -> None:
        compiler = SubqueryCompiler()
        assert (compiler.knn_prefilter, compiler.knn_conditions) == (False, False)
        assert VecDoc.objects()._subquery_compiler().knn_prefilter is False

    async def test_cached_major_reads_each_client_once(self) -> None:
        newer, older = _VersionClient("surrealdb-3.3.4"), _VersionClient("surrealdb-2.7.0")
        assert [await time_travel.cached_major(newer) for _ in range(3)] == [3, 3, 3]
        assert [await time_travel.cached_major(older) for _ in range(2)] == [2, 2]
        assert (newer.calls, older.calls) == (1, 1)

    async def test_cached_major_reads_a_client_without_weak_references_every_time(self) -> None:
        client = _SlottedClient()
        assert [await time_travel.cached_major(client) for _ in range(2)] == [2, 2]
        assert client.calls == 2

    async def test_cached_major_of_an_unreadable_version(self) -> None:
        assert await time_travel.cached_major(_VersionClient("unknown")) == 3

    async def test_the_line_is_decided_once_per_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reads = _line_answers(monkeypatch, 2)
        ranked = Subquery(VecRanked.objects().filter(n__gt=2))
        queryset = VecDoc.objects().filter(category="tech", id__in=ranked).similar_to("embedding", _Q, 2)
        executed = _executed(monkeypatch, queryset)
        assert await queryset.exec() == []
        assert executed == [
            (_MISSING_FIRST, {"_f0": "tech", "_f1": 2, "_f2": [1.0, 0.0, 0.0]}),
            (_MISSING_AGAIN, {"_f0": "tech", "_f1": [1.0, 0.0, 0.0]}),
        ]
        assert len(reads) == 1

    async def test_the_3x_line_sends_the_inline_form(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reads = _line_answers(monkeypatch, 3)
        queryset = _k10()
        executed = _executed(monkeypatch, queryset)
        assert await queryset.count() == 0
        assert await queryset.exec() == []
        assert [query for query, _ in executed] == [
            "SELECT count() FROM vec_doc WHERE category = $_f0 AND ((title).is_string() AND string::starts_with(title, "
            "$_f1)) AND embedding <|2,100|> $_f2 GROUP ALL;",
            _K10,
        ]
        assert len(reads) == 2

    async def test_no_line_without_a_condition(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reads = _line_answers(monkeypatch, 2)
        for queryset in (VecDoc.objects().similar_to("embedding", _Q, 2), _k9(), VecDoc.objects().filter(category="x")):
            _executed(monkeypatch, queryset)
            await queryset.exec()
        assert reads == []


def _scored(*annotations: tuple[str, Any], annotate_first: bool = False) -> Any:
    """K18's queryset: ``search(title="cats")``, ``similar_to("embedding", _Q2, 2)`` and *annotations*
    (``s=SearchScore(0)`` by default), ``annotate()`` called last or first."""
    given = dict(annotations or [("s", SearchScore(0))])
    searched = VecDoc.objects().search(title="cats")
    if annotate_first:
        return searched.annotate(**given).similar_to("embedding", _Q2, 2)
    return searched.similar_to("embedding", _Q2, 2).annotate(**given)


def _programming() -> Any:
    """``search(title="programming").similar_to("embedding", _Q2, 2).annotate(s=SearchScore(0))``."""
    return VecDoc.objects().search(title="programming").similar_to("embedding", _Q2, 2).annotate(s=SearchScore(0))


#: The variables of ``_programming()``'s statements.
_PROGRAMMING = {"_f0": "programming", "_f1": [0.0, 1.0, 0.0]}


class _ReadTx:
    """A transaction that records what reaches it and answers every read with one record."""

    def __init__(self) -> None:
        self.reads: list[tuple[str, dict[str, Any]]] = []
        self.added: list[tuple[str, Any]] = []

    @property
    def is_interactive(self) -> bool:
        return True

    async def run_read(self, sql: str, variables: dict[str, Any] | None = None) -> Any:
        self.reads.append((sql, dict(variables or {})))
        return [{"id": "vec_doc:d3"}]

    async def add(self, sql: str, variables: dict[str, Any] | None = None) -> Any:
        self.added.append((sql, variables))
        return []


class _RecordingClient:
    """A client that records every call and answers each query with no rows."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def query(self, statement: str, variables: Any = None) -> Any:
        self.calls.append(("query", (statement, variables)))
        return []

    async def version(self) -> str:
        self.calls.append(("version", None))
        return "surrealdb-2.7.0"


class TestSearchAnnotationsWithKnn:
    """The search annotations of a ``similar_to()`` queryset: projected next to the distance in the form for
    SurrealDB 3.x; refused when the form below 3 compiles (``exec()``, ``first()`` and ``get()`` without an id), after
    the line is read and before anything is sent, queued or cached; left out by ``exists()``, which answers on every
    line. A queryset without its own ``similar_to()`` compiles as before."""

    @pytest.mark.parametrize("annotate_first", [False, True])
    def test_k18_the_3x_form(self, annotate_first: bool) -> None:
        statement, compiler = _compiled(_scored(annotate_first=annotate_first))
        assert statement == (_K18, {"_f0": "cats", "_f1": [0.0, 1.0, 0.0]})
        assert compiler.knn_conditions is True

    @pytest.mark.parametrize("annotation", _SEARCH_ANNOTATIONS, ids=lambda a: type(a).__name__)
    @pytest.mark.parametrize("annotate_first", [False, True])
    def test_k18_vs1_below_3(self, annotation: Any, annotate_first: bool) -> None:
        queryset = _scored(("a", annotation), annotate_first=annotate_first)
        compiler = queryset._subquery_compiler(knn_prefilter=True)
        text = _raised(SurrealDbError, queryset._compile_query, subqueries=compiler)
        assert text == _VS1.format(type(annotation).__name__)
        assert compiler.lets == []

    def test_vs1_names_the_first_annotation_in_alias_order(self) -> None:
        queryset = _scored(("h", SearchHighlight("<b>", "</b>", 0)), ("s", SearchScore(0)))
        compiler = queryset._subquery_compiler(knn_prefilter=True)
        assert _raised(SurrealDbError, queryset._compile_query, subqueries=compiler) == _VS1.format("SearchHighlight")

    def test_k18b_below_3(self) -> None:
        variables = {"_f0": "cats", "_f1": [0.0, 1.0, 0.0]}
        assert _compiled(_scored().limit(1), knn_prefilter=True, for_exists=True)[0] == (_K18B, variables)
        highlighted = _scored(("h", SearchHighlight("<b>", "</b>", 0))).limit(1)
        assert _compiled(highlighted, knn_prefilter=True, for_exists=True)[0] == (_K18B, variables)

    def test_k18b_on_3x(self) -> None:
        flagged = _compiled(_scored().limit(1), for_exists=True)[0]
        assert flagged == (_K18B_3X, {"_f0": "cats", "_f1": [0.0, 1.0, 0.0]})
        assert flagged == _compiled(_scored().limit(1))[0]

    @pytest.mark.parametrize("knn_prefilter", [False, True])
    def test_the_flag_changes_nothing_without_similar_to(self, knn_prefilter: bool) -> None:
        def annotated() -> Any:
            searched = VecDoc.objects().search(title="programming").limit(1)
            return searched.annotate(s=SearchScore(0), h=SearchHighlight("<b>", "</b>", 0))

        flagged = _compiled(annotated(), knn_prefilter=knn_prefilter, for_exists=True)[0]
        assert flagged == _compiled(annotated(), knn_prefilter=knn_prefilter)[0]
        assert flagged[0] == (
            "SELECT *, search::score(0) AS s, search::highlight($_sh0, $_sh1, 0) AS h FROM vec_doc WHERE title @0@ $_f0 "
            "LIMIT 1;"
        )

    def test_sr2_comes_first_with_the_flag(self) -> None:
        queryset = VecDoc.objects().similar_to("embedding", _Q2, 2).annotate(s=SearchScore(0))
        compiler = queryset._subquery_compiler(knn_prefilter=True)
        text = _raised(ValueError, queryset._compile_query, subqueries=compiler, for_exists=True)
        assert text == _SR2.format("SearchScore(0)")

    def test_an_outer_search_next_to_a_subquery_knn(self) -> None:
        outer = (
            VecDoc.objects()
            .search(title="programming")
            .annotate(s=SearchScore(0))
            .filter(id__in=Subquery(VecDoc.objects().filter(category="tech").similar_to("embedding", _Q, 2)))
        )
        assert _compiled(outer, knn_prefilter=True)[0] == (
            _SCORED_OUTER_PREFILTERED,
            {"_f0": "tech", "_f1": [1.0, 0.0, 0.0], "_f2": "programming"},
        )

    async def test_exists_without_similar_to_sends_v035s_statement(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _RecordingClient()

        async def get_client(*args: Any, **kwargs: Any) -> Any:
            return client

        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", get_client)
        assert await VecDoc.objects().search(title="programming").annotate(s=SearchScore(0)).exists() is False
        assert client.calls == [
            ("query", ("SELECT *, search::score(0) AS s FROM vec_doc WHERE title @0@ $_f0 LIMIT 1;", {"_f0": "programming"}))
        ]

    @pytest.mark.parametrize("method", ["exec", "first", "get"])
    async def test_below_3_exec_first_and_get_raise_vs1(self, method: str, monkeypatch: pytest.MonkeyPatch) -> None:
        reads = _line_answers(monkeypatch, 2)
        queryset = _programming()
        executed = _executed(monkeypatch, queryset)
        with pytest.raises(SurrealDbError) as raised:
            await getattr(queryset, method)()
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == _VS1.format("SearchScore")
        assert executed == []
        assert len(reads) == 1
        assert queryset._limit is None

    async def test_below_3_exists_answers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reads = _line_answers(monkeypatch, 2)
        queryset = _programming()
        executed = _executed(monkeypatch, queryset, [{"id": "vec_doc:d3"}])
        assert await queryset.exists() is True
        assert executed == [(_K18B, _PROGRAMMING)]
        assert len(reads) == 1
        assert queryset._limit is None

    async def test_on_3x_exec_and_exists_send_the_inline_form(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reads = _line_answers(monkeypatch, 3)
        queryset = _programming()
        executed = _executed(monkeypatch, queryset, [])
        assert await queryset.exec() == []
        assert await queryset.exists() is False
        assert executed == [(_K18, _PROGRAMMING), (_K18B_3X, _PROGRAMMING)]
        assert len(reads) == 2

    @pytest.mark.usefixtures("empty_cache")
    async def test_below_3_with_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reads = _line_answers(monkeypatch, 2)
        queryset = _programming().cache(ttl=30)
        executed = _executed(monkeypatch, queryset, [{"id": "vec_doc:d3"}])
        keyed: list[str] = []

        async def cached_read(kind: str, statement: str, variables: Any, tables: Any, any_table: bool, run: Any) -> Any:
            keyed.append(statement)
            return await run()

        monkeypatch.setattr(queryset, "_cached_read", cached_read)
        with pytest.raises(SurrealDbError) as raised:
            await queryset.exec()
        assert str(raised.value) == _VS1.format("SearchScore")
        assert (keyed, executed, QueryCache.stats()["entries"]) == ([], [], 0)
        assert await queryset.exists() is True
        assert keyed == [_K18B]
        assert executed == [(_K18B, _PROGRAMMING)]
        assert len(reads) == 2

    async def test_below_3_in_a_transaction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reads = _line_answers(monkeypatch, 2)
        tx = _ReadTx()
        queryset = VecDoc.objects(tx=tx).search(title="programming").similar_to("embedding", _Q2, 2)
        queryset = queryset.annotate(s=SearchScore(0))
        with pytest.raises(SurrealDbError) as raised:
            await queryset.exec()
        assert str(raised.value) == _VS1.format("SearchScore")
        assert (tx.reads, tx.added) == ([], [])
        assert await queryset.exists() is True
        assert tx.reads == [(_K18B, _PROGRAMMING)]
        assert tx.added == []
        assert len(reads) == 2


class TestPrefilterE2E:
    """``similar_to()`` next to every kind of condition, on every engine: the same records on every line — SurrealDB
    3.x gets the conditions inline, SurrealDB 2.x and the embedded engine the ``LET`` form (the spy shows each) —, the
    line read once per client and only for a statement with such a condition."""

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize(
        ("condition", "expected"),
        [
            (lambda qs: qs.filter(category="tech"), ["d4", "d3"]),
            (lambda qs: qs.filter(title__startswith="Cats"), ["d1", "d2"]),
            (lambda qs: qs.filter(Q(category="tech") | Q(category="news")), ["d6", "d4"]),
            (lambda qs: qs.filter(~Q(category="pets")), ["d6", "d4"]),
            (lambda qs: qs.nearby("loc", (0.0, 0.0), 3000), ["d1", "d6"]),
        ],
        ids=["a-comparison", "a-function", "a-q-or-group", "a-negated-q", "nearby"],
    )
    async def test_a_condition(self, kind: str, condition: Any, expected: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
        async with _vec_models(kind) as client:
            await _seed(client)
            major = await _major(client)
            queryset = condition(VecDoc.objects()).similar_to("embedding", _Q, 2)
            statement = _compiled(queryset, knn_prefilter=major < 3)[0][0]
            calls = _spy(monkeypatch, client)
            docs = await queryset.exec()
            sent = _calls(calls)
            exact = await condition(VecDoc.objects()).similar_to("embedding", _Q, 2, dist="cosine").exec()
        assert sent == [("version", None), *_query(statement)]
        assert statement.startswith("{ LET $_sq0 = (SELECT VALUE id FROM vec_doc WHERE ") == (major < 3)
        assert _ids(docs) == expected
        assert _distances(docs) == pytest.approx([_cosine(i) for i in expected], rel=1e-5)
        assert _ids(exact) == expected

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_search(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _vec_models(kind) as client:
            await _seed(client)
            line = await _line(client)
            calls = _spy(monkeypatch, client)
            docs = await VecDoc.objects().search(title="cats").similar_to("embedding", _Q2, 2).exec()
            sent = _calls(calls)
            exact = await VecDoc.objects().search(title="cats").similar_to("embedding", _Q2, 2, dist="cosine").exec()
            nearest = await VecDoc.objects().search(title="cats").similar_to("embedding", _Q2, 1, dist="cosine").exec()
        assert sent == [("version", None), *_query(_K17 if line[0] < 3 else _SEARCH_KNN)]
        if line == (3, 1):
            # README row "similar_to() and search() in one queryset": 3.1 answers every match, with no distance.
            assert sorted(_ids(docs)) == ["d1", "d2", "d6"]
            assert _distances(docs) == [None, None, None]
        else:
            assert _ids(docs) == ["d6", "d2"]
            assert _distances(docs) == pytest.approx([_cosine(i, _Q2) for i in ("d6", "d2")], rel=1e-5)
        assert _ids(exact) == ["d6", "d2"]
        assert _distances(exact) == pytest.approx([_cosine(i, _Q2) for i in ("d6", "d2")], rel=1e-9)
        assert _ids(nearest) == ["d6"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_line_is_read_once_and_only_for_a_condition(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _vec_models(kind) as client:
            await _seed(client)
            calls = _spy(monkeypatch, client)
            plain = await VecDoc.objects().filter(category="tech").exec()
            alone = await VecDoc.objects().similar_to("embedding", _Q, 2).exec()
            by_ids = await _k9().exec()
            before = _calls(calls)
            tech = await VecDoc.objects().filter(category="tech").similar_to("embedding", _Q, 2).exec()
            count = await VecDoc.objects().filter(title__startswith="Cats").similar_to("embedding", _Q, 2).count()
            versions = [call for call in _calls(calls) if call[0] == "version"]
        assert before == [*_query("SELECT * FROM vec_doc WHERE category = $_f0;"), *_query(_K1), *_query(_K9)]
        assert sorted(_ids(plain)) == ["d3", "d4"]
        assert _ids(alone) == ["d1", "d2"]
        assert _ids(by_ids) == ["d1", "d2"]
        assert _ids(tech) == ["d4", "d3"]
        assert count == 2
        assert versions == [("version", None)]

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize("dist", [None, "cosine"])
    @pytest.mark.parametrize(
        ("condition", "expected"),
        [({"n__gt": 2}, ["d6", "d4"]), ({"category": "tech"}, ["d4", "d3"])],
        ids=["n__gt", "category"],
    )
    async def test_an_indexed_column(
        self, kind: str, dist: str | None, condition: dict[str, Any], expected: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with _vec_models(kind) as client:
            await _seed_ranked(client)
            major = await _major(client)
            queryset = VecRanked.objects().filter(**condition).similar_to("embedding", _Q, 2, dist=dist)
            statement = _compiled(queryset, knn_prefilter=major < 3)[0][0]
            calls = _spy(monkeypatch, client)
            docs = await queryset.exec()
            sent = _calls(calls)
        assert sent == [("version", None), *_query(statement)]
        assert statement.startswith("{ LET $_sq0 = (SELECT VALUE id FROM vec_ranked WHERE ") == (major < 3)
        assert _ids(docs) == expected

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_groups_exists_and_count_through_the_let(self, kind: str) -> None:
        async with _vec_models(kind) as client:
            await _seed_ranked(client)

            def nearest() -> Any:
                return VecRanked.objects().filter(n__gt=2).similar_to("embedding", _Q, 2)

            groups = await nearest().values("category").annotate(c=Count()).exec()
            exists = await nearest().exists()
            count = await nearest().count()
            exact = await VecRanked.objects().filter(n__gt=2).similar_to("embedding", _Q, 2, dist="cosine").count()
        assert sorted(groups, key=lambda row: row["category"]) == [{"category": "news", "c": 1}, {"category": "tech", "c": 1}]
        assert (exists, count, exact) == (True, 2, 2)

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize("dist", [None, "cosine"])
    async def test_a_linked_record_s_field(self, kind: str, dist: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _vec_models(kind) as client:
            await _seed_owned(client)
            major = await _major(client)
            queryset = VecOwned.objects().filter(**{"owner.team": "x"}).similar_to("embedding", _Q, 2, dist=dist)
            statement = _compiled(queryset, knn_prefilter=major < 3)[0][0]
            calls = _spy(monkeypatch, client)
            docs = await queryset.exec()
            sent = _calls(calls)
        assert sent == [("version", None), *_query(statement)]
        assert statement.startswith("{ LET $_sq0 = (SELECT VALUE id FROM vec_owned WHERE owner.team = $_f0); ") == (major < 3)
        assert _ids(docs) == ["d1", "d2"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_subquery_holding_similar_to_and_a_condition(self, kind: str) -> None:
        async with _vec_models(kind) as client:
            await _seed(client)
            docs = await _k8_tech().exec()
            count = await _k8_tech().count()
        assert sorted(_ids(docs)) == ["d3", "d4"]
        assert count == 2


#: The search annotations of the E2E case: the score, the highlight and the offsets of ``search()``'s first field.
_ANN = {"s": SearchScore(0), "h": SearchHighlight("<b>", "</b>", 0), "o": SearchOffsets(0)}


def _annotated_knn(dist: str | None = None) -> Any:
    """``search(title="programming")`` with ``similar_to("embedding", _Q2, 2)`` and ``_ANN``: the matches d3, d4."""
    return VecDoc.objects().search(title="programming").similar_to("embedding", _Q2, 2, dist=dist).annotate(**_ANN)


class TestSearchAnnotationsWithKnnE2E:
    """The search annotations next to ``similar_to()``, on every engine: on SurrealDB 3.x each of the nearest matches
    carries the score, highlight and offsets the plain search gives it; below 3 ``exec()``, ``first()`` and a cached
    read refuse them before any statement is sent, while ``exists()`` and ``count()`` answer."""

    @pytest.mark.usefixtures("empty_cache")
    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_annotations(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _vec_models(kind) as client:
            await _seed(client)
            line = await _line(client)
            plain = {str(doc.id): doc for doc in await VecDoc.objects().search(title="programming").annotate(**_ANN).exec()}
            if line[0] >= 3:
                docs = await _annotated_knn().exec()
                exact = await _annotated_knn("cosine").exec()
                exists = await _annotated_knn().exists()
                count = await _annotated_knn().count()
            else:
                calls = _spy(monkeypatch, client)
                refused = []
                for read in (lambda: _annotated_knn().exec(), lambda: _annotated_knn().first()):
                    with pytest.raises(SurrealDbError) as raised:
                        await read()
                    refused.append(str(raised.value))
                with pytest.raises(SurrealDbError) as raised:
                    await _annotated_knn().cache(ttl=30).exec()
                refused.append(str(raised.value))
                before = _calls(calls)
                entries = QueryCache.stats()["entries"]
                exists = await _annotated_knn().exists()
                sent = _calls(calls)[len(before) :]
                count = await _annotated_knn().count()
        assert sorted(plain) == ["d3", "d4"]
        assert (exists, count) == (True, 2)
        if line[0] < 3:
            assert refused == [_VS1.format("SearchScore")] * 3
            assert before == [("version", None)], "the line is read, then nothing is sent"
            assert entries == 0
            assert sent == _query(_K18B)
            return
        if line == (3, 1):
            # README row "similar_to() and search() in one queryset": 3.1's index search gives no distance.
            assert sorted(_ids(docs)) == ["d3", "d4"]
            assert _distances(docs) == [None, None]
        else:
            assert _ids(docs) == ["d3", "d4"]
            assert _distances(docs) == pytest.approx([0.0, _cosine("d4", _Q2)], rel=1e-5)
        assert _ids(exact) == ["d3", "d4"]
        assert _distances(exact) == pytest.approx([0.0, _cosine("d4", _Q2)], rel=1e-9)
        for doc in [*docs, *exact]:
            same = plain[str(doc.id)]
            assert doc.s == pytest.approx(same.s)
            assert (doc.h, doc.o) == (same.h, same.o)


# === Bulk writes by the ids of the nearest records ==================================================================

#: K12: ``similar_to()`` alone and ``bulk_update(n=9)``: the ids of the nearest in a ``LET``, the same on every line.
_K12 = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_doc WHERE embedding <|1,100|> $_f0); (UPDATE vec_doc SET n = $_v0 WHERE id "
    "IN $_sq0) };"
)
#: K12b: a range next to an exact search and ``bulk_delete()``, in the form for SurrealDB 3.x, then below 3.
_K12B_3X = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_doc WHERE n > $_f0 AND embedding <|2,COSINE|> $_f1); (DELETE vec_doc WHERE "
    "id IN $_sq0 RETURN BEFORE) };"
)
_K12B = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_doc WHERE n > $_f0); LET $_sq1 = (SELECT VALUE id FROM vec_doc WHERE id IN "
    "$_sq0 AND embedding <|2,COSINE|> $_f1); (DELETE vec_doc WHERE id IN $_sq1 RETURN BEFORE) };"
)
#: K12c: a comparison next to the index search and ``patch()``, in the form below SurrealDB 3.
_K12C = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_doc WHERE category = $_f0); LET $_sq1 = (SELECT VALUE id FROM vec_doc WHERE "
    "id IN $_sq0 AND embedding <|1,100|> $_f1); (UPDATE vec_doc PATCH $_ops WHERE id IN $_sq1) };"
)
#: K12d: K12's write once SurrealDB 3.x confirmed that the table does not exist.
_K12D = "{ LET $_sq0 = []; (UPDATE vec_doc SET n = $_v0 WHERE id IN $_sq0) };"
#: An id subquery next to the KNN: its ``LET`` first, then the ids of the nearest; with its table missing, ``[]``.
_K12_BY_SUBQUERY = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_ranked WHERE n > $_f0); LET $_sq1 = (SELECT VALUE id FROM vec_doc WHERE id "
    "IN $_sq0 AND embedding <|2,100|> $_f1); (UPDATE vec_doc SET n = $_v0 WHERE id IN $_sq1) };"
)
_K12_BY_EMPTY_SUBQUERY = (
    "{ LET $_sq0 = []; LET $_sq1 = (SELECT VALUE id FROM vec_doc WHERE id IN $_sq0 AND embedding <|2,100|> $_f0); "
    "(UPDATE vec_doc SET n = $_v0 WHERE id IN $_sq1) };"
)

#: ``VecRanked``'s writes: ``filter(n__gt=2).similar_to("embedding", _Q, 2)`` (and ``dist="cosine"``) with
#: ``bulk_update(hit=True)``, in the form for SurrealDB 3.x and in the form below 3.
_RANGE_INLINE = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_ranked WHERE n > $_f0 AND embedding <|2,100|> $_f1); (UPDATE vec_ranked "
    "SET hit = $_v0 WHERE id IN $_sq0) };"
)
_RANGE_PREFILTERED = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_ranked WHERE n > $_f0); LET $_sq1 = (SELECT VALUE id FROM vec_ranked WHERE "
    "id IN $_sq0 AND embedding <|2,100|> $_f1); (UPDATE vec_ranked SET hit = $_v0 WHERE id IN $_sq1) };"
)
_RANGE_EXACT_INLINE = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_ranked WHERE n > $_f0 AND embedding <|2,COSINE|> $_f1); (UPDATE "
    "vec_ranked SET hit = $_v0 WHERE id IN $_sq0) };"
)
_RANGE_EXACT_PREFILTERED = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_ranked WHERE n > $_f0); LET $_sq1 = (SELECT VALUE id FROM vec_ranked WHERE "
    "id IN $_sq0 AND embedding <|2,COSINE|> $_f1); (UPDATE vec_ranked SET hit = $_v0 WHERE id IN $_sq1) };"
)
#: ``similar_to("embedding", _Q, 2, dist="cosine").bulk_delete()``: the same on every line.
_DELETE_NEAREST = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_ranked WHERE embedding <|2,COSINE|> $_f0); (DELETE vec_ranked WHERE id IN "
    "$_sq0 RETURN BEFORE) };"
)
#: ``filter(category="tech").similar_to("embedding", _Q, 1).patch(…)``, in the form for SurrealDB 3.x, then below 3.
_PATCH_INLINE = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_ranked WHERE category = $_f0 AND embedding <|1,100|> $_f1); (UPDATE "
    "vec_ranked PATCH $_ops WHERE id IN $_sq0) };"
)
_PATCH_PREFILTERED = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_ranked WHERE category = $_f0); LET $_sq1 = (SELECT VALUE id FROM vec_ranked "
    "WHERE id IN $_sq0 AND embedding <|1,100|> $_f1); (UPDATE vec_ranked PATCH $_ops WHERE id IN $_sq1) };"
)
#: ``similar_to("embedding", _Q, 2).bulk_update(hit=True)``: the same on every line; then with the table missing.
_UPDATE_NEAREST = (
    "{ LET $_sq0 = (SELECT VALUE id FROM vec_ranked WHERE embedding <|2,100|> $_f0); (UPDATE vec_ranked SET hit = $_v0 "
    "WHERE id IN $_sq0) };"
)
_UPDATE_NONE = "{ LET $_sq0 = []; (UPDATE vec_ranked SET hit = $_v0 WHERE id IN $_sq0) };"

#: The variables of ``VecRanked``'s range writes.
_RANGE_VARIABLES = {"_f0": 2, "_f1": [1.0, 0.0, 0.0], "_v0": True}
#: The operations of the ``patch()`` writes.
_HIT = [{"op": "replace", "path": "/hit", "value": True}]


def _the_write(statement: str) -> str:
    """The write of a compiled block: what follows its last ``LET …;``."""
    return statement.rsplit("LET ", 1)[1].split(";", 1)[1]


async def _captured_write(
    monkeypatch: pytest.MonkeyPatch,
    queryset: Any,
    call: Callable[[Any], Awaitable[int]],
    *,
    empty: frozenset[str] = frozenset(),
    knn_prefilter: bool = False,
) -> tuple[tuple[str, dict[str, Any]], SubqueryCompiler]:
    """*call* (a bulk write of *queryset*) with a fake ``_run_compiled``, which compiles the write once with a new
    compiler (*empty*, *knn_prefilter* given), sends nothing and returns ``[]``: the statement and that compiler."""
    captured: list[tuple[tuple[str, dict[str, Any]], SubqueryCompiler]] = []

    async def run_compiled(
        compile_statement: Callable[[SubqueryCompiler], tuple[str, dict[str, Any]]], *, write: bool = False
    ) -> Any:
        assert write is True
        compiler = queryset._subquery_compiler(empty, knn_prefilter=knn_prefilter)
        captured.append((compile_statement(compiler), compiler))
        return []

    monkeypatch.setattr(queryset, "_run_compiled", run_compiled)
    assert await call(queryset) == 0
    assert len(captured) == 1
    statement, compiler = captured[0]
    if queryset._similar is not None:
        assert "<|" not in _the_write(statement[0]), "a write never carries the KNN operator"
        assert " WHERE id IN $_sq" in _the_write(statement[0])
    return statement, compiler


def _ranged(dist: str | None = None) -> Any:
    """``VecRanked.objects().filter(n__gt=2).similar_to("embedding", _Q, 2, dist=dist)``: d6, d4."""
    return VecRanked.objects().filter(n__gt=2).similar_to("embedding", _Q, 2, dist=dist)


async def _hits(client: Any) -> list[str]:
    """The keys of the ``vec_ranked`` records whose ``hit`` is true, sorted."""
    rows = await client.query("SELECT VALUE id FROM vec_ranked WHERE hit = true;", {})
    return sorted(str(row.id) for row in rows)


async def _ranked_left(client: Any) -> list[str]:
    """The keys of the ``vec_ranked`` records, sorted."""
    rows = await client.query("SELECT VALUE id FROM vec_ranked;", {})
    return sorted(str(row.id) for row in rows)


def _added(monkeypatch: pytest.MonkeyPatch, tx: Any) -> list[tuple[str, dict[str, Any]]]:
    """Record each ``(statement, variables)`` *tx*'s ``add()`` receives (and let it through)."""
    added: list[tuple[str, dict[str, Any]]] = []
    real = tx.add

    async def add(statement: str, variables: dict[str, Any] | None = None) -> Any:
        added.append((statement, dict(variables or {})))
        return await real(statement, variables)

    monkeypatch.setattr(tx, "add", add)
    return added


class TestWriteByIds:
    """``bulk_update()``, ``bulk_delete()`` and ``patch()`` of a ``similar_to()`` queryset write the records whose ids
    the KNN ``SELECT`` returns, bound in a ``LET`` of the statement's block — the other conditions' ``LET`` s first,
    below SurrealDB 3 the pre-filter's too —, so the write itself never carries the KNN operator; the ``LET`` reads the
    model's table, so a table SurrealDB 3.x confirms missing binds it to ``[]``. The line is read once per write, and
    only for a KNN next to another condition; a transaction queues the block of its line."""

    @pytest.mark.parametrize("knn_prefilter", [False, True])
    async def test_k12_similar_to_alone(self, knn_prefilter: bool, monkeypatch: pytest.MonkeyPatch) -> None:
        queryset = VecDoc.objects().similar_to("embedding", _Q, 1)
        statement, compiler = await _captured_write(
            monkeypatch, queryset, lambda qs: qs.bulk_update(n=9), knn_prefilter=knn_prefilter
        )
        assert statement == (_K12, {"_f0": [1.0, 0.0, 0.0], "_v0": 9})
        assert compiler.knn_conditions is False
        assert compiler.tables == {"vec_doc"}

    async def test_k12b_a_range_next_to_an_exact_search(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def k12b() -> Any:
            return VecDoc.objects().filter(n__gt=2).similar_to("embedding", _Q, 2, dist="cosine")

        variables = {"_f0": 2, "_f1": [1.0, 0.0, 0.0]}
        inline, compiler = await _captured_write(monkeypatch, k12b(), lambda qs: qs.bulk_delete())
        assert inline == (_K12B_3X, variables)
        assert compiler.knn_conditions is True
        assert compiler.tables == {"vec_doc"}
        prefiltered, compiler = await _captured_write(monkeypatch, k12b(), lambda qs: qs.bulk_delete(), knn_prefilter=True)
        assert prefiltered == (_K12B, variables)
        assert compiler.knn_conditions is True
        assert compiler.tables == {"vec_doc"}

    async def test_k12c_patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        operations = [{"op": "replace", "path": "/n", "value": 9}]
        queryset = VecDoc.objects().filter(category="tech").similar_to("embedding", _Q, 1)
        statement, compiler = await _captured_write(monkeypatch, queryset, lambda qs: qs.patch(operations), knn_prefilter=True)
        assert statement == (_K12C, {"_f0": "tech", "_f1": [1.0, 0.0, 0.0], "_ops": operations})
        assert compiler.tables == {"vec_doc"}

    async def test_k12d_a_missing_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        queryset = VecDoc.objects().similar_to("embedding", _Q, 1)
        statement, compiler = await _captured_write(
            monkeypatch, queryset, lambda qs: qs.bulk_update(n=9), empty=frozenset({"vec_doc"})
        )
        assert statement == (_K12D, {"_v0": 9})
        assert compiler.tables == {"vec_doc"}

    @pytest.mark.parametrize("knn_prefilter", [False, True])
    async def test_an_id_subquery_s_let_comes_first(self, knn_prefilter: bool, monkeypatch: pytest.MonkeyPatch) -> None:
        def by_subquery() -> Any:
            ranked = Subquery(VecRanked.objects().filter(n__gt=2))
            return VecDoc.objects().filter(id__in=ranked).similar_to("embedding", _Q, 2)

        statement, compiler = await _captured_write(
            monkeypatch, by_subquery(), lambda qs: qs.bulk_update(n=9), knn_prefilter=knn_prefilter
        )
        assert statement == (_K12_BY_SUBQUERY, {"_f0": 2, "_f1": [1.0, 0.0, 0.0], "_v0": 9})
        assert compiler.knn_conditions is False
        assert compiler.tables == {"vec_ranked", "vec_doc"}
        emptied, compiler = await _captured_write(
            monkeypatch, by_subquery(), lambda qs: qs.bulk_update(n=9), empty=frozenset({"vec_ranked"})
        )
        assert emptied == (_K12_BY_EMPTY_SUBQUERY, {"_f0": [1.0, 0.0, 0.0], "_v0": 9})
        assert compiler.tables == {"vec_ranked", "vec_doc"}

    async def test_without_similar_to_the_write_is_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        queryset = VecDoc.objects().filter(category="tech")
        statement, compiler = await _captured_write(monkeypatch, queryset, lambda qs: qs.bulk_update(n=9))
        assert statement == ("UPDATE vec_doc SET n = $_v0 WHERE category = $_f0;", {"_f0": "tech", "_v0": 9})
        assert compiler.tables == set()

    @pytest.mark.parametrize(
        ("call", "name"),
        [
            (lambda qs: qs.bulk_update(title="x"), "bulk_update()"),
            (lambda qs: qs.bulk_delete(), "bulk_delete()"),
            (lambda qs: qs.patch(_HIT), "patch()"),
        ],
        ids=["bulk_update", "bulk_delete", "patch"],
    )
    async def test_a_view_model_s_write_is_refused_first(self, call: Any, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
        queryset = VecView.objects().filter(category="tech").similar_to("embedding", _Q, 2)
        compiled: list[Any] = []

        async def run_compiled(compile_statement: Any, *, write: bool = False) -> Any:
            compiled.append(compile_statement)
            return []

        monkeypatch.setattr(queryset, "_run_compiled", run_compiled)
        with pytest.raises(TypeError) as raised:
            await call(queryset)
        assert str(raised.value) == view_write_refused("vec_view", f"VecView.objects().{name}")
        assert compiled == []

    @pytest.mark.parametrize(("major", "first"), [(2, _RANGE_PREFILTERED), (3, _RANGE_INLINE)], ids=["2x", "3x"])
    async def test_the_line_is_decided_once_per_write(self, major: int, first: str, monkeypatch: pytest.MonkeyPatch) -> None:
        reads = _line_answers(monkeypatch, major)
        queryset = _ranged()
        executed = _executed(monkeypatch, queryset)
        assert await queryset.bulk_update(hit=True) == 0
        # The fake confirms the table missing: the write is compiled again, its ids bound to [], in the same form.
        assert executed == [(first, _RANGE_VARIABLES), (_UPDATE_NONE, {"_v0": True})]
        assert len(reads) == 1

    async def test_no_line_without_a_condition(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reads = _line_answers(monkeypatch, 2)
        alone = VecRanked.objects().similar_to("embedding", _Q, 2)
        by_ids = VecRanked.objects().filter(id__in=Subquery(VecRanked.objects().filter(n__gt=2)))
        for call in (
            lambda: alone.bulk_update(hit=True),
            lambda: alone.bulk_delete(),
            lambda: alone.patch(_HIT),
            lambda: by_ids.similar_to("embedding", _Q, 2).bulk_update(hit=True),
        ):
            for queryset in (alone, by_ids):
                _executed(monkeypatch, queryset, [])
            await call()
        assert reads == []

    async def test_a_transaction_queues_the_block_of_its_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reads = _line_answers(monkeypatch, 2)
        tx = _ReadTx()
        written = await VecRanked.objects(tx=tx).filter(n__gt=2).similar_to("embedding", _Q, 2).bulk_update(hit=True)
        assert written == 0
        assert tx.added == [(_RANGE_PREFILTERED, _RANGE_VARIABLES)]
        assert tx.reads == []
        assert len(reads) == 1


class TestWriteByIdsE2E:
    """The bulk writes of a ``similar_to()`` queryset on every engine: they write exactly the records the same read
    returns — next to a range on an indexed column, with an exact search, with a comparison and alone — and return
    their number; the spy shows one block whose write goes by the ids of the nearest, the other conditions selected
    first below SurrealDB 3 and inline on 3.x, the line read only for a write with a condition. A table that does not
    exist is written as empty."""

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize(
        ("dist", "inline", "prefiltered"),
        [(None, _RANGE_INLINE, _RANGE_PREFILTERED), ("cosine", _RANGE_EXACT_INLINE, _RANGE_EXACT_PREFILTERED)],
        ids=["index", "exact"],
    )
    async def test_bulk_update_next_to_an_indexed_range(
        self, kind: str, dist: str | None, inline: str, prefiltered: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with _vec_models(kind) as client:
            await _seed_ranked(client)
            major = await _major(client)
            calls = _spy(monkeypatch, client)
            written = await _ranged(dist).bulk_update(hit=True)
            sent = _calls(calls)
            hits = await _hits(client)
        assert written == 2
        assert hits == ["d4", "d6"]
        assert sent == [("version", None), *_query(prefiltered if major < 3 else inline)]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_bulk_delete_of_the_exact_nearest(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _vec_models(kind) as client:
            await _seed_ranked(client)
            calls = _spy(monkeypatch, client)
            deleted = await VecRanked.objects().similar_to("embedding", _Q, 2, dist="cosine").bulk_delete()
            sent = _calls(calls)
            left = await _ranked_left(client)
        assert deleted == 2
        assert left == ["d3", "d4", "d5", "d6"]
        assert sent == _query(_DELETE_NEAREST)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_patch_next_to_an_indexed_column(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _vec_models(kind) as client:
            await _seed_ranked(client)
            major = await _major(client)
            calls = _spy(monkeypatch, client)
            patched = await VecRanked.objects().filter(category="tech").similar_to("embedding", _Q, 1).patch(_HIT)
            sent = _calls(calls)
            hits = await _hits(client)
        assert patched == 1
        assert hits == ["d4"]
        assert sent == [("version", None), *_query(_PATCH_PREFILTERED if major < 3 else _PATCH_INLINE)]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_bulk_update_of_the_nearest(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _vec_models(kind) as client:
            await _seed_ranked(client)
            calls = _spy(monkeypatch, client)
            written = await VecRanked.objects().similar_to("embedding", _Q, 2).bulk_update(hit=True)
            sent = _calls(calls)
            hits = await _hits(client)
        assert written == 2
        assert hits == ["d1", "d2"]
        assert sent == _query(_UPDATE_NEAREST)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_table_that_does_not_exist(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """SurrealDB 3.x refuses the ``LET`` on a table that does not exist: confirmed missing, the ids are bound to
        ``[]`` and the write is sent again, as for every subquery table; 2.x and the embedded engine answer none."""
        async with _vec_models(kind) as client:
            major = await _major(client)
            calls = _spy(monkeypatch, client)
            written = await VecRanked.objects().similar_to("embedding", _Q, 2).bulk_update(hit=True)
            sent = _calls(calls)
        assert written == 0
        if major >= 3:
            assert sent == [*_query(_UPDATE_NEAREST), *_query(TABLE_PROBE), *_query(_UPDATE_NONE)]
        else:
            assert sent == _query(_UPDATE_NEAREST)


class TestWriteByIdsTxE2E:
    """A bulk write of a ``similar_to()`` queryset next to an indexed range in a transaction: in an interactive one
    (SurrealDB 3.x over WebSocket) it runs at once and returns its count; in a buffered one (HTTP, SurrealDB 2.x, the
    embedded engine) the block of the line — both ``LET`` s below 3 — is queued and runs at the commit. Either way the
    commit leaves exactly the nearest records the read gives written."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_write_in_a_transaction(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _vec_models(kind) as client:
            await _seed_ranked(client)
            major = await _major(client)
            interactive = kind == "ws" and major >= 3
            async with SurrealDBConnectionManager.transaction("vec") as tx:
                assert tx.is_interactive is interactive
                added = _added(monkeypatch, tx)
                ranged = VecRanked.objects(tx=tx).filter(n__gt=2).similar_to("embedding", _Q, 2)
                written = await ranged.bulk_update(hit=True)
            hits = await _hits(client)
        assert added == [(_RANGE_PREFILTERED if major < 3 else _RANGE_INLINE, _RANGE_VARIABLES)]
        assert written == (2 if interactive else 0)
        assert hits == ["d4", "d6"]


#: SurrealDB's refusal of a query vector of two numbers for a vector index of dimension 3, the same on every engine.
_DIMENSION_ERROR = "Incorrect vector dimension (2). Expected a vector of 3 dimension."
#: The ORM's error for that refusal on the field ``embedding``.
_VD1 = "similar_to(): the vector has 2 dimensions; the vector index on field 'embedding' expects 3."
#: The query vector of two numbers.
_SHORT = [1.0, 0.0]


class VecLoose(BaseSurrealModel):
    """A schemaless model whose vectors are plain lists: its vector index is defined by hand, so the ORM does not know
    the dimension."""

    model_config = SurrealConfigDict(table_name="vec_loose")

    id: str | None = None
    title: str = ""
    category: str = ""
    hit: bool = False
    embedding: list[float] | None = None


#: ``VecLoose``'s vector index, written by hand: dimension 3.
_LOOSE_INDEX = "DEFINE INDEX vec_loose_embedding ON vec_loose FIELDS embedding HNSW DIMENSION 3 DIST COSINE TYPE F32;"


async def _seed_loose(client: Any) -> None:
    """``VecLoose``'s index, then the six records."""
    await _apply_ddl(client, f"{_LOOSE_INDEX} {_seed_statements('vec_loose')}")


class _DimensionClient:
    """A client whose every query fails with SurrealDB's dimension error: records what it was sent."""

    def __init__(self) -> None:
        self.error = InternalError("Internal", _DIMENSION_ERROR)
        self.sent: list[str] = []

    async def query(self, statement: str, variables: Any = None) -> Any:
        self.sent.append(statement)
        raise self.error

    async def version(self) -> str:
        pytest.fail("a statement without another condition reads no line")


def _dimension_client(monkeypatch: pytest.MonkeyPatch) -> _DimensionClient:
    """Every read acquires one :class:`_DimensionClient`, returned."""
    client = _DimensionClient()

    async def get_client(*args: Any, **kwargs: Any) -> Any:
        return client

    monkeypatch.setattr(SurrealDBConnectionManager, "get_client", get_client)
    return client


class _DimensionTx:
    """A transaction whose every read fails with SurrealDB's dimension error, noted among its failures as a
    transaction notes the error of a statement run in it."""

    def __init__(self) -> None:
        self.error = InternalError("Internal", _DIMENSION_ERROR)
        self.failures: list[BaseException] = []

    @property
    def is_interactive(self) -> bool:
        return True

    async def run_read(self, sql: str, variables: dict[str, Any] | None = None) -> Any:
        self.failures.append(self.error)
        raise self.error

    def _absorb_failure(self, error: BaseException) -> None:
        self.failures = [failure for failure in self.failures if failure is not error]

    def _note_failure(self, error: BaseException) -> None:
        self.failures.append(error)


class TestDimensionRefusal:
    """SurrealDB's refusal of a query vector whose length is not its vector index's dimension, read without a request:
    ``SurrealDbError`` names the statement's vector field — several as "one of the fields" — and both lengths read from
    the server's text, chained to the server's error; any other error, an unreadable text or a statement without a
    vector search is relayed. In a transaction the ORM's error replaces the server's among its failures."""

    def test_the_server_s_refusal(self) -> None:
        error = InternalError("Internal", _DIMENSION_ERROR)
        assert vector.is_dimension_error(error) is True
        refusal = vector.dimension_refusal(error, {"embedding": "embedding"})
        assert type(refusal) is SurrealDbError
        assert str(refusal) == _VD1

    def test_the_field_as_given_and_the_lengths_read(self) -> None:
        error = InternalError("Internal", "Incorrect vector dimension (0). Expected a vector of 1536 dimension.")
        assert str(vector.dimension_refusal(error, {"summary": "summary_vec"})) == (
            "similar_to(): the vector has 0 dimensions; the vector index on field 'summary_vec' expects 1536."
        )

    def test_several_fields(self) -> None:
        error = InternalError("Internal", _DIMENSION_ERROR)
        fields = {"embedding": "embedding", "summary": "summary_vec", "meta.vec": "embedding"}
        assert str(vector.dimension_refusal(error, fields)) == (
            "similar_to(): the vector has 2 dimensions; the vector index on one of the fields 'embedding', "
            "'summary_vec' expects 3."
        )

    def test_any_other_error_is_relayed(self) -> None:
        fields = {"embedding": "embedding"}
        assert vector.is_dimension_error(ValueError(_DIMENSION_ERROR)) is False
        assert vector.dimension_refusal(ValueError(_DIMENSION_ERROR), fields) is None
        other = InternalError("Internal", "There was a problem with the database")
        assert vector.is_dimension_error(other) is False
        assert vector.dimension_refusal(other, fields) is None
        unreadable = InternalError("Internal", "Incorrect vector dimension (two).")
        assert vector.is_dimension_error(unreadable) is True
        assert vector.dimension_refusal(unreadable, fields) is None
        assert vector.dimension_refusal(InternalError("Internal", _DIMENSION_ERROR), {}) is None

    async def test_a_read_names_its_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _dimension_client(monkeypatch)
        with pytest.raises(SurrealDbError) as raised:
            await VecLoose.objects().similar_to("embedding", _SHORT).exec()
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == _VD1
        assert raised.value.__cause__ is client.error
        assert len(client.sent) == 1

    async def test_a_subquery_s_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _dimension_client(monkeypatch)
        inner = VecDoc.objects().similar_to("summary_vec", [1.0, 0.0, 0.0], 2)
        with pytest.raises(SurrealDbError) as raised:
            await VecRanked.objects().filter(id__in=Subquery(inner)).count()
        assert str(raised.value) == (
            "similar_to(): the vector has 2 dimensions; the vector index on field 'summary_vec' expects 3."
        )
        assert raised.value.__cause__ is client.error
        assert len(client.sent) == 1

    async def test_its_own_and_a_subquery_s_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _dimension_client(monkeypatch)
        inner = VecDoc.objects().similar_to("summary_vec", [1.0, 0.0, 0.0], 2)
        queryset = VecLoose.objects().filter(id__in=Subquery(inner)).similar_to("embedding", _SHORT)
        with pytest.raises(SurrealDbError) as raised:
            await queryset.exec()
        assert str(raised.value) == (
            "similar_to(): the vector has 2 dimensions; the vector index on one of the fields 'embedding', "
            "'summary_vec' expects 3."
        )
        assert raised.value.__cause__ is client.error

    async def test_a_write_by_the_ids_of_the_nearest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _dimension_client(monkeypatch)
        with pytest.raises(SurrealDbError) as raised:
            await VecLoose.objects().similar_to("embedding", _SHORT).bulk_update(hit=True)
        assert str(raised.value) == _VD1
        assert raised.value.__cause__ is client.error
        assert len(client.sent) == 1

    async def test_a_query_without_a_vector_search_relays_the_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _dimension_client(monkeypatch)
        with pytest.raises(InternalError) as raised:
            await VecLoose.objects().filter(category="tech").exec()
        assert raised.value is client.error

    async def test_a_read_in_a_transaction(self) -> None:
        tx = _DimensionTx()
        with pytest.raises(SurrealDbError) as raised:
            await VecLoose.objects(tx=tx).similar_to("embedding", _SHORT).exec()
        assert str(raised.value) == _VD1
        assert raised.value.__cause__ is tx.error
        assert tx.failures == [raised.value]


class TestDimensionE2E:
    """A query vector of another length than the vector index's dimension, on a model that does not declare the
    dimension (a hand-made index on a plain list field): SurrealDB's refusal becomes ``SurrealDbError`` naming the field
    and both lengths, caused by the server's error — a read and a count on every engine; a write by the ids of the
    nearest outside a transaction on SurrealDB 3.3, 2.x and the embedded engine, where SurrealDB 3.1 relays its own
    error from the ``LET`` — and nothing is written. A declared dimension refuses the vector before any request."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_read_and_a_count(self, kind: str) -> None:
        async with _vec_models(kind) as client:
            await _seed_loose(client)
            queryset = VecLoose.objects().similar_to("embedding", _SHORT)
            errors: list[BaseException] = []
            for call in (queryset.exec, queryset.count):
                with pytest.raises(SurrealDbError) as raised:
                    await call()
                errors.append(raised.value)
        assert [type(error) for error in errors] == [SurrealDbError, SurrealDbError]
        assert [str(error) for error in errors] == [_VD1, _VD1]
        for error in errors:
            assert isinstance(error.__cause__, ServerError)
            assert _DIMENSION_ERROR in str(error.__cause__)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_write_by_the_ids_of_the_nearest(self, kind: str) -> None:
        async with _vec_models(kind) as client:
            await _seed_loose(client)
            line = await _line(client)
            with pytest.raises((SurrealDbError, ServerError)) as raised:
                await VecLoose.objects().similar_to("embedding", _SHORT).bulk_update(hit=True)
            hits = await client.query("SELECT VALUE id FROM vec_loose WHERE hit = true;", {})
        assert hits == []
        if line == (3, 1):
            assert isinstance(raised.value, ServerError)
            assert "HNSW KNN search failed" in str(raised.value), "SurrealDB 3.1 fails the LET with its own error"
            return
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == _VD1
        assert isinstance(raised.value.__cause__, ServerError)
        assert _DIMENSION_ERROR in str(raised.value.__cause__)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_declared_dimension_refuses_before_any_request(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _vec_models(kind) as client:
            await _seed(client)
            calls = _spy(monkeypatch, client)
            with pytest.raises(ValueError) as raised:
                await VecDoc.objects().similar_to("embedding", _SHORT).exec()
            sent = _calls(calls)
        assert str(raised.value) == (
            "similar_to(): the vector has 2 dimensions; field 'embedding' holds vectors of 3 (VectorField[3])."
        )
        assert sent == []


async def _native_transactions(kind: str, client: Any) -> bool:
    """Whether ``transaction()`` on *kind* is interactive: WebSocket on SurrealDB 3.x."""
    return kind == "ws" and await _major(client) >= 3


#: The existing refusal of a read inside a buffered transaction.
_BUFFERED_READ = (
    "Reads inside a transaction require a WebSocket connection to SurrealDB 3.x (native interactive transactions); "
    "the current transaction is buffered (HTTP or SurrealDB 2.6.x)."
)


class TestTransactionsE2E:
    """``similar_to()`` in a transaction, on every engine: in an interactive one (SurrealDB 3.x over WebSocket) the
    vector search sees the transaction's own write, which the others see after the commit, and a read refused for its
    vector's dimension fails the commit, caused by the ORM's error; in a buffered one (HTTP, SurrealDB 2.x, the embedded
    engine) a read raises, as every read does there, and the write is applied at the commit."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_search_sees_the_transaction_s_write(self, kind: str) -> None:
        async with _vec_models(kind) as client:
            await _seed(client)
            interactive = await _native_transactions(kind, client)
            refused: pytest.ExceptionInfo[SurrealDbError] | None = None
            inside: list[str] | None = None
            outside: list[str] | None = None
            async with SurrealDBConnectionManager.transaction("vec") as tx:
                await VecDoc(id="d7", title="Cats and mice", category="pets", embedding=[0.95, 0.05, 0.0]).save(tx=tx)
                if interactive:
                    inside = _ids(await VecDoc.objects(tx=tx).similar_to("embedding", _Q, 3).exec())
                    outside = _ids(await VecDoc.objects().similar_to("embedding", _Q, 3).exec())
                else:
                    with pytest.raises(SurrealDbError) as refused:
                        await VecDoc.objects(tx=tx).similar_to("embedding", _Q, 3).exec()
            after = _ids(await VecDoc.objects().similar_to("embedding", _Q, 3).exec())
        assert after == ["d1", "d7", "d2"]
        if interactive:
            assert inside == ["d1", "d7", "d2"]
            assert outside == ["d1", "d2", "d6"], "the others see the write after the commit"
        else:
            assert refused is not None
            assert type(refused.value) is SurrealDbError
            assert str(refused.value) == _BUFFERED_READ

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_dimension_error_fails_the_commit(self, kind: str) -> None:
        """The read raises the ORM's error; the block that catches it and ends normally gets the commit's error,
        caused by that very error, and nothing is committed."""
        async with _vec_models(kind) as client:
            if not await _native_transactions(kind, client):
                pytest.skip("requires SurrealDB 3.x native transactions")
            await _seed_loose(client)
            read: pytest.ExceptionInfo[SurrealDbError] | None = None
            with pytest.raises(SurrealDbError) as commit:
                async with SurrealDBConnectionManager.transaction("vec") as tx:
                    with pytest.raises(SurrealDbError) as read:
                        await VecLoose.objects(tx=tx).similar_to("embedding", _SHORT).exec()
                    await VecLoose(id="d7", title="Cats and mice", embedding=[0.95, 0.05, 0.0]).save(tx=tx)
            stored = sorted(_ids(await VecLoose.objects().all()))
        assert read is not None
        assert type(read.value) is SurrealDbError
        assert str(read.value) == _VD1
        assert isinstance(read.value.__cause__, ServerError)
        assert str(commit.value) == (
            "The transaction was cancelled: a statement in it failed; nothing was committed (SurrealDbError)"
        )
        assert commit.value.__cause__ is read.value
        assert stored == ["d1", "d2", "d3", "d4", "d5", "d6"]


# === Pinned line facts ==============================================================================================

#: The README rows the line pins back (with ``_VECTOR_INDEX_ROW`` above).
_ROW_EF = "README row '`similar_to()` with `ef` below `limit`'"
_ROW_CONDITIONS = "README row '`similar_to()` with other conditions (`filter()`, `Q`, `nearby()`, `search()`)'"
_ROW_SEARCH = "README row '`similar_to()` and `search()` in one queryset'"
_ROW_WRITES = "README row '`bulk_update()`, `bulk_delete()`, `patch()` with `similar_to()`'"
_ROW_LINKED = "README row '`similar_to(dist=…)` with a condition on a linked record's field'"
_ROW_NO_INDEX = "README row '`similar_to()` on a field without a vector index'"

#: The raw statements of the pins, each a literal string, every value bound (``$q`` the query vector).
_PIN_K_ONLY = "SELECT VALUE id FROM vec_doc WHERE embedding <|2|> $q;"
_PIN_EF_BELOW_K = "SELECT VALUE id FROM vec_doc WHERE embedding <|4,3|> $q;"
_PIN_TWO_KNN = "SELECT VALUE id FROM vec_doc WHERE embedding <|2,100|> $q AND embedding <|2,100|> $p;"
_PIN_NO_INDEX = "SELECT id, vector::distance::knn() AS d FROM vec_loose WHERE embedding <|2,100|> $q;"
_PIN_FUNCTION = "SELECT VALUE id FROM vec_doc WHERE string::starts_with(title, $p) AND embedding <|2,100|> $q;"
_PIN_LINKED = "SELECT VALUE id FROM vec_owned WHERE owner.team = $t AND embedding <|2,COSINE|> $q;"
_PIN_RANGE = "SELECT VALUE id FROM vec_ranked WHERE n > $a AND embedding <|2,100|> $q;"
_PIN_RANGE_COUNT = "SELECT count() FROM vec_ranked WHERE n > $a AND embedding <|2,100|> $q GROUP ALL;"
_PIN_EQUAL_EXACT = "SELECT VALUE id FROM vec_ranked WHERE category = $c AND embedding <|2,COSINE|> $q;"
_PIN_RANGE_LET = (
    "{ LET $s = (SELECT VALUE id FROM vec_ranked WHERE n > $a); "
    "(SELECT VALUE id FROM vec_ranked WHERE id IN $s AND embedding <|2,100|> $q) };"
)
_PIN_RANGE_COUNT_LET = (
    "{ LET $s = (SELECT VALUE id FROM vec_ranked WHERE n > $a); "
    "(SELECT count() FROM vec_ranked WHERE id IN $s AND embedding <|2,100|> $q GROUP ALL) };"
)
_PIN_EQUAL_EXACT_LET = (
    "{ LET $s = (SELECT VALUE id FROM vec_ranked WHERE category = $c); "
    "(SELECT VALUE id FROM vec_ranked WHERE id IN $s AND embedding <|2,COSINE|> $q) };"
)
_PIN_MATCH = "SELECT id, vector::distance::knn() AS d FROM vec_doc WHERE title @0@ $t AND embedding <|2,100|> $q;"
_PIN_MATCH_EXACT = "SELECT VALUE id FROM vec_doc WHERE title @0@ $t AND embedding <|2,COSINE|> $q;"
_PIN_MATCH_LET = (
    "{ LET $s = (SELECT VALUE id FROM vec_doc WHERE title @0@ $t); "
    "(SELECT VALUE id FROM vec_doc WHERE id IN $s AND embedding <|2,100|> $q) };"
)
_PIN_SCORE_LET = (
    "{ LET $s = (SELECT VALUE id FROM vec_doc WHERE title @0@ $t); "
    "(SELECT VALUE search::score(0) FROM vec_doc WHERE id IN $s AND embedding <|2,100|> $q) };"
)

#: The scratch tables of the write pins: ``vec_pin_w`` with its vector index only, ``vec_pin_r`` with an index on ``n``
#: too; the writes in the ``UPDATE`` form only, and what they wrote.
_PIN_WRITE_DDL = (
    "DEFINE INDEX vec_pin_w_e ON vec_pin_w FIELDS embedding HNSW DIMENSION 3 DIST COSINE TYPE F32 EFC 150 M 12; "
    "DEFINE INDEX vec_pin_r_e ON vec_pin_r FIELDS embedding HNSW DIMENSION 3 DIST COSINE TYPE F32 EFC 150 M 12; "
    "DEFINE INDEX vec_pin_r_n ON vec_pin_r FIELDS n;"
)
_PIN_W_UPDATE = "UPDATE vec_pin_w SET hit = true WHERE embedding <|2,COSINE|> $q;"
_PIN_W_HITS = "SELECT VALUE id FROM vec_pin_w WHERE hit = true;"
_PIN_R_UPDATE = "UPDATE vec_pin_r SET hit = true WHERE n > $a AND embedding <|2,100|> $q;"
_PIN_R_HITS = "SELECT VALUE id FROM vec_pin_r WHERE hit = true;"
_PIN_R_SELECT = "SELECT VALUE id FROM vec_pin_r WHERE n > $a AND embedding <|2,100|> $q;"

#: A live query whose condition holds the KNN (a literal vector: SurrealDB 2.x carries no bound value into a live
#: query), and one on the whole table.
_LIVE_KNN = "LIVE SELECT * FROM vec_doc WHERE embedding <|2,100|> [1.0, 0.0, 0.0];"
_LIVE_ALL = "LIVE SELECT * FROM vec_doc;"

#: The six records' keys.
_SIX = ["d1", "d2", "d3", "d4", "d5", "d6"]


async def _keys(client: Any, statement: str, **variables: Any) -> list[str]:
    """The keys of the record ids *statement* (a ``SELECT VALUE id`` constant of these pins) answers, sorted."""
    rows = await client.query(statement, variables)
    return sorted(str(row.id) for row in rows)


@pytest.fixture
def _live_registry() -> Iterator[None]:
    """The live module's per-loop registries emptied after the test, as ``tests/test_live_queryset.py`` does."""
    from surreal_orm_lite import live

    yield
    live._SUBSCRIBERS.clear()
    live._RECENTLY_KILLED.clear()


async def _take(stream: Any, count: int, timeout: float = 10.0) -> list[Any]:
    """The next *count* items of *stream*, failing after *timeout* seconds (``tests/test_live_queryset.py``)."""

    async def _read() -> list[Any]:
        seen: list[Any] = []
        async for item in stream:
            seen.append(item)
            if len(seen) >= count:
                break
        return seen

    return await asyncio.wait_for(_read(), timeout=timeout)


async def _assert_silent(stream: Any, wait: float = 0.5) -> None:
    """Nothing more arrives (``tests/test_live_queryset.py``)."""
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(stream.__anext__(), timeout=wait)


class TestLinePinsE2E:
    """What each engine answers to KNN statements the ORM never sends, pinned with raw statements on six records, on
    every engine (the line under test over WebSocket and HTTP, and the embedded engine), each test naming the README
    row or sentence it backs. They are why the ORM writes ``<|K,EF|>`` with ``EF`` raised to ``K`` and one KNN operator
    per statement, keeps the other conditions next to the KNN on SurrealDB 3.x and selects their records first in a
    ``LET`` below 3, makes its bulk writes by the ids of the nearest, and refuses search annotations next to
    ``similar_to()`` below 3 and a live query holding the KNN. Each line's answer is asserted, 3.1's where it differs.
    """

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_operator_without_ef(self, kind: str) -> None:
        """README row "Vector index (``Index(hnsw=True)``, ``define_indexes()``)": ``<|K|>`` is the operator of the
        ``MTREE`` index ``similar_to()`` does not use. On an HNSW index SurrealDB 3.x refuses it, naming ``<|k, EF|>``;
        2.x and the embedded engine answer no record. The ORM writes ``<|K,EF|>`` on every line."""
        async with _vec_models(kind) as client:
            await _seed(client)
            major = await _major(client)
            refused: pytest.ExceptionInfo[ServerError] | None = None
            answered: list[str] | None = None
            if major >= 3:
                with pytest.raises(ServerError) as refused:
                    await client.query(_PIN_K_ONLY, {"q": _Q})
            else:
                answered = await _keys(client, _PIN_K_ONLY, q=_Q)
        if major >= 3:
            assert refused is not None
            assert "Use `<|k, EF|>` against an HNSW index" in str(refused.value), (
                f"{_VECTOR_INDEX_ROW}: SurrealDB 3.x refuses the MTREE operator"
            )
        else:
            assert answered == [], f"{_VECTOR_INDEX_ROW}: 2.x and the embedded engine answer no record from HNSW"

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_ef_below_k(self, kind: str) -> None:
        """README row "``similar_to()`` with ``ef`` below ``limit``": with ``K`` 4 and ``EF`` 3, 2.x and the embedded
        engine return at most ``EF`` records, the three nearest, in every index build; SurrealDB 3.x returns the four
        nearest in most builds, and 3.3 the three nearest in a few (SurrealDB draws the HNSW levels at random, so the
        answer depends on the build). The ORM writes ``ef`` raised to ``limit``: the four nearest on every line."""
        async with _vec_models(kind) as client:
            await _seed(client)
            major = await _major(client)
            raw = await _keys(client, _PIN_EF_BELOW_K, q=_Q)
            docs = await VecDoc.objects().similar_to("embedding", _Q, 4, ef=3).exec()
        if major >= 3:
            assert raw in (["d1", "d2", "d4", "d6"], ["d1", "d2", "d6"]), (
                f"{_ROW_EF}: SurrealDB 3.x returns K records, or at most EF in some index builds"
            )
        else:
            assert raw == ["d1", "d2", "d6"], f"{_ROW_EF}: 2.x and the embedded engine return at most EF records"
        assert _ids(docs) == ["d1", "d2", "d6", "d4"], f"{_ROW_EF}: the ORM writes ef raised to limit"

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_two_knn_operators(self, kind: str) -> None:
        """README row "``similar_to()`` with other conditions (``filter()``, ``Q``, ``nearby()``, ``search()``)": a
        second KNN operator next to the first is no condition SurrealDB applies — 3.x returns the first operator's
        neighbours, 2.x and the embedded engine no record. The ORM writes one KNN operator per statement: a second
        ``similar_to()`` replaces the first."""
        async with _vec_models(kind) as client:
            await _seed(client)
            major = await _major(client)
            keys = await _keys(client, _PIN_TWO_KNN, q=_Q, p=_Q2)
        if major >= 3:
            assert keys == ["d1", "d2"], f"{_ROW_CONDITIONS}: SurrealDB 3.x returns the first operator's neighbours"
        else:
            assert keys == [], f"{_ROW_CONDITIONS}: 2.x and the embedded engine answer no record"

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_field_without_a_vector_index(self, kind: str) -> None:
        """README row "``similar_to()`` on a field without a vector index" and section 37's ``_S37_NO_INDEX``: no
        record and no error on SurrealDB 3.3, 2.x and the embedded engine, through the ORM too; 3.1 answers every
        record, with no distance. ``dist=`` is an exact scan that needs no index: the two nearest on every line."""
        async with _vec_models(kind) as client:
            await _apply_ddl(client, _seed_statements("vec_loose"))
            line = await _line(client)
            raw = await client.query(_PIN_NO_INDEX, {"q": _Q})
            docs = await VecLoose.objects().similar_to("embedding", _Q, 2).exec()
            exact = await VecLoose.objects().similar_to("embedding", _Q, 2, dist="cosine").exec()
        if line == (3, 1):
            assert {str(row["id"].id): row["d"] for row in raw} == dict.fromkeys(_SIX), (
                f"{_ROW_NO_INDEX}: 3.1 answers every record, with no distance"
            )
            assert sorted(_ids(docs)) == _SIX, f"{_ROW_NO_INDEX}: 3.1 answers every record"
            assert _distances(docs) == [None] * 6, f"{_ROW_NO_INDEX}: 3.1 answers no distance"
        else:
            assert raw == [], f"{_ROW_NO_INDEX}: no record, no error"
            assert docs == [], f"{_ROW_NO_INDEX}: no record, no error"
        assert _ids(exact) == ["d1", "d2"], "README section 37, _S37_NO_INDEX: dist= needs no index"

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_function_call_next_to_the_knn(self, kind: str) -> None:
        """README row "``similar_to()`` with other conditions (``filter()``, ``Q``, ``nearby()``, ``search()``)": next
        to a condition holding a function call (``string::starts_with()``, what ``title__startswith`` writes), an HNSW
        search answers the two nearest matching records on SurrealDB 3.x, too few or none on 2.x and the embedded
        engine. The ORM, which selects those records first below 3, answers the two on every line."""
        async with _vec_models(kind) as client:
            await _seed(client)
            major = await _major(client)
            raw = await _keys(client, _PIN_FUNCTION, p="Cats", q=_Q)
            docs = await VecDoc.objects().filter(title__startswith="Cats").similar_to("embedding", _Q, 2).exec()
        if major >= 3:
            assert raw == ["d1", "d2"], f"{_ROW_CONDITIONS}: SurrealDB 3.x applies the function during the index search"
        else:
            assert set(raw) < {"d1", "d2"}, f"{_ROW_CONDITIONS}: 2.x and the embedded engine answer too few records or none"
        assert _ids(docs) == ["d1", "d2"], f"{_ROW_CONDITIONS}: the ORM's answer, the same on every line"

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_condition_on_an_indexed_field(self, kind: str) -> None:
        """README row "``similar_to()`` with other conditions (``filter()``, ``Q``, ``nearby()``, ``search()``)", "an
        indexed field's included": next to a range on the indexed ``n``, an HNSW search answers the two nearest on
        SurrealDB 3.x and no record on 2.x and the embedded engine, whose ``count()`` then ignores ``K`` (4); next to a
        comparison on the indexed ``category``, the exact scan answers the two nearest on 3.x and no record on 2.x and
        the embedded engine. The same records selected first in a ``LET`` are right on every engine."""
        async with _vec_models(kind) as client:
            await _seed_ranked(client)
            major = await _major(client)
            ranged = await _keys(client, _PIN_RANGE, a=2, q=_Q)
            counted = await client.query(_PIN_RANGE_COUNT, {"a": 2, "q": _Q})
            exact = await _keys(client, _PIN_EQUAL_EXACT, c="tech", q=_Q)
            ranged_first = await _keys(client, _PIN_RANGE_LET, a=2, q=_Q)
            counted_first = await client.query(_PIN_RANGE_COUNT_LET, {"a": 2, "q": _Q})
            exact_first = await _keys(client, _PIN_EQUAL_EXACT_LET, c="tech", q=_Q)
        if major >= 3:
            assert (ranged, counted, exact) == (["d4", "d6"], [{"count": 2}], ["d3", "d4"]), (
                f"{_ROW_CONDITIONS}: SurrealDB 3.x applies a condition on an indexed field during the search"
            )
        else:
            assert ranged == [], f"{_ROW_CONDITIONS}: 2.x answers no record next to a range on an indexed field"
            assert counted == [{"count": 4}], f"{_ROW_CONDITIONS}: 2.x counts more records than K there"
            assert exact == [], f"{_ROW_CONDITIONS}: 2.x's exact scan answers no record next to an indexed field"
        assert (ranged_first, counted_first, exact_first) == (["d4", "d6"], [{"count": 2}], ["d3", "d4"]), (
            f"{_ROW_CONDITIONS}: the records selected first in a LET, right on every engine"
        )

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_condition_through_a_record_link(self, kind: str) -> None:
        """README row "``similar_to(dist=…)`` with a condition on a linked record's field": next to ``owner.team``,
        the exact scan answers the two nearest records of the team on SurrealDB 3.x, and every record of the team,
        ``K`` ignored, on 2.x and the embedded engine."""
        async with _vec_models(kind) as client:
            await _seed_owned(client)
            major = await _major(client)
            keys = await _keys(client, _PIN_LINKED, t="x", q=_Q)
        if major >= 3:
            assert keys == ["d1", "d2"], f"{_ROW_LINKED}: SurrealDB 3.x keeps the condition next to the KNN operator"
        else:
            assert keys == ["d1", "d2", "d3", "d5"], f"{_ROW_LINKED}: 2.x's exact scan returns every matching record"

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_writes_whose_where_holds_the_knn(self, kind: str) -> None:
        """README row "``bulk_update()``, ``bulk_delete()``, ``patch()`` with ``similar_to()``": an ``UPDATE`` whose
        ``WHERE`` holds the exact scan writes the two nearest on SurrealDB 3.x and every record on 2.x and the embedded
        engine, while it returns two records everywhere; next to a range on the indexed ``n``, an ``UPDATE`` writes no
        record on any engine, while on 3.x the same ``SELECT`` answers the two nearest. The 3.x half is strict: it
        fails the day SurrealDB 3.x writes those records, when that row is to be rewritten."""
        async with _vec(kind) as client:
            await _apply_ddl(client, _PIN_WRITE_DDL)
            await _apply_ddl(client, _seed_statements("vec_pin_w", numbered=True))
            await _apply_ddl(client, _seed_statements("vec_pin_r", numbered=True))
            major = await _major(client)
            returned = await client.query(_PIN_W_UPDATE, {"q": _Q})
            written = await _keys(client, _PIN_W_HITS)
            ranged = await client.query(_PIN_R_UPDATE, {"a": 2, "q": _Q})
            ranged_written = await _keys(client, _PIN_R_HITS)
            selected = await _keys(client, _PIN_R_SELECT, a=2, q=_Q)
        assert sorted(str(row["id"].id) for row in returned) == ["d1", "d2"], f"{_ROW_WRITES}: the K nearest returned"
        if major >= 3:
            assert written == ["d1", "d2"], f"{_ROW_WRITES}: SurrealDB 3.x writes the K nearest"
            assert selected == ["d4", "d6"], f"{_ROW_WRITES}: SurrealDB 3.x selects the K nearest of the range"
            assert (ranged, ranged_written) == ([], []), (
                f"{_ROW_WRITES}: SurrealDB 3.x now writes the records a range on an indexed field selects next to the "
                "KNN operator: rewrite that row's 3.x cell"
            )
        else:
            assert written == _SIX, f"{_ROW_WRITES}: 2.x's exact scan in a write acts on every matching record"
            assert (ranged, ranged_written, selected) == ([], [], []), f"{_ROW_WRITES}: 2.x writes no record there"

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_full_text_match_next_to_the_knn(self, kind: str) -> None:
        """README row "``similar_to()`` and ``search()`` in one queryset": next to ``title @0@ 'cats'``, the HNSW
        search answers the two nearest matches on SurrealDB 3.3 — 3.1 every match, with no distance — and no record on
        2.x and the embedded engine; the exact scan, the two nearest on 3.x and no record below 3. The matches
        selected first in a ``LET`` are right on every engine."""
        async with _vec_models(kind) as client:
            await _seed(client)
            line = await _line(client)
            indexed = await client.query(_PIN_MATCH, {"t": "cats", "q": _Q2})
            exact = await _keys(client, _PIN_MATCH_EXACT, t="cats", q=_Q2)
            matched_first = await _keys(client, _PIN_MATCH_LET, t="cats", q=_Q2)
        found = {str(row["id"].id): row["d"] for row in indexed}
        if line == (3, 1):
            assert found == {"d1": None, "d2": None, "d6": None}, f"{_ROW_SEARCH}: 3.1 answers every match, no distance"
        elif line[0] >= 3:
            assert found == {key: pytest.approx(_cosine(key, _Q2), rel=1e-5) for key in ("d2", "d6")}, (
                f"{_ROW_SEARCH}: SurrealDB 3.x keeps the match next to the KNN operator"
            )
        else:
            assert found == {}, f"{_ROW_SEARCH}: 2.x and the embedded engine answer no record next to a full-text match"
        if line[0] >= 3:
            assert exact == ["d2", "d6"], f"{_ROW_SEARCH}: SurrealDB 3.x keeps the match next to the exact scan"
        else:
            assert exact == [], f"{_ROW_SEARCH}: 2.x and the embedded engine answer no record next to a full-text match"
        assert matched_first == ["d2", "d6"], f"{_ROW_SEARCH}: the matches selected first in a LET, right everywhere"

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_search_score_in_the_let_form(self, kind: str) -> None:
        """README row "``similar_to()`` and ``search()`` in one queryset", "search annotations are refused": with the
        matches of ``'programming'`` selected first in a ``LET``, the vector search holds no match to score — 2.x and
        the embedded engine answer ``None`` for each of the two records, silently, and SurrealDB 3.x refuses the
        statement ("no MATCHES clause found in WHERE condition"). Below 3 the ORM therefore refuses search annotations
        next to ``similar_to()``."""
        async with _vec_models(kind) as client:
            await _seed(client)
            major = await _major(client)
            refused: pytest.ExceptionInfo[ServerError] | None = None
            scores: list[Any] | None = None
            if major >= 3:
                with pytest.raises(ServerError) as refused:
                    await client.query(_PIN_SCORE_LET, {"t": "programming", "q": _Q2})
            else:
                scores = await client.query(_PIN_SCORE_LET, {"t": "programming", "q": _Q2})
        if major >= 3:
            assert refused is not None
            assert "no MATCHES clause found in WHERE condition" in str(refused.value), (
                f"{_ROW_SEARCH}: SurrealDB 3.x refuses a search score with no match in the statement"
            )
        else:
            assert scores == [None, None], f"{_ROW_SEARCH}: 2.x and the embedded engine score no record, silently"

    @pytest.mark.usefixtures("_live_registry")
    async def test_a_live_query_whose_condition_holds_the_knn(self) -> None:
        """README section 37, *Not supported* (``live()`` and ``watch()`` with ``similar_to()``, the reason the ORM's
        refusal gives): SurrealDB accepts a live query whose condition holds the KNN operator and never notifies it —
        not even the create of a record nearest the query vector, which a live query on the whole table reports. Over
        WebSocket, on every server line (the embedded engine has no live queries)."""
        async with _vec_models("ws") as client:
            await _seed(client)
            nearest_id = await client.query(_LIVE_KNN, {})
            every_id = await client.query(_LIVE_ALL, {})
            try:
                nearest = SurrealDBConnectionManager.subscribe_live(nearest_id)
                every = SurrealDBConnectionManager.subscribe_live(every_id)
                await VecDoc(id="d7", title="Cats and mice", category="pets", embedding=[1.0, 0.0, 0.0]).save()
                [created] = await _take(every, 1)
                await _assert_silent(nearest)
            finally:
                await SurrealDBConnectionManager.kill(nearest_id)
                await SurrealDBConnectionManager.kill(every_id)
        assert created["action"] == LiveAction.CREATE
        assert created["result"]["title"] == "Cats and mice"


# === README: section 37, the behaviour tables, the features, roadmap and comparison rows; docs/ROADMAP.md ===========

_SECTION_36 = "### 36. Full-text search (`search()`, `SearchScore`, `SearchHighlight`, `Analyzer`, `Index`)"
_SECTION_37 = "### 37. Vector search (`similar_to()`, `VectorField`, `Index(hnsw=True)`)"
#: Section 37's subsections, in order, after an untitled intro.
_SUBSECTIONS_37 = [
    "Declare and apply",
    "Type checkers",
    "Find the nearest",
    "With other conditions",
    "What SurrealDB decides",
    "Not supported",
    "Coming from the full SurrealDB-ORM",
]
#: The parts of section 37 whose ``python`` blocks run on ``mem://`` (the others have none).
_PARTS_WITH_EXAMPLES_37 = [
    "Declare and apply",
    "Type checkers",
    "Find the nearest",
    "With other conditions",
    "What SurrealDB decides",
]
_S37_TYPE = (
    "The ORM writes every HNSW option — `DIST`, `TYPE`, `EFC` and `M`, SurrealDB's defaults when not given — so "
    "every engine stores the same index: the embedded engine would otherwise store `F64` vectors where servers "
    "store `F32`."
)
_S37_TYPED = (
    'Type checkers do not understand `VectorField[1536]` (mypy: `"VectorField" expects no type arguments`), as '
    "in the full ORM: in type-checked code, write `embedding: Annotated[list[float] | None, Vector(1536)] = None`, "
    "which the ORM reads the same way."
)
_S37_LIMIT = (
    "`similar_to(field, vector, limit)` keeps the `limit` nearest records, `limit` and `ef` going up to 10 000; "
    "`limit()` and `offset()` then page through them, and without `order_by()` they come nearest first."
)
_S37_EF = (
    "`ef` (100 by default) sizes the HNSW search: a larger value finds the true nearest records more often, at a "
    "cost. The ORM writes it raised to `limit`, so without other conditions every line returns `limit` records "
    "when the table holds them."
)
_S37_NO_INDEX = (
    "`similar_to()` reads the field's HNSW index: on a field without one it finds no record. Pass `dist=` for an "
    "exact scan that needs no index."
)
_S37_FILTERS = (
    "Other conditions select the records first, then `similar_to()` keeps the nearest among them: SurrealDB 3.x "
    "applies them during the index search. SurrealDB 2.x and the embedded engine answer too few records, or none, "
    "next to many conditions (one on an indexed field among them), so there the ORM selects the records matching "
    "them in a `LET` first, which is slower: upgrade to SurrealDB 3.x for large tables."
)
_S37_SCORES = (
    "On SurrealDB 3.x, search annotations (`SearchScore`, `SearchHighlight`, `SearchOffsets`) give each of the "
    "nearest matches the score, highlight and offsets that the same `search()` gives it without `similar_to()`. "
    "On SurrealDB 2.x and the embedded engine the ORM refuses them before the statement is sent, since there it "
    "selects the matches first and the vector search holds no match to score, highlight or locate; `count()`, "
    "`exists()` and the aggregations, which return no annotation, still answer. The examples run on the embedded "
    "engine, which is why this one prints the refusal."
)
_S37_APPROX = (
    "HNSW is approximate: with a condition that keeps few records, a search can return fewer than `limit` "
    "records, depending on how the index was built. SurrealDB 2.x and the embedded engine do so more often, "
    "even with a condition that keeps a tenth of the table. Pass `dist=` for an exact scan."
)
_S37_PRECISION = (
    "Distances are computed in the index's `vector_type` (`F32` by default: single precision), so they can differ "
    "from Python's in the last digits; `dist=` computes in double precision."
)
#: What *Type checkers* says of the union spellings of a vector field (issue #318).
_S37_UNION = (
    "`VectorField[d]` is already optional, so it needs no `| None`. Written `VectorField[d] | None` or "
    "`Optional[VectorField[d]]`, its `None` default sits inside a union member, where Pydantic ignores it with an "
    "`UnsupportedFieldAttributeWarning`: write `= None`, or the field is required. The ORM reads these spellings, and "
    "`Annotated[list[float], Vector(d)] | None`, like the plain ones: the same column, the same checks of the vector "
    "index when the class is defined, the same dimension for `similar_to()`, the same answer from `get_vector_info()`. "
    "A union that holds a vector next to a type other than `None`, or two different `Vector` markers, has no single "
    "dimension: the class statement refuses it, naming the field."
)
#: The nine sentences section 37 says word for word (whitespace normalised), with their subsection.
_SENTENCES_37 = [
    ("Declare and apply", _S37_TYPE),
    ("Type checkers", _S37_TYPED),
    ("Find the nearest", _S37_LIMIT),
    ("Find the nearest", _S37_EF),
    ("Find the nearest", _S37_NO_INDEX),
    ("With other conditions", _S37_FILTERS),
    ("With other conditions", _S37_SCORES),
    ("What SurrealDB decides", _S37_APPROX),
    ("What SurrealDB decides", _S37_PRECISION),
]
#: What each part of section 37 says (key phrases, whitespace normalised); the intro under ``""``.
_PART_NEEDLES_37 = {
    "": (
        "Since v0.36.0",
        "the full SurrealDB-ORM's",
        "`similar_to()`",
        "`_knn_distance`",
        "`Index(hnsw=True)`",
        "binds the query vector",
        "SurrealDB 2.x, 3.x and the embedded engine",
        "(#orm-behaviour-surrealdb-2x-vs-3x)",
        "`mem://`",
    ),
    "Declare and apply": (
        "`VectorField[d]`",
        '`VectorField[d, "F64"]`',
        "`option<array<float, d>>`",
        "`CreateIndex`'s arguments",
        "`dimension=`",
        "`dist=`",
        "`vector_type=`",
        "`efc=`",
        "`m=`",
        "`SurrealConfigDict(indexes=[...])`",
        "two vector indexes on one column are refused",
        "`define_table()`",
        "`define_indexes()`",
        "`index_ddl()`",
        "on every write and on every read",
        "`ValidationError`",
        "return its row as a dict",
        "`list[float] | None`",
        "`remove_index()`",
        "`VectorField[768]`",
    ),
    "Type checkers": (
        '`Vector(dimension, vector_type="F32")`',
        'Field(default=None, alias="…")',
        "`get_vector_info()`",
        "`is_vector_field()`",
        "`surreal_orm_lite.vector`",
    ),
    "Find the nearest": (
        "`similar_to(field, vector, limit=10, *, ef=None, dist=None)`",
        "an aliased field searches its column",
        "a dotted path",
        "`.tolist()`",
        "A second `similar_to()` replaces the first",
        "`first()`",
        "never written back",
        "dicts holding it",
        '`order_by("-_knn_distance")`',
    ),
    "With other conditions": (
        "`filter()`",
        "`Q`",
        "`nearby()`",
        "`search()`",
        "`count()`",
        "`exists()`",
        "`values()`",
        "`bulk_update()`",
        "`bulk_delete()`",
        "`patch()`",
        "`Subquery`",
        "`get(<id>)` and `all()` ignore it",
        "whose ids they select first",
        "ranked by the vector",
    ),
    "What SurrealDB decides": ("zero vector", "`nan`", "naming the field and both lengths"),
    "Not supported": (
        "on SurrealDB 2.x and the embedded engine",
        "`search()` as a condition stays available there",
        "`as_of()`",
        "`live()`/`watch()`",
        "`changes()`",
        "`MTREE`",
        "`MINKOWSKI`",
        "`JACCARD`",
        "`PEARSON`",
        "`F16`",
        "`DISKANN`",
        "`concurrently`",
    ),
}
#: The thirteen "Coming from the full SurrealDB-ORM" bullets, word for word (whitespace normalised), in order.
_COMING_FROM_37 = [
    "**`similar_to()` keeps the full ORM's signature and `_knn_distance`**: `similar_to(field, vector, limit=10, *, "
    "ef=None)`, the distance set on each instance as `_knn_distance`, nearest first unless `order_by()` is given.",
    "**`dist=` is an addition** of this library: an exact, brute-force search that needs no index (SurrealDB's "
    "`<|K,DISTANCE|>`).",
    "**`similar_to()` applies wherever `filter()` applies**: `count()`, the aggregations, `values()` groups, "
    "`exists()`, `bulk_update()`, `bulk_delete()`, `patch()` and a `Subquery`; the full ORM applies it to "
    "`exec()` only. The bulk writes act on the ids of the `limit` nearest records, selected first.",
    "**Refused where SurrealDB cannot honour it**: `as_of()`, `live()`/`watch()` or `changes()` with "
    "`similar_to()`, before any request, and search annotations with `similar_to()` on SurrealDB 2.x and the "
    "embedded engine, before the statement is sent; the full ORM sends them.",
    "**Checked before any request**: the vector's numbers, its length against the field's `VectorField` or "
    "index, `limit` and `ef` (1 to 10 000; the full ORM sends any value), and the `Index` vector arguments.",
    "**`ef` is raised to `limit`** on every line; the full ORM writes it as given, and SurrealDB 2.x then returns "
    "at most `ef` records.",
    "**Other conditions are selected first on SurrealDB 2.x** (a `LET`), since 2.x answers too few or no records "
    "next to many of them, one on an indexed field among them; the full ORM sends one statement.",
    "**Every HNSW option is written**, and `define_indexes()` compares before it applies: an unchanged vector index "
    "is never rebuilt.",
    "**`VectorField` checks its values**: their length and finite numbers, on reads too; the full ORM's accepts "
    "any list of floats. `Vector(d)` is its typed form, which type checkers accept.",
    "**No `MTREE`, `MINKOWSKI`, `JACCARD` or `PEARSON`, and no `concurrently` yet**; the full ORM writes "
    "`MINKOWSKI` without its order, which SurrealDB refuses.",
    "**Vector functions in `annotate()`** (`vector::similarity::cosine()`…) are not available yet; "
    "`hybrid_search()` comes in v0.37.0.",
    "**`get_vector_info()` and `is_vector_field()` are imported from `surreal_orm_lite.vector`**: the full ORM has "
    "them in `surreal_orm.fields`, a module this library does not have.",
    "**`generate_models_from_db()` does not write `VectorField` yet**, while the full ORM's generator writes it for "
    "a field with an HNSW index.",
]
#: One key phrase per "Coming from" bullet, in order (the accepted differences).
_COMING_FROM_PHRASES_37 = (
    "keeps the full ORM's signature",
    "`dist=` is an addition",
    "`similar_to()` applies wherever",
    "search annotations with `similar_to()`",
    "its length against the field's",
    "`ef` is raised to `limit`",
    "selected first on SurrealDB 2.x",
    "Every HNSW option is written",
    "`VectorField` checks its values",
    "No `MTREE`",
    "Vector functions in `annotate()`",
    "imported from `surreal_orm_lite.vector`",
    "does not write `VectorField` yet",
)
#: The search annotations' block (*With other conditions*): run on the embedded engine, it prints the ORM's refusal.
_ANNOTATED_EXCEPT = "except SurrealDbError as exc:  # the embedded engine is a 2.x core, like a SurrealDB 2.x server"
#: The term that block searches: found in fewer than half the section's documents, so its BM25 score is not 0.
_ANNOTATED_TERM = "programming"
_HEADER_SENTENCE_36 = (
    "Rows added in v0.36.0 were measured on 2.7.0, 3.3.4 and the embedded engine, with 3.1.5 where it differs."
)
#: Exactly the eight rows v0.36.0 adds to the 2.x/3.x table, cell by cell, within the table's column widths.
_VECTOR_ROWS = [
    [
        "Vector index (`Index(hnsw=True)`, `define_indexes()`)",
        "`HNSW` as on 3.x (the ORM writes `DIST`, `TYPE`, `EFC` and `M` and one field); 2.x also accepts `MTREE`, "
        "which `similar_to()` does not use",
        "`HNSW`, one field per index; `MTREE` is a parse error",
        "v0.36.0",
    ],
    [
        "`similar_to()` with `ef` below `limit`",
        "the ORM writes `ef` raised to `limit`: 2.x would return at most `ef` records",
        "the same statement: 3.x returns `limit` records either way",
        "v0.36.0",
    ],
    [
        "`similar_to()` with other conditions (`filter()`, `Q`, `nearby()`, `search()`)",
        "the ORM selects the records matching them first (a `LET`): 2.x answers too few or no records next to many "
        "of them, an indexed field's included",
        "every condition stays next to the KNN operator, applied during the index search (faster)",
        "v0.36.0",
    ],
    [
        "`similar_to()` and `search()` in one queryset",
        "the ORM selects the full-text matches first (a `LET`): 2.x answers no record next to a full-text match; "
        "search annotations are refused",
        "the match stays next to the KNN operator, with its scores, highlights and offsets; 3.1 answers every match, "
        "with no distance",
        "v0.36.0",
    ],
    [
        "`bulk_update()`, `bulk_delete()`, `patch()` with `similar_to()`",
        "the ORM writes the ids of the `limit` nearest: 2.x's exact scan in a write would act on every matching record",
        "the same: 3.x writes nothing when a range on an indexed field shares the KNN operator",
        "v0.36.0",
    ],
    [
        "`similar_to()` with a condition that keeps few records",
        "HNSW is approximate: fewer than `limit` records, depending on how the index was built, even with a tenth of "
        "the table kept; `dist=` is exact",
        "rarer; also seen with a `Subquery` id list keeping a few percent of the table",
        "v0.36.0",
    ],
    [
        "`similar_to(dist=…)` with a condition on a linked record's field",
        "the ORM selects the matching records first (a `LET`): 2.x's exact scan would return every matching record, "
        "ignoring `limit`",
        "the condition stays next to the KNN operator",
        "v0.36.0",
    ],
    [
        "`similar_to()` on a field without a vector index",
        "no record",
        "no record; 3.1 answers every record, with no distance",
        "v0.36.0",
    ],
]
#: The row v0.36.0 adds to the embedded engine's table, cell by cell.
_EMBEDDED_VECTOR_ROW = [
    "Vector search (v0.36.0)",
    "as on a 2.x server: `MTREE` accepted, at most `ef` records returned (the ORM raises `ef` to `limit`), other "
    "conditions selected first in a `LET` (search annotations refused), writes made by the ids of the nearest, "
    "fewer records with a selective condition; an index without `TYPE` is `F64` (servers: `F32`), so the ORM "
    "always writes `TYPE`",
    "3.x refuses `MTREE`, applies the conditions during the index search and keeps search annotations",
    "v0.36.0",
]
_FEATURES_ROW_37 = ["Vector search (`similar_to()`, KNN, HNSW indexes)", "✅"]
_ROADMAP_ROWS_37 = [
    ["v0.35.0", "Tier 3 — Full-text search", "✅ Released"],
    ["v0.36.0", "Tier 3 — Vector search", "✅ Released"],
    ["v0.37.0 – v0.40.0", "Tier 3 — Advanced (hybrid search, migrations, CLI)", "📋 Planned"],
    ["v0.41.0", "Beta Phase (API freeze, hardening)", "📋 Planned"],
]
_COMPARISON_ROW_37 = ["Vector Search (KNN/HNSW)", "✅ v0.36.0 (`similar_to()`)", "✅"]
#: The rows the pins of this module name in their messages (each must be one of ``_VECTOR_ROWS``).
_PINNED_ROWS = [_VECTOR_INDEX_ROW, _ROW_EF, _ROW_CONDITIONS, _ROW_SEARCH, _ROW_WRITES, _ROW_LINKED, _ROW_NO_INDEX]


def _section_37() -> str:
    return _readme_part(_SECTION_37)


def _part_37(heading: str) -> str:
    """One ``####`` subsection of section 37, as written (the intro for ``""``)."""
    if not heading:
        return _section_37().split("\n#### ", 1)[0]
    return _subsection(f"#### {heading}", _section_37())


def _readme_blocks(text: str) -> list[str]:
    """The ``python`` blocks of *text*, in order."""
    blocks: list[str] = re.findall(r"^```python\n(.*?)^```$", text, re.MULTILINE | re.DOTALL)
    return blocks


def _readme_printed(block: str) -> list[str]:
    """What a README example prints: its comment lines that start a line (``# <printed line>``), in order."""
    return re.findall(r"^# (.*)$", block, re.MULTILINE)


def _readme_bullets(text: str) -> list[str]:
    """The ``- `` bullets of *text*, each with its continuation lines, whitespace normalised."""
    return [_normalised(bullet) for bullet in re.findall(r"^- .*?(?=^- |^\s*$|\Z)", text, re.MULTILINE | re.DOTALL)]


def _anchor(heading: str) -> str:
    """GitHub's anchor of a README heading: lower case, punctuation but ``-`` and ``_`` dropped, spaces as ``-``."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def _similar_to_bounds(block: str) -> list[tuple[str, object]]:
    """The ``limit`` and ``ef`` of every ``similar_to()`` call of a README block, as written: an ``int`` literal, or
    the source of any other expression."""
    tree = compile(block, "<README section 37>", "exec", flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    found: list[tuple[str, object]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "similar_to"):
            continue
        given = {"limit": node.args[2]} if len(node.args) > 2 else {}
        given.update({keyword.arg: keyword.value for keyword in node.keywords if keyword.arg in ("limit", "ef")})
        for name, value in given.items():
            found.append((name, value.value if isinstance(value, ast.Constant) else ast.unparse(value)))
    return found


class TestReadmeSection37:
    """README section 37 and what v0.36.0 changes elsewhere in the README: section 37's place, subsections, examples
    (run on ``mem://`` as written, each printed line equal to its comment), sentences and "Coming from" bullets; the
    features, roadmap, comparison and both behaviour tables."""

    def test_section_37_follows_section_36(self) -> None:
        """Right after section 36 (after its "Coming from" bullets) and before the ``---`` of "Configuration Options"."""
        text = _readme()
        start_36 = text.index(f"\n{_SECTION_36}\n")
        start_37 = text.index(f"\n{_SECTION_37}\n")
        configuration = text.index("\n## Configuration Options\n")
        assert start_36 < start_37 < configuration
        assert re.findall(r"^#{2,3} .*$", text[start_36 + 1 : configuration], re.MULTILINE) == [_SECTION_36, _SECTION_37]
        assert text[:configuration].rstrip().endswith("\n---"), "section 37 ends before the --- of the next part"

    def test_subsections_in_order(self) -> None:
        assert re.findall(r"^#### (.*)$", _section_37(), re.MULTILINE) == _SUBSECTIONS_37

    def test_where_the_examples_stand(self) -> None:
        """Each part that shows code opens with prose and holds ``python`` blocks; the intro and the other parts hold
        none; the section has ``python`` fences only."""
        for heading in ["", *_SUBSECTIONS_37]:
            part = _part_37(heading)
            blocks = _readme_blocks(part)
            assert bool(blocks) is (heading in _PARTS_WITH_EXAMPLES_37), heading
            if blocks:
                assert not part.split("\n", 2)[2].lstrip().startswith("```"), heading
        fences = re.findall(r"^```(\S*)$", _section_37(), re.MULTILINE)
        assert fences and fences == ["python", ""] * (len(fences) // 2), fences

    @pytest.mark.parametrize("heading", list(_PART_NEEDLES_37))
    def test_what_each_part_says(self, heading: str) -> None:
        text = _normalised(_part_37(heading))
        missing = [needle for needle in _PART_NEEDLES_37[heading] if needle not in text]
        assert not missing, missing

    @pytest.mark.parametrize(("heading", "sentence"), _SENTENCES_37, ids=[f"s{index}" for index in range(1, 10)])
    def test_sentences_word_for_word(self, heading: str, sentence: str) -> None:
        """Each of the nine sentences exactly once in its subsection, and nowhere else in the section."""
        assert len(_SENTENCES_37) == 9
        assert _normalised(_part_37(heading)).count(sentence) == 1, sentence
        assert _normalised(_section_37()).count(sentence) == 1, sentence

    def test_the_union_spellings(self) -> None:
        """*Type checkers* says once, word for word, that ``VectorField[d]`` is already optional, what ``| None`` asks
        for, and that the union spellings are read like the plain ones (issue #318)."""
        assert _normalised(_part_37("Type checkers")).count(_S37_UNION) == 1
        assert _normalised(_section_37()).count(_S37_UNION) == 1

    def test_coming_from_the_full_orm(self) -> None:
        """Thirteen bullets, each word for word, a bold lead and one key phrase each, in order."""
        bullets = _readme_bullets(_part_37("Coming from the full SurrealDB-ORM"))
        assert len(bullets) == len(_COMING_FROM_37) == len(_COMING_FROM_PHRASES_37) == 13, bullets
        for bullet, text, phrase in zip(bullets, _COMING_FROM_37, _COMING_FROM_PHRASES_37, strict=True):
            assert bullet == f"- {text}", (bullet, text)
            assert bullet.startswith("- **") and phrase in bullet, (phrase, bullet)

    def test_the_search_annotations_block(self) -> None:
        """*With other conditions*: one block annotates a ``SearchScore`` and a ``SearchHighlight`` next to
        ``similar_to()`` inside ``try``, catching ``SurrealDbError`` on the embedded engine; what it prints is the
        ORM's refusal for ``SearchScore``. The section's full-text index declares ``highlights=True``."""
        [block] = [code for code in _readme_blocks(_part_37("With other conditions")) if "SearchScore(" in code]
        assert "similar_to(" in block and 'SearchHighlight("<b>", "</b>", 0)' in block, block
        assert f'search(title="{_ANNOTATED_TERM}")' in block, block
        assert re.search(rf"^{re.escape(_ANNOTATED_EXCEPT)}$", block, re.MULTILINE), block
        assert _readme_printed(block)[0] == _VS1.format("SearchScore"), _readme_printed(block)
        assert "highlights=True" in "".join(_readme_blocks(_part_37("Declare and apply")))

    def test_limit_and_ef_within_the_range(self) -> None:
        """Every ``similar_to()`` of the examples gives ``limit`` and ``ef`` as ``int`` literals from 1 to 10000."""
        found = [bound for block in _readme_blocks(_section_37()) for bound in _similar_to_bounds(block)]
        assert {name for name, _ in found} == {"limit", "ef"}, found
        assert all(isinstance(value, int) and 1 <= value <= 10_000 for _, value in found), found

    def test_no_local_reference(self) -> None:
        """No design reference and no issue number in the section."""
        section = _section_37()
        for marker in ("§", r"\bspec\b", r"\.claude"):
            assert re.search(marker, section) is None, marker
        assert re.findall(r"(?<![(\w])#\d+", section) == []

    def test_links_resolve(self) -> None:
        """Every in-page link of section 37 names a heading."""
        anchors = {_anchor(heading) for heading in re.findall(r"^#{2,4} (.*)$", _readme(), re.MULTILINE)}
        links = re.findall(r"\]\(#([^)]+)\)", _section_37())
        assert len(links) >= 5, links
        assert [link for link in links if link not in anchors] == []

    def test_server_behaviour_rows(self) -> None:
        """Exactly eight rows, Since v0.36.0, together and in version order before the table's last row; the table
        keeps its widths."""
        assert_server_rows(_VECTOR_ROWS)

    def test_the_pins_name_these_rows(self) -> None:
        """Each README row a pin of this module names is one of the eight rows."""
        firsts = {f"README row '{row[0]}'" for row in _VECTOR_ROWS}
        assert [row for row in _PINNED_ROWS if row not in firsts] == []

    def test_server_behaviour_header_sentence(self) -> None:
        assert_server_header_sentence(_HEADER_SENTENCE_36)

    def test_embedded_table_row(self) -> None:
        """All four cells, in version order before the catch-all "Everything else" row; widths kept."""
        assert_embedded_rows([_EMBEDDED_VECTOR_ROW])

    def test_features_row(self) -> None:
        features = [row for _, row in _table_rows(_readme_part("## Features"))]
        firsts = [row[0] for row in features]
        assert _FEATURES_ROW_37 in features, features
        full_text = "Full-text search (`search()`, BM25 score and highlights)"
        assert firsts.index(_FEATURES_ROW_37[0]) == firsts.index(full_text) + 1

    def test_roadmap_rows(self) -> None:
        roadmap = [row for _, row in _table_rows(_readme_part("## Roadmap"))]
        start = [row[0] for row in roadmap].index("v0.35.0")
        assert roadmap[start : start + 4] == _ROADMAP_ROWS_37, roadmap
        assert "v0.36.0 – v0.40.0" not in [row[0] for row in roadmap], roadmap

    def test_comparison_row(self) -> None:
        """Released, right after full-text search and before hybrid search."""
        comparison = [row for _, row in _table_rows(_readme_part("## SurrealDB-ORM-lite vs SurrealDB-ORM"))]
        firsts = [row[0] for row in comparison]
        start = firsts.index("Full-Text Search")
        assert comparison[start + 1] == _COMPARISON_ROW_37, comparison
        assert firsts[start + 2] == "Hybrid Search (RRF)", firsts

    def test_tables_have_no_raw_pipe_in_a_cell(self) -> None:
        """Every table v0.36.0 touches keeps one cell count per line and no escaped pipe; section 37 has no table."""
        parts = [
            _readme_part("## Features"),
            _readme_part("### ORM behaviour: SurrealDB 2.x vs 3.x"),
            _readme_part("### ORM behaviour: the embedded engine"),
            _readme_part("## Roadmap"),
            _readme_part("## SurrealDB-ORM-lite vs SurrealDB-ORM"),
        ]
        found = [lines for part in parts for lines in _md_tables(part)]
        assert len(found) == len(parts)
        for lines in found:
            assert len({line.count("|") for line in lines}) == 1 and "\\|" not in "".join(lines), lines
        assert _md_tables(_section_37()) == []

    async def test_section_37_examples(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Every ``python`` block runs as written, in order and in one namespace, on ``mem://`` (inside
        ``embedded_database()``); what each block prints is, line by line, its ``# `` comment lines. The term the
        search annotations' block searches is in fewer than half the section's documents."""
        capsys.readouterr()
        namespace: dict[str, Any] = {"__name__": "readme_section_37"}
        try:
            async with embedded_database():
                for code in _readme_blocks(_section_37()):
                    compiled = compile(code, "<README section 37>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                    # A doctest of this repository's own README (trusted text), top-level awaits included.
                    outcome = eval(compiled, namespace)
                    if inspect.iscoroutine(outcome):
                        await outcome
                    assert capsys.readouterr().out.splitlines() == _readme_printed(code), code
            titles = [str(row[1]).lower() for row in namespace["corpus"]]
            assert 0 < 2 * sum(_ANNOTATED_TERM in title for title in titles) < len(titles), titles
        finally:
            namespace.clear()


# --- docs/ROADMAP.md --------------------------------------------------------------------------------------------------

_ROADMAP = Path(__file__).resolve().parents[1] / "docs" / "ROADMAP.md"
_PHASE_G = "### 🔎 Phase G — Advanced search"
_SDK_TABLE = "## 🆕 Everything unlocked by the official SDK 2.0"
_COMPARISON_TABLE = "## SurrealDB-ORM vs SurrealDB-ORM-lite Comparison"
#: The rows v0.36.0 holds while planned, by table.
_PLANNED_OVERVIEW_36 = [["v0.36.0 – v0.40.0", "Tier 3 — Advanced (vector/hybrid search, migrations, CLI), 5 minors", "Planned"]]
_PLANNED_UNLOCKED_36 = ["Vector Search (KNN, HNSW/MTREE)", "`DEFINE INDEX … HNSW/MTREE` + KNN operator", "v0.36.0"]
_PLANNED_COMPARISON_36 = ["Vector Search (KNN/HNSW)", "yes", "v0.36.0"]
_PLANNED_PHASE_G_36 = ["v0.36.0", "Vector Search (`similar_to()`, KNN)", "`DEFINE INDEX … HNSW/MTREE` + KNN operator"]
#: The rows v0.36.0 holds once released, by table, and its "Completed versions" heading (a ``|`` in a cell is
#: written ``\|`` in the Markdown, read back as ``|``).
_OVERVIEW_36 = [
    ["v0.36.0", "Tier 3 — Vector search", "Done"],
    ["v0.37.0 – v0.40.0", "Tier 3 — Advanced (hybrid search, migrations, CLI), 4 minors", "Planned"],
]
_UNLOCKED_36 = ["Vector Search (KNN, HNSW)", "`DEFINE INDEX … HNSW` + KNN operator `<|K,EF|>`", "✅ v0.36.0"]
_COMPARISON_36 = ["Vector Search (KNN/HNSW)", "yes", "✅ v0.36.0"]
_PHASE_G_36 = ["✅ v0.36.0", "Vector Search (`similar_to()`, KNN)", "`DEFINE INDEX … HNSW` + KNN operator `<|K,EF|>`"]
_COMPLETED_HEADING_35 = "### Version 0.35.0 — Full-text search (`search()`)"
_COMPLETED_HEADING_36 = "### Version 0.36.0 — Vector search (`similar_to()`)"
#: What the released block names among what it defers: the background build of an index.
_DEFERRED_NEEDLES_36 = ("concurrently=True", "build status check")


def _roadmap() -> str:
    return _ROADMAP.read_text(encoding="utf-8")


def _roadmap_rows(heading: str) -> list[list[str]]:
    """The rows of the first table under *heading*, each cell's escaped ``\\|`` read as ``|``."""
    return [[cell.replace("\\|", "|") for cell in cells] for _, cells in _table_rows(_subsection(heading, _roadmap()))]


def _released_36() -> bool:
    """Whether ``docs/ROADMAP.md`` records v0.36.0 as released: its Phase G row carries the ✅ the release adds."""
    return _roadmap_rows(_PHASE_G)[1][0] == "✅ v0.36.0"


class TestRoadmap36:
    """``docs/ROADMAP.md`` records v0.36.0 as planned, or — once its Phase G row carries ✅ — as released: done in the
    overview, ✅ in the SDK and comparison tables (the SDK table naming HNSW alone) and in Phase G, its block in
    "Completed versions" right after v0.35.0's. The Phase G row decides which state the whole file must be in."""

    def test_overview(self) -> None:
        overview = _roadmap_rows("## Overview")
        start = [cells[0] for cells in overview].index("v0.35.0") + 1
        expected = _OVERVIEW_36 if _released_36() else _PLANNED_OVERVIEW_36
        assert overview[start : start + len(expected)] == expected, overview
        assert overview[start + len(expected)][0] == "v0.41.0", overview

    def test_sdk_table(self) -> None:
        expected = _UNLOCKED_36 if _released_36() else _PLANNED_UNLOCKED_36
        rows = _roadmap_rows(_SDK_TABLE)
        assert [cells for cells in rows if cells[0].startswith("Vector Search")] == [expected], rows

    def test_comparison_table(self) -> None:
        expected = _COMPARISON_36 if _released_36() else _PLANNED_COMPARISON_36
        rows = _roadmap_rows(_COMPARISON_TABLE)
        assert [cells for cells in rows if cells[0].startswith("Vector Search")] == [expected], rows

    def test_phase_g(self) -> None:
        rows = _roadmap_rows(_PHASE_G)
        assert rows[1] == (_PHASE_G_36 if _released_36() else _PLANNED_PHASE_G_36), rows
        assert [cells[0] for cells in rows[2:]] == ["v0.37.0"], rows

    def test_completed_versions(self) -> None:
        """Released: v0.36.0's block right after v0.35.0's, naming the deferred background build. Planned: no such
        block."""
        roadmap = _roadmap()
        completed = roadmap.split("\n## Completed versions\n", 1)[1].split("\n## ", 1)[0]
        headings = re.findall(r"^### .*$", completed, re.MULTILINE)
        if not _released_36():
            assert _COMPLETED_HEADING_36 not in headings, headings
            return
        assert headings[headings.index(_COMPLETED_HEADING_36) - 1] == _COMPLETED_HEADING_35, headings
        block = _normalised(_subsection(_COMPLETED_HEADING_36, completed))
        missing = [needle for needle in _DEFERRED_NEEDLES_36 if needle not in block]
        assert not missing, missing
