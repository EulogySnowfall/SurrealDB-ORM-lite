"""Tests for v0.15.0 ``call_function()``: calling custom ``fn::`` stored functions.

The pure helpers (name normalisation, signature parsing, statement building) are unit-tested;
everything that talks to a server lives in a ``…E2E`` class and runs on both DB lines.
"""

import contextlib
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import BaseModel as PydanticBaseModel

from surreal_orm_lite import BaseSurrealModel, SurrealDBConnectionManager
from surreal_orm_lite.exceptions import SurrealDbError, SurrealDbNotFoundError, SurrealDbValidationError
from surreal_orm_lite.functions import (
    build_call_statement,
    normalize_function_name,
    parse_function_parameters,
)


class TestFunctionNameNormalisation:
    def test_adds_the_fn_prefix(self) -> None:
        assert normalize_function_name("greet") == "fn::greet"

    def test_is_idempotent_when_already_prefixed(self) -> None:
        assert normalize_function_name("fn::greet") == "fn::greet"

    def test_keeps_nested_namespaces(self) -> None:
        assert normalize_function_name("fn::billing::total") == "fn::billing::total"
        assert normalize_function_name("billing::total") == "fn::billing::total"

    def test_strips_surrounding_whitespace(self) -> None:
        assert normalize_function_name("  fn::greet  ") == "fn::greet"

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "   ",
            "fn::",
            "fn::x; DROP TABLE user",
            "fn::x()",
            "fn::1bad",
            "fn::a::",
            "fn::a b",
            "fn::a-b",
            "fn::a::::b",
            "fn::$a",
        ],
    )
    def test_rejects_anything_that_is_not_an_identifier_path(self, name: str) -> None:
        with pytest.raises(ValueError):
            normalize_function_name(name)

    def test_a_bare_name_is_just_an_identifier(self) -> None:
        """``"fn"`` is a legal identifier, so it names ``fn::fn`` rather than raising —
        predictable beats clever; a genuinely missing function fails at the server."""
        assert normalize_function_name("fn") == "fn::fn"

    def test_rejects_a_non_string(self) -> None:
        with pytest.raises(TypeError):
            normalize_function_name(42)  # type: ignore[arg-type]


class TestSignatureParsing:
    def test_reads_the_parameter_names_in_declaration_order(self) -> None:
        define = "DEFINE FUNCTION fn::greet($name: string, $times: int) { RETURN 1 } PERMISSIONS FULL"
        assert parse_function_parameters(define) == ("name", "times")

    def test_zero_argument_function(self) -> None:
        assert parse_function_parameters("DEFINE FUNCTION fn::noargs() { RETURN 42 } PERMISSIONS FULL") == ()

    def test_a_type_clause_containing_commas_does_not_split_wrongly(self) -> None:
        define = "DEFINE FUNCTION fn::pick($opts: option<array<string>>, $n: int) { RETURN 1 } PERMISSIONS FULL"
        assert parse_function_parameters(define) == ("opts", "n")

    def test_the_2_6_variant_with_a_trailing_semicolon_parses_identically(self) -> None:
        """SurrealDB 2.6.x keeps the body's trailing ``;``; the parameter list is unchanged."""
        define = "DEFINE FUNCTION fn::greet($name: string, $times: int) { RETURN 1; } PERMISSIONS FULL"
        assert parse_function_parameters(define) == ("name", "times")

    def test_a_body_containing_parentheses_does_not_confuse_the_parser(self) -> None:
        define = "DEFINE FUNCTION fn::f($a: int) { RETURN math::max([$a, (1 + 2)]) } PERMISSIONS FULL"
        assert parse_function_parameters(define) == ("a",)

    def test_nested_namespace_declaration(self) -> None:
        define = "DEFINE FUNCTION fn::billing::total($cart: array) { RETURN 1 } PERMISSIONS FULL"
        assert parse_function_parameters(define) == ("cart",)

    def test_reads_a_backtick_quoted_parameter_name(self) -> None:
        """SurrealDB 2.6.x quotes a name that collides with a reserved word — ``fn::f($by)``
        comes back as ``$`by```, while 3.x leaves it bare. Missing this yields an EMPTY
        signature, so ``params=`` fails on one line only."""
        define = "DEFINE FUNCTION fn::cf_bump($`by`: int) { RETURN 1; } PERMISSIONS FULL"
        assert parse_function_parameters(define) == ("by",)

    def test_reads_a_bracket_quoted_parameter_name(self) -> None:
        define = "DEFINE FUNCTION fn::f($\u27e8order\u27e9: int) { RETURN 1 } PERMISSIONS FULL"
        assert parse_function_parameters(define) == ("order",)

    def test_mixes_quoted_and_bare_names_in_declaration_order(self) -> None:
        define = "DEFINE FUNCTION fn::f($a: int, $`by`: int, $c: int) { RETURN 1 } PERMISSIONS FULL"
        assert parse_function_parameters(define) == ("a", "by", "c")

    def test_rejects_text_that_is_not_a_define_function(self) -> None:
        with pytest.raises(ValueError):
            parse_function_parameters("DEFINE TABLE user SCHEMALESS")


