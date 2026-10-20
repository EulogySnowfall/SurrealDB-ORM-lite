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

``define_relation()`` and its guard (spec §5.4, §5.6, §5.8 RG1–RG3 S1–S4 E1, §7; AC-5, AC-6, AC-9, AC-11, AC-15; D8,
D9, D11, P2): ``TestRelationGuard`` drives it on fake clients — one ``INFO FOR DB``, then the bare, carried or ``IF NOT
EXISTS`` statement, RG1–RG3 before any DDL, S1–S4 naming the table and never the statement, a ``tx`` of another
connection and E1 before any request. ``TestDefineRelationE2E`` applies it on every engine: on a missing table (G1's
form, idempotent), on AC-6's table — K6 exactly, its other clauses, fields, index, event, edges and the view that reads
it kept, the view still counting, a wrong-table endpoint refused at once on a second connection too —, RG1 on a view,
RG3 on a ``TYPE ANY`` table, ``overwrite=False`` leaving a relation table's ends. ``TestRelationPermissionsE2E``: a
record user gets S1. The query cache classifies it as DDL that changes no row (``tests/test_query_cache.py``).

Edge creation refusals (spec §5.7, §5.8 W1–W2, §6.3, §6.4; AC-7, AC-8; D6, D7, D15): ``utils.edge_write_error()`` is
v0.22.0's rule of ``insert_edges()``, moved and shared — ``SurrealDbNotFoundError`` for a missing endpoint, else
``SurrealDbError`` (``TestEdgeWriteError``) —, and the string ``relate()`` raises it outside a transaction, the SDK's
error as its ``__cause__`` (``TestStringRelateError``). ``TestEnforcementE2E``: after ``define_relation()`` every ORM
edge creation path — the typed and the string ``relate()``, an edge's ``save()``, ``bulk_create()`` — raises W1 or W2
and writes nothing, the class of an endpoint both outside ``IN``/``OUT`` and missing pinned per line; a node model's
``save()`` into the relation table keeps the SDK's error.

