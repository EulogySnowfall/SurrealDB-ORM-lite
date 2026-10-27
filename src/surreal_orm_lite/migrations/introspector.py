"""The schema state of models (v0.38.0): ``ModelIntrospector`` and ``introspect_models()``, the full SurrealDB-ORM's.

A model's state is what its appliers write on a new table, read back by the parser that reads the database
(``render.read_own()``, M17), so a models' state and a database's compare clause by clause: the table statement and
fields of ``table_ddl()`` (a view: its view statement; an edge: its relation statement and data fields), for an
``AuthenticatedUserMixin`` model the table statement of ``access_ddl()`` — its permissions — with ``table_ddl()``'s
fields, the analyzers and indexes of ``index_ddl(line=3)``, ``event_ddl()``, and an auth model's access as
``schema.access_statement(model._access_parts(), clause="")`` (the ``id = $<key>`` item of a primary-key model among
its ``signup_fields``).

The state carries the **marks** the diff reads from its target (spec §5.9, D28): ``TableState.undeclared`` — ``drop``
always, each table clause the model leaves to the database, ``table_type`` for a user model whose access statement
writes no ``TYPE`` —, ``FieldState.undeclared`` — the clauses no model can declare on every field, ``default`` without
a ``DEFAULT``, ``value`` without a ``VALUE`` — and ``FieldState.server`` on the ``server_fields`` columns. So a diff
against models targets what ``define_table()`` writes over an existing definition, and keeps what the models leave to
the database.

Imports ``model_base`` and ``model_auth`` at module level: importing any ``surreal_orm_lite.migrations`` module runs
``surreal_orm_lite/__init__.py`` first, so both are fully initialised (spec §5.1).
"""

from collections.abc import Collection, Iterable
from dataclasses import replace
from typing import TypeVar

from ..model_auth import AuthenticatedUserMixin
from ..model_base import BaseSurrealModel, get_registered_models
from ..schema import TableClauses, access_statement
from ..schema_state import AccessState, AnalyzerState, EventState, FieldState, IndexState, SchemaState, TableState
from . import render

__all__ = ["ModelIntrospector", "introspect_models"]

#: The field clauses no model declares (F8), in every field's ``undeclared``.
_NOT_DECLARABLE = frozenset(
    {"flexible", "reference", "default_always", "readonly", "computed", "assertion", "permissions", "comment"}
)
#: The table clauses a model may declare, the keys of ``TableClauses``.
_TABLE_CLAUSES = ("schema_mode", "comment", "changefeed", "permissions")

_S = TypeVar("_S", TableState, FieldState, IndexState, EventState, AccessState)
_D = TypeVar("_D", TableState, AnalyzerState, AccessState)


class ModelIntrospector:
    """Builds the schema state the models describe (the full ORM's class).

    *models*: the model classes, any iterable of them, kept in ``models`` as a list; ``None`` (the default) reads
    :func:`~surreal_orm_lite.get_registered_models` once, when the introspector is built. *connection*: keeps the
    models whose ``get_connection_name()`` is that name — read when the introspector is built, so a ``using()`` block
    active then counts —; ``None`` keeps them all. The state's ``connection`` is *connection*, ``"default"`` when
    ``None``.

    :raises TypeError: *models* is a ``str`` or holds something that is not a ``BaseSurrealModel`` subclass;
        *connection* is neither a ``str`` nor ``None``.
    """

    models: list[type[BaseSurrealModel]]

    def __init__(self, models: Iterable[type[BaseSurrealModel]] | None = None, *, connection: str | None = None) -> None:
        if connection is not None and not isinstance(connection, str):
            raise TypeError(
                f"ModelIntrospector: connection= takes a connection name (str) or None, not {type(connection).__name__}."
            )
        listed = get_registered_models() if models is None else _checked(models)
        if connection is not None:
            listed = [model for model in listed if model.get_connection_name() == connection]
        self.models = listed
        self._connection = "default" if connection is None else connection

    def introspect(self) -> SchemaState:
        """The state of :attr:`models` — pure: no connection is used.

        Each model's table, with its fields, indexes and events, its analyzers and an auth model's access, read back
        from the statements its appliers write, with the marks (module docstring). Two models may declare the same
        table, analyzer or access when their definitions are equal (marks included).

        :raises ValueError: two models declare one table, or one access, with different definitions; one analyzer
            name is declared with two definitions.
        :raises TypeError: a model's own refusal (``table_ddl()``'s, ``access_ddl()``'s, which raise ``ValueError``
            too).
        """
        tables: dict[str, TableState] = {}
        analyzers: dict[str, AnalyzerState] = {}
        accesses: dict[str, AccessState] = {}
        owners: dict[tuple[str, str], type[BaseSurrealModel]] = {}
        for model in self.models:
            table, model_analyzers, access = _model_state(model)
            _add(tables, owners, model, table, "table")
            for analyzer in model_analyzers:
                _add(analyzers, owners, model, analyzer, "analyzer")
            if access is not None:
                _add(accesses, owners, model, access, "access")
        return SchemaState(tables, analyzers=analyzers, accesses=accesses, connection=self._connection)


