"""Tests for v0.14.0 computed fields: the ``Computed`` type, DDL emission/application,
write exclusion, and the write guards."""

from typing import Annotated, get_args, get_origin

import pytest

from surreal_orm_lite import BaseSurrealModel
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
