"""``TYPE RELATION`` enforcement (v0.33.0): an edge model declares its graph constraint with the full SurrealDB-ORM's
keys — ``SurrealConfigDict(table_type=TableType.RELATION, relation_in=…, relation_out=…, enforced=…)``.

``TableType`` and the keys (spec §5.1, §6.2, §6.6; AC-1): the full ORM's enum — six members, same values — exported
from the package root, and the four ``SurrealConfigDict`` keys.

The class-time check (spec §5.2, §5.8, §6.1, §6.3; AC-2; rulings P1, P6): ``schema.relation_tables()`` reads
``relation_in``/``relation_out`` — ``None``, ``""``, ``[]`` and ``()`` mean any table, each name stripped, a repeated
name kept in order — with the exact texts of R2–R5; ``schema.check_relation_config()`` runs T1–T3, R1, R2–R5 for both
keys and R7 in that order when the ``class`` statement runs, inherited values re-checked with the subclass's name.

The getters (spec §5.3; AC-1): ``get_table_type()`` on every model (``RELATION`` for an edge, else the configured
value, else ``NORMAL``), ``get_relation_in()``, ``get_relation_out()`` and ``is_enforced()`` on an edge model, with
the tuple shape of ``TableState.relation_in``/``relation_out``; a subclass inherits them, and ``None`` or an empty
value resets one.

``relation_ddl()`` (spec §5.4, §5.5 G1–G8, §6.1; AC-3): ``schema.relation_type_clause()`` and
``schema.relation_statement()`` render ``DEFINE TABLE {OVERWRITE | IF NOT EXISTS} `<table>` TYPE RELATION[ IN …][ OUT
…][ ENFORCED];`` — pure, no connection, E1 for a non-``bool`` ``overwrite``. ``TestRelationDdlE2E`` applies each golden
on ``ws``, ``http`` and ``mem`` (spec §9.2: the connection ``"rel"`` on the database ``rel_db``, reset before each
test) and reads it back with ``parse_define_table()``: the model's getters equal the stored ``TableState`` (M2, M16,
M22).

The carried statement (spec §5.5 K1–K7, §5.8 RG1–RG3, §6.1, §8; AC-4): ``schema.relation_statement_keeping()`` writes
the model's type clause, then the stored definition's ``DROP``, ``SCHEMAFULL``, ``COMMENT``, ``CHANGEFEED`` and
``PERMISSIONS`` re-rendered from its ``TableState`` — K1–K5 from SurrealDB 3.3.4's renderings, K6 and K7 the
statements the E2E classes apply —, or ``None`` when a stored value fails its check (a rule with a top-level ``;`` or a
comment marker, a NUL in the comment). ``schema.table_permissions_text()`` is the model generator's renderer, moved;
``schema.relation_table_refused()`` gives the guard's refusal texts.
"""

import contextlib
import inspect
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from surrealdb import RecordID

import surreal_orm_lite
from surreal_orm_lite import (
    BaseSurrealEdge,
    BaseSurrealModel,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    TableType,
    model_generator,
    schema,
)
from surreal_orm_lite.define_parser import parse_define_table
from surreal_orm_lite.schema_state import TableState

# --- the exact texts of spec §5.8 ----------------------------------------------------------------------------------


def _t1(name: str, type_name: str) -> str:
    return f"{name}: model_config['table_type'] must be a surreal_orm_lite.TableType, got {type_name}."


def _t2(name: str, member: str) -> str:
    return (
        f"{name}: an edge model's table is TYPE RELATION; leave table_type out or set TableType.RELATION, "
        f"got TableType.{member}."
    )


def _t3(name: str) -> str:
    return (
        f"{name}: table_type=TableType.RELATION needs an edge model: derive {name} from BaseSurrealEdge, which "
        "declares the endpoints in_ and out."
    )


def _r1(name: str, keys: str) -> str:
    return f"{name}: relation constraints ({keys}) apply to an edge model only: derive {name} from BaseSurrealEdge."


def _r2(name: str, key: str, type_name: str) -> str:
    return (
        f"{name}: model_config['{key}'] must be a table name, several separated by '|', or a list or tuple of table "
        f"names, got {type_name}."
    )


def _r3(name: str, key: str, type_name: str) -> str:
    return f"{name}: model_config['{key}'] holds a {type_name}, not a table name."


def _r4(name: str, key: str) -> str:
    return f"{name}: model_config['{key}'] names no table; leave it out (None) to accept any table."


def _r5(name: str, key: str, part: str) -> str:
    return (
        f"{name}: model_config['{key}'] names {part!r}, which is not a table name the ORM can write: an identifier "
        "is expected (letters, digits, underscores; not starting with a digit)."
    )


def _r7(name: str, type_name: str) -> str:
    return f"{name}: model_config['enforced'] must be a bool or None, got {type_name}."


def _rg1(name: str, table: str) -> str:
    return (
        f"{name}.define_relation(): the table '{table}' is a materialized view; defining it as a relation table would "
        f"drop its query and turn it into an ordinary table that no longer updates. Give {name} another table_name, or "
        "remove the view first. Nothing was defined."
    )


def _rg2(name: str, table: str) -> str:
    return (
        f"{name}.define_relation(): the definition of the table '{table}' could not be read, so the ORM cannot keep "
        "its other clauses (schema mode, permissions, change feed, comment). Nothing was defined."
    )


def _rg3(name: str, table: str) -> str:
    return (
        f"{name}.define_relation(overwrite=False): the table '{table}' exists and is not a relation table; DEFINE "
        "TABLE IF NOT EXISTS would leave it as it is, and the relation constraint would not apply. Call "
        "define_relation() with overwrite=True, which keeps the table's other clauses. Nothing was defined."
    )


# --- the value forms (P1) ------------------------------------------------------------------------------------------

#: A value that means "any table": no IN (or OUT) clause, the getter returns ().
_ANY_TABLE: list[object] = [None, "", [], ()]

#: (value, the names it gives) — each name stripped, in order, a repeated name kept.
_NAMES: list[tuple[object, tuple[str, ...]]] = [
    ("a", ("a",)),
    (["a"], ("a",)),
    (("a",), ("a",)),
    ([" a "], ("a",)),
    ("a | b", ("a", "b")),
    ("a|b", ("a", "b")),
    (" a |b ", ("a", "b")),
    (["a", "b"], ("a", "b")),
    ((" a", "b "), ("a", "b")),
    ([" a ", "b"], ("a", "b")),
    ("a | a", ("a", "a")),
    (["a", "a"], ("a", "a")),
    (["b", "B"], ("b", "B")),
    ("person_2 | _Company", ("person_2", "_Company")),
]

