"""Schema introspection (v0.30.0): the private parser of the ``DEFINE`` statements ``INFO`` returns (spec §6.3).

The scanner (spec §6.3.1; M11, M34, M35): string literals and their decoding, identifiers and field paths, the
top-level keyword candidates with ``IF`` units, top-level splitting; the kinds (spec §6.3.5; M7, M15, M21): their
grammar, one canonical rendering for SurrealDB 2.x and 3.x, nullability; the clause reader and its validators (spec
§6.3.2), ``DEFINE TABLE``, ``DEFINE FIELD`` and permissions (spec §6.3.3; M6, M8–M11, M20, M31, M34–M36, M43) on the
golden statements of spec §9.2 — the 2.x and 3.x renderings side by side; and the fixed reasons ``DefineParseError``
is raised with (spec §6.3.6). Unit tests only (no server).
"""

import ast
import dataclasses
import pathlib
import time

import pytest

from surreal_orm_lite import define_parser
from surreal_orm_lite.define_parser import (
    _R_AMBIGUOUS,
    _R_BRACKETS,
    _R_HEADER,
    _R_IDENT,
    _R_IF,
    _R_KIND,
    _R_NO_READING,
    _R_PATH,
    _R_PERMISSIONS,
    _R_STATEMENT,
    _R_STRING,
    Clause,
    DefineParseError,
    KLiteral,
    KName,
    KObject,
    KTuple,
    KUnion,
    ParsedField,
    Slot,
    canonical_name,
    normalise_kind,
    parse_define_field,
    parse_define_table,
    parse_kind,
    parse_kind_prefix,
    parse_permissions,
    read_clauses,
    read_identifier,
    read_idiom,
    render_kind,
    split_top_level,
    top_level_keywords,
    unquote,
)
from surreal_orm_lite.schema_state import FieldState, TableState

#: Spec §6.3.6: the only reasons ``DefineParseError`` is raised with.
_SPEC_REASONS = {
    "_R_STATEMENT": "not the expected DEFINE statement",
    "_R_HEADER": "its name or ON clause is not where expected",
    "_R_STRING": "a string literal is not terminated",
    "_R_IDENT": "an identifier is not terminated",
    "_R_BRACKETS": "its brackets are not balanced",
    "_R_IF": "an IF expression has no END",
    "_R_PATH": "its field path is not valid",
    "_R_NO_READING": "no reading of its clauses fits",
    "_R_AMBIGUOUS": "its clauses can be read in more than one way",
    "_R_KIND": "its kind is not understood",
    "_R_PERMISSIONS": "its permissions are not understood",
    "_R_INDEX": "its index options are not understood",
    "_R_SIGNATURE": "its function signature is not understood",
}

_UNIT_WORDS = ["IF", "THEN", "ELSE", "END"]


def _positions(text: str, *words: str) -> list[tuple[int, str]]:
    """``(position, word)`` of each space-delimited occurrence of *words*, in text order — the expected result of
    ``top_level_keywords`` when every occurrence listed is at the top level."""
    found: list[tuple[int, str]] = []
    position = 0
    for word in words:
        position = (" " + text + " ").index(f" {word} ", position)
        found.append((position, word))
        position += len(word)
    return found


def _reason(error: pytest.ExceptionInfo[DefineParseError]) -> str:
    assert len(error.value.args) == 1
    reason = error.value.args[0]
    assert str(error.value) == reason
    return str(reason)


