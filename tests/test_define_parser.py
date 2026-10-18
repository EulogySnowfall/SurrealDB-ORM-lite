"""Schema introspection (v0.30.0): the private parser of the ``DEFINE`` statements ``INFO`` returns (spec §6.3).

The scanner (spec §6.3.1; M11, M34, M35): string literals and their decoding, identifiers and field paths, the
top-level keyword candidates with ``IF`` units, top-level splitting; and the fixed reasons ``DefineParseError`` is
raised with (spec §6.3.6). Unit tests only (no server).
"""

import ast
import pathlib
import time

import pytest

from surreal_orm_lite import define_parser
from surreal_orm_lite.define_parser import (
    _R_BRACKETS,
    _R_HEADER,
    _R_IDENT,
    _R_IF,
    _R_PATH,
    _R_STRING,
    DefineParseError,
    canonical_name,
    read_identifier,
    read_idiom,
    split_top_level,
    top_level_keywords,
    unquote,
)

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
