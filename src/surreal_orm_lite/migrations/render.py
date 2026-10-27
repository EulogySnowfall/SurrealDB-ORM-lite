"""The statements of the migration system (v0.38.0) and the reader of the ORM's own statements — private.

An operation's statement is built by the renderers the appliers already use (``table_ddl()``'s, ``Index.ddl()``'s,
``Analyzer.ddl()``'s, ``Event.ddl()``'s, ``function_ddl()``, ``schema.access_statement()``); this module adds what
they do not write:

- :func:`plain` — the creation form of an ``OVERWRITE`` statement (``DEFINE <KIND>`` with no clause);
- :func:`field_statement` — the one writer of a field statement from a ``FieldState``: every clause it holds, in the
  order the servers render them (``TYPE``, ``FLEXIBLE``, ``REFERENCE[ ON DELETE …]``, ``DEFAULT[ ALWAYS]``,
  ``READONLY``, ``VALUE``, ``ASSERT``, ``COMMENT``, ``PERMISSIONS``; with ``COMPUTED``: ``TYPE``, ``FLEXIBLE``,
  ``COMPUTED``, ``COMMENT``, ``PERMISSIONS``), so :func:`read_own` reads the ``FieldState`` back;
- the line-dependent ``FLEXIBLE`` rule (v0.34.0): before SurrealDB 3.x — a 2.x server or the embedded engine —, a
  ``SCHEMAFULL`` table empties an object held by an ``any`` column, and drops a ``FLEXIBLE`` or ``any`` column's value
  from every item of a list of sub-models unless the list is ``FLEXIBLE`` too. :func:`flexible_before_3` reads that
  rule from a schema state, :func:`flexible_candidate` says whether a kind could ever be selected by it, and
  :func:`flexible_redefinitions` writes the columns an ``AlterTable`` making a table ``SCHEMAFULL`` redefines on line 2
  (SurrealDB 3.x refuses ``FLEXIBLE`` on a kind holding no object and keeps the values without it);
- :func:`removal` — ``REMOVE …`` statements, every name in backticks (``surql_literal._identifier()``: any name
  SurrealDB accepts there, ``user-events`` included), a field's path through ``model_ddl.field_path()``;
- :func:`check_statements` — the count of top-level statements of a ``RawSQL``/``DataMigration`` text, and its checks;
- :func:`read_own` — a statement the ORM wrote read into its state by ``define_parser`` (M17), and
  :func:`literal_value` — a ``DEFAULT`` text read back as the Python value the ORM writes it from.

Nothing here quotes a statement in an error: a statement may hold a credential (a literal in an expression).

Imports ``schema_state``, ``define_parser``, ``model_ddl``, ``search``, ``surql_literal`` and ``functions`` — never
``model_base``, ``model_auth`` or ``connection_manager`` — and, of the ``migrations`` package, ``compare`` only.
"""

import re
from collections.abc import Callable, Collection, Sequence
from typing import Literal

from ..define_parser import (
    DefineParseError,
    Kind,
    KLiteral,
    KName,
    KUnion,
    blank_literals,
    parse_define_access,
    parse_define_analyzer,
    parse_define_event,
    parse_define_field,
    parse_define_function,
    parse_define_index,
    parse_define_table,
    parse_kind,
    render_kind,
    scan_fragment,
    top_level_keywords,
)
from ..functions import normalize_function_name
from ..model_ddl import _children, field_path, field_permissions_text, holds_any, holds_object
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
from ..search import checked_line
from ..surql_literal import _identifier, _quoted, to_surql_literal
from . import compare

__all__ = [
    "check_statements",
    "field_statement",
    "flexible_before_3",
    "flexible_candidate",
    "flexible_redefinitions",
    "literal_value",
    "plain",
    "read_own",
    "removal",
]

