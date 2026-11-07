"""``QuerySet.first()``'s order (v0.50.0, spec §5.4, D22, D26, AC-21).

On SurrealDB 3.x, 4.x and the embedded engine (a 4.0 core), a ``first()`` whose compiled statement has no ``ORDER BY``
of its own ends ``ORDER BY id LIMIT 1``: it returns the matching record with the smallest id, an indexed range filter
included (SurrealDB 4.x promises no order without ``ORDER BY``, and every line answers an indexed filter in index
order). Four states keep their own order and get no ``ORDER BY id``: ``order_by()``, ``similar_to()``,
``hybrid_search()`` (which never goes through ``first()``'s statement) and the ``values()``/``annotate()`` aggregation.
``nearby()``, ``search()`` without ``order_by()``, ``select()``, ``fetch()``, a projection through a link and
``as_of()`` rank nothing by themselves: they get it.

On SurrealDB 2.x the statement is unchanged, and ``first()`` returns what the server yields first, as in v0.40.3: id
order for a plain table scan, index order for an indexed filter. Those orders were measured on 2.7.0 (2026-11-06,
through the ORM, on this file's seed) and are pinned here as "server order, unchanged".

The line is read once per client, as for the KNN pre-filter, and the two compiled forms never share a cache key.
"""

from __future__ import annotations

import contextlib
import os
import random
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from surreal_orm_lite import (
    Analyzer,
    BaseSurrealModel,
    Count,
    GeometryPoint,
    Index,
    QueryCache,
    SurrealConfigDict,
    SurrealDBConnectionManager,
)
from surreal_orm_lite import query_set as _query_set
from surreal_orm_lite.testing import embedded_database
from surreal_orm_lite.time_travel import line_major
from tests import _sdk_calls
from tests._clock import now as _now
from tests.conftest import orm_client

_TABLE = "ord_t"
_FRIEND = "ord_friend"


class OrdT(BaseSurrealModel):
    """Records ``ord_t:1..200`` created in shuffled order: ``r = 201 - n`` (indexed, the inverse of the id order),
    ``g = n % 10`` (indexed), ``n`` (not indexed), a full-text indexed ``title``, a point and a vector."""

    model_config = SurrealConfigDict(
        table_name=_TABLE,
        indexes=[
            Index("ord_t_r", "r"),
            Index("ord_t_g", "g"),
            Index("ord_t_title", "title", search_analyzer=Analyzer("ord_plain", ["blank"], ["lowercase"])),
        ],
    )

    id: str | None = None
    r: int = 0
    g: int = 0
    n: int = 0
    title: str = ""
    loc: GeometryPoint | None = None
    emb: list[float] = []
    friend: Any = None


def _creates() -> str:
    """One statement creating ``ord_t:1..200`` in a fixed shuffled order, then two string ids, ``ord_t:⟨aa⟩`` and
    ``ord_t:⟨zz⟩`` (record ids order numbers before strings), each linked to ``ord_friend:f``."""
    numbers = list(range(1, 201))
    random.Random(7).shuffle(numbers)
    rows = [
        f"CREATE {_TABLE}:{n} SET r = {201 - n}, g = {n % 10}, n = {n}, title = '{'word' if n % 2 else 'other'}', "
        f"loc = ({n / 10}, 0.0), emb = [{float(n)}, 0.0], friend = {_FRIEND}:f;"
        for n in numbers
    ]
    rows.append(f"CREATE {_TABLE}:⟨zz⟩ SET r = 1000, g = 3, n = 1000, title = 'word', emb = [500.0, 0.0];")
    rows.append(f"CREATE {_TABLE}:⟨aa⟩ SET r = 2000, g = 3, n = 2000, title = 'other', emb = [600.0, 0.0];")
    rows.append(f"CREATE {_FRIEND}:f SET n = 7;")
    return "\n".join(rows)


@contextlib.asynccontextmanager
async def order_db(kind: str) -> AsyncIterator[tuple[Any, int]]:
    """The ORM on *kind* (``"ws"``: the server of ``SURREALDB_PORT``; ``"mem"``: a fresh embedded engine), seeded;
    yields ``(client, line)``."""
    context = embedded_database("mem://") if kind == "mem" else orm_client(_TABLE, _FRIEND)
    async with context as client:
        await OrdT.define_indexes()
        await client.query(_creates())
        yield client, line_major(await client.version())


