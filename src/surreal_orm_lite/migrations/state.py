"""The schema states of the migration system (v0.38.0) — private.

How a definition enters or leaves a state, following SurrealDB's DDL semantics (spec §6.2, D31 as amended; M28–M31):

- :func:`place` — a definition replaces the one of the same name, whether it existed or not (``DEFINE … OVERWRITE``
  defines or replaces). A table definition keeps the table's fields, indexes and events, as ``DEFINE TABLE OVERWRITE``
  does (M29); a field, an index or an event on a table the state does not hold first places SurrealDB's implicit table,
  ``TYPE ANY SCHEMALESS`` (M28).
- :func:`take_out` — a table goes with its fields, indexes and events; a field goes alone, its children stay (M30); a
  definition the state does not hold leaves the state as it is, as ``REMOVE … IF EXISTS`` does (M31).

Both are pure: they build a new state with ``dataclasses.replace``, every other part of it (``connection``,
``other``, ``issues``, ``applied_migrations``) kept. ``Operation.state_forwards()`` and ``state_backwards()`` go
through them.

The replays (spec §6.3, C9): :func:`advance` applies a migration's operations to a state, :func:`replay` a list of
migrations, :func:`rewind` undoes one. ``strict`` has no default — every caller says which replay it runs:
``strict=True`` for a replay anchored on a database read (``migrate()``, ``makemigrations(from_db=True)``), where the
removal of a definition the state does not hold raises E-R1 (SurrealDB would refuse it), unless an issue of the read
could hide that definition (D36); ``strict=False`` for a file replay, where such a removal is a no-op. A ``RawSQL``'s
``state_operations`` are never checked (D37).

The diff (spec §5.7): :func:`diff` gives the operations that turn a state into a target, comparing definitions with
``compare``'s keys and each one with its **effective** target (:func:`effective`), the target's definition with the
clauses its marks leave to the database taken from the current one — what ``define_table()`` writes over it.

Imports ``schema_state``, ``define_parser``, ``model_ddl``, ``constants`` and ``compare`` at module level:
``operations`` imports this module, so a function that needs an operation class imports ``operations`` inside it,
never ``from .operations import …`` at module level.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal, TypeVar, overload

from ..define_parser import DefineParseError, KName, normalise_kind, parse_kind, render_kind
from ..model_ddl import holds_object
from ..schema_state import (
    AccessState,
    AnalyzerState,
    EventState,
    FieldState,
    FunctionState,
    IndexState,
    SchemaIssue,
    SchemaState,
    TableState,
)
from . import compare
from .constants import MIGRATIONS_TABLE

if TYPE_CHECKING:
    from .migration import Migration
    from .operations import Operation

__all__ = ["advance", "diff", "effective", "place", "replay", "rewind", "take_out"]

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


# --- the effective target ----------------------------------------------------------------------------------------------


@overload
def effective(current: TableState, target: TableState) -> TableState: ...


@overload
def effective(current: FieldState, target: FieldState) -> FieldState: ...


def effective(current: TableState | FieldState, target: TableState | FieldState) -> TableState | FieldState:
    """*target* as ``define_table()`` writes it over the existing definition *current* (spec §5.7, D12, D28) — pure.

    Read from *target*'s marks only; *target* itself when it carries none. A table: each key of
    ``target.undeclared`` takes *current*'s value (``changefeed`` with ``INCLUDE ORIGINAL``, ``table_type`` with the
    relation ends its ``TYPE`` clause writes). A field: each key of ``target.undeclared`` among ``assertion``,
    ``readonly``, ``reference`` (with ``on_delete``), ``permissions``, ``comment`` and ``default_always`` takes
    *current*'s; ``flexible`` is *target*'s, or *current*'s when the kind holds an object; then the value rule of
    ``model_ddl.field_statement_keeping()``: a ``server`` field keeps *current*'s ``DEFAULT``, ``VALUE`` and
    ``COMPUTED`` when *current* has one; else a ``COMPUTED`` *current* with ``computed`` undeclared stays ``COMPUTED``
    — *target*'s ``value`` as its expression when it has one, else *current*'s — with no ``DEFAULT`` or ``VALUE``;
    else ``default`` and ``value`` are *current*'s when undeclared, *target*'s otherwise. ``DEFAULT ALWAYS`` is kept
    only with a default.
    """
    if isinstance(current, TableState) and isinstance(target, TableState):
        return _effective_table(current, target)
    if isinstance(current, FieldState) and isinstance(target, FieldState):
        return _effective_field(current, target)
    raise TypeError("effective() takes two TableStates or two FieldStates.")


def _effective_table(current: TableState, target: TableState) -> TableState:
    marks = target.undeclared
    if not marks:
        return target
    changes: dict[str, Any] = {}
    if "schema_mode" in marks:
        changes["schema_mode"] = current.schema_mode
    if "table_type" in marks:
        changes |= {
            "table_type": current.table_type,
            "relation_in": current.relation_in,
            "relation_out": current.relation_out,
            "enforced": current.enforced,
        }
    if "changefeed" in marks:
        changes |= {"changefeed": current.changefeed, "changefeed_include_original": current.changefeed_include_original}
    for key in ("drop", "comment", "permissions"):
        if key in marks:
            changes[key] = getattr(current, key)
    return replace(target, **changes)


def _holds_object(field_type: str) -> bool:
    try:
        return holds_object(parse_kind(normalise_kind(field_type)[0]))
    except DefineParseError:
        return False


def _effective_field(current: FieldState, target: FieldState) -> FieldState:
    marks = target.undeclared
    if not marks and not target.server:
        return target
    changes: dict[str, Any] = {}
    for key in ("assertion", "readonly", "permissions", "comment", "default_always"):
        if key in marks:
            changes[key] = getattr(current, key)
    if "reference" in marks:
        changes |= {"reference": current.reference, "on_delete": current.on_delete}
    if "flexible" in marks:
        changes["flexible"] = target.flexible or (current.flexible and _holds_object(target.field_type))
    if target.server and (current.default is not None or current.value is not None or current.computed is not None):
        changes |= {
            "default": current.default,
            "default_always": current.default_always,
            "value": current.value,
            "computed": current.computed,
        }
    elif "computed" in marks and current.computed is not None:
        computed = target.value if target.value is not None else current.computed
        changes |= {"default": None, "default_always": False, "value": None, "computed": computed}
    else:
        if "default" in marks:
            changes["default"] = current.default
        if "value" in marks:
            changes["value"] = current.value
    if changes.get("default", target.default) is None:
        changes["default_always"] = False
    return replace(target, **changes)


# --- the diff ----------------------------------------------------------------------------------------------------------

_RELATION_FIELDS = ("in", "out")


def _compared_fields(table: TableState) -> dict[str, FieldState]:
    """The fields of *table* the diff compares: a relation's ``in`` and ``out`` left out (SurrealDB manages them)."""
    if table.table_type.lower() != "relation":
        return dict(table.fields)
    return {name: field for name, field in table.fields.items() if name not in _RELATION_FIELDS}


