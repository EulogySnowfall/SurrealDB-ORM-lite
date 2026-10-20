"""Query cache (v0.28.0): ``QuerySet.cache()`` and ``QueryCache``, a client-side store of read results.

A cached read is answered from this process's memory when an earlier read sent the same statement with the same
variables on the same connection and identity, within its TTL, and no ORM write that can change its result has
completed since. ``QueryCache`` is the full SurrealDB-ORM's API (``configure``, ``make_key``, ``get``, ``set``,
``invalidate``, ``clear``, ``stats``); the store, its bookkeeping (tickets, per-table generations, writes in
flight), the identity scopes and the write wrappers live in ``surreal_orm_lite.cache``.

Unit tests first (the store, the keys, the bookkeeping, the scopes, the write wrappers: no engine, no server); the
classes that open an engine or a server are suffixed ``E2E`` and run on the embedded ``mem://`` engine and on the
server of ``SURREALDB_PORT`` over WebSocket and HTTP.
"""

import ast
import asyncio
import contextlib
import functools
import gc
import hashlib
import inspect
import logging
import math
import random
import re
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any, NamedTuple

import pytest
from surrealdb import RecordID

import surreal_orm_lite
from surreal_orm_lite import (
    AuthenticatedUserMixin,
    BaseSurrealEdge,
    BaseSurrealModel,
    Computed,
    Count,
    GeoDistance,
    GeometryPoint,
    PointField,
    Q,
    QueryCache,
    QuerySet,
    Subquery,
    SurrealConfigDict,
    SurrealDbConflictError,
    SurrealDbConnectionError,
    SurrealDBConnectionManager,
    SurrealDbError,
    SurrealDbNotFoundError,
    around_save,
    cache,
    computed,
    encoding,
    post_save,
)
from surreal_orm_lite._sdk import AsyncSurreal, cbor_encode
from surreal_orm_lite.cache import EDGES, CacheRequest, follows_links
from surreal_orm_lite.testing import embedded_database
from surreal_orm_lite.transaction import BufferedTransaction, InteractiveTransaction
from tests._readme_tables import assert_server_header_sentence, assert_server_rows, assert_server_table
from tests.conftest import _ws_url, default_connection_class
from tests.test_embedded import _cells, _normalised, _readme, _readme_part, _subsection, _table_rows, _tables
from tests.test_time_travel import (
    _SERVER_BEHAVIOUR,
    _forbidden_client,
    _http_url,
    _line,
    _record_queries,
    connected,
)

# --- models (spec §9) ---------------------------------------------------------------------------------


class QcItem(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="qc_item")

    id: str | None = None
    name: str = ""
    category: str = ""
    price: int = 0
    tags: list[str] = []


class QcAuthor(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="qc_author")

    id: str | None = None
    name: str = ""


class QcBook(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="qc_book")

    id: str | None = None
    title: str = ""
    author: RecordID | None = None
    price: int = 0


class QcPlace(BaseSurrealModel):
    """A point field for ``GeoDistance`` (``QcItem`` stays plain); ``owner`` links to another record."""

    model_config = SurrealConfigDict(table_name="qc_place")

    id: str | None = None
    name: str = ""
    loc: PointField = None
    owner: RecordID | None = None


class QcUser(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="qc_user")

    id: str | None = None
    name: str = ""


class QcPost(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="qc_post")

    id: str | None = None
    title: str = ""


class QcLikes(BaseSurrealEdge):
    """An edge ``QcUser`` → ``QcPost``: its reads carry ``EDGES``."""

    model_config = SurrealConfigDict(table_name="qc_likes")


class QcGhost(BaseSurrealModel):
    """A table never created: SurrealDB 3.x answers ``NotFoundError``, 2.x and the embedded engine ``[]``."""

    model_config = SurrealConfigDict(table_name="qc_ghost")

    id: str | None = None
    x: int = 0


class QcNote(BaseSurrealModel):
    """A note of :func:`auth_db`'s record-access fixture, readable by its owner only."""

    model_config = SurrealConfigDict(table_name="qc_note")

    id: str | None = None
    owner: RecordID | None = None


class QcPlayer(BaseSurrealModel):
    """A ``computed()`` field: ``define_computed_fields()`` writes DDL on its table."""

    model_config = SurrealConfigDict(table_name="qc_player")

    id: str | None = None
    name: str = ""
    shout: Computed[str] = computed("string::uppercase(name)")


class QcAccount(AuthenticatedUserMixin, BaseSurrealModel):
    """A record-access model: ``define_access()`` and ``signup()`` write its table."""

    model_config = SurrealConfigDict(table_name="qc_account", access_name="qc_account_access")

    id: str | None = None
    email: str
    password: str


# --- helpers ------------------------------------------------------------------------------------------

_W2 = "QueryCache: the installed surrealdb SDK exposes no CBOR encoder; cache() reads go to the database"
_C9 = (
    "QueryCache.make_key() needs the surrealdb SDK's CBOR encoder (surrealdb.data.cbor.encode), which the installed "
    "SDK does not expose"
)


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Default settings and an empty store around every test; W2 re-armed; nothing left in flight (spec §9)."""
    QueryCache.configure(default_ttl=60, max_size=1000, enabled=True)
    QueryCache.clear()
    monkeypatch.setattr(QueryCache, "_warned_no_encoder", False)
    yield
    QueryCache.configure(default_ttl=60, max_size=1000, enabled=True)
    QueryCache.clear()
    assert QueryCache._in_flight == {}
    assert QueryCache._in_flight_all == 0


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The cache's clock, frozen at 1000.0 s; tests move it by assigning ``now[0]``."""
    clock = [1000.0]
    monkeypatch.setattr(cache, "_clock", lambda: clock[0])
    return clock


@pytest.fixture
def scope() -> int:
    """A live scope, as the manager mints one for a connection identity."""
    return QueryCache._new_scope(None)


def _raises(exc_type: type[BaseException], text: str, call: Callable[..., Any], *args: Any, **kwargs: Any) -> BaseException:
    """Call ``call`` and check it raises ``exc_type`` with exactly ``text``; return the exception."""
    with pytest.raises(exc_type) as info:
        call(*args, **kwargs)
    assert str(info.value) == text
    return info.value


def _store(key: str, data: Any, tables: set[str], *, scope: int, any_table: bool = False, ttl: float = 60.0) -> None:
    """Store ``data`` under ``key`` as a cached read does: a miss, its ticket, then the store."""
    hit, copy, ticket = QueryCache._begin_read(key, frozenset(tables), any_table, scope, ttl)
    assert (hit, copy) == (False, None)
    assert ticket is not None
    QueryCache._finish_read(ticket, data)


def _counts() -> tuple[int, int]:
    stats = QueryCache.stats()
    return stats["hits"], stats["misses"]


# --- AC-1: API and exports ----------------------------------------------------------------------------


class TestExports:
    def test_query_cache_is_exported_from_both_modules(self) -> None:
        assert surreal_orm_lite.QueryCache is cache.QueryCache
        assert "QueryCache" in surreal_orm_lite.__all__
        assert cache.__all__ == ["QueryCache"]

    @pytest.mark.parametrize(
        ("name", "signature"),
        [
            (
                "configure",
                "(*, default_ttl: 'float | timedelta' = 60, max_size: 'int' = 1000, enabled: 'bool' = True) -> 'None'",
            ),
            ("make_key", "(query: 'str', variables: 'Mapping[str, Any]', table: 'str') -> 'str'"),
            ("get", "(key: 'str') -> 'Any | None'"),
            ("set", "(key: 'str', data: 'Any', table: 'str', ttl: 'float | timedelta | None' = None) -> 'None'"),
            ("invalidate", "(model: 'type[BaseSurrealModel] | str') -> 'int'"),
            ("clear", "() -> 'None'"),
            ("stats", "() -> 'dict[str, Any]'"),
        ],
    )
    def test_signatures(self, name: str, signature: str) -> None:
        assert isinstance(inspect.getattr_static(QueryCache, name), classmethod)
        assert str(inspect.signature(getattr(QueryCache, name))) == signature

    def test_default_stats(self) -> None:
        stats = QueryCache.stats()
        assert stats == {
            "entries": 0,
            "tables": 0,
            "max_size": 1000,
            "default_ttl": 60.0,
            "enabled": True,
            "hits": 0,
            "misses": 0,
        }
        assert list(stats) == ["entries", "tables", "max_size", "default_ttl", "enabled", "hits", "misses"]


# --- C1/C2: ttl values (P2) ---------------------------------------------------------------------------


class TestTtlSeconds:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (30, 30.0),
            (0.5, 0.5),
            (Fraction(1, 4), 0.25),
            (timedelta(minutes=2), 120.0),
            (0, 0.0),
            (0.0, 0.0),
            (-0.0, 0.0),
            (timedelta(0), 0.0),
        ],
    )
    def test_accepted(self, value: object, expected: float) -> None:
        seconds = cache.ttl_seconds(value, "cache() ttl")
        assert seconds == expected
        assert type(seconds) is float
        assert math.copysign(1.0, seconds) == 1.0

    @pytest.mark.parametrize("value", [True, False, "30", Decimal("1"), None, [30]])
    def test_not_a_number_of_seconds(self, value: object) -> None:
        _raises(
            TypeError,
            f"cache() ttl must be a number of seconds or a timedelta, not {type(value).__name__}",
            cache.ttl_seconds,
            value,
            "cache() ttl",
        )

    @pytest.mark.parametrize("value", [-1, -0.5, timedelta(seconds=-1), float("nan"), float("inf"), float("-inf"), 10**400])
    def test_negative_or_not_finite(self, value: object) -> None:
        _raises(
            ValueError,
            f"QueryCache.set() ttl must be a finite number of seconds >= 0, got {value!r}",
            cache.ttl_seconds,
            value,
            "QueryCache.set() ttl",
        )


# --- AC-2: the store ----------------------------------------------------------------------------------


class TestConfigure:
    def test_keyword_only_defaults(self) -> None:
        parameters = inspect.signature(QueryCache.configure).parameters
        assert [(p.name, p.kind, p.default) for p in parameters.values()] == [
            ("default_ttl", inspect.Parameter.KEYWORD_ONLY, 60),
            ("max_size", inspect.Parameter.KEYWORD_ONLY, 1000),
            ("enabled", inspect.Parameter.KEYWORD_ONLY, True),
        ]

    def test_every_call_sets_all_three_settings(self) -> None:
        QueryCache.configure(max_size=5)
        QueryCache.configure(default_ttl=10)
        stats = QueryCache.stats()
        assert (stats["max_size"], stats["default_ttl"], stats["enabled"]) == (1000, 10.0, True)
        QueryCache.configure(enabled=False)
        QueryCache.configure(max_size=7)
        stats = QueryCache.stats()
        assert (stats["max_size"], stats["default_ttl"], stats["enabled"]) == (7, 60.0, True)

    def test_a_call_never_clears(self) -> None:
        QueryCache.set("k", 1, "t")
        QueryCache.configure(default_ttl=30)
        assert QueryCache.get("k") == 1
        assert QueryCache.stats()["hits"] == 1

    @pytest.mark.parametrize("value", [0, 0.0, timedelta(0)])
    def test_zero_default_ttl_is_accepted(self, value: object) -> None:
        QueryCache.configure(default_ttl=value)  # type: ignore[arg-type]
        assert QueryCache.stats()["default_ttl"] == 0.0
        QueryCache.set("k", 1, "t")
        assert QueryCache.stats()["entries"] == 0

    def test_timedelta_default_ttl(self) -> None:
        QueryCache.configure(default_ttl=timedelta(minutes=1, seconds=30))
        assert QueryCache.stats()["default_ttl"] == 90.0

    @pytest.mark.parametrize(
        ("value", "exc_type", "text"),
        [
            (True, TypeError, "QueryCache.configure() default_ttl must be a number of seconds or a timedelta, not bool"),
            ("60", TypeError, "QueryCache.configure() default_ttl must be a number of seconds or a timedelta, not str"),
            (-1, ValueError, "QueryCache.configure() default_ttl must be a finite number of seconds >= 0, got -1"),
            (
                float("nan"),
                ValueError,
                "QueryCache.configure() default_ttl must be a finite number of seconds >= 0, got nan",
            ),
        ],
    )
    def test_default_ttl_refused(self, value: object, exc_type: type[Exception], text: str) -> None:
        _raises(exc_type, text, QueryCache.configure, default_ttl=value)

    @pytest.mark.parametrize(
        ("value", "exc_type", "text"),
        [
            (True, TypeError, "QueryCache.configure() max_size must be an int, not bool"),
            (1.5, TypeError, "QueryCache.configure() max_size must be an int, not float"),
            ("10", TypeError, "QueryCache.configure() max_size must be an int, not str"),
            (0, ValueError, "QueryCache.configure() max_size must be >= 1, got 0"),
            (-3, ValueError, "QueryCache.configure() max_size must be >= 1, got -3"),
        ],
    )
    def test_max_size_refused(self, value: object, exc_type: type[Exception], text: str) -> None:
        _raises(exc_type, text, QueryCache.configure, max_size=value)

    @pytest.mark.parametrize("value", ["yes", 1, None])
    def test_enabled_refused(self, value: object) -> None:
        _raises(
            TypeError,
            f"QueryCache.configure() enabled must be a bool, not {type(value).__name__}",
            QueryCache.configure,
            enabled=value,
        )

    @pytest.mark.parametrize(
        "arguments",
        [
            {"default_ttl": 7, "max_size": 0},
            {"default_ttl": 7, "enabled": "no"},
            {"default_ttl": -1, "max_size": 20},
            {"max_size": True, "enabled": False},
        ],
    )
    def test_one_bad_argument_changes_nothing(self, arguments: dict[str, Any]) -> None:
        QueryCache.configure(default_ttl=5, max_size=10)
        QueryCache.set("k", 1, "t")
        before = QueryCache.stats()
        with pytest.raises((TypeError, ValueError)):
            QueryCache.configure(**arguments)
        assert QueryCache.stats() == before
        assert QueryCache.get("k") == 1

    def test_disabled_hides_the_entries(self, now: list[float]) -> None:
        QueryCache.set("k", 1, "t")
        QueryCache.set("ku", 2, "u", ttl=100)
        QueryCache.set("kv", 3, "v", ttl=10)
        QueryCache.set("kw", 4, "w", ttl=100)
        assert QueryCache.get("k") == 1
        counts = _counts()
        QueryCache.configure(enabled=False)

        assert QueryCache.get("k") is None
        QueryCache.set("k2", 2, "t")
        assert _counts() == counts
        stats = QueryCache.stats()
        assert stats["entries"] == 4
        assert stats["enabled"] is False

        # Hidden entries are still removed: by invalidate(), by a write, by expiry.
        assert QueryCache.invalidate("t") == 1
        QueryCache._write_finished(QueryCache._write_started(frozenset({"u"})))
        now[0] += 10
        assert QueryCache.stats()["entries"] == 1
        assert _counts() == counts

        QueryCache.configure()
        assert QueryCache.stats()["enabled"] is True
        assert QueryCache.get("kw") == 4
        assert QueryCache.get("k") is None
        assert QueryCache.get("ku") is None
        assert QueryCache.get("kv") is None

    def test_lowering_max_size_keeps_the_most_recently_used(self) -> None:
        for key in ("a", "b", "c", "d"):
            QueryCache.set(key, key, "t")
        assert QueryCache.get("a") == "a"
        QueryCache.configure(max_size=2)
        assert QueryCache.stats()["entries"] == 2
        assert QueryCache.get("d") == "d"
        assert QueryCache.get("a") == "a"
        assert QueryCache.get("b") is None
        assert QueryCache.get("c") is None

    def test_lowering_max_size_drops_expired_entries_first(self, now: list[float]) -> None:
        QueryCache.set("y", "y", "t")
        QueryCache.set("z", "z", "t")
        QueryCache.set("x", "x", "t", ttl=1)
        now[0] += 2
        QueryCache.configure(max_size=2)
        assert QueryCache.get("y") == "y"
        assert QueryCache.get("z") == "z"


class TestSetGet:
    def test_copies_both_ways(self) -> None:
        data = {"a": [1], "b": {"c": "d"}}
        QueryCache.set("k", data, "t")
        data["a"].append(2)
        got = QueryCache.get("k")
        assert got == {"a": [1], "b": {"c": "d"}}
        got["a"].append(3)
        again = QueryCache.get("k")
        assert again == {"a": [1], "b": {"c": "d"}}
        assert again is not got

    def test_missing_key_is_a_miss(self) -> None:
        assert QueryCache.get("nope") is None
        assert _counts() == (0, 1)

    def test_hit(self) -> None:
        QueryCache.set("k", [1, 2], "t")
        assert QueryCache.get("k") == [1, 2]
        assert _counts() == (1, 0)

    def test_cached_none_is_returned_as_none(self) -> None:
        QueryCache.set("k", None, "t")
        assert QueryCache.get("k") is None
        assert _counts() == (1, 0)

    def test_expiry(self, now: list[float]) -> None:
        QueryCache.set("k", 1, "t", ttl=10)
        now[0] += 9.5
        assert QueryCache.get("k") == 1
        now[0] += 0.5
        assert QueryCache.get("k") is None
        assert QueryCache.stats()["entries"] == 0
        assert _counts() == (1, 1)

    def test_default_ttl_applies(self, now: list[float]) -> None:
        QueryCache.configure(default_ttl=5)
        QueryCache.set("k", 1, "t")
        now[0] += 4.9
        assert QueryCache.get("k") == 1
        now[0] += 0.1
        assert QueryCache.get("k") is None

    def test_timedelta_ttl(self, now: list[float]) -> None:
        QueryCache.set("k", 1, "t", ttl=timedelta(seconds=5))
        now[0] += 5
        assert QueryCache.get("k") is None

    @pytest.mark.parametrize("ttl", [0, 0.0, timedelta(0)])
    def test_zero_ttl_stores_nothing_and_removes_the_previous_entry(self, ttl: object) -> None:
        QueryCache.set("k", 1, "t")
        QueryCache.set("k", 2, "t", ttl=ttl)  # type: ignore[arg-type]
        assert QueryCache.get("k") is None
        assert QueryCache.stats()["entries"] == 0

    def test_ttl_refused(self) -> None:
        _raises(
            ValueError,
            "QueryCache.set() ttl must be a finite number of seconds >= 0, got -1",
            QueryCache.set,
            "k",
            1,
            "t",
            ttl=-1,
        )
        _raises(
            TypeError,
            "QueryCache.set() ttl must be a number of seconds or a timedelta, not str",
            QueryCache.set,
            "k",
            1,
            "t",
            ttl="5",
        )
        assert QueryCache.stats()["entries"] == 0

    def test_key_and_table_must_be_strings(self) -> None:
        _raises(TypeError, "QueryCache.get() key must be a str, not int", QueryCache.get, 1)
        _raises(TypeError, "QueryCache.set() key must be a str, not int", QueryCache.set, 1, "v", "t")
        _raises(TypeError, "QueryCache.set() table must be a str, not int", QueryCache.set, "k", 1, 2)
        assert QueryCache.stats()["entries"] == 0

    def test_set_while_disabled_is_a_no_op(self) -> None:
        QueryCache.configure(enabled=False)
        QueryCache.set("k", 1, "t")
        QueryCache.configure()
        assert QueryCache.stats()["entries"] == 0
        assert QueryCache.get("k") is None

    def test_disabled_during_the_copy_stores_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = cache._deepcopy

        def disable_then_copy(value: Any) -> Any:
            QueryCache.configure(enabled=False)
            return real(value)

        monkeypatch.setattr(cache, "_deepcopy", disable_then_copy)
        QueryCache.set("k", 1, "t")
        QueryCache.configure()
        assert QueryCache.stats()["entries"] == 0

    def test_set_replaces_an_entry_and_its_tables(self) -> None:
        QueryCache.set("k", 1, "t")
        QueryCache.set("k", 2, "u")
        assert QueryCache.stats()["entries"] == 1
        assert QueryCache.invalidate("t") == 0
        assert QueryCache.get("k") == 2

    def test_lru_eviction(self) -> None:
        QueryCache.configure(max_size=3)
        for key in ("a", "b", "c"):
            QueryCache.set(key, key, "t")
        assert QueryCache.get("a") == "a"
        QueryCache.set("d", "d", "t")
        assert QueryCache.stats()["entries"] == 3
        assert QueryCache.get("b") is None
        assert [QueryCache.get(key) for key in ("a", "c", "d")] == ["a", "c", "d"]