def _statements(monkeypatch: pytest.MonkeyPatch, client: Any) -> list[str]:
    """The statements the ORM sends for *client* from now on."""
    return _sdk_calls.record(monkeypatch, client, *_sdk_calls.QUERIES, entry=lambda call: str(call.target))


def _assert_ordered(statement: str, line: int) -> None:
    """*statement* is ``first()``'s, with ``ORDER BY id`` from SurrealDB 3 on and unchanged below."""
    if line >= 3:
        assert statement.rstrip(";").endswith(" ORDER BY id LIMIT 1"), statement
    else:
        assert "ORDER BY" not in statement, statement
        assert statement.rstrip(";").endswith(" LIMIT 1"), statement


@pytest.mark.parametrize("kind", ["ws", "mem"])
class TestFirstOrderE2E:
    """AC-21 on the line of ``SURREALDB_PORT`` (2.x, 3.x or 4.x) and on the embedded engine."""

    async def test_plain_scan(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with order_db(kind) as (client, line):
            sent = _statements(monkeypatch, client)
            # Every line: the smallest id (a table scan is in id order on 2.x and 3.x).
            assert (await OrdT.objects().first()).id == "1"
            _assert_ordered(sent[-1], line)

    async def test_indexed_range_filter(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with order_db(kind) as (client, line):
            sent = _statements(monkeypatch, client)
            found = await OrdT.objects().filter(r__gt=5).first()
            _assert_ordered(sent[-1], line)
            if line >= 3:
                assert found.id == "1"
            else:
                # 2.x: server order, unchanged (index order: the smallest r, the largest id), measured on 2.7.0.
                assert found.id == "195"

    async def test_indexed_equality_filter(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with order_db(kind) as (client, line):
            sent = _statements(monkeypatch, client)
            # 3.x/4.x: the smallest id. 2.x: server order, unchanged (id order within one index key), measured on 2.7.0.
            assert (await OrdT.objects().filter(g=3).first()).id == "3"
            _assert_ordered(sent[-1], line)

    async def test_filter_on_a_field_without_index(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with order_db(kind) as (client, line):
            sent = _statements(monkeypatch, client)
            assert (await OrdT.objects().filter(n__gt=50).first()).id == "51"
            _assert_ordered(sent[-1], line)

    async def test_the_queryset_stays_unordered(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with order_db(kind) as (client, _line):
            sent = _statements(monkeypatch, client)
            qs = OrdT.objects().filter(n__lte=3)
            await qs.first()
            rows = await qs.exec()
            assert "ORDER BY" not in sent[-1]
            assert sorted(row.id for row in rows) == ["1", "2", "3"]

    async def test_order_by_is_kept(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with order_db(kind) as (client, _line):
            sent = _statements(monkeypatch, client)
            assert (await OrdT.objects().order_by("-r").first()).id == "aa"
            assert "ORDER BY r DESC LIMIT 1" in sent[-1]
            assert "ORDER BY id" not in sent[-1]

    async def test_similar_to_keeps_its_distance_order(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with order_db(kind) as (client, _line):
            sent = _statements(monkeypatch, client)
            nearest = await OrdT.objects().similar_to("emb", [3.2, 0.0], limit=3, dist="EUCLIDEAN").first()
            assert nearest.id == "3"
            assert "ORDER BY _knn_distance" in sent[-1]
            assert "ORDER BY id" not in sent[-1]

    async def test_aggregation_gets_no_id_order(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with order_db(kind) as (client, _line):
            sent = _statements(monkeypatch, client)
            row = await OrdT.objects().filter(n__lte=200).values("g").annotate(c=Count()).first()
            assert row["c"] == 20
            assert "GROUP BY" in sent[-1]
            assert "ORDER BY" not in sent[-1]

    async def test_nearby_gets_the_id_order(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with order_db(kind) as (client, line):
            sent = _statements(monkeypatch, client)
            # Points (n / 10, 0): within ~120 km of (10, 0) are n = 90 to 110.
            found = await OrdT.objects().nearby("loc", (10.0, 0.0), max_distance=120_000).first()
            _assert_ordered(sent[-1], line)
            assert found.id == "90"

    async def test_search_without_order_by_gets_the_id_order(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with order_db(kind) as (client, line):
            sent = _statements(monkeypatch, client)
            found = await OrdT.objects().search(title="word").first()
            _assert_ordered(sent[-1], line)
            if line >= 3:
                assert found.id == "1"
            else:
                # 2.x: server order, unchanged (the full-text index's order): any match.
                assert found.n % 2 == 1 or found.id == "zz"

    async def test_select_fetch_and_link_projection_get_the_id_order(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with order_db(kind) as (client, line):
            sent = _statements(monkeypatch, client)
            row = await OrdT.objects().filter(r__gt=5).select("n").first()
            _assert_ordered(sent[-1], line)
            if line >= 3:
                assert (row["n"] if isinstance(row, dict) else row.n) == 1
            fetched = await OrdT.objects().filter(g=3).fetch("friend").first()
            _assert_ordered(sent[-1].split(" FETCH ")[0], line)
            assert sent[-1].rstrip(";").endswith(" FETCH friend")
            assert fetched.id == "3"
            linked = await OrdT.objects().filter(g=3).select("id", "friend.n").first()
            _assert_ordered(sent[-1], line)
            assert (linked["friend"] if isinstance(linked, dict) else linked.friend) == {"n": 7}


class TestFirstOrderAsOfE2E:
    """``as_of()`` gets the id order too: the clause comes before ``VERSION``."""

    @staticmethod
    async def _check(client: Any, line: int, monkeypatch: pytest.MonkeyPatch) -> None:
        await OrdT.define_indexes()
        await client.query(_creates())
        moment = await _now(client)
        sent = _statements(monkeypatch, client)
        found = await OrdT.objects().filter(r__gt=5).as_of(moment).first()
        statement = sent[-1]
        assert " VERSION $_as_of" in statement
        _assert_ordered(statement.rsplit(" VERSION ", 1)[0], line)
        if line >= 3:
            assert found.id == "1"

    async def test_embedded_versioned(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        async with embedded_database(f"surrealkv+versioned://{tmp_path / 'kv'}") as client:
            await self._check(client, line_major(await client.version()), monkeypatch)

    async def test_versioned_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        port = os.environ.get("SURREALDB_VERSIONED_PORT")
        if not port:
            pytest.skip("no versioned SurrealDB server (SURREALDB_VERSIONED_PORT)")
        host = os.environ.get("SURREALDB_VERSIONED_HOST", os.environ.get("SURREALDB_HOST", "localhost"))
        SurrealDBConnectionManager.set_connection(
            url=f"ws://{host}:{port}/rpc", user="root", password="root", namespace="ns", database="ord_asof"
        )
        try:
            client = await SurrealDBConnectionManager.get_client()
            await client.query(f"REMOVE TABLE IF EXISTS {_TABLE}; REMOVE TABLE IF EXISTS {_FRIEND};")
            try:
                await self._check(client, line_major(await client.version()), monkeypatch)
            finally:
                await client.query(f"REMOVE TABLE IF EXISTS {_TABLE}; REMOVE TABLE IF EXISTS {_FRIEND};")
        finally:
            await SurrealDBConnectionManager.close_connection()


# --- the cache: one key per line ------------------------------------------------------------------------------------


def _fake_version(monkeypatch: pytest.MonkeyPatch, client: Any, answer: str) -> None:
    """*client*'s ``version()`` answers *answer* (patched on its class, keyed on ``id(client)``)."""
    cls = type(client)
    real = cls.version
    owner = id(client)

    async def version(self: Any, *args: Any, **kwargs: Any) -> Any:
        return answer if id(self) == owner else await real(self, *args, **kwargs)

    monkeypatch.setattr(cls, "version", version)


@pytest.fixture
def _fresh_cache() -> Any:
    QueryCache.configure(default_ttl=60, max_size=1000, enabled=True)
    QueryCache.clear()
    yield
    QueryCache.clear()


@pytest.mark.usefixtures("_fresh_cache")
class TestFirstOrderCacheKeys:
    """Two named connections whose clients answer ``version()`` ``surrealdb-2.7.0`` and ``surrealdb-4.0.0`` (two
    ``mem://`` engines), one shared ``QueryCache``: the same ``cache()`` queryset's ``first()`` stores two entries,
    keyed on two statements, and each line's second read is answered by its own entry."""

    async def test_per_line_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        names = {"ord_2x": "surrealdb-2.7.0", "ord_4x": "surrealdb-4.0.0"}
        keyed: list[tuple[str, str]] = []
        real_key = _query_set.statement_key

        def spy(scope: int, kind: str, statement: str, variables: Any) -> str | None:
            key = real_key(scope, kind, statement, variables)
            keyed.append((statement, str(key)))
            return key

        monkeypatch.setattr(_query_set, "statement_key", spy)
        try:
            for name, answer in names.items():
                SurrealDBConnectionManager.add_connection(name, url="mem://", namespace="ns", database=name)
                client = await SurrealDBConnectionManager.get_client(name)
                _fake_version(monkeypatch, client, answer)
                await client.query(_creates())
            qs = OrdT.objects().filter(r__gt=5).cache()
            found: dict[str, list[str]] = {}
            for name in names:
                async with SurrealDBConnectionManager.using(name):
                    found[name] = [(await qs.first()).id, (await qs.first()).id]
            # No index on these engines: a scan in id order on both, with or without the clause.
            assert found == {"ord_2x": ["1", "1"], "ord_4x": ["1", "1"]}
            statements = [statement for statement, _key in keyed]
            assert len(statements) == 4
            assert "ORDER BY" not in statements[0] and statements[0] == statements[1]
            assert statements[2].rstrip(";").endswith("ORDER BY id LIMIT 1") and statements[2] == statements[3]
            keys = [key for _statement, key in keyed]
            assert keys[0] == keys[1] != keys[2] == keys[3]
            stats = QueryCache.stats()
            assert (stats["entries"], stats["hits"], stats["misses"]) == (2, 2, 2)
        finally:
            for name in names:
                await SurrealDBConnectionManager.remove_connection(name)


class TestFirstOrderMixedLinesE2E:
    """The same ``cache()`` queryset through a 2.x and a 3.x server in one process (``SURREALDB_PORT_3X`` and
    ``SURREALDB_PORT_2X``, set by the team's ``test-dbs.sh test``; skipped otherwise): each answers its own line's
    row, never the other's cached one."""

    @pytest.mark.usefixtures("_fresh_cache")
    async def test_each_line_its_own_row(self) -> None:
        port_3x, port_2x = os.environ.get("SURREALDB_PORT_3X"), os.environ.get("SURREALDB_PORT_2X")
        if not (port_3x and port_2x):
            pytest.skip("needs SURREALDB_PORT_3X and SURREALDB_PORT_2X (both server lines)")
        host = os.environ.get("SURREALDB_HOST", "localhost")
        names = {"ord_3x": port_3x, "ord_2x": port_2x}
        try:
            for name, port in names.items():
                SurrealDBConnectionManager.add_connection(
                    name, url=f"ws://{host}:{port}/rpc", user="root", password="root", namespace="ns", database="ord_mixed"
                )
                client = await SurrealDBConnectionManager.get_client(name)
                await client.query(f"REMOVE TABLE IF EXISTS {_TABLE}; REMOVE TABLE IF EXISTS {_FRIEND};")
                async with SurrealDBConnectionManager.using(name):
                    await OrdT.define_indexes()
                await client.query(_creates())
            qs = OrdT.objects().filter(r__gt=5).cache()
            found: dict[str, list[str]] = {}
            for name in ("ord_2x", "ord_3x", "ord_2x", "ord_3x"):
                async with SurrealDBConnectionManager.using(name):
                    found.setdefault(name, []).append((await qs.first()).id)
            # 2.x: server order, unchanged (index order), measured on 2.7.0; 3.x: the smallest id.
            assert found == {"ord_2x": ["195", "195"], "ord_3x": ["1", "1"]}
            assert QueryCache.stats()["hits"] == 2
        finally:
            for name in names:
                with contextlib.suppress(Exception):
                    client = await SurrealDBConnectionManager.get_client(name)
                    await client.query(f"REMOVE TABLE IF EXISTS {_TABLE}; REMOVE TABLE IF EXISTS {_FRIEND};")
                await SurrealDBConnectionManager.remove_connection(name)
