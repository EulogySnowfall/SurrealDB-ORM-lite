"""``QuerySet.limit()`` and ``offset()``: each takes a non-negative integer, checked when it is called.

A value that is not an integer (a ``bool``, a ``str`` such as ``"10"``, a ``float``, ``None``) raises ``TypeError``
and a negative one, or one above ``2**63 - 1``, raises ``ValueError``, each naming the method and the value, before
any request; the value in place is kept. An integer type that is not an ``int`` (one with ``__index__``, as NumPy's)
gives its value. Every read writes the checked value: ``exec()``, ``first()``, ``get()``, ``values()`` with
``annotate()``, ``cache()``, ``as_of()`` and a subquery. Unit tests first (no engine, no server); the ``E2E`` class
pages on the embedded ``mem://`` engine and on the server of ``SURREALDB_PORT`` (both lines in CI).
"""

import contextlib
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from enum import IntEnum
from typing import Any

import pytest

from surreal_orm_lite import BaseSurrealModel, Count, QueryCache, Subquery, SurrealConfigDict, SurrealDBConnectionManager
from surreal_orm_lite._sdk import ServerError
from surreal_orm_lite.testing import embedded_database
from surreal_orm_lite.time_travel import line_major
from tests.conftest import orm_client
from tests.test_embedded import _normalised, _readme_part, _subsection, _table_rows
from tests.test_filter_values import _FILTER_VALUE_ROW
from tests.test_time_travel import _SERVER_BEHAVIOUR


class PgItem(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="pg_item")

    id: str | None = None
    name: str = ""
    kind: str = ""
    rank: int = 0


class PgNote(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="pg_note")

    id: str | None = None
    item: Any = None


_TABLES = ("pg_item", "pg_note")


async def _forbidden_client(*args: Any, **kwargs: Any) -> Any:
    pytest.fail("a client was acquired")


@pytest.fixture
def _no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything asks for a client: ``limit()`` and ``offset()`` refuse before any request."""
    monkeypatch.setattr(SurrealDBConnectionManager, "get_client", _forbidden_client)


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    QueryCache.configure(default_ttl=60, max_size=1000, enabled=True)
    QueryCache.clear()
    yield
    QueryCache.clear()


class _Size(IntEnum):
    PAGE = 2


class _Index:
    """An integer type that is not an ``int``, as NumPy's ``int64``: it has ``__index__``."""

    def __init__(self, value: int) -> None:
        self.value = value

    def __index__(self) -> int:
        return self.value

    def __repr__(self) -> str:
        return f"_Index({self.value})"


#: The largest value ``limit()`` and ``offset()`` take: SurrealQL's largest integer.
_MAX = 2**63 - 1


#: ``(value, exception)``: what ``limit()`` and ``offset()`` refuse.
_REFUSED: list[tuple[Any, type[Exception]]] = [
    ("10", TypeError),
    ("1.5", TypeError),
    ("abc", TypeError),
    ("", TypeError),
    (1.5, TypeError),
    (2.0, TypeError),
    (True, TypeError),
    (False, TypeError),
    (None, TypeError),
    ([1], TypeError),
    (b"1", TypeError),
    (-1, ValueError),
    (-(10**20), ValueError),
    (_Index(-1), ValueError),
]

#: ``(value, exception)``: what ``limit()`` and ``offset()`` refuse as too large, naming the bound.
_TOO_LARGE: list[Any] = [_MAX + 1, 10**20, _Index(_MAX + 1)]

_METHODS = ["limit", "offset"]


def _message(method: str, value: Any) -> str:
    return f"{method}() takes a non-negative integer, got {value!r}"


def _too_large(method: str, value: Any) -> str:
    return f"{method}() takes an integer up to 9223372036854775807 (2**63 - 1), got {value!r}"


@pytest.mark.usefixtures("_no_client")
class TestAccepted:
    """A non-negative ``int`` is written as the ``LIMIT``/``START`` of the statement."""

    @pytest.mark.parametrize("value", [0, 1, 25, 10**12, _MAX])
    def test_compiles(self, value: int) -> None:
        query, variables = PgItem.objects().limit(value).offset(value)._compile_query()
        assert query == f"SELECT * FROM pg_item LIMIT {value} START {value};"
        assert variables == {}

    def test_an_int_subclass_is_written_as_its_value(self) -> None:
        qs = PgItem.objects().limit(_Size.PAGE).offset(_Size.PAGE)
        assert type(qs._limit) is int and type(qs._offset) is int
        assert qs._compile_query() == ("SELECT * FROM pg_item LIMIT 2 START 2;", {})

    def test_an_integer_type_is_written_as_its_value(self) -> None:
        """An object with ``__index__`` (NumPy's integers) is an integer: its value, as a plain ``int``."""
        qs = PgItem.objects().limit(_Index(3)).offset(_Index(_MAX))
        assert type(qs._limit) is int and type(qs._offset) is int
        assert qs._compile_query() == (f"SELECT * FROM pg_item LIMIT 3 START {_MAX};", {})

    def test_last_call_wins(self) -> None:
        qs = PgItem.objects().limit(5).limit(3).offset(4).offset(1)
        assert (qs._limit, qs._offset) == (3, 1)


