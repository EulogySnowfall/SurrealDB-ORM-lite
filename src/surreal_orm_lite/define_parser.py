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

**Kinds** — ``parse_kind()`` reads a ``TYPE`` (or a function's ``->``) kind into a small tree (``KName``,
``KLiteral``, ``KUnion``, ``KObject``, ``KTuple``); ``render_kind()`` writes a tree back in one canonical form that
both lines accept, preferring the 3.x spacing: SurrealDB 2.x renders an optional kind ``option<int>``, 3.x
``none | int`` (also ``int | none``, at any depth: ``array<none | int, 5>``), and both become ``option<…>``.
``normalise_kind()`` gives ``(field_type, nullable)``: the top-level optional is stripped and read as ``nullable``
(``"none | int"`` and ``"option<int>"`` both give ``("int", True)``). ``null`` is a value, not ``none``:
``null | int`` stays as it is.

**Statements** — each ``parse_define_*`` reads its header positionally (``DEFINE <KIND>``, the name, ``ON`` and the
table: ``DEFINE FIELD TYPE ON TYPE TYPE int`` is the field ``TYPE`` of the table ``TYPE``), then the clauses with
``read_clauses()`` against its grammar, a sequence of ``Slot``s in the order the servers render them. A reading
assigns top-level keyword candidates to slots in that order, each content passing its slot's validator (``expr``,
the weak one, refuses an empty text or one ending with an operator; ``string``, ``kind``, ``perms`` and the other
strict ones rule bare identifiers out), and leaves no keyword inside a content where it can start its own clause.
Exactly one reading is read; none or several raise ``DefineParseError`` — ``VALUE b ASSERT ASSERT c`` is never
guessed. ``parse_define_table()`` and ``parse_define_field()`` build ``TableState`` and ``FieldState`` by keyword,
``raw`` the statement; ``parse_permissions()`` reads a ``PERMISSIONS`` clause.
"""

import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple, TypeAlias

from .schema_state import FieldState, TableState

__all__ = [
    "Clause",
    "DefineParseError",
    "KLiteral",
    "KName",
    "KObject",
    "KTuple",
    "KUnion",
    "Kind",
    "ParsedField",
    "Slot",
    "canonical_name",
    "normalise_kind",
    "parse_define_field",
    "parse_define_table",
    "parse_kind",
    "parse_kind_prefix",
    "parse_permissions",
    "read_clauses",
    "read_identifier",
    "read_idiom",
    "render_kind",
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


def _keyword_tokens(text: str, tokens: Sequence[_Token], keywords: Sequence[str], start: int) -> list[tuple[int, int, str]]:
    """``(token index, position, keyword)`` of every top-level keyword candidate among the *tokens* of
    ``text[start:]`` — what ``top_level_keywords()`` returns, with the index of the keyword's first token."""
    length = len(text)
    found: list[tuple[int, int, str]] = []
    for index, token in enumerate(tokens):
        if token.kind != _WORD_TOKEN or token.depth or (token.start > start and not text[token.start - 1].isspace()):
            continue
        for keyword in keywords:
            end = token.start + len(keyword)
            if text.startswith(keyword, token.start) and (end == length or text[end].isspace()):
                found.append((index, token.start, keyword))
    return found


def top_level_keywords(text: str, keywords: Iterable[str], start: int = 0) -> list[tuple[int, str]]:
    """Every ``(position, keyword)`` at depth 0 of ``text[start:]`` where one of *keywords* stands as a whole word.

    Outside strings and quoted identifiers, outside brackets and IF units, with whitespace or an edge of the scanned
    text on both sides. Case-sensitive: the server renders keywords upper-case. A keyword may be two words separated
    by the single space the server renders (``WITH REFRESH``, ``ON DELETE``). The result is in text order, keywords
    matching at one position in the order given. These are candidates only: a bare identifier may spell a keyword.
    """
    return [(position, keyword) for _, position, keyword in _keyword_tokens(text, _scan(text, start), tuple(keywords), start)]


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


# --- kinds ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KName:
    """A named kind and its generic arguments: ``int``, ``array<string, 10>``, ``record<a | b>``.

    ``args``: for ``array`` and ``set``, the item kind then the size if one is given (``(KName("string"), "10")``);
    for ``record`` and ``geometry``, the names as rendered (``("a", "b")``); for any other name, the text between the
    angle brackets, stripped (``file<bucket>`` → ``("bucket",)``). An unknown name is kept (``KName("frob")``).
    """

    name: str
    args: tuple["Kind | str", ...] = ()


@dataclass(frozen=True, slots=True)
class KLiteral:
    """A literal kind, as rendered: ``'a'``, ``"it's"``, ``1.5f``, ``-2``, ``1.5dec``, ``true``, ``null`` or ``none``."""

    text: str


@dataclass(frozen=True, slots=True)
class KUnion:
    """A union of two or more kinds, flattened: ``option<T>`` is the union of ``none`` and the members of ``T``."""

    members: tuple["Kind", ...]


@dataclass(frozen=True, slots=True)
class KObject:
    """An object literal kind ``{ a: int, b: string }``: its ``(key, kind)`` items in order, keys as rendered."""

    items: tuple[tuple[str, "Kind"], ...]


@dataclass(frozen=True, slots=True)
class KTuple:
    """A tuple literal kind ``[int, string]``: its item kinds in order."""

    items: tuple["Kind", ...]


#: A parsed kind (spec §6.3.5).
Kind: TypeAlias = KName | KLiteral | KUnion | KObject | KTuple

_KIND_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: ``-? digits [. digits] [f | dec]`` — 3.x and 2.x render a float literal ``1.5f``.
_KIND_NUMBER = re.compile(r"-?[0-9]+(?:\.[0-9]+)?(?:f|dec)?(?![A-Za-z0-9_])")
_KIND_SIZE = re.compile(r"[0-9]+(?![A-Za-z0-9_])")
_KIND_LITERAL_WORDS = frozenset({"true", "false", "none", "null"})
#: Kinds whose arguments are an item kind and an optional size.
_ITEM_KINDS = frozenset({"array", "set"})
#: Kinds whose arguments are names separated by ``|``.
_NAMED_KINDS = frozenset({"record", "geometry"})
_KIND_CLOSERS = frozenset(_KIND_BRACKETS.values())
_NONE_KIND = KLiteral("none")
#: Nesting levels past any kind a server renders: deeper server text is refused, never a ``RecursionError``.
_MAX_KIND_DEPTH = 100