Removing a constraint (spec §5.4, G6, D14, P1; AC-20; M18, M19): ``define_relation()`` of an edge model without keys —
another model on the table, or a subclass resetting them to ``None`` or emptying them — sends K7, ``TYPE RELATION`` with
no ends and the table's other clauses carried (``TestRemoveConstraint``). ``TestRemoveConstraintE2E``: on AC-6's table
after K6, the table reads back without ends, ``in``/``out`` as ``TYPE record``, its clauses, fields, index, event, view
and edges kept; edges from another table and to a missing record are accepted at once, on a second connection too;
``RelLikes.define_relation()`` puts K6 back; ``overwrite=False`` removes nothing; in a transaction the removal is
applied at commit, and not at all when the block raises.
"""

import contextlib
import inspect
import os
import re
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from surrealdb import InternalError, NotAllowedError, NotFoundError, RecordID, ServerError

import surreal_orm_lite
from surreal_orm_lite import (
    BaseSurrealEdge,
    BaseSurrealModel,
    SurrealConfigDict,
    SurrealDbAuthenticationError,
    SurrealDBConnectionManager,
    SurrealDbError,
    SurrealDbNotFoundError,
    SurrealORMError,
    TableType,
    Transaction,
    is_conflict_error,
    model_generator,
    schema,
    utils,
)
from surreal_orm_lite.define_parser import parse_define_field, parse_define_table
from surreal_orm_lite.edge import insert_edges
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


# === define_relation() and its table guard (spec §5.4, §5.6, §5.8 RG1–RG3 S1–S4 E1; AC-9, AC-11) =================

#: The guard's one read.
_INFO = "INFO FOR DB;"

#: A record user's refusal, as the servers word it.
_PERMISSIONS = "IAM error: Not enough permissions to perform this action"

#: Stored definitions of ``likes`` as ``INFO FOR DB`` renders them: a table the first write created, a ``TYPE
#: NORMAL`` table, a ``SCHEMAFULL`` table with other clauses, a view, a definition the parser cannot read, a rule
#: that fails its check (a top-level ``;``), a relation table with other ends.
_STORED_DEFAULT = "DEFINE TABLE likes TYPE ANY SCHEMALESS PERMISSIONS NONE"
_STORED_NORMAL = "DEFINE TABLE likes TYPE NORMAL SCHEMALESS PERMISSIONS NONE"
_STORED_SCHEMAFULL = (
    "DEFINE TABLE likes TYPE NORMAL SCHEMAFULL COMMENT 'keep' CHANGEFEED 1h PERMISSIONS FOR select FULL, FOR create, "
    "update, delete NONE"
)
_STORED_VIEW = "DEFINE TABLE likes TYPE ANY SCHEMALESS AS SELECT count() AS n FROM person GROUP ALL PERMISSIONS NONE"
_STORED_UNREADABLE = "DEFINE TABLE likes TYPE ???"
_STORED_BAD_RULE = "DEFINE TABLE likes TYPE ANY SCHEMALESS PERMISSIONS FOR select WHERE a; b, FOR create, update, delete NONE"
_STORED_OTHER_ENDS = "DEFINE TABLE likes TYPE RELATION IN company OUT post SCHEMALESS PERMISSIONS NONE"

#: ``G1Likes``' carried statement on :data:`_STORED_SCHEMAFULL` (the K-form).
_SCHEMAFULL_KEPT = (
    "DEFINE TABLE OVERWRITE `likes` TYPE RELATION IN `person` OUT `post` | `book` ENFORCED SCHEMAFULL COMMENT 'keep' "
    "CHANGEFEED 1h PERMISSIONS FOR select FULL, FOR create, update, delete NONE;"
)

#: ``G1Likes.define_relation()`` on each stored ``likes``: (case, stored definition, overwrite, the statements
#: sent, the guard that refuses or ``None``). Every call reads ``INFO FOR DB`` once; a refusal sends nothing else.
_RELATION_GUARD: list[tuple[str, str | None, bool, list[str], str | None]] = [
    ("missing", None, True, [_INFO, _G1], None),
    ("missing", None, False, [_INFO, _G1_KEEP], None),
    ("default", _STORED_DEFAULT, True, [_INFO, _G1], None),
    ("default", _STORED_DEFAULT, False, [_INFO], "RG3"),
    ("normal", _STORED_NORMAL, True, [_INFO, _G1], None),
    ("normal", _STORED_NORMAL, False, [_INFO], "RG3"),
    ("schemafull", _STORED_SCHEMAFULL, True, [_INFO, _SCHEMAFULL_KEPT], None),
    ("schemafull", _STORED_SCHEMAFULL, False, [_INFO], "RG3"),
    ("view", _STORED_VIEW, True, [_INFO], "RG1"),
    ("view", _STORED_VIEW, False, [_INFO], "RG1"),
    ("unreadable", _STORED_UNREADABLE, True, [_INFO], "RG2"),
    ("unreadable", _STORED_UNREADABLE, False, [_INFO], "RG2"),
    ("bad-rule", _STORED_BAD_RULE, True, [_INFO], "RG2"),
    ("bad-rule", _STORED_BAD_RULE, False, [_INFO], "RG3"),
    ("other-ends", _STORED_OTHER_ENDS, True, [_INFO, _G1], None),
    ("other-ends", _STORED_OTHER_ENDS, False, [_INFO, _G1_KEEP], None),
]

_GUARD_TEXTS = {"RG1": _rg1, "RG2": _rg2, "RG3": _rg3}


class _GuardClient:
    """A client answering ``INFO FOR DB;`` with ``{"tables": tables}`` and every other statement with ``[]``; it
    records each statement and raises the error given for the ``INFO`` or the DDL (``tests/test_views.py``'s
    pattern). ``version()`` is counted: the guard never reads it."""

    def __init__(
        self,
        tables: dict[str, object] | None = None,
        *,
        info_error: BaseException | None = None,
        ddl_error: BaseException | None = None,
    ) -> None:
        self.info = {"tables": dict(tables or {}), "functions": {}}
        self.info_error = info_error
        self.ddl_error = ddl_error
        self.queries: list[str] = []
        self.version_calls = 0

    async def query(self, statement: str, variables: object) -> object:
        assert variables == {}
        self.queries.append(statement)
        if statement == _INFO:
            if self.info_error is not None:
                raise self.info_error
            return self.info
        if self.ddl_error is not None:
            raise self.ddl_error
        return []

    async def version(self) -> str:
        self.version_calls += 1
        return "surrealdb-3.3.4"


class _QueuingTx:
    """A transaction of the connection *name* whose ``add()`` records each statement."""

    def __init__(self, name: str = "default") -> None:
        self._connection_name = name
        self.added: list[tuple[str, object]] = []

    async def add(self, statement: str, variables: object) -> None:
        self.added.append((statement, variables))


def _guard_client(stored: str | None, **errors: Any) -> _GuardClient:
    """A fake client whose database holds ``person`` and, unless *stored* is ``None``, ``likes`` stored as *stored*."""
    tables: dict[str, object] = {"person": _STORED_DEFAULT.replace("likes", "person")}
    if stored is not None:
        tables["likes"] = stored
    return _GuardClient(tables, **errors)


@pytest.fixture
def use_client(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], None]:
    """Make ``SurrealDBConnectionManager.get_client()`` answer the client given."""

    def use(client: Any) -> None:
        async def get_client(*args: Any, **kwargs: Any) -> Any:
            return client

        monkeypatch.setattr(SurrealDBConnectionManager, "get_client", get_client)

    return use


class TestRelationGuard:
    """AC-9, AC-11 (unit): ``define_relation()`` on fake clients — one ``INFO FOR DB`` per call, then one statement:
    the bare one for a missing table, the carried one for a table that exists, ``IF NOT EXISTS`` for
    ``overwrite=False`` on a missing table or a relation table; RG1–RG3 before any DDL; S1/S2 for a refused ``INFO``,
    S3/S4 for a refused DDL, naming the table and never the statement; a ``tx`` of another connection and E1 before
    any request."""

    def test_signature(self) -> None:
        function = inspect.getattr_static(BaseSurrealEdge, "define_relation")
        assert isinstance(function, classmethod)
        assert inspect.iscoroutinefunction(function.__func__)
        assert not getattr(function.__func__, "__surreal_cache_writes__", False)
        assert inspect.signature(BaseSurrealEdge.define_relation) == inspect.Signature(
            [
                inspect.Parameter("overwrite", inspect.Parameter.KEYWORD_ONLY, default=True, annotation=bool),
                inspect.Parameter("tx", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Transaction | None),
            ],
            return_annotation=str,
        )

    @pytest.mark.parametrize(
        ("stored", "overwrite", "sent", "guard"),
        [case[1:] for case in _RELATION_GUARD],
        ids=[f"{case[0]}-{'overwrite' if case[2] else 'if-not-exists'}" for case in _RELATION_GUARD],
    )
    async def test_the_requests_sent(
        self,
        use_client: Callable[[Any], None],
        stored: str | None,
        overwrite: bool,
        sent: list[str],
        guard: str | None,
    ) -> None:
        client = _guard_client(stored)
        use_client(client)
        if guard is None:
            assert await G1Likes.define_relation(overwrite=overwrite) == sent[-1]
        else:
            with pytest.raises(SurrealDbError) as raised:
                await G1Likes.define_relation(overwrite=overwrite)
            assert type(raised.value) is SurrealDbError
            assert str(raised.value) == _GUARD_TEXTS[guard]("G1Likes", "likes")
        assert client.queries == sent
        assert client.version_calls == 0

    async def test_a_transaction_gets_the_ddl_only(self, use_client: Callable[[Any], None]) -> None:
        """Spec §5.4, §7: the guard reads committed state on the client, before anything is queued; the DDL — the
        carried statement — is queued in the transaction."""
        client = _guard_client(_STORED_SCHEMAFULL)
        use_client(client)
        tx = _QueuingTx()
        assert await G1Likes.define_relation(tx=tx) == _SCHEMAFULL_KEPT  # type: ignore[arg-type]
        assert client.queries == [_INFO]
        assert tx.added == [(_SCHEMAFULL_KEPT, None)]

    async def test_a_transaction_of_another_connection(self, use_client: Callable[[Any], None]) -> None:
        client = _guard_client(None)
        use_client(client)
        tx = _QueuingTx("other")
        with pytest.raises(SurrealDbError) as raised:
            await G1Likes.define_relation(tx=tx)  # type: ignore[arg-type]
        assert type(raised.value) is SurrealDbError
        assert str(raised.value).startswith("G1Likes.define_relation() cannot run in this transaction: ")
        assert client.queries == []
        assert tx.added == []

    @pytest.mark.parametrize("overwrite", ["yes", 1, None], ids=repr)
    async def test_e1_before_any_request(self, use_client: Callable[[Any], None], overwrite: Any) -> None:
        client = _guard_client(None)
        use_client(client)
        with pytest.raises(TypeError) as raised:
            await G1Likes.define_relation(overwrite=overwrite)
        assert str(raised.value) == f"overwrite= must be a bool, got {type(overwrite).__name__}."
        assert client.queries == []

    @pytest.mark.parametrize(
        ("error", "refusal"),
        [
            (InternalError("Internal", _PERMISSIONS), SurrealDbAuthenticationError),
            (NotAllowedError("NotAllowed", _PERMISSIONS), SurrealDbAuthenticationError),
            (InternalError("Internal", "There was a problem with the database"), SurrealDbError),
            (RuntimeError("the engine is closed"), SurrealDbError),
        ],
        ids=["s1-internal", "s1-not-allowed", "s2-internal", "s2-runtime"],
    )
    async def test_s1_s2_a_refused_info(
        self, use_client: Callable[[Any], None], error: BaseException, refusal: type[SurrealDbError]
    ) -> None:
        client = _GuardClient(info_error=error)
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await G1Likes.define_relation()
        assert type(raised.value) is refusal
        assert str(raised.value) == f"Can't apply relation definition: 'likes' -> {error}"
        assert raised.value.__cause__ is error
        assert client.queries == [_INFO]

    async def test_s3_a_refused_ddl(self, use_client: Callable[[Any], None]) -> None:
        error = NotAllowedError("NotAllowed", _PERMISSIONS)
        client = _guard_client(_STORED_SCHEMAFULL, ddl_error=error)
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await G1Likes.define_relation()
        assert type(raised.value) is SurrealDbAuthenticationError
        assert str(raised.value) == f"Can't apply relation definition: 'likes' -> {_PERMISSIONS}"
        assert raised.value.__cause__ is error
        assert client.queries == [_INFO, _SCHEMAFULL_KEPT]

    @pytest.mark.parametrize(
        ("reason", "text"),
        [
            ("The table 'likes' is locked", "The table 'likes' is locked"),
            (
                "Parse error: Unexpected token `ENFORCED`\n --> [1:72]\n  |\n1 | DEFINE TABLE OVERWRITE `likes` TYPE "
                "RELATION IN `person` OUT `post` | `book` ENFORCED SCHEMAFULL",
                "Parse error: Unexpected token `ENFORCED` (at line 1, column 72 of the statement)",
            ),
            (
                "Found a problem in TYPE RELATION IN `person` OUT `post` | `book` ENFORCED SCHEMAFULL COMMENT 'keep'",
                "the server's message quotes the statement, so it is left out",
            ),
        ],
        ids=["whole", "parse-excerpt", "quoting"],
    )
    async def test_s4_never_quotes_the_statement(self, use_client: Callable[[Any], None], reason: str, text: str) -> None:
        error = InternalError("Internal", reason)
        client = _guard_client(_STORED_SCHEMAFULL, ddl_error=error)
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await G1Likes.define_relation()
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == f"Can't apply relation definition: 'likes' -> {text}"
        assert "PERMISSIONS FOR select" not in str(raised.value)
        assert client.queries == [_INFO, _SCHEMAFULL_KEPT]

    async def test_the_view_guard_texts_are_unchanged(self) -> None:
        """``read_table_definitions()``'s new ``what=`` keyword keeps v0.32.0's texts by default."""
        client = _GuardClient(info_error=InternalError("Internal", _PERMISSIONS))
        with pytest.raises(SurrealDbAuthenticationError) as view:
            await schema.read_table_definitions(client, "order_stats", kind="definition")
        assert str(view.value) == f"Can't apply view definition: 'order_stats' -> {_PERMISSIONS}"
        with pytest.raises(SurrealDbAuthenticationError) as relation:
            await schema.read_table_definitions(client, "likes", kind="definition", what="relation")
        assert str(relation.value) == f"Can't apply relation definition: 'likes' -> {_PERMISSIONS}"


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


# --- define_relation() on the engines (spec §5.4, §5.6; AC-5, AC-6, AC-9, AC-11; M4, M6, M9, M17, M23) -------------


class RelPerson(BaseSurrealModel):
    """A node of the E2E classes: ``rel_likes``' ``IN`` table."""

    model_config = SurrealConfigDict(table_name="rel_person", connection="rel")

    id: str | None = None


