"""Server-side events and DDL helpers (v0.31.0).

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

The module sits low in the package: it imports neither ``model_base`` nor ``connection_manager``, so both can import
it.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import KW_ONLY, dataclass, field
from typing import Any

from .define_parser import DefineParseError, parse_kind, render_kind, scan_fragment
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