class TestScanner:
    """Spec §6.3.1: one left-to-right pass; strings and quoted identifiers are opaque, depth counts brackets and
    ``IF`` units."""

    # --- unquote ---------------------------------------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("literal", "expected"),
        [
            ("'it\\'s'", "it's"),
            ('"it\'s"', "it's"),  # a COMMENT holding ' is rendered double-quoted (M11)
            ("'a\\\\b'", "a\\b"),  # a backslash is rendered doubled
            ('"dq"', "dq"),
            ("'a\\nb'", "a\nb"),  # the servers render a newline as \n (M11)
            ("'a\nb'", "a\nb"),  # the embedded engine renders it as a raw newline (M11)
            ("'tab\\there'", "tab\there"),
            ("'cr\\r'", "cr\r"),
            ("'nul\\0'", "nul\0"),
            ("'q\\\"'", 'q"'),
            ("'other\\x'", "otherx"),  # any other escape keeps the character
            ("''", ""),
            ('""', ""),
        ],
    )
    def test_unquote(self, literal: str, expected: str) -> None:
        assert unquote(literal) == expected

    @pytest.mark.parametrize(
        "literal",
        ["'a", "'a\\'", "\"a'", "'a' 'b'", "'a'x", "a", "", "d'2020-01-01'", " 'a'"],
    )
    def test_unquote_refuses_anything_but_one_terminated_literal(self, literal: str) -> None:
        with pytest.raises(DefineParseError) as error:
            unquote(literal)
        assert _reason(error) == _R_STRING

    # --- top_level_keywords ----------------------------------------------------------------------------------------

    def test_keyword_at_the_top_level(self) -> None:
        text = "TYPE string PERMISSIONS FULL"
        assert top_level_keywords(text, ["PERMISSIONS"]) == [(12, "PERMISSIONS")]
        assert top_level_keywords(text, ["TYPE", "PERMISSIONS", "FULL"]) == _positions(text, "TYPE", "PERMISSIONS", "FULL")

    @pytest.mark.parametrize(
        "text",
        [
            "VALUE 'a PERMISSIONS b'",
            'VALUE "a PERMISSIONS b"',
            "VALUE `PERMISSIONS`",
            "VALUE ⟨PERMISSIONS⟩",
            "VALUE (a PERMISSIONS b)",
            "VALUE [a, PERMISSIONS]",
            "VALUE { a: PERMISSIONS }",
            "VALUE f(g([{ x: (PERMISSIONS) }]))",
        ],
    )
    def test_no_keyword_inside_strings_identifiers_or_brackets(self, text: str) -> None:
        assert top_level_keywords(text, ["PERMISSIONS"]) == []
        assert top_level_keywords(text, ["VALUE", "PERMISSIONS"]) == [(0, "VALUE")]

    @pytest.mark.parametrize(
        "text",
        ["$value", "$VALUE", "a.type", "a.TYPE", "type::is::string(x)", "TYPE::x", "value", "VALUEX", "XVALUE", "TYPE,"],
    )
    def test_keyword_is_a_whole_upper_case_word_between_spaces(self, text: str) -> None:
        assert top_level_keywords(f"DEFAULT {text}", ["VALUE", "TYPE"]) == []

    def test_every_occurrence_is_a_candidate_in_order(self) -> None:
        # M34: a bare identifier may spell a keyword; the clause reader resolves the candidates.
        text = "DEFAULT PERMISSIONS PERMISSIONS FULL"
        assert top_level_keywords(text, ["DEFAULT", "PERMISSIONS"]) == [
            (0, "DEFAULT"),
            (8, "PERMISSIONS"),
            (20, "PERMISSIONS"),
        ]

    def test_keywords_matching_at_one_position_come_in_the_order_given(self) -> None:
        assert top_level_keywords("WITH REFRESH", ["WITH REFRESH", "WITH"]) == [(0, "WITH REFRESH"), (0, "WITH")]

    def test_two_word_keywords(self) -> None:
        text = "SIGNIN (SELECT * FROM u WITH x) WITH REFRESH WITH JWT ALGORITHM HS512 WITH ISSUER KEY 'k'"
        keywords = ["SIGNIN", "WITH REFRESH", "WITH JWT", "ALGORITHM", "WITH ISSUER"]
        assert top_level_keywords(text, keywords) == _positions(
            text, "SIGNIN", "WITH REFRESH", "WITH JWT", "ALGORITHM", "WITH ISSUER"
        )
        text = "TYPE record<a> REFERENCE ON DELETE UNSET PERMISSIONS FULL"
        assert top_level_keywords(text, ["REFERENCE", "ON DELETE", "PERMISSIONS"]) == _positions(
            text, "REFERENCE", "ON DELETE", "PERMISSIONS"
        )
        # The single space the server renders; the second word must end where a word ends.
        assert top_level_keywords("ON  DELETE x", ["ON DELETE"]) == []
        assert top_level_keywords("ON DELETEX", ["ON DELETE"]) == []

    def test_start(self) -> None:
        text = "DEFINE FIELD TYPE ON t TYPE int PERMISSIONS FULL"
        header_end = text.index(" ON t") + len(" ON t")
        assert top_level_keywords(text, ["TYPE", "ON"], header_end) == [(header_end + 1, "TYPE")]
        # The position start is an edge of the scanned text.
        assert top_level_keywords("xTYPE int", ["TYPE"], 1) == [(1, "TYPE")]

    # --- IF units (M35) --------------------------------------------------------------------------------------------

    def test_if_unit_hides_its_then(self) -> None:
        text = "IF $event = 'CREATE' THEN true ELSE false END THEN (CREATE log SET x = 1)"
        assert top_level_keywords(text, ["THEN"]) == [(text.rindex("THEN"), "THEN")]
        # IF and END stand at the depth around the unit; ELSE and the unit's THEN are inside it.
        assert top_level_keywords(text, _UNIT_WORDS) == [
            (0, "IF"),
            (text.index("END"), "END"),
            (text.rindex("THEN"), "THEN"),
        ]

    def test_else_if_chain_has_one_end(self) -> None:
        text = "IF a THEN 1 ELSE IF b THEN 2 ELSE 3 END THEN (x)"
        assert top_level_keywords(text, ["THEN"]) == [(text.rindex("THEN"), "THEN")]
        assert top_level_keywords(text, _UNIT_WORDS) == [
            (0, "IF"),
            (text.index("END"), "END"),
            (text.rindex("THEN"), "THEN"),
        ]

    @pytest.mark.parametrize(
        "text",
        [
            "IF $event = 'CREATE' { true } ELSE { false } THEN (x)",
            "IF a { 1 } ELSE IF b { 2 } ELSE { 3 } THEN (x)",
        ],
    )
    def test_brace_form_opens_no_unit(self, text: str) -> None:
        found = top_level_keywords(text, _UNIT_WORDS)
        assert [word for _, word in found] == [word for word in text.split() if word in _UNIT_WORDS]
        assert found[-1] == (text.rindex("THEN"), "THEN")

    def test_parenthesised_if_is_nested_by_its_parentheses(self) -> None:
        text = "(IF $event = 'CREATE' THEN true ELSE false END) THEN (CREATE log SET x = 1)"
        assert top_level_keywords(text, _UNIT_WORDS) == [(text.rindex("THEN"), "THEN")]

    def test_end_alone_is_an_ordinary_word(self) -> None:
        # M34: a field named END, rendered bare.
        text = "WHEN END THEN (CREATE log SET x = 1)"
        assert top_level_keywords(text, ["WHEN", "THEN", "END"]) == [(0, "WHEN"), (5, "END"), (9, "THEN")]
        text = "VALUE END + COMMENT PERMISSIONS FULL"
        assert top_level_keywords(text, ["VALUE", "END", "COMMENT", "PERMISSIONS"]) == _positions(
            text, "VALUE", "END", "COMMENT", "PERMISSIONS"
        )

    @pytest.mark.parametrize(
        "text",
        [
            # M35 goldens: the WHEN of e1, e3 and ev3 on 2.x/emb (bare) and 3.x (parenthesised).
            "WHEN IF $event = 'CREATE' THEN true ELSE false END THEN (CREATE log SET x = 1)",
            "WHEN (IF $event = 'CREATE' THEN true ELSE false END) THEN (CREATE log SET x = 1)",
            "WHEN IF $event = 'CREATE' { true } ELSE { false } THEN (CREATE log SET x = 1)",
            "WHEN (IF $event = 'CREATE' { true } ELSE { false }) THEN (CREATE log SET x = 1)",
            "WHEN IF $event = 'CREATE' THEN true ELSE IF $event = 'UPDATE' THEN true ELSE false END THEN { IF $event ="
            " 'CREATE' { CREATE log SET x = 1; } ELSE { CREATE log SET x = 2; }; }",
            "WHEN (IF $event = 'CREATE' THEN true ELSE IF $event = 'UPDATE' THEN true ELSE false END) THEN { IF $event"
            " = 'CREATE' { CREATE log SET x = 1 } ELSE { CREATE log SET x = 2 } }",
            "WHEN IF $event = 'CREATE' THEN (WHEN) ELSE (THEN) END THEN (CREATE log SET x = 1), (CREATE log SET x = 2)"
            " COMMENT 'c'",
            "WHEN (IF $event = 'CREATE' THEN WHEN ELSE THEN END) THEN (CREATE log SET x = 1), (CREATE log SET x = 2)"
            " COMMENT 'c'",
        ],
    )
    def test_event_goldens_have_one_top_level_then(self, text: str) -> None:
        found = top_level_keywords(text, ["WHEN", "THEN", "COMMENT"])
        then = text.index(" THEN (CREATE") if " THEN (CREATE" in text else text.index(" THEN {")
        expected = [(0, "WHEN"), (then + 1, "THEN")]
        if text.endswith("COMMENT 'c'"):
            expected.append((text.rindex("COMMENT"), "COMMENT"))
        assert found == expected

    @pytest.mark.parametrize(
        "text",
        [
            # M35 goldens: VALUE i1–i5 and k7, both renderings.
            "VALUE IF b THEN 1 ELSE 2 END PERMISSIONS FULL",
            "VALUE (IF b THEN 1 ELSE 2 END) PERMISSIONS FULL",
            "VALUE IF b { 1 } ELSE { 2 } PERMISSIONS FULL",
            "VALUE (IF b { 1 } ELSE { 2 }) PERMISSIONS FULL",
            "VALUE IF b THEN 1 ELSE IF c THEN 2 ELSE 3 END PERMISSIONS FULL",
            "VALUE (IF b THEN 1 ELSE IF c THEN 2 ELSE 3 END) PERMISSIONS FULL",
            "VALUE IF b { 1 } ELSE IF c { 2 } ELSE { 3 } PERMISSIONS FULL",
            "VALUE (IF b { 1 } ELSE IF c { 2 } ELSE { 3 }) PERMISSIONS FULL",
            "VALUE IF b THEN 1 END PERMISSIONS FULL",
            "VALUE (IF b THEN 1 END) PERMISSIONS FULL",
            "VALUE IF a THEN END ELSE b END PERMISSIONS FULL",
            "VALUE (IF a THEN END ELSE b END) PERMISSIONS FULL",
            # The 2.x and 3.x blocks.
            "VALUE {\nLET $x = 1;\nRETURN $x + 1;\n} PERMISSIONS FULL",
            "VALUE { LET $x = 1; RETURN $x + 1; } PERMISSIONS FULL",
        ],
    )
    def test_value_goldens_read_their_clauses(self, text: str) -> None:
        assert top_level_keywords(text, ["VALUE", "PERMISSIONS"]) == [
            (0, "VALUE"),
            (text.index(" PERMISSIONS") + 1, "PERMISSIONS"),
        ]

    @pytest.mark.parametrize(
        "text",
        [
            "IF a THEN IF b THEN 1 END ELSE 2 END THEN (x)",  # a unit inside a branch
            "IF IF a THEN b END THEN c END THEN (x)",  # a unit inside a condition
            "IF END THEN 1 END THEN (x)",  # a field named END in the condition
            "IF a THEN IF b { 1 } ELSE IF c { 2 } END THEN (x)",  # the ELSE IF of a brace chain inside a unit
            "IF a THEN { 1 } ELSE IF b THEN 2 END THEN (x)",  # a block as a branch
            "IF a THEN [1, (2)] ELSE { x: 1 } END THEN (x)",
            "IF a THEN $END ELSE b END THEN (x)",  # a parameter is never the END of a unit
            "IF a THEN x.END ELSE b END THEN (x)",  # nor a field path segment
        ],
    )
    def test_nested_units(self, text: str) -> None:
        assert top_level_keywords(text, ["THEN", "ELSE"]) == [(text.rindex("THEN"), "THEN")]

    @pytest.mark.parametrize(
        "text",
        [
            "IF a THEN 1",
            "VALUE IF a THEN 1 PERMISSIONS FULL",
            "(IF a THEN 1) THEN (x)",
            "IF a THEN (1 END)",
            "IF a THEN IF b THEN 1 END",
        ],
    )
    def test_unit_without_end(self, text: str) -> None:
        with pytest.raises(DefineParseError) as error:
            top_level_keywords(text, ["THEN"])
        assert _reason(error) == _R_IF

    @pytest.mark.parametrize(
        "body",
        [
            "IF " * 5000 + "x",  # IFs that open no unit: each one stops at the next
            "IF a THEN " * 3000 + "1" + " END" * 3000,  # units nested 3000 deep
            "(" * 5000 + ")" * 5000,
            "IF a { 1 } ELSE " * 3000 + "{ 2 }",
        ],
        ids=["plain-ifs", "nested-units", "nested-brackets", "brace-chain"],
    )
    def test_scan_stays_linear(self, body: str) -> None:
        # Server text is not trusted to be small or kind: a quadratic scan of the first case takes seconds.
        started = time.perf_counter()
        found = top_level_keywords(f"VALUE {body} PERMISSIONS FULL", ["VALUE", "PERMISSIONS"])
        assert time.perf_counter() - started < 1.0
        assert [keyword for _, keyword in found] == ["VALUE", "PERMISSIONS"]

    @pytest.mark.parametrize(
        ("text", "reason"),
        [
            ("(a", _R_BRACKETS),
            ("a)", _R_BRACKETS),
            ("(a]", _R_BRACKETS),
            ("{ [ }", _R_BRACKETS),
            ("'a", _R_STRING),
            ('VALUE "a', _R_STRING),
            ("VALUE 'a\\'", _R_STRING),
            ("VALUE `a", _R_IDENT),
            ("VALUE ⟨a", _R_IDENT),
        ],
    )
    def test_scan_errors(self, text: str, reason: str) -> None:
        with pytest.raises(DefineParseError) as error:
            top_level_keywords(text, ["VALUE"])
        assert _reason(error) == reason

    # --- split_top_level -------------------------------------------------------------------------------------------

    def test_split_top_level(self) -> None:
        parts = split_top_level("a, f(b, c), [d, e], 'x, y'", ",")
        assert parts == ["a", " f(b, c)", " [d, e]", " 'x, y'"]
        assert split_top_level('`a,b`, ⟨c,d⟩, { e: 1, f: 2 }, "g,h"', ",") == [
            "`a,b`",
            " ⟨c,d⟩",
            " { e: 1, f: 2 }",
            ' "g,h"',
        ]
        assert split_top_level("", ",") == [""]
        assert split_top_level("a", ",") == ["a"]
        assert split_top_level("a,", ",") == ["a", ""]
        assert split_top_level("a = 1 == b", "=") == ["a ", " 1 ", "", " b"]

    def test_split_top_level_angle_brackets_only_for_kinds(self) -> None:
        kind = "a: array<int, 3>, b: set<string, 2>"
        assert split_top_level(kind, ",", angle=True) == ["a: array<int, 3>", " b: set<string, 2>"]
        assert len(split_top_level(kind, ",")) == 4
        with pytest.raises(DefineParseError) as error:
            split_top_level("array<int", ",", angle=True)
        assert _reason(error) == _R_BRACKETS

    def test_split_top_level_errors(self) -> None:
        with pytest.raises(DefineParseError) as error:
            split_top_level("f(a, b", ",")
        assert _reason(error) == _R_BRACKETS
        with pytest.raises(DefineParseError) as error:
            split_top_level("a, 'b", ",")
        assert _reason(error) == _R_STRING

    # --- read_identifier -------------------------------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("text", "pos", "name", "end"),
        [
            ("TYPE", 0, "TYPE", 4),
            ("ON ON t", 0, "ON", 2),
            ("ON ON t", 3, "ON", 5),
            ("`TABLE` TYPE ANY", 0, "TABLE", 7),
            ("`IF`", 0, "IF", 4),
            ("`we\\`ird` TYPE", 0, "we`ird", 9),
            ("`back\\\\slash`", 0, "back\\slash", 13),
            ("`multi\\nline`", 0, "multi\nline", 13),  # the server renders the newline as \n (M5b)
            ("⟨with space⟩ ON t", 0, "with space", 12),
            ("⟨a\\⟩b⟩", 0, "a⟩b", 6),
            ("9lives", 0, "9lives", 6),
            ("isp_user SET", 0, "isp_user", 8),
            ("a-b", 0, "a", 1),
        ],
    )
    def test_read_identifier(self, text: str, pos: int, name: str, end: int) -> None:
        assert read_identifier(text, pos) == (name, end)

    @pytest.mark.parametrize(
        ("text", "pos", "reason"),
        [
            ("`unterminated", 0, _R_IDENT),
            ("`a\\`", 0, _R_IDENT),
            ("⟨open", 0, _R_IDENT),
            ("", 0, _R_HEADER),
            (" x", 0, _R_HEADER),
            ("(x", 0, _R_HEADER),
            ("$x", 0, _R_HEADER),
            ("abc", 3, _R_HEADER),
        ],
    )
    def test_read_identifier_errors(self, text: str, pos: int, reason: str) -> None:
        with pytest.raises(DefineParseError) as error:
            read_identifier(text, pos)
        assert _reason(error) == reason

    # --- read_idiom and canonical_name -----------------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("text", "path", "name"),
        [
            ("email", ("email",), "email"),
            ("address.city", ("address", "city"), "address.city"),
            ("`first-name`", ("first-name",), "first-name"),
            ("`select`", ("select",), "select"),
            ("`with space`", ("with space",), "with space"),
            ("TYPE", ("TYPE",), "TYPE"),
            ("tags[*]", ("tags", "*"), "tags.*"),
            ("tags.*", ("tags", "*"), "tags.*"),
            ("w[*].z", ("w", "*", "z"), "w.*.z"),
            ("w.*.z", ("w", "*", "z"), "w.*.z"),
            ("`a-b`.c", ("a-b", "c"), "a-b.c"),
            ("a.`b.c`", ("a", "b.c"), "a.b.c"),  # a segment holding a dot: FieldState.path keeps it apart
            ("m[*][*]", ("m", "*", "*"), "m.*.*"),
        ],
    )
    def test_read_idiom(self, text: str, path: tuple[str, ...], name: str) -> None:
        assert read_idiom(text, 0) == (path, len(text))
        assert read_idiom(f"DEFINE FIELD {text} ON t", 13) == (path, 13 + len(text))
        assert canonical_name(path) == name

    def test_canonical_name_is_the_same_for_both_item_renderings(self) -> None:
        # M8: 2.x renders tags[*], 3.x tags.* — one name (D5).
        assert canonical_name(read_idiom("tags[*]", 0)[0]) == canonical_name(read_idiom("tags.*", 0)[0]) == "tags.*"
        assert canonical_name(read_idiom("w[*].z", 0)[0]) == canonical_name(read_idiom("w.*.z", 0)[0]) == "w.*.z"
        assert canonical_name(["address", "city"]) == "address.city"

    @pytest.mark.parametrize(
        ("text", "reason"),
        [
            ("", _R_PATH),
            (" a", _R_PATH),
            ("*", _R_PATH),
            ("`unterminated", _R_IDENT),
            ("a.`unterminated", _R_IDENT),
            ("a..b", _R_PATH),
            ("a.", _R_PATH),
            ("a. b", _R_PATH),
            ("a[0]", _R_PATH),
            ("a[*", _R_PATH),
            ("a[", _R_PATH),
        ],
    )
    def test_read_idiom_errors(self, text: str, reason: str) -> None:
        with pytest.raises(DefineParseError) as error:
            read_idiom(text, 0)
        assert _reason(error) == reason


_INT = KName("int")
_STRING = KName("string")
_NONE = KLiteral("none")

