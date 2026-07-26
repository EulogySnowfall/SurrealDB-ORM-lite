"""Tests for v0.13.0 server-side computation: SurrealFunc, the curated function-name
enums, the SET-clause compiler, and ``server_values=`` / ``extra_vars=`` on save/merge."""

import contextlib
import os
from datetime import datetime
from typing import Any

import pytest

from surreal_orm_lite import (
    BaseSurrealModel,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    post_save,
    post_update,
    pre_save,
    pre_update,
)
from surreal_orm_lite.exceptions import SurrealDbError
from surreal_orm_lite.functions import (
    SurrealArrayFunction,
    SurrealCryptoFunction,
    SurrealFunc,
    SurrealFunction,
    SurrealMathFunction,
    SurrealRandFunction,
    SurrealStringFunction,
    SurrealTimeFunction,
)
from surreal_orm_lite.utils import build_set_clause, merge_extra_vars

ALL_FUNCTION_ENUMS: list[type[SurrealFunction]] = [
    SurrealTimeFunction,
    SurrealMathFunction,
    SurrealStringFunction,
    SurrealArrayFunction,
    SurrealCryptoFunction,
    SurrealRandFunction,
]

# One sample call per catalogued member: raw SurrealQL argument fragments that make the
# call valid. Every entry is executed against BOTH 2.6.5 and 3.1.3 by the E2E test below,
# which is what makes the catalog a dual-line guarantee rather than a claim.
ENUM_SAMPLE_ARGS: dict[SurrealFunction, tuple[str, ...]] = {
    # time::
    SurrealTimeFunction.NOW: (),
    SurrealTimeFunction.CEIL: ("time::now()", "1w"),
    SurrealTimeFunction.FLOOR: ("time::now()", "1w"),
    SurrealTimeFunction.ROUND: ("time::now()", "1w"),
    SurrealTimeFunction.GROUP: ("time::now()", "'day'"),
    SurrealTimeFunction.UNIX: ("time::now()",),
    SurrealTimeFunction.DAY: ("time::now()",),
    SurrealTimeFunction.MONTH: ("time::now()",),
    SurrealTimeFunction.YEAR: ("time::now()",),
    SurrealTimeFunction.HOUR: ("time::now()",),
    SurrealTimeFunction.MINUTE: ("time::now()",),
    SurrealTimeFunction.SECOND: ("time::now()",),
    SurrealTimeFunction.WDAY: ("time::now()",),
    SurrealTimeFunction.WEEK: ("time::now()",),
    SurrealTimeFunction.YDAY: ("time::now()",),
    SurrealTimeFunction.MIN: ("[time::now()]",),
    SurrealTimeFunction.MAX: ("[time::now()]",),
    # math::
    SurrealMathFunction.ABS: ("-1",),
    SurrealMathFunction.CEIL: ("1.2",),
    SurrealMathFunction.FLOOR: ("1.2",),
    SurrealMathFunction.ROUND: ("1.5",),
    SurrealMathFunction.SQRT: ("4",),
    SurrealMathFunction.POW: ("2", "3"),
    SurrealMathFunction.MEAN: ("[1, 2]",),
    SurrealMathFunction.MEDIAN: ("[1, 2, 3]",),
    SurrealMathFunction.SUM: ("[1, 2]",),
    SurrealMathFunction.PRODUCT: ("[2, 3]",),
    SurrealMathFunction.MIN: ("[1, 2]",),
    SurrealMathFunction.MAX: ("[1, 2]",),
    SurrealMathFunction.FIXED: ("1.234", "2"),
    SurrealMathFunction.STDDEV: ("[1, 2, 3]",),
    SurrealMathFunction.VARIANCE: ("[1, 2, 3]",),
    # string::
    SurrealStringFunction.CONCAT: ("'a'", "'b'"),
    SurrealStringFunction.LOWERCASE: ("'A'",),
    SurrealStringFunction.UPPERCASE: ("'a'",),
    SurrealStringFunction.TRIM: ("' a '",),
    SurrealStringFunction.LEN: ("'abc'",),
    SurrealStringFunction.SLUG: ("'Hello World'",),
    SurrealStringFunction.REPLACE: ("'aa'", "'a'", "'b'"),
    SurrealStringFunction.REVERSE: ("'abc'",),
    SurrealStringFunction.SPLIT: ("'a,b'", "','"),
    SurrealStringFunction.JOIN: ("','", "['a', 'b']"),
    SurrealStringFunction.STARTS_WITH: ("'abc'", "'a'"),
    SurrealStringFunction.ENDS_WITH: ("'abc'", "'c'"),
    SurrealStringFunction.CONTAINS: ("'abc'", "'b'"),
    SurrealStringFunction.REPEAT: ("'a'", "3"),
    # array::
    SurrealArrayFunction.APPEND: ("[]", "1"),
    SurrealArrayFunction.CONCAT: ("[1]", "[2]"),
    SurrealArrayFunction.ADD: ("[1]", "2"),
    SurrealArrayFunction.COMPLEMENT: ("[1, 2]", "[2]"),
    SurrealArrayFunction.DISTINCT: ("[1, 1]",),
    SurrealArrayFunction.LEN: ("[1]",),
    SurrealArrayFunction.REVERSE: ("[1, 2]",),
    SurrealArrayFunction.SORT: ("[2, 1]",),
    SurrealArrayFunction.FLATTEN: ("[[1], [2]]",),
    SurrealArrayFunction.FIRST: ("[1, 2]",),
    SurrealArrayFunction.LAST: ("[1, 2]",),
    SurrealArrayFunction.MIN: ("[1, 2]",),
    SurrealArrayFunction.MAX: ("[1, 2]",),
    # crypto::
    SurrealCryptoFunction.ARGON2_GENERATE: ("'x'",),
    SurrealCryptoFunction.ARGON2_COMPARE: ("crypto::argon2::generate('x')", "'x'"),
    SurrealCryptoFunction.BCRYPT_GENERATE: ("'x'",),
    SurrealCryptoFunction.BCRYPT_COMPARE: ("crypto::bcrypt::generate('x')", "'x'"),
    SurrealCryptoFunction.SHA256: ("'x'",),
    SurrealCryptoFunction.SHA512: ("'x'",),
    SurrealCryptoFunction.MD5: ("'x'",),
    # rand::
    SurrealRandFunction.RAND: (),
    SurrealRandFunction.UUID: (),
    SurrealRandFunction.UUID_V4: (),
    SurrealRandFunction.UUID_V7: (),
    SurrealRandFunction.ULID: (),
    SurrealRandFunction.BOOL: (),
    SurrealRandFunction.FLOAT: (),
    SurrealRandFunction.INT: (),
    SurrealRandFunction.STRING: ("5",),
    SurrealRandFunction.TIME: (),
    SurrealRandFunction.ENUM: ("'a'", "'b'"),
}