def introspect_models(models: Iterable[type[BaseSurrealModel]] | None = None, *, connection: str | None = None) -> SchemaState:
    """The schema state *models* describe (the full ORM's function): ``ModelIntrospector(models,
    connection=connection).introspect()`` — every model of :func:`~surreal_orm_lite.get_registered_models` when
    *models* is ``None``. Pure.

    :raises TypeError: see :class:`ModelIntrospector`; a model's own refusal.
    :raises ValueError: see :meth:`ModelIntrospector.introspect`; a model's own refusal.
    """
    return ModelIntrospector(models, connection=connection).introspect()


def _checked(models: Iterable[type[BaseSurrealModel]]) -> list[type[BaseSurrealModel]]:
    """*models* as a list of model classes; ``TypeError`` for a ``str`` or an item that is not one."""
    if isinstance(models, str):
        raise TypeError("ModelIntrospector: models= takes an iterable of model classes, not a str.")
    listed = list(models)
    for model in listed:
        if not (isinstance(model, type) and issubclass(model, BaseSurrealModel)):
            shown = model.__name__ if isinstance(model, type) else type(model).__name__
            raise TypeError(f"ModelIntrospector: models= holds {shown}, not a BaseSurrealModel subclass.")
    return listed


def _add(
    definitions: dict[str, _D],
    owners: dict[tuple[str, str], type[BaseSurrealModel]],
    model: type[BaseSurrealModel],
    definition: _D,
    kind: str,
) -> None:
    """Put *definition*, declared by *model*, into *definitions*; one already there under its name must be equal."""
    name = definition.name
    existing = definitions.get(name)
    if existing is None:
        definitions[name] = definition
        owners[(kind, name)] = model
        return
    if existing == definition:
        return
    first, second = owners[(kind, name)].__name__, model.__name__
    if kind == "analyzer":
        raise ValueError(
            f"Analyzer {name!r} is declared twice with different definitions (models {first} and {second}); declare it "
            "once and reference that object."
        )
    raise ValueError(
        f"Models {first} and {second} both declare {kind} {name!r} with different definitions; pass one of them in models=."
    )


def _bug(model: type[BaseSurrealModel], what: str) -> RuntimeError:
    return RuntimeError(
        f"ModelIntrospector: a statement {model.__name__} writes for its {what} defines something else; this is a bug "
        "of surreal_orm_lite."
    )


def _read(model: type[BaseSurrealModel], statement: str, kind: type[_S]) -> _S:
    """The state of *statement*, one of *model*'s, which must be a *kind*: ``RuntimeError`` otherwise (a bug)."""
    state = render.read_own(statement)
    if not isinstance(state, kind):
        raise _bug(model, kind.__name__)
    return state


def _model_state(model: type[BaseSurrealModel]) -> tuple[TableState, list[AnalyzerState], AccessState | None]:
    """*model*'s table (with its fields, indexes, events and marks), its analyzers and its access."""
    ddl = model.table_ddl()
    access: AccessState | None = None
    if issubclass(model, AuthenticatedUserMixin):
        table = _read(model, model.access_ddl()[0], TableState)
        access = _read(model, access_statement(model._access_parts(), clause=""), AccessState)
        schemafull = model._schemafull_access()
        clauses = model._access_table_clauses() if schemafull else model._declared_table_clauses()
        # Its access_ddl() table statement writes no TYPE unless the model declares one (v0.17.0).
        untyped = not schemafull and model._auth_setting("table_type", None) is None
    else:
        table = _read(model, ddl[0], TableState)
        clauses = model._declared_table_clauses()
        untyped = False
    fields = [_read(model, statement, FieldState) for statement in ddl[1:]]
    server = {column.path for column in model._table_columns() if column.server} if fields else set[tuple[str, ...]]()
    analyzers: list[AnalyzerState] = []
    indexes: dict[str, IndexState] = {}
    for statement in model.index_ddl(line=3):
        defined = render.read_own(statement)
        if isinstance(defined, AnalyzerState):
            analyzers.append(defined)
        elif isinstance(defined, IndexState):
            indexes[defined.name] = defined
        else:
            raise _bug(model, "IndexState")
    events = [_read(model, statement, EventState) for statement in model.event_ddl()]
    table = replace(
        table,
        fields={field.name: _field_marks(field, server) for field in fields},
        indexes=indexes,
        events={event.name: event for event in events},
        undeclared=_table_marks(clauses, untyped=untyped),
    )
    return table, analyzers, access


def _table_marks(clauses: TableClauses, *, untyped: bool) -> frozenset[str]:
    """``TableState.undeclared`` of a model whose table statement writes *clauses*: ``drop``, each clause left
    ``None``, and ``table_type`` when *untyped*."""
    keys = {"drop", *(key for key in _TABLE_CLAUSES if getattr(clauses, key) is None)}
    if untyped:
        keys.add("table_type")
    return frozenset(keys)


def _field_marks(field: FieldState, server: Collection[tuple[str, ...]]) -> FieldState:
    """*field* with its marks: the clauses no model declares, ``default``/``value`` when its statement writes none,
    ``server`` when its path is among *server*."""
    keys = set(_NOT_DECLARABLE)
    if field.default is None:
        keys.add("default")
    if field.value is None:
        keys.add("value")
    return replace(field, server=tuple(field.path) in server, undeclared=frozenset(keys))