#: (value, error class, text for the model "M" and the key "relation_in") — R2–R5. No R6: a repeated name is kept.
_REFUSED: list[tuple[object, type[Exception], str]] = [
    (3, TypeError, _r2("M", "relation_in", "int")),
    ({"a"}, TypeError, _r2("M", "relation_in", "set")),
    (True, TypeError, _r2("M", "relation_in", "bool")),
    ({"a": 1}, TypeError, _r2("M", "relation_in", "dict")),
    (["a", 1], TypeError, _r3("M", "relation_in", "int")),
    (("a", None), TypeError, _r3("M", "relation_in", "NoneType")),
    ("  ", ValueError, _r4("M", "relation_in")),
    ("\n", ValueError, _r4("M", "relation_in")),
    ("a-b", ValueError, _r5("M", "relation_in", "a-b")),
    ("1a", ValueError, _r5("M", "relation_in", "1a")),
    (["a | b"], ValueError, _r5("M", "relation_in", "a | b")),
    ("a | b-c", ValueError, _r5("M", "relation_in", "b-c")),
    (["a", " 1a "], ValueError, _r5("M", "relation_in", "1a")),
    ("a||b", ValueError, _r5("M", "relation_in", "")),
    ("a |", ValueError, _r5("M", "relation_in", "")),
    ("|", ValueError, _r5("M", "relation_in", "")),
    ([""], ValueError, _r5("M", "relation_in", "")),
    (["  "], ValueError, _r5("M", "relation_in", "")),
]


class TestTableType:
    """AC-1: ``TableType`` is the full SurrealDB-ORM's enum — six members, same values — exported from the root."""

    def test_imported_from_the_package_root(self) -> None:
        assert surreal_orm_lite.TableType is TableType
        assert "TableType" in surreal_orm_lite.__all__
        assert surreal_orm_lite.__all__.index("TableType") == surreal_orm_lite.__all__.index("SurrealConfigDict") + 1
        assert "TableType" in surreal_orm_lite.types.__all__

    def test_members_and_values(self) -> None:
        assert {member.name: member.value for member in TableType} == {
            "NORMAL": "normal",
            "USER": "user",
            "STREAM": "stream",
            "HASH": "hash",
            "RELATION": "relation",
            "ANY": "any",
        }
        assert [member.name for member in TableType] == ["NORMAL", "USER", "STREAM", "HASH", "RELATION", "ANY"]

    def test_a_str_enum(self) -> None:
        assert TableType.RELATION == "relation"
        assert isinstance(TableType.RELATION, str)
        assert TableType("relation") is TableType.RELATION
        assert str(TableType.ANY) == "any"


class TestRelationTables:
    """AC-1, AC-2 (P1): ``schema.relation_tables()`` — the names of ``relation_in``/``relation_out`` or R2–R5."""

    @pytest.mark.parametrize("value", _ANY_TABLE, ids=repr)
    def test_any_table(self, value: object) -> None:
        assert schema.relation_tables(value, model="M", key="relation_in") == ()

    @pytest.mark.parametrize(("value", "names"), _NAMES, ids=repr)
    def test_names(self, value: object, names: tuple[str, ...]) -> None:
        result = schema.relation_tables(value, model="M", key="relation_in")
        assert result == names
        assert type(result) is tuple
        assert all(type(name) is str for name in result)

    def test_a_str_enum_value_gives_plain_strings(self) -> None:
        result = schema.relation_tables([TableType.USER], model="M", key="relation_out")
        assert result == ("user",)
        assert type(result[0]) is str

    @pytest.mark.parametrize(("value", "error", "text"), _REFUSED, ids=repr)
    def test_refused(self, value: object, error: type[Exception], text: str) -> None:
        with pytest.raises(error) as raised:
            schema.relation_tables(value, model="M", key="relation_in")
        assert type(raised.value) is error
        assert str(raised.value) == text

    def test_the_key_and_the_model_are_named(self) -> None:
        with pytest.raises(ValueError) as raised:
            schema.relation_tables("a-b", model="Likes", key="relation_out")
        assert str(raised.value) == _r5("Likes", "relation_out", "a-b")

    def test_every_item_is_a_str_before_any_name_is_read(self) -> None:
        """R3 before R5 (spec §5.2's order): an item that is not a ``str`` is reported even after a bad name."""
        with pytest.raises(TypeError) as raised:
            schema.relation_tables(["a-b", 1], model="M", key="relation_in")
        assert str(raised.value) == _r3("M", "relation_in", "int")

    def test_no_r6_for_a_repeated_name(self) -> None:
        assert schema.relation_tables("a | a | b", model="M", key="relation_in") == ("a", "a", "b")
        assert schema.relation_tables(("a", "a"), model="M", key="relation_out") == ("a", "a")