class RelPost(BaseSurrealModel):
    """A node of the E2E classes: one of ``rel_likes``' ``OUT`` tables."""

    model_config = SurrealConfigDict(table_name="rel_post", connection="rel")

    id: str | None = None


class RelBook(BaseSurrealModel):
    """A node of the E2E classes: the other ``OUT`` table of ``rel_likes``."""

    model_config = SurrealConfigDict(table_name="rel_book", connection="rel")

    id: str | None = None


class RelCompany(BaseSurrealModel):
    """A node of the E2E classes, outside ``rel_likes``' ends."""

    model_config = SurrealConfigDict(table_name="rel_company", connection="rel")

    id: str | None = None


class RelStats(BaseSurrealEdge):
    """An edge model whose table is a view (AC-9, RG1)."""

    model_config = SurrealConfigDict(table_name="rel_stats", connection="rel", relation_in="rel_person")


#: ``RelLikes``' bare statement (G1's form on the ``rel_`` tables) and SurrealDB's rendering of it (M2).
_REL_G1 = "DEFINE TABLE OVERWRITE `rel_likes` TYPE RELATION IN `rel_person` OUT `rel_post` | `rel_book` ENFORCED;"
_REL_G1_READ_BACK = (
    "DEFINE TABLE rel_likes TYPE RELATION IN rel_person OUT rel_post | rel_book ENFORCED SCHEMALESS PERMISSIONS NONE"
)

#: AC-6's fixture, as written in spec AC-6 (M17): ``TYPE ANY`` spelled out — a ``SCHEMAFULL`` table without ``TYPE``
#: is stored ``TYPE NORMAL`` and refuses the edges —, and ``in``/``out`` defined before the ``UNIQUE`` index — a
#: ``SCHEMAFULL`` table refuses an index on undefined fields.
_AC6_FIXTURE = (
    "DEFINE TABLE rel_likes TYPE ANY SCHEMAFULL COMMENT 'keep' CHANGEFEED 1h INCLUDE ORIGINAL "
    "PERMISSIONS FOR select WHERE in = $auth.id, FOR create FULL; "
    "DEFINE FIELD in ON rel_likes TYPE record; "
    "DEFINE FIELD out ON rel_likes TYPE record; "
    "DEFINE FIELD stars ON rel_likes TYPE int DEFAULT 1; "
    "DEFINE INDEX rel_likes_pair ON rel_likes FIELDS in, out UNIQUE; "
    "DEFINE EVENT rel_likes_ev ON rel_likes WHEN $event = 'CREATE' THEN {};"
)

#: AC-6's two raw edges, with explicit ids.
_AC6_EDGES = (
    "INSERT RELATION INTO rel_likes [{id: 'e1', in: rel_person:ada, out: rel_post:p1, stars: 3}, "
    "{id: 'e2', in: rel_person:ada, out: rel_book:b1}];"
)

#: The view of P2 (M23): it reads ``rel_likes``.
_AC6_VIEW = "DEFINE TABLE rel_like_counts AS SELECT count() AS n, out FROM rel_likes GROUP BY out;"

#: A record access on ``rel_db``: a user signed up through it is a record user, who may not define (AC-11).
_REL_ACCESS = (
    "DEFINE ACCESS rel_acc ON DATABASE TYPE RECORD SIGNUP (CREATE rel_user SET name = $name) "
    "SIGNIN (SELECT * FROM rel_user WHERE name = $name);"
)


async def _line(client: Any) -> tuple[int, int]:
    """``(major, minor)`` of the engine *client* talks to: ``"surrealdb-3.1.5"`` → ``(3, 1)``; ``mem://`` →
    ``(2, 0)`` (the SDK's version, whose core is 2.x)."""
    match = re.search(r"(\d+)\.(\d+)", await client.version())
    assert match is not None
    return int(match.group(1)), int(match.group(2))


async def _major(client: Any) -> int:
    """The major version of the engine *client* talks to: ``"surrealdb-3.3.4"`` → 3; ``mem://`` → 2."""
    return (await _line(client))[0]


def _spy(monkeypatch: pytest.MonkeyPatch, client: Any) -> list[tuple[str, tuple[object, ...]]]:
    """Record every ``query``, ``query_raw`` and ``version`` call *client* receives, with its arguments (and let it
    through) — v0.31.0's spy."""
    calls: list[tuple[str, tuple[object, ...]]] = []

    def recording(method: str) -> Any:
        real = getattr(client, method)

        async def call(*args: object, **kwargs: object) -> object:
            calls.append((method, args))
            return await real(*args, **kwargs)

        return call

    for method in ("query", "query_raw", "version"):
        monkeypatch.setattr(client, method, recording(method))
    return calls


def _sent(calls: list[tuple[str, tuple[object, ...]]]) -> list[object]:
    """The statements *calls* (:func:`_spy`'s) sent through ``query()``, after checking that nothing else was sent: no
    ``version()``, and a ``query_raw()`` only for those statements (the SDK's ``query()`` runs it itself)."""
    statements = [args[0] for method, args in calls if method == "query"]
    assert [method for method, _ in calls if method == "version"] == []
    assert [args[0] for method, args in calls if method == "query_raw"] == statements
    return statements


async def _table_info(client: Any, table: str) -> dict[str, Any]:
    """``INFO FOR TABLE`` *table* (a constant name of these tests)."""
    info: dict[str, Any] = await client.query(f"INFO FOR TABLE {table};", {})
    return info


async def _edge_ids(client: Any) -> list[str]:
    """The ids of ``rel_likes``' edges, ordered, as ``table:id`` texts."""
    return [str(edge) for edge in await client.query("SELECT VALUE id FROM rel_likes ORDER BY id;", {})]


async def _like_counts(client: Any) -> dict[str, int]:
    """The view ``rel_like_counts``: ``out`` (as a ``table:id`` text) → ``n``."""
    return {str(row["out"]): row["n"] for row in await client.query("SELECT n, out FROM rel_like_counts;", {})}


async def _ac6(client: Any) -> dict[str, BaseSurrealModel]:
    """Spec AC-6's fixture on ``rel_db``: the table, its fields, index and event (each statement checked), the five
    nodes saved through the ORM, the edges ``e1`` and ``e2`` written raw, then the view of P2 — and the stored
    definition is K6's stored text. Returns the nodes by name."""
    await _apply_ddl(client, _AC6_FIXTURE)
    nodes: dict[str, BaseSurrealModel] = {
        "ada": await RelPerson(id="ada").save(),
        "bob": await RelPerson(id="bob").save(),
        "p1": await RelPost(id="p1").save(),
        "b1": await RelBook(id="b1").save(),
        "c": await RelCompany(id="c").save(),
    }
    await _apply_ddl(client, _AC6_EDGES)
    await _apply_ddl(client, _AC6_VIEW)
    assert await _like_counts(client) == {"rel_post:p1": 1, "rel_book:b1": 1}
    assert (await _tables(client))["rel_likes"] == _K6_STORED
    return nodes