def _derived_item(table: TableState, field: FieldState) -> bool:
    """Whether *field* of the current *table* is the item field SurrealDB derives for a typed array or set: ``<p>.*``
    of a ``<p>`` of kind ``array<T>``/``set<T>`` (``T`` not ``any``), of kind ``T``, with no clause but ``FLEXIBLE``
    and the default permissions (M15 g; 3.3.4 renders it ``p.*``, 2.7.0 ``p[*]``)."""
    if not field.name.endswith(".*"):
        return False
    parent = table.fields.get(field.name[:-2])
    if parent is None:
        return False
    try:
        kind = parse_kind(normalise_kind(parent.field_type)[0])
    except DefineParseError:
        return False
    if not isinstance(kind, KName) or kind.name not in ("array", "set"):
        return False
    items = [argument for argument in kind.args if not isinstance(argument, str)]
    if not items or items[0] == KName("any"):
        return False
    item = FieldState(field.name, render_kind(items[0]), False, path=field.path)
    return compare.field_key(replace(field, flexible=False)) == compare.field_key(item)


def _unwritable(what: str) -> ValueError:
    return ValueError(
        f"SchemaState.diff(): {what} of the target cannot be written by a migration operation (an MTREE or COUNT "
        "index, an access other than TYPE RECORD, a definition search.Analyzer or schema.Event refuses …)."
    )


