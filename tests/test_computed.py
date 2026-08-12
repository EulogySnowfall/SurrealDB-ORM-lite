"""Tests for v0.14.0 computed fields: the ``Computed`` type, DDL emission/application,
write exclusion, and the write guards."""

import contextlib
import os
import warnings
from typing import Annotated, Any, get_args, get_origin

import pytest
from pydantic import BaseModel as PydanticBaseModel

from surreal_orm_lite import BaseSurrealModel, SurrealDBConnectionManager
from surreal_orm_lite.exceptions import SurrealDbError
from surreal_orm_lite.functions import Computed, SurrealFunc, _ComputedDefault, _ComputedMarker, computed


class TestComputedType:
    def test_subscript_builds_annotated_optional(self) -> None:
        annotation = Computed[str]
        assert get_origin(annotation) is Annotated
        args = get_args(annotation)
        assert args[0] == (str | None)
        assert isinstance(args[1], _ComputedMarker)

    def test_every_subscript_shares_the_one_marker(self) -> None:
        """``Computed`` is a generic alias, so its metadata is fixed once, not per subscript."""
        assert get_args(Computed[str])[1] is get_args(Computed[int])[1]

    def test_subscript_substitutes_the_inner_type(self) -> None:
        assert get_args(Computed[int])[0] == (int | None)

    def test_call_builds_sentinel_from_string(self) -> None:
        default = computed("string::concat(first_name, ' ', last_name)")
        assert isinstance(default, _ComputedDefault)
        assert default.expression == "string::concat(first_name, ' ', last_name)"

    def test_call_accepts_a_surreal_func(self) -> None:
        assert computed(SurrealFunc("time::now()")).expression == "time::now()"

    def test_expression_is_stripped(self) -> None:
        assert computed("  time::now()  ").expression == "time::now()"

    def test_rejects_empty_expression(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            computed("")

    def test_rejects_statement_terminator(self) -> None:
        with pytest.raises(ValueError, match="';'"):
            computed("time::now(); REMOVE TABLE User")

    def test_rejects_non_string_expression(self) -> None:
        with pytest.raises(TypeError):
            computed(42)  # type: ignore[arg-type]

    def test_repr_round_trips(self) -> None:
        assert repr(computed("time::now()")) == "computed('time::now()')"


class Plain(BaseSurrealModel):
    id: str
    name: str = ""


class Person(BaseSurrealModel):
    id: str
    first_name: str = ""
    last_name: str = ""
    full_name: Computed[str] = computed("string::concat(first_name, ' ', last_name)")
    name_len: Computed[int] = computed(SurrealFunc("string::len(full_name)"))
    plain_style: str | None = computed("string::uppercase(first_name)")


class PersonChild(Person):
    id: str
    nickname: str = ""
    shout: Computed[str] = computed("string::uppercase(nickname)")


class TestGetComputedFields:
    def test_plain_model_has_none(self) -> None:
        assert Plain.get_computed_fields() == {}

    def test_collects_in_declaration_order(self) -> None:
        assert list(Person.get_computed_fields()) == ["full_name", "name_len", "plain_style"]

    def test_records_expressions(self) -> None:
        expressions = Person.get_computed_fields()
        assert expressions["full_name"] == "string::concat(first_name, ' ', last_name)"
        assert expressions["name_len"] == "string::len(full_name)"
        assert expressions["plain_style"] == "string::uppercase(first_name)"

    def test_returns_a_copy(self) -> None:
        Person.get_computed_fields()["injected"] = "boom"
        assert "injected" not in Person.get_computed_fields()

    def test_subclass_inherits_and_extends(self) -> None:
        assert Person.get_computed_fields().keys() <= PersonChild.get_computed_fields().keys()
        assert PersonChild.get_computed_fields()["shout"] == "string::uppercase(nickname)"
        assert "shout" not in Person.get_computed_fields()

    def test_fields_are_nullable_with_none_default(self) -> None:
        for name in ("full_name", "name_len", "plain_style"):
            field = Person.model_fields[name]
            assert field.default is None
            assert not field.is_required()

    def test_instance_constructible_without_computed_values(self) -> None:
        person = Person(id="ada", first_name="Ada", last_name="Lovelace")
        assert person.full_name is None
        assert person.name_len is None

    def test_hydrates_from_a_db_row(self) -> None:
        person = Person(id="ada", first_name="Ada", last_name="Lovelace", full_name="Ada Lovelace", name_len=12)
        assert person.full_name == "Ada Lovelace"
        assert person.name_len == 12

    def test_computed_is_exported_from_the_package(self) -> None:
        import surreal_orm_lite

        assert surreal_orm_lite.Computed is Computed
        assert surreal_orm_lite.computed is computed
        assert "Computed" in surreal_orm_lite.__all__
        assert "computed" in surreal_orm_lite.__all__


class TestComputedFieldDDL:
    def test_plain_model_yields_no_statements(self) -> None:
        assert Plain.computed_field_ddl() == []

    def test_renders_overwrite_by_default(self) -> None:
        assert Person.computed_field_ddl()[0] == (
            "DEFINE FIELD OVERWRITE full_name ON Person VALUE string::concat(first_name, ' ', last_name);"
        )

    def test_renders_if_not_exists_when_asked(self) -> None:
        assert Person.computed_field_ddl(overwrite=False)[0] == (
            "DEFINE FIELD IF NOT EXISTS full_name ON Person VALUE string::concat(first_name, ' ', last_name);"
        )

    def test_one_statement_per_computed_field_in_order(self) -> None:
        statements = Person.computed_field_ddl()
        assert len(statements) == 3
        assert [s.split()[3] for s in statements] == ["full_name", "name_len", "plain_style"]

    def test_uses_the_model_table_name(self) -> None:
        assert all(" ON Person " in s for s in Person.computed_field_ddl())

    def test_subclass_renders_its_own_table(self) -> None:
        assert all(" ON PersonChild " in s for s in PersonChild.computed_field_ddl())

    def test_is_pure_and_repeatable(self) -> None:
        assert Person.computed_field_ddl() == Person.computed_field_ddl()


def _url() -> str:
    host = os.environ.get("SURREALDB_HOST", "localhost")
    port = os.environ.get("SURREALDB_PORT", "8000")
    return f"ws://{host}:{port}/rpc"


def _connect() -> None:
    SurrealDBConnectionManager.set_connection(url=_url(), user="root", password="root", namespace="ns", database="db")


class CfPlayer(BaseSurrealModel):
    id: str
    first_name: str = ""
    last_name: str = ""
    full_name: Computed[str] = computed("string::concat(first_name, ' ', last_name)")


@contextlib.asynccontextmanager
async def cf_client(*tables: str):
    """Connected ORM client with the given tables dropped before and after.

    An async context manager rather than a pytest fixture: the SDK's WebSocket client is
    bound to the event loop that created it, and fixtures run in the module-scoped loop
    while tests get their own — so the client must be opened inside the test's loop.
    Dropping the table also drops its field definitions, which is what isolates DDL tests.
    """
    tables = tables or ("CfPlayer",)
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


async def _field_definitions(client: Any, table: str) -> dict[str, str]:
    info = await client.query(f"INFO FOR TABLE {table};", {})
    return dict(info["fields"]) if isinstance(info, dict) else {}


class TestDefineComputedFieldsE2E:
    @pytest.mark.asyncio
    async def test_defines_the_field_on_the_server(self) -> None:
        async with cf_client() as client:
            statements = await CfPlayer.define_computed_fields()
            assert statements == CfPlayer.computed_field_ddl()
            definitions = await _field_definitions(client, "CfPlayer")
            assert "full_name" in definitions
            assert "string::concat(first_name, ' ', last_name)" in definitions["full_name"]

    @pytest.mark.asyncio
    async def test_is_idempotent(self) -> None:
        async with cf_client():
            await CfPlayer.define_computed_fields()
            await CfPlayer.define_computed_fields()
            await CfPlayer.define_computed_fields()
            player = await CfPlayer(id="a", first_name="Ada", last_name="L").save()
            assert player.full_name == "Ada L"

    @pytest.mark.asyncio
    async def test_overwrite_replaces_an_existing_definition(self) -> None:
        async with cf_client() as client:
            await client.query("DEFINE FIELD full_name ON CfPlayer VALUE 'stale';", {})
            await CfPlayer.define_computed_fields(overwrite=True)
            player = await CfPlayer(id="a", first_name="Ada", last_name="L").save()
            assert player.full_name == "Ada L"

    @pytest.mark.asyncio
    async def test_if_not_exists_keeps_an_existing_definition(self) -> None:
        async with cf_client() as client:
            await client.query("DEFINE FIELD full_name ON CfPlayer VALUE 'kept';", {})
            await CfPlayer.define_computed_fields(overwrite=False)
            player = await CfPlayer(id="a", first_name="Ada", last_name="L").save()
            assert player.full_name == "kept"

    @pytest.mark.asyncio
    async def test_model_without_computed_fields_is_a_no_op(self) -> None:
        async with cf_client("CfNoComputed"):

            class CfNoComputed(BaseSurrealModel):
                id: str
                name: str = ""

            assert await CfNoComputed.define_computed_fields() == []

    @pytest.mark.asyncio
    async def test_invalid_expression_raises_one_orm_error_on_both_lines(self) -> None:
        """2.6.5 raises InternalError, 3.1.3 raises ValidationError — both normalised here."""
        async with cf_client("CfBroken"):

            class CfBroken(BaseSurrealModel):
                id: str
                oops: Computed[str] = computed("nope::missing(x)")

            with pytest.raises(SurrealDbError, match="DEFINE FIELD"):
                await CfBroken.define_computed_fields()

    @pytest.mark.asyncio
    async def test_save_syncs_the_server_row_without_retyping_an_explicit_id(self) -> None:
        """save() applies what the server stored, but leaves an explicitly-chosen id alone."""
        async with cf_client():
            await CfPlayer.define_computed_fields()
            player = await CfPlayer(id="ada", first_name="Ada", last_name="Lovelace").save()
            assert player.full_name == "Ada Lovelace"
            assert player.id == "ada"
            assert isinstance(player.id, str)


class TestWritePayload:
    def test_drops_id_and_computed_fields(self) -> None:
        person = Person(id="ada", first_name="Ada", last_name="Lovelace")
        assert person._write_payload() == {"first_name": "Ada", "last_name": "Lovelace"}

    def test_drops_computed_even_when_locally_set(self) -> None:
        person = Person(id="ada", first_name="Ada", last_name="L", full_name="STALE")
        assert "full_name" not in person._write_payload()

    def test_plain_model_keeps_everything_but_id(self) -> None:
        assert Plain(id="x", name="n")._write_payload() == {"name": "n"}


class TestComputedLifecycleE2E:
    @pytest.mark.asyncio
    async def test_save_hydrates_the_computed_value(self) -> None:
        async with cf_client():
            await CfPlayer.define_computed_fields()
            player = await CfPlayer(id="ada", first_name="Ada", last_name="Lovelace").save()
            assert player.full_name == "Ada Lovelace"

    @pytest.mark.asyncio
    async def test_recomputed_by_merge_update_upsert_and_patch(self) -> None:
        async with cf_client():
            await CfPlayer.define_computed_fields()
            player = await CfPlayer(id="ada", first_name="Ada", last_name="Lovelace").save()

            await player.merge(last_name="Byron")
            assert player.full_name == "Ada Byron"

            player.last_name = "King"
            await player.update()
            await player.refresh()
            assert player.full_name == "Ada King"

            player.last_name = "Noel"
            await player.upsert()
            assert player.full_name == "Ada Noel"

            await player.patch([{"op": "replace", "path": "/last_name", "value": "Gordon"}])
            assert player.full_name == "Ada Gordon"

    @pytest.mark.asyncio
    async def test_queryset_can_filter_and_order_on_a_computed_field(self) -> None:
        async with cf_client():
            await CfPlayer.define_computed_fields()
            await CfPlayer(id="a", first_name="Ada", last_name="L").save()
            await CfPlayer(id="b", first_name="Bob", last_name="M").save()

            found = await CfPlayer.objects().filter(full_name="Ada L").exec()
            assert [p.id for p in found] == ["a"]

            ordered = await CfPlayer.objects().order_by("-full_name").exec()
            assert [p.full_name for p in ordered] == ["Bob M", "Ada L"]

    @pytest.mark.asyncio
    async def test_bulk_create_excludes_computed_fields(self) -> None:
        async with cf_client():
            await CfPlayer.define_computed_fields()
            created = await CfPlayer.objects().bulk_create(
                [
                    CfPlayer(id="a", first_name="Ada", last_name="L"),
                    CfPlayer(id="b", first_name="Bob", last_name="M"),
                ]
            )
            assert sorted(p.full_name for p in created) == ["Ada L", "Bob M"]

    @pytest.mark.asyncio
    async def test_the_server_wins_over_a_hostile_client_value(self) -> None:
        """A write that bypasses the ORM's exclusion still cannot override the expression."""
        async with cf_client() as client:
            await CfPlayer.define_computed_fields()
            await client.query("CREATE CfPlayer:evil CONTENT {first_name: 'Cy', last_name: 'N', full_name: 'HACKED'};", {})
            rows = await client.query("SELECT * FROM CfPlayer:evil;", {})
            assert rows[0]["full_name"] == "Cy N"

    @pytest.mark.asyncio
    async def test_value_is_null_before_the_ddl_is_applied(self) -> None:
        """Without define_computed_fields() the column is simply never written."""
        async with cf_client():
            player = await CfPlayer(id="ada", first_name="Ada", last_name="Lovelace").save()
            await player.refresh()
            assert player.full_name is None


class CfGuard(BaseSurrealModel):
    id: str
    first_name: str = ""
    last_name: str = ""
    tags: list[str] = []
    score: int = 0
    full_name: Computed[str] = computed("string::concat(first_name, ' ', last_name)")
    tag_count: Computed[int] = computed("array::len(tags)")


class TestComputedWriteGuards:
    @pytest.mark.asyncio
    async def test_merge_rejects_a_computed_field(self) -> None:
        guard = CfGuard(id="a", first_name="Ada")
        with pytest.raises(ValueError, match="full_name"):
            await guard.merge(full_name="Ada Byron")

    @pytest.mark.asyncio
    async def test_merge_reports_every_offender(self) -> None:
        guard = CfGuard(id="a")
        with pytest.raises(ValueError, match="full_name.*tag_count|tag_count.*full_name"):
            await guard.merge(full_name="x", tag_count=3)

    def test_guard_ignores_ordinary_fields(self) -> None:
        """The guard must not fire for a normal field (asserted without a DB round-trip)."""
        CfGuard._reject_computed_writes(["first_name", "last_name", "tags"], "merge()")

    @pytest.mark.asyncio
    async def test_server_values_rejects_a_computed_field(self) -> None:
        guard = CfGuard(id="a")
        with pytest.raises(ValueError, match="full_name"):
            await guard.save(server_values={"full_name": SurrealFunc("time::now()")})

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method,args",
        [
            ("atomic_append", ("tag_count", "x")),
            ("atomic_remove", ("tag_count", "x")),
            ("atomic_set_add", ("tag_count", "x")),
            ("atomic_increment", ("tag_count", 1)),
            ("atomic_append_many", ("tag_count", ["x"])),
            ("atomic_set_add_many", ("tag_count", ["x"])),
            ("atomic_remove_many", ("tag_count", ["x"])),
        ],
    )
    async def test_atomic_helpers_reject_a_computed_field(self, method: str, args: tuple) -> None:
        guard = CfGuard(id="a")
        with pytest.raises(ValueError, match="tag_count"):
            await getattr(guard, method)(*args)

    @pytest.mark.asyncio
    async def test_bulk_update_rejects_a_computed_field(self) -> None:
        with pytest.raises(ValueError, match="full_name"):
            await CfGuard.objects().filter(id="a").bulk_update(full_name="x")

    @pytest.mark.asyncio
    async def test_patch_rejects_a_computed_field(self) -> None:
        guard = CfGuard(id="a")
        with pytest.raises(ValueError, match="full_name"):
            await guard.patch([{"op": "replace", "path": "/full_name", "value": "x"}])

    @pytest.mark.asyncio
    async def test_patch_rejects_a_nested_path_into_a_computed_field(self) -> None:
        """A computed field is server-owned in full: ``/tag_count/0`` is no more writable."""
        guard = CfGuard(id="a")
        with pytest.raises(ValueError, match="tag_count"):
            await guard.patch([{"op": "add", "path": "/tag_count/0", "value": "x"}])

    @pytest.mark.asyncio
    async def test_patch_rejects_a_move_out_of_a_computed_field(self) -> None:
        """``move`` removes its source, so ``from`` is a write too — unlike ``copy``."""
        guard = CfGuard(id="a")
        with pytest.raises(ValueError, match="full_name"):
            await guard.patch([{"op": "move", "from": "/full_name", "path": "/last_name"}])

    @pytest.mark.asyncio
    async def test_queryset_patch_rejects_a_computed_field(self) -> None:
        with pytest.raises(ValueError, match="full_name"):
            await CfGuard.objects().filter(id="a").patch([{"op": "replace", "path": "/full_name", "value": "x"}])

    def test_patch_guard_ignores_ordinary_paths(self) -> None:
        """No DB round-trip: the guard alone must pass an ordinary patch through."""
        CfGuard._reject_computed_patch(
            [
                {"op": "replace", "path": "/first_name", "value": "Ada"},
                {"op": "add", "path": "/tags/0", "value": "x"},
                {"op": "copy", "from": "/full_name", "path": "/last_name"},
            ],
            "patch()",
        )

    @pytest.mark.asyncio
    async def test_patch_rejects_a_whole_document_replace(self) -> None:
        """RFC 6901's empty pointer targets the whole document, computed fields included."""
        guard = CfGuard(id="a")
        with pytest.raises(ValueError, match="full_name"):
            await guard.patch([{"op": "replace", "path": "", "value": {"first_name": "Ada", "full_name": "x"}}])

    def test_whole_document_replace_without_computed_keys_passes(self) -> None:
        """Replacing the document with a value that omits them is a legitimate write."""
        CfGuard._reject_computed_patch(
            [{"op": "replace", "path": "", "value": {"first_name": "Ada"}}],
            "patch()",
        )

    @pytest.mark.asyncio
    async def test_atomic_helpers_reject_a_path_into_a_computed_field(self) -> None:
        """``tag_count.items`` is as server-owned as ``tag_count`` — cf. ``/tag_count/0``."""
        guard = CfGuard(id="a")
        with pytest.raises(ValueError, match="tag_count"):
            await guard.atomic_append("tag_count.items", "x")

    def test_plain_model_is_unaffected(self) -> None:
        Plain._reject_computed_writes(["name", "anything"], "merge()")