#: How a statement defines: ``plain`` creates (``DEFINE <KIND>``), ``overwrite`` defines or replaces (``OVERWRITE``),
#: ``if_not_exists`` keeps what exists (``IF NOT EXISTS``).
Mode = Literal["plain", "overwrite", "if_not_exists"]
_MODE_CLAUSES: dict[str, str] = {"plain": "", "overwrite": " OVERWRITE", "if_not_exists": " IF NOT EXISTS"}

#: The kinds of definition a removal names.
RemovalKind = Literal["table", "field", "index", "event", "analyzer", "access", "function"]
#: A state ``read_own()`` returns.
OwnState = TableState | FieldState | IndexState | AnalyzerState | EventState | AccessState | FunctionState

_NONE = KLiteral("none")
_ANY = KName("any")
_ITEM_KINDS = ("array", "set")

_OVERWRITE_HEAD = re.compile(r"DEFINE (TABLE|FIELD|INDEX|EVENT|ANALYZER|ACCESS|FUNCTION|PARAM) OVERWRITE ")
_READ_HEAD = re.compile(r"DEFINE (TABLE|FIELD|INDEX|EVENT|ANALYZER|ACCESS|FUNCTION)(?: OVERWRITE| IF NOT EXISTS)?(?= )")

#: The first word of a statement, and those a migration's SQL may not start a statement with.
_FIRST_WORD = re.compile(r"[A-Za-z0-9_]+")
_REFUSED_WORDS = frozenset({"BEGIN", "COMMIT", "CANCEL", "USE"})
_OPENING = frozenset("([{")
_CLOSING = frozenset(")]}")
#: The ``COMMENT`` ``schema.view_statement()`` writes right after a view's ``AS (<query>)``, up to its opening quote.
_VIEW_COMMENT = re.compile(r" COMMENT ['\"]")


def _mode_clause(mode: object) -> str:
    """The clause written after ``DEFINE <KIND>`` for *mode*; ``ValueError`` for another value."""
    clause = _MODE_CLAUSES.get(mode) if isinstance(mode, str) else None
    if clause is None:
        raise ValueError(f"mode= must be 'plain', 'overwrite' or 'if_not_exists', got {mode!r}.")
    return clause


def plain(statement: str) -> str:
    """*statement* with the ``OVERWRITE`` right after ``DEFINE <KIND>`` removed: the statement that creates the
    definition and fails when it exists (``DEFINE TABLE OVERWRITE `t` …;`` → ``DEFINE TABLE `t` …;``). An ``OVERWRITE``
    anywhere else — a name, a comment, a body — is kept. Pure.

    :raises ValueError: *statement* does not start with ``DEFINE <KIND> OVERWRITE`` (the statement is not quoted).
    """
    head = _OVERWRITE_HEAD.match(statement)
    if head is None:
        raise ValueError("plain(): the statement does not start with DEFINE <KIND> OVERWRITE.")
    return f"DEFINE {head.group(1)} {statement[head.end() :]}"


# --- kinds and the FLEXIBLE rule -------------------------------------------------------------------------------------


def _kind(text: str, *, what: str) -> Kind:
    """The kind *text* parsed; ``ValueError`` naming *what* (never quoting the text) when the parser cannot read it."""
    try:
        return parse_kind(text)
    except DefineParseError as error:
        raise ValueError(f"{what} is not a SurrealQL kind the ORM can read: {error}.") from error


def _admits_none(kind: Kind) -> bool:
    """Whether *kind* already holds ``NONE``: ``any``, ``none`` or a union with one of them."""
    if kind in (_ANY, _NONE):
        return True
    return isinstance(kind, KUnion) and any(_admits_none(member) for member in kind.members)


def _field_kind(field: FieldState) -> Kind:
    """The kind a field statement writes for *field*: its ``field_type``, as ``option<…>`` when it is ``nullable`` and
    does not admit ``NONE`` already (``option<any>`` is a parse error on both lines; ``any`` holds ``NONE``)."""
    kind = _kind(field.field_type, what=f"the kind of field {field.name!r}")
    if field.nullable and not _admits_none(kind):
        kind = KUnion((_NONE, *(kind.members if isinstance(kind, KUnion) else (kind,))))
    return kind


