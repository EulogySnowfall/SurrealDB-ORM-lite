"""Tests for v0.15.0 ``call_function()``: calling custom ``fn::`` stored functions.

The pure helpers (name normalisation, signature parsing, statement building) are unit-tested;
everything that talks to a server lives in a ``…E2E`` class and runs on both DB lines.
"""

import pytest

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