def _quote_close(char: str, *, strings: bool) -> str | None:
    """The closing character of a quoted identifier (or, with *strings*, of a string literal) opening with *char*."""
    close = _IDENTIFIER_QUOTES.get(char)
    if close is None and strings and char in _QUOTES:
        return char
    return close


class _KindReader:
    """A recursive-descent reader of the kind grammar of spec §6.3.5, from a position of a text.

    ::

        kind    := member ("|" member)*
        member  := "option" "<" kind ">" | NAME ["<" args ">"] | STRING | NUMBER | true | false | none | null
                 | "{" [key ":" kind ("," key ":" kind)*] "}" | "[" kind ("," kind)* "]"
        args    := array/set: kind ["," INT]; record/geometry: NAME ("|" NAME)*; any other name: its text

    Every failure raises ``DefineParseError(_R_KIND)``.
    """

    __slots__ = ("pos", "text")

    def __init__(self, text: str, pos: int) -> None:
        self.text = text
        self.pos = pos

    def skip_space(self) -> str:
        """Skip whitespace; return the character now at the position (``""`` at the end of the text)."""
        space = _SPACE.match(self.text, self.pos)
        if space is not None:
            self.pos = space.end()
        return self.text[self.pos : self.pos + 1]

    def expect(self, char: str) -> None:
        """Read *char*, after whitespace."""
        if self.skip_space() != char:
            raise DefineParseError(_R_KIND)
        self.pos += 1

    def kind(self, depth: int) -> Kind:
        """``member ("|" member)*``: one member, or a flattened ``KUnion``."""
        if depth > _MAX_KIND_DEPTH:
            raise DefineParseError(_R_KIND)
        members = self.member(depth)
        while self.skip_space() == "|":
            self.pos += 1
            members.extend(self.member(depth))
        return members[0] if len(members) == 1 else KUnion(tuple(members))

    def member(self, depth: int) -> list[Kind]:
        """One member — ``option<T>`` gives ``none`` and the members of ``T``."""
        char = self.skip_space()
        text, start = self.text, self.pos
        if char in _QUOTES:
            end = _delimited_end(text, start, char)
            if end is None:
                raise DefineParseError(_R_KIND)
            self.pos = end
            return [KLiteral(text[start:end])]
        if char == "{":
            return [self.object_literal(depth)]
        if char == "[":
            return [self.tuple_literal(depth)]
        number = _KIND_NUMBER.match(text, start)
        if number is not None:
            self.pos = number.end()
            return [KLiteral(number.group())]
        name = _KIND_NAME.match(text, start)
        if name is None:
            raise DefineParseError(_R_KIND)
        word = name.group()
        self.pos = name.end()
        if word in _KIND_LITERAL_WORDS:
            return [KLiteral(word)]
        opens = text.startswith("<", self.pos)
        if word == "option":
            if not opens:
                raise DefineParseError(_R_KIND)
            self.pos += 1
            inner = self.kind(depth + 1)
            self.expect(">")
            return [_NONE_KIND, *(inner.members if isinstance(inner, KUnion) else (inner,))]
        if not opens:
            return [KName(word)]
        self.pos += 1
        return [KName(word, self.arguments(word, depth))]

    def arguments(self, name: str, depth: int) -> tuple[Kind | str, ...]:
        """The arguments of ``name<…>`` (the position is just after ``<``), up to and including the closing ``>``."""
        if name in _ITEM_KINDS:
            item = self.kind(depth + 1)
            if self.skip_space() != ",":
                self.expect(">")
                return (item,)
            self.pos += 1
            self.skip_space()
            size = _KIND_SIZE.match(self.text, self.pos)
            if size is None:
                raise DefineParseError(_R_KIND)
            self.pos = size.end()
            self.expect(">")
            return (item, size.group())
        if name in _NAMED_KINDS:
            names = [self.identifier(strings=False)]
            while self.skip_space() == "|":
                self.pos += 1
                names.append(self.identifier(strings=False))
            self.expect(">")
            return tuple(names)
        return (self.raw_argument(),)

    def identifier(self, *, strings: bool) -> str:
        """An identifier as rendered — bare, or quoted between backquotes or ``⟨⟩`` — or, with *strings*, a string
        literal too (an object key)."""
        char = self.skip_space()
        start = self.pos
        close = _quote_close(char, strings=strings)
        if close is not None:
            end = _delimited_end(self.text, start, close)
        else:
            word = _WORD.match(self.text, start)
            end = None if word is None else word.end()
        if end is None:
            raise DefineParseError(_R_KIND)
        self.pos = end
        return self.text[start:end]

    def raw_argument(self) -> str:
        """The text up to the ``>`` that closes ``name<``, stripped and non-empty: brackets (``<>`` included) must
        match, string literals and quoted identifiers are skipped."""
        text = self.text
        start = index = self.pos
        expected: list[str] = []
        length = len(text)
        while index < length:
            char = text[index]
            close = _quote_close(char, strings=True)
            if close is not None:
                end = _delimited_end(text, index, close)
                if end is None:
                    raise DefineParseError(_R_KIND)
                index = end
                continue
            if char in _KIND_BRACKETS:
                expected.append(_KIND_BRACKETS[char])
            elif char in _KIND_CLOSERS:
                if expected:
                    if expected.pop() != char:
                        raise DefineParseError(_R_KIND)
                elif char == ">" and text[start:index].strip():
                    self.pos = index + 1
                    return text[start:index].strip()
                else:
                    raise DefineParseError(_R_KIND)
            index += 1
        raise DefineParseError(_R_KIND)

    def object_literal(self, depth: int) -> KObject:
        """``{ key: kind, … }`` (the position is at ``{``); ``{}`` is the empty object."""
        self.pos += 1
        items: list[tuple[str, Kind]] = []
        if self.skip_space() != "}":
            while True:
                key = self.identifier(strings=True)
                self.expect(":")
                items.append((key, self.kind(depth + 1)))
                if self.skip_space() != ",":
                    break
                self.pos += 1
        self.expect("}")
        return KObject(tuple(items))

    def tuple_literal(self, depth: int) -> KTuple:
        """``[kind, …]`` (the position is at ``[``), one item at least."""
        self.pos += 1
        items = [self.kind(depth + 1)]
        while self.skip_space() == ",":
            self.pos += 1
            items.append(self.kind(depth + 1))
        self.expect("]")
        return KTuple(tuple(items))


