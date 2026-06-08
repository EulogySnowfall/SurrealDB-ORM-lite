import contextlib
import os

import pytest

from surreal_orm_lite import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from surreal_orm_lite.exceptions import SurrealDbError
from surreal_orm_lite.utils import validate_json_pointer, validate_patch_operations

TABLES = ("PatchUser",)


def _connect() -> None:
    SurrealDBConnectionManager.set_connection(
        url=f"ws://{os.environ.get('SURREALDB_HOST', 'localhost')}:{os.environ.get('SURREALDB_PORT', '8000')}/rpc",
        user="root",
        password="root",
        namespace="ns",
        database="db",
    )


async def _setup():
    _connect()
    client = await SurrealDBConnectionManager.get_client()
    for table in TABLES:
        with contextlib.suppress(Exception):
            await client.query(f"DELETE {table};", {})
    return client


async def _native_txn_supported() -> bool:
    client = await SurrealDBConnectionManager.get_client()
    try:
        txn = await client.begin()
    except Exception:
        return False
    with contextlib.suppress(Exception):
        await client.cancel(txn)
    return True


class PatchUser(BaseSurrealModel):
    model_config = SurrealConfigDict(primary_key="id")
    id: str | None = None
    name: str = ""
    age: int = 0
    tags: list[str] = []
    views: int = 0
    score: float = 0.0
    counters: dict[str, int] = {}


class TestPatchValidators:
    def test_valid_pointers(self) -> None:
        validate_json_pointer("/age")
        validate_json_pointer("")  # whole document (RFC 6901)
        validate_json_pointer("/tags/-")
        validate_json_pointer("/settings/notifications")

    def test_pointer_missing_leading_slash_raises(self) -> None:
        with pytest.raises(ValueError, match="JSON Pointer"):
            validate_json_pointer("age")

    def test_pointer_non_string_raises(self) -> None:
        with pytest.raises(ValueError, match="string"):
            validate_json_pointer(123)  # type: ignore[arg-type]

    def test_valid_operations(self) -> None:
        validate_patch_operations(
            [
                {"op": "replace", "path": "/age", "value": 26},
                {"op": "add", "path": "/tags/-", "value": "x"},
                {"op": "remove", "path": "/tmp"},
                {"op": "move", "from": "/a", "path": "/b"},
            ]
        )

    def test_operations_must_be_nonempty_list(self) -> None:
        with pytest.raises(ValueError, match="non-empty list"):
            validate_patch_operations([])
        with pytest.raises(ValueError, match="non-empty list"):
            validate_patch_operations({"op": "add", "path": "/x", "value": 1})  # type: ignore[arg-type]

    def test_operation_must_be_dict(self) -> None:
        with pytest.raises(ValueError, match="must be a dict"):
            validate_patch_operations(["nope"])  # type: ignore[list-item]

    def test_invalid_op_name_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid op"):
            validate_patch_operations([{"op": "frobnicate", "path": "/x", "value": 1}])

    def test_missing_path_raises(self) -> None:
        with pytest.raises(ValueError, match="missing required 'path'"):
            validate_patch_operations([{"op": "add", "value": 1}])

    def test_value_required_for_add(self) -> None:
        with pytest.raises(ValueError, match="missing required 'value'"):
            validate_patch_operations([{"op": "add", "path": "/x"}])

    def test_from_required_for_move(self) -> None:
        with pytest.raises(ValueError, match="missing required 'from'"):
            validate_patch_operations([{"op": "move", "path": "/x"}])

    def test_remove_needs_only_path(self) -> None:
        validate_patch_operations([{"op": "remove", "path": "/x"}])


class TestPatchE2E:
    @pytest.mark.asyncio
    async def test_patch_replace_add_remove_and_syncs_self(self) -> None:
        client = await _setup()
        u = PatchUser(id="p1", name="Al", age=20, tags=["a"])
        await u.save()
        await u.patch(
            [
                {"op": "replace", "path": "/age", "value": 26},
                {"op": "add", "path": "/tags/-", "value": "premium"},
            ]
        )
        assert u.age == 26  # non-tx applies the returned row to self
        assert u.tags == ["a", "premium"]
        rows = await client.query("SELECT * FROM PatchUser:p1;", {})
        assert rows[0]["age"] == 26
        assert rows[0]["tags"] == ["a", "premium"]
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_patch_requires_id(self) -> None:
        _connect()
        with pytest.raises(SurrealDbError, match="explicit id"):
            await PatchUser(name="NoId").patch([{"op": "replace", "path": "/age", "value": 1}])
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_patch_invalid_ops_raise_before_io(self) -> None:
        _connect()
        with pytest.raises(ValueError):
            await PatchUser(id="p2", name="x").patch([])
        await SurrealDBConnectionManager.close_connection()


