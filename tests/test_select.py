"""``QuerySet.select()``: what it accepts, checked when it is called, like ``order_by()`` and ``values()``.

An item is ``"*"``, a field name (an aliased field by its Python name), a dotted path into a nested object or
through a record link, or such a path followed by ``.*``. Anything else raises ``ValueError`` naming the item
before any request. Unit tests first (no engine, no server); the ``E2E`` classes run each accepted form on the
embedded ``mem://`` engine and on the server of ``SURREALDB_PORT`` (both lines in CI).
"""

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import Field
from surrealdb import RecordID

from surreal_orm_lite import BaseSurrealEdge, BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from surreal_orm_lite.testing import embedded_database
from surreal_orm_lite.time_travel import line_major
from tests.conftest import orm_client
from tests.test_embedded import _normalised, _readme_part, _subsection, _table_rows
from tests.test_query_cache import _QUERY_CACHE_ROW, _SECTION_29
from tests.test_time_travel import _SERVER_BEHAVIOUR


class SelAuthor(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="sel_author")

    id: str | None = None
    name: str = ""


class SelBook(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="sel_book")

    id: str | None = None
    title: str = ""
    label: str = Field("", alias="display_label")
    author: RecordID | None = None
    meta: dict[str, Any] = {}
    price: int = 0


class SelReview(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="sel_review")

    id: str | None = None
    book: RecordID | None = None
    stars: int = 0


class SelLikes(BaseSurrealEdge):
    model_config = SurrealConfigDict(table_name="sel_likes")


class SelOddAlias(BaseSurrealModel):
    """An alias whose column is not a field name: ``select()`` cannot name it."""

    model_config = SurrealConfigDict(table_name="sel_odd")

    id: str | None = None
    label: str = Field("", alias="label col")


_TABLES = ("sel_author", "sel_book", "sel_review")


async def _forbidden_client(*args: Any, **kwargs: Any) -> Any:
    pytest.fail("a client was acquired")


@pytest.fixture
def _no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything asks for a client: ``select()`` refuses before any request."""
    monkeypatch.setattr(SurrealDBConnectionManager, "get_client", _forbidden_client)


#: ``(items given to select(), the projection compiled)``: every form ``select()`` accepts.
_ACCEPTED: list[tuple[tuple[str, ...], str]] = [
    (("*",), "*"),
    (("title",), "title"),
    (("id", "title", "price"), "id, title, price"),
    (("label",), "display_label"),
    (("author.name",), "author.name"),
    (("meta.inner.x",), "meta.inner.x"),
    (("label.first",), "display_label.first"),
    (("author.*",), "author.*"),
    (("meta.inner.*",), "meta.inner.*"),
    (("label.*",), "display_label.*"),
    (("title", "author.*"), "title, author.*"),
    (("*", "author.*"), "*, author.*"),
    (("_private", "field2"), "_private, field2"),
]

#: Items ``select()`` refuses: not a field name, a dotted path or a path followed by ``.*``.
_REFUSED: list[str] = [
    "",
    " ",
    " title",
    "title ",
    "a b",
    "x-y",
    "title.",
    ".title",
    "author..name",
    "2fast",
    "title\n",
    "count()",
    "time::now()",
    "author.{name}",
    "author[*]",
    "author[0]",
    "x AS y",
    "author.name AS who",
    "title, price",
    "->likes->sel_book",
    "$param",
    "author.**",
    "author.*.name",
    "*.name",
    ".*",
    "**",
    "`title`",
    "'title'",
]


@pytest.mark.usefixtures("_no_client")
class TestAccepted:
    """Each accepted form compiles to its column, an aliased field to the column it is stored under."""

    @pytest.mark.parametrize(("items", "projection"), _ACCEPTED, ids=[", ".join(items) for items, _ in _ACCEPTED])
    def test_compiles(self, items: tuple[str, ...], projection: str) -> None:
        assert SelBook.objects().select(*items)._compile_query() == (f"SELECT {projection} FROM sel_book;", {})

    def test_no_item_selects_every_field(self) -> None:
        assert SelBook.objects().select()._compile_query() == ("SELECT * FROM sel_book;", {})

    def test_edge_endpoints(self) -> None:
        """``in_`` is the Python name of an edge's ``in``; ``in`` and ``out`` are field names too."""
        assert SelLikes.objects().select("in_", "out")._compile_query() == ("SELECT in, out FROM sel_likes;", {})
        assert SelLikes.objects().select("in", "in.*")._compile_query() == ("SELECT in, in.* FROM sel_likes;", {})

    def test_last_call_wins(self) -> None:
        qs = SelBook.objects().select("title").select("price")
        assert qs.select_item == ["price"]


