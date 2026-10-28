"""Hybrid search — ``QuerySet.hybrid_search()`` (v0.37.0): the fusion, the statements, the refusals and the behaviour
on SurrealDB 2.x, 3.x and the embedded engine.

The refusal texts that name their caller (``TestSubjectKeywords``): the checks and texts ``hybrid_search()`` shares
with ``similar_to()`` (``vector``) and ``search()`` (``search``) take a keyword-only ``subject=`` — and, for a limit,
``arg=`` — that starts each text with ``hybrid_search()`` and names its argument. Without the keyword each text is
v0.35.0's or v0.36.0's, byte for byte. These are pure: no client is involved, no request is sent.

The ``hybrid`` module (``TestHybridChecks``, ``TestFuse``): the checks of the two field names and of ``rrf_k``, the
texts of the refusals only ``hybrid_search()`` raises, the reserved names a ``select()`` item cannot read, and
``fuse()`` — the Reciprocal Rank Fusion the ORM computes on SurrealDB 2.x and the embedded engine, which have no
``search::rrf()``: the two lists each case of the corpus gets on 2.7.0, written as literals, fuse to the records,
order and scores SurrealDB 3.x's ``search::rrf()`` answers; each record keeps only the per-list value of the lists that
found it, whatever columns the records store; the input rows are never changed. Pure too.

The statement (``TestCompileHybrid``): what ``hybrid_search()`` sends, compiled on a private copy of the queryset that
holds both searches — on SurrealDB 3.x one ``SELECT`` fusing the two lists written inline in ``search::rrf()``, each
list leaving out the other list's value (``OMIT``), the ORM ordering and paging the fused records; below 3 the two
lists in ``LET``s, the block's value both lists, for the ORM to fuse. The queryset's conditions, ``select()`` (an item
naming a reserved column left out) and ``fetch()`` apply to both lists; the queryset itself, its link heads included,
is never changed. Pure: the statements are compiled, never sent.

The method (``TestHybridSearchChecks``, ``TestHybridRows``, ``TestHybridServerErrors``): ``hybrid_search()``'s
signature, and every refusal of its arguments and of the queryset it is called on, each with its exact class and text,
in a fixed order, before any client is acquired, the queryset unchanged; the dicts it returns, built from the rows of
either line — the record's columns keyed by their Python names, ``_rrf_score``, ``_knn_distance`` and
``_search_score``, never SurrealDB's ``rrf_score`` nor the ORM's positions; SurrealDB's refusals after the request — no
full-text index, a vector of another length than its index's dimension — naming ``hybrid_search()``, in a transaction
too, read from a client and a transaction that fail every read.

On a server (``TestHybridSearchE2E``, ``TestHybridDispatchE2E``), over WebSocket and HTTP on the line under test and on
the embedded engine: the cases of an eight-record corpus fuse to the same records, order and scores on every line —
equal scores in the vector list's order, then the full-text list's, every time —, each distance and score the one
``similar_to()`` and ``search()`` give the same record; an empty text matches nothing, so the vector list alone is
fused, each ``_search_score`` ``None``; SurrealDB 3.x fuses with ``search::rrf()``, the ORM below 3, the line read once
per client. Every seed of a vector index waits for the index's background compaction before the test reads or writes
(``tests/_compaction.py``, issue #344).

With the rest of the queryset and what the server decides (``TestHybridCompositionE2E``,
``TestHybridReservedColumnsE2E``, ``TestHybridErrorsE2E``, ``TestHybridTransactionsE2E``), on the same engines:
``select()``, ``fetch()``, a ``Subquery`` filter, ``variables()``, named connections, ``using()`` and ``cache()``;
records storing columns named as the method's own values answering as records without them; a field without its
full-text or vector index, a vector of another length than its index's dimension and a table that does not exist, per
line; an interactive transaction's own writes, and a buffered transaction's refusal before anything is sent.

What each line answers to ``search::rrf()`` (``TestHybridLinePinsE2E``), pinned with literal statements on the same
engines: SurrealDB 3.x has the function, 2.x and the embedded engine refuse it; its score is the sum of 1/(k + rank)
over the lists, bit for bit, the records, scores and order the ORM's fusion gives the same lists.

Records at an equal distance at the ``vector_limit``-th place (``TestExactScanTiesE2E``), on the same engines: the
HNSW index and the exact scan (``dist=``, in ``hybrid_search()`` as in ``similar_to()``) give identical calls one answer
on every engine; below SurrealDB 3, whose own exact scan picks among the tied records in no fixed order, the ORM ranks
the scan by distance then id, so the tied records kept are the first by id, in that order.

The documentation (``TestHybridSearchDocstring``, ``TestReadmeSection38``, ``TestRoadmap37``), with no server: the
method's docstring — the cross-line guarantee for the same two lists, as README section 38 words it, and the exact
scan's ties in its ``dist`` argument —; README section 38 — its place, its examples run as written on the embedded
engine, each printed line equal to its comment, its texts word for word —, the bullets of sections 36 and 37 that point
to it, the rows v0.37.0 adds to the features, roadmap, comparison and both behaviour tables, and v0.37.4's row of the
exact scan's ties with its sentence; ``docs/ROADMAP.md`` recording v0.37.0 as planned or, once its Phase G row carries
✅, as released.
"""

import ast
import contextlib
import copy
import inspect
import math
import os
import re
import typing
import warnings
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field
from surrealdb import InternalError, RecordID, ServerError

from surreal_orm_lite import (
    Analyzer,
    BaseSurrealEdge,
    BaseSurrealModel,
    Count,
    Index,
    Q,
    QueryCache,
    SearchScore,
    Subquery,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    SurrealDbError,
    Var,
    VectorField,
    hybrid,
    search,
    vector,
)
from surreal_orm_lite.query_set import QuerySet
from surreal_orm_lite.testing import embedded_database
from surreal_orm_lite.utils import validate_field_name
from tests import _compaction
from tests._readme_tables import assert_embedded_rows, assert_server_header_sentence, assert_server_rows
from tests._readme_tables import normalised as _normalised
from tests._readme_tables import readme as _readme
from tests._readme_tables import readme_part as _readme_part
from tests._readme_tables import subsection as _subsection
from tests._readme_tables import table_rows as _table_rows
from tests._readme_tables import tables as _md_tables

#: The subject ``hybrid_search()`` gives the shared refusal texts.
_SUBJECT = "hybrid_search()"

#: SurrealDB's refusal of a query vector of two numbers on a vector index of dimension 3 (as ``tests/test_vector.py``).
_DIMENSION_ERROR = "Incorrect vector dimension (2). Expected a vector of 3 dimension."

#: SurrealDB's refusal of a match on ``title``, which has no full-text index.
_NO_INDEX = "There was no suitable index supporting the expression: title @0@ $_f1"

#: SurrealDB's refusal of a match whose column cannot be read.
_NO_INDEX_UNREADABLE = "There was no suitable index supporting the expression: title 'words'"

#: The advice that ends the missing-index refusal (``tests/test_search.py``'s ``_SI1_ADVICE``).
_SI1_ADVICE = (
    "SurrealDB matches only a field with a FULLTEXT (3.x) or SEARCH (2.x) index: declare Index(..., search_analyzer=...) "
    "on the model and apply define_indexes()."
)


def _raised(error: type[Exception], call: Any, *args: Any, **kwargs: Any) -> str:
    """The text of the *error* ``call(*args, **kwargs)`` raises, its class checked exactly."""
    with pytest.raises(error) as raised:
        call(*args, **kwargs)
    assert type(raised.value) is error
    return str(raised.value)


class TestSubjectKeywords:
    """Each vector and full-text text ``hybrid_search()`` shares takes ``subject=`` (``arg=`` for a limit) and comes
    out as spec §5.5 writes it (HV3–HV9, HV11, HD1, HT3–HT5, HS1); without the keyword it is v0.35.0/v0.36.0's text."""

    # --- the query vector: HV4, HV5 ---------------------------------------------------------------------------------

    def test_hv4_a_vector_that_is_not_a_list(self) -> None:
        assert _raised(TypeError, vector.checked_query_vector, "x", subject=_SUBJECT) == (
            "hybrid_search() takes the vector as a list or tuple of numbers, got str; convert an array with .tolist()."
        )

    def test_hv5_an_empty_vector(self) -> None:
        assert _raised(ValueError, vector.checked_query_vector, [], subject=_SUBJECT) == (
            "hybrid_search(): the vector is empty."
        )

    def test_hv4_an_item_that_is_not_a_number(self) -> None:
        assert _raised(TypeError, vector.checked_query_vector, [1, "a"], subject=_SUBJECT) == (
            "hybrid_search(): vector item 1 is str, not a number."
        )

    def test_hv5_an_item_that_is_not_finite(self) -> None:
        assert _raised(ValueError, vector.checked_query_vector, [1, float("nan")], subject=_SUBJECT) == (
            "hybrid_search(): vector item 1 is not finite (nan)."
        )

    def test_the_query_vector_is_returned_with_a_subject(self) -> None:
        assert vector.checked_query_vector((1, 2.5), subject=_SUBJECT) == [1.0, 2.5]

    def test_the_query_vector_texts_without_a_subject(self) -> None:
        assert _raised(TypeError, vector.checked_query_vector, "x") == (
            "similar_to() takes the vector as a list or tuple of numbers, got str; convert an array with .tolist()."
        )
        assert _raised(ValueError, vector.checked_query_vector, []) == "similar_to(): the vector is empty."
        assert _raised(TypeError, vector.checked_query_vector, [1, "a"]) == (
            "similar_to(): vector item 1 is str, not a number."
        )
        assert _raised(ValueError, vector.checked_query_vector, [1, float("nan")]) == (
            "similar_to(): vector item 1 is not finite (nan)."
        )

    # --- the limits: HV7 (vector_limit), HT5 (text_limit) -----------------------------------------------------------

    def test_hv7_vector_limit(self) -> None:
        def refused(error: type[Exception], value: object) -> str:
            return _raised(error, vector.checked_limit, value, subject=_SUBJECT, arg="vector_limit")

        assert refused(TypeError, True) == "hybrid_search() vector_limit must be an int, got bool."
        assert refused(TypeError, "4") == "hybrid_search() vector_limit must be an int, got str."
        assert refused(ValueError, 0) == "hybrid_search() vector_limit must be between 1 and 10000, got 0."
        assert refused(ValueError, 10_001) == "hybrid_search() vector_limit must be between 1 and 10000, got 10001."

    def test_ht5_text_limit(self) -> None:
        def refused(error: type[Exception], value: object) -> str:
            return _raised(error, vector.checked_limit, value, subject=_SUBJECT, arg="text_limit")

        assert refused(TypeError, True) == "hybrid_search() text_limit must be an int, got bool."
        assert refused(TypeError, 2.0) == "hybrid_search() text_limit must be an int, got float."
        assert refused(ValueError, 0) == "hybrid_search() text_limit must be between 1 and 10000, got 0."
        assert refused(ValueError, -1) == "hybrid_search() text_limit must be between 1 and 10000, got -1."

    def test_a_limit_in_range_is_returned_with_a_subject(self) -> None:
        assert vector.checked_limit(1, subject=_SUBJECT, arg="vector_limit") == 1
        assert vector.checked_limit(10_000, subject=_SUBJECT, arg="text_limit") == 10_000

    def test_the_limit_texts_without_a_subject(self) -> None:
        assert _raised(TypeError, vector.checked_limit, True) == "similar_to() limit must be an int, got bool."
        assert _raised(ValueError, vector.checked_limit, 0) == "similar_to() limit must be between 1 and 10000, got 0."

    # --- ef: HV8; ef with dist: HV9 ---------------------------------------------------------------------------------

    def test_hv8_ef(self) -> None:
        assert _raised(TypeError, vector.checked_ef, "40", subject=_SUBJECT) == (
            "hybrid_search() ef must be an int or None, got str."
        )
        assert _raised(TypeError, vector.checked_ef, True, subject=_SUBJECT) == (
            "hybrid_search() ef must be an int or None, got bool."
        )
        assert _raised(ValueError, vector.checked_ef, 0, subject=_SUBJECT) == (
            "hybrid_search() ef must be between 1 and 10000, got 0."
        )
        assert vector.checked_ef(None, subject=_SUBJECT) is None
        assert vector.checked_ef(40, subject=_SUBJECT) == 40

    def test_the_ef_texts_without_a_subject(self) -> None:
        assert _raised(TypeError, vector.checked_ef, "40") == "similar_to() ef must be an int or None, got str."
        assert _raised(ValueError, vector.checked_ef, 0) == "similar_to() ef must be between 1 and 10000, got 0."

    def test_hv9_ef_and_dist(self) -> None:
        assert vector.ef_and_dist_refused(subject=_SUBJECT) == (
            "hybrid_search() takes ef= (the HNSW index's search) or dist= (an exact scan without an index), not both."
        )
        assert vector.ef_and_dist_refused() == (
            "similar_to() takes ef= (the HNSW index's search) or dist= (an exact scan without an index), not both."
        )

    # --- the vector field: HV3 --------------------------------------------------------------------------------------

    def test_hv3_the_record_id_and_an_endpoint(self) -> None:
        assert vector.field_refused("id", endpoint=False, subject=_SUBJECT) == (
            "hybrid_search() compares vectors: 'id' is the record id, not a vector field."
        )
        assert vector.field_refused("in", endpoint=True, subject=_SUBJECT) == (
            "hybrid_search() compares vectors: 'in' is an edge endpoint, not a vector field."
        )
        assert vector.field_refused("id", endpoint=False) == (
            "similar_to() compares vectors: 'id' is the record id, not a vector field."
        )
        assert vector.field_refused("in", endpoint=True) == (
            "similar_to() compares vectors: 'in' is an edge endpoint, not a vector field."
        )

    # --- the dimension: HV6 (declared), HD1 (the server's refusal) -------------------------------------------------

    def test_hv6_the_declared_dimension(self) -> None:
        assert vector.dimension_refused("embedding", 2, 3, "VectorField[3]", subject=_SUBJECT) == (
            "hybrid_search(): the vector has 2 dimensions; field 'embedding' holds vectors of 3 (VectorField[3])."
        )
        assert vector.dimension_refused("embedding", 2, 3, "VectorField[3]") == (
            "similar_to(): the vector has 2 dimensions; field 'embedding' holds vectors of 3 (VectorField[3])."
        )

    def test_hd1_the_server_s_dimension_refusal(self) -> None:
        assert vector.dimension_error_refused(["embedding"], 2, 3, subject=_SUBJECT) == (
            "hybrid_search(): the vector has 2 dimensions; the vector index on field 'embedding' expects 3."
        )
        assert vector.dimension_error_refused(["embedding", "summary_vec"], 2, 3, subject=_SUBJECT) == (
            "hybrid_search(): the vector has 2 dimensions; the vector index on one of the fields 'embedding', "
            "'summary_vec' expects 3."
        )
        assert vector.dimension_error_refused(["embedding"], 2, 3) == (
            "similar_to(): the vector has 2 dimensions; the vector index on field 'embedding' expects 3."
        )

    def test_hd1_the_refusal_read_from_the_server_s_error(self) -> None:
        error = InternalError("Internal", _DIMENSION_ERROR)
        refusal = vector.dimension_refusal(error, {"embedding": "embedding"}, subject=_SUBJECT)
        assert type(refusal) is SurrealDbError
        assert str(refusal) == (
            "hybrid_search(): the vector has 2 dimensions; the vector index on field 'embedding' expects 3."
        )
        assert str(vector.dimension_refusal(error, {"embedding": "embedding"})) == (
            "similar_to(): the vector has 2 dimensions; the vector index on field 'embedding' expects 3."
        )
        # Any other error is still relayed, whatever the subject.
        other = InternalError("Internal", "There was a problem with the database")
        assert vector.dimension_refusal(other, {"embedding": "embedding"}, subject=_SUBJECT) is None

    # --- a zero vector with COSINE: HV11 ----------------------------------------------------------------------------

    def test_hv11_a_zero_vector(self) -> None:
        assert vector.zero_vector_refused(subject=_SUBJECT) == (
            "hybrid_search(): a zero vector has no COSINE distance (SurrealDB answers NaN); use another vector or distance."
        )
        assert vector.zero_vector_refused() == (
            "similar_to(): a zero vector has no COSINE distance (SurrealDB answers NaN); use another vector or distance."
        )

    # --- the text field and the text: HT3, HT4 ----------------------------------------------------------------------

    def test_ht3_the_record_id_and_an_endpoint(self) -> None:
        assert search.field_refused("id", endpoint=False, subject=_SUBJECT) == (
            "hybrid_search() matches text: 'id' is the record id, not a text field."
        )
        assert search.field_refused("out", endpoint=True, subject=_SUBJECT) == (
            "hybrid_search() matches text: 'out' is an edge endpoint, not a text field."
        )
        assert search.field_refused("id", endpoint=False) == ("search() matches text: 'id' is the record id, not a text field.")
        assert search.field_refused("out", endpoint=True) == (
            "search() matches text: 'out' is an edge endpoint, not a text field."
        )

    def test_ht4_a_text_that_is_not_a_str(self) -> None:
        assert search.text_refused("title", 3, subject=_SUBJECT) == (
            "hybrid_search() takes a str to match in 'title', got int."
        )
        assert search.text_refused("title", 3) == "search() takes a str to match in 'title', got int."

    # --- no full-text index: HS1 ------------------------------------------------------------------------------------

    def test_hs1_no_full_text_index(self) -> None:
        assert search.search_index_refused(["title"], subject=_SUBJECT) == (
            f"hybrid_search(): no full-text index on field 'title'. {_SI1_ADVICE}"
        )
        assert search.search_index_refused(["title", "category"], subject=_SUBJECT) == (
            f"hybrid_search(): no full-text index on one of the fields 'title', 'category'. {_SI1_ADVICE}"
        )
        assert search.search_index_refused(["title"]) == f"search(): no full-text index on field 'title'. {_SI1_ADVICE}"

    def test_hs1_the_refusal_read_from_the_server_s_error(self) -> None:
        refusal = search.missing_index_refusal(InternalError("Internal", _NO_INDEX), {"title": "title"}, subject=_SUBJECT)
        assert type(refusal) is SurrealDbError
        assert str(refusal) == f"hybrid_search(): no full-text index on field 'title'. {_SI1_ADVICE}"
        unreadable = InternalError("Internal", _NO_INDEX_UNREADABLE)
        both = search.missing_index_refusal(unreadable, {"title": "title", "head_text": "heading"}, subject=_SUBJECT)
        assert str(both) == f"hybrid_search(): no full-text index on one of the fields 'title', 'heading'. {_SI1_ADVICE}"
        assert str(search.missing_index_refusal(InternalError("Internal", _NO_INDEX), {"title": "title"})) == (
            f"search(): no full-text index on field 'title'. {_SI1_ADVICE}"
        )
        # Any other error is still relayed, whatever the subject.
        other = InternalError("Internal", "There was a problem")
        assert search.missing_index_refusal(other, {"title": "title"}, subject=_SUBJECT) is None


# --- the hybrid module: the checks and the texts ---------------------------------------------------------------------

#: The text ``validate_field_name()`` gives a field name that is not one, after ``Invalid <context> name '<field>': ``.
_FIELD_NAME_RULE = (
    "must contain only alphanumeric characters, underscores, and dots (for nested fields), and start with a letter or "
    "underscore"
)


