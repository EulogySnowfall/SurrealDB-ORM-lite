"""Tests for v0.18.0 — field aliases, ``server_fields`` and ``merge(refresh=False)``.

All three features are pure client-side behaviour (Pydantic aliasing, payload exclusion, a
skipped round-trip), so nothing here is version-gated: every test must pass identically on
SurrealDB 2.6.x and 3.x. A ``pytest.skip`` appearing in this file would be a design smell.
"""

import contextlib
import os
from typing import Any

import pytest
from pydantic import Field

from surreal_orm_lite import BaseSurrealModel, SurrealDBConnectionManager
from surreal_orm_lite.functions import Computed, computed
from surreal_orm_lite.model_base import SurrealConfigDict

# ==================== Models ====================


class Plain(BaseSurrealModel):
    id: str
    name: str = ""


class Aliased(BaseSurrealModel):
    id: str
    password: str = Field(default="", alias="password_hash")
    display: str = Field(default="", alias="display_name")
    plain: int = 0


class AliasedChild(Aliased):
    extra: str = Field(default="", alias="extra_col")


# ==================== Task 1 — alias mapping primitives ====================


class TestFieldAliases:
    def test_maps_python_name_to_column(self) -> None:
        assert Aliased.get_field_aliases() == {"password": "password_hash", "display": "display_name"}

    def test_unaliased_model_maps_nothing(self) -> None:
        assert Plain.get_field_aliases() == {}

    def test_subclass_inherits_and_extends(self) -> None:
        assert AliasedChild.get_field_aliases() == {
            "password": "password_hash",
            "display": "display_name",
            "extra": "extra_col",
        }

    def test_returns_a_copy(self) -> None:
        """The mapping is cached, so a caller mutating it must not poison the class."""
        Aliased.get_field_aliases()["password"] = "hacked"
        assert Aliased.get_field_aliases()["password"] == "password_hash"

    def test_to_db_field_translates(self) -> None:
        assert Aliased.to_db_field("password") == "password_hash"

    def test_to_db_field_passes_through_unaliased(self) -> None:
        assert Aliased.to_db_field("plain") == "plain"
        assert Aliased.to_db_field("unknown") == "unknown"

    def test_to_py_field_translates(self) -> None:
        assert Aliased.to_py_field("password_hash") == "password"

    def test_to_py_field_passes_through_unmapped(self) -> None:
        assert Aliased.to_py_field("plain") == "plain"
        assert Aliased.to_py_field("id") == "id"

    def test_round_trips(self) -> None:
        for name in Aliased.model_fields:
            assert Aliased.to_py_field(Aliased.to_db_field(name)) == name

    def test_translates_only_the_first_dotted_segment(self) -> None:
        """A nested path is owned by its root field, the rule the computed guards already use."""
        assert Aliased.to_db_field("password.inner") == "password_hash.inner"
        assert Aliased.to_py_field("password_hash.inner") == "password.inner"

    def test_empty_name_is_left_alone(self) -> None:
        assert Aliased.to_db_field("") == ""
        assert Aliased.to_py_field("") == ""


class TestPopulateByName:
    def test_accepts_the_python_name(self) -> None:
        assert Aliased(id="a", password="secret").password == "secret"

    def test_accepts_the_alias(self) -> None:
        assert Aliased(id="a", password_hash="secret").password == "secret"

    def test_plain_models_are_unaffected(self) -> None:
        assert Plain(id="a", name="x").name == "x"


# ==================== Task 2 — server_fields ====================


class Stamped(BaseSurrealModel):
    model_config = SurrealConfigDict(server_fields=["created_at"])
    id: str
    title: str = ""
    created_at: str | None = None


class StampedComputed(BaseSurrealModel):
    model_config = SurrealConfigDict(server_fields=["created_at"])
    id: str
    first: str = ""
    created_at: str | None = None
    shouted: Computed[str] = computed("string::uppercase(first)")


class BadServerFields(BaseSurrealModel):
    model_config = SurrealConfigDict(server_fields=["nope"])
    id: str


class TestServerFields:
    def test_returns_the_configured_names(self) -> None:
        assert Stamped.get_server_fields() == frozenset({"created_at"})

    def test_absent_config_is_empty(self) -> None:
        assert Plain.get_server_fields() == frozenset()

    def test_merges_computed_fields(self) -> None:
        """A computed field is server-owned by construction, so it belongs to the same set."""
        assert StampedComputed.get_server_fields() == frozenset({"created_at", "shouted"})

    def test_computed_only_model_still_reports_them(self) -> None:
        class OnlyComputed(BaseSurrealModel):
            id: str
            first: str = ""
            shouted: Computed[str] = computed("string::uppercase(first)")

        assert OnlyComputed.get_server_fields() == frozenset({"shouted"})

    def test_unknown_name_raises_naming_it(self) -> None:
        with pytest.raises(ValueError, match="nope"):
            BadServerFields.get_server_fields()

    def test_subclass_inherits_the_config(self) -> None:
        class StampedChild(Stamped):
            more: str = ""

        assert StampedChild.get_server_fields() == frozenset({"created_at"})


# ==================== E2E helpers ====================


def _url() -> str:
    host = os.environ.get("SURREALDB_HOST", "localhost")
    port = os.environ.get("SURREALDB_PORT", "8000")
    return f"ws://{host}:{port}/rpc"


def _connect() -> None:
    SurrealDBConnectionManager.set_connection(url=_url(), user="root", password="root", namespace="ns", database="db")


@contextlib.asynccontextmanager
async def alias_client(*tables: str):
    """Connected ORM client with the given tables dropped before and after.

    Same shape as the computed-field suite's helper: the SDK's WebSocket client is bound to
    the loop that created it, so it must be opened inside the test's own loop rather than in
    a module-scoped fixture.
    """
    tables = tables or ("Aliased",)
    _connect()
    client = await SurrealDBConnectionManager.get_client()
    for table in tables:
        with contextlib.suppress(Exception):
            await client.query(f"REMOVE TABLE {table};", {})
    try:
        yield client
    finally:
        for table in tables:
            with contextlib.suppress(Exception):
                await client.query(f"REMOVE TABLE {table};", {})
        await SurrealDBConnectionManager.close_connection()


async def _raw_row(client: Any, table: str, record: str) -> dict[str, Any]:
    """Read a row as the *server* stores it, bypassing the ORM's hydration."""
    rows = await client.query(f"SELECT * FROM {table}:{record};", {})
    if isinstance(rows, list):
        return dict(rows[0]) if rows else {}
    return dict(rows) if isinstance(rows, dict) else {}
