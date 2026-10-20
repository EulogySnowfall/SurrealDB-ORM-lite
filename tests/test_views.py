"""Materialized views (v0.32.0): a model whose table SurrealDB computes from a ``SELECT`` (``DEFINE TABLE … AS``).

Unit tests (no server): ``define_parser.blank_literals()`` (spec §6.2), the query check ``schema.check_view_query()``
with the exact texts of V1–V7 (spec §5.6; AC-2), the reading of a projection — ``view_query_outline()`` element by
element and ``view_query_hazards()`` on the grouped projections spec §3 measured (spec §6.1) — and the pure statements
``view_statement()`` / ``remove_view_statement()`` against the goldens of spec §5.3 (AC-4). The check catches mistakes
and is not an injection defence: every refused query below is a plain malformed query.

On the model (spec §5.1, §5.2; AC-1, AC-3, AC-4): ``SurrealConfigDict(view_query=…)`` checked at the ``class``
statement (V1–V10, ``schema.check_view_config()``), ``get_view_query()``, ``_is_view()``, inheritance and
``view_query=None``, and ``view_ddl()`` — pure, the goldens of spec §5.3, V11, E13, no connection needed.

The write guard (spec §5.4, §5.6 W1; AC-6): each of the 20 ORM writes of a view model raises ``TypeError`` before any
signal, transaction use or request — unit, and E2E on the connection ``"mv"`` (database ``mv_db``, spec §9.2) over
WebSocket, HTTP and ``mem://`` on both server lines, where the view's records stay as they were, the relation helpers
still write their edge table (M16), and a raw ``DELETE`` of the view is pinned per line (M6: refused from 3.2).

Applying and removing (spec §5.2, §5.6 G1–G4, S1–S3, §6.6; AC-5, AC-8, AC-9, AC-12, AC-17): the refusal reason of view
DDL kept whole (``redact=False``, D16), the table guard — ``read_table_definitions()``, ``table_kind()``,
``table_holds_records()`` on fake clients, then through ``define_view()``/``remove_view()``, one ``INFO FOR DB`` per
call —, and E2E on both lines: the view defined, read and kept up to date, removed, refused on a table that holds
records, named in every refusal, defined on the model's connection, and refused to a record user.
"""

import contextlib
import inspect
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from surrealdb import InternalError, NotAllowedError, NotFoundError, RecordID

import surreal_orm_lite
from surreal_orm_lite import (
    AuthenticatedUserMixin,
    BaseSurrealEdge,
    BaseSurrealModel,
    Computed,
    Event,
    SurrealConfigDict,
    SurrealDbAuthenticationError,
    SurrealDBConnectionManager,
    SurrealDbError,
    SurrealDbNotFoundError,
    Transaction,
    computed,
    define_parser,
    pre_delete,
    pre_save,
    pre_update,
    schema,
    utils,
)
from surreal_orm_lite.define_parser import blank_literals
from surreal_orm_lite.schema import (
    ViewElement,
    ViewHazard,
    ViewOutline,
    check_view_config,
    check_view_query,
    remove_view_statement,
    view_query_hazards,
    view_query_outline,
    view_statement,
)
from surreal_orm_lite.utils import apply_ddl_statements
from tests.conftest import default_connection_class

# --- the goldens of spec §5.3 ----------------------------------------------------------------------------------------

_STATS_QUERY = "SELECT status, count() AS total, math::sum(amount) AS revenue FROM orders GROUP BY status"
_PAID_QUERY = "SELECT customer, amount FROM orders WHERE status = 'paid'"
_TOTALS_QUERY = "SELECT count() AS orders, math::sum(amount) AS revenue FROM orders GROUP ALL"
_SELECT_QUERY = "  select * from orders\n  where amount > 1;  "

_GOLDEN_QUERIES = [_STATS_QUERY, _PAID_QUERY, _TOTALS_QUERY]


# --- blank_literals() ------------------------------------------------------------------------------------------------


class TestBlankLiterals:
    """Spec §6.2: the content of every literal and quoted identifier blanked, delimiters and length kept."""

    @pytest.mark.parametrize(
        ("text", "blanked"),
        [
            ("SELECT 'a(b' AS x, `g h` FROM t", "SELECT '   ' AS x, `   ` FROM t"),
            ("SELECT ⟨x y⟩ AS a FROM t", "SELECT ⟨   ⟩ AS a FROM t"),
            ('SELECT "y(" AS a FROM t', 'SELECT "  " AS a FROM t'),
            # Escaped quotes stay inside their literal.
            ("SELECT 'it\\'s' FROM t", "SELECT '     ' FROM t"),
            ('SELECT "a\\"b" FROM t', 'SELECT "    " FROM t'),
            ("SELECT `a\\`b` FROM t", "SELECT `    ` FROM t"),
            ("SELECT ⟨a\\⟩b⟩ FROM t", "SELECT ⟨    ⟩ FROM t"),
            # A quote of another kind inside a literal is data.
            ('SELECT "it\'s" FROM t WHERE a = \'say "x"\'', "SELECT \"    \" FROM t WHERE a = '       '"),
            # Comment markers and semicolons inside a literal are blanked too.
            ("SELECT * FROM t WHERE a = 'x; -- y'", "SELECT * FROM t WHERE a = '       '"),
            # Empty literals.
            ("SELECT '' AS a, `` FROM t", "SELECT '' AS a, `` FROM t"),
        ],
    )
    def test_blanks_the_content(self, text: str, blanked: str) -> None:
        assert blank_literals(text) == blanked
        assert len(blank_literals(text)) == len(text)

    @pytest.mark.parametrize(
        "text",
        ["SELECT status, count() AS total FROM orders GROUP BY status", "", "a(b)[c]{d}", "SELECT * FROM t\n  WHERE a > 1"],
    )
    def test_text_without_literals_is_unchanged(self, text: str) -> None:
        assert blank_literals(text) == text

    def test_an_unterminated_literal_blanks_the_rest(self) -> None:
        assert blank_literals("SELECT 'abc FROM t") == "SELECT '" + " " * len("abc FROM t")
        assert blank_literals("SELECT `abc") == "SELECT `   "

    def test_exported(self) -> None:
        assert "blank_literals" in define_parser.__all__
        assert "scan_fragment" in define_parser.__all__


# --- check_view_query(): V1–V7 ---------------------------------------------------------------------------------------

_KEY = "M: model_config['view_query']"
_V2 = f"{_KEY} is empty; it is the SELECT SurrealDB computes the view from."
_V3 = f"{_KEY} must be a SELECT statement (it starts with SELECT)."
_V5 = f"{_KEY} is one SELECT; it holds a ';' outside brackets."
_V6 = f"{_KEY} uses SELECT VALUE; a view stores records, so select fields (SELECT a, b FROM …)."


def _v4(reason: str) -> str:
    return f"{_KEY} is not SurrealQL the ORM can embed: {reason}."


def _v7(function: str) -> str:
    return (
        f"{_KEY} groups its records and calls {function}(), which SurrealDB cannot keep up to date in a view; use "
        "count(), math::sum(), math::mean(), math::min(), math::max(), time::min() or time::max()."
    )


#: ``(value, exception class, exact text)`` — one case per refusal of spec §5.6 V1–V7, with the inputs of AC-2.
_QUERY_ERRORS = [
    # V1
    pytest.param(1, TypeError, f"{_KEY} must be a str, got int.", id="V1-int"),
    pytest.param(None, TypeError, f"{_KEY} must be a str, got NoneType.", id="V1-none"),
    pytest.param(b"SELECT * FROM t", TypeError, f"{_KEY} must be a str, got bytes.", id="V1-bytes"),
    # V2
    pytest.param("  ", ValueError, _V2, id="V2-blank"),
    pytest.param("", ValueError, _V2, id="V2-empty"),
    pytest.param(" ; ", ValueError, _V2, id="V2-semicolon"),
    pytest.param(";", ValueError, _V2, id="V2-bare-semicolon"),
    # V3
    pytest.param("UPDATE t SET a = 1", ValueError, _V3, id="V3-update"),
    pytest.param("(SELECT * FROM t)", ValueError, _V3, id="V3-parenthesised"),
    pytest.param("SELECT", ValueError, _V3, id="V3-select-alone"),
    pytest.param("SELECT* FROM t", ValueError, _V3, id="V3-no-space"),
    # V4
    pytest.param("SELECT * FROM t WHERE (a", ValueError, _v4("its brackets are not balanced"), id="V4-brackets"),
    pytest.param(
        "SELECT * FROM t -- x",
        ValueError,
        _v4("it holds a SurrealQL comment marker (-- , #, // or /* */)"),
        id="V4-comment",
    ),
    pytest.param("SELECT *\x00 FROM t", ValueError, _v4("it holds a NUL character (\\x00)"), id="V4-nul"),
    pytest.param("SELECT * FROM t WHERE a = 'x", ValueError, _v4("a string literal is not terminated"), id="V4-literal"),
    pytest.param("SELECT `a FROM t", ValueError, _v4("an identifier is not terminated"), id="V4-identifier"),
    # V5
    pytest.param("SELECT 1; SELECT 2", ValueError, _V5, id="V5-two-statements"),
    pytest.param("SELECT * FROM t;;", ValueError, _V5, id="V5-two-semicolons"),
    # V6
    pytest.param("select value a from t", ValueError, _V6, id="V6-lower"),
    pytest.param("SELECT VALUE a FROM t", ValueError, _V6, id="V6-upper"),
    # V7
    pytest.param("SELECT g, math::median(v) AS m FROM t GROUP BY g", ValueError, _v7("math::median"), id="V7-median"),
    pytest.param("SELECT g, ARRAY::GROUP(v) AS m FROM t GROUP ALL", ValueError, _v7("array::group"), id="V7-upper"),
    pytest.param(
        "SELECT g, array::join(v, ',') AS a, math::median(v) AS m FROM t GROUP BY g",
        ValueError,
        _v7("array::join"),
        id="V7-first",
    ),
    pytest.param(
        "SELECT g, string::len(array::distinct(v)) AS d FROM t GROUP BY g",
        ValueError,
        _v7("array::distinct"),
        id="V7-nested",
    ),
    pytest.param("SELECT g, vector::sum(v) AS s FROM t GROUP BY g", ValueError, _v7("vector::sum"), id="V7-vector"),
]

#: ``(value, returned text)`` — accepted as AC-2 lists them.
_QUERY_ACCEPTED = [
    pytest.param("SELECT * FROM t;", "SELECT * FROM t", id="final-semicolon"),
    pytest.param("  SELECT * FROM t ;  ", "SELECT * FROM t", id="final-semicolon-spaced"),
    pytest.param(_SELECT_QUERY, "select * from orders\n  where amount > 1", id="lower-case-multi-line"),
    pytest.param("SELECT g, math::median(scores) AS m FROM t", "SELECT g, math::median(scores) AS m FROM t", id="not-grouped"),
    pytest.param(
        "SELECT g FROM t WHERE math::median(s) > 1 GROUP BY g",
        "SELECT g FROM t WHERE math::median(s) > 1 GROUP BY g",
        id="outside-the-projection",
    ),
    pytest.param(
        "SELECT g, math::max(array::len(array::distinct(tags))) AS m FROM t GROUP BY g",
        "SELECT g, math::max(array::len(array::distinct(tags))) AS m FROM t GROUP BY g",
        id="inside-an-aggregate",
    ),
    pytest.param(
        "SELECT g, count() AS n FROM t WHERE x = 'math::median(' GROUP BY g",
        "SELECT g, count() AS n FROM t WHERE x = 'math::median(' GROUP BY g",
        id="inside-a-literal",
    ),
    pytest.param(
        "SELECT * FROM t WHERE a = 'x; y -- z'", "SELECT * FROM t WHERE a = 'x; y -- z'", id="semicolon-comment-literal"
    ),
    pytest.param('SELECT * FROM t WHERE a = "x;"', 'SELECT * FROM t WHERE a = "x;"', id="semicolon-double-quoted-literal"),
    pytest.param(
        "SELECT status,\n       count() AS total\nFROM orders\nGROUP BY status",
        "SELECT status,\n       count() AS total\nFROM orders\nGROUP BY status",
        id="several-lines",
    ),
    pytest.param("\tSELECT\n*\nFROM t\n", "SELECT\n*\nFROM t", id="tabs-and-newlines"),
    pytest.param(
        "SELECT * FROM t WHERE a IN [1, 2]; ", "SELECT * FROM t WHERE a IN [1, 2]", id="final-semicolon-after-bracket"
    ),
    pytest.param("SELECT * FROM t WHERE (a; b)", "SELECT * FROM t WHERE (a; b)", id="semicolon-inside-brackets"),
    *[pytest.param(query, query, id=f"golden-{index}") for index, query in enumerate(_GOLDEN_QUERIES)],
]


class TestViewQueryCheck:
    """AC-2: ``check_view_query()`` — V1–V7 with their exact texts, the accepted forms, one final ``;`` dropped."""

    @pytest.mark.parametrize(("value", "error", "text"), _QUERY_ERRORS)
    def test_refusals(self, value: object, error: type[Exception], text: str) -> None:
        with pytest.raises(error) as raised:
            check_view_query(value, model="M")
        assert type(raised.value) is error
        assert str(raised.value) == text

    @pytest.mark.parametrize(("value", "text"), _QUERY_ACCEPTED)
    def test_accepted(self, value: str, text: str) -> None:
        assert check_view_query(value, model="M") == text

    def test_the_model_name_starts_the_message(self) -> None:
        with pytest.raises(ValueError, match=r"^OrderStats: model_config\['view_query'\] is empty;"):
            check_view_query(" ", model="OrderStats")

    def test_v4_chains_the_parse_error(self) -> None:
        with pytest.raises(ValueError) as raised:
            check_view_query("SELECT * FROM t WHERE (a", model="M")
        assert isinstance(raised.value.__cause__, define_parser.DefineParseError)

    def test_only_one_final_semicolon_is_dropped(self) -> None:
        assert check_view_query("SELECT * FROM t ; ", model="M") == "SELECT * FROM t"
        with pytest.raises(ValueError, match="holds a ';' outside brackets"):
            check_view_query("SELECT * FROM t ; ;", model="M")

    def test_every_c1_hazard_is_accepted(self) -> None:
        """C1 is a ``define_view()`` check of the connection's line, not a class-time one (D8)."""
        for query, *_ in [*_HAZARD_QUERIES, *[(_element_query(element), None) for element, _ in _HAZARD_ELEMENTS]]:
            assert check_view_query(query, model="M") == query