class TestInvalidateClear:
    def test_invalidate_by_model_and_by_table_name(self) -> None:
        QueryCache.set("k1", 1, "qc_item")
        QueryCache.set("k2", 2, "qc_item")
        QueryCache.set("k3", 3, "other")
        assert QueryCache.invalidate(QcItem) == 2
        assert QueryCache.get("k1") is None
        QueryCache.set("k1", 1, "qc_item")
        QueryCache.set("k2", 2, "qc_item")
        assert QueryCache.invalidate("qc_item") == 2
        assert QueryCache.invalidate("qc_item") == 0
        assert QueryCache.get("k3") == 3

    def test_invalidate_removes_the_reads_that_follow_links(self, scope: int) -> None:
        _store("ka", [1], {"x"}, scope=scope, any_table=True)
        _store("kx", [2], {"x"}, scope=scope)
        assert QueryCache.invalidate("unrelated") == 1
        assert QueryCache.stats()["entries"] == 1

    def test_invalidate_refuses_anything_else(self) -> None:
        _raises(TypeError, "QueryCache.invalidate() takes a model class or a table name, not int", QueryCache.invalidate, 3)
        _raises(TypeError, "QueryCache.invalidate() takes a model class or a table name, not type", QueryCache.invalidate, dict)
        _raises(
            TypeError,
            "QueryCache.invalidate() takes a model class or a table name, not QcItem",
            QueryCache.invalidate,
            QcItem(),
        )

    def test_clear_empties_and_zeroes_the_counters(self, scope: int, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(QueryCache, "_warned_no_encoder", True)
        QueryCache.set("k", 1, "t")
        _store("ko", [1], {"t", EDGES}, scope=scope, any_table=True)
        QueryCache.get("k")
        QueryCache.get("nope")
        QueryCache.clear()
        stats = QueryCache.stats()
        assert (stats["entries"], stats["tables"], stats["hits"], stats["misses"]) == (0, 0, 0, 0)
        assert QueryCache._by_table == {}
        assert QueryCache._by_scope == {}
        assert QueryCache._any_table == set()
        assert QueryCache._warned_no_encoder is True


class TestMakeKey:
    def test_deterministic_hex_sha256(self) -> None:
        key = QueryCache.make_key("SELECT * FROM t WHERE a = $a;", {"a": 1}, "t")
        assert re.fullmatch("[0-9a-f]{64}", key)
        assert QueryCache.make_key("SELECT * FROM t WHERE a = $a;", {"a": 1}, "t") == key

    def test_differs_by_table_query_and_variables(self) -> None:
        keys = {
            QueryCache.make_key("q", {"a": 1}, "t"),
            QueryCache.make_key("q", {"a": 1}, "u"),
            QueryCache.make_key("r", {"a": 1}, "t"),
            QueryCache.make_key("q", {"a": 2}, "t"),
            QueryCache.make_key("q", {"b": 1}, "t"),
            QueryCache.make_key("q", {}, "t"),
        }
        assert len(keys) == 6

    def test_framing_cannot_be_confused(self) -> None:
        assert QueryCache.make_key("c", {}, "a\x00b") != QueryCache.make_key("b\x00c", {}, "a")
        assert QueryCache.make_key("bc", {}, "a") != QueryCache.make_key("c", {}, "ab")

    def test_the_framing_is_pinned(self) -> None:
        variables = {"b": {2, 1}, "a": RecordID("user", 1)}
        canonical = cache._canonical(encoding.encode(dict(variables)))
        assert QueryCache.make_key("SELECT 1;", variables, "t") == (
            hashlib.sha256(cbor_encode(["user", "t", "SELECT 1;", canonical])).hexdigest()
        )
        assert (
            cache.statement_key(7, "query", "q", {"a": 1})
            == hashlib.sha256(cbor_encode(["orm", 7, "query", "q", {"a": 1}])).hexdigest()
        )

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            (RecordID("user", 1), RecordID("user", "1")),
            (RecordID("user", 1), "user:1"),
            (RecordID("user", "1"), "user:1"),
            (1, 1.0),
            (1, True),
            (1.0, True),
            (Decimal("1.0"), "1.0"),
            (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC).isoformat()),
            ([1, 2], [2, 1]),
        ],
    )
    def test_type_faithful(self, first: object, second: object) -> None:
        assert QueryCache.make_key("q", {"v": first}, "t") != QueryCache.make_key("q", {"v": second}, "t")
        assert cache.statement_key(1, "query", "q", {"v": first}) != cache.statement_key(1, "query", "q", {"v": second})

    def test_list_tuple_and_set_are_one_array(self) -> None:
        keys = {QueryCache.make_key("q", {"v": value}, "t") for value in ([1, 2], (1, 2), {1, 2})}
        assert len(keys) == 1
        keys = {cache.statement_key(1, "query", "q", {"v": value}) for value in ([1, 2], (1, 2), {1, 2})}
        assert len(keys) == 1

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ({"a": 1, "b": 2}, {"b": 2, "a": 1}),
            ({"o": {"x": 1, "y": 2}}, {"o": {"y": 2, "x": 1}}),
            ({"l": [{"x": 1, "y": 2}]}, {"l": [{"y": 2, "x": 1}]}),
        ],
    )
    def test_mapping_key_order_is_ignored(self, first: dict[str, Any], second: dict[str, Any]) -> None:
        assert QueryCache.make_key("q", first, "t") == QueryCache.make_key("q", second, "t")
        assert cache.statement_key(1, "query", "q", first) == cache.statement_key(1, "query", "q", second)
        assert QueryCache.make_key("q", {"v": first}, "t") == QueryCache.make_key("q", {"v": second}, "t")

    def test_canonical(self) -> None:
        assert list(cache._canonical({"b": 1, "a": {"d": 2, "c": 3}})) == ["a", "b"]
        assert list(cache._canonical({"b": 1, "a": {"d": 2, "c": 3}})["a"]) == ["c", "d"]
        assert cache._canonical((1, (2, 3))) == [1, [2, 3]]
        mixed = {2: "x", 1: "y"}
        assert cache._canonical(mixed) is mixed
        assert cache._canonical("s") == "s"

    def test_the_orm_and_user_domains_never_meet(self) -> None:
        orm = cache.statement_key(1, "query", "q", {})
        assert orm is not None
        for table in ("t", "", "query", "orm", "user", "1", "q"):
            assert QueryCache.make_key("q", {}, table) != orm

    def test_mapping_and_string_types(self) -> None:
        _raises(TypeError, "QueryCache.make_key() query must be a str, not int", QueryCache.make_key, 1, {}, "t")
        _raises(TypeError, "QueryCache.make_key() table must be a str, not int", QueryCache.make_key, "q", {}, 2)
        _raises(TypeError, "QueryCache.make_key() variables must be a mapping, not list", QueryCache.make_key, "q", [], "t")

    def test_a_value_the_sdk_cannot_send(self) -> None:
        with pytest.raises(TypeError) as info:
            QueryCache.make_key("q", {"a": object()}, "t")
        cause = info.value.__cause__
        assert isinstance(cause, BufferError)
        assert str(info.value) == f"QueryCache.make_key(): the variables hold a value the SDK cannot send ({cause})"

    def test_no_encoder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cache, "cbor_encode", None)
        _raises(SurrealDbError, _C9, QueryCache.make_key, "q", {}, "t")

    def test_statement_key_without_encoder_warns_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(cache, "cbor_encode", None)
        with caplog.at_level(logging.WARNING, logger="surreal_orm_lite.cache"):
            assert cache.statement_key(1, "query", "q", {}) is None
            assert cache.statement_key(1, "query", "q", {}) is None
        assert [r.getMessage() for r in caplog.records if r.name == "surreal_orm_lite.cache"] == [_W2]
        assert all(r.levelno == logging.WARNING for r in caplog.records if r.name == "surreal_orm_lite.cache")

    def test_statement_key_of_a_value_the_sdk_cannot_send(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG, logger="surreal_orm_lite.cache"):
            assert cache.statement_key(1, "query", "q", {"a": object()}) is None
        assert not [r for r in caplog.records if r.name == "surreal_orm_lite.cache"]

    def test_statement_key_tells_scopes_and_kinds_apart(self) -> None:
        keys = {
            cache.statement_key(1, "query", "q", {}),
            cache.statement_key(2, "query", "q", {}),
            cache.statement_key(1, "select", "q", {}),
            cache.statement_key(1, "query", "r", {}),
        }
        assert len(keys) == 4


class TestFollowsLinks:
    @pytest.mark.parametrize(
        ("statement", "expected"),
        [
            ("SELECT * FROM qa_book WHERE price >= $_f0 ORDER BY title ASC;", False),
            ("SELECT * FROM qa_book WHERE author.name = $_f0;", True),
            ("SELECT * FROM qa_author WHERE address.city = $_f0;", True),
            ("SELECT title, author.name FROM qa_book;", True),
            ("SELECT * FROM qa_book FETCH author;", False),
            (
                "{ LET $_sq0 = (SELECT VALUE id FROM qa_author WHERE name = $_f0); "
                "(SELECT * FROM qa_book WHERE author IN $_sq0) };",
                False,
            ),
            (
                "{ LET $_sq0 = (SELECT id AS _sqv, name FROM qa_author ORDER BY name ASC LIMIT 2)._sqv; "
                "(SELECT * FROM qa_book WHERE author IN $_sq0) };",
                False,
            ),
            ("SELECT author, count() AS n, math::sum(price) AS s FROM qa_book GROUP BY author;", False),
            ("SELECT *, IF loc.is_point() THEN geo::distance(loc, $_gd0) END AS d FROM qa_place;", False),
            (
                "SELECT *, IF author.loc.is_point() THEN geo::distance(author.loc, $_gd0) END AS d FROM qa_place;",
                True,
            ),
            ("SELECT * FROM t WHERE price = math::floor(1.5);", False),
            ("SELECT * FROM t WHERE n = count(<-likes);", True),
            ("SELECT * FROM t WHERE $parent.id = x;", True),
            ("SELECT * FROM t WHERE x = $_f0 LIMIT 1;", False),
            ("SELECT * FROM t WHERE d > 1.5s;", False),
        ],
    )
    def test_statements(self, statement: str, expected: bool) -> None:
        assert cache.follows_links(statement) is expected

    def test_graph_arrows(self) -> None:
        assert cache.follows_links("SELECT ->likes->post AS liked FROM user;") is True
        assert cache.follows_links("SELECT <-likes<-user AS fans FROM post;") is True

    @pytest.mark.parametrize(
        ("build", "expected"),
        [
            pytest.param(lambda: QcBook.objects().filter(price__gte=3)._compile_query(), False, id="filter"),
            pytest.param(lambda: QcBook.objects().filter(**{"author.name": "Ada"})._compile_query(), True, id="dotted_filter"),
            pytest.param(
                lambda: QcBook.objects().filter(Q(**{"author.name": "Ada"}) | Q(price=1))._compile_query(), True, id="dotted_q"
            ),
            pytest.param(lambda: QcBook.objects().select("title", "author.name")._compile_query(), True, id="dotted_select"),
            pytest.param(lambda: QcBook.objects().order_by("author.name")._compile_query(), True, id="dotted_order_by"),
            pytest.param(
                lambda: QcBook.objects().values("author.name").annotate(n=Count())._compile_group_by_query(),
                True,
                id="dotted_values",
            ),
            pytest.param(
                lambda: QcBook.objects().values("author").annotate(n=Count())._compile_group_by_query(), False, id="values"
            ),
            pytest.param(
                lambda: QcPlace.objects().annotate(d=GeoDistance("loc", (1.0, 2.0)))._compile_query(), False, id="geo_distance"
            ),
            pytest.param(
                lambda: QcPlace.objects().annotate(d=GeoDistance("owner.loc", (1.0, 2.0)))._compile_query(),
                True,
                id="dotted_geo_distance",
            ),
            pytest.param(
                lambda: QcBook.objects().filter(author__in=Subquery(QcAuthor.objects().filter(name="Ada")))._compile_query(),
                False,
                id="subquery",
            ),
            pytest.param(
                lambda: (
                    QcBook.objects()
                    .filter(author__in=Subquery(QcAuthor.objects().filter(**{"profile.city": "Paris"})))
                    ._compile_query()
                ),
                True,
                id="dotted_subquery",
            ),
            pytest.param(
                lambda: QcBook.objects().filter(price__gte=3)._compile_aggregation_query("count()"), False, id="count"
            ),
            pytest.param(lambda: QcBook.objects().fetch("author")._compile_query(), False, id="fetch"),
        ],
    )
    def test_compiled_statements(self, build: Callable[[], tuple[str, dict[str, Any]]], expected: bool) -> None:
        statement, _ = build()
        assert cache.follows_links(statement) is expected


# --- AC-3: read bookkeeping ---------------------------------------------------------------------------

_SPOILERS: dict[str, Callable[[int], object]] = {
    "invalidate": lambda scope: QueryCache.invalidate("t"),
    "clear": lambda scope: QueryCache.clear(),
    "disabled": lambda scope: QueryCache.configure(enabled=False),
    "disabled_then_enabled": lambda scope: (QueryCache.configure(enabled=False), QueryCache.configure()),
    "scope_retired": lambda scope: QueryCache._new_scope(scope),
    "write": lambda scope: QueryCache._write_finished(QueryCache._write_started(frozenset({"t"}))),
    "write_every_table": lambda scope: QueryCache._write_finished(QueryCache._write_started(None)),
}


class TestReadBookkeeping:
    def test_miss_ticket_store_hit(self, scope: int) -> None:
        hit, data, ticket = QueryCache._begin_read("k", frozenset({"t"}), False, scope, 60.0)
        assert (hit, data) == (False, None)
        assert ticket is not None
        rows = [{"id": 1, "tags": ["a"]}]
        QueryCache._finish_read(ticket, rows)
        rows[0]["tags"].append("b")

        hit, first, ticket = QueryCache._begin_read("k", frozenset({"t"}), False, scope, 60.0)
        assert hit is True
        assert ticket is None
        assert first == [{"id": 1, "tags": ["a"]}]
        first[0]["tags"].append("c")
        hit, second, _ = QueryCache._begin_read("k", frozenset({"t"}), False, scope, 60.0)
        assert second == [{"id": 1, "tags": ["a"]}]
        assert second is not first
        assert _counts() == (2, 1)
        assert QueryCache.stats()["entries"] == 1

    @pytest.mark.parametrize("spoiler", list(_SPOILERS))
    def test_the_store_is_refused(self, scope: int, spoiler: str) -> None:
        _, _, ticket = QueryCache._begin_read("k", frozenset({"t"}), False, scope, 60.0)
        assert ticket is not None
        _SPOILERS[spoiler](scope)
        QueryCache._finish_read(ticket, [1])
        assert "k" not in QueryCache._entries
        assert QueryCache.stats()["entries"] == 0

    def test_a_write_to_another_table(self, scope: int) -> None:
        _, _, plain = QueryCache._begin_read("plain", frozenset({"t"}), False, scope, 60.0)
        _, _, linked = QueryCache._begin_read("linked", frozenset({"t"}), True, scope, 60.0)
        assert plain is not None
        assert linked is not None
        QueryCache._write_finished(QueryCache._write_started(frozenset({"u"})))
        QueryCache._finish_read(plain, [1])
        QueryCache._finish_read(linked, [2])
        assert set(QueryCache._entries) == {"plain"}

    def test_disabled_bypasses_counts_nothing_and_keeps_the_entry(self, scope: int) -> None:
        _store("k", [1], {"t"}, scope=scope)
        counts = _counts()
        QueryCache.configure(enabled=False)
        assert QueryCache._begin_read("k", frozenset({"t"}), False, scope, 60.0) == (False, None, None)
        assert QueryCache._begin_read("new", frozenset({"t"}), False, scope, 60.0) == (False, None, None)
        assert _counts() == counts
        assert QueryCache.stats()["entries"] == 1
        QueryCache.configure()
        assert QueryCache._begin_read("k", frozenset({"t"}), False, scope, 60.0) == (True, [1], None)

    def test_a_write_in_flight_bypasses(self, scope: int) -> None:
        _store("k", [1], {"t"}, scope=scope)
        _, _, before = QueryCache._begin_read("k2", frozenset({"t"}), False, scope, 60.0)
        assert before is not None
        token = QueryCache._write_started(frozenset({"t"}))
        assert QueryCache._begin_read("k", frozenset({"t"}), False, scope, 60.0) == (False, None, None)
        assert QueryCache._begin_read("k3", frozenset({"t"}), False, scope, 60.0) == (False, None, None)
        assert _counts() == (0, 4)
        QueryCache._finish_read(before, [2])
        assert "k2" not in QueryCache._entries
        # A plain read of another table is not concerned; one that follows links is.
        _, _, other = QueryCache._begin_read("o", frozenset({"u"}), False, scope, 60.0)
        assert other is not None
        assert QueryCache._begin_read("l", frozenset({"u"}), True, scope, 60.0) == (False, None, None)
        QueryCache._write_finished(token)
        assert QueryCache._in_flight == {}

    def test_a_write_to_every_table_in_flight_bypasses_every_read(self, scope: int) -> None:
        token = QueryCache._write_started(None)
        assert QueryCache._in_flight_all == 1
        assert QueryCache._begin_read("k", frozenset({"t"}), False, scope, 60.0) == (False, None, None)
        QueryCache._write_finished(token)
        assert QueryCache._in_flight_all == 0

    def test_a_write_to_every_table_empties_everything(self, scope: int) -> None:
        other = QueryCache._new_scope(None)
        _store("a", [1], {"t"}, scope=scope)
        _store("b", [2], {"u", EDGES}, scope=other)
        QueryCache.set("c", 3, "v")
        QueryCache._write_finished(QueryCache._write_started(None))
        assert QueryCache.stats()["entries"] == 0
        assert QueryCache._by_table == {}
        assert QueryCache._by_scope == {}

    def test_nested_writes_keep_counting(self, scope: int) -> None:
        first = QueryCache._write_started(frozenset({"t"}))
        second = QueryCache._write_started(frozenset({"t", "u"}))
        assert QueryCache._in_flight == {"t": 2, "u": 1}
        QueryCache._write_finished(first)
        assert QueryCache._in_flight == {"t": 1, "u": 1}
        assert QueryCache._begin_read("k", frozenset({"t"}), False, scope, 60.0) == (False, None, None)
        QueryCache._write_finished(second)
        assert QueryCache._in_flight == {}

    def test_reader_ttl(self, scope: int, now: list[float]) -> None:
        now[0] = 0.0
        _store("k", ["old"], {"t"}, scope=scope, ttl=300)
        now[0] = 6.0
        assert QueryCache._begin_read("k", frozenset({"t"}), False, scope, 300.0) == (True, ["old"], None)
        hit, data, ticket = QueryCache._begin_read("k", frozenset({"t"}), False, scope, 5.0)
        assert (hit, data) == (False, None)
        assert ticket is not None
        QueryCache._finish_read(ticket, ["new"])
        assert QueryCache.stats()["entries"] == 1
        assert QueryCache._begin_read("k", frozenset({"t"}), False, scope, 5.0) == (True, ["new"], None)
        # The new entry lives for its reader's TTL: 5 s.
        now[0] = 11.0
        assert QueryCache._begin_read("k", frozenset({"t"}), False, scope, 300.0)[0] is False
        assert QueryCache.stats()["entries"] == 0

    def test_a_retired_scope_bypasses(self, scope: int) -> None:
        QueryCache._retire_scope(scope)
        assert QueryCache._begin_read("k", frozenset({"t"}), False, scope, 60.0) == (False, None, None)
        assert _counts() == (0, 1)

    def test_count_miss_only_when_enabled(self) -> None:
        QueryCache._count_miss()
        assert _counts() == (0, 1)
        QueryCache.configure(enabled=False)
        QueryCache._count_miss()
        QueryCache.configure()
        assert _counts() == (0, 1)

    def test_default(self) -> None:
        assert QueryCache._default() == 60.0
        QueryCache.configure(default_ttl=timedelta(seconds=3))
        assert QueryCache._default() == 3.0

    def test_edges(self, scope: int) -> None:
        _store("edge", [1], {"t", EDGES}, scope=scope)
        assert QueryCache.stats()["tables"] == 1
        QueryCache._write_finished(QueryCache._write_started(frozenset({"u"})))
        assert "edge" in QueryCache._entries
        QueryCache._write_finished(QueryCache._write_started(frozenset({"u", EDGES})))
        assert "edge" not in QueryCache._entries

    def test_a_copy_that_fails_never_fails_the_read(self, scope: int, monkeypatch: pytest.MonkeyPatch) -> None:
        _store("stored", [1], {"t"}, scope=scope)

        def refuse(value: Any) -> Any:
            raise RuntimeError("no copy")

        monkeypatch.setattr(cache, "_deepcopy", refuse)
        _, _, ticket = QueryCache._begin_read("new", frozenset({"t"}), False, scope, 60.0)
        assert ticket is not None
        QueryCache._finish_read(ticket, [2])
        assert "new" not in QueryCache._entries
        counts = _counts()
        assert QueryCache._begin_read("stored", frozenset({"t"}), False, scope, 60.0) == (False, None, None)
        assert "stored" not in QueryCache._entries
        assert _counts() == (counts[0], counts[1] + 1)

    def test_a_failing_clock_never_fails_the_read(self, scope: int, monkeypatch: pytest.MonkeyPatch) -> None:
        _, _, ticket = QueryCache._begin_read("k", frozenset({"t"}), False, scope, 60.0)
        assert ticket is not None

        def broken() -> float:
            raise RuntimeError("no clock")

        monkeypatch.setattr(cache, "_clock", broken)
        assert QueryCache._begin_read("k", frozenset({"t"}), False, scope, 60.0) == (False, None, None)
        QueryCache._finish_read(ticket, [1])
        assert "k" not in QueryCache._entries

    def test_a_hit_whose_copy_fails_while_disabled(self, scope: int, monkeypatch: pytest.MonkeyPatch) -> None:
        _store("k", [1], {"t"}, scope=scope)
        counts = _counts()

        def disable_then_fail(value: Any) -> Any:
            QueryCache.configure(enabled=False)
            raise RuntimeError("no copy")

        monkeypatch.setattr(cache, "_deepcopy", disable_then_fail)
        assert QueryCache._begin_read("k", frozenset({"t"}), False, scope, 60.0) == (False, None, None)
        QueryCache.configure()
        assert "k" not in QueryCache._entries
        assert _counts() == counts

    def test_a_hit_whose_copy_fails_after_a_clear(self, scope: int, monkeypatch: pytest.MonkeyPatch) -> None:
        _store("k", [1], {"t"}, scope=scope)
        real = cache._deepcopy
        calls: list[int] = []

        def clear_then_fail(value: Any) -> Any:
            if calls:  # the set() below copies too
                return real(value)
            calls.append(1)
            QueryCache.clear()
            QueryCache.set("k", "user", "t")
            raise RuntimeError("no copy")

        monkeypatch.setattr(cache, "_deepcopy", clear_then_fail)
        assert QueryCache._begin_read("k", frozenset({"t"}), False, scope, 60.0) == (False, None, None)
        monkeypatch.undo()
        assert QueryCache.get("k") == "user"  # another entry under that key since: not the one that failed
        assert _counts() == (1, 1)

    def test_a_hit_disabled_during_its_copy_is_not_counted(self, scope: int, monkeypatch: pytest.MonkeyPatch) -> None:
        _store("k", [1], {"t"}, scope=scope)
        counts = _counts()
        real = cache._deepcopy

        def disable_then_copy(value: Any) -> Any:
            QueryCache.configure(enabled=False)
            return real(value)

        monkeypatch.setattr(cache, "_deepcopy", disable_then_copy)
        assert QueryCache._begin_read("k", frozenset({"t"}), False, scope, 60.0) == (True, [1], None)
        assert _counts() == counts


