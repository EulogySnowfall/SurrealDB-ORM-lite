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
"""

import ast
import math
import pickle
from pathlib import Path
from typing import Any

import pytest

import surreal_orm_lite
from surreal_orm_lite import Analyzer, Index, SearchHighlight, SearchOffsets, SearchScore, search
from surreal_orm_lite.define_parser import parse_define_analyzer
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