# --- view_query_outline() --------------------------------------------------------------------------------------------


def _element(
    text: str,
    alias: str | None = None,
    aggregate: str | None = None,
    calls: tuple[str, ...] = (),
    fields: tuple[str, ...] = (),
    opaque: bool = False,
) -> ViewElement:
    return ViewElement(text, alias, aggregate, calls, fields, opaque)


class TestViewOutline:
    """Spec §6.1: ``view_query_outline()`` element by element (values of the design's prototype)."""

    def test_golden_grouped(self) -> None:
        assert view_query_outline(_STATS_QUERY) == ViewOutline(
            grouped=True,
            group=("status",),
            elements=(
                ViewElement("status", None, None, (), ("status",), False),
                ViewElement("count()", "total", "count", ("count",), (), False),
                ViewElement("math::sum(amount)", "revenue", "math::sum", ("math::sum",), (), False),
            ),
        )

    def test_golden_group_all(self) -> None:
        assert view_query_outline(_TOTALS_QUERY) == ViewOutline(
            grouped=True,
            group=(),
            elements=(
                _element("count()", "orders", "count", ("count",)),
                _element("math::sum(amount)", "revenue", "math::sum", ("math::sum",)),
            ),
        )

    def test_golden_where(self) -> None:
        assert view_query_outline(_PAID_QUERY) == ViewOutline(
            grouped=False,
            group=(),
            elements=(_element("customer", fields=("customer",)), _element("amount", fields=("amount",))),
        )

    def test_aliases_sub_select_and_quoted_field(self) -> None:
        outline = view_query_outline(
            "SELECT c.country AS cc, count() AS n, (SELECT count() FROM other GROUP ALL) AS o, `my f` AS f FROM src GROUP BY cc"
        )
        assert outline.grouped is True
        assert outline.group == ("cc",)
        assert outline.elements == (
            ViewElement("c.country", "cc", None, (), ("c.country",), False),
            ViewElement("count()", "n", "count", ("count",), (), False),
            ViewElement("(SELECT count() FROM other GROUP ALL)", "o", None, (), (), True),
            ViewElement("`my f`", "f", None, (), (), False),
        )

    def test_objects_casts_parameters_and_if(self) -> None:
        outline = view_query_outline(
            "SELECT g, { s: math::sum(v) } AS x, <float> v AS y, $value AS p, IF g = 'a' THEN 1 ELSE 0 END AS f "
            "FROM src GROUP BY g, c.country"
        )
        assert outline.grouped is True
        assert outline.group == ("g", "c.country")
        assert outline.elements == (
            _element("g", fields=("g",)),
            _element("{ s: math::sum(v) }", "x", None, ("math::sum",), ("v",)),
            _element("<float> v", "y", fields=("v",)),
            _element("$value", "p"),
            _element("IF g = 'a' THEN 1 ELSE 0 END", "f", fields=("g",)),
        )

    def test_function_names_are_lower_cased(self) -> None:
        element = view_query_outline("SELECT g, MATH::STDDEV(v) AS s FROM t GROUP BY g").elements[1]
        assert element == _element("MATH::STDDEV(v)", "s", "math::stddev", ("math::stddev",))
        assert view_query_outline("SELECT COUNT() AS n FROM t GROUP ALL").elements[0].aggregate == "count"

    def test_nested_calls_in_order(self) -> None:
        element = view_query_outline("SELECT g, math::max(array::len(array::distinct(tags))) AS m FROM t GROUP BY g").elements[
            1
        ]
        assert element == _element(
            "math::max(array::len(array::distinct(tags)))",
            "m",
            "math::max",
            ("math::max", "array::len", "array::distinct"),
        )

    def test_an_expression_around_an_aggregate_is_not_an_aggregate(self) -> None:
        element = view_query_outline("SELECT g, math::max(v) - math::min(v) AS d FROM t GROUP BY g").elements[1]
        assert element == _element("math::max(v) - math::min(v)", "d", None, ("math::max", "math::min"), ("v", "v"))

    def test_a_quoted_group_by_does_not_group(self) -> None:
        outline = view_query_outline("SELECT `group by` AS x FROM t")
        assert outline.grouped is False
        assert outline.group == ()
        assert outline.elements == (_element("`group by`", "x"),)

    def test_a_group_by_inside_a_literal_does_not_group(self) -> None:
        assert view_query_outline("SELECT g FROM t WHERE x = 'GROUP BY y'").grouped is False

    def test_a_multi_line_lower_case_group_by_groups(self) -> None:
        outline = view_query_outline("select g,\n  count() as n\nfrom t\ngroup\n  by g")
        assert outline.grouped is True
        assert outline.group == ("g",)
        assert outline.elements == (_element("g", fields=("g",)), _element("count()", "n", "count", ("count",)))

    def test_a_call_inside_a_literal_is_no_call(self) -> None:
        element = view_query_outline("SELECT g, 'math::median(' AS m FROM t GROUP BY g").elements[1]
        assert element == _element("'math::median('", "m")

    def test_quoted_aliases_and_group_items(self) -> None:
        outline = view_query_outline("SELECT `status`, count() AS `my n`, g AS ⟨h i⟩ FROM t GROUP BY `status`, ⟨h i⟩")
        assert outline.group == ("status", "h i")
        assert [element.alias for element in outline.elements] == [None, "my n", "h i"]
        assert outline.elements[0] == _element("`status`")

    def test_without_from(self) -> None:
        outline = view_query_outline("SELECT 1 AS one, time::now() AS now")
        assert outline.grouped is False
        assert outline.elements == (_element("1", "one"), _element("time::now()", "now", calls=("time::now",)))

    def test_a_from_in_a_function_path_is_not_the_from_clause(self) -> None:
        """``time::from::unix()`` (SurrealDB 2.x) holds the word ``from``: the projection does not end there."""
        outline = view_query_outline("SELECT g, time::from::unix(v) AS t, count() AS n FROM src GROUP BY g")
        assert outline.elements == (
            _element("g", fields=("g",)),
            _element("time::from::unix(v)", "t", calls=("time::from::unix",), fields=("v",)),
            _element("count()", "n", "count", ("count",)),
        )

    def test_record_ids_and_object_keys_are_not_fields(self) -> None:
        element = view_query_outline("SELECT g, { k: t:1, n: a.b } AS o FROM src GROUP BY g").elements[1]
        assert element.fields == ("a.b",)

    def test_graph_paths_are_opaque(self) -> None:
        element = view_query_outline("SELECT g, ->knows->person AS p FROM src GROUP BY g").elements[1]
        assert element == _element("->knows->person", "p", opaque=True)

    def test_hashable_and_frozen(self) -> None:
        outline = view_query_outline(_STATS_QUERY)
        assert hash(outline) == hash(view_query_outline(_STATS_QUERY))
        with pytest.raises(AttributeError):
            outline.grouped = False  # type: ignore[misc]


# --- view_query_hazards() --------------------------------------------------------------------------------------------


def _element_query(element: str) -> str:
    return f"SELECT g, {element} AS x FROM src GROUP BY g"


#: ``(element, (kind, detail) or None)`` for ``SELECT g, <element> AS x FROM src GROUP BY g`` — spec §3's table.
_HAZARD_ELEMENTS: list[tuple[str, tuple[str, str] | None]] = [
    # Maintained on every line: one whole aggregate call, an expression of the group fields, a constant, a parameter,
    # a sub-select.
    *[
        (element, None)
        for element in [
            "math::sum(v)",
            "math::sum(v * 2)",
            "math::sum(math::abs(v))",
            "count(v > 15)",
            "string::uppercase(g)",
            "g + 'x'",
            "math::sum(v ?? 0)",
            "time::max(t)",
            "IF g = 'a' THEN 1 ELSE 0 END",
            "$value",
            "(SELECT count() FROM other GROUP ALL)",
            "math::max(array::len(array::distinct(tags)))",
            "time::now()",
            "'math::sum(v)'",
            "g.x",
            "<string> g",
            "COUNT()",
            "`v`",
            "count()",
        ]
    ],
    # An expression around an aggregate.
    ("math::round(math::mean(v))", ("wrapped", "math::mean")),
    ("math::fixed(math::sum(v), 2)", ("wrapped", "math::sum")),
    ("math::max(v) - math::min(v)", ("wrapped", "math::max")),
    ("math::sum(v) / count()", ("wrapped", "math::sum")),
    ("count() * 2", ("wrapped", "count")),
    ("count() + 0", ("wrapped", "count")),
    ("IF count() > 1 THEN 'a' ELSE 'b' END", ("wrapped", "count")),
    ("(math::sum(v))", ("wrapped", "math::sum")),
    ("-math::sum(v)", ("wrapped", "math::sum")),
    ("math::abs(math::sum(v))", ("wrapped", "math::sum")),
    ("math::sum(v) + math::sum(w)", ("wrapped", "math::sum")),
    ("<float> math::sum(v)", ("wrapped", "math::sum")),
    ("math::sum(v) ?? 0", ("wrapped", "math::sum")),
    ("[math::sum(v)]", ("wrapped", "math::sum")),
    ("{ s: math::sum(v) }", ("wrapped", "math::sum")),
    ("MATH::SUM(v) * 2", ("wrapped", "math::sum")),
    ("math::round(math::stddev(v))", ("wrapped", "math::stddev")),
    # A whole SurrealDB 3.x-only aggregate.
    ("math::stddev(v)", ("aggregate", "math::stddev")),
    ("math::variance(v)", ("aggregate", "math::variance")),
    # A field outside GROUP BY and outside an aggregate.
    ("math::percentile(v, 50)", ("field", "v")),
    ("math::mode(v)", ("field", "v")),
    ("array::len(v)", ("field", "v")),
    ("v", ("field", "v")),
    ("v * 2", ("field", "v")),
    ("time::from::unix(v)", ("field", "v")),
]

#: ``(query, expected hazards)`` — whole queries.
_HAZARD_QUERIES: list[tuple[str, tuple[ViewHazard, ...]]] = [
    (
        "SELECT time::month(t) AS m, count() AS n FROM src GROUP BY m",
        (ViewHazard("alias", "time::month(t)", "m"),),
    ),
    (
        "SELECT string::uppercase(g) AS gg, count() AS n FROM src GROUP BY gg",
        (ViewHazard("alias", "string::uppercase(g)", "gg"),),
    ),
    ("SELECT c.country AS cc, count() AS n FROM src GROUP BY cc", (ViewHazard("alias", "c.country", "cc"),)),
    ("SELECT g AS h, count() AS n FROM src GROUP BY h", (ViewHazard("alias", "g", "h"),)),
    ("SELECT g AS ⟨h⟩, count() AS n FROM src GROUP BY `h`", (ViewHazard("alias", "g", "h"),)),
    ("SELECT g AS h, count() AS n FROM src GROUP BY g", ()),
    ("SELECT g AS g, count() AS n FROM src GROUP BY g", ()),
    ("SELECT c.country, count() AS n FROM src GROUP BY c.country", ()),
    ("SELECT *, count() AS n FROM src GROUP BY g", (ViewHazard("field", "*", "*"),)),
    ("SELECT math::sum(v) / count() AS x FROM src GROUP ALL", (ViewHazard("wrapped", "math::sum(v) / count()", "math::sum"),)),
    ("SELECT count() * 2 AS x FROM src GROUP ALL", (ViewHazard("wrapped", "count() * 2", "count"),)),
    ("SELECT math::percentile(v, 50) AS x FROM src GROUP ALL", (ViewHazard("field", "math::percentile(v, 50)", "v"),)),
    ("SELECT math::stddev(v) AS x FROM src GROUP ALL", (ViewHazard("aggregate", "math::stddev(v)", "math::stddev"),)),
    ("SELECT g, count() AS n FROM src GROUP ALL", (ViewHazard("field", "g", "g"),)),
    ("SELECT id, math::sum(v) AS x FROM src", ()),
    ("SELECT v, v * 2 AS w FROM src WHERE v > 1", ()),
    *[(query, ()) for query in _GOLDEN_QUERIES],
    ("SELECT `status`, count() AS n FROM orders GROUP BY `status`", ()),
    ("SELECT status, count() AS n FROM orders WHERE math::sum(x) > 1 GROUP BY status", ()),
    ("select status,\n  count() as n,\n  math::sum(amount) as revenue\nfrom orders\ngroup by status", ()),
    ("SELECT g, count() AS `my n` FROM src GROUP BY g", ()),
    ("SELECT g, count() AS n FROM src WHERE x = 'GROUP BY y' GROUP BY g", ()),
    ("SELECT g, count() AS n, $value AS p FROM src GROUP BY g", ()),
    ("SELECT g, c.country.code AS cc, count() AS n FROM src GROUP BY g, c.country", ()),
    ("SELECT g, gx AS x, count() AS n FROM src GROUP BY g", (ViewHazard("field", "gx", "gx"),)),
    # Every element yields one hazard at most, in projection order.
    (
        "SELECT g, count() * 2 AS a, v AS b FROM src GROUP BY g",
        (ViewHazard("wrapped", "count() * 2", "count"), ViewHazard("field", "v", "v")),
    ),
    (
        "SELECT math::stddev(v) AS s, (v * 2) / count() AS r, v AS b FROM src GROUP ALL",
        (
            ViewHazard("aggregate", "math::stddev(v)", "math::stddev"),
            ViewHazard("wrapped", "(v * 2) / count()", "count"),
            ViewHazard("field", "v", "v"),
        ),
    ),
]


