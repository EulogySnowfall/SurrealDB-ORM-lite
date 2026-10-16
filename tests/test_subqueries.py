"""Subqueries (v0.27.0): ``Subquery(qs)`` — or a QuerySet itself — as a filter value in ``filter()`` and ``Q``.

The inner queryset compiles to a sub-SELECT the server runs inside the outer statement: one request, a block
``{ LET $_sq0 = (…); (<statement>) };`` for every read, count, aggregation, write by filter and ``get_or_create``
lookup, the sub-SELECT inline in a live query, read at the outer moment under ``as_of()``. Unit tests first (the
``Subquery`` object, the per-statement compiler, the compiled text, the refusals: no engine, no server); the
classes that open an engine or a server are suffixed ``E2E``. Most of them run on the embedded ``mem://`` engine
and on the server of ``SURREALDB_PORT`` (both lines in CI, over WebSocket and HTTP); the time-travel classes also
use the embedded ``surrealkv+versioned://`` engine and a server started with versioned storage, found through
``SURREALDB_VERSIONED_PORT`` (skipped without it).
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import Field
from surrealdb import RecordID

import surreal_orm_lite
from surreal_orm_lite import (
    BaseSurrealEdge,
    BaseSurrealModel,
    Count,
    PointField,
    Q,
    Subquery,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    SurrealDbError,
    Var,
)
from surreal_orm_lite.query_set import QuerySet
from surreal_orm_lite.subquery import SubqueryCompiler, _same_value, missing_table, subquery_of
from surreal_orm_lite.utils import build_filter_condition

M2020 = datetime(2020, 6, 1, tzinfo=UTC)
M2021 = datetime(2021, 6, 1, tzinfo=UTC)

# --- models (spec §9) ---------------------------------------------------------------------------------


class SqPerson(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="sq_person")

    id: str | None = None
    name: str = ""
    active: bool = True
    age: int = 0
    nick: str = ""


class SqPost(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="sq_post")

    id: str | None = None
    title: str = ""
    author: RecordID | None = None
    score: int = 0
    tags: list[str] = []
    spot: PointField = None


class SqAliased(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="sq_aliased")

    id: str | None = None
    display: str = Field("", alias="display_name")
    rank: int = 0


class SqLikes(BaseSurrealEdge):
    model_config = SurrealConfigDict(table_name="sq_likes")

    w: int = 1


class SqGhost(BaseSurrealModel):
    """A table never written: a subquery reading it reads a table that does not exist."""

    model_config = SurrealConfigDict(table_name="sq_ghost")

    id: str | None = None
    x: int = 0


# The exact texts of the messages (spec §5.5), written out once so a drift in a builder shows here.


def _s1(value: object) -> str:
    return f"Subquery() takes a QuerySet (Model.objects()...), not {type(value).__name__}"


def _s2(lookup: str) -> str:
    return (
        f"'{lookup}' cannot take a subquery: in, not_in, containsall and containsany compare with all its values; "
        "exact, gt, gte, lt, lte, contains and not_contains with its first value"
    )


_S3 = "A queryset cannot be its own subquery: build the inner queryset with a new Model.objects() call."


def _s4(*offenders: str) -> str:
    return (
        f"A subquery cannot honour {', '.join(offenders)}: it gives the outer filter one list of values, SELECT VALUE "
        "<one field>. Drop the clause; filter(), Q, nearby(), select() of one field, order_by(), limit(), offset() "
        "and variables() are supported."
    )


_S5 = "A subquery reads at the moment of the queryset that uses it: call as_of() on the outer queryset, not on the subquery."
_S6 = (
    "A subquery runs inside the statement of the queryset that uses it, so it cannot have a transaction of its own: "
    "build it with the same objects(tx=) as the outer queryset, or with none."
)


def _s7(name: str) -> str:
    return (
        f"The subquery binds ${name} to another value than the queryset that uses it; they share one statement: "
        "rename one of the variables()."
    )


_S8 = (
    "A subquery compiles only inside a QuerySet (filter(), get_or_create(), live()...), which binds it in the "
    "statement it sends; Q.to_sql() alone cannot."
)


def _s12(clause: str, value: object) -> str:
    return f"A subquery's {clause} takes an int >= 0, got {value!r}"


_S13 = (
    "'_sqv' names the values of an ordered subquery: a subquery can order by the column _sqv only when it selects "
    "_sqv itself, and never by a path under it such as _sqv.x."
)


def _s15(field: str, lookup: str) -> str:
    return f"'{lookup}' takes a subquery as its whole value, not inside a list: {field}__{lookup}=Subquery(...)"


def _record_id_refused(lookup: str) -> str:
    """The pre-existing refusal of a text/collection lookup on the ``id`` column (issue #159), unchanged."""
    return (
        f"'{lookup}' cannot be applied to the record id column 'id': a record id is not a string. Filter on a "
        "regular column, or reach for the textual form explicitly in a raw query (e.g. "
        "string::contains(record::id(id), '...'))."
    )


class _Raising:
    """An object whose ``==`` raises against any other object (``is`` still holds for itself)."""

    def __eq__(self, other: object) -> bool:
        raise RuntimeError("cannot compare")

    __hash__ = object.__hash__


# --- AC-1: the Subquery object --------------------------------------------------------------------


class TestSubqueryObject:
    """AC-1: ``Subquery(qs)`` holds the queryset, refuses anything else, compares by queryset, is exported."""

    def test_queryset_attribute(self) -> None:
        qs = SqPerson.objects().filter(active=True)
        assert Subquery(qs).queryset is qs

    def test_a_queryset_subclass_is_accepted(self) -> None:
        class MyQuerySet(QuerySet):
            pass

        qs = MyQuerySet(SqPerson)
        assert Subquery(qs).queryset is qs

    @pytest.mark.parametrize(
        "value",
        ["x", [SqPerson.objects()], SqPerson, None, 1],
        ids=["string", "list-of-queryset", "model-class", "none", "int"],
    )
    def test_anything_but_a_queryset_raises_s1(self, value: object) -> None:
        with pytest.raises(TypeError, match=r"Subquery\(\) takes a QuerySet") as info:
            Subquery(value)  # type: ignore[arg-type]
        assert str(info.value) == _s1(value)
        assert type(value).__name__ in str(info.value)

    def test_a_subquery_is_not_a_queryset(self) -> None:
        sub = Subquery(SqPerson.objects())
        with pytest.raises(TypeError) as info:
            Subquery(sub)  # type: ignore[arg-type]
        assert str(info.value) == _s1(sub) == "Subquery() takes a QuerySet (Model.objects()...), not Subquery"

    def test_repr(self) -> None:
        qs = SqPerson.objects()
        text = repr(Subquery(qs))
        assert text.startswith("Subquery(")
        assert text == f"Subquery({qs!r})"

    def test_equal_for_the_same_queryset(self) -> None:
        qs = SqPerson.objects()
        assert Subquery(qs) == Subquery(qs)
        assert (Subquery(qs) != Subquery(qs)) is False

    def test_not_equal_for_two_querysets(self) -> None:
        assert Subquery(SqPerson.objects()) != Subquery(SqPerson.objects())

    def test_not_equal_to_another_type_without_raising(self) -> None:
        sub = Subquery(SqPerson.objects())
        assert sub != "x"
        assert (sub == "x") is False
        assert sub != sub.queryset

    def test_hash_follows_the_queryset(self) -> None:
        qs = SqPerson.objects()
        assert hash(Subquery(qs)) == hash(Subquery(qs))
        assert len({Subquery(qs), Subquery(qs)}) == 1

    def test_slots(self) -> None:
        sub = Subquery(SqPerson.objects())
        with pytest.raises(AttributeError):
            sub.other = 1  # type: ignore[attr-defined]

    def test_subquery_of_a_subquery_is_itself(self) -> None:
        sub = Subquery(SqPerson.objects())
        assert subquery_of(sub) is sub

    def test_subquery_of_a_queryset_wraps_it(self) -> None:
        qs = SqPerson.objects()
        sub = subquery_of(qs)
        assert isinstance(sub, Subquery)
        assert sub.queryset is qs

    @pytest.mark.parametrize(
        "value", [1, None, "x", [SqPerson.objects()], SqPerson], ids=["int", "none", "str", "list", "model"]
    )
    def test_subquery_of_anything_else_is_none(self, value: object) -> None:
        assert subquery_of(value) is None

    def test_export(self) -> None:
        assert "Subquery" in surreal_orm_lite.__all__
        assert surreal_orm_lite.__all__.index("Subquery") == surreal_orm_lite.__all__.index("Q") + 1
        assert surreal_orm_lite.Subquery is surreal_orm_lite.subquery.Subquery


# --- the compiler's bookkeeping (spec §6.1) -------------------------------------------------------


_DIFFERENT: list[tuple[object, object]] = [
    (1, True),
    (0, False),
    (1, 1.0),
    ([1], [True]),
    ((1,), (True,)),
    ({"k": 1}, {"k": True}),
    ({1}, {True}),
    ([1], [1, 2]),
    ({"a": 1}, {"b": 1}),
    (_Raising(), _Raising()),
]
_DIFFERENT_IDS = [
    "int-bool",
    "zero-false",
    "int-float",
    "list-int-bool",
    "tuple-int-bool",
    "dict-int-bool",
    "set-int-bool",
    "list-lengths",
    "dict-keys",
    "eq-raises",
]

_RAISING = _Raising()
_SAME: list[tuple[object, object]] = [
    (_RAISING, _RAISING),
    ("a", "a"),
    ([1, 2], [1, 2]),
    ({"k": [1]}, {"k": [1]}),
    (datetime(2021, 6, 1, tzinfo=UTC), datetime(2021, 6, 1, tzinfo=UTC)),
    (None, None),
]
_SAME_IDS = ["same-object", "equal-strings", "equal-lists", "equal-nested-dicts", "equal-aware-datetimes", "none"]


class TestCompilerBookkeeping:
    """Spec §6.1: ``SubqueryCompiler`` builds on its own, wraps a statement, merges variables strictly (S7)."""

    def test_builds_with_no_argument(self) -> None:
        compiler = SubqueryCompiler()
        assert compiler.inline is False
        assert compiler.empty_tables == frozenset()
        assert compiler.outer_tx is None
        assert compiler.moment is None
        assert compiler.variables == {}
        assert compiler.lets == []
        assert compiler.tables == set()
        assert compiler._stack == []

    def test_mutable_fields_are_not_shared(self) -> None:
        one, two = SubqueryCompiler(), SubqueryCompiler()
        one.variables["a"] = 1
        one.lets.append("LET $_sq0 = [];")
        one.tables.add("t")
        one._stack.append(1)
        assert (two.variables, two.lets, two.tables, two._stack) == ({}, [], set(), [])

    def test_wrap_without_lets_is_the_statement(self) -> None:
        statement = "SELECT 1;"
        assert SubqueryCompiler().wrap(statement) is statement

    def test_wrap_with_one_let(self) -> None:
        compiler = SubqueryCompiler(lets=["LET $_sq0 = (SELECT VALUE id FROM t);"])
        assert (
            compiler.wrap("SELECT * FROM u WHERE x IN $_sq0;")
            == "{ LET $_sq0 = (SELECT VALUE id FROM t); (SELECT * FROM u WHERE x IN $_sq0) };"
        )

    def test_wrap_with_two_lets_keeps_their_order(self) -> None:
        compiler = SubqueryCompiler(lets=["LET $_sq0 = (SELECT VALUE a FROM t);", "LET $_sq1 = [];"])
        assert compiler.wrap("SELECT * FROM u WHERE x IN $_sq1;") == (
            "{ LET $_sq0 = (SELECT VALUE a FROM t); LET $_sq1 = []; (SELECT * FROM u WHERE x IN $_sq1) };"
        )

    @pytest.mark.parametrize(("a", "b"), _DIFFERENT, ids=_DIFFERENT_IDS)
    def test_same_value_tells_different_values_apart(self, a: object, b: object) -> None:
        assert _same_value(a, b) is False
        assert _same_value(b, a) is False

    @pytest.mark.parametrize(("a", "b"), _SAME, ids=_SAME_IDS)
    def test_same_value_accepts_the_same_value(self, a: object, b: object) -> None:
        assert _same_value(a, b) is True

    def test_merge_adds_new_names_in_order(self) -> None:
        compiler = SubqueryCompiler(variables={"outer": 0})
        compiler.merge_variables({"b": 2, "a": 1})
        compiler.merge_variables({"c": 3})
        assert list(compiler.variables.items()) == [("outer", 0), ("b", 2), ("a", 1), ("c", 3)]

    def test_merge_accepts_an_equal_value_of_the_same_type(self) -> None:
        first = [1, 2]
        compiler = SubqueryCompiler(variables={"x": first})
        compiler.merge_variables({"x": [1, 2]})
        assert compiler.variables == {"x": [1, 2]}
        assert compiler.variables["x"] is first

    def test_merge_accepts_the_same_object(self) -> None:
        value = _Raising()
        compiler = SubqueryCompiler(variables={"x": value})
        compiler.merge_variables({"x": value})
        assert compiler.variables["x"] is value

    @pytest.mark.parametrize(
        ("outer", "inner"),
        [(1, 2), (1, True), (1, 1.0), (_Raising(), _Raising())],
        ids=["two-after-one", "true-after-one", "float-after-int", "eq-raises"],
    )
    def test_merge_refuses_another_value_s7(self, outer: object, inner: object) -> None:
        compiler = SubqueryCompiler(variables={"x": outer})
        with pytest.raises(ValueError, match=r"binds \$x") as info:
            compiler.merge_variables({"x": inner})
        assert str(info.value) == _s7("x")
        assert compiler.variables["x"] is outer

    @pytest.mark.parametrize(
        ("message", "table"),
        [
            ("The table 'sq_ghost' does not exist", "sq_ghost"),
            ("An error occurred: The table 'a_b' does not exist", "a_b"),
            ("boom", None),
        ],
        ids=["not-found", "thrown", "other"],
    )
    def test_missing_table(self, message: str, table: str | None) -> None:
        assert missing_table(Exception(message)) == table


# --- AC-2: the compiled text (spec §6.5) -----------------------------------------------------------

_GUARD = "(SELECT VALUE id FROM ONLY $parent.id VERSION $_as_of) != NONE"
_C1_LET = "LET $_sq0 = (SELECT VALUE id FROM sq_person WHERE active = $_f0);"


def _active() -> Any:
    """The active persons: C1's inner queryset."""
    return SqPerson.objects().filter(active=True)


def _with_inner(inner: Any) -> Any:
    """A C1-shaped outer queryset: the posts whose author is one of *inner*'s values."""
    return SqPost.objects().filter(author__in=Subquery(inner))


def _let_value(inner: Any) -> tuple[str, dict[str, Any]]:
    """The ``LET`` value a C1-shaped statement binds for *inner*, and the statement's variables."""
    text, variables = _with_inner(inner)._compile_query()
    prefix, suffix = "{ LET $_sq0 = ", "; (SELECT * FROM sq_post WHERE author IN $_sq0) };"
    assert text.startswith(prefix), text
    assert text.endswith(suffix), text
    return text[len(prefix) : -len(suffix)], variables


class TestCompileSubquery:
    """AC-2: every C-text of spec §6.5 a compile function returns, exactly, with its variables."""

    def test_c1(self) -> None:
        assert _with_inner(_active())._compile_query() == (
            "{ LET $_sq0 = (SELECT VALUE id FROM sq_person WHERE active = $_f0); "
            "(SELECT * FROM sq_post WHERE author IN $_sq0) };",
            {"_f0": True},
        )

    def test_c1_bare_queryset_compiles_the_same(self) -> None:
        bare = SqPost.objects().filter(author__in=_active())
        assert bare._compile_query() == _with_inner(_active())._compile_query()

    def test_c3(self) -> None:
        qs = SqPost.objects().filter(score__gt=5, author__in=Subquery(_active()))
        assert qs._compile_query() == (
            "{ LET $_sq0 = (SELECT VALUE id FROM sq_person WHERE active = $_f1); "
            "(SELECT * FROM sq_post WHERE score > $_f0 AND author IN $_sq0) };",
            {"_f0": 5, "_f1": True},
        )

    def test_an_outer_filter_after_a_subquery_continues_the_numbering(self) -> None:
        qs = SqPost.objects().filter(author__in=Subquery(_active()), score__gt=5)
        assert qs._compile_query() == (
            f"{{ {_C1_LET} (SELECT * FROM sq_post WHERE author IN $_sq0 AND score > $_f1) }};",
            {"_f0": True, "_f1": 5},
        )

    def test_c4_ordered_and_limited(self) -> None:
        inner = _active().order_by("-age").limit(1)
        assert _with_inner(inner)._compile_query() == (
            "{ LET $_sq0 = (SELECT id AS _sqv, age FROM sq_person WHERE active = $_f0 ORDER BY age DESC LIMIT 1)._sqv; "
            "(SELECT * FROM sq_post WHERE author IN $_sq0) };",
            {"_f0": True},
        )

    def test_c5_first_value(self) -> None:
        best = SqPost.objects().select("score").order_by("-score").limit(1)
        assert SqPost.objects().filter(score=Subquery(best))._compile_query() == (
            "{ LET $_sq0 = (SELECT score AS _sqv, score FROM sq_post ORDER BY score DESC LIMIT 1)._sqv; "
            "(SELECT * FROM sq_post WHERE ($_sq0 != [] AND score = array::first($_sq0))) };",
            {},
        )

    @pytest.mark.parametrize(
        ("lookup", "op"),
        [("gt", ">"), ("gte", ">="), ("lt", "<"), ("lte", "<="), ("contains", "CONTAINS"), ("not_contains", "CONTAINSNOT")],
    )
    def test_every_first_value_lookup_is_guarded(self, lookup: str, op: str) -> None:
        names = SqPerson.objects().select("nick")
        text, variables = SqPost.objects().filter(**{f"tags__{lookup}": Subquery(names)})._compile_query()
        assert text == (
            "{ LET $_sq0 = (SELECT VALUE nick FROM sq_person); "
            f"(SELECT * FROM sq_post WHERE ($_sq0 != [] AND tags {op} array::first($_sq0))) }};"
        )
        assert variables == {}

    @pytest.mark.parametrize(
        ("lookup", "op"), [("in", "IN"), ("not_in", "NOT IN"), ("containsall", "CONTAINSALL"), ("containsany", "CONTAINSANY")]
    )
    def test_every_collection_lookup_takes_all_the_values(self, lookup: str, op: str) -> None:
        names = SqPerson.objects().select("nick")
        text, _ = SqPost.objects().filter(**{f"tags__{lookup}": Subquery(names)})._compile_query()
        assert text == f"{{ LET $_sq0 = (SELECT VALUE nick FROM sq_person); (SELECT * FROM sq_post WHERE tags {op} $_sq0) }};"

    def test_c6_nested(self) -> None:
        writers = SqPost.objects().filter(score__gt=8).select("author")
        inner = SqPerson.objects().filter(id__in=Subquery(writers))
        assert _with_inner(inner)._compile_query() == (
            "{ LET $_sq0 = (SELECT VALUE author FROM sq_post WHERE score > $_f0); "
            "LET $_sq1 = (SELECT VALUE id FROM sq_person WHERE id IN $_sq0); "
            "(SELECT * FROM sq_post WHERE author IN $_sq1) };",
            {"_f0": 8},
        )

    def test_c7_q(self) -> None:
        inactive = SqPerson.objects().filter(active=False)
        qs = SqPost.objects().filter(Q(author__in=Subquery(inactive)) | Q(score__gte=9))
        assert qs._compile_query() == (
            "{ LET $_sq0 = (SELECT VALUE id FROM sq_person WHERE active = $_f0); "
            "(SELECT * FROM sq_post WHERE (author IN $_sq0 OR score >= $_f1)) };",
            {"_f0": False, "_f1": 9},
        )

    def test_negated_q(self) -> None:
        qs = SqPost.objects().filter(~Q(author__in=Subquery(_active())))
        assert qs._compile_query() == (
            f"{{ {_C1_LET} (SELECT * FROM sq_post WHERE NOT (author IN $_sq0)) }};",
            {"_f0": True},
        )

    def test_c8_count(self) -> None:
        assert _with_inner(_active())._compile_aggregation_query("count()") == (
            f"{{ {_C1_LET} (SELECT count() FROM sq_post WHERE author IN $_sq0 GROUP ALL) }};",
            {"_f0": True},
        )

    def test_c8_sum(self) -> None:
        assert _with_inner(_active())._compile_aggregation_query("math::sum(score)", alias="sum") == (
            f"{{ {_C1_LET} (SELECT math::sum(score) AS sum FROM sq_post WHERE author IN $_sq0 GROUP ALL) }};",
            {"_f0": True},
        )

    def test_c8_group_by(self) -> None:
        qs = _with_inner(_active()).values("author").annotate(n=Count())
        assert qs._compile_group_by_query() == (
            f"{{ {_C1_LET} (SELECT author, count() AS n FROM sq_post WHERE author IN $_sq0 GROUP BY author) }};",
            {"_f0": True},
        )

    def test_c10_outer_clauses(self) -> None:
        qs = _with_inner(_active()).order_by("-score").limit(2).offset(1).fetch("author")
        assert qs._compile_query() == (
            f"{{ {_C1_LET} (SELECT * FROM sq_post WHERE author IN $_sq0 ORDER BY score DESC LIMIT 2 START 1 FETCH author) }};",
            {"_f0": True},
        )

    def test_c11_inner_var_and_variables(self) -> None:
        inner = SqPerson.objects().filter(age__gte=Var("min_age")).variables(min_age=30)
        assert _with_inner(inner)._compile_query() == (
            "{ LET $_sq0 = (SELECT VALUE id FROM sq_person WHERE age >= $min_age); "
            "(SELECT * FROM sq_post WHERE author IN $_sq0) };",
            {"min_age": 30},
        )

    def test_merged_variables_follow_the_outer_ones(self) -> None:
        inner = SqPerson.objects().filter(age__gte=Var("min_age")).variables(min_age=30, shared="s")
        qs = _with_inner(inner).variables(shared="s", a=1)
        assert list(qs._compile_query()[1].items()) == [("shared", "s"), ("a", 1), ("min_age", 30)]

    def test_c13_as_of(self) -> None:
        qs = SqPost.objects().as_of(M2021).filter(author__in=Subquery(_active()))
        assert qs._compile_query(M2021) == (
            f"{{ LET $_sq0 = (SELECT VALUE id FROM sq_person WHERE active = $_f0 AND {_GUARD} VERSION $_as_of); "
            f"(SELECT * FROM sq_post WHERE author IN $_sq0 AND {_GUARD} VERSION $_as_of) }};",
            {"_f0": True, "_as_of": M2021},
        )

    def test_c13_ordered_inner_under_as_of(self) -> None:
        qs = SqPost.objects().as_of(M2021).filter(author__in=Subquery(_active().order_by("-age").limit(1)))
        assert qs._compile_query(M2021) == (
            f"{{ LET $_sq0 = (SELECT id AS _sqv, age FROM sq_person WHERE active = $_f0 AND {_GUARD} ORDER BY age "
            f"DESC LIMIT 1 VERSION $_as_of)._sqv; (SELECT * FROM sq_post WHERE author IN $_sq0 AND {_GUARD} "
            "VERSION $_as_of) };",
            {"_f0": True, "_as_of": M2021},
        )

    def test_c13_aggregation_and_group_by_under_as_of(self) -> None:
        qs = SqPost.objects().as_of(M2021).filter(author__in=Subquery(_active()))
        let = f"LET $_sq0 = (SELECT VALUE id FROM sq_person WHERE active = $_f0 AND {_GUARD} VERSION $_as_of);"
        assert qs._compile_aggregation_query("count()", moment=M2021) == (
            f"{{ {let} (SELECT count() FROM sq_post WHERE author IN $_sq0 AND {_GUARD} GROUP ALL VERSION $_as_of) }};",
            {"_f0": True, "_as_of": M2021},
        )
        grouped = qs.values("author").annotate(n=Count())
        assert grouped._compile_group_by_query(M2021) == (
            f"{{ {let} (SELECT author, count() AS n FROM sq_post WHERE author IN $_sq0 AND {_GUARD} GROUP BY author "
            "VERSION $_as_of) };",
            {"_f0": True, "_as_of": M2021},
        )

    def test_c14_retry_form(self) -> None:
        qs = _with_inner(_active())
        assert qs._compile_query(subqueries=qs._subquery_compiler(frozenset({"sq_person"}))) == (
            "{ LET $_sq0 = []; (SELECT * FROM sq_post WHERE author IN $_sq0) };",
            {},
        )

    def test_c14_an_empty_table_stops_the_nesting(self) -> None:
        writers = SqPost.objects().filter(score__gt=8).select("author")
        qs = _with_inner(SqPerson.objects().filter(id__in=Subquery(writers)))
        compiler = qs._subquery_compiler(frozenset({"sq_person"}))
        assert qs._compile_query(subqueries=compiler) == (
            "{ LET $_sq0 = []; (SELECT * FROM sq_post WHERE author IN $_sq0) };",
            {},
        )
        assert compiler.tables == {"sq_person"}

    def test_the_compiler_records_the_tables_read(self) -> None:
        writers = SqPost.objects().filter(score__gt=8).select("author")
        qs = _with_inner(SqPerson.objects().filter(id__in=Subquery(writers)))
        compiler = qs._subquery_compiler()
        qs._compile_query(subqueries=compiler)
        assert compiler.tables == {"sq_person", "sq_post"}
        assert compiler.moment is None
        assert compiler._stack == [id(qs)]

    def test_c15_live_inlines_the_sub_select(self) -> None:
        assert _with_inner(_active())._compile_live(False) == (
            "LIVE SELECT * FROM sq_post WHERE author IN (SELECT VALUE id FROM sq_person WHERE active = true);"
        )

    def test_c16_live_first_value(self) -> None:
        best = SqPost.objects().select("score").order_by("-score").limit(1)
        sub = "(SELECT score AS _sqv, score FROM sq_post ORDER BY score DESC LIMIT 1)._sqv"
        assert SqPost.objects().filter(score=Subquery(best))._compile_live(False) == (
            f"LIVE SELECT * FROM sq_post WHERE ({sub} != [] AND score = array::first({sub}));"
        )

    def test_live_inlines_the_inner_variables(self) -> None:
        inner = SqPerson.objects().filter(age__gte=Var("min_age")).variables(min_age=30)
        assert _with_inner(inner)._compile_live(True) == (
            "LIVE SELECT DIFF FROM sq_post WHERE author IN (SELECT VALUE id FROM sq_person WHERE age >= 30);"
        )

    def test_c22_alias(self) -> None:
        qs = SqPost.objects().filter(title__in=Subquery(SqAliased.objects().select("display").order_by("-rank")))
        assert qs._compile_query() == (
            "{ LET $_sq0 = (SELECT display_name AS _sqv, rank FROM sq_aliased ORDER BY rank DESC)._sqv; "
            "(SELECT * FROM sq_post WHERE title IN $_sq0) };",
            {},
        )

    def test_c23_one_field_no_order(self) -> None:
        assert _let_value(SqPost.objects().filter(score__gt=8).select("author")) == (
            "(SELECT VALUE author FROM sq_post WHERE score > $_f0)",
            {"_f0": 8},
        )

    def test_inner_fetch_is_ignored(self) -> None:
        """P1: ``fetch()`` on the inner queryset is never compiled: C23's text, no ``FETCH`` anywhere."""
        inner = SqPost.objects().filter(score__gt=8).select("author").fetch("author")
        assert _let_value(inner) == ("(SELECT VALUE author FROM sq_post WHERE score > $_f0)", {"_f0": 8})
        assert "FETCH" not in _with_inner(inner)._compile_query()[0]

    def test_c24_limit_and_offset_without_order(self) -> None:
        assert _let_value(SqPerson.objects().limit(2).offset(1)) == ("(SELECT VALUE id FROM sq_person LIMIT 2 START 1)", {})

    def test_c25_edge_endpoint(self) -> None:
        assert SqLikes.objects().filter(in___in=Subquery(_active()))._compile_query() == (
            f"{{ {_C1_LET} (SELECT * FROM sq_likes WHERE in IN $_sq0) }};",
            {"_f0": True},
        )

    def test_c26_containsany(self) -> None:
        qs = SqPost.objects().filter(tags__containsany=Subquery(SqPerson.objects().select("name")))
        assert qs._compile_query() == (
            "{ LET $_sq0 = (SELECT VALUE name FROM sq_person); (SELECT * FROM sq_post WHERE tags CONTAINSANY $_sq0) };",
            {},
        )

    @pytest.mark.parametrize(
        ("inner", "expected"),
        [
            (
                lambda: SqPerson.objects().select("_sqv").order_by("_sqv"),
                "(SELECT _sqv AS _sqv, _sqv FROM sq_person ORDER BY _sqv ASC)._sqv",
            ),
            (
                lambda: SqPerson.objects().select("_sqv.x").order_by("age"),
                "(SELECT _sqv.x AS _sqv, age FROM sq_person ORDER BY age ASC)._sqv",
            ),
        ],
        ids=["select-and-order-sqv", "select-sqv-path-order-other"],
    )
    def test_the_two_sqv_shapes_s13_allows(self, inner: Callable[[], Any], expected: str) -> None:
        assert _let_value(inner()) == (expected, {})

    def test_the_order_columns_are_listed_once(self) -> None:
        inner = SqPerson.objects().order_by("-active", "age", "-age").limit(1)
        assert _let_value(inner) == (
            "(SELECT id AS _sqv, active, age FROM sq_person ORDER BY active DESC, age ASC, age DESC LIMIT 1)._sqv",
            {},
        )

    def test_the_same_inner_queryset_twice_gets_two_lets(self) -> None:
        sub = Subquery(_active())
        qs = SqPost.objects().filter(author__in=sub).filter(Q(author__not_in=sub))
        assert qs._compile_query() == (
            "{ LET $_sq0 = (SELECT VALUE id FROM sq_person WHERE active = $_f0); "
            "LET $_sq1 = (SELECT VALUE id FROM sq_person WHERE active = $_f1); "
            "(SELECT * FROM sq_post WHERE author IN $_sq0 AND author NOT IN $_sq1) };",
            {"_f0": True, "_f1": True},
        )

    def test_the_inner_queryset_is_read_when_the_outer_one_compiles(self) -> None:
        inner = SqPerson.objects()
        qs = _with_inner(inner)
        inner.filter(active=True)
        assert qs._compile_query() == _with_inner(_active())._compile_query()

    def test_the_outer_transaction_may_be_shared(self) -> None:
        tx = object()
        qs = SqPost.objects(tx=tx).filter(author__in=Subquery(SqPerson.objects(tx=tx).filter(active=True)))
        assert qs._compile_query() == _with_inner(_active())._compile_query()

    def test_the_outer_queryset_seeds_the_stack(self) -> None:
        qs = SqPost.objects()
        assert qs._subquery_compiler()._stack == [id(qs)]

    def test_the_compiler_carries_the_outer_context(self) -> None:
        tx = object()
        qs = SqPost.objects(tx=tx).variables(a=1)
        compiler = qs._subquery_compiler(frozenset({"t"}), inline=True)
        assert compiler.inline is True
        assert compiler.empty_tables == frozenset({"t"})
        assert compiler.outer_tx is tx
        assert compiler.variables == {"a": 1}
        assert compiler.variables is not qs._variables
        assert (compiler.lets, compiler.tables, compiler.moment) == ([], set(), None)


# --- AC-3: unchanged without a subquery ------------------------------------------------------------


class TestUnchangedWithoutSubquery:
    """AC-3: without a subquery every compile function returns v0.26.0's text and variables, in the same order."""

    def test_select(self) -> None:
        assert SqPost.objects().filter(score__gt=5).order_by("-score").limit(2)._compile_query() == (
            "SELECT * FROM sq_post WHERE score > $_f0 ORDER BY score DESC LIMIT 2;",
            {"_f0": 5},
        )

    def test_count(self) -> None:
        assert SqPost.objects()._compile_aggregation_query("count()") == ("SELECT count() FROM sq_post GROUP ALL;", {})

    def test_group_by(self) -> None:
        assert SqPost.objects().values("author").annotate(n=Count())._compile_group_by_query() == (
            "SELECT author, count() AS n FROM sq_post GROUP BY author;",
            {},
        )

    def test_live(self) -> None:
        assert SqPost.objects().filter(score__gt=5)._compile_live(False) == "LIVE SELECT * FROM sq_post WHERE score > 5;"

    def test_where_with_q(self) -> None:
        assert SqPost.objects().filter(Q(score__gte=9) | Q(title="x"))._build_where() == (
            " WHERE (score >= $_f0 OR title = $_f1)",
            {"_f0": 9, "_f1": "x"},
        )

    def test_variables_keep_their_order(self) -> None:
        qs = SqPost.objects().variables(a=1).filter(score__gt=5)
        assert list(qs._compile_query()[1].items()) == [("a", 1), ("_f0", 5)]
        assert list(qs._compile_aggregation_query("count()")[1].items()) == [("a", 1), ("_f0", 5)]
        grouped = SqPost.objects().variables(a=1).filter(score__gt=5).values("author").annotate(n=Count())
        assert list(grouped._compile_group_by_query()[1].items()) == [("a", 1), ("_f0", 5)]

    def test_a_q_compiled_on_its_own(self) -> None:
        assert Q(score__gte=9).to_sql() == ("score >= $_f0", {"_f0": 9}, 1)


# --- AC-4: refusals, before any request (compile-time part) -----------------------------------------


@pytest.fixture
def _no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything asks for a client: the refusals come before any request."""

    async def _forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("a request was made")

    monkeypatch.setattr(SurrealDBConnectionManager, "get_client", _forbidden)


def _filtered(key: str, inner: Callable[[], Any] = _active) -> Callable[[], Any]:
    """A builder of ``SqPost.objects().filter(<key>=Subquery(inner()))``."""
    return lambda: SqPost.objects().filter(**{key: Subquery(inner())})


def _own_subquery(qs: Any) -> Any:
    """*qs* filtered by a subquery of itself."""
    return qs.filter(id__in=Subquery(qs))


def _a_b_a() -> Any:
    """A filtered by B, which is filtered by A: a cycle through another subquery."""
    a = SqPost.objects()
    b = SqPerson.objects().filter(id__in=Subquery(a))
    return a.filter(author__in=Subquery(b))


_TX_A, _TX_B = object(), object()

_S2_KEYS = [
    ("author__isnull", "isnull"),
    ("title__like", "like"),
    ("title__ilike", "ilike"),
    ("title__startswith", "startswith"),
    ("title__endswith", "endswith"),
    ("title__match", "match"),
    ("title__regex", "regex"),
    ("spot__inside", "inside"),
    ("spot__outside", "outside"),
    ("spot__intersects", "intersects"),
]

# (id, outer queryset builder, error type, exact message, whether live()/watch() reach the same refusal: an outer
# clause a live query refuses — values(), objects(tx=) — is reported first there).
_REFUSALS: list[tuple[str, Callable[[], Any], type[Exception], str, bool]] = [
    *[(f"S2-{lookup}", _filtered(key), ValueError, _s2(lookup), True) for key, lookup in _S2_KEYS],
    ("S3-direct", lambda: _own_subquery(SqPost.objects()), ValueError, _S3, True),
    ("S3-a-b-a", _a_b_a, ValueError, _S3, True),
    ("S3-before-S4", lambda: _own_subquery(SqPost.objects().values("title")), ValueError, _S3, False),
    ("S4-values", _filtered("author__in", lambda: SqPerson.objects().values("active")), ValueError, _s4("values()"), True),
    (
        "S4-annotate",
        _filtered("author__in", lambda: SqPerson.objects().annotate(n=Count())),
        ValueError,
        _s4("annotate()"),
        True,
    ),
    (
        "S4-select",
        _filtered("author__in", lambda: SqPerson.objects().select("id", "name")),
        ValueError,
        _s4("select() of several fields"),
        True,
    ),
    (
        "S4-all-three",
        _filtered("author__in", lambda: SqPerson.objects().values("active").annotate(n=Count()).select("id", "name")),
        ValueError,
        _s4("annotate()", "select() of several fields", "values()"),
        True,
    ),
    ("S5", _filtered("author__in", lambda: SqPerson.objects().as_of(M2021)), SurrealDbError, _S5, True),
    ("S6-inner-only", _filtered("author__in", lambda: SqPerson.objects(tx=_TX_A)), SurrealDbError, _S6, True),
    (
        "S6-two-transactions",
        lambda: SqPost.objects(tx=_TX_A).filter(author__in=Subquery(SqPerson.objects(tx=_TX_B))),
        SurrealDbError,
        _S6,
        False,
    ),
    (
        "S7-two-for-one",
        lambda: SqPost.objects().variables(x=1).filter(author__in=Subquery(SqPerson.objects().variables(x=2))),
        ValueError,
        _s7("x"),
        True,
    ),
    (
        "S7-true-for-one",
        lambda: SqPost.objects().variables(x=1).filter(author__in=Subquery(SqPerson.objects().variables(x=True))),
        ValueError,
        _s7("x"),
        True,
    ),
    (
        "S12-limit-negative",
        _filtered("author__in", lambda: SqPerson.objects().limit(-1)),
        ValueError,
        _s12("limit()", -1),
        True,
    ),
    (
        "S12-limit-bool",
        _filtered("author__in", lambda: SqPerson.objects().limit(True)),
        ValueError,
        _s12("limit()", True),
        True,
    ),
    (
        "S12-offset-str",
        _filtered("author__in", lambda: SqPerson.objects().offset("1")),
        ValueError,
        _s12("offset()", "1"),
        True,
    ),
    ("S13-order-sqv", _filtered("author__in", lambda: SqPerson.objects().order_by("_sqv")), ValueError, _S13, True),
    ("S13-order-sqv-path", _filtered("author__in", lambda: SqPerson.objects().order_by("_sqv.x")), ValueError, _S13, True),
    (
        "S13-select-other-order-sqv",
        _filtered("author__in", lambda: SqPerson.objects().select("name").order_by("-_sqv")),
        ValueError,
        _S13,
        True,
    ),
    (
        "S13-select-and-order-sqv-path",
        _filtered("author__in", lambda: SqPerson.objects().select("_sqv.x").order_by("_sqv.x")),
        ValueError,
        _S13,
        True,
    ),
    (
        "S15-subquery-in-a-list",
        lambda: SqPost.objects().filter(author__in=[Subquery(_active())]),
        ValueError,
        _s15("author", "in"),
        True,
    ),
    ("S15-queryset-in-a-list", lambda: SqPost.objects().filter(author__in=[_active()]), ValueError, _s15("author", "in"), True),
    (
        "S15-later-item",
        lambda: SqPost.objects().filter(tags__containsany=["x", _active()]),
        ValueError,
        _s15("tags", "containsany"),
        True,
    ),
    (
        "record-id-containsany",
        _filtered("id__containsany"),
        ValueError,
        _record_id_refused("containsany"),
        True,
    ),
    ("record-id-contains", _filtered("id__contains"), ValueError, _record_id_refused("contains"), True),
    ("unsupported-lookup", _filtered("title__nope"), ValueError, "Unsupported lookup type: 'nope'", True),
]


def _refusal_params(*, live_only: bool = False) -> list[Any]:
    return [
        pytest.param(build, error, message, id=case) for case, build, error, message, live in _REFUSALS if live or not live_only
    ]


class TestSubqueryRefusals:
    """AC-4 (compile-time part): every refusal raises its exact text when the outer queryset compiles, before any
    request — from the compile function, from ``exec()`` and, at the call, from ``live()``/``watch()``."""

    @pytest.mark.parametrize(("build", "error", "message"), _refusal_params())
    def test_compile_raises(self, build: Callable[[], Any], error: type[Exception], message: str) -> None:
        with pytest.raises(error) as info:
            build()._compile_query()
        assert str(info.value) == message

    @pytest.mark.usefixtures("_no_request")
    @pytest.mark.parametrize(("build", "error", "message"), _refusal_params())
    async def test_exec_raises_before_any_request(self, build: Callable[[], Any], error: type[Exception], message: str) -> None:
        with pytest.raises(error) as info:
            await build().exec()
        assert str(info.value) == message

    @pytest.mark.usefixtures("_no_request")
    @pytest.mark.parametrize("method", ["live", "watch"])
    @pytest.mark.parametrize(("build", "error", "message"), _refusal_params(live_only=True))
    def test_live_and_watch_raise_at_the_call(
        self, build: Callable[[], Any], error: type[Exception], message: str, method: str
    ) -> None:
        with pytest.raises(error) as info:
            getattr(build(), method)()
        assert str(info.value) == message

    def test_s3_before_s5_under_as_of(self) -> None:
        """An outer ``as_of()`` queryset given as its own subquery raises S3, not S5 (compile function only:
        ``exec()`` under ``as_of()`` acquires a client to resolve the moment)."""
        qs = _own_subquery(SqPost.objects().as_of(M2021))
        with pytest.raises(ValueError) as info:
            qs._compile_query(M2021)
        assert str(info.value) == _S3

    def test_s3_through_an_aggregation(self) -> None:
        with pytest.raises(ValueError) as info:
            _a_b_a()._compile_aggregation_query("count()")
        assert str(info.value) == _S3

    def test_s8_a_q_on_its_own(self) -> None:
        with pytest.raises(ValueError) as info:
            Q(author__in=Subquery(_active())).to_sql()
        assert str(info.value) == _S8

    def test_s8_build_filter_condition_without_a_compiler(self) -> None:
        with pytest.raises(ValueError) as info:
            build_filter_condition("author", "in", _active(), 0)
        assert str(info.value) == _S8

    def test_s2_comes_before_s8(self) -> None:
        with pytest.raises(ValueError) as info:
            Q(author__isnull=Subquery(_active())).to_sql()
        assert str(info.value) == _s2("isnull")

    @pytest.mark.usefixtures("_no_request")
    def test_changes_refuses_the_filter(self) -> None:
        with pytest.raises(SurrealDbError) as info:
            _with_inner(_active()).changes()
        assert str(info.value) == (
            "A change feed cannot honour filter(): SurrealDB's SHOW CHANGES reads the whole table's log, with no "
            "WHERE, projection, ordering or paging, outside any transaction. Call changes() on Model.objects() and "
            "filter the events."
        )