class TestDefineRelationE2E:
    """AC-5, AC-6, AC-9 (E2E, every engine of both lines): ``define_relation()`` reads ``INFO FOR DB`` once, then
    sends the bare statement on a missing table and the carried one (K6) on an existing table — whose other clauses,
    fields, indexes, events, edges and the views that read it are kept, and which refuses a wrong-table endpoint at
    once, on every connection —; RG1 on a view, RG3 for ``overwrite=False`` on a ``TYPE ANY`` table, and
    ``overwrite=False`` leaves a relation table's stored ends."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_ac5_on_a_missing_table(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _rel(kind) as client:
            calls = _spy(monkeypatch, client)
            assert await RelLikes.define_relation() == _REL_G1 == RelLikes.relation_ddl()
            assert _sent(calls) == [_INFO, _REL_G1]
            stored = (await _tables(client))["rel_likes"]
            assert stored == _REL_G1_READ_BACK
            state = parse_define_table(stored)
            assert state.table_type == "relation"
            assert state.relation_in == ("rel_person",)
            assert state.relation_out == ("rel_post", "rel_book")
            assert state.enforced is True
            calls.clear()
            assert await RelLikes.define_relation() == _REL_G1
            assert _sent(calls) == [_INFO, _REL_G1]
            assert (await _tables(client))["rel_likes"] == stored

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_ac6_on_an_existing_table(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _rel(kind) as client:
            nodes = await _ac6(client)
            if kind != "mem":
                _register_rel(kind, "rel2")
            try:
                if kind != "mem":
                    # The second connection's session is open before the define (M9).
                    second = await SurrealDBConnectionManager.get_client("rel2")
                    assert await second.query("SELECT VALUE id FROM rel_likes ORDER BY id;", {}) == [
                        RecordID("rel_likes", "e1"),
                        RecordID("rel_likes", "e2"),
                    ]
                calls = _spy(monkeypatch, client)
                assert await RelLikes.define_relation() == _K6
                assert _sent(calls) == [_INFO, _K6]
                tables = await _tables(client)
                assert tables["rel_likes"] == _K6_READ_BACK
                assert parse_define_table(tables["rel_like_counts"]).view_query is not None
                info = await _table_info(client, "rel_likes")
                assert {"in", "out", "stars"} <= set(info["fields"])
                assert "TYPE record<rel_person>" in info["fields"]["in"]
                assert "TYPE record<rel_post | rel_book>" in info["fields"]["out"]
                assert list(info["indexes"]) == ["rel_likes_pair"]
                assert list(info["events"]) == ["rel_likes_ev"]
                assert list(info["tables"]) == ["rel_like_counts"]
                assert await _edge_ids(client) == ["rel_likes:e1", "rel_likes:e2"]

                # P2, M23: the view that reads the table keeps updating.
                edge = await nodes["bob"].relate(RelLikes, nodes["p1"])
                assert (edge.in_, edge.out, edge.stars) == (RecordID("rel_person", "bob"), RecordID("rel_post", "p1"), 1)
                assert await _like_counts(client) == {"rel_post:p1": 2, "rel_book:b1": 1}

                # The UNIQUE index is kept.
                with pytest.raises(SurrealDbError) as duplicate:
                    await nodes["ada"].relate(RelLikes, nodes["p1"])
                assert type(duplicate.value) is SurrealDbError
                assert "rel_likes_pair" in str(duplicate.value)

                # A wrong-table endpoint is refused at once (M9), also on the second connection.
                with pytest.raises(SurrealDbError) as company:
                    await nodes["c"].relate(RelLikes, nodes["p1"])
                assert type(company.value) is SurrealDbError
                if kind != "mem":
                    async with SurrealDBConnectionManager.using("rel2"):
                        with pytest.raises(SurrealDbError) as other:
                            await nodes["c"].relate(RelLikes, nodes["b1"])
                    assert type(other.value) is SurrealDbError
                # e1, e2 and bob's edge (an auto id: compared as a set), nothing else.
                ids = await _edge_ids(client)
                assert len(ids) == 3
                assert {"rel_likes:e1", "rel_likes:e2", str(edge.id)} == set(ids)
                assert await _like_counts(client) == {"rel_post:p1": 2, "rel_book:b1": 1}
            finally:
                if kind != "mem":
                    await SurrealDBConnectionManager.remove_connection("rel2")

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_ac6_a_second_call_converges(self, kind: str) -> None:
        """Applied again, the carried statement is the same K6 and the table reads back the same."""
        async with _rel(kind) as client:
            await _ac6(client)
            assert await RelLikes.define_relation() == _K6
            assert await RelLikes.define_relation() == _K6
            assert (await _tables(client))["rel_likes"] == _K6_READ_BACK

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize("overwrite", [True, False], ids=["overwrite", "if-not-exists"])
    async def test_rg1_a_view(self, kind: str, overwrite: bool, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _rel(kind) as client:
            await RelPerson(id="ada").save()
            await _apply_ddl(client, "DEFINE TABLE rel_stats AS SELECT count() AS n FROM rel_person GROUP ALL;")
            before = (await _tables(client))["rel_stats"]
            calls = _spy(monkeypatch, client)
            with pytest.raises(SurrealDbError) as raised:
                await RelStats.define_relation(overwrite=overwrite)
            assert _sent(calls) == [_INFO]
            assert (type(raised.value), str(raised.value)) == (SurrealDbError, _rg1("RelStats", "rel_stats"))
            after = (await _tables(client))["rel_stats"]
            assert after == before
            assert parse_define_table(after).view_query is not None
            assert await client.query("SELECT VALUE n FROM rel_stats;", {}) == [1]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_rg3_a_type_any_table_holding_an_edge(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _rel(kind) as client:
            await _apply_ddl(client, "INSERT RELATION INTO rel_likes {id: 'e1', in: rel_person:ada, out: rel_post:p1};")
            before = (await _tables(client))["rel_likes"]
            assert parse_define_table(before).table_type == "any"
            calls = _spy(monkeypatch, client)
            with pytest.raises(SurrealDbError) as raised:
                await RelLikes.define_relation(overwrite=False)
            assert _sent(calls) == [_INFO]
            assert (type(raised.value), str(raised.value)) == (SurrealDbError, _rg3("RelLikes", "rel_likes"))
            assert (await _tables(client))["rel_likes"] == before
            assert await _edge_ids(client) == ["rel_likes:e1"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_overwrite_false_on_a_missing_table(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _rel(kind) as client:
            statement = RelLikes.relation_ddl(overwrite=False)
            calls = _spy(monkeypatch, client)
            assert await RelLikes.define_relation(overwrite=False) == statement
            assert _sent(calls) == [_INFO, statement]
            assert (await _tables(client))["rel_likes"] == _REL_G1_READ_BACK

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_overwrite_false_leaves_a_relation_tables_ends(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """D8: ``IF NOT EXISTS`` on a relation table with other ends is sent, and changes nothing."""
        async with _rel(kind) as client:
            await _apply_ddl(client, "DEFINE TABLE rel_likes TYPE RELATION IN rel_company OUT rel_post;")
            before = (await _tables(client))["rel_likes"]
            statement = RelLikes.relation_ddl(overwrite=False)
            calls = _spy(monkeypatch, client)
            assert await RelLikes.define_relation(overwrite=False) == statement
            assert _sent(calls) == [_INFO, statement]
            assert (await _tables(client))["rel_likes"] == before
            state = parse_define_table(before)
            assert (state.relation_in, state.relation_out, state.enforced) == (("rel_company",), ("rel_post",), False)


class TestRelationPermissionsE2E:
    """AC-11 (servers): a record user may not define a relation constraint — S1 from the guard's ``INFO FOR DB``, and
    nothing else is sent. The embedded engine's session is its owner, which may always define."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_s1_a_record_user(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        if kind == "mem":
            pytest.skip("the embedded engine's session is its owner: it may always define")
        async with _rel(kind) as client, SurrealDBConnectionManager.using("rel"):
            await _apply_ddl(client, _REL_ACCESS)
            await SurrealDBConnectionManager.signup(access="rel_acc", variables={"name": "rel-user"})
            try:
                calls = _spy(monkeypatch, client)
                with pytest.raises(SurrealDbError) as raised:
                    await RelLikes.define_relation()
                assert _sent(calls) == [_INFO]
            finally:
                await SurrealDBConnectionManager.invalidate()
            assert "rel_likes" not in await _tables(client)
        assert type(raised.value) is SurrealDbAuthenticationError
        assert str(raised.value).startswith("Can't apply relation definition: 'rel_likes' -> ")
        assert "not enough permissions" in str(raised.value).lower()