def _holds_object_items(kind: Kind) -> bool:
    """Whether *kind* holds an ``array`` or a ``set`` whose item kind holds ``object`` (``array<object>``,
    ``option<array<object>>``), at any depth."""
    if isinstance(kind, KName) and kind.name in _ITEM_KINDS:
        items = [argument for argument in kind.args if not isinstance(argument, str)]
        if items and holds_object(items[0]):
            return True
    return any(_holds_object_items(child) for child in _children(kind))


def _text_holds_any(text: str) -> bool:
    """Whether the kind *text* holds ``any``; ``False`` for a kind the parser cannot read."""
    try:
        return holds_any(parse_kind(text))
    except DefineParseError:
        return False


def _selects(state: SchemaState | None, table: str, path: Sequence[str], kind: Kind) -> bool:
    """:func:`flexible_before_3` on a parsed *kind*."""
    if state is None:
        return False
    stored = state.tables.get(table)
    if stored is None or stored.schema_mode.upper() != "SCHEMAFULL":
        return False
    if holds_any(kind):
        return True
    if not _holds_object_items(kind):
        return False
    prefix = (*path, "*")
    return any(
        len(child.path) > len(prefix)
        and tuple(child.path[: len(prefix)]) == prefix
        and (child.flexible or _text_holds_any(child.field_type))
        for child in stored.fields.values()
    )


def flexible_before_3(state: SchemaState | None, table: str, path: Sequence[str], kind: str) -> bool:
    """Whether, before SurrealDB 3.x, the column *path* of *table* with the kind *kind* is written ``FLEXIBLE`` — the
    rule ``define_table()`` applies there (v0.34.0), read from the schema state *state*.

    True when *state* holds *table* ``SCHEMAFULL`` and the kind holds ``any`` (``model_ddl.holds_any()``: ``any``, an
    untyped ``array`` or ``set``, ``option<any>`` …), or is a list of sub-models (an ``array``/``set`` whose item kind
    holds ``object``) under which *state* holds a column — at any depth below its items, ``<path>.*.…`` — that is
    ``FLEXIBLE`` or holds ``any``. False without *state*, for a table *state* does not hold and on a ``SCHEMALESS``
    table. Pure.

    :raises ValueError: *kind* is not a kind the parser reads.
    """
    return _selects(state, table, tuple(path), _kind(kind, what="flexible_before_3(): the kind"))


def flexible_candidate(field_type: str) -> bool:
    """Whether :func:`flexible_before_3` could select the kind *field_type* on some state (P2) — state-free.

    True when the kind holds ``any`` (``any``, ``option<any>``, an untyped ``array``/``set``, ``array<any>``) or an
    ``array``/``set`` whose item kind holds ``object`` (``array<object>``, ``option<array<object>>``); false for any
    other kind (``string``, ``object``, ``array<string>``, ``record<t>``). Pure.

    :raises ValueError: *field_type* is not a kind the parser reads.
    """
    kind = _kind(field_type, what="flexible_candidate(): the kind")
    return holds_any(kind) or _holds_object_items(kind)


def _flexible(table: str, field: FieldState, kind: Kind, *, line: int, state: SchemaState | None) -> bool:
    """Whether :func:`field_statement` writes ``FLEXIBLE``: on line 3 when the field is ``FLEXIBLE`` and its kind holds
    an object (SurrealDB 3.x refuses it elsewhere); on line 2 when the field is ``FLEXIBLE`` or
    :func:`flexible_before_3` selects it."""
    if line == 3:
        return field.flexible and holds_object(kind)
    return field.flexible or _selects(state, table, field.path, kind)