class TestRelationConfig:
    """AC-2: the keys are checked at the ``class`` statement — T1–T3, R1, R2–R5 for ``relation_in`` then
    ``relation_out``, R7, in that order (the first failure wins), inherited values re-checked with the subclass's
    name."""

    @pytest.mark.parametrize(("value", "names"), [*((value, ()) for value in _ANY_TABLE), *_NAMES], ids=repr)
    def test_an_edge_model_with_each_value_form(self, value: object, names: tuple[str, ...]) -> None:
        class Likes(BaseSurrealEdge):
            model_config = SurrealConfigDict(
                table_name="likes",
                table_type=TableType.RELATION,
                relation_in=value,  # type: ignore[typeddict-item]
                relation_out=value,  # type: ignore[typeddict-item]
                enforced=True,
            )

        assert Likes.get_relation_in() == names
        assert Likes.get_relation_out() == names
        assert Likes.is_enforced() is True

    def test_the_readme_example(self) -> None:
        class Likes(BaseSurrealEdge):
            model_config = SurrealConfigDict(
                table_name="likes",
                table_type=TableType.RELATION,
                relation_in="person",
                relation_out=["post", "book"],
                enforced=True,
            )

            stars: int = 1

        assert Likes.get_table_type() is TableType.RELATION
        assert Likes.get_relation_in() == ("person",)
        assert Likes.get_relation_out() == ("post", "book")
        assert Likes.is_enforced() is True

    def test_t1_a_plain_string_on_an_edge(self) -> None:
        with pytest.raises(TypeError) as raised:

            class Likes(BaseSurrealEdge):
                model_config = SurrealConfigDict(table_name="likes", table_type="relation")  # type: ignore[typeddict-item]

        assert type(raised.value) is TypeError
        assert str(raised.value) == _t1("Likes", "str")

    def test_t1_a_plain_string_on_a_node(self) -> None:
        with pytest.raises(TypeError) as raised:

            class Person(BaseSurrealModel):
                model_config = SurrealConfigDict(table_name="person", table_type="relation")  # type: ignore[typeddict-item]

                id: RecordID | None = None

        assert type(raised.value) is TypeError
        assert str(raised.value) == _t1("Person", "str")

    def test_t1_another_type(self) -> None:
        with pytest.raises(TypeError) as raised:

            class Person(BaseSurrealModel):
                model_config = SurrealConfigDict(table_name="person", table_type=1)  # type: ignore[typeddict-item]

                id: RecordID | None = None

        assert str(raised.value) == _t1("Person", "int")

    @pytest.mark.parametrize("member", [m for m in TableType if m is not TableType.RELATION], ids=str)
    def test_t2_another_member_on_an_edge(self, member: TableType) -> None:
        with pytest.raises(TypeError) as raised:

            class Likes(BaseSurrealEdge):
                model_config = SurrealConfigDict(table_name="likes", table_type=member)

        assert type(raised.value) is TypeError
        assert str(raised.value) == _t2("Likes", member.name)

    def test_t3_relation_on_a_node(self) -> None:
        with pytest.raises(TypeError) as raised:

            class Likes(BaseSurrealModel):
                model_config = SurrealConfigDict(table_name="likes", table_type=TableType.RELATION)

                id: RecordID | None = None

        assert type(raised.value) is TypeError
        assert str(raised.value) == _t3("Likes")

    def test_t3_before_r1(self) -> None:
        """The full ORM's ``class Likes(BaseSurrealModel)`` form, ported as is: T3 is reported first."""
        with pytest.raises(TypeError) as raised:

            class Likes(BaseSurrealModel):
                model_config = SurrealConfigDict(
                    table_name="likes",
                    table_type=TableType.RELATION,
                    relation_in="person",
                    relation_out="post",
                    enforced=True,
                )

                id: RecordID | None = None

        assert str(raised.value) == _t3("Likes")

    @pytest.mark.parametrize(
        ("config", "keys"),
        [
            ({"relation_in": "a"}, "relation_in"),
            ({"relation_out": ["a", "b"]}, "relation_out"),
            ({"enforced": True}, "enforced"),
            ({"relation_in": "a", "enforced": True}, "relation_in, enforced"),
            ({"enforced": True, "relation_out": "b", "relation_in": "a"}, "relation_in, relation_out, enforced"),
            # P1 c: any key that is not None, an empty value and enforced=False included.
            ({"relation_in": ""}, "relation_in"),
            ({"relation_in": []}, "relation_in"),
            ({"relation_out": ()}, "relation_out"),
            ({"relation_out": []}, "relation_out"),
            ({"enforced": False}, "enforced"),
            ({"relation_in": "", "relation_out": [], "enforced": False}, "relation_in, relation_out, enforced"),
            # R1 comes before the values' own checks.
            ({"relation_in": "a-b"}, "relation_in"),
            ({"enforced": "yes"}, "enforced"),
        ],
        ids=repr,
    )
    def test_r1_on_a_node(self, config: dict[str, Any], keys: str) -> None:
        with pytest.raises(TypeError) as raised:

            class Person(BaseSurrealModel):
                model_config = SurrealConfigDict(table_name="person", **config)

                id: RecordID | None = None

        assert type(raised.value) is TypeError
        assert str(raised.value) == _r1("Person", keys)

    def test_r1_on_a_node_with_a_table_type_member(self) -> None:
        with pytest.raises(TypeError) as raised:

            class Person(BaseSurrealModel):
                model_config = SurrealConfigDict(table_name="person", table_type=TableType.NORMAL, relation_out="b")

                id: RecordID | None = None

        assert str(raised.value) == _r1("Person", "relation_out")

    def test_r1_on_a_view_model(self) -> None:
        with pytest.raises(TypeError) as raised:

            class LikeCounts(BaseSurrealModel):
                model_config = SurrealConfigDict(
                    table_name="like_counts",
                    view_query="SELECT count() AS n, out FROM likes GROUP BY out",
                    enforced=True,
                )

                id: RecordID | None = None

        assert type(raised.value) is TypeError
        assert str(raised.value) == _r1("LikeCounts", "enforced")

    @pytest.mark.parametrize(("value", "error", "text"), _REFUSED, ids=repr)
    def test_relation_in_refused_on_an_edge(self, value: object, error: type[Exception], text: str) -> None:
        with pytest.raises(error) as raised:

            class Likes(BaseSurrealEdge):
                model_config = SurrealConfigDict(table_name="likes", relation_in=value)  # type: ignore[typeddict-item]

        assert type(raised.value) is error
        assert str(raised.value) == text.replace("M: ", "Likes: ", 1)

    @pytest.mark.parametrize(("value", "error", "text"), _REFUSED, ids=repr)
    def test_relation_out_refused_on_an_edge(self, value: object, error: type[Exception], text: str) -> None:
        with pytest.raises(error) as raised:

            class Likes(BaseSurrealEdge):
                model_config = SurrealConfigDict(table_name="likes", relation_out=value)  # type: ignore[typeddict-item]

        assert type(raised.value) is error
        assert str(raised.value) == text.replace("M: ", "Likes: ", 1).replace("'relation_in'", "'relation_out'")

    def test_relation_in_before_relation_out(self) -> None:
        with pytest.raises(ValueError) as raised:

            class Likes(BaseSurrealEdge):
                model_config = SurrealConfigDict(table_name="likes", relation_in="  ", relation_out=3)  # type: ignore[typeddict-item]

        assert type(raised.value) is ValueError
        assert str(raised.value) == _r4("Likes", "relation_in")

    @pytest.mark.parametrize(("value", "type_name"), [("yes", "str"), (1, "int"), (0, "int"), ([], "list")], ids=repr)
    def test_r7_enforced_not_a_bool(self, value: object, type_name: str) -> None:
        with pytest.raises(TypeError) as raised:

            class Likes(BaseSurrealEdge):
                model_config = SurrealConfigDict(table_name="likes", relation_in="a", enforced=value)  # type: ignore[typeddict-item]

        assert type(raised.value) is TypeError
        assert str(raised.value) == _r7("Likes", type_name)

    def test_r7_after_both_keys(self) -> None:
        with pytest.raises(ValueError) as raised:

            class Likes(BaseSurrealEdge):
                model_config = SurrealConfigDict(table_name="likes", relation_out="a-b", enforced="yes")  # type: ignore[typeddict-item]

        assert str(raised.value) == _r5("Likes", "relation_out", "a-b")

    def test_the_first_failure_wins(self) -> None:
        """``table_type=TableType.ANY`` and an R5 value on an edge: T2 is reported."""
        with pytest.raises(TypeError) as raised:

            class Likes(BaseSurrealEdge):
                model_config = SurrealConfigDict(table_name="likes", table_type=TableType.ANY, relation_in="a-b")

        assert type(raised.value) is TypeError
        assert str(raised.value) == _t2("Likes", "ANY")

    def test_t1_before_t2(self) -> None:
        with pytest.raises(TypeError) as raised:

            class Likes(BaseSurrealEdge):
                model_config = SurrealConfigDict(table_name="likes", table_type="any", enforced="yes")  # type: ignore[typeddict-item]

        assert str(raised.value) == _t1("Likes", "str")

    def test_a_subclass_value_is_checked_with_the_subclass_name(self) -> None:
        class Likes(BaseSurrealEdge):
            model_config = SurrealConfigDict(table_name="likes", relation_in="a")

        with pytest.raises(ValueError) as raised:

            class Loves(Likes):
                model_config = SurrealConfigDict(table_name="loves", relation_in="b-c")

        assert type(raised.value) is ValueError
        assert str(raised.value) == _r5("Loves", "relation_in", "b-c")

    def test_an_inherited_value_is_checked_again_with_the_subclass_name(self) -> None:
        """The parent's ``relation_in`` is inherited and checked with the subclass's other keys."""

        class Likes(BaseSurrealEdge):
            model_config = SurrealConfigDict(table_name="likes", relation_in="a")

        with pytest.raises(TypeError) as raised:

            class Loves(Likes):
                model_config = SurrealConfigDict(table_name="loves", table_type=TableType.HASH)

        assert str(raised.value) == _t2("Loves", "HASH")

    def test_an_inherited_enforced_is_kept(self) -> None:
        class Likes(BaseSurrealEdge):
            model_config = SurrealConfigDict(table_name="likes", enforced=True)

        class Loves(Likes):
            pass

        assert Loves.is_enforced() is True
        assert Loves.get_relation_in() == ()

    def test_a_repeated_name_is_accepted(self) -> None:
        """R6 is withdrawn (P1 b): a repeated name is kept, in order, as SurrealDB stores it."""

        class Likes(BaseSurrealEdge):
            model_config = SurrealConfigDict(table_name="likes", relation_in=["a", "a"], relation_out="b | B")

        assert Likes.get_relation_in() == ("a", "a")
        assert Likes.get_relation_out() == ("b", "B")

    @pytest.mark.parametrize("member", [m for m in TableType if m is not TableType.RELATION], ids=str)
    def test_every_other_member_on_a_node(self, member: TableType) -> None:
        """D4: recorded and returned, no effect before the table DDL."""

        class Person(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="person", table_type=member)

            id: RecordID | None = None

        assert Person.get_table_type() is member

    def test_a_model_without_the_keys_is_unaffected(self) -> None:
        class Person(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="person")

            id: RecordID | None = None

        class Likes(BaseSurrealEdge):
            model_config = SurrealConfigDict(table_name="likes")

        assert Person.get_table_type() is TableType.NORMAL
        assert Likes.get_table_type() is TableType.RELATION
        assert Likes.get_relation_in() == ()
        assert Likes.get_relation_out() == ()
        assert Likes.is_enforced() is False