# === edge creation refusals (spec §5.7, §5.8 W1–W2, §6.3, §6.4; AC-7, AC-8; D6, D7, D15; M11, M12, M20) ============


def _w(message: str) -> str:
    """W1/W2's text (spec §5.8) for the server message *message*."""
    return f"There was a problem with the database: {message}"


def _sdk_error(kind: str, message: str) -> Exception:
    """A fresh SDK error of *kind* (``"NotFound"`` or ``"Internal"``), as the SDK raises it for a refused statement."""
    return NotFoundError(kind, message) if kind == "NotFound" else InternalError(kind, message)


#: (id, SDK error kind, server message, the ORM class it becomes): the refusals of an edge creation measured on the
#: four engines (M11), with the class ``edge_write_error()`` gives them — W2 for a missing endpoint (the SDK's
#: ``NotFoundError``, or "does not exist" in any case), W1 for anything else.
_EDGE_REFUSALS: list[tuple[str, str, str, type[SurrealDbError]]] = [
    ("3x-missing", "NotFound", "The record 'person:ghost' does not exist", SurrealDbNotFoundError),
    ("not-found-class", "NotFound", "The record is gone", SurrealDbNotFoundError),
    ("2x-missing", "Internal", "The record 'person:ghost' does not exist", SurrealDbNotFoundError),
    ("upper-case", "Internal", "The record 'person:ghost' DOES NOT EXIST", SurrealDbNotFoundError),
    (
        "2x-wrong-table",
        "Internal",
        "Found company:c for field `in`, with record `likes:e1`, but expected a record<person>",
        SurrealDbError,
    ),
    (
        "3x-wrong-table",
        "Internal",
        "Couldn't coerce value for field `in` of `likes:e1`: Expected `record<person>` but found `company:c`",
        SurrealDbError,
    ),
    (
        "unique-duplicate",
        "Internal",
        "Database index `likes_pair` already contains [person:ada, post:p1], with record `likes:e2`",
        SurrealDbError,
    ),
    ("type-normal", "Internal", "Found record: `likes:e1` which is a relation, but expected a NORMAL", SurrealDbError),
]
_EDGE_REFUSAL_IDS = [case[0] for case in _EDGE_REFUSALS]


class WriteNode(BaseSurrealModel):
    """A node on the default connection: the source of the string ``relate()``'s unit tests."""

    model_config = SurrealConfigDict(table_name="person")

    id: str | None = None


class _RefusingClient:
    """A client whose ``query()`` and ``insert_relation()`` record the call and raise *error*."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls: list[str] = []

    async def query(self, statement: str, variables: object) -> object:
        self.calls.append(statement)
        raise self.error

    async def insert_relation(self, table: str, data: object) -> object:
        self.calls.append(f"insert_relation({table!r})")
        raise self.error


class _RefusingTx:
    """A transaction of the default connection whose ``add()`` records the statement and raises *error*."""

    def __init__(self, error: BaseException) -> None:
        self._connection_name = "default"
        self.error = error
        self.added: list[str] = []

    async def add(self, statement: str, variables: object) -> object:
        self.added.append(statement)
        raise self.error


class TestEdgeWriteError:
    """AC-8 (unit): ``utils.edge_write_error()`` is v0.22.0's rule of ``insert_edges()``, moved and shared —
    ``SurrealDbNotFoundError`` for the SDK's ``NotFoundError`` or a "does not exist" message in any case, exactly
    ``SurrealDbError`` otherwise, the text ``There was a problem with the database: {error}`` (W1, W2) —, and
    ``insert_edges()`` raises it with the SDK's error as ``__cause__``, its classes and texts unchanged."""

    @pytest.mark.parametrize(("kind", "message", "expected"), [case[1:] for case in _EDGE_REFUSALS], ids=_EDGE_REFUSAL_IDS)
    def test_the_class_and_the_text(self, kind: str, message: str, expected: type[SurrealDbError]) -> None:
        error = _sdk_error(kind, message)
        result = utils.edge_write_error(error)
        assert type(result) is expected
        assert str(result) == _w(message)
        assert result.__cause__ is None  # the caller chains it: raise edge_write_error(exc) from exc

    def test_a_conflict_is_still_recognised(self) -> None:
        """``retry_on_conflict`` reads the text: a conflict refused the same way stays a conflict."""
        error = InternalError("Internal", "Transaction conflict: Resource busy. This transaction can be retried")
        result = utils.edge_write_error(error)
        assert type(result) is SurrealDbError
        assert is_conflict_error(result)

    @pytest.mark.parametrize("with_id", [False, True], ids=["insert_relation", "guarded-statement"])
    @pytest.mark.parametrize(("kind", "message", "expected"), [case[1:] for case in _EDGE_REFUSALS], ids=_EDGE_REFUSAL_IDS)
    async def test_insert_edges_raises_it(self, kind: str, message: str, expected: type[SurrealDbError], with_id: bool) -> None:
        """The typed paths' one write (``relate(EdgeModel, …)``, an edge's ``save()``, ``bulk_create()``)."""
        error = _sdk_error(kind, message)
        client = _RefusingClient(error)

        class _Manager:
            @classmethod
            async def get_client(cls) -> Any:
                return client

        row: dict[str, Any] = {"in": RecordID("person", "ada"), "out": RecordID("post", "p1")}
        if with_id:
            row["id"] = "e1"
        with pytest.raises(SurrealDbError) as raised:
            await insert_edges("likes", [row], tx=None, manager=_Manager)  # type: ignore[arg-type]
        assert type(raised.value) is expected
        assert str(raised.value) == _w(message)
        assert raised.value.__cause__ is error
        assert len(client.calls) == 1


