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
"""

import ast
import contextlib
import dataclasses
import math
import os
import pickle
import re
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import Field
from surrealdb import InternalError, NotAllowedError

import surreal_orm_lite
from surreal_orm_lite import (
    Analyzer,
    AnalyzerState,
    BaseSurrealModel,
    DatabaseIntrospector,
    Index,
    IndexState,
    SearchHighlight,
    SearchOffsets,
    SearchScore,
    SurrealConfigDict,
    SurrealDbAuthenticationError,
    SurrealDBConnectionManager,
    SurrealDbError,
    search,
)
from surreal_orm_lite.define_parser import DefineParseError, parse_define_analyzer, parse_define_index
from surreal_orm_lite.functions import normalize_function_name

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
