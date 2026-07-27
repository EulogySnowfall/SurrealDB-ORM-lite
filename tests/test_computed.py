"""Tests for v0.14.0 computed fields: the ``Computed`` type, DDL emission/application,
write exclusion, and the write guards."""

from typing import Annotated, get_args, get_origin

import pytest

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