_T = TypeVar("_T")


def _required(operation: _T | None, what: str) -> _T:
    """*operation*, built from a definition of the target; ``ValueError`` when no operation writes it."""
    if operation is None:
        raise _unwritable(what)
    return operation


def _redefinition(operation: Any, previous: Any) -> Any:
    """*operation* redefining a definition: with ``previous=`` when the current one can be written, else
    ``overwrite=True`` (irreversible)."""
    return replace(operation, previous=previous) if previous is not None else replace(operation, overwrite=True)


def diff(current: SchemaState, target: SchemaState) -> "list[Operation]":
    """The operations that turn *current* into *target* (``SchemaState.diff()``, spec §5.7) — pure, in this order:

    1. ``DefineAnalyzer`` for each analyzer of *target* missing from *current*, then each one that differs (with
       ``previous=``), by name;
    2. the tables of *target* missing from *current*, by name, views last: ``CreateTable`` (the schema mode and the
       table type *target*'s marks leave to the database not written: SurrealDB's defaults), ``AddField`` for each
       field (by name: a parent before its children), ``CreateIndex`` for each index, ``DefineEvent`` for each event;
    3. the tables both hold, by name, compared with their effective target (:func:`effective`): ``AlterTable`` when a
       table clause differs; per field ``AddField``/``AlterField``, then ``DropField`` for the extra ones (children
       before parents); per index ``CreateIndex`` (with ``previous=`` when it differs), then ``DropIndex``; per event
       ``DefineEvent`` (with ``previous=``), then ``RemoveEvent``;
    4. ``RebuildIndex`` for each index of a table both hold that uses an analyzer step 1 redefines and that step 3
       does not redefine;
    5. accesses: ``DefineAccess`` (with ``previous=`` when it differs), then ``RemoveAccess`` for the extra ones;
    6. ``DropTable`` for each table of *current* missing from *target*, views first;
    7. ``RemoveAnalyzer`` for each analyzer of *current* missing from *target*.

    A redefinition or a removal carries the current definition as ``previous=`` when an operation can write it, else
    the redefinition is written ``overwrite=True`` and the removal without ``previous`` (both irreversible). Ignored:
    the tracking table, a relation's ``in`` and ``out``, the item field a typed array derives (on *current*'s side
    only), functions, params, ``other``, ``issues`` and ``applied_migrations``.

    :raises ValueError: *target* holds a definition no operation can write.
    """
    from . import operations as ops

    result: list[Operation] = []
    tables = {name: table for name, table in target.tables.items() if name != MIGRATIONS_TABLE}
    stored = {name: table for name, table in current.tables.items() if name != MIGRATIONS_TABLE}

    # 1. analyzers
    analyzers, redefined = _analyzer_changes(ops, current, target)
    result.extend(analyzers)

    # 2. new tables, views last
    new = sorted((name for name in tables if name not in stored), key=lambda name: (tables[name].view_query is not None, name))
    for name in new:
        table = tables[name]
        result.append(_table_creation(ops, table))
        result.extend(ops.AddField.from_field_state(name, field) for field in _compared_fields(table).values())
        result.extend(_index_creation(ops, name, index) for index in table.indexes.values())
        result.extend(_event_definition(ops, name, event) for event in table.events.values())

    # 3. tables in both
    rebuilds: list[Operation] = []
    for name in sorted(name for name in tables if name in stored):
        result.extend(_table_changes(ops, stored[name], tables[name], redefined, rebuilds))

    # 4. indexes to rebuild
    result.extend(rebuilds)

    # 5. accesses
    result.extend(_access_changes(ops, current, target))

    # 6. removed tables, views first
    gone = sorted((name for name in stored if name not in tables), key=lambda name: (stored[name].view_query is None, name))
    result.extend(ops.DropTable(name) for name in gone)

    # 7. removed analyzers
    for name, analyzer in current.analyzers.items():
        if name not in target.analyzers:
            result.append(ops.RemoveAnalyzer(name, previous=ops.DefineAnalyzer.from_analyzer_state(analyzer)))
    return result


