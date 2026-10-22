"""Full-text search (v0.35.0): the search annotations, analyzers and indexes, and the statements that define them.

``SearchScore``, ``SearchHighlight`` and ``SearchOffsets`` are the full SurrealDB-ORM's ``annotate()`` values for
``QuerySet.search()`` — SurrealQL's ``search::score(ref)``, ``search::highlight(open, close, ref)`` and
``search::offsets(ref)`` — with its names, arguments, defaults and ``repr``. ``ref`` is the match reference a
``search()`` field was given (``@0@``, ``@1@`` …), an ``int`` from 0 to 255, SurrealDB's range; the highlight tags are
any ``str``, sent as bound query variables, never written into a statement.

``Analyzer`` and ``Index`` declare SurrealDB's ``DEFINE ANALYZER`` and ``DEFINE INDEX`` with the full ORM's
``DefineAnalyzer`` and ``CreateIndex`` argument names. Every argument is checked when the object is built, before any
request, and kept normalised. **Names and grammar.** Analyzer, index and table names are identifiers (letters, digits,
underscores; not starting with a digit), matched in full and written in backticks; an index field is a field name or a
dotted path, each segment written in backticks. Tokenizers come from SurrealDB's closed list (``blank``, ``camel``,
``class``, ``punct``) and filters from its closed grammar (``ascii``, ``lowercase``, ``uppercase``,
``ngram(min,max)``, ``edgengram(min,max)``, ``snowball(language)``, ``mapper('path')``), in any case with any spaces:
the ORM re-renders each from what it read, never the caller's text. Numbers are written from Python numbers (BM25's
``k1`` and ``b`` with ``repr(float(x))``), comments and a ``mapper()`` path as string literals, their backslashes and
quotes escaped, a function name through ``normalize_function_name()``. Analyzer and index definitions are trusted
application configuration; ``mapper('path')`` makes the server read one of its own files when the analyzer is defined.

The renderers are pure — no connection, the same text for the same input. ``Analyzer.ddl()`` and ``Index.ddl()``
render the statements a fresh database receives; ``analyzer_definition()`` renders an analyzer as ``INFO FOR DB`` does,
with no ``OVERWRITE``/``IF NOT EXISTS`` clause and no final ``;``, the form ``parse_define_analyzer()`` reads back.
``fulltext_keyword()`` gives the index keyword of a SurrealDB line: ``FULLTEXT`` on 3.x, ``SEARCH`` on 2.x and on the
embedded engine of the SDK's 2.x range. ``index_statement()``, ``remove_index_statement()``,
``remove_analyzer_statement()`` and ``rebuild_index_statement()`` render the rest.

A model declares its indexes with ``SurrealConfigDict(indexes=[Index(...)])``: ``check_indexes_config()`` checks the
key at the ``class`` statement, ``declared_analyzers()`` lists the analyzers the indexes hold as ``Analyzer`` objects
and ``model_index_statements()`` renders what ``index_ddl()`` returns.

**What the database stores.** ``read_database_info()`` sends ``INFO FOR DB`` once and returns the stored analyzers,
each read by ``parse_define_analyzer()``, and the table names; ``read_table_indexes()`` sends ``INFO FOR TABLE`` once
and returns a table's indexes, each read by ``parse_define_index()``. A definition the parser cannot read is ``None``
under its name, so the caller redefines it rather than trusting it. ``analyzer_matches()`` and ``index_matches()``
compare a declaration with a stored definition, pure, attribute by attribute, whichever line rendered it (3.x's
``LOWERCASE, ASCII`` or 2.x's ``LOWERCASE,ASCII``, 2.x's tuning options, ``BM25(1.2,0.75)`` for ``BM25`` and for no
clause), so an unchanged index or analyzer is never redefined — which would rebuild the index.

**What ``define_indexes()`` applies.** ``check_named_analyzers()`` refuses an index naming by ``str`` an analyzer the
database lacks; ``plan_analyzers()`` sorts the model's analyzers into those to define and those to redefine;
``plan_indexes()`` gives the index definitions and the rebuilds the table needs, comparing each index with its stored
definition; ``transaction_analyzer_refused()`` words the refusal of an analyzer to define in a transaction. All pure:
the model's method reads, decides, then applies.

**A changed analyzer.** SurrealDB leaves the records indexed before a ``DEFINE ANALYZER OVERWRITE`` with their old
tokens until the index is rebuilt, so a redefinition rebuilds every index of the database that uses it:
``indexes_using()`` reads ``INFO FOR TABLE`` of each table and lists them (``stored_indexes_using()`` for a table
already read), ``apply_analyzers()`` and ``apply_rebuilds()`` apply the statements, ``warn_redefined()`` logs one
warning per redefined analyzer. ``apply_analyzer()`` and ``apply_analyzer_removal()`` are the bodies of the connection
manager's ``define_analyzer()`` and ``remove_analyzer()``; the latter refuses, below SurrealDB 3 (2.x, the embedded
engine, which would remove it and then refuse every write to the table), an analyzer an index uses, with 3.x's own
reason.

**The query side.** ``QuerySet.search()`` checks its fields and texts with ``checked_search_field()`` and words its
refusals with ``text_refused()``, ``field_refused()``, ``too_many_fields()``, ``time_travel_refused()`` (with
``as_of()``) and ``live_refused()`` (in a live query's own condition). SurrealDB refuses a match on a field without a
full-text index with an error that names the column and, on 3.x, quotes the bound search text:
``is_missing_index_error()`` recognises it, ``missing_index_column()`` reads the column, and
``missing_index_refusal()`` builds the ORM's error naming the field, never the server's text. ``live_index_probe()``
is the empty search a live query's start check sends for each column its subqueries search.

The module sits low in the package: it imports neither ``model_base``, ``query_set`` nor ``connection_manager``, so
each of them can import it.
"""

import logging
import math
import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from numbers import Real
from typing import Any, Literal

from ._sdk import ServerError
from .define_parser import DefineParseError, parse_define_analyzer, parse_define_index, unquote
from .exceptions import SurrealDbError
from .functions import normalize_function_name
from .schema import _comment_text, _identifier_name, _missing_clause, _overwrite_clause
from .schema_state import AnalyzerState, IndexState
from .surql_literal import _identifier, _quoted
from .time_travel import line_major
from .utils import VALID_FIELD_PATTERN, _ddl_refusal_class, apply_ddl_statements, validate_field_name

__all__ = ["SearchScore", "SearchHighlight", "SearchOffsets", "Analyzer", "Index"]

#: The logger of a redefined analyzer's warning (``surreal_orm_lite.search``).
logger = logging.getLogger(__name__)

#: The largest match reference SurrealDB accepts (``@0@`` … ``@255@``).
MAX_MATCH_REFERENCE = 255

#: SurrealDB's tokenizers, in the order its documentation lists them.
TOKENIZERS = ("blank", "camel", "class", "punct")

#: The languages of SurrealDB's ``snowball()`` filter.
SNOWBALL_LANGUAGES = (
    "arabic",
    "danish",
    "dutch",
    "english",
    "finnish",
    "french",
    "german",
    "greek",
    "hungarian",
    "italian",
    "norwegian",
    "portuguese",
    "romanian",
    "russian",
    "spanish",
    "swedish",
    "tamil",
    "turkish",
)

#: The largest ``ngram``/``edgengram`` bound SurrealDB accepts (an unsigned 16-bit count).
_MAX_GRAM = 65535

#: The filters without an argument.
_PLAIN_FILTERS = frozenset({"ascii", "lowercase", "uppercase"})

#: A filter: its name, then an optional argument in parentheses — matched in full on the stripped item.
_FILTER = re.compile(r"([A-Za-z]+)\s*(?:\((.*)\))?", re.DOTALL)

#: The two bounds of ``ngram``/``edgengram``, matched in full once every space is removed.
_GRAM_BOUNDS = re.compile(r"(-?[0-9]+),(-?[0-9]+)")

#: The language of ``snowball()``, matched in full on the stripped argument.
_LANGUAGE = re.compile(r"[A-Za-z]+")

_FILTER_FORMS = (
    "SurrealDB's filters are ascii, lowercase, uppercase, ngram(min,max), edgengram(min,max), snowball(language) and "
    "mapper('path')."
)
_LANGUAGES_TEXT = f"{', '.join(SNOWBALL_LANGUAGES[:-1])} and {SNOWBALL_LANGUAGES[-1]}"
_TOKENIZERS_TEXT = f"{', '.join(TOKENIZERS[:-1])} and {TOKENIZERS[-1]}"


# --- the search annotations -----------------------------------------------------------------------------------------


