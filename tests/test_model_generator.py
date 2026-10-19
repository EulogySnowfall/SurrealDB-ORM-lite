"""Schema introspection (v0.30.0): the private model generator behind ``ModelCodeGenerator`` (spec §6.4).

The output boundaries (spec §6.4.1, §8; AC-12 boundary part) — the only ways server text reaches the generated source:
``_py_str()`` (a Python string literal that ``ast.literal_eval`` reads back exactly, with ruff format's quote) and
``_comment()`` (one printable-ASCII comment line, no trailing whitespace, cut to 128 columns), on the §8 corpus and on
2,000 seeded random strings. The names (spec §6.4.2; M37, M41; AC-17 part): which tables can be modelled (S1, S6),
the class-name pass (tables, then each table's sub-model candidates) and the field-name pass (reserved sets, every
class name of the module, aliases, collisions), deterministic from one run to the next. Unit tests only (no server).
"""

import ast
import io
import keyword
import random
import subprocess
import sys
import tokenize
from pathlib import Path
from typing import Any

import pytest

from surreal_orm_lite.model_generator import (
    _AMBIGUOUS,
    _ANNOTATION_BUILTINS,
    _IMPORTABLE,
    _KEYWORDS,
    _S1_NOT_IDENTIFIER,
    _S6_KEYWORD,
    _UNQUERYABLE_TABLE_NAMES,
    _comment,
    _modelable,
    _Names,
    _py_str,
    _skip_reason,
)
from surreal_orm_lite.schema_state import FieldState, TableState

# Spec §8 / AC-12: the injection corpus — quotes, backslashes, line breaks, separators, payloads, non-ASCII.
CORPUS: tuple[str, ...] = (
    "",
    '"""',
    "'''",
    "'",
    '"',
    "\\",
    "\\n",
    "\n",
    "\r",
    "\r\n",
    "\t",
    "\x0c",
    "\x0b",
    " ",
    "  ",
    "x  ",
    "\u2028",
    "\u2029",
    "\x85",
    "\xa0",
    "\u200b",
    "\ufeff",
    "#",
    "# noqa",
    ")",
    "(",
    ";",
    "'; REMOVE TABLE x; '",
    '"; REMOVE TABLE x; "',
    "__import__('os').system('x')",
    '""")\nimport os\nos.system(\'x\')\n"""',
    'say "hi"',
    "it's",
    'it\'s "quoted"',
    "'a' \"b\"",
    "{x}",
    "{0}",
    "%s",
    "\\N{BULLET}",
    "\\x41",
    "end\\",
    "é",
    "naïve café",
    "分析",
    "🎲 dice",
    "\U000e0001",
    "\x01\x02\x1b[31m",
    "\x7f",
    "\x9b",
    "string::concat(a, ';', b)",
    "{ LET $x = 1; RETURN $x + 1; }",
    "first-name",
    "`we\\`ird`",
    "⟨with space⟩",
)


def _random_strings(count: int = 2000) -> list[str]:
    """``count`` strings from a fixed seed over printable, control, quote, backslash and non-ASCII code points."""
    rng = random.Random(30_000)
    printable = [chr(c) for c in range(0x20, 0x7F)]
    control = [chr(c) for c in (*range(0x00, 0x20), *range(0x7F, 0xA0))]
    special = ["'", '"', "\\", "\n", "\r", "#", "\u2028"]

    def non_ascii() -> str:
        while True:
            code = rng.randint(0xA0, 0x10FFFF)
            if not 0xD800 <= code <= 0xDFFF:  # surrogate-free
                return chr(code)

    strings = []
    for _ in range(count):
        chars = []
        for _ in range(rng.randint(0, 24)):
            pick = rng.random()
            if pick < 0.45:
                chars.append(rng.choice(printable))
            elif pick < 0.6:
                chars.append(rng.choice(control))
            elif pick < 0.8:
                chars.append(rng.choice(special))
            else:
                chars.append(non_ascii())
        strings.append("".join(chars))
    return strings


RANDOM = _random_strings()

# Spec M37, as measured: the table names the ORM's unescaped statements cannot use (case-insensitively).
M37_WORDS = (
    "alter break continue create define delete explain false for function if info insert let none null only outside "
    "rebuild relate remove return select sleep throw true update upsert"
)


