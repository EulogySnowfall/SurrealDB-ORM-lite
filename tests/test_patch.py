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
