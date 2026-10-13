"""Tests for v0.20.0 — the SurrealQL literal encoder used by live-query filters.

SurrealDB 2.x drops bound parameters inside a live query, so the ORM inlines filter values as
literals. The unit tests pin the exact text; the E2E class proves on the running server that
every literal means exactly the value it came from (``RETURN <literal> == $p``), which is what
keeps the inlining from becoming an injection or a silent mismatch.
"""

import math
import re
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import IntEnum, StrEnum
from typing import Any
from uuid import UUID

import pytest
import surrealdb

from surreal_orm_lite import BoundExcluded, BoundIncluded, Duration, Range
from surreal_orm_lite._sdk import Bound, RecordID
from surreal_orm_lite.surql_literal import inline_variables, to_surql_literal
from surreal_orm_lite.wire import encode_wire
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
        with pytest.raises(TypeError, match="time"):
            to_surql_literal(time(1))


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
            inline_variables("x = $a", {"a": time(1)})


class _Day(date):
    """A ``date`` subclass: rendered as the date it is, never as something else."""


MAX_DURATION_TEXT = "584942417355y3w5d7h15s999ms999us999ns"


class TestRichLiteralText:
    """v0.23.0: durations, dates and ranges (spec §5.5); the round trip is ``TestRichLiteralsE2E``."""

    @pytest.mark.parametrize(
        ("value", "text"),
        [
            (timedelta(hours=1, minutes=30), "1h30m"),
            (timedelta(days=1, microseconds=5), "1d5us"),
            (timedelta(days=15), "2w1d"),
            (timedelta(0), "0ns"),
            (Duration(1), "1ns"),
            (Duration(0), "0ns"),
            (Duration.parse("1y2w3d4h5m6s7ms8us9ns"), "1y2w3d4h5m6s7ms8us9ns"),
            (Duration(Duration.MAX_NANOSECONDS), MAX_DURATION_TEXT),
            (surrealdb.Duration(5400 * 10**9), "1h30m"),
            (surrealdb.Duration(5400 * 10**9 + 1), "1h30m1ns"),
            # Through ``duration_ns``: exact integers, never the SDK's float arithmetic.
            (surrealdb.Duration(Duration.MAX_NANOSECONDS), MAX_DURATION_TEXT),
            (surrealdb.Duration(0), "0ns"),
        ],
    )
    def test_durations(self, value: Any, text: str) -> None:
        assert to_surql_literal(value) == text

    @pytest.mark.parametrize(
        "value",
        [
            timedelta(seconds=-1),
            timedelta(microseconds=-1),
            surrealdb.Duration(-1),
            surrealdb.Duration(Duration.MAX_NANOSECONDS + 1),
            surrealdb.Duration(1.5),  # type: ignore[arg-type]
        ],
    )
    def test_durations_outside_the_server_range_are_refused(self, value: Any) -> None:
        with pytest.raises(TypeError):
            to_surql_literal(value)

    def test_dates(self) -> None:
        assert to_surql_literal(date(2026, 1, 2)) == "d'2026-01-02T00:00:00Z'"
        assert to_surql_literal(_Day(2026, 1, 2)) == "d'2026-01-02T00:00:00Z'"
        assert to_surql_literal(date(1, 1, 1)) == "d'0001-01-01T00:00:00Z'"
        # A datetime still renders as before (v0.20.0), never as its date.
        assert to_surql_literal(datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=UTC)) == "d'2026-01-02T03:04:05.000006Z'"
        assert to_surql_literal(datetime(2026, 1, 1)) == "d'2026-01-01T00:00:00Z'"

    @pytest.mark.parametrize(
        ("value", "text"),
        [
            (Range(BoundIncluded(1), BoundExcluded(5)), "1..5"),
            (Range(BoundIncluded(1), BoundIncluded(5)), "1..=5"),
            (Range(BoundExcluded(1), BoundExcluded(5)), "1>..5"),
            (Range(BoundExcluded(1), BoundIncluded(5)), "1>..=5"),
            (Range(None, BoundExcluded(5)), "..5"),
            (Range(None, BoundIncluded(5)), "..=5"),
            (Range(BoundIncluded(1), None), "1.."),
            (Range(BoundExcluded(1), None), "1>.."),
            (Range(), ".."),
            (Range(BoundIncluded(-5), BoundExcluded(-1)), "-5..-1"),
            (Range(BoundIncluded(1.5), BoundExcluded(2.5)), "1.5..2.5"),
            (Range(BoundIncluded(1e20), BoundExcluded(1e21)), "1e+20..1e+21"),
            (Range(BoundIncluded(Decimal("1.5")), BoundExcluded(Decimal("2"))), "1.5dec..2dec"),
            (
                Range(BoundIncluded(datetime(2026, 1, 1, tzinfo=UTC)), BoundExcluded(datetime(2026, 2, 1, tzinfo=UTC))),
                "d'2026-01-01T00:00:00Z'..d'2026-02-01T00:00:00Z'",
            ),
            (Range(BoundIncluded(date(2026, 1, 2)), None), "d'2026-01-02T00:00:00Z'.."),
            (Range(BoundIncluded("a"), BoundExcluded("m")), "'a'..'m'"),
            (Range(BoundIncluded("it's"), BoundExcluded("a..b")), "'it\\'s'..'a..b'"),
            (Range(BoundIncluded(timedelta(minutes=1)), BoundExcluded(Duration.parse("1h"))), "1m..1h"),
            (Range(BoundIncluded(surrealdb.Duration(60 * 10**9)), None), "1m.."),
            # The SDK's own Range, open sides as ``None`` or a bare ``Bound()``.
            (surrealdb.Range(BoundIncluded(1), BoundExcluded(5)), "1..5"),
            (surrealdb.Range(BoundExcluded(1), BoundIncluded(5)), "1>..=5"),
            (surrealdb.Range(None, BoundIncluded(5)), "..=5"),  # type: ignore[arg-type]
            (surrealdb.Range(BoundIncluded(1), Bound()), "1.."),
            (surrealdb.Range(Bound(), Bound()), ".."),
        ],
    )
    def test_ranges(self, value: Any, text: str) -> None:
        assert to_surql_literal(value) == text

    @pytest.mark.parametrize(
        "bound",
        [
            True,
            UUID("0190c4b6-0000-7000-8000-000000000000"),
            RecordID("user", 1),
            [1],
            {"a": 1},
            Range(),
            None,
            math.nan,
            math.inf,
            -math.inf,
            time(1),
        ],
        ids=repr,
    )
    def test_range_bounds_outside_the_closed_set_are_refused(self, bound: Any) -> None:
        name = re.escape(type(bound).__name__)
        for value in (
            Range(BoundIncluded(bound), None),
            Range(None, BoundExcluded(bound)),
            Range(BoundIncluded(1), BoundIncluded(bound)),
            surrealdb.Range(BoundExcluded(bound), None),  # type: ignore[arg-type]
        ):
            with pytest.raises(TypeError, match=name):
                to_surql_literal(value)

    def test_sdk_range_with_non_bound_sides_is_refused(self) -> None:
        with pytest.raises(TypeError, match="int"):
            to_surql_literal(surrealdb.Range(1, 5))  # type: ignore[arg-type]

    def test_range_bounds_keep_the_scalar_refusals(self) -> None:
        with pytest.raises(TypeError, match="64-bit"):
            to_surql_literal(Range(BoundIncluded(2**63), None))
        with pytest.raises(TypeError, match="NUL"):
            to_surql_literal(Range(BoundIncluded("a\x00"), None))
        with pytest.raises(TypeError, match="finite"):
            to_surql_literal(Range(BoundIncluded(Decimal("NaN")), None))
        with pytest.raises(TypeError, match="negative"):
            to_surql_literal(Range(BoundIncluded(timedelta(seconds=-1)), None))

    def test_containers_and_variables(self) -> None:
        assert to_surql_literal([timedelta(hours=1), date(2026, 1, 2)]) == "[1h, d'2026-01-02T00:00:00Z']"
        assert to_surql_literal({"ttl": Duration(1)}) == "{'ttl': 1ns}"
        assert inline_variables("n IN $a", {"a": Range(BoundIncluded(1), BoundExcluded(5))}) == "n IN 1..5"
        assert inline_variables("ttl >= $a", {"a": timedelta(hours=1)}) == "ttl >= 1h"

    def test_unsupported_message_names_the_new_types(self) -> None:
        with pytest.raises(TypeError) as info:
            to_surql_literal(time(1))
        for name in ("timedelta", "Duration", "date", "Range"):
            assert re.search(rf"\b{name}\b", str(info.value)), name


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