#: Every kind the goldens hold (M7, M15, M21, probe13 ``p`` and ``l``; spec §6.3.5) → its canonical rendering. A 2.x
#: rendering and its 3.x rendering sit side by side where they differ.
_GOLDEN_KINDS = [
    # M21: the names, rendered alike by both lines.
    *[
        (name, name)
        for name in (
            "any",
            "bool",
            "bytes",
            "datetime",
            "decimal",
            "duration",
            "float",
            "int",
            "number",
            "object",
            "string",
            "uuid",
            "regex",
            "range",
            "function",
            "file",
            "record",
            "geometry",
            "array",
            "set",
        )
    ],
    # M21: generic arguments, literals, object and tuple literals, rendered alike by both lines.
    ("array<string, 10>", "array<string, 10>"),
    ("set<int, 3>", "set<int, 3>"),
    ("set<string>", "set<string>"),
    ("set<record<a>>", "set<record<a>>"),
    ("array<object>", "array<object>"),
    ("record<a | b>", "record<a | b>"),
    ("record<k | other>", "record<k | other>"),
    ("geometry<point>", "geometry<point>"),
    ("geometry<point | polygon>", "geometry<point | polygon>"),
    ("'a' | 'b'", "'a' | 'b'"),
    ("'dq'", "'dq'"),  # "dq" is rendered 'dq'
    ("1 | 2 | 3", "1 | 2 | 3"),
    ("true | false", "true | false"),
    ("{ a: int, b: string }", "{ a: int, b: string }"),
    ("[int, string]", "[int, string]"),
    ("null | int", "null | int"),
    # probe13 p (its TYPE is followed by two spaces) and l.
    ("  1.5f | 2", "1.5f | 2"),
    ("'dq' | \"it's\"", "'dq' | \"it's\""),
    # M7: optional kinds — 2.x renders option<T>, 3.x none | T (order kept: int | none stays as written).
    ("option<int>", "option<int>"),
    ("none | int", "option<int>"),
    ("int | none", "option<int>"),
    ("option<int | string>", "option<int | string>"),
    ("none | int | string", "option<int | string>"),
    ("array<option<int>, 5>", "array<option<int>, 5>"),
    ("array<none | int, 5>", "array<option<int>, 5>"),
    ("option<array<option<string>>>", "option<array<option<string>>>"),
    ("none | array<none | string>", "option<array<option<string>>>"),
    ("option<{ x: int, y: option<string> }>", "option<{ x: int, y: option<string> }>"),
    ("none | { x: int, y: none | string }", "option<{ x: int, y: option<string> }>"),
    ("option<record<k | other>>", "option<record<k | other>>"),
    ("none | record<k | other>", "option<record<k | other>>"),
    # M15: a function's return kinds.
    ("option<string>", "option<string>"),
    ("none | string", "option<string>"),
    ("{ a: int }", "{ a: int }"),
]


class TestKinds:
    """Spec §6.3.5 (D5; AC-6): one grammar for every rendered kind, one canonical rendering both lines accept
    (``option<T>`` at every depth, the 3.x spacing), and the top-level optional read as ``nullable``."""

    # --- parse_kind: the tree ----------------------------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("text", "tree"),
        [
            ("int", _INT),
            ("frob", KName("frob")),  # an unknown name is kept
            ("array", KName("array")),
            ("array<string, 10>", KName("array", (_STRING, "10"))),
            ("set<int, 3>", KName("set", (_INT, "3"))),
            ("set<record<a>>", KName("set", (KName("record", ("a",)),))),
            ("record", KName("record")),
            ("record<a | b>", KName("record", ("a", "b"))),
            ("record<`weird-name` | b>", KName("record", ("`weird-name`", "b"))),  # names kept as rendered
            ("geometry<point | polygon>", KName("geometry", ("point", "polygon"))),
            ("file<bucket>", KName("file", ("bucket",))),  # other arguments: their text, as rendered
            ("frob<a<b>, 'c>'>", KName("frob", ("a<b>, 'c>'",))),
            ("'a' | 'b'", KUnion((KLiteral("'a'"), KLiteral("'b'")))),
            ("'dq' | \"it's\"", KUnion((KLiteral("'dq'"), KLiteral('"it\'s"')))),
            ("1.5f | 2", KUnion((KLiteral("1.5f"), KLiteral("2")))),
            ("-0f | 1.5dec | -2 | 1000f", KUnion((KLiteral("-0f"), KLiteral("1.5dec"), KLiteral("-2"), KLiteral("1000f")))),
            ("true | false", KUnion((KLiteral("true"), KLiteral("false")))),
            ("null | int", KUnion((KLiteral("null"), _INT))),
            ("none", _NONE),
            ("{ a: int, b: string }", KObject((("a", _INT), ("b", _STRING)))),
            ("{ `a-b`: int, 'c d': string }", KObject((("`a-b`", _INT), ("'c d'", _STRING)))),
            ("{}", KObject(())),
            ("[int, string]", KTuple((_INT, _STRING))),
            # option<T> is the union of none and T's members: both renderings give one tree.
            ("option<int>", KUnion((_NONE, _INT))),
            ("none | int", KUnion((_NONE, _INT))),
            ("option<int | string>", KUnion((_NONE, _INT, _STRING))),
            ("none | int | string", KUnion((_NONE, _INT, _STRING))),
            ("array<option<int>, 5>", KName("array", (KUnion((_NONE, _INT)), "5"))),
            ("array<none | int, 5>", KName("array", (KUnion((_NONE, _INT)), "5"))),
            (
                "option<{ x: int, y: option<string> }>",
                KUnion((_NONE, KObject((("x", _INT), ("y", KUnion((_NONE, _STRING))))))),
            ),
            (
                "none | { x: int, y: none | string }",
                KUnion((_NONE, KObject((("x", _INT), ("y", KUnion((_NONE, _STRING))))))),
            ),
        ],
    )
    def test_parse_kind(self, text: str, tree: object) -> None:
        assert parse_kind(text) == tree

    @pytest.mark.parametrize(
        ("text", "canonical"),
        [
            ("array<string,10>", "array<string, 10>"),
            ("set< int ,3 >", "set<int, 3>"),
            ("record<a|b>", "record<a | b>"),
            ("geometry<point|polygon>", "geometry<point | polygon>"),
            ("{a:int,b:string}", "{ a: int, b: string }"),
            ("[ int , string ]", "[int, string]"),
            ("'a'|'b'", "'a' | 'b'"),
            ("option< int >", "option<int>"),
            ("\tint\n", "int"),
            ("{}", "{  }"),
            ("{  }", "{  }"),
        ],
    )
    def test_render_kind_spacing(self, text: str, canonical: str) -> None:
        assert render_kind(parse_kind(text)) == canonical

    def test_render_kind_moves_none_into_option_at_any_depth(self) -> None:
        assert render_kind(KUnion((_INT, _NONE))) == "option<int>"
        assert render_kind(KUnion((_INT, _NONE, _STRING))) == "option<int | string>"
        assert render_kind(KTuple((KUnion((_NONE, _INT)), _STRING))) == "[option<int>, string]"
        assert render_kind(KObject((("a", KName("array", (KUnion((_STRING, _NONE)),))),))) == "{ a: array<option<string>> }"
        assert render_kind(KUnion((_NONE, _NONE))) == "none"
        assert render_kind(KUnion((KLiteral("null"), _INT))) == "null | int"  # null is a value, not none

    # --- parse_kind_prefix -------------------------------------------------------------------------------------------

    def test_prefix_stops_before_a_function_body(self) -> None:
        # M15 fn::kw: an object kind before the body's brace.
        text = "{ a: int } { RETURN $a; }"
        assert parse_kind_prefix(text, 0) == (KObject((("a", _INT),)), text.index("{ RETURN"))
        assert parse_kind_prefix("string { RETURN $s; }", 0) == (_STRING, 7)
        assert parse_kind_prefix("int", 0) == (_INT, 3)
        # The text after the kind is not read: a comparison in the body is not an angle bracket.
        assert parse_kind_prefix("bool { RETURN $a < $b; }", 0) == (KName("bool"), 5)
        text = "int | { a: int } {\nRETURN 1;\n}"
        assert parse_kind_prefix(text, 0) == (KUnion((_INT, KObject((("a", _INT),)))), text.index("{\n"))

    @pytest.mark.parametrize(
        "header",
        [
            "DEFINE FUNCTION fn::f($s: string) -> option<string> {\nRETURN $s;\n} PERMISSIONS FULL",  # 2.x
            "DEFINE FUNCTION fn::f($s: string) -> none | string { RETURN $s } PERMISSIONS FULL",  # 3.x
        ],
    )
    def test_prefix_reads_a_return_kind_in_a_header(self, header: str) -> None:
        kind, end = parse_kind_prefix(header, header.index("->") + 2)
        assert kind == KUnion((_NONE, _STRING))
        assert render_kind(kind) == "option<string>"
        assert end == header.index("{")

    @pytest.mark.parametrize("text", ["", "   ", "{ RETURN 1; }", "int | { RETURN 1 }", "<int> { }", "int |"])
    def test_prefix_refuses(self, text: str) -> None:
        with pytest.raises(DefineParseError) as error:
            parse_kind_prefix(text, 0)
        assert _reason(error) == _R_KIND

    # --- normalise_kind ----------------------------------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("renderings", "expected"),
        [
            # Spec §6.3.5's pairs: each 2.x rendering and its 3.x rendering give one result (AC-6).
            (["option<int>", "none | int", "int | none"], ("int", True)),
            (["option<int | string>", "none | int | string"], ("int | string", True)),
            (["array<option<int>, 5>", "array<none | int, 5>"], ("array<option<int>, 5>", False)),
            (["option<array<option<string>>>", "none | array<none | string>"], ("array<option<string>>", True)),
            (
                ["option<{ x: int, y: option<string> }>", "none | { x: int, y: none | string }"],
                ("{ x: int, y: option<string> }", True),
            ),
            (["option<record<k | other>>", "none | record<k | other>"], ("record<k | other>", True)),
            (["option<string>", "none | string"], ("string", True)),  # M15
            (["'a' | 'b'"], ("'a' | 'b'", False)),
            (["null | int"], ("null | int", False)),
            (["int"], ("int", False)),
            (["  1.5f | 2"], ("1.5f | 2", False)),
            (["none"], ("none", False)),  # not a union holding none: nothing to strip
        ],
    )
    def test_normalise_kind(self, renderings: list[str], expected: tuple[str, bool]) -> None:
        for text in renderings:
            assert normalise_kind(text) == expected, text

    @pytest.mark.parametrize(("text", "canonical"), _GOLDEN_KINDS)
    def test_golden_kinds_render_canonically_and_stably(self, text: str, canonical: str) -> None:
        kind = parse_kind(text)
        assert render_kind(kind) == canonical
        # The canonical text is a fixed point: it parses back to the same tree, which renders the same text.
        assert render_kind(parse_kind(canonical)) == canonical
        assert parse_kind(render_kind(parse_kind(canonical))) == parse_kind(canonical)
        # Spec §6.3.5 item 3 and AC-6: normalising the rendering changes nothing; normalise is stable on its output.
        assert normalise_kind(render_kind(kind)) == normalise_kind(text)
        field_type, _ = normalise_kind(text)
        assert normalise_kind(field_type) == (field_type, False)

    # --- refused kinds -----------------------------------------------------------------------------------------------

    @pytest.mark.parametrize(
        "text",
        [
            # The plan's cases.
            "option<",
            "array<string,>",
            "{ a int }",
            "<>",
            "int |",
            "+ 1",
            # And more of the same.
            "",
            "   ",
            "| int",
            "int || string",
            "option",
            "option<>",
            "option <int>",
            "option<int",
            "array<int, x>",
            "array<int, 3",
            "array<int, -1>",
            "array<int, 3, 4>",
            "array <int>",
            "record<>",
            "record<a |>",
            "record<a b>",
            "record<`a>",
            "record<a, b>",
            "geometry<>",
            "frob<>",
            "frob<a",
            "frob<a)>",
            "frob<'a>",
            "[]",
            "[int,]",
            "[int",
            "{ a: int, }",
            "{ a: }",
            "{ a: int",
            "{ `a: int }",
            "{ (a): int }",
            "int string",
            "int,",
            "int>",
            "array<int>>",
            "'unterminated",
            "1abc",
            "9lives",
            "1.",
            "1.5e3",
            "- 1",
            "(int)",
            "$value",
            "int::x",
        ],
    )
    def test_refused_kinds(self, text: str) -> None:
        with pytest.raises(DefineParseError) as error:
            parse_kind(text)
        assert _reason(error) == _R_KIND
        with pytest.raises(DefineParseError) as error:
            normalise_kind(text)
        assert _reason(error) == _R_KIND

    @pytest.mark.parametrize(
        "text",
        [
            "array<" * 5000 + "int" + ">" * 5000,
            "option<" * 5000 + "int" + ">" * 5000,
            "[" * 5000 + "int" + "]" * 5000,
            "{ a: " * 5000 + "int" + " }" * 5000,
        ],
        ids=["array", "option", "tuple", "object"],
    )
    def test_too_deep_a_kind_is_refused(self, text: str) -> None:
        # Server text is not trusted to be small: a kind nested past any real one is refused, not a RecursionError.
        with pytest.raises(DefineParseError) as error:
            parse_kind(text)
        assert _reason(error) == _R_KIND

    def test_nested_kinds_parse(self) -> None:
        text = "array<" * 32 + "option<{ a: [int, set<string, 2>] }>" + ">" * 32
        canonical = render_kind(parse_kind(text))
        assert canonical == text
        assert normalise_kind(text) == (text, False)

    @pytest.mark.parametrize(
        "text",
        [
            " | ".join(["int"] * 20_000),
            " | ".join(f"'v{n}'" for n in range(20_000)),
            "frob<" + "(" * 5000 + ")" * 5000 + ">",
            "{ " + ", ".join(f"k{n}: int" for n in range(20_000)) + " }",
        ],
        ids=["union", "literals", "raw-argument", "object"],
    )
    def test_parse_kind_stays_linear(self, text: str) -> None:
        started = time.perf_counter()
        assert render_kind(parse_kind(text)) == text
        assert time.perf_counter() - started < 1.0