class TestComputedSiblingBases:
    """Demotion must work from a sibling base, not just from the subclass body."""

    def test_ordinary_redeclaration_in_a_sibling_base_demotes(self) -> None:
        class HasComputed(BaseSurrealModel):
            id: str
            label: Computed[str] = computed("string::uppercase(id)")

        class Ordinary(BaseSurrealModel):
            label: str = ""

        class Combined(Ordinary, HasComputed):
            pass

        # Ordinary precedes HasComputed in the MRO, so its plain `label` wins — and Pydantic
        # builds it as a writable field. The verdict must agree, or `label` is stripped from
        # every write payload and merge(label=...) raises on a field the user owns.
        assert "label" not in Combined.get_computed_fields()
        Combined._reject_computed_writes(["label"], "merge()")

    def test_computed_base_still_wins_when_it_precedes(self) -> None:
        class HasComputed(BaseSurrealModel):
            id: str
            label: Computed[str] = computed("string::uppercase(id)")

        class Ordinary(BaseSurrealModel):
            label: str = ""

        class Combined(HasComputed, Ordinary):
            pass

        assert Combined.get_computed_fields() == {"label": "string::uppercase(id)"}


class TestComputedGuardsE2E:
    @pytest.mark.asyncio
    async def test_ordinary_writes_still_work_end_to_end(self) -> None:
        async with cf_client("CfGuard"):
            await CfGuard.define_computed_fields()
            guard = await CfGuard(id="a", first_name="Ada", last_name="L", tags=["x"]).save()
            assert guard.full_name == "Ada L"
            assert guard.tag_count == 1

            await guard.atomic_append("tags", "y")
            assert guard.tag_count == 2

            await guard.atomic_increment("score", 5)
            assert guard.score == 5

            # Filter on an ordinary field: `id` holds a RecordID server-side, so comparing it
            # to the bare string "a" matches nothing (unrelated to computed fields).
            assert await CfGuard.objects().filter(first_name="Ada").bulk_update(last_name="Byron") == 1
            await guard.refresh()
            assert guard.full_name == "Ada Byron"


