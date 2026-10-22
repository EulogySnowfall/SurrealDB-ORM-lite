"""Server-side events and DDL helpers (v0.31.0), materialized views (v0.32.0), relation constraints (v0.33.0), model
table DDL (v0.34.0).

``Event`` declares a server-side event — SurrealDB's ``DEFINE EVENT`` on a table — with the full SurrealDB-ORM's call
shape: a name, the SurrealQL the event runs (``then``), an optional condition (``when``), SurrealDB 3.x's ``ASYNC``
options and a comment. Every argument is checked when the object is built, before any request. ``Event.ddl(table)``
renders its ``DEFINE EVENT`` statement and ``function_ddl()`` a stored function's ``DEFINE FUNCTION`` statement: both
are pure — no connection, the same text for the same input — and the appliers send what they render.

**Names and expressions.** Event, table and parameter names are identifiers (letters, digits, underscores; not
starting with a digit), checked with ``fullmatch``; event and table names are written in backticks, function names go
through ``normalize_function_name()``. Expressions (``when``, ``then``, a function's body, a permissions condition) are
embedded in the statement as written, since SurrealQL takes no bound parameter in DDL, after a structural check
(``define_parser.scan_fragment()``): string literals and quoted identifiers terminated, brackets balanced, no SurrealQL
comment marker and no NUL, and no ``;`` outside brackets where one expression is expected. The check catches mistakes —
unbalanced brackets, a comment that would swallow the clauses the ORM appends, a second statement where one expression
is expected — and is not an injection defence: expressions are trusted application code, never user input. To use a
value, read it from the record (``$after.email``) or from a ``DEFINE PARAM``; never format it into the text. Kinds are
parsed and written in canonical form (``option<int>``); a comment is a string literal, its backslashes and quotes
escaped.

**Materialized views (v0.32.0).** A model whose ``SurrealConfigDict(view_query=…)`` is set reads a table SurrealDB
computes from that ``SELECT`` (``DEFINE TABLE … AS``). ``check_view_query()`` checks the query when the class is
defined: one ``SELECT``, not ``SELECT VALUE`` (one final ``;`` dropped), accepted by ``scan_fragment()``, and, when it
groups its records, no function SurrealDB documents as unusable in a view outside an aggregate's argument. The check
catches mistakes — unbalanced brackets, a comment that would swallow the parenthesis the ORM appends, a second
statement where one SELECT is expected — and is not an injection defence: a view's query is trusted application code,
never user input. ``view_query_outline()`` and ``view_query_hazards()`` read a grouped projection to find what
SurrealDB 2.x computes once per source record instead of keeping it up to date; that reading is a reading of the
text, not a parser: it serves the line checks and leaves what it cannot read — a sub-select, a graph path, a quoted
field name — to the server. ``view_statement()`` and ``remove_view_statement()`` render the statements, the table name
in backticks and the query inside ``AS ( … )``; ``view_write_refused()`` is the text every ORM write of a view model
raises. The table guard of ``define_view()`` and ``remove_view()`` reads ``INFO FOR DB`` once
(``read_table_definitions()``), tells a view from an ordinary table (``table_kind()``) and, for an ordinary table,
whether it holds a record (``table_holds_records()``), so a view never replaces a table that holds records and
``remove_view()`` never removes an ordinary table (``view_table_refused()``). It reads committed state: a check, then
the DDL, not a lock. Before the guard, ``define_view()`` refuses on SurrealDB 2.x and the embedded engine a grouped
projection those engines compute once per source record (``ensure_view_query_supported()``, C1), reading the line
from ``client.version()`` only when the query holds one; 3.x keeps such a view up to date or refuses it itself. On the
guard's one ``INFO FOR DB``, ``remove_view()`` finds the views that read the view it removes (``dependent_views()``,
through the plain ``FROM`` targets ``view_reads_table()`` reads; in a buffered transaction, without those whose
removal the block has already queued, ``dependents_not_removed()``) and, when there is one, refuses on SurrealDB 2.x
and the embedded engine before any DDL (``ensure_view_removable()``, G5): those engines would remove it and turn those
views into ordinary tables that no longer update, where 3.x refuses the removal itself.

**Relation constraints (v0.33.0).** An edge model (``BaseSurrealEdge``) declares its table's ``TYPE RELATION``
constraint with the full SurrealDB-ORM's keys, ``SurrealConfigDict(table_type=TableType.RELATION, relation_in=…,
relation_out=…, enforced=…)``. ``check_relation_config()`` checks them when the class is defined — ``table_type`` a
``TableType`` (``RELATION`` on an edge), the three relation keys on an edge model only — and ``relation_tables()`` reads
``relation_in``/``relation_out``: ``None``, ``""``, ``[]`` or ``()`` for any table, else table names given as one
``str`` (several separated by ``|``) or a ``list``/``tuple``, each stripped and an identifier, kept in order (a repeated
name too, as SurrealDB stores it). ``relation_type_clause()`` writes those names as ``TYPE RELATION[ IN `a` | `b`][ OUT
`c`][ ENFORCED]`` and ``relation_statement()`` the bare statement an edge model's ``relation_ddl()`` returns,
``DEFINE TABLE {OVERWRITE | IF NOT EXISTS} `<table>` <clause>;`` — both pure, the names in backticks. An
``OVERWRITE`` resets every clause it does not write, so for a table that exists ``relation_statement_keeping()`` writes
the stored definition's other clauses back after the type clause, re-rendered from its ``TableState``: ``DROP``,
``SCHEMAFULL``, the comment re-quoted, the change feed, the permissions through ``table_permissions_text()`` (the
model generator's renderer, moved here). The carried clauses are re-checked — no NUL in the comment, each permissions
rule ``FULL``, ``NONE`` or ``WHERE`` with one expression ``scan_fragment()`` accepts — and a value that fails gives
``None``, which ``define_relation()`` refuses. These checks catch a definition the ORM would misread; they are not an
injection defence: the source is the database itself. ``define_relation()``'s guard reads the stored definitions with
the views' ``read_table_definitions()`` (``what="relation"`` names it in a refusal) and ``table_kind()``;
``relation_table_refused()`` gives the texts of its refusals.

**Model table DDL (v0.34.0).** A model declares its table's clauses with the full SurrealDB-ORM's keys,
``SurrealConfigDict(schema_mode=SchemaMode.…, changefeed=…, permissions={…}, comment=…, flexible_fields=[…])``.
``check_table_config()`` checks them when the class is defined, on every model: no ``schema_mode``, ``table_type`` or
``flexible_fields`` on a materialized view (its records are computed by SurrealDB); ``schema_mode`` a ``SchemaMode``,
``SCHEMALESS`` refused on a ``TableType.USER`` table and a declared ``SCHEMAFULL`` refused with ``extra="allow"``; the
change feed through ``changefeed_text()`` (a duration, optionally ``INCLUDE ORIGINAL`` in any case, whitespace folded);
the permissions through ``permissions_rules()`` (a mapping of ``select``, ``create``, ``update`` and ``delete`` to
``FULL``, ``NONE``, ``WHERE <condition>`` or a bare condition, normalised to the four operations, those left out
``NONE``); the comment through ``comment_text()`` (no NUL); ``flexible_fields`` a ``list`` or ``tuple`` of the model's
field names. A permissions condition is one expression ``scan_fragment()`` accepts, with no ``;`` outside brackets: a
check against mistakes, not an injection defence — a rule is trusted application code. The checks validate and never
rewrite: the model keeps every key as declared. ``TableClauses`` holds the clauses a model declares, normalised;
``declared_clauses()`` writes them in SurrealDB's order (schema mode, ``COMMENT``, ``CHANGEFEED``, ``PERMISSIONS``) and
``table_statement()`` the table statement of ``table_ddl()``: the type clause, then those clauses, the table name in
backticks; ``view_statement()`` writes a view's after its ``AS (…)``. For a table that exists,
``table_statement_keeping()`` writes each clause the model declares and keeps each one it does not from the stored
definition (``DROP``, ``SCHEMAFULL``, the comment, the change feed, the permissions) — ``relation_statement_keeping()``
is it with nothing declared — and ``view_statement_keeping()`` does the same around a stored view's ``AS (…)``; a
carried value is re-checked as v0.33.0's are, and one that fails gives ``None``. ``read_table_fields()`` reads a
table's field definitions with one ``INFO FOR TABLE``, keyed by path whichever rendering the line uses, ``None`` for a
definition the parser cannot read. ``table_refused()`` gives the texts of the node models' table guard (TG1–TG4),
``relation_table_refused()`` names ``define_table()`` when it is the caller, and ``relation_schemafull_refused()``
refuses ``define_relation()`` of an edge model declared ``SCHEMAFULL`` with data fields, which it would not define.
``user_extra_refused()`` and ``signup_excluded_refused()`` give the texts of UX1 and SG1, the refusals of a
``SCHEMAFULL`` table the model table DDL checks before any request.

The module sits low in the package: it imports neither ``model_base`` nor ``connection_manager``, so both can import
it.
"""

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, field, replace
from typing import Any, Literal

from ._sdk import ServerError
from .define_parser import (
    _DURATION,
    DefineParseError,
    blank_literals,
    parse_define_field,
    parse_define_table,
    parse_kind,
    read_idiom,
    render_kind,
    scan_fragment,
)
from .exceptions import SurrealDbError
from .functions import normalize_function_name
from .schema_state import FieldState, TableState
from .surql_literal import _SERVER_PARAMETERS, _identifier, _quoted
from .time_travel import line_major
from .types import SchemaMode, TableType
from .utils import VALID_ALIAS_PATTERN, _ddl_refusal_class

__all__ = ["Event", "function_ddl"]

#: The largest ``RETRY`` and ``MAXDEPTH`` SurrealDB accepts (an unsigned 16-bit count).
_MAX_COUNT = 65535

#: A ``WHERE`` permissions rule, matched in full on the stripped value: ``WHERE`` in any case, whitespace, a condition.
_PERMISSIONS_WHERE = re.compile(r"WHERE\s+(.+)", re.IGNORECASE | re.DOTALL)

#: The parameters SurrealDB defines itself, as F6 lists them.
_RESERVED_PARAMETERS = ", ".join(f"${name}" for name in sorted(_SERVER_PARAMETERS))


def _identifier_name(value: object, *, what: str) -> str:
    """*value*, checked as the name of an event or a table (*what*: ``"Event"`` or ``"Table"``).

    ``TypeError`` when it is not a ``str``; ``ValueError`` when it is not an identifier — letters, digits and
    underscores, not starting with a digit, matched in full (so a trailing newline is refused too).
    """
    if not isinstance(value, str):
        raise TypeError(f"{what} name must be a str, got {type(value).__name__}.")
    if VALID_ALIAS_PATTERN.fullmatch(value) is None:
        raise ValueError(
            f"Invalid {what.lower()} name {value!r}: an identifier is expected "
            "(letters, digits, underscores; not starting with a digit)."
        )
    return value


def _fragment(value: object, *, subject: str, what: str, empty: str, one_expression: bool) -> str:
    """*value*, a hand-written SurrealQL fragment, checked and stripped.

    *subject* names the owner in the messages (``"Event 'audit'"``), *what* the argument (``"then="``), *empty* what
    the message for a blank text adds. ``TypeError`` when *value* is not a ``str``; ``ValueError`` when it is blank,
    when ``scan_fragment()`` refuses it (with its reason) and, with *one_expression*, when it holds a ``;`` outside
    brackets.
    """
    if not isinstance(value, str):
        raise TypeError(f"{subject}: {what} must be a str, got {type(value).__name__}.")
    text = value.strip()
    if not text:
        raise ValueError(f"{subject}: {what} is empty; {empty}.")
    try:
        top_level_semicolon = scan_fragment(text)
    except DefineParseError as error:
        raise ValueError(f"{subject}: {what} is not SurrealQL the ORM can embed: {error}.") from error
    if one_expression and top_level_semicolon:
        raise ValueError(f"{subject}: {what} is one expression; it holds a ';' outside brackets.")
    return text