def parse_kind(text: str) -> Kind:
    """Parse the whole of *text* — a ``TYPE`` kind as the server rendered it, whitespace around it allowed.

    ``option<T>`` and a union holding ``none`` give the same tree (``"option<int>"`` and ``"none | int"``:
    ``KUnion((KLiteral("none"), KName("int")))``). Raises ``DefineParseError`` (``_R_KIND``) for anything the
    grammar does not cover, or a kind nested deeper than any a server renders.
    """
    reader = _KindReader(text, 0)
    kind = reader.kind(0)
    if reader.skip_space():
        raise DefineParseError(_R_KIND)
    return kind


def parse_kind_prefix(text: str, pos: int) -> tuple[Kind, int]:
    """Parse the kind starting at *pos* (after whitespace) and stop where it ends: ``(kind, end)``.

    ``end`` is the position of the first character after the kind and the whitespace following it — the ``{`` of a
    function's body after ``-> { a: int }`` or ``-> string``. Nothing after the kind is read. Raises
    ``DefineParseError`` (``_R_KIND``) when no kind starts there.
    """
    reader = _KindReader(text, pos)
    kind = reader.kind(0)
    reader.skip_space()
    return kind, reader.pos


def _flat_members(members: Iterable[Kind]) -> list[Kind]:
    flat: list[Kind] = []
    for member in members:
        flat.extend(_flat_members(member.members) if isinstance(member, KUnion) else (member,))
    return flat


def _render_argument(argument: Kind | str) -> str:
    return argument if isinstance(argument, str) else render_kind(argument)


def render_kind(kind: Kind) -> str:
    """The canonical text of *kind*, which both lines accept as input and which parses back to the same tree.

    Members joined by ``" | "``, a union holding ``none`` written ``option<…>`` at any depth (``none`` moved out,
    the other members in order), generic arguments joined by ``", "`` (``record`` and ``geometry`` names by
    ``" | "``), ``{ a: int, b: string }``, ``[int, string]``; literals, names and keys as rendered.
    """
    if isinstance(kind, KLiteral):
        return kind.text
    if isinstance(kind, KUnion):
        members = _flat_members(kind.members)
        rest = [member for member in members if member != _NONE_KIND]
        if not rest:
            return _NONE_KIND.text
        joined = " | ".join(render_kind(member) for member in rest)
        return joined if len(rest) == len(members) else f"option<{joined}>"
    if isinstance(kind, KObject):
        return "{ " + ", ".join(f"{key}: {render_kind(value)}" for key, value in kind.items) + " }"
    if isinstance(kind, KTuple):
        return "[" + ", ".join(render_kind(item) for item in kind.items) + "]"
    if not kind.args:
        return kind.name
    separator = " | " if kind.name in _NAMED_KINDS else ", "
    return f"{kind.name}<{separator.join(_render_argument(argument) for argument in kind.args)}>"


def normalise_kind(text: str) -> tuple[str, bool]:
    """``(field_type, nullable)`` for a rendered kind: the canonical text with the top-level optional stripped.

    ``option<X>`` → ``X``, nullable; a top-level union holding ``none`` → that union without ``none`` (its only other
    member alone), nullable; anything else → its canonical text, not nullable. So each SurrealDB 2.x rendering and its
    3.x rendering give one result: ``"option<int>"``, ``"none | int"`` and ``"int | none"`` → ``("int", True)``;
    ``"array<none | int, 5>"`` → ``("array<option<int>, 5>", False)``; ``"null | int"`` → ``("null | int", False)``.
    Raises ``DefineParseError`` (``_R_KIND``) as ``parse_kind()``.
    """
    kind = parse_kind(text)
    if isinstance(kind, KUnion):
        members = _flat_members(kind.members)
        rest = tuple(member for member in members if member != _NONE_KIND)
        if rest and len(rest) < len(members):
            return render_kind(rest[0] if len(rest) == 1 else KUnion(rest)), True
    return render_kind(kind), False


# --- clause contents and their validators --------------------------------------------------------------------------


class _Span(NamedTuple):
    """The content of a clause: the tokens ``first`` to ``stop`` (excluded) of a scanned text, the whitespace around
    them left out.

    A content runs between two top-level keywords, or from one to the end of the text: every bracket and IF unit
    opened in it closes in it, and its own tokens stand at depth 0.
    """

    text: str
    tokens: Sequence[_Token]
    first: int
    stop: int

    def content(self) -> str:
        """The text of the content (``""`` when it is empty)."""
        if self.first >= self.stop:
            return ""
        return self.text[self.tokens[self.first].start : self.tokens[self.stop - 1].end]

    def top(self) -> list[_Token]:
        """The tokens at the content's own depth, whitespace left out."""
        return [token for token in self.tokens[self.first : self.stop] if not token.depth and token.kind != _SPACE_TOKEN]

    def word(self, index: int) -> str | None:
        """The text of the token at *index* when it is a word, else ``None``."""
        token = self.tokens[index]
        return self.text[token.start : token.end] if token.kind == _WORD_TOKEN else None


#: A validator says whether a clause's content is one its slot accepts (spec §6.3.2's table).
_Validator: TypeAlias = Callable[[_Span], bool]


def _trimmed(text: str, tokens: Sequence[_Token], first: int, stop: int) -> _Span:
    """The tokens ``first`` to ``stop`` of *text*, the whitespace tokens at both ends left out."""
    while first < stop and tokens[first].kind == _SPACE_TOKEN:
        first += 1
    while stop > first and tokens[stop - 1].kind == _SPACE_TOKEN:
        stop -= 1
    return _Span(text, tokens, first, stop)


def _scan_span(content: str) -> _Span:
    """*content* scanned on its own, as a span; raises ``DefineParseError`` as ``_scan()``."""
    tokens = _scan(content)
    return _trimmed(content, tokens, 0, len(tokens))


def _validates(validator: _Validator, content: str) -> bool:
    """Whether *validator* accepts *content* scanned on its own (a text that does not scan is refused)."""
    try:
        span = _scan_span(content)
    except DefineParseError:
        return False
    return validator(span)


def _skip_space(text: str, pos: int) -> int:
    space = _SPACE.match(text, pos)
    return pos if space is None else space.end()


