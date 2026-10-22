"""Full-text search (v0.35.0): the full SurrealDB-ORM's query API — ``search()``, ``SearchScore`` and
``SearchHighlight`` — plus ``SearchOffsets``, with analyzers and indexes declared on the model.

The search annotations (``TestSearchAnnotations``): ``SearchScore(ref=0)``, ``SearchHighlight(open_tag="<b>",
close_tag="</b>", ref=0)`` and ``SearchOffsets(ref=0)`` keep their arguments as attributes, with the full ORM's
``repr`` (``SearchScore(0)``, ``SearchHighlight('<b>', '</b>', 0)``), equal and hashed by class and attributes,
without a ``__dict__``. A reference is an ``int`` (not a ``bool``) from 0 to 255, SurrealDB's match references; a
tag is a ``str``. Each refusal is compared with its exact text and class. The five names are exported from the
package root and from ``surreal_orm_lite.search``, which imports no model, queryset or connection module.

The analyzer (``TestAnalyzer``): ``Analyzer(name, tokenizers, filters, *, function=, comment=)`` — the full ORM's
``DefineAnalyzer`` arguments — checks every argument when it is built and keeps them normalised: tokenizers
lower-cased, each filter re-rendered from what the ORM read (``NGRAM( 1 , 3 )`` → ``ngram(1,3)``,
``Snowball(French)`` → ``snowball(french)``, ``mapper('path')`` re-quoted), the function prefixed ``fn::``. Two
spellings of one definition are equal. ``ddl()`` renders ``DEFINE ANALYZER {OVERWRITE | IF NOT EXISTS} `<name>`
…;`` with no connection; ``search.analyzer_definition()`` renders the same definition without the clause and the
final ``;``, which ``parse_define_analyzer()`` reads back as declared.

The index (``TestIndex``): ``Index(name, fields, *, unique=, search_analyzer=, bm25=, highlights=, comment=)`` — the
full ORM's ``CreateIndex`` arguments — covers plain, ``UNIQUE`` and full-text indexes; a full-text one covers one
field, is never ``UNIQUE``, and alone takes ``bm25=``/``highlights=``. ``ddl(table, line=3)`` renders ``DEFINE
INDEX … FIELDS `<a>`.`<b>` …`` with the line's keyword (``FULLTEXT`` on 3.x, ``SEARCH`` on 2.x), BM25's ``k1`` and
``b`` written as Python floats, with no connection. ``search.remove_index_statement()``,
``remove_analyzer_statement()`` and ``rebuild_index_statement()`` render the removals and the rebuild.

The model's indexes (``TestIndexesConfig``, ``TestGetIndexes``, ``TestIndexDdl``): ``SurrealConfigDict(indexes=[...])``
is checked at the ``class`` statement — a ``list`` or ``tuple`` of ``Index``, distinct names, none on a materialized
view, at most one full-text index per column (an aliased field counted under its column), one ``Analyzer`` definition
per analyzer name —, the first fault raising with its exact text; a subclass inherits the key unless it sets its own.
``get_indexes()`` returns them in declaration order; ``index_ddl()`` renders, with no connection, the analyzers the
indexes hold as ``Analyzer`` objects (each name once) and then one ``DEFINE INDEX`` per index on the model's table, each
field under its column, with the line's keyword.

The comparison with what the database stores (``TestMatches``): ``search.analyzer_matches()`` holds an ``Analyzer``
equal to a stored definition as SurrealDB 3.x renders it (``LOWERCASE, ASCII``) and as 2.x and the embedded engine do
(``LOWERCASE,ASCII``), a ``mapper()`` path compared without its quotes and case (the embedded engine renders it
unquoted), and never parses the analyzer's own statement; ``search.index_matches()`` holds an ``Index`` equal to its
stored definition on both lines (2.x's tuning options aside, ``bm25=None`` and ``True`` equal to ``BM25(1.2,0.75)``,
the BM25 numbers compared as floats), and never to a vector or ``COUNT`` index.

The reads (``TestReads``, ``TestReadsE2E``): ``search.read_database_info()`` sends ``INFO FOR DB`` once and returns the
stored analyzers and the table names, ``search.read_table_indexes()`` sends ``INFO FOR TABLE`` once and returns the
table's indexes; a definition the parser cannot read is ``None`` under its name, a missing table has no index, and a
session that may not read gets ``SurrealDbAuthenticationError``.

On every engine (``TestGoldensE2E``: SurrealDB 3.x and 2.x over WebSocket and HTTP, and the embedded engine) each
golden statement is accepted with the line's keyword, what ``INFO`` returns matches its declaration, and the
introspector reads the indexes and the analyzer as declared. Pinned (README row "Full-text index"): each line refuses
the other's keyword; the embedded engine reports major version 2 and takes ``SEARCH`` — the keyword tripwire.

Applying them (``TestDefineIndexesE2E``, ``TestRemoveIndexE2E``, every engine): ``define_indexes()`` reads ``INFO FOR
DB``, the line (``client.version()``, when a full-text index is declared) and ``INFO FOR TABLE``, then applies only
what changed — on a fresh database the analyzers and the indexes ``index_ddl()`` renders with the line's keyword, on an
unchanged one nothing (``[]``, the reads alone sent), a changed or unreadable index redefined (``overwrite=False``
keeps it), the indexes of a redefined analyzer rebuilt. An analyzer an index names by ``str`` must exist in the
database or among the model's ``Analyzer`` objects, and ``define_indexes(tx=…)`` defines no analyzer: each refusal
comes before any DDL. With a transaction the index statements join it (applied at the commit when buffered, at once
when interactive, not at all when the block raises). ``remove_index()`` removes by name, ``missing_ok`` deciding
whether a missing index is an error, and drops the model's cached reads; a transaction of another connection is refused
before any request. Pinned: an analyzer and an index using it defined in one transaction, on a table holding records
(README row "Full-text index"); the table ``DEFINE INDEX`` creates, which ``define_table(overwrite=False)`` keeps and
``define_table()`` replaces (README section 36, the order of ``define_table()`` and ``define_indexes()``); an index on a
materialized view's table, which SurrealDB 3.x does not update with the records the view gains (README section 36, Not
supported: surrealdb/surrealdb#7377).

A changed analyzer (``TestAnalyzerScan``, ``TestAnalyzerChangeE2E``, every engine): ``define_indexes()`` redefines it,
then rebuilds every index of the database that uses it — the other tables' (one ``INFO FOR TABLE`` each, sorted by
table then index), then the model's table's, declared or not — and logs one warning naming them; a search tokenised the
new way then finds the records indexed before. Pinned (README section 36, Changing an analyzer): without the rebuild,
SurrealDB leaves those records with their old tokens. The connection manager's ``define_analyzer()`` and
``remove_analyzer()`` (``TestAnalyzerManagerApi``, ``TestDefineAnalyzerE2E``, ``TestRemoveAnalyzerE2E``) run on the
current connection: a new analyzer defined, an equal one left, a changed one redefined with its indexes rebuilt
(``overwrite=False`` keeps it), a redefinition with indexes to rebuild refused in a transaction before any DDL, every
cached read dropped; ``remove_analyzer()`` takes ``analyzer`` (never ``name``, which the manager reads as a connection
name), removes by name with ``missing_ok``, and refuses an analyzer an index uses with SurrealDB 3.x's own text on every
engine — the server's on 3.x, the ORM's before any DDL on 2.x and the embedded engine. Pinned (README row "Removing an
analyzer an index uses"): 2.x and the embedded engine accept that removal, then refuse the next write to the table.

``search()`` (``TestSearchCompile``, ``TestSearchRefusals``): each field compiles ``<column> @<ref>@ $_f<N>``, AND-ed
after the filters, ``Q`` and ``nearby()``, its text bound (data, whatever it holds), the references in call order
across calls, a subquery's starting again at 0 while its texts continue the statement's numbering; an aliased field
compiles its column; each refusal — a text that is not a ``str``, a field that is not a name or a dotted path, the
record id or an edge endpoint, more than 256 fields — comes before any request and leaves the queryset unchanged.
``search()`` with ``as_of()`` (either way round, and in a subquery under the outer one), in a live query or a change
feed is refused before any request. A search on a field without a full-text index (``TestMissingIndexTexts``,
``TestMissingIndexE2E``) raises ``SurrealDbError`` naming the field — the one whose column SurrealDB names, a
subquery's included —, never the server's text, which on SurrealDB 3.x quotes the search text: in a read, a count, a
write by filter, and in a transaction, whose commit then fails with that error as its cause; a write by filter in a
transaction gets the transaction's own error, cut after the expression's operator (``TestTransactionReason``). Before a
live query whose subquery searches (``TestLiveIndexCheck``, ``TestLiveIndexCheckE2E``), the ORM checks that each
searched field has a full-text index and raises ``SurrealDbError`` naming the field otherwise, before the live query
starts: without the index SurrealDB 3.x would accept the live query and never notify. On every engine
(``TestSearchE2E``) the corpus's matches, the terms of one text AND-ed, an empty text matching nothing, with
``filter()``, ``Q``, ``order_by()``, ``limit()``/``offset()``, ``select()``, ``fetch()``, ``first()`` and ``get()``.
Pinned (README rows "``search()`` on a field without a full-text index" and "``live()`` with a ``Subquery`` that
searches a field without a full-text index"): SurrealDB 3.1 answers no record, and starts the live query.

The search annotations in ``annotate()`` (``TestAnnotateCompile``): ``SearchScore``, ``SearchHighlight`` and
``SearchOffsets`` compile ``search::score(<ref>)``, ``search::highlight($_sh<2k>, $_sh<2k+1>, <ref>)`` (the tags bound)
and ``search::offsets(<ref>)``, after the ``GeoDistance`` projections, in ``annotate()`` order; an alias that would
shadow a field, its column or an attribute, a search annotation with an aggregation or ``values()``, a value of another
type, a reference ``search()`` did not give and a search annotation without ``search()`` are each refused with an
exact text, before any request — the references when the queryset runs, since ``annotate()`` may come before
``search()``. ``count()`` ignores them; ``live()``, ``watch()``, ``changes()`` and a ``Subquery`` refuse them; an alias
given again replaces the earlier value whatever its kind. On every engine (``TestAnnotateE2E``) the instances carry the
corpus's scores — equal across engines on an index built once —, highlights (a list for an array field, the text
unchanged without ``HIGHLIGHTS``), offsets and tags exactly as given, never written back; rows that do not validate are
dicts holding the aliases. Pinned (``TestSearchLinesE2E``, README rows "Two ``search()`` fields AND-ed", "BM25 score
of a term in more than half the documents" and "BM25 statistics after a rebuild, a redefinition or a deletion"): each
field of an AND scored as if searched alone, SurrealDB 3.1 also returning records that match one field only; a common
term scored ``0.0`` on 3.x and below zero on 2.x and the embedded engine; after a rebuild or a deleted record, 3.x
keeps the scores of the freshly built index while 2.x and the embedded engine count the records again.
"""

import ast
import contextlib
import dataclasses
import inspect
import logging
import math
import os
import pickle
import re
import traceback
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import pytest
from pydantic import Field
from surrealdb import InternalError, NotAllowedError, NotFoundError

import surreal_orm_lite
from surreal_orm_lite import (
    Analyzer,
    AnalyzerState,
    BaseSurrealEdge,
    BaseSurrealModel,
    Count,
    DatabaseIntrospector,
    GeoDistance,
    Index,
    IndexState,
    Q,
    QueryCache,
    SearchHighlight,
    SearchOffsets,
    SearchScore,
    Subquery,
    SurrealConfigDict,
    SurrealDbAuthenticationError,
    SurrealDBConnectionManager,
    SurrealDbError,
    SurrealDbNotFoundError,
    Transaction,
    search,
    transaction,
)
from surreal_orm_lite.define_parser import DefineParseError, parse_define_analyzer, parse_define_index
from surreal_orm_lite.functions import normalize_function_name
from surreal_orm_lite.utils import validate_field_name

FTS_ENGLISH = Analyzer("fts_english", ["class", "blank"], ["lowercase", "ascii", "snowball(english)"], comment="English text")

_EXPORTS = ["SearchScore", "SearchHighlight", "SearchOffsets", "Analyzer", "Index"]