def _field(table: str, name: str, kind: str = "string", **kwargs: Any) -> FieldState:
    """A field built by keyword, as the parser builds it (``path`` from the dotted name unless given)."""
    return FieldState(name=name, field_type=kind, table=table, **kwargs)


def _table(name: str, *fields: FieldState, table_type: str = "normal") -> TableState:
    return TableState(name=name, table_type=table_type, fields={f.name: f for f in fields})


def _bare(names: list[str]) -> list[TableState]:
    return [TableState(name=name) for name in names]


class TestBoundaries:
    """Spec §6.4.1 and §8: ``_py_str`` and ``_comment``."""

    @pytest.mark.parametrize("text", CORPUS)
    def test_py_str_round_trip_corpus(self, text: str) -> None:
        literal = _py_str(text)
        assert ast.literal_eval(literal) == text
        assert all(char.isprintable() for char in literal), literal
        tree = ast.parse(f"x = {literal}")
        assert len(tree.body) == 1
        assigned = tree.body[0]
        assert isinstance(assigned, ast.Assign)
        assert isinstance(assigned.value, ast.Constant)

    def test_py_str_round_trip_random(self) -> None:
        assert len(RANDOM) == 2000
        assert len(set(RANDOM)) > 1850  # the generator is not degenerate (short strings repeat)
        for text in RANDOM:
            literal = _py_str(text)
            assert ast.literal_eval(literal) == text, repr(text)
            assert all(char.isprintable() for char in literal), repr(text)

    def test_py_str_never_holds_a_raw_control_character(self) -> None:
        for text in (*CORPUS, *RANDOM):
            literal = _py_str(text)
            assert not any(char in literal for char in "\n\r\t\x0b\x0c\x00\x85\u2028\u2029"), repr(text)

    def test_py_str_quote_choice(self) -> None:
        assert _py_str('say "hi"') == "'say \"hi\"'"
        assert _py_str("it's") == '"it\'s"'
        assert _py_str("") == '""'
        assert _py_str("plain") == '"plain"'
        # Ties are double-quoted, the chosen quote is escaped, the other one is not.
        assert _py_str("'a' \"b\"") == '"\'a\' \\"b\\""'
        assert _py_str('it\'s "x" "y"') == '\'it\\\'s "x" "y"\''

    def test_py_str_escapes(self) -> None:
        assert _py_str("a\\b") == '"a\\\\b"'
        assert _py_str("a\nb\rc\td") == '"a\\nb\\rc\\td"'
        assert _py_str("\x00\x0c\x7f") == '"\\x00\\x0c\\x7f"'
        assert _py_str("\u2028") == '"\\u2028"'
        assert _py_str("\U000e0001") == '"\\U000e0001"'
        # Printable non-ASCII is kept as it is.
        assert _py_str("café 分析 🎲") == '"café 分析 🎲"'

    def test_py_str_is_what_ruff_format_writes(self, tmp_path: Path) -> None:
        """ruff format (``--line-length 128``, the generated module's check) changes none of the literals."""
        lines = [f"x{i} = {_py_str(text)}\n" for i, text in enumerate((*CORPUS, *RANDOM[:400]))]
        target = tmp_path / "literals.py"
        target.write_text("".join(lines), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "format", "--isolated", "--line-length", "128", "--diff", str(target)],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize("indent", [0, 4, 8])
    def test_comment_rules_corpus_and_random(self, indent: int) -> None:
        for text in (*CORPUS, *RANDOM):
            line = _comment(text, indent=indent)
            assert line.startswith("#"), repr(text)
            assert line == "#" or line.startswith("# "), repr(text)
            assert all(" " <= char <= "~" for char in line), repr(text)  # printable ASCII only
            assert "\n" not in line and "\r" not in line and "\x0c" not in line
            assert line == line.rstrip(), repr(text)  # W291
            assert indent + len(line) <= 128, repr(text)  # E501
            # One comment token, nothing else: it cannot end the line early nor open code.
            source = "class A:\n" + " " * 4 + line + "\n    x = 1\n"
            tokens = [t for t in tokenize.generate_tokens(io.StringIO(source).readline) if t.type == tokenize.COMMENT]
            assert [t.string for t in tokens] == [line], repr(text)
            tree = ast.parse(source)
            body = tree.body[0]
            assert isinstance(body, ast.ClassDef)
            assert len(body.body) == 1

    def test_comment_escapes_and_trailing_whitespace(self) -> None:
        assert _comment("x  ", indent=4) == "# x"
        assert _comment("", indent=0) == "#"
        assert _comment("a\nb", indent=0) == "# a\\nb"
        assert _comment("a\r\nb\x0cc", indent=0) == "# a\\r\\nb\\x0cc"
        assert _comment("é\u2028", indent=0) == "# \\xe9\\u2028"
        assert _comment("tab\tend", indent=0) == "# tab\\tend"
        assert _comment("trailing\n", indent=0) == "# trailing\\n"

    def test_comment_that_fits_is_unchanged(self) -> None:
        assert _comment("Comment: Companies", indent=4) == "# Comment: Companies"
        text = "y" * 124  # "# " + 124 = 126 columns at indent 2 → 128: fits exactly
        assert _comment(text, indent=2) == "# " + text
        assert len(_comment("y" * 126, indent=0)) == 128

    def test_comment_truncation(self) -> None:
        long = "z" * 300
        at4 = _comment(long, indent=4)
        assert len(at4) == 124
        assert at4.endswith("...")
        assert at4 == "# " + "z" * 119 + "..."
        at0 = _comment(long, indent=0)
        assert len(at0) == 128
        assert at0.endswith("...")
        # One column over → cut to exactly 128.
        over = _comment("y" * 127, indent=0)
        assert len(over) == 128
        assert over.endswith("...")
        # Whitespace before the cut is dropped, never left before the dots.
        spaced = _comment("w" * 120 + "      " + "v" * 20, indent=0)
        assert spaced == "# " + "w" * 120 + "..."

    def test_comment_truncates_escaped_text(self) -> None:
        line = _comment("\n" * 200, indent=8)
        assert len(line) == 120
        assert line.endswith("...")
        assert set(line[2:-3]) <= {"\\", "n"}


