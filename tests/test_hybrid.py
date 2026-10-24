"""Hybrid search — ``QuerySet.hybrid_search()`` (v0.37.0): the fusion, the statements, the refusals and the behaviour
on SurrealDB 2.x, 3.x and the embedded engine.

The refusal texts that name their caller (``TestSubjectKeywords``): the checks and texts ``hybrid_search()`` shares
with ``similar_to()`` (``vector``) and ``search()`` (``search``) take a keyword-only ``subject=`` — and, for a limit,
``arg=`` — that starts each text with ``hybrid_search()`` and names its argument. Without the keyword each text is
v0.35.0's or v0.36.0's, byte for byte. These are pure: no client is involved, no request is sent.
"""

from typing import Any

import pytest
from surrealdb import InternalError

from surreal_orm_lite import SurrealDbError, search, vector

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
