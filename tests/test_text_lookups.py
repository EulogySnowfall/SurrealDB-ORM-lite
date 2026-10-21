"""The text lookups (#251): ``startswith``, ``endswith``, ``like``, ``ilike``, ``match`` and ``regex``.

SurrealQL has no infix operator for them (``name STARTSWITH $v`` is a parse error on 2.7.0, 3.3.4 and the
embedded engine), so each compiles to a string function, guarded by the field's type:
``((name).is_string() AND string::starts_with(name, $_f0))``. The bare function fails the whole query as soon as
one record holds ``NONE``, ``NULL`` or a non-string in that field; the guard makes such a record never match,
and match the negation (``~Q(...)``). ``type::is::string`` is 2.x-only and ``type::is_string`` 3.x-only: the
method form ``.is_string()`` parses on both lines and on the embedded engine (measured on 2.7.0, 3.1.5, 3.3.4 and
SDK 2.0.0's engine). The path is parenthesised: 3.x applies ``authors.name.is_string()`` to each element of the
array a dotted path through an array of links or objects gives, while ``(authors.name).is_string()`` is false.

``like``/``ilike`` take an SQL ``LIKE`` pattern, translated before it is bound: ``%`` matches any run of
characters (newlines included), ``_`` exactly one, the pattern is anchored at both ends, and every other
character is literal (the regex metacharacters are escaped). ``ilike`` adds ``(?i)``. ``match`` and ``regex``
take a regular expression (SurrealDB's, the Rust ``regex`` crate). The value stays bound as ``$_f<n>``.

Unit tests first (no engine, no server); the ``E2E`` classes run on the embedded ``mem://`` engine and on the
server of ``SURREALDB_PORT`` (both lines in CI), the live queries on the server only.
"""

import asyncio
import contextlib
import warnings
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from surrealdb import RecordID

from surreal_orm_lite import BaseSurrealModel, Q, Subquery, SurrealConfigDict, Var
from surreal_orm_lite.constants import LOOKUP_OPERATORS, TEXT_LOOKUP_FUNCTIONS
from surreal_orm_lite.testing import embedded_database
from surreal_orm_lite.utils import build_filter_condition
from tests._readme_tables import assert_server_header_sentence, assert_server_rows
from tests.conftest import orm_client
from tests.test_embedded import _normalised, _readme_part

TABLE = "tl_item"


class TlItem(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name=TABLE)

    id: str | None = None
    name: Any = None
    age: int = 0


#: Every regex metacharacter of SurrealDB's ``string::matches`` outside a class, and a backslash.
META = "a.b*c+d(e[f$g\\h|i?j{k}l^m)n]o"

#: The records whose name is a string: key → name.
STRINGS = {
    "alice": "Alice",
    "alice_smith": "alice smith",
    "bob": "Bob",
    "newline": "Al\nice",
    "meta": META,
    "dotless": "axb*c",
    "percent": "100% sure_thing",
    "dollar": "$admin",
}
#: The records whose name is not a string: missing (``NONE``), ``NULL``, a number, an array, an object.
NOT_STRINGS = ("none", "null", "number", "array", "object")
ALL = frozenset(STRINGS) | frozenset(NOT_STRINGS)
ALL_STRINGS = frozenset(STRINGS)