class TestComputedTransactionE2E:
    @pytest.mark.asyncio
    async def test_ddl_applied_inside_a_transaction_takes_effect(self) -> None:
        async with cf_client():
            async with SurrealDBConnectionManager.transaction() as tx:
                await CfPlayer.define_computed_fields(tx=tx)
            player = await CfPlayer(id="ada", first_name="Ada", last_name="Lovelace").save()
            assert player.full_name == "Ada Lovelace"

    @pytest.mark.asyncio
    async def test_rolled_back_ddl_leaves_no_definition(self) -> None:
        async with cf_client():
            with contextlib.suppress(RuntimeError):
                async with SurrealDBConnectionManager.transaction() as tx:
                    await CfPlayer.define_computed_fields(tx=tx)
                    raise RuntimeError("abort")
            player = await CfPlayer(id="ada", first_name="Ada", last_name="Lovelace").save()
            await player.refresh()
            assert player.full_name is None

    @pytest.mark.asyncio
    async def test_writes_in_a_transaction_still_exclude_computed_fields(self) -> None:
        async with cf_client():
            await CfPlayer.define_computed_fields()
            async with SurrealDBConnectionManager.transaction() as tx:
                await CfPlayer(id="ada", first_name="Ada", last_name="Lovelace").save(tx=tx)
            stored = await CfPlayer.objects().filter(first_name="Ada").exec()
            assert stored[0].full_name == "Ada Lovelace"


