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

from datetime import UTC, datetime

import pytest
from pydantic import Field
from surrealdb import RecordID

import surreal_orm_lite
from surreal_orm_lite import (
    BaseSurrealEdge,
    BaseSurrealModel,
    PointField,
    Subquery,
    SurrealConfigDict,
)
from surreal_orm_lite.query_set import QuerySet
from surreal_orm_lite.subquery import SubqueryCompiler, _same_value, missing_table, subquery_of

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


def _s7(name: str) -> str:
    return (
        f"The subquery binds ${name} to another value than the queryset that uses it; they share one statement: "
        "rename one of the variables()."
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