class TestGetters:
    """AC-1: ``get_table_type()``, ``get_relation_in()``, ``get_relation_out()``, ``is_enforced()``; inheritance and
    the reset of a subclass (``None`` or an empty value, P1 a)."""

    def test_get_table_type_of_the_bases(self) -> None:
        assert BaseSurrealModel.get_table_type() is TableType.NORMAL
        assert BaseSurrealEdge.get_table_type() is TableType.RELATION

    def test_get_table_type_of_an_edge_with_and_without_the_key(self) -> None:
        class Likes(BaseSurrealEdge):
            model_config = SurrealConfigDict(table_name="likes")

        class Loves(BaseSurrealEdge):
            model_config = SurrealConfigDict(table_name="loves", table_type=TableType.RELATION)

        class Hates(BaseSurrealEdge):
            model_config = SurrealConfigDict(table_name="hates", table_type=None)

        for model in (Likes, Loves, Hates):
            assert model.get_table_type() is TableType.RELATION, model.__name__

    def test_get_table_type_of_a_node(self) -> None:
        class Person(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="person")

            id: RecordID | None = None

        class Member(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="member", table_type=TableType.USER)

            id: RecordID | None = None

        class Guest(Member):
            model_config = SurrealConfigDict(table_name="guest")

        class Visitor(Member):
            model_config = SurrealConfigDict(table_name="visitor", table_type=None)

        assert Person.get_table_type() is TableType.NORMAL
        assert Member.get_table_type() is TableType.USER
        assert Guest.get_table_type() is TableType.USER
        assert Visitor.get_table_type() is TableType.NORMAL

    def test_the_getters_of_the_base_edge(self) -> None:
        assert BaseSurrealEdge.get_relation_in() == ()
        assert BaseSurrealEdge.get_relation_out() == ()
        assert BaseSurrealEdge.is_enforced() is False

    @pytest.mark.parametrize(("value", "names"), [*((value, ()) for value in _ANY_TABLE), *_NAMES], ids=repr)
    def test_each_value_form(self, value: object, names: tuple[str, ...]) -> None:
        class Likes(BaseSurrealEdge):
            model_config = SurrealConfigDict(table_name="likes", relation_in=value, relation_out=value)  # type: ignore[typeddict-item]

        assert Likes.get_relation_in() == names
        assert Likes.get_relation_out() == names
        assert type(Likes.get_relation_in()) is tuple
        assert Likes.is_enforced() is False

    @pytest.mark.parametrize(("value", "expected"), [(None, False), (False, False), (True, True)], ids=repr)
    def test_is_enforced(self, value: bool | None, expected: bool) -> None:
        class Likes(BaseSurrealEdge):
            model_config = SurrealConfigDict(table_name="likes", enforced=value)

        assert Likes.is_enforced() is expected

    def test_the_getters_compare_with_a_table_state(self) -> None:
        """The tuple shape of ``TableState.relation_in``/``relation_out`` (spec §5.3)."""

        class Likes(BaseSurrealEdge):
            model_config = SurrealConfigDict(table_name="likes", relation_in=" person ", relation_out="post|book")

        state = surreal_orm_lite.TableState(
            name="likes", table_type="relation", relation_in="person", relation_out=("post", "book")
        )
        assert Likes.get_relation_in() == state.relation_in
        assert Likes.get_relation_out() == state.relation_out
        assert Likes.is_enforced() is state.enforced

    def test_a_subclass_inherits_the_three_keys(self) -> None:
        class Likes(BaseSurrealEdge):
            model_config = SurrealConfigDict(
                table_name="likes", relation_in="person", relation_out=["post", "book"], enforced=True
            )

        class Loves(Likes):
            model_config = SurrealConfigDict(table_name="loves")

        class LovesAgain(Loves):
            pass

        for model in (Loves, LovesAgain):
            assert model.get_relation_in() == ("person",), model.__name__
            assert model.get_relation_out() == ("post", "book"), model.__name__
            assert model.is_enforced() is True, model.__name__
            assert model.get_table_type() is TableType.RELATION, model.__name__

    def test_a_subclass_overrides_one_key(self) -> None:
        class Likes(BaseSurrealEdge):
            model_config = SurrealConfigDict(table_name="likes", relation_in="person", relation_out="post")

        class Loves(Likes):
            model_config = SurrealConfigDict(table_name="loves", relation_out=["book"], enforced=True)

        assert Loves.get_relation_in() == ("person",)
        assert Loves.get_relation_out() == ("book",)
        assert Loves.is_enforced() is True
        # The parent is unchanged.
        assert Likes.get_relation_out() == ("post",)
        assert Likes.is_enforced() is False

    @pytest.mark.parametrize("reset", [None, [], (), ""], ids=repr)
    def test_none_or_an_empty_value_resets_a_key_in_a_subclass(self, reset: object) -> None:
        class Likes(BaseSurrealEdge):
            model_config = SurrealConfigDict(
                table_name="likes", relation_in="person", relation_out=["post", "book"], enforced=True
            )

        class LikesAny(Likes):
            model_config = SurrealConfigDict(relation_in=reset, relation_out=reset)  # type: ignore[typeddict-item]

        assert LikesAny.get_relation_in() == ()
        assert LikesAny.get_relation_out() == ()
        assert LikesAny.is_enforced() is True
        # The parent is unchanged.
        assert Likes.get_relation_in() == ("person",)

    @pytest.mark.parametrize("reset", [None, False], ids=repr)
    def test_enforced_reset_in_a_subclass(self, reset: bool | None) -> None:
        class Likes(BaseSurrealEdge):
            model_config = SurrealConfigDict(table_name="likes", enforced=True)

        class LikesLoose(Likes):
            model_config = SurrealConfigDict(enforced=reset)

        assert LikesLoose.is_enforced() is False
        assert Likes.is_enforced() is True