#: (lookup, value, the records it finds): the negation finds every other record, the non-strings included.
CASES: list[tuple[str, str, frozenset[str]]] = [
    ("startswith", "Al", frozenset({"alice", "newline"})),
    ("startswith", "$adm", frozenset({"dollar"})),
    ("startswith", "", ALL_STRINGS),
    ("endswith", "ice", frozenset({"alice", "newline"})),
    ("endswith", "]o", frozenset({"meta"})),
    ("like", "Al%", frozenset({"alice", "newline"})),
    ("like", "%ice", frozenset({"alice", "newline"})),
    ("like", "B_b", frozenset({"bob"})),
    ("like", "b_b", frozenset()),
    ("like", "Bo", frozenset()),
    ("like", "%", ALL_STRINGS),
    ("like", "a.b%", frozenset({"meta"})),
    ("like", "a_b%", frozenset({"meta", "dotless"})),
    ("like", META, frozenset({"meta"})),
    ("like", "%(e[f$g\\h|i?%", frozenset({"meta"})),
    ("like", "100% sure_thing", frozenset({"percent"})),
    ("like", "$%", frozenset({"dollar"})),
    ("ilike", "ALICE%", frozenset({"alice", "alice_smith"})),
    ("ilike", "al%", frozenset({"alice", "alice_smith", "newline"})),
    ("ilike", "b_B", frozenset({"bob"})),
    ("ilike", META.upper(), frozenset({"meta"})),
    ("regex", "^[Aa]l", frozenset({"alice", "alice_smith", "newline"})),
    ("regex", "ice$", frozenset({"alice", "newline"})),
    ("regex", r"^\d+%", frozenset({"percent"})),
    ("match", "(?i)^bob$", frozenset({"bob"})),
    ("match", r"\$", frozenset({"meta", "dollar"})),
]

TEXT_LOOKUPS = ["startswith", "endswith", "like", "ilike", "match", "regex"]


def _case_id(case: tuple[str, str, frozenset[str]]) -> str:
    lookup, value, _ = case
    return f"{lookup}-{value!r}"