def field_statement(table: str, field: FieldState, *, mode: Mode, line: Literal[2, 3], state: SchemaState | None) -> str:
    """``DEFINE FIELD[ OVERWRITE | IF NOT EXISTS] <path> ON `<table>` TYPE <kind>…;`` — every clause *field* holds, the
    one field-statement writer of the migration system. Pure.

    The path through ``model_ddl.field_path()`` (each segment in backticks, an item ``*``), *table* in backticks, then,
    in the order the servers render them:

    - ``TYPE <kind>``: ``field_type`` in canonical form, ``option<…>`` when ``nullable`` and the kind does not admit
      ``NONE`` already (``any`` does);
    - ``FLEXIBLE``: on line 3 when ``field.flexible`` and the kind holds an object; on line 2 when ``field.flexible``
      or :func:`flexible_before_3` selects the column on *state*;
    - ``REFERENCE[ ON DELETE <action>]``, ``DEFAULT[ ALWAYS] <expr>``, ``READONLY``, ``VALUE <expr>``,
      ``ASSERT <expr>`` — or, for a ``computed`` field, ``COMPUTED <expr>`` alone (SurrealDB refuses those clauses
      with it);
    - ``COMMENT '<text>'`` (a string literal), ``PERMISSIONS <rules>`` (``model_ddl.field_permissions_text()``: none
      when every rule is ``FULL``, a field's default).

    The expressions are written as *field* holds them: the state was read from SurrealQL, or its operation checked
    them. :func:`read_own` of the result gives *field* back (``raw`` aside; a nullable ``any`` reads back not nullable,
    default ``FULL`` permissions back empty). *mode*: ``"plain"``, ``"overwrite"`` or ``"if_not_exists"``.

    :raises ValueError: an unknown *mode*, a *line* other than 2 or 3 (``TypeError`` for a non-``int``), a
        ``field_type`` the parser cannot read, a comment holding a NUL character.
    """
    clause = _mode_clause(mode)
    checked = checked_line(line)
    kind = _field_kind(field)
    parts = [f"DEFINE FIELD{clause} {field_path(field.path)} ON {_identifier(table)} TYPE {render_kind(kind)}"]
    if _flexible(table, field, kind, line=checked, state=state):
        parts.append("FLEXIBLE")
    if field.computed is not None:
        parts.append(f"COMPUTED {field.computed}")
    else:
        if field.reference:
            parts.append("REFERENCE" if field.on_delete is None else f"REFERENCE ON DELETE {field.on_delete}")
        if field.default is not None:
            parts.append(f"DEFAULT ALWAYS {field.default}" if field.default_always else f"DEFAULT {field.default}")
        if field.readonly:
            parts.append("READONLY")
        if field.value is not None:
            parts.append(f"VALUE {field.value}")
        if field.assertion is not None:
            parts.append(f"ASSERT {field.assertion}")
    if field.comment is not None:
        if "\x00" in field.comment:
            raise ValueError(
                f"The comment of field {field.name!r} holds a NUL character (\\x00), which SurrealQL cannot carry."
            )
        parts.append(f"COMMENT {_quoted(field.comment)}")
    permissions = field_permissions_text(field.permissions)
    if permissions is not None:
        parts.append(f"PERMISSIONS {permissions}")
    return " ".join(parts) + ";"