# --- AC-4 (unit part): scopes of clients --------------------------------------------------------------


class _Client:
    """A stand-in for an SDK client: a plain object that accepts weak references."""


class _Unhashable:
    """Weakly referenceable, but unhashable: no scope either."""

    __hash__ = None  # type: ignore[assignment]

    def __eq__(self, other: object) -> bool:
        return self is other


class TestScopeCells:
    def test_bind_scope_mints_a_new_scope_at_every_call(self) -> None:
        fake = _Client()
        first = cache.bind_scope(fake)
        assert first is not None
        assert first in QueryCache._live_scopes
        assert cache.client_scope(fake) == first
        _store("k", [1], {"t"}, scope=first)
        second = cache.bind_scope(fake)
        assert second is not None
        assert second != first
        assert first not in QueryCache._live_scopes
        assert second in QueryCache._live_scopes
        assert cache.client_scope(fake) == second
        assert QueryCache.stats()["entries"] == 0
        cache.retire_client(fake)

    @pytest.mark.parametrize("client", [1, "client", _Unhashable()])
    def test_a_client_that_cannot_be_weakly_keyed_gets_no_scope(self, client: object) -> None:
        live = set(QueryCache._live_scopes)
        assert cache.bind_scope(client) is None
        assert cache.client_scope(client) is None
        assert QueryCache._live_scopes == live
        cache.retire_client(client)

    def test_retire_client(self) -> None:
        fake = _Client()
        scope = cache.bind_scope(fake)
        assert scope is not None
        _store("k", [1], {"t"}, scope=scope)
        cache.retire_client(fake)
        assert scope not in QueryCache._live_scopes
        assert QueryCache.stats()["entries"] == 0
        assert cache.client_scope(fake) is None
        cache.retire_client(fake)
        assert cache.client_scope(_Client()) is None

    def test_a_collected_client_is_retired_by_the_next_locked_call(self) -> None:
        fake = _Client()
        scope = cache.bind_scope(fake)
        assert scope is not None
        _store("k", [1], {"t"}, scope=scope)
        del fake
        gc.collect()
        assert scope in cache._retired
        QueryCache.stats()
        assert scope not in cache._retired
        assert scope not in QueryCache._live_scopes
        assert "k" not in QueryCache._entries

    def test_a_retired_client_is_not_retired_again_when_collected(self) -> None:
        fake = _Client()
        scope = cache.bind_scope(fake)
        cache.retire_client(fake)
        del fake
        gc.collect()
        assert scope not in cache._retired


# --- AC-4 (manager): a scope per identity stamp, retired where the manager drops the client -------------


class _ClosableClient(_Client):
    """A stand-in for an SDK client the manager may close."""

    closed = False

    async def close(self) -> None:
        self.closed = True


def _manager_clients() -> Any:
    """The manager's per-loop clients (its private ``WeakKeyDictionary``, reached through the mangled name)."""
    return default_connection_class()._SurrealDBConnectionManager__clients  # type: ignore[attr-defined]


def _stamped(client: Any) -> int:
    """Stamp ``client`` as the manager does when it publishes one; return its live scope."""
    SurrealDBConnectionManager._stamp_identity(client, replayable=True)
    scope = SurrealDBConnectionManager._cache_scope(client)
    assert scope is not None
    assert scope in QueryCache._live_scopes
    return scope


def _retired(scope: int, key: str) -> bool:
    """Whether ``scope`` is no longer live and the entry stored under it (``key``) has left memory."""
    return scope not in QueryCache._live_scopes and key not in QueryCache._entries


@contextlib.asynccontextmanager
async def _bare_manager() -> AsyncIterator[Any]:
    """The manager with no settings and no client, before and after; yields its per-loop clients (for fakes)."""
    await SurrealDBConnectionManager.unset_connection()
    try:
        yield _manager_clients()
    finally:
        await SurrealDBConnectionManager.unset_connection()


class TestScopeUnit:
    def test_every_stamp_mints_a_new_scope(self) -> None:
        fake = _Client()
        first = _stamped(fake)
        _store("k", [1], {"t"}, scope=first)
        second = _stamped(fake)
        assert second != first
        assert first not in QueryCache._live_scopes
        assert "k" not in QueryCache._entries
        SurrealDBConnectionManager._stamp_identity(fake, replayable=False, fingerprint="other")
        third = SurrealDBConnectionManager._cache_scope(fake)
        assert third is not None
        assert third not in (first, second)
        assert second not in QueryCache._live_scopes
        cache.retire_client(fake)
        assert third not in QueryCache._live_scopes

    def test_a_client_that_cannot_be_weakly_referenced_gets_no_scope(self) -> None:
        live = set(QueryCache._live_scopes)
        SurrealDBConnectionManager._stamp_identity(1, replayable=True)
        assert SurrealDBConnectionManager._cache_scope(1) is None
        assert QueryCache._live_scopes == live

    def test_an_unstamped_client_has_no_scope(self) -> None:
        assert SurrealDBConnectionManager._cache_scope(_Client()) is None

    async def test_close_connection_retires_the_running_loops_client(self) -> None:
        async with _bare_manager() as clients:
            fake = _ClosableClient()
            scope = _stamped(fake)
            clients[asyncio.get_running_loop()] = fake
            _store("k", [1], {"t"}, scope=scope)
            await SurrealDBConnectionManager.close_connection()
            assert fake.closed
            assert _retired(scope, "k")
            assert SurrealDBConnectionManager._cache_scope(fake) is None

    async def test_close_all_connections_retires_every_loops_client(self) -> None:
        other_loop = asyncio.new_event_loop()
        try:
            async with _bare_manager() as clients:
                mine, other = _ClosableClient(), _ClosableClient()
                mine_scope, other_scope = _stamped(mine), _stamped(other)
                clients[asyncio.get_running_loop()] = mine
                clients[other_loop] = other
                _store("mine", [1], {"t"}, scope=mine_scope)
                _store("other", [2], {"t"}, scope=other_scope)
                await SurrealDBConnectionManager.close_all_connections()
                assert mine.closed
                assert not other.closed  # another live loop's client is dropped, not awaited
                assert _retired(mine_scope, "mine")
                assert _retired(other_scope, "other")
        finally:
            other_loop.close()

    async def test_a_closed_loops_client_is_retired_when_pruned(self) -> None:
        async with _bare_manager() as clients:
            dead_loop = asyncio.new_event_loop()
            dead_loop.close()
            fake = _ClosableClient()
            scope = _stamped(fake)
            clients[dead_loop] = fake
            _store("k", [1], {"t"}, scope=scope)
            assert not SurrealDBConnectionManager.is_connected()  # prunes the closed loop's client
            assert dead_loop not in clients
            assert _retired(scope, "k")

    async def test_a_dead_client_is_retired_when_discarded(self) -> None:
        async with _bare_manager() as clients:
            loop = asyncio.get_running_loop()
            fake = _ClosableClient()
            scope = _stamped(fake)
            clients[loop] = fake
            _store("k", [1], {"t"}, scope=scope)
            await SurrealDBConnectionManager._discard_dead_client(loop, fake)
            assert loop not in clients
            assert _retired(scope, "k")

    async def test_a_stale_client_is_retired_when_its_replacement_cannot_connect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def refused(*args: Any, **kwargs: Any) -> Any:
            raise SurrealDbConnectionError("Can't connect to the database.")

        async with _bare_manager() as clients:
            loop = asyncio.get_running_loop()
            fake = _ClosableClient()
            scope = _stamped(fake)
            clients[loop] = fake
            _store("k", [1], {"t"}, scope=scope)
            SurrealDBConnectionManager.set_connection(
                url=_ws_url(), user="root", password="root", namespace="ns", database="db"
            )
            monkeypatch.setattr(SurrealDBConnectionManager, "_open_client", refused)
            with pytest.raises(SurrealDbConnectionError):
                await SurrealDBConnectionManager._connect_for_loop(loop)
            assert fake.closed
            assert loop not in clients
            assert _retired(scope, "k")


# --- AC-3: threads ------------------------------------------------------------------------------------


class TestThreads:
    def test_the_indexes_match_the_entries(self) -> None:
        QueryCache.configure(max_size=30)
        tables = ["a", "b", "c", EDGES]
        keys = [f"k{i}" for i in range(50)]
        scopes = [QueryCache._new_scope(None) for _ in range(4)]
        errors: list[BaseException] = []

        def work(seed: int) -> None:
            rng = random.Random(seed)
            mine = list(scopes)
            try:
                for _ in range(2000):
                    op = rng.randrange(12)
                    key = rng.choice(keys)
                    picked = frozenset(rng.sample(tables, rng.randint(1, 2)))
                    if op == 0:
                        QueryCache.set(key, {"v": [rng.random()]}, rng.choice(tables[:3]), ttl=rng.choice([0, 5, 60]))
                    elif op == 1:
                        QueryCache.get(key)
                    elif op == 2:
                        QueryCache.invalidate(rng.choice(tables[:3]))
                    elif op in (3, 4, 5):
                        _, _, ticket = QueryCache._begin_read(key, picked, rng.random() < 0.2, rng.choice(mine), 60.0)
                        if ticket is not None:
                            QueryCache._finish_read(ticket, [{"k": key}])
                    elif op == 6:
                        QueryCache._write_finished(QueryCache._write_started(None if rng.random() < 0.05 else picked))
                    elif op == 7:
                        index = rng.randrange(len(mine))
                        mine[index] = QueryCache._new_scope(mine[index])
                    elif op == 8:
                        QueryCache._retire_scope(rng.choice(mine))
                    elif op == 9:
                        cache._retired.append(rng.choice(mine))
                    elif op == 10:
                        QueryCache.stats()
                    else:
                        QueryCache._count_miss()
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(seed,)) for seed in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []

        QueryCache.stats()
        entries = QueryCache._entries
        by_table: dict[str, set[str]] = {}
        by_scope: dict[int, set[str]] = {}
        for key, entry in entries.items():
            for table in entry.tables:
                by_table.setdefault(table, set()).add(key)
            if entry.scope is not None:
                by_scope.setdefault(entry.scope, set()).add(key)
                assert entry.scope in QueryCache._live_scopes
        assert QueryCache._by_table == by_table
        assert QueryCache._by_scope == by_scope
        assert QueryCache._any_table == {key for key, entry in entries.items() if entry.any_table}
        assert len(entries) <= 30
        assert not cache._retired
        assert QueryCache._in_flight == {}
        assert QueryCache._in_flight_all == 0


# --- spec §6.1: writing() and invalidates() -----------------------------------------------------------


def _in_flight() -> tuple[dict[str, int], int]:
    return dict(QueryCache._in_flight), QueryCache._in_flight_all


class _Tx:
    """A transaction as ``writing()`` sees one: it notes the tables written."""

    def __init__(self) -> None:
        self.noted: list[frozenset[str] | None] = []

    def _note_cache_write(self, tables: frozenset[str] | None) -> None:
        self.noted.append(tables)


class TestWriting:
    async def test_in_flight_inside_invalidated_after(self) -> None:
        QueryCache.set("k", 1, "t")
        QueryCache.set("other", 2, "u")
        async with cache.writing({"t"}):
            assert _in_flight() == ({"t": 1}, 0)
            assert QueryCache.stats()["entries"] == 2
        assert _in_flight() == ({}, 0)
        assert QueryCache.get("k") is None
        assert QueryCache.get("other") == 2

    async def test_get_hides_the_entries_of_a_table_in_flight(self) -> None:
        """From the start of a write to its end (its ``post_*`` handlers included), ``get()`` answers ``None``, a
        miss, for an entry tagged with a table it writes, an entry set meanwhile included; other tables are served."""
        QueryCache.set("k", 1, "t")
        QueryCache.set("other", 2, "u")
        async with cache.writing({"t"}):
            assert QueryCache.get("k") is None
            QueryCache.set("during", 3, "t")
            assert QueryCache.get("during") is None
            assert QueryCache.get("other") == 2
            assert _counts() == (1, 2)
        assert QueryCache.get("k") is None
        assert QueryCache.get("during") is None
        assert QueryCache.get("other") == 2

    async def test_get_hides_every_entry_while_every_table_is_in_flight(self) -> None:
        QueryCache.set("k", 1, "t")
        async with cache.writing(None):
            assert QueryCache.get("k") is None
            QueryCache.set("during", 2, "u")
            assert QueryCache.get("during") is None
            assert _counts() == (0, 2)
        assert QueryCache.stats()["entries"] == 0

    async def test_invalidated_after_an_exception(self) -> None:
        QueryCache.set("k", 1, "t")
        with pytest.raises(RuntimeError, match="boom"):
            async with cache.writing(["t"]):
                raise RuntimeError("boom")
        assert _in_flight() == ({}, 0)
        assert QueryCache.get("k") is None

    @pytest.mark.parametrize("tables", [(), [], set(), frozenset()])
    async def test_no_table_is_a_no_op(self, tables: Any) -> None:
        QueryCache.set("k", 1, "t")
        async with cache.writing(tables):
            assert _in_flight() == ({}, 0)
        assert QueryCache.get("k") == 1

    async def test_every_table(self) -> None:
        QueryCache.set("k", 1, "t")
        async with cache.writing(None):
            assert _in_flight() == ({}, 1)
        assert _in_flight() == ({}, 0)
        assert QueryCache.stats()["entries"] == 0

    async def test_noted_on_a_transaction(self) -> None:
        QueryCache.set("k", 1, "t")
        tx = _Tx()
        async with cache.writing({"t", EDGES}, tx=tx):
            assert _in_flight() == ({}, 0)
        assert tx.noted == [frozenset({"t", EDGES})]
        assert QueryCache.get("k") == 1

    async def test_every_table_noted_on_a_transaction(self) -> None:
        tx = _Tx()
        async with cache.writing(None, tx=tx):
            assert _in_flight() == ({}, 0)
        assert tx.noted == [None]

    async def test_an_object_without_note_is_no_transaction(self) -> None:
        QueryCache.set("k", 1, "t")
        async with cache.writing({"t"}, tx=object()):
            assert _in_flight() == ({"t": 1}, 0)
        assert QueryCache.get("k") is None

    async def test_the_transaction_is_recognised_by_its_class(self) -> None:
        """Never by asking the object: a write refused before it reaches its transaction (a time-travel snapshot,
        v0.26.0) leaves an object that answers through ``__getattr__`` untouched, as no transaction."""

        class _Untouchable:
            def __getattr__(self, name: str) -> Any:
                raise AssertionError(f"the transaction was used ({name})")

        QueryCache.set("k", 1, "t")
        async with cache.writing({"t"}, tx=_Untouchable()):
            assert _in_flight() == ({"t": 1}, 0)
        assert QueryCache.get("k") is None


class _Writer:
    """A model as ``invalidates()`` sees one."""

    @classmethod
    def get_table_name(cls) -> str:
        return "w"

    @staticmethod
    def _edge_names(edge: object) -> list[str]:
        if edge == "bad":
            raise ValueError("bad edge")
        return list(edge) if isinstance(edge, list) else [str(edge)]

    @cache.invalidates("self")
    async def save(self, tx: Any = None) -> Any:
        """Save the record."""
        return _in_flight()

    @cache.invalidates("self")
    async def plain(self, value: int = 0) -> Any:
        return _in_flight()

    @cache.invalidates("self", deletes=True)
    async def delete(self, tx: Any = None) -> Any:
        return _in_flight()

    @cache.invalidates("edge")
    async def relate(self, edge: Any, to: Any = None, tx: Any = None) -> Any:
        return _in_flight()

    @classmethod
    @cache.invalidates("cls")
    async def define(cls, tx: Any = None) -> Any:
        """Define the table."""
        return _in_flight()

    @classmethod
    @cache.invalidates("cls")
    async def signup(cls, data: Any) -> Any:
        return _in_flight()


class _OtherWriter(_Writer):
    @classmethod
    def get_table_name(cls) -> str:
        return "w2"


class _Qs:
    """A queryset as ``invalidates()`` sees one."""

    def __init__(self, table: str, tx: Any = None) -> None:
        self._model_table = table
        self._tx = tx

    @cache.invalidates("queryset")
    async def bulk_update(self) -> Any:
        return _in_flight()

    @cache.invalidates("queryset", deletes=True)
    async def bulk_delete(self) -> Any:
        return _in_flight()

    @cache.invalidates("table", deletes=True)
    async def delete_table(self) -> Any:
        return _in_flight()


class _Manager:
    @classmethod
    @cache.invalidates("all")
    async def signup(cls, data: Any) -> Any:
        return _in_flight()