class TestHybridChecks:
    """``hybrid``'s constants, the checks of ``vector_field``/``text_field`` (HV1, HV2, HT1, HT2) and ``rrf_k`` (HK1),
    the texts of HY1, HY2 and HY4 (spec §5.5), and ``reserved()`` (D16). There is no HY3: nothing is set as an
    attribute."""

    # --- the constants ----------------------------------------------------------------------------------------------

    def test_the_constants(self) -> None:
        assert hybrid.DEFAULT_LIMIT == 20
        assert hybrid.DEFAULT_RRF_K == 60
        assert hybrid.MAX_RRF_K == 10_000
        assert hybrid.SUBJECT == "hybrid_search()"
        assert hybrid.RRF_SCORE == "_rrf_score"
        assert hybrid.SEARCH_SCORE == "_search_score"
        assert hybrid.SERVER_SCORE == "rrf_score"
        assert hybrid.POSITION == "_hybrid_pos"
        assert hybrid.VECTOR_POSITION == "_hybrid_vec_pos"
        assert hybrid.TEXT_POSITION == "_hybrid_text_pos"
        assert vector.DISTANCE_ALIAS == "_knn_distance"

    def test_the_reserved_columns_in_hy4_s_order(self) -> None:
        assert hybrid.RESERVED_COLUMNS == (
            "rrf_score",
            "_rrf_score",
            "_knn_distance",
            "_search_score",
            "_hybrid_pos",
            "_hybrid_vec_pos",
            "_hybrid_text_pos",
        )

    def test_the_keys_each_list_leaves_out(self) -> None:
        # D15: the 3.x lists' OMIT names, and the keys fuse() drops from each list's rows.
        assert hybrid.KNN_OMITTED == ("_search_score",)
        assert hybrid.TEXT_OMITTED == ("_knn_distance", "_hybrid_vec_pos")

    def test_the_refused_clauses(self) -> None:
        assert hybrid.REFUSED_CLAUSES == ("annotate()", "order_by()", "search()", "similar_to()", "values()")

    def test_nothing_is_set_as_an_attribute(self) -> None:
        # D2/D9: dicts, not instances — no attribute names, no HY3.
        assert not hasattr(hybrid, "ATTRIBUTES")
        assert not hasattr(hybrid, "attribute_refused")

    def test_the_module_imports_no_queryset_model_or_connection_module(self) -> None:
        tree = ast.parse(Path(hybrid.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(f"{node.module or ''}.{alias.name}".lstrip(".") for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        for module in ("query_set", "model_base", "connection_manager", "edge", "model_auth"):
            assert not any(
                name == module or name.endswith(f".{module}") or name.startswith(f"{module}.") for name in imported
            ), module

    # --- vector_field and text_field: HV1, HV2, HT1, HT2 ------------------------------------------------------------

    @pytest.mark.parametrize("arg", ["vector_field", "text_field"])
    def test_hv1_ht1_a_field_that_is_not_a_str(self, arg: str) -> None:
        assert _raised(TypeError, hybrid.checked_field, 3, arg) == (
            f"hybrid_search() takes {arg} as a field name (str), not int."
        )
        assert _raised(TypeError, hybrid.checked_field, None, arg) == (
            f"hybrid_search() takes {arg} as a field name (str), not NoneType."
        )

    @pytest.mark.parametrize("arg", ["vector_field", "text_field"])
    def test_hv2_ht2_a_str_that_is_not_a_field_name(self, arg: str) -> None:
        text = _raised(ValueError, hybrid.checked_field, "a b", arg)
        assert text == f"Invalid hybrid_search {arg} name 'a b': {_FIELD_NAME_RULE}"
        with pytest.raises(ValueError) as raised:
            validate_field_name("a b", f"hybrid_search {arg}")
        assert text == str(raised.value)
        assert _raised(ValueError, hybrid.checked_field, "", arg) == f"hybrid_search {arg} name cannot be empty"

    @pytest.mark.parametrize("arg", ["vector_field", "text_field"])
    def test_hv2_ht2_a_trailing_newline_is_refused_with_the_same_text(self, arg: str) -> None:
        # validate_field_name()'s pattern ends with $, which accepts a trailing newline: the full match refuses it.
        assert _raised(ValueError, hybrid.checked_field, "embedding\n", arg) == (
            f"Invalid hybrid_search {arg} name 'embedding\n': {_FIELD_NAME_RULE}"
        )

    @pytest.mark.parametrize("arg", ["vector_field", "text_field"])
    def test_a_field_name_or_a_dotted_path_is_returned(self, arg: str) -> None:
        assert hybrid.checked_field("embedding", arg) == "embedding"
        assert hybrid.checked_field("meta.vec", arg) == "meta.vec"
        assert hybrid.checked_field("_private", arg) == "_private"

    # --- rrf_k: HK1 -------------------------------------------------------------------------------------------------

    @pytest.mark.parametrize(("value", "kind"), [(True, "bool"), ("60", "str"), (60.0, "float"), (None, "NoneType")])
    def test_hk1_an_rrf_k_that_is_not_an_int(self, value: object, kind: str) -> None:
        assert _raised(TypeError, hybrid.checked_rrf_k, value) == f"hybrid_search() rrf_k must be an int, got {kind}."

    @pytest.mark.parametrize("value", [-1, 10_001])
    def test_hk1_an_rrf_k_out_of_range(self, value: int) -> None:
        assert _raised(ValueError, hybrid.checked_rrf_k, value) == (
            f"hybrid_search() rrf_k must be between 0 and 10000, got {value!r}."
        )

    @pytest.mark.parametrize("value", [0, 1, 60, 10_000])
    def test_an_rrf_k_in_range_is_returned(self, value: int) -> None:
        assert hybrid.checked_rrf_k(value) == value

    # --- the texts: HY1, HY2, HY4 -----------------------------------------------------------------------------------

    def test_hy1_the_clauses(self) -> None:
        assert hybrid.clauses_refused(["annotate()", "order_by()"]) == (
            "hybrid_search() cannot be combined with annotate(), order_by(): it runs its own vector and full-text "
            "searches and orders the records by their fused score. Remove them from the queryset."
        )

    def test_hy1_names_every_offender_sorted(self) -> None:
        assert hybrid.clauses_refused(["values()", "order_by()", "search()"]) == (
            "hybrid_search() cannot be combined with order_by(), search(), values(): it runs its own vector and "
            "full-text searches and orders the records by their fused score. Remove them from the queryset."
        )
        assert hybrid.clauses_refused(["similar_to()"]) == (
            "hybrid_search() cannot be combined with similar_to(): it runs its own vector and full-text searches and "
            "orders the records by their fused score. Remove them from the queryset."
        )

    def test_hy2_time_travel(self) -> None:
        assert hybrid.time_travel_refused() == (
            "hybrid_search() cannot be combined with as_of(): the vector and full-text indexes hold the records' "
            "current values, so a time-travel read would rank the records by their values now. Run it without as_of()."
        )

    def test_hy4_a_field_stored_under_a_reserved_name(self) -> None:
        assert hybrid.column_refused("Doc", "score", "rrf_score") == (
            "hybrid_search() cannot read Doc: its field 'score' is stored as rrf_score, a name hybrid_search() uses "
            "for its own values, which would replace it. Store it under another name (Field(alias=...))."
        )
        assert hybrid.column_refused("Doc", "relevance", "_search_score") == (
            "hybrid_search() cannot read Doc: its field 'relevance' is stored as _search_score, a name hybrid_search() "
            "uses for its own values, which would replace it. Store it under another name (Field(alias=...))."
        )

    # --- reserved(): the select() items both lists leave out (D16) --------------------------------------------------

    @pytest.mark.parametrize(
        "column",
        [
            "rrf_score",
            "_rrf_score",
            "_knn_distance",
            "_search_score",
            "_hybrid_pos",
            "_hybrid_vec_pos",
            "_hybrid_text_pos",
            "_knn_distance.x",
            "_hybrid_vec_pos.x.y",
        ],
    )
    def test_a_reserved_column_or_path(self, column: str) -> None:
        assert hybrid.reserved(column) is True

    @pytest.mark.parametrize("column", ["title", "rrf", "meta._knn_distance", "*", "_search", "rrf_score_x", "author.*"])
    def test_a_column_that_is_not_reserved(self, column: str) -> None:
        assert hybrid.reserved(column) is False


# --- the hybrid module: the fusion ------------------------------------------------------------------------------------

#: The titles of the corpus (spec §9.3), by record key.
_TITLES = {
    "d1": "Cats and dogs",
    "d2": "Cats at home",
    "d3": "Rust programming",
    "d4": "Python programming",
    "d5": "Gardening",
    "d6": "Cats and code",
    "d7": "Rust and Python programming",
    "d8": "Cooking",
}

# The distances and scores the two lists hold on 2.7.0 (any floats in the right order serve: fuse() reads ranks).
_Q2_DISTANCES = {"d3": 0.0, "d4": 0.0061162757863052475, "d7": 0.029857499854668, "d6": 0.29289321881345254}
_V_DISTANCES = {"d1": 0.0, "d2": 0.0061162757863052475, "d6": 0.29289321881345254}
_PROGRAMMING = {"d3": 0.4831963777542114, "d4": 0.4831963777542114, "d7": 0.3531397581100464}
_CATS = {"d1": 0.36464953422546387, "d2": 0.36464953422546387, "d6": 0.36464953422546387}


def _rid(key: str) -> RecordID:
    """The record id of corpus record *key* (``d1`` … ``d8``)."""
    return RecordID("hy_doc", key)


def _knn(distances: dict[str, float | None], *keys: str) -> list[dict[str, Any]]:
    """A KNN list as the server returns it below 3: one row per key, in order, each with its ``_knn_distance``."""
    return [{"id": _rid(key), "title": _TITLES[key], "_knn_distance": distances[key]} for key in keys]


def _text(scores: dict[str, float], *keys: str) -> list[dict[str, Any]]:
    """A full-text list as the server returns it below 3: one row per key, in order, each with its ``_search_score``."""
    return [{"id": _rid(key), "title": _TITLES[key], "_search_score": scores[key]} for key in keys]


#: Each case of spec §9.3: its two lists on 2.7.0, its vector_limit, and the records it fuses to — key, score (the
#: Python expression, summed in list order), whether the record holds a distance, whether it holds a score.
_CASES: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]], int, list[tuple[str, float, bool, bool]]]] = {
    "C1": (
        _knn(_Q2_DISTANCES, "d3", "d4", "d7", "d6"),
        _text(_PROGRAMMING, "d3", "d4", "d7"),
        4,
        [
            ("d3", 1 / 61 + 1 / 61, True, True),
            ("d4", 1 / 62 + 1 / 62, True, True),
            ("d7", 1 / 63 + 1 / 63, True, True),
            ("d6", 1 / 64, True, False),
        ],
    ),
    "C2": (
        _knn(_V_DISTANCES, "d1", "d2", "d6"),
        _text(_PROGRAMMING, "d3", "d4", "d7"),
        3,
        [
            ("d1", 1 / 61, True, False),
            ("d3", 1 / 61, False, True),
            ("d2", 1 / 62, True, False),
            ("d4", 1 / 62, False, True),
            ("d6", 1 / 63, True, False),
            ("d7", 1 / 63, False, True),
        ],
    ),
    "C5": (
        _knn(_Q2_DISTANCES, "d3", "d4", "d7"),
        _text(_PROGRAMMING, "d3", "d4", "d7"),
        3,
        [
            ("d3", 1 / 61 + 1 / 61, True, True),
            ("d4", 1 / 62 + 1 / 62, True, True),
            ("d7", 1 / 63 + 1 / 63, True, True),
        ],
    ),
    "C6": (
        _knn(_V_DISTANCES, "d1", "d2"),
        _text(_CATS, "d1", "d2"),
        2,
        [("d1", 1 / 61 + 1 / 61, True, True), ("d2", 1 / 62 + 1 / 62, True, True)],
    ),
    "C7": (
        _knn(_Q2_DISTANCES, "d3", "d4"),
        _text(_CATS, "d1", "d2", "d6"),
        2,
        [
            ("d3", 1 / 61, True, False),
            ("d1", 1 / 61, False, True),
            ("d4", 1 / 62, True, False),
            ("d2", 1 / 62, False, True),
            ("d6", 1 / 63, False, True),
        ],
    ),
}


def _check(fused: list[dict[str, Any]], expected: list[tuple[str, float, bool, bool]]) -> None:
    """*fused* holds exactly the *expected* records, in order: each ``rrf_score`` equal (``==``) to its expression, a
    distance and a score exactly where expected."""
    assert [row["id"] for row in fused] == [_rid(key) for key, _, _, _ in expected]
    for row, (key, score, distance, text_score) in zip(fused, expected, strict=True):
        assert type(row) is dict, key
        assert row["rrf_score"] == score, key
        assert type(row["rrf_score"]) is float, key
        assert ("_knn_distance" in row) is distance, key
        assert ("_search_score" in row) is text_score, key


class TestFuse:
    """``hybrid.fuse()`` (AC-4): the ORM's Reciprocal Rank Fusion below SurrealDB 3 — each record scores Σ 1/(k + rank)
    over the lists it is in, the records best first, equal scores in the vector list's order then the text list's
    (spec §6.1, D15)."""

    @pytest.mark.parametrize("case", sorted(_CASES))
    def test_the_cases_of_the_corpus(self, case: str) -> None:
        knn, text, vector_limit, expected = _CASES[case]
        _check(hybrid.fuse([knn, text], k=60, vector_limit=vector_limit), expected)

    def test_c3_and_c4_page_the_fused_records(self) -> None:
        knn, text, vector_limit, _ = _CASES["C2"]
        fused = hybrid.fuse([knn, text], k=60, vector_limit=vector_limit)
        assert [row["id"] for row in fused[0:3]] == [_rid("d1"), _rid("d3"), _rid("d2")]  # C3: .limit(3)
        assert [row["id"] for row in fused[1:3]] == [_rid("d3"), _rid("d2")]  # C4: .offset(1).limit(2)

    def test_c8_the_lists_of_surrealdb_3_1_and_3_2(self) -> None:
        # No vector index: 3.1 and 3.2 list every record by id, with no distance; LIMIT 3 keeps d1, d2, d3.
        knn = _knn(dict.fromkeys(("d1", "d2", "d3")), "d1", "d2", "d3")
        fused = hybrid.fuse([knn, _text(_PROGRAMMING, "d3", "d4", "d7")], k=60, vector_limit=3)
        assert [row["id"] for row in fused] == [_rid(key) for key in ("d3", "d1", "d2", "d4", "d7")]
        assert [row["rrf_score"] for row in fused] == [1 / 63 + 1 / 61, 1 / 61, 1 / 62, 1 / 62, 1 / 63]
        assert [row.get("_knn_distance", "absent") for row in fused] == [None, None, None, "absent", "absent"]
        assert [row.get("_search_score", "absent") for row in fused] == [
            _PROGRAMMING["d3"],
            "absent",
            "absent",
            _PROGRAMMING["d4"],
            _PROGRAMMING["d7"],
        ]

    def test_the_values_of_a_record_found_by_both_lists(self) -> None:
        knn, text, vector_limit, _ = _CASES["C1"]
        fused = hybrid.fuse([knn, text], k=60, vector_limit=vector_limit)
        assert fused[0] == {
            "id": _rid("d3"),
            "title": "Rust programming",
            "_knn_distance": 0.0,
            "_search_score": _PROGRAMMING["d3"],
            "rrf_score": 1 / 61 + 1 / 61,
        }
        assert fused[3] == {
            "id": _rid("d6"),
            "title": "Cats and code",
            "_knn_distance": _Q2_DISTANCES["d6"],
            "rrf_score": 1 / 64,
        }

    def test_the_text_list_s_value_wins_for_a_shared_key(self) -> None:
        knn = _knn(_Q2_DISTANCES, "d3")
        text = [{"id": _rid("d3"), "title": "Rust programming, as indexed", "_search_score": 0.5}]
        (row,) = hybrid.fuse([knn, text], k=60, vector_limit=1)
        assert row == {
            "id": _rid("d3"),
            "title": "Rust programming, as indexed",
            "_knn_distance": 0.0,
            "_search_score": 0.5,
            "rrf_score": 1 / 61 + 1 / 61,
        }

    def test_a_row_without_an_id_is_dropped(self) -> None:
        knn = [*_knn(_V_DISTANCES, "d1", "d2"), {"title": "no id", "_knn_distance": 0.5}]
        text = [*_text(_PROGRAMMING, "d3"), {"title": "no id either", "_search_score": 0.1}]
        fused = hybrid.fuse([knn, text], k=60, vector_limit=3)
        assert [row["id"] for row in fused] == [_rid("d1"), _rid("d3"), _rid("d2")]
        assert [row["rrf_score"] for row in fused] == [1 / 61, 1 / 61, 1 / 62]

    def test_a_row_that_is_not_an_object_is_dropped(self) -> None:
        knn: list[Any] = [*_knn(_V_DISTANCES, "d1"), _rid("d5"), None, "d8", {"id": None, "_knn_distance": 0.9}]
        fused = hybrid.fuse([knn, []], k=60, vector_limit=5)
        assert [row["id"] for row in fused] == [_rid("d1")]

    def test_a_list_that_is_not_a_list_is_ignored(self) -> None:
        lists: list[Any] = [None, _text(_PROGRAMMING, "d3")]
        assert [(row["id"], row["rrf_score"]) for row in hybrid.fuse(lists, k=60, vector_limit=3)] == [(_rid("d3"), 1 / 61)]
        # A third list is not fused.
        lists = [_knn(_V_DISTANCES, "d1"), [], _text(_PROGRAMMING, "d3")]
        assert [row["id"] for row in hybrid.fuse(lists, k=60, vector_limit=3)] == [_rid("d1")]

    def test_no_list_or_two_empty_lists(self) -> None:
        assert hybrid.fuse([], k=60, vector_limit=3) == []
        assert hybrid.fuse([[], []], k=60, vector_limit=3) == []

    def test_one_empty_list(self) -> None:
        fused = hybrid.fuse([[], _text(_PROGRAMMING, "d3", "d4", "d7")], k=60, vector_limit=3)
        assert [(row["id"], row["rrf_score"]) for row in fused] == [
            (_rid("d3"), 1 / 61),
            (_rid("d4"), 1 / 62),
            (_rid("d7"), 1 / 63),
        ]
        fused = hybrid.fuse([_knn(_V_DISTANCES, "d1", "d2"), []], k=60, vector_limit=2)
        assert [(row["id"], row["rrf_score"]) for row in fused] == [(_rid("d1"), 1 / 61), (_rid("d2"), 1 / 62)]

    def test_a_row_s_own_rrf_score_is_replaced(self) -> None:
        knn = [{**_knn(_V_DISTANCES, "d1")[0], "rrf_score": 500.0}]
        text = [{**_text(_PROGRAMMING, "d3")[0], "rrf_score": 400.0}]
        fused = hybrid.fuse([knn, text], k=60, vector_limit=1)
        assert [(row["id"], row["rrf_score"]) for row in fused] == [(_rid("d1"), 1 / 61), (_rid("d3"), 1 / 61)]

    def test_k_0_scores_one_over_the_rank(self) -> None:
        fused = hybrid.fuse([_knn(_V_DISTANCES, "d1", "d2"), _text(_PROGRAMMING, "d3")], k=0, vector_limit=2)
        assert [(row["id"], row["rrf_score"]) for row in fused] == [
            (_rid("d1"), 1.0),
            (_rid("d3"), 1.0),
            (_rid("d2"), 0.5),
        ]
        fused = hybrid.fuse([_knn(_V_DISTANCES, "d1", "d2"), _text(_CATS, "d2")], k=0, vector_limit=2)
        assert [(row["id"], row["rrf_score"]) for row in fused] == [(_rid("d2"), 1 / 2 + 1 / 1), (_rid("d1"), 1.0)]

    def test_a_higher_score_comes_first_whatever_its_list(self) -> None:
        # d4 is second in both lists (2/62) and passes d1, first in the KNN list only (1/61).
        knn = _knn({"d1": 0.0, "d4": 0.1}, "d1", "d4")
        text = _text({"d3": 0.9, "d4": 0.5}, "d3", "d4")
        fused = hybrid.fuse([knn, text], k=60, vector_limit=2)
        assert [(row["id"], row["rrf_score"]) for row in fused] == [
            (_rid("d4"), 1 / 62 + 1 / 62),
            (_rid("d1"), 1 / 61),
            (_rid("d3"), 1 / 61),
        ]

    def test_equal_scores_keep_the_vector_list_first_then_the_text_list(self) -> None:
        # The text list's own records come after every KNN record of the same score, in the text list's order.
        knn = _knn(_V_DISTANCES, "d6", "d2", "d1")
        text = _text(_PROGRAMMING, "d7", "d4", "d3")
        fused = hybrid.fuse([knn, text], k=60, vector_limit=3)
        assert [row["id"] for row in fused] == [_rid(key) for key in ("d6", "d7", "d2", "d4", "d1", "d3")]

    def test_the_identity_of_a_record(self) -> None:
        assert hybrid.identity(RecordID("t", 1)) != hybrid.identity(RecordID("t", "1"))
        assert hybrid.identity(RecordID("t", 1)) == hybrid.identity(RecordID("t", 1))
        assert hybrid.identity(RecordID("t", "d3")) == hybrid.identity(copy.deepcopy(RecordID("t", "d3")))
        assert hybrid.identity(RecordID("t", "d3")) != hybrid.identity(RecordID("u", "d3"))
        assert hybrid.identity(RecordID("t", ["a", 1])) == hybrid.identity(RecordID("t", ["a", 1]))

    def test_records_of_two_tables_or_two_id_types_are_not_merged(self) -> None:
        knn = [{"id": RecordID("t", 1), "_knn_distance": 0.0}]
        text = [{"id": RecordID("t", "1"), "_search_score": 0.5}, {"id": RecordID("u", 1), "_search_score": 0.4}]
        fused = hybrid.fuse([knn, text], k=60, vector_limit=1)
        assert [(row["id"], row["rrf_score"]) for row in fused] == [
            (RecordID("t", 1), 1 / 61),
            (RecordID("t", "1"), 1 / 61),
            (RecordID("u", 1), 1 / 62),
        ]

    # --- D15: a stored column named as another list's key is never returned -----------------------------------------

    def test_stored_reserved_columns_never_replace_the_lists_values(self) -> None:
        # M25: a SCHEMALESS record storing _search_score (700.0) reaches the KNN list's rows with it, one storing
        # _knn_distance (900.0) and _hybrid_vec_pos (-3) the text list's rows: each list keeps only its own key.
        knn = [{**row, "_search_score": 700.0} for row in _knn(_Q2_DISTANCES, "d3", "d4", "d6")]
        text = [{**row, "_knn_distance": 900.0, "_hybrid_vec_pos": -3} for row in _text({"d1": 0.6, "d3": 0.4}, "d1", "d3")]
        fused = hybrid.fuse([knn, text], k=60, vector_limit=3)
        assert [(row["id"].id, row["rrf_score"]) for row in fused] == [
            ("d3", 1 / 61 + 1 / 62),
            ("d1", 1 / 61),
            ("d4", 1 / 62),
            ("d6", 1 / 63),
        ]
        rows = {row["id"].id: row for row in fused}
        # In the KNN list only: its distance, no _search_score.
        for key in ("d4", "d6"):
            assert rows[key] == {
                "id": _rid(key),
                "title": _TITLES[key],
                "_knn_distance": _Q2_DISTANCES[key],
                "rrf_score": rows[key]["rrf_score"],
            }, key
        # In the text list only: its score, neither _knn_distance nor _hybrid_vec_pos.
        assert rows["d1"] == {"id": _rid("d1"), "title": _TITLES["d1"], "_search_score": 0.6, "rrf_score": 1 / 61}
        # In both: the KNN row's distance and the text row's score.
        assert rows["d3"] == {
            "id": _rid("d3"),
            "title": _TITLES["d3"],
            "_knn_distance": 0.0,
            "_search_score": 0.4,
            "rrf_score": 1 / 61 + 1 / 62,
        }

    def test_the_stored_values_of_m25_on_the_c2_lists(self) -> None:
        # C2 on a table storing the seven names: the clean table's records, scores and order (equal scores included);
        # the other names are the row builder's to drop or overwrite.
        stored = {
            "rrf_score": 500.0,
            "_rrf_score": 400.0,
            "_knn_distance": 900.0,
            "_search_score": 700.0,
            "_hybrid_pos": -5,
            "_hybrid_vec_pos": -3,
            "_hybrid_text_pos": -4,
        }
        knn = [{**stored, **row} for row in _knn(_V_DISTANCES, "d1", "d2", "d6")]
        text = [{**stored, **row} for row in _text(_PROGRAMMING, "d3", "d4", "d7")]
        fused = hybrid.fuse([knn, text], k=60, vector_limit=3)
        _check(fused, _CASES["C2"][3])
        for row in fused:
            assert 900.0 not in row.values(), row["id"]
            assert 700.0 not in row.values(), row["id"]
            assert row["rrf_score"] != 500.0, row["id"]
        assert [row["id"].id for row in fused if "_hybrid_vec_pos" not in row] == ["d3", "d4", "d7"]

    def test_the_input_lists_and_rows_are_never_changed(self) -> None:
        knn = [{**row, "rrf_score": 500.0, "_search_score": 700.0} for row in _knn(_V_DISTANCES, "d1", "d2", "d6")]
        text = [
            {**row, "rrf_score": 400.0, "_knn_distance": 900.0, "_hybrid_vec_pos": -3}
            for row in _text({"d3": 0.4, "d6": 0.3}, "d3", "d6")
        ]
        lists = [knn, text]
        before = copy.deepcopy(lists)
        fused = hybrid.fuse(lists, k=60, vector_limit=3)
        assert lists == before
        assert all(fused_row is not row for fused_row in fused for row in [*knn, *text])
        assert [(row["id"].id, row["rrf_score"]) for row in fused] == [
            ("d6", 1 / 63 + 1 / 62),
            ("d1", 1 / 61),
            ("d3", 1 / 61),
            ("d2", 1 / 62),
        ]
        assert (fused[0]["_knn_distance"], fused[0]["_search_score"]) == (_V_DISTANCES["d6"], 0.3)


# --- the statement: _hybrid_runner() and _compile_hybrid() -----------------------------------------------------------

#: The analyzer of the corpus's full-text indexes.
HY_PLAIN = Analyzer("hy_plain", ["blank"], ["lowercase"])


class HyDoc(BaseSurrealModel):
    """The model of the statements and of the corpus: a ``COSINE`` vector index on ``embedding``, a full-text index on
    ``title``."""

    model_config = SurrealConfigDict(
        table_name="hy_doc",
        indexes=[
            Index("hy_doc_embedding", "embedding", hnsw=True, dimension=3, dist="cosine"),
            Index("hy_doc_title", "title", search_analyzer=HY_PLAIN),
        ],
    )

    id: str | None = None
    title: str
    category: str = ""
    n: int = 0
    embedding: VectorField[3]


class HyAuthor(BaseSurrealModel):
    """The record a ``HyLinked`` record links to."""

    model_config = SurrealConfigDict(table_name="hy_author")

    id: str | None = None
    name: str = ""


class HyLinked(BaseSurrealModel):
    """``HyDoc``'s fields and indexes on a model with a record link, ``author``: ``fetch()`` and a condition through
    the link."""

    model_config = SurrealConfigDict(
        table_name="hy_linked",
        indexes=[
            Index("hy_linked_embedding", "embedding", hnsw=True, dimension=3, dist="cosine"),
            Index("hy_linked_title", "title", search_analyzer=HY_PLAIN),
        ],
    )

    id: str | None = None
    title: str
    category: str = ""
    n: int = 0
    embedding: VectorField[3]
    author: HyAuthor | RecordID | None = None