_FIELD_ACTIONS = ("select", "create", "update")
_TABLE_ACTIONS = ("select", "create", "update", "delete")
_FIELD_FULL = dict.fromkeys(_FIELD_ACTIONS, "FULL")
_TABLE_NONE = dict.fromkeys(_TABLE_ACTIONS, "NONE")

_VALUE = Slot("value", ("VALUE",), define_parser._valid_expr)
_ASSERT = Slot("assert", ("ASSERT",), define_parser._valid_expr)
_READONLY = Slot("readonly", ("READONLY",), define_parser._valid_flag)
_COMMENT = Slot("comment", ("COMMENT",), define_parser._valid_string)
_PERMISSIONS = Slot("permissions", ("PERMISSIONS",), define_parser._valid_field_permissions)


class TestClauseReader:
    """Spec §6.3.2: a reading assigns a strictly increasing run of top-level keyword candidates to slots in their
    order; only a text with exactly one reading is read — none gives ``_R_NO_READING``, more ``_R_AMBIGUOUS``."""

    def test_one_reading(self) -> None:
        text = "VALUE a + 1 ASSERT $value > 0 PERMISSIONS FULL"
        clauses = read_clauses(text, 0, (_VALUE, _ASSERT, _PERMISSIONS))
        assert clauses == {
            "value": Clause("VALUE", "a + 1"),
            "assert": Clause("ASSERT", "$value > 0"),
            "permissions": Clause("PERMISSIONS", "FULL"),
        }

    def test_the_text_starts_after_the_header(self) -> None:
        text = "DEFINE FIELD VALUE ON t VALUE VALUE + 1"
        assert read_clauses(text, text.index(" VALUE VALUE"), (_VALUE,)) == {"value": Clause("VALUE", "VALUE + 1")}

    @pytest.mark.parametrize("text", ["", "   ", "\n"])
    def test_an_empty_text_has_one_reading_without_clauses(self, text: str) -> None:
        assert read_clauses(text, 0, (_VALUE, _ASSERT)) == {}

    def test_slots_come_in_their_order(self) -> None:
        # VALUE cannot follow ASSERT: the second keyword is part of the assertion.
        assert read_clauses("ASSERT a VALUE b", 0, (_VALUE, _ASSERT)) == {"assert": Clause("ASSERT", "a VALUE b")}
        # READONLY cannot follow VALUE (M34 k1).
        assert read_clauses("VALUE a * READONLY", 0, (_READONLY, _VALUE)) == {"value": Clause("VALUE", "a * READONLY")}

    def test_a_keyword_starts_its_clause_wherever_it_can(self) -> None:
        # The expression does not take the ASSERT that can start its clause: one reading, not two.
        assert read_clauses("VALUE a ASSERT b", 0, (_VALUE, _ASSERT)) == {
            "value": Clause("VALUE", "a"),
            "assert": Clause("ASSERT", "b"),
        }
        # M34 k4: `a +` ends with an operator, so the first ASSERT is a field the expression ends with.
        clauses = read_clauses("VALUE a + ASSERT ASSERT $value > 0 PERMISSIONS FULL", 0, (_VALUE, _ASSERT, _PERMISSIONS))
        assert clauses["value"] == Clause("VALUE", "a + ASSERT")
        assert clauses["assert"] == Clause("ASSERT", "$value > 0")

    @pytest.mark.parametrize(
        "text",
        [
            "READONLY x VALUE y",  # a flag has no content
            "x VALUE y",  # the text does not start at a keyword
            "VALUE a +",  # an expression does not end with an operator
            "VALUE",  # nor is it empty
            "COMMENT 'x' 'y'",  # one string literal
            "COMMENT 'a' COMMENT 'b'",  # a clause twice
            "READONLY READONLY",
            "PERMISSIONS FOR delete NONE",  # a field has no delete permission
        ],
    )
    def test_no_reading(self, text: str) -> None:
        with pytest.raises(DefineParseError) as error:
            read_clauses(text, 0, (_READONLY, _VALUE, _COMMENT, _PERMISSIONS))
        assert _reason(error) == _R_NO_READING

    def test_an_unknown_clause_after_an_expression_is_part_of_it(self) -> None:
        # Spec §6.3.2: after an expression clause, an unknown clause is read as part of the expression.
        slots = (_VALUE, _COMMENT, _PERMISSIONS)
        assert read_clauses("VALUE a COMMENT 'x' 'y'", 0, slots) == {"value": Clause("VALUE", "a COMMENT 'x' 'y'")}
        assert read_clauses("VALUE a FROB 3 PERMISSIONS FULL", 0, slots)["value"] == Clause("VALUE", "a FROB 3")

    def test_two_readings(self) -> None:
        # Spec §9.2's fabricated statement: `b` then `ASSERT c`, or `b ASSERT` then `c` — never a guess.
        with pytest.raises(DefineParseError) as error:
            read_clauses("VALUE b ASSERT ASSERT c PERMISSIONS FULL", 0, (_VALUE, _ASSERT, _PERMISSIONS))
        assert _reason(error) == _R_AMBIGUOUS

    def test_a_required_slot(self) -> None:
        when = Slot("when", ("WHEN",), define_parser._valid_expr, required=True)
        then = Slot("then", ("THEN",), define_parser._valid_body, required=True)
        assert read_clauses("WHEN THEN = 1 THEN (x)", 0, (when, then)) == {
            "when": Clause("WHEN", "THEN = 1"),
            "then": Clause("THEN", "(x)"),
        }
        for text in ["WHEN a", "THEN (x)", ""]:
            with pytest.raises(DefineParseError) as error:
                read_clauses(text, 0, (when, then))
            assert _reason(error) == _R_NO_READING

    def test_two_slots_of_one_name_are_used_once(self) -> None:
        # FLEXIBLE has a slot before TYPE (2.x) and one after (3.x), M9: one of them only.
        slots = (
            Slot("flexible", ("FLEXIBLE",), define_parser._valid_flag),
            Slot("type", ("TYPE",), define_parser._valid_kind),
            Slot("flexible", ("FLEXIBLE",), define_parser._valid_flag),
        )
        assert read_clauses("FLEXIBLE TYPE object", 0, slots) == {
            "flexible": Clause("FLEXIBLE", ""),
            "type": Clause("TYPE", "object"),
        }
        assert read_clauses("TYPE object FLEXIBLE", 0, slots) == {
            "type": Clause("TYPE", "object"),
            "flexible": Clause("FLEXIBLE", ""),
        }
        with pytest.raises(DefineParseError) as error:
            read_clauses("FLEXIBLE TYPE object FLEXIBLE", 0, slots)
        assert _reason(error) == _R_NO_READING

    def test_one_slot_of_two_keywords(self) -> None:
        schema = Slot("schema", ("SCHEMAFULL", "SCHEMALESS"), define_parser._valid_flag)
        assert read_clauses("SCHEMALESS", 0, (schema,)) == {"schema": Clause("SCHEMALESS", "")}
        assert read_clauses("SCHEMAFULL", 0, (schema,)) == {"schema": Clause("SCHEMAFULL", "")}

    def test_two_word_keywords(self) -> None:
        slots = (
            Slot("refresh", ("WITH REFRESH",), define_parser._valid_flag),
            Slot("jwt", ("WITH JWT",), define_parser._valid_tokens),
            Slot("algorithm", ("ALGORITHM",), define_parser._valid_tokens),
        )
        assert read_clauses("WITH REFRESH WITH JWT ALGORITHM HS512 KEY '[REDACTED]'", 0, slots) == {
            "refresh": Clause("WITH REFRESH", ""),
            "jwt": Clause("WITH JWT", ""),
            "algorithm": Clause("ALGORITHM", "HS512 KEY '[REDACTED]'"),
        }

    @pytest.mark.parametrize(
        ("text", "reason"),
        [
            ("VALUE (a", _R_BRACKETS),
            ("VALUE 'a", _R_STRING),
            ("VALUE `a", _R_IDENT),
            ("VALUE IF a THEN 1 PERMISSIONS FULL", _R_IF),
        ],
    )
    def test_a_truncated_text(self, text: str, reason: str) -> None:
        with pytest.raises(DefineParseError) as error:
            read_clauses(text, 0, (_VALUE, _PERMISSIONS))
        assert _reason(error) == reason

    # --- validators ------------------------------------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("validator", "accepted", "refused"),
        [
            ("_valid_flag", [""], ["x", "'a'"]),
            (
                "_valid_expr",
                ["a + 1", "x.*", "a.*", "IF b THEN 1 ELSE 2 END", "(IF b THEN 1 END)", "'a +'", "f(a, b)", "-1", "[]"],
                ["a +", "", "(a", "a.", "a,", "IF a THEN 1", "a ->", "$x ="],
            ),
            ("_valid_string", ["'a'", '"it\'s"', "''"], ["'a' 'b'", "a", "d'2020-01-01'", "'a' + 'b'", ""]),
            ("_valid_kind", ["option<int>", "none | { x: int }", "  1.5f | 2"], ["frob<", "int TYPE", ""]),
            ("_valid_names", ["person", "person | company", "`we-ird` | b", "a|b"], ["person |", "a b", "", "| a"]),
            ("_valid_table_type", ["NORMAL", "ANY", "RELATION"], ["normal", "NORMAL x", "", "VIEW"]),
            ("_valid_default", ["ALWAYS 0", "ALWAYS", "18", "time::now()", "{  }"], ["ALWAYS +", "", "a +"]),
            (
                "_valid_reference",
                ["", "ON DELETE IGNORE", "ON DELETE UNSET", "ON DELETE CASCADE", "ON DELETE REJECT", "ON DELETE THEN (x)"],
                ["ON DELETE", "ON DELETE THEN", "ON DELETE FROB", "ON DELETE UNSET x", "ON UNSET", "x"],
            ),
            ("_valid_view", ["SELECT * FROM t", "SELECT AS FROM t"], ["SELECT", "VALUE x", "SELECT a +", ""]),
            (
                "_valid_changefeed",
                ["1d", "2h", "1h30m INCLUDE ORIGINAL", "100ms", "1µs", "4w2d"],
                ["1x", "1d INCLUDE", "", "d", "1d ORIGINAL", "1.5d"],
            ),
            (
                "_valid_body",
                ["{ a; }", "(a)", "(a), (b)", "(CREATE log SET x = 1),(x)", "{\nLET $x = 1;\n}"],
                ["(b) ELSE (c) END THEN (x)", "(a) (b)", "{ a } { b }", "a", "", "(a),", "{ a }, (b)"],
            ),
            ("_valid_group", ["(a)", "(SELECT * FROM t WHERE a = 1)"], ["(a), (b)", "{ a }", "a", ""]),
            (
                "_valid_durations",
                ["FOR TOKEN 15m, FOR SESSION NONE", "FOR GRANT 4w2d, FOR TOKEN 1m, FOR SESSION NONE", "FOR TOKEN 1h"],
                ["FOR TOKEN", "FOR TOKEN 1x", "FOR TOKEN 1m, FOR TOKEN 2m", "TOKEN 1m", "", "FOR TOKEN 1m,"],
            ),
            ("_valid_tokens", ["", "HS512 KEY '[REDACTED]'", "KEY 'k'", "1.5"], ["KEY (x)", "a = b", "[a]"]),
            ("_valid_fn_name", ["fn::an", "fn::a::b"], ["fn::", "an", "fn::a b", "fn::a::", "fn:a"]),
            ("_valid_word_list", ["BLANK,CLASS", "BLANK", "BLANK, CLASS"], ["BLANK,", "BLANK CLASS", "", ",BLANK"]),
            (
                "_valid_filter_list",
                [
                    "LOWERCASE,SNOWBALL(ENGLISH),EDGENGRAM(2,10)",
                    "LOWERCASE, SNOWBALL(ENGLISH), EDGENGRAM(2,10)",
                    "LOWERCASE, MAPPER('path/File.txt')",
                ],
                ["LOWERCASE,", "SNOWBALL(ENGLISH) x", "(x)", "", "A(b)(c)", "'a'"],
            ),
            (
                "_valid_field_permissions",
                ["FULL", "NONE", "FOR select, create FULL, FOR update NONE", "FOR select WHERE a = 1"],
                ["FOR delete NONE", "WHERE x", "", "FOR select"],
            ),
            (
                "_valid_table_permissions",
                ["NONE", "FOR select WHERE owner = $auth.id, FOR create, update, delete NONE"],
                ["FOR select", "FOR select WHERE", "FULL NONE"],
            ),
        ],
    )
    def test_validator(self, validator: str, accepted: list[str], refused: list[str]) -> None:
        check = getattr(define_parser, validator)
        for content in accepted:
            assert define_parser._validates(check, content), content
        for content in refused:
            assert not define_parser._validates(check, content), content

    def test_default_reads_always_as_the_flag(self) -> None:
        assert define_parser._split_default("ALWAYS 0") == (True, "0")
        assert define_parser._split_default("ALWAYS time::now()") == (True, "time::now()")
        assert define_parser._split_default("ALWAYS") == (False, "ALWAYS")  # a field named ALWAYS
        assert define_parser._split_default("18") == (False, "18")

    # --- memoisation -----------------------------------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ("VALUE a " + "READONLY " * 200, {"value"}),
            ("DEFAULT " + "READONLY " * 200, {"default", "readonly"}),
            ("TYPE int " + "READONLY " * 200, None),
            ("READONLY " * 200, None),
            ("VALUE a " + "ASSERT a " * 200, None),
            ("DEFAULT " + "PERMISSIONS " * 200, {"default"}),
            ("COMMENT 'x' " * 200, None),
        ],
        ids=["value", "default", "type", "readonly", "asserts", "permissions", "comments"],
    )
    def test_many_keywords_read_fast(self, body: str, expected: set[str] | None) -> None:
        # Server text is not trusted to be small or kind: counting readings is memoised and stops at two.
        text = f"DEFINE FIELD x ON t {body}PERMISSIONS FULL"
        started = time.perf_counter()
        try:
            clauses: set[str] | None = set(read_clauses(text, len("DEFINE FIELD x ON t"), define_parser._FIELD_SLOTS))
        except DefineParseError as error:
            assert str(error) in (_R_NO_READING, _R_AMBIGUOUS)
            clauses = None
        assert time.perf_counter() - started < 1.0
        assert clauses == (None if expected is None else expected | {"permissions"})


