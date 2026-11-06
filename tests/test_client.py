"""Unit tests for the SDK 3.0 call frontier, ``surreal_orm_lite._client`` (v0.50.0).

Every SDK call the ORM makes goes through ``_client``: these tests drive each of its functions with a fake async client
that records the SDK method called, its arguments and how many times the returned builder was awaited. They pin the
call mapping (``merge`` and ``patch`` are ``update(target).merge()``/``.patch()``, ``insert_relation`` is
``insert(Table(t), d, relation=True)``), the ``Table`` wrapping of ``str`` table names, ``txn_id`` passed only when given
(the SDK's HTTP methods take no such keyword), one ``await`` per builder, and the ``normalize()`` table: SurrealDB's
``NULL`` reads as ``None``, a native set as a ``set``, an open range bound as ``None``. No database is needed.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any

import pytest

from surreal_orm_lite import _client, _sdk
from surreal_orm_lite._sdk import BoundIncluded, Null, PreciseDatetime, RecordID, SdkRange, SurrealSet, Table


class _Builder:
    """An awaitable stand-in for an SDK 3.0 builder: records chained calls and awaits, resolves to ``result``."""

    def __init__(self, owner: _FakeClient, method: str, result: Any) -> None:
        self.owner = owner
        self.method = method
        self.result = result
        self.awaited = 0
        self.chain: list[tuple[str, Any]] = []

    def merge(self, data: Any) -> _Builder:
        self.chain.append(("merge", data))
        return self

    def patch(self, ops: Any) -> _Builder:
        self.chain.append(("patch", ops))
        return self

    def __await__(self) -> Generator[Any, None, Any]:
        self.awaited += 1
        if False:  # pragma: no cover - makes this a generator
            yield
        return self.result


class _FakeClient:
    """A WebSocket-like fake: every data method takes ``txn_id`` and returns a ``_Builder``."""

    def __init__(self, result: Any = None) -> None:
        self.result = result
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.builders: list[_Builder] = []

    def _builder(self, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> _Builder:
        self.calls.append((method, args, kwargs))
        builder = _Builder(self, method, self.result)
        self.builders.append(builder)
        return builder

    def query(self, sql: str, variables: Any = None, *, txn_id: Any = None) -> _Builder:
        return self._builder("query", (sql, variables), {"txn_id": txn_id} if txn_id is not None else {})

    async def query_raw(self, sql: str, variables: Any = None, *, txn_id: Any = None) -> Any:
        self.calls.append(("query_raw", (sql, variables), {"txn_id": txn_id} if txn_id is not None else {}))
        return self.result

    def select(self, target: Any, **kwargs: Any) -> _Builder:
        return self._builder("select", (target,), kwargs)

    def create(self, target: Any, data: Any, **kwargs: Any) -> _Builder:
        return self._builder("create", (target, data), kwargs)

    def update(self, target: Any, *args: Any, **kwargs: Any) -> _Builder:
        return self._builder("update", (target, *args), kwargs)

    def upsert(self, target: Any, data: Any, **kwargs: Any) -> _Builder:
        return self._builder("upsert", (target, data), kwargs)

    def insert(self, table: Any, data: Any, **kwargs: Any) -> _Builder:
        return self._builder("insert", (table, data), kwargs)

    def delete(self, target: Any, **kwargs: Any) -> _Builder:
        return self._builder("delete", (target,), kwargs)


class _FakeHttpClient(_FakeClient):
    """An HTTP-like fake: like the SDK's HTTP client, no method accepts a ``txn_id`` keyword."""

    def query(self, sql: str, variables: Any = None) -> _Builder:  # type: ignore[override]
        return self._builder("query", (sql, variables), {})

    def select(self, target: Any) -> _Builder:  # type: ignore[override]
        return self._builder("select", (target,), {})

    def create(self, target: Any, data: Any) -> _Builder:  # type: ignore[override]
        return self._builder("create", (target, data), {})

    def update(self, target: Any, *args: Any) -> _Builder:  # type: ignore[override]
        return self._builder("update", (target, *args), {})

    def upsert(self, target: Any, data: Any) -> _Builder:  # type: ignore[override]
        return self._builder("upsert", (target, data), {})

    def insert(self, table: Any, data: Any, *, relation: bool = False) -> _Builder:  # type: ignore[override]
        kwargs: dict[str, Any] = {"relation": True} if relation else {}
        return self._builder("insert", (table, data), kwargs)

    def delete(self, target: Any) -> _Builder:  # type: ignore[override]
        return self._builder("delete", (target,), {})


TXN = uuid.UUID("00000000-0000-0000-0000-000000000001")
RID = RecordID("person", 1)


def _only_call(client: _FakeClient) -> tuple[str, tuple[Any, ...], dict[str, Any]]:
    assert len(client.calls) == 1, client.calls
    return client.calls[0]