class TestInvalidatesDecorator:
    async def test_self_without_a_transaction(self) -> None:
        QueryCache.set("k", 1, "w")
        QueryCache.set("k2", 2, "w2")
        assert await _Writer().save() == ({"w": 1}, 0)
        assert QueryCache.get("k") is None
        assert QueryCache.get("k2") == 2
        assert await _OtherWriter().save() == ({"w2": 1}, 0)
        assert QueryCache.get("k2") is None

    async def test_self_with_a_transaction(self) -> None:
        QueryCache.set("k", 1, "w")
        tx = _Tx()
        assert await _Writer().save(tx=tx) == ({}, 0)
        assert await _Writer().save(tx) == ({}, 0)
        assert tx.noted == [frozenset({"w"}), frozenset({"w"})]
        assert QueryCache.get("k") == 1

    async def test_self_without_a_tx_parameter(self) -> None:
        assert await _Writer().plain(3) == ({"w": 1}, 0)

    async def test_deletes_add_edges(self) -> None:
        assert await _Writer().delete() == ({"w": 1, EDGES: 1}, 0)
        tx = _Tx()
        await _Writer().delete(tx=tx)
        assert tx.noted == [frozenset({"w", EDGES})]

    async def test_cls_with_a_tx_parameter(self) -> None:
        QueryCache.set("k", 1, "w")
        tx = _Tx()
        assert await _Writer.define(tx=tx) == ({}, 0)
        assert tx.noted == [frozenset({"w"})]
        assert QueryCache.get("k") == 1
        assert await _Writer.define() == ({"w": 1}, 0)
        assert QueryCache.get("k") is None
        assert await _OtherWriter.define() == ({"w2": 1}, 0)

    async def test_cls_without_a_tx_parameter(self) -> None:
        assert await _Writer.signup({"a": 1}) == ({"w": 1}, 0)

    async def test_queryset(self) -> None:
        QueryCache.set("k", 1, "q")
        assert await _Qs("q").bulk_update() == ({"q": 1}, 0)
        assert QueryCache.get("k") is None
        QueryCache.set("k", 1, "q")
        tx = _Tx()
        assert await _Qs("q", tx).bulk_update() == ({}, 0)
        assert await _Qs("q", tx).bulk_delete() == ({}, 0)
        assert tx.noted == [frozenset({"q"}), frozenset({"q", EDGES})]
        assert QueryCache.get("k") == 1
        assert await _Qs("q").bulk_delete() == ({"q": 1, EDGES: 1}, 0)

    async def test_table_ignores_the_transaction(self) -> None:
        QueryCache.set("k", 1, "q")
        tx = _Tx()
        assert await _Qs("q", tx).delete_table() == ({"q": 1, EDGES: 1}, 0)
        assert tx.noted == []
        assert QueryCache.get("k") is None

    async def test_edge(self) -> None:
        QueryCache.set("k", 1, "likes")
        QueryCache.set("w", 2, "w")
        assert await _Writer().relate("likes") == ({"likes": 1}, 0)
        assert QueryCache.get("k") is None
        assert QueryCache.get("w") == 2
        assert await _Writer().relate(edge=["a", "b"]) == ({"a": 1, "b": 1}, 0)
        tx = _Tx()
        assert await _Writer().relate("likes", None, tx) == ({}, 0)
        assert tx.noted == [frozenset({"likes"})]

    async def test_all(self) -> None:
        QueryCache.set("a", 1, "t")
        QueryCache.set("b", 2, "u")
        assert await _Manager.signup({}) == ({}, 1)
        assert QueryCache.stats()["entries"] == 0

    async def test_a_resolver_that_raises_means_every_table(self) -> None:
        QueryCache.set("a", 1, "t")
        assert await _Writer().relate("bad") == ({}, 1)
        assert QueryCache.stats()["entries"] == 0

    async def test_a_bind_error_is_the_functions_own(self) -> None:
        with pytest.raises(TypeError) as wrapped:
            await _Writer().save(1, 2, 3)
        with pytest.raises(TypeError) as direct:
            await _Writer.save.__wrapped__(_Writer(), 1, 2, 3)  # type: ignore[attr-defined]
        assert str(wrapped.value) == str(direct.value)
        assert _in_flight() == ({}, 0)

    def test_the_wrapper_keeps_the_function(self) -> None:
        for method in (_Writer.save, _Writer.define, _Qs.bulk_update, _Manager.signup):
            assert inspect.iscoroutinefunction(method)
            assert method.__surreal_cache_writes__ is True  # type: ignore[attr-defined]
            wrapped = method.__wrapped__  # type: ignore[attr-defined]
            assert inspect.signature(wrapped) == inspect.signature(inspect.unwrap(method))
        assert _Writer.save.__doc__ == "Save the record."
        assert _Writer.save.__name__ == "save"
        assert str(inspect.signature(_Writer.save)) == "(self, tx: Any = None) -> Any"
        assert _Writer.define.__doc__ == "Define the table."
        assert str(inspect.signature(_Writer.define)) == "(tx: Any = None) -> Any"
        assert isinstance(inspect.getattr_static(_Writer, "define"), classmethod)

    def test_an_unknown_resolver_is_refused(self) -> None:
        with pytest.raises(ValueError, match="invalidates"):
            cache.invalidates("nope")  # type: ignore[arg-type]

    async def test_concurrent_writes_and_reads(self, scope: int) -> None:
        """A cached read that begins while a wrapped write runs goes to the database and stores nothing."""
        started = asyncio.Event()
        release = asyncio.Event()

        class _Slow:
            @classmethod
            def get_table_name(cls) -> str:
                return "t"

            @cache.invalidates("self")
            async def save(self) -> None:
                started.set()
                await release.wait()

        _store("k", [1], {"t"}, scope=scope)
        write = asyncio.create_task(_Slow().save())
        await started.wait()
        assert QueryCache._begin_read("k", frozenset({"t"}), False, scope, 60.0) == (False, None, None)
        release.set()
        await write
        assert QueryCache.stats()["entries"] == 0


# --- spec §6.5: transactions note the tables written and invalidate them at commit ----------------------


class _TxClient:
    """A client as a transaction's commit and cancel see one: it records what is in flight while it commits."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.in_flight_during_commit: list[tuple[dict[str, int], int]] = []
        self.cancelled = 0

    async def query_raw(self, query: str, variables: Any = None) -> Any:
        """The buffered commit: one ``BEGIN … COMMIT`` request; ``fail`` answers an ``ERR`` statement."""
        self.in_flight_during_commit.append(_in_flight())
        if self.fail:
            return {"result": [{"status": "ERR", "result": "Database record `t:1` already exists"}]}
        return {"result": [{"status": "OK", "result": []}]}

    async def commit(self, txn_id: Any) -> None:
        """The interactive commit; ``fail`` raises as a refused commit does."""
        self.in_flight_during_commit.append(_in_flight())
        if self.fail:
            raise RuntimeError("commit refused")

    async def cancel(self, txn_id: Any) -> None:
        self.cancelled += 1


_STRATEGIES = ("buffered", "interactive")


async def _transaction(strategy: str, client: _TxClient, *, statement: bool = True) -> Any:
    """A transaction of ``strategy`` on the fake ``client``; a buffered one holds one statement unless told not to."""
    if strategy == "interactive":
        return InteractiveTransaction(client, "txn")
    tx = BufferedTransaction(client)
    if statement:
        await tx.add("CREATE t:1;")
    return tx


def _commit_error(strategy: str) -> tuple[type[Exception], str]:
    if strategy == "interactive":
        return RuntimeError, "commit refused"
    return SurrealDbError, "Transaction failed and rolled back: Database record `t:1` already exists"


class TestTransactionBookkeeping:
    """Spec §6.5: a write in a transaction is noted, not in flight; the commit puts the tables noted in flight and
    invalidates them when it returns or raises; ``cancel()`` forgets them; ``None`` is every table."""

    @pytest.mark.parametrize("strategy", _STRATEGIES)
    async def test_a_write_is_noted_not_invalidated(self, strategy: str) -> None:
        QueryCache.set("k", 1, "t")
        tx = await _transaction(strategy, _TxClient())
        async with cache.writing({"t"}, tx=tx):
            assert _in_flight() == ({}, 0)
        assert _in_flight() == ({}, 0)
        assert tx._cache_written() == frozenset({"t"})
        assert QueryCache.get("k") == 1

    @pytest.mark.parametrize("strategy", _STRATEGIES)
    async def test_the_commit_invalidates_the_tables_noted(self, strategy: str) -> None:
        QueryCache.set("k", 1, "t")
        QueryCache.set("other", 2, "u")
        client = _TxClient()
        tx = await _transaction(strategy, client)
        async with cache.writing({"t"}, tx=tx):
            pass
        async with cache.writing(["t"], tx=tx):
            pass
        await tx.commit()
        assert client.in_flight_during_commit == [({"t": 1}, 0)]
        assert _in_flight() == ({}, 0)
        assert QueryCache.get("k") is None
        assert QueryCache.get("other") == 2
        assert tx._cache_written() == frozenset()

    @pytest.mark.parametrize("strategy", _STRATEGIES)
    async def test_a_failed_commit_still_invalidates(self, strategy: str) -> None:
        QueryCache.set("k", 1, "t")
        QueryCache.set("other", 2, "u")
        client = _TxClient(fail=True)
        tx = await _transaction(strategy, client)
        async with cache.writing({"t"}, tx=tx):
            pass
        error, text = _commit_error(strategy)
        with pytest.raises(error) as info:
            await tx.commit()
        assert str(info.value) == text
        assert client.in_flight_during_commit == [({"t": 1}, 0)]
        assert _in_flight() == ({}, 0)
        assert QueryCache.get("k") is None
        assert QueryCache.get("other") == 2
        assert tx._cache_written() == frozenset()

    @pytest.mark.parametrize("strategy", _STRATEGIES)
    @pytest.mark.parametrize("tables", [{"t"}, None])
    async def test_cancel_forgets_without_invalidating(self, strategy: str, tables: set[str] | None) -> None:
        QueryCache.set("k", 1, "t")
        client = _TxClient()
        tx = await _transaction(strategy, client)
        async with cache.writing(tables, tx=tx):
            pass
        await tx.cancel()
        assert client.cancelled == (1 if strategy == "interactive" else 0)
        assert _in_flight() == ({}, 0)
        assert QueryCache.get("k") == 1
        assert tx._cache_written() == frozenset()
        assert client.in_flight_during_commit == []

    @pytest.mark.parametrize("strategy", _STRATEGIES)
    async def test_none_is_every_table(self, strategy: str) -> None:
        QueryCache.set("k", 1, "t")
        QueryCache.set("other", 2, "u")
        client = _TxClient()
        tx = await _transaction(strategy, client)
        tx._note_cache_write(None)
        assert tx._cache_written() is None
        tx._note_cache_write({"t"})
        assert tx._cache_written() is None
        await tx.commit()
        assert client.in_flight_during_commit == [({}, 1)]
        assert _in_flight() == ({}, 0)
        assert QueryCache.stats()["entries"] == 0
        assert tx._cache_written() == frozenset()

    @pytest.mark.parametrize("strategy", _STRATEGIES)
    async def test_none_through_writing(self, strategy: str) -> None:
        QueryCache.set("k", 1, "t")
        tx = await _transaction(strategy, _TxClient())
        async with cache.writing(None, tx=tx):
            assert _in_flight() == ({}, 0)
        assert tx._cache_written() is None
        assert QueryCache.get("k") == 1
        await tx.commit()
        assert QueryCache.stats()["entries"] == 0

    @pytest.mark.parametrize("strategy", _STRATEGIES)
    async def test_edges_is_noted_like_a_table(self, strategy: str, scope: int) -> None:
        _store("edge", [1], {"qc_likes", EDGES}, scope=scope)
        _store("user", [2], {"qc_user"}, scope=scope)
        client = _TxClient()
        tx = await _transaction(strategy, client)
        async with cache.writing({"t", EDGES}, tx=tx):
            pass
        assert tx._cache_written() == frozenset({"t", EDGES})
        assert QueryCache.get("edge") == [1]
        await tx.commit()
        assert client.in_flight_during_commit == [({"t": 1, EDGES: 1}, 0)]
        assert QueryCache.get("edge") is None
        assert QueryCache.get("user") == [2]

    async def test_an_empty_buffered_commit_invalidates_nothing(self) -> None:
        QueryCache.set("k", 1, "t")
        client = _TxClient()
        tx = await _transaction("buffered", client, statement=False)
        await tx.commit()
        assert client.in_flight_during_commit == []
        assert QueryCache.get("k") == 1
        assert tx._cache_written() == frozenset()

    async def test_a_second_commit_invalidates_nothing_more(self) -> None:
        client = _TxClient()
        tx = await _transaction("interactive", client)
        async with cache.writing({"t"}, tx=tx):
            pass
        await tx.commit()
        QueryCache.set("k", 1, "t")
        await tx.commit()
        assert client.in_flight_during_commit == [({"t": 1}, 0), ({}, 0)]
        assert QueryCache.get("k") == 1


# --- spec §5.1, §5.3, §6.3: QuerySet.cache(), its tags and its refusals ---------------------------------

_C3 = "cache() cannot be combined with as_of(): a time-travel read is never cached"
_C10 = (
    "query() runs your own SurrealQL and is never cached: drop cache(), or cache its result yourself with "
    "QueryCache.make_key(), get() and set()"
)
_W1 = "QueryCache is disabled — .cache() has no effect"


def _live_refused(offenders: str) -> str:
    """The pre-existing refusal of ``live()``/``watch()``, naming *offenders* (unchanged text, spec §5.1)."""
    return (
        f"A live query cannot honour {offenders}: SurrealDB's LIVE SELECT has no ORDER BY, LIMIT or START, streams "
        "whole records, and runs outside any transaction. Drop the clause — filter(), variables() and fetch() are "
        "supported."
    )


def _changes_refused(offenders: str) -> str:
    """The pre-existing refusal of ``changes()``, naming *offenders* (unchanged text, spec §5.1)."""
    return (
        f"A change feed cannot honour {offenders}: SurrealDB's SHOW CHANGES reads the whole table's log, with no "
        "WHERE, projection, ordering or paging, outside any transaction. Call changes() on Model.objects() and "
        "filter the events."
    )


def _warnings(caplog: pytest.LogCaptureFixture, logger: str) -> list[tuple[int, str]]:
    return [(record.levelno, record.getMessage()) for record in caplog.records if record.name == logger]


class TestCacheMethod:
    """Spec §5.1: ``cache()`` records the oldest result its reads accept; C1/C2 on ``ttl``, then C3; W1."""

    @pytest.mark.parametrize(
        ("ttl", "expected"),
        [
            (30, 30.0),
            (0.5, 0.5),
            (Fraction(1, 2), 0.5),
            (timedelta(minutes=2), 120.0),
            (0, 0.0),
            (0.0, 0.0),
            (timedelta(0), 0.0),
            (None, None),
        ],
        ids=["int", "float", "fraction", "timedelta", "zero", "zero-float", "zero-timedelta", "none"],
    )
    def test_records_the_request(self, ttl: Any, expected: float | None) -> None:
        qs = QcItem.objects()
        assert qs.cache(ttl) is qs
        assert qs._cache == CacheRequest(expected)
        assert QcItem.objects().cache(ttl=ttl)._cache == CacheRequest(expected)

    def test_without_an_argument_the_default_is_read_when_the_read_runs(self) -> None:
        assert QcItem.objects()._cache is None
        assert QcItem.objects().cache()._cache == CacheRequest(None)

    def test_called_again_replaces_the_ttl(self) -> None:
        qs = QcItem.objects().cache(30).cache(5)
        assert qs._cache == CacheRequest(5.0)
        assert qs.cache()._cache == CacheRequest(None)

    @pytest.mark.parametrize("ttl", [True, "30", Decimal("1"), [1]], ids=repr)
    def test_c1(self, ttl: Any) -> None:
        qs = QcItem.objects()
        expected = f"cache() ttl must be a number of seconds or a timedelta, not {type(ttl).__name__}"
        _raises(TypeError, expected, qs.cache, ttl)
        assert qs._cache is None

    @pytest.mark.parametrize("ttl", [-1, -0.5, timedelta(seconds=-1), math.nan, math.inf, -math.inf], ids=repr)
    def test_c2(self, ttl: Any) -> None:
        qs = QcItem.objects()
        _raises(ValueError, f"cache() ttl must be a finite number of seconds >= 0, got {ttl!r}", qs.cache, ttl)
        assert qs._cache is None

    def test_c3_after_as_of(self) -> None:
        qs = QcItem.objects().as_of(datetime(2026, 1, 1, tzinfo=UTC))
        _raises(SurrealDbError, _C3, qs.cache)
        _raises(SurrealDbError, _C3, qs.cache, 30)
        assert qs._cache is None

    def test_c1_and_c2_come_before_c3(self) -> None:
        qs = QcItem.objects().as_of(datetime(2026, 1, 1, tzinfo=UTC))
        _raises(ValueError, "cache() ttl must be a finite number of seconds >= 0, got -1", qs.cache, -1)
        _raises(TypeError, "cache() ttl must be a number of seconds or a timedelta, not bool", qs.cache, True)

    @pytest.mark.parametrize(
        "at",
        [datetime(2026, 1, 1, tzinfo=UTC), "2026-01-01T00:00:00Z", timedelta(hours=1), "not a moment"],
        ids=["datetime", "iso", "timedelta", "invalid-string"],
    )
    def test_c3_on_as_of_after_cache(self, at: Any) -> None:
        """C3 comes before ``as_of()`` normalises its moment: even an invalid one is refused with C3."""
        qs = QcItem.objects().cache()
        _raises(SurrealDbError, _C3, qs.as_of, at)
        assert qs._as_of is None

    def test_as_of_none_after_cache_is_accepted(self) -> None:
        qs = QcItem.objects().cache(30)
        assert qs.as_of(None) is qs
        assert qs._as_of is None
        assert qs._cache == CacheRequest(30.0)

    def test_the_transaction_check_of_as_of_comes_first(self) -> None:
        qs = QcItem.objects(tx=_Tx()).cache()
        with pytest.raises(SurrealDbError) as info:
            qs.as_of(datetime(2026, 1, 1, tzinfo=UTC))
        assert str(info.value) != _C3

    def test_w1_while_disabled(self, caplog: pytest.LogCaptureFixture) -> None:
        QueryCache.configure(enabled=False)
        qs = QcItem.objects()
        with caplog.at_level(logging.WARNING, logger="surreal_orm_lite.query_set"):
            assert qs.cache(30) is qs
        assert _warnings(caplog, "surreal_orm_lite.query_set") == [(logging.WARNING, _W1)]
        assert qs._cache == CacheRequest(30.0)  # still set: it applies if the cache is enabled before the read

    def test_no_warning_while_enabled(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="surreal_orm_lite.query_set"):
            QcItem.objects().cache()
        assert _warnings(caplog, "surreal_orm_lite.query_set") == []


@pytest.fixture
def _no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything asks for a client: these refusals come before any request."""
    monkeypatch.setattr(SurrealDBConnectionManager, "get_client", _forbidden_client)


@pytest.mark.usefixtures("_no_client")
class TestRefusals:
    """AC-6, D18: ``query()``, ``live()``, ``watch()`` and ``changes()`` refuse a queryset with ``cache()``."""

    async def test_query_raises_c10(self) -> None:
        with pytest.raises(SurrealDbError) as info:
            await QcItem.objects().cache().query("SELECT * FROM qc_item;")
        assert str(info.value) == _C10

    async def test_c10_comes_before_the_from_check(self) -> None:
        with pytest.raises(SurrealDbError) as info:
            await QcItem.objects().cache().query("SELECT 1;")
        assert str(info.value) == _C10

    @pytest.mark.parametrize("method", ["live", "watch"])
    def test_live_and_watch_name_cache(self, method: str) -> None:
        _raises(SurrealDbError, _live_refused("cache()"), getattr(QcItem.objects().cache(), method))
        _raises(SurrealDbError, _live_refused("cache(), limit()"), getattr(QcItem.objects().cache().limit(1), method))

    def test_changes_names_cache(self) -> None:
        _raises(SurrealDbError, _changes_refused("cache()"), QcItem.objects().cache().changes)
        _raises(SurrealDbError, _changes_refused("cache(), limit()"), QcItem.objects().cache().limit(1).changes)

    async def test_without_cache_the_texts_are_unchanged(self) -> None:
        _raises(SurrealDbError, _live_refused("limit()"), QcItem.objects().limit(1).live)
        _raises(SurrealDbError, _live_refused("limit()"), QcItem.objects().limit(1).watch)
        _raises(SurrealDbError, _changes_refused("limit()"), QcItem.objects().limit(1).changes)
        with pytest.raises(SurrealDbError) as info:
            await QcItem.objects().query("SELECT 1;")
        assert str(info.value) == "The query must include 'FROM qc_item' to reference the correct table."


class _Read(NamedTuple):
    """What ``QuerySet._cached_read`` was asked."""

    kind: str
    statement: str
    variables: dict[str, Any]
    tables: frozenset[str]
    any_table: bool


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[_Read]:
    """Replace ``QuerySet._cached_read`` with a recorder answering ``[]``; no client may be acquired."""
    calls: list[_Read] = []

    async def recording(
        self: QuerySet,
        kind: str,
        statement: str,
        variables: dict[str, Any],
        tables: frozenset[str],
        any_table: bool,
        run: Callable[[], Awaitable[Any]],
    ) -> Any:
        calls.append(_Read(kind, statement, dict(variables), tables, any_table))
        return []

    monkeypatch.setattr(QuerySet, "_cached_read", recording)
    monkeypatch.setattr(SurrealDBConnectionManager, "get_client", _forbidden_client)
    return calls


def _active_likers() -> Any:
    return QcUser.objects().filter(id__in=Subquery(QcLikes.objects().select("in")))


# Reads whose statement follows a link (spec §5.3 "any"): a dotted path wherever it sits, or FETCH.
_FOLLOWING: list[tuple[str, Callable[[], Any]]] = [
    ("filter", lambda: QcBook.objects().filter(**{"author.name": "x"}).cache().exec()),
    ("q", lambda: QcBook.objects().filter(Q(**{"author.name": "x"}) | Q(price=1)).cache().exec()),
    ("order_by", lambda: QcBook.objects().order_by("author.name").cache().exec()),
    ("select", lambda: QcBook.objects().select("title", "author.name").cache().exec()),
    ("values", lambda: QcBook.objects().values("author.name").annotate(n=Count()).cache().exec()),
    ("count", lambda: QcBook.objects().filter(**{"author.name": "x"}).cache().count()),
    (
        "in-a-subquery",
        lambda: (
            QcAuthor.objects()
            .filter(id__in=Subquery(QcBook.objects().filter(**{"author.name": "x"}).select("author")))
            .cache()
            .exec()
        ),
    ),
    ("fetch", lambda: QcBook.objects().fetch("author").cache().exec()),
]


