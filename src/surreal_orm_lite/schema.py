"""Server-side events and DDL helpers (v0.31.0).

``Event`` declares a server-side event — SurrealDB's ``DEFINE EVENT`` on a table — with the full SurrealDB-ORM's call
shape: a name, the SurrealQL the event runs (``then``), an optional condition (``when``), SurrealDB 3.x's ``ASYNC``
options and a comment. Every argument is checked when the object is built, before any request.

**Names and expressions.** An event's name is an identifier (letters, digits, underscores; not starting with a
digit), checked with ``fullmatch``. Expressions (``when``, ``then``) are embedded in the statement as written, since
SurrealQL takes no bound parameter in DDL, after a structural check (``define_parser.scan_fragment()``): string
literals and quoted identifiers terminated, brackets balanced, no SurrealQL comment marker and no NUL, and no ``;``
outside brackets where one expression is expected. The check catches mistakes — unbalanced brackets, a comment that
would swallow the clauses the ORM appends, a second statement where one expression is expected — and is not an
injection defence: expressions are trusted application code, never user input. To use a value, read it from the record
(``$after.email``) or from a ``DEFINE PARAM``; never format it into the text.

The module sits low in the package: it imports neither ``model_base`` nor ``connection_manager``, so both can import
it.
"""

from dataclasses import KW_ONLY, dataclass, field

from .define_parser import DefineParseError, scan_fragment
from .utils import VALID_ALIAS_PATTERN

__all__ = ["Event"]

#: The largest ``RETRY`` and ``MAXDEPTH`` SurrealDB accepts (an unsigned 16-bit count).
_MAX_COUNT = 65535


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
