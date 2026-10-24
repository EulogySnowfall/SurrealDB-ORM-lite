"""Hybrid search (v0.37.0): the checks, the refusal texts and the fusion of ``QuerySet.hybrid_search()``.

``hybrid_search()`` ranks a queryset's records by two searches at once — the ``vector_limit`` records nearest to a
query vector (an HNSW search or an exact scan, as ``similar_to()`` runs it) and the ``text_limit`` best full-text
matches of a text (as ``search()`` and ``SearchScore`` find them) — fused by Reciprocal Rank Fusion: each record scores
Σ 1/(``rrf_k`` + rank) over the lists it is in, its rank counted from 1. SurrealDB 3.x fuses the two lists with its
``search::rrf()``; SurrealDB 2.x and the embedded engine have no such function, so the ORM reads both lists and fuses
them itself with ``fuse()``: the same formula, summed in the same order, so the records, scores and order are the same
on every line.

**The order.** The records come back best first. Equal scores keep the vector list's records first, in its order, then
the records only the full-text search found, in its order: ``fuse()`` gives each record a position — its index in the
vector list, else ``vector_limit`` plus its index in the full-text list — and sorts by score, then position, as the
3.x statement's ``ORDER BY rrf_score DESC, _hybrid_pos`` does with the positions it computes.

**The reserved names.** ``RESERVED_COLUMNS`` are the names the statement writes or a result holds: SurrealDB's
``rrf_score``, the result's ``_rrf_score``, ``_knn_distance`` and ``_search_score``, and the ORM's three positions. A
model field stored under one of them is refused before any request (``column_refused()``); a ``select()`` item naming
one, or a dotted path starting with one, is left out of both lists (``reserved()``). A column a record stores under one
of them without the model declaring it never replaces the method's values: each list leaves out the other list's key
(``KNN_OMITTED``, ``TEXT_OMITTED``) — the 3.x statement with ``OMIT``, ``fuse()`` by dropping them from each list's
rows —, so a record holds the distance the vector list computed and the score the full-text list computed, nothing
else under those names.

**Record ids.** The SDK's ``RecordID`` is not hashable: ``fuse()`` keys each record by ``identity()``, the ``repr`` of
its ``id``, equal for equal ids and different for an ``int`` id and a ``str`` id that print alike.

Everything here is pure: no connection, no request. The module imports neither ``query_set``, ``model_base`` nor
``connection_manager``, so the queryset can import it.
"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .utils import VALID_FIELD_PATTERN, validate_field_name
from .vector import DISTANCE_ALIAS

#: The ``vector_limit`` and ``text_limit`` of ``hybrid_search()`` when not given (the full SurrealDB-ORM's defaults).
DEFAULT_LIMIT = 20

#: The ``rrf_k`` of ``hybrid_search()`` when not given: Reciprocal Rank Fusion's usual constant, SurrealDB's default.
DEFAULT_RRF_K = 60

# The ORM's range for rrf_k: from 0, SurrealDB's own minimum, to this, far above the usual 60.
MAX_RRF_K = 10_000

#: The name that starts every refusal of ``hybrid_search()``.
SUBJECT = "hybrid_search()"

#: The key of each record's fused score in a result.
RRF_SCORE = "_rrf_score"

#: The alias of each record's BM25 score in the full-text list, and its key in a result.
SEARCH_SCORE = "_search_score"

#: The key of each record's fused score in the rows ``search::rrf()`` returns, and in those ``fuse()`` returns.
SERVER_SCORE = "rrf_score"

#: The alias of each fused record's position in the 3.x statement, which orders equal scores.
POSITION = "_hybrid_pos"

#: The key of a record's index in the vector list, which the 3.x statement adds to each of its rows.
VECTOR_POSITION = "_hybrid_vec_pos"

#: The key of a record's index in the full-text list, which the 3.x statement adds to each of its rows.
TEXT_POSITION = "_hybrid_text_pos"

#: The names the statement writes or a result holds, in the order the refusal of a model field lists them.
RESERVED_COLUMNS = (SERVER_SCORE, RRF_SCORE, DISTANCE_ALIAS, SEARCH_SCORE, POSITION, VECTOR_POSITION, TEXT_POSITION)

#: The keys the vector list leaves out of its rows: the 3.x statement's ``OMIT``, and what ``fuse()`` drops.
KNN_OMITTED = (SEARCH_SCORE,)

#: The keys the full-text list leaves out of its rows: the 3.x statement's ``OMIT``, and what ``fuse()`` drops.
TEXT_OMITTED = (DISTANCE_ALIAS, VECTOR_POSITION)

#: The queryset clauses ``hybrid_search()`` refuses: it runs its own searches and orders the records itself.
REFUSED_CLAUSES = ("annotate()", "order_by()", "search()", "similar_to()", "values()")


# --- the checks -----------------------------------------------------------------------------------------------------


def checked_field(value: object, arg: str) -> str:
    """``value``, the ``vector_field`` or ``text_field`` (*arg*) of ``hybrid_search()``: a field name or a dotted path,
    as ``validate_field_name()`` checks one, matched in full (its pattern's ``$`` accepts a trailing newline, which
    this refuses with the same text).

    A non-``str`` raises ``TypeError``; a ``str`` that is not a field name ``ValueError``, naming *arg*.
    """
    if not isinstance(value, str):
        raise TypeError(f"{SUBJECT} takes {arg} as a field name (str), not {type(value).__name__}.")
    context = f"hybrid_search {arg}"
    validate_field_name(value, context)
    if VALID_FIELD_PATTERN.fullmatch(value) is None:
        raise ValueError(
            f"Invalid {context} name '{value}': must contain only alphanumeric characters, underscores, and dots (for "
            "nested fields), and start with a letter or underscore"
        )
    return value


def checked_rrf_k(value: object) -> int:
    """The ``rrf_k`` of ``hybrid_search()`` — the constant added to each rank —, an ``int`` (not a ``bool``) from 0 to
    ``MAX_RRF_K``. Another type raises ``TypeError``, a value out of range ``ValueError``."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{SUBJECT} rrf_k must be an int, got {type(value).__name__}.")
    if not 0 <= value <= MAX_RRF_K:
        raise ValueError(f"{SUBJECT} rrf_k must be between 0 and {MAX_RRF_K}, got {value!r}.")
    return value