class TestCallStatementBuilding:
    def test_binds_each_argument_positionally(self) -> None:
        statement, variables = build_call_statement("fn::greet", ["ada", 3])
        assert statement == "fn::greet($_fnarg0, $_fnarg1);"
        assert variables == {"_fnarg0": "ada", "_fnarg1": 3}

    def test_zero_arguments(self) -> None:
        assert build_call_statement("fn::noargs", []) == ("fn::noargs();", {})

    def test_never_emits_a_return_keyword(self) -> None:
        """``RETURN`` inside a BEGIN…COMMIT batch silently aborts the transaction (v0.15.0
        design finding), so the ORM only ever emits the bare call form."""
        statement, _ = build_call_statement("fn::greet", ["ada"])
        assert "RETURN" not in statement.upper()

    def test_argument_values_are_never_interpolated_into_the_statement(self) -> None:
        hostile = ");DELETE user;--"
        statement, variables = build_call_statement("fn::greet", [hostile])
        assert hostile not in statement
        assert variables["_fnarg0"] == hostile

    def test_accepts_any_sequence(self) -> None:
        assert build_call_statement("fn::sum", (1, 2))[1] == {"_fnarg0": 1, "_fnarg1": 2}


# ---------------------------------------------------------------------------
# E2E — everything below talks to a real server and runs on both DB lines.
# ---------------------------------------------------------------------------


def _url() -> str:
    host = os.environ.get("SURREALDB_HOST", "localhost")
    port = os.environ.get("SURREALDB_PORT", "8000")
    return f"ws://{host}:{port}/rpc"


def _connect() -> None:
    SurrealDBConnectionManager.set_connection(url=_url(), user="root", password="root", namespace="ns", database="db")


#: The demo functions every E2E class relies on, defined fresh per test.
CF_FIXTURES: dict[str, str] = {
    "cf_greet": "DEFINE FUNCTION OVERWRITE fn::cf_greet($name: string) { RETURN 'hi ' + $name; };",
    "cf_sum": "DEFINE FUNCTION OVERWRITE fn::cf_sum($a: int, $b: int) { RETURN $a + $b; };",
    "cf_noargs": "DEFINE FUNCTION OVERWRITE fn::cf_noargs() { RETURN 42; };",
    "cf_none": "DEFINE FUNCTION OVERWRITE fn::cf_none() { RETURN NONE; };",
    "cf_ns::cf_nested": "DEFINE FUNCTION OVERWRITE fn::cf_ns::cf_nested($x: int) { RETURN $x * 2; };",
}


@contextlib.asynccontextmanager
async def cf_client(*tables: str) -> AsyncIterator[Any]:
    """Connected ORM client with the demo functions defined, dropped again afterwards.

    An async context manager rather than a pytest fixture: the SDK's WebSocket client is bound
    to the event loop that created it, and fixtures run in the module-scoped loop while tests
    get their own — so the client must be opened inside the test's loop.
    """
    _connect()
    client = await SurrealDBConnectionManager.get_client()
    for statement in CF_FIXTURES.values():
        await client.query(statement, {})
    for table in tables:
        with contextlib.suppress(Exception):
            await client.query(f"REMOVE TABLE {table};", {})
    try:
        yield client
    finally:
        for name in CF_FIXTURES:
            with contextlib.suppress(Exception):
                await client.query(f"REMOVE FUNCTION fn::{name};", {})
        for table in tables:
            with contextlib.suppress(Exception):
                await client.query(f"REMOVE TABLE {table};", {})
        await SurrealDBConnectionManager.close_connection()


