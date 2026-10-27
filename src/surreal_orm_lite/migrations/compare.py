"""The normalising comparator of the migration system (v0.38.0) — private.

Every equality between two definitions goes through this module, so a definition the ORM wrote and the same one as a
SurrealDB server renders it back compare equal (spec §5.7.1, M15).

``DEFAULT`` literals: :func:`read_literal` turns the text of a literal the ORM can write — a ``bool``, an ``int``, a
finite ``float`` (``1.5`` or ``1.5f``), a ``decimal`` (``1.10dec``), a string literal however quoted, a UTC datetime
``d'…Z'`` with at most six fractional digits — into its Python value; :func:`literal_equal` compares two texts by that
value and its kind (``1.5`` equals ``1.5f`` and ``1.10dec`` equals ``1.1dec``, M15 b and c), any other text by the
text itself.

Definitions: :func:`table_key`, :func:`field_key`, :func:`index_key`, :func:`event_key`, :func:`access_key` and
:func:`analyzer_key` reduce a state to a hashable tuple — two definitions are equal when their keys are. A key holds
the definition's name and clauses, normalised:

- an expression (``VALUE``, ``COMPUTED``, ``ASSERT``, a ``DEFAULT`` that is not a literal, a view's query, an event's
  ``WHEN`` and ``THEN``, a permission rule, an access's ``signin_where`` and the values of its ``signup_fields``)
  through :func:`expression_key`: every whitespace run outside literals and quoted identifiers removed (M15 a), the
  ``;`` the servers write before a block's ``}`` removed and a block holding one parenthesised expression unwrapped
  (measured on 3.3.4 and 2.7.0, 2026-10-27);
- a literal ``DEFAULT`` by its value and kind (:func:`read_literal`);
- a duration by its length (``types.Duration``: ``7d`` equals ``1w``, M15 d); a change feed by its length and
  ``INCLUDE ORIGINAL``;
- permissions without the rules equal to SurrealDB's default (``FULL`` for a field, ``NONE`` for a table, M15 e);
- a kind in canonical form with its nullability (``option<int>`` equals ``int`` nullable; a kind holding ``NONE`` —
  ``any`` — is nullable either way), and ``FLEXIBLE`` ignored on a kind holding ``any`` (M15 f);
- an index with the server's defaults (``search.index_matches()``'s rule: ``BM25`` equals ``BM25(1.2,0.75)``, BM25's
  numbers to six significant digits, an HNSW option left out equals its default, ``DIST`` and ``TYPE`` upper-case;
  ``CONCURRENTLY``, a build mode, not compared);
- an ``ASYNC`` event's ``RETRY``/``MAXDEPTH`` left out equal to ``1``/``3`` (v0.31.0);
- an access by its type, table, ``signup_fields`` as a mapping (M19), ``signin_where``, durations — the grant's only
  with ``WITH REFRESH`` —, ``with_refresh`` and comment, and by its ``SIGNUP``/``SIGNIN`` texts only when its table is
  empty (another shape); the generated ``WITH JWT`` clauses are not compared;
- an analyzer by its tokenizers, filters (``search._filter_key()``), function and comment.

A key never holds ``raw``, a field's ``path`` or ``table``, ``other`` or the marks (``undeclared``, ``server``):
two definitions differing only by those are equal.

Imports no other module of the ``migrations`` package: ``render`` imports this one.
"""

import math
import re
from collections.abc import Hashable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from ..define_parser import (
    DefineParseError,
    Kind,
    KLiteral,
    KName,
    KUnion,
    _delimited_end,
    _quote_close,
    normalise_kind,
    parse_kind,
    unquote,
)
from ..model_ddl import holds_any
from ..schema_state import AccessState, AnalyzerState, EventState, FieldState, IndexState, TableState
from ..search import _bm25_pair, _filter_key
from ..types import Duration
from ..vector import DEFAULTS

__all__ = [
    "LiteralValue",
    "access_key",
    "analyzer_key",
    "event_key",
    "expression_key",
    "field_key",
    "index_key",
    "literal_equal",
    "read_literal",
    "table_key",
]

#: The value of a literal the ORM can write, as :func:`read_literal` returns it.
LiteralValue = bool | int | float | Decimal | str | datetime

_INTEGER = re.compile(r"-?[0-9]+")
#: A float as the ORM writes it (``1.5``) or as the servers render it (``1.5f``); ``2f`` too.
_FLOAT = re.compile(r"-?[0-9]+(?:\.[0-9]+f?|f)")
_DECIMAL = re.compile(r"(-?[0-9]+(?:\.[0-9]+)?)dec")
#: A UTC datetime as ``to_surql_literal()`` writes it: seconds, then at most six fractional digits.
_DATETIME = re.compile(r"d'([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?)Z'")


