"""Tests for v0.13.0 server-side computation: SurrealFunc, the curated function-name
enums, the SET-clause compiler, and ``server_values=`` / ``extra_vars=`` on save/merge."""

import os

import pytest

from surreal_orm_lite import SurrealDBConnectionManager
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