class TestTags:
    """Spec §5.3, §6.3: what a cached read depends on, decided once from its compiled statement and its models."""

    async def test_a_plain_read(self, reads: list[_Read]) -> None:
        assert await QcItem.objects().filter(category="book").cache().exec() == []
        expected = QcItem.objects().filter(category="book")._compile_query()
        assert reads == [_Read("query", *expected, frozenset({"qc_item"}), False)]

    async def test_every_compiled_read_goes_through_the_cache(self, reads: list[_Read]) -> None:
        qs = QcItem.objects().filter(price__gt=1).cache()
        assert await qs.exec() == []
        with pytest.raises(SurrealDbNotFoundError):
            await qs.first()
        with pytest.raises(SurrealDbNotFoundError):
            await qs.get()
        assert await qs.count() == 0
        assert await qs.sum("price") == 0
        assert await qs.avg("price") == 0.0
        assert await qs.min("price") is None
        assert await qs.max("price") is None
        assert await qs.exists() is False
        assert await QcItem.objects().values("category").annotate(n=Count()).cache().exec() == []
        assert [read.kind for read in reads] == ["query"] * 10
        assert {read.tables for read in reads} == {frozenset({"qc_item"})}
        assert not any(read.any_table for read in reads)
        # exec() and get() send the same statement, as first() and exists() do (LIMIT 1).
        assert len({read.statement for read in reads}) == 8
        assert qs._limit is None  # first() and exists() leave the queryset untouched

    async def test_an_edge_read(self, reads: list[_Read]) -> None:
        await QcLikes.objects().cache().exec()
        await QcLikes.objects().cache().count()
        assert [(read.tables, read.any_table) for read in reads] == [(frozenset({"qc_likes", EDGES}), False)] * 2

    async def test_a_subquery_of_an_edge(self, reads: list[_Read]) -> None:
        await _active_likers().cache().exec()
        (read,) = reads
        assert read.tables == frozenset({"qc_user", "qc_likes", EDGES})
        assert read.any_table is False

    async def test_a_subquery_of_a_node(self, reads: list[_Read]) -> None:
        await QcBook.objects().filter(author__in=Subquery(QcAuthor.objects().filter(name="Ada"))).cache().exec()
        (read,) = reads
        assert read.tables == frozenset({"qc_book", "qc_author"})
        assert read.any_table is False

    @pytest.mark.parametrize("read", [read for _, read in _FOLLOWING], ids=[name for name, _ in _FOLLOWING])
    async def test_a_read_that_follows_links_is_any(self, reads: list[_Read], read: Callable[[], Any]) -> None:
        await read()
        (recorded,) = reads
        assert recorded.any_table is True

    async def test_geo_distance_on_a_plain_point_field(self, reads: list[_Read]) -> None:
        await QcPlace.objects().annotate(d=GeoDistance("loc", (0.0, 0.0))).order_by("d").cache().exec()
        await QcPlace.objects().nearby("loc", (0.0, 0.0), max_distance=10).cache().count()
        assert [(read.tables, read.any_table) for read in reads] == [(frozenset({"qc_place"}), False)] * 2
        assert "loc.is_point()" in reads[0].statement

    async def test_get_by_id_and_all(self, reads: list[_Read]) -> None:
        with pytest.raises(SurrealDbNotFoundError, match="No result found."):
            await QcItem.objects().cache().get("i1")
        assert await QcItem.objects().cache().all() == []
        with pytest.raises(SurrealDbNotFoundError, match="No result found."):
            await QcLikes.objects().cache().get("e1")
        assert await QcLikes.objects().cache().all() == []
        assert reads == [
            _Read("select", "", {"target": RecordID("qc_item", "i1")}, frozenset({"qc_item"}), False),
            _Read("select", "", {"target": "qc_item"}, frozenset({"qc_item"}), False),
            _Read("select", "", {"target": RecordID("qc_likes", "e1")}, frozenset({"qc_likes", EDGES}), False),
            _Read("select", "", {"target": "qc_likes"}, frozenset({"qc_likes", EDGES}), False),
        ]

    @pytest.mark.parametrize(
        ("item", "dotted"), [("author.*", False), ("author.name.*", True)], ids=["author.*", "author.name.*"]
    )
    async def test_a_select_item_that_is_not_a_field_is_any(self, reads: list[_Read], item: str, dotted: bool) -> None:
        """P3: a ``select()`` item ending in ``.*`` reads every field of the linked record."""
        await QcBook.objects().select("title", item).cache().exec()
        (read,) = reads
        assert read.any_table is True
        assert read.statement == f"SELECT title, {item} FROM qc_book;"
        assert follows_links(read.statement) is dotted  # for author.*, the projection rule is what tags it

    @pytest.mark.parametrize("item", ["author.{name}", "author[*]", "x AS y", "count()"])
    async def test_a_select_item_that_is_not_a_field_path_is_refused(self, reads: list[_Read], item: str) -> None:
        """``select()`` refuses it when called: nothing is read, nothing is cached."""
        with pytest.raises(ValueError, match=r"select\(\)"):
            await QcBook.objects().select("title", item).cache().exec()
        assert reads == []

    async def test_plain_select_fields_are_not_any(self, reads: list[_Read]) -> None:
        await QcBook.objects().select("title", "price").cache().exec()
        await QcBook.objects().select("*").cache().exec()
        assert [read.any_table for read in reads] == [False, False]

    def test_an_inner_querysets_cache_changes_nothing(self) -> None:
        plain = QcUser.objects().filter(id__in=Subquery(QcLikes.objects().select("in")))
        cached = QcUser.objects().filter(id__in=Subquery(QcLikes.objects().select("in").cache(5)))
        assert cached._compile_query() == plain._compile_query()


class TestUncachedUnchanged:
    """AC-12: without ``cache()`` (or inside a transaction, or for a write), every read runs today's code."""

    @pytest.fixture
    def executed(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
        calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        async def recording(self: QuerySet, *args: Any, **kwargs: Any) -> Any:
            calls.append((args, kwargs))
            return []

        async def never(*args: Any, **kwargs: Any) -> Any:
            pytest.fail("the cache was consulted")

        monkeypatch.setattr(QuerySet, "_execute_query", recording)
        monkeypatch.setattr(QuerySet, "_cached_read", never)
        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", _forbidden_client)
        return calls

    async def test_exec_and_count(self, executed: list[tuple[tuple[Any, ...], dict[str, Any]]]) -> None:
        qs = QcItem.objects().filter(category="book")
        await qs.exec()
        await qs.count()
        assert executed == [(qs._compile_query(), {}), (qs._compile_aggregation_query("count()"), {})]

    async def test_a_subquery_read(self, executed: list[tuple[tuple[Any, ...], dict[str, Any]]]) -> None:
        qs = QcUser.objects().filter(id__in=Subquery(QcLikes.objects().select("in").cache()))
        await qs.exec()
        assert executed == [(qs._compile_query(), {"subquery_tables": frozenset({"qc_likes"})})]

    async def test_a_cached_read_in_a_transaction(self, executed: list[tuple[tuple[Any, ...], dict[str, Any]]]) -> None:
        qs = QcItem.objects(tx=_Tx()).cache()
        await qs.count()
        assert executed == [(qs._compile_aggregation_query("count()"), {})]

    async def test_a_write_on_a_cached_queryset(self, executed: list[tuple[tuple[Any, ...], dict[str, Any]]]) -> None:
        await QcItem.objects().filter(category="book").cache().bulk_update(price=1)
        assert executed == [
            (("UPDATE qc_item SET price = $_v0 WHERE category = $_f0;", {"_f0": "book", "_v0": 1}), {}),
        ]

    async def test_get_by_id_and_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        selected: list[Any] = []

        class _Selecting:
            async def select(self, target: Any) -> Any:
                selected.append(target)
                row = {"id": RecordID("qc_item", "i1"), "name": "a"}
                return row if isinstance(target, RecordID) else [row]

        client = _Selecting()

        async def get_client(*args: Any, **kwargs: Any) -> Any:
            return client

        async def never(*args: Any, **kwargs: Any) -> Any:
            pytest.fail("the cache was consulted")

        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", get_client)
        monkeypatch.setattr(QuerySet, "_cached_read", never)
        assert (await QcItem.objects().get("i1")).name == "a"
        assert [item.name for item in await QcItem.objects().all()] == ["a"]
        assert selected == [RecordID("qc_item", "i1"), "qc_item"]


# --- AC-11, AC-12: every public coroutine is classified; every write is wrapped ------------------------------

#: The 26 writes of spec §5.4, by the class that defines them (``vars(cls)``), and v0.32.0's ``define_view`` and
#: ``remove_view``.
_INVALIDATING_WRITES: dict[type, frozenset[str]] = {
    BaseSurrealModel: frozenset(
        {
            "save",
            "upsert",
            "update",
            "merge",
            "patch",
            "atomic_append",
            "atomic_remove",
            "atomic_set_add",
            "atomic_increment",
            "atomic_append_many",
            "atomic_set_add_many",
            "atomic_remove_many",
            "delete",
            "relate",
            "remove_relation",
            "remove_all_relations",
            "define_computed_fields",
            "define_view",
            "remove_view",
        }
    ),
    BaseSurrealEdge: frozenset({"save"}),
    AuthenticatedUserMixin: frozenset({"define_access", "signup"}),
    QuerySet: frozenset({"bulk_create", "bulk_update", "patch", "bulk_delete", "delete_table"}),
    SurrealDBConnectionManager: frozenset({"signup"}),
}

#: Every public coroutine of the classes a user calls, by what it does to the cache (spec §5.3, §5.4, AC-11). A
#: new public coroutine fails :class:`TestExhaustiveness` until it is classified here.
_CLASSIFIED: dict[type, dict[str, frozenset[str]]] = {
    BaseSurrealModel: {
        "invalidating writes": _INVALIDATING_WRITES[BaseSurrealModel],
        "raw, refused or not cached": frozenset({"raw_query", "call_function", "refresh", "get_related", "traverse"}),
        "DDL that changes no row": frozenset({"define_events", "remove_event"}),
    },
    BaseSurrealEdge: {
        "invalidating writes": _INVALIDATING_WRITES[BaseSurrealEdge],
        "refused": frozenset({"upsert"}),
        "DDL that changes no row": frozenset({"define_relation"}),
    },
    AuthenticatedUserMixin: {
        "invalidating writes": _INVALIDATING_WRITES[AuthenticatedUserMixin],
        "identity": frozenset({"signin", "authenticate", "refresh"}),
    },
    QuerySet: {
        "cached reads": frozenset({"exec", "first", "get", "all", "count", "sum", "avg", "min", "max", "exists"}),
        "invalidating writes": _INVALIDATING_WRITES[QuerySet],
        "writes through wrapped helpers": frozenset({"get_or_create", "update_or_create"}),
        "raw, refused or not cached": frozenset({"query"}),
    },
    SurrealDBConnectionManager: {
        "invalidating writes": _INVALIDATING_WRITES[SurrealDBConnectionManager],
        "identity": frozenset({"signin", "authenticate", "invalidate"}),
        "raw, refused or not cached": frozenset({"call_function"}),
        "DDL that changes no row": frozenset({"define_event", "remove_event", "define_function", "remove_function"}),
        "other": frozenset(
            {
                "unset_connection",
                "remove_connection",
                "get_client",
                "close_connection",
                "close_all_connections",
                "info",
                "kill",
                "reconnect",
                "validate_connection",
                "set_url",
                "set_user",
                "set_password",
                "set_namespace",
                "set_database",
            }
        ),
    },
}

#: The writes defined as class methods: ``invalidates()`` is the inner decorator there.
_CLASS_METHOD_WRITES = {
    (BaseSurrealModel, "define_computed_fields"),
    (BaseSurrealModel, "define_view"),
    (BaseSurrealModel, "remove_view"),
    (AuthenticatedUserMixin, "define_access"),
    (AuthenticatedUserMixin, "signup"),
    (SurrealDBConnectionManager, "signup"),
}


def _defined(cls: type, name: str) -> Any:
    """The function *cls* defines as *name*, class and static methods unwrapped."""
    value = vars(cls)[name]
    return value.__func__ if isinstance(value, classmethod | staticmethod) else value


def _public_coroutines(cls: type) -> set[str]:
    return {name for name in vars(cls) if not name.startswith("_") and inspect.iscoroutinefunction(_defined(cls, name))}


def _ids(classes: Any) -> list[str]:
    return [cls.__name__ for cls in classes]


class TestExhaustiveness:
    """AC-11: each public coroutine is a cached read, an invalidating write, or classified as neither on purpose."""

    @pytest.mark.parametrize("cls", list(_CLASSIFIED), ids=_ids(_CLASSIFIED))
    def test_every_public_coroutine_is_classified(self, cls: type) -> None:
        groups = _CLASSIFIED[cls]
        classified = [name for names in groups.values() for name in names]
        assert len(classified) == len(set(classified)), "a coroutine is in two classes"
        assert _public_coroutines(cls) == set(classified)

    def test_the_28_writes_of_the_spec(self) -> None:
        assert sum(len(names) for names in _INVALIDATING_WRITES.values()) == 28

    @pytest.mark.parametrize("cls", list(_CLASSIFIED), ids=_ids(_CLASSIFIED))
    def test_every_invalidating_write_is_wrapped_and_nothing_else(self, cls: type) -> None:
        for group, names in _CLASSIFIED[cls].items():
            for name in names:
                wrapped = getattr(_defined(cls, name), "__surreal_cache_writes__", False) is True
                assert wrapped is (group == "invalidating writes"), f"{cls.__name__}.{name}"

    def test_save_naming_is_wrapped(self) -> None:
        """The create of ``get_or_create()``/``update_or_create()`` and of ``relate(EdgeModel, …)``."""
        assert BaseSurrealModel._save_naming.__surreal_cache_writes__ is True  # type: ignore[attr-defined]

    @pytest.mark.parametrize("cls", list(_INVALIDATING_WRITES), ids=_ids(_INVALIDATING_WRITES))
    def test_a_wrapped_write_keeps_its_signature_and_docs(self, cls: type) -> None:
        """AC-12: ``inspect.signature``, ``__doc__``, ``__name__`` and ``__wrapped__``; still a coroutine function,
        and still a class method where it was one."""
        for name in _INVALIDATING_WRITES[cls]:
            function = _defined(cls, name)
            original = function.__wrapped__
            assert inspect.iscoroutinefunction(original), name
            assert not hasattr(original, "__surreal_cache_writes__"), name
            assert inspect.signature(function) == inspect.signature(original), name
            assert function.__doc__ == original.__doc__, name
            assert function.__doc__, name
            assert function.__name__ == original.__name__ == name
            assert inspect.iscoroutinefunction(function), name
            is_class_method = isinstance(inspect.getattr_static(cls, name), classmethod)
            assert is_class_method is ((cls, name) in _CLASS_METHOD_WRITES), name


# === E2E: an engine or a server ===========================================================================

#: The tables of this module's models, removed before and after each E2E test on a server.
_TABLES = (
    "qc_item",
    "qc_author",
    "qc_book",
    "qc_place",
    "qc_user",
    "qc_post",
    "qc_likes",
    "qc_ghost",
    "qc_player",
    "qc_account",
)

#: The engines every cached read runs on (spec §9).
_KINDS = ["mem", "ws", "http"]


@contextlib.asynccontextmanager
async def cache_db(kind: str) -> AsyncIterator[Any]:
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


#: Spec M1/M2's record-access fixture, in a database of its own.
_AUTH_DB = "qc_auth"
_AUTH_ACCESS = "qc_access"
_AUTH_PASS = "qc-passphrase"
_AUTH_DEFINE = (
    "DEFINE TABLE OVERWRITE qc_acct SCHEMALESS PERMISSIONS FOR select WHERE id = $auth.id;",
    "DEFINE TABLE OVERWRITE qc_note SCHEMALESS PERMISSIONS FOR select WHERE owner = $auth.id;",
    f"DEFINE ACCESS OVERWRITE {_AUTH_ACCESS} ON DATABASE TYPE RECORD"
    " SIGNUP ( CREATE qc_acct SET name = $name, pass = crypto::argon2::generate($pass) )"
    " SIGNIN ( SELECT * FROM qc_acct WHERE name = $name AND crypto::argon2::compare(pass, $pass) );",
    "CREATE qc_acct:a SET name = 'a', pass = crypto::argon2::generate($pass);",
    "CREATE qc_acct:b SET name = 'b', pass = crypto::argon2::generate($pass);",
    "CREATE qc_note:a1 SET owner = qc_acct:a;",
    "CREATE qc_note:b1 SET owner = qc_acct:b;",
    "CREATE qc_note:b2 SET owner = qc_acct:b;",
)
_AUTH_REMOVE = (f"REMOVE ACCESS {_AUTH_ACCESS} ON DATABASE;", "REMOVE TABLE qc_note;", "REMOVE TABLE qc_acct;")


async def _remove_auth(client: Any) -> None:
    for statement in _AUTH_REMOVE:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.query(statement), 10)


@contextlib.asynccontextmanager
async def auth_db(kind: str) -> AsyncIterator[Any]:
    """Spec M1/M2's data on the server of ``SURREALDB_PORT`` over ``ws`` or ``http``, in database ``qc_auth``.

    Accounts ``a`` and ``b`` (record access ``qc_access``, variables ``name`` and ``pass``), notes ``a1`` (``a``'s),
    ``b1`` and ``b2`` (``b``'s), each readable by its owner only. Yields the ORM's client, signed in as root; at the
    end it signs in as root again, removes everything and unsets the connection settings.
    """
    url = _ws_url() if kind == "ws" else _http_url()
    SurrealDBConnectionManager.set_connection(url=url, user="root", password="root", namespace="ns", database=_AUTH_DB)
    try:
        client = await SurrealDBConnectionManager.get_client()
        await _remove_auth(client)
        for statement in _AUTH_DEFINE:
            await client.query(statement, {"pass": _AUTH_PASS})
        try:
            yield client
        finally:
            with contextlib.suppress(Exception):
                await client.signin({"username": "root", "password": "root"})
                await client.use("ns", _AUTH_DB)
            await _remove_auth(client)
    finally:
        await SurrealDBConnectionManager.unset_connection()


def _sign_in_as(name: str, *, store: bool) -> Callable[[], Awaitable[Any]]:
    """The manager's record sign-in as account *name* of :func:`auth_db`."""

    async def sign_in() -> Any:
        variables = {"name": name, "pass": _AUTH_PASS}
        return await SurrealDBConnectionManager.signin(access=_AUTH_ACCESS, variables=variables, store=store)

    return sign_in


async def _note_keys(client: Any) -> list[str]:
    """The keys of the ``qc_note`` rows the client's current identity may read."""
    rows = await client.query("SELECT VALUE id FROM qc_note;")
    return sorted(str(row.id) for row in rows)


class TestScopeE2E:
    @pytest.mark.parametrize("kind", ["ws", "http"])
    async def test_every_identity_change_mints_a_new_scope(self, kind: str) -> None:
        """M1: root, ``signin(store=False)`` as ``a``, as ``b``, ``signin`` as ``a``, ``invalidate()`` — the same
        client reads other rows at each step, so each step has a scope of its own and retires the previous one."""

        async def as_root() -> None:
            return None

        steps: list[tuple[Callable[[], Awaitable[Any]], list[str]]] = [
            (as_root, ["a1", "b1", "b2"]),
            (_sign_in_as("a", store=False), ["a1"]),
            (_sign_in_as("b", store=False), ["b1", "b2"]),
            (_sign_in_as("a", store=True), ["a1"]),
            (SurrealDBConnectionManager.invalidate, ["a1", "b1", "b2"]),
        ]
        async with auth_db(kind) as client:
            scopes: list[int] = []
            for step, notes in steps:
                await step()
                assert await SurrealDBConnectionManager.get_client() is client
                scope = SurrealDBConnectionManager._cache_scope(client)
                assert scope is not None
                assert scope in QueryCache._live_scopes
                assert not set(scopes) & QueryCache._live_scopes  # every previous scope is retired…
                assert QueryCache.stats()["entries"] == 0  # …with its entries
                assert await _note_keys(client) == notes
                _store(f"k{len(scopes)}", notes, {"qc_note"}, scope=scope)
                scopes.append(scope)
            assert len(set(scopes)) == 5

    @pytest.mark.parametrize("kind", ["mem", "ws", "http"])
    async def test_a_published_client_has_a_scope(self, kind: str) -> None:
        async with cache_db(kind) as client:
            scope = SurrealDBConnectionManager._cache_scope(client)
            assert scope is not None
            assert scope in QueryCache._live_scopes
            assert await SurrealDBConnectionManager.get_client() is client
            assert SurrealDBConnectionManager._cache_scope(client) == scope  # handing it out again mints nothing

    @pytest.mark.parametrize("kind", ["ws", "http"])
    @pytest.mark.parametrize("closing", ["close_connection", "close_all_connections", "unset_connection", "reconnect"])
    async def test_dropping_the_client_retires_its_scope(self, kind: str, closing: str) -> None:
        async with connected(_ws_url() if kind == "ws" else _http_url(), "db") as client:
            scope = SurrealDBConnectionManager._cache_scope(client)
            assert scope is not None
            _store("k", [1], {"qc_item"}, scope=scope)
            await getattr(SurrealDBConnectionManager, closing)()
            assert _retired(scope, "k")
            assert SurrealDBConnectionManager._cache_scope(client) is None
            if closing == "reconnect":
                fresh = await SurrealDBConnectionManager.get_client()
                assert fresh is not client
                fresh_scope = SurrealDBConnectionManager._cache_scope(fresh)
                assert fresh_scope is not None
                assert fresh_scope in QueryCache._live_scopes

    async def test_leaving_embedded_database_retires_its_engines_scope(self) -> None:
        async with embedded_database("mem://") as outer:
            outer_scope = SurrealDBConnectionManager._cache_scope(outer)
            assert outer_scope is not None
            _store("outer", [1], {"qc_item"}, scope=outer_scope)
            async with embedded_database("mem://") as inner:
                inner_scope = SurrealDBConnectionManager._cache_scope(inner)
                assert inner_scope is not None
                assert inner_scope != outer_scope
                _store("inner", [2], {"qc_item"}, scope=inner_scope)
                assert outer_scope in QueryCache._live_scopes  # the outer engine is detached, not retired
            del inner
            assert _retired(inner_scope, "inner")
            assert await SurrealDBConnectionManager.get_client() is outer
            assert SurrealDBConnectionManager._cache_scope(outer) == outer_scope
            assert "outer" in QueryCache._entries
        assert _retired(outer_scope, "outer")

    async def test_a_close_inside_embedded_database_retires_the_engines_scope(self) -> None:
        async with embedded_database("mem://") as first:
            scope = SurrealDBConnectionManager._cache_scope(first)
            assert scope is not None
            _store("k", [1], {"qc_item"}, scope=scope)
            await SurrealDBConnectionManager.close_connection()
            assert _retired(scope, "k")
            second = await SurrealDBConnectionManager.get_client()
            assert second is not first
            second_scope = SurrealDBConnectionManager._cache_scope(second)
            assert second_scope is not None
            assert second_scope != scope
            assert second_scope in QueryCache._live_scopes
            del first, second
        assert second_scope not in QueryCache._live_scopes