def _analyzer_changes(ops: Any, current: SchemaState, target: SchemaState) -> "tuple[list[Operation], set[str]]":
    """Step 1 of :func:`diff`: the analyzers *target* adds, then those it redefines (by name), and the names of the
    redefined ones."""
    added: list[Operation] = []
    changed: list[Operation] = []
    redefined: set[str] = set()
    for name, analyzer in target.analyzers.items():
        before = current.analyzers.get(name)
        if before is not None and compare.analyzer_key(before) == compare.analyzer_key(analyzer):
            continue
        operation = _required(ops.DefineAnalyzer.from_analyzer_state(analyzer), f"analyzer {name!r}")
        if before is None:
            added.append(operation)
            continue
        previous = ops.DefineAnalyzer.from_analyzer_state(before)
        changed.append(operation if previous is None else replace(operation, previous=previous))
        redefined.add(name)
    return [*added, *changed], redefined


def _access_changes(ops: Any, current: SchemaState, target: SchemaState) -> "list[Operation]":
    """Step 5 of :func:`diff`: the accesses *target* defines or redefines, then those it no longer holds."""
    result: list[Operation] = []
    for name, access in target.accesses.items():
        before = current.accesses.get(name)
        if before is not None and compare.access_key(before) == compare.access_key(access):
            continue
        operation = _required(ops.DefineAccess.from_access_state(access), f"access {name!r}")
        result.append(operation if before is None else _redefinition(operation, ops.DefineAccess.from_access_state(before)))
    for name, access in current.accesses.items():
        if name not in target.accesses:
            result.append(ops.RemoveAccess(name, previous=ops.DefineAccess.from_access_state(access)))
    return result


#: The table keys a new table takes SurrealDB's default for when its model leaves them to the database.
_CREATION_DEFAULTS = ("schema_mode", "table_type")


def _table_creation(ops: Any, table: TableState) -> "Operation":
    """The ``CreateTable`` of a *target* table: every clause it holds, but the schema mode and the table type its marks
    leave to the database, written as nothing — the statement its model's applier writes on a new table, which
    SurrealDB defines ``SCHEMALESS`` and ``TYPE ANY`` (spec §5.4's golden equality)."""
    operation = ops.CreateTable.from_table_state(table)
    unwritten = {key: None for key in _CREATION_DEFAULTS if key in table.undeclared}
    created: Operation = replace(operation, **unwritten) if unwritten else operation
    return created


def _index_creation(ops: Any, table: str, index: IndexState) -> "Operation":
    operation: Operation = _required(ops.CreateIndex.from_index_state(table, index), f"index {index.name!r} on table {table!r}")
    return operation


def _event_definition(ops: Any, table: str, event: EventState) -> "Operation":
    operation: Operation = _required(ops.DefineEvent.from_event_state(event), f"event {event.name!r} on table {table!r}")
    return operation