_LANGUAGES = (
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

_IDENTIFIER = "an identifier is expected (letters, digits, underscores; not starting with a digit)."
_LINE = "line= must be 2 or 3 (the SurrealDB major line the statements are for), got {!r}."


def _raised(error: type[Exception], call: Any, *args: Any, **kwargs: Any) -> str:
    """The text of the *error* ``call(*args, **kwargs)`` raises, its class checked exactly."""
    with pytest.raises(error) as raised:
        call(*args, **kwargs)
    assert type(raised.value) is error
    return str(raised.value)


# --- the search annotations -----------------------------------------------------------------------------------------


class TestSearchAnnotations:
    """``SearchScore``, ``SearchHighlight`` and ``SearchOffsets``: the full ORM's defaults, attributes and ``repr``;
    equal by class and attributes; references and tags checked when built."""

    def test_defaults(self) -> None:
        assert SearchScore().ref == 0
        highlight = SearchHighlight()
        assert (highlight.open_tag, highlight.close_tag, highlight.ref) == ("<b>", "</b>", 0)
        assert SearchOffsets().ref == 0

    def test_arguments_kept(self) -> None:
        assert SearchScore(3).ref == 3
        highlight = SearchHighlight("<em class='hit'>", "</em>\\", 255)
        assert (highlight.open_tag, highlight.close_tag, highlight.ref) == ("<em class='hit'>", "</em>\\", 255)
        assert SearchHighlight(ref=2).ref == 2
        assert SearchOffsets(ref=1).ref == 1

    def test_repr(self) -> None:
        assert repr(SearchScore()) == "SearchScore(0)"
        assert repr(SearchHighlight()) == "SearchHighlight('<b>', '</b>', 0)"
        assert repr(SearchOffsets()) == "SearchOffsets(0)"
        assert repr(SearchScore(7)) == "SearchScore(7)"
        assert repr(SearchHighlight("<em>", "</em>", 1)) == "SearchHighlight('<em>', '</em>', 1)"

    def test_equality_and_hash(self) -> None:
        assert SearchScore(1) == SearchScore(1)
        assert hash(SearchScore(1)) == hash(SearchScore(1))
        assert SearchScore(1) != SearchScore(2)
        assert SearchScore(0) != SearchOffsets(0)
        assert SearchOffsets(0) != SearchScore(0)
        assert SearchHighlight() == SearchHighlight("<b>", "</b>", 0)
        assert hash(SearchHighlight()) == hash(SearchHighlight("<b>", "</b>", 0))
        assert SearchHighlight("<i>") != SearchHighlight()
        assert SearchHighlight(close_tag="</i>") != SearchHighlight()
        assert SearchHighlight(ref=1) != SearchHighlight()
        assert len({SearchScore(0), SearchScore(0), SearchOffsets(0), SearchHighlight()}) == 3

    @pytest.mark.parametrize("cls", [SearchScore, SearchHighlight, SearchOffsets])
    def test_slots_and_read_only(self, cls: Any) -> None:
        annotation = cls()
        assert not hasattr(annotation, "__dict__")
        assert "ref" in cls.__slots__
        with pytest.raises(AttributeError):
            annotation.ref = 1
        assert pickle.loads(pickle.dumps(annotation)) == annotation

    @pytest.mark.parametrize("cls", [SearchScore, SearchOffsets])
    @pytest.mark.parametrize(("ref", "type_name"), [("1", "str"), (True, "bool"), (1.0, "float"), (None, "NoneType")])
    def test_ref_not_an_int(self, cls: Any, ref: object, type_name: str) -> None:
        assert _raised(TypeError, cls, ref) == f"{cls.__name__} ref must be an int, got {type_name}."

    @pytest.mark.parametrize(("ref", "type_name"), [("1", "str"), (True, "bool"), (1.0, "float")])
    def test_highlight_ref_not_an_int(self, ref: object, type_name: str) -> None:
        assert _raised(TypeError, SearchHighlight, ref=ref) == f"SearchHighlight ref must be an int, got {type_name}."

    @pytest.mark.parametrize("cls", [SearchScore, SearchOffsets])
    @pytest.mark.parametrize("ref", [-1, 256])
    def test_ref_out_of_range(self, cls: Any, ref: int) -> None:
        assert _raised(ValueError, cls, ref) == (
            f"{cls.__name__} ref must be between 0 and 255 (SurrealDB's match references), got {ref}."
        )

    @pytest.mark.parametrize("ref", [-1, 256])
    def test_highlight_ref_out_of_range(self, ref: int) -> None:
        assert _raised(ValueError, SearchHighlight, ref=ref) == (
            f"SearchHighlight ref must be between 0 and 255 (SurrealDB's match references), got {ref}."
        )

    def test_ref_bounds_accepted(self) -> None:
        assert SearchScore(255).ref == 255
        assert SearchOffsets(255).ref == 255
        assert SearchHighlight(ref=255).ref == 255
        assert SearchScore(0).ref == 0

    @pytest.mark.parametrize(("value", "type_name"), [(1, "int"), (None, "NoneType"), (b"<b>", "bytes")])
    def test_tags_not_a_str(self, value: object, type_name: str) -> None:
        assert _raised(TypeError, SearchHighlight, value) == f"SearchHighlight open_tag must be a str, got {type_name}."
        assert _raised(TypeError, SearchHighlight, "<b>", value) == (
            f"SearchHighlight close_tag must be a str, got {type_name}."
        )

    def test_any_tag_text_accepted(self) -> None:
        assert SearchHighlight("", "").open_tag == ""
        assert SearchHighlight("'", "\\").close_tag == "\\"

    def test_exported(self) -> None:
        for name in _EXPORTS:
            assert name in surreal_orm_lite.__all__, name
            assert name in search.__all__, name
            assert getattr(surreal_orm_lite, name) is getattr(search, name)
        assert sorted(search.__all__) == sorted(_EXPORTS)
        names = surreal_orm_lite.__all__
        assert names[names.index("SearchScore") : names.index("SearchScore") + 5] == _EXPORTS


class TestSearchImports:
    """``surreal_orm_lite.search`` sits low in the package: no model, queryset or connection module is imported, so
    each of them can import it."""

    def test_no_model_queryset_or_connection_module_imported(self) -> None:
        tree = ast.parse(Path(search.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(f"{node.module or ''}.{alias.name}".lstrip(".") for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        for module in ("model_base", "query_set", "edge", "model_auth", "connection_manager"):
            assert not any(
                name == module or name.endswith(f".{module}") or name.startswith(f"{module}.") for name in imported
            ), module


# --- the analyzer ---------------------------------------------------------------------------------------------------

_A1 = (
    "DEFINE ANALYZER OVERWRITE `fts_english` TOKENIZERS class,blank FILTERS lowercase,ascii,snowball(english) "
    "COMMENT 'English text';"
)
_A2 = (
    "DEFINE ANALYZER IF NOT EXISTS `fts_english` TOKENIZERS class,blank FILTERS lowercase,ascii,snowball(english) "
    "COMMENT 'English text';"
)
_A3 = "DEFINE ANALYZER OVERWRITE `fts_fn` FUNCTION fn::fts_low TOKENIZERS blank FILTERS ascii;"
_A4 = "DEFINE ANALYZER OVERWRITE `fts_empty`;"
_A5 = (
    "DEFINE ANALYZER OVERWRITE `fts_grams` TOKENIZERS blank,punct FILTERS "
    "ngram(1,3),edgengram(2,10),snowball(french),uppercase;"
)
_A6 = "DEFINE ANALYZER OVERWRITE `fts_map` TOKENIZERS blank FILTERS mapper('dict/a.txt');"

_FILTERS = (
    "SurrealDB's filters are ascii, lowercase, uppercase, ngram(min,max), edgengram(min,max), snowball(language) and "
    "mapper('path')."
)
_SNOWBALL = (
    "SurrealDB's are arabic, danish, dutch, english, finnish, french, german, greek, hungarian, italian, norwegian, "
    "portuguese, romanian, russian, spanish, swedish, tamil and turkish."
)


def _a3() -> Analyzer:
    return Analyzer("fts_fn", ["blank"], ["ascii"], function="fts_low")


def _a5() -> Analyzer:
    return Analyzer("fts_grams", ["BLANK", "Punct"], ["NGRAM( 1 , 3 )", "EdgeNGram(2,10)", "Snowball(French)", "UPPERCASE"])


def _a6() -> Analyzer:
    return Analyzer("fts_map", ["blank"], ["mapper('dict/a.txt')"])


def _analyzers() -> list[tuple[Analyzer, str]]:
    """The analyzers of the goldens and their statements."""
    return [(FTS_ENGLISH, _A1), (_a3(), _A3), (Analyzer("fts_empty"), _A4), (_a5(), _A5), (_a6(), _A6)]


class TestAnalyzer:
    """``Analyzer``: every argument checked when it is built, kept normalised; its statements rendered with no
    connection."""

    def test_goldens(self) -> None:
        assert FTS_ENGLISH.ddl() == _A1
        assert FTS_ENGLISH.ddl(overwrite=False) == _A2
        assert _a3().ddl() == _A3
        assert Analyzer("fts_empty").ddl() == _A4
        assert _a5().ddl() == _A5
        assert _a6().ddl() == _A6
        assert Analyzer("fts_empty").ddl(overwrite=False) == "DEFINE ANALYZER IF NOT EXISTS `fts_empty`;"

    def test_attributes(self) -> None:
        assert FTS_ENGLISH.name == "fts_english"
        assert FTS_ENGLISH.tokenizers == ("class", "blank")
        assert FTS_ENGLISH.filters == ("lowercase", "ascii", "snowball(english)")
        assert FTS_ENGLISH.function is None
        assert FTS_ENGLISH.comment == "English text"
        empty = Analyzer("fts_empty")
        assert (empty.tokenizers, empty.filters, empty.function, empty.comment) == ((), (), None, None)

    def test_normalisation(self) -> None:
        grams = _a5()
        assert grams.tokenizers == ("blank", "punct")
        assert grams.filters == ("ngram(1,3)", "edgengram(2,10)", "snowball(french)", "uppercase")
        assert Analyzer("fts_t", ("CAMEL", " class "), ("LowerCase", " ascii ")).tokenizers == ("camel", "class")
        assert Analyzer("fts_t", filters=("LowerCase", " ascii ", "edgengram (1, 65535)")).filters == (
            "lowercase",
            "ascii",
            "edgengram(1,65535)",
        )

    def test_function(self) -> None:
        assert _a3().function == "fn::fts_low"
        assert Analyzer("fts_t", function="fn::text::low").function == "fn::text::low"

    def test_mapper(self) -> None:
        assert _a6().filters == ("mapper('dict/a.txt')",)
        assert Analyzer("fts_t", filters=['MAPPER( "Dict/A.txt" )']).filters == ("mapper('Dict/A.txt')",)
        assert Analyzer("fts_t", filters=["mapper('dict/it\\'s.txt')"]).filters == ("mapper('dict/it\\'s.txt')",)
        assert Analyzer("fts_t", filters=["mapper('a\\\\b.txt')"]).filters == ("mapper('a\\\\b.txt')",)

    @pytest.mark.parametrize("item", ["mapper('dict/it''s.txt')", "mapper('')", "mapper(dict/a.txt)", "mapper('\\0')"])
    def test_mapper_not_one_quoted_path(self, item: str) -> None:
        assert _raised(ValueError, Analyzer, "fts_map", ["blank"], [item]) == (
            f"Analyzer 'fts_map': cannot read filter {item!r}; {_FILTERS}"
        )

    def test_name_not_a_str(self) -> None:
        assert _raised(TypeError, Analyzer, 1) == "Analyzer name must be a str, got int."

    @pytest.mark.parametrize("name", ["1x", "a b", "a\n", ""])
    def test_name_not_an_identifier(self, name: str) -> None:
        assert _raised(ValueError, Analyzer, name) == f"Invalid analyzer name {name!r}: {_IDENTIFIER}"

    def test_tokenizers_not_a_list(self) -> None:
        assert _raised(TypeError, Analyzer, "fts_x", "blank") == (
            "Analyzer 'fts_x': tokenizers= must be a list or tuple of str, got str."
        )
        assert _raised(TypeError, Analyzer, "fts_x", None) == (
            "Analyzer 'fts_x': tokenizers= must be a list or tuple of str, got NoneType."
        )

    def test_tokenizer_not_a_str(self) -> None:
        assert _raised(TypeError, Analyzer, "fts_x", ["blank", 1]) == ("Analyzer 'fts_x': tokenizers= holds int 1, not a str.")

    def test_unknown_tokenizer(self) -> None:
        assert _raised(ValueError, Analyzer, "fts_x", ["whitespace"]) == (
            "Analyzer 'fts_x': unknown tokenizer 'whitespace'; SurrealDB's tokenizers are blank, camel, class and punct."
        )

    def test_filters_not_a_list(self) -> None:
        assert _raised(TypeError, Analyzer, "fts_x", ["blank"], "ascii") == (
            "Analyzer 'fts_x': filters= must be a list or tuple of str, got str."
        )

    def test_filter_not_a_str(self) -> None:
        assert _raised(TypeError, Analyzer, "fts_x", ["blank"], ["ascii", 1]) == (
            "Analyzer 'fts_x': filters= holds int 1, not a str."
        )

    @pytest.mark.parametrize(
        "item", ["stem", "ngram(1)", "ngram", "ascii()", "lowercase(1)", "snowball()", "snowball('english')", "ngram(a,b)"]
    )
    def test_filter_not_read(self, item: str) -> None:
        assert _raised(ValueError, Analyzer, "fts_x", ["blank"], [item]) == (
            f"Analyzer 'fts_x': cannot read filter {item!r}; {_FILTERS}"
        )

    @pytest.mark.parametrize("item", ["ngram(0,1)", "ngram(3,1)", "edgengram(1,70000)", "ngram(-1,3)"])
    def test_gram_bounds(self, item: str) -> None:
        assert _raised(ValueError, Analyzer, "fts_x", ["blank"], [item]) == (
            f"Analyzer 'fts_x': filter {item!r} needs 1 <= min <= max <= 65535."
        )

    def test_unknown_snowball_language(self) -> None:
        assert _raised(ValueError, Analyzer, "fts_x", ["blank"], ["snowball(klingon)"]) == (
            f"Analyzer 'fts_x': unknown snowball language 'klingon'; {_SNOWBALL}"
        )

    @pytest.mark.parametrize("language", _LANGUAGES)
    def test_snowball_languages_in_any_case(self, language: str) -> None:
        analyzer = Analyzer("fts_x", ["blank"], [f"SNOWBALL({language.upper()})"])
        assert analyzer.filters == (f"snowball({language})",)

    def test_function_refused(self) -> None:
        for value in (1, "", "fn::bad name", "fn::x;y"):
            error: type[Exception] = TypeError if not isinstance(value, str) else ValueError
            with pytest.raises(error) as expected:
                normalize_function_name(value)  # type: ignore[arg-type]
            assert _raised(error, Analyzer, "fts_x", function=value) == str(expected.value)

    def test_comment_refused(self) -> None:
        assert _raised(TypeError, Analyzer, "fts_x", comment=1) == "Analyzer 'fts_x': comment= must be a str, got int."
        assert _raised(ValueError, Analyzer, "fts_x", comment="a\x00b") == (
            "Analyzer 'fts_x': comment= holds a NUL character (\\x00), which SurrealQL cannot carry."
        )

    def test_comment_quoted(self) -> None:
        assert (
            Analyzer("fts_x", comment="it's \\ fine").ddl() == "DEFINE ANALYZER OVERWRITE `fts_x` COMMENT 'it\\'s \\\\ fine';"
        )

    def test_overwrite_not_a_bool(self) -> None:
        assert _raised(TypeError, FTS_ENGLISH.ddl, overwrite=1) == "overwrite= must be a bool, got int."

    def test_equality_across_spellings(self) -> None:
        first = Analyzer("fts_x", ["BLANK"], ["LOWERCASE", "Snowball( English )"], function="low")
        second = Analyzer("fts_x", ("blank",), ("lowercase", "snowball(english)"), function="fn::low")
        assert first == second
        assert hash(first) == hash(second)
        assert first != Analyzer("fts_x", ["blank"], ["lowercase"], function="low")
        assert first != Analyzer("fts_y", ["BLANK"], ["LOWERCASE", "Snowball( English )"], function="low")
        assert Analyzer("fts_x", comment="a") != Analyzer("fts_x")

    def test_repr(self) -> None:
        assert repr(FTS_ENGLISH) == (
            "Analyzer('fts_english', ('class', 'blank'), ('lowercase', 'ascii', 'snowball(english)'), comment='English text')"
        )
        assert repr(_a3()) == "Analyzer('fts_fn', ('blank',), ('ascii',), function='fn::fts_low')"
        assert repr(Analyzer("fts_empty")) == "Analyzer('fts_empty', (), ())"

    def test_frozen_and_picklable(self) -> None:
        assert not hasattr(FTS_ENGLISH, "__dict__")
        with pytest.raises(AttributeError):
            FTS_ENGLISH.name = "other"  # type: ignore[misc]
        assert pickle.loads(pickle.dumps(FTS_ENGLISH)) == FTS_ENGLISH

    @pytest.mark.parametrize(("analyzer", "statement"), _analyzers())
    def test_definition_read_back(self, analyzer: Analyzer, statement: str) -> None:
        definition = search.analyzer_definition(analyzer)
        assert definition == statement.replace("DEFINE ANALYZER OVERWRITE ", "DEFINE ANALYZER ", 1).removesuffix(";")
        assert search.analyzer_statement(analyzer, True) == statement
        state = parse_define_analyzer(definition)
        assert state.name == analyzer.name
        assert tuple(state.tokenizers) == analyzer.tokenizers
        assert tuple(state.filters) == analyzer.filters
        assert state.function == analyzer.function
        assert state.comment == analyzer.comment


# --- the index ------------------------------------------------------------------------------------------------------

_I4 = "DEFINE INDEX OVERWRITE `fts_ab` ON `fts_t` FIELDS `a`, `b`.`c`;"
_I5 = "DEFINE INDEX OVERWRITE `fts_k` ON `fts_t` FIELDS `k` SEARCH ANALYZER `fts_english` BM25(2.0,1.0);"


class TestIndex:
    """``Index``: plain, ``UNIQUE`` and full-text indexes, every argument checked when it is built; its statement
    rendered with the line's keyword and no connection; the removals and the rebuild."""

    def test_goldens(self) -> None:
        assert Index("fts_ab", ["a", "b.c"]).ddl("fts_t") == _I4
        assert Index("fts_k", "k", search_analyzer="fts_english", bm25=(2, 1)).ddl("fts_t", line=2) == _I5

    def test_full_text_statements(self) -> None:
        title = Index("fts_article_title", "title", search_analyzer=FTS_ENGLISH, bm25=True, highlights=True)
        assert title.ddl("fts_article") == (
            "DEFINE INDEX OVERWRITE `fts_article_title` ON `fts_article` FIELDS `title` FULLTEXT ANALYZER "
            "`fts_english` BM25 HIGHLIGHTS;"
        )
        assert title.ddl("fts_article", overwrite=False, line=2) == (
            "DEFINE INDEX IF NOT EXISTS `fts_article_title` ON `fts_article` FIELDS `title` SEARCH ANALYZER "
            "`fts_english` BM25 HIGHLIGHTS;"
        )
        body = Index("fts_article_body", ["body"], search_analyzer="fts_english", bm25=(1.2, 0.75))
        assert body.ddl("fts_article") == (
            "DEFINE INDEX OVERWRITE `fts_article_body` ON `fts_article` FIELDS `body` FULLTEXT ANALYZER `fts_english` "
            "BM25(1.2,0.75);"
        )
        slug = Index("fts_article_slug", "slug", unique=True, comment="it's unique")
        assert slug.ddl("fts_article") == (
            "DEFINE INDEX OVERWRITE `fts_article_slug` ON `fts_article` FIELDS `slug` UNIQUE COMMENT 'it\\'s unique';"
        )
        head = Index("fts_article_head", "meta.headline", search_analyzer=FTS_ENGLISH)
        assert head.ddl("fts_article") == (
            "DEFINE INDEX OVERWRITE `fts_article_head` ON `fts_article` FIELDS `meta`.`headline` FULLTEXT ANALYZER "
            "`fts_english`;"
        )
        assert Index("fts_k", "k", search_analyzer="a", bm25=(1e-05, 0.5)).ddl("t") == (
            "DEFINE INDEX OVERWRITE `fts_k` ON `t` FIELDS `k` FULLTEXT ANALYZER `a` BM25(1e-05,0.5);"
        )

    def test_index_statement_takes_the_columns(self) -> None:
        heading = Index("fts_article_heading", "heading", search_analyzer=FTS_ENGLISH, highlights=True)
        assert search.index_statement(heading, "fts_article", ("head_text",), overwrite=True, keyword="SEARCH") == (
            "DEFINE INDEX OVERWRITE `fts_article_heading` ON `fts_article` FIELDS `head_text` SEARCH ANALYZER "
            "`fts_english` HIGHLIGHTS;"
        )

    def test_attributes(self) -> None:
        plain = Index("x", "a")
        assert plain.fields == ("a",)
        assert (plain.unique, plain.search_analyzer, plain.bm25, plain.highlights, plain.comment) == (
            False,
            None,
            None,
            False,
            None,
        )
        assert Index("x", ("a", "b.c")).fields == ("a", "b.c")
        assert Index("x", "a", search_analyzer="fts_english", bm25=False).bm25 is None
        assert Index("x", "a", search_analyzer="fts_english", bm25=True).bm25 is True
        assert Index("x", "a", search_analyzer="fts_english", bm25=(2, 1)).bm25 == (2.0, 1.0)
        assert Index("x", "a", search_analyzer=FTS_ENGLISH).search_analyzer is FTS_ENGLISH

    def test_analyzer_name(self) -> None:
        assert Index("x", "a", search_analyzer="fts_english").analyzer_name == "fts_english"
        assert Index("x", "a", search_analyzer=FTS_ENGLISH).analyzer_name == "fts_english"
        assert Index("x", "a").analyzer_name is None

    def test_equality_and_repr(self) -> None:
        assert Index("x", "a") == Index("x", ["a"])
        assert hash(Index("x", "a")) == hash(Index("x", ("a",)))
        assert Index("x", "a", search_analyzer="e", bm25=False) == Index("x", "a", search_analyzer="e")
        assert Index("x", "a", search_analyzer="e", bm25=(2, 1)) == Index("x", "a", search_analyzer="e", bm25=(2.0, 1.0))
        assert Index("x", "a") != Index("x", "a", unique=True)
        assert Index("x", "a", search_analyzer="fts_english") != Index("x", "a", search_analyzer=FTS_ENGLISH)
        assert repr(Index("fts_k", "k", search_analyzer="fts_english", bm25=(2, 1))) == (
            "Index('fts_k', ('k',), search_analyzer='fts_english', bm25=(2.0, 1.0))"
        )
        assert repr(Index("fts_s", "slug", unique=True, comment="c")) == "Index('fts_s', ('slug',), unique=True, comment='c')"
        assert repr(Index("fts_t", "t", search_analyzer=FTS_ENGLISH, bm25=True, highlights=True)) == (
            f"Index('fts_t', ('t',), search_analyzer={FTS_ENGLISH!r}, bm25=True, highlights=True)"
        )

    def test_frozen_and_picklable(self) -> None:
        index = Index("fts_t", "t", search_analyzer=FTS_ENGLISH, bm25=(1.2, 0.75))
        assert not hasattr(index, "__dict__")
        with pytest.raises(AttributeError):
            index.unique = True  # type: ignore[misc]
        assert pickle.loads(pickle.dumps(index)) == index

    def test_name_refused(self) -> None:
        assert _raised(TypeError, Index, 1, "a") == "Index name must be a str, got int."
        assert _raised(ValueError, Index, "1x", "a") == f"Invalid index name '1x': {_IDENTIFIER}"

    @pytest.mark.parametrize(
        ("fields", "error", "text"),
        [
            (3, TypeError, "Index 'fts_x': fields= must be a str or a list or tuple of str, got int."),
            ([3], TypeError, "Index 'fts_x': fields= holds int 3, not a str."),
            ([], ValueError, "Index 'fts_x': fields= names no field."),
            ((), ValueError, "Index 'fts_x': fields= names no field."),
            (["a b"], ValueError, "Index 'fts_x': field 'a b' is not a field name or a dotted path."),
            (["a", "a"], ValueError, "Index 'fts_x': field 'a' is named twice."),
            (["a\n"], ValueError, "Index 'fts_x': field 'a\\n' is not a field name or a dotted path."),
            ("", ValueError, "Index 'fts_x': field '' is not a field name or a dotted path."),
            ("a.", ValueError, "Index 'fts_x': field 'a.' is not a field name or a dotted path."),
        ],
    )
    def test_fields_refused(self, fields: object, error: type[Exception], text: str) -> None:
        assert _raised(error, Index, "fts_x", fields) == text

    @pytest.mark.parametrize("argument", ["unique", "highlights"])
    def test_flags_not_a_bool(self, argument: str) -> None:
        assert _raised(TypeError, Index, "fts_x", "a", **{argument: 1}) == (
            f"Index 'fts_x': {argument}= must be a bool, got int."
        )

    def test_search_analyzer_refused(self) -> None:
        assert _raised(TypeError, Index, "fts_x", "a", search_analyzer=1) == (
            "Index 'fts_x': search_analyzer= must be an Analyzer, an analyzer name or None, got int."
        )
        assert _raised(ValueError, Index, "fts_x", "a", search_analyzer="1a") == (f"Invalid analyzer name '1a': {_IDENTIFIER}")

    def test_full_text_on_several_fields(self) -> None:
        assert _raised(ValueError, Index, "fts_x", ["title", "body"], search_analyzer="fts_english") == (
            "Index 'fts_x': a full-text index covers one field (SurrealDB 3.x refuses several); got 2: 'title', "
            "'body'. Define one index per field."
        )

    def test_full_text_unique(self) -> None:
        assert _raised(ValueError, Index, "fts_x", "a", search_analyzer="fts_english", unique=True) == (
            "Index 'fts_x': a full-text index cannot be UNIQUE (SurrealDB drops UNIQUE from it)."
        )

    @pytest.mark.parametrize("arguments", [{"bm25": True}, {"bm25": (1.2, 0.75)}, {"highlights": True}])
    def test_full_text_options_without_an_analyzer(self, arguments: dict[str, Any]) -> None:
        assert _raised(ValueError, Index, "fts_x", "a", **arguments) == (
            "Index 'fts_x': bm25= and highlights= need search_analyzer= (a full-text index)."
        )

    @pytest.mark.parametrize(
        ("bm25", "type_name"), [("x", "str"), ((1,), "tuple"), ((True, 0.5), "tuple"), ([1.2, 0.75], "list"), (1, "int")]
    )
    def test_bm25_not_a_pair(self, bm25: object, type_name: str) -> None:
        assert _raised(TypeError, Index, "fts_x", "a", search_analyzer="e", bm25=bm25) == (
            f"Index 'fts_x': bm25= must be True, False, None or a (k1, b) pair of numbers, got {type_name}."
        )

    @pytest.mark.parametrize("bm25", [(-1, 0.5), (1, 2), (float("inf"), 0.5), (1.2, -0.1), (math.nan, 0.5), (10**400, 0.5)])
    def test_bm25_out_of_range(self, bm25: tuple[float, float]) -> None:
        assert _raised(ValueError, Index, "fts_x", "a", search_analyzer="e", bm25=bm25) == (
            f"Index 'fts_x': bm25= needs finite k1 >= 0 and 0 <= b <= 1, got {bm25!r}."
        )

    def test_bm25_bounds_accepted(self) -> None:
        assert Index("fts_x", "a", search_analyzer="e", bm25=(0, 0)).bm25 == (0.0, 0.0)
        assert Index("fts_x", "a", search_analyzer="e", bm25=(-0.0, 1)).ddl("t").endswith(" BM25(0.0,1.0);")

    def test_comment_refused(self) -> None:
        assert _raised(TypeError, Index, "fts_x", "a", comment=1) == "Index 'fts_x': comment= must be a str, got int."
        assert _raised(ValueError, Index, "fts_x", "a", comment="\x00") == (
            "Index 'fts_x': comment= holds a NUL character (\\x00), which SurrealQL cannot carry."
        )

    @pytest.mark.parametrize(("line", "error"), [(4, ValueError), (1, ValueError), ("3", TypeError), (True, TypeError)])
    def test_line_refused(self, line: object, error: type[Exception]) -> None:
        assert _raised(error, Index("fts_x", "a").ddl, "fts_t", line=line) == _LINE.format(line)

    def test_table_checked(self) -> None:
        assert _raised(TypeError, Index("fts_x", "a").ddl, 1) == "Table name must be a str, got int."
        assert _raised(ValueError, Index("fts_x", "a").ddl, "1t") == f"Invalid table name '1t': {_IDENTIFIER}"

    def test_overwrite_not_a_bool(self) -> None:
        assert _raised(TypeError, Index("fts_x", "a").ddl, "fts_t", overwrite=1) == "overwrite= must be a bool, got int."

    def test_removals_and_rebuild(self) -> None:
        assert search.remove_index_statement("fts_article_title", "fts_article", True) == (
            "REMOVE INDEX IF EXISTS `fts_article_title` ON `fts_article`;"
        )
        assert search.remove_index_statement("fts_article_title", "fts_article", False) == (
            "REMOVE INDEX `fts_article_title` ON `fts_article`;"
        )
        assert search.remove_analyzer_statement("fts_english", True) == "REMOVE ANALYZER IF EXISTS `fts_english`;"
        assert search.remove_analyzer_statement("fts_english", False) == "REMOVE ANALYZER `fts_english`;"
        assert search.rebuild_index_statement("fts_article_title", "fts_article") == (
            "REBUILD INDEX `fts_article_title` ON `fts_article`;"
        )

    def test_removals_checked(self) -> None:
        assert _raised(ValueError, search.remove_index_statement, "1x", "t", True) == (
            f"Invalid index name '1x': {_IDENTIFIER}"
        )
        assert _raised(ValueError, search.remove_index_statement, "x", "1t", True) == (
            f"Invalid table name '1t': {_IDENTIFIER}"
        )
        assert _raised(TypeError, search.remove_index_statement, "x", "t", 1) == "missing_ok= must be a bool, got int."
        assert _raised(ValueError, search.remove_analyzer_statement, "1a", True) == (
            f"Invalid analyzer name '1a': {_IDENTIFIER}"
        )
        assert _raised(TypeError, search.remove_analyzer_statement, "a", 1) == "missing_ok= must be a bool, got int."

    def test_rebuild_writes_a_stored_name_in_backticks(self) -> None:
        assert search.rebuild_index_statement("my-index", "my`table") == "REBUILD INDEX `my-index` ON `my\\`table`;"

    def test_keyword(self) -> None:
        assert search.fulltext_keyword(3) == "FULLTEXT"
        assert search.fulltext_keyword(4) == "FULLTEXT"
        assert search.fulltext_keyword(2) == "SEARCH"


# --- the model's indexes --------------------------------------------------------------------------------------------


class FtsArticle(BaseSurrealModel):
    """The model of the goldens and of the E2E tests: four full-text indexes holding ``FTS_ENGLISH``, one naming it,
    one ``UNIQUE``, on plain, dotted, array and aliased fields."""

    model_config = SurrealConfigDict(
        table_name="fts_article",
        indexes=[
            Index("fts_article_title", "title", search_analyzer=FTS_ENGLISH, bm25=True, highlights=True),
            Index("fts_article_body", ["body"], search_analyzer="fts_english", bm25=(1.2, 0.75)),
            Index("fts_article_slug", "slug", unique=True, comment="it's unique"),
            Index("fts_article_head", "meta.headline", search_analyzer=FTS_ENGLISH),
            Index("fts_article_tags", "tags", search_analyzer=FTS_ENGLISH, highlights=True),
            Index("fts_article_heading", "heading", search_analyzer=FTS_ENGLISH, highlights=True),
        ],
    )

    id: str | None = None
    title: str
    body: str = ""
    slug: str
    meta: dict[str, Any] = {}
    tags: list[str] = []
    heading: str = Field(default="", alias="head_text")
    views: int = 0


class FtsPlain(BaseSurrealModel):
    """A model that declares no index."""

    model_config = SurrealConfigDict(table_name="fts_plain")

    id: str | None = None
    title: str = ""


_ARTICLE_INDEXES = [
    "fts_article_title",
    "fts_article_body",
    "fts_article_slug",
    "fts_article_head",
    "fts_article_tags",
    "fts_article_heading",
]

#: I1's index statements, after A1.
_I1_INDEXES = [
    "DEFINE INDEX OVERWRITE `fts_article_title` ON `fts_article` FIELDS `title` FULLTEXT ANALYZER `fts_english` BM25 "
    "HIGHLIGHTS;",
    "DEFINE INDEX OVERWRITE `fts_article_body` ON `fts_article` FIELDS `body` FULLTEXT ANALYZER `fts_english` BM25(1.2,0.75);",
    "DEFINE INDEX OVERWRITE `fts_article_slug` ON `fts_article` FIELDS `slug` UNIQUE COMMENT 'it\\'s unique';",
    "DEFINE INDEX OVERWRITE `fts_article_head` ON `fts_article` FIELDS `meta`.`headline` FULLTEXT ANALYZER `fts_english`;",
    "DEFINE INDEX OVERWRITE `fts_article_tags` ON `fts_article` FIELDS `tags` FULLTEXT ANALYZER `fts_english` HIGHLIGHTS;",
    "DEFINE INDEX OVERWRITE `fts_article_heading` ON `fts_article` FIELDS `head_text` FULLTEXT ANALYZER `fts_english` "
    "HIGHLIGHTS;",
]
_I1 = [_A1, *_I1_INDEXES]
_I2 = [_A1, *(statement.replace(" FULLTEXT ", " SEARCH ") for statement in _I1_INDEXES)]
_I3 = [_A2, *(statement.replace("DEFINE INDEX OVERWRITE ", "DEFINE INDEX IF NOT EXISTS ") for statement in _I1_INDEXES)]

_IC1 = "FtsBad: model_config['indexes'] must be a list or tuple of Index, got {}."
_IC3 = "FtsBad: two indexes are named {!r} in model_config['indexes']."
_IC4 = (
    "FtsBad: two full-text indexes cover column {!r} ({!r}, {!r}); SurrealDB would use either for search(), with "
    "different scores. Keep one."
)
_IC5 = (
    "FtsBad: model_config['indexes'] holds two definitions of analyzer {!r}; declare one Analyzer object and reference "
    "it from every index."
)
_IC6 = (
    "{}: a materialized view (view_query) declares no indexes: SurrealDB 3.x does not update an index on a view as "
    "the view changes, and define_view() drops them."
)


def _text(name: str) -> Index:
    """A full-text index on ``title`` holding ``FTS_ENGLISH``."""
    return Index(name, "title", search_analyzer=FTS_ENGLISH)


def _bad_model(indexes: object, **config: Any) -> type[BaseSurrealModel]:
    """A model ``FtsBad`` with ``indexes=`` *indexes* and the other keys of *config*, built by its ``class``
    statement."""

    class FtsBad(BaseSurrealModel):
        model_config = SurrealConfigDict(table_name="fts_bad", indexes=indexes, **config)  # type: ignore[typeddict-item]

        id: str | None = None
        title: str = ""
        body: str = ""
        heading: str = Field(default="", alias="head_text")

    return FtsBad


class TestIndexesConfig:
    """``SurrealConfigDict(indexes=[...])``, checked at the ``class`` statement — the first fault raises, in the order
    list or tuple, ``Index`` items, distinct names, no view, one full-text index per column, one definition per
    analyzer name."""

    def test_the_golden_model_is_accepted(self) -> None:
        assert FtsArticle.get_table_name() == "fts_article"
        assert [index.name for index in FtsArticle.get_indexes()] == _ARTICLE_INDEXES
        assert FtsArticle.to_db_field("heading") == "head_text"

    def test_indexes_is_a_config_key(self) -> None:
        assert SurrealConfigDict.__annotations__["indexes"] == Sequence[Index] | None
        assert "indexes" in SurrealConfigDict.__optional_keys__
        doc = " ".join((SurrealConfigDict.__doc__ or "").split())
        assert "indexes (Sequence[Index] | None): The table's indexes (v0.35.0)" in doc

    @pytest.mark.parametrize(
        ("indexes", "error", "text"),
        [
            ({}, TypeError, _IC1.format("dict")),
            ("x", TypeError, _IC1.format("str")),
            (Index("fts_x", "a"), TypeError, _IC1.format("Index")),
            ({Index("fts_x", "a")}, TypeError, _IC1.format("set")),
            (["x"], TypeError, "FtsBad: model_config['indexes'][0] is not an Index, got str."),
            ((Index("fts_x", "a"), 1), TypeError, "FtsBad: model_config['indexes'][1] is not an Index, got int."),
            ([FTS_ENGLISH], TypeError, "FtsBad: model_config['indexes'][0] is not an Index, got Analyzer."),
            ([Index("fts_x", "a"), Index("fts_x", "b", unique=True)], ValueError, _IC3.format("fts_x")),
            (
                [_text("fts_t1"), Index("fts_t2", "title", search_analyzer="fts_other")],
                ValueError,
                _IC4.format("title", "fts_t1", "fts_t2"),
            ),
            (
                [
                    Index("fts_h1", "heading", search_analyzer=FTS_ENGLISH),
                    Index("fts_h2", "head_text", search_analyzer=FTS_ENGLISH),
                ],
                ValueError,
                _IC4.format("head_text", "fts_h1", "fts_h2"),
            ),
            (
                [
                    Index("fts_a", "title", search_analyzer=Analyzer("fts_english", ["blank"], ["lowercase"])),
                    Index("fts_b", "body", search_analyzer=Analyzer("fts_english", ["blank"], ["ascii"])),
                ],
                ValueError,
                _IC5.format("fts_english"),
            ),
        ],
        ids=[
            "ic1-dict",
            "ic1-str",
            "ic1-index",
            "ic1-set",
            "ic2-str",
            "ic2-int",
            "ic2-analyzer",
            "ic3",
            "ic4",
            "ic4-alias",
            "ic5",
        ],
    )
    def test_refused_at_the_class_statement(self, indexes: object, error: type[Exception], text: str) -> None:
        assert _raised(error, _bad_model, indexes) == text

    def test_ic6_a_materialized_view_declares_no_index(self) -> None:
        assert _raised(ValueError, _bad_model, [Index("fts_v", "title")], view_query="SELECT title FROM fts_src") == (
            _IC6.format("FtsBad")
        )

    def test_an_inherited_value_is_checked_again_naming_the_subclass(self) -> None:
        with pytest.raises(ValueError) as raised:

            class FtsArticleView(FtsArticle):
                model_config = SurrealConfigDict(table_name="fts_article_view", view_query="SELECT * FROM fts_article")

        assert str(raised.value) == _IC6.format("FtsArticleView")

    @pytest.mark.parametrize(
        ("indexes", "config", "error", "text"),
        [
            # IC2 before IC3: the item that is not an Index comes last.
            (
                [Index("fts_x", "a"), Index("fts_x", "b"), 1],
                {},
                TypeError,
                "FtsBad: model_config['indexes'][2] is not an Index, got int.",
            ),
            # IC3 before IC6.
            (
                [Index("fts_x", "a"), Index("fts_x", "b")],
                {"view_query": "SELECT * FROM fts_src"},
                ValueError,
                _IC3.format("fts_x"),
            ),
            # IC3 before IC4: two full-text indexes on one column, under one name.
            ([_text("fts_x"), _text("fts_x")], {}, ValueError, _IC3.format("fts_x")),
            # IC6 before IC4.
            ([_text("fts_t1"), _text("fts_t2")], {"view_query": "SELECT * FROM fts_src"}, ValueError, _IC6.format("FtsBad")),
            # IC4 before IC5.
            (
                [
                    Index("fts_t1", "title", search_analyzer=Analyzer("fts_english", ["blank"])),
                    Index("fts_t2", "title", search_analyzer=Analyzer("fts_english", ["punct"])),
                ],
                {},
                ValueError,
                _IC4.format("title", "fts_t1", "fts_t2"),
            ),
        ],
        ids=["ic2-before-ic3", "ic3-before-ic6", "ic3-before-ic4", "ic6-before-ic4", "ic4-before-ic5"],
    )
    def test_the_first_fault_raises(self, indexes: object, config: dict[str, Any], error: type[Exception], text: str) -> None:
        assert _raised(error, _bad_model, indexes, **config) == text

    @pytest.mark.parametrize("indexes", [None, [], ()], ids=["none", "empty-list", "empty-tuple"])
    def test_no_index_accepted(self, indexes: object) -> None:
        assert _bad_model(indexes).get_indexes() == ()
        assert _bad_model(indexes, view_query="SELECT * FROM fts_src").get_indexes() == ()

    def test_a_plain_a_unique_and_a_full_text_index_on_one_field_accepted(self) -> None:
        indexes = [Index("fts_s", "title"), _text("fts_t"), Index("fts_u", "title", unique=True)]
        assert _bad_model(indexes).get_indexes() == tuple(indexes)

    def test_one_analyzer_name_given_as_an_object_and_as_a_str_accepted(self) -> None:
        indexes = [_text("fts_t"), Index("fts_b", "body", search_analyzer="fts_english")]
        assert _bad_model(indexes).get_indexes() == tuple(indexes)

    def test_two_equal_analyzer_objects_under_one_name_accepted(self) -> None:
        twin = Analyzer("fts_english", ["CLASS", "blank"], ["lowercase", "ASCII", "snowball(English)"], comment="English text")
        assert twin == FTS_ENGLISH
        assert twin is not FTS_ENGLISH
        indexes = [_text("fts_t"), Index("fts_b", "body", search_analyzer=twin)]
        assert _bad_model(indexes).get_indexes() == tuple(indexes)

    def test_a_subclass_inherits_the_indexes(self) -> None:
        class FtsArticleCopy(FtsArticle):
            model_config = SurrealConfigDict(table_name="fts_article_copy")

        assert FtsArticleCopy.get_indexes() == FtsArticle.get_indexes()

    def test_an_empty_list_drops_the_inherited_indexes(self) -> None:
        class FtsArticleBare(FtsArticle):
            model_config = SurrealConfigDict(indexes=[])

        assert FtsArticleBare.get_indexes() == ()
        assert FtsArticleBare.index_ddl() == []


class TestGetIndexes:
    """``get_indexes()``: the declared indexes, a ``tuple`` in declaration order; ``()`` when none."""

    def test_declaration_order(self) -> None:
        indexes = FtsArticle.get_indexes()
        assert isinstance(indexes, tuple)
        assert indexes == tuple(FtsArticle.model_config["indexes"] or ())
        assert [index.name for index in indexes] == _ARTICLE_INDEXES

    def test_a_tuple_declaration(self) -> None:
        indexes = (Index("fts_z", "title", unique=True), Index("fts_a", "body"))
        assert _bad_model(indexes).get_indexes() == indexes

    def test_none_declared(self) -> None:
        assert FtsPlain.get_indexes() == ()


class TestIndexDdl:
    """``index_ddl()``: the analyzers the indexes hold as objects, each once, then the indexes on the model's table,
    each field under its column — pure, with no connection."""

    def test_i1(self) -> None:
        assert FtsArticle.index_ddl() == _I1
        assert FtsArticle.index_ddl(line=3) == _I1

    def test_i2(self) -> None:
        assert FtsArticle.index_ddl(line=2) == _I2
        assert not any("FULLTEXT" in statement for statement in _I2)

    def test_i3(self) -> None:
        assert FtsArticle.index_ddl(overwrite=False) == _I3
        assert FtsArticle.index_ddl(overwrite=False, line=2) == [
            _A2,
            *(statement.replace(" FULLTEXT ", " SEARCH ") for statement in _I3[1:]),
        ]

    def test_none_declared(self) -> None:
        assert FtsPlain.index_ddl() == []
        assert FtsPlain.index_ddl(overwrite=False, line=2) == []

    def test_each_analyzer_once_in_declaration_order(self) -> None:
        other = Analyzer("fts_other", ["blank"], ["lowercase"])
        twin = Analyzer("fts_english", ["class", "blank"], ["lowercase", "ascii", "snowball(english)"], comment="English text")
        model = _bad_model(
            [
                Index("fts_t", "title", search_analyzer=FTS_ENGLISH),
                Index("fts_b", "body", search_analyzer=other),
                Index("fts_h", "heading", search_analyzer=twin),
            ]
        )
        assert model.index_ddl() == [
            _A1,
            "DEFINE ANALYZER OVERWRITE `fts_other` TOKENIZERS blank FILTERS lowercase;",
            "DEFINE INDEX OVERWRITE `fts_t` ON `fts_bad` FIELDS `title` FULLTEXT ANALYZER `fts_english`;",
            "DEFINE INDEX OVERWRITE `fts_b` ON `fts_bad` FIELDS `body` FULLTEXT ANALYZER `fts_other`;",
            "DEFINE INDEX OVERWRITE `fts_h` ON `fts_bad` FIELDS `head_text` FULLTEXT ANALYZER `fts_english`;",
        ]

    def test_an_analyzer_named_by_a_str_is_not_rendered(self) -> None:
        model = _bad_model([Index("fts_b", "body", search_analyzer="fts_english", bm25=True)])
        assert model.index_ddl() == [
            "DEFINE INDEX OVERWRITE `fts_b` ON `fts_bad` FIELDS `body` FULLTEXT ANALYZER `fts_english` BM25;"
        ]

    def test_a_subclass_renders_on_its_own_table(self) -> None:
        class FtsArticleCopy(FtsArticle):
            model_config = SurrealConfigDict(table_name="fts_article_copy")

        assert FtsArticleCopy.index_ddl() == [
            statement.replace("ON `fts_article`", "ON `fts_article_copy`") for statement in _I1
        ]

    @pytest.mark.parametrize("model", [FtsArticle, FtsPlain], ids=["indexes", "none"])
    def test_overwrite_not_a_bool(self, model: type[BaseSurrealModel]) -> None:
        assert _raised(TypeError, model.index_ddl, overwrite=1) == "overwrite= must be a bool, got int."

    @pytest.mark.parametrize(("line", "error"), [(4, ValueError), (1, ValueError), ("3", TypeError), (True, TypeError)])
    @pytest.mark.parametrize("model", [FtsArticle, FtsPlain], ids=["indexes", "none"])
    def test_line_refused(self, model: type[BaseSurrealModel], line: object, error: type[Exception]) -> None:
        assert _raised(error, model.index_ddl, line=line) == _LINE.format(line)

    async def test_works_with_no_connection_configured(self) -> None:
        await SurrealDBConnectionManager.unset_connection()
        assert SurrealDBConnectionManager.is_connection_set() is False
        assert FtsArticle.index_ddl() == _I1
        assert FtsArticle.index_ddl() == FtsArticle.index_ddl()
        assert SurrealDBConnectionManager.is_connection_set() is False


# --- the comparison with the stored definitions ---------------------------------------------------------------------

#: How ``INFO FOR DB`` renders the goldens' analyzers on SurrealDB 3.x (measured on 3.3.4 and 3.1.5).
_ANALYZERS_3X = {
    "fts_english": (
        "DEFINE ANALYZER fts_english TOKENIZERS CLASS,BLANK FILTERS LOWERCASE, ASCII, SNOWBALL(ENGLISH) COMMENT 'English text'"
    ),
    "fts_fn": "DEFINE ANALYZER fts_fn FUNCTION fn::fts_low TOKENIZERS BLANK FILTERS ASCII",
    "fts_empty": "DEFINE ANALYZER fts_empty",
    "fts_grams": (
        "DEFINE ANALYZER fts_grams TOKENIZERS BLANK,PUNCT FILTERS NGRAM(1,3), EDGENGRAM(2,10), SNOWBALL(FRENCH), UPPERCASE"
    ),
}

#: The same on SurrealDB 2.x and the embedded engine (measured on 2.7.0 and ``mem://``): no space after a comma.
_ANALYZERS_2X = {name: statement.replace(", ", ",") for name, statement in _ANALYZERS_3X.items()}

#: The tuning options SurrealDB 2.x renders after a full-text index's ``BM25`` (measured on 2.7.0 and ``mem://``).
_TUNING_2X = (
    " DOC_IDS_ORDER 100 DOC_LENGTHS_ORDER 100 POSTINGS_ORDER 100 TERMS_ORDER 100 DOC_IDS_CACHE 100 DOC_LENGTHS_CACHE 100"
    " POSTINGS_CACHE 100 TERMS_CACHE 100"
)

#: How ``INFO FOR TABLE`` renders the goldens' indexes on SurrealDB 3.x (measured on 3.3.4 and 3.1.5).
_INDEXES_3X = {
    "fts_article_title": (
        "DEFINE INDEX fts_article_title ON fts_article FIELDS title FULLTEXT ANALYZER fts_english BM25(1.2,0.75) HIGHLIGHTS"
    ),
    "fts_article_body": "DEFINE INDEX fts_article_body ON fts_article FIELDS body FULLTEXT ANALYZER fts_english BM25(1.2,0.75)",
    "fts_article_slug": 'DEFINE INDEX fts_article_slug ON fts_article FIELDS slug UNIQUE COMMENT "it\'s unique"',
    "fts_article_head": (
        "DEFINE INDEX fts_article_head ON fts_article FIELDS meta.headline FULLTEXT ANALYZER fts_english BM25(1.2,0.75)"
    ),
    "fts_k": "DEFINE INDEX fts_k ON fts_t FIELDS k FULLTEXT ANALYZER fts_english BM25(2,1)",
    "fts_k2": "DEFINE INDEX fts_k2 ON fts_t FIELDS k2 FULLTEXT ANALYZER fts_english BM25(0.00001,0.5)",
    "fts_ab": "DEFINE INDEX fts_ab ON fts_t FIELDS a, b.c",
}


def _index_2x(statement: str) -> str:
    """*statement* (a 3.x rendering) as SurrealDB 2.x renders it: ``SEARCH``, the tuning options after ``BM25``."""
    statement = statement.replace(" FULLTEXT ", " SEARCH ")
    return re.sub(r"( BM25\([^)]*\))", lambda found: found.group(1) + _TUNING_2X, statement)


#: The same on SurrealDB 2.x and the embedded engine (measured on 2.7.0 and ``mem://``).
_INDEXES_2X = {name: _index_2x(statement) for name, statement in _INDEXES_3X.items()}

#: The declarations of ``_INDEXES_3X``'s indexes on ``fts_t``.
_FTS_T_INDEXES = {
    "fts_k": Index("fts_k", "k", search_analyzer="fts_english", bm25=(2, 1)),
    "fts_k2": Index("fts_k2", "k2", search_analyzer="fts_english", bm25=(1e-05, 0.5)),
    "fts_ab": Index("fts_ab", ["a", "b.c"]),
}


def _article_index(name: str) -> Index:
    """The index *name* of ``FtsArticle``."""
    return next(index for index in FtsArticle.get_indexes() if index.name == name)


def _columns(index: Index) -> list[str]:
    """The columns of *index*'s fields on ``FtsArticle`` (``heading`` → ``head_text``)."""
    return [FtsArticle.to_db_field(field) for field in index.fields]


def _golden_analyzers() -> list[Analyzer]:
    """The analyzers of A1, A3, A4 and A5: the goldens applied on the engines (A6 reads a server file)."""
    return [FTS_ENGLISH, _a3(), Analyzer("fts_empty"), _a5()]


class TestMatches:
    """``analyzer_matches()`` and ``index_matches()``: a declaration against what ``INFO`` stores, on both lines'
    renderings — pure."""

    @pytest.mark.parametrize("rendering", [_ANALYZERS_3X, _ANALYZERS_2X], ids=["3x", "2x"])
    def test_analyzers_match_each_lines_rendering(self, rendering: dict[str, str]) -> None:
        for analyzer in _golden_analyzers():
            assert search.analyzer_matches(analyzer, parse_define_analyzer(rendering[analyzer.name])), analyzer.name

    def test_two_spellings_of_one_analyzer_match(self) -> None:
        twin = Analyzer(
            "fts_english", ["CLASS", "Blank"], ["LowerCase", " ascii ", "Snowball( English )"], comment="English text"
        )
        assert search.analyzer_matches(twin, parse_define_analyzer(_ANALYZERS_3X["fts_english"]))

    @pytest.mark.parametrize(
        "analyzer",
        [
            Analyzer("fts_english", ["class", "blank"], ["lowercase", "ascii", "snowball(french)"], comment="English text"),
            Analyzer("fts_english", ["class", "blank"], ["lowercase", "ascii"], comment="English text"),
            Analyzer("fts_english", ["class", "blank"], ["ascii", "lowercase", "snowball(english)"], comment="English text"),
            Analyzer("fts_english", ["blank", "class"], ["lowercase", "ascii", "snowball(english)"], comment="English text"),
            Analyzer("fts_english", ["class"], ["lowercase", "ascii", "snowball(english)"], comment="English text"),
            Analyzer("fts_english", ["class", "blank"], ["lowercase", "ascii", "snowball(english)"], comment="Other text"),
            Analyzer("fts_english", ["class", "blank"], ["lowercase", "ascii", "snowball(english)"]),
            Analyzer(
                "fts_english",
                ["class", "blank"],
                ["lowercase", "ascii", "snowball(english)"],
                function="fts_low",
                comment="English text",
            ),
        ],
        ids=[
            "filter",
            "filter-missing",
            "filter-order",
            "tokenizer-order",
            "tokenizer-missing",
            "comment",
            "no-comment",
            "function",
        ],
    )
    def test_an_analyzer_that_differs(self, analyzer: Analyzer) -> None:
        assert not search.analyzer_matches(analyzer, parse_define_analyzer(_ANALYZERS_3X["fts_english"]))
        assert not search.analyzer_matches(analyzer, parse_define_analyzer(_ANALYZERS_2X["fts_english"]))

    def test_another_function(self) -> None:
        assert not search.analyzer_matches(
            Analyzer("fts_fn", ["blank"], ["ascii"], function="fts_up"), parse_define_analyzer(_ANALYZERS_3X["fts_fn"])
        )
        assert not search.analyzer_matches(
            Analyzer("fts_fn", ["blank"], ["ascii"]), parse_define_analyzer(_ANALYZERS_3X["fts_fn"])
        )

    def test_no_analyzer_stored(self) -> None:
        assert not search.analyzer_matches(FTS_ENGLISH, None)

    def test_a_mapper_path_without_its_quotes_and_case(self) -> None:
        """The embedded engine renders a ``mapper()`` path unquoted, which ``parse_define_analyzer()`` lower-cases."""
        mapper = Analyzer("fts_map", ["blank"], ["mapper('/tmp/Dict/map.txt')", "lowercase"])
        embedded = parse_define_analyzer("DEFINE ANALYZER fts_map TOKENIZERS BLANK FILTERS MAPPER(/tmp/Dict/map.txt),LOWERCASE")
        assert embedded.filters == ("mapper(/tmp/dict/map.txt)", "lowercase")
        assert search.analyzer_matches(mapper, embedded)
        quoted = parse_define_analyzer(
            "DEFINE ANALYZER fts_map TOKENIZERS BLANK FILTERS MAPPER('/tmp/Dict/map.txt'), LOWERCASE"
        )
        assert search.analyzer_matches(mapper, quoted)
        other = parse_define_analyzer("DEFINE ANALYZER fts_map TOKENIZERS BLANK FILTERS MAPPER(/tmp/other.txt),LOWERCASE")
        assert not search.analyzer_matches(mapper, other)
        assert not search.analyzer_matches(Analyzer("fts_map", ["blank"], ["mapper('/tmp/Dict/map.txt')"]), embedded)

    def test_a_mapper_path_with_an_escape(self) -> None:
        mapper = Analyzer("fts_map", ["blank"], ["mapper('C:\\\\Dict\\\\map.txt')"])
        assert mapper.filters == ("mapper('C:\\\\Dict\\\\map.txt')",)
        assert search.analyzer_matches(mapper, parse_define_analyzer(search.analyzer_definition(mapper)))
        # A quoted path is compared decoded: the ORM's escaped quote equals a double-quoted one, and a path whose
        # quotes are not one literal is compared without its outer pair.
        quote = Analyzer("fts_map", ["blank"], ['mapper("dict/it\'s.txt")'])
        assert quote.filters == ("mapper('dict/it\\'s.txt')",)
        assert search.analyzer_matches(quote, AnalyzerState("fts_map", ("blank",), ('mapper("dict/it\'s.txt")',)))
        assert search.analyzer_matches(quote, AnalyzerState("fts_map", ("blank",), ("mapper('dict/it's.txt')",)))
        assert not search.analyzer_matches(quote, AnalyzerState("fts_map", ("blank",), ("mapper('dict/its.txt')",)))

    @pytest.mark.parametrize(("analyzer", "statement"), _analyzers(), ids=["a1", "a3", "a4", "a5", "a6"])
    def test_never_parses_the_analyzers_own_statement(self, analyzer: Analyzer, statement: str) -> None:
        """``parse_define_analyzer()`` reads no ``OVERWRITE`` clause and no final ``;``: ``analyzer_matches()`` reads
        the definition without them, so it never raises for an analyzer the ORM renders."""
        with pytest.raises(DefineParseError):
            parse_define_analyzer(analyzer.ddl())
        stored = parse_define_analyzer(search.analyzer_definition(analyzer))
        assert search.analyzer_matches(analyzer, stored)

    @pytest.mark.parametrize("rendering", [_INDEXES_3X, _INDEXES_2X], ids=["3x", "2x"])
    def test_indexes_match_each_lines_rendering(self, rendering: dict[str, str]) -> None:
        for name in ("fts_article_title", "fts_article_body", "fts_article_slug", "fts_article_head"):
            index = _article_index(name)
            assert search.index_matches(index, _columns(index), parse_define_index(rendering[name])), name
        for name, index in _FTS_T_INDEXES.items():
            assert search.index_matches(index, index.fields, parse_define_index(rendering[name])), name

    @pytest.mark.parametrize("bm25", [None, True, (1.2, 0.75), (1.2000000001, 0.75)])
    def test_the_default_bm25(self, bm25: Any) -> None:
        """``bm25=None`` and ``True`` are SurrealDB's ``BM25(1.2,0.75)``, which every line renders."""
        index = Index("fts_article_body", "body", search_analyzer="fts_english", bm25=bm25)
        for rendering in (_INDEXES_3X, _INDEXES_2X):
            assert search.index_matches(index, ["body"], parse_define_index(rendering["fts_article_body"]))
        for stored in ("BM25", ""):
            state = parse_define_index(
                f"DEFINE INDEX fts_article_body ON fts_article FIELDS body FULLTEXT ANALYZER fts_english {stored}"
            )
            assert search.index_matches(index, ["body"], state), stored

    @pytest.mark.parametrize("bm25", [(1.2, 0.7), (1.3, 0.75), (2, 1), (0, 0)])
    def test_other_bm25_numbers(self, bm25: tuple[float, float]) -> None:
        index = Index("fts_article_body", "body", search_analyzer="fts_english", bm25=bm25)
        assert not search.index_matches(index, ["body"], parse_define_index(_INDEXES_3X["fts_article_body"]))

    @pytest.mark.parametrize(
        ("index", "columns"),
        [
            (_article_index("fts_article_title"), ["body"]),
            (_article_index("fts_article_title"), ["title", "body"]),
            (Index("fts_article_title", "title", search_analyzer="fts_other", bm25=True, highlights=True), ["title"]),
            (Index("fts_article_title", "title", search_analyzer=FTS_ENGLISH, bm25=True), ["title"]),
            (
                Index("fts_article_title", "title", search_analyzer=FTS_ENGLISH, bm25=True, highlights=True, comment="c"),
                ["title"],
            ),
            (Index("fts_article_title", "title"), ["title"]),
        ],
        ids=["field", "fields", "analyzer", "highlights", "comment", "plain"],
    )
    def test_an_index_that_differs(self, index: Index, columns: list[str]) -> None:
        for rendering in (_INDEXES_3X, _INDEXES_2X):
            assert not search.index_matches(index, columns, parse_define_index(rendering["fts_article_title"]))

    def test_unique_and_comment(self) -> None:
        stored = parse_define_index(_INDEXES_3X["fts_article_slug"])
        assert search.index_matches(Index("fts_article_slug", "slug", unique=True, comment="it's unique"), ["slug"], stored)
        assert not search.index_matches(Index("fts_article_slug", "slug", comment="it's unique"), ["slug"], stored)
        assert not search.index_matches(Index("fts_article_slug", "slug", unique=True), ["slug"], stored)
        assert not search.index_matches(Index("fts_article_slug", "slug", unique=True, comment="its unique"), ["slug"], stored)
        plain = parse_define_index('DEFINE INDEX fts_article_slug ON fts_article FIELDS slug COMMENT "it\'s unique"')
        assert not search.index_matches(Index("fts_article_slug", "slug", unique=True, comment="it's unique"), ["slug"], plain)

    def test_the_fields_in_order(self) -> None:
        stored = parse_define_index(_INDEXES_3X["fts_ab"])
        assert search.index_matches(Index("fts_ab", ["a", "b.c"]), ["a", "b.c"], stored)
        assert not search.index_matches(Index("fts_ab", ["b.c", "a"]), ["b.c", "a"], stored)

    @pytest.mark.parametrize("flag", ["hnsw", "mtree", "count"])
    def test_a_vector_or_count_index_never_matches(self, flag: str) -> None:
        index = Index("fts_ab", ["a", "b.c"])
        assert search.index_matches(index, ["a", "b.c"], IndexState("fts_ab", ("a", "b.c")))
        assert not search.index_matches(index, ["a", "b.c"], IndexState("fts_ab", ("a", "b.c"), **{flag: True}))

    def test_no_index_stored(self) -> None:
        assert not search.index_matches(_article_index("fts_article_title"), ["title"], None)


# --- the reads (a fake client) --------------------------------------------------------------------------------------

#: A record user's refusal, as the servers word it.
_NOT_ENOUGH_PERMISSIONS = "IAM error: Not enough permissions to perform this action"


class _InfoClient:
    """A client answering every statement with *answer*, or raising *error*; it records each statement."""

    def __init__(self, answer: object = None, *, error: BaseException | None = None) -> None:
        self.answer = answer
        self.error = error
        self.queries: list[str] = []

    async def query(self, statement: str, variables: object) -> object:
        assert variables == {}
        self.queries.append(statement)
        if self.error is not None:
            raise self.error
        return self.answer


def _db_answer(analyzers: object, tables: object) -> dict[str, object]:
    """``INFO FOR DB``'s answer holding *analyzers* and *tables*."""
    return {"accesses": {}, "analyzers": analyzers, "functions": {}, "params": {}, "tables": tables, "users": {}}


def _table_answer(indexes: object) -> dict[str, object]:
    """``INFO FOR TABLE``'s answer holding *indexes*."""
    return {"events": {}, "fields": {}, "indexes": indexes, "lives": {}, "tables": {}}


_REFUSALS = [
    (InternalError("Internal", _NOT_ENOUGH_PERMISSIONS), SurrealDbAuthenticationError),
    (NotAllowedError("NotAllowed", _NOT_ENOUGH_PERMISSIONS), SurrealDbAuthenticationError),
    (InternalError("Internal", "There was a problem with the database"), SurrealDbError),
    (RuntimeError("the engine is closed"), SurrealDbError),
]
_REFUSAL_IDS = ["permissions-internal", "permissions-not-allowed", "internal", "runtime"]


class TestReads:
    """``read_database_info()`` and ``read_table_indexes()``: one ``INFO`` each, the definitions parsed and keyed by
    name, ``None`` for one the parser cannot read; a refused read raises the DDL's refusal, naming its subject."""

    @pytest.mark.parametrize("rendering", [_ANALYZERS_3X, _ANALYZERS_2X], ids=["3x", "2x"])
    async def test_the_database_info(self, rendering: dict[str, str]) -> None:
        client = _InfoClient(_db_answer(rendering, {"fts_t": "DEFINE TABLE fts_t", "fts_article": "DEFINE TABLE fts_article"}))
        analyzers, tables = await search.read_database_info(client, what="index", kind="definition", subject="table 'fts_t'")
        assert analyzers == {name: parse_define_analyzer(statement) for name, statement in rendering.items()}
        assert tables == ("fts_article", "fts_t")
        assert client.queries == ["INFO FOR DB;"]

    async def test_an_unreadable_analyzer_is_none_under_its_name(self) -> None:
        stored = {"fts_english": _ANALYZERS_3X["fts_english"], "fts_bad": "DEFINE ANALYZER fts_bad FILTERS", "fts_odd": 3}
        client = _InfoClient(_db_answer(stored, {}))
        analyzers, tables = await search.read_database_info(client, what="index", kind="definition", subject="table 'fts_t'")
        assert analyzers == {
            "fts_english": parse_define_analyzer(_ANALYZERS_3X["fts_english"]),
            "fts_bad": None,
            "fts_odd": None,
        }
        assert tables == ()

    @pytest.mark.parametrize(
        "answer",
        [_db_answer({}, {}), _db_answer(None, None), _db_answer([], []), {"functions": {}}, [], None],
        ids=["empty", "none", "lists", "no-sections", "a-list", "nothing"],
    )
    async def test_no_analyzer_and_no_table(self, answer: object) -> None:
        client = _InfoClient(answer)
        assert await search.read_database_info(client, what="index", kind="definition", subject="table 'fts_t'") == ({}, ())

    @pytest.mark.parametrize("rendering", [_INDEXES_3X, _INDEXES_2X], ids=["3x", "2x"])
    async def test_the_table_indexes(self, rendering: dict[str, str]) -> None:
        client = _InfoClient(_table_answer(rendering))
        indexes = await search.read_table_indexes(client, "fts_t", what="index", kind="definition")
        assert indexes == {name: parse_define_index(statement) for name, statement in rendering.items()}
        assert client.queries == ["INFO FOR TABLE `fts_t`;"]

    async def test_an_unreadable_index_is_none_under_its_name(self) -> None:
        stored = {
            "fts_ab": _INDEXES_3X["fts_ab"],
            "fts_bad": "DEFINE INDEX fts_bad ON fts_t",
            "fts_odd": None,
        }
        indexes = await search.read_table_indexes(_InfoClient(_table_answer(stored)), "fts_t", what="index", kind="definition")
        assert indexes == {"fts_ab": parse_define_index(_INDEXES_3X["fts_ab"]), "fts_bad": None, "fts_odd": None}

    @pytest.mark.parametrize(
        "answer",
        [_table_answer({}), _table_answer(None), {"fields": {}}, [], None],
        ids=["empty", "none", "no-section", "a-list", "nothing"],
    )
    async def test_no_index(self, answer: object) -> None:
        assert await search.read_table_indexes(_InfoClient(answer), "fts_t", what="index", kind="definition") == {}

    @pytest.mark.parametrize(("error", "refusal"), _REFUSALS, ids=_REFUSAL_IDS)
    async def test_a_refused_database_info(self, error: BaseException, refusal: type[SurrealDbError]) -> None:
        client = _InfoClient(error=error)
        with pytest.raises(SurrealDbError) as raised:
            await search.read_database_info(client, what="analyzer", kind="removal", subject="'fts_english'")
        assert type(raised.value) is refusal
        assert str(raised.value) == f"Can't apply analyzer removal: 'fts_english' -> {error}"
        assert raised.value.__cause__ is error
        assert client.queries == ["INFO FOR DB;"]

    @pytest.mark.parametrize(("error", "refusal"), _REFUSALS, ids=_REFUSAL_IDS)
    async def test_a_refused_table_read(self, error: BaseException, refusal: type[SurrealDbError]) -> None:
        client = _InfoClient(error=error)
        with pytest.raises(SurrealDbError) as raised:
            await search.read_table_indexes(client, "fts_t", what="index", kind="rebuild")
        assert type(raised.value) is refusal
        assert str(raised.value) == f"Can't apply index rebuild: table 'fts_t' -> {error}"
        assert raised.value.__cause__ is error
        assert client.queries == ["INFO FOR TABLE `fts_t`;"]

    @pytest.mark.parametrize(("table", "error"), [("a-b", ValueError), (3, TypeError)], ids=repr)
    async def test_the_table_name_is_checked(self, table: Any, error: type[Exception]) -> None:
        client = _InfoClient(_table_answer({}))
        with pytest.raises(error):
            await search.read_table_indexes(client, table, what="index", kind="definition")
        assert client.queries == []


# --- what define_indexes() applies (pure) ---------------------------------------------------------------------------

#: ``fts_english`` as SurrealDB 3.x stores it, parsed.
_STORED_ENGLISH = parse_define_analyzer(_ANALYZERS_3X["fts_english"])

#: Another definition of ``fts_english``.
_PLAIN_ENGLISH = Analyzer("fts_english", ["blank"], ["ascii"])


class TestPlans:
    """The pure steps of ``define_indexes()``: the analyzers to define or redefine, the index definitions and rebuilds
    a table needs, and the refusals before any DDL."""

    def test_the_analyzers_to_define_and_to_redefine(self) -> None:
        fts_fn, grams = _a3(), _a5()
        stored = {
            "fts_english": _STORED_ENGLISH,
            "fts_fn": parse_define_analyzer(_ANALYZERS_3X["fts_english"]),
            "fts_odd": None,
        }
        odd = Analyzer("fts_odd", ["blank"])
        analyzers = [FTS_ENGLISH, fts_fn, grams, odd]
        # fts_english equal, fts_fn different, fts_grams missing, fts_odd unreadable.
        assert search.plan_analyzers(analyzers, stored, True) == ([grams], [fts_fn, odd])
        assert search.plan_analyzers(analyzers, stored, False) == ([grams], [])
        assert search.plan_analyzers([_PLAIN_ENGLISH], stored, True) == ([], [_PLAIN_ENGLISH])
        assert search.plan_analyzers([], stored, True) == ([], [])

    def test_the_index_definitions_and_rebuilds(self) -> None:
        indexes = FtsArticle.get_indexes()
        title, body, slug = indexes[0], indexes[1], indexes[2]
        stored: dict[str, IndexState | None] = {
            "fts_article_title": parse_define_index(
                "DEFINE INDEX fts_article_title ON fts_article FIELDS title FULLTEXT ANALYZER fts_english BM25(1.2,0.75) HIGHLIGHTS"
            ),
            "fts_article_body": parse_define_index(
                "DEFINE INDEX fts_article_body ON fts_article FIELDS body FULLTEXT ANALYZER fts_english BM25(2,1)"
            ),
            "fts_article_slug": None,
        }
        statements = dict(zip(_ARTICLE_INDEXES, _I1_INDEXES, strict=True))

        def plan(overwrite: bool, redefined: set[str]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
            return search.plan_indexes(
                [title, body, slug, indexes[3]],
                "fts_article",
                stored,
                column=FtsArticle.to_db_field,
                overwrite=overwrite,
                keyword="FULLTEXT",
                redefined=redefined,
            )

        # title equal, body different, slug unreadable, fts_article_head missing.
        changed = [(name, statements[name]) for name in ("fts_article_body", "fts_article_slug", "fts_article_head")]
        assert plan(True, set()) == (changed, [])
        assert plan(True, {"fts_english"}) == (
            changed,
            [("fts_article_title", "REBUILD INDEX `fts_article_title` ON `fts_article`;")],
        )
        missing = statements["fts_article_head"].replace(" OVERWRITE ", " IF NOT EXISTS ")
        assert plan(False, set()) == ([("fts_article_head", missing)], [])
        assert plan(False, {"other"}) == ([("fts_article_head", missing)], [])

    def test_an_analyzer_named_by_str(self) -> None:
        index = Index("fts_x", "x", search_analyzer="fts_none")
        with pytest.raises(SurrealDbError) as raised:
            search.check_named_analyzers([_text("fts_t"), index], [FTS_ENGLISH], {}, table="fts_doc")
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == (
            "Can't apply index definition: 'fts_x' on table 'fts_doc' -> the analyzer 'fts_none' does not exist; pass "
            "search_analyzer=Analyzer(...) or define it with SurrealDBConnectionManager.define_analyzer() first."
        )
        named = Index("fts_y", "y", search_analyzer="fts_english")
        # Stored (even unreadable), or one of the model's own Analyzer objects: it exists.
        search.check_named_analyzers([named], [], {"fts_english": None}, table="fts_doc")
        search.check_named_analyzers([named], [FTS_ENGLISH], {}, table="fts_doc")
        search.check_named_analyzers([Index("fts_p", "p")], [], {}, table="fts_doc")

    def test_the_texts(self) -> None:
        assert search.transaction_analyzer_refused("FtsArticle", "fts_english") == (
            "FtsArticle.define_indexes(tx=…) cannot define analyzer 'fts_english' in a transaction: SurrealDB builds an "
            "index with the analyzers committed before the transaction began. Call "
            "SurrealDBConnectionManager.define_analyzer() without tx= first, then define_indexes(tx=…)."
        )
        assert search.index_subject("fts_article_title", "fts_article") == "'fts_article_title' on table 'fts_article'"
        assert search.checked_overwrite(True) is True
        assert search.checked_overwrite(False) is False
        assert _raised(TypeError, search.checked_overwrite, 1) == "overwrite= must be a bool, got int."
        assert _raised(TypeError, search.checked_overwrite, None) == "overwrite= must be a bool, got NoneType."


# --- the indexes a changed analyzer rebuilds; define_analyzer() and remove_analyzer() (no server) -------------------


class _TablesClient:
    """A client answering each statement from *answers* (``{}`` for any other), or raising *error*; it records each
    statement."""

    def __init__(self, answers: dict[str, object], *, error: BaseException | None = None) -> None:
        self.answers = answers
        self.error = error
        self.queries: list[str] = []

    async def query(self, statement: str, variables: object) -> object:
        assert variables == {}
        self.queries.append(statement)
        if self.error is not None:
            raise self.error
        return self.answers.get(statement, {})


#: Indexes of a table ``fts_t`` as SurrealDB 3.x renders them: two holding ``fts_english``, one ``fts_plain``, a plain
#: one, and one the parser cannot read.
_USING_STORED: dict[str, IndexState | None] = {
    "fts_k": parse_define_index(_INDEXES_3X["fts_k"]),
    "fts_k2": parse_define_index(_INDEXES_3X["fts_k2"]),
    "fts_ab": parse_define_index(_INDEXES_3X["fts_ab"]),
    "fts_p": parse_define_index("DEFINE INDEX fts_p ON fts_t FIELDS p FULLTEXT ANALYZER fts_plain BM25(1.2,0.75)"),
    "fts_odd": None,
}

#: An analyzer that keeps the case: a search for ``physics`` misses ``'Physics Rocks'`` — exactly ``["ascii"]``, no
#: ``lowercase`` and no ``snowball(…)``, which lower-cases too.
FTS_PLAIN = Analyzer("fts_plain", ["class", "blank"], ["ascii"])

#: The same analyzer lower-casing: once an index using it is rebuilt, ``physics`` finds ``'Physics Rocks'``.
FTS_PLAIN_LOWER = Analyzer("fts_plain", ["class", "blank"], ["lowercase", "ascii"])

_DA2 = (
    "define_analyzer(tx=…) cannot redefine analyzer 'fts_plain' in a transaction: the indexes that use it "
    "(fts_post.fts_post_title) must be rebuilt once the new definition is committed. Call define_analyzer() without "
    "tx=."
)
_RA1 = (
    "Can't apply analyzer removal: 'fts_english' -> The analyzer 'fts_english' is in use by index 'fts_article_title' "
    "on table 'fts_article' and cannot be removed"
)


def _parameter(name: str, annotation: object, default: object = inspect.Parameter.empty) -> inspect.Parameter:
    """A parameter of ``define_analyzer()``/``remove_analyzer()``: the first positional, the others keyword-only."""
    kind = inspect.Parameter.POSITIONAL_OR_KEYWORD if default is inspect.Parameter.empty else inspect.Parameter.KEYWORD_ONLY
    return inspect.Parameter(name, kind, default=default, annotation=annotation)


class TestAnalyzerScan:
    """The steps a changed analyzer adds: the indexes of the database that use it (one ``INFO FOR TABLE`` per table,
    sorted by table then index), the warning of each redefinition, and the texts of ``define_analyzer()`` and
    ``remove_analyzer()``."""

    def test_the_stored_indexes_using_an_analyzer(self) -> None:
        assert search.stored_indexes_using("fts_t", _USING_STORED, {"fts_english"}) == [
            ("fts_t", "fts_k", "fts_english"),
            ("fts_t", "fts_k2", "fts_english"),
        ]
        assert search.stored_indexes_using("fts_t", _USING_STORED, {"fts_english", "fts_plain"}, skip={"fts_k"}) == [
            ("fts_t", "fts_k2", "fts_english"),
            ("fts_t", "fts_p", "fts_plain"),
        ]
        assert search.stored_indexes_using("fts_t", _USING_STORED, set()) == []
        assert search.stored_indexes_using("fts_t", {}, {"fts_english"}) == []

    async def test_indexes_using_reads_each_table_once(self) -> None:
        """The tables come from ``INFO FOR DB``: a name SurrealDB stores in backticks is read as it is."""
        client = _TablesClient(
            {
                "INFO FOR TABLE `fts_t`;": _table_answer({name: _INDEXES_3X[name] for name in ("fts_k", "fts_ab")}),
                "INFO FOR TABLE `fts-u`;": _table_answer(
                    {"fts_u": "DEFINE INDEX fts_u ON `fts-u` FIELDS u FULLTEXT ANALYZER fts_english BM25(1.2,0.75)"}
                ),
                "INFO FOR TABLE `fts_a`;": _table_answer({"fts_z": _index_2x(_INDEXES_3X["fts_k"])}),
            }
        )
        found = await search.indexes_using(
            client, ["fts_t", "fts-u", "fts_a", "fts_none"], {"fts_english"}, what="analyzer", kind="definition"
        )
        assert found == [
            ("fts-u", "fts_u", "fts_english"),
            ("fts_a", "fts_z", "fts_english"),
            ("fts_t", "fts_k", "fts_english"),
        ]
        assert client.queries == [
            "INFO FOR TABLE `fts_t`;",
            "INFO FOR TABLE `fts-u`;",
            "INFO FOR TABLE `fts_a`;",
            "INFO FOR TABLE `fts_none`;",
        ]
        assert await search.indexes_using(client, [], {"fts_english"}, what="analyzer", kind="definition") == []

    @pytest.mark.parametrize(("error", "refusal"), _REFUSALS, ids=_REFUSAL_IDS)
    async def test_a_refused_read(self, error: BaseException, refusal: type[SurrealDbError]) -> None:
        client = _TablesClient({}, error=error)
        with pytest.raises(SurrealDbError) as raised:
            await search.indexes_using(client, ["fts_t"], {"fts_english"}, what="analyzer", kind="removal")
        assert type(raised.value) is refusal
        assert str(raised.value).startswith("Can't apply analyzer removal: table 'fts_t' -> ")

    def test_the_warning_of_each_redefinition(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger="surreal_orm_lite.search")
        rebuilt = [("fts_page", "fts_page_title", "fts_plain"), ("fts_t", "fts_k", "fts_english")]
        search.warn_redefined([FTS_PLAIN, FTS_ENGLISH, _a3()], [*rebuilt, ("fts_post", "fts_post_title", "fts_plain")])
        search.warn_redefined([], rebuilt)
        records = [record for record in caplog.records if record.name == "surreal_orm_lite.search"]
        assert [record.levelno for record in records] == [logging.WARNING] * 3
        assert [record.getMessage() for record in records] == [
            "Analyzer 'fts_plain' redefined; 2 index(es) rebuilt: fts_page.fts_page_title, fts_post.fts_post_title",
            "Analyzer 'fts_english' redefined; 1 index(es) rebuilt: fts_t.fts_k",
            "Analyzer 'fts_fn' redefined; 0 index(es) rebuilt: ",
        ]

    def test_the_texts(self) -> None:
        assert search.checked_analyzer(FTS_PLAIN) is FTS_PLAIN
        assert _raised(TypeError, search.checked_analyzer, "fts_plain") == "define_analyzer() takes an Analyzer, got str."
        assert _raised(TypeError, search.checked_analyzer, None) == "define_analyzer() takes an Analyzer, got NoneType."
        assert search.analyzer_redefinition_refused("fts_plain", [("fts_post", "fts_post_title", "fts_plain")]) == _DA2
        both = [("fts_page", "fts_page_title", "fts_plain"), ("fts_post", "fts_post_title", "fts_plain")]
        assert search.analyzer_redefinition_refused("fts_plain", both) == _DA2.replace(
            "(fts_post.fts_post_title)", "(fts_page.fts_page_title, fts_post.fts_post_title)"
        )
        assert search.analyzer_in_use_refused("fts_english", "fts_article_title", "fts_article") == _RA1


class TestAnalyzerManagerApi:
    """``SurrealDBConnectionManager.define_analyzer()`` and ``remove_analyzer()``: coroutines on the current connection,
    their signatures pinned. ``remove_analyzer()`` takes ``analyzer``, never ``name``, as ``remove_event()`` takes
    ``event``: the manager reads a first ``name`` parameter as a connection name."""

    def test_the_signatures(self) -> None:
        tx = _parameter("tx", Transaction | None, None)
        assert inspect.signature(SurrealDBConnectionManager.define_analyzer) == inspect.Signature(
            [_parameter("analyzer", Analyzer), _parameter("overwrite", bool, True), tx], return_annotation=list[str]
        )
        assert inspect.signature(SurrealDBConnectionManager.remove_analyzer) == inspect.Signature(
            [_parameter("analyzer", str), _parameter("missing_ok", bool, True), tx], return_annotation=str
        )

    @pytest.mark.parametrize("method", ["define_analyzer", "remove_analyzer"])
    def test_routed_to_the_current_connection(self, method: str) -> None:
        function = vars(SurrealDBConnectionManager)[method].__func__
        assert function.__surreal_routed__ == "current"
        assert inspect.iscoroutinefunction(getattr(SurrealDBConnectionManager, method)) is True
        default = SurrealDBConnectionManager._manager_for("default")
        assert inspect.signature(getattr(default, method)) == inspect.signature(getattr(SurrealDBConnectionManager, method))
        assert "name" not in inspect.signature(inspect.unwrap(function)).parameters

    def test_define_analyzer_drops_the_cache_through_its_body(self) -> None:
        """The public method checks its transaction and arguments first; the body it calls drops every cached read."""
        assert not hasattr(vars(SurrealDBConnectionManager)["define_analyzer"].__func__, "__surreal_cache_writes__")
        body = vars(SurrealDBConnectionManager)["_define_analyzer"].__func__
        assert body.__surreal_cache_writes__ is True
        assert body.__surreal_routed__ == "current"
        assert not hasattr(vars(SurrealDBConnectionManager)["remove_analyzer"].__func__, "__surreal_cache_writes__")


# --- search(): its statements and its refusals ----------------------------------------------------------------------

#: A search text no error the ORM raises may show: 3.x quotes the bound search text in its missing-index error.
_SECRET = "secret words"

#: SurrealDB 3.3's missing-index error for a search of ``slug``, which has no full-text index.
_NO_INDEX_3X = f"There was no suitable index supporting the expression: slug @0@ '{_SECRET}'"

_S2 = (
    "Invalid search field name '{}': must contain only alphanumeric characters, underscores, and dots (for nested "
    "fields), and start with a letter or underscore"
)
_S4 = "search() takes at most 256 fields per queryset (SurrealDB's match references are 0 to 255)."
_TT1 = (
    "search() cannot be combined with as_of(): a full-text match reads the index, which holds the records' current "
    "text, so a time-travel read would select records by their text now. Run the search without as_of()."
)
_LV1 = (
    "A live query cannot honour search(): SurrealDB accepts a LIVE SELECT whose condition holds a full-text match but "
    "never notifies it. Search in a subquery instead — filter(id__in=Subquery(Model.objects().search(...))) — which "
    "notifies the records created or updated to match, not their deletion."
)
_CH1 = (
    "A change feed cannot honour {}: SurrealDB's SHOW CHANGES reads the whole table's log, with no WHERE, projection, "
    "ordering or paging, outside any transaction. Call changes() on Model.objects() and filter the events."
)
_SI1_ADVICE = (
    "SurrealDB matches only a field with a FULLTEXT (3.x) or SEARCH (2.x) index: declare Index(..., search_analyzer=...) "
    "on the model and apply define_indexes()."
)


def _si1(field: str) -> str:
    """The refusal of a search on *field*, which has no full-text index."""
    return f"search(): no full-text index on field {field!r}. {_SI1_ADVICE}"


#: Q7's queryset: the record ids of the articles whose title matches "quantum", as a subquery.
def _q7() -> Any:
    return FtsArticle.objects().filter(id__in=Subquery(FtsArticle.objects().search(title="quantum")))


_Q7 = "{ LET $_sq0 = (SELECT VALUE id FROM fts_article WHERE title @0@ $_f0); (SELECT * FROM fts_article WHERE id IN $_sq0) };"
_Q8 = "LIVE SELECT * FROM fts_article WHERE id IN (SELECT VALUE id FROM fts_article WHERE title @0@ 'quantum');"
_Q10 = "RETURN [(SELECT VALUE id FROM fts_article LIMIT 1), (SELECT VALUE id FROM fts_article WHERE title @0@ '' LIMIT 1)];"

#: A moment for ``as_of()``.
_MOMENT = datetime(2026, 1, 1, tzinfo=UTC)


async def _forbidden_client(*args: Any, **kwargs: Any) -> Any:
    pytest.fail("a client was acquired")


@pytest.fixture
def _no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """No client may be acquired: a refusal comes before any request."""
    monkeypatch.setattr(SurrealDBConnectionManager, "get_client", _forbidden_client)


class TestSearchCompile:
    """``search()`` compiles one ``<column> @<ref>@ $_f<N>`` condition per field, AND-ed after the filters, its text
    bound; each refusal comes before any request and leaves the queryset unchanged."""

    def test_q1(self) -> None:
        assert FtsArticle.objects().search(title="quantum physics")._compile_query() == (
            "SELECT * FROM fts_article WHERE title @0@ $_f0;",
            {"_f0": "quantum physics"},
        )

    def test_q2_after_the_filters(self) -> None:
        compiled = FtsArticle.objects().filter(views__gt=10).search(title="quantum", body="physics")._compile_query()
        assert compiled == (
            "SELECT * FROM fts_article WHERE views > $_f0 AND title @0@ $_f1 AND body @1@ $_f2;",
            {"_f0": 10, "_f1": "quantum", "_f2": "physics"},
        )

    def test_q4_an_aliased_field_compiles_its_column(self) -> None:
        assert FtsArticle.objects().search(heading="intro")._compile_query()[0] == (
            "SELECT * FROM fts_article WHERE head_text @0@ $_f0;"
        )

    def test_q5_values_and_annotate(self) -> None:
        queryset = FtsArticle.objects().search(title="quantum").values("views").annotate(n=Count())
        assert queryset._compile_group_by_query() == (
            "SELECT views, count() AS n FROM fts_article WHERE title @0@ $_f0 GROUP BY views;",
            {"_f0": "quantum"},
        )

    def test_q6_an_aggregation(self) -> None:
        assert FtsArticle.objects().search(title="quantum")._compile_aggregation_query("count()") == (
            "SELECT count() FROM fts_article WHERE title @0@ $_f0 GROUP ALL;",
            {"_f0": "quantum"},
        )

    def test_q7_a_subquery(self) -> None:
        assert _q7()._compile_query() == (_Q7, {"_f0": "quantum"})

    def test_q8_a_live_query_inlines_the_text(self) -> None:
        assert _q7()._compile_live(False) == _Q8

    def test_after_q_and_nearby(self) -> None:
        queryset = FtsArticle.objects().search(title="x").filter(Q(views=1) | Q(views=2)).nearby("meta.at", (1.0, 2.0), 10)
        query, variables = queryset._compile_query()
        assert query == (
            "SELECT * FROM fts_article WHERE (views = $_f0 OR views = $_f1) AND (meta.at.is_point() AND "
            "geo::distance(meta.at, $_f2) <= $_f3) AND title @0@ $_f4;"
        )
        assert variables["_f4"] == "x"

    def test_the_references_follow_the_calls(self) -> None:
        queryset = FtsArticle.objects().search(title="a").search(body="b")
        assert queryset._compile_query() == (
            "SELECT * FROM fts_article WHERE title @0@ $_f0 AND body @1@ $_f1;",
            {"_f0": "a", "_f1": "b"},
        )

    def test_the_same_field_twice(self) -> None:
        assert FtsArticle.objects().search(title="a").search(title="b")._compile_query()[0] == (
            "SELECT * FROM fts_article WHERE title @0@ $_f0 AND title @1@ $_f1;"
        )

    def test_a_dotted_path_and_an_array_field(self) -> None:
        assert FtsArticle.objects().search(**{"meta.headline": "rust"}, tags="quantum")._compile_query()[0] == (
            "SELECT * FROM fts_article WHERE meta.headline @0@ $_f0 AND tags @1@ $_f1;"
        )

    def test_a_subquerys_references_start_at_0_and_its_texts_continue_the_numbering(self) -> None:
        queryset = (
            FtsArticle.objects()
            .search(body="kitchen")
            .filter(id__in=Subquery(FtsArticle.objects().search(title="physics", slug="a2")))
        )
        assert queryset._compile_query() == (
            "{ LET $_sq0 = (SELECT VALUE id FROM fts_article WHERE title @0@ $_f0 AND slug @1@ $_f1); "
            "(SELECT * FROM fts_article WHERE id IN $_sq0 AND body @0@ $_f2) };",
            {"_f0": "physics", "_f1": "a2", "_f2": "kitchen"},
        )

    def test_no_keyword_changes_nothing(self) -> None:
        queryset = FtsArticle.objects().filter(views__gt=1)
        before = queryset._compile_query()
        assert queryset.search() is queryset
        assert queryset._compile_query() == before

    def test_a_text_is_data(self, recwarn: pytest.WarningsRecorder) -> None:
        queryset = FtsArticle.objects().variables(title="x").search(title="$title")
        assert queryset._compile_query() == (
            "SELECT * FROM fts_article WHERE title @0@ $_f0;",
            {"title": "x", "_f0": "$title"},
        )
        assert len(recwarn) == 0

    def test_a_subquery_records_the_fields_it_searches(self) -> None:
        inner = FtsArticle.objects().search(title="quantum", heading="intro")
        queryset = FtsArticle.objects().filter(id__in=Subquery(inner))
        compiler = queryset._subquery_compiler()
        queryset._compile_query(subqueries=compiler)
        assert compiler.searched == {("fts_article", "title"): "title", ("fts_article", "head_text"): "heading"}
        plain = FtsArticle.objects().filter(id__in=Subquery(FtsArticle.objects().filter(views=1)))
        compiler = plain._subquery_compiler()
        plain._compile_query(subqueries=compiler)
        assert compiler.searched == {}

    @pytest.mark.parametrize(("text", "kind"), [(3, "int"), (None, "NoneType"), (b"x", "bytes")])
    def test_s1_a_text_that_is_not_a_str(self, text: object, kind: str) -> None:
        queryset = FtsArticle.objects().search(body="x")
        before = queryset._compile_query()
        assert _raised(TypeError, queryset.search, title=text) == f"search() takes a str to match in 'title', got {kind}."
        assert queryset._compile_query() == before

    @pytest.mark.parametrize("field", ["a b", "title\n", "meta..headline", "1title", "title;"])
    def test_s2_a_field_that_is_not_a_name(self, field: str) -> None:
        queryset = FtsArticle.objects().search(body="x")
        before = queryset._compile_query()
        assert _raised(ValueError, queryset.search, **{field: "x"}) == _S2.format(field)
        assert queryset._compile_query() == before

    def test_s2_is_validate_field_names_text(self) -> None:
        assert _raised(ValueError, FtsArticle.objects().search, **{"a b": "x"}) == _raised(
            ValueError, validate_field_name, "a b", "search field"
        )
        assert _raised(ValueError, FtsArticle.objects().search, **{"": "x"}) == "search field name cannot be empty"

    def test_s3_the_record_id(self) -> None:
        queryset = FtsArticle.objects()
        assert _raised(ValueError, queryset.search, id="a1") == (
            "search() matches text: 'id' is the record id, not a text field."
        )
        assert queryset._search == []

    @pytest.mark.parametrize("field", ["in_", "in", "out"])
    def test_s3_an_edge_endpoint(self, field: str) -> None:
        assert _raised(ValueError, FtsCites.objects().search, **{field: "x"}) == (
            f"search() matches text: {field!r} is an edge endpoint, not a text field."
        )
        assert FtsCites.objects().search(note="x")._compile_query()[0] == "SELECT * FROM fts_cites WHERE note @0@ $_f0;"

    def test_s4_at_most_256_fields_across_calls(self) -> None:
        queryset = FtsArticle.objects().search(**{f"f{i}": "x" for i in range(200)})
        before = queryset._compile_query()
        assert _raised(ValueError, queryset.search, **{f"g{i}": "x" for i in range(57)}) == _S4
        assert queryset._compile_query() == before
        queryset.search(**{f"g{i}": "x" for i in range(56)})
        query, variables = queryset._compile_query()
        assert re.findall(r"@(\d+)@", query) == [str(reference) for reference in range(256)]
        assert len(variables) == 256
        assert _raised(ValueError, queryset.search, title="x") == _S4

    def test_a_refusal_records_no_link(self) -> None:
        queryset = FtsArticle.objects()
        _raised(TypeError, queryset.search, **{"meta.headline": 3})
        assert queryset._link_heads == set()
        queryset.search(**{"meta.headline": "rust"})
        assert queryset._link_heads == {"meta"}


@pytest.mark.usefixtures("_no_client")
class TestSearchRefusals:
    """``search()`` with ``as_of()`` (either way round, and in a subquery under the outer one), in a live query or a
    change feed: each refused before any request."""

    def test_tt1_search_after_as_of(self) -> None:
        queryset = FtsArticle.objects().as_of(_MOMENT)
        assert _raised(SurrealDbError, queryset.search, title="x") == _TT1
        assert queryset._search == []
        assert queryset.search() is queryset

    @pytest.mark.parametrize("moment", [_MOMENT, timedelta(days=1), "2026-01-01T00:00:00Z"])
    def test_tt1_as_of_after_search(self, moment: Any) -> None:
        queryset = FtsArticle.objects().search(title="x")
        assert _raised(SurrealDbError, queryset.as_of, moment) == _TT1
        assert queryset._as_of is None
        assert queryset.as_of(None) is queryset
        assert queryset._compile_query() == ("SELECT * FROM fts_article WHERE title @0@ $_f0;", {"_f0": "x"})

    def test_tt1_a_subquery_under_the_outer_as_of(self) -> None:
        queryset = FtsArticle.objects().as_of(_MOMENT).filter(id__in=Subquery(FtsArticle.objects().search(title="x")))
        assert _raised(SurrealDbError, queryset._compile_query, _MOMENT) == _TT1
        assert _raised(SurrealDbError, queryset._compile_aggregation_query, "count()", moment=_MOMENT) == _TT1

    @pytest.mark.parametrize("method", ["live", "watch"])
    def test_lv1(self, method: str) -> None:
        assert _raised(SurrealDbError, getattr(FtsArticle.objects().search(title="x"), method)) == _LV1

    def test_lv1_comes_before_the_other_clauses(self) -> None:
        queryset = FtsArticle.objects().search(title="x").order_by("views").limit(3)
        assert _raised(SurrealDbError, queryset.live) == _LV1

    def test_a_subquery_that_searches_is_allowed_in_a_live_query(self) -> None:
        assert _q7()._compile_live(True).startswith("LIVE SELECT DIFF FROM fts_article WHERE id IN (SELECT VALUE id ")

    def test_ch1(self) -> None:
        assert _raised(SurrealDbError, FtsArticle.objects().search(title="x").changes) == _CH1.format("search()")
        both = FtsArticle.objects().filter(views=1).search(title="x").nearby("meta", (0.0, 0.0), 1.0)
        assert _raised(SurrealDbError, both.changes) == _CH1.format("filter(), nearby(), search()")


class TestMissingIndexTexts:
    """The ORM's error for SurrealDB's refusal of a search on a field without a full-text index: the field named,
    never the server's text."""

    def test_the_column_the_server_names(self) -> None:
        def column(message: str) -> str | None:
            return search.missing_index_column(InternalError("Internal", message))

        assert column(_NO_INDEX_3X) == "slug"
        assert column("There was no suitable index supporting the expression: meta.headline @1@ $_f0") == "meta.headline"
        assert column("There was no suitable index supporting the expression: `meta`.`headline` @1@ $_f0") == ("meta.headline")
        assert column("There was no suitable index supporting the expression: ⟨meta⟩.headline @0@ $_f0") == ("meta.headline")
        assert column(f"There was no suitable index supporting the expression: slug '{_SECRET}'") is None

    def test_the_refusal_names_the_field(self) -> None:
        refusal = search.missing_index_refusal(InternalError("Internal", _NO_INDEX_3X), {"slug": "slug"})
        assert type(refusal) is SurrealDbError
        assert str(refusal) == _si1("slug")
        aliased = InternalError("Internal", "There was no suitable index supporting the expression: head_text @0@ $_f0")
        assert str(search.missing_index_refusal(aliased, {"head_text": "heading"})) == _si1("heading")
        assert str(search.missing_index_refusal(aliased, {})) == _si1("head_text")

    def test_an_unreadable_column_names_every_searched_field(self) -> None:
        error = InternalError("Internal", f"There was no suitable index supporting the expression: slug '{_SECRET}'")
        both = search.missing_index_refusal(error, {"title": "title", "head_text": "heading"})
        assert str(both) == f"search(): no full-text index on one of the fields 'title', 'heading'. {_SI1_ADVICE}"
        assert str(search.missing_index_refusal(error, {"slug": "slug"})) == _si1("slug")
        assert search.missing_index_refusal(error, {}) is None

    def test_any_other_error_is_not_translated(self) -> None:
        assert search.missing_index_refusal(InternalError("Internal", "There was a problem"), {"slug": "slug"}) is None
        assert search.missing_index_refusal(RuntimeError(_NO_INDEX_3X), {"slug": "slug"}) is None
        assert search.is_missing_index_error(InternalError("Internal", _NO_INDEX_3X)) is True
        assert search.is_missing_index_error(SurrealDbError(_NO_INDEX_3X)) is False

    def test_the_live_probe(self) -> None:
        assert search.live_index_probe("fts_article", "meta.headline") == (
            "(SELECT VALUE id FROM fts_article WHERE meta.headline @0@ '' LIMIT 1)"
        )


_LIVE_ID = UUID("00000000-0000-4000-8000-000000000035")


class _LiveClient:
    """A client that records every statement, answers the start check (``RETURN …``) with *check* — raised when it
    is an exception — and a ``LIVE SELECT`` with :data:`_LIVE_ID`."""

    def __init__(self, check: Any = None) -> None:
        self.check = check
        self.calls: list[str] = []

    async def query(self, query: str, variables: dict[str, Any] | None = None) -> Any:
        self.calls.append(query)
        if not query.startswith("RETURN "):
            return _LIVE_ID
        if isinstance(self.check, BaseException):
            raise self.check
        return self.check


class TestLiveIndexCheck:
    """Before a live query whose subquery searches, the ORM checks, in the request that checks the subquery tables,
    that each searched field has a full-text index, and raises ``SurrealDbError`` naming the field otherwise, before
    the live query starts: without the index, SurrealDB 3.x would accept the live query and never notify."""

    async def test_q10_the_check_comes_before_the_live_select(self) -> None:
        client = _LiveClient([[], []])
        assert await _q7()._live_starter(False)(client) == _LIVE_ID
        assert client.calls == [_Q10, _Q8]

    async def test_the_searched_columns_sorted_after_the_tables(self) -> None:
        inner = FtsArticle.objects().search(title="a", heading="b", **{"meta.headline": "c"})
        queryset = FtsArticle.objects().filter(id__in=Subquery(inner), slug__in=Subquery(FtsPlain.objects().select("title")))
        client = _LiveClient([[], [], [], [], []])
        await queryset._live_starter(False)(client)
        assert client.calls[0] == (
            "RETURN [(SELECT VALUE id FROM fts_article LIMIT 1), (SELECT VALUE id FROM fts_plain LIMIT 1), "
            "(SELECT VALUE id FROM fts_article WHERE head_text @0@ '' LIMIT 1), "
            "(SELECT VALUE id FROM fts_article WHERE meta.headline @0@ '' LIMIT 1), "
            "(SELECT VALUE id FROM fts_article WHERE title @0@ '' LIMIT 1)];"
        )

    async def test_a_field_without_an_index_raises_and_starts_nothing(self) -> None:
        error = InternalError("Internal", "There was no suitable index supporting the expression: head_text @0@ ''")
        client = _LiveClient(error)
        queryset = FtsArticle.objects().filter(id__in=Subquery(FtsArticle.objects().search(heading=_SECRET)))
        with pytest.raises(SurrealDbError) as raised:
            await queryset._live_starter(False)(client)
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == _si1("heading")
        assert raised.value.__cause__ is None
        assert raised.value.__suppress_context__
        assert client.calls == [_Q10.replace("title", "head_text")]

    async def test_a_missing_table_is_still_reported(self) -> None:
        details = {"kind": "Table", "details": {"name": "fts_article"}}
        error = NotFoundError("NotFound", "The table 'fts_article' does not exist", details=details)
        with pytest.raises(SurrealDbNotFoundError):
            await _q7()._live_starter(False)(_LiveClient(error))

    async def test_any_other_error_is_relayed(self) -> None:
        error = InternalError("Internal", "There was a problem")
        with pytest.raises(InternalError) as raised:
            await _q7()._live_starter(False)(_LiveClient(error))
        assert raised.value is error

    async def test_every_start_checks_again(self) -> None:
        """A resubscribe runs the starter again on the new connection, the check included."""
        start = _q7()._live_starter(False)
        first, second = _LiveClient([[], []]), _LiveClient([[], []])
        await start(first)
        await start(second)
        assert first.calls == second.calls == [_Q10, _Q8]


# --- a missing full-text index in a transaction's reason ------------------------------------------------------------

#: What the transaction layer keeps of :data:`_NO_INDEX_3X`: the expression cut after its operator.
_NO_INDEX_REASON = "There was no suitable index supporting the expression: slug @0@"


def _shown(exc: BaseException, text: str) -> list[str]:
    """Where *exc* shows *text*: its ``str()``, its ``repr()`` and its formatted traceback (chained errors included)."""
    shown = {"str": str(exc), "repr": repr(exc), "traceback": "".join(traceback.format_exception(exc))}
    return [where for where, value in shown.items() if text in value]


class TestTransactionReason:
    """A failed statement's reason in a transaction names the column a search lacks an index for and the operator,
    never the search text SurrealDB 3.x quotes after them; every other reason is unchanged."""

    def test_the_value_is_cut_after_the_operator(self) -> None:
        assert transaction._server_reason(_NO_INDEX_3X) == (_NO_INDEX_REASON, [])
        dotted = "There was no suitable index supporting the expression: meta.headline @1@ $_f0"
        assert transaction._server_reason(dotted) == (
            "There was no suitable index supporting the expression: meta.headline @1@",
            [],
        )

    def test_an_expression_without_an_operator_is_cut_after_expression(self) -> None:
        message = f"There was no suitable index supporting the expression: slug '{_SECRET}'"
        assert transaction._server_reason(message) == ("There was no suitable index supporting the expression", [])

    def test_a_failed_statement_hides_the_server_text(self) -> None:
        with pytest.raises(SurrealDbError) as raised:
            Transaction.raise_for_status({"result": [{"status": "ERR", "result": _NO_INDEX_3X}]})
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == f"Transaction failed and rolled back: {_NO_INDEX_REASON}"
        assert _shown(raised.value, _SECRET) == []
        assert raised.value.__cause__ is None
        assert raised.value.__suppress_context__
        assert str(raised.value.__context__) == _NO_INDEX_3X

    def test_a_refused_query_hides_the_server_text(self) -> None:
        with pytest.raises(SurrealDbError) as raised:
            Transaction.raise_for_status({"error": {"code": -32000, "message": _NO_INDEX_3X}})
        assert str(raised.value) == f"Transaction failed: the server refused the query: {_NO_INDEX_REASON}"
        assert _shown(raised.value, _SECRET) == []
        assert str(raised.value.__context__) == _NO_INDEX_3X

    def test_the_other_reasons_are_unchanged(self) -> None:
        plain = "Database record `fts_article:a1` already exists"
        assert transaction._server_reason(plain) == (plain, [])
        with pytest.raises(SurrealDbError) as raised:
            Transaction.raise_for_status({"result": [{"status": "ERR", "result": plain}]})
        assert str(raised.value) == f"Transaction failed and rolled back: {plain}"
        assert raised.value.__context__ is None
        assert not raised.value.__suppress_context__
        excerpt = "Parse error: Unexpected token `a strand`, expected Eof\n --> [1:7]\n  |\n1 | SELEC 'x' FRM t;\n"
        assert transaction._server_reason(excerpt) == ("Parse error: Unexpected token `a strand`, expected Eof", [(1, 7)])
        with pytest.raises(SurrealDbError) as parsed:
            Transaction.raise_for_status({"result": [{"status": "ERR", "result": excerpt}]})
        assert str(parsed.value) == "Transaction failed and rolled back: Parse error: Unexpected token `a strand`, expected Eof"
        assert str(parsed.value.__context__) == excerpt


# --- the search annotations in annotate() ---------------------------------------------------------------------------

_Q3 = (
    "SELECT *, search::score(0) AS relevance, search::highlight($_sh0, $_sh1, 0) AS snippet, search::offsets(0) AS spans "
    "FROM fts_article WHERE title @0@ $_f0 ORDER BY relevance DESC;"
)
_PR1 = "annotate(): a {} is computed per record; it cannot be combined with GROUP BY aggregations or values()"
_AT1 = (
    "annotate() argument {!r} must be an Aggregation instance, a GeoDistance, a SearchScore, a SearchHighlight or a "
    "SearchOffsets, got {}"
)
_SR1 = "annotate(): {} names match reference {}, but search() gave this queryset {}."
_SR2 = "annotate(): {} needs search(): SurrealDB scores, highlights and locates the fields search() matches."
_LIVE_ANNOTATE = (
    "A live query cannot honour annotate(): SurrealDB's LIVE SELECT has no ORDER BY, LIMIT or START, streams whole "
    "records, and runs outside any transaction. Drop the clause — filter(), variables() and fetch() are supported."
)
_SUBQUERY_ANNOTATE = (
    "A subquery cannot honour annotate(): it gives the outer filter one list of values, SELECT VALUE <one field>. Drop "
    "the clause; filter(), Q, nearby(), select() of one field, order_by(), limit(), offset() and variables() are "
    "supported."
)

#: A search annotation of each kind, with the noun its alias refusals use.
_SEARCH_ANNOTATIONS = [(SearchScore(0), "score"), (SearchHighlight(), "highlight"), (SearchOffsets(0), "offsets")]


def _q3(text: str = "quantum") -> Any:
    """Q3's queryset: the articles whose title matches *text*, with their score, highlight and offsets, best first."""
    return (
        FtsArticle.objects()
        .search(title=text)
        .annotate(relevance=SearchScore(0), snippet=SearchHighlight("<b>", "</b>", 0), spans=SearchOffsets(0))
        .order_by("-relevance")
    )


class TestAnnotateCompile:
    """``SearchScore``, ``SearchHighlight`` and ``SearchOffsets`` in ``annotate()``: one projection each, after the
    ``GeoDistance`` ones, the highlight tags bound; their aliases, their mixing with groups and their references
    refused before any request, with exact texts."""

    def test_q3(self) -> None:
        assert _q3()._compile_query() == (_Q3, {"_f0": "quantum", "_sh0": "<b>", "_sh1": "</b>"})

    def test_q9_a_reference_of_a_later_call(self) -> None:
        queryset = FtsArticle.objects().search(title="a").search(body="b").annotate(s=SearchScore(1))
        assert queryset._compile_query() == (
            "SELECT *, search::score(1) AS s FROM fts_article WHERE title @0@ $_f0 AND body @1@ $_f1;",
            {"_f0": "a", "_f1": "b"},
        )

    def test_after_the_geo_distances_unchanged(self) -> None:
        queryset = (
            FtsArticle.objects()
            .search(title="a")
            .annotate(s=SearchScore(0), dist=GeoDistance("meta.at", (1.0, 2.0)), h=SearchHighlight("[", "]"))
        )
        query, variables = queryset._compile_query()
        assert query == (
            "SELECT *, IF meta.at.is_point() THEN geo::distance(meta.at, $_gd0) END AS dist, search::score(0) AS s, "
            "search::highlight($_sh0, $_sh1, 0) AS h FROM fts_article WHERE title @0@ $_f0;"
        )
        assert sorted(variables) == ["_f0", "_gd0", "_sh0", "_sh1"]
        assert (variables["_sh0"], variables["_sh1"]) == ("[", "]")

    def test_two_highlights_bind_four_tags(self) -> None:
        queryset = (
            FtsArticle.objects()
            .search(title="a", tags="b")
            .annotate(a=SearchHighlight("<i>", "</i>", 0), b=SearchScore(1), c=SearchHighlight("<em class='x'>", "\\", 1))
        )
        assert queryset._compile_query() == (
            "SELECT *, search::highlight($_sh0, $_sh1, 0) AS a, search::score(1) AS b, search::highlight($_sh2, $_sh3, 1) "
            "AS c FROM fts_article WHERE title @0@ $_f0 AND tags @1@ $_f1;",
            {"_f0": "a", "_f1": "b", "_sh0": "<i>", "_sh1": "</i>", "_sh2": "<em class='x'>", "_sh3": "\\"},
        )

    def test_kept_in_annotations_and_in_the_search_annotations(self) -> None:
        queryset = FtsArticle.objects().annotate(s=SearchScore(0), h=SearchHighlight(ref=1), n=SearchOffsets(2))
        assert queryset._annotations == {"s": SearchScore(0), "h": SearchHighlight(ref=1), "n": SearchOffsets(2)}
        assert queryset._search_annotations == queryset._annotations
        assert queryset._geo_annotations == {}

    @pytest.mark.parametrize("value", ["relevance", 3, None, SearchScore], ids=["str", "int", "none", "class"])
    def test_at1_another_type(self, value: object) -> None:
        queryset = FtsArticle.objects().search(title="a")
        assert _raised(TypeError, queryset.annotate, x=value) == _AT1.format("x", type(value).__name__)
        assert queryset._annotations == {}

    @pytest.mark.parametrize(("annotation", "noun"), _SEARCH_ANNOTATIONS, ids=["score", "highlight", "offsets"])
    def test_al1_an_alias_that_would_shadow(self, annotation: Any, noun: str) -> None:
        kind = type(annotation).__name__
        reasons = {
            "title": f"is a field of FtsArticle: the {noun} would replace its value in the results",
            "head_text": f"is the column of field 'heading': the {noun} would replace its value in the results",
            "heading": f"is a field of FtsArticle: the {noun} would replace its value in the results",
            "save": f"is an attribute of FtsArticle: the {noun} would shadow it on the instances",
            "objects": f"is an attribute of FtsArticle: the {noun} would shadow it on the instances",
            "id": "is reserved: it names the record's id or an edge endpoint",
            "_score": "is reserved: names starting with '_' or 'model_' belong to Pydantic",
            "model_score": "is reserved: names starting with '_' or 'model_' belong to Pydantic",
        }
        for alias, reason in reasons.items():
            queryset = FtsArticle.objects().search(title="a")
            text = _raised(ValueError, queryset.annotate, **{alias: annotation})
            assert text == f"annotate() alias {alias!r} {reason}; choose another alias for the {kind}"
            assert queryset._annotations == {} and queryset._search_annotations == {}

    def test_al1_an_edge_reserves_its_endpoints(self) -> None:
        for alias in ("in", "out"):
            assert _raised(ValueError, FtsCites.objects().annotate, **{alias: SearchScore(0)}) == (
                f"annotate() alias {alias!r} is reserved: it names the record's id or an edge endpoint; choose another "
                "alias for the SearchScore"
            )

    def test_the_geo_distance_texts_are_unchanged(self) -> None:
        assert _raised(ValueError, FtsArticle.objects().annotate, title=GeoDistance("meta.at", (1.0, 2.0))) == (
            "annotate() alias 'title' is a field of FtsArticle: the distance would replace its value in the results; "
            "choose another alias for the GeoDistance"
        )
        mixed = FtsArticle.objects().annotate(dist=GeoDistance("meta.at", (1.0, 2.0)), s=SearchScore(0))
        assert _raised(ValueError, mixed.annotate, n=Count()) == _PR1.format("GeoDistance")

    @pytest.mark.parametrize(("annotation", "noun"), _SEARCH_ANNOTATIONS, ids=["score", "highlight", "offsets"])
    def test_pr1_with_an_aggregation_or_values(self, annotation: Any, noun: str) -> None:
        kind = type(annotation).__name__
        refused = _PR1.format(kind)
        assert _raised(ValueError, FtsArticle.objects().annotate, n=Count(), s=annotation) == refused
        counted = FtsArticle.objects().annotate(n=Count())
        assert _raised(ValueError, counted.annotate, s=annotation) == refused
        assert list(counted._annotations) == ["n"] and counted._search_annotations == {}
        grouped = FtsArticle.objects().search(title="a").values("views")
        assert _raised(ValueError, grouped.annotate, s=annotation) == refused
        assert grouped._annotations == {} and grouped._search_annotations == {}
        annotated = FtsArticle.objects().search(title="a").annotate(s=annotation)
        assert _raised(ValueError, annotated.values, "views") == refused
        assert annotated._group_by_fields == []
        assert _raised(ValueError, annotated.annotate, n=Count()) == refused
        assert list(annotated._annotations) == ["s"]

    def test_pr1_names_the_first_per_record_annotation(self) -> None:
        point = GeoDistance("meta.at", (1.0, 2.0))
        assert _raised(ValueError, FtsArticle.objects().annotate, h=SearchHighlight(), d=point, n=Count()) == (
            _PR1.format("SearchHighlight")
        )
        assert _raised(ValueError, FtsArticle.objects().annotate, d=point, h=SearchHighlight(), n=Count()) == (
            _PR1.format("GeoDistance")
        )

    def test_pr1_in_the_group_by_compiler(self) -> None:
        queryset = FtsArticle.objects().annotate(o=SearchOffsets(0))
        assert _raised(ValueError, queryset._compile_group_by_query) == _PR1.format("SearchOffsets")

    @pytest.mark.usefixtures("_no_client")
    async def test_pr1_from_exec_of_a_grouped_queryset(self) -> None:
        queryset = FtsArticle.objects().search(title="a").annotate(s=SearchScore(0))
        queryset._group_by_fields = ["views"]
        with pytest.raises(ValueError) as raised:
            await queryset.exec()
        assert str(raised.value) == _PR1.format("SearchScore")

    def test_sr1_a_reference_search_did_not_give(self) -> None:
        one = FtsArticle.objects().search(title="a").annotate(h=SearchHighlight("<b>", "</b>", 1))
        assert _raised(ValueError, one._compile_query) == _SR1.format("SearchHighlight('<b>', '</b>', 1)", 1, "reference 0")
        two = FtsArticle.objects().search(title="a", body="b").annotate(s=SearchScore(2))
        assert _raised(ValueError, two._compile_query) == _SR1.format("SearchScore(2)", 2, "references 0 and 1")
        three = FtsArticle.objects().search(title="a", body="b").search(tags="c").annotate(o=SearchOffsets(5))
        assert _raised(ValueError, three._compile_query) == _SR1.format("SearchOffsets(5)", 5, "references 0, 1 and 2")

    def test_sr1_is_checked_when_the_queryset_runs(self) -> None:
        """``annotate()`` may come before ``search()``: the references are checked against the searches of the
        queryset as it runs."""
        queryset = FtsArticle.objects().annotate(s=SearchScore(1)).search(title="a")
        assert _raised(ValueError, queryset._compile_query) == _SR1.format("SearchScore(1)", 1, "reference 0")
        queryset.search(body="b")
        assert queryset._compile_query()[0] == (
            "SELECT *, search::score(1) AS s FROM fts_article WHERE title @0@ $_f0 AND body @1@ $_f1;"
        )

    @pytest.mark.parametrize(("annotation", "noun"), _SEARCH_ANNOTATIONS, ids=["score", "highlight", "offsets"])
    def test_sr2_without_search(self, annotation: Any, noun: str) -> None:
        queryset = FtsArticle.objects().filter(views__gt=1).annotate(x=annotation)
        assert _raised(ValueError, queryset._compile_query) == _SR2.format(repr(annotation))

    @pytest.mark.usefixtures("_no_client")
    @pytest.mark.parametrize("method", ["exec", "exists", "first", "get"])
    async def test_sr1_and_sr2_before_any_request(self, method: str) -> None:
        unsearched = FtsArticle.objects().annotate(s=SearchScore(0))
        with pytest.raises(ValueError) as raised:
            await getattr(unsearched, method)()
        assert str(raised.value) == _SR2.format("SearchScore(0)")
        unknown = FtsArticle.objects().search(title="a").annotate(s=SearchScore(1))
        with pytest.raises(ValueError) as raised:
            await getattr(unknown, method)()
        assert str(raised.value) == _SR1.format("SearchScore(1)", 1, "reference 0")

    @pytest.mark.usefixtures("_no_client")
    @pytest.mark.parametrize("method", ["exec", "exists"])
    async def test_sr2_before_the_moment_of_as_of_is_resolved(self, method: str) -> None:
        """Under ``as_of(<duration>)`` the read resolves its moment with a ``RETURN`` request first: the refusal
        comes before it."""
        queryset = FtsArticle.objects().as_of(timedelta(hours=1)).annotate(h=SearchHighlight())
        with pytest.raises(ValueError) as raised:
            await getattr(queryset, method)()
        assert str(raised.value) == _SR2.format("SearchHighlight('<b>', '</b>', 0)")

    async def test_count_ignores_the_annotations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``count()`` and the aggregations ignore every annotation, as they ignore ``GeoDistance``: no reference
        check, no projection."""
        sent: list[tuple[str, dict[str, Any]]] = []

        async def execute(self: Any, query: str, variables: dict[str, Any] | None = None, **kwargs: Any) -> Any:
            sent.append((query, dict(variables or {})))
            return [{"count": 3}]

        monkeypatch.setattr(type(FtsArticle.objects()), "_execute_query", execute)
        assert await FtsArticle.objects().annotate(s=SearchScore(0)).count() == 3
        assert await FtsArticle.objects().search(title="a").annotate(s=SearchScore(4)).count() == 3
        assert sent == [
            ("SELECT count() FROM fts_article GROUP ALL;", {}),
            ("SELECT count() FROM fts_article WHERE title @0@ $_f0 GROUP ALL;", {"_f0": "a"}),
        ]

    @pytest.mark.usefixtures("_no_client")
    def test_refused_by_a_live_query_a_change_feed_and_a_subquery(self) -> None:
        for annotation, _ in _SEARCH_ANNOTATIONS:
            annotated = FtsArticle.objects().annotate(x=annotation)
            assert _raised(SurrealDbError, annotated.live) == _LIVE_ANNOTATE
            assert _raised(SurrealDbError, annotated.watch) == _LIVE_ANNOTATE
            assert _raised(SurrealDbError, annotated.changes) == _CH1.format("annotate()")
            inner = FtsArticle.objects().search(title="a").annotate(x=annotation)
            outer = FtsArticle.objects().filter(id__in=Subquery(inner))
            assert _raised(ValueError, outer._compile_query) == _SUBQUERY_ANNOTATE

    def test_an_alias_given_again_replaces_the_earlier_value(self) -> None:
        point = GeoDistance("meta.at", (1.0, 2.0))
        aggregated = FtsArticle.objects().annotate(x=SearchScore(0)).annotate(x=Count())
        assert aggregated._search_annotations == {} and aggregated._geo_annotations == {}
        assert isinstance(aggregated._annotations["x"], Count)
        assert aggregated._compile_group_by_query()[0] == "SELECT count() AS x FROM fts_article GROUP ALL;"
        scored = FtsArticle.objects().annotate(x=Count()).annotate(x=SearchScore(0))
        assert scored._annotations == {"x": SearchScore(0)} and scored._search_annotations == {"x": SearchScore(0)}
        moved = FtsArticle.objects().annotate(x=point).annotate(x=SearchOffsets(0))
        assert moved._geo_annotations == {} and moved._search_annotations == {"x": SearchOffsets(0)}
        back = FtsArticle.objects().annotate(x=SearchOffsets(0)).annotate(x=point)
        assert back._search_annotations == {} and list(back._geo_annotations) == ["x"]
        assert back.search(title="a")._compile_query()[0] == (
            "SELECT *, IF meta.at.is_point() THEN geo::distance(meta.at, $_gd0) END AS x FROM fts_article WHERE title @0@ $_f0;"
        )


# === E2E: the connection "fts" on the database fts_db, over WebSocket and HTTP, and mem:// =========================

#: The engines every E2E class runs on: the line under test over WebSocket and HTTP, and the embedded engine.
_KINDS = ["ws", "http", "mem"]

#: The reset before each E2E test, in one query.
_FTS_RESET = "REMOVE DATABASE IF EXISTS fts_db; DEFINE DATABASE fts_db;"

#: The function A3's analyzer applies.
_FN_LOW = "DEFINE FUNCTION fn::fts_low($s: string) { RETURN string::lowercase($s); };"


def _url(kind: str) -> str:
    """The URL of *kind*: ``ws`` or ``http`` on the line under test (``SURREALDB_PORT``), or ``mem``."""
    if kind == "mem":
        return "mem://"
    host = os.environ.get("SURREALDB_HOST", "localhost")
    port = os.environ.get("SURREALDB_PORT", "8000")
    return f"ws://{host}:{port}/rpc" if kind == "ws" else f"http://{host}:{port}"


def _register_fts(kind: str) -> None:
    if kind == "mem":
        SurrealDBConnectionManager.add_connection("fts", url="mem://", namespace="ns", database="fts_db")
    else:
        SurrealDBConnectionManager.add_connection(
            "fts", url=_url(kind), user="root", password="root", namespace="ns", database="fts_db"
        )


async def _apply_ddl(client: Any, ddl: str) -> None:
    """Run *ddl* and check every statement: ``query()`` checks only the first one."""
    response = await client.query_raw(ddl, {})
    assert "error" not in response, response["error"]
    refused = [result for result in response["result"] if result["status"] != "OK"]
    assert response["result"] and not refused, refused


async def _refusal(client: Any, query: str) -> str | None:
    """Run *query* raw: ``None`` when SurrealDB accepts every statement, else the first refusal's text (a parse error
    refuses the whole query; the embedded engine raises it)."""
    try:
        response = await client.query_raw(query, {})
    except RuntimeError as error:
        return str(error)
    if "error" in response:
        return str(response["error"])
    refused = [str(result["result"]) for result in response["result"] if result["status"] != "OK"]
    return refused[0] if refused else None


@contextlib.asynccontextmanager
async def _fts(kind: str) -> AsyncIterator[Any]:
    """The connection ``"fts"`` on *kind*, its database ``fts_db`` reset in one query; removed, then unregistered, on
    exit."""
    _register_fts(kind)
    client = await SurrealDBConnectionManager.get_client("fts")
    await _apply_ddl(client, _FTS_RESET)
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            cleanup = await SurrealDBConnectionManager.get_client("fts")
            await cleanup.query("REMOVE DATABASE IF EXISTS fts_db;", {})
        await SurrealDBConnectionManager.remove_connection("fts")


async def _line(client: Any) -> tuple[int, int]:
    """``(major, minor)`` of the engine *client* talks to: ``"surrealdb-3.1.5"`` → ``(3, 1)``; ``mem://`` →
    ``(2, 0)`` (the SDK's version, whose core is 2.x)."""
    match = re.search(r"(\d+)\.(\d+)", await client.version())
    assert match is not None
    return int(match.group(1)), int(match.group(2))


async def _major(client: Any) -> int:
    """The major version of the engine *client* talks to: ``"surrealdb-3.3.4"`` → 3; ``mem://`` → 2."""
    return (await _line(client))[0]


async def _keyword_line(client: Any) -> Literal[2, 3]:
    """The ``line=`` of the renderers for the engine *client* talks to: 3 from SurrealDB 3.x on, else 2."""
    return 3 if await _major(client) >= 3 else 2


class TestGoldensE2E:
    """Every golden statement applied on every engine with the line's keyword (SurrealDB 3.x ``FULLTEXT``, 2.x and the
    embedded engine ``SEARCH``): accepted, matched by what ``INFO`` returns, and read by the introspector as
    declared."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_goldens_are_applied_and_match(self, kind: str) -> None:
        async with _fts(kind) as client:
            line = await _keyword_line(client)
            article = FtsArticle.index_ddl(line=line)
            assert article == (_I1 if line == 3 else _I2)
            i5 = _FTS_T_INDEXES["fts_k"]
            statements = [
                _A1,
                _FN_LOW,
                _A3,
                _A4,
                _A5,
                *article,
                _I4,
                i5.ddl("fts_t", line=line),
                _FTS_T_INDEXES["fts_k2"].ddl("fts_t", line=line),
            ]
            if line == 2:
                assert i5.ddl("fts_t", line=line) == _I5
            for statement in statements:
                await _apply_ddl(client, statement)
            analyzers, tables = await search.read_database_info(
                client, what="index", kind="definition", subject="table 'fts_article'"
            )
            article_indexes = await search.read_table_indexes(client, "fts_article", what="index", kind="definition")
            other_indexes = await search.read_table_indexes(client, "fts_t", what="index", kind="definition")
        assert sorted(analyzers) == ["fts_empty", "fts_english", "fts_fn", "fts_grams"]
        for analyzer in _golden_analyzers():
            assert search.analyzer_matches(analyzer, analyzers[analyzer.name]), analyzer.name
        assert tables == ("fts_article", "fts_t")
        assert sorted(article_indexes) == sorted(_ARTICLE_INDEXES)
        for index in FtsArticle.get_indexes():
            assert search.index_matches(index, _columns(index), article_indexes[index.name]), index.name
        assert sorted(other_indexes) == sorted(_FTS_T_INDEXES)
        for name, index in _FTS_T_INDEXES.items():
            assert search.index_matches(index, index.fields, other_indexes[name]), name

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_introspector_reads_them_as_declared(self, kind: str) -> None:
        async with _fts(kind) as client:
            for statement in FtsArticle.index_ddl(line=await _keyword_line(client)):
                await _apply_ddl(client, statement)
            schema = await DatabaseIntrospector("fts", tables=["fts_article"]).introspect()
        assert schema.issues == ()
        assert dataclasses.replace(schema.analyzers["fts_english"], raw="") == AnalyzerState(
            "fts_english", ("class", "blank"), ("lowercase", "ascii", "snowball(english)"), comment="English text"
        )
        indexes = schema.tables["fts_article"].indexes
        assert sorted(indexes) == sorted(_ARTICLE_INDEXES)
        title = indexes["fts_article_title"]
        assert (title.fields, title.search_analyzer, title.bm25, title.highlights) == (
            ("title",),
            "fts_english",
            (1.2, 0.75),
            True,
        )
        body = indexes["fts_article_body"]
        assert (body.search_analyzer, body.bm25, body.highlights) == ("fts_english", (1.2, 0.75), False)
        head = indexes["fts_article_head"]
        assert (head.fields, head.search_analyzer, head.bm25, head.highlights) == (
            ("meta.headline",),
            "fts_english",
            (1.2, 0.75),
            False,
        )
        assert indexes["fts_article_heading"].fields == ("head_text",)
        assert indexes["fts_article_tags"].highlights is True
        slug = indexes["fts_article_slug"]
        assert (slug.unique, slug.comment, slug.search_analyzer, slug.bm25) == (True, "it's unique", None, None)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_each_line_refuses_the_others_keyword(self, kind: str) -> None:
        """README row "Full-text index": SurrealDB 3.x writes a full-text index ``FULLTEXT`` and refuses ``SEARCH``;
        2.x and the embedded engine write it ``SEARCH`` and refuse ``FULLTEXT`` — a parse error."""
        async with _fts(kind) as client:
            line = await _keyword_line(client)
            other: Literal[2, 3] = 2 if line == 3 else 3
            await _apply_ddl(client, _A1)
            index = Index("fts_k", "k", search_analyzer="fts_english")
            refused = await _refusal(client, index.ddl("fts_t", line=other))
            accepted = await _refusal(client, index.ddl("fts_t", line=line))
        assert refused is not None and "Parse error" in refused, (
            f"README row 'Full-text index': SurrealDB {line}.x refuses the {search.fulltext_keyword(other)} keyword"
        )
        assert accepted is None, f"README row 'Full-text index': SurrealDB {line}.x takes {search.fulltext_keyword(line)}"

    async def test_the_embedded_engine_takes_search(self) -> None:
        """The keyword tripwire: an SDK whose embedded core changes line must change the keyword dispatch. The
        embedded engine reports the SDK's version (major 2, its core's line) and takes ``SEARCH``, refusing
        ``FULLTEXT``; the ORM writes the keyword of the major version the engine reports."""
        async with _fts("mem") as client:
            major = await _major(client)
            await _apply_ddl(client, _A1)
            index = Index("fts_k", "k", search_analyzer="fts_english")
            fulltext = await _refusal(client, index.ddl("fts_t", line=3))
            keyword = await _refusal(client, index.ddl("fts_t", line=2))
        assert major == 2, "the embedded engine's major version changed: check the full-text keyword it takes"
        assert fulltext is not None and "Parse error" in fulltext, "the embedded engine now takes FULLTEXT"
        assert keyword is None, "the embedded engine now refuses SEARCH"


#: A record access on ``fts_db``: a user signed up through it is a record user, who may not read the schema.
_FTS_ACCESS = (
    "DEFINE ACCESS fts_acc ON DATABASE TYPE RECORD SIGNUP (CREATE fts_user SET name = $name) "
    "SIGNIN (SELECT * FROM fts_user WHERE name = $name);"
)


class TestReadsE2E:
    """``read_table_indexes()`` and ``read_database_info()`` on every engine: a missing table has no index, a
    definition the parser cannot read is ``None`` under its name, and a record user's read is refused with
    ``SurrealDbAuthenticationError``."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_missing_table_and_an_empty_database(self, kind: str) -> None:
        async with _fts(kind) as client:
            indexes = await search.read_table_indexes(client, "fts_missing", what="index", kind="definition")
            info = await search.read_database_info(client, what="index", kind="definition", subject="table 'fts_missing'")
        assert indexes == {}
        assert info == ({}, ())

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_unreadable_definition_is_none_under_its_name(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _fts(kind) as client:
            for statement in FtsArticle.index_ddl(line=await _keyword_line(client)):
                await _apply_ddl(client, statement)
            real = client.query
            sent: list[str] = []

            async def answering(statement: str, variables: Any) -> Any:
                sent.append(statement)
                answer = await real(statement, variables)
                if statement.startswith("INFO FOR TABLE "):
                    answer = {**answer, "indexes": {**answer["indexes"], "fts_article_title": "DEFINE INDEX fts_article_title"}}
                elif statement == "INFO FOR DB;":
                    answer = {**answer, "analyzers": {"fts_english": "DEFINE ANALYZER fts_english FILTERS"}}
                return answer

            monkeypatch.setattr(client, "query", answering)
            indexes = await search.read_table_indexes(client, "fts_article", what="index", kind="definition")
            analyzers, tables = await search.read_database_info(
                client, what="index", kind="definition", subject="table 'fts_article'"
            )
            monkeypatch.undo()
        assert sent == ["INFO FOR TABLE `fts_article`;", "INFO FOR DB;"]
        assert sorted(indexes) == sorted(_ARTICLE_INDEXES)
        assert indexes["fts_article_title"] is None
        body = _article_index("fts_article_body")
        assert search.index_matches(body, _columns(body), indexes["fts_article_body"])
        assert analyzers == {"fts_english": None}
        assert tables == ("fts_article",)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_record_user_may_not_read(self, kind: str) -> None:
        if kind == "mem":
            pytest.skip("the embedded engine's session is its owner: it may always read")
        async with _fts(kind) as client, SurrealDBConnectionManager.using("fts"):
            await _apply_ddl(client, _FTS_ACCESS)
            await SurrealDBConnectionManager.signup(access="fts_acc", variables={"name": "fts-user"})
            try:
                with pytest.raises(SurrealDbError) as table_read:
                    await search.read_table_indexes(client, "fts_article", what="index", kind="definition")
                with pytest.raises(SurrealDbError) as database_read:
                    await search.read_database_info(client, what="analyzer", kind="definition", subject="'fts_english'")
            finally:
                await SurrealDBConnectionManager.invalidate()
        assert type(table_read.value) is SurrealDbAuthenticationError
        assert str(table_read.value).startswith("Can't apply index definition: table 'fts_article' -> ")
        assert "not enough permissions" in str(table_read.value).lower()
        assert type(database_read.value) is SurrealDbAuthenticationError
        assert str(database_read.value).startswith("Can't apply analyzer definition: 'fts_english' -> ")
        assert "not enough permissions" in str(database_read.value).lower()


# --- define_indexes() and remove_index() on the engines -------------------------------------------------------------


class FtsArticleV2(FtsArticle):
    """``FtsArticle`` on the same table, ``fts_article_title`` redeclared without ``HIGHLIGHTS``."""

    model_config = SurrealConfigDict(
        table_name="fts_article",
        indexes=[
            Index("fts_article_title", "title", search_analyzer=FTS_ENGLISH, bm25=True),
            *FtsArticle.get_indexes()[1:],
        ],
    )


class FtsDoc(BaseSurrealModel):
    """An index naming by ``str`` an analyzer neither the model nor the database defines."""

    model_config = SurrealConfigDict(table_name="fts_doc", indexes=[Index("fts_x", "x", search_analyzer="fts_none")])

    id: str | None = None
    x: str = ""


class FtsNamed(BaseSurrealModel):
    """An index naming by ``str`` the analyzer ``fts_english``, which the model does not declare."""

    model_config = SurrealConfigDict(
        table_name="fts_named", indexes=[Index("fts_named_x", "x", search_analyzer="fts_english", bm25=True)]
    )

    id: str | None = None
    x: str = ""


class FtsCites(BaseSurrealEdge):
    """An edge model with a full-text index."""

    model_config = SurrealConfigDict(
        table_name="fts_cites", indexes=[Index("fts_cites_note", "note", search_analyzer=FTS_ENGLISH)]
    )

    note: str = ""


#: The reads ``define_indexes()`` sends, as the ORM writes them.
_INFO_DB = "INFO FOR DB;"
_INFO_ARTICLE = "INFO FOR TABLE `fts_article`;"

_DI1 = (
    "Can't apply index definition: 'fts_x' on table 'fts_doc' -> the analyzer 'fts_none' does not exist; pass "
    "search_analyzer=Analyzer(...) or define it with SurrealDBConnectionManager.define_analyzer() first."
)
_DI2 = (
    "FtsArticle.define_indexes(tx=…) cannot define analyzer 'fts_english' in a transaction: SurrealDB builds an index "
    "with the analyzers committed before the transaction began. Call SurrealDBConnectionManager.define_analyzer() "
    "without tx= first, then define_indexes(tx=…)."
)

#: The assertion message of the materialized-view pin on SurrealDB 3.x.
_VIEW_PIN_3X = (
    "README section 36, Not supported (_S36_VIEWS): SurrealDB 3.x does not index the records a view gains after the "
    "index is defined (surrealdb/surrealdb#7377, fixed in SurrealDB 4.0). If this fails, the fix reached this line: "
    "indexes= on view models can be reconsidered."
)


class _Rollback(Exception):
    """Raised inside a ``transaction()`` block to roll it back."""


@contextlib.asynccontextmanager
async def _fts_models(kind: str) -> AsyncIterator[Any]:
    """:func:`_fts`, the models routed to the connection ``"fts"`` for the block (``using("fts")``)."""
    async with _fts(kind) as client, SurrealDBConnectionManager.using("fts"):
        yield client


def _spy(monkeypatch: pytest.MonkeyPatch, client: Any) -> list[tuple[str, tuple[object, ...]]]:
    """Record every ``query``, ``query_raw`` and ``version`` call *client* receives, with its arguments (and let it
    through) — v0.31.0's spy."""
    calls: list[tuple[str, tuple[object, ...]]] = []

    def recording(method: str) -> Any:
        real = getattr(client, method)

        async def call(*args: object, **kwargs: object) -> object:
            calls.append((method, args))
            return await real(*args, **kwargs)

        return call

    for method in ("query", "query_raw", "version"):
        monkeypatch.setattr(client, method, recording(method))
    return calls


def _calls(calls: list[tuple[str, tuple[object, ...]]]) -> list[tuple[str, object]]:
    """What *calls* (:func:`_spy`'s) sent: each method with its first argument (``None`` for ``version()``)."""
    return [(method, args[0] if args else None) for method, args in calls]


def _query(statement: str) -> list[tuple[str, object]]:
    """The calls of one ``client.query(statement, {})``: the SDK's ``query()`` sends it through ``query_raw()``."""
    return [("query", statement), ("query_raw", statement)]


#: The reads of ``FtsArticle.define_indexes()``: ``INFO FOR DB``, the line, ``INFO FOR TABLE``.
_ARTICLE_READS = [*_query(_INFO_DB), ("version", None), *_query(_INFO_ARTICLE)]


async def _tables(client: Any) -> dict[str, Any]:
    """The ``tables`` of ``INFO FOR DB`` on *client*'s database."""
    return dict((await client.query("INFO FOR DB;", {}))["tables"])


async def _indexes(client: Any, table: str = "fts_article") -> dict[str, IndexState | None]:
    """The indexes of *table*, as ``read_table_indexes()`` reads them."""
    return await search.read_table_indexes(client, table, what="index", kind="definition")


async def _refusals(client: Any, query: str) -> list[str]:
    """Run *query* raw: the text of every statement SurrealDB refused, ``[]`` when it accepts them all (a parse error
    refuses the whole query; the embedded engine raises it)."""
    try:
        response = await client.query_raw(query, {})
    except RuntimeError as error:
        return [str(error)]
    if "error" in response:
        return [str(response["error"])]
    return [str(result["result"]) for result in response["result"] if result["status"] != "OK"]


def _register_other(kind: str) -> None:
    """The connection ``"fts2"``, on the same database as ``"fts"``."""
    if kind == "mem":
        SurrealDBConnectionManager.add_connection("fts2", url="mem://", namespace="ns", database="fts_db")
    else:
        SurrealDBConnectionManager.add_connection(
            "fts2", url=_url(kind), user="root", password="root", namespace="ns", database="fts_db"
        )


class TestDefineIndexesE2E:
    """``define_indexes()`` on every engine (SurrealDB 3.x and 2.x over WebSocket and HTTP, and the embedded engine):
    it reads ``INFO FOR DB``, the line and ``INFO FOR TABLE``, then applies only what changed, with the line's keyword;
    an analyzer an index names must exist, and a transaction defines no analyzer — both refused before any DDL."""

    @pytest.mark.parametrize("overwrite", [True, False])
    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_fresh_database_then_nothing_changed(
        self, kind: str, overwrite: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with _fts_models(kind) as client:
            expected = FtsArticle.index_ddl(overwrite=overwrite, line=await _keyword_line(client))
            # fts_article_body names "fts_english" by str: one of the model's Analyzer objects defines it.
            assert await FtsArticle.define_indexes(overwrite=overwrite) == expected
            analyzers, tables = await search.read_database_info(
                client, what="index", kind="definition", subject="table 'fts_article'"
            )
            indexes = await _indexes(client)
            calls = _spy(monkeypatch, client)
            assert await FtsArticle.define_indexes(overwrite=overwrite) == []
            sent = _calls(calls)
            monkeypatch.undo()
        assert sent == _ARTICLE_READS
        assert sorted(analyzers) == ["fts_english"]
        assert search.analyzer_matches(FTS_ENGLISH, analyzers["fts_english"])
        assert tables == ("fts_article",)
        assert sorted(indexes) == sorted(_ARTICLE_INDEXES)
        for index in FtsArticle.get_indexes():
            assert search.index_matches(index, _columns(index), indexes[index.name]), index.name

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_only_a_changed_index_is_redefined(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _fts_models(kind) as client:
            keyword = search.fulltext_keyword(await _keyword_line(client))
            await FtsArticle.define_indexes()
            changed = (
                f"DEFINE INDEX OVERWRITE `fts_article_title` ON `fts_article` FIELDS `title` {keyword} ANALYZER "
                "`fts_english` BM25;"
            )
            calls = _spy(monkeypatch, client)
            assert await FtsArticleV2.define_indexes(overwrite=False) == []
            kept = _calls(calls)
            kept_title = (await _indexes(client))["fts_article_title"]
            calls.clear()
            assert await FtsArticleV2.define_indexes() == [changed]
            sent = _calls(calls)
            monkeypatch.undo()
            title = (await _indexes(client))["fts_article_title"]
            # The model wins again: FtsArticle redefines its own title index, nothing else.
            assert await FtsArticle.define_indexes() == [FtsArticle.index_ddl(line=await _keyword_line(client))[1]]
        assert kept == _ARTICLE_READS
        assert kept_title is not None and kept_title.highlights is True
        assert sent == [*_ARTICLE_READS, *_query(changed)]
        assert title is not None and title.highlights is False
        assert search.index_matches(FtsArticleV2.get_indexes()[0], ["title"], title)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_unreadable_stored_index_is_redefined(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _fts_models(kind) as client:
            await FtsArticle.define_indexes()
            real = client.query

            async def answering(statement: str, variables: Any) -> Any:
                answer = await real(statement, variables)
                if statement == _INFO_ARTICLE:
                    answer = {**answer, "indexes": {**answer["indexes"], "fts_article_title": "DEFINE INDEX fts_article_title"}}
                return answer

            monkeypatch.setattr(client, "query", answering)
            applied = await FtsArticle.define_indexes()
            monkeypatch.undo()
            title = FtsArticle.index_ddl(line=await _keyword_line(client))[1]
            indexes = await _indexes(client)
        assert applied == [title]
        assert sorted(indexes) == sorted(_ARTICLE_INDEXES)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_unreadable_stored_analyzer_is_redefined_and_its_indexes_rebuilt(
        self, kind: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An analyzer redefined leaves the records indexed before with their old tokens: every index of the table that
        uses it is rebuilt, after the index definitions, in declaration order."""
        async with _fts_models(kind) as client:
            await FtsArticle.define_indexes()
            real = client.query

            async def answering(statement: str, variables: Any) -> Any:
                answer = await real(statement, variables)
                if statement == _INFO_DB:
                    answer = {**answer, "analyzers": {"fts_english": "DEFINE ANALYZER fts_english FILTERS"}}
                return answer

            monkeypatch.setattr(client, "query", answering)
            applied = await FtsArticle.define_indexes()
            monkeypatch.undo()
            analyzers, _ = await search.read_database_info(
                client, what="index", kind="definition", subject="table 'fts_article'"
            )
        rebuilt = [name for name in _ARTICLE_INDEXES if name != "fts_article_slug"]
        assert applied == [_A1, *(f"REBUILD INDEX `{name}` ON `fts_article`;" for name in rebuilt)]
        assert search.analyzer_matches(FTS_ENGLISH, analyzers["fts_english"])

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_model_without_indexes_sends_nothing(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _fts_models(kind) as client:
            calls = _spy(monkeypatch, client)
            assert await FtsPlain.define_indexes() == []
            assert await FtsPlain.define_indexes(overwrite=False) == []
            with pytest.raises(TypeError) as plain:
                await FtsPlain.define_indexes(overwrite=1)  # type: ignore[arg-type]
            with pytest.raises(TypeError) as article:
                await FtsArticle.define_indexes(overwrite="yes")  # type: ignore[arg-type]
            sent = _calls(calls)
            monkeypatch.undo()
        assert sent == []
        assert str(plain.value) == "overwrite= must be a bool, got int."
        assert str(article.value) == "overwrite= must be a bool, got str."

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_analyzer_named_by_str_must_exist(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _fts_models(kind) as client:
            calls = _spy(monkeypatch, client)
            with pytest.raises(SurrealDbError) as raised:
                await FtsDoc.define_indexes()
            sent = _calls(calls)
            monkeypatch.undo()
            tables = await _tables(client)
            # Once the database defines it, an analyzer named by str is not defined again: only the index is.
            await _apply_ddl(client, _A1)
            keyword = search.fulltext_keyword(await _keyword_line(client))
            applied = await FtsNamed.define_indexes()
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == _DI1
        assert sent == _query(_INFO_DB)
        assert tables == {}
        assert applied == [
            f"DEFINE INDEX OVERWRITE `fts_named_x` ON `fts_named` FIELDS `x` {keyword} ANALYZER `fts_english` BM25;"
        ]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_transaction_defines_no_analyzer(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """Refused before any DDL, for an analyzer to define (a fresh database) and to redefine (another definition
        stored): SurrealDB builds an index with the analyzers committed before the transaction began."""
        async with _fts_models(kind) as client:
            async with SurrealDBConnectionManager.transaction("fts") as tx:
                calls = _spy(monkeypatch, client)
                with pytest.raises(SurrealDbError) as fresh:
                    await FtsArticle.define_indexes(tx=tx)
                sent = _calls(calls)
                monkeypatch.undo()
                queued = getattr(tx, "statements", None)
            await _apply_ddl(client, Analyzer("fts_english", ["blank"], ["ascii"]).ddl())
            async with SurrealDBConnectionManager.transaction("fts") as tx:
                with pytest.raises(SurrealDbError) as changed:
                    await FtsArticle.define_indexes(tx=tx)
            tables = await _tables(client)
        assert type(fresh.value) is SurrealDbError
        assert str(fresh.value) == _DI2
        assert sent == _query(_INFO_DB)
        assert queued in (None, [])
        assert type(changed.value) is SurrealDbError
        assert str(changed.value) == _DI2
        assert "fts_article" not in tables

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_index_statements_join_the_transaction(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """With the analyzers in place, the index statements join the block: queued and applied at the commit in a
        buffered transaction (HTTP, SurrealDB 2.x, the embedded engine), applied at once in an interactive one (3.x over
        WebSocket); the reads see the committed state before anything joins it."""
        async with _fts_models(kind) as client:
            interactive = kind == "ws" and await _major(client) >= 3
            await _apply_ddl(client, _A1)
            statements = FtsArticle.index_ddl(line=await _keyword_line(client))[1:]
            async with SurrealDBConnectionManager.transaction("fts") as tx:
                assert tx.is_interactive is interactive
                calls = _spy(monkeypatch, client)
                assert await FtsArticle.define_indexes(tx=tx) == statements
                sent = _calls(calls)
                monkeypatch.undo()
                queued = getattr(tx, "statements", None)
                # Outside the block, until the commit, there is no index.
                before = await _indexes(client)
            after = await _indexes(client)
        if interactive:
            assert sent == [*_ARTICLE_READS, *(("query_raw", statement) for statement in statements)]
        else:
            assert sent == _ARTICLE_READS
            assert queued == statements
        assert before == {}
        assert sorted(after) == sorted(_ARTICLE_INDEXES)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_block_that_raises_applies_no_index(self, kind: str) -> None:
        async with _fts_models(kind) as client:
            await _apply_ddl(client, _A1)
            with pytest.raises(_Rollback):
                async with SurrealDBConnectionManager.transaction("fts") as tx:
                    assert await FtsArticle.define_indexes(tx=tx) != []
                    raise _Rollback
            indexes = await _indexes(client)
            tables = await _tables(client)
        assert indexes == {}
        assert "fts_article" not in tables

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_analyzer_and_its_index_in_one_transaction(self, kind: str) -> None:
        """README row "Full-text index" and section 36, Transactions: on a table holding records, a transaction that
        defines an analyzer and then an index using it fails on every SurrealDB server — the server builds the index
        with the analyzers committed before the transaction began — and succeeds on the embedded engine. Hence
        ``define_indexes(tx=…)`` defines no analyzer, on every engine."""
        async with _fts(kind) as client:
            keyword_line = await _keyword_line(client)
            await _apply_ddl(client, "CREATE fts_note:n1 SET title = 'Physics rocks';")
            index = Index("fts_note_title", "title", search_analyzer=FTS_ENGLISH).ddl("fts_note", line=keyword_line)
            refusals = await _refusals(client, f"BEGIN TRANSACTION; {_A1} {index} COMMIT TRANSACTION;")
            analyzers, _ = await search.read_database_info(client, what="index", kind="definition", subject="table 'fts_note'")
            indexes = await _indexes(client, "fts_note")
        if kind == "mem":
            assert refusals == [], "README row 'Full-text index': the embedded engine sees the analyzer"
            assert sorted(analyzers) == ["fts_english"]
            assert sorted(indexes) == ["fts_note_title"]
        else:
            assert any("The analyzer 'fts_english' does not exist" in text for text in refusals), (
                f"README row 'Full-text index': a SurrealDB server refuses the index, got {refusals}"
            )
            assert analyzers == {}
            assert indexes == {}

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_table_an_index_creates(self, kind: str) -> None:
        """README section 36, the sentence on calling ``define_table()`` before ``define_indexes()``: on a missing table
        ``DEFINE INDEX`` creates it ``TYPE ANY SCHEMALESS PERMISSIONS NONE``, which ``define_table(overwrite=False)``
        leaves as it is and ``define_table()`` replaces, keeping the indexes."""
        async with _fts_models(kind) as client:
            await FtsArticle.define_indexes()
            created = (await _tables(client))["fts_article"]
            await FtsArticle.define_table(overwrite=False)
            kept = (await _tables(client))["fts_article"]
            await FtsArticle.define_table()
            replaced = (await _tables(client))["fts_article"]
            indexes = await _indexes(client)
        assert created == "DEFINE TABLE fts_article TYPE ANY SCHEMALESS PERMISSIONS NONE", "README section 36, _S36_ORDER"
        assert kept == created, "README section 36, _S36_ORDER: define_table(overwrite=False) leaves the table as it is"
        assert replaced.startswith("DEFINE TABLE fts_article TYPE NORMAL "), "README section 36, _S36_ORDER"
        assert sorted(indexes) == sorted(_ARTICLE_INDEXES), "README section 36, _S36_ORDER: the indexes are kept"

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_edge_model_before_define_relation(self, kind: str) -> None:
        async with _fts_models(kind) as client:
            applied = await FtsCites.define_indexes()
            expected = FtsCites.index_ddl(line=await _keyword_line(client))
            created = (await _tables(client))["fts_cites"]
            await FtsCites.define_relation()
            related = (await _tables(client))["fts_cites"]
            indexes = await _indexes(client, "fts_cites")
        assert applied == expected
        assert " TYPE ANY " in created
        assert " TYPE RELATION" in related
        assert sorted(indexes) == ["fts_cites_note"]


@pytest.fixture
def empty_cache() -> Iterator[None]:
    """An empty query cache around the test."""
    QueryCache.clear()
    yield
    QueryCache.clear()


def _hits() -> int:
    """The query cache's hits so far."""
    return int(QueryCache.stats()["hits"])


class TestRemoveIndexE2E:
    """``remove_index()`` on every engine: ``REMOVE INDEX [IF EXISTS] `<name>` ON `<table>`;`` by name, applied and
    returned; ``missing_ok=False`` raises ``SurrealDbNotFoundError`` for a missing index; the arguments are checked and a
    transaction of another connection refused before any request; the model's cached reads are dropped."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_statements_and_missing_ok(self, kind: str) -> None:
        async with _fts_models(kind) as client:
            await FtsArticle.define_indexes()
            removed = await FtsArticle.remove_index("fts_article_title")
            strict = await FtsArticle.remove_index("fts_article_body", missing_ok=False)
            again = await FtsArticle.remove_index("fts_article_title")
            with pytest.raises(SurrealDbError) as missing:
                await FtsArticle.remove_index("fts_article_title", missing_ok=False)
            indexes = await _indexes(client)
        assert removed == again == "REMOVE INDEX IF EXISTS `fts_article_title` ON `fts_article`;"
        assert strict == "REMOVE INDEX `fts_article_body` ON `fts_article`;"
        assert type(missing.value) is SurrealDbNotFoundError
        assert str(missing.value).startswith("Can't apply index removal: 'fts_article_title' on table 'fts_article' -> ")
        assert "fts_article_title" in str(missing.value) and "REMOVE" not in str(missing.value)
        assert sorted(indexes) == sorted(set(_ARTICLE_INDEXES) - {"fts_article_title", "fts_article_body"})

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_arguments_are_checked_before_any_request(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _fts_models(kind) as client:
            calls = _spy(monkeypatch, client)
            with pytest.raises(ValueError) as bad_name:
                await FtsArticle.remove_index("fts-title")
            with pytest.raises(TypeError) as not_a_name:
                await FtsArticle.remove_index(3)  # type: ignore[arg-type]
            with pytest.raises(TypeError) as missing_ok:
                await FtsArticle.remove_index("fts_article_title", missing_ok=1)  # type: ignore[arg-type]
            sent = _calls(calls)
            monkeypatch.undo()
        assert sent == []
        assert str(bad_name.value) == f"Invalid index name 'fts-title': {_IDENTIFIER}"
        assert str(not_a_name.value) == "Index name must be a str, got int."
        assert str(missing_ok.value) == "missing_ok= must be a bool, got int."

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_transaction_of_another_connection(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _fts_models(kind) as client:
            _register_other(kind)
            try:
                async with SurrealDBConnectionManager.transaction("fts2") as tx:
                    other = await SurrealDBConnectionManager.get_client("fts2")
                    calls = _spy(monkeypatch, client)
                    other_calls = _spy(monkeypatch, other)
                    with pytest.raises(SurrealDbError) as removal:
                        await FtsArticle.remove_index("fts_article_title", tx=tx)
                    with pytest.raises(SurrealDbError) as definition:
                        await FtsArticle.define_indexes(tx=tx)
                    sent = (_calls(calls), _calls(other_calls))
                    queued = getattr(tx, "statements", None)
                    monkeypatch.undo()
            finally:
                await SurrealDBConnectionManager.remove_connection("fts2")
        assert sent == ([], [])
        assert queued in (None, [])
        for raised, method in ((removal, "remove_index"), (definition, "define_indexes")):
            assert type(raised.value) is SurrealDbError
            assert str(raised.value).startswith(
                f"FtsArticle.{method}() cannot run in this transaction: the transaction belongs to connection 'fts2' and "
                "FtsArticle runs on connection 'fts'."
            )

    @pytest.mark.usefixtures("empty_cache")
    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_cached_read_of_the_model_is_dropped(self, kind: str) -> None:
        async with _fts_models(kind):
            await FtsArticle.define_indexes()
            await FtsArticle(id="a1", title="Quantum physics for beginners", slug="a1").save()

            async def cached() -> list[str | None]:
                return [article.id for article in await FtsArticle.objects().cache(60).exec()]

            assert await cached() == ["a1"]
            hits = _hits()
            assert await cached() == ["a1"]
            assert _hits() == hits + 1
            await FtsArticle.remove_index("fts_article_title")
            assert await cached() == ["a1"]
            assert _hits() == hits + 1


class TestViewIndexPinE2E:
    """README section 36, Not supported: a materialized view model declares no ``indexes=``. Pinned on every engine
    with raw statements: a full-text index on a view's table is defined and searchable, but SurrealDB 3.x does not index
    the records the view gains after the index is defined (surrealdb/surrealdb#7377, fixed in SurrealDB 4.0), while
    2.x and the embedded engine do."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_index_on_a_view_misses_the_records_the_view_gains_on_3x(self, kind: str) -> None:
        async with _fts(kind) as client:
            major = await _major(client)
            keyword = search.fulltext_keyword(major)
            for statement in (
                "CREATE fts_src:b1 SET title = 'Quantum physics for beginners';",
                "DEFINE TABLE `fts_view` AS SELECT title FROM fts_src;",
                _A1,
                f"DEFINE INDEX `fts_view_title` ON `fts_view` FIELDS `title` {keyword} ANALYZER `fts_english` BM25;",
                "CREATE fts_src:b2 SET title = 'Cooking with physics';",
            ):
                await _apply_ddl(client, statement)
            held = await client.query("SELECT VALUE title FROM fts_view;", {})
            found = await client.query("SELECT VALUE title FROM fts_view WHERE title @0@ 'physics';", {})
        both = ["Cooking with physics", "Quantum physics for beginners"]
        assert sorted(held) == both
        if major >= 3:
            assert found == ["Quantum physics for beginners"], _VIEW_PIN_3X
        else:
            assert sorted(found) == both, "README section 36, Not supported: 2.x and the embedded engine index them"


# --- a changed analyzer, define_analyzer() and remove_analyzer() on the engines -------------------------------------


class FtsPost(BaseSurrealModel):
    """A full-text index on ``title`` holding ``FTS_PLAIN``."""

    model_config = SurrealConfigDict(
        table_name="fts_post", indexes=[Index("fts_post_title", "title", search_analyzer=FTS_PLAIN)]
    )

    id: str | None = None
    title: str = ""
    body: str = ""


class FtsPage(BaseSurrealModel):
    """Another table, its full-text index on ``title`` holding ``FTS_PLAIN`` too."""

    model_config = SurrealConfigDict(
        table_name="fts_page", indexes=[Index("fts_page_title", "title", search_analyzer=FTS_PLAIN)]
    )

    id: str | None = None
    title: str = ""


class FtsPostLower(FtsPost):
    """``FtsPost`` on the same table, the same names, its analyzer redeclared lower-casing."""

    model_config = SurrealConfigDict(
        table_name="fts_post", indexes=[Index("fts_post_title", "title", search_analyzer=FTS_PLAIN_LOWER)]
    )


_INFO_POST = "INFO FOR TABLE `fts_post`;"
_INFO_PAGE = "INFO FOR TABLE `fts_page`;"
_REBUILD_POST = "REBUILD INDEX `fts_post_title` ON `fts_post`;"
_REBUILD_PAGE = "REBUILD INDEX `fts_page_title` ON `fts_page`;"

#: SurrealDB 3.x's refusal of an analyzer an index uses (measured on 3.3.4 and 3.1.5), which RA1 repeats on 2.x.
_IN_USE = "The analyzer 'fts_english' is in use by index 'fts_article_title' on table 'fts_article' and cannot be removed"


def _sent(statements: Sequence[str]) -> list[tuple[str, object]]:
    """The calls of ``client.query(statement, {})`` for each of *statements*, in order."""
    return [call for statement in statements for call in _query(statement)]


async def _found(client: Any, table: str, field: str = "title") -> list[str]:
    """The ids of *table*'s records a raw search for ``physics`` in *field* finds, sorted."""
    found = await client.query(f"SELECT VALUE id FROM {table} WHERE {field} @0@ 'physics';", {})
    return sorted(str(record_id) for record_id in found)


async def _stored_analyzers(client: Any) -> dict[str, AnalyzerState | None]:
    """The analyzers ``INFO FOR DB`` holds, as ``read_database_info()`` reads them."""
    analyzers, _ = await search.read_database_info(client, what="analyzer", kind="definition", subject="'fts_plain'")
    return analyzers


def _redefinitions(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The warnings ``surreal_orm_lite.search`` logged."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "surreal_orm_lite.search" and record.levelno == logging.WARNING
    ]


def _x1m(operation: str) -> str:
    """The refusal of a manager call on ``"fts"`` with a transaction of ``"fts2"``."""
    return (
        f"{operation} cannot run in this transaction: the transaction belongs to connection 'fts2' and the call runs on "
        "connection 'fts'. A transaction is one connection's (one database): call it inside using('fts2'), or open the "
        "transaction on 'fts'."
    )


class TestAnalyzerChangeE2E:
    """An analyzer changed in code, on every engine: ``define_indexes()`` redefines it, then rebuilds every index of the
    database that uses it — the other tables' (sorted by table then index), then the model's table's — and logs a
    warning; a search tokenised the new way then finds the records indexed before. Pinned (README section 36, Changing
    an analyzer): without the rebuild SurrealDB leaves them with their old tokens."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_changed_analyzer_rebuilds_every_index_using_it(
        self, kind: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with _fts_models(kind) as client:
            await FtsPost.define_indexes()
            await FtsPage.define_indexes()
            await _apply_ddl(client, "CREATE fts_post:p1 SET title = 'Physics Rocks';")
            await _apply_ddl(client, "CREATE fts_page:g1 SET title = 'Physics Rocks';")
            before = [await _found(client, "fts_post"), await _found(client, "fts_page")]
            caplog.set_level(logging.WARNING, logger="surreal_orm_lite.search")
            calls = _spy(monkeypatch, client)
            applied = await FtsPostLower.define_indexes()
            sent = _calls(calls)
            monkeypatch.undo()
            after = [await _found(client, "fts_post"), await _found(client, "fts_page")]
            again = await FtsPostLower.define_indexes()
        expected = [FTS_PLAIN_LOWER.ddl(), _REBUILD_PAGE, _REBUILD_POST]
        assert before == [[], []]
        assert applied == expected
        assert sent == [*_query(_INFO_DB), ("version", None), *_query(_INFO_POST), *_query(_INFO_PAGE), *_sent(expected)]
        assert after == [["fts_post:p1"], ["fts_page:g1"]]
        assert again == []
        assert _redefinitions(caplog) == [
            "Analyzer 'fts_plain' redefined; 2 index(es) rebuilt: fts_page.fts_page_title, fts_post.fts_post_title"
        ]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_index_of_the_table_the_model_does_not_declare_is_rebuilt_too(self, kind: str) -> None:
        async with _fts_models(kind) as client:
            keyword = search.fulltext_keyword(await _keyword_line(client))
            await FtsPost.define_indexes()
            await _apply_ddl(
                client, f"DEFINE INDEX `fts_post_body` ON `fts_post` FIELDS `body` {keyword} ANALYZER `fts_plain`;"
            )
            await _apply_ddl(client, "CREATE fts_post:p1 SET title = 'Physics Rocks', body = 'Physics rules';")
            before = await _found(client, "fts_post", "body")
            applied = await FtsPostLower.define_indexes()
            after = await _found(client, "fts_post", "body")
        assert before == []
        assert applied == [FTS_PLAIN_LOWER.ddl(), _REBUILD_POST, "REBUILD INDEX `fts_post_body` ON `fts_post`;"]
        assert after == ["fts_post:p1"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_overwrite_false_keeps_a_changed_analyzer(
        self, kind: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with _fts_models(kind) as client:
            await FtsPost.define_indexes()
            await FtsPage.define_indexes()
            caplog.set_level(logging.WARNING, logger="surreal_orm_lite.search")
            calls = _spy(monkeypatch, client)
            applied = await FtsPostLower.define_indexes(overwrite=False)
            sent = _calls(calls)
            monkeypatch.undo()
            analyzers = await _stored_analyzers(client)
        assert applied == []
        assert sent == [*_query(_INFO_DB), ("version", None), *_query(_INFO_POST)]
        assert search.analyzer_matches(FTS_PLAIN, analyzers["fts_plain"])
        assert _redefinitions(caplog) == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_redefined_analyzer_leaves_the_records_indexed_before(self, kind: str) -> None:
        """README section 36, Changing an analyzer: SurrealDB leaves the records indexed before a ``DEFINE ANALYZER
        OVERWRITE`` with their old tokens — a search tokenised the new way misses them — and indexes the records
        written afterwards the new way, until ``REBUILD INDEX`` re-tokenises them all (every engine)."""
        async with _fts(kind) as client:
            index = Index("fts_post_title", "title", search_analyzer=FTS_PLAIN).ddl(
                "fts_post", line=await _keyword_line(client)
            )
            for statement in (
                FTS_PLAIN.ddl(),
                index,
                "CREATE fts_post:p1 SET title = 'Physics Rocks';",
                FTS_PLAIN_LOWER.ddl(),
                "CREATE fts_post:p2 SET title = 'Physics Rolls';",
            ):
                await _apply_ddl(client, statement)
            found = await _found(client, "fts_post")
            await _apply_ddl(client, _REBUILD_POST)
            rebuilt = await _found(client, "fts_post")
        assert found == ["fts_post:p2"], "README section 36, Changing an analyzer: the record indexed before is missed"
        assert rebuilt == ["fts_post:p1", "fts_post:p2"], "README section 36, Changing an analyzer: a rebuild finds both"


class TestDefineAnalyzerE2E:
    """``SurrealDBConnectionManager.define_analyzer()`` on every engine, on the current connection: a new analyzer
    defined, an equal one left (``INFO FOR DB`` alone sent), a changed one redefined and every index using it rebuilt
    (``overwrite=False`` keeps it); with a transaction a new analyzer or a redefinition no index uses joins it, a
    redefinition with indexes to rebuild is refused before any DDL; a transaction of another connection is refused
    before any request; every cached read is dropped."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_arguments_are_checked_before_any_request(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _fts_models(kind) as client:
            calls = _spy(monkeypatch, client)
            with pytest.raises(TypeError) as not_an_analyzer:
                await SurrealDBConnectionManager.define_analyzer("fts_plain")  # type: ignore[arg-type]
            with pytest.raises(TypeError) as overwrite:
                await SurrealDBConnectionManager.define_analyzer(FTS_PLAIN, overwrite=1)  # type: ignore[arg-type]
            sent = _calls(calls)
            monkeypatch.undo()
        assert sent == []
        assert str(not_an_analyzer.value) == "define_analyzer() takes an Analyzer, got str."
        assert str(overwrite.value) == "overwrite= must be a bool, got int."

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_new_equal_changed_and_overwrite_false(
        self, kind: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        manager = SurrealDBConnectionManager
        async with _fts_models(kind) as client:
            created = await manager.define_analyzer(FTS_PLAIN)
            calls = _spy(monkeypatch, client)
            equal = await manager.define_analyzer(FTS_PLAIN)
            kept = await manager.define_analyzer(FTS_PLAIN_LOWER, overwrite=False)
            unchanged_sent = _calls(calls)
            monkeypatch.undo()
            # The model's index holds the stored analyzer: only the index is defined.
            indexed = await FtsPost.define_indexes()
            await _apply_ddl(client, "CREATE fts_post:p1 SET title = 'Physics Rocks';")
            before = await _found(client, "fts_post")
            caplog.set_level(logging.WARNING, logger="surreal_orm_lite.search")
            calls = _spy(monkeypatch, client)
            changed = await manager.define_analyzer(FTS_PLAIN_LOWER)
            changed_sent = _calls(calls)
            monkeypatch.undo()
            after = await _found(client, "fts_post")
            fresh = await manager.define_analyzer(Analyzer("fts_fresh", ["blank"]), overwrite=False)
            analyzers = await _stored_analyzers(client)
        assert created == [FTS_PLAIN.ddl()]
        assert equal == [] and kept == []
        assert unchanged_sent == [*_query(_INFO_DB), *_query(_INFO_DB)]
        assert [statement.split(" `")[0] for statement in indexed] == ["DEFINE INDEX OVERWRITE"]
        assert before == []
        assert changed == [FTS_PLAIN_LOWER.ddl(), _REBUILD_POST]
        assert changed_sent == [*_query(_INFO_DB), *_query(_INFO_POST), *_sent(changed)]
        assert after == ["fts_post:p1"]
        assert fresh == ["DEFINE ANALYZER IF NOT EXISTS `fts_fresh` TOKENIZERS blank;"]
        assert search.analyzer_matches(FTS_PLAIN_LOWER, analyzers["fts_plain"])
        assert sorted(analyzers) == ["fts_fresh", "fts_plain"]
        assert _redefinitions(caplog) == ["Analyzer 'fts_plain' redefined; 1 index(es) rebuilt: fts_post.fts_post_title"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_with_a_transaction(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """A new analyzer joins the transaction (visible once it commits); a redefinition with an index to rebuild is
        refused before any DDL (DA2) — the rebuild must follow the committed definition —; a redefinition no index uses
        joins it."""
        manager = SurrealDBConnectionManager
        lone, lone_ascii = Analyzer("fts_lone", ["blank"]), Analyzer("fts_lone", ["blank"], ["ascii"])
        async with _fts_models(kind) as client:
            async with manager.transaction("fts") as tx:
                queued = await manager.define_analyzer(FTS_PLAIN, tx=tx)
                inside = await _stored_analyzers(client)
            committed = await _stored_analyzers(client)
            await FtsPost.define_indexes()
            async with manager.transaction("fts") as tx:
                calls = _spy(monkeypatch, client)
                with pytest.raises(SurrealDbError) as refused:
                    await manager.define_analyzer(FTS_PLAIN_LOWER, tx=tx)
                sent = _calls(calls)
                monkeypatch.undo()
                pending = getattr(tx, "statements", None)
            await manager.define_analyzer(lone)
            async with manager.transaction("fts") as tx:
                redefined = await manager.define_analyzer(lone_ascii, tx=tx)
            analyzers = await _stored_analyzers(client)
        assert queued == [FTS_PLAIN.ddl()]
        assert "fts_plain" not in inside
        assert search.analyzer_matches(FTS_PLAIN, committed["fts_plain"])
        assert type(refused.value) is SurrealDbError
        assert str(refused.value) == _DA2
        assert sent == [*_query(_INFO_DB), *_query(_INFO_POST)]
        assert pending in (None, [])
        assert search.analyzer_matches(FTS_PLAIN, analyzers["fts_plain"])
        assert redefined == [lone_ascii.ddl()]
        assert search.analyzer_matches(lone_ascii, analyzers["fts_lone"])

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_it_runs_on_the_using_connection(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _fts(kind) as client:
            calls = _spy(monkeypatch, client)
            async with SurrealDBConnectionManager.using("fts"):
                applied = await SurrealDBConnectionManager.define_analyzer(FTS_PLAIN)
                removed = await SurrealDBConnectionManager.remove_analyzer("fts_plain")
            sent = _calls(calls)
            monkeypatch.undo()
            analyzers = await _stored_analyzers(client)
        assert _sent(applied) == [call for call in sent if call[1] == applied[0]]
        assert ("query", removed) in sent
        assert analyzers == {}

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_transaction_of_another_connection(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _fts_models(kind) as client:
            _register_other(kind)
            try:
                async with SurrealDBConnectionManager.transaction("fts2") as tx:
                    other = await SurrealDBConnectionManager.get_client("fts2")
                    calls = _spy(monkeypatch, client)
                    other_calls = _spy(monkeypatch, other)
                    stats = QueryCache.stats()
                    with pytest.raises(SurrealDbError) as definition:
                        await SurrealDBConnectionManager.define_analyzer(FTS_PLAIN, tx=tx)
                    with pytest.raises(SurrealDbError) as removal:
                        await SurrealDBConnectionManager.remove_analyzer("fts_plain", tx=tx)
                    sent = (_calls(calls), _calls(other_calls))
                    after = QueryCache.stats()
                    queued = getattr(tx, "statements", None)
                    monkeypatch.undo()
            finally:
                await SurrealDBConnectionManager.remove_connection("fts2")
        assert sent == ([], [])
        assert after == stats
        assert queued in (None, [])
        assert type(definition.value) is SurrealDbError
        assert str(definition.value) == _x1m("define_analyzer()")
        assert type(removal.value) is SurrealDbError
        assert str(removal.value) == _x1m("remove_analyzer()")

    @pytest.mark.usefixtures("empty_cache")
    @pytest.mark.parametrize("kind", _KINDS)
    async def test_every_cached_read_is_dropped(self, kind: str) -> None:
        """``define_analyzer()`` drops every cached read (a rebuild may serve any table's search); ``remove_analyzer()``
        none — it succeeds only when no index uses the analyzer."""
        async with _fts_models(kind):
            await FtsPage(id="g1", title="Physics Rocks").save()

            async def cached() -> list[str | None]:
                return [page.id for page in await FtsPage.objects().cache(60).exec()]

            assert await cached() == ["g1"]
            hits = _hits()
            assert await cached() == ["g1"]
            assert _hits() == hits + 1
            await SurrealDBConnectionManager.define_analyzer(FTS_PLAIN)
            assert await cached() == ["g1"]
            assert _hits() == hits + 1
            assert await cached() == ["g1"]
            assert _hits() == hits + 2
            await SurrealDBConnectionManager.remove_analyzer("fts_plain")
            assert await cached() == ["g1"]
            assert _hits() == hits + 3


class TestRemoveAnalyzerE2E:
    """``SurrealDBConnectionManager.remove_analyzer()`` on every engine: ``REMOVE ANALYZER [IF EXISTS] `<name>`;``
    returned; ``missing_ok=False`` raises ``SurrealDbNotFoundError`` for a missing analyzer; an analyzer an index uses is
    refused with SurrealDB 3.x's own text on every engine — by the server on 3.x, by the ORM before any DDL on 2.x and
    the embedded engine, which would remove it (README row "Removing an analyzer")."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_statements_and_missing_ok(self, kind: str) -> None:
        manager = SurrealDBConnectionManager
        async with _fts_models(kind) as client:
            await manager.define_analyzer(FTS_PLAIN)
            await manager.define_analyzer(FTS_ENGLISH)
            removed = await manager.remove_analyzer("fts_plain")
            again = await manager.remove_analyzer("fts_plain")
            strict = await manager.remove_analyzer("fts_english", missing_ok=False)
            with pytest.raises(SurrealDbError) as missing:
                await manager.remove_analyzer("fts_english", missing_ok=False)
            analyzers = await _stored_analyzers(client)
        assert removed == again == "REMOVE ANALYZER IF EXISTS `fts_plain`;"
        assert strict == "REMOVE ANALYZER `fts_english`;"
        assert type(missing.value) is SurrealDbNotFoundError
        assert str(missing.value).startswith("Can't apply analyzer removal: 'fts_english' -> ")
        assert "REMOVE" not in str(missing.value)
        assert analyzers == {}

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_arguments_are_checked_before_any_request(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _fts_models(kind) as client:
            calls = _spy(monkeypatch, client)
            with pytest.raises(ValueError) as bad_name:
                await SurrealDBConnectionManager.remove_analyzer("fts-plain")
            with pytest.raises(TypeError) as not_a_name:
                await SurrealDBConnectionManager.remove_analyzer(3)  # type: ignore[arg-type]
            with pytest.raises(TypeError) as missing_ok:
                await SurrealDBConnectionManager.remove_analyzer("fts_plain", missing_ok=1)  # type: ignore[arg-type]
            sent = _calls(calls)
            monkeypatch.undo()
        assert sent == []
        assert str(bad_name.value) == f"Invalid analyzer name 'fts-plain': {_IDENTIFIER}"
        assert str(not_a_name.value) == "Analyzer name must be a str, got int."
        assert str(missing_ok.value) == "missing_ok= must be a bool, got int."

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_analyzer_an_index_uses_is_refused_on_every_engine(
        self, kind: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with _fts_models(kind) as client:
            major = await _major(client)
            index = Index("fts_article_title", "title", search_analyzer=FTS_ENGLISH)
            for statement in (
                _A1,
                index.ddl("fts_article", line=await _keyword_line(client)),
                Index("fts_other_code", "code", unique=True).ddl("fts_other"),
            ):
                await _apply_ddl(client, statement)
            calls = _spy(monkeypatch, client)
            with pytest.raises(SurrealDbError) as raised:
                await SurrealDBConnectionManager.remove_analyzer("fts_english")
            sent = _calls(calls)
            monkeypatch.undo()
            analyzers = await _stored_analyzers(client)
            created = await _refusals(client, "CREATE fts_article:x SET title = 'Physics';")
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == _RA1
        if major >= 3:
            assert sent == [("version", None), *_query("REMOVE ANALYZER IF EXISTS `fts_english`;")]
        else:
            assert sent == [
                ("version", None),
                *_query(_INFO_DB),
                *_query(_INFO_ARTICLE),
                *_query("INFO FOR TABLE `fts_other`;"),
            ]
        assert sorted(analyzers) == ["fts_english"]
        assert created == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_buffered_or_interactive_removal(self, kind: str) -> None:
        """An analyzer no index uses is removed with the transaction, once it commits."""
        async with _fts_models(kind) as client:
            await SurrealDBConnectionManager.define_analyzer(FTS_PLAIN)
            async with SurrealDBConnectionManager.transaction("fts") as tx:
                removed = await SurrealDBConnectionManager.remove_analyzer("fts_plain", tx=tx)
            analyzers = await _stored_analyzers(client)
        assert removed == "REMOVE ANALYZER IF EXISTS `fts_plain`;"
        assert analyzers == {}

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_removing_an_analyzer_an_index_uses(self, kind: str) -> None:
        """README row "Removing an analyzer an index uses": SurrealDB 3.x refuses the removal; 2.x and the embedded
        engine accept it, and every write to the table then fails — which ``remove_analyzer()`` prevents."""
        async with _fts(kind) as client:
            major = await _major(client)
            index = Index("fts_article_title", "title", search_analyzer=FTS_ENGLISH)
            await _apply_ddl(client, _A1)
            await _apply_ddl(client, index.ddl("fts_article", line=await _keyword_line(client)))
            removal = await _refusals(client, "REMOVE ANALYZER `fts_english`;")
            created = await _refusals(client, "CREATE fts_article:x SET title = 'Physics';")
        if major >= 3:
            assert len(removal) == 1 and _IN_USE in removal[0], (
                f"README row 'Removing an analyzer an index uses': SurrealDB 3.x refuses it, got {removal}"
            )
            assert created == []
        else:
            assert removal == [], "README row 'Removing an analyzer an index uses': 2.x and the embedded engine accept it"
            assert len(created) == 1 and "The analyzer 'fts_english' does not exist" in created[0], (
                f"README row 'Removing an analyzer an index uses': the next write fails, got {created}"
            )


# --- search() on the engines ----------------------------------------------------------------------------------------

#: The corpus: six articles — id, title, body, tags, meta.headline, heading — each searched term in fewer than half of
#: them. ``slug`` is the id, ``views`` its number (1 … 6).
_CORPUS = [
    ("a1", "Quantum physics for beginners", "An introduction to quantum mechanics", ["science", "quantum world"],
     "Rust web programming", "Introduction"),
    ("a2", "Cooking with physics", "Heat and physics in the kitchen", ["food"], "Kitchen tips", "Recipes"),
    ("a3", "Gardening in spring", "Plants and soil", ["garden"], "Soil care", "Seasons"),
    ("a4", "Quantum computing explained", "Qubits and gates", ["tech"], "Qubits", "Hardware"),
    ("a5", "Baking bread at home", "Flour water salt", ["food"], "Bread", "Recipes at home"),
    ("a6", "Sewing for beginners", "Needles and thread", ["craft"], "Thread", "Crafts"),
]  # fmt: skip


async def _seed() -> None:
    """Save the corpus."""
    for views, (key, title, body, tags, headline, heading) in enumerate(_CORPUS, start=1):
        article = FtsArticle(
            id=key, title=title, body=body, slug=key, meta={"headline": headline}, tags=tags, heading=heading, views=views
        )
        await article.save()


@contextlib.asynccontextmanager
async def _corpus(kind: str) -> AsyncIterator[Any]:
    """:func:`_fts_models`, ``FtsArticle``'s indexes defined in the fresh database, then the corpus saved."""
    async with _fts_models(kind) as client:
        await FtsArticle.define_indexes()
        await _seed()
        yield client


def _ids(rows: Sequence[Any]) -> list[str]:
    """The ids of *rows*, sorted."""
    return sorted(row.id for row in rows)


class TestSearchE2E:
    """``search()`` on every engine (SurrealDB 3.x and 2.x over WebSocket and HTTP, and the embedded engine): the
    corpus's matches, the terms of one text AND-ed, an empty text matching nothing, with the other clauses."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_corpus_matches(self, kind: str) -> None:
        async with _corpus(kind):
            title = await FtsArticle.objects().search(title="physics").exec()
            quantum = await FtsArticle.objects().search(title="quantum").exec()
            body = await FtsArticle.objects().search(body="physics").exec()
            headline = await FtsArticle.objects().search(**{"meta.headline": "rust"}).exec()
            tags = await FtsArticle.objects().search(tags="quantum").exec()
            heading = await FtsArticle.objects().search(heading="recipes").exec()
        assert _ids(title) == ["a1", "a2"]
        assert _ids(quantum) == ["a1", "a4"]
        assert _ids(body) == ["a2"]
        assert _ids(headline) == ["a1"]
        assert _ids(tags) == ["a1"]
        assert _ids(heading) == ["a2", "a5"]
        assert all(isinstance(article, FtsArticle) for article in title)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_terms_of_a_text_are_and_ed(self, kind: str) -> None:
        async with _corpus(kind):
            both = await FtsArticle.objects().search(title="quantum physics").exec()
            empty = await FtsArticle.objects().search(title="").exec()
            none = await FtsArticle.objects().search(title="knitting").exec()
        assert _ids(both) == ["a1"]
        assert empty == []
        assert none == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_with_filter_and_q(self, kind: str) -> None:
        async with _corpus(kind):
            filtered = await FtsArticle.objects().filter(views__gt=1).search(title="physics").exec()
            either = await FtsArticle.objects().filter(Q(views=4) | Q(views=5)).search(title="quantum").exec()
            text = await FtsArticle.objects().search(title="$title").variables(title="physics").exec()
        assert _ids(filtered) == ["a2"]
        assert _ids(either) == ["a4"]
        assert text == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_order_limit_and_offset(self, kind: str) -> None:
        async with _corpus(kind):
            first = await FtsArticle.objects().search(title="quantum").order_by("views").limit(1).exec()
            second = await FtsArticle.objects().search(title="quantum").order_by("views").limit(1).offset(1).exec()
            descending = await FtsArticle.objects().search(title="quantum").order_by("-views").exec()
        assert [article.id for article in first] == ["a1"]
        assert [article.id for article in second] == ["a4"]
        assert [article.id for article in descending] == ["a4", "a1"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_select_and_fetch(self, kind: str) -> None:
        async with _corpus(kind):
            titles = await FtsArticle.objects().search(title="physics").select("title").order_by("title").exec()
            fetched = await FtsArticle.objects().search(title="physics").fetch("tags").exec()
        assert titles == [{"title": "Cooking with physics"}, {"title": "Quantum physics for beginners"}]
        assert _ids(fetched) == ["a1", "a2"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_first_and_get_without_an_id(self, kind: str) -> None:
        async with _corpus(kind):
            first = await FtsArticle.objects().search(title="quantum").order_by("views").first()
            got = await FtsArticle.objects().search(title="cooking").get()
            with pytest.raises(SurrealDbError, match="More than one result found"):
                await FtsArticle.objects().search(title="quantum").get()
            with pytest.raises(SurrealDbNotFoundError):
                await FtsArticle.objects().search(title="knitting").first()
            by_id = await FtsArticle.objects().search(title="knitting").get("a3")
        assert first.id == "a1"
        assert got.id == "a2"
        assert by_id.id == "a3", "get(<id>) ignores search(), as it ignores filter()"


def _is_31(line: tuple[int, int]) -> bool:
    """Whether *line* is SurrealDB 3.1, whose answers to a search on a field without a full-text index differ."""
    return line == (3, 1)


async def _native_transactions(kind: str, client: Any) -> bool:
    """Whether ``transaction()`` on *kind* is interactive: WebSocket on SurrealDB 3.x."""
    return kind == "ws" and await _major(client) >= 3


class TestMissingIndexE2E:
    """A search on a field without a full-text index raises ``SurrealDbError`` naming the field, never the server's
    text — which on SurrealDB 3.x quotes the search text — on every engine, in a read, a count, a write by filter and a
    subquery; in a transaction, the commit fails with that error as its cause. Pinned (README row "``search()`` on a
    field without a full-text index"): SurrealDB 3.1 answers no record instead."""

    @pytest.mark.parametrize("field", ["slug", "views"])
    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_read(self, kind: str, field: str) -> None:
        async with _corpus(kind) as client:
            line = await _line(client)
            if _is_31(line):
                rows = await FtsArticle.objects().search(**{field: _SECRET}).exec()
                assert rows == [], "README row 'search() on a field without a full-text index': 3.1 answers no record"
                return
            with pytest.raises(SurrealDbError) as raised:
                await FtsArticle.objects().search(**{field: _SECRET}).exec()
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == _si1(field)
        assert _shown(raised.value, _SECRET) == []
        assert raised.value.__cause__ is None
        assert raised.value.__suppress_context__

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_count_an_exists_and_a_write_by_filter(self, kind: str) -> None:
        async with _corpus(kind) as client:
            line = await _line(client)
            searched = FtsArticle.objects().search(slug=_SECRET)
            errors: list[BaseException] = []
            answers: list[Any] = []
            for call in (searched.count, searched.exists, lambda: searched.bulk_update(views=9)):
                try:
                    answers.append(await call())
                except SurrealDbError as error:
                    errors.append(error)
            views = sorted(article.views for article in await FtsArticle.objects().all())
        if _is_31(line):
            assert answers == [0, False], "README row 'search() on a field without a full-text index': 3.1 answers no record"
            assert [str(error) for error in errors] == [_si1("slug")]
        else:
            assert answers == []
            assert [str(error) for error in errors] == [_si1("slug")] * 3
        assert all(_shown(error, _SECRET) == [] for error in errors)
        assert views == [1, 2, 3, 4, 5, 6]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_subquery_names_its_field(self, kind: str) -> None:
        async with _corpus(kind) as client:
            line = await _line(client)
            inner = FtsArticle.objects().search(views=_SECRET)
            queryset = FtsArticle.objects().search(title="physics").filter(id__in=Subquery(inner))
            if _is_31(line):
                assert await queryset.exec() == []
                return
            with pytest.raises(SurrealDbError) as raised:
                await queryset.exec()
        assert str(raised.value) == _si1("views")
        assert _shown(raised.value, _SECRET) == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_read_in_an_interactive_transaction(self, kind: str) -> None:
        """The read raises the ORM's error; the block that catches it and ends normally gets the commit's error,
        caused by that very error: no trace of the search text."""
        async with _corpus(kind) as client:
            if not await _native_transactions(kind, client):
                pytest.skip("requires SurrealDB 3.x native transactions")
            if _is_31(await _line(client)):
                async with SurrealDBConnectionManager.transaction("fts") as tx:
                    assert await FtsArticle.objects(tx=tx).search(slug=_SECRET).exec() == []
                return
            read: pytest.ExceptionInfo[SurrealDbError] | None = None
            with pytest.raises(SurrealDbError) as commit:
                async with SurrealDBConnectionManager.transaction("fts") as tx:
                    with pytest.raises(SurrealDbError) as read:
                        await FtsArticle.objects(tx=tx).search(slug=_SECRET).exec()
                    await FtsArticle(id="a7", title="Knitting basics", slug="a7").save(tx=tx)
            stored = _ids(await FtsArticle.objects().all())
        assert read is not None
        assert str(read.value) == _si1("slug")
        assert str(commit.value) == (
            "The transaction was cancelled: a statement in it failed; nothing was committed (SurrealDbError)"
        )
        assert commit.value.__cause__ is read.value
        assert _shown(commit.value, _SECRET) == []
        assert stored == ["a1", "a2", "a3", "a4", "a5", "a6"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_write_by_filter_in_a_transaction(self, kind: str) -> None:
        """A write by filter whose subquery searches a field without a full-text index, in a transaction (interactive:
        3.x over WebSocket; buffered: HTTP, 2.x, the embedded engine): the transaction's own error names the column and
        the operator, never the search text — in the write's error and in the commit's."""
        async with _corpus(kind) as client:
            line = await _line(client)
            interactive = await _native_transactions(kind, client)
            matched = FtsArticle.objects().search(slug=_SECRET)
            errors: list[BaseException] = []
            try:
                async with SurrealDBConnectionManager.transaction("fts") as tx:
                    try:
                        await FtsArticle.objects(tx=tx).filter(id__in=Subquery(matched)).bulk_update(views=9)
                    except SurrealDbError as error:
                        errors.append(error)
            except SurrealDbError as error:
                errors.append(error)
            views = sorted(article.views for article in await FtsArticle.objects().all())
        assert views == [1, 2, 3, 4, 5, 6]
        if _is_31(line):
            assert errors == [], "README row 'search() on a field without a full-text index': 3.1 answers no record"
            return
        failed = f"Transaction failed and rolled back: {_NO_INDEX_REASON}"
        if interactive:
            assert [str(error) for error in errors] == [
                failed,
                "The transaction was cancelled: a statement in it failed; nothing was committed (SurrealDbError)",
            ]
            assert errors[1].__cause__ is errors[0]
        else:
            assert [str(error) for error in errors] == [failed]
        assert all(_shown(error, _SECRET) == [] for error in errors)


@pytest.fixture
def _live_registry() -> Iterator[None]:
    """The live module's per-loop registries emptied after the test, as ``tests/test_live_queryset.py`` does."""
    from surreal_orm_lite import live

    yield
    live._SUBSCRIBERS.clear()
    live._RECENTLY_KILLED.clear()


def _live_selects(calls: list[tuple[str, tuple[object, ...]]]) -> list[object]:
    """The statements *calls* (:func:`_spy`'s) sent through ``query()``."""
    return [statement for method, statement in _calls(calls) if method == "query"]


@pytest.mark.usefixtures("_live_registry")
class TestLiveIndexCheckE2E:
    """Before a live query whose subquery searches, the ORM checks that each searched field has a full-text index and
    raises ``SurrealDbError`` naming the field otherwise, before the live query starts — without the index SurrealDB
    3.x would accept the live query and never notify. Over WebSocket, on every server line. Pinned (README row
    "``live()`` with a ``Subquery`` that searches a field without a full-text index"): SurrealDB 3.1 starts it."""

    async def test_q10_the_check_comes_before_the_live_select(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _corpus("ws") as client:
            calls = _spy(monkeypatch, client)
            async with _q7().live():
                sent = _live_selects(calls)
            monkeypatch.undo()
        assert sent == [_Q10, _Q8]

    @pytest.mark.parametrize("method", ["live", "watch"])
    async def test_a_field_without_an_index_is_refused_before_the_live_query(
        self, method: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The check raises the ORM's error naming the field and no live query is sent; a write to the table then
        succeeds. On SurrealDB 3.1, which answers the check, the live query starts (and ends with the block)."""
        async with _corpus("ws") as client:
            line = await _line(client)
            queryset = FtsArticle.objects().filter(id__in=Subquery(FtsArticle.objects().search(slug=_SECRET)))
            calls = _spy(monkeypatch, client)
            raised: pytest.ExceptionInfo[SurrealDbError] | None = None
            if _is_31(line):
                async with getattr(queryset, method)():
                    started = _live_selects(calls)
            else:
                with pytest.raises(SurrealDbError) as raised:
                    async with getattr(queryset, method)():
                        pass
                started = _live_selects(calls)
            monkeypatch.undo()
            await FtsArticle(id="a7", title="Knitting basics", slug="a7").save()
            stored = _ids(await FtsArticle.objects().all())
        check = _Q10.replace("title @0@", "slug @0@")
        assert stored == ["a1", "a2", "a3", "a4", "a5", "a6", "a7"]
        if _is_31(line):
            assert raised is None
            assert started[0] == check
            assert len(started) == 2 and str(started[1]).startswith("LIVE SELECT "), (
                "README row 'live() with a Subquery that searches a field without a full-text index': 3.1 starts it"
            )
            return
        assert raised is not None
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == _si1("slug")
        assert _shown(raised.value, _SECRET) == []
        assert started == [check]


# --- the search annotations on the engines --------------------------------------------------------------------------

#: The BM25 scores of ``search(title="physics")`` on the corpus, its index built once in a fresh database: the same on
#: every engine.
_PHYSICS_SCORES = {"a1": 0.543332, "a2": 0.612858}

#: The README rows the line pins back.
_ROW_AND = "README row 'Two search() fields AND-ed'"
_ROW_COMMON = "README row 'BM25 score of a term in more than half the documents (SearchScore)'"
_ROW_STATISTICS = "README row 'BM25 statistics after a rebuild, a redefinition or a deletion (SearchScore)'"

#: A rebuild of the title index, sent raw.
_REBUILD_TITLE = "REBUILD INDEX `fts_article_title` ON `fts_article`;"


def _approx(value: float) -> Any:
    """*value* to the relative precision scores agree to across engines."""
    return pytest.approx(value, rel=1e-5)


async def _scores(**field_queries: str) -> dict[str, float]:
    """The ``SearchScore(0)`` of each article ``search(**field_queries)`` matches, by id."""
    rows = await FtsArticle.objects().search(**field_queries).annotate(relevance=SearchScore(0)).exec()
    return {row.id: row.relevance for row in rows}


class TestAnnotateE2E:
    """The search annotations on every engine (SurrealDB 3.x and 2.x over WebSocket and HTTP, and the embedded
    engine): scores ordered and equal across engines on an index built once, highlights and offsets exact, tags given
    back exactly, the values carried by the instances and never written back."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_q3_score_highlight_and_offsets(self, kind: str) -> None:
        async with _corpus(kind):
            articles = await _q3("physics").exec()
        assert [article.id for article in articles] == ["a2", "a1"]
        assert all(isinstance(article, FtsArticle) for article in articles)
        assert [article.relevance for article in articles] == [_approx(0.612858), _approx(0.543332)]
        assert articles[0].relevance > articles[1].relevance > 0
        assert [article.snippet for article in articles] == [
            "Cooking with <b>physics</b>",
            "Quantum <b>physics</b> for beginners",
        ]
        assert [article.spans for article in articles] == [{"0": [{"s": 13, "e": 20}]}, {"0": [{"s": 8, "e": 15}]}]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_tags_are_given_back_exactly(self, kind: str) -> None:
        """The tags are bound query variables: a quote and a trailing backslash come back as they were given."""
        async with _corpus(kind):
            articles = await (
                FtsArticle.objects()
                .search(title="physics")
                .annotate(relevance=SearchScore(0), snippet=SearchHighlight("<em class='hit'>", "</em>\\", 0))
                .order_by("-relevance")
                .exec()
            )
        assert [article.snippet for article in articles] == [
            "Cooking with <em class='hit'>physics</em>\\",
            "Quantum <em class='hit'>physics</em>\\ for beginners",
        ]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_array_field_and_an_index_without_highlights(self, kind: str) -> None:
        async with _corpus(kind):
            tagged = await FtsArticle.objects().search(tags="quantum").annotate(snippet=SearchHighlight()).exec()
            body = await FtsArticle.objects().search(body="physics").annotate(snippet=SearchHighlight()).exec()
        assert [article.id for article in tagged] == ["a1"]
        assert isinstance(tagged[0].snippet, list)
        assert "<b>quantum</b> world" in tagged[0].snippet
        assert [article.snippet for article in body] == ["Heat and physics in the kitchen"], (
            "an index without HIGHLIGHTS gives the text unchanged"
        )

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_reference_names_its_field(self, kind: str) -> None:
        """Each annotation reads the field its reference names; offsets of an array field are keyed by the item's
        position; an index without ``HIGHLIGHTS`` gives no offsets."""
        async with _corpus(kind):
            articles = await (
                FtsArticle.objects()
                .search(tags="quantum")
                .search(heading="introduction")
                .annotate(snippet=SearchHighlight("[", "]", 1), spans=SearchOffsets(0))
                .exec()
            )
            body = await FtsArticle.objects().search(body="physics").annotate(spans=SearchOffsets(0)).exec()
        assert [(article.id, article.snippet, article.spans) for article in articles] == [
            ("a1", "[Introduction]", {"1": [{"s": 0, "e": 7}]})
        ]
        assert [(article.id, article.spans) for article in body] == [("a2", None)]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_values_are_never_written_back(self, kind: str) -> None:
        async with _corpus(kind) as client:
            article = await _q3("cooking").first()
            read = (article.id, article.relevance)
            dumped = article.model_dump()
            article.views = 20
            await article.update()
            await article.upsert()
            stored = await client.query("SELECT * FROM fts_article;", {})
        assert read[0] == "a2" and read[1] > 0
        assert not {"relevance", "snippet", "spans"} & set(dumped)
        assert all(not {"relevance", "snippet", "spans"} & set(row) for row in stored)
        assert sorted(row["views"] for row in stored) == [1, 3, 4, 5, 6, 20]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_rows_that_do_not_validate_are_dicts_holding_the_aliases(self, kind: str) -> None:
        async with _corpus(kind):
            rows = await (
                FtsArticle.objects()
                .search(title="physics")
                .select("title")
                .annotate(relevance=SearchScore(0))
                .order_by("-relevance")
                .exec()
            )
        assert [row["title"] for row in rows] == ["Cooking with physics", "Quantum physics for beginners"]
        assert all(set(row) == {"title", "relevance"} and row["relevance"] > 0 for row in rows)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_first_and_get_without_an_id(self, kind: str) -> None:
        async with _corpus(kind):
            first = await _q3("physics").first()
            got = (
                await FtsArticle.objects()
                .search(title="cooking")
                .annotate(relevance=SearchScore(0), snippet=SearchHighlight())
                .get()
            )
        assert (first.id, first.relevance, first.snippet) == ("a2", _approx(0.612858), "Cooking with <b>physics</b>")
        assert first.spans == {"0": [{"s": 13, "e": 20}]}
        assert (got.id, got.snippet) == ("a2", "<b>Cooking</b> with physics")
        assert got.relevance > 0


class TestSearchLinesE2E:
    """Pinned line facts on the corpus, each backing a README row: two ``search()`` fields AND-ed, the BM25 score of a
    term in more than half the documents, and the BM25 statistics after a rebuild or a deletion. Every score compared
    with a recorded value or across queries is read from the index built once in the test's fresh database."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_two_fields_and_ed(self, kind: str) -> None:
        """README row "Two ``search()`` fields AND-ed": only the records that match every field, each field scored as
        if searched alone; SurrealDB 3.1 can also return records that match only one of the fields, scored ``0.0`` on
        the other (here ``title`` alone)."""
        async with _corpus(kind) as client:
            line = await _line(client)
            rows = await (
                FtsArticle.objects().search(title="physics", body="kitchen").annotate(t=SearchScore(0), b=SearchScore(1)).exec()
            )
            title = await _scores(title="physics")
            body = await _scores(body="kitchen")
        found = {row.id: (row.t, row.b) for row in rows}
        if _is_31(line):
            assert sorted(found) == ["a1", "a2"], f"{_ROW_AND}: 3.1 also returns a record that matches one field only"
            assert found["a1"][1] == 0.0, f"{_ROW_AND}: 3.1 scores 0.0 the field that record does not match"
        else:
            assert sorted(found) == ["a2"], _ROW_AND
        assert found["a2"] == (_approx(title["a2"]), _approx(body["a2"])), f"{_ROW_AND}: each field scored alone"

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_term_in_more_than_half_the_documents(self, kind: str) -> None:
        """README row "BM25 score of a term in more than half the documents (``SearchScore``)": SurrealDB 3.x gives
        it no weight (``0.0``); 2.x and the embedded engine score it below zero, so a text holding it scores lower
        than without it. The records matched are the same."""
        async with _corpus(kind) as client:
            major = await _major(client)
            common = await _scores(body="and")
            mixed = await _scores(body="and physics")
            alone = await _scores(body="physics")
        assert sorted(common) == ["a2", "a3", "a4", "a6"]
        assert sorted(mixed) == sorted(alone) == ["a2"]
        if major >= 3:
            assert all(score == 0.0 for score in common.values()), f"{_ROW_COMMON}: 3.x scores 0.0"
            assert mixed["a2"] == _approx(alone["a2"]), f"{_ROW_COMMON}: on 3.x the term adds nothing"
        else:
            assert all(score < 0 for score in common.values()), f"{_ROW_COMMON}: 2.x scores below zero"
            assert mixed["a2"] < alone["a2"], f"{_ROW_COMMON}: on 2.x a text holding the term scores lower"

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_bm25_statistics_after_a_rebuild(self, kind: str) -> None:
        """README row "BM25 statistics after a rebuild, a redefinition or a deletion (``SearchScore``)": after a
        ``REBUILD INDEX``, SurrealDB 3.x gives the scores of the freshly built index; 2.x and the embedded engine count
        the records again, so the scores grow. The matches are unaffected."""
        async with _corpus(kind) as client:
            major = await _major(client)
            fresh = await _scores(title="physics")
            await _apply_ddl(client, _REBUILD_TITLE)
            rebuilt = await _scores(title="physics")
        assert fresh == {key: _approx(value) for key, value in _PHYSICS_SCORES.items()}, "an index built once"
        assert sorted(rebuilt) == ["a1", "a2"], f"{_ROW_STATISTICS}: the matches are unaffected"
        if major >= 3:
            assert rebuilt == {key: _approx(value) for key, value in fresh.items()}, (
                f"{_ROW_STATISTICS}: 3.x keeps its statistics in step with the records"
            )
        else:
            assert all(rebuilt[key] > fresh[key] for key in fresh), (
                f"{_ROW_STATISTICS}: 2.x and the embedded engine count the records again at a rebuild"
            )

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_bm25_statistics_after_a_deletion(self, kind: str) -> None:
        """README row "BM25 statistics after a rebuild, a redefinition or a deletion (``SearchScore``)": after a
        record is created then deleted, SurrealDB 3.x gives the scores of the freshly built index; 2.x and the embedded
        engine keep counting the deleted record, so the scores change. The matches are unaffected."""
        async with _corpus(kind) as client:
            major = await _major(client)
            fresh = await _scores(title="physics")
            knitting = FtsArticle(id="a7", title="Knitting basics", slug="a7")
            await knitting.save()
            await knitting.delete()
            after = await _scores(title="physics")
            stored = _ids(await FtsArticle.objects().all())
        assert fresh == {key: _approx(value) for key, value in _PHYSICS_SCORES.items()}, "an index built once"
        assert stored == ["a1", "a2", "a3", "a4", "a5", "a6"]
        assert sorted(after) == ["a1", "a2"], f"{_ROW_STATISTICS}: the matches are unaffected"
        if major >= 3:
            assert after == {key: _approx(value) for key, value in fresh.items()}, (
                f"{_ROW_STATISTICS}: 3.x keeps its statistics in step with the records"
            )
        else:
            assert all(after[key] > fresh[key] for key in fresh), (
                f"{_ROW_STATISTICS}: 2.x and the embedded engine keep counting a deleted record"
            )