def read_literal(text: str) -> LiteralValue | None:
    """The Python value of the SurrealQL literal *text* (stripped), or ``None`` when it is not one of these forms:

    - ``true``, ``false``;
    - ``-?[0-9]+`` → ``int``;
    - ``-?[0-9]+.[0-9]+`` with or without the ``f`` suffix, or ``-?[0-9]+f`` → a finite ``float``;
    - ``-?[0-9]+(.[0-9]+)?dec`` → ``Decimal``, its digits kept (``1.10dec`` → ``Decimal('1.10')``);
    - ``'…'`` or ``"…"`` → ``str`` (``define_parser.unquote()``);
    - ``d'YYYY-MM-DDTHH:MM:SS[.ffffff]Z'`` (one to six fractional digits) → an aware UTC ``datetime``.

    ``NONE``, ``NULL``, arrays, objects, function calls, other prefixed strings (``u'…'``) and every expression give
    ``None``. Pure.
    """
    value = text.strip()
    if value in ("true", "false"):
        return value == "true"
    if _INTEGER.fullmatch(value):
        return int(value)
    if _FLOAT.fullmatch(value):
        number = float(value.removesuffix("f"))
        return number if math.isfinite(number) else None
    decimal = _DECIMAL.fullmatch(value)
    if decimal is not None:
        try:
            return Decimal(decimal.group(1))
        except InvalidOperation:  # pragma: no cover - the pattern only admits decimal digits
            return None
    if value[:1] in ("'", '"'):
        try:
            return unquote(value)
        except DefineParseError:
            return None
    moment = _DATETIME.fullmatch(value)
    if moment is not None:
        seconds, _, fraction = moment.group(1).partition(".")
        try:
            parsed = datetime.strptime(seconds, "%Y-%m-%dT%H:%M:%S")
        except ValueError:  # a date that does not exist: 2026-13-01
            return None
        return parsed.replace(microsecond=int(fraction.ljust(6, "0")) if fraction else 0, tzinfo=UTC)
    return None


def literal_equal(a: str, b: str) -> bool:
    """Whether the ``DEFAULT`` texts *a* and *b* mean the same value.

    Two literals :func:`read_literal` reads are equal when their values are of the same kind and equal: ``1.5`` and
    ``1.5f``, ``1.10dec`` and ``1.1dec``, ``"it's"`` and ``'it\\'s'`` are equal; ``1`` and ``1.0``, ``1`` and
    ``1dec``, ``true`` and ``1``, ``'a'`` and ``'a '`` are not. Any other pair compares its texts, stripped: ``NONE``
    equals ``NONE``, an expression equals itself only. Pure.
    """
    left, right = read_literal(a), read_literal(b)
    if left is not None and right is not None:
        return type(left) is type(right) and left == right
    return a.strip() == b.strip()


# --- expressions -------------------------------------------------------------------------------------------------------

#: What stands for a literal's or a quoted identifier's characters in the shape :func:`expression_key` reads.
_LITERAL_MARK = "_"
_PAIRS = {"(": ")", "[": "]", "{": "}"}


def _squeezed(text: str) -> tuple[list[str], list[str]]:
    """The characters of *text* without its whitespace outside literals and quoted identifiers, and their shape: the
    same list with every character of a literal (delimiters included) replaced by :data:`_LITERAL_MARK`, so a bracket
    or a ``;`` of the shape is never one a literal holds (``define_parser``'s left-to-right walk, backslash escapes
    skipped; an unterminated literal runs to the end)."""
    chars: list[str] = []
    shape: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        close = _quote_close(char, strings=True)
        if close is not None:
            end = _delimited_end(text, index, close) or length
            chars.extend(text[index:end])
            shape.extend(_LITERAL_MARK * (end - index))
            index = end
            continue
        if not char.isspace():
            chars.append(char)
            shape.append(char)
        index += 1
    return chars, shape


def _pairs(shape: list[str]) -> dict[int, int]:
    """The position of the closing bracket of each opening bracket of *shape* that is closed by its own kind."""
    pairs: dict[int, int] = {}
    stack: list[int] = []
    for position, char in enumerate(shape):
        if char in _PAIRS:
            stack.append(position)
        elif char in _PAIRS.values() and stack:
            opening = stack.pop()
            if _PAIRS[shape[opening]] == char:
                pairs[opening] = position
    return pairs


