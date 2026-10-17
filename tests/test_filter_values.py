"""Filter values: a ``str`` is data, whatever it starts with; ``Var("name")`` is the only variable reference.

``filter(token="$x")`` compares ``token`` with the string ``"$x"``, bound like any other value, on every filter
path: ``filter()``, ``Q`` (``|``, ``&``, ``~``), ``get()``, ``count()``, ``exists()``, ``get_or_create()``,
``update_or_create()``, ``bulk_update()`` by filter, ``cache()``, a subquery and a live query (the value written as
the string literal ``'$x'``). ``filter(token=Var("x"))`` reads the variable ``$x`` given to ``variables()``.

The ``"$$"`` escape of v0.14.3 keeps its meaning for one more cycle: a scalar value starting with ``"$$"`` is the
same value with one ``$`` less (``"$$x"`` is the string ``"$x"``), with a ``DeprecationWarning`` saying to write
``"$x"``. Unit tests first (no engine, no server); the ``E2E`` classes run on the embedded ``mem://`` engine and on
the server of ``SURREALDB_PORT`` (both lines in CI), the live query on the server only.
"""

import asyncio
import contextlib
import warnings
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from surrealdb import RecordID

from surreal_orm_lite import (
    BaseSurrealEdge,
    BaseSurrealModel,
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


_TABLES = ("fv_item", "fv_note")

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


def _escape_warning(value: str) -> str:
    return (
        f"The filter value {value!r} is read as {value[1:]!r}: the '$$' escape is deprecated. A string filter value "
        f"is always data, so write {value[1:]!r}; Var('name') references a query variable."
    )


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

    def test_variables_do_not_change_the_value(self) -> None:
        query, variables = FvItem.objects().filter(token="$x").variables(x="other")._compile_query()
        assert query == "SELECT * FROM fv_item WHERE token = $_f0;"
        assert variables == {"x": "other", "_f0": "$x"}

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
        assert [str(warning.message) for warning in caught] == [_escape_warning(value)]

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
            assert _ids(await FvItem.objects().filter(token="$x").variables(x="x").exec()) == ["a"]
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

    async def test_get_or_create_converges(self, kind: str) -> None:
        async with values_db(kind):
            with pytest.warns(DeprecationWarning):
                found, created = await FvItem.objects().get_or_create(token="$$x")
            assert (found.id, created) == ("a", False)


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
        ):
            assert needle in text, needle
        assert "silently matches nothing" not in text

    def test_server_behaviour_row(self) -> None:
        """Right after the ``select("meta.*")`` row of v0.28.1, before the table's last row."""
        rows = [cells for _, cells in _table_rows(_readme_part(_SERVER_BEHAVIOUR))]
        index = rows.index(_FILTER_VALUE_ROW)
        assert rows[index - 1] == _SELECT_ROW, rows[index - 1]
        assert index == len(rows) - 2, rows[index + 1]