def _url() -> str:
    host = os.environ.get("SURREALDB_HOST", "localhost")
    port = os.environ.get("SURREALDB_PORT", "8000")
    return f"ws://{host}:{port}/rpc"


def _connect() -> None:
    SurrealDBConnectionManager.set_connection(
        url=_url(),
        user="root",
        password="root",
        namespace="ns",
        database="db",
    )


class TestSurrealFunc:
    def test_wraps_and_strips_expression(self) -> None:
        assert SurrealFunc("  time::now()  ").expression == "time::now()"

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            SurrealFunc("")
        with pytest.raises(ValueError, match="empty"):
            SurrealFunc("   ")

    def test_rejects_non_string(self) -> None:
        with pytest.raises(TypeError):
            SurrealFunc(42)  # type: ignore[arg-type]

    def test_rejects_statement_terminator(self) -> None:
        with pytest.raises(ValueError, match=";"):
            SurrealFunc("time::now(); REMOVE TABLE User")

    def test_call_renders_name_and_args(self) -> None:
        assert SurrealFunc.call("time::now").expression == "time::now()"
        assert SurrealFunc.call("crypto::argon2::generate", "$password").expression == "crypto::argon2::generate($password)"
        assert SurrealFunc.call("math::pow", "2", "3").expression == "math::pow(2, 3)"

    def test_repr_eq_hash(self) -> None:
        a, b = SurrealFunc("time::now()"), SurrealFunc("time::now()")
        assert a == b
        assert hash(a) == hash(b)
        assert len({a, b}) == 1
        assert "time::now()" in repr(a)
        assert a != SurrealFunc("rand()")
        assert (a == "time::now()") is False


