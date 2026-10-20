"""Server-side events and DDL helpers (v0.31.0), materialized views (v0.32.0).

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
in backticks and the query inside ``AS ( … )``.

The module sits low in the package: it imports neither ``model_base`` nor ``connection_manager``, so both can import
it.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import KW_ONLY, dataclass, field
from typing import Any, Literal

from .define_parser import DefineParseError, blank_literals, parse_kind, render_kind, scan_fragment
from .exceptions import SurrealDbError
from .functions import normalize_function_name
from .surql_literal import _SERVER_PARAMETERS, _identifier, _quoted
from .time_travel import line_major
from .utils import VALID_ALIAS_PATTERN

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
#: A projection element's alias, at its end: ``… AS name``, ``… AS `a name```, ``… AS ⟨a name⟩``.
_VIEW_ALIAS = re.compile(r"\s+AS\s+(`[^`]*`|⟨[^⟩]*⟩|[^\s`⟨]+)\s*$", re.IGNORECASE)
#: A sub-select or a graph path: an element the reading leaves to the server.
_VIEW_OPAQUE = re.compile(r"\bSELECT\b|->|<-", re.IGNORECASE)
_VIEW_CALL = re.compile(r"(?<![\w$.:])([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\s*\(")
_VIEW_FIELD = re.compile(r"(?<![\w$.:])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)(?![\w.])(?!\s*[(:])")
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
    - ``fields``: the fields it reads, in order, a repeated one repeated (``("*",)`` for ``*``); ``()`` when
      ``aggregate`` is set — the aggregate's argument is read per record, as SurrealDB intends.
    - ``opaque``: it holds a sub-select or a graph path, which the reading does not analyse (``calls`` and ``fields``
      are then empty).
    """

    text: str
    alias: str | None
    aggregate: str | None
    calls: tuple[str, ...]
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
      an element that is one whole aggregate call: SurrealDB documents them as unusable in a view. Inside an
      aggregate's argument (``math::max(array::len(array::distinct(tags)))``) they work on every line.

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
            (
                call
                for element in outline.elements
                if element.aggregate is None
                for call in element.calls
                if call in _NOT_VIEW_AGGREGATES
            ),
            None,
        )
        if unusable is not None:
            raise ValueError(
                f"{key} groups its records and calls {unusable}(), which SurrealDB cannot keep up to date in a view; "
                "use count(), math::sum(), math::mean(), math::min(), math::max(), time::min() or time::max()."
            )
    return text


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
    ``a.b`` path that is not a function, a ``::`` path, an object key, a record id's table or a keyword."""
    if blanked == "*":
        return ("*",)
    plain = _VIEW_PARAMETER.sub(_spaces, _VIEW_CAST.sub(_spaces, blanked))
    return tuple(name for name in _VIEW_FIELD.findall(plain) if name.upper() not in _PROJECTION_KEYWORDS)


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
        return ViewElement(written, alias, None, (), (), True)
    calls = tuple(name.lower() for name in _VIEW_CALL.findall(plain) if "::" in name or name.lower() == "count")
    aggregate: str | None = None
    first = _VIEW_CALL.match(plain)
    if (
        first is not None
        and first.group(1).lower() in _ALL_VIEW_AGGREGATES
        and _closing_bracket(plain, first.end() - 1) == len(plain) - 1
    ):
        aggregate = first.group(1).lower()
    fields = () if aggregate is not None else _view_fields(plain)
    return ViewElement(written, alias, aggregate, calls, fields, False)


def view_query_outline(text: str) -> ViewOutline:
    """The projection and grouping of a view query *text* that :func:`check_view_query` accepted (v0.32.0).

    It is a reading of the text, not a parser: it serves the line checks (V7, C1) and leaves what it cannot read to
    the server. Literals are blanked first (``define_parser.blank_literals()``), so a keyword, a call or a comma inside
    a string or a quoted name is never read. ``grouped`` is a top-level ``GROUP BY``/``GROUP ALL``; the projection
    runs from ``SELECT`` to the top-level ``FROM`` (or the end) and is split at its top-level commas; each element's
    alias, calls, whole aggregate and fields are read as :class:`ViewElement` describes. A sub-select or a graph path
    (``->``, ``<-``) makes the element opaque, and a backquoted or ``⟨⟩`` field name is invisible: both are left to
    the server. Pure.
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


def view_statement(table: str, query: str, overwrite: object) -> str:
    """``DEFINE TABLE {OVERWRITE | IF NOT EXISTS} `<table>` AS (<query>);`` — the full SurrealDB-ORM's form (v0.32.0).

    *table* is checked as an identifier and written in backticks; *query* is a text :func:`check_view_query` returned,
    embedded as it is inside one pair of parentheses. ``overwrite=True`` recomputes an existing view (``OVERWRITE``),
    ``False`` keeps it (``IF NOT EXISTS``); a non-``bool`` is a ``TypeError``. Pure.
    """
    table = _identifier_name(table, what="Table")
    return f"DEFINE TABLE {_overwrite_clause(overwrite)} {_identifier(table)} AS ({query});"


def remove_view_statement(table: str, missing_ok: object) -> str:
    """``REMOVE TABLE [IF EXISTS] `<table>`;`` — ``IF EXISTS`` with *missing_ok* (v0.32.0).

    *table* is checked as an identifier and written in backticks; *missing_ok* must be a ``bool``. Pure.
    """
    table = _identifier_name(table, what="Table")
    return f"REMOVE TABLE{_missing_clause(missing_ok)} {_identifier(table)};"