@pytest.mark.usefixtures("_no_client")
class TestRefused:
    """Anything else raises when ``limit()``/``offset()`` is called, naming the method and the value."""

    @pytest.mark.parametrize("method", _METHODS)
    @pytest.mark.parametrize(("value", "error"), _REFUSED, ids=[repr(value) for value, _ in _REFUSED])
    def test_raises_naming_the_value(self, method: str, value: Any, error: type[Exception]) -> None:
        with pytest.raises(error) as info:
            getattr(PgItem.objects(), method)(value)
        assert str(info.value) == _message(method, value)

    @pytest.mark.parametrize("method", _METHODS)
    @pytest.mark.parametrize("value", _TOO_LARGE, ids=repr)
    def test_too_large_raises_naming_the_bound(self, method: str, value: Any) -> None:
        with pytest.raises(ValueError) as info:
            getattr(PgItem.objects(), method)(value)
        assert str(info.value) == _too_large(method, value)

    @pytest.mark.parametrize("method", _METHODS)
    def test_the_value_in_place_is_kept(self, method: str) -> None:
        qs = getattr(PgItem.objects(), method)(5)
        with pytest.raises(TypeError):
            getattr(qs, method)("10")
        with pytest.raises(ValueError):
            getattr(qs, method)(-1)
        with pytest.raises(ValueError):
            getattr(qs, method)(_MAX + 1)
        assert getattr(qs, f"_{method}") == 5
        assert qs._compile_query()[0].count(" 5") == 1


#: Querysets on every read path, each given a ``limit()`` or ``offset()`` value by *page*.
_PATHS: dict[str, Callable[[Callable[[Any], Any]], Any]] = {
    "exec": lambda page: page(PgItem.objects().filter(kind="a")),
    "values-annotate": lambda page: page(PgItem.objects().values("kind").annotate(n=Count())),
    "cache": lambda page: page(PgItem.objects().cache()),
    "as_of": lambda page: page(PgItem.objects().as_of(datetime(2021, 1, 1, tzinfo=UTC))),
    "subquery": lambda page: PgNote.objects().filter(item__in=Subquery(page(PgItem.objects()))),
    "queryset-as-subquery": lambda page: PgNote.objects().filter(item__in=page(PgItem.objects())),
}


@pytest.mark.usefixtures("_no_client")
class TestEveryPath:
    """The check is the same on every read path: a refused value never reaches a statement."""

    @pytest.mark.parametrize("path", list(_PATHS))
    @pytest.mark.parametrize("method", _METHODS)
    @pytest.mark.parametrize(("value", "error"), [("10", TypeError), (True, TypeError), (-1, ValueError)])
    def test_refused_at_the_call(self, path: str, method: str, value: Any, error: type[Exception]) -> None:
        with pytest.raises(error) as info:
            _PATHS[path](lambda qs: getattr(qs, method)(value))
        assert str(info.value) == _message(method, value)

    def test_values_annotate_writes_the_value(self) -> None:
        query, _ = PgItem.objects().values("kind").annotate(n=Count()).limit(1).offset(2)._compile_group_by_query()
        assert query == "SELECT kind, count() AS n FROM pg_item GROUP BY kind LIMIT 1 START 2;"

    def test_a_subquery_writes_the_value(self) -> None:
        query, _ = PgNote.objects().filter(item__in=PgItem.objects().limit(2).offset(1))._compile_query()
        assert "(SELECT VALUE id FROM pg_item LIMIT 2 START 1)" in query

    def test_as_of_writes_the_value(self) -> None:
        moment = datetime(2021, 1, 1, tzinfo=UTC)
        query, _ = PgItem.objects().as_of(moment).limit(3).offset(1)._compile_query(moment)
        assert " LIMIT 3 START 1 VERSION $_as_of;" in query


@contextlib.asynccontextmanager
async def paging_db(kind: str) -> AsyncIterator[Any]:
    """The ORM on *kind*: a fresh ``mem://`` engine, or the server of ``SURREALDB_PORT`` over WebSocket.

    Seeded with five items ranked 1 to 5, kinds ``a`` and ``b``, and a note on the first two.
    """
    context = embedded_database("mem://") if kind == "mem" else orm_client(*_TABLES)
    async with context as client:
        for rank, kind_ in enumerate("aabab", start=1):
            await PgItem(id=f"i{rank}", name=f"n{rank}", kind=kind_, rank=rank).save()
        await client.query("CREATE pg_note:x SET item = pg_item:i1;")
        await client.query("CREATE pg_note:y SET item = pg_item:i2;")
        yield client