def _reference(value: object, *, owner: str) -> int:
    """*value*, checked as a match reference of *owner* (the class name): an ``int``, not a ``bool``, in 0..255."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{owner} ref must be an int, got {type(value).__name__}.")
    if not 0 <= value <= MAX_MATCH_REFERENCE:
        raise ValueError(
            f"{owner} ref must be between 0 and {MAX_MATCH_REFERENCE} (SurrealDB's match references), got {value}."
        )
    return value


@dataclass(frozen=True, slots=True, init=False, repr=False)
class SearchScore:
    """BM25 relevance score of a ``search()`` field, for ``QuerySet.annotate()``: SurrealQL's ``search::score(ref)``.

    The full SurrealDB-ORM's annotation::

        articles = await (
            Article.objects()
            .search(title="physics")
            .annotate(relevance=SearchScore(0))
            .order_by("-relevance")
            .exec()
        )
        articles[0].relevance  # a float

    ``ref`` is the match reference of the searched field: ``search()`` gives each field the next one, in call order
    (``search(title="a", body="b")``: ``title`` 0, ``body`` 1). Frozen, hashable and equal by value.

    Raises:
        TypeError: ``ref`` is not an ``int`` (a ``bool`` included).
        ValueError: ``ref`` is outside 0..255, SurrealDB's match references.
    """

    ref: int

    def __init__(self, ref: int = 0) -> None:
        object.__setattr__(self, "ref", _reference(ref, owner="SearchScore"))

    def __repr__(self) -> str:
        return f"SearchScore({self.ref})"


@dataclass(frozen=True, slots=True, init=False, repr=False)
class SearchHighlight:
    """The matched terms of a ``search()`` field wrapped in ``open_tag``/``close_tag``, for ``QuerySet.annotate()``:
    SurrealQL's ``search::highlight(open_tag, close_tag, ref)``.

    The full SurrealDB-ORM's annotation::

        articles = await Article.objects().search(title="physics").annotate(snippet=SearchHighlight()).exec()
        articles[0].snippet  # 'Cooking with <b>physics</b>'

    The value is the field's text (a list of texts for an array field) with each matched term wrapped; the field's
    index needs ``highlights=True``, without which SurrealDB returns the text unchanged. The tags are any text, sent
    as bound query variables. ``ref`` is the match reference of the searched field, as for ``SearchScore``. Frozen,
    hashable and equal by value.

    Raises:
        TypeError: a tag is not a ``str``, or ``ref`` is not an ``int`` (a ``bool`` included).
        ValueError: ``ref`` is outside 0..255, SurrealDB's match references.
    """

    open_tag: str
    close_tag: str
    ref: int

    def __init__(self, open_tag: str = "<b>", close_tag: str = "</b>", ref: int = 0) -> None:
        for argument, value in (("open_tag", open_tag), ("close_tag", close_tag)):
            if not isinstance(value, str):
                raise TypeError(f"SearchHighlight {argument} must be a str, got {type(value).__name__}.")
        object.__setattr__(self, "open_tag", open_tag)
        object.__setattr__(self, "close_tag", close_tag)
        object.__setattr__(self, "ref", _reference(ref, owner="SearchHighlight"))

    def __repr__(self) -> str:
        return f"SearchHighlight({self.open_tag!r}, {self.close_tag!r}, {self.ref})"


@dataclass(frozen=True, slots=True, init=False, repr=False)
class SearchOffsets:
    """Where the matched terms of a ``search()`` field sit, for ``QuerySet.annotate()``: SurrealQL's
    ``search::offsets(ref)`` (a lite addition to the full SurrealDB-ORM's annotations).

    ::

        articles = await Article.objects().search(title="physics").annotate(spans=SearchOffsets(0)).exec()
        articles[0].spans  # {'0': [{'s': 13, 'e': 20}]}

    The value is SurrealDB's: a ``dict`` keyed by the position of the matched item as a string (``"0"`` for a
    string field), each holding the start (``s``) and end (``e``) of every matched term. ``ref`` is the match
    reference of the searched field, as for ``SearchScore``. Frozen, hashable and equal by value.

    Raises:
        TypeError: ``ref`` is not an ``int`` (a ``bool`` included).
        ValueError: ``ref`` is outside 0..255, SurrealDB's match references.
    """

    ref: int

    def __init__(self, ref: int = 0) -> None:
        object.__setattr__(self, "ref", _reference(ref, owner="SearchOffsets"))

    def __repr__(self) -> str:
        return f"SearchOffsets({self.ref})"


#: A search annotation, whichever its kind.
SearchAnnotation = SearchScore | SearchHighlight | SearchOffsets


# --- the analyzer ---------------------------------------------------------------------------------------------------


def _str_items(value: object, *, subject: str, what: str) -> tuple[str, ...]:
    """*value*, checked as a ``list`` or ``tuple`` of ``str`` (*what*: ``"tokenizers="``, ``"filters="``)."""
    if not isinstance(value, list | tuple):
        raise TypeError(f"{subject}: {what} must be a list or tuple of str, got {type(value).__name__}.")
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"{subject}: {what} holds {type(item).__name__} {item!r}, not a str.")
    return tuple(value)


def _tokenizers(value: object, *, subject: str) -> tuple[str, ...]:
    """The tokenizers of *value*, lower-cased, each one of SurrealDB's."""
    tokenizers: list[str] = []
    for item in _str_items(value, subject=subject, what="tokenizers="):
        tokenizer = item.strip().lower()
        if tokenizer not in TOKENIZERS:
            raise ValueError(f"{subject}: unknown tokenizer {item!r}; SurrealDB's tokenizers are {_TOKENIZERS_TEXT}.")
        tokenizers.append(tokenizer)
    return tuple(tokenizers)


def _mapper_path(argument: str) -> str | None:
    """The path of a ``mapper()`` filter: *argument*, one single- or double-quoted string literal, decoded — or
    ``None`` when it is not exactly one literal, is empty or holds a NUL."""
    try:
        path = unquote(argument)
    except DefineParseError:
        return None
    return path if path and "\x00" not in path else None


def _filter_text(item: str, *, subject: str) -> str:
    """One filter of an analyzer, re-rendered from what the grammar read: its name lower-cased, ``ngram``/
    ``edgengram`` bounds written from their integers, a ``snowball`` language lower-cased, a ``mapper`` path
    re-quoted — never the caller's text.

    The name and its argument may be written in any case with any spaces (a ``mapper`` path keeps its own). Refused
    with a ``ValueError``: a filter the grammar cannot read, ``ngram``/``edgengram`` bounds outside ``1 <= min <= max
    <= 65535``, an unknown ``snowball`` language.
    """
    form = _FILTER.fullmatch(item.strip())
    if form is not None:
        name, argument = form.group(1).lower(), form.group(2)
        if argument is None:
            if name in _PLAIN_FILTERS:
                return name
        elif name in ("ngram", "edgengram"):
            bounds = _GRAM_BOUNDS.fullmatch("".join(argument.split()))
            if bounds is not None:
                low, high = int(bounds.group(1)), int(bounds.group(2))
                if not 1 <= low <= high <= _MAX_GRAM:
                    raise ValueError(f"{subject}: filter {item!r} needs 1 <= min <= max <= {_MAX_GRAM}.")
                return f"{name}({low},{high})"
        elif name == "snowball":
            language = argument.strip()
            if _LANGUAGE.fullmatch(language) is not None:
                if language.lower() not in SNOWBALL_LANGUAGES:
                    raise ValueError(f"{subject}: unknown snowball language {language!r}; SurrealDB's are {_LANGUAGES_TEXT}.")
                return f"snowball({language.lower()})"
        elif name == "mapper":
            path = _mapper_path(argument.strip())
            if path is not None:
                return f"mapper({_quoted(path)})"
    raise ValueError(f"{subject}: cannot read filter {item!r}; {_FILTER_FORMS}")


def _filters(value: object, *, subject: str) -> tuple[str, ...]:
    """The filters of *value*, each re-rendered by :func:`_filter_text`."""
    return tuple(_filter_text(item, subject=subject) for item in _str_items(value, subject=subject, what="filters="))


