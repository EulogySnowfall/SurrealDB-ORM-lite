# SurrealDB SDK 2.0 Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate `surreal-orm-lite` from the `surrealdb` 1.0.8 SDK to `surrealdb[pydantic] 2.0.x` (SurrealDB 3.x protocol), exposing native `RecordID` and structured exceptions, while keeping every ORM method working.

**Architecture:** Adapt the thin SDK-facing layer (`connection_manager.py`, the CRUD methods of `model_base.py`, and the result-handling code of `query_set.py`) to SDK 2.0 semantics: errors are raised as `surrealdb` exceptions (no more error strings), `id` is kept as a native `RecordID` after loading from the DB, and SDK calls receive `RecordID` objects instead of hand-built `table:id` strings. Defensive result handling is preserved and only simplified where the empirical probe (Task 1) proves the 2.0 shape deterministic.

**Tech Stack:** Python 3.11+, `surrealdb[pydantic]>=2.0.0,<3.0.0`, Pydantic 2.x, pytest + pytest-asyncio, uv, Docker (SurrealDB v2.6.3 and v3.1.3), ruff, mypy.

---

## Reference Facts (verified during design)

SDK 2.0 exception hierarchy (all importable from `surrealdb`):

```
SurrealError(Exception)
├── ServerError
│   ├── ValidationError
│   ├── QueryError
│   ├── NotFoundError
│   ├── AlreadyExistsError
│   └── ... (ThrownError, ConfigurationError, SerializationError, NotAllowedError, InternalError)
├── ConnectionUnavailableError
├── InvalidRecordIdError
└── ... (UnsupportedEngineError, UnexpectedResponseError, ...)
```

Importable symbols from `surrealdb`: `AsyncSurreal`, `Surreal`, `RecordID`, `Table`, `Datetime`, plus all exception classes above.

`RecordID` API: `RecordID(table_name, id)`, attributes `.table_name` and `.id`, `str(rid) == "table:id"`, `RecordID.parse("t:x")`.

CI currently uses server `v2.6.3` (not v2.6.0). The health check greps `http://localhost:8000/health` for `OK`.

---

## File Structure

| File | Responsibility | Change |
| --- | --- | --- |
| `pyproject.toml` | Dependency + version | Modify |
| `uv.lock` | Lockfile | Regenerate |
| `src/surreal_orm_lite/_sdk.py` | **New** — single place importing SDK exceptions/symbols | Create |
| `src/surreal_orm_lite/connection_manager.py` | Connection lifecycle | Modify (minor) |
| `src/surreal_orm_lite/model_base.py` | Model CRUD, RecordID helpers, error handling | Modify (core) |
| `src/surreal_orm_lite/query_set.py` | Result handling, `get()` by id | Modify |
| `.github/workflows/ci.yml` | CI server-version matrix | Modify |
| `tests/test_sdk_probe.py` | **New, temporary** — empirical probe (deleted in Task 1) | Create/Delete |
| `tests/test_unit.py`, `tests/test_e2e.py`, `tests/test_relations.py`, `tests/test_signals.py`, `tests/test_aggregations.py`, `tests/test_v050.py` | RecordID-aware assertions | Modify |
| `CHANGELOG.md` | Release notes | Modify |
| `README.md` | RecordID examples | Modify |
| `docs/ROADMAP.md` | SDK constraint + version table | Modify |

**Helper names locked for this plan (used consistently across tasks):**
- `BaseSurrealModel.get_id() -> RecordID | str | None` — raw id value, no `str()` coercion.
- `BaseSurrealModel.get_raw_id() -> str | None` — the bare identifier portion (`RecordID.id` or the string after `:`).
- `BaseSurrealModel._record_id() -> RecordID | None` — a `RecordID` to pass to SDK methods.
- `BaseSurrealModel._get_thing() -> str` — a `table:id` string for SurrealQL building (relations).

---

## Prerequisites

Start SurrealDB 3.1.3 locally (the default target for development):

```bash
docker run -d --name surreal -p 8000:8000 surrealdb/surrealdb:v3.1.3 start --user root --pass root
for i in {1..15}; do curl -s http://localhost:8000/health | grep -q OK && break; sleep 2; done
```

Test command used throughout (matches CI env vars):

```bash
SURREALDB_HOST=localhost SURREALDB_PORT=8000 uv run pytest tests/ -v
```

---

## Phase 0 — Dependency bump

### Task 1: Bump the SDK dependency and lock

**Files:**
- Modify: `pyproject.toml:27-30`
- Regenerate: `uv.lock`

- [ ] **Step 1: Edit `pyproject.toml` dependencies**

Replace lines 27-30:

```toml
dependencies = [
    "pydantic>=2.12.5",
    "surrealdb[pydantic]>=2.0.0,<3.0.0",
]
```

- [ ] **Step 2: Regenerate the lockfile and install**

Run:

```bash
uv lock && uv sync --group dev
```

Expected: `uv.lock` now pins `surrealdb` 2.0.x.

- [ ] **Step 3: Verify the installed version**

