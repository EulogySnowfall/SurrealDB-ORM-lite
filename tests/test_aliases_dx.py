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

from surreal_orm_lite import BaseSurrealModel, Q, SurrealDBConnectionManager
from surreal_orm_lite.functions import Computed, SurrealFunc, computed
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


class Stamped2(BaseSurrealModel):
    """E2E twin of ``Stamped``: ``created_at`` is filled by a server-side ``DEFAULT``."""

    model_config = SurrealConfigDict(server_fields=["created_at"])
    id: str
    title: str = ""
    created_at: Any = None


# ==================== Task 3 — write payloads ====================


class TestWritePayload:
    def test_uses_the_column_names(self) -> None:
        payload = Aliased(id="a", password="secret", display="Ada")._write_payload()
        assert payload == {"password_hash": "secret", "display_name": "Ada", "plain": 0}

    def test_omits_the_id(self) -> None:
        assert "id" not in Aliased(id="a")._write_payload()

    def test_omits_server_fields_on_a_create(self) -> None:
        payload = Stamped(id="a", title="t", created_at="anything")._write_payload()
        assert payload == {"title": "t"}

    def test_keeps_server_fields_on_a_replace(self) -> None:
        """On ``UPDATE/UPSERT … CONTENT`` omission *deletes* the column — a ``DEFAULT`` is a
        create-time default on both DB lines — so the instance's value is sent instead."""
        payload = Stamped(id="a", title="t", created_at="kept")._write_payload(replace=True)
        assert payload == {"title": "t", "created_at": "kept"}

    def test_omits_computed_fields_on_both(self) -> None:
        """A ``VALUE`` clause re-evaluates on every write, so a computed field is never sent."""
        assert StampedComputed(id="a", first="ada")._write_payload() == {"first": "ada"}
        # The replace payload keeps the server_fields half, but never the computed one.
        assert StampedComputed(id="a", first="ada")._write_payload(replace=True) == {
            "first": "ada",
            "created_at": None,
        }

    def test_plain_model_is_unchanged(self) -> None:
        assert Plain(id="a", name="x")._write_payload() == {"name": "x"}


class TestWritePayloadE2E:
    @pytest.mark.asyncio
    async def test_save_stores_the_aliased_column(self) -> None:
        async with alias_client() as client:
            await Aliased(id="ada", password="secret", display="Ada").save()
            row = await _raw_row(client, "Aliased", "ada")
            assert row["password_hash"] == "secret"
            assert row["display_name"] == "Ada"
            assert "password" not in row and "display" not in row

    @pytest.mark.asyncio
    async def test_server_default_survives_a_save(self) -> None:
        """The point of ``server_fields``: the client never volunteers the column, so the
        server's ``DEFAULT`` applies instead of being overwritten with the model's ``None``."""
        async with alias_client("Stamped2") as client:
            await client.query("DEFINE FIELD created_at ON Stamped2 TYPE option<datetime> DEFAULT time::now();", {})
            await Stamped2(id="a", title="first").save()
            row = await _raw_row(client, "Stamped2", "a")
            assert row["created_at"] is not None

    @pytest.mark.asyncio
    async def test_server_stamp_survives_a_replace(self) -> None:
        """``update()``/``upsert()`` are REPLACE: the stamp is carried in the payload rather
        than omitted, because omitting it would delete the column outright."""
        async with alias_client("Stamped2") as client:
            await client.query("DEFINE FIELD created_at ON Stamped2 TYPE option<datetime> DEFAULT time::now();", {})
            instance = await Stamped2(id="a", title="first").save()
            stamped = (await _raw_row(client, "Stamped2", "a"))["created_at"]
            assert stamped is not None

            instance.title = "second"
            await instance.update()
            row = await _raw_row(client, "Stamped2", "a")
            assert row["title"] == "second"
            assert row["created_at"] == stamped

            instance.title = "third"
            await instance.upsert()
            row = await _raw_row(client, "Stamped2", "a")
            assert row["title"] == "third"
            assert row["created_at"] == stamped

    @pytest.mark.asyncio
    async def test_required_server_field_does_not_break_a_replace(self) -> None:
        """A non-optional server column omitted from a REPLACE is a hard server error
        ("Found NONE for field …"), on 2.6.x and 3.x alike. Keeping it in the payload is what
        makes ``update()`` usable on such a model at all."""
        async with alias_client("Stamped2") as client:
            await client.query("DEFINE FIELD created_at ON Stamped2 TYPE datetime DEFAULT time::now();", {})
            instance = await Stamped2(id="a", title="first").save()
            instance.title = "second"
            await instance.update()
            assert (await _raw_row(client, "Stamped2", "a"))["title"] == "second"


