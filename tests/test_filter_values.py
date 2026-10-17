"""Filter values: a ``str`` is data, whatever it starts with; ``Var("name")`` is the only variable reference.

``filter(token="$x")`` compares ``token`` with the string ``"$x"``, bound like any other value, on every filter
path: ``filter()``, ``Q`` (``|``, ``&``, ``~``), ``get()``, ``count()``, ``exists()``, ``get_or_create()``,
``update_or_create()``, ``bulk_update()`` by filter, ``cache()``, a subquery and a live query (the value written as
the string literal ``'$x'``). ``filter(token=Var("x"))`` reads the variable ``$x`` given to ``variables()``.

The ``"$$"`` escape of v0.14.3 keeps its meaning for one more cycle: a scalar value starting with ``"$$"`` is the
same value with one ``$`` less (``"$$x"`` is the string ``"$x"``), with a ``DeprecationWarning`` whose text is the
same whatever the value. A scalar value that is exactly ``$`` and the name of a variable the statement's
``variables()`` give — ``filter(age__lte="$max_age").variables(max_age=25)``, which read ``$max_age`` until v0.28.1
— is still the string, with a ``UserWarning`` naming ``Var("max_age")``. Each warning is given once per read, at
the caller's line, ``cache()`` and a re-sent statement included. Unit tests first (no engine, no server); the ``E2E``
classes run on the embedded ``mem://`` engine and on the server of ``SURREALDB_PORT`` (both lines in CI), the live
query on the server only.
"""

import asyncio
import contextlib
import warnings
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import pytest
from surrealdb import RecordID

from surreal_orm_lite import (
    BaseSurrealEdge,
    BaseSurrealModel,
    Count,
    Q,
    QueryCache,
    Subquery,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    Var,
)
from surreal_orm_lite.constants import LOOKUP_OPERATORS
from surreal_orm_lite.testing import embedded_database
from surreal_orm_lite.utils import build_filter_condition
from tests.conftest import orm_client
from tests.test_embedded import _normalised, _readme_part, _subsection, _table_rows
from tests.test_select import _SELECT_ROW
from tests.test_time_travel import _SERVER_BEHAVIOUR


class FvItem(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="fv_item")

    id: str | None = None
    token: str | None = None
    name: str = ""


class FvNote(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="fv_note")

    id: str | None = None
    item: Any = None


class FvLink(BaseSurrealEdge):
    model_config = SurrealConfigDict(table_name="fv_link")


class FvGhost(BaseSurrealModel):
    """A table no test creates: a subquery on it is re-sent by SurrealDB 3.x (it reads as empty)."""

    model_config = SurrealConfigDict(table_name="fv_ghost")

    id: str | None = None
    name: str = ""


_TABLES = ("fv_item", "fv_note", "fv_ghost")

#: The scalar lookups, each with its operator: a ``str`` value is bound as ``$_f0``.
_SCALAR_LOOKUPS = [
    (lookup, op)
    for lookup, op in LOOKUP_OPERATORS.items()
    if lookup not in ("isnull", "in", "not_in", "containsall", "containsany")
]

#: Strings that start with ``$``: each is data. ``$auth``, ``$this`` and ``$parent`` name server parameters,
#: ``$_f0`` the ORM's own first bound value.
_DOLLAR_VALUES = ["$x", "$min_age", "$auth", "$this", "$parent", "$_f0", "$", "$1", "$x y"]


async def _forbidden_client(*args: Any, **kwargs: Any) -> Any:
    pytest.fail("a client was acquired")


@pytest.fixture
def _no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SurrealDBConnectionManager, "get_client", _forbidden_client)


