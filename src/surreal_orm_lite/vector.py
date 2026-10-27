"""Vector search (v0.36.0): the vector field type, its typed marker, and the checks and renderers of the KNN search.

``VectorField[d]`` / ``VectorField[d, type]`` is the full SurrealDB-ORM's field type for the vectors SurrealDB's HNSW
indexes hold: an optional ``list[float]`` of exactly ``d`` items, ``None`` by default, stored as ``F32`` unless another
storage type is given (``F64``, ``F32``, ``I64``, ``I32`` or ``I16``, in any case). Type checkers do not understand
``VectorField[1536]`` (a class subscripted with a number), so the field has a second spelling, the typed form
``Annotated[list[float] | None, Vector(1536)] = None``: type checkers read ``list[float] | None`` with a default, and
Pydantic reads the ``Vector`` marker exactly as ``VectorField[1536]``'s. Both spellings are one field: the same
validation, the same column type (``option<array<float, 1536>>``), the same schema. ``get_vector_info()`` and
``is_vector_field()`` — the full ORM's helpers, under their names — read the marker of either spelling from a model
field or an annotation, and of their unions with ``None`` (``VectorField[1536] | None``,
``Annotated[list[float], Vector(1536)] | None``, ``VectorField[1536] | Literal[None]``), as ``define_table()`` reads
them; never from inside a container (``list[VectorField[3]]``). A field or an annotation holding two different markers,
or a marker next to a type other than ``None``, has no single dimension: the helpers refuse it, and a model's ``class``
statement refuses such a field (``declared_vectors()``) — or, for a field declared through a forward reference, the
completion of the model once the name resolves: ``model_rebuild()``, its first instantiation, validation or JSON schema,
or the ORM's first DDL or queryset of the model (``BaseSurrealModel._complete_pending_checks()``).

A value is ``None`` or a ``list``/``tuple`` of exactly ``d`` real numbers — ``int``, ``float``, any ``numbers.Real``,
a ``decimal.Decimal`` (SurrealDB's decimals read back as ``Decimal``) —, never a ``bool`` nor a ``str``, each finite
once converted to ``float``. It is stored as a ``list[float]``, on a write and on a read alike, and serialised as the
list.

**The KNN search.** ``QuerySet.similar_to()`` writes SurrealDB's ``<column> <|K,EF|> $vector`` (an HNSW search) or
``<column> <|K,DIST|> $vector`` (an exact search, no index). The checkers and renderers here keep that statement safe:

- the query vector is always a bound variable — a ``list[float]`` the ORM builds from checked real numbers
  (``checked_query_vector()``), never written into the statement;
- ``K`` (the ``limit``) and ``EF`` are integers from 1 to 10000 (``checked_limit()``, ``checked_ef()``), written
  from Python ``int``s — SurrealDB takes them as literals only — and the ``EF`` written is never below ``K``;
- a distance and a storage type are words of closed lists (``DISTANCES``, ``VECTOR_TYPES``): the ORM writes its own
  constant for them, never the caller's text; an index's dimension, ``EFC`` and ``M`` are checked integers
  (``checked_count()``).

``hnsw_clause()`` and ``knn_condition()`` are pure renderers, and check what they write again: a value outside these
rules raises rather than reaching a statement. ``KnnSearch`` is one ``similar_to()`` of a queryset once checked, and
the texts of its refusals are here, each naming the field or the model, never the statement nor the vector.

**The other conditions below SurrealDB 3.** SurrealDB 3.x applies the conditions AND-ed with the KNN during the index
search. SurrealDB 2.x and the embedded engine answer wrong records for many of them — a condition on an indexed
column, a function call, a negation, a group, a dotted path, a full-text match —, so below 3 the queryset selects the
records matching them first, in a ``LET`` of the statement, and the KNN searches those ids. ``id_subquery_part()``
tells the only parts that stay next to the KNN on every line: the ORM's own id subqueries.

**The exact scan below SurrealDB 3.** SurrealDB 2.x's and the embedded engine's exact scan (``<|K,DIST|>``) picks among
records at an equal distance at the ``K``-th place in no fixed order, so the same read can return other records, and a
page skip or repeat one. Below 3 the queryset selects the ``K`` nearest first, in a ``LET`` ranked by distance then id
(``exact_scan_select()``): the distance is the function SurrealDB's scan computes (``exact_distance()``), bit for bit,
on the records its scan scores (``scored_vector()``). The KNN then searches exactly those ids, so each record's
``vector::distance::knn()`` stays SurrealDB's own; SurrealDB 3.x's exact scan, which gives one answer, is unchanged.

**The server's dimension error.** When the model does not declare a field's dimension, SurrealDB checks the query
vector against the vector index and refuses one of another length; ``dimension_refusal()`` reads that refusal into the
ORM's error, naming the field and both lengths (``is_dimension_error()`` tells it).

The module sits low in the package: it imports neither ``search``, ``model_base``, ``query_set`` nor
``connection_manager``, so each of them can import it.
"""

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from numbers import Real
from types import NoneType, UnionType
from typing import Annotated, Any, Literal, NoReturn, Self, TypedDict, Union, get_args, get_origin

from pydantic import Field, GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.fields import FieldInfo
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import core_schema