#: The arguments of the statements: the vector search's four nearest to ``[0, 1, 0]``, the four best "programming".
_ARGS: dict[str, Any] = {
    "vector_field": "embedding",
    "vector": [0.0, 1.0, 0.0],
    "vector_limit": 4,
    "text_field": "title",
    "text_query": "programming",
    "text_limit": 4,
}

#: The query vector of ``_ARGS``, as it is bound.
_VECTOR = [0.0, 1.0, 0.0]

#: ``HyDoc.objects()`` on SurrealDB 3.x: both lists inline in ``search::rrf()``, each leaving out the other's value.
_G1 = (
    "SELECT *, IF _hybrid_vec_pos != NONE THEN _hybrid_vec_pos ELSE 4 + _hybrid_text_pos END AS _hybrid_pos FROM "
    "search::rrf([(SELECT *, vector::distance::knn() AS _knn_distance OMIT _search_score FROM hy_doc WHERE embedding "
    "<|4,100|> $_f0 ORDER BY _knn_distance, id LIMIT 4).map(|$r, $i| object::extend($r, {_hybrid_vec_pos: $i})), "
    "(SELECT *, search::score(0) AS _search_score OMIT _knn_distance, _hybrid_vec_pos FROM hy_doc WHERE title @0@ $_f1 "
    "ORDER BY _search_score DESC, id LIMIT 4).map(|$r, $i| object::extend($r, {_hybrid_text_pos: $i}))], 8, 60) ORDER "
    "BY rrf_score DESC, _hybrid_pos;"
)

#: ``HyDoc.objects().filter(category="tech").offset(1).limit(2)`` on SurrealDB 3.x: the condition in both lists, the
#: fused records paged.
_G2 = (
    "SELECT *, IF _hybrid_vec_pos != NONE THEN _hybrid_vec_pos ELSE 4 + _hybrid_text_pos END AS _hybrid_pos FROM "
    "search::rrf([(SELECT *, vector::distance::knn() AS _knn_distance OMIT _search_score FROM hy_doc WHERE category = "
    "$_f0 AND embedding <|4,100|> $_f1 ORDER BY _knn_distance, id LIMIT 4).map(|$r, $i| object::extend($r, "
    "{_hybrid_vec_pos: $i})), (SELECT *, search::score(0) AS _search_score OMIT _knn_distance, _hybrid_vec_pos FROM "
    "hy_doc WHERE category = $_f2 AND title @0@ $_f3 ORDER BY _search_score DESC, id LIMIT 4).map(|$r, $i| "
    "object::extend($r, {_hybrid_text_pos: $i}))], 8, 60) ORDER BY rrf_score DESC, _hybrid_pos LIMIT 2 START 1;"
)

#: ``_G2``'s variables, on every line.
_G2_VARIABLES = {"_f0": "tech", "_f1": _VECTOR, "_f2": "tech", "_f3": "programming"}

#: ``_G2``'s queryset below SurrealDB 3: the KNN list's condition selected first, the lists in ``LET``s, no paging
#: (the ORM pages once it fused them).
_G3 = (
    "{ LET $_sq0 = (SELECT VALUE id FROM hy_doc WHERE category = $_f0); LET $_sq1 = (SELECT *, vector::distance::knn() "
    "AS _knn_distance FROM hy_doc WHERE id IN $_sq0 AND embedding <|4,100|> $_f1 ORDER BY _knn_distance, id LIMIT 4); "
    "LET $_sq2 = (SELECT *, search::score(0) AS _search_score FROM hy_doc WHERE category = $_f2 AND title @0@ $_f3 ORDER "
    "BY _search_score DESC, id LIMIT 4); ([$_sq1, $_sq2]) };"
)

#: ``_G1``'s queryset below SurrealDB 3: no condition, so no pre-filter.
_G4 = (
    "{ LET $_sq0 = (SELECT *, vector::distance::knn() AS _knn_distance FROM hy_doc WHERE embedding <|4,100|> $_f0 ORDER "
    "BY _knn_distance, id LIMIT 4); LET $_sq1 = (SELECT *, search::score(0) AS _search_score FROM hy_doc WHERE title "
    "@0@ $_f1 ORDER BY _search_score DESC, id LIMIT 4); ([$_sq0, $_sq1]) };"
)

#: ``_G4`` with ``dist="cosine"`` once the line is read below SurrealDB 3: the four nearest ranked by distance then id in
#: a ``LET``, the vector list searching their ids.
_G4_EXACT = (
    "{ LET $_sq0 = (SELECT id, 1 - vector::similarity::cosine(embedding, $_f0) AS _knn_distance FROM hy_doc WHERE "
    "type::is::array(embedding) AND array::len(embedding) = 3 AND type::is::number(array::min(embedding)) AND "
    "type::is::number(array::max(embedding)) ORDER BY _knn_distance, id LIMIT 4).id; LET $_sq1 = (SELECT *, "
    "vector::distance::knn() AS _knn_distance FROM hy_doc WHERE id IN $_sq0 AND embedding <|4,COSINE|> $_f0 ORDER BY "
    "_knn_distance, id LIMIT 4); LET $_sq2 = (SELECT *, search::score(0) AS _search_score FROM hy_doc WHERE title @0@ "
    "$_f1 ORDER BY _search_score DESC, id LIMIT 4); ([$_sq1, $_sq2]) };"
)
#: ``_G4_EXACT`` with ``filter(category="tech")``: the condition ranked with the nearest, and in the full-text list.
_G4_EXACT_TECH = (
    "{ LET $_sq0 = (SELECT id, 1 - vector::similarity::cosine(embedding, $_f1) AS _knn_distance FROM hy_doc WHERE "
    "category = $_f0 AND type::is::array(embedding) AND array::len(embedding) = 3 AND "
    "type::is::number(array::min(embedding)) AND type::is::number(array::max(embedding)) ORDER BY _knn_distance, id "
    "LIMIT 4).id; LET $_sq1 = (SELECT *, vector::distance::knn() AS _knn_distance FROM hy_doc WHERE id IN $_sq0 AND "
    "embedding <|4,COSINE|> $_f1 ORDER BY _knn_distance, id LIMIT 4); LET $_sq2 = (SELECT *, search::score(0) AS "
    "_search_score FROM hy_doc WHERE category = $_f2 AND title @0@ $_f3 ORDER BY _search_score DESC, id LIMIT 4); "
    "([$_sq1, $_sq2]) };"
)

#: ``HyLinked.objects().fetch("author")`` on SurrealDB 3.x: each list fetches the link, before its ``.map()``.
_G6 = (
    "SELECT *, IF _hybrid_vec_pos != NONE THEN _hybrid_vec_pos ELSE 4 + _hybrid_text_pos END AS _hybrid_pos FROM "
    "search::rrf([(SELECT *, vector::distance::knn() AS _knn_distance OMIT _search_score FROM hy_linked WHERE embedding "
    "<|4,100|> $_f0 ORDER BY _knn_distance, id LIMIT 4 FETCH author).map(|$r, $i| object::extend($r, {_hybrid_vec_pos: "
    "$i})), (SELECT *, search::score(0) AS _search_score OMIT _knn_distance, _hybrid_vec_pos FROM hy_linked WHERE title "
    "@0@ $_f1 ORDER BY _search_score DESC, id LIMIT 4 FETCH author).map(|$r, $i| object::extend($r, {_hybrid_text_pos: "
    "$i}))], 8, 60) ORDER BY rrf_score DESC, _hybrid_pos;"
)

#: ``_G6``'s queryset below SurrealDB 3: each ``LET`` fetches the link.
_G6_BELOW_3 = (
    "{ LET $_sq0 = (SELECT *, vector::distance::knn() AS _knn_distance FROM hy_linked WHERE embedding <|4,100|> $_f0 "
    "ORDER BY _knn_distance, id LIMIT 4 FETCH author); LET $_sq1 = (SELECT *, search::score(0) AS _search_score FROM "
    "hy_linked WHERE title @0@ $_f1 ORDER BY _search_score DESC, id LIMIT 4 FETCH author); ([$_sq0, $_sq1]) };"
)

#: ``HyDoc.objects().filter(id__in=Subquery(HyDoc.objects().filter(n__gt=2)))`` on SurrealDB 3.x: the subquery bound
#: once per list, the statement inside the block.
_G7 = (
    "{ LET $_sq0 = (SELECT VALUE id FROM hy_doc WHERE n > $_f0); LET $_sq1 = (SELECT VALUE id FROM hy_doc WHERE n > "
    "$_f2); (SELECT *, IF _hybrid_vec_pos != NONE THEN _hybrid_vec_pos ELSE 4 + _hybrid_text_pos END AS _hybrid_pos "
    "FROM search::rrf([(SELECT *, vector::distance::knn() AS _knn_distance OMIT _search_score FROM hy_doc WHERE id IN "
    "$_sq0 AND embedding <|4,100|> $_f1 ORDER BY _knn_distance, id LIMIT 4).map(|$r, $i| object::extend($r, "
    "{_hybrid_vec_pos: $i})), (SELECT *, search::score(0) AS _search_score OMIT _knn_distance, _hybrid_vec_pos FROM "
    "hy_doc WHERE id IN $_sq1 AND title @0@ $_f3 ORDER BY _search_score DESC, id LIMIT 4).map(|$r, $i| "
    "object::extend($r, {_hybrid_text_pos: $i}))], 8, 60) ORDER BY rrf_score DESC, _hybrid_pos) };"
)

#: ``_G7``'s queryset below SurrealDB 3: each list's ``LET`` after its subquery's; the id subquery stays next to the
#: KNN, so no pre-filter.
_G7_BELOW_3 = (
    "{ LET $_sq0 = (SELECT VALUE id FROM hy_doc WHERE n > $_f0); LET $_sq1 = (SELECT *, vector::distance::knn() AS "
    "_knn_distance FROM hy_doc WHERE id IN $_sq0 AND embedding <|4,100|> $_f1 ORDER BY _knn_distance, id LIMIT 4); LET "
    "$_sq2 = (SELECT VALUE id FROM hy_doc WHERE n > $_f2); LET $_sq3 = (SELECT *, search::score(0) AS _search_score "
    "FROM hy_doc WHERE id IN $_sq2 AND title @0@ $_f3 ORDER BY _search_score DESC, id LIMIT 4); ([$_sq1, $_sq3]) };"
)

#: ``_G7``'s variables, on every line.
_G7_VARIABLES = {"_f0": 2, "_f1": _VECTOR, "_f2": 2, "_f3": "programming"}


def _knn_of(queryset: QuerySet, **args: Any) -> vector.KnnSearch:
    """The vector search ``hybrid_search(**_ARGS, **args)`` builds for *queryset* (its arguments taken as checked)."""
    call = {**_ARGS, "ef": None, "dist": None, **args}
    field = call["vector_field"]
    column = queryset.model.to_db_field(field)
    return vector.KnnSearch(field, column, tuple(call["vector"]), call["vector_limit"], call["ef"], call["dist"])


def _runner(queryset: QuerySet, **args: Any) -> QuerySet:
    """The private copy ``hybrid_search(**_ARGS, **args)`` runs for *queryset*."""
    call = {**_ARGS, **args}
    return queryset._hybrid_runner(_knn_of(queryset, **args), call["text_field"], call["text_query"])


def _compiled(queryset: QuerySet, *, below_3: bool, knn_prefilter: bool = False, **args: Any) -> tuple[str, dict[str, Any]]:
    """The statement and variables ``hybrid_search(**_ARGS, **args)`` sends for *queryset*, compiled on a fresh compiler
    of its runner in the form for SurrealDB 3.x, or below 3 (*below_3*), the KNN's other conditions selected first when
    *knn_prefilter*."""
    call = {**_ARGS, "rrf_k": 60, **args}
    runner = _runner(queryset, **args)
    compiler = runner._subquery_compiler(knn_prefilter=knn_prefilter)
    return runner._compile_hybrid(compiler, below_3=below_3, text_limit=call["text_limit"], rrf_k=call["rrf_k"])


def _selected(statement: str, projection: str) -> str:
    """*statement* (``_G1`` or ``_G4``) with each list's projection *projection* instead of ``*``."""
    return statement.replace("SELECT *, vector::", f"SELECT {projection}, vector::").replace(
        "SELECT *, search::", f"SELECT {projection}, search::"
    )


class TestCompileHybrid:
    """The statement ``hybrid_search()`` sends, compiled on its runner — the queryset's private copy holding both
    searches —, exactly: on SurrealDB 3.x the two lists inline in ``search::rrf()``, each with its ``OMIT``, the ORM's
    order and paging; below 3 the lists in ``LET``s, without ``OMIT``, the block's value both lists. Each list's
    ``LIMIT``, the queryset's conditions, ``select()`` (a reserved item left out, on both lines) and ``fetch()``; the
    queryset unchanged, its link heads included."""

    # --- the forms of each line -------------------------------------------------------------------------------------

    def test_g1_both_lists_inline_on_3x(self) -> None:
        assert _compiled(HyDoc.objects(), below_3=False) == (_G1, {"_f0": _VECTOR, "_f1": "programming"})

    def test_g2_a_condition_in_both_lists_and_the_fused_records_paged_on_3x(self) -> None:
        queryset = HyDoc.objects().filter(category="tech").offset(1).limit(2)
        assert _compiled(queryset, below_3=False) == (_G2, _G2_VARIABLES)

    def test_g3_the_knn_condition_selected_first_below_3(self) -> None:
        queryset = HyDoc.objects().filter(category="tech").offset(1).limit(2)
        assert _compiled(queryset, below_3=True, knn_prefilter=True) == (_G3, _G2_VARIABLES)

    def test_g4_the_lists_in_lets_below_3(self) -> None:
        expected = (_G4, {"_f0": _VECTOR, "_f1": "programming"})
        assert _compiled(HyDoc.objects(), below_3=True) == expected
        assert _compiled(HyDoc.objects(), below_3=True, knn_prefilter=True) == expected

    def test_a_knn_next_to_a_condition_asks_for_the_line(self) -> None:
        # The flag _run_uncached() and _run_cached() read the line by (the pre-filter below 3, the exact scan's ranking),
        # as for similar_to().
        for queryset, dist, expected in [
            (HyDoc.objects(), None, False),
            (HyDoc.objects().filter(category="tech"), None, True),
            (HyDoc.objects().filter(id__in=Subquery(HyDoc.objects().filter(n__gt=2))), None, False),
            (HyDoc.objects(), "COSINE", True),
        ]:
            runner = _runner(queryset, dist=dist)
            compiler = runner._subquery_compiler()
            runner._compile_hybrid(compiler, below_3=False, text_limit=4, rrf_k=60)
            assert compiler.knn_conditions is expected, (queryset._filters, dist)

    def test_a_fresh_compiler_compiles_the_same_statement_again(self) -> None:
        runner = _runner(HyDoc.objects().filter(category="tech"))
        for below_3 in (False, True):
            first = runner._compile_hybrid(runner._subquery_compiler(), below_3=below_3, text_limit=4, rrf_k=60)
            second = runner._compile_hybrid(runner._subquery_compiler(), below_3=below_3, text_limit=4, rrf_k=60)
            assert first == second

    # --- select(), the KNN's options, the limits and rrf_k ----------------------------------------------------------

    def test_g5_select_reads_id_and_the_items_in_both_lists(self) -> None:
        for statement, below_3 in [(_G1, False), (_G4, True)]:
            assert _compiled(HyDoc.objects().select("title"), below_3=below_3)[0] == _selected(statement, "id, title")
            assert _compiled(HyDoc.objects().select("id", "title"), below_3=below_3)[0] == _selected(statement, "id, title")
            assert _compiled(HyDoc.objects().select("title", "id"), below_3=below_3)[0] == _selected(statement, "title, id")
            assert _compiled(HyDoc.objects().select("*"), below_3=below_3)[0] == statement

    def test_g5_on_3x_the_omit_follows_the_alias(self) -> None:
        query = _compiled(HyDoc.objects().select("title"), below_3=False)[0]
        assert "(SELECT id, title, vector::distance::knn() AS _knn_distance OMIT _search_score FROM hy_doc " in query
        assert "(SELECT id, title, search::score(0) AS _search_score OMIT _knn_distance, _hybrid_vec_pos FROM " in query

    def test_g5_an_item_naming_a_reserved_column_is_left_out_on_both_lines(self) -> None:
        for below_3 in (False, True):
            plain = _compiled(HyDoc.objects().select("title"), below_3=below_3)
            for reserved in ["_knn_distance", "_search_score", "rrf_score", "_hybrid_vec_pos.x"]:
                assert _compiled(HyDoc.objects().select("title", reserved), below_3=below_3) == plain, reserved
            alone = _compiled(HyDoc.objects().select("_knn_distance"), below_3=below_3)[0]
            assert alone == _selected(_G4 if below_3 else _G1, "id")

    def test_g5_an_exact_scan(self) -> None:
        cosine = vector.checked_distance("cosine", subject=_SUBJECT)
        assert _compiled(HyDoc.objects(), below_3=False, dist=cosine)[0] == _G1.replace("<|4,100|>", "<|4,COSINE|>")
        assert _compiled(HyDoc.objects(), below_3=True, dist=cosine)[0] == _G4.replace("<|4,100|>", "<|4,COSINE|>")
        # Below 3 (the line read: knn_prefilter) the vector list searches the four nearest ranked by distance then id.
        ranked = _compiled(HyDoc.objects(), below_3=True, knn_prefilter=True, dist=cosine)
        assert ranked == (_G4_EXACT, {"_f0": _VECTOR, "_f1": "programming"})
        tech = _compiled(HyDoc.objects().filter(category="tech"), below_3=True, knn_prefilter=True, dist=cosine)[0]
        assert tech == _G4_EXACT_TECH

    def test_g5_ef_raised_to_the_vector_limit(self) -> None:
        query = _compiled(HyDoc.objects(), below_3=False, ef=40, vector_limit=50)[0]
        assert query == (
            _G1.replace("<|4,100|>", "<|50,50|>")
            .replace("ELSE 4 + ", "ELSE 50 + ")
            .replace("_knn_distance, id LIMIT 4)", "_knn_distance, id LIMIT 50)")
            .replace("], 8, 60)", "], 54, 60)")
        )
        below = _compiled(HyDoc.objects(), below_3=True, ef=40, vector_limit=50)[0]
        assert below == _G4.replace("<|4,100|>", "<|50,50|>").replace(
            "_knn_distance, id LIMIT 4)", "_knn_distance, id LIMIT 50)"
        )

    def test_g5_rrf_k(self) -> None:
        assert _compiled(HyDoc.objects(), below_3=False, rrf_k=0)[0] == _G1.replace("], 8, 60)", "], 8, 0)")
        assert _compiled(HyDoc.objects(), below_3=False, rrf_k=10_000)[0] == _G1.replace("], 8, 60)", "], 8, 10000)")
        # Below 3 the ORM fuses: rrf_k is not written.
        assert _compiled(HyDoc.objects(), below_3=True, rrf_k=0)[0] == _G4

    def test_g5_the_text_limit(self) -> None:
        query = _compiled(HyDoc.objects(), below_3=False, text_limit=7)[0]
        assert query == _G1.replace("DESC, id LIMIT 4)", "DESC, id LIMIT 7)").replace("], 8, 60)", "], 11, 60)")
        assert "ELSE 4 + _hybrid_text_pos" in query
        below = _compiled(HyDoc.objects(), below_3=True, text_limit=7)[0]
        assert below == _G4.replace("DESC, id LIMIT 4)", "DESC, id LIMIT 7)")

    def test_limit_and_offset_page_the_fused_records_on_3x_only(self) -> None:
        assert _compiled(HyDoc.objects().limit(3), below_3=False)[0] == _G1.replace("_hybrid_pos;", "_hybrid_pos LIMIT 3;")
        assert _compiled(HyDoc.objects().offset(2), below_3=False)[0] == _G1.replace("_hybrid_pos;", "_hybrid_pos START 2;")
        assert _compiled(HyDoc.objects().limit(3).offset(2), below_3=True)[0] == _G4

    # --- fetch() and a Subquery filter ------------------------------------------------------------------------------

    def test_g6_fetch_in_both_lists(self) -> None:
        variables = {"_f0": _VECTOR, "_f1": "programming"}
        assert _compiled(HyLinked.objects().fetch("author"), below_3=False) == (_G6, variables)
        assert _compiled(HyLinked.objects().fetch("author"), below_3=True) == (_G6_BELOW_3, variables)

    def test_g7_a_subquery_filter_on_3x(self) -> None:
        queryset = HyDoc.objects().filter(id__in=Subquery(HyDoc.objects().filter(n__gt=2)))
        assert _compiled(queryset, below_3=False) == (_G7, _G7_VARIABLES)

    def test_g7_a_subquery_filter_below_3(self) -> None:
        queryset = HyDoc.objects().filter(id__in=Subquery(HyDoc.objects().filter(n__gt=2)))
        assert _compiled(queryset, below_3=True, knn_prefilter=True) == (_G7_BELOW_3, _G7_VARIABLES)
        assert _compiled(queryset, below_3=True) == (_G7_BELOW_3, _G7_VARIABLES)

    def test_the_queryset_s_variables_are_bound(self) -> None:
        queryset = HyDoc.objects().filter(n__gt=Var("lo")).variables(lo=3)
        query, variables = _compiled(queryset, below_3=False)
        assert "WHERE n > $lo AND embedding <|4,100|> $_f0 " in query
        assert "WHERE n > $lo AND title @0@ $_f1 " in query
        assert variables == {"lo": 3, "_f0": _VECTOR, "_f1": "programming"}

    def test_the_filter_values_warn_once(self) -> None:
        # "$lo" is the string, compared as data: the warning naming Var("lo") is given once, though both lists hold it.
        queryset = HyDoc.objects().filter(category="$lo").variables(lo="tech")
        runner = _runner(queryset)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            runner._compile_hybrid(runner._subquery_compiler(), below_3=False, text_limit=4, rrf_k=60)
        assert [str(warning.message) for warning in caught] == [
            "The filter value '$lo' is compared as the string '$lo', not with the variable lo given to variables(); to "
            "reference the variable, use Var('lo')."
        ]
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            runner._compile_hybrid(runner._subquery_compiler(warn=False), below_3=False, text_limit=4, rrf_k=60)
        assert caught == []

    # --- the runner: the queryset's private copy --------------------------------------------------------------------

    def test_the_runner_holds_both_searches_and_the_subject(self) -> None:
        queryset = HyDoc.objects()
        knn = _knn_of(queryset)
        runner = queryset._hybrid_runner(knn, "title", "programming")
        assert runner is not queryset
        assert runner._similar is knn
        assert runner._search == [("title", "title", "programming")]
        assert runner._refusal_subject == "hybrid_search()"
        assert queryset._refusal_subject is None
        assert QuerySet(HyDoc)._refusal_subject is None

    def test_the_queryset_is_never_changed(self) -> None:
        queryset = HyLinked.objects().filter(category="tech").select("title").fetch("author").offset(1).limit(2)

        def state() -> tuple[Any, ...]:
            return (
                queryset._compile_query(),
                queryset._similar,
                list(queryset._search),
                queryset._limit,
                queryset._offset,
                set(queryset._link_heads),
                queryset._refusal_subject,
                list(queryset.select_item),
                list(queryset._filters),
                list(queryset._fetch_fields),
                dict(queryset._variables),
                sorted(vars(queryset)),
            )

        before = state()
        for below_3 in (False, True):
            _compiled(queryset, below_3=below_3, knn_prefilter=below_3)
        assert state() == before
        assert queryset._similar is None and queryset._search == []

    def test_the_runner_has_its_own_link_heads(self) -> None:
        queryset = HyLinked.objects().filter(**{"author.name": "Ann"})
        assert queryset._link_heads == {"author"}
        runner = _runner(queryset)
        assert runner._link_heads is not queryset._link_heads
        assert runner._link_heads == {"author"}
        runner._link_heads.add("other")
        assert queryset._link_heads == {"author"}

    def test_a_dotted_vector_path_records_its_head_on_the_runner_only(self) -> None:
        queryset = HyDoc.objects()
        runner = queryset._hybrid_runner(vector.KnnSearch("meta.vec", "meta.vec", (1.0, 0.0), 2, None, None), "title", "x")
        assert runner._link_heads == {"meta"}
        assert queryset._link_heads == set()
        assert runner._similar is not None and runner._similar.column == "meta.vec"

    def test_a_dotted_text_path_records_its_head_on_the_runner_only(self) -> None:
        queryset = HyDoc.objects()
        runner = queryset._hybrid_runner(_knn_of(queryset), "info.headline", "x")
        assert runner._link_heads == {"info"}
        assert runner._search == [("info.headline", "info.headline", "x")]
        assert queryset._link_heads == set()

    def test_a_condition_through_a_link_records_its_head_on_the_runner_only(self) -> None:
        # A Q object names its fields when it compiles: the queryset has recorded no link head yet.
        queryset = HyLinked.objects().filter(Q(**{"author.name": "Ann"}))
        assert queryset._link_heads == set()
        for below_3 in (False, True):
            runner = _runner(queryset)
            assert runner._link_heads == set()
            query = runner._compile_hybrid(runner._subquery_compiler(), below_3=below_3, text_limit=4, rrf_k=60)[0]
            assert "WHERE author.name = $_f0 AND embedding <|4,100|> $_f1 " in query
            assert "WHERE author.name = $_f2 AND title @0@ $_f3 " in query
            assert runner._link_heads == {"author"}
            assert queryset._link_heads == set()