#: An expression never ends with one of these characters outside a string (spec §6.3.2, ``expr``).
_OPERATOR_ENDS = frozenset("+-*/%=<>!&|^,.:?~@")
_TABLE_TYPES = frozenset({"NORMAL", "ANY", "RELATION"})
_ON_DELETE_ACTIONS = frozenset({"IGNORE", "UNSET", "CASCADE", "REJECT"})
_DURATION = r"(?:[0-9]+(?:ns|us|µs|ms|s|m|h|d|w|y))+"
_CHANGEFEED = re.compile(rf"({_DURATION})( INCLUDE ORIGINAL)?")
_DURATION_ITEM = re.compile(rf"FOR (GRANT|TOKEN|SESSION) ({_DURATION}|NONE)")
_GROUPS = re.compile(r"\(\)(?:,\(\))*")
_FN_NAME = re.compile(r"fn(?:::[A-Za-z0-9_]+)+")
_WORD_LIST = re.compile(r"[A-Za-z0-9_]+(?:\s*,\s*[A-Za-z0-9_]+)*")
_FIELD_ACTIONS = ("select", "create", "update")
_TABLE_ACTIONS = ("select", "create", "update", "delete")


def _valid_flag(span: _Span) -> bool:
    """``flag``: an empty content."""
    return span.first >= span.stop


def _valid_expr(span: _Span) -> bool:
    """``expr`` — the weak validator: non-empty, depth-balanced (true of every content), and its last character
    outside strings is not an operator, but for the ``*`` of the idiom ``x.*``."""
    if span.first >= span.stop:
        return False
    last = span.tokens[span.stop - 1]
    char = span.text[last.start]
    if last.kind != _PUNCT_TOKEN or char not in _OPERATOR_ENDS:
        return True
    if char != "*" or span.stop - 2 < span.first:
        return False
    before = span.tokens[span.stop - 2]
    return before.kind == _PUNCT_TOKEN and span.text[before.start] == "." and before.end == last.start


def _valid_string(span: _Span) -> bool:
    """``string``: exactly one string literal."""
    return span.stop - span.first == 1 and span.tokens[span.first].kind == _STRING_TOKEN


def _valid_kind(span: _Span) -> bool:
    """``kind``: ``parse_kind()`` reads all of it."""
    try:
        parse_kind(span.content())
    except DefineParseError:
        return False
    return True


def _valid_type_as_written(span: _Span) -> bool:
    """The ``TYPE`` of the second pass of spec §6.3.2 item 4 (I3): non-empty — every content is depth-balanced."""
    return span.first < span.stop


def _relation_names(content: str) -> tuple[str, ...] | None:
    """The names of an ``IN``/``OUT`` clause — identifiers separated by ``|`` (``person | company``) — unescaped, or
    ``None`` when *content* is not such a list."""
    names: list[str] = []
    pos = 0
    try:
        while True:
            found = _identifier_at(content, pos)
            if found is None:
                return None
            names.append(found[0])
            pos = _skip_space(content, found[1])
            if pos == len(content):
                return tuple(names)
            if content[pos] != "|":
                return None
            pos = _skip_space(content, pos + 1)
    except DefineParseError:
        return None


def _valid_names(span: _Span) -> bool:
    """``names``: identifiers separated by ``|``."""
    return _relation_names(span.content()) is not None


def _valid_table_type(span: _Span) -> bool:
    """``table_type``: ``NORMAL``, ``ANY`` or ``RELATION``."""
    return span.content() in _TABLE_TYPES


def _default_parts(span: _Span) -> tuple[bool, _Span]:
    """``(always, expression)`` of a ``DEFAULT`` content: a content starting with the word ``ALWAYS`` followed by more
    text is ``DEFAULT ALWAYS <expression>``; anything else is the expression (a field named ``ALWAYS``)."""
    first = span.first
    if span.stop - first > 2 and span.word(first) == "ALWAYS" and span.tokens[first + 1].kind == _SPACE_TOKEN:
        return True, _trimmed(span.text, span.tokens, first + 2, span.stop)
    return False, span


def _valid_default(span: _Span) -> bool:
    """``default``: ``ALWAYS <expr>`` or ``<expr>``."""
    return _valid_expr(_default_parts(span)[1])


def _split_default(content: str) -> tuple[bool, str]:
    """``(default_always, default)`` of a ``DEFAULT`` content that ``_valid_default`` accepted."""
    always, expression = _default_parts(_scan_span(content))
    return always, expression.content()


def _reference_parts(span: _Span) -> tuple[bool, str | None]:
    """``(valid, on_delete)`` of a ``REFERENCE`` content: empty (``None``), or ``ON DELETE`` followed by ``IGNORE``,
    ``UNSET``, ``CASCADE``, ``REJECT`` or ``THEN <expr>`` — that text, as rendered."""
    first, stop = span.first, span.stop
    if first >= stop:
        return True, None
    head = stop - first >= 5 and span.word(first) == "ON" and span.word(first + 2) == "DELETE"
    if not head or span.tokens[first + 1].kind != _SPACE_TOKEN or span.tokens[first + 3].kind != _SPACE_TOKEN:
        return False, None
    action = first + 4
    word = span.word(action)
    if stop - action == 1 and word in _ON_DELETE_ACTIONS:
        return True, word
    then = (
        word == "THEN"
        and stop - action > 2
        and span.tokens[action + 1].kind == _SPACE_TOKEN
        and _valid_expr(_trimmed(span.text, span.tokens, action + 2, stop))
    )
    if then:
        return True, span.text[span.tokens[action].start : span.tokens[stop - 1].end]
    return False, None


def _valid_reference(span: _Span) -> bool:
    """``reference``: empty, or ``ON DELETE`` and its action."""
    return _reference_parts(span)[0]


def _valid_view(span: _Span) -> bool:
    """``view``: starts with ``SELECT `` and passes ``expr``."""
    first = span.first
    return (
        span.stop - first > 2
        and span.word(first) == "SELECT"
        and span.tokens[first + 1].kind == _SPACE_TOKEN
        and _valid_expr(span)
    )


def _changefeed_parts(content: str) -> tuple[str, bool] | None:
    """``(duration, include_original)`` of a ``CHANGEFEED`` content, or ``None``."""
    match = _CHANGEFEED.fullmatch(content)
    return None if match is None else (match.group(1), match.group(2) is not None)


def _valid_changefeed(span: _Span) -> bool:
    """``changefeed``: a duration, optionally followed by ``INCLUDE ORIGINAL``."""
    return _changefeed_parts(span.content()) is not None