class TestViewHazards:
    """Spec §6.1 rules 1–4: the grouped projections SurrealDB 2.x computes per source record (C1's reading)."""

    @pytest.mark.parametrize(("element", "expected"), _HAZARD_ELEMENTS, ids=[element for element, _ in _HAZARD_ELEMENTS])
    def test_element(self, element: str, expected: tuple[str, str] | None) -> None:
        hazards = view_query_hazards(view_query_outline(_element_query(element)))
        if expected is None:
            assert hazards == ()
        else:
            kind, detail = expected
            assert hazards == (ViewHazard(kind, element, detail),)  # type: ignore[arg-type]

    @pytest.mark.parametrize(("query", "expected"), _HAZARD_QUERIES, ids=[query for query, _ in _HAZARD_QUERIES])
    def test_query(self, query: str, expected: tuple[ViewHazard, ...]) -> None:
        assert view_query_hazards(view_query_outline(query)) == expected

    def test_not_grouped_has_no_hazard(self) -> None:
        assert view_query_hazards(view_query_outline("SELECT math::stddev(v) AS s, v FROM src")) == ()


# --- view_statement() / remove_view_statement() ----------------------------------------------------------------------


class TestViewStatements:
    """AC-4: the golden statements of spec §5.3, pure; E12/E13 as v0.31.0 words them."""

    @pytest.mark.parametrize(
        ("table", "query", "overwrite", "statement"),
        [
            (
                "order_stats",
                _STATS_QUERY,
                True,
                "DEFINE TABLE OVERWRITE `order_stats` AS (SELECT status, count() AS total, math::sum(amount) AS revenue "
                "FROM orders GROUP BY status);",
            ),
            (
                "order_stats",
                _STATS_QUERY,
                False,
                "DEFINE TABLE IF NOT EXISTS `order_stats` AS (SELECT status, count() AS total, math::sum(amount) AS "
                "revenue FROM orders GROUP BY status);",
            ),
            (
                "paid_orders",
                _PAID_QUERY,
                True,
                "DEFINE TABLE OVERWRITE `paid_orders` AS (SELECT customer, amount FROM orders WHERE status = 'paid');",
            ),
            (
                "order_totals",
                _TOTALS_QUERY,
                True,
                "DEFINE TABLE OVERWRITE `order_totals` AS (SELECT count() AS orders, math::sum(amount) AS revenue FROM "
                "orders GROUP ALL);",
            ),
        ],
    )
    def test_golden_statements(self, table: str, query: str, overwrite: bool, statement: str) -> None:
        assert view_statement(table, query, overwrite) == statement

    def test_keyword_table_and_raw_newline(self) -> None:
        query = check_view_query(_SELECT_QUERY, model="M")
        assert view_statement("select", query, True) == (
            "DEFINE TABLE OVERWRITE `select` AS (select * from orders\n  where amount > 1);"
        )

    def test_deterministic(self) -> None:
        assert view_statement("order_stats", _STATS_QUERY, True) == view_statement("order_stats", _STATS_QUERY, True)

    @pytest.mark.parametrize(
        ("missing_ok", "statement"),
        [(True, "REMOVE TABLE IF EXISTS `order_stats`;"), (False, "REMOVE TABLE `order_stats`;")],
    )
    def test_removal_statement(self, missing_ok: bool, statement: str) -> None:
        assert remove_view_statement("order_stats", missing_ok) == statement

    def test_removal_quotes_a_keyword_name(self) -> None:
        assert remove_view_statement("select", False) == "REMOVE TABLE `select`;"

    @pytest.mark.parametrize(
        ("overwrite", "text"),
        [
            (1, "overwrite= must be a bool, got int."),
            ("yes", "overwrite= must be a bool, got str."),
            (None, "overwrite= must be a bool, got NoneType."),
        ],
    )
    def test_e13_overwrite(self, overwrite: object, text: str) -> None:
        with pytest.raises(TypeError) as raised:
            view_statement("order_stats", _STATS_QUERY, overwrite)
        assert type(raised.value) is TypeError
        assert str(raised.value) == text

    @pytest.mark.parametrize(
        ("missing_ok", "text"),
        [
            ("yes", "missing_ok= must be a bool, got str."),
            (1, "missing_ok= must be a bool, got int."),
            (None, "missing_ok= must be a bool, got NoneType."),
        ],
    )
    def test_e13_missing_ok(self, missing_ok: object, text: str) -> None:
        with pytest.raises(TypeError) as raised:
            remove_view_statement("order_stats", missing_ok)
        assert type(raised.value) is TypeError
        assert str(raised.value) == text

    @pytest.mark.parametrize(
        ("table", "error", "text"),
        [
            (1, TypeError, "Table name must be a str, got int."),
            (
                "a b",
                ValueError,
                "Invalid table name 'a b': an identifier is expected (letters, digits, underscores; not starting with "
                "a digit).",
            ),
        ],
    )
    def test_table_name_checked(self, table: Any, error: type[Exception], text: str) -> None:
        for render in (lambda: view_statement(table, _STATS_QUERY, True), lambda: remove_view_statement(table, True)):
            with pytest.raises(error) as raised:
                render()
            assert str(raised.value) == text


# --- the model: SurrealConfigDict(view_query=…) (spec §5.1, §5.2; AC-1, AC-3, AC-4) ---------------------------------


class OrderStats(BaseSurrealModel):
    """The grouped golden of spec §5.3."""

    model_config = SurrealConfigDict(table_name="order_stats", view_query=_STATS_QUERY)

    id: RecordID | None = None
    status: str
    total: int
    revenue: float


class PaidOrder(BaseSurrealModel):
    """The ``WHERE`` golden of spec §5.3: a plain view keeps the source ids."""

    model_config = SurrealConfigDict(table_name="paid_orders", view_query=_PAID_QUERY)

    id: RecordID | None = None
    customer: str | None = None
    amount: float


class OrderTotals(BaseSurrealModel):
    """The ``GROUP ALL`` golden of spec §5.3."""

    model_config = SurrealConfigDict(table_name="order_totals", view_query=_TOTALS_QUERY)

    id: RecordID | None = None
    orders: int
    revenue: float


class SelectView(BaseSurrealModel):
    """A keyword table name and a query on several lines with a final ``;`` (spec §5.3: rendering only, M20)."""

    model_config = SurrealConfigDict(table_name="select", view_query=_SELECT_QUERY)

    id: RecordID | None = None


class PlainOrder(BaseSurrealModel):
    """An ordinary model: not a view."""

    model_config = SurrealConfigDict(table_name="plain_orders")

    id: str | None = None
    status: str


_STATS_DDL = (
    "DEFINE TABLE OVERWRITE `order_stats` AS (SELECT status, count() AS total, math::sum(amount) AS revenue FROM orders "
    "GROUP BY status);"
)


def _v11(model: str, method: str) -> str:
    return f'{model} is not a materialized view: set model_config = SurrealConfigDict(view_query="SELECT …") to use {method}.'


def _v8(model: str) -> str:
    return (
        f"{model}: view_query is not supported on an edge model (BaseSurrealEdge): SurrealDB computes a view's records, "
        "the ORM never relates them."
    )


def _v9(model: str) -> str:
    return (
        f"{model}: view_query is not supported with AuthenticatedUserMixin: signing up writes a record, and SurrealDB "
        "computes a view's records."
    )


def _v10(model: str, names: str) -> str:
    return (
        f"{model}: view_query is not supported with computed fields ({names}): a view's columns come from its SELECT, "
        "so compute them there."
    )


#: ``(value, exception class, text for the model M)`` — one case per query refusal V1–V7, raised at the ``class``
#: statement (AC-2, AC-3).
_CLASS_QUERY_ERRORS = [
    pytest.param(1, TypeError, f"{_KEY} must be a str, got int.", id="V1"),
    pytest.param("  ", ValueError, _V2, id="V2"),
    pytest.param("UPDATE t SET a = 1", ValueError, _V3, id="V3"),
    pytest.param("SELECT * FROM t WHERE (a", ValueError, _v4("its brackets are not balanced"), id="V4"),
    pytest.param("SELECT 1; SELECT 2", ValueError, _V5, id="V5"),
    pytest.param("select value a from t", ValueError, _V6, id="V6"),
    pytest.param("SELECT g, math::median(v) AS m FROM t GROUP BY g", ValueError, _v7("math::median"), id="V7"),
]


class TestViewConfig:
    """AC-2, AC-3: ``SurrealConfigDict(view_query=…)`` is checked at the ``class`` statement — V1–V7 on the query, then
    V8 (edge), V9 (``AuthenticatedUserMixin``), V10 (computed fields) — and inherited by subclasses."""

    @pytest.mark.parametrize(("value", "error", "text"), _CLASS_QUERY_ERRORS)
    def test_query_refused_at_the_class_statement(self, value: object, error: type[Exception], text: str) -> None:
        with pytest.raises(error) as raised:

            class BadView(BaseSurrealModel):
                model_config = SurrealConfigDict(table_name="bad_view", view_query=value)  # type: ignore[typeddict-item]

                id: RecordID | None = None

        assert type(raised.value) is error
        assert str(raised.value) == text.replace("M: ", "BadView: ", 1)

    def test_v8_on_an_edge_model(self) -> None:
        with pytest.raises(TypeError) as raised:

            class WatchesView(BaseSurrealEdge):
                model_config = SurrealConfigDict(table_name="watches_view", view_query="SELECT * FROM watches")

        assert type(raised.value) is TypeError
        assert str(raised.value) == _v8("WatchesView")

    def test_the_query_is_checked_before_the_model(self) -> None:
        """``check_view_query()`` first, then V8–V10 (spec §6.1): an edge with an empty query gets V2."""
        with pytest.raises(ValueError) as raised:

            class EmptyEdgeView(BaseSurrealEdge):
                model_config = SurrealConfigDict(table_name="empty_edge_view", view_query=" ; ")

        assert str(raised.value) == _V2.replace("M: ", "EmptyEdgeView: ", 1)

    def test_v9_with_the_mixin_first(self) -> None:
        with pytest.raises(TypeError) as raised:

            class MemberView(AuthenticatedUserMixin, BaseSurrealModel):
                model_config = SurrealConfigDict(table_name="member_view", view_query="SELECT * FROM member")

                id: str | None = None

        assert type(raised.value) is TypeError
        assert str(raised.value) == _v9("MemberView")

    def test_v9_with_the_mixin_second(self) -> None:
        with pytest.raises(TypeError) as raised:

            class MemberView(BaseSurrealModel, AuthenticatedUserMixin):
                model_config = SurrealConfigDict(table_name="member_view", view_query="SELECT * FROM member")

                id: str | None = None

        assert type(raised.value) is TypeError
        assert str(raised.value) == _v9("MemberView")

    def test_v10_lists_the_computed_fields_in_declaration_order(self) -> None:
        with pytest.raises(TypeError) as raised:

            class ScoredView(BaseSurrealModel):
                model_config = SurrealConfigDict(table_name="scored_view", view_query="SELECT * FROM t")

                id: RecordID | None = None
                a: Computed[int] = computed("1")
                b: Computed[str] = computed("'x'")

        assert type(raised.value) is TypeError
        assert str(raised.value) == _v10("ScoredView", "'a', 'b'")

    def test_v10_on_a_subclass_adding_a_computed_field(self) -> None:
        with pytest.raises(TypeError) as raised:

            class ScoredStats(OrderStats):
                model_config = SurrealConfigDict(table_name="scored_stats")

                score: Computed[float] = computed("revenue / total")

        assert type(raised.value) is TypeError
        assert str(raised.value) == _v10("ScoredStats", "'score'")

    def test_a_c1_hazard_is_accepted_at_the_class_statement(self) -> None:
        """C1 depends on the connection's line: ``define_view()`` checks it, never the ``class`` statement (D8)."""

        class AverageView(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_name="average_view", view_query="SELECT g, math::sum(v) / count() AS a FROM src GROUP BY g"
            )

            id: RecordID | None = None

        assert AverageView._is_view() is True
        assert AverageView.get_view_query() == "SELECT g, math::sum(v) / count() AS a FROM src GROUP BY g"

    def test_get_view_query(self) -> None:
        assert OrderStats.get_view_query() == _STATS_QUERY
        assert PaidOrder.get_view_query() == _PAID_QUERY
        assert OrderTotals.get_view_query() == _TOTALS_QUERY
        # Stripped, one final ";" dropped, the inner text kept as written.
        assert SelectView.get_view_query() == "select * from orders\n  where amount > 1"
        assert PlainOrder.get_view_query() is None
        assert BaseSurrealModel.get_view_query() is None

    def test_is_view(self) -> None:
        for model in (OrderStats, PaidOrder, OrderTotals, SelectView):
            assert model._is_view() is True, model.__name__
        for ordinary in (PlainOrder, BaseSurrealModel, BaseSurrealEdge):
            assert ordinary._is_view() is False, ordinary.__name__

    def test_a_subclass_inherits_view_query(self) -> None:
        class ArchivedStats(OrderStats):
            model_config = SurrealConfigDict(table_name="archived_stats")

        assert ArchivedStats._is_view() is True
        assert ArchivedStats.get_view_query() == _STATS_QUERY
        assert ArchivedStats.view_ddl() == _STATS_DDL.replace("`order_stats`", "`archived_stats`")
        # The parent is unchanged.
        assert OrderStats.view_ddl() == _STATS_DDL

    def test_a_subclass_without_its_own_table_reads_the_same_view(self) -> None:
        class StatsAgain(OrderStats):
            pass

        assert StatsAgain._is_view() is True
        assert StatsAgain.view_ddl() == _STATS_DDL

    def test_view_query_none_makes_a_subclass_ordinary(self) -> None:
        class OrdinaryStats(OrderStats):
            model_config = SurrealConfigDict(table_name="ordinary_stats", view_query=None)

            score: Computed[float] = computed("revenue / total")

        assert OrdinaryStats._is_view() is False
        assert OrdinaryStats.get_view_query() is None
        with pytest.raises(TypeError) as raised:
            OrdinaryStats.view_ddl()
        assert str(raised.value) == _v11("OrdinaryStats", "view_ddl()")
        # Its subclass may set a view again.
        assert OrderStats._is_view() is True

    def test_an_ordinary_model_is_unaffected(self) -> None:
        class ComputedOrder(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="computed_orders")

            id: str | None = None
            amount: float
            doubled: Computed[float] = computed("amount * 2")

        assert ComputedOrder._is_view() is False
        assert ComputedOrder.get_computed_fields() == {"doubled": "amount * 2"}


