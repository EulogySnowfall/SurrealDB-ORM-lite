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

The line facts (spec §1.4, §4, §5.6 C1a–C1d; AC-7): C1 — ``ensure_view_query_supported()`` with a fake ``version()``,
read only when the grouped projection holds a hazard, the exact texts, then from ``define_view()`` before the table
guard — and E2E: on SurrealDB 2.x and the embedded engine each C1 kind refused with ``version()`` as the only request,
on 3.x the same views kept up to date or refused by the server (``Found idiom``), ``math::sum()``/``math::mean()``
value types per line (M5), and a source write missing an aggregated field refused on every line (M8).

Removing a view that another view reads (spec §1.4, §5.6 G5 and S3, §6.1; AC-20; M11): ``view_reads_table()`` and
``dependent_views()`` (pure), G5 — ``ensure_view_removable()`` reading ``version()`` only when another view reads the
table — on fake clients, then E2E: on SurrealDB 2.x and the embedded engine ``remove_view()`` of the inner view raises
G5 before any DDL (in a transaction too), on 3.x the server refuses it (``Can't apply view removal: …``), both views
stay; the outer view first, then the inner one, is removed on every line; a raw ``REMOVE TABLE`` of the source table is
pinned per line.

Transactions, connections, ``OVERWRITE`` and events (spec §7; AC-10, AC-11, AC-12; M10, M14): ``OVERWRITE`` drops the
view's events and ``define_events()`` restores them (3.1 refusing ``OVERWRITE`` on a view that holds records, pinned);
``define_view(tx=tx)`` and ``remove_view(tx=tx)`` apply with the block's source writes, buffered or interactive, and a
block that raises applies neither; on 3.x over WebSocket the block reads its own view and writes, unseen outside it
until the commit; a transaction of another connection is refused (X1) before any request.

Array ids, reads and live queries (spec §5.5, §6.6; AC-13, AC-14; M4, M12, M17): ``utils.coerce_record_id()`` reads a
``list`` or a ``tuple`` as an array record id, so ``get(["paid"])`` and ``filter(id=["paid"])`` address a grouped
view's record; E2E on both lines, a view model read by array id, re-read by ``refresh()``, counted, summed and read by
a ``Subquery``, ``id__in`` sent as ``$_f0 CONTAINS id`` on a view model (SurrealDB 3.x answers no record to
``id IN`` on a view table, pinned raw) and an ``id: str`` declaration pinned (``"['paid']"``); over WebSocket,
``watch()`` and ``live()`` notify a group emptied by a delete as one ``DELETE`` on 3.x, an ``UPDATE`` to a count of 0
then a ``DELETE`` on 2.x; a 3.x notification's record carries no ``id``, which ``live()`` takes from the
notification's record id.

The query cache (spec §6.4, §6.5, D11; AC-15): a read of a view model — ``exec()``, ``all()``, ``get(["paid"])`` — and
a read whose ``Subquery`` reads one (``SubqueryCompiler.view_tables``) are tagged "any table" (``_cached_read`` spied,
no client); E2E on both lines, such a cached read is read again after an ORM write to the view's source table, with no
``QueryCache.invalidate()``, and after ``define_view()``/``remove_view()``.

The round trip (spec §6.7, D12; AC-16; M18): E2E on both lines, ``DatabaseIntrospector`` reads back the ``view_query``
of each §5.3 view as spec §3 renders it, ``generate_models_from_db()`` writes view models whose module imports, and each
generated class's ``view_ddl()``, applied to a fresh database, reads back the same query (the generator's unit cases
are in ``tests/test_model_generator.py``).