class TestAtomicArrayE2E:
    @pytest.mark.asyncio
    async def test_atomic_append_allows_duplicates(self) -> None:
        client = await _setup()
        p = PatchUser(id="a1", tags=["x"])
        await p.save()
        await p.atomic_append("tags", "x")  # duplicate allowed
        await p.atomic_append("tags", "y")
        assert p.tags == ["x", "x", "y"]
        rows = await client.query("SELECT tags FROM PatchUser:a1;", {})
        assert rows[0]["tags"] == ["x", "x", "y"]
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_atomic_remove_removes_all_occurrences(self) -> None:
        """Portability: must remove ALL occurrences on BOTH 2.6.x and 3.x."""
        client = await _setup()
        p = PatchUser(id="a2", tags=["x", "y", "x", "x"])
        await p.save()
        await p.atomic_remove("tags", "x")
        assert p.tags == ["y"]  # every x gone, identical on both DB lines
        rows = await client.query("SELECT tags FROM PatchUser:a2;", {})
        assert rows[0]["tags"] == ["y"]
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_atomic_invalid_field_raises(self) -> None:
        _connect()
        with pytest.raises(ValueError):
            await PatchUser(id="a3").atomic_append("tags; DROP", "x")
        await SurrealDBConnectionManager.close_connection()


class TestAtomicSetAddIncrementE2E:
    @pytest.mark.asyncio
    async def test_atomic_set_add_no_duplicate(self) -> None:
        client = await _setup()
        p = PatchUser(id="s1", tags=["x"])
        await p.save()
        await p.atomic_set_add("tags", "x")  # already present → unchanged
        assert p.tags == ["x"]
        await p.atomic_set_add("tags", "z")  # absent → added
        assert p.tags == ["x", "z"]
        rows = await client.query("SELECT tags FROM PatchUser:s1;", {})
        assert rows[0]["tags"] == ["x", "z"]
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_atomic_increment_default_amount_and_negative(self) -> None:
        client = await _setup()
        p = PatchUser(id="i1", views=10)
        await p.save()
        await p.atomic_increment("views")  # +1 default
        assert p.views == 11
        await p.atomic_increment("views", 5)  # +5
        assert p.views == 16
        await p.atomic_increment("views", -6)  # decrement
        assert p.views == 10
        rows = await client.query("SELECT views FROM PatchUser:i1;", {})
        assert rows[0]["views"] == 10
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_atomic_increment_nested_field(self) -> None:
        client = await _setup()
        p = PatchUser(id="n1", counters={"views": 10})
        await p.save()
        await p.atomic_increment("counters.views", 5)  # dotted nested path
        assert p.counters["views"] == 15
        rows = await client.query("SELECT counters FROM PatchUser:n1;", {})
        assert rows[0]["counters"]["views"] == 15
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_atomic_requires_id(self) -> None:
        _connect()
        with pytest.raises(SurrealDbError, match="explicit id"):
            await PatchUser().atomic_increment("views")
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_atomic_on_missing_id_is_noop(self) -> None:
        """An atomic op on an id that does not exist is a silent no-op on BOTH lines.

        ``UPDATE specific:id SET …`` against a non-existent record neither raises nor
        creates the row (unlike ``upsert``); the empty result leaves ``self`` unchanged.
        Identical on 2.6.5 and 3.1.3.
        """
        client = await _setup()
        await PatchUser(id="real", views=1).save()  # materialise the table
        ghost = PatchUser(id="ghost", views=5)
        await ghost.atomic_increment("views", 3)  # no raise
        assert ghost.views == 5  # untouched: nothing came back to apply
        rows = await client.query("SELECT id FROM PatchUser;", {})
        assert {str(r["id"].id) for r in rows} == {"real"}  # ghost was NOT created
        await SurrealDBConnectionManager.close_connection()


class TestQuerySetPatchE2E:
    @pytest.mark.asyncio
    async def test_patch_filtered_set(self) -> None:
        client = await _setup()
        await PatchUser(id="q1", name="A", age=1).save()
        await PatchUser(id="q2", name="A", age=1).save()
        await PatchUser(id="q3", name="B", age=1).save()
        n = await PatchUser.objects().filter(name="A").patch([{"op": "replace", "path": "/age", "value": 99}])
        assert n == 2
        rows = await client.query("SELECT id, age FROM PatchUser ORDER BY id;", {})
        by_age = {str(r["id"].id): r["age"] for r in rows}
        assert by_age == {"q1": 99, "q2": 99, "q3": 1}
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_patch_whole_table(self) -> None:
        client = await _setup()
        await PatchUser(id="w1", age=1).save()
        await PatchUser(id="w2", age=2).save()
        n = await PatchUser.objects().patch([{"op": "replace", "path": "/age", "value": 0}])
        assert n == 2
        rows = await client.query("SELECT age FROM PatchUser;", {})
        assert all(r["age"] == 0 for r in rows)
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_queryset_patch_invalid_ops_raise(self) -> None:
        _connect()
        with pytest.raises(ValueError):
            await PatchUser.objects().patch([])
        await SurrealDBConnectionManager.close_connection()