class TestCallFunctionE2E:
    @pytest.mark.asyncio
    async def test_returns_the_value_of_a_one_argument_function(self) -> None:
        async with cf_client():
            assert await SurrealDBConnectionManager.call_function("fn::cf_greet", ["ada"]) == "hi ada"

    @pytest.mark.asyncio
    async def test_returns_the_value_of_a_two_argument_function(self) -> None:
        async with cf_client():
            assert await SurrealDBConnectionManager.call_function("fn::cf_sum", [2, 3]) == 5

    @pytest.mark.asyncio
    async def test_adds_the_fn_prefix_when_absent(self) -> None:
        async with cf_client():
            assert await SurrealDBConnectionManager.call_function("cf_greet", ["ada"]) == "hi ada"

    @pytest.mark.asyncio
    async def test_resolves_a_nested_namespace_function(self) -> None:
        async with cf_client():
            assert await SurrealDBConnectionManager.call_function("fn::cf_ns::cf_nested", [21]) == 42

    @pytest.mark.asyncio
    async def test_zero_argument_function(self) -> None:
        async with cf_client():
            assert await SurrealDBConnectionManager.call_function("fn::cf_noargs") == 42

    @pytest.mark.asyncio
    async def test_a_function_returning_none_yields_python_none(self) -> None:
        async with cf_client():
            assert await SurrealDBConnectionManager.call_function("fn::cf_none") is None

    @pytest.mark.asyncio
    async def test_arguments_are_bound_never_interpolated(self) -> None:
        """A hostile argument comes back as data; the record it tries to delete survives."""
        hostile = ");DELETE cf_probe;--"
        async with cf_client("cf_probe") as client:
            await client.query("UPSERT cf_probe:a SET n = 1;", {})
            assert await SurrealDBConnectionManager.call_function("fn::cf_greet", [hostile]) == f"hi {hostile}"
            survivors = await client.query("SELECT * FROM cf_probe;", {})
            assert len(survivors) == 1

    @pytest.mark.asyncio
    async def test_an_invalid_name_raises_before_any_query_is_issued(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _explode() -> Any:
            raise AssertionError("call_function must validate the name before touching the client")

        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", _explode)
        with pytest.raises(ValueError):
            await SurrealDBConnectionManager.call_function("fn::x; DROP TABLE user")

    @pytest.mark.asyncio
    async def test_a_missing_function_raises_not_found(self) -> None:
        """Asserted on the exception TYPE: 3.x and 2.6.x word this error differently."""
        async with cf_client():
            with pytest.raises(SurrealDbNotFoundError):
                await SurrealDBConnectionManager.call_function("fn::cf_does_not_exist")

    @pytest.mark.asyncio
    async def test_wrong_arity_raises_a_database_error_not_a_not_found(self) -> None:
        """The function exists — misclassifying arity as 'missing' would mislead the caller.

        ``SurrealDbNotFoundError`` subclasses ``SurrealDbError``, so asserting the base class
        alone would pass either way; the type is pinned exactly.
        """
        async with cf_client():
            with pytest.raises(SurrealDbError) as caught:
                await SurrealDBConnectionManager.call_function("fn::cf_sum", [1])
            assert not isinstance(caught.value, SurrealDbNotFoundError)

    @pytest.mark.asyncio
    async def test_a_failure_inside_the_function_body_is_not_reported_as_missing(self) -> None:
        """A 'not found' raised by the function's own body must not be mistaken for the
        function itself being absent."""
        async with cf_client() as client:
            await client.query("DEFINE FUNCTION OVERWRITE fn::cf_inner_fail() { RETURN fn::cf_absent_inner(); };", {})
            with pytest.raises(SurrealDbError) as caught:
                await SurrealDBConnectionManager.call_function("fn::cf_inner_fail")
            assert not isinstance(caught.value, SurrealDbNotFoundError)
            with contextlib.suppress(Exception):
                await client.query("REMOVE FUNCTION fn::cf_inner_fail;", {})


class CfLock(PydanticBaseModel):
    acquired: bool
    holder: str


class TestCallFunctionReturnTypeE2E:
    @pytest.mark.asyncio
    async def test_coerces_an_object_into_a_pydantic_model(self) -> None:
        async with cf_client() as client:
            await client.query(
                "DEFINE FUNCTION OVERWRITE fn::cf_lock($who: string) { RETURN { acquired: true, holder: $who }; };",
                {},
            )
            lock = await SurrealDBConnectionManager.call_function("fn::cf_lock", ["pod-1"], return_type=CfLock)
            assert isinstance(lock, CfLock)
            assert lock.acquired is True
            assert lock.holder == "pod-1"
            with contextlib.suppress(Exception):
                await client.query("REMOVE FUNCTION fn::cf_lock;", {})

    @pytest.mark.asyncio
    async def test_coerces_a_scalar(self) -> None:
        async with cf_client():
            value = await SurrealDBConnectionManager.call_function("fn::cf_sum", [2, 3], return_type=float)
            assert isinstance(value, float)
            assert value == 5.0

    @pytest.mark.asyncio
    async def test_scalar_coercion_follows_pydantic_and_does_not_stringify_numbers(self) -> None:
        """``return_type`` is plain Pydantic validation, not a cast: an ``int`` asked to be a
        ``str`` is a mismatch, not a silent ``str(5)``."""
        async with cf_client():
            with pytest.raises(SurrealDbValidationError):
                await SurrealDBConnectionManager.call_function("fn::cf_sum", [2, 3], return_type=str)

    @pytest.mark.asyncio
    async def test_coerces_a_list_element_wise(self) -> None:
        async with cf_client() as client:
            await client.query(
                "DEFINE FUNCTION OVERWRITE fn::cf_locks() "
                "{ RETURN [{ acquired: true, holder: 'a' }, { acquired: false, holder: 'b' }]; };",
                {},
            )
            locks = await SurrealDBConnectionManager.call_function("fn::cf_locks", return_type=list[CfLock])
            assert [type(item) for item in locks] == [CfLock, CfLock]
            assert locks[1].holder == "b"
            with contextlib.suppress(Exception):
                await client.query("REMOVE FUNCTION fn::cf_locks;", {})

    @pytest.mark.asyncio
    async def test_without_a_return_type_the_raw_value_is_returned(self) -> None:
        async with cf_client():
            assert await SurrealDBConnectionManager.call_function("fn::cf_sum", [2, 3]) == 5

    @pytest.mark.asyncio
    async def test_a_value_that_does_not_fit_raises_a_validation_error(self) -> None:
        async with cf_client():
            with pytest.raises(SurrealDbValidationError):
                await SurrealDBConnectionManager.call_function("fn::cf_greet", ["ada"], return_type=CfLock)


class TestCallFunctionNamedParamsE2E:
    @pytest.mark.asyncio
    async def test_named_params_match_the_positional_call(self) -> None:
        async with cf_client() as client:
            # A non-commutative function: a wrong order would silently give a wrong answer.
            await client.query("DEFINE FUNCTION OVERWRITE fn::cf_sub($a: int, $b: int) { RETURN $a - $b; };", {})
            positional = await SurrealDBConnectionManager.call_function("fn::cf_sub", [10, 3])
            named = await SurrealDBConnectionManager.call_function("fn::cf_sub", params={"b": 3, "a": 10})
            assert positional == 7
            assert named == 7
            with contextlib.suppress(Exception):
                await client.query("REMOVE FUNCTION fn::cf_sub;", {})

    @pytest.mark.asyncio
    async def test_passing_both_args_and_params_raises(self) -> None:
        async with cf_client():
            with pytest.raises(ValueError, match="both"):
                await SurrealDBConnectionManager.call_function("fn::cf_sum", [1, 2], params={"a": 1, "b": 2})

    @pytest.mark.asyncio
    async def test_an_unknown_key_raises_naming_the_expected_parameters(self) -> None:
        async with cf_client():
            with pytest.raises(ValueError) as caught:
                await SurrealDBConnectionManager.call_function("fn::cf_sum", params={"a": 1, "nope": 2})
            assert "a" in str(caught.value) and "b" in str(caught.value)

    @pytest.mark.asyncio
    async def test_a_missing_key_raises_naming_the_expected_parameters(self) -> None:
        async with cf_client():
            with pytest.raises(ValueError) as caught:
                await SurrealDBConnectionManager.call_function("fn::cf_sum", params={"a": 1})
            assert "b" in str(caught.value)

    @pytest.mark.asyncio
    async def test_empty_params_on_a_zero_argument_function(self) -> None:
        async with cf_client():
            assert await SurrealDBConnectionManager.call_function("fn::cf_noargs", params={}) == 42

    @pytest.mark.asyncio
    async def test_the_signature_is_cached_across_calls(self) -> None:
        async with cf_client() as client:
            SurrealDBConnectionManager.clear_function_signature_cache()
            await SurrealDBConnectionManager.call_function("fn::cf_sum", params={"a": 1, "b": 2})

            info_calls = 0
            original = client.query

            async def counting(statement: str, variables: Any = None, **kwargs: Any) -> Any:
                nonlocal info_calls
                if "INFO FOR DB" in statement:
                    info_calls += 1
                return await original(statement, variables, **kwargs)

            client.query = counting  # type: ignore[method-assign]
            try:
                await SurrealDBConnectionManager.call_function("fn::cf_sum", params={"a": 1, "b": 2})
                assert info_calls == 0
            finally:
                client.query = original  # type: ignore[method-assign]

    @pytest.mark.asyncio
    async def test_a_redefined_function_resolves_after_one_automatic_refresh(self) -> None:
        """A stale cached signature must self-heal rather than reject a valid call."""
        async with cf_client() as client:
            await client.query("DEFINE FUNCTION OVERWRITE fn::cf_shift($a: int) { RETURN $a; };", {})
            assert await SurrealDBConnectionManager.call_function("fn::cf_shift", params={"a": 1}) == 1

            await client.query("DEFINE FUNCTION OVERWRITE fn::cf_shift($x: int, $y: int) { RETURN $x + $y; };", {})
            assert await SurrealDBConnectionManager.call_function("fn::cf_shift", params={"x": 1, "y": 2}) == 3
            with contextlib.suppress(Exception):
                await client.query("REMOVE FUNCTION fn::cf_shift;", {})

    @pytest.mark.asyncio
    async def test_named_params_for_a_missing_function_raise_not_found(self) -> None:
        async with cf_client():
            with pytest.raises(SurrealDbNotFoundError):
                await SurrealDBConnectionManager.call_function("fn::cf_absent", params={"a": 1})


class TestSignatureCache:
    def test_unset_connection_clears_the_cache(self) -> None:
        SurrealDBConnectionManager.clear_function_signature_cache()
        assert SurrealDBConnectionManager.function_signature_cache_size() == 0


class CfCounter(BaseSurrealModel):
    id: str
    n: int = 0


class TestCallFunctionTransactionE2E:
    """``tx=`` on both strategies. ``fn::cf_bump`` mutates, so the writes are observable."""

    @staticmethod
    async def _define_bump(client: Any) -> None:
        await client.query("DEFINE FUNCTION OVERWRITE fn::cf_bump($by: int) { UPDATE cf_ctr:a SET n += $by; };", {})
        await client.query("UPSERT cf_ctr:a SET n = 0;", {})

    @staticmethod
    async def _counter(client: Any) -> int:
        rows = await client.query("SELECT * FROM cf_ctr:a;", {})
        return int(rows[0]["n"])

    @pytest.mark.asyncio
    async def test_the_functions_write_lands_after_commit(self) -> None:
        async with cf_client("cf_ctr", "CfCounter") as client:
            await self._define_bump(client)
            async with SurrealDBConnectionManager.transaction() as tx:
                await SurrealDBConnectionManager.call_function("fn::cf_bump", [3], tx=tx)
            assert await self._counter(client) == 3
            with contextlib.suppress(Exception):
                await client.query("REMOVE FUNCTION fn::cf_bump;", {})

    @pytest.mark.asyncio
    async def test_a_statement_queued_after_the_call_still_executes(self) -> None:
        """Regression guard for the v0.15.0 design finding.

        ``RETURN`` inside a ``BEGIN … COMMIT`` batch terminates the transaction early and
        SILENTLY: the statement after it is reported ``status: OK`` and never runs, on BOTH DB
        lines. If anyone reintroduces ``RETURN`` into the generated call, the ``save()`` below
        vanishes while the test's other assertion still passes — which is exactly the silent
        data loss this guards.
        """
        async with cf_client("cf_ctr", "CfCounter") as client:
            await self._define_bump(client)
            async with SurrealDBConnectionManager.transaction() as tx:
                await SurrealDBConnectionManager.call_function("fn::cf_bump", [5], tx=tx)
                await CfCounter(id="after", n=99).save(tx=tx)

            assert await self._counter(client) == 5, "the function's own write was lost"
            survivors = await client.query("SELECT * FROM CfCounter;", {})
            assert len(survivors) == 1, "the statement queued AFTER the call never executed"
            with contextlib.suppress(Exception):
                await client.query("REMOVE FUNCTION fn::cf_bump;", {})

    @pytest.mark.asyncio
    async def test_a_rollback_undoes_the_functions_write(self) -> None:
        async with cf_client("cf_ctr", "CfCounter") as client:
            await self._define_bump(client)
            with contextlib.suppress(RuntimeError):
                async with SurrealDBConnectionManager.transaction() as tx:
                    await SurrealDBConnectionManager.call_function("fn::cf_bump", [7], tx=tx)
                    raise RuntimeError("abort")
            assert await self._counter(client) == 0
            with contextlib.suppress(Exception):
                await client.query("REMOVE FUNCTION fn::cf_bump;", {})

    @pytest.mark.asyncio
    async def test_interactive_transaction_returns_the_value(self) -> None:
        async with cf_client("cf_ctr") as client:
            await self._define_bump(client)
            async with SurrealDBConnectionManager.transaction() as tx:
                if not tx.is_interactive:
                    pytest.skip("interactive transactions need WebSocket + SurrealDB 3.x")
                assert await SurrealDBConnectionManager.call_function("fn::cf_greet", ["ada"], tx=tx) == "hi ada"
            with contextlib.suppress(Exception):
                await client.query("REMOVE FUNCTION fn::cf_bump;", {})

    @pytest.mark.asyncio
    async def test_buffered_transaction_returns_none(self) -> None:
        async with cf_client("cf_ctr") as client:
            await self._define_bump(client)
            async with SurrealDBConnectionManager.transaction() as tx:
                if tx.is_interactive:
                    pytest.skip("buffered strategy only (SurrealDB 2.6.x or HTTP)")
                assert await SurrealDBConnectionManager.call_function("fn::cf_greet", ["ada"], tx=tx) is None
            with contextlib.suppress(Exception):
                await client.query("REMOVE FUNCTION fn::cf_bump;", {})

    @pytest.mark.asyncio
    async def test_return_type_with_a_buffered_transaction_raises(self) -> None:
        """There is no value to coerce yet — better a loud error than a silent ``None``."""
        async with cf_client("cf_ctr") as client:
            await self._define_bump(client)
            async with SurrealDBConnectionManager.transaction() as tx:
                if tx.is_interactive:
                    pytest.skip("buffered strategy only (SurrealDB 2.6.x or HTTP)")
                with pytest.raises(ValueError, match="return_type"):
                    await SurrealDBConnectionManager.call_function("fn::cf_greet", ["ada"], tx=tx, return_type=str)
            with contextlib.suppress(Exception):
                await client.query("REMOVE FUNCTION fn::cf_bump;", {})

    @pytest.mark.asyncio
    async def test_named_params_work_inside_a_transaction(self) -> None:
        """The signature read must bypass ``tx`` — buffered transactions forbid reads."""
        async with cf_client("cf_ctr") as client:
            await self._define_bump(client)
            async with SurrealDBConnectionManager.transaction() as tx:
                await SurrealDBConnectionManager.call_function("fn::cf_bump", params={"by": 4}, tx=tx)
            assert await self._counter(client) == 4
            with contextlib.suppress(Exception):
                await client.query("REMOVE FUNCTION fn::cf_bump;", {})
