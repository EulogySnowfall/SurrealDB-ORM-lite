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
``is_vector_field()`` tells which.

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
"""

import ast
import contextlib
import copy
import os
import pickle
import re
from collections.abc import AsyncIterator
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest
from pydantic import Field, ValidationError

import surreal_orm_lite
from surreal_orm_lite import (
    Analyzer,
    BaseSurrealModel,
    DatabaseIntrospector,
    Index,
    IndexState,
    PointField,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    Vector,
    VectorField,
    search,
    vector,
)
from surreal_orm_lite.define_parser import parse_define_index
from surreal_orm_lite.vector import get_vector_info, is_vector_field

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


# === E2E: the connection "vec" on the database vec_db, over WebSocket and HTTP, and mem:// =========================

#: The engines every E2E class runs on: the line under test over WebSocket and HTTP, and the embedded engine.
_KINDS = ["ws", "http", "mem"]

#: The reset before each E2E test, in one query.
_VEC_RESET = "REMOVE DATABASE IF EXISTS vec_db; DEFINE DATABASE vec_db;"


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
async def _vec(kind: str) -> AsyncIterator[Any]:
    """The connection ``"vec"`` on *kind*, its database ``vec_db`` reset in one query; removed, then unregistered, on
    exit."""
    if kind == "mem":
        SurrealDBConnectionManager.add_connection("vec", url="mem://", namespace="ns", database="vec_db")
    else:
        SurrealDBConnectionManager.add_connection(
            "vec", url=_url(kind), user="root", password="root", namespace="ns", database="vec_db"
        )
    client = await SurrealDBConnectionManager.get_client("vec")
    await _apply_ddl(client, _VEC_RESET)
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            cleanup = await SurrealDBConnectionManager.get_client("vec")
            await cleanup.query("REMOVE DATABASE IF EXISTS vec_db;", {})
        await SurrealDBConnectionManager.remove_connection("vec")


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