def _comment_text(value: object, *, subject: str) -> str | None:
    """*value*, checked as a ``COMMENT``: ``None``, or a ``str`` without a NUL character (the literal is written when
    the statement is rendered)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{subject}: comment= must be a str, got {type(value).__name__}.")
    if "\x00" in value:
        raise ValueError(f"{subject}: comment= holds a NUL character (\\x00), which SurrealQL cannot carry.")
    return value


def _count(value: object, *, subject: str, what: str) -> int | None:
    """*value*, checked as ``RETRY`` or ``MAXDEPTH``: ``None``, or an ``int`` (not a ``bool``) in ``0..65535``."""
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{subject}: {what} must be an int or None, got {type(value).__name__}.")
    if not 0 <= value <= _MAX_COUNT:
        raise ValueError(f"{subject}: {what}{value} is outside 0..{_MAX_COUNT}, the range SurrealDB accepts.")
    return value


def _flag(value: object, *, what: str) -> bool:
    """*value*, checked as the ``bool`` argument *what* (``"overwrite="``, ``"missing_ok="``)."""
    if not isinstance(value, bool):
        raise TypeError(f"{what} must be a bool, got {type(value).__name__}.")
    return value


def _overwrite_clause(value: object) -> str:
    """``OVERWRITE`` for ``overwrite=True`` (redefine in place), ``IF NOT EXISTS`` for ``False`` (keep what exists)."""
    return "OVERWRITE" if _flag(value, what="overwrite=") else "IF NOT EXISTS"


def _missing_clause(value: object) -> str:
    """`` IF EXISTS`` for ``missing_ok=True`` (a missing definition is not an error), nothing for ``False``."""
    return " IF EXISTS" if _flag(value, what="missing_ok=") else ""


def _kind(text: str, *, subject: str, what: str) -> str:
    """The canonical text of the SurrealQL kind *text* (``render_kind(parse_kind(text))``), never *text* itself.

    *what* names the kind in the message (``"returns="``, ``"the kind of parameter 'a'"``). A kind the parser cannot
    read — or whose free-text generic argument holds a comment marker, a quote or a ``;`` — is a ``ValueError``:
    nothing is rewritten silently.
    """
    try:
        return render_kind(parse_kind(text))
    except DefineParseError as error:
        raise ValueError(f"{subject}: {what} is not a SurrealQL kind the ORM can read: {error}.") from error


def _parameters(value: object, *, subject: str) -> str:
    """The parameter list of a ``DEFINE FUNCTION`` — ``$a: int, $b: option<string>`` — from the mapping *value*.

    Names are identifiers without the ``$`` and not one of the parameters SurrealDB defines itself; kinds are checked
    and written by :func:`_kind`. The mapping's order is the declaration order.
    """
    if value is None:
        return ""
    if not isinstance(value, Mapping):
        raise TypeError(
            f"{subject}: parameters= must be a mapping of parameter name to SurrealQL kind, got {type(value).__name__}."
        )
    rendered: list[str] = []
    for parameter, kind in value.items():
        if not isinstance(parameter, str) or VALID_ALIAS_PATTERN.fullmatch(parameter) is None:
            raise ValueError(f"{subject}: invalid parameter name {parameter!r}: an identifier is expected, without the '$'.")
        if parameter in _SERVER_PARAMETERS:
            raise ValueError(
                f"{subject}: the parameter name {parameter!r} is one SurrealDB defines itself "
                f"({_RESERVED_PARAMETERS}); choose another."
            )
        if not isinstance(kind, str):
            raise TypeError(f"{subject}: the kind of parameter {parameter!r} must be a str, got {type(kind).__name__}.")
        rendered.append(f"${parameter}: {_kind(kind, subject=subject, what=f'the kind of parameter {parameter!r}')}")
    return ", ".join(rendered)


def _permissions(value: object, *, subject: str) -> str | None:
    """The rule of a ``PERMISSIONS`` clause — ``FULL``, ``NONE`` or ``WHERE <condition>`` — or ``None`` for none.

    ``FULL`` and ``NONE`` in any case, written upper-case; ``WHERE`` in any case followed by one expression, checked
    as ``when=`` is (``scan_fragment()``, no ``;`` outside brackets) and written stripped.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{subject}: permissions= must be a str or None, got {type(value).__name__}.")
    rule = value.strip()
    if rule.upper() in ("FULL", "NONE"):
        return rule.upper()
    where = _PERMISSIONS_WHERE.fullmatch(rule)
    if where is None:
        raise ValueError(f"{subject}: permissions= must be 'FULL', 'NONE' or 'WHERE <condition>', got {value!r}.")
    condition = _fragment(
        where.group(1),
        subject=subject,
        what="the permissions condition",
        empty="it is the condition a caller must meet",
        one_expression=True,
    )
    return f"WHERE {condition}"


@dataclass(frozen=True, slots=True)
class Event:
    """A server-side event of a table: SurrealDB's ``DEFINE EVENT``, declared as the full SurrealDB-ORM does.

    ::

        Event(
            "email_audit",
            when="$before.email != $after.email",
            then="CREATE audit_log SET record = $value.id, action = $event, at = time::now()",
        )
        Event("notify", when="$event = 'CREATE'", then="CREATE outbox SET id = $after.id", async_mode=True, retry=3)

    - ``name``: the event's name, an identifier (letters, digits, underscores; not starting with a digit).
    - ``then`` (keyword-only, required): the SurrealQL the event runs — one statement, several separated by ``;``, or
      a block. ``$event`` (``'CREATE'``, ``'UPDATE'`` or ``'DELETE'``), ``$before``, ``$after`` and ``$value``
      describe the write that fired it.
    - ``when``: the condition, one expression (no ``;`` outside brackets); ``None`` runs the event on every write.
    - ``async_mode``: ``True`` for SurrealDB 3.x's ``ASYNC`` event, run after the write rather than as part of it.
      ``retry`` and ``max_depth`` — SurrealDB's ``RETRY`` and ``MAXDEPTH``, each ``None`` or an ``int`` in
      ``0..65535`` — apply to such an event only.
    - ``comment``: SurrealDB's ``COMMENT`` (the full ORM's ``DefineEvent(comment=)``), any text without a NUL.

    ``then`` and ``when`` are checked by ``define_parser.scan_fragment()`` and kept stripped: string literals and
    quoted identifiers terminated, brackets balanced, no SurrealQL comment marker (``--``, ``#``, ``//``, ``/* */``)
    and no NUL outside a literal — a comment marker inside a string is data. The check catches mistakes — unbalanced
    brackets, a comment that would swallow the clauses the ORM appends, a second statement where one expression is
    expected — and is not an injection defence: expressions are trusted application code, never user input. Every
    refusal is a ``TypeError`` or a ``ValueError`` raised here, before any request.

    ``ddl(table)`` renders the event's ``DEFINE EVENT`` statement on a table, without a connection.

    Frozen, hashable, equal by value and picklable. ``repr()`` leaves ``when`` and ``then`` out, as ``EventState``
    does: an expression may hold a credential (a webhook token in a URL).
    """

    name: str
    _: KW_ONLY
    then: str = field(repr=False)
    when: str | None = field(default=None, repr=False)
    async_mode: bool = False
    retry: int | None = None
    max_depth: int | None = None
    comment: str | None = None

    def __post_init__(self) -> None:
        """Check every argument in field order — the name first, so every later message names the event."""
        _identifier_name(self.name, what="Event")
        subject = f"Event {self.name!r}"
        then = _fragment(
            self.then, subject=subject, what="then=", empty="it is the SurrealQL the event runs", one_expression=False
        )
        when = (
            None
            if self.when is None
            else _fragment(
                self.when,
                subject=subject,
                what="when=",
                empty="leave it out to run the event on every write",
                one_expression=True,
            )
        )
        if not isinstance(self.async_mode, bool):
            raise TypeError(f"{subject}: async_mode= must be a bool, got {type(self.async_mode).__name__}.")
        retry = _count(self.retry, subject=subject, what="retry=")
        max_depth = _count(self.max_depth, subject=subject, what="max_depth=")
        if not self.async_mode and (retry is not None or max_depth is not None):
            raise ValueError(
                f"{subject}: retry= and max_depth= apply to ASYNC events only; set async_mode=True (SurrealDB 3.x)."
            )
        _comment_text(self.comment, subject=subject)
        object.__setattr__(self, "then", then)
        object.__setattr__(self, "when", when)

    def ddl(self, table: str, *, overwrite: bool = True) -> str:
        """The ``DEFINE EVENT`` statement of this event on *table* — pure: no connection, the same text every time.

        ::

            Event("audit", then="CREATE log SET kind = $event").ddl("person")
            # DEFINE EVENT OVERWRITE `audit` ON `person` THEN { CREATE log SET kind = $event };

        ``overwrite=True`` redefines an existing event (``OVERWRITE``), ``False`` keeps it (``IF NOT EXISTS``). The
        clauses, in order: ``ASYNC`` with ``RETRY``/``MAXDEPTH`` when set, ``WHEN`` when given, ``THEN`` with ``then``
        always wrapped in a ``{ … }`` block, ``COMMENT`` when given. The names are written in backticks, so a keyword
        name (``select``) is a name. *table* is an identifier; ``TypeError``/``ValueError`` otherwise, and when
        *overwrite* is not a ``bool``.
        """
        table = _identifier_name(table, what="Table")
        clauses = [f"DEFINE EVENT {_overwrite_clause(overwrite)} {_identifier(self.name)} ON {_identifier(table)}"]
        if self.async_mode:
            clauses.append("ASYNC")
            if self.retry is not None:
                clauses.append(f"RETRY {self.retry}")
            if self.max_depth is not None:
                clauses.append(f"MAXDEPTH {self.max_depth}")
        if self.when is not None:
            clauses.append(f"WHEN {self.when}")
        clauses.append(f"THEN {{ {self.then} }}")
        if self.comment is not None:
            clauses.append(f"COMMENT {_quoted(self.comment)}")
        return " ".join(clauses) + ";"


def function_ddl(
    function: str,
    body: str,
    *,
    parameters: Mapping[str, str] | None = None,
    returns: str | None = None,
    permissions: str | None = None,
    comment: str | None = None,
    overwrite: bool = True,
) -> str:
    """The ``DEFINE FUNCTION`` statement of a stored function — pure: no connection, the same text every time.

    ::

        function_ddl(
            "fn::greet",
            "RETURN string::concat('Hello ', $name);",
            parameters={"name": "string", "times": "option<int>"},
            returns="string",
            comment="Greets",
        )
        # DEFINE FUNCTION OVERWRITE fn::greet($name: string, $times: option<int>) -> string
        #     { RETURN string::concat('Hello ', $name); } COMMENT 'Greets';   (one line)

    - ``function``: as ``call_function()`` takes it — ``"greet"``, ``"fn::greet"``, ``"fn::billing::total"`` —,
      validated and prefixed by ``normalize_function_name()``.
    - ``body``: the SurrealQL the function runs, one statement or several separated by ``;``; always wrapped in a
      ``{ … }`` block.
    - ``parameters``: a mapping of parameter name (an identifier, without the ``$``; not one of ``$access``,
      ``$auth``, ``$parent``, ``$session``, ``$this``, ``$token``) to its SurrealQL kind, in declaration order — the
      order of ``call_function()``'s positional ``args``.
    - ``returns``: the SurrealQL kind of the result, or ``None``.
    - ``permissions``: ``None`` (SurrealDB's default, ``FULL``), ``"FULL"``, ``"NONE"`` or ``"WHERE <condition>"``,
      in any case; the condition is one expression.
    - ``comment``: SurrealDB's ``COMMENT``, any text without a NUL. ``overwrite``: ``True`` redefines an existing
      function (``OVERWRITE``), ``False`` keeps it (``IF NOT EXISTS``).

    Kinds are parsed and written in canonical form: ``"none | int"`` and ``"int | none"`` are both written
    ``option<int>``, and a kind the ORM cannot read is refused rather than rewritten. The body and the permissions
    condition are checked by ``define_parser.scan_fragment()``. The check catches mistakes — unbalanced brackets, a
    comment that would swallow the clauses the ORM appends, a second statement where one expression is expected — and
    is not an injection defence: expressions are trusted application code, never user input. Every refusal is a
    ``TypeError`` or a ``ValueError`` raised before any request.
    """
    name = normalize_function_name(function)
    subject = f"define_function({name!r})"
    text = _fragment(body, subject=subject, what="body=", empty="it is the SurrealQL the function runs", one_expression=False)
    signature = _parameters(parameters, subject=subject)
    if returns is not None and not isinstance(returns, str):
        raise TypeError(f"{subject}: returns= must be a str or None, got {type(returns).__name__}.")
    result = "" if returns is None else f" -> {_kind(returns, subject=subject, what='returns=')}"
    rule = _permissions(permissions, subject=subject)
    note = _comment_text(comment, subject=subject)
    statement = f"DEFINE FUNCTION {_overwrite_clause(overwrite)} {name}({signature}){result} {{ {text} }}"
    if note is not None:
        statement += f" COMMENT {_quoted(note)}"
    if rule is not None:
        statement += f" PERMISSIONS {rule}"
    return statement + ";"


def remove_event_statement(name: str, table: str, missing_ok: bool) -> str:
    """``REMOVE EVENT [IF EXISTS] `<name>` ON `<table>`;`` — ``IF EXISTS`` with *missing_ok*.

    The names are checked as ``Event`` and ``Event.ddl()`` check them, *missing_ok* must be a ``bool``.
    """
    name = _identifier_name(name, what="Event")
    table = _identifier_name(table, what="Table")
    return f"REMOVE EVENT{_missing_clause(missing_ok)} {_identifier(name)} ON {_identifier(table)};"


def remove_function_statement(function: str, missing_ok: bool) -> str:
    """``REMOVE FUNCTION [IF EXISTS] fn::<name>;`` — ``IF EXISTS`` with *missing_ok*.

    *function* is normalised by ``normalize_function_name()`` (``"greet"`` → ``fn::greet``), *missing_ok* must be a
    ``bool``.
    """
    name = normalize_function_name(function)
    return f"REMOVE FUNCTION{_missing_clause(missing_ok)} {name};"