# --- hybrid_search(): the checks, before any request ------------------------------------------------------------------


class HyScored(BaseSurrealModel):
    """``HyDoc``'s fields and ``score``, stored as ``rrf_score`` — SurrealDB's key for the fused score."""

    model_config = SurrealConfigDict(table_name="hy_scored")

    id: str | None = None
    title: str
    category: str = ""
    n: int = 0
    embedding: VectorField[3]
    score: float = Field(default=0.0, alias="rrf_score")


class HyRelevance(BaseSurrealModel):
    """``HyDoc``'s fields and ``relevance``, stored as ``_search_score`` — the key of a record's BM25 score."""

    model_config = SurrealConfigDict(table_name="hy_relevance")

    id: str | None = None
    title: str
    category: str = ""
    n: int = 0
    embedding: VectorField[3]
    relevance: float = Field(default=0.0, alias="_search_score")


class HyRrfField(BaseSurrealModel):
    """``HyDoc``'s fields and a field named ``rrf_score``."""

    model_config = SurrealConfigDict(table_name="hy_rrf_field")

    id: str | None = None
    title: str
    embedding: VectorField[3]
    rrf_score: float = 0.0


class HyBoth(BaseSurrealModel):
    """Two fields stored under reserved names: ``relevance`` (``_search_score``) declared before ``score``
    (``rrf_score``)."""

    model_config = SurrealConfigDict(table_name="hy_both")

    id: str | None = None
    title: str
    embedding: VectorField[3]
    relevance: float = Field(default=0.0, alias="_search_score")
    score: float = Field(default=0.0, alias="rrf_score")


class HyShadow(BaseSurrealModel):
    """A model defining a private attribute ``_search_score``: no field is stored under that name."""

    model_config = SurrealConfigDict(table_name="hy_shadow")

    id: str | None = None
    title: str
    embedding: VectorField[3]
    _search_score: float = 0.0


class HyIndexed(BaseSurrealModel):
    """A vector index of dimension 3 (``EUCLIDEAN``, the default) on a field declared without ``VectorField``."""

    model_config = SurrealConfigDict(
        table_name="hy_indexed",
        indexes=[
            Index("hy_indexed_embedding", "embedding", hnsw=True, dimension=3),
            Index("hy_indexed_title", "title", search_analyzer=HY_PLAIN),
        ],
    )

    id: str | None = None
    title: str
    embedding: list[float] | None = None


class HyEdge(BaseSurrealEdge):
    """An edge model: its endpoints are neither vector nor text fields."""

    model_config = SurrealConfigDict(table_name="hy_edge")

    title: str = ""
    embedding: VectorField[3]


class HyAliased(BaseSurrealModel):
    """``HyDoc``'s fields and indexes and ``label``, stored as ``label_text``: a dict keys it ``label``."""

    model_config = SurrealConfigDict(
        table_name="hy_aliased",
        indexes=[
            Index("hy_aliased_embedding", "embedding", hnsw=True, dimension=3, dist="cosine"),
            Index("hy_aliased_title", "title", search_analyzer=HY_PLAIN),
        ],
    )

    id: str | None = None
    title: str
    category: str = ""
    n: int = 0
    embedding: VectorField[3]
    label: str = Field(default="", alias="label_text")


#: An absolute moment for ``as_of()``: refused by ``hybrid_search()`` before it is ever resolved.
_MOMENT = datetime(2026, 1, 1, tzinfo=UTC)

#: HY1, the refusal of the queryset's clauses, for the offenders named.
_HY1 = (
    "hybrid_search() cannot be combined with {}: it runs its own vector and full-text searches and orders the records "
    "by their fused score. Remove them from the queryset."
)

#: HY2, the refusal of ``as_of()``.
_HY2 = (
    "hybrid_search() cannot be combined with as_of(): the vector and full-text indexes hold the records' current "
    "values, so a time-travel read would rank the records by their values now. Run it without as_of()."
)

#: HY4, the refusal of a model whose field (its Python name) is stored under a reserved name.
_HY4 = (
    "hybrid_search() cannot read {}: its field {!r} is stored as {}, a name hybrid_search() uses for its own values, "
    "which would replace it. Store it under another name (Field(alias=...))."
)

#: HV9, both ``ef`` and ``dist``.
_HV9 = "hybrid_search() takes ef= (the HNSW index's search) or dist= (an exact scan without an index), not both."

#: HV11, a zero vector with ``COSINE``.
_HV11 = "hybrid_search(): a zero vector has no COSINE distance (SurrealDB answers NaN); use another vector or distance."


async def _forbidden_client(*args: Any, **kwargs: Any) -> Any:
    pytest.fail("a client was acquired")


@pytest.fixture
def _no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """No client may be acquired: a refusal comes before any request."""
    monkeypatch.setattr(SurrealDBConnectionManager, "get_client", _forbidden_client)


def _snapshot(queryset: QuerySet) -> tuple[dict[str, Any], tuple[str, dict[str, Any]]]:
    """Every attribute of *queryset* (its containers copied), then the statement it compiles — on a copy holding its
    own set of link heads, so that compiling it changes nothing."""
    attributes = {
        name: copy.copy(value) if isinstance(value, list | set | dict) else value for name, value in vars(queryset).items()
    }
    probe = copy.copy(queryset)
    probe._link_heads = set(queryset._link_heads)
    return attributes, probe._compile_query(None if queryset._as_of is None else _MOMENT)


async def _refused(error: type[Exception], queryset: QuerySet, **args: Any) -> str:
    """The text of the *error* ``queryset.hybrid_search(**_ARGS, **args)`` raises, its class checked exactly; the
    queryset is checked unchanged after it, its link heads included."""
    before = _snapshot(queryset)
    with pytest.raises(error) as raised:
        await queryset.hybrid_search(**{**_ARGS, **args})
    assert type(raised.value) is error
    assert _snapshot(queryset) == before
    return str(raised.value)


async def _reaches_the_request(queryset: QuerySet, **args: Any) -> None:
    """``queryset.hybrid_search(**_ARGS, **args)`` passes every check: it goes on to acquire a client, which
    ``_no_client`` forbids."""
    before = _snapshot(queryset)
    with pytest.raises(pytest.fail.Exception, match="a client was acquired"):
        await queryset.hybrid_search(**{**_ARGS, **args})
    assert _snapshot(queryset) == before


def _invalid_name(field: str, arg: str) -> str:
    """``validate_field_name()``'s text for *field*, given as ``hybrid_search()``'s *arg*."""
    return _raised(ValueError, validate_field_name, field, f"hybrid_search {arg}")


@pytest.mark.usefixtures("_no_client")
class TestHybridSearchChecks:
    """``hybrid_search()``'s signature — the full SurrealDB-ORM's names, keyword-only, its defaults, a coroutine
    returning ``list[dict[str, Any]]`` — and its checks, in a fixed order: the queryset's clauses (HY1), ``as_of()``
    (HY2), a field stored under a reserved name (HY4), ``vector_field`` (HV1–HV3), the vector (HV4, HV5),
    ``vector_limit`` (HV7), ``ef`` (HV8), ``ef`` with ``dist`` (HV9), ``dist`` (HV10), the declared dimension (HV6), a
    zero vector with ``COSINE`` (HV11), ``text_field`` (HT1–HT3), ``text_query`` (HT4), ``text_limit`` (HT5), ``rrf_k``
    (HK1). Each refusal with its exact class and text, before any client is acquired, the queryset unchanged."""

    def test_the_signature(self) -> None:
        parameters = list(inspect.signature(QuerySet.hybrid_search).parameters.values())[1:]
        empty = inspect.Parameter.empty
        assert [(parameter.name, parameter.default) for parameter in parameters] == [
            ("vector_field", empty),
            ("vector", empty),
            ("vector_limit", 20),
            ("text_field", empty),
            ("text_query", empty),
            ("text_limit", 20),
            ("rrf_k", 60),
            ("ef", None),
            ("dist", None),
        ]
        assert {parameter.kind for parameter in parameters} == {inspect.Parameter.KEYWORD_ONLY}
        assert typing.get_type_hints(QuerySet.hybrid_search)["return"] == list[dict[str, Any]]
        assert inspect.iscoroutinefunction(QuerySet.hybrid_search)

    async def test_every_check_passed_goes_on_to_the_request(self) -> None:
        await _reaches_the_request(HyDoc.objects())
        await _reaches_the_request(HyDoc.objects().filter(category="tech").select("title").fetch("x").offset(1).limit(2))
        await _reaches_the_request(HyDoc.objects(), dist="euclidean", text_query="")

    # --- the queryset: HY1, HY2, HY4 --------------------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("clause", "make"),
        [
            ("annotate()", lambda: HyDoc.objects().annotate(count=Count())),
            ("order_by()", lambda: HyDoc.objects().order_by("-title")),
            ("search()", lambda: HyDoc.objects().search(title="rust")),
            ("similar_to()", lambda: HyDoc.objects().similar_to("embedding", _VECTOR, 2)),
            ("values()", lambda: HyDoc.objects().values("category")),
        ],
    )
    async def test_hy1_a_clause_the_method_cannot_honour(self, clause: str, make: Callable[[], QuerySet]) -> None:
        assert await _refused(ValueError, make()) == _HY1.format(clause)

    async def test_hy1_names_every_offender_sorted(self) -> None:
        both = HyDoc.objects().search(title="rust").order_by("title")
        assert await _refused(ValueError, both) == _HY1.format("order_by(), search()")
        every = (
            HyDoc.objects()
            .values("category")
            .annotate(count=Count())
            .order_by("title")
            .similar_to("embedding", _VECTOR, 2)
            .search(title="rust")
        )
        assert await _refused(ValueError, every) == _HY1.format("annotate(), order_by(), search(), similar_to(), values()")

    async def test_hy2_as_of(self) -> None:
        assert await _refused(SurrealDbError, HyDoc.objects().as_of(_MOMENT)) == _HY2

    @pytest.mark.parametrize(
        ("model", "field", "column"),
        [(HyScored, "score", "rrf_score"), (HyRelevance, "relevance", "_search_score"), (HyRrfField, "rrf_score", "rrf_score")],
    )
    async def test_hy4_a_field_stored_under_a_reserved_name(
        self, model: type[BaseSurrealModel], field: str, column: str
    ) -> None:
        assert await _refused(ValueError, model.objects()) == _HY4.format(model.__name__, field, column)

    async def test_hy4_names_the_first_offender_in_declaration_order(self) -> None:
        assert await _refused(ValueError, HyBoth.objects()) == _HY4.format("HyBoth", "relevance", "_search_score")

    async def test_a_private_attribute_named_as_a_reserved_column_is_not_refused(self) -> None:
        await _reaches_the_request(HyShadow.objects())

    # --- vector_field and text_field: HV1–HV3, HT1–HT3 --------------------------------------------------------------

    @pytest.mark.parametrize(("field", "kind"), [(3, "int"), (None, "NoneType"), (b"embedding", "bytes")])
    async def test_hv1_ht1_a_field_that_is_not_a_str(self, field: object, kind: str) -> None:
        assert await _refused(TypeError, HyDoc.objects(), vector_field=field) == (
            f"hybrid_search() takes vector_field as a field name (str), not {kind}."
        )
        assert await _refused(TypeError, HyDoc.objects(), text_field=field) == (
            f"hybrid_search() takes text_field as a field name (str), not {kind}."
        )

    @pytest.mark.parametrize("field", ["a b", "meta..vec", "1embedding", "embedding;", "", " "])
    async def test_hv2_ht2_a_field_that_is_not_a_name(self, field: str) -> None:
        refused = await _refused(ValueError, HyDoc.objects(), vector_field=field)
        assert refused == _invalid_name(field, "vector_field")
        assert "hybrid_search vector_field name " in refused
        refused = await _refused(ValueError, HyDoc.objects(), text_field=field)
        assert refused == _invalid_name(field, "text_field")
        assert "hybrid_search text_field name " in refused

    async def test_hv2_ht2_the_name_is_matched_in_full(self) -> None:
        # validate_field_name()'s pattern accepts a trailing newline; hybrid_search() refuses it with the same text.
        rule = (
            "must contain only alphanumeric characters, underscores, and dots (for nested fields), and start with a "
            "letter or underscore"
        )
        assert await _refused(ValueError, HyDoc.objects(), vector_field="embedding\n") == (
            f"Invalid hybrid_search vector_field name 'embedding\n': {rule}"
        )
        assert await _refused(ValueError, HyDoc.objects(), text_field="title\n") == (
            f"Invalid hybrid_search text_field name 'title\n': {rule}"
        )

    async def test_hv3_ht3_the_record_id(self) -> None:
        assert await _refused(ValueError, HyDoc.objects(), vector_field="id") == (
            "hybrid_search() compares vectors: 'id' is the record id, not a vector field."
        )
        assert await _refused(ValueError, HyDoc.objects(), text_field="id") == (
            "hybrid_search() matches text: 'id' is the record id, not a text field."
        )

    @pytest.mark.parametrize("field", ["in_", "in", "out"])
    async def test_hv3_ht3_an_edge_endpoint(self, field: str) -> None:
        assert await _refused(ValueError, HyEdge.objects(), vector_field=field) == (
            f"hybrid_search() compares vectors: {field!r} is an edge endpoint, not a vector field."
        )
        assert await _refused(ValueError, HyEdge.objects(), text_field=field) == (
            f"hybrid_search() matches text: {field!r} is an edge endpoint, not a text field."
        )

    async def test_an_aliased_or_dotted_field_is_accepted(self) -> None:
        await _reaches_the_request(HyAliased.objects(), text_field="label")
        await _reaches_the_request(HyDoc.objects(), vector_field="meta.vec", text_field="meta.headline")

    # --- the vector: HV4, HV5 ---------------------------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("value", "error", "text"),
        [
            (
                {"x": 1.0},
                TypeError,
                "hybrid_search() takes the vector as a list or tuple of numbers, got dict; convert an array with .tolist().",
            ),
            (
                "1,0,0",
                TypeError,
                "hybrid_search() takes the vector as a list or tuple of numbers, got str; convert an array with .tolist().",
            ),
            (
                None,
                TypeError,
                "hybrid_search() takes the vector as a list or tuple of numbers, got NoneType; convert an array with "
                ".tolist().",
            ),
            ([True, 0.0, 0.0], TypeError, "hybrid_search(): vector item 0 is bool, not a number."),
            ([1.0, "0", 0.0], TypeError, "hybrid_search(): vector item 1 is str, not a number."),
            ([], ValueError, "hybrid_search(): the vector is empty."),
            ((), ValueError, "hybrid_search(): the vector is empty."),
            ([1.0, math.nan, 0.0], ValueError, "hybrid_search(): vector item 1 is not finite (nan)."),
            ([1.0, 0.0, math.inf], ValueError, "hybrid_search(): vector item 2 is not finite (inf)."),
        ],
    )
    async def test_hv4_hv5_the_vector_and_its_items(self, value: object, error: type[Exception], text: str) -> None:
        assert await _refused(error, HyDoc.objects(), vector=value) == text

    async def test_a_tuple_of_real_numbers_is_accepted(self) -> None:
        await _reaches_the_request(HyDoc.objects(), vector=(0, 1, 0.5))

    # --- the limits, ef and dist: HV7–HV10, HT5 ---------------------------------------------------------------------

    @pytest.mark.parametrize(("value", "kind"), [(True, "bool"), ("3", "str"), (2.0, "float"), (None, "NoneType")])
    async def test_hv7_ht5_a_limit_that_is_not_an_int(self, value: object, kind: str) -> None:
        assert await _refused(TypeError, HyDoc.objects(), vector_limit=value) == (
            f"hybrid_search() vector_limit must be an int, got {kind}."
        )
        assert await _refused(TypeError, HyDoc.objects(), text_limit=value) == (
            f"hybrid_search() text_limit must be an int, got {kind}."
        )

    @pytest.mark.parametrize("value", [0, -1, 10_001, 2**32])
    async def test_hv7_ht5_a_limit_out_of_range(self, value: int) -> None:
        assert await _refused(ValueError, HyDoc.objects(), vector_limit=value) == (
            f"hybrid_search() vector_limit must be between 1 and 10000, got {value!r}."
        )
        assert await _refused(ValueError, HyDoc.objects(), text_limit=value) == (
            f"hybrid_search() text_limit must be between 1 and 10000, got {value!r}."
        )

    @pytest.mark.parametrize(("value", "kind"), [(True, "bool"), ("40", "str"), (40.0, "float")])
    async def test_hv8_an_ef_that_is_not_an_int(self, value: object, kind: str) -> None:
        assert await _refused(TypeError, HyDoc.objects(), ef=value) == f"hybrid_search() ef must be an int or None, got {kind}."

    @pytest.mark.parametrize("value", [0, -1, 10_001, 2**32])
    async def test_hv8_an_ef_out_of_range(self, value: int) -> None:
        assert await _refused(ValueError, HyDoc.objects(), ef=value) == (
            f"hybrid_search() ef must be between 1 and 10000, got {value!r}."
        )

    async def test_the_bounds_of_the_limits_and_ef_are_accepted(self) -> None:
        await _reaches_the_request(HyDoc.objects(), vector_limit=1, text_limit=1, ef=1)
        await _reaches_the_request(HyDoc.objects(), vector_limit=10_000, text_limit=10_000, ef=10_000)

    async def test_hv9_ef_and_dist(self) -> None:
        assert await _refused(ValueError, HyDoc.objects(), ef=40, dist="cosine") == _HV9

    @pytest.mark.parametrize(
        ("dist", "error", "text"),
        [
            (3, TypeError, "hybrid_search(): dist= must be a str, got int."),
            (
                "hamming2",
                ValueError,
                "hybrid_search(): unknown distance 'hamming2'; the ORM's are EUCLIDEAN, COSINE, MANHATTAN, CHEBYSHEV and "
                "HAMMING.",
            ),
            (
                "minkowski",
                ValueError,
                "hybrid_search(): MINKOWSKI needs its order, which this version does not write; use EUCLIDEAN (order 2) "
                "or MANHATTAN (order 1).",
            ),
            (
                "Jaccard",
                ValueError,
                "hybrid_search(): JACCARD is not a distance SurrealDB ranks by: identical vectors are not the nearest "
                "with it. Use COSINE.",
            ),
            (
                "PEARSON",
                ValueError,
                "hybrid_search(): PEARSON is not a distance SurrealDB ranks by: identical vectors are not the nearest "
                "with it. Use COSINE.",
            ),
        ],
    )
    async def test_hv10_a_distance_the_orm_does_not_write(self, dist: object, error: type[Exception], text: str) -> None:
        assert await _refused(error, HyDoc.objects(), dist=dist) == text

    # --- the declared dimension and a zero vector: HV6, HV11 --------------------------------------------------------

    async def test_hv6_the_dimension_vectorfield_declares(self) -> None:
        assert await _refused(ValueError, HyDoc.objects(), vector=[1.0, 0.0]) == (
            "hybrid_search(): the vector has 2 dimensions; field 'embedding' holds vectors of 3 (VectorField[3])."
        )

    async def test_hv6_the_dimension_a_vector_index_declares(self) -> None:
        assert await _refused(ValueError, HyIndexed.objects(), vector=[1.0, 0.0, 0.0, 0.0]) == (
            "hybrid_search(): the vector has 4 dimensions; field 'embedding' holds vectors of 3 (index 'hy_indexed_embedding')."
        )

    async def test_hv11_a_zero_vector_with_the_cosine_index(self) -> None:
        assert await _refused(ValueError, HyDoc.objects(), vector=[0.0, 0.0, 0.0]) == _HV11

    async def test_hv11_a_zero_vector_with_dist_cosine(self) -> None:
        assert await _refused(ValueError, HyIndexed.objects(), vector=[0, 0, 0], dist="cosine") == _HV11
        # Another distance measures a zero vector.
        await _reaches_the_request(HyIndexed.objects(), vector=[0, 0, 0])
        await _reaches_the_request(HyDoc.objects(), vector=[0, 0, 0], dist="euclidean")

    # --- text_query, rrf_k: HT4, HK1 --------------------------------------------------------------------------------

    @pytest.mark.parametrize(("value", "kind"), [(3, "int"), (None, "NoneType"), (b"rust", "bytes")])
    async def test_ht4_a_text_that_is_not_a_str(self, value: object, kind: str) -> None:
        assert await _refused(TypeError, HyDoc.objects(), text_query=value) == (
            f"hybrid_search() takes a str to match in 'title', got {kind}."
        )

    @pytest.mark.parametrize(("value", "kind"), [(True, "bool"), ("60", "str"), (60.0, "float"), (None, "NoneType")])
    async def test_hk1_an_rrf_k_that_is_not_an_int(self, value: object, kind: str) -> None:
        assert await _refused(TypeError, HyDoc.objects(), rrf_k=value) == f"hybrid_search() rrf_k must be an int, got {kind}."

    @pytest.mark.parametrize("value", [-1, 10_001, 2**63])
    async def test_hk1_an_rrf_k_out_of_range(self, value: int) -> None:
        assert await _refused(ValueError, HyDoc.objects(), rrf_k=value) == (
            f"hybrid_search() rrf_k must be between 0 and 10000, got {value!r}."
        )

    @pytest.mark.parametrize("value", [0, 10_000])
    async def test_the_bounds_of_rrf_k_are_accepted(self, value: int) -> None:
        await _reaches_the_request(HyDoc.objects(), rrf_k=value)

    # --- the order: of two faults, the earlier check's refusal ------------------------------------------------------

    @pytest.mark.parametrize(
        ("make", "args", "error", "text"),
        [
            pytest.param(
                lambda: HyDoc.objects().order_by("title").as_of(_MOMENT),
                {},
                ValueError,
                _HY1.format("order_by()"),
                id="HY1-before-HY2",
            ),
            pytest.param(lambda: HyScored.objects().as_of(_MOMENT), {}, SurrealDbError, _HY2, id="HY2-before-HY4"),
            pytest.param(
                lambda: HyScored.objects(),
                {"vector_field": 3},
                ValueError,
                _HY4.format("HyScored", "score", "rrf_score"),
                id="HY4-before-HV1",
            ),
            pytest.param(
                lambda: HyDoc.objects(),
                {"vector_field": "id", "vector": "x"},
                ValueError,
                "hybrid_search() compares vectors: 'id' is the record id, not a vector field.",
                id="HV3-before-HV4",
            ),
            pytest.param(
                lambda: HyDoc.objects(),
                {"vector": [], "vector_limit": 0},
                ValueError,
                "hybrid_search(): the vector is empty.",
                id="HV5-before-HV7",
            ),
            pytest.param(
                lambda: HyDoc.objects(),
                {"vector_limit": 0, "ef": 0},
                ValueError,
                "hybrid_search() vector_limit must be between 1 and 10000, got 0.",
                id="HV7-before-HV8",
            ),
            pytest.param(
                lambda: HyDoc.objects(),
                {"ef": 0, "dist": "cosine"},
                ValueError,
                "hybrid_search() ef must be between 1 and 10000, got 0.",
                id="HV8-before-HV9",
            ),
            pytest.param(lambda: HyDoc.objects(), {"ef": 40, "dist": "jaccard"}, ValueError, _HV9, id="HV9-before-HV10"),
            pytest.param(
                lambda: HyDoc.objects(),
                {"dist": 3, "vector": [1.0, 0.0]},
                TypeError,
                "hybrid_search(): dist= must be a str, got int.",
                id="HV10-before-HV6",
            ),
            pytest.param(
                lambda: HyDoc.objects(),
                {"vector": [0.0, 0.0]},
                ValueError,
                "hybrid_search(): the vector has 2 dimensions; field 'embedding' holds vectors of 3 (VectorField[3]).",
                id="HV6-before-HV11",
            ),
            pytest.param(
                lambda: HyDoc.objects(), {"vector": [0.0, 0.0, 0.0], "text_field": 3}, ValueError, _HV11, id="HV11-before-HT1"
            ),
            pytest.param(
                lambda: HyDoc.objects(),
                {"text_field": "id", "text_query": 3},
                ValueError,
                "hybrid_search() matches text: 'id' is the record id, not a text field.",
                id="HT3-before-HT4",
            ),
            pytest.param(
                lambda: HyDoc.objects(),
                {"text_query": 3, "text_limit": 0},
                TypeError,
                "hybrid_search() takes a str to match in 'title', got int.",
                id="HT4-before-HT5",
            ),
            pytest.param(
                lambda: HyDoc.objects(),
                {"text_limit": 0, "rrf_k": -1},
                ValueError,
                "hybrid_search() text_limit must be between 1 and 10000, got 0.",
                id="HT5-before-HK1",
            ),
        ],
    )
    async def test_the_first_fault_wins(
        self, make: Callable[[], QuerySet], args: dict[str, Any], error: type[Exception], text: str
    ) -> None:
        assert await _refused(error, make(), **args) == text

    async def test_a_refusal_after_a_dotted_field_leaves_the_link_heads(self) -> None:
        # A condition through a link names its head when it compiles, and the fields' heads are the runner's: a refusal
        # leaves the queryset's set as it was, empty.
        queryset = HyLinked.objects().filter(Q(**{"author.name": "Ann"}))
        assert await _refused(TypeError, queryset, vector_field="author.vec", text_field=3) == (
            "hybrid_search() takes text_field as a field name (str), not int."
        )
        assert await _refused(TypeError, queryset, text_field="author.bio", text_query=3) == (
            "hybrid_search() takes a str to match in 'author.bio', got int."
        )
        assert queryset._link_heads == set()