def _awaited_once(client: _FakeClient) -> None:
    assert [b.awaited for b in client.builders] == [1] * len(client.builders)


# --- query, query_all, query_raw ------------------------------------------------------------------


class TestQuery:
    async def test_query_returns_the_first_statement_result(self) -> None:
        client = _FakeClient(result=[[{"id": RID}], 2])
        assert await _client.query(client, "SELECT * FROM person; RETURN 2", {"a": 1}) == [{"id": RID}]
        assert _only_call(client) == ("query", ("SELECT * FROM person; RETURN 2", {"a": 1}), {})
        _awaited_once(client)

    async def test_query_of_an_empty_result_list_is_none(self) -> None:
        assert await _client.query(_FakeClient(result=[]), "") is None

    async def test_query_normalises_the_result(self) -> None:
        assert await _client.query(_FakeClient(result=[{"a": Null}]), "RETURN {a: NULL}") == {"a": None}

    async def test_query_all_returns_every_statement_result_normalised(self) -> None:
        client = _FakeClient(result=[1, Null, [Null]])
        assert await _client.query_all(client, "RETURN 1; RETURN NULL; RETURN [NULL]") == [1, None, [None]]
        _awaited_once(client)

    async def test_query_passes_txn_id_only_when_given(self) -> None:
        client = _FakeClient(result=[1])
        await _client.query(client, "RETURN 1", txn_id=TXN)
        await _client.query_all(client, "RETURN 1", txn_id=TXN)
        await _client.query(client, "RETURN 1")
        assert [c[2] for c in client.calls] == [{"txn_id": TXN}, {"txn_id": TXN}, {}]

    async def test_query_on_an_http_client_sends_no_txn_id(self) -> None:
        client = _FakeHttpClient(result=[1])
        assert await _client.query(client, "RETURN 1") == 1
        assert await _client.query_all(client, "RETURN 1") == [1]

    async def test_query_raw_returns_the_envelope_normalised(self) -> None:
        client = _FakeClient(result={"result": [{"status": "OK", "result": Null}]})
        assert await _client.query_raw(client, "RETURN NULL", None, txn_id=TXN) == {
            "result": [{"status": "OK", "result": None}]
        }
        assert _only_call(client) == ("query_raw", ("RETURN NULL", None), {"txn_id": TXN})


# --- CRUD -----------------------------------------------------------------------------------------


class TestCrud:
    async def test_select_record(self) -> None:
        client = _FakeClient(result={"id": RID, "nick": Null})
        assert await _client.select(client, RID) == {"id": RID, "nick": None}
        assert _only_call(client) == ("select", (RID,), {})
        _awaited_once(client)

    async def test_select_wraps_a_str_table_name_in_table(self) -> None:
        client = _FakeClient(result=[])
        assert await _client.select(client, "person") == []
        method, args, _ = _only_call(client)
        assert method == "select"
        assert isinstance(args[0], Table)
        assert args[0] == Table("person")

    async def test_table_target_is_passed_as_is(self) -> None:
        client = _FakeClient(result=[])
        table = Table("person")
        await _client.delete(client, table)
        assert _only_call(client)[1][0] is table

    @pytest.mark.parametrize("method", ["create", "update", "upsert"])
    async def test_write_with_data(self, method: str) -> None:
        client = _FakeClient(result={"id": RID})
        result = await getattr(_client, method)(client, RID, {"a": 1}, txn_id=TXN)
        assert result == {"id": RID}
        assert _only_call(client) == (method, (RID, {"a": 1}), {"txn_id": TXN})
        assert client.builders[0].chain == []
        _awaited_once(client)

    @pytest.mark.parametrize("method", ["create", "update", "upsert"])
    async def test_write_wraps_a_str_table_name(self, method: str) -> None:
        client = _FakeClient(result=[])
        await getattr(_client, method)(client, "person", {"a": 1})
        assert _only_call(client)[1][0] == Table("person")

    async def test_merge_is_update_then_merge(self) -> None:
        client = _FakeClient(result={"id": RID, "a": 2})
        assert await _client.merge(client, RID, {"a": 2}) == {"id": RID, "a": 2}
        assert _only_call(client) == ("update", (RID,), {})
        assert client.builders[0].chain == [("merge", {"a": 2})]
        _awaited_once(client)

    async def test_patch_is_update_then_patch(self) -> None:
        ops = [{"op": "replace", "path": "/a", "value": 3}]
        client = _FakeClient(result=None)
        assert await _client.patch(client, RID, ops, txn_id=TXN) is None
        assert _only_call(client) == ("update", (RID,), {"txn_id": TXN})
        assert client.builders[0].chain == [("patch", ops)]
        _awaited_once(client)

    async def test_insert_wraps_the_table(self) -> None:
        client = _FakeClient(result=[{"id": RID}])
        assert await _client.insert(client, "person", [{"a": 1}]) == [{"id": RID}]
        method, args, kwargs = _only_call(client)
        assert (method, args, kwargs) == ("insert", (Table("person"), [{"a": 1}]), {})
        assert isinstance(args[0], Table)
        _awaited_once(client)

    async def test_insert_relation_is_insert_with_relation(self) -> None:
        edge = {"in": RID, "out": RecordID("post", 1)}
        client = _FakeClient(result=[edge])
        assert await _client.insert_relation(client, "likes", edge, txn_id=TXN) == [edge]
        assert _only_call(client) == ("insert", (Table("likes"), edge), {"relation": True, "txn_id": TXN})
        _awaited_once(client)

    async def test_delete(self) -> None:
        client = _FakeClient(result={"id": RID})
        assert await _client.delete(client, RID, txn_id=TXN) == {"id": RID}
        assert _only_call(client) == ("delete", (RID,), {"txn_id": TXN})

    @pytest.mark.parametrize(
        ("method", "args"),
        [
            ("select", (RID,)),
            ("create", (RID, {})),
            ("update", (RID, {})),
            ("upsert", (RID, {})),
            ("merge", (RID, {})),
            ("patch", (RID, [])),
            ("insert", ("person", {})),
            ("insert_relation", ("likes", {})),
            ("delete", (RID,)),
        ],
    )
    async def test_http_client_gets_no_txn_id(self, method: str, args: tuple[Any, ...]) -> None:
        client = _FakeHttpClient(result=None)
        await getattr(_client, method)(client, *args)
        assert "txn_id" not in _only_call(client)[2]
        _awaited_once(client)


