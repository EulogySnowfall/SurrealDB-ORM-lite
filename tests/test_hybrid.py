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

The method (``TestHybridSearchChecks``, ``TestHybridRows``): ``hybrid_search()``'s signature, and every refusal of its
arguments and of the queryset it is called on, each with its exact class and text, in a fixed order, before any client
is acquired, the queryset unchanged; the dicts it returns, built from the rows of either line — the record's columns
keyed by their Python names, ``_rrf_score``, ``_knn_distance`` and ``_search_score``, never SurrealDB's ``rrf_score``
nor the ORM's positions.

On a server (``TestHybridSearchE2E``, ``TestHybridDispatchE2E``), over WebSocket and HTTP on the line under test and on
the embedded engine: the cases of an eight-record corpus fuse to the same records, order and scores on every line —
equal scores in the vector list's order, then the full-text list's, every time —, each distance and score the one
``similar_to()`` and ``search()`` give the same record; SurrealDB 3.x fuses with ``search::rrf()``, the ORM below 3,
the line read once per client.
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
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field
from surrealdb import InternalError, RecordID

from surreal_orm_lite import (
    Analyzer,
    BaseSurrealEdge,
    BaseSurrealModel,
    Count,
    Index,
    Q,
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
from surreal_orm_lite.utils import validate_field_name

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
        # The flag _run_uncached() and _run_cached() read the line by (the pre-filter below 3), as for similar_to().
        for queryset, expected in [
            (HyDoc.objects(), False),
            (HyDoc.objects().filter(category="tech"), True),
            (HyDoc.objects().filter(id__in=Subquery(HyDoc.objects().filter(n__gt=2))), False),
        ]:
            runner = _runner(queryset)
            compiler = runner._subquery_compiler()
            runner._compile_hybrid(compiler, below_3=False, text_limit=4, rrf_k=60)
            assert compiler.knn_conditions is expected, queryset._filters

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


# === E2E: the connection "hy" on the database hy_db, over WebSocket and HTTP, and mem:// ============================

#: The engines every E2E class runs on: the line under test over WebSocket and HTTP, and the embedded engine.
_KINDS = ["ws", "http", "mem"]

#: The reset before each E2E test, in one query, of the database ``hy_db``.
_HY_RESET = "REMOVE DATABASE IF EXISTS hy_db; DEFINE DATABASE hy_db;"


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
async def _hy(kind: str) -> AsyncIterator[Any]:
    """The connection ``"hy"`` on *kind*, its database ``hy_db`` reset in one query; removed, then unregistered, on
    exit."""
    if kind == "mem":
        SurrealDBConnectionManager.add_connection("hy", url="mem://", namespace="ns", database="hy_db")
    else:
        SurrealDBConnectionManager.add_connection(
            "hy", url=_url(kind), user="root", password="root", namespace="ns", database="hy_db"
        )
    client = await SurrealDBConnectionManager.get_client("hy")
    await _apply_ddl(client, _HY_RESET)
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            cleanup = await SurrealDBConnectionManager.get_client("hy")
            await cleanup.query("REMOVE DATABASE IF EXISTS hy_db;", {})
        await SurrealDBConnectionManager.remove_connection("hy")


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
    the eight records."""
    await model.define_table()
    await model.define_indexes()
    await _apply_ddl(client, _corpus_statements(model.get_table_name()))


async def _setup_linked(client: Any) -> None:
    """``HyLinked`` set up as ``HyDoc``, each record linked to an author: ``Ann`` for the odd numbers, ``Bob`` for the
    even ones."""
    await HyAuthor.define_table()
    await HyLinked.define_table()
    await HyLinked.define_indexes()
    await _apply_ddl(client, "CREATE hy_author:ann SET name = 'Ann'; CREATE hy_author:bob SET name = 'Bob';")
    await _apply_ddl(
        client, _corpus_statements("hy_linked", lambda number: f", author = hy_author:{'ann' if number % 2 else 'bob'}")
    )


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
    args = _E2E_CASES[case][3]
    near = (
        await _case_queryset(case, model, paged=False).similar_to("embedding", args["vector"], 8, dist=args.get("dist")).exec()
    )
    found = (
        await _case_queryset(case, model, paged=False).search(title=args["text_query"]).annotate(score=SearchScore(0)).exec()
    )
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
    the full-text list's, five times in a row the same; each record a dict of its columns and the three values, its
    distance and score those ``similar_to()`` and ``search()`` give it; the queryset unchanged, its link heads
    included."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_cases_of_the_corpus(self, kind: str) -> None:
        async with _hy_models(kind) as client:
            await _setup(client)
            for case, (_, _, _, _, expected) in _E2E_CASES.items():
                rows = await _run_case(case)
                distances, scores = await _references(case)
                _check_answer(case, rows, expected, distances, scores)

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