class TestFunctionEnums:
    def test_members_are_plain_function_names(self) -> None:
        assert SurrealTimeFunction.NOW == "time::now"
        assert SurrealMathFunction.POW == "math::pow"
        assert SurrealStringFunction.CONCAT == "string::concat"
        assert SurrealArrayFunction.APPEND == "array::append"
        assert SurrealCryptoFunction.ARGON2_GENERATE == "crypto::argon2::generate"
        assert SurrealRandFunction.RAND == "rand"
        assert SurrealRandFunction.UUID_V7 == "rand::uuid::v7"

    def test_all_enums_subclass_the_base(self) -> None:
        for enum_cls in ALL_FUNCTION_ENUMS:
            assert issubclass(enum_cls, SurrealFunction)

    def test_call_accepts_enum_members(self) -> None:
        assert SurrealFunc.call(SurrealTimeFunction.NOW).expression == "time::now()"
        assert (
            SurrealFunc.call(SurrealCryptoFunction.ARGON2_GENERATE, "$password").expression
            == "crypto::argon2::generate($password)"
        )

    def test_catalog_is_exhaustively_sampled(self) -> None:
        # Any member added to an enum must gain sample args, so the E2E test below
        # keeps proving the WHOLE catalog resolves on both server lines.
        missing = [f"{type(m).__name__}.{m.name}" for e in ALL_FUNCTION_ENUMS for m in e if m not in ENUM_SAMPLE_ARGS]
        assert missing == []

    def test_divergent_functions_are_excluded(self) -> None:
        # rand::guid is 2.6-only; type::is::* was renamed to type::is_* in 3.x.
        values = {str(m) for e in ALL_FUNCTION_ENUMS for m in e}
        assert "rand::guid" not in values
        assert not any(v.startswith("type::is") for v in values)


class TestBuildSetClause:
    def test_binds_values_and_inlines_funcs(self) -> None:
        clause, variables = build_set_clause({"name": "Alice", "joined": SurrealFunc("time::now()")})
        assert clause == "name = $_sv_name, joined = time::now()"
        assert variables == {"_sv_name": "Alice"}

    def test_injection_shaped_value_stays_bound(self) -> None:
        evil = "x; REMOVE TABLE User;"
        clause, variables = build_set_clause({"bio": evil})
        assert clause == "bio = $_sv_bio"
        assert evil not in clause
        assert variables == {"_sv_bio": evil}

    def test_empty_mapping_yields_empty_clause(self) -> None:
        assert build_set_clause({}) == ("", {})

    def test_invalid_key_raises(self) -> None:
        for bad in ("a b", "a;b", "a.b", "1a", "", "a-b"):
            with pytest.raises(ValueError):
                build_set_clause({bad: 1})

    def test_custom_prefix(self) -> None:
        clause, variables = build_set_clause({"n": 1}, param_prefix="_x_")
        assert clause == "n = $_x_n"
        assert variables == {"_x_n": 1}


class TestMergeExtraVars:
    def test_merges_extra_vars(self) -> None:
        variables = {"_sv_name": "A", "rid": "r"}
        merged = merge_extra_vars(variables, {"password": "s3cret"})
        assert merged["password"] == "s3cret"
        assert merged["_sv_name"] == "A"

    def test_collision_with_internal_binding_raises(self) -> None:
        variables = {"_sv_name": "A", "rid": "r"}
        with pytest.raises(ValueError, match="rid"):
            merge_extra_vars(variables, {"rid": "boom"})
        with pytest.raises(ValueError, match="_sv_name"):
            merge_extra_vars(variables, {"_sv_name": "boom"})

    def test_collision_message_lists_all_offenders(self) -> None:
        with pytest.raises(ValueError, match="_sv_name.*rid|rid.*_sv_name"):
            merge_extra_vars({"_sv_name": "A", "rid": "r"}, {"rid": 1, "_sv_name": 2})

    def test_none_or_empty_is_a_no_op(self) -> None:
        variables = {"_sv_name": "A"}
        assert merge_extra_vars(variables, None) == {"_sv_name": "A"}
        assert merge_extra_vars(variables, {}) == {"_sv_name": "A"}


class TestFunctionEnumsE2E:
    """Every catalogued function must resolve on the connected server.

    Run by the dual-DB harness against 3.1.3 (:8001) AND 2.6.5 (:8002) — that is what
    backs the "verified on both lines" claim in the docs.
    """

    @pytest.mark.asyncio
    async def test_every_catalogued_member_resolves(self) -> None:
        _connect()
        client = await SurrealDBConnectionManager.get_client()
        try:
            for member, args in ENUM_SAMPLE_ARGS.items():
                expr = SurrealFunc.call(member, *args).expression
                # Raises if the server does not know the function / signature.
                await client.query(f"RETURN {expr};", {})
        finally:
            await SurrealDBConnectionManager.close_connection()