class TestStringRelateError:
    """AC-8 (unit, D7): outside a transaction, ``relate("<edge table>", …)`` raises ``edge_write_error()`` of the SDK's
    error, which stays its ``__cause__`` — ``SurrealDbNotFoundError`` for a missing endpoint, ``SurrealDbError``
    otherwise; with ``tx=`` nothing changes: the transaction's own error propagates as it is."""

    @pytest.mark.parametrize(("kind", "message", "expected"), [case[1:] for case in _EDGE_REFUSALS], ids=_EDGE_REFUSAL_IDS)
    async def test_a_refusal_becomes_the_orms_error(
        self, use_client: Callable[[Any], None], kind: str, message: str, expected: type[SurrealDbError]
    ) -> None:
        error = _sdk_error(kind, message)
        client = _RefusingClient(error)
        use_client(client)
        with pytest.raises(SurrealDbError) as raised:
            await WriteNode(id="ada").relate("likes", RecordID("post", "p1"))
        assert type(raised.value) is expected
        assert str(raised.value) == _w(message)
        assert raised.value.__cause__ is error
        assert client.calls == ["RELATE $src->likes->$dst;"]

    async def test_reverse_and_fields_go_the_same_way(self, use_client: Callable[[Any], None]) -> None:
        error = _sdk_error("NotFound", "The record 'post:ghost' does not exist")
        client = _RefusingClient(error)
        use_client(client)
        with pytest.raises(SurrealDbNotFoundError) as raised:
            await WriteNode(id="ada").relate("likes", "post:ghost", reverse=True, data={"stars": 2})
        assert raised.value.__cause__ is error
        assert client.calls == ["RELATE $dst->likes->$src SET stars = $_rd0;"]

    async def test_a_conflict_is_still_recognised(self, use_client: Callable[[Any], None]) -> None:
        error = InternalError("Internal", "Transaction conflict: Resource busy. This transaction can be retried")
        use_client(_RefusingClient(error))
        with pytest.raises(SurrealDbError) as raised:
            await WriteNode(id="ada").relate("likes", RecordID("post", "p1"))
        assert is_conflict_error(raised.value)

    async def test_another_error_is_not_translated(self, use_client: Callable[[Any], None]) -> None:
        """Only the server's refusals (``ServerError``) are translated: a lost connection propagates as it is."""
        error = ConnectionError("connection lost")
        use_client(_RefusingClient(error))
        with pytest.raises(ConnectionError) as raised:
            await WriteNode(id="ada").relate("likes", RecordID("post", "p1"))
        assert raised.value is error

    @pytest.mark.parametrize(
        "error",
        [
            SurrealDbError("Transaction failed and rolled back: Found company:c for field `in`"),
            InternalError("Internal", "The record 'post:ghost' does not exist"),
        ],
        ids=["transaction-error", "sdk-error"],
    )
    async def test_in_a_transaction_nothing_changes(self, use_client: Callable[[Any], None], error: Exception) -> None:
        client = _RefusingClient(AssertionError("the client is not used in a transaction"))
        use_client(client)
        tx = _RefusingTx(error)
        with pytest.raises(type(error)) as raised:
            await WriteNode(id="ada").relate("likes", RecordID("post", "p1"), tx=tx)  # type: ignore[arg-type]
        assert raised.value is error
        assert tx.added == ["RELATE $src->likes->$dst;"]
        assert client.calls == []


class RelPlain(BaseSurrealModel):
    """A node model declared on the relation table ``rel_likes`` (AC-7, M20, D15): its ``save()`` is refused by the
    server, and keeps the SDK's error."""

    model_config = SurrealConfigDict(table_name="rel_likes", connection="rel")

    id: str | None = None
    stars: int = 1


#: The node models of the E2E classes by table: the source of a ``relate()`` from a ``RecordID``.
_REL_NODES: dict[str, type[BaseSurrealModel]] = {
    "rel_person": RelPerson,
    "rel_post": RelPost,
    "rel_book": RelBook,
    "rel_company": RelCompany,
}

_ADA = RecordID("rel_person", "ada")
_P1 = RecordID("rel_post", "p1")

#: Every ORM path that creates an edge (spec §5.7): the typed ``relate()``, an edge's ``save()`` without and with an
#: id (``insert_relation()``, then the guarded statement), ``bulk_create()`` of a batch whose second edge is the one
#: refused, and the string ``relate()`` (D7).
_CREATION_PATHS = ["relate", "save", "save-with-id", "bulk_create", "relate-by-name"]


async def _create_edge(path: str, start: RecordID, end: RecordID) -> None:
    """Create the ``rel_likes`` edge ``start → end`` through *path* (outside a transaction)."""
    if path == "relate":
        await _REL_NODES[start.table_name](id=start.id).relate(RelLikes, end)
    elif path == "save":
        await RelLikes(in_=start, out=end).save()
    elif path == "save-with-id":
        await RelLikes(id="x1", in_=start, out=end).save()
    elif path == "bulk_create":
        await RelLikes.objects().bulk_create([RelLikes(in_=_ADA, out=_P1), RelLikes(in_=start, out=end)])
    else:
        await _REL_NODES[start.table_name](id=start.id).relate("rel_likes", end)


async def _enforced(client: Any) -> None:
    """``rel_likes`` defined by ``RelLikes.define_relation()`` on a missing table (``IN rel_person OUT rel_post |
    rel_book ENFORCED``), then the nodes ``ada``, ``bob``, ``p1``, ``b1`` and ``c`` saved."""
    assert await RelLikes.define_relation() == _REL_G1
    await RelPerson(id="ada").save()
    await RelPerson(id="bob").save()
    await RelPost(id="p1").save()
    await RelBook(id="b1").save()
    await RelCompany(id="c").save()


def _assert_refusal(raised: pytest.ExceptionInfo[SurrealDbError], expected: type[SurrealDbError]) -> None:
    """*raised* is exactly *expected* with W1/W2's text, the SDK's error as its ``__cause__``."""
    cause = raised.value.__cause__
    assert type(raised.value) is expected, f"{type(raised.value).__name__}: {raised.value}"
    assert isinstance(cause, ServerError), repr(cause)
    assert str(raised.value) == _w(str(cause))