def _table(statement: str, name: str, **attributes: object) -> tuple[str, TableState]:
    """A golden table statement and the state it reads as — every attribute by keyword, ``raw`` the statement."""
    defaults: dict[str, object] = {"schema_mode": "SCHEMALESS", "table_type": "any", "permissions": _TABLE_NONE}
    return statement, TableState(name=name, **{**defaults, **attributes, "raw": statement})  # type: ignore[arg-type]


#: Spec §9.2's tables (both lines render them alike) and the probe's ``company``.
_TABLES = [
    _table(
        "DEFINE TABLE company TYPE NORMAL SCHEMAFULL PERMISSIONS NONE",
        "company",
        schema_mode="SCHEMAFULL",
        table_type="normal",
    ),
    _table(
        "DEFINE TABLE company TYPE NORMAL SCHEMAFULL COMMENT 'Companies' PERMISSIONS NONE",
        "company",
        schema_mode="SCHEMAFULL",
        table_type="normal",
        comment="Companies",
    ),
    _table(
        "DEFINE TABLE feed TYPE ANY SCHEMALESS CHANGEFEED 1d INCLUDE ORIGINAL PERMISSIONS NONE",
        "feed",
        changefeed="1d",
        changefeed_include_original=True,
    ),
    _table(
        "DEFINE TABLE likes TYPE RELATION IN person OUT person | company ENFORCED SCHEMAFULL PERMISSIONS NONE",
        "likes",
        schema_mode="SCHEMAFULL",
        table_type="relation",
        relation_in=("person",),
        relation_out=("person", "company"),
        enforced=True,
    ),
    _table("DEFINE TABLE relany TYPE RELATION SCHEMALESS PERMISSIONS NONE", "relany", table_type="relation"),
    _table(
        "DEFINE TABLE person_count TYPE ANY SCHEMALESS AS SELECT count() AS total FROM person GROUP ALL PERMISSIONS NONE",
        "person_count",
        view_query="SELECT count() AS total FROM person GROUP ALL",
    ),
    _table(
        "DEFINE TABLE secret TYPE NORMAL SCHEMAFULL PERMISSIONS FOR select WHERE owner = $auth.id, FOR create, update,"
        " delete NONE",
        "secret",
        schema_mode="SCHEMAFULL",
        table_type="normal",
        permissions={"select": "WHERE owner = $auth.id", "create": "NONE", "update": "NONE", "delete": "NONE"},
    ),
    _table("DEFINE TABLE `weird-name` TYPE ANY SCHEMALESS PERMISSIONS NONE", "weird-name"),
    _table("DEFINE TABLE `we\\`ird` TYPE ANY SCHEMALESS PERMISSIONS NONE", "we`ird"),
    _table("DEFINE TABLE `back\\\\slash` TYPE ANY SCHEMALESS PERMISSIONS NONE", "back\\slash"),
    _table("DEFINE TABLE `multi\\nline` TYPE ANY SCHEMALESS PERMISSIONS NONE", "multi\nline"),
    _table(
        'DEFINE TABLE t TYPE NORMAL SCHEMAFULL COMMENT "it\'s" PERMISSIONS NONE',
        "t",
        schema_mode="SCHEMAFULL",
        table_type="normal",
        comment="it's",
    ),
    _table("DEFINE TABLE td TYPE ANY DROP SCHEMALESS PERMISSIONS NONE", "td", drop=True),
    _table(
        "DEFINE TABLE v1 TYPE NORMAL DROP SCHEMAFULL COMMENT 'cv' AS SELECT count() AS total, math::sum(n) AS s FROM src"
        " WHERE n > 1 GROUP ALL CHANGEFEED 2h INCLUDE ORIGINAL PERMISSIONS FOR select FULL, FOR create, update, delete"
        " NONE",
        "v1",
        schema_mode="SCHEMAFULL",
        table_type="normal",
        drop=True,
        comment="cv",
        view_query="SELECT count() AS total, math::sum(n) AS s FROM src WHERE n > 1 GROUP ALL",
        changefeed="2h",
        changefeed_include_original=True,
        permissions={"select": "FULL", "create": "NONE", "update": "NONE", "delete": "NONE"},
    ),
    _table(
        "DEFINE TABLE t5 TYPE NORMAL SCHEMAFULL COMMENT 'x' CHANGEFEED 1d PERMISSIONS FOR select, update WHERE a = 1,"
        " FOR create FULL, FOR delete NONE",
        "t5",
        schema_mode="SCHEMAFULL",
        table_type="normal",
        comment="x",
        changefeed="1d",
        permissions={"select": "WHERE a = 1", "update": "WHERE a = 1", "create": "FULL", "delete": "NONE"},
    ),
    # Table permissions as both lines render them (probe of 2026-10-18).
    _table(
        "DEFINE TABLE t1 TYPE ANY SCHEMALESS PERMISSIONS FOR select FULL, FOR create, update, delete NONE",
        "t1",
        permissions={"select": "FULL", "create": "NONE", "update": "NONE", "delete": "NONE"},
    ),
    _table(
        "DEFINE TABLE t2 TYPE ANY SCHEMALESS PERMISSIONS FOR select, create, update, delete WHERE a = 1",
        "t2",
        permissions=dict.fromkeys(_TABLE_ACTIONS, "WHERE a = 1"),
    ),
    _table(
        "DEFINE TABLE t3 TYPE ANY SCHEMALESS PERMISSIONS FULL",
        "t3",
        permissions=dict.fromkeys(_TABLE_ACTIONS, "FULL"),
    ),
    # M34: keyword-named identifiers, rendered bare.
    _table("DEFINE TABLE TYPE TYPE ANY SCHEMALESS PERMISSIONS NONE", "TYPE"),
    _table("DEFINE TABLE `TABLE` TYPE ANY SCHEMALESS PERMISSIONS NONE", "TABLE"),
    _table(
        "DEFINE TABLE OUT TYPE RELATION IN t OUT t SCHEMALESS PERMISSIONS NONE",
        "OUT",
        table_type="relation",
        relation_in=("t",),
        relation_out=("t",),
    ),
    _table(
        "DEFINE TABLE ENFORCED TYPE RELATION IN OUT OUT ENFORCED ENFORCED SCHEMALESS PERMISSIONS NONE",
        "ENFORCED",
        table_type="relation",
        relation_in=("OUT",),
        relation_out=("ENFORCED",),
        enforced=True,
    ),
    _table(
        "DEFINE TABLE v TYPE NORMAL SCHEMALESS AS SELECT AS FROM t PERMISSIONS NONE",
        "v",
        table_type="normal",
        view_query="SELECT AS FROM t",
    ),
]


class TestDefineTable:
    """Spec §6.3.3 (AC-7): ``DEFINE TABLE <ident>`` read positionally, then ``TYPE``, ``IN``/``OUT``/``ENFORCED`` (a
    relation only), ``DROP``, ``SCHEMAFULL|SCHEMALESS``, ``COMMENT``, ``AS``, ``CHANGEFEED``, ``PERMISSIONS``."""

    @pytest.mark.parametrize(("statement", "expected"), _TABLES, ids=[state.name for _, state in _TABLES])
    def test_golden(self, statement: str, expected: TableState) -> None:
        state = parse_define_table(statement)
        assert state == expected
        # Every attribute, not only the compared ones (TableState's equality compares them all, these are the exact
        # types the generator relies on).
        assert isinstance(state.relation_in, tuple) and isinstance(state.relation_out, tuple)
        assert dict(state.permissions) == dict(expected.permissions)

    def test_the_view_query_keeps_its_inner_as(self) -> None:
        state = parse_define_table(next(statement for statement, expected in _TABLES if expected.name == "v1"))
        assert state.view_query == "SELECT count() AS total, math::sum(n) AS s FROM src WHERE n > 1 GROUP ALL"
        assert (state.changefeed, state.changefeed_include_original) == ("2h", True)

    def test_from_and_to_are_read_as_in_and_out(self) -> None:
        state = parse_define_table("DEFINE TABLE l TYPE RELATION FROM a TO b | c SCHEMALESS PERMISSIONS NONE")
        assert (state.relation_in, state.relation_out) == (("a",), ("b", "c"))

    def test_defaults_without_type_or_schema_keyword(self) -> None:
        state = parse_define_table("DEFINE TABLE t PERMISSIONS NONE")
        assert (state.table_type, state.schema_mode) == ("any", "SCHEMALESS")
        assert parse_define_table("DEFINE TABLE t").permissions == {}

    @pytest.mark.parametrize(
        ("statement", "reason"),
        [
            ("DEFINE FIELD f ON t TYPE int PERMISSIONS FULL", _R_STATEMENT),
            ("DEFINE TABLES t", _R_STATEMENT),
            ("REMOVE TABLE t", _R_STATEMENT),
            ("", _R_STATEMENT),
            ("DEFINE TABLE", _R_HEADER),
            ("DEFINE TABLE ", _R_HEADER),
            ("DEFINE TABLE (t)", _R_HEADER),
            ("DEFINE TABLE t(x) TYPE ANY", _R_HEADER),
            ("DEFINE TABLE `t TYPE ANY", _R_IDENT),
            # An unknown clause after a non-expression clause; a clause twice; IN on a table that is not a relation.
            ("DEFINE TABLE t TYPE NORMAL SCHEMAFULL FROBNICATE 3 PERMISSIONS NONE", _R_NO_READING),
            ("DEFINE TABLE t TYPE NORMAL SCHEMAFULL SCHEMAFULL PERMISSIONS NONE", _R_NO_READING),
            ("DEFINE TABLE t TYPE NORMAL SCHEMAFULL PERMISSIONS NONE PERMISSIONS NONE", _R_NO_READING),
            ("DEFINE TABLE t TYPE NORMAL IN a SCHEMAFULL PERMISSIONS NONE", _R_NO_READING),
            ("DEFINE TABLE t TYPE VIEW SCHEMAFULL PERMISSIONS NONE", _R_NO_READING),
            ("DEFINE TABLE t TYPE RELATION IN a | PERMISSIONS NONE", _R_NO_READING),
            ("DEFINE TABLE t TYPE ANY SCHEMALESS CHANGEFEED soon PERMISSIONS NONE", _R_NO_READING),
            ("DEFINE TABLE t TYPE ANY SCHEMALESS COMMENT cv PERMISSIONS NONE", _R_NO_READING),
            ("DEFINE TABLE t TYPE ANY SCHEMALESS PERMISSIONS FOR select", _R_NO_READING),
            # Truncated.
            ("DEFINE TABLE t TYPE ANY SCHEMALESS PERMISSIONS FOR select WHERE (a", _R_BRACKETS),
            ("DEFINE TABLE t TYPE ANY SCHEMALESS COMMENT 'cv", _R_STRING),
        ],
    )
    def test_refused(self, statement: str, reason: str) -> None:
        with pytest.raises(DefineParseError) as error:
            parse_define_table(statement)
        assert _reason(error) == reason