def flexible_redefinitions(table: str, state: SchemaState, *, skip: Collection[str]) -> list[str]:
    """The ``DEFINE FIELD OVERWRITE … FLEXIBLE;`` statements that keep the object values of *table*'s columns when an
    ``AlterTable`` makes it ``SCHEMAFULL`` on line 2 (spec §5.3, R33; M27) — pure.

    One :func:`field_statement` (``mode="overwrite"``, ``line=2``, every clause of the column kept: ``OVERWRITE``
    resets the others) per column of *table* in *state* that :func:`flexible_before_3` selects, in *state*'s order
    (sorted by name: a parent before its children), but those *skip* names (canonical paths: the migration defines or
    redefines them itself). None when *state* does not hold *table* or holds it ``SCHEMALESS``; a column whose kind the
    parser cannot read is not selected.

    :raises TypeError: *skip* is a ``str`` (a collection of names is expected).
    """
    if isinstance(skip, str):
        raise TypeError("flexible_redefinitions(): skip= takes a collection of field names, not a str.")
    stored = state.tables.get(table)
    if stored is None:
        return []
    statements: list[str] = []
    for name, field in stored.fields.items():
        if name in skip:
            continue
        try:
            kind = _field_kind(field)
        except ValueError:
            continue
        if _selects(state, table, field.path, kind):
            statements.append(field_statement(table, field, mode="overwrite", line=2, state=state))
    return statements


# --- removals --------------------------------------------------------------------------------------------------------

#: The removals of a definition that belongs to a table, and their keyword.
_ON_TABLE = {"field": "FIELD", "index": "INDEX", "event": "EVENT"}
#: The removals of a database-level definition: their keyword and what follows the name.
_ON_DATABASE = {"table": ("TABLE", ""), "analyzer": ("ANALYZER", ""), "access": ("ACCESS", " ON DATABASE")}


def _removal_name(value: object, *, what: str) -> str:
    """*value*, checked as a name a removal writes in backticks: a non-empty ``str`` without a NUL character."""
    if not isinstance(value, str):
        raise TypeError(f"removal(): {what} must be a str, got {type(value).__name__}.")
    if not value or "\x00" in value:
        raise ValueError(f"removal(): {what} must be a non-empty str without a NUL character (\\x00).")
    return value


def removal(
    kind: RemovalKind,
    name: str,
    *,
    table: str | None = None,
    path: Sequence[str] = (),
    if_exists: bool = False,
) -> str:
    """The ``REMOVE`` statement of a definition — pure.

    ``REMOVE TABLE|ANALYZER[ IF EXISTS] `<name>`;``, ``REMOVE ACCESS[ IF EXISTS] `<name>` ON DATABASE;``, ``REMOVE
    FIELD|INDEX|EVENT[ IF EXISTS] <name> ON `<table>`;`` and ``REMOVE FUNCTION[ IF EXISTS] fn::<name>;``. Names are
    written in backticks (``surql_literal._identifier()``: backslash and backtick escaped), so any name SurrealDB
    accepts there is written as data — ``user-events``, ``2024_sales`` (M24); a field is named by its *path* (its
    canonical *name* split at ``.`` when *path* is empty) through ``model_ddl.field_path()``, each segment in
    backticks and an item ``*``; a function through ``normalize_function_name()``. ``IF EXISTS`` with *if_exists*.

    :raises ValueError: an unknown *kind*; a field, index or event without *table*; an empty name, table or path
        segment, or one holding a NUL character; an invalid function name.
    :raises TypeError: a name or a table that is not a ``str``.
    """
    exists = " IF EXISTS" if if_exists else ""
    if kind == "function":
        return f"REMOVE FUNCTION{exists} {normalize_function_name(name)};"
    if kind in _ON_DATABASE:
        keyword, tail = _ON_DATABASE[kind]
        return f"REMOVE {keyword}{exists} {_identifier(_removal_name(name, what='the name'))}{tail};"
    keyword_on = _ON_TABLE.get(kind) if isinstance(kind, str) else None
    if keyword_on is None:
        raise ValueError(
            f"removal(): unknown kind {kind!r}; expected one of table, field, index, event, analyzer, access, function."
        )
    if table is None:
        raise ValueError(f"removal(): a {kind} removal needs table=.")
    on = _identifier(_removal_name(table, what="table="))
    name = _removal_name(name, what="the name")
    if kind != "field":
        return f"REMOVE {keyword_on}{exists} {_identifier(name)} ON {on};"
    segments = tuple(path) or tuple(name.split("."))
    for segment in segments:
        _removal_name(segment, what="a path segment")
    return f"REMOVE FIELD{exists} {field_path(segments)} ON {on};"