def reserved(column: str) -> bool:
    """Whether *column*, a ``select()`` item, names one of ``RESERVED_COLUMNS`` — itself or as the first segment of a
    dotted path. Such an item is left out of both lists: its key holds ``hybrid_search()``'s own value, or none."""
    return column.partition(".")[0] in RESERVED_COLUMNS


# --- the texts ------------------------------------------------------------------------------------------------------


def clauses_refused(offenders: Iterable[str]) -> str:
    """The refusal of ``hybrid_search()`` on a queryset holding the clauses *offenders* (names of
    ``REFUSED_CLAUSES``), every one named, sorted."""
    named = ", ".join(sorted(offenders))
    return (
        f"{SUBJECT} cannot be combined with {named}: it runs its own vector and full-text searches and orders the "
        "records by their fused score. Remove them from the queryset."
    )


def time_travel_refused() -> str:
    """The refusal of ``hybrid_search()`` with ``as_of()``: both indexes hold the records' current values."""
    return (
        f"{SUBJECT} cannot be combined with as_of(): the vector and full-text indexes hold the records' current values, "
        "so a time-travel read would rank the records by their values now. Run it without as_of()."
    )


def column_refused(model: str, field: str, column: str) -> str:
    """The refusal of ``hybrid_search()`` on a model (named *model*) whose *field* is stored as *column*, one of
    ``RESERVED_COLUMNS``: the method's own value would replace it."""
    return (
        f"{SUBJECT} cannot read {model}: its field {field!r} is stored as {column}, a name {SUBJECT} uses for its own "
        "values, which would replace it. Store it under another name (Field(alias=...))."
    )


# --- the fusion -----------------------------------------------------------------------------------------------------


def identity(value: object) -> str:
    """The key of a record's ``id`` in the fusion: its ``repr``. The SDK's ``RecordID`` is not hashable; the same id
    always gives the same ``repr``, and an ``int`` id and a ``str`` id that print alike different ones."""
    return repr(value)


def fuse(lists: Sequence[Sequence[Mapping[str, Any]]], *, k: int, vector_limit: int) -> list[dict[str, Any]]:
    """The Reciprocal Rank Fusion of *lists* — ``[vector_rows, text_rows]`` as the server returned them —, the
    ``search::rrf()`` of SurrealDB 3.x computed by the ORM. Pure.

    For each list in order and each row at index ``i`` (rank ``i + 1``), the record its ``id`` names scores
    ``1 / (k + i + 1)``, summed in list order, as SurrealDB sums it. Its row, without the keys that list leaves out
    (``KNN_OMITTED`` from a vector row, ``TEXT_OMITTED`` from a full-text row), is merged into the record's: the later
    list's value wins for a key both rows hold. A record keeps the position it was first met at: ``i`` in the vector
    list, ``vector_limit + i`` in the full-text list.

    Returns new dicts — the input rows are never changed —, each holding the merged row and ``SERVER_SCORE`` set to the
    record's score (replacing a row's own), best first, equal scores by position. A row that is not an object or holds
    no ``id`` is dropped, as SurrealDB drops it; a list beyond the second, or one that is not a list, is ignored.
    """
    scores: dict[str, float] = {}
    positions: dict[str, int] = {}
    merged: dict[str, dict[str, Any]] = {}
    for start, omitted, rows in zip((0, vector_limit), (KNN_OMITTED, TEXT_OMITTED), lists, strict=False):
        if not isinstance(rows, list | tuple):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping) or row.get("id") is None:
                continue
            key = identity(row["id"])
            scores[key] = scores.get(key, 0.0) + 1 / (k + index + 1)
            positions.setdefault(key, start + index)
            merged[key] = {**merged.get(key, {}), **{name: value for name, value in row.items() if name not in omitted}}
    ranked = sorted(merged, key=lambda key: (-scores[key], positions[key]))
    return [{**merged[key], SERVER_SCORE: scores[key]} for key in ranked]