Run:

```bash
uv run python -c "import surrealdb; print(surrealdb.__version__)"
```

Expected: prints `2.0.0` (or `2.0.x`).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore(deps): bump to surrealdb[pydantic]>=2.0.0,<3.0.0"
```

---

### Task 2: Empirical probe of SDK 2.0 behavior

**Goal:** Record the exact runtime behavior of SDK 2.0 against SurrealDB 3.1.3 so later tasks rely on facts, not guesses. This probe is temporary and is deleted at the end of the task; its findings are written into the plan's checkboxes below.

**Files:**
- Create then delete: `tests/test_sdk_probe.py`

- [ ] **Step 1: Write the probe test**

Create `tests/test_sdk_probe.py`:

```python
import os

import pytest

from surrealdb import AsyncSurreal, AlreadyExistsError, RecordID

SURREALDB_HOST = os.environ.get("SURREALDB_HOST", "localhost")
SURREALDB_PORT = os.environ.get("SURREALDB_PORT", "8000")
URL = f"http://{SURREALDB_HOST}:{SURREALDB_PORT}"


@pytest.fixture
async def db():
    client = AsyncSurreal(URL)
    await client.signin({"username": "root", "password": "root"})
    await client.use("probe_ns", "probe_db")
    yield client
    await client.query("REMOVE TABLE probe;")
    await client.close()


async def test_probe_shapes(db) -> None:
    # create with RecordID
    rid = RecordID("probe", "alice")
    await db.create(rid, {"name": "Alice"})

    single = await db.select(rid)
    print("SELECT single ->", type(single).__name__, single)

    table = await db.select("probe")
    print("SELECT table ->", type(table).__name__, table)

    q = await db.query("SELECT * FROM probe;")
    print("QUERY ->", type(q).__name__, q)

    # id shape after round-trip
    print("id type ->", type(single["id"]).__name__, single["id"])

    # duplicate create -> exception?
    raised = None
    try:
        await db.create(rid, {"name": "Dup"})
    except Exception as e:  # noqa: BLE001
        raised = e
    print("DUP create raised ->", type(raised).__name__ if raised else None, raised)
    assert isinstance(raised, AlreadyExistsError)
```

- [ ] **Step 2: Run the probe and capture output**

Run:

```bash
SURREALDB_HOST=localhost SURREALDB_PORT=8000 uv run pytest tests/test_sdk_probe.py -v -s
```

Expected: PASS, and the `print(...)` lines reveal the shapes. Record the answers here:

- `select(record_id)` single returns: **dict** / list-of-dict → `____`
- `select("table")` returns: list → `____`
- `query("SELECT ...")` returns: list-of-rows / `[{"result": [...]}]` → `____`
- `single["id"]` type: **RecordID** → `____`
- duplicate `create` raises `AlreadyExistsError`: **yes/no** → `____`

> If `select(record_id)` returns a dict (not a list), the SDK-1.0.8 "always a list" workarounds in `refresh()` and `QuerySet.get()` are no longer needed but the defensive `isinstance(record, list)` guards remain harmless — keep them per the spec.

- [ ] **Step 3: Delete the probe**

```bash
rm tests/test_sdk_probe.py
```

- [ ] **Step 4: Commit the recorded findings (plan only) — no code commit needed**

The probe is deleted; findings live in this plan. Skip git for this task.

---

## Phase 1 — Centralized SDK exception handling

### Task 3: Create the `_sdk.py` shim

**Files:**
- Create: `src/surreal_orm_lite/_sdk.py`
- Test: `tests/test_sdk_shim.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_sdk_shim.py`:

```python
def test_sdk_shim_exports() -> None:
    from src.surreal_orm_lite import _sdk

    # SDK symbols are importable from the single shim module
    assert _sdk.RecordID is not None
    assert _sdk.AsyncSurreal is not None
    # Base SDK exception and the "already exists" specialization
    assert issubclass(_sdk.AlreadyExistsError, _sdk.SurrealError)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_sdk_shim.py -v`
Expected: FAIL with `ModuleNotFoundError: ... _sdk`.

- [ ] **Step 3: Create the shim**

Create `src/surreal_orm_lite/_sdk.py`:

```python
"""Single import surface for the external `surrealdb` SDK.

Centralizing SDK imports here keeps the rest of the ORM decoupled from the
SDK's module layout and makes a future SDK upgrade a one-file change.
"""

from surrealdb import (  # noqa: F401
    AlreadyExistsError,
    AsyncSurreal,
    NotFoundError,
    RecordID,
    ServerError,
    SurrealError,
)