def _top_punctuation(span: _Span) -> str | None:
    """The characters of the content's top-level tokens when they are all brackets or punctuation, else ``None``."""
    top = span.top()
    if any(token.kind != _PUNCT_TOKEN for token in top):
        return None
    return "".join(span.text[token.start] for token in top)


def _valid_body(span: _Span) -> bool:
    """``body``: one ``{…}`` block, or ``(…)`` groups separated by top-level commas — nothing else."""
    shape = _top_punctuation(span)
    return shape is not None and (shape == "{}" or _GROUPS.fullmatch(shape) is not None)


def _valid_group(span: _Span) -> bool:
    """``group``: one ``(…)`` group."""
    return _top_punctuation(span) == "()"


def _durations(content: str) -> dict[str, str] | None:
    """``{"GRANT" | "TOKEN" | "SESSION": duration or "NONE"}`` of a ``DURATION`` content, or ``None``."""
    try:
        items = split_top_level(content, ",")
    except DefineParseError:
        return None
    durations: dict[str, str] = {}
    for item in items:
        match = _DURATION_ITEM.fullmatch(item.strip())
        if match is None or match.group(1) in durations:
            return None
        durations[match.group(1)] = match.group(2)
    return durations


def _valid_durations(span: _Span) -> bool:
    """``durations``: ``FOR GRANT|TOKEN|SESSION <duration|NONE>`` items separated by commas, each kind once."""
    return _durations(span.content()) is not None


def _valid_tokens(span: _Span) -> bool:
    """``tokens``: words, numbers and string literals only (no brackets); empty is accepted."""
    return all(
        token.kind in (_WORD_TOKEN, _STRING_TOKEN, _SPACE_TOKEN)
        or (token.kind == _PUNCT_TOKEN and span.text[token.start] in "-.")
        for token in span.tokens[span.first : span.stop]
    )


def _valid_fn_name(span: _Span) -> bool:
    """``fn_name``: ``fn::`` followed by ``::``-separated identifiers."""
    return _FN_NAME.fullmatch(span.content()) is not None


def _valid_word_list(span: _Span) -> bool:
    """``word_list``: words separated by commas (spaces optional)."""
    return _WORD_LIST.fullmatch(span.content()) is not None


def _valid_filter_list(span: _Span) -> bool:
    """``filter_list``: ``WORD`` or ``WORD(args)`` items separated by commas, spaces optional after a comma."""
    try:
        items = [_scan_span(item) for item in split_top_level(span.content(), ",")]
    except DefineParseError:
        return False
    for item in items:
        top = item.top()
        if not top or top[0].kind != _WORD_TOKEN:
            return False
        if len(top) != 1 and "".join(item.text[token.start : token.end] for token in top[1:]) != "()":
            return False
    return True


def _permission_rule(span: _Span, top: Sequence[int], begin: int, end: int) -> str | None:
    """The rule of a permission group — its top-level tokens ``top[begin:end]`` — as rendered: ``FULL``, ``NONE`` or
    ``WHERE <expr>``; ``None`` for anything else."""
    if begin >= end:
        return None
    word = span.word(top[begin])
    if end - begin == 1 and word in ("FULL", "NONE"):
        return word
    if word == "WHERE" and end - begin > 1 and _valid_expr(_Span(span.text, span.tokens, top[begin + 1], top[end - 1] + 1)):
        return span.text[span.tokens[top[begin]].start : span.tokens[top[end - 1]].end]
    return None


def _read_permissions(span: _Span, actions: Sequence[str], unnamed: str) -> dict[str, str] | None:
    """The rules of a ``PERMISSIONS`` content (see ``parse_permissions()``), or ``None``."""
    content = span.content()
    if content in ("FULL", "NONE"):
        return dict.fromkeys(actions, content)
    top = [index for index in range(span.first, span.stop) if not span.tokens[index].depth]
    top = [index for index in top if span.tokens[index].kind != _SPACE_TOKEN]

    def comma(position: int) -> bool:
        token = span.tokens[top[position]]
        return token.kind == _PUNCT_TOKEN and span.text[token.start] == ","

    # A group starts at a top-level FOR followed by an action, at the start or after a top-level comma.
    starts = [
        position
        for position in range(len(top) - 1)
        if span.word(top[position]) == "FOR"
        and span.word(top[position + 1]) in actions
        and (position == 0 or comma(position - 1))
    ]
    if not starts or starts[0] != 0:
        return None
    rules: dict[str, str] = {}
    for number, begin in enumerate(starts):
        end = starts[number + 1] - 1 if number + 1 < len(starts) else len(top)  # the comma before the next FOR
        named = [span.word(top[begin + 1])]
        position = begin + 2
        while position + 1 < end and comma(position) and span.word(top[position + 1]) in actions:
            named.append(span.word(top[position + 1]))
            position += 2
        rule = _permission_rule(span, top, position, end)
        if rule is None:
            return None
        for action in named:
            if action is None or action in rules:
                return None
            rules[action] = rule
    return {action: rules.get(action, unnamed) for action in actions}


def _valid_field_permissions(span: _Span) -> bool:
    """``perms`` of a field: actions ``select``, ``create`` and ``update``."""
    return _read_permissions(span, _FIELD_ACTIONS, "FULL") is not None


def _valid_table_permissions(span: _Span) -> bool:
    """``perms`` of a table: actions ``select``, ``create``, ``update`` and ``delete``."""
    return _read_permissions(span, _TABLE_ACTIONS, "NONE") is not None


def parse_permissions(text: str, actions: Sequence[str], *, unnamed: str) -> dict[str, str]:
    """The rules of a ``PERMISSIONS`` clause's content: ``{action: rule}`` for every one of *actions*.

    ``NONE`` or ``FULL`` gives that rule to every action. Otherwise the content is groups ``FOR a[, b] <rule>`` (the
    actions lower-case), each starting at a top-level ``FOR`` followed by an action — at the start of the content or
    after a top-level comma; a rule is ``FULL``, ``NONE`` or ``WHERE <expr>``, kept as rendered (``"WHERE owner =
    $auth.id"``). An action no group names gets *unnamed* — ``"FULL"`` for a field, ``"NONE"`` for a table (a
    fallback only: both lines render every action). Anything else, an action named twice included, raises
    ``DefineParseError`` (``_R_PERMISSIONS``).
    """
    try:
        span = _scan_span(text)
    except DefineParseError:
        raise DefineParseError(_R_PERMISSIONS) from None
    rules = _read_permissions(span, actions, unnamed)
    if rules is None:
        raise DefineParseError(_R_PERMISSIONS)
    return rules