@pytest.mark.usefixtures("_no_client")
class TestRefused:
    """Anything else raises ``ValueError`` naming the item, when ``select()`` is called: no request is made."""

    @pytest.mark.parametrize("item", _REFUSED)
    def test_value_error_names_the_item(self, item: str) -> None:
        with pytest.raises(ValueError) as info:
            SelBook.objects().select("title", item)
        assert repr(item) in str(info.value)
        assert "select()" in str(info.value)

    @pytest.mark.parametrize("item", ["count()", "x AS y", "author.{name}"])
    async def test_before_any_read(self, item: str) -> None:
        with pytest.raises(ValueError, match=r"select\(\)"):
            await SelBook.objects().select(item).exec()
        with pytest.raises(ValueError, match=r"select\(\)"):
            await SelBook.objects().filter(title="x").select(item).first()
        with pytest.raises(ValueError, match=r"select\(\)"):
            await SelBook.objects().select(item).cache().exec()

    def test_the_selection_in_place_is_kept(self) -> None:
        qs = SelBook.objects().select("title")
        with pytest.raises(ValueError):
            qs.select("price", "a b")
        assert qs.select_item == ["title"]

    @pytest.mark.parametrize("item", [1, None, b"title", ("title",)])
    def test_not_a_string(self, item: Any) -> None:
        with pytest.raises(TypeError, match=type(item).__name__):
            SelBook.objects().select(item)

    def test_an_alias_whose_column_is_not_a_field_name(self) -> None:
        with pytest.raises(ValueError) as info:
            SelOddAlias.objects().select("label")
        assert "'label'" in str(info.value) and "'label col'" in str(info.value)


@contextlib.asynccontextmanager
async def select_db(kind: str) -> AsyncIterator[Any]:
    """The ORM on *kind*: a fresh ``mem://`` engine, or the server of ``SURREALDB_PORT`` over WebSocket.

    Seeded with two authors, two books linking them (one with a nested object) and a review linking a book.
    """
    context = embedded_database("mem://") if kind == "mem" else orm_client(*_TABLES)
    async with context as client:
        await SelAuthor(id="ada", name="Ada").save()
        await SelAuthor(id="bea", name="Bea").save()
        await client.query(
            "CREATE sel_book:b1 SET title = 'Dune', display_label = 'first', author = sel_author:ada, price = 12, "
            "meta = { inner: { x: 1 } };"
        )
        await client.query(
            "CREATE sel_book:b2 SET title = 'Emma', display_label = 'second', author = sel_author:bea, price = 9;"
        )
        await SelReview(id="r1", book=RecordID("sel_book", "b1"), stars=5).save()
        yield client


def _author(row: Any) -> dict[str, Any]:
    """The linked author of a row read with ``author.*``, without its record id."""
    return {key: value for key, value in row["author"].items() if key != "id"}