class TestEnforcementE2E:
    """AC-7, AC-8 (E2E, every engine of both lines): after ``define_relation()``, SurrealDB refuses every edge creation
    the constraint forbids, and every ORM creation path raises the ORM's error with the SDK's as its ``__cause__`` — W1
    (``SurrealDbError``) for an endpoint outside ``IN``/``OUT``, W2 (``SurrealDbNotFoundError``) for a missing
    ``ENFORCED`` endpoint —, nothing written. The class of an endpoint both outside ``IN``/``OUT`` and missing depends
    on the line (D6). The string ``relate()`` translates too (D7): a ``UNIQUE`` duplicate and a ``TYPE NORMAL`` table
    as well; inside a transaction the transaction's error is unchanged. A node model's ``save()`` into the relation
    table keeps the SDK's error (D15, M20)."""

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize("path", _CREATION_PATHS)
    async def test_a_wrong_table_endpoint(self, kind: str, path: str) -> None:
        async with _rel(kind) as client:
            await _enforced(client)
            with pytest.raises(SurrealDbError) as raised:
                await _create_edge(path, RecordID("rel_company", "c"), _P1)
            _assert_refusal(raised, SurrealDbError)
            assert await _edge_ids(client) == []

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize("path", _CREATION_PATHS)
    async def test_a_missing_endpoint(self, kind: str, path: str) -> None:
        async with _rel(kind) as client:
            await _enforced(client)
            with pytest.raises(SurrealDbNotFoundError) as raised:
                await _create_edge(path, RecordID("rel_person", "ghost"), _P1)
            _assert_refusal(raised, SurrealDbNotFoundError)
            assert "does not exist" in str(raised.value)
            assert await _edge_ids(client) == []

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize("path", _CREATION_PATHS)
    async def test_an_endpoint_outside_the_ends_and_missing_is_pinned_by_line(self, kind: str, path: str) -> None:
        """D6: SurrealDB 2.x and the embedded engine report the missing record (``SurrealDbNotFoundError``), 3.x the
        table mismatch (``SurrealDbError``); the ORM does not even them out."""
        async with _rel(kind) as client:
            await _enforced(client)
            expected = SurrealDbError if await _major(client) >= 3 else SurrealDbNotFoundError
            with pytest.raises(SurrealDbError) as raised:
                await _create_edge(path, RecordID("rel_company", "ghost"), _P1)
            _assert_refusal(raised, expected)
            assert await _edge_ids(client) == []

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize("path", _CREATION_PATHS)
    async def test_the_declared_ends_are_accepted(self, kind: str, path: str) -> None:
        async with _rel(kind) as client:
            await _enforced(client)
            await _create_edge(path, _ADA, RecordID("rel_book", "b1"))
            ends = await client.query("SELECT VALUE [in, out] FROM rel_likes;", {})
            expected = [[_ADA, RecordID("rel_book", "b1")]]
            if path == "bulk_create":
                expected.append([_ADA, _P1])
            assert sorted(ends, key=str) == sorted(expected, key=str)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_endpoint_saved_earlier_in_the_transaction(self, kind: str) -> None:
        """M11: ``ENFORCED`` sees a node created earlier in the same transaction (buffered or interactive)."""
        async with _rel(kind) as client:
            await _enforced(client)
            async with SurrealDBConnectionManager.transaction("rel") as tx:
                eve = await RelPerson(id="eve").save(tx=tx)
                await eve.relate(RelLikes, _P1, tx=tx)
                await eve.relate("rel_likes", RecordID("rel_book", "b1"), tx=tx)
            ends = await client.query("SELECT VALUE [in, out] FROM rel_likes;", {})
            eve_id = RecordID("rel_person", "eve")
            assert sorted(ends, key=str) == sorted([[eve_id, _P1], [eve_id, RecordID("rel_book", "b1")]], key=str)

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize("with_id", [True, False], ids=["with-id", "without-id"])
    async def test_a_node_models_save_keeps_the_sdks_error(self, kind: str, with_id: bool) -> None:
        """D15, M20: a refused write that is not an edge creation keeps the SDK's error (``InternalError``)."""
        async with _rel(kind) as client:
            await _enforced(client)
            plain = RelPlain(id="plain") if with_id else RelPlain()
            with pytest.raises(ServerError) as raised:
                await plain.save()
            assert "RELATION" in str(raised.value)
            assert not isinstance(raised.value, SurrealORMError)
            assert await _edge_ids(client) == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_by_name_a_unique_duplicate(self, kind: str) -> None:
        """README section 23's idempotent edges: a ``UNIQUE`` index on ``(in, out)`` refuses the second pair."""
        async with _rel(kind) as client:
            await _enforced(client)
            await _apply_ddl(client, "DEFINE INDEX rel_follows_pair ON rel_follows FIELDS in, out UNIQUE;")
            ada, bob = RelPerson(id="ada"), RelPerson(id="bob")
            await ada.relate("rel_follows", bob)
            with pytest.raises(SurrealDbError) as raised:
                await ada.relate("rel_follows", bob)
            _assert_refusal(raised, SurrealDbError)
            assert "rel_follows_pair" in str(raised.value)
            assert len(await client.query("SELECT VALUE id FROM rel_follows;", {})) == 1

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_by_name_a_type_normal_table(self, kind: str) -> None:
        async with _rel(kind) as client:
            await _enforced(client)
            await _apply_ddl(client, "DEFINE TABLE rel_normal TYPE NORMAL;")
            with pytest.raises(SurrealDbError) as raised:
                await RelPerson(id="ada").relate("rel_normal", _P1)
            _assert_refusal(raised, SurrealDbError)
            assert await client.query("SELECT VALUE id FROM rel_normal;", {}) == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_by_name_in_a_transaction(self, kind: str) -> None:
        """Inside a transaction the transaction layer's error is unchanged (interactive: at the ``relate()``;
        buffered: at the block's exit)."""
        async with _rel(kind) as client:
            await _enforced(client)
            with pytest.raises(SurrealDbError, match="Transaction failed and rolled back") as raised:
                async with SurrealDBConnectionManager.transaction("rel") as tx:
                    await RelPerson(id="bob").relate("rel_likes", _P1, tx=tx)
                    await RelCompany(id="c").relate("rel_likes", _P1, tx=tx)
            assert not str(raised.value).startswith(_w(""))
            assert await _edge_ids(client) == []


# === removing a constraint (spec §5.4 "Removing a constraint", G6, D14, P1; AC-20; M18, M19) ======================


class RelLikesAny(BaseSurrealEdge):
    """Another edge model on ``rel_likes``, without relation keys (AC-20): its ``define_relation()`` removes the
    constraint."""

    model_config = SurrealConfigDict(table_name="rel_likes", connection="rel")

    stars: int = 1


class RelLikesReset(RelLikes):
    """``RelLikes`` with its three keys reset to ``None`` in a subclass (AC-20)."""

    model_config = SurrealConfigDict(relation_in=None, relation_out=None, enforced=None)


class RelLikesEmpty(RelLikes):
    """``RelLikes`` with its ends emptied and ``enforced=False`` in a subclass (AC-20, P1 a): empty means any table."""

    model_config = SurrealConfigDict(relation_in=[], relation_out=(), enforced=False)


#: The three forms of an edge model without keys on ``rel_likes`` (spec §5.4).
_KEYLESS_MODELS: list[type[BaseSurrealEdge]] = [RelLikesAny, RelLikesReset, RelLikesEmpty]

#: Their bare statements (G2's form, G7's text): ``TYPE RELATION`` with no ends.
_REL_KEYLESS = "DEFINE TABLE OVERWRITE `rel_likes` TYPE RELATION;"
_REL_KEYLESS_IF_NOT_EXISTS = "DEFINE TABLE IF NOT EXISTS `rel_likes` TYPE RELATION;"

#: The ``TableState`` attributes of the clauses ``define_relation()`` carries (spec §5.5's clause order).
_CARRIED = ("drop", "schema_mode", "comment", "changefeed", "changefeed_include_original", "permissions")

_GHOST_BOOK = RecordID("rel_book", "ghost")


def _carried(state: TableState) -> dict[str, object]:
    """The carried clauses of *state*, by attribute."""
    return {name: getattr(state, name) for name in _CARRIED}


def _field_type(info: dict[str, Any], name: str) -> str:
    """The ``TYPE`` of the field *name* in ``INFO FOR TABLE``'s *info*, as ``parse_define_field()`` reads it."""
    return parse_define_field(info["fields"][name]).state.field_type


async def _ends(client: Any) -> list[list[RecordID]]:
    """``[in, out]`` of every ``rel_likes`` edge, sorted."""
    return sorted(await client.query("SELECT VALUE [in, out] FROM rel_likes;", {}), key=str)


async def _k6(client: Any) -> dict[str, BaseSurrealModel]:
    """AC-6's fixture, then ``RelLikes.define_relation()`` (K6): the table holds the constraint, its other clauses and
    ``e1``, ``e2``. Returns the nodes by name."""
    nodes = await _ac6(client)
    assert await RelLikes.define_relation() == _K6
    assert (await _tables(client))["rel_likes"] == _K6_READ_BACK
    return nodes


async def _assert_removed(client: Any) -> None:
    """``rel_likes`` reads back K7's form: a relation table without ``IN``, ``OUT`` or ``ENFORCED``, K6's other clauses,
    ``in``/``out`` rewritten ``TYPE record``, its field ``stars``, index, event and view kept."""
    state = parse_define_table((await _tables(client))["rel_likes"])
    assert (state.table_type, state.relation_in, state.relation_out, state.enforced) == ("relation", (), (), False)
    assert _carried(state) == _carried(parse_define_table(_K6_READ_BACK))
    info = await _table_info(client, "rel_likes")
    assert (_field_type(info, "in"), _field_type(info, "out")) == ("record", "record")
    assert {"in", "out", "stars"} <= set(info["fields"])
    assert list(info["indexes"]) == ["rel_likes_pair"]
    assert list(info["events"]) == ["rel_likes_ev"]
    assert list(info["tables"]) == ["rel_like_counts"]


class _Rollback(Exception):
    """Raised inside a ``transaction()`` block to roll it back."""