@pytest.fixture
def _no_warning() -> Iterator[None]:
    """Any warning raised by the test body is an error."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        # A socket another test left open can be finalised by the garbage collector during this one: its
        # ResourceWarning is not this test's, and pytest reports such finaliser errors while this filter holds.
        warnings.simplefilter("ignore", ResourceWarning)
        warnings.simplefilter("default", pytest.PytestUnraisableExceptionWarning)
        yield


@pytest.fixture(autouse=True)
def _clear_live_registry() -> Iterator[None]:
    from surreal_orm_lite import live

    yield
    live._SUBSCRIBERS.clear()
    live._RECENTLY_KILLED.clear()


# ==================== compilation (unit) ====================


class TestCompile:
    """Each text lookup compiles to a guarded string function, its value bound as ``$_f<n>``."""

    def test_the_mappings(self) -> None:
        assert TEXT_LOOKUP_FUNCTIONS == {
            "startswith": "string::starts_with",
            "endswith": "string::ends_with",
            "like": "string::matches",
            "ilike": "string::matches",
            "match": "string::matches",
            "regex": "string::matches",
        }
        assert not set(TEXT_LOOKUP_FUNCTIONS) & set(LOOKUP_OPERATORS)

    @pytest.mark.parametrize(
        ("lookup", "function"),
        [
            ("startswith", "string::starts_with"),
            ("endswith", "string::ends_with"),
            ("match", "string::matches"),
            ("regex", "string::matches"),
        ],
    )
    def test_the_value_is_bound_as_it_is(self, lookup: str, function: str) -> None:
        assert build_filter_condition("name", lookup, "A.b%", 3) == (
            f"((name).is_string() AND {function}(name, $_f3))",
            {"_f3": "A.b%"},
            4,
        )

    @pytest.mark.parametrize(
        ("pattern", "regex"),
        [
            ("", "(?s)^$"),
            ("%", "(?s)^.*$"),
            ("_", "(?s)^.$"),
            ("Al%", "(?s)^Al.*$"),
            ("%a_b%", "(?s)^.*a.b.*$"),
            (META, "(?s)^a\\.b\\*c\\+d\\(e\\[f\\$g\\\\h\\|i\\?j\\{k\\}l\\^m\\)n\\]o$"),
            ("x # y-z&~ é", "(?s)^x # y-z&~ é$"),
            ("100%_", "(?s)^100.*.$"),
        ],
    )
    def test_like_translates_its_pattern(self, pattern: str, regex: str) -> None:
        assert build_filter_condition("name", "like", pattern, 0) == (
            "((name).is_string() AND string::matches(name, $_f0))",
            {"_f0": regex},
            1,
        )

    def test_ilike_is_case_insensitive(self) -> None:
        assert build_filter_condition("name", "ilike", "Al_c%.", 0) == (
            "((name).is_string() AND string::matches(name, $_f0))",
            {"_f0": "(?is)^Al.c.*\\.$"},
            1,
        )

    def test_a_nested_field(self) -> None:
        sql, _, _ = build_filter_condition("profile.name", "startswith", "A", 0)
        assert sql == "((profile.name).is_string() AND string::starts_with(profile.name, $_f0))"

    def test_a_nested_field_with_a_var(self) -> None:
        sql, _, _ = build_filter_condition("authors.name", "regex", Var("p"), 0)
        assert sql == "((authors.name).is_string() AND string::matches(authors.name, $p))"

    def test_live_with_a_nested_field(self) -> None:
        sql = TlItem.objects().filter(~Q(**{"authors.name__like": "B%"}))._compile_live(False)
        assert sql == (
            "LIVE SELECT * FROM tl_item WHERE NOT (((authors.name).is_string() "
            "AND string::matches(authors.name, '(?s)^B.*$')));"
        )

    def test_the_queryset_and_q(self) -> None:
        query, variables = TlItem.objects().filter(Q(name__like="A%") | ~Q(name__endswith="z"), age__gte=18)._compile_query()
        assert query == (
            "SELECT * FROM tl_item WHERE age >= $_f0 AND (((name).is_string() AND string::matches(name, $_f1)) "
            "OR NOT (((name).is_string() AND string::ends_with(name, $_f2))));"
        )
        assert variables == {"_f0": 18, "_f1": "(?s)^A.*$", "_f2": "z"}

    @pytest.mark.parametrize("lookup", ["startswith", "endswith", "match", "regex"])
    def test_a_var(self, lookup: str) -> None:
        function = TEXT_LOOKUP_FUNCTIONS[lookup]
        assert build_filter_condition("name", lookup, Var("p"), 2) == (
            f"((name).is_string() AND {function}(name, $p))",
            {},
            2,
        )

    @pytest.mark.parametrize("lookup", ["like", "ilike"])
    def test_a_var_cannot_be_a_like_pattern(self, lookup: str) -> None:
        with pytest.raises(ValueError, match=rf"^'{lookup}' cannot take a Var: .*use 'regex'"):
            build_filter_condition("name", lookup, Var("p"), 0)

    @pytest.mark.parametrize("lookup", TEXT_LOOKUPS)
    @pytest.mark.parametrize("value", [5, 1.5, None, True, ["A"], b"A", RecordID("tl_item", "a")])
    def test_a_value_that_is_not_a_string(self, lookup: str, value: Any) -> None:
        with pytest.raises(ValueError, match=rf"^'{lookup}' lookup requires a string, got {type(value).__name__}$"):
            build_filter_condition("name", lookup, value, 0)

    @pytest.mark.parametrize("lookup", TEXT_LOOKUPS)
    def test_a_model_instance_is_not_a_string(self, lookup: str) -> None:
        with pytest.raises(ValueError, match=rf"^'{lookup}' lookup requires a string, got TlItem$"):
            build_filter_condition("name", lookup, TlItem(id="a"), 0)

    @pytest.mark.usefixtures("_no_warning")
    @pytest.mark.parametrize("lookup", ["startswith", "endswith", "match", "regex"])
    @pytest.mark.parametrize("value", ["$x", "$auth", "$_f0", "$"])
    def test_a_dollar_string_is_data(self, lookup: str, value: str) -> None:
        _, variables, _ = build_filter_condition("name", lookup, value, 0)
        assert variables == {"_f0": value}

    @pytest.mark.usefixtures("_no_warning")
    def test_a_dollar_like_pattern_is_literal(self) -> None:
        assert build_filter_condition("name", "like", "$x%", 0)[1] == {"_f0": "(?s)^\\$x.*$"}

    @pytest.mark.parametrize(
        ("lookup", "bound"),
        [
            ("startswith", "$x"),
            ("endswith", "$x"),
            ("match", "$x"),
            ("regex", "$x"),
            ("like", "(?s)^\\$x$"),
            ("ilike", "(?is)^\\$x$"),
        ],
    )
    def test_the_dollar_escape_is_unescaped_first(self, lookup: str, bound: str) -> None:
        with pytest.warns(DeprecationWarning):
            assert build_filter_condition("name", lookup, "$$x", 0)[1] == {"_f0": bound}

    @pytest.mark.parametrize("lookup", TEXT_LOOKUPS)
    def test_a_dollar_string_naming_a_variable_warns(self, lookup: str) -> None:
        qs = TlItem.objects().filter(**{f"name__{lookup}": "$p"}).variables(p="A")
        with pytest.warns(UserWarning, match=r"Var\('p'\)"):
            qs._compile_query()

    def test_live_inlines_the_pattern_and_keeps_its_backslashes(self) -> None:
        sql = TlItem.objects().filter(name__like="a\\b'%", age__gte=1)._compile_live(False)
        assert sql == (
            "LIVE SELECT * FROM tl_item WHERE ((name).is_string() AND string::matches(name, '(?s)^a\\\\\\\\b\\'.*$')) "
            "AND age >= 1;"
        )
        assert "$" not in sql.replace("$'", "")

    def test_live_with_a_var(self) -> None:
        sql = TlItem.objects().filter(name__startswith=Var("p")).variables(p="A")._compile_live(False)
        assert sql == "LIVE SELECT * FROM tl_item WHERE ((name).is_string() AND string::starts_with(name, 'A'));"


class TestRefusalsUnchanged:
    """The refusals of v0.14.4 (the id column), v0.22.0 (edge endpoints) and v0.27.0 (subqueries) still hold."""

    @pytest.mark.parametrize("lookup", TEXT_LOOKUPS)
    def test_the_record_id_column(self, lookup: str) -> None:
        with pytest.raises(ValueError, match=rf"^'{lookup}' cannot be applied to the record id column 'id'"):
            TlItem.objects().filter(**{f"id__{lookup}": "a"})._compile_query()

    @pytest.mark.parametrize("lookup", TEXT_LOOKUPS)
    def test_an_edge_endpoint_column(self, lookup: str) -> None:
        with pytest.raises(ValueError, match=rf"^'{lookup}' cannot be applied to the edge endpoint column 'out'"):
            build_filter_condition("out", lookup, "a", 0, "e", endpoint_columns=("in", "out"))

    @pytest.mark.parametrize("lookup", TEXT_LOOKUPS)
    def test_a_subquery(self, lookup: str) -> None:
        with pytest.raises(ValueError, match=rf"^'{lookup}' cannot take a subquery"):
            TlItem.objects().filter(**{f"name__{lookup}": TlItem.objects().select("name")})._compile_query()

    def test_an_unknown_lookup(self) -> None:
        with pytest.raises(ValueError, match="Unsupported lookup type: 'istartswith'"):
            build_filter_condition("name", "istartswith", "a", 0)


#: The row the 2.x/3.x behaviour table gives the text lookups, cell by cell.
_TEXT_LOOKUP_ROW = [
    "Text lookups (`startswith`, `endswith`, `like`, `ilike`, `match`, `regex`)",
    "same on both lines: string functions guarded by `(field).is_string()` (`type::is::string` is 2.x-only); "
    "a `NONE`, `NULL`, non-string or array path value never matches",
    "the same (`type::is_string` is 3.x-only; `a.b.is_string()` tests each element of an array path); "
    "until v0.29.1: operators SurrealQL lacks (`LIKE`…), a parse error on both lines",
    "v0.29.2",
]
#: The sentence v0.29.2 adds to the 2.x/3.x table's intro paragraph.
_HEADER_SENTENCE_292 = "The row added in v0.29.2 was measured on 2.7.0, 3.3.4 and the embedded engine, identical on 3.1.5."


class TestReadme:
    def test_the_lookup_list_says_what_each_text_lookup_compiles_to(self) -> None:
        section = _normalised(_readme_part("### Supported Filter Lookups"))
        for text in ("string::starts_with", "string::ends_with", "string::matches", "is_string()", "(?i)"):
            assert text in section, text
        assert "full-text search is planned for v0.35.0, as a separate API" in section, section

    def test_server_behaviour_row(self) -> None:
        """In version order before the table's last row (later releases add their rows in version order)."""
        assert_server_rows([_TEXT_LOOKUP_ROW])

    def test_server_behaviour_header_sentence(self) -> None:
        """The sentence v0.29.2 adds to the table's intro paragraph, in version order among the others."""
        assert_server_header_sentence(_HEADER_SENTENCE_292)