# --- the statements of a RawSQL or a DataMigration -------------------------------------------------------------------


def _top_level_segments(blanked: str) -> list[str]:
    """*blanked* (literals blanked by ``blank_literals()``, brackets balanced) split at each ``;`` outside brackets."""
    segments: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(blanked):
        if char in _OPENING:
            depth += 1
        elif char in _CLOSING:
            depth -= 1
        elif char == ";" and depth == 0:
            segments.append(blanked[start:index])
            start = index + 1
    segments.append(blanked[start:])
    return segments


def check_statements(sql: str, *, what: str) -> int:
    """The number of top-level statements of the SurrealQL *sql* of a ``RawSQL`` or a ``DataMigration`` (*what* names
    it in the messages), checked — pure.

    *sql* is checked by ``define_parser.scan_fragment()``'s scanner: literals and quoted identifiers terminated,
    brackets balanced, no comment marker and no NUL. A ``;`` outside every literal and bracket separates statements;
    an empty or blank segment is not a statement (M2: ``DEFINE TABLE c;;`` is one, and SurrealDB gives it one status
    entry). A statement whose first word is ``BEGIN``, ``COMMIT``, ``CANCEL`` or ``USE`` (any case) is refused: each
    migration runs in a transaction of its own, on the executor's namespace and database. The checks catch mistakes;
    they are not an injection defence — a migration's SQL is trusted application code.

    :raises TypeError: *sql* is not a ``str``.
    :raises ValueError: the scanner refuses *sql* (its reason given, the SQL never quoted); a transaction or ``USE``
        statement.
    """
    if not isinstance(sql, str):
        raise TypeError(f"{what}: the SQL must be a str, got {type(sql).__name__}.")
    try:
        scan_fragment(sql)
    except DefineParseError as error:
        raise ValueError(f"{what}: the SQL is not SurrealQL the ORM can send: {error}.") from error
    count = 0
    for segment in _top_level_segments(blank_literals(sql)):
        statement = segment.strip()
        if not statement:
            continue
        word = _FIRST_WORD.match(statement)
        if word is not None and word.group().upper() in _REFUSED_WORDS:
            raise ValueError(
                f"{what}: BEGIN, COMMIT, CANCEL and USE are not allowed; each migration runs in a transaction of its own."
            )
        count += 1
    return count


# --- reading the ORM's own statements --------------------------------------------------------------------------------


def _skip_spaces(text: str, position: int) -> int:
    while position < len(text) and text[position].isspace():
        position += 1
    return position


def _unwrapped_view(text: str, position: int, opening: int) -> str:
    """The view statement *text* whose ``AS`` stands at *position* and its ``(`` at *opening*, as the servers render
    it: ``AS <query>``, and the ``COMMENT`` that ``schema.view_statement()`` writes after the query moved before
    ``AS`` (the servers render it there, and the parser reads it only there)."""
    blanked = blank_literals(text)
    depth = 0
    closing = None
    for index in range(opening, len(text)):
        if blanked[index] in _OPENING:
            depth += 1
        elif blanked[index] in _CLOSING:
            depth -= 1
            if depth == 0:
                closing = index
                break
    if closing is None:  # pragma: no cover - top_level_keywords() refused unbalanced brackets before
        raise DefineParseError("its brackets are not balanced")
    query = text[opening + 1 : closing]
    rest = text[closing + 1 :]
    comment = _VIEW_COMMENT.match(rest)
    if comment is None:
        return f"{text[:opening]}{query}{rest}"
    quote = closing + 1 + comment.end() - 1
    end = blanked.find(text[quote], quote + 1) + 1
    return f"{text[:position]}COMMENT {text[quote:end]} AS {query}{text[end:]}"