# --------------------------------------------------------------------------- #
# server_values= / extra_vars= on save() and merge()
# --------------------------------------------------------------------------- #


class SvUser(BaseSurrealModel):
    model_config = SurrealConfigDict(primary_key="id")
    id: str | None = None
    name: str = ""
    plan: str = "free"
    bio: str = ""
    joined_at: Any = None
    updated_at: Any = None
    password_hash: str = ""


@contextlib.asynccontextmanager
async def sv_client():
    """Connected ORM client with the SvUser table cleared before and after.

    An async context manager rather than a pytest fixture: the SDK's WebSocket client is
    bound to the event loop that created it, and fixtures run in the module-scoped loop
    while tests get their own — so the client must be opened inside the test's loop.
    """
    _connect()
    client = await SurrealDBConnectionManager.get_client()
    with contextlib.suppress(Exception):
        await client.query("REMOVE TABLE SvUser;", {})
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            await client.query("REMOVE TABLE SvUser;", {})
        await SurrealDBConnectionManager.close_connection()


class TestSaveServerValuesE2E:
    @pytest.mark.asyncio
    async def test_server_function_is_evaluated_server_side(self) -> None:
        async with sv_client() as client:
            user = SvUser(id="alice", name="Alice")
            await user.save(server_values={"joined_at": SurrealFunc.call(SurrealTimeFunction.NOW)})

            # The instance is synced with the row the server returned…
            assert isinstance(user.joined_at, datetime)
            # …and the stored value is a real datetime, not the literal expression string.
            rows = await client.query("SELECT * FROM SvUser:alice;", {})
            assert isinstance(rows[0]["joined_at"], datetime)
            assert rows[0]["name"] == "Alice"

    @pytest.mark.asyncio
    async def test_duplicate_id_raises_already_exists(self) -> None:
        async with sv_client():
            await SvUser(id="dup", name="A").save(server_values={"joined_at": SurrealFunc("time::now()")})
            with pytest.raises(SurrealDbError, match="already exists"):
                await SvUser(id="dup", name="B").save(server_values={"joined_at": SurrealFunc("time::now()")})

    @pytest.mark.asyncio
    async def test_auto_generated_id_with_server_values(self) -> None:
        async with sv_client():
            user = SvUser(name="NoId")
            await user.save(server_values={"joined_at": SurrealFunc("time::now()")})
            assert user.id is not None
            assert isinstance(user.joined_at, datetime)

    @pytest.mark.asyncio
    async def test_extra_vars_are_bound_not_interpolated(self) -> None:
        async with sv_client() as client:
            raw_password = "hunter2-s3cret"
            user = SvUser(id="bob", name="Bob")
            await user.save(
                server_values={"password_hash": SurrealFunc.call(SurrealCryptoFunction.ARGON2_GENERATE, "$password")},
                extra_vars={"password": raw_password},
            )
            assert user.password_hash.startswith("$argon2")

            # The hash verifies server-side against the original password…
            ok = await client.query(
                "RETURN crypto::argon2::compare($h, $p);",
                {"h": user.password_hash, "p": raw_password},
            )
            assert ok is True
            # …and the raw password was never stored.
            rows = await client.query("SELECT * FROM SvUser:bob;", {})
            assert raw_password not in str(rows[0])

    @pytest.mark.asyncio
    async def test_injection_shaped_field_value_stays_literal(self) -> None:
        async with sv_client() as client:
            evil = "'; REMOVE TABLE SvUser; --"
            user = SvUser(id="evil", name="E", bio=evil)
            await user.save(server_values={"joined_at": SurrealFunc("time::now()")})

            rows = await client.query("SELECT * FROM SvUser:evil;", {})
            assert rows[0]["bio"] == evil  # stored verbatim, table intact

    @pytest.mark.asyncio
    async def test_signals_fire_around_server_values_save(self) -> None:
        seen: list[str] = []

        @pre_save.connect(SvUser)
        async def _pre(sender, instance, **kw) -> None:
            seen.append("pre")

        @post_save.connect(SvUser)
        async def _post(sender, instance, **kw) -> None:
            seen.append("post")

        try:
            async with sv_client():
                await SvUser(id="sig", name="S").save(server_values={"joined_at": SurrealFunc("time::now()")})
            assert seen == ["pre", "post"]
        finally:
            pre_save.clear(SvUser)
            post_save.clear(SvUser)


