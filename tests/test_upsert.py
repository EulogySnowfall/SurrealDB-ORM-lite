import contextlib
import os

import pytest

from surreal_orm_lite import (
    BaseSurrealModel,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    post_save,
    post_update,
)
from surreal_orm_lite.exceptions import SurrealDbError

TABLES = ("UpUser", "UpAccount", "UpEmailUser")


def _connect() -> None:
    SurrealDBConnectionManager.set_connection(
        url=f"ws://{os.environ.get('SURREALDB_HOST', 'localhost')}:{os.environ.get('SURREALDB_PORT', '8000')}/rpc",
        user="root",
        password="root",
        namespace="ns",
        database="db",
    )


async def _setup():
    """Connect and clear the fixtures' tables; return the client.

    Per-test connect/close (no fixture) — matches the rest of the suite and avoids the
    module-scoped-fixture-loop vs WS-singleton clash.
    """
    _connect()
    client = await SurrealDBConnectionManager.get_client()
    for table in TABLES:
        with contextlib.suppress(Exception):
            await client.query(f"DELETE {table};", {})
    return client


async def _native_txn_supported() -> bool:
    """True if the server exposes the SDK's native transaction RPC (SurrealDB 3.x)."""
    client = await SurrealDBConnectionManager.get_client()
    try:
        txn = await client.begin()
    except Exception:
        return False
    with contextlib.suppress(Exception):
        await client.cancel(txn)
    return True


class UpUser(BaseSurrealModel):
    model_config = SurrealConfigDict(primary_key="id")
    id: str | None = None
    name: str
    age: int = 0


class UpAccount(BaseSurrealModel):
    """Has fields outside the usual criteria/defaults so REPLACE-vs-MERGE is observable."""

    model_config = SurrealConfigDict(primary_key="id")
    id: str | None = None
    name: str
    email: str = ""
    role: str = "user"
    age: int = 0


class UpEmailUser(BaseSurrealModel):
    """Non-``id`` primary key, to check identity stays anchored on the PK."""

    model_config = SurrealConfigDict(primary_key="email")
    email: str
    name: str = ""


class TestUpsertE2E:
    @pytest.mark.asyncio
    async def test_upsert_creates_then_replaces(self) -> None:
        client = await _setup()
        u = UpUser(id="alice", name="Alice", age=30)
        await u.upsert()
        rows = await client.query("SELECT * FROM UpUser;", {})
        assert len(rows) == 1
        assert rows[0]["name"] == "Alice"
        assert rows[0]["age"] == 30
        # replace (age omitted from the new model defaults to 0 → REPLACE drops the old 30)
        u2 = UpUser(id="alice", name="Alice2")
        await u2.upsert()
        rows = await client.query("SELECT * FROM UpUser;", {})
        assert len(rows) == 1  # still one record (no duplicate)
        assert rows[0]["name"] == "Alice2"
        assert rows[0]["age"] == 0  # REPLACE semantics
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_upsert_without_id_raises(self) -> None:
        _connect()
        with pytest.raises(SurrealDbError, match="explicit id"):
            await UpUser(name="NoId").upsert()
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_upsert_in_tx_commits(self) -> None:
        client = await _setup()
        async with SurrealDBConnectionManager.transaction() as tx:
            await UpUser(id="txu", name="Tx").upsert(tx=tx)
        rows = await client.query("SELECT * FROM UpUser WHERE id = UpUser:txu;", {})
        assert len(rows) == 1
        assert rows[0]["name"] == "Tx"
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_upsert_in_tx_rolls_back(self) -> None:
        client = await _setup()
        with pytest.raises(RuntimeError):
            async with SurrealDBConnectionManager.transaction() as tx:
                await UpUser(id="txr", name="TxR").upsert(tx=tx)
                raise RuntimeError("boom")
        rows = await client.query("SELECT * FROM UpUser WHERE id = UpUser:txr;", {})
        assert not rows
        await SurrealDBConnectionManager.close_connection()