# === relation_ddl() (spec §5.4, §5.5 G1–G8; AC-3) =================================================================


class G1Likes(BaseSurrealEdge):
    """G1: both ends and ``ENFORCED``."""

    model_config = SurrealConfigDict(table_name="likes", relation_in="person", relation_out=["post", "book"], enforced=True)


class G2Follows(BaseSurrealEdge):
    """G2: no key — any table on both sides."""

    model_config = SurrealConfigDict(table_name="follows")


class G3Enforced(BaseSurrealEdge):
    """G3: ``enforced=True`` only."""

    model_config = SurrealConfigDict(table_name="e3", enforced=True)


class G4Out(BaseSurrealEdge):
    """G4: ``relation_out`` only."""

    model_config = SurrealConfigDict(table_name="e4", relation_out="post")


class G5Piped(BaseSurrealEdge):
    """G5: two tables in one pipe-joined string."""

    model_config = SurrealConfigDict(table_name="e5", relation_in="person | company")


class G5Bare(BaseSurrealEdge):
    """G5, written without spaces."""

    model_config = SurrealConfigDict(table_name="e5", relation_in="person|company")


class G5Tuple(BaseSurrealEdge):
    """G5, as a tuple."""

    model_config = SurrealConfigDict(table_name="e5", relation_in=("person", "company"))


class G6Keywords(BaseSurrealEdge):
    """G6: keyword names, the table's included."""

    model_config = SurrealConfigDict(table_name="select", relation_in="order", relation_out="select")


class G7Loose(BaseSurrealEdge):
    """G7: ``enforced=False`` writes nothing."""

    model_config = SurrealConfigDict(table_name="e7", enforced=False)


class G7EmptyStr(BaseSurrealEdge):
    """G7: an empty string means any table (P1 a)."""

    model_config = SurrealConfigDict(table_name="e7", relation_in="")


class G7EmptyList(BaseSurrealEdge):
    """G7: an empty list means any table (P1 a)."""

    model_config = SurrealConfigDict(table_name="e7", relation_in=[])


class G7EmptyTuple(BaseSurrealEdge):
    """G7: an empty tuple means any table (P1 a)."""

    model_config = SurrealConfigDict(table_name="e7", relation_out=())


class G8Twice(BaseSurrealEdge):
    """G8: a repeated name is kept, in order (P1 b, M22)."""

    model_config = SurrealConfigDict(table_name="e8", relation_in=["a", "a"])


_G1 = "DEFINE TABLE OVERWRITE `likes` TYPE RELATION IN `person` OUT `post` | `book` ENFORCED;"
_G1_KEEP = "DEFINE TABLE IF NOT EXISTS `likes` TYPE RELATION IN `person` OUT `post` | `book` ENFORCED;"
_G5 = "DEFINE TABLE OVERWRITE `e5` TYPE RELATION IN `person` | `company`;"
_G7 = "DEFINE TABLE OVERWRITE `e7` TYPE RELATION;"

#: (model, overwrite, the exact statement of spec §5.5).
_GOLDENS: list[tuple[type[BaseSurrealEdge], bool, str]] = [
    (G1Likes, True, _G1),
    (G1Likes, False, _G1_KEEP),
    (G2Follows, True, "DEFINE TABLE OVERWRITE `follows` TYPE RELATION;"),
    (G3Enforced, True, "DEFINE TABLE OVERWRITE `e3` TYPE RELATION ENFORCED;"),
    (G4Out, True, "DEFINE TABLE OVERWRITE `e4` TYPE RELATION OUT `post`;"),
    (G5Piped, True, _G5),
    (G5Bare, True, _G5),
    (G5Tuple, True, _G5),
    (G6Keywords, True, "DEFINE TABLE OVERWRITE `select` TYPE RELATION IN `order` OUT `select`;"),
    (G7Loose, True, _G7),
    (G7EmptyStr, True, _G7),
    (G7EmptyList, True, _G7),
    (G7EmptyTuple, True, _G7),
    (G8Twice, True, "DEFINE TABLE OVERWRITE `e8` TYPE RELATION IN `a` | `a`;"),
]

_GOLDEN_IDS = [f"{model.__name__}-{'overwrite' if overwrite else 'if-not-exists'}" for model, overwrite, _ in _GOLDENS]

#: (relation_in, relation_out, enforced, the type clause) — the ends of each G-case.
_CLAUSES: list[tuple[tuple[str, ...], tuple[str, ...], bool, str]] = [
    (("person",), ("post", "book"), True, "TYPE RELATION IN `person` OUT `post` | `book` ENFORCED"),
    ((), (), False, "TYPE RELATION"),
    ((), (), True, "TYPE RELATION ENFORCED"),
    ((), ("post",), False, "TYPE RELATION OUT `post`"),
    (("person", "company"), (), False, "TYPE RELATION IN `person` | `company`"),
    (("order",), ("select",), False, "TYPE RELATION IN `order` OUT `select`"),
    (("a", "a"), (), False, "TYPE RELATION IN `a` | `a`"),
]