from ._sdk import ServerError
from .exceptions import SurrealDbError
from .utils import VALID_FIELD_PATTERN, validate_field_name

__all__ = ["VectorField", "Vector", "get_vector_info", "is_vector_field"]

#: The largest dimension SurrealDB's ``DIMENSION`` accepts.
MAX_DIMENSION = 65535

#: The largest ``EFC`` (the HNSW index's construction candidate list) the ORM writes.
MAX_EFC = 65535

#: The ``M`` (the HNSW index's connections per node) the ORM writes, from the first to the second, both included.
M_RANGE = (2, 127)

# The ORM's range for K and EF.
MAX_K = 10_000
MAX_EF = 10_000

#: The ``EF`` of an HNSW search when ``similar_to()`` is given no ``ef`` (the full SurrealDB-ORM's default).
DEFAULT_EF = 100

#: The distances the ORM writes, in SurrealDB's spelling.
DISTANCES = ("EUCLIDEAN", "COSINE", "MANHATTAN", "CHEBYSHEV", "HAMMING")

#: The function call that computes each distance as SurrealDB's exact scan does, on a vector ``{column}`` and the query
#: vector ``${variable}``: the same value and type as ``vector::distance::knn()`` on SurrealDB 2.x and the embedded engine.
#: SurrealDB has no ``vector::distance::cosine()``: its scan's ``COSINE`` distance is 1 minus the cosine similarity.
_DISTANCE_CALLS = {
    "EUCLIDEAN": "vector::distance::euclidean({column}, ${variable})",
    "COSINE": "1 - vector::similarity::cosine({column}, ${variable})",
    "MANHATTAN": "vector::distance::manhattan({column}, ${variable})",
    "CHEBYSHEV": "vector::distance::chebyshev({column}, ${variable})",
    "HAMMING": "vector::distance::hamming({column}, ${variable})",
}

#: The storage types of a vector field and of an HNSW index, in SurrealDB's spelling.
VECTOR_TYPES = ("F64", "F32", "I64", "I32", "I16")


class _HnswDefaults(TypedDict):
    """The type of ``DEFAULTS``."""

    dist: str
    vector_type: str
    efc: int
    m: int


#: SurrealDB's documented default of each HNSW option an ``Index`` does not declare: what the ORM writes for it.
DEFAULTS: _HnswDefaults = {"dist": "EUCLIDEAN", "vector_type": "F32", "efc": 150, "m": 12}

#: The attribute that carries each record's distance from the query vector.
DISTANCE_ALIAS = "_knn_distance"

#: The two forms the ORM writes for a ``Subquery`` given to ``id__in``: ``id IN $_sq<N>``, and ``$_sq<N> CONTAINS id``
#: on a view model (v0.32.0) — the only parts of a KNN statement's ``WHERE`` kept next to the KNN on every line.
_ID_SUBQUERY_PART = re.compile(r"id IN \$_sq[0-9]+|\$_sq[0-9]+ CONTAINS id")

#: The start of SurrealDB's refusal of a query vector whose length is not its vector index's dimension, the same on
#: every engine: ``Incorrect vector dimension (2). Expected a vector of 3 dimension.``
_DIMENSION_MARKER = "Incorrect vector dimension ("
#: The two lengths that refusal gives: the query vector's, then the index's dimension.
_DIMENSION_LENGTHS = re.compile(r"Incorrect vector dimension \((\d+)\)\. Expected a vector of (\d+) dimension")

_DISTANCE_LIST = "EUCLIDEAN, COSINE, MANHATTAN, CHEBYSHEV and HAMMING"
_VECTOR_TYPE_LIST = "F64, F32, I64, I32 and I16"

# The distances SurrealDB knows that the ORM does not write, each with its reason.
_MINKOWSKI = "MINKOWSKI needs its order, which this version does not write; use EUCLIDEAN (order 2) or MANHATTAN (order 1)."
_NOT_RANKING = "{dist} is not a distance SurrealDB ranks by: identical vectors are not the nearest with it. Use COSINE."


# --- the values -----------------------------------------------------------------------------------------------------


def _as_float(item: object) -> float | None:
    """``item`` as a ``float``, or ``None`` when it is not a number: a ``bool``, a ``str``, anything neither a
    ``numbers.Real`` nor a ``Decimal``. A number that has no finite ``float`` (NaN, an infinity, a magnitude beyond
    ``float``'s range) gives NaN or an infinity, which the callers refuse."""
    if isinstance(item, bool) or not isinstance(item, Real | Decimal):
        return None
    if isinstance(item, Decimal) and not item.is_finite():
        return math.nan
    try:
        return float(item)
    except (OverflowError, ValueError):
        return math.inf


# --- the field ------------------------------------------------------------------------------------------------------


def _vf4(value: object) -> NoReturn:
    """Refuse an argument of ``Vector(...)`` of the wrong type, naming the offending value's type."""
    raise TypeError(
        "Vector takes a dimension (an int) and an optional storage type (a str), such as Vector(1536) or "
        f'Vector(384, "F64"), not {type(value).__name__}.'
    )


