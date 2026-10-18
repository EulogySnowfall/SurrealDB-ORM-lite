r"""Read the ``DEFINE`` statements that ``INFO FOR DB`` and ``INFO FOR TABLE`` return (v0.30.0) — private.

``DatabaseIntrospector`` hands every definition text the server returns to this module. The servers render each
statement in a fixed clause order with upper-case keywords, but they render a keyword-named identifier bare
(``DEFINE FIELD TYPE ON t TYPE int``, ``VALUE a * READONLY``), a string holding ``'`` double-quoted, an ``IF``
expression with or without parentheses, and SurrealDB 2.x and 3.x differ in places. So nothing here guesses: what
cannot be read exactly raises ``DefineParseError`` — always with one of the fixed ``_R_*`` reasons below, never with
text taken from the statement, which may hold a credential. The reader turns it into a ``SchemaIssue``.

**The scanner** — pure functions, one left-to-right pass with explicit states and no regular expression beyond
simple anchored tokens:

- String literals ``'…'`` and ``"…"`` with backslash escapes are opaque; ``unquote()`` decodes one (``\n`` → a
  newline; the embedded engine renders that newline raw, which is kept). ``d'…'`` and ``u'…'`` stay opaque tokens.
- Identifiers: bare ``[A-Za-z0-9_]+``, or quoted between backquotes or between ``⟨`` and ``⟩``, escapes decoded.
  ``read_identifier()`` reads one, ``read_idiom()`` a field path — segments separated by ``.``, a segment an
  identifier or ``*``, ``[*]`` the segment ``*`` (``tags[*]`` and ``tags.*`` are the same path) — and
  ``canonical_name()`` joins a path with ``.`` (``"tags.*"``, the name ``FieldState`` and ``IndexState`` use).
- Depth: nesting over ``()``, ``[]``, ``{}`` (and ``<>`` inside a kind, behind a flag) **and over IF units**. The
  bare upper-case word ``IF`` (an identifier named ``IF`` is always quoted) not preceded by the word ``ELSE`` opens a
  unit when, scanning forward at its own nesting, the word ``THEN`` comes before any ``{``; the unit closes at its
  matching ``END`` (the first one at its own nesting after that ``THEN``). ``ELSE IF`` continues the unit, so a chain
  has one ``END``. The brace form ``IF c { a } ELSE { b }`` opens no unit: its branches are ``{}``-nested already.
  An ``END`` outside a unit is an ordinary word (a field named ``END``). ``$END``, ``a.END`` and ``x:END`` are never
  unit words.
- ``top_level_keywords()`` lists **every** position at depth 0 where a clause keyword stands as a whole word: these
  are candidates only, since a bare identifier may spell a keyword; the clause reader resolves them.
  ``split_top_level()`` splits at a separator at depth 0.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import NamedTuple

__all__ = [
    "DefineParseError",
    "canonical_name",
    "read_identifier",
    "read_idiom",
    "split_top_level",
    "top_level_keywords",
    "unquote",
]


class DefineParseError(ValueError):
    """A ``DEFINE`` statement that cannot be read exactly.

    It is only ever raised with one of the module's fixed ``_R_*`` reasons: never with text taken from the statement,
    which may hold a credential (a literal password in an expression, a key). ``DatabaseIntrospector`` reports it as
    a ``SchemaIssue`` and keeps the statement in ``SchemaIssue.statement``.
    """


# The reasons DefineParseError is raised with — fixed texts, never text from the statement.
_R_STATEMENT = "not the expected DEFINE statement"
_R_HEADER = "its name or ON clause is not where expected"
_R_STRING = "a string literal is not terminated"
_R_IDENT = "an identifier is not terminated"
_R_BRACKETS = "its brackets are not balanced"
_R_IF = "an IF expression has no END"
_R_PATH = "its field path is not valid"
_R_NO_READING = "no reading of its clauses fits"
_R_AMBIGUOUS = "its clauses can be read in more than one way"
_R_KIND = "its kind is not understood"
_R_PERMISSIONS = "its permissions are not understood"
_R_INDEX = "its index options are not understood"
_R_SIGNATURE = "its function signature is not understood"

_WORD = re.compile(r"[A-Za-z0-9_]+")
_SPACE = re.compile(r"\s+")
_QUOTES = frozenset("'\"")
#: The opening character of a quoted identifier → its closing one.
_IDENTIFIER_QUOTES = {"`": "`", "⟨": "⟩"}
#: The escapes that do not stand for their own character; any other ``\x`` is ``x``.
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0"}
_BRACKETS = {"(": ")", "[": "]", "{": "}"}
_KIND_BRACKETS = {**_BRACKETS, "<": ">"}
_UNIT_WORDS = frozenset({"IF", "THEN", "ELSE", "END"})
#: A word glued to one of these is a parameter, a path segment or a function or record part — never a unit word.
_NOT_BARE_AFTER = frozenset("$.:")

_WORD_TOKEN = "word"
_STRING_TOKEN = "string"
_IDENT_TOKEN = "ident"
_SPACE_TOKEN = "space"
_PUNCT_TOKEN = "punct"

#: ``(kind, start, end)`` of a token before its depth is known.
_Lexeme = tuple[str, int, int]


class _Token(NamedTuple):
    """A token of a scanned text: its kind (word, string, ident, space or punct), its span and its depth.

    ``depth`` counts the brackets and IF units around the token. A bracket, and the ``IF`` and ``END`` of a unit,
    stand at the depth of what surrounds them.
    """

    kind: str
    start: int
    end: int
    depth: int


# --- strings and identifiers ---------------------------------------------------------------------------------------


def _delimited_end(text: str, pos: int, close: str) -> int | None:
    """The position after the literal or quoted identifier opening at *pos* (backslash escapes skipped), or
    ``None`` when *close* never comes."""
    index = pos + 1
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\\":
            index += 2
        elif char == close:
            return index + 1
        else:
            index += 1
    return None


def _decode(text: str, start: int, end: int) -> str:
    """``text[start:end]`` — the body of a literal or of a quoted identifier — with its escapes decoded."""
    pieces: list[str] = []
    index = start
    while index < end:
        char = text[index]
        if char == "\\" and index + 1 < end:
            following = text[index + 1]
            pieces.append(_ESCAPES.get(following, following))
            index += 2
        else:
            pieces.append(char)
            index += 1
    return "".join(pieces)


def unquote(literal: str) -> str:
    r"""Decode one SurrealQL string literal: ``'it\'s'`` and ``"it's"`` both give ``it's``.

    *literal* must be exactly one literal, single- or double-quoted. Escapes: ``\\``, ``\'``, ``\"``, ``\n``,
    ``\t``, ``\r`` and ``\0``; any other ``\x`` gives ``x``. A raw newline inside the literal — how the embedded
    engine renders ``\n`` — is kept. Anything else raises ``DefineParseError`` (``_R_STRING``).
    """
    if not literal or literal[0] not in _QUOTES or _delimited_end(literal, 0, literal[0]) != len(literal):
        raise DefineParseError(_R_STRING)
    return _decode(literal, 1, len(literal) - 1)


def _identifier_at(text: str, pos: int) -> tuple[str, int] | None:
    """The identifier starting at *pos* and the position after it, or ``None`` when none starts there."""
    if not 0 <= pos < len(text):
        return None
    close = _IDENTIFIER_QUOTES.get(text[pos])
    if close is not None:
        end = _delimited_end(text, pos, close)
        if end is None:
            raise DefineParseError(_R_IDENT)
        return _decode(text, pos + 1, end - 1), end
    match = _WORD.match(text, pos)
    return None if match is None else (match.group(), match.end())


def read_identifier(text: str, pos: int) -> tuple[str, int]:
    """Read the identifier starting exactly at *pos*: ``(name, end)``, the name unescaped.

    Bare (``TYPE``, ``isp_user``), between backquotes (escapes ``\\``, the backquote and the control escapes such as
    ``\n`` decoded) or between ``⟨`` and ``⟩``. A quoted identifier that is not closed raises ``DefineParseError``
    (``_R_IDENT``); no identifier at *pos* raises it with ``_R_HEADER``.
    """
    found = _identifier_at(text, pos)
    if found is None:
        raise DefineParseError(_R_HEADER)
    return found


def read_idiom(text: str, pos: int) -> tuple[tuple[str, ...], int]:
    """Read the field path starting exactly at *pos*: ``(path, end)``.

    Segments are separated by ``.``; a segment is an identifier (``read_identifier``) or ``*``, and ``[*]`` is the
    segment ``*`` — ``tags[*]`` and ``tags.*`` both give ``("tags", "*")``, ``w[*].z`` and ``w.*.z`` both
    ``("w", "*", "z")``. The path ends at the first character that continues none of these. A missing segment
    (``a..b``, ``a.``) or a bracket other than ``[*]`` raises ``DefineParseError`` (``_R_PATH``).
    """
    first = _identifier_at(text, pos)
    if first is None:
        raise DefineParseError(_R_PATH)
    segments = [first[0]]
    end = first[1]
    length = len(text)
    while end < length:
        char = text[end]
        if char == "[":
            if not text.startswith("[*]", end):
                raise DefineParseError(_R_PATH)
            segments.append("*")
            end += 3
        elif char == ".":
            if text.startswith("*", end + 1):
                segments.append("*")
                end += 2
                continue
            following = _identifier_at(text, end + 1)
            if following is None:
                raise DefineParseError(_R_PATH)
            segments.append(following[0])
            end = following[1]
        else:
            break
    return tuple(segments), end


def canonical_name(path: Sequence[str]) -> str:
    """The name of a field path: its segments joined with ``.`` (``("tags", "*")`` → ``"tags.*"``)."""
    return ".".join(path)


# --- depth: brackets and IF units ----------------------------------------------------------------------------------


def _lex(text: str, start: int) -> list[_Lexeme]:
    """Cut ``text[start:]`` into tokens; an unterminated literal or quoted identifier raises."""
    lexemes: list[_Lexeme] = []
    pos = start
    length = len(text)
    while pos < length:
        char = text[pos]
        kind, end = _PUNCT_TOKEN, pos + 1
        if char in _QUOTES:
            closed = _delimited_end(text, pos, char)
            if closed is None:
                raise DefineParseError(_R_STRING)
            kind, end = _STRING_TOKEN, closed
        elif char in _IDENTIFIER_QUOTES:
            closed = _delimited_end(text, pos, _IDENTIFIER_QUOTES[char])
            if closed is None:
                raise DefineParseError(_R_IDENT)
            kind, end = _IDENT_TOKEN, closed
        elif (word := _WORD.match(text, pos)) is not None:
            kind, end = _WORD_TOKEN, word.end()
        elif (space := _SPACE.match(text, pos)) is not None:
            kind, end = _SPACE_TOKEN, space.end()
        lexemes.append((kind, pos, end))
        pos = end
    return lexemes


def _match_brackets(text: str, lexemes: Sequence[_Lexeme], pairs: Mapping[str, str]) -> dict[int, int]:
    """Index of each opening bracket → index of its closing one; unbalanced brackets raise."""
    closers = frozenset(pairs.values())
    open_at: list[int] = []
    matches: dict[int, int] = {}
    for index, (kind, start, _) in enumerate(lexemes):
        if kind != _PUNCT_TOKEN:
            continue
        char = text[start]
        if char in pairs:
            open_at.append(index)
        elif char in closers:
            if not open_at or pairs[text[lexemes[open_at[-1]][1]]] != char:
                raise DefineParseError(_R_BRACKETS)
            matches[open_at.pop()] = index
    if open_at:
        raise DefineParseError(_R_BRACKETS)
    return matches


class _Structure(NamedTuple):
    """What the IF-unit search needs about the lexemes of one text."""

    text: str
    lexemes: Sequence[_Lexeme]
    words: Sequence[str | None]  # the unit word (IF, THEN, ELSE, END) a lexeme is, if any
    matches: Mapping[int, int]  # opening bracket → closing bracket
    closers: frozenset[int]


def _unit_word(text: str, lexeme: _Lexeme, start: int) -> str | None:
    kind, begin, end = lexeme
    if kind != _WORD_TOKEN or text[begin:end] not in _UNIT_WORDS:
        return None
    if begin > start and text[begin - 1] in _NOT_BARE_AFTER:
        return None
    return text[begin:end]


def _unit_then(structure: _Structure, units: Mapping[int, int], plain: set[int], index: int) -> int | None:
    """The ``THEN`` that makes the ``IF`` at *index* open a unit, or ``None`` (the brace form, or no ``THEN``).

    Scans forward at the IF's own nesting: brackets and the units after it (already known) are skipped. An IF of the
    brace form met at this nesting met a ``{``, a closing bracket or the end first, and so does this one.
    """
    position = index + 1
    count = len(structure.lexemes)
    while position < count:
        if position in structure.matches:
            if structure.text[structure.lexemes[position][1]] == "{":
                return None
            position = structure.matches[position] + 1
            continue
        if position in structure.closers or position in plain:
            return None
        if structure.words[position] == "THEN":
            return position
        position = units[position] + 1 if position in units else position + 1
    return None


def _unit_end(structure: _Structure, units: Mapping[int, int], then: int) -> int:
    """The ``END`` closing the unit whose ``THEN`` is at *then*: the first ``END`` at the unit's own nesting."""
    position = then + 1
    count = len(structure.lexemes)
    while position < count:
        if position in structure.matches:
            position = structure.matches[position] + 1
            continue
        if position in structure.closers:
            raise DefineParseError(_R_IF)
        if structure.words[position] == "END":
            return position
        position = units[position] + 1 if position in units else position + 1
    raise DefineParseError(_R_IF)


def _if_units(text: str, lexemes: Sequence[_Lexeme], matches: Mapping[int, int], start: int) -> dict[int, int]:
    """Index of each ``IF`` that opens a unit → index of its ``END``.

    Whether an IF opens a unit depends only on what follows it, so the IFs are decided from the last to the first:
    each forward scan then skips the units after it, already known.
    """
    words = [_unit_word(text, lexeme, start) for lexeme in lexemes]
    structure = _Structure(text, lexemes, words, matches, frozenset(matches.values()))
    previous: int | None = None
    after_else: set[int] = set()
    for index, (kind, _, _) in enumerate(lexemes):
        if words[index] == "IF" and previous is not None and words[previous] == "ELSE":
            after_else.add(index)
        if kind != _SPACE_TOKEN:
            previous = index
    units: dict[int, int] = {}
    plain: set[int] = set()
    for index in range(len(lexemes) - 1, -1, -1):
        if words[index] != "IF" or index in after_else:  # ELSE IF continues the unit of the IF before it
            continue
        then = _unit_then(structure, units, plain, index)
        if then is None:
            plain.add(index)
        else:
            units[index] = _unit_end(structure, units, then)
    return units


def _scan(text: str, start: int = 0, *, angle: bool = False) -> list[_Token]:
    """The tokens of ``text[start:]`` with their depth (``angle``: ``<>`` nest too, inside a kind).

    Raises ``DefineParseError``: ``_R_STRING`` or ``_R_IDENT`` for an unterminated literal or quoted identifier,
    ``_R_BRACKETS`` for unbalanced brackets, ``_R_IF`` for an IF unit without its ``END``.
    """
    lexemes = _lex(text, start)
    matches = _match_brackets(text, lexemes, _KIND_BRACKETS if angle else _BRACKETS)
    units = _if_units(text, lexemes, matches, start)
    shift = [0] * (len(lexemes) + 1)
    for opening, closing in [*matches.items(), *units.items()]:
        shift[opening + 1] += 1
        shift[closing] -= 1
    tokens: list[_Token] = []
    depth = 0
    for index, (kind, begin, end) in enumerate(lexemes):
        depth += shift[index]
        tokens.append(_Token(kind, begin, end, depth))
    return tokens


def top_level_keywords(text: str, keywords: Iterable[str], start: int = 0) -> list[tuple[int, str]]:
    """Every ``(position, keyword)`` at depth 0 of ``text[start:]`` where one of *keywords* stands as a whole word.

    Outside strings and quoted identifiers, outside brackets and IF units, with whitespace or an edge of the scanned
    text on both sides. Case-sensitive: the server renders keywords upper-case. A keyword may be two words separated
    by the single space the server renders (``WITH REFRESH``, ``ON DELETE``). The result is in text order, keywords
    matching at one position in the order given. These are candidates only: a bare identifier may spell a keyword.
    """
    wanted = tuple(keywords)
    length = len(text)
    found: list[tuple[int, str]] = []
    for token in _scan(text, start):
        if token.kind != _WORD_TOKEN or token.depth or (token.start > start and not text[token.start - 1].isspace()):
            continue
        for keyword in wanted:
            end = token.start + len(keyword)
            if text.startswith(keyword, token.start) and (end == length or text[end].isspace()):
                found.append((token.start, keyword))
    return found


def split_top_level(text: str, separator: str, *, angle: bool = False) -> list[str]:
    """Split *text* at each *separator* (one character) at depth 0; the parts are not stripped.

    ``angle=True`` nests ``<>`` too — for a kind (``array<int, 3>``), never for an expression, where ``<`` compares.
    """
    parts: list[str] = []
    begin = 0
    for token in _scan(text, angle=angle):
        if token.kind == _PUNCT_TOKEN and not token.depth and text[token.start] == separator:
            parts.append(text[begin : token.start])
            begin = token.end
    parts.append(text[begin:])
    return parts