def _ranks(rows: list[Any]) -> list[int]:
    return [row.rank for row in rows]


@pytest.mark.parametrize("kind", ["mem", "ws"])
class TestPagingE2E:
    """``limit()`` and ``offset()`` page the same on SurrealDB 2.x, 3.x and the embedded engine."""

    async def test_exec(self, kind: str) -> None:
        async with paging_db(kind):
            assert _ranks(await PgItem.objects().order_by("rank").limit(2).offset(1).exec()) == [2, 3]
            assert _ranks(await PgItem.objects().order_by("rank").offset(3).exec()) == [4, 5]
            assert await PgItem.objects().limit(0).exec() == []

    async def test_first_and_get_keep_the_queryset_paging(self, kind: str) -> None:
        async with paging_db(kind):
            qs = PgItem.objects().order_by("rank").limit(3).offset(1)
            assert (await qs.first()).rank == 2
            assert (qs._limit, qs._offset) == (3, 1)
            assert (await PgItem.objects().filter(rank=4).limit(5).get()).rank == 4
            assert _ranks(await qs.exec()) == [2, 3, 4]

    async def test_values_annotate(self, kind: str) -> None:
        async with paging_db(kind):
            rows = await PgItem.objects().values("kind").annotate(n=Count()).order_by("kind").limit(1).offset(1).exec()
            assert rows == [{"kind": "b", "n": 2}]

    async def test_cache(self, kind: str) -> None:
        async with paging_db(kind):
            for _ in range(2):
                assert _ranks(await PgItem.objects().order_by("rank").limit(2).cache().exec()) == [1, 2]
            assert _ranks(await PgItem.objects().order_by("rank").limit(1).cache().exec()) == [1]

    async def test_above_2_32(self, kind: str) -> None:
        """SurrealDB 3.x reads it; 2.x and the embedded engine (2.x core) refuse a ``LIMIT``/``START`` above
        ``2**32 - 1`` when the read runs."""
        async with paging_db(kind) as client:
            reads = [PgItem.objects().order_by("rank").limit(2**32).offset(3), PgItem.objects().offset(2**32)]
            if kind == "mem" or line_major(await client.version()) == 2:
                for qs in reads:
                    with pytest.raises(ServerError, match="must evaluate to a positive integer"):
                        await qs.exec()
            else:
                assert [_ranks(await qs.exec()) for qs in reads] == [[4, 5], []]

    async def test_subquery(self, kind: str) -> None:
        async with paging_db(kind):
            first_item = PgItem.objects().order_by("rank").limit(1)
            assert [note.id for note in await PgNote.objects().filter(item__in=first_item).exec()] == ["x"]
            second_item = PgItem.objects().order_by("rank").limit(1).offset(1)
            assert [note.id for note in await PgNote.objects().filter(item__in=Subquery(second_item)).exec()] == ["y"]


#: The row the 2.x/3.x behaviour table gives a large ``limit()``/``offset()``, cell by cell.
_PAGING_ROW = [
    "`limit(n)` or `offset(n)` above `4294967295` (`2**32 - 1`)",
    "the server refuses the read: `Found <n> but the LIMIT clause must evaluate to a positive integer` (`START` likewise)",
    "read; above `2**63 - 1` the ORM raises `ValueError` when `limit()` or `offset()` is called, on both lines",
    "pre-existing (documented in v0.28.1)",
]


class TestPagingDocs:
    """Unit: what the README says ``limit()`` and ``offset()`` take."""

    def test_section_4(self) -> None:
        text = _normalised(_subsection("#### `limit()` and `offset()`", _readme_part("### 4. QuerySet Methods")))
        for needle in (
            "a non-negative integer",
            "`TypeError`",
            "`ValueError`",
            "when `limit()` or `offset()` is called, before any request",
            '`"10"`',
            "NumPy",
            "`2**63 - 1`",
            "`4294967295`",
        ):
            assert needle in text, needle

    def test_server_behaviour_row(self) -> None:
        """Right after the filter value row of v0.28.1, before the table's last row."""
        rows = [cells for _, cells in _table_rows(_readme_part(_SERVER_BEHAVIOUR))]
        index = rows.index(_PAGING_ROW)
        assert rows[index - 1] == _FILTER_VALUE_ROW, rows[index - 1]
        assert index == len(rows) - 2, rows[index + 1]