@pytest.mark.parametrize("kind", ["mem", "ws"])
class TestAcceptedFormsE2E:
    """Each form ``select()`` accepts reads the same on SurrealDB 2.x, 3.x and the embedded engine."""

    async def test_field_names(self, kind: str) -> None:
        async with select_db(kind):
            books = await SelBook.objects().select("id", "title", "price").order_by("title").exec()
            assert [(book.id, book.title, book.price) for book in books] == [("b1", "Dune", 12), ("b2", "Emma", 9)]
            assert [book.label for book in books] == ["", ""]  # not selected: the default

    async def test_an_aliased_field_by_its_python_name(self, kind: str) -> None:
        async with select_db(kind):
            books = await SelBook.objects().select("id", "label").order_by("label").exec()
            assert [(book.id, book.label) for book in books] == [("b1", "first"), ("b2", "second")]

    async def test_every_field(self, kind: str) -> None:
        async with select_db(kind):
            book = await SelBook.objects().select("*").filter(title="Dune").get()
            assert (book.id, book.title, book.label, book.price, book.meta) == ("b1", "Dune", "first", 12, {"inner": {"x": 1}})

    async def test_a_dotted_path_through_a_link(self, kind: str) -> None:
        async with select_db(kind):
            rows = await SelBook.objects().select("title", "author.name").order_by("title").exec()
            assert rows == [
                {"title": "Dune", "author": {"name": "Ada"}},
                {"title": "Emma", "author": {"name": "Bea"}},
            ]

    async def test_a_dotted_path_into_a_nested_object(self, kind: str) -> None:
        async with select_db(kind):
            book = await SelBook.objects().select("id", "meta.inner.x").filter(title="Dune").first()
            assert (book.id, book.meta) == ("b1", {"inner": {"x": 1}})

    async def test_every_field_of_the_linked_record(self, kind: str) -> None:
        async with select_db(kind):
            rows = await SelBook.objects().select("title", "author.*").order_by("title").exec()
            assert [(row["title"], _author(row)) for row in rows] == [("Dune", {"name": "Ada"}), ("Emma", {"name": "Bea"})]
            assert [row["author"]["id"] for row in rows] == [RecordID("sel_author", "ada"), RecordID("sel_author", "bea")]

    async def test_every_field_two_links_away(self, kind: str) -> None:
        async with select_db(kind):
            [row] = await SelReview.objects().select("stars", "book.author.*").exec()
            assert row["stars"] == 5
            assert {key: value for key, value in row["book"]["author"].items() if key != "id"} == {"name": "Ada"}

    async def test_every_field_of_a_nested_object_by_line(self, kind: str) -> None:
        """README's 2.x/3.x table: the object on SurrealDB 3.x; its values as a list on 2.x and the embedded engine.

        A list does not validate as the ``meta`` field: that row comes back as a dict.
        """
        async with select_db(kind) as client:
            [row] = await SelBook.objects().select("id", "meta.*").filter(title="Dune").exec()
            if line_major(await client.version()) >= 3:
                assert isinstance(row, SelBook) and (row.id, row.meta) == ("b1", {"inner": {"x": 1}})
            else:
                assert row == {"id": RecordID("sel_book", "b1"), "meta": [{"x": 1}]}

    async def test_every_field_and_the_linked_record(self, kind: str) -> None:
        async with select_db(kind):
            [row] = await SelBook.objects().select("*", "author.*").filter(title="Emma").exec()
            assert (row["id"], row["title"], row["price"], _author(row)) == (
                RecordID("sel_book", "b2"),
                "Emma",
                9,
                {"name": "Bea"},
            )

    async def test_with_cache(self, kind: str) -> None:
        async with select_db(kind):
            for _ in range(2):
                rows = await SelBook.objects().select("title", "author.*").order_by("title").cache().exec()
                assert [(row["title"], _author(row)) for row in rows] == [("Dune", {"name": "Ada"}), ("Emma", {"name": "Bea"})]

    async def test_in_a_subquery(self, kind: str) -> None:
        async with select_db(kind):
            authors = SelBook.objects().filter(price__gt=10).select("author")
            assert [author.name for author in await SelAuthor.objects().filter(id__in=authors).exec()] == ["Ada"]


#: The row the 2.x/3.x behaviour table gives a path followed by ``.*``, cell by cell.
_SELECT_ROW = [
    '`select("meta.*")` on a nested object that is not a record link',
    'its values, as a list; through a link (`select("author.*")`), the linked record',
    "the object; through a link, the linked record",
    "pre-existing (documented in v0.28.1)",
]


class TestSelectDocs:
    """Unit: what the README says ``select()`` accepts, and the 2.x/3.x row of a path followed by ``.*``."""

    def test_what_select_accepts(self) -> None:
        text = _normalised(_subsection("#### What `select()` accepts", _readme_part("### 4. QuerySet Methods")))
        for needle in (
            '"author.name"',
            '"author.*"',
            '"*", "author.*"',
            "aliased field by its Python name",
            "`order_by()` and `values()`",
            "raises `ValueError` naming the item when `select()` is called, before any request",
            "`query()` and bound parameters",
            "dicts keyed by Python names",
            "its values as a list on 2.x and the embedded engine",
        ):
            assert needle in text, needle

    def test_server_behaviour_row(self) -> None:
        """Right after v0.28.0's query cache row, before v0.28.1's other rows and the table's last row; the header
        names its lines."""
        rows = [cells for _, cells in _table_rows(_readme_part(_SERVER_BEHAVIOUR))]
        index = rows.index(_SELECT_ROW)
        assert rows[index - 1] == _QUERY_CACHE_ROW, rows[index - 1]
        assert index < len(rows) - 1 and rows[-1][3] == "v0.19.1", rows[-1]
        header = _normalised(_readme_part(_SERVER_BEHAVIOUR).split("\n|", 1)[0])
        assert "; the rows added in v0.28.1 on 2.7.0 and 3.3.4, identical on 3.1.5" in header, header

    def test_the_cache_section_names_select_paths_only(self) -> None:
        text = _normalised(_readme_part(_SECTION_29))
        assert '`select()` item that is not a plain field, such as `"author.*"` (the projection reads the linked' in text
        assert "select()` expression" not in text and '"author.*"` or an expression' not in text