# ==================== v0.23.0 — durations, dates and ranges mean their value, on every line ====================

_JAN_1 = datetime(2026, 1, 1, tzinfo=UTC)
_FEB_1 = datetime(2026, 2, 1, 0, 0, 0, 123456, tzinfo=UTC)

# Every form of spec §5.5 the encoder can produce. The reference is the value the ORM binds for it
# everywhere else (``encode_wire(v, stored=False)``): the SDK alone cannot send a ``timedelta``
# (``BufferError``) nor a ``date`` (the WebSocket drops).
RICH_ROUND_TRIP: list[Any] = [
    # durations: composed units, largest first
    timedelta(hours=1, minutes=30),
    timedelta(days=15, microseconds=5),
    timedelta(0),  # ``0ns``: a zero duration may filter, only a zero *result* trips the SDK
    Duration(1),
    Duration.parse("1h30m1ns"),
    Duration.parse("1y2w3d4h5m6s7ms8us9ns"),
    Duration(10**18 - 1),
    Duration(Duration.MAX_NANOSECONDS),
    surrealdb.Duration(5400 * 10**9 + 1),
    # dates: midnight UTC
    date(2026, 1, 2),
    _Day(2026, 1, 2),
    # ranges: every inclusivity, open sides
    Range(BoundIncluded(1), BoundExcluded(5)),
    Range(BoundIncluded(1), BoundIncluded(5)),
    Range(BoundExcluded(1), BoundExcluded(5)),
    Range(BoundExcluded(1), BoundIncluded(5)),
    Range(None, BoundExcluded(5)),
    Range(None, BoundIncluded(5)),
    Range(BoundIncluded(1), None),
    Range(BoundExcluded(1), None),
    Range(),
    Range(BoundIncluded(-5), BoundExcluded(-1)),
    # each bound type of the closed set
    Range(BoundIncluded(1.5), BoundExcluded(2.5)),
    Range(BoundIncluded(1e20), BoundExcluded(1e21)),
    Range(BoundIncluded(-0.0), BoundIncluded(1e-05)),
    Range(BoundIncluded(Decimal("1.5")), BoundExcluded(Decimal("2"))),
    Range(BoundIncluded(_JAN_1), BoundExcluded(_FEB_1)),
    Range(BoundExcluded(_JAN_1), None),
    Range(BoundIncluded(date(2026, 1, 2)), BoundIncluded(date(2026, 2, 1))),
    Range(BoundIncluded("a"), BoundExcluded("m")),
    Range(BoundIncluded("it's"), BoundExcluded("a..b")),
    Range(BoundIncluded(timedelta(minutes=1)), BoundExcluded(Duration.parse("1h"))),
    Range(BoundIncluded(Duration.parse("1h30m1ns")), BoundIncluded(Duration(Duration.MAX_NANOSECONDS))),
    # the SDK's own values
    surrealdb.Range(BoundExcluded(1), BoundIncluded(5)),
    surrealdb.Range(BoundIncluded(surrealdb.Duration(60 * 10**9)), Bound()),
]