def _table_changes(
    ops: Any, current: TableState, target: TableState, redefined: set[str], rebuilds: "list[Operation]"
) -> "list[Operation]":
    """Step 3 of :func:`diff` for one table both states hold; the indexes to rebuild go to *rebuilds*."""
    name = target.name
    result: list[Operation] = []
    wanted = effective(current, target)
    if compare.table_key(current) != compare.table_key(wanted):
        result.append(ops.AlterTable.from_table_states(current, wanted))
    fields, stored = _compared_fields(target), _compared_fields(current)
    for key, field in fields.items():
        before = stored.get(key)
        if before is None:
            result.append(ops.AddField.from_field_state(name, field))
            continue
        wanted_field = effective(before, field)
        if compare.field_key(before) != compare.field_key(wanted_field):
            result.append(ops.AlterField.from_field_states(name, before, wanted_field))
    for key in sorted(stored, reverse=True):
        if key not in fields and not _derived_item(current, stored[key]):
            result.append(ops.DropField(name, key, path=_path(stored[key])))
    for key, index in target.indexes.items():
        before_index = current.indexes.get(key)
        if before_index is None:
            result.append(_index_creation(ops, name, index))
        elif compare.index_key(before_index) != compare.index_key(index):
            previous_index = ops.CreateIndex.from_index_state(name, before_index)
            result.append(_redefinition(_index_creation(ops, name, index), previous_index))
        elif index.search_analyzer in redefined:
            rebuilds.append(ops.RebuildIndex(name, key))
    for key, index in current.indexes.items():
        if key not in target.indexes:
            result.append(ops.DropIndex(name, key, previous=ops.CreateIndex.from_index_state(name, index)))
    for key, event in target.events.items():
        before_event = current.events.get(key)
        if before_event is None:
            result.append(_event_definition(ops, name, event))
        elif compare.event_key(before_event) != compare.event_key(event):
            previous_event = ops.DefineEvent.from_event_state(before_event)
            result.append(_redefinition(_event_definition(ops, name, event), previous_event))
    for key, event in current.events.items():
        if key not in target.events:
            result.append(ops.RemoveEvent(key, name, previous=ops.DefineEvent.from_event_state(event)))
    return result


def _path(field: FieldState) -> tuple[str, ...]:
    """``path=`` for an operation on *field*: its segments when one holds a dot, else ``()`` (derived from the name)."""
    path = tuple(field.path)
    return () if path == tuple(field.name.split(".")) else path


# --- the replays -------------------------------------------------------------------------------------------------------

#: The ``"section"`` issue that hides a database-level definition of each kind (D36).
_SECTIONS = {"analyzer": "analyzers", "access": "accesses", "function": "functions"}


def _removed(operation: "Operation") -> tuple[Kind, str, str | None] | None:
    """What the removal *operation* removes — ``(kind, name, table)`` —, ``None`` for any other operation."""
    from . import operations as ops

    if isinstance(operation, ops.DropTable):
        return "table", operation.name, None
    if isinstance(operation, ops.DropField):
        return "field", operation.name, operation.table
    if isinstance(operation, ops.DropIndex):
        return "index", operation.name, operation.table
    if isinstance(operation, ops.RemoveEvent):
        return "event", operation.name, operation.table
    if isinstance(operation, ops.RemoveAnalyzer):
        return "analyzer", operation.name, None
    if isinstance(operation, ops.RemoveAccess):
        return "access", operation.name, None
    if isinstance(operation, ops.RemoveFunction):
        return "function", operation.name, None
    return None


def _holds(state: SchemaState, kind: Kind, name: str, table: str | None) -> bool:
    """Whether *state* holds the definition *kind* *name* (on *table*)."""
    if kind == "table":
        return name in state.tables
    if kind in ("field", "index", "event"):
        stored = state.tables.get(table or "")
        if stored is None:
            return False
        definitions: Mapping[str, object] = {"field": stored.fields, "index": stored.indexes, "event": stored.events}[kind]
        return name in definitions
    definitions = {"analyzer": state.analyzers, "access": state.accesses, "function": state.functions}[kind]
    return name in definitions


