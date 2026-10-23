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
from typing import Any, ClassVar, Literal, Self

from pydantic import AliasChoices, Field, field_validator

from . import cache as _cache
from . import encoding as _encoding
from . import routing as _routing
from ._sdk import RecordID, ServerError
from .connection_manager import SurrealDBConnectionManager
from .define_parser import parse_define_table
from .exceptions import SurrealDbError
from .functions import SurrealFunc
from .model_base import BaseSurrealModel, _own_annotation_names, _write_refusals
from .schema import (
    TableClauses,
    read_table_definitions,
    relation_schemafull_refused,
    relation_table_refused,
    relation_tables,
    relation_type_clause,
    table_kind,
    table_statement,
    table_statement_keeping,
)
from .schema_state import TableState
from .transaction import Transaction
from .types import SchemaMode
from .utils import apply_ddl_statements, coerce_endpoint, edge_write_error

_ENDPOINT_FIELDS = frozenset({"in_", "out"})
_ENDPOINT_COLUMNS = frozenset({"in", "out"})

# ``INSERT RELATION`` that refuses an explicit id which already exists, the same way on every line.
# The raw statement does not: SurrealDB 2.7.0 and 3.1.5 silently keep the stored edge
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


async def insert_edges(
    table: str,
    rows: list[dict[str, Any]],
    *,
    tx: Transaction | None,
    manager: type[SurrealDBConnectionManager],
) -> list[dict[str, Any]]:
    """Create edges in ``table`` with ``INSERT RELATION`` and return the rows the server sent back.

    The one write path of every edge creation, so the rules below hold whichever API creates the
    edge. ``rows`` are column-keyed (``in``/``out``, data, and ``id`` when the caller chose one).
    The SDK's ``insert_relation()`` is called here and nowhere else. Outside a transaction it runs
    on *manager*, the edge model's connection (v0.29.0), which the caller resolves.

    - **Statement**: outside a transaction, when no row carries an ``id``, the SDK's own
      ``insert_relation()`` — no duplicate is possible. Otherwise the guarded statement
      (:data:`_GUARDED_INSERT`), so a duplicate explicit id raises on SurrealDB 2.x and 3.x alike,
      through ``client.query()`` or, under ``tx``, ``tx.add()``. The rows are bound as ``$data``.
    - **Errors** outside a transaction: a server refusal becomes
      :func:`~surreal_orm_lite.utils.edge_write_error` of it, the SDK's error as ``__cause__`` —
      :class:`SurrealDbNotFoundError` for a missing endpoint of an ``ENFORCED`` relation
      (``NotFoundError`` on 3.x, a "does not exist" message on 2.x), else :class:`SurrealDbError`: a
      duplicate id ("already exists"), a ``TYPE RELATION IN … OUT …`` mismatch, a ``TYPE NORMAL``
      table. The string ``relate()`` raises the same since v0.33.0. Inside a transaction the
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
        client = await manager.get_client()
        try:
            if any("id" in row for row in rows):
                result = await client.query(_GUARDED_INSERT.format(table=table), _encoding.encode({"data": data}))
            else:
                result = await client.insert_relation(table, _encoding.encode(data))
        except ServerError as exc:
            raise edge_write_error(exc) from exc
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

    **Relation constraint (v0.33.0).** An edge model's table is SurrealDB's ``TYPE RELATION``
    (``get_table_type()`` is ``TableType.RELATION``). The full SurrealDB-ORM's keys declare which
    tables its endpoints may come from and point to, and whether they must exist::

        class Likes(BaseSurrealEdge):
            model_config = SurrealConfigDict(
                table_name="likes",
                relation_in="person",
                relation_out=["post", "book"],
                enforced=True,
            )

    They are checked at the ``class`` statement and read back by :meth:`get_relation_in`,
    :meth:`get_relation_out` and :meth:`is_enforced`; ``table_type=TableType.RELATION`` may be
    written, and any other ``table_type`` is refused on an edge. :meth:`relation_ddl` renders the
    table's ``DEFINE TABLE … TYPE RELATION`` statement and :meth:`define_relation` applies it,
    keeping the table's other clauses; SurrealDB then refuses every edge that breaks it. A model
    without the keys applies an unconstrained ``TYPE RELATION``, which removes a constraint.
    README section 34. The table clauses an edge model declares (v0.34.0: ``schema_mode``,
    ``comment``, ``changefeed``, ``permissions``) follow the type clause in both statements, and
    ``define_table()`` applies the relation statement then the data fields.
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
    def get_relation_in(cls) -> tuple[str, ...]:
        """The tables an edge's ``in`` may come from — ``relation_in`` as a tuple, in declared order (v0.33.0).

        Each name stripped, a repeated name kept (as SurrealDB stores ``IN a | a``); ``()`` for any table
        (``relation_in`` left out, ``None`` or empty). The same shape as ``TableState.relation_in``, so
        ``Likes.get_relation_in() == table_state.relation_in`` compares the model with the database.
        """
        # Checked at the class statement, so this only reads the names.
        return relation_tables(cls.model_config.get("relation_in"), model=cls.__name__, key="relation_in")

    @classmethod
    def get_relation_out(cls) -> tuple[str, ...]:
        """The tables an edge's ``out`` may point to, as :meth:`get_relation_in` reads ``relation_in``; ``()`` for any
        table (v0.33.0)."""
        return relation_tables(cls.model_config.get("relation_out"), model=cls.__name__, key="relation_out")

    @classmethod
    def is_enforced(cls) -> bool:
        """``enforced=True``: SurrealDB refuses an edge whose endpoint record does not exist (v0.33.0).

        ``False`` when ``enforced`` is left out, ``None`` or ``False``.
        """
        return cls.model_config.get("enforced") is True

    @classmethod
    def _table_head(cls) -> str:
        """The type clause of the edge's table statement (v0.34.0): ``TYPE RELATION[ IN …][ OUT …][ ENFORCED]``, from
        :meth:`get_relation_in`, :meth:`get_relation_out` and :meth:`is_enforced` — what :meth:`relation_ddl` writes."""
        return relation_type_clause(cls.get_relation_in(), cls.get_relation_out(), cls.is_enforced())

    @classmethod
    def _guarded_table_statement(
        cls,
        tables: Mapping[str, Any],
        statement: str,
        *,
        overwrite: bool,
        clauses: TableClauses,
        method: Literal["define_table", "define_access", "define_relation"],
    ) -> tuple[str, TableState | None]:
        """The table guard of :meth:`define_relation` and of ``define_table()`` (v0.33.0, shared in v0.34.0): the
        relation statement to apply over what *tables* (``INFO FOR DB``'s map) holds, and the stored definition
        (``None`` for a new table). Pure.

        A new table, or ``overwrite=False`` over a relation table: *statement* (:meth:`relation_ddl`'s with
        *overwrite*). A table that exists with ``overwrite=True``: ``schema.table_statement_keeping()`` of the
        relation's type clause and *clauses* — the clauses the model declares, the stored ones for the rest.

        Raises:
            SurrealDbError: RG1 the table is a materialized view; RG2 its definition cannot be read, or a carried
                clause fails its check; RG3 ``overwrite=False`` over a table that is not a relation table — each
                naming *method*.
        """
        name, table = cls.__name__, cls.get_table_name()
        existing = table_kind(tables, table)
        if existing == "view":
            raise SurrealDbError(relation_table_refused("RG1", name, table, method=method))
        if existing == "unreadable":
            raise SurrealDbError(relation_table_refused("RG2", name, table, method=method))
        if existing is None:
            return statement, None
        state = parse_define_table(tables[table])
        if not overwrite:
            if state.table_type != "relation":
                raise SurrealDbError(relation_table_refused("RG3", name, table, method=method))
            return statement, state
        kept = table_statement_keeping(table, cls._table_head(), clauses, state)
        if kept is None:
            raise SurrealDbError(relation_table_refused("RG2", name, table, method=method))
        return kept, state

    @classmethod
    def relation_ddl(cls, *, overwrite: bool = True) -> str:
        """Render the ``DEFINE TABLE … TYPE RELATION`` statement of this edge model's table (v0.33.0).

        Pure: builds the SurrealQL without touching the database (no connection needed), the same text every time,
        so it can be printed, diffed or fed into a migration::

            DEFINE TABLE {OVERWRITE | IF NOT EXISTS} `<table>` TYPE RELATION[ IN `a` | `b`][ OUT `c`][ ENFORCED]
                [ SCHEMAFULL | SCHEMALESS][ COMMENT '…'][ CHANGEFEED …][ PERMISSIONS …];

        The table of :meth:`get_table_name` and the names of :meth:`get_relation_in` and :meth:`get_relation_out`
        are written in backticks, several joined with ``|`` in declared order; ``IN`` (or ``OUT``) is left out for
        any table, and ``ENFORCED`` when :meth:`is_enforced` is false. A model without keys renders
        ``TYPE RELATION`` alone: a relation table whose edges may come from and point to any table. The table clauses
        the model declares follow (v0.34.0): ``schema_mode`` (:meth:`get_schema_mode`), ``comment``, ``changefeed`` and
        ``permissions`` — none for a model without them.

        Applied by hand, ``OVERWRITE`` resets the table's other clauses — schema mode, permissions, change feed,
        comment, ``DROP`` — to SurrealDB's defaults: ``define_relation()`` keeps those the model does not declare.

        Args:
            overwrite: ``True`` (default) emits ``DEFINE TABLE OVERWRITE …``, which redefines an existing table.
                ``False`` emits ``DEFINE TABLE IF NOT EXISTS …``, which leaves an existing table as it is.

        Raises:
            TypeError: *overwrite* is not a ``bool``.

        Example:
            >>> Likes.relation_ddl()
            'DEFINE TABLE OVERWRITE `likes` TYPE RELATION IN `person` OUT `post` | `book` ENFORCED;'
        """
        return table_statement(cls.get_table_name(), cls._table_head(), cls._declared_table_clauses(), overwrite)

    @classmethod
    async def define_relation(cls, *, overwrite: bool = True, tx: Transaction | None = None) -> str:
        """Apply this edge model's ``TYPE RELATION`` constraint to its table (v0.33.0) and return the statement applied.

        On the model's connection (the ``using()`` block's, else ``SurrealConfigDict(connection=…)``, else
        ``"default"``), sends ``INFO FOR DB`` once — committed state, outside any transaction — then one ``DEFINE
        TABLE`` statement:

        - no such table: :meth:`relation_ddl` (``DEFINE TABLE OVERWRITE `<table>` TYPE RELATION …;``);
        - a table that exists: ``DEFINE TABLE OVERWRITE`` with the model's type clause followed by the table's other
          clauses — ``DROP``, ``SCHEMAFULL``, its comment, its change feed, its permissions —, each the one the model
          declares (v0.34.0: ``schema_mode``, ``comment``, ``changefeed``, ``permissions``), else the stored one,
          re-rendered from ``INFO FOR DB``. A bare ``OVERWRITE`` would reset them to SurrealDB's defaults (``PERMISSIONS
          NONE`` among them); ``ALTER TABLE … TYPE`` is not enforced on SurrealDB 2.x and the embedded engine once the
          table has been written to. This statement is enforced at once, on every connection, the same on every line;
        - ``overwrite=False``: :meth:`relation_ddl` with ``IF NOT EXISTS``, which defines a missing table and leaves a
          relation table as it is, its stored ends included.

        Safe to call at application start-up: with the default ``overwrite=True`` each call converges the table onto
        the model. The table keeps its records, fields, indexes, events, the views that read it (README section 33:
        they keep updating with the edges written afterwards), its registered live queries and its change-feed
        history. On a table with a change feed, each call adds a ``define_table`` entry to the feed: ``changes()``
        skips it, a raw ``SHOW CHANGES`` shows it. SurrealDB rewrites the ``in``/``out`` field definitions from the
        ends (``TYPE record<person>``); an ``ASSERT`` written on them by hand is lost — declare the ends with
        ``relation_in``/``relation_out`` instead.

        **Records already in the table are not checked** — SurrealDB checks only later writes: an edge stored before
        the constraint keeps its endpoints, and updating one that breaks it (``merge()``, ``update()``, …) raises the
        SDK's ``InternalError``. README section 34 gives the queries that find them. A model without keys
        (``relation_in`` and ``relation_out`` left out, ``None`` or empty, ``enforced`` left out, ``None`` or
        ``False``) applies ``TYPE RELATION`` with no ends: this is how a constraint is removed — edges from or to any
        table, and to missing records, are accepted at once, the other clauses kept.

        ``define_relation()`` defines no field. An edge model that declares ``schema_mode=SchemaMode.SCHEMAFULL`` and
        has data fields is refused before any request: its table would refuse (SurrealDB 3.x) or drop (2.x, the
        embedded engine) every edge's data. ``define_table()`` applies the same relation statement, then defines them.

        Before any DDL the guard refuses (nothing is sent but the ``INFO``): a materialized view (an ``OVERWRITE``
        would drop its query); a definition the ORM cannot read back, or a stored value that fails its check (it could
        not keep the other clauses); ``overwrite=False`` on a table that exists and is not a relation table (``IF NOT
        EXISTS`` would leave the constraint unapplied). The guard is a check, then the DDL, not a lock: a clause
        another process changes in between is overwritten with the value read.

        With *tx* the guard still reads committed state before anything is queued, and the statement joins the
        transaction: applied at once in an interactive one (WebSocket, SurrealDB 3.x), at commit in a buffered one
        (HTTP, SurrealDB 2.x, the embedded engine) — where a refusal comes back as the transaction's own error. A
        block that raises applies nothing; an edge that breaks the constraint later in the same block fails the
        block. **Limit:** a definition of the same table queued earlier in the block (``tx.add("DEFINE TABLE …")``)
        is not seen, and is replaced at commit by the statement built from the committed definition — define the
        table's other clauses before the block.

        A refusal names the table and keeps the server's reason, never the statement, which may carry permission
        rules: ``Can't apply relation definition: 'likes' -> <reason>``. Applying needs a session allowed to read the
        definitions and to define (a system user such as root, or a namespace or database ``OWNER``/``EDITOR``).

        Args:
            overwrite: see :meth:`relation_ddl`.
            tx: optional transaction the statement is applied with; the guard reads before anything is queued.

        Raises:
            TypeError: *overwrite* is not a ``bool`` — before any request.
            SurrealDbError: *tx* belongs to another connection, or the model declares
                ``schema_mode=SchemaMode.SCHEMAFULL`` and has data fields — before any request; the table is a
                materialized view, its definition cannot be read back, or ``overwrite=False`` meets a table that is not
                a relation table — before any DDL; the server refused the statement (``Can't apply relation definition:
                …``).
            SurrealDbAuthenticationError: the session may not read the database's definitions or define the table.

        Example:
            >>> await Likes.define_relation()
            'DEFINE TABLE OVERWRITE `likes` TYPE RELATION IN `person` OUT `post` | `book` ENFORCED;'
        """
        _routing.check_transaction(cls, tx, f"{cls.__name__}.define_relation()")
        if cls.model_config.get("schema_mode") is SchemaMode.SCHEMAFULL:
            # RG4: a SCHEMAFULL table without the data fields refuses (3.x) or drops (2.x) every edge's data.
            columns = [column.path[0] for column in cls._table_columns() if len(column.path) == 1]
            if columns:
                raise SurrealDbError(relation_schemafull_refused(cls.__name__, columns))
        statement = cls.relation_ddl(overwrite=overwrite)
        table = cls.get_table_name()
        # Cheap even inside a transaction: get_client() returns the already-connected client.
        client = await cls._connection_manager().get_client()
        tables = await read_table_definitions(client, table, kind="definition", what="relation")
        statement, _ = cls._guarded_table_statement(
            tables, statement, overwrite=overwrite, clauses=cls._declared_table_clauses(), method="define_relation"
        )
        await apply_ddl_statements([statement], client=client, tx=tx, what="relation", classify=True, subjects=[repr(table)])
        return statement

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

    @_cache.invalidates("self", refuse=_write_refusals("save()"))
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
        from the row it returns, which is applied whole (#261: the sub-fields a ``DEFAULT``
        filled too). ``server_values``/``extra_vars`` are not available on an edge:
        ``INSERT RELATION`` cannot evaluate ``SET`` expressions — use ``DEFINE FIELD …
        DEFAULT``/``VALUE`` on the edge table.

        Args:
            tx: Optional transaction; on an interactive one the row comes back at once, on a
                buffered one the edge is written at commit and ``id`` stays ``None``.

        Raises:
            SurrealDbError: the edge was read with ``as_of()`` (a snapshot) — before its table is noted
                on the transaction (#315); ``server_values``/``extra_vars`` given (before any call); a
                duplicate id, a refused or rejected write.
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
            SurrealDbError: always — first for a ``tx`` of another connection than the edge's
                (v0.29.0), as every write does.
        """
        _routing.check_transaction(type(self), tx, f"{type(self).__name__}.upsert()")
        raise SurrealDbError(
            "upsert() is not available on an edge: UPSERT stores a plain record that no graph traversal sees. "
            "Use save()/relate() to create an edge, merge()/update() to change its data."
        )

    async def _do_save(self, tx: Transaction | None = None, keep: Collection[str] = ()) -> tuple[Self, bool]:
        """Create the edge through :func:`insert_edges`. Returns ``(self, True)``.

        The row is :meth:`_write_payload` (column-keyed: ``in``, ``out``, the data; computed
        fields and un-named ``server_fields`` left out, as on any create) plus ``id`` when the
        instance has one. The returned row is applied whole, as a node's ``save()`` applies it
        (#261): ``id``, the server-owned fields and the sub-fields a ``DEFAULT`` filled are read
        back, each field with the type a read gives (nested models stay models).
        """
        _check_edge_id(self)
        row = self._write_payload(keep=keep)
        record_id = self._record_id()
        if record_id is not None:
            row["id"] = record_id
        rows = await insert_edges(self.get_table_name(), [row], tx=tx, manager=type(self)._connection_manager())
        if rows:
            self._apply_record(rows)
        return self, True