@dataclass(frozen=True, slots=True, init=False, repr=False)
class Analyzer:
    """A full-text analyzer: SurrealDB's ``DEFINE ANALYZER`` (v0.35.0), with the full SurrealDB-ORM's
    ``DefineAnalyzer`` arguments.

    ::

        ENGLISH = Analyzer("english", ["class", "blank"], ["lowercase", "ascii", "snowball(english)"])
        ENGLISH.ddl()
        # DEFINE ANALYZER OVERWRITE `english` TOKENIZERS class,blank FILTERS lowercase,ascii,snowball(english);

    - ``name``: the analyzer's name, an identifier (letters, digits, underscores; not starting with a digit).
    - ``tokenizers``: a ``list`` or ``tuple`` of SurrealDB's tokenizers — ``blank``, ``camel``, ``class``,
      ``punct`` — in any case; kept lower-cased, in order.
    - ``filters``: a ``list`` or ``tuple`` of SurrealDB's filters — ``ascii``, ``lowercase``, ``uppercase``,
      ``ngram(min,max)`` and ``edgengram(min,max)`` (integers, ``1 <= min <= max <= 65535``),
      ``snowball(language)`` (arabic, danish, dutch, english, finnish, french, german, greek, hungarian, italian,
      norwegian, portuguese, romanian, russian, spanish, swedish, tamil, turkish) and ``mapper('path')`` (one
      quoted path, which the server reads when the analyzer is defined) — in any case with any spaces; each kept as
      the ORM re-renders it (``"ngram(1,3)"``, ``"snowball(english)"``, ``"mapper('a.txt')"``).
    - ``function``: a stored function the analyzer applies first, as ``call_function()`` takes it (``"low"`` or
      ``"fn::low"``); kept as ``"fn::…"``.
    - ``comment``: SurrealDB's ``COMMENT``, any text without a NUL.

    An analyzer belongs to the database, not to a table: one definition per name. Declare it once (a module
    constant) and reference that object from every index that uses it. Frozen, hashable and equal by its normalised
    attributes, so two spellings of one definition are equal. Every refusal is a ``TypeError`` or a ``ValueError``
    raised here, before any request.
    """

    name: str
    tokenizers: tuple[str, ...]
    filters: tuple[str, ...]
    function: str | None
    comment: str | None

    def __init__(
        self,
        name: str,
        tokenizers: Sequence[str] = (),
        filters: Sequence[str] = (),
        *,
        function: str | None = None,
        comment: str | None = None,
    ) -> None:
        _identifier_name(name, what="Analyzer")
        subject = f"Analyzer {name!r}"
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "tokenizers", _tokenizers(tokenizers, subject=subject))
        object.__setattr__(self, "filters", _filters(filters, subject=subject))
        object.__setattr__(self, "function", None if function is None else normalize_function_name(function))
        object.__setattr__(self, "comment", _comment_text(comment, subject=subject))

    def __repr__(self) -> str:
        extra = "".join(
            f", {key}={value!r}" for key, value in (("function", self.function), ("comment", self.comment)) if value is not None
        )
        return f"Analyzer({self.name!r}, {self.tokenizers!r}, {self.filters!r}{extra})"

    def ddl(self, *, overwrite: bool = True) -> str:
        """The ``DEFINE ANALYZER`` statement of this analyzer — pure: no connection, the same text every time.

        ::

            Analyzer("fts_fn", ["blank"], ["ascii"], function="fts_low").ddl()
            # DEFINE ANALYZER OVERWRITE `fts_fn` FUNCTION fn::fts_low TOKENIZERS blank FILTERS ascii;

        ``overwrite=True`` redefines an existing analyzer (``OVERWRITE``), ``False`` keeps it (``IF NOT EXISTS``);
        ``TypeError`` when it is not a ``bool``. The clauses, each when set: ``FUNCTION``, ``TOKENIZERS``,
        ``FILTERS``, ``COMMENT``.
        """
        return analyzer_statement(self, overwrite)


# --- the index ------------------------------------------------------------------------------------------------------


def _index_fields(value: object, *, subject: str) -> tuple[str, ...]:
    """The fields of an index: one ``str``, or a non-empty ``list``/``tuple`` of distinct field names or dotted
    paths, each matched in full."""
    if isinstance(value, str):
        items: tuple[object, ...] = (value,)
    elif isinstance(value, list | tuple):
        items = tuple(value)
    else:
        raise TypeError(f"{subject}: fields= must be a str or a list or tuple of str, got {type(value).__name__}.")
    fields: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise TypeError(f"{subject}: fields= holds {type(item).__name__} {item!r}, not a str.")
        fields.append(item)
    if not fields:
        raise ValueError(f"{subject}: fields= names no field.")
    for position, field in enumerate(fields):
        if VALID_FIELD_PATTERN.fullmatch(field) is None:
            raise ValueError(f"{subject}: field {field!r} is not a field name or a dotted path.")
        if field in fields[:position]:
            raise ValueError(f"{subject}: field {field!r} is named twice.")
    return tuple(fields)


def _index_flag(value: object, *, subject: str, what: str) -> bool:
    """*value*, checked as the ``bool`` argument *what* of an index (``"unique="``, ``"highlights="``)."""
    if not isinstance(value, bool):
        raise TypeError(f"{subject}: {what} must be a bool, got {type(value).__name__}.")
    return value


def _search_analyzer(value: object, *, subject: str) -> str | Analyzer | None:
    """The analyzer of a full-text index: ``None``, an ``Analyzer``, or the name of one (an identifier)."""
    if value is None or isinstance(value, Analyzer):
        return value
    if isinstance(value, str):
        return _identifier_name(value, what="Analyzer")
    raise TypeError(f"{subject}: search_analyzer= must be an Analyzer, an analyzer name or None, got {type(value).__name__}.")


def _bm25(value: object, *, subject: str) -> tuple[float, float] | Literal[True] | None:
    """BM25's parameters of a full-text index: ``None`` for ``None`` and ``False`` (no clause), ``True`` (``BM25``,
    SurrealDB's defaults), or a ``(k1, b)`` ``tuple`` of two real numbers (not ``bool``) kept as ``float``s, finite,
    ``k1 >= 0`` and ``0 <= b <= 1``."""
    if value is None or value is False:
        return None
    if value is True:
        return True
    if (
        not isinstance(value, tuple)
        or len(value) != 2
        or not all(isinstance(item, Real) and not isinstance(item, bool) for item in value)
    ):
        raise TypeError(f"{subject}: bm25= must be True, False, None or a (k1, b) pair of numbers, got {type(value).__name__}.")
    try:
        k1, b = float(value[0]), float(value[1])
    except OverflowError:
        k1 = b = math.inf
    if not (math.isfinite(k1) and math.isfinite(b) and k1 >= 0 and 0 <= b <= 1):
        raise ValueError(f"{subject}: bm25= needs finite k1 >= 0 and 0 <= b <= 1, got {value!r}.")
    # ``+ 0.0`` writes -0.0 as 0.0.
    return (k1 + 0.0, b + 0.0)