class Nested(PydanticBaseModel):
    city: str = ""


class CfNested(BaseSurrealModel):
    id: str
    name: str = ""
    addr: Nested = Nested()
    shout: Computed[str] = computed("string::uppercase(name)")


class CfPlainNested(BaseSurrealModel):
    """Same shape, but with no computed field at all."""

    id: str
    name: str = ""
    addr: Nested = Nested()


class TestSaveDoesNotDowngradeNestedModelsE2E:
    """save() must not replace a nested Pydantic model with the raw dict from the server.

    ``_apply_record`` writes with ``object.__setattr__`` (no validation), so applying a whole
    returned row would turn ``addr`` into a ``dict`` — breaking attribute access and the next
    ``model_dump()``. Only server-owned computed fields may be re-applied.
    """

    @pytest.mark.asyncio
    async def test_nested_model_survives_save_on_a_computed_model(self) -> None:
        async with cf_client("CfNested"):
            await CfNested.define_computed_fields()
            obj = await CfNested(id="n1", name="ada", addr=Nested(city="Quebec")).save()
            assert obj.shout == "ADA"  # computed field still synced
            assert isinstance(obj.addr, Nested)
            assert obj.addr.city == "Quebec"

    @pytest.mark.asyncio
    async def test_nested_model_survives_save_without_computed_fields(self) -> None:
        async with cf_client("CfPlainNested"):
            obj = await CfPlainNested(id="n1", name="ada", addr=Nested(city="Quebec")).save()
            assert isinstance(obj.addr, Nested)
            assert obj.addr.city == "Quebec"

    @pytest.mark.asyncio
    async def test_instance_is_still_serialisable_after_save(self) -> None:
        async with cf_client("CfNested"):
            await CfNested.define_computed_fields()
            obj = await CfNested(id="n1", name="ada", addr=Nested(city="Quebec")).save()
            with warnings.catch_warnings():
                warnings.simplefilter("error")  # a serializer warning must not appear
                assert obj.model_dump()["addr"] == {"city": "Quebec"}