def event_subject(name: str, table: str) -> str:
    """How a refusal names the event *name* of *table* (``'email_audit' on table 'account'``) — never by its statement,
    whose ``when`` and ``then`` may hold a secret (``apply_ddl_statements(subjects=)``)."""
    return f"{name!r} on table {table!r}"


def check_events_config(model: type, value: object) -> None:
    """The ``events`` key of a model's ``SurrealConfigDict``, checked when the class is defined (v0.31.0).

    ``None`` (no events) is not passed here. *value* must be a ``list`` or a ``tuple`` of ``Event`` with distinct
    names: two events of one table cannot share a name, the second definition would replace the first. Each ``Event``
    was checked when it was built. The messages name *model*.

    :raises TypeError: *value* is not a ``list`` or a ``tuple``, or holds something other than an ``Event``.
    :raises ValueError: two events share a name.
    """
    key = f"{model.__name__}: model_config['events']"
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{key} must be a list or tuple of Event, got {type(value).__name__}.")
    names: set[str] = set()
    for event in value:
        if not isinstance(event, Event):
            raise TypeError(f"{key} holds a {type(event).__name__}, not an Event.")
        if event.name in names:
            raise ValueError(f"{key} declares the event {event.name!r} more than once.")
        names.add(event.name)


async def ensure_async_events_supported(client: Any, events: Iterable[Event], *, embedded: bool) -> None:
    """Refuse ``ASYNC`` events on a connection that runs SurrealDB before 3.x, before any DDL is sent.

    ``ASYNC`` events exist on SurrealDB 3.x only: 2.x servers and the embedded engine fail to parse the clause. When
    one of *events* has ``async_mode=True``, the line is read from ``client.version()`` — once, and only then, so
    applying synchronous events costs no request — with the lite's rule (``time_travel.line_major()``): a version
    that cannot be read counts as 3.x and the server decides. *embedded* says the connection is the embedded engine,
    named so in the message.

    On the embedded engine ``version()`` answers the SDK's version (``surrealdb-2.0.0``), not its bundled core's
    (2.3.10); both are 2.x today. Should an SDK release ship a 3.x core under a 2.x version, this check would refuse
    an ``ASYNC`` event that core could run — an accepted, documented limit while the SDK bundles a 2.x core.

    :param client: the SDK client of the connection the events are applied on (outside any transaction:
        ``version()`` takes none).
    :raises SurrealDbError: an event has ``async_mode=True`` and the connection runs SurrealDB before 3.x; the
        message names every such event.
    """
    names = [event.name for event in events if event.async_mode]
    if not names:
        return
    major = line_major(await client.version())
    if major >= 3:
        return
    engine = "the embedded engine (a SurrealDB 2.x core)" if embedded else f"SurrealDB {major}.x"
    listed = ", ".join(repr(name) for name in names)
    raise SurrealDbError(
        f"ASYNC events need SurrealDB 3.x, and this connection runs {engine}: {listed} cannot be defined with "
        "async_mode=True. Drop async_mode, retry and max_depth, or connect to a SurrealDB 3.x server. Nothing was "
        "defined."
    )


# --- materialized views (v0.32.0) ----------------------------------------------------------------------------------

#: The aggregates SurrealDB keeps up to date in a grouped view on every line, when one call is the whole element.
_VIEW_AGGREGATES = ("count", "math::sum", "math::mean", "math::min", "math::max", "time::min", "time::max")

#: The aggregates SurrealDB 3.x keeps up to date in a grouped view, and 2.x computes once per source record.
_VIEW_AGGREGATES_3X = ("math::stddev", "math::variance")

_ALL_VIEW_AGGREGATES = _VIEW_AGGREGATES + _VIEW_AGGREGATES_3X

#: The functions SurrealDB documents as unusable in a view: refused in a grouped projection outside an aggregate's
#: argument, on every line (V7).
_NOT_VIEW_AGGREGATES = ("array::distinct", "array::group", "array::join", "math::median", "vector::sum")

#: The words of a projection element that are never fields (compared upper-cased).
_PROJECTION_KEYWORDS = frozenset(
    {
        "AND",
        "OR",
        "NOT",
        "IS",
        "IN",
        "INSIDE",
        "NOTINSIDE",
        "ALLINSIDE",
        "ANYINSIDE",
        "NONEINSIDE",
        "OUTSIDE",
        "INTERSECTS",
        "CONTAINS",
        "CONTAINSNOT",
        "CONTAINSALL",
        "CONTAINSANY",
        "CONTAINSNONE",
        "IF",
        "THEN",
        "ELSE",
        "END",
        "NONE",
        "NULL",
        "TRUE",
        "FALSE",
        "AS",
        "NAN",
        "INFINITY",
    }
)

_OPENING = frozenset("([{")
_CLOSING = frozenset(")]}")

#: ``SELECT`` and whitespace, at the start of a view query (V3).
_VIEW_SELECT = re.compile(r"SELECT\s", re.IGNORECASE)
_VIEW_SELECT_VALUE = re.compile(r"SELECT\s+VALUE\b", re.IGNORECASE)
_VIEW_GROUP = re.compile(r"\bGROUP\s+(BY|ALL)\b", re.IGNORECASE)
#: The ``FROM`` keyword — not the word ``from`` of a function path (``time::from::unix()``), a field path or a
#: parameter.
_VIEW_FROM = re.compile(r"(?<![\w$.:])FROM(?![\w:])", re.IGNORECASE)
#: The first clause after a view query's ``FROM`` targets (``view_reads_table()``).
_VIEW_TARGETS_END = re.compile(
    r"\b(?:WHERE|GROUP|SPLIT|ORDER|LIMIT|START|FETCH|TIMEOUT|PARALLEL|EXPLAIN|WITH)\b", re.IGNORECASE
)
#: A projection element's alias, at its end: ``… AS name``, ``… AS `a name```, ``… AS ⟨a name⟩``.
_VIEW_ALIAS = re.compile(r"\s+AS\s+(`[^`]*`|⟨[^⟩]*⟩|[^\s`⟨]+)\s*$", re.IGNORECASE)
#: A sub-select or a graph path: an element the reading leaves to the server.
_VIEW_OPAQUE = re.compile(r"\bSELECT\b|->|<-", re.IGNORECASE)
_VIEW_CALL = re.compile(r"(?<![\w$.:])([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\s*\(")
#: A name or an ``a.b`` path a projection element reads — not one followed by ``(`` or ``:`` (a call, an object key,
#: a record id's table), nor by a quote: the prefix of a typed literal (``d'…'``, ``r"…"``, ``u'…'``, ``s'…'``,
#: ``b"…"``), whose delimiters ``blank_literals()`` keeps.
_VIEW_FIELD = re.compile(r"(?<![\w$.:])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)(?![\w.'\"])(?!\s*[(:])")
_VIEW_CAST = re.compile(r"<\s*[A-Za-z_][\w:]*(?:\s*<[^<>]*>)?\s*>")
_VIEW_PARAMETER = re.compile(r"\$[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")

#: The first characters of a view query ``check_view_query()`` accepted: ``SELECT`` (where the projection starts).
_SELECT_LENGTH = len("SELECT")


@dataclass(frozen=True, slots=True)
class ViewElement:
    """One element of a view query's projection, as :func:`view_query_outline` reads it (v0.32.0; private).

    - ``text``: the expression as written, without its alias, stripped.
    - ``alias``: the name after ``AS`` (one pair of backquotes or ``⟨⟩`` removed), or ``None``.
    - ``aggregate``: the lower-cased name of the aggregate when the element is one whole call of it
      (``math::sum(amount)``), else ``None``.
    - ``calls``: the lower-cased names of the functions it calls (a ``::`` path, or ``count``), in order, nested calls
      included.
    - ``free_calls``: the calls of ``calls`` that stand outside the argument of every call of an aggregate SurrealDB
      keeps up to date (``count``, ``math::sum`` … ``math::variance``), in order — the aggregate calls themselves
      included, the calls inside their arguments left out (v0.32.1, issue #286): ``("math::sum", "math::sum")`` for
      ``math::sum(a) + math::sum(array::len(array::distinct(tags)))``.
    - ``fields``: the fields it reads, in order, a repeated one repeated (``("*",)`` for ``*``); ``()`` when
      ``aggregate`` is set — the aggregate's argument is read per record, as SurrealDB intends.
    - ``opaque``: it holds a sub-select or a graph path, which the reading does not analyse (``calls``,
      ``free_calls`` and ``fields`` are then empty).
    """

    text: str
    alias: str | None
    aggregate: str | None
    calls: tuple[str, ...]
    free_calls: tuple[str, ...]
    fields: tuple[str, ...]
    opaque: bool


@dataclass(frozen=True, slots=True)
class ViewOutline:
    """A view query's projection and grouping, as :func:`view_query_outline` reads it (v0.32.0; private).

    ``grouped`` for a top-level ``GROUP BY`` or ``GROUP ALL``; ``group`` the ``GROUP BY`` items (one pair of
    backquotes or ``⟨⟩`` removed; ``()`` for ``GROUP ALL`` or no group); ``elements`` the projection, in order.
    """

    grouped: bool
    group: tuple[str, ...]
    elements: tuple[ViewElement, ...]


@dataclass(frozen=True, slots=True)
class ViewHazard:
    """A grouped projection element SurrealDB 2.x computes once per source record (v0.32.0; private).

    ``kind``: ``"aggregate"`` (a whole ``math::stddev()``/``math::variance()`` call), ``"wrapped"`` (an expression
    around an aggregate), ``"field"`` (a field read outside ``GROUP BY`` and outside an aggregate) or ``"alias"`` (a
    ``GROUP BY`` item that is the alias of another expression). ``element`` is the element's text without its alias;
    ``detail`` the aggregate, the field or the alias concerned.
    """

    kind: Literal["aggregate", "wrapped", "field", "alias"]
    element: str
    detail: str


def check_view_query(value: object, *, model: str) -> str:
    """A model's ``view_query``, checked: the ``SELECT`` a materialized view is computed from (v0.32.0).

    Returns the query stripped, with **one** final ``;`` dropped (a copy-paste habit) — the text ``view_ddl()``
    embeds. In order, each refusal naming *model* (the class name):

    - V1, ``TypeError``: *value* is not a ``str``;
    - V2, ``ValueError``: it is empty;
    - V3: it does not start with ``SELECT`` and whitespace (in any case);
    - V4: ``define_parser.scan_fragment()`` refuses it — an unterminated literal, unbalanced brackets, a SurrealQL
      comment marker, a NUL —, with that reason;
    - V5: it holds another ``;`` outside brackets (``"SELECT 1; SELECT 2"``, ``"SELECT * FROM t;;"``);
    - V6: it is a ``SELECT VALUE``: a view stores records;
    - V7: it groups its records (a top-level ``GROUP BY`` or ``GROUP ALL``) and its projection calls one of
      ``array::distinct``, ``array::group``, ``array::join``, ``math::median``, ``vector::sum`` (in any case) outside
      the argument of every aggregate call (``ViewElement.free_calls``): SurrealDB documents them as unusable in a
      view. Inside an aggregate's argument (``math::max(array::len(array::distinct(tags)))``) they work on every line,
      also when an element combines several aggregates (``math::sum(amount) +
      math::sum(array::len(array::distinct(tags)))``, since v0.32.1): such an expression around aggregates passes V7,
      and ``define_view()`` refuses it on SurrealDB 2.x and the embedded engine (C1). Beside or around an aggregate
      (``math::sum(a) + array::len(array::distinct(tags))``) they are still refused.

    The check catches mistakes — unbalanced brackets, a comment that would swallow the parenthesis the ORM appends, a
    second statement where one SELECT is expected — and is not an injection defence: a view's query is trusted
    application code, never user input. To filter on a value, store it in a record or a ``DEFINE PARAM`` and read it
    from the query; never format it into the text. The hazards that depend on the server line (C1) are not checked
    here. Pure: no connection.
    """
    key = f"{model}: model_config['view_query']"
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a str, got {type(value).__name__}.")
    text = value.strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    if not text:
        raise ValueError(f"{key} is empty; it is the SELECT SurrealDB computes the view from.")
    if _VIEW_SELECT.match(text) is None:
        raise ValueError(f"{key} must be a SELECT statement (it starts with SELECT).")
    try:
        top_level_semicolon = scan_fragment(text)
    except DefineParseError as error:
        raise ValueError(f"{key} is not SurrealQL the ORM can embed: {error}.") from error
    if top_level_semicolon:
        raise ValueError(f"{key} is one SELECT; it holds a ';' outside brackets.")
    if _VIEW_SELECT_VALUE.match(text) is not None:
        raise ValueError(f"{key} uses SELECT VALUE; a view stores records, so select fields (SELECT a, b FROM …).")
    outline = view_query_outline(text)
    if outline.grouped:
        unusable = next(
            (call for element in outline.elements for call in element.free_calls if call in _NOT_VIEW_AGGREGATES),
            None,
        )
        if unusable is not None:
            raise ValueError(
                f"{key} groups its records and calls {unusable}(), which SurrealDB cannot keep up to date in a view; "
                "use count(), math::sum(), math::mean(), math::min(), math::max(), time::min() or time::max()."
            )
    return text


