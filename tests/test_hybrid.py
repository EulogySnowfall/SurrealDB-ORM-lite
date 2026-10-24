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
"""

import ast
import copy
from pathlib import Path
from typing import Any

import pytest
from surrealdb import InternalError, RecordID

from surreal_orm_lite import SurrealDbError, hybrid, search, vector
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
