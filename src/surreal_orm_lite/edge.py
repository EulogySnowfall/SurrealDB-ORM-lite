"""Typed graph edges (v0.22.0): a model whose records are SurrealDB relations.

An edge table (``RELATE``, ``INSERT RELATION``, ``DEFINE TABLE … TYPE RELATION``) stores every
record with two endpoints — the columns ``in`` and ``out``, both record ids — next to whatever
data the edge carries. :class:`BaseSurrealEdge` is the model of such a table: its subclasses
declare the data fields, the base declares the endpoints.

``in`` is a Python keyword, so the field is ``in_``; the column stays ``in``. That rename is an
implicit alias of the v0.18.0 alias map (:meth:`BaseSurrealModel._implicit_aliases`), so every
path that already honours ``Field(alias=…)`` — writes, filters, ``order_by``, ``values``,
hydration, live queries and change feeds — translates it without knowing about edges.

Edges are created through ``INSERT RELATION`` only — never ``CREATE``/``UPSERT``, which store a
plain record no graph traversal sees — by one helper, :func:`insert_edges`.
"""

from collections.abc import Collection, Mapping
from typing import Any, ClassVar, Self

from pydantic import AliasChoices, Field, field_validator

from ._sdk import NotFoundError, RecordID, ServerError
from .connection_manager import SurrealDBConnectionManager
from .exceptions import SurrealDbError, SurrealDbNotFoundError
from .functions import SurrealFunc
from .model_base import BaseSurrealModel, _own_annotation_names
from .transaction import Transaction
from .utils import coerce_endpoint

_ENDPOINT_FIELDS = frozenset({"in_", "out"})
_ENDPOINT_COLUMNS = frozenset({"in", "out"})

# ``INSERT RELATION`` that refuses an explicit id which already exists, the same way on every line
# (spec §2.4). The raw statement does not: SurrealDB 2.7.0 and 3.1.5 silently keep the stored edge
# and return it as if it had been inserted, while 3.3.3 raises. ``$before`` is the stored record
# (``NONE`` for a new one), so the ``THROW`` turns the silent case into an error whose text matches
# 3.3.3's own. It is built server-side from ``$before.id``: no caller text enters the statement. On
# a batch, one duplicate fails the whole statement and nothing is written. ``{table}`` is the only
# interpolation — a ``get_table_name()`` validated as an identifier at class definition; the SurrealQL
# braces are doubled for ``str.format``.
_GUARDED_INSERT = (
    "INSERT RELATION INTO {table} $data "
    "RETURN VALUE IF $before != NONE "
    "{{ THROW 'Database record `' + <string> $before.id + '` already exists' }} "
    "ELSE {{ $after }};"
)


def _check_edge_id(model: BaseSurrealModel) -> None:
    """Refuse, before any call, an edge id the server would misread.

    A ``RecordID`` of another table is silently re-tabled into the edge table by SurrealDB 2.x and
    refused by 3.x; a ``bool`` would become the record id ``true``/``false``.
    """
    value = model.get_id()
    if isinstance(value, bool):
        raise ValueError("a record id cannot be a boolean")
    table = model.get_table_name()
    if isinstance(value, RecordID) and value.table_name != table:
        raise ValueError(
            f"{type(model).__name__}: id {value} belongs to table {value.table_name!r}, not to the edge table {table!r}"
        )


def _as_rows(result: Any) -> list[dict[str, Any]]:
    """Normalise a statement's result to a list of row dicts (``[]`` for ``None``)."""
    if isinstance(result, dict):
        return [result]
    if isinstance(result, list):
        return [row for row in result if isinstance(row, dict)]
    return []