class TestRelationDdl:
    """AC-3: ``relation_ddl()`` renders G1–G8 exactly — pure, the same text every time, no connection needed; E1 for a
    non-``bool`` ``overwrite``."""

    @pytest.mark.parametrize(("relation_in", "relation_out", "enforced", "clause"), _CLAUSES, ids=repr)
    def test_relation_type_clause(
        self, relation_in: tuple[str, ...], relation_out: tuple[str, ...], enforced: bool, clause: str
    ) -> None:
        assert schema.relation_type_clause(relation_in, relation_out, enforced) == clause
        assert schema.relation_type_clause(list(relation_in), list(relation_out), enforced) == clause

    @pytest.mark.parametrize(
        ("overwrite", "statement"),
        [
            (True, "DEFINE TABLE OVERWRITE `likes` TYPE RELATION ENFORCED;"),
            (False, "DEFINE TABLE IF NOT EXISTS `likes` TYPE RELATION ENFORCED;"),
        ],
    )
    def test_relation_statement(self, overwrite: bool, statement: str) -> None:
        assert schema.relation_statement("likes", "TYPE RELATION ENFORCED", overwrite) == statement

    @pytest.mark.parametrize(("table", "error"), [("a-b", ValueError), ("1a", ValueError), (3, TypeError)], ids=repr)
    def test_relation_statement_checks_the_table_name(self, table: Any, error: type[Exception]) -> None:
        with pytest.raises(error):
            schema.relation_statement(table, "TYPE RELATION", True)

    @pytest.mark.parametrize(("model", "overwrite", "statement"), _GOLDENS, ids=_GOLDEN_IDS)
    def test_golden_statements(self, model: type[BaseSurrealEdge], overwrite: bool, statement: str) -> None:
        assert model.relation_ddl(overwrite=overwrite) == statement

    def test_overwrite_by_default(self) -> None:
        assert G1Likes.relation_ddl() == G1Likes.relation_ddl(overwrite=True) == _G1

    def test_deterministic(self) -> None:
        assert [G1Likes.relation_ddl() for _ in range(3)] == [_G1] * 3

    def test_signature(self) -> None:
        signature = inspect.signature(BaseSurrealEdge.relation_ddl)
        assert list(signature.parameters) == ["overwrite"]
        parameter = signature.parameters["overwrite"]
        assert (parameter.kind, parameter.default) == (inspect.Parameter.KEYWORD_ONLY, True)
        assert signature.return_annotation is str
        assert isinstance(inspect.getattr_static(BaseSurrealEdge, "relation_ddl"), classmethod)
        assert not hasattr(BaseSurrealModel, "relation_ddl")

    def test_a_subclass_renders_its_own_table(self) -> None:
        class Loves(G1Likes):
            model_config = SurrealConfigDict(table_name="loves")

        assert Loves.relation_ddl() == "DEFINE TABLE OVERWRITE `loves` TYPE RELATION IN `person` OUT `post` | `book` ENFORCED;"
        assert G1Likes.relation_ddl() == _G1

    @pytest.mark.parametrize(("overwrite", "type_name"), [("yes", "str"), (1, "int"), (None, "NoneType")], ids=repr)
    def test_e1_overwrite_must_be_a_bool(self, overwrite: Any, type_name: str) -> None:
        with pytest.raises(TypeError) as raised:
            G1Likes.relation_ddl(overwrite=overwrite)
        assert type(raised.value) is TypeError
        assert str(raised.value) == f"overwrite= must be a bool, got {type_name}."

    async def test_works_with_no_connection_configured(self) -> None:
        await SurrealDBConnectionManager.unset_connection()
        assert SurrealDBConnectionManager.is_connection_set() is False
        assert G1Likes.relation_ddl() == _G1
        assert G8Twice.relation_ddl(overwrite=False) == "DEFINE TABLE IF NOT EXISTS `e8` TYPE RELATION IN `a` | `a`;"


# === the carried statement (spec §5.5 K1–K7, §5.8 RG1–RG3, §6.1, §8; AC-4) ======================================


class RelLikes(BaseSurrealEdge):
    """The edge model of the E2E classes on ``rel_likes`` (spec AC-6): the constraint of K6."""

    model_config = SurrealConfigDict(
        table_name="rel_likes",
        connection="rel",
        relation_in="rel_person",
        relation_out=["rel_post", "rel_book"],
        enforced=True,
    )

    stars: int = 1


#: G1's type clause, the clause of K1–K5.
_G1_CLAUSE = "TYPE RELATION IN `person` OUT `post` | `book` ENFORCED"

#: The stored definitions of K1–K5 as SurrealDB 3.3.4 renders them in ``INFO FOR DB`` (the same text on 2.7.0, 3.1.5
#: and the embedded engine, M10). K1's comment holds a quote, a backslash and double quotes: the server writes it
#: between double quotes.
_K1_STORED = (
    'DEFINE TABLE t TYPE NORMAL SCHEMAFULL COMMENT "it\'s \\\\ a \\"c\\" line" CHANGEFEED 1h INCLUDE ORIGINAL '
    "PERMISSIONS FOR select WHERE in = $auth.id OR out = $auth.id, FOR create FULL, FOR update, delete NONE"
)
_K2_STORED = "DEFINE TABLE t TYPE ANY DROP SCHEMALESS PERMISSIONS NONE"
_K3_STORED = "DEFINE TABLE t TYPE ANY SCHEMALESS PERMISSIONS FULL"
_K4_STORED = "DEFINE TABLE t TYPE ANY SCHEMALESS PERMISSIONS NONE"
_K5_STORED = (
    "DEFINE TABLE t TYPE RELATION IN company OUT post ENFORCED SCHEMAFULL PERMISSIONS FOR select FULL, FOR create, "
    "update, delete NONE"
)

#: K1–K5: the stored definition, then the carried statement of G1's clause on the table ``t`` (spec §5.5).
_K_CASES: list[tuple[str, str, str]] = [
    (
        "K1",
        _K1_STORED,
        "DEFINE TABLE OVERWRITE `t` TYPE RELATION IN `person` OUT `post` | `book` ENFORCED SCHEMAFULL COMMENT "
        "'it\\'s \\\\ a \"c\" line' CHANGEFEED 1h INCLUDE ORIGINAL PERMISSIONS FOR select WHERE in = $auth.id OR "
        "out = $auth.id, FOR create FULL, FOR update, delete NONE;",
    ),
    ("K2", _K2_STORED, "DEFINE TABLE OVERWRITE `t` TYPE RELATION IN `person` OUT `post` | `book` ENFORCED DROP;"),
    (
        "K3",
        _K3_STORED,
        "DEFINE TABLE OVERWRITE `t` TYPE RELATION IN `person` OUT `post` | `book` ENFORCED PERMISSIONS FULL;",
    ),
    ("K4", _K4_STORED, "DEFINE TABLE OVERWRITE `t` TYPE RELATION IN `person` OUT `post` | `book` ENFORCED;"),
    (
        "K5",
        _K5_STORED,
        "DEFINE TABLE OVERWRITE `t` TYPE RELATION IN `person` OUT `post` | `book` ENFORCED SCHEMAFULL PERMISSIONS FOR "
        "select FULL, FOR create, update, delete NONE;",
    ),
]