class TestQuerySetPatchTxE2E:
    @pytest.mark.asyncio
    async def test_queryset_patch_in_tx_commits(self) -> None:
        client = await _setup()
        await PatchUser(id="qt1", name="A", age=1).save()
        await PatchUser(id="qt2", name="A", age=1).save()
        interactive = await _native_txn_supported()
        async with SurrealDBConnectionManager.transaction() as tx:
            n = await PatchUser.objects(tx=tx).filter(name="A").patch([{"op": "replace", "path": "/age", "value": 5}])
            # Real affected count on the interactive (3.x) strategy; the buffered (2.6.x / HTTP)
            # strategy cannot know it before commit and reports 0 — same contract as bulk_update.
            assert n == 2 if interactive else n == 0
        rows = await client.query("SELECT age FROM PatchUser WHERE name = 'A';", {})
        assert all(r["age"] == 5 for r in rows)  # persisted on both lines
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_queryset_patch_in_tx_rolls_back(self) -> None:
        client = await _setup()
        await PatchUser(id="qt3", name="B", age=1).save()
        with pytest.raises(RuntimeError):
            async with SurrealDBConnectionManager.transaction() as tx:
                await PatchUser.objects(tx=tx).filter(name="B").patch([{"op": "replace", "path": "/age", "value": 9}])
                raise RuntimeError("boom")
        rows = await client.query("SELECT age FROM PatchUser WHERE name = 'B';", {})
        assert rows[0]["age"] == 1  # rolled back
        await SurrealDBConnectionManager.close_connection()


class TestPatchTxE2E:
    @pytest.mark.asyncio
    async def test_patch_and_atomic_in_tx_commit(self) -> None:
        client = await _setup()
        p = PatchUser(id="t1", age=1, score=0.0)
        await p.save()
        async with SurrealDBConnectionManager.transaction() as tx:
            await p.patch([{"op": "replace", "path": "/age", "value": 7}], tx=tx)
            await p.atomic_increment("score", 2.0, tx=tx)
        rows = await client.query("SELECT age, score FROM PatchUser:t1;", {})
        assert rows[0]["age"] == 7
        assert rows[0]["score"] == 2.0
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_patch_in_tx_rolls_back(self) -> None:
        client = await _setup()
        p = PatchUser(id="t2", age=1)
        await p.save()
        with pytest.raises(RuntimeError):
            async with SurrealDBConnectionManager.transaction() as tx:
                await p.patch([{"op": "replace", "path": "/age", "value": 99}], tx=tx)
                raise RuntimeError("boom")
        rows = await client.query("SELECT age FROM PatchUser:t2;", {})
        assert rows[0]["age"] == 1  # rolled back
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_patch_interactive_tx_syncs_self(self) -> None:
        client = await _setup()
        if not await _native_txn_supported():
            await SurrealDBConnectionManager.close_connection()
            pytest.skip("requires SurrealDB 3.x (interactive transactions)")
        p = PatchUser(id="t3", age=1)
        await p.save()
        async with SurrealDBConnectionManager.transaction() as tx:
            await p.patch([{"op": "replace", "path": "/age", "value": 42}], tx=tx)
            assert p.age == 42  # interactive tx applies the returned row to self
        rows = await client.query("SELECT age FROM PatchUser:t3;", {})
        assert rows[0]["age"] == 42
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_atomic_increment_interactive_tx_syncs_self(self) -> None:
        client = await _setup()
        if not await _native_txn_supported():
            await SurrealDBConnectionManager.close_connection()
            pytest.skip("requires SurrealDB 3.x (interactive transactions)")
        p = PatchUser(id="t4", views=10)
        await p.save()
        async with SurrealDBConnectionManager.transaction() as tx:
            await p.atomic_increment("views", 5, tx=tx)
            assert p.views == 15  # interactive tx applies the server-computed row to self
        rows = await client.query("SELECT views FROM PatchUser:t4;", {})
        assert rows[0]["views"] == 15
        await SurrealDBConnectionManager.close_connection()