async def insert_edges(table: str, rows: list[dict[str, Any]], *, tx: Transaction | None) -> list[dict[str, Any]]:
    """Create edges in ``table`` with ``INSERT RELATION`` and return the rows the server sent back.

    The one write path of every edge creation, so the rules below hold whichever API creates the
    edge. ``rows`` are column-keyed (``in``/``out``, data, and ``id`` when the caller chose one).
    The SDK's ``insert_relation()`` is called here and nowhere else.

    - **Statement**: outside a transaction, when no row carries an ``id``, the SDK's own
      ``insert_relation()`` — no duplicate is possible. Otherwise the guarded statement
      (:data:`_GUARDED_INSERT`), so a duplicate explicit id raises on SurrealDB 2.x and 3.x alike,
      through ``client.query()`` or, under ``tx``, ``tx.add()``. The rows are bound as ``$data``.
    - **Errors** outside a transaction: a server refusal becomes :class:`SurrealDbNotFoundError`
      for a missing endpoint of an ``ENFORCED`` relation (``NotFoundError`` on 3.x, a "does not
      exist" message on 2.x), else :class:`SurrealDbError` — a duplicate id ("already exists"), a
      ``TYPE RELATION IN … OUT …`` mismatch, a ``TYPE NORMAL`` table. Inside a transaction the
      transaction layer's own errors apply.
    - **Refused writes**: the server answers a create its ``PERMISSIONS`` refuse — or a row this
      user may not select — with fewer rows and no error. When the statement ran (no transaction,
      or an interactive one) and fewer rows came back than were sent, this raises
      :class:`SurrealDbError`; a partly refused batch outside a transaction stays partly written.
    - A **buffered** transaction runs nothing before commit: ``[]``, no count check.
    """
    if not rows:
        return []
    data: dict[str, Any] | list[dict[str, Any]] = rows[0] if len(rows) == 1 else rows
    if tx is not None:
        result = await tx.add(_GUARDED_INSERT.format(table=table), {"data": data})
        if not tx.is_interactive:
            return []
    else:
        client = await SurrealDBConnectionManager.get_client()
        try:
            if any("id" in row for row in rows):
                result = await client.query(_GUARDED_INSERT.format(table=table), {"data": data})
            else:
                result = await client.insert_relation(table, data)
        except ServerError as exc:
            message = f"There was a problem with the database: {exc}"
            if isinstance(exc, NotFoundError) or "does not exist" in str(exc).lower():
                raise SurrealDbNotFoundError(message) from exc
            raise SurrealDbError(message) from exc
    returned = _as_rows(result)
    if len(returned) < len(rows):
        if len(rows) == 1:
            raise SurrealDbError(
                "Can't save data, no record returned: the edge table's PERMISSIONS refused the create, "
                "or do not let this user select the new edge."
            )
        raise SurrealDbError(
            f"Only {len(returned)} of {len(rows)} edges came back: the edge table's PERMISSIONS refused the "
            "others, or do not let this user select them. Outside a transaction this batch is not atomic: "
            "the edges the server accepted stay written."
        )
    return returned


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
    move an edge, delete it and relate again. Every write that would change them on a stored
    edge — ``merge()`` (and its ``server_values=``), ``patch()`` and ``objects().patch()``, the
    ``atomic_*`` helpers, ``objects().bulk_update()``, ``update_or_create(defaults=)`` — raises
    ``ValueError`` before any call; ``update()`` sends them unchanged. ``upsert()`` is refused
    (:class:`SurrealDbError`): it would store a plain record.

    A subclass may not redeclare ``in_`` or ``out`` (``TypeError`` at the ``class``
    statement): the redeclared field would lose both the column mapping and the immutability.
    ``edge.model_copy(update={"out": …})`` is **not** checked by
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

    async def save(
        self,
        tx: Transaction | None = None,
        server_values: Mapping[str, SurrealFunc] | None = None,
        extra_vars: Mapping[str, Any] | None = None,
    ) -> Self:
        """Create this edge with ``INSERT RELATION`` — a traversable relation, on SurrealDB 2.x and 3.x.

        Without an id, through the SDK's ``insert_relation()``; ``edge.id`` then holds the
        server's ``RecordID``. With an explicit id (a string, or a ``RecordID`` of this edge
        table), through a guarded statement: an id that already exists raises
        :class:`SurrealDbError` ("already exists") on every line — a raw ``INSERT RELATION``
        silently keeps the stored edge on SurrealDB 2.x — and the stored edge is unchanged. A
        ``RecordID`` of another table raises ``ValueError`` before any call.

        Same signal envelope as a node's ``save()`` (``pre_save``/``around_save``/``post_save``,
        ``created=True``; ``post_save`` deferred to commit under ``tx``). The write raises — and
        ``post_save`` does not fire — when the server refuses it: :class:`SurrealDbNotFoundError`
        for a missing endpoint of an ``ENFORCED`` relation, :class:`SurrealDbError` for any other
        refusal, including a create the edge table's ``PERMISSIONS`` deny (the server answers
        that one with no row and no error, so the ORM reports it). An edge is never returned with
        ``id`` ``None`` outside a buffered transaction.

        ``server_fields`` and computed fields are left to the server on a create and read back
        from the row it returns. ``server_values``/``extra_vars`` are not available on an edge:
        ``INSERT RELATION`` cannot evaluate ``SET`` expressions — use ``DEFINE FIELD …
        DEFAULT``/``VALUE`` on the edge table.

        Args:
            tx: Optional transaction; on an interactive one the row comes back at once, on a
                buffered one the edge is written at commit and ``id`` stays ``None``.

        Raises:
            SurrealDbError: ``server_values``/``extra_vars`` given (before any call), a duplicate
                id, a refused or rejected write.
            SurrealDbNotFoundError: an ``ENFORCED`` endpoint that does not exist.
            ValueError: an id that is a ``RecordID`` of another table, or a ``bool``.
        """
        if server_values or extra_vars:
            raise SurrealDbError(
                "save(server_values=) is not available on an edge: INSERT RELATION cannot evaluate SET "
                "expressions. Use DEFINE FIELD … DEFAULT/VALUE on the edge table."
            )
        return await super().save(tx=tx)

    async def upsert(self, tx: Transaction | None = None) -> Self:
        """Not available on an edge: always raises :class:`SurrealDbError`, before any call or signal.

        ``UPSERT`` (and ``CREATE``) on an edge table store a plain record carrying ``in``/``out``
        fields, which no graph traversal sees — on SurrealDB 2.x and 3.x alike. Create an edge
        with :meth:`save` or ``relate(EdgeModel, …)``; change its data with ``merge()`` or
        ``update()``.

        Raises:
            SurrealDbError: always.
        """
        raise SurrealDbError(
            "upsert() is not available on an edge: UPSERT stores a plain record that no graph traversal sees. "
            "Use save()/relate() to create an edge, merge()/update() to change its data."
        )

    async def _do_save(self, tx: Transaction | None = None, keep: Collection[str] = ()) -> tuple[Self, bool]:
        """Create the edge through :func:`insert_edges`. Returns ``(self, True)``.

        The row is :meth:`_write_payload` (column-keyed: ``in``, ``out``, the data; computed
        fields and un-named ``server_fields`` left out, as on any create) plus ``id`` when the
        instance has one. Only ``id`` and the server-owned fields are read back from the returned
        row: the data the caller sent is not re-applied, so nested models stay models.
        """
        _check_edge_id(self)
        row = self._write_payload(keep=keep)
        record_id = self._record_id()
        if record_id is not None:
            row["id"] = record_id
        rows = await insert_edges(self.get_table_name(), [row], tx=tx)
        if rows:
            self._apply_record(rows, only={"id", *self.get_server_fields()})
        return self, True