@dataclass(frozen=True, slots=True, init=False, repr=False)
class Index:
    """An index of a model's table: SurrealDB's ``DEFINE INDEX`` (v0.35.0), with the full SurrealDB-ORM's
    ``CreateIndex`` arguments.

    ::

        ENGLISH = Analyzer("english", ["class", "blank"], ["lowercase", "ascii", "snowball(english)"])

        Index("post_slug", "slug", unique=True)
        Index("post_title", "title", search_analyzer=ENGLISH, bm25=True, highlights=True)
        Index("post_title", "title", search_analyzer=ENGLISH, bm25=True).ddl("post")
        # DEFINE INDEX OVERWRITE `post_title` ON `post` FIELDS `title` FULLTEXT ANALYZER `english` BM25;

    - ``name``: the index's name, an identifier (letters, digits, underscores; not starting with a digit).
    - ``fields``: one field (a ``str``) or a non-empty ``list``/``tuple`` of distinct ones — Python field names or
      dotted paths (``"meta.headline"``) when declared on a model, which translates an aliased field to its column;
      columns for ``ddl(table)``. Kept as a ``tuple``.
    - ``unique``: a ``UNIQUE`` index.
    - ``search_analyzer``: makes it a full-text index — an ``Analyzer`` (defined with the index), or the name of an
      analyzer the database defines. A full-text index covers one field and is never ``UNIQUE``.
    - ``bm25`` (full-text only): ``None`` or ``False`` writes no clause (SurrealDB applies ``BM25(1.2,0.75)``),
      ``True`` writes ``BM25`` (the same defaults), a ``(k1, b)`` pair of finite numbers (``k1 >= 0``,
      ``0 <= b <= 1``) writes ``BM25(k1,b)``. ``False`` is kept as ``None`` and a pair as two ``float``s.
    - ``highlights`` (full-text only): writes ``HIGHLIGHTS``, which ``SearchHighlight`` needs.
    - ``comment``: SurrealDB's ``COMMENT``, any text without a NUL.

    Frozen, hashable and equal by its seven attributes. Every refusal is a ``TypeError`` or a ``ValueError`` raised
    here, before any request.
    """

    name: str
    fields: tuple[str, ...]
    unique: bool
    search_analyzer: str | Analyzer | None
    bm25: tuple[float, float] | Literal[True] | None
    highlights: bool
    comment: str | None

    def __init__(
        self,
        name: str,
        fields: str | Sequence[str],
        *,
        unique: bool = False,
        search_analyzer: str | Analyzer | None = None,
        bm25: tuple[float, float] | bool | None = None,
        highlights: bool = False,
        comment: str | None = None,
    ) -> None:
        _identifier_name(name, what="Index")
        subject = f"Index {name!r}"
        columns = _index_fields(fields, subject=subject)
        is_unique = _index_flag(unique, subject=subject, what="unique=")
        analyzer = _search_analyzer(search_analyzer, subject=subject)
        weights = _bm25(bm25, subject=subject)
        has_highlights = _index_flag(highlights, subject=subject, what="highlights=")
        text = _comment_text(comment, subject=subject)
        if analyzer is None:
            if weights is not None or has_highlights:
                raise ValueError(f"{subject}: bm25= and highlights= need search_analyzer= (a full-text index).")
        else:
            if len(columns) > 1:
                listed = ", ".join(repr(column) for column in columns)
                raise ValueError(
                    f"{subject}: a full-text index covers one field (SurrealDB 3.x refuses several); got "
                    f"{len(columns)}: {listed}. Define one index per field."
                )
            if is_unique:
                raise ValueError(f"{subject}: a full-text index cannot be UNIQUE (SurrealDB drops UNIQUE from it).")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "fields", columns)
        object.__setattr__(self, "unique", is_unique)
        object.__setattr__(self, "search_analyzer", analyzer)
        object.__setattr__(self, "bm25", weights)
        object.__setattr__(self, "highlights", has_highlights)
        object.__setattr__(self, "comment", text)

    def __repr__(self) -> str:
        options = (
            ("unique", self.unique or None),
            ("search_analyzer", self.search_analyzer),
            ("bm25", self.bm25),
            ("highlights", self.highlights or None),
            ("comment", self.comment),
        )
        extra = "".join(f", {key}={value!r}" for key, value in options if value is not None)
        return f"Index({self.name!r}, {self.fields!r}{extra})"

    @property
    def analyzer_name(self) -> str | None:
        """The name of the index's analyzer, given as a ``str`` or an ``Analyzer``; ``None`` for a plain index."""
        if isinstance(self.search_analyzer, Analyzer):
            return self.search_analyzer.name
        return self.search_analyzer

    def ddl(self, table: str, *, overwrite: bool = True, line: Literal[2, 3] = 3) -> str:
        """The ``DEFINE INDEX`` statement of this index on *table* — pure: no connection, the same text every time.

        ::

            Index("fts_ab", ["a", "b.c"]).ddl("fts_t")
            # DEFINE INDEX OVERWRITE `fts_ab` ON `fts_t` FIELDS `a`, `b`.`c`;

        ``fields`` are written as columns, each dotted segment in backticks. ``overwrite=True`` redefines an existing
        index (``OVERWRITE``), ``False`` keeps it (``IF NOT EXISTS``). ``line`` is the SurrealDB major line the
        statement is for: a full-text index is written ``FULLTEXT`` for 3 and ``SEARCH`` for 2 (and for the embedded
        engine of the SDK's 2.x range). *table* is an identifier; ``TypeError``/``ValueError`` otherwise, when
        *overwrite* is not a ``bool`` and when *line* is not 2 or 3.
        """
        table = _identifier_name(table, what="Table")
        _overwrite_clause(overwrite)
        keyword = fulltext_keyword(checked_line(line))
        return index_statement(self, table, self.fields, overwrite=overwrite, keyword=keyword)


# --- the renderers --------------------------------------------------------------------------------------------------


def checked_line(value: object) -> Literal[2, 3]:
    """*value*, checked as the ``line=`` argument of a renderer: the SurrealDB major line, 2 or 3.

    ``TypeError`` for a value that is not an ``int`` (a ``bool`` included), ``ValueError`` for another ``int``.
    """
    text = f"line= must be 2 or 3 (the SurrealDB major line the statements are for), got {value!r}."
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(text)
    if value == 2:
        return 2
    if value == 3:
        return 3
    raise ValueError(text)


def fulltext_keyword(line: int) -> Literal["FULLTEXT", "SEARCH"]:
    """The keyword of a full-text index on the SurrealDB major *line*: ``FULLTEXT`` from 3 on, ``SEARCH`` before
    (which 3.x refuses, as 2.x refuses ``FULLTEXT``)."""
    return "FULLTEXT" if line >= 3 else "SEARCH"


def _analyzer_body(analyzer: Analyzer) -> str:
    """The name and clauses of *analyzer*'s definition: ```<name>`[ FUNCTION …][ TOKENIZERS …][ FILTERS …][ COMMENT
    '…']``."""
    parts = [_identifier(analyzer.name)]
    if analyzer.function is not None:
        parts.append(f"FUNCTION {analyzer.function}")
    if analyzer.tokenizers:
        parts.append(f"TOKENIZERS {','.join(analyzer.tokenizers)}")
    if analyzer.filters:
        parts.append(f"FILTERS {','.join(analyzer.filters)}")
    if analyzer.comment is not None:
        parts.append(f"COMMENT {_quoted(analyzer.comment)}")
    return " ".join(parts)


def analyzer_definition(analyzer: Analyzer) -> str:
    """*analyzer*'s definition as ``INFO FOR DB`` renders one — ``DEFINE ANALYZER `<name>` …`` with no ``OVERWRITE``
    or ``IF NOT EXISTS`` clause and no final ``;`` —, the form ``parse_define_analyzer()`` reads. Pure."""
    return f"DEFINE ANALYZER {_analyzer_body(analyzer)}"


def analyzer_statement(analyzer: Analyzer, overwrite: bool) -> str:
    """*analyzer*'s ``DEFINE ANALYZER {OVERWRITE | IF NOT EXISTS} `<name>` …;`` statement: the body of
    :func:`analyzer_definition` with the clause *overwrite* selects and a final ``;``. Pure."""
    return f"DEFINE ANALYZER {_overwrite_clause(overwrite)} {_analyzer_body(analyzer)};"


def _column_text(column: str) -> str:
    """A column or dotted path, each segment in backticks (``meta.headline`` → ```meta`.`headline```)."""
    return ".".join(_identifier(segment) for segment in column.split("."))


def index_statement(index: Index, table: str, columns: Sequence[str], *, overwrite: bool, keyword: str) -> str:
    """*index*'s ``DEFINE INDEX`` statement on *table* over *columns* — its fields as the table's columns — with the
    full-text *keyword* (``FULLTEXT`` or ``SEARCH``, :func:`fulltext_keyword`). Pure.

    ``DEFINE INDEX {OVERWRITE | IF NOT EXISTS} `<name>` ON `<table>` FIELDS `<a>`.`<b>`[, …][ UNIQUE][ <keyword>
    ANALYZER `<analyzer>`[ BM25[(k1,b)]][ HIGHLIGHTS]][ COMMENT '…'];``
    """
    fields = ", ".join(_column_text(column) for column in columns)
    parts = [f"DEFINE INDEX {_overwrite_clause(overwrite)} {_identifier(index.name)} ON {_identifier(table)} FIELDS {fields}"]
    if index.unique:
        parts.append("UNIQUE")
    analyzer = index.analyzer_name
    if analyzer is not None:
        parts.append(f"{keyword} ANALYZER {_identifier(analyzer)}")
        if index.bm25 is True:
            parts.append("BM25")
        elif index.bm25 is not None:
            k1, b = index.bm25
            parts.append(f"BM25({k1!r},{b!r})")
        if index.highlights:
            parts.append("HIGHLIGHTS")
    if index.comment is not None:
        parts.append(f"COMMENT {_quoted(index.comment)}")
    return " ".join(parts) + ";"


def rebuild_index_statement(name: str, table: str) -> str:
    """``REBUILD INDEX `<name>` ON `<table>`;`` — the names written in backticks, escaped, as the database stores
    them (an index the rebuild scan finds may have been defined by hand under any name). Pure."""
    return f"REBUILD INDEX {_identifier(name)} ON {_identifier(table)};"