def _field(statement: str, name: str, field_type: str = "any", **attributes: object) -> tuple[str, FieldState]:
    """A golden field statement and the state it reads as — every attribute by keyword, ``path=`` always passed."""
    table = statement.split(" ON ", 1)[1].split(" ", 1)[0].strip("`")
    defaults: dict[str, object] = {
        "nullable": False,
        "path": tuple(name.split(".")),
        "table": table,
        "permissions": _FIELD_FULL,
    }
    merged = {**defaults, **attributes, "raw": statement}
    return statement, FieldState(name=name, field_type=field_type, **merged)  # type: ignore[arg-type]


#: The probed fields of M6–M9, M20 and M21 (probe of 2026-10-18 on 2.7.0 and 3.3.4): ``(2.x, 3.x)`` renderings of
#: one definition and the state both read as. ``None`` where a line refuses the definition (M7 ``int | none`` on 2.x,
#: M20 ``REFERENCE``/``COMPUTED`` on 2.x).
_FIELD_PAIRS: list[tuple[str | None, str | None, str, str, dict[str, object]]] = [
    (
        "DEFINE FIELD age ON pt TYPE option<int> DEFAULT 18 PERMISSIONS FULL",
        "DEFINE FIELD age ON pt TYPE none | int DEFAULT 18 PERMISSIONS FULL",
        "age",
        "int",
        {"nullable": True, "default": "18"},
    ),
    (None, "DEFINE FIELD nick ON pt TYPE int | none PERMISSIONS FULL", "nick", "int", {"nullable": True}),
    (
        "DEFINE FIELD nick2 ON pt TYPE option<string> PERMISSIONS FULL",
        "DEFINE FIELD nick2 ON pt TYPE none | string PERMISSIONS FULL",
        "nick2",
        "string",
        {"nullable": True},
    ),
    (
        "DEFINE FIELD nested ON pt TYPE option<{ x: int, y: option<string> }> PERMISSIONS FULL",
        "DEFINE FIELD nested ON pt TYPE none | { x: int, y: none | string } PERMISSIONS FULL",
        "nested",
        "{ x: int, y: option<string> }",
        {"nullable": True},
    ),
    (
        "DEFINE FIELD g ON pt TYPE option<array<option<string>>> PERMISSIONS FULL",
        "DEFINE FIELD g ON pt TYPE none | array<none | string> PERMISSIONS FULL",
        "g",
        "array<option<string>>",
        {"nullable": True},
    ),
    (
        "DEFINE FIELD g[*] ON pt TYPE option<string> PERMISSIONS FULL",
        "DEFINE FIELD g.* ON pt TYPE none | string PERMISSIONS FULL",
        "g.*",
        "string",
        {"nullable": True},
    ),
    (
        "DEFINE FIELD tags ON pt TYPE array<string> PERMISSIONS FULL",
        "DEFINE FIELD tags ON pt TYPE array<string> PERMISSIONS FULL",
        "tags",
        "array<string>",
        {},
    ),
    (
        "DEFINE FIELD tags[*] ON pt TYPE string PERMISSIONS FULL",
        "DEFINE FIELD tags.* ON pt TYPE string PERMISSIONS FULL",
        "tags.*",
        "string",
        {},
    ),
    (
        "DEFINE FIELD w[*] ON pt TYPE object PERMISSIONS FULL",
        "DEFINE FIELD w.* ON pt TYPE object PERMISSIONS FULL",
        "w.*",
        "object",
        {},
    ),
    (
        "DEFINE FIELD w[*].z ON pt TYPE int PERMISSIONS FULL",
        "DEFINE FIELD w.*.z ON pt TYPE int PERMISSIONS FULL",
        "w.*.z",
        "int",
        {},
    ),
    (
        "DEFINE FIELD `first-name` ON pt TYPE option<string> PERMISSIONS FULL",
        "DEFINE FIELD `first-name` ON pt TYPE none | string PERMISSIONS FULL",
        "first-name",
        "string",
        {"nullable": True},
    ),
    (
        "DEFINE FIELD `select` ON pt TYPE option<string> PERMISSIONS FULL",
        "DEFINE FIELD `select` ON pt TYPE none | string PERMISSIONS FULL",
        "select",
        "string",
        {"nullable": True},
    ),
    (
        "DEFINE FIELD `with space` ON pt TYPE option<string> PERMISSIONS FULL",
        "DEFINE FIELD `with space` ON pt TYPE none | string PERMISSIONS FULL",
        "with space",
        "string",
        {"nullable": True},
    ),
    (
        "DEFINE FIELD meta ON pt FLEXIBLE TYPE object PERMISSIONS FULL",
        "DEFINE FIELD meta ON pt TYPE object FLEXIBLE PERMISSIONS FULL",
        "meta",
        "object",
        {"flexible": True},
    ),
    (
        "DEFINE FIELD created ON pt TYPE datetime DEFAULT time::now() READONLY PERMISSIONS FULL",
        "DEFINE FIELD created ON pt TYPE datetime DEFAULT time::now() READONLY PERMISSIONS FULL",
        "created",
        "datetime",
        {"default": "time::now()", "readonly": True},
    ),
    (
        "DEFINE FIELD x ON pt TYPE int DEFAULT ALWAYS 0 PERMISSIONS FULL",
        "DEFINE FIELD x ON pt TYPE int DEFAULT ALWAYS 0 PERMISSIONS FULL",
        "x",
        "int",
        {"default": "0", "default_always": True},
    ),
    (
        "DEFINE FIELD f ON pt TYPE option<int> DEFAULT 1 READONLY VALUE $value * 2 ASSERT $value > 0 COMMENT 'the name'"
        " PERMISSIONS FOR select, create FULL, FOR update NONE",
        "DEFINE FIELD f ON pt TYPE none | int DEFAULT 1 READONLY VALUE $value * 2 ASSERT $value > 0 COMMENT 'the name'"
        " PERMISSIONS FOR select, create FULL, FOR update NONE",
        "f",
        "int",
        {
            "nullable": True,
            "default": "1",
            "readonly": True,
            "value": "$value * 2",
            "assertion": "$value > 0",
            "comment": "the name",
            "permissions": {"select": "FULL", "create": "FULL", "update": "NONE"},
        },
    ),
    (
        "DEFINE FIELD p ON pt TYPE 1.5f | 2 PERMISSIONS FULL",
        "DEFINE FIELD p ON pt TYPE  1.5f | 2 PERMISSIONS FULL",
        "p",
        "1.5f | 2",
        {},
    ),
    (
        "DEFINE FIELD l ON pt TYPE 'dq' | \"it's\" PERMISSIONS FULL",
        "DEFINE FIELD l ON pt TYPE 'dq' | \"it's\" PERMISSIONS FULL",
        "l",
        "'dq' | \"it's\"",
        {},
    ),
    (
        "DEFINE FIELD ol ON pt TYPE option<'a' | 'b'> PERMISSIONS FULL",
        "DEFINE FIELD ol ON pt TYPE none | 'a' | 'b' PERMISSIONS FULL",
        "ol",
        "'a' | 'b'",
        {"nullable": True},
    ),
    (
        "DEFINE FIELD tup ON pt TYPE [int, string] PERMISSIONS FULL",
        "DEFINE FIELD tup ON pt TYPE [int, string] PERMISSIONS FULL",
        "tup",
        "[int, string]",
        {},
    ),
    (
        "DEFINE FIELD lit ON pt TYPE 1 | 2 | 3 PERMISSIONS FULL",
        "DEFINE FIELD lit ON pt TYPE 1 | 2 | 3 PERMISSIONS FULL",
        "lit",
        "1 | 2 | 3",
        {},
    ),
    (
        "DEFINE FIELD geo ON pt TYPE option<geometry<point | polygon>> PERMISSIONS FULL",
        "DEFINE FIELD geo ON pt TYPE none | geometry<point | polygon> PERMISSIONS FULL",
        "geo",
        "geometry<point | polygon>",
        {"nullable": True},
    ),
    (
        "DEFINE FIELD anyrec ON pt TYPE option<record> PERMISSIONS FULL",
        "DEFINE FIELD anyrec ON pt TYPE none | record PERMISSIONS FULL",
        "anyrec",
        "record",
        {"nullable": True},
    ),
    ("DEFINE FIELD noty ON pt PERMISSIONS FULL", "DEFINE FIELD noty ON pt PERMISSIONS FULL", "noty", "any", {}),
    (
        "DEFINE FIELD typeany ON pt TYPE any PERMISSIONS FULL",
        "DEFINE FIELD typeany ON pt TYPE any PERMISSIONS FULL",
        "typeany",
        "any",
        {},
    ),
    (
        "DEFINE FIELD owner ON pt TYPE option<string> PERMISSIONS FOR select, create FULL, FOR update NONE",
        "DEFINE FIELD owner ON pt TYPE none | string PERMISSIONS FOR select, create FULL, FOR update NONE",
        "owner",
        "string",
        {"nullable": True, "permissions": {"select": "FULL", "create": "FULL", "update": "NONE"}},
    ),
    # Field permissions: 3.x renders PERMISSIONS NONE as the three actions; no line renders delete on a field.
    (
        "DEFINE FIELD p3 ON pt TYPE int PERMISSIONS NONE",
        "DEFINE FIELD p3 ON pt TYPE int PERMISSIONS FOR select, create, update NONE",
        "p3",
        "int",
        {"permissions": dict.fromkeys(_FIELD_ACTIONS, "NONE")},
    ),
    (
        "DEFINE FIELD p4 ON pt TYPE int PERMISSIONS FOR select NONE, FOR create, update FULL",
        "DEFINE FIELD p4 ON pt TYPE int PERMISSIONS FOR select NONE, FOR create, update FULL",
        "p4",
        "int",
        {"permissions": {"select": "NONE", "create": "FULL", "update": "FULL"}},
    ),
    (
        "DEFINE FIELD p5 ON pt TYPE int PERMISSIONS FOR select, create, update WHERE a = 1",
        "DEFINE FIELD p5 ON pt TYPE int PERMISSIONS FOR select, create, update WHERE a = 1",
        "p5",
        "int",
        {"permissions": dict.fromkeys(_FIELD_ACTIONS, "WHERE a = 1")},
    ),
    (
        "DEFINE FIELD p6 ON pt TYPE int PERMISSIONS FOR select FULL, FOR create, update WHERE a = 1",
        "DEFINE FIELD p6 ON pt TYPE int PERMISSIONS FOR select FULL, FOR create, update WHERE a = 1",
        "p6",
        "int",
        {"permissions": {"select": "FULL", "create": "WHERE a = 1", "update": "WHERE a = 1"}},
    ),
    (
        "DEFINE FIELD email ON pt TYPE string VALUE string::lowercase($value) PERMISSIONS FULL",
        "DEFINE FIELD email ON pt TYPE string VALUE string::lowercase($value) PERMISSIONS FULL",
        "email",
        "string",
        {"value": "string::lowercase($value)"},
    ),
    (
        "DEFINE FIELD status ON pt TYPE 'active' | 'inactive' DEFAULT 'active' PERMISSIONS FULL",
        "DEFINE FIELD status ON pt TYPE 'active' | 'inactive' DEFAULT 'active' PERMISSIONS FULL",
        "status",
        "'active' | 'inactive'",
        {"default": "'active'"},
    ),
    (
        "DEFINE FIELD st2 ON pt TYPE string ASSERT $value INSIDE ['a', 'b'] PERMISSIONS FULL",
        "DEFINE FIELD st2 ON pt TYPE string ASSERT $value INSIDE ['a', 'b'] PERMISSIONS FULL",
        "st2",
        "string",
        {"assertion": "$value INSIDE ['a', 'b']"},
    ),
    (
        "DEFINE FIELD nm ON pt TYPE string ASSERT string::len($value) > 0 PERMISSIONS FULL",
        "DEFINE FIELD nm ON pt TYPE string ASSERT string::len($value) > 0 PERMISSIONS FULL",
        "nm",
        "string",
        {"assertion": "string::len($value) > 0"},
    ),
    (
        "DEFINE FIELD obj ON pt TYPE object DEFAULT {  } PERMISSIONS FULL",
        "DEFINE FIELD obj ON pt TYPE object DEFAULT {  } PERMISSIONS FULL",
        "obj",
        "object",
        {"default": "{  }"},
    ),
    (
        "DEFINE FIELD cm ON pt TYPE string COMMENT 'the name' PERMISSIONS FULL",
        "DEFINE FIELD cm ON pt TYPE string COMMENT 'the name' PERMISSIONS FULL",
        "cm",
        "string",
        {"comment": "the name"},
    ),
    # M20: 3.x only.
    (
        None,
        "DEFINE FIELD friend ON pt TYPE none | record<pt> REFERENCE ON DELETE UNSET PERMISSIONS FULL",
        "friend",
        "record<pt>",
        {"nullable": True, "reference": True, "on_delete": "UNSET"},
    ),
    (
        None,
        "DEFINE FIELD refs ON pt TYPE none | record<pt> REFERENCE ON DELETE THEN (UPDATE c SET x = 1) PERMISSIONS FULL",
        "refs",
        "record<pt>",
        {"nullable": True, "reference": True, "on_delete": "THEN (UPDATE c SET x = 1)"},
    ),
    (None, "DEFINE FIELD comp ON pt COMPUTED age * 2 PERMISSIONS FULL", "comp", "any", {"computed": "age * 2"}),
    (
        None,
        "DEFINE FIELD comp2 ON pt TYPE int COMPUTED age * 3 PERMISSIONS FULL",
        "comp2",
        "int",
        {"computed": "age * 3"},
    ),
    (None, "DEFINE FIELD back ON pt COMPUTED <~pt PERMISSIONS FULL", "back", "any", {"computed": "<~pt"}),
]