#: K6 (spec §5.5, AC-6, M17): ``rel_likes`` as AC-6's fixture stores it, ``RelLikes``'s carried statement, and the
#: definition SurrealDB stores after it — the same text on the four engines. Tasks 4 and 6 assert them end to end.
_K6_STORED = (
    "DEFINE TABLE rel_likes TYPE ANY SCHEMAFULL COMMENT 'keep' CHANGEFEED 1h INCLUDE ORIGINAL PERMISSIONS FOR select "
    "WHERE in = $auth.id, FOR create FULL, FOR update, delete NONE"
)
_K6 = (
    "DEFINE TABLE OVERWRITE `rel_likes` TYPE RELATION IN `rel_person` OUT `rel_post` | `rel_book` ENFORCED SCHEMAFULL "
    "COMMENT 'keep' CHANGEFEED 1h INCLUDE ORIGINAL PERMISSIONS FOR select WHERE in = $auth.id, FOR create FULL, FOR "
    "update, delete NONE;"
)
_K6_READ_BACK = (
    "DEFINE TABLE rel_likes TYPE RELATION IN rel_person OUT rel_post | rel_book ENFORCED SCHEMAFULL COMMENT 'keep' "
    "CHANGEFEED 1h INCLUDE ORIGINAL PERMISSIONS FOR select WHERE in = $auth.id, FOR create FULL, FOR update, delete NONE"
)

#: K7 (spec §5.5, M19): K6's read-back carried with the keyless clause — the statement that removes the constraint.
_K7 = (
    "DEFINE TABLE OVERWRITE `rel_likes` TYPE RELATION SCHEMAFULL COMMENT 'keep' CHANGEFEED 1h INCLUDE ORIGINAL "
    "PERMISSIONS FOR select WHERE in = $auth.id, FOR create FULL, FOR update, delete NONE;"
)

_TABLE_OPERATIONS = ("select", "create", "update", "delete")

#: (permissions, text) — the generator's cases (``tests/test_model_generator.py``) and K1's rules: SurrealDB's
#: rendering, operations in SurrealQL's order, grouped by rule.
_PERMISSIONS_TEXTS: list[tuple[dict[str, str], str | None]] = [
    (dict.fromkeys(_TABLE_OPERATIONS, "NONE"), None),
    ({}, None),
    (dict.fromkeys(_TABLE_OPERATIONS, "FULL"), "FULL"),
    (
        {"select": "WHERE in = $auth.id OR out = $auth.id", "create": "FULL", "update": "NONE", "delete": "NONE"},
        "FOR select WHERE in = $auth.id OR out = $auth.id, FOR create FULL, FOR update, delete NONE",
    ),
    (dict.fromkeys(_TABLE_OPERATIONS, "WHERE a > 1"), "FOR select, create, update, delete WHERE a > 1"),
    ({"select": "FULL"}, "FOR select FULL"),
    (
        {"delete": "FULL", "update": "NONE", "create": "WHERE a, b", "select": "WHERE a, b"},
        "FOR select, create WHERE a, b, FOR update NONE, FOR delete FULL",
    ),
]

#: A stored value that fails its check (spec §6.1, §8): the carried statement is ``None`` (RG2). Plain malformed values.
_UNCARRIABLE: list[tuple[str, TableState]] = [
    ("top-level-semicolon", TableState(name="t", permissions={"select": "WHERE a; b"})),
    ("comment-marker", TableState(name="t", permissions={"select": "WHERE a -- c"})),
    ("nul-in-comment", TableState(name="t", comment="a\x00b")),
    ("unterminated-literal", TableState(name="t", permissions={"update": "WHERE a = 'b"})),
    ("unbalanced-bracket", TableState(name="t", permissions={"create": "WHERE (a"})),
    ("not-a-rule", TableState(name="t", permissions={"delete": "SOMETIMES"})),
]


class TestCarriedStatement:
    """AC-4: ``relation_statement_keeping()`` re-renders a stored definition's other clauses after the model's type
    clause — K1–K7 exactly —, ``None`` when a stored value fails its check; ``table_permissions_text()`` is the
    generator's renderer, moved; the guard's refusal texts RG1–RG3."""

    @pytest.mark.parametrize(("permissions", "text"), _PERMISSIONS_TEXTS, ids=repr)
    def test_table_permissions_text(self, permissions: dict[str, str], text: str | None) -> None:
        assert schema.table_permissions_text(permissions) == text

    def test_the_generator_uses_the_moved_renderer(self) -> None:
        assert not hasattr(model_generator, "_permissions_text")
        assert not hasattr(model_generator, "_OPERATIONS")
        assert model_generator.table_permissions_text is schema.table_permissions_text

    @pytest.mark.parametrize(("stored", "statement"), [case[1:] for case in _K_CASES], ids=[case[0] for case in _K_CASES])
    def test_k1_to_k5(self, stored: str, statement: str) -> None:
        assert schema.relation_statement_keeping("t", _G1_CLAUSE, parse_define_table(stored)) == statement

    def test_k1_comment_is_requoted(self) -> None:
        """The server's double-quoted comment is read back unquoted, then written as a single-quoted literal."""
        state = parse_define_table(_K1_STORED)
        assert state.comment == 'it\'s \\ a "c" line'
        statement = schema.relation_statement_keeping("t", _G1_CLAUSE, state)
        assert statement is not None
        assert " COMMENT 'it\\'s \\\\ a \"c\" line' " in statement

    def test_k4_defaults_only_is_the_bare_statement(self) -> None:
        state = parse_define_table(_K4_STORED)
        assert schema.relation_statement_keeping("t", _G1_CLAUSE, state) == schema.relation_statement("t", _G1_CLAUSE, True)
        assert schema.relation_statement_keeping("likes", _G1_CLAUSE, state) == G1Likes.relation_ddl() == _G1

    def test_k6(self) -> None:
        clause = schema.relation_type_clause(RelLikes.get_relation_in(), RelLikes.get_relation_out(), RelLikes.is_enforced())
        assert schema.relation_statement_keeping("rel_likes", clause, parse_define_table(_K6_STORED)) == _K6

    def test_k6_again_on_its_read_back(self) -> None:
        """Applied twice, the carried statement is the same: K6's read-back carries the same clauses."""
        clause = schema.relation_type_clause(RelLikes.get_relation_in(), RelLikes.get_relation_out(), RelLikes.is_enforced())
        assert schema.relation_statement_keeping("rel_likes", clause, parse_define_table(_K6_READ_BACK)) == _K6

    def test_k7_the_keyless_clause_removes_the_constraint(self) -> None:
        assert schema.relation_statement_keeping("rel_likes", "TYPE RELATION", parse_define_table(_K6_READ_BACK)) == _K7

    def test_clause_order(self) -> None:
        """The type clause, ``DROP``, ``SCHEMAFULL``, ``COMMENT``, ``CHANGEFEED``, ``PERMISSIONS`` (spec §5.5)."""
        state = TableState(
            name="t",
            schema_mode="SCHEMAFULL",
            changefeed="2d",
            permissions={"select": "FULL", "create": "FULL", "update": "FULL", "delete": "FULL"},
            comment="c",
            drop=True,
        )
        assert schema.relation_statement_keeping("t", "TYPE RELATION", state) == (
            "DEFINE TABLE OVERWRITE `t` TYPE RELATION DROP SCHEMAFULL COMMENT 'c' CHANGEFEED 2d PERMISSIONS FULL;"
        )

    def test_a_rule_holding_literals_and_brackets_is_carried(self) -> None:
        """String literals are data and brackets balance: a ``WHERE`` rule holding ``, FOR`` in a literal and an array
        (M10) is carried as stored."""
        rule = "WHERE name = 'a, FOR b' AND tags CONTAINSANY [1, 2]"
        state = TableState(name="t", schema_mode="SCHEMALESS", permissions={"select": rule, "create": "NONE"})
        assert schema.relation_statement_keeping("t", "TYPE RELATION", state) == (
            f"DEFINE TABLE OVERWRITE `t` TYPE RELATION PERMISSIONS FOR select {rule}, FOR create NONE;"
        )

    @pytest.mark.parametrize("state", [case[1] for case in _UNCARRIABLE], ids=[case[0] for case in _UNCARRIABLE])
    def test_none_for_a_stored_value_that_fails_its_check(self, state: TableState) -> None:
        assert schema.relation_statement_keeping("t", _G1_CLAUSE, state) is None

    @pytest.mark.parametrize(("table", "error"), [("a-b", ValueError), (3, TypeError)], ids=repr)
    def test_the_table_name_is_checked(self, table: Any, error: type[Exception]) -> None:
        with pytest.raises(error):
            schema.relation_statement_keeping(table, _G1_CLAUSE, parse_define_table(_K4_STORED))

    @pytest.mark.parametrize(("guard", "text"), [("RG1", _rg1), ("RG2", _rg2), ("RG3", _rg3)])
    def test_guard_texts(self, guard: Any, text: Any) -> None:
        assert schema.relation_table_refused(guard, "Likes", "likes") == text("Likes", "likes")

    def test_guard_texts_exactly(self) -> None:
        """RG1–RG3 of spec §5.8, word for word."""
        assert schema.relation_table_refused("RG1", "Likes", "likes") == (
            "Likes.define_relation(): the table 'likes' is a materialized view; defining it as a relation table would "
            "drop its query and turn it into an ordinary table that no longer updates. Give Likes another table_name, "
            "or remove the view first. Nothing was defined."
        )
        assert schema.relation_table_refused("RG2", "Likes", "likes") == (
            "Likes.define_relation(): the definition of the table 'likes' could not be read, so the ORM cannot keep its "
            "other clauses (schema mode, permissions, change feed, comment). Nothing was defined."
        )
        assert schema.relation_table_refused("RG3", "Likes", "likes") == (
            "Likes.define_relation(overwrite=False): the table 'likes' exists and is not a relation table; DEFINE TABLE "
            "IF NOT EXISTS would leave it as it is, and the relation constraint would not apply. Call define_relation() "
            "with overwrite=True, which keeps the table's other clauses. Nothing was defined."
        )