class TestMergeServerValuesE2E:
    @pytest.mark.asyncio
    async def test_merge_is_partial_and_syncs_instance(self) -> None:
        async with sv_client() as client:
            user = SvUser(id="carol", name="Carol", plan="free", bio="hello")
            await user.save()

            await user.merge(plan="pro", server_values={"updated_at": SurrealFunc.call(SurrealTimeFunction.NOW)})

            # Listed fields changed (bound + server-computed), instance synced without refresh()…
            assert user.plan == "pro"
            assert isinstance(user.updated_at, datetime)
            # …and the unlisted field survived (MERGE semantics, not REPLACE).
            rows = await client.query("SELECT * FROM SvUser:carol;", {})
            assert rows[0]["plan"] == "pro"
            assert rows[0]["bio"] == "hello"
            assert rows[0]["name"] == "Carol"
            assert isinstance(rows[0]["updated_at"], datetime)

    @pytest.mark.asyncio
    async def test_server_value_overrides_same_named_kwarg(self) -> None:
        async with sv_client() as client:
            user = SvUser(id="dave", name="Dave")
            await user.save()

            await user.merge(
                updated_at="literal-string",
                server_values={"updated_at": SurrealFunc("time::now()")},
            )
            rows = await client.query("SELECT * FROM SvUser:dave;", {})
            assert isinstance(rows[0]["updated_at"], datetime)  # the func won

    @pytest.mark.asyncio
    async def test_merge_with_only_server_values(self) -> None:
        async with sv_client() as client:
            user = SvUser(id="erin", name="Erin")
            await user.save()

            await user.merge(server_values={"updated_at": SurrealFunc("time::now()")})
            rows = await client.query("SELECT * FROM SvUser:erin;", {})
            assert isinstance(rows[0]["updated_at"], datetime)
            assert rows[0]["name"] == "Erin"

    @pytest.mark.asyncio
    async def test_merge_extra_vars_are_bound(self) -> None:
        async with sv_client() as client:
            user = SvUser(id="frank", name="Frank")
            await user.save()

            raw_password = "p@ssw0rd-merge"
            await user.merge(
                server_values={"password_hash": SurrealFunc.call(SurrealCryptoFunction.ARGON2_GENERATE, "$password")},
                extra_vars={"password": raw_password},
            )
            assert user.password_hash.startswith("$argon2")
            ok = await client.query(
                "RETURN crypto::argon2::compare($h, $p);",
                {"h": user.password_hash, "p": raw_password},
            )
            assert ok is True

    @pytest.mark.asyncio
    async def test_update_signals_carry_all_merged_fields(self) -> None:
        seen: list[list[str]] = []

        @pre_update.connect(SvUser)
        async def _pre(sender, instance, update_fields=None, **kw) -> None:
            seen.append(sorted(update_fields or []))

        @post_update.connect(SvUser)
        async def _post(sender, instance, update_fields=None, **kw) -> None:
            seen.append(sorted(update_fields or []))

        try:
            async with sv_client():
                user = SvUser(id="gina", name="Gina")
                await user.save()
                await user.merge(plan="pro", server_values={"updated_at": SurrealFunc("time::now()")})
            assert seen == [["plan", "updated_at"], ["plan", "updated_at"]]
        finally:
            pre_update.clear(SvUser)
            post_update.clear(SvUser)