# ==================== end to end: mem:// and the server ====================


@contextlib.asynccontextmanager
async def text_db(kind: str) -> AsyncIterator[Any]:
    """The ORM on *kind* (a fresh ``mem://`` engine, or the server of ``SURREALDB_PORT``), seeded with
    :data:`STRINGS` and :data:`NOT_STRINGS`. ``alice`` is 30, ``newline`` 12, every other record 40."""
    context = embedded_database("mem://") if kind == "mem" else orm_client(TABLE)
    async with context as client:
        await client.query(
            f"CREATE {TABLE}:none SET age = 40; CREATE {TABLE}:null SET name = NULL, age = 40; "
            f"CREATE {TABLE}:number SET name = 5, age = 40; CREATE {TABLE}:array SET name = ['Alice'], age = 40; "
            f"CREATE {TABLE}:object SET name = {{ first: 'Alice' }}, age = 40;",
            {},
        )
        for key, name in STRINGS.items():
            age = {"alice": 30, "newline": 12}.get(key, 40)
            await client.query(f"CREATE {TABLE}:{key} SET name = $name, age = $age;", {"name": name, "age": age})
        yield client


def _keys(rows: list[Any]) -> frozenset[str]:
    return frozenset(str(row.id) for row in rows)


@pytest.mark.usefixtures("_no_warning")
@pytest.mark.parametrize("kind", ["mem", "ws"])
class TestTextLookupsE2E:
    """Each lookup finds exactly its records; ``NONE``, ``NULL`` and non-strings never fail the query nor match."""

    @pytest.mark.parametrize("case", CASES, ids=[_case_id(case) for case in CASES])
    async def test_the_records_found_and_the_negation(self, kind: str, case: tuple[str, str, frozenset[str]]) -> None:
        lookup, value, expected = case
        criterion = {f"name__{lookup}": value}
        async with text_db(kind):
            assert _keys(await TlItem.objects().filter(**criterion).exec()) == expected
            assert _keys(await TlItem.objects().filter(~Q(**criterion)).exec()) == ALL - expected
            assert await TlItem.objects().filter(**criterion).count() == len(expected)

    async def test_the_readme_example(self, kind: str) -> None:
        async with text_db(kind):
            users = await TlItem.objects().filter(age__gte=18, name__startswith="A").exec()
            assert _keys(users) == {"alice"}

    async def test_a_var(self, kind: str) -> None:
        async with text_db(kind):
            for lookup, pattern, expected in [
                ("startswith", "Al", {"alice", "newline"}),
                ("endswith", "]o", {"meta"}),
                ("regex", "^\\d", {"percent"}),
                ("match", "(?i)^BOB$", {"bob"}),
            ]:
                qs = TlItem.objects().filter(**{f"name__{lookup}": Var("p")}).variables(p=pattern)
                assert _keys(await qs.exec()) == expected, lookup

    async def test_combined_with_or(self, kind: str) -> None:
        async with text_db(kind):
            rows = await TlItem.objects().filter(Q(name__like="B%") | Q(name__ilike="ALICE SMITH")).exec()
            assert _keys(rows) == {"bob", "alice_smith"}

    async def test_inside_a_subquery(self, kind: str) -> None:
        async with text_db(kind):
            inner = TlItem.objects().filter(name__like="Al%")
            assert _keys(await TlItem.objects().filter(id__in=inner).exec()) == {"alice", "newline"}
            assert _keys(await TlItem.objects().filter(~Q(id__in=Subquery(inner))).exec()) == ALL - {"alice", "newline"}

    async def test_the_dollar_escape(self, kind: str) -> None:
        async with text_db(kind):
            with pytest.warns(DeprecationWarning):
                assert _keys(await TlItem.objects().filter(name__like="$$adm%").exec()) == {"dollar"}