class _Probe:
    """A stand-in model class for :func:`check_view_config` called directly."""


class TestCheckViewConfig:
    """Spec §6.1: ``check_view_config()`` — the query, then V8, V9, V10, each naming the model."""

    def test_returns_none_for_an_accepted_view(self) -> None:
        assert check_view_config(_Probe, _STATS_QUERY, edge=False, user=False, computed=()) is None

    @pytest.mark.parametrize(
        ("options", "text"),
        [
            ({"edge": True, "user": False, "computed": ()}, _v8("_Probe")),
            ({"edge": False, "user": True, "computed": ()}, _v9("_Probe")),
            ({"edge": False, "user": False, "computed": ("x", "y")}, _v10("_Probe", "'x', 'y'")),
            # The first refusal wins: V8, then V9, then V10.
            ({"edge": True, "user": True, "computed": ("x",)}, _v8("_Probe")),
            ({"edge": False, "user": True, "computed": ("x",)}, _v9("_Probe")),
        ],
    )
    def test_model_refusals(self, options: dict[str, Any], text: str) -> None:
        with pytest.raises(TypeError) as raised:
            check_view_config(_Probe, _STATS_QUERY, **options)
        assert type(raised.value) is TypeError
        assert str(raised.value) == text

    def test_the_query_first(self) -> None:
        with pytest.raises(TypeError) as raised:
            check_view_config(_Probe, 1, edge=True, user=True, computed=("x",))
        assert str(raised.value) == "_Probe: model_config['view_query'] must be a str, got int."


#: ``(model, overwrite, statement)`` — the goldens of spec §5.3 through ``view_ddl()``.
_MODEL_GOLDENS = [
    pytest.param(OrderStats, True, _STATS_DDL, id="grouped"),
    pytest.param(
        OrderStats,
        False,
        "DEFINE TABLE IF NOT EXISTS `order_stats` AS (SELECT status, count() AS total, math::sum(amount) AS revenue FROM "
        "orders GROUP BY status);",
        id="grouped-if-not-exists",
    ),
    pytest.param(
        PaidOrder,
        True,
        "DEFINE TABLE OVERWRITE `paid_orders` AS (SELECT customer, amount FROM orders WHERE status = 'paid');",
        id="where",
    ),
    pytest.param(
        OrderTotals,
        True,
        "DEFINE TABLE OVERWRITE `order_totals` AS (SELECT count() AS orders, math::sum(amount) AS revenue FROM orders "
        "GROUP ALL);",
        id="group-all",
    ),
    pytest.param(
        SelectView, True, "DEFINE TABLE OVERWRITE `select` AS (select * from orders\n  where amount > 1);", id="select"
    ),
]


def _keyword(name: str, annotation: Any, default: Any) -> inspect.Parameter:
    return inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=annotation)


class TestViewDdl:
    """AC-1, AC-4: ``view_ddl()`` is pure — the goldens of spec §5.3, deterministic, no connection — with V11 on an
    ordinary model and E13 for a non-``bool`` ``overwrite``; the signatures of spec §5.2."""

    @pytest.mark.parametrize(("model", "overwrite", "statement"), _MODEL_GOLDENS)
    def test_golden_statements(self, model: type[BaseSurrealModel], overwrite: bool, statement: str) -> None:
        assert model.view_ddl(overwrite=overwrite) == statement

    def test_overwrite_by_default(self) -> None:
        assert OrderStats.view_ddl() == OrderStats.view_ddl(overwrite=True) == _STATS_DDL

    def test_deterministic(self) -> None:
        assert [OrderStats.view_ddl() for _ in range(3)] == [_STATS_DDL] * 3

    async def test_works_with_no_connection_configured(self) -> None:
        await SurrealDBConnectionManager.unset_connection()
        assert SurrealDBConnectionManager.is_connection_set() is False
        assert OrderStats.view_ddl() == _STATS_DDL
        assert OrderTotals.view_ddl(overwrite=False).startswith("DEFINE TABLE IF NOT EXISTS `order_totals` AS (")

    def test_v11_on_an_ordinary_model(self) -> None:
        with pytest.raises(TypeError) as raised:
            PlainOrder.view_ddl()
        assert type(raised.value) is TypeError
        assert str(raised.value) == _v11("PlainOrder", "view_ddl()")

    def test_v11_before_e13(self) -> None:
        with pytest.raises(TypeError) as raised:
            PlainOrder.view_ddl(overwrite=1)  # type: ignore[arg-type]
        assert str(raised.value) == _v11("PlainOrder", "view_ddl()")

    @pytest.mark.parametrize(
        ("overwrite", "text"),
        [
            (1, "overwrite= must be a bool, got int."),
            ("yes", "overwrite= must be a bool, got str."),
            (None, "overwrite= must be a bool, got NoneType."),
        ],
    )
    def test_e13(self, overwrite: object, text: str) -> None:
        with pytest.raises(TypeError) as raised:
            OrderStats.view_ddl(overwrite=overwrite)  # type: ignore[arg-type]
        assert type(raised.value) is TypeError
        assert str(raised.value) == text

    @pytest.mark.parametrize(
        ("method", "signature"),
        [
            ("get_view_query", inspect.Signature([], return_annotation=str | None)),
            ("_is_view", inspect.Signature([], return_annotation=bool)),
            ("view_ddl", inspect.Signature([_keyword("overwrite", bool, True)], return_annotation=str)),
        ],
    )
    def test_signatures(self, method: str, signature: inspect.Signature) -> None:
        function = getattr(BaseSurrealModel, method)
        assert inspect.iscoroutinefunction(function) is False
        assert inspect.signature(function) == signature
        assert inspect.signature(getattr(OrderStats, method)) == signature

    def test_the_config_key(self) -> None:
        assert "view_query" in SurrealConfigDict.__annotations__

    def test_no_new_export(self) -> None:
        """No new top-level name (spec §5.7): the view helpers are private to the package."""
        assert [name for name in surreal_orm_lite.__all__ if "view" in name.lower()] == []
        assert schema.__all__ == ["Event", "function_ddl"]


# --- the write guard (spec §5.4, §5.6 W1, §6.3, §6.4; AC-6) ----------------------------------------------------------


def _w1(table: str, call: str) -> str:
    """W1 of spec §5.6; *call* names the class and the method, as ``OrderStats.save()``."""
    return (
        f"Cannot modify materialized view '{table}': {call} writes to it, and SurrealDB computes a view's records "
        "from its view_query. Write to the tables it selects from instead."
    )


_PATCH_TOTAL = [{"op": "replace", "path": "/total", "value": 9}]

#: The 20 refused paths of spec §5.4: (test id, the call as W1 names it after the class name, the call made on an
#: instance *s* of the model with the transaction *tx*). The QuerySet paths run on ``type(s).objects(tx=tx)``.
_VIEW_WRITES: list[tuple[str, str, Callable[[Any, Any], Awaitable[Any]]]] = [
    ("save", "save()", lambda s, tx: s.save(tx=tx)),
    ("upsert", "upsert()", lambda s, tx: s.upsert(tx=tx)),
    ("update", "update()", lambda s, tx: s.update(tx=tx)),
    ("merge", "merge()", lambda s, tx: s.merge(tx=tx, total=9)),
    ("patch", "patch()", lambda s, tx: s.patch(_PATCH_TOTAL, tx=tx)),
    ("delete", "delete()", lambda s, tx: s.delete(tx=tx)),
    ("atomic_append", "atomic_append()", lambda s, tx: s.atomic_append("status", "x", tx=tx)),
    ("atomic_remove", "atomic_remove()", lambda s, tx: s.atomic_remove("status", "x", tx=tx)),
    ("atomic_set_add", "atomic_set_add()", lambda s, tx: s.atomic_set_add("status", "x", tx=tx)),
    ("atomic_increment", "atomic_increment()", lambda s, tx: s.atomic_increment("total", tx=tx)),
    ("atomic_append_many", "atomic_append_many()", lambda s, tx: s.atomic_append_many("status", ["x"], tx=tx)),
    ("atomic_set_add_many", "atomic_set_add_many()", lambda s, tx: s.atomic_set_add_many("status", ["x"], tx=tx)),
    ("atomic_remove_many", "atomic_remove_many()", lambda s, tx: s.atomic_remove_many("status", ["x"], tx=tx)),
    ("bulk_create", "objects().bulk_create()", lambda s, tx: type(s).objects(tx=tx).bulk_create([s])),
    (
        "bulk_update",
        "objects().bulk_update()",
        lambda s, tx: type(s).objects(tx=tx).filter(status="paid").bulk_update(total=9),
    ),
    ("bulk_delete", "objects().bulk_delete()", lambda s, tx: type(s).objects(tx=tx).bulk_delete()),
    ("queryset_patch", "objects().patch()", lambda s, tx: type(s).objects(tx=tx).patch(_PATCH_TOTAL)),
    (
        "update_or_create",
        "objects().update_or_create()",
        lambda s, tx: type(s).objects(tx=tx).update_or_create(status="paid", defaults={"total": 9}),
    ),
    # Refused even when the record exists (D4): the E2E class reads it first.
    ("get_or_create", "objects().get_or_create()", lambda s, tx: type(s).objects(tx=tx).get_or_create(status="paid")),
    ("delete_table", "objects().delete_table()", lambda s, tx: type(s).objects(tx=tx).delete_table()),
]

_WRITE_IDS = [row[0] for row in _VIEW_WRITES]


class _UntouchableTx:
    """A transaction a refused write must never reach: any use of it fails the test."""

    def __getattr__(self, name: str) -> Any:
        pytest.fail(f"the transaction was used ({name})")


class _ClientRequested(Exception):
    """Raised by the patched ``SurrealDBConnectionManager.get_client``: the call went past the guard."""