The README (spec §11.1; AC-18): section 33's place and subsections, its examples run in order on ``mem://`` (each
printed line equal to its comment), the key phrases of each part and the Security wording (D18), section 31's
generated-models bullet and long-line sentence, and the rows v0.32.0 adds to the features, roadmap and comparison
tables and to both behaviour tables (exact cells, widths kept, in version order).
"""

import ast
import asyncio
import contextlib
import importlib.util
import inspect
import os
import re
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from surrealdb import InternalError, NotAllowedError, NotFoundError, RecordID

import surreal_orm_lite
from surreal_orm_lite import (
    AuthenticatedUserMixin,
    BaseSurrealEdge,
    BaseSurrealModel,
    BufferedTransaction,
    Computed,
    DatabaseIntrospector,
    Event,
    LiveAction,
    Q,
    QueryCache,
    QuerySet,
    Subquery,
    SurrealConfigDict,
    SurrealDbAuthenticationError,
    SurrealDBConnectionManager,
    SurrealDbError,
    SurrealDbNotFoundError,
    Transaction,
    computed,
    define_parser,
    generate_models_from_db,
    live,
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
from surreal_orm_lite.subquery import SubqueryCompiler
from surreal_orm_lite.testing import embedded_database
from surreal_orm_lite.utils import apply_ddl_statements
from tests._readme_tables import EMBEDDED_BEHAVIOUR as _EMBEDDED_BEHAVIOUR
from tests._readme_tables import SERVER_BEHAVIOUR as _SERVER_BEHAVIOUR
from tests._readme_tables import (
    assert_embedded_rows,
    assert_server_header_sentence,
    assert_server_rows,
    assert_server_table,
)
from tests._readme_tables import cells as _cells
from tests._readme_tables import normalised as _normalised
from tests._readme_tables import readme as _readme
from tests._readme_tables import readme_part as _readme_part
from tests._readme_tables import subsection as _subsection
from tests._readme_tables import table_rows as _table_rows
from tests._readme_tables import tables as _md_tables
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
    # V7, issue #286: the calls beside an aggregate, or around one, are outside its argument.
    pytest.param(
        "SELECT g, math::sum(a) + array::len(array::distinct(tags)) AS x FROM t GROUP BY g",
        ValueError,
        _v7("array::distinct"),
        id="V7-beside-an-aggregate",
    ),
    pytest.param(
        "SELECT g, math::sum(v) + math::median(v) AS x FROM t GROUP BY g",
        ValueError,
        _v7("math::median"),
        id="V7-after-an-aggregate",
    ),
    pytest.param(
        "SELECT g, array::distinct(math::sum(v)) AS x FROM t GROUP BY g",
        ValueError,
        _v7("array::distinct"),
        id="V7-around-an-aggregate",
    ),
    pytest.param(
        "SELECT g, math::max(string::len(')')) + array::join(v, ',') AS x FROM t GROUP BY g",
        ValueError,
        _v7("array::join"),
        id="V7-bracket-in-a-literal",
    ),
]

#: Issue #286: the issue's query — two aggregates in one element, a listed function inside the second's argument.
_SEVERAL_AGGREGATES = (
    "SELECT status, math::sum(amount) + math::sum(array::len(array::distinct(tags))) AS x FROM orders GROUP BY status"
)

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
    pytest.param(_SEVERAL_AGGREGATES, _SEVERAL_AGGREGATES, id="inside-one-of-several-aggregates"),
    pytest.param(
        "SELECT math::max(array::len(array::distinct(tags))) - math::min(array::len(array::group(v))) AS d FROM t GROUP ALL",
        "SELECT math::max(array::len(array::distinct(tags))) - math::min(array::len(array::group(v))) AS d FROM t GROUP ALL",
        id="inside-each-of-two-aggregates",
    ),
    pytest.param(
        "SELECT g, count(array::distinct(tags)) * 2 AS n FROM t GROUP BY g",
        "SELECT g, count(array::distinct(tags)) * 2 AS n FROM t GROUP BY g",
        id="inside-count-in-an-expression",
    ),
    pytest.param(
        "SELECT g, math::sum(math::max(array::distinct(v))) AS s FROM t GROUP BY g",
        "SELECT g, math::sum(math::max(array::distinct(v))) AS s FROM t GROUP BY g",
        id="inside-nested-aggregates",
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
    free_calls: tuple[str, ...] | None = None,
) -> ViewElement:
    """A :class:`ViewElement` built by keyword; its ``free_calls`` are its ``calls`` unless given (issue #286)."""
    return ViewElement(text, alias, aggregate, calls, calls if free_calls is None else free_calls, fields, opaque)


class TestViewOutline:
    """Spec §6.1: ``view_query_outline()`` element by element (values of the design's prototype)."""

    def test_golden_grouped(self) -> None:
        assert view_query_outline(_STATS_QUERY) == ViewOutline(
            grouped=True,
            group=("status",),
            elements=(
                ViewElement("status", None, None, (), (), ("status",), False),
                ViewElement("count()", "total", "count", ("count",), ("count",), (), False),
                ViewElement("math::sum(amount)", "revenue", "math::sum", ("math::sum",), ("math::sum",), (), False),
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
            ViewElement("c.country", "cc", None, (), (), ("c.country",), False),
            ViewElement("count()", "n", "count", ("count",), ("count",), (), False),
            ViewElement("(SELECT count() FROM other GROUP ALL)", "o", None, (), (), (), True),
            ViewElement("`my f`", "f", None, (), (), (), False),
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
            free_calls=("math::max",),
        )

    def test_an_expression_around_an_aggregate_is_not_an_aggregate(self) -> None:
        element = view_query_outline("SELECT g, math::max(v) - math::min(v) AS d FROM t GROUP BY g").elements[1]
        assert element == _element("math::max(v) - math::min(v)", "d", None, ("math::max", "math::min"), ("v", "v"))

    @pytest.mark.parametrize(
        ("element", "calls", "free_calls"),
        [
            pytest.param(
                "math::sum(amount) + math::sum(array::len(array::distinct(tags)))",
                ("math::sum", "math::sum", "array::len", "array::distinct"),
                ("math::sum", "math::sum"),
                id="two-aggregates",
            ),
            pytest.param(
                "math::sum(a) + array::len(array::distinct(tags))",
                ("math::sum", "array::len", "array::distinct"),
                ("math::sum", "array::len", "array::distinct"),
                id="beside-an-aggregate",
            ),
            pytest.param(
                "array::distinct(math::sum(array::len(v)))",
                ("array::distinct", "math::sum", "array::len"),
                ("array::distinct", "math::sum"),
                id="around-an-aggregate",
            ),
            pytest.param(
                "math::max(string::len(')')) + math::median(v)",
                ("math::max", "string::len", "math::median"),
                ("math::max", "math::median"),
                id="bracket-in-a-literal",
            ),
            pytest.param(
                "COUNT(ARRAY::DISTINCT(tags)) * MATH::STDDEV(array::len(v))",
                ("count", "array::distinct", "math::stddev", "array::len"),
                ("count", "math::stddev"),
                id="upper-case",
            ),
            pytest.param(
                "math::sum(math::max(array::distinct(v)))",
                ("math::sum", "math::max", "array::distinct"),
                ("math::sum",),
                id="nested-aggregates",
            ),
        ],
    )
    def test_free_calls_skip_the_arguments_of_aggregates(
        self, element: str, calls: tuple[str, ...], free_calls: tuple[str, ...]
    ) -> None:
        """Issue #286: ``free_calls`` are the calls outside the argument of every aggregate call, in order — those V7
        reads — while ``calls`` keeps every call (C1 reads it)."""
        [_, read] = view_query_outline(_element_query(element)).elements
        assert (read.calls, read.free_calls) == (calls, free_calls)

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

    def test_typed_literal_prefixes_are_not_fields(self) -> None:
        """The prefix of ``d'…'``, ``r"…"``, ``u'…'``, ``s'…'`` (both lines) and ``b"…"`` (3.x) is part of the literal,
        never a field; the fields around such a literal are still read."""
        outline = view_query_outline(
            "SELECT g, d'2026-01-01T00:00:00Z' AS a, r\"src:1\" AS b, u'0190d2b6-0000-7000-8000-000000000000' AS c, "
            "s'x' AS e, b\"00ff\" AS f, amount AS h, status + 'x' AS i, c.country AS j, v + s'x' AS k FROM src"
        )
        assert [element.fields for element in outline.elements] == [
            ("g",),
            (),
            (),
            (),
            (),
            (),
            ("amount",),
            ("status",),
            ("c.country",),
            ("v",),
        ]

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
            # A typed literal: its prefix is no field.
            "d'2026-01-01T00:00:00Z'",
            'r"src:1"',
            "u'0190d2b6-0000-7000-8000-000000000000'",
            "s'x'",
            'b"00ff"',
            "g + s'x'",
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
    # Issue #286: V7 accepts it (the listed function is inside an aggregate's argument); C1 reads it as before.
    ("math::sum(amount) + math::sum(array::len(array::distinct(tags)))", ("wrapped", "math::sum")),
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
    ("v + s'x'", ("field", "v")),
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
    ("SELECT g, count() AS n, d'2026-01-01T00:00:00Z' AS since FROM src GROUP BY g", ()),
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


# --- C1: the grouped projections SurrealDB 2.x computes once per source record (spec §4, §5.6 C1a–C1d, §6.1) ---------

_ENGINE_2X = "SurrealDB 2.x"
_ENGINE_EMBEDDED = "the embedded engine (a SurrealDB 2.x core)"


def _c1a(model: str, function: str, engine: str) -> str:
    return (
        f"{model}.define_view(): {function}() keeps a grouped view up to date on SurrealDB 3.x only, and this "
        f"connection runs {engine}, which accepts such a view over an empty table and then refuses every write to that "
        "table. Drop it from view_query, or connect to a SurrealDB 3.x server. Nothing was defined."
    )


def _c1b(model: str, element: str, function: str, engine: str) -> str:
    return (
        f"{model}.define_view(): view_query selects {element!r}, an expression around the aggregate {function}(), in "
        f"a grouped view; SurrealDB 3.x keeps it up to date, and this connection runs {engine}, which computes it once "
        "per source record: the view would hold a wrong value, or every write to the source table would be refused. "
        f"Select {function}() alone, as its own field, and compute the rest when you read the model; or connect to a "
        "SurrealDB 3.x server. Nothing was defined."
    )


def _c1c(model: str, element: str, field: str, engine: str) -> str:
    if field == "*":
        reading, remedy = "every field", "Select the GROUP BY fields and the aggregates by name instead of '*'."
    else:
        reading = f"the field {field!r}"
        remedy = f"Use {field!r} inside an aggregate (math::max(), math::sum(), …) or add it to GROUP BY."
    return (
        f"{model}.define_view(): view_query selects {element!r} in a grouped view, which reads {reading} outside an "
        f"aggregate and outside its GROUP BY; SurrealDB 3.x refuses such a view, and this connection runs {engine}, "
        "which computes it once per source record: the view would keep the last record's value, or every write to the "
        f"source table would be refused. {remedy} Nothing was defined."
    )


def _c1d(model: str, alias: str, element: str, engine: str) -> str:
    return (
        f"{model}.define_view(): view_query groups by {alias!r}, the alias of {element!r}; SurrealDB 3.x groups by its "
        f"value, and this connection runs {engine}, which groups by the source records' own fields, so every record "
        "would fall into one group. Group by a field of the source table (store the computed value in the source "
        "records if needed), or connect to a SurrealDB 3.x server. Nothing was defined."
    )


#: The three worked examples of spec §5.6, verbatim: (query, ``version()``, embedded, the refusal) — ``OrderStats``.
_C1_EXAMPLES = [
    (
        "SELECT status, math::round(math::sum(amount)) AS revenue FROM orders GROUP BY status",
        "surrealdb-2.7.0",
        False,
        "OrderStats.define_view(): view_query selects 'math::round(math::sum(amount))', an expression around the "
        "aggregate math::sum(), in a grouped view; SurrealDB 3.x keeps it up to date, and this connection runs "
        "SurrealDB 2.x, which computes it once per source record: the view would hold a wrong value, or every write to "
        "the source table would be refused. Select math::sum() alone, as its own field, and compute the rest when you "
        "read the model; or connect to a SurrealDB 3.x server. Nothing was defined.",
    ),
    (
        "SELECT status, math::percentile(amount, 50) AS p FROM orders GROUP BY status",
        "surrealdb-2.7.0",
        False,
        "OrderStats.define_view(): view_query selects 'math::percentile(amount, 50)' in a grouped view, which reads the "
        "field 'amount' outside an aggregate and outside its GROUP BY; SurrealDB 3.x refuses such a view, and this "
        "connection runs SurrealDB 2.x, which computes it once per source record: the view would keep the last "
        "record's value, or every write to the source table would be refused. Use 'amount' inside an aggregate "
        "(math::max(), math::sum(), …) or add it to GROUP BY. Nothing was defined.",
    ),
    (
        "SELECT string::lowercase(status) AS state, count() AS n FROM orders GROUP BY state",
        "surrealdb-2.0.0",
        True,
        "OrderStats.define_view(): view_query groups by 'state', the alias of 'string::lowercase(status)'; SurrealDB "
        "3.x groups by its value, and this connection runs the embedded engine (a SurrealDB 2.x core), which groups by "
        "the source records' own fields, so every record would fall into one group. Group by a field of the source "
        "table (store the computed value in the source records if needed), or connect to a SurrealDB 3.x server. "
        "Nothing was defined.",
    ),
]

#: One query per C1 kind and variant on a 2.7.0 server (model ``M``): (query, the refusal).
_C1_KINDS = [
    ("SELECT g, MATH::STDDEV(v) AS s FROM src GROUP BY g", _c1a("M", "math::stddev", _ENGINE_2X)),
    ("SELECT math::variance(v) AS s FROM src GROUP ALL", _c1a("M", "math::variance", _ENGINE_2X)),
    ("SELECT g, math::sum(v) / count() AS x FROM src GROUP BY g", _c1b("M", "math::sum(v) / count()", "math::sum", _ENGINE_2X)),
    ("SELECT count() * 2 AS x FROM src GROUP ALL", _c1b("M", "count() * 2", "count", _ENGINE_2X)),
    ("SELECT g, v AS x FROM src GROUP BY g", _c1c("M", "v", "v", _ENGINE_2X)),
    (
        "SELECT g, math::percentile(c.v, 50) AS x FROM src GROUP BY g",
        _c1c("M", "math::percentile(c.v, 50)", "c.v", _ENGINE_2X),
    ),
    ("SELECT *, count() AS n FROM src GROUP BY g", _c1c("M", "*", "*", _ENGINE_2X)),
    ("SELECT g AS h, count() AS n FROM src GROUP BY h", _c1d("M", "h", "g", _ENGINE_2X)),
    (
        "SELECT string::lowercase(s) AS `my state`, count() AS n FROM src GROUP BY `my state`",
        _c1d("M", "my state", "string::lowercase(s)", _ENGINE_2X),
    ),
    # Two hazards: the first in projection order is reported.
    ("SELECT g, count() * 2 AS a, v AS b FROM src GROUP BY g", _c1b("M", "count() * 2", "count", _ENGINE_2X)),
]

_C1_KIND_IDS = [
    "aggregate",
    "aggregate-group-all",
    "wrapped",
    "wrapped-count",
    "field",
    "field-path",
    "field-star",
    "alias",
    "alias-quoted",
    "first-of-two",
]

#: Queries without hazard: no ``version()``, no request (the goldens, a non-grouped aggregate, a group alias that
#: names its own field, a backquoted field the reading leaves to the server).
_C1_NONE = [
    *_GOLDEN_QUERIES,
    "SELECT g, math::stddev(scores) AS s FROM t",
    "SELECT g AS h, count() AS n FROM src GROUP BY g",
    "SELECT g, `v` AS x FROM src GROUP BY g",
]


class RoundedStats(BaseSurrealModel):
    """A grouped view with an expression around an aggregate (C1 kind ``wrapped``)."""

    model_config = SurrealConfigDict(
        table_name="order_stats",
        view_query="SELECT status, math::round(math::sum(amount)) AS revenue FROM orders GROUP BY status",
    )

    id: RecordID | None = None
    status: str
    revenue: float


class TestC1:
    """Spec §4, §5.6 C1a–C1d, §6.1 (unit, fake ``version()``): ``ensure_view_query_supported()`` reads the line only
    when the view's grouped projection holds a hazard, refuses it on SurrealDB 2.x and the embedded engine with the
    exact texts, and lets 3.x (and an unreadable version) decide; ``define_view()`` runs it before the table guard."""

    def test_engine_name(self) -> None:
        assert schema._engine_name(2, False) == _ENGINE_2X
        assert schema._engine_name(1, False) == "SurrealDB 1.x"
        assert schema._engine_name(2, True) == _ENGINE_EMBEDDED

    @pytest.mark.parametrize("query", _C1_NONE, ids=["stats", "paid", "totals", "not-grouped", "own-field", "quoted"])
    @pytest.mark.parametrize("embedded", [False, True], ids=["server", "embedded"])
    async def test_no_hazard_no_request(self, query: str, embedded: bool) -> None:
        client = _GuardClient(version="surrealdb-2.7.0")
        assert await schema.ensure_view_query_supported(client, query, model="M", embedded=embedded) is None
        assert client.queries == []
        assert client.version_calls == 0

    @pytest.mark.parametrize(("query", "version", "embedded", "text"), _C1_EXAMPLES, ids=["wrapped", "field", "alias"])
    async def test_the_worked_examples(self, query: str, version: str, embedded: bool, text: str) -> None:
        client = _GuardClient(version=version)
        with pytest.raises(SurrealDbError) as raised:
            await schema.ensure_view_query_supported(client, query, model="OrderStats", embedded=embedded)
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == text
        assert client.queries == []
        assert client.version_calls == 1

    @pytest.mark.parametrize(("query", "text"), _C1_KINDS, ids=_C1_KIND_IDS)
    async def test_each_kind_on_2x(self, query: str, text: str) -> None:
        client = _GuardClient(version="surrealdb-2.7.0")
        with pytest.raises(SurrealDbError) as raised:
            await schema.ensure_view_query_supported(client, query, model="M", embedded=False)
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == text
        assert client.queries == []
        assert client.version_calls == 1

    async def test_the_embedded_wording(self) -> None:
        client = _GuardClient(version="surrealdb-2.0.0")
        with pytest.raises(SurrealDbError) as raised:
            await schema.ensure_view_query_supported(client, _C1_KINDS[0][0], model="M", embedded=True)
        assert str(raised.value) == _c1a("M", "math::stddev", _ENGINE_EMBEDDED)

    @pytest.mark.parametrize("version", ["surrealdb-3.3.4", "surrealdb-3.1.5", "surrealdb", ""])
    @pytest.mark.parametrize("query", [_C1_KINDS[0][0], _C1_KINDS[4][0]], ids=["aggregate", "field"])
    async def test_3x_and_an_unreadable_version_leave_it_to_the_server(self, version: str, query: str) -> None:
        client = _GuardClient(version=version)
        assert await schema.ensure_view_query_supported(client, query, model="M", embedded=False) is None
        assert client.queries == []
        assert client.version_calls == 1

    async def test_define_view_checks_before_the_table_guard(self, use_client: Callable[[Any], None]) -> None:
        """Spec §5.2 step (e): C1 comes before the guard's ``INFO FOR DB`` — even over a table that holds records —
        and before anything is queued in a transaction."""
        client = _guard_client("table", True, version="surrealdb-2.7.0")
        use_client(client)
        tx = _QueuingTx()
        calls: list[Callable[[], Awaitable[str]]] = [
            lambda: RoundedStats.define_view(),
            lambda: RoundedStats.define_view(overwrite=False, tx=tx),  # type: ignore[arg-type]
        ]
        for call in calls:
            with pytest.raises(SurrealDbError) as raised:
                await call()
            assert type(raised.value) is SurrealDbError
            assert str(raised.value) == _c1b("RoundedStats", "math::round(math::sum(amount))", "math::sum", _ENGINE_2X)
        assert client.queries == []
        assert client.version_calls == 2
        assert tx.added == []

    async def test_define_view_names_the_embedded_engine(
        self, use_client: Callable[[Any], None], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _guard_client("none", False, version="surrealdb-2.0.0")
        use_client(client)
        monkeypatch.setattr(RoundedStats._connection_manager(), "is_embedded", classmethod(lambda cls: True))
        with pytest.raises(SurrealDbError) as raised:
            await RoundedStats.define_view()
        assert str(raised.value) == _c1b("RoundedStats", "math::round(math::sum(amount))", "math::sum", _ENGINE_EMBEDDED)
        assert client.queries == []

    async def test_define_view_on_3x_goes_on(self, use_client: Callable[[Any], None]) -> None:
        client = _guard_client("none", False, version="surrealdb-3.3.4")
        use_client(client)
        assert await RoundedStats.define_view() == RoundedStats.view_ddl()
        assert client.queries == [_INFO, RoundedStats.view_ddl()]
        assert client.version_calls == 1


# --- G5: removing a view that another view reads (spec §1.4, §4, §5.2 step e′, §5.6 G5, §6.1; AC-20) -----------------

#: ``view_reads_table()``'s cases (spec §6.1, ruling P5): (view query, table, whether the query reads the table).
_READS_TABLE = [
    ("SELECT n FROM inner_v", "inner_v", True),
    ("SELECT * FROM a, inner_v WHERE x", "inner_v", True),
    # 3.x renders a keyword table name bare, 2.x in backquotes (M18).
    ("SELECT * FROM `select`", "select", True),
    ("SELECT * FROM select", "select", True),
    ("SELECT * FROM inner_vx", "inner_v", False),
    ("SELECT * FROM a WHERE x INSIDE (SELECT VALUE id FROM inner_v)", "inner_v", False),
    ("SELECT status, count() AS n FROM orders GROUP BY status", "orders", True),
    ("SELECT * FROM ⟨inner_v⟩", "inner_v", True),
    ("SELECT n", "inner_v", False),
    # Left to the server: a graph path, a record id, ONLY.
    ("SELECT * FROM ->knows->person", "person", False),
    ("SELECT * FROM inner_v:1", "inner_v", False),
    ("SELECT * FROM ONLY inner_v", "inner_v", False),
    # The targets end at the first clause keyword; ``from`` in a function path is not the clause.
    ("select n from inner_v split n", "inner_v", True),
    ("SELECT time::from::unix(at) AS t FROM inner_v", "inner_v", True),
    ("SELECT * FROM a, b", "inner_v", False),
]

_READS_TABLE_IDS = [
    "one",
    "second-of-two",
    "backquoted-keyword",
    "bare-keyword",
    "longer-name",
    "sub-select",
    "group-by",
    "angle-quoted",
    "no-from",
    "graph-path",
    "record-id",
    "only",
    "lower-case-split",
    "from-in-a-function-path",
    "other-tables",
]

#: The view over ``MvStats``' view (AC-20), as ``INFO FOR DB`` renders it on every engine.
_MV_TOTALS_RENDERED = "DEFINE TABLE mv_stats_totals TYPE ANY SCHEMALESS AS SELECT total FROM mv_order_stats PERMISSIONS NONE"

#: A view definition ``parse_define_table()`` cannot read, though its text names ``mv_order_stats`` after ``FROM``.
_MV_UNREADABLE_VIEW = "DEFINE TABLE mv_bad TYPE ANY SCHEMALESS SOMEDAY 1 AS SELECT total FROM mv_order_stats PERMISSIONS NONE"

#: ``mv_orders``, an ordinary table, as ``INFO FOR DB`` renders it.
_MV_ORDERS_TABLE = "DEFINE TABLE mv_orders TYPE ANY SCHEMALESS PERMISSIONS NONE"

_REMOVE_MV_STATS = "REMOVE TABLE IF EXISTS `mv_order_stats`;"


def _g5(model: str, table: str, names: str, engine: str) -> str:
    return (
        f"{model}.remove_view(): other views read the table '{table}' ({names}); SurrealDB 3.x refuses to remove it, "
        f"and this connection runs {engine}, which would remove it and turn those views into ordinary tables that keep "
        "their last records and no longer update. Remove those views first. Nothing was removed."
    )


def _removal_client(*dependents: str, version: str, ddl_error: BaseException | None = None) -> _GuardClient:
    """A fake client whose ``INFO FOR DB`` holds ``mv_order_stats``, its source and a view over it per *dependents*."""
    tables: dict[str, object] = {"mv_order_stats": _MV_STATS_RENDERED, "mv_orders": _MV_ORDERS_TABLE}
    for name in dependents:
        tables[name] = f"DEFINE TABLE {name} TYPE ANY SCHEMALESS AS SELECT total FROM mv_order_stats PERMISSIONS NONE"
    return _GuardClient(tables, version=version, ddl_error=ddl_error)


@pytest.fixture
def use_mv_client(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Route ``MvStats`` and ``MvStatsTotals`` to the client given — through a fake connection class, embedded or not —
    without registering the connection ``"mv"``."""

    def use(client: Any, *, embedded: bool = False) -> None:
        class Manager:
            @staticmethod
            async def get_client() -> Any:
                return client

            @staticmethod
            def is_embedded() -> bool:
                return embedded

        for model in (MvStats, MvStatsTotals):
            monkeypatch.setattr(model, "_connection_manager", classmethod(lambda cls: Manager))

    return use


class TestViewReadsTable:
    """Spec §6.1, ruling P5 (pure): ``view_reads_table()`` reads the plain ``FROM`` targets of a view query and leaves
    graph paths, record ids and sub-selects to the server; ``dependent_views()`` lists, sorted, the other views of an
    ``INFO FOR DB`` map whose query reads a table."""

    @pytest.mark.parametrize(("query", "table", "reads"), _READS_TABLE, ids=_READS_TABLE_IDS)
    def test_view_reads_table(self, query: str, table: str, reads: bool) -> None:
        assert schema.view_reads_table(query, table) is reads

    @pytest.mark.parametrize("view", sorted(_RENDERED))
    def test_the_golden_renderings(self, view: str) -> None:
        query = define_parser.parse_define_table(_RENDERED[view]).view_query
        assert query is not None
        assert schema.view_reads_table(query, "orders") is True
        assert schema.view_reads_table(query, "order_stats") is False
        assert schema.view_reads_table(query, view) is False

    def test_dependent_views(self) -> None:
        with pytest.raises(define_parser.DefineParseError):
            define_parser.parse_define_table(_MV_UNREADABLE_VIEW)
        tables: dict[str, object] = {
            "mv_order_stats": _MV_STATS_RENDERED,
            "mv_stats_totals": _MV_TOTALS_RENDERED,
            "mv_b": "DEFINE TABLE mv_b TYPE ANY SCHEMALESS AS SELECT status FROM mv_order_stats WHERE total > 1 "
            "PERMISSIONS NONE",
            "mv_orders": _MV_ORDERS_TABLE,
            "mv_bad": _MV_UNREADABLE_VIEW,
            "mv_odd": 42,
        }
        assert schema.dependent_views(tables, "mv_order_stats") == ("mv_b", "mv_stats_totals")
        assert schema.dependent_views(tables, "mv_orders") == ("mv_order_stats",)
        assert schema.dependent_views(tables, "mv_stats_totals") == ()
        assert schema.dependent_views(tables, "mv_missing") == ()
        assert schema.dependent_views({}, "mv_order_stats") == ()

    def test_the_table_itself_is_never_listed(self) -> None:
        tables = {"loop": "DEFINE TABLE loop TYPE ANY SCHEMALESS AS SELECT * FROM loop PERMISSIONS NONE"}
        assert schema.view_reads_table("SELECT * FROM loop", "loop") is True
        assert schema.dependent_views(tables, "loop") == ()


class TestRemovalGuard:
    """Spec §5.2 step (e′), §5.6 G5, AC-20 (unit, fake clients): ``ensure_view_removable()`` reads ``version()`` only
    when another view reads the table, refuses the removal on SurrealDB 2.x and the embedded engine with G5's exact
    text, and lets 3.x refuse it itself (S3, ``removal``); ``remove_view()`` runs it on the guard's one ``INFO FOR
    DB``, before any DDL and before anything is queued."""

    async def test_no_dependent_no_request(self) -> None:
        client = _GuardClient(version="surrealdb-2.7.0")
        assert await schema.ensure_view_removable(client, "t", (), model="M", embedded=False) is None
        assert client.queries == []
        assert client.version_calls == 0

    async def test_the_text_names_the_dependents_sorted(self) -> None:
        client = _GuardClient(version="surrealdb-2.7.0")
        with pytest.raises(SurrealDbError) as raised:
            await schema.ensure_view_removable(client, "t", ["zeta", "alpha"], model="M", embedded=False)
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == _g5("M", "t", "'alpha', 'zeta'", _ENGINE_2X)
        assert client.queries == []
        assert client.version_calls == 1

    @pytest.mark.parametrize("version", ["surrealdb-3.3.4", "surrealdb-3.1.5", "surrealdb", ""])
    async def test_3x_and_an_unreadable_version_leave_it_to_the_server(self, version: str) -> None:
        client = _GuardClient(version=version)
        assert await schema.ensure_view_removable(client, "t", ("u",), model="M", embedded=False) is None
        assert client.queries == []
        assert client.version_calls == 1

    async def test_g5_from_remove_view(self, use_mv_client: Callable[..., None]) -> None:
        """AC-20: the exact text of the spec; the spy sees ``INFO FOR DB;`` and ``version()`` only, no ``REMOVE``."""
        client = _removal_client("mv_stats_totals", version="surrealdb-2.7.0")
        use_mv_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await MvStats.remove_view()
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == (
            "MvStats.remove_view(): other views read the table 'mv_order_stats' ('mv_stats_totals'); SurrealDB 3.x "
            "refuses to remove it, and this connection runs SurrealDB 2.x, which would remove it and turn those views "
            "into ordinary tables that keep their last records and no longer update. Remove those views first. Nothing "
            "was removed."
        )
        assert client.queries == [_INFO]
        assert client.version_calls == 1

    async def test_two_dependents_sorted(self, use_mv_client: Callable[..., None]) -> None:
        client = _removal_client("mv_totals", "mv_b", version="surrealdb-2.7.0")
        use_mv_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await MvStats.remove_view(missing_ok=False)
        assert str(raised.value) == _g5("MvStats", "mv_order_stats", "'mv_b', 'mv_totals'", _ENGINE_2X)
        assert client.queries == [_INFO]
        assert client.version_calls == 1

    async def test_the_embedded_wording(self, use_mv_client: Callable[..., None]) -> None:
        client = _removal_client("mv_stats_totals", version="surrealdb-2.0.0")
        use_mv_client(client, embedded=True)
        with pytest.raises(SurrealDbError) as raised:
            await MvStats.remove_view()
        assert str(raised.value) == _g5("MvStats", "mv_order_stats", "'mv_stats_totals'", _ENGINE_EMBEDDED)
        assert client.queries == [_INFO]
        assert client.version_calls == 1

    async def test_before_anything_is_queued(self, use_mv_client: Callable[..., None]) -> None:
        client = _removal_client("mv_stats_totals", version="surrealdb-2.7.0")
        use_mv_client(client)
        tx = _QueuingTx()
        tx._connection_name = "mv"
        with pytest.raises(SurrealDbError) as raised:
            await MvStats.remove_view(tx=tx)  # type: ignore[arg-type]
        assert str(raised.value) == _g5("MvStats", "mv_order_stats", "'mv_stats_totals'", _ENGINE_2X)
        assert tx.added == []
        assert client.queries == [_INFO]

    @pytest.mark.parametrize("version", ["surrealdb-3.3.4", "surrealdb-3.1.5", "surrealdb"])
    async def test_3x_sends_the_removal(self, use_mv_client: Callable[..., None], version: str) -> None:
        client = _removal_client("mv_stats_totals", version=version)
        use_mv_client(client)
        assert await MvStats.remove_view() == _REMOVE_MV_STATS
        assert client.queries == [_INFO, _REMOVE_MV_STATS]
        assert client.version_calls == 1

    async def test_s3_the_removal_3x_refuses(self, use_mv_client: Callable[..., None]) -> None:
        """P3: the server's reason whole, ``removal``."""
        reason = (
            "Invalid query: Cannot delete table `mv_order_stats` on which a view is defined, table(s) `mv_stats_totals` "
            "are defined as a view on this table."
        )
        error = InternalError("Internal", reason)
        client = _removal_client("mv_stats_totals", version="surrealdb-3.3.4", ddl_error=error)
        use_mv_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await MvStats.remove_view()
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == f"Can't apply view removal: 'mv_order_stats' -> {reason}"
        assert raised.value.__cause__ is error
        assert client.queries == [_INFO, _REMOVE_MV_STATS]

    @pytest.mark.parametrize("embedded", [False, True], ids=["server", "embedded"])
    async def test_no_dependent_view_no_version(self, use_mv_client: Callable[..., None], embedded: bool) -> None:
        """The outer view first: no view reads it, so no ``version()``, on every line."""
        client = _removal_client("mv_stats_totals", version="surrealdb-2.7.0")
        use_mv_client(client, embedded=embedded)
        assert await MvStatsTotals.remove_view() == "REMOVE TABLE IF EXISTS `mv_stats_totals`;"
        assert client.queries == [_INFO, "REMOVE TABLE IF EXISTS `mv_stats_totals`;"]
        assert client.version_calls == 0

    async def test_an_ordinary_table_is_g2_without_version(self, use_mv_client: Callable[..., None]) -> None:
        """Step (e) first: a table a view still reads but that is not a view itself is G2, not G5."""
        client = _GuardClient(
            {"mv_order_stats": _MV_STATS_TABLE, "mv_stats_totals": _MV_TOTALS_RENDERED}, version="surrealdb-2.7.0"
        )
        use_mv_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await MvStats.remove_view()
        assert str(raised.value) == _g2("MvStats", "mv_order_stats")
        assert client.queries == [_INFO]
        assert client.version_calls == 0


#: ``mv_stats_totals``' removal, as ``remove_view()`` queues it (``missing_ok`` True, then False).
_REMOVE_MV_TOTALS = "REMOVE TABLE IF EXISTS `mv_stats_totals`;"
_REMOVE_MV_TOTALS_STRICT = "REMOVE TABLE `mv_stats_totals`;"


def _buffered(connection: str = "mv") -> BufferedTransaction:
    """A buffered transaction of *connection*, as ``transaction()`` opens one on SurrealDB 2.x (nothing is sent before
    its commit)."""
    tx = BufferedTransaction(client=None)
    tx._connection_name = connection
    return tx


#: ``(queued statements, dependents left)`` for the dependents ``("mv_b", "mv_stats_totals")`` (issue #286).
_QUEUED_REMOVALS = [
    pytest.param([], ("mv_b", "mv_stats_totals"), id="nothing-queued"),
    pytest.param([_REMOVE_MV_TOTALS], ("mv_b",), id="removed-if-exists"),
    pytest.param([_REMOVE_MV_TOTALS_STRICT], ("mv_b",), id="removed-strict"),
    pytest.param([_REMOVE_MV_TOTALS, "REMOVE TABLE IF EXISTS `mv_b`;"], (), id="both-removed"),
    pytest.param(
        ["CREATE mv_orders:o9 SET status = 'new';", _REMOVE_MV_TOTALS, "REMOVE TABLE IF EXISTS `mv_other`;"],
        ("mv_b",),
        id="other-statements-around",
    ),
    pytest.param(
        [_REMOVE_MV_TOTALS, "DEFINE TABLE OVERWRITE `mv_stats_totals` AS (SELECT total FROM mv_order_stats);"],
        ("mv_b", "mv_stats_totals"),
        id="defined-again-after",
    ),
    pytest.param(
        [_REMOVE_MV_TOTALS, "DEFINE TABLE mv_stats_totals AS SELECT total FROM mv_order_stats;"],
        ("mv_b", "mv_stats_totals"),
        id="hand-written-define-after",
    ),
    pytest.param(
        [_REMOVE_MV_TOTALS, "CREATE MV_STATS_TOTALS SET total = 1;"],
        ("mv_b", "mv_stats_totals"),
        id="named-again-after-in-any-case",
    ),
    pytest.param(
        ["DEFINE TABLE OVERWRITE `mv_stats_totals` AS (SELECT total FROM mv_order_stats);", _REMOVE_MV_TOTALS],
        ("mv_b",),
        id="defined-then-removed",
    ),
    pytest.param(["REMOVE TABLE mv_stats_totals;"], ("mv_b", "mv_stats_totals"), id="hand-written-removal"),
    pytest.param(["REMOVE TABLE IF EXISTS `mv_stats_totals`; "], ("mv_b", "mv_stats_totals"), id="not-the-exact-text"),
    pytest.param(["REMOVE TABLE IF EXISTS `mv_stats_totals_2`;"], ("mv_b", "mv_stats_totals"), id="a-longer-name"),
    pytest.param(["REMOVE TABLE IF EXISTS `mv_stats`;"], ("mv_b", "mv_stats_totals"), id="a-shorter-name"),
]


class TestQueuedRemovals:
    """Issue #286 (G5 in a buffered transaction): ``dependents_not_removed()`` leaves out a dependent view only when
    the last queued statement that names it is ``remove_view()``'s exact removal of it; ``remove_view(tx=tx)`` applies
    it to a :class:`BufferedTransaction` before G5, so a chain removed outer view first in one block is accepted, and
    every other case is still refused."""

    @pytest.mark.parametrize(("statements", "left"), _QUEUED_REMOVALS)
    def test_dependents_not_removed(self, statements: list[str], left: tuple[str, ...]) -> None:
        assert schema.dependents_not_removed(("mv_b", "mv_stats_totals"), statements) == left

    def test_the_order_of_the_dependents_is_kept(self) -> None:
        assert schema.dependents_not_removed(["zeta", "alpha"], []) == ("zeta", "alpha")
        assert schema.dependents_not_removed(("zeta", "mid", "alpha"), ["REMOVE TABLE IF EXISTS `mid`;"]) == (
            "zeta",
            "alpha",
        )

    def test_a_name_that_is_no_identifier_is_kept(self) -> None:
        """``remove_view()`` never removes such a table (a model's table name is an identifier): it stays counted."""
        assert schema.dependents_not_removed(("my-view",), ["REMOVE TABLE IF EXISTS `my-view`;"]) == ("my-view",)

    async def test_the_chain_outer_view_first_in_one_block(self, use_mv_client: Callable[..., None]) -> None:
        """The issue's block on SurrealDB 2.x: both removals queued, G5 not raised, ``version()`` never read."""
        client = _removal_client("mv_stats_totals", version="surrealdb-2.7.0")
        use_mv_client(client)
        tx = _buffered()
        assert await MvStatsTotals.remove_view(tx=tx) == _REMOVE_MV_TOTALS
        assert await MvStats.remove_view(tx=tx) == _REMOVE_MV_STATS
        assert tx.statements == [_REMOVE_MV_TOTALS, _REMOVE_MV_STATS]
        assert client.queries == [_INFO, _INFO]
        assert client.version_calls == 0

    @pytest.mark.parametrize("embedded", [False, True], ids=["server", "embedded"])
    async def test_missing_ok_false_and_the_embedded_engine(self, use_mv_client: Callable[..., None], embedded: bool) -> None:
        client = _removal_client("mv_stats_totals", version="surrealdb-2.0.0" if embedded else "surrealdb-2.7.0")
        use_mv_client(client, embedded=embedded)
        tx = _buffered()
        assert await MvStatsTotals.remove_view(missing_ok=False, tx=tx) == _REMOVE_MV_TOTALS_STRICT
        assert await MvStats.remove_view(missing_ok=False, tx=tx) == "REMOVE TABLE `mv_order_stats`;"
        assert tx.statements == [_REMOVE_MV_TOTALS_STRICT, "REMOVE TABLE `mv_order_stats`;"]
        assert client.version_calls == 0

    async def test_only_the_dependents_removed_are_left_out(self, use_mv_client: Callable[..., None]) -> None:
        client = _removal_client("mv_stats_totals", "mv_b", version="surrealdb-2.7.0")
        use_mv_client(client)
        tx = _buffered()
        await MvStatsTotals.remove_view(tx=tx)
        with pytest.raises(SurrealDbError) as raised:
            await MvStats.remove_view(tx=tx)
        assert str(raised.value) == _g5("MvStats", "mv_order_stats", "'mv_b'", _ENGINE_2X)
        assert tx.statements == [_REMOVE_MV_TOTALS]

    async def test_the_inner_view_first_is_still_refused(self, use_mv_client: Callable[..., None]) -> None:
        client = _removal_client("mv_stats_totals", version="surrealdb-2.7.0")
        use_mv_client(client)
        tx = _buffered()
        with pytest.raises(SurrealDbError) as raised:
            await MvStats.remove_view(tx=tx)
        assert str(raised.value) == _g5("MvStats", "mv_order_stats", "'mv_stats_totals'", _ENGINE_2X)
        assert tx.statements == []
        assert client.version_calls == 1

    async def test_removed_in_another_transaction_is_still_refused(self, use_mv_client: Callable[..., None]) -> None:
        client = _removal_client("mv_stats_totals", version="surrealdb-2.7.0")
        use_mv_client(client)
        other, tx = _buffered(), _buffered()
        await MvStatsTotals.remove_view(tx=other)
        with pytest.raises(SurrealDbError) as raised:
            await MvStats.remove_view(tx=tx)
        assert str(raised.value) == _g5("MvStats", "mv_order_stats", "'mv_stats_totals'", _ENGINE_2X)
        assert (other.statements, tx.statements) == ([_REMOVE_MV_TOTALS], [])

    async def test_defined_again_after_its_removal_is_still_refused(self, use_mv_client: Callable[..., None]) -> None:
        client = _removal_client("mv_stats_totals", version="surrealdb-2.7.0")
        use_mv_client(client)
        tx = _buffered()
        await MvStatsTotals.remove_view(tx=tx)
        await MvStatsTotals.define_view(tx=tx)
        with pytest.raises(SurrealDbError) as raised:
            await MvStats.remove_view(tx=tx)
        assert str(raised.value) == _g5("MvStats", "mv_order_stats", "'mv_stats_totals'", _ENGINE_2X)
        assert tx.statements == [_REMOVE_MV_TOTALS, MvStatsTotals.view_ddl()]

    async def test_a_hand_written_removal_is_still_refused(self, use_mv_client: Callable[..., None]) -> None:
        """Only the ORM's exact statement is read: a removal added by hand leaves the refusal on the safe side."""
        client = _removal_client("mv_stats_totals", version="surrealdb-2.7.0")
        use_mv_client(client)
        tx = _buffered()
        await tx.add("REMOVE TABLE mv_stats_totals;")
        with pytest.raises(SurrealDbError) as raised:
            await MvStats.remove_view(tx=tx)
        assert str(raised.value) == _g5("MvStats", "mv_order_stats", "'mv_stats_totals'", _ENGINE_2X)

    async def test_an_interactive_transaction_is_unchanged(self, use_mv_client: Callable[..., None]) -> None:
        """Not a buffered transaction (SurrealDB 3.x over WebSocket): its statements are not read, and 3.x decides."""
        client = _removal_client("mv_stats_totals", version="surrealdb-3.3.4")
        use_mv_client(client)
        tx = _QueuingTx()
        tx._connection_name = "mv"
        await MvStatsTotals.remove_view(tx=tx)  # type: ignore[arg-type]
        assert await MvStats.remove_view(tx=tx) == _REMOVE_MV_STATS  # type: ignore[arg-type]
        assert client.version_calls == 1
        assert tx.added == [(_REMOVE_MV_TOTALS, None), (_REMOVE_MV_STATS, None)]


class TestCoerceArrayIds:
    """AC-13 (unit): ``utils.coerce_record_id()`` reads a ``list`` or a ``tuple`` as an array record id — a grouped
    view's (M4) — and every row of its table before v0.32.0 is unchanged."""

    @pytest.mark.parametrize("value", [["a", 1], ("a", 1)], ids=["list", "tuple"])
    def test_a_sequence_is_an_array_id(self, value: Any) -> None:
        record_id = utils.coerce_record_id(value, "t")
        assert record_id == RecordID("t", ["a", 1])
        assert type(record_id.id) is list

    def test_an_empty_list_is_the_group_all_id(self) -> None:
        record_id = utils.coerce_record_id([], "t")
        assert (record_id, type(record_id.id)) == (RecordID("t", []), list)

    def test_the_sequence_is_copied(self) -> None:
        value = ["paid"]
        record_id = utils.coerce_record_id(value, "t")
        value.append("eu")
        assert record_id == RecordID("t", ["paid"])

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (5, RecordID("t", 5)),
            ("5", RecordID("t", "5")),
            ("`5`", RecordID("t", "5")),
            ("t:d", RecordID("t", "d")),
            ("Other:d", RecordID("t", "Other:d")),
            ("['paid']", RecordID("t", "['paid']")),
        ],
        ids=["int", "str", "backticks", "own-prefix", "other-prefix", "array-text"],
    )
    def test_the_earlier_rows_are_unchanged(self, value: Any, expected: RecordID) -> None:
        record_id = utils.coerce_record_id(value, "t")
        assert (record_id, type(record_id.id)) == (expected, type(expected.id))

    def test_a_record_id_is_returned_unchanged(self) -> None:
        record_id = RecordID("t", ["paid"])
        assert utils.coerce_record_id(record_id, "t") is record_id

    def test_a_boolean_is_refused(self) -> None:
        with pytest.raises(ValueError, match="^a record id cannot be a boolean$"):
            utils.coerce_record_id(True, "t")

    @pytest.mark.parametrize(
        ("lookup", "value", "sql", "bound"),
        [
            ("exact", ["paid"], "id = $_f0", RecordID("t", ["paid"])),
            ("in", [["paid"], ("new",)], "id IN $_f0", [RecordID("t", ["paid"]), RecordID("t", ["new"])]),
            ("not_in", [["paid"]], "id NOT IN $_f0", [RecordID("t", ["paid"])]),
        ],
        ids=["exact", "in", "not_in"],
    )
    def test_the_id_filter_binds_the_array_id(self, lookup: str, value: Any, sql: str, bound: Any) -> None:
        """``filter(id=[…])`` and ``id__in=[[…], …]`` go through the same rule: the bound value addresses the array
        record, never the text of its ``repr()``."""
        assert utils.build_filter_condition("id", lookup, value, 0, record_table="t") == (sql, {"_f0": bound}, 1)


#: ``MvStats``' two array ids, as ``id__in`` binds them on its table.
_PAID_NEW = [RecordID("mv_order_stats", ["paid"]), RecordID("mv_order_stats", ["new"])]


class TestViewIdIn:
    """AC-13 (unit, ruling A1): on a view model's ``id`` column, ``in`` compiles ``$_f0 CONTAINS id`` — through
    ``filter()``, a ``Q`` (nested or negated) and a ``Subquery`` —, since SurrealDB 3.x answers no record to ``id IN``
    on a view table; ``not_in``, every other column, every ordinary model and a ``Q`` compiled on its own are
    unchanged."""

    def test_a_list(self) -> None:
        assert MvStats.objects().filter(id__in=[["paid"], ("new",)])._compile_query() == (
            "SELECT * FROM mv_order_stats WHERE $_f0 CONTAINS id;",
            {"_f0": _PAID_NEW},
        )

    def test_a_tuple_and_a_set(self) -> None:
        assert MvPaid.objects().filter(id__in=("o1", "o2"))._compile_query() == (
            "SELECT * FROM mv_paid_orders WHERE $_f0 CONTAINS id;",
            {"_f0": [RecordID("mv_paid_orders", "o1"), RecordID("mv_paid_orders", "o2")]},
        )
        assert MvPaid.objects().filter(id__in={"o1"})._compile_query() == (
            "SELECT * FROM mv_paid_orders WHERE $_f0 CONTAINS id;",
            {"_f0": [RecordID("mv_paid_orders", "o1")]},
        )

    def test_a_q(self) -> None:
        assert MvStats.objects().filter(Q(id__in=[["paid"], ["new"]]))._compile_query() == (
            "SELECT * FROM mv_order_stats WHERE $_f0 CONTAINS id;",
            {"_f0": _PAID_NEW},
        )

    def test_a_negated_q(self) -> None:
        assert MvStats.objects().filter(~Q(id__in=[["paid"]]))._compile_query() == (
            "SELECT * FROM mv_order_stats WHERE NOT ($_f0 CONTAINS id);",
            {"_f0": [_PAID_NEW[0]]},
        )

    def test_a_nested_q(self) -> None:
        nested = Q(total__gte=1) & (Q(status="new") | Q(id__in=[["paid"]]))
        assert MvStats.objects().filter(nested)._compile_query() == (
            "SELECT * FROM mv_order_stats WHERE (total >= $_f0 AND (status = $_f1 OR $_f2 CONTAINS id));",
            {"_f0": 1, "_f1": "new", "_f2": [_PAID_NEW[0]]},
        )

    def test_a_subquery(self) -> None:
        busy = Subquery(MvStats.objects().filter(total__gte=2).select("id"))
        assert MvStats.objects().filter(id__in=busy)._compile_query() == (
            "{ LET $_sq0 = (SELECT VALUE id FROM mv_order_stats WHERE total >= $_f0); "
            "(SELECT * FROM mv_order_stats WHERE $_sq0 CONTAINS id) };",
            {"_f0": 2},
        )

    def test_a_subquery_in_a_q(self) -> None:
        busy = Subquery(MvStats.objects().filter(total__gte=2).select("id"))
        assert MvStats.objects().filter(Q(status="new") | Q(id__in=busy))._compile_query() == (
            "{ LET $_sq0 = (SELECT VALUE id FROM mv_order_stats WHERE total >= $_f1); "
            "(SELECT * FROM mv_order_stats WHERE (status = $_f0 OR $_sq0 CONTAINS id)) };",
            {"_f0": "new", "_f1": 2},
        )

    def test_count(self) -> None:
        text, variables = MvStats.objects().filter(id__in=[["paid"], ["new"]])._compile_aggregation_query("count()")
        assert " WHERE $_f0 CONTAINS id " in text, text
        assert variables == {"_f0": _PAID_NEW}

    def test_not_in_is_unchanged(self) -> None:
        assert MvStats.objects().filter(id__not_in=[["paid"]])._compile_query() == (
            "SELECT * FROM mv_order_stats WHERE id NOT IN $_f0;",
            {"_f0": [_PAID_NEW[0]]},
        )
        assert MvStats.objects().filter(~Q(id__not_in=[["paid"]]))._compile_query() == (
            "SELECT * FROM mv_order_stats WHERE NOT (id NOT IN $_f0);",
            {"_f0": [_PAID_NEW[0]]},
        )

    def test_another_column_is_unchanged(self) -> None:
        assert MvStats.objects().filter(status__in=["paid"])._compile_query() == (
            "SELECT * FROM mv_order_stats WHERE status IN $_f0;",
            {"_f0": ["paid"]},
        )

    def test_an_ordinary_model_is_unchanged(self) -> None:
        assert MvOrder.objects().filter(id__in=["o1"])._compile_query() == (
            "SELECT * FROM mv_orders WHERE id IN $_f0;",
            {"_f0": [RecordID("mv_orders", "o1")]},
        )
        busy = Subquery(MvStats.objects().filter(total__gte=2).select("status"))
        assert MvOrder.objects().filter(Q(id__in=["o1"]) | Q(status__in=busy))._compile_query() == (
            "{ LET $_sq0 = (SELECT VALUE status FROM mv_order_stats WHERE total >= $_f1); "
            "(SELECT * FROM mv_orders WHERE (id IN $_f0 OR status IN $_sq0)) };",
            {"_f0": [RecordID("mv_orders", "o1")], "_f1": 2},
        )

    def test_a_q_on_its_own_is_unchanged(self) -> None:
        assert Q(id__in=[["paid"]]).to_sql() == ("id IN $_f0", {"_f0": [["paid"]]}, 1)

    def test_the_view_keyword(self) -> None:
        """``view=True`` changes ``in`` on the ``id`` column only, and only when the table is known."""
        condition = utils.build_filter_condition
        assert condition("id", "in", [["paid"]], 0, record_table="t", view=True) == (
            "$_f0 CONTAINS id",
            {"_f0": [RecordID("t", ["paid"])]},
            1,
        )
        assert condition("id", "in", [["paid"]], 0, record_table="t") == ("id IN $_f0", {"_f0": [RecordID("t", ["paid"])]}, 1)
        assert condition("id", "not_in", ["a"], 0, record_table="t", view=True) == (
            "id NOT IN $_f0",
            {"_f0": [RecordID("t", "a")]},
            1,
        )
        assert condition("id", "exact", ["paid"], 0, record_table="t", view=True) == (
            "id = $_f0",
            {"_f0": RecordID("t", ["paid"])},
            1,
        )
        assert condition("status", "in", ["a"], 0, record_table="t", view=True) == ("status IN $_f0", {"_f0": ["a"]}, 1)
        assert condition("id", "in", ["a"], 0, view=True) == ("id IN $_f0", {"_f0": ["a"]}, 1)


def _view_envelope(action: str, record: Any, result: Any) -> dict[str, Any]:
    """A live notification as the SDK yields it."""
    return {"action": action, "record": record, "result": result, "id": "lq"}


class TestViewLiveIds:
    """AC-14 (unit, ruling A2): SurrealDB 3.x's notifications of a view carry no ``id``; ``live()``'s instance of a
    view model takes the notification's record id, ``raw`` stays as received; an ordinary model is unchanged."""

    def test_a_view_payload_without_an_id_takes_the_record(self) -> None:
        result = {"status": "paid", "total": 2, "revenue": 15.5}
        event = live.to_change_event(MvStats, _view_envelope("UPDATE", _PAID, result), diff=False)
        assert (event.instance.id, event.instance.status, event.validation_error) == (_PAID, "paid", None)
        assert event.raw is result
        assert result == {"status": "paid", "total": 2, "revenue": 15.5}

    def test_a_view_payload_with_an_id_keeps_it(self) -> None:
        result = {"id": _NEW, "status": "paid", "total": 2, "revenue": 15.5}
        event = live.to_change_event(MvStats, _view_envelope("UPDATE", _PAID, result), diff=False)
        assert event.instance.id == _NEW
        assert event.raw is result

    def test_a_plain_view_payload_takes_the_record(self) -> None:
        record = RecordID("mv_paid_orders", "o1")
        event = live.to_change_event(MvPaid, _view_envelope("CREATE", record, {"customer": "ann", "amount": 10.0}), diff=False)
        assert (event.instance.id, event.instance.customer) == (record, "ann")

    def test_a_diff_create_without_an_id_takes_the_record(self) -> None:
        patches = [{"op": "replace", "path": "", "value": {"status": "gone", "total": 1, "revenue": 3.0}}]
        event = live.to_change_event(MvStats, _view_envelope("CREATE", _GONE, patches), diff=True)
        assert (event.instance.id, event.instance.total) == (_GONE, 1)
        assert event.raw is patches
        assert patches[0]["value"] == {"status": "gone", "total": 1, "revenue": 3.0}

    def test_without_a_record_the_payload_is_hydrated_as_received(self) -> None:
        event = live.to_change_event(
            MvStats, _view_envelope("UPDATE", None, {"status": "paid", "total": 2, "revenue": 1.0}), diff=False
        )
        assert event.instance.id is None

    def test_an_ordinary_model_is_unchanged(self) -> None:
        record = RecordID("mv_orders", "o1")
        event = live.to_change_event(MvOrder, _view_envelope("UPDATE", record, {"status": "paid"}), diff=False)
        assert (event.instance.id, event.instance.status) == (None, "paid")


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
    """AC-5, AC-9, AC-10 (E2E, both lines, WebSocket, HTTP and ``mem://``): ``define_view()`` applies the view and
    returns its statement, SurrealDB fills it and keeps it up to date, a second call converges, ``overwrite=False``
    keeps what exists, ``remove_view()`` removes it; a missing source table is named by the refusal; ``OVERWRITE``
    drops the view's events, which ``define_events()`` restores."""

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
    @pytest.mark.parametrize("filled", [False, True], ids=["empty", "filled"])
    async def test_overwrite_drops_the_events_define_events_restores_them(self, kind: str, filled: bool) -> None:
        """AC-10 (M10, D14): ``define_view()`` then ``define_events()`` puts the event on the view; the next
        ``define_view()`` (``OVERWRITE``) drops it, the next ``define_events()`` restores it, and it fires on a source
        write. Measured with orders saved (``filled``), over WebSocket and HTTP: on 3.3 the event fires once per group
        the ``OVERWRITE`` recomputes, then is dropped; 2.x and the embedded engine drop it first; 3.1 refuses
        ``OVERWRITE`` on a view that holds records (S3: the view and its event are kept) and drops the event of an
        empty view as every line does."""
        async with _mv(kind) as client:
            await _apply_ddl(client, "DEFINE TABLE mv_log TYPE ANY SCHEMALESS;")
            if filled:
                await _save_orders()
            line = await _line(client)
            assert await MvStatsEvents.define_view() == MvStatsEvents.view_ddl()
            await MvStatsEvents.define_events()
            assert list(await _events_info(client, "mv_order_stats")) == ["mv_seen"]
            assert await _rows(client, "mv_log") == []
            if filled and line == (3, 1):
                with pytest.raises(SurrealDbError) as raised:
                    await MvStatsEvents.define_view()
                assert type(raised.value) is SurrealDbError
                assert str(raised.value) == (
                    "Can't apply view definition: 'mv_order_stats' -> There was a problem with the key-value store: The "
                    "key being inserted already exists"
                )
                assert list(await _events_info(client, "mv_order_stats")) == ["mv_seen"]
                fired = 0
            else:
                assert await MvStatsEvents.define_view() == MvStatsEvents.view_ddl()
                assert await _events_info(client, "mv_order_stats") == {}
                fired = 2 if filled and line[0] >= 3 else 0
            assert len(await _rows(client, "mv_log")) == fired
            assert (await _tables(client))["mv_order_stats"] == _MV_STATS_RENDERED
            assert await _groups() == ({"new": (1, 1.0), "paid": (2, 15.5)} if filled else {})
            await MvStatsEvents.define_events()
            assert list(await _events_info(client, "mv_order_stats")) == ["mv_seen"]
            await MvOrder(id="o4", status="paid", amount=1.0).save()
            assert len(await _rows(client, "mv_log")) == fired + 1


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


# --- the line facts and C1 on the engines (spec §1.4, §4, §5.6 C1a–C1d; AC-7; M5, M7, M8) ----------------------------


class MvSpread(BaseSurrealModel):
    """A whole ``math::stddev()`` in a grouped view: kept up to date on 3.x, C1a on 2.x (kind ``aggregate``)."""

    model_config = SurrealConfigDict(
        table_name="mv_order_spread",
        connection="mv",
        view_query="SELECT status, math::stddev(amount) AS spread FROM mv_orders GROUP BY status",
    )

    id: RecordID | None = None
    status: str
    spread: float


class MvRounded(BaseSurrealModel):
    """An expression around ``math::sum()``: kept up to date on 3.x, C1b on 2.x (kind ``wrapped``)."""

    model_config = SurrealConfigDict(
        table_name="mv_rounded",
        connection="mv",
        view_query="SELECT status, math::round(math::sum(amount)) AS revenue FROM mv_orders GROUP BY status",
    )

    id: RecordID | None = None
    status: str
    revenue: float


class MvAverage(BaseSurrealModel):
    """Two aggregates in one expression: kept up to date on 3.x, C1b naming the first on 2.x (kind ``wrapped``)."""

    model_config = SurrealConfigDict(
        table_name="mv_average",
        connection="mv",
        view_query="SELECT status, math::sum(amount) / count() AS average FROM mv_orders GROUP BY status",
    )

    id: RecordID | None = None
    status: str
    average: float


class MvPercentile(BaseSurrealModel):
    """A field read outside an aggregate and outside ``GROUP BY``: refused by 3.x at define (S3), C1c on 2.x (kind
    ``field``)."""

    model_config = SurrealConfigDict(
        table_name="mv_percentile",
        connection="mv",
        view_query="SELECT status, math::percentile(amount, 50) AS p50 FROM mv_orders GROUP BY status",
    )

    id: RecordID | None = None
    status: str
    p50: float


class MvByState(BaseSurrealModel):
    """A ``GROUP BY`` on an alias: one group per value on 3.x, C1d on 2.x (kind ``alias``)."""

    model_config = SurrealConfigDict(
        table_name="mv_by_state",
        connection="mv",
        view_query="SELECT string::lowercase(status) AS state, count() AS total FROM mv_orders GROUP BY state",
    )

    id: RecordID | None = None
    state: str
    total: int


class MvSafeSum(BaseSurrealModel):
    """``math::sum(amount ?? 0)``: a source write without ``amount`` passes (M8's workaround, surrealdb#7447)."""

    model_config = SurrealConfigDict(
        table_name="mv_safe_sums",
        connection="mv",
        view_query="SELECT status, math::sum(amount ?? 0) AS revenue FROM mv_orders GROUP BY status",
    )

    id: RecordID | None = None
    status: str
    revenue: float


#: The ``d'…'`` literal of :class:`MvSince`, as the ORM reads it back.
_MV_SINCE = datetime(2026, 1, 1, tzinfo=UTC)


class MvSince(BaseSurrealModel):
    """A typed literal (``d'…'``) in a grouped projection: a constant, no C1 hazard (its prefix is no field)."""

    model_config = SurrealConfigDict(
        table_name="mv_since",
        connection="mv",
        view_query="SELECT status, count() AS total, d'2026-01-01T00:00:00Z' AS since FROM mv_orders GROUP BY status",
    )

    id: RecordID | None = None
    status: str
    total: int
    since: datetime


class MvTaggedOrder(BaseSurrealModel):
    """``mv_orders`` with tags (issue #286): the source of :class:`MvTagged`."""

    model_config = SurrealConfigDict(table_name="mv_orders", connection="mv")

    id: str | None = None
    status: str
    amount: float
    tags: list[str]


#: Issue #286: two aggregates in one element, ``array::distinct()`` inside the second's argument.
_MV_TAGGED_ELEMENT = "math::sum(amount) + math::sum(array::len(array::distinct(tags)))"


class MvTagged(BaseSurrealModel):
    """Issue #286: V7 accepts it at the class statement; kept up to date on 3.x, C1b on 2.x (kind ``wrapped``)."""

    model_config = SurrealConfigDict(
        table_name="mv_tagged",
        connection="mv",
        view_query=f"SELECT status, {_MV_TAGGED_ELEMENT} AS score FROM mv_orders GROUP BY status",
    )

    id: RecordID | None = None
    status: str
    score: float


async def _scores() -> dict[str, float]:
    """``MvTagged``'s groups, read through the ORM: status → score."""
    return {row.status: row.score for row in await MvTagged.objects().exec()}


#: AC-7 (b): each C1 kind from a model, and its refusal on SurrealDB 2.x (*engine*).
_C1_MODELS: list[tuple[type[BaseSurrealModel], Callable[[str], str]]] = [
    (MvSpread, lambda engine: _c1a("MvSpread", "math::stddev", engine)),
    (MvRounded, lambda engine: _c1b("MvRounded", "math::round(math::sum(amount))", "math::sum", engine)),
    (MvAverage, lambda engine: _c1b("MvAverage", "math::sum(amount) / count()", "math::sum", engine)),
    (MvPercentile, lambda engine: _c1c("MvPercentile", "math::percentile(amount, 50)", "amount", engine)),
    (MvByState, lambda engine: _c1d("MvByState", "state", "string::lowercase(status)", engine)),
]

#: Two integer orders of 10 (AC-7 a): ``math::sum()`` of integers is a float on 3.x and an int on 2.x (M5).
_MV_INTEGER_ORDERS = (
    "CREATE mv_orders:i1 SET status = 'paid', amount = 10; CREATE mv_orders:i2 SET status = 'paid', amount = 10;"
)


class TestViewLinesE2E:
    """AC-7 (E2E, both lines, WebSocket, HTTP and ``mem://``): the value types of each line (M5); C1 refusing, before
    any request but ``version()``, on SurrealDB 2.x and the embedded engine, each grouped projection they compute once
    per source record, while 3.x keeps them up to date or refuses at define (M7); the server's own refusal of a
    source write that misses an aggregated field, and its ``?? 0`` workaround (M8)."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_sum_and_mean_types_per_line(self, kind: str) -> None:
        async with _mv(kind) as client:
            await _apply_ddl(client, _MV_INTEGER_ORDERS)
            await MvStats.define_view()
            major = await _major(client)
            [raw] = await client.query("SELECT revenue FROM mv_order_stats:['paid'];", {})
            assert (type(raw["revenue"]), raw["revenue"]) == ((float, 20.0) if major >= 3 else (int, 20))
            stats = await MvStats.objects().get(RecordID("mv_order_stats", ["paid"]))
            assert (type(stats.revenue), stats.revenue) == (float, 20.0)
            await _apply_ddl(
                client, "DEFINE TABLE mv_means AS SELECT status, math::mean(amount) AS mean FROM mv_orders GROUP BY status;"
            )
            [mean] = await client.query("SELECT mean FROM mv_means:['paid'];", {})
            assert (type(mean["mean"]), mean["mean"]) == ((float, 10.0) if major >= 3 else (Decimal, Decimal("10")))

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize(("model", "refusal"), _C1_MODELS, ids=[model.__name__ for model, _ in _C1_MODELS])
    async def test_c1_on_2x_before_any_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
        kind: str,
        model: type[BaseSurrealModel],
        refusal: Callable[[str], str],
    ) -> None:
        """Over an empty source 2.x would accept each of these views and then compute it per source record, or refuse
        every write to ``mv_orders``: C1 refuses it with ``version()`` as its only request."""
        async with _mv(kind) as client:
            await _require_line(client, 2, "SurrealDB 2.x computes these grouped projections once per source record")
            calls = _spy(monkeypatch, client)
            with pytest.raises(SurrealDbError) as raised:
                await model.define_view()
            assert calls == [("version", ())]
            assert type(raised.value) is SurrealDbError
            assert str(raised.value) == refusal(_ENGINE_EMBEDDED if kind == "mem" else _ENGINE_2X)
            assert model.get_table_name() not in await _tables(client)
            await MvOrder(id="o1", status="paid", amount=10.0).save()
            assert [row["id"] for row in await _rows(client, "mv_orders")] == [RecordID("mv_orders", "o1")]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_3x_keeps_them_up_to_date(self, kind: str) -> None:
        """Defined over the empty source, then maintained by each source write (spec AC-7 b, measured on 3.3.4 and
        3.1.5)."""
        async with _mv(kind) as client:
            await _require_line(client, 3, "these grouped projections are kept up to date on SurrealDB 3.x")
            for model in (MvSpread, MvRounded, MvAverage, MvByState):
                assert await model.define_view() == model.view_ddl()
            await _save_orders()
            assert {row.status: row.revenue for row in await MvRounded.objects().exec()} == {"new": 1.0, "paid": 16.0}
            assert {row.status: row.average for row in await MvAverage.objects().exec()} == {"new": 1.0, "paid": 7.75}
            assert {row.state: row.total for row in await MvByState.objects().exec()} == {"new": 1, "paid": 2}
            spread = {row.status: row.spread for row in await MvSpread.objects().exec()}
            assert spread == pytest.approx({"new": 0.0, "paid": 3.181980515339464})
            await (await MvOrder.objects().get("o2")).delete()
            assert {row.status: row.revenue for row in await MvRounded.objects().exec()} == {"new": 1.0, "paid": 10.0}
            assert {row.status: row.average for row in await MvAverage.objects().exec()} == {"new": 1.0, "paid": 10.0}
            assert {row.state: row.total for row in await MvByState.objects().exec()} == {"new": 1, "paid": 1}
            assert {row.status: row.spread for row in await MvSpread.objects().exec()} == {"new": 0.0, "paid": 0.0}

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_3x_refuses_a_field_outside_group_by(self, kind: str) -> None:
        """C1 lets 3.x decide: the server refuses the view at define (``Found idiom``), named by S3."""
        async with _mv(kind) as client:
            await _require_line(client, 3, "SurrealDB 3.x refuses a field read outside GROUP BY at define")
            with pytest.raises(SurrealDbError) as raised:
                await MvPercentile.define_view()
            assert type(raised.value) is SurrealDbError
            assert str(raised.value).startswith("Can't apply view definition: 'mv_percentile' -> ")
            assert "Found idiom" in str(raised.value)
            assert "mv_percentile" not in await _tables(client)
            await MvOrder(id="o1", status="paid", amount=10.0).save()
            assert [row["id"] for row in await _rows(client, "mv_orders")] == [RecordID("mv_orders", "o1")]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_m8_a_source_write_without_the_aggregated_field(self, kind: str) -> None:
        """M8 (surrealdb#7447), every line: with ``math::sum(amount)`` in a view, a source write without ``amount`` is
        refused by the server, unchanged by the ORM; ``math::sum(amount ?? 0)`` lets it through."""
        async with _mv(kind) as client:
            await MvStats.define_view()
            with pytest.raises(InternalError) as raised:
                await MvOrder(id="x", status="paid").save()
            assert "math::sum()" in str(raised.value)
            assert await _rows(client, "mv_orders") == []
            await MvStats.remove_view()
            await MvSafeSum.define_view()
            await MvOrder(id="x", status="paid").save()
            assert [row["id"] for row in await _rows(client, "mv_orders")] == [RecordID("mv_orders", "x")]
            assert [(row.status, row.revenue) for row in await MvSafeSum.objects().exec()] == [("paid", 0.0)]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_typed_literal_is_no_hazard(self, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
        """A ``d'…'`` literal in a grouped projection reads no field: C1 finds no hazard and sends no ``version()``,
        and every line — SurrealDB 2.x and the embedded engine included — defines the view and keeps it up to date."""
        async with _mv(kind) as client:
            calls = _spy(monkeypatch, client)
            assert await MvSince.define_view() == MvSince.view_ddl()
            assert [method for method, _ in calls if method == "version"] == []
            await _save_orders()
            rows = {row.status: (row.total, row.since) for row in await MvSince.objects().exec()}
            assert rows == {"new": (1, _MV_SINCE), "paid": (2, _MV_SINCE)}
            await (await MvOrder.objects().get("o2")).delete()
            assert {row.status: row.total for row in await MvSince.objects().exec()} == {"new": 1, "paid": 1}

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_3x_keeps_several_aggregates_up_to_date(self, kind: str) -> None:
        """Issue #286: the class statement accepted the query (V7 reads no call inside an aggregate's argument);
        SurrealDB 3.x defines the view and keeps each group's score up to date through saves, updates and deletes."""
        async with _mv(kind) as client:
            await _require_line(client, 3, "an expression around aggregates is kept up to date on SurrealDB 3.x")
            await MvTaggedOrder(id="o1", status="paid", amount=10.0, tags=["a", "b", "a"]).save()
            assert await MvTagged.define_view() == MvTagged.view_ddl()
            assert await _scores() == {"paid": 12.0}
            await MvTaggedOrder(id="o2", status="paid", amount=5.5, tags=["c"]).save()
            await MvTaggedOrder(id="o3", status="new", amount=1.0, tags=[]).save()
            assert await _scores() == {"new": 1.0, "paid": 18.5}
            second = await MvTaggedOrder.objects().get("o2")
            await second.merge(tags=["c", "d", "c"])
            assert await _scores() == {"new": 1.0, "paid": 19.5}
            await (await MvTaggedOrder.objects().get("o1")).delete()
            assert await _scores() == {"new": 1.0, "paid": 7.5}

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_several_aggregates_c1_on_2x(self, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
        """Issue #286: on SurrealDB 2.x and the embedded engine the same view is refused by C1 (``wrapped``, naming
        the first aggregate), not by V7, with ``version()`` as the only request; the source stays writable."""
        async with _mv(kind) as client:
            await _require_line(client, 2, "SurrealDB 2.x computes an expression around aggregates once per record")
            calls = _spy(monkeypatch, client)
            with pytest.raises(SurrealDbError) as raised:
                await MvTagged.define_view()
            assert calls == [("version", ())]
            engine = _ENGINE_EMBEDDED if kind == "mem" else _ENGINE_2X
            assert type(raised.value) is SurrealDbError
            assert str(raised.value) == _c1b("MvTagged", _MV_TAGGED_ELEMENT, "math::sum", engine)
            assert "mv_tagged" not in await _tables(client)
            await MvTaggedOrder(id="o1", status="paid", amount=10.0, tags=["a"]).save()
            assert [row["id"] for row in await _rows(client, "mv_orders")] == [RecordID("mv_orders", "o1")]


# --- removing a view that another view reads (spec §1.4, §4, §7, §5.6 G5 and S3; AC-20; M11) -------------------------


class MvStatsTotals(BaseSurrealModel):
    """A view over ``MvStats``' view (revision 3, AC-20): SurrealDB updates it when the inner view changes (M3)."""

    model_config = SurrealConfigDict(
        table_name="mv_stats_totals", connection="mv", view_query="SELECT total FROM mv_order_stats"
    )

    id: RecordID | None = None
    total: int


async def _totals() -> dict[str, int]:
    """``MvStatsTotals``' records, read through the ORM: the group of ``mv_order_stats`` it copies → its total."""
    totals: dict[str, int] = {}
    for row in await MvStatsTotals.objects().exec():
        assert row.id is not None
        totals[row.id.id[0]] = row.total
    return totals


def _requests(calls: list[tuple[str, tuple[object, ...]]]) -> list[tuple[str, object]]:
    """What :func:`_spy` saw, as ``(method, statement)`` — ``None`` for ``version()`` — without the ``query_raw()``
    the SDK's ``query()`` runs itself."""
    return [(method, args[0] if args else None) for method, args in calls if method != "query_raw"]


class TestViewRemovalE2E:
    """AC-20 (E2E, both lines, WebSocket, HTTP and ``mem://``; 3.1.5 is a 3.x line): removing a view that another
    view reads — G5 before any DDL on SurrealDB 2.x and the embedded engine, the server's refusal on 3.x (S3,
    ``removal``) — leaves both views as they were; the outer view first, then the inner one, works everywhere; a raw
    ``REMOVE TABLE`` of the source table (not an ORM path, README row 8) is pinned per line (M11)."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_inner_view_is_kept(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _mv(kind) as client:
            await _save_orders()
            await MvStats.define_view()
            await MvStatsTotals.define_view()
            assert await _totals() == {"new": 1, "paid": 2}
            major = await _major(client)
            calls = _spy(monkeypatch, client)
            with pytest.raises(SurrealDbError) as raised:
                await MvStats.remove_view()
            requests = _requests(calls)
            tables = await _tables(client)
            assert (tables["mv_order_stats"], tables["mv_stats_totals"]) == (_MV_STATS_RENDERED, _MV_TOTALS_RENDERED)
            await MvOrder(id="o4", status="new", amount=2.0).save()
            assert await _totals() == {"new": 2, "paid": 2}
        assert type(raised.value) is SurrealDbError
        if major >= 3:
            assert str(raised.value).startswith("Can't apply view removal: 'mv_order_stats' -> ")
            assert "Cannot delete table" in str(raised.value)
            assert requests == [("query", "INFO FOR DB;"), ("version", None), ("query", _REMOVE_MV_STATS)]
        else:
            engine = _ENGINE_EMBEDDED if kind == "mem" else _ENGINE_2X
            assert str(raised.value) == _g5("MvStats", "mv_order_stats", "'mv_stats_totals'", engine)
            assert requests == [("query", "INFO FOR DB;"), ("version", None)]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_outer_view_first_then_the_inner_one(self, kind: str) -> None:
        async with _mv(kind) as client:
            await _save_orders()
            await MvStats.define_view()
            await MvStatsTotals.define_view()
            assert await MvStatsTotals.remove_view() == "REMOVE TABLE IF EXISTS `mv_stats_totals`;"
            assert await MvStats.remove_view() == _REMOVE_MV_STATS
            tables = await _tables(client)
            assert "mv_stats_totals" not in tables
            assert "mv_order_stats" not in tables
            assert len(await _rows(client, "mv_orders")) == 3

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_chain_in_one_transaction_block(self, kind: str) -> None:
        """Issue #286: the outer view, then the inner one, in one ``transaction()`` block — buffered on SurrealDB 2.x,
        HTTP and the embedded engine, where G5 leaves out the outer view the block already removes; interactive on 3.x
        over WebSocket, where the server decides — commits on every line, and both views are gone."""
        async with _mv(kind) as client:
            await _save_orders()
            await MvStats.define_view()
            await MvStatsTotals.define_view()
            async with SurrealDBConnectionManager.transaction("mv") as tx:
                assert await MvStatsTotals.remove_view(tx=tx) == "REMOVE TABLE IF EXISTS `mv_stats_totals`;"
                assert await MvStats.remove_view(tx=tx) == _REMOVE_MV_STATS
            tables = await _tables(client)
            assert "mv_stats_totals" not in tables
            assert "mv_order_stats" not in tables
            assert len(await _rows(client, "mv_orders")) == 3

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_g5_when_the_block_defines_the_outer_view_again(self, kind: str) -> None:
        """Issue #286: a ``define_view()`` of the outer view queued after its removal puts it back: G5 refuses the
        inner view's removal, the block commits nothing, both views stay."""
        async with _mv(kind) as client:
            await _require_line(client, 2, "SurrealDB 2.x and the embedded engine would remove a view another view reads")
            await _save_orders()
            await MvStats.define_view()
            await MvStatsTotals.define_view()
            with pytest.raises(SurrealDbError) as raised:
                async with SurrealDBConnectionManager.transaction("mv") as tx:
                    await MvStatsTotals.remove_view(tx=tx)
                    await MvStatsTotals.define_view(tx=tx)
                    await MvStats.remove_view(tx=tx)
            tables = await _tables(client)
            assert (tables["mv_order_stats"], tables["mv_stats_totals"]) == (_MV_STATS_RENDERED, _MV_TOTALS_RENDERED)
            assert await _totals() == {"new": 1, "paid": 2}
        engine = _ENGINE_EMBEDDED if kind == "mem" else _ENGINE_2X
        assert str(raised.value) == _g5("MvStats", "mv_order_stats", "'mv_stats_totals'", engine)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_g5_before_anything_is_queued(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """Inside a ``transaction()`` block (buffered on SurrealDB 2.x and the embedded engine), G5 is raised on
        committed state before the removal is queued; it leaves the block, which commits nothing."""
        async with _mv(kind) as client:
            await _require_line(client, 2, "SurrealDB 2.x and the embedded engine would remove a view another view reads")
            await _save_orders()
            await MvStats.define_view()
            await MvStatsTotals.define_view()
            calls = _spy(monkeypatch, client)
            queued: list[str] | None = None
            with pytest.raises(SurrealDbError) as raised:
                async with SurrealDBConnectionManager.transaction("mv") as tx:
                    assert not tx.is_interactive
                    try:
                        await MvStats.remove_view(tx=tx)
                    finally:
                        queued = list(tx.statements)
            assert queued == []
            assert _requests(calls) == [("query", "INFO FOR DB;"), ("version", None)]
            tables = await _tables(client)
            assert (tables["mv_order_stats"], tables["mv_stats_totals"]) == (_MV_STATS_RENDERED, _MV_TOTALS_RENDERED)
        engine = _ENGINE_EMBEDDED if kind == "mem" else _ENGINE_2X
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == _g5("MvStats", "mv_order_stats", "'mv_stats_totals'", engine)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_raw_removal_of_the_source_table(self, kind: str) -> None:
        """README row 8 (N7: a raw ``REMOVE TABLE`` is not an ORM path): SurrealDB 3.x refuses to remove a table a view
        reads; 2.x and the embedded engine remove it, and the view becomes an ordinary table that keeps its last
        records — its ``define_view()`` then raises G1 and its ``remove_view()`` G2."""
        async with _mv(kind) as client:
            await _save_orders()
            await MvStats.define_view()
            if await _major(client) >= 3:
                with pytest.raises(InternalError) as raised:
                    await client.query("REMOVE TABLE mv_orders;", {})
                assert "Cannot delete table" in str(raised.value)
                tables = await _tables(client)
                assert tables["mv_order_stats"] == _MV_STATS_RENDERED
                assert "mv_orders" in tables
                await MvOrder(id="o4", status="new", amount=2.0).save()
                assert await _groups() == {"new": (2, 3.0), "paid": (2, 15.5)}
            else:
                await client.query("REMOVE TABLE mv_orders;", {})
                tables = await _tables(client)
                assert "mv_orders" not in tables
                assert tables["mv_order_stats"] == _MV_STATS_TABLE
                assert await _rows(client, "mv_order_stats") == _MV_STATS_ROWS
                with pytest.raises(SurrealDbError) as g1:
                    await MvStats.define_view()
                assert (type(g1.value), str(g1.value)) == (SurrealDbError, _g1("MvStats", "mv_order_stats"))
                with pytest.raises(SurrealDbError) as g2:
                    await MvStats.remove_view()
                assert (type(g2.value), str(g2.value)) == (SurrealDbError, _g2("MvStats", "mv_order_stats"))
                assert await _rows(client, "mv_order_stats") == _MV_STATS_ROWS


# --- transactions and connections (spec §7; AC-11, AC-12) -----------------------------------------------------------


class _Rollback(Exception):
    """Raised inside a ``transaction()`` block to roll it back."""


async def _native_txn_supported(client: Any) -> bool:
    """Whether *client* opens native interactive transactions (SurrealDB 3.x over WebSocket): the testing-dual-db
    probe, on the connection ``"mv"``."""
    try:
        txn = await client.begin()
    except Exception:
        return False
    with contextlib.suppress(Exception):
        await client.cancel(txn)
    return True


async def _require_interactive(client: Any) -> None:
    """Skip without native transactions: SurrealDB 2.x buffers a WebSocket transaction (reads in it raise)."""
    if not await _native_txn_supported(client):
        pytest.skip("requires native interactive transactions (SurrealDB 3.x over WebSocket)")


class TestViewTransactionsE2E:
    """AC-11 (E2E, both lines, WebSocket, HTTP and ``mem://``): ``define_view(tx=tx)`` and ``remove_view(tx=tx)`` with
    a source write in the same block — buffered (HTTP, SurrealDB 2.x, the embedded engine) or interactive (3.x over
    WebSocket) — apply together at the end of the block, and a block that raises applies neither (M14); interactive
    only, the block reads its own view and source writes, which nothing outside it sees before the commit."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_define_view_and_a_source_write_commit_together(self, kind: str) -> None:
        async with _mv(kind) as client:
            await _save_orders()
            interactive = kind == "ws" and await _major(client) >= 3
            async with SurrealDBConnectionManager.transaction("mv") as tx:
                assert tx.is_interactive is interactive
                assert await MvStats.define_view(tx=tx) == MvStats.view_ddl()
                await MvOrder(id="o4", status="new", amount=2.0).save(tx=tx)
            assert (await _tables(client))["mv_order_stats"] == _MV_STATS_RENDERED
            assert await _groups() == {"new": (2, 3.0), "paid": (2, 15.5)}

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_block_that_raises_leaves_no_view(self, kind: str) -> None:
        async with _mv(kind) as client:
            await _save_orders()
            with pytest.raises(_Rollback):
                async with SurrealDBConnectionManager.transaction("mv") as tx:
                    await MvStats.define_view(tx=tx)
                    await MvOrder(id="o4", status="new", amount=2.0).save(tx=tx)
                    raise _Rollback
            assert "mv_order_stats" not in await _tables(client)
            assert len(await _rows(client, "mv_orders")) == 3

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_remove_view_and_a_source_write_commit_together(self, kind: str) -> None:
        async with _mv(kind) as client:
            await _save_orders()
            await MvStats.define_view()
            async with SurrealDBConnectionManager.transaction("mv") as tx:
                assert await MvStats.remove_view(tx=tx) == _REMOVE_MV_STATS
                await MvOrder(id="o4", status="new", amount=2.0).save(tx=tx)
            tables = await _tables(client)
            assert "mv_order_stats" not in tables
            assert tables["mv_orders"] == _MV_ORDERS_TABLE
            assert len(await _rows(client, "mv_orders")) == 4

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_block_that_raises_keeps_the_view(self, kind: str) -> None:
        async with _mv(kind) as client:
            await _save_orders()
            await MvStats.define_view()
            with pytest.raises(_Rollback):
                async with SurrealDBConnectionManager.transaction("mv") as tx:
                    await MvStats.remove_view(tx=tx)
                    await MvOrder(id="o4", status="new", amount=2.0).save(tx=tx)
                    raise _Rollback
            assert (await _tables(client))["mv_order_stats"] == _MV_STATS_RENDERED
            assert await _groups() == {"new": (1, 1.0), "paid": (2, 15.5)}
            assert len(await _rows(client, "mv_orders")) == 3

    async def test_the_block_reads_the_view_it_defines(self) -> None:
        """Interactive only: the view defined in the block holds the block's own source write when the block reads
        it; outside the block, on the same connection, the view does not exist until the commit (a missing table
        reads as no rows)."""
        async with _mv("ws") as client:
            await _require_interactive(client)
            await _save_orders()
            async with SurrealDBConnectionManager.transaction("mv") as tx:
                assert tx.is_interactive
                await MvStats.define_view(tx=tx)
                await MvOrder(id="o4", status="new", amount=2.0).save(tx=tx)
                inside = {row.status: (row.total, row.revenue) for row in await MvStats.objects(tx=tx).exec()}
                outside = await MvStats.objects().exec()
                tables = await _tables(client)
            assert inside == {"new": (2, 3.0), "paid": (2, 15.5)}
            assert outside == []
            assert "mv_order_stats" not in tables
            assert await _groups() == {"new": (2, 3.0), "paid": (2, 15.5)}

    async def test_a_source_write_of_the_block_is_seen_inside_only(self) -> None:
        """Interactive only: with the view defined before the block, the block's source write is in the view the block
        reads, not in the one read outside it, until the commit."""
        async with _mv("ws") as client:
            await _require_interactive(client)
            await _save_orders()
            await MvStats.define_view()
            async with SurrealDBConnectionManager.transaction("mv") as tx:
                await MvOrder(id="o4", status="new", amount=2.0).save(tx=tx)
                inside = {row.status: (row.total, row.revenue) for row in await MvStats.objects(tx=tx).exec()}
                outside = await _groups()
            assert inside == {"new": (2, 3.0), "paid": (2, 15.5)}
            assert outside == {"new": (1, 1.0), "paid": (2, 15.5)}
            assert await _groups() == {"new": (2, 3.0), "paid": (2, 15.5)}

    async def test_the_block_removes_the_view_for_itself_first(self) -> None:
        """Interactive only: after ``remove_view(tx=tx)`` the block reads no view (a missing table reads as no rows);
        outside the block the view is still there, kept up to date, until the commit."""
        async with _mv("ws") as client:
            await _require_interactive(client)
            await _save_orders()
            await MvStats.define_view()
            async with SurrealDBConnectionManager.transaction("mv") as tx:
                assert await MvStats.remove_view(tx=tx) == _REMOVE_MV_STATS
                inside = await MvStats.objects(tx=tx).exec()
                outside = await _groups()
                tables = await _tables(client)
            assert inside == []
            assert outside == {"new": (1, 1.0), "paid": (2, 15.5)}
            assert tables["mv_order_stats"] == _MV_STATS_RENDERED
            assert "mv_order_stats" not in await _tables(client)


def _x1(model: str, call: str, tx_connection: str, connection: str) -> str:
    """X1 (spec §5.6): ``routing.check_transaction()``'s text for *call* of *model* (on *connection*) given a
    transaction of *tx_connection*."""
    return (
        f"{model}.{call} cannot run in this transaction: the transaction belongs to connection {tx_connection!r} and "
        f"{model} runs on connection {connection!r}. A transaction is one connection's (one database): open it on "
        f"{connection!r} — SurrealDBConnectionManager.transaction({connection!r}) — or run the block inside "
        f"using({tx_connection!r})."
    )


class TestViewConnectionsE2E:
    """AC-12 (E2E, both lines, WebSocket, HTTP and ``mem://``; the default connection over WebSocket for ``mem``):
    a view model applies its view on its own connection, one without ``connection`` on the ``using()`` block's, and a
    transaction of another connection is refused (X1) before any request."""

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

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize("method", ["define_view", "remove_view"])
    async def test_x1_a_transaction_of_another_connection(
        self, kind: str, method: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with _default(kind) as default, _mv(kind) as client:
            await _save_orders()
            await MvStats.define_view()
            async with SurrealDBConnectionManager.transaction() as tx:
                calls = _spy(monkeypatch, client)
                default_calls = _spy(monkeypatch, default)
                with pytest.raises(SurrealDbError) as raised:
                    await getattr(MvStats, method)(tx=tx)
                assert (calls, default_calls) == ([], [])
                if not tx.is_interactive:
                    assert tx.statements == []
            assert (await _tables(client))["mv_order_stats"] == _MV_STATS_RENDERED
            assert "mv_order_stats" not in await _tables(default)
            assert await _groups() == {"new": (1, 1.0), "paid": (2, 15.5)}
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == _x1("MvStats", f"{method}()", "default", "mv")


class MvStatsStr(BaseSurrealModel):
    """``MvStats``' view read with ``id: str | None``: the id reads as the text of the array id (M4 pin, AC-13)."""

    model_config = SurrealConfigDict(table_name="mv_order_stats", connection="mv", view_query=_MV_STATS_QUERY)

    id: str | None = None
    status: str
    total: int
    revenue: float


#: The array record ids of ``MvStats``' groups (M4).
_PAID = RecordID("mv_order_stats", ["paid"])
_NEW = RecordID("mv_order_stats", ["new"])
_GONE = RecordID("mv_order_stats", ["gone"])


#: The failure of :meth:`TestViewReadsE2E.test_raw_id_in_on_a_view_per_line` once SurrealDB 3.x answers ``id IN`` on
#: a view table again.
_ID_IN_FIXED = (
    "SurrealDB 3.x answers `id IN` on a view table again: narrow the README paragraph on `CONTAINS` (section 33, "
    "'Reading') to the affected versions, and this test's version range with it"
)


def _stat(row: MvStats | None) -> tuple[RecordID | None, str, int, float]:
    """*row*'s id and columns."""
    assert row is not None
    return row.id, row.status, row.total, row.revenue


def _busy() -> Subquery:
    """The statuses of ``MvStats``' groups holding two orders or more, as a ``Subquery`` (AC-13)."""
    return Subquery(MvStats.objects().filter(total__gte=2).select("status"))


class TestViewReadsE2E:
    """AC-13 (E2E, both lines, WebSocket, HTTP and ``mem://``): a grouped view's records looked up by their array id —
    ``get(["paid"])``, ``filter(id=["paid"])``, ``id__in`` (a list, a ``Q``, a ``Subquery``), ``id__not_in`` —,
    re-read by ``refresh()`` after a source write, counted, summed and read by a ``Subquery``; the raw ``id IN`` the
    ORM avoids pinned per line (SurrealDB 3.x answers no record on a view table); declared ``id: str``, the id reads
    as the text of the array (M4 pin)."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_get_by_array_id(self, kind: str) -> None:
        async with _mv(kind):
            await _save_orders()
            await MvStats.define_view()
            stats = await MvStats.objects().get(["paid"])
            assert type(stats) is MvStats
            assert _stat(stats) == (_PAID, "paid", 2, 15.5)
            assert _stat(await MvStats.objects().get(("new",))) == (_NEW, "new", 1, 1.0)
            with pytest.raises(SurrealDbNotFoundError):
                await MvStats.objects().get(["gone"])

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_filter_by_array_id(self, kind: str) -> None:
        async with _mv(kind):
            await _save_orders()
            await MvStats.define_view()
            assert _stat(await MvStats.objects().filter(id=["paid"]).first()) == (_PAID, "paid", 2, 15.5)
            assert [_stat(row) for row in await MvStats.objects().filter(id=("new",)).exec()] == [(_NEW, "new", 1, 1.0)]
            assert [_stat(row) for row in await MvStats.objects().filter(id__not_in=[["paid"]]).exec()] == [
                (_NEW, "new", 1, 1.0)
            ]
            assert await MvStats.objects().filter(id=["gone"]).exec() == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_filter_by_array_ids_in(self, kind: str) -> None:
        """AC-13 as written: ``filter(id__in=[["paid"], ["new"]])`` gives both groups on every line — sent as
        ``$_f0 CONTAINS id`` (ruling A1) —, counted the same, and ``~Q(id__in=…)`` gives the other group."""
        async with _mv(kind):
            await _save_orders()
            await MvStats.define_view()
            both = await MvStats.objects().filter(id__in=[["paid"], ["new"]]).order_by("status").exec()
            assert [_stat(row) for row in both] == [(_NEW, "new", 1, 1.0), (_PAID, "paid", 2, 15.5)]
            assert await MvStats.objects().filter(id__in=[["paid"], ["new"]]).count() == 2
            assert await MvStats.objects().filter(id__in=[["paid"], ["gone"]]).count() == 1
            assert [_stat(row) for row in await MvStats.objects().filter(~Q(id__in=[["paid"]])).exec()] == [
                (_NEW, "new", 1, 1.0)
            ]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_id_in_on_a_plain_view(self, kind: str) -> None:
        """A plain view keeps the source's string ids (M4): ``id__in=["o1"]`` gives that record on every line."""
        async with _mv(kind):
            await _save_orders()
            await MvPaid.define_view()
            paid = await MvPaid.objects().filter(id__in=["o1"]).exec()
            assert [(row.id, row.customer) for row in paid] == [(RecordID("mv_paid_orders", "o1"), "ann")]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_id_in_a_subquery_over_the_view(self, kind: str) -> None:
        async with _mv(kind):
            await _save_orders()
            await MvStats.define_view()
            busy = Subquery(MvStats.objects().filter(total__gte=2).select("id"))
            assert [_stat(row) for row in await MvStats.objects().filter(id__in=busy).exec()] == [(_PAID, "paid", 2, 15.5)]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_raw_id_in_on_a_view_per_line(self, kind: str) -> None:
        """The raw ``id IN`` the ORM avoids on a view table: no record on SurrealDB 3.x (3.1 and 3.3 measured), both
        on 2.x and the embedded engine. Fails on purpose when a 3.x release answers it again."""
        async with _mv(kind) as client:
            await _save_orders()
            await MvStats.define_view()
            answer = await client.query("SELECT VALUE id FROM mv_order_stats WHERE id IN $ids", {"ids": [_PAID, _NEW]})
            if await _major(client) >= 3:
                if answer != []:
                    pytest.fail(_ID_IN_FIXED)
            else:
                assert sorted(answer, key=str) == [_NEW, _PAID]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_refresh_after_a_source_write(self, kind: str) -> None:
        async with _mv(kind):
            await _save_orders()
            await MvStats.define_view()
            stats = await MvStats.objects().get(["paid"])
            await MvOrder(id="o4", status="paid", amount=4.5).save()
            await stats.refresh()
            assert _stat(stats) == (_PAID, "paid", 3, 20.0)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_id_declared_str_reads_the_array_as_text(self, kind: str) -> None:
        """M4 pin (documented): declared ``id: str | None``, a grouped view's id reads as the text of its array —
        ``"['paid']"`` —, which addresses no record; ``id: RecordID | None`` is the declaration of a grouped view."""
        async with _mv(kind):
            await _save_orders()
            await MvStats.define_view()
            rows = await MvStatsStr.objects().order_by("status").exec()
            assert [(row.id, row.status, row.total, row.revenue) for row in rows] == [
                ("['new']", "new", 1, 1.0),
                ("['paid']", "paid", 2, 15.5),
            ]
            with pytest.raises(SurrealDbError, match=r"^Can't refresh data, no record found\.$"):
                await rows[1].refresh()

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_count_and_sum(self, kind: str) -> None:
        async with _mv(kind):
            await _save_orders()
            await MvStats.define_view()
            assert await MvStats.objects().count() == 2
            assert await MvStats.objects().filter(total__gte=2).count() == 1
            assert await MvStats.objects().sum("revenue") == 16.5

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_subquery_over_the_view(self, kind: str) -> None:
        async with _mv(kind):
            await _save_orders()
            await MvStats.define_view()
            orders = await MvOrder.objects().filter(status__in=_busy()).order_by("id").exec()
            assert [order.id for order in orders] == ["o1", "o2"]
            await MvOrder(id="o4", status="new", amount=2.0).save()
            orders = await MvOrder.objects().filter(status__in=_busy()).order_by("id").exec()
            assert [order.id for order in orders] == ["o1", "o2", "o3", "o4"]


async def _take(stream: Any, count: int) -> list[Any]:
    """The next *count* items of *stream* — the envelopes of ``watch()``, the events of ``live()`` —, read within the
    5 s deadline of spec §9.3."""

    async def read() -> list[Any]:
        seen: list[Any] = []
        async for item in stream:
            seen.append(item)
            if len(seen) == count:
                break
        return seen

    return await asyncio.wait_for(read(), timeout=5)


async def _churn_groups() -> None:
    """M12's source writes, after the stream started: a second ``new`` order (its group updated), a ``gone`` order
    (a group created), then its delete (that group emptied)."""
    await asyncio.sleep(0.3)
    await MvOrder(id="o4", status="new", amount=2.0).save()
    await MvOrder(id="o5", status="gone", amount=3.0).save()
    await (await MvOrder.objects().get("o5")).delete()


def _notified(major: int) -> list[tuple[str, RecordID, tuple[str, int, float]]]:
    """What the view notifies for :func:`_churn_groups` on *major*, in order — action, record, ``(status, total,
    revenue)``: the emptied group is one ``DELETE`` (its last state) on 3.x, an ``UPDATE`` to a count of 0 then a
    ``DELETE`` on 2.x (M12)."""
    emptied = (
        [("DELETE", _GONE, ("gone", 1, 3.0))]
        if major >= 3
        else [("UPDATE", _GONE, ("gone", 0, 0.0)), ("DELETE", _GONE, ("gone", 0, 0.0))]
    )
    return [("UPDATE", _NEW, ("new", 2, 3.0)), ("CREATE", _GONE, ("gone", 1, 3.0)), *emptied]


def _payload_ids(major: int) -> list[RecordID | None]:
    """The ``id`` each notification's record holds on *major* (measured on 3.3.4, 3.1.5 and 2.7.0): none on 3.x — the
    envelope's ``record`` names the view record —, the record's own on 2.x. ``watch()`` yields it as received."""
    return [None, None, None] if major >= 3 else [_NEW, _GONE, _GONE, _GONE]


class TestViewLiveE2E:
    """AC-14 (E2E, both lines, WebSocket only: the embedded engine has no live queries): ``watch()`` on a view model
    notifies the records SurrealDB recomputes when the source changes — a group emptied by a delete is one ``DELETE``
    on 3.x, an ``UPDATE`` with a count of 0 then a ``DELETE`` on 2.x (M12) —, its envelopes as received (no ``id`` in
    a 3.x ``result``), and ``live()`` yields ``MvStats`` instances carrying the record id on both lines."""

    async def test_watch_notifies_the_view_changes_per_line(self) -> None:
        async with _mv("ws") as client:
            await _save_orders()
            await MvStats.define_view()
            major = await _major(client)
            async with MvStats.objects().watch() as stream:
                await _churn_groups()
                envelopes = await _take(stream, len(_notified(major)))
        results = [envelope["result"] for envelope in envelopes]
        seen = [
            (envelope["action"], envelope["record"], (result["status"], result["total"], result["revenue"]))
            for envelope, result in zip(envelopes, results, strict=True)
        ]
        assert seen == _notified(major)
        assert [result.get("id") for result in results] == _payload_ids(major)

    async def test_live_yields_typed_instances(self) -> None:
        async with _mv("ws") as client:
            await _save_orders()
            await MvStats.define_view()
            major = await _major(client)
            async with MvStats.objects().live() as stream:
                await _churn_groups()
                events = await _take(stream, len(_notified(major)))
        assert [(type(event.instance), event.validation_error) for event in events] == [(MvStats, None)] * len(events)
        seen = [
            (event.action, event.record_id, (event.instance.status, event.instance.total, event.instance.revenue))
            for event in events
        ]
        assert seen == [(LiveAction(action), str(record), row) for action, record, row in _notified(major)]
        assert [event.instance.id for event in events] == [record for _, record, _ in _notified(major)]


#: A cached read as ``QuerySet._cached_read`` is called with it: ``(kind, tables, any_table)``.
_CachedRead = tuple[str, frozenset[str], bool]


@pytest.fixture
def cached_reads(monkeypatch: pytest.MonkeyPatch) -> list[_CachedRead]:
    """Spy on ``QuerySet._cached_read`` (AC-15, unit): record each cached read's ``(kind, tables, any_table)`` and
    answer it without a client — a record read by id gets a row of ``MvStats``' ``paid`` group, any other read no row."""
    reads: list[_CachedRead] = []

    async def spy(
        self: QuerySet,
        kind: str,
        statement: str,
        variables: dict[str, Any],
        tables: frozenset[str],
        any_table: bool,
        run: Callable[[], Awaitable[Any]],
    ) -> Any:
        reads.append((kind, tables, any_table))
        target = variables.get("target")
        if kind == "select" and isinstance(target, RecordID):
            return {"id": target, "status": "paid", "total": 2, "revenue": 15.5}
        return []

    monkeypatch.setattr(QuerySet, "_cached_read", spy)
    return reads


class TestViewCacheTags:
    """AC-15 (unit): the two call sites of ``_cached_read()`` — ``_run_cached()`` (``exec()``) and ``_cached_select()``
    (``all()``, ``get(<id>)``) — tag a read of a view model "any table", and leave an ordinary model's read as it was;
    a ``Subquery`` over a view model fills ``SubqueryCompiler.view_tables``, which tags the outer read."""

    async def test_exec_of_a_view_model_reads_any_table(self, cached_reads: list[_CachedRead]) -> None:
        assert await MvStats.objects().cache(60).exec() == []
        assert cached_reads == [("query", frozenset({"mv_order_stats"}), True)]

    async def test_all_of_a_view_model_reads_any_table(self, cached_reads: list[_CachedRead]) -> None:
        assert await MvStats.objects().cache(60).all() == []
        assert cached_reads == [("select", frozenset({"mv_order_stats"}), True)]

    async def test_get_by_array_id_of_a_view_model_reads_any_table(self, cached_reads: list[_CachedRead]) -> None:
        assert _stat(await MvStats.objects().cache(60).get(["paid"])) == (_PAID, "paid", 2, 15.5)
        assert cached_reads == [("select", frozenset({"mv_order_stats"}), True)]

    async def test_an_ordinary_model_is_unchanged(self, cached_reads: list[_CachedRead]) -> None:
        assert await MvOrder.objects().cache(60).exec() == []
        assert await MvOrder.objects().cache(60).all() == []
        order = await MvOrder.objects().cache(60).get("o1")
        assert (type(order), order.status) == (MvOrder, "paid")
        assert await MvOrder.objects().cache(60).filter(status__in=Subquery(MvOrder.objects().select("status"))).exec() == []
        assert cached_reads == [
            ("query", frozenset({"mv_orders"}), False),
            ("select", frozenset({"mv_orders"}), False),
            ("select", frozenset({"mv_orders"}), False),
            ("query", frozenset({"mv_orders"}), False),
        ]

    async def test_a_subquery_over_a_view_model_reads_any_table(self, cached_reads: list[_CachedRead]) -> None:
        assert await MvOrder.objects().cache(60).filter(status__in=_busy()).exec() == []
        assert cached_reads == [("query", frozenset({"mv_orders", "mv_order_stats"}), True)]

    def test_a_subquery_over_a_view_model_fills_view_tables(self) -> None:
        compiler = SubqueryCompiler()
        MvOrder.objects().filter(status__in=_busy())._compile_query(subqueries=compiler)
        assert (compiler.tables, compiler.view_tables) == ({"mv_order_stats"}, {"mv_order_stats"})

    def test_a_subquery_over_an_ordinary_model_leaves_view_tables_empty(self) -> None:
        compiler = SubqueryCompiler()
        MvStats.objects().filter(status__in=Subquery(MvOrder.objects().select("status")))._compile_query(subqueries=compiler)
        assert (compiler.tables, compiler.view_tables) == ({"mv_orders"}, set())
        assert SubqueryCompiler().view_tables == set()


class MvWatch(BaseSurrealModel):
    """A status someone watches (AC-15): an ordinary table outside the view's ``FROM``, read through a ``Subquery`` over
    ``MvStats`` — only the view tag drops such a cached read on a write to ``mv_orders``."""

    model_config = SurrealConfigDict(table_name="mv_watches", connection="mv")

    id: str | None = None
    status: str


@pytest.fixture
def empty_cache() -> Iterator[None]:
    """An empty query cache around the test (AC-15, E2E)."""
    QueryCache.clear()
    yield
    QueryCache.clear()


def _hits() -> int:
    """The query cache's hits so far."""
    return int(QueryCache.stats()["hits"])


def _cached_stats() -> QuerySet:
    """``MvStats``' groups by status, with ``cache(60)``."""
    return MvStats.objects().cache(60).order_by("status")


def _by_status(rows: list[MvStats]) -> list[tuple[RecordID | None, str, int, float]]:
    """*rows* as :func:`_stat` reads them, by status (``all()`` keeps the server's order)."""
    return sorted((_stat(row) for row in rows), key=lambda stat: stat[1])


#: ``MvStats``' groups after :func:`_save_orders`, as :func:`_stat` reads them.
_STATS_BEFORE = [(_NEW, "new", 1, 1.0), (_PAID, "paid", 2, 15.5)]


@pytest.mark.usefixtures("empty_cache")
class TestViewCacheE2E:
    """AC-15 (E2E, both lines, WebSocket, HTTP and ``mem://``): a cached read of a view model — ``exec()``,
    ``get(["paid"])``, ``all()`` — and a cached read filtered by a ``Subquery`` over one are read again after an ORM
    write to the view's source table, with no ``QueryCache.invalidate()``; ``define_view()`` and ``remove_view()``
    drop the cached read too. Each read is first answered by the cache (one more hit), so a stale answer would show."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_exec_after_a_source_write(self, kind: str) -> None:
        async with _mv(kind):
            await _save_orders()
            await MvStats.define_view()
            assert [_stat(row) for row in await _cached_stats().exec()] == _STATS_BEFORE
            hits = _hits()
            assert [_stat(row) for row in await _cached_stats().exec()] == _STATS_BEFORE
            assert _hits() == hits + 1
            await MvOrder(id="o4", status="paid", amount=4.5).save()
            assert [_stat(row) for row in await _cached_stats().exec()] == [(_NEW, "new", 1, 1.0), (_PAID, "paid", 3, 20.0)]
            assert _hits() == hits + 1

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_get_by_array_id_and_all_after_a_source_write(self, kind: str) -> None:
        async with _mv(kind):
            await _save_orders()
            await MvStats.define_view()
            assert _stat(await MvStats.objects().cache(60).get(["paid"])) == (_PAID, "paid", 2, 15.5)
            assert _by_status(await MvStats.objects().cache(60).all()) == _STATS_BEFORE
            hits = _hits()
            assert _stat(await MvStats.objects().cache(60).get(["paid"])) == (_PAID, "paid", 2, 15.5)
            assert _by_status(await MvStats.objects().cache(60).all()) == _STATS_BEFORE
            assert _hits() == hits + 2
            await MvOrder(id="o4", status="paid", amount=4.5).save()
            assert _stat(await MvStats.objects().cache(60).get(["paid"])) == (_PAID, "paid", 3, 20.0)
            assert _by_status(await MvStats.objects().cache(60).all()) == [
                (_NEW, "new", 1, 1.0),
                (_PAID, "paid", 3, 20.0),
            ]
            assert _hits() == hits + 2

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_subquery_over_the_view_after_a_source_write(self, kind: str) -> None:
        """The ``MvOrder`` read of AC-15, and an ``MvWatch`` read: its own table is not the view's source, so only the
        view tag (``SubqueryCompiler.view_tables``) drops it when an order is saved."""
        async with _mv(kind):
            await _save_orders()
            await MvStats.define_view()
            await MvWatch(id="w1", status="paid").save()
            await MvWatch(id="w2", status="new").save()

            async def busy() -> tuple[list[str | None], list[str | None]]:
                orders = await MvOrder.objects().cache(60).filter(status__in=_busy()).order_by("id").exec()
                watches = await MvWatch.objects().cache(60).filter(status__in=_busy()).order_by("id").exec()
                return [order.id for order in orders], [watch.id for watch in watches]

            assert await busy() == (["o1", "o2"], ["w1"])
            hits = _hits()
            assert await busy() == (["o1", "o2"], ["w1"])
            assert _hits() == hits + 2
            await MvOrder(id="o4", status="new", amount=2.0).save()
            assert await busy() == (["o1", "o2", "o3", "o4"], ["w1", "w2"])
            assert _hits() == hits + 2

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_define_view_and_remove_view_drop_the_cached_read(self, kind: str) -> None:
        async with _mv(kind):
            await _save_orders()
            assert await _cached_stats().exec() == []
            hits = _hits()
            assert await _cached_stats().exec() == []
            assert _hits() == hits + 1
            await MvStats.define_view()
            assert [_stat(row) for row in await _cached_stats().exec()] == _STATS_BEFORE
            assert [_stat(row) for row in await _cached_stats().exec()] == _STATS_BEFORE
            assert _hits() == hits + 2
            await MvStats.remove_view()
            assert await _cached_stats().exec() == []
            assert _hits() == hits + 2


# --- the round trip: introspection and the generator (spec §6.7, D12; AC-16; M18) ------------------------------------

#: Spec §3's renderings of the three goldens over ``mv_orders``: the ``view_query`` ``INFO FOR DB`` reads back, the
#: same on every engine (M18) — and the same text as the models declare.
_MV_ROUND_TRIP = {
    "mv_order_stats": _MV_STATS_QUERY,
    "mv_order_totals": "SELECT count() AS orders, math::sum(amount) AS revenue FROM mv_orders GROUP ALL",
    "mv_paid_orders": "SELECT customer, amount FROM mv_orders WHERE status = 'paid'",
}

#: The generated class of each view table.
_MV_GENERATED = {"mv_order_stats": "MvOrderStats", "mv_order_totals": "MvOrderTotals", "mv_paid_orders": "MvPaidOrders"}


def _generated_view(table: str) -> str:
    """The class ``generate_models_from_db()`` writes for the view *table* of :data:`_MV_ROUND_TRIP` (spec §6.7)."""
    return (
        f"class {_MV_GENERATED[table]}(BaseSurrealModel):\n"
        "    # Read-only materialized view\n"
        '    # SCHEMALESS: fields not declared here are kept (extra="allow")\n'
        "    model_config = SurrealConfigDict(\n"
        f'        table_name="{table}",\n'
        f'        view_query="{_MV_ROUND_TRIP[table]}",\n'
        '        connection="mv",\n'
        '        extra="allow",\n'
        "    )\n"
        "\n"
        "    id: RecordID | None = None\n"
    )


def _import_generated(path: Path) -> ModuleType:
    """Import the module written at *path* (removed from ``sys.modules`` afterwards)."""
    name = f"generated_views_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[name]
    return module


async def _view_queries() -> dict[str, str | None]:
    """The ``view_query`` of each table of :data:`_MV_ROUND_TRIP`, read by ``DatabaseIntrospector`` with no issue."""
    schema = await DatabaseIntrospector("mv", tables=sorted(_MV_ROUND_TRIP)).introspect()
    assert schema.issues == ()
    return {table: schema.tables[table].view_query for table in _MV_ROUND_TRIP}


class TestViewRoundTripE2E:
    """AC-16 (E2E, both lines, WebSocket, HTTP and ``mem://``): after ``define_view()`` of the §5.3 views,
    ``DatabaseIntrospector`` reads each ``view_query`` as spec §3 renders it; ``generate_models_from_db()`` writes view
    models (``view_query="…"``, ``# Read-only materialized view``, ``id: RecordID | None = None``) whose module imports;
    each generated class's ``view_ddl()``, applied to a fresh ``mv_db``, reads back the same ``view_query``, and the
    generated class reads the view's records."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_introspect_generate_and_define_again(self, kind: str, tmp_path: Path) -> None:
        async with _mv(kind) as client:
            await _save_orders()
            for model in (MvStats, MvPaid, MvTotals):
                await model.define_view()
            assert await _view_queries() == _MV_ROUND_TRIP
            path = tmp_path / "mv_models.py"
            source = await generate_models_from_db(path, "mv", tables=sorted(_MV_ROUND_TRIP))
            assert path.read_text(encoding="utf-8") == source
            for table in _MV_ROUND_TRIP:
                assert _generated_view(table) in source
            assert "\nfrom surrealdb import RecordID\n" in source
            assert "View: AS" not in source
            module = _import_generated(path)
            generated = {table: getattr(module, name) for table, name in _MV_GENERATED.items()}
            for table, model in generated.items():
                assert model._is_view()
                assert model.get_table_name() == table
                assert model.get_view_query() == _MV_ROUND_TRIP[table]
            assert generated["mv_order_stats"].view_ddl() == MvStats.view_ddl()

            await _apply_ddl(client, _MV_RESET)
            await _save_orders()
            for model in generated.values():
                assert await model.define_view() == model.view_ddl()
            assert await _view_queries() == _MV_ROUND_TRIP
            rows = await generated["mv_order_stats"].objects().order_by("status").exec()
            assert [(row.id, row.status, row.total, row.revenue) for row in rows] == [
                (_NEW, "new", 1, 1.0),
                (_PAID, "paid", 2, 15.5),
            ]


# --- README: section 33, section 31 and the tables (spec §11.1; AC-18) -----------------------------------------------

_SECTION_31 = "### 31. Schema introspection (`DatabaseIntrospector`, `generate_models_from_db()`)"
_SECTION_32 = "### 32. Server-side events and stored functions (`Event`, `define_events()`, `define_function()`)"
_SECTION_33 = "### 33. Materialized views (`view_query`, `define_view()`)"
#: Spec §11.1 item 1: section 33's subsections, in order, after an untitled intro paragraph that holds example A.
_SUBSECTIONS_33 = [
    "Declaring a view",
    "Applying and removing",
    "Reading",
    "Writes are refused",
    "Transactions, connections and the query cache",
    "SurrealDB 2.x and 3.x",
    "Security",
    "Coming from the full SurrealDB-ORM",
]
#: Spec §11.1 item 1, example A, exactly: the intro's block.
_README_EXAMPLE_A = """\
from surrealdb import RecordID

from surreal_orm_lite import BaseSurrealModel, SurrealConfigDict


class Order(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="orders")

    id: str | None = None
    status: str
    amount: float
    customer: str | None = None


class OrderStats(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_name="order_stats",
        view_query="SELECT status, count() AS total, math::sum(amount) AS revenue FROM orders GROUP BY status",
    )

    id: RecordID | None = None
    status: str
    total: int
    revenue: float


print(OrderStats.view_ddl())
# DEFINE TABLE OVERWRITE `order_stats` AS (SELECT status, count() AS total, math::sum(amount) AS revenue FROM orders GROUP BY status);

await Order(id="o1", status="paid", amount=10.0, customer="ada").save()  # the source table exists first
await OrderStats.define_view()  # idempotent (OVERWRITE): run it at start-up

await Order(id="o2", status="paid", amount=5.5, customer="bob").save()
await Order(id="o3", status="new", amount=1.0, customer="ada").save()
for stats in await OrderStats.objects().order_by("status").exec():
    print(stats.status, stats.total, stats.revenue)
# new 1 1.0
# paid 2 15.5

paid = await OrderStats.objects().get(["paid"])
print(paid.id, paid.total)
# order_stats:['paid'] 2

try:
    await paid.merge(total=0)
except TypeError as error:
    print(error)
# Cannot modify materialized view 'order_stats': OrderStats.merge() writes to it, and SurrealDB computes a view's records from its view_query. Write to the tables it selects from instead.
"""
#: Spec §11.1 item 1, example B, exactly: it opens "Declaring a view".
_README_EXAMPLE_B = """\
class PaidOrder(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_name="paid_orders", view_query="SELECT customer, amount FROM orders WHERE status = 'paid'"
    )

    id: RecordID | None = None
    customer: str | None = None
    amount: float


await PaidOrder.define_view()
print([(str(order.id), order.amount) for order in await PaidOrder.objects().order_by("amount").exec()])
# [('paid_orders:o2', 5.5), ('paid_orders:o1', 10.0)]
"""
_README_EXAMPLES_33 = [_README_EXAMPLE_A, _README_EXAMPLE_B]
#: Spec §8 and §11.1 item 1 (D18, binding, adapted to views): the sentence "Security" carries, whitespace normalised.
_D18_SENTENCE_33 = (
    "The check catches mistakes — unbalanced brackets, a comment that would swallow the parenthesis the ORM appends, a "
    "second statement where one SELECT is expected — and is not an injection defence: a view's query is trusted "
    "application code, never user input."
)
#: Spec §11.1 item 1: what the intro and each subsection say (key phrases, whitespace normalised).
_INTRO_NEEDLES_33 = (
    "Since v0.32.0",
    "materialized view",
    "`SELECT`",
    "keeps up to date",
    "counts, sums, filtered copies",
    "`view_query`",
    "idempotently",
    "every write of the model is refused",
    "SurrealDB 2.x, 3.x and the embedded engine",
    "(#orm-behaviour-surrealdb-2x-vs-3x)",
    "`mem://`",
)
_PART_NEEDLES_33 = {
    "Declaring a view": (
        "`view_query` is one `SELECT … FROM <table> [WHERE …] [GROUP BY … | GROUP ALL]`",
        "a final `;` is dropped",
        "checked when the class is defined",
        "`ORDER BY`",
        "`LIMIT`",
        "`FETCH`",
        "`SELECT VALUE`",
        "**A grouped view**",
        "`count()`, `math::sum()`, `math::mean()`, `math::min()`, `math::max()`, `time::min()`, `time::max()`",
        "`math::stddev()`, `math::variance()` on 3.x",
        "whatever their argument (`math::sum(amount ?? 0)`)",
        "`math::median()`, `array::group()`, `array::distinct()`, `array::join()`",
        "`vector::sum()`",
        "inside an aggregate's argument they are allowed",
        "`math::sum(amount) / count()`",
        "a `GROUP BY` on an alias",
        "on 2.x and the embedded engine `define_view()` refuses them",
        "Every source write runs the view's query for that record",
        "surrealdb#7447",
        "on every line",
        "a plain view keeps the source records' ids",
        "`order_stats:['paid']`",
        "`GROUP ALL` gives `[]`",
        "`id: RecordID | None = None`",
        '`get(["paid"])`',
        '`filter(id=["paid"])`',
        "`first()`",
        "Declare sums and means `float`",
        "`BaseSurrealEdge`",
        "`AuthenticatedUserMixin`",
        "computed fields",
        "`select`",
        "`get_view_query()`",
    ),
    "Applying and removing": (
        "`DEFINE TABLE OVERWRITE … AS (…)`",
        "SurrealDB fills the view from the source's records",
        "each call recomputes the view and drops the events, fields and indexes defined on it",
        "one pass over the source",
        "`overwrite=False` writes `IF NOT EXISTS` and keeps a view already defined, whatever its query",
        "The source table must exist",
        "`SurrealDbError` naming it",
        "run `define_events()` after `define_view()`",
        "`REMOVE TABLE IF EXISTS`",
        "`missing_ok=False`",
        "`SurrealDbNotFoundError`",
        "**without records**",
        "`watch()`",
        "turns it into the view",
        "SurrealDB would delete them",
        '`raw_query("REMOVE TABLE …")`',
        "which `IF NOT EXISTS` would silently keep",
        "`remove_view()` raises on a table that is not a view",
        "`view_ddl()` returns the statement without applying it",
        "not recomputed when the inner one is redefined",
        "Remove the views that read another view first",
        "`Cannot delete table … on which a view is defined`",
        "no longer updates",
        "it is not a view",
        "plain `FROM` targets",
    ),
    "Reading": (
        "`order_by()`",
        "`get()`",
        "`refresh()`",
        "`Subquery`",
        "`fetch()`",
        "`using()`",
        "`live()`/`watch()`",
        "`relate()` and `remove_relation()`",
        "write the edge table and are allowed",
        "On a view model, `filter(id__in=...)` is sent as `$ids CONTAINS id`: SurrealDB 3.x (3.1 and 3.3 measured) "
        "answers no record to `id IN` on a view table, while `CONTAINS` is right on every line; write raw SurrealQL "
        "over a view the same way.",
        "`live()` instances carry the record id on both lines; `watch()` yields the server's envelopes as received: on "
        "3.x their `result` has no `id`, read `record`.",
        "On SurrealDB 3.x a live query on a view filtered on `id` never notifies (the server's view notifications carry "
        'no id); filter on the view\'s fields, e.g. `filter(status="paid")`.',
    ),
    "Writes are refused": (
        "`TypeError`",
        "before any signal, transaction or request",
        "`save()`, `upsert()`, `update()`, `merge()`, `patch()`, `delete()`",
        "`atomic_append()`, `atomic_remove()`, `atomic_set_add()`, `atomic_increment()`, `atomic_append_many()`, "
        "`atomic_set_add_many()`, `atomic_remove_many()`",
        "`bulk_create()`, `bulk_update()`, `bulk_delete()`, `patch()`, `update_or_create()`, `get_or_create()`",
        "even when the record exists",
        "`delete_table()`",
        "`Cannot modify materialized view '<table>'`",
        "Raw SurrealQL",
        "is not checked",
        "SurrealDB 3.2 and later refuse",
        "2.x and 3.1 accept",
        "change the view's records",
    ),
    "Transactions, connections and the query cache": (
        "`tx=`",
        "buffered",
        "interactive",
        "`objects(tx=tx)`",
        "a block that raises leaves no view",
        "before anything is queued",
        "the transaction's own error",
        "`connection`",
        "`using()`",
        "another connection raises `SurrealDbError` before any request",
        "(#29-query-cache-cache-querycache)",
        "dropped by every ORM write",
        "a write to a source table changes the view",
        "system user",
        "`VIEWER`",
        "`SurrealDbAuthenticationError`",
        "signal",
    ),
    "SurrealDB 2.x and 3.x": (
        "Raw writes",
        "3.2 and later",
        "keep up to date only whole aggregates and `GROUP BY` fields",
        "once per source record",
        "`define_view()` raises `SurrealDbError` before any DDL",
        "`math::stddev()`",
        "`math::variance()`",
        "`math::sum(amount) / count()`",
        "a field outside `GROUP BY` and outside an aggregate (3.x refuses that one too)",
        "a `GROUP BY` on an alias",
        "every record in one group",
        "`int` sums and `Decimal` means on 2.x, `float` on 3.x",
        "`count()` of an array field adds the arrays' lengths on 2.x",
        "an emptied group",
        "3.x refuses it",
        "a raw `REMOVE TABLE`",
        "keeping their last records",
        "surrealdb#7447",
        "`math::sum(amount ?? 0)`",
        "(#orm-behaviour-surrealdb-2x-vs-3x)",
    ),
    "Security": (
        "identifier",
        "quoted",
        "`DEFINE PARAM`",
        "names the view's table, never the statement",
        "check-then-act on committed state",
        "not an access control",
        "`PERMISSIONS NONE`",
        "a record user reads nothing from a view",
        "resets permissions set by hand",
        "privileged system user",
    ),
}
#: Spec §11.1 item 1: one key phrase per "Coming from the full SurrealDB-ORM" bullet, in order.
_COMING_FROM_PHRASES_33 = (
    "**`view_query` is the same key**",
    "**`TypeError` with the same first words**",
    "**`define_view()`/`remove_view()`/`view_ddl()`**",
    "**`AS (…)` as in the full ORM, the table name quoted**",
    "**generated models write `view_query=`**",
    "**the documented `MaterializedView` base class with `source_query` is not shipped by the full ORM either**",
)
#: Spec §11.1 items 2–4: the features row, the roadmap rows and the comparison row.
_FEATURES_ROW_33 = ["Materialized views (`view_query`, `define_view()`, read-only)", "✅"]
_ROADMAP_ROWS_33 = [
    ["v0.31.0", "Tier 3 — Server-side events and DDL helpers", "✅ Released"],
    ["v0.32.0", "Tier 3 — Materialized views", "✅ Released"],
    ["v0.33.0 – v0.39.0", "Tier 3 — Advanced (search, DDL, migrations, CLI)", "📋 Planned"],
    ["v0.40.0", "Beta Phase (API freeze, hardening)", "📋 Planned"],
]
_COMPARISON_ROW_33 = ["Materialized views", "✅ v0.32.0 (`view_query`, `define_view()`)", "✅"]
#: Spec §11.1 item 5 (P4, A3): exactly eight rows v0.32.0 adds to the 2.x/3.x table, cell by cell.
_VIEW_ROWS = [
    [
        "Writes to a materialized view (`save()`, `merge()`, `delete()`, bulk writes … of a `view_query` model)",
        "`TypeError` before any request; a raw write is accepted by the server and changes the view's records",
        "`TypeError` before any request; the server refuses raw writes too (3.2 and later; 3.1 accepts them)",
        "v0.32.0",
    ],
    [
        "Aggregates in a grouped view (`view_query` with `GROUP BY` or `GROUP ALL`)",
        "`count()`, `math::sum()`, `math::mean()`, `math::min()`, `math::max()`, `time::min()`, `time::max()`; "
        "`math::stddev()`, `math::variance()`: `SurrealDbError` before any DDL",
        "the same, plus `math::stddev()` and `math::variance()`",
        "v0.32.0",
    ],
    [
        "An expression around an aggregate in a grouped view (`math::sum(amount) / count()`, "
        "`math::round(math::mean(amount))`, `count() * 2`)",
        "`SurrealDbError` before any DDL: the server would compute it once per source record (a wrong `count()`, or "
        "every source write refused)",
        "kept up to date",
        "v0.32.0",
    ],
    [
        "A grouped view selecting a field outside `GROUP BY` and outside an aggregate (`amount`, "
        "`math::percentile(amount, 50)`, `*`)",
        "`SurrealDbError` before any DDL: the server would keep the last source record's value, or refuse every source write",
        "`SurrealDbError` when the view is defined (the server refuses it)",
        "v0.32.0",
    ],
    [
        "A grouped view whose `GROUP BY` names the alias of a computed field (`time::month(at) AS month` … `GROUP BY month`)",
        "`SurrealDbError` before any DDL: the server would put every source record in one group",
        "one group per computed value",
        "v0.32.0",
    ],
    [
        "Values a view computes (`math::sum()`, `math::mean()`, `count()` of an array)",
        "a sum of integers is an `int`, a mean a `Decimal`; `count(tags)` of an array field adds the arrays' lengths",
        "both are `float`; `count(tags)` counts the records whose array is not empty",
        "v0.32.0",
    ],
    [
        "Live queries and events on a view (a group emptied by a delete; a live query filtered on `id`)",
        "an emptied group: an `UPDATE` with the emptied values (a `count()` of 0), then a `DELETE`; a filter on `id` notifies",
        "an emptied group: a `DELETE`; a filter on `id` never notifies (the view's notifications carry no `id`): "
        "filter on the view's fields",
        "v0.32.0",
    ],
    [
        "Removing a materialized view, or a source table, that another view reads (`remove_view()`, `REMOVE TABLE`)",
        # Issue #286 (v0.32.1): a chain removed outer view first in one buffered block is accepted.
        "`remove_view()` raises `SurrealDbError` before any DDL, unless its transaction removes those views first; a "
        "raw `REMOVE TABLE` makes them ordinary tables, last records kept",
        "`SurrealDbError` from the server (`Cannot delete table … on which a view is defined`): remove the views that "
        "read it first",
        "v0.32.0",
    ],
]
_HEADER_SENTENCE_32 = (
    "Rows added in v0.32.0 were measured on 2.7.0, 3.3.4 and the embedded engine; 3.1.5 behaves as 3.3.4 but accepts "
    "raw writes to a view."
)
#: Spec §11.1 item 6: the row v0.32.0 adds to the embedded-engine table, cell by cell.
_EMBEDDED_VIEWS_ROW = [
    "Materialized views (v0.32.0)",
    "as on a 2.x server: `int` sums and `Decimal` means; in a grouped view, `math::stddev()`/`math::variance()`, an "
    "expression around an aggregate, a field outside `GROUP BY` and a `GROUP BY` on an alias raise `SurrealDbError` "
    "before any DDL; writes to a view refused by the ORM (the engine would accept them); no live queries",
    "`float` sums and means on 3.x",
    "v0.32.0",
]
#: Spec §11.1 item 7: section 31's generated-models bullet, whitespace normalised.
_GENERATED_MODELS_BULLET_31 = (
    "- **Generated models use comments** where the full ORM writes `SurrealConfigDict(permissions=…, changefeed=…, "
    "relation_in=…, relation_out=…, enforced=…)` (`# Permissions: …`, `# Changefeed: …`, `# IN … OUT … ENFORCED`), "
    "and the base class where it writes `table_type=` (`BaseSurrealEdge` for a relation table): those keys arrive in "
    "the lite's `SurrealConfigDict` with later minors. A view is generated with `view_query=…` since v0.32.0, as in "
    "the full ORM (a `# View: AS …` comment when the lite cannot declare it: an edge, a computed field, a query it "
    "refuses). `READONLY` is a comment too (full ORM: `Field(readonly=True)`)."
)
#: Spec §11.1 item 7: section 31's long-line sentence, whitespace normalised.
_LONG_LINES_31 = (
    'Other long lines — long names, a long `Literal` union, a long view query (`view_query="…"`) — are left on one '
    "line for your formatter."
)
#: Spec §11.1 item 7: section 31's prose on generated classes says the same as the bullet.
_GENERATING_NEEDLES_31 = (
    "`view_query` for a view table",
    "`id: RecordID | None = None` for a view",
    "`# Read-only materialized view`",
    "`# View: AS …`",
)


def _section_33() -> str:
    return _readme_part(_SECTION_33)


def _part_33(heading: str) -> str:
    """One ``####`` subsection of section 33, as written."""
    return _subsection(f"#### {heading}", _section_33())


def _readme_blocks(text: str) -> list[str]:
    """The ``python`` blocks of *text*, in order."""
    blocks: list[str] = re.findall(r"^```python\n(.*?)^```$", text, re.MULTILINE | re.DOTALL)
    return blocks


def _readme_printed(block: str) -> list[str]:
    """What a README example prints: its comment lines that start a line (``# <printed line>``), in order."""
    return re.findall(r"^# (.*)$", block, re.MULTILINE)


def _readme_bullets(text: str) -> list[str]:
    """The ``- `` bullets of *text*, each with its continuation lines, whitespace normalised."""
    return [_normalised(bullet) for bullet in re.findall(r"^- .*?(?=^- |^\s*$|\Z)", text, re.MULTILINE | re.DOTALL)]


def _anchor(heading: str) -> str:
    """GitHub's anchor of a README heading: lower case, punctuation but ``-`` and ``_`` dropped, spaces as ``-``."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


class TestReadmeSection33:
    """AC-18: README section 33 — its place, subsections, examples (run on ``mem://`` as written, each printed line
    equal to its comment), the Security wording (D18), the removal prose (P1) and the D2/D6 wording —, section 31's
    generated-models bullet and long-line sentence, and the features, roadmap, comparison and both behaviour tables
    (spec §11.1)."""

    def test_section_33_follows_section_32(self) -> None:
        """Right after section 32 (after its "Coming from" bullets) and before the ``---`` of "Configuration Options"."""
        text = _readme()
        start_32 = text.index(f"\n{_SECTION_32}\n")
        start_33 = text.index(f"\n{_SECTION_33}\n")
        configuration = text.index("\n## Configuration Options\n")
        assert start_32 < start_33 < configuration
        assert re.findall(r"^#{2,3} .*$", text[start_32 + 1 : configuration], re.MULTILINE) == [
            _SECTION_32,
            _SECTION_33,
        ]
        assert text[:configuration].rstrip().endswith("\n---"), "section 33 ends before the --- of the next part"

    def test_subsections_in_order(self) -> None:
        assert re.findall(r"^#### (.*)$", _section_33(), re.MULTILINE) == _SUBSECTIONS_33

    def test_the_spec_examples_open_their_parts(self) -> None:
        """Example A exactly, the first block of the untitled intro; example B exactly, the first block of "Declaring a
        view"; no other block; ``python`` fences only."""
        section = _section_33()
        assert _readme_blocks(section) == _README_EXAMPLES_33
        intro = section.split("\n#### ", 1)[0]
        assert _readme_blocks(intro) == [_README_EXAMPLE_A]
        part = _part_33("Declaring a view")
        assert part.split("\n\n", 1)[1].startswith(f"```python\n{_README_EXAMPLE_B}```"), part
        fences = re.findall(r"^```(\S*)$", section, re.MULTILINE)
        assert fences == ["python", ""] * len(_README_EXAMPLES_33), fences

    def test_intro_paragraph(self) -> None:
        intro = _normalised(_section_33().split("\n```python\n", 1)[0])
        missing = [needle for needle in _INTRO_NEEDLES_33 if needle not in intro]
        assert not missing, missing

    @pytest.mark.parametrize("heading", list(_PART_NEEDLES_33))
    def test_what_each_subsection_says(self, heading: str) -> None:
        text = _normalised(_part_33(heading))
        missing = [needle for needle in _PART_NEEDLES_33[heading] if needle not in text]
        assert not missing, missing

    def test_security_wording(self) -> None:
        """D18: the binding sentence, and no stronger claim about the query check anywhere in the section."""
        assert _D18_SENTENCE_33 in _normalised(_part_33("Security"))
        section = _normalised(_section_33())
        for claim in ("cannot inject", "cannot close", "boundary", "sound"):
            assert claim not in section, claim

    def test_coming_from_the_full_orm(self) -> None:
        """One key phrase per bullet, in order; D2: ``define_table()`` of v0.37.0 accepts a view model."""
        bullets = _readme_bullets(_part_33("Coming from the full SurrealDB-ORM"))
        assert len(bullets) == len(_COMING_FROM_PHRASES_33), bullets
        for bullet, phrase in zip(bullets, _COMING_FROM_PHRASES_33, strict=True):
            assert phrase.lower() in bullet.lower(), (phrase, bullet)
        assert "v0.37.0" in bullets[2] and "`define_table()` accepts a view model" in bullets[2], bullets[2]
        assert "`CreateTable`/`AlterTable`" in bullets[2], bullets[2]
        assert "`get_view_query()`" in bullets[0] and "`_is_view()`" in bullets[0], bullets[0]
        assert "`save()`, `update()`, `merge()`, `delete()` only" in bullets[1], bullets[1]

    def test_links_resolve(self) -> None:
        """Every in-page link of section 33 names a heading of the README."""
        anchors = {_anchor(heading) for heading in re.findall(r"^#{2,4} (.*)$", _readme(), re.MULTILINE)}
        links = re.findall(r"\]\(#([^)]+)\)", _section_33())
        assert links, "section 33 links to the behaviour tables and to section 29"
        assert [link for link in links if link not in anchors] == []

    def test_issue_286_follow_ups(self) -> None:
        """Issue #286 (v0.32.1): section 33 says that V7 allows a listed function inside the argument of each of several
        aggregates (still refused beside one), and that a chain of views removed outer view first in one buffered
        block is accepted (and when it is not)."""
        declaring = _normalised(_part_33("Declaring a view"))
        transactions = _normalised(_part_33("Transactions, connections and the query cache"))
        needles = [
            (declaring, "Since v0.32.1 that holds when an expression combines several aggregates too"),
            (declaring, "(`math::sum(amount) + math::sum(array::len(array::distinct(tags)))`)"),
            (declaring, "beside an aggregate (`math::sum(amount) + array::len(array::distinct(tags))`) they are still"),
            (transactions, "A chain of views can be removed in one buffered block, outer view first (since v0.32.1)"),
            (transactions, "the same block has already queued"),
            (transactions, "unless the block names it again afterwards (a `define_view(tx=tx)` of it)"),
            (transactions, "A view removed in another transaction, or by a `REMOVE TABLE` you add to the block yourself, is"),
        ]
        assert [needle for text, needle in needles if needle not in text] == []

    def test_features_row(self) -> None:
        features = [row for _, row in _table_rows(_readme_part("## Features"))]
        firsts = [row[0] for row in features]
        assert _FEATURES_ROW_33 in features, features
        events = "Server-side events & function DDL (`Event`, `define_function()`)"
        assert firsts.index(_FEATURES_ROW_33[0]) == firsts.index(events) + 1

    def test_roadmap_rows(self) -> None:
        roadmap = [row for _, row in _table_rows(_readme_part("## Roadmap"))]
        start = [row[0] for row in roadmap].index("v0.31.0")
        assert roadmap[start : start + 4] == _ROADMAP_ROWS_33, roadmap

    def test_comparison_row(self) -> None:
        """The materialized views row filled in, after the ``define_function()`` row and before full-text search."""
        comparison = [row for _, row in _table_rows(_readme_part("## SurrealDB-ORM-lite vs SurrealDB-ORM"))]
        firsts = [row[0] for row in comparison]
        start = firsts.index("`define_function()` (DDL)")
        assert comparison[start + 1] == _COMPARISON_ROW_33, comparison
        assert firsts[start + 2] == "Full-Text Search", firsts

    def test_server_behaviour_rows(self) -> None:
        """Exactly eight rows, Since v0.32.0, together and in version order: earlier releases above them, later ones
        below them, then the table's last row (v0.19.1's re-run)."""
        assert_server_rows(_VIEW_ROWS)

    def test_server_behaviour_table_keeps_its_widths(self) -> None:
        """Every line 535 characters, the columns 136, 175, 181 and 38 wide: the new rows are padded to them."""
        assert_server_table()

    def test_server_behaviour_header_sentence(self) -> None:
        """The sentence v0.32.0 adds to the table's intro paragraph, once, among sentences naming their releases in
        increasing version order."""
        assert_server_header_sentence(_HEADER_SENTENCE_32)

    def test_embedded_table_row(self) -> None:
        """All four cells, in version order before the catch-all "Everything else" row; widths kept (69, 343, 113,
        9)."""
        assert_embedded_rows([_EMBEDDED_VIEWS_ROW])

    def test_tables_have_no_raw_pipe_in_a_cell(self) -> None:
        """Every table v0.32.0 touches keeps one cell count per line and no escaped pipe; section 33 has no table."""
        parts = [
            _readme_part("## Features"),
            _readme_part(_SERVER_BEHAVIOUR),
            _readme_part(_EMBEDDED_BEHAVIOUR),
            _readme_part("## Roadmap"),
            _readme_part("## SurrealDB-ORM-lite vs SurrealDB-ORM"),
        ]
        found = [lines for part in parts for lines in _md_tables(part)]
        assert len(found) == len(parts)
        assert _md_tables(_section_33()) == [], "section 33 has no table"
        for lines in found:
            assert len({len(_cells(line)) for line in lines}) == 1 and "\\|" not in "".join(lines), lines

    def test_section_31_generated_models(self) -> None:
        """Spec §11.1 item 7: section 31's generated-models bullet, its long-line sentence and its prose on generated
        classes say that a view is generated with ``view_query=``."""
        section_31 = _readme_part(_SECTION_31)
        bullets = _readme_bullets(_subsection("#### Coming from the full SurrealDB-ORM", section_31))
        assert _GENERATED_MODELS_BULLET_31 in bullets, [b for b in bullets if "Generated models" in b]
        assert _LONG_LINES_31 in _normalised(_subsection("#### What the generator does not do", section_31))
        generating = _normalised(_subsection("#### Generating models", section_31))
        missing = [needle for needle in _GENERATING_NEEDLES_31 if needle not in generating]
        assert not missing, missing

    async def test_section_33_examples(self, capsys: pytest.CaptureFixture[str]) -> None:
        """AC-18: every ``python`` block runs as written, in order and in one namespace, on ``mem://`` (inside
        ``embedded_database()``); what each block prints is, line by line, its ``# `` comment lines."""
        capsys.readouterr()
        namespace: dict[str, Any] = {"__name__": "readme_section_33"}
        try:
            async with embedded_database():
                for code in _readme_blocks(_section_33()):
                    compiled = compile(code, "<README section 33>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                    # A doctest of this repository's own README (trusted text), top-level awaits included.
                    outcome = eval(compiled, namespace)
                    if inspect.iscoroutine(outcome):
                        await outcome
                    assert capsys.readouterr().out.splitlines() == _readme_printed(code), code
        finally:
            namespace.clear()
