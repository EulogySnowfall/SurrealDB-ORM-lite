"""Tests for v0.18.0 — field aliases, ``server_fields`` and ``merge(refresh=False)``.

All three features are pure client-side behaviour (Pydantic aliasing, payload exclusion, a
skipped round-trip), so nothing here is version-gated: every test must pass identically on
SurrealDB 2.6.x and 3.x. A ``pytest.skip`` appearing in this file would be a design smell.
"""

from typing import Any

import pytest
from pydantic import Field

from surreal_orm_lite import BaseSurrealModel, Q, SurrealDBConnectionManager
from surreal_orm_lite.exceptions import SurrealDbError
from surreal_orm_lite.functions import Computed, SurrealFunc, computed
from surreal_orm_lite.model_base import _ALIAS_MAPS, SurrealConfigDict
from tests.conftest import orm_client

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


async def _raw_row(client: Any, table: str, record: str) -> dict[str, Any]:
    """Read a row as the *server* stores it, bypassing the ORM's hydration."""
    rows = await client.query(f"SELECT * FROM {table}:{record};", {})
    if isinstance(rows, list):
        return dict(rows[0]) if rows else {}
    return dict(rows) if isinstance(rows, dict) else {}


def _spy(client: Any, method: str) -> list[tuple[Any, ...]]:
    """Record every call to ``client.<method>`` from now on, still calling through.

    Used to assert on *round-trips* rather than on results: "did this issue a SELECT" is the
    observable difference ``refresh=False`` makes, and it is invisible in the final state.
    """
    calls: list[tuple[Any, ...]] = []
    original = getattr(client, method)

    async def recorder(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return await original(*args, **kwargs)

    setattr(client, method, recorder)
    return calls


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
        async with orm_client() as client:
            await Aliased(id="ada", password="secret", display="Ada").save()
            row = await _raw_row(client, "Aliased", "ada")
            assert row["password_hash"] == "secret"
            assert row["display_name"] == "Ada"
            assert "password" not in row and "display" not in row

    @pytest.mark.asyncio
    async def test_server_default_survives_a_save(self) -> None:
        """The point of ``server_fields``: the client never volunteers the column, so the
        server's ``DEFAULT`` applies instead of being overwritten with the model's ``None``."""
        async with orm_client("Stamped2") as client:
            await client.query("DEFINE FIELD created_at ON Stamped2 TYPE option<datetime> DEFAULT time::now();", {})
            await Stamped2(id="a", title="first").save()
            row = await _raw_row(client, "Stamped2", "a")
            assert row["created_at"] is not None

    @pytest.mark.asyncio
    async def test_server_stamp_survives_a_replace(self) -> None:
        """``update()``/``upsert()`` are REPLACE: the stamp is carried in the payload rather
        than omitted, because omitting it would delete the column outright."""
        async with orm_client("Stamped2") as client:
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
        async with orm_client("Stamped2") as client:
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
        async with orm_client():
            await Aliased(id="ada", password="secret", display="Ada").save()
            found = await Aliased.objects().filter(id="ada").exec()
            assert found[0].password == "secret"
            assert found[0].display == "Ada"

    @pytest.mark.asyncio
    async def test_refresh_hydrates_the_python_attribute(self) -> None:
        async with orm_client() as client:
            instance = await Aliased(id="ada", password="secret").save()
            await client.query("UPDATE Aliased:ada SET password_hash = 'rotated';", {})
            await instance.refresh()
            assert instance.password == "rotated"

    @pytest.mark.asyncio
    async def test_server_field_is_hydrated_after_save(self) -> None:
        async with orm_client("Stamped2") as client:
            await client.query("DEFINE FIELD created_at ON Stamped2 TYPE option<datetime> DEFAULT time::now();", {})
            instance = await Stamped2(id="a", title="first").save()
            assert instance.created_at is not None


# ==================== Task 5 — merge() and server_values ====================


class TestMergeAliasesE2E:
    @pytest.mark.asyncio
    async def test_merge_writes_the_aliased_column(self) -> None:
        async with orm_client() as client:
            instance = await Aliased(id="ada", password="secret").save()
            await instance.merge(password="rotated")
            row = await _raw_row(client, "Aliased", "ada")
            assert row["password_hash"] == "rotated"
            assert "password" not in row
            assert instance.password == "rotated"

    @pytest.mark.asyncio
    async def test_server_values_compile_against_the_column(self) -> None:
        async with orm_client() as client:
            instance = await Aliased(id="ada", password="secret").save()
            await instance.merge(server_values={"display": SurrealFunc("string::uppercase($who)")}, extra_vars={"who": "ada"})
            row = await _raw_row(client, "Aliased", "ada")
            assert row["display_name"] == "ADA"
            assert "display" not in row
            assert instance.display == "ADA"

    @pytest.mark.asyncio
    async def test_save_with_server_values_uses_the_column(self) -> None:
        async with orm_client() as client:
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
        async with orm_client() as client:
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
        async with orm_client():
            await Aliased(id="a", password="alpha", display="A").save()
            await Aliased(id="b", password="beta", display="B").save()

            found = await Aliased.objects().filter(password="beta").exec()
            assert len(found) == 1
            assert found[0].display == "B"

            ordered = await Aliased.objects().order_by("-password").exec()
            assert [item.password for item in ordered] == ["beta", "alpha"]

    @pytest.mark.asyncio
    async def test_bulk_update_writes_the_column(self) -> None:
        async with orm_client() as client:
            await Aliased(id="a", password="alpha").save()
            await Aliased(id="b", password="beta").save()

            count = await Aliased.objects().filter(password="beta").bulk_update(password="rotated")
            assert count == 1
            assert (await _raw_row(client, "Aliased", "b"))["password_hash"] == "rotated"
            assert (await _raw_row(client, "Aliased", "a"))["password_hash"] == "alpha"

    @pytest.mark.asyncio
    async def test_aggregation_helpers_name_the_column(self) -> None:
        async with orm_client("Scored"):

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
        async with orm_client() as client:
            created_obj, created = await Aliased.objects().get_or_create(id="a", defaults={"password": "alpha"})
            assert created is True
            assert created_obj.password == "alpha"
            assert (await _raw_row(client, "Aliased", "a"))["password_hash"] == "alpha"

            again, created = await Aliased.objects().get_or_create(id="a", defaults={"password": "ignored"})
            assert created is False
            assert again.password == "alpha"


# ==================== Task 7 — merge(refresh=False) ====================


class TestMergeRefreshE2E:
    @pytest.mark.asyncio
    async def test_default_resyncs_from_the_returned_row(self) -> None:
        async with orm_client() as client:
            instance = await Aliased(id="ada", password="secret").save()
            merges = _spy(client, "merge")
            await instance.merge(password="rotated")
            assert len(merges) == 1
            assert instance.password == "rotated"

    @pytest.mark.asyncio
    async def test_refresh_false_skips_the_round_trip(self) -> None:
        async with orm_client() as client:
            instance = await Aliased(id="ada", password="secret").save()
            selects = _spy(client, "select")
            await instance.merge(password="rotated", refresh=False)
            assert selects == [], "refresh=False must not read anything back"
            assert instance.password == "rotated"

    @pytest.mark.asyncio
    async def test_refresh_false_still_persists(self) -> None:
        async with orm_client() as client:
            instance = await Aliased(id="ada", password="secret").save()
            await instance.merge(password="rotated", refresh=False)
            assert (await _raw_row(client, "Aliased", "ada"))["password_hash"] == "rotated"

    @pytest.mark.asyncio
    async def test_server_values_ask_for_no_row_back(self) -> None:
        async with orm_client() as client:
            instance = await Aliased(id="ada", password="secret").save()
            queries = _spy(client, "query")
            await instance.merge(
                display="local",
                server_values={"password": SurrealFunc("string::uppercase('x')")},
                refresh=False,
            )
            statements = [call[0][0] for call in queries if call[0]]
            assert any("RETURN NONE" in statement for statement in statements)
            # The literal kwarg is applied locally; the server-computed one stays as it was.
            assert instance.display == "local"
            assert (await _raw_row(client, "Aliased", "ada"))["password_hash"] == "X"

    @pytest.mark.asyncio
    async def test_server_values_default_still_syncs(self) -> None:
        async with orm_client():
            instance = await Aliased(id="ada", password="secret").save()
            await instance.merge(server_values={"password": SurrealFunc("string::uppercase('x')")})
            assert instance.password == "X"


# ==================== Task 8 — computed vs server_fields write guards ====================


class TestWriteGuards:
    """The two halves of ``get_server_fields()`` are equal for payload building and opposite
    for explicit writes: a computed field can never be written, a ``server_fields`` entry can."""

    @pytest.mark.asyncio
    async def test_merge_still_refuses_a_computed_field(self) -> None:
        with pytest.raises(ValueError, match="computed field"):
            await StampedComputed(id="a").merge(shouted="nope")

    @pytest.mark.asyncio
    async def test_merge_accepts_an_explicit_server_field(self) -> None:
        async with orm_client("Stamped2") as client:
            instance = await Stamped2(id="a", title="first").save()
            await instance.merge(created_at="backfilled")
            assert (await _raw_row(client, "Stamped2", "a"))["created_at"] == "backfilled"
            assert instance.created_at == "backfilled"

    @pytest.mark.asyncio
    async def test_bulk_update_accepts_an_explicit_server_field(self) -> None:
        async with orm_client("Stamped2") as client:
            await Stamped2(id="a", title="first").save()
            count = await Stamped2.objects().filter(id="a").bulk_update(created_at="backfilled")
            assert count == 1
            assert (await _raw_row(client, "Stamped2", "a"))["created_at"] == "backfilled"

    @pytest.mark.asyncio
    async def test_server_values_accept_an_explicit_server_field(self) -> None:
        async with orm_client("Stamped2") as client:
            instance = await Stamped2(id="a", title="first").save()
            await instance.merge(server_values={"created_at": SurrealFunc("'computed'")})
            assert (await _raw_row(client, "Stamped2", "a"))["created_at"] == "computed"

    @pytest.mark.asyncio
    async def test_bulk_update_still_refuses_a_computed_field(self) -> None:
        with pytest.raises(ValueError, match="computed field"):
            await StampedComputed.objects().bulk_update(shouted="nope")


# ==================== Review follow-up: every other alias boundary ====================


class Scored(BaseSurrealModel):
    id: str
    amount: int = Field(default=0, alias="amt")
    tags: list[str] = Field(default_factory=list, alias="tag_list")
    group: str = Field(default="", alias="grp")


class TestAtomicOpsAliases:
    """Atomic ops interpolate the field straight into a SET clause. Before the fix they wrote
    the Python name, which SurrealDB happily honours — by creating a *second*, phantom column
    and leaving the real one untouched."""

    @pytest.mark.asyncio
    async def test_increment_targets_the_column(self) -> None:
        async with orm_client("Scored") as client:
            item = await Scored(id="a", amount=1).save()
            await item.atomic_increment("amount", 5)
            row = await _raw_row(client, "Scored", "a")
            assert row["amt"] == 6
            assert "amount" not in row
            assert item.amount == 6

    @pytest.mark.asyncio
    async def test_array_ops_target_the_column(self) -> None:
        async with orm_client("Scored") as client:
            item = await Scored(id="a", tags=["x"]).save()
            await item.atomic_append("tags", "y")
            await item.atomic_set_add("tags", "y")
            await item.atomic_append_many("tags", ["z", "w"])
            await item.atomic_remove("tags", "x")
            row = await _raw_row(client, "Scored", "a")
            assert row["tag_list"] == ["y", "z", "w"]
            assert "tags" not in row
            assert item.tags == ["y", "z", "w"]

    @pytest.mark.asyncio
    async def test_remove_many_targets_the_column(self) -> None:
        async with orm_client("Scored") as client:
            item = await Scored(id="a", tags=["x", "y", "z"]).save()
            await item.atomic_remove_many("tags", ["x", "z"])
            assert (await _raw_row(client, "Scored", "a"))["tag_list"] == ["y"]


class TestAnnotateAliases:
    """``values()`` was translated but the ``Aggregation`` objects were not, so the statement
    looked right and silently aggregated a column that does not exist — a permanent zero."""

    def test_aggregation_expressions_name_the_column(self) -> None:
        from surreal_orm_lite import Count, Sum

        query, _ = Scored.objects().values("group").annotate(total=Sum("amount"), n=Count("amount"))._compile_group_by_query()
        assert "math::sum(amt)" in query
        assert "count(amt)" in query
        assert "GROUP BY grp" in query

    @pytest.mark.asyncio
    async def test_annotate_returns_real_numbers_and_python_keys(self) -> None:
        from surreal_orm_lite import Sum

        async with orm_client("Scored"):
            await Scored(id="a", amount=10, group="g").save()
            await Scored(id="b", amount=30, group="g").save()

            rows = await Scored.objects().values("group").annotate(total=Sum("amount")).exec()
            assert len(rows) == 1
            assert rows[0]["total"] == 40
            # The caller asked in Python names, so the result is keyed that way too.
            assert rows[0]["group"] == "g"
            assert "grp" not in rows[0]


class TestSignalPayloadNames:
    @pytest.mark.asyncio
    async def test_update_reports_python_field_names(self) -> None:
        from surreal_orm_lite import post_update

        seen: list[list[str]] = []

        async def handler(sender: Any, instance: Any, update_fields: list[str]) -> None:
            seen.append(sorted(update_fields))

        post_update.connect(Scored)(handler)
        try:
            async with orm_client("Scored"):
                item = await Scored(id="a", amount=1).save()
                await item.update()
                await item.merge(amount=2)
        finally:
            post_update.disconnect(handler, Scored)

        # Both paths must speak the vocabulary a handler knows: the model's attributes.
        assert seen[0] == ["amount", "group", "tags"]
        assert seen[1] == ["amount"]


class TestAliasMapValidation:
    def test_alias_colliding_with_another_field_raises(self) -> None:
        class Collide(BaseSurrealModel):
            id: str
            pw: str = Field(default="", alias="hash")
            hash: str = ""

        with pytest.raises(ValueError, match="collides"):
            Collide.get_field_aliases()

    def test_two_fields_sharing_one_column_raises(self) -> None:
        class Shared(BaseSurrealModel):
            id: str
            a: str = Field(default="", alias="same")
            b: str = Field(default="", alias="same")

        with pytest.raises(ValueError, match="both alias"):
            Shared.get_field_aliases()

    def test_alias_equal_to_its_own_name_is_fine(self) -> None:
        class SelfNamed(BaseSurrealModel):
            id: str
            a: str = Field(default="", alias="a")

        assert SelfNamed.get_field_aliases() == {"a": "a"}


class TestSerializationAliasIsNotAColumnRename:
    """``model_dump(by_alias=True)`` also honours ``serialization_alias``, which the ORM's map
    deliberately ignores. Letting the two disagree wrote rows under a name no read path ever
    looks for — the value landed in the database and became unreachable."""

    def test_payload_follows_the_orm_map_not_pydantic(self) -> None:
        class Mixed(BaseSurrealModel):
            id: str
            a: str = Field(default="A", serialization_alias="a_col")
            b: str = Field(default="B", alias="b_in", serialization_alias="b_out")

        assert Mixed.get_field_aliases() == {"b": "b_in"}
        assert Mixed(id="x")._write_payload() == {"a": "A", "b_in": "B"}

    @pytest.mark.asyncio
    async def test_the_written_row_is_findable(self) -> None:
        class Mixed2(BaseSurrealModel):
            id: str
            b: str = Field(default="", alias="b_in", serialization_alias="b_out")

        async with orm_client("Mixed2") as client:
            await Mixed2(id="x", b="value").save()
            assert (await _raw_row(client, "Mixed2", "x"))["b_in"] == "value"
            found = await Mixed2.objects().filter(b="value").exec()
            assert len(found) == 1


class TestModelAuthAliases:
    def test_signup_and_signin_ddl_name_the_columns(self) -> None:
        from surreal_orm_lite import AuthenticatedUserMixin

        class AliasUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(access_name="alias_acct")
            id: str | None = None
            email: str = Field(default="", alias="email_addr")
            password: str = Field(default="", alias="password_hash")

        ddl = "\n".join(AliasUser.access_ddl())
        # Column on the left, the caller's keyword on the right.
        assert "email_addr = $email" in ddl
        assert "password_hash = crypto::argon2::generate($password)" in ddl
        assert "WHERE email_addr = $email" in ddl
        assert "crypto::argon2::compare(password_hash, $password)" in ddl

    def test_signup_skips_server_owned_columns(self) -> None:
        from surreal_orm_lite import AuthenticatedUserMixin

        class StampedUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(access_name="stamped_acct", server_fields=["created_at"])
            id: str | None = None
            email: str = ""
            password: str = ""
            created_at: Any = None

        ddl = "\n".join(StampedUser.access_ddl())
        # SIGNUP is a CREATE: listing the column would overwrite the server's DEFAULT with NONE.
        assert "created_at" not in ddl


class TestComputedDdlAliases:
    def test_define_field_names_the_column(self) -> None:
        class ComputedAliased(BaseSurrealModel):
            id: str
            first: str = ""
            shouted: Computed[str] = computed("string::uppercase(first)")

        ComputedAliased.model_fields["shouted"].alias = "shouted_col"
        _ALIAS_MAPS.pop(ComputedAliased, None)
        try:
            assert "DEFINE FIELD OVERWRITE shouted_col ON ComputedAliased" in ComputedAliased.computed_field_ddl()[0]
        finally:
            ComputedAliased.model_fields["shouted"].alias = None
            _ALIAS_MAPS.pop(ComputedAliased, None)


# ==================== PR #183 review ====================


class Located(BaseSurrealModel):
    id: str
    address: dict[str, Any] = Field(default_factory=dict, alias="addr")
    category: str = ""
    display: str = Field(default="", alias="display_name")


class Regioned(BaseSurrealModel):
    """``region`` is filled by a server ``DEFAULT`` unless a caller names it."""

    model_config = SurrealConfigDict(server_fields=["region"])
    id: str | None = None
    owner: str = ""
    region: str | None = None


class TestDottedPathsTranslateTheRoot:
    """``filter(**kw)`` translated the root of a dotted path; ``Q`` and ``Aggregation`` compared
    the whole key against the map, so ``address.city`` never matched and filtered nothing."""

    def test_q_translates_the_first_segment(self) -> None:
        query, _ = Located.objects().filter(Q(**{"address.city": "x"}))._compile_query()
        assert "addr.city" in query
        assert "address.city" not in query

    def test_q_and_kwarg_compile_the_same_column(self) -> None:
        kwarg, _ = Located.objects().filter(**{"address.city": "x"})._compile_query()
        q_obj, _ = Located.objects().filter(Q(**{"address.city": "x"}))._compile_query()
        assert kwarg == q_obj

    def test_aggregation_translates_the_first_segment(self) -> None:
        from surreal_orm_lite import Sum

        query, _ = Located.objects().values("category").annotate(n=Sum("address.n"))._compile_group_by_query()
        assert "math::sum(addr.n)" in query

    @pytest.mark.asyncio
    async def test_q_on_a_nested_path_finds_the_row(self) -> None:
        async with orm_client("Located"):
            await Located(id="a", address={"city": "Paris"}).save()
            found = await Located.objects().filter(Q(**{"address.city": "Paris"})).exec()
            assert [row.id for row in found] == ["a"]


class TestPatchTranslatesPointers:
    """Pointers used to go out verbatim, so ``/display`` created a phantom ``display`` column,
    left ``display_name`` alone, and the instance kept its old value — with no error."""

    def test_python_name_pointer_is_rewritten(self) -> None:
        ops = Located._columns_for_patch([{"op": "replace", "path": "/display/0", "value": "A"}])
        assert ops == [{"op": "replace", "path": "/display_name/0", "value": "A"}]

    def test_column_pointer_is_left_alone(self) -> None:
        ops = Located._columns_for_patch([{"op": "replace", "path": "/display_name", "value": "A"}])
        assert ops[0]["path"] == "/display_name"

    def test_from_is_rewritten_for_move_and_copy(self) -> None:
        ops = Located._columns_for_patch(
            [
                {"op": "move", "from": "/display", "path": "/category"},
                {"op": "copy", "from": "/address/city", "path": "/display"},
            ]
        )
        assert ops[0] == {"op": "move", "from": "/display_name", "path": "/category"}
        assert ops[1] == {"op": "copy", "from": "/addr/city", "path": "/display_name"}

    def test_whole_document_value_is_rekeyed(self) -> None:
        ops = Located._columns_for_patch([{"op": "replace", "path": "", "value": {"display": "A", "category": "c"}}])
        assert ops[0]["value"] == {"display_name": "A", "category": "c"}

    def test_the_callers_list_is_not_mutated(self) -> None:
        original = [{"op": "replace", "path": "/display", "value": "A"}]
        Located._columns_for_patch(original)
        assert original == [{"op": "replace", "path": "/display", "value": "A"}]

    def test_computed_guard_sees_through_a_column_pointer(self) -> None:
        class ComputedCol(BaseSurrealModel):
            id: str
            first: str = ""
            shouted: Computed[str] = computed("string::uppercase(first)")

        ComputedCol.model_fields["shouted"].alias = "shouted_col"
        _ALIAS_MAPS.pop(ComputedCol, None)
        try:
            with pytest.raises(ValueError, match="computed field"):
                ComputedCol._reject_computed_patch([{"op": "replace", "path": "/shouted_col", "value": "x"}], "patch()")
        finally:
            ComputedCol.model_fields["shouted"].alias = None
            _ALIAS_MAPS.pop(ComputedCol, None)

    @pytest.mark.asyncio
    async def test_instance_patch_by_python_name(self) -> None:
        async with orm_client("Located") as client:
            instance = await Located(id="a", display="old").save()
            await instance.patch([{"op": "replace", "path": "/display", "value": "new"}])
            row = await _raw_row(client, "Located", "a")
            assert row["display_name"] == "new"
            assert "display" not in row
            assert instance.display == "new"

    @pytest.mark.asyncio
    async def test_queryset_patch_by_python_name(self) -> None:
        async with orm_client("Located") as client:
            await Located(id="a", display="old").save()
            assert await Located.objects().filter(id="a").patch([{"op": "replace", "path": "/display", "value": "new"}]) == 1
            row = await _raw_row(client, "Located", "a")
            assert row["display_name"] == "new"
            assert "display" not in row


class TestOrCreateHonoursExplicitServerFields:
    """``_writable_payload`` dropped a named ``server_fields`` entry, so ``update_or_create``
    lost it on the merge branch and ``get_or_create`` created a row the next identical call
    could not find."""

    def test_writable_payload_keeps_a_server_field(self) -> None:
        assert Regioned.objects()._writable_payload({"owner": "bob", "region": "us"}) == {"owner": "bob", "region": "us"}

    def test_writable_payload_still_drops_a_computed_field(self) -> None:
        payload = StampedComputed.objects()._writable_payload({"first": "a", "shouted": "X", "created_at": "t"})
        assert payload == {"first": "a", "created_at": "t"}

    @pytest.mark.asyncio
    async def test_get_or_create_converges_on_a_server_field(self) -> None:
        async with orm_client("Regioned") as client:
            await client.query("DEFINE FIELD region ON Regioned TYPE option<string> DEFAULT 'eu';", {})
            _, created = await Regioned.objects().get_or_create(owner="bob", region="us")
            assert created is True
            _, created = await Regioned.objects().get_or_create(owner="bob", region="us")
            assert created is False
            assert await Regioned.objects().count() == 1

    @pytest.mark.asyncio
    async def test_the_default_still_applies_when_unnamed(self) -> None:
        async with orm_client("Regioned") as client:
            await client.query("DEFINE FIELD region ON Regioned TYPE option<string> DEFAULT 'eu';", {})
            obj, _ = await Regioned.objects().get_or_create(id="x", owner="bob")
            assert obj.region == "eu"

    @pytest.mark.asyncio
    async def test_update_or_create_writes_a_server_field_on_update(self) -> None:
        async with orm_client("Regioned") as client:
            await Regioned(id="a", owner="bob", region="eu").save()
            _, created = await Regioned.objects().update_or_create(id="a", defaults={"region": "us"})
            assert created is False
            assert (await _raw_row(client, "Regioned", "a"))["region"] == "us"


class TestGroupedResultKeys:
    def _queryset(self, **annotations: Any) -> Any:
        from surreal_orm_lite import Count

        return Located.objects().values("category").annotate(**(annotations or {"n": Count()}))

    def test_an_annotation_alias_is_never_renamed(self) -> None:
        from surreal_orm_lite import Count

        queryset = self._queryset(display_name=Count())
        assert queryset._py_keyed({"category": "a", "display_name": 3}) == {"category": "a", "display_name": 3}

    def test_a_grouped_column_is_renamed(self) -> None:
        from surreal_orm_lite import Count

        queryset = Located.objects().values("display").annotate(n=Count())
        assert queryset._py_keyed({"display_name": "A", "n": 1}) == {"display": "A", "n": 1}

    def test_an_alias_colliding_with_a_grouped_field_is_refused(self) -> None:
        from surreal_orm_lite import Count

        with pytest.raises(ValueError, match="display"):
            Located.objects().values("display").annotate(display=Count())._compile_group_by_query()
        with pytest.raises(ValueError, match="display_name"):
            Located.objects().values("display").annotate(display_name=Count())._compile_group_by_query()

    @pytest.mark.asyncio
    async def test_validation_fallback_is_keyed_by_python_names(self) -> None:
        """A projection missing a required field fails model validation and falls back to raw
        dicts — which must speak the same vocabulary as the GROUP BY branch."""

        class Strict(BaseSurrealModel):
            id: str
            label: str = Field(alias="label_col")
            required: int

        async with orm_client("Strict") as client:
            await client.query("CREATE Strict:a SET label_col = 'x', required = 1;", {})
            rows = await Strict.objects().select("label").exec()
            assert rows == [{"label": "x"}]


class TestMergeUsesTheReturnedRow:
    """``client.merge()`` already returns the merged row; re-reading it was a self-inflicted
    second round-trip."""

    @pytest.mark.asyncio
    async def test_default_merge_issues_no_select(self) -> None:
        async with orm_client() as client:
            instance = await Aliased(id="ada", password="secret").save()
            selects = _spy(client, "select")
            await instance.merge(password="rotated")
            assert selects == []
            assert instance.password == "rotated"

    @pytest.mark.asyncio
    async def test_default_merge_hydrates_server_side_changes(self) -> None:
        async with orm_client("Stamped2") as client:
            await client.query(
                "DEFINE FIELD title ON Stamped2 TYPE string VALUE string::uppercase($value);",
                {},
            )
            instance = await Stamped2(id="a", title="first").save()
            await instance.merge(title="second")
            assert instance.title == "SECOND"

    @pytest.mark.asyncio
    async def test_default_merge_on_a_missing_record_raises(self) -> None:
        async with orm_client() as client:
            ghost = Aliased(id="ghost")
            with pytest.raises(SurrealDbError, match="no record found"):
                await ghost.merge(password="x")  # table never created
            await Aliased(id="real").save()
            with pytest.raises(SurrealDbError, match="no record found"):
                await ghost.merge(password="x")  # table exists, record does not
            assert await client.query("SELECT * FROM Aliased:ghost;", {}) == []

    @pytest.mark.asyncio
    async def test_refresh_false_asks_for_no_row(self) -> None:
        async with orm_client() as client:
            instance = await Aliased(id="ada", password="secret").save()
            queries = _spy(client, "query")
            merges = _spy(client, "merge")
            await instance.merge(password="rotated", refresh=False)
            assert merges == []
            assert any("RETURN NONE" in call[0][0] for call in queries if call[0])
            assert (await _raw_row(client, "Aliased", "ada"))["password_hash"] == "rotated"

    @pytest.mark.asyncio
    async def test_interactive_tx_merge_on_a_missing_record_raises(self) -> None:
        async with orm_client() as client:
            await Aliased(id="real").save()
            error: Exception | None = None
            interactive = False
            try:
                async with SurrealDBConnectionManager.transaction() as tx:
                    interactive = tx.is_interactive
                    await Aliased(id="ghost").merge(tx=tx, password="x")
            except SurrealDbError as exc:
                error = exc
            if interactive:
                assert error is not None and "no record found" in str(error)
            else:
                assert error is None
            assert await client.query("SELECT * FROM Aliased:ghost;", {}) == []