class TestServerValuesTransactionE2E:
    """Transaction semantics are inherited from v0.9.0, not introduced here.

    Both strategies commit the same DB state; they differ only in when the instance can
    know a server-computed value (interactive: at ``add()`` time — buffered: not before
    commit). Each test asserts the shared contract on both lines and gates only the
    strategy-specific half.
    """

    @pytest.mark.asyncio
    async def test_save_in_transaction_commits_computed_value(self) -> None:
        async with sv_client() as client:
            async with SurrealDBConnectionManager.transaction() as tx:
                user = SvUser(id="txa", name="TxA")
                await user.save(tx=tx, server_values={"joined_at": SurrealFunc("time::now()")})
                interactive = tx.is_interactive
                if interactive:
                    # Interactive: the row comes back from add(), so the instance knows it.
                    assert isinstance(user.joined_at, datetime)
                else:
                    # Buffered: the statement has not run yet — the field stays stale.
                    assert user.joined_at is None

            rows = await client.query("SELECT * FROM SvUser:txa;", {})
            assert isinstance(rows[0]["joined_at"], datetime)  # same DB state either way
            assert rows[0]["name"] == "TxA"

    @pytest.mark.asyncio
    async def test_rollback_leaves_no_record(self) -> None:
        async with sv_client():
            with contextlib.suppress(RuntimeError):
                async with SurrealDBConnectionManager.transaction() as tx:
                    await SvUser(id="txroll", name="Rollback").save(
                        tx=tx, server_values={"joined_at": SurrealFunc("time::now()")}
                    )
                    raise RuntimeError("abort the transaction")

            # Read through the ORM: it honours the "missing table = empty" contract, which a
            # raw SELECT does not on SurrealDB 3.x (it raises NotFound instead).
            assert await SvUser.objects().count() == 0

    @pytest.mark.asyncio
    async def test_merge_in_transaction(self) -> None:
        async with sv_client() as client:
            user = SvUser(id="txm", name="TxM", plan="free")
            await user.save()

            async with SurrealDBConnectionManager.transaction() as tx:
                await user.merge(tx=tx, plan="pro", server_values={"updated_at": SurrealFunc("time::now()")})
                # The literal kwarg is applied to the instance immediately on both strategies.
                assert user.plan == "pro"
                if not tx.is_interactive:
                    assert user.updated_at is None  # computed value unknown until commit

            rows = await client.query("SELECT * FROM SvUser:txm;", {})
            assert rows[0]["plan"] == "pro"
            assert isinstance(rows[0]["updated_at"], datetime)
            assert rows[0]["name"] == "TxM"  # untouched field preserved

    @pytest.mark.asyncio
    async def test_auto_id_in_transaction_matches_strategy(self) -> None:
        async with sv_client() as client:
            async with SurrealDBConnectionManager.transaction() as tx:
                user = SvUser(name="TxAuto")
                if tx.is_interactive:
                    await user.save(tx=tx, server_values={"joined_at": SurrealFunc("time::now()")})
                    assert user.id is not None
                else:
                    # Buffered transactions cannot allocate an id before commit (v0.8.0 rule).
                    with pytest.raises(SurrealDbError, match="explicit id"):
                        await user.save(tx=tx, server_values={"joined_at": SurrealFunc("time::now()")})

            rows = await client.query("SELECT * FROM SvUser WHERE name = 'TxAuto';", {})
            assert len(rows) == (1 if user.id is not None else 0)


class TestServerValuesValidation:
    @pytest.mark.asyncio
    async def test_non_surrealfunc_value_raises_type_error(self) -> None:
        with pytest.raises(TypeError, match="SurrealFunc"):
            await SvUser(id="x", name="X").save(server_values={"joined_at": "time::now()"})  # type: ignore[dict-item]
        with pytest.raises(TypeError, match="SurrealFunc"):
            await SvUser(id="x", name="X").merge(server_values={"updated_at": 42})  # type: ignore[dict-item]

    @pytest.mark.asyncio
    async def test_extra_vars_without_server_values_raises(self) -> None:
        with pytest.raises(ValueError, match="extra_vars"):
            await SvUser(id="x", name="X").save(extra_vars={"password": "p"})
        with pytest.raises(ValueError, match="extra_vars"):
            await SvUser(id="x", name="X").merge(plan="pro", extra_vars={"password": "p"})

    @pytest.mark.asyncio
    async def test_invalid_server_values_key_raises(self) -> None:
        with pytest.raises(ValueError):
            await SvUser(id="x", name="X").save(server_values={"bad key": SurrealFunc("time::now()")})
        with pytest.raises(ValueError):
            await SvUser(id="x", name="X").merge(server_values={"bad key": SurrealFunc("time::now()")})

    @pytest.mark.asyncio
    async def test_extra_vars_colliding_with_internal_binding_raises(self) -> None:
        with pytest.raises(ValueError, match="collide"):
            await SvUser(id="x", name="X").save(
                server_values={"joined_at": SurrealFunc("time::now()")},
                extra_vars={"rid": "boom"},
            )
        with pytest.raises(ValueError, match="collide"):
            await SvUser(id="x", name="X").save(
                server_values={"joined_at": SurrealFunc("time::now()")},
                extra_vars={"_sv_name": "boom"},
            )
