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
"""

import contextlib
import inspect
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from surrealdb import InternalError, RecordID

import surreal_orm_lite
from surreal_orm_lite import (
    AuthenticatedUserMixin,
    BaseSurrealEdge,
    BaseSurrealModel,
    Computed,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    computed,
    define_parser,
    pre_delete,
    pre_save,
    pre_update,
    schema,
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