def expression_key(text: str | None) -> str | None:
    """*text*, a SurrealQL expression, as the comparator compares it — ``None`` for ``None``. Pure.

    Every whitespace run outside literals and quoted identifiers is removed (``age+1`` equals ``age + 1``; ``' '``
    and ``'  '`` stay different), then two renderings of a block the servers give are normalised (measured on 3.3.4
    and 2.7.0): the ``;`` right before a ``}`` is removed (``{ a; }`` equals ``{ a }``), and a block holding one
    parenthesised expression loses its parentheses (``{ (a) }`` equals ``{ a }``, which 3.3.4 renders). A ``;`` or a
    bracket inside a literal is never touched.
    """
    if text is None:
        return None
    chars, shape = _squeezed(text)
    dropped: set[int] = set()
    following = ""
    for position in range(len(shape) - 1, -1, -1):
        if shape[position] == ";" and following == "}":
            dropped.add(position)
            continue
        following = shape[position]
    kept = [position for position in range(len(shape)) if position not in dropped]
    kept_shape = [shape[position] for position in kept]
    pairs = _pairs(kept_shape)
    for opening, closing in pairs.items():
        if kept_shape[opening] != "{" or kept_shape[opening + 1] != "(":
            continue
        inner = pairs.get(opening + 1)
        if inner == closing - 1:
            dropped.add(kept[opening + 1])
            dropped.add(kept[inner])
    return "".join(char for position, char in enumerate(chars) if position not in dropped)


# --- clauses -----------------------------------------------------------------------------------------------------------

_WHERE = re.compile(r"WHERE\s+(.+)", re.IGNORECASE | re.DOTALL)
_THEN = re.compile(r"THEN\s+(.+)", re.IGNORECASE | re.DOTALL)
_RULE_WORDS = ("FULL", "NONE")
_ON_DELETE_WORDS = ("IGNORE", "UNSET", "CASCADE", "REJECT")
#: BM25's numbers are compared to six significant digits: the servers keep them as 32-bit floats (``1.2`` reads back
#: ``1.2000000476837158``), ``search.index_matches()``'s relative ``1e-6``.
_BM25_DIGITS = "{:.6g}"


def _rule_key(rule: str) -> str:
    """A permission rule: ``FULL``/``NONE`` upper-case, ``WHERE <expression>`` with its expression's key."""
    text = rule.strip()
    if text.upper() in _RULE_WORDS:
        return text.upper()
    where = _WHERE.fullmatch(text)
    if where is not None:
        return f"WHERE {expression_key(where.group(1))}"
    return f"{expression_key(text)}"


def _rules_key(permissions: Mapping[str, str], *, default: str) -> tuple[tuple[str, str], ...]:
    """The permissions without the rules equal to SurrealDB's *default* (``FULL`` for a field, ``NONE`` for a table:
    an operation left out takes it), sorted by operation."""
    rules = ((operation.lower(), _rule_key(rule)) for operation, rule in permissions.items())
    return tuple(sorted(item for item in rules if item[1] != default))


def _duration_key(text: str | None) -> Duration | str | None:
    """A duration by its length (``7d`` equals ``1w``); a text ``Duration`` cannot read (``NONE``) as it is."""
    if text is None:
        return None
    try:
        return Duration.parse(text)
    except (TypeError, ValueError):
        return text.strip().upper()


def _default_key(text: str | None) -> tuple[str, Hashable] | None:
    """A ``DEFAULT``: a literal by its kind and value (:func:`read_literal`), anything else as an expression."""
    if text is None:
        return None
    value = read_literal(text)
    if value is not None:
        return (type(value).__name__, value)
    return ("expression", expression_key(text))


def _on_delete_key(text: str | None) -> str | None:
    """``REFERENCE ON DELETE``'s action: a word upper-case, ``THEN <expression>`` with its expression's key."""
    if text is None:
        return None
    action = text.strip()
    if action.upper() in _ON_DELETE_WORDS:
        return action.upper()
    then = _THEN.fullmatch(action)
    return f"THEN {expression_key(then.group(1))}" if then is not None else expression_key(action)


_ANY = KName("any")
_NONE = KLiteral("none")


def _admits_none(kind: Kind) -> bool:
    """Whether *kind* holds ``NONE`` already: ``any``, ``none`` or a union with one of them."""
    if kind in (_ANY, _NONE):
        return True
    return isinstance(kind, KUnion) and any(_admits_none(member) for member in kind.members)


def _kind_key(field: FieldState) -> tuple[str, bool, bool]:
    """``(canonical kind, nullable, holds any)`` of *field*: ``option<X>`` read as ``X`` nullable, a kind holding
    ``NONE`` nullable either way; a kind the parser cannot read kept as written."""
    try:
        text, wrapped = normalise_kind(field.field_type)
        kind = parse_kind(text)
    except DefineParseError:
        return field.field_type, field.nullable, False
    return text, field.nullable or wrapped or _admits_none(kind), holds_any(kind)


