"""Tests for v0.14.0 computed fields: the ``Computed`` type, DDL emission/application,
write exclusion, and the write guards."""

import contextlib
import os
from typing import Annotated, Any, get_args, get_origin

import pytest

from surreal_orm_lite import BaseSurrealModel, SurrealDBConnectionManager
from surreal_orm_lite.exceptions import SurrealDbError
from surreal_orm_lite.functions import Computed, SurrealFunc, _ComputedDefault, _ComputedMarker


class TestComputedType:
    def test_subscript_builds_annotated_optional(self) -> None:
        annotation = Computed[str]
        assert get_origin(annotation) is Annotated
        args = get_args(annotation)
        assert args[0] == (str | None)
        assert isinstance(args[1], _ComputedMarker)
        assert args[1].inner_type is str

    def test_each_subscript_gets_its_own_marker(self) -> None:
        assert get_args(Computed[str])[1] is not get_args(Computed[int])[1]

    def test_call_builds_sentinel_from_string(self) -> None:
        default = Computed("string::concat(first_name, ' ', last_name)")
        assert isinstance(default, _ComputedDefault)
        assert default.expression == "string::concat(first_name, ' ', last_name)"

    def test_call_accepts_a_surreal_func(self) -> None:
        assert Computed(SurrealFunc("time::now()")).expression == "time::now()"

    def test_expression_is_stripped(self) -> None:
        assert Computed("  time::now()  ").expression == "time::now()"

    def test_rejects_empty_expression(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            Computed("")

    def test_rejects_statement_terminator(self) -> None:
        with pytest.raises(ValueError, match="';'"):
            Computed("time::now(); REMOVE TABLE User")

    def test_rejects_non_string_expression(self) -> None:
        with pytest.raises(TypeError):
            Computed(42)  # type: ignore[arg-type]

    def test_repr_round_trips(self) -> None:
        assert repr(Computed("time::now()")) == "Computed('time::now()')"


class Plain(BaseSurrealModel):
    id: str
    name: str = ""


class Person(BaseSurrealModel):
    id: str
    first_name: str = ""
    last_name: str = ""
    full_name: Computed[str] = Computed("string::concat(first_name, ' ', last_name)")
    name_len: Computed[int] = Computed(SurrealFunc("string::len(full_name)"))
    plain_style: str | None = Computed("string::uppercase(first_name)")


class PersonChild(Person):
    id: str
    nickname: str = ""
    shout: Computed[str] = Computed("string::uppercase(nickname)")


class TestGetComputedFields:
    def test_plain_model_has_none(self) -> None:
        assert Plain.get_computed_fields() == {}

    def test_collects_in_declaration_order(self) -> None:
        assert list(Person.get_computed_fields()) == ["full_name", "name_len", "plain_style"]

    def test_records_expressions(self) -> None:
        computed = Person.get_computed_fields()
        assert computed["full_name"] == "string::concat(first_name, ' ', last_name)"
        assert computed["name_len"] == "string::len(full_name)"
        assert computed["plain_style"] == "string::uppercase(first_name)"

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
        assert "Computed" in surreal_orm_lite.__all__


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
    full_name: Computed[str] = Computed("string::concat(first_name, ' ', last_name)")


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
                oops: Computed[str] = Computed("nope::missing(x)")

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
    full_name: Computed[str] = Computed("string::concat(first_name, ' ', last_name)")
    tag_count: Computed[int] = Computed("array::len(tags)")


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

    def test_plain_model_is_unaffected(self) -> None:
        Plain._reject_computed_writes(["name", "anything"], "merge()")


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
