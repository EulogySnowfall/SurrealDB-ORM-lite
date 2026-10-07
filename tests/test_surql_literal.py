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
        assert to_surql_literal(aware) == "d'2026-01-01T00:00:00.123456Z'"
        assert to_surql_literal(datetime(2026, 1, 1)) == "d'2026-01-01T00:00:00Z'"

    def test_aware_datetimes_are_rendered_in_utc(self) -> None:
        """An offset with seconds (historic local mean time) parses on neither line correctly:
        2.x rejects it and 3.x compares it wrongly. UTC is unambiguous on both."""
        odd = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=5, minutes=30, seconds=15)))
        assert to_surql_literal(odd) == "d'2025-12-31T18:29:45Z'"

    def test_decimal_beyond_28_fractional_digits_is_refused(self) -> None:
        """SurrealDB decimals keep 28 fractional digits; a 29th is rounded away silently."""
        assert to_surql_literal(Decimal("1E-28")) == "0.0000000000000000000000000001dec"
        with pytest.raises(TypeError, match="28"):
            to_surql_literal(Decimal("1E-29"))

    def test_decimal_beyond_96_bits_is_refused(self) -> None:
        assert to_surql_literal(Decimal(2**96 - 1)) == f"{2**96 - 1}dec"
        with pytest.raises(TypeError, match="96-bit"):
            to_surql_literal(Decimal(2**96))

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
    -(2**63),
    1e-05,
    datetime(2026, 1, 1),
    UUID("0190c4b6-0000-7000-8000-000000000000"),
    RecordID("user", 1),
    RecordID("user", "abc"),
    RecordID("user", "123"),
    RecordID("user", "x⟩y`z\\w'q"),
    RecordID("we-ird", "a b"),
    RecordID("user", [1, "a"]),
    RecordID("user", {"a": 1}),
    RecordID("user", UUID("0190c4b6-0000-7000-8000-000000000000")),
    Decimal("1." + "0" * 29),
    [1, "a", None, [True]],
    {"a b": 1, "it's": "x", "nested": {"k": [1]}},
]


class TestRoundTripE2E:
    @pytest.mark.asyncio
    async def test_offset_with_seconds_is_the_right_instant(self) -> None:
        """Checked against ``time::unix`` rather than a bound parameter: the SDK itself encodes
        this datetime wrongly (3.x drops the offset's seconds; 2.x loses the connection)."""
        value = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=5, minutes=30, seconds=15)))
        async with orm_client() as client:
            result = await client.query(f"RETURN time::unix({to_surql_literal(value)});", {})
        assert result == int(value.timestamp())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ROUND_TRIP, ids=repr)
    async def test_literal_equals_the_bound_parameter(self, value: Any) -> None:
        async with orm_client() as client:
            result = await client.query(f"RETURN {to_surql_literal(value)} == $p;", {"p": value})
        assert result is True


# ==================== PR #199 review fixes ====================


class TestReviewFixesEncoder:
    def test_record_id_with_a_uuid_key(self) -> None:
        """Review #5: ``CREATE user:uuid()`` keys decode to ``uuid.UUID``."""
        key = UUID("0190c4b6-0000-7000-8000-000000000000")
        assert to_surql_literal(RecordID("user", key)) == "`user`:u'0190c4b6-0000-7000-8000-000000000000'"

    def test_trailing_zeros_do_not_count_against_the_scale(self) -> None:
        """Review #8: 1.000…0 (29 zeros) is exactly 1 — nothing would be rounded."""
        assert to_surql_literal(Decimal("1." + "0" * 29)) == "1dec"
        assert to_surql_literal(Decimal("2.50")) == "2.50dec"
        assert to_surql_literal(Decimal("-0.000")) == "-0.000dec"

    def test_unbound_reference_is_refused(self) -> None:
        """Review #4: an unbound $name evaluates to NONE on every notification, so
        ``age > $min_age`` silently matches every record (and ``=`` none)."""
        from surreal_orm_lite.exceptions import SurrealDbError

        with pytest.raises(SurrealDbError, match=r"\$min_age.*variables\(min_age="):
            inline_variables("age > $min_age", {})

    @pytest.mark.parametrize("name", ["auth", "session", "token", "access", "scope", "this", "parent"])
    def test_server_parameters_are_left_alone(self, name: str) -> None:
        assert inline_variables(f"owner = ${name}", {}) == f"owner = ${name}"

    def test_where_builders_emit_dollar_only_as_references(self) -> None:
        """Review #9: inlining rewrites every ``$name`` in the WHERE, so the builders must never
        write a ``$`` of their own. Pins that invariant for every lookup and for Q objects."""
        import re

        from surreal_orm_lite import Q
        from surreal_orm_lite.constants import LOOKUP_OPERATORS
        from surreal_orm_lite.utils import build_filter_condition

        reference = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")
        bound = re.compile(r"\$_f\d+")
        for lookup in LOOKUP_OPERATORS:
            if lookup == "isnull":
                value: Any = True
            elif lookup in ("in", "not_in", "containsall", "containsany"):
                value = ["$a", "b$c"]
            else:
                value = "x$y $z"
            sql, variables, _ = build_filter_condition("field", lookup, value, 0, "T")
            assert "$" not in reference.sub("", sql), (lookup, sql)
            assert not bound.search(inline_variables(sql, variables)), (lookup, sql)
        with pytest.warns(DeprecationWarning):  # "$x" is the deprecated variable-reference form
            sql, variables, _ = (Q(a="$x") | ~Q(b__in=["$y"])).to_sql(0, "T")
        assert "$" not in reference.sub("", sql)
