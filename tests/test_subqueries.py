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

import contextlib
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import Field
from surrealdb import InternalError, RecordID, ThrownError

import surreal_orm_lite
from surreal_orm_lite import (
    BaseSurrealEdge,
    BaseSurrealModel,
    Count,
    GeoDistance,
    GeometryPoint,
    PointField,
    Q,
    Subquery,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    SurrealDbError,
    Var,
)
from surreal_orm_lite._sdk import NotFoundError, ServerError
from surreal_orm_lite.query_set import QuerySet
from surreal_orm_lite.subquery import (
    TABLE_PROBE,
    SubqueryCompiler,
    _MissingSubqueryTable,
    _same_value,
    missing_table,
    subquery_of,
)
from surreal_orm_lite.testing import embedded_database
from surreal_orm_lite.utils import build_filter_condition
from tests.conftest import _ws_url
from tests.test_time_travel import _http_url, _line, _record_queries, connected

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


def _s9a(field: str) -> str:
    return (
        f"Cannot create a record from the subquery given for '{field}': a subquery is a filter whose values only the "
        f"database knows. Use a lookup such as {field}__in, and pass the value to write in defaults=."
    )


def _s9b(method: str, key: str) -> str:
    return f"{method}(defaults=) cannot write the subquery given for '{key}': a subquery is a filter value, not data."