class TestRetire:
    async def test_retire_is_not_implemented_yet(self) -> None:
        with pytest.raises(NotImplementedError):
            await _client.retire(_FakeClient(), "closed")


# --- normalize() ----------------------------------------------------------------------------------


class TestNormalize:
    def test_null_is_none(self) -> None:
        assert _client.normalize(Null) is None

    def test_null_at_any_depth(self) -> None:
        value = {"a": [1, {"b": Null}], "c": Null}
        assert _client.normalize(value) == {"a": [1, {"b": None}], "c": None}

    def test_native_set_is_a_set(self) -> None:
        result = _client.normalize(SurrealSet([1, 2, 3]))
        assert type(result) is set
        assert result == {1, 2, 3}

    def test_set_items_are_normalised(self) -> None:
        assert _client.normalize(SurrealSet([Null, 1])) == {None, 1}

    def test_set_whose_items_are_equal_in_python_stays_a_list(self) -> None:
        result = _client.normalize(SurrealSet([1, True]))
        assert type(result) is list
        assert result == [1, True]

    def test_set_of_unhashable_items_is_a_list(self) -> None:
        result = _client.normalize(SurrealSet([[1]]))
        assert type(result) is list
        assert result == [[1]]

    def test_nested_set(self) -> None:
        assert _client.normalize({"tags": SurrealSet(["a", "b"])}) == {"tags": {"a", "b"}}

    @pytest.mark.parametrize("side", ["begin", "end"])
    def test_open_range_bound_is_none(self, side: str) -> None:
        bound = BoundIncluded(1)
        value = SdkRange(bound, Null) if side == "end" else SdkRange(Null, bound)
        result = _client.normalize(value)
        assert isinstance(result, SdkRange)
        assert result is not value
        assert getattr(result, side) is None
        assert (result.begin if side == "end" else result.end) is bound
        assert getattr(value, side) is Null  # the SDK's value is left as it was

    def test_closed_range_is_the_same_object(self) -> None:
        value = SdkRange(BoundIncluded(1), BoundIncluded(2))
        assert _client.normalize(value) is value

    @pytest.mark.parametrize(
        "value",
        [
            1,
            "x",
            None,
            RID,
            PreciseDatetime(2026, 11, 6, tzinfo=UTC),
            datetime(2026, 11, 6, tzinfo=UTC),
            _sdk.SdkDuration(0),
            [1, "a", {"b": [RID]}],
            {"a": {"b": [1, 2]}},
        ],
    )
    def test_identity_when_nothing_changes(self, value: Any) -> None:
        assert _client.normalize(value) is value

    def test_precise_datetime_is_kept(self) -> None:
        value = {"at": PreciseDatetime(2026, 11, 6, tzinfo=UTC)}
        assert _client.normalize(value)["at"] is value["at"]

    def test_unchanged_siblings_keep_their_identity(self) -> None:
        kept = {"b": [1]}
        result = _client.normalize({"a": Null, "kept": kept})
        assert result["kept"] is kept

    def test_input_is_never_mutated(self) -> None:
        value = {"a": [Null]}
        _client.normalize(value)
        assert value["a"][0] is Null