class TestUpdateOrCreateE2E:
    @pytest.mark.asyncio
    async def test_creates_when_absent(self) -> None:
        client = await _setup()
        obj, created = await UpUser.objects().update_or_create(name="Alice", defaults={"age": 30})
        assert created is True
        assert obj.name == "Alice"
        assert obj.age == 30
        rows = await client.query("SELECT * FROM UpUser;", {})
        assert len(rows) == 1
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_updates_when_present(self) -> None:
        client = await _setup()
        first, c1 = await UpUser.objects().update_or_create(name="Bob", defaults={"age": 1})
        assert c1 is True
        second, c2 = await UpUser.objects().update_or_create(name="Bob", defaults={"age": 2})
        assert c2 is False
        assert second.age == 2
        rows = await client.query("SELECT * FROM UpUser;", {})
        assert len(rows) == 1  # no duplicate
        assert rows[0]["age"] == 2
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_update_preserves_untouched_fields(self) -> None:
        """The update path must MERGE, not REPLACE: fields outside criteria/defaults survive."""
        client = await _setup()
        await UpAccount(id="acc1", name="Bob", email="b@x.io", role="admin", age=5).save()
        obj, created = await UpAccount.objects().update_or_create(name="Bob", defaults={"age": 9})
        assert created is False
        rows = await client.query("SELECT * FROM UpAccount WHERE id = UpAccount:acc1;", {})
        assert len(rows) == 1
        assert rows[0]["age"] == 9
        assert rows[0]["email"] == "b@x.io"  # NOT dropped
        assert rows[0]["role"] == "admin"  # NOT dropped
        assert obj.email == "b@x.io"
        assert obj.role == "admin"
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_lookup_form_criteria_does_not_corrupt(self) -> None:
        """A ``field__exact`` criterion must write the base field, never a literal ``field__exact`` column."""
        client = await _setup()
        await UpUser(id="carol", name="Carol", age=1).save()
        _obj, created = await UpUser.objects().update_or_create(name__exact="Carol", defaults={"age": 7})
        assert created is False
        rows = await client.query("SELECT * FROM UpUser WHERE id = UpUser:carol;", {})
        assert rows[0]["age"] == 7
        assert rows[0]["name"] == "Carol"
        assert "name__exact" not in rows[0]  # no corrupt column written
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_create_on_non_id_pk_anchors_identity(self) -> None:
        """For a non-``id`` PK model, create must use the PK as record id (no auto-id duplicate)."""
        client = await _setup()
        _obj, created = await UpEmailUser.objects().update_or_create(email="z@x.io", defaults={"name": "Z"})
        assert created is True
        # upsert() the SAME logical entity by PK; must hit the SAME physical record.
        await UpEmailUser(email="z@x.io", name="Z2").upsert()
        rows = await client.query("SELECT * FROM UpEmailUser;", {})
        assert len(rows) == 1  # not two (auto-id record + PK record)
        assert rows[0]["name"] == "Z2"
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_emits_save_and_update_signals(self) -> None:
        await _setup()
        events: list[tuple[str, object]] = []

        async def on_save(sender, instance, created, **kw):
            events.append(("save", created))

        async def on_update(sender, instance, update_fields, **kw):
            events.append(("update", tuple(sorted(update_fields))))

        post_save.connect(UpUser)(on_save)
        post_update.connect(UpUser)(on_update)
        try:
            await UpUser.objects().update_or_create(name="Sig", defaults={"age": 1})  # create
            await UpUser.objects().update_or_create(name="Sig", defaults={"age": 2})  # update
        finally:
            post_save.disconnect(on_save, UpUser)
            post_update.disconnect(on_update, UpUser)
            await SurrealDBConnectionManager.close_connection()
        assert ("save", True) in events
        assert any(e[0] == "update" for e in events)

    @pytest.mark.asyncio
    async def test_raises_on_multiple_matches(self) -> None:
        await _setup()
        await UpUser(id="m1", name="Dup", age=1).save()
        await UpUser(id="m2", name="Dup", age=2).save()
        with pytest.raises(SurrealDbError, match="multiple"):
            await UpUser.objects().update_or_create(name="Dup", defaults={"age": 9})
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_requires_criteria(self) -> None:
        _connect()
        with pytest.raises(SurrealDbError, match="criteria"):
            await UpUser.objects().update_or_create(defaults={"age": 1})
        await SurrealDBConnectionManager.close_connection()