def remove_index_statement(name: str, table: str, missing_ok: bool) -> str:
    """``REMOVE INDEX [IF EXISTS] `<name>` ON `<table>`;`` — ``IF EXISTS`` with *missing_ok*. Pure.

    The names are checked as ``Index`` and ``Index.ddl()`` check them, *missing_ok* must be a ``bool``.
    """
    name = _identifier_name(name, what="Index")
    table = _identifier_name(table, what="Table")
    return f"REMOVE INDEX{_missing_clause(missing_ok)} {_identifier(name)} ON {_identifier(table)};"


def remove_analyzer_statement(name: str, missing_ok: bool) -> str:
    """``REMOVE ANALYZER [IF EXISTS] `<name>`;`` — ``IF EXISTS`` with *missing_ok*. Pure.

    The name is checked as ``Analyzer`` checks it, *missing_ok* must be a ``bool``.
    """
    name = _identifier_name(name, what="Analyzer")
    return f"REMOVE ANALYZER{_missing_clause(missing_ok)} {_identifier(name)};"


# --- the model's indexes --------------------------------------------------------------------------------------------


def check_indexes_config(model: type, value: object, *, view: bool, column: Callable[[str], str]) -> None:
    """The ``indexes`` key of a model's ``SurrealConfigDict``, checked when the class is defined (v0.35.0).

    ``None`` (no indexes) is not passed here. The first fault raises, in this order: *value* is not a ``list`` or a
    ``tuple``; an item is not an ``Index``; two indexes share a name (the second definition would replace the first);
    the model is a materialized view (*view*) and declares an index (SurrealDB 3.x does not update an index on a view as
    the view changes, and ``define_view()`` drops them); two full-text indexes cover one column, each field translated
    by *column* — the model's ``to_db_field`` — so an aliased field counts under its column (SurrealDB would use either
    for ``search()``, with different scores); two different ``Analyzer`` objects share a name (an analyzer belongs to
    the database: one definition per name). Each ``Index`` was checked when it was built. The messages name *model*.

    :raises TypeError: *value* is not a ``list`` or a ``tuple``, or holds something other than an ``Index``.
    :raises ValueError: two indexes share a name; a view declares an index; two full-text indexes cover one column;
        two different analyzers share a name.
    """
    owner = model.__name__
    key = f"{owner}: model_config['indexes']"
    if not isinstance(value, list | tuple):
        raise TypeError(f"{key} must be a list or tuple of Index, got {type(value).__name__}.")
    indexes: list[Index] = []
    for position, item in enumerate(value):
        if not isinstance(item, Index):
            raise TypeError(f"{key}[{position}] is not an Index, got {type(item).__name__}.")
        indexes.append(item)
    names: set[str] = set()
    for index in indexes:
        if index.name in names:
            raise ValueError(f"{owner}: two indexes are named {index.name!r} in model_config['indexes'].")
        names.add(index.name)
    if view and indexes:
        raise ValueError(
            f"{owner}: a materialized view (view_query) declares no indexes: SurrealDB 3.x does not update an index on a "
            "view as the view changes, and define_view() drops them."
        )
    covered: dict[str, str] = {}
    for index in indexes:
        if index.search_analyzer is None:
            continue
        # A full-text index covers one field (Index checked it).
        target = column(index.fields[0])
        if target in covered:
            raise ValueError(
                f"{owner}: two full-text indexes cover column {target!r} ({covered[target]!r}, {index.name!r}); "
                "SurrealDB would use either for search(), with different scores. Keep one."
            )
        covered[target] = index.name
    analyzers: dict[str, Analyzer] = {}
    for index in indexes:
        analyzer = index.search_analyzer
        if isinstance(analyzer, Analyzer) and analyzers.setdefault(analyzer.name, analyzer) != analyzer:
            raise ValueError(
                f"{key} holds two definitions of analyzer {analyzer.name!r}; declare one Analyzer object and reference "
                "it from every index."
            )


def declared_analyzers(indexes: Iterable[Index]) -> tuple[Analyzer, ...]:
    """The analyzers *indexes* hold as ``Analyzer`` objects, each name once, in declaration order.

    An analyzer named by a ``str`` is the database's: it is not listed. A model's indexes hold one definition per
    analyzer name (:func:`check_indexes_config`), so the first object of a name stands for every other.
    """
    found: dict[str, Analyzer] = {}
    for index in indexes:
        if isinstance(index.search_analyzer, Analyzer):
            found.setdefault(index.search_analyzer.name, index.search_analyzer)
    return tuple(found.values())


def model_index_statements(
    indexes: Sequence[Index], table: str, *, column: Callable[[str], str], overwrite: bool, line: int
) -> list[str]:
    """The statements of a model's *indexes* on *table* — what ``index_ddl()`` returns. Pure.

    The ``DEFINE ANALYZER`` of each analyzer of :func:`declared_analyzers`, then one ``DEFINE INDEX`` per index, in
    declaration order, each field written as the column *column* (the model's ``to_db_field``) gives it, with the
    full-text keyword of *line*. *overwrite* and *line* are checked first, even when *indexes* is empty.

    :raises TypeError: *overwrite* is not a ``bool``; *line* is not an ``int`` (a ``bool`` included).
    :raises ValueError: *line* is an ``int`` other than 2 or 3.
    """
    _overwrite_clause(overwrite)
    keyword = fulltext_keyword(checked_line(line))
    statements = [analyzer_statement(analyzer, overwrite) for analyzer in declared_analyzers(indexes)]
    statements.extend(
        index_statement(index, table, [column(field) for field in index.fields], overwrite=overwrite, keyword=keyword)
        for index in indexes
    )
    return statements


# --- the comparison with the stored definitions ---------------------------------------------------------------------

#: SurrealDB's BM25 parameters ``(k1, b)`` for ``BM25`` and for a full-text index without the clause.
DEFAULT_BM25 = (1.2, 0.75)

#: The relative tolerance of a BM25 parameter read back (``1e-05`` is rendered ``0.00001``).
_BM25_TOLERANCE = 1e-6


def _filter_key(item: str) -> str:
    """A filter of a parsed analyzer as :func:`analyzer_matches` compares it.

    A ``mapper(…)`` filter by its path, case-folded, without one pair of surrounding quotes — a quoted path decoded
    (``unquote()``), since the embedded engine renders the path bare (``MAPPER(/tmp/Dict/map.txt)``), which
    ``parse_define_analyzer()`` reads lower-cased, while the ORM writes it quoted. Any other filter as the parser
    normalised it (``snowball(english)``, ``ngram(1,3)``).
    """
    if not (item.startswith("mapper(") and item.endswith(")")):
        return item
    path = item[len("mapper(") : -1]
    if len(path) >= 2 and path[0] == path[-1] and path[0] in "'\"":
        try:
            path = unquote(path)
        except DefineParseError:
            path = path[1:-1]
    return f"mapper({path.casefold()})"


def analyzer_matches(analyzer: Analyzer, state: AnalyzerState | None) -> bool:
    """Whether the stored analyzer *state* (``INFO FOR DB``'s, read by ``parse_define_analyzer()``) is *analyzer*'s
    definition — pure.

    ``False`` when nothing is stored (``None``: missing or unreadable). Otherwise *analyzer* is read back by the same
    parser from :func:`analyzer_definition` — never from ``analyzer.ddl()``, whose ``OVERWRITE`` clause and final
    ``;`` the parser does not read — and compared attribute by attribute: tokenizers, function and comment equal, the
    filters equal one by one in order (a ``mapper()`` path without its quotes and case, :func:`_filter_key`). Both
    lines' renderings compare alike (3.x's ``LOWERCASE, ASCII``, 2.x's ``LOWERCASE,ASCII``).
    """
    if state is None:
        return False
    mine = parse_define_analyzer(analyzer_definition(analyzer))
    return (
        tuple(mine.tokenizers) == tuple(state.tokenizers)
        and mine.function == state.function
        and mine.comment == state.comment
        and [_filter_key(item) for item in mine.filters] == [_filter_key(item) for item in state.filters]
    )


def _bm25_pair(value: object) -> tuple[float, float]:
    """BM25's ``(k1, b)`` of a full-text index: a pair as given, :data:`DEFAULT_BM25` for ``True`` (``BM25``) and for
    ``None`` (no clause)."""
    if isinstance(value, tuple) and len(value) == 2:
        return float(value[0]), float(value[1])
    return DEFAULT_BM25