def _hidden(issues: Iterable[SchemaIssue], kind: Kind, table: str | None, name: str) -> bool:
    """Whether an issue of the read could hide the definition *kind* *name* (D36, spec §6.3): for a table, a
    ``"table"`` issue with its name or the ``"tables"`` section issue; for a field, an index or an event, one of
    those for its table or any issue whose ``table`` is its table; for an analyzer, an access or a function, an issue
    of that kind or its section issue."""
    for issue in issues:
        if kind in ("table", "field", "index", "event"):
            subject = name if kind == "table" else table
            if (issue.kind == "table" and issue.name == subject) or (issue.kind == "section" and issue.name == "tables"):
                return True
            if kind != "table" and issue.table == subject:
                return True
        elif issue.kind == kind or (issue.kind == "section" and issue.name == _SECTIONS[kind]):
            return True
    return False


_WHAT: dict[str, Callable[[str, str | None], str]] = {
    "table": lambda name, table: f"table {name!r}",
    "field": lambda name, table: f"field {name!r} of table {table!r}",
    "index": lambda name, table: f"index {name!r} of table {table!r}",
    "event": lambda name, table: f"event {name!r} of table {table!r}",
    "analyzer": lambda name, table: f"analyzer {name!r}",
    "access": lambda name, table: f"access {name!r}",
    "function": lambda name, table: f"function {name}",
}


def _check_removal(migration: "Migration", position: int, operation: "Operation", state: SchemaState) -> None:
    """E-R1 (spec §7): a strict replay meets the removal of a definition *state* does not hold and no issue of the
    read could hide — SurrealDB would refuse it (M12, M31)."""
    removed = _removed(operation)
    if removed is None:
        return
    kind, name, table = removed
    if _holds(state, kind, name, table) or _hidden(state.issues, kind, table, name):
        return
    raise ValueError(
        f"Migration {migration.name!r}, operation {position} ({operation.describe()}): {_WHAT[kind](name, table)} is "
        "neither in the database nor defined by an earlier migration, so SurrealDB would refuse the removal. A RawSQL "
        "that defines it declares that in state_operations=."
    )


def advance(migration: "Migration", start: SchemaState, *, strict: bool) -> SchemaState:
    """*start* after *migration*: each operation's ``state_forwards()`` in order, the migration's name appended to
    ``applied_migrations`` — pure (spec §6.3).

    *strict* (no default): ``True`` for a replay anchored on a database read — the removal of a definition the state
    does not hold raises E-R1, unless an issue of the read (*start*'s ``issues``, kept through the replay) could hide
    it (D36); ``False`` for a file replay, where it is a no-op. A ``RawSQL``'s ``state_operations`` are never checked
    (D37).

    :raises ValueError: E-R1, naming the migration and the operation (1-based).
    """
    result = start
    for position, operation in enumerate(migration.operations, 1):
        if strict:
            _check_removal(migration, position, operation, result)
        result = operation.state_forwards(result)
    return replace(result, applied_migrations=(*result.applied_migrations, migration.name))


def replay(
    migrations: "Iterable[Migration]", *, start: SchemaState | None = None, until: str | None = None, strict: bool
) -> SchemaState:
    """*start* (an empty state when ``None``) after each of *migrations* in turn (:func:`advance`) — pure. The
    result's ``applied_migrations`` is *start*'s followed by the names replayed. *until* stops after the migration of
    that name. *strict* (no default) as :func:`advance`.

    :raises ValueError: E-R1 in a strict replay; *until* names none of *migrations*.
    """
    result = SchemaState() if start is None else start
    for migration in migrations:
        result = advance(migration, result, strict=strict)
        if migration.name == until:
            return result
    if until is not None:
        raise ValueError(f"replay(): no migration named {until!r} among those replayed.")
    return result


def rewind(migration: "Migration", start: SchemaState) -> SchemaState:
    """*start* — a state *migration* was applied to — with *migration* undone: each operation's
    ``state_backwards()`` in reverse order, its name taken out of ``applied_migrations`` — pure (spec §6.3).

    :raises ValueError: an operation is not reversible (``"<describe()> is not reversible."``, D39).
    """
    result = start
    for operation in reversed(migration.operations):
        result = operation.state_backwards(result)
    return replace(result, applied_migrations=tuple(name for name in result.applied_migrations if name != migration.name))