# --- the clause reader ---------------------------------------------------------------------------------------------


class Slot(NamedTuple):
    """One clause of a ``DEFINE`` grammar (spec §6.3.3): its name in the result, the keywords that open it (two for
    ``SCHEMAFULL|SCHEMALESS``, ``IN``/``FROM`` …), the validator of its content, and whether a reading must use it.
    Two slots may share a name (``FLEXIBLE`` before and after ``TYPE``): a reading uses one of them at most."""

    name: str
    keywords: tuple[str, ...]
    validator: _Validator
    required: bool = False


class Clause(NamedTuple):
    """A clause of the reading: the keyword that opened it and its content, stripped (``""`` for a flag)."""

    keyword: str
    content: str


class _Candidate(NamedTuple):
    """A top-level keyword: its first token, the token after it (the length of the tokens at the end of the text),
    the keyword and the slots it may open."""

    token: int
    after: int
    keyword: str
    slots: tuple[int, ...]


#: A step of a reading: the candidate that opens the next clause (``None``: the end of the text), the slot it opens
#: (the number of slots for the end) and how many readings it leads to (1, or 2 for two or more).
_Step: TypeAlias = tuple[int | None, int, int]


class _ClauseReader:
    """The readings of the clauses of one statement after its header (spec §6.3.2), counted up to two.

    A reading picks a strictly increasing run of top-level keyword candidates, the first one where the text starts,
    and gives each a slot after the previous one's; every content must pass its slot's validator and every required
    slot be used. A reading must also leave no keyword where it can open its own clause: when a candidate inside a
    content could open a free slot between that content's slot and the next one — its keyword starting the clause
    and both contents still valid — the reading that swallows it is not counted. So an expression keeps a
    keyword-named field (``VALUE a * READONLY``, ``VALUE a + ASSERT ASSERT $value > 0``) only where the keyword
    cannot start its clause, and ``VALUE b ASSERT ASSERT c`` still has two readings. Counts are memoised on
    ``(candidate, slot, names used)``: the work is polynomial in the number of candidates whatever the text.
    """

    __slots__ = ("_candidates", "_counts", "_first", "_fits_known", "_required", "_shared", "_slots", "_text", "_tokens")

    def __init__(self, text: str, start: int, slots: Sequence[Slot]) -> None:
        self._text = text
        self._slots = tuple(slots)
        self._tokens = _scan(text, start)
        token_at = {token.start: index for index, token in enumerate(self._tokens)}
        keywords = tuple(dict.fromkeys(keyword for slot in self._slots for keyword in slot.keywords))
        self._candidates = [
            _Candidate(
                index,
                token_at.get(position + len(keyword), len(self._tokens)),
                keyword,
                tuple(number for number, slot in enumerate(self._slots) if keyword in slot.keywords),
            )
            for index, position, keyword in _keyword_tokens(text, self._tokens, keywords, start)
        ]
        names = [slot.name for slot in self._slots]
        self._shared = frozenset(name for name in names if names.count(name) > 1)
        required = [0]
        for slot in self._slots:
            required.append(required[-1] + int(slot.required))
        self._required = required
        self._first = next((index for index, token in enumerate(self._tokens) if token.kind != _SPACE_TOKEN), len(self._tokens))
        self._counts: dict[tuple[int, int, frozenset[str]], int] = {}
        self._fits_known: dict[tuple[int, int, int], bool] = {}

    def read(self) -> dict[str, Clause]:
        """The clauses of the one reading; ``DefineParseError`` with ``_R_NO_READING`` or ``_R_AMBIGUOUS``."""
        total = 0
        for *_, readings in self._starts():
            total += readings
            if total > 1:
                raise DefineParseError(_R_AMBIGUOUS)
        if not total:
            raise DefineParseError(_R_NO_READING)
        clauses: dict[str, Clause] = {}
        following, slot, _ = next(self._starts())
        used = frozenset[str]()
        while following is not None:
            current = following
            used = self._uses(used, slot)
            following, opened, _ = next(self._steps(current, slot, used))
            candidate = self._candidates[current]
            content = _trimmed(self._text, self._tokens, candidate.after, self._stop(following)).content()
            clauses[self._slots[slot].name] = Clause(candidate.keyword, content)
            slot = opened
        return clauses

    def _starts(self) -> Iterator[_Step]:
        """The first steps: a candidate where the text starts (or the end of an empty text)."""
        end = len(self._slots)
        if self._first == len(self._tokens):
            if not self._skips_required(-1, end):
                yield None, end, 1
            return
        for number, candidate in enumerate(self._candidates):
            if candidate.token != self._first:
                continue
            for opened in candidate.slots:
                if self._skips_required(-1, opened):
                    continue
                readings = self._count(number, opened, self._uses(frozenset(), opened))
                if readings:
                    yield number, opened, readings

    def _steps(self, current: int, slot: int, used: frozenset[str]) -> Iterator[_Step]:
        """The steps that complete a reading after candidate *current* opened *slot* (*used*: the shared names of the
        slots used so far, *slot*'s included)."""
        after = self._candidates[current].after
        end = len(self._slots)
        if (
            not self._skips_required(slot, end)
            and self._fits(slot, after, len(self._tokens))
            and self._maximal(current, slot, used, None, end)
        ):
            yield None, end, 1
        for following in range(current + 1, len(self._candidates)):
            candidate = self._candidates[following]
            if candidate.token < after:
                continue
            for opened in candidate.slots:
                if opened <= slot or self._slots[opened].name in used or self._skips_required(slot, opened):
                    continue
                if not self._fits(slot, after, candidate.token):
                    break
                readings = self._count(following, opened, self._uses(used, opened))
                if readings and self._maximal(current, slot, used, following, opened):
                    yield following, opened, readings

    def _count(self, current: int, slot: int, used: frozenset[str]) -> int:
        """The readings (0, 1, or 2 for two or more) that complete after candidate *current* opened *slot*."""
        key = (current, slot, used)
        known = self._counts.get(key)
        if known is None:
            known = 0
            for *_, readings in self._steps(current, slot, used):
                known += readings
                if known > 1:
                    break
            known = min(known, 2)
            self._counts[key] = known
        return known

    def _maximal(self, current: int, slot: int, used: frozenset[str], following: int | None, opened: int) -> bool:
        """Whether no candidate between *current* and *following* can open a free slot between *slot* and *opened*
        with both contents valid — the content of *slot* then swallows no keyword that can start its clause."""
        after = self._candidates[current].after
        stop = self._stop(following)
        taken = used if following is None else used | {self._slots[opened].name}
        for between in range(current + 1, len(self._candidates)):
            candidate = self._candidates[between]
            if candidate.token >= stop:
                break
            if candidate.token < after or candidate.after > stop:
                continue
            for inserted in candidate.slots:
                if (
                    slot < inserted < opened
                    and self._slots[inserted].name not in taken
                    and self._fits(slot, after, candidate.token)
                    and self._fits(inserted, candidate.after, stop)
                ):
                    return False
        return True

    def _fits(self, slot: int, first: int, stop: int) -> bool:
        """Whether the tokens ``first`` to ``stop`` are a content *slot* accepts (memoised)."""
        key = (slot, first, stop)
        known = self._fits_known.get(key)
        if known is None:
            known = self._slots[slot].validator(_trimmed(self._text, self._tokens, first, stop))
            self._fits_known[key] = known
        return known

    def _skips_required(self, slot: int, following: int) -> bool:
        """Whether a required slot lies strictly between *slot* (-1: the start) and *following* (the number of slots:
        the end)."""
        return self._required[following] - self._required[slot + 1] > 0

    def _uses(self, used: frozenset[str], slot: int) -> frozenset[str]:
        name = self._slots[slot].name
        return used | {name} if name in self._shared else used

    def _stop(self, following: int | None) -> int:
        return len(self._tokens) if following is None else self._candidates[following].token