class TestRemoveConstraint:
    """AC-20 (unit, P1 a): an edge model without keys — another model on the table, or a subclass resetting them to
    ``None`` or emptying them — renders ``TYPE RELATION`` with no ends, says any table on both sides and not enforced,
    and carries K6's read-back into K7."""

    @pytest.mark.parametrize("model", _KEYLESS_MODELS, ids=lambda model: model.__name__)
    def test_the_keyless_statement(self, model: type[BaseSurrealEdge]) -> None:
        assert model.get_table_name() == "rel_likes"
        assert model.relation_ddl() == _REL_KEYLESS
        assert model.relation_ddl(overwrite=False) == _REL_KEYLESS_IF_NOT_EXISTS
        assert model.get_relation_in() == ()
        assert model.get_relation_out() == ()
        assert model.is_enforced() is False
        assert model.get_table_type() is TableType.RELATION

    @pytest.mark.parametrize("model", _KEYLESS_MODELS, ids=lambda model: model.__name__)
    def test_k7_from_k6s_read_back(self, model: type[BaseSurrealEdge]) -> None:
        clause = schema.relation_type_clause(model.get_relation_in(), model.get_relation_out(), model.is_enforced())
        assert clause == "TYPE RELATION"
        assert schema.relation_statement_keeping("rel_likes", clause, parse_define_table(_K6_READ_BACK)) == _K7

    def test_the_parent_keeps_its_keys(self) -> None:
        assert RelLikes.relation_ddl() == _REL_G1
        assert (RelLikes.get_relation_in(), RelLikes.get_relation_out()) == (("rel_person",), ("rel_post", "rel_book"))
        assert RelLikes.is_enforced() is True


class TestRemoveConstraintE2E:
    """AC-20 (E2E, every engine of both lines; G6, D14, M19): ``define_relation()`` of an edge model without keys on
    the constrained table (K6) sends K7 — ``TYPE RELATION`` with no ends, the table's other clauses carried. The table
    reads back without ``IN``/``OUT``/``ENFORCED``, ``in``/``out`` as ``TYPE record``, its fields, index, event, view and
    edges kept; edges from or to any table, and to a missing record, are accepted at once, on every connection; and
    ``RelLikes.define_relation()`` puts K6 back. ``overwrite=False`` removes nothing (D8). In a transaction the removal
    is applied at commit, and not at all when the block raises."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_removal_out_of_a_transaction(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _rel(kind) as client:
            nodes = await _k6(client)
            if kind != "mem":
                _register_rel(kind, "rel2")
            try:
                if kind != "mem":
                    # The second connection's session is open before the removal (M9).
                    second = await SurrealDBConnectionManager.get_client("rel2")
                    assert await second.query("SELECT VALUE id FROM rel_likes ORDER BY id;", {}) == [
                        RecordID("rel_likes", "e1"),
                        RecordID("rel_likes", "e2"),
                    ]
                calls = _spy(monkeypatch, client)
                assert await RelLikesAny.define_relation() == _K7
                assert _sent(calls) == [_INFO, _K7]
                await _assert_removed(client)
                assert await _edge_ids(client) == ["rel_likes:e1", "rel_likes:e2"]

                # At once: an endpoint outside the old ends, and a missing one (no ENFORCED any more).
                company = await nodes["c"].relate(RelLikesAny, nodes["p1"])
                ghost = await nodes["ada"].relate(RelLikesAny, _GHOST_BOOK)
                assert (company.in_, company.out) == (RecordID("rel_company", "c"), RecordID("rel_post", "p1"))
                assert (ghost.in_, ghost.out) == (RecordID("rel_person", "ada"), _GHOST_BOOK)
                expected = [
                    [RecordID("rel_company", "c"), RecordID("rel_post", "p1")],
                    [RecordID("rel_person", "ada"), _GHOST_BOOK],
                    [RecordID("rel_person", "ada"), RecordID("rel_post", "p1")],
                    [RecordID("rel_person", "ada"), RecordID("rel_book", "b1")],
                ]
                if kind != "mem":
                    async with SurrealDBConnectionManager.using("rel2"):
                        other = await nodes["c"].relate(RelLikesAny, nodes["b1"])
                    assert (other.in_, other.out) == (RecordID("rel_company", "c"), RecordID("rel_book", "b1"))
                    expected.append([RecordID("rel_company", "c"), RecordID("rel_book", "b1")])
                assert await _ends(client) == sorted(expected, key=str)

                # The round trip: the constraint back, the edges written meanwhile kept (not checked, M5).
                calls.clear()
                assert await RelLikes.define_relation() == _K6
                assert _sent(calls) == [_INFO, _K6]
                assert (await _tables(client))["rel_likes"] == _K6_READ_BACK
                info = await _table_info(client, "rel_likes")
                assert (_field_type(info, "in"), _field_type(info, "out")) == (
                    "record<rel_person>",
                    "record<rel_post | rel_book>",
                )
                with pytest.raises(SurrealDbError) as refused:
                    await nodes["bob"].relate(RelLikes, nodes["c"])
                assert type(refused.value) is SurrealDbError
                assert await _ends(client) == sorted(expected, key=str)
            finally:
                if kind != "mem":
                    await SurrealDBConnectionManager.remove_connection("rel2")

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_each_keyless_form_returns_k7(self, kind: str) -> None:
        """The other model, the subclass resetting the keys to ``None`` and the one emptying them: each removes the
        constraint with K7 (the table re-defined with K6 before each)."""
        async with _rel(kind) as client:
            await _k6(client)
            for model in _KEYLESS_MODELS:
                assert await model.define_relation() == _K7, model.__name__
                await _assert_removed(client)
                assert await RelLikes.define_relation() == _K6, model.__name__
                assert (await _tables(client))["rel_likes"] == _K6_READ_BACK, model.__name__

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_overwrite_false_removes_nothing(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """D8: ``IF NOT EXISTS`` on the constrained table is sent, and its ends stay."""
        async with _rel(kind) as client:
            nodes = await _k6(client)
            calls = _spy(monkeypatch, client)
            assert await RelLikesAny.define_relation(overwrite=False) == _REL_KEYLESS_IF_NOT_EXISTS
            assert _sent(calls) == [_INFO, _REL_KEYLESS_IF_NOT_EXISTS]
            assert (await _tables(client))["rel_likes"] == _K6_READ_BACK
            with pytest.raises(SurrealDbError) as refused:
                await nodes["c"].relate(RelLikesAny, nodes["p1"])
            assert type(refused.value) is SurrealDbError
            assert await _edge_ids(client) == ["rel_likes:e1", "rel_likes:e2"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_in_a_transaction_the_removal_is_applied_at_commit(self, kind: str) -> None:
        """Buffered (HTTP, SurrealDB 2.x, the embedded engine) or interactive (3.x over WebSocket): outside the block the
        constraint holds until the commit; after it, the table reads back without ends."""
        async with _rel(kind) as client:
            nodes = await _k6(client)
            interactive = kind == "ws" and await _major(client) >= 3
            async with SurrealDBConnectionManager.transaction("rel") as tx:
                assert tx.is_interactive is interactive
                assert await RelLikesAny.define_relation(tx=tx) == _K7
                assert (await _tables(client))["rel_likes"] == _K6_READ_BACK
            await _assert_removed(client)
            await nodes["c"].relate(RelLikesAny, nodes["p1"])
            assert [RecordID("rel_company", "c"), RecordID("rel_post", "p1")] in await _ends(client)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_block_that_raises_removes_nothing(self, kind: str) -> None:
        async with _rel(kind) as client:
            nodes = await _k6(client)
            interactive = kind == "ws" and await _major(client) >= 3
            with pytest.raises(_Rollback):
                async with SurrealDBConnectionManager.transaction("rel") as tx:
                    assert tx.is_interactive is interactive
                    assert await RelLikesAny.define_relation(tx=tx) == _K7
                    raise _Rollback
            assert (await _tables(client))["rel_likes"] == _K6_READ_BACK
            with pytest.raises(SurrealDbError) as refused:
                await nodes["c"].relate(RelLikesAny, nodes["p1"])
            assert type(refused.value) is SurrealDbError
            assert await _edge_ids(client) == ["rel_likes:e1", "rel_likes:e2"]