@pytest.fixture
def _no_warning() -> Iterator[None]:
    """Any warning raised by the test body is an error: a ``str`` value that is data warns of nothing."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        yield


@pytest.fixture(autouse=True)
def _fresh_state() -> Iterator[None]:
    from surreal_orm_lite import live

    QueryCache.configure(default_ttl=60, max_size=1000, enabled=True)
    QueryCache.clear()
    yield
    QueryCache.clear()
    live._SUBSCRIBERS.clear()
    live._RECENTLY_KILLED.clear()


#: The ``"$$"`` escape's ``DeprecationWarning``: the same text whatever the value.
_ESCAPE_WARNING = (
    "A filter value starting with '$$' is read with one '$' less: the '$$' escape is deprecated. A string filter "
    "value is always data, so write it with one '$' less; Var('name') references a query variable."
)


def _variable_warning(name: str) -> str:
    """The ``UserWarning`` of a ``str`` value that is ``$`` and the name *name* of a variable of ``variables()``."""
    return (
        f"The filter value '${name}' is compared as the string '${name}', not with the variable {name} given to "
        f"variables(); to reference the variable, use Var('{name}')."
    )


@contextlib.contextmanager
def _recorded() -> Iterator[list[warnings.WarningMessage]]:
    """Every warning raised in the block, none filtered out."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        yield caught


def _messages(caught: list[warnings.WarningMessage], category: type[Warning] = UserWarning) -> list[str]:
    return [str(warning.message) for warning in caught if warning.category is category]


@pytest.mark.usefixtures("_no_client", "_no_warning")
class TestStringsAreData:
    """A ``str`` value is bound as data on every filter path, and nothing warns."""

    @pytest.mark.parametrize("value", _DOLLAR_VALUES)
    @pytest.mark.parametrize(("lookup", "op"), _SCALAR_LOOKUPS, ids=[lookup for lookup, _ in _SCALAR_LOOKUPS])
    def test_every_scalar_lookup_binds_the_string(self, lookup: str, op: str, value: str) -> None:
        assert build_filter_condition("token", lookup, value, 0) == (f"token {op} $_f0", {"_f0": value}, 1)

    def test_filter(self) -> None:
        assert FvItem.objects().filter(token="$x")._compile_query() == (
            "SELECT * FROM fv_item WHERE token = $_f0;",
            {"_f0": "$x"},
        )

    def test_q(self) -> None:
        query, variables = FvItem.objects().filter(Q(token="$x") | ~Q(name="$y"), name__startswith="$")._compile_query()
        assert query == "SELECT * FROM fv_item WHERE name STARTSWITH $_f0 AND (token = $_f1 OR NOT (name = $_f2));"
        assert variables == {"_f0": "$", "_f1": "$x", "_f2": "$y"}

    def test_a_list_lookup(self) -> None:
        assert FvItem.objects().filter(token__in=["$x", "$$y"])._compile_query() == (
            "SELECT * FROM fv_item WHERE token IN $_f0;",
            {"_f0": ["$x", "$$y"]},
        )

    def test_the_record_id_column(self) -> None:
        assert FvItem.objects().filter(id="$x")._compile_query() == (
            "SELECT * FROM fv_item WHERE id = $_f0;",
            {"_f0": RecordID("fv_item", "$x")},
        )

    def test_an_edge_endpoint_column_wants_a_record_id(self) -> None:
        with pytest.raises(ValueError):
            FvLink.objects().filter(in_="$x")._compile_query()

    def test_a_subquery(self) -> None:
        query, variables = FvNote.objects().filter(item__in=FvItem.objects().filter(token="$x"))._compile_query()
        assert query == (
            "{ LET $_sq0 = (SELECT VALUE id FROM fv_item WHERE token = $_f0); (SELECT * FROM fv_note WHERE item IN $_sq0) };"
        )
        assert variables == {"_f0": "$x"}

    @pytest.mark.parametrize("value", ["$x", "$auth", "$this"])
    def test_a_live_query_writes_the_string(self, value: str) -> None:
        assert (
            FvItem.objects().filter(token=value)._compile_live(False) == f"LIVE SELECT * FROM fv_item WHERE token = '{value}';"
        )

    def test_the_get_or_create_payload(self) -> None:
        assert FvItem.objects()._criteria_payload({"token": "$x", "name__startswith": "$"}) == {"token": "$x"}


