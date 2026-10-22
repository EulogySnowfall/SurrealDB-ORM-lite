"""Vector search (v0.36.0): the vector field type, its typed marker, and the checks and renderers of the KNN search.

``VectorField[d]`` / ``VectorField[d, type]`` is the full SurrealDB-ORM's field type for the vectors SurrealDB's HNSW
indexes hold: an optional ``list[float]`` of exactly ``d`` items, ``None`` by default, stored as ``F32`` unless another
storage type is given (``F64``, ``F32``, ``I64``, ``I32`` or ``I16``, in any case). Type checkers do not understand
``VectorField[1536]`` (a class subscripted with a number), so the field has a second spelling, the typed form
``Annotated[list[float] | None, Vector(1536)] = None``: type checkers read ``list[float] | None`` with a default, and
Pydantic reads the ``Vector`` marker exactly as ``VectorField[1536]``'s. Both spellings are one field: the same
validation, the same column type (``option<array<float, 1536>>``), the same schema. ``get_vector_info()`` and
``is_vector_field()`` — the full ORM's helpers, under their names — read the marker of either spelling from a model
field or an annotation.

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

The module sits low in the package: it imports neither ``search``, ``model_base``, ``query_set`` nor
``connection_manager``, so each of them can import it.
"""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from numbers import Real
from typing import Annotated, Any, NoReturn, Self, TypedDict, get_args, get_origin

from pydantic import Field, GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.fields import FieldInfo
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import core_schema

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


def _marker(annotation_or_field: Any) -> Vector | None:
    """The first ``Vector`` of a ``FieldInfo``'s metadata or of an ``Annotated`` annotation's, else ``None``."""
    if isinstance(annotation_or_field, FieldInfo):
        metadata: Sequence[Any] = annotation_or_field.metadata
    elif get_origin(annotation_or_field) is Annotated:
        metadata = annotation_or_field.__metadata__
    else:
        return None
    for item in metadata:
        if isinstance(item, Vector):
            return item
    if isinstance(annotation_or_field, FieldInfo):
        return None
    return _marker(get_args(annotation_or_field)[0])


def get_vector_info(annotation_or_field: Any) -> tuple[int, str] | None:
    """The ``(dimension, storage type)`` of a vector field, or ``None`` when it is not one (the full ORM's helper).

    Takes a model field (``Model.model_fields["embedding"]``, a Pydantic ``FieldInfo``) or an annotation
    (``VectorField[3]``, ``Annotated[list[float] | None, Vector(3)]``): both spellings give ``(3, "F32")``. Anything
    without a ``Vector`` marker — ``list[float]``, another field — gives ``None``.
    """
    marker = _marker(annotation_or_field)
    return None if marker is None else (marker.dimension, marker.vector_type)


def is_vector_field(annotation_or_field: Any) -> bool:
    """Whether a model field or an annotation is a vector field, either spelling (the full ORM's helper)."""
    return _marker(annotation_or_field) is not None


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


def checked_query_vector(value: object) -> list[float]:
    """The query vector of ``similar_to()`` as a ``list[float]``, the value the ORM binds.

    A ``list`` or ``tuple`` of real numbers — ``int``, ``float``, any ``numbers.Real``, a ``Decimal``; not a ``bool``
    nor a ``str`` —, not empty, each finite. Another container (a NumPy array: ``.tolist()``) or an item that is not a
    number raises ``TypeError``; an empty vector or an item that is not finite ``ValueError``.
    """
    if not isinstance(value, list | tuple):
        raise TypeError(
            f"similar_to() takes the vector as a list or tuple of numbers, got {type(value).__name__}; convert an "
            "array with .tolist()."
        )
    if not value:
        raise ValueError("similar_to(): the vector is empty.")
    floats: list[float] = []
    for position, item in enumerate(value):
        number = _as_float(item)
        if number is None:
            raise TypeError(f"similar_to(): vector item {position} is {type(item).__name__}, not a number.")
        if not math.isfinite(number):
            raise ValueError(f"similar_to(): vector item {position} is not finite ({item!r}).")
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


def checked_limit(value: object) -> int:
    """The ``limit`` of ``similar_to()`` — ``K``, the number of neighbours —, an ``int`` (not a ``bool``) from 1 to
    ``MAX_K``. Another type raises ``TypeError``, a value out of range ``ValueError``."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"similar_to() limit must be an int, got {type(value).__name__}.")
    if not 1 <= value <= MAX_K:
        raise ValueError(f"similar_to() limit must be between 1 and {MAX_K}, got {value!r}.")
    return value


def checked_ef(value: object) -> int | None:
    """The ``ef`` of ``similar_to()`` — the HNSW search's candidate list —, ``None`` (``DEFAULT_EF``) or an ``int``
    (not a ``bool``) from 1 to ``MAX_EF``. Another type raises ``TypeError``, a value out of range ``ValueError``."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"similar_to() ef must be an int or None, got {type(value).__name__}.")
    if not 1 <= value <= MAX_EF:
        raise ValueError(f"similar_to() ef must be between 1 and {MAX_EF}, got {value!r}.")
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


def field_refused(field: str, *, endpoint: bool) -> str:
    """The refusal of ``similar_to()`` on *field*, the record id — or an edge endpoint when *endpoint*."""
    what = "an edge endpoint" if endpoint else "the record id"
    return f"similar_to() compares vectors: {field!r} is {what}, not a vector field."


def distance_attribute_refused(model: str) -> str:
    """The refusal of ``similar_to()`` on a model (named *model*) that defines ``_knn_distance`` itself: the distance
    would replace it on the instances."""
    return f"similar_to() sets each record's distance as {DISTANCE_ALIAS}, which {model} defines; rename that attribute."


def ef_and_dist_refused() -> str:
    """The refusal of ``similar_to()`` given both ``ef`` and ``dist``: an HNSW search or an exact scan, not both."""
    return "similar_to() takes ef= (the HNSW index's search) or dist= (an exact scan without an index), not both."


def dimension_refused(field: str, n: int, d: int, source: str) -> str:
    """The refusal of a query vector of *n* numbers for *field*, which holds vectors of *d* as *source* declares
    (``VectorField[d]``, or ``index 'name'``)."""
    return f"similar_to(): the vector has {n} dimensions; field {field!r} holds vectors of {d} ({source})."


def zero_vector_refused() -> str:
    """The refusal of a zero query vector for a ``COSINE`` search, which has no distance to rank by."""
    return "similar_to(): a zero vector has no COSINE distance (SurrealDB answers NaN); use another vector or distance."


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