# ==================== Task 4 — hydration ====================


class TestApplyRecord:
    def test_maps_a_column_onto_the_python_attribute(self) -> None:
        instance = Aliased(id="a")
        instance._apply_record({"password_hash": "secret"})
        assert instance.password == "secret"

    def test_only_is_matched_on_python_names(self) -> None:
        instance = Aliased(id="a")
        instance._apply_record({"password_hash": "secret", "display_name": "Ada"}, only={"password"})
        assert instance.password == "secret"
        assert instance.display == ""

    def test_ignores_an_unknown_column(self) -> None:
        instance = Aliased(id="a")
        instance._apply_record({"nope": 1})
        assert not hasattr(instance, "nope")

    def test_plain_columns_still_apply(self) -> None:
        instance = Aliased(id="a")
        instance._apply_record({"plain": 7})
        assert instance.plain == 7


class TestHydrationE2E:
    @pytest.mark.asyncio
    async def test_from_db_hydrates_the_python_attribute(self) -> None:
        async with alias_client():
            await Aliased(id="ada", password="secret", display="Ada").save()
            found = await Aliased.objects().filter(id="ada").exec()
            assert found[0].password == "secret"
            assert found[0].display == "Ada"

    @pytest.mark.asyncio
    async def test_refresh_hydrates_the_python_attribute(self) -> None:
        async with alias_client() as client:
            instance = await Aliased(id="ada", password="secret").save()
            await client.query("UPDATE Aliased:ada SET password_hash = 'rotated';", {})
            await instance.refresh()
            assert instance.password == "rotated"

    @pytest.mark.asyncio
    async def test_server_field_is_hydrated_after_save(self) -> None:
        async with alias_client("Stamped2") as client:
            await client.query("DEFINE FIELD created_at ON Stamped2 TYPE option<datetime> DEFAULT time::now();", {})
            instance = await Stamped2(id="a", title="first").save()
            assert instance.created_at is not None


# ==================== Task 5 — merge() and server_values ====================


class TestMergeAliasesE2E:
    @pytest.mark.asyncio
    async def test_merge_writes_the_aliased_column(self) -> None:
        async with alias_client() as client:
            instance = await Aliased(id="ada", password="secret").save()
            await instance.merge(password="rotated")
            row = await _raw_row(client, "Aliased", "ada")
            assert row["password_hash"] == "rotated"
            assert "password" not in row
            assert instance.password == "rotated"

    @pytest.mark.asyncio
    async def test_server_values_compile_against_the_column(self) -> None:
        async with alias_client() as client:
            instance = await Aliased(id="ada", password="secret").save()
            await instance.merge(server_values={"display": SurrealFunc("string::uppercase($who)")}, extra_vars={"who": "ada"})
            row = await _raw_row(client, "Aliased", "ada")
            assert row["display_name"] == "ADA"
            assert "display" not in row
            assert instance.display == "ADA"

    @pytest.mark.asyncio
    async def test_save_with_server_values_uses_the_column(self) -> None:
        async with alias_client() as client:
            instance = await Aliased(id="ada").save(
                server_values={"password": SurrealFunc("string::concat('h:', $raw)")},
                extra_vars={"raw": "secret"},
            )
            row = await _raw_row(client, "Aliased", "ada")
            assert row["password_hash"] == "h:secret"
            assert "password" not in row
            assert instance.password == "h:secret"

    @pytest.mark.asyncio
    async def test_upsert_and_patch_address_the_column(self) -> None:
        async with alias_client() as client:
            instance = await Aliased(id="ada", password="secret").save()
            instance.password = "upserted"
            await instance.upsert()
            assert (await _raw_row(client, "Aliased", "ada"))["password_hash"] == "upserted"

            await instance.patch([{"op": "replace", "path": "/password_hash", "value": "patched"}])
            assert (await _raw_row(client, "Aliased", "ada"))["password_hash"] == "patched"
            assert instance.password == "patched"


