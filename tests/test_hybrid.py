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
"""

import ast
import copy
import warnings
from pathlib import Path
from typing import Any

import pytest
from surrealdb import InternalError, RecordID

from surreal_orm_lite import (
    Analyzer,
    BaseSurrealModel,
    Index,
    Q,
    Subquery,
    SurrealConfigDict,
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