def _s10(field: str) -> str:
    return (
        f"bulk_update() writes its values as data; '{field}' got a subquery, which is a filter value: filter() with it "
        "and pass the value to write."
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


# --- the server's missing-table error, and only it ------------------------------------------------------------

#: The sentence SurrealDB 3.x says for a table that does not exist, naming ``sq_person``.
_SENTENCE = "The table 'sq_person' does not exist"
_THROWN = "An error occurred: "


def _not_found(message: str = _SENTENCE, table: str | None = "sq_person") -> NotFoundError:
    """A ``NotFoundError`` as SurrealDB 3.3.4 and 3.1.5 send it: its details name the table."""
    details = None if table is None else {"kind": "Table", "details": {"name": table}}
    return NotFoundError("NotFound", message, details=details)


class TestMissingTableMatcher:
    """``missing_table()`` reads only the server's exact forms, measured on 3.3.4 and 3.1.5 over WebSocket and HTTP:
    ``NotFoundError`` "The table '<t>' does not exist" whose details name ``<t>``, and 3.1's ``ThrownError``
    "An error occurred: The table '<t>' does not exist" for a ``LET`` inside a block. The sentence inside another
    error — a value the statement read, quoted by the server — is not the server saying the table is missing."""

    @pytest.mark.parametrize(
        ("error", "table"),
        [
            (_not_found(), "sq_person"),
            (_not_found("The table 'a_b' does not exist", "a_b"), "a_b"),
            (ThrownError("Thrown", _THROWN + _SENTENCE), "sq_person"),
        ],
        ids=["not-found", "not-found-other-name", "thrown-3.1-block"],
    )
    def test_the_server_forms(self, error: Exception, table: str) -> None:
        assert missing_table(error) == table

    @pytest.mark.parametrize(
        "error",
        [
            # The sentence inside a longer message: a stored value quoted by the server.
            InternalError("Internal", f'Could not cast into `int` using input `"{_SENTENCE}"`'),
            InternalError(
                "Internal",
                "Incorrect arguments for function math::sum(). Argument 1 was the wrong type. "
                f'Expected `array<number>` but found `"{_SENTENCE}"`',
            ),
            InternalError("Internal", f'Expected a int but cannot convert "{_SENTENCE}" into a int'),
            ThrownError("Thrown", f'{_THROWN}Could not cast into `int` using input `"{_SENTENCE}"`'),
            _not_found(f"{_SENTENCE}."),
            _not_found(f"Error: {_SENTENCE}"),
            _not_found(f"{_SENTENCE}\n"),
            # A user THROW: an InternalError on 2.x, wrapped twice inside a block on 3.1.
            InternalError("Internal", _THROWN + _SENTENCE),
            ThrownError("Thrown", _THROWN + _THROWN + _SENTENCE),
            # Not the type the server sends the sentence with.
            InternalError("Internal", _SENTENCE),
            ServerError("Internal", _SENTENCE),
            ThrownError("Thrown", _SENTENCE),
            Exception(_SENTENCE),
            # A NotFoundError whose details do not name that table, or a thrown error carrying details.
            _not_found(table=None),
            _not_found(table="sq_post"),
            ThrownError("Thrown", _THROWN + _SENTENCE, details={"kind": "Table", "details": {"name": "sq_person"}}),
            # Another sentence.
            _not_found("The record 'sq_person:ada' does not exist"),
            NotFoundError("NotFound", "boom"),
        ],
        ids=[
            "3.x-cast-quoting-it",
            "3.x-function-quoting-it",
            "2.x-cast-quoting-it",
            "3.1-cast-quoting-it",
            "trailing-text",
            "leading-text",
            "trailing-newline",
            "2.x-user-throw",
            "3.1-user-throw-in-a-block",
            "internal",
            "server-error",
            "thrown-without-prefix",
            "plain-exception",
            "not-found-without-details",
            "not-found-naming-another-table",
            "thrown-with-details",
            "record",
            "other",
        ],
    )
    def test_anything_else_is_none(self, error: Exception) -> None:
        assert missing_table(error) is None


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

    @pytest.mark.usefixtures("_no_request")
    @pytest.mark.parametrize(
        ("call", "error", "message"),
        [
            pytest.param(
                lambda: SqPost.objects().get_or_create(author=Subquery(_active())),
                SurrealDbError,
                _s9a("author"),
                id="S9a-get_or_create",
            ),
            pytest.param(
                lambda: SqPost.objects().update_or_create(title="one", author=_active(), defaults={"score": 1}),
                SurrealDbError,
                _s9a("author"),
                id="S9a-update_or_create-bare-queryset",
            ),
            pytest.param(
                lambda: SqPost.objects().get_or_create(score__exact=Subquery(_no_score())),
                SurrealDbError,
                _s9a("score"),
                id="S9a-exact-suffix",
            ),
            pytest.param(
                lambda: SqPost.objects().get_or_create(author__in=_active(), defaults={"author": Subquery(_active())}),
                SurrealDbError,
                _s9b("get_or_create", "author"),
                id="S9b-get_or_create",
            ),
            pytest.param(
                lambda: SqPost.objects().update_or_create(title="one", defaults={"score": _no_score()}),
                SurrealDbError,
                _s9b("update_or_create", "score"),
                id="S9b-update_or_create-bare-queryset",
            ),
            pytest.param(
                lambda: _with_inner(_active()).bulk_update(score=Subquery(_no_score())),
                ValueError,
                _s10("score"),
                id="S10",
            ),
            pytest.param(
                lambda: SqPost.objects().bulk_update(title="x", score=_no_score()),
                ValueError,
                _s10("score"),
                id="S10-bare-queryset",
            ),
        ],
    )
    async def test_a_subquery_as_a_written_value_raises_before_any_request(
        self, call: Callable[[], Awaitable[Any]], error: type[Exception], message: str
    ) -> None:
        """S9 and S10 (spec N3): a subquery is a filter value, never data to write."""
        with pytest.raises(error) as info:
            await call()
        assert str(info.value) == message


# --- the engines and the data (spec §9) ---------------------------------------------------------------

_TABLES = ("sq_person", "sq_post", "sq_aliased", "sq_likes", "sq_ghost")
#: Where the E2E classes run: the embedded engine, and the server of ``SURREALDB_PORT`` over WebSocket and HTTP.
_KINDS = ["mem", "ws", "http"]
_ALL_POSTS = {"p1", "p2", "p3", "p4", "p5"}
_PARIS = (2.35, 48.85)


@contextlib.asynccontextmanager
async def subquery_db(kind: str) -> AsyncIterator[Any]:
    """The ORM on *kind*: a fresh ``mem://`` engine, or the server of ``SURREALDB_PORT`` over ``ws`` or ``http``.

    On a server the tables of this module are removed before and after, and the connection settings are unset at
    the end (``connected()``); yields the SDK client.
    """
    if kind == "mem":
        async with embedded_database("mem://") as client:
            yield client
        return
    async with connected(_ws_url() if kind == "ws" else _http_url(), "db", *_TABLES) as client:
        yield client


async def _line_version(client: Any) -> tuple[int, int]:
    """The server's ``(major, minor)``; ``(3, 99)`` when its version cannot be read (as ``line_major`` reads 3)."""
    match = re.search(r"(\d+)\.(\d+)", str(await client.version()))
    return (int(match.group(1)), int(match.group(2))) if match else (3, 99)


async def _interactive_transactions(client: Any) -> bool:
    """Whether the server has interactive transactions (``begin()``: SurrealDB 3.x), as ``tests/test_transactions.py``
    probes it: ``begin()``, then ``cancel()``."""
    try:
        txn = await client.begin()
    except Exception:
        return False
    with contextlib.suppress(Exception):
        await client.cancel(txn)
    return True


def _person(key: str) -> RecordID:
    return RecordID("sq_person", key)


async def _seed() -> None:
    """Spec §9's data through the ORM's ``save()``: three persons, five posts (p5 without an author), two edges."""
    for key, name, active, age, nick in (
        ("ada", "Ada", True, 36, "x"),
        ("bob", "Bob", True, 50, "y"),
        ("cy", "Cy", False, 20, "z"),
    ):
        await SqPerson(id=key, name=name, active=active, age=age, nick=nick).save()
    for key, title, author, score, tags in (
        ("p1", "one", "ada", 5, ["x"]),
        ("p2", "two", "bob", 8, ["y", "z"]),
        ("p3", "three", "cy", 2, ["q"]),
        ("p4", "four", "ada", 9, []),
        ("p5", "five", None, 1, []),
    ):
        await SqPost(id=key, title=title, author=_person(author) if author else None, score=score, tags=tags).save()
    await SqLikes(in_=_person("ada"), out=RecordID("sq_post", "p2")).save()
    await SqLikes(in_=_person("cy"), out=RecordID("sq_post", "p1")).save()


def _ids(rows: Any) -> set[str]:
    return {row.id for row in rows}


def _inactive() -> Any:
    return SqPerson.objects().filter(active=False)


def _nobody() -> Any:
    """An inner queryset that matches nothing."""
    return SqPerson.objects().filter(name="Nobody")


def _no_score() -> Any:
    """An inner queryset of one field that matches nothing."""
    return SqPost.objects().filter(score__gt=100).select("score")


def _nicks(**criteria: Any) -> Any:
    return SqPerson.objects().filter(**criteria).select("nick")


# --- AC-5: reads, on every engine -------------------------------------------------------------------------


@pytest.mark.parametrize("kind", _KINDS)
class TestSubqueryReadsE2E:
    """AC-5: every read honours a subquery, in one request, on the embedded engine and on the server (WebSocket
    and HTTP)."""

    async def test_in_is_one_request(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with subquery_db(kind) as client:
            await _seed()
            calls = _record_queries(monkeypatch, client)
            assert _ids(await SqPost.objects().filter(author__in=Subquery(_active())).exec()) == {"p1", "p2", "p4"}
            assert len(calls) == 1
            assert calls[0][0].startswith("{ LET $_sq0 = "), calls[0][0]

    async def test_not_in_and_the_bare_queryset(self, kind: str) -> None:
        async with subquery_db(kind):
            await _seed()
            assert _ids(await SqPost.objects().filter(author__not_in=Subquery(_active())).exec()) == {"p3", "p5"}
            assert _ids(await SqPost.objects().filter(author__not_in=_active()).exec()) == {"p3", "p5"}
            assert _ids(await SqPost.objects().filter(author__in=_active()).exec()) == {"p1", "p2", "p4"}

    async def test_count_exists_first_and_get(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with subquery_db(kind) as client:
            await _seed()
            calls = _record_queries(monkeypatch, client)
            assert await SqPost.objects().filter(author__in=Subquery(_active())).count() == 3
            assert await SqPost.objects().filter(author__in=Subquery(_active())).exists() is True
            assert await SqPost.objects().filter(author__in=Subquery(_nobody())).exists() is False
            assert (await SqPost.objects().filter(author__in=Subquery(_active())).order_by("-score").first()).id == "p4"
            oldest_active = SqPerson.objects().filter(active=True).order_by("-age").limit(1)
            assert (await SqPost.objects().filter(author__in=Subquery(oldest_active)).get()).id == "p2"
            assert len(calls) == 5
            assert all(statement.startswith("{ LET $_sq0 = ") for statement, _ in calls)

    async def test_aggregations(self, kind: str) -> None:
        async with subquery_db(kind):
            await _seed()
            qs = SqPost.objects().filter(author__in=Subquery(_active()))
            assert await qs.sum("score") == 22
            assert await qs.avg("score") == pytest.approx(22 / 3)
            assert await qs.min("score") == 5
            assert await qs.max("score") == 9
            rows = await SqPost.objects().filter(author__in=Subquery(_active())).values("author").annotate(n=Count()).exec()
            assert sorted((str(row["author"]), row["n"]) for row in rows) == [("sq_person:ada", 2), ("sq_person:bob", 1)]

    async def test_nested(self, kind: str) -> None:
        """C6: the posts of the persons who wrote a post scoring more than 8."""
        async with subquery_db(kind):
            await _seed()
            writers = SqPost.objects().filter(score__gt=8).select("author")
            qs = SqPost.objects().filter(author__in=Subquery(SqPerson.objects().filter(id__in=Subquery(writers))))
            assert _ids(await qs.exec()) == {"p1", "p4"}

    async def test_q(self, kind: str) -> None:
        async with subquery_db(kind):
            await _seed()
            either = Q(author__in=Subquery(_inactive())) | Q(score__gte=9)
            assert _ids(await SqPost.objects().filter(either).exec()) == {"p3", "p4"}
            assert _ids(await SqPost.objects().filter(~Q(author__in=Subquery(_active()))).exec()) == {"p3", "p5"}

    async def test_collection_lookups_on_an_array_field(self, kind: str) -> None:
        async with subquery_db(kind):
            await _seed()
            posts = SqPost.objects
            assert _ids(await posts().filter(tags__containsany=Subquery(_nicks(active=True))).exec()) == {"p1", "p2"}
            assert _ids(await posts().filter(tags__containsall=Subquery(_nicks(name__in=["Bob", "Cy"]))).exec()) == {"p2"}
            assert _ids(await posts().filter(tags__contains=Subquery(_nicks(name="Cy"))).exec()) == {"p2"}
            assert _ids(await posts().filter(tags__not_contains=Subquery(_nicks(name="Cy"))).exec()) == {
                "p1",
                "p3",
                "p4",
                "p5",
            }

    async def test_first_value(self, kind: str) -> None:
        async with subquery_db(kind):
            await _seed()
            best = SqPost.objects().select("score").order_by("-score").limit(1)
            assert _ids(await SqPost.objects().filter(score=Subquery(best)).exec()) == {"p4"}

    async def test_an_empty_first_value_matches_no_record(self, kind: str) -> None:
        """Never every record (``x > NONE``), nor the records without the field (``author = NONE``: p5)."""
        async with subquery_db(kind):
            await _seed()
            assert await SqPost.objects().filter(score__gt=Subquery(_no_score())).exec() == []
            assert _ids(await SqPost.objects().filter(~Q(score__gt=Subquery(_no_score()))).exec()) == _ALL_POSTS
            assert await SqPost.objects().filter(title=Subquery(_no_score())).exec() == []
            assert await SqPost.objects().filter(author=Subquery(_nobody())).exec() == []

    async def test_the_id_column_outside(self, kind: str) -> None:
        async with subquery_db(kind):
            await _seed()
            writers = SqPost.objects().filter(score__gt=4).select("author")
            assert _ids(await SqPerson.objects().filter(id__in=Subquery(writers)).exec()) == {"ada", "bob"}

    async def test_an_edge_endpoint(self, kind: str) -> None:
        """C25: the edges whose ``in`` is an inactive person."""
        async with subquery_db(kind):
            await _seed()
            edges = await SqLikes.objects().filter(in___in=Subquery(_inactive())).exec()
            assert [(str(edge.in_), str(edge.out)) for edge in edges] == [("sq_person:cy", "sq_post:p1")]

    async def test_an_aliased_inner_column(self, kind: str) -> None:
        """C22: the inner ``select()`` and ``order_by()`` compile the column of an aliased field."""
        async with subquery_db(kind):
            await _seed()
            await SqAliased.model_validate({"id": "a1", "display_name": "one", "rank": 1}).save()
            await SqAliased.model_validate({"id": "a2", "display_name": "two", "rank": 2}).save()
            titles = SqAliased.objects().select("display").order_by("-rank")
            assert _ids(await SqPost.objects().filter(title__in=Subquery(titles)).exec()) == {"p1", "p2"}
            top = SqAliased.objects().select("display").order_by("-rank").limit(1)
            assert _ids(await SqPost.objects().filter(title__in=Subquery(top)).exec()) == {"p2"}

    async def test_an_inner_nearby(self, kind: str) -> None:
        async with subquery_db(kind):
            await _seed()
            await SqPost.objects().filter(title="four").bulk_update(spot=GeometryPoint(*_PARIS))
            near = SqPost.objects().nearby("spot", _PARIS, 1000).select("author")
            assert _ids(await SqPerson.objects().filter(id__in=Subquery(near)).exec()) == {"ada"}

    async def test_a_geo_distance_annotation(self, kind: str) -> None:
        async with subquery_db(kind):
            await _seed()
            await SqPost.objects().filter(title="four").bulk_update(spot=GeometryPoint(*_PARIS))
            qs = SqPost.objects().filter(author__in=Subquery(_active())).annotate(d=GeoDistance("spot", _PARIS))
            distances = {post.id: post.d for post in await qs.exec()}
            assert distances == {"p1": None, "p2": None, "p4": pytest.approx(0.0)}

    async def test_none_values_of_the_selected_field(self, kind: str) -> None:
        """M14, documented: the subquery holds p5's missing ``author`` as ``NONE``, which ``in`` matches."""
        async with subquery_db(kind):
            await _seed()
            authors = SqPost.objects().select("author")
            assert _ids(await SqPost.objects().filter(author__in=Subquery(authors)).exec()) == _ALL_POSTS


# --- AC-6: ordered and limited subqueries; surrealdb/surrealdb#7470 ------------------------------------------

_Q_AUTHORS = ("ada", "bob", "cy")
_RUNS = 12
_PIN = "SELECT VALUE id FROM sq_post WHERE author IN (SELECT id AS _sqv, age FROM sq_person ORDER BY age DESC LIMIT 1)._sqv;"
_PIN_FIXED = (
    "SurrealDB fixed surrealdb/surrealdb#7470: narrow this test's version range to the affected 3.x minors and rewrite "
    "the README 2.x/3.x row 'A subquery with order_by() and limit()'"
)


async def _seed_ordered() -> None:
    """Spec §9's persons and 30 posts ``q0…q29`` (no ``p`` post): authors ada, bob, cy in turn, ``score = i``."""
    for key, name, active, age in (("ada", "Ada", True, 36), ("bob", "Bob", True, 50), ("cy", "Cy", False, 20)):
        await SqPerson(id=key, name=name, active=active, age=age).save()
    await SqPost.objects().bulk_create(
        [SqPost(id=f"q{i}", title=f"q{i}", author=_person(_Q_AUTHORS[i % 3]), score=i) for i in range(30)]
    )


def _posts_of(author: str) -> set[str]:
    return {f"q{i}" for i in range(30) if _Q_AUTHORS[i % 3] == author}


class TestOrderedLimitedE2E:
    """AC-6: an ordered and limited subquery gives the right rows in 12 of 12 runs on every line, 3.2+ included,
    and the pin on the server's own inline form (surrealdb/surrealdb#7470)."""

    @pytest.mark.parametrize("kind", ["mem", "ws"])
    async def test_ordered_and_limited(self, kind: str) -> None:
        async with subquery_db(kind):
            await _seed_ordered()
            oldest = SqPerson.objects().order_by("-age").limit(1)
            second = SqPerson.objects().order_by("-age").limit(1).offset(1)
            youngest_active = SqPerson.objects().order_by("-active", "age").limit(1)
            for _ in range(_RUNS):
                assert _ids(await SqPost.objects().filter(author__in=Subquery(oldest)).exec()) == _posts_of("bob")
                assert _ids(await SqPost.objects().filter(author__in=Subquery(second)).exec()) == _posts_of("ada")
                assert _ids(await SqPost.objects().filter(author__in=Subquery(youngest_active)).exec()) == _posts_of("ada")

    @pytest.mark.parametrize("kind", ["mem", "ws"])
    async def test_ordered_first_value(self, kind: str) -> None:
        async with subquery_db(kind):
            await _seed_ordered()
            best = SqPost.objects().select("score").order_by("-score").limit(1)
            for _ in range(_RUNS):
                assert _ids(await SqPost.objects().filter(score=Subquery(best)).exec()) == {"q29"}

    async def test_7470_pin(self) -> None:
        """The raw inline form the ORM avoids: wrong rows on 3.2 and later, right on 2.x and 3.1.

        Fails on purpose when an upstream 3.x release fixes surrealdb/surrealdb#7470 (spec D16).
        """
        async with subquery_db("ws") as client:
            await _seed_ordered()
            counts = [len(await client.query(_PIN)) for _ in range(_RUNS)]
            if await _line_version(client) >= (3, 2):
                if min(counts) >= 10:
                    pytest.fail(_PIN_FIXED)
            else:
                assert counts == [10] * _RUNS


# --- AC-7 (reads): a subquery table that does not exist ------------------------------------------------------


def _ghost() -> Subquery:
    return Subquery(SqGhost.objects())


def _nested_ghost() -> Any:
    """A ghost two levels deep: the posts of the persons not in the (missing) ghost table — every authored post."""
    return SqPost.objects().filter(author__in=Subquery(SqPerson.objects().filter(id__not_in=_ghost())))


_EMPTY_LET = "LET $_sq0 = [];"


_PROBE_SQ_GHOST = (TABLE_PROBE, {"table": "sq_ghost"})


def _assert_probed_then_sent_again(calls: list[tuple[str, dict[str, Any]]]) -> None:
    """3.x: the statement, the probe that confirms ``sq_ghost`` does not exist, the statement with it empty."""
    assert len(calls) == 3, calls
    assert _EMPTY_LET not in calls[0][0]
    assert calls[1] == _PROBE_SQ_GHOST
    assert _EMPTY_LET in calls[2][0]


class TestMissingSubqueryTableReadsE2E:
    """AC-7 (reads): a subquery table that does not exist reads as empty on every line — in one request on 2.x
    and the embedded engine; on 3.x, whose server raises, the ORM asks the server whether the table exists (one
    probe) and, as it does not, sends the statement again with it empty."""

    async def test_3x_sends_the_statement_again_with_the_subquery_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with subquery_db("ws") as client:
            if await _line(client) < 3:
                pytest.skip("SurrealDB 3.x only: 2.x reads a missing subquery table as empty in one request")
            await _seed()
            calls = _record_queries(monkeypatch, client)
            assert _ids(await SqPost.objects().filter(author__not_in=_ghost()).exec()) == _ALL_POSTS
            _assert_probed_then_sent_again(calls)
            calls.clear()
            assert await SqPost.objects().filter(author__not_in=_ghost()).count() == 5
            _assert_probed_then_sent_again(calls)
            calls.clear()
            assert await SqPost.objects().filter(author__in=_ghost()).exec() == []
            _assert_probed_then_sent_again(calls)
            calls.clear()
            assert _ids(await _nested_ghost().exec()) == {"p1", "p2", "p3", "p4"}
            _assert_probed_then_sent_again(calls)

    @pytest.mark.parametrize("kind", ["mem", "ws"])
    async def test_2x_reads_it_as_empty_in_one_request(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with subquery_db(kind) as client:
            if await _line(client) >= 3:
                pytest.skip("SurrealDB 2.x and the embedded engine only: 3.x raises, and the ORM sends again")
            await _seed()
            calls = _record_queries(monkeypatch, client)
            assert _ids(await SqPost.objects().filter(author__not_in=_ghost()).exec()) == _ALL_POSTS
            assert await SqPost.objects().filter(author__not_in=_ghost()).count() == 5
            assert await SqPost.objects().filter(author__in=_ghost()).exec() == []
            assert _ids(await _nested_ghost().exec()) == {"p1", "p2", "p3", "p4"}
            assert len(calls) == 4
            assert not any(_EMPTY_LET in statement for statement, _ in calls)

    async def test_3x_the_outer_table_missing_as_the_subquery_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """M17: the ``LET`` fails first and names the table, which is a subquery's: sent again with it empty, where
        ``x IN []`` reads nothing and ``x NOT IN []`` names the outer table, which reads as empty."""
        async with subquery_db("ws") as client:
            if await _line(client) < 3:
                pytest.skip("SurrealDB 3.x only: 2.x reads both missing tables as empty in one request")
            calls = _record_queries(monkeypatch, client)
            for lookup in ("x__in", "x__not_in"):
                calls.clear()
                assert await SqGhost.objects().filter(**{lookup: _ghost()}).exec() == []
                _assert_probed_then_sent_again(calls)
                calls.clear()
                assert await SqGhost.objects().filter(**{lookup: _ghost()}).count() == 0
                _assert_probed_then_sent_again(calls)

    @pytest.mark.parametrize("kind", ["mem", "ws"])
    async def test_2x_the_outer_table_missing_as_the_subquery_table(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with subquery_db(kind) as client:
            if await _line(client) >= 3:
                pytest.skip("SurrealDB 2.x and the embedded engine only: 3.x raises, and the ORM sends again")
            calls = _record_queries(monkeypatch, client)
            for lookup in ("x__in", "x__not_in"):
                assert await SqGhost.objects().filter(**{lookup: _ghost()}).exec() == []
                assert await SqGhost.objects().filter(**{lookup: _ghost()}).count() == 0
            assert len(calls) == 4

    @pytest.mark.parametrize("kind", ["mem", "ws"])
    async def test_the_outer_table_missing_alone(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """The subquery reads a table that exists: the outer table reads as empty, as without a subquery, in one
        request on every line."""
        async with subquery_db(kind) as client:
            await _seed()
            calls = _record_queries(monkeypatch, client)
            assert await SqGhost.objects().filter(x__in=Subquery(SqPerson.objects())).exec() == []
            assert len(calls) == 1
            assert await SqGhost.objects().filter(x__in=Subquery(SqPerson.objects())).count() == 0
            assert len(calls) == 2

    async def test_3x_in_an_interactive_transaction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """M9: the probe and the read sent again run inside the transaction, which still writes and commits."""
        async with subquery_db("ws") as client:
            if not await _interactive_transactions(client):
                pytest.skip("SurrealDB 3.x only: 2.x has no interactive transaction (begin())")
            await _seed()
            calls = _record_queries(monkeypatch, client)
            async with SurrealDBConnectionManager.transaction() as tx:
                rows = await SqPost.objects(tx=tx).filter(author__not_in=_ghost()).exec()
                assert _ids(rows) == _ALL_POSTS
                _assert_probed_then_sent_again(calls)
                await SqPerson(id="dan", name="Dan", age=40).save(tx=tx)
            assert (await SqPerson.objects().get("dan")).name == "Dan"


# --- an error quoting a stored value that reads like the missing-table error ---------------------------------

#: What makes reading ``sq_person.boom`` fail on a stored ``nick``: a ``THROW`` of it, or a cast that quotes it.
_FAILING_READS = {"throw": "{ THROW nick }", "cast": "{ <int> nick }"}


async def _boom_quotes_the_sentence(client: Any, how: str) -> None:
    """Give every person the nick :data:`_SENTENCE` ("The table 'sq_person' does not exist", naming the subquery
    table) and a field ``boom`` computed when it is read, which fails quoting that stored value (*how*).

    3.x computes a ``COMPUTED`` field on every read; 2.x and the embedded engine evaluate a ``<future>`` on output,
    once a write has stored it (so the field is defined before the update there, after it on 3.x).
    """
    expression = _FAILING_READS[how]
    if await _line(client) >= 3:
        await client.query("UPDATE sq_person SET nick = $nick RETURN NONE;", {"nick": _SENTENCE})
        await client.query(f"DEFINE FIELD boom ON sq_person COMPUTED {expression};")
    else:
        await client.query(f"DEFINE FIELD boom ON sq_person VALUE <future> {expression};")
        await client.query("UPDATE sq_person SET nick = $nick RETURN NONE;", {"nick": _SENTENCE})


def _quoting_subquery() -> Any:
    """The posts whose author is not one of the persons' ``boom`` values: reading them fails on every line."""
    return SqPost.objects().filter(author__not_in=Subquery(SqPerson.objects().select("boom")))


async def _assert_relayed(read: Awaitable[Any], calls: list[tuple[str, dict[str, Any]]]) -> None:
    """*read* raises the server's error quoting the sentence — it never reads the subquery as empty, which would make
    ``author__not_in`` match every post — and no statement binds the subquery to ``[]``."""
    try:
        rows = await read
    except ServerError as exc:
        assert _SENTENCE in str(exc)
    else:
        pytest.fail(f"the subquery was read as empty: {rows!r}")
    assert not any(_EMPTY_LET in statement for statement, _ in calls), calls


@pytest.mark.parametrize("how", sorted(_FAILING_READS))
class TestAnErrorQuotingTheSentenceE2E:
    """A server error raised while the subquery runs, quoting a stored value that reads "The table '<subquery
    table>' does not exist", is relayed on every line: the ORM never reads that subquery as empty. On 3.2 and later
    a ``THROW`` gives the very text of 3.1's missing-table error; the table exists, so it is still relayed."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_relayed_not_read_as_empty(self, kind: str, how: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with subquery_db(kind) as client:
            await _seed()
            await _boom_quotes_the_sentence(client, how)
            calls = _record_queries(monkeypatch, client)
            await _assert_relayed(_quoting_subquery().exec(), calls)
            await _assert_relayed(_quoting_subquery().count(), calls)
            await _assert_relayed(_quoting_subquery().exists(), calls)

    async def test_3x_in_an_interactive_transaction(self, how: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with subquery_db("ws") as client:
            if not await _interactive_transactions(client):
                pytest.skip("SurrealDB 3.x only: 2.x has no interactive transaction (begin())")
            await _seed()
            await _boom_quotes_the_sentence(client, how)
            calls = _record_queries(monkeypatch, client)
            async with SurrealDBConnectionManager.transaction() as tx:
                inner = SqPerson.objects().select("boom")
                await _assert_relayed(SqPost.objects(tx=tx).filter(author__not_in=Subquery(inner)).exec(), calls)


@pytest.mark.parametrize("kind", ["ws", "http"])
async def test_3_2_and_later_a_throw_reading_like_3_1s_error_is_relayed_once_the_server_says_the_table_exists(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """From 3.2 on a ``THROW`` inside a block is a ``ThrownError`` "An error occurred: The table 'sq_person' does
    not exist", the very text of 3.1's missing-table error: the ORM asks the server, which answers that
    ``sq_person`` exists, and relays the error (3.1 and 2.x: other texts, relayed without asking)."""
    async with subquery_db(kind) as client:
        if await _line_version(client) < (3, 2):
            pytest.skip("SurrealDB 3.2 and later only: 3.1 wraps a THROW inside a block twice, 2.x has no ThrownError")
        await _seed()
        await _boom_quotes_the_sentence(client, "throw")
        calls = _record_queries(monkeypatch, client)
        with pytest.raises(ThrownError) as info:
            await _quoting_subquery().exec()
        assert str(info.value) == _THROWN + _SENTENCE
        assert len(calls) == 2, calls
        assert calls[1] == (TABLE_PROBE, {"table": "sq_person"})


# --- the 3.x re-send, without a server ---------------------------------------------------------------------

#: What a fake answers the table probe when no probe is expected: the test fails if one is sent.
_NO_PROBE = AssertionError("no table probe expected")


class _RaisingClient:
    """A client whose ``query`` raises *error*, except for :data:`TABLE_PROBE`, answered with *probe* (raised when
    it is an exception, returned otherwise); it records every ``(statement, variables)``."""

    def __init__(self, error: Exception, probe: Any = _NO_PROBE) -> None:
        self.error = error
        self.probe = probe
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query(self, query: str, variables: dict[str, Any] | None = None) -> Any:
        self.calls.append((query, dict(variables or {})))
        if query != TABLE_PROBE:
            raise self.error
        if isinstance(self.probe, BaseException):
            raise self.probe
        return self.probe


class _RaisingTx(_RaisingClient):
    """A transaction whose ``run_read`` answers as :class:`_RaisingClient` does (the pattern of
    ``tests/test_transactions.py::_FakeTx``)."""

    @property
    def is_interactive(self) -> bool:
        return True

    async def run_read(self, sql: str, variables: dict[str, Any] | None = None) -> Any:
        return await self.query(sql, variables)

    async def add(self, sql: str, variables: dict[str, Any] | None = None) -> Any:
        raise AssertionError("no write expected")


def _raising_queryset(
    branch: str, monkeypatch: pytest.MonkeyPatch, error: Exception, probe: Any = _NO_PROBE
) -> tuple[QuerySet, list[tuple[str, dict[str, Any]]]]:
    """``SqPost.objects()`` whose reads raise *error* — through the client, or through a transaction — and the
    ``(statement, variables)`` it sends; the table probe is answered with *probe*."""
    if branch == "tx":
        tx = _RaisingTx(error, probe)
        return SqPost.objects(tx=tx), tx.calls
    client = _RaisingClient(error, probe)

    async def _client(*args: Any, **kwargs: Any) -> Any:
        return client

    monkeypatch.setattr(SurrealDBConnectionManager, "get_client", _client)
    return SqPost.objects(), client.calls


_SELECT = "SELECT * FROM sq_post;"
_READS_SQ_PERSON = frozenset({"sq_person"})
_PROBE_SQ_PERSON = (TABLE_PROBE, {"table": "sq_person"})
#: The forms the server says a subquery table is missing with: 3.x's ``NotFoundError``, 3.1's in a block.
_MISSING_SQ_PERSON = [_not_found(), ThrownError("Thrown", _THROWN + _SENTENCE)]


@pytest.mark.parametrize("branch", ["client", "tx"])
class TestExecuteQueryNotFound:
    """AC-7 (rev. 3, P2): what ``_execute_query`` makes of a server error, on the client and the transaction
    branches, while a ``LET`` reads subquery tables (``subquery_tables``) and without."""

    async def test_a_not_found_naming_no_table_is_relayed_while_a_let_reads_subquery_tables(
        self, branch: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        error = NotFoundError("NotFound", "nope")
        qs, calls = _raising_queryset(branch, monkeypatch, error)
        with pytest.raises(NotFoundError) as info:
            await qs._execute_query(_SELECT, {}, subquery_tables=_READS_SQ_PERSON)
        assert info.value is error
        assert calls == [(_SELECT, {})]

    async def test_without_subquery_tables_a_not_found_reads_as_empty(
        self, branch: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        qs, calls = _raising_queryset(branch, monkeypatch, NotFoundError("NotFound", "nope"))
        assert await qs._execute_query(_SELECT, {}, subquery_tables=frozenset()) == []
        assert await qs._execute_query(_SELECT, {}) == []
        assert calls == [(_SELECT, {}), (_SELECT, {})]

    async def test_the_outer_table_missing_reads_as_empty(self, branch: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """A ``NotFoundError`` naming a table no ``LET`` reads is the outer table's: no rows, without a probe."""
        error = _not_found("The table 'sq_post' does not exist", "sq_post")
        qs, calls = _raising_queryset(branch, monkeypatch, error)
        assert await qs._execute_query(_SELECT, {}, subquery_tables=_READS_SQ_PERSON) == []
        assert calls == [(_SELECT, {})]

    @pytest.mark.parametrize("error", _MISSING_SQ_PERSON, ids=["NotFoundError", "ThrownError"])
    async def test_a_missing_subquery_table_the_server_confirms_asks_for_the_statement_again(
        self, branch: str, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        qs, calls = _raising_queryset(branch, monkeypatch, error, probe=_not_found())
        with pytest.raises(_MissingSubqueryTable) as info:
            await qs._execute_query(_SELECT, {}, subquery_tables=_READS_SQ_PERSON)
        assert info.value.table == "sq_person"
        assert info.value.__cause__ is error
        assert calls == [(_SELECT, {}), _PROBE_SQ_PERSON]

    @pytest.mark.parametrize("error", _MISSING_SQ_PERSON, ids=["NotFoundError", "ThrownError"])
    @pytest.mark.parametrize(
        "probe",
        [
            [],
            [RecordID("sq_person", "ada")],
            None,
            InternalError("Internal", "boom"),
            ThrownError("Thrown", _THROWN + _SENTENCE),
            _not_found("The table 'sq_post' does not exist", "sq_post"),
            _not_found(table=None),
            _not_found(f"Error: {_SENTENCE}"),
        ],
        ids=[
            "no-row",
            "a-row",
            "none",
            "another-error",
            "a-thrown-error",
            "another-table",
            "not-found-without-details",
            "not-found-quoting-it",
        ],
    )
    async def test_a_subquery_table_the_server_does_not_confirm_missing_is_relayed(
        self, branch: str, monkeypatch: pytest.MonkeyPatch, error: Exception, probe: Any
    ) -> None:
        """The table exists (a row, or none), or the probe's answer is not the server's own missing-table error
        for that table: the statement's error is relayed unchanged, never read as an empty subquery. On 3.2 and
        later a user ``THROW`` gives the very text of 3.1's ``ThrownError``."""
        qs, calls = _raising_queryset(branch, monkeypatch, error, probe=probe)
        with pytest.raises(type(error)) as info:
            await qs._execute_query(_SELECT, {}, subquery_tables=_READS_SQ_PERSON)
        assert info.value is error
        assert calls == [(_SELECT, {}), _PROBE_SQ_PERSON]

    @pytest.mark.parametrize(
        "error",
        [
            InternalError("Internal", f'Could not cast into `int` using input `"{_SENTENCE}"`'),
            InternalError("Internal", _THROWN + _SENTENCE),
            ThrownError("Thrown", _THROWN + _THROWN + _SENTENCE),
            ThrownError("Thrown", f'{_THROWN}Could not cast into `int` using input `"{_SENTENCE}"`'),
            ThrownError("Thrown", _THROWN + "The table 'sq_post' does not exist"),
            _not_found(f"{_SENTENCE}."),
        ],
        ids=["3.x-cast", "2.x-throw", "3.1-throw-in-a-block", "3.1-cast", "3.1-thrown-outer-table", "trailing-text"],
    )
    async def test_an_error_quoting_the_sentence_is_relayed_without_a_probe(
        self, branch: str, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        qs, calls = _raising_queryset(branch, monkeypatch, error)
        with pytest.raises(type(error)) as info:
            await qs._execute_query(_SELECT, {}, subquery_tables=_READS_SQ_PERSON)
        assert info.value is error
        assert calls == [(_SELECT, {})]

    async def test_a_probe_that_cannot_reach_the_server_raises_its_own_error(
        self, branch: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only a server answer is read: a probe that fails otherwise (here, a lost connection) raises its error."""
        lost = ConnectionError("lost")
        qs, _ = _raising_queryset(branch, monkeypatch, _not_found(), probe=lost)
        with pytest.raises(ConnectionError) as info:
            await qs._execute_query(_SELECT, {}, subquery_tables=_READS_SQ_PERSON)
        assert info.value is lost

    async def test_without_subquery_tables_a_thrown_error_is_relayed(
        self, branch: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without subquery tables the 3.1 ``ThrownError`` stays the server's error, as today."""
        error = ThrownError("Thrown", _THROWN + _SENTENCE)
        qs, calls = _raising_queryset(branch, monkeypatch, error)
        with pytest.raises(ThrownError) as info:
            await qs._execute_query(_SELECT, {})
        assert info.value is error
        assert calls == [(_SELECT, {})]

    async def test_another_server_error_is_relayed(self, branch: str, monkeypatch: pytest.MonkeyPatch) -> None:
        error = ServerError("Internal", "boom")
        qs, calls = _raising_queryset(branch, monkeypatch, error)
        with pytest.raises(ServerError) as info:
            await qs._execute_query(_SELECT, {}, subquery_tables=_READS_SQ_PERSON)
        assert info.value is error
        assert calls == [(_SELECT, {})]


def _recording_execute(
    monkeypatch: pytest.MonkeyPatch, qs: QuerySet, answer: Any = None
) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    """Replace *qs*'s ``_execute_query`` with a fake recording ``(args, kwargs)``: it raises ``_MissingSubqueryTable``
    for the greatest table of a non-empty ``subquery_tables``, else returns *answer* (``[]`` by default)."""
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def _execute(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        tables = kwargs.get("subquery_tables", frozenset())
        if tables:
            raise _MissingSubqueryTable(max(tables))
        return [] if answer is None else answer

    monkeypatch.setattr(qs, "_execute_query", _execute)
    return calls


class TestRunCompiled:
    """The read loop (spec §6.3 ``_run_compiled``), without a server: one re-send per missing subquery table, and
    today's two-argument call once no ``LET`` reads a table."""

    async def test_one_resend_per_missing_subquery_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        writers = SqPost.objects().filter(score__gt=8).select("author")
        qs = SqPost.objects().filter(author__in=Subquery(SqPerson.objects().filter(id__in=Subquery(writers))))
        calls = _recording_execute(monkeypatch, qs)
        assert await qs.exec() == []
        assert len(calls) == 3
        assert calls[0][1] == {"subquery_tables": frozenset({"sq_person", "sq_post"})}
        assert calls[1] == (
            (
                "{ LET $_sq0 = []; LET $_sq1 = (SELECT VALUE id FROM sq_person WHERE id IN $_sq0); "
                "(SELECT * FROM sq_post WHERE author IN $_sq1) };",
                {},
            ),
            {"subquery_tables": frozenset({"sq_person"})},
        )
        assert calls[2] == (("{ LET $_sq0 = []; (SELECT * FROM sq_post WHERE author IN $_sq0) };", {}), {})

    @pytest.mark.parametrize(
        ("read", "statement"),
        [
            (lambda qs: qs.exec(), "SELECT * FROM sq_post WHERE score > $_f0;"),
            (lambda qs: qs.exists(), "SELECT * FROM sq_post WHERE score > $_f0 LIMIT 1;"),
            (lambda qs: qs.count(), "SELECT count() FROM sq_post WHERE score > $_f0 GROUP ALL;"),
            (lambda qs: qs.sum("score"), "SELECT math::sum(score) AS sum FROM sq_post WHERE score > $_f0 GROUP ALL;"),
            (lambda qs: qs.avg("score"), "SELECT math::mean(score) AS avg FROM sq_post WHERE score > $_f0 GROUP ALL;"),
            (lambda qs: qs.min("score"), "SELECT math::min(score) AS min FROM sq_post WHERE score > $_f0 GROUP ALL;"),
            (lambda qs: qs.max("score"), "SELECT math::max(score) AS max FROM sq_post WHERE score > $_f0 GROUP ALL;"),
        ],
        ids=["exec", "exists", "count", "sum", "avg", "min", "max"],
    )
    async def test_without_a_subquery_the_call_is_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, read: Callable[[Any], Any], statement: str
    ) -> None:
        qs = SqPost.objects().filter(score__gt=5)
        calls = _recording_execute(monkeypatch, qs)
        await read(qs)
        assert calls == [((statement, {"_f0": 5}), {})]

    @pytest.mark.parametrize(
        ("read", "statement"),
        [
            (lambda qs: qs.exec(), "SELECT * FROM sq_post WHERE author IN $_sq0"),
            (lambda qs: qs.exists(), "SELECT * FROM sq_post WHERE author IN $_sq0 LIMIT 1"),
            (lambda qs: qs.count(), "SELECT count() FROM sq_post WHERE author IN $_sq0 GROUP ALL"),
            (lambda qs: qs.sum("score"), "SELECT math::sum(score) AS sum FROM sq_post WHERE author IN $_sq0 GROUP ALL"),
            (lambda qs: qs.avg("score"), "SELECT math::mean(score) AS avg FROM sq_post WHERE author IN $_sq0 GROUP ALL"),
            (lambda qs: qs.min("score"), "SELECT math::min(score) AS min FROM sq_post WHERE author IN $_sq0 GROUP ALL"),
            (lambda qs: qs.max("score"), "SELECT math::max(score) AS max FROM sq_post WHERE author IN $_sq0 GROUP ALL"),
            (
                lambda qs: qs.values("author").annotate(n=Count()).exec(),
                "SELECT author, count() AS n FROM sq_post WHERE author IN $_sq0 GROUP BY author",
            ),
            (
                lambda qs: qs.annotate(d=GeoDistance("spot", _PARIS)).exec(),
                "SELECT *, IF spot.is_point() THEN geo::distance(spot, $_gd0) END AS d FROM sq_post WHERE author IN $_sq0",
            ),
        ],
        ids=["exec", "exists", "count", "sum", "avg", "min", "max", "values-annotate", "geo-distance"],
    )
    async def test_every_read_sends_again_with_the_subquery_empty(
        self, monkeypatch: pytest.MonkeyPatch, read: Callable[[Any], Any], statement: str
    ) -> None:
        qs = _with_inner(_active())
        calls = _recording_execute(monkeypatch, qs)
        await read(qs)
        assert [(args[0], kwargs) for args, kwargs in calls] == [
            (f"{{ {_C1_LET} ({statement}) }};", {"subquery_tables": _READS_SQ_PERSON}),
            (f"{{ LET $_sq0 = []; ({statement}) }};", {}),
        ]

    async def test_the_limit_of_exists_is_restored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        qs = _with_inner(_active()).limit(7)
        _recording_execute(monkeypatch, qs)
        assert await qs.exists() is False
        assert qs._limit == 7


# --- AC-8 and AC-7 (writes): writes by filter, the get_or_create lookups, transactions ---------------------------

_C17 = (
    "{ LET $_sq0 = (SELECT VALUE id FROM sq_person WHERE active = $_f0); "
    "(UPDATE sq_post SET score = $_v0 WHERE author IN $_sq0) };"
)
_C18 = "{ LET $_sq0 = (SELECT VALUE id FROM sq_person WHERE active = $_f0); (DELETE sq_post WHERE author IN $_sq0 RETURN BEFORE) };"
_C19 = (
    "{ LET $_sq0 = (SELECT VALUE id FROM sq_person WHERE active = $_f0); (UPDATE sq_post PATCH $_ops WHERE author IN $_sq0) };"
)
_C20 = "{ LET $_sq0 = (SELECT VALUE id FROM sq_person WHERE active = $_f0); (SELECT * FROM sq_post WHERE author IN $_sq0 LIMIT 2) };"
_RETITLE = [{"op": "replace", "path": "/title", "value": "x"}]
_SEEDED_SCORES = {"p1": 5, "p2": 8, "p3": 2, "p4": 9, "p5": 1}


async def _scores() -> dict[str, int]:
    """Every post's score, by id."""
    return {post.id: post.score for post in await SqPost.objects().exec()}


class TestSubqueryWriteStatementsE2E:
    """AC-2 (C17–C20): what the writes by filter and the ``get_or_create`` lookup send, recorded from the embedded
    engine's client — one block per call, exactly, with its variables."""

    async def test_c17_bulk_update(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with subquery_db("mem") as client:
            await _seed()
            calls = _record_queries(monkeypatch, client)
            assert await _with_inner(_inactive()).bulk_update(score=0) == 1
            assert calls == [(_C17, {"_f0": False, "_v0": 0})]

    async def test_c18_bulk_delete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with subquery_db("mem") as client:
            await _seed()
            calls = _record_queries(monkeypatch, client)
            assert await _with_inner(_inactive()).bulk_delete() == 1
            assert calls == [(_C18, {"_f0": False})]

    async def test_c19_patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with subquery_db("mem") as client:
            await _seed()
            calls = _record_queries(monkeypatch, client)
            assert await _with_inner(_active()).patch(_RETITLE) == 3
            assert calls == [(_C19, {"_f0": True, "_ops": _RETITLE})]

    async def test_c20_get_or_create_lookup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with subquery_db("mem") as client:
            await _seed()
            calls = _record_queries(monkeypatch, client)
            post, created = await SqPost.objects().get_or_create(author__in=Subquery(_inactive()), defaults={"title": "x"})
            assert (post.id, created) == ("p3", False)
            assert calls == [(_C20, {"_f0": False})]

    async def test_without_a_subquery_the_writes_send_what_they_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AC-3 for the writes: no block, and the variables of v0.26.0 in their order."""
        async with subquery_db("mem") as client:
            await _seed()
            calls = _record_queries(monkeypatch, client)
            qs = SqPost.objects().variables(a=1).filter(score__gt=5)
            assert await qs.bulk_update(title="t") == 2
            assert await SqPost.objects().filter(score__gt=8).patch(_RETITLE) == 1
            assert await SqPost.objects().filter(score__lt=2).bulk_delete() == 1
            await SqPost.objects().get_or_create(title="two")
            assert calls == [
                ("UPDATE sq_post SET title = $_v0 WHERE score > $_f0;", {"a": 1, "_f0": 5, "_v0": "t"}),
                ("UPDATE sq_post PATCH $_ops WHERE score > $_f0;", {"_f0": 8, "_ops": _RETITLE}),
                ("DELETE sq_post WHERE score < $_f0 RETURN BEFORE;", {"_f0": 2}),
                ("SELECT * FROM sq_post WHERE title = $_f0 LIMIT 2;", {"_f0": "two"}),
            ]
            assert [list(variables) for _, variables in calls[:1]] == [["a", "_f0", "_v0"]]


@pytest.mark.parametrize("kind", _KINDS)
class TestSubqueryWritesE2E:
    """AC-8 (writes and helpers): the writes by filter and the ``get_or_create``/``update_or_create`` lookups honour
    a subquery, on the embedded engine and on the server (WebSocket and HTTP)."""

    async def test_bulk_update(self, kind: str) -> None:
        async with subquery_db(kind):
            await _seed()
            assert await _with_inner(_inactive()).bulk_update(score=0) == 1
            assert await _scores() == {**_SEEDED_SCORES, "p3": 0}

    async def test_bulk_delete(self, kind: str) -> None:
        async with subquery_db(kind):
            await _seed()
            assert await _with_inner(_inactive()).bulk_delete() == 1
            assert _ids(await SqPost.objects().exec()) == _ALL_POSTS - {"p3"}

    async def test_patch(self, kind: str) -> None:
        async with subquery_db(kind):
            await _seed()
            assert await _with_inner(_active()).patch(_RETITLE) == 3
            assert _ids(await SqPost.objects().filter(title="x").exec()) == {"p1", "p2", "p4"}

    async def test_get_or_create_finds_through_a_subquery(self, kind: str) -> None:
        async with subquery_db(kind):
            await _seed()
            post, created = await SqPost.objects().get_or_create(author__in=_active(), title="one", defaults={"score": 7})
            assert (post.id, created, post.score) == ("p1", False, 5)
            assert await _scores() == _SEEDED_SCORES

    async def test_get_or_create_creates_without_writing_the_subquery(self, kind: str) -> None:
        async with subquery_db(kind) as client:
            await _seed()
            post, created = await SqPost.objects().get_or_create(author__in=Subquery(_nobody()), title="new")
            assert created is True
            assert (post.title, post.author) == ("new", None)
            rows = await client.query("SELECT * FROM sq_post WHERE title = 'new';")
            assert len(rows) == 1
            assert "author" not in rows[0], rows

    async def test_update_or_create_updates_through_a_subquery(self, kind: str) -> None:
        async with subquery_db(kind):
            await _seed()
            post, created = await SqPost.objects().update_or_create(author__in=_active(), title="one", defaults={"score": 6})
            assert (created, post.title, post.score) == (False, "one", 6)
            assert await _scores() == {**_SEEDED_SCORES, "p1": 6}
            stored = await SqPost.objects().get("p1")
            assert stored.author == _person("ada")


class TestSubqueryTransactionsE2E:
    """AC-8 (transactions) and AC-7 (writes in a transaction): the block runs in the transaction — interactive on
    SurrealDB 3.x over WebSocket, buffered over HTTP on both lines and on the embedded engine."""

    async def test_3x_interactive_reads_and_writes_see_the_transaction(self) -> None:
        async with subquery_db("ws") as client:
            if not await _interactive_transactions(client):
                pytest.skip("SurrealDB 3.x only: 2.x has no interactive transaction (begin())")
            await _seed()
            async with SurrealDBConnectionManager.transaction() as tx:
                assert tx.is_interactive
                await SqPerson(id="dan", name="Dan", active=True, age=40).save(tx=tx)
                await SqPost(id="p6", title="six", author=_person("dan"), score=6).save(tx=tx)
                rows = await SqPost.objects(tx=tx).filter(author__in=_active()).exec()
                assert _ids(rows) == {"p1", "p2", "p4", "p6"}
                assert await SqPost.objects(tx=tx).filter(author__in=_active()).bulk_update(score=3) == 4
            assert await _scores() == {"p1": 3, "p2": 3, "p3": 2, "p4": 3, "p5": 1, "p6": 3}

    @pytest.mark.parametrize("kind", ["mem", "http"])
    async def test_buffered_bulk_delete_applies_at_commit(self, kind: str) -> None:
        async with subquery_db(kind):
            await _seed()
            async with SurrealDBConnectionManager.transaction() as tx:
                assert not tx.is_interactive
                assert await SqPost.objects(tx=tx).filter(author__in=_inactive()).bulk_delete() == 0
                assert "p3" in _ids(await SqPost.objects().exec())
            assert _ids(await SqPost.objects().exec()) == _ALL_POSTS - {"p3"}

    @pytest.mark.parametrize("kind", ["mem", "http"])
    async def test_buffered_two_blocks_in_one_batch(self, kind: str) -> None:
        """M8 (rev. 2): two blocks binding the same ``$_sq0`` in one ``BEGIN … COMMIT`` batch both apply."""
        async with subquery_db(kind):
            await _seed()
            async with SurrealDBConnectionManager.transaction() as tx:
                assert await SqPost.objects(tx=tx).filter(author__in=_active()).bulk_update(score=5) == 0
                assert await SqPost.objects(tx=tx).filter(author__in=_inactive()).bulk_delete() == 0
                assert [statement.startswith("{ LET $_sq0 = ") for statement in tx.statements] == [True, True]
            assert await _scores() == {"p1": 5, "p2": 5, "p4": 5, "p5": 1}

    async def test_3x_a_missing_subquery_table_fails_an_interactive_write(self) -> None:
        async with subquery_db("ws") as client:
            if not await _interactive_transactions(client):
                pytest.skip("SurrealDB 3.x only: 2.x has no interactive transaction (begin())")
            await _seed()
            with pytest.raises(SurrealDbError, match="does not exist"):
                async with SurrealDBConnectionManager.transaction() as tx:
                    await SqPost.objects(tx=tx).filter(author__not_in=_ghost()).bulk_update(score=1)
            assert await _scores() == _SEEDED_SCORES

    async def test_3x_a_missing_subquery_table_fails_a_buffered_commit(self) -> None:
        async with subquery_db("http") as client:
            if await _line(client) < 3:
                pytest.skip("SurrealDB 3.x only: 2.x commits, reading the missing table as empty")
            await _seed()
            with pytest.raises(SurrealDbError, match="does not exist"):
                async with SurrealDBConnectionManager.transaction() as tx:
                    assert await SqPost.objects(tx=tx).filter(author__not_in=_ghost()).bulk_update(score=1) == 0
            assert await _scores() == _SEEDED_SCORES

    @pytest.mark.parametrize("kind", ["mem", "http"])
    async def test_2x_a_missing_subquery_table_reads_as_empty_in_a_buffered_commit(self, kind: str) -> None:
        async with subquery_db(kind) as client:
            if await _line(client) >= 3:
                pytest.skip("SurrealDB 2.x and the embedded engine only: on 3.x the commit fails")
            await _seed()
            async with SurrealDBConnectionManager.transaction() as tx:
                assert await SqPost.objects(tx=tx).filter(author__not_in=_ghost()).bulk_update(score=1) == 0
            assert await _scores() == dict.fromkeys(_ALL_POSTS, 1)


async def _assert_every_post_scored_one_and_one_created() -> None:
    """After the ghost writes: the five posts updated to 1, none deleted or retitled, and the post ``get_or_create``
    created (score 0)."""
    scores = await _scores()
    assert {key: scores.pop(key) for key in sorted(_ALL_POSTS)} == dict.fromkeys(_ALL_POSTS, 1)
    assert list(scores.values()) == [0]
    assert await SqPost.objects().filter(title="x").count() == 0


class TestMissingSubqueryTableWritesE2E:
    """AC-7 (writes outside a transaction): a write by filter or a ``get_or_create`` lookup whose subquery reads a
    table that does not exist reads it as empty — in one request on 2.x and the embedded engine; on 3.x after the
    server confirms the table is missing, sent again with the subquery empty (nothing was written: the ``LET``
    fails before the statement runs)."""

    async def test_3x_sends_the_write_again_with_the_subquery_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with subquery_db("ws") as client:
            if await _line(client) < 3:
                pytest.skip("SurrealDB 3.x only: 2.x reads a missing subquery table as empty in one request")
            await _seed()
            calls = _record_queries(monkeypatch, client)
            assert await SqPost.objects().filter(author__not_in=_ghost()).bulk_update(score=1) == 5
            _assert_probed_then_sent_again(calls)
            calls.clear()
            assert await SqPost.objects().filter(author__in=_ghost()).bulk_delete() == 0
            _assert_probed_then_sent_again(calls)
            calls.clear()
            assert await SqPost.objects().filter(author__in=_ghost()).patch(_RETITLE) == 0
            _assert_probed_then_sent_again(calls)
            calls.clear()
            _, created = await SqPost.objects().get_or_create(author__in=_ghost(), title="g")
            assert created is True
            assert calls[:3] == [
                (
                    "{ LET $_sq0 = (SELECT VALUE id FROM sq_ghost); "
                    "(SELECT * FROM sq_post WHERE author IN $_sq0 AND title = $_f0 LIMIT 2) };",
                    {"_f0": "g"},
                ),
                _PROBE_SQ_GHOST,
                ("{ LET $_sq0 = []; (SELECT * FROM sq_post WHERE author IN $_sq0 AND title = $_f0 LIMIT 2) };", {"_f0": "g"}),
            ]
            await _assert_every_post_scored_one_and_one_created()

    @pytest.mark.parametrize("kind", ["mem", "ws"])
    async def test_2x_reads_it_as_empty_in_one_request(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with subquery_db(kind) as client:
            if await _line(client) >= 3:
                pytest.skip("SurrealDB 2.x and the embedded engine only: 3.x raises, and the ORM sends again")
            await _seed()
            calls = _record_queries(monkeypatch, client)
            assert await SqPost.objects().filter(author__not_in=_ghost()).bulk_update(score=1) == 5
            assert await SqPost.objects().filter(author__in=_ghost()).bulk_delete() == 0
            assert await SqPost.objects().filter(author__in=_ghost()).patch(_RETITLE) == 0
            assert len(calls) == 3
            _, created = await SqPost.objects().get_or_create(author__in=_ghost(), title="g")
            assert created is True
            assert not any(_EMPTY_LET in statement for statement, _ in calls), calls
            await _assert_every_post_scored_one_and_one_created()