def index_matches(index: Index, columns: Sequence[str], state: IndexState | None) -> bool:
    """Whether the stored index *state* (``INFO FOR TABLE``'s, read by ``parse_define_index()``) is *index*'s
    definition over *columns* — its fields as the table's columns (``index_ddl()``'s translation) — pure.

    ``False`` when nothing is stored (``None``: missing or unreadable) and for a vector (``HNSW``, ``MTREE``) or
    ``COUNT`` index. Otherwise compared attribute by attribute: the fields in order, ``UNIQUE``, the analyzer, for a
    full-text index BM25's ``k1`` and ``b`` (``None`` and ``True`` read as ``BM25(1.2,0.75)``, which every line
    renders, each number within a relative ``1e-6``: ``1e-05`` reads back ``0.00001``) and ``HIGHLIGHTS``, and the
    comment. 2.x's tuning options are not compared: the ORM never writes them.
    """
    if state is None or state.hnsw or state.mtree or state.count:
        return False
    if tuple(columns) != tuple(state.fields) or index.unique is not state.unique:
        return False
    if index.analyzer_name != state.search_analyzer or index.highlights is not state.highlights:
        return False
    if index.comment != state.comment:
        return False
    if index.analyzer_name is None:
        return True
    declared, stored = _bm25_pair(index.bm25), _bm25_pair(state.bm25)
    return all(math.isclose(mine, theirs, rel_tol=_BM25_TOLERANCE) for mine, theirs in zip(declared, stored, strict=True))


# --- the reads ------------------------------------------------------------------------------------------------------

#: The read of the database's analyzers and tables.
_INFO_FOR_DB = "INFO FOR DB;"


def _read_refused(statement: str, error: BaseException, *, what: str, kind: str, subject: str) -> SurrealDbError:
    """The refusal of a read (*statement*) a DDL of *what* needs: the text of a refused DDL, ``Can't apply {what}
    {kind}: {subject} -> <the server's message>`` — ``SurrealDbAuthenticationError`` when the session may not read
    (a record user), ``SurrealDbError`` otherwise, as ``schema._guard_read_refused()`` does."""
    refusal = _ddl_refusal_class(statement, error)
    return refusal(f"Can't apply {what} {kind}: {subject} -> {error}")


def _parsed(definition: object, parse: Callable[[str], Any]) -> Any:
    """*definition* read by *parse*, or ``None`` when it is not a ``str`` or the parser cannot read it."""
    if not isinstance(definition, str):
        return None
    try:
        return parse(definition)
    except DefineParseError:
        return None


async def read_database_info(
    client: Any, *, what: str, kind: str, subject: str
) -> tuple[dict[str, AnalyzerState | None], tuple[str, ...]]:
    """The analyzers and the table names of *client*'s database, read once with ``INFO FOR DB;``.

    The analyzers by name (``INFO FOR DB``'s key), each read by ``parse_define_analyzer()`` — ``None`` for a
    definition the parser cannot read, or that is not a ``str``, so the caller redefines it rather than trusting it —
    and the table names, sorted. ``({}, ())`` when the answer holds neither. The read is on *client* outside any
    transaction (committed state).

    :param what: what the read is for, for the message of a refusal (``"index"``, ``"analyzer"``).
    :param kind: ``"definition"``, ``"removal"`` or ``"rebuild"``, likewise.
    :param subject: what is applied, likewise, already quoted (``"'english'"``, ``"table 'post'"``).
    :raises SurrealDbAuthenticationError: the session may not read the database's definitions (a record user).
    :raises SurrealDbError: any other refusal of the ``INFO``.
    """
    try:
        info = await client.query(_INFO_FOR_DB, {})
    except (ServerError, SurrealDbError, RuntimeError) as error:
        raise _read_refused(_INFO_FOR_DB, error, what=what, kind=kind, subject=subject) from error
    analyzers = info.get("analyzers") if isinstance(info, dict) else None
    tables = info.get("tables") if isinstance(info, dict) else None
    stored: dict[str, AnalyzerState | None] = {}
    if isinstance(analyzers, dict):
        stored = {name: _parsed(definition, parse_define_analyzer) for name, definition in analyzers.items()}
    names = tuple(sorted(name for name in tables if isinstance(name, str))) if isinstance(tables, dict) else ()
    return stored, names


async def read_table_indexes(client: Any, table: str, *, what: str, kind: str) -> dict[str, IndexState | None]:
    """The indexes of *table*, read once with ``INFO FOR TABLE `<table>`;``, by name.

    Each definition is read by ``parse_define_index()`` under its ``INFO FOR TABLE`` key — ``None`` for one the
    parser cannot read, or that is not a ``str``, so the caller redefines it rather than trusting it. ``{}`` for a
    table without indexes, and for a missing one (SurrealDB answers empty maps). *table* is checked as an identifier
    and written in backticks; the read is on *client* outside any transaction (committed state).

    :param what: what the read is for, for the message of a refusal (``"index"``, ``"analyzer"``), which names
        ``table '<table>'``.
    :param kind: ``"definition"``, ``"removal"`` or ``"rebuild"``, likewise.
    :raises TypeError: *table* is not a ``str``.
    :raises ValueError: *table* is not an identifier.
    :raises SurrealDbAuthenticationError: the session may not read the table's definitions (a record user).
    :raises SurrealDbError: any other refusal of the ``INFO``.
    """
    return await _table_indexes(client, _identifier_name(table, what="Table"), what=what, kind=kind)


async def _table_indexes(client: Any, table: str, *, what: str, kind: str) -> dict[str, IndexState | None]:
    """:func:`read_table_indexes` without the identifier check of *table* — a name ``INFO FOR DB`` gave, which
    SurrealDB may store in backticks (``fts-t``) —, written in backticks, escaped."""
    statement = f"INFO FOR TABLE {_identifier(table)};"
    try:
        info = await client.query(statement, {})
    except (ServerError, SurrealDbError, RuntimeError) as error:
        raise _read_refused(statement, error, what=what, kind=kind, subject=f"table {table!r}") from error
    indexes = info.get("indexes") if isinstance(info, dict) else None
    if not isinstance(indexes, dict):
        return {}
    return {name: _parsed(definition, parse_define_index) for name, definition in indexes.items()}


#: An index that uses an analyzer: ``(table, index, analyzer)``.
IndexUse = tuple[str, str, str]


def stored_indexes_using(
    table: str, stored: Mapping[str, IndexState | None], analyzers: Collection[str], *, skip: Collection[str] = ()
) -> list[IndexUse]:
    """The indexes of *table* (*stored*: ``INFO FOR TABLE``'s, by name) whose analyzer is among *analyzers*, as
    ``(table, index, analyzer)``, sorted by index — pure.

    An index named in *skip* is left out, and so is one the parser could not read (``None``): its analyzer is unknown.
    """
    return sorted(
        (table, name, state.search_analyzer)
        for name, state in stored.items()
        if name not in skip and state is not None and state.search_analyzer is not None and state.search_analyzer in analyzers
    )


async def indexes_using(
    client: Any, tables: Iterable[str], analyzers: Collection[str], *, what: str, kind: str
) -> list[IndexUse]:
    """The indexes of *tables* whose analyzer is among *analyzers*, as ``(table, index, analyzer)``, sorted by table
    then index — one ``INFO FOR TABLE`` per table, in the order of *tables*, outside any transaction (committed state).

    *tables* are names ``INFO FOR DB`` gave (:func:`read_database_info`), each written in backticks, escaped. An index
    the parser cannot read is left out (its analyzer is unknown).

    :param what: what the reads are for, for the message of a refusal (``"index"``, ``"analyzer"``).
    :param kind: ``"definition"``, ``"removal"`` or ``"rebuild"``, likewise.
    :raises SurrealDbAuthenticationError: the session may not read a table's definitions (a record user).
    :raises SurrealDbError: any other refusal of an ``INFO``.
    """
    found: list[IndexUse] = []
    for table in tables:
        found.extend(stored_indexes_using(table, await _table_indexes(client, table, what=what, kind=kind), analyzers))
    return sorted(found)


# --- the appliers' plans --------------------------------------------------------------------------------------------


def checked_overwrite(value: object) -> bool:
    """*value*, checked as the ``overwrite=`` argument of an applier: a ``bool``.

    :raises TypeError: ``overwrite= must be a bool, got <type>.``
    """
    _overwrite_clause(value)
    return value is True


def index_subject(name: str, table: str) -> str:
    """How a refusal names the index *name* of *table*: ``'<name>' on table '<table>'``."""
    return f"{name!r} on table {table!r}"