@pytest.mark.usefixtures("_no_client", "_no_warning")
class TestVarIsTheReference:
    """``Var("name")`` reads the query variable ``$name``; nothing else does."""

    def test_filter(self) -> None:
        assert FvItem.objects().filter(token=Var("x")).variables(x="$x")._compile_query() == (
            "SELECT * FROM fv_item WHERE token = $x;",
            {"x": "$x"},
        )

    def test_a_live_query_inlines_the_variable(self) -> None:
        live = FvItem.objects().filter(token=Var("x")).variables(x="$y")._compile_live(False)
        assert live == "LIVE SELECT * FROM fv_item WHERE token = '$y';"

    def test_cannot_be_written_by_get_or_create(self) -> None:
        from surreal_orm_lite.exceptions import SurrealDbError

        with pytest.raises(SurrealDbError, match="query-variable reference"):
            FvItem.objects()._criteria_payload({"token": Var("x")})


@pytest.mark.usefixtures("_no_client")
class TestDollarEscape:
    """``"$$x"`` is still the string ``"$x"`` as a scalar value, with a ``DeprecationWarning``."""

    @pytest.mark.parametrize(("value", "data"), [("$$x", "$x"), ("$$", "$"), ("$$$x", "$$x"), ("$$x y", "$x y")])
    def test_one_dollar_less(self, value: str, data: str) -> None:
        with pytest.warns(DeprecationWarning) as caught:
            assert build_filter_condition("token", "exact", value, 0) == ("token = $_f0", {"_f0": data}, 1)
        assert [str(warning.message) for warning in caught] == [_ESCAPE_WARNING]

    def test_the_warning_points_at_the_caller(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            FvItem.objects().filter(token="$$x")._build_where()
        [warning] = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert warning.filename == __file__

    def test_the_record_id_column(self) -> None:
        with pytest.warns(DeprecationWarning):
            assert FvItem.objects().filter(id="$$x")._compile_query()[1] == {"_f0": RecordID("fv_item", "$x")}

    @pytest.mark.usefixtures("_no_warning")
    def test_the_get_or_create_payload_matches_the_lookup(self) -> None:
        """The record ``get_or_create(token="$$x")`` creates is the one its lookup finds: ``token = "$x"``."""
        assert FvItem.objects()._criteria_payload({"token": "$$x"}) == {"token": "$x"}

    @pytest.mark.usefixtures("_no_warning")
    def test_a_list_item_is_not_unescaped(self) -> None:
        assert build_filter_condition("token", "in", ["$$x"], 0) == ("token IN $_f0", {"_f0": ["$$x"]}, 1)


#: Each statement a queryset compiles, for ``filter(token="$x")`` (its subquery, for the last three), with
#: ``variables(x=…)`` on the outer queryset, the inner one, or an inner one compiled after the outer filter.
_STATEMENTS: dict[str, Callable[[], Any]] = {
    "select": lambda: FvItem.objects().filter(token="$x").variables(x=1)._compile_query(),
    "count": lambda: FvItem.objects().filter(token="$x").variables(x=1)._compile_aggregation_query("count()"),
    "values-annotate": lambda: (
        FvItem.objects().filter(token="$x").variables(x=1).values("name").annotate(n=Count())._compile_group_by_query()
    ),
    "live": lambda: FvItem.objects().filter(token="$x").variables(x=1)._compile_live(False),
    "subquery, outer variables": lambda: (
        FvNote.objects().filter(item__in=FvItem.objects().filter(token="$x")).variables(x=1)._compile_query()
    ),
    "subquery, its own variables": lambda: (
        FvNote.objects().filter(item__in=FvItem.objects().filter(token="$x").variables(x=1))._compile_query()
    ),
    "outer filter, a later subquery's variables": lambda: (
        FvNote.objects().filter(item="$x").filter(item__in=Subquery(FvItem.objects().variables(x=1)))._compile_query()
    ),
}


@pytest.mark.usefixtures("_no_client")
class TestVariableNameWarning:
    """A ``str`` value that is ``$`` and a name ``variables()`` gives is still data, with a ``UserWarning``."""

    def test_filter(self) -> None:
        """The value stays bound as data; the warning names ``Var("x")``, once, at the caller's line."""
        with _recorded() as caught:
            query, variables = FvItem.objects().filter(token="$x").variables(x="other")._compile_query()
        assert query == "SELECT * FROM fv_item WHERE token = $_f0;"
        assert variables == {"x": "other", "_f0": "$x"}
        assert [(warning.category, str(warning.message)) for warning in caught] == [(UserWarning, _variable_warning("x"))]
        assert caught[0].filename == __file__

    @pytest.mark.parametrize(("lookup", "op"), _SCALAR_LOOKUPS, ids=[lookup for lookup, _ in _SCALAR_LOOKUPS])
    def test_every_scalar_lookup(self, lookup: str, op: str) -> None:
        """``lte``/``lt`` would otherwise match every number silently: a number sorts below a string."""
        with _recorded() as caught:
            qs = FvItem.objects().filter(**{f"token__{lookup}": "$max_age"}).variables(max_age=25)
            assert qs._compile_query() == (
                f"SELECT * FROM fv_item WHERE token {op} $_f0;",
                {"max_age": 25, "_f0": "$max_age"},
            )
        assert _messages(caught) == [_variable_warning("max_age")]

    def test_q(self) -> None:
        with _recorded() as caught:
            qs = FvItem.objects().filter(Q(token="$x") | ~Q(name="$y")).variables(x=1, y=2)
            assert qs._compile_query()[1] == {"x": 1, "y": 2, "_f0": "$x", "_f1": "$y"}
        assert _messages(caught) == [_variable_warning("x"), _variable_warning("y")]

    @pytest.mark.parametrize("statement", list(_STATEMENTS))
    def test_every_statement(self, statement: str) -> None:
        with _recorded() as caught:
            _STATEMENTS[statement]()
        assert _messages(caught) == [_variable_warning("x")]
        assert caught[0].filename == __file__

    def test_a_live_query_writes_the_string(self) -> None:
        with _recorded() as caught:
            live = FvItem.objects().filter(token="$x").variables(x="y")._compile_live(False)
        assert live == "LIVE SELECT * FROM fv_item WHERE token = '$x';"
        assert _messages(caught) == [_variable_warning("x")]

    def test_once_per_name_and_statement(self) -> None:
        with _recorded() as caught:
            FvItem.objects().filter(token="$x", name="$x").filter(Q(token__lte="$x")).variables(x=1)._compile_query()
        assert _messages(caught) == [_variable_warning("x")]

    def test_the_escape_warns_of_the_escape_only(self) -> None:
        """``"$$x"`` was the string ``"$x"`` before v0.28.1 too: nothing changed, the escape's warning is the only one."""
        with _recorded() as caught:
            FvItem.objects().filter(token="$$x").variables(x=1)._compile_query()
        assert [(warning.category, str(warning.message)) for warning in caught] == [(DeprecationWarning, _ESCAPE_WARNING)]

    @pytest.mark.usefixtures("_no_warning")
    def test_a_name_variables_does_not_give(self) -> None:
        """Data such as the string ``"$x"``, with no variable ``x``: nothing warns."""
        FvItem.objects().filter(token="$x")._compile_query()
        FvItem.objects().filter(token="$x").variables(y=1)._compile_query()
        FvNote.objects().filter(item__in=FvItem.objects().filter(token="$x").variables(y=1))._compile_query()

    @pytest.mark.usefixtures("_no_warning")
    @pytest.mark.parametrize("value", ["$x y", "$x.y", "$x\n", " $x", "$", "$1", "x"])
    def test_a_value_that_is_not_a_variable_name(self, value: str) -> None:
        FvItem.objects().filter(token=value).variables(**{"x": 1, "1": 2, "x y": 3})._compile_query()

    @pytest.mark.usefixtures("_no_warning")
    def test_an_item_of_a_list_and_var(self) -> None:
        """An item of a list lookup was never a reference; ``Var("x")`` is the reference."""
        FvItem.objects().filter(token__in=["$x"], name__not_in=["$x"]).variables(x=1)._compile_query()
        FvItem.objects().filter(token=Var("x")).variables(x=1)._compile_query()

    @pytest.mark.usefixtures("_no_warning")
    def test_the_get_or_create_payload(self) -> None:
        """The lookup warns (its statement); the create payload of the same call does not warn again."""
        assert FvItem.objects().variables(x=1)._criteria_payload({"token": "$x"}) == {"token": "$x"}


@contextlib.asynccontextmanager
async def values_db(kind: str) -> AsyncIterator[Any]:
    """The ORM on *kind*: a fresh ``mem://`` engine, or the server of ``SURREALDB_PORT`` over WebSocket.

    Seeded with ``a`` (token ``"$x"``), ``b`` (no token: ``NONE``), ``c`` (token ``"x"``) and ``d`` (token
    ``"$y"``), and a note on ``a``.
    """
    context = embedded_database("mem://") if kind == "mem" else orm_client(*_TABLES)
    async with context as client:
        await client.query("CREATE fv_item:a SET token = '$x', name = 'A';")
        await client.query("CREATE fv_item:b SET name = 'B';")
        await client.query("CREATE fv_item:c SET token = 'x', name = 'C';")
        await client.query("CREATE fv_item:d SET token = '$y', name = 'D';")
        await client.query("CREATE fv_note:n SET item = fv_item:a;")
        yield client


def _ids(rows: list[Any]) -> list[str]:
    return sorted(str(row.id) for row in rows)


@pytest.mark.usefixtures("_no_warning")
@pytest.mark.parametrize("kind", ["mem", "ws"])
class TestStringsAreDataE2E:
    """``filter(token="$x")`` finds the record whose token is the string ``"$x"``, never one with no token."""

    async def test_filter(self, kind: str) -> None:
        async with values_db(kind):
            assert _ids(await FvItem.objects().filter(token="$x").exec()) == ["a"]
            assert _ids(await FvItem.objects().filter(token="$x").variables(y="x").exec()) == ["a"]
            assert _ids(await FvItem.objects().filter(token="$nothing").exec()) == []
            assert _ids(await FvItem.objects().filter(token__in=["$x", "$y"]).exec()) == ["a", "d"]

    async def test_q(self, kind: str) -> None:
        async with values_db(kind):
            assert _ids(await FvItem.objects().filter(Q(token="$x") | Q(token="$y")).exec()) == ["a", "d"]
            assert _ids(await FvItem.objects().filter(~Q(token="$x")).exec()) == ["b", "c", "d"]

    async def test_reads(self, kind: str) -> None:
        async with values_db(kind):
            qs = FvItem.objects().filter(token="$x")
            assert (await qs.get()).name == "A"
            assert (await qs.first()).name == "A"
            assert await qs.count() == 1
            assert await qs.exists() is True
            assert _ids(await FvItem.objects().filter(token="$x").cache().exec()) == ["a"]

    async def test_var_reads_the_variable(self, kind: str) -> None:
        async with values_db(kind):
            assert _ids(await FvItem.objects().filter(token=Var("x")).variables(x="x").exec()) == ["c"]
            assert _ids(await FvItem.objects().filter(token=Var("x")).variables(x="$x").exec()) == ["a"]

    async def test_get_or_create(self, kind: str) -> None:
        async with values_db(kind):
            found, created = await FvItem.objects().get_or_create(token="$x")
            assert (found.id, created) == ("a", False)
            made, created = await FvItem.objects().get_or_create(token="$z", defaults={"id": "z", "name": "Z"})
            assert (made.token, created) == ("$z", True)
            again, created = await FvItem.objects().get_or_create(token="$z", defaults={"id": "z", "name": "Z"})
            assert (again.id, created) == ("z", False)

    async def test_update_or_create(self, kind: str) -> None:
        async with values_db(kind):
            updated, created = await FvItem.objects().update_or_create(token="$y", defaults={"name": "D2"})
            assert (updated.name, created) == ("D2", False)
            assert {row.id: row.name for row in await FvItem.objects().all()} == {"a": "A", "b": "B", "c": "C", "d": "D2"}

    async def test_bulk_update_by_filter(self, kind: str) -> None:
        async with values_db(kind):
            await FvItem.objects().filter(token="$x").bulk_update(name="hit")
            assert {row.id: row.name for row in await FvItem.objects().all()} == {"a": "hit", "b": "B", "c": "C", "d": "D"}

    async def test_a_subquery(self, kind: str) -> None:
        async with values_db(kind):
            assert _ids(await FvNote.objects().filter(item__in=FvItem.objects().filter(token="$x")).exec()) == ["n"]
            assert _ids(await FvNote.objects().filter(item__in=Subquery(FvItem.objects().filter(token="$q"))).exec()) == []


@pytest.mark.parametrize("kind", ["mem", "ws"])
class TestDollarEscapeE2E:
    async def test_escaped_value_finds_the_string(self, kind: str) -> None:
        async with values_db(kind):
            with pytest.warns(DeprecationWarning):
                assert _ids(await FvItem.objects().filter(token="$$x").exec()) == ["a"]

    async def test_once_per_cached_read(self, kind: str) -> None:
        """A miss compiles the statement twice (key, then read): the escape still warns once per read."""
        async with values_db(kind):
            for _ in range(2):  # a miss, then a hit
                with _recorded() as caught:
                    assert _ids(await FvItem.objects().filter(token="$$x").cache().exec()) == ["a"]
                assert _messages(caught, DeprecationWarning) == [_ESCAPE_WARNING]

    async def test_get_or_create_converges(self, kind: str) -> None:
        async with values_db(kind):
            with pytest.warns(DeprecationWarning):
                found, created = await FvItem.objects().get_or_create(token="$$x")
            assert (found.id, created) == ("a", False)


@pytest.mark.parametrize("kind", ["mem", "ws"])
class TestVariableNameWarningE2E:
    """Every read and write by filter compares with the string, and warns once, at the caller's line."""

    async def test_reads(self, kind: str) -> None:
        async with values_db(kind):
            qs = FvItem.objects().filter(token="$x").variables(x="x")
            expected = {"exec": ["a"], "get": "A", "first": "A", "count": 1, "exists": True}
            for name, want in expected.items():
                with _recorded() as caught:
                    result = await getattr(qs, name)()
                got = _ids(result) if name == "exec" else result.name if name in ("get", "first") else result
                assert got == want, name
                assert _messages(caught) == [_variable_warning("x")], name
                [warning] = [warning for warning in caught if warning.category is UserWarning]
                assert warning.filename == __file__, name

    async def test_once_per_cached_read(self, kind: str) -> None:
        async with values_db(kind):
            for _ in range(2):  # a miss, then a hit
                with _recorded() as caught:
                    assert _ids(await FvItem.objects().filter(token="$x").variables(x="x").cache().exec()) == ["a"]
                assert _messages(caught) == [_variable_warning("x")]

    async def test_once_when_a_missing_subquery_table_resends(self, kind: str) -> None:
        """SurrealDB 3.x has the statement re-sent with the subquery of the missing table empty: still one warning."""
        async with values_db(kind):
            qs = FvItem.objects().filter(token="$x", name="$$A").filter(name__in=FvGhost.objects().select("name"))
            with _recorded() as caught:
                assert await qs.variables(x="x").exec() == []
            assert _messages(caught) == [_variable_warning("x")]
            assert _messages(caught, DeprecationWarning) == [_ESCAPE_WARNING]

    async def test_get_or_create_and_update_or_create(self, kind: str) -> None:
        """Their lookup warns for the queryset's ``variables()``; the record found or created holds the string."""
        async with values_db(kind):
            with _recorded() as caught:
                found, created = await FvItem.objects().variables(x="x").get_or_create(token="$x")
            assert (found.id, created) == ("a", False)
            assert _messages(caught) == [_variable_warning("x")]
            with _recorded() as caught:
                made, created = await FvItem.objects().variables(z=1).get_or_create(token="$z", defaults={"id": "z"})
            assert (made.token, created) == ("$z", True)
            assert _messages(caught) == [_variable_warning("z")]
            with _recorded() as caught:
                updated, created = await FvItem.objects().variables(y="x").update_or_create(token="$y", defaults={"name": "D2"})
            assert (updated.name, created) == ("D2", False)
            assert (await FvItem.objects().get("d")).name == "D2"
            assert _messages(caught) == [_variable_warning("y")]

    async def test_writes_by_filter(self, kind: str) -> None:
        async with values_db(kind):
            with _recorded() as caught:
                assert await FvItem.objects().filter(token="$x").variables(x="x").bulk_update(name="hit") == 1
            assert _messages(caught) == [_variable_warning("x")]
            with _recorded() as caught:
                assert await FvItem.objects().filter(token="$y").variables(y="x").bulk_delete() == 1
            assert _messages(caught) == [_variable_warning("y")]
            assert {row.id: row.name for row in await FvItem.objects().all()} == {"a": "hit", "b": "B", "c": "C"}


async def _take(stream: Any, count: int, timeout: float = 10.0) -> list[Any]:
    async def _read() -> list[Any]:
        seen: list[Any] = []
        async for item in stream:
            seen.append(item)
            if len(seen) >= count:
                break
        return seen

    return await asyncio.wait_for(_read(), timeout=timeout)


@pytest.mark.usefixtures("_no_warning")
class TestLiveQueryE2E:
    """A live query (server only: the embedded engine has none) notifies for the string ``"$x"`` only."""

    async def test_watch(self) -> None:
        async with orm_client(*_TABLES) as client:
            await client.query("DEFINE TABLE fv_item SCHEMALESS;", {})
            async with FvItem.objects().filter(token="$x").watch() as stream:
                await client.query("CREATE fv_item:none SET name = 'no token';", {})
                await client.query("CREATE fv_item:other SET token = 'x';", {})
                await client.query("CREATE fv_item:hit SET token = '$x';", {})
                [envelope] = await _take(stream, 1)
                assert envelope["result"]["token"] == "$x"
                assert str(envelope["record"].id) == "hit"

    async def test_live(self) -> None:
        async with orm_client(*_TABLES) as client:
            await client.query("DEFINE TABLE fv_item SCHEMALESS;", {})
            async with FvItem.objects().filter(token="$auth").live() as stream:
                await client.query("CREATE fv_item:none SET name = 'no token';", {})
                await client.query("CREATE fv_item:hit SET token = '$auth';", {})
                [event] = await _take(stream, 1)
                assert (event.instance.id, event.instance.token) == ("hit", "$auth")


#: The row the 2.x/3.x behaviour table gives a filter string value, cell by cell.
_FILTER_VALUE_ROW = [
    '`filter(f="$x")`: a string filter value starting with `$`',
    'same on both lines: compared with the string `"$x"`, bound like any value, in a live query too; `Var("x")` reads the variable `$x`',
    "the same",
    "v0.28.1",
]


class TestFilterValueDocs:
    """Unit: what the README says of string filter values and ``Var()``."""

    def test_section(self) -> None:
        text = _normalised(_subsection("#### String filter values and `Var()`", _readme_part("### Supported Filter Lookups")))
        for needle in (
            'compares `name` with the string `"$admin"`',
            "`Var(...)` is the only way to reference a query variable",
            '`"$$admin"`',
            "`DeprecationWarning`",
            'write `"$admin"`',
            "`UserWarning`",
            '`filter(age__lte="$max_age").variables(max_age=25)`',
            '`Var("max_age")`',
            "once per read",
        ):
            assert needle in text, needle
        assert "silently matches nothing" not in text

    def test_server_behaviour_row(self) -> None:
        """Right after the ``select("meta.*")`` row of v0.28.1, before the table's last row."""
        rows = [cells for _, cells in _table_rows(_readme_part(_SERVER_BEHAVIOUR))]
        index = rows.index(_FILTER_VALUE_ROW)
        assert rows[index - 1] == _SELECT_ROW, rows[index - 1]
        assert index < len(rows) - 1 and rows[-1][3] == "v0.19.1", rows[-1]
