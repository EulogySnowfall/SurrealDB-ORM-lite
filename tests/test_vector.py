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
"""

import ast
import copy
import pickle
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Any

import pytest
from pydantic import Field, ValidationError

import surreal_orm_lite
from surreal_orm_lite import BaseSurrealModel, SurrealConfigDict, Vector, VectorField, vector
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
