"""Tests for v0.13.0 server-side computation: SurrealFunc, the curated function-name
enums, the SET-clause compiler, and ``server_values=`` / ``extra_vars=`` on save/merge."""

import pytest

from surreal_orm_lite.functions import SurrealFunc


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