def check_view_config(model: type, value: object, *, edge: bool, user: bool, computed: Sequence[str]) -> None:
    """The ``view_query`` key of a model's ``SurrealConfigDict``, checked when the class is defined (v0.32.0).

    ``None`` (an ordinary model) is not passed here. *value* goes through :func:`check_view_query` (V1–V7), then the
    model itself, the messages naming *model*:

    - V8: *edge* — an edge model (``BaseSurrealEdge``): SurrealDB computes a view's records, the ORM never relates them;
    - V9: *user* — the model mixes in ``AuthenticatedUserMixin``: signing up writes a record;
    - V10: *computed* — the model's computed field names, in declaration order: a view's columns come from its
      ``SELECT``.

    :raises TypeError: V1, V8, V9, V10.
    :raises ValueError: V2–V7.
    """
    name = model.__name__
    check_view_query(value, model=name)
    if edge:
        raise TypeError(
            f"{name}: view_query is not supported on an edge model (BaseSurrealEdge): SurrealDB computes a view's "
            "records, the ORM never relates them."
        )
    if user:
        raise TypeError(
            f"{name}: view_query is not supported with AuthenticatedUserMixin: signing up writes a record, and "
            "SurrealDB computes a view's records."
        )
    if computed:
        names = ", ".join(repr(field_name) for field_name in computed)
        raise TypeError(
            f"{name}: view_query is not supported with computed fields ({names}): a view's columns come from its "
            "SELECT, so compute them there."
        )


def _top_level(text: str) -> tuple[str, str]:
    """``(blanked, top)`` for *text*, all three strings of the same length (v0.32.0).

    ``blanked`` is ``define_parser.blank_literals(text)``; ``top`` is ``blanked`` with every character strictly inside
    ``()``, ``[]`` or ``{}`` replaced by a space, the outermost brackets kept: a keyword or a comma of ``top`` stands
    outside every literal and every bracket of *text*, at the same position. For a text ``scan_fragment()`` accepted.
    """
    blanked = blank_literals(text)
    top: list[str] = []
    depth = 0
    for char in blanked:
        if char in _CLOSING:
            depth = max(depth - 1, 0)
        top.append(char if depth == 0 else " ")
        if char in _OPENING:
            depth += 1
    return blanked, "".join(top)


def _top_spans(top: str, start: int, stop: int) -> list[tuple[int, int]]:
    """The spans of ``top[start:stop]`` between its commas (the commas of *top* stand outside every bracket)."""
    spans: list[tuple[int, int]] = []
    begin = start
    for index in range(start, stop):
        if top[index] == ",":
            spans.append((begin, index))
            begin = index + 1
    spans.append((begin, stop))
    return spans


def _unquoted_name(name: str) -> str:
    """*name* without one pair of enclosing backquotes or ``⟨⟩``, its content as written."""
    if len(name) >= 2 and ((name[0] == "`" and name[-1] == "`") or (name[0] == "⟨" and name[-1] == "⟩")):
        return name[1:-1]
    return name


def _closing_bracket(blanked: str, opening: int) -> int | None:
    """The position of the bracket that closes the one at *opening* in *blanked*, or ``None``."""
    depth = 0
    for index in range(opening, len(blanked)):
        char = blanked[index]
        if char in _OPENING:
            depth += 1
        elif char in _CLOSING:
            depth -= 1
            if depth == 0:
                return index
    return None


def _spaces(match: re.Match[str]) -> str:
    return " " * len(match.group())


def _view_fields(blanked: str) -> tuple[str, ...]:
    """The fields the blanked expression *blanked* reads: casts and parameters first blanked, then every name or
    ``a.b`` path that is not a function, a ``::`` path, an object key, a record id's table, a typed literal's prefix
    (``d'…'``) or a keyword."""
    if blanked == "*":
        return ("*",)
    plain = _VIEW_PARAMETER.sub(_spaces, _VIEW_CAST.sub(_spaces, blanked))
    return tuple(name for name in _VIEW_FIELD.findall(plain) if name.upper() not in _PROJECTION_KEYWORDS)