# --- AC-5, AC-6, AC-9: cached reads on every engine ------------------------------------------------------


def _record_selects(monkeypatch: pytest.MonkeyPatch, client: Any) -> list[Any]:
    """Record the target of each ``select()`` *client* runs from now on (``_record_queries``' twin)."""
    calls: list[Any] = []
    cls = type(client)
    real = cls.select
    owner = id(client)

    @functools.wraps(real)
    async def recording(self: Any, *args: Any, **kwargs: Any) -> Any:
        if id(self) == owner:
            calls.append(args[0] if args else next(iter(kwargs.values()), None))
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(cls, "select", recording)
    return calls


def _requests(monkeypatch: pytest.MonkeyPatch, client: Any) -> Callable[[], int]:
    """The number of ``query()`` and ``select()`` requests *client* sends from now on."""
    queries = _record_queries(monkeypatch, client)
    selects = _record_selects(monkeypatch, client)
    return lambda: len(queries) + len(selects)


def _hold_next_reply(
    monkeypatch: pytest.MonkeyPatch, client: Any, method: str = "query"
) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold the next *method* call of *client* **after** its reply: ``(reached, release)`` (spec §9).

    The call runs for real first, so the rows it returns are computed before whatever the test does while it is
    held; then ``reached`` is set and the call waits for ``release``. One-shot: disarmed before waiting, so the
    test's own calls meanwhile pass through. Patched on the client's class and keyed on ``id(client)``.
    """
    reached, release = asyncio.Event(), asyncio.Event()
    cls = type(client)
    real = getattr(cls, method)
    owner = id(client)
    armed = [True]

    @functools.wraps(real)  # keeps the signature: _record_queries() binds it
    async def holding(self: Any, *args: Any, **kwargs: Any) -> Any:
        hold = id(self) == owner and armed[0]
        if hold:
            armed[0] = False
        result = await real(self, *args, **kwargs)
        if hold:
            reached.set()
            await release.wait()
        return result

    monkeypatch.setattr(cls, method, holding)
    return reached, release


async def _until_held(reached: asyncio.Event, task: asyncio.Task[Any]) -> None:
    """Wait until *task*'s held call has its reply; raise *task*'s error if it ended first."""
    waiter = asyncio.ensure_future(reached.wait())
    done, _ = await asyncio.wait({waiter, task}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
    if waiter in done:
        return
    waiter.cancel()
    if task in done:
        task.result()
    raise AssertionError("the held call was never answered")


async def _seed() -> None:
    """Four items, two authors and their books, two places, two users, two posts and one like."""
    for key, name, category, price, tags in (
        ("i1", "a", "book", 10, ["x"]),
        ("i2", "b", "book", 20, []),
        ("i3", "c", "home", 30, []),
        ("i4", "d", "home", 40, []),
    ):
        await QcItem(id=key, name=name, category=category, price=price, tags=tags).save()
    await QcAuthor(id="ada", name="Ada").save()
    await QcAuthor(id="bea", name="Bea").save()
    await QcBook(id="b1", title="Dune", author=RecordID("qc_author", "ada"), price=12).save()
    await QcBook(id="b2", title="Emma", author=RecordID("qc_author", "bea"), price=9).save()
    await QcPlace(id="pl1", name="here", loc=GeometryPoint(0.0, 0.0)).save()
    await QcPlace(id="pl2", name="there", loc=GeometryPoint(1.0, 1.0)).save()
    await QcUser(id="ada", name="Ada").save()
    await QcUser(id="bob", name="Bob").save()
    await QcPost(id="p1", title="one").save()
    await QcPost(id="p2", title="two").save()
    await QcLikes(in_=RecordID("qc_user", "ada"), out=RecordID("qc_post", "p1")).save()


def _qs(model: type[BaseSurrealModel], cached: bool) -> Any:
    qs = model.objects()
    return qs.cache() if cached else qs


def _names(rows: Any) -> list[str]:
    return [row.name for row in rows]


def _id_price(row: Any) -> tuple[str, int]:
    return row.id, row.price


def _fetched_author(rows: Any) -> list[str]:
    """The author's name of each book read with ``fetch("author")`` (a dict row: the link is now a record)."""
    return [row["author"]["name"] if isinstance(row, dict) else row.author["name"] for row in rows]


class _ReadCase(NamedTuple):
    """One read of AC-5: ``read(cached)``, an out-of-band statement that changes its result, and that result
    rendered before and after it."""

    read: Callable[[bool], Awaitable[Any]]
    change: str
    render: Callable[[Any], Any]
    before: Any
    after: Any


_READ_CASES: dict[str, _ReadCase] = {
    "exec": _ReadCase(
        lambda c: _qs(QcItem, c).filter(category="book").order_by("name").exec(),
        "UPDATE qc_item:i1 SET name = 'z';",
        _names,
        ["a", "b"],
        ["b", "z"],
    ),
    "first": _ReadCase(
        lambda c: _qs(QcItem, c).order_by("price").first(),
        "UPDATE qc_item:i1 SET price = 99;",
        _id_price,
        ("i1", 10),
        ("i2", 20),
    ),
    "get_filtered": _ReadCase(
        lambda c: _qs(QcItem, c).filter(name="c").get(),
        "UPDATE qc_item:i3 SET price = 31;",
        _id_price,
        ("i3", 30),
        ("i3", 31),
    ),
    "get_id": _ReadCase(
        lambda c: _qs(QcItem, c).get("i1"),
        "UPDATE qc_item:i1 SET price = 11;",
        _id_price,
        ("i1", 10),
        ("i1", 11),
    ),
    "all": _ReadCase(
        lambda c: _qs(QcItem, c).all(),
        "DELETE qc_item:i4;",
        lambda rows: sorted(row.id for row in rows),
        ["i1", "i2", "i3", "i4"],
        ["i1", "i2", "i3"],
    ),
    "count": _ReadCase(
        lambda c: _qs(QcItem, c).filter(category="book").count(),
        "UPDATE qc_item:i3 SET category = 'book';",
        lambda value: value,
        2,
        3,
    ),
    "sum": _ReadCase(lambda c: _qs(QcItem, c).sum("price"), "UPDATE qc_item:i1 SET price = 11;", lambda value: value, 100, 101),
    "avg": _ReadCase(
        lambda c: _qs(QcItem, c).avg("price"), "UPDATE qc_item:i1 SET price = 14;", lambda value: value, 25.0, 26.0
    ),
    "min": _ReadCase(lambda c: _qs(QcItem, c).min("price"), "UPDATE qc_item:i1 SET price = 5;", lambda value: value, 10, 5),
    "max": _ReadCase(lambda c: _qs(QcItem, c).max("price"), "UPDATE qc_item:i4 SET price = 50;", lambda value: value, 40, 50),
    "exists": _ReadCase(
        lambda c: _qs(QcItem, c).filter(category="toy").exists(),
        "UPDATE qc_item:i1 SET category = 'toy';",
        lambda value: value,
        False,
        True,
    ),
    "values_annotate": _ReadCase(
        lambda c: _qs(QcItem, c).values("category").annotate(n=Count()).order_by("category").exec(),
        "UPDATE qc_item:i3 SET category = 'book';",
        lambda rows: [(row["category"], row["n"]) for row in rows],
        [("book", 2), ("home", 2)],
        [("book", 3), ("home", 1)],
    ),
    "geo_distance": _ReadCase(
        lambda c: _qs(QcPlace, c).annotate(d=GeoDistance("loc", (0.0, 0.0))).order_by("d").exec(),
        "UPDATE qc_place:pl1 SET loc = (2.0, 2.0);",
        lambda rows: [(row.id, row.d == 0) for row in rows],
        [("pl1", True), ("pl2", False)],
        [("pl2", False), ("pl1", False)],
    ),
    "subquery": _ReadCase(
        lambda c: _qs(QcUser, c).filter(id__in=Subquery(QcLikes.objects().select("in"))).order_by("name").exec(),
        "UPDATE qc_user:ada SET name = 'Zed';",
        lambda rows: [(row.id, row.name) for row in rows],
        [("ada", "Ada")],
        [("ada", "Zed")],
    ),
    "fetch": _ReadCase(
        lambda c: _qs(QcBook, c).filter(title="Dune").fetch("author").exec(),
        "UPDATE qc_author:ada SET name = 'Zed';",
        _fetched_author,
        ["Ada"],
        ["Zed"],
    ),
}


def _mutate(result: Any) -> None:
    """Change the first row of *result* in place, nested values included (a scalar result has none)."""
    first = result[0] if isinstance(result, list) and result else result
    if isinstance(first, dict):
        for value in first.values():
            if isinstance(value, dict | list):
                value.clear()
        first.clear()
    elif isinstance(first, BaseSurrealModel):
        for value in first.__dict__.values():
            if isinstance(value, dict | list):
                value.clear()
        first.__dict__.update(dict.fromkeys(type(first).model_fields))


def _assert_new_objects(first: Any, second: Any) -> None:
    """A hit is built from a copy: no instance, row or list of it is one the first read returned."""
    if isinstance(first, list | dict | BaseSurrealModel):
        assert second is not first
    if isinstance(first, list) and isinstance(second, list):
        for a, b in zip(first, second, strict=False):
            assert b is not a