# === E2E (spec §9.2): the connection "rel" on the database rel_db, over WebSocket and HTTP, and mem:// ===========

#: The engines every E2E class runs on: the line under test over WebSocket and HTTP, and the embedded engine.
_KINDS = ["ws", "http", "mem"]

#: The reset of spec §9.2, in one query.
_REL_RESET = "REMOVE DATABASE IF EXISTS rel_db; DEFINE DATABASE rel_db;"


def _url(kind: str) -> str:
    """The URL of *kind*: ``ws`` or ``http`` on the line under test (``SURREALDB_PORT``), or ``mem``."""
    if kind == "mem":
        return "mem://"
    host = os.environ.get("SURREALDB_HOST", "localhost")
    port = os.environ.get("SURREALDB_PORT", "8000")
    return f"ws://{host}:{port}/rpc" if kind == "ws" else f"http://{host}:{port}"


def _register_rel(kind: str, name: str = "rel") -> None:
    if kind == "mem":
        SurrealDBConnectionManager.add_connection(name, url="mem://", namespace="ns", database="rel_db")
    else:
        SurrealDBConnectionManager.add_connection(
            name, url=_url(kind), user="root", password="root", namespace="ns", database="rel_db"
        )


async def _apply_ddl(client: Any, ddl: str) -> None:
    """Run *ddl* and check every statement: ``query()`` checks only the first one."""
    response = await client.query_raw(ddl, {})
    assert "error" not in response, response["error"]
    refused = [result for result in response["result"] if result["status"] != "OK"]
    assert response["result"] and not refused, refused


@contextlib.asynccontextmanager
async def _rel(kind: str) -> AsyncIterator[Any]:
    """The connection ``"rel"`` on *kind*, its database ``rel_db`` reset in one query; removed, then unregistered, on
    exit (spec §9.2)."""
    _register_rel(kind)
    client = await SurrealDBConnectionManager.get_client("rel")
    await _apply_ddl(client, _REL_RESET)
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            cleanup = await SurrealDBConnectionManager.get_client("rel")
            await cleanup.query("REMOVE DATABASE IF EXISTS rel_db;", {})
        await SurrealDBConnectionManager.remove_connection("rel")


async def _tables(client: Any) -> dict[str, Any]:
    """The ``tables`` of ``INFO FOR DB`` on *client*'s database."""
    return dict((await client.query("INFO FOR DB;", {}))["tables"])


class TestRelationDdlE2E:
    """AC-3 (E2E): each golden applied raw on every engine of both lines is accepted and read back by
    ``parse_define_table()`` as a relation table whose ends and ``ENFORCED`` equal the model's getters (M1, M2, M16),
    the keyword names of G6 and the repeated name of G8 included (M22)."""

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize(("model", "overwrite", "statement"), _GOLDENS, ids=_GOLDEN_IDS)
    async def test_each_golden_is_applied_and_read_back(
        self, kind: str, model: type[BaseSurrealEdge], overwrite: bool, statement: str
    ) -> None:
        async with _rel(kind) as client:
            ddl = model.relation_ddl(overwrite=overwrite)
            assert ddl == statement
            await _apply_ddl(client, ddl)
            table = model.get_table_name()
            state = parse_define_table((await _tables(client))[table])
            assert state.name == table
            assert state.table_type == "relation"
            assert state.relation_in == model.get_relation_in()
            assert state.relation_out == model.get_relation_out()
            assert state.enforced is model.is_enforced()

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_g8_keeps_the_repeated_name(self, kind: str) -> None:
        """M22: ``IN `a` | `a``` is stored and read back with the name twice, as ``get_relation_in()`` returns it."""
        async with _rel(kind) as client:
            await _apply_ddl(client, G8Twice.relation_ddl())
            state = parse_define_table((await _tables(client))["e8"])
            assert state.relation_in == ("a", "a") == G8Twice.get_relation_in()
            assert state.relation_out == ()
            assert state.enforced is False

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_reapplying_a_golden_changes_nothing(self, kind: str) -> None:
        """The statement is the same text every time; applied twice (``OVERWRITE``, then ``IF NOT EXISTS``), the table
        reads back the same definition."""
        async with _rel(kind) as client:
            await _apply_ddl(client, G1Likes.relation_ddl())
            first = (await _tables(client))["likes"]
            await _apply_ddl(client, G1Likes.relation_ddl())
            await _apply_ddl(client, G1Likes.relation_ddl(overwrite=False))
            assert (await _tables(client))["likes"] == first