def _read_table(text: str) -> TableState:
    """A table statement, a view's ``AS (<query>)`` unwrapped as the servers render it (M17)."""
    for position, _ in top_level_keywords(text, ("AS",)):
        opening = _skip_spaces(text, position + len("AS"))
        if opening < len(text) and text[opening] == "(":
            text = _unwrapped_view(text, position, opening)
            break
    return parse_define_table(text)


def _read_field(text: str) -> FieldState:
    """A field statement; a kind the parser keeps as written is not one the ORM writes."""
    parsed = parse_define_field(text)
    if parsed.type_as_written:
        raise DefineParseError("its kind is not understood")
    return parsed.state


def _read_event(text: str) -> EventState:
    """An event statement; without ``WHEN``, the servers' rendering of the default, ``WHEN true``, before ``THEN``."""
    keywords = top_level_keywords(text, ("WHEN", "THEN"))
    if not any(keyword == "WHEN" for _, keyword in keywords):
        then = next((position for position, keyword in keywords if keyword == "THEN"), None)
        if then is not None:
            text = f"{text[:then]}WHEN true {text[then:]}"
    return parse_define_event(text)


_READERS: dict[str, Callable[[str], OwnState]] = {
    "TABLE": _read_table,
    "FIELD": _read_field,
    "INDEX": parse_define_index,
    "EVENT": _read_event,
    "ANALYZER": parse_define_analyzer,
    "ACCESS": parse_define_access,
    "FUNCTION": parse_define_function,
}


def read_own(statement: str) -> OwnState:
    """The state of a ``DEFINE`` statement the ORM wrote — a table (a view and a relation included), a field, an
    index, an analyzer, an event, an access or a function — read by ``define_parser`` as ``DatabaseIntrospector``
    reads the server's rendering of it (M17).

    Before the parser: the ``OVERWRITE`` or ``IF NOT EXISTS`` after ``DEFINE <KIND>`` and the final ``;`` are removed,
    a view's ``AS (<query>)`` is unwrapped to ``AS <query>``, and an event without ``WHEN`` reads ``WHEN true`` — the
    servers render neither clause, nor the parentheses, and render that default. ``raw`` holds the text the parser
    read. Pure.

    :raises RuntimeError: the statement is not one the ORM writes, or the parser cannot read it — a bug of the ORM;
        the message gives the parser's fixed reason, never the statement.
    """
    head = _READ_HEAD.match(statement)
    if head is None:
        raise RuntimeError(
            "read_own(): the statement is not a DEFINE statement of a kind the migrations write; this is a bug of "
            "surreal_orm_lite."
        )
    kind = head.group(1)
    text = f"DEFINE {kind}{statement[head.end() :]}".rstrip().removesuffix(";").rstrip()
    try:
        return _READERS[kind](text)
    except DefineParseError as error:
        raise RuntimeError(
            f"read_own(): the ORM could not read its own DEFINE {kind} statement ({error}); this is a bug of surreal_orm_lite."
        ) from error


def literal_value(text: str) -> compare.LiteralValue | None:
    """The Python value the ``DEFAULT`` text *text* is the literal of, when the ORM can write it back — the reverse of
    ``to_surql_literal()``: ``true``/``false``, an ``int``, a finite ``float`` (``1.5`` and ``1.5f``), a ``Decimal``
    (``1.10dec``), a string literal, an aware UTC ``datetime`` (``d'…Z'``, at most six fractional digits) —, else
    ``None`` (``NONE``, an array, an object, an expression, a value ``to_surql_literal()`` refuses: an ``int`` beyond
    64 bits, a string holding a NUL). The caller checks the round trip with ``compare.literal_equal()``. Pure.
    """
    value = compare.read_literal(text)
    if value is None:
        return None
    try:
        to_surql_literal(value)
    except TypeError:
        return None
    return value
