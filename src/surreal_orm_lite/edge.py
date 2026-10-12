"""Typed graph edges (v0.22.0): a model whose records are SurrealDB relations.

An edge table (``RELATE``, ``INSERT RELATION``, ``DEFINE TABLE … TYPE RELATION``) stores every
record with two endpoints — the columns ``in`` and ``out``, both record ids — next to whatever
data the edge carries. :class:`BaseSurrealEdge` is the model of such a table: its subclasses
declare the data fields, the base declares the endpoints.

``in`` is a Python keyword, so the field is ``in_``; the column stays ``in``. That rename is an
implicit alias of the v0.18.0 alias map (:meth:`BaseSurrealModel._implicit_aliases`), so every
path that already honours ``Field(alias=…)`` — writes, filters, ``order_by``, ``values``,
hydration, live queries and change feeds — translates it without knowing about edges.
"""

from typing import Any, ClassVar

from pydantic import AliasChoices, Field, field_validator

from ._sdk import RecordID
from .model_base import BaseSurrealModel, _own_annotation_names
from .utils import coerce_endpoint

_ENDPOINT_FIELDS = frozenset({"in_", "out"})
_ENDPOINT_COLUMNS = frozenset({"in", "out"})


class BaseSurrealEdge(BaseSurrealModel):
    """Base class for a model of a SurrealDB edge (relation) table.

    Example::

        class Purchased(BaseSurrealEdge):
            model_config = SurrealConfigDict(table_name="purchased")

            quantity: int = Field(ge=1)
            price: float

        edge = Purchased(in_=RecordID("User", "alice"), out=RecordID("Product", "w"), quantity=2, price=9.5)
        edge.in_   # RecordID("User", "alice") — stored in the column "in"

    **Endpoints.** ``in_`` (column ``in``) and ``out`` are declared here and hold a
    ``RecordID``. Each accepts a ``RecordID``, a saved model instance (its record id) or a
    ``"table:id"`` string — the id read as a string, one surrounding pair of backticks removed
    (``"user:`a-b`"``). Anything else, an unsaved instance included, is a Pydantic
    ``ValidationError``. An id with other characters (``joe@x.com``) or an integer id: pass the
    instance or a ``RecordID``. A row keyed ``in`` validates too
    (``Purchased.model_validate({"in": alice, "out": widget, …})``, the type-checker-clean form
    when the endpoints are model instances).

    **Endpoints are immutable.** Assigning ``edge.in_``/``edge.out`` raises
    ``ValidationError``; SurrealDB ignores a change to an edge's endpoints on both lines, so to
    move an edge, delete it and relate again. A subclass may not redeclare ``in_`` or ``out``
    (``TypeError`` at the ``class`` statement): the redeclared field would lose both the column
    mapping and the immutability. ``edge.model_copy(update={"out": …})`` is **not** checked by
    Pydantic — legitimate for a copy meant as a *new* edge
    (``model_copy(update={"id": None, "out": other})``); on a copy that kept the id, the
    endpoint change is one the server silently ignores.

    ``id`` keeps the ``RecordID`` a row comes back with, or takes a plain string id.
    """

    __surreal_edge__: ClassVar[bool] = True

    id: str | RecordID | None = None
    in_: RecordID = Field(validation_alias=AliasChoices("in", "in_"), frozen=True)
    out: RecordID = Field(frozen=True)

    @field_validator("in_", "out", mode="before")
    @classmethod
    def _coerce_endpoint(cls, value: Any) -> RecordID:
        """Read an endpoint through the one rule shared with the endpoint filters.

        Runs before the SDK's ``RecordID`` schema, which refuses a model instance and raises its
        own ``InvalidRecordIdError`` (not a ``ValueError``, so not a ``ValidationError``) for a
        string without a colon. :func:`~surreal_orm_lite.utils.coerce_endpoint` raises
        ``ValueError`` only, which Pydantic reports as a ``ValidationError``.
        """
        return coerce_endpoint(value)

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        """Refuse a subclass that redeclares an endpoint.

        Reads the class body's own annotations through ``_own_annotation_names()``, which stays
        correct under Python 3.14's lazily evaluated annotations (PEP 649).
        """
        super().__pydantic_init_subclass__(**kwargs)
        if _ENDPOINT_FIELDS & _own_annotation_names(cls):
            raise TypeError(
                f"{cls.__name__}: 'in_'/'out' are the endpoints declared by BaseSurrealEdge and cannot be "
                "redeclared (a redeclared endpoint loses its column mapping and its immutability)."
            )

    @classmethod
    def _implicit_aliases(cls) -> dict[str, str]:
        """``in_`` is stored in the column ``in``."""
        return {"in_": "in"}

    @classmethod
    def _endpoint_columns(cls) -> frozenset[str]:
        """``in``/``out`` hold record ids: the filter builder coerces ``"table:id"`` strings there."""
        return _ENDPOINT_COLUMNS

    @classmethod
    def _write_protected_fields(cls) -> frozenset[str]:
        """``in_``/``out``: SurrealDB ignores a change to an existing edge's endpoints."""
        return _ENDPOINT_FIELDS