class TestHybridRows:
    """The dicts ``hybrid_search()`` returns, built from the rows of either line (``_hybrid_rows()``): the record's
    columns keyed by their Python names, ``_rrf_score`` (SurrealDB's ``rrf_score``), ``_knn_distance`` and
    ``_search_score`` (``None`` when the list that computes it did not find the record); never ``rrf_score`` nor the
    ORM's positions; a row that is not an object skipped; the rows given never changed."""

    def test_the_rows_of_the_3x_statement(self) -> None:
        rows = [
            {
                "id": _rid("d1"),
                "title": "Cats and dogs",
                "label_text": "one",
                "_knn_distance": 0.0,
                "_hybrid_vec_pos": 0,
                "rrf_score": 1 / 61,
                "_hybrid_pos": 0,
            },
            {
                "id": _rid("d3"),
                "title": "Rust programming",
                "label_text": "three",
                "_search_score": 0.48,
                "_hybrid_text_pos": 0,
                "rrf_score": 1 / 61,
                "_hybrid_pos": 3,
            },
            {
                "id": _rid("d2"),
                "title": "Cats at home",
                "label_text": "two",
                "_knn_distance": 0.006,
                "_search_score": 0.36,
                "_hybrid_vec_pos": 1,
                "_hybrid_text_pos": 2,
                "rrf_score": 1 / 62 + 1 / 63,
                "_hybrid_pos": 1,
            },
        ]
        before = copy.deepcopy(rows)
        assert HyAliased.objects()._hybrid_rows(rows) == [
            {
                "id": _rid("d1"),
                "title": "Cats and dogs",
                "label": "one",
                "_knn_distance": 0.0,
                "_search_score": None,
                "_rrf_score": 1 / 61,
            },
            {
                "id": _rid("d3"),
                "title": "Rust programming",
                "label": "three",
                "_knn_distance": None,
                "_search_score": 0.48,
                "_rrf_score": 1 / 61,
            },
            {
                "id": _rid("d2"),
                "title": "Cats at home",
                "label": "two",
                "_knn_distance": 0.006,
                "_search_score": 0.36,
                "_rrf_score": 1 / 62 + 1 / 63,
            },
        ]
        assert rows == before

    def test_the_rows_the_orm_fuses_below_3(self) -> None:
        lists = [
            [{"id": _rid("d1"), "label_text": "one", "_knn_distance": 0.0}],
            [{"id": _rid("d3"), "label_text": "three", "_search_score": 0.48}],
        ]
        rows = hybrid.fuse(lists, k=60, vector_limit=1)
        before = copy.deepcopy(rows)
        assert HyAliased.objects()._hybrid_rows(rows) == [
            {"id": _rid("d1"), "label": "one", "_knn_distance": 0.0, "_search_score": None, "_rrf_score": 1 / 61},
            {"id": _rid("d3"), "label": "three", "_knn_distance": None, "_search_score": 0.48, "_rrf_score": 1 / 61},
        ]
        assert rows == before

    def test_the_built_dicts_never_hold_the_server_s_key_nor_a_position(self) -> None:
        row = {
            "id": _rid("d1"),
            "rrf_score": 0.5,
            "_rrf_score": 400.0,
            "_hybrid_pos": -5,
            "_hybrid_vec_pos": -3,
            "_hybrid_text_pos": -4,
        }
        (built,) = HyDoc.objects()._hybrid_rows([row])
        assert built == {"id": _rid("d1"), "_rrf_score": 0.5, "_knn_distance": None, "_search_score": None}

    def test_a_row_that_is_not_an_object_is_skipped(self) -> None:
        rows: list[Any] = [None, "hy_doc:d1", 3, [{"id": _rid("d1")}], {"id": _rid("d2"), "rrf_score": 1 / 61}]
        assert HyDoc.objects()._hybrid_rows(rows) == [
            {"id": _rid("d2"), "_rrf_score": 1 / 61, "_knn_distance": None, "_search_score": None}
        ]
        assert HyDoc.objects()._hybrid_rows([]) == []


# --- the errors after the request: HS1, HD1 ---------------------------------------------------------------------------

#: HS1 for ``title``: SurrealDB refused the match, the field has no full-text index.
_HS1_TITLE = f"hybrid_search(): no full-text index on field 'title'. {_SI1_ADVICE}"

#: HD1 for ``embedding``: SurrealDB refused a vector of two numbers, the vector index's dimension is 3.
_HD1 = "hybrid_search(): the vector has 2 dimensions; the vector index on field 'embedding' expects 3."


class _RefusingClient:
    """A client on SurrealDB 3.3 whose every query fails with *error*: records what it was sent."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.sent: list[str] = []

    async def version(self) -> str:
        return "surrealdb-3.3.4"

    async def query(self, statement: str, variables: Any = None) -> Any:
        self.sent.append(statement)
        raise self.error


class _RefusingTx:
    """A transaction whose every read fails with *error*, noted among its failures as a transaction notes the error of a
    statement run in it."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
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