def read_clauses(text: str, start: int, slots: Sequence[Slot]) -> dict[str, Clause]:
    """Read the clauses of ``text[start:]`` — what follows a statement's header — against the grammar *slots*, in
    their order: ``{slot name: Clause(keyword, content)}`` for the clauses of the one reading.

    The text after the header is empty or starts, after whitespace, at a top-level keyword. A reading assigns a
    strictly increasing run of top-level keyword candidates to slots in their order, each slot (each name) once;
    each content — the stripped text up to the next chosen keyword, the last one to the end — passes its slot's
    validator; every required slot is used; and no keyword is left inside a content where it could open its own
    clause (see ``_ClauseReader``). Exactly one reading gives the clauses; none raises ``DefineParseError``
    (``_R_NO_READING``), two or more ``_R_AMBIGUOUS`` — never a guess. A text that does not scan raises its scan
    error (``_R_STRING``, ``_R_IDENT``, ``_R_BRACKETS``, ``_R_IF``).
    """
    return _ClauseReader(text, start, slots).read()


# --- DEFINE TABLE and DEFINE FIELD ---------------------------------------------------------------------------------

_TABLE_TYPE_SLOT = Slot("type", ("TYPE",), _valid_table_type)
_RELATION_SLOTS = (
    Slot("in", ("IN", "FROM"), _valid_names),
    Slot("out", ("OUT", "TO"), _valid_names),
    Slot("enforced", ("ENFORCED",), _valid_flag),
)
_TABLE_TAIL_SLOTS = (
    Slot("drop", ("DROP",), _valid_flag),
    Slot("schema", ("SCHEMAFULL", "SCHEMALESS"), _valid_flag),
    Slot("comment", ("COMMENT",), _valid_string),
    Slot("as", ("AS",), _valid_view),
    Slot("changefeed", ("CHANGEFEED",), _valid_changefeed),
    Slot("permissions", ("PERMISSIONS",), _valid_table_permissions),
)
#: Spec §6.3.3, a table: ``IN``, ``OUT`` and ``ENFORCED`` only after ``TYPE RELATION``.
_TABLE_SLOTS = (_TABLE_TYPE_SLOT, *_TABLE_TAIL_SLOTS)
_RELATION_TABLE_SLOTS = (_TABLE_TYPE_SLOT, *_RELATION_SLOTS, *_TABLE_TAIL_SLOTS)
#: ``TYPE RELATION`` right after a table's name: every reading of the clauses then has that TYPE, and no other does.
_RELATION_TYPE = re.compile(r"\s+TYPE\s+RELATION(?:\s|\Z)")

#: Spec §6.3.3, a field (M10's order; ``FLEXIBLE`` before ``TYPE`` on 2.x, after it on 3.x, M9).
_FIELD_SLOTS = (
    Slot("flexible", ("FLEXIBLE",), _valid_flag),
    Slot("type", ("TYPE",), _valid_kind),
    Slot("flexible", ("FLEXIBLE",), _valid_flag),
    Slot("reference", ("REFERENCE",), _valid_reference),
    Slot("default", ("DEFAULT",), _valid_default),
    Slot("readonly", ("READONLY",), _valid_flag),
    Slot("value", ("VALUE",), _valid_expr),
    Slot("computed", ("COMPUTED",), _valid_expr),
    Slot("assert", ("ASSERT",), _valid_expr),
    Slot("comment", ("COMMENT",), _valid_string),
    Slot("permissions", ("PERMISSIONS",), _valid_field_permissions),
)
#: The second pass of spec §6.3.2 item 4 (I3): ``TYPE`` only non-empty.
_FIELD_SLOTS_TYPE_AS_WRITTEN = tuple(
    slot._replace(validator=_valid_type_as_written) if slot.name == "type" else slot for slot in _FIELD_SLOTS
)


def _header(text: str, kind: str) -> int:
    """The position after ``DEFINE <kind>`` and the whitespace after it; ``_R_STATEMENT`` for another statement."""
    prefix = f"DEFINE {kind}"
    if not text.startswith(prefix) or (len(text) > len(prefix) and not text[len(prefix)].isspace()):
        raise DefineParseError(_R_STATEMENT)
    return _skip_space(text, len(prefix))


def _expect_space(text: str, pos: int) -> int:
    """The position after the whitespace at *pos*, which must be there (``_R_HEADER``)."""
    if pos >= len(text) or not text[pos].isspace():
        raise DefineParseError(_R_HEADER)
    return _skip_space(text, pos)


def _expect_word(text: str, pos: int, word: str) -> int:
    """The position after the header word *word* at *pos* and the whitespace after it (``_R_HEADER``)."""
    if not text.startswith(word, pos):
        raise DefineParseError(_R_HEADER)
    return _expect_space(text, pos + len(word))