def check_named_analyzers(
    indexes: Iterable[Index], analyzers: Iterable[Analyzer], stored: Mapping[str, AnalyzerState | None], *, table: str
) -> None:
    """Refuse an index of *table* that names by ``str`` an analyzer the database lacks — before any DDL.

    An analyzer named by a ``str`` exists when the database stores it (*stored*, ``INFO FOR DB``'s, a definition the
    parser cannot read included) or when it is one of the model's own ``Analyzer`` objects (*analyzers*), which the
    applier defines first. The first such index in declaration order raises.

    :raises SurrealDbError: ``Can't apply index definition: '<index>' on table '<table>' -> the analyzer '<name>' does
        not exist; …``.
    """
    own = {analyzer.name for analyzer in analyzers}
    for index in indexes:
        name = index.search_analyzer
        if isinstance(name, str) and name not in own and name not in stored:
            raise SurrealDbError(
                f"Can't apply index definition: {index_subject(index.name, table)} -> the analyzer {name!r} does not "
                "exist; pass search_analyzer=Analyzer(...) or define it with "
                "SurrealDBConnectionManager.define_analyzer() first."
            )


def plan_analyzers(
    analyzers: Iterable[Analyzer], stored: Mapping[str, AnalyzerState | None], overwrite: bool
) -> tuple[list[Analyzer], list[Analyzer]]:
    """The analyzers to define and the analyzers to redefine, each in the order of *analyzers* — pure.

    An analyzer the database does not store (*stored*: ``INFO FOR DB``'s, by name) is to define; one it stores equal
    (:func:`analyzer_matches`) is left as it is; one it stores different, or in a definition the parser cannot read, is
    to redefine with *overwrite* (``DEFINE ANALYZER OVERWRITE``) and left as it is without.
    """
    define: list[Analyzer] = []
    redefine: list[Analyzer] = []
    for analyzer in analyzers:
        if analyzer.name not in stored:
            define.append(analyzer)
        elif overwrite and not analyzer_matches(analyzer, stored[analyzer.name]):
            redefine.append(analyzer)
    return define, redefine


def transaction_analyzer_refused(model: str, name: str) -> str:
    """The refusal of ``define_indexes(tx=…)`` when the analyzer *name* must be defined or redefined: the servers build
    an index with the analyzers committed before the transaction began."""
    return (
        f"{model}.define_indexes(tx=…) cannot define analyzer {name!r} in a transaction: SurrealDB builds an index with "
        "the analyzers committed before the transaction began. Call SurrealDBConnectionManager.define_analyzer() "
        "without tx= first, then define_indexes(tx=…)."
    )