@pytest.fixture
def client_requests(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every ``get_client()`` call from now on, each one refused with :class:`_ClientRequested`."""
    requests: list[str] = []

    async def get_client(*args: Any, **kwargs: Any) -> Any:
        requests.append("get_client")
        raise _ClientRequested

    monkeypatch.setattr(SurrealDBConnectionManager, "get_client", get_client)
    return requests


@contextlib.contextmanager
def _pre_signals(model: type[BaseSurrealModel]) -> Iterator[list[str]]:
    """Count the ``pre_save``/``pre_update``/``pre_delete`` calls for *model*; the handlers leave on exit."""
    fired: list[str] = []

    async def on_save(sender: type, **kwargs: Any) -> None:
        fired.append("pre_save")

    async def on_update(sender: type, **kwargs: Any) -> None:
        fired.append("pre_update")

    async def on_delete(sender: type, **kwargs: Any) -> None:
        fired.append("pre_delete")

    pre_save.connect(model)(on_save)
    pre_update.connect(model)(on_update)
    pre_delete.connect(model)(on_delete)
    try:
        yield fired
    finally:
        pre_save.disconnect(on_save, model)
        pre_update.disconnect(on_update, model)
        pre_delete.disconnect(on_delete, model)


class PlainStats(BaseSurrealModel):
    """An ordinary model with :class:`OrderStats`'s fields: the guard lets its writes through."""

    model_config = SurrealConfigDict(table_name="plain_stats")

    id: RecordID | None = None
    status: str
    total: int
    revenue: float


def _stats(model: type[BaseSurrealModel] = OrderStats, table: str = "order_stats") -> Any:
    """An instance of *model* holding the ``paid`` group, with its array id (spec §5.5)."""
    return model(id=RecordID(table, ["paid"]), status="paid", total=2, revenue=15.5)


class TestWriteGuard:
    """AC-6 (unit): each of the 20 writes of spec §5.4 raises W1 as its first statement — with or without a
    transaction, which it never touches —, fires no ``pre_*`` signal and asks for no client; an ordinary model's same
    calls go through to the client."""

    @pytest.mark.parametrize("in_tx", [False, True], ids=["no-tx", "tx"])
    @pytest.mark.parametrize(("name", "call", "write"), _VIEW_WRITES, ids=_WRITE_IDS)
    async def test_every_write_is_refused(
        self,
        client_requests: list[str],
        in_tx: bool,
        name: str,
        call: str,
        write: Callable[[Any, Any], Awaitable[Any]],
    ) -> None:
        stats = _stats()
        with _pre_signals(OrderStats) as fired, pytest.raises(TypeError) as raised:
            await write(stats, _UntouchableTx() if in_tx else None)
        assert type(raised.value) is TypeError
        assert str(raised.value) == _w1("order_stats", f"OrderStats.{call}")
        assert str(raised.value).startswith("Cannot modify materialized view 'order_stats'")
        assert fired == []
        assert client_requests == []

    @pytest.mark.parametrize(("name", "call", "write"), _VIEW_WRITES, ids=_WRITE_IDS)
    async def test_an_ordinary_model_is_not_refused(
        self, client_requests: list[str], name: str, call: str, write: Callable[[Any, Any], Awaitable[Any]]
    ) -> None:
        with pytest.raises(_ClientRequested):
            await write(_stats(PlainStats, "plain_stats"), None)
        assert client_requests == ["get_client"]

    @pytest.mark.parametrize(("name", "call", "write"), _VIEW_WRITES, ids=_WRITE_IDS)
    async def test_a_subclass_is_refused_under_its_own_name(
        self, client_requests: list[str], name: str, call: str, write: Callable[[Any, Any], Awaitable[Any]]
    ) -> None:
        """AC-3: a subclass inherits ``view_query``, and W1 names the subclass and its own table."""

        class ArchivedStats(OrderStats):
            model_config = SurrealConfigDict(table_name="archived_stats")

        with pytest.raises(TypeError) as raised:
            await write(_stats(ArchivedStats, "archived_stats"), None)
        assert str(raised.value) == _w1("archived_stats", f"ArchivedStats.{call}")
        assert client_requests == []

    @pytest.mark.parametrize(("name", "call", "write"), _VIEW_WRITES, ids=_WRITE_IDS)
    async def test_view_query_none_lets_a_subclass_write(
        self, client_requests: list[str], name: str, call: str, write: Callable[[Any, Any], Awaitable[Any]]
    ) -> None:
        class OrdinaryStats(OrderStats):
            model_config = SurrealConfigDict(table_name="ordinary_stats", view_query=None)

        with pytest.raises(_ClientRequested):
            await write(_stats(OrdinaryStats, "ordinary_stats"), None)
        assert client_requests == ["get_client"]

    @pytest.mark.parametrize(("name", "call", "write"), _VIEW_WRITES[:13], ids=_WRITE_IDS[:13])
    async def test_before_the_snapshot_refusal(
        self, client_requests: list[str], name: str, call: str, write: Callable[[Any, Any], Awaitable[Any]]
    ) -> None:
        """Spec §7: an instance of a view model read with ``as_of()`` gets W1, not the snapshot refusal."""
        stats = _stats()
        assert stats.__pydantic_private__ is not None
        stats.__pydantic_private__["_surreal_as_of"] = datetime(2021, 1, 1, tzinfo=UTC)
        with pytest.raises(TypeError) as raised:
            await write(stats, None)
        assert str(raised.value) == _w1("order_stats", f"OrderStats.{call}")

    @pytest.mark.parametrize(
        ("call", "write"),
        [
            ("bulk_update()", lambda qs: qs.bulk_update(total=9)),
            ("bulk_delete()", lambda qs: qs.bulk_delete()),
            ("patch()", lambda qs: qs.patch(_PATCH_TOTAL)),
            ("update_or_create()", lambda qs: qs.update_or_create(status="paid")),
            ("get_or_create()", lambda qs: qs.get_or_create(status="paid")),
            ("delete_table()", lambda qs: qs.delete_table()),
            ("bulk_create()", lambda qs: qs.bulk_create([])),
        ],
    )
    async def test_before_the_time_travel_refusal(
        self, client_requests: list[str], call: str, write: Callable[[Any], Awaitable[Any]]
    ) -> None:
        """Spec §5.4: on a queryset read ``as_of()``, W1 comes first."""
        queryset = OrderStats.objects().as_of(datetime(2021, 1, 1, tzinfo=UTC))
        with pytest.raises(TypeError) as raised:
            await write(queryset)
        assert str(raised.value) == _w1("order_stats", f"OrderStats.objects().{call}")
        assert client_requests == []

    async def test_an_empty_bulk_create_is_refused(self, client_requests: list[str]) -> None:
        with pytest.raises(TypeError) as raised:
            await OrderStats.objects().bulk_create([])
        assert str(raised.value) == _w1("order_stats", "OrderStats.objects().bulk_create()")

    def test_the_refusal_text(self) -> None:
        assert schema.view_write_refused("order_stats", "OrderStats.save()") == _w1("order_stats", "OrderStats.save()")

    def test_reject_view_write(self) -> None:
        with pytest.raises(TypeError) as raised:
            OrderStats._reject_view_write("save()")
        assert str(raised.value) == _w1("order_stats", "OrderStats.save()")
        assert PlainStats._reject_view_write("save()") is None
        assert BaseSurrealModel._reject_view_write("save()") is None


# --- the refusal reason of view DDL (spec §6.6, D16; AC-9's unit half) ----------------------------------------------

#: A view over a source table that does not exist (M9): the server's reason names that table, whose name is long
#: enough to be a run of the statement's text.
_ARCHIVE_DDL = "DEFINE TABLE OVERWRITE `order_stats` AS (SELECT status, count() AS total FROM order_archive GROUP BY status);"
_ARCHIVE_MISSING = "The table 'order_archive' does not exist"

#: A parse error quoting the statement from ``--> [1:20]`` on (SurrealDB 3.x's shape).
_ARCHIVE_PARSE_ERROR = (
    "Parse error: Unexpected token `AS`, expected Eof\n --> [1:20]\n  |\n1 | DEFINE TABLE OVERWRITE `order_stats` AS (SELECT "
    "status\n  |                    ^^\n"
)


class _RefusingClient:
    """A client whose ``query()`` records each statement and raises *error*."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.queries: list[str] = []

    async def query(self, statement: str, variables: object) -> object:
        self.queries.append(statement)
        raise self.error


class TestRefusalReason:
    """D16 (spec §6.6): with ``redact=False`` — the view DDL's — a refusal reason that quotes the statement is kept
    whole, since a view's query is trusted application code and its refusals name its source tables (M9); a parse
    error's excerpt is still replaced by its position. The default keeps v0.31.0's redaction."""

    def test_a_reason_naming_a_source_table_is_kept(self) -> None:
        reason = utils._ddl_refusal_reason(_ARCHIVE_DDL, Exception(_ARCHIVE_MISSING), redact=False)
        assert reason == (_ARCHIVE_MISSING, True)

    def test_the_default_still_redacts(self) -> None:
        left_out = ("the server's message quotes the statement, so it is left out", False)
        assert utils._ddl_refusal_reason(_ARCHIVE_DDL, Exception(_ARCHIVE_MISSING)) == left_out
        assert utils._ddl_refusal_reason(_ARCHIVE_DDL, Exception(_ARCHIVE_MISSING), redact=True) == left_out

    @pytest.mark.parametrize("redact", [False, True])
    def test_a_parse_error_keeps_only_the_position(self, redact: bool) -> None:
        reason = utils._ddl_refusal_reason(_ARCHIVE_DDL, Exception(_ARCHIVE_PARSE_ERROR), redact=redact)
        assert reason == ("Parse error: Unexpected token `AS`, expected Eof (at line 1, column 20 of the statement)", False)

    async def test_apply_ddl_statements_keeps_the_reason(self) -> None:
        error = InternalError("Internal", _ARCHIVE_MISSING)
        client = _RefusingClient(error)
        with pytest.raises(SurrealDbError) as raised:
            await apply_ddl_statements(
                [_ARCHIVE_DDL], client=client, what="view", classify=True, subjects=["'order_stats'"], redact=False
            )
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == f"Can't apply view definition: 'order_stats' -> {_ARCHIVE_MISSING}"
        assert raised.value.__cause__ is error
        assert client.queries == [_ARCHIVE_DDL]

    async def test_apply_ddl_statements_redacts_by_default(self) -> None:
        error = InternalError("Internal", _ARCHIVE_MISSING)
        with pytest.raises(SurrealDbError) as raised:
            await apply_ddl_statements(
                [_ARCHIVE_DDL], client=_RefusingClient(error), what="view", classify=True, subjects=["'order_stats'"]
            )
        assert str(raised.value) == (
            "Can't apply view definition: 'order_stats' -> the server's message quotes the statement, so it is left out"
        )
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is error

    async def test_a_parse_error_is_not_chained(self) -> None:
        error = InternalError("Internal", _ARCHIVE_PARSE_ERROR)
        client = _RefusingClient(error)
        with pytest.raises(SurrealDbError) as raised:
            await apply_ddl_statements(
                [_ARCHIVE_DDL], client=client, what="view", classify=True, subjects=["'order_stats'"], redact=False
            )
        assert str(raised.value) == (
            "Can't apply view definition: 'order_stats' -> Parse error: Unexpected token `AS`, expected Eof (at line 1, "
            "column 20 of the statement)"
        )
        assert "DEFINE TABLE" not in str(raised.value)
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is error

    def test_the_keyword_defaults_to_true(self) -> None:
        for function in (utils._ddl_refusal_reason, apply_ddl_statements):
            parameter = inspect.signature(function).parameters["redact"]
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
            assert parameter.default is True


# --- the table guard (spec §5.2, §5.6 G1–G4, S1–S3, §6.1; AC-8's unit half) -----------------------------------------


def _g1(model: str, table: str) -> str:
    return (
        f"{model}.define_view(): the table '{table}' exists, is not a view and holds records; defining the view would "
        "delete them, with the table's fields, indexes and events. If they are not needed, remove the table first "
        f'(await {model}.raw_query("REMOVE TABLE `{table}`;")); otherwise give the model another table_name. Nothing '
        "was defined."
    )


def _g2(model: str, table: str) -> str:
    return (
        f"{model}.remove_view(): the table '{table}' is not a view; remove_view() removes views only (removing it "
        "would delete its records, fields, indexes and events). Nothing was removed."
    )


def _g3(model: str, table: str, method: str, done: str) -> str:
    return (
        f"{model}.{method}: the definition of the table '{table}' could not be read, so the ORM cannot tell whether "
        f"it is a view. Nothing was {done}."
    )


def _g4(model: str, table: str) -> str:
    return (
        f"{model}.define_view(overwrite=False): the table '{table}' exists and is not a view; DEFINE TABLE IF NOT "
        f"EXISTS would leave it as it is, and {model} would not read a view. Call define_view() with overwrite=True, "
        "which turns a table that holds no record into the view. Nothing was defined."
    )


_PERMISSIONS = "IAM error: Not enough permissions to perform this action"
_INFO = "INFO FOR DB;"
_SELECT_ONE = "SELECT id FROM `order_stats` LIMIT 1;"
_REMOVE_DDL = "REMOVE TABLE IF EXISTS `order_stats`;"
_KEEP_DDL = (
    "DEFINE TABLE IF NOT EXISTS `order_stats` AS (SELECT status, count() AS total, math::sum(amount) AS revenue FROM "
    "orders GROUP BY status);"
)

#: Spec §3's golden renderings, as ``INFO FOR DB`` reads them back on every engine (M18).
_RENDERED = {
    "order_stats": "DEFINE TABLE order_stats TYPE ANY SCHEMALESS AS SELECT status, count() AS total, "
    "math::sum(amount) AS revenue FROM orders GROUP BY status PERMISSIONS NONE",
    "paid_orders": "DEFINE TABLE paid_orders TYPE ANY SCHEMALESS AS SELECT customer, amount FROM orders WHERE "
    "status = 'paid' PERMISSIONS NONE",
    "order_totals": "DEFINE TABLE order_totals TYPE ANY SCHEMALESS AS SELECT count() AS orders, math::sum(amount) AS "
    "revenue FROM orders GROUP ALL PERMISSIONS NONE",
}

#: An ordinary table, as ``INFO FOR DB`` renders one an event or a write created (M19).
_ORDINARY = "DEFINE TABLE order_stats TYPE ANY SCHEMALESS PERMISSIONS NONE"

#: A definition ``parse_define_table()`` cannot read (a clause it does not know).
_UNREADABLE = "DEFINE TABLE order_stats TYPE ANY SCHEMALESS SOMEDAY 1 PERMISSIONS NONE"

#: The table kinds of the guard, as the fake ``INFO FOR DB`` holds them.
_DEFINITIONS: dict[str, object] = {"view": _RENDERED["order_stats"], "table": _ORDINARY, "unreadable": _UNREADABLE}


class _GuardClient:
    """A client answering ``INFO FOR DB;`` with *info* (``{"tables": tables}`` by default) and ``SELECT id … LIMIT 1;``
    with *rows*, every other statement with ``[]``; it records each statement and raises the error given for the
    ``INFO``, the ``SELECT`` or the DDL. ``version()`` answers *version* and is counted."""

    def __init__(
        self,
        tables: dict[str, object] | None = None,
        rows: object = (),
        *,
        info: object = None,
        info_error: BaseException | None = None,
        select_error: BaseException | None = None,
        ddl_error: BaseException | None = None,
        version: str = "surrealdb-3.3.4",
    ) -> None:
        self.info = {"tables": dict(tables or {}), "functions": {}} if info is None else info
        self.rows = list(rows) if isinstance(rows, tuple) else rows
        self.info_error = info_error
        self.select_error = select_error
        self.ddl_error = ddl_error
        self.answer = version
        self.queries: list[str] = []
        self.version_calls = 0

    async def query(self, statement: str, variables: object) -> object:
        assert variables == {}
        self.queries.append(statement)
        if statement == _INFO:
            if self.info_error is not None:
                raise self.info_error
            return self.info
        if statement.startswith("SELECT "):
            if self.select_error is not None:
                raise self.select_error
            return self.rows
        if self.ddl_error is not None:
            raise self.ddl_error
        return []

    async def version(self) -> str:
        self.version_calls += 1
        return self.answer


class _QueuingTx:
    """A transaction of the default connection whose ``add()`` records each statement."""

    _connection_name = "default"

    def __init__(self) -> None:
        self.added: list[tuple[str, object]] = []

    async def add(self, statement: str, variables: object) -> None:
        self.added.append((statement, variables))


@pytest.fixture
def use_client(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], None]:
    """Make ``SurrealDBConnectionManager.get_client()`` answer the client given."""

    def use(client: Any) -> None:
        async def get_client(*args: Any, **kwargs: Any) -> Any:
            return client

        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", get_client)

    return use


class TestTableKind:
    """Spec §6.1: ``read_table_definitions()`` (the guard's one ``INFO FOR DB``), ``table_kind()`` (pure) and
    ``table_holds_records()`` (``SELECT id … LIMIT 1``), with their refusals S1 and S3."""

    async def test_read_table_definitions(self) -> None:
        client = _GuardClient({"order_stats": _ORDINARY, "orders": _ORDINARY})
        tables = await schema.read_table_definitions(client, "order_stats", kind="definition")
        assert tables == {"order_stats": _ORDINARY, "orders": _ORDINARY}
        assert client.queries == [_INFO]
        tables["x"] = "y"
        assert "x" not in client.info["tables"]  # type: ignore[index]

    @pytest.mark.parametrize(
        "info",
        [[], None, "INFO", {"functions": {}}, {"tables": []}, {"tables": None}],
        ids=["list", "none", "str", "no-tables", "list-tables", "none-tables"],
    )
    async def test_an_answer_without_a_tables_map(self, info: object) -> None:
        assert await schema.read_table_definitions(_GuardClient(info=info), "t", kind="definition") == {}

    @pytest.mark.parametrize(
        ("tables", "expected"),
        [
            ({}, None),
            ({"other": _ORDINARY}, None),
            ({"order_stats": _RENDERED["order_stats"]}, "view"),
            ({"order_stats": _RENDERED["paid_orders"]}, "view"),
            ({"order_stats": _RENDERED["order_totals"]}, "view"),
            ({"order_stats": _ORDINARY}, "table"),
            ({"order_stats": "DEFINE TABLE mv_orders TYPE ANY SCHEMALESS PERMISSIONS NONE"}, "table"),
            ({"order_stats": _UNREADABLE}, "unreadable"),
            ({"order_stats": 1}, "unreadable"),
            ({"order_stats": {"sql": _ORDINARY}}, "unreadable"),
        ],
        ids=["empty", "missing", "view", "plain-view", "group-all", "table", "other-name", "unparseable", "int", "dict"],
    )
    def test_table_kind(self, tables: dict[str, object], expected: str | None) -> None:
        assert schema.table_kind(tables, "order_stats") == expected

    def test_the_unreadable_definition_does_not_parse(self) -> None:
        with pytest.raises(define_parser.DefineParseError):
            define_parser.parse_define_table(_UNREADABLE)

    @pytest.mark.parametrize(
        ("rows", "expected"),
        [([{"id": RecordID("order_stats", 1)}], True), ([], False), ({}, False), (None, False)],
        ids=["a-record", "none", "dict", "null"],
    )
    async def test_table_holds_records(self, rows: object, expected: bool) -> None:
        client = _GuardClient(rows=rows)
        assert await schema.table_holds_records(client, "order_stats", kind="definition") is expected
        assert client.queries == [_SELECT_ONE]

    @pytest.mark.parametrize(
        ("table", "statement"),
        [("mv_orders", "SELECT id FROM `mv_orders` LIMIT 1;"), ("select", "SELECT id FROM `select` LIMIT 1;")],
    )
    async def test_the_select_quotes_the_table(self, table: str, statement: str) -> None:
        client = _GuardClient()
        await schema.table_holds_records(client, table, kind="definition")
        assert client.queries == [statement]

    @pytest.mark.parametrize(
        "error",
        [NotAllowedError("NotAllowed", _PERMISSIONS), InternalError("Internal", _PERMISSIONS)],
        ids=["3.x", "2.x"],
    )
    @pytest.mark.parametrize("kind", ["definition", "removal"])
    @pytest.mark.parametrize("read", ["info", "select"])
    async def test_s1_a_refused_read(self, error: Exception, kind: str, read: str) -> None:
        raised = await self._refused_read(error, kind, read)
        assert type(raised.value) is SurrealDbAuthenticationError
        assert str(raised.value) == f"Can't apply view {kind}: 'order_stats' -> {_PERMISSIONS}"
        assert raised.value.__cause__ is error

    @pytest.mark.parametrize(
        "error",
        [
            InternalError("Internal", "There was a problem with the database"),
            NotFoundError("NotFound", "The table 'order_stats' does not exist"),
            RuntimeError("the engine stopped"),
            SurrealDbError("Transaction failed"),
        ],
        ids=["internal", "not-found", "runtime", "orm"],
    )
    @pytest.mark.parametrize("kind", ["definition", "removal"])
    @pytest.mark.parametrize("read", ["info", "select"])
    async def test_s3_any_other_refused_read(self, error: Exception, kind: str, read: str) -> None:
        raised = await self._refused_read(error, kind, read)
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == f"Can't apply view {kind}: 'order_stats' -> {error}"
        assert raised.value.__cause__ is error

    @staticmethod
    async def _refused_read(error: Exception, kind: str, read: str) -> pytest.ExceptionInfo[BaseException]:
        with pytest.raises(BaseException) as raised:
            if read == "info":
                await schema.read_table_definitions(_GuardClient(info_error=error), "order_stats", kind=kind)
            else:
                await schema.table_holds_records(_GuardClient(select_error=error), "order_stats", kind=kind)
        return raised


#: The guard of ``define_view()`` (spec §5.2 step f): (the table's kind, its records, ``overwrite``, what is sent, the
#: refusal or ``None``). ``"none"`` is no such table.
_DEFINE_GUARD = [
    ("none", False, True, [_INFO, _STATS_DDL], None),
    ("none", False, False, [_INFO, _KEEP_DDL], None),
    ("view", True, True, [_INFO, _STATS_DDL], None),
    ("view", True, False, [_INFO, _KEEP_DDL], None),
    ("table", True, True, [_INFO, _SELECT_ONE], _g1("OrderStats", "order_stats")),
    ("table", False, True, [_INFO, _SELECT_ONE, _STATS_DDL], None),
    ("table", True, False, [_INFO], _g4("OrderStats", "order_stats")),
    ("table", False, False, [_INFO], _g4("OrderStats", "order_stats")),
    ("unreadable", True, True, [_INFO, _SELECT_ONE], _g3("OrderStats", "order_stats", "define_view()", "defined")),
    ("unreadable", False, True, [_INFO, _SELECT_ONE, _STATS_DDL], None),
    ("unreadable", True, False, [_INFO], _g3("OrderStats", "order_stats", "define_view()", "defined")),
    ("unreadable", False, False, [_INFO], _g3("OrderStats", "order_stats", "define_view()", "defined")),
]

#: The guard of ``remove_view()`` (spec §5.2 step e): (the table's kind, its records, what is sent, the refusal).
_REMOVE_GUARD = [
    ("none", False, [_INFO, _REMOVE_DDL], None),
    ("view", True, [_INFO, _REMOVE_DDL], None),
    ("table", True, [_INFO], _g2("OrderStats", "order_stats")),
    ("table", False, [_INFO], _g2("OrderStats", "order_stats")),
    ("unreadable", True, [_INFO], _g3("OrderStats", "order_stats", "remove_view()", "removed")),
    ("unreadable", False, [_INFO], _g3("OrderStats", "order_stats", "remove_view()", "removed")),
]


def _guard_client(existing: str, records: bool, **errors: Any) -> _GuardClient:
    tables = {} if existing == "none" else {"order_stats": _DEFINITIONS[existing], "orders": _ORDINARY}
    return _GuardClient(tables, [{"id": RecordID("order_stats", 1)}] if records else [], **errors)


class TestTableKindGuard:
    """AC-8 (unit): the table guard of ``define_view()``/``remove_view()`` on fake clients — one ``INFO FOR DB`` per
    call, the ``SELECT … LIMIT 1`` only for an ordinary or unreadable table with ``overwrite=True``, G1–G4 before any
    DDL, and the DDL's refusals S1–S3 naming the view's table (``removal`` for a removal, P3)."""

    @pytest.mark.parametrize(("existing", "records", "overwrite", "sent", "refusal"), _DEFINE_GUARD)
    async def test_define_view(
        self,
        use_client: Callable[[Any], None],
        existing: str,
        records: bool,
        overwrite: bool,
        sent: list[str],
        refusal: str | None,
    ) -> None:
        client = _guard_client(existing, records)
        use_client(client)
        if refusal is None:
            statement = OrderStats.view_ddl(overwrite=overwrite)
            assert await OrderStats.define_view(overwrite=overwrite) == statement
            assert client.queries == sent
        else:
            with pytest.raises(SurrealDbError) as raised:
                await OrderStats.define_view(overwrite=overwrite)
            assert type(raised.value) is SurrealDbError
            assert str(raised.value) == refusal
            assert client.queries == sent
        assert client.queries.count(_INFO) == 1
        assert client.version_calls == 0

    @pytest.mark.parametrize(("existing", "records", "sent", "refusal"), _REMOVE_GUARD)
    async def test_remove_view(
        self, use_client: Callable[[Any], None], existing: str, records: bool, sent: list[str], refusal: str | None
    ) -> None:
        client = _guard_client(existing, records)
        use_client(client)
        if refusal is None:
            assert await OrderStats.remove_view() == _REMOVE_DDL
        else:
            with pytest.raises(SurrealDbError) as raised:
                await OrderStats.remove_view()
            assert type(raised.value) is SurrealDbError
            assert str(raised.value) == refusal
        assert client.queries == sent
        assert client.version_calls == 0

    async def test_a_transaction_gets_the_ddl_only(self, use_client: Callable[[Any], None]) -> None:
        """Spec §7: the guard reads committed state on the client, outside the transaction; the DDL is queued."""
        client = _guard_client("table", False)
        use_client(client)
        tx = _QueuingTx()
        assert await OrderStats.define_view(tx=tx) == _STATS_DDL  # type: ignore[arg-type]
        assert client.queries == [_INFO, _SELECT_ONE]
        assert tx.added == [(_STATS_DDL, None)]
        removal = _QueuingTx()
        client.queries.clear()
        client.info = {"tables": {"order_stats": _RENDERED["order_stats"]}}
        assert await OrderStats.remove_view(missing_ok=False, tx=removal) == "REMOVE TABLE `order_stats`;"  # type: ignore[arg-type]
        assert client.queries == [_INFO]
        assert removal.added == [("REMOVE TABLE `order_stats`;", None)]

    async def test_p3_a_removal_the_server_refuses(self, use_client: Callable[[Any], None]) -> None:
        reason = (
            "Invalid query: Cannot delete table `order_stats` on which a view is defined, table(s) `x` are defined as "
            "a view on this table."
        )
        error = InternalError("Internal", reason)
        client = _guard_client("view", True, ddl_error=error)
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await OrderStats.remove_view()
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == f"Can't apply view removal: 'order_stats' -> {reason}"
        assert raised.value.__cause__ is error
        assert client.queries == [_INFO, _REMOVE_DDL]

    async def test_s2_a_missing_table(self, use_client: Callable[[Any], None]) -> None:
        error = NotFoundError("NotFound", "The table 'order_stats' does not exist")
        use_client(_guard_client("none", False, ddl_error=error))
        with pytest.raises(SurrealDbError) as raised:
            await OrderStats.remove_view(missing_ok=False)
        assert type(raised.value) is SurrealDbNotFoundError
        assert str(raised.value) == "Can't apply view removal: 'order_stats' -> The table 'order_stats' does not exist"
        assert raised.value.__cause__ is error

    @pytest.mark.parametrize("remove", [False, True], ids=["define", "remove"])
    async def test_s1_a_refused_ddl(self, use_client: Callable[[Any], None], remove: bool) -> None:
        error = NotAllowedError("NotAllowed", _PERMISSIONS)
        use_client(_guard_client("view", True, ddl_error=error))
        with pytest.raises(SurrealDbError) as raised:
            await (OrderStats.remove_view() if remove else OrderStats.define_view())
        kind = "removal" if remove else "definition"
        assert type(raised.value) is SurrealDbAuthenticationError
        assert str(raised.value) == f"Can't apply view {kind}: 'order_stats' -> {_PERMISSIONS}"

    @pytest.mark.parametrize("remove", [False, True], ids=["define", "remove"])
    async def test_s1_a_refused_info(self, use_client: Callable[[Any], None], remove: bool) -> None:
        client = _GuardClient(info_error=InternalError("Internal", _PERMISSIONS))
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await (OrderStats.remove_view() if remove else OrderStats.define_view())
        kind = "removal" if remove else "definition"
        assert type(raised.value) is SurrealDbAuthenticationError
        assert str(raised.value) == f"Can't apply view {kind}: 'order_stats' -> {_PERMISSIONS}"
        assert client.queries == [_INFO]

    async def test_s3_a_refused_select(self, use_client: Callable[[Any], None]) -> None:
        error = InternalError("Internal", "There was a problem with the database")
        client = _guard_client("table", False, select_error=error)
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await OrderStats.define_view()
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == f"Can't apply view definition: 'order_stats' -> {error}"
        assert client.queries == [_INFO, _SELECT_ONE]

    async def test_s3_names_a_missing_source_table(self, use_client: Callable[[Any], None]) -> None:
        """M9, D16: the reason that names the missing source table is kept whole, never the statement."""
        error = InternalError("Internal", "The table 'orders' does not exist")
        use_client(_guard_client("none", False, ddl_error=error))
        with pytest.raises(SurrealDbError) as raised:
            await OrderStats.define_view()
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == "Can't apply view definition: 'order_stats' -> The table 'orders' does not exist"


def _txn_signature(name: str) -> inspect.Signature:
    return inspect.Signature([_keyword(name, bool, True), _keyword("tx", Transaction | None, None)], return_annotation=str)


class TestViewAppliers:
    """AC-1, AC-4: ``define_view()``/``remove_view()`` are coroutine class methods with spec §5.2's signatures, wrapped
    by the query cache; V11 on an ordinary model and E13 before any request."""

    @pytest.mark.parametrize(
        ("method", "signature"),
        [("define_view", _txn_signature("overwrite")), ("remove_view", _txn_signature("missing_ok"))],
    )
    def test_signatures(self, method: str, signature: inspect.Signature) -> None:
        function = getattr(BaseSurrealModel, method)
        assert inspect.iscoroutinefunction(function)
        assert inspect.signature(function) == signature
        assert inspect.signature(getattr(OrderStats, method)) == signature
        assert isinstance(inspect.getattr_static(BaseSurrealModel, method), classmethod)
        assert vars(BaseSurrealModel)[method].__func__.__surreal_cache_writes__ is True

    @pytest.mark.parametrize("method", ["define_view", "remove_view"])
    async def test_v11_before_any_request(self, client_requests: list[str], method: str) -> None:
        await SurrealDBConnectionManager.unset_connection()
        with pytest.raises(TypeError) as raised:
            await getattr(PlainOrder, method)()
        assert type(raised.value) is TypeError
        assert str(raised.value) == _v11("PlainOrder", f"{method}()")
        assert client_requests == []

    @pytest.mark.parametrize(
        ("call", "text"),
        [
            (lambda: OrderStats.define_view(overwrite=1), "overwrite= must be a bool, got int."),
            (lambda: OrderStats.remove_view(missing_ok="yes"), "missing_ok= must be a bool, got str."),
        ],
        ids=["overwrite", "missing_ok"],
    )
    async def test_e13_before_any_request(
        self, client_requests: list[str], call: Callable[[], Awaitable[Any]], text: str
    ) -> None:
        with pytest.raises(TypeError) as raised:
            await call()
        assert type(raised.value) is TypeError
        assert str(raised.value) == text
        assert client_requests == []


# === E2E (spec §9.2): the connection "mv" on the database mv_db, over WebSocket and HTTP, and mem:// ================

#: The engines every E2E class runs on: the line under test over WebSocket and HTTP, and the embedded engine.
_KINDS = ["ws", "http", "mem"]

#: The reset of spec §9.2, in one query: the source table exists (M9).
_MV_RESET = "REMOVE DATABASE IF EXISTS mv_db; DEFINE DATABASE mv_db; DEFINE TABLE mv_orders TYPE ANY SCHEMALESS;"

#: The grouped golden of spec §5.3 over ``mv_orders``.
_MV_STATS_QUERY = "SELECT status, count() AS total, math::sum(amount) AS revenue FROM mv_orders GROUP BY status"

#: Three orders, then ``mv_order_stats`` defined by hand (``define_view()`` comes in Task 4): two groups.
_MV_SETUP = (
    "CREATE mv_orders:o1 SET status = 'paid', amount = 10.0, customer = 'ann'; "
    "CREATE mv_orders:o2 SET status = 'paid', amount = 5.5, customer = 'bob'; "
    "CREATE mv_orders:o3 SET status = 'new', amount = 1.0, customer = 'ann'; "
    f"DEFINE TABLE mv_order_stats AS ({_MV_STATS_QUERY});"
)

#: The two groups of ``_MV_SETUP``, as a raw ``SELECT * FROM mv_order_stats ORDER BY id`` reads them.
_MV_STATS_ROWS = [
    {"id": RecordID("mv_order_stats", ["new"]), "revenue": 1.0, "status": "new", "total": 1},
    {"id": RecordID("mv_order_stats", ["paid"]), "revenue": 15.5, "status": "paid", "total": 2},
]

#: M6: SurrealDB 3.2+ refuses a raw write to a view table.
_M6 = (
    "Cannot write to the `mv_order_stats` table, as it is a view (defined with `AS SELECT`); view tables are "
    "read-only and their records are computed from the source query"
)

#: The SDK methods a refused write must reach none of (AC-6).
_SDK_METHODS = (
    "query",
    "query_raw",
    "create",
    "insert",
    "insert_relation",
    "update",
    "merge",
    "patch",
    "upsert",
    "delete",
    "select",
)


class MvOrder(BaseSurrealModel):
    """The source table of the E2E views (spec §9.2)."""

    model_config = SurrealConfigDict(table_name="mv_orders", connection="mv")

    id: str | None = None
    status: str
    amount: float | None = None
    customer: str | None = None


class MvStats(BaseSurrealModel):
    """The grouped golden of spec §5.3 over ``mv_orders``."""

    model_config = SurrealConfigDict(table_name="mv_order_stats", connection="mv", view_query=_MV_STATS_QUERY)

    id: RecordID | None = None
    status: str
    total: int
    revenue: float


def _url(kind: str) -> str:
    """The URL of *kind*: ``ws`` or ``http`` on the line under test (``SURREALDB_PORT``), or ``mem``."""
    if kind == "mem":
        return "mem://"
    host = os.environ.get("SURREALDB_HOST", "localhost")
    port = os.environ.get("SURREALDB_PORT", "8000")
    return f"ws://{host}:{port}/rpc" if kind == "ws" else f"http://{host}:{port}"


def _register_mv(kind: str) -> None:
    if kind == "mem":
        SurrealDBConnectionManager.add_connection("mv", url="mem://", namespace="ns", database="mv_db")
    else:
        SurrealDBConnectionManager.add_connection(
            "mv", url=_url(kind), user="root", password="root", namespace="ns", database="mv_db"
        )


async def _apply_ddl(client: Any, ddl: str) -> None:
    """Run *ddl* and check every statement: ``query()`` checks only the first one."""
    response = await client.query_raw(ddl, {})
    assert "error" not in response, response["error"]
    refused = [result for result in response["result"] if result["status"] != "OK"]
    assert response["result"] and not refused, refused


@contextlib.asynccontextmanager
async def _mv(kind: str) -> AsyncIterator[Any]:
    """The connection ``"mv"`` on *kind*, its database ``mv_db`` reset in one query; removed, then unregistered, on
    exit (spec §9.2)."""
    _register_mv(kind)
    client = await SurrealDBConnectionManager.get_client("mv")
    await _apply_ddl(client, _MV_RESET)
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            cleanup = await SurrealDBConnectionManager.get_client("mv")
            await cleanup.query("REMOVE DATABASE IF EXISTS mv_db;", {})
        await SurrealDBConnectionManager.remove_connection("mv")


async def _line(client: Any) -> tuple[int, int]:
    """``(major, minor)`` of the engine *client* talks to: ``"surrealdb-3.1.5"`` → ``(3, 1)``; ``mem://`` →
    ``(2, 0)`` (the SDK's version, whose core is 2.x)."""
    match = re.search(r"(\d+)\.(\d+)", await client.version())
    assert match is not None
    return int(match.group(1)), int(match.group(2))


async def _rows(client: Any, table: str) -> list[dict[str, Any]]:
    """Every record of *table* (a constant name of these tests), ordered by id, read raw."""
    rows: list[dict[str, Any]] = await client.query(f"SELECT * FROM {table} ORDER BY id;", {})
    return rows


def _spy_sdk(monkeypatch: pytest.MonkeyPatch, client: Any) -> list[str]:
    """Record the name of every SDK method of :data:`_SDK_METHODS` *client* is called with (and let it through)."""
    calls: list[str] = []

    def recording(method: str) -> Any:
        real = getattr(client, method)

        async def call(*args: object, **kwargs: object) -> object:
            calls.append(method)
            return await real(*args, **kwargs)

        return call

    for method in _SDK_METHODS:
        monkeypatch.setattr(client, method, recording(method))
    return calls


class TestViewWritesE2E:
    """AC-6 (E2E): on every engine of both lines, each of the 20 writes raises W1 with no SDK call and leaves the
    view's records as they were; the relation helpers write the edge table (M16); a raw write is pinned per line (M6)."""

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize(("name", "call", "write"), _VIEW_WRITES, ids=_WRITE_IDS)
    async def test_every_write_is_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
        kind: str,
        name: str,
        call: str,
        write: Callable[[Any, Any], Awaitable[Any]],
    ) -> None:
        async with _mv(kind) as client:
            await _apply_ddl(client, _MV_SETUP)
            assert await _rows(client, "mv_order_stats") == _MV_STATS_ROWS
            stats = await MvStats.objects().get(RecordID("mv_order_stats", ["paid"]))
            assert (stats.status, stats.total, stats.revenue) == ("paid", 2, 15.5)
            calls = _spy_sdk(monkeypatch, client)
            for tx in (None, _UntouchableTx()):
                with _pre_signals(MvStats) as fired, pytest.raises(TypeError) as raised:
                    await write(stats, tx)
                assert str(raised.value) == _w1("mv_order_stats", f"MvStats.{call}")
                assert fired == []
            assert calls == []
            assert await _rows(client, "mv_order_stats") == _MV_STATS_ROWS

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize("reverse", [False, True], ids=["view-to-order", "order-to-view"])
    async def test_relations_write_the_edge_table(self, kind: str, reverse: bool) -> None:
        """M16: ``relate()`` and ``remove_relation()`` from a view instance write the edge table only, on every line
        (3.x included)."""
        async with _mv(kind) as client:
            await _apply_ddl(client, _MV_SETUP)
            stats = await MvStats.objects().get(RecordID("mv_order_stats", ["paid"]))
            order = MvOrder(id="o1", status="paid")
            view_id, order_id = RecordID("mv_order_stats", ["paid"]), RecordID("mv_orders", "o1")
            ends = (order_id, view_id) if reverse else (view_id, order_id)
            rows = await stats.relate("mv_watches", order, reverse=reverse)
            assert [(row["in"], row["out"]) for row in rows] == [ends]
            edges = await client.query("SELECT in, out FROM mv_watches;", {})
            assert [(edge["in"], edge["out"]) for edge in edges] == [ends]
            await stats.remove_relation("mv_watches", order, reverse=reverse)
            assert await client.query("SELECT * FROM mv_watches;", {}) == []
            assert await _rows(client, "mv_order_stats") == _MV_STATS_ROWS

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_raw_write_follows_the_server_line(self, kind: str) -> None:
        """M6, not an ORM path: SurrealDB 3.2 and later refuse a raw ``DELETE`` of a view table; 2.x, 3.1 and the
        embedded engine accept it and empty the view — what W1 keeps the ORM's own writes from doing."""
        async with _mv(kind) as client:
            await _apply_ddl(client, _MV_SETUP)
            if await _line(client) >= (3, 2):
                with pytest.raises(InternalError) as raised:
                    await client.query("DELETE mv_order_stats;", {})
                assert str(raised.value) == _M6
                assert await _rows(client, "mv_order_stats") == _MV_STATS_ROWS
            else:
                await client.query("DELETE mv_order_stats;", {})
                assert await _rows(client, "mv_order_stats") == []


# --- define_view() and remove_view() on the engines (spec §5.2, §5.6 G1–G4, S1–S3; AC-5, AC-8, AC-9, AC-12, AC-17) ---

#: Spec §3's golden for ``mv_order_stats``, as ``INFO FOR DB`` renders it on every engine (M18).
_MV_STATS_RENDERED = (
    "DEFINE TABLE mv_order_stats TYPE ANY SCHEMALESS AS SELECT status, count() AS total, math::sum(amount) AS revenue "
    "FROM mv_orders GROUP BY status PERMISSIONS NONE"
)

#: ``mv_order_stats`` as an empty ordinary table, created by a ``DEFINE EVENT`` on it (M19).
_MV_STATS_TABLE = "DEFINE TABLE mv_order_stats TYPE ANY SCHEMALESS PERMISSIONS NONE"


class MvPaid(BaseSurrealModel):
    """The ``WHERE`` golden of spec §5.3 over ``mv_orders``: a plain view keeps the source ids (M4)."""

    model_config = SurrealConfigDict(
        table_name="mv_paid_orders", connection="mv", view_query="SELECT customer, amount FROM mv_orders WHERE status = 'paid'"
    )

    id: RecordID | None = None
    customer: str | None = None
    amount: float


class MvTotals(BaseSurrealModel):
    """The ``GROUP ALL`` golden of spec §5.3 over ``mv_orders``: one record, id ``mv_order_totals:[]`` (M4)."""

    model_config = SurrealConfigDict(
        table_name="mv_order_totals",
        connection="mv",
        view_query="SELECT count() AS orders, math::sum(amount) AS revenue FROM mv_orders GROUP ALL",
    )

    id: RecordID | None = None
    orders: int
    revenue: float


class MvOnSource(BaseSurrealModel):
    """A view model whose table is the source ``mv_orders`` itself: G1, G2, G4 (AC-8 a)."""

    model_config = SurrealConfigDict(table_name="mv_orders", connection="mv", view_query=_MV_STATS_QUERY)

    id: RecordID | None = None


class MvTaken(BaseSurrealModel):
    """The grouped golden over ``mv_orders`` on a table a raw write took first: G1 and its remedy (AC-8 d)."""

    model_config = SurrealConfigDict(table_name="mv_taken", connection="mv", view_query=_MV_STATS_QUERY)

    id: RecordID | None = None
    status: str
    total: int
    revenue: float


class MvArchiveStats(BaseSurrealModel):
    """A view over ``mv_order_archive`` (16 characters), never created: S3 names it (AC-9)."""

    model_config = SurrealConfigDict(
        table_name="mv_archive_stats",
        connection="mv",
        view_query="SELECT status, count() AS total FROM mv_order_archive GROUP BY status",
    )

    id: RecordID | None = None
    status: str
    total: int


class MvStatsEvents(MvStats):
    """``MvStats`` with an event on the view (D14): ``OVERWRITE`` drops it, ``define_events()`` restores it."""

    model_config = SurrealConfigDict(
        table_name="mv_order_stats", events=[Event("mv_seen", then="CREATE mv_log SET at = time::now()")]
    )


class MvCurrentStats(BaseSurrealModel):
    """The grouped golden over ``mv_orders`` with no ``connection``: it defines on the ``using()`` block's (AC-12)."""

    model_config = SurrealConfigDict(table_name="mv_order_stats", view_query=_MV_STATS_QUERY)

    id: RecordID | None = None
    status: str
    total: int
    revenue: float


#: A record access on ``mv_db``: a user signed up through it is a record user, who may not define (M15).
_MV_ACCESS = (
    "DEFINE ACCESS mv_acc ON DATABASE TYPE RECORD SIGNUP (CREATE mv_user SET name = $name) "
    "SIGNIN (SELECT * FROM mv_user WHERE name = $name);"
)


async def _major(client: Any) -> int:
    """The major version of the engine *client* talks to: ``"surrealdb-3.3.4"`` → 3; ``mem://`` → 2."""
    return (await _line(client))[0]


async def _require_line(client: Any, line: int, item: str) -> None:
    """A *line*-only *item*: the test runs on that line, where a refusal fails it, and is skipped on the other."""
    if await _major(client) != line:
        pytest.skip(f"SurrealDB {line}.x only: {item}")


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


def _sent(calls: list[tuple[str, tuple[object, ...]]]) -> list[object]:
    """The statements *calls* (:func:`_spy`'s) sent through ``query()``, after checking that nothing else was sent: no
    ``version()``, and a ``query_raw()`` only for those statements (the SDK's ``query()`` runs it itself)."""
    statements = [args[0] for method, args in calls if method == "query"]
    assert [method for method, _ in calls if method == "version"] == []
    assert [args[0] for method, args in calls if method == "query_raw"] == statements
    return statements


async def _tables(client: Any) -> dict[str, Any]:
    """The ``tables`` of ``INFO FOR DB`` on *client*'s database."""
    return dict((await client.query("INFO FOR DB;", {}))["tables"])


async def _events_info(client: Any, table: str) -> dict[str, Any]:
    """The ``events`` of ``INFO FOR TABLE`` *table* (a constant name of these tests)."""
    return dict((await client.query(f"INFO FOR TABLE {table};", {}))["events"])


@contextlib.asynccontextmanager
async def _default(kind: str) -> AsyncIterator[Any]:
    """The default connection on the line under test (``ns``/``db``) — over *kind*, WebSocket for ``mem`` — and its
    client; its previous settings put back on exit."""
    saved_url, *saved = default_connection_class()._settings()
    url = _url("ws" if kind == "mem" else kind)
    SurrealDBConnectionManager.set_connection(url=url, user="root", password="root", namespace="ns", database="db")
    try:
        yield await SurrealDBConnectionManager.get_client()
    finally:
        await SurrealDBConnectionManager.unset_connection()
        if saved_url is not None:
            SurrealDBConnectionManager.set_connection(saved_url, *saved)


async def _save_orders() -> None:
    """Spec AC-5's three orders, saved through the ORM: ``paid`` 10.0 and 5.5, ``new`` 1.0."""
    await MvOrder(id="o1", status="paid", amount=10.0, customer="ann").save()
    await MvOrder(id="o2", status="paid", amount=5.5, customer="bob").save()
    await MvOrder(id="o3", status="new", amount=1.0, customer="ann").save()


async def _groups() -> dict[str, tuple[int, float]]:
    """``MvStats``' groups, read through the ORM: status → (total, revenue)."""
    return {stats.status: (stats.total, stats.revenue) for stats in await MvStats.objects().exec()}


class TestDefineViewE2E:
    """AC-5, AC-9, AC-12 (E2E, both lines, WebSocket, HTTP and ``mem://``): ``define_view()`` applies the view and
    returns its statement, SurrealDB fills it and keeps it up to date, a second call converges, ``overwrite=False``
    keeps what exists, ``remove_view()`` removes it; a missing source table is named by the refusal; the view lands on
    the model's connection."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_define_and_read(self, kind: str) -> None:
        async with _mv(kind) as client:
            await _save_orders()
            assert await MvStats.define_view() == MvStats.view_ddl()
            assert (await _tables(client))["mv_order_stats"] == _MV_STATS_RENDERED
            stats = await MvStats.objects().order_by("status").exec()
            assert all(type(row) is MvStats for row in stats)
            assert [(row.id, row.status, row.total, row.revenue) for row in stats] == [
                (RecordID("mv_order_stats", ["new"]), "new", 1, 1.0),
                (RecordID("mv_order_stats", ["paid"]), "paid", 2, 15.5),
            ]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_source_writes_are_reflected_at_once(self, kind: str) -> None:
        async with _mv(kind):
            await _save_orders()
            await MvStats.define_view()
            await MvOrder(id="o4", status="new", amount=2.0).save()
            assert await _groups() == {"new": (2, 3.0), "paid": (2, 15.5)}
            await (await MvOrder.objects().get("o1")).merge(amount=20.0)
            assert await _groups() == {"new": (2, 3.0), "paid": (2, 25.5)}
            await (await MvOrder.objects().get("o3")).delete()
            assert await _groups() == {"new": (1, 2.0), "paid": (2, 25.5)}
            await (await MvOrder.objects().get("o4")).delete()
            assert await _groups() == {"paid": (2, 25.5)}

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_plain_view_and_a_group_all_view(self, kind: str) -> None:
        async with _mv(kind):
            await _save_orders()
            assert await MvPaid.define_view() == MvPaid.view_ddl()
            assert await MvTotals.define_view() == MvTotals.view_ddl()
            paid = await MvPaid.objects().order_by("customer").exec()
            assert [(row.id, row.customer, row.amount) for row in paid] == [
                (RecordID("mv_paid_orders", "o1"), "ann", 10.0),
                (RecordID("mv_paid_orders", "o2"), "bob", 5.5),
            ]
            totals = await MvTotals.objects().first()
            assert totals is not None
            assert (totals.id, totals.orders, totals.revenue) == (RecordID("mv_order_totals", []), 3, 16.5)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_idempotent_and_overwrite_false_keeps_what_exists(self, kind: str) -> None:
        """A second ``define_view()`` converges (``INFO`` unchanged) — except on SurrealDB 3.1, measured on 3.1.5 over
        WebSocket and HTTP: its key-value store refuses ``DEFINE TABLE OVERWRITE … AS`` on a view that holds records
        (S3), and the view stays as it was; 3.3, 2.x and the embedded engine recompute it."""
        async with _mv(kind) as client:
            await _save_orders()
            await MvStats.define_view()
            before = await _tables(client)
            if await _line(client) == (3, 1):
                with pytest.raises(SurrealDbError) as raised:
                    await MvStats.define_view()
                assert type(raised.value) is SurrealDbError
                assert str(raised.value) == (
                    "Can't apply view definition: 'mv_order_stats' -> There was a problem with the key-value store: The "
                    "key being inserted already exists"
                )
            else:
                assert await MvStats.define_view() == MvStats.view_ddl()
            assert await _tables(client) == before
            assert await _groups() == {"new": (1, 1.0), "paid": (2, 15.5)}
            await _apply_ddl(
                client,
                "REMOVE TABLE mv_order_stats; DEFINE TABLE mv_order_stats AS SELECT status, count() AS total, "
                "math::sum(amount) AS revenue FROM mv_orders WHERE status = 'x' GROUP BY status;",
            )
            by_hand = (await _tables(client))["mv_order_stats"]
            assert "WHERE status = 'x'" in by_hand
            assert await MvStats.define_view(overwrite=False) == MvStats.view_ddl(overwrite=False)
            assert (await _tables(client))["mv_order_stats"] == by_hand
            assert await MvStats.objects().exec() == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_remove_view(self, kind: str) -> None:
        async with _mv(kind) as client:
            await _save_orders()
            await MvStats.define_view()
            assert await MvStats.remove_view() == "REMOVE TABLE IF EXISTS `mv_order_stats`;"
            tables = await _tables(client)
            assert "mv_order_stats" not in tables
            assert "mv_orders" in tables
            assert len(await _rows(client, "mv_orders")) == 3
            assert await MvStats.remove_view() == "REMOVE TABLE IF EXISTS `mv_order_stats`;"
            with pytest.raises(SurrealDbError) as raised:
                await MvStats.remove_view(missing_ok=False)
            assert type(raised.value) is SurrealDbNotFoundError
            assert str(raised.value) == (
                "Can't apply view removal: 'mv_order_stats' -> The table 'mv_order_stats' does not exist"
            )

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_s3_names_a_missing_source_table(self, kind: str) -> None:
        """AC-9 (M9, D16): the refusal keeps the server's reason, which names the source table, and never quotes the
        statement. The raw ``REMOVE TABLE mv_orders`` runs before any view reads it (3.x refuses it then, M11)."""
        async with _mv(kind) as client:
            with pytest.raises(SurrealDbError) as archive:
                await MvArchiveStats.define_view()
            assert "mv_archive_stats" not in await _tables(client)
            await _apply_ddl(client, "REMOVE TABLE mv_orders;")
            with pytest.raises(SurrealDbError) as orders:
                await MvStats.define_view()
            assert "mv_order_stats" not in await _tables(client)
        for raised, view, source in (
            (archive, "mv_archive_stats", "mv_order_archive"),
            (orders, "mv_order_stats", "mv_orders"),
        ):
            text = str(raised.value)
            assert type(raised.value) is SurrealDbError
            assert text.startswith(f"Can't apply view definition: '{view}' -> ")
            assert f"The table '{source}' does not exist" in text
            assert "quotes the statement" not in text
            assert "DEFINE TABLE" not in text

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_model_defines_on_its_own_connection(self, kind: str) -> None:
        async with _default(kind) as default, _mv(kind) as client:
            await _save_orders()
            await MvStats.define_view()
            assert (await _tables(client))["mv_order_stats"] == _MV_STATS_RENDERED
            assert "mv_order_stats" not in await _tables(default)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_model_without_connection_defines_on_the_current_one(self, kind: str) -> None:
        async with _default(kind) as default, _mv(kind) as client:
            await _save_orders()
            async with SurrealDBConnectionManager.using("mv"):
                assert await MvCurrentStats.define_view() == MvCurrentStats.view_ddl()
            assert (await _tables(client))["mv_order_stats"] == _MV_STATS_RENDERED
            assert "mv_order_stats" not in await _tables(default)


class TestViewGuardE2E:
    """AC-8 (E2E): the table guard on both lines — never a table that holds records (G1), never ``IF NOT EXISTS`` on an
    ordinary table (G4), never the removal of an ordinary table (G2) — and an empty ordinary table, created by an event
    or a 2.x live query, becomes the view."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_source_table_is_never_touched(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _mv(kind) as client:
            await _save_orders()
            before = await _rows(client, "mv_orders")
            calls = _spy(monkeypatch, client)
            with pytest.raises(SurrealDbError) as g1:
                await MvOnSource.define_view()
            assert _sent(calls) == ["INFO FOR DB;", "SELECT id FROM `mv_orders` LIMIT 1;"]
            calls.clear()
            with pytest.raises(SurrealDbError) as g4:
                await MvOnSource.define_view(overwrite=False)
            assert _sent(calls) == ["INFO FOR DB;"]
            calls.clear()
            with pytest.raises(SurrealDbError) as g2:
                await MvOnSource.remove_view()
            assert _sent(calls) == ["INFO FOR DB;"]
            assert await _rows(client, "mv_orders") == before
            assert "AS SELECT" not in (await _tables(client))["mv_orders"]
        assert (type(g1.value), str(g1.value)) == (SurrealDbError, _g1("MvOnSource", "mv_orders"))
        assert (type(g4.value), str(g4.value)) == (SurrealDbError, _g4("MvOnSource", "mv_orders"))
        assert (type(g2.value), str(g2.value)) == (SurrealDbError, _g2("MvOnSource", "mv_orders"))

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_table_an_event_created_becomes_the_view(self, kind: str) -> None:
        """M19, D14: ``define_events()`` first creates the table, empty; ``define_view()`` turns it into the view and
        its ``OVERWRITE`` drops the event, which the next ``define_events()`` restores."""
        async with _mv(kind) as client:
            await _save_orders()
            await MvStatsEvents.define_events()
            assert (await _tables(client))["mv_order_stats"] == _MV_STATS_TABLE
            with pytest.raises(SurrealDbError) as g4:
                await MvStatsEvents.define_view(overwrite=False)
            assert str(g4.value) == _g4("MvStatsEvents", "mv_order_stats")
            assert await MvStatsEvents.define_view() == MvStatsEvents.view_ddl()
            assert (await _tables(client))["mv_order_stats"] == _MV_STATS_RENDERED
            assert await _groups() == {"new": (1, 1.0), "paid": (2, 15.5)}
            assert "mv_seen" not in await _events_info(client, "mv_order_stats")
            # Measured: on 3.x the event, defined before the OVERWRITE, fired once per group the view was filled
            # with, and was dropped after; 2.x and the embedded engine dropped it first.
            assert len(await _rows(client, "mv_log")) == (2 if await _major(client) >= 3 else 0)
            logged = len(await _rows(client, "mv_log"))
            await MvStatsEvents.define_events()
            assert list(await _events_info(client, "mv_order_stats")) == ["mv_seen"]
            await MvOrder(id="o4", status="paid", amount=1.0).save()
            assert len(await _rows(client, "mv_log")) == logged + 1

    async def test_a_table_a_2x_live_query_created_becomes_the_view(self) -> None:
        """M19: on 2.x a live query creates the table; ``define_view()`` then turns it into the view. WebSocket only
        (the embedded engine has no live queries); 3.x refuses a live query on a missing table."""
        async with _mv("ws") as client:
            await _require_line(client, 2, "a live query creates the table it watches")
            async with MvStats.objects().watch():
                pass
            assert "mv_order_stats" in await _tables(client)
            await _save_orders()
            assert await MvStats.define_view() == MvStats.view_ddl()
            assert (await _tables(client))["mv_order_stats"] == _MV_STATS_RENDERED
            assert await _groups() == {"new": (1, 1.0), "paid": (2, 15.5)}

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_g1_then_its_remedy_as_written(self, kind: str) -> None:
        async with _mv(kind) as client:
            await _save_orders()
            await _apply_ddl(client, "CREATE mv_taken:1 SET a = 1;")
            with pytest.raises(SurrealDbError) as g1:
                await MvTaken.define_view()
            assert str(g1.value) == _g1("MvTaken", "mv_taken")
            assert await _rows(client, "mv_taken") == [{"id": RecordID("mv_taken", 1), "a": 1}]
            await MvTaken.raw_query("REMOVE TABLE `mv_taken`;")
            assert await MvTaken.define_view() == MvTaken.view_ddl()
            assert {row.status: row.total for row in await MvTaken.objects().exec()} == {"new": 1, "paid": 2}


class TestViewPermissionsE2E:
    """AC-17 (servers): a record user may not define nor remove a view — S1 from the guard's ``INFO FOR DB`` — and
    reads ``[]`` from a view defined without ``PERMISSIONS`` (``PERMISSIONS NONE``, M15, D13). The embedded engine's
    session is its owner, which may always define."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_s1_a_record_user(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        if kind == "mem":
            pytest.skip("the embedded engine's session is its owner: it may always define")
        async with _mv(kind) as client, SurrealDBConnectionManager.using("mv"):
            await _save_orders()
            await MvStats.define_view()
            await _apply_ddl(client, _MV_ACCESS)
            await SurrealDBConnectionManager.signup(access="mv_acc", variables={"name": "mv-user"})
            try:
                calls = _spy(monkeypatch, client)
                with pytest.raises(SurrealDbError) as define:
                    await MvStats.define_view()
                with pytest.raises(SurrealDbError) as remove:
                    await MvStats.remove_view()
                assert _sent(calls) == ["INFO FOR DB;"] * 2
                rows = await MvStats.objects().exec()
            finally:
                await SurrealDBConnectionManager.invalidate()
            assert rows == []
            assert (await _tables(client))["mv_order_stats"] == _MV_STATS_RENDERED
        for raised, kind_ in ((define, "definition"), (remove, "removal")):
            assert type(raised.value) is SurrealDbAuthenticationError
            assert str(raised.value).startswith(f"Can't apply view {kind_}: 'mv_order_stats' -> ")
            assert "not enough permissions" in str(raised.value).lower()
