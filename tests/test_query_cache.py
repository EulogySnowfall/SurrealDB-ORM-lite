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

import asyncio
import gc
import hashlib
import inspect
import logging
import math
import random
import re
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any

import pytest
from surrealdb import RecordID

import surreal_orm_lite
from surreal_orm_lite import (
    BaseSurrealModel,
    Count,
    GeoDistance,
    PointField,
    Q,
    QueryCache,
    Subquery,
    SurrealConfigDict,
    SurrealDbError,
    cache,
    encoding,
)
from surreal_orm_lite._sdk import cbor_encode
from surreal_orm_lite.cache import EDGES

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