# --- definitions -------------------------------------------------------------------------------------------------------


def table_key(table: TableState) -> tuple[Hashable, ...]:
    """The comparator's key of a table's own clauses (its fields, indexes and events are compared one by one): schema
    mode, type, ``DROP``, change feed, permissions, comment, view query, relation ends (as sets) and ``ENFORCED``."""
    changefeed = None
    if table.changefeed is not None:
        changefeed = (_duration_key(table.changefeed), table.changefeed_include_original)
    return (
        table.name,
        table.schema_mode.upper(),
        table.table_type.lower(),
        table.drop,
        changefeed,
        _rules_key(table.permissions, default="NONE"),
        table.comment,
        expression_key(table.view_query),
        tuple(sorted(table.relation_in)),
        tuple(sorted(table.relation_out)),
        table.enforced,
    )


def field_key(field: FieldState) -> tuple[Hashable, ...]:
    """The comparator's key of a field: every clause it holds, normalised (spec §5.7.1)."""
    kind, nullable, any_kind = _kind_key(field)
    return (
        field.name,
        kind,
        nullable,
        field.flexible and not any_kind,
        field.reference,
        _on_delete_key(field.on_delete),
        _default_key(field.default),
        field.default_always,
        field.readonly,
        expression_key(field.value),
        expression_key(field.assertion),
        expression_key(field.computed),
        field.comment,
        _rules_key(field.permissions, default="FULL"),
    )


def _upper(word: str | None) -> str | None:
    return None if word is None else word.upper()


def index_key(index: IndexState) -> tuple[Hashable, ...]:
    """The comparator's key of an index, with the server's defaults (``search.index_matches()``'s rule)."""
    bm25 = None
    if index.search_analyzer is not None:
        bm25 = tuple(float(_BM25_DIGITS.format(number)) for number in _bm25_pair(index.bm25))
    if index.hnsw:
        vector: tuple[Hashable, ...] | None = (
            index.dimension,
            _upper(index.dist) or DEFAULTS["dist"],
            _upper(index.vector_type) or DEFAULTS["vector_type"],
            DEFAULTS["efc"] if index.efc is None else index.efc,
            DEFAULTS["m"] if index.m is None else index.m,
        )
    elif index.mtree:
        vector = (index.dimension, _upper(index.dist), _upper(index.vector_type))
    else:
        vector = None
    return (
        index.name,
        tuple(index.fields),
        index.unique,
        index.search_analyzer,
        bm25,
        index.highlights,
        index.hnsw,
        index.mtree,
        index.count,
        vector,
        index.comment,
    )


def event_key(event: EventState) -> tuple[Hashable, ...]:
    """The comparator's key of an event: ``WHEN`` and ``THEN`` as expressions, an ``ASYNC`` event's counts with
    SurrealDB's defaults (``RETRY 1``, ``MAXDEPTH 3``)."""
    retry, max_depth = event.retry, event.max_depth
    if event.async_mode:
        retry = 1 if retry is None else retry
        max_depth = 3 if max_depth is None else max_depth
    return (
        event.name,
        expression_key(event.when),
        expression_key(event.then),
        event.comment,
        event.async_mode,
        retry,
        max_depth,
    )


def access_key(access: AccessState) -> tuple[Hashable, ...]:
    """The comparator's key of an access (spec §5.7.1): ``signup_fields`` as a mapping, durations by length — the
    grant's only with ``WITH REFRESH`` —, the ``SIGNUP``/``SIGNIN`` texts only when the table is empty."""
    signup_fields = tuple(sorted((column, expression_key(text)) for column, text in access.signup_fields.items()))
    texts = (expression_key(access.signup), expression_key(access.signin)) if not access.table else None
    return (
        access.name,
        access.access_type.upper(),
        access.table,
        signup_fields,
        expression_key(access.signin_where),
        _duration_key(access.duration_token),
        _duration_key(access.duration_session),
        _duration_key(access.duration_grant) if access.with_refresh else None,
        access.with_refresh,
        access.comment,
        texts,
    )


def analyzer_key(analyzer: AnalyzerState) -> tuple[Hashable, ...]:
    """The comparator's key of an analyzer: tokenizers (lower-case), filters (``search._filter_key()``: a
    ``mapper()`` path without its quotes and case), function and comment."""
    return (
        analyzer.name,
        tuple(tokenizer.lower() for tokenizer in analyzer.tokenizers),
        tuple(_filter_key(item) for item in analyzer.filters),
        analyzer.function,
        analyzer.comment,
    )