_FIELD_GOLDENS = [
    _field(statement, name, field_type, **attributes)
    for two, three, name, field_type, attributes in _FIELD_PAIRS
    for statement in (two, three)
    if statement is not None
]

#: M34: keyword-named identifiers, rendered bare (both lines; ``k5`` is 2.x only — 3.x refuses a bare ``FOR``).
_M34_FIELDS = [
    _field("DEFINE FIELD TYPE ON t TYPE int PERMISSIONS FULL", "TYPE", "int"),
    _field("DEFINE FIELD f ON TYPE TYPE int PERMISSIONS FULL", "f", "int"),
    _field("DEFINE FIELD ON ON t TYPE int PERMISSIONS FULL", "ON", "int", table="t"),
    _field("DEFINE FIELD g ON `TABLE` TYPE int PERMISSIONS FULL", "g", "int"),
    _field("DEFINE FIELD `IF` ON t TYPE int PERMISSIONS FULL", "IF", "int"),
    _field("DEFINE FIELD PERMISSIONS ON t TYPE int PERMISSIONS FULL", "PERMISSIONS", "int"),
    _field("DEFINE FIELD x1 ON t VALUE TYPE + 1 PERMISSIONS FULL", "x1", value="TYPE + 1"),
    _field("DEFINE FIELD x2 ON t VALUE END + COMMENT PERMISSIONS FULL", "x2", value="END + COMMENT"),
    _field("DEFINE FIELD x3 ON t DEFAULT PERMISSIONS PERMISSIONS FULL", "x3", default="PERMISSIONS"),
    _field("DEFINE FIELD x5 ON t VALUE THEN PERMISSIONS FULL", "x5", value="THEN"),
    _field("DEFINE FIELD k1 ON t VALUE a * READONLY PERMISSIONS FULL", "k1", value="a * READONLY"),
    _field("DEFINE FIELD k2 ON t DEFAULT READONLY READONLY PERMISSIONS FULL", "k2", default="READONLY", readonly=True),
    _field("DEFINE FIELD k3 ON t VALUE COMMENT COMMENT 'real' PERMISSIONS FULL", "k3", value="COMMENT", comment="real"),
    _field(
        "DEFINE FIELD k4 ON t VALUE a + ASSERT ASSERT $value > 0 PERMISSIONS FULL",
        "k4",
        value="a + ASSERT",
        assertion="$value > 0",
    ),
    _field("DEFINE FIELD k6 ON t VALUE `VALUE` + DEFAULT PERMISSIONS FULL", "k6", value="`VALUE` + DEFAULT"),
    _field(
        "DEFINE FIELD k5 ON t TYPE int PERMISSIONS FOR select WHERE `FOR` = 1, FOR create, update FULL",
        "k5",
        "int",
        permissions={"select": "WHERE `FOR` = 1", "create": "FULL", "update": "FULL"},
    ),
]

#: M35: ``;``, blocks and IF — the 2.x/embedded rendering, then the 3.x one (``\n`` a real newline).
_M35_FIELDS = [
    _field("DEFINE FIELD a ON t VALUE string::concat(b, ';', c) PERMISSIONS FULL", "a", value="string::concat(b, ';', c)"),
    _field(
        "DEFINE FIELD blk ON t VALUE {\nLET $x = 1;\nRETURN $x + 1;\n} PERMISSIONS FULL",
        "blk",
        value="{\nLET $x = 1;\nRETURN $x + 1;\n}",
    ),
    _field(
        "DEFINE FIELD blk ON t VALUE { LET $x = 1; RETURN $x + 1; } PERMISSIONS FULL",
        "blk",
        value="{ LET $x = 1; RETURN $x + 1; }",
    ),
    _field("DEFINE FIELD i1 ON t VALUE IF b THEN 1 ELSE 2 END PERMISSIONS FULL", "i1", value="IF b THEN 1 ELSE 2 END"),
    _field("DEFINE FIELD i1 ON t VALUE (IF b THEN 1 ELSE 2 END) PERMISSIONS FULL", "i1", value="(IF b THEN 1 ELSE 2 END)"),
    _field("DEFINE FIELD i2 ON t VALUE IF b { 1 } ELSE { 2 } PERMISSIONS FULL", "i2", value="IF b { 1 } ELSE { 2 }"),
    _field("DEFINE FIELD i2 ON t VALUE (IF b { 1 } ELSE { 2 }) PERMISSIONS FULL", "i2", value="(IF b { 1 } ELSE { 2 })"),
    _field(
        "DEFINE FIELD i3 ON t VALUE IF b THEN 1 ELSE IF c THEN 2 ELSE 3 END PERMISSIONS FULL",
        "i3",
        value="IF b THEN 1 ELSE IF c THEN 2 ELSE 3 END",
    ),
    _field(
        "DEFINE FIELD i3 ON t VALUE (IF b THEN 1 ELSE IF c THEN 2 ELSE 3 END) PERMISSIONS FULL",
        "i3",
        value="(IF b THEN 1 ELSE IF c THEN 2 ELSE 3 END)",
    ),
    _field(
        "DEFINE FIELD i4 ON t VALUE IF b { 1 } ELSE IF c { 2 } ELSE { 3 } PERMISSIONS FULL",
        "i4",
        value="IF b { 1 } ELSE IF c { 2 } ELSE { 3 }",
    ),
    _field(
        "DEFINE FIELD i4 ON t VALUE (IF b { 1 } ELSE IF c { 2 } ELSE { 3 }) PERMISSIONS FULL",
        "i4",
        value="(IF b { 1 } ELSE IF c { 2 } ELSE { 3 })",
    ),
    _field("DEFINE FIELD i5 ON t VALUE IF b THEN 1 END PERMISSIONS FULL", "i5", value="IF b THEN 1 END"),
    _field("DEFINE FIELD i5 ON t VALUE (IF b THEN 1 END) PERMISSIONS FULL", "i5", value="(IF b THEN 1 END)"),
    _field("DEFINE FIELD k7 ON t VALUE IF a THEN END ELSE b END PERMISSIONS FULL", "k7", value="IF a THEN END ELSE b END"),
    _field("DEFINE FIELD k7 ON t VALUE (IF a THEN END ELSE b END) PERMISSIONS FULL", "k7", value="(IF a THEN END ELSE b END)"),
]

#: M36: literal defaults, rendered alike by both lines but where noted: ``(name, TYPE, field_type, nullable, DEFAULT)``.
_M36_DEFAULTS = [
    ("d1", "float", "float", False, "0.5f"),
    ("d2", "decimal", "decimal", False, "1.5dec"),
    ("d3", "float", "float", False, "1000f"),
    ("d4", "int", "int", False, "-2"),
    ("d5", "datetime", "datetime", False, "d'2020-01-01T00:00:00Z'"),
    ("d6", "duration", "duration", False, "1h30m"),
    ("d7", "option<int>", "int", True, "NONE"),
    ("d7", "none | int", "int", True, "NONE"),  # 3.x
    ("d8", "null | int", "null | int", False, "NULL"),
    ("d9", "bool", "bool", False, "true"),
    ("d10", "string", "string", False, '"it\'s"'),
    ("d11", "array<int>", "array<int>", False, "[]"),
    ("d12", "float", "float", False, "1"),
    ("d13", "decimal", "decimal", False, "0.5f"),
    ("d14", "int", "int", False, "9223372036854775807"),
    ("d15", "uuid", "uuid", False, "u'0190d3b4-0000-7000-8000-000000000000'"),
    ("d16", "float", "float", False, "0.1f"),
    ("d17", "float", "float", False, "-0f"),
    ("d18", "string", "string", False, "'a\\nb'"),  # the servers
    ("d18", "string", "string", False, "'a\nb'"),  # the embedded engine: a raw newline
    ("d19", "int", "int", False, "1000"),
    ("d20", "float", "float", False, "math::INF"),  # 2.x and the embedded engine
    ("d20", "float", "float", False, "math::INFINITY"),  # 3.x
    ("d21", "string", "string", False, "'x'"),
    ("d22", "record", "record", False, "a:1"),
    ("d24", "float", "float", False, "0.0025f"),
]


