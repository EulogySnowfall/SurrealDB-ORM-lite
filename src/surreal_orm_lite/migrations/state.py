"""The schema states of the migration system (v0.38.0) — private.

How a definition enters or leaves a state, following SurrealDB's DDL semantics (spec §6.2, D31 as amended; M28–M31):

- :func:`place` — a definition replaces the one of the same name, whether it existed or not (``DEFINE … OVERWRITE``
  defines or replaces). A table definition keeps the table's fields, indexes and events, as ``DEFINE TABLE OVERWRITE``
  does (M29); a field, an index or an event on a table the state does not hold first places SurrealDB's implicit table,
  ``TYPE ANY SCHEMALESS`` (M28).
- :func:`take_out` — a table goes with its fields, indexes and events; a field goes alone, its children stay (M30); a
  definition the state does not hold leaves the state as it is, as ``REMOVE … IF EXISTS`` does (M31).

Both are pure: they build a new state with ``dataclasses.replace``, every other part of it (``connection``,
``other``, ``issues``) kept. ``Operation.state_forwards()`` and ``state_backwards()`` go through them.

Imports ``schema_state`` only: ``operations`` imports this module, so a later addition that needs an operation class
imports it inside the function (or ``from . import operations``), never ``from .operations import …`` at module level.
"""

from collections.abc import Mapping
from dataclasses import replace
from typing import Literal, TypeVar

from ..schema_state import (
    AccessState,
    AnalyzerState,
    EventState,
    FieldState,
    FunctionState,
    IndexState,
    SchemaState,
    TableState,
)

__all__ = ["place", "take_out"]

#: A definition :func:`place` puts into a state.
Definition = TableState | FieldState | IndexState | AnalyzerState | EventState | AccessState | FunctionState
#: The kinds of definition :func:`take_out` removes.
Kind = Literal["table", "field", "index", "event", "analyzer", "access", "function"]

_D = TypeVar("_D")


def _implicit_table(name: str) -> TableState:
    """The table SurrealDB creates for a field, an index or an event defined on a table that does not exist: ``TYPE
    ANY SCHEMALESS PERMISSIONS NONE`` (M28; no rule is ``NONE``, the comparator's rule)."""
    return TableState(name, schema_mode="SCHEMALESS", table_type="any")


def place(state: SchemaState, definition: Definition) -> SchemaState:
    """*state* with *definition* in it, replacing the definition of the same name — pure.

    A ``TableState`` replaces the table's clauses and keeps its fields, indexes, events and ``other`` (M29); a missing
    table is added. A ``FieldState`` (keyed by its canonical name), an ``IndexState`` or an ``EventState`` goes into the
    table its ``table`` names — SurrealDB's implicit ``TYPE ANY SCHEMALESS`` table first when *state* does not hold it
    (M28) —; a field's children are not touched. An ``AnalyzerState``, an ``AccessState`` or a ``FunctionState``
    (keyed ``fn::…``) replaces the one of the same name.

    :raises ValueError: a field, an index or an event whose ``table`` is empty (a hand-built state; ``read_own()``
        always sets it).
    """
    if isinstance(definition, TableState):
        stored = state.tables.get(definition.name)
        if stored is not None:
            definition = replace(
                definition, fields=stored.fields, indexes=stored.indexes, events=stored.events, other=stored.other
            )
        return replace(state, tables={**state.tables, definition.name: definition})
    if isinstance(definition, FieldState | IndexState | EventState):
        if not definition.table:
            raise ValueError(f"place(): the {type(definition).__name__} {definition.name!r} names no table.")
        table = state.tables.get(definition.table) or _implicit_table(definition.table)
        if isinstance(definition, FieldState):
            table = replace(table, fields={**table.fields, definition.name: definition})
        elif isinstance(definition, IndexState):
            table = replace(table, indexes={**table.indexes, definition.name: definition})
        else:
            table = replace(table, events={**table.events, definition.name: definition})
        return replace(state, tables={**state.tables, table.name: table})
    if isinstance(definition, AnalyzerState):
        return replace(state, analyzers={**state.analyzers, definition.name: definition})
    if isinstance(definition, AccessState):
        return replace(state, accesses={**state.accesses, definition.name: definition})
    return replace(state, functions={**state.functions, definition.name: definition})


def take_out(state: SchemaState, kind: Kind, name: str, table: str | None = None) -> SchemaState:
    """*state* without the definition *kind* *name* (on *table* for a field, an index or an event) — pure.

    A table goes with its fields, indexes and events; a field goes alone, its children (``<name>.…``) stay (M30). A
    definition *state* does not hold — on a table it does not hold included — leaves *state* as it is (the
    ``REMOVE … IF EXISTS`` semantics; whether SurrealDB would refuse a plain removal is the strict replay's question).
    A function is named ``fn::…``.

    :raises ValueError: an unknown *kind*; a field, an index or an event without *table*.
    """
    if kind == "table":
        if name not in state.tables:
            return state
        return replace(state, tables={key: value for key, value in state.tables.items() if key != name})
    if kind in ("field", "index", "event"):
        if table is None:
            raise ValueError(f"take_out(): a {kind} needs table=.")
        stored = state.tables.get(table)
        if stored is None:
            return state
        if kind == "field":
            if name not in stored.fields:
                return state
            changed = replace(stored, fields=_without(stored.fields, name))
        elif kind == "index":
            if name not in stored.indexes:
                return state
            changed = replace(stored, indexes=_without(stored.indexes, name))
        else:
            if name not in stored.events:
                return state
            changed = replace(stored, events=_without(stored.events, name))
        return replace(state, tables={**state.tables, table: changed})
    if kind == "analyzer":
        return state if name not in state.analyzers else replace(state, analyzers=_without(state.analyzers, name))
    if kind == "access":
        return state if name not in state.accesses else replace(state, accesses=_without(state.accesses, name))
    if kind == "function":
        return state if name not in state.functions else replace(state, functions=_without(state.functions, name))
    raise ValueError(f"take_out(): unknown kind {kind!r}.")


def _without(definitions: Mapping[str, _D], name: str) -> dict[str, _D]:
    """*definitions* without *name*."""
    return {key: value for key, value in definitions.items() if key != name}
