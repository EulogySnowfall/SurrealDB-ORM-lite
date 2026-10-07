"""Tests for v0.20.0 — the SurrealQL literal encoder used by live-query filters.

SurrealDB 2.x drops bound parameters inside a live query, so the ORM inlines filter values as
literals. The unit tests pin the exact text; the E2E class proves on the running server that
every literal means exactly the value it came from (``RETURN <literal> == $p``), which is what
keeps the inlining from becoming an injection or a silent mismatch.
"""

import math
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from enum import IntEnum, StrEnum
from typing import Any
from uuid import UUID

import pytest

from surreal_orm_lite._sdk import RecordID
from surreal_orm_lite.surql_literal import inline_variables, to_surql_literal
from tests.conftest import orm_client

ADVERSARIAL = [
    "plain",
    "it's",
    'dq"x',
    "back\\slash",
    "trailing\\",
    "\\'; DELETE person; --",
    "' OR true OR '",
    "nl\nx\ttab\r",
    "é漢字🙂",
    " ls",
    "${x}",
    "$x",
    "$_f0",
    "`tick`",
    "⟨angle⟩",
    "\x7f",
    "",
]


class Colour(StrEnum):
    RED = "red"


class Level(IntEnum):
    HIGH = 3


class TestScalars:
    def test_none(self) -> None:
        assert to_surql_literal(None) == "NONE"

    def test_bools(self) -> None:
        assert to_surql_literal(True) == "true"
        assert to_surql_literal(False) == "false"

    def test_ints(self) -> None:
        assert to_surql_literal(42) == "42"
        assert to_surql_literal(-7) == "-7"
        assert to_surql_literal(Level.HIGH) == "3"

    def test_int_outside_64_bits_is_refused(self) -> None:
        with pytest.raises(TypeError, match="64-bit"):
            to_surql_literal(2**63)

    def test_floats(self) -> None:
        assert to_surql_literal(1.5) == "1.5"
        assert to_surql_literal(1e20) == "1e+20"
        assert to_surql_literal(math.nan) == "NaN"
        assert to_surql_literal(math.inf) == "math::inf"
        assert to_surql_literal(-math.inf) == "-math::inf"

    def test_strings_escape_backslash_then_quote(self) -> None:
        assert to_surql_literal("it's") == "'it\\'s'"
        assert to_surql_literal("a\\b") == "'a\\\\b'"
        assert to_surql_literal(Colour.RED) == "'red'"

    def test_nul_is_refused(self) -> None:
        with pytest.raises(TypeError, match="NUL"):
            to_surql_literal("a\x00b")

    def test_decimal_uses_fixed_notation(self) -> None:
        assert to_surql_literal(Decimal("1.10")) == "1.10dec"
        assert to_surql_literal(Decimal("1E+3")) == "1000dec"

    def test_non_finite_decimal_is_refused(self) -> None:
        with pytest.raises(TypeError, match="finite"):
            to_surql_literal(Decimal("NaN"))

    def test_datetimes(self) -> None:
        aware = datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=UTC)
        assert to_surql_literal(aware) == "d'2026-01-01T00:00:00.123456+00:00'"
        assert to_surql_literal(datetime(2026, 1, 1)) == "d'2026-01-01T00:00:00Z'"

    def test_uuid(self) -> None:
        value = UUID("0190c4b6-0000-7000-8000-000000000000")
        assert to_surql_literal(value) == "u'0190c4b6-0000-7000-8000-000000000000'"


class TestRecordIds:
    def test_int_id_is_bare(self) -> None:
        assert to_surql_literal(RecordID("user", 1)) == "`user`:1"

    def test_str_id_is_backticked_and_escaped(self) -> None:
        assert to_surql_literal(RecordID("user", "a`b\\c")) == "`user`:`a\\`b\\\\c`"

    def test_table_is_escaped_too(self) -> None:
        assert to_surql_literal(RecordID("ta`b", 1)) == "`ta\\`b`:1"

    def test_array_and_object_ids_recurse(self) -> None:
        assert to_surql_literal(RecordID("user", [1, "a"])) == "`user`:[1, 'a']"
        assert to_surql_literal(RecordID("user", {"a": 1})) == "`user`:{'a': 1}"


class TestContainers:
    def test_sequences(self) -> None:
        assert to_surql_literal([1, "a", None]) == "[1, 'a', NONE]"
        assert to_surql_literal((True,)) == "[true]"
        assert to_surql_literal([]) == "[]"

    def test_dict(self) -> None:
        assert to_surql_literal({"it's": 1, "b": [2]}) == "{'it\\'s': 1, 'b': [2]}"
        assert to_surql_literal({}) == "{}"

    def test_non_str_key_is_refused(self) -> None:
        with pytest.raises(TypeError, match="key"):
            to_surql_literal({1: "a"})

    def test_unknown_type_is_refused_by_name(self) -> None:
        with pytest.raises(TypeError, match="timedelta"):
            to_surql_literal(timedelta(hours=1))


class TestInlineVariables:
    def test_substitutes_bound_references(self) -> None:
        sql = "WHERE a = $_f0 AND b IN $_f1"
        assert inline_variables(sql, {"_f0": "x", "_f1": [1, 2]}) == "WHERE a = 'x' AND b IN [1, 2]"

    def test_longest_name_wins(self) -> None:
        assert inline_variables("$_f1 $_f10", {"_f1": 1, "_f10": 10}) == "1 10"

    def test_unbound_references_are_left_alone(self) -> None:
        assert inline_variables("a = $auth AND b = $_f0", {"_f0": 1}) == "a = $auth AND b = 1"

    def test_inserted_text_is_never_rescanned(self) -> None:
        """Review focus #2: a value that spells another reference stays a literal."""
        assert inline_variables("$a = $b", {"a": "$b", "b": 1}) == "'$b' = 1"

    def test_backslashes_are_inserted_verbatim(self) -> None:
        assert inline_variables("x = $a", {"a": "\\t"}) == "x = '\\\\t'"

    def test_unused_unsupported_value_does_not_raise(self) -> None:
        assert inline_variables("x = $a", {"a": 1, "unused": timedelta(1)}) == "x = 1"

    def test_error_names_the_variable(self) -> None:
        with pytest.raises(TypeError, match=r"\$a"):
            inline_variables("x = $a", {"a": timedelta(1)})


# ==================== E2E — the literal means the value, on every line ====================

ROUND_TRIP: list[Any] = [
    *ADVERSARIAL,
    0,
    42,
    -7,
    2**63 - 1,
    1.5,
    -0.25,
    1e20,
    math.inf,
    -math.inf,
    True,
    False,
    None,
    Decimal("1.10"),
    Decimal("-1000"),
    Decimal("0.000001"),
    datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=UTC),
    datetime(2026, 1, 1, 2, 0, tzinfo=timezone(timedelta(hours=2))),
    datetime(2026, 1, 1),
    UUID("0190c4b6-0000-7000-8000-000000000000"),
    RecordID("user", 1),
    RecordID("user", "abc"),
    RecordID("user", "123"),
    RecordID("user", "x⟩y`z\\w'q"),
    RecordID("we-ird", "a b"),
    RecordID("user", [1, "a"]),
    RecordID("user", {"a": 1}),
    [1, "a", None, [True]],
    {"a b": 1, "it's": "x", "nested": {"k": [1]}},
]


class TestRoundTripE2E:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ROUND_TRIP, ids=repr)
    async def test_literal_equals_the_bound_parameter(self, value: Any) -> None:
        async with orm_client() as client:
            result = await client.query(f"RETURN {to_surql_literal(value)} == $p;", {"p": value})
        assert result is True