def _refusing_client(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> _RefusingClient:
    """Every client acquired is one :class:`_RefusingClient` failing with *error*, returned."""
    client = _RefusingClient(error)

    async def get_client(*args: Any, **kwargs: Any) -> Any:
        return client

    monkeypatch.setattr(SurrealDBConnectionManager, "get_client", get_client)
    return client


class TestHybridServerErrors:
    """SurrealDB's refusals after the request, read without a server: a match on a field without a full-text index
    (HS1, raised ``from None``) and a vector of another length than its vector index's dimension (HD1, raised ``from``
    the server's error) name ``hybrid_search()``, outside a transaction and inside one — where the ORM's error replaces
    the server's among the transaction's failures —; ``search()`` and ``similar_to()`` keep their own names."""

    async def test_hs1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _refusing_client(monkeypatch, InternalError("Internal", _NO_INDEX))
        with pytest.raises(SurrealDbError) as raised:
            await HyDoc.objects().hybrid_search(**_ARGS)
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == _HS1_TITLE
        assert raised.value.__cause__ is None
        assert raised.value.__suppress_context__
        assert len(client.sent) == 1

    async def test_hd1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _refusing_client(monkeypatch, InternalError("Internal", _DIMENSION_ERROR))
        with pytest.raises(SurrealDbError) as raised:
            await HyIndexed.objects().hybrid_search(**_ARGS)
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == _HD1
        assert raised.value.__cause__ is client.error
        assert len(client.sent) == 1

    @pytest.mark.parametrize(
        ("message", "text", "caused"),
        [
            pytest.param(_NO_INDEX, _HS1_TITLE, False, id="HS1"),
            pytest.param(_DIMENSION_ERROR, _HD1, True, id="HD1"),
        ],
    )
    async def test_in_a_transaction(self, monkeypatch: pytest.MonkeyPatch, message: str, text: str, caused: bool) -> None:
        client = _refusing_client(monkeypatch, InternalError("Internal", "never sent"))
        tx = _RefusingTx(InternalError("Internal", message))
        with pytest.raises(SurrealDbError) as raised:
            await HyIndexed.objects(tx=tx).hybrid_search(**_ARGS)
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == text
        assert raised.value.__cause__ is (tx.error if caused else None)
        assert tx.failures == [raised.value]
        assert client.sent == []

    async def test_search_and_similar_to_keep_their_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _refusing_client(monkeypatch, InternalError("Internal", _NO_INDEX))
        with pytest.raises(SurrealDbError) as searched:
            await HyDoc.objects().search(title="programming").exec()
        _refusing_client(monkeypatch, InternalError("Internal", _DIMENSION_ERROR))
        with pytest.raises(SurrealDbError) as near:
            await HyIndexed.objects().similar_to("embedding", [0.0, 1.0, 0.0], 4).exec()
        assert str(searched.value) == f"search(): no full-text index on field 'title'. {_SI1_ADVICE}"
        assert str(near.value) == (
            "similar_to(): the vector has 2 dimensions; the vector index on field 'embedding' expects 3."
        )


# === E2E: the connection "hy" on the database hy_db, over WebSocket and HTTP, and mem:// ============================

#: The engines every E2E class runs on: the line under test over WebSocket and HTTP, and the embedded engine.
_KINDS = ["ws", "http", "mem"]

#: The reset before each E2E test, in one query, of a database of these tests (``hy_db``, ``hy_db_two``).
_HY_RESET = "REMOVE DATABASE IF EXISTS {database}; DEFINE DATABASE {database};"


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
async def _hy(kind: str, name: str = "hy", database: str = "hy_db") -> AsyncIterator[Any]:
    """The connection *name* on *kind*, its *database* (a constant of these tests) reset in one query; removed, then
    unregistered, on exit."""
    if kind == "mem":
        SurrealDBConnectionManager.add_connection(name, url="mem://", namespace="ns", database=database)
    else:
        SurrealDBConnectionManager.add_connection(
            name, url=_url(kind), user="root", password="root", namespace="ns", database=database
        )
    client = await SurrealDBConnectionManager.get_client(name)
    await _apply_ddl(client, _HY_RESET.format(database=database))
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            cleanup = await SurrealDBConnectionManager.get_client(name)
            await cleanup.query(f"REMOVE DATABASE IF EXISTS {database};", {})
        await SurrealDBConnectionManager.remove_connection(name)


@contextlib.asynccontextmanager
async def _hy_models(kind: str) -> AsyncIterator[Any]:
    """:func:`_hy`, the models routed to the connection ``"hy"`` for the block (``using("hy")``)."""
    async with _hy(kind) as client, SurrealDBConnectionManager.using("hy"):
        yield client


async def _line(client: Any) -> tuple[int, int]:
    """``(major, minor)`` of the engine *client* talks to: ``"surrealdb-3.1.5"`` → ``(3, 1)``; ``mem://`` →
    ``(2, 0)`` (the SDK's version, whose core is 2.x)."""
    match = re.search(r"(\d+)\.(\d+)", await client.version())
    assert match is not None
    return int(match.group(1)), int(match.group(2))


async def _major(client: Any) -> int:
    """The major version of the engine *client* talks to: ``"surrealdb-3.3.4"`` → 3; ``mem://`` → 2."""
    return (await _line(client))[0]


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
    """The calls of one ``client.query(statement, …)``: the SDK's ``query()`` sends it through ``query_raw()``."""
    return [("query", statement), ("query_raw", statement)]


def _sent(statements: list[str]) -> list[tuple[str, object]]:
    """The calls of ``client.query(statement, …)`` for each of *statements*, in order."""
    return [call for statement in statements for call in _query(statement)]


#: The corpus: id, title, category and embedding of each record; ``n`` is its number, 1 to 8.
_CORPUS = [
    ("d1", "Cats and dogs", "pets", [1.0, 0.0, 0.0]),
    ("d2", "Cats at home", "pets", [0.9, 0.1, 0.0]),
    ("d3", "Rust programming", "tech", [0.0, 1.0, 0.0]),
    ("d4", "Python programming", "tech", [0.1, 0.9, 0.0]),
    ("d5", "Gardening", "home", [0.0, 0.0, 1.0]),
    ("d6", "Cats and code", "news", [0.5, 0.5, 0.0]),
    ("d7", "Rust and Python programming", "tech", [0.2, 0.8, 0.0]),
    ("d8", "Cooking", "home", [0.0, 0.2, 0.8]),
]


def _corpus_statements(table: str, extra: Callable[[int], str] | None = None) -> str:
    """The ``CREATE`` of the eight records in *table* (a constant of these tests), each with the columns *extra* gives
    its number (a literal ``, column = value`` text) too."""
    return " ".join(
        f"CREATE {table}:{key} SET title = '{title}', category = '{category}', n = {number}, embedding = {embedding}"
        f"{'' if extra is None else extra(number)};"
        for number, (key, title, category, embedding) in enumerate(_CORPUS, start=1)
    )


async def _setup(client: Any, model: type[BaseSurrealModel] = HyDoc) -> None:
    """*model*'s table and fields (``define_table()``), its indexes — the analyzer first — (``define_indexes()``), then
    the eight records, and the wait for its vector index's compaction (issue #344)."""
    await model.define_table()
    await model.define_indexes()
    await _apply_ddl(client, _corpus_statements(model.get_table_name()))
    await _compaction.wait_for_model_indexes(client, model)


async def _setup_linked(client: Any) -> None:
    """``HyLinked`` set up as ``HyDoc``, each record linked to an author: ``Ann`` for the odd numbers, ``Bob`` for the
    even ones; then the wait for its vector index's compaction (issue #344)."""
    await HyAuthor.define_table()
    await HyLinked.define_table()
    await HyLinked.define_indexes()
    await _apply_ddl(client, "CREATE hy_author:ann SET name = 'Ann'; CREATE hy_author:bob SET name = 'Bob';")
    await _apply_ddl(
        client, _corpus_statements("hy_linked", lambda number: f", author = hy_author:{'ann' if number % 2 else 'bob'}")
    )
    await _compaction.wait_for_model_indexes(client, HyLinked)


#: The query vectors of the cases.
_V = [1.0, 0.0, 0.0]
_Q2 = [0.0, 1.0, 0.0]

#: The cases on a server: the queryset's filter, its limit() and offset(), the call's arguments (with
#: ``vector_field="embedding"`` and ``text_field="title"``) and the answer — the records ``TestFuse``'s cases fuse to,
#: ``(id, fused score, distance present, score present)``, the score the Python expression ``search::rrf()`` computes.
_E2E_CASES: dict[str, tuple[dict[str, Any], int | None, int | None, dict[str, Any], list[tuple[str, float, bool, bool]]]] = {
    "C1": ({}, None, None, {"vector": _Q2, "text_query": "programming", "vector_limit": 4, "text_limit": 4}, _CASES["C1"][3]),
    "C2": ({}, None, None, {"vector": _V, "text_query": "programming", "vector_limit": 3, "text_limit": 3}, _CASES["C2"][3]),
    "C3": ({}, 3, None, {"vector": _V, "text_query": "programming", "vector_limit": 3, "text_limit": 3}, _CASES["C2"][3][:3]),
    "C4": ({}, 2, 1, {"vector": _V, "text_query": "programming", "vector_limit": 3, "text_limit": 3}, _CASES["C2"][3][1:3]),
    "C5": (
        {"category": "tech"},
        None,
        None,
        {"vector": _Q2, "text_query": "programming", "vector_limit": 3, "text_limit": 3},
        _CASES["C5"][3],
    ),
    "C6": (
        {},
        None,
        None,
        {"vector": _V, "text_query": "cats", "vector_limit": 2, "text_limit": 2, "dist": "cosine"},
        _CASES["C6"][3],
    ),
    "C7": ({}, None, None, {"vector": _Q2, "text_query": "cats", "vector_limit": 2, "text_limit": 3}, _CASES["C7"][3]),
}

#: C1's vector list alone, fused: the four nearest to ``[0, 1, 0]``, each found by the vector search only — the answer
#: to C1's call with an empty ``text_query``, which matches nothing on every line.
_C1_VECTOR_LIST_ALONE = [
    ("d3", 1 / 61, True, False),
    ("d4", 1 / 62, True, False),
    ("d7", 1 / 63, True, False),
    ("d6", 1 / 64, True, False),
]


def _case_queryset(case: str, model: type[BaseSurrealModel] = HyDoc, *, paged: bool = True) -> QuerySet:
    """*model*'s queryset of *case*: its filter, and with *paged* its ``limit()`` and ``offset()``."""
    filters, limit, offset, _, _ = _E2E_CASES[case]
    queryset = model.objects().filter(**filters) if filters else model.objects()
    if paged and limit is not None:
        queryset = queryset.limit(limit)
    if paged and offset is not None:
        queryset = queryset.offset(offset)
    return queryset


async def _run_case(case: str, model: type[BaseSurrealModel] = HyDoc) -> list[dict[str, Any]]:
    """``hybrid_search()`` of *case* on *model*."""
    args = _E2E_CASES[case][3]
    rows: list[dict[str, Any]] = await _case_queryset(case, model).hybrid_search(
        vector_field="embedding", text_field="title", **args
    )
    return rows


async def _references(case: str, model: type[BaseSurrealModel] = HyDoc) -> tuple[dict[str, Any], dict[str, Any]]:
    """The distance ``similar_to()`` and the score ``search()`` with ``SearchScore(0)`` give each record of *case*'s
    queryset on the same engine, by id."""
    return await _references_of(lambda: _case_queryset(case, model, paged=False), _E2E_CASES[case][3])


async def _references_of(make: Callable[[], QuerySet], args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The distance ``similar_to()`` and the score ``search()`` of ``title`` with ``SearchScore(0)`` give each record of
    the queryset *make* builds, for the vector, the text and the distance of *args*, on the same engine, by id."""
    near = await make().similar_to("embedding", args["vector"], 8, dist=args.get("dist")).exec()
    found = await make().search(title=args["text_query"]).annotate(score=SearchScore(0)).exec()
    return {doc.id: doc._knn_distance for doc in near}, {doc.id: doc.score for doc in found}


def _ranking(rows: list[dict[str, Any]]) -> list[tuple[str, float]]:
    """Each fused record's id and score, in order."""
    return [(row["id"].id, row["_rrf_score"]) for row in rows]


def _check_answer(
    case: str,
    rows: Any,
    expected: list[tuple[str, float, bool, bool]],
    distances: dict[str, Any],
    scores: dict[str, Any],
    table: str = "hy_doc",
) -> None:
    """*rows* is *expected*: dicts in its order, each ``_rrf_score`` equal to its score, each ``_knn_distance`` and
    ``_search_score`` the one ``similar_to()`` and ``search()`` gave the record (*distances*, *scores*), else ``None``."""
    assert type(rows) is list, case
    assert all(type(row) is dict for row in rows), case
    assert _ranking(rows) == [(key, score) for key, score, _, _ in expected], case
    for row, (key, _, has_distance, has_score) in zip(rows, expected, strict=True):
        assert row["id"] == RecordID(table, key), (case, key)
        assert type(row["_rrf_score"]) is float, (case, key)
        if has_distance:
            assert row["_knn_distance"] == pytest.approx(distances[key], rel=1e-9), (case, key)
        else:
            assert row["_knn_distance"] is None, (case, key)
        if has_score:
            assert row["_search_score"] == pytest.approx(scores[key], rel=1e-9), (case, key)
        else:
            assert row["_search_score"] is None, (case, key)


class TestHybridSearchE2E:
    """``hybrid_search()`` on the eight-record corpus, on every engine (the line under test over WebSocket and HTTP,
    and the embedded engine): each case fuses to the same records, order and scores — the expression SurrealDB 3.x's
    ``search::rrf()`` computes, the ORM's fusion below 3 equal to it —, equal scores in the vector list's order then
    the full-text list's, five times in a row the same; an empty text fuses the vector list alone; each record a dict
    of its columns and the three values, its distance and score those ``similar_to()`` and ``search()`` give it; the
    queryset unchanged, its link heads included."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_cases_of_the_corpus(self, kind: str) -> None:
        async with _hy_models(kind) as client:
            await _setup(client)
            for case, (_, _, _, _, expected) in _E2E_CASES.items():
                rows = await _run_case(case)
                distances, scores = await _references(case)
                _check_answer(case, rows, expected, distances, scores)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_empty_text_fuses_the_vector_list_alone(self, kind: str) -> None:
        """An empty ``text_query`` matches nothing on every line: the answer is the vector list alone, fused — the
        records ``similar_to()`` finds, in its order, each with its distance and ``_search_score`` ``None`` —, with the
        HNSW index and with an exact scan (``dist=``)."""
        answers: dict[str | None, tuple[list[dict[str, Any]], list[Any]]] = {}
        async with _hy_models(kind) as client:
            await _setup(client)
            for dist in (None, "cosine"):
                rows = await HyDoc.objects().hybrid_search(**{**_ARGS, "text_query": "", "dist": dist})
                near = await HyDoc.objects().similar_to("embedding", _ARGS["vector"], 4, dist=dist).exec()
                answers[dist] = (rows, near)
        for dist, (rows, near) in answers.items():
            case = f"C1, an empty text, dist={dist}"
            _check_answer(case, rows, _C1_VECTOR_LIST_ALONE, {doc.id: doc._knn_distance for doc in near}, {})
            assert [row["id"].id for row in rows] == [doc.id for doc in near], case
            assert all(row["_search_score"] is None for row in rows), case

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_equal_scores_keep_one_order_every_time(self, kind: str) -> None:
        async with _hy_models(kind) as client:
            await _setup(client)
            for case in ("C2", "C3", "C4"):
                expected = [(key, score) for key, score, _, _ in _E2E_CASES[case][4]]
                for run in range(5):
                    assert _ranking(await _run_case(case)) == expected, (case, run)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_each_record_is_a_dict_of_its_columns_and_the_three_values(self, kind: str) -> None:
        async with _hy_models(kind) as client:
            await _setup(client)
            rows = await HyDoc.objects().hybrid_search(**_ARGS)
        assert type(rows) is list and len(rows) == 4
        for row in rows:
            assert type(row) is dict
            assert not isinstance(row, BaseSurrealModel)
            assert set(row) == {"id", "title", "category", "n", "embedding", "_rrf_score", "_knn_distance", "_search_score"}
            assert type(row["id"]) is RecordID
            assert type(row["_rrf_score"]) is float
        by_id = {row["id"].id: row for row in rows}
        assert (by_id["d3"]["title"], by_id["d3"]["category"], by_id["d3"]["n"]) == ("Rust programming", "tech", 3)
        assert by_id["d3"]["embedding"] == [0.0, 1.0, 0.0]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_queryset_is_unchanged_its_link_heads_included(self, kind: str) -> None:
        async with _hy_models(kind) as client:
            await _setup_linked(client)
            # A condition through a link, never compiled before: the queryset has recorded no link head.
            queryset = HyLinked.objects().filter(Q(**{"author.name": "Ann"}))
            before = _snapshot(queryset)
            rows = await queryset.hybrid_search(**{**_ARGS, "vector_limit": 2})
            assert _snapshot(queryset) == before
            assert queryset._link_heads == set()
            again = await queryset.hybrid_search(**{**_ARGS, "vector_limit": 2})
        assert _ranking(rows) == [("d3", 1 / 61 + 1 / 61), ("d7", 1 / 62 + 1 / 62)]
        assert [row["author"] for row in rows] == [RecordID("hy_author", "ann"), RecordID("hy_author", "ann")]
        assert again == rows


class TestHybridDispatchE2E:
    """What ``hybrid_search()`` sends on each line, one request per call: on SurrealDB 3.x the ``SELECT`` that fuses the
    two lists inline in ``search::rrf()``, each list with its ``OMIT``, ordered by fused score then position; below 3
    the two lists in ``LET``s, without ``OMIT``, which the ORM fuses — the vector list's other conditions selected
    first, as ``similar_to()`` does. The line is read once per client."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_statement_of_each_line(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _hy_models(kind) as client:
            await _setup(client)
            major = await _major(client)
            calls = _spy(monkeypatch, client)
            first = await HyDoc.objects().hybrid_search(**_ARGS)
            second = await HyDoc.objects().hybrid_search(**_ARGS)
            sent = _calls(calls)
            monkeypatch.undo()
        statements = [statement for method, statement in sent if method == "query"]
        assert [call for call in sent if call[0] != "version"] == _sent(statements)
        assert len([call for call in sent if call[0] == "version"]) <= 1
        assert first == second
        assert _ranking(first) == [(key, score) for key, score, _, _ in _CASES["C1"][3]]
        markers = [
            "search::rrf([(SELECT ",
            "AS _knn_distance OMIT _search_score FROM",
            "AS _search_score OMIT _knn_distance, _hybrid_vec_pos FROM",
            "ORDER BY rrf_score DESC, _hybrid_pos",
        ]
        for statement in statements:
            assert "ORDER BY _knn_distance, id LIMIT 4)" in statement
            if major >= 3:
                assert statement == _G1
                assert statement.startswith("SELECT *, IF _hybrid_vec_pos")
                assert all(marker in statement for marker in markers)
                assert "LET" not in statement
            else:
                assert statement == _G4
                assert not statement.startswith("SELECT *, IF _hybrid_vec_pos")
                assert not any(marker in statement for marker in markers)
                assert "OMIT" not in statement
                assert "([$_sq" in statement

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_condition_next_to_the_vector_search(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _hy_models(kind) as client:
            await _setup(client)
            major = await _major(client)
            calls = _spy(monkeypatch, client)
            rows = await _run_case("C5")
            sent = _calls(calls)
            monkeypatch.undo()
        (statement,) = [statement for method, statement in sent if method == "query"]
        assert isinstance(statement, str)
        assert _ranking(rows) == [(key, score) for key, score, _, _ in _CASES["C5"][3]]
        assert "ORDER BY _knn_distance, id LIMIT 3)" in statement
        if major >= 3:
            assert "WHERE category = $_f0 AND embedding <|3,100|> $_f1 ORDER BY _knn_distance, id LIMIT 3)" in statement
            assert "WHERE category = $_f2 AND title @0@ $_f3 ORDER BY _search_score DESC, id LIMIT 3)" in statement
            assert "LET" not in statement
        else:
            # v0.36.0's pre-filter in the vector list only; the full-text list keeps its condition inline.
            assert statement.startswith(
                "{ LET $_sq0 = (SELECT VALUE id FROM hy_doc WHERE category = $_f0); LET $_sq1 = (SELECT *, "
                "vector::distance::knn() AS _knn_distance FROM hy_doc WHERE id IN $_sq0 AND embedding <|3,100|> $_f1 "
                "ORDER BY _knn_distance, id LIMIT 3); "
            )
            assert "WHERE category = $_f2 AND title @0@ $_f3 ORDER BY _search_score DESC, id LIMIT 3);" in statement
            assert statement.endswith("([$_sq1, $_sq2]) };")


# === E2E: composition, stored reserved columns, the errors after the request, transactions ===========================


class HyNov(BaseSurrealModel):
    """``HyDoc``'s fields with a full-text index on ``title`` and no vector index on ``embedding``."""

    model_config = SurrealConfigDict(table_name="hy_nov", indexes=[Index("hy_nov_title", "title", search_analyzer=HY_PLAIN)])

    id: str | None = None
    title: str
    category: str = ""
    n: int = 0
    embedding: VectorField[3]


class HySl(BaseSurrealModel):
    """``HyDoc``'s fields and indexes on a SCHEMALESS table (the model declares no ``schema_mode``), whose records also
    store columns the model does not declare, named as the seven names ``hybrid_search()`` uses for its own values."""

    model_config = SurrealConfigDict(
        table_name="hy_sl",
        indexes=[
            Index("hy_sl_embedding", "embedding", hnsw=True, dimension=3, dist="cosine"),
            Index("hy_sl_title", "title", search_analyzer=HY_PLAIN),
        ],
    )

    id: str | None = None
    title: str
    category: str = ""
    n: int = 0
    embedding: VectorField[3]


class HyBare(BaseSurrealModel):
    """A model that declares no dimension for ``embedding`` — a plain list, no vector ``Index`` —: its table's indexes
    are defined by hand, so only SurrealDB knows the vector index's dimension."""

    model_config = SurrealConfigDict(table_name="hy_bare")

    id: str | None = None
    title: str
    category: str = ""
    n: int = 0
    embedding: list[float] | None = None


class HyMissing(BaseSurrealModel):
    """``HyDoc``'s fields on a table that is never defined."""

    model_config = SurrealConfigDict(table_name="hy_missing")

    id: str | None = None
    title: str
    category: str = ""
    n: int = 0
    embedding: VectorField[3]


class HyRouted(BaseSurrealModel):
    """``HyDoc``'s fields and indexes on the connection ``"hy"`` (``SurrealConfigDict(connection=…)``)."""

    model_config = SurrealConfigDict(
        table_name="hy_routed",
        connection="hy",
        indexes=[
            Index("hy_routed_embedding", "embedding", hnsw=True, dimension=3, dist="cosine"),
            Index("hy_routed_title", "title", search_analyzer=HY_PLAIN),
        ],
    )

    id: str | None = None
    title: str
    category: str = ""
    n: int = 0
    embedding: VectorField[3]


#: The columns of every ``HySl`` record named as the seven names ``hybrid_search()`` uses for its own values, with
#: their stored values: a literal statement, the model declares none of them.
_SL_UPDATE = (
    "UPDATE hy_sl SET rrf_score = 500.0, _rrf_score = 400.0, _knn_distance = 900.0, _search_score = 700.0, "
    "_hybrid_pos = -5, _hybrid_vec_pos = -3, _hybrid_text_pos = -4;"
)

#: The values ``_SL_UPDATE`` stores, by column.
_SL_STORED = {
    "rrf_score": 500.0,
    "_rrf_score": 400.0,
    "_knn_distance": 900.0,
    "_search_score": 700.0,
    "_hybrid_pos": -5,
    "_hybrid_vec_pos": -3,
    "_hybrid_text_pos": -4,
}

#: The stored distance and score: no value ``hybrid_search()`` returns is either.
_SL_DISTANCE_AND_SCORE = (900.0, 700.0)

#: The keys of a dict ``hybrid_search()`` returns for ``HyDoc``'s records, without ``select()``.
_DOC_KEYS = {"id", "title", "category", "n", "embedding", "_rrf_score", "_knn_distance", "_search_score"}

#: HS1 for ``category``, which has no full-text index.
_HS1_CATEGORY = f"hybrid_search(): no full-text index on field 'category'. {_SI1_ADVICE}"

#: C1's call on the records numbered above 3 (d4 to d8): the vector list d4, d7, d6, d8, the full-text list d4, d7.
_ABOVE_3 = [
    ("d4", 1 / 61 + 1 / 61, True, True),
    ("d7", 1 / 62 + 1 / 62, True, True),
    ("d6", 1 / 63, True, False),
    ("d8", 1 / 64, True, False),
]

#: C8 — ``HyNov``, C2's call — on SurrealDB 3.3, 2.x and the embedded engine: the vector search finds nothing without
#: its index, so the full-text ranking alone is fused.
_C8_NO_VECTOR_LIST = [("d3", 1 / 61, False, True), ("d4", 1 / 62, False, True), ("d7", 1 / 63, False, True)]

#: C8 on SurrealDB 3.1 and 3.2: the vector list holds the first three records by id, with no distance.
_C8_NO_DISTANCE = [
    ("d3", 1 / 63 + 1 / 61, False, True),
    ("d1", 1 / 61, False, False),
    ("d2", 1 / 62, False, False),
    ("d4", 1 / 62, False, True),
    ("d7", 1 / 63, False, True),
]

#: C9 — ``HyDoc``, C1's call with ``text_field="category"`` (no full-text index) — on SurrealDB 3.1 and 3.2, which match
#: nothing: the vector list alone is fused.
_C9_NO_MATCH = _C1_VECTOR_LIST_ALONE

#: The existing refusal of a read inside a buffered transaction.
_BUFFERED_READ = (
    "Reads inside a transaction require a WebSocket connection to SurrealDB 3.x (native interactive transactions); "
    "the current transaction is buffered (HTTP or SurrealDB 2.6.x)."
)

#: The commit's error once a statement of the transaction failed.
_COMMIT_FAILED = "The transaction was cancelled: a statement in it failed; nothing was committed (SurrealDbError)"


@pytest.fixture
def empty_cache() -> Iterator[None]:
    """An empty query cache around the test."""
    QueryCache.clear()
    yield
    QueryCache.clear()


def _expected(case: str) -> list[tuple[str, float]]:
    """*case*'s records and fused scores, in order."""
    return [(key, score) for key, score, _, _ in _E2E_CASES[case][4]]


def _is_31_or_32(line: tuple[int, int]) -> bool:
    """Whether *line* is SurrealDB 3.1 or 3.2, which answer a search on a field without its index with no error."""
    return (3, 0) <= line < (3, 3)


def _bare_ddl(major: int) -> str:
    """``HyBare``'s indexes, by hand, in this order: the analyzer, the full-text index on ``title`` (``FULLTEXT`` on
    SurrealDB 3.x, ``SEARCH`` below), then the vector index on ``embedding``, of dimension 3."""
    full_text = "FULLTEXT" if major >= 3 else "SEARCH"
    return (
        "DEFINE ANALYZER hy_plain TOKENIZERS blank FILTERS lowercase; "
        f"DEFINE INDEX hy_bare_title ON hy_bare FIELDS title {full_text} ANALYZER hy_plain; "
        "DEFINE INDEX hy_bare_embedding ON hy_bare FIELDS embedding HNSW DIMENSION 3 DIST COSINE TYPE F32;"
    )


async def _setup_sl(client: Any) -> None:
    """``HySl`` set up as ``HyDoc`` — :func:`_setup` waits for its vector index's compaction (issue #344) —, then every
    record given the stored columns of ``_SL_UPDATE``, checked: an ``UPDATE`` that leaves the vectors alone starts no
    compaction (measured on 3.3.4)."""
    await _setup(client, HySl)
    await _apply_ddl(client, _SL_UPDATE)
    stored = await client.query("SELECT * FROM hy_sl;", {})
    assert len(stored) == 8
    for row in stored:
        assert {column: row.get(column) for column in _SL_STORED} == _SL_STORED, row


def _check_as(case: str, rows: list[dict[str, Any]], reference: list[dict[str, Any]], table: str) -> None:
    """*rows* (of *table*) are *reference* (``HyDoc``'s answer on the same engine): the same records in the same order,
    each fused score equal, each distance and score ``None`` where *reference*'s is and equal to it elsewhere, the same
    keys; no value is a stored distance or score."""
    assert _ranking(rows) == _ranking(reference), case
    for row, expected in zip(rows, reference, strict=True):
        key = expected["id"].id
        assert row["id"] == RecordID(table, key), (case, key)
        assert set(row) == set(expected) == _DOC_KEYS, (case, key)
        for value in ("_knn_distance", "_search_score"):
            if expected[value] is None:
                assert row[value] is None, (case, key, value)
            else:
                assert row[value] == pytest.approx(expected[value], rel=1e-9), (case, key, value)
        assert not [value for value in row.values() if value in _SL_DISTANCE_AND_SCORE], (case, key)


async def _native_transactions(kind: str, client: Any) -> bool:
    """Whether ``transaction()`` on *kind* is interactive: WebSocket on SurrealDB 3.x."""
    return kind == "ws" and await _major(client) >= 3


class _Rollback(Exception):
    """Raised inside a transaction's block so that nothing is committed."""


class TestHybridCompositionE2E:
    """``hybrid_search()`` with the rest of the queryset, on every engine: ``select()`` reads the fields named in both
    lists — an item naming a reserved column left out, so the answer is the same —, each dict keyed by the fields'
    Python names; ``fetch()`` resolves the link in both lists' records; a ``Subquery`` filter, a condition and a
    ``Var()`` bound by ``variables()`` select the records of both lists alike; a model's named connection and
    ``using()`` route the read; ``cache()`` answers an identical call without a request until a write to the table."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_select(self, kind: str) -> None:
        async with _hy_models(kind) as client:
            await _setup(client)
            plain = await HyDoc.objects().select("title").hybrid_search(**_ARGS)
            named = await HyDoc.objects().select("title", "_knn_distance").hybrid_search(**_ARGS)
            distances, scores = await _references("C1")
        _check_answer("C1", plain, _E2E_CASES["C1"][4], distances, scores)
        for row in plain:
            assert set(row) == {"id", "title", "_rrf_score", "_knn_distance", "_search_score"}
            assert row["title"] == _TITLES[row["id"].id]
        # The item naming the distance is left out of both lists: the vector list is still ordered by distance.
        assert named == plain

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_aliased_field_is_keyed_by_its_python_name(self, kind: str) -> None:
        async with _hy_models(kind) as client:
            await HyAliased.define_table()
            await HyAliased.define_indexes()
            await _apply_ddl(client, _corpus_statements("hy_aliased", lambda number: f", label_text = 'Label {number}'"))
            await _compaction.wait_for_model_indexes(client, HyAliased)
            rows = await HyAliased.objects().hybrid_search(**_ARGS)
            selected = await HyAliased.objects().select("title", "label").hybrid_search(**_ARGS)
        assert _ranking(rows) == _expected("C1")
        assert _ranking(selected) == _expected("C1")
        for row in rows:
            assert set(row) == _DOC_KEYS | {"label"}
            assert row["label"] == f"Label {row['n']}"
        for row in selected:
            assert set(row) == {"id", "title", "label", "_rrf_score", "_knn_distance", "_search_score"}
            assert row["label"] == f"Label {row['id'].id[1:]}"

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_fetch(self, kind: str) -> None:
        async with _hy_models(kind) as client:
            await _setup_linked(client)
            both = await HyLinked.objects().fetch("author").hybrid_search(**_ARGS)
            alone = await HyLinked.objects().fetch("author").hybrid_search(**{**_ARGS, **_E2E_CASES["C7"][3]})
        assert _ranking(both) == _expected("C1")
        # C7: records found by the vector list alone (d3, d4) and by the full-text list alone (d1, d2, d6).
        assert _ranking(alone) == _expected("C7")
        for row in [*both, *alone]:
            key = "ann" if row["n"] % 2 else "bob"
            assert type(row["author"]) is dict, row["id"]
            assert row["author"] == {"id": RecordID("hy_author", key), "name": key.capitalize()}, row["id"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_subquery_filter_a_condition_and_a_variable(self, kind: str) -> None:
        async with _hy_models(kind) as client:
            await _setup(client)
            querysets = {
                "Subquery": HyDoc.objects().filter(id__in=Subquery(HyDoc.objects().filter(n__gt=3))),
                "filter": HyDoc.objects().filter(n__gt=3),
                "Var": HyDoc.objects().filter(n__gt=Var("lo")).variables(lo=3),
            }
            answers = {name: await queryset.hybrid_search(**_ARGS) for name, queryset in querysets.items()}
            distances, scores = await _references_of(lambda: HyDoc.objects().filter(n__gt=3), _ARGS)
        for name, rows in answers.items():
            _check_answer(name, rows, _ABOVE_3, distances, scores)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_named_connection_and_using(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _hy(kind) as client, _hy(kind, "hy_two", "hy_db_two") as other:
            await HyRouted.define_table()
            await HyRouted.define_indexes()
            await _apply_ddl(client, _corpus_statements("hy_routed"))
            await _compaction.wait_for_model_indexes(client, HyRouted)
            routed = await HyRouted.objects().hybrid_search(**_ARGS)
            async with SurrealDBConnectionManager.using("hy_two"):
                await _setup(other)
                calls = _spy(monkeypatch, client)
                rows = await HyDoc.objects().hybrid_search(**_ARGS)
                sent = _calls(calls)
                monkeypatch.undo()
        assert _ranking(routed) == _expected("C1")
        assert {row["id"].table_name for row in routed} == {"hy_routed"}
        assert _ranking(rows) == _expected("C1")
        assert {row["id"].table_name for row in rows} == {"hy_doc"}
        assert sent == [], "the read inside using() never reaches the connection 'hy'"

    @pytest.mark.usefixtures("empty_cache")
    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_cache(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _hy_models(kind) as client:
            await _setup(client)

            async def cached() -> list[dict[str, Any]]:
                rows: list[dict[str, Any]] = await HyDoc.objects().cache(ttl=30).hybrid_search(**_ARGS)
                return rows

            calls = _spy(monkeypatch, client)
            first = await cached()
            sent_first = _calls(calls)
            embedding = list(first[0]["embedding"])
            first[0]["embedding"].append(9.0)
            del calls[:]
            second = await cached()
            sent_second = _calls(calls)
            await HyDoc(id="d9", title="Knitting", embedding=[0.0, 0.0, 1.0]).save()
            del calls[:]
            third = await cached()
            sent_third = _calls(calls)
            monkeypatch.undo()
        statements = [statement for method, statement in sent_first if method == "query"]
        assert len(statements) == 1
        assert sent_second == [], "the cache answers the identical call"
        assert [statement for method, statement in sent_third if method == "query"] == statements
        assert _ranking(first) == _ranking(second) == _ranking(third) == _expected("C1")
        # A dict handed out never changes the cache.
        assert second[0]["embedding"] == embedding
        first[0]["embedding"] = embedding
        assert second == first


class TestHybridReservedColumnsE2E:
    """Records that store columns named as the names ``hybrid_search()`` uses for its own values — a SCHEMALESS table's
    undeclared columns —, on every engine: the answer is the one of the same records without them — the same records,
    order and fused scores, each distance and score the lists computed, never a stored one —, five times in a row the
    same, and no dict holds SurrealDB's ``rrf_score`` nor the ORM's positions."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_c1_and_c2_as_without_the_stored_columns(self, kind: str) -> None:
        async with _hy_models(kind) as client:
            await _setup(client)
            await _setup_sl(client)
            answers = {case: (await _run_case(case), await _run_case(case, HySl)) for case in ("C1", "C2")}
        for case, (clean, stored) in answers.items():
            assert _ranking(clean) == _expected(case), case
            _check_as(case, stored, clean, "hy_sl")

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_c2_five_times_in_a_row(self, kind: str) -> None:
        async with _hy_models(kind) as client:
            await _setup(client)
            await _setup_sl(client)
            clean = await _run_case("C2")
            runs = [await _run_case("C2", HySl) for _ in range(5)]
        assert _ranking(clean) == _expected("C2")
        for run, rows in enumerate(runs):
            assert _ranking(rows) == _ranking(clean), f"run {run}"
            stored = [(row["id"].id, value) for row in rows for value in row.values() if value in _SL_DISTANCE_AND_SCORE]
            assert stored == [], f"run {run}"


class TestHybridErrorsE2E:
    """What the server decides, after the request, on each line: a full-text field without its index (SurrealDB 3.3,
    2.x and the embedded engine refuse the match, ``SurrealDbError`` naming the field; 3.1 and 3.2 match nothing); a
    vector of another length than its vector index's dimension on a model that does not declare it (every engine,
    ``SurrealDbError`` naming the field and both lengths, from the server's error); a table that does not exist (no
    records); a vector field without its vector index (3.3, 2.x and the embedded engine find nothing; 3.1 and 3.2 list
    records with no distance)."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_text_field_without_a_full_text_index(self, kind: str) -> None:
        async with _hy_models(kind) as client:
            await _setup(client)
            line = await _line(client)
            if _is_31_or_32(line):
                rows = await HyDoc.objects().hybrid_search(**{**_ARGS, "text_field": "category"})
                distances, scores = await _references("C1")
                _check_answer("C9: SurrealDB 3.1 and 3.2 match nothing", rows, _C9_NO_MATCH, distances, scores)
                return
            with pytest.raises(SurrealDbError) as raised:
                await HyDoc.objects().hybrid_search(**{**_ARGS, "text_field": "category"})
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == _HS1_CATEGORY
        assert raised.value.__cause__ is None
        assert raised.value.__suppress_context__

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_vector_of_another_length_than_its_index(self, kind: str) -> None:
        async with _hy_models(kind) as client:
            bare = _bare_ddl(await _major(client))
            await _apply_ddl(client, bare)
            await _apply_ddl(client, _corpus_statements("hy_bare"))
            await _compaction.wait_for_defined_indexes(client, bare)
            with pytest.raises(SurrealDbError) as raised:
                await HyBare.objects().hybrid_search(**{**_ARGS, "vector": [0.0, 1.0]})
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == _HD1
        assert isinstance(raised.value.__cause__, ServerError)
        assert _DIMENSION_ERROR in str(raised.value.__cause__)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_table_that_does_not_exist(self, kind: str) -> None:
        async with _hy_models(kind):
            plain = await HyMissing.objects().hybrid_search(**_ARGS)
            inner = HyMissing.objects().filter(n__gt=0)
            filtered = await HyMissing.objects().filter(id__in=Subquery(inner)).hybrid_search(**_ARGS)
        assert plain == []
        assert filtered == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_vector_field_without_a_vector_index(self, kind: str) -> None:
        async with _hy_models(kind) as client:
            await _setup(client, HyNov)
            line = await _line(client)
            rows = await _run_case("C2", HyNov)
            _, scores = await _references("C2", HyNov)
        expected = _C8_NO_DISTANCE if _is_31_or_32(line) else _C8_NO_VECTOR_LIST
        _check_answer("C8", rows, expected, {}, scores, table="hy_nov")


class TestHybridTransactionsE2E:
    """``hybrid_search()`` in a transaction, on every engine: in an interactive one (SurrealDB 3.x over WebSocket) both
    lists see the transaction's own write, gone once it rolls back, and a field without a full-text index (SurrealDB
    3.3) fails the commit, caused by the ORM's error; in a buffered one (HTTP, SurrealDB 2.x, the embedded engine) the
    read is refused before any statement is sent, as every read is there."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_search_sees_the_transaction_s_write(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        args = {"vector_field": "embedding", "text_field": "title", **_E2E_CASES["C2"][3]}
        async with _hy_models(kind) as client:
            await _setup(client)
            interactive = await _native_transactions(kind, client)
            inside: list[dict[str, Any]] = []
            refused: pytest.ExceptionInfo[SurrealDbError] | None = None
            sent: list[tuple[str, object]] = []
            with pytest.raises(_Rollback):
                async with SurrealDBConnectionManager.transaction("hy") as tx:
                    await HyDoc(id="d9", title="Go programming", embedding=[0.95, 0.05, 0.0]).save(tx=tx)
                    if interactive:
                        inside = await HyDoc.objects(tx=tx).hybrid_search(**args)
                    else:
                        calls = _spy(monkeypatch, client)
                        with pytest.raises(SurrealDbError) as refused:
                            await HyDoc.objects(tx=tx).hybrid_search(**args)
                        sent = _calls(calls)
                        monkeypatch.undo()
                    raise _Rollback
            after = await _run_case("C2")
        assert _ranking(after) == _expected("C2"), "rolled back: nothing of the transaction remains"
        if interactive:
            # d9 is second nearest (after d1), and "Go programming" scores as "Rust programming" and "Python
            # programming": equal scores by id, it is third of the full-text list (d3, d4, d9), which keeps three.
            assert _ranking(inside) == [
                ("d9", 1 / 62 + 1 / 63),
                ("d1", 1 / 61),
                ("d3", 1 / 61),
                ("d4", 1 / 62),
                ("d2", 1 / 63),
            ]
            written = inside[0]
            assert written["_knn_distance"] is not None
            assert written["_search_score"] is not None
            assert written["_search_score"] == inside[2]["_search_score"]
        else:
            assert refused is not None
            assert type(refused.value) is SurrealDbError
            assert str(refused.value) == _BUFFERED_READ
            assert [method for method, _ in sent if method != "version"] == [], "nothing sent"

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_text_field_without_a_full_text_index_fails_the_commit(self, kind: str) -> None:
        """The read raises the ORM's error; the block that catches it and ends normally gets the commit's error,
        caused by that very error, and nothing is committed."""
        async with _hy_models(kind) as client:
            if not await _native_transactions(kind, client):
                pytest.skip("requires SurrealDB 3.x native transactions")
            if _is_31_or_32(await _line(client)):
                pytest.skip("SurrealDB 3.1 and 3.2 match nothing on a field without a full-text index")
            await _setup(client)
            read: pytest.ExceptionInfo[SurrealDbError] | None = None
            with pytest.raises(SurrealDbError) as commit:
                async with SurrealDBConnectionManager.transaction("hy") as tx:
                    with pytest.raises(SurrealDbError) as read:
                        await HyDoc.objects(tx=tx).hybrid_search(**{**_ARGS, "text_field": "category"})
                    await HyDoc(id="d9", title="Go programming", embedding=[0.95, 0.05, 0.0]).save(tx=tx)
            stored = await client.query("SELECT VALUE id FROM hy_doc;", {})
        assert read is not None
        assert type(read.value) is SurrealDbError
        assert str(read.value) == _HS1_CATEGORY
        assert str(commit.value) == _COMMIT_FAILED
        assert commit.value.__cause__ is read.value
        assert sorted(record.id for record in stored) == [f"d{number}" for number in range(1, 9)]


# === E2E: what each line answers to search::rrf(), pinned with literal statements ===================================

#: The README row the first pin backs.
_ROW_FUSION = "README row '`hybrid_search()`: the fusion'"

#: The README sentence the second pin backs (section 38, "Search").
_S38_FORMULA = "README section 38, 'Each record gets 1 / (`rrf_k` + rank) from each list it is in, its rank counted from 1'"

#: Two lists of one record each, fused: both records score 1/61.
_PIN_RRF = "RETURN search::rrf([[{id: 1}], [{id: 2}]], 2, 60);"

#: Two lists sharing the record 2 (rank 2 in the first, rank 1 in the second), fused.
_PIN_RRF_FORMULA = "RETURN search::rrf([[{id: 1}, {id: 2}], [{id: 2}, {id: 3}]], 3, 60);"

#: ``_PIN_RRF_FORMULA``'s lists, as the ORM fuses them below 3.
_FORMULA_LISTS = [[{"id": 1}, {"id": 2}], [{"id": 2}, {"id": 3}]]

#: ``_PIN_RRF_FORMULA``'s answer: each record, its fused score Σ 1/(60 + rank) summed in list order, best first.
_FORMULA_ANSWER = [(2, 1 / 62 + 1 / 61), (1, 1 / 61), (3, 1 / 62)]

#: What SurrealDB 2.x and the embedded engine answer to a statement calling ``search::rrf()``.
_NO_RRF = "Invalid function/constant path"


class TestHybridLinePinsE2E:
    """What each engine answers to ``search::rrf()``, pinned with literal statements on every engine (the line under
    test over WebSocket and HTTP, and the embedded engine), each test naming the README row or sentence it backs: the
    function exists on SurrealDB 3.x only, which is why the ORM fuses the two lists itself below 3, and its score is
    the sum of 1/(k + rank) over the lists, bit for bit as the ORM computes it. Equal scores are never compared in the
    server's order, which is not fixed."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_search_rrf_exists_on_3x_only(self, kind: str) -> None:
        """README row "``hybrid_search()``: the fusion": SurrealDB 3.x answers ``search::rrf()`` — here two records
        scoring 1/61 each —; SurrealDB 2.x and the embedded engine have no such function and refuse the statement."""
        async with _hy(kind) as client:
            major = await _major(client)
            rows: list[dict[str, Any]] | None = None
            refused: pytest.ExceptionInfo[Exception] | None = None
            if major >= 3:
                rows = await client.query(_PIN_RRF, {})
            else:
                with pytest.raises((InternalError, RuntimeError)) as refused:
                    await client.query(_PIN_RRF, {})
        if major >= 3:
            assert rows is not None
            assert sorted((row["id"], row["rrf_score"]) for row in rows) == [(1, 1 / 61), (2, 1 / 61)], (
                f"{_ROW_FUSION}: SurrealDB 3.x fuses the lists with search::rrf()"
            )
        else:
            assert refused is not None
            assert _NO_RRF in str(refused.value), f"{_ROW_FUSION}: SurrealDB 2.x has no search::rrf()"

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_fused_score_is_the_sum_of_1_over_k_plus_rank(self, kind: str) -> None:
        """README section 38, "Search": each record gets 1 / (``rrf_k`` + rank) from each list it is in, its rank
        counted from 1. SurrealDB 3.x's ``search::rrf()`` gives the record of both lists 1/62 + 1/61, bit for bit, and
        the records the ORM fuses itself below 3, the same records, scores and order; 2.x and the embedded engine refuse
        the function."""
        fused = hybrid.fuse(_FORMULA_LISTS, k=60, vector_limit=2)
        async with _hy(kind) as client:
            major = await _major(client)
            rows: list[dict[str, Any]] | None = None
            refused: pytest.ExceptionInfo[Exception] | None = None
            if major >= 3:
                rows = await client.query(_PIN_RRF_FORMULA, {})
            else:
                with pytest.raises((InternalError, RuntimeError)) as refused:
                    await client.query(_PIN_RRF_FORMULA, {})
        assert [(row["id"], row["rrf_score"]) for row in fused] == _FORMULA_ANSWER, f"{_S38_FORMULA}: the ORM's fusion"
        if major >= 3:
            assert rows is not None
            assert [(row["id"], row["rrf_score"]) for row in rows] == _FORMULA_ANSWER, (
                f"{_S38_FORMULA}: SurrealDB 3.x's search::rrf()"
            )
            assert rows == fused, f"{_S38_FORMULA}: the same records, scores and order as the ORM's fusion"
        else:
            assert refused is not None
            assert _NO_RRF in str(refused.value), f"{_ROW_FUSION}: SurrealDB 2.x has no search::rrf()"


# === E2E: an exact scan with records at an equal distance at the last place, on each line ===========================

#: The README row the tie pins back.
_ROW_TIES = (
    "README row 'An exact scan (`similar_to(dist=…)`, `hybrid_search(dist=…)`) with records at an equal distance at "
    "the last place'"
)

#: The records ``d2`` to ``d8``: one and the same embedding, at one cosine distance (0.4) from ``_TIE_VECTOR``.
_TIED = [f"d{number}" for number in range(2, 9)]

#: The tie corpus, in ``hy_doc``: ``d1`` alone nearest to ``_TIE_VECTOR`` and the only title holding "news", then the
#: seven tied records, so the three nearest are ``d1`` and two of the seven.
_TIE_CORPUS = "CREATE hy_doc:d1 SET title = 'news one', category = 'tie', n = 1, embedding = [1.0, 0.0, 0.0]; " + " ".join(
    f"CREATE hy_doc:{key} SET title = 'other', category = 'tie', n = {number}, embedding = [0.6, 0.8, 0.0];"
    for number, key in enumerate(_TIED, start=2)
)

_TIE_VECTOR = [1.0, 0.0, 0.0]

#: Identical calls of each search. SurrealDB 2.x's and the embedded engine's own exact scan picks two of the seven tied
#: records in no fixed order — measured on 60 calls: 20 of the 21 pairs, none above one call in eight —, so twelve
#: calls giving a single answer without the ORM's ranking would happen far less than once in 10^9 runs.
_TIE_CALLS = 12


async def _tie_answers(dist: str | None) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]]]:
    """``_TIE_CALLS`` identical calls of ``hybrid_search()`` (three nearest, the one match of "news") and of
    ``similar_to()`` (three nearest), with *dist* (an exact scan) or the HNSW index: each fused answer's ids in order,
    after checking that the two tied records it holds come at one distance; each ``similar_to()`` answer's ids, in
    order."""
    fused: list[tuple[str, ...]] = []
    nearest: list[tuple[str, ...]] = []
    for _ in range(_TIE_CALLS):
        rows = await HyDoc.objects().hybrid_search(
            vector_field="embedding",
            vector=_TIE_VECTOR,
            vector_limit=3,
            text_field="title",
            text_query="news",
            text_limit=1,
            dist=dist,
        )
        assert len(rows) == 3 and rows[1]["_knn_distance"] == rows[2]["_knn_distance"], rows
        fused.append(tuple(row["id"].id for row in rows))
        records = await HyDoc.objects().similar_to("embedding", _TIE_VECTOR, 3, dist=dist).exec()
        nearest.append(tuple(str(record.id) for record in records))
    return fused, nearest


class TestExactScanTiesE2E:
    """Records at an equal distance at the ``vector_limit``-th place, pinned on every engine (the line under test over
    WebSocket and HTTP, and the embedded engine), the test naming the README row it backs: every answer holds the
    nearest record and two of the tied ones, the fused records in the ORM's order (distance, then id); the HNSW index
    and the exact scan give one answer to identical calls on every engine. Below SurrealDB 3, whose own exact scan picks
    among the tied records in no fixed order, the ORM ranks the scan by distance then id: the tied records kept are the
    first two by id, and ``similar_to()`` returns them in that order. On SurrealDB 3.x ``similar_to()`` orders by
    distance only, so its answers are compared as sets there."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_exact_scan_gives_one_answer_on_every_engine(self, kind: str) -> None:
        async with _hy_models(kind) as client:
            major = await _major(client)
            await HyDoc.define_table()
            await HyDoc.define_indexes()
            await _apply_ddl(client, _TIE_CORPUS)
            await _compaction.wait_for_model_indexes(client, HyDoc)
            exact_fused, exact_nearest = await _tie_answers("cosine")
            index_fused, index_nearest = await _tie_answers(None)
        for answer in exact_fused + index_fused:
            assert answer[0] == "d1" and set(answer[1:]) <= set(_TIED) and list(answer[1:]) == sorted(answer[1:]), (
                f"{_ROW_TIES}: the nearest record, then two tied ones by id: {answer}"
            )
        for keys in exact_nearest + index_nearest:
            assert len(keys) == 3 and "d1" in keys and set(keys) - {"d1"} <= set(_TIED), f"{_ROW_TIES}: {sorted(keys)}"
        assert len(set(index_fused)) == 1 and len({frozenset(keys) for keys in index_nearest}) == 1, (
            f"{_ROW_TIES}: the HNSW index returns the same records on every call"
        )
        assert len(set(exact_fused)) == 1 and len({frozenset(keys) for keys in exact_nearest}) == 1, (
            f"{_ROW_TIES}: the exact scan returns the same records on every call"
        )
        if major < 3:
            assert set(exact_fused) == {("d1", "d2", "d3")} and set(exact_nearest) == {("d1", "d2", "d3")}, (
                f"{_ROW_TIES}: below 3 the ORM keeps the tied records by id, in that order"
            )


# === README: section 38, the behaviour tables, the features, roadmap and comparison rows; docs/ROADMAP.md ===========

_SECTION_36 = "### 36. Full-text search (`search()`, `SearchScore`, `SearchHighlight`, `Analyzer`, `Index`)"
_SECTION_37 = "### 37. Vector search (`similar_to()`, `VectorField`, `Index(hnsw=True)`)"
_SECTION_38 = "### 38. Hybrid search (`hybrid_search()`, Reciprocal Rank Fusion)"
#: Section 38's subsections, in order, after an untitled intro.
_SUBSECTIONS_38 = [
    "Search",
    "With the queryset",
    "What SurrealDB decides",
    "Not supported",
    "Coming from the full SurrealDB-ORM",
]
_INTRO_38 = (
    "Since v0.37.0 a queryset fuses two rankings of its records with the full SurrealDB-ORM's `hybrid_search()`: the "
    "records nearest to a query vector, as `similar_to()` finds them "
    "([section 37](#37-vector-search-similar_to-vectorfield-indexhnswtrue)), and the best matches of a text, as "
    "`search()` ranks them by BM25 score "
    "([section 36](#36-full-text-search-search-searchscore-searchhighlight-analyzer-index)), are fused into one ranking "
    "by Reciprocal Rank Fusion. SurrealDB 3.x fuses the two lists with its `search::rrf()`; SurrealDB 2.x and the "
    "embedded engine have no such function, so the ORM fuses them with the same formula: the same two lists give the "
    "same records, scores and order on every line. Each record comes back as a dict, as in the full ORM. The examples "
    "assume a configured connection: they run as written, in order, on the embedded engine (`mem://`)."
)
#: The four ``python`` blocks of section 38, in order, word for word: one opens each part but "Coming from".
_BLOCK_SEARCH_38 = """from surreal_orm_lite import Analyzer, BaseSurrealModel, Index, SurrealConfigDict, SurrealDbError, VectorField

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
"""
_BLOCK_QUERYSET_38 = """tech = await Passage.objects().filter(topic="tech").limit(2).hybrid_search(
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
"""
_BLOCK_DECIDES_38 = """try:
    await Passage.objects().hybrid_search(
        vector_field="embedding", vector=[1.0, 0.0, 0.0], text_field="topic", text_query="tech",
    )
except SurrealDbError as error:
    print(error)
# hybrid_search(): no full-text index on field 'topic'. SurrealDB matches only a field with a FULLTEXT (3.x) or SEARCH (2.x) index: declare Index(..., search_analyzer=...) on the model and apply define_indexes().
"""
_BLOCK_NOT_SUPPORTED_38 = """try:
    await Passage.objects().order_by("title").hybrid_search(
        vector_field="embedding", vector=[1.0, 0.0, 0.0], text_field="title", text_query="cats",
    )
except ValueError as error:
    print(error)
# hybrid_search() cannot be combined with order_by(): it runs its own vector and full-text searches and orders the records by their fused score. Remove them from the queryset.
"""
#: Each part's block (the parts not named hold none).
_BLOCKS_BY_PART_38 = {
    "Search": _BLOCK_SEARCH_38,
    "With the queryset": _BLOCK_QUERYSET_38,
    "What SurrealDB decides": _BLOCK_DECIDES_38,
    "Not supported": _BLOCK_NOT_SUPPORTED_38,
}
#: What the two dict-printing blocks print, line by line: the fused records of the corpus, the same on every line.
_PRINTED_SEARCH_38 = [
    "passage:p1 0.0164 True False",
    "passage:p3 0.0164 False True",
    "passage:p2 0.0161 True False",
    "passage:p4 0.0161 False True",
    "passage:p6 0.0159 True False",
    "passage:p7 0.0159 False True",
]
_PRINTED_QUERYSET_38 = ["passage:p3 0.0328", "passage:p4 0.0323", "['passage:p3', 'passage:p2']"]
#: The prose under the "Search" block.
_S38_RANKS = (
    "Each record gets 1 / (`rrf_k` + rank) from each list it is in, its rank counted from 1 (`rrf_k` is 60 by "
    "default): `p1`, nearest, and `p3`, the best match, both score 1/61. Records with equal scores keep the vector "
    "list's order first, then the full-text list's for the records it alone found, on every line — SurrealDB's "
    "`search::rrf()` alone returns them in no fixed order. `_knn_distance` is `None` for a record only the full-text "
    "search found, `_search_score` for one only the vector search found."
)
#: The prose under the "With the queryset" block.
_S38_QUERYSET = (
    "The queryset's conditions select the records of both lists. `select()` and `fetch()` apply to both, `id` always "
    "read; `limit()` and `offset()` page through the fused records, of which there are at most `vector_limit + "
    "text_limit`. `cache()`, `objects(tx=)` and named connections apply as for `exec()`, and `dist=` scans exactly, as "
    "in section 37."
)
#: Each part's prose, the text of its block left out (whitespace normalised).
_PROSE_38 = {"Search": _S38_RANKS, "With the queryset": _S38_QUERYSET}
#: The bullets of the other parts, in order, word for word (whitespace normalised, without the leading ``- ``).
_BULLETS_38 = {
    "What SurrealDB decides": [
        "Each list is SurrealDB's own: the vector list as `similar_to()` finds it — HNSW is approximate (section 37) — "
        "and the full-text list as `search()` ranks it, with section 36's BM25 scores. Those depend on the line: a term "
        "found in more than half the documents scores `0.0` on SurrealDB 3.x and below zero on 2.x and the embedded "
        "engine ([2.x/3.x table](#orm-behaviour-surrealdb-2x-vs-3x)), so the full-text list, and the fused ranking with "
        "it, can differ between lines.",
        "**An exact scan (`dist=`) returns the same vector list on every call**, on every line. SurrealDB 2.x's and the "
        "embedded engine's own scan picks among records at an equal distance at the `vector_limit`-th place in no fixed "
        "order, so there the ORM ranks it by distance, then id, as for `similar_to()` (section 37): the tied records are "
        "kept by id, and a page read with `offset()` and `limit()` never skips nor repeats one. The HNSW index (no "
        "`dist=`) returns the same list on every call too.",
        "Each search needs its index. Without a full-text index on `text_field`, SurrealDB 3.3, 2.x and the embedded "
        "engine refuse the search, and `hybrid_search()` raises `SurrealDbError` naming the field, as above; SurrealDB "
        "3.1 and 3.2 match nothing, so the vector list alone is fused.",
        "Without a vector index on `vector_field`, the vector search finds nothing on SurrealDB 3.3, 2.x and the "
        "embedded engine, so the full-text ranking alone is fused, every `_knn_distance` `None`; SurrealDB 3.1 and 3.2 "
        "list the first `vector_limit` records by id, with no distance. Pass `dist=` for an exact scan that needs no "
        "index.",
    ],
    "Not supported": [
        "`order_by()`, `annotate()`, `values()`, `search()` and `similar_to()` raise `ValueError` before any request, as "
        "above; `as_of()` raises `SurrealDbError`: the indexes hold the records' current values.",
        "Several full-text fields, weights and score normalisation (`search::linear()`), and the full-text side's "
        "highlights are not available yet.",
        "**A field stored under a name `hybrid_search()` uses for its own values** — `rrf_score`, `_rrf_score`, "
        "`_knn_distance`, `_search_score`, `_hybrid_pos`, `_hybrid_vec_pos` or `_hybrid_text_pos` — raises `ValueError` "
        "before any request: store it under another name (`Field(alias=...)`). On a SCHEMALESS table, a column with "
        "one of these names that the model does not declare is not returned, on every line, even when `select()` "
        "names it.",
    ],
    "Coming from the full SurrealDB-ORM": [
        "**`hybrid_search()` keeps the full ORM's signature**: keyword-only `vector_field`, `vector`, `vector_limit=20`, "
        "`text_field`, `text_query`, `text_limit=20`, `rrf_k=60`; `ef` is `None` for 100, so that `dist=` (an exact "
        "scan, section 37) can be given instead.",
        "**It returns dicts, as the full ORM does**: each holds the record's fields, keyed by their Python names as "
        "`exec()`'s dict rows are, and `_rrf_score`, plus `_knn_distance` and `_search_score`, which the full ORM does "
        "not return.",
        "**It applies the queryset**: its conditions, `select()`, `fetch()`, `limit()`/`offset()`, `cache()`, "
        "transactions and named connections; the full ORM ignores them.",
        "**SurrealDB's `search::rrf()` fuses the lists on 3.x**, given both lists in one statement, and the ORM fuses "
        "them on 2.x and the embedded engine; the full ORM calls `search::rrf()` with arguments SurrealDB 3.3 refuses, "
        "and runs on 3.x only.",
        "**Equal fused scores have a fixed order** (the vector list's first), so the same two lists always give the "
        "same page, and an exact scan (`dist=`) gives the same vector list on every line, as above.",
        "**Checked before any request**: both fields, the vector, `vector_limit` and `text_limit` (1 to 10 000), "
        "`rrf_k` (0 to 10 000), `ef` and `dist`; the full ORM checks the field names only.",
        "**Reciprocal Rank Fusion only**: `search::linear()` (weights and score normalisation) is not offered yet.",
    ],
}
#: What v0.37.0 changes in sections 36 and 37: section 36's last "Coming from" bullet, section 37's eleventh, and the
#: sentence that ends section 37's first "Not supported" bullet (whitespace normalised).
_BULLET_36_HYBRID = "- **`hybrid_search()` fuses `search()`'s ranking with `similar_to()`'s** (section 38, v0.37.0)."
_BULLET_37_ANNOTATE = (
    "- **Vector functions in `annotate()`** (`vector::similarity::cosine()`…) are not available yet; `hybrid_search()` "
    "(section 38) fuses this ranking with `search()`'s."
)
_S37_SCORES_HYBRID = (
    " `hybrid_search()` gives each record found by both searches its BM25 score and its distance, on every line."
)
_HEADER_SENTENCE_37 = (
    "Rows added in v0.37.0 were measured on 2.7.0, 3.3.4, 3.2.6 and the embedded engine, with 3.1.5 and 3.2.6 where "
    "they differ from 3.3.4."
)
#: Exactly the three rows v0.37.0 adds to the 2.x/3.x table, cell by cell, within the table's column widths. Its fourth
#: row, the exact scan's records at an equal distance, is v0.37.4's since the ORM ranks that scan below 3 (``_TIE_ROW``).
_HYBRID_ROWS = [
    [
        "`hybrid_search()`: the fusion",
        "the ORM fuses the two lists with `search::rrf()`'s formula, which SurrealDB 2.x does not have: the same records, "
        "scores and order for the same two lists",
        "SurrealDB's `search::rrf()` fuses them; the ORM orders equal scores, which SurrealDB returns in no fixed order",
        "v0.37.0",
    ],
    [
        "`hybrid_search()` with other conditions",
        "applied to both lists; the vector list selects the records first (a `LET`), as `similar_to()` does",
        "applied to both lists, during each search",
        "v0.37.0",
    ],
    [
        "`hybrid_search()` on a field without a vector or full-text index",
        "no vector index: the vector list is empty, so the full-text ranking alone is fused; no full-text index: "
        "`SurrealDbError` naming the field",
        "the same; 3.1 and 3.2 put the first `vector_limit` records by id in the vector list, with no distance, and "
        "match no record for the text",
        "v0.37.0",
    ],
]
#: The row of the exact scan's records at an equal distance at the last place, rewritten by v0.37.4 (it documents
#: ``similar_to(dist=…)`` too), and the sentence v0.37.4 adds to the table's intro.
_TIE_ROW = [
    "An exact scan (`similar_to(dist=…)`, `hybrid_search(dist=…)`) with records at an equal distance at the last place",
    "the ORM ranks the scan by distance, then id: the same records on every call, the tied ones by id (2.x's own scan "
    "picks among them in no fixed order)",
    "the same records on every call, as with the HNSW index",
    "v0.37.4",
]
_HEADER_SENTENCE_TIES = (
    "The row added in v0.37.4 was measured on 2.7.0 and 3.3.4, over WebSocket and HTTP, and on the embedded engine, "
    "identical on 3.1.5."
)
#: The row v0.37.0 adds to the embedded engine's table, cell by cell.
_EMBEDDED_HYBRID_ROW = [
    "Hybrid search (v0.37.0)",
    "as on a 2.x server: the ORM fuses the two lists with `search::rrf()`'s formula, which the engine does not have, "
    "and the vector list selects the other conditions first (a `LET`); an exact scan (`dist=`) keeps the records at an "
    "equal distance at the `vector_limit`-th place by id (since v0.37.4), as `similar_to(dist=…)` does",
    "3.x fuses with `search::rrf()`, each list applying the conditions; its exact scan returns the same list",
    "v0.37.0",
]
_FEATURES_ROW_38 = ["Hybrid search (`hybrid_search()`, Reciprocal Rank Fusion)", "✅"]
#: The roadmap from v0.36.0: v0.37.0 ships hybrid search, so the planned range starts at v0.38.0.
_ROADMAP_ROWS_38 = [
    ["v0.36.0", "Tier 3 — Vector search", "✅ Released"],
    ["v0.37.0", "Tier 3 — Hybrid search", "✅ Released"],
    ["v0.38.0 – v0.40.0", "Tier 3 — Advanced (migrations, CLI)", "📋 Planned"],
    ["v0.41.0", "Beta Phase (API freeze, hardening)", "📋 Planned"],
]
_COMPARISON_ROW_38 = ["Hybrid Search (RRF)", "✅ v0.37.0 (`hybrid_search()`)", "✅"]
#: The arguments of ``hybrid_search()`` a README example may give as numbers, with the range each accepts.
_HYBRID_BOUNDS = {"vector_limit": (1, 10_000), "text_limit": (1, 10_000), "rrf_k": (0, 10_000), "ef": (1, 10_000)}


def _section_38() -> str:
    """Section 38, as written, without the ``---`` that ends it."""
    body, separator, rest = _readme_part(_SECTION_38).rpartition("\n---\n")
    assert separator and not rest.strip(), "section 38 ends with the --- of the next part"
    return f"{body}\n"


def _part_38(heading: str) -> str:
    """One ``####`` subsection of section 38, as written (the intro, its heading included, for ``""``)."""
    if not heading:
        return _section_38().split("\n#### ", 1)[0]
    return _subsection(f"#### {heading}", _section_38())


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


def _prose(text: str) -> str:
    """*text* without its ``python`` blocks, whitespace normalised."""
    return _normalised(re.sub(r"^```python\n.*?^```$", "", text, flags=re.MULTILINE | re.DOTALL))


def _anchor(heading: str) -> str:
    """GitHub's anchor of a README heading: lower case, punctuation but ``-`` and ``_`` dropped, spaces as ``-``."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def _hybrid_search_numbers(block: str) -> list[tuple[str, object]]:
    """The numbers every ``hybrid_search()`` call of a README block gives (``vector_limit``, ``text_limit``, ``rrf_k``,
    ``ef``), as written: an ``int`` literal, or the source of any other expression."""
    tree = compile(block, "<README section 38>", "exec", flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    found: list[tuple[str, object]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "hybrid_search":
            assert not node.args, ast.unparse(node)
            for keyword in node.keywords:
                if keyword.arg in _HYBRID_BOUNDS:
                    value = keyword.value
                    found.append((keyword.arg, value.value if isinstance(value, ast.Constant) else ast.unparse(value)))
    return found


#: The cross-line guarantee, word for word in README section 38's intro and in ``hybrid_search()``'s docstring.
_SAME_TWO_LISTS = "the same two lists give the same records, scores and order on every line."

#: What may make the two lists differ between lines, in ``hybrid_search()``'s docstring right after the guarantee.
_EACH_LIST_DOC = (
    "Each list is SurrealDB's own, so the lists themselves can differ between lines: the HNSW search is approximate, "
    "and the full-text list follows the line's BM25 scores (a term found in more than half the documents scores "
    "``0.0`` on SurrealDB 3.x and below zero on 2.x and the embedded engine, README section 36)."
)

#: The ``dist`` argument of ``hybrid_search()``'s docstring, whitespace normalised.
_DIST_ARG_DOC = (
    "An exact scan by this distance instead of the index's search: ``EUCLIDEAN``, ``COSINE``, ``MANHATTAN``, "
    "``CHEBYSHEV`` or ``HAMMING``, in any case; not with ``ef``. SurrealDB 2.x's and the embedded engine's own scan "
    "picks among records at an equal distance at the ``vector_limit``-th place in no fixed order, so there the ORM "
    "ranks it by distance, then id, as ``similar_to(dist=...)`` does: the tied records are kept by id, and identical "
    "calls get one vector list on every line."
)


class TestHybridSearchDocstring:
    """``help(QuerySet.hybrid_search)``: the cross-line guarantee worded as README section 38 words it — the same two
    lists give the same records, scores and order on every line —, followed by what makes the lists differ; the
    ``dist`` argument naming the exact scan's ties at the ``vector_limit``-th place on SurrealDB 2.x and the embedded
    engine, which the ORM keeps by id."""

    def test_the_guarantee_holds_for_the_same_two_lists(self) -> None:
        doc = _normalised(inspect.getdoc(QuerySet.hybrid_search) or "")
        assert _SAME_TWO_LISTS in _normalised(_part_38(""))
        assert (
            "SurrealDB 2.x and the embedded engine have no such function, so the ORM fuses them with the same formula: "
            f"{_SAME_TWO_LISTS} {_EACH_LIST_DOC}"
        ) in doc
        assert "the records, scores and order are the same on every line" not in doc

    def test_dist_names_the_ties_of_the_exact_scan(self) -> None:
        doc = inspect.getdoc(QuerySet.hybrid_search) or ""
        match = re.search(r"^    dist: (.*?)\n\n", doc, re.MULTILINE | re.DOTALL)
        assert match is not None
        assert _normalised(match.group(1)) == _DIST_ARG_DOC
        # The ties are said once, in the argument they belong to.
        assert _normalised(doc).count("in no fixed order") == 1


class TestReadmeSection38:
    """README section 38 and what v0.37.0 changes elsewhere in the README: section 38's place, subsections, examples
    (run on ``mem://`` as written, each printed line equal to its comment), its intro, prose and bullets word for word;
    the bullets of sections 36 and 37 that point to it; the features, roadmap, comparison and both behaviour tables."""

    def test_section_38_follows_section_37(self) -> None:
        """Right after section 37 (after its "Coming from" bullets), the last section before the ``---`` of
        "Configuration Options"."""
        text = _readme()
        start_37 = text.index(f"\n{_SECTION_37}\n")
        start_38 = text.index(f"\n{_SECTION_38}\n")
        configuration = text.index("\n## Configuration Options\n")
        assert start_37 < start_38 < configuration
        assert re.findall(r"^#{2,3} .*$", text[start_37 + 1 : configuration], re.MULTILINE) == [_SECTION_37, _SECTION_38]
        assert text[:configuration].rstrip().endswith("\n---"), "section 38 ends before the --- of the next part"

    def test_subsections_in_order(self) -> None:
        assert re.findall(r"^#### (.*)$", _section_38(), re.MULTILINE) == _SUBSECTIONS_38

    def test_the_intro_word_for_word(self) -> None:
        """The untitled intro is the heading and one paragraph, exactly."""
        assert _normalised(_part_38("")) == f"{_SECTION_38} {_INTRO_38}"

    def test_the_examples_and_where_they_stand(self) -> None:
        """Each block exactly, opening its part; the intro and "Coming from" hold none; ``python`` fences only."""
        assert _readme_blocks(_section_38()) == list(_BLOCKS_BY_PART_38.values())
        for heading in ["", *_SUBSECTIONS_38]:
            part = _part_38(heading)
            block = _BLOCKS_BY_PART_38.get(heading)
            assert _readme_blocks(part) == ([block] if block else []), heading
            if block:
                assert part.split("\n", 1)[1].lstrip().startswith(f"```python\n{block}```\n"), heading
        fences = re.findall(r"^```(\S*)$", _section_38(), re.MULTILINE)
        assert fences == ["python", ""] * len(_BLOCKS_BY_PART_38), fences

    def test_what_the_blocks_print(self) -> None:
        """The dict-printing blocks print the corpus's fused records; the refusals print HS1 for ``topic`` and HY1 for
        ``order_by()``, the texts the method raises."""
        assert _readme_printed(_BLOCK_SEARCH_38) == _PRINTED_SEARCH_38
        assert _readme_printed(_BLOCK_QUERYSET_38) == _PRINTED_QUERYSET_38
        assert _readme_printed(_BLOCK_DECIDES_38) == [f"hybrid_search(): no full-text index on field 'topic'. {_SI1_ADVICE}"]
        assert _readme_printed(_BLOCK_NOT_SUPPORTED_38) == [_HY1.format("order_by()")]

    @pytest.mark.parametrize("heading", list(_PROSE_38))
    def test_the_prose_word_for_word(self, heading: str) -> None:
        """ "Search" and "With the queryset": their block, then one paragraph, exactly."""
        assert _prose(_part_38(heading)) == f"#### {heading} {_PROSE_38[heading]}"

    @pytest.mark.parametrize("heading", list(_BULLETS_38))
    def test_the_bullets_word_for_word(self, heading: str) -> None:
        """ "What SurrealDB decides" (four bullets), "Not supported" (three) and "Coming from the full
        SurrealDB-ORM" (seven): each bullet exactly, in order, and nothing else but the part's block."""
        part = _part_38(heading)
        bullets = _BULLETS_38[heading]
        assert len(bullets) == {"What SurrealDB decides": 4, "Not supported": 3}.get(heading, 7)
        assert _readme_bullets(part) == [f"- {bullet}" for bullet in bullets]
        assert _prose(part) == _normalised(" ".join([f"#### {heading}", *(f"- {bullet}" for bullet in bullets)]))

    def test_the_pins_name_section_38(self) -> None:
        """The README rows and sentence the line pins of this module name are the first row of v0.37.0, the row of
        v0.37.4 and the "Search" prose."""
        row = _ROW_FUSION.removeprefix("README row '").removesuffix("'")
        assert row == _HYBRID_ROWS[0][0], row
        ties = _ROW_TIES.removeprefix("README row '").removesuffix("'")
        assert ties == _TIE_ROW[0], ties
        quoted = _S38_FORMULA.split(", '", 1)[1].removesuffix("'")
        assert _S38_RANKS.startswith(quoted), quoted

    def test_sections_36_and_37_point_to_section_38(self) -> None:
        """Section 36's last "Coming from" bullet and section 37's eleventh name section 38; section 37's first "Not
        supported" bullet ends saying that ``hybrid_search()`` gives both values."""
        section_36 = _readme_part(_SECTION_36)
        section_37 = _readme_part(_SECTION_37)
        assert _readme_bullets(_subsection("#### Coming from the full SurrealDB-ORM", section_36))[-1] == _BULLET_36_HYBRID
        assert _readme_bullets(_subsection("#### Coming from the full SurrealDB-ORM", section_37))[10] == _BULLET_37_ANNOTATE
        first = _readme_bullets(_subsection("#### Not supported", section_37))[0]
        assert first.startswith("- Search annotations with `similar_to()`") and first.endswith(_S37_SCORES_HYBRID), first
        assert "comes in v0.37.0" not in section_36 + section_37

    def test_numbers_within_the_ranges(self) -> None:
        """Every ``hybrid_search()`` of the examples gives its numbers as ``int`` literals within their ranges, by
        keyword."""
        found = [number for block in _readme_blocks(_section_38()) for number in _hybrid_search_numbers(block)]
        assert {name for name, _ in found} == {"vector_limit", "text_limit"}, found
        for name, value in found:
            low, high = _HYBRID_BOUNDS[name]
            assert isinstance(value, int) and low <= value <= high, (name, value)

    def test_no_local_reference(self) -> None:
        """No design reference and no issue number in the section."""
        section = _section_38()
        for marker in ("§", r"\bspec\b", r"\.claude"):
            assert re.search(marker, section) is None, marker
        assert re.findall(r"(?<![(\w])#\d+", section) == []

    def test_links_resolve(self) -> None:
        """Every in-page link of section 38 names a heading: sections 37 and 36, then the 2.x/3.x table."""
        anchors = {_anchor(heading) for heading in re.findall(r"^#{2,4} (.*)$", _readme(), re.MULTILINE)}
        links = re.findall(r"\]\(#([^)]+)\)", _section_38())
        assert links == [
            _anchor(_SECTION_37.lstrip("# ")),
            _anchor(_SECTION_36.lstrip("# ")),
            _anchor("ORM behaviour: SurrealDB 2.x vs 3.x"),
        ], links
        assert [link for link in links if link not in anchors] == []

    def test_server_behaviour_rows(self) -> None:
        """Exactly three rows, Since v0.37.0, together and in version order before the table's last row; the table
        keeps its widths."""
        assert_server_rows(_HYBRID_ROWS)

    def test_server_behaviour_header_sentence(self) -> None:
        assert_server_header_sentence(_HEADER_SENTENCE_37)

    def test_the_exact_scan_row(self) -> None:
        """v0.37.4's one row, the exact scan's records at an equal distance (``TestExactScanTiesE2E``), Since v0.37.4,
        in version order before the table's last row, and its sentence in the table's intro."""
        assert_server_rows([_TIE_ROW])
        assert_server_header_sentence(_HEADER_SENTENCE_TIES)

    def test_embedded_table_row(self) -> None:
        """All four cells, in version order before the catch-all "Everything else" row; widths kept."""
        assert_embedded_rows([_EMBEDDED_HYBRID_ROW])

    def test_features_row(self) -> None:
        """Right after vector search."""
        features = [row for _, row in _table_rows(_readme_part("## Features"))]
        firsts = [row[0] for row in features]
        assert _FEATURES_ROW_38 in features, features
        assert firsts.index(_FEATURES_ROW_38[0]) == firsts.index("Vector search (`similar_to()`, KNN, HNSW indexes)") + 1

    def test_roadmap_rows(self) -> None:
        roadmap = [row for _, row in _table_rows(_readme_part("## Roadmap"))]
        start = [row[0] for row in roadmap].index("v0.36.0")
        assert roadmap[start : start + 4] == _ROADMAP_ROWS_38, roadmap
        assert "v0.37.0 – v0.40.0" not in [row[0] for row in roadmap], roadmap

    def test_comparison_row(self) -> None:
        """Released, right after vector search and before the migrations."""
        comparison = [row for _, row in _table_rows(_readme_part("## SurrealDB-ORM-lite vs SurrealDB-ORM"))]
        firsts = [row[0] for row in comparison]
        start = firsts.index("Vector Search (KNN/HNSW)")
        assert comparison[start + 1] == _COMPARISON_ROW_38, comparison
        assert firsts[start + 2] == "Migrations & CLI", firsts

    def test_tables_have_no_raw_pipe_in_a_cell(self) -> None:
        """Every table v0.37.0 touches keeps one cell count per line and no escaped pipe; section 38 has no table."""
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
        assert _md_tables(_section_38()) == []

    async def test_section_38_examples(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Every ``python`` block runs as written, in order and in one namespace, on ``mem://`` (inside
        ``embedded_database()``); what each block prints is, line by line, its ``# `` comment lines."""
        capsys.readouterr()
        namespace: dict[str, Any] = {"__name__": "readme_section_38"}
        try:
            async with embedded_database():
                for code in _readme_blocks(_section_38()):
                    compiled = compile(code, "<README section 38>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                    # A doctest of this repository's own README (trusted text), top-level awaits included.
                    outcome = eval(compiled, namespace)
                    if inspect.iscoroutine(outcome):
                        await outcome
                    assert capsys.readouterr().out.splitlines() == _readme_printed(code), code
        finally:
            namespace.clear()


# --- docs/ROADMAP.md --------------------------------------------------------------------------------------------------

_ROADMAP = Path(__file__).resolve().parents[1] / "docs" / "ROADMAP.md"
_PHASE_G = "### 🔎 Phase G — Advanced search"
_SDK_TABLE = "## 🆕 Everything unlocked by the official SDK 2.0"
_COMPARISON_TABLE = "## SurrealDB-ORM vs SurrealDB-ORM-lite Comparison"
#: The rows v0.37.0 holds while planned, by table.
_PLANNED_OVERVIEW_37 = [["v0.37.0 – v0.40.0", "Tier 3 — Advanced (hybrid search, migrations, CLI), 4 minors", "Planned"]]
_PLANNED_UNLOCKED_37 = ["Hybrid Search (RRF)", "vector + FTS fusion", "v0.37.0"]
_PLANNED_COMPARISON_37 = ["Hybrid Search (RRF)", "yes", "v0.37.0"]
_PLANNED_PHASE_G_37 = ["v0.37.0", "Hybrid Search (RRF)", "vector + FTS fusion"]
#: The rows v0.37.0 holds once released, by table, and its "Completed versions" heading.
_OVERVIEW_37 = [
    ["v0.37.0", "Tier 3 — Hybrid search", "Done"],
    ["v0.38.0 – v0.40.0", "Tier 3 — Advanced (migrations, CLI), 3 minors", "Planned"],
]
_UNLOCKED_37 = ["Hybrid Search (RRF)", "`search::rrf()` (3.x) or the same fusion in the ORM (2.x)", "✅ v0.37.0"]
_COMPARISON_37 = ["Hybrid Search (RRF)", "yes", "✅ v0.37.0"]
_PHASE_G_37 = ["✅ v0.37.0", "Hybrid Search (`hybrid_search()`, RRF)", "`search::rrf()` (3.x) + RRF in the ORM (2.x)"]
_COMPLETED_HEADING_36 = "### Version 0.36.0 — Vector search (`similar_to()`)"
_COMPLETED_HEADING_37 = "### Version 0.37.0 — Hybrid search (`hybrid_search()`)"
#: What the released block names among what it defers: the other fusion.
_DEFERRED_NEEDLES_37 = ("`search::linear()`",)


def _roadmap() -> str:
    return _ROADMAP.read_text(encoding="utf-8")


def _roadmap_rows(heading: str) -> list[list[str]]:
    """The rows of the first table under *heading*, each cell's escaped ``\\|`` read as ``|``."""
    return [[cell.replace("\\|", "|") for cell in cells] for _, cells in _table_rows(_subsection(heading, _roadmap()))]


def _released_37() -> bool:
    """Whether ``docs/ROADMAP.md`` records v0.37.0 (hybrid search) as released: its Phase G row carries the ✅ the
    release adds."""
    return _roadmap_rows(_PHASE_G)[2][0] == "✅ v0.37.0"


class TestRoadmap37:
    """``docs/ROADMAP.md`` records v0.37.0 as planned, or — once its Phase G row carries ✅ — as released: done in the
    overview, ✅ in the SDK and comparison tables and in Phase G, its block in "Completed versions" right after
    v0.36.0's. The Phase G row decides which state the whole file must be in."""

    def test_overview(self) -> None:
        overview = _roadmap_rows("## Overview")
        start = [cells[0] for cells in overview].index("v0.36.0") + 1
        expected = _OVERVIEW_37 if _released_37() else _PLANNED_OVERVIEW_37
        assert overview[start : start + len(expected)] == expected, overview
        assert overview[start + len(expected)][0] == "v0.41.0", overview

    def test_sdk_table(self) -> None:
        expected = _UNLOCKED_37 if _released_37() else _PLANNED_UNLOCKED_37
        rows = _roadmap_rows(_SDK_TABLE)
        assert [cells for cells in rows if cells[0].startswith("Hybrid Search")] == [expected], rows

    def test_comparison_table(self) -> None:
        expected = _COMPARISON_37 if _released_37() else _PLANNED_COMPARISON_37
        rows = _roadmap_rows(_COMPARISON_TABLE)
        assert [cells for cells in rows if cells[0].startswith("Hybrid Search")] == [expected], rows

    def test_phase_g(self) -> None:
        """Hybrid search is Phase G's third and last row."""
        rows = _roadmap_rows(_PHASE_G)
        assert rows[2:] == [_PHASE_G_37 if _released_37() else _PLANNED_PHASE_G_37], rows

    def test_completed_versions(self) -> None:
        """Released: v0.37.0's block right after v0.36.0's, naming the deferred ``search::linear()``. Planned: no such
        block."""
        completed = _roadmap().split("\n## Completed versions\n", 1)[1].split("\n## ", 1)[0]
        headings = re.findall(r"^### .*$", completed, re.MULTILINE)
        if not _released_37():
            assert _COMPLETED_HEADING_37 not in headings, headings
            return
        assert headings[headings.index(_COMPLETED_HEADING_37) - 1] == _COMPLETED_HEADING_36, headings
        block = _normalised(_subsection(_COMPLETED_HEADING_37, completed))
        missing = [needle for needle in _DEFERRED_NEEDLES_37 if needle not in block]
        assert not missing, missing