@pytest.mark.parametrize("kind", _KINDS)
class TestCachedReadsE2E:
    """AC-5: every read of a cached queryset is answered from the cache, as new objects, on every engine."""

    @pytest.mark.parametrize("case", list(_READ_CASES.values()), ids=list(_READ_CASES))
    async def test_the_second_read_is_answered_from_the_cache(
        self, kind: str, case: _ReadCase, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with cache_db(kind) as client:
            await _seed()
            requests = _requests(monkeypatch, client)
            first = await case.read(True)
            assert requests() >= 1
            assert case.render(first) == case.before
            _mutate(first)
            await client.query(case.change)
            sent = requests()
            second = await case.read(True)
            assert requests() == sent, "the second read sent a request"
            assert case.render(second) == case.before
            _assert_new_objects(first, second)
            assert _counts() == (1, 1)
            assert QueryCache.stats()["entries"] == 1
            assert case.render(await case.read(False)) == case.after  # the database did change

    async def test_without_cache_every_read_is_sent(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with cache_db(kind) as client:
            await _seed()
            requests = _requests(monkeypatch, client)
            for name, case in _READ_CASES.items():
                sent = requests()
                assert case.render(await case.read(False)) == case.before, name
                once = requests() - sent
                assert once >= 1, name
                assert case.render(await case.read(False)) == case.before, name
                assert requests() - sent == 2 * once, name
            assert QueryCache.stats() == {
                "entries": 0,
                "tables": 0,
                "max_size": 1000,
                "default_ttl": 60.0,
                "enabled": True,
                "hits": 0,
                "misses": 0,
            }

    async def test_negative_results_are_cached(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with cache_db(kind) as client:
            await _seed()
            requests = _requests(monkeypatch, client)
            empty = QcItem.objects().filter(category="none").cache()

            async def read_all() -> None:
                assert await empty.exec() == []
                assert await empty.count() == 0
                with pytest.raises(SurrealDbNotFoundError, match="No result found."):
                    await QcItem.objects().cache().get("nope")
                assert await QcGhost.objects().cache().exec() == []  # a table never created

            await read_all()
            await client.query("UPDATE qc_item:i1 SET category = 'none';")
            await client.query("CREATE qc_item:nope SET name = 'n';")
            await client.query("CREATE qc_ghost:g SET x = 1;")
            sent = requests()
            await read_all()
            assert requests() == sent
            assert _counts() == (4, 4)
            assert _names(await QcItem.objects().filter(category="none").exec()) == ["a"]
            assert (await QcItem.objects().get("nope")).name == "n"
            assert [ghost.x for ghost in await QcGhost.objects().exec()] == [1]

    async def test_different_requests_are_separate_entries(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with cache_db(kind) as client:
            await _seed()
            requests = _requests(monkeypatch, client)

            async def read_all() -> list[Any]:
                return [
                    await QcItem.objects().filter(category="book").cache().count(),
                    await QcItem.objects().filter(category="home").cache().count(),
                    sorted(
                        book.id for book in await QcBook.objects().filter(author=RecordID("qc_author", "ada")).cache().exec()
                    ),
                    sorted(book.id for book in await QcBook.objects().filter(author="qc_author:ada").cache().exec()),
                    _names(await QcItem.objects().filter(category="home").order_by("name").cache().exec()),
                ]

            assert await read_all() == [2, 2, ["b1"], [], ["c", "d"]]
            assert requests() == 5
            assert _counts() == (0, 5)
            assert await read_all() == [2, 2, ["b1"], [], ["c", "d"]]
            assert requests() == 5
            assert _counts() == (5, 5)
            assert QueryCache.stats()["entries"] == 5

    async def test_expiry_and_the_readers_ttl(self, kind: str, monkeypatch: pytest.MonkeyPatch, now: list[float]) -> None:
        async with cache_db(kind) as client:
            await _seed()
            requests = _requests(monkeypatch, client)

            def books() -> Awaitable[int]:
                return QcItem.objects().filter(category="book").cache().count()

            assert await books() == 2
            now[0] += 59
            assert await books() == 2
            assert requests() == 1
            now[0] += 2  # 61 s: past the default TTL
            assert await books() == 2
            assert requests() == 2

            def home(ttl: float) -> Awaitable[int]:
                return QcItem.objects().filter(category="home").cache(ttl).count()

            assert await home(300) == 2
            now[0] += 6
            assert await home(300) == 2
            assert requests() == 3
            assert await home(5) == 2  # D10: older than this reader accepts
            assert requests() == 4
            assert await home(5) == 2  # the entry was replaced
            assert await home(300) == 2
            assert requests() == 4

    async def test_a_failing_copy_never_fails_a_read(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """G7: the cache's own failures send the read to the database, never raise."""
        async with cache_db(kind) as client:
            await _seed()
            requests = _requests(monkeypatch, client)

            def books() -> Awaitable[Any]:
                return QcItem.objects().filter(category="book").order_by("name").cache().exec()

            assert _names(await books()) == ["a", "b"]  # stored
            assert requests() == 1

            def broken(value: Any) -> Any:
                raise RuntimeError("no copy")

            monkeypatch.setattr(cache, "_deepcopy", broken)
            assert _names(await books()) == ["a", "b"]  # the hit cannot be copied: the database answers
            assert requests() == 2
            assert QueryCache.stats()["entries"] == 0
            assert _names(await books()) == ["a", "b"]  # nothing can be stored: the database again
            assert _names(await books()) == ["a", "b"]
            assert requests() == 4
            assert QueryCache.stats()["entries"] == 0

    @pytest.mark.parametrize("zero", [0, timedelta(0)], ids=["int", "timedelta"])
    async def test_ttl_zero_is_never_cached(self, kind: str, zero: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """P2: a ``cache(0)`` read, or ``cache()`` under ``default_ttl=0``, goes to the database every time."""
        async with cache_db(kind) as client:
            await _seed()
            requests = _requests(monkeypatch, client)
            for _ in range(2):
                assert await QcItem.objects().filter(category="book").cache(zero).count() == 2
            assert requests() == 2
            assert QueryCache.stats()["entries"] == 0
            assert _counts() == (0, 2)
            QueryCache.configure(default_ttl=zero)
            QueryCache.clear()
            for _ in range(2):
                assert await QcItem.objects().filter(category="book").cache().count() == 2
            assert requests() == 4
            assert QueryCache.stats()["entries"] == 0
            assert _counts() == (0, 2)


@pytest.mark.parametrize("kind", _KINDS)
class TestNotCachedE2E:
    """AC-6: what goes to the database although the queryset has ``cache()``."""

    async def test_reads_in_a_transaction(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """Interactive (WebSocket on 3.x): the read sees the transaction's uncommitted write and nothing is cached.
        Buffered (2.x, HTTP, the embedded engine): it raises exactly as without ``cache()``."""
        async with cache_db(kind) as client:
            await _seed()
            interactive = kind == "ws" and await _line(client) >= 3
            async with SurrealDBConnectionManager.transaction() as tx:
                assert tx.is_interactive is interactive
                if interactive:
                    await QcItem(id="i5", name="e", category="book", price=50).save(tx=tx)
                    requests = _requests(monkeypatch, client)
                    for _ in range(2):
                        assert await QcItem.objects(tx=tx).filter(category="book").cache().count() == 3
                    assert requests() == 2
                else:
                    with pytest.raises(SurrealDbError) as plain:
                        await QcItem.objects(tx=tx).filter(category="book").count()
                    with pytest.raises(SurrealDbError) as cached:
                        await QcItem.objects(tx=tx).filter(category="book").cache().count()
                    assert str(cached.value) == str(plain.value)
            assert QueryCache.stats()["entries"] == 0
            assert _counts() == (0, 0)

    async def test_a_disabled_cache(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with cache_db(kind) as client:
            await _seed()
            QueryCache.configure(enabled=False)
            requests = _requests(monkeypatch, client)
            for _ in range(2):
                assert await QcItem.objects().filter(category="book").cache().count() == 2
                assert (await QcItem.objects().cache().get("i1")).name == "a"
            assert requests() == 4
            assert QueryCache.stats()["entries"] == 0
            assert _counts() == (0, 0)

    async def test_without_a_cbor_encoder(
        self, kind: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with cache_db(kind) as client:
            await _seed()
            monkeypatch.setattr(cache, "cbor_encode", None)
            requests = _requests(monkeypatch, client)
            with caplog.at_level(logging.WARNING, logger="surreal_orm_lite.cache"):
                for _ in range(2):
                    assert await QcItem.objects().filter(category="book").cache().count() == 2
            assert requests() == 2
            assert _warnings(caplog, "surreal_orm_lite.cache") == [(logging.WARNING, _W2)]
            assert QueryCache.stats()["entries"] == 0
            assert _counts() == (0, 2)

    async def test_a_client_without_a_scope(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with cache_db(kind) as client:
            await _seed()
            monkeypatch.setattr(SurrealDBConnectionManager, "_cache_scope", classmethod(lambda cls, client: None))
            requests = _requests(monkeypatch, client)
            for _ in range(2):
                assert await QcItem.objects().filter(category="book").cache().count() == 2
            assert requests() == 2
            assert QueryCache.stats()["entries"] == 0
            assert _counts() == (0, 2)

    async def test_a_write_on_a_cached_queryset_runs_as_without_cache(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with cache_db(kind) as client:
            await _seed()
            requests = _requests(monkeypatch, client)
            books = QcItem.objects().filter(category="book").cache()
            assert await books.bulk_update(price=1) == 2
            assert await books.bulk_update(price=2) == 2
            assert requests() == 2
            assert _counts() == (0, 0)  # a write never reads the cache
            assert [item.price for item in await books.exec()] == [2, 2]
            assert requests() == 3


async def _note_ids() -> list[str]:
    """A cached read of the ``qc_note`` rows the current identity may read."""
    return [note.id for note in await QcNote.objects().order_by("id").cache().exec()]


@pytest.mark.parametrize("kind", ["ws", "http"])
class TestIdentityE2E:
    """AC-9: a cached result never crosses identities (spec M1/M2's record-access fixture)."""

    async def test_each_identity_reads_its_own_rows(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with auth_db(kind) as client:
            requests = _requests(monkeypatch, client)

            async def read(expected: list[str], sends: int) -> None:
                sent = requests()
                assert await _note_ids() == expected
                assert requests() - sent == sends

            await _sign_in_as("a", store=True)()
            await read(["a1"], 1)
            await read(["a1"], 0)
            assert QueryCache.stats()["entries"] == 1
            await _sign_in_as("b", store=True)()
            assert QueryCache.stats()["entries"] == 0
            await read(["b1", "b2"], 1)
            await read(["b1", "b2"], 0)
            await _sign_in_as("a", store=False)()
            assert QueryCache.stats()["entries"] == 0
            await read(["a1"], 1)
            await read(["a1"], 0)
            await SurrealDBConnectionManager.invalidate()
            assert QueryCache.stats()["entries"] == 0
            await read(["a1", "b1", "b2"], 1)
            await read(["a1", "b1", "b2"], 0)

    async def test_a_read_answered_before_a_sign_in_is_not_stored(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with auth_db(kind) as client:
            await _sign_in_as("a", store=True)()
            reached, release = _hold_next_reply(monkeypatch, client)
            held = asyncio.create_task(_note_ids())
            try:
                await _until_held(reached, held)  # a's rows are computed
                await _sign_in_as("b", store=True)()
            finally:
                release.set()
            assert await asyncio.wait_for(held, 10) == ["a1"]
            assert QueryCache.stats()["entries"] == 0
            requests = _requests(monkeypatch, client)
            assert await _note_ids() == ["b1", "b2"]
            assert requests() == 1


# --- AC-7, AC-8, AC-10: every ORM write invalidates ------------------------------------------------------


@contextlib.asynccontextmanager
async def _account_access(client: Any, wanted: bool = True) -> AsyncIterator[None]:
    """``QcAccount``'s access method is removed at the end (its table goes with ``_TABLES``); nothing when not
    *wanted*."""
    try:
        yield
    finally:
        if wanted:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.query(f"REMOVE ACCESS {QcAccount.get_access_name()} ON DATABASE;"), 10)


async def _item(key: str = "i1") -> Any:
    """The stored item *key*, read without the cache."""
    return await QcItem.objects().get(key)


def _items(cached: bool) -> Awaitable[Any]:
    return _qs(QcItem, cached).order_by("id").exec()


def _item_rows(rows: Any) -> list[tuple[Any, ...]]:
    return [(row.id, row.name, row.category, row.price, row.tags) for row in rows]


def _likes(cached: bool) -> Awaitable[Any]:
    return _qs(QcLikes, cached).exec()


def _like_pairs(rows: Any) -> list[tuple[str, str]]:
    return sorted((str(row.in_), str(row.out)) for row in rows)


def _players(cached: bool) -> Awaitable[Any]:
    return _qs(QcPlayer, cached).order_by("id").exec()


def _player_rows(rows: Any) -> list[tuple[Any, Any]]:
    return [(row.id, row.name) for row in rows]


def _accounts(cached: bool) -> Awaitable[Any]:
    return _qs(QcAccount, cached).order_by("email").exec()


def _emails(rows: Any) -> list[str]:
    return [row.email for row in rows]


async def _seed_player(client: Any) -> None:
    await QcPlayer(id="p1", name="ann").save()


async def _seed_account(client: Any) -> None:
    await client.query("CREATE qc_account:x SET email = 'x@qc', password = 'h';")


async def _define_accounts(client: Any) -> None:
    await QcAccount.define_access()


async def _ada() -> Any:
    return await QcUser.objects().get("ada")


async def _relate_by_name(client: Any) -> Any:
    return await (await _ada()).relate("qc_likes", RecordID("qc_post", "p2"))


async def _remove_relation(client: Any) -> None:
    await (await _ada()).remove_relation("qc_likes", RecordID("qc_post", "p1"))


async def _remove_all_relations(client: Any) -> None:
    await (await _ada()).remove_all_relations(QcLikes)


async def _signup_through_the_manager(client: Any) -> None:
    """A record sign-up on the shared connection, then back to root (``store=False``: nothing remembered)."""
    variables = {"email": "manager@qc", "password": _AUTH_PASS}
    await SurrealDBConnectionManager.signup(access=QcAccount.get_access_name(), variables=variables, store=False)
    await SurrealDBConnectionManager.invalidate()


class _WriteCase(NamedTuple):
    """One write of spec §5.4 (AC-7): a cached read of the table it writes, and the write.

    ``table`` tags a ``QueryCache.set()`` entry that must be gone after the write. ``setup`` runs before the read is
    primed; ``change`` is an out-of-band statement run after it, before the write: for a write whose own effect a
    read cannot see (DDL), the change is what the refreshed read shows. ``server``: runs on a server only.
    """

    read: Callable[[bool], Awaitable[Any]]
    render: Callable[[Any], Any]
    write: Callable[[Any], Awaitable[Any]]
    table: str
    setup: Callable[[Any], Awaitable[None]] | None = None
    change: str | None = None
    server: bool = False


def _item_write(write: Callable[[Any], Awaitable[Any]]) -> _WriteCase:
    """A write of ``qc_item:i1`` through the instance read without the cache."""

    async def run(client: Any) -> Any:
        return await write(await _item())

    return _WriteCase(_items, _item_rows, run, "qc_item")


def _qs_write(write: Callable[[], Awaitable[Any]]) -> _WriteCase:
    async def run(client: Any) -> Any:
        return await write()

    return _WriteCase(_items, _item_rows, run, "qc_item")


async def _update_i1(item: Any) -> Any:
    item.price = 11
    return await item.update()


_WRITE_CASES: dict[str, _WriteCase] = {
    "save": _qs_write(lambda: QcItem(id="i5", name="e", category="toy", price=50).save()),
    "upsert": _qs_write(lambda: QcItem(id="i1", name="a2", category="book", price=10).upsert()),
    "update": _item_write(_update_i1),
    "merge": _item_write(lambda item: item.merge(price=12)),
    "patch": _item_write(lambda item: item.patch([{"op": "replace", "path": "/price", "value": 13}])),
    "atomic_append": _item_write(lambda item: item.atomic_append("tags", "y")),
    "atomic_remove": _item_write(lambda item: item.atomic_remove("tags", "x")),
    "atomic_set_add": _item_write(lambda item: item.atomic_set_add("tags", "y")),
    "atomic_increment": _item_write(lambda item: item.atomic_increment("price", 5)),
    "atomic_append_many": _item_write(lambda item: item.atomic_append_many("tags", ["y", "z"])),
    "atomic_set_add_many": _item_write(lambda item: item.atomic_set_add_many("tags", ["x", "y"])),
    "atomic_remove_many": _item_write(lambda item: item.atomic_remove_many("tags", ["x"])),
    "delete": _item_write(lambda item: item.delete()),
    "relate": _WriteCase(_likes, _like_pairs, _relate_by_name, "qc_likes"),
    "remove_relation": _WriteCase(_likes, _like_pairs, _remove_relation, "qc_likes"),
    "remove_all_relations": _WriteCase(_likes, _like_pairs, _remove_all_relations, "qc_likes"),
    "define_computed_fields": _WriteCase(
        _players,
        _player_rows,
        lambda client: QcPlayer.define_computed_fields(),
        "qc_player",
        setup=_seed_player,
        change="UPDATE qc_player:p1 SET name = 'bo';",
    ),
    "edge_save": _WriteCase(
        _likes,
        _like_pairs,
        lambda client: QcLikes(in_=RecordID("qc_user", "bob"), out=RecordID("qc_post", "p2")).save(),
        "qc_likes",
    ),
    "bulk_create": _qs_write(lambda: QcItem.objects().bulk_create([QcItem(id="i5", name="e", category="toy", price=50)])),
    "bulk_update": _qs_write(lambda: QcItem.objects().filter(category="home").bulk_update(price=1)),
    "queryset_patch": _qs_write(
        lambda: QcItem.objects().filter(category="home").patch([{"op": "replace", "path": "/price", "value": 2}])
    ),
    "bulk_delete": _qs_write(lambda: QcItem.objects().filter(category="home").bulk_delete()),
    "delete_table": _qs_write(lambda: QcItem.objects().delete_table()),
    "define_access": _WriteCase(
        _accounts,
        _emails,
        lambda client: QcAccount.define_access(),
        "qc_account",
        setup=_seed_account,
        change="UPDATE qc_account:x SET email = 'y@qc';",
        server=True,
    ),
    "signup": _WriteCase(
        _accounts,
        _emails,
        lambda client: QcAccount.signup(email="new@qc", password=_AUTH_PASS),
        "qc_account",
        setup=_define_accounts,
        server=True,
    ),
    "manager_signup": _WriteCase(
        _items,
        _item_rows,
        _signup_through_the_manager,
        "qc_item",  # D24: every table, not only the access method's
        setup=_define_accounts,
        change="UPDATE qc_item:i1 SET price = 77;",
        server=True,
    ),
    "get_or_create_creating": _qs_write(
        lambda: QcItem.objects().get_or_create(name="e", defaults={"category": "toy", "price": 50})
    ),
    "update_or_create_creating": _qs_write(
        lambda: QcItem.objects().update_or_create(name="e", defaults={"category": "toy", "price": 50})
    ),
    "update_or_create_updating": _qs_write(lambda: QcItem.objects().update_or_create(name="a", defaults={"price": 99})),
}


def _book_titles(rows: Any) -> list[str]:
    return [row.title if isinstance(row, BaseSurrealModel) else row["title"] for row in rows]


@pytest.mark.parametrize("kind", ["mem", "ws"])
class TestWriteInvalidationE2E:
    """AC-7: a cached read of the table a write changes is served fresh right after it."""

    @pytest.mark.parametrize("case", list(_WRITE_CASES.values()), ids=list(_WRITE_CASES))
    async def test_a_write_refreshes_the_cached_reads_of_its_table(
        self, kind: str, case: _WriteCase, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if case.server and kind == "mem":
            pytest.skip("sign-up runs on a server here")
        async with cache_db(kind) as client, _account_access(client, case.server):
            await _seed()
            if case.setup is not None:
                await case.setup(client)
            requests = _requests(monkeypatch, client)
            first = case.render(await case.read(True))
            sent = requests()
            assert case.render(await case.read(True)) == first
            assert requests() == sent  # primed: a hit
            QueryCache.set("user", 1, case.table)
            if case.change is not None:
                await client.query(case.change)
            await case.write(client)
            assert QueryCache.get("user") is None
            sent = requests()
            fresh = case.render(await case.read(True))
            assert requests() == sent + 1, "the read after the write was answered from the cache"
            assert fresh == case.render(await case.read(False))
            assert fresh != first

    async def test_get_or_create_that_finds_invalidates_nothing(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with cache_db(kind) as client:
            await _seed()
            requests = _requests(monkeypatch, client)
            first = _item_rows(await _items(True))
            found, created = await QcItem.objects().get_or_create(name="a", defaults={"price": 99})
            assert (found.id, created) == ("i1", False)
            sent = requests()
            assert _item_rows(await _items(True)) == first
            assert requests() == sent

    @pytest.mark.parametrize("relation", ["relate_name", "relate_model", "remove_relation", "remove_all_relations"])
    async def test_relations_refresh_the_edge_table_not_the_source(
        self, kind: str, relation: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def users() -> Awaitable[Any]:
            return QcUser.objects().order_by("name").cache().exec()

        async with cache_db(kind) as client:
            await _seed()
            requests = _requests(monkeypatch, client)
            likes = _like_pairs(await _likes(True))
            names = _names(await users())
            ada = await _ada()
            if relation == "relate_name":
                await ada.relate("qc_likes", RecordID("qc_post", "p2"))
            elif relation == "relate_model":
                await ada.relate(QcLikes, RecordID("qc_post", "p2"))
            elif relation == "remove_relation":
                await ada.remove_relation(QcLikes, RecordID("qc_post", "p1"))
            else:
                await ada.remove_all_relations(["qc_likes"])
            sent = requests()
            fresh = _like_pairs(await _likes(True))
            assert requests() == sent + 1
            assert fresh == _like_pairs(await _likes(False))
            assert fresh != likes
            sent = requests()
            assert _names(await users()) == names
            assert requests() == sent  # the source table's entry is still a hit

    async def test_a_fetch_read_is_refreshed_by_a_write_to_the_fetched_table(
        self, kind: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def dune() -> Awaitable[Any]:
            return QcBook.objects().filter(title="Dune").fetch("author").cache().exec()

        async with cache_db(kind) as client:
            await _seed()
            requests = _requests(monkeypatch, client)
            assert _fetched_author(await dune()) == ["Ada"]
            await (await QcAuthor.objects().get("ada")).merge(name="Ann")
            sent = requests()
            assert _fetched_author(await dune()) == ["Ann"]
            assert requests() == sent + 1

    async def test_a_subquery_read_is_refreshed_by_a_write_to_the_inner_table(
        self, kind: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def by_bea() -> Awaitable[Any]:
            authors = QcAuthor.objects().filter(name="Bea").select("id")
            return QcBook.objects().filter(author__in=Subquery(authors)).order_by("title").cache().exec()

        async with cache_db(kind) as client:
            await _seed()
            requests = _requests(monkeypatch, client)
            assert _book_titles(await by_bea()) == ["Emma"]
            await (await QcAuthor.objects().get("ada")).merge(name="Bea")
            sent = requests()
            assert _book_titles(await by_bea()) == ["Dune", "Emma"]
            assert requests() == sent + 1

    async def test_a_raw_write_is_seen_once_invalidated(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """D13: ``client.query()`` invalidates nothing; ``QueryCache.invalidate()`` by model, then by table name."""
        async with cache_db(kind) as client:
            await _seed()
            requests = _requests(monkeypatch, client)
            for price, target in ((77, QcItem), (78, "qc_item")):
                first = _item_rows(await _items(True))
                await client.query(f"UPDATE qc_item:i1 SET price = {price};")
                sent = requests()
                assert _item_rows(await _items(True)) == first
                assert requests() == sent
                assert QueryCache.invalidate(target) == 1
                fresh = _item_rows(await _items(True))
                assert requests() == sent + 1
                assert fresh[0][3] == price

    async def test_a_write_that_raises_still_invalidates(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with cache_db(kind) as client:
            await _seed()
            requests = _requests(monkeypatch, client)
            first = _item_rows(await _items(True))
            await client.query("UPDATE qc_item:i2 SET price = 77;")  # unseen by the entry
            with pytest.raises(SurrealDbError, match="already exists"):
                await QcItem(id="i1", name="dup").save()
            sent = requests()
            fresh = _item_rows(await _items(True))
            assert requests() == sent + 1
            assert fresh == _item_rows(await _items(False))
            assert fresh != first

    async def test_signal_handlers_see_the_write(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """While a write runs (pre → around → write → post), its tables are in flight: a handler's cached read goes
        to the database, sees the write and stores nothing."""
        seen: list[tuple[str, int]] = []

        async def on_saved(sender: Any, instance: Any, created: bool, **kwargs: Any) -> None:
            seen.append(("post_save", await QcItem.objects().cache().count()))

        async def around(sender: Any, instance: Any, **kwargs: Any) -> AsyncIterator[None]:
            yield
            seen.append(("around_save", await QcItem.objects().cache().count()))

        async with cache_db(kind) as client:
            await _seed()
            requests = _requests(monkeypatch, client)
            post_save.connect(QcItem)(on_saved)
            around_save.connect(QcItem)(around)
            try:
                assert await QcItem.objects().cache().count() == 4
                await QcItem(id="i5", name="e", category="toy", price=50).save()
            finally:
                post_save.disconnect(on_saved, QcItem)
                around_save.disconnect(around, QcItem)
            assert seen == [("around_save", 5), ("post_save", 5)]
            sent = requests()
            assert await QcItem.objects().cache().count() == 5
            assert requests() == sent + 1  # neither handler's read was stored

    async def test_signal_handlers_get_no_user_entry_of_the_table_written(self, kind: str) -> None:
        """README's ``total_price()``: an entry of ``QueryCache.set()`` tagged with the table a write changes is hidden
        from ``QueryCache.get()`` before the write's handlers run (``around_save`` after its ``yield``, ``post_save``),
        so they compute the value from the database, never from the entry stored before the write."""
        statement = "SELECT math::sum(price) AS total FROM qc_item GROUP ALL"
        key = QueryCache.make_key(statement, {}, "qc_item")
        seen: list[tuple[str, Any, int]] = []
        clients: list[Any] = []

        async def total_price() -> int:
            rows = QueryCache.get(key)
            if rows is None:
                rows = await clients[0].query(statement)
                QueryCache.set(key, rows, "qc_item", ttl=60)
            return int(rows[0]["total"])

        async def on_saved(sender: Any, instance: Any, created: bool, **kwargs: Any) -> None:
            seen.append(("post_save", QueryCache.get(key), await total_price()))

        async def around(sender: Any, instance: Any, **kwargs: Any) -> AsyncIterator[None]:
            yield
            seen.append(("around_save", QueryCache.get(key), await total_price()))

        async with cache_db(kind) as client:
            clients.append(client)
            await _seed()
            assert await total_price() == 100
            assert QueryCache.get(key) is not None  # stored before the write
            post_save.connect(QcItem)(on_saved)
            around_save.connect(QcItem)(around)
            try:
                await QcItem(id="i5", name="e", category="toy", price=50).save()
            finally:
                post_save.disconnect(on_saved, QcItem)
                around_save.disconnect(around, QcItem)
            assert seen == [("around_save", None, 150), ("post_save", None, 150)]
            assert QueryCache.get(key) is None  # what the handlers stored left with the write
            assert await total_price() == 150


@pytest.mark.parametrize("kind", ["mem", "ws"])
class TestDeleteCascadeE2E:
    """AC-7, D21 (M10): deleting a record deletes the edges that point at it, so every delete refreshes the reads of
    edge tables — and only them."""

    @pytest.mark.parametrize("deletion", ["delete", "bulk_delete", "delete_table"])
    async def test_deleting_a_node_refreshes_the_edge_reads(
        self, kind: str, deletion: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def like_count() -> Awaitable[int]:
            return QcLikes.objects().cache().count()

        def likers() -> Awaitable[Any]:
            return QcUser.objects().filter(id__in=Subquery(QcLikes.objects().select("in"))).cache().exec()

        def user_count() -> Awaitable[int]:
            return QcUser.objects().cache().count()

        async with cache_db(kind) as client:
            await _seed()
            requests = _requests(monkeypatch, client)
            assert await like_count() == 1
            assert _names(await likers()) == ["Ada"]
            assert await user_count() == 2
            if deletion == "delete":
                await (await QcPost.objects().get("p1")).delete()
            elif deletion == "bulk_delete":
                await QcPost.objects().bulk_delete()
            else:
                await QcPost.objects().delete_table()
            sent = requests()
            assert await like_count() == 0
            assert await likers() == []
            assert requests() == sent + 2
            assert await user_count() == 2
            assert requests() == sent + 2  # a read without edges is still a hit


@pytest.mark.parametrize("kind", ["mem", "ws"])
class TestDeleteTableInTransactionE2E:
    """AC-7 (M11): ``delete_table()`` runs at once, outside ``objects(tx=)``: it invalidates when it ends, never at a
    commit, and a rollback does not leave a deleted row in the cache."""

    async def test_delete_table_in_a_transaction_invalidates_at_once(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        def posts() -> Awaitable[Any]:
            return QcPost.objects().order_by("id").cache().exec()

        def post_count() -> Awaitable[int]:
            return QcPost.objects().cache().count()

        async with cache_db(kind) as client:
            await _seed()
            requests = _requests(monkeypatch, client)
            assert [post.id for post in await posts()] == ["p1", "p2"]
            assert await post_count() == 2
            with pytest.raises(RuntimeError, match="roll back"):
                async with SurrealDBConnectionManager.transaction() as tx:
                    assert await QcPost.objects(tx=tx).delete_table() is True
                    sent = requests()
                    assert await post_count() == 0  # not deferred to the commit
                    assert requests() == sent + 1
                    raise RuntimeError("roll back")
            sent = requests()
            assert await posts() == []  # not lost with the rollback
            assert requests() == sent + 1
            assert await post_count() == 0  # stored inside the block, after the delete: still right
            assert requests() == sent + 1


class TestDdlInTransactionE2E:
    """AC-7: ``define_computed_fields(tx=)`` and ``define_access(tx=)`` join the transaction (``"cls"`` reads the
    bound ``tx``): a cached read outside it keeps hitting until the commit, and is fresh after it."""

    @pytest.mark.parametrize("kind", ["mem", "ws"])
    async def test_define_computed_fields(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with cache_db(kind) as client:
            await _seed_player(client)
            requests = _requests(monkeypatch, client)
            assert _player_rows(await _players(True)) == [("p1", "ann")]
            async with SurrealDBConnectionManager.transaction() as tx:
                assert await QcPlayer.define_computed_fields(tx=tx) == QcPlayer.computed_field_ddl()
                sent = requests()
                assert _player_rows(await _players(True)) == [("p1", "ann")]
                assert requests() == sent  # the committed state has not changed: a hit
            sent = requests()
            assert _player_rows(await _players(True)) == [("p1", "ann")]
            assert requests() == sent + 1  # invalidated at the commit

    @pytest.mark.parametrize("kind", ["ws"])
    async def test_define_access(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with cache_db(kind) as client, _account_access(client):
            await _seed_account(client)
            requests = _requests(monkeypatch, client)
            assert _emails(await _accounts(True)) == ["x@qc"]
            async with SurrealDBConnectionManager.transaction() as tx:
                assert await QcAccount.define_access(tx=tx) == QcAccount.access_ddl()
                sent = requests()
                assert _emails(await _accounts(True)) == ["x@qc"]
                assert requests() == sent
            sent = requests()
            assert _emails(await _accounts(True)) == ["x@qc"]
            assert requests() == sent + 1


def _author_names(rows: Any) -> list[tuple[str, str]]:
    """``(title, author's name)`` of each book read with a projection of the linked author."""
    return [(row["title"], row["author"]["name"]) for row in rows]


def _dotted(qs: Any, cached: bool) -> Awaitable[Any]:
    return (qs.cache() if cached else qs).exec()


#: D22 and P3: reads that follow the ``author`` link — ``(read, render, the author's new name, before, after)``.
#: ``order_by`` alone keeps its order: SurrealDB orders ``SELECT *`` by the output document, where the link is not
#: followed. The read still follows a link on its face, so the write refreshes it (a request, the database's rows).
_DOTTED_READS: dict[str, tuple[Callable[[bool], Awaitable[Any]], Callable[[Any], Any], str, Any, Any]] = {
    "filter": (
        lambda c: _dotted(QcBook.objects().filter(**{"author.name": "Ada"}), c),
        _book_titles,
        "Bea",
        ["Dune"],
        [],
    ),
    "order_by": (
        lambda c: _dotted(QcBook.objects().order_by("author.name"), c),
        _book_titles,
        "Zoe",
        ["Dune", "Emma"],
        ["Dune", "Emma"],
    ),
    "select": (
        lambda c: _dotted(QcBook.objects().select("title", "author.name").order_by("title"), c),
        _author_names,
        "Zoe",
        [("Dune", "Ada"), ("Emma", "Bea")],
        [("Dune", "Zoe"), ("Emma", "Bea")],
    ),
    "select_star": (
        lambda c: _dotted(QcBook.objects().select("title", "author.*").order_by("title"), c),
        _author_names,
        "Bea",
        [("Dune", "Ada"), ("Emma", "Bea")],
        [("Dune", "Bea"), ("Emma", "Bea")],
    ),
}


@pytest.mark.parametrize("kind", ["mem", "ws"])
class TestDottedPathsE2E:
    """AC-7, D22, P3: a read that follows a link is refreshed by a write to the linked table."""

    @pytest.mark.parametrize("read", list(_DOTTED_READS))
    async def test_a_write_to_the_linked_table_refreshes_the_read(
        self, kind: str, read: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run, render, name, before, after = _DOTTED_READS[read]
        async with cache_db(kind) as client:
            await _seed()
            requests = _requests(monkeypatch, client)
            assert render(await run(True)) == before
            sent = requests()
            assert render(await run(True)) == before
            assert requests() == sent  # primed: a hit
            await (await QcAuthor.objects().get("ada")).merge(name=name)
            sent = requests()
            assert render(await run(True)) == after
            assert requests() == sent + 1
            assert render(await run(False)) == after


async def _raw_ws_client() -> Any:
    """A connection of its own to the server of ``SURREALDB_PORT`` (not the ORM's), as root on ``ns``/``db``."""
    other = AsyncSurreal(_ws_url())
    await other.connect(_ws_url())
    await other.signin({"username": "root", "password": "root"})
    await other.use("ns", "db")
    return other


class TestTransactionsE2E:
    """AC-8: a write in a transaction is noted and invalidated at the commit — a failed one too —, never on a
    rollback; deferred ``post_save`` handlers run after the invalidation."""

    @pytest.mark.parametrize("kind", ["ws", "http"])
    async def test_a_write_is_invalidated_at_the_commit(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[int] = []

        async def on_saved(sender: Any, instance: Any, created: bool, **kwargs: Any) -> None:
            seen.append(await QcItem.objects().cache().count())

        async with cache_db(kind) as client:
            await _seed()
            requests = _requests(monkeypatch, client)
            first = _item_rows(await _items(True))
            assert await QcItem.objects().cache().count() == 4
            post_save.connect(QcItem)(on_saved)
            try:
                async with SurrealDBConnectionManager.transaction() as tx:
                    await QcItem(id="i5", name="e", category="toy", price=50).save(tx=tx)
                    sent = requests()
                    assert _item_rows(await _items(True)) == first  # the committed state, from the cache
                    assert requests() == sent
                    assert seen == []
            finally:
                post_save.disconnect(on_saved, QcItem)
            assert seen == [5]  # the deferred handler's cached count() ran after the invalidation
            sent = requests()
            fresh = _item_rows(await _items(True))
            assert requests() == sent + 1
            assert ("i5", "e", "toy", 50, []) in fresh

    @pytest.mark.parametrize("kind", ["ws", "http"])
    async def test_a_rollback_leaves_the_entries(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with cache_db(kind) as client:
            await _seed()
            requests = _requests(monkeypatch, client)
            first = _item_rows(await _items(True))
            with pytest.raises(RuntimeError, match="roll back"):
                async with SurrealDBConnectionManager.transaction() as tx:
                    await QcItem(id="i5", name="e", category="toy", price=50).save(tx=tx)
                    raise RuntimeError("roll back")
            sent = requests()
            assert _item_rows(await _items(True)) == first
            assert requests() == sent

    @pytest.mark.parametrize("kind", ["ws", "http"])
    async def test_a_failed_buffered_commit_still_invalidates(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """A duplicate id in the batch: the commit raises, its state is unknown, the tables are invalidated."""
        async with cache_db(kind) as client:
            await _seed()
            interactive = kind == "ws" and await _line(client) >= 3
            requests = _requests(monkeypatch, client)
            first = _item_rows(await _items(True))
            await client.query("UPDATE qc_item:i2 SET price = 77;")  # unseen by the entry
            duplicate = QcItem(id="i1", name="dup")
            with pytest.raises(SurrealDbError, match="already exists"):
                if interactive:  # the manager would open an interactive transaction here: buffer by hand
                    tx = BufferedTransaction(client)
                    await duplicate.save(tx=tx)
                    await tx.commit()
                else:
                    async with SurrealDBConnectionManager.transaction() as tx:
                        await duplicate.save(tx=tx)
            sent = requests()
            fresh = _item_rows(await _items(True))
            assert requests() == sent + 1
            assert fresh == _item_rows(await _items(False))
            assert fresh != first

    async def test_a_failed_interactive_commit_still_invalidates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two transactions write ``qc_item:i1``; the other one commits first, so the ORM's commit is a conflict."""
        async with cache_db("ws") as client:
            if await _line(client) < 3:
                pytest.skip("interactive transactions need SurrealDB 3.x")
            await _seed()
            requests = _requests(monkeypatch, client)
            first = _item_rows(await _items(True))
            other = await _raw_ws_client()
            try:
                with pytest.raises(SurrealDbConflictError):
                    async with SurrealDBConnectionManager.transaction() as tx:
                        assert tx.is_interactive
                        item = await QcItem.objects(tx=tx).get("i1")
                        await item.merge(tx=tx, price=11)
                        rival = InteractiveTransaction(other, await other.begin())
                        await rival.run_read("SELECT * FROM qc_item:i1;", {})
                        await rival.add("UPDATE qc_item:i1 SET price = 22;", {})
                        await rival.commit()
            finally:
                with contextlib.suppress(Exception):
                    await other.close()
            sent = requests()
            fresh = _item_rows(await _items(True))
            assert requests() == sent + 1
            assert fresh == _item_rows(await _items(False))
            assert fresh[0][3] == 22
            assert fresh != first


class TestRaceE2E:
    """AC-10: a read answered before a write completes is returned to its caller, never stored."""

    async def test_a_read_answered_before_a_write_is_not_stored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def books() -> Awaitable[Any]:
            return QcItem.objects().filter(category="book").order_by("name").cache().exec()

        async with cache_db("mem") as client:
            await _seed()
            reached, release = _hold_next_reply(monkeypatch, client)
            held = asyncio.create_task(books())
            try:
                await _until_held(reached, held)  # the old rows are computed
                await QcItem(id="i5", name="e", category="book", price=50).save()
            finally:
                release.set()
            assert _names(await asyncio.wait_for(held, 10)) == ["a", "b"]
            assert QueryCache.stats()["entries"] == 0
            requests = _requests(monkeypatch, client)
            assert _names(await books()) == ["a", "b", "e"]
            assert requests() == 1


# --- AC-14: README section 29, the tables it touches and its examples (spec §11; D13, D16, P1, P2, P3, P5) -----

_SECTION_28 = "### 28. Subqueries (`Subquery`)"
_SECTION_29 = "### 29. Query cache (`cache()`, `QueryCache`)"
_COMING_FROM_29 = "#### Coming from the full SurrealDB-ORM"
#: Spec §11: section 29's subsections, in order, after an untitled intro.
_SUBSECTIONS_29 = [
    "What is cached",
    "Time to live",
    "Invalidation",
    "One cache per connection and identity",
    "Configuration and statistics",
    "Coming from the full SurrealDB-ORM",
]
#: The row v0.28.0 adds to the 2.x/3.x table, cell by cell (spec §11: 37, 167, 100 and 7 characters).
_QUERY_CACHE_ROW = [
    "Query cache (`cache()`, `QueryCache`)",
    "same on both lines: client-side, per connection and identity; writes in a transaction invalidate at commit; "
    "reads in one are never cached (on 2.x they raise: buffered)",
    "the same; reads inside an interactive transaction are never cached (they see its uncommitted writes)",
    "v0.28.0",
]
#: The clause v0.28.0 adds to the 2.x/3.x table's intro paragraph.
_HEADER_CLAUSE_28 = "rows added in v0.28.0 on 2.7.0 and 3.3.4, identical on 3.1.5"
#: Spec §11 (revision 3, P5): what the cache cannot see, one bullet of "Invalidation" holding each group of names.
_CANNOT_SEE = {
    "another process or client": ("another process",),
    "raw SurrealQL": ("`raw_query()`", "`QuerySet.query()`", "`call_function()`", "`Transaction.add()`"),
    "a write cancelled or failed client-side": ("cancelled", "after its request was sent"),
    "an access method's own clauses": ("`SIGNIN`", "`SIGNUP`"),
    "what the server derives": ("`DEFINE EVENT`", "`REFERENCE … ON DELETE`", "AS SELECT", "`PERMISSIONS`"),
    "values computed when the read runs": ("`Var()`", "`DEFINE PARAM`", "`<future>`", "`COMPUTED`"),
    "relations through a model that is not an edge": ("`BaseSurrealEdge`",),
}


def _section_29() -> str:
    return _readme_part(_SECTION_29)


def _section_29_blocks(language: str) -> list[str]:
    """Section 29's fenced blocks of *language*, in order."""
    return re.findall(rf"^```{language}\n(.*?)^```$", _section_29(), re.MULTILINE | re.DOTALL)


def _part_29(heading: str) -> str:
    """One ``####`` subsection of section 29, whitespace normalised."""
    return _normalised(_subsection(f"#### {heading}", _section_29()))


def _bullets(text: str) -> list[str]:
    """The ``- `` bullets of *text*, each with its continuation lines, whitespace normalised."""
    return [_normalised(bullet) for bullet in re.findall(r"^- .*?(?=^- |^\s*$|\Z)", text, re.MULTILINE | re.DOTALL)]


class TestQueryCacheDocs:
    """Unit: what README section 29, the features table, the 2.x/3.x behaviour table, the comparison and the
    roadmap say about the query cache (spec §11, AC-14; P1, P2, P3, P5)."""

    def test_section_29_follows_section_28(self) -> None:
        """Right after section 28 (after its "Coming from" bullets), then section 30 (v0.29.0), section 31
        (v0.30.0), section 32 (v0.31.0), section 33 (v0.32.0) and section 34 (v0.33.0), and before the ``---`` of
        "Configuration Options"."""
        readme = _readme()
        start_28 = readme.index(f"\n{_SECTION_28}\n")
        start_29 = readme.index(f"\n{_SECTION_29}\n")
        configuration = readme.index("\n## Configuration Options\n")
        assert start_28 < start_29 < configuration
        assert re.findall(r"^#{2,3} .*$", readme[start_28 + 1 : configuration], re.MULTILINE) == [
            _SECTION_28,
            _SECTION_29,
            "### 30. Multiple databases (`add_connection()`, `using()`)",
            "### 31. Schema introspection (`DatabaseIntrospector`, `generate_models_from_db()`)",
            "### 32. Server-side events and stored functions (`Event`, `define_events()`, `define_function()`)",
            "### 33. Materialized views (`view_query`, `define_view()`)",
            "### 34. Relation constraints (`relation_in`, `define_relation()`)",
        ]
        assert readme[:configuration].rstrip().endswith("\n---"), "section 29 ends before the --- of the next part"

    def test_subsections_in_order(self) -> None:
        section = _section_29()
        assert re.findall(r"^#### (.*)$", section, re.MULTILINE) == _SUBSECTIONS_29
        intro = section.split("\n#### ", 1)[0]
        assert "```python\n" in intro, "the untitled intro holds the first example"

    def test_coming_from_the_full_orm(self) -> None:
        """What differs from the full ORM; no bullet about ``configure()``'s semantics, which are the same (P1)."""
        section = _section_29()
        assert re.search(r"^#### Coming from the full SurrealDB-ORM$", section, re.MULTILINE), "heading missing"
        text = _normalised(_subsection(_COMING_FROM_29, section))
        for name in (
            "`cache(",
            "`QueryCache`",
            "`exec()`",
            "post_save",
            "`configure()`",
            "default=str",
            "sort_keys",
            "FIFO",
            "`timedelta`",
            "`hits`",
            "`live()`",
            "never cached",
        ):
            assert name in text, name
        assert "keeps what you do not pass" not in text
        assert _bullets(_subsection(_COMING_FROM_29, section)), "the differences are bullets"

    def test_what_is_cached(self) -> None:
        text = _part_29("What is cached")
        for name in (
            "`exec()`",
            "`get(<id>)`",
            "`all()`",
            "`count()`",
            "`exists()`",
            "`query()`",
            "`live()`",
            "`watch()`",
            "`changes()`",
            "`as_of()`",
            "`refresh()`",
            "`raw_query()`",
            "`Subquery`",
            "mem://",
        ):
            assert name in text, name

    def test_time_to_live(self) -> None:
        """P2: ``0`` means never cached; negative or non-finite values are refused."""
        text = _part_29("Time to live")
        for name in ("`0`", "never cached", "`timedelta`", "60", "non-finite", "oldest result"):
            assert name in text, name

    def test_invalidation(self) -> None:
        """P3 (an expression in ``select()``) and P5 (the completed and corrected list of what the cache cannot see)."""
        text = _part_29("Invalidation")
        for name in (
            "DEFINE EVENT",
            "REFERENCE",
            "COMPUTED",
            "<future>",
            "`Var()`",
            "AS SELECT",
            "`raw_query()`",
            "`Transaction.add()`",
            "`QueryCache.invalidate(",
            "`fetch()`",
            "author.name",
            "`select()`",
            "SIGNIN",
            "cancelled",
            "BaseSurrealEdge",
            "`delete_table()`",
            "another process",
            '"author.*"',
            "`SurrealDBConnectionManager.signup()`",
            "commit",
        ):
            assert name in text, name
        assert "`SurrealFunc` filter" not in text and "SurrealFunc" not in text, "P5: a SurrealFunc is bound, not seen"

    @pytest.mark.parametrize("needles", list(_CANNOT_SEE.values()), ids=list(_CANNOT_SEE))
    def test_what_the_cache_cannot_see_is_a_bullet_list(self, needles: tuple[str, ...]) -> None:
        bullets = _bullets(_subsection("#### Invalidation", _section_29()))
        assert [bullet for bullet in bullets if all(needle in bullet for needle in needles)], bullets

    def test_one_cache_per_connection_and_identity(self) -> None:
        text = _part_29("One cache per connection and identity")
        for name in (
            "`invalidate()`",
            "memory",
            "`QueryCache.set()`",
            "USE",
            "`QueryCache.clear()`",
            "`embedded_database()`",
            "never cross",
        ):
            assert name in text, name

    def test_configuration_and_statistics(self) -> None:
        """P1: every call sets all three settings, never clears; ``enabled=False`` hides; ``clear()`` drops."""
        text = _part_29("Configuration and statistics")
        for name in (
            "enabled=False",
            "hides",
            "never clears",
            "`clear()`",
            "`stats()`",
            "`make_key()`",
            "`raw_query()`",
            "takes its default",
        ):
            assert name in text, name
        for key in ("entries", "tables", "max_size", "default_ttl", "enabled", "hits", "misses"):
            assert f"`{key}`" in text, key

    def test_features_row(self) -> None:
        features = [cells for _, cells in _table_rows(_readme_part("## Features"))]
        firsts = [cells[0] for cells in features]
        row = ["Query cache (`cache()`, TTL, invalidated by ORM writes)", "✅"]
        assert row in features, features
        assert firsts.index(row[0]) == firsts.index("Subqueries (`Subquery`, a QuerySet as a filter value)") + 1

    def test_server_behaviour_row(self) -> None:
        """One row, Since v0.28.0, in version order before the table's last row (later releases add their rows in
        version order)."""
        assert [len(cell) for cell in _QUERY_CACHE_ROW] == [37, 167, 100, 7]
        assert_server_rows([_QUERY_CACHE_ROW])

    def test_server_behaviour_table_keeps_its_widths(self) -> None:
        """Every line 535 characters, the columns 136, 175, 181 and 38 wide: no line re-padded."""
        assert_server_table()

    def test_server_behaviour_header_clause(self) -> None:
        """The header's clause names v0.28.0's lines, in version order among the other releases' sentences."""
        assert_server_header_sentence(_HEADER_CLAUSE_28)

    def test_comparison_row(self) -> None:
        comparison = [cells for _, cells in _table_rows(_readme_part("## SurrealDB-ORM-lite vs SurrealDB-ORM"))]
        firsts = [cells[0] for cells in comparison]
        start = firsts.index("Subqueries") + 1
        assert comparison[start] == ["Query Cache", "✅ v0.28.0 (`cache()`, `QueryCache`)", "✅"], comparison
        assert comparison[start + 1][0] == "Multi-database", comparison

    def test_roadmap_split(self) -> None:
        roadmap = [cells for _, cells in _table_rows(_readme_part("## Roadmap"))]
        firsts = [cells[0] for cells in roadmap]
        start = firsts.index("v0.27.0") + 1
        assert roadmap[start : start + 2] == [
            ["v0.28.0", "Query cache", "✅ Released"],
            ["v0.29.0", "Tier 2 — Multi-database", "✅ Released"],
        ], roadmap
        assert "v0.28.0 – v0.29.0" not in firsts

    def test_tables_have_no_raw_pipe_in_a_cell(self) -> None:
        """Every table v0.28.0 touches keeps one cell count per line and no escaped pipe."""
        parts = [
            _section_29(),
            _readme_part("## Features"),
            _readme_part(_SERVER_BEHAVIOUR),
            _readme_part("## Roadmap"),
            _readme_part("## SurrealDB-ORM-lite vs SurrealDB-ORM"),
        ]
        tables = [lines for part in parts for lines in _tables(part)]
        assert len(tables) >= 4, "the four tables v0.28.0 touches"
        for lines in tables:
            assert len({len(_cells(line)) for line in lines}) == 1 and "\\|" not in "".join(lines), lines

    def test_section_29_fences(self) -> None:
        """Python blocks only, which the E2E test runs."""
        fences = re.findall(r"^```(\S*)$", _section_29(), re.MULTILINE)
        assert fences and fences[1::2] == [""] * (len(fences) // 2) and set(fences[0::2]) == {"python"}, fences


#: The results section 29's comments state, by the names its examples assign (AC-14: each one asserted).
_STATED_RESULTS_29 = {"books", "again", "hits", "fresh", "n", "lamp", "stale", "price", "ttl", "total", "cached_total"}


def _rendered(value: Any) -> str:
    """How a section 29 comment writes *value*: a record by its name, records by their sorted names, else ``str()``."""
    if isinstance(value, BaseSurrealModel):
        return str(value.name)  # type: ignore[attr-defined]
    if isinstance(value, list):
        return ", ".join(sorted(_rendered(item) for item in value)) if value else "[]"
    return str(value)


def _stated_results(blocks: list[str]) -> dict[str, str]:
    """``{name: comment}`` for each line of *blocks* that assigns a result and states it in a trailing comment."""
    stated: dict[str, str] = {}
    for block in blocks:
        for name, comment in re.findall(r"^(\w+) = .*?  # (.+)$", block, re.MULTILINE):
            assert name not in stated, f"{name} is stated twice"
            stated[name] = comment
    return stated


class TestQueryCacheReadmeExamplesE2E:
    """AC-14: every ``python`` block of README section 29 runs as written, in order and in one namespace, on
    ``embedded_database("mem://")`` with the cache reset first; every result a comment states is the one the
    examples got."""

    async def test_section_29_examples(self) -> None:
        blocks = _section_29_blocks("python")
        stated = _stated_results(blocks)
        assert set(stated) == _STATED_RESULTS_29, sorted(stated)
        namespace: dict[str, Any] = {"__name__": "readme_section_29"}
        QueryCache.configure()
        QueryCache.clear()
        try:
            async with embedded_database("mem://"):
                for code in blocks:
                    compiled = compile(code, "<README section 29>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                    # A doctest of this repository's own README (trusted text), top-level awaits included.
                    outcome = eval(compiled, namespace)
                    if inspect.iscoroutine(outcome):
                        await outcome

                for name, comment in stated.items():
                    got = _rendered(namespace[name])
                    assert comment == got or comment.startswith(f"{got} ("), (name, comment, got)
        finally:
            namespace.clear()