class TestGetOrCreateE2E:
    @pytest.mark.asyncio
    async def test_creates_when_absent(self) -> None:
        await _setup()
        obj, created = await UpUser.objects().get_or_create(name="Zoe", defaults={"age": 5})
        assert created is True
        assert obj.name == "Zoe"
        assert obj.age == 5
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_returns_existing_without_writing(self) -> None:
        client = await _setup()
        first, c1 = await UpUser.objects().get_or_create(name="Yan", defaults={"age": 7})
        assert c1 is True
        # defaults differ, but the existing record must NOT be overwritten
        second, c2 = await UpUser.objects().get_or_create(name="Yan", defaults={"age": 99})
        assert c2 is False
        assert second.age == 7  # unchanged
        rows = await client.query("SELECT * FROM UpUser;", {})
        assert len(rows) == 1
        assert rows[0]["age"] == 7
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_raises_on_multiple_matches(self) -> None:
        await _setup()
        await UpUser(id="g1", name="DupG", age=1).save()
        await UpUser(id="g2", name="DupG", age=2).save()
        with pytest.raises(SurrealDbError, match="multiple"):
            await UpUser.objects().get_or_create(name="DupG")
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_requires_criteria(self) -> None:
        _connect()
        with pytest.raises(SurrealDbError, match="criteria"):
            await UpUser.objects().get_or_create(defaults={"age": 1})
        await SurrealDBConnectionManager.close_connection()


class TestUpsertTxParticipationE2E:
    @pytest.mark.asyncio
    async def test_update_or_create_participates_in_interactive_tx(self) -> None:
        client = await _setup()
        if not await _native_txn_supported():
            await SurrealDBConnectionManager.close_connection()
            pytest.skip("requires SurrealDB 3.x (interactive transactions)")
        # The create must be part of the tx → a rollback removes it.
        with pytest.raises(RuntimeError):
            async with SurrealDBConnectionManager.transaction() as tx:
                _obj, created = await UpUser.objects(tx=tx).update_or_create(name="TxC", defaults={"age": 1})
                assert created is True
                raise RuntimeError("rollback")
        rows = await client.query("SELECT * FROM UpUser WHERE name = 'TxC';", {})
        assert not rows  # rolled back → never persisted
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_get_or_create_lookup_uses_tx(self) -> None:
        client = await _setup()
        if not await _native_txn_supported():
            await SurrealDBConnectionManager.close_connection()
            pytest.skip("requires SurrealDB 3.x (interactive transactions)")
        # A write earlier in the tx must be visible to a later get_or_create lookup in the SAME
        # tx → no duplicate created.
        async with SurrealDBConnectionManager.transaction() as tx:
            _o1, c1 = await UpUser.objects(tx=tx).get_or_create(name="Seen", defaults={"age": 1})
            _o2, c2 = await UpUser.objects(tx=tx).get_or_create(name="Seen", defaults={"age": 2})
        assert c1 is True
        assert c2 is False  # the second call saw the first's uncommitted write
        rows = await client.query("SELECT * FROM UpUser WHERE name = 'Seen';", {})
        assert len(rows) == 1
        await SurrealDBConnectionManager.close_connection()

    @pytest.mark.asyncio
    async def test_update_or_create_raises_in_buffered_tx(self) -> None:
        await _setup()
        if await _native_txn_supported():
            await SurrealDBConnectionManager.close_connection()
            pytest.skip("buffered-only behaviour (SurrealDB 2.6.x / HTTP)")
        # On a buffered tx the lookup read cannot run → a clear error, not a silent autocommit.
        with pytest.raises(SurrealDbError):
            async with SurrealDBConnectionManager.transaction() as tx:
                await UpUser.objects(tx=tx).update_or_create(name="NoBuf", defaults={"age": 1})
        await SurrealDBConnectionManager.close_connection()