def _vf1(value: object) -> NoReturn:
    """Refuse a parameter of ``VectorField[...]`` of the wrong shape, naming the offending value's type."""
    raise TypeError(
        "VectorField takes a dimension (an int), or a dimension and a storage type, such as VectorField[1536] or "
        f'VectorField[384, "F64"], not {type(value).__name__}.'
    )


def _checked_marker(dimension: int, vector_type: str, *, subject: str) -> tuple[int, str]:
    """The dimension and the upper-case storage type of a ``Vector``, checked once its arguments' types are (VF2).

    ``subject`` is the name the refusal gives: ``Vector`` or ``VectorField``.
    """
    if not 1 <= dimension <= MAX_DIMENSION:
        raise ValueError(
            f"{subject} dimension must be between 1 and {MAX_DIMENSION} (SurrealDB's DIMENSION), got {dimension!r}."
        )
    upper = vector_type.upper()
    if upper not in VECTOR_TYPES:
        raise ValueError(f"{subject} storage type must be one of {_VECTOR_TYPE_LIST}, got {vector_type!r}.")
    return dimension, VECTOR_TYPES[VECTOR_TYPES.index(upper)]


class Vector:
    """The ``Annotated`` marker of a vector field (v0.36.0): its dimension and storage type.

    The typed form of ``VectorField[1536]``, which type checkers accept::

        class Document(BaseSurrealModel):
            embedding: Annotated[list[float] | None, Vector(1536)] = None
            small: Annotated[list[float] | None, Vector(384, "F64")] = Field(default=None, alias="small_vec")

    ``dimension`` is an ``int`` from 1 to 65535 (SurrealDB's ``DIMENSION``); ``vector_type`` one of ``F64``, ``F32``,
    ``I64``, ``I32`` and ``I16`` in any case, held upper case — ``F32`` by default. A ``bool`` or a non-``int``
    dimension, or a non-``str`` type, raises ``TypeError``; a value out of range ``ValueError``.

    The marker is read-only, equal to and hashed with a marker of the same dimension and type, and its ``repr`` is
    ``Vector(1536, 'F32')``. As a field's metadata it validates the value: ``None``, or a ``list``/``tuple`` of exactly
    ``dimension`` real numbers (not a ``bool`` nor a ``str``), each finite, stored as a ``list[float]`` — any other
    value raises a ``ValidationError``, on a write and on a read. The field's JSON schema is a fixed-length array of
    numbers, or null. Give the field its ``None`` default in the class body (``= None``, or ``Field(default=None, …)``
    with an alias): the marker does not make the field optional by itself.
    """

    __slots__ = ("dimension", "vector_type")

    dimension: int
    vector_type: str

    def __init__(self, dimension: int, vector_type: str = "F32") -> None:
        if isinstance(dimension, bool) or not isinstance(dimension, int):
            _vf4(dimension)
        if not isinstance(vector_type, str):
            _vf4(vector_type)
        checked = _checked_marker(dimension, vector_type, subject="Vector")
        object.__setattr__(self, "dimension", checked[0])
        object.__setattr__(self, "vector_type", checked[1])

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"Vector is read-only: {name!r} cannot be set.")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"Vector is read-only: {name!r} cannot be deleted.")

    def __reduce__(self) -> tuple[type["Vector"], tuple[int, str]]:
        return Vector, (self.dimension, self.vector_type)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Vector):
            return NotImplemented
        return (self.dimension, self.vector_type) == (other.dimension, other.vector_type)

    def __hash__(self) -> int:
        return hash((Vector, self.dimension, self.vector_type))

    def __repr__(self) -> str:
        return f"Vector({self.dimension}, {self.vector_type!r})"

    def _validate(self, value: Any) -> list[float]:
        """The value as a ``list[float]``, or ``ValueError`` (Pydantic raises it as a ``ValidationError``)."""
        dimension = self.dimension
        if not isinstance(value, list | tuple):
            raise ValueError(f"expected a list or tuple of {dimension} numbers, got {type(value).__name__}")
        if len(value) != dimension:
            raise ValueError(f"expected {dimension} numbers, got {len(value)}")
        floats: list[float] = []
        for position, item in enumerate(value):
            number = _as_float(item)
            if number is None:
                raise ValueError(f"item {position} is {type(item).__name__}, not a number")
            if not math.isfinite(number):
                raise ValueError(f"item {position} is not finite ({item!r})")
            floats.append(number)
        return floats

    def __get_pydantic_core_schema__(self, source_type: Any, handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
        # The handler is not called: ``list[float] | None`` is what type checkers read, the validator decides.
        return core_schema.nullable_schema(
            core_schema.no_info_plain_validator_function(
                self._validate, serialization=core_schema.plain_serializer_function_ser_schema(list)
            )
        )

    def __get_pydantic_json_schema__(self, schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        array = {"type": "array", "items": {"type": "number"}, "minItems": self.dimension, "maxItems": self.dimension}
        return {"anyOf": [array, {"type": "null"}]}


# ``Annotated`` subscripted with values built at run time (mypy takes it for a type expression).
_ANNOTATED: Any = Annotated


class VectorField:
    """A vector of floats for SurrealDB's HNSW indexes (v0.36.0) — the full SurrealDB-ORM's field type.

    ``VectorField[1536]`` is ``Annotated[list[float] | None, Vector(1536, "F32"), Field(default=None)]``: an optional
    field, ``None`` by default, holding exactly 1536 real numbers stored as floats. ``VectorField[384, "F64"]`` gives
    the storage type (``F64``, ``F32``, ``I64``, ``I32`` or ``I16``, in any case). A parameter of another shape — a
    ``bool`` or non-``int`` dimension, a non-``str`` type, a tuple of another length — raises ``TypeError``, a value
    out of range ``ValueError``, where the class is defined.

    Type checkers do not understand ``VectorField[1536]``; ``Annotated[list[float] | None, Vector(1536)] = None`` is
    the same field in a form they accept (``Vector``). ``VectorField`` is an annotation: it is never instantiated.
    """

    __slots__ = ()

    def __new__(cls, *args: Any, **kwargs: Any) -> Self:
        raise TypeError("VectorField is an annotation, not a value: write `embedding: VectorField[1536]`.")

    def __class_getitem__(cls, params: int | tuple[int, str]) -> Any:
        if isinstance(params, tuple):
            if len(params) != 2:
                _vf1(params)
            dimension, vector_type = params
        else:
            dimension, vector_type = params, "F32"
        if isinstance(dimension, bool) or not isinstance(dimension, int):
            _vf1(dimension)
        if not isinstance(vector_type, str):
            _vf1(vector_type)
        checked = _checked_marker(dimension, vector_type, subject="VectorField")
        return _ANNOTATED[list[float] | None, Vector(*checked), Field(default=None)]


def _held(annotation: Any, metadata: Sequence[Any] = ()) -> tuple[tuple[Vector, ...], bool]:
    """The distinct ``Vector`` markers that declare a field of *annotation* (with the field's own *metadata*), in order,
    and whether the field also takes a type other than ``None`` next to them.

    The ``Annotated`` layers are unwrapped and their metadata added to *metadata*: when it holds a ``Vector``, those
    markers decide, whatever the type under them (a ``Vector`` validates the value itself). Otherwise ``None`` gives
    neither, and so does a ``Literal`` whose values are all ``None`` (``Literal[None]`` admits ``None`` only, issue
    #327); a union gives the markers and other types of its members, each read the same way; any other type is
    another type — a ``Literal`` holding another value (``Literal[None, "x"]``) and a container holding vectors
    (``list[VectorField[3]]``) included, whose arguments are never read. This is the reading ``model_ddl`` gives the
    column its kind by, so the column and the checks agree.
    """
    while get_origin(annotation) is Annotated:
        metadata = (*metadata, *annotation.__metadata__)
        annotation = annotation.__origin__
    markers = tuple(dict.fromkeys(item for item in metadata if isinstance(item, Vector)))
    if markers:
        return markers, False
    origin = get_origin(annotation)
    if annotation is None or annotation is NoneType or (origin is Literal and all(arg is None for arg in get_args(annotation))):
        return (), False
    if origin is not Union and origin is not UnionType:
        return (), True
    members = [_held(member) for member in get_args(annotation)]
    markers = tuple(dict.fromkeys(marker for held, _ in members for marker in held))
    return markers, any(other for _, other in members)


def ambiguous_marker_refused(subject: str, markers: Sequence[Vector], other: bool) -> str:
    """The refusal of *subject* — a field, or an annotation —, which holds several different *markers*, or a marker and
    a type other than ``None`` (*other*): a vector field of no single dimension and storage type."""
    held = repr(markers[-1]) if len(markers) == 1 else f"{', '.join(map(repr, markers[:-1]))} and {markers[-1]!r}"
    if other:
        held += " next to a type other than None"
    return (
        f"{subject} holds {held}; a vector field holds one Vector marker, and None at most. Write VectorField[d] alone "
        "(it is already optional) or Annotated[list[float] | None, Vector(d)]."
    )


def _marker(annotation_or_field: Any, *, subject: str | None = None) -> Vector | None:
    """The ``Vector`` marker of a ``FieldInfo`` or of an annotation (``_held()``), ``None`` when it holds none.

    Several different markers, or a marker next to a type other than ``None`` in a union, raise ``ValueError`` naming
    *subject* (by default "The field" or "The annotation").
    """
    if isinstance(annotation_or_field, FieldInfo):
        markers, other = _held(annotation_or_field.annotation, annotation_or_field.metadata)
        default = "The field"
    else:
        markers, other = _held(annotation_or_field)
        default = "The annotation"
    if not markers:
        return None
    if len(markers) > 1 or other:
        raise ValueError(ambiguous_marker_refused(subject or default, markers, other))
    return markers[0]


def get_vector_info(annotation_or_field: Any) -> tuple[int, str] | None:
    """The ``(dimension, storage type)`` of a vector field, or ``None`` when it is not one (the full ORM's helper).

    Takes a model field (``Model.model_fields["embedding"]``, a Pydantic ``FieldInfo``) or an annotation
    (``VectorField[3]``, ``Annotated[list[float] | None, Vector(3)]``): both spellings give ``(3, "F32")``, and so do
    their unions with ``None`` (``VectorField[3] | None``, ``Optional[VectorField[3]]``,
    ``Annotated[list[float], Vector(3)] | None``, ``VectorField[3] | Literal[None]``), read as ``define_table()`` reads
    them. Anything without a ``Vector`` marker — ``list[float]``, another field, vectors held in a container such as
    ``list[VectorField[3]]`` — gives ``None``.

    :raises ValueError: the field or annotation holds two different ``Vector`` markers, or a marker next to a type
        other than ``None`` in a union (``VectorField[3] | str``, ``VectorField[3] | Literal[None, "x"]``): its
        dimension and storage type are not one. A model's ``class`` statement refuses such a field already, naming it
        (for a field declared through a forward reference, once Pydantic completes the model).
    """
    marker = _marker(annotation_or_field)
    return None if marker is None else (marker.dimension, marker.vector_type)


def is_vector_field(annotation_or_field: Any) -> bool:
    """Whether a model field or an annotation is a vector field, any spelling — a union with ``None`` or
    ``Literal[None]`` included — (the full ORM's helper). Refuses what ``get_vector_info()`` refuses, with its
    ``ValueError``: a field or an annotation holding two different ``Vector`` markers, or a marker next to a type other
    than ``None``."""
    return _marker(annotation_or_field) is not None


def declared_vectors(owner: str, fields: Mapping[str, FieldInfo]) -> dict[str, tuple[int, str]]:
    """The ``(dimension, storage type)`` of each vector field among a model's *fields*, by field name, as
    ``get_vector_info()`` reads them: what the model's ``class`` statement checks (issue #318), and checks again once
    Pydantic completes a model a forward reference left incomplete there (issue #327).

    A field whose ``Vector`` markers are ambiguous raises ``ValueError`` naming the model *owner* and the field, so the
    ``class`` statement refuses it whether or not the model declares indexes. A field still a forward reference has no
    marker yet: it is not a vector field here.
    """
    declared: dict[str, tuple[int, str]] = {}
    for name, field in fields.items():
        marker = _marker(field, subject=f"{owner}: field {name!r}")
        if marker is not None:
            declared[name] = (marker.dimension, marker.vector_type)
    return declared


# --- the checks -----------------------------------------------------------------------------------------------------


def checked_distance(value: object, *, subject: str) -> str:
    """The distance ``value`` names, as the ORM's own constant from ``DISTANCES``; any case is accepted.

    ``subject`` starts each refusal: ``Index 'name'`` or ``similar_to()``. A non-``str`` raises ``TypeError``;
    ``MINKOWSKI``, ``JACCARD``, ``PEARSON`` and an unknown name raise ``ValueError``, each with its reason.
    """
    if not isinstance(value, str):
        raise TypeError(f"{subject}: dist= must be a str, got {type(value).__name__}.")
    upper = value.upper()
    if upper in DISTANCES:
        return DISTANCES[DISTANCES.index(upper)]
    if upper == "MINKOWSKI":
        raise ValueError(f"{subject}: {_MINKOWSKI}")
    if upper in ("JACCARD", "PEARSON"):
        raise ValueError(f"{subject}: {_NOT_RANKING.format(dist=upper)}")
    raise ValueError(f"{subject}: unknown distance {value!r}; the ORM's are {_DISTANCE_LIST}.")


def checked_vector_type(value: object, *, subject: str) -> str:
    """The storage type ``value`` names, as the ORM's own constant from ``VECTOR_TYPES``; any case is accepted.

    A non-``str`` raises ``TypeError``, another name ``ValueError``; ``subject`` starts each refusal.
    """
    if not isinstance(value, str):
        raise TypeError(f"{subject}: vector_type= must be a str, got {type(value).__name__}.")
    upper = value.upper()
    if upper not in VECTOR_TYPES:
        raise ValueError(f"{subject}: vector_type= must be one of {_VECTOR_TYPE_LIST}, got {value!r}.")
    return VECTOR_TYPES[VECTOR_TYPES.index(upper)]


def checked_count(value: object, *, subject: str, arg: str, low: int, high: int) -> int:
    """``value``, an ``int`` (not a ``bool``) from ``low`` to ``high``: an index's ``dimension``, ``efc`` or ``m``.

    A ``bool`` or a non-``int`` raises ``TypeError``, a value out of range ``ValueError``; ``subject`` starts each
    refusal and ``arg`` names the argument.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{subject}: {arg}= must be an int, got {type(value).__name__}.")
    if not low <= value <= high:
        raise ValueError(f"{subject}: {arg}= must be between {low} and {high}, got {value!r}.")
    return value


def checked_query_vector(value: object, *, subject: str = "similar_to()") -> list[float]:
    """The query vector of ``similar_to()`` (or of *subject*, which starts each refusal) as a ``list[float]``, the
    value the ORM binds.

    A ``list`` or ``tuple`` of real numbers — ``int``, ``float``, any ``numbers.Real``, a ``Decimal``; not a ``bool``
    nor a ``str`` —, not empty, each finite. Another container (a NumPy array: ``.tolist()``) or an item that is not a
    number raises ``TypeError``; an empty vector or an item that is not finite ``ValueError``.
    """
    if not isinstance(value, list | tuple):
        raise TypeError(
            f"{subject} takes the vector as a list or tuple of numbers, got {type(value).__name__}; convert an "
            "array with .tolist()."
        )
    if not value:
        raise ValueError(f"{subject}: the vector is empty.")
    floats: list[float] = []
    for position, item in enumerate(value):
        number = _as_float(item)
        if number is None:
            raise TypeError(f"{subject}: vector item {position} is {type(item).__name__}, not a number.")
        if not math.isfinite(number):
            raise ValueError(f"{subject}: vector item {position} is not finite ({item!r}).")
        floats.append(number)
    return floats


def checked_knn_field(value: object) -> str:
    """The ``field`` of ``similar_to()``: a field name or a dotted path, as ``validate_field_name()`` checks one,
    matched in full (its pattern's ``$`` accepts a trailing newline, which this refuses with the same text).

    A non-``str`` raises ``TypeError``; a ``str`` that is not a field name ``ValueError``.
    """
    if not isinstance(value, str):
        raise TypeError(f"similar_to() takes a field name (str), not {type(value).__name__}.")
    validate_field_name(value, "similar_to field")
    if VALID_FIELD_PATTERN.fullmatch(value) is None:
        raise ValueError(
            f"Invalid similar_to field name '{value}': must contain only alphanumeric characters, underscores, and "
            "dots (for nested fields), and start with a letter or underscore"
        )
    return value


def checked_limit(value: object, *, subject: str = "similar_to()", arg: str = "limit") -> int:
    """The ``limit`` of ``similar_to()`` — ``K``, the number of neighbours —, an ``int`` (not a ``bool``) from 1 to
    ``MAX_K``. Another type raises ``TypeError``, a value out of range ``ValueError``; each refusal reads
    ``{subject} {arg} must be …`` (``hybrid_search() vector_limit``, ``hybrid_search() text_limit``)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{subject} {arg} must be an int, got {type(value).__name__}.")
    if not 1 <= value <= MAX_K:
        raise ValueError(f"{subject} {arg} must be between 1 and {MAX_K}, got {value!r}.")
    return value


def checked_ef(value: object, *, subject: str = "similar_to()") -> int | None:
    """The ``ef`` of ``similar_to()`` — the HNSW search's candidate list —, ``None`` (``DEFAULT_EF``) or an ``int``
    (not a ``bool``) from 1 to ``MAX_EF``. Another type raises ``TypeError``, a value out of range ``ValueError``;
    *subject* starts each refusal."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{subject} ef must be an int or None, got {type(value).__name__}.")
    if not 1 <= value <= MAX_EF:
        raise ValueError(f"{subject} ef must be between 1 and {MAX_EF}, got {value!r}.")
    return value


# --- the renderers --------------------------------------------------------------------------------------------------


def hnsw_clause(dimension: int, dist: str, vector_type: str, efc: int, m: int) -> str:
    """``HNSW DIMENSION <d> DIST <dist> TYPE <type> EFC <efc> M <m>``, the clause of a vector index. Pure.

    Every option is written; the words are the ORM's own constants and the numbers checked ``int``s — anything else
    raises, as the checkers do, instead of reaching the statement.
    """
    subject = "hnsw_clause()"
    low, high = M_RANGE
    written = (
        checked_count(dimension, subject=subject, arg="dimension", low=1, high=MAX_DIMENSION),
        checked_distance(dist, subject=subject),
        checked_vector_type(vector_type, subject=subject),
        checked_count(efc, subject=subject, arg="efc", low=1, high=MAX_EFC),
        checked_count(m, subject=subject, arg="m", low=low, high=high),
    )
    return "HNSW DIMENSION {} DIST {} TYPE {} EFC {} M {}".format(*written)


def knn_condition(column: str, k: int, ef: int | None, dist: str | None, variable: str) -> str:
    """The KNN condition of a query: ``<column> <|K,EF|> $<variable>`` or ``<column> <|K,DIST|> $<variable>``. Pure.

    ``column`` is the field's column, validated by the queryset; ``variable`` the bound query vector's name. Without
    ``dist`` it is an HNSW search whose ``EF`` is ``ef`` (``DEFAULT_EF`` when ``None``) but never below ``k``; with
    ``dist`` an exact search by that distance, and ``ef`` must be ``None``. ``k`` and ``ef`` are checked as
    ``similar_to()`` checks them and ``dist`` is written as the ORM's own constant.
    """
    limit = checked_limit(k)
    candidates = checked_ef(ef)
    if dist is None:
        return f"{column} <|{limit},{max(candidates or DEFAULT_EF, limit)}|> ${variable}"
    if candidates is not None:
        raise ValueError("knn_condition() takes ef or dist, not both.")
    return f"{column} <|{limit},{checked_distance(dist, subject='similar_to()')}|> ${variable}"


def exact_distance(column: str, dist: str, variable: str) -> str:
    """The distance *dist* of *column* from the query vector ``$<variable>``, written as the function SurrealDB's exact
    scan computes it: ``vector::distance::<dist>(<column>, $<variable>)``, ``1 - vector::similarity::cosine(<column>,
    $<variable>)`` for ``COSINE``. Pure.

    On SurrealDB 2.x and the embedded engine the value is ``vector::distance::knn()``'s for the same record: bit for bit
    and of the same type (an ``int`` with ``HAMMING``) on a vector of floats or integers, the same number on one of
    decimals (which may come as a decimal). The call raises on a record whose column is not a vector of the query's
    length: :func:`scored_vector` keeps such records out. ``dist`` is written as the ORM's own constant, ``column`` must
    be a field path and ``variable`` the name the ORM bound the query vector under (``_f<N>``).
    """
    validate_field_name(column, "similar_to field")
    if not variable.startswith("_f") or not variable[2:].isdigit():
        raise ValueError(f"exact_distance() binds the query vector as $_f<N>, not ${variable}.")
    return _DISTANCE_CALLS[checked_distance(dist, subject="similar_to()")].format(column=column, variable=variable)


def scored_vector(column: str, dimension: int) -> str:
    """The condition that *column* is a vector SurrealDB's exact scan scores against a query vector of *dimension*
    numbers: ``type::is::array(<column>) AND array::len(<column>) = <dimension> AND type::is::number(array::min(<column>))
    AND type::is::number(array::max(<column>))``. Pure; for SurrealDB 2.x and the embedded engine only (3.x renamed
    ``type::is::array()``).

    The scan skips, silently, a record whose column is missing, ``NULL``, not an array, an array of another length or
    one holding an item that is not a number; :func:`exact_distance` would raise on it. SurrealDB orders values by their
    type first, numbers between booleans and strings, so an array whose least and greatest items are numbers holds
    numbers only: two passes over the array, where a closure on each item costs several times the scan.
    """
    validate_field_name(column, "similar_to field")
    length = checked_count(dimension, subject="scored_vector()", arg="dimension", low=1, high=MAX_DIMENSION)
    return (
        f"type::is::array({column}) AND array::len({column}) = {length} AND type::is::number(array::min({column})) AND "
        f"type::is::number(array::max({column}))"
    )


def exact_scan_select(table: str, search: "KnnSearch", variable: str, conditions: Sequence[str]) -> str:
    """The ids of the ``K`` nearest records of an exact scan, ranked by distance then id, for SurrealDB 2.x and the
    embedded engine: ``(SELECT id, <distance> AS _knn_distance FROM <table> WHERE <conditions AND> <scored vector> ORDER
    BY _knn_distance, id LIMIT <K>).id``. Pure.

    *search* is the queryset's ``similar_to()`` with ``dist``, its query vector bound as ``$<variable>``; *conditions*
    the other parts of the statement's ``WHERE``, already compiled, each AND-ed before the records are ranked. The
    distance is :func:`exact_distance`'s and only the records the scan scores are ranked (:func:`scored_vector`), so
    without a tie at the ``K``-th place the ids are the ones SurrealDB's scan returns, and with one the tied records
    kept are the first by id, on every call. SurrealDB orders a ``NaN`` distance (a zero vector with ``COSINE``) first,
    as its scan does.
    """
    if search.dist is None:
        raise ValueError("exact_scan_select() ranks an exact scan: the search has no distance.")
    where = " AND ".join([*conditions, scored_vector(search.column, len(search.vector))])
    distance = exact_distance(search.column, search.dist, variable)
    return (
        f"(SELECT id, {distance} AS {DISTANCE_ALIAS} FROM {table} WHERE {where} ORDER BY {DISTANCE_ALIAS}, id "
        f"LIMIT {checked_limit(search.k)}).id"
    )


def id_subquery_part(part: str) -> bool:
    """Whether *part*, one AND-ed condition the queryset wrote for a KNN statement's ``WHERE``, is exactly one of the
    ORM's id subqueries — ``id IN $_sq<N>`` (``filter(id__in=Subquery(...))``) or ``$_sq<N> CONTAINS id`` (its form on a
    view model, v0.32.0). Pure.

    Such a part stays next to the KNN on every line: SurrealDB 2.x answers right with id subqueries AND-ed with a KNN,
    two of them included. Below SurrealDB 3 every other part of the statement's ``WHERE`` goes into the ``LET`` that
    selects the records first: a comparison may be on an indexed column, which SurrealDB 2.x answers wrongly next to a
    KNN, and the ORM cannot know a column's indexes. Anything that is not exactly one of the two forms — ``(id IN
    $_sq0)`` inside a ``Q``, a literal ``id IN $_f0`` — goes there too, which is always right, only slower.
    """
    return _ID_SUBQUERY_PART.fullmatch(part) is not None


# --- the query side -------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KnnSearch:
    """One ``similar_to()`` of a queryset, its arguments checked: what its KNN condition writes and binds.

    ``field`` is the field as the caller gave it (the refusals name it), ``column`` the column it is stored under, the
    one the condition names; ``vector`` the query vector as floats (a ``tuple``, so the value is hashable; it is bound
    as a ``list``); ``k`` the ``limit``; ``ef`` the HNSW search's candidate list as given (``None``: ``DEFAULT_EF``), or
    ``dist`` the distance of an exact scan (one of ``DISTANCES``) — never both.
    """

    field: str
    column: str
    vector: tuple[float, ...]
    k: int
    ef: int | None
    dist: str | None

    def condition(self, variable: str) -> str:
        """The KNN condition, the query vector bound as ``$<variable>`` (``knn_condition()``)."""
        return knn_condition(self.column, self.k, self.ef, self.dist, variable)


def field_refused(field: str, *, endpoint: bool, subject: str = "similar_to()") -> str:
    """The refusal of ``similar_to()`` (or *subject*) on *field*, the record id — or an edge endpoint when
    *endpoint*."""
    what = "an edge endpoint" if endpoint else "the record id"
    return f"{subject} compares vectors: {field!r} is {what}, not a vector field."


def distance_attribute_refused(model: str) -> str:
    """The refusal of ``similar_to()`` on a model (named *model*) that defines ``_knn_distance`` itself: the distance
    would replace it on the instances."""
    return f"similar_to() sets each record's distance as {DISTANCE_ALIAS}, which {model} defines; rename that attribute."


def ef_and_dist_refused(*, subject: str = "similar_to()") -> str:
    """The refusal of ``similar_to()`` (or *subject*) given both ``ef`` and ``dist``: an HNSW search or an exact scan,
    not both."""
    return f"{subject} takes ef= (the HNSW index's search) or dist= (an exact scan without an index), not both."


def dimension_refused(field: str, n: int, d: int, source: str, *, subject: str = "similar_to()") -> str:
    """The refusal of a query vector of *n* numbers for *field*, which holds vectors of *d* as *source* declares
    (``VectorField[d]``, or ``index 'name'``); *subject* starts it."""
    return f"{subject}: the vector has {n} dimensions; field {field!r} holds vectors of {d} ({source})."


def is_dimension_error(error: BaseException) -> bool:
    """Whether *error* is SurrealDB's refusal of a query vector whose length is not its vector index's dimension (a
    ``ServerError``)."""
    return isinstance(error, ServerError) and _DIMENSION_MARKER in str(error)


def dimension_error_refused(fields: Sequence[str], n: int, d: int, *, subject: str = "similar_to()") -> str:
    """The ORM's error for a query vector of *n* numbers that a vector index of dimension *d* refused. *fields*: the
    vector fields of the statement; one is named alone, several as "one of the fields". *subject* starts it."""
    named = ", ".join(repr(field) for field in fields)
    where = f"field {named}" if len(fields) == 1 else f"one of the fields {named}"
    return f"{subject}: the vector has {n} dimensions; the vector index on {where} expects {d}."


def dimension_refusal(
    error: BaseException, fields: Mapping[str, str], *, subject: str = "similar_to()"
) -> SurrealDbError | None:
    """The ORM's error for SurrealDB's refusal *error* of a query vector whose length is not its vector index's
    dimension, not raised; ``None`` for any other error.

    *fields* maps each column the statement searches with a KNN to its field as given (the queryset's own, then its
    subqueries'). The server names neither, so the error names that field, or each of them when there are several. The
    two lengths are read from the server's text, which holds nothing else: the caller raises the error ``from`` it.
    ``None`` too — the server's error relayed — when that text cannot be read or the statement searches no vector.
    *subject* starts the error (``dimension_error_refused()``).
    """
    if not is_dimension_error(error):
        return None
    lengths = _DIMENSION_LENGTHS.search(str(error))
    names = list(dict.fromkeys(fields.values()))
    if lengths is None or not names:
        return None
    return SurrealDbError(dimension_error_refused(names, int(lengths.group(1)), int(lengths.group(2)), subject=subject))


def zero_vector_refused(*, subject: str = "similar_to()") -> str:
    """The refusal of a zero query vector for a ``COSINE`` search, which has no distance to rank by; *subject* starts
    it."""
    return f"{subject}: a zero vector has no COSINE distance (SurrealDB answers NaN); use another vector or distance."


def search_annotation_refused(annotation: str) -> str:
    """The refusal of a search annotation (its class name, *annotation*) on a ``similar_to()`` queryset read below
    SurrealDB 3: there the full-text matches are selected first, in a ``LET``, so the statement that searches the
    vectors holds no match its score, highlight or offsets could read (SurrealDB 2.x answers ``NONE`` for each)."""
    return (
        f"similar_to() cannot be combined with {annotation} on SurrealDB 2.x or the embedded engine: there the ORM "
        "selects the full-text matches first, so the vector search holds no match to score, highlight or locate. Keep "
        "search() as a condition, or use SurrealDB 3.x."
    )


def time_travel_refused() -> str:
    """The refusal of ``similar_to()`` with ``as_of()`` — either way round, or a ``Subquery`` holding ``similar_to()``
    under the outer ``as_of()``: the vector index holds the records' current vectors."""
    return (
        "similar_to() cannot be combined with as_of(): the vector index holds the records' current vectors, so a "
        "time-travel read would pick its neighbours by their vectors now. Run it without as_of()."
    )


def live_refused() -> str:
    """The refusal of ``similar_to()`` in a live query's own condition (``live()``, ``watch()``)."""
    return (
        "A live query cannot honour similar_to(): SurrealDB accepts a LIVE SELECT whose condition holds a KNN operator "
        "but never notifies it."
    )


def live_subquery_refused() -> str:
    """The refusal of a ``Subquery`` holding ``similar_to()`` in a live query's condition."""
    return (
        "A live query cannot use a Subquery that holds similar_to(): the nearest records change with every write, and "
        "SurrealDB 2.x applies a live subquery's other conditions to a KNN wrongly. Read them with similar_to() and "
        "watch them by id."
    )