# ==================== a dotted path through an array: mem:// and the server ====================

POST_TABLE = "tl_post"
USER_TABLE = "tl_user"


class TlPost(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name=POST_TABLE)

    id: str | None = None
    authors: Any = None


#: The posts, by what their ``authors`` hold. ``authors.name`` is an array on ``links`` and ``objects``, never a
#: string: those posts never match a text lookup on it. It is the string ``'Alice'`` on ``object`` and ``link``.
ALL_POSTS = frozenset({"links", "objects", "object", "link", "empty", "none"})
ALICE_POSTS = frozenset({"object", "link"})

#: (lookup, value): each finds ``'Alice'``.
ALICE_CASES = [
    ("startswith", "A"),
    ("endswith", "ice"),
    ("like", "A%"),
    ("ilike", "a%"),
    ("match", "^A"),
    ("regex", "ice$"),
]


@contextlib.asynccontextmanager
async def posts_db(kind: str) -> AsyncIterator[Any]:
    """The ORM on *kind*, seeded with two users and the posts of :data:`ALL_POSTS`."""
    context = embedded_database("mem://") if kind == "mem" else orm_client(POST_TABLE, USER_TABLE)
    async with context as client:
        await client.query(
            f"CREATE {USER_TABLE}:al SET name = 'Alice'; CREATE {USER_TABLE}:bo SET name = 'Bob'; "
            f"CREATE {POST_TABLE}:links SET authors = [{USER_TABLE}:al, {USER_TABLE}:bo]; "
            f"CREATE {POST_TABLE}:objects SET authors = [{{ name: 'Alice' }}, {{ name: 'Bob' }}]; "
            f"CREATE {POST_TABLE}:object SET authors = {{ name: 'Alice' }}; "
            f"CREATE {POST_TABLE}:link SET authors = {USER_TABLE}:al; "
            f"CREATE {POST_TABLE}:empty SET authors = []; CREATE {POST_TABLE}:none;",
            {},
        )
        yield client