# WHERE fragments as the ORM builds them: a range literal is followed by ``AND``/``OR``, so its open
# end must not swallow what comes next.
MEMBERSHIP_CLAUSES = [
    "$x IN $r AND $b",
    "$b AND $x IN $r",
    "$x IN $r OR $b",
    "$x NOT IN $r AND $b",
    "$x NOT IN $r OR $b",
    "$x IN $r AND $x IN $s",
    "($x IN $r OR NOT ($x IN $s)) AND $b",
]
MEMBERSHIP_RANGES = [
    Range(BoundIncluded(3), None),
    Range(BoundExcluded(3), None),
    Range(None, BoundIncluded(5)),
    Range(),
    Range(BoundExcluded(3), BoundIncluded(5)),
]


def _rich_id(value: Any) -> str:
    return f"{type(value).__name__}:{to_surql_literal(value)}"


class TestRichLiteralsE2E:
    """v0.23.0 (AC-23): the literal of a duration, a date or a range means exactly the value the ORM
    binds for it elsewhere, on both lines — a live filter watches the records a query would return."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", RICH_ROUND_TRIP, ids=_rich_id)
    async def test_literal_equals_the_encoded_parameter(self, value: Any) -> None:
        async with orm_client() as client:
            result = await client.query(f"RETURN {to_surql_literal(value)} == $p;", {"p": encode_wire(value, stored=False)})
        assert result is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("clause", MEMBERSHIP_CLAUSES)
    async def test_range_membership_keeps_its_precedence(self, clause: str) -> None:
        """The inlined clause evaluates as the bound one does, an open range end included."""
        mismatches = []
        async with orm_client() as client:
            for r in MEMBERSHIP_RANGES:
                for x in (2, 3, 4, 5, 7):
                    for b in (True, False):
                        variables = {"x": x, "r": r, "s": Range(None, BoundExcluded(6)), "b": b}
                        inlined = inline_variables(clause, variables)
                        bound = await client.query(f"RETURN {clause};", encode_wire(variables, stored=False))
                        literal = await client.query(f"RETURN {inlined};", {})
                        if literal is not bound or not isinstance(bound, bool):
                            mismatches.append(f"{inlined} -> {literal!r}, bound -> {bound!r}")
        assert not mismatches, "\n".join(mismatches)


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