def _name_end(text: str, pos: int) -> int:
    """*pos*, where a header name ends: the end of the text or whitespace (``_R_HEADER`` otherwise)."""
    if pos < len(text) and not text[pos].isspace():
        raise DefineParseError(_R_HEADER)
    return pos


def _content(clause: Clause | None) -> str | None:
    return None if clause is None else clause.content


def parse_define_table(text: str) -> TableState:
    r"""Read a ``DEFINE TABLE`` statement as the server renders it (spec §6.3.3) into a ``TableState``.

    The header ``DEFINE TABLE <ident>`` is read positionally (``DEFINE TABLE TYPE TYPE ANY …`` names the table
    ``TYPE``; ``\``we\`ird\``` gives ``we`ird``), then the clauses ``TYPE``, ``IN``/``FROM``, ``OUT``/``TO`` and
    ``ENFORCED`` (after ``TYPE RELATION`` only), ``DROP``, ``SCHEMAFULL|SCHEMALESS``, ``COMMENT``, ``AS``,
    ``CHANGEFEED`` and ``PERMISSIONS`` with ``read_clauses()``. No ``TYPE`` gives ``"any"``, no schema keyword
    ``"SCHEMALESS"``, no ``PERMISSIONS`` an empty mapping. ``fields``, ``indexes``, ``events`` and ``other`` are left
    empty: they come from ``INFO FOR TABLE``. Raises ``DefineParseError``.
    """
    pos = _header(text, "TABLE")
    name, pos = read_identifier(text, pos)
    _name_end(text, pos)
    relation = _RELATION_TYPE.match(text, pos) is not None
    clauses = read_clauses(text, pos, _RELATION_TABLE_SLOTS if relation else _TABLE_SLOTS)
    changefeed = clauses.get("changefeed")
    feed = None if changefeed is None else _changefeed_parts(changefeed.content)
    comment = clauses.get("comment")
    permissions = clauses.get("permissions")
    relation_in = clauses.get("in")
    relation_out = clauses.get("out")
    return TableState(
        name=name,
        schema_mode=clauses["schema"].keyword if "schema" in clauses else "SCHEMALESS",
        table_type=clauses["type"].content.lower() if "type" in clauses else "any",
        changefeed=None if feed is None else feed[0],
        permissions={} if permissions is None else parse_permissions(permissions.content, _TABLE_ACTIONS, unnamed="NONE"),
        view_query=_content(clauses.get("as")),
        relation_in=() if relation_in is None else _relation_names(relation_in.content) or (),
        relation_out=() if relation_out is None else _relation_names(relation_out.content) or (),
        enforced="enforced" in clauses,
        comment=None if comment is None else unquote(comment.content),
        drop="drop" in clauses,
        changefeed_include_original=feed is not None and feed[1],
        raw=text,
    )


class ParsedField(NamedTuple):
    """What ``parse_define_field()`` read: the state, and whether its ``TYPE`` was kept as written because
    ``parse_kind()`` does not understand it (the reader reports it as issue I3; ``FieldState`` has no such flag)."""

    state: FieldState
    type_as_written: bool


def parse_define_field(text: str) -> ParsedField:
    """Read a ``DEFINE FIELD`` statement as the server renders it (spec §6.3.3) into a ``FieldState``.

    The header ``DEFINE FIELD <idiom> ON <ident>`` is read positionally (``DEFINE FIELD TYPE ON TYPE TYPE int``,
    ``DEFINE FIELD ON ON t``): ``name`` is the path's canonical name (``tags[*]`` and ``tags.*`` both give
    ``"tags.*"``) and ``path`` its unescaped segments, ``table`` the identifier after ``ON``. The clauses —
    ``FLEXIBLE`` (before ``TYPE`` on 2.x, after it on 3.x), ``TYPE``, ``REFERENCE``, ``DEFAULT [ALWAYS]``,
    ``READONLY``, ``VALUE``, ``COMPUTED``, ``ASSERT``, ``COMMENT``, ``PERMISSIONS`` — are read with
    ``read_clauses()``. The kind goes through ``normalise_kind()`` (``option<int>`` and ``none | int`` both give
    ``field_type="int"``, ``nullable=True``); no ``TYPE`` gives ``"any"``, not nullable (as ``TYPE any``; ``raw``
    keeps the two apart). Expression texts are kept as rendered. When no reading fits, the clauses are read once more
    with ``TYPE`` only non-empty: exactly one reading then gives the field with its ``TYPE`` text as ``field_type``,
    not nullable, and ``type_as_written=True`` (spec §6.3.2 item 4). Raises ``DefineParseError``.
    """
    pos = _header(text, "FIELD")
    if _identifier_at(text, pos) is None:
        raise DefineParseError(_R_HEADER)
    path, pos = read_idiom(text, pos)
    pos = _expect_word(text, _expect_space(text, pos), "ON")
    table, pos = read_identifier(text, pos)
    _name_end(text, pos)
    try:
        clauses = read_clauses(text, pos, _FIELD_SLOTS)
        as_written = False
    except DefineParseError as error:
        if error.args[0] != _R_NO_READING:
            raise
        clauses = read_clauses(text, pos, _FIELD_SLOTS_TYPE_AS_WRITTEN)
        as_written = True
    kind = clauses.get("type")
    if kind is None:
        field_type, nullable = "any", False
    elif as_written:
        field_type, nullable = kind.content, False
    else:
        field_type, nullable = normalise_kind(kind.content)
    default = clauses.get("default")
    default_always, default_text = (False, None) if default is None else _split_default(default.content)
    reference = clauses.get("reference")
    comment = clauses.get("comment")
    permissions = clauses.get("permissions")
    state = FieldState(
        name=canonical_name(path),
        field_type=field_type,
        nullable=nullable,
        default=default_text,
        assertion=_content(clauses.get("assert")),
        flexible="flexible" in clauses,
        readonly="readonly" in clauses,
        value=_content(clauses.get("value")),
        reference=reference is not None,
        on_delete=None if reference is None else _reference_parts(_scan_span(reference.content))[1],
        comment=None if comment is None else unquote(comment.content),
        path=path,
        table=table,
        default_always=default_always,
        computed=_content(clauses.get("computed")),
        permissions={} if permissions is None else parse_permissions(permissions.content, _FIELD_ACTIONS, unnamed="FULL"),
        raw=text,
    )
    return ParsedField(state, as_written)