@pytest.mark.usefixtures("_no_warning")
@pytest.mark.parametrize("kind", ["mem", "ws"])
class TestDottedPathE2E:
    """A dotted path through an array of record links or of objects is an array, never a string: it never matches,
    matches the negation, and does not fail the query (SurrealDB 3.x applies ``authors.name.is_string()`` to each
    element; ``(authors.name).is_string()`` tests the array)."""

    @pytest.mark.parametrize(("lookup", "value"), ALICE_CASES, ids=[lookup for lookup, _ in ALICE_CASES])
    async def test_the_lookup_and_its_negation(self, kind: str, lookup: str, value: str) -> None:
        criterion = {f"authors.name__{lookup}": value}
        async with posts_db(kind):
            assert _keys(await TlPost.objects().filter(**criterion).exec()) == ALICE_POSTS
            assert _keys(await TlPost.objects().filter(~Q(**criterion)).exec()) == ALL_POSTS - ALICE_POSTS
            assert await TlPost.objects().filter(**criterion).count() == len(ALICE_POSTS)

    @pytest.mark.parametrize("lookup", ["startswith", "regex"])
    async def test_a_var(self, kind: str, lookup: str) -> None:
        async with posts_db(kind):
            qs = TlPost.objects().filter(**{f"authors.name__{lookup}": Var("p")}).variables(p="A")
            assert _keys(await qs.exec()) == ALICE_POSTS
            negated = TlPost.objects().filter(~Q(**{f"authors.name__{lookup}": Var("p")})).variables(p="A")
            assert _keys(await negated.exec()) == ALL_POSTS - ALICE_POSTS