# ==================== Task 6 — QuerySet and Q ====================


class TestQuerySetAliases:
    def test_filter_names_the_column(self) -> None:
        query, _ = Aliased.objects().filter(password="secret")._compile_query()
        assert "password_hash =" in query
        assert "password =" not in query

    def test_filter_lookup_names_the_column(self) -> None:
        query, _ = Aliased.objects().filter(display__contains="Ad")._compile_query()
        assert "display_name" in query
        assert "display " not in query.replace("display_name", "")

    def test_q_object_names_the_column(self) -> None:
        query, _ = Aliased.objects().filter(Q(password="a") | Q(display="b"))._compile_query()
        assert "password_hash" in query
        assert "display_name" in query

    def test_nested_q_names_the_column(self) -> None:
        query, _ = Aliased.objects().filter(~(Q(password="a") & Q(plain=1)))._compile_query()
        assert "password_hash" in query

    def test_order_by_names_the_column(self) -> None:
        query, _ = Aliased.objects().order_by("-password")._compile_query()
        assert "ORDER BY password_hash DESC" in query

    def test_values_group_by_names_the_column(self) -> None:
        """``values()`` feeds the GROUP BY of the aggregation compiler, not ``_compile_query``."""
        queryset = Aliased.objects().values("display")
        assert queryset._group_by_fields == ["display_name"]
        query, _ = queryset._compile_group_by_query()
        assert "GROUP BY display_name" in query

    def test_fetch_names_the_column(self) -> None:
        query, _ = Aliased.objects().fetch("password")._compile_query()
        assert "FETCH password_hash" in query

    def test_unaliased_model_is_untouched(self) -> None:
        query, _ = Plain.objects().filter(name="x").order_by("name")._compile_query()
        assert "name =" in query
        assert "ORDER BY name ASC" in query


class TestQuerySetAliasesE2E:
    @pytest.mark.asyncio
    async def test_filter_and_order_round_trip(self) -> None:
        async with alias_client():
            await Aliased(id="a", password="alpha", display="A").save()
            await Aliased(id="b", password="beta", display="B").save()

            found = await Aliased.objects().filter(password="beta").exec()
            assert len(found) == 1
            assert found[0].display == "B"

            ordered = await Aliased.objects().order_by("-password").exec()
            assert [item.password for item in ordered] == ["beta", "alpha"]

    @pytest.mark.asyncio
    async def test_bulk_update_writes_the_column(self) -> None:
        async with alias_client() as client:
            await Aliased(id="a", password="alpha").save()
            await Aliased(id="b", password="beta").save()

            count = await Aliased.objects().filter(password="beta").bulk_update(password="rotated")
            assert count == 1
            assert (await _raw_row(client, "Aliased", "b"))["password_hash"] == "rotated"
            assert (await _raw_row(client, "Aliased", "a"))["password_hash"] == "alpha"

    @pytest.mark.asyncio
    async def test_aggregation_helpers_name_the_column(self) -> None:
        async with alias_client("Scored"):

            class Scored(BaseSurrealModel):
                id: str
                score: int = Field(default=0, alias="score_value")

            await Scored(id="a", score=10).save()
            await Scored(id="b", score=30).save()

            assert await Scored.objects().sum("score") == 40
            assert await Scored.objects().avg("score") == 20.0
            assert await Scored.objects().min("score") == 10
            assert await Scored.objects().max("score") == 30

    @pytest.mark.asyncio
    async def test_get_or_create_round_trips_on_an_aliased_field(self) -> None:
        async with alias_client() as client:
            created_obj, created = await Aliased.objects().get_or_create(id="a", defaults={"password": "alpha"})
            assert created is True
            assert created_obj.password == "alpha"
            assert (await _raw_row(client, "Aliased", "a"))["password_hash"] == "alpha"

            again, created = await Aliased.objects().get_or_create(id="a", defaults={"password": "ignored"})
            assert created is False
            assert again.password == "alpha"