class TestNames:
    """Spec §6.4.2 (M37, M41; AC-17 part): modelable tables, class names, field names."""

    def test_reserved_sets(self) -> None:
        assert {
            "Any",
            "Literal",
            "Decimal",
            "datetime",
            "UUID",
            "BaseModel",
            "ConfigDict",
            "Field",
            "BaseSurrealModel",
            "BaseSurrealEdge",
            "SurrealConfigDict",
            "Computed",
            "computed",
            "Duration",
            "Range",
            "Geometry",
            "GeometryPoint",
            "GeometryLine",
            "GeometryPolygon",
            "GeometryMultiPoint",
            "GeometryMultiLine",
            "GeometryMultiPolygon",
            "GeometryCollection",
            "PointField",
            "LineStringField",
            "PolygonField",
            "MultiPointField",
            "MultiLineStringField",
            "MultiPolygonField",
            "GeometryCollectionField",
            "GeometryField",
            "RecordID",
        } == _IMPORTABLE
        assert {"str", "int", "float", "bool", "bytes", "list", "set", "dict", "tuple"} == _ANNOTATION_BUILTINS
        assert {"l", "O", "I"} == _AMBIGUOUS
        assert set(keyword.kwlist) <= _KEYWORDS
        assert set(keyword.softkwlist) <= _KEYWORDS
        # Fixed soft keywords: the generated names do not depend on the running Python (3.11 has no `type`,
        # 3.15 adds `lazy`).
        assert {"_", "case", "match", "type", "lazy"} <= _KEYWORDS

    def test_unqueryable_table_names_are_m37(self) -> None:
        words = M37_WORDS.split()
        assert len(words) == 28
        assert set(words) == _UNQUERYABLE_TABLE_NAMES

    @pytest.mark.parametrize("name", ["isp_company", "type", "value", "TYPE", "M", "ENFORCED", "string", "_x", "__"])
    def test_modelable(self, name: str) -> None:
        assert _modelable(name)
        assert _skip_reason(name) is None

    @pytest.mark.parametrize("name", ["isp-weird", "with space", "9lives", "", "a.b", "café", "x\n"])
    def test_not_an_identifier_s1(self, name: str) -> None:
        assert not _modelable(name)
        assert _skip_reason(name) == _S1_NOT_IDENTIFIER
        assert _S1_NOT_IDENTIFIER == "its name is not an identifier, which table_name requires"

    @pytest.mark.parametrize(
        "name",
        [
            "select",
            "Select",
            "SELECT",
            "only",
            "outside",
            "none",
            *sorted(_UNQUERYABLE_TABLE_NAMES),
            *sorted(word.upper() for word in _UNQUERYABLE_TABLE_NAMES),
        ],
    )
    def test_surrealql_keyword_s6(self, name: str) -> None:
        assert not _modelable(name)
        assert _skip_reason(name) == _S6_KEYWORD
        assert _S6_KEYWORD == "its name is a SurrealQL keyword the ORM cannot use as a table name"

    def test_class_names(self) -> None:
        names = _Names(
            _bare(
                [
                    "isp_company",
                    "game_tables",
                    "person",
                    "Person",
                    "range",
                    "field",
                    "i",
                    "o",
                    "class",
                    "_x",
                    "__",
                    "HTTPLog",
                    "none_",
                    "a__b",
                    "_9x",
                ]
            )
        )
        assert names.classes == {
            "HTTPLog": "HTTPLog",
            "Person": "Person",  # codepoint order: "Person" < "person"
            "_9x": "T9x",
            "__": "Table",
            "_x": "X",
            "a__b": "AB",
            "class": "Class",
            "field": "FieldModel",  # Field is imported
            "game_tables": "GameTables",  # no singularisation
            "i": "IModel",  # E742
            "isp_company": "IspCompany",
            "none_": "NoneModel",  # None is a keyword
            "o": "OModel",
            "person": "PersonModel",
            "range": "RangeModel",  # Range is imported
        }

    def test_unmodelable_tables_get_no_class(self) -> None:
        names = _Names(_bare(["select", "Select", "isp-weird", "isp_company"]))
        assert names.classes == {"isp_company": "IspCompany"}
        assert names.class_names == frozenset({"IspCompany"})

    def test_model_suffixes_count_up(self) -> None:
        names = _Names(_bare(["person", "Person", "PERSON", "person_model"]))
        # Codepoint order: PERSON, Person, person, person_model.
        assert names.classes == {
            "PERSON": "PERSON",
            "Person": "Person",
            "person": "PersonModel",
            "person_model": "PersonModelModel",
        }
        names = _Names(_bare(["range", "Range_", "range_"]))
        assert names.classes == {"Range_": "RangeModel", "range": "RangeModel2", "range_": "RangeModel3"}

    def test_sub_model_candidates(self) -> None:
        person = _table(
            "isp_person",
            _field("isp_person", "address", "object", nullable=True),
            _field("isp_person", "address.city"),
            _field("isp_person", "address.geo", "object"),
            _field("isp_person", "address.geo.lat", "float"),
            _field("isp_person", "meta", "object", flexible=True),  # no declared child: dict[str, Any]
            _field("isp_person", "w", "array<object>"),
            _field("isp_person", "w.*", "object"),
            _field("isp_person", "w.*.z", "int"),
            _field("isp_person", "v", "array<object, 3>"),  # no item definition: the array's item kind
            _field("isp_person", "v.*.q", "int"),
            _field("isp_person", "tags", "array<string>"),
            _field("isp_person", "tags.*"),
            _field("isp_person", "lit", "{ a: int }"),  # an object literal: dict[str, Any]
            _field("isp_person", "lit.a", "int"),
            _field("isp_person", "first-name", "object"),
            _field("isp_person", "first-name.x", "int"),
            _field("isp_person", "bad", "frob<"),  # I3: kept as written, never a candidate
            _field("isp_person", "bad.x", "int"),
        )
        names = _Names([person])
        assert list(names.sub_models.items()) == [
            (("isp_person", ("address",)), "IspPersonAddress"),
            (("isp_person", ("address", "geo")), "IspPersonAddressGeo"),
            (("isp_person", ("first-name",)), "IspPersonFirstName"),
            (("isp_person", ("v", "*")), "IspPersonVItem"),
            (("isp_person", ("w", "*")), "IspPersonWItem"),
        ]
        assert names.sub_model_class("isp_person", ("w", "*")) == "IspPersonWItem"
        assert names.class_names == frozenset(
            {"IspPerson", "IspPersonAddress", "IspPersonAddressGeo", "IspPersonFirstName", "IspPersonVItem", "IspPersonWItem"}
        )

    def test_sub_models_are_allocated_right_after_their_table(self) -> None:
        tables = [
            _table("isp_person_address"),
            _table("isp_person", _field("isp_person", "address", "object"), _field("isp_person", "address.city")),
        ]
        names = _Names(tables)
        assert names.classes == {"isp_person": "IspPerson", "isp_person_address": "IspPersonAddressModel"}
        assert names.sub_model_class("isp_person", ("address",)) == "IspPersonAddress"

    def test_sub_model_names_go_through_the_reservation(self) -> None:
        tables = [
            _table("geometry", _field("geometry", "point", "object"), _field("geometry", "point.x", "int")),
            _table("point", _field("point", "field", "object"), _field("point", "field.x", "int")),
            _table("t", _field("t", "日本", "object"), _field("t", "日本.x", "int")),
        ]
        names = _Names(tables)
        assert names.class_name("geometry") == "GeometryModel"  # an imported name
        assert names.sub_model_class("geometry", ("point",)) == "GeometryModelPoint"
        assert names.class_name("point") == "Point"
        assert names.sub_model_class("point", ("field",)) == "PointFieldModel"  # PointField is imported
        assert names.sub_model_class("t", ("日本",)) == "TField"  # nothing ASCII left of the column

    def test_field_names(self) -> None:
        columns = [
            "first-name",
            "class",
            "select",
            "save",
            "objects",
            "json",
            "schema",
            "model_config",
            "model_x",
            "_private",
            "9lives",
            "name",
            "in",
            "out",
            "café",
            "-",
            "a b",
            "id_",
            "_id",
        ]
        table = _table("isp_person", *(_field("isp_person", column, path=[column]) for column in columns))
        names = _Names([table])
        assert names.fields("isp_person") == {
            "-": ("field", "-"),
            "9lives": ("f_9lives", "9lives"),
            "_id": ("id_", "_id"),  # id is provided
            "_private": ("private", "_private"),
            "a b": ("a_b", "a b"),
            "café": ("caf_", "café"),
            "class": ("class_", "class"),
            "first-name": ("first_name", "first-name"),
            "id_": ("id__2", "id_"),  # "_id" took id_ first (codepoint order)
            "in": ("in_", "in"),
            "json": ("json_", "json"),
            "model_config": ("model_config_", "model_config"),
            "model_x": ("model_x_", "model_x"),
            "name": ("name", None),
            "objects": ("objects_", "objects"),
            "out": ("out", None),  # provided only on an edge
            "save": ("save_", "save"),
            "schema": ("schema_", "schema"),
            "select": ("select", None),  # a SurrealQL keyword, not a Python one
        }

    def test_field_name_collisions(self) -> None:
        table = _table(
            "t",
            *(_field("t", column, path=[column]) for column in ["first-name", "first_name", "first name", "x", "x_"]),
        )
        assert _Names([table]).fields("t") == {
            "first name": ("first_name", "first name"),
            "first-name": ("first_name_2", "first-name"),
            "first_name": ("first_name_3", "first_name"),
            "x": ("x", None),
            "x_": ("x_", None),
        }

    def test_reserved_field_names_ac17(self) -> None:
        columns = ["datetime", "computed", "list", "str", "Field", "Any", "l", "O", "I", "IspPersonAddress"]
        table = _table(
            "isp_person",
            *(_field("isp_person", column) for column in columns),
            _field("isp_person", "address", "object"),
            _field("isp_person", "address.city"),
            _field("isp_person", "updated", "datetime"),
            _field("isp_person", "x", "int", computed="a * 2"),
            _field("isp_person", "tags", "array<string>"),
            _field("isp_person", "name"),
            _field("isp_person", "Type"),
            _field("isp_person", "type"),
            _field("isp_person", "match"),
            _field("isp_person", "lazy"),
        )
        fields = _Names([table]).fields("isp_person")
        for column in columns:
            assert fields[column] == (column + "_", column), column
        for column in ["updated", "x", "tags", "name", "address", "Type"]:
            assert fields[column] == (column, None), column
        for column in ["type", "match", "lazy"]:  # soft keywords, on every Python
            assert fields[column] == (column + "_", column), column

    def test_column_named_like_another_tables_class(self) -> None:
        tables = [
            _table("isp_company"),
            _table("isp_log", _field("isp_log", "IspCompany"), _field("isp_log", "IspLog"), _field("isp_log", "Other")),
        ]
        assert _Names(tables).fields("isp_log") == {
            "IspCompany": ("IspCompany_", "IspCompany"),
            "IspLog": ("IspLog_", "IspLog"),
            "Other": ("Other", None),
        }

    def test_id_is_never_allocated(self) -> None:
        table = _table("t", _field("t", "id", "record"), _field("t", "x", "int"))
        assert _Names([table]).fields("t") == {"x": ("x", None)}

    def test_edge_does_not_allocate_in_out_id(self) -> None:
        edge = _table(
            "isp_likes",
            _field("isp_likes", "in", "record<isp_person>"),
            _field("isp_likes", "out", "record<isp_person>"),
            _field("isp_likes", "id", "record"),
            _field("isp_likes", "since", "datetime"),
            _field("isp_likes", "in_", "int"),
            _field("isp_likes", "_out", "int"),
            table_type="relation",
        )
        assert _Names([edge]).fields("isp_likes") == {
            "_out": ("out_", "_out"),  # out is provided by BaseSurrealEdge
            "in_": ("in__", "in_"),
            "since": ("since", None),
        }

    def test_nested_paths_allocate_their_top_level_column(self) -> None:
        """S5: a nested path whose parent is not defined still gives the parent an attribute name."""
        table = _table("t", _field("t", "a.b", "int"), _field("t", "tags.*"), _field("t", "with.dot", path=["with.dot"]))
        assert _Names([table]).fields("t") == {
            "a": ("a", None),
            "tags": ("tags", None),
            "with.dot": ("with_dot", "with.dot"),
        }

    @pytest.mark.parametrize(
        ("column", "usable"),
        [
            ("city", True),
            ("id", True),  # nothing is provided in a sub-model
            ("out", True),
            ("select", True),
            ("datetime", False),
            ("l", False),
            ("first-name", False),
            ("save", False),
            ("class", False),
            ("_x", False),
            ("model_x", False),
            ("Field", False),
            ("str", False),
            ("IspPerson", False),  # a class name of the module
            ("IspPersonAddress", False),
            ("café", False),
            ("type", False),
        ],
    )
    def test_sub_model_children(self, column: str, usable: bool) -> None:
        table = _table("isp_person", _field("isp_person", "address", "object"), _field("isp_person", "address.city"))
        assert _Names([table]).usable_child(column) is usable

    def test_deterministic(self) -> None:
        def build(order: int) -> _Names:
            tables = [
                _table(
                    "isp_person",
                    *(
                        _field("isp_person", column, path=[column])
                        for column in ["first-name", "first_name", "class", "save", "datetime", "address"]
                    ),
                    _field("isp_person", "address.city"),
                ),
                _table("person"),
                _table("Person"),
                _table("isp_person_address"),
                _table("select"),
                _table("range"),
            ]
            return _Names(tables if order > 0 else list(reversed(tables)))

        first, second, reversed_ = build(1), build(1), build(-1)
        for other in (second, reversed_):
            assert first.classes == other.classes
            assert list(first.classes.items()) == list(other.classes.items())
            assert first.sub_models == other.sub_models
            assert list(first.sub_models.items()) == list(other.sub_models.items())
            assert first.fields("isp_person") == other.fields("isp_person")
            assert list(first.fields("isp_person")) == list(other.fields("isp_person"))

    def test_generated_names_are_identifiers(self) -> None:
        tables = [
            _table(
                name,
                *(_field(name, column, path=[column]) for column in [*CORPUS[1:40], *RANDOM[:200]]),
            )
            for name in ["t", "isp_company", "I"]
        ]
        names = _Names(tables)
        for class_name in names.class_names:
            assert class_name.isascii() and class_name.isidentifier()
            assert not keyword.iskeyword(class_name)
        for table in ("t", "isp_company", "I"):
            attributes = [attribute for attribute, _ in names.fields(table).values()]
            assert len(set(attributes)) == len(attributes)
            for attribute in attributes:
                assert attribute.isascii() and attribute.isidentifier(), attribute
                assert attribute not in _KEYWORDS
                assert not attribute.startswith("_")
            for column, (attribute, alias) in names.fields(table).items():
                assert alias == (None if attribute == column else column)