def plan_indexes(
    indexes: Iterable[Index],
    table: str,
    stored: Mapping[str, IndexState | None],
    *,
    column: Callable[[str], str],
    overwrite: bool,
    keyword: str,
    redefined: Collection[str],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """The index definitions and the rebuilds a model's *indexes* need on *table* — pure — each as ``(index name,
    statement)``, in declaration order.

    Each field is written as the column *column* (the model's ``to_db_field``) gives it, a full-text index with
    *keyword* (:func:`fulltext_keyword`). An index *stored* (``INFO FOR TABLE``'s, by name) does not hold is defined
    (``OVERWRITE``, or ``IF NOT EXISTS`` without *overwrite*); one it holds equal (:func:`index_matches`) is left as it
    is — but rebuilt when its analyzer is among *redefined*, since SurrealDB leaves the records indexed before with
    their old tokens; one it holds different, or in a definition the parser cannot read, is redefined
    (``OVERWRITE``, which rebuilds it) with *overwrite* and left as it is without.
    """
    definitions: list[tuple[str, str]] = []
    rebuilds: list[tuple[str, str]] = []
    for index in indexes:
        columns = [column(field) for field in index.fields]
        if index.name not in stored:
            definitions.append((index.name, index_statement(index, table, columns, overwrite=overwrite, keyword=keyword)))
        elif index_matches(index, columns, stored[index.name]):
            if index.analyzer_name is not None and index.analyzer_name in redefined:
                rebuilds.append((index.name, rebuild_index_statement(index.name, table)))
        elif overwrite:
            definitions.append((index.name, index_statement(index, table, columns, overwrite=True, keyword=keyword)))
    return definitions, rebuilds


# --- the appliers' shared steps -------------------------------------------------------------------------------------


async def apply_analyzers(
    client: Any, tx: Any, define: Sequence[Analyzer], redefine: Sequence[Analyzer], overwrite: bool
) -> list[str]:
    """Apply the analyzers :func:`plan_analyzers` planned, one statement per request, and return the statements: each
    of *define* (``DEFINE ANALYZER OVERWRITE``, or ``IF NOT EXISTS`` without *overwrite*), then each of *redefine*
    (``DEFINE ANALYZER OVERWRITE``). With *tx*, each joins it. A refusal names the analyzer, never the statement:
    ``Can't apply analyzer definition: '<name>' -> <reason>``.
    """
    analyzers = [*define, *redefine]
    statements = [analyzer_statement(analyzer, overwrite) for analyzer in define]
    statements.extend(analyzer_statement(analyzer, True) for analyzer in redefine)
    return await apply_ddl_statements(
        statements,
        client=client,
        tx=tx,
        what="analyzer",
        classify=True,
        subjects=[repr(analyzer.name) for analyzer in analyzers],
    )


async def apply_rebuilds(client: Any, tx: Any, rebuilds: Sequence[IndexUse]) -> list[str]:
    """``REBUILD INDEX `<index>` ON `<table>`;`` for each of *rebuilds* (``(table, index, analyzer)``), one per request,
    in order; the statements returned. With *tx*, each joins it. A refusal names the index and its table: ``Can't apply
    index rebuild: '<index>' on table '<table>' -> <reason>``."""
    return await apply_ddl_statements(
        [rebuild_index_statement(index, table) for table, index, _ in rebuilds],
        client=client,
        tx=tx,
        what="index",
        kind="rebuild",
        classify=True,
        subjects=[index_subject(index, table) for table, index, _ in rebuilds],
    )


def warn_redefined(redefine: Iterable[Analyzer], rebuilt: Sequence[IndexUse]) -> None:
    """Log one warning per analyzer of *redefine*, naming the indexes of *rebuilt* (``(table, index, analyzer)``) that
    use it, in order: ``Analyzer 'english' redefined; 2 index(es) rebuilt: page.page_title, post.post_title``.

    An analyzer belongs to the database: two models declaring two definitions of one name redefine it in turn, and
    rebuild every index using it each time — the warning makes that visible.
    """
    for analyzer in redefine:
        names = [f"{table}.{index}" for table, index, name in rebuilt if name == analyzer.name]
        logger.warning("Analyzer %r redefined; %d index(es) rebuilt: %s", analyzer.name, len(names), ", ".join(names))


def checked_analyzer(value: object) -> Analyzer:
    """*value*, checked as the argument of ``define_analyzer()``: an ``Analyzer``.

    :raises TypeError: ``define_analyzer() takes an Analyzer, got <type>.``
    """
    if not isinstance(value, Analyzer):
        raise TypeError(f"define_analyzer() takes an Analyzer, got {type(value).__name__}.")
    return value


def analyzer_redefinition_refused(name: str, rebuilds: Sequence[IndexUse]) -> str:
    """The refusal of ``define_analyzer(tx=…)`` redefining the analyzer *name* that the indexes of *rebuilds* use: their
    rebuild must follow the committed definition."""
    indexes = ", ".join(f"{table}.{index}" for table, index, _ in rebuilds)
    return (
        f"define_analyzer(tx=…) cannot redefine analyzer {name!r} in a transaction: the indexes that use it ({indexes}) "
        "must be rebuilt once the new definition is committed. Call define_analyzer() without tx=."
    )


def analyzer_in_use_refused(name: str, index: str, table: str) -> str:
    """The refusal of ``remove_analyzer()`` for the analyzer *name* that *index* of *table* uses — SurrealDB 3.x's own
    reason, in the form of a refused removal."""
    return (
        f"Can't apply analyzer removal: {name!r} -> The analyzer {name!r} is in use by index {index!r} on table "
        f"{table!r} and cannot be removed"
    )


async def apply_analyzer(client: Any, analyzer: Analyzer, *, overwrite: bool, tx: Any) -> list[str]:
    """The body of ``SurrealDBConnectionManager.define_analyzer()``, its arguments checked: compare *analyzer* with the
    stored definition, then apply; the statements applied — or queued, with a buffered *tx* — returned, in order.

    ``INFO FOR DB`` once. Missing: ``DEFINE ANALYZER`` (``OVERWRITE``, or ``IF NOT EXISTS`` without *overwrite*). Equal
    (:func:`analyzer_matches`): nothing. Different or unreadable, with *overwrite*: ``INFO FOR TABLE`` of every table
    (:func:`indexes_using`), then ``DEFINE ANALYZER OVERWRITE`` and ``REBUILD INDEX`` of each index using it — SurrealDB
    leaves the records indexed before with their old tokens — and the warning of :func:`warn_redefined`; nothing
    without *overwrite*. With *tx*, a redefinition with indexes to rebuild is refused before any DDL (the rebuild must
    follow the committed definition); a new analyzer, or a redefinition no index uses, joins the transaction.

    :raises SurrealDbError: *tx* is given and the redefinition has indexes to rebuild; a refused read or statement.
    :raises SurrealDbAuthenticationError: the session may not read the definitions or define the analyzer.
    """
    subject = repr(analyzer.name)
    stored, tables = await read_database_info(client, what="analyzer", kind="definition", subject=subject)
    define, redefine = plan_analyzers([analyzer], stored, overwrite)
    rebuilds: list[IndexUse] = []
    if redefine:
        rebuilds = await indexes_using(client, tables, {analyzer.name}, what="analyzer", kind="definition")
        if tx is not None and rebuilds:
            raise SurrealDbError(analyzer_redefinition_refused(analyzer.name, rebuilds))
    applied = await apply_analyzers(client, tx, define, redefine, overwrite)
    applied += await apply_rebuilds(client, tx, rebuilds)
    warn_redefined(redefine, rebuilds)
    return applied


async def apply_analyzer_removal(client: Any, name: str, statement: str, *, tx: Any) -> str:
    """The body of ``SurrealDBConnectionManager.remove_analyzer()``, *statement* rendered (its arguments checked): apply
    it, unless an index uses the analyzer *name* on a line that would remove it; *statement* returned.

    ``client.version()`` once. Below 3 (SurrealDB 2.x, the embedded engine), which accept the removal of an analyzer an
    index uses and then refuse every write to that table: ``INFO FOR DB``, then ``INFO FOR TABLE`` of every table
    (:func:`indexes_using`, committed state), and the first index using it, in table then index order, refused with
    SurrealDB 3.x's own reason, before any DDL. From 3 on, the server refuses it itself, in the same words. Then the
    statement, joining *tx* when given: ``Can't apply analyzer removal: '<name>' -> <reason>`` for a refusal.

    :raises SurrealDbError: an index uses the analyzer; a refused read or statement.
    :raises SurrealDbNotFoundError: ``REMOVE ANALYZER`` without ``IF EXISTS`` of a missing analyzer (outside a buffered
        transaction).
    :raises SurrealDbAuthenticationError: the session may not read the definitions or remove the analyzer.
    """
    if line_major(await client.version()) < 3:
        _, tables = await read_database_info(client, what="analyzer", kind="removal", subject=repr(name))
        using = await indexes_using(client, tables, {name}, what="analyzer", kind="removal")
        if using:
            table, index, _ = using[0]
            raise SurrealDbError(analyzer_in_use_refused(name, index, table))
    await apply_ddl_statements(
        [statement], client=client, tx=tx, what="analyzer", kind="removal", classify=True, subjects=[repr(name)]
    )
    return statement


# --- the query side: search(), its refusals, a missing full-text index ----------------------------------------------

#: The most fields one queryset may search: one match reference each, ``@0@`` … ``@255@``.
MAX_SEARCH_FIELDS = MAX_MATCH_REFERENCE + 1

#: The words of SurrealDB's error for a match on a field without a full-text index, on every engine.
_MISSING_INDEX_MARKER = "no suitable index supporting the expression"

#: The column that error names: ``… the expression: <column> @<N>@ <value>`` — bare on every engine measured.
_MISSING_INDEX_COLUMN = re.compile(r"expression: (\S+) @\d*@")

#: The pairs of delimiters a server could put around one segment of that column.
_SEGMENT_QUOTES = (("`", "`"), ("⟨", "⟩"))


def checked_search_field(field: str) -> None:
    """*field*, a keyword of ``search()``, checked as ``validate_field_name()`` checks a field name, matched in full.

    :raises ValueError: ``validate_field_name()``'s text, for a field that is not a field name or a dotted path —
        a trailing newline included, which its pattern's ``$`` accepts.
    """
    validate_field_name(field, "search field")
    if VALID_FIELD_PATTERN.fullmatch(field) is None:
        raise ValueError(
            f"Invalid search field name '{field}': must contain only alphanumeric characters, underscores, and dots "
            "(for nested fields), and start with a letter or underscore"
        )


def text_refused(field: str, text: object) -> str:
    """The refusal of a ``search()`` text that is not a ``str``: *text* given for *field*."""
    return f"search() takes a str to match in {field!r}, got {type(text).__name__}."


def field_refused(field: str, *, endpoint: bool) -> str:
    """The refusal of ``search()`` on *field*, the record id — or an edge endpoint when *endpoint*."""
    what = "an edge endpoint" if endpoint else "the record id"
    return f"search() matches text: {field!r} is {what}, not a text field."


def too_many_fields() -> str:
    """The refusal of a queryset searching more fields than SurrealDB has match references."""
    return (
        f"search() takes at most {MAX_SEARCH_FIELDS} fields per queryset (SurrealDB's match references are 0 to "
        f"{MAX_MATCH_REFERENCE})."
    )


def time_travel_refused() -> str:
    """The refusal of ``search()`` with ``as_of()``: the index holds the records' current text."""
    return (
        "search() cannot be combined with as_of(): a full-text match reads the index, which holds the records' current "
        "text, so a time-travel read would select records by their text now. Run the search without as_of()."
    )


def live_refused() -> str:
    """The refusal of ``search()`` in a live query's own condition, naming the subquery form that notifies."""
    return (
        "A live query cannot honour search(): SurrealDB accepts a LIVE SELECT whose condition holds a full-text match "
        "but never notifies it. Search in a subquery instead — filter(id__in=Subquery(Model.objects().search(...))) — "
        "which notifies the records created or updated to match, not their deletion."
    )


def search_index_refused(fields: Sequence[str]) -> str:
    """The refusal of a search on a field without a full-text index. *fields*: the field whose column the server named
    — or, when that column cannot be read, every field the statement searches; one is named alone, several as "one of
    the fields"."""
    advice = (
        "SurrealDB matches only a field with a FULLTEXT (3.x) or SEARCH (2.x) index: declare "
        "Index(..., search_analyzer=...) on the model and apply define_indexes()."
    )
    if len(fields) == 1:
        return f"search(): no full-text index on field {fields[0]!r}. {advice}"
    return f"search(): no full-text index on one of the fields {', '.join(repr(field) for field in fields)}. {advice}"


def is_missing_index_error(error: BaseException) -> bool:
    """Whether *error* is SurrealDB's refusal of a match on a field without a full-text index (a ``ServerError``)."""
    return isinstance(error, ServerError) and _MISSING_INDEX_MARKER in str(error)


def _unquoted_segment(segment: str) -> str:
    """*segment* without one pair of surrounding backticks or ``⟨⟩``."""
    for opening, closing in _SEGMENT_QUOTES:
        if len(segment) >= 2 and segment.startswith(opening) and segment.endswith(closing):
            return segment[1:-1]
    return segment


def missing_index_column(error: BaseException) -> str | None:
    """The column SurrealDB's missing-index *error* names (``… the expression: meta.headline @1@ …`` →
    ``meta.headline``), each dotted segment without its backticks or ``⟨⟩`` should a server escape one; ``None`` when
    the expression cannot be read."""
    found = _MISSING_INDEX_COLUMN.search(str(error))
    if found is None:
        return None
    return ".".join(_unquoted_segment(segment) for segment in found[1].split("."))


def missing_index_refusal(error: BaseException, searched: Mapping[str, str]) -> SurrealDbError | None:
    """The ORM's error for SurrealDB's missing-index *error*, not raised; ``None`` for any other error.

    *searched* maps each column the statement searches to its field as given (the queryset's and its subqueries'). The
    error names the field whose column the server names, else that column; when the column cannot be read, every
    field of *searched*. Never the server's text, which on SurrealDB 3.x quotes the search text: the caller raises it
    ``from None``.
    """
    if not is_missing_index_error(error):
        return None
    column = missing_index_column(error)
    if column is not None:
        return SurrealDbError(search_index_refused([searched.get(column, column)]))
    fields = list(dict.fromkeys(searched.values()))
    if not fields:
        return None
    return SurrealDbError(search_index_refused(fields))


def live_index_probe(table: str, column: str) -> str:
    """One item of a live query's start check: an empty search of *column* of *table*, which answers ``[]`` when the
    column has a full-text index and raises SurrealDB's missing-index error when it has none.

    *table* and *column* are written as the live query writes them (the model's table, a column the queryset checked);
    the text is the constant ``''``, never a caller's.
    """
    return f"(SELECT VALUE id FROM {table} WHERE {column} @0@ '' LIMIT 1)"