class TestDefineField:
    """Spec §6.3.3 (AC-6, AC-7): ``DEFINE FIELD <idiom> ON <ident>`` read positionally, then the clauses in M10's
    order; the kind normalised (one result for both lines), expression texts kept as rendered."""

    @pytest.mark.parametrize(
        ("statement", "expected"), _FIELD_GOLDENS, ids=[f"{state.name}:{n}" for n, (_, state) in enumerate(_FIELD_GOLDENS)]
    )
    def test_golden(self, statement: str, expected: FieldState) -> None:
        parsed = parse_define_field(statement)
        assert isinstance(parsed, ParsedField)
        assert parsed.state == expected
        assert parsed.type_as_written is False
        assert parsed.state.path == expected.path and isinstance(parsed.state.path, tuple)

    @pytest.mark.parametrize(
        ("two", "three"),
        [(two, three) for two, three, *_ in _FIELD_PAIRS if two is not None and three is not None and two != three],
    )
    def test_both_renderings_read_alike(self, two: str, three: str) -> None:
        # AC-6: the 2.x and the 3.x rendering of one definition give equal states but raw.
        state_two = parse_define_field(two).state
        state_three = parse_define_field(three).state
        assert state_two.raw == two and state_three.raw == three
        assert dataclasses.replace(state_two, raw="") == dataclasses.replace(state_three, raw="")

    def test_no_type_reads_as_any(self) -> None:
        # M43: a field without TYPE has the server semantics of TYPE any; raw keeps the two apart.
        no_type = parse_define_field("DEFINE FIELD x ON t PERMISSIONS FULL").state
        typed = parse_define_field("DEFINE FIELD y ON t TYPE any PERMISSIONS FULL").state
        assert (no_type.field_type, no_type.nullable, no_type.raw) == ("any", False, "DEFINE FIELD x ON t PERMISSIONS FULL")
        assert dataclasses.replace(no_type, name="y", path=("y",), raw="") == dataclasses.replace(typed, raw="")

    def test_the_every_clause_fields_of_both_lines_differ_only_by_name_and_raw(self) -> None:
        # The probe's `f` on 2.x and the same definition named `g` on 3.x.
        two_text, three_text = next((two, three) for two, three, name, *_ in _FIELD_PAIRS if name == "f")
        two = parse_define_field(two_text or "").state
        three = parse_define_field((three_text or "").replace("FIELD f ON", "FIELD g ON")).state
        assert two != three
        assert dataclasses.replace(two, name="g", path=("g",), raw="") == dataclasses.replace(three, raw="")

    @pytest.mark.parametrize(
        ("statement", "expected"), _M34_FIELDS, ids=[f"{state.name}:{n}" for n, (_, state) in enumerate(_M34_FIELDS)]
    )
    def test_keyword_named_identifiers(self, statement: str, expected: FieldState) -> None:
        assert parse_define_field(statement).state == expected

    def test_keyword_named_tables(self) -> None:
        assert parse_define_field("DEFINE FIELD f ON TYPE TYPE int PERMISSIONS FULL").state.table == "TYPE"
        assert parse_define_field("DEFINE FIELD g ON `TABLE` TYPE int PERMISSIONS FULL").state.table == "TABLE"
        assert parse_define_field("DEFINE FIELD ON ON t TYPE int PERMISSIONS FULL").state.name == "ON"

    @pytest.mark.parametrize(
        ("statement", "expected"), _M35_FIELDS, ids=[f"{state.name}:{n}" for n, (_, state) in enumerate(_M35_FIELDS)]
    )
    def test_blocks_and_if(self, statement: str, expected: FieldState) -> None:
        assert parse_define_field(statement).state == expected

    @pytest.mark.parametrize(
        ("name", "kind", "field_type", "nullable", "default"),
        _M36_DEFAULTS,
        ids=[f"{row[0]}:{n}" for n, row in enumerate(_M36_DEFAULTS)],
    )
    def test_literal_defaults(self, name: str, kind: str, field_type: str, nullable: bool, default: str) -> None:
        statement = f"DEFINE FIELD {name} ON t TYPE {kind} DEFAULT {default} PERMISSIONS FULL"
        state = parse_define_field(statement).state
        assert state == _field(statement, name, field_type, nullable=nullable, default=default)[1]

    def test_an_unknown_clause_after_an_expression_is_part_of_it(self) -> None:
        # Spec §6.3.2: kept as rendered (§0.3).
        state = parse_define_field("DEFINE FIELD f ON t VALUE a FROBNICATE 3 PERMISSIONS FULL").state
        assert state.value == "a FROBNICATE 3"

    @pytest.mark.parametrize(
        ("statement", "reason"),
        [
            ("DEFINE TABLE t TYPE ANY SCHEMALESS PERMISSIONS NONE", _R_STATEMENT),
            ("DEFINE INDEX i ON t FIELDS a", _R_STATEMENT),
            ("DEFINE FIELDS f ON t", _R_STATEMENT),
            ("define field f ON t", _R_STATEMENT),
            ("DEFINE FIELD", _R_HEADER),
            ("DEFINE FIELD f", _R_HEADER),
            ("DEFINE FIELD f ON", _R_HEADER),
            ("DEFINE FIELD f t TYPE int", _R_HEADER),
            ("DEFINE FIELD f ONt TYPE int", _R_HEADER),
            ("DEFINE FIELD f ON t(x) TYPE int", _R_HEADER),
            ("DEFINE FIELD a-b ON t TYPE int", _R_HEADER),
            ("DEFINE FIELD a..b ON t TYPE int", _R_PATH),
            ("DEFINE FIELD a[0] ON t TYPE int", _R_PATH),
            ("DEFINE FIELD `a ON t TYPE int", _R_IDENT),
            # An unknown clause after a non-expression clause (a flag, a string); a clause twice; a clause that its
            # validator refuses. (After a TYPE, the second pass keeps them in the TYPE text: see the I3 tests.)
            ("DEFINE FIELD f ON t READONLY FROBNICATE 3 PERMISSIONS FULL", _R_NO_READING),
            ("DEFINE FIELD f ON t COMMENT 'c' FROBNICATE 3 PERMISSIONS FULL", _R_NO_READING),
            ("DEFINE FIELD f ON t COMMENT 'a' COMMENT 'b' PERMISSIONS FULL", _R_NO_READING),
            ("DEFINE FIELD f ON t READONLY READONLY PERMISSIONS FULL", _R_NO_READING),
            ("DEFINE FIELD f ON t REFERENCE ON DELETE FROB PERMISSIONS FULL", _R_NO_READING),
            ("DEFINE FIELD f ON t PERMISSIONS FOR select, delete NONE", _R_NO_READING),
            ("DEFINE FIELD f ON t FROBNICATE", _R_NO_READING),
            # Spec §9.2's fabricated ambiguous statement.
            ("DEFINE FIELD a ON t VALUE b ASSERT ASSERT c PERMISSIONS FULL", _R_AMBIGUOUS),
            # Truncated.
            ("DEFINE FIELD f ON t VALUE IF a THEN 1 PERMISSIONS FULL", _R_IF),
            ("DEFINE FIELD f ON t DEFAULT 'abc PERMISSIONS FULL", _R_STRING),
            ("DEFINE FIELD f ON t VALUE f(a PERMISSIONS FULL", _R_BRACKETS),
        ],
    )
    def test_refused(self, statement: str, reason: str) -> None:
        with pytest.raises(DefineParseError) as error:
            parse_define_field(statement)
        assert _reason(error) == reason

    @pytest.mark.parametrize(
        ("statement", "field_type"),
        [
            ("DEFINE FIELD f ON t TYPE frob< PERMISSIONS FULL", "frob<"),
            ("DEFINE FIELD f ON t TYPE option<int DEFAULT 1 PERMISSIONS FULL", "option<int"),
            # Spec §6.3.2 item 4: with TYPE only non-empty, a clause no reading fits after the TYPE (an unknown
            # one, one twice, one its validator refuses) is part of the TYPE text kept as written — issue I3.
            ("DEFINE FIELD f ON t TYPE int FROBNICATE 3 PERMISSIONS FULL", "int FROBNICATE 3"),
            ("DEFINE FIELD f ON t TYPE int READONLY FROBNICATE 3 PERMISSIONS FULL", "int READONLY FROBNICATE 3"),
            ("DEFINE FIELD f ON t TYPE int COMMENT 'a' COMMENT 'b' PERMISSIONS FULL", "int COMMENT 'a'"),
            ("DEFINE FIELD f ON t TYPE int PERMISSIONS FOR select, delete NONE", "int PERMISSIONS FOR select, delete NONE"),
            ("DEFINE FIELD f ON t FLEXIBLE TYPE object FLEXIBLE PERMISSIONS FULL", "object FLEXIBLE"),
        ],
    )
    def test_a_type_not_understood_is_kept_as_written(self, statement: str, field_type: str) -> None:
        # I3: the second pass of spec §6.3.2 item 4; the flag is the parser's, FieldState has none.
        parsed = parse_define_field(statement)
        assert parsed.type_as_written is True
        assert (parsed.state.field_type, parsed.state.nullable) == (field_type, False)
        assert parsed.state.raw == statement
        assert not hasattr(parsed.state, "type_as_written")

    def test_a_type_not_understood_keeps_the_other_clauses(self) -> None:
        parsed = parse_define_field("DEFINE FIELD f ON t TYPE option<int DEFAULT 1 READONLY PERMISSIONS FOR select NONE")
        assert parsed.state.default == "1" and parsed.state.readonly
        assert parsed.state.permissions == {"select": "NONE", "create": "FULL", "update": "FULL"}

    def test_an_ambiguous_statement_has_no_second_pass(self) -> None:
        with pytest.raises(DefineParseError) as error:
            parse_define_field("DEFINE FIELD a ON t TYPE frob< VALUE b ASSERT ASSERT c PERMISSIONS FULL")
        assert _reason(error) == _R_AMBIGUOUS


class TestPermissions:
    """Spec §6.3.3: ``NONE``/``FULL`` for every action, or ``FOR a[, b] <rule>`` groups; a rule is ``FULL``, ``NONE``
    or ``WHERE <expr>``; an action not named is ``FULL`` for a field and ``NONE`` for a table."""

    def test_full_and_none(self) -> None:
        assert parse_permissions("FULL", _FIELD_ACTIONS, unnamed="FULL") == _FIELD_FULL
        assert parse_permissions("NONE", _TABLE_ACTIONS, unnamed="NONE") == _TABLE_NONE
        assert parse_permissions(" NONE ", _FIELD_ACTIONS, unnamed="FULL") == dict.fromkeys(_FIELD_ACTIONS, "NONE")

    @pytest.mark.parametrize(
        ("text", "actions", "unnamed", "expected"),
        [
            (
                "FOR select WHERE owner = $auth.id, FOR create, update, delete NONE",
                _TABLE_ACTIONS,
                "NONE",
                {"select": "WHERE owner = $auth.id", "create": "NONE", "update": "NONE", "delete": "NONE"},
            ),
            (
                "FOR select, create FULL, FOR update NONE",
                _FIELD_ACTIONS,
                "FULL",
                {"select": "FULL", "create": "FULL", "update": "NONE"},
            ),
            (
                "FOR select, update WHERE a = 1, FOR create FULL, FOR delete NONE",
                _TABLE_ACTIONS,
                "NONE",
                {"select": "WHERE a = 1", "update": "WHERE a = 1", "create": "FULL", "delete": "NONE"},
            ),
            (
                # 2.x k5 (M34): a backticked FOR inside the rule.
                "FOR select WHERE `FOR` = 1, FOR create, update FULL",
                _FIELD_ACTIONS,
                "FULL",
                {"select": "WHERE `FOR` = 1", "create": "FULL", "update": "FULL"},
            ),
            (
                "FOR select WHERE f(a, FOR select) = 1",  # nested: not a group
                _FIELD_ACTIONS,
                "FULL",
                {"select": "WHERE f(a, FOR select) = 1", "create": "FULL", "update": "FULL"},
            ),
            (
                "FOR select WHERE a = 'x, FOR create NONE'",  # in a string: not a group
                _FIELD_ACTIONS,
                "FULL",
                {"select": "WHERE a = 'x, FOR create NONE'", "create": "FULL", "update": "FULL"},
            ),
            # An action not named: the fallback.
            ("FOR select NONE", _FIELD_ACTIONS, "FULL", {"select": "NONE", "create": "FULL", "update": "FULL"}),
            (
                "FOR select FULL",
                _TABLE_ACTIONS,
                "NONE",
                {"select": "FULL", "create": "NONE", "update": "NONE", "delete": "NONE"},
            ),
        ],
    )
    def test_groups(self, text: str, actions: tuple[str, ...], unnamed: str, expected: dict[str, str]) -> None:
        assert parse_permissions(text, actions, unnamed=unnamed) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "FULL NONE",
            "full",
            "FOR select",
            "FOR select WHERE",
            "FOR select WHERE a +",
            "FOR frob FULL",
            "FOR select FULL, FOR select NONE",
            "FOR select, FULL",
            "FOR delete NONE",
            "for select FULL",
            "FOR SELECT FULL",
            "FOR select FULL FOR create NONE",
            "FOR select FULL,",
            "FOR select FULL, NONE",
            "WHERE a = 1",
            "FOR select WHERE (a",
            "FOR select WHERE 'a",
            "FOR select FROB",
        ],
    )
    def test_refused(self, text: str) -> None:
        with pytest.raises(DefineParseError) as error:
            parse_permissions(text, _FIELD_ACTIONS, unnamed="FULL")
        assert _reason(error) == _R_PERMISSIONS


class TestParseErrorReasons:
    """Spec §6.3.6 and §8: ``DefineParseError`` carries one of the fixed reasons, never text from the statement."""

    @staticmethod
    def _tree() -> ast.Module:
        path = pathlib.Path(define_parser.__file__)
        return ast.parse(path.read_text(encoding="utf-8"), path.name)

    def test_the_reasons_are_the_spec_constants(self) -> None:
        constants: dict[str, object] = {}
        for node in self._tree().body:
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id.startswith("_R_"):
                    assert isinstance(node.value, ast.Constant), target.id
                    constants[target.id] = node.value.value
        assert constants == _SPEC_REASONS
        for name, text in _SPEC_REASONS.items():
            assert getattr(define_parser, name) == text

    def test_every_parse_error_takes_one_reason_constant(self) -> None:
        tree = self._tree()
        module_level = {
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "DefineParseError"
        ]
        assert calls, "the scan is vacuous"
        for call in calls:
            where = f"define_parser.py:{call.lineno}"
            assert len(call.args) == 1 and not call.keywords, where
            argument = call.args[0]
            assert isinstance(argument, ast.Name), where
            assert argument.id.startswith("_R_") and argument.id in module_level, where
        # No bare `raise DefineParseError`, and no _R_ name rebound anywhere but at module level.
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Name):
                assert node.exc.id != "DefineParseError", f"define_parser.py:{node.lineno}"
        stores = [
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id.startswith("_R_")
        ]
        assert sorted(stores) == sorted(_SPEC_REASONS)

    def test_define_parse_error_is_a_value_error(self) -> None:
        assert issubclass(DefineParseError, ValueError)
        with pytest.raises(ValueError, match="^a string literal is not terminated$"):
            unquote("'s3cr3t-0042")