class ComputedParent(BaseSurrealModel):
    id: str
    a: str = ""
    c1: Computed[str] = computed("string::uppercase(a)")


class OverridingChild(ComputedParent):
    id: str
    c1: Computed[str] = computed("string::lowercase(a)")


class DemotingChild(ComputedParent):
    id: str
    c1: str = "local"


class TestComputedInheritanceOverrides:
    def test_subclass_can_override_the_expression(self) -> None:
        assert OverridingChild.get_computed_fields()["c1"] == "string::lowercase(a)"
        assert ComputedParent.get_computed_fields()["c1"] == "string::uppercase(a)"

    def test_subclass_can_demote_a_computed_field_to_an_ordinary_one(self) -> None:
        assert "c1" not in DemotingChild.get_computed_fields()

    def test_a_demoted_field_is_writable_again(self) -> None:
        obj = DemotingChild(id="x", c1="hello")
        assert obj._write_payload()["c1"] == "hello"
        DemotingChild._reject_computed_writes(["c1"], "merge()")  # must not raise


class TestOrCreateWithComputedCriteriaE2E:
    """A computed field is filterable, so it can appear in `criteria` — but never be written.

    Without stripping it from the write payload, the same call would succeed when creating and
    raise ValueError when updating, i.e. depend on whether the row already existed.
    """

    @pytest.mark.asyncio
    async def test_update_or_create_is_symmetric_for_a_computed_criterion(self) -> None:
        async with cf_client():
            await CfPlayer.define_computed_fields()
            await CfPlayer(id="a", first_name="Ada", last_name="L").save()

            obj, created = await CfPlayer.objects().update_or_create(defaults={"last_name": "Byron"}, full_name="Ada L")
            assert created is False
            assert obj.full_name == "Ada Byron"

    @pytest.mark.asyncio
    async def test_get_or_create_accepts_a_computed_criterion_when_creating(self) -> None:
        async with cf_client():
            await CfPlayer.define_computed_fields()
            obj, created = await CfPlayer.objects().get_or_create(
                defaults={"id": "grace", "first_name": "Grace", "last_name": "H"}, full_name="Grace H"
            )
            assert created is True
            assert obj.full_name == "Grace H"


class TestComputedForwardReference:
    def test_subscript_accepts_a_forward_reference(self) -> None:
        """Computed["Foo"] must not raise — "Foo" | None would be a TypeError."""
        assert get_origin(Computed["SomeLaterType"]) is Annotated