__all__ = [
    "AsyncSurreal",
    "RecordID",
    "SurrealError",
    "ServerError",
    "AlreadyExistsError",
    "NotFoundError",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_sdk_shim.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/surreal_orm_lite/_sdk.py tests/test_sdk_shim.py
git commit -m "feat: add _sdk shim centralizing surrealdb 2.0 imports"
```

---

### Task 4: Route `connection_manager` and `model_base` imports through the shim

**Files:**
- Modify: `src/surreal_orm_lite/connection_manager.py:5`
- Modify: `src/surreal_orm_lite/model_base.py:6`

- [ ] **Step 1: Update `connection_manager.py` import**

Replace line 5 (`from surrealdb import AsyncSurreal`) with:

```python
from ._sdk import AsyncSurreal
```

- [ ] **Step 2: Update `model_base.py` import**

Replace line 6 (`from surrealdb import RecordID`) with:

```python
from ._sdk import AlreadyExistsError, RecordID
```

- [ ] **Step 3: Run the unit tests (no DB needed)**

Run: `uv run pytest tests/test_unit.py -v`
Expected: PASS (imports still resolve; behavior unchanged so far).

- [ ] **Step 4: Commit**

```bash
git add src/surreal_orm_lite/connection_manager.py src/surreal_orm_lite/model_base.py
git commit -m "refactor: import SDK symbols via _sdk shim"
```

---

### Task 5: Replace error-string handling in `_do_save` with exception handling

**Files:**
- Modify: `src/surreal_orm_lite/model_base.py:126-168` (`_do_save`)
- Test: `tests/test_e2e.py` (new test for duplicate-create raising `SurrealDbError`)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_e2e.py` (inside the E2E section that has a live connection; use a unique id):

```python
async def test_duplicate_save_raises_surreal_error() -> None:
    from src.surreal_orm_lite.exceptions import SurrealDbError

    class DupModel(BaseSurrealModel):
        id: str | RecordID | None = None
        name: str

    await DupModel(id="dup_x", name="first").save()
    with pytest.raises(SurrealDbError):
        await DupModel(id="dup_x", name="second").save()
    await DupModel(id="dup_x", name="first").delete()
```

> `RecordID` is already imported at the top of `tests/test_e2e.py` (line 5). Ensure `BaseSurrealModel` and `pytest` are imported in that file (they are used elsewhere in it).

- [ ] **Step 2: Run test to verify it fails**

Run: `SURREALDB_HOST=localhost SURREALDB_PORT=8000 uv run pytest tests/test_e2e.py::test_duplicate_save_raises_surreal_error -v`
Expected: FAIL — under SDK 2.0 the SDK raises `AlreadyExistsError`, which currently propagates as the SDK type (not `SurrealDbError`), or the old `isinstance(result, str)` branch never triggers.

- [ ] **Step 3: Rewrite `_do_save`**

Replace the body of `_do_save` (lines 126-168) with:

```python
    async def _do_save(self) -> tuple[Self, bool]:
        """
        Internal save logic. Returns (self, created) where created indicates
        whether a new record was created (always True for save).
        """
        client = await SurrealDBConnectionManager.get_client()
        data = self.model_dump(exclude={"id"})
        record_id = self._record_id()
        table = self.get_table_name()

        if record_id is not None:
            # SDK 2.0 raises AlreadyExistsError instead of returning a string.
            try:
                await client.create(record_id, data)
            except AlreadyExistsError as e:
                raise SurrealDbError(f"There was a problem with the database: {e}") from e
            return self, True

        # Auto-generate the ID
        record = await client.create(table, data)  # pragma: no cover

        if isinstance(record, list):
            raise SurrealDbError("Can't save data, multiple records returned.")  # pragma: no cover

        if record is None:
            raise SurrealDbError("Can't save data, no record returned.")  # pragma: no cover

        # Update current instance with the auto-generated ID (kept as native RecordID)
        if isinstance(record, dict):
            for key, value in record.items():
                if hasattr(self, key):
                    object.__setattr__(self, key, value)
            return self, True

        raise SurrealDbError("Can't save data, no record returned.")  # pragma: no cover
```

> `self._record_id()` is added in Task 6. If executing strictly in order, do Task 6 before running Step 4; the test for this task is re-run at the end of Task 6.

- [ ] **Step 4: Defer verification to Task 6**

The duplicate-save test depends on `_record_id()` (Task 6). Proceed to Task 6, then run:

`SURREALDB_HOST=localhost SURREALDB_PORT=8000 uv run pytest tests/test_e2e.py::test_duplicate_save_raises_surreal_error -v`
Expected: PASS.

- [ ] **Step 5: Commit (after Task 6 passes)**

```bash
git add src/surreal_orm_lite/model_base.py tests/test_e2e.py
git commit -m "feat: raise SurrealDbError on duplicate save via AlreadyExistsError"
```

---

## Phase 2 — Native RecordID

### Task 6: Add RecordID helper methods

**Files:**
- Modify: `src/surreal_orm_lite/model_base.py` (`get_id`, add `get_raw_id`, `_record_id`)
- Test: `tests/test_unit.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_unit.py`:

```python
from src.surreal_orm_lite._sdk import RecordID


def test_record_id_helper_from_string_id() -> None:
    m = ModelTest(id="1", name="Test", age=45)
    rid = m._record_id()
    assert isinstance(rid, RecordID)
    assert rid.table_name == "ModelTest"
    assert str(rid.id) == "1"


def test_record_id_helper_preserves_recordid() -> None:
    class RidModel(BaseSurrealModel):
        id: str | RecordID | None = None
        name: str

    existing = RecordID("RidModel", "abc")
    m = RidModel(id=existing, name="x")
    assert m._record_id() is existing


def test_get_raw_id_from_recordid() -> None:
    class RidModel2(BaseSurrealModel):
        id: str | RecordID | None = None
        name: str

    m = RidModel2(id=RecordID("RidModel2", "abc"), name="x")
    assert m.get_raw_id() == "abc"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_unit.py::test_record_id_helper_from_string_id tests/test_unit.py::test_get_raw_id_from_recordid -v`
Expected: FAIL with `AttributeError: ... _record_id` / `get_raw_id`.

- [ ] **Step 3: Implement the helpers**

In `model_base.py`, replace `get_id` (lines 62-76) with the version below and add the two new methods right after it:

```python
    def get_id(self) -> None | str | RecordID:
        """
        Get the raw ID of the model instance.

        Returns the value as stored: a native ``RecordID`` for records loaded
        from the database, or the raw value for in-memory instances.
        """
        if hasattr(self, "id"):
            id_value = self.id  # type: ignore[attr-defined]
            return id_value if id_value is not None else None

        if hasattr(self, "model_config"):
            primary_key = self.model_config.get("primary_key", None)
            if isinstance(primary_key, str) and hasattr(self, primary_key):
                primary_key_value = getattr(self, primary_key)
                return primary_key_value if primary_key_value is not None else None

        return None  # pragma: no cover

    def get_raw_id(self) -> str | None:
        """Return the bare identifier portion (without the table prefix)."""
        id_value = self.get_id()
        if id_value is None:
            return None
        if isinstance(id_value, RecordID):
            return str(id_value.id)
        return str(id_value)

    def _record_id(self) -> RecordID | None:
        """Return a ``RecordID`` suitable for passing to SDK 2.0 methods."""
        id_value = self.get_id()
        if id_value is None:
            return None
        if isinstance(id_value, RecordID):
            return id_value
        return RecordID(self.get_table_name(), id_value)
```

- [ ] **Step 4: Run the new unit tests**

Run: `uv run pytest tests/test_unit.py::test_record_id_helper_from_string_id tests/test_unit.py::test_record_id_helper_preserves_recordid tests/test_unit.py::test_get_raw_id_from_recordid -v`
Expected: PASS.

- [ ] **Step 5: Verify existing get_id test still passes**

Run: `uv run pytest tests/test_unit.py::test_model_get_id -v`
Expected: PASS (`get_id() == "1"` for the in-memory string id).

- [ ] **Step 6: Run the deferred duplicate-save test from Task 5**

Run: `SURREALDB_HOST=localhost SURREALDB_PORT=8000 uv run pytest tests/test_e2e.py::test_duplicate_save_raises_surreal_error -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/surreal_orm_lite/model_base.py tests/test_unit.py tests/test_e2e.py
git commit -m "feat: add get_raw_id() and _record_id() RecordID helpers"
```

---

### Task 7: Stop stripping RecordID in `set_data` and `refresh`

**Files:**
- Modify: `src/surreal_orm_lite/model_base.py:88-97` (`set_data`)
- Modify: `src/surreal_orm_lite/model_base.py:99-124` (`refresh`)
- Test: `tests/test_e2e.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_e2e.py`:

```python
async def test_loaded_record_keeps_native_recordid() -> None:
    class RidUser(BaseSurrealModel):
        id: str | RecordID | None = None
        name: str

    await RidUser(id="rid_keep", name="Keep").save()
    loaded = await RidUser.objects().get("rid_keep")
    assert isinstance(loaded.id, RecordID)
    assert loaded.id.table_name == "RidUser"
    assert str(loaded.id.id) == "rid_keep"
    await loaded.delete()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `SURREALDB_HOST=localhost SURREALDB_PORT=8000 uv run pytest tests/test_e2e.py::test_loaded_record_keeps_native_recordid -v`
Expected: FAIL — `set_data` currently converts `id` to a plain string, so `isinstance(loaded.id, RecordID)` is False.

- [ ] **Step 3: Update `set_data` to keep the RecordID**

Replace `set_data` (lines 88-97) with:

```python
    @model_validator(mode="before")
    @classmethod
    def set_data(cls, data: Any) -> Any:
        """
        Pre-process data before model validation.

        SDK 2.0 returns a native ``RecordID`` for the ``id`` field; it is kept
        as-is so callers get the native type (validated by the pydantic extra).
        """
        return data
```

- [ ] **Step 4: Update `refresh` to keep the RecordID**

In `refresh` (lines 119-124), replace the id-stripping loop:

```python
        # Update current instance with refreshed data
        if isinstance(record, dict):
            for key, value in record.items():
                if hasattr(self, key):
                    object.__setattr__(self, key, value)
```

Also update the select call on line 107 to use the RecordID:

```python
        record = await client.select(self._record_id())
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `SURREALDB_HOST=localhost SURREALDB_PORT=8000 uv run pytest tests/test_e2e.py::test_loaded_record_keeps_native_recordid -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/surreal_orm_lite/model_base.py tests/test_e2e.py
git commit -m "feat: keep native RecordID on id when loading from DB"
```

---

### Task 8: Route `update`, `merge`, `delete`, `_get_thing` through RecordID

**Files:**
- Modify: `src/surreal_orm_lite/model_base.py` (`update` ~192-221, `merge` ~223-257, `delete` ~259-293, `_get_thing` ~310-317)
- Test: `tests/test_e2e.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_e2e.py`:

```python
async def test_update_merge_delete_with_recordid_roundtrip() -> None:
    class RidCrud(BaseSurrealModel):
        id: str | RecordID | None = None
        name: str
        age: int = 0

    obj = RidCrud(id="rid_crud", name="A", age=1)
    await obj.save()

    loaded = await RidCrud.objects().get("rid_crud")  # id is a RecordID here
    loaded.age = 2
    await loaded.update()
    await loaded.merge(name="B")

    again = await RidCrud.objects().get("rid_crud")
    assert again.age == 2
    assert again.name == "B"

    await again.delete()
    with pytest.raises(Exception):
        await RidCrud.objects().get("rid_crud")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `SURREALDB_HOST=localhost SURREALDB_PORT=8000 uv run pytest tests/test_e2e.py::test_update_merge_delete_with_recordid_roundtrip -v`
Expected: FAIL — the current methods build `f"{table}:{id}"` from a `RecordID`, producing the doubled `RidCrud:RidCrud:rid_crud`.

- [ ] **Step 3: Update `update` to use `_record_id()`**

In `update`, replace the id/thing logic:

```python
        data = self.model_dump(exclude={"id"})
        record_id = self._record_id()
        if record_id is not None:
            update_fields = list(data.keys())
            has_signals = (
                pre_update.has_handlers(sender) or post_update.has_handlers(sender) or around_update.has_handlers(sender)
            )

            if not has_signals:
                return await client.update(record_id, data)

            await pre_update.send(sender, instance=self, update_fields=update_fields)

            async with around_update.wrap(sender, instance=self, update_fields=update_fields):
                result = await client.update(record_id, data)

            await post_update.send(sender, instance=self, update_fields=update_fields)

            return result
        raise SurrealDbError("Can't update data, no id found.")
```

- [ ] **Step 4: Update `merge` to use `_record_id()`**

In `merge`, replace the id/thing logic:

```python
        record_id = self._record_id()
        if record_id is not None:
            update_fields = list(data_set.keys())
            has_signals = (
                pre_update.has_handlers(sender) or post_update.has_handlers(sender) or around_update.has_handlers(sender)
            )

            if not has_signals:
                await client.merge(record_id, data_set)
                await self.refresh()
                return

            await pre_update.send(sender, instance=self, update_fields=update_fields)

            async with around_update.wrap(sender, instance=self, update_fields=update_fields):
                await client.merge(record_id, data_set)
                await self.refresh()

            await post_update.send(sender, instance=self, update_fields=update_fields)

            return

        raise SurrealDbError(f"No Id for the data to merge: {data}")
```

- [ ] **Step 5: Update `delete` to use `_record_id()`**

In `delete`, replace the id/thing logic (the lines computing `id`, the `raise` guard, and `thing`):

```python
        record_id = self._record_id()
        if record_id is None:
            raise SurrealDbError("Can't delete data, no id found.")

        has_signals = pre_delete.has_handlers(sender) or post_delete.has_handlers(sender) or around_delete.has_handlers(sender)

        if not has_signals:
            deleted = await client.delete(record_id)
            if not deleted:
                raise SurrealDbError(f"Can't delete Record id -> '{record_id}' not found!")
            logger.info(f"Record deleted -> {deleted}.")
            return

        await pre_delete.send(sender, instance=self)

        async with around_delete.wrap(sender, instance=self):
            deleted = await client.delete(record_id)

            if not deleted:
                raise SurrealDbError(f"Can't delete Record id -> '{record_id}' not found!")

        await post_delete.send(sender, instance=self)

        logger.info(f"Record deleted -> {deleted}.")
```

- [ ] **Step 6: Make `_get_thing` RecordID-safe**

Replace `_get_thing` (lines 310-317):

```python
    def _get_thing(self) -> str:
        """Return ``table:id`` string for this instance."""
        id_val = self.get_id()
        if id_val is None:
            raise SurrealDbError("Cannot use relations on an unsaved model (no id).")
        if isinstance(id_val, RecordID):
            thing = str(id_val)
        else:
            thing = f"{self.get_table_name()}:{id_val}"
        validate_thing(thing)
        return thing
```

- [ ] **Step 7: Run the roundtrip test**

Run: `SURREALDB_HOST=localhost SURREALDB_PORT=8000 uv run pytest tests/test_e2e.py::test_update_merge_delete_with_recordid_roundtrip -v`
Expected: PASS.

- [ ] **Step 8: Run the relations tests (depend on `_get_thing`)**

Run: `SURREALDB_HOST=localhost SURREALDB_PORT=8000 uv run pytest tests/test_relations.py -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/surreal_orm_lite/model_base.py tests/test_e2e.py
git commit -m "feat: pass native RecordID to update/merge/delete and guard _get_thing"
```

---

### Task 9: Use RecordID in `QuerySet.get()` by id

**Files:**
- Modify: `src/surreal_orm_lite/query_set.py:391-416` (`get`)
- Test: `tests/test_e2e.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_e2e.py`:

```python
async def test_queryset_get_by_id_returns_native_recordid() -> None:
    class GetRid(BaseSurrealModel):
        id: str | RecordID | None = None
        name: str

    await GetRid(id="g1", name="G").save()
    obj = await GetRid.objects().get("g1")
    assert isinstance(obj.id, RecordID)
    await obj.delete()
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `SURREALDB_HOST=localhost SURREALDB_PORT=8000 uv run pytest tests/test_e2e.py::test_queryset_get_by_id_returns_native_recordid -v`
Expected: After Task 7 this may already PASS (id kept native). If it PASSES, still apply Step 3 for clarity/robustness, then re-run. If it FAILS, Step 3 fixes it.

- [ ] **Step 3: Update `get()` to build a RecordID**

In `query_set.py`, add `from ._sdk import RecordID` to the imports, then replace the `if id_item:` branch (lines 401-408):

```python
        if id_item:
            client = await SurrealDBConnectionManager.get_client()
            record_id = id_item if isinstance(id_item, RecordID) else RecordID(self._model_table, id_item)
            data = await client.select(record_id)
            if isinstance(data, list):
                if len(data) == 0:
                    raise SurrealDbNotFoundError("No result found.")
                data = data[0]
            if data is None:
                raise SurrealDbNotFoundError("No result found.")
            return self.model.from_db(data)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `SURREALDB_HOST=localhost SURREALDB_PORT=8000 uv run pytest tests/test_e2e.py::test_queryset_get_by_id_returns_native_recordid -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/surreal_orm_lite/query_set.py tests/test_e2e.py
git commit -m "feat: QuerySet.get() selects by native RecordID"
```

---

## Phase 3 — Result-shape cleanup

### Task 10: Update SDK-1.0.8 comments and confirm defensive handling

**Files:**
- Modify: `src/surreal_orm_lite/model_base.py` (comments referencing "SDK 1.0.8")
- Modify: `src/surreal_orm_lite/query_set.py` (comments + `exec`/`query` result handling)

- [ ] **Step 1: Replace stale comments in `model_base.py`**

Search for `SDK 1.0.8` in `model_base.py`. Replace each comment to reflect 2.0 (defensive handling retained). Example, the `refresh` comment becomes:

```python
        # SDK 2.0 returns a dict for single-record select; list guard kept defensively.
```

And the `raw_query` comment becomes:

```python
        # SDK 2.0 returns the rows list directly from query()
```

- [ ] **Step 2: Replace stale comments in `query_set.py`**

Search `query_set.py` for any `SDK 1.0.8` / `result` wrapping comments and update them to describe SDK 2.0. Do **not** remove the `data.get("result", [])` fallback branches in `exec()` (lines ~371-372) and `query()` (lines ~713-714) — keep them defensively per the spec.

- [ ] **Step 3: Run the full suite against 3.1.3**

Run: `SURREALDB_HOST=localhost SURREALDB_PORT=8000 uv run pytest tests/ -v`
Expected: all collection/aggregation/query tests PASS (the comment edits are no-ops behaviorally). Any failure here is a real shape difference — fix by adjusting the specific branch the probe (Task 2) flagged, not by broad rewrites.

- [ ] **Step 4: Commit**

```bash
git add src/surreal_orm_lite/model_base.py src/surreal_orm_lite/query_set.py
git commit -m "docs: update SDK comments to 2.0 semantics"
```

---

## Phase 4 — Update existing tests for RecordID

### Task 11: Make DB-loaded id assertions RecordID-aware

**Files:**
- Modify: `tests/test_e2e.py`, `tests/test_signals.py`, `tests/test_relations.py`, `tests/test_aggregations.py`, `tests/test_v050.py`

- [ ] **Step 1: Find string-id assertions on loaded records**

Run:

```bash
grep -rn "\.id ==\|id=='\|\.get_id() ==\|str(.*\.id)" tests/
```

For each assertion that runs on a record **loaded from the DB** (not constructed in memory), update it to compare against the native `RecordID` or its raw id. Two acceptable patterns:

```python
# Pattern A: compare the bare identifier
assert loaded.get_raw_id() == "alice"

# Pattern B: compare the full RecordID
from src.surreal_orm_lite._sdk import RecordID
assert loaded.id == RecordID("User", "alice")
```

> In-memory assertions like `ModelTest(id="1")` then `model.id == "1"` (in `tests/test_unit.py`) stay unchanged — those instances never round-trip through the DB.

- [ ] **Step 2: Run the full suite against 3.1.3**

Run: `SURREALDB_HOST=localhost SURREALDB_PORT=8000 uv run pytest tests/ -v`
Expected: PASS.

- [ ] **Step 3: Verify coverage threshold**

Run: `SURREALDB_HOST=localhost SURREALDB_PORT=8000 uv run pytest tests/ --cov=src.surreal_orm_lite --cov-fail-under=70`
Expected: PASS (coverage ≥ 70%).

- [ ] **Step 4: Commit**

```bash
git add tests/
git commit -m "test: make DB-loaded id assertions RecordID-aware"
```

---

### Task 12: Lint, format, and type-check

**Files:** none (verification task)

- [ ] **Step 1: Run ruff format**

Run: `uv run ruff format src/`
Expected: files reformatted if needed.

- [ ] **Step 2: Run ruff check**

Run: `uv run ruff check src/`
Expected: no errors.

- [ ] **Step 3: Run mypy**

Run: `uv run mypy src/`
Expected: no errors. (The `surrealdb.*` mypy override in `pyproject.toml:64-67` keeps SDK imports unchecked; `_sdk.py` is covered by it via re-export.)

- [ ] **Step 4: Commit any formatting changes**

```bash
git add src/
git commit -m "style: ruff format after SDK 2.0 migration"
```

---

## Phase 5 — CI, versioning, docs

### Task 13: Add the server-version matrix (v2.6.3 + v3.1.3) to CI

**Files:**
- Modify: `.github/workflows/ci.yml:44`

- [ ] **Step 1: Edit the matrix**

Replace line 44:

```yaml
        surrealdb-version: ['v2.6.3', 'v3.1.3']
```

(The job already templatizes the Docker image with `${{ matrix.surrealdb-version }}` on line 52, and the Codecov upload is already gated on `python-version == '3.14'` only — no change needed there. The matrix now runs integration tests against both 2.6.x and 3.1.x for every Python version.)

- [ ] **Step 2: Validate the YAML locally**

Run:

```bash
uv run python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml')); print('ok')"
```

Expected: prints `ok`.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: run integration tests against SurrealDB v2.6.3 and v3.1.3"
```

---

### Task 14: Run the full suite against SurrealDB v2.6.3 (back-compat gate)

**Files:** none (verification task)

- [ ] **Step 1: Swap the local DB to v2.6.3**

Run:

```bash
docker rm -f surreal
docker run -d --name surreal -p 8000:8000 surrealdb/surrealdb:v2.6.3 start --user root --pass root
for i in {1..15}; do curl -s http://localhost:8000/health | grep -q OK && break; sleep 2; done
```

- [ ] **Step 2: Run the full suite against v2.6.3**

Run: `SURREALDB_HOST=localhost SURREALDB_PORT=8000 uv run pytest tests/ -v`
Expected: PASS. If a test fails only on 2.6.3, it indicates a server-version-specific behavior — fix the affected branch so both versions pass, then re-run on both.

- [ ] **Step 3: Restore the 3.1.3 dev DB**

Run:

```bash
docker rm -f surreal
docker run -d --name surreal -p 8000:8000 surrealdb/surrealdb:v3.1.3 start --user root --pass root
for i in {1..15}; do curl -s http://localhost:8000/health | grep -q OK && break; sleep 2; done
```

- [ ] **Step 4: No commit (verification only)**

---

### Task 15: Bump version to 0.7.0

**Files:**
- Modify: `pyproject.toml:3`
- Modify: `src/surreal_orm_lite/__init__.py:1`

> Version number is provisional — see the "Versioning decision" note at the end of this plan. If the user chooses a different number during ROADMAP review, use that number in this task.

- [ ] **Step 1: Update `pyproject.toml` version**

Replace line 3: `version = "0.7.0"`.

- [ ] **Step 2: Update `__init__.py` version**

Replace line 1: `__version__ = "0.7.0"`.

- [ ] **Step 3: Verify they match**

Run:

```bash
grep -E '^version' pyproject.toml && grep __version__ src/surreal_orm_lite/__init__.py
```

Expected: both show `0.7.0`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml src/surreal_orm_lite/__init__.py
git commit -m "chore(release): bump version to 0.7.0"
```

---

### Task 16: Update CHANGELOG

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Read the existing CHANGELOG format**

Run: `head -30 CHANGELOG.md`
Expected: see the heading style used (e.g. `## [0.6.4]`).

- [ ] **Step 2: Add the 0.7.0 entry**

Insert at the top of the entries, matching the existing format:

```markdown
## [0.7.0]

### Changed
- **BREAKING**: migrated to the official `surrealdb[pydantic]>=2.0.0,<3.0.0` SDK (SurrealDB 3.x protocol).
- **BREAKING**: the model `id` field is now exposed as a native `surrealdb.RecordID`
  for records loaded from the database (previously a bare string). Use `get_raw_id()`
  to obtain the bare identifier, or compare against `RecordID(table, id)`.
- Database errors now surface as `SurrealDbError` (the SDK raises structured
  exceptions such as `AlreadyExistsError` instead of returning error strings).

### Added
- CI integration-test matrix against SurrealDB **v2.6.3** and **v3.1.3**.
- `BaseSurrealModel.get_raw_id()` helper.
```

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog for 0.7.0 SDK 2.0 migration"
```

---

### Task 17: Update README RecordID examples

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Find id-related examples**

Run: `grep -n "\.id\b\|get_id\|surrealdb>=1\|surrealdb >= 1\|1.0.8" README.md`

- [ ] **Step 2: Update each match**

- Update any dependency mention from `surrealdb>=1.0.8` to `surrealdb[pydantic]>=2.0.0,<3.0.0`.
- Where examples read `model.id` on a loaded record and assume a string, add a note that `id` is a native `RecordID` and show `model.get_raw_id()` for the bare value.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: README examples for native RecordID and SDK 2.0"
```

---

### Task 18: Update ROADMAP

**Files:**
- Modify: `docs/ROADMAP.md:5` (SDK constraint)
- Modify: `docs/ROADMAP.md:11-21` (version table) and `docs/ROADMAP.md:397` (0.7.0 theme), per the versioning decision

- [ ] **Step 1: Update the SDK constraint line**

Replace line 5:

```markdown
> **Main constraint**: All features must be compatible with the official SDK `surrealdb[pydantic]>=2.0.0,<3.0.0` (SurrealDB 3.x protocol)
```

- [ ] **Step 2: Resolve the 0.7.0 collision in the version table**

The migration occupies a version that the roadmap had assigned to "Transactions ORM". Apply the decision recorded at the end of this plan. The default (migration = 0.7.0, shift the rest by one minor) makes the table:

```markdown
| v0.7.0  | SDK 2.0 / SurrealDB 3.x migration  | ✅ Done     |
| v0.8.0  | Transactions ORM                   | 📋 Planned  |
| v0.9.0  | SurrealFunc & Computed Fields      | 📋 Planned  |
| v0.10.0 | FETCH, Field Aliases & DX          | 📋 Planned  |
| v0.11.0 | Beta Phase                         | 📋 Planned  |
```

Also update the "Target version" column entries in the comparison table (lines 46-52) and the section headers at lines 397/439/493 to match the chosen numbering.

- [ ] **Step 3: Commit**

```bash
git add docs/ROADMAP.md
git commit -m "docs: roadmap SDK 2.0 constraint and version renumbering"
```

---

### Task 19: Final full verification

**Files:** none (verification task)

- [ ] **Step 1: Lint + format check + mypy**

Run:

```bash
uv run ruff check src/ && uv run ruff format --check src/ && uv run mypy src/
```

Expected: all pass.

- [ ] **Step 2: Full suite on 3.1.3 with coverage gate**

Run: `SURREALDB_HOST=localhost SURREALDB_PORT=8000 uv run pytest tests/ -v --cov=src.surreal_orm_lite --cov-fail-under=70`
Expected: PASS, coverage ≥ 70%.

- [ ] **Step 3: Full suite on 2.6.3 (repeat Task 14 swap), then restore 3.1.3**

Expected: PASS on both server versions.

- [ ] **Step 4: Confirm clean tree**

Run: `git status`
Expected: clean (all work committed).

---

## Self-Review

**Spec coverage:**
- §1 Dependency → Task 1. ✅
- §2 Empirical probe → Task 2. ✅
- §3 Structured errors → Tasks 3-5. ✅
- §4 RecordID native (`set_data`, `get_id`, `_get_thing`, save/update/merge/delete/refresh, serialization) → Tasks 5-9. ✅
- §5 Return-shape handling → Task 10. ✅
- §6 Tests & CI matrix (2.6.x + 3.1.x) → Tasks 11, 13, 14. ✅
- §7 Versioning + CHANGELOG + README → Tasks 15-17. ✅
- ROADMAP review (user request) → Task 18. ✅
- Success criteria 1-6 → covered by Tasks 1, 14/19, 12/19, 11/19, 6-9, 15-16. ✅

**Placeholder scan:** No `TBD`/`TODO`. The only intentionally deferred value is the version number (Task 15 note) and the empirical fill-ins in Task 2, both with explicit commands to resolve them.

**Type consistency:** Helper names (`get_id`, `get_raw_id`, `_record_id`, `_get_thing`) are used identically across Tasks 5-11. `_record_id()` is defined in Task 6 and consumed in Tasks 5, 7, 8. `_sdk` exports (`RecordID`, `AsyncSurreal`, `AlreadyExistsError`) are defined in Task 3 and consumed in Tasks 4-9.

---

## Versioning decision (open — for ROADMAP review)

The roadmap currently assigns **v0.7.0 = Transactions ORM**. This migration is a breaking change needing its own bump. Default chosen by this plan: **migration = v0.7.0**, shifting Transactions → v0.8.0 and the rest down by one minor. Confirm or override before Task 15.