def _view_calls(blanked: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(calls, free_calls)`` of the blanked expression *blanked*, as :class:`ViewElement` describes them.

    Each call is read in order; one that starts inside the argument of an aggregate call read before it (up to that
    call's closing bracket, found on the blanked text, so a bracket inside a literal is never one) is a call, not a free
    call (issue #286)."""
    calls: list[str] = []
    free_calls: list[str] = []
    argument_end = -1
    for match in _VIEW_CALL.finditer(blanked):
        name = match.group(1).lower()
        if "::" not in name and name != "count":
            continue
        calls.append(name)
        if match.start() < argument_end:
            continue
        free_calls.append(name)
        if name in _ALL_VIEW_AGGREGATES:
            closing = _closing_bracket(blanked, match.end() - 1)
            argument_end = len(blanked) if closing is None else closing
    return tuple(calls), tuple(free_calls)


def _view_element(text: str, blanked: str, top: str) -> ViewElement:
    """One projection element from its three slices (*text*, *blanked*, *top*, of the same length)."""
    alias: str | None = None
    cut = len(text)
    named = _VIEW_ALIAS.search(top)
    if named is not None:
        alias = _unquoted_name(text[named.start(1) : named.end(1)])
        cut = named.start()
    expression = text[:cut]
    left = len(expression) - len(expression.lstrip())
    right = len(expression.rstrip())
    written = expression[left:right]
    plain = blanked[left:right] if right > left else ""
    if _VIEW_OPAQUE.search(plain) is not None:
        return ViewElement(written, alias, None, (), (), (), True)
    calls, free_calls = _view_calls(plain)
    aggregate: str | None = None
    first = _VIEW_CALL.match(plain)
    if (
        first is not None
        and first.group(1).lower() in _ALL_VIEW_AGGREGATES
        and _closing_bracket(plain, first.end() - 1) == len(plain) - 1
    ):
        aggregate = first.group(1).lower()
    fields = () if aggregate is not None else _view_fields(plain)
    return ViewElement(written, alias, aggregate, calls, free_calls, fields, False)


def view_query_outline(text: str) -> ViewOutline:
    """The projection and grouping of a view query *text* that :func:`check_view_query` accepted (v0.32.0).

    It is a reading of the text, not a parser: it serves the line checks (V7, C1) and leaves what it cannot read to
    the server. Literals are blanked first (``define_parser.blank_literals()``), so a keyword, a call or a comma inside
    a string or a quoted name is never read. ``grouped`` is a top-level ``GROUP BY``/``GROUP ALL``; the projection
    runs from ``SELECT`` to the top-level ``FROM`` (or the end) and is split at its top-level commas; each element's
    alias, calls, free calls, whole aggregate and fields are read as :class:`ViewElement` describes. A sub-select or a
    graph path (``->``, ``<-``) makes the element opaque, and a backquoted or ``⟨⟩`` field name is invisible: both are
    left to the server. Pure.
    """
    blanked, top = _top_level(text)
    grouping = _VIEW_GROUP.search(top)
    clause = _VIEW_FROM.search(top, _SELECT_LENGTH)
    end = len(text) if clause is None else clause.start()
    elements = tuple(
        _view_element(text[start:stop], blanked[start:stop], top[start:stop])
        for start, stop in _top_spans(top, _SELECT_LENGTH, end)
    )
    group: tuple[str, ...] = ()
    if grouping is not None and grouping.group(1).upper() == "BY":
        group = tuple(_unquoted_name(text[start:stop].strip()) for start, stop in _top_spans(top, grouping.end(), len(text)))
    return ViewOutline(grouped=grouping is not None, group=group, elements=elements)


def _element_hazard(element: ViewElement, group: tuple[str, ...]) -> ViewHazard | None:
    """The hazard of one element of a grouped projection (rules 1–4 of :func:`view_query_hazards`), or ``None``."""
    if element.alias is not None and element.alias in group:
        return None if element.text == element.alias else ViewHazard("alias", element.text, element.alias)
    aggregates = [call for call in element.calls if call in _ALL_VIEW_AGGREGATES]
    if element.aggregate is not None and len(aggregates) == 1:
        if element.aggregate in _VIEW_AGGREGATES_3X:
            return ViewHazard("aggregate", element.text, element.aggregate)
        return None
    if aggregates:
        return ViewHazard("wrapped", element.text, aggregates[0])
    for name in element.fields:
        if not any(name == item or name.startswith(item + ".") for item in group):
            return ViewHazard("field", element.text, name)
    return None


def view_query_hazards(outline: ViewOutline) -> tuple[ViewHazard, ...]:
    """The elements of a grouped projection that SurrealDB 2.x computes once per source record (v0.32.0).

    ``()`` when *outline* is not grouped. Else, per element in projection order, at most one hazard each:

    1. its alias is a ``GROUP BY`` item and the element is something else → ``"alias"`` (2.x groups by the source
       records' own fields, so every record falls into one group); an element named as its group item is fine;
    2. it is one whole call of an aggregate, with no other aggregate inside → ``"aggregate"`` for
       ``math::stddev()``/``math::variance()``, nothing for the seven 2.x keeps up to date;
    3. it calls an aggregate otherwise (an expression around it) → ``"wrapped"``, naming the first;
    4. it reads a field that is neither a ``GROUP BY`` item nor under one (``c.country.code`` under ``c.country``) →
       ``"field"``, naming the first (``*`` for ``*``).

    SurrealDB 3.x keeps the first three up to date and refuses the fourth at define. Pure.
    """
    if not outline.grouped:
        return ()
    hazards = (_element_hazard(element, outline.group) for element in outline.elements)
    return tuple(hazard for hazard in hazards if hazard is not None)


def view_statement(table: str, query: str, overwrite: object, clauses: "TableClauses | None" = None) -> str:
    """``DEFINE TABLE {OVERWRITE | IF NOT EXISTS} `<table>` AS (<query>)[ COMMENT '…'][ CHANGEFEED …][ PERMISSIONS
    …];`` — the full SurrealDB-ORM's form (v0.32.0), with the clauses a view model declares (v0.34.0).

    *table* is checked as an identifier and written in backticks; *query* is a text :func:`check_view_query` returned,
    embedded as it is inside one pair of parentheses. *clauses* (``None``: ``TableClauses()``, nothing declared) are
    written as :func:`declared_clauses` writes them, after ``AS (…)`` — a schema mode, which a view model cannot
    declare, before it. ``overwrite=True`` recomputes an existing view (``OVERWRITE``), ``False`` keeps it (``IF NOT
    EXISTS``); a non-``bool`` is a ``TypeError``. Pure.
    """
    return _view_statement(table, query, overwrite, TableClauses() if clauses is None else clauses, drop=False)


def remove_view_statement(table: str, missing_ok: object) -> str:
    """``REMOVE TABLE [IF EXISTS] `<table>`;`` — ``IF EXISTS`` with *missing_ok* (v0.32.0).

    *table* is checked as an identifier and written in backticks; *missing_ok* must be a ``bool``. Pure.
    """
    table = _identifier_name(table, what="Table")
    return f"REMOVE TABLE{_missing_clause(missing_ok)} {_identifier(table)};"


def view_write_refused(table: str, call: str) -> str:
    """W1's text: *call* (``"OrderStats.save()"``) would write to the materialized view *table* (v0.32.0).

    SurrealDB computes a view's records from its query, so every ORM write of a view model is refused with this text
    before any signal, transaction use or request (``BaseSurrealModel._reject_view_write()``). It starts with the full
    SurrealDB-ORM's ``Cannot modify materialized view '<table>'``. Pure.
    """
    return (
        f"Cannot modify materialized view '{table}': {call} writes to it, and SurrealDB computes a view's records "
        "from its view_query. Write to the tables it selects from instead."
    )


# --- the table guard of define_view() and remove_view() (v0.32.0) ---------------------------------------------------

#: The guard's one read of the database's definitions.
_INFO_FOR_DB = "INFO FOR DB;"

#: What ``table_kind()`` answers for a table that exists: a view, an ordinary table, or a definition it cannot read.
_TableKind = Literal["view", "table", "unreadable"]


def _guard_read_refused(statement: str, error: BaseException, *, kind: str, table: str, what: str = "view") -> SurrealDbError:
    """The refusal of one of the guard's reads (*statement*): S1 when the session may not read, else S3 — the text of
    a refused DDL of *what*, ``Can't apply {what} {kind}: '<table>' -> <the server's message>`` (``view`` for
    v0.32.0's guard, ``relation`` for ``define_relation()``'s, v0.33.0)."""
    refusal = _ddl_refusal_class(statement, error)
    return refusal(f"Can't apply {what} {kind}: {table!r} -> {error}")


async def read_table_definitions(client: Any, table: str, *, kind: str, what: str = "view") -> dict[str, Any]:
    """The ``tables`` map of ``INFO FOR DB`` — table name → its ``DEFINE TABLE`` statement — read once by the table
    guard of ``define_view()`` and ``remove_view()`` (v0.32.0) and of ``define_relation()`` (v0.33.0).

    A copy; ``{}`` when the answer is not a mapping or holds no ``tables`` mapping (a database never defined answers
    ``tables: {}``). It reads committed state, on *client* outside any transaction.

    :param kind: ``"definition"`` or ``"removal"``, for the message of a refusal; *table* only names the table there.
    :param what: what is applied, for the message of a refusal: ``"view"`` (the default) or ``"relation"``.
    :raises SurrealDbAuthenticationError: the session may not read the database's definitions (a record user).
    :raises SurrealDbError: any other refusal of the ``INFO``.
    """
    try:
        info = await client.query(_INFO_FOR_DB, {})
    except (ServerError, SurrealDbError, RuntimeError) as error:
        raise _guard_read_refused(_INFO_FOR_DB, error, kind=kind, table=table, what=what) from error
    tables = info.get("tables") if isinstance(info, dict) else None
    return dict(tables) if isinstance(tables, dict) else {}


def table_kind(tables: Mapping[str, Any], table: str) -> _TableKind | None:
    """What *table* is in *tables* (``read_table_definitions()``'s map): ``None`` when it does not exist, ``"view"``
    when its definition has an ``AS`` clause, ``"table"`` when it has none, ``"unreadable"`` when the definition is
    not a ``str`` or ``define_parser.parse_define_table()`` cannot read it (v0.32.0). Pure."""
    definition = tables.get(table)
    if definition is None:
        return None
    if not isinstance(definition, str):
        return "unreadable"
    try:
        state = parse_define_table(definition)
    except DefineParseError:
        return "unreadable"
    return "view" if state.view_query is not None else "table"


async def table_holds_records(client: Any, table: str, *, kind: str) -> bool:
    """Whether *table* holds a record: ``SELECT id FROM `<table>` LIMIT 1;`` answers a row (v0.32.0).

    The table guard of ``define_view()`` sends it only for an ordinary or unreadable table with ``overwrite=True``,
    on *client* outside any transaction. *table* is checked as an identifier and written in backticks.

    :param kind: ``"definition"`` or ``"removal"``, for the message of a refusal.
    :raises SurrealDbAuthenticationError: the session may not read the table.
    :raises SurrealDbError: any other refusal of the ``SELECT``.
    """
    statement = f"SELECT id FROM {_identifier(_identifier_name(table, what='Table'))} LIMIT 1;"
    try:
        rows = await client.query(statement, {})
    except (ServerError, SurrealDbError, RuntimeError) as error:
        raise _guard_read_refused("SELECT", error, kind=kind, table=table) from error
    return isinstance(rows, list) and len(rows) > 0


def view_table_refused(guard: Literal["G1", "G2", "G3", "G4"], model: str, table: str, *, removal: bool = False) -> str:
    """The table guard's refusals (v0.32.0): the text of *guard* for the view model *model* on *table*. Pure.

    - ``G1``: ``define_view()`` would turn an ordinary table that holds records into the view, deleting them;
    - ``G2``: ``remove_view()`` would remove an ordinary table;
    - ``G3``: the table's definition cannot be read (``removal`` says which applier refuses);
    - ``G4``: ``define_view(overwrite=False)`` would leave an ordinary table as it is (``IF NOT EXISTS``).
    """
    if guard == "G1":
        return (
            f"{model}.define_view(): the table '{table}' exists, is not a view and holds records; defining the view "
            "would delete them, with the table's fields, indexes and events. If they are not needed, remove the table "
            f'first (await {model}.raw_query("REMOVE TABLE `{table}`;")); otherwise give the model another '
            "table_name. Nothing was defined."
        )
    if guard == "G2":
        return (
            f"{model}.remove_view(): the table '{table}' is not a view; remove_view() removes views only (removing it "
            "would delete its records, fields, indexes and events). Nothing was removed."
        )
    if guard == "G4":
        return (
            f"{model}.define_view(overwrite=False): the table '{table}' exists and is not a view; DEFINE TABLE IF NOT "
            f"EXISTS would leave it as it is, and {model} would not read a view. Call define_view() with "
            "overwrite=True, which turns a table that holds no record into the view. Nothing was defined."
        )
    method, done = ("remove_view()", "removed") if removal else ("define_view()", "defined")
    return (
        f"{model}.{method}: the definition of the table '{table}' could not be read, so the ORM cannot tell whether "
        f"it is a view. Nothing was {done}."
    )


# --- the line check of define_view(): C1 (v0.32.0) ------------------------------------------------------------------


def _engine_name(major: int, embedded: bool) -> str:
    """How a refusal names an engine before SurrealDB 3.x (v0.32.0's C1 and G5): ``the embedded engine (a SurrealDB 2.x
    core)`` when *embedded*, else ``SurrealDB <major>.x`` — v0.31.0's wording for ``ASYNC`` events. Pure."""
    return "the embedded engine (a SurrealDB 2.x core)" if embedded else f"SurrealDB {major}.x"


def view_hazard_refused(model: str, hazard: ViewHazard, engine: str) -> str:
    """C1's texts (C1a–C1d): *hazard* — the first of the view's grouped projection — on *engine*, before 3.x (v0.32.0).

    The text quotes the projection element (``repr()``), the model's own class configuration, checked before any
    request (ruling D9): no statement is sent. Pure.
    """
    element, detail = hazard.element, hazard.detail
    if hazard.kind == "aggregate":
        return (
            f"{model}.define_view(): {detail}() keeps a grouped view up to date on SurrealDB 3.x only, and this "
            f"connection runs {engine}, which accepts such a view over an empty table and then refuses every write to "
            "that table. Drop it from view_query, or connect to a SurrealDB 3.x server. Nothing was defined."
        )
    if hazard.kind == "wrapped":
        return (
            f"{model}.define_view(): view_query selects {element!r}, an expression around the aggregate {detail}(), "
            f"in a grouped view; SurrealDB 3.x keeps it up to date, and this connection runs {engine}, which computes "
            "it once per source record: the view would hold a wrong value, or every write to the source table would "
            f"be refused. Select {detail}() alone, as its own field, and compute the rest when you read the model; or "
            "connect to a SurrealDB 3.x server. Nothing was defined."
        )
    if hazard.kind == "field":
        if detail == "*":
            reading, remedy = "every field", "Select the GROUP BY fields and the aggregates by name instead of '*'."
        else:
            reading = f"the field {detail!r}"
            remedy = f"Use {detail!r} inside an aggregate (math::max(), math::sum(), …) or add it to GROUP BY."
        return (
            f"{model}.define_view(): view_query selects {element!r} in a grouped view, which reads {reading} outside "
            "an aggregate and outside its GROUP BY; SurrealDB 3.x refuses such a view, and this connection runs "
            f"{engine}, which computes it once per source record: the view would keep the last record's value, or "
            f"every write to the source table would be refused. {remedy} Nothing was defined."
        )
    return (
        f"{model}.define_view(): view_query groups by {detail!r}, the alias of {element!r}; SurrealDB 3.x groups by "
        f"its value, and this connection runs {engine}, which groups by the source records' own fields, so every "
        "record would fall into one group. Group by a field of the source table (store the computed value in the "
        "source records if needed), or connect to a SurrealDB 3.x server. Nothing was defined."
    )


async def ensure_view_query_supported(client: Any, query: str, *, model: str, embedded: bool) -> None:
    """Refuse, on a connection before SurrealDB 3.x, a grouped view that engine computes once per source record —
    before any DDL (C1, v0.32.0).

    SurrealDB 3.x keeps every element of a grouped projection up to date, groups by a projection alias, and refuses
    at define a field read outside ``GROUP BY`` and outside an aggregate. SurrealDB 2.x and the embedded engine keep
    up to date only an element that is one whole call of ``count()``, ``math::sum()``, ``math::mean()``,
    ``math::min()``, ``math::max()``, ``time::min()`` or ``time::max()``, or an expression of the ``GROUP BY`` fields:
    anything else — ``math::stddev()``/``math::variance()``, an expression around an aggregate, a field outside
    ``GROUP BY``, a ``GROUP BY`` on an alias — they compute once per source record, so the view holds a wrong value or
    one group, or every write to the source table is refused. :func:`view_query_hazards` finds those elements in
    *query* (a text :func:`check_view_query` returned) by reading its text; only when it finds one is the line read
    from ``client.version()`` — once, so a view without hazard costs no request — with the lite's rule
    (``time_travel.line_major()``): a version that cannot be read counts as 3.x and the server decides. The first
    hazard in projection order is reported (C1a–C1d, :func:`view_hazard_refused`).

    What the reading cannot see — a sub-select or a graph path in an element, a backquoted field name, a field read
    through a parameter, a field's type — is left to the server. On the embedded engine ``version()`` answers the
    SDK's version (``surrealdb-2.0.0``), whose bundled core is 2.x too; *embedded* names it so in the message.

    :param client: the SDK client of the model's connection (outside any transaction: ``version()`` takes none).
    :param model: the class name the message starts with.
    :raises SurrealDbError: the grouped projection holds a hazard and the connection runs SurrealDB before 3.x.
    """
    hazards = view_query_hazards(view_query_outline(query))
    if not hazards:
        return
    major = line_major(await client.version())
    if major >= 3:
        return
    raise SurrealDbError(view_hazard_refused(model, hazards[0], _engine_name(major, embedded)))


# --- the removal check of remove_view(): G5 (v0.32.0) ----------------------------------------------------------------


def view_reads_table(view_query: str, table: str) -> bool:
    """Whether the view query *view_query* reads *table* through one of its plain ``FROM`` targets (v0.32.0).

    A reading of the text, not a parser: literals and quoted names are blanked and brackets skipped
    (``_top_level()``); the targets run from the top-level ``FROM`` to the first clause after it (``WHERE``,
    ``GROUP``, ``SPLIT``, ``ORDER``, ``LIMIT``, ``START``, ``FETCH``, ``TIMEOUT``, ``PARALLEL``, ``EXPLAIN``,
    ``WITH``) or the end, split at their top-level commas; each, stripped and without one pair of enclosing
    backquotes or ``⟨⟩``, is compared with *table* exactly — so ``FROM `select``` (2.x's rendering) and ``FROM
    select`` (3.x's) both read ``select``. It reads the plain ``FROM`` targets only: a graph path
    (``->knows->person``), a record id (``t:1``), ``ONLY t`` or a sub-select never equals a table name, and a view
    that reads the table that way is left to the server. Pure.
    """
    _, top = _top_level(view_query)
    clause = _VIEW_FROM.search(top)
    if clause is None:
        return False
    end = _VIEW_TARGETS_END.search(top, clause.end())
    stop = len(view_query) if end is None else end.start()
    return any(
        _unquoted_name(view_query[start:finish].strip()) == table for start, finish in _top_spans(top, clause.end(), stop)
    )


def dependent_views(tables: Mapping[str, Any], table: str) -> tuple[str, ...]:
    """The views of *tables* (``read_table_definitions()``'s map) whose query reads *table*, sorted (v0.32.0).

    A view is listed when ``define_parser.parse_define_table()`` reads its definition with an ``AS`` clause whose query
    reads *table* through a plain ``FROM`` target (:func:`view_reads_table`). *table* itself is never listed; an
    ordinary table, a definition that is not a ``str`` and one the parser cannot read are skipped: the server decides
    for them. Pure.
    """
    names: list[str] = []
    for name, definition in tables.items():
        if name == table or not isinstance(definition, str):
            continue
        try:
            view_query = parse_define_table(definition).view_query
        except DefineParseError:
            continue
        if view_query is not None and view_reads_table(view_query, table):
            names.append(name)
    return tuple(sorted(names))


def dependents_not_removed(dependents: Sequence[str], statements: Sequence[str]) -> tuple[str, ...]:
    """The views of *dependents* that the *statements* queued in a buffered transaction do not remove, in order
    (v0.32.1, issue #286).

    G5 (:func:`ensure_view_removable`) reads committed state, where a view whose removal the same buffered block has
    already queued is still listed: removing a chain of views outer view first in one block would be refused. A
    dependent is left out when the last of *statements* that names it — as a whole name, in any case, quoted or not —
    is exactly the removal ``remove_view()`` queues for it (:func:`remove_view_statement`, with ``missing_ok`` ``True``
    or ``False``). It is kept when no statement names it, when a statement names it after its removal (a
    ``DEFINE TABLE … AS`` that defines it again, or any other), when its removal was written by hand
    (``tx.add("REMOVE TABLE …")``), and when its name is not an identifier: a reading of the text, not a parser, that
    errs on the side of the refusal. Pure.
    """
    left: list[str] = []
    for name in dependents:
        try:
            removals = {remove_view_statement(name, True), remove_view_statement(name, False)}
        except (TypeError, ValueError):
            left.append(name)
            continue
        naming = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)", re.IGNORECASE)
        last = next((statement for statement in reversed(statements) if naming.search(statement)), None)
        if last not in removals:
            left.append(name)
    return tuple(left)


async def ensure_view_removable(client: Any, table: str, dependents: Sequence[str], *, model: str, embedded: bool) -> None:
    """Refuse, on a connection before SurrealDB 3.x, to remove the view *table* that other views read — before any
    DDL (G5, v0.32.0).

    While a view reads a table — a source table or an inner view —, SurrealDB 3.x refuses to remove it (``Cannot
    delete table … on which a view is defined``). SurrealDB 2.x and the embedded engine remove it, and every view that
    read it loses its ``AS`` clause: an ordinary table that keeps its last records and no longer updates. *dependents*
    are those views (:func:`dependent_views`, over the table guard's one ``INFO FOR DB``; in a buffered transaction,
    without those whose removal the block has already queued, :func:`dependents_not_removed`): none, and nothing is
    read.
    Else the line is read from ``client.version()`` with the lite's rule (``time_travel.line_major()``): on 3.x — or a
    version that cannot be read — the removal goes on and the server refuses it; before 3.x, G5 names the dependent
    views, sorted. On the embedded engine ``version()`` answers the SDK's version (``surrealdb-2.0.0``), whose bundled
    core is 2.x too; *embedded* names it so in the message.

    :param client: the SDK client of the model's connection (outside any transaction: ``version()`` takes none).
    :param model: the class name the message starts with.
    :raises SurrealDbError: other views read *table* and the connection runs SurrealDB before 3.x.
    """
    if not dependents:
        return
    major = line_major(await client.version())
    if major >= 3:
        return
    names = ", ".join(repr(name) for name in sorted(dependents))
    raise SurrealDbError(
        f"{model}.remove_view(): other views read the table '{table}' ({names}); SurrealDB 3.x refuses to remove it, "
        f"and this connection runs {_engine_name(major, embedded)}, which would remove it and turn those views into "
        "ordinary tables that keep their last records and no longer update. Remove those views first. Nothing was "
        "removed."
    )


# --- relation constraints (v0.33.0) ----------------------------------------------------------------------------------


def relation_tables(value: object, *, model: str, key: str) -> tuple[str, ...]:
    """The table names of a model's ``relation_in`` or ``relation_out`` (*key*), checked (v0.33.0).

    ``None``, ``""``, ``[]`` and ``()`` mean any table: ``()``, no ``IN`` (or ``OUT``) clause. Otherwise *value* is a
    ``str`` naming one table or several separated by ``|`` (SurrealQL's spelling, the full SurrealDB-ORM's pipe-joined
    string: ``"person | company"``), or a ``list`` or ``tuple`` of table names. Each name is stripped and returned in
    order; a repeated name is kept, as SurrealDB stores it (``IN a | a``), so the result compares with
    ``TableState.relation_in``/``relation_out``. In order, each refusal naming *model* (the class name) and *key*:

    - R2, ``TypeError``: *value* is neither a ``str``, a ``list``, a ``tuple`` nor ``None``;
    - R3, ``TypeError``: an item of the ``list`` or ``tuple`` is not a ``str``;
    - R4, ``ValueError``: a non-empty string that is blank (``"  "``);
    - R5, ``ValueError``: a name, after stripping, is not an identifier (letters, digits, underscores; not starting
      with a digit) — ``''`` for a part or an item blank after stripping (``"a||b"``, ``[""]``), ``'a | b'`` for the
      item ``"a | b"`` (an item is one name).

    Pure: no connection.
    """
    subject = f"{model}: model_config['{key}']"
    if value is None:
        return ()
    if isinstance(value, str):
        if not value:
            return ()
        if not value.strip():
            raise ValueError(f"{subject} names no table; leave it out (None) to accept any table.")
        names = tuple(part.strip() for part in value.split("|"))
    elif isinstance(value, (list, tuple)):
        for item in value:
            if not isinstance(item, str):
                raise TypeError(f"{subject} holds a {type(item).__name__}, not a table name.")
        names = tuple(item.strip() for item in value)
    else:
        raise TypeError(
            f"{subject} must be a table name, several separated by '|', or a list or tuple of table names, got "
            f"{type(value).__name__}."
        )
    for name in names:
        if VALID_ALIAS_PATTERN.fullmatch(name) is None:
            raise ValueError(
                f"{subject} names {name!r}, which is not a table name the ORM can write: an identifier is expected "
                "(letters, digits, underscores; not starting with a digit)."
            )
    return names


def check_relation_config(
    model: type,
    *,
    edge: bool,
    table_type: object,
    relation_in: object,
    relation_out: object,
    enforced: object,
) -> None:
    """The ``table_type``, ``relation_in``, ``relation_out`` and ``enforced`` keys of a model's ``SurrealConfigDict``,
    checked when the class is defined (v0.33.0).

    Called when any of the four is not ``None`` (an empty value and ``enforced=False`` included), inherited values
    included. *edge* tells an edge model (``BaseSurrealEdge``) from a node. In order — the first failure raises, the
    messages naming *model*:

    - T1, ``TypeError``: *table_type* is not a ``TableType`` (a plain ``"relation"`` string included);
    - T2, ``TypeError``: an edge model with a *table_type* other than ``TableType.RELATION``;
    - T3, ``TypeError``: ``TableType.RELATION`` on a model that is not an edge (the lite's relation model is
      ``BaseSurrealEdge``);
    - R1, ``TypeError``: *relation_in*, *relation_out* or *enforced* not ``None`` on a model that is not an edge;
    - R2–R5: :func:`relation_tables` of *relation_in*, then of *relation_out*;
    - R7, ``TypeError``: *enforced* is not a ``bool`` (and not ``None``).

    On a node model, the other ``TableType`` members are accepted: returned by ``get_table_type()``, they take effect
    in ``define_table()`` (v0.34.0). The table's other clauses are checked by :func:`check_table_config`, which runs
    after this check.

    :raises TypeError: T1–T3, R1, R2, R3, R7.
    :raises ValueError: R4, R5.
    """
    name = model.__name__
    if table_type is not None:
        if not isinstance(table_type, TableType):
            raise TypeError(
                f"{name}: model_config['table_type'] must be a surreal_orm_lite.TableType, got {type(table_type).__name__}."
            )
        if edge and table_type is not TableType.RELATION:
            raise TypeError(
                f"{name}: an edge model's table is TYPE RELATION; leave table_type out or set TableType.RELATION, "
                f"got TableType.{table_type.name}."
            )
        if not edge and table_type is TableType.RELATION:
            raise TypeError(
                f"{name}: table_type=TableType.RELATION needs an edge model: derive {name} from BaseSurrealEdge, "
                "which declares the endpoints in_ and out."
            )
    if not edge:
        relation = {"relation_in": relation_in, "relation_out": relation_out, "enforced": enforced}
        keys = [key for key, value in relation.items() if value is not None]
        if keys:
            raise TypeError(
                f"{name}: relation constraints ({', '.join(keys)}) apply to an edge model only: derive {name} from "
                "BaseSurrealEdge."
            )
    relation_tables(relation_in, model=name, key="relation_in")
    relation_tables(relation_out, model=name, key="relation_out")
    if enforced is not None and not isinstance(enforced, bool):
        raise TypeError(f"{name}: model_config['enforced'] must be a bool or None, got {type(enforced).__name__}.")


def relation_type_clause(relation_in: Sequence[str], relation_out: Sequence[str], enforced: bool) -> str:
    """``TYPE RELATION[ IN `a` | `b`][ OUT `c`][ ENFORCED]`` — the type clause of a relation table (v0.33.0).

    *relation_in* and *relation_out* are names :func:`relation_tables` returned: each written in backticks, joined
    with `` | `` in order (a repeated name written twice, as SurrealDB stores ``IN a | a``); an empty sequence writes no
    ``IN`` (or ``OUT``) clause — any table. ``ENFORCED`` follows the ends: SurrealDB 2.x, 3.1 and the embedded engine
    refuse it before them. ``enforced=False`` writes nothing. Pure.
    """
    clause = "TYPE RELATION"
    if relation_in:
        clause += " IN " + " | ".join(_identifier(name) for name in relation_in)
    if relation_out:
        clause += " OUT " + " | ".join(_identifier(name) for name in relation_out)
    if enforced:
        clause += " ENFORCED"
    return clause


def relation_statement(table: str, clause: str, overwrite: object) -> str:
    """``DEFINE TABLE {OVERWRITE | IF NOT EXISTS} `<table>` <clause>;`` — a relation table's bare statement (v0.33.0).

    *clause* is a text :func:`relation_type_clause` returned; *table* is checked as an identifier and written in
    backticks. ``overwrite=True`` redefines an existing table (``OVERWRITE``, which resets the clauses the statement
    does not write: schema mode, permissions, change feed, comment, ``DROP``), ``False`` keeps it (``IF NOT EXISTS``);
    a non-``bool`` is a ``TypeError``. Pure.
    """
    return f"DEFINE TABLE {_overwrite_clause(overwrite)} {_identifier(_identifier_name(table, what='Table'))} {clause};"


#: The operations of a table's ``PERMISSIONS``, in the order SurrealDB renders them.
_TABLE_OPERATIONS = ("select", "create", "update", "delete")


def table_permissions_text(permissions: Mapping[str, str]) -> str | None:
    """A table's ``PERMISSIONS`` as SurrealDB renders them: ``FULL`` when ``FULL`` covers the four operations, else
    ``FOR select WHERE …, FOR create, update FULL`` — operations in SurrealQL's order, grouped by rule in the order the
    rules first appear, so one ``WHERE`` rule for all four reads ``FOR select, create, update, delete WHERE …``.
    ``None`` when there is none, or every rule is ``NONE`` (a table's default)."""
    if all(rule == "NONE" for rule in permissions.values()):
        return None
    order = {operation: position for position, operation in enumerate(_TABLE_OPERATIONS)}
    groups: dict[str, list[str]] = {}
    for operation in sorted(permissions, key=lambda operation: (order.get(operation, len(order)), operation)):
        groups.setdefault(permissions[operation], []).append(operation)
    if set(groups) == {"FULL"} and permissions.keys() == order.keys():
        return "FULL"
    return ", ".join("FOR " + ", ".join(operations) + " " + rule for rule, operations in groups.items())


def _carried_expression(text: str) -> bool:
    """Whether the stored expression *text* can be written back as it is: ``scan_fragment()`` accepts it — literals
    and quoted identifiers terminated, brackets balanced, no comment marker, no NUL — with no ``;`` outside brackets
    (v0.34.0). A check of a definition read from the database, which the ORM would otherwise misread; not an injection
    defence. Pure."""
    try:
        return not scan_fragment(text)
    except DefineParseError:
        return False


def _carried_rule(rule: str) -> bool:
    """Whether the stored permissions rule *rule* can be written back as it is: ``FULL``, ``NONE`` or ``WHERE <expr>``
    whose ``<expr>`` :func:`_carried_expression` accepts. Pure."""
    if rule in ("FULL", "NONE"):
        return True
    where = _PERMISSIONS_WHERE.fullmatch(rule)
    return where is not None and _carried_expression(where.group(1))


def relation_statement_keeping(table: str, clause: str, state: TableState) -> str | None:
    """``DEFINE TABLE OVERWRITE `<table>` <clause>…;`` keeping the other clauses of the table's stored definition
    *state* (v0.33.0) — the statement ``define_relation()`` sends for a table that exists.

    A bare ``OVERWRITE`` resets every clause it does not write, so after *clause* (a text
    :func:`relation_type_clause` returned) the statement writes back, from *state* (``parse_define_table()`` of the
    stored definition), in this order: ``DROP``; ``SCHEMAFULL`` (``SCHEMALESS`` is the default and not written);
    ``COMMENT`` re-quoted as a string literal; ``CHANGEFEED <duration>[ INCLUDE ORIGINAL]`` as the parser read it;
    ``PERMISSIONS`` rendered by :func:`table_permissions_text` (left out when every rule is ``NONE``). *table* is
    checked as an identifier and written in backticks. It is :func:`table_statement_keeping` with no declared clause
    (v0.34.0).

    ``None`` when a stored value fails its check: a comment holding a NUL, or a permissions rule that is not ``FULL``,
    ``NONE`` or ``WHERE <expr>`` whose ``<expr>`` ``scan_fragment()`` accepts with no ``;`` outside brackets. These
    checks catch a definition the ORM would misread; they are not an injection defence: the source is the database
    itself. Pure.
    """
    return table_statement_keeping(table, clause, TableClauses(), state)


def relation_table_refused(
    guard: Literal["RG1", "RG2", "RG3"],
    model: str,
    table: str,
    *,
    method: Literal["define_relation", "define_table", "define_access"] = "define_relation",
) -> str:
    """The refusals of an edge model's table guard (v0.33.0): the text of *guard* for the edge model *model* on
    *table*, naming *method* — ``define_relation()``, or ``define_table()`` (``define_access()``) when it is the caller
    (v0.34.0). Pure.

    - ``RG1``: the table is a materialized view, whose query the ``OVERWRITE`` would drop;
    - ``RG2``: the table's definition cannot be read, or a stored value fails its check
      (:func:`relation_statement_keeping` returned ``None``), so its other clauses cannot be kept;
    - ``RG3``: ``overwrite=False`` on a table that exists and is not a relation table (``IF NOT EXISTS`` would leave
      the constraint unapplied).
    """
    if guard == "RG1":
        return (
            f"{model}.{method}(): the table '{table}' is a materialized view; defining it as a relation table "
            f"would drop its query and turn it into an ordinary table that no longer updates. Give {model} another "
            "table_name, or remove the view first. Nothing was defined."
        )
    if guard == "RG2":
        return (
            f"{model}.{method}(): the definition of the table '{table}' could not be read, so the ORM cannot keep "
            "its other clauses (schema mode, permissions, change feed, comment). Nothing was defined."
        )
    return (
        f"{model}.{method}(overwrite=False): the table '{table}' exists and is not a relation table; DEFINE "
        "TABLE IF NOT EXISTS would leave it as it is, and the relation constraint would not apply. Call "
        f"{method}() with overwrite=True, which keeps the table's other clauses. Nothing was defined."
    )


# --- model table DDL (v0.34.0) ---------------------------------------------------------------------------------------

#: A model's ``changefeed`` once stripped and its whitespace runs folded: a duration as ``define_parser`` reads one, then
#: optionally `` INCLUDE ORIGINAL`` in any case.
_CHANGEFEED_FORM = re.compile(rf"({_DURATION})( (?i:INCLUDE ORIGINAL))?")


def _model_name(model: type | str) -> str:
    """The name the class-time messages give *model*: the class's name, or *model* itself when it is one."""
    return model if isinstance(model, str) else model.__name__


def changefeed_text(value: object, *, model: type | str) -> str:
    """The change feed a model declares, checked and normalised (v0.34.0): ``'7d'``, ``'1h INCLUDE ORIGINAL'``.

    *value* is stripped and its whitespace runs folded to one space; it must then read ``<duration>[ INCLUDE
    ORIGINAL]`` — the duration as ``define_parser`` reads one (``7d``, ``1h30m``, ``1w2d``, ``0s``), ``INCLUDE
    ORIGINAL`` in any case, written upper-case. For the full SurrealDB-ORM's form, a bare duration written without
    extra spaces, the result is *value* itself. *model* (a class or its name) is named in the messages. Pure.

    :raises TypeError: *value* is not a ``str``.
    :raises ValueError: *value* is not a change feed SurrealDB reads.
    """
    name = _model_name(model)
    if not isinstance(value, str):
        raise TypeError(
            f"{name}: model_config['changefeed'] must be a str such as '7d' or '7d INCLUDE ORIGINAL', got "
            f"{type(value).__name__}."
        )
    match = _CHANGEFEED_FORM.fullmatch(" ".join(value.split()))
    if match is None:
        raise ValueError(
            f"{name}: model_config['changefeed'] is {value!r}, not a change feed SurrealDB reads: a duration such as "
            "'7d' or '1h30m', optionally followed by ' INCLUDE ORIGINAL'."
        )
    return match.group(1) + (" INCLUDE ORIGINAL" if match.group(2) else "")


def _table_rule(value: object, *, subject: str) -> str:
    """One rule of a model's ``permissions``, checked and normalised: ``FULL``, ``NONE`` or ``WHERE <condition>``.

    ``FULL`` and ``NONE`` in any case are written upper-case; ``WHERE <condition>`` (``WHERE`` in any case) and a bare
    condition (the full SurrealDB-ORM's form) are written ``WHERE <condition>``, the condition stripped. A condition is
    one expression ``scan_fragment()`` accepts with no ``;`` outside brackets. *subject* names the rule in the
    messages (``"M: model_config['permissions']['select']"``).
    """
    if not isinstance(value, str):
        raise TypeError(f"{subject} must be a str, got {type(value).__name__}.")
    rule = value.strip()
    if rule.upper() in ("FULL", "NONE"):
        return rule.upper()
    where = _PERMISSIONS_WHERE.fullmatch(rule)
    if where is not None:
        condition = where.group(1).strip()
    elif rule.upper() == "WHERE":
        condition = ""
    else:
        condition = rule
    if not condition:
        raise ValueError(f"{subject} is empty; write 'FULL', 'NONE' or a condition.")
    try:
        top_level_semicolon = scan_fragment(condition)
    except DefineParseError as error:
        raise ValueError(f"{subject} is not SurrealQL the ORM can embed: {error}.") from error
    if top_level_semicolon:
        raise ValueError(f"{subject} is one condition; it holds a ';' outside brackets.")
    return f"WHERE {condition}"


def permissions_rules(value: object, *, model: type | str) -> dict[str, str]:
    """The table permissions a model declares, checked and normalised to the four operations (v0.34.0).

    *value* is a mapping of operation — ``"select"``, ``"create"``, ``"update"`` or ``"delete"``, lower-case — to a
    rule: ``"FULL"`` or ``"NONE"`` in any case, ``"WHERE <condition>"`` or a bare condition. The result maps the four
    operations, in SurrealQL's order, to ``"FULL"``, ``"NONE"`` or ``"WHERE <condition>"`` — the shape of
    ``TableState.permissions`` —, an operation left out to ``"NONE"`` (SurrealDB's default), so ``{}`` gives four
    ``"NONE"``. The operations are checked in the mapping's order, each key before its rule. Private to the ORM: a
    model's ``get_permissions()`` returns the mapping as declared; the renderers use this form. *model* (a class or its
    name) is named in the messages. Pure.

    A condition is checked by ``scan_fragment()`` — literals terminated, brackets balanced, no comment marker, no NUL
    — and must hold no ``;`` outside brackets: a check against mistakes, not an injection defence; a rule is trusted
    application code.

    :raises TypeError: *value* is not a mapping; a rule is not a ``str``.
    :raises ValueError: an operation is not one of the four; a rule is blank, not embeddable, or more than one
        condition.
    """
    name = _model_name(model)
    if not isinstance(value, Mapping):
        raise TypeError(
            f"{name}: model_config['permissions'] must be a mapping of operation to rule, such as {{'select': 'FULL'}}, "
            f"got {type(value).__name__}."
        )
    rules = dict.fromkeys(_TABLE_OPERATIONS, "NONE")
    for operation, rule in value.items():
        if not isinstance(operation, str) or operation not in rules:
            raise ValueError(
                f"{name}: model_config['permissions'] names the operation {operation!r}; a table's operations are "
                "select, create, update and delete."
            )
        rules[operation] = _table_rule(rule, subject=f"{name}: model_config['permissions'][{operation!r}]")
    return rules


def comment_text(value: object, *, model: type | str) -> str | None:
    """The table comment a model declares, checked (v0.34.0): ``None``, or a ``str`` without a NUL character, returned
    as given (the literal is written when the statement is rendered). *model* (a class or its name) is named in the
    messages. Pure.

    :raises TypeError: *value* is neither a ``str`` nor ``None``.
    :raises ValueError: *value* holds a NUL character.
    """
    if value is None:
        return None
    name = _model_name(model)
    if not isinstance(value, str):
        raise TypeError(f"{name}: model_config['comment'] must be a str or None, got {type(value).__name__}.")
    if "\x00" in value:
        raise ValueError(f"{name}: model_config['comment'] holds a NUL character (\\x00), which SurrealQL cannot carry.")
    return value


def _flexible_fields(value: object, *, name: str, fields: Collection[str]) -> None:
    """``flexible_fields``, checked: a ``list`` or ``tuple`` of ``str``, each a field of the model (*fields*)."""
    if not isinstance(value, (list, tuple)):
        raise TypeError(
            f"{name}: model_config['flexible_fields'] must be a list or tuple of field names, got {type(value).__name__}."
        )
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"{name}: model_config['flexible_fields'] holds a {type(item).__name__}, not a field name.")
    for item in value:
        if item not in fields:
            raise ValueError(f"{name}: model_config['flexible_fields'] names {item!r}, which is not a field of {name}.")


def check_table_config(
    model: type,
    *,
    view: bool,
    extra_allow: bool,
    table_type: object,
    schema_mode: object,
    changefeed: object,
    permissions: object,
    comment: object,
    flexible_fields: object,
    fields: Collection[str],
) -> None:
    """The ``schema_mode``, ``changefeed``, ``permissions``, ``comment`` and ``flexible_fields`` keys of a model's
    ``SurrealConfigDict``, checked when the class is defined (v0.34.0).

    Called on every model, after :func:`check_relation_config` (so *table_type* is ``None`` or a ``TableType``),
    inherited values included: the messages name *model*, the class being defined. *view* tells a materialized view
    model (``view_query`` set), *extra_allow* a model with ``extra="allow"``, *fields* the model's field names (its
    ``model_fields``). In order — the first failure raises:

    - ``TypeError``: a view model with ``schema_mode``, ``table_type`` or ``flexible_fields`` (its records are computed
      by SurrealDB; it takes ``permissions``, ``changefeed`` and ``comment``), the keys named in that order;
    - ``TypeError``: *schema_mode* is not a ``SchemaMode`` (a plain ``"SCHEMAFULL"`` string included);
    - ``TypeError``: ``TableType.USER`` with ``SchemaMode.SCHEMALESS`` (a ``USER`` table is ``SCHEMAFULL``);
    - ``TypeError``: a declared ``SchemaMode.SCHEMAFULL`` with ``extra="allow"``, whose undeclared fields such a table
      refuses (SurrealDB 3.x) or drops (2.x). A ``TableType.USER`` model with ``extra="allow"`` and no declared mode is
      accepted here: its table DDL refuses it;
    - :func:`changefeed_text`, :func:`permissions_rules` and :func:`comment_text` of their keys;
    - ``TypeError`` / ``ValueError``: *flexible_fields* is not a ``list`` or ``tuple``; an item is not a ``str``; a name
      is not a field of the model (Python names, not columns).

    The values are validated, never rewritten: the model keeps every key as declared.

    :raises TypeError: see above.
    :raises ValueError: see above.
    """
    name = model.__name__
    if view:
        declared = {"schema_mode": schema_mode, "table_type": table_type, "flexible_fields": flexible_fields}
        keys = [key for key, value in declared.items() if value is not None]
        if keys:
            raise TypeError(
                f"{name}: a materialized view's table is computed by SurrealDB; leave {', '.join(keys)} out (a view "
                "model takes permissions, changefeed and comment)."
            )
    if schema_mode is not None and not isinstance(schema_mode, SchemaMode):
        raise TypeError(
            f"{name}: model_config['schema_mode'] must be a surreal_orm_lite.SchemaMode, got {type(schema_mode).__name__}."
        )
    if table_type is TableType.USER and schema_mode is SchemaMode.SCHEMALESS:
        raise TypeError(f"{name}: a TableType.USER table is SCHEMAFULL; leave schema_mode out or set SchemaMode.SCHEMAFULL.")
    if schema_mode is SchemaMode.SCHEMAFULL and extra_allow:
        raise TypeError(
            f'{name}: extra="allow" keeps fields the model does not declare, which a SCHEMAFULL table refuses '
            '(SurrealDB 3.x) or drops (2.x); declare SchemaMode.SCHEMALESS or remove extra="allow".'
        )
    if changefeed is not None:
        changefeed_text(changefeed, model=model)
    if permissions is not None:
        permissions_rules(permissions, model=model)
    comment_text(comment, model=model)
    if flexible_fields is not None:
        _flexible_fields(flexible_fields, name=name, fields=fields)


@dataclass(frozen=True, slots=True)
class TableClauses:
    """The table clauses a model declares, as a table statement writes them (v0.34.0).

    ``schema_mode``: ``"SCHEMAFULL"``, ``"SCHEMALESS"`` or ``None``; ``comment``: the comment's text or ``None``;
    ``changefeed``: the normalised change feed (``"7d"``, ``"1h INCLUDE ORIGINAL"``) or ``None``; ``permissions``: the
    four-operation map of :func:`permissions_rules` or ``None``. ``None`` means not declared: the statement writes
    nothing for it. ``TableClauses()`` declares nothing.
    """

    schema_mode: str | None = None
    comment: str | None = None
    changefeed: str | None = None
    permissions: Mapping[str, str] | None = None


def declared_clauses(clauses: TableClauses) -> list[str]:
    """The clauses of *clauses* a table statement writes after its type clause, in order: the schema mode
    (``SCHEMAFULL`` or ``SCHEMALESS``), ``COMMENT '<text>'`` (a string literal), ``CHANGEFEED <duration>[ INCLUDE
    ORIGINAL]``, ``PERMISSIONS <rules>`` rendered by :func:`table_permissions_text` — ``PERMISSIONS NONE`` when every
    rule is ``NONE`` (v0.34.0). A clause left ``None`` is not written. Pure.

    The values come from a model's keys, checked when its class was defined (:func:`check_table_config`): a comment
    without a NUL, a change feed :func:`changefeed_text` normalised, rules :func:`permissions_rules` normalised.
    """
    parts: list[str] = []
    if clauses.schema_mode is not None:
        parts.append(clauses.schema_mode)
    if clauses.comment is not None:
        parts.append(f"COMMENT {_quoted(clauses.comment)}")
    if clauses.changefeed is not None:
        parts.append(f"CHANGEFEED {clauses.changefeed}")
    if clauses.permissions is not None:
        parts.append(f"PERMISSIONS {table_permissions_text(clauses.permissions) or 'NONE'}")
    return parts


def table_statement(table: str, head: str, clauses: TableClauses, overwrite: object) -> str:
    """``DEFINE TABLE {OVERWRITE | IF NOT EXISTS} `<table>` <head>[ <declared clauses>];`` — a model's table statement
    (v0.34.0).

    *head* is the table's type clause — ``TYPE NORMAL`` or ``TYPE ANY`` for a node model, :func:`relation_type_clause`'s
    text for an edge model —, followed by :func:`declared_clauses` of *clauses*. *table* is checked as an identifier and
    written in backticks. ``overwrite=True`` redefines an existing table (``OVERWRITE``, which resets the clauses the
    statement does not write), ``False`` keeps it (``IF NOT EXISTS``); a non-``bool`` is a ``TypeError``. Pure.
    """
    return relation_statement(table, " ".join([head, *declared_clauses(clauses)]), overwrite)


def _kept_clauses(clauses: TableClauses, state: TableState) -> TableClauses | None:
    """*clauses* with each clause they leave out taken from the stored definition *state* (v0.34.0): ``SCHEMAFULL``
    (``SCHEMALESS`` is the default and not written), the comment, the change feed as the parser read it, the
    permissions unless every rule is ``NONE`` (a table's default). A declared clause replaces the stored one whole.

    ``None`` when a carried value fails its check: a comment holding a NUL, or a rule that is not ``FULL``, ``NONE`` or
    ``WHERE <expr>`` whose ``<expr>`` ``scan_fragment()`` accepts with no ``;`` outside brackets. A declared value is not
    checked here (``check_table_config()`` checked it when the class was defined), nor is a stored one it replaces.
    """
    comment = clauses.comment
    if comment is None and state.comment is not None:
        if "\x00" in state.comment:
            return None
        comment = state.comment
    permissions = clauses.permissions
    if permissions is None:
        if not all(_carried_rule(rule) for rule in state.permissions.values()):
            return None
        if table_permissions_text(state.permissions) is not None:
            permissions = state.permissions
    changefeed = clauses.changefeed
    if changefeed is None and state.changefeed is not None:
        changefeed = state.changefeed + (" INCLUDE ORIGINAL" if state.changefeed_include_original else "")
    schema_mode = clauses.schema_mode
    if schema_mode is None and state.schema_mode == "SCHEMAFULL":
        schema_mode = "SCHEMAFULL"
    return TableClauses(schema_mode=schema_mode, comment=comment, changefeed=changefeed, permissions=permissions)


def table_statement_keeping(table: str, head: str, clauses: TableClauses, state: TableState) -> str | None:
    """``DEFINE TABLE OVERWRITE `<table>` <head>…;`` for a table that exists, keeping the clauses of its stored
    definition *state* that *clauses* do not declare (v0.34.0) — the statement ``define_table()`` and
    ``define_relation()`` send for a table that exists.

    A bare ``OVERWRITE`` resets every clause it does not write, so after *head* (``TYPE NORMAL``, ``TYPE ANY`` or a
    relation's type clause) the statement writes, in this order: ``DROP`` when stored (no key declares it); the schema
    mode, ``COMMENT``, ``CHANGEFEED`` and ``PERMISSIONS`` — each the declared one (as :func:`declared_clauses` writes
    it: ``PERMISSIONS NONE`` when every declared rule is ``NONE``), else the stored one: ``SCHEMAFULL`` (``SCHEMALESS``
    is the default and not written), the comment re-quoted as a string literal, the change feed as the parser read it,
    the permissions rendered by :func:`table_permissions_text` (left out when every rule is ``NONE``). *state* is
    ``parse_define_table()`` of the stored definition; *table* is checked as an identifier and written in backticks.

    ``None`` when a carried value fails its check: a comment holding a NUL, or a permissions rule that is not ``FULL``,
    ``NONE`` or ``WHERE <expr>`` whose ``<expr>`` ``scan_fragment()`` accepts with no ``;`` outside brackets. These
    checks catch a definition the ORM would misread; they are not an injection defence: the source is the database
    itself. A value the model declares replaces the stored one, which is then not checked. Pure.
    """
    kept = _kept_clauses(clauses, state)
    if kept is None:
        return None
    return table_statement(table, f"{head} DROP" if state.drop else head, kept, True)


def _view_statement(table: str, query: str, overwrite: object, clauses: TableClauses, *, drop: bool) -> str:
    """A view's statement: ``DROP`` and the schema mode before ``AS (<query>)``, the other clauses after it."""
    table = _identifier_name(table, what="Table")
    before = ["DROP"] if drop else []
    if clauses.schema_mode is not None:
        before.append(clauses.schema_mode)
    body = " ".join([*before, f"AS ({query})", *declared_clauses(replace(clauses, schema_mode=None))])
    return f"DEFINE TABLE {_overwrite_clause(overwrite)} {_identifier(table)} {body};"


def view_statement_keeping(table: str, query: str, clauses: TableClauses, state: TableState) -> str | None:
    """``DEFINE TABLE OVERWRITE `<table>`[ DROP][ SCHEMAFULL] AS (<query>)[ COMMENT '…'][ CHANGEFEED …][ PERMISSIONS
    …];`` for a view that exists, keeping the clauses of its stored definition *state* that *clauses* do not declare
    (v0.34.0) — the statement ``define_view()`` sends over a stored view.

    The rule of :func:`table_statement_keeping`, around the view's ``AS (<query>)`` instead of after a type clause:
    ``DROP`` and the schema mode before it, the comment, the change feed and the permissions after it, each the declared
    one, else the stored one. *query* is a text :func:`check_view_query` returned; *table* is checked as an identifier
    and written in backticks. ``None`` when a carried value fails its check (a comment holding a NUL, a rule that is not
    ``FULL``, ``NONE`` or one ``WHERE`` expression ``scan_fragment()`` accepts). Pure.
    """
    kept = _kept_clauses(clauses, state)
    if kept is None:
        return None
    return _view_statement(table, query, True, kept, drop=state.drop)


def _field_definition(definition: object) -> FieldState | None:
    """``parse_define_field()``'s state of one ``INFO FOR TABLE`` definition, ``None`` when it cannot read it."""
    if not isinstance(definition, str):
        return None
    try:
        return parse_define_field(definition).state
    except DefineParseError:
        return None


def _info_path(key: object) -> tuple[str, ...] | None:
    """The path an ``INFO FOR TABLE`` key reads as in full (``subs[*].city``, `` `select` ``), or ``None``."""
    if not isinstance(key, str):
        return None
    try:
        path, end = read_idiom(key, 0)
    except DefineParseError:
        return None
    return path if end == len(key) else None


async def read_table_fields(client: Any, table: str, *, kind: str) -> dict[tuple[str, ...], FieldState | None]:
    """The field definitions of *table*, read once with ``INFO FOR TABLE `<table>`;`` and keyed by path (v0.34.0).

    Each definition is read by ``parse_define_field()`` and keyed by its ``state.path``, the unescaped segments
    (``("address", "city")``, ``("subs", "*", "city")``, ``("a.b",)`` for one segment holding a dot), whichever
    rendering the line uses (``bag.*`` on 3.x, ``bag[*]`` on 2.x; a keyword's key bare or in backticks). A definition
    the parser cannot read, or that is not a ``str``, is ``None`` under the path its key reads as
    (``define_parser.read_idiom()`` of the whole key), so the caller can refuse to redefine that field without its
    clauses; one whose key is not a path is left out. ``{}`` when the answer holds no ``fields`` mapping. *table* is
    checked as an identifier and written in backticks; the read is on *client* outside any transaction (committed
    state).

    :param kind: ``"definition"``, for the message of a refusal.
    :raises SurrealDbAuthenticationError: the session may not read the table's definitions (a record user).
    :raises SurrealDbError: any other refusal of the ``INFO``.
    """
    statement = f"INFO FOR TABLE {_identifier(_identifier_name(table, what='Table'))};"
    try:
        info = await client.query(statement, {})
    except (ServerError, SurrealDbError, RuntimeError) as error:
        raise _guard_read_refused(statement, error, kind=kind, table=table, what="table") from error
    fields = info.get("fields") if isinstance(info, dict) else None
    read: dict[tuple[str, ...], FieldState | None] = {}
    if not isinstance(fields, dict):
        return read
    for key, definition in fields.items():
        state = _field_definition(definition)
        path = _info_path(key) if state is None else tuple(state.path)
        # Two definitions of one path: an unreadable one wins, so the field is never redefined without its clauses.
        if path is not None and (state is None or path not in read):
            read[path] = state
    return read


def table_refused(
    guard: Literal["TG1", "TG2", "TG3", "TG4"],
    model: str,
    table: str,
    *,
    method: Literal["define_table", "define_access"] = "define_table",
    field: str | None = None,
) -> str:
    """The refusals of a node model's table guard (v0.34.0): the text of *guard* for the model *model* on *table*,
    naming *method* — ``define_table()``, or ``define_access()`` when it defines the table. Pure.

    - ``TG1``: the table is a materialized view, whose query the statement would drop;
    - ``TG2``: the table's definition cannot be read, or a carried clause fails its check
      (:func:`table_statement_keeping` returned ``None``), so the clauses the model does not declare cannot be kept;
    - ``TG3``: the table is a relation table, whose constraint an ordinary table statement would drop;
    - ``TG4``: the definition of the field *field* (its canonical name: ``address.city``, ``tags.*``) cannot be read,
      or one of its carried clauses fails its check, so the clauses the model does not write cannot be kept.

    :raises ValueError: ``TG4`` without *field*.
    """
    call = f"{model}.{method}()"
    if guard == "TG1":
        return (
            f"{call}: the table '{table}' is a materialized view; defining it as an ordinary table would drop its query. "
            f"Declare view_query on {model}, give it another table_name, or remove the view first. Nothing was defined."
        )
    if guard == "TG2":
        return (
            f"{call}: the definition of the table '{table}' could not be read, so the ORM cannot keep the clauses {model} "
            "does not declare (schema mode, permissions, change feed, comment). Nothing was defined."
        )
    if guard == "TG3":
        return (
            f"{call}: the table '{table}' is a relation table (TYPE RELATION) and {model} is not an edge model; defining "
            f"it as an ordinary table would drop its relation constraint and refuse every edge. Derive {model} from "
            "BaseSurrealEdge, or give it another table_name. Nothing was defined."
        )
    if field is None:
        raise ValueError("TG4 names the field whose definition could not be read: pass field=.")
    return (
        f"{call}: the definition of the field '{field}' on the table '{table}' could not be read, so the ORM cannot keep "
        f"the clauses {model} does not write (ASSERT, PERMISSIONS, READONLY, DEFAULT, VALUE, COMPUTED, COMMENT). Nothing "
        "was defined."
    )


def relation_schemafull_refused(model: str, columns: Sequence[str]) -> str:
    """RG4 (v0.34.0): ``define_relation()`` of the edge model *model*, which declares ``SchemaMode.SCHEMAFULL`` and has
    the data columns *columns* (top level, in declaration order). ``define_relation()`` defines no field, so a
    ``SCHEMAFULL`` table would refuse (SurrealDB 3.x) or drop (2.x, the embedded engine) every edge's data;
    ``define_table()`` applies the same relation statement and defines them. Pure."""
    names = ", ".join(repr(column) for column in columns)
    return (
        f"{model}.define_relation(): {model} declares SchemaMode.SCHEMAFULL and has data fields ({names}); "
        "define_relation() defines no field, and a SCHEMAFULL table refuses (SurrealDB 3.x) or drops (2.x and the "
        f"embedded engine) the fields it does not define. Call {model}.define_table(), which applies the same relation "
        "statement and defines them. Nothing was defined."
    )


def user_extra_refused(model: str) -> str:
    """The refusal of a ``TableType.USER`` model with ``extra="allow"`` and no declared schema mode (v0.34.0): its
    table is ``SCHEMAFULL``, which refuses (SurrealDB 3.x) or drops (2.x) the fields the model does not declare. Pure."""
    return (
        f'{model}: extra="allow" keeps fields the model does not declare, and a TableType.USER table is SCHEMAFULL, which '
        f'refuses them (SurrealDB 3.x) or drops them (2.x); remove extra="allow" from {model}.'
    )


def signup_excluded_refused(model: str, fields: Sequence[str]) -> str:
    """The refusal of an auth model whose ``SCHEMAFULL`` table would leave out *fields* (SG1, #296): SIGNUP writes
    them, ``Field(exclude=True)`` keeps them out of ``model_dump()`` and so out of the columns the model table DDL
    defines — in ``table_ddl()``, ``define_table()`` and ``define_access()`` alike. Pure."""
    names = ", ".join(repr(name) for name in fields)
    pronoun = "it" if len(fields) == 1 else "them"
    return (
        f"{model}: SIGNUP writes {names}, but Field(exclude=True) keeps {pronoun} out of model_dump() and so out of the "
        f"SCHEMAFULL table that table_ddl(), define_table() and define_access() define for the model; that table would "
        f"refuse {pronoun} (SurrealDB 3.x) or drop {pronoun} (2.x), so no signup or signin would work. Remove "
        f"exclude=True, or define the table by hand with a column for each field named here and call "
        f"define_access(with_table=False)."
    )