# ==================== live queries: the server only ====================


async def _take(stream: Any, count: int, timeout: float = 10.0) -> list[Any]:
    async def _read() -> list[Any]:
        seen: list[Any] = []
        async for item in stream:
            seen.append(item)
            if len(seen) >= count:
                break
        return seen

    return await asyncio.wait_for(_read(), timeout=timeout)


async def _assert_silent(stream: Any, wait: float = 1.0) -> None:
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(stream.__anext__(), timeout=wait)


async def _create_others(client: Any) -> None:
    """Records no text lookup matches: ``NONE``, ``NULL``, a number, an array; then a near miss."""
    await client.query(
        f"CREATE {TABLE}:none SET age = 1; CREATE {TABLE}:null SET name = NULL; CREATE {TABLE}:number SET name = 5; "
        f"CREATE {TABLE}:array SET name = ['a\\\\bc'];",
        {},
    )


@pytest.mark.usefixtures("_no_warning")
class TestLiveE2E:
    async def test_like_with_a_backslash(self) -> None:
        async with orm_client(TABLE) as client:
            await client.query(f"DEFINE TABLE {TABLE} SCHEMALESS;", {})
            async with TlItem.objects().filter(name__like="a\\b_").live() as stream:
                await _create_others(client)
                await client.query(f"CREATE {TABLE}:miss SET name = $name;", {"name": "ab_"})
                await client.query(f"CREATE {TABLE}:hit SET name = $name;", {"name": "a\\bc"})
                [event] = await _take(stream, 1)
                await _assert_silent(stream)
            assert (event.instance.id, event.instance.name) == ("hit", "a\\bc")

    async def test_a_negated_startswith(self) -> None:
        async with orm_client(TABLE) as client:
            await client.query(f"DEFINE TABLE {TABLE} SCHEMALESS;", {})
            async with TlItem.objects().filter(~Q(name__startswith="A")).live() as stream:
                await client.query(f"CREATE {TABLE}:alice SET name = 'Alice';", {})
                await _create_others(client)
                events = await _take(stream, 4)
                await _assert_silent(stream)
            # One multi-statement request: 3.1.5 does not keep its notifications in statement order.
            assert sorted(event.instance.id for event in events) == ["array", "none", "null", "number"]

    async def test_regex_and_ilike(self) -> None:
        async with orm_client(TABLE) as client:
            await client.query(f"DEFINE TABLE {TABLE} SCHEMALESS;", {})
            async with (
                TlItem.objects().filter(name__regex="^\\d+$").live() as digits,
                TlItem.objects().filter(name__ilike="%SMITH").live() as smiths,
            ):
                await _create_others(client)
                await client.query(f"CREATE {TABLE}:n SET name = '42'; CREATE {TABLE}:s SET name = 'Alice Smith';", {})
                [digit] = await _take(digits, 1)
                [smith] = await _take(smiths, 1)
                await _assert_silent(digits, 0.5)
                await _assert_silent(smiths, 0.5)
            assert (digit.instance.id, smith.instance.id) == ("n", "s")
